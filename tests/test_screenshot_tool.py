"""The chat's get_screenshot tool reads the transcript's clock times.

Report (2026-10-27): asked to grab screenshots of the slides, the model called
get_screenshot with "2:45", the time as the transcript shows it. The tool ran
float() on it, the error ended the whole answer ("could not convert string to
float: '2:45'"), and the tool row read "at NaNs". Both providers' tool paths
now read clock times, and a timestamp that can't be read goes back to the model
as a tool error instead of ending the chat.

Run: .venv/Scripts/python -m pytest tests/test_screenshot_tool.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ai.assistant import AIAssistant, _screenshot_seconds

ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("value, seconds", [
    (165, 165.0),
    (165.5, 165.5),
    ("165", 165.0),
    ("165s", 165.0),
    ("2:45", 165.0),
    ("[2:45]", 165.0),
    ("125:30", 7530.0),      # the transcript's minutes run past 59
    ("1:02:45", 3765.0),
    ("0:05.5", 5.5),
])
def test_seconds_and_clock_times_are_read(value, seconds):
    assert _screenshot_seconds(value) == seconds


@pytest.mark.parametrize("value", ["", "soon", "2:xx", "-5", "nan", None])
def test_unreadable_timestamps_are_refused(value):
    assert _screenshot_seconds(value) is None


def _extractor(calls):
    def fe(ts):
        calls.append(ts)
        return (b"jpeg", "/screenshots/x.jpg")
    return fe


def test_anthropic_takes_a_clock_time():
    calls, events = [], []
    out = AIAssistant.__new__(AIAssistant)._execute_tool_anthropic(
        {"id": "t1", "name": "get_screenshot", "input": {"timestamp": "2:45"}},
        _extractor(calls), None, lambda kind, payload: events.append((kind, payload)),
    )
    assert calls == [165.0]
    assert not out.get("is_error")
    assert "Screenshot at 165.0s captured." in out["content"][0]["text"]
    assert events[-1][1]["success"] is True


def test_anthropic_returns_an_unreadable_timestamp_to_the_model():
    calls, events = [], []
    out = AIAssistant.__new__(AIAssistant)._execute_tool_anthropic(
        {"id": "t1", "name": "get_screenshot", "input": {"timestamp": "the end"}},
        _extractor(calls), None, lambda kind, payload: events.append((kind, payload)),
    )
    assert calls == []
    assert out["is_error"] and "the end" in out["content"]
    assert events[-1][1]["success"] is False


def test_openai_takes_a_clock_time():
    calls, msgs = [], []
    AIAssistant.__new__(AIAssistant)._execute_tool_openai(
        "c1", "get_screenshot", json.dumps({"timestamp": "1:02:45"}), msgs,
        _extractor(calls), None, None,
    )
    assert calls == [3765.0]
    assert msgs[0]["output"].startswith("Screenshot at 3765.0s captured.")


def test_openai_returns_an_unreadable_timestamp_to_the_model():
    calls, msgs = [], []
    AIAssistant.__new__(AIAssistant)._execute_tool_openai(
        "c1", "get_screenshot", json.dumps({"timestamp": "later"}), msgs,
        _extractor(calls), None, None,
    )
    assert calls == []
    assert len(msgs) == 1 and "later" in msgs[0]["output"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_the_tool_row_shows_the_time_it_was_given():
    start = APP_JS.index("function _toolInputSummary(")
    fn = APP_JS[start:APP_JS.index("\n}\n", start)] + "\n}"
    script = "\n".join([
        fn,
        "console.log(JSON.stringify([",
        "  _toolInputSummary('get_screenshot', { timestamp: 165 }),",
        "  _toolInputSummary('get_screenshot', { timestamp: '2:45' }),",
        "]));",
    ])
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["at 165.0s", "at 2:45"]
