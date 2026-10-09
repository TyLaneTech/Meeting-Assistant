"""The visual timeline: what the meeting app showed, moment by moment.

An observation belongs to a meeting second, never to a speaker key, so it
stays true when a reanalysis renames the keys or a user moves lines, and a
rerun reuses it instead of paying for the same look twice. Boxes are kept in
video pixels whatever crop or size the model was shown.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core import storage

from ai.speaker_detect import prompts

CUE_WEIGHT = {"banner": 1.0, "caption": 1.0, "main_tile": 0.8, "border": 0.8,
              "audio_indicator": 0.5, "other": 0.3}


@dataclass
class Sighting:
    label: str | None
    cue: str
    confidence: float
    box: list[float] | None
    self_view: bool
    person: str | None = None          # filled in by names.NameBook
    profile_id: str | None = None
    known: bool = False


@dataclass
class Observation:
    t: float
    kind: str
    meeting_visible: bool
    app: str
    layout: str
    speaking: list[Sighting]
    roster: list[dict] = field(default_factory=list)
    participants_box: list[float] | None = None
    pinned: bool = False
    legibility: str = "good"
    crop: list[float] | None = None
    model: str = ""
    prompt_version: str = prompts.PROMPT_VERSION
    id: int | None = None
    ref: int | None = None             # its preview in the run's FrameStore (not stored)


def _to_video(box, crop, img_size, video_size):
    """Map an image-pixel box to video pixels."""
    if not box or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    # A model can give the corners in either order.
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    iw, ih = img_size
    if crop:
        cx0, cy0, cx1, cy1 = crop
        sx, sy = (cx1 - cx0) / max(1, iw), (cy1 - cy0) / max(1, ih)
        ox, oy = cx0, cy0
    else:
        if not video_size:
            return None          # the recording's size could not be read
        vw, vh = video_size
        sx, sy = vw / max(1, iw), vh / max(1, ih)
        ox = oy = 0.0
    return [round(ox + x0 * sx, 1), round(oy + y0 * sy, 1),
            round(ox + x1 * sx, 1), round(oy + y1 * sy, 1)]


def parse(frame: dict, *, t: float, kind: str, crop, img_size, video_size,
          model: str) -> Observation:
    """One frame of a model answer as an observation (defensive: a missing or
    odd field never raises)."""
    def _b(v):
        return bool(v) if isinstance(v, bool) else str(v).lower() == "true"

    speaking = []
    for s in frame.get("speaking") or []:
        if not isinstance(s, dict):
            continue
        try:
            conf = max(0.0, min(1.0, float(s.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        cue = s.get("cue") if s.get("cue") in prompts.CUES else "other"
        label = s.get("label")
        speaking.append(Sighting(
            label=label.strip() if isinstance(label, str) and label.strip() else None,
            cue=cue, confidence=conf,
            box=_to_video(s.get("box"), crop, img_size, video_size),
            self_view=_b(s.get("self_view", False))))
    roster = []
    for r in frame.get("roster") or []:
        if isinstance(r, dict) and isinstance(r.get("label"), str) and r["label"].strip():
            roster.append({"label": r["label"].strip(),
                           "box": _to_video(r.get("box"), crop, img_size, video_size),
                           "self_view": _b(r.get("self_view", False))})
    return Observation(
        t=round(float(t), 2), kind=kind,
        meeting_visible=_b(frame.get("meeting_visible", False)),
        app=frame.get("app") if frame.get("app") in prompts.APPS else "unknown",
        layout=frame.get("layout") if frame.get("layout") in prompts.LAYOUTS else "other",
        speaking=speaking, roster=roster,
        participants_box=_to_video(frame.get("participants_box"), crop, img_size, video_size),
        pinned=_b(frame.get("pinned_or_spotlight", False)),
        legibility=frame.get("legibility") if frame.get("legibility") in
        ("good", "partial", "poor") else "good",
        crop=list(crop) if crop else None, model=model)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def store(session_id: str, obs: Observation, run_id: str | None, t_video: float | None) -> int:
    with storage._conn() as conn:
        cur = conn.execute(
            "INSERT INTO speaker_observations (session_id, t_audio, t_video, kind, crop, model, "
            "prompt_version, meeting_visible, app, layout, speaking, roster, flags, run_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, obs.t, t_video, obs.kind, json.dumps(obs.crop) if obs.crop else None,
             obs.model, obs.prompt_version, int(obs.meeting_visible), obs.app, obs.layout,
             json.dumps([{"label": s.label, "cue": s.cue, "confidence": s.confidence,
                          "box": s.box, "self_view": s.self_view} for s in obs.speaking]),
             json.dumps({"roster": obs.roster, "participants_box": obs.participants_box}),
             json.dumps({"pinned": obs.pinned, "legibility": obs.legibility}),
             run_id, _now()))
        obs.id = int(cur.lastrowid)
        return obs.id


def load(session_id: str, prompt_version: str = prompts.PROMPT_VERSION) -> list[Observation]:
    """Stored observations for a meeting from this prompt version, by time."""
    with storage._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM speaker_observations WHERE session_id = ? AND prompt_version = ? "
            "ORDER BY t_audio, id", (session_id, prompt_version)).fetchall()
    out = []
    for r in rows:
        extra = json.loads(r["roster"] or "{}") or {}
        flags = json.loads(r["flags"] or "{}") or {}
        out.append(Observation(
            t=r["t_audio"], kind=r["kind"], meeting_visible=bool(r["meeting_visible"]),
            app=r["app"] or "unknown", layout=r["layout"] or "other",
            speaking=[Sighting(s.get("label"), s.get("cue") or "other",
                               float(s.get("confidence") or 0.0), s.get("box"),
                               bool(s.get("self_view")))
                      for s in json.loads(r["speaking"] or "[]")],
            roster=extra.get("roster") or [], participants_box=extra.get("participants_box"),
            pinned=bool(flags.get("pinned")), legibility=flags.get("legibility") or "good",
            crop=json.loads(r["crop"]) if r["crop"] else None, model=r["model"] or "",
            prompt_version=r["prompt_version"] or "", id=r["id"]))
    return out


def get(obs_id: int) -> tuple[str, Observation] | None:
    with storage._conn() as conn:
        r = conn.execute("SELECT session_id, prompt_version FROM speaker_observations "
                         "WHERE id = ?", (obs_id,)).fetchone()
    if not r:
        return None
    for o in load(r["session_id"], r["prompt_version"]):
        if o.id == obs_id:
            return r["session_id"], o
    return None
