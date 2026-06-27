"""System tray icon for Meeting Assistant.

Cross-platform: Windows notification area, macOS menu bar, Linux system tray.
Backend (pystray) is platform-agnostic; we adapt the icon styling per OS so
it reads correctly against macOS's dark/light menu bar.

Requires: pystray, Pillow.  If not installed the app runs without a tray.
"""
from __future__ import annotations

import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw, ImageOps

    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

from core import config as config

# The menu-bar icon is shown in colour on every platform so macOS matches the
# Windows tray: the brand green mic when ready, a red mic while recording, a grey
# mic while models load, and amber when setup (an API key) is required. On macOS
# we deliberately do NOT mark the NSImage as a template (that would force a
# monochrome silhouette); we only size it to the menu-bar point size after
# pystray creates the NSStatusItem (see _size_status_image).
_IS_MACOS = sys.platform == "darwin"

# ── Icon loading ───────────────────────────────────────────────────────────────
_IMAGES_DIR = Path(__file__).parent.parent / "ui_web" / "static" / "images"
_TRAY_SIZE  = 64
_icons: dict[str, "Image.Image"] = {}   # populated lazily by _ensure_icons()


def _tint(img: "Image.Image", color: tuple[int, int, int]) -> "Image.Image":
    """Return a tinted copy of *img* using the given RGB colour, preserving alpha."""
    alpha   = img.split()[3]
    gray    = ImageOps.grayscale(img)
    colored = ImageOps.colorize(gray, black=(0, 0, 0), white=color).convert("RGBA")
    colored.putalpha(alpha)
    return colored


def _ensure_icons() -> None:
    """Load PNG assets and derive tray variants on first call."""
    if _icons:
        return
    try:
        def _load(name: str) -> "Image.Image":
            return (
                Image.open(_IMAGES_DIR / name)
                .convert("RGBA")
                .resize((_TRAY_SIZE, _TRAY_SIZE), Image.LANCZOS)
            )

        idle      = _load("logo.png")
        recording = _load("logo_recording.png")

        # Same colour scheme on every platform (macOS included, per the note
        # above): green when ready, red while recording, grey while loading,
        # amber when setup is required.
        _icons["ready"]     = idle                               # brand green
        _icons["recording"] = recording                          # red
        _icons["loading"]   = _tint(idle, (110, 118, 129))       # gray
        _icons["setup"]     = _tint(idle, (210, 153, 34))        # amber
    except Exception as e:
        print(f"[tray] Could not load PNG icons, falling back to drawn icons: {e}")


def _create_fallback_icon(color: tuple[int, int, int], size: int = 64) -> "Image.Image":
    """Programmatically draw a simple mic icon - used only if PNG assets are missing."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    m   = 2
    d.ellipse([m, m, size - m, size - m], fill=color)
    cw, ch = int(size * 0.26), int(size * 0.34)
    cx, cy = (size - cw) // 2, int(size * 0.16)
    d.rounded_rectangle([cx, cy, cx + cw, cy + ch], radius=cw // 2, fill="white")
    aw = int(cw + size * 0.14)
    ax = (size - aw) // 2
    ay = cy + ch - int(size * 0.08)
    lw = max(2, size // 22)
    d.arc([ax, ay, ax + aw, ay + int(size * 0.22)], start=0, end=180, fill="white", width=lw)
    mid = size // 2
    lt  = ay + int(size * 0.11)
    lb  = lt + int(size * 0.10)
    d.line([(mid, lt), (mid, lb)], fill="white", width=lw)
    d.line([(mid - int(size * 0.10), lb), (mid + int(size * 0.10), lb)], fill="white", width=lw)
    return img


class MeetingTray:
    """Manages the system tray icon and its context menu.

    Parameters
    ----------
    server_url : str
        e.g. "http://127.0.0.1:6969"
    state_getter : callable
        Returns a dict snapshot of app state (called under the app's state lock).
    on_quit : callable
        Called when the user clicks Quit.  Receives the pystray Icon as argument.
    """

    def __init__(
        self,
        server_url: str,
        state_getter: Callable[[], dict],
        on_quit: Callable[["pystray.Icon"], None],
    ) -> None:
        self._url = server_url
        self._get_state = state_getter
        self._on_quit = on_quit
        self._icon: pystray.Icon | None = None

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the tray icon.  Blocks the calling thread (must be main)."""
        if _IS_MACOS:
            self._make_accessory_app()
        self._icon = pystray.Icon(
            name="Meeting Assistant",
            icon=self._pick_icon(),
            title=self._pick_tooltip(),
            menu=self._build_menu(),
        )
        self._icon.run(setup=self._on_setup)

    @staticmethod
    def _make_accessory_app() -> None:
        """Run as a menu-bar-only (accessory) app on macOS: no Dock icon and no
        Cmd-Tab entry, with the status-bar icon as the app's entry point. Must
        run on the main thread before the NSApplication loop starts. Best-effort:
        if AppKit is unavailable the app simply keeps its default Dock icon.
        """
        try:
            from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
            NSApplication.sharedApplication().setActivationPolicy_(
                NSApplicationActivationPolicyAccessory
            )
        except Exception:
            pass

    def refresh(self) -> None:
        """Update the icon image and tooltip to reflect current state. Thread-safe."""
        if self._icon is None:
            return
        # AppKit objects (NSImage, NSStatusItem, NSMenu) may only be touched
        # from the main thread; refresh() is called from Flask request threads
        # and the state-push loop, so on macOS hop onto the main run loop. On
        # Windows/Linux this runs inline (identical to a direct call).
        self._call_on_ui_thread(self._refresh_ui)

    def _refresh_ui(self) -> None:
        try:
            self._icon.icon = self._pick_icon()
            self._icon.title = self._pick_tooltip()
            self._icon.update_menu()
            # Re-apply icon sizing: pystray rebuilds the NSImage whenever .icon
            # is reassigned, so the menu-bar size is lost on each refresh.
            if _IS_MACOS:
                self._size_status_image(self._icon)
        except Exception:
            pass

    @staticmethod
    def _call_on_ui_thread(fn: Callable[[], None]) -> None:
        """Run *fn* on the AppKit main thread (macOS) or inline elsewhere.

        Best-effort: if the Foundation bridge is unavailable the call runs
        inline, which matches the pre-macOS behavior. On Windows/Linux
        _IS_MACOS is False, so this is always a plain inline call.
        """
        if _IS_MACOS and threading.current_thread() is not threading.main_thread():
            try:
                from Foundation import NSOperationQueue
                NSOperationQueue.mainQueue().addOperationWithBlock_(fn)
                return
            except Exception:
                pass
        try:
            fn()
        except Exception:
            pass

    def stop(self) -> None:
        """Remove the tray icon and unblock run()."""
        if self._icon:
            self._call_on_ui_thread(self._stop_ui)

    def _stop_ui(self) -> None:
        try:
            self._icon.stop()
        except Exception:
            pass

    # ── Private ───────────────────────────────────────────────────────────────

    def _on_setup(self, icon: "pystray.Icon") -> None:
        """Called once after the icon enters its event loop.

        pystray invokes setup on a helper thread, so the AppKit touches are
        marshaled to the main thread like every other mutation.
        """
        def _apply() -> None:
            icon.visible = True
            if _IS_MACOS:
                self._size_status_image(icon)
        self._call_on_ui_thread(_apply)

    @staticmethod
    def _size_status_image(icon: "pystray.Icon") -> None:
        """Size the menu-bar NSImage to match the system icons (macOS only).

        pystray hands AppKit the full 64px bitmap, which it renders at the full
        bar height with no padding, so the glyph reads noticeably larger than its
        neighbours; we pin it to the bar thickness minus a little padding. We also
        clear the template flag so the icon keeps its colour (green / red / grey /
        amber) instead of being flattened to a monochrome silhouette.

        pystray exposes the underlying NSStatusItem via internal attrs; we walk
        it defensively and re-apply on every refresh (pystray rebuilds the
        NSImage whenever .icon is reassigned, which drops these touches)."""
        try:
            status_item = getattr(icon, "_status_item", None) or getattr(icon, "_status_bar_item", None)
            if status_item is None:
                return
            ns_button = status_item.button() if callable(getattr(status_item, "button", None)) else None
            ns_image = ns_button.image() if ns_button is not None else None
            if ns_image is None:
                return
            if hasattr(ns_image, "setTemplate_"):
                ns_image.setTemplate_(False)  # keep colour; do not flatten to a silhouette
            try:
                from AppKit import NSStatusBar
                thickness = float(NSStatusBar.systemStatusBar().thickness()) or 24.0
            except Exception:
                thickness = 24.0
            side = max(15.0, min(19.0, round(thickness - 6.0)))
            if hasattr(ns_image, "setSize_"):
                ns_image.setSize_((side, side))
        except Exception:
            pass  # styling polish only, not worth crashing the tray over

    def _get_tray_state(self) -> str:
        """Return the current tray state key."""
        st = self._get_state()
        provider = st.get("ai_provider", "anthropic")
        if config.needs_setup(provider):
            return "setup"
        if st.get("is_recording"):
            return "recording"
        if st.get("recording_ready"):
            return "ready"
        return "loading"

    def _pick_icon(self) -> "Image.Image":
        _ensure_icons()
        key = self._get_tray_state()
        if key in _icons:
            return _icons[key]
        # Fallback: drawn icon if PNG assets were not found
        fallbacks = {
            "setup":     (210, 153, 34),
            "recording": (248,  81, 73),
            "ready":     ( 88, 166, 255),
            "loading":   (110, 118, 129),
        }
        return _create_fallback_icon(fallbacks[key])

    def _pick_tooltip(self) -> str:
        """Return a tooltip string reflecting current state."""
        key = self._get_tray_state()
        tooltips = {
            "setup":     "Meeting Assistant | Setup required",
            "recording": "Meeting Assistant | Recording",
            "ready":     "Meeting Assistant | Ready",
            "loading":   "Meeting Assistant | Loading models…",
        }
        return tooltips.get(key, "Meeting Assistant")

    def _build_menu(self) -> "pystray.Menu":
        S = pystray.MenuItem  # shorthand
        SEP = pystray.Menu.SEPARATOR

        return pystray.Menu(
            S("Meeting Assistant", None, enabled=False),
            SEP,
            # ── Status ────────────────────────────────────────────────────
            S(lambda _: self._status_text(), None, enabled=False),
            SEP,
            # ── Actions ───────────────────────────────────────────────────
            S("Open Web Interface", self._open_browser, default=True),
            S(
                lambda _: "Stop Recording" if self._get_state().get("is_recording") else "Start Recording",
                self._toggle_recording,
                enabled=lambda _: self._get_state().get("recording_ready", False),
            ),
            S("Settings...", self._open_settings),
            S("Test Toast", self._test_toast),
            SEP,
            # ── Server ───────────────────────────────────────────────────
            S("Check for Updates", self._check_updates),
            S("Restart Server", self._restart_server),
            SEP,
            S("Quit", self._quit),
        )

    def _status_text(self) -> str:
        st = self._get_state()
        if config.needs_setup(st.get("ai_provider", "anthropic")):
            return "Setup required"
        if st.get("is_recording"):
            return "Recording..."
        if st.get("recording_ready"):
            return "Ready"
        return st.get("recording_ready_reason", "Loading models...")

    def _diarizer_text(self) -> str:
        st = self._get_state()
        if st.get("diarizer_ready"):
            return "Diarizer: Ready"
        if config.get_key_status()["HUGGING_FACE_KEY"]["is_set"]:
            return "Diarizer: Loading..."
        return "Diarizer: No HF key"

    def _key_line(self, key_name: str) -> str:
        info = config.get_key_status().get(key_name, {})
        label = info.get("label", key_name)
        if info.get("is_set"):
            return f"{label}: {info['masked']}"
        suffix = "" if info.get("required") else " (optional)"
        return f"{label}: not set{suffix}"

    # ── Menu callbacks ────────────────────────────────────────────────────────

    def _open_browser(self, icon=None, item=None) -> None:
        webbrowser.open(self._url)

    def _open_settings(self, icon=None, item=None) -> None:
        webbrowser.open(f"{self._url}/session?settings=1")

    def _check_updates(self, icon=None, item=None) -> None:
        """Open the web UI with the settings panel on the System tab to check for updates."""
        webbrowser.open(f"{self._url}/session?settings=1&section=system")

    def _restart_server(self, icon=None, item=None) -> None:
        """Restart the server via the API.

        Dispatched on a daemon thread: on macOS this menu callback fires on the
        AppKit main run loop, and the POST targets a server that is tearing
        itself down, so the socket can hang until the timeout and freeze the
        whole menu bar. Returning immediately keeps the run loop pumping.
        """
        def _do() -> None:
            try:
                req = urllib.request.Request(
                    f"{self._url}/api/restart",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception:
                pass  # server is restarting, connection will drop
        threading.Thread(target=_do, daemon=True).start()

    def _toggle_recording(self, icon=None, item=None) -> None:
        """Start or stop recording via the local Flask API."""
        st = self._get_state()
        if st.get("is_recording"):
            # Stop: POST on a daemon thread so the AppKit main run loop (which
            # invokes this callback on macOS) is never blocked on the socket.
            def _do_stop() -> None:
                try:
                    req = urllib.request.Request(
                        f"{self._url}/api/recording/stop",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=5)
                except Exception as e:
                    print(f"[tray] stop recording failed: {e}")
            threading.Thread(target=_do_stop, daemon=True).start()
        else:
            # Start: open the session page with ?autostart so the recording
            # goes through the same audio-initialisation path as a normal
            # session-page start (avoids DirectShow echo issues).
            webbrowser.open(f"{self._url}/session?autostart=1")

    def _test_toast(self, icon=None, item=None) -> None:
        """Fire a diagnostic system toast — verifies callbacks + visibility.

        Runs on a daemon thread: send_test_toast() shells out to osascript
        (subprocess.run with a 5 s timeout) on macOS, and this callback fires on
        the AppKit main run loop, so doing it inline would stall the menu bar.
        """
        def _do() -> None:
            try:
                from ui_desktop import notifications
                ok = notifications.send_test_toast()
                if not ok:
                    print("[tray] Test toast failed to dispatch — see [notify] log lines above.")
            except Exception as e:
                print(f"[tray] Test toast error: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _quit(self, icon=None, item=None) -> None:
        self._on_quit(self._icon)
