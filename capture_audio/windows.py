"""
Desktop audio capture using WASAPI loopback (Windows only).
Captures system audio output (loopback) AND the default microphone input,
mixing both streams into a single mono feed for transcription.
"""
import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import resample_poly

from core import log as log
from capture_audio._webrtc import WebRTCMicProcessor
from capture_audio.wav_writer import WavWriter

# ── INPUT_DEBUG ──────────────────────────────────────────────────────────
# Verbose tracing of every input stream and the mixer that joins them.
# Enable by setting the env var INPUT_DEBUG=1 before launching the app
# (or flip the default below to True for a permanent on-state during
# debugging). Output is throttled per metric to ~once per second so the
# log stays readable. Leaves no stone unturned: per-chunk byte counts,
# RMS / peak, queue depths, ffmpeg stderr in real time, mixer routing
# decisions, AEC state, and gating reasons.
INPUT_DEBUG = os.environ.get("INPUT_DEBUG", "0").strip() not in ("", "0", "false", "False")


def _idbg(msg: str) -> None:
    if INPUT_DEBUG:
        log.info("input-debug", msg)


class _Throttle:
    """Per-key one-line-per-interval throttle for INPUT_DEBUG output."""
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._last: dict[str, float] = {}

    def ready(self, key: str) -> bool:
        if not INPUT_DEBUG:
            return False
        now = time.monotonic()
        if now - self._last.get(key, 0.0) >= self.interval:
            self._last[key] = now
            return True
        return False

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._last.clear()
        else:
            self._last.pop(key, None)

# FFT window size for the spectrum visualizer.  2048 samples ≈ 43 ms at 48 kHz,
# giving ~23 Hz frequency resolution.  The deque keeps the most recent window
# and is refilled by the mixer loop at ~512 samples per chunk.
_FFT_SIZE = 4096
_N_BARS   = 32   # number of log-spaced frequency bands sent to the frontend

# A stream must never be closed while a thread is inside read() on it:
# Pa_CloseStream frees the stream under that read and the process dies with an
# access violation (0xC0000005, reproduced 2026-09-24). A WASAPI loopback read
# waits for as long as its output is silent, which is how this first showed up,
# as the whole process exiting when a loopback stream was closed. The capture
# loops therefore never wait inside read() (they poll get_read_available()), so
# once a reader thread has been joined its stream is closed. A stream whose
# reader could not be confirmed gone is parked here instead, together with the
# PyAudio that owns it: PyAudio.terminate() closes every stream it opened, and
# so does the last Pa_Terminate(), so keeping the owner alive is what keeps the
# stream open.
#
# Nothing else may be kept alive. PortAudio enumerates devices only on the
# Pa_Initialize() that takes its reference count from zero, and every other
# PyAudio() just adds a reference, so while any instance lives the device list
# stays frozen at that moment. A device that has since gone (a headset
# unplugged, Bluetooth switched off, a laptop undocked) then fails in
# IMMDevice::Activate, which PortAudio's WASAPI host reports as
# paInsufficientMemory: "[Errno -9992] Insufficient memory" with gigabytes free.
# Parking every stopped capture here used to freeze the list after the first
# recording, until the app restarted (2026-09-24).
_stream_graveyard: list = []


def _is_parked(pa) -> bool:
    return any(p is pa for p in _stream_graveyard)


def _park(stream, pa) -> None:
    """Keep a stream that a thread may still be reading, and the PyAudio that
    owns it, alive for the life of the process (see _stream_graveyard). A
    loopback helper is never parked: killing it is always safe (its reader just
    sees the pipe end), and a parked one would capture for nothing until exit."""
    if isinstance(stream, _LoopbackChild):
        stream.close()
        stream = None
    if stream is not None:
        _stream_graveyard.append(stream)
    if pa is not None and not _is_parked(pa):
        _stream_graveyard.append(pa)


def _retire_stream(stream, reader: "threading.Thread | None", pa) -> None:
    """Close a stream whose reader thread has gone, or park it when that thread
    may still be inside read() (closing it then frees it under the read). A
    loopback helper is always killed: that is safe under a read."""
    if stream is None:
        return
    if isinstance(stream, _LoopbackChild):
        stream.close()
        return
    if reader is not None and reader.is_alive():
        log.warn("audio", "An audio reader thread did not exit; keeping its "
                          "stream open (devices refresh after a restart)")
        _park(stream, pa)
        return
    try:
        stream.close()
    except Exception as e:
        log.warn("audio", f"Closing an audio stream failed: {e}")


def _terminate_quietly(pa) -> None:
    """Terminate a PyAudio unless a parked stream belongs to it: terminate()
    closes every stream the instance opened. Terminating the last instance is
    what lets the next PyAudio() enumerate the devices afresh. No-op on None
    or error."""
    if pa is None or _is_parked(pa):
        return
    try:
        pa.terminate()
    except Exception:
        pass


def _open_failure(what: str, name: str, err: Exception) -> RuntimeError:
    """A device-open error the user can act on. PyAudio raises OSError(code,
    text), and PortAudio's WASAPI host reports every failure to activate an
    endpoint as paInsufficientMemory (-9992), whose text is "Insufficient
    memory", so the text alone always pointed at the wrong problem."""
    code = getattr(err, "errno", None)
    label = name.replace(" [Loopback]", "").strip() or "the selected device"
    return RuntimeError(
        f"Windows could not open the {what} device '{label}'. It may have been "
        f"disconnected, switched off or disabled. Reconnect it or choose another "
        f"device, then start again."
        + (f" (PortAudio error {code})" if isinstance(code, int) else ""))


def probe_render_endpoints(duration: float = 1.2) -> dict | None:
    """Ask Windows where audio is ACTUALLY playing right now.

    Runs capture_audio/render_probe.py as a subprocess (COM must never run
    in-process here; see render_probe.py) and returns its parsed JSON:
    active render endpoints with name, max peak over the sampling window,
    and default-Multimedia / default-Communications flags. Returns None on
    any failure - callers treat that as "no information".
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "render_probe.py")
    try:
        r = subprocess.run(
            [sys.executable, script, "--duration", str(duration)],
            capture_output=True, timeout=duration + 15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
        data = json.loads(out) if out else None
        if not data or not data.get("ok"):
            log.warn("audio", f"Render probe returned no data"
                              f"{': ' + str(data.get('error')) if data else ''}")
            return None
        return data
    except Exception as e:
        log.warn("audio", f"Render probe failed: {e}")
        return None

_LOOPBACK_CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "loopback_child.py")


class _DeviceSnapshot:
    """Read-only stand-in for PyAudio's device queries, built from the device
    report of a loopback helper process (a fresh PortAudio scan). The device
    resolution code (_resolve_loopback and friends) runs on it unchanged."""

    def __init__(self, report: dict):
        self._host = dict(report.get("wasapi") or {})
        self._devices = {int(d["index"]): d for d in report.get("devices") or []}

    def get_host_api_info_by_type(self, _api) -> dict:
        return dict(self._host)

    def get_device_info_by_index(self, index) -> dict:
        try:
            return dict(self._devices[int(index)])
        except (KeyError, TypeError, ValueError):
            raise IOError(f"Invalid device index {index}") from None

    def get_device_count(self) -> int:
        return len(self._devices)

    def get_loopback_device_info_generator(self):
        wasapi = self._host.get("index")
        return iter([dict(d) for d in self._devices.values()
                     if d.get("isLoopbackDevice")
                     and (wasapi is None or d.get("hostApi") == wasapi)])


def _fresh_device_snapshot(timeout: float = 15.0) -> _DeviceSnapshot | None:
    """The audio devices Windows has right now, scanned by a helper process.

    In this process PortAudio's device list is frozen at its first
    initialisation (see loopback_child.py), so a device list for the UI or for
    choosing a recording device must come from a helper: the warm spare
    (rescanned) when there is one, else a one-off process. None on failure;
    callers fall back to an in-process scan."""
    spare = _borrow_spare()
    if spare is not None:
        snapshot = spare.devices
        _return_spare(spare)
        return snapshot
    with _spare_lock:
        _refill_spare_locked()
    try:
        r = subprocess.run(
            [sys.executable, _LOOPBACK_CHILD, "--list"],
            capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        first = (r.stdout or b"").split(b"\n", 1)[0].strip()
        report = json.loads(first) if first else None
        if report and report.get("ok"):
            return _DeviceSnapshot(report)
        log.warn("audio", f"Fresh device scan returned no data"
                          f"{': ' + str(report.get('error')) if report else ''}")
    except Exception as e:
        log.warn("audio", f"Fresh device scan failed: {e}")
    return None


class _LoopbackChild:
    """A WASAPI loopback stream captured in a helper process
    (capture_audio/loopback_child.py), read through the two stream calls
    _capture_loop makes. The helper scans the devices Windows has at the moment
    it starts, where this process's PortAudio list is frozen at the first
    recording (2026-10-06: a call on newly connected headphones could not be
    found and was recorded one-sided). Killing the helper retires the stream,
    which an in-process WASAPI loopback stream can never do safely.

    Constructing one starts the helper and reads its device report
    (``.devices``); ``open()`` then starts capture on one device."""

    def __init__(self, timeout: float = 15.0):
        self.channels = 1
        self._dead = False
        self.warm = False   # set when it was the pre-started spare
        self._proc = subprocess.Popen(
            [sys.executable, _LOOPBACK_CHILD],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
            bufsize=0,   # unbuffered, so get_read_available() sees every byte
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        report = self._read_json(timeout)
        if not report.get("ok"):
            self.close()
            raise RuntimeError(f"loopback helper: {report.get('error')}")
        self.devices = _DeviceSnapshot(report)

    def _read_json(self, timeout: float) -> dict:
        box: list[bytes] = []
        t = threading.Thread(target=lambda: box.append(self._proc.stdout.readline()),
                             daemon=True)
        t.start()
        t.join(timeout)
        if not box or not box[0].strip():
            self.close()
            raise RuntimeError("loopback helper did not answer" if not box
                               else "loopback helper exited")
        return json.loads(box[0])

    def _send(self, cmd: dict) -> None:
        try:
            self._proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
            self._proc.stdin.flush()
        except Exception as e:
            self.close()
            raise RuntimeError(f"loopback helper: {e}") from None

    def rescan(self, timeout: float = 10.0) -> None:
        """Rescan the devices in this idle helper (PortAudio re-initialised,
        about 0.2 s), so a warm spare answers with what Windows has now."""
        self._send({"scan": True})
        report = self._read_json(timeout)
        if not report.get("ok"):
            self.close()
            raise RuntimeError(f"loopback helper: {report.get('error')}")
        self.devices = _DeviceSnapshot(report)

    def open(self, info: dict, channels: int, rate: int,
             frames_per_buffer: int, chunk: int, timeout: float = 10.0) -> None:
        self._send({"open": int(info["index"]), "channels": int(channels),
                    "rate": int(rate), "frames_per_buffer": int(frames_per_buffer),
                    "chunk": int(chunk)})
        reply = self._read_json(timeout)
        if not reply.get("ok"):
            self.close()
            raise RuntimeError(reply.get("error") or "open failed")
        self.channels = int(channels)

    def get_read_available(self) -> int:
        """Whole frames waiting in the pipe, like PyAudio's count of frames
        buffered, so a reader that only reads what is already there never
        blocks. A broken pipe means the helper exited: 0, and ``dead``."""
        if self._dead:
            return 0
        import ctypes
        import msvcrt
        avail = ctypes.c_ulong(0)
        try:
            ok = ctypes.windll.kernel32.PeekNamedPipe(
                ctypes.c_void_p(msvcrt.get_osfhandle(self._proc.stdout.fileno())),
                None, 0, None, ctypes.byref(avail), None)
        except Exception:
            ok = 0
        if not ok:
            self._dead = True
            return 0
        return avail.value // (self.channels * 2)

    def read(self, frames: int, exception_on_overflow: bool = False) -> bytes:
        want = frames * self.channels * 2
        data = bytearray()
        while not self._dead and len(data) < want:
            part = self._proc.stdout.read(want - len(data))
            if not part:
                break
            data += part
        data = bytes(data)
        if len(data) < want:
            # The helper exited. Pause so the reader loop does not spin; the
            # silence watchdog reopens the capture.
            self._dead = True
            time.sleep(0.05)
            raise IOError("loopback helper stopped")
        return data

    @property
    def dead(self) -> bool:
        return self._dead or self._proc.poll() is not None

    def close(self) -> None:
        self._dead = True
        try:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass

    def _drain_stderr(self) -> None:
        try:
            for line in self._proc.stderr:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    log.warn("audio", f"Desktop audio helper: {text}")
        except Exception:
            pass


# One idle helper is kept started ahead of time. Starting a Python process takes
# 3 to 5 s on this machine, which would delay every recording start and device
# switch by that much; an idle helper rescans in ~0.2 s instead. Taking it starts
# the next spare in the background.
_spare_lock = threading.Lock()
_spare: _LoopbackChild | None = None
_spare_refilling = False


def _refill_spare_locked() -> None:
    """Start a background spare unless one exists or is starting. Caller holds
    _spare_lock."""
    global _spare_refilling
    if _spare is not None or _spare_refilling:
        return
    _spare_refilling = True
    threading.Thread(target=_refill_spare, daemon=True).start()


def _refill_spare() -> None:
    global _spare, _spare_refilling
    try:
        child = _LoopbackChild()
    except Exception as e:
        log.warn("audio", f"Could not start a spare desktop audio helper: {e}")
        child = None
    with _spare_lock:
        _spare_refilling = False
        if child is not None and _spare is None:
            _spare, child = child, None
    if child is not None:
        child.close()


def prewarm_loopback_helper() -> None:
    """Start the spare helper now, so the first recording does not wait for one."""
    with _spare_lock:
        _refill_spare_locked()


def _borrow_spare() -> _LoopbackChild | None:
    """The spare, rescanned so its device list is current, or None. The caller
    owns it until it is opened, closed, or handed back with _return_spare()."""
    global _spare
    with _spare_lock:
        child, _spare = _spare, None
    if child is None:
        return None
    try:
        if child.dead:
            raise RuntimeError("it had exited")
        child.rescan()
        child.warm = True
        return child
    except Exception as e:
        child.close()
        log.warn("audio", f"Spare desktop audio helper unusable ({e}); starting a new one")
        return None


def _return_spare(child: _LoopbackChild) -> None:
    global _spare
    with _spare_lock:
        if _spare is None and not child.dead:
            _spare, child = child, None
    if child is not None:
        child.close()


def _take_loopback_child() -> _LoopbackChild:
    """A helper with a device scan from right now: the warm spare when there is
    one, otherwise a newly started process. Either way the next spare starts."""
    child = _borrow_spare()
    with _spare_lock:
        _refill_spare_locked()
    return child or _LoopbackChild()


# Guards the per-source Opus encode, which now runs after a recording has
# already been reported as stopped. Module level (not per instance) because the
# capture object that produced the temp WAVs is not the one that resumes them.
_PER_SOURCE_ENCODE_LOCK = threading.Lock()



def _follow_output_enabled() -> bool:
    """The loopback_follow_output preference. Imported lazily so this module
    keeps loading in the audio self-test, which runs without the app's settings."""
    try:
        from core import settings as _settings
        return bool(_settings.get("loopback_follow_output", False))
    except Exception:
        return False


# How long the watchdog keeps the Communications-role device through a silent
# stretch while some OTHER output is audibly playing, before it concludes the
# call is not on the comms device at all and follows the audio.
STICKY_COMMS_HOLD_SEC = 90.0


def follow_decision(*, held: str | None, held_had_signal: bool, silent_for: float,
                    comms: str | None, target: str | None,
                    sticky_hold: float = STICKY_COMMS_HOLD_SEC) -> tuple[str, str]:
    """The one decision the watchdog makes once the render probe names a
    playing endpoint: ``("switch", why)`` to move the loopback onto *target*,
    ``("hold", why)`` to stay on the current device, or ``("none", why)``.

    *held* is the loopback device we capture now (with its " [Loopback]"
    suffix), *comms* the default Communications endpoint the probe reported,
    *target* the endpoint the probe says is actually playing, *silent_for* how
    long the held device has produced nothing, *held_had_signal* whether it
    ever produced anything in this recording.

    The comms device is sticky only while it is plausibly mid-call: it has
    produced signal before and went quiet less than *sticky_hold* ago (a
    far-end pause while music or a notification plays elsewhere). A comms
    device that never produced signal, or has been silent longer than the hold
    while another output is audibly playing, is not carrying the call, and the
    loopback follows the audio. 2026-09-15: Teams was pinned to the Realtek
    headphone jack while Windows' comms default was the Shure; an
    unconditional hold kept the loopback on the silent Shure for three whole
    calls even though every probe saw the call playing on the Realtek jack.
    Pure: no I/O, so the sequence is unit-testable.
    """
    if not target:
        return "none", "nothing is playing"
    held = held or ""
    if held.removesuffix(" [Loopback]") == target:
        return "none", f"already capturing '{target}'"
    on_comms = bool(comms) and comms in held
    if on_comms and target != comms:
        if held_had_signal and silent_for < sticky_hold:
            return ("hold", f"call device '{comms}' went quiet {silent_for:.0f}s ago; "
                            f"not leaving it for '{target}'")
        if held_had_signal:
            why = f"call device '{comms}' has been silent for {silent_for:.0f}s"
        else:
            why = f"call device '{comms}' has never produced signal"
        return "switch", f"{why} while '{target}' is playing; following the audio"
    return "switch", f"'{target}' is playing"


class AudioCapture:
    CHUNK_SIZE = 512
    FORMAT = pyaudio.paInt16

    def __init__(self, audio_queue: queue.Queue):
        self.audio_queue = audio_queue
        self.is_running = False
        self._pa: pyaudio.PyAudio | None = None
        # Device resolution reads _devices: the loopback helper's fresh scan,
        # or _pa when the helper cannot start (see _open_start_loopback).
        self._devices = None
        self._loopback_stream = None
        self._mic_stream = None
        self._loopback_thread: threading.Thread | None = None
        self._mic_thread: threading.Thread | None = None
        self._mixer_thread: threading.Thread | None = None

        # Reported to Transcriber - always mono after mixing
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
        self._mic_buf_size: int = 512
        self._resample_up: int = 1
        self._resample_down: int = 1

        # WAV writer - set via start_wav() before start()
        self.wav_writer: WavWriter | None = None
        self._wav_path: str | None = None
        self._wav_append: bool = False

        # Per-source recording ("mic = Me" feature). When enabled and a mic is
        # present, the mixer also writes a mic-only and a desktop-only track to
        # temp WAVs sample-aligned with the mix; on stop() they are encoded to
        # Opus ({sid}_mic.opus / {sid}_desktop.opus) and the temp WAVs deleted.
        # Reanalysis re-separates from these so mic audio is always the app user.
        self.mic_is_me_enabled: bool = False
        self._mic_wav_writer: WavWriter | None = None
        self._desktop_wav_writer: WavWriter | None = None
        self._mic_wav_path: str | None = None
        self._desktop_wav_path: str | None = None
        self._per_source_active: bool = False

        # Live RMS levels - read by app.py to push to the visualizer
        self.loopback_level: float = 0.0
        self.mic_level: float = 0.0
        # Peak-hold since the last take_peaks(), for the silence watchdog. It
        # polls every couple of seconds, and a single instantaneous RMS read at
        # that cadence lands in the gap between two words all the time: it was
        # reporting a live call as silent and firing the capture alarm during
        # ordinary conversation. A peak over the whole interval answers the
        # question actually being asked, "did this device produce anything".
        self.loopback_peak: float = 0.0
        self.mic_peak: float = 0.0

        # Device names + first-valid-audio flags. The capture loops emit a
        # one-shot "Verified audio device ..." log line as soon as a non-zero
        # PCM chunk arrives, so we can tell at-a-glance whether each stream
        # actually produced audio (vs. opening successfully and going silent).
        self._loopback_device_name: str = ""
        self._mic_device_name: str = ""
        self._loopback_verified: bool = False
        self._mic_verified: bool = False

        # Real-signal tracking for the loopback silence watchdog. `_verified`
        # flips on a single non-zero byte (unreliable), so `loopback_had_signal`
        # tracks whether the loopback ever produced ACTUAL audio above the noise
        # floor. If it stays dead, the watchdog fires on_loopback_silent so a call
        # whose audio plays to a device we are not capturing never fails silently.
        self.loopback_had_signal: bool = False
        self.on_loopback_silent = None       # optional callback(dev_name, kind)
        # Fired once when the loopback comes back after an alarm, so the UI can
        # take its banner down by itself instead of leaving the user to dismiss
        # a warning about a problem that has already gone away.
        self.on_loopback_recovered = None    # optional callback(dev_name)
        self._silence_watchdog: threading.Thread | None = None
        # Live device following: which PyAudio owns the loopback stream (None
        # when a loopback helper process captures it, the normal case; self._pa
        # only in the in-process fallback), and a lock serialising switches with
        # each other and with stop(). A switch opens the new stream in a new
        # helper first and then retires the old one (see _LoopbackChild).
        self._loopback_pa = None
        self._loopback_restart_lock = threading.Lock()
        # When a mid-recording switch lands on a device whose native mix format
        # differs from the one bound at start (e.g. a Bluetooth/USB call endpoint
        # is mono, or 16 kHz HFP), the mixer must resample the switched loopback
        # back to the pipeline rate (self.sample_rate, fixed at start for the WAV
        # + transcriber). 1/1 means no resampling (the common 48 kHz case).
        self._loopback_resample_up: int = 1
        self._loopback_resample_down: int = 1
        # Communications-role default endpoint name from the most recent render
        # probe (or None). Used to keep the loopback "sticky" on the call device
        # through a far-end pause, rather than chasing an unrelated sound.
        self._last_probe_comms: str | None = None
        # The desktop device the user selected, by the name it carries now, set
        # by _resolve_loopback() at start. While the capture is on another output
        # because that device was unavailable (missing at start, or it went away
        # mid-recording), the watchdog switches back as soon as it exists again.
        # None when there is nothing to return to: no saved selection, or Follow
        # call audio, where following decides the device.
        self._selected_loopback_name: str | None = None

        # User-controlled gain multipliers (1.0 = no change, persisted via localStorage)
        self.loopback_gain: float = 1.0
        self.mic_gain: float = 1.0

        # Echo cancellation (disabled by default - enable for speaker+mic setups)
        self.echo_cancel_enabled: bool = False
        # Noise suppression, independent of echo cancellation. Echo cancellation
        # always includes it; this flag enables it on its own (e.g. to stop the
        # custom AGC boosting quiet background noise when echo cancellation is off).
        self.noise_suppress_enabled: bool = False

        # Automatic gain control (soft compressor / normaliser)
        self.agc_loopback_enabled: bool = True
        self.agc_mic_enabled: bool = True
        self.agc_target_rms: float = 0.15
        self.agc_max_gain: float = 4.0
        self.agc_gate_threshold: float = 0.005

        # Live AGC debug state (read by the level-push loop for the UI)
        self.agc_lb_gain: float = 1.0
        self.agc_lb_envelope: float = 0.0
        self.agc_lb_gated: bool = True
        self.agc_mic_gain: float = 1.0
        self.agc_mic_envelope: float = 0.0
        self.agc_mic_gated: bool = True

        # Rolling sample buffers for the FFT spectrum visualizer (post-gain)
        self._lb_fft_buf:  collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._mic_fft_buf: collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._hann_window: np.ndarray | None = None   # precomputed; set on first use

        # FFmpeg subprocess mic capture (mic_index=-3)
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_mic_name: str | None = None
        self._ffmpeg_stderr_thread: threading.Thread | None = None
        # The DirectShow mic the user selected ("ffmpeg:<name>" in the mic menu),
        # by its saved name; None for every other mic choice. While the mic is
        # captured from anything else (the default mic standing in because the
        # selected one was missing at start or dropped mid-recording), the
        # watchdog switches back the moment it is available again, the same
        # rule as the desktop device (_check_selected_mic).
        self._selected_mic_name: str | None = None
        self._mic_on_selected: bool = False
        # When a mic reader last delivered data. A live mic sends samples all
        # the time, silence included, so a gap means the device is gone even if
        # its ffmpeg has not exited.
        self._mic_last_data_ts: float = 0.0
        # Serialises mic switches with each other and with stop().
        self._mic_switch_lock = threading.Lock()

        # INPUT_DEBUG bookkeeping. Counters are advanced from the capture
        # threads and the mixer; the throttle ensures we emit summaries at
        # most once per second per key so the log isn't drowned.
        self._idbg_throttle = _Throttle(interval=1.0)
        self._idbg_lb_bytes = 0
        self._idbg_mic_bytes = 0
        self._idbg_lb_chunks = 0
        self._idbg_mic_chunks = 0
        self._idbg_lb_zero_chunks = 0
        self._idbg_mic_zero_chunks = 0
        self._idbg_mic_inject_bytes = 0
        self._idbg_mix_src_counts: dict[str, int] = {"loopback": 0, "mic": 0, "both": 0}
        self._idbg_mix_emitted = 0
        self._idbg_audio_q_full_drops = 0
        self._idbg_mic_q_full_drops = 0
        self._idbg_lb_q_full_drops = 0

    # ── Device discovery ──────────────────────────────────────────────────────

    @staticmethod
    def _compute_mic_buffer_size(mic_info: dict) -> int:
        """Compute a frames_per_buffer for the mic aligned with the WASAPI device period.

        WASAPI shared mode delivers data in chunks tied to the device's period
        (typically 10 ms).  A too-small buffer causes underruns/glitches because
        PortAudio's internal ring buffer can't bridge the timing gap reliably.
        We derive a safe size from the device's reported high-input latency and
        round up to the next power of two (required by some drivers, and always
        safe for FFT-friendly alignment).
        """
        rate = int(mic_info["defaultSampleRate"])
        latency = mic_info.get("defaultHighInputLatency", 0.02)
        frames = int(rate * latency)
        frames = max(1024, min(frames, 8192))
        power = 1
        while power < frames:
            power <<= 1
        return power

    @staticmethod
    def _compute_loopback_buffer_size(lb_info: dict) -> int:
        """frames_per_buffer for the desktop (loopback) stream: the mic rule with a
        2048-frame floor.

        The loopback used to open with CHUNK_SIZE (512) frames while the reader
        blocks in stream.read(512). PortAudio's blocking read on WASAPI can wait
        up to ~47 ms between returns, its ring buffer (sized from
        frames_per_buffer) overflowed, and the excess was dropped silently, with
        no overflow error: about 3 percent of the far end on the Creative BT-W6
        and on the Realtek speakers. The mixer then padded every missing chunk
        with 10.7 ms of digital silence, heard as popping and crackling whenever
        the other side talked (2026-09-21). Measured: 512 loses 3.1 percent,
        1024 and up lose 0.1 percent (the device's own clock offset). The read
        chunk stays CHUNK_SIZE; only the ring buffer grows.
        """
        rate = int(lb_info.get("defaultSampleRate") or 48000)
        latency = lb_info.get("defaultHighInputLatency", 0.02)
        frames = int(rate * latency)
        frames = max(2048, min(frames, 8192))
        power = 1
        while power < frames:
            power <<= 1
        return power

    def _find_loopback_device(self, pa=None) -> dict:
        """
        Find the WASAPI loopback device for the current default audio output.
        Falls back gracefully when device names are truncated or don't match exactly.

        ``pa`` is any device source with PyAudio's query methods; a live switch
        passes the new loopback helper's fresh scan, which sees a device
        connected mid-recording (a second in-process PyAudio would not: while one
        is alive a new one only adds a reference to PortAudio). Defaults to this
        recording's device list (self._devices).
        """
        pa = pa or self._devices
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_output = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        default_name: str = default_output["name"]

        all_loopbacks = list(pa.get_loopback_device_info_generator())
        if not all_loopbacks:
            raise RuntimeError(
                "No WASAPI loopback devices found. "
                "Make sure your audio driver supports WASAPI loopback capture."
            )

        # 1. Exact substring match (the common case)
        for lb in all_loopbacks:
            if default_name in lb["name"] or lb["name"].startswith(default_name):
                return lb

        # 2. Prefix match - Windows can truncate long device names differently
        #    for the output vs its loopback counterpart
        prefix = default_name[:20]
        for lb in all_loopbacks:
            if prefix and prefix in lb["name"]:
                return lb

        # 3. Word-level match - e.g. "USB Audio" appears in both names
        words = [w for w in default_name.split() if len(w) >= 4]
        for lb in all_loopbacks:
            if any(w in lb["name"] for w in words):
                return lb

        # 4. Last resort: first available loopback device
        log.warn("audio", f"No loopback device matched '{default_name}'. "
                          f"Using '{all_loopbacks[0]['name']}' as fallback.")
        return all_loopbacks[0]

    def _match_loopback_by_name(self, wanted: str, pa=None,
                                strict: bool = False) -> dict | None:
        """Find a loopback device whose name matches ``wanted``.

        Uses the same tiered matching as _find_loopback_device (exact,
        substring, truncated-prefix, word-level) so a device survives the small
        name variations Windows introduces between sessions, but returns None
        instead of falling back to the first device: a wrong first-device is
        exactly the bug we are guarding against, so the caller picks the
        fallback (system default) itself.

        ``pa`` lets a caller pass another device source, e.g. a live switch
        passing the new loopback helper's fresh scan so an endpoint connected
        after the recording started is found (see _find_loopback_device).

        ``strict`` stops after the exact + substring tiers. Probe-sourced
        targets use it so a render name never maps onto a *sibling* endpoint of
        the same hardware via the loose word/prefix tiers (e.g. "Headset
        Earphone (Jabra)" vs "Speakers (Jabra)" both contain "Jabra"), which
        would bind the wrong, idle endpoint.
        """
        try:
            all_lb = list((pa or self._devices).get_loopback_device_info_generator())
        except Exception:
            return None
        if not all_lb:
            return None
        # 1. Exact
        for lb in all_lb:
            if lb["name"] == wanted:
                return lb
        # 2. Substring either direction (added/removed suffixes, e.g. the
        #    " [Loopback]" the loopback name carries but the render name does not)
        for lb in all_lb:
            if wanted in lb["name"] or lb["name"] in wanted:
                return lb
        if strict:
            return None
        # 3. Truncated prefix (Windows truncates long output vs loopback names
        #    differently)
        prefix = wanted[:20]
        for lb in all_lb:
            if prefix and prefix in lb["name"]:
                return lb
        # 4. Word level (e.g. "USB Audio" shared between both names). Every
        #    loopback name ends in "[Loopback]", so that token would match any
        #    device at all and turn a missing headset into random speakers.
        words = [w for w in wanted.split() if len(w) >= 4 and w != "[Loopback]"]
        for lb in all_lb:
            if any(w in lb["name"] for w in words):
                return lb
        return None

    def _resolve_loopback(self, index: int | None, name: str | None) -> dict:
        """Resolve the loopback (desktop) capture device, self-healing when a
        saved PyAudio index has drifted onto a different endpoint.

        PyAudio device indices are positional and unstable: plugging in a
        headset, a meeting app spinning up a virtual audio endpoint, a driver
        update or a reboot can renumber them. We persist the device *name*
        alongside the index and treat the name as authoritative; the index is
        only trusted when the device still sitting there carries the expected
        name. This mirrors resolve_dshow_mic_name() for the microphone, which is
        why mic selections already survived re-enumeration and loopback ones did
        not.
        """
        # No hint at all: follow the current system default render device.
        self._selected_loopback_name = None
        if index is None and not name:
            return self._find_loopback_device()

        saved = self._resolve_saved_loopback(index, name)

        # The default: the device chosen in the recorder is the device captured.
        # Windows keeps two output roles (the Default Device and the Default
        # Communications Device) and PortAudio only ever reports the first, so
        # "the default output" is routinely not the device the user is
        # listening on. Following it would swap a deliberately chosen headset
        # for idle speakers and record silence for the whole call (2026-09-05:
        # a saved Arctis headset was replaced by an idle Bose endpoint). The
        # saved device is authoritative; the default is only the fallback when
        # the saved device has gone.
        if not _follow_output_enabled():
            # Remember the selection so the watchdog can return to it while the
            # capture runs anywhere else: by the name it carries now when it is
            # here, else by the saved name. That includes a stand-in found only
            # by the loose name tiers (missing "Headphones (Realtek(R) Audio)"
            # matches "Speakers (Realtek(R) Audio)" on the word "Audio)"), so the
            # real device is picked up when it is plugged in.
            if name:
                here = self._match_loopback_by_name(name, strict=True)
                self._selected_loopback_name = here["name"] if here else name
            else:
                self._selected_loopback_name = saved["name"] if saved is not None else None
            if saved is not None:
                return saved
            if name:
                log.warn("audio", f"Loopback device '{name}' not found; using system "
                                  f"default until it is available again")
            return self._find_loopback_device()

        # Follow mode (Settings > System > "Follow call audio"): call/system
        # audio plays to the CURRENT default output, so bind to it. A saved
        # device is only a hint: when the default has since moved (headphones
        # plugged in, a Bluetooth dongle, a call app's own endpoint), a stale
        # saved device captures an idle endpoint and records pure silence (the
        # 2026-09-01 dead-loopback failure: a call on the headphones was missed
        # because the loopback stayed pinned to idle Realtek speakers). Use the
        # saved device only when the default cannot be resolved.
        default_lb = None
        try:
            default_lb = self._find_loopback_device()
        except Exception as e:
            log.warn("audio", f"Could not resolve the default-output loopback: {e}")

        # NOTE: we deliberately do NOT run the render probe here. It costs a
        # subprocess (~1-3s) and would delay every recording start, and at
        # start-time nothing may be playing yet, so we cannot tell a browser
        # call (renders to the Console default we already resolve below) from a
        # native call (renders to the Communications default). We bind the
        # Console default now (instant, correct for browser + single-device)
        # and let the silence watchdog FOLLOW the audio to wherever it is
        # actually playing within a few seconds - it only ever switches to an
        # endpoint that is demonstrably producing sound, so it can never bind
        # an idle device or flip-flop. See _loopback_silence_watchdog.
        if default_lb is not None:
            if saved is not None and saved.get("index") != default_lb.get("index"):
                log.info("audio",
                         f"Loopback: following current default output "
                         f"'{default_lb['name']}' instead of the saved "
                         f"'{saved['name']}' (system audio plays to the default output)")
            else:
                log.info("audio", f"Loopback: default output '{default_lb['name']}'")
            return default_lb

        if saved is not None:
            log.info("audio", f"Loopback: default output not resolvable; using saved '{saved['name']}'")
            return saved

        return self._find_loopback_device()  # raises the clear "no loopback devices" error

    def _resolve_saved_loopback(self, index: int | None, name: str | None) -> dict | None:
        """The device the user chose: the saved index when it still carries the
        saved name, otherwise the live device with that name. None when neither
        resolves, so the caller decides the fallback."""
        if index is not None:
            try:
                info = self._devices.get_device_info_by_index(index)
                if info.get("maxInputChannels", 0) > 0 and (
                        not name or info["name"] == name):
                    return info
                if name:
                    log.info("audio", f"Loopback index {index} now "
                             f"'{info.get('name')}', expected '{name}'; "
                             f"re-resolving by name")
            except Exception:
                if name:
                    log.info("audio", f"Loopback index {index} no longer valid; "
                             f"re-resolving by name")
        if name:
            match = self._match_loopback_by_name(name)
            if match is not None:
                if index is None or match.get("index") != index:
                    log.info("audio", f"Loopback '{name}' re-resolved to index "
                             f"{match.get('index')}")
                return match
        return None

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

    def start_wav(self, path: str, append: bool = False) -> None:
        """Request WAV recording.  Call before start().

        The actual WavWriter is created inside start() once the sample rate
        is known from the loopback device.
        """
        self._wav_path = path
        self._wav_append = append

    def stop_wav(self) -> None:
        """Finalize and close the WAV file.  Safe to call multiple times."""
        if self.wav_writer is not None:
            self.wav_writer.close()
            self.wav_writer = None

    # ── Per-source ("mic = Me") tracks ─────────────────────────────────────

    @staticmethod
    def _per_source_paths(base_wav_path: str) -> dict:
        """Derive the per-source temp WAV + final Opus paths from the mixed WAV
        path ``{dir}/{sid}.wav``."""
        root = base_wav_path[:-4] if base_wav_path.lower().endswith(".wav") else base_wav_path
        return {
            "mic_wav":     root + "_mic.wav",
            "desktop_wav": root + "_desktop.wav",
            "mic_opus":    root + "_mic.opus",
            "desktop_opus": root + "_desktop.opus",
        }

    def _open_per_source_writers(self, base_wav_path: str, append: bool) -> None:
        """Open the mic-only and desktop-only temp WAV writers. On resume
        (append) with an existing Opus part, decode it back to the temp WAV first
        so the final encode covers the whole session."""
        # A previous session's deferred encode may still be running; it deletes
        # the temp WAV on success, so wait it out before deciding what exists.
        with _PER_SOURCE_ENCODE_LOCK:
            pass
        p = self._per_source_paths(base_wav_path)
        self._mic_wav_path     = p["mic_wav"]
        self._desktop_wav_path = p["desktop_wav"]
        for wav_path, opus_path in ((p["mic_wav"], p["mic_opus"]),
                                    (p["desktop_wav"], p["desktop_opus"])):
            if append and not os.path.isfile(wav_path) and os.path.isfile(opus_path):
                # Resume: rebuild the temp WAV from the previously encoded part
                # so WavWriter(append=True) continues the full track.
                self._decode_opus_to_wav(opus_path, wav_path)
        self._mic_wav_writer = WavWriter(p["mic_wav"], self.sample_rate, append=append)
        self._desktop_wav_writer = WavWriter(p["desktop_wav"], self.sample_rate, append=append)

    def _close_per_source_writers(self) -> None:
        for attr in ("_mic_wav_writer", "_desktop_wav_writer"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _decode_opus_to_wav(self, opus_path: str, wav_path: str) -> bool:
        """Decode an Opus part back to a temp WAV at the capture sample rate."""
        from capture_video.ffmpeg_util import find_ffmpeg, subprocess_no_window_flag
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return False
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", opus_path,
                 # A plain 44-byte header: no LIST/INFO chunk for the appender to trip on.
                 "-fflags", "+bitexact",
                 "-acodec", "pcm_s16le", "-ar", str(self.sample_rate or 48000),
                 "-ac", "1", wav_path],
                capture_output=True, timeout=300,
                creationflags=subprocess_no_window_flag(),
            )
            return os.path.isfile(wav_path)
        except Exception:
            log.warn("audio", f"Could not decode {os.path.basename(opus_path)} for resume")
            return False

    @staticmethod
    def _encode_one_opus(wav_path: str, opus_path: str, ffmpeg: str) -> None:
        """Encode a single per-source temp WAV to Opus, deleting the WAV only on
        success. Never raises: a failed track keeps its WAV as a fallback."""
        from capture_video.ffmpeg_util import subprocess_no_window_flag
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-i", wav_path,
                 "-c:a", "libopus", "-b:a", "32k", "-vbr", "on",
                 "-application", "voip", opus_path],
                capture_output=True, timeout=600,
                creationflags=subprocess_no_window_flag(),
            )
            if r.returncode == 0 and os.path.isfile(opus_path):
                os.remove(wav_path)
            else:
                log.warn("audio", f"Opus encode failed for {os.path.basename(wav_path)} "
                                  f"(rc={r.returncode}); keeping WAV")
        except Exception:
            log.warn("audio", f"Opus encode error for {os.path.basename(wav_path)}; keeping WAV")
            traceback.print_exc()

    def _encode_per_source_opus(self) -> None:
        """Encode the per-source temp WAVs to Opus and delete the temps.

        The two tracks encode concurrently: libopus is single-threaded per
        stream, so running them back to back doubled the wall time of the
        slowest step in stopping a recording for no reason. Failures are
        non-fatal: the temp WAV is kept so reanalysis can still fall back to it.
        """
        from capture_video.ffmpeg_util import find_ffmpeg
        ffmpeg = find_ffmpeg()
        pairs = []
        if self._mic_wav_path:
            pairs.append((self._mic_wav_path, self._per_source_paths_opus(self._mic_wav_path)))
        if self._desktop_wav_path:
            pairs.append((self._desktop_wav_path, self._per_source_paths_opus(self._desktop_wav_path)))
        pairs = [(w, o) for w, o in pairs if os.path.isfile(w)]
        self._mic_wav_path = None
        self._desktop_wav_path = None
        if not pairs:
            return
        if not ffmpeg:
            log.warn("audio", "ffmpeg not found - keeping per-source WAV (no Opus encode)")
            return
        # Held for the whole encode so a recording started (or resumed) while
        # this runs cannot append to a temp WAV that ffmpeg is about to delete.
        with _PER_SOURCE_ENCODE_LOCK:
            t0 = time.monotonic()
            threads = [threading.Thread(target=self._encode_one_opus,
                                        args=(w, o, ffmpeg), daemon=True)
                       for w, o in pairs]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            log.info("audio", f"Per-source Opus encode finished: {len(pairs)} track(s) "
                              f"in {time.monotonic() - t0:.1f}s")

    def finalize_per_source_tracks(self) -> None:
        """Run the deferred per-source Opus encode.

        ``stop(encode_per_source=False)`` closes the writers but leaves the
        encode for the caller to run once the UI has been told the recording
        ended (it is by far the slowest part of stopping). Safe to call when
        nothing is pending, and safe to skip entirely: the temp WAVs simply
        stay on disk and reanalysis still finds them."""
        try:
            self._encode_per_source_opus()
        except Exception:
            log.warn("audio", "Per-source Opus encode raised; per-source temp "
                              "WAVs left in place.")
            traceback.print_exc()

    @staticmethod
    def _per_source_paths_opus(wav_path: str) -> str:
        """Map a per-source temp WAV path to its Opus sibling."""
        return wav_path[:-4] + ".opus" if wav_path.lower().endswith(".wav") else wav_path + ".opus"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    @staticmethod
    def _new_loopback_child() -> "_LoopbackChild":
        """A loopback helper with a fresh device scan. A method so tests can
        substitute one."""
        return _take_loopback_child()

    def _open_start_loopback(self, loopback_index: int | None,
                             loopback_name: str | None) -> tuple[dict, int, str]:
        """Resolve and open the desktop (loopback) stream for start().

        Normally the stream runs in a helper process whose fresh device scan is
        also what the device is resolved against, so an output connected after
        the app started is both visible and openable, and a later switch can
        reach one connected mid-recording. Only if the helper cannot start or
        open does this capture in-process on self._pa. Returns (device info,
        ring buffer frames, where)."""
        t0 = time.monotonic()
        try:
            child = self._new_loopback_child()
        except Exception as e:
            child = None
            log.warn("audio", f"Desktop audio helper unavailable ({e}); capturing "
                              f"in-process, so a device switch cannot reach an "
                              f"output connected during the recording")
        if child is not None:
            log.info("audio", f"Desktop audio helper ready in "
                              f"{(time.monotonic() - t0) * 1000:.0f} ms "
                              f"({'warm spare' if getattr(child, 'warm', False) else 'new process'})")
            self._devices = child.devices
            try:
                lb_info = self._resolve_loopback(loopback_index, loopback_name)
            except Exception:
                # Nothing to capture (no default output, no loopback device).
                # The helper has not opened anything; close it here, or it
                # waits for a command until the app exits.
                child.close()
                raise
            lb_buf_size = self._compute_loopback_buffer_size(lb_info)
            try:
                child.open(lb_info,
                           channels=max(1, lb_info["maxInputChannels"]),
                           rate=int(lb_info["defaultSampleRate"]),
                           frames_per_buffer=lb_buf_size,
                           chunk=self.CHUNK_SIZE)
                self._loopback_stream = child
                self._loopback_pa = None
                return lb_info, lb_buf_size, "helper process"
            except Exception as e:
                log.warn("audio", f"Desktop audio helper could not open "
                                  f"'{lb_info['name']}' ({e}); capturing in-process")
        self._devices = self._pa
        lb_info = self._resolve_loopback(loopback_index, loopback_name)
        lb_buf_size = self._compute_loopback_buffer_size(lb_info)
        try:
            self._loopback_stream = self._pa.open(
                format=self.FORMAT,
                channels=max(1, lb_info["maxInputChannels"]),
                rate=int(lb_info["defaultSampleRate"]),
                input=True,
                input_device_index=lb_info["index"],
                frames_per_buffer=lb_buf_size,
            )
        except OSError as e:
            log.error("audio", f"Could not open loopback '{lb_info['name']}' "
                               f"(index {lb_info.get('index')}): {e}")
            raise _open_failure("desktop audio", lb_info["name"], e) from e
        self._loopback_pa = self._pa   # the in-process fallback uses the main PyAudio
        return lb_info, lb_buf_size, "in-process"

    def _open_devices(self, loopback_index: int | None, mic_index: int | None,
                      ffmpeg_mic_name: str | None, loopback_name: str | None) -> None:
        """Everything start() does before a thread reads anything: resolve and
        open the loopback (required) and the microphone (best effort), then the
        WAV writers. Raises when the loopback cannot be opened."""
        # --- Loopback stream (required) ---
        # Resolve name-first so a drifted PyAudio index self-heals onto the same
        # physical device instead of silently capturing whatever now sits at
        # that index (see _resolve_loopback), against a fresh device scan.
        lb_info, lb_buf_size, lb_where = self._open_start_loopback(
            loopback_index, loopback_name)
        self.sample_rate = int(lb_info["defaultSampleRate"])
        self._loopback_channels = max(1, lb_info["maxInputChannels"])
        self._loopback_device_name = lb_info["name"]
        self._loopback_verified = False
        self._mic_verified = False
        if INPUT_DEBUG:
            log.info("input-debug", "INPUT_DEBUG enabled - verbose audio tracing on")
            log.info("input-debug",
                     f"loopback_index={loopback_index!r} mic_index={mic_index!r} "
                     f"ffmpeg_mic_name={ffmpeg_mic_name!r}")
            log.info("input-debug",
                     f"loopback dev: name='{lb_info['name']}' index={lb_info.get('index')} "
                     f"defaultSampleRate={lb_info.get('defaultSampleRate')} "
                     f"maxIn={lb_info.get('maxInputChannels')} "
                     f"hostApi={lb_info.get('hostApi')}")
            self._idbg_throttle.reset()
            self._idbg_lb_bytes = self._idbg_mic_bytes = 0
            self._idbg_lb_chunks = self._idbg_mic_chunks = 0
            self._idbg_lb_zero_chunks = self._idbg_mic_zero_chunks = 0
            self._idbg_mic_inject_bytes = 0
            self._idbg_mix_src_counts = {"loopback": 0, "mic": 0, "both": 0}
            self._idbg_mix_emitted = 0
            self._idbg_audio_q_full_drops = 0
            self._idbg_mic_q_full_drops = 0
            self._idbg_lb_q_full_drops = 0
        log.info("audio", f"Loopback: '{lb_info['name']}' @ {self.sample_rate} Hz, "
                          f"{self._loopback_channels} ch, ring buffer {lb_buf_size} frames "
                          f"({lb_where})")



        # --- Microphone stream (best-effort) ---
        self._selected_mic_name = None
        self._mic_on_selected = False
        self._mic_last_data_ts = time.monotonic()
        if mic_index == -3:
            # FFmpeg subprocess mic via DirectShow - completely independent of
            # Python/WASAPI audio stack for maximum reliability.
            from capture_video import find_ffmpeg
            ffmpeg_path = find_ffmpeg()
            # The selection, whatever happens below: if the default mic has to
            # stand in for it, the watchdog switches back once it is available.
            self._selected_mic_name = ffmpeg_mic_name or None
            if not ffmpeg_path:
                log.warn("audio", "ffmpeg not found - cannot use FFmpeg mic capture")
                mic_info = None
            elif not ffmpeg_mic_name:
                log.warn("audio", "No DirectShow mic device name provided for ffmpeg capture")
                mic_info = None
            else:
                # Re-resolve the saved name against the live dshow device list.
                # The friendly name we persisted may have shifted (driver update,
                # USB re-enumeration) or the device may be gone entirely. Doing
                # this here, instead of trusting the caller's stale string,
                # turns "ffmpeg silently records nothing" into a clean failure
                # or an automatic retarget onto the same physical device.
                resolved, reason = resolve_dshow_mic_name(ffmpeg_mic_name)
                if resolved is None:
                    # The saved DirectShow mic is gone (dead/disabled device, USB
                    # unplugged - e.g. the retired "USB PnP Audio Device"). Do NOT
                    # fall through to loopback-only: that silently drops the user's
                    # OWN voice (the inverse of the dead-loopback failure). Retarget
                    # to the live WASAPI default input so the user is always
                    # captured; only if that is also unavailable do we go
                    # loopback-only.
                    fallback = self._find_mic_device()
                    if fallback is not None:
                        log.warn("audio", f"Mic '{ffmpeg_mic_name}' not found in dshow "
                                          f"device list ({reason}); falling back to the "
                                          f"default microphone '{fallback['name']}'")
                        mic_info = fallback
                    else:
                        log.warn("audio", f"Mic '{ffmpeg_mic_name}' not found in dshow "
                                          f"device list ({reason}) and no fallback mic "
                                          f"available - capturing loopback only")
                        mic_info = None
                else:
                    if resolved != ffmpeg_mic_name:
                        log.info("audio", f"Mic name re-resolved: '{ffmpeg_mic_name}' "
                                          f"-> '{resolved}' ({reason})")
                    ffmpeg_mic_name = resolved
                    self._set_mic_format(48000, 1)
                    self._has_mic      = True
                    self._ffmpeg_mic_name = ffmpeg_mic_name
                    self._mic_device_name = ffmpeg_mic_name
                    log.info("audio", f"Mic: ffmpeg dshow '{ffmpeg_mic_name}' @ {self._mic_rate} Hz, 1 ch")
                    self._ffmpeg_proc = self._spawn_ffmpeg_mic(ffmpeg_path, ffmpeg_mic_name)
                    self._mic_on_selected = True
                    mic_info = None   # skip the WASAPI-open block below
        elif mic_index == -2:
            # Browser mic - no WASAPI stream; audio arrives via inject_mic_data()
            self._mic_rate     = 48000   # browser AudioContext default
            self._mic_channels = 1
            self._has_mic      = True
            self._mic_device_name = "browser (getUserMedia)"
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
                self._mic_buf_size = self._compute_mic_buffer_size(mic_info)
                self._mic_stream = self._pa.open(
                    format=self.FORMAT,
                    channels=self._mic_channels,
                    rate=self._mic_rate,
                    input=True,
                    input_device_index=mic_info["index"],
                    frames_per_buffer=self._mic_buf_size,
                )
                self._has_mic = True
                self._mic_device_name = mic_info["name"]
                if self._mic_rate != self.sample_rate:
                    g = gcd(self.sample_rate, self._mic_rate)
                    self._resample_up = self.sample_rate // g
                    self._resample_down = self._mic_rate // g
                log.info("audio", f"Mic: '{mic_info['name']}' @ {self._mic_rate} Hz, "
                                  f"{self._mic_channels} ch, buf={self._mic_buf_size}")
            except Exception as e:
                log.warn("audio", f"Mic unavailable: {e}")
                self._mic_stream = None
                self._has_mic = False
        elif mic_index not in (-2, -3):
            # Neither -2 (browser) nor -3 (ffmpeg) nor a valid WASAPI mic
            self._has_mic = False
            if mic_index == -1:
                log.info("audio", "Microphone explicitly disabled - capturing loopback only.")
            else:
                log.info("audio", "No microphone device found - capturing loopback only.")

        # Open WAV writer now that sample_rate is known
        base_wav_path = self._wav_path
        if self._wav_path:
            self.wav_writer = WavWriter(self._wav_path, self.sample_rate,
                                        append=self._wav_append)
            self._wav_path = None

        # Per-source tracks for the "mic = Me" feature. Only meaningful when a
        # mic is actually present (otherwise mixed == desktop) and we're writing
        # to a file (not the live audio-test path).
        self._per_source_active = False
        if self.mic_is_me_enabled and self._has_mic and base_wav_path:
            try:
                self._open_per_source_writers(base_wav_path, self._wav_append)
                self._per_source_active = True
                log.info("audio", "Per-source capture on: writing mic-only + "
                                  "desktop-only tracks for source-aware diarization.")
            except Exception:
                log.warn("audio", "Could not open per-source writers; "
                                  "continuing with mixed audio only.")
                traceback.print_exc()
                self._close_per_source_writers()
                self._per_source_active = False

    def _undo_open(self) -> None:
        """Release what a failed start() opened. No thread has read a stream
        yet, so closing them is safe, and terminating the PyAudio is what lets
        the next attempt enumerate the devices afresh."""
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
            self._ffmpeg_proc = None
        for attr in ("_loopback_stream", "_mic_stream"):
            _retire_stream(getattr(self, attr), None, self._pa)
            setattr(self, attr, None)
        self._close_per_source_writers()
        self._per_source_active = False
        self.stop_wav()
        _terminate_quietly(self._pa)
        self._pa = None
        self._loopback_pa = None

    def start(self, loopback_index: int | None = None, mic_index: int | None = None,
              ffmpeg_mic_name: str | None = None, loopback_name: str | None = None) -> None:
        """
        Start capture.  loopback_index / mic_index override auto-detection;
        pass mic_index=-1 to explicitly disable the microphone,
        mic_index=-2 to receive mic audio injected from the browser
        (via inject_mic_data()), or mic_index=-3 to capture via an ffmpeg
        subprocess using DirectShow (requires ffmpeg_mic_name).

        loopback_name, when given, is the friendly name saved alongside
        loopback_index; if that index has since been renumbered onto a
        different device the capture re-resolves to the named device instead.
        """
        # With every earlier capture stopped this is a fresh Pa_Initialize, so
        # the devices are enumerated now rather than inherited from whatever
        # was plugged in when PortAudio was last initialised.
        self._pa = pyaudio.PyAudio()
        try:
            self._open_devices(loopback_index, mic_index, ffmpeg_mic_name, loopback_name)
        except Exception:
            self._undo_open()
            raise

        self.is_running = True

        self._loopback_thread = threading.Thread(
            target=self._capture_loop,
            args=(self._loopback_stream, self._loopback_q),
            kwargs={"is_loopback": True},
            daemon=True,
        )
        self._loopback_thread.start()

        if self._has_mic and self._ffmpeg_proc is not None:
            # FFmpeg subprocess mic - read raw PCM from stdout
            self._mic_thread = threading.Thread(
                target=self._ffmpeg_capture_loop,
                daemon=True,
            )
            self._mic_thread.start()
        elif self._has_mic and self._mic_stream is not None:
            # WASAPI mic stream.
            # NOTE: Do NOT pass _mic_buf_size here.  _mic_buf_size is the
            # frames_per_buffer for the WASAPI stream's internal ring buffer
            # (large = prevents underruns).  The *read* chunk size must stay
            # at CHUNK_SIZE (512) so mic data flows at the same cadence as
            # loopback and the mixer can interleave them without gaps.
            self._mic_thread = threading.Thread(
                target=self._capture_loop,
                args=(self._mic_stream, self._mic_q),
                daemon=True,
            )
            self._mic_thread.start()

        self._mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True)
        self._mixer_thread.start()

        self._silence_watchdog = threading.Thread(
            target=self._loopback_silence_watchdog, daemon=True)
        self._silence_watchdog.start()

    def restart_loopback(self, target_name: str | None = None,
                         reopen_same: bool = False, reason: str | None = None) -> bool:
        """Switch the loopback to another output device without stopping the
        recording, so a change of output during a call (headphones plugged in,
        a new default output, the call app's own endpoint) is followed
        automatically.

        ``target_name``, when given, names the render endpoint that is ACTUALLY
        playing audio (from the render probe - typically the default
        Communications device a call app renders to, which PortAudio cannot
        see). Without it, falls back to the current default output.

        Every switch starts a NEW loopback helper process, so the target is
        looked up in a fresh device scan. A second in-process PyAudio would see
        only the list enumerated when the recording started, because self._pa
        keeps PortAudio initialised: on 2026-10-06 the probe heard a call on
        "Headphones (Realtek(R) Audio)", the switch found no such device, and the
        whole call was recorded one-sided. The old stream is retired once the
        new helper is capturing (a helper is killed; an in-process fallback
        stream is closed once its reader has left, see _stream_graveyard).
        ``reopen_same`` reopens even when the target is the device already held
        (its helper died). ``reason`` names where the target came from in the
        log line. Returns True only when it actually switched to a live device.
        Never raises; any failure leaves the current stream untouched (the
        silence watchdog then alarms)."""
        if not self.is_running:
            return False
        if not self._loopback_restart_lock.acquire(blocking=False):
            return False  # a switch is already in progress
        child = None
        try:
            try:
                child = self._new_loopback_child()
                devices = child.devices
                if target_name:
                    new_info = self._match_loopback_by_name(target_name, pa=devices, strict=True)
                    if new_info is None:
                        # The probe named an endpoint with no matching loopback
                        # device. Do NOT fall back to the console default and
                        # switch to it - that fallback is the flip-flop that
                        # moved a live call onto an idle device. Stay put; the
                        # watchdog tries again next tick.
                        log.warn("audio", f"Loopback switch: no loopback device matches "
                                          f"'{target_name}'; staying on the current device")
                        child.close()
                        return False
                else:
                    new_info = self._find_loopback_device(pa=devices)
            except Exception as e:
                log.warn("audio", f"Loopback switch: could not resolve a target device ({e})")
                if child is not None:
                    child.close()
                return False

            # Same device as now? Then the silence is not a device move (the call
            # is muted, or its app is pinned to a non-default output); don't churn.
            if new_info["name"] == self._loopback_device_name and not reopen_same:
                child.close()
                return False

            # Open the new loopback at ITS OWN native mix format, not the start
            # device's. A call endpoint (Bluetooth HFP, many USB headsets) is
            # often MONO and sometimes 16 kHz; forcing the start device's 48 kHz
            # stereo made the open fail and the call was lost with only an alarm
            # (the real gap behind the 2026-09-01 failure on headset hardware).
            # We take the device's channels/rate and reconcile downstream: the
            # mixer already downmixes with self._loopback_channels, and we set a
            # resample ratio so a different rate is converted back to the
            # pipeline rate (self.sample_rate, fixed at start for WAV/transcriber).
            new_ch = max(1, int(new_info.get("maxInputChannels") or self._loopback_channels))
            new_rate = int(new_info.get("defaultSampleRate") or self.sample_rate)
            try:
                child.open(new_info, channels=new_ch, rate=new_rate,
                           frames_per_buffer=self._compute_loopback_buffer_size(new_info),
                           chunk=self.CHUNK_SIZE)
            except Exception as e:
                log.warn("audio", f"Loopback switch: could not open '{new_info['name']}' "
                                  f"at {new_rate}Hz/{new_ch}ch ({e})")
                child.close()
                return False
            if not self.is_running:
                # stop() ran while the new helper was starting and retires this
                # capture's streams itself. Publishing this one now would leave
                # a helper capturing until the app exits.
                child.close()
                return False

            old_stream = self._loopback_stream
            old_thread = self._loopback_thread
            old_pa = self._loopback_pa

            # Reconcile the new device's format with the fixed pipeline BEFORE the
            # new capture thread starts queueing data, so the mixer reads the
            # right channel count and resample ratio for the very first chunk.
            if new_rate != self.sample_rate:
                g = gcd(self.sample_rate, new_rate)
                self._loopback_resample_up = self.sample_rate // g
                self._loopback_resample_down = new_rate // g
                log.info("audio", f"Loopback resampling {new_rate}->{self.sample_rate} Hz "
                                  f"on the switched device")
            else:
                self._loopback_resample_up = self._loopback_resample_down = 1
            self._loopback_channels = new_ch

            # Publish the new stream: the old loopback thread's while-condition
            # (self._loopback_stream is stream) goes False and it exits.
            self._loopback_stream = child
            self._loopback_pa = None
            self._devices = devices
            self._loopback_device_name = new_info["name"]
            self._loopback_verified = False
            self.loopback_had_signal = False
            self.loopback_level = 0.0
            self.loopback_peak = 0.0
            self._loopback_thread = threading.Thread(
                target=self._capture_loop,
                args=(child, self._loopback_q),
                kwargs={"is_loopback": True},
                daemon=True,
            )
            self._loopback_thread.start()

            # The old reader leaves within one poll of the swap above. Its
            # stream is retired once it has (a helper is killed either way); the
            # main PyAudio is kept, because it still owns the mic.
            if old_thread is not None:
                old_thread.join(timeout=2)
            _retire_stream(old_stream, old_thread, old_pa)
            if old_pa is not None and old_pa is not self._pa:
                _terminate_quietly(old_pa)
            src = reason or ("the live endpoint (render probe)" if target_name
                             else "the current default output")
            verb = "reopened on" if reopen_same else "switched to"
            log.info("audio", f"Loopback {verb} {src}: '{new_info['name']}'")
            return True
        finally:
            self._loopback_restart_lock.release()

    # While the capture runs on another output because the selected device was
    # unavailable, the watchdog checks whether it is back: every 10 s at first,
    # easing off to every 30 s while it stays away. After a return attempt that
    # found the device but could not open it (another app holding it in
    # exclusive mode, say), the gap doubles up to the maximum.
    SELECTED_RECHECK_SEC = 10.0
    SELECTED_RECHECK_MAX_SEC = 60.0

    @staticmethod
    def _scan_devices_now():
        """The devices Windows has right now, from the spare helper's rescan, or
        None when no spare is ready (the next check tries again). It never
        waits for a new process, which is what makes it cheap enough to run
        every few seconds. A method so tests can substitute a scan."""
        spare = _borrow_spare()
        if spare is None:
            with _spare_lock:
                _refill_spare_locked()
            return None
        try:
            return spare.devices
        finally:
            _return_spare(spare)

    def _return_to_selected_device(self) -> bool | None:
        """Switch back to the desktop device the user selected once it exists
        again, while the capture runs on another output because that device was
        unavailable (missing at start, or gone mid-recording).

        Returns True when it switched back, False when the device is listed but
        the switch failed, and None when there is nothing to do: no selection,
        already on it, not back yet, or no device scan available. Never raises.
        """
        selected = self._selected_loopback_name
        if not selected or self._loopback_device_name == selected:
            return None
        try:
            devices = self._scan_devices_now()
        except Exception as e:
            log.warn("audio", f"Device scan for the selected output failed: {e}")
            return None
        if devices is None:
            return None
        match = self._match_loopback_by_name(selected, pa=devices, strict=True)
        if match is None:
            return None
        if match.get("name") == self._loopback_device_name:
            # Already capturing it; it only carries a slightly different name.
            self._selected_loopback_name = match["name"]
            return None
        log.info("audio", f"Loopback: the selected device "
                          f"'{selected.removesuffix(' [Loopback]')}' is available again; "
                          f"returning to it")
        try:
            switched = self.restart_loopback(target_name=selected,
                                             reason="the selected device")
        except Exception as e:
            log.warn("audio", f"loopback switch failed: {e}")
            return False
        if switched:
            # Adopt the name it has now, so a small rename (a driver update) is
            # not read as "still away" on every later check.
            self._selected_loopback_name = self._loopback_device_name
        return switched

    # ── Microphone: the same rule as the desktop device ─────────────────────
    # The mic the user selected is the mic recorded. While it is unavailable
    # (missing at start, or unplugged / switched off mid-recording) the default
    # mic stands in, and the capture goes back to the selected one as soon as
    # it is there again. Before 2026-10-07 the stand-in was kept for the rest
    # of the recording, and a mic lost mid-recording stayed silent.

    # No data from the mic for this long means the device is gone, even if its
    # ffmpeg has not exited: a live mic sends samples continuously, silence
    # included.
    MIC_DEAD_AFTER_SEC = 5.0
    # A DirectShow device that cannot be opened makes ffmpeg exit within
    # moments; a new one still running after this long is taken as working.
    MIC_OPEN_CHECK_SEC = 1.0

    def _mic_is_on_selected(self) -> bool:
        """The selected mic is the one being captured, and it is delivering."""
        proc = self._ffmpeg_proc
        return bool(self._mic_on_selected and proc is not None and proc.poll() is None
                    and time.monotonic() - self._mic_last_data_ts < self.MIC_DEAD_AFTER_SEC)

    def _mic_is_live(self) -> bool:
        """Some mic (the selected one or a stand-in) is delivering."""
        proc = self._ffmpeg_proc
        source = ((proc is not None and proc.poll() is None)
                  or self._mic_stream is not None)
        return bool(source and time.monotonic() - self._mic_last_data_ts
                    < self.MIC_DEAD_AFTER_SEC)

    def _default_mic_dshow_name(self) -> str | None:
        """The DirectShow name of Windows' default recording device, from a
        fresh scan through the spare helper (this process's PortAudio list is
        frozen at the start of the recording). None when there is no default
        input, no scan, or no DirectShow device by that name."""
        devices = self._scan_devices_now()
        if devices is None:
            return None
        try:
            default = devices.get_host_api_info_by_type(pyaudio.paWASAPI).get(
                "defaultInputDevice")
            name = devices.get_device_info_by_index(default).get("name") or ""
        except Exception:
            return None
        if not name:
            return None
        resolved, _why = resolve_dshow_mic_name(name)
        return resolved

    def _retire_mic_source(self) -> None:
        """Stop the mic source in use (an ffmpeg process or a WASAPI stream)
        and wait for its reader to leave. Caller holds _mic_switch_lock."""
        old_thread, old_proc, old_stream = (self._mic_thread, self._ffmpeg_proc,
                                            self._mic_stream)
        self._ffmpeg_proc = None
        self._mic_stream = None   # a WASAPI reader leaves within a poll (_capture_loop)
        if old_proc is not None:
            try:
                old_proc.terminate()
            except Exception:
                pass
        if old_thread is not None:
            old_thread.join(timeout=3)
        if old_stream is not None:
            _retire_stream(old_stream, old_thread, self._pa)

    def _switch_mic_to_dshow(self, name: str, *, selected: bool, reason: str) -> bool:
        """Capture the mic from DirectShow device ``name`` in place of whatever
        captures it now, without stopping the recording.

        The new ffmpeg starts with its reader draining (and dropping) its output
        and must still be running MIC_OPEN_CHECK_SEC later; only then is the old
        source retired, so a device that will not open costs nothing. The mixer
        gets a moment to take the old source's last chunks in their own format
        before the new format is set. Returns True when it switched. Never
        raises."""
        if not self.is_running:
            return False
        if not self._mic_switch_lock.acquire(blocking=False):
            return False   # a switch is already under way
        proc = None
        reader = None
        try:
            from capture_video import find_ffmpeg
            ffmpeg_path = find_ffmpeg()
            if not ffmpeg_path:
                return False
            try:
                proc = self._spawn_ffmpeg_mic(ffmpeg_path, name)
            except Exception as e:
                log.warn("audio", f"Mic switch: could not start ffmpeg for '{name}' ({e})")
                return False
            live = threading.Event()
            reader = threading.Thread(target=self._ffmpeg_capture_loop,
                                      args=(proc, live), daemon=True)
            reader.start()
            deadline = time.monotonic() + self.MIC_OPEN_CHECK_SEC
            while time.monotonic() < deadline and proc.poll() is None and self.is_running:
                time.sleep(0.05)
            if proc.poll() is not None or not self.is_running:
                if self.is_running:
                    log.warn("audio", f"Mic switch: could not open '{name}'")
                return False

            self._retire_mic_source()
            time.sleep(0.05)
            self._set_mic_format(48000, 1)
            self._ffmpeg_proc = proc
            self._ffmpeg_mic_name = name
            self._mic_device_name = name
            self._mic_thread = reader
            self._mic_on_selected = selected
            self._mic_verified = False
            self._mic_last_data_ts = time.monotonic()
            self._has_mic = True
            live.set()
            proc = reader = None   # published: stop() owns them now
            log.info("audio", f"Mic switched to {reason}: '{name}'")
            return True
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
                if reader is not None:
                    reader.join(timeout=3)
            self._mic_switch_lock.release()

    def _check_selected_mic(self) -> bool | None:
        """Keep the mic on the device the user selected, with the default mic
        standing in while it is unavailable.

        Returns True when it switched (back to the selected mic, or to a
        stand-in), False when a switch was tried and failed, None when there
        was nothing to do. Never raises."""
        selected = self._selected_mic_name
        if not selected or self._mic_is_on_selected():
            return None
        try:
            resolved, _why = resolve_dshow_mic_name(selected)
        except Exception as e:
            log.warn("audio", f"Mic check failed: {e}")
            return None
        if resolved:
            log.info("audio", f"Mic: the selected microphone '{selected}' is available; "
                              f"switching to it")
            return self._switch_mic_to_dshow(resolved, selected=True,
                                              reason="the selected microphone")
        if self._mic_is_live():
            return None   # the stand-in is recording; wait for the selected one
        name = self._default_mic_dshow_name()
        if not name:
            return None
        log.warn("audio", f"Mic: the selected microphone '{selected}' is unavailable; "
                          f"recording the default microphone until it is back")
        return self._switch_mic_to_dshow(name, selected=False,
                                          reason="the default microphone, standing in")

    def _probe_live_output(self, require_playing: bool = True,
                           duration: float = 1.2) -> str | None:
        """Name of the render endpoint the loopback SHOULD capture, per the
        render probe, or None when the probe fails / has nothing to offer.

        Preference order: the endpoint that is ACTUALLY playing and is the
        default Communications device (call apps render there, and a call is
        the thing we must never lose), then a playing default-output endpoint,
        then the loudest playing endpoint (a call app pinned to a non-default
        device).

        ``require_playing=True`` (the watchdog's use) returns None when nothing
        is audible, so a silent moment cannot move the device. With
        ``require_playing=False`` a role-flagged default is returned even while
        silent, but an arbitrary idle endpoint never is (only a genuinely
        playing endpoint is used as the loudest-fallback). Recording start does
        NOT probe (it would delay capture and cannot yet tell a browser call
        from a native one); the watchdog follows the audio a few seconds in.

        Probing runs in a subprocess (~duration plus interpreter start); only
        call it from the watchdog thread, never on the recording-start path.
        """
        PLAYING = 0.01   # meter peak: 0.0 dead silence; quiet speech > 0.03
        probe = probe_render_endpoints(duration)
        if not probe:
            return None
        eps = probe.get("endpoints") or []
        if not eps:
            return None
        summary = ", ".join(
            f"'{e['name']}' peak={e['peak']}"
            + ("[comm]" if e.get("is_default_communications") else "")
            + ("[default]" if e.get("is_default") else "")
            for e in eps)
        log.info("audio", f"Render probe: {summary}")
        # Remember the Communications-role default (the device a call app renders
        # to) so the watchdog can stay sticky on it through a far-end pause.
        self._last_probe_comms = next(
            (e["name"] for e in eps if e.get("is_default_communications")), None)
        playing = [e for e in eps if e.get("peak", 0.0) >= PLAYING]
        pool = playing if playing else ([] if require_playing else eps)
        if not pool:
            return None
        for e in pool:
            if e.get("is_default_communications"):
                return e["name"]
        for e in pool:
            if e.get("is_default"):
                return e["name"]
        # Only fall back to "loudest" among endpoints that are ACTUALLY
        # playing; never pick an arbitrary idle endpoint (e.g. an HDMI output
        # that happens to enumerate first) when nothing is audible.
        if playing:
            return max(playing, key=lambda e: e.get("peak", 0.0))["name"]
        return None

    def take_peaks(self) -> tuple[float, float]:
        """Loudest (loopback, mic) RMS since the last call, and reset.

        Read-and-reset, so consecutive calls partition the timeline into
        windows with no sample falling through the gap between them.
        """
        lb, mic = self.loopback_peak, self.mic_peak
        self.loopback_peak = 0.0
        self.mic_peak = 0.0
        return lb, mic

    def _loopback_silence_watchdog(self) -> None:
        """Keep the loopback bound to wherever the call/desktop audio is ACTUALLY
        playing, and alarm if it is genuinely not being captured.

        PortAudio only ever hands us the Console/Multimedia default output, but a
        call app can render to the Communications-role default (Teams, Zoom) or
        to a device the user picked inside it, and the user may switch output
        devices mid-call. So while the loopback is silent, we ask the
        out-of-process render probe which endpoint is producing sound RIGHT NOW
        and, only when that is a DIFFERENT endpoint than the one we hold, switch
        to it. We never switch to an idle device and never switch on mere
        silence, so a healthy recording cannot churn or flip-flop between two
        endpoints (the 2026-09-01 one-sided-call failure was the loopback pinned
        to an idle device while the call played elsewhere; following the audio
        recovers it as soon as the far end makes a sound).

        A proactive first check shortly after start catches a call already in
        progress; after that we only probe while silent, backing off the more
        consecutive quiet probes find nothing, so a legitimately silent
        recording (mic-only, muted call) does not spawn a COM subprocess every
        few seconds for the whole meeting.

        The Communications-role device is held through a far-end pause, but
        not unconditionally: see follow_decision(). 2026-09-15 lost three
        calls because the hold kept the loopback on a comms device that never
        carried the call while every probe heard it on another jack. Having
        left the comms device, the watchdog re-probes it every
        RETURN_PROBE_EVERY seconds and goes back once RETURN_CONFIRM
        consecutive probes hear it playing.
        """
        INITIAL_CHECK = 1.5        # first proactive device check after start
        RECOVER_AFTER = 10.0       # silent this long (had signal) -> probe
        RECOVER_AFTER_NEVER = 4.0  # faster when there was never ANY signal
        RECOVER_COOLDOWN = 15.0    # base gap between probe attempts
        MAX_COOLDOWN = 30.0        # backoff ceiling: cap the follow delay at ~30s
        GRACE = 40.0               # alarm if the CURRENT device never produced signal
        # A dead WASAPI loopback delivers digital silence, so the floor sits
        # far below the speech threshold the meters use. Measured over a minute
        # of a real two-way call (2026-09-08): the loopback ran 0.0003 to 0.15,
        # and against the old 0.003 threshold it read as SILENT for stretches
        # of 16 s, which is how a healthy call kept tripping a 25 s alarm. At
        # 0.0008 the longest silent stretch in the same minute was 4 s.
        SILENT_FLOOR = 0.0008
        MIC_ACTIVE = 0.003         # the mic peaked like someone talking
        # The failure this alarm exists for (the 2026-09-01 dead loopback) is
        # permanent: the call plays to a device nobody is capturing and the
        # desktop side is silent for the whole meeting. A pause is not that.
        # Ninety seconds of true digital silence while someone is talking on
        # this end is the shape of the real thing, and it is not the shape of a
        # conversation.
        DROP_AFTER = 90.0          # alarm if a live loopback dropped
        MIC_ACTIVE_NEEDED = 45.0   # of which this much had the mic talking
        ALARM_COOLDOWN = 300.0
        STICKY_HOLD = STICKY_COMMS_HOLD_SEC  # keep the comms device through a pause this long
        RETURN_PROBE_EVERY = 45.0  # after leaving the comms device, re-check it this often
        RETURN_CONFIRM = 2         # consecutive probes that must see it playing to go back
        started = time.monotonic()
        last_signal_ts = started
        # Mic-active seconds accrued since the loopback last produced anything.
        # Without it a recording nobody is talking on (or an idle desk between
        # meetings) reads exactly like a one-sided call.
        mic_active_for = 0.0
        alarm_showing = False
        last_recover_ts = started - RECOVER_COOLDOWN
        last_alarm_ts = started - ALARM_COOLDOWN
        # grace_base resets on every switch so each newly-bound device gets its
        # own GRACE window before the "never captured" alarm can fire - a
        # successful switch to a device that is momentarily silent must not be
        # reported as a capture failure.
        grace_base = started
        fired_start = False
        did_initial = False
        quiet_probe_streak = 0

        # Following the audio to another output device is opt-in (Settings >
        # System > "Follow call audio"). Off keeps the classic behaviour: the
        # loopback stays on the device chosen at start and only the silence
        # alarm below runs. Read once per recording so a mid-recording toggle
        # applies to the next one, not this one.
        follow_output = _follow_output_enabled()

        # Set once the watchdog deliberately leaves the Communications device
        # for another output that was playing. Only then does it keep checking
        # whether the call device has come back to life, so a recording that
        # never left it costs no extra probes.
        left_comms = False
        last_return_probe_ts = started
        return_streak = 0
        last_reopen_ts = started - 10.0
        # Return to the device the user selected (see _return_to_selected_device).
        last_selected_check_ts = started
        selected_check_every = self.SELECTED_RECHECK_SEC
        # The same for the microphone (see _check_selected_mic). Checking that
        # the selected mic is the live source is cheap and runs every tick, so
        # a mic lost mid-recording gets its stand-in within seconds; looking
        # for a mic that is away lists the DirectShow devices, so it backs off.
        next_mic_check = 0.0
        mic_check_every = self.SELECTED_RECHECK_SEC

        def _try_follow_audio(silent_for: float) -> bool:
            """Probe for a live endpoint and switch ONLY to a different,
            actually-playing device. Returns True if it switched. Never raises."""
            nonlocal left_comms
            if not follow_output:
                return False
            try:
                target = self._probe_live_output(require_playing=True)
            except Exception as e:
                log.warn("audio", f"loopback probe failed: {e}")
                return False
            held = self._loopback_device_name or ""
            comms = self._last_probe_comms
            action, why = follow_decision(
                held=held, held_had_signal=self.loopback_had_signal,
                silent_for=silent_for, comms=comms, target=target,
                sticky_hold=STICKY_HOLD)
            if action == "none":
                return False
            if action == "hold":
                log.info("audio", f"Loopback: holding, {why}")
                return False
            # Cheap same-device check against this recording's device list so we
            # skip starting a new helper when already on the playing device (the
            # probe's render name lacks the ' [Loopback]' suffix, so a plain
            # compare to _loopback_device_name never matches). A target absent
            # from that list may have been connected since; restart_loopback's
            # new helper scans the devices afresh and finds it.
            cached = self._match_loopback_by_name(target, strict=True)
            if cached is not None and cached.get("name") == held:
                return False
            leaving_comms = bool(comms) and comms in held
            if leaving_comms:
                log.info("audio", f"Loopback: {why}")
            try:
                switched = self.restart_loopback(target_name=target)
            except Exception as e:
                log.warn("audio", f"loopback switch failed: {e}")
                return False
            if switched and leaving_comms:
                left_comms = True
            return switched

        def _try_return_to_comms() -> bool:
            """After leaving the call device for another playing output, go
            back the moment the call device is audibly playing again. Two
            consecutive probes must agree so a one-second notification chime on
            the idle comms device cannot drag the capture off a live call.
            Returns True if it switched back. Never raises."""
            nonlocal left_comms, return_streak
            comms = self._last_probe_comms
            try:
                target = self._probe_live_output(require_playing=True)
            except Exception as e:
                log.warn("audio", f"loopback probe failed: {e}")
                return False
            comms = self._last_probe_comms or comms
            held = self._loopback_device_name or ""
            if not (target and comms and target == comms and comms not in held):
                return_streak = 0
                return False
            return_streak += 1
            if return_streak < RETURN_CONFIRM:
                return False
            log.info("audio", f"Loopback: call device '{comms}' is playing again; "
                              f"returning to it")
            try:
                switched = self.restart_loopback(target_name=comms)
            except Exception as e:
                log.warn("audio", f"loopback switch failed: {e}")
                return False
            if switched:
                left_comms = False
                return_streak = 0
            return switched

        while self.is_running:
            # Proactive first check: a call already audible at record-start (e.g.
            # auto-record joined a call in progress) is followed within a couple
            # of seconds instead of waiting out the full silence timer.
            if not did_initial:
                time.sleep(INITIAL_CHECK)
                did_initial = True
                if not self.is_running:
                    break
                if _try_follow_audio(silent_for=0.0):
                    last_signal_ts = last_recover_ts = grace_base = time.monotonic()
                    fired_start = False
                continue

            time.sleep(2)
            now = time.monotonic()

            # The helper capturing the desktop audio exited (its output was
            # unplugged or disabled, or it crashed). Reopen at once, whatever
            # the follow setting: the same output if it still exists, else the
            # current default output until the selected one returns (below).
            stream = self._loopback_stream
            if (self.is_running and isinstance(stream, _LoopbackChild)
                    and stream.dead and now - last_reopen_ts > 5.0):
                last_reopen_ts = now
                held_render = (self._loopback_device_name or "").removesuffix(" [Loopback]")
                log.warn("audio", f"Desktop audio capture of '{held_render}' stopped; "
                                  f"reopening")
                if (self.restart_loopback(target_name=held_render, reopen_same=True)
                        or self.restart_loopback()):
                    last_signal_ts = last_recover_ts = grace_base = time.monotonic()
                    mic_active_for = 0.0
                    fired_start = False
                    continue

            # The default output stands in for the selected device only while
            # that device is unavailable: switch back as soon as it returns.
            # Only set outside Follow call audio (see _resolve_loopback), so
            # following never fights this.
            if (self.is_running and self._selected_loopback_name
                    and self._loopback_device_name != self._selected_loopback_name
                    and now - last_selected_check_ts >= selected_check_every):
                last_selected_check_ts = now
                back = self._return_to_selected_device()
                if back:
                    selected_check_every = self.SELECTED_RECHECK_SEC
                    last_signal_ts = last_recover_ts = grace_base = time.monotonic()
                    mic_active_for = 0.0
                    fired_start = False
                    continue
                if back is False:
                    selected_check_every = min(selected_check_every * 2,
                                               self.SELECTED_RECHECK_MAX_SEC)
                else:
                    # Still away. Ease off to every 30 s, so a device renamed
                    # for good (a driver update) costs little for the rest of
                    # the recording, while a reconnect is still picked up soon.
                    selected_check_every = min(selected_check_every + 5.0,
                                               max(self.SELECTED_RECHECK_SEC, 30.0))

            # The microphone: the default mic stands in only while the selected
            # one is unavailable. Nothing here touches the desktop accounting.
            if self.is_running and self._selected_mic_name:
                if self._mic_is_on_selected():
                    next_mic_check = 0.0
                    mic_check_every = self.SELECTED_RECHECK_SEC
                elif now >= next_mic_check:
                    switched_mic = self._check_selected_mic()
                    if switched_mic:
                        mic_check_every = self.SELECTED_RECHECK_SEC
                        # Back on the selected mic: watch it from the next tick,
                        # so losing it again is acted on at once. On a stand-in:
                        # look for the selected one again after the usual gap.
                        next_mic_check = (0.0 if self._mic_on_selected
                                          else time.monotonic() + mic_check_every)
                    else:
                        if switched_mic is False:
                            mic_check_every = min(mic_check_every * 2,
                                                  self.SELECTED_RECHECK_MAX_SEC)
                        else:
                            mic_check_every = min(mic_check_every + 5.0,
                                                  max(self.SELECTED_RECHECK_SEC, 30.0))
                        next_mic_check = time.monotonic() + mic_check_every

            lb_peak, mic_peak = self.take_peaks()
            if lb_peak > SILENT_FLOOR:
                last_signal_ts = now
                mic_active_for = 0.0
                quiet_probe_streak = 0
                if alarm_showing:
                    # The desktop side is back. Say so, so the banner and the
                    # toast do not outlive the problem.
                    alarm_showing = False
                    self._emit_loopback_recovered()
            elif mic_peak > MIC_ACTIVE:
                mic_active_for += 2.0
            silent_for = now - last_signal_ts

            recover_after = (RECOVER_AFTER if self.loopback_had_signal
                             else RECOVER_AFTER_NEVER)
            # Back off: each consecutive quiet probe that finds nothing new
            # widens the gap up to MAX_COOLDOWN, so a silent recording is not
            # probed every 15s for the whole meeting.
            cooldown = min(RECOVER_COOLDOWN * (2 ** min(quiet_probe_streak, 3)),
                           MAX_COOLDOWN)
            if silent_for > recover_after and now - last_recover_ts > cooldown:
                switched = _try_follow_audio(silent_for)
                now = time.monotonic()   # the probe + any switch took real time
                last_recover_ts = now
                if switched:
                    quiet_probe_streak = 0
                    # New device: give it its own signal + alarm grace so a
                    # momentary silence right after the switch is not reported
                    # as a capture failure.
                    last_signal_ts = grace_base = now
                    mic_active_for = 0.0
                    fired_start = False
                    continue
                quiet_probe_streak += 1
            elif (follow_output and left_comms and self._last_probe_comms
                    and now - last_return_probe_ts > RETURN_PROBE_EVERY):
                # We are capturing a non-call output we followed; keep an eye
                # on the call device even though the current one is not silent.
                switched = _try_return_to_comms()
                now = time.monotonic()
                last_return_probe_ts = last_recover_ts = now
                if switched:
                    quiet_probe_streak = 0
                    last_signal_ts = grace_base = now
                    mic_active_for = 0.0
                    fired_start = False
                    continue

            if not self.loopback_had_signal:
                if not fired_start and now - grace_base > GRACE:
                    fired_start = True
                    alarm_showing = True
                    last_alarm_ts = now
                    self._emit_loopback_silent("never")
            elif (self._has_mic
                    and silent_for > DROP_AFTER
                    and mic_active_for >= MIC_ACTIVE_NEEDED
                    and now - last_alarm_ts > ALARM_COOLDOWN):
                alarm_showing = True
                last_alarm_ts = now
                self._emit_loopback_silent("dropped")

    def _emit_loopback_recovered(self) -> None:
        log.info("audio", f"Loopback has signal again (device "
                          f"'{self._loopback_device_name}')")
        cb = self.on_loopback_recovered
        if cb:
            try:
                cb(self._loopback_device_name)
            except Exception as e:
                log.warn("audio", f"on_loopback_recovered callback failed: {e}")

    def _emit_loopback_silent(self, kind: str) -> None:
        log.warn("audio", f"Loopback has no signal ({kind}); desktop/call audio "
                          f"may not be captured (device '{self._loopback_device_name}')")
        cb = self.on_loopback_silent
        if cb:
            try:
                cb(self._loopback_device_name, kind)
            except Exception as e:
                log.warn("audio", f"on_loopback_silent callback failed: {e}")

    def stop(self, encode_per_source: bool = True) -> None:
        """Stop capture and finalize the mixed WAV.

        ``encode_per_source=False`` closes the per-source writers but leaves the
        Opus encode to a later ``finalize_per_source_tracks()`` call, so the
        caller can report the recording as stopped without waiting on it."""
        self.is_running = False
        # Terminate ffmpeg subprocess so the capture thread unblocks on stdout.read()
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
        # Let a device switch already under way finish, and keep a new one from
        # starting, so the streams retired below are the ones the capture ended
        # on. A switch wedged in a device open is not waited out: its streams
        # are parked instead of closed.
        switch_idle = self._loopback_restart_lock.acquire(timeout=5)
        # The same for a mic switch: one under way gives up once it sees
        # is_running is False, and one that already finished has published its
        # ffmpeg, which is ended here with the rest.
        mic_idle = self._mic_switch_lock.acquire(timeout=5)
        try:
            if self._ffmpeg_proc is not None:
                try:
                    self._ffmpeg_proc.terminate()
                except Exception:
                    pass
            # Wait for the capture and mixer threads to finish their current
            # iteration and exit (they check is_running at the top of every
            # loop). The capture loops never wait inside read(), so they leave
            # within a poll even while the desktop output is silent.
            lb_reader, mic_reader = self._loopback_thread, self._mic_thread
            for t in (self._loopback_thread, self._mic_thread, self._mixer_thread,
                      self._silence_watchdog):
                if t:
                    t.join(timeout=3)
            self._loopback_thread = None
            self._mic_thread = None
            self._mixer_thread = None
            self._silence_watchdog = None
            # Finalize WAV *after* the mixer thread has stopped - calling stop_wav()
            # while the mixer is still running is a race condition that can corrupt
            # the file or crash on a write to a closed handle.
            self.stop_wav()
            # Close the streams whose reader has gone and release PortAudio, so
            # the next recording enumerates the devices as they are by then
            # rather than as they were when this one started (see
            # _stream_graveyard for why a stream is never closed under a reader).
            if switch_idle:
                _retire_stream(self._loopback_stream, lb_reader, self._loopback_pa)
                _retire_stream(self._mic_stream, mic_reader, self._pa)
                if self._loopback_pa is not self._pa:
                    _terminate_quietly(self._loopback_pa)
                _terminate_quietly(self._pa)
            else:
                log.warn("audio", "A loopback device switch is still running; "
                                  "keeping this capture's streams open")
                _park(self._loopback_stream, self._loopback_pa)
                _park(self._mic_stream, self._pa)
            self._loopback_stream = None
            self._mic_stream = None
            self._ffmpeg_proc = None
            self._pa = None
            self._loopback_pa = None
        finally:
            if switch_idle:
                self._loopback_restart_lock.release()
            if mic_idle:
                self._mic_switch_lock.release()
        # Close the per-source tracks (mic-only / desktop-only). Done after the
        # mixer thread joins so no writes race the close. The Opus encode that
        # follows is the slowest step in stopping, so callers can defer it.
        if self._per_source_active:
            self._close_per_source_writers()
            self._per_source_active = False
            if encode_per_source:
                self.finalize_per_source_tracks()

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
        # Zero-pad to _FFT_SIZE so low-frequency bins always have enough
        # resolution (~11.7 Hz at 4096/48 kHz) regardless of buffer fill.
        padded = windowed if n >= _FFT_SIZE else np.pad(windowed, (0, _FFT_SIZE - n))
        fft_mag  = np.abs(np.fft.rfft(padded)) / (n * 0.5)   # normalise by window area
        freqs    = np.fft.rfftfreq(len(padded), d=1.0 / (self.sample_rate or 48000))

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
            if not self._mic_verified and data and data.strip(b"\x00"):
                self._mic_verified = True
                log.info("audio", f"Verified audio device "
                                  f"(microphone): {self._mic_device_name}")
            if INPUT_DEBUG:
                nsamp, peak, rms = self._chunk_stats(data)
                self._idbg_mic_inject_bytes += len(data)
                if self._idbg_throttle.ready("inject_mic"):
                    log.info("input-debug",
                             f"mic(inject) rd: bytes_total={self._idbg_mic_inject_bytes} "
                             f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                             f"q={self._mic_q.qsize()}/{self._mic_q.maxsize}")
            try:
                self._mic_q.put_nowait(data)
            except queue.Full:
                if INPUT_DEBUG:
                    self._idbg_mic_q_full_drops += 1
                    if self._idbg_throttle.ready("mic_q_full_inject"):
                        log.warn("input-debug",
                                 f"mic queue FULL on inject, dropped "
                                 f"(total drops={self._idbg_mic_q_full_drops})")

    # ── Capture threads ───────────────────────────────────────────────────────

    def _set_mic_format(self, rate: int, channels: int) -> None:
        """The mic source's native format, and the ratio the mixer resamples it
        by to reach the pipeline rate (self.sample_rate)."""
        self._mic_rate = int(rate)
        self._mic_channels = max(1, int(channels))
        if self._mic_rate != self.sample_rate:
            g = gcd(self.sample_rate, self._mic_rate)
            self._resample_up = self.sample_rate // g
            self._resample_down = self._mic_rate // g
        else:
            self._resample_up = self._resample_down = 1

    def _spawn_ffmpeg_mic(self, ffmpeg_path: str, name: str) -> subprocess.Popen:
        """Start ffmpeg capturing DirectShow mic ``name`` as 48 kHz mono s16le on
        its stdout. A method so tests can substitute a process."""
        cmd = [
            ffmpeg_path,
            "-f", "dshow",
            "-rtbufsize", "32k",         # small DirectShow buffer for low latency
            "-audio_buffer_size", "40",   # dshow audio buffer in ms (default ~500)
            "-i", f"audio={name}",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            "-fflags", "+nobuffer",       # minimize internal buffering
            "-flags", "+low_delay",
            "-loglevel", "error",
            "pipe:1",
        ]
        if INPUT_DEBUG:
            log.info("input-debug", "ffmpeg cmd: " + " ".join(
                f'"{a}"' if " " in a else a for a in cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if INPUT_DEBUG:
            log.info("input-debug",
                     f"ffmpeg pid={proc.pid} started; continuous stderr drain thread spawning")
            self._ffmpeg_stderr_thread = threading.Thread(
                target=self._ffmpeg_stderr_drain, args=(proc,), daemon=True)
            self._ffmpeg_stderr_thread.start()
        return proc

    def _ffmpeg_stderr_drain(self, proc: subprocess.Popen) -> None:
        """Continuously surface ffmpeg's stderr while INPUT_DEBUG is on.

        Without this, ffmpeg's stderr is only read after the process exits
        (see _ffmpeg_capture_loop's finally block), which means a silently
        failing dshow capture leaves no breadcrumbs. With INPUT_DEBUG on we
        run this in a side thread so every line lands in the log in real
        time, including non-fatal warnings ffmpeg emits while still alive.
        """
        try:
            while self.is_running and proc and proc.poll() is None:
                line = proc.stderr.readline() if proc.stderr else b""
                if not line:
                    break
                txt = line.decode("utf-8", errors="replace").rstrip()
                if txt:
                    log.info("input-debug", f"ffmpeg[{proc.pid}] {txt}")
        except Exception:
            if self.is_running:
                log.warn("input-debug",
                         f"ffmpeg stderr drain crashed:\n{traceback.format_exc()}")

    @staticmethod
    def _chunk_stats(data: bytes) -> tuple[int, int, float]:
        """Return (n_samples, peak_abs, rms) for an Int16 PCM byte buffer."""
        if not data:
            return 0, 0, 0.0
        arr = np.frombuffer(data, dtype=np.int16)
        if arr.size == 0:
            return 0, 0, 0.0
        peak = int(np.abs(arr).max())
        rms  = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
        return arr.size, peak, rms

    def _capture_loop(self, stream, out_queue: queue.Queue,
                      buf_size: int = 0, is_loopback: bool = False) -> None:
        chunk = buf_size or self.CHUNK_SIZE
        # A capture thread services one stream. On a live device switch the
        # stream is swapped (self._loopback_stream or self._mic_stream points at
        # the new one, or at None), so this thread's identity check goes False
        # and it exits, handing off to the new reader.
        while self.is_running and (self._loopback_stream is stream if is_loopback
                                   else self._mic_stream is stream):
            try:
                # Read however many frames WASAPI has ready, clamped to a
                # reasonable range.  This adapts to the device's actual
                # delivery cadence instead of demanding a fixed count that
                # may not align with the WASAPI shared-mode period - the
                # main cause of choppy mic input on Windows.
                avail = stream.get_read_available()
                if avail < chunk:
                    # Not enough yet: poll again shortly, never wait inside
                    # read(). A loopback delivers nothing while its output is
                    # silent, so a blocking read held this thread in PortAudio
                    # past stop()'s join, and a stream closed under a read is
                    # freed while in use (see _stream_graveyard). The count is
                    # everything buffered (GetCurrentPadding), so it crosses a
                    # chunk within a device period or two.
                    time.sleep(0.005)
                    continue
                n = min(avail, chunk * 4)  # cap to avoid huge reads
                data = stream.read(n, exception_on_overflow=False)
                if is_loopback:
                    if not self._loopback_verified and data and data.strip(b"\x00"):
                        self._loopback_verified = True
                        log.info("audio", f"Verified audio device "
                                          f"(desktop/loopback): {self._loopback_device_name}")
                else:
                    self._mic_last_data_ts = time.monotonic()
                    if not self._mic_verified and data and data.strip(b"\x00"):
                        self._mic_verified = True
                        log.info("audio", f"Verified audio device "
                                          f"(microphone): {self._mic_device_name}")
                if INPUT_DEBUG:
                    nsamp, peak, rms = self._chunk_stats(data)
                    if is_loopback:
                        self._idbg_lb_bytes += len(data)
                        self._idbg_lb_chunks += 1
                        if peak == 0:
                            self._idbg_lb_zero_chunks += 1
                        if self._idbg_throttle.ready("cap_lb"):
                            log.info("input-debug",
                                     f"loopback rd: chunks={self._idbg_lb_chunks} "
                                     f"bytes={self._idbg_lb_bytes} "
                                     f"zero_chunks={self._idbg_lb_zero_chunks} "
                                     f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                                     f"q={out_queue.qsize()}/{out_queue.maxsize}")
                    else:
                        self._idbg_mic_bytes += len(data)
                        self._idbg_mic_chunks += 1
                        if peak == 0:
                            self._idbg_mic_zero_chunks += 1
                        if self._idbg_throttle.ready("cap_mic"):
                            log.info("input-debug",
                                     f"mic(WASAPI) rd: chunks={self._idbg_mic_chunks} "
                                     f"bytes={self._idbg_mic_bytes} "
                                     f"zero_chunks={self._idbg_mic_zero_chunks} "
                                     f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                                     f"q={out_queue.qsize()}/{out_queue.maxsize}")
                try:
                    out_queue.put_nowait(data)
                except queue.Full:
                    if INPUT_DEBUG:
                        if is_loopback:
                            self._idbg_lb_q_full_drops += 1
                            if self._idbg_throttle.ready("lb_q_full"):
                                log.warn("input-debug",
                                         f"loopback queue FULL — dropped chunk "
                                         f"(total drops={self._idbg_lb_q_full_drops})")
                        else:
                            self._idbg_mic_q_full_drops += 1
                            if self._idbg_throttle.ready("mic_q_full"):
                                log.warn("input-debug",
                                         f"mic queue FULL — dropped chunk "
                                         f"(total drops={self._idbg_mic_q_full_drops})")
            except Exception:
                if not self.is_running:
                    break
                time.sleep(0.01)  # brief pause to avoid a tight error loop

    def _ffmpeg_capture_loop(self, proc: "subprocess.Popen | None" = None,
                             live: "threading.Event | None" = None) -> None:
        """Read raw PCM from an ffmpeg subprocess capturing via DirectShow.

        ``proc`` defaults to the current mic process. ``live`` is for a mic
        switch: until it is set the data is read and dropped, so the new device
        is drained from the moment ffmpeg starts (no backlog building up in the
        pipe to land late) while the old source is still the one recorded."""
        # 512 frames * 2 bytes (Int16) * 1 channel = 1024 bytes per chunk
        read_size = self.CHUNK_SIZE * 2
        proc = proc or self._ffmpeg_proc
        try:
            while self.is_running and proc and proc.poll() is None:
                data = proc.stdout.read(read_size)
                if not data:
                    if INPUT_DEBUG:
                        log.warn("input-debug",
                                 f"ffmpeg stdout returned empty — process "
                                 f"poll={proc.poll() if proc else 'n/a'}")
                    break
                if live is not None and not live.is_set():
                    continue
                self._mic_last_data_ts = time.monotonic()
                if not self._mic_verified and data.strip(b"\x00"):
                    self._mic_verified = True
                    log.info("audio", f"Verified audio device "
                                      f"(microphone): {self._mic_device_name}")
                if INPUT_DEBUG:
                    nsamp, peak, rms = self._chunk_stats(data)
                    self._idbg_mic_bytes += len(data)
                    self._idbg_mic_chunks += 1
                    if peak == 0:
                        self._idbg_mic_zero_chunks += 1
                    if self._idbg_throttle.ready("cap_mic_ffmpeg"):
                        log.info("input-debug",
                                 f"mic(ffmpeg) rd: chunks={self._idbg_mic_chunks} "
                                 f"bytes={self._idbg_mic_bytes} "
                                 f"zero_chunks={self._idbg_mic_zero_chunks} "
                                 f"last_n={nsamp} peak={peak} rms={rms:.4f} "
                                 f"q={self._mic_q.qsize()}/{self._mic_q.maxsize} "
                                 f"pid={proc.pid} poll={proc.poll()}")
                try:
                    self._mic_q.put_nowait(data)
                except queue.Full:
                    if INPUT_DEBUG:
                        self._idbg_mic_q_full_drops += 1
                        if self._idbg_throttle.ready("mic_q_full_ffmpeg"):
                            log.warn("input-debug",
                                     f"mic queue FULL — dropped chunk "
                                     f"(total drops={self._idbg_mic_q_full_drops})")
        except Exception:
            if self.is_running:
                log.warn("audio", f"ffmpeg mic capture error:\n{traceback.format_exc()}")
        finally:
            # Drain stderr for diagnostics
            if proc and proc.poll() is None:
                proc.terminate()
            if proc:
                try:
                    stderr_out = proc.stderr.read() if proc.stderr else b""
                    if proc.wait(timeout=3) != 0 and stderr_out:
                        log.warn("audio", f"ffmpeg mic exited with code {proc.returncode}: "
                                          f"{stderr_out.decode(errors='replace')[:500]}")
                except Exception:
                    pass

    # ── AGC (automatic gain control) ─────────────────────────────────────────

    @staticmethod
    def _agc_apply(chunk: np.ndarray, envelope: float, target_rms: float,
                   max_gain: float, gate_threshold: float,
                   sample_rate: int) -> tuple[np.ndarray, float, float, bool]:
        """Apply soft automatic gain to a chunk.

        Returns (gained_chunk, new_envelope, applied_gain, is_gated).

        Uses a slow-tracking RMS envelope (fast attack ~50 ms, slow release
        ~1.5 s) to compute a smooth gain multiplier.  Gain is capped at
        *max_gain* and only boosts — signals already above *target_rms* are
        left untouched (gain clamped to 1.0).

        *gate_threshold* is a noise gate: if the envelope is below this level
        the signal is treated as silence/background noise and no boost is applied.
        This prevents amplifying room tone or short noise bursts.
        """
        chunk_rms = float(np.sqrt(np.mean(chunk ** 2)))
        # Envelope time constants (in per-chunk coefficients)
        chunk_dur = len(chunk) / max(sample_rate, 1)
        attack  = 1.0 - np.exp(-chunk_dur / 0.05)   # ~50 ms attack
        release = 1.0 - np.exp(-chunk_dur / 1.5)     # ~1.5 s release
        coeff = attack if chunk_rms > envelope else release
        envelope += coeff * (chunk_rms - envelope)

        # Noise gate: don't boost signals below the gate threshold
        # (silence, room tone, brief noise bursts).
        # Only boost (gain >= 1.0).  If signal is already loud, gain = 1.
        gated = envelope <= gate_threshold
        if not gated and envelope < target_rms:
            gain = min(target_rms / envelope, max_gain)
        else:
            gain = 1.0

        # Transient protection: if the *actual* chunk RMS times the computed
        # gain would overshoot the target, instantly cap the gain so the
        # output stays near target_rms.  This prevents hard-clipping when a
        # loud speaker suddenly jumps in while the envelope is still low.
        if chunk_rms > 1e-6 and chunk_rms * gain > target_rms:
            gain = target_rms / chunk_rms

        return np.clip(chunk * gain, -1.0, 1.0), envelope, gain, gated

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
        # data - O(n²) over many calls.  Lists just append pointers, and a single
        # np.concatenate at emit time is bounded by a small number of chunks.
        lb_parts: list[np.ndarray] = []
        lb_len = 0
        mic_parts: list[np.ndarray] = []
        mic_len = 0
        # Cap internal buffers at 3 seconds to prevent unbounded growth if the
        # downstream audio_queue backs up.
        max_buf_samples = int((self.sample_rate or 48000) * 3.0)

        # AGC envelope state (per-source, persists across chunks)
        _agc_lb_env  = 0.0
        _agc_mic_env = 0.0

        # Shared WebRTC echo-cancel / noise-suppression processor (raw mic, pre-AGC).
        _mic_proc = WebRTCMicProcessor(self.CHUNK_SIZE)

        # ── Wall-clock pacing ────────────────────────────────────────────────
        # Emit exactly one CHUNK_SIZE-sized mixed chunk per wall-clock period
        # (CHUNK_SIZE / sample_rate seconds). Without this, mic and loopback
        # arrive in independent bursts and the mixer emits a fresh chunk for
        # whichever stream's data lands first, producing 2× real-time output
        # when both are active — choppy/distorted audio and audio_queue overrun.
        # With wall-clock pacing, one tick = one chunk = one slice of real time,
        # regardless of whether one or both queues happen to have data right then.
        chunk_dur = self.CHUNK_SIZE / float(self.sample_rate or 48000)
        next_emit_time = 0.0   # set on first available data so we don't emit
                               # a long stretch of silence before audio starts

        while self.is_running:
            try:
                got_data = False

                # Drain loopback queue
                try:
                    while True:
                        data = self._loopback_q.get_nowait()
                        chunk = self._to_mono_float(data, self._loopback_channels)
                        # A mid-recording switch may have landed on a device at a
                        # different native rate; resample it back to the pipeline
                        # rate so the mix / WAV / transcriber stay consistent.
                        # 1/1 (the common case) is a no-op.
                        if self._loopback_resample_up != self._loopback_resample_down:
                            chunk = resample_poly(chunk, self._loopback_resample_up,
                                                  self._loopback_resample_down)
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

                # Bootstrap the emit clock the first time data appears, so we
                # don't fire a stretch of silence before either stream has
                # produced anything. Check the parts lists (raw drained data)
                # rather than the buffer here — the buffer hasn't been built yet.
                now = time.monotonic()
                if next_emit_time == 0.0:
                    if lb_len > 0 or mic_len > 0:
                        next_emit_time = now
                    else:
                        time.sleep(0.005)
                        continue

                # If we're behind by more than 0.5s (e.g. system was paused),
                # reset the clock instead of dumping a wall of catch-up audio.
                if now - next_emit_time > 0.5:
                    next_emit_time = now

                # If it's not yet time to emit, sleep and loop. Crucially we
                # do NOT touch the parts lists here — earlier code that
                # concatenated parts into a temporary `mic_buf` on every
                # iteration silently dropped the data on sleep iterations
                # (the buffer went out of scope before being consumed).
                if now < next_emit_time:
                    time.sleep(min(0.002, next_emit_time - now))
                    continue

                # Now we're actually emitting. Flatten the part lists into
                # contiguous arrays for this single-chunk consumption.
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

                # Emit exactly ONE chunk per wall-clock tick, taking whatever
                # is in the buffers right now. Each side that has ≥CHUNK_SIZE
                # contributes its real samples; the side that doesn't is
                # zero-filled. This decouples emission rate from the burstiness
                # of either source — mic and loopback can arrive in independent
                # bursts and the output still tracks real time exactly.
                lb_pos = 0
                mic_pos = 0
                _zero_chunk = np.zeros(self.CHUNK_SIZE, dtype=np.float32)
                next_emit_time += chunk_dur
                # Single-iteration emit (kept as a `while False`-style block
                # via `if`/`pass` only to preserve the existing nested
                # structure below; we always emit exactly one chunk per tick).
                if True:
                    have_lb  = lb_pos + self.CHUNK_SIZE <= len(lb_buf)
                    have_mic = self._has_mic and mic_pos + self.CHUNK_SIZE <= len(mic_buf)

                    # ── Loopback chunk (raw; AGC applied after AEC below) ───
                    if have_lb:
                        lb_chunk = np.clip(
                            lb_buf[lb_pos:lb_pos + self.CHUNK_SIZE] * self.loopback_gain,
                            -1.0, 1.0,
                        )
                        lb_pos += self.CHUNK_SIZE
                    else:
                        lb_chunk = _zero_chunk

                    # ── Mic chunk (raw) ─────────────────────────────────────
                    if have_mic:
                        mic_chunk = np.clip(
                            mic_buf[mic_pos:mic_pos + self.CHUNK_SIZE] * self.mic_gain,
                            -1.0, 1.0,
                        )
                        mic_pos += self.CHUNK_SIZE
                    else:
                        mic_chunk = _zero_chunk

                    # ── WebRTC echo cancel / noise suppression on the raw mic ──
                    # Runs before AGC and against the raw loopback reference: AGC's
                    # time-varying gain would otherwise break the echo relationship
                    # the canceller relies on. Echo cancellation and noise
                    # suppression are independent; noise suppression runs on the mic
                    # alone, so it works even with echo cancellation off.
                    if have_mic:
                        mic_chunk = _mic_proc.process(
                            mic_chunk, lb_chunk, self.sample_rate or 16000,
                            enable_aec=self.echo_cancel_enabled,
                            enable_ns=(self.echo_cancel_enabled
                                       or self.noise_suppress_enabled),
                        )

                    # ── Loopback auto-gain (for the mix; AEC used the raw ref) ──
                    if have_lb:
                        if self.agc_loopback_enabled:
                            lb_chunk, _agc_lb_env, _g, _gated = self._agc_apply(
                                lb_chunk, _agc_lb_env, self.agc_target_rms,
                                self.agc_max_gain, self.agc_gate_threshold,
                                self.sample_rate or 48000,
                            )
                            self.agc_lb_gain = _g
                            self.agc_lb_envelope = _agc_lb_env
                            self.agc_lb_gated = _gated
                        else:
                            self.agc_lb_gain = 1.0
                            self.agc_lb_gated = True
                        lb_rms = float(np.sqrt(np.mean(lb_chunk ** 2)))
                        self.loopback_level = lb_rms
                        if lb_rms > self.loopback_peak:
                            self.loopback_peak = lb_rms
                        if not self.loopback_had_signal and lb_rms > 0.003:
                            self.loopback_had_signal = True
                        self._lb_fft_buf.extend(lb_chunk.tolist())
                    else:
                        lb_rms = 0.0
                        self.loopback_level = 0.0
                        self.agc_lb_gain = 1.0
                        self.agc_lb_gated = True

                    # ── Mic auto-gain. Bypassed whenever the mic is being cleaned
                    # (echo cancellation or noise suppression on) so the suppressed
                    # echo residual / background noise is not re-boosted straight
                    # back up by the gain stage. ────────────────────────────────
                    if have_mic:
                        if (self.agc_mic_enabled and not self.echo_cancel_enabled
                                and not self.noise_suppress_enabled):
                            mic_chunk, _agc_mic_env, _g, _gated = self._agc_apply(
                                mic_chunk, _agc_mic_env, self.agc_target_rms,
                                self.agc_max_gain, self.agc_gate_threshold,
                                self.sample_rate or 48000,
                            )
                            self.agc_mic_gain = _g
                            self.agc_mic_envelope = _agc_mic_env
                            self.agc_mic_gated = _gated
                        else:
                            self.agc_mic_gain = 1.0
                            self.agc_mic_gated = True
                        mic_rms = float(np.sqrt(np.mean(mic_chunk ** 2)))
                        self.mic_level = mic_rms
                        if mic_rms > self.mic_peak:
                            self.mic_peak = mic_rms
                        self._mic_fft_buf.extend(mic_chunk.tolist())
                    else:
                        mic_rms = 0.0
                        self.mic_level = 0.0
                        self.agc_mic_gain = 1.0
                        self.agc_mic_gated = True

                    # ── Mix: always sum. The previous "louder side wins"
                    # gate was muting the mic the moment desktop audio got
                    # loud, which is the opposite of what a meeting tool
                    # should do. Both sources are clipped before summing
                    # and the sum itself is clipped, so headroom is fine.
                    if have_lb and have_mic:
                        src = "both"
                    elif have_mic:
                        src = "mic"
                    else:
                        src = "loopback"
                    mixed = np.clip(lb_chunk + mic_chunk, -1.0, 1.0)

                    if INPUT_DEBUG:
                        self._idbg_mix_src_counts[src] += 1
                        self._idbg_mix_emitted += 1
                        if self._idbg_throttle.ready("mix"):
                            cnt = self._idbg_mix_src_counts
                            log.info("input-debug",
                                     f"mix: lb_rms={lb_rms:.4f} mic_rms={mic_rms:.4f} "
                                     f"have_lb={have_lb} have_mic={have_mic} "
                                     f"-> src={src} | "
                                     f"emitted={self._idbg_mix_emitted} "
                                     f"src_counts={cnt} "
                                     f"audio_q={self.audio_queue.qsize()}/"
                                     f"{self.audio_queue.maxsize} "
                                     f"agc_lb={self.agc_lb_gain:.2f}/gated={self.agc_lb_gated} "
                                     f"agc_mic={self.agc_mic_gain:.2f}/gated={self.agc_mic_gated} "
                                     f"echo_cancel={self.echo_cancel_enabled}")

                    int16_bytes = (mixed * 32767).astype(np.int16).tobytes()

                    # Write to WAV (before queue - never lose audio even if queue is full)
                    sample_offset = -1
                    if self.wav_writer is not None:
                        sample_offset = self.wav_writer.write(int16_bytes)

                    # Per-source ("mic = Me") tracks: write the desktop-only and
                    # mic-only chunks every tick (zeros included) so they stay
                    # sample-aligned with the mix, and hand the transcriber the
                    # separated PCM. The mixed writer remains the single source of
                    # truth for sample_offset / video sync.
                    mic_int16 = lb_int16 = None
                    if self._per_source_active:
                        mic_int16 = (mic_chunk * 32767).astype(np.int16).tobytes()
                        lb_int16  = (lb_chunk * 32767).astype(np.int16).tobytes()
                        if self._desktop_wav_writer is not None:
                            self._desktop_wav_writer.write(lb_int16)
                        if self._mic_wav_writer is not None:
                            self._mic_wav_writer.write(mic_int16)

                    try:
                        if self._per_source_active:
                            self.audio_queue.put_nowait(
                                (src, int16_bytes, sample_offset, mic_int16, lb_int16))
                        else:
                            self.audio_queue.put_nowait((src, int16_bytes, sample_offset))
                    except queue.Full:
                        if INPUT_DEBUG:
                            self._idbg_audio_q_full_drops += 1
                            if self._idbg_throttle.ready("audio_q_full"):
                                log.warn("input-debug",
                                         f"audio_queue FULL — dropped chunk "
                                         f"(total drops={self._idbg_audio_q_full_drops}, "
                                         f"src={src})")

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

                # Pacing sleep handled at top of loop via next_emit_time.
                # We deliberately do NOT sleep here — if we just emitted a
                # chunk and we're already past the next deadline (catch-up),
                # we should immediately loop and emit another.

            except Exception:
                # Log but never let the mixer thread die silently
                traceback.print_exc()
                time.sleep(0.05)


def auto_detect_devices() -> dict:
    """Test all audio devices simultaneously and return the best ones.

    Opens every loopback and dshow mic device in parallel, plays a system
    chime so loopback devices have signal, captures ~2 s of audio, then
    picks the devices with the highest RMS.

    Returns {"best_loopback": {...}, "best_mic": {...}, "loopback": [...], "mic": [...]}.
    """
    stop_event = threading.Event()

    # ── Enumerate ────────────────────────────────────────────────────────
    # Each loopback is captured by its own helper process (a fresh device scan,
    # see _LoopbackChild), so the list includes outputs connected since the
    # app started.
    snapshot = _fresh_device_snapshot()
    loopbacks = list(snapshot.get_loopback_device_info_generator()) if snapshot else []
    dshow_mics = enumerate_dshow_audio_devices()
    log.info("auto-detect", f"Found {len(loopbacks)} loopback, {len(dshow_mics)} dshow mic devices")

    # ── Open every loopback in its own helper ────────────────────────────
    # The helpers start side by side: a new Python process takes 3 to 5 s on
    # some machines, and starting one per output in turn made Auto-detect wait
    # that long for every output in the list before the tone even played.
    opened: list = [None] * len(loopbacks)
    opened_lock = threading.Lock()
    collected = False   # set once the list below is taken; a later opener closes its own

    def _open_loopback(slot: int, lb: dict) -> None:
        child = None
        try:
            child = _LoopbackChild()
            info = next((d for d in child.devices.get_loopback_device_info_generator()
                         if d["name"] == lb["name"]), None)
            if info is None:
                raise RuntimeError("gone before it could be opened")
            child.open(info, channels=max(1, info["maxInputChannels"]),
                       rate=int(info["defaultSampleRate"]),
                       frames_per_buffer=AudioCapture._compute_loopback_buffer_size(info),
                       chunk=512)
            with opened_lock:
                if not collected:
                    opened[slot] = (info, child)
                    child = None   # handed over; retired with the others below
            if child is None:
                log.info("auto-detect", f"  Opened loopback: {lb['name']}")
        except Exception as e:
            log.warn("auto-detect", f"  Failed loopback '{lb['name']}': {e}")
        finally:
            if child is not None:
                child.close()

    openers = [threading.Thread(target=_open_loopback, args=(i, lb), daemon=True)
               for i, lb in enumerate(loopbacks)]
    for t in openers:
        t.start()
    for t in openers:
        t.join(timeout=30)
    with opened_lock:
        collected = True
        lb_streams: list[tuple[dict, object, list]] = [  # (info, stream, data_chunks)
            (info, child, []) for info, child in (o for o in opened if o is not None)]

    # ── Spawn ffmpeg for each dshow mic ──────────────────────────────────
    from capture_video import find_ffmpeg
    ffmpeg_path = find_ffmpeg()
    mic_procs: list[tuple[dict, subprocess.Popen, list]] = []  # (info, proc, data_chunks)
    if ffmpeg_path:
        for mic in dshow_mics:
            try:
                proc = subprocess.Popen(
                    [ffmpeg_path, "-f", "dshow",
                     "-i", f"audio={mic['name']}",
                     "-f", "s16le", "-ar", "48000", "-ac", "1",
                     "-loglevel", "error", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                mic_procs.append((mic, proc, []))
                log.info("auto-detect", f"  Opened dshow mic: {mic['name']}")
            except Exception as e:
                log.warn("auto-detect", f"  Failed dshow '{mic['name']}': {e}")

    # ── Reader threads ───────────────────────────────────────────────────
    def _lb_reader(stream, buf, stop_ev):
        # Polls rather than waiting inside read(): most of these endpoints are
        # silent, and a reader parked in read() makes its stream unsafe to
        # close (see _stream_graveyard).
        while not stop_ev.is_set():
            try:
                avail = stream.get_read_available()
                if avail < 512:
                    time.sleep(0.005)
                    continue
                buf.append(stream.read(min(avail, 2048), exception_on_overflow=False))
            except Exception:
                if not stop_ev.is_set():
                    break

    def _mic_reader(proc, buf, stop_ev):
        while not stop_ev.is_set():
            try:
                data = proc.stdout.read(1024)
                if not data:
                    break
                buf.append(data)
            except Exception:
                break

    threads: list[threading.Thread] = []
    for _, stream, buf in lb_streams:
        t = threading.Thread(target=_lb_reader, args=(stream, buf, stop_event), daemon=True)
        t.start()
        threads.append(t)
    for _, proc, buf in mic_procs:
        t = threading.Thread(target=_mic_reader, args=(proc, buf, stop_event), daemon=True)
        t.start()
        threads.append(t)

    # ── Play test sample through default audio output ──────────────────
    from pathlib import Path
    sample_path = Path(__file__).parent / "audio" / "test_sample.mp3"

    time.sleep(0.3)  # let streams stabilize

    def _play_sample():
        try:
            from playsound import playsound
            playsound(str(sample_path))
        except Exception as e:
            log.warn("auto-detect", f"  playsound failed: {e}")

    if sample_path.exists():
        log.info("auto-detect", f"  Playing test sample: {sample_path.name}")
        play_thread = threading.Thread(target=_play_sample, daemon=True)
        play_thread.start()
    else:
        log.warn("auto-detect", f"  Test sample not found: {sample_path}")

    time.sleep(3.0)  # capture window — matches the 3s sample duration
    stop_event.set()
    for t in threads:
        t.join(timeout=1)

    # ── Compute RMS per device ───────────────────────────────────────────
    def _compute_rms(chunks: list[bytes]) -> float:
        if not chunks:
            return 0.0
        raw = b"".join(chunks)
        if len(raw) < 2:
            return 0.0
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(samples ** 2)))

    lb_results = []
    for info, stream, buf in lb_streams:
        rms = _compute_rms(buf)
        lb_results.append({"index": int(info["index"]), "name": info["name"],
                           "rms": round(rms, 6)})
        log.info("auto-detect", f"  Loopback '{info['name']}': RMS={rms:.6f}")

    mic_results = []
    for info, proc, buf in mic_procs:
        rms = _compute_rms(buf)
        mic_results.append({"name": info["name"], "rms": round(rms, 6)})
        log.info("auto-detect", f"  Mic '{info['name']}': RMS={rms:.6f}")

    # ── Cleanup ──────────────────────────────────────────────────────────
    # The first len(lb_streams) threads are the loopback readers, in order.
    for (_, stream, _), reader in zip(lb_streams, threads):
        _retire_stream(stream, reader, None)   # kills the helper

    for _, proc, _ in mic_procs:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass

    # ── Pick winners ─────────────────────────────────────────────────────
    lb_results.sort(key=lambda d: d["rms"], reverse=True)
    mic_results.sort(key=lambda d: d["rms"], reverse=True)

    best_lb = lb_results[0] if lb_results else None
    best_mic = mic_results[0] if mic_results else None

    if best_lb:
        log.info("auto-detect", f"  >> Best loopback: '{best_lb['name']}' (RMS={best_lb['rms']:.6f})")
    if best_mic:
        log.info("auto-detect", f"  >> Best mic: '{best_mic['name']}' (RMS={best_mic['rms']:.6f})")

    # ── Play completion chime ───────────────────────────────────────────
    complete_path = Path(__file__).parent / "audio" / "complete.mp3"
    if complete_path.exists():
        def _play_complete():
            try:
                from playsound import playsound
                playsound(str(complete_path))
            except Exception:
                pass
        threading.Thread(target=_play_complete, daemon=True).start()

    return {
        "best_loopback": best_lb,
        "best_mic": best_mic,
        "loopback": lb_results,
        "mic": mic_results,
    }


def default_device_name_matches(output_name: str, loopback_name: str) -> bool:
    """Check if a loopback device corresponds to the given output device."""
    return output_name in loopback_name


def enumerate_audio_devices() -> dict:
    """
    Return lists of available loopback and microphone input devices.
    Scans in a helper process (see _fresh_device_snapshot): this process's
    PortAudio list is frozen at the first recording, which hid headphones
    plugged in later from the recorder's device menu. Falls back to a
    temporary in-process PyAudio. Safe to call even while recording is active.

    Input devices are filtered to WASAPI only (same API used for capture)
    to avoid showing the same physical device three times (MME / DirectSound /
    WASAPI) and to exclude loopback virtual devices from the mic list.
    """
    snapshot = _fresh_device_snapshot()
    if snapshot is not None:
        return _list_audio_devices(snapshot)
    pa = pyaudio.PyAudio()
    try:
        return _list_audio_devices(pa)
    finally:
        pa.terminate()


def _list_audio_devices(pa) -> dict:
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
        # WASAPI only - skip MME / DirectSound duplicates
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


def enumerate_dshow_audio_devices() -> list[dict]:
    """List DirectShow audio input devices via ffmpeg.

    Returns a list of {"name": "..."} dicts.  These names are what ffmpeg
    expects in ``-i audio=<name>``.  Returns an empty list if ffmpeg is
    unavailable or the query fails.
    """
    from capture_video import find_ffmpeg
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return []
    try:
        result = subprocess.run(
            [ffmpeg_path, "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # ffmpeg prints device list to stderr
        output = result.stderr
    except Exception:
        return []

    devices: list[dict] = []
    for line in output.splitlines():
        # "Alternative name" lines follow the device line and carry the stable
        # @device_cm_{GUID}\wave_{GUID} ID — attach to the last device.
        if "Alternative name" in line:
            m = re.search(r'"(.+?)"', line)
            if m and devices:
                devices[-1]["alt_name"] = m.group(1)
            continue
        # Device lines look like: [in#0 @ ...] "Device Name" (audio)
        if "(audio)" not in line.lower():
            continue
        m = re.search(r'"(.+?)"', line)
        if m:
            devices.append({"name": m.group(1)})
    return devices


def resolve_dshow_mic_name(requested: str) -> tuple[str | None, str]:
    """Re-resolve a saved DirectShow mic name against the current device list.

    Device friendly names can change between sessions (driver updates, USB
    re-enumeration) and devices can be unplugged entirely, so the name we
    persisted may no longer match anything ffmpeg can open. This re-queries
    ffmpeg's live device list and picks the best surviving match.

    Resolution order:
      1. Exact match on friendly name.
      2. Exact match on the alternative (GUID) name — survives friendly-name
         changes for the same physical device.
      3. Case-insensitive friendly-name match.
      4. Substring match (requested ⊂ candidate or candidate ⊂ requested).

    Returns (resolved_name, reason). resolved_name is None if nothing matched.
    The reason string is suitable for logging (e.g. "exact", "alt-name",
    "substring", "no-match").
    """
    if not requested:
        return None, "empty-request"
    devices = enumerate_dshow_audio_devices()
    if not devices:
        return None, "enumeration-failed"

    # 1. Exact friendly-name match
    for d in devices:
        if d.get("name") == requested:
            return requested, "exact"

    # 2. Alternative-name match: requested may itself be an alt name, or a
    #    previously-resolved alt may still exist under a different friendly name.
    for d in devices:
        if d.get("alt_name") == requested:
            return d["name"], "alt-name"

    # 3. Case-insensitive
    req_lower = requested.lower()
    for d in devices:
        if d.get("name", "").lower() == req_lower:
            return d["name"], "case-insensitive"

    # 4. Substring (prefer longest candidate name)
    cand: list[tuple[int, str]] = []
    for d in devices:
        name = d.get("name", "")
        if not name:
            continue
        nl = name.lower()
        if req_lower in nl or nl in req_lower:
            cand.append((len(name), name))
    if cand:
        cand.sort(reverse=True)
        return cand[0][1], "substring"

    return None, "no-match"
