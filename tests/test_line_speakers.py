"""A name given to single transcript lines is a speaker of the meeting.

Report 2026-10-12: lines renamed with Ctrl+click (Reassign to) showed their new
name in the transcript, but the chat model never listed that person among the
meeting's speakers. The roster was built from each line's raw source key, so a
name held only by line overrides (or by a custom key that exists only as a
source_override) was missing, and a key whose every line had moved stayed.
And the meeting last recorded or reanalysed is the app's active session, whose
chat read the lines held in _state as they were transcribed: no rename made
afterwards reached the model until a restart.

app.py is never imported (that loads the models): the functions are lifted out
of the source and run against stand-ins or a temporary database.

Run: .venv/Scripts/python -m pytest tests/test_line_speakers.py
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from core import paths, storage

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")

_SOURCE_LABELS = {"loopback": "Desktop", "mic": "Mic", "both": "Desktop+Mic", "me": "Me"}


def _lift(*names: str, **scope) -> dict:
    tree = ast.parse(APP_PY)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {f.name for f in fns} == set(names)
    scope = {"_SOURCE_LABELS": _SOURCE_LABELS, "_NOISE_LABEL": "[Noise]", **scope}
    exec(compile(ast.Module(body=fns, type_ignores=[]), "<app>", "exec"), scope)
    return scope


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    yield tmp_path


def _line(source, text, t, **extra):
    return {"source": source, "text": text, "start_time": t, "end_time": t + 2.0, **extra}


LABELS = {"Speaker 1": "Dana Whitfield", "Speaker 3": "Old Name", "custom:1": "Kim Park"}
LINES = [
    _line("Speaker 1", "Morning all.", 1.0),
    _line("Speaker 2", "Hi, I am new here.", 4.0, label_override="Visitor"),
    _line("Speaker 2", "Still unnamed.", 7.0),
    _line("Speaker 2", "Moved to a new name.", 10.0, source_override="custom:1",
          label_override="Kim Park"),
    _line("Speaker 3", "Every line of mine moved.", 13.0, source_override="Speaker 1"),
    _line("Speaker 4", "um", 16.0, label_override="[Noise]"),
    _line("mic", "That's me.", 19.0),
]


def test_the_chat_roster_lists_every_name_the_lines_show():
    app = _lift("_segment_speaker", "_fmt_segment", "_fmt_time", "_build_session_meta")
    meta = app["_build_session_meta"](LINES, LABELS)
    assert meta["speakers"] == ["Dana Whitfield", "Visitor", "Speaker 2", "Kim Park", "Mic"]
    # The roster and the lines the model reads agree.
    shown = [app["_fmt_segment"](s, LABELS) for s in LINES]
    assert shown[1] == "[0:04] [Visitor] Hi, I am new here."
    assert shown[3] == "[0:10] [Kim Park] Moved to a new name."
    assert shown[4] == "[0:13] [Dana Whitfield] Every line of mine moved."


def test_the_reassign_field_keeps_focus_while_the_selection_grows():
    # Ctrl/Shift+clicking more speaker pills left the open Reassign picker
    # without focus, so the name could not be typed straight away.
    app_js = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
    picker = app_js[app_js.index("function _openBulkSpeakerPicker("):]
    already_open = picker[:picker.index("document.querySelector('.speaker-picker')?.remove()")]
    assert "_bulkPicker.input.focus();" in already_open
    # The pill's mousedown does not take focus from the field in the first place.
    outside = app_js[app_js.index("function _bulkPickerOutside("):]
    outside = outside[:outside.index("\n}")]
    grow = outside[outside.index("e.target.closest('.src-badge')"):]
    assert grow.index("e.preventDefault();") < grow.index("return;")


def _live(sid, lines):
    return {"session_id": sid, "segments": lines,
            "speaker_labels": {"Speaker 1": "Dana Whitfield"}}


def test_ai_context_for_the_active_meeting_reads_the_saved_speakers(data):
    sid = storage.create_session("Standup")
    ids = [storage.save_segment(sid, t, "Speaker 1", s, s + 2.0)
           for t, s in (("One.", 1.0), ("Two.", 4.0), ("Three.", 7.0))]
    storage.save_speaker_label(sid, "Speaker 1", name="Dana Whitfield")
    # In memory, as transcribed.
    lines = [{"text": t, "source": "Speaker 1", "start_time": s, "end_time": s + 2.0,
              "_seg_id": i} for i, (t, s) in zip(ids, (("One.", 1.0), ("Two.", 4.0), ("Three.", 7.0)))]
    # Afterwards, in the database: one line renamed on its own, one moved to a
    # new key typed in Reassign.
    storage.save_segment_label_override(ids[1], "Visitor")
    storage.save_segment_label_override(ids[2], "Kim Park")
    storage.save_segment_source_override(ids[2], "custom:9")
    storage.save_speaker_label(sid, "custom:9", name="Kim Park")

    state = _live(sid, lines)
    app = _lift("_saved_speakers_if_active", "_live_lines", "_segment_speaker", "_fmt_time",
                "_build_session_meta", storage=storage, _state=state,
                _state_lock=threading.Lock(), log=None)
    saved = app["_saved_speakers_if_active"](sid)
    segments, labels = app["_live_lines"](saved)
    assert [s.get("label_override") for s in segments] == [None, "Visitor", "Kim Park"]
    assert segments[2]["source_override"] == "custom:9" and segments[2]["_seg_id"] == ids[2]
    assert labels["custom:9"] == "Kim Park"
    assert app["_build_session_meta"](segments, labels)["speakers"] == [
        "Dana Whitfield", "Visitor", "Kim Park"]
    # _state itself is left as it was.
    assert "label_override" not in lines[1]

    # Any other meeting reads nothing, and _state is used as it is.
    assert app["_saved_speakers_if_active"]("someone-else") is None
    assert app["_live_lines"](None)[0] == lines


def test_every_ai_context_of_the_active_meeting_lays_the_saved_speakers_over():
    # Chat, the summary refresh after a speaker change, Summarize and chapters.
    for fn in ("chat", "_speaker_summary_args", "summarize", "api_generate_chapters"):
        body = APP_PY[APP_PY.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 1)]
        assert "saved = _saved_speakers_if_active(session_id)" in body, fn
        assert "_live_lines(saved)" in body, fn
        assert 'list(_state["segments"])' not in body, fn


def test_renaming_a_line_updates_the_open_meetings_lines():
    class _Storage:
        def save_segment_label_override(self, seg_id, label): pass
        def save_segment_source_override(self, seg_id, key): pass
        def get_segment(self, seg_id): return None

    live = [{"text": "a", "source": "Speaker 1", "_seg_id": 6},
            {"text": "b", "source": "Speaker 1", "_seg_id": 7, "source_override": "Speaker 2"}]
    app = _lift("_relabel_segment", storage=_Storage(), _state={"segments": live},
                _state_lock=threading.Lock(), _maybe_update_live_redirect=lambda *a: None,
                fingerprint_db=type("F", (), {"ready": False})())
    app["_relabel_segment"](7, "Jenny", "custom:5")
    assert live[1]["label_override"] == "Jenny" and live[1]["source_override"] == "custom:5"
    app["_relabel_segment"](7, "Visitor", None)
    assert live[1]["label_override"] == "Visitor" and "source_override" not in live[1]
    assert "label_override" not in live[0]


def _meeting(title="Planning"):
    sid = storage.create_session(title)
    ids = {}
    for name, source, text, t in (
        ("dana", "Speaker 1", "Let's start.", 1.0),
        ("visitor", "Speaker 1", "Hello, I'm visiting.", 4.0),
        ("same", "Speaker 1", "Same name, own label.", 7.0),
        ("moved", "Speaker 3", "All of Speaker 3 moved.", 10.0),
        ("noise", "Speaker 4", "um", 13.0),
        ("noisy", "Speaker 5", "hm", 16.0),
    ):
        ids[name] = storage.save_segment(sid, text, source, t, t + 3.0)
    storage.save_speaker_label(sid, "Speaker 1", name="Dana Whitfield", color="#123456")
    storage.save_speaker_label(sid, "Speaker 3", name="Stale Name")
    storage.put_speaker_label_rows(sid, {"Speaker 5": {"name": "Hidden Person", "is_noise": 1}})
    storage.save_segment_label_override(ids["visitor"], "Visitor")
    storage.save_segment_label_override(ids["same"], "dana whitfield")
    storage.save_segment_source_override(ids["moved"], "Speaker 1")
    storage.save_segment_label_override(ids["noise"], "[Noise]")
    return sid


def test_meeting_lists_show_the_names_the_lines_carry(data):
    sid = _meeting()
    speakers = storage.get_sessions_meta([sid])[sid]["speakers"]
    # A line's own name counts; a label with no lines left, noise, and a
    # speaker marked as noise do not.
    assert speakers == ["Dana Whitfield", "Visitor"]


def test_the_participant_filter_and_search_find_a_name_given_to_lines(data):
    sid = _meeting()
    other = storage.create_session("Other")
    storage.save_segment(other, "Nothing here.", "Speaker 1", 0.0, 2.0)
    assert storage.list_session_ids(speaker="visit") == [sid]
    assert storage.list_session_ids(speaker="Dana") == [sid]
    hits = storage.search_speakers("Visit")
    assert [h["session_id"] for h in hits] == [sid]
    assert hits[0]["matches"][0]["snippet"] == "<mark>Visit</mark>or"


def test_agent_meeting_speakers_give_a_lines_own_name_a_row(data):
    sid = _meeting()
    rows = {(r["speaker_key"], r["name"]): r for r in storage.speaker_time_stats(sid, per_line=True)}
    dana = rows[("Speaker 1", "Dana Whitfield")]
    visitor = rows[("Speaker 1", "Visitor")]
    # The line labelled with the speaker's own name (any case) stays in its row.
    assert dana["segment_count"] == 3 and dana["per_line"] is False
    assert dana["color"] == "#123456"
    assert visitor["segment_count"] == 1 and visitor["per_line"] is True
    assert visitor["color"] is None and visitor["global_id"] is None
    # Without per_line nothing changes for the other callers.
    plain = storage.speaker_time_stats(sid)
    assert all("per_line" not in r for r in plain)
    assert next(r for r in plain if r["speaker_key"] == "Speaker 1")["segment_count"] == 4
    # The Agent API's meeting bundle asks for the split.
    rest = (ROOT / "agent_api/rest.py").read_text(encoding="utf-8")
    assert "storage.speaker_time_stats(session_id, per_line=True)" in rest
