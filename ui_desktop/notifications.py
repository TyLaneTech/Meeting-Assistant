"""System toast helpers for Meeting Assistant.

Backend dispatch is automatic via sys.platform:
  - Windows: winotify (actionable toast with click-through URL)
  - macOS:   osascript (Notification Center; no action buttons, but URL is
             logged for the user to copy)
  - Other:   no-op
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core import log as log


_ICON = Path(__file__).parent.parent / "ui_web" / "static" / "images" / "logo_recording.ico"


def _osascript_escape(s: str) -> str:
    # AppleScript double-quoted strings: backslash-escape " and \ only.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_macos_notification(title: str, body: str, url: str) -> bool:
    # AppleScript notifications don't carry click actions; we still surface the
    # URL in the body so the user can act on it from Notification Center.
    body_with_url = f"{body}\n{url}"
    script = (
        f'display notification "{_osascript_escape(body_with_url)}" '
        f'with title "{_osascript_escape(title)}" sound name "Pop"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warn("notify", f"osascript notification failed: {e}")
        return False


def _send_windows_toast(title: str, body: str, url: str) -> bool:
    try:
        from winotify import Notification  # type: ignore[import-not-found]

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
        return False


def send_quiet_recording_toast(session_id: str, server_url: str) -> bool:
    """Show a system toast that routes back to the active recording session.

    Returns True when the platform notification API accepted the notification.
    """
    url = f"{server_url.rstrip('/')}/session?id={session_id}&quiet_prompt=1"
    title = "Still in the meeting?"
    body = "Things have gone quiet. Click to stop the recording."

    if sys.platform == "win32":
        if _send_windows_toast(title, body, url):
            return True
    elif sys.platform == "darwin":
        if _send_macos_notification(title, body, url):
            return True
    else:
        log.warn("notify", f"Quiet recording toast skipped: unsupported platform {sys.platform}")
        return False

    log.warn("notify", "Quiet recording toast skipped: no working toast backend")
    return False
