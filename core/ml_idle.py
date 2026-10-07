"""Idle clock for the ML stack (Whisper, diarizer, fingerprint embedder).

Pure policy, no model handles. app.py owns the loaders and unloaders and asks
this clock, once a minute, whether the models have sat unused long enough to
drop. Anything that uses the models stamps the clock with ``touch()``; the
sweep passes its busy flags in so a running capture, test, reanalysis or
summary keeps the clock reset instead of racing an unload.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class IdleClock:
    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._last_used = now()

    def touch(self) -> None:
        """Record that the models were just used (or are about to be)."""
        with self._lock:
            self._last_used = self._now()

    def idle_seconds(self) -> float:
        with self._lock:
            return max(0.0, self._now() - self._last_used)

    def idle_minutes(self) -> int:
        return int(round(self.idle_seconds() / 60))

    def unload_due(self, minutes: float, *, busy: bool, ready: bool, waking: bool) -> bool:
        """True when the models should be dropped on this tick.

        ``busy`` (a capture, test, reanalysis or summary in flight) stamps the
        clock and is never due, so the idle span always starts from the last
        minute anything was happening. ``minutes <= 0`` disables the sweep.
        Nothing is due while the models are not loaded or are mid-wake.
        """
        if busy:
            self.touch()
            return False
        if minutes <= 0 or not ready or waking:
            return False
        return self.idle_seconds() >= minutes * 60
