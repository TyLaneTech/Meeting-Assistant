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
