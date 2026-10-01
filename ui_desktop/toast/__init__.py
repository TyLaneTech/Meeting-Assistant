"""The app's own desktop notifications (Windows).

Windows' toasts were unreliable here: Focus Assist swallowed them during the
very meetings the app is for, the AppUserModelID dance decided whether they
appeared at all, and they offered nothing the app could shape. So the app
draws its own. A notification is a small always-on-top window painted by
``paint.py`` in the user's app theme, stacked and timed by ``manager.py``,
hosted by ``win32.py``, with a sound from ``sounds.py``. macOS keeps the
system notification (``ui_desktop/notifications.py``).

    from ui_desktop import toast
    t = toast.show("Zoom meeting detected", "Want to record and transcribe it?",
                   kind="prompt", icon="video", tag="meeting",
                   actions=[toast.Action("Start recording", on_click=start),
                            toast.Action("Not now")])
    ...
    toast.dismiss("meeting")          # the recording started some other way

Kinds (``spec.KIND_DEFAULTS``) decide the colour, the icon, how long it stays
and which sound it makes; every one of those can be given explicitly.
``tag`` lets a later toast replace an earlier one, and lets the app take a
toast down once it no longer applies.

Callbacks (``on_click``, an action's ``on_click``, ``on_dismiss``) run on
their own thread, never on the UI thread.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

from ui_desktop.toast import sounds
from ui_desktop.toast.manager import DISMISS_REASONS, POSITIONS, ToastHandle, ToastManager
from ui_desktop.toast.spec import ICONS, KIND_DEFAULTS, KINDS, Action, ToastSpec

__all__ = [
    "Action", "ToastSpec", "ToastHandle", "ICONS", "KINDS", "KIND_DEFAULTS", "POSITIONS",
    "DISMISS_REASONS", "show", "dismiss", "dismiss_all", "flush", "is_supported",
    "configure", "play_sound", "sound_sets", "sounds",
]

_manager: Optional[ToastManager] = None
_manager_lock = threading.Lock()


def _host_factory(manager: ToastManager):
    from ui_desktop.toast.win32 import WindowHost
    return WindowHost(manager)


def _get_manager() -> ToastManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ToastManager(_host_factory)
        return _manager


def is_supported() -> bool:
    """Whether a toast can be shown here at all (Windows, with a desktop)."""
    if sys.platform != "win32":
        return False
    return _get_manager().supported()


def configure(*, is_recording: Optional[Callable[[], bool]] = None) -> None:
    """Wire in what the app knows: ``is_recording`` softens sounds while a
    recording is running, since they land in the recording."""
    m = _get_manager()
    if is_recording is not None:
        m.is_recording = is_recording


def show(title: str, body: str = "", *, kind: str = "info", icon: Optional[str] = None,
         actions=None, on_click: Optional[Callable[[str], None]] = None,
         on_dismiss: Optional[Callable[[str], None]] = None,
         timeout: Optional[float] = None, tag: Optional[str] = None,
         sound=None) -> ToastHandle:
    """Show a notification. Returns at once; the handle's ``closed`` event
    is set when the toast is gone, with ``reason`` saying why.

    On a platform without the widget the handle comes back already closed
    with reason ``"unsupported"``; nothing raises.
    """
    spec = ToastSpec(title=title, body=body, kind=kind, icon=icon, actions=list(actions or []),
                     on_click=on_click, on_dismiss=on_dismiss, timeout=timeout, tag=tag,
                     sound=sound)
    if sys.platform != "win32":
        h = ToastHandle(0, _get_manager())
        h._finish("unsupported")
        return h
    return _get_manager().show(spec)


def dismiss(tag_or_id, reason: str = "dismissed") -> None:
    """Take down every toast carrying ``tag`` (or the one with this id)."""
    m = _manager
    if m is not None:
        m.dismiss(tag_or_id, reason)


def dismiss_all(reason: str = "dismissed") -> None:
    m = _manager
    if m is not None:
        m.dismiss_all(reason)


def flush(timeout: Optional[float] = None) -> bool:
    """Wait until no toast is on screen; True if that happened in time."""
    m = _manager
    return True if m is None else m.flush(timeout)


def play_sound(motif: str, sound_set: Optional[str] = None, volume: Optional[float] = None) -> bool:
    """Play one motif, for a preview in Settings."""
    if sys.platform != "win32":
        return False
    return _get_manager().play_preview(motif, sound_set, volume)


def sound_sets() -> list[dict]:
    """The sound sets, for the Settings picker."""
    return [{"id": sid, **meta} for sid, meta in sounds.SETS.items()]
