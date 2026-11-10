"""StartRequestCoordinator: the three-tier "start recording" escalation.

Everything the coordinator touches (clock, sleep, SSE push, window opening,
recording state, readiness, client count, logging, threads, failure reporting)
is injected, so these tests run with no server, no browser, no network and no
real waiting: the fake clock advances only when the coordinator sleeps.

Run: .venv/Scripts/python -m pytest tests/test_recording_request.py -q
  or .venv/Scripts/python tests/test_recording_request.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.recording_request import StartRequestCoordinator

BASE = "http://localhost:6969"
SESSION_URL = BASE + "/session"
AUTOSTART_URL = BASE + "/session?autostart=1"


class FakeClock:
    """Monotonic clock whose only source of movement is sleep()."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, seconds)


class Harness:
    """A coordinator plus recorders for every side effect it can produce."""

    def __init__(self, **kwargs) -> None:
        self.clock = FakeClock()
        self.pushed = []      # [(event, data)]
        self.windows = []     # [(url, prefer_pwa)]
        self.logs = []        # [(level, msg)]
        self.spawned = []     # [callable]
        self.failures = []    # [(source, reason)]
        self.recording = False
        self.recording_at = None  # clock time at which recording flips on
        self.ready = True         # mirrors the recording_ready status field
        self.clients = 1
        self.on_push = None      # hook(data) called for a recording_command
        self.on_window = None    # hook(url, prefer_pwa)
        self.coord = StartRequestCoordinator(
            push=self._push,
            open_window=self._open_window,
            is_recording=self._is_recording,
            base_url=lambda: BASE,
            now=self.clock.now,
            sleep=self.clock.sleep,
            log=lambda level, tag, msg: self.logs.append((level, msg)),
            spawn=lambda fn: self.spawned.append(fn),
            client_count=lambda: self.clients,
            is_ready=lambda: self.ready,
            on_failure=lambda source, reason: self.failures.append((source, reason)),
            **kwargs,
        )

    def _is_recording(self):
        if self.recording:
            return True
        return self.recording_at is not None and self.clock.t >= self.recording_at

    def _push(self, event, data):
        self.pushed.append((event, data))
        if self.on_push and event == "recording_command":
            self.on_push(data)

    def _open_window(self, url, prefer_pwa):
        self.windows.append((url, prefer_pwa))
        if self.on_window:
            self.on_window(url, prefer_pwa)
        return True

    def nonce(self):
        return self.pushed[0][1]["nonce"]

    def levels(self):
        return [level for level, _ in self.logs]


# -- tiers ---------------------------------------------------------------------

def test_tier1_existing_window_acks_and_no_window_is_opened():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")

    result = h.coord.request_start("tray")

    assert result["status"] == "started_by_client", result
    assert result["tier"] == 1, result
    assert result["acked"] is True
    assert h.windows == [], "tier 1 must not open any window"
    assert h.pushed[0][0] == "recording_command"
    assert h.pushed[0][1]["action"] == "start"
    assert h.pushed[0][1]["source"] == "tray"
    # No time passes: the ack lands on the first check.
    assert h.clock.t == 1000.0
    # The ack is not proof of a capture, so the outcome is confirmed later.
    assert len(h.spawned) == 1


def test_tier2_opens_the_pwa_window_which_then_acks():
    h = Harness()

    def _window(url, prefer_pwa):
        h.coord.acknowledge(h.nonce(), "client-2")

    h.on_window = _window

    result = h.coord.request_start("toast:autostart", "Teams meeting detected")

    assert result["status"] == "started_by_client", result
    assert result["tier"] == 2, result
    assert h.windows == [(SESSION_URL, True)], h.windows
    # Tier 1 ran its full wait before escalating.
    assert h.clock.t == 1000.0 + h.coord.ack_wait_existing


def test_tier3_falls_back_to_the_autostart_window():
    h = Harness()

    def _window(url, prefer_pwa):
        if url == AUTOSTART_URL:
            h.recording = True   # the proven path started the capture

    h.on_window = _window

    result = h.coord.request_start("meeting-detect")

    assert result["status"] == "started_by_client", result
    assert result["tier"] == 3, result
    assert result["acked"] is False
    assert h.windows == [(SESSION_URL, True), (AUTOSTART_URL, False)], h.windows
    assert h.clock.t == 1000.0 + 5.0 + 20.0
    assert h.coord.pending_command() is None


def test_total_failure_reports_failed_and_logs_an_error():
    h = Harness()

    result = h.coord.request_start("tray")

    assert result["status"] == "failed", result
    assert h.windows == [(SESSION_URL, True), (AUTOSTART_URL, False)], h.windows
    assert h.clock.t == 1000.0 + 5.0 + 20.0 + 25.0
    assert "error" in h.levels(), h.logs
    assert h.coord.pending_command() is None
    # State is released, so the next request is not stuck behind this one.
    assert h.coord.request_start("tray")["status"] != "in_flight"


def test_already_recording_is_a_no_op():
    h = Harness()
    h.recording = True

    result = h.coord.request_start("tray")

    assert result == {"status": "already_recording", "source": "tray"}
    assert h.pushed == []
    assert h.windows == []


def test_concurrent_requests_coalesce_into_one():
    h = Harness()
    seen = {}

    def _push_hook(d):
        # A second caller arriving while the first is still escalating.
        seen["second"] = h.coord.request_start("agent_api")
        h.coord.acknowledge(d["nonce"], "client-1")

    h.on_push = _push_hook

    first = h.coord.request_start("tray")

    assert seen["second"]["status"] == "in_flight", seen
    assert first["status"] == "started_by_client"
    # One command, one nonce, no extra windows.
    assert len(h.pushed) == 1
    assert h.windows == []


# -- P0-3: a broken tier must not swallow the proven path ----------------------

def test_a_tier2_window_that_raises_still_reaches_the_autostart_window():
    h = Harness()

    def _window(url, prefer_pwa):
        if url == SESSION_URL:
            raise OSError("chrome_proxy is not where we thought")
        h.recording = True   # the tier-3 autostart window did start the capture

    h.on_window = _window

    result = h.coord.request_start("tray")

    assert result["status"] == "started_by_client", result
    assert result["tier"] == 3, result
    assert h.windows == [(SESSION_URL, True), (AUTOSTART_URL, False)], h.windows
    assert "error" in h.levels(), h.logs
    assert h.failures == []


def test_a_push_that_raises_at_tier1_still_reaches_the_later_tiers():
    h = Harness()

    def _push_hook(d):
        raise RuntimeError("SSE fan-out blew up")

    def _window(url, prefer_pwa):
        if url == AUTOSTART_URL:
            h.recording = True

    h.on_push = _push_hook
    h.on_window = _window

    result = h.coord.request_start("tray")

    assert result["status"] == "started_by_client", result
    assert result["tier"] == 3, result
    assert h.windows == [(SESSION_URL, True), (AUTOSTART_URL, False)], h.windows


# -- P2-1: do not wait on windows that are not there ---------------------------

def test_no_connected_client_skips_tier_one():
    h = Harness()
    h.clients = 0
    seen = {}

    def _window(url, prefer_pwa):
        seen["t"] = h.clock.t
        # A window connecting during tier 2 still finds the command waiting.
        seen["pending"] = h.coord.pending_command()
        h.coord.acknowledge(seen["pending"]["nonce"], "client-2")

    h.on_window = _window

    result = h.coord.request_start("meeting-detect")

    assert h.pushed == [], "nobody is listening, so there is nothing to push to"
    assert seen["t"] == 1000.0, "tier 1 must not burn its wait with zero clients"
    assert seen["pending"] is not None, "the command is minted before the window opens"
    assert h.windows == [(SESSION_URL, True)], h.windows
    assert result["tier"] == 2, result


# -- P1-1: a lost start is reported to the user --------------------------------

def test_on_failure_fires_exactly_once_on_total_failure():
    h = Harness()

    result = h.coord.request_start("toast:autostart")

    assert result["status"] == "failed"
    assert len(h.failures) == 1, h.failures
    assert h.failures[0][0] == "toast:autostart"
    assert "NOT being captured" in h.failures[0][1]


def test_on_failure_is_not_called_when_a_client_starts():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")
    h.recording_at = 1000.0   # the client started it straight away

    h.coord.request_start("tray")
    for fn in h.spawned:
        fn()

    assert h.failures == []


def test_a_failing_failure_callback_cannot_break_the_coordinator():
    h = Harness()

    def _boom(source, reason):
        raise RuntimeError("the toast backend is missing")

    h.coord._on_failure = _boom

    result = h.coord.request_start("tray")

    assert result["status"] == "failed", result
    assert "warn" in h.levels(), h.logs


# -- P1-2: an ack is watched, not trusted --------------------------------------

def test_watcher_is_quiet_when_the_recording_appears_within_the_grace():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")
    h.recording_at = 1010.0   # the elected window started it 10 s later

    h.coord.request_start("tray")
    assert len(h.spawned) == 1
    h.spawned[0]()   # the watcher, run inline instead of on its thread

    assert h.windows == [], "a start that lands needs no fallback window"
    assert h.failures == []
    assert h.clock.t == 1010.0


def test_watcher_opens_the_autostart_window_after_the_ready_grace():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")
    h.ready = True   # the app is ready, so the elected window has no excuse

    def _window(url, prefer_pwa):
        h.recording = True   # the fallback window starts it

    h.on_window = _window

    h.coord.request_start("tray")
    h.spawned[0]()

    assert h.windows == [(AUTOSTART_URL, False)], h.windows
    assert h.failures == [], "the fallback worked, so nothing to report"
    assert "warn" in h.levels(), h.logs
    assert h.clock.t == 1000.0 + h.coord.ready_grace


def test_watcher_reports_a_failure_when_the_fallback_window_also_fails():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")

    h.coord.request_start("tray")
    h.spawned[0]()

    assert h.windows == [(AUTOSTART_URL, False)], h.windows
    assert len(h.failures) == 1, h.failures
    assert h.clock.t == 1000.0 + h.coord.ready_grace + h.coord.ack_wait_after_autostart_window


def test_watcher_keeps_waiting_while_the_app_is_not_ready():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")
    h.ready = False   # the transcription model is still loading

    h.coord.request_start("tray")
    h.spawned[0]()

    assert h.windows == [], "more windows cannot make the model load faster"
    assert len(h.failures) == 1, h.failures
    assert h.clock.t == 1000.0 + h.coord.not_ready_wait
    assert h.coord.not_ready_wait >= 600.0, "a reanalysis can hold the gate for minutes"


def test_watcher_waits_out_a_slow_model_and_then_succeeds():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")
    h.ready = False
    h.recording_at = 1090.0   # model finally loaded and the window started it

    h.coord.request_start("tray")
    h.spawned[0]()

    assert h.windows == []
    assert h.failures == []
    assert h.clock.t == 1090.0


# -- pending command lifecycle -------------------------------------------------

def test_pending_command_is_cleared_on_success():
    h = Harness()
    seen = {}

    def _push_hook(d):
        seen["during"] = h.coord.pending_command()
        h.coord.acknowledge(d["nonce"])

    h.on_push = _push_hook
    h.coord.request_start("tray")

    assert seen["during"] is not None
    assert seen["during"]["nonce"] == h.nonce()
    assert seen["during"]["action"] == "start"
    assert h.coord.pending_command() is None


def test_pending_command_expires_and_is_not_replayed_later():
    h = Harness(pending_ttl=30.0)
    seen = {}

    def _window(url, prefer_pwa):
        if url == SESSION_URL:
            # A window that opens long after the request (a machine that slept)
            # must not be handed a stale start command.
            h.clock.t += 31.0
            seen["after_ttl"] = h.coord.pending_command()
            seen["ack_after_ttl"] = h.coord.acknowledge(h.nonce())

    h.on_window = _window
    result = h.coord.request_start("tray")

    assert seen["after_ttl"] is None
    assert seen["ack_after_ttl"] is False
    assert result["status"] == "failed", result
    assert h.coord.pending_command() is None


def test_pending_command_is_hidden_once_a_recording_is_running():
    h = Harness()
    seen = {}

    def _push_hook(d):
        seen["before"] = h.coord.pending_command()
        h.recording = True   # a client took it and the capture began
        seen["after"] = h.coord.pending_command()

    h.on_push = _push_hook
    result = h.coord.request_start("tray")

    assert seen["before"] is not None
    assert seen["after"] is None, "a running recording must not replay a command"
    assert result["status"] == "started_by_client"
    assert result["tier"] == 1


def test_acknowledge_rejects_an_unknown_nonce():
    h = Harness()
    seen = {}

    def _push_hook(d):
        seen["wrong"] = h.coord.acknowledge("not-the-nonce", "client-x")
        seen["empty"] = h.coord.acknowledge("", "client-x")
        seen["right"] = h.coord.acknowledge(d["nonce"], "client-x")

    h.on_push = _push_hook
    h.coord.request_start("tray")

    assert seen["wrong"] is False
    assert seen["empty"] is False
    assert seen["right"] is True
    # Nothing is pending afterwards, so a late ack is rejected too.
    assert h.coord.acknowledge(h.nonce()) is False


def test_only_the_first_window_wins_the_ack():
    h = Harness()
    seen = {}

    def _push_hook(d):
        # Two windows are open; both receive the command.
        seen["first"] = h.coord.acknowledge(d["nonce"], "window-a")
        seen["second"] = h.coord.acknowledge(d["nonce"], "window-b")

    h.on_push = _push_hook
    h.coord.request_start("tray")

    assert seen["first"] is True
    assert seen["second"] is False, "a second window must stand down, not double-start"


def test_cancel_unwinds_a_request_without_opening_a_window():
    h = Harness()
    h.on_push = lambda d: h.coord.cancel("user stopped it")

    result = h.coord.request_start("tray")

    assert result["status"] == "cancelled", result
    assert h.windows == []
    assert h.coord.pending_command() is None
    assert h.failures == []


# -- registry ------------------------------------------------------------------

def test_no_registered_coordinator_degrades_honestly():
    from core import recording_request

    previous = recording_request.get_default()
    # Stub the default logger so the test never touches core.log (which would
    # create storage/data/logs on disk).
    previous_log = recording_request._default_log
    seen = []
    recording_request._default_log = lambda level, tag, msg: seen.append(level)
    recording_request.set_default(None)
    try:
        assert recording_request.request_start("tray")["status"] == "unavailable"
        assert seen == ["error"], seen
    finally:
        recording_request.set_default(previous)
        recording_request._default_log = previous_log


def test_registered_coordinator_receives_module_level_requests():
    from core import recording_request

    h = Harness()
    h.recording = True   # returns immediately; we only care about the routing
    previous = recording_request.get_default()
    recording_request.set_default(h.coord)
    try:
        assert recording_request.request_start("tray")["status"] == "already_recording"
    finally:
        recording_request.set_default(previous)


# -- P1-4: only launch a PWA that is actually installed ------------------------

def test_pwa_launch_requires_the_installed_app_folder():
    from core import browser

    app_id = "pmaddcbhfddcgdflmbmpneamdilppkbn"
    previous = os.environ.get("LOCALAPPDATA")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        try:
            assert browser.pwa_is_installed(app_id) is False, "no profile folder yet"

            installed = os.path.join(tmp, "Google", "Chrome", "User Data", "Default",
                                     "Web Applications", f"_crx_{app_id}")
            os.makedirs(installed)
            assert browser.pwa_is_installed(app_id) is True
            assert browser.pwa_is_installed("some-other-app-id") is False
            assert browser.pwa_is_installed("") is False

            os.environ.pop("LOCALAPPDATA")
            assert browser.pwa_is_installed(app_id) is False, "no LOCALAPPDATA, no claim"
        finally:
            if previous is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = previous


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in TESTS:
        fn()
        print("ok  " + fn.__name__)
    print(f"OK test_recording_request ({len(TESTS)} tests)")


def test_pwa_launch_is_skipped_without_the_installed_app_folder(monkeypatch, tmp_path):
    from core import browser
    calls = []
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(browser, "_chrome_proxy_path", lambda: str(tmp_path / "chrome_proxy.exe"))
    monkeypatch.setattr(browser.settings, "get", lambda key, default="": "abcdefghijklmnopabcdefghijklmnop")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert browser._open_installed_pwa() is False
    assert calls == []


# -- a start that asks for no window (the tray) ----------------------------------

class FakePage:
    """A hidden page: records whether it was closed and when."""

    def __init__(self, clock=None) -> None:
        self.closes = 0
        self.clock = clock
        self.closed_at = None

    def close(self):
        self.closes += 1
        if self.clock:
            self.closed_at = self.clock.t


def _hidden_harness(clients=0, page="fresh", on_open=None):
    """A harness whose open_hidden hands out one FakePage (or None)."""
    opened = []
    box = {}

    def _open_hidden(url):
        opened.append(url)
        if page is None:
            return None
        box["page"] = FakePage(h.clock)
        if on_open:
            on_open(h)
        return box["page"]

    h = Harness(open_hidden=_open_hidden)
    h.clients = clients
    h.opened_hidden = opened
    h.page = lambda: box.get("page")
    return h


def test_a_windowless_start_with_no_window_open_uses_a_hidden_page():
    h = _hidden_harness(on_open=lambda h: h.coord.acknowledge(h.coord.pending_command()["nonce"],
                                                               "hidden"))

    result = h.coord.request_start("tray", show_window=False)

    assert result["status"] == "started_by_client", result
    assert result["acked"] is True
    assert h.opened_hidden == [AUTOSTART_URL], "the hidden page is the ?autostart page"
    assert h.windows == [], "no window may open"
    assert h.page().closes == 0, "the page is still starting the recording"

    # The watcher sees the recording, lets the page finish, then closes it.
    h.recording_at = h.clock.t + 2.0
    started_at = h.clock.t
    h.spawned[0]()
    assert h.page().closes == 1
    assert h.page().closed_at >= started_at + 2.0 + h.coord.hidden_linger
    assert h.windows == []
    assert h.failures == []


def test_a_hidden_page_that_starts_at_once_is_closed_after_its_linger():
    def _open(h):
        h.recording = True

    h = _hidden_harness(on_open=_open)
    result = h.coord.request_start("tray", show_window=False)

    assert result["status"] == "started_by_client" and result["acked"] is False
    assert h.page().closes == 0
    assert len(h.spawned) == 1
    before = h.clock.t
    h.spawned[0]()
    assert h.page().closes == 1
    assert h.page().closed_at == before + h.coord.hidden_linger
    assert h.windows == []


def test_an_open_window_still_takes_a_windowless_start():
    h = _hidden_harness(clients=1)
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "window")

    result = h.coord.request_start("tray", show_window=False)

    assert result["tier"] == 1
    assert h.opened_hidden == [], "no hidden page when a window answered"
    assert h.windows == []


def test_a_normal_start_never_uses_a_hidden_page():
    h = _hidden_harness()

    def _window(url, prefer_pwa):
        h.coord.acknowledge(h.coord.pending_command()["nonce"], "window")

    h.on_window = _window
    result = h.coord.request_start("toast:autostart")

    assert result["tier"] == 2
    assert h.opened_hidden == []
    assert h.windows == [(SESSION_URL, True)]


def test_no_hidden_browser_falls_back_to_the_app_window():
    h = _hidden_harness(page=None)

    def _window(url, prefer_pwa):
        h.coord.acknowledge(h.coord.pending_command()["nonce"], "window")

    h.on_window = _window
    result = h.coord.request_start("tray", show_window=False)

    assert h.opened_hidden == [AUTOSTART_URL]
    assert h.windows == [(SESSION_URL, True)], "a meeting is never lost to a missing browser"
    assert result["status"] == "started_by_client"
    assert any("No hidden page available" in msg for _, msg in h.logs)


def test_a_hidden_page_that_never_answers_is_closed_before_a_window_opens():
    h = _hidden_harness()
    seen = {}

    def _window(url, prefer_pwa):
        seen.setdefault("closed_first", h.page().closes == 1)
        h.coord.acknowledge(h.coord.pending_command()["nonce"], "window")

    h.on_window = _window
    result = h.coord.request_start("tray", show_window=False)

    assert seen["closed_first"] is True, "it could otherwise start a second recording"
    assert h.windows == [(SESSION_URL, True)]
    assert result["status"] == "started_by_client"
    assert h.page().closes == 1


def test_a_hidden_page_that_acks_but_never_starts_gives_way_to_the_autostart_window():
    h = _hidden_harness(on_open=lambda h: h.coord.acknowledge(h.coord.pending_command()["nonce"],
                                                               "hidden"))
    seen = {}

    def _window(url, prefer_pwa):
        if url == AUTOSTART_URL:
            seen["closed_first"] = h.page().closes == 1
            h.recording = True

    h.on_window = _window
    h.coord.request_start("tray", show_window=False)
    h.spawned[0]()

    assert seen.get("closed_first") is True
    assert h.windows == [(AUTOSTART_URL, False)]
    assert h.page().closes == 1, "closed once, not again after the window started it"
    assert h.failures == []


def test_cancelling_a_windowless_start_closes_the_hidden_page():
    h = _hidden_harness(on_open=lambda h: h.coord.cancel("user stopped it"))

    result = h.coord.request_start("tray", show_window=False)

    assert result["status"] == "cancelled"
    assert h.page().closes == 1
    assert h.windows == []


def test_a_hidden_page_holds_the_request_in_flight_until_it_is_closed():
    h = _hidden_harness(on_open=lambda h: h.coord.acknowledge(h.coord.pending_command()["nonce"],
                                                               "hidden"))
    h.coord.request_start("tray", show_window=False)
    assert h.coord.request_start("agent_api")["status"] == "in_flight"
    h.recording_at = h.clock.t
    h.spawned[0]()
    h.recording = False   # that meeting ended; a new request is a new request
    assert h.coord.request_start("tray", show_window=False)["status"] != "in_flight"


def test_the_hidden_page_is_a_headless_muted_browser_with_its_own_profile(monkeypatch, tmp_path):
    from core import browser
    calls = []

    class _Proc:
        def poll(self):
            return None

    monkeypatch.setattr(browser, "_chromium_path", lambda: r"C:\Chrome\chrome.exe")
    monkeypatch.setattr(browser, "_HIDDEN_PROFILES", tmp_path / "hidden-browser")
    monkeypatch.setattr(browser.subprocess, "Popen",
                        lambda args, **kw: calls.append((args, kw)) or _Proc())

    page = browser.open_hidden_page(AUTOSTART_URL)

    assert isinstance(page, browser.HiddenPage)
    args, kw = calls[0]
    assert args[0] == r"C:\Chrome\chrome.exe"
    assert "--headless=new" in args and "--mute-audio" in args
    assert args[-1] == AUTOSTART_URL
    profile = [a for a in args if a.startswith("--user-data-dir=")]
    assert len(profile) == 1
    assert profile[0].startswith(f"--user-data-dir={tmp_path / 'hidden-browser'}")
    # Never the user's profile, and never an app window.
    assert not any(a.startswith(("--profile-directory", "--app")) for a in args)
    assert kw["stdout"] is browser.subprocess.DEVNULL
    if sys.platform == "win32":
        assert kw["creationflags"] == browser.subprocess.CREATE_NO_WINDOW


def test_no_browser_means_no_hidden_page(monkeypatch):
    from core import browser
    monkeypatch.setattr(browser, "_chromium_path", lambda: None)
    monkeypatch.setattr(browser.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("launched")))
    assert browser.open_hidden_page(AUTOSTART_URL) is None


def test_closing_a_hidden_page_ends_its_browser_and_deletes_its_profile(tmp_path):
    from core import browser

    class _Proc:
        def __init__(self):
            self.killed = False

        def poll(self):
            return 0 if self.killed else None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    profile = tmp_path / "abc"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Preferences").write_text("{}")
    proc = _Proc()
    page = browser.HiddenPage(proc, profile)
    page.close()
    assert proc.killed
    assert not profile.exists()
    page.close()   # a second close is harmless


def test_the_profile_sweep_leaves_a_page_that_is_still_running_alone(monkeypatch, tmp_path):
    from core import browser
    root = tmp_path / "hidden-browser"
    old, fresh = root / "old", root / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    stale = time.time() - 7200
    os.utime(old, (stale, stale))
    monkeypatch.setattr(browser, "_HIDDEN_PROFILES", root)
    browser._sweep_hidden_profiles()
    assert not old.exists()
    assert fresh.exists()


def test_a_request_during_the_watcher_is_coalesced():
    h = Harness()
    h.on_push = lambda d: h.coord.acknowledge(d["nonce"], "client-1")

    first = h.coord.request_start("tray")
    assert first["status"] == "started_by_client"
    assert len(h.spawned) == 1, "the watcher should have been spawned"

    # The watcher has not run yet: a second request must coalesce rather than
    # push a second command at the same window.
    second = h.coord.request_start("agent_api")
    assert second["status"] == "in_flight"

    h.recording_at = h.clock.t + 2.0
    h.spawned[0]()   # the watcher sees the recording and releases the request
    assert h.windows == []

    third = h.coord.request_start("tray")
    assert third["status"] == "already_recording"
