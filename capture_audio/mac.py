"""
macOS audio capture (ScreenCaptureKit loopback + CoreAudio mic via sounddevice).

System audio "loopback" on macOS comes from Apple's ScreenCaptureKit framework
(macOS 13+) — the same API Zoom, Loom, OBS, and Audio Hijack use today. SCK
captures the system audio mix directly, with no virtual audio driver, no
aggregate device, and no system-output reroute. Permission is the standard
Screen & System Audio Recording prompt under Privacy & Security.

Mic capture goes through the default CoreAudio input or any user-selected
device via sounddevice. The browser-mic path (mic_index=-2) and the
avfoundation subprocess path (mic_index=-3) both work the same as on Windows
because they're platform-agnostic at the audio layer.

Public API matches audio_capture_win.py exactly so the dispatcher can swap
backends without app.py noticing.
"""
from __future__ import annotations

import collections
import ctypes
import os
import queue
import re
import subprocess
import threading
import time
import traceback
from math import gcd
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from core import log as log
from capture_audio._webrtc import WebRTCMicProcessor
from capture_audio.wav_writer import WavWriter

# FFT window size for the spectrum visualizer.  2048 samples ≈ 43 ms at 48 kHz,
# giving ~23 Hz frequency resolution.
_FFT_SIZE = 4096
_N_BARS   = 32

# SCK loopback runs at this fixed rate, matching the rest of the pipeline's
# expected system-audio sample rate.
_SCK_SAMPLE_RATE = 48000
_SCK_CHANNELS = 2  # stereo; downmixed to mono in _mixer_loop

# Virtual loopback/sink devices that must never be offered (or auto-picked)
# as the mic source — selecting one feeds system audio back in labelled as
# 'mic', corrupting speaker attribution. Matched case-insensitively as
# substrings. Deliberately NOT listed: 'aggregate' — aggregate devices are
# user-built and often wrap a real microphone, so hiding them would break
# legitimate setups. app.py's mac filter reuses this constant.
_HIDDEN_INPUT_NAME_PARTS = (
    "blackhole",
    "meeting assistant output",
    "microsoft teams audio",  # Teams' virtual sink, exposed as an input
    "zoomaudiodevice",        # Zoom's virtual audio device
    "soundflower",            # legacy virtual loopback driver
    "loopback",               # Rogue Amoeba Loopback virtual devices
)

# Watchdog: SCK delivers audio sample buffers continuously even during
# silence (verified live on this machine: ~1200 buffers in 4 quiet seconds),
# so a delivery gap of this many seconds means the stream is dead — not that
# the room is quiet.
_SCK_WATCHDOG_TIMEOUT = 5.0
# Restart backoff between supervisor attempts (audiotee supervisor pattern).
_SCK_RESTART_BACKOFF = (0.5, 1.0, 2.0)


def _is_hidden_input_device(name: str) -> bool:
    lowered = name.lower()
    return any(part in lowered for part in _HIDDEN_INPUT_NAME_PARTS)


def _input_device_info(index: int) -> dict | None:
    """Return a safe CoreAudio input device dict, excluding virtual loopbacks."""
    try:
        raw = sd.query_devices(index)
    except Exception:
        return None
    if raw.get("max_input_channels", 0) <= 0:
        return None
    if _is_hidden_input_device(raw.get("name", "")):
        return None
    return {
        "index": int(index),
        "name": raw["name"],
        "default_samplerate": int(raw.get("default_samplerate") or 48000),
        "max_input_channels": int(raw["max_input_channels"]),
    }


class _SCKLoopbackStream:
    """ScreenCaptureKit-backed system audio loopback.

    Configures an SCStream on the main display with audio capture enabled,
    receives CMSampleBuffers in a delegate, extracts Int16 PCM, and pushes
    raw bytes onto the AudioCapture._loopback_q.

    Permission: triggers the Screen & System Audio Recording TCC prompt on
    first start. If denied, start() raises with an actionable message.
    """

    def __init__(self, out_queue: queue.Queue, channels: int = _SCK_CHANNELS):
        self._out_queue = out_queue
        self._channels = channels
        self._stream = None
        self._delegate = None
        self._stream_delegate = None
        self._sample_queue = None
        self._started = False
        self._stopping = False
        # Frame size in samples (per channel). Matches AudioCapture.CHUNK_SIZE
        # so the mixer loop sees familiar granularity.
        self._frame_samples = 512
        # Liveness signals read by the AudioCapture watchdog. The audio
        # delegate bumps last_audio_monotonic on every audio sample buffer;
        # the stream delegate sets died/died_error on stream:didStopWithError:.
        self.last_audio_monotonic: float = 0.0
        self.died: bool = False
        self.died_error: str | None = None

    def start(self) -> None:
        # PyObjC imports are deferred to start() so module import never fails
        # on platforms without ScreenCaptureKit (e.g. older macOS).
        try:
            import objc  # noqa: F401
            import Foundation  # noqa: F401
            from ScreenCaptureKit import (
                SCStream, SCStreamConfiguration, SCContentFilter,
                SCShareableContent, SCStreamOutputTypeAudio,
                SCStreamOutputTypeScreen,
            )
        except ImportError as e:
            raise RuntimeError(
                "ScreenCaptureKit unavailable. Install dependencies with "
                "`pip install pyobjc-framework-ScreenCaptureKit "
                "pyobjc-framework-CoreMedia` and ensure macOS 13+."
            ) from e

        # ── Discover shareable content (synchronous wait on async API) ────
        result: dict = {}
        ev = threading.Event()

        def _content_handler(content, error):
            if error is not None:
                result["error"] = str(error)
            else:
                result["content"] = content
            ev.set()

        SCShareableContent.getShareableContentWithCompletionHandler_(_content_handler)
        if not ev.wait(timeout=10):
            raise RuntimeError(
                "ScreenCaptureKit timed out enumerating shareable content. "
                "Grant Screen & System Audio Recording permission in "
                "System Settings → Privacy & Security, then restart."
            )
        if "error" in result:
            raise RuntimeError(
                f"ScreenCaptureKit permission denied or unavailable: {result['error']}. "
                f"Grant Screen & System Audio Recording permission in "
                f"System Settings → Privacy & Security."
            )

        content = result["content"]
        displays = content.displays()
        if displays is None or len(displays) == 0:
            raise RuntimeError("ScreenCaptureKit reported no displays.")
        display = displays[0]

        # ── Build content filter and audio-only stream config ────────────
        # Filter on the main display with no app exclusions; v1 captures the
        # full system audio mix.
        content_filter = SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
            display, [], [],
        )

        config = SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setExcludesCurrentProcessAudio_(False)
        config.setSampleRate_(_SCK_SAMPLE_RATE)
        config.setChannelCount_(self._channels)
        # Registering a screen output keeps SCK's stream clock moving. Throttle
        # video to 1 fps, but use the display's real dimensions so SCK doesn't
        # silently skip frame delivery on newer macOS builds.
        config.setWidth_(int(display.width()))
        config.setHeight_(int(display.height()))
        config.setMinimumFrameInterval_(_make_cmtime(1, 1))  # 1 fps cap
        config.setShowsCursor_(False)

        # ── Delegate: receive CMSampleBuffers, push PCM to queue ─────────
        delegate_cls = _build_sck_audio_delegate()
        delegate = delegate_cls.alloc().init()
        delegate.configureWithQueue_channels_owner_(self._out_queue, self._channels, self)
        # Stream delegate: hears stream:didStopWithError: so a stream that
        # dies mid-recording (TCC revoked, display reconfig, user clicks Stop
        # in the system pill) is reported instead of silently starving the
        # mixer. The AudioCapture watchdog acts on the died flag it sets.
        stream_delegate_cls = _build_sck_stream_delegate()
        stream_delegate = stream_delegate_cls.alloc().init()
        stream_delegate.configureWithOwner_(self)
        self._sample_queue = _make_dispatch_queue(b"com.meetingassistant.sck.audio")

        stream = SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, config, stream_delegate,
        )

        err_holder: dict = {}
        ok = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            delegate, SCStreamOutputTypeScreen, self._sample_queue, None,
        )
        if isinstance(ok, tuple):
            ok, err = ok[0], ok[1]
            if err is not None:
                err_holder["err"] = str(err)
        if not ok:
            raise RuntimeError(
                f"SCStream add screen output failed: {err_holder.get('err', 'unknown error')}"
            )

        ok = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            delegate, SCStreamOutputTypeAudio, self._sample_queue, None,
        )
        # Older PyObjC bindings return an NSError tuple; newer ones a bool.
        if isinstance(ok, tuple):
            ok, err = ok[0], ok[1]
            if err is not None:
                err_holder["err"] = str(err)
        if not ok:
            raise RuntimeError(
                f"SCStream add audio output failed: {err_holder.get('err', 'unknown error')}"
            )

        start_ev = threading.Event()
        start_result: dict = {}

        def _start_handler(error):
            if error is not None:
                start_result["error"] = str(error)
            start_ev.set()

        # Keep live references BEFORE the async start: if startCapture times
        # out or errors, the capture may still come up afterwards, and these
        # are needed for teardown — otherwise an unreachable SCStream keeps
        # recording (purple indicator) until the process exits.
        self._stream = stream
        self._delegate = delegate
        self._stream_delegate = stream_delegate

        start_mono = time.monotonic()
        self.last_audio_monotonic = start_mono  # watchdog baseline
        stream.startCaptureWithCompletionHandler_(_start_handler)
        # The completion handler is flaky on some macOS builds (observed live
        # on 26.5: buffers flowing, handler silent), so poll for either the
        # handler or delivered audio. The 1 s grace period gives a real error
        # completion (e.g. permission denial) time to land first. The window
        # is 20 s because SCK warm-up was measured taking ~20 s on this
        # machine; healthy starts exit the loop as soon as audio flows.
        confirmed = False
        live_without_completion = False
        while time.monotonic() - start_mono < 20.0:
            if start_ev.wait(timeout=0.25):
                confirmed = True
                break
            if (self.last_audio_monotonic > start_mono
                    and time.monotonic() - start_mono >= 1.0):
                live_without_completion = True
                break
        if "error" in start_result:
            self._abort_start()
            raise RuntimeError(
                f"SCStream startCapture failed: {start_result['error']}. "
                f"Verify Screen & System Audio Recording permission."
            )
        if not confirmed and not live_without_completion:
            self._abort_start()
            raise RuntimeError("SCStream startCapture timed out.")
        if live_without_completion:
            log.warn("audio", "SCStream startCapture completion never fired but "
                              "audio is flowing - treating stream as live")

        # Reseed the watchdog baseline to "now". On the confirmed branch the
        # completion handler can fire before the first audio sample buffer is
        # delivered, so last_audio_monotonic is still the pre-startCapture
        # timestamp. If the handler took more than the watchdog timeout to land
        # (plausible inside the 20 s warm-up budget), the very first watchdog
        # tick would see a stale clock and tear down a healthy, still-warming-up
        # stream. Anchoring to the moment we treat the stream as live closes that
        # window without masking a genuinely stalled stream thereafter.
        self.last_audio_monotonic = time.monotonic()
        self._started = True
        log.info("audio", f"ScreenCaptureKit loopback started @ {_SCK_SAMPLE_RATE} Hz, "
                          f"{self._channels} ch")

    def _abort_start(self) -> None:
        """Best-effort teardown after a failed/timed-out startCapture.

        The async start may still complete later, so stop the stream rather
        than just dropping the reference — otherwise an unreachable live
        capture (purple indicator) survives the error path.
        """
        stream, self._stream = self._stream, None
        delegate, self._delegate = self._delegate, None
        stream_delegate, self._stream_delegate = self._stream_delegate, None
        self._sample_queue = None
        if stream is not None:
            try:
                stream.stopCaptureWithCompletionHandler_(lambda error: None)
            except Exception as e:
                log.warn("audio", f"SCStream abort-stop failed: {e}")
        for d in (delegate, stream_delegate):
            if d is not None:
                try:
                    d.detach()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stopping = True  # suppress didStopWithError death reports
        if not self._started or self._stream is None:
            return
        ev = threading.Event()

        def _stop_handler(error):
            ev.set()

        try:
            self._stream.stopCaptureWithCompletionHandler_(_stop_handler)
            ev.wait(timeout=5)
        except Exception as e:
            log.warn("audio", f"SCStream stopCapture error: {e}")
        # Drop the delegates' back-references so the Python/ObjC reference
        # cycle (stream object ↔ delegates) can't outlive the capture.
        for d in (self._delegate, self._stream_delegate):
            if d is not None:
                try:
                    d.detach()
                except Exception:
                    pass
        self._stream = None
        self._delegate = None
        self._stream_delegate = None
        self._sample_queue = None
        self._started = False


def _make_cmtime(value: int, timescale: int):
    """Build a CMTime for SCStreamConfiguration.setMinimumFrameInterval_."""
    from CoreMedia import CMTimeMake
    return CMTimeMake(value, timescale)


# One shared serial queue for all SCK sample callbacks, created once and
# reused across capture starts. dispatch_queue_create returns a +1-owned
# reference that objc.objc_object() does NOT take ownership of (the proxy
# adds its own retain), so creating a fresh queue per start() leaked one
# queue per recording session. A serial queue can serve any number of
# successive SCStreams, so caching is the simplest leak-free fix.
_SCK_DISPATCH_QUEUE = None


def _make_dispatch_queue(label: bytes):
    """Return the shared serial libdispatch queue for SCK sample callbacks.

    The label only applies on first call; later calls reuse the cached queue
    regardless of label (there is a single call site).
    """
    global _SCK_DISPATCH_QUEUE
    if _SCK_DISPATCH_QUEUE is not None:
        return _SCK_DISPATCH_QUEUE
    import objc
    libdispatch = ctypes.CDLL("/usr/lib/system/libdispatch.dylib")
    libdispatch.dispatch_queue_create.restype = ctypes.c_void_p
    libdispatch.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    ptr = libdispatch.dispatch_queue_create(label, None)
    if not ptr:
        raise RuntimeError("dispatch_queue_create failed for ScreenCaptureKit audio")
    _SCK_DISPATCH_QUEUE = objc.objc_object(c_void_p=ptr)
    return _SCK_DISPATCH_QUEUE


# Define the SCK delegate class. Using objc.python_method / lazy class creation
# so the module imports cleanly on non-darwin platforms (where Foundation
# isn't available). The class is built on first instantiation.
_SCKAudioDelegate = None  # populated below

def _build_sck_audio_delegate():
    """Define the Objective-C delegate class lazily (mac-only)."""
    global _SCKAudioDelegate
    if _SCKAudioDelegate is not None:
        return _SCKAudioDelegate

    import objc
    import ScreenCaptureKit  # noqa: F401  # registers SCStreamOutput protocol
    from Foundation import NSObject

    class SCKAudioDelegate(NSObject, protocols=[objc.protocolNamed("SCStreamOutput")]):
        # Instance attrs initialised by configure_().
        def init(self):
            self = objc.super(SCKAudioDelegate, self).init()
            if self is None:
                return None
            self._queue = None
            self._channels = _SCK_CHANNELS
            self._owner = None
            self._error_logged = False
            self._seen_output_types = set()
            return self

        def configureWithQueue_channels_owner_(self, q, ch, owner):
            self._queue = q
            self._channels = int(ch)
            self._owner = owner

        def detach(self):
            # Called on stop/abort; breaks the owner back-reference.
            self._owner = None
            self._queue = None

        # Selector: stream:didOutputSampleBuffer:ofType:
        @objc.typedSelector(b"v@:@^{opaqueCMSampleBuffer=}q")
        def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
            # output_type: 0=screen, 1=audio (SCStreamOutputTypeAudio)
            if output_type not in self._seen_output_types:
                self._seen_output_types.add(output_type)
                log.info("audio", f"ScreenCaptureKit sample callback type={output_type}")
            if output_type != 1:
                return
            owner = self._owner
            if owner is not None:
                # Liveness signal for the AudioCapture watchdog.
                owner.last_audio_monotonic = time.monotonic()
            try:
                pcm = _extract_int16_from_sample_buffer(sample_buffer, self._channels)
                if pcm and self._queue is not None:
                    try:
                        self._queue.put_nowait(pcm)
                    except queue.Full:
                        pass
            except Exception:
                # Don't propagate exceptions across the ObjC boundary.
                if not self._error_logged:
                    self._error_logged = True
                    log.warn("audio", "ScreenCaptureKit audio delegate failed to extract PCM")
                pass

    # Patch configure_ to take (queue, channels) directly. PyObjC selectors
    # treat trailing underscores as argument slots, so configure_ takes one arg.
    _SCKAudioDelegate = SCKAudioDelegate
    return _SCKAudioDelegate


# Stream (lifecycle) delegate, distinct from the sample-output delegate above.
_SCKStreamDelegate = None  # populated below


def _build_sck_stream_delegate():
    """Define the SCStreamDelegate class lazily (mac-only).

    Implements stream:didStopWithError: so a stream that dies mid-recording
    (TCC revoked, display reconfiguration, user clicks Stop in the system
    pill) sets the died flag on its owning _SCKLoopbackStream instead of
    silently going quiet. The AudioCapture watchdog reacts to the flag.
    """
    global _SCKStreamDelegate
    if _SCKStreamDelegate is not None:
        return _SCKStreamDelegate

    import objc
    import ScreenCaptureKit  # noqa: F401  # registers SCStreamDelegate protocol
    from Foundation import NSObject

    class SCKStreamDelegate(NSObject, protocols=[objc.protocolNamed("SCStreamDelegate")]):
        def init(self):
            self = objc.super(SCKStreamDelegate, self).init()
            if self is None:
                return None
            self._owner = None
            return self

        def configureWithOwner_(self, owner):
            self._owner = owner

        def detach(self):
            self._owner = None

        # Selector: stream:didStopWithError:
        def stream_didStopWithError_(self, stream, error):
            owner = self._owner
            if owner is not None and owner._stopping:
                return  # expected: we initiated this stop
            msg = str(error) if error is not None else "unknown error"
            log.error("audio", f"ScreenCaptureKit stream stopped unexpectedly: {msg}")
            if owner is not None:
                owner.died_error = msg
                owner.died = True

    _SCKStreamDelegate = SCKStreamDelegate
    return _SCKStreamDelegate


def _extract_int16_from_sample_buffer(sample_buffer, channels: int) -> bytes | None:
    """Convert a CMSampleBuffer of Float32 audio to interleaved Int16 bytes.

    SCK delivers audio as planar (non-interleaved) Float32. We read the raw
    bytes from the underlying CMBlockBuffer, interleave channels, clamp, and
    quantize to Int16 — the format the rest of the pipeline expects.
    """
    from CoreMedia import (
        CMSampleBufferGetNumSamples,
        CMSampleBufferGetDataBuffer,
        CMBlockBufferGetDataLength,
        CMBlockBufferCopyDataBytes,
    )

    n_samples = CMSampleBufferGetNumSamples(sample_buffer)
    if n_samples <= 0:
        return None

    block = CMSampleBufferGetDataBuffer(sample_buffer)
    if block is None:
        return None

    total_bytes = CMBlockBufferGetDataLength(block)
    if total_bytes <= 0:
        return None

    buf = (ctypes.c_byte * total_bytes)()
    # PyObjC returns (status, filled_buffer) for the out-parameter variant,
    # not a bare OSStatus — unpack before comparing, or every buffer is
    # silently rejected.
    result = CMBlockBufferCopyDataBytes(block, 0, total_bytes, buf)
    if isinstance(result, tuple):
        status = result[0]
        if len(result) > 1 and result[1] is not None:
            buf = result[1]
    else:
        status = result
    if status != 0:
        return None

    floats = np.frombuffer(bytes(buf), dtype=np.float32)
    expected = n_samples * channels
    if len(floats) < expected:
        return None

    if channels > 1:
        # Planar layout: [c0_s0..c0_sN, c1_s0..c1_sN]. Interleave to
        # [c0_s0, c1_s0, c0_s1, c1_s1, ...] for downstream consumers.
        try:
            interleaved = floats[:expected].reshape(channels, n_samples).T.reshape(-1)
        except Exception:
            interleaved = floats[:expected]
    else:
        interleaved = floats[:expected]

    int16 = (np.clip(interleaved, -1.0, 1.0) * 32767.0).astype(np.int16)
    return int16.tobytes()


class AudioCapture:
    CHUNK_SIZE = 512
    # sounddevice uses numpy dtype strings rather than PortAudio constants.
    SD_DTYPE = "int16"

    def __init__(self, audio_queue: queue.Queue):
        self.audio_queue = audio_queue
        self.is_running = False
        self._sck_loopback: _SCKLoopbackStream | None = None
        self._mic_stream: sd.RawInputStream | None = None
        self._mic_thread: threading.Thread | None = None
        self._mixer_thread: threading.Thread | None = None
        self._sck_watchdog_thread: threading.Thread | None = None
        # Set when the SCK loopback dies and the supervisor gives up; callers
        # can poll it to surface the failure instead of recording silence.
        self.loopback_error: str | None = None

        self.sample_rate: int | None = None
        self.channels: int = 1

        self._loopback_q: queue.Queue = queue.Queue(maxsize=200)
        self._mic_q: queue.Queue = queue.Queue(maxsize=200)

        self._loopback_channels: int = _SCK_CHANNELS
        self._mic_rate: int | None = None
        self._mic_channels: int = 1
        self._has_mic: bool = False
        self._mic_buf_size: int = 1024
        self._resample_up: int = 1
        self._resample_down: int = 1

        self.wav_writer: WavWriter | None = None
        self._wav_path: str | None = None
        self._wav_append: bool = False

        # Per-source recording ("mic = Me" feature). When enabled and a mic is
        # present, the mixer also writes a mic-only and a desktop-only track to
        # temp WAVs sample-aligned with the mix; on stop() they are encoded to
        # Opus ({sid}_mic.opus / {sid}_desktop.opus) and the temp WAVs deleted.
        # Reanalysis re-separates from these so mic audio is always the app user.
        # Mirrors capture_audio/windows.py so macOS behaves identically.
        self.mic_is_me_enabled: bool = False
        self._mic_wav_writer: WavWriter | None = None
        self._desktop_wav_writer: WavWriter | None = None
        self._mic_wav_path: str | None = None
        self._desktop_wav_path: str | None = None
        self._per_source_active: bool = False

        self.loopback_level: float = 0.0
        self.mic_level: float = 0.0

        self.loopback_gain: float = 1.0
        self.mic_gain: float = 1.0

        self.echo_cancel_enabled: bool = False
        # Noise suppression, independent of echo cancellation. Echo cancellation
        # always includes it; this flag enables it on its own (e.g. to stop the
        # custom AGC boosting quiet background noise when echo cancellation is off).
        self.noise_suppress_enabled: bool = False

        self.agc_loopback_enabled: bool = True
        self.agc_mic_enabled: bool = True
        self.agc_target_rms: float = 0.15
        self.agc_max_gain: float = 4.0
        self.agc_gate_threshold: float = 0.005

        self.agc_lb_gain: float = 1.0
        self.agc_lb_envelope: float = 0.0
        self.agc_lb_gated: bool = True
        self.agc_mic_gain: float = 1.0
        self.agc_mic_envelope: float = 0.0
        self.agc_mic_gated: bool = True

        # Desktop-bleed rejection for the per-source "mic = Me" track lives in the
        # transcriber now (a segment-level decision, see Transcriber._mic_flush).
        # Here we only report each tick's RAW (pre-gain, pre-AEC) mic and loopback
        # levels alongside the per-source PCM so the transcriber can compare, over
        # a whole segment, whether the mic carried genuine near-end speech or was
        # just the desktop bleeding in. Reporting raw levels (not the AGC-boosted
        # visualiser levels) keeps the comparison volume-honest, and deciding per
        # segment instead of per chunk avoids chopping word onsets into fragments
        # that Whisper then transcribes as spurious "You" lines.

        self._lb_fft_buf:  collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._mic_fft_buf: collections.deque = collections.deque(maxlen=_FFT_SIZE)
        self._hann_window: np.ndarray | None = None

        # avfoundation subprocess mic capture (mic_index=-3 on Mac)
        self._ffmpeg_proc: subprocess.Popen | None = None
        self._ffmpeg_mic_name: str | None = None

    # ── Device discovery ──────────────────────────────────────────────────────

    @staticmethod
    def _list_input_devices() -> list[dict]:
        """Return all CoreAudio input devices (with at least one input channel)."""
        out: list[dict] = []
        try:
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) > 0:
                    if _is_hidden_input_device(dev.get("name", "")):
                        continue
                    out.append({
                        "index": idx,
                        "name": dev["name"],
                        "default_samplerate": int(dev.get("default_samplerate") or 48000),
                        "max_input_channels": int(dev["max_input_channels"]),
                    })
        except Exception as e:
            log.warn("audio", f"sd.query_devices failed: {e}")
        return out

    def _find_mic_device(self) -> dict | None:
        """Return the system default CoreAudio input device."""
        try:
            default_in_idx = sd.default.device[0]
        except Exception:
            default_in_idx = None
        if default_in_idx is not None and default_in_idx >= 0:
            info = _input_device_info(int(default_in_idx))
            if info:
                return info

        # Fallback: first input device with at least one channel.
        devs = self._list_input_devices()
        return devs[0] if devs else None

    # ── WAV recording ──────────────────────────────────────────────────────

    def start_wav(self, path: str, append: bool = False) -> None:
        self._wav_path = path
        self._wav_append = append

    def stop_wav(self) -> None:
        if self.wav_writer is not None:
            self.wav_writer.close()
            self.wav_writer = None

    # ── Per-source ("mic = Me") tracks ─────────────────────────────────────
    # Lifted verbatim from capture_audio/windows.py so the macOS source-aware
    # diarization path is byte-for-byte the same shape downstream.

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
                 "-acodec", "pcm_s16le", "-ar", str(self.sample_rate or 48000),
                 "-ac", "1", wav_path],
                capture_output=True, timeout=300,
                creationflags=subprocess_no_window_flag(),
            )
            return os.path.isfile(wav_path)
        except Exception:
            log.warn("audio", f"Could not decode {os.path.basename(opus_path)} for resume")
            return False

    def _encode_per_source_opus(self) -> None:
        """Encode the per-source temp WAVs to Opus and delete the temps. Called
        from stop() after writers are closed. Failures are non-fatal: the temp
        WAV is kept so reanalysis can still fall back to it."""
        from capture_video.ffmpeg_util import find_ffmpeg, subprocess_no_window_flag
        ffmpeg = find_ffmpeg()
        pairs = []
        if self._mic_wav_path:
            pairs.append((self._mic_wav_path, self._per_source_paths_opus(self._mic_wav_path)))
        if self._desktop_wav_path:
            pairs.append((self._desktop_wav_path, self._per_source_paths_opus(self._desktop_wav_path)))
        for wav_path, opus_path in pairs:
            if not os.path.isfile(wav_path):
                continue
            if not ffmpeg:
                log.warn("audio", "ffmpeg not found - keeping per-source WAV (no Opus encode)")
                continue
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
        self._mic_wav_path = None
        self._desktop_wav_path = None

    @staticmethod
    def _per_source_paths_opus(wav_path: str) -> str:
        """Map a per-source temp WAV path to its Opus sibling."""
        return wav_path[:-4] + ".opus" if wav_path.lower().endswith(".wav") else wav_path + ".opus"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self, loopback_index: int | None = None, mic_index: int | None = None,
              ffmpeg_mic_name: str | None = None) -> None:
        """Start capture with the same semantics as the Windows backend:
            mic_index=-1  explicitly disable mic
            mic_index=-2  receive mic audio injected from the browser
            mic_index=-3  capture via ffmpeg avfoundation subprocess

        loopback_index is accepted for signature compatibility but ignored —
        SCK is the only system-audio path on macOS.
        """
        # ── Loopback (ScreenCaptureKit) ──────────────────────────────────
        self.sample_rate = _SCK_SAMPLE_RATE
        self._loopback_channels = _SCK_CHANNELS
        self.loopback_error = None

        self._sck_loopback = _SCKLoopbackStream(self._loopback_q, channels=_SCK_CHANNELS)
        self._sck_loopback.start()

        # ── Microphone (best-effort, multi-mode) ─────────────────────────
        mic_info = None

        if mic_index == -3:
            # avfoundation subprocess via ffmpeg.
            from capture_video.ffmpeg_util import find_ffmpeg
            ffmpeg_path = find_ffmpeg()
            if not ffmpeg_path:
                log.warn("audio", "ffmpeg not found - cannot use ffmpeg mic capture")
            elif not ffmpeg_mic_name:
                log.warn("audio", "No avfoundation mic device name provided")
            elif _is_hidden_input_device(ffmpeg_mic_name):
                log.warn("audio", f"Ignoring hidden virtual mic '{ffmpeg_mic_name}'")
                mic_info = self._find_mic_device()
            else:
                self._mic_rate = 48000
                self._mic_channels = 1
                self._has_mic = True
                self._ffmpeg_mic_name = ffmpeg_mic_name
                if self._mic_rate != self.sample_rate:
                    g = gcd(self.sample_rate, self._mic_rate)
                    self._resample_up   = self.sample_rate // g
                    self._resample_down = self._mic_rate    // g
                cmd = [
                    ffmpeg_path,
                    "-f", "avfoundation",
                    # NB: do NOT pass -audio_buffer_size here. That is a
                    # DirectShow (Windows) input option; avfoundation rejects it
                    # with "Unrecognized option 'audio_buffer_size'" and ffmpeg
                    # exits immediately, so the mic captures nothing. Low latency
                    # is handled by -fflags +nobuffer / -flags +low_delay below.
                    "-i", f":{ffmpeg_mic_name}",
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "-ar", str(self._mic_rate),
                    "-ac", "1",
                    "-fflags", "+nobuffer",
                    "-flags", "+low_delay",
                    "-loglevel", "error",
                    "pipe:1",
                ]
                log.info("audio", f"Mic: ffmpeg avfoundation '{ffmpeg_mic_name}' "
                                  f"@ {self._mic_rate} Hz, 1 ch")
                self._ffmpeg_proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
        elif mic_index == -2:
            # Browser mic: data arrives via inject_mic_data().
            self._mic_rate = 48000
            self._mic_channels = 1
            self._has_mic = True
            if self._mic_rate != self.sample_rate:
                g = gcd(self.sample_rate, self._mic_rate)
                self._resample_up   = self.sample_rate // g
                self._resample_down = self._mic_rate    // g
            log.info("audio", f"Mic: browser (inject_mic_data) @ {self._mic_rate} Hz, 1 ch")
        elif mic_index == -1:
            pass  # explicitly disabled
        elif mic_index is not None:
            mic_info = _input_device_info(int(mic_index))
            if mic_info is None:
                log.warn("audio", f"Specified mic device {mic_index} unavailable or hidden")
                mic_info = self._find_mic_device()
        else:
            mic_info = self._find_mic_device()

        if mic_info:
            try:
                self._mic_rate = int(mic_info["default_samplerate"])
                self._mic_channels = max(1, mic_info["max_input_channels"])
                self._mic_buf_size = 1024
                self._mic_stream = sd.RawInputStream(
                    samplerate=self._mic_rate,
                    channels=self._mic_channels,
                    dtype=self.SD_DTYPE,
                    blocksize=self._mic_buf_size,
                    device=mic_info["index"],
                )
                self._mic_stream.start()
                self._has_mic = True
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
            self._has_mic = False
            if mic_index == -1:
                log.info("audio", "Microphone explicitly disabled - capturing loopback only.")
            else:
                log.info("audio", "No microphone device found - capturing loopback only.")

        base_wav_path = self._wav_path
        if self._wav_path:
            self.wav_writer = WavWriter(self._wav_path, self.sample_rate, append=self._wav_append)
            self._wav_path = None

        # Per-source tracks for the "mic = Me" feature. Only meaningful when a
        # mic is actually present (otherwise mixed == desktop) and we're writing
        # to a file (not the live audio-test path). Mirrors capture_audio/windows.py.
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

        self.is_running = True

        if self._has_mic and self._ffmpeg_proc is not None:
            self._mic_thread = threading.Thread(
                target=self._ffmpeg_capture_loop,
                daemon=True,
            )
            self._mic_thread.start()
        elif self._has_mic and self._mic_stream is not None:
            self._mic_thread = threading.Thread(
                target=self._capture_loop,
                args=(self._mic_stream, self._mic_q, self._mic_channels),
                daemon=True,
            )
            self._mic_thread.start()

        self._mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True)
        self._mixer_thread.start()

        self._sck_watchdog_thread = threading.Thread(
            target=self._sck_watchdog_loop, daemon=True,
        )
        self._sck_watchdog_thread.start()

    def stop(self) -> None:
        self.is_running = False
        if self._ffmpeg_proc is not None:
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
        # Join the watchdog before tearing down the SCK stream so it can't
        # race a restart against this shutdown.
        for t in (self._mic_thread, self._mixer_thread, self._sck_watchdog_thread):
            if t:
                t.join(timeout=3)
        self._mic_thread = None
        self._mixer_thread = None
        self._sck_watchdog_thread = None

        self.stop_wav()

        # Close + encode the per-source tracks (mic-only / desktop-only) to Opus.
        # Done after the mixer thread joins so no writes race the close. Encode
        # failures are non-fatal (the temp WAV is kept as a reanalysis fallback).
        if self._per_source_active:
            self._close_per_source_writers()
            try:
                self._encode_per_source_opus()
            except Exception:
                log.warn("audio", "Per-source Opus encode raised; per-source temp "
                                  "WAVs left in place.")
                traceback.print_exc()
            self._per_source_active = False

        if self._sck_loopback is not None:
            try:
                self._sck_loopback.stop()
            except Exception as e:
                log.warn("audio", f"SCK loopback stop failed: {e}")
            self._sck_loopback = None

        if self._mic_stream is not None:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
        self._mic_stream = None
        self._ffmpeg_proc = None

    # ── SCK supervision (watchdog + restart) ──────────────────────────────

    def _sck_watchdog_loop(self) -> None:
        """Supervise the SCK loopback and restart it if it dies mid-recording.

        Death signals: the stream delegate's didStopWithError flag, or no
        audio sample buffers for _SCK_WATCHDOG_TIMEOUT seconds — SCK delivers
        buffers continuously even during silence, so a gap means a dead
        stream (TCC revoked, display reconfig), not a quiet room.
        """
        while self.is_running:
            time.sleep(0.5)
            sck = self._sck_loopback
            if not self.is_running or sck is None:
                continue
            stalled_for = time.monotonic() - sck.last_audio_monotonic
            if not sck.died and stalled_for <= _SCK_WATCHDOG_TIMEOUT:
                continue
            reason = (sck.died_error if sck.died
                      else f"no audio buffers for {stalled_for:.1f}s")
            log.error("audio", f"ScreenCaptureKit loopback died ({reason}) - "
                               f"attempting restart")
            if self._restart_sck_loopback():
                self.loopback_error = None
            else:
                self.loopback_error = (
                    f"System audio capture died ({reason}) and could not be "
                    f"restarted - loopback audio is no longer being recorded."
                )
                log.error("audio", self.loopback_error)
                return

    def _restart_sck_loopback(self) -> bool:
        """Tear down and restart the SCK loopback (audiotee supervisor
        pattern: 0.5/1/2 s backoff). Returns True once a new stream is live."""
        for attempt, backoff in enumerate(_SCK_RESTART_BACKOFF, start=1):
            if not self.is_running:
                return False
            old = self._sck_loopback
            if old is not None:
                try:
                    old.stop()
                except Exception as e:
                    log.warn("audio", f"SCK restart: old stream stop failed: {e}")
            time.sleep(backoff)
            if not self.is_running:
                return False
            try:
                sck = _SCKLoopbackStream(self._loopback_q, channels=_SCK_CHANNELS)
                sck.start()
                if not self.is_running:
                    # stop() ran while we were restarting — tear the new
                    # stream down instead of orphaning a live capture.
                    sck.stop()
                    return False
                self._sck_loopback = sck
                log.info("audio", f"ScreenCaptureKit loopback restarted "
                                  f"(attempt {attempt}/{len(_SCK_RESTART_BACKOFF)})")
                return True
            except Exception as e:
                log.error("audio", f"SCK restart attempt "
                                   f"{attempt}/{len(_SCK_RESTART_BACKOFF)} failed: {e}")
        return False

    def compute_spectrum(self, buf: collections.deque) -> list[float]:
        """Return _N_BARS log-spaced frequency magnitudes from the sample buffer."""
        if len(buf) < _FFT_SIZE // 4:
            return [0.0] * _N_BARS

        samples = np.array(buf, dtype=np.float32)
        n = len(samples)

        if self._hann_window is None or len(self._hann_window) != n:
            self._hann_window = np.hanning(n).astype(np.float32)

        windowed = samples * self._hann_window
        padded = windowed if n >= _FFT_SIZE else np.pad(windowed, (0, _FFT_SIZE - n))
        fft_mag = np.abs(np.fft.rfft(padded)) / (n * 0.5)
        freqs   = np.fft.rfftfreq(len(padded), d=1.0 / (self.sample_rate or 48000))

        f_min = 40.0
        f_max = min(20000.0, (self.sample_rate or 48000) / 2.0)
        edges = np.logspace(np.log10(f_min), np.log10(f_max), _N_BARS + 1)

        result: list[float] = []
        for i in range(_N_BARS):
            mask = (freqs >= edges[i]) & (freqs < edges[i + 1])
            val  = float(np.mean(fft_mag[mask])) if mask.any() else 0.0
            result.append(round(min(1.0, (val * 80) ** 0.5), 4))
        return result

    def inject_mic_data(self, data: bytes) -> None:
        if self.is_running and self._has_mic:
            try:
                self._mic_q.put_nowait(data)
            except queue.Full:
                pass

    # ── Capture threads ───────────────────────────────────────────────────────

    def _capture_loop(self, stream: sd.RawInputStream, out_queue: queue.Queue,
                      channels: int) -> None:
        """Read sounddevice RawInputStream into the queue. Blocks per chunk."""
        while self.is_running:
            try:
                data, overflowed = stream.read(self.CHUNK_SIZE)
                if overflowed:
                    log.warn("audio", "sounddevice input overflow")
                payload = bytes(data)
                try:
                    out_queue.put_nowait(payload)
                except queue.Full:
                    pass
            except Exception:
                if not self.is_running:
                    break
                time.sleep(0.01)

    def _ffmpeg_capture_loop(self) -> None:
        """Read raw PCM from an ffmpeg avfoundation subprocess."""
        read_size = self.CHUNK_SIZE * 2
        proc = self._ffmpeg_proc
        try:
            while self.is_running and proc and proc.poll() is None:
                data = proc.stdout.read(read_size)
                if not data:
                    break
                try:
                    self._mic_q.put_nowait(data)
                except queue.Full:
                    pass
        except Exception:
            if self.is_running:
                log.warn("audio", f"ffmpeg mic capture error:\n{traceback.format_exc()}")
        finally:
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

    # ── DSP helpers (mirror audio_capture_win.py exactly) ─────────────────

    @staticmethod
    def _agc_apply(chunk: np.ndarray, envelope: float, target_rms: float,
                   max_gain: float, gate_threshold: float,
                   sample_rate: int) -> tuple[np.ndarray, float, float, bool]:
        chunk_rms = float(np.sqrt(np.mean(chunk ** 2)))
        chunk_dur = len(chunk) / max(sample_rate, 1)
        attack  = 1.0 - np.exp(-chunk_dur / 0.05)
        release = 1.0 - np.exp(-chunk_dur / 1.5)
        coeff = attack if chunk_rms > envelope else release
        envelope += coeff * (chunk_rms - envelope)

        gated = envelope <= gate_threshold
        if not gated and envelope < target_rms:
            gain = min(target_rms / envelope, max_gain)
        else:
            gain = 1.0

        if chunk_rms > 1e-6 and chunk_rms * gain > target_rms:
            gain = target_rms / chunk_rms

        return np.clip(chunk * gain, -1.0, 1.0), envelope, gain, gated

    @staticmethod
    def _to_mono_float(data: bytes, channels: int) -> np.ndarray:
        samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return samples

    def _mixer_loop(self) -> None:
        # Pure DSP, no platform calls. Matches capture_audio/windows.py so the
        # macOS pipeline behaves identically: wall-clock-paced emission, always
        # sum (never "louder side wins"), zero-fill the absent side, and the
        # "mic = Me" per-source 5-tuple. (The Windows-only INPUT_DEBUG tracing
        # is omitted here; it is a dev aid and does not affect output.)
        lb_parts: list[np.ndarray] = []
        lb_len = 0
        mic_parts: list[np.ndarray] = []
        mic_len = 0
        # Cap internal buffers at 3 seconds to prevent unbounded growth if the
        # downstream audio_queue backs up.
        max_buf_samples = int((self.sample_rate or 48000) * 3.0)

        _agc_lb_env  = 0.0
        _agc_mic_env = 0.0

        # Shared WebRTC echo-cancel / noise-suppression processor (raw mic, pre-AGC).
        _mic_proc = WebRTCMicProcessor(self.CHUNK_SIZE)

        # ── Wall-clock pacing ────────────────────────────────────────────────
        # Emit exactly one CHUNK_SIZE-sized mixed chunk per wall-clock period
        # (CHUNK_SIZE / sample_rate seconds). Without this, mic and loopback
        # arrive in independent bursts and the mixer emits a fresh chunk for
        # whichever stream's data lands first, producing 2x real-time output
        # when both are active. With wall-clock pacing, one tick = one chunk =
        # one slice of real time, regardless of which queue has data right then.
        chunk_dur = self.CHUNK_SIZE / float(self.sample_rate or 48000)
        next_emit_time = 0.0   # set on first available data

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

                # Bootstrap the emit clock the first time data appears, so we
                # don't fire a stretch of silence before either stream has
                # produced anything.
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

                # If it's not yet time to emit, sleep and loop. We do NOT touch
                # the parts lists here.
                if now < next_emit_time:
                    time.sleep(min(0.002, next_emit_time - now))
                    continue

                # Flatten the part lists into contiguous arrays for this
                # single-chunk consumption.
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

                # Emit exactly ONE chunk per wall-clock tick, taking whatever is
                # in the buffers right now. Each side that has >=CHUNK_SIZE
                # contributes its real samples; the side that doesn't is
                # zero-filled. This decouples emission rate from the burstiness
                # of either source.
                lb_pos = 0
                mic_pos = 0
                _zero_chunk = np.zeros(self.CHUNK_SIZE, dtype=np.float32)
                next_emit_time += chunk_dur
                if True:
                    have_lb  = lb_pos + self.CHUNK_SIZE <= len(lb_buf)
                    have_mic = self._has_mic and mic_pos + self.CHUNK_SIZE <= len(mic_buf)

                    # ── Raw chunks (pre-AGC) ────────────────────────────────
                    # Take both sources at their captured level first. Echo
                    # cancellation and noise suppression run on these RAW signals
                    # below, BEFORE any auto-gain: AGC's time-varying gain
                    # destroys the linear echo relationship the canceller relies
                    # on (which is why desktop bleed survived with AGC on), and
                    # boosting before suppression would amplify the noise floor.
                    # AGC is therefore deferred until after this block.
                    # RAW (pre-gain) RMS of each source is captured here, before any
                    # user gain / AEC / AGC, so the transcriber's bleed gate compares
                    # the true acoustic relationship rather than boosted levels.
                    if have_lb:
                        lb_slice = lb_buf[lb_pos:lb_pos + self.CHUNK_SIZE]
                        lb_chunk = np.clip(lb_slice * self.loopback_gain, -1.0, 1.0)
                        raw_lb_rms = float(np.sqrt(np.mean(lb_slice ** 2)))
                        lb_pos += self.CHUNK_SIZE
                    else:
                        lb_chunk = _zero_chunk
                        raw_lb_rms = 0.0
                    if have_mic:
                        mic_slice = mic_buf[mic_pos:mic_pos + self.CHUNK_SIZE]
                        mic_chunk = np.clip(mic_slice * self.mic_gain, -1.0, 1.0)
                        raw_mic_rms = float(np.sqrt(np.mean(mic_slice ** 2)))
                        mic_pos += self.CHUNK_SIZE
                    else:
                        mic_chunk = _zero_chunk
                        raw_mic_rms = 0.0

                    # ── WebRTC echo cancel / noise suppression on the raw mic ──
                    # Runs on the RAW mic, before any auto-gain. Echo cancellation
                    # and noise suppression are independent: noise suppression also
                    # runs on its own (no loopback needed), so it can lower the mic
                    # noise floor even when echo cancellation is off. AEC is fed the
                    # loopback as its reference every tick (zeros when no desktop).
                    # The custom mic AGC below is bypassed while echo cancellation is
                    # on, so the cleaned residual is not re-boosted.
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
                        self._lb_fft_buf.extend(lb_chunk.tolist())
                    else:
                        lb_rms = 0.0
                        self.loopback_level = 0.0
                        self.agc_lb_gain = 1.0
                        self.agc_lb_gated = True

                    # ── Mic auto-gain ───────────────────────────────────────────
                    # Bypassed whenever the mic is being cleaned (echo cancellation
                    # or noise suppression on): any gain stage would just re-boost
                    # what was suppressed straight back up. That is the "desktop
                    # still bleeds through" / "background noise boosted 4x" you get
                    # otherwise, since the AGC's gate cannot tell loud room noise from
                    # speech. With both off the custom AGC runs exactly as before.
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
                        # Report to the visualiser AFTER echo-cancel, noise
                        # suppression and AGC so the bar reflects what is actually
                        # captured, not the raw, boosted mic.
                        mic_rms = float(np.sqrt(np.mean(mic_chunk ** 2)))
                        self.mic_level = mic_rms
                        self._mic_fft_buf.extend(mic_chunk.tolist())
                    else:
                        mic_rms = 0.0
                        self.mic_level = 0.0
                        self.agc_mic_gain = 1.0
                        self.agc_mic_gated = True

                    # ── Mix: always sum. Both sources are clipped before
                    # summing and the sum itself is clipped, so headroom is fine.
                    if have_lb and have_mic:
                        src = "both"
                    elif have_mic:
                        src = "mic"
                    else:
                        src = "loopback"
                    mixed = np.clip(lb_chunk + mic_chunk, -1.0, 1.0)

                    int16_bytes = (mixed * 32767).astype(np.int16).tobytes()

                    # Write to WAV (before queue - never lose audio even if queue is full)
                    sample_offset = -1
                    if self.wav_writer is not None:
                        sample_offset = self.wav_writer.write(int16_bytes)

                    # Per-source ("mic = Me") tracks: write the desktop-only and
                    # mic-only chunks every tick (zeros included) so they stay
                    # sample-aligned with the mix, and hand the transcriber the
                    # separated PCM. The mixed writer remains the single source
                    # of truth for sample_offset / video sync.
                    mic_int16 = lb_int16 = None
                    if self._per_source_active:
                        # The mic track is written intact (no per-chunk muting): the
                        # transcriber drops whole bleed-only segments, which avoids
                        # chopping word onsets into transcribable fragments.
                        mic_int16 = (mic_chunk * 32767).astype(np.int16).tobytes()
                        lb_int16  = (lb_chunk * 32767).astype(np.int16).tobytes()
                        if self._desktop_wav_writer is not None:
                            self._desktop_wav_writer.write(lb_int16)
                        if self._mic_wav_writer is not None:
                            self._mic_wav_writer.write(mic_int16)

                    try:
                        if self._per_source_active:
                            self.audio_queue.put_nowait(
                                (src, int16_bytes, sample_offset, mic_int16, lb_int16,
                                 (raw_mic_rms, raw_lb_rms)))
                        else:
                            self.audio_queue.put_nowait((src, int16_bytes, sample_offset))
                    except queue.Full:
                        pass

                # Keep leftover samples (less than CHUNK_SIZE) for next iteration
                if lb_pos < len(lb_buf):
                    lb_parts.append(lb_buf[lb_pos:])
                    lb_len = len(lb_buf) - lb_pos
                if mic_pos < len(mic_buf):
                    mic_parts.append(mic_buf[mic_pos:])
                    mic_len = len(mic_buf) - mic_pos

                # Backpressure: discard oldest data if buffers exceed the cap.
                if lb_len > max_buf_samples:
                    lb_parts.clear()
                    lb_len = 0
                if mic_len > max_buf_samples:
                    mic_parts.clear()
                    mic_len = 0

                # Pacing handled at top via next_emit_time; no sleep here so a
                # catch-up emit can happen immediately.

            except Exception:
                traceback.print_exc()
                time.sleep(0.05)


# ── Module-level enumeration / auto-detect ──────────────────────────────────

# Synthetic loopback entry returned to the frontend. SCK has no device list —
# system audio is captured via the OS-level Screen Recording permission.
_SCK_LOOPBACK_ENTRY = {"index": 0, "name": "System Audio (ScreenCaptureKit)"}


def enumerate_audio_devices() -> dict:
    """Return {'loopback': [...], 'input': [...]}.

    On macOS the loopback list is a single synthetic entry — actual system
    audio capture uses ScreenCaptureKit and isn't a CoreAudio device. The
    input list is every CoreAudio input device.
    """
    inputs: list[dict] = []
    try:
        for idx, dev in enumerate(sd.query_devices()):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            if _is_hidden_input_device(dev.get("name", "")):
                continue
            inputs.append({"index": idx, "name": dev["name"]})
    except Exception as e:
        log.warn("audio", f"enumerate_audio_devices failed: {e}")
    return {"loopback": [_SCK_LOOPBACK_ENTRY], "input": inputs}


def enumerate_dshow_audio_devices() -> list[dict]:
    """List avfoundation audio input devices via ffmpeg.

    Naming kept as 'dshow' to match the Windows API for callers; on macOS
    we query avfoundation. Returns [{'name': '...'}] as ffmpeg expects in
    `-i :<name>` arguments.
    """
    from capture_video.ffmpeg_util import find_ffmpeg
    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return []
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
        output = result.stderr or ""
    except Exception:
        return []

    devices: list[dict] = []
    in_audio_section = False
    for line in output.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if "AVFoundation video devices" in line:
            in_audio_section = False
            continue
        if not in_audio_section:
            continue
        # Lines look like: [AVFoundation indev @ ...] [0] MacBook Pro Microphone
        m = re.search(r"\]\s*\[\d+\]\s+(.+?)$", line.rstrip())
        if m:
            name = m.group(1).strip()
            if name and not _is_hidden_input_device(name):
                devices.append({"name": name})
    return devices


def _play_audio_file(p: Path, device: int | str | None = None) -> None:
    """Play an audio file through `device` (or the system default if None)."""
    try:
        import soundfile as sf  # type: ignore
        data, sr = sf.read(str(p), dtype="float32", always_2d=False)
        sd.play(data, samplerate=sr, device=device, blocking=True)
    except Exception as e:
        log.warn("auto-detect", f"  audio playback failed for {p.name}: {e}")


def auto_detect_devices() -> dict:
    """Open all input devices in parallel, play a chime, capture ~3 s, rank by RMS.

    On macOS with SCK, loopback isn't a CoreAudio device so it's never probed
    — the result always reports the synthetic SCK entry as the loopback. We
    still rank microphones by signal strength so the user gets a sane default.
    """
    stop_event = threading.Event()

    all_inputs = AudioCapture._list_input_devices()
    log.info("auto-detect", f"Found {len(all_inputs)} CoreAudio input devices")

    streams: list[tuple[dict, sd.RawInputStream, list]] = []
    for dev in all_inputs:
        try:
            stream = sd.RawInputStream(
                samplerate=dev["default_samplerate"],
                channels=max(1, dev["max_input_channels"]),
                dtype="int16",
                blocksize=512,
                device=dev["index"],
            )
            stream.start()
            streams.append((dev, stream, []))
            log.info("auto-detect", f"  Opened mic: {dev['name']}")
        except Exception as e:
            log.warn("auto-detect", f"  Failed mic '{dev['name']}': {e}")

    def _reader(stream, buf, channels, stop_ev):
        while not stop_ev.is_set():
            try:
                data, _of = stream.read(512)
                buf.append(bytes(data))
            except Exception:
                if not stop_ev.is_set():
                    break

    threads: list[threading.Thread] = []
    for dev, stream, buf in streams:
        t = threading.Thread(
            target=_reader,
            args=(stream, buf, max(1, dev["max_input_channels"]), stop_event),
            daemon=True,
        )
        t.start()
        threads.append(t)

    sample_path = Path(__file__).parent / "audio" / "test_sample.mp3"
    time.sleep(0.3)

    if sample_path.exists():
        log.info("auto-detect", f"  Playing test sample: {sample_path.name}")
        threading.Thread(
            target=_play_audio_file, args=(sample_path, None), daemon=True,
        ).start()

    time.sleep(3.0)
    stop_event.set()
    for t in threads:
        t.join(timeout=1)

    def _compute_rms(chunks: list[bytes]) -> float:
        if not chunks:
            return 0.0
        raw = b"".join(chunks)
        if len(raw) < 2:
            return 0.0
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return float(np.sqrt(np.mean(samples ** 2)))

    mic_results: list[dict] = []
    for dev, stream, buf in streams:
        rms = _compute_rms(buf)
        entry = {"index": int(dev["index"]), "name": dev["name"], "rms": round(rms, 6)}
        mic_results.append(entry)
        log.info("auto-detect", f"  mic '{dev['name']}': RMS={rms:.6f}")

    for _, stream, _ in streams:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    mic_results.sort(key=lambda d: d["rms"], reverse=True)

    best_mic = mic_results[0] if mic_results else None
    best_lb = dict(_SCK_LOOPBACK_ENTRY)  # always the SCK synthetic entry

    log.info("auto-detect", "  >> Loopback: ScreenCaptureKit (system audio)")
    if best_mic:
        log.info("auto-detect", f"  >> Best mic: '{best_mic['name']}' (RMS={best_mic['rms']:.6f})")

    complete_path = Path(__file__).parent / "audio" / "complete.mp3"
    if complete_path.exists():
        threading.Thread(target=_play_audio_file, args=(complete_path,), daemon=True).start()

    return {
        "best_loopback": best_lb,
        "best_mic": best_mic,
        "loopback": [best_lb],
        "mic": mic_results,
    }
