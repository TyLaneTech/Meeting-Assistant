"""Shared WebRTC microphone processor: acoustic echo cancellation (AEC) and/or
noise suppression (NS).

Both the macOS (ScreenCaptureKit) and Windows (WASAPI) capture mixers feed their
raw microphone through this one helper so the two platforms behave identically.

Design notes that matter:

* WebRTC's own automatic gain control is **never** enabled. The echo canceller
  only partially removes the acoustic echo, and desktop audio is usually speech,
  so any gain stage (WebRTC's or our custom AGC) re-boosts the speech-like echo
  residual via its VAD and the bleed comes back. The mic is kept clean and the
  custom AGC is bypassed by the caller while echo cancellation is on.

* AEC and NS are independent and can each be enabled on their own. AEC needs the
  loopback reference (the reverse stream) fed in lock-step with the mic; NS works
  on the mic alone. Run the processor on the **raw** mic, before any auto-gain:
  AGC's time-varying gain destroys the linear echo relationship the canceller
  relies on, and boosting before suppression would amplify the noise floor.

* The processor is (re)built automatically whenever the (aec, ns) configuration
  changes, so toggling either setting mid-recording takes effect cleanly.

* If the native ``aec_audio_processing`` module is unavailable, ``process`` is a
  transparent pass-through (returns the mic unchanged), so capture never breaks.
"""

from __future__ import annotations

import traceback

import numpy as np

from core import log as log


class WebRTCMicProcessor:
    """Stateful AEC/NS mic processor with internal frame buffering.

    Call :meth:`process` once per mixer tick with the raw mic chunk and the
    concurrent loopback chunk (zeros when no desktop audio that tick). It returns
    a processed chunk of ``chunk_size`` samples, or the input unchanged while the
    feature is disabled or the first frame is still buffering.
    """

    def __init__(self, chunk_size: int):
        self.chunk_size = int(chunk_size)
        self._proc = None
        self._frame = 0
        self._cfg: tuple[bool, bool] | None = None   # (enable_aec, enable_ns)
        self._mic_buf = np.array([], dtype=np.float32)
        self._lb_buf  = np.array([], dtype=np.float32)
        self._out_buf = np.array([], dtype=np.float32)

    @property
    def active(self) -> bool:
        return self._proc is not None

    def reset(self) -> None:
        """Drop the processor and all buffered audio (e.g. both features off)."""
        self._proc = None
        self._frame = 0
        self._cfg = None
        self._mic_buf = np.array([], dtype=np.float32)
        self._lb_buf  = np.array([], dtype=np.float32)
        self._out_buf = np.array([], dtype=np.float32)

    def _build(self, cfg: tuple[bool, bool], sample_rate: int) -> bool:
        enable_aec, enable_ns = cfg
        try:
            from aec_audio_processing import AudioProcessor
            self._proc = AudioProcessor(
                enable_aec=enable_aec, enable_ns=enable_ns, enable_agc=False,
            )
            sr = int(sample_rate or 16000)
            self._proc.set_stream_format(sr, 1)
            self._proc.set_reverse_stream_format(sr, 1)
            self._frame = self._proc.get_frame_size()
            self._cfg = cfg
            self._mic_buf = np.array([], dtype=np.float32)
            self._lb_buf  = np.array([], dtype=np.float32)
            self._out_buf = np.array([], dtype=np.float32)
            log.info("audio",
                     f"WebRTC mic processor: aec={enable_aec} ns={enable_ns} "
                     f"@ {sr} Hz, frame={self._frame} samples")
            return True
        except Exception:
            traceback.print_exc()
            self._proc = None
            self._cfg = None
            self._frame = 0
            return False

    def process(self, mic_chunk: np.ndarray, lb_chunk: np.ndarray,
                sample_rate: int, enable_aec: bool, enable_ns: bool) -> np.ndarray:
        """Process one mic chunk. ``enable_aec`` / ``enable_ns`` are the desired
        settings (stable across ticks); ``lb_chunk`` is the loopback reference and
        must be passed every tick when AEC is on (zeros when no desktop audio).
        Returns the processed mic chunk, or ``mic_chunk`` unchanged when both
        features are off, the native module is missing, or the first frame has not
        finished buffering yet."""
        enable_aec = bool(enable_aec)
        enable_ns  = bool(enable_ns)
        if not (enable_aec or enable_ns):
            if self._proc is not None:
                self.reset()
            return mic_chunk

        desired = (enable_aec, enable_ns)
        if self._proc is None or self._cfg != desired:
            if not self._build(desired, sample_rate):
                return mic_chunk

        self._mic_buf = np.concatenate((self._mic_buf, mic_chunk))
        if enable_aec:
            # Keep the reference in lock-step with the mic (zeros when silent).
            self._lb_buf = np.concatenate((self._lb_buf, lb_chunk))

        cleaned: list[np.ndarray] = []
        while len(self._mic_buf) >= self._frame and (
                not enable_aec or len(self._lb_buf) >= self._frame):
            mf = self._mic_buf[:self._frame]
            self._mic_buf = self._mic_buf[self._frame:]
            if enable_aec:
                lf = self._lb_buf[:self._frame]
                self._lb_buf = self._lb_buf[self._frame:]
                self._proc.process_reverse_stream(
                    (lf * 32767).astype(np.int16).tobytes())
            result = self._proc.process_stream(
                (mf * 32767).astype(np.int16).tobytes())
            cleaned.append(
                np.frombuffer(result, dtype=np.int16).astype(np.float32) / 32768.0)

        if cleaned:
            self._out_buf = np.concatenate((self._out_buf, *cleaned))
        if len(self._out_buf) >= self.chunk_size:
            out = self._out_buf[:self.chunk_size]
            self._out_buf = self._out_buf[self.chunk_size:]
            return out
        # Not enough processed audio yet (one-frame warm-up): pass the raw mic.
        return mic_chunk
