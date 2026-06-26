"""
macOS audio bootstrap (retired no-op shim).

System audio on macOS is now captured directly via Apple's ScreenCaptureKit
(see capture_audio/mac.py). SCK needs no virtual audio driver, no CoreAudio
aggregate device, and no system-output reroute, so the old BlackHole install
and recording-time routing machinery this module used to provide is gone.

The module is kept only as a safety shim: nothing in the app imports it after
the ScreenCaptureKit migration, but any stale import (e.g. an old script) gets
harmless no-ops instead of an ImportError. The full BlackHole implementation
remains in git history if it is ever needed again.
"""
from __future__ import annotations

from core import log as log

_RETIRED = (
    "capture_audio.mac_bootstrap is retired: macOS system audio now uses "
    "ScreenCaptureKit, which needs no BlackHole driver or aggregate device."
)


def install_blackhole() -> bool:
    """Retired. ScreenCaptureKit needs no virtual audio driver."""
    log.info("audio", _RETIRED)
    return False


def ensure_aggregate_device(prev_default_name=None) -> bool:
    """Retired. ScreenCaptureKit needs no CoreAudio aggregate device."""
    return False


def prepare_recording_routing() -> dict:
    """Retired. ScreenCaptureKit needs no system-output reroute."""
    return {"ok": False, "prev_default_id": None, "message": _RETIRED}


def restore_recording_routing(prev_default_id=None) -> bool:
    """Retired no-op counterpart to prepare_recording_routing()."""
    return True


def bootstrap_first_launch() -> dict:
    """Retired. Returns an inert status dict for any legacy caller."""
    return {
        "installed": False,
        "aggregate_ready": False,
        "messages": [_RETIRED],
    }
