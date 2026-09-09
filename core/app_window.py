"""One app window: show the one that is open, and only open one when there is none.

Every click that means "show me the app" comes through here: the tray icon and
its menu, a toast, and the Start Menu shortcut. Before this, each of them called
core/browser.py directly and Chromium made a new window every time, so a day of
clicking toasts left the user with a row of identical windows.

What a click does now:

  1. Raise the window that is already open (core/window_focus.py finds it).
  2. If the click had a destination (Settings, a particular meeting), send that
     window there over SSE, the same way a "start recording" request is offered
     to an open window rather than opening one. The page's own router handles
     it, so a destination behaves exactly as it does when it arrives in the URL.
  3. Only when there is no window to raise does anything get opened, and that is
     the path that shipped before this module.

A destination needs a window that is listening, not just one on screen, so a
click that carries one falls back to opening a window unless a client is
connected. ``configure`` is what wires those two things in from app.py; until it
runs, every call opens a window exactly as it always did.

The destination goes to every listening window, not only the raised one: the
server knows which window it raised by its handle, and nothing ties a handle to
an SSE client. In the one-window world this keeps, that is the same thing. A
user who still has several open sees them all follow, which fades as they close
them.
"""
from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlsplit

from core import browser
from core import log
from core import window_focus

_push: Optional[Callable[[str, dict], None]] = None
_client_count: Optional[Callable[[], int]] = None


def configure(*, push: Callable[[str, dict], None],
              client_count: Callable[[], int]) -> None:
    """Wire in the app's SSE push and its count of listening windows."""
    global _push, _client_count
    _push, _client_count = push, client_count


def route_of(url: str) -> str:
    """The path and query of a URL, which is all the page's router needs."""
    parts = urlsplit(url)
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def show(url: str, *, prefer_pwa: bool = False, navigate: bool = True,
         reason: str = "") -> str:
    """Show the app at ``url``. Returns "focused", "opened" or "browser".

    ``navigate=False`` means the click had no destination of its own ("open the
    app"), so a window that is already open is raised where it stands. Sending
    it home would take a user watching a recording away from it.
    """
    if _can_focus(navigate) and window_focus.focus_app_window():
        if navigate:
            _tell_window(url)
        log.info("app", f"Raised the open app window{f' ({reason})' if reason else ''}")
        return "focused"
    opened = browser.open_app_window(url, prefer_pwa=prefer_pwa)
    return "opened" if opened else "browser"


def _can_focus(navigate: bool) -> bool:
    """Whether raising a window is worth trying at all.

    Unconfigured, the answer is always no: this module is only in charge inside
    the running app, and a unit test that imports the tray must never reach out
    and grab the user's desktop.
    """
    if _client_count is None:
        return False
    if not navigate:
        return True
    return _client_count() > 0


def _tell_window(url: str) -> None:
    """Send the open window to ``url``. Its router does the rest."""
    if _push is None:
        return
    try:
        _push("navigate", {"url": route_of(url)})
    except Exception as e:  # pragma: no cover - a push failure is not a click failure
        log.warn("app", f"Could not send the open window to {url}: {e}")
