"""Deleting a meeting removes every file recorded for it, not just the mixed
WAV and the video: the per-source Opus tracks and temp WAVs, the fragmented
screen capture, leftover video parts from an interrupted merge, and the
calendar candidates file. 19 orphaned files (91 MB) had piled up from deleted
meetings (2026-09-05).
Run with pytest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core import paths, storage


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    return tmp_path


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_delete_session_removes_every_per_session_file(data_dir):
    sid = "11111111-2222-3333-4444-555555555555"
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    audio, video = paths.audio_dir(), paths.video_dir()
    gone = [
        _touch(audio / f"{sid}.wav"),
        _touch(audio / f"{sid}_mic.opus"),
        _touch(audio / f"{sid}_desktop.opus"),
        _touch(audio / f"{sid}_mic.wav"),
        _touch(video / f"{sid}.mp4"),
        _touch(video / f"{sid}.mp4.frag.mp4"),
        _touch(video / f"{sid}_part1.mp4"),
        _touch(video / f"{sid}_concat.txt"),
        _touch(data_dir / "resolution_candidates" / f"{sid}.json"),
        _touch(data_dir / "notes" / sid / "a.png"),
    ]
    kept = [
        _touch(audio / f"{other}.wav"),
        _touch(audio / f"{other}_mic.opus"),
        _touch(video / f"{other}.mp4"),
        _touch(data_dir / "resolution_candidates" / f"{other}.json"),
    ]

    storage.delete_session(sid)

    for p in gone:
        assert not p.exists(), f"left behind: {p.name}"
    for p in kept:
        assert p.exists(), f"wrongly removed: {p.name}"


def test_delete_session_tolerates_missing_files(data_dir):
    storage.delete_session("99999999-0000-0000-0000-000000000000")  # must not raise


@pytest.mark.parametrize("bad_id", ["*", "?*", "[0-9a-f]*", "..", "..\\..", "../..",
                                    "11111111-2222-3333-4444-555555555555*"])
def test_a_malformed_id_touches_no_file(data_dir, bad_id):
    # The leftovers are found by pattern, so "*" as an id deleted every
    # recording in the folder, and an id with "..\" reached outside the data
    # folder (the notes folder is removed with rmtree). Reachable from any local
    # client: DELETE /api/sessions/%2A (review of PR 1086, 2026-10-07).
    a = "11111111-2222-3333-4444-555555555555"
    b = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    audio, video = paths.audio_dir(), paths.video_dir()
    files = [
        _touch(audio / f"{a}.wav"),
        _touch(audio / f"{a}_mic.opus"),
        _touch(audio / f"{b}.opus"),
        _touch(audio / f"{b}_desktop.opus"),
        _touch(video / f"{a}.mp4"),
        _touch(video / f"{b}.mp4.frag.mp4"),
        _touch(data_dir / "notes" / a / "a.png"),
        _touch(data_dir / "keep.json"),
    ]
    storage.delete_session(bad_id)
    gone = [p.name for p in files if not p.exists()]
    assert not gone, f"delete_session({bad_id!r}) removed: {gone}"
    assert data_dir.exists()


def test_a_folder_delete_takes_the_leftovers_too(data_dir):
    from core import media
    sid = "11111111-2222-3333-4444-555555555555"
    audio, video = paths.audio_dir(), paths.video_dir()
    leftovers = [_touch(video / f"{sid}_part1.mp4"), _touch(video / f"{sid}_concat.txt"),
                 _touch(audio / f"{sid}_mic.opus.dec16k.wav")]
    media.delete_session_media(sid)   # what deleting a folder with its meetings calls
    for p in leftovers:
        assert not p.exists(), f"left behind: {p.name}"
