"""Detect when a Zoom or Teams meeting is actively in progress on this PC.

The goal is to answer one question reliably: "is the user in a live call
right now?" so the app can offer to record it. Process existence is useless
here (Zoom and Teams typically run all day), so we lean on two signals:

  1. MICROPHONE IN USE by a known meeting app (primary, app-agnostic).
     A live meeting holds the microphone capture device open for its entire
     duration, even when the user is muted: app-level mute is a software gain
     mute and does NOT release the device. This is the same fact that keeps
     the Windows taskbar "microphone in use" indicator lit through a whole
     muted meeting. We read it from the CapabilityAccessManager ConsentStore,
     the registry the OS itself uses to drive that indicator. Currently-in-use
     is encoded as LastUsedTimeStart > 0 with LastUsedTimeStop == 0.
     This signal survives muting and survives the Teams calling stack moving
     into the ms-teams_modulehost.exe child process, because packaged-app
     usage is attributed to the package family name (MSTeams_8wekyb3d8bbwe),
     not the child exe.

  2. ZOOM MEETING WINDOW present (corroborating, Zoom-specific).
     A joined Zoom meeting creates a top-level window of class
     ZPContentViewWndClass (distinct from the main app window
     ZPPTMainFrmWndClassEx and the connecting window zWaitHostWndClass).
     This catches the rare case of a Zoom meeting joined without computer
     audio, where signal 1 would not fire. New Teams is WebView2/Chromium,
     so its meeting windows carry generic classes and are not reliably
     classifiable; for Teams we rely on signal 1.

detect_active_meeting() returns the OR of these signals. Everything is a safe
no-op on non-Windows platforms.
"""
from __future__ import annotations

import sys

from core import log as log

_IS_WIN = sys.platform == "win32"

# Registry path (relative; valid under both HKCU and HKLM, case-insensitive).
_MIC_BASE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
_MAX_DEPTH = 4  # ConsentStore nests at most a couple levels (NonPackaged, packaged AppIDs)

# Zoom's joined-meeting window class.
_ZOOM_MEETING_CLASS = "ZPContentViewWndClass"

if _IS_WIN:
    import ctypes
    import winreg
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = ctypes.c_bool


# ── Microphone-in-use detection (CapabilityAccessManager ConsentStore) ────────

def _read_qword(key, name):
    """Read a REG_QWORD value as an int, or None if absent/unreadable."""
    try:
        val, _typ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, (bytes, bytearray)):
        try:
            return int.from_bytes(bytes(val), "little")
        except ValueError:
            return None
    return None


def _collect_usage(key, name, depth, out):
    """Recursively gather (identifier, start, stop) for keys that record usage.

    ``identifier`` is the accumulated subkey path under the microphone store
    (for NonPackaged win32 apps the leaf is the exe path with '\\' encoded as
    '#'; for packaged apps it is the package/app id). We match meeting apps by
    substring against this identifier, so the exact nesting does not matter.
    """
    start = _read_qword(key, "LastUsedTimeStart")
    stop = _read_qword(key, "LastUsedTimeStop")
    if start is not None or stop is not None:
        out.append((name, start or 0, stop))
    if depth >= _MAX_DEPTH:
        return
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(key, i)
        except OSError:
            break
        i += 1
        try:
            with winreg.OpenKey(key, sub, 0, winreg.KEY_READ) as subkey:
                child = (name + "\\" + sub) if name else sub
                _collect_usage(subkey, child, depth + 1, out)
        except OSError:
            continue


def _mic_in_use_by_meeting_app():
    """Return "Zoom" / "Microsoft Teams" if one currently holds the mic, else None."""
    if not _IS_WIN:
        return None
    entries: list = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, _MIC_BASE, 0, winreg.KEY_READ) as root:
                _collect_usage(root, "", 0, entries)
        except OSError:
            continue

    for identifier, start, stop in entries:
        # In use right now: started at a real time and not yet stopped.
        if not (start and stop == 0):
            continue
        ident = identifier.replace("#", "\\").lower()
        if "zoom" in ident:
            return "Zoom"
        if "teams" in ident:
            return "Microsoft Teams"
    return None


# ── Zoom meeting-window detection (EnumWindows + class name) ───────────────────

def _zoom_meeting_window_present():
    """True if a visible top-level window of class ZPContentViewWndClass exists."""
    if not _IS_WIN:
        return False
    found = [False]
    buf = ctypes.create_unicode_buffer(256)

    def _cb(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            if _user32.GetClassNameW(hwnd, buf, 256) and buf.value == _ZOOM_MEETING_CLASS:
                found[0] = True
                return False  # stop enumeration
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    except Exception as e:
        log.warn("meetdetect", f"EnumWindows failed: {e}")
        return False
    return found[0]


# ── Public API ────────────────────────────────────────────────────────────────

def detect_active_meeting():
    """Return {"app": str, "signal": "microphone"|"window"} or None.

    Logic (the user-chosen high-recall mode): a meeting is active if a known
    meeting app holds the microphone OR a Zoom meeting window is present.
    """
    if not _IS_WIN:
        return None
    try:
        app = _mic_in_use_by_meeting_app()
    except Exception as e:
        log.warn("meetdetect", f"mic scan failed: {e}")
        app = None
    if app:
        return {"app": app, "signal": "microphone"}
    try:
        if _zoom_meeting_window_present():
            return {"app": "Zoom", "signal": "window"}
    except Exception as e:
        log.warn("meetdetect", f"window scan failed: {e}")
    return None


def is_meeting_active() -> bool:
    """Convenience boolean wrapper around detect_active_meeting()."""
    return detect_active_meeting() is not None
