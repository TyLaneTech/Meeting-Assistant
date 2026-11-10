"""Route a "start recording" request into the app window that is already open.

The problem this solves: when a meeting is auto-detected (or the tray / an
agent asks to record), the old code opened a SECOND browser window at
``/session?autostart=1`` just so the page could start the capture. The user
already had the installed app window open, so they ended up with two windows
and two taskbar identities.

The fix is a three-tier escalation that always ends at the old, proven path, so
a meeting can never be lost:

  Tier 1  Push a ``recording_command`` SSE event to every connected client and
          wait a few seconds. A window that is already open picks it up, acks,
          and starts the recording itself. No new window at all. Skipped when
          no client is connected: there is nobody to answer.
  Tier 2  Nothing acked, so open (or focus) the installed PWA window. Chrome's
          ``--app-id`` launch cannot carry a URL, which is exactly why the
          command rides the SSE handshake instead of a query string: see
          ``pending_command()``, which app.py replays into every new client.
  Tier 3  Still nothing, so fall back to what shipped before this module:
          open ``/session?autostart=1`` in a plain app window. This is the only
          remaining place in the app that opens an autostart window.

Each tier runs in its own try, so a failure inside tier 1 or tier 2 falls
through to tier 3 rather than skipping the proven path.

A request made with ``show_window=False`` (the tray's Start) puts one more step
between tiers 1 and 2: the same ``?autostart=1`` page, loaded in a hidden
browser (core/browser.py ``open_hidden_page``). The page does the start exactly
as it does in a window; nothing appears on screen; the hidden browser is closed
once the recording is running. If it cannot be opened, or never answers, tiers
2 and 3 follow as usual. A window showing up beats a meeting going unrecorded.

An ack is not proof of a capture: the elected window can be closed, throw, or
sit on a model that never loads. ``_confirm_started`` therefore watches the
outcome, escalates to the tier-3 window once if the server has been ready for
``ready_grace`` with still no recording, and reports a failure to the caller
(which raises a toast) if even that does not take.

The start itself ALWAYS happens on the page (device selection and readiness
gating live there). Nothing here starts a capture directly.

The class takes every side effect as a callable so it is unit-testable without
Flask, a browser, or a clock: see tests/test_recording_request.py.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

# Poll cadence while waiting on is_recording(). Acks do not wait for this: they
# set an Event that wakes the waiter immediately.
_POLL_SEC = 0.25


def _default_log(level: str, tag: str, msg: str) -> None:
    from core import log as _log
    getattr(_log, level, _log.info)(tag, msg)


def _default_spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True, name="record-confirm").start()


class _HiddenPage:
    """Closes the hidden page it wraps exactly once, whoever gets there first."""

    def __init__(self, page) -> None:
        self._page = page
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._page is None

    def close(self) -> None:
        with self._lock:
            page, self._page = self._page, None
        if page is None:
            return
        try:
            page.close()
        except Exception:  # pragma: no cover - a page that will not close is not a lost start
            pass


class StartRequestCoordinator:
    """Owns the pending "start recording" command and the tier escalation.

    Parameters are all injected so tests can drive this with a fake clock.

    ``log`` is called as ``log(level, tag, msg)`` with level in
    {"info", "warn", "error"}; the default adapts core.log, which exposes those
    three as separate functions. ``on_failure(source, reason)`` is how the
    caller surfaces a lost start to the user (app.py raises a toast).
    ``client_count()`` is how many browser windows are listening, and
    ``is_ready()`` mirrors the recording_ready status field: both let the
    escalation tell "nobody is there" apart from "the model is still loading".
    ``open_hidden(url)`` loads a page nobody can see and returns something with
    ``close()``, or None; without it, ``show_window=False`` is ignored.
    ``hidden_linger`` is how long a hidden page is kept once the recording
    runs, so the page's own start request finishes before it goes.
    """

    def __init__(
        self,
        push: Callable[[str, dict], None],
        open_window: Callable[[str, bool], bool],
        is_recording: Callable[[], bool],
        base_url: Callable[[], str],
        now: Callable[[], float] = time.monotonic,
        sleep: Optional[Callable[[float], None]] = None,
        log: Optional[Callable[[str, str, str], None]] = None,
        spawn: Optional[Callable[[Callable[[], None]], None]] = None,
        client_count: Optional[Callable[[], int]] = None,
        is_ready: Optional[Callable[[], bool]] = None,
        on_failure: Optional[Callable[[str, str], None]] = None,
        open_hidden: Optional[Callable[[str], object]] = None,
        hidden_linger: float = 5.0,
        ack_wait_existing: float = 5.0,
        ack_wait_after_window: float = 20.0,
        ack_wait_after_autostart_window: float = 25.0,
        pending_ttl: float = 120.0,
        ready_grace: float = 45.0,
        not_ready_wait: float = 900.0,
    ) -> None:
        self._push = push
        self._open_window = open_window
        self._is_recording = is_recording
        self._base_url = base_url
        self._now = now
        # When no sleep is injected we can also wake early on an ack; an
        # injected sleep (tests) drives a fake clock instead.
        self._sleep = sleep or time.sleep
        self._ack_wakes_sleep = sleep is None
        self._log = log or _default_log
        self._spawn = spawn or _default_spawn
        self._client_count = client_count or (lambda: 1)
        self._is_ready = is_ready or (lambda: True)
        self._on_failure = on_failure or (lambda source, reason: None)
        self._open_hidden = open_hidden
        self.hidden_linger = float(hidden_linger)

        self.ack_wait_existing = float(ack_wait_existing)
        self.ack_wait_after_window = float(ack_wait_after_window)
        self.ack_wait_after_autostart_window = float(ack_wait_after_autostart_window)
        self.pending_ttl = float(pending_ttl)
        self.ready_grace = float(ready_grace)
        # How long the post-ack watcher tolerates "not ready" (model loading, a
        # reanalysis holding the gate) before reporting a failure. A slow start
        # is not a lost meeting; the acked page starts the moment the gate opens.
        self.not_ready_wait = float(not_ready_wait)

        self._lock = threading.Lock()
        self._pending: Optional[dict] = None
        self._in_flight = False
        self._cancelled = False
        self._ack_event = threading.Event()

    # -- public API ----------------------------------------------------------

    def request_start(self, source: str, reason: str = "", *,
                      show_window: bool = True) -> dict:
        """Ask a client window to start recording, escalating until one does.

        Blocking (up to ~50 s worst case): run it on a worker thread. Returns a
        dict whose "status" is one of already_recording, in_flight,
        started_by_client, cancelled, failed.

        ``show_window=False`` asks for the start without putting a window on
        screen: when no open window takes it, a hidden page does (see the
        module docstring). The visible tiers stay behind it.
        """
        if self._is_recording():
            return {"status": "already_recording", "source": source}

        with self._lock:
            if self._in_flight:
                self._log("info", "record",
                          f"Start request from {source} coalesced into the one in flight")
                return {"status": "in_flight", "source": source}
            nonce = uuid.uuid4().hex
            self._pending = {
                "nonce": nonce,
                "requested_at": self._now(),
                "source": source,
                "reason": reason,
                "acked_by": None,
            }
            self._in_flight = True
            self._cancelled = False
            self._ack_event.clear()

        try:
            # Tier 1: a window may already be open and listening. Each tier is
            # wrapped on its own so a push or launch that blows up still falls
            # through to the proven path below instead of skipping it.
            outcome = None
            try:
                listeners = int(self._client_count())
            except Exception as e:
                self._log("warn", "record", f"Client count unavailable ({e}); assuming one")
                listeners = 1
            if listeners > 0:
                try:
                    self._push("recording_command", {
                        "action": "start", "nonce": nonce,
                        "source": source, "reason": reason,
                    })
                    outcome = self._wait(self.ack_wait_existing)
                except Exception as e:
                    self._log("error", "record", f"Tier 1 (offer to open windows) failed: {e}")
            else:
                self._log("info", "record",
                          f"No app window is connected; going straight to opening one ({source})")
            done = self._settle(outcome, tier=1, nonce=nonce, source=source)
            if done:
                return done

            if not show_window:
                done = self._start_hidden(nonce, source)
                if done:
                    return done

            # Tier 2: open or focus the installed app window. The pending
            # command reaches it through the SSE handshake.
            outcome = None
            try:
                base = self._base_url().rstrip("/")
                self._log("info", "record",
                          f"No client took the start command; opening the app window ({source})")
                self._open_window(f"{base}/session", True)
                outcome = self._wait(self.ack_wait_after_window)
            except Exception as e:
                self._log("error", "record", f"Tier 2 (app window) failed: {e}")
            done = self._settle(outcome, tier=2, nonce=nonce, source=source)
            if done:
                return done

            # Tier 3: the proven path. Never removed, only moved behind the
            # faster tiers, so a meeting is still captured if everything above
            # this line fails.
            outcome = None
            try:
                base = self._base_url().rstrip("/")
                self._log("warn", "record",
                          f"App window did not take the start command; falling back to "
                          f"the autostart window ({source})")
                self._open_window(f"{base}/session?autostart=1", False)
                outcome = self._wait(self.ack_wait_after_autostart_window)
            except Exception as e:
                self._log("error", "record", f"Tier 3 (autostart window) failed: {e}")
            done = self._settle(outcome, tier=3, nonce=nonce, source=source)
            if done:
                return done

            self._clear()
            self._fail(source,
                       "no recording after all three tiers. The meeting is NOT being captured.")
            return {"status": "failed", "source": source, "nonce": nonce}
        except Exception as e:  # pragma: no cover - defensive; never leave state stuck
            self._clear()
            self._log("error", "record", f"Start request from {source} errored: {e}")
            self._fail(source, f"the start request errored ({e}).")
            return {"status": "failed", "source": source, "error": str(e)}

    def acknowledge(self, nonce: str, client_id: str = "") -> bool:
        """Record that a client took the pending command. Wakes the waiter.

        Single-shot: only the FIRST caller for a given nonce gets True. Every
        connected window receives the command, so this is what elects one of
        them to do the start; the others see False and stand down instead of
        racing into a second /api/recording/start.
        """
        with self._lock:
            pending = self._pending
            if not pending or not nonce or pending["nonce"] != nonce:
                return False
            if pending.get("acked_by"):
                return False
            if self._now() - pending["requested_at"] >= self.pending_ttl:
                self._pending = None
                return False
            pending["acked_by"] = client_id or "client"
            # Set under the lock: a set() racing a clear() from a fresh request
            # would otherwise leave the event armed with nothing acked, and the
            # waiter would spin through its whole escalation.
            self._ack_event.set()
        self._log("info", "record", f"Start command acked by {client_id or 'a client'}")
        return True

    def pending_command(self) -> Optional[dict]:
        """The command a newly connected client should act on, or None.

        None once it has expired, been cleared, or a recording is running.
        """
        with self._lock:
            pending = self._pending
            if not pending:
                return None
            if self._now() - pending["requested_at"] >= self.pending_ttl:
                self._pending = None
                return None
            payload = {
                "action": "start",
                "nonce": pending["nonce"],
                "source": pending["source"],
                "reason": pending["reason"],
            }
        if self._is_recording():
            return None
        return payload

    def cancel(self, reason: str = "") -> None:
        """Drop the pending command; any waiting request_start unwinds."""
        with self._lock:
            had = self._pending is not None
            self._pending = None
            self._cancelled = True
        self._ack_event.set()  # wake the waiter so it notices the cancel
        if had:
            self._log("info", "record", f"Start command cancelled ({reason or 'no reason given'})")

    # -- internals -----------------------------------------------------------

    def _clear(self, keep_in_flight: bool = False) -> None:
        with self._lock:
            self._pending = None
            if not keep_in_flight:
                self._in_flight = False
            self._cancelled = False
            self._ack_event.clear()

    def _fail(self, source: str, reason: str) -> None:
        """Log a lost start and hand it to the caller to surface (a toast)."""
        self._log("error", "record", f"Start request from {source} FAILED: {reason}")
        try:
            self._on_failure(source, reason)
        except Exception as e:  # pragma: no cover - a broken toast must not matter
            self._log("warn", "record", f"Failure notification failed: {e}")

    def _start_hidden(self, nonce: str, source: str) -> Optional[dict]:
        """The step for a start that asked for no window: a hidden page.

        It is the ``?autostart=1`` page, so the start is the page's own, the
        same as tier 3's. It also answers the pending command over the SSE
        handshake like any window, which is how its ack reaches ``_wait``.
        None means it could not be opened or never answered, and the visible
        tiers take over; the page is closed first so it cannot start a second
        recording beside them.
        """
        if self._open_hidden is None:
            return None
        page: Optional[_HiddenPage] = None
        outcome = None
        try:
            base = self._base_url().rstrip("/")
            opened = self._open_hidden(f"{base}/session?autostart=1")
            if opened is None:
                self._log("warn", "record",
                          f"No hidden page available; opening the app window instead ({source})")
                return None
            page = _HiddenPage(opened)
            self._log("info", "record",
                      f"No app window took the start command; starting it in a hidden page "
                      f"({source})")
            outcome = self._wait(self.ack_wait_after_window)
        except Exception as e:
            self._log("error", "record", f"Hidden page failed: {e}")
        done = self._settle(outcome, tier=2, nonce=nonce, source=source, page=page)
        if not done and page is not None:
            self._log("warn", "record",
                      f"The hidden page did not take the start command; opening the app "
                      f"window ({source})")
            page.close()
        return done

    def _let_go(self, page: Optional[_HiddenPage]) -> None:
        """Close a hidden page once its start request has had time to finish."""
        if page is None or page.closed:
            return
        if self.hidden_linger > 0:
            self._sleep(self.hidden_linger)
        page.close()

    def _safe_is_ready(self) -> bool:
        try:
            return bool(self._is_ready())
        except Exception:
            return False

    def _nap(self, seconds: float, ack_aware: bool) -> None:
        if ack_aware and self._ack_wakes_sleep:
            self._ack_event.wait(seconds)
        else:
            self._sleep(seconds)

    def _wait(self, timeout: float) -> Optional[str]:
        """Wait for "recording", "ack" or "cancelled"; None on timeout."""
        deadline = self._now() + timeout
        while True:
            if self._is_recording():
                return "recording"
            with self._lock:
                cancelled = self._cancelled
                acked = bool(self._pending and self._pending.get("acked_by"))
            if cancelled:
                return "cancelled"
            if acked:
                return "ack"
            remaining = deadline - self._now()
            if remaining <= 0:
                return None
            self._nap(min(_POLL_SEC, remaining), True)

    def _await_recording(self, timeout: float) -> bool:
        deadline = self._now() + timeout
        while True:
            if self._is_recording():
                return True
            remaining = deadline - self._now()
            if remaining <= 0:
                return False
            self._nap(min(_POLL_SEC, remaining), False)

    def _settle(self, outcome: Optional[str], tier: int, nonce: str,
                source: str, page: Optional[_HiddenPage] = None) -> Optional[dict]:
        """Turn a wait outcome into a return value, or None to keep escalating.

        ``page`` is the hidden page that took the command, if one did. It is
        closed here or by the watcher, never left running.
        """
        if outcome is None:
            return None
        if outcome == "cancelled":
            self._clear()
            if page is not None:
                page.close()
            return {"status": "cancelled", "tier": tier, "source": source, "nonce": nonce}
        if outcome == "recording":
            self._log("info", "record", f"Recording started by a client at tier {tier} ({source})")
            self._clear()
            if page is not None:
                self._spawn(lambda: self._let_go(page))
            return {"status": "started_by_client", "tier": tier, "source": source,
                    "nonce": nonce, "acked": False}
        # Acked but not recording yet: the page acks first, then waits for the
        # model to be ready before it starts. Release the request and watch the
        # real outcome in the background, escalating if the ack goes nowhere.
        self._log("info", "record",
                  f"Start command taken by a client at tier {tier} ({source}); "
                  f"waiting for the recording to begin")
        # The request stays in flight until the watcher below has seen a real
        # recording (or given up), so a second request arriving meanwhile is
        # coalesced instead of pushing a second command at the same window.
        self._clear(keep_in_flight=True)
        self._spawn(lambda: self._confirm_started(tier, source, page))
        return {"status": "started_by_client", "tier": tier, "source": source,
                "nonce": nonce, "acked": True}

    def _confirm_started(self, tier: int, source: str,
                         page: Optional[_HiddenPage] = None) -> None:
        try:
            self._confirm_started_inner(tier, source, page)
        finally:
            # Still in flight while a hidden page lingers, so a second request
            # cannot open another one beside it.
            try:
                self._let_go(page)
            finally:
                with self._lock:
                    self._in_flight = False

    def _confirm_started_inner(self, tier: int, source: str,
                               page: Optional[_HiddenPage] = None) -> None:
        """Make an ack stick, or escalate.

        An ack only means a window took the command. If that window is closed,
        throws, or its start never lands, nothing else would notice. So:

        - recording appears: done.
        - the server has been READY for ``ready_grace`` and there is still no
          recording: the elected window is not coming through. Open the
          tier-3 autostart window once and give it its usual wait.
        - the server is not ready (model still loading): keep waiting, up to
          ``pending_ttl``. That is a slow start, not a lost one, and opening
          more windows would not speed it up.
        - nothing works: report the failure so the caller can toast it.
        """
        deadline = self._now() + self.not_ready_wait
        ready_since: Optional[float] = None
        while True:
            if self._is_recording():
                self._log("info", "record",
                          f"Recording confirmed running (tier {tier}, {source})")
                return
            now = self._now()
            if self._safe_is_ready():
                if ready_since is None:
                    ready_since = now
                elif now - ready_since >= self.ready_grace:
                    break
            else:
                ready_since = None
            if now >= deadline:
                self._fail(source,
                           f"a window acked the start command (tier {tier}) but no recording "
                           f"after {self.not_ready_wait:g}s and the app never became ready.")
                return
            self._nap(min(_POLL_SEC, max(0.0, deadline - now)), False)

        self._log("warn", "record",
                  f"A window acked the start command (tier {tier}, {source}) but never "
                  f"started after {self.ready_grace:g}s of readiness; falling back to the "
                  f"autostart window")
        # A hidden page that took the command and never started goes first, so
        # it cannot wake up and start a second recording beside the window.
        if page is not None:
            page.close()
        try:
            base = self._base_url().rstrip("/")
            self._open_window(f"{base}/session?autostart=1", False)
        except Exception as e:
            self._log("error", "record", f"Fallback autostart window failed: {e}")
        if self._await_recording(self.ack_wait_after_autostart_window):
            self._log("info", "record",
                      f"Recording confirmed running after the fallback window ({source})")
            return
        self._fail(source,
                   "a window acked the start command but never started, and the fallback "
                   "window did not either.")


# -- module-level registry -----------------------------------------------------
# notifications.py, tray.py and the Agent API all need the coordinator app.py
# built. Importing app.py from them would be circular, so app.py registers the
# instance here and they reach it through these helpers.

_default: Optional[StartRequestCoordinator] = None


def set_default(coordinator: Optional[StartRequestCoordinator]) -> None:
    global _default
    _default = coordinator


def get_default() -> Optional[StartRequestCoordinator]:
    return _default


def request_start(source: str, reason: str = "", *, show_window: bool = True) -> dict:
    """Blocking start request against the registered coordinator."""
    coordinator = _default
    if coordinator is None:
        _default_log("error", "record",
                     f"Start request from {source} dropped: no coordinator registered")
        return {"status": "unavailable", "source": source}
    return coordinator.request_start(source, reason, show_window=show_window)


def request_start_async(source: str, reason: str = "", *, show_window: bool = True) -> None:
    """Fire-and-forget start request. Safe to call from a UI callback thread."""
    threading.Thread(target=request_start, args=(source, reason),
                     kwargs={"show_window": show_window},
                     daemon=True, name="record-request").start()
