"""Open the Meeting Assistant UI as a standalone app window instead of a browser
tab. Uses Chrome/Edge ``--app=`` mode when available (a chromeless window that
looks and behaves like a native app) and falls back to the default browser when
no Chromium-family browser is found, so the UI always opens either way.

Design goal: starting a meeting should open the app, not dump a new browser
tab. Every launch/meeting-start entry point routes through
``open_app_window`` so the experience is a window, not a tab.
"""
import os
import shutil
import subprocess
import sys

from core import log
from core import settings

# Roughly a comfortable default; the window is resizable and remembers its size
# per Chrome's app-window state.
_WINDOW_SIZE = "1360,900"


def _chromium_path() -> str | None:
    """Locate a Chromium-family browser (Chrome preferred, then Edge)."""
    names = ["chrome.exe", "msedge.exe"] if sys.platform == "win32" else [
        "google-chrome", "chrome", "chromium", "microsoft-edge", "msedge",
    ]
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    candidates: list[str] = []
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
            candidates.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _chrome_proxy_path() -> str | None:
    """Locate chrome_proxy.exe, the launcher Chrome uses for installed PWAs.

    It sits next to chrome.exe. Launching through it (rather than chrome.exe)
    is what makes the window dock under the existing taskbar pin with the app
    icon instead of spawning a second, generically-branded window.
    """
    if sys.platform != "win32":
        return None
    exe = _chromium_path()
    if exe and os.path.basename(exe).lower() == "chrome.exe":
        proxy = os.path.join(os.path.dirname(exe), "chrome_proxy.exe")
        if os.path.exists(proxy):
            return proxy
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if not base:
            continue
        proxy = os.path.join(base, "Google", "Chrome", "Application", "chrome_proxy.exe")
        if os.path.exists(proxy):
            return proxy
    return None


def pwa_is_installed(app_id: str) -> bool:
    """True when Chrome's Default profile actually has that app installed.

    Chrome creates ``<profile>/Web Applications/_crx_<app_id>`` when a PWA is
    installed and leaves it there. Without this check a ``--app-id`` launch for
    an app that is not installed still starts a process and still "succeeds",
    so the caller waits on a window that never appears. Checking the folder
    lets the caller fall straight back to an ``--app=<url>`` window.
    """
    if not app_id:
        return False
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return False
    return os.path.isdir(os.path.join(
        local, "Google", "Chrome", "User Data", "Default",
        "Web Applications", f"_crx_{app_id}"))


def _open_installed_pwa() -> bool:
    """Launch (or focus) the installed Meeting Assistant PWA. Never raises.

    Mirrors app_launcher.vbs, which is what the taskbar pin runs:
    ``chrome_proxy.exe --profile-directory=Default --app-id=<id>``. An
    ``--app-id`` launch cannot carry a URL, so it always lands on the
    manifest's start_url (/session); anything the app needs the window to DO
    arrives over SSE instead (see core/recording_request.py).

    Everything, settings read included, is inside the try: this is the fast
    path in front of a capture guarantee and it must never raise.
    """
    try:
        app_id = str(settings.get("pwa_app_id", "") or "").strip()
        if not app_id:
            return False
        proxy = _chrome_proxy_path()
        if not proxy:
            return False
        if not pwa_is_installed(app_id):
            log.info("app", f"PWA {app_id} is not installed in the Default profile; "
                            f"using an app window instead")
            return False
        subprocess.Popen(
            [proxy, "--profile-directory=Default", f"--app-id={app_id}"],
            close_fds=True,
        )
        return True
    except Exception as e:  # pragma: no cover - environment dependent
        log.warn("app", f"PWA launch failed ({e}); falling back to an app window")
        return False


def open_app_window(url: str, prefer_pwa: bool = False) -> bool:
    """Open ``url`` in a chromeless app window.

    With ``prefer_pwa`` the installed PWA is launched first, so the window
    docks under the taskbar pin the user already has instead of becoming a
    second app identity. That launch ignores ``url`` (Chrome allows no URL on
    an ``--app-id`` launch), so only pass ``prefer_pwa=True`` when the PWA's
    start page (/session) is an acceptable destination.

    Returns True when launched as an app window, False when it fell back to the
    default browser (the UI still opens either way). Never raises.
    """
    if prefer_pwa and _open_installed_pwa():
        return True

    exe = _chromium_path()
    if exe:
        try:
            args = [exe, f"--app={url}", f"--window-size={_WINDOW_SIZE}"]
            # Open in the user's normal signed-in profile (Chrome's "Default"),
            # the same profile the taskbar-pinned Meeting Assistant shortcut
            # uses, so the app window carries the custom icon and groups under
            # the pin instead of spawning under a throwaway profile. Chrome
            # ignores an unknown profile gracefully; Edge accepts the same flag.
            if sys.platform == "win32" and os.path.basename(exe).lower() in (
                    "chrome.exe", "msedge.exe"):
                args.insert(1, "--profile-directory=Default")
            subprocess.Popen(args, close_fds=True)
            return True
        except Exception as e:  # pragma: no cover - environment dependent
            log.warn("app", f"app-window launch failed ({e}); opening default browser")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception as e:  # pragma: no cover
        log.warn("app", f"browser open failed: {e}")
    return False
