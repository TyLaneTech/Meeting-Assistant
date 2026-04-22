"""Windows toast helpers for Meeting Assistant."""
from __future__ import annotations

import sys
from pathlib import Path

import log


_ICON = Path(__file__).parent / "static" / "images" / "logo_recording.ico"


def send_quiet_recording_toast(session_id: str, server_url: str) -> bool:
    """Show a Windows toast that routes back to the active recording session.

    Returns True when a toast API accepted the notification.  The helper is
    intentionally no-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        log.warn("notify", "Quiet recording toast skipped: Windows-only")
        return False

    url = f"{server_url.rstrip('/')}/session?id={session_id}&quiet_prompt=1"
    title = "Still in the meeting?"
    body = "Things have gone quiet. Click to stop the recording."

    # Preferred path: actionable Windows toast.  winotify's action button is
    # enough for the reminder flow even if body-click activation varies by OS.
    try:
        from winotify import Notification

        toast = Notification(
            app_id="Meeting Assistant",
            title=title,
            msg=body,
            icon=str(_ICON) if _ICON.exists() else "",
            duration="short",
            launch=url,
        )
        toast.add_actions(label="Stop recording", launch=url)
        toast.show()
        return True
    except Exception as e:
        log.warn("notify", f"winotify toast failed: {e}")

    log.warn("notify", "Quiet recording toast skipped: no working Windows toast backend")
    return False
