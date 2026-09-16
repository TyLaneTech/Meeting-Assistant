"""A second instance must not take over one that is rebuilding a transcript.

The handshake read ``is_recording`` and nothing else, so launching the app
again (Start Menu, tray, app_launcher.vbs) during a reanalysis made the busy
instance answer "idle", accept /api/shutdown and hard exit through os._exit.
The reanalysis worker is a daemon thread, so it died mid-rebuild with the old
transcript already deleted. It happened twice on the same meeting.

app.py is never imported here (that loads the transcription model), so the
decision function is lifted out of the source and exercised on its own, and the
routes that consume it are checked at source level.

Run: .venv/Scripts/python.exe -m pytest tests/test_instance_handshake_busy.py -q
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")


def _busy_reason_for(state: dict):
    """Run app.py's own _busy_reason against a stand-in _state."""
    tree = ast.parse(APP_PY)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_busy_reason"), None)
    assert fn is not None, "app.py no longer defines _busy_reason"
    module = ast.Module(body=[fn], type_ignores=[])
    scope: dict = {"_state": state, "_state_lock": threading.RLock()}
    exec(compile(module, "<app._busy_reason>", "exec"), scope)
    return scope["_busy_reason"]()


# ── The decision ────────────────────────────────────────────────────────────

def test_idle_is_idle():
    assert _busy_reason_for({"is_recording": False, "is_reanalyzing": False}) == ""


def test_a_recording_is_busy():
    assert _busy_reason_for({"is_recording": True, "is_reanalyzing": False}) == "recording"


def test_a_reanalysis_is_busy():
    """The regression. This used to come back as idle."""
    assert _busy_reason_for({"is_recording": False, "is_reanalyzing": True}) == "reanalyzing"


def test_recording_wins_when_both_are_set():
    assert _busy_reason_for({"is_recording": True, "is_reanalyzing": True}) == "recording"


def test_a_missing_flag_is_not_busy():
    assert _busy_reason_for({"is_recording": False}) == ""


# ── The routes that consume it ──────────────────────────────────────────────

def _body(name: str) -> str:
    """The source of one top-level function in app.py."""
    tree = ast.parse(APP_PY)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"app.py no longer defines {name}"
    return ast.get_source_segment(APP_PY, fn) or ""


def test_the_handshake_asks_for_the_busy_reason():
    src = _body("instance_handshake")
    assert "_busy_reason()" in src, "the handshake is back to reading is_recording alone"
    assert '"busy"' in src and '"reason"' in src, "the reply must carry busy/reason"


def test_the_handshake_still_reports_recording_for_older_instances():
    """An instance built before this change reads only "recording"; it must not
    start seeing True for a reanalysis, or it prints the wrong reason."""
    src = _body("instance_handshake")
    assert 'recording = reason == "recording"' in src


def test_shutdown_refuses_during_a_reanalysis():
    src = _body("shutdown")
    assert '"reanalyzing"' in src, "shutdown no longer guards a reanalysis"
    assert "409" in src, "a refused shutdown should say so with a status"
    assert 'data.get("force")' in src, "there must be a way to override it"


def test_the_requester_aborts_on_busy():
    src = _body("_handshake_existing_instance")
    assert 'data.get("busy")' in src, "the new instance ignores the busy flag"
    assert "return False" in src


@pytest.mark.parametrize("fn_name", ["_force_quit", "restart"])
def test_an_intentional_exit_rolls_a_reanalysis_back(fn_name):
    """os._exit kills the daemon worker mid-rebuild, so the exit paths put the
    transcript back rather than leaving the meeting empty."""
    src = _body(fn_name)
    assert "_rollback_reanalysis" in src, (
        f"{fn_name} can still exit with a transcript deleted and not rebuilt")
