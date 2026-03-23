"""
Desktop audio capture using WASAPI loopback (Windows only).
Captures system audio output (loopback) AND the default microphone input,
mixing both streams into a single mono feed for transcription.
"""
import collections
import queue
import threading
import time
import traceback
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

import log
from wav_writer import WavWriter

# FFT window size for the spectrum visualizer.  2048 samples ≈ 43 ms at 48 kHz,
# giving ~23 Hz frequency resolution.  The deque keeps the most recent window
# and is refilled by the mixer loop at ~512 samples per chunk.
_FFT_SIZE = 2048
_N_BARS   = 32   # number of log-spaced frequency bands sent to the frontend

# On Windows, calling Pa_StopStream / Pa_CloseStream on a WASAPI loopback stream
# invokes ExitProcess() at the C level and kills the entire Python process.
# We work around this by parking retired stream objects and their PyAudio instances
# here so Python GC never calls __del__ → close() on them.  The PortAudio atexit
# handler (registered automatically when PyAudio() is constructed) will clean up
# all open streams/handles when the process exits normally.
_stream_graveyard: list = []


class AudioCapture:
    CHUNK_SIZE = 512
    FORMAT = pyaudio.paInt16

    def __init__(self, audio_queue: queue.Queue):
        self.audio_queue = audio_queue
        self.is_running = False
        self._pa: pyaudio.PyAudio | None = None
        self._loopback_stream = None
        self._mic_stream = None
        self._loopback_thread: threading.Thread | None = None
        self._mic_thread: threading.Thread | None = None
        self._mixer_thread: threading.Thread | None = None

        # Reported to Transcriber — always mono after mixing
        self.sample_rate: int | None = None
        self.channels: int = 1

        # Internal source queues
        self._loopback_q: queue.Queue = queue.Queue(maxsize=200)
        self._mic_q: queue.Queue = queue.Queue(maxsize=200)

        # Per-stream properties (set in start())
        self._loopback_channels: int = 1
        self._mic_rate: int | None = None
        self._mic_channels: int = 1
        self._has_mic: bool = False
        self._resample_up: int = 1
        self._resample_down: int = 1

        # WAV writer — set via start_wav() before start()
        self.wav_writer: WavWriter | None = None

        # Live RMS levels — read by app.py to push to the visualizer
        self.loopback_level: float = 0.0
        self.mic_level: float = 0.0

        # User-controlled gain multipliers (1.0 = no change, persisted via localStorage)
        self.loopback_gain: float = 1.0
        self.mic_gain: float = 1.0

        # Echo cancellation parameters (disabled by default — enable for speaker+mic setups)
        self.echo_cancel_enabled: bool = False
        self.echo_gate_ratio: float = 2.0
        self.echo_silence_floor: float = 0.005
        self.echo_spectral_sub: float = 0.6
        self.echo_hold_ms: int = 150
        self.echo_crossfade_ms: int = 30
        self.echo_mic_suppress_db: int = -18

        # Rolling sample buffers for the FFT spectrum visualizer (post-gain)
        self._lb_fft_buf:  collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._mic_fft_buf: collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._hann_window: np.ndarray | None = None   # precomputed; set on first use

    # ── Device discovery ──────────────────────────────────────────────────────

    def _find_loopback_device(self) -> dict:
        """
        Find the WASAPI loopback device for the current default audio output.
        Falls back gracefully when device names are truncated or don't match exactly.
        """
        wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = self._pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        default_name: str = default_output["name"]

        all_loopbacks = list(self._pa.get_loopback_device_info_generator())
        if not all_loopbacks:
            raise RuntimeError(
                "No WASAPI loopback devices found. "
                "Make sure your audio driver supports WASAPI loopback capture."
            )

        # 1. Exact substring match (the common case)
        for lb in all_loopbacks:
            if default_name in lb["name"] or lb["name"].startswith(default_name):
                return lb

        # 2. Prefix match — Windows can truncate long device names differently
        #    for the output vs its loopback counterpart
        prefix = default_name[:20]
        for lb in all_loopbacks:
            if prefix and prefix in lb["name"]:
                return lb

        # 3. Word-level match — e.g. "USB Audio" appears in both names
        words = [w for w in default_name.split() if len(w) >= 4]
        for lb in all_loopbacks:
            if any(w in lb["name"] for w in words):
                return lb

        # 4. Last resort: first available loopback device
        log.warn("audio", f"No loopback device matched '{default_name}'. "
                          f"Using '{all_loopbacks[0]['name']}' as fallback.")
        return all_loopbacks[0]

    def _find_mic_device(self) -> dict | None:
        """Find the system default microphone input device (WASAPI only)."""
        try:
            wasapi_idx = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        except Exception:
            wasapi_idx = None

        # Collect loopback indices so we never accidentally pick one as the mic
        try:
            loopback_indices = {
                int(d["index"]) for d in self._pa.get_loopback_device_info_generator()
            }
        except Exception:
            loopback_indices = set()

        # Prefer the WASAPI default input device
        try:
            wasapi_info = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_idx = wasapi_info.get("defaultInputDevice", -1)
            if default_idx >= 0:
                info = self._pa.get_device_info_by_index(default_idx)
                if (info.get("maxInputChannels", 0) > 0
                        and int(info["index"]) not in loopback_indices):
                    return info
        except Exception:
            pass

        # Fallback: first WASAPI input that isn't a loopback
        try:
            for i in range(self._pa.get_device_count()):
                info = self._pa.get_device_info_by_index(i)
                if wasapi_idx is not None and info.get("hostApi") != wasapi_idx:
                    continue
                if info.get("maxInputChannels", 0) <= 0:
                    continue
                if int(info["index"]) in loopback_indices:
                    continue
                if "[Loopback]" in info.get("name", ""):
                    continue
                return info
        except Exception:
            pass

        return None

    # ── WAV recording ──────────────────────────────────────────────────────

    def start_wav(self, path: str) -> None:
        """Open a WAV file for recording.  Call before start()."""
        # sample_rate isn't known yet — defer opening until start() sets it.
        self._wav_path = path

    def stop_wav(self) -> None:
        """Finalize and close the WAV file."""
        if self.wav_writer is not None:
            self.wav_writer.close()
            self.wav_writer = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, loopback_index: int | None = None, mic_index: int | None = None) -> None:
        """
        Start capture.  loopback_index / mic_index override auto-detection;
        pass mic_index=-1 to explicitly disable the microphone,
        or mic_index=-2 to receive mic audio injected from the browser
        (via inject_mic_data()) rather than opening a WASAPI device.
        """
        self._pa = pyaudio.PyAudio()

        # --- Loopback stream (required) ---
        if loopback_index is not None:
            lb_info = self._pa.get_device_info_by_index(loopback_index)
        else:
            lb_info = self._find_loopback_device()
        self.sample_rate = int(lb_info["defaultSampleRate"])
        self._loopback_channels = max(1, lb_info["maxInputChannels"])
        log.info("audio", f"Loopback: '{lb_info['name']}' @ {self.sample_rate} Hz, "
                          f"{self._loopback_channels} ch")
        self._loopback_stream = self._pa.open(
            format=self.FORMAT,
            channels=self._loopback_channels,
            rate=self.sample_rate,
            input=True,
            input_device_index=lb_info["index"],
            frames_per_buffer=self.CHUNK_SIZE,
        )

        # --- Microphone stream (best-effort) ---
        if mic_index == -2:
            # Browser mic — no WASAPI stream; audio arrives via inject_mic_data()
            self._mic_rate     = 48000   # browser AudioContext default
            self._mic_channels = 1
            self._has_mic      = True
            if self._mic_rate != self.sample_rate:
                g = gcd(self.sample_rate, self._mic_rate)
                self._resample_up   = self.sample_rate // g
                self._resample_down = self._mic_rate    // g
            log.info("audio", f"Mic: browser (inject_mic_data) @ {self._mic_rate} Hz, 1 ch")
            mic_info = None   # skip the WASAPI-open block below
        elif mic_index == -1:
            mic_info = None   # explicitly disabled by caller
        elif mic_index is not None:
            try:
                mic_info = self._pa.get_device_info_by_index(mic_index)
            except Exception as e:
                log.warn("audio", f"Specified mic device {mic_index} invalid: {e}")
                mic_info = None
        else:
            mic_info = self._find_mic_device()
        if mic_info:
            try:
                self._mic_rate = int(mic_info["defaultSampleRate"])
                self._mic_channels = max(1, mic_info["maxInputChannels"])
                self._mic_stream = self._pa.open(
                    format=self.FORMAT,
                    channels=self._mic_channels,
                    rate=self._mic_rate,
                    input=True,
                    input_device_index=mic_info["index"],
                    frames_per_buffer=self.CHUNK_SIZE,
                )
                self._has_mic = True
                if self._mic_rate != self.sample_rate:
                    g = gcd(self.sample_rate, self._mic_rate)
                    self._resample_up = self.sample_rate // g
                    self._resample_down = self._mic_rate // g
                log.info("audio", f"Mic: '{mic_info['name']}' @ {self._mic_rate} Hz, "
                                  f"{self._mic_channels} ch")
            except Exception as e:
                log.warn("audio", f"Mic unavailable: {e}")
                self._mic_stream = None
                self._has_mic = False
        else:
            log.info("audio", "No microphone device found — capturing loopback only.")

        # Open WAV writer now that sample_rate is known
        if hasattr(self, "_wav_path") and self._wav_path:
            self.wav_writer = WavWriter(self._wav_path, self.sample_rate)
            self._wav_path = None

        self.is_running = True

        self._loopback_thread = threading.Thread(
            target=self._capture_loop,
            args=(self._loopback_stream, self._loopback_q),
            daemon=True,
        )
        self._loopback_thread.start()

        if self._has_mic and self._mic_stream is not None:
            # Only start a capture thread when there's an actual WASAPI stream.
            # For browser mic (mic_index=-2) _mic_stream is None; data arrives
            # externally via inject_mic_data() which feeds _mic_q directly.
            self._mic_thread = threading.Thread(
                target=self._capture_loop,
                args=(self._mic_stream, self._mic_q),
                daemon=True,
            )
            self._mic_thread.start()

        self._mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True)
        self._mixer_thread.start()

    def stop(self) -> None:
        self.is_running = False
        # Wait for the capture and mixer threads to finish their current iteration
        # and exit naturally (they check is_running at the top of every loop).
        # Loopback/mic streams always have data so stream.read() returns quickly.
        for t in (self._loopback_thread, self._mic_thread, self._mixer_thread):
            if t:
                t.join(timeout=3)
        self._loopback_thread = None
        self._mic_thread = None
        self._mixer_thread = None
        # Park streams + PyAudio instance in the graveyard instead of closing them.
        # Pa_StopStream / Pa_CloseStream on a WASAPI loopback stream calls
        # ExitProcess() at the C level on Windows, killing the whole process.
        # Setting these to None would also trigger __del__ → close() on the stream
        # objects, so we keep live references here and let PortAudio's own atexit
        # handler (registered at PyAudio() construction time) clean up on exit.
        for s in (self._loopback_stream, self._mic_stream):
            if s is not None:
                _stream_graveyard.append(s)
        if self._pa is not None:
            _stream_graveyard.append(self._pa)
        self._loopback_stream = None
        self._mic_stream = None
        self._pa = None

    def compute_spectrum(self, buf: collections.deque) -> list[float]:
        """Return _N_BARS log-spaced frequency magnitudes from the sample buffer.

        Uses a Hann-windowed real FFT on the most recent _FFT_SIZE samples.
        Values are normalised to [0, 1] on a power-law scale suitable for display.
        Returns all-zeros if the buffer is too short.
        """
        if len(buf) < _FFT_SIZE // 4:
            return [0.0] * _N_BARS

        samples = np.array(buf, dtype=np.float32)
        n = len(samples)

        if self._hann_window is None or len(self._hann_window) != n:
            self._hann_window = np.hanning(n).astype(np.float32)

        windowed = samples * self._hann_window
        fft_mag  = np.abs(np.fft.rfft(windowed)) / (n * 0.5)   # normalise by window area
        freqs    = np.fft.rfftfreq(n, d=1.0 / (self.sample_rate or 48000))

        f_min  = 40.0
        f_max  = min(20000.0, (self.sample_rate or 48000) / 2.0)
        edges  = np.logspace(np.log10(f_min), np.log10(f_max), _N_BARS + 1)

        result: list[float] = []
        for i in range(_N_BARS):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            val  = float(np.mean(fft_mag[mask])) if mask.any() else 0.0
            # Power-law scale so quiet signals are still visible
            result.append(round(min(1.0, (val * 80) ** 0.5), 4))

        return result

    def inject_mic_data(self, data: bytes) -> None:
        """Push raw mono Int16 PCM bytes into the mic pipeline.

        Used by the browser-mic pathway (mic_index=-2): the browser captures
        audio via getUserMedia, converts it to Int16, and POSTs it to
        /api/audio/mic-chunk, which calls this method on the active capture.
        """
        if self.is_running and self._has_mic:
            try:
                self._mic_q.put_nowait(data)
            except queue.Full:
                pass   # drop rather than block

    # ── Capture threads ───────────────────────────────────────────────────────

    def _capture_loop(self, stream, out_queue: queue.Queue) -> None:
        while self.is_running:
            try:
                data = stream.read(self.CHUNK_SIZE, exception_on_overflow=False)
                try:
                    out_queue.put_nowait(data)
                except queue.Full:
                    pass
            except Exception:
                if not self.is_running:
                    break
                time.sleep(0.01)  # brief pause to avoid a tight error loop

    # ── Mixer thread ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_mono_float(data: bytes, channels: int) -> np.ndarray:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return samples

    def _mixer_loop(self) -> None:
        # Use list-based accumulation instead of np.concatenate on every drain.
        # np.concatenate allocates a new array every call and copies all existing
        # data — O(n²) over many calls.  Lists just append pointers, and a single
        # np.concatenate at emit time is bounded by a small number of chunks.
        lb_parts: list[np.ndarray] = []
        lb_len = 0
        mic_parts: list[np.ndarray] = []
        mic_len = 0
        # Cap internal buffers at 3 seconds to prevent unbounded growth if the
        # downstream audio_queue backs up.
        max_buf_samples = int((self.sample_rate or 48000) * 3.0)

        # Echo cancellation state
        _ec_hold_remaining = 0          # samples remaining in gate hold
        _ec_prev_gate: str = "both"     # previous gate decision
        _ec_crossfade_pos = 0           # current crossfade position (0 = done)

        while self.is_running:
            try:
                got_data = False

                # Drain loopback queue
                try:
                    while True:
                        data = self._loopback_q.get_nowait()
                        chunk = self._to_mono_float(data, self._loopback_channels)
                        lb_parts.append(chunk)
                        lb_len += len(chunk)
                        got_data = True
                except queue.Empty:
                    pass

                # Drain mic queue (resample to loopback rate if necessary)
                if self._has_mic:
                    try:
                        while True:
                            data = self._mic_q.get_nowait()
                            samples = self._to_mono_float(data, self._mic_channels)
                            if self._resample_up != 1 or self._resample_down != 1:
                                samples = resample_poly(
                                    samples, self._resample_up, self._resample_down
                                ).astype(np.float32)
                            mic_parts.append(samples)
                            mic_len += len(samples)
                            got_data = True
                    except queue.Empty:
                        pass

                # Flatten the part lists into contiguous arrays only when we
                # have enough data to emit chunks (amortizes the copy cost).
                if lb_parts and lb_len >= self.CHUNK_SIZE:
                    lb_buf = np.concatenate(lb_parts)
                    lb_parts.clear()
                    lb_len = 0
                else:
                    lb_buf = np.array([], dtype=np.float32)

                if mic_parts and mic_len >= self.CHUNK_SIZE:
                    mic_buf = np.concatenate(mic_parts)
                    mic_parts.clear()
                    mic_len = 0
                else:
                    mic_buf = np.array([], dtype=np.float32)

                # Emit mixed chunks whenever loopback has enough data
                lb_pos = 0
                mic_pos = 0
                while lb_pos + self.CHUNK_SIZE <= len(lb_buf):
                    lb_chunk = np.clip(
                        lb_buf[lb_pos:lb_pos + self.CHUNK_SIZE] * self.loopback_gain,
                        -1.0, 1.0,
                    )
                    lb_pos += self.CHUNK_SIZE

                    lb_rms = float(np.sqrt(np.mean(lb_chunk ** 2)))
                    self.loopback_level = lb_rms
                    self._lb_fft_buf.extend(lb_chunk.tolist())

                    if self._has_mic and mic_pos + self.CHUNK_SIZE <= len(mic_buf):
                        mic_chunk = np.clip(
                            mic_buf[mic_pos:mic_pos + self.CHUNK_SIZE] * self.mic_gain,
                            -1.0, 1.0,
                        )
                        mic_pos += self.CHUNK_SIZE
                        mic_rms = float(np.sqrt(np.mean(mic_chunk ** 2)))
                        self.mic_level = mic_rms
                        self._mic_fft_buf.extend(mic_chunk.tolist())

                        # ── Source-gated mixing with optional echo cancellation ──
                        if self.echo_cancel_enabled:
                            floor = self.echo_silence_floor
                            ratio = self.echo_gate_ratio
                            hold_samples = int(self.echo_hold_ms * (self.sample_rate or 16000) / 1000)
                            crossfade_samples = int(self.echo_crossfade_ms * (self.sample_rate or 16000) / 1000)
                            suppress_gain = 10 ** (self.echo_mic_suppress_db / 20.0)  # dB → linear

                            # Determine raw gate decision
                            if mic_rms < floor or lb_rms > mic_rms * ratio:
                                raw_gate = "loopback"
                            elif lb_rms < floor or mic_rms > lb_rms * ratio:
                                raw_gate = "mic"
                            else:
                                raw_gate = "both"

                            # Hold: if loopback was dominant, keep mic suppressed
                            # for hold_samples after loopback drops
                            if raw_gate == "loopback":
                                _ec_hold_remaining = hold_samples
                            elif _ec_hold_remaining > 0:
                                _ec_hold_remaining -= self.CHUNK_SIZE
                                if _ec_hold_remaining > 0 and raw_gate != "loopback":
                                    raw_gate = "loopback"

                            # Spectral subtraction: remove loopback frequencies from mic
                            if raw_gate in ("mic", "both") and self.echo_spectral_sub > 0 and lb_rms > floor:
                                fft_size = len(mic_chunk)
                                mic_fft = np.fft.rfft(mic_chunk, n=fft_size)
                                lb_fft = np.fft.rfft(lb_chunk, n=fft_size)
                                mic_mag = np.abs(mic_fft)
                                lb_mag = np.abs(lb_fft)
                                # Subtract scaled loopback magnitude; floor at 0
                                cleaned_mag = np.maximum(mic_mag - lb_mag * self.echo_spectral_sub, 0.0)
                                # Preserve original phase
                                mic_phase = np.angle(mic_fft)
                                mic_chunk = np.fft.irfft(cleaned_mag * np.exp(1j * mic_phase), n=fft_size).astype(np.float32)

                            # Build the mixed output based on gate decision
                            if raw_gate == "loopback":
                                target = lb_chunk + mic_chunk * suppress_gain
                                src = "loopback"
                            elif raw_gate == "mic":
                                target = mic_chunk
                                src = "mic"
                            else:
                                target = np.clip(lb_chunk + mic_chunk, -1.0, 1.0)
                                src = "both"

                            # Crossfade when gate state changes
                            if raw_gate != _ec_prev_gate and crossfade_samples > 0:
                                _ec_crossfade_pos = crossfade_samples
                            _ec_prev_gate = raw_gate

                            if _ec_crossfade_pos > 0:
                                fade_len = min(_ec_crossfade_pos, len(target))
                                fade_in = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
                                fade_out = 1.0 - fade_in
                                # Blend the tail of the previous output with the new target
                                target[:fade_len] = target[:fade_len] * fade_in + lb_chunk[:fade_len] * fade_out
                                _ec_crossfade_pos -= fade_len

                            mixed = np.clip(target, -1.0, 1.0)
                        else:
                            # Basic source gating (original behaviour — no echo cancellation)
                            if mic_rms < 0.005 or lb_rms > mic_rms * 2.0:
                                src = "loopback"
                                mixed = lb_chunk
                            elif lb_rms < 0.005 or mic_rms > lb_rms * 2.0:
                                src = "mic"
                                mixed = mic_chunk
                            else:
                                src = "both"
                                mixed = np.clip(lb_chunk + mic_chunk, -1.0, 1.0)
                    else:
                        self.mic_level = 0.0
                        mixed = lb_chunk
                        src = "loopback"

                    int16_bytes = (mixed * 32767).astype(np.int16).tobytes()

                    # Write to WAV (before queue — never lose audio even if queue is full)
                    sample_offset = -1
                    if self.wav_writer is not None:
                        sample_offset = self.wav_writer.write(int16_bytes)

                    try:
                        self.audio_queue.put_nowait((src, int16_bytes, sample_offset))
                    except queue.Full:
                        pass  # drop the chunk rather than blocking forever

                # Keep leftover samples (less than CHUNK_SIZE) for next iteration
                if lb_pos < len(lb_buf):
                    lb_parts.append(lb_buf[lb_pos:])
                    lb_len = len(lb_buf) - lb_pos
                if mic_pos < len(mic_buf):
                    mic_parts.append(mic_buf[mic_pos:])
                    mic_len = len(mic_buf) - mic_pos

                # Backpressure: if buffers grow beyond the cap, discard the oldest
                # data.  This prevents unbounded memory growth when the transcriber
                # can't keep up (e.g. slow diarizer).
                if lb_len > max_buf_samples:
                    lb_parts.clear()
                    lb_len = 0
                if mic_len > max_buf_samples:
                    mic_parts.clear()
                    mic_len = 0

                if not got_data:
                    time.sleep(0.005)

            except Exception:
                # Log but never let the mixer thread die silently
                traceback.print_exc()
                time.sleep(0.05)


def default_device_name_matches(output_name: str, loopback_name: str) -> bool:
    """Check if a loopback device corresponds to the given output device."""
    return output_name in loopback_name


def enumerate_audio_devices() -> dict:
    """
    Return lists of available loopback and microphone input devices.
    Creates and destroys a temporary PyAudio instance — safe to call
    even while recording is active.

    Input devices are filtered to WASAPI only (same API used for capture)
    to avoid showing the same physical device three times (MME / DirectSound /
    WASAPI) and to exclude loopback virtual devices from the mic list.
    """
    pa = pyaudio.PyAudio()
    try:
        loopbacks = [
            {"index": int(d["index"]), "name": d["name"]}
            for d in pa.get_loopback_device_info_generator()
        ]

        try:
            wasapi_idx = pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"]
        except Exception:
            wasapi_idx = None

        # Collect the loopback device indices so we can exclude them from mic list
        loopback_indices = {lb["index"] for lb in loopbacks}

        inputs = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            # WASAPI only — skip MME / DirectSound duplicates
            if wasapi_idx is not None and info.get("hostApi") != wasapi_idx:
                continue
            # Must have at least one input channel
            if info.get("maxInputChannels", 0) <= 0:
                continue
            # Exclude loopback virtual devices (they're already in the loopback list)
            if int(info["index"]) in loopback_indices:
                continue
            if "[Loopback]" in info.get("name", ""):
                continue
            inputs.append({"index": int(info["index"]), "name": info["name"]})

        return {"loopback": loopbacks, "input": inputs}
    finally:
        pa.terminate()
