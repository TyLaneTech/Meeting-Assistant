"""The external watchdog's one decision: restart a crashed or frozen app, leave
a clean quit alone, and never kill a pid that is already dead (Windows reuses
pids quickly, and taskkill /T on a reused pid takes down an unrelated tree).
Run: .venv/Scripts/python tests/test_watchdog_decision.py  (or pytest)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchdog  # noqa: E402


class _FakeHeartbeat:
    def __init__(self, record):
        self.record = record
        self.cleared = 0

    def read(self):
        return self.record

    def clear(self):
        self.cleared += 1


def _run(monkeypatch, *, http_ok, record, pid_alive, seen_healthy=True):
    calls = {"kill": [], "relaunch": 0, "log": []}
    hb = _FakeHeartbeat(record)
    monkeypatch.setattr(watchdog, "heartbeat", hb)
    monkeypatch.setattr(watchdog, "_http_ok", lambda: http_ok)
    monkeypatch.setattr(watchdog, "_pid_alive", lambda pid: pid_alive)
    monkeypatch.setattr(watchdog, "_kill", lambda pid: calls["kill"].append(pid))
    monkeypatch.setattr(watchdog, "_relaunch", lambda: calls.__setitem__("relaunch", calls["relaunch"] + 1))
    monkeypatch.setattr(watchdog, "_toast", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "_log", lambda m: calls["log"].append(m))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    state = {"seen_healthy": seen_healthy, "restarts": []}
    watchdog._decide_and_act(state)
    return calls, hb, state


def test_absent_heartbeat_is_a_clean_quit(monkeypatch):
    calls, hb, _ = _run(monkeypatch, http_ok=False, record=None, pid_alive=False)
    assert calls["relaunch"] == 0 and calls["kill"] == []


def test_dead_pid_relaunches_without_killing(monkeypatch):
    record = {"pid": 2128, "ts": time.time() - 30, "recording": False}
    calls, hb, state = _run(monkeypatch, http_ok=False, record=record, pid_alive=False)
    assert calls["relaunch"] == 1
    assert calls["kill"] == [], "a dead pid may already belong to another process"
    assert hb.cleared == 1
    assert state["seen_healthy"] is False


def test_frozen_app_is_killed_then_relaunched(monkeypatch):
    record = {"pid": 4242, "ts": time.time() - (watchdog.GRACE_SEC + 5), "recording": True}
    calls, hb, _ = _run(monkeypatch, http_ok=False, record=record, pid_alive=True)
    assert calls["kill"] == [4242]
    assert calls["relaunch"] == 1


def test_unreachable_but_fresh_heartbeat_is_only_watched(monkeypatch):
    record = {"pid": 4242, "ts": time.time() - 5, "recording": False}
    calls, hb, _ = _run(monkeypatch, http_ok=False, record=record, pid_alive=True)
    assert calls["relaunch"] == 0 and calls["kill"] == []
    assert any("watching" in m for m in calls["log"])


def test_never_acts_before_seeing_the_app_healthy(monkeypatch):
    record = {"pid": 2128, "ts": time.time() - 500, "recording": False}
    calls, hb, _ = _run(monkeypatch, http_ok=False, record=record, pid_alive=False, seen_healthy=False)
    assert calls["relaunch"] == 0 and calls["kill"] == []


def test_healthy_app_arms_the_watchdog(monkeypatch):
    calls, hb, state = _run(monkeypatch, http_ok=True, record=None, pid_alive=True, seen_healthy=False)
    assert state["seen_healthy"] is True
    assert calls["relaunch"] == 0
