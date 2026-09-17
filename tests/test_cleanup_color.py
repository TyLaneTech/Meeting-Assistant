"""Applying a staged colour from the Speakers window actually writes it.

The Cleanup payload has always carried a `color` per group, but
``apply_cluster_corrections`` only used it when creating a brand new profile:
for a group already linked to a Voice Library profile it read the colour back
out of that profile's row, so the colour the user picked was written over with
the one they were trying to change. That mattered the moment the colour picker
moved off the retired Manage tab and onto the group headers (2026-09-17).

Run: .venv/Scripts/python.exe -m pytest tests/test_cleanup_color.py -q
"""
from __future__ import annotations

import pytest

from core import paths, storage
from ml.speaker_db import SpeakerFingerprintDB


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
    sid = storage.create_session("Website Colour Review")
    for i in range(4):
        storage.save_segment(sid, f"line {i}", f"Speaker {i % 2 + 1}",
                             float(i * 10), float(i * 10 + 9))
    return sid


def test_a_staged_colour_reaches_the_linked_profile(db):
    sid = _meeting()
    gid = db.create_global_speaker("Brent Meyer", color="#2dd4bf")
    db.apply_cluster_corrections(
        sid,
        [{"global_id": gid, "new_name": None, "color": "#2dd4bf",
          "member_keys": ["Speaker 1"]}],
        noise_keys=[],
    )
    assert db.get_global_speaker(gid)["color"] == "#2dd4bf"

    # Restage the same group with a different colour, exactly as the picker does.
    db.apply_cluster_corrections(
        sid,
        [{"global_id": gid, "new_name": None, "color": "#f0883e",
          "member_keys": ["Speaker 1"]}],
        noise_keys=[],
    )
    assert db.get_global_speaker(gid)["color"] == "#f0883e", \
        "the profile keeps the colour the user picked"

    labels = {sp["speaker_key"]: sp for sp in storage.list_speaker_profiles(sid)}
    assert labels["Speaker 1"]["color"] == "#f0883e", \
        "the meeting's own speaker row follows the profile"


def test_the_recolour_rebuilds_the_profile_centroid(db):
    """A recolour marks the profile touched, so pass 4 does not skip it."""
    sid = _meeting()
    gid = db.create_global_speaker("Tana Taylor", color="#d2a8ff")
    result = db.apply_cluster_corrections(
        sid,
        [{"global_id": gid, "new_name": None, "color": "#db61a2",
          "member_keys": ["Speaker 2"]}],
        noise_keys=[],
    )
    assert gid in result["profiles_touched"]


def test_a_new_group_is_created_with_the_colour_it_was_given(db):
    sid = _meeting()
    result = db.apply_cluster_corrections(
        sid,
        [{"global_id": None, "new_name": "Antonio Debouse", "color": "#e3b341",
          "member_keys": ["Speaker 1", "Speaker 2"]}],
        noise_keys=[],
    )
    assert len(result["created"]) == 1
    gid = result["created"][0]["global_id"]
    assert db.get_global_speaker(gid)["color"] == "#e3b341"


def test_an_unchanged_colour_does_not_rewrite_the_profile(db, monkeypatch):
    """Every Apply carries the current colour along for the ride, and
    rename_global_speaker rewrites every label row that profile owns across
    every meeting, so only a real change may reach it."""
    sid = _meeting()
    gid = db.create_global_speaker("Brent Meyer", color="#2dd4bf")

    renames = []
    original = db.rename_global_speaker
    monkeypatch.setattr(
        db, "rename_global_speaker",
        lambda *a, **kw: (renames.append((a, kw)), original(*a, **kw))[1],
    )

    db.apply_cluster_corrections(
        sid,
        [{"global_id": gid, "new_name": None, "color": "#2dd4bf",
          "member_keys": ["Speaker 1"]}],
        noise_keys=[],
    )
    assert renames == []

    db.apply_cluster_corrections(
        sid,
        [{"global_id": gid, "new_name": None, "color": "#f0883e",
          "member_keys": ["Speaker 1"]}],
        noise_keys=[],
    )
    assert len(renames) == 1, "a real change goes through exactly once"


def test_a_group_with_no_colour_keeps_the_profile_s_own(db):
    """The picker leaves `color` null for a group that never had one staged."""
    sid = _meeting()
    gid = db.create_global_speaker("Brent Meyer", color="#2dd4bf")
    db.apply_cluster_corrections(
        sid,
        [{"global_id": gid, "new_name": None, "color": None,
          "member_keys": ["Speaker 1"]}],
        noise_keys=[],
    )
    assert db.get_global_speaker(gid)["color"] == "#2dd4bf"
