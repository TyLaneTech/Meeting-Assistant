"""One person, one voice profile.

Regression 2026-10-09: moving lines to a new name one at a time left two
"Jorge Remirez" profiles made 3 ms apart, so Cleanup showed him as two groups.
The rename's profile sync and each moved line's training ran on two workers,
and each looked for a profile of that name before either had made one. A
profile is now found or made under one lock, a typed name in Cleanup reuses the
profile that has it, and profiles that share a name are folded into one (never
the Me profile). Also here: the diarizer's own [Noise] key is noise in Cleanup,
not an unnamed group.

Run: .venv/Scripts/python.exe -m pytest tests/test_voice_profile_names.py -q
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

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


def _count(name: str) -> int:
    with storage._conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM global_speakers WHERE lower(name) = lower(?)",
                            (name,)).fetchone()[0]


def test_two_threads_naming_the_same_new_person_make_one_profile(db, monkeypatch):
    # Widen the window between the look and the make, the way two workers
    # racing on a busy machine would.
    real_create = SpeakerFingerprintDB.create_global_speaker

    def slow_create(self, name, color=None):
        threading.Event().wait(0.05)
        return real_create(self, name, color)

    monkeypatch.setattr(SpeakerFingerprintDB, "create_global_speaker", slow_create)
    gate = threading.Barrier(6)
    got = []

    def name_him():
        gate.wait()
        got.append(db.find_or_create("Jorge Remirez"))

    threads = [threading.Thread(target=name_him) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _count("Jorge Remirez") == 1
    assert len({gid for gid, _made in got}) == 1
    assert sum(made for _gid, made in got) == 1


def test_find_or_create_never_lands_on_an_avoided_profile(db):
    me = db.create_global_speaker("Ty Lane")
    gid, made = db.find_or_create("ty lane ", avoid={me})
    assert made and gid != me
    # With two of a name, the one with more voice samples is found first.
    assert db.find_or_create("Ty Lane", avoid={me}) == (gid, False)


def test_a_name_typed_in_cleanup_reuses_the_profile_that_has_it(db):
    sid = storage.create_session("Partnership review")
    for i in range(4):
        storage.save_segment(sid, f"line {i}", f"Speaker {i % 2 + 1}", float(i * 10), float(i * 10 + 9))
    gid = db.create_global_speaker("Jorge Remirez", color="#768390")
    res = db.apply_cluster_corrections(
        sid, [{"global_id": None, "new_name": "jorge remirez", "color": None,
               "member_keys": ["Speaker 2"]}], noise_keys=[])
    assert _count("Jorge Remirez") == 1
    assert storage.get_speaker_label_rows(sid, ["Speaker 2"])["Speaker 2"]["global_id"] == gid
    assert not res.get("created")


def test_same_name_groups_list_the_keeper_first_and_leave_me_out(db):
    a = db.create_global_speaker("Jorge Remirez")
    b = db.create_global_speaker("jorge remirez ")
    me = db.create_global_speaker("Ty Lane")
    db.create_global_speaker("Ty Lane")
    with storage._conn() as conn:
        conn.execute("UPDATE global_speakers SET emb_count = 26 WHERE id = ?", (b,))
    groups = db.same_name_groups(exclude={me})
    assert [[p["id"] for p in g] for g in groups] == [[b, a]]


def _lift(name: str, scope: dict):
    """One function out of app.py, run against stand-ins (app.py itself loads
    the models when imported)."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), f"<app.{name}>", "exec"), scope)
    return scope[name]


def test_profiles_that_share_a_name_fold_into_one_and_me_is_never_folded(db):
    keep = db.create_global_speaker("Jorge Remirez")
    dup = db.create_global_speaker("Jorge Remirez")
    me = db.create_global_speaker("Ty Lane")
    echo = db.create_global_speaker("Ty Lane")
    with storage._conn() as conn:
        conn.execute("UPDATE global_speakers SET emb_count = 26 WHERE id = ?", (keep,))
    merged = []

    def apply_merge(keep_id, merge_id):
        merged.append((keep_id, merge_id))
        return db.merge_global_speakers(keep_id=keep_id, merge_id=merge_id)

    log = type("L", (), {"info": staticmethod(lambda *a: None), "warn": staticmethod(lambda *a: None)})
    fp = type("FP", (), {"ready": True, "same_name_groups": staticmethod(db.same_name_groups)})
    fold = _lift("_merge_same_name_profiles", {
        "fingerprint_db": fp, "_me_profile_id": lambda: me, "_apply_profile_merge": apply_merge,
        "log": log})
    assert fold() == [(keep, dup)]
    assert merged == [(keep, dup)]
    assert db.get_global_speaker(dup) is None and db.get_global_speaker(keep)
    assert db.get_global_speaker(me) and db.get_global_speaker(echo)   # Me stays out of it
    assert fold("Jorge Remirez") == []


def test_every_find_then_create_in_app_py_goes_through_the_lock():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    # The only direct create left is none: each one finds first, under the lock.
    assert "create_global_speaker(" not in source
    assert source.count("find_or_create(") >= 6
    load = source[source.index("def _load_fingerprint_db"):]
    load = load[:load.index("\ndef ")]
    assert "_merge_same_name_profiles()" in load
    rename = source[source.index("def _rename_profile"):]
    rename = rename[:rename.index("\n@app.route")]
    assert "_merge_same_name_profiles(name)" in rename


def test_the_diarizers_noise_key_is_noise_in_cleanup(db):
    sid = storage.create_session("Standup")
    storage.save_segment(sid, "hello", "Speaker 1", 0.0, 5.0)
    storage.save_segment(sid, "*cough*", "[Noise]", 6.0, 6.4)
    payload = db.cluster_session_speakers(sid)
    unnamed = [m["speaker_key"] for c in payload["unlabeled_clusters"] for m in c["members"]]
    assert "[Noise]" not in unnamed and "Speaker 1" in unnamed
    assert [m["speaker_key"] for m in payload["noise_cluster"]["members"]] == ["[Noise]"]
