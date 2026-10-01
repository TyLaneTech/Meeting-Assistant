"""Desktop notifications for Meeting Assistant.

Dispatch is by platform:
  - Windows: the app's own toast widget (ui_desktop/toast). Windows' toasts
             were dropped silently by Focus Assist during the very meetings
             the app records, needed an AppUserModelID to appear at all, and
             could not be styled, timed or taken down by the app.
  - macOS:   osascript (Notification Center; no action buttons).
  - Other:   no-op.

Every notification the app sends has a function here, so app.py never
composes one. Each carries a tag, which is what lets a later notification
about the same thing replace the earlier one, and lets the app take one down
the moment it stops applying: a "meeting detected" question goes when the
recording starts, "still in the meeting?" goes when it stops, and "call audio
not captured" goes when the audio comes back.
"""
from __future__ import annotations

import subprocess
import sys
from typing import Callable, Optional

from core import app_window
from core import log
from core import recording_request
from ui_desktop import toast
from ui_desktop.toast import Action

APP_DISPLAY_NAME = "Meeting Assistant"

# One tag per situation.
TAG_MEETING = "meeting"             # a detected meeting, asked about
TAG_RECORDING = "recording"         # a recording the app started by itself
TAG_QUIET = "quiet"                 # "still in the meeting?"
TAG_CAPTURE = "capture"             # call audio is not being captured
TAG_START_FAILED = "start-failed"   # an automatic start did not happen
TAG_TEST = "test"

_LEGACY_TIMEOUTS = {"short": 8.0, "long": 20.0}


# ── Public API ────────────────────────────────────────────────────────────────


def notify(
    title: str,
    body: str = "",
    *,
    kind: str = "info",
    icon: Optional[str] = None,
    tag: Optional[str] = None,
    timeout: Optional[float] = None,
    sound=None,
    on_click: Optional[Callable[[str], None]] = None,
    on_dismiss: Optional[Callable[[str], None]] = None,
    actions: Optional[list] = None,
    duration: Optional[str] = None,
    scenario: str = "",
    mac_url: Optional[str] = None,
) -> bool:
    """Show a notification.

    Parameters
    ----------
    title, body
        The headline and the message.
    kind
        "info" | "success" | "warning" | "error" | "prompt" | "recording". Sets
        the colour, the icon, how long it stays and the sound, each of which
        ``icon``, ``timeout`` and ``sound`` override. Errors and prompts stay
        until dealt with; ``timeout=0`` makes anything do that.
    tag
        A later notification with the same tag replaces this one, and
        ``dismiss(tag)`` takes it down.
    on_click
        Runs when the body is clicked, with "". The toast closes.
    on_dismiss
        Runs with why the toast went: clicked, action, closed, timeout,
        replaced, dismissed or shutdown.
    actions
        Up to three buttons: ``toast.Action`` objects, or dicts with
        ``label``, ``on_click``, ``arg`` (passed to on_click; the label when
        omitted), ``style`` ("primary" | "secondary" | "danger") and ``close``.
    duration, scenario
        The old vocabulary, still honoured: "long" stays 20 s, a scenario
        ("reminder", "alarm", ...) stays until dismissed.
    mac_url
        macOS only: appended to the body, since its notifications carry no
        buttons.

    Callbacks run on their own thread, never on the toast's UI thread.

    Returns True when the platform accepted the notification. On Windows that
    means it is on screen; nothing can swallow it.
    """
    if sys.platform == "win32":
        if timeout is None and (scenario or duration):
            timeout = 0.0 if scenario else _LEGACY_TIMEOUTS.get(duration, 8.0)
        handle = toast.show(title, body, kind=kind, icon=icon, actions=_actions(actions),
                            on_click=on_click, on_dismiss=on_dismiss, timeout=timeout,
                            tag=tag, sound=sound)
        if handle.reason == "unsupported":
            log.warn("notify", f"Notification not shown ({title}): the toast window is unavailable")
            return False
        return True
    if sys.platform == "darwin":
        return _send_macos_notification(title, body, url=mac_url)
    log.warn("notify", f"Notification skipped: unsupported platform {sys.platform}")
    return False


def _actions(actions: Optional[list]) -> list[Action]:
    out: list[Action] = []
    for spec in actions or []:
        if isinstance(spec, Action):
            out.append(spec)
            continue
        label = str(spec.get("label", "")).strip()
        if not label:
            continue
        out.append(Action(label, on_click=spec.get("on_click"), arg=str(spec.get("arg") or label),
                          style=str(spec.get("style") or "secondary"),
                          close=bool(spec.get("close", True))))
    return out


def dismiss(tag: str) -> None:
    """Take down the notification carrying ``tag``, if it is still up."""
    if sys.platform == "win32":
        toast.dismiss(tag)


def configure(*, is_recording: Optional[Callable[[], bool]] = None) -> None:
    """What the toast needs from the app: whether a recording is running, so
    sounds can be softer while they are being recorded."""
    if sys.platform == "win32":
        toast.configure(is_recording=is_recording)


def recording_started() -> None:
    """A recording is running, however it was started: the question about
    the meeting and any "not recording" alarm are answered."""
    dismiss(TAG_MEETING)
    dismiss(TAG_START_FAILED)


def recording_stopped() -> None:
    """Nothing is recording: the prompts and alarms about the one that was
    are moot."""
    dismiss(TAG_QUIET)
    dismiss(TAG_CAPTURE)
    dismiss(TAG_RECORDING)


def meeting_ended() -> None:
    """The detected meeting is gone, so the offer to record it is withdrawn."""
    dismiss(TAG_MEETING)


def capture_recovered() -> None:
    """Call audio is being captured again: the alarm comes down by itself."""
    dismiss(TAG_CAPTURE)


def preview_sound(motif: str = "ask", sound_set: Optional[str] = None,
                  volume: Optional[float] = None) -> bool:
    """Play one notification sound, for the Settings picker."""
    if sys.platform != "win32":
        return False
    return toast.play_sound(motif, sound_set, volume)


def sound_sets() -> list[dict]:
    return toast.sound_sets()


# ── The notifications the app sends ───────────────────────────────────────────


def _post_stop(stop_url: str) -> None:
    try:
        import urllib.request
        req = urllib.request.Request(
            stop_url, data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        log.warn("notify", f"Stop-from-notification failed: {e}")


def send_quiet_recording_toast(session_id: str, server_url: str) -> bool:
    """Ask whether a recording that has gone quiet should go on."""
    base = server_url.rstrip("/")
    session_url = f"{base}/session?id={session_id}&quiet_prompt=1"
    stop_url = f"{base}/api/recording/stop"

    def _open_session(_arg: str) -> None:
        # The window the user already has is raised and sent to the meeting,
        # so a notification never leaves a second one behind it.
        app_window.show(session_url, reason="toast:quiet")

    def _stop_recording(_arg: str) -> None:
        _post_stop(stop_url)
        app_window.show(session_url, reason="toast:quiet-stop")

    return notify(
        "Still in the meeting?",
        "Things have gone quiet. Stop the recording, or keep it going.",
        kind="prompt", icon="moon", tag=TAG_QUIET, timeout=30,
        on_click=_open_session,
        actions=[
            {"label": "Stop recording", "arg": "stop", "on_click": _stop_recording, "style": "danger"},
            {"label": "Keep recording", "arg": "keep"},
        ],
        mac_url=session_url,
    )


def send_meeting_detected_toast(app_name: str, server_url: str) -> bool:
    """Offer to record a just-detected Zoom/Teams meeting.

    "Start recording" hands the request to the start coordinator rather than
    POSTing /api/recording/start. Every recording is still kicked off by the
    session page (that is where device selection and the readiness gate live),
    but the coordinator offers it to the window that is already open first and
    only opens a new one if nothing takes it. See core/recording_request.py.

    The question stays up until it is answered, the recording starts some
    other way (recording_started), or the meeting ends (meeting_ended).
    """
    base = server_url.rstrip("/")
    # macOS has no notification buttons: clicking the notification just opens
    # a URL, so the autostart page stays the only affordance there.
    start_url = f"{base}/session?autostart=1"

    def _start(_arg: str) -> None:
        recording_request.request_start_async(
            "toast:detected", f"{app_name} meeting detected")

    return notify(
        f"{app_name} meeting detected",
        "Want to record and transcribe it?",
        kind="prompt", icon="video", tag=TAG_MEETING,
        on_click=_start,
        actions=[
            {"label": "Start recording", "arg": "start", "on_click": _start, "style": "primary"},
            {"label": "Not now", "arg": "dismiss"},
        ],
        mac_url=start_url,
    )


def send_meeting_autostarted_toast(app_name: str, server_url: str) -> bool:
    """Auto-start recording a just-detected meeting and confirm it.

    Mirrors ``send_meeting_detected_toast`` but, instead of asking, it requests
    the start immediately and then shows a confirmation. The request goes to the
    start coordinator, which offers it to the app window that is already open
    before opening anything (core/recording_request.py); the page still performs
    the start, exactly as a manual click would.

    Returns True once the start request has been dispatched, EVEN IF the
    notification itself failed. The caller (the meeting-detect loop) uses the
    return value to mark the meeting as handled and to arm auto-stop, so tying
    it to the notification would leave a recording running forever and re-fire
    the detection every two seconds. A failed notification is logged.
    """
    base = server_url.rstrip("/")
    session_url = f"{base}/session"
    stop_url = f"{base}/api/recording/stop"

    # Kick off the recording. Async: the coordinator escalates over tens of
    # seconds and this runs on the meeting-detect loop.
    recording_request.request_start_async(
        "toast:autostart", f"{app_name} meeting detected")
    dismiss(TAG_MEETING)

    def _open(_arg: str) -> None:
        # Raise the app window the user already has rather than opening a
        # second one, and leave it on whatever page it is showing: the
        # recording is already being requested, and a still-pending command
        # reaches this window over SSE the moment it connects.
        app_window.show(session_url, prefer_pwa=True, navigate=False,
                        reason="toast:autostarted")

    def _stop(_arg: str) -> None:
        _post_stop(stop_url)

    shown = notify(
        f"Recording {app_name} meeting",
        "Recording and transcription started by themselves. Click to open.",
        kind="recording", icon="microphone", tag=TAG_RECORDING, timeout=12,
        on_click=_open,
        actions=[
            {"label": "Open", "arg": "open", "on_click": _open, "style": "primary"},
            {"label": "Stop recording", "arg": "stop", "on_click": _stop, "style": "danger"},
        ],
        mac_url=session_url,
    )
    if not shown:
        log.warn("notify", f"Recording {app_name} meeting: start requested but the "
                           f"confirmation did not display")
    return True


def send_test_toast(server_url: Optional[str] = None) -> bool:
    """A notification to look at: the tray's Test Notification item and the
    Settings button both send it."""
    def _clicked(arg: str) -> None:
        log.info("notify", f"Test notification: {'button ' + arg if arg else 'body'} clicked")

    actions = [{"label": "Looks good", "arg": "ok", "on_click": _clicked, "style": "primary"}]
    if server_url:
        settings_url = f"{server_url.rstrip('/')}/session?settings=1&section=reminders"

        def _settings(_arg: str) -> None:
            app_window.show(settings_url, reason="toast:test")

        actions.append({"label": "Open settings", "arg": "settings", "on_click": _settings})

    return notify(
        "Notifications are working",
        "This is how Meeting Assistant gets your attention. Buttons work too.",
        kind="success", icon="bell", tag=TAG_TEST, timeout=20,
        on_click=_clicked,
        actions=actions,
    )


# ── macOS backend ─────────────────────────────────────────────────────────────


def _osascript_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _send_macos_notification(title: str, body: str, url: Optional[str] = None) -> bool:
    # osascript notifications cannot carry a click action, so when a caller has
    # a destination URL we surface it in the body: on macOS it is the only way
    # for the user to reach the session/page from the notification.
    if url:
        body = f"{body}\n{url}" if body else url
    script = (
        f'display notification "{_osascript_escape(body)}" '
        f'with title "{_osascript_escape(title)}" sound name "Pop"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warn("notify", f"osascript notification failed: {e}")
        return False
