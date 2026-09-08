"""The Agent API's organisation and speaker-identification surface.

agent_api/speakers.py (the evidence pack), the new routes in
agent_api/rest.py (queue, review, frames, label, segment reattribution,
folder edits, bulk moves, profile detail / rename / merge, library health,
plan / apply relabel), the OpenAPI cross-check, the MCP tool wiring, and the
app.py hooks the routes rely on. Everything runs in a temporary data folder
with a fake voice library; nothing here needs a model or the app process.
"""
from __future__ import annotations

import base64
import json
import re
import uuid
from pathlib import Path

import numpy as np
import pytest
from flask import Flask

from agent_api import openapi, rest, speakers
from agent_api.context import AgentContext
from ai import speaker_relabel
from core import paths, storage

import mcp_server

ROOT = Path(__file__).parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    yield tmp_path


def _unit(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=256).astype(np.float32)
    return v / np.linalg.norm(v)


def _near(v: np.ndarray, sim: float, seed: int) -> np.ndarray:
    """A unit vector whose cosine similarity to ``v`` is about ``sim``."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(size=256).astype(np.float32)
    noise -= noise.dot(v) * v
    noise /= np.linalg.norm(noise)
    out = sim * v + np.sqrt(max(0.0, 1 - sim * sim)) * noise
    return (out / np.linalg.norm(out)).astype(np.float32)


class FakeLibrary:
    """Just enough of SpeakerFingerprintDB for the evidence pack and routes."""

    AUTO_APPLY_THRESHOLD = 0.82
    MARGIN_FLOOR = 0.66
    MARGIN_GAP = 0.10
    SUGGEST_THRESHOLD = 0.70

    def __init__(self, ready=True, me_id=None):
        self.ready = ready
        self._me_id = me_id
        self.profiles: dict[str, dict] = {}
        self.embeddings: dict[tuple, list] = {}
        self.linked: dict[str, list] = {}
        self.created: list = []
        self.backfilled: list = []
        self.health_calls = 0

    def add_profile(self, name, centroid=None, gid=None, emb_count=5):
        gid = gid or uuid.uuid4().hex
        self.profiles[gid] = {"id": gid, "name": name, "color": "#58a6ff",
                              "emb_count": emb_count if centroid is not None else 0,
                              "created_at": "2026-01-01T00:00:00",
                              "updated_at": "2026-09-01T00:00:00", "centroid": centroid}
        return gid

    # reads
    def list_global_speakers(self):
        return [{k: v for k, v in p.items() if k != "centroid"} for p in self.profiles.values()]

    def get_global_speaker(self, gid):
        p = self.profiles.get(gid)
        return {k: v for k, v in p.items() if k != "centroid"} if p else None

    def find_by_name(self, name):
        for p in self.profiles.values():
            if p["name"].lower() == name.strip().lower():
                return self.get_global_speaker(p["id"])
        return None

    def get_centroid(self, gid):
        p = self.profiles.get(gid)
        return p["centroid"] if p else None

    def get_linked_labels(self, gid):
        return list(self.linked.get(gid, []))

    def get_profile_sessions(self, gid):
        return [{"session_id": r["session_id"], "title": "t", "started_at": "",
                 "speaker_keys": [r["speaker_key"]], "seg_count": 3}
                for r in self.linked.get(gid, [])]

    def _gather_speaker_embeddings(self, sid, key, gid):
        return list(self.embeddings.get((sid, key), []))

    def _backfill_embedding_from_wav(self, sid, key, segments, wav):
        self.backfilled.append(key)
        return None

    def find_matches(self, emb, exclude_global_ids=None, top_k=3, min_similarity=None):
        threshold = self.SUGGEST_THRESHOLD if min_similarity is None else min_similarity
        out = []
        for gid, p in self.profiles.items():
            if gid in (exclude_global_ids or set()) or p["centroid"] is None:
                continue
            sim = float(np.dot(emb, p["centroid"]))
            if sim >= threshold:
                out.append({"global_id": gid, "name": p["name"], "color": p["color"],
                            "similarity": round(sim, 3),
                            "auto_apply": sim >= self.AUTO_APPLY_THRESHOLD})
        out.sort(key=lambda m: -m["similarity"])
        return out[:top_k]

    def library_health(self):
        self.health_calls += 1
        return {"profiles": len(self.profiles), "embeddings": 0, "duplicates": [],
                "foreign": {"profiles": {}, "removed_total": 0, "flagged": []},
                "splits": [], "confusable": []}

    # writes
    def create_global_speaker(self, name, color=None):
        gid = self.add_profile(name, None)
        self.created.append(name)
        return gid


def _seed_library(data):
    """Two meetings. Design review: Dana named and linked, Speaker 2 unnamed
    (Marcus, who introduces himself), Speaker 3 a two-second fragment, the
    owner on the mic. Standup: everyone named."""
    lib = FakeLibrary(me_id="me-profile")
    dana_v, marcus_v, priya_v = _unit(1), _unit(2), _unit(3)
    dana = lib.add_profile("Dana Whitfield", dana_v, gid="dana")
    marcus = lib.add_profile("Marcus Chen", marcus_v, gid="marcus")
    priya = lib.add_profile("Priya Raman", priya_v, gid="priya")
    lib.add_profile("You", None, gid="me-profile")

    s1 = storage.create_session("Design review", started_at="2026-09-01T14:00:00")
    lines = [
        ("Speaker 1", 0.0, 6.0, "Let's get started. Marcus, do you want to kick us off?"),
        ("Speaker 2", 6.5, 14.0, "Sure. Hi everyone, this is Marcus Chen from the platform team."),
        ("Speaker 1", 14.5, 17.0, "Thanks, Marcus."),
        ("Speaker 2", 17.5, 40.0, "So the migration plan has three phases and the first one lands next week, "
                                  "which means the data layer freezes on Tuesday."),
        ("me", 40.5, 58.0, "That works for us on the client side, we can hold the release until the "
                           "data layer is frozen and the checks have run."),
        ("Speaker 2", 58.5, 81.0, "Great. The second phase is the cutover itself and that is where I need "
                                  "everyone's eyes on the runbook."),
        ("Speaker 3", 81.5, 83.5, "Mm-hm."),
        ("Speaker 1", 84.0, 101.0, "Let's make sure the runbook has owners next to each step before Friday."),
    ]
    seg_ids = {}
    for key, start, end, text in lines:
        sid = storage.save_segment(s1, text, key, start, end)
        seg_ids.setdefault(key, []).append(sid)
    storage.save_speaker_label(s1, "Speaker 1", name="Dana Whitfield", color="#58a6ff")
    storage.save_speaker_label(s1, "me", name="You")
    with storage._conn() as conn:
        conn.execute("UPDATE speaker_labels SET global_id='dana' WHERE session_id=? AND speaker_key='Speaker 1'", (s1,))
        conn.execute("UPDATE speaker_labels SET global_id='me-profile' WHERE session_id=? AND speaker_key='me'", (s1,))
    lib.linked["dana"] = [{"session_id": s1, "speaker_key": "Speaker 1", "name": "Dana Whitfield", "color": "#58a6ff"}]
    lib.embeddings[(s1, "Speaker 1")] = [_near(dana_v, 0.9, 11)]
    lib.embeddings[(s1, "Speaker 2")] = [_near(marcus_v, 0.88, 12), _near(marcus_v, 0.86, 13)]
    storage.set_expected_speaker_count(s1, 3, "calendar")
    storage.set_calendar_match(s1, {"uid": "evt-1", "title": "Design review sync",
                                    "start": "2026-09-01T14:00:00", "attendee_count": 3})
    cand_dir = data / "resolution_candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    (cand_dir / f"{s1}.json").write_text(json.dumps({
        "meeting": {"calendar_subject": "Design review sync"},
        "candidates": [
            {"name": "Dana Whitfield", "email": "dana@x.test", "role": "organizer", "source": "calendar"},
            {"name": "Marcus Chen", "email": "marcus@x.test", "role": "required", "source": "calendar"},
            {"name": "Priya Raman", "email": "priya@x.test", "role": "required", "source": "calendar"},
        ],
        "speaker_hints": ["Marcus usually presents"],
    }), encoding="utf-8")

    s2 = storage.create_session("Standup", started_at="2026-08-30T09:00:00")
    storage.save_segment(s2, "Quick round, what is everyone on today?", "Speaker 1", 0.0, 20.0)
    storage.save_segment(s2, "Finishing the export path.", "Speaker 2", 20.0, 45.0)
    storage.save_speaker_label(s2, "Speaker 1", name="Dana Whitfield")
    storage.save_speaker_label(s2, "Speaker 2", name="Priya Raman")
    return {"lib": lib, "s1": s1, "s2": s2, "segs": seg_ids,
            "dana": dana, "marcus": marcus, "priya": priya}


class Recorder:
    """Stub write capabilities: record the call, mimic the UI's storage effect."""

    def __init__(self):
        self.labels: list = []
        self.corrections: list = []
        self.segments: list = []
        self.renames: list = []
        self.merges: list = []
        self.events: list = []

    def label_speaker(self, sid, keys, name, color, gid, train):
        self.labels.append((sid, list(keys), name, color, gid, train))
        out = []
        for k in keys:
            out.append(storage.save_speaker_label(sid, k, name=name, color=color))
        if gid:
            with storage._conn() as conn:
                for k in keys:
                    conn.execute("UPDATE speaker_labels SET global_id=? WHERE session_id=? AND speaker_key=?",
                                 (gid, sid, k))
        return out

    def apply_speaker_corrections(self, sid, proposed, noise_keys):
        self.corrections.append((sid, proposed, list(noise_keys)))
        with storage._conn() as conn:
            for k in noise_keys:
                conn.execute("INSERT INTO speaker_labels (session_id, speaker_key, name, is_noise) VALUES (?, ?, ?, 1) "
                             "ON CONFLICT(session_id, speaker_key) DO UPDATE SET is_noise=1, global_id=NULL", (sid, k, k))
            for cluster in proposed:
                for k in cluster.get("member_keys", []):
                    conn.execute("INSERT INTO speaker_labels (session_id, speaker_key, name, is_noise) VALUES (?, ?, ?, 0) "
                                 "ON CONFLICT(session_id, speaker_key) DO UPDATE SET name=excluded.name, global_id=NULL, is_noise=0",
                                 (sid, k, k))
        return {"relinked": 0, "unlinked": sum(len(c.get("member_keys", [])) for c in proposed),
                "noise_marked": len(noise_keys)}

    def relabel_segment(self, seg_id, label, source_override, *, train=True):
        self.segments.append((seg_id, label, source_override, train))
        storage.save_segment_label_override(seg_id, label)
        storage.save_segment_source_override(seg_id, source_override)
        return storage.get_segment(seg_id)

    def rename_profile(self, gid, name=None, color=...):
        self.renames.append((gid, name))
        return {"name": name, "color": "#58a6ff"}

    def merge_profiles(self, keep, merge):
        self.merges.append((keep, merge))
        return {"name": "Marcus Chen", "color": "#58a6ff"}

    def push_event(self, name, payload):
        self.events.append((name, payload))


def _scope_filters(tool_input: dict) -> dict:
    """A small stand-in for app._scope_filters: folder by id / name / path."""
    folders = storage.folder_tree()
    spec = (tool_input.get("folder") or "").strip()
    include_sub = tool_input.get("include_subfolders", True)
    include_sub = True if include_sub is None else bool(include_sub)
    base = {"start": None, "end": None, "speaker": (tool_input.get("speaker") or None),
            "folders": folders, "desc": "", "active": bool(spec)}
    if not spec:
        return {**base, "folder_ids": None, "error": None, "label": ""}
    matches = [f for f in folders if spec in (f["id"], f["name"], f["path"])
               or f["name"].lower() == spec.lower()]
    if not matches:
        return {**base, "folder_ids": [], "label": spec,
                "error": {"error": f"No folder matches '{spec}'."}}
    if len(matches) > 1:
        return {**base, "folder_ids": [], "label": spec,
                "error": {"error": "ambiguous", "candidates": [{"id": f["id"]} for f in matches]}}
    f = matches[0]
    return {**base, "folder_ids": storage.folder_with_descendants(f["id"], recursive=include_sub),
            "error": None, "label": f["path"], "desc": f" in {f['path']}"}


def _scoped_session_ids(filters):
    if not filters["active"]:
        return None
    return storage.list_session_ids(folder_ids=filters["folder_ids"], speaker=filters["speaker"])


def _folder_labels(folders=None):
    return {f["id"]: f for f in (folders or storage.folder_tree())}


def _describe_session(meta, labels, *, summary_chars):
    info = labels.get(meta.get("folder_id")) if meta.get("folder_id") else None
    return {"session_id": meta["session_id"], "title": meta["title"],
            "started_at": meta["started_at"], "folder_path": info["path"] if info else None,
            "summary": (meta.get("summary") or "")[:summary_chars]}


def _relabel_deps(lib):
    return speaker_relabel.RelabelDeps(
        find_labels=lambda name, match, ids: storage.find_speaker_labels_by_name(name, match=match, session_ids=ids),
        speaker_time_stats=storage.speaker_time_stats,
        count_label_overrides=lambda name, match, ids: storage.count_label_overrides_by_name(name, match=match, session_ids=ids),
        find_profile_by_name=lambda name: None,
        create_profile=lambda name: "new",
        bulk_link=lambda name, gid: {},
        merge_profiles=lambda keep, merge: {},
        patch_session=lambda sid, keys, name: [storage.save_speaker_label(sid, k, name=name) for k in keys],
        library_ready=lambda: False,
    )


@pytest.fixture()
def api(data):
    seeded = _seed_library(data)
    lib = seeded["lib"]
    rec = Recorder()
    status = {"recording": False, "session_id": None, "is_reanalyzing": False}
    ctx = AgentContext(
        status_payload=lambda: dict(status),
        live_extras=lambda: {},
        live_media=lambda: {"recording": False},
        scope_filters=_scope_filters,
        scoped_session_ids=_scoped_session_ids,
        folder_labels=_folder_labels,
        describe_session=_describe_session,
        source_labels={"loopback": "Desktop", "mic": "Mic", "me": "Me"},
        model_snapshot=lambda: {},
        ai_snapshot=lambda: {},
        apply_ai_settings=lambda p, m: {},
        list_global_speakers=lib.list_global_speakers,
        get_profile_sessions=lib.get_profile_sessions,
        changelog=lambda n: [],
        stop_recording=lambda: None,
        push_status=lambda: None,
        push_event=rec.push_event,
        server_url="http://test",
        voice_library=lib,
        label_speaker=rec.label_speaker,
        apply_speaker_corrections=rec.apply_speaker_corrections,
        relabel_segment=rec.relabel_segment,
        rename_profile=rec.rename_profile,
        merge_profiles=rec.merge_profiles,
        relabel_deps=lambda: _relabel_deps(lib),
        me_profile_id=lambda: "me-profile",
    )
    app = Flask(__name__)
    rest.register_agent_api(app, ctx)
    client = app.test_client()
    return {"client": client, "rec": rec, "status": status, **seeded}


def _get(api, path, **params):
    resp = api["client"].get(f"/api/agent/v1{path}", query_string=params)
    return resp.status_code, resp.get_json()


def _post(api, path, body, method="post"):
    resp = getattr(api["client"], method)(f"/api/agent/v1{path}", json=body)
    return resp.status_code, resp.get_json()


# ── agent_api/speakers.py ───────────────────────────────────────────────────

def test_speaker_rows_give_each_key_a_status(data):
    s = _seed_library(data)
    sess = storage.get_session(s["s1"])
    rows = speakers.speaker_rows(s["s1"], sess["segments"], {"me": "Me"}, "me-profile")
    by_key = {r["speaker_key"]: r for r in rows}
    assert by_key["Speaker 1"]["status"] == "named" and by_key["Speaker 1"]["global_id"] == "dana"
    assert by_key["Speaker 2"]["status"] == "unnamed" and by_key["Speaker 2"]["is_generic"]
    assert by_key["Speaker 3"]["status"] == "minor", "two seconds of 'Mm-hm' is a fragment"
    assert by_key["me"]["status"] == "me" and by_key["me"]["kind"] == "me"
    assert by_key["Speaker 2"]["first_heard"] == 6.5 and by_key["Speaker 2"]["last_heard"] == 81.0
    # Unnamed first, then named, then the rest.
    assert [r["status"] for r in rows][:2] == ["unnamed", "named"]


def test_quotes_flag_self_introductions_and_hints_read_addresses(data):
    s = _seed_library(data)
    sess = storage.get_session(s["s1"])
    by_key = speakers.segments_by_key(sess["segments"])
    intros = speakers.self_introductions(by_key["Speaker 2"])
    assert intros and intros[0]["name"] == "Marcus Chen"
    qs = speakers.quotes(by_key["Speaker 2"], 2)
    assert any(q["self_introduction"] for q in qs), "the introduction always makes the cut"
    assert qs == sorted(qs, key=lambda q: q["t"]), "quotes come back in meeting order"
    hints = speakers.name_hints(sess["segments"], "Speaker 2", lambda seg: seg["source"])
    assert [(h["name"], h["count"]) for h in hints] == [("Marcus", 1)]
    assert hints[0]["examples"][0]["pattern"] == "addressed after speaking"
    assert hints[0]["examples"][0]["by"] == "Speaker 1"


def test_self_introduction_ignores_common_false_positives():
    """The real library produced 'It's probably less work' and 'this is the
    plan' as introductions when the trigger was case-insensitive."""
    segs = [{"id": 1, "start_time": 0, "end_time": 3, "text": "This is Going to be a long one."},
            {"id": 2, "start_time": 3, "end_time": 6, "text": "I'm Not sure about that."},
            {"id": 3, "start_time": 6, "end_time": 9, "text": "this is the plan"},
            {"id": 4, "start_time": 9, "end_time": 12, "text": "It's probably less work overall."},
            {"id": 5, "start_time": 12, "end_time": 15, "text": "Well, it's demographics."},
            {"id": 6, "start_time": 15, "end_time": 18, "text": "this is UKG territory"}]
    assert speakers.self_introductions(segs) == []
    real = [{"id": 7, "start_time": 20, "end_time": 24, "text": "Morning all, this is Dana Whitfield speaking."},
            {"id": 8, "start_time": 24, "end_time": 27, "text": "I'm Priya, I run the data team."}]
    assert [i["name"] for i in speakers.self_introductions(real)] == ["Dana Whitfield", "Priya"]


def test_name_hints_need_capitalised_names_and_a_comma_before_a_question():
    segs = [
        {"id": 1, "source": "A", "start_time": 0, "end_time": 3, "text": "Right, works for me."},
        {"id": 2, "source": "B", "start_time": 3, "end_time": 6, "text": "Are there any other systems like Snowflake?"},
        {"id": 3, "source": "A", "start_time": 6, "end_time": 9, "text": "Yes, UKG is one."},
        {"id": 4, "source": "B", "start_time": 9, "end_time": 12, "text": "What do you think, Dana?"},
        {"id": 5, "source": "A", "start_time": 12, "end_time": 15, "text": "I think it is fine."},
        {"id": 6, "source": "B", "start_time": 15, "end_time": 18, "text": "Good point, thanks Dana."},
    ]
    hints = speakers.name_hints(segs, "A", lambda seg: seg["source"])
    assert [(h["name"], h["count"]) for h in hints] == [("Dana", 2)]
    assert {e["pattern"] for e in hints[0]["examples"]} == {"asked by name, then answered",
                                                             "addressed after speaking"}
    # 'works' and 'UKG' after a trigger are not names; 'Snowflake?' without a
    # comma is not an address.
    assert speakers.name_hints(segs, "B", lambda seg: seg["source"]) == []


def test_moments_spread_across_the_longest_turns():
    segs = [{"id": i, "start_time": i * 60.0, "end_time": i * 60.0 + (30.0 if i % 2 else 1.0),
             "text": f"turn {i}"} for i in range(10)]
    ms = speakers.moments(segs, 3)
    assert len(ms) == 3
    assert [m["t"] for m in ms] == sorted(m["t"] for m in ms)
    for m in ms:
        assert m["start"] < m["t"] <= m["end"]
        assert m["end"] - m["start"] == 30.0, "the one-second turns are never chosen"
    assert speakers.moments([], 3) == []


def test_library_matches_grade_the_best_candidate():
    lib = FakeLibrary()
    a, b = _unit(21), _unit(22)
    lib.add_profile("Ana", a, gid="ana")
    lib.add_profile("Ben", b, gid="ben")
    th = speakers._thresholds(lib)
    strong = speakers.library_matches(lib, _near(a, 0.9, 5), top_k=3)
    assert strong["verdict"] == "strong" and strong["best"]["name"] == "Ana"
    clear = speakers.library_matches(lib, _near(a, 0.72, 6), top_k=3)
    assert clear["verdict"] == "clear", clear
    assert clear["runner_up_gap"] is None or clear["runner_up_gap"] >= th["margin_gap"]
    weak = speakers.library_matches(lib, _near(a, 0.58, 7), top_k=3)
    assert weak["verdict"] == "weak"
    nothing = speakers.library_matches(lib, _unit(99), top_k=3)
    assert nothing["verdict"] == "none"
    # A profile already carrying another key in this meeting is called out.
    taken = speakers.library_matches(lib, _near(a, 0.9, 8), top_k=3, assigned={"ana": "Speaker 1"})
    assert taken["best"]["already_in_this_meeting_as"] == "Speaker 1"
    assert "same_as" in taken["note"]
    # No model, no matching.
    lib.ready = False
    off = speakers.library_matches(lib, _near(a, 0.9, 9))
    assert off["verdict"] == "none" and "not loaded" in off["note"]


def test_verdict_uses_the_margin_rule():
    th = {"strong": 0.82, "margin_floor": 0.66, "margin_gap": 0.10, "possible": 0.70, "weak": 0.55}
    assert speakers.verdict(0.85, 0.0, th) == "strong"
    assert speakers.verdict(0.70, 0.12, th) == "clear"
    assert speakers.verdict(0.75, 0.02, th) == "possible"
    assert speakers.verdict(0.60, 0.5, th) == "weak"
    assert speakers.verdict(None, None, th) == "none"


def test_proximity_marks_the_same_voice():
    a = _unit(31)
    rows = speakers.proximity("Speaker 2", a, [
        ("Speaker 2", "x", "unnamed", a),
        ("Speaker 5", "Speaker 5", "minor", _near(a, 0.8, 1)),
        ("Speaker 1", "Dana", "named", _near(a, 0.3, 2)),
        ("Speaker 9", "Speaker 9", "minor", None),
    ])
    assert [r["speaker_key"] for r in rows] == ["Speaker 5", "Speaker 1"]
    assert rows[0]["likely_same_voice"] and not rows[1]["likely_same_voice"]
    assert speakers.proximity("k", None, []) == []


def test_calendar_context_knows_who_is_already_assigned(data):
    s = _seed_library(data)
    sess = storage.get_session(s["s1"])
    rows = speakers.speaker_rows(s["s1"], sess["segments"], {}, "me-profile")
    cal = speakers.calendar_context(s["s1"], rows)
    assert cal["subject"] == "Design review sync" and cal["expected_speakers"] == 3
    people = {p["name"]: p for p in cal["people"]}
    assert people["Dana Whitfield"]["assigned_to"] == "Speaker 1"
    assert cal["unassigned_people"] == ["Marcus Chen", "Priya Raman"]
    assert cal["speaker_hints"] == ["Marcus usually presents"]
    assert speakers.calendar_context(s["s2"], []) is None


# ── Routes: the review and the queue ────────────────────────────────────────

def test_review_gathers_every_kind_of_evidence(api):
    code, out = _get(api, f"/meetings/{api['s1']}/speakers/review")
    assert code == 200, out
    assert out["counts"] == {"unnamed": 1, "named": 1, "minor": 1, "noise": 0, "me": 1}
    assert out["library"]["ready"] is True
    assert out["detail"] == "unnamed"
    by_key = {sp["speaker_key"]: sp for sp in out["speakers"]}
    assert list(by_key) == ["Speaker 2"], "full evidence for the unnamed speaker only"
    others = {o["speaker_key"]: o for o in out["others"]}
    assert set(others) == {"Speaker 1", "Speaker 3", "me"}
    assert "library_matches" not in others["me"] and "closest_other" not in others["me"]
    dana_closest = others["Speaker 1"]["closest_other"]
    assert dana_closest["speaker_key"] == "Speaker 2" and dana_closest["likely_same_voice"] is False
    assert "closest_other" not in others["Speaker 3"], "no voice sample, nothing to compare"
    assert any("others lists every other speaker" in d for d in out["disclosures"])
    marcus = by_key["Speaker 2"]
    assert marcus["status"] == "unnamed"
    assert marcus["self_introductions"][0]["name"] == "Marcus Chen"
    assert marcus["library_matches"]["best"]["name"] == "Marcus Chen"
    assert marcus["library_matches"]["verdict"] == "strong"
    assert marcus["voice"] == {"embeddings": 2, "source": "stored"}
    assert marcus["name_hints"][0]["name"] == "Marcus"
    assert marcus["frame_moments"] == [], "no screen recording, no moments"
    prox = {p["speaker_key"]: p for p in marcus["proximity"]}
    assert "Speaker 1" in prox and not prox["Speaker 1"]["likely_same_voice"]
    assert "me" not in prox, "the owner's mic is never compared"
    assert out["calendar"]["unassigned_people"] == ["Marcus Chen", "Priya Raman"]
    assert out["attention"]["unresolved"] == 1 and out["attention"]["expected"] == 3
    assert any("reinforce" in d for d in out["disclosures"])
    assert any("no screen recording" in d for d in out["disclosures"])
    # No audio in the fixture, so no speaker was offered a backfill.
    assert api["lib"].backfilled == []


def test_review_focuses_on_one_speaker_and_rejects_unknown_keys(api):
    code, out = _get(api, f"/meetings/{api['s1']}/speakers/review", speaker_key="Speaker 1")
    assert code == 200 and out["detail"] == "speaker"
    assert [sp["speaker_key"] for sp in out["speakers"]] == ["Speaker 1"], "a named speaker on request"
    assert out["speakers"][0]["library_matches"]["best"]["name"] == "Dana Whitfield"
    assert {o["speaker_key"] for o in out["others"]} == {"Speaker 2", "Speaker 3", "me"}
    code, out = _get(api, f"/meetings/{api['s1']}/speakers/review", detail="all")
    assert code == 200 and out["detail"] == "all"
    assert {sp["speaker_key"] for sp in out["speakers"]} == {"Speaker 1", "Speaker 2", "Speaker 3"}
    assert [o["speaker_key"] for o in out["others"]] == ["me"], "the owner is never detailed"
    assert _get(api, f"/meetings/{api['s1']}/speakers/review", detail="bogus")[0] == 400
    code, out = _get(api, f"/meetings/{api['s1']}/speakers/review", speaker_key="Speaker 42")
    assert code == 404 and "Speaker 2" in out["speakers"]
    code, out = _get(api, f"/meetings/{api['s1']}/speakers/review", matches="false")
    assert code == 200 and "library_matches" not in out["speakers"][0]
    assert all("closest_other" not in o for o in out["others"])
    assert _get(api, "/meetings/nope/speakers/review")[0] == 404


def test_review_without_the_model_still_reads_text_and_calendar(api):
    api["lib"].ready = False
    code, out = _get(api, f"/meetings/{api['s1']}/speakers/review")
    assert code == 200
    assert out["library"]["ready"] is False
    marcus = next(sp for sp in out["speakers"] if sp["speaker_key"] == "Speaker 2")
    assert marcus["library_matches"]["verdict"] == "none"
    assert marcus["self_introductions"][0]["name"] == "Marcus Chen"
    assert out["disclosures"][0].startswith("The voice library model is not loaded")


def test_queue_lists_only_meetings_that_need_speaker_work(api):
    code, out = _get(api, "/speakers/queue")
    assert code == 200
    assert [m["session_id"] for m in out["meetings"]] == [api["s1"]]
    # Three material voices (Dana, Marcus, the owner) match the invite's three,
    # so the only reason is the unnamed one.
    assert out["meetings"][0]["attention"]["reasons"] == ["unresolved_speakers"]
    assert out["meetings"][0]["attention"]["found"] == 3
    assert out["total"] == 1 and out["library_ready"] is True
    code, out = _get(api, "/speakers/queue", reason="mismatch")
    assert code == 200 and out["meetings"] == []
    assert _get(api, "/speakers/queue", reason="bogus")[0] == 400
    # Naming the speaker empties the queue.
    _post(api, f"/meetings/{api['s1']}/speakers/label",
          {"speaker_keys": ["Speaker 2"], "name": "Marcus Chen", "evidence": "he says so"})
    assert _get(api, "/speakers/queue")[1]["meetings"] == []


def test_meeting_speakers_route_carries_the_status(api):
    code, out = _get(api, f"/meetings/{api['s1']}/speakers")
    assert code == 200 and out["unnamed"] == 1
    assert {s["speaker_key"]: s["status"] for s in out["speakers"]}["Speaker 2"] == "unnamed"
    code, out = _get(api, f"/meetings/{api['s1']}", include="calendar,attention")
    assert code == 200 and out["calendar"]["expected_speakers"] == 3
    assert out["attention"]["unresolved"] == 1


# ── Routes: labelling ───────────────────────────────────────────────────────

def test_label_by_name_links_the_existing_profile_through_the_ui_path(api):
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 2"], "name": "Marcus Chen",
                       "evidence": "self-introduction at 0:06"})
    assert code == 200, out
    # No colour is forced: the UI path inherits the profile's colour when it
    # links, exactly as a rename typed into the dialog does.
    assert api["rec"].labels == [(api["s1"], ["Speaker 2"], "Marcus Chen", None, "marcus", False)]
    assert out["profile"] == {"global_id": "marcus", "name": "Marcus Chen", "created": False}
    assert out["speakers"][0]["name"] == "Marcus Chen" and out["speakers"][0]["status"] == "named"
    assert out["reinforce"] is False and out["evidence"] == "self-introduction at 0:06"
    assert out["attention"]["unresolved"] == 0
    assert any("without training" in n for n in out["notes"])


def test_label_by_new_name_creates_the_profile_first(api):
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_key": "Speaker 2", "name": "Jordan Lee", "reinforce": True,
                       "evidence": "name tag on screen"})
    assert code == 200, out
    assert api["lib"].created == ["Jordan Lee"]
    assert out["profile"]["created"] is True and out["profile"]["name"] == "Jordan Lee"
    sid, keys, name, color, gid, train = api["rec"].labels[0]
    assert gid == out["profile"]["global_id"] and train is True
    assert any("reinforce was set" in n for n in out["notes"])


def test_label_by_profile_id_uses_the_profile_name(api):
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 2"], "global_id": "priya"})
    assert code == 200
    assert api["rec"].labels[0][2:5] == ("Priya Raman", "#58a6ff", "priya")
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 3"], "global_id": "priya", "name": "Someone Else"})
    assert code == 409, "a name that contradicts the profile is refused"
    assert _post(api, f"/meetings/{api['s1']}/speakers/label",
                 {"speaker_keys": ["Speaker 3"], "global_id": "nope"})[0] == 404


def test_label_same_as_merges_under_the_other_speakers_name(api):
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 3"], "same_as": "Speaker 1"})
    assert code == 200, out
    assert api["rec"].labels == [(api["s1"], ["Speaker 3"], "Dana Whitfield", "#58a6ff", "dana", False)]
    assert out["action"] == "same_as"
    # same_as an unnamed key is refused: name it first.
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 1"], "same_as": "Speaker 2"})
    assert code == 409 and "no name yet" in out["error"]
    assert _post(api, f"/meetings/{api['s1']}/speakers/label",
                 {"speaker_keys": ["Speaker 3"], "same_as": "Speaker 3"})[0] == 400
    assert _post(api, f"/meetings/{api['s1']}/speakers/label",
                 {"speaker_keys": ["Speaker 3"], "same_as": "me"})[0] == 403


def test_label_refuses_the_owner_placeholders_and_mixed_actions(api):
    path = f"/meetings/{api['s1']}/speakers/label"
    assert _post(api, path, {"speaker_keys": ["me"], "name": "Ty"})[0] == 403
    code, out = _post(api, path, {"speaker_keys": ["Speaker 2"], "name": "Speaker 9"})
    assert code == 400 and "reset" in out["error"]
    assert _post(api, path, {"speaker_keys": ["Speaker 2"], "name": "X", "noise": True})[0] == 400
    assert _post(api, path, {"speaker_keys": ["Speaker 2"]})[0] == 400
    code, out = _post(api, path, {"speaker_keys": ["Speaker 77"], "name": "X"})
    assert code == 404 and any(s["speaker_key"] == "Speaker 2" for s in out["speakers"])
    assert _post(api, path, {"name": "X"})[0] == 400
    # The owner's profile is never the target, by id or by name.
    assert _post(api, path, {"speaker_keys": ["Speaker 2"], "global_id": "me-profile"})[0] == 403
    assert _post(api, path, {"speaker_keys": ["Speaker 2"], "name": "You"})[0] == 403
    assert api["rec"].labels == []


def test_label_noise_and_reset_go_through_the_cleanup_save(api):
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 3"], "noise": True, "evidence": "just a murmur"})
    assert code == 200, out
    assert api["rec"].corrections == [(api["s1"], [], ["Speaker 3"])]
    assert out["speakers"][0]["status"] == "noise"
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 1"], "reset": True})
    assert code == 200, out
    assert api["rec"].corrections[-1] == (api["s1"], [{"global_id": None, "member_keys": ["Speaker 1"]}], [])
    assert out["speakers"][0]["status"] == "unnamed" and out["speakers"][0]["global_id"] is None


def test_label_waits_while_the_meeting_is_being_reanalysed(api):
    api["status"].update(session_id=api["s1"], is_reanalyzing=True)
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 2"], "name": "Marcus Chen"})
    assert code == 409 and "reanalysed" in out["error"]
    api["status"].update(is_reanalyzing=False, recording=True)
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 2"], "name": "Marcus Chen"})
    assert code == 200 and any("recording" in n for n in out["notes"])


def test_label_without_the_model_names_the_meeting_only(api):
    api["lib"].ready = False
    code, out = _post(api, f"/meetings/{api['s1']}/speakers/label",
                      {"speaker_keys": ["Speaker 2"], "name": "Marcus Chen"})
    assert code == 200
    assert api["rec"].labels[0][4] is None and out["profile"] is None
    assert any("not loaded" in n for n in out["notes"])


def test_segment_reattribution_pins_one_line(api):
    seg_id = api["segs"]["Speaker 2"][0]
    code, out = _post(api, f"/meetings/{api['s1']}/segments/{seg_id}/speaker",
                      {"speaker_key": "Speaker 1"})
    assert code == 200, out
    assert api["rec"].segments == [(seg_id, "Dana Whitfield", "Speaker 1", False)]
    assert out["segment"]["speaker"] == "Dana Whitfield" and out["segment"]["source"] == "Speaker 1"
    # A one-off label, with reinforce honoured only for a real name.
    code, out = _post(api, f"/meetings/{api['s1']}/segments/{seg_id}/speaker",
                      {"name": "Guest presenter", "reinforce": True})
    assert code == 200 and api["rec"].segments[-1] == (seg_id, "Guest presenter", None, True)
    assert _post(api, f"/meetings/{api['s2']}/segments/{seg_id}/speaker",
                 {"speaker_key": "Speaker 1"})[0] == 404, "the line belongs to another meeting"
    assert _post(api, f"/meetings/{api['s1']}/segments/{seg_id}/speaker", {})[0] == 400
    assert _post(api, f"/meetings/{api['s1']}/segments/{seg_id}/speaker",
                 {"speaker_key": "Speaker 55"})[0] == 404


def test_speaker_frames_need_a_screen_recording(api):
    code, out = _get(api, f"/meetings/{api['s1']}/speakers/Speaker%202/frames")
    assert code == 404 and "screen recording" in out["error"]


# ── Routes: folders and bulk moves ──────────────────────────────────────────

def test_folder_rename_move_and_cycle_guard(api):
    a = storage.create_folder("Clients")
    b = storage.create_folder("Acme", parent_id=a)
    code, out = _post(api, f"/folders/{b}", {"name": "Acme Corp"}, method="patch")
    assert code == 200 and out["folder"]["path"] == "Clients / Acme Corp"
    assert out["changed"] == {"name": "Acme Corp"}
    code, out = _post(api, f"/folders/{a}", {"parent": "Acme Corp"}, method="patch")
    assert code == 409, "a parent cannot move under its own child"
    code, out = _post(api, f"/folders/{b}", {"parent": None}, method="patch")
    assert code == 200 and out["folder"]["parent_id"] is None and out["folder"]["path"] == "Acme Corp"
    code, out = _post(api, f"/folders/{b}", {"parent": "Clients"}, method="patch")
    assert code == 200 and out["changed"]["parent_path"] == "Clients"
    assert _post(api, f"/folders/{b}", {}, method="patch")[0] == 400
    assert _post(api, f"/folders/{b}", {"name": "  "}, method="patch")[0] == 400
    assert _post(api, "/folders/nope", {"name": "x"}, method="patch")[0] == 404
    assert ("library_changed", {"reason": "folder_updated", "folder_id": b}) in api["rec"].events


def test_move_meetings_reports_moved_already_there_and_missing(api):
    fid = storage.create_folder("Design")
    code, out = _post(api, "/meetings/move",
                      {"meeting_ids": [api["s1"], api["s2"], "ghost"], "folder": "Design"})
    assert code == 200, out
    assert sorted(out["moved"]) == sorted([api["s1"], api["s2"]])
    assert out["missing"] == ["ghost"] and out["folder_id"] == fid and out["folder_path"] == "Design"
    assert storage.get_sessions_meta([api["s1"]])[api["s1"]]["folder_id"] == fid
    code, out = _post(api, "/meetings/move", {"meeting_ids": [api["s1"]], "folder": fid})
    assert code == 200 and out["already_there"] == [api["s1"]] and out["moved"] == []
    code, out = _post(api, "/meetings/move", {"meeting_ids": [api["s1"]], "folder": None})
    assert code == 200 and out["moved"] == [api["s1"]] and out["folder_id"] is None
    assert _post(api, "/meetings/move", {"meeting_ids": [], "folder": None})[0] == 400
    assert _post(api, "/meetings/move", {"meeting_ids": [api["s1"]]})[0] == 400
    assert _post(api, "/meetings/move", {"meeting_ids": ["ghost"], "folder": None})[0] == 404
    assert any(e[0] == "library_changed" and e[1]["reason"] == "meetings_moved" for e in api["rec"].events)


# ── Routes: the voice library ───────────────────────────────────────────────

def test_profile_detail_rename_and_merge(api):
    code, out = _get(api, "/speakers/dana")
    assert code == 200
    assert out["profile"]["name"] == "Dana Whitfield" and out["session_count"] == 1
    assert out["is_me"] is False and out["recent_meetings"][0]["session_id"] == api["s1"]
    # Standup labels Dana by name without a link: that is the count the route reports.
    assert out["labels_with_this_name_not_linked"] == 1
    assert _get(api, "/speakers/me-profile")[1]["is_me"] is True
    code, out = _get(api, "/speakers/nobody")
    assert code == 404

    code, out = _post(api, "/speakers/marcus", {"name": "Marcus  Chen-Ortiz"}, method="patch")
    assert code == 200 and api["rec"].renames == [("marcus", "Marcus  Chen-Ortiz")]
    assert out["previous_name"] == "Marcus Chen"
    code, out = _post(api, "/speakers/marcus", {"name": "Priya Raman"}, method="patch")
    assert code == 409 and out["existing_profile_id"] == "priya"
    assert _post(api, "/speakers/marcus", {"name": "Speaker 4"}, method="patch")[0] == 400
    assert _post(api, "/speakers/me-profile", {"name": "Ty"}, method="patch")[0] == 403
    assert _post(api, "/speakers/nope", {"name": "X"}, method="patch")[0] == 404

    code, out = _post(api, "/speakers/marcus/merge", {"source_id": "priya"})
    assert code == 400 and out["merge"]["name"] == "Priya Raman" and api["rec"].merges == []
    code, out = _post(api, "/speakers/marcus/merge", {"source_id": "priya", "confirm": True})
    assert code == 200 and api["rec"].merges == [("marcus", "priya")]
    assert out["merged_away"] == {"global_id": "priya", "name": "Priya Raman"}
    assert _post(api, "/speakers/marcus/merge", {"source_id": "me-profile", "confirm": True})[0] == 403
    assert _post(api, "/speakers/marcus/merge", {"source_id": "marcus", "confirm": True})[0] == 400
    assert _post(api, "/speakers/marcus/merge", {"source_id": "ghost", "confirm": True})[0] == 404
    api["lib"].ready = False
    assert _post(api, "/speakers/marcus/merge", {"source_id": "dana", "confirm": True})[0] == 503


def test_library_health_is_read_only(api):
    code, out = _get(api, "/speakers/library/health")
    assert code == 200 and out["read_only"] is True and out["profiles"] == 4
    assert api["lib"].health_calls == 1


def test_relabel_plan_confirm_apply_cycle(api):
    code, plan = _post(api, "/speakers/relabel/plan",
                       {"from_name": "Dana Whitfield", "to_name": "Dana W.", "scope": "library"})
    assert code == 200, plan
    assert plan["token"] and plan["session_count"] == 2 and plan["matched"] == 2
    assert "Nothing has changed" in plan["next_step"]
    assert _post(api, "/speakers/relabel/apply", {"token": plan["token"]})[0] == 400
    code, out = _post(api, "/speakers/relabel/apply", {"token": plan["token"], "confirm": True})
    assert code == 200, out
    assert out["applied"] and out["session_count"] == 2
    assert storage.get_speaker_profile(api["s2"], "Speaker 1")["name"] == "Dana W."
    assert any(e[0] == "library_changed" for e in api["rec"].events)
    # Spent tokens are refused; unknown tokens cancel to nothing.
    assert _post(api, "/speakers/relabel/apply", {"token": plan["token"], "confirm": True})[0] == 409
    code, out = _post(api, "/speakers/relabel/cancel", {"token": "nope"})
    assert code == 200 and out["cancelled"] is False
    code, out = _post(api, "/speakers/relabel/plan",
                      {"from_name": "Nobody", "to_name": "Someone"})
    assert code == 200 and out["matched"] == 0 and out["token"] is None
    assert _post(api, "/speakers/relabel/plan", {"from_name": "A", "to_name": "B", "scope": "session"})[0] == 400
    # The library-wide apply above renamed her everywhere, so a session-scoped
    # plan starts from the new spelling and touches one meeting.
    code, out = _post(api, "/speakers/relabel/plan",
                      {"from_name": "Dana W.", "to_name": "Dana", "scope": "session",
                       "session_id": api["s2"]})
    assert code == 200, out
    assert out["session_count"] == 1 and out["scope"] == "session"


# ── The spec, the MCP server, and the app.py hooks ──────────────────────────

def test_every_blueprint_route_is_in_the_openapi_spec():
    app = Flask(__name__)
    app.register_blueprint(rest.bp)
    spec = openapi.build_spec("http://x")["paths"]
    # Two long-standing aliases are documented under their primary method.
    known_aliases = {"POST /search", "PUT /settings"}
    missing = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/agent/v1"):
            continue
        sub = rule.rule[len("/api/agent/v1"):] or "/"
        tpl = re.sub(r"<(?:[a-z]+:)?([a-zA-Z_]+)>", r"{\1}", sub) or "/"
        alt = tpl.replace("{global_id}", "{spec}")
        for m in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            if f"{m} {tpl}" in known_aliases:
                continue
            if not ((tpl in spec and m.lower() in spec[tpl])
                    or (alt in spec and m.lower() in spec[alt])):
                missing.append(f"{m} {tpl}")
    assert missing == [], missing


def test_mcp_tools_cover_the_new_surface(monkeypatch):
    names = [t["name"] for t in mcp_server.TOOLS]
    for expected in ("update_folder", "move_meetings", "list_meetings_needing_speakers",
                     "review_meeting_speakers", "get_speaker_frames", "label_speaker",
                     "relabel_segment", "get_speaker_profile", "rename_speaker_profile",
                     "merge_speaker_profiles", "get_voice_library_health",
                     "plan_speaker_relabel", "apply_speaker_relabel", "cancel_speaker_relabel"):
        assert expected in names, expected
    assert len(names) == len(set(names))
    src = _read("mcp_server.py")
    for n in names:
        assert f'if name == "{n}":' in src, f"{n} has no dispatch branch"
    assert mcp_server.SERVER_VERSION == "1.1.0"
    # The orientation teaches the loop and the evidence rule.
    text = mcp_server._get_started()[0]["text"]
    assert "review_meeting_speakers" in text and "reinforce" in text

    calls = []

    def fake_http(method, path, params=None, body=None, timeout=60.0):
        calls.append((method, path, params, body))
        if path == "/folders/resolve":
            return {"resolved": True, "folder": {"id": "F1", "path": "Clients"}}
        if path.endswith("/frames"):
            return {"speaker_name": "Speaker 2", "speaker_key": "Speaker 2", "how_to_read": "Look.",
                    "frames": [{"t": 10.5, "start": 9.0, "end": 20.0, "text": "hello",
                                "jpeg_base64": base64.b64encode(b"jpg").decode()},
                               {"t": 30.0, "text": "x", "jpeg_base64": None, "note": "nope"}]}
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_http", fake_http)

    mcp_server.call_tool("update_folder", {"folder": "Clients", "name": "Customers", "parent": None})
    assert calls[-1][:2] == ("PATCH", "/folders/F1") and calls[-1][3] == {"name": "Customers", "parent": None}
    mcp_server.call_tool("move_meetings", {"meeting_ids": ["a", "b"], "folder": None})
    assert calls[-1] == ("POST", "/meetings/move", None, {"meeting_ids": ["a", "b"], "folder": None})
    mcp_server.call_tool("list_meetings_needing_speakers", {"within_days": 7, "reason": "unresolved"})
    assert calls[-1][1] == "/speakers/queue" and calls[-1][2]["within_days"] == 7
    mcp_server.call_tool("review_meeting_speakers", {"meeting_id": "m1", "speaker_key": "Speaker 2"})
    assert calls[-1][1] == "/meetings/m1/speakers/review" and calls[-1][2]["speaker_key"] == "Speaker 2"
    assert calls[-1][2]["detail"] is None
    mcp_server.call_tool("review_meeting_speakers", {"meeting_id": "m1", "include_named": True})
    assert calls[-1][2]["detail"] == "all"
    content, is_error = mcp_server.call_tool("get_speaker_frames", {"meeting_id": "m1", "speaker_key": "Speaker 2"})
    assert not is_error and calls[-1][1] == "/meetings/m1/speakers/Speaker%202/frames"
    assert [c["type"] for c in content] == ["text", "image", "text", "text"]
    assert "hello" in content[2]["text"] and "nope" in content[3]["text"]
    mcp_server.call_tool("label_speaker", {"meeting_id": "m1", "speaker_keys": ["Speaker 2"],
                                           "profile_id": "marcus", "evidence": "why"})
    assert calls[-1][1] == "/meetings/m1/speakers/label"
    assert calls[-1][3] == {"speaker_keys": ["Speaker 2"], "reinforce": False, "evidence": "why",
                            "global_id": "marcus"}
    mcp_server.call_tool("relabel_segment", {"meeting_id": "m1", "segment_id": 7, "speaker_key": "Speaker 1"})
    assert calls[-1][1] == "/meetings/m1/segments/7/speaker"
    mcp_server.call_tool("get_speaker_profile", {"speaker": "Dana W"})
    assert calls[-1][1] == "/speakers/Dana%20W"
    mcp_server.call_tool("rename_speaker_profile", {"profile_id": "p1", "name": "Dana"})
    assert calls[-1][:2] == ("PATCH", "/speakers/p1") and calls[-1][3] == {"name": "Dana"}
    mcp_server.call_tool("merge_speaker_profiles", {"keep_id": "k", "merge_id": "m", "confirm": True})
    assert calls[-1] == ("POST", "/speakers/k/merge", None, {"source_id": "m", "confirm": True})
    mcp_server.call_tool("get_voice_library_health", {})
    assert calls[-1][1] == "/speakers/library/health"
    mcp_server.call_tool("plan_speaker_relabel", {"from_name": "A", "to_name": "B", "scope": "session",
                                                  "meeting_id": "m1"})
    assert calls[-1][3]["session_id"] == "m1" and calls[-1][3]["scope"] == "session"
    mcp_server.call_tool("apply_speaker_relabel", {"token": "t", "confirm": True})
    assert calls[-1][3] == {"token": "t", "confirm": True}
    mcp_server.call_tool("cancel_speaker_relabel", {"token": "t"})
    assert calls[-1][1] == "/speakers/relabel/cancel"


def test_app_wires_the_ui_write_paths_into_the_agent_context():
    src = _read("app.py")
    wiring = src[src.index("register_agent_api(app, AgentContext("):]
    wiring = wiring[:wiring.index("))\n")]
    for needle in ("voice_library=fingerprint_db", "label_speaker=lambda sid, keys, name, color, gid, train: "
                   "_patch_session_speakers(", "apply_speaker_corrections=_apply_speaker_corrections",
                   "relabel_segment=_relabel_segment", "rename_profile=_rename_profile",
                   "merge_profiles=_apply_profile_merge", "relabel_deps=_relabel_deps",
                   "me_profile_id=lambda"):
        assert needle in wiring, needle
    # The UI's own paths took the two agent knobs and nothing else changed shape.
    sig = src[src.index("def _patch_session_speakers("):src.index("    updated_speakers = []")]
    assert 'global_id: "str | None" = None' in sig and "train_profile: bool = True" in sig
    body = src[src.index("def _sync_voice_profile(sid, keys, label, col, gid_hint, train):"):
               src.index("    # ── End auto-link")]
    assert "fingerprint_db.get_global_speaker(gid_hint) if gid_hint else None" in body
    assert "if not train:\n                    return" in body
    assert body.count("if not train:") == 1
    assert "def _relabel_segment(seg_id: int, label: str, source_override:" in src
    assert "if train and fingerprint_db.ready and label != _NOISE_LABEL:" in src
    assert "def _apply_speaker_corrections(session_id: str, proposed: list, noise_keys: list)" in src
    assert "def _rename_profile(global_id: str, name:" in src
    # The UI routes now call the helpers instead of carrying their own copies.
    assert src.count("fingerprint_db.apply_cluster_corrections(") == 1
    route = src[src.index("def fp_update_speaker("):src.index("def fp_delete_speaker(")]
    assert "_rename_profile(global_id, name=name or None, color=color)" in route
    assert "fingerprint_db.rename_global_speaker(" not in route


def test_ui_refreshes_when_an_agent_reorganises_the_library():
    js = _read("ui_web/static/app.js")
    assert "src.addEventListener('library_changed', () => {" in js
    block = js[js.index("src.addEventListener('library_changed'"):]
    block = block[:block.index("});")]
    assert "refreshSidebar();" in block and "'analytics', 'attention'" in block


def test_new_storage_helpers(data):
    s = _seed_library(data)
    full = storage.list_speaker_labels_full(s["s1"])
    assert full["Speaker 1"] == {"name": "Dana Whitfield", "color": "#58a6ff", "global_id": "dana", "is_noise": False}
    a = storage.create_folder("A")
    b = storage.create_folder("B")
    storage.set_folder_parent(b, a)
    assert storage.get_folder(b)["parent_id"] == a
    storage.set_folder_parent(b, None)
    assert storage.get_folder(b)["parent_id"] is None
    assert storage.get_folder("nope") is None
    att = storage.attention_by_session()
    assert att[s["s1"]]["needs"] is True and att[s["s2"]]["needs"] is False
