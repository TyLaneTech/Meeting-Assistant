"""Raise the app window the user already has, instead of opening a second one.

Clicking a toast or the tray icon should show the window that is open, not add
another one next to it. Chromium gives us no way to ask for that: ``--app=<url>``
always makes a new window, and only an installed PWA (``--app-id``) focuses the
window it already has. So for every other case we find the window ourselves and
raise it through the window manager.

Finding it. A Chromium ``--app`` window's caption IS the document title, and
every page in the app ends its title with "Meeting Assistant" (applyTitle in
ui_web/static/app.js). An ordinary browser window appends the browser's own name
to that ("... - Google Chrome"), and an Electron editor appends its own, so
matching the END of the caption picks out the app window and leaves browser
tabs, editors and folder windows alone. The owning process has to be a browser
as well, which is what stops an editor sitting in a folder of this name from
being mistaken for the app.

Everything here is best effort and never raises. A False return means the caller
should open a window the way it always has.
"""
from __future__ import annotations

import subprocess
import sys

from core import log

# Every page title ends with this, so it is the end of a Chromium app window's
# caption too.
TITLE_SUFFIX = "Meeting Assistant"

# Chromium's top-level window class, shared by Chrome, Edge and the rest of the
# family for both ordinary windows and --app / PWA windows. Electron apps use it
# too, which is why the process name is checked as well.
_CHROMIUM_CLASS_PREFIX = "Chrome_WidgetWin"

_BROWSER_EXES = frozenset({
    "chrome.exe", "msedge.exe", "brave.exe", "chromium.exe",
    "vivaldi.exe", "opera.exe", "thorium.exe",
})

_SW_RESTORE = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def window_matches(title: str, class_name: str, exe: str,
                   *, suffix: str = TITLE_SUFFIX) -> bool:
    """True when a top-level window is one of our own app windows.

    All three have to agree, because each one alone has a false positive: the
    caption matches a browser tab showing the app, the class matches every
    Electron app on the machine, and the process matches every browser window
    the user has open.
    """
    if not title or not title.strip().endswith(suffix):
        return False
    if not class_name.startswith(_CHROMIUM_CLASS_PREFIX):
        return False
    return exe.rsplit("\\", 1)[-1].rsplit("/", 1)[-1].lower() in _BROWSER_EXES


def focus_app_window(*, suffix: str = TITLE_SUFFIX) -> bool:
    """Bring an already-open app window to the front. False when there is none."""
    try:
        if sys.platform == "win32":
            return _focus_windows(suffix)
        if sys.platform == "darwin":
            return _focus_macos(suffix)
    except Exception as e:  # pragma: no cover - environment dependent
        log.warn("app", f"Could not raise the open app window ({e}); opening one")
    return False


# ── Windows ───────────────────────────────────────────────────────────────────

def _win32():
    """user32 and kernel32 with the signatures this module uses.

    Declaring argtypes matters on 64-bit: a window handle is a pointer, and
    ctypes truncates it to 32 bits without them, so every call would be made
    against a handle that is not the window we found.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL

    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    return ctypes, wintypes, user32, kernel32


def _process_image(kernel32, ctypes, wintypes, pid: int) -> str:
    """The full path of a process, or "" when it cannot be read."""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def app_windows(suffix: str = TITLE_SUFFIX) -> list[int]:
    """Handles of every open app window, front-most first."""
    if sys.platform != "win32":
        return []
    ctypes, wintypes, user32, kernel32 = _win32()
    found: list[int] = []

    # EnumWindows walks top-level windows in Z order, so the first match is the
    # one nearest the front: the window the user last looked at.
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        if not title.value:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _process_image(kernel32, ctypes, wintypes, pid.value) if pid.value else ""
        if window_matches(title.value, cls.value, exe, suffix=suffix):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(_visit, 0)
    return found


def _focus_windows(suffix: str) -> bool:
    ctypes, wintypes, user32, kernel32 = _win32()
    windows = app_windows(suffix)
    if not windows:
        return False
    hwnd = windows[0]

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True

    # Windows only lets the foreground process hand the foreground away. Sharing
    # an input queue with whoever holds it is the documented way around that,
    # and it is what makes the raise work when the click came from a toast the
    # user opened over another app.
    ours = kernel32.GetCurrentThreadId()
    front = user32.GetForegroundWindow()
    theirs = user32.GetWindowThreadProcessId(front, None) if front else 0
    attached = bool(theirs and theirs != ours
                    and user32.AttachThreadInput(ours, theirs, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)
    return user32.GetForegroundWindow() == hwnd


# ── macOS ─────────────────────────────────────────────────────────────────────

_MAC_SCRIPT = """
if application "{app}" is running then
  tell application "{app}"
    repeat with w in windows
      if title of w ends with "{suffix}" then
        set index of w to 1
        activate
        return "focused"
      end if
    end repeat
  end tell
end if
return "none"
"""


def _focus_macos(suffix: str) -> bool:
    """Raise the Chrome window showing the app. Best effort, bounded by a timeout.

    The "is running" guard matters: telling an application that is not running
    launches it, and a click that silently started Chrome would be worse than
    opening the window we were going to open anyway.
    """
    for app in ("Google Chrome", "Microsoft Edge"):
        script = _MAC_SCRIPT.format(app=app, suffix=suffix)
        try:
            done = subprocess.run(["osascript", "-e", script],
                                  capture_output=True, text=True, timeout=3)
        except Exception:
            continue
        if "focused" in (done.stdout or ""):
            return True
    return False
