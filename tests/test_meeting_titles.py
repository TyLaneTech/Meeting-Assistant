"""Meeting titles: the calendar's name first, then a title from what was said.

Regression 2026-10-07: a 1 h 49 m leadership update matched its Outlook event
"P&C Leadership" with 97% confidence, yet was titled "PMO Planning Session".
The title step never looked at the calendar, read only the first 1,000
characters (small talk), and was shown an unrelated August meeting's AI title
as the user's naming style, which it copied because the same people attended.

Run: .venv/Scripts/python -m pytest tests/test_meeting_titles.py
"""
from __future__ import annotations

import ast
from pathlib import Path

from ai.assistant import AIAssistant
from core import paths, storage

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")


# ── What the title model reads ──────────────────────────────────────────────

def test_a_short_transcript_is_read_whole():
    text = "hello " * 200
    assert AIAssistant._title_excerpt(text) == text.strip()


def test_a_long_transcript_is_sampled_from_start_to_end():
    lines = [f"line {k:05d} about topic {k // 1000}" for k in range(6000)]
    text = "\n".join(lines)
    excerpt = AIAssistant._title_excerpt(text)
    budget = (AIAssistant._TITLE_OPENING
              + AIAssistant._TITLE_WINDOWS * (AIAssistant._TITLE_WINDOW + len("\n[...]\n")))
    assert len(excerpt) <= budget
    assert excerpt.startswith("line 00000")
    assert "topic 5" in excerpt, "the end of the meeting is in the excerpt"
    assert "topic 2" in excerpt and "topic 3" in excerpt


# ── Past titles offered as the user's naming style ──────────────────────────

def test_only_titles_the_user_chose_are_offered_as_their_style(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    guessed = storage.create_session("PMO Planning Session")      # an AI title
    storage.update_session_title(guessed, "PMO Planning Session", user_set=False)
    chosen = storage.create_session("Weekly Carrier Sync")
    storage.update_session_title(chosen, "Weekly Carrier Sync", user_set=True)
    now = storage.create_session("Meeting 2026-10-07 20:31")
    titles = [m["title"] for m in
              storage.get_title_generation_context(now)["similar_past_meetings"]]
    assert "Weekly Carrier Sync" in titles
    assert "PMO Planning Session" not in titles


# ── Calendar first, then the AI ─────────────────────────────────────────────

class _Calendar:
    def __init__(self, title):
        self.title, self.asked = title, []

    def title_for_start(self, start, end):
        self.asked.append((start, end))
        return self.title


class _AI:
    def __init__(self, fits=True):
        self.calls, self.fits = 0, fits

    def calendar_fits(self, subject, transcript):
        return self.fits

    def generate_title(self, transcript, context=None, system_prompt=None):
        self.calls += 1
        return "Innovation Strategy Update"


class _Log:
    @staticmethod
    def info(*a):
        pass


def _meeting_title(calendar, ai):
    tree = ast.parse(APP_PY)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_meeting_title")
    session = {"started_at": "2026-10-07T20:31:28", "ended_at": "2026-10-07T22:21:00"}
    scope = {
        "storage": type("S", (), {
            "get_session": staticmethod(lambda sid: session),
            "get_title_generation_context": staticmethod(lambda sid: {}),
        }),
        "calendar_sync": calendar, "ai": ai, "log": _Log,
        "settings": type("P", (), {"get": staticmethod(lambda k, d=None: d)}),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<app._meeting_title>", "exec"), scope)
    return scope["_meeting_title"]("s1", "transcript")


def test_a_confident_calendar_match_names_the_meeting():
    cal, ai = _Calendar("P&C Leadership"), _AI()
    assert _meeting_title(cal, ai) == ("P&C Leadership", True)
    assert ai.calls == 0
    assert cal.asked == [("2026-10-07T20:31:28", "2026-10-07T22:21:00")], \
        "the match uses the real start and end, after the meeting"


def test_no_calendar_match_falls_back_to_a_title_from_the_content():
    cal, ai = _Calendar(""), _AI()
    assert _meeting_title(cal, ai) == ("Innovation Strategy Update", False)
    assert ai.calls == 1


def test_a_call_taken_during_a_scheduled_slot_is_titled_from_what_was_said():
    # 2026-10-07: a 3-minute deal call overlapped "Proposal tool Presentation".
    cal, ai = _Calendar("Proposal tool Presentation"), _AI(fits=False)
    assert _meeting_title(cal, ai) == ("Innovation Strategy Update", False)


def test_when_the_ai_cannot_judge_the_calendar_name_stands():
    cal, ai = _Calendar("P&C Leadership"), _AI(fits=None)
    assert _meeting_title(cal, ai) == ("P&C Leadership", True)
    assert ai.calls == 0
