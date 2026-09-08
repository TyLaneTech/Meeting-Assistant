"""Speaker identification evidence for the Agent API.

An agent working through a meeting's unnamed speakers needs what a person
opening the Speakers dialog looks at, gathered into one answer: who is already
named, how much each unnamed voice spoke, what they said (a self-introduction
settles it), who the calendar invited, which Voice Library profiles sound
closest and how clearly, whether two diarizer keys are really one voice, and
the moments worth pulling a screen frame from. Everything here is read-only.
It works from storage plus the voice library object app.py owns (duck-typed,
so tests pass a fake); the writes live in agent_api/rest.py behind explicit
routes that reuse the UI's own code paths.

Thresholds are the library's own (ml/speaker_db.py): the verdicts reported
here say what the app itself would have done with the same evidence live.
"""
from __future__ import annotations

import re
from typing import Any, Callable

import numpy as np

from core import attention, storage

# Reserved per-session key for the owner's microphone. Never a candidate here.
ME_KEY = "me"
# Capture-source buckets from before diarization or from a mic-only session.
SOURCE_KEYS = ("loopback", "mic", "both")

# Fallbacks when the library object does not expose its thresholds.
_AUTO = 0.82
_MARGIN_FLOOR = 0.66
_MARGIN_GAP = 0.10
_SUGGEST = 0.70
_WEAK = 0.55
# Two diarizer keys this close are usually one voice (the Cleanup tab's
# cluster threshold).
SAME_VOICE_SIM = 0.70
# Two library profiles this close are hard to tell apart by voice.
CONFUSABLE_SIM = 0.75

# "This is Priya" / "I'm Marcus Chen" / "my name is ...". The trigger words
# carry their own case variants and the name must be Capitalised (a capital
# then a lowercase letter, so acronyms do not count): Whisper capitalises names
# mid-sentence and little else. Case-insensitive matching turned "It's
# probably less work" into an introduction of someone called Probably.
_SELF_INTRO_RE = re.compile(
    r"\b(?:[Tt]his is|I am|I'm|[Mm]y name is|[Mm]y name's)\s+"
    r"([A-Z][a-z][\w'\-]*(?:\s+[A-Z][a-z][\w'\-]*)?)"
)
# "Thanks, Dana" / "go ahead Dana" said by someone else right after Dana spoke.
_ADDRESS_AFTER_RE = re.compile(
    r"\b(?:[Tt]hanks|[Tt]hank you|[Gg]o ahead|[Oo]ver to you|[Ww]elcome|"
    r"[Gg]reat|[Gg]ood point|[Yy]es|[Yy]eah|[Rr]ight|[Aa]greed|[Ee]xactly|"
    r"[Aa]bsolutely|[Ss]ure|[Hh]i|[Hh]ey|[Hh]ello|[Mm]orning),?\s+"
    r"([A-Z][a-z]{2,})\b"
)
# "... what do you think, Dana?" said by someone else right before Dana spoke.
# The comma is required: "any other systems, Snowflake?" is a product, not a
# person, but a comma before a capitalised word at the end of a question is
# nearly always an address.
_ADDRESS_BEFORE_RE = re.compile(r",\s+([A-Z][a-z]{2,})[?]\s*$")
# Capitalised words that follow the triggers without being names.
_NOT_NAMES = {
    "a", "an", "the", "not", "just", "going", "gonna", "sure", "sorry", "here",
    "okay", "ok", "fine", "good", "great", "really", "very", "so", "also",
    "still", "actually", "happy", "glad", "wondering", "curious", "thinking",
    "looking", "trying", "able", "in", "on", "at", "back", "done", "right",
    "now", "today", "pretty", "all", "everyone", "everybody", "guys", "team",
    "folks", "it", "that", "this", "what", "why", "how", "where", "when",
    "who", "yes", "no", "yeah", "well", "true", "correct", "there", "again",
    "please", "then", "because", "if", "but", "and", "or", "we", "you", "i",
    "me", "us", "them", "they", "he", "she", "one", "two", "three", "first",
    "next", "last", "new", "old", "big", "small", "kind", "sort", "like",
    "about", "over", "out", "up", "down", "off", "away", "more", "less",
    "much", "many", "some", "any", "every", "each", "both", "either",
    "recording", "monday", "tuesday", "wednesday", "thursday", "friday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "zoom", "teams",
}


# ── Small helpers ─────────────────────────────────────────────────────────────

def effective_key(seg: dict) -> str:
    """The speaker key a transcript line resolves to (override wins)."""
    return seg.get("source_override") or seg.get("source") or "loopback"


def segments_by_key(segments: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for seg in sorted(segments, key=lambda s: (s.get("start_time") or 0.0, s.get("id") or 0)):
        out.setdefault(effective_key(seg), []).append(seg)
    return out


def _duration(seg: dict) -> float:
    return max(0.0, float(seg.get("end_time") or 0.0) - float(seg.get("start_time") or 0.0))


def _normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-8 else v


def norm_name(name: str) -> str:
    return " ".join(str(name or "").casefold().split())


def _plausible_name(word: str) -> bool:
    return bool(word) and word.casefold() not in _NOT_NAMES and len(word) >= 2


def _thresholds(library) -> dict:
    return {
        "strong": float(getattr(library, "AUTO_APPLY_THRESHOLD", _AUTO)),
        "margin_floor": float(getattr(library, "MARGIN_FLOOR", _MARGIN_FLOOR)),
        "margin_gap": float(getattr(library, "MARGIN_GAP", _MARGIN_GAP)),
        "possible": float(getattr(library, "SUGGEST_THRESHOLD", _SUGGEST)),
        "weak": _WEAK,
        "same_voice": SAME_VOICE_SIM,
    }


# ── The speaker inventory ─────────────────────────────────────────────────────

def speaker_rows(session_id: str, segments: list[dict], source_labels: dict,
                 me_id: str | None) -> list[dict]:
    """One row per effective speaker key in the meeting, with the label, the
    talk stats and a status the agent can act on:

    - ``named``: carries a real name (or is a capture-source bucket)
    - ``unnamed``: a generic name (Speaker 4) with enough speech to matter
    - ``minor``: generic and below the attention thresholds (a fragment or a
      diarizer phantom; usually noise or a sliver of another speaker)
    - ``noise``: flagged as noise in the Cleanup tab
    - ``me``: the owner's own microphone; never relabelled through the API
    """
    stats = storage.speaker_time_stats(session_id)
    labels = storage.list_speaker_labels_full(session_id)
    min_seconds, min_words = attention.get_attention_thresholds()
    by_key = segments_by_key(segments)
    rows = []
    for st in stats:
        key = st["speaker_key"]
        lab = labels.get(key) or {}
        raw_name = (lab.get("name") or st.get("name") or "").strip()
        display = raw_name or source_labels.get(key, key)
        global_id = lab.get("global_id") or st.get("global_id")
        is_me = key == ME_KEY or bool(me_id and global_id == me_id)
        is_noise = bool(lab.get("is_noise"))
        generic = attention.is_generic_speaker_name(raw_name or key)
        talk = float(st.get("talk_seconds") or 0.0)
        words = int(st.get("word_count") or 0)
        material = talk >= min_seconds or words >= min_words
        if is_me:
            status = "me"
        elif is_noise:
            status = "noise"
        elif not generic:
            status = "named"
        elif material:
            status = "unnamed"
        else:
            status = "minor"
        if key == ME_KEY:
            kind = "me"
        elif key in SOURCE_KEYS:
            kind = "source"
        elif key.startswith("custom:"):
            kind = "custom"
        else:
            kind = "diarized"
        segs = by_key.get(key, [])
        rows.append({
            "speaker_key": key,
            "name": display,
            "status": status,
            "kind": kind,
            "is_generic": generic,
            "is_noise": is_noise,
            "is_me": is_me,
            "global_id": global_id,
            "color": lab.get("color") or st.get("color"),
            "segment_count": int(st.get("segment_count") or 0),
            "talk_seconds": round(talk, 1),
            "word_count": words,
            "first_heard": round(float(segs[0].get("start_time") or 0.0), 1) if segs else None,
            "last_heard": round(float(segs[-1].get("end_time") or 0.0), 1) if segs else None,
        })
    order = {"unnamed": 0, "named": 1, "minor": 2, "noise": 3, "me": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), -r["talk_seconds"]))
    return rows


# ── Text evidence ─────────────────────────────────────────────────────────────

def self_introductions(segments: list[dict]) -> list[dict]:
    """Lines where the speaker appears to say their own name."""
    out = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        for m in _SELF_INTRO_RE.finditer(text):
            name = m.group(1).strip()
            first = name.split()[0]
            if not _plausible_name(first):
                continue
            out.append({
                "segment_id": seg.get("id"),
                "t": round(float(seg.get("start_time") or 0.0), 1),
                "name": name,
                "text": text[:200],
            })
            break
    return out


def quotes(segments: list[dict], n: int = 4, max_chars: int = 220) -> list[dict]:
    """The lines most likely to say who this is: any self-introduction, then
    the longest utterances, returned in meeting order."""
    intros = {q["segment_id"] for q in self_introductions(segments)}
    scored = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        score = _duration(seg) + len(text) / 25.0
        if seg.get("id") in intros:
            score += 1_000.0
        scored.append((score, seg))
    scored.sort(key=lambda p: -p[0])
    chosen = [seg for _, seg in scored[:max(1, n)]]
    chosen.sort(key=lambda s: s.get("start_time") or 0.0)
    return [{
        "segment_id": seg.get("id"),
        "t": round(float(seg.get("start_time") or 0.0), 1),
        "duration_sec": round(_duration(seg), 1),
        "text": (seg.get("text") or "").strip()[:max_chars],
        "self_introduction": seg.get("id") in intros,
    } for seg in chosen]


def name_hints(all_segments: list[dict], speaker_key: str,
               resolve: Callable[[dict], str], limit: int = 5) -> list[dict]:
    """Names other people appear to call this speaker.

    Two patterns on the lines that bracket this speaker's turns: someone else
    says "thanks, Dana" (or a greeting or agreement followed by a name) right
    after this speaker finishes, or asks "... Dana?" right before this speaker
    answers. A heuristic: the name may address a third person, so it is
    reported as a hint with the lines that produced it, never applied.
    """
    ordered = sorted(all_segments, key=lambda s: (s.get("start_time") or 0.0, s.get("id") or 0))
    hits: dict[str, dict] = {}

    def _note(name: str, seg: dict, why: str) -> None:
        if not _plausible_name(name):
            return
        entry = hits.setdefault(name.casefold(), {"name": name, "count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < 3:
            entry["examples"].append({
                "t": round(float(seg.get("start_time") or 0.0), 1),
                "by": resolve(seg),
                "text": (seg.get("text") or "").strip()[:160],
                "pattern": why,
            })

    for i, seg in enumerate(ordered):
        if effective_key(seg) != speaker_key:
            continue
        if i + 1 < len(ordered):
            nxt = ordered[i + 1]
            if effective_key(nxt) != speaker_key:
                m = _ADDRESS_AFTER_RE.search((nxt.get("text") or "").strip())
                if m:
                    _note(m.group(1), nxt, "addressed after speaking")
        if i > 0:
            prev = ordered[i - 1]
            if effective_key(prev) != speaker_key:
                m = _ADDRESS_BEFORE_RE.search((prev.get("text") or "").strip())
                if m:
                    _note(m.group(1), prev, "asked by name, then answered")
    out = sorted(hits.values(), key=lambda h: -h["count"])
    return out[:limit]


# ── Moments for screen frames ─────────────────────────────────────────────────

def moments(segments: list[dict], count: int = 3, lead_in: float = 1.5,
            min_duration: float = 2.0) -> list[dict]:
    """Timestamps while this speaker was talking, chosen for a screen frame:
    the longest utterances, spread across the meeting, a moment into each one
    (an active-speaker highlight takes a beat to light up)."""
    count = max(1, int(count))
    usable = [s for s in segments if _duration(s) >= min_duration] or list(segments)
    if not usable:
        return []
    longest = sorted(usable, key=_duration, reverse=True)[:count * 3]
    longest.sort(key=lambda s: s.get("start_time") or 0.0)
    if len(longest) > count:
        step = len(longest) / count
        longest = [longest[int(i * step)] for i in range(count)]
    out = []
    for seg in longest:
        start = float(seg.get("start_time") or 0.0)
        dur = _duration(seg)
        t = start + min(lead_in, max(0.2, dur / 2.0))
        out.append({
            "t": round(t, 2),
            "start": round(start, 2),
            "end": round(start + dur, 2),
            "text": (seg.get("text") or "").strip()[:160],
        })
    return out


# ── Voice evidence ────────────────────────────────────────────────────────────

def speaker_centroid(library, session_id: str, speaker_key: str,
                     global_id: str | None, segments: list[dict],
                     wav_path, *, allow_backfill: bool = True) -> tuple[np.ndarray | None, dict]:
    """The mean voice embedding for one speaker key in one meeting.

    Stored embeddings first (labelled or unlabelled, whatever the live
    pipeline kept); failing that, and only when the model is loaded, one is
    extracted from the recording's longest utterances and kept for next time,
    exactly as the Cleanup tab does.
    """
    info = {"embeddings": 0, "source": None}
    if library is None:
        return None, info
    embs: list = []
    try:
        embs = list(library._gather_speaker_embeddings(session_id, speaker_key, global_id) or [])
    except Exception:
        embs = []
    if embs:
        info.update(embeddings=len(embs), source="stored")
    elif allow_backfill and wav_path is not None and getattr(library, "ready", False) and segments:
        try:
            emb = library._backfill_embedding_from_wav(session_id, speaker_key, segments, str(wav_path))
        except Exception:
            emb = None
        if emb is not None:
            embs = [emb]
            info.update(embeddings=1, source="extracted_now")
    if not embs:
        return None, info
    return _normalize(np.stack(embs).mean(axis=0)), info


def verdict(similarity: float | None, gap: float | None, thresholds: dict) -> str:
    """What the app would do live with this match.

    ``strong``: applied silently. ``clear``: applied by the margin rule (a
    clear lead over the best differently named profile). ``possible``: only
    suggested to the user. ``weak``/``none``: not worth acting on by voice.
    """
    if similarity is None:
        return "none"
    if similarity >= thresholds["strong"]:
        return "strong"
    if similarity >= thresholds["margin_floor"] and gap is not None and gap >= thresholds["margin_gap"]:
        return "clear"
    if similarity >= thresholds["possible"]:
        return "possible"
    if similarity >= thresholds["weak"]:
        return "weak"
    return "none"


def library_matches(library, centroid: np.ndarray | None, *, top_k: int = 5,
                    exclude: set | None = None, counts: dict | None = None,
                    assigned: dict | None = None) -> dict:
    """Closest Voice Library profiles to a voice, with the margin verdict.

    ``assigned`` maps global_id to the speaker key already carrying that
    profile in this meeting: a match to one of those means the diarizer split
    a person, and the right action is same_as, not a second person.
    """
    thresholds = _thresholds(library)
    out: dict = {"candidates": [], "verdict": "none", "best": None,
                 "runner_up_gap": None, "thresholds": thresholds}
    if library is None or centroid is None:
        out["note"] = "No voice embedding for this speaker, so the library was not consulted."
        return out
    if not getattr(library, "ready", False):
        out["note"] = "The voice library model is not loaded; no voice matching is possible right now."
        return out
    try:
        raw = library.find_matches(centroid, exclude_global_ids=exclude or set(),
                                   top_k=max(top_k, 8), min_similarity=0.0) or []
    except Exception:
        raw = []
    counts = counts or {}
    assigned = assigned or {}
    cands = []
    for m in raw:
        gid = m.get("global_id")
        stats = counts.get(gid, {})
        cands.append({
            "global_id": gid,
            "name": m.get("name"),
            "similarity": round(float(m.get("similarity") or 0.0), 3),
            "session_count": stats.get("session_count", 0),
            "last_seen": stats.get("last_seen"),
            "already_in_this_meeting_as": assigned.get(gid),
        })
    cands = cands[:max(1, top_k)]
    out["candidates"] = cands
    if not cands:
        out["note"] = "The library has no profiles with voice samples to compare against."
        return out
    best = cands[0]
    runner = next((c for c in cands[1:] if norm_name(c["name"]) != norm_name(best["name"])), None)
    gap = round(best["similarity"] - runner["similarity"], 3) if runner else None
    out["best"] = best
    out["runner_up_gap"] = gap
    out["verdict"] = verdict(best["similarity"], gap if runner else 1.0, thresholds)
    if runner and gap is not None and gap < 0.05 and best["similarity"] >= thresholds["weak"]:
        out["note"] = (f'"{best["name"]}" and "{runner["name"]}" score within {gap:.3f} of each '
                       f"other, so the voice alone cannot separate them.")
    if best.get("already_in_this_meeting_as"):
        out["note"] = ((out.get("note") + " ") if out.get("note") else "") + (
            f'"{best["name"]}" is already speaker {best["already_in_this_meeting_as"]} in this '
            f"meeting; if this is the same voice, label with same_as instead of a second copy.")
    return out


def proximity(target_key: str, target: np.ndarray | None,
              others: list[tuple[str, str, str, np.ndarray | None]],
              same_voice: float = SAME_VOICE_SIM) -> list[dict]:
    """Similarity of one voice to every other speaker key in the meeting.

    ``others`` are (speaker_key, name, status, centroid). Sorted closest first;
    ``likely_same_voice`` marks pairs the Cleanup tab would cluster together.
    """
    if target is None:
        return []
    out = []
    for key, name, status, cent in others:
        if key == target_key or cent is None:
            continue
        sim = float(np.dot(target, cent))
        out.append({
            "speaker_key": key,
            "name": name,
            "status": status,
            "similarity": round(sim, 3),
            "likely_same_voice": sim >= same_voice,
        })
    out.sort(key=lambda p: -p["similarity"])
    return out


def trim_proximity(rows: list[dict], keep: int = 3, floor: float = 0.45,
                   cap: int = 8) -> list[dict]:
    """The proximity rows worth reading: every likely-same-voice pair, then
    anything above ``floor``, always at least the closest ``keep`` and never
    more than ``cap``. A long meeting has seventy keys; the agent needs the
    handful that might be this person, not the whole matrix."""
    out = [p for i, p in enumerate(rows)
           if i < keep or p["likely_same_voice"] or p["similarity"] >= floor]
    return out[:cap]


# ── Calendar evidence ─────────────────────────────────────────────────────────

def calendar_context(session_id: str, rows: list[dict],
                     load_candidates: Callable[[str], dict] | None = None) -> dict | None:
    """Who the calendar says was in the room, against who is already named."""
    times = storage.get_session_times(session_id) or {}
    match = storage.get_calendar_match(session_id) or {}
    if load_candidates is None:
        from core import calendar_sync
        load_candidates = calendar_sync.load_candidates
    try:
        cands = load_candidates(session_id) or {}
    except Exception:
        cands = {}
    people_raw = cands.get("candidates") if isinstance(cands.get("candidates"), list) else []
    hints_raw = cands.get("speaker_hints") if isinstance(cands.get("speaker_hints"), list) else []
    named = {norm_name(r["name"]): r["speaker_key"] for r in rows if r["status"] in ("named", "me")}
    people = []
    for p in people_raw:
        if not isinstance(p, dict) or not (p.get("name") or "").strip():
            continue
        name = p["name"].strip()
        assigned = named.get(norm_name(name))
        if assigned is None:
            # A first-name-only label still counts as assigned.
            first = norm_name(name).split()[0]
            assigned = next((k for n, k in named.items() if n.split()[0] == first), None)
        people.append({
            "name": name,
            "email": p.get("email") or "",
            "role": p.get("role") or "",
            "source": p.get("source") or "",
            "assigned_to": assigned,
        })
    expected = times.get("expected_speaker_count")
    if not (expected and match.get("cleared") is not True) and not people and not hints_raw:
        return None
    return {
        "subject": match.get("title") or (cands.get("meeting") or {}).get("calendar_subject") or None,
        "event_start": match.get("start"),
        "attendee_count": match.get("attendee_count"),
        "expected_speakers": expected,
        "expected_source": times.get("expected_speaker_source"),
        "people": people,
        "unassigned_people": [p["name"] for p in people if not p["assigned_to"]],
        "speaker_hints": [h for h in hints_raw if isinstance(h, (str, dict))][:20],
    }


# ── Disclosures ───────────────────────────────────────────────────────────────

def disclosures(*, library_ready: bool, has_video: bool, thresholds: dict) -> list[str]:
    out = [
        "Voice similarity is the cosine similarity between this speaker's voice and a "
        "library profile. Measured on this app's own libraries, precision is nearly flat "
        f"(about 83%) from 0.60 up to {thresholds['strong']:.2f}, so a single score is a hint, "
        "not an identification.",
        f"Verdicts: 'strong' (>= {thresholds['strong']:.2f}) is what the app applies on its own "
        f"live; 'clear' means the best profile beats the best differently named runner-up by "
        f"{thresholds['margin_gap']:.2f} or more at {thresholds['margin_floor']:.2f} or higher, "
        f"also applied live; 'possible' (>= {thresholds['possible']:.2f}) is only ever "
        "suggested to the user. Below that, do not label on voice alone.",
        f"In-meeting proximity of {thresholds['same_voice']:.2f} or more between two keys "
        "usually means the diarizer split one person; label them together with same_as "
        "rather than as two people.",
        "Quotes and name hints are text heuristics: a self-introduction is strong evidence; "
        "'thanks, Name' after a turn is weaker (it can address someone else). Read the lines.",
        "Calendar attendees are who was invited, not who spoke. Use them to choose between "
        "candidates. Assign by elimination only when one person is left AND the voice or the "
        "screen agrees.",
        "Labelling writes the name into this meeting, links the voice profile so the library "
        "knows the appearance, and never changes other meetings. Set reinforce only when the "
        "identity is certain: reinforcement teaches the profile, and a wrong one pollutes it.",
        "The owner's own microphone speaker (Me) is never a candidate and cannot be relabelled "
        "here.",
    ]
    if has_video:
        out.insert(3, "Screen frames (get_speaker_frames) show the meeting window while this "
                      "speaker talks: look for a highlighted or outlined tile, a 'Name is "
                      "speaking' banner, or a presenter name. Highlights can lag the audio by a "
                      "second or two, so compare several moments, and compare against a frame of "
                      "an already named speaker to learn the layout.")
    else:
        out.insert(3, "This meeting has no screen recording, so there is no visual evidence; "
                      "rely on voice, text and the calendar.")
    if not library_ready:
        out.insert(0, "The voice library model is not loaded right now, so no voice matching or "
                      "proximity was computed. Text, calendar and screen evidence still apply.")
    return out


# ── The review ────────────────────────────────────────────────────────────────

def review(session_id: str, *, library, source_labels: dict, me_id: str | None,
           speaker_key: str | None = None, include_matches: bool = True,
           quote_count: int = 4, top_k: int = 5, wav_path=None,
           has_video: bool = False, load_candidates=None,
           max_backfills: int = 12, detail: str = "unnamed") -> dict | None:
    """Everything an agent needs to decide who an unnamed speaker is.

    ``detail`` is "unnamed" (the default: the full evidence for every unnamed
    speaker and a compact row for everyone else under ``others``) or "all"
    (full evidence for every speaker but the owner). A ``speaker_key`` gets
    the full evidence whatever its status, with everyone else compact.
    """
    sess = storage.get_session(session_id)
    if not sess:
        return None
    segments = sess.get("segments", [])
    labels = sess.get("speaker_labels") or {}
    rows = speaker_rows(session_id, segments, source_labels, me_id)
    if speaker_key is not None and not any(r["speaker_key"] == speaker_key for r in rows):
        return {"error": "unknown_speaker", "speakers": [r["speaker_key"] for r in rows]}
    by_key = segments_by_key(segments)
    if detail not in ("unnamed", "all"):
        detail = "unnamed"

    def _detailed(r: dict) -> bool:
        if speaker_key is not None:
            return r["speaker_key"] == speaker_key
        if r["status"] == "me":
            return False
        return detail == "all" or r["status"] == "unnamed"

    def resolve(seg: dict) -> str:
        if seg.get("label_override"):
            return seg["label_override"]
        key = effective_key(seg)
        return labels.get(key) or source_labels.get(key, key)

    library_ready = bool(library is not None and getattr(library, "ready", False))
    thresholds = _thresholds(library)
    counts = storage.global_speaker_session_counts() if library_ready else {}
    assigned = {r["global_id"]: r["speaker_key"] for r in rows if r.get("global_id")}
    focus = [r for r in rows if _detailed(r)]
    compact = [r for r in rows if not _detailed(r)]

    # Voice centroids: the focused speakers plus every other material speaker
    # (proximity needs both sides). Backfills are capped so a long meeting with
    # many fragments does not turn a review into a minute of extraction.
    centroids: dict[str, tuple[np.ndarray | None, dict]] = {}
    backfills = 0
    if include_matches and library is not None:
        want = [r for r in rows if r["status"] in ("unnamed", "named")]
        want += [r for r in rows if r["status"] == "minor"]
        for r in want:
            key = r["speaker_key"]
            if key == ME_KEY or r["is_me"]:
                continue
            allow = r["status"] != "minor" and backfills < max_backfills
            cent, info = speaker_centroid(library, session_id, key, r.get("global_id"),
                                          by_key.get(key, []), wav_path, allow_backfill=allow)
            if info.get("source") == "extracted_now":
                backfills += 1
            centroids[key] = (cent, info)

    others = [(r["speaker_key"], r["name"], r["status"], centroids.get(r["speaker_key"], (None, {}))[0])
              for r in rows if not r["is_me"]]

    speakers = []
    for r in focus:
        key = r["speaker_key"]
        segs = by_key.get(key, [])
        entry = dict(r)
        entry["quotes"] = quotes(segs, quote_count)
        entry["self_introductions"] = self_introductions(segs)
        entry["name_hints"] = name_hints(segments, key, resolve) if r["status"] != "me" else []
        entry["frame_moments"] = moments(segs, 3) if has_video else []
        cent, info = centroids.get(key, (None, {"embeddings": 0, "source": None}))
        entry["voice"] = info
        if include_matches and r["status"] not in ("me", "noise"):
            entry["library_matches"] = library_matches(
                library, cent, top_k=top_k, counts=counts, assigned=assigned,
                exclude={me_id} if me_id else set())
            entry["proximity"] = trim_proximity(
                proximity(key, cent, others, thresholds["same_voice"]))
        speakers.append(entry)

    # Everyone else in one line each, with the key they sound most like: enough
    # to spot a named speaker whose voice an unnamed key matches, without the
    # quotes and candidates that a seventy-key meeting would multiply.
    compact_rows = []
    for r in compact:
        key = r["speaker_key"]
        entry = {k: r[k] for k in ("speaker_key", "name", "status", "kind", "global_id",
                                   "talk_seconds", "segment_count", "word_count")}
        cent = centroids.get(key, (None, {}))[0]
        if include_matches and cent is not None and not r["is_me"]:
            prox = proximity(key, cent, others, thresholds["same_voice"])
            if prox:
                entry["closest_other"] = prox[0]
        compact_rows.append(entry)

    out = {
        "session_id": session_id,
        "title": sess.get("title"),
        "started_at": sess.get("started_at"),
        "duration_sec": round(max((s.get("end_time") or 0.0) for s in segments), 1) if segments else 0.0,
        "has_video": has_video,
        "attention": storage.get_session_attention(session_id),
        "calendar": calendar_context(session_id, rows, load_candidates),
        "library": {"ready": library_ready,
                    "profiles_with_voice": len(counts) if library_ready else None},
        "detail": "speaker" if speaker_key is not None else detail,
        "speakers": speakers,
        "others": compact_rows,
        "counts": {s: sum(1 for r in rows if r["status"] == s)
                   for s in ("unnamed", "named", "minor", "noise", "me")},
        "disclosures": disclosures(library_ready=library_ready, has_video=has_video,
                                   thresholds=thresholds),
    }
    if compact_rows:
        out["disclosures"].append(
            "speakers carries the full evidence; others lists every other speaker in one "
            "line with closest_other (the key its voice is nearest to). Pass speaker_key "
            "for one of them, or detail=all, to get their full evidence.")
    return out
