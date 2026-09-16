"""Reanalysis deletes a transcript before it has a replacement.

``_run_reanalysis`` clears the session's segments and speaker rows, then
rebuilds them from the audio. Anything that interrupted the rebuild used to
leave the meeting empty, permanently: the delete was a hard DELETE with no
backup and the worker is a daemon thread. These cover the guard that makes the
delete reversible, and the snapshot/restore pair it is built on.

Run: .venv/Scripts/python.exe -m pytest tests/test_reanalysis_rollback.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import paths, reanalysis_guard, storage


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    yield tmp_path


def _meeting(segments=3) -> str:
    sid = storage.create_session("Website Color Review")
    for i in range(segments):
        storage.save_segment(sid, f"line {i}", f"Speaker {i % 2 + 1}",
                             float(i * 10), float(i * 10 + 9))
    storage.save_speaker_label(sid, "Speaker 1", name="Dana", color="#ff0000")
    storage.save_speaker_label(sid, "Speaker 2", name="Kim")
    return sid


# ── The snapshot / restore pair ─────────────────────────────────────────────

def test_restore_puts_back_exactly_what_reset_deletes(data):
    sid = _meeting(segments=5)
    before = storage.get_session(sid)
    snapshot = storage.snapshot_session_transcript(sid)

    storage.reset_session_transcript(sid)
    assert storage.get_session(sid)["segments"] == [], "reset should empty it"

    storage.restore_session_transcript(sid, snapshot)
    after = storage.get_session(sid)

    assert len(after["segments"]) == 5
    assert [s["text"] for s in after["segments"]] == [s["text"] for s in before["segments"]]
    assert [s["source"] for s in after["segments"]] == [s["source"] for s in before["segments"]]
    assert [s["start_time"] for s in after["segments"]] == [s["start_time"] for s in before["segments"]]
    assert after["speaker_labels"] == before["speaker_labels"]
    assert after["speaker_profiles"] == before["speaker_profiles"]


def test_restore_reindexes_search(data):
    """A restored transcript has to be findable again, or search silently
    forgets the meeting. reset_session_transcript drops the FTS rows, so the
    restore has to put them back too."""
    sid = _meeting(segments=3)
    snapshot = storage.snapshot_session_transcript(sid)

    storage.reset_session_transcript(sid)
    gone = storage.search_sessions("line")
    assert not any(h["session_id"] == sid for h in gone), (
        "reset should have dropped the segment index")

    storage.restore_session_transcript(sid, snapshot)
    hits = storage.search_sessions("line")
    found = [h for h in hits if h["session_id"] == sid]
    assert found, "restored text is not searchable"
    assert any(m.get("kind") == "segment" for m in found[0]["matches"]), (
        "the restored segments were not re-indexed")


def test_restore_keeps_the_voice_library_link(data):
    """global_id is the Voice Library link and the session view omits it, which
    is why the snapshot reads the raw rows."""
    sid = _meeting(segments=1)
    with storage._conn() as conn:
        conn.execute("UPDATE speaker_labels SET global_id = ? WHERE session_id = ? "
                     "AND speaker_key = ?", ("gid-123", sid, "Speaker 1"))

    snapshot = storage.snapshot_session_transcript(sid)
    storage.reset_session_transcript(sid)
    storage.restore_session_transcript(sid, snapshot)

    with storage._conn() as conn:
        row = conn.execute("SELECT global_id FROM speaker_labels WHERE session_id = ? "
                           "AND speaker_key = ?", (sid, "Speaker 1")).fetchone()
    assert row["global_id"] == "gid-123", "the profile link was not restored"


# ── The guard file ──────────────────────────────────────────────────────────

def test_guard_marks_a_pass_and_clears_it(data):
    sid = _meeting()
    assert reanalysis_guard.pending_session_ids() == []

    reanalysis_guard.begin(sid, storage.snapshot_session_transcript(sid))
    assert reanalysis_guard.pending_session_ids() == [sid], (
        "a started pass must be detectable after a hard kill")

    reanalysis_guard.clear(sid)
    assert reanalysis_guard.pending_session_ids() == []
    assert reanalysis_guard.load(sid) is None


def test_guard_survives_the_process_and_still_holds_the_transcript(data):
    """The whole point: the snapshot outlives the process that wrote it."""
    sid = _meeting(segments=4)
    reanalysis_guard.begin(sid, storage.snapshot_session_transcript(sid))
    storage.reset_session_transcript(sid)          # the pass deletes, then dies

    recovered = reanalysis_guard.load(sid)
    assert recovered is not None
    assert len(recovered["segments"]) == 4
    storage.restore_session_transcript(sid, recovered)
    assert len(storage.get_session(sid)["segments"]) == 4


def test_guard_does_not_touch_the_trim_backup(data):
    """session-original.json backs the user-visible "restore original" and must
    not be consumed, overwritten or deleted by a reanalysis."""
    sid = _meeting()
    trim = paths.backup_dir() / sid / "session-original.json"
    trim.parent.mkdir(parents=True, exist_ok=True)
    trim.write_text('{"session": {"title": "trimmed"}}', encoding="utf-8")

    reanalysis_guard.begin(sid, storage.snapshot_session_transcript(sid))
    reanalysis_guard.clear(sid)

    assert trim.exists(), "the reanalysis guard deleted the trim backup"
    assert "trimmed" in trim.read_text(encoding="utf-8")


def test_a_corrupt_guard_does_not_raise(data):
    sid = _meeting()
    path = reanalysis_guard.snapshot_path(sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert reanalysis_guard.load(sid) is None, "a corrupt guard must not wedge startup"


def test_pending_ids_is_empty_with_no_backups_folder(data):
    assert reanalysis_guard.pending_session_ids() == []
