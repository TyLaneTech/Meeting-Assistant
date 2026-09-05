"""Ties the published calendar feed to recordings.

``core.calendar_feed`` reads the ICS feed; this module is where the side
effects live: it refreshes the cache, matches every recording to a calendar
instance, stores the match plus an expected speaker count, and merges the
attendee list into the candidates file the Speakers Cleanup tab reads.

Nothing here is destructive and nothing here starts a reanalysis. The smart
cleanup plan is computed here; only an explicit user action (the apply path in
app.py) ever runs it, and names reach voices only through the Voice Library
auto-match that reanalysis performs on its own.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from core import calendar_feed as calendar_feed
from core import log as log
from core import paths as paths
from core import settings as settings
from core import storage as storage

UTC = timezone.utc

# How far the expansion window reaches. Recordings are matched backwards over
# the library's recent history; the forward end only has to cover meetings the
# owner is about to record.
WINDOW_BACK_DAYS = 120
WINDOW_FORWARD_DAYS = 45

# Score below which a match is not worth storing.
MIN_MATCH_SCORE = 0.5

MAX_ALTERNATIVES = 3

# No real recording runs longer than this; a span beyond it is a healed
# ended_at from a crash, not a meeting.
MAX_RECORDING_SECONDS = 6 * 3600
# ended_at may legitimately trail the last transcript segment by a little
# (silence at the end); beyond this it is a healed value, not the meeting.
HEALED_END_GRACE_SECONDS = 90 * 60
# A transcript-derived span never shrinks below this, so a quiet recording
# still overlaps the meeting it was made in.
MIN_TRANSCRIPT_SPAN_SECONDS = 15 * 60
# A recording with no ended_at and no transcript still needs a span.
FALLBACK_RECORDING_MINUTES = 30


# ── Small helpers ────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """The module's only clock. Tests freeze this so fixtures do not expire."""
    return datetime.now(UTC)


def _now_iso() -> str:
    return _utcnow().replace(microsecond=0).isoformat()


def _prefs() -> dict:
    return settings.load()


def _timezone_name() -> str:
    return (settings.get("calendar_timezone") or calendar_feed.DEFAULT_TIMEZONE).strip() \
        or calendar_feed.DEFAULT_TIMEZONE


def _match_window() -> int:
    try:
        return max(1, int(settings.get("calendar_match_window_minutes", 20) or 20))
    except (TypeError, ValueError):
        return 20


def _refresh_minutes() -> int:
    try:
        return max(15, int(settings.get("calendar_refresh_minutes", 60) or 60))
    except (TypeError, ValueError):
        return 60


def _session_span(session: dict):
    """Return (start_utc, end_utc) for a session row, both aware UTC.

    started_at / ended_at are naive UTC strings; a session that never ended
    falls back to its last transcript segment, then to a fixed span.
    """
    start = calendar_feed.parse_iso_utc(session.get("started_at"))
    if start is None:
        return None, None
    end = calendar_feed.parse_iso_utc(session.get("ended_at"))
    # A session healed after a crash or freeze can carry an ended_at hours or
    # days after its start. The transcript's last segment is the honest span:
    # whenever ended_at overshoots it by more than a grace period, or exceeds
    # any plausible recording, the transcript wins.
    try:
        segment_seconds = float(session.get("last_segment_time") or 0.0)
    except (TypeError, ValueError):
        segment_seconds = 0.0
    if end is not None:
        span = (end - start).total_seconds()
        # Healed values overshoot by hours; a quiet meeting with little
        # transcript does not. Both conditions must hold to distrust ended_at.
        overshoot = (segment_seconds > 0
                     and span > segment_seconds + HEALED_END_GRACE_SECONDS
                     and span > 2 * segment_seconds)
        if span > MAX_RECORDING_SECONDS or overshoot:
            end = None
    if end is None or end <= start:
        seconds = session.get("last_segment_time")
        try:
            seconds = float(seconds) if seconds is not None else 0.0
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds > 0:
            seconds = max(MIN_TRANSCRIPT_SPAN_SECONDS, min(seconds, MAX_RECORDING_SECONDS))
            end = start + timedelta(seconds=seconds)
        else:
            end = start + timedelta(minutes=FALLBACK_RECORDING_MINUTES)
    return start, end


def _instance_summary(instance) -> dict:
    return {
        "uid": instance.uid,
        "recurrence_id": instance.recurrence_id.isoformat() if instance.recurrence_id else None,
        "title": "Private appointment" if instance.is_private else instance.summary,
        "start": instance.start.isoformat() if instance.start else None,
        "end": instance.end.isoformat() if instance.end else None,
    }


def _count_for(instance) -> tuple:
    """Return (count, source) where source is 'calendar', 'memory' or ''."""
    count = calendar_feed.expected_count(instance)
    if count:
        return count, "calendar"
    remembered = calendar_feed.recall_expected_count(
        calendar_feed.normalize_title(instance.summary)
    )
    if remembered:
        return remembered, "memory"
    return None, ""


def build_match(scored: dict, alternatives, confirmed: bool = False) -> dict:
    """Build the stored calendar_match payload from a scored match.

    A guessed timezone rides along in the reason so it surfaces in the
    reanalyze dialog: an hours-wrong match is otherwise invisible.
    """
    instance = scored["instance"]
    count, count_source = _count_for(instance)
    reason = scored.get("reason", "")
    if getattr(instance, "tz_note", ""):
        reason = f"{reason} ({instance.tz_note})" if reason else instance.tz_note
    payload = {
        **_instance_summary(instance),
        "organizer": instance.organizer or {},
        "attendees": list(instance.attendees or [])[:40],
        "attendee_count": count,
        "attendee_count_source": count_source,
        "is_private": bool(instance.is_private),
        "score": scored.get("score"),
        "overlap_seconds": scored.get("overlap_seconds"),
        "start_delta_seconds": scored.get("start_delta_seconds"),
        "reason": reason,
        "matched_at": _now_iso(),
        "confirmed": bool(confirmed),
        "alternatives": [
            {
                **_instance_summary(alt["instance"]),
                "score": alt.get("score"),
                "reason": alt.get("reason", ""),
                "attendee_count": calendar_feed.expected_count(alt["instance"]),
            }
            for alt in (alternatives or [])[:MAX_ALTERNATIVES]
        ],
    }
    return payload


# ── Resolution candidates merge ──────────────────────────────────────────────

def candidates_path(session_id: str):
    return paths.data_dir() / "resolution_candidates" / f"{session_id}.json"


class CandidatesUnreadable(Exception):
    """The candidates file exists, holds bytes, and does not parse."""


def _read_candidates(session_id: str) -> dict:
    """Return the parsed candidates file, or raise if it exists but is broken.

    A missing or empty file is a normal starting point. A file with content
    that will not parse is a half-written or corrupted file holding hand-fed
    hints, so callers must leave it alone rather than replace it.
    """
    path = candidates_path(session_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise CandidatesUnreadable(str(exc)) from exc
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CandidatesUnreadable(str(exc)) from exc
    return data if isinstance(data, dict) else {}


def _load_candidates(session_id: str) -> dict:
    """Best-effort read for callers that only display what is there."""
    try:
        return _read_candidates(session_id)
    except CandidatesUnreadable:
        return {}


def merge_candidates(session_id: str, instance, match: dict) -> bool:
    """Fold the calendar's meeting fields and attendees into the candidates file.

    Only calendar-owned keys are touched: hand-fed hints, candidates from other
    sources, and the meeting title/date already in the file are left alone. A
    file that exists but does not parse is never overwritten: it may be a
    half-written copy of hand-fed data, and this runs from a background thread.
    """
    try:
        payload = _read_candidates(session_id)
    except CandidatesUnreadable as exc:
        log.warn(
            "calendar",
            f"Resolution candidates for {session_id[:8]} are unreadable, "
            f"leaving the file untouched: {exc}",
        )
        return False
    meeting = payload.get("meeting")
    if not isinstance(meeting, dict):
        meeting = {}
    meeting["calendar_subject"] = match.get("title") or ""
    meeting["calendar_uid"] = match.get("uid") or ""
    # A re-match onto an event with no attendees must not leave the previous
    # event's count behind: that count drives the reanalysis ceiling.
    meeting["attendee_count"] = match.get("attendee_count") or None
    payload["meeting"] = meeting

    existing = payload.get("candidates")
    kept = [
        person for person in (existing if isinstance(existing, list) else [])
        if isinstance(person, dict) and person.get("source") != "calendar"
    ]
    fresh = [] if match.get("is_private") else calendar_feed.candidate_people(instance)
    seen = {
        (person.get("email") or person.get("name") or "").strip().lower()
        for person in kept
    }
    merged = kept + [
        person for person in fresh
        if (person.get("email") or person.get("name") or "").strip().lower() not in seen
    ]
    payload["candidates"] = merged
    if not isinstance(payload.get("speaker_hints"), list):
        payload["speaker_hints"] = []
    payload["generated_at"] = _now_iso()

    return _write_candidates(session_id, payload)


def _write_candidates(session_id: str, payload: dict) -> bool:
    """Write the candidates file atomically (temp file, then replace).

    The hourly refresh writes this from a background thread while the Cleanup
    tab may be reading it, so a reader must never see a half-written file.
    """
    path = candidates_path(session_id)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        tmp.replace(path)
    except OSError as exc:
        log.warn("calendar", f"Could not write resolution candidates: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


# ── Feed loading ─────────────────────────────────────────────────────────────

def load_feed(url: str) -> dict:
    """Fetch, parse and expand one feed. Raises CalendarFeedError on failure."""
    text = calendar_feed.fetch_ics(url, timeout=20)
    notes: list = []
    events = calendar_feed.parse_ics(text, default_tz=_timezone_name(), notes=notes)
    now = _utcnow()
    instances = calendar_feed.expand(
        events,
        now - timedelta(days=WINDOW_BACK_DAYS),
        now + timedelta(days=WINDOW_FORWARD_DAYS),
    )
    return {
        "events": events,
        "instances": instances,
        "notes": notes,
        "event_count": len(events),
        "instance_count": len(instances),
        "has_attendees_ratio": calendar_feed.attendee_coverage(instances),
    }


def test_link(url: str) -> dict:
    """Fetch and parse a candidate link without saving anything."""
    result = {
        "ok": False,
        "event_count": 0,
        "instance_count": 0,
        "first_start": None,
        "last_start": None,
        "has_attendees_ratio": 0.0,
        "sample_titles": [],
        "timezone_notes": [],
        "error": "",
    }
    try:
        feed = load_feed(url)
    except calendar_feed.CalendarFeedError as exc:
        result["error"] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - the URL must never leak
        result["error"] = f"Could not read the calendar ({type(exc).__name__})."
        return result

    instances = feed["instances"]
    starts = [inst.start for inst in instances if inst.start]
    result.update({
        "ok": True,
        "event_count": feed["event_count"],
        "instance_count": feed["instance_count"],
        "first_start": min(starts).isoformat() if starts else None,
        "last_start": max(starts).isoformat() if starts else None,
        "has_attendees_ratio": feed["has_attendees_ratio"],
        "sample_titles": [
            "Private appointment" if inst.is_private else (inst.summary or "(no title)")
            for inst in instances[:5]
        ],
        "timezone_notes": feed["notes"],
    })
    return result


# ── Refresh ──────────────────────────────────────────────────────────────────

def refresh(force: bool = False, active_session_id: str | None = None) -> dict:
    """Refresh the feed and re-match every recording. Never raises.

    Sessions whose match the user confirmed are left alone, as is the session
    currently recording. A machine match that no longer has a calendar event
    behind it is cleared, along with the expected count it contributed.
    """
    summary = {
        "ok": False,
        "skipped": "",
        "matched": 0,
        "unmatched": 0,
        "updated": 0,
        "cleared": 0,
        "event_count": 0,
        "instance_count": 0,
        "has_attendees_ratio": 0.0,
        "error": "",
        "last_refresh": settings.get("calendar_last_refresh", "") or "",
    }
    try:
        prefs = _prefs()
        if not force and not prefs.get("calendar_enabled"):
            summary["skipped"] = "disabled"
            return summary
        url = (prefs.get("calendar_ics_url") or "").strip()
        if not url:
            summary["error"] = "No calendar link saved."
            summary["skipped"] = "no_link"
            return summary

        try:
            feed = load_feed(url)
        except calendar_feed.CalendarFeedError as exc:
            summary["error"] = str(exc)
            settings.update({"calendar_last_error": str(exc)})
            log.warn("calendar", f"Calendar refresh failed: {exc}")
            return summary

        instances = feed["instances"]
        summary["event_count"] = feed["event_count"]
        summary["instance_count"] = feed["instance_count"]
        summary["has_attendees_ratio"] = feed["has_attendees_ratio"]

        window = _match_window()
        now = _utcnow()
        expansion_start = now - timedelta(days=WINDOW_BACK_DAYS)
        expansion_end = now + timedelta(days=WINDOW_FORWARD_DAYS)
        matched = unmatched = updated = cleared = 0
        for session in storage.list_sessions():
            session_id = session.get("id")
            if not session_id or session_id == active_session_id:
                continue
            existing = session.get("calendar_match") or {}
            if existing.get("confirmed"):
                # Both a pinned meeting and a "not a calendar meeting"
                # tombstone are user decisions; neither is re-matched.
                if is_cleared(existing):
                    unmatched += 1
                else:
                    matched += 1
                continue
            start, end = _session_span(session)
            if start is None:
                continue
            # A recording older than the expansion window has no candidate
            # instances by construction. "No candidates" there means "not
            # looked at", not "the meeting was deleted", so leave its stored
            # match and count alone instead of clearing them every hour.
            if not (expansion_start <= start <= expansion_end):
                if existing:
                    matched += 1
                continue
            result = calendar_feed.match_session(instances, start, end, window)
            best = result.get("best")
            if best and best.get("score", 0) >= MIN_MATCH_SCORE:
                match = build_match(best, result.get("alternatives"))
                storage.set_calendar_match(session_id, match)
                # set_calendar_match adopts a known count; it cannot know that
                # a NEW match without attendees should retire the old ceiling.
                if (not match.get("attendee_count")
                        and session.get("expected_speaker_source") == "calendar"):
                    storage.set_expected_speaker_count(session_id, None, "calendar")
                if merge_candidates(session_id, best["instance"], match):
                    updated += 1
                matched += 1
            else:
                if existing:
                    storage.set_calendar_match(session_id, None)
                    if session.get("expected_speaker_source") == "calendar":
                        storage.set_expected_speaker_count(session_id, None, "calendar")
                    cleared += 1
                unmatched += 1

        summary.update({
            "ok": True,
            "matched": matched,
            "unmatched": unmatched,
            "updated": updated,
            "cleared": cleared,
            "last_refresh": _now_iso(),
        })
        calendar_feed.save_cache({
            "fetched_at": summary["last_refresh"],
            "event_count": summary["event_count"],
            "instances": [inst.to_dict() for inst in instances],
            "has_attendees_ratio": summary["has_attendees_ratio"],
            "matched_sessions": matched,
            "timezone_notes": feed["notes"],
            "error": "",
        })
        settings.update({
            "calendar_last_refresh": summary["last_refresh"],
            "calendar_last_error": "",
        })
        log.info(
            "calendar",
            f"Calendar refresh: {summary['instance_count']} instances, "
            f"{matched} matched, {unmatched} unmatched, {cleared} cleared",
        )
        return summary
    except Exception as exc:  # noqa: BLE001 - a background loop calls this
        summary["error"] = f"Calendar refresh failed ({type(exc).__name__})."
        try:
            settings.update({"calendar_last_error": summary["error"]})
        except Exception:
            pass
        log.warn("calendar", summary["error"])
        return summary


def refresh_due() -> bool:
    """True when the interval since the last refresh has elapsed."""
    last = calendar_feed.parse_iso_utc(settings.get("calendar_last_refresh", "") or "")
    if last is None:
        return True
    return (_utcnow() - last) >= timedelta(minutes=_refresh_minutes())


def next_refresh_due() -> str:
    last = calendar_feed.parse_iso_utc(settings.get("calendar_last_refresh", "") or "")
    if last is None:
        return ""
    return (last + timedelta(minutes=_refresh_minutes())).replace(microsecond=0).isoformat()


def status() -> dict:
    """Everything the Calendar settings tab needs, with the URL masked."""
    prefs = _prefs()
    cache = calendar_feed.load_cache()
    instances = cache.get("instances")
    return {
        "enabled": bool(prefs.get("calendar_enabled")),
        "url_masked": calendar_feed.mask_url(prefs.get("calendar_ics_url") or ""),
        "has_url": bool((prefs.get("calendar_ics_url") or "").strip()),
        "timezone": _timezone_name(),
        "refresh_minutes": _refresh_minutes(),
        "match_window_minutes": _match_window(),
        "last_refresh": prefs.get("calendar_last_refresh", "") or "",
        "last_error": prefs.get("calendar_last_error", "") or "",
        "event_count": cache.get("event_count") or 0,
        "instance_count": len(instances) if isinstance(instances, list) else 0,
        "has_attendees_ratio": cache.get("has_attendees_ratio") or 0.0,
        "matched_sessions": cache.get("matched_sessions") or 0,
        "timezone_notes": cache.get("timezone_notes") or [],
        "next_refresh_due": next_refresh_due(),
    }


# ── The calendar link (a credential) ─────────────────────────────────────────

# The one settings key the generic preferences route may never write. Every tab
# holds a MASKED copy of it after a GET, and a tab opened before the link was
# saved holds an empty string, so any whole-object save would either blank the
# credential or replace it with its own mask.
PROTECTED_SETTINGS_KEYS = ("calendar_ics_url",)


def sanitize_preferences(updates: dict) -> dict:
    """Strip keys the generic preferences route must not write."""
    if not isinstance(updates, dict):
        return {}
    for key in PROTECTED_SETTINGS_KEYS:
        updates.pop(key, None)
    return updates


def set_link(url: str) -> dict:
    """Store the ICS link. The only writer of calendar_ics_url, with clear_link."""
    candidate = (url or "").strip()
    if not candidate.lower().startswith("https://"):
        return {
            "ok": False,
            "error": "The calendar link must start with https:// . Copy the ICS "
                     "link from Outlook's Publish a calendar page.",
        }
    settings.update({"calendar_ics_url": candidate, "calendar_last_error": ""})
    return {"ok": True, "url_masked": calendar_feed.mask_url(candidate)}


def clear_link() -> dict:
    """Forget the stored link, switch the feed off, and empty the cache."""
    settings.update({
        "calendar_ics_url": "",
        "calendar_enabled": False,
        "calendar_last_refresh": "",
        "calendar_last_error": "",
    })
    calendar_feed.save_cache({
        "instances": [],
        "event_count": 0,
        "matched_sessions": 0,
        "has_attendees_ratio": 0.0,
        "timezone_notes": [],
        "fetched_at": "",
    })
    log.info("calendar", "Calendar link removed.")
    return {"ok": True, "cleared": True, "url_masked": ""}


# ── Per-session match handling ───────────────────────────────────────────────

def is_cleared(match) -> bool:
    """True for the tombstone written by "Not a calendar meeting"."""
    return bool(isinstance(match, dict) and match.get("cleared"))


def get_match(session_id: str) -> dict:
    match = storage.get_calendar_match(session_id) or {}
    return {
        "match": None if is_cleared(match) else (match or None),
        "alternatives": [] if is_cleared(match) else (match.get("alternatives") or []),
        "cleared": is_cleared(match),
    }


def _find_instance(uid: str, recurrence_id, near=None):
    """Find a cached instance by uid, and by recurrence when one is given.

    Without a recurrence id, a recurring uid matches every occurrence in the
    window, so the one nearest ``near`` (the recording's start) wins. Taking
    the first would pin a weekly meeting to whichever occurrence the expansion
    happened to emit first, months away from the recording.
    """
    wanted = calendar_feed.parse_iso_utc(recurrence_id)
    anchor = calendar_feed.parse_iso_utc(near)
    candidates = [inst for inst in calendar_feed.cached_instances() if inst.uid == uid]
    if not candidates:
        return None
    if wanted is not None:
        for instance in candidates:
            if instance.start == wanted or instance.recurrence_id == wanted:
                return instance
    if anchor is not None:
        return min(
            candidates,
            key=lambda inst: abs((inst.start - anchor).total_seconds())
            if inst.start else float("inf"),
        )
    return candidates[0]


def confirm_match(session_id: str, uid: str, recurrence_id=None) -> dict:
    """Pin a recording to a calendar instance the user picked.

    The count stays calendar-sourced (the user vouched for the meeting, not for
    a number they typed), so clearing the match can still clear it; ``confirmed``
    on the match is what protects it from the next refresh. The count is
    remembered against the normalized title so the next instance of a recurring
    meeting still has one when Exchange drops the attendees.
    """
    session = storage.get_session_times(session_id)
    if not session:
        return {"ok": False, "error": "Session not found.", "reason": "no_session"}
    instance = _find_instance(uid, recurrence_id, near=session.get("started_at"))
    if instance is None:
        return {
            "ok": False,
            "error": "That calendar event is no longer in the feed.",
            "reason": "stale_event",
        }

    start, end = _session_span(session)
    result = calendar_feed.match_session([instance], start, end, _match_window())
    scored = result.get("best") or {
        "instance": instance,
        "score": 1.0,
        "overlap_seconds": 0,
        "start_delta_seconds": 0,
        "reason": "Chosen by hand",
    }
    scored = dict(scored)
    scored["reason"] = scored.get("reason") or "Chosen by hand"
    match = build_match(scored, [], confirmed=True)
    match["score"] = 1.0
    storage.set_calendar_match(session_id, match)
    count = match.get("attendee_count")
    if count:
        # Source stays "calendar": a count the user actually typed is what
        # "user" means, and only that should survive clearing the match.
        storage.set_expected_speaker_count(session_id, int(count), "calendar")
        calendar_feed.remember_expected_count(
            calendar_feed.normalize_title(instance.summary), int(count)
        )
    elif session.get("expected_speaker_source") == "calendar":
        # Pinned to a meeting whose attendees the feed does not share: the
        # previous meeting's ceiling must not stay behind.
        storage.set_expected_speaker_count(session_id, None, "calendar")
    merge_candidates(session_id, instance, match)
    return {"ok": True, "match": match}


def remerge_candidates(session_id: str) -> bool:
    """Re-merge the stored match's people into the candidates file.

    Used as the follow-up after a smart cleanup reanalysis, which wipes the
    session's speaker labels. The cached instance is preferred; if the feed has
    moved on, the attendees captured in the stored match are enough.
    """
    match = storage.get_calendar_match(session_id)
    if not match:
        return False
    session = storage.get_session_times(session_id) or {}
    instance = _find_instance(
        match.get("uid") or "", match.get("recurrence_id"),
        near=match.get("start") or session.get("started_at"),
    )
    if instance is None:
        instance = calendar_feed.Instance(
            uid=match.get("uid") or "",
            summary=match.get("title") or "",
            organizer=match.get("organizer") or {},
            attendees=list(match.get("attendees") or []),
            is_private=bool(match.get("is_private")),
        )
    return merge_candidates(session_id, instance, match)


def clear_match(session_id: str) -> dict:
    """Drop the stored match, plus any expected count the calendar supplied.

    A count the user typed (source "user") is theirs and survives: only the
    calendar-derived ceiling goes away with the match it came from.
    """
    session = storage.get_session_times(session_id)
    if not session:
        return {"ok": False, "error": "Session not found.", "reason": "no_session"}
    # A tombstone, not a NULL: writing nothing would let the next refresh
    # re-match the recording to the very meeting the user just rejected.
    storage.set_calendar_match(session_id, {
        "cleared": True,
        "confirmed": True,
        "cleared_at": _now_iso(),
        "title": None,
        "uid": None,
        "attendee_count": None,
    })
    if session.get("expected_speaker_source") != "user":
        storage.set_expected_speaker_count(session_id, None, "calendar")
    return {"ok": True, "match": None, "cleared": True}


# ── Smart cleanup plan ───────────────────────────────────────────────────────

def build_plan(session_id: str) -> dict:
    """Describe what a smart cleanup would do. Reads only; changes nothing.

    ``action`` is "reanalyze" when the speaker count disagrees with the
    calendar, "resolve_only" when the count looks right but names are missing,
    and "none" when there is nothing to do. ``max_speakers`` is a ceiling for
    reanalysis, never a forced exact count.
    """
    attention = storage.get_session_attention(session_id)
    if attention is None:
        return {"error": "Session not found."}
    match = storage.get_calendar_match(session_id) or {}
    payload = _load_candidates(session_id)
    candidates = payload.get("candidates")
    candidates = candidates if isinstance(candidates, list) else []

    expected = attention.get("expected")
    found = attention.get("found") or 0
    unresolved = attention.get("unresolved") or 0

    if expected and found != expected:
        action = "reanalyze"
    elif unresolved:
        action = "resolve_only"
    else:
        action = "none"

    if action == "reanalyze":
        detail = (
            f"The calendar expects {expected} "
            f"{'person' if expected == 1 else 'people'}; this recording has {found}. "
            f"Reanalysis will run again with up to {expected} speakers; if it "
            "still finds fewer, the voices may be too similar to separate."
        )
    elif action == "resolve_only":
        detail = (
            f"{unresolved} speaker{'' if unresolved == 1 else 's'} still "
            "unnamed. The speaker count already looks right, so name them in "
            "the Speakers Cleanup tab instead of reanalyzing."
        )
    else:
        detail = "Nothing to clean up: the speakers are named and the count matches."

    return {
        "session_id": session_id,
        "expected": expected,
        "found": found,
        "unresolved": unresolved,
        "below_threshold": attention.get("below_threshold") or 0,
        "action": action,
        "detail": detail,
        "max_speakers": expected if expected and expected > 0 else None,
        "candidates": candidates,
        "calendar": {
            "title": match.get("title") or "",
            "start": match.get("start"),
            "attendee_count": match.get("attendee_count"),
            "attendee_count_source": match.get("attendee_count_source") or "",
            "confirmed": bool(match.get("confirmed")),
        } if match else None,
        # The Voice Library decides which names land on which voice, and that
        # only happens during reanalysis. Nothing here can know it in advance.
        "library_matches_expected": "unknown",
    }
