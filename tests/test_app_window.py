"""One app window: a click that means "show me the app" raises the window that
is already open instead of opening another one next to it.

Source-and-unit checks. Nothing here starts a server, opens a browser or
touches the desktop: the two side effects (find and raise a window, open a new
one) are swapped out, which is the whole reason they sit behind one module.
"""
from pathlib import Path

import pytest

from core import app_window
from core import window_focus


ROOT = Path(__file__).parents[1]


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.fixture
def wired(monkeypatch):
    """app_window with its two side effects recorded instead of performed."""
    calls = {"focused": 0, "opened": [], "pushed": [], "clients": 1, "raises": True}

    def _focus(**_kw):
        calls["focused"] += 1
        return calls["raises"]

    def _open(url, prefer_pwa=False):
        calls["opened"].append((url, prefer_pwa))
        return True

    monkeypatch.setattr(window_focus, "focus_app_window", _focus)
    monkeypatch.setattr(app_window.browser, "open_app_window", _open)
    monkeypatch.setattr(app_window, "_push",
                        lambda event, data: calls["pushed"].append((event, data)))
    monkeypatch.setattr(app_window, "_client_count", lambda: calls["clients"])
    return calls


# ── Which window a click lands in ─────────────────────────────────────────────

def test_a_click_with_a_destination_raises_the_open_window_and_sends_it_there(wired):
    how = app_window.show("http://127.0.0.1:6969/session?settings=1", reason="tray")
    assert how == "focused"
    assert wired["opened"] == [], "no second window"
    assert wired["pushed"] == [("navigate", {"url": "/session?settings=1"})]


def test_a_click_with_no_destination_leaves_the_window_where_it_is(wired):
    """The tray icon means "show me the app". A window watching a recording
    must not be sent home by it."""
    how = app_window.show("http://127.0.0.1:6969", navigate=False, reason="tray")
    assert how == "focused"
    assert wired["pushed"] == []
    assert wired["opened"] == []


def test_with_no_window_to_raise_one_is_opened_the_way_it_always_was(wired):
    wired["raises"] = False
    how = app_window.show("http://127.0.0.1:6969/", prefer_pwa=True)
    assert how == "opened"
    assert wired["opened"] == [("http://127.0.0.1:6969/", True)]
    assert wired["pushed"] == [], "nothing to tell: the new window loads the URL"


def test_a_destination_needs_a_window_that_is_listening(wired):
    """Raising a window we cannot talk to would show the wrong page, so a click
    that carries a destination opens one instead."""
    wired["clients"] = 0
    how = app_window.show("http://127.0.0.1:6969/session?settings=1")
    assert how == "opened"
    assert wired["focused"] == 0, "not even tried"
    assert wired["opened"] == [("http://127.0.0.1:6969/session?settings=1", False)]


def test_show_me_the_app_still_raises_while_the_stream_is_reconnecting(wired):
    """No destination to deliver, so a window on screen is enough."""
    wired["clients"] = 0
    assert app_window.show("http://127.0.0.1:6969", navigate=False) == "focused"


def test_the_default_browser_fallback_is_reported_as_such(wired, monkeypatch):
    """No Chromium on the machine, so the UI opened in whatever browser there
    is. The caller is told, because that window is not one we can raise."""
    wired["raises"] = False
    monkeypatch.setattr(app_window.browser, "open_app_window",
                        lambda url, prefer_pwa=False: False)
    assert app_window.show("http://127.0.0.1:6969/") == "browser"


def test_unconfigured_it_opens_a_window_exactly_as_before(monkeypatch):
    """Imported outside the running app (a unit test, a script), this module
    must never reach out and grab the user's desktop."""
    seen = []
    monkeypatch.setattr(app_window, "_client_count", None)
    monkeypatch.setattr(app_window, "_push", None)
    monkeypatch.setattr(window_focus, "focus_app_window",
                        lambda **_kw: pytest.fail("focused without being configured"))
    monkeypatch.setattr(app_window.browser, "open_app_window",
                        lambda url, prefer_pwa=False: seen.append(url) or True)
    assert app_window.show("http://127.0.0.1:6969/") == "opened"
    assert seen == ["http://127.0.0.1:6969/"]


@pytest.mark.parametrize("url,want", [
    ("http://127.0.0.1:6969", "/"),
    ("http://127.0.0.1:6969/", "/"),
    ("http://127.0.0.1:6969/session?settings=1&section=system",
     "/session?settings=1&section=system"),
    ("http://localhost:6969/session?id=abc&quiet_prompt=1",
     "/session?id=abc&quiet_prompt=1"),
])
def test_the_window_is_told_the_route_not_the_url(url, want):
    assert app_window.route_of(url) == want


# ── Finding the window, without mistaking something else for it ───────────────

_CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@pytest.mark.parametrize("title,cls,exe", [
    # A Chromium --app window's caption IS the document title.
    ("Home \u00b7 Meeting Assistant", "Chrome_WidgetWin_1", _CHROME),
    ("Calendar \u00b7 Meeting Assistant", "Chrome_WidgetWin_1", _CHROME),
    ("Meeting Assistant", "Chrome_WidgetWin_1", _CHROME),
    ("Home \u00b7 Meeting Assistant ", "Chrome_WidgetWin_1", _CHROME),
    ("Meeting Assistant", "Chrome_WidgetWin_2",
     r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
])
def test_an_app_window_is_recognised(title, cls, exe):
    assert window_focus.window_matches(title, cls, exe)


@pytest.mark.parametrize("title,cls,exe,why", [
    ("Home \u00b7 Meeting Assistant - Google Chrome", "Chrome_WidgetWin_1", _CHROME,
     "an ordinary browser window, not the app window"),
    ("Meeting Assistant \u00b7 CHANGELOG.md", "Zed::Window", r"C:\Users\x\Zed.exe",
     "an editor open in a folder of this name"),
    ("app.js - Meeting Assistant - Visual Studio Code", "Chrome_WidgetWin_1",
     r"C:\Users\x\Code.exe", "an Electron editor shares Chromium's window class"),
    ("Meeting Assistant", "CabinetWClass", r"C:\Windows\explorer.exe",
     "the project folder in Explorer"),
    ("", "Chrome_WidgetWin_1", _CHROME, "no caption at all"),
    ("Meeting Assistant", "Chrome_WidgetWin_1", "", "the process could not be read"),
])
def test_other_windows_are_left_alone(title, cls, exe, why):
    assert not window_focus.window_matches(title, cls, exe), why


def test_the_window_hunt_is_windows_and_mac_only():
    """Everywhere else the caller opens a window the way it always has."""
    assert window_focus.focus_app_window.__doc__
    src = _read("core/window_focus.py")
    assert 'if sys.platform == "win32"' in src
    assert 'if sys.platform == "darwin"' in src
    # Telling an application that is not running launches it, and a click that
    # silently started Chrome would be worse than opening our own window.
    assert 'if application "{app}" is running then' in src


# ── Every entry point goes through the one module ─────────────────────────────

def test_the_app_puts_app_window_in_charge():
    app = _read("app.py")
    assert "app_window.configure(push=_push, client_count=_connected_client_count)" in app


def test_the_tray_raises_the_open_window():
    tray = _read("ui_desktop/tray.py")
    assert "browser.open_app_window(" not in tray
    # The icon itself: show the app, wherever it is.
    icon = tray[tray.index("def _open_browser("):tray.index("def _open_settings(")]
    assert "app_window.show(" in icon and "navigate=False" in icon
    # The two items that carry a destination send the open window there.
    rest = tray[tray.index("def _open_settings("):tray.index("def _restart_server(")]
    assert rest.count("app_window.show(") == 2
    assert "navigate=False" not in rest


def test_a_toast_click_lands_in_the_window_that_is_open():
    notif = _read("ui_desktop/notifications.py")
    assert "browser.open_app_window(" not in notif
    quiet = notif[notif.index("def send_quiet_recording_toast("):
                  notif.index("def send_meeting_detected_toast(")]
    # Both the toast body and its Stop button route back to the meeting.
    assert quiet.count("app_window.show(") == 2
    started = notif[notif.index("def send_meeting_autostarted_toast("):]
    assert "app_window.show(" in started


def test_the_page_follows_the_desktop_when_it_is_sent_somewhere():
    js = _read("ui_web/static/app.js")
    listener = js[js.index("src.addEventListener('navigate'"):]
    listener = listener[:listener.index("});") + 3]
    # The page's own router, so a destination behaves the same as a URL.
    assert "navigateTo(d.url)" in listener
    assert "JSON.parse" in listener
