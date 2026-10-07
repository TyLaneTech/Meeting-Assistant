"""Record stays live on the page of a meeting being transcribed after it ended.

The server lets a recording start during the after-meeting transcription pass
(the start pauses the pass, which resumes when the recording ends), but the
page of that meeting saw a reanalysis on its own session and held the Record
button as it does for a manual reanalysis: Record was disabled there and only
there. The reanalysis_start event now says which kind of pass it is.

app.py is never imported here (that loads the transcription model): the gate is
lifted out of the source and run against stand-ins, and the page's side is
checked at source level.

Run: .venv/Scripts/python -m pytest tests/test_post_meeting_record_button.py
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")


class _Queue:
    def __init__(self, running):
        self.running_session = running


def _prereqs(state: dict, running):
    tree = ast.parse(APP_PY)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_recording_prereqs_locked")
    scope: dict = {"_state": state, "_post_meeting": _Queue(running),
                   "_state_lock": threading.RLock()}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<app._recording_prereqs_locked>",
                 "exec"), scope)
    return scope["_recording_prereqs_locked"]()


def _js_function(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    return APP_JS[start:APP_JS.index("\n}\n", start)]


def _js_listener(event: str) -> str:
    start = APP_JS.index(f"src.addEventListener('{event}'")
    return APP_JS[start:APP_JS.index("\n  });", start)]


# ── The server: what the page relies on ───────────────────────────────────────

def test_the_server_allows_a_start_during_the_after_meeting_pass():
    ok, _ = _prereqs({"is_reanalyzing": True, "session_id": "s1"}, running="s1")
    assert ok


def test_the_server_still_holds_a_start_during_a_manual_reanalysis():
    ok, reason = _prereqs({"is_reanalyzing": True, "session_id": "s1"}, running=None)
    assert not ok and "Reanalysis" in reason


def test_the_start_event_says_which_kind_of_pass_it_is():
    assert re.search(r'_push\("reanalysis_start", \{"session_id": session_id, '
                     r'"post_meeting": post_meeting\}\)', APP_PY)


# ── The page ──────────────────────────────────────────────────────────────────

def test_the_page_tracks_the_kind_of_pass():
    assert "state.isPostMeetingPass = !!d.post_meeting;" in _js_listener("reanalysis_start")
    for event in ("reanalysis_done", "reanalysis_error"):
        assert "state.isPostMeetingPass = false;" in _js_listener(event), event


def test_only_a_manual_reanalysis_holds_the_record_button():
    assert "return state.isReanalyzing && !state.isPostMeetingPass;" in \
        _js_function("_reanalysisHoldsRecord")
    sync = _js_function("_syncRecordBtnDisabled")
    assert "_reanalysisHoldsRecord()" in sync and "state.isReanalyzing" not in sync
    preparing = next(line for line in APP_JS.splitlines() if "const preparing =" in line)
    assert "_reanalysisHoldsRecord()" in preparing and "state.isReanalyzing" not in preparing


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_the_button_follows_the_rule():
    """Run the page's own two functions against each state."""
    script = "\n".join([
        "const btn = { disabled: null };",
        "const document = { getElementById: () => btn };",
        "let state;",
        _js_function("_reanalysisHoldsRecord") + "\n}",
        _js_function("_syncRecordBtnDisabled") + "\n}",
        "const out = [];",
        "for (const [isReanalyzing, isPostMeetingPass] of "
        "[[true, true], [true, false], [false, false]]) {",
        "  state = { isRecording: false, isStartingRecording: false, recordingReady: true,",
        "            isReanalyzing, isPostMeetingPass };",
        "  _syncRecordBtnDisabled();",
        "  out.push(btn.disabled);",
        "}",
        "console.log(JSON.stringify(out));",
    ])
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    # after-meeting pass: live; manual reanalysis: held; nothing running: live
    assert result.stdout.strip() == "[false,true,false]"
