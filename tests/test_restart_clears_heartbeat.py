"""An in-app restart must read as a clean quit to the external watchdog.

Source-level only (importing app.py loads the models). The restart routes stop
everything, spawn the relaunch chain and os._exit(0); the chain takes about
20 s to answer HTTP and the watchdog polls every 20 s, so a poll landing in
that gap saw a dead pid with the heartbeat still present, logged a crash,
toasted the user and launched a second, competing chain (2026-09-05). Every
relaunch path now stops the heartbeat writer and removes the file first.
"""
import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    m = re.search(rf"^(\s*)def {name}\(.*?\n(.*?)(?=^\1def |\Z)", APP_PY, re.M | re.S)
    assert m, f"{name} not found"
    return m.group(2)


def test_heartbeat_loop_stops_on_the_event():
    body = _function_body("_heartbeat_loop")
    assert "while not _heartbeat_stop.is_set():" in body
    assert "_heartbeat_stop.wait(" in body, "sleep must be interruptible by the stop event"


def test_stop_heartbeat_sets_the_event_and_clears_the_file():
    body = _function_body("_stop_heartbeat")
    assert "_heartbeat_stop.set()" in body
    assert "heartbeat.clear()" in body


def test_every_relaunch_stops_the_heartbeat_first():
    calls = [m.start() for m in re.finditer(r"^\s+_relaunch_app\(\)", APP_PY, re.M)]
    assert len(calls) >= 2, "expected the restart and update paths to relaunch"
    for pos in calls:
        head = APP_PY.rfind("\n    def ", 0, pos)
        head = max(head, APP_PY.rfind("\ndef ", 0, pos))
        between = APP_PY[head:pos]
        assert "_stop_heartbeat()" in between, (
            "a relaunch path exits without clearing the heartbeat:\n" + between[-400:])


def test_clean_quit_uses_the_same_helper():
    assert "_stop_heartbeat()" in _function_body("_force_quit")


def test_a_write_under_way_cannot_land_after_the_clear():
    # Stopping the event alone left a gap: a write that had already started
    # could recreate the file just after it was removed.
    loop = _function_body("_heartbeat_loop")
    locked = loop[loop.index("with _heartbeat_write_lock:"):]
    assert locked.index("if not _heartbeat_stop.is_set():") < locked.index("heartbeat.write(")
    stop = _function_body("_stop_heartbeat")
    assert stop.index("_heartbeat_write_lock.acquire(") < stop.index("_heartbeat_stop.set()")
    assert stop.index("_heartbeat_stop.set()") < stop.index("heartbeat.clear()")
