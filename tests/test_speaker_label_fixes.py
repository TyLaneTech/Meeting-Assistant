"""Speaker label and voice-library fixes found while planning AI speaker
detection (2026-10-08).

- A voice match on a key new to the meeting named the speaker without linking
  it: the link was an UPDATE on a label row that did not exist yet (13 of 370
  named speakers in one library). link_session_speaker now creates the row.
- Unlinking a speaker (Cleanup's empty group, the Agent API's reset) cleared
  the link but kept the old name, so the meeting still showed the person it
  had just been told was not this speaker.
- Training audio and Cleanup read a line's original speaker, so a line moved
  to another speaker trained the wrong profile and was listed under the wrong
  speaker.
- A deleted meeting left its unnamed speakers' voice vectors behind.
- The UI's profile merge accepted the Me profile.

Run: .venv/Scripts/python -m pytest tests/test_speaker_label_fixes.py
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from core import paths, storage
from ml.speaker_db import SpeakerFingerprintDB

ROOT = Path(__file__).parents[1]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A fingerprint DB on a temp file, with no embedding model loaded."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    fp = SpeakerFingerprintDB.__new__(SpeakerFingerprintDB)
    fp._db_path = paths.db_path()
    fp._ready = False
    fp._inference = None
    fp._me_id = None
    return fp


def _meeting() -> str:
    sid = storage.create_session("Brillian Integration Intro")
    for i in range(4):
        storage.save_segment(sid, f"line {i}", f"Speaker {i % 2 + 1}",
                             float(i * 10), float(i * 10 + 9))
    return sid


def _labels(sid: str) -> dict:
    return {sp["speaker_key"]: sp for sp in storage.list_speaker_profiles(sid)}


def test_linking_a_key_with_no_label_row_creates_it(db):
    sid = _meeting()
    gid = db.create_global_speaker("Tom McDonnell", color="#2dd4bf")
    db.link_session_speaker(sid, "Speaker 2", gid)        # no label row yet
    assert db.get_link(sid, "Speaker 2") == gid
    assert _labels(sid)["Speaker 2"]["name"] == "Tom McDonnell"


def test_the_auto_apply_order_now_keeps_both_the_name_and_the_link(db):
    # _auto_apply_fingerprint links first and saves the label second.
    sid = _meeting()
    gid = db.create_global_speaker("Tom McDonnell", color="#2dd4bf")
    db.link_session_speaker(sid, "Speaker 1", gid)
    storage.save_speaker_label(sid, "Speaker 1", name="Tom McDonnell", color="#2dd4bf")
    assert db.get_link(sid, "Speaker 1") == gid


def test_linking_an_existing_row_keeps_its_name(db):
    sid = _meeting()
    storage.save_speaker_label(sid, "Speaker 1", name="Tom", color="#111111")
    gid = db.create_global_speaker("Tom McDonnell")
    db.link_session_speaker(sid, "Speaker 1", gid)
    assert _labels(sid)["Speaker 1"]["name"] == "Tom"
    assert db.get_link(sid, "Speaker 1") == gid


def test_unlinking_puts_the_speaker_back_to_its_key(db):
    sid = _meeting()
    gid = db.create_global_speaker("Antonio Debouse", color="#e3b341")
    db.apply_cluster_corrections(
        sid, [{"global_id": gid, "member_keys": ["Speaker 1"]}], noise_keys=[])
    assert _labels(sid)["Speaker 1"]["name"] == "Antonio Debouse"

    db.apply_cluster_corrections(sid, [{"global_id": None, "member_keys": ["Speaker 1"]}],
                                 noise_keys=[])
    row = _labels(sid)["Speaker 1"]
    assert row["name"] == "Speaker 1", "an unlinked speaker no longer carries the old name"
    assert row["color"] is None
    assert db.get_link(sid, "Speaker 1") is None


def test_unlinking_a_custom_speaker_keeps_the_name_the_user_typed(db):
    sid = _meeting()
    gid = db.create_global_speaker("Caleb Smith")
    storage.save_speaker_label(sid, "custom:abc12345", name="Caleb Smith")
    db.link_session_speaker(sid, "custom:abc12345", gid)
    db.apply_cluster_corrections(sid, [{"global_id": None, "member_keys": ["custom:abc12345"]}],
                                 noise_keys=[])
    assert _labels(sid)["custom:abc12345"]["name"] == "Caleb Smith"
    assert db.get_link(sid, "custom:abc12345") is None


def test_a_moved_line_trains_and_lists_under_the_speaker_it_moved_to(db):
    sid = _meeting()
    segs = storage.get_segments_by_speaker(sid, "Speaker 1")
    moved = segs[0]["id"]
    storage.save_segment_source_override(moved, "Speaker 2")

    assert moved not in [s["id"] for s in storage.get_segments_by_speaker(sid, "Speaker 1")]
    assert moved in [s["id"] for s in storage.get_segments_by_speaker(sid, "Speaker 2")]

    gathered = {sp["speaker_key"]: sp for sp in db._gather_session_speakers(sid)}
    assert moved in [s["id"] for s in gathered["Speaker 2"]["segments"]]
    assert moved not in [s["id"] for s in gathered["Speaker 1"]["segments"]]


def test_a_line_moved_to_a_new_speaker_makes_that_speaker_appear(db):
    sid = _meeting()
    moved = storage.get_segments_by_speaker(sid, "Speaker 1")[0]["id"]
    storage.save_segment_source_override(moved, "custom:9f8e7d6c")
    gathered = {sp["speaker_key"] for sp in db._gather_session_speakers(sid)}
    assert "custom:9f8e7d6c" in gathered


def test_deleting_a_meeting_drops_its_unnamed_speakers_voice_vectors(db):
    sid = _meeting()
    other = _meeting()
    vec = np.ones(256, dtype=np.float32)
    db.add_unlabeled_embedding(sid, "Speaker 1", vec, 3.0)
    db.add_unlabeled_embedding(other, "Speaker 1", vec, 3.0)
    storage.delete_session(sid)
    assert db.get_unlabeled_embeddings(sid) == []
    assert len(db.get_unlabeled_embeddings(other)) == 1, "other meetings keep theirs"


# ── The UI's profile merge refuses the Me profile ──────────────────────────

def _merge_route(me_id: str | None):
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    wanted = {"_me_profile_id", "fp_merge_speaker"}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    for fn in fns:
        fn.decorator_list = []
    merged = []

    class _Req:
        payload: dict = {}

        @classmethod
        def get_json(cls, silent=False):
            return cls.payload

    scope = {
        "fingerprint_db": type("FP", (), {"ready": True, "_me_id": me_id})(),
        "settings": type("S", (), {"get": staticmethod(lambda k, d=None: None)}),
        "request": _Req,
        "jsonify": lambda d: d,
        "_fp_unavailable": lambda: ({"error": "unavailable"}, 503),
        "_apply_profile_merge": lambda target, source: merged.append((target, source)),
    }
    exec(compile(ast.Module(body=fns, type_ignores=[]), "<app merge>", "exec"), scope)
    return scope["fp_merge_speaker"], _Req, merged


def test_the_me_profile_cannot_be_merged_either_way():
    route, req, merged = _merge_route("me-123")
    req.payload = {"source_id": "me-123"}
    assert route("tom-1")[1] == 403
    req.payload = {"source_id": "tom-1"}
    assert route("me-123")[1] == 403
    assert merged == []


def test_other_profiles_still_merge():
    route, req, merged = _merge_route("me-123")
    req.payload = {"source_id": "tom-2"}
    assert route("tom-1") == {"ok": True}
    assert merged == [("tom-1", "tom-2")]
