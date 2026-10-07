"""Is the machine on the charger or on battery?

GPU work (the post-meeting transcription and reanalysis on CUDA) only runs on
charger power: on a hybrid-graphics laptop the NVIDIA card powering up for a
batch job costs a large share of the battery. Callers ask once per job, and a
job that is waiting for the charger re-asks at most once a minute; nothing
polls this in the background.

Test override, so the battery path can be verified without unplugging:
    MA_FORCE_POWER=battery|ac     (environment, read on every call)
    <data_dir>/force_power        (file holding "battery" or "ac"; lets a
                                   running app switch without a restart)
The environment wins over the file. Every answer an override produced is
logged, so a forgotten override cannot hide.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable

_VALID = ("battery", "ac")


def _override() -> str | None:
    env = (os.environ.get("MA_FORCE_POWER") or "").strip().lower()
    if env in _VALID:
        return env
    try:
        from core import paths
        p = paths.data_dir() / "force_power"
        if p.is_file():
            val = p.read_text(encoding="utf-8", errors="replace").strip().lower()
            if val in _VALID:
                return val
    except Exception:
        pass
    return None


def _windows_ac_line() -> bool | None:
    """GetSystemPowerStatus: ACLineStatus 0 = battery, 1 = charger, 255 =
    unknown. A machine with no system battery (BatteryFlag 128) is a desktop
    and always counts as charger power. None when the call fails."""
    import ctypes
    from ctypes import wintypes

    class _SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", wintypes.BYTE),
            ("BatteryFlag", wintypes.BYTE),
            ("BatteryLifePercent", wintypes.BYTE),
            ("SystemStatusFlag", wintypes.BYTE),
            ("BatteryLifeTime", wintypes.DWORD),
            ("BatteryFullLifeTime", wintypes.DWORD),
        ]

    status = _SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    ac = status.ACLineStatus & 0xFF
    flag = status.BatteryFlag & 0xFF
    if flag != 255 and flag & 128:
        return True
    if ac == 0:
        return False
    if ac == 1:
        return True
    return None


def on_ac_power() -> bool:
    """True on charger power (or when it cannot be told), False on battery."""
    forced = _override()
    if forced is not None:
        try:
            from core import log
            log.info("power", f"Power source forced to {forced} by the test override")
        except Exception:
            pass
        return forced == "ac"
    if sys.platform == "win32":
        try:
            ac = _windows_ac_line()
        except Exception:
            ac = None
        return True if ac is None else ac
    # Other platforms: the battery rule exists for the NVIDIA card on a
    # Windows laptop; elsewhere GPU work keeps running as before.
    return True


class ChargerGate:
    """Decides whether a queued GPU job may start, re-asking about power at
    most once every RECHECK_SEC while the job waits for the charger.

    check(plan_fn) calls plan_fn() (a device plan from
    core.compute_device.plan_batch_devices) and returns it when the job may
    start, or None while it has to wait. The owner sleeps between checks;
    nothing here runs on its own.
    """

    RECHECK_SEC = 60.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.waiting_since: float | None = None
        self._last_check = 0.0

    @property
    def waiting(self) -> bool:
        return self.waiting_since is not None

    def check(self, plan_fn: Callable[[], dict]) -> dict | None:
        now = self._clock()
        if self.waiting and now - self._last_check < self.RECHECK_SEC:
            return None
        self._last_check = now
        plan = plan_fn()
        from core import log
        if plan.get("wait_for_charger"):
            if not self.waiting:
                self.waiting_since = now
                log.info("reanalysis", "On battery: the post-meeting transcription runs on "
                                       "the GPU, so it waits for the charger (checked once "
                                       "a minute)")
            return None
        if self.waiting:
            waited = (now - self.waiting_since) / 60.0
            log.info("reanalysis", f"Charger connected: starting the post-meeting "
                                   f"transcription that waited {waited:.0f} min")
            self.waiting_since = None
        return plan
