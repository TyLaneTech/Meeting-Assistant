"""The record of speaker changes, and their undo.

AI speaker detection (ai/speaker_detect) is allowed to act without asking, so
everything it changes has to come back exactly. Each change is written here
with the rows it touched as they were before and after (the meeting's speaker
labels and the transcript lines' overrides) and what it did to the voice
library: the voice samples it added (by row id) and the profiles it created.
Undo checks that those rows are still as the change left them, puts the
before-state back, deletes exactly those samples (rebuilding the centroids
they fed) and removes a profile it created once nothing else uses it.

A change can also be recorded as a suggestion: nothing applied, kept for the
user to accept from the meeting page. Accepting one applies it through the
same path as a run and journals it like any other.

Rows live in ``speaker_changes`` (core.storage). Actors: ``ai`` (a detection
run), ``user``, ``voice_auto`` (the voice library's own matches), ``agent``
(the Agent API), ``chat``. Risk classes: ``session`` (one meeting's names and
lines), ``link`` (a meeting speaker linked to a voice profile), ``profile``
(a profile created), ``train`` (voice samples added).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from core import log, storage


class Conflict(Exception):
    """The rows a change touched were changed again since; undoing it would
    throw that later change away."""

    def __init__(self, change_id: int, detail: str):
        super().__init__(detail)
        self.change_id = change_id
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(v) -> str | None:
    return None if v is None else json.dumps(v)


def _load(v):
    return json.loads(v) if v else None


def _snapshot_load(raw) -> dict:
    snap = _load(raw) or {}
    labels = snap.get("labels") or {}
    segments = {int(k): v for k, v in (snap.get("segments") or {}).items()}
    return {"labels": labels, "segments": segments}


def snapshot(session_id: str, keys=(), segment_ids=()) -> dict:
    """The rows a change is about to touch, as they are now."""
    return {
        "labels": storage.get_speaker_label_rows(session_id, sorted(set(keys))),
        "segments": storage.get_segment_overrides(sorted(set(segment_ids))),
    }


def record(*, session_id: str, actor: str, op: dict, state: str = "applied",
           run_id: str | None = None, seq: int = 0, risk: str | None = None,
           confidence: float | None = None, evidence=None, summary: str = "",
           before: dict | None = None, after: dict | None = None,
           effects: dict | None = None) -> int:
    """Write a change (or a suggestion, ``state="suggested"``). Returns its id."""
    now = _now()
    with storage._conn() as conn:
        cur = conn.execute(
            "INSERT INTO speaker_changes (run_id, session_id, seq, actor, op, risk, confidence, "
            "evidence, summary, state, before, after, effects, created_at, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, session_id, seq, actor, json.dumps(op), risk, confidence,
             _dump(evidence), summary, state, _dump(before), _dump(after), _dump(effects),
             now, now if state == "applied" else None))
        return int(cur.lastrowid)


def mark_applied(change_id: int, *, before: dict, after: dict, effects: dict) -> None:
    with storage._conn() as conn:
        conn.execute(
            "UPDATE speaker_changes SET state = 'applied', before = ?, after = ?, effects = ?, "
            "applied_at = ?, undone_at = NULL WHERE id = ?",
            (_dump(before), _dump(after), _dump(effects), _now(), change_id))


def set_state(change_id: int, state: str) -> None:
    with storage._conn() as conn:
        conn.execute("UPDATE speaker_changes SET state = ? WHERE id = ?", (state, change_id))


def claim(change_id: int, from_state: str = "suggested", to_state: str = "applying") -> bool:
    """Move a change from one state to another only if it is still in the
    first: two accepts of one suggestion (Apply all and a row's Apply, two
    tabs, a retried agent call) apply it once."""
    with storage._conn() as conn:
        cur = conn.execute("UPDATE speaker_changes SET state = ? WHERE id = ? AND state = ?",
                           (to_state, change_id, from_state))
        return cur.rowcount == 1


def supersede_suggestions(session_id: str, run_id: str, keys=None) -> int:
    """Retire suggestions earlier runs left waiting for this meeting (all of
    them, or those touching ``keys``): a new run re-decides from everything
    read so far, and two answers to one question would both be offered."""
    with storage._conn() as conn:
        rows = conn.execute(
            "SELECT id, op FROM speaker_changes WHERE session_id = ? AND state = 'suggested' "
            "AND (run_id IS NULL OR run_id != ?)", (session_id, run_id)).fetchall()
        ids = []
        for r in rows:
            op = _load(r["op"]) or {}
            touched = set(op.get("keys") or []) | {k for k in (op.get("to_key"),) if k}
            if keys is None or touched & set(keys):
                ids.append(r["id"])
        for i in ids:
            conn.execute("UPDATE speaker_changes SET state = 'superseded' WHERE id = ?", (i,))
    return len(ids)


def retire_name_suggestions(session_id: str, keys) -> int:
    """Someone named these speakers themselves: the name suggestions waiting
    for them are answered, whichever name was chosen. Moves stay: a line in
    another person's voice is still in the wrong place."""
    keys = set(keys or ())
    if not keys:
        return 0
    with storage._conn() as conn:
        rows = conn.execute(
            "SELECT id, op FROM speaker_changes WHERE session_id = ? AND state = 'suggested'",
            (session_id,)).fetchall()
        ids = []
        for r in rows:
            op = _load(r["op"]) or {}
            if op.get("type") == "name" and keys & set(op.get("keys") or []):
                ids.append(r["id"])
        for i in ids:
            conn.execute("UPDATE speaker_changes SET state = 'superseded' WHERE id = ?", (i,))
    return len(ids)


def expire_session(session_id: str) -> int:
    """A reanalysis rebuilt the meeting's speakers: earlier changes and
    suggestions name keys that now mean other voices, so none can be
    accepted or undone any more."""
    with storage._conn() as conn:
        cur = conn.execute(
            "UPDATE speaker_changes SET state = 'expired' WHERE session_id = ? "
            "AND state IN ('suggested', 'applied', 'applying')", (session_id,))
        return cur.rowcount


def _row(r) -> dict:
    d = dict(r)
    d["op"] = _load(d.get("op")) or {}
    d["evidence"] = _load(d.get("evidence"))
    d["effects"] = _load(d.get("effects")) or {}
    d["before"] = _snapshot_load(d.get("before"))
    d["after"] = _snapshot_load(d.get("after"))
    return d


def get(change_id: int) -> dict | None:
    with storage._conn() as conn:
        r = conn.execute("SELECT * FROM speaker_changes WHERE id = ?", (change_id,)).fetchone()
    return _row(r) if r else None


def list_changes(*, session_id: str | None = None, run_id: str | None = None,
                 states: tuple[str, ...] | None = None, limit: int = 500) -> list[dict]:
    """Newest first."""
    where, args = [], []
    if session_id:
        where.append("session_id = ?")
        args.append(session_id)
    if run_id:
        where.append("run_id = ?")
        args.append(run_id)
    if states:
        where.append(f"state IN ({','.join('?' * len(states))})")
        args.extend(states)
    sql = "SELECT * FROM speaker_changes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with storage._conn() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_row(r) for r in rows]


def _same(a: dict, b: dict) -> bool:
    return json.loads(json.dumps(a, sort_keys=True)) == json.loads(json.dumps(b, sort_keys=True))


def undo(change_id: int, fingerprint_db=None, *, force: bool = False) -> dict:
    """Put back what a change replaced. Returns {"session_id", "keys",
    "segment_ids", "profiles"} for the caller to refresh. Raises Conflict when
    those rows were changed again since (unless ``force``) and ValueError for
    a change that is not applied."""
    ch = get(change_id)
    if not ch or ch["state"] != "applied":
        raise ValueError("That change is not applied.")
    sid = ch["session_id"]
    before, after, effects = ch["before"], ch["after"], ch["effects"]
    keys = sorted(set(before["labels"]) | set(after["labels"]))
    seg_ids = sorted(set(before["segments"]) | set(after["segments"]))
    if not force:
        now_labels = storage.get_speaker_label_rows(sid, keys)
        now_segs = storage.get_segment_overrides(seg_ids)
        if not _same(now_labels, {k: after["labels"].get(k) for k in keys}) or \
                not _same({int(k): v for k, v in now_segs.items()},
                          {k: after["segments"].get(k) for k in seg_ids}):
            raise Conflict(change_id, "Those speakers were changed again after this change.")

    storage.put_speaker_label_rows(sid, {k: before["labels"].get(k) for k in keys})
    storage.put_segment_overrides({k: before["segments"].get(k) or
                                   {"source_override": None, "label_override": None}
                                   for k in seg_ids})
    profiles: set[str] = set()
    if fingerprint_db is not None:
        profiles.update(fingerprint_db.remove_embeddings(effects.get("embedding_ids") or []))
        for gid in effects.get("created_profiles") or []:
            try:
                if not fingerprint_db.profile_in_use(gid):
                    fingerprint_db.delete_global_speaker(gid)
                    profiles.discard(gid)
            except Exception as e:  # noqa: BLE001 - the rest of the undo stands
                log.warn("speakers", f"Could not remove profile {gid[:8]} on undo: {e}")
    with storage._conn() as conn:
        conn.execute("UPDATE speaker_changes SET state = 'undone', undone_at = ? WHERE id = ?",
                     (_now(), change_id))
    return {"session_id": sid, "keys": keys, "segment_ids": seg_ids,
            "profiles": sorted(profiles)}


def undo_run(run_id: str, fingerprint_db=None, *, force: bool = False) -> dict:
    """Undo a run's applied changes, the last applied first (a suggestion
    accepted later keeps its older id, so ids are not the order). Returns
    {"undone": [ids], "conflicts": [{"change_id", "detail"}], "sessions":
    [...]}. A conflict skips that change and the run's older changes to the
    same speakers stay."""
    done, conflicts, sessions = [], [], set()
    changes = sorted(list_changes(run_id=run_id, states=("applied",)),
                     key=lambda c: (c.get("applied_at") or "", c["id"]), reverse=True)
    for ch in changes:
        try:
            res = undo(ch["id"], fingerprint_db, force=force)
            done.append(ch["id"])
            sessions.add(res["session_id"])
        except Conflict as c:
            conflicts.append({"change_id": c.change_id, "detail": c.detail})
    return {"undone": done, "conflicts": conflicts, "sessions": sorted(sessions)}
