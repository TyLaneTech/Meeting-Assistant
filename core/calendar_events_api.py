"""Read-only calendar event presentation data for the Calendar view."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, jsonify, request

from core import calendar_feed, paths, settings

UTC = timezone.utc
DEFAULT_TIMEZONE = "America/Chicago"
MAX_RANGE_DAYS = 62

bp = Blueprint("calendar_events_api", __name__)


def _utcnow() -> datetime:
    """Return the current aware UTC time through the module's patchable clock."""
    return datetime.now(UTC)


def _connect() -> sqlite3.Connection:
    """Open the current application database without write capability."""
    uri = paths.db_path().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


def _parse_day(value: str | None, field: str) -> date:
    if not value:
        raise ValueError(f"{field} is required in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must use YYYY-MM-DD format") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use YYYY-MM-DD format")
    return parsed


def _timezone() -> tuple[str, ZoneInfo]:
    name = (request.args.get("tz") or settings.get("calendar_timezone")
            or DEFAULT_TIMEZONE).strip()
    if not name:
        name = DEFAULT_TIMEZONE
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("tz must be a valid IANA timezone") from exc


def _duration_span(row: sqlite3.Row | dict) -> tuple[datetime | None, datetime | None]:
    """Return the session span using the UI duration rule."""
    start = calendar_feed.parse_iso_utc(row["started_at"])
    if start is None:
        return None, None
    segment_end = row["last_segment_time"]
    if segment_end is not None:
        try:
            seconds = max(0.0, float(segment_end))
        except (TypeError, ValueError):
            seconds = 0.0
        return (start, start + timedelta(seconds=seconds)) if seconds > 0 else (start, None)
    end = calendar_feed.parse_iso_utc(row["ended_at"])
    if end is None:
        return start, None
    seconds = (end - start).total_seconds()
    return (start, end) if 0 < seconds < 6 * 60 * 60 else (start, None)


def _sessions() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.started_at, s.ended_at, s.calendar_match,"
            " (SELECT MAX(ts.end_time) FROM transcript_segments ts"
            "  WHERE ts.session_id = s.id) AS last_segment_time"
            " FROM sessions s"
        ).fetchall()
    result = []
    for row in rows:
        try:
            match = json.loads(row["calendar_match"]) if row["calendar_match"] else None
        except (TypeError, ValueError):
            match = None
        start, end = _duration_span(row)
        result.append({
            "id": row["id"],
            "title": row["title"],
            "start": start,
            "end": end,
            "match": match if isinstance(match, dict) else None,
        })
    return result


def _same_instant(left, right) -> bool:
    left_dt = calendar_feed.parse_iso_utc(left)
    right_dt = calendar_feed.parse_iso_utc(right)
    return left_dt is not None and right_dt is not None and left_dt == right_dt


def _stored_match(session: dict, instance: calendar_feed.Instance) -> bool:
    match = session.get("match") or {}
    if match.get("cleared") or match.get("uid") != instance.uid:
        return False
    stored_recurrence = match.get("recurrence_id")
    if stored_recurrence is None and instance.recurrence_id is None:
        stored_start = match.get("start")
        return not stored_start or _same_instant(stored_start, instance.start)
    return _same_instant(stored_recurrence, instance.recurrence_id)


def _overlap_ratio(
    session: dict,
    event_start: datetime,
    event_end: datetime,
) -> float:
    session_start = session.get("start")
    session_end = session.get("end")
    if session_start is None or session_end is None or event_end <= event_start:
        return 0.0
    overlap = (min(session_end, event_end) - max(session_start, event_start)).total_seconds()
    if overlap <= 0:
        return 0.0
    shorter = min(
        (session_end - session_start).total_seconds(),
        (event_end - event_start).total_seconds(),
    )
    return overlap / shorter if shorter > 0 else 0.0


def _find_session(
    instance: calendar_feed.Instance,
    sessions: list[dict],
) -> dict | None:
    exact = [session for session in sessions if _stored_match(session, instance)]
    if exact:
        return min(exact, key=lambda session: abs(
            (session["start"] - instance.start).total_seconds()
        ) if session["start"] else float("inf"))
    if instance.all_day:
        # An all-day block ("Remote", "Out of office") overlaps every recording
        # made that day; only a stored match can tie one to a recording.
        return None
    candidates = [
        (_overlap_ratio(session, instance.start, instance.end), session)
        for session in sessions
    ]
    candidates = [item for item in candidates if item[0] >= 0.5]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _opaque_key(instance: calendar_feed.Instance) -> str:
    occurrence = instance.recurrence_id or instance.start
    recurrence = calendar_feed.as_utc(occurrence).isoformat() if occurrence else ""
    raw = f"{instance.uid}\0{recurrence}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _event_status(instance: calendar_feed.Instance) -> str:
    value = str(instance.status or "CONFIRMED").strip().lower()
    return value if value in {"confirmed", "tentative", "cancelled"} else "confirmed"


@bp.route("/api/calendar/events")
def get_calendar_events():
    """Return cached event occurrences as a strictly redacted view model."""
    try:
        start_day = _parse_day(request.args.get("start"), "start")
        end_day = _parse_day(request.args.get("end"), "end")
        if end_day < start_day:
            raise ValueError("end must be on or after start")
        if (end_day - start_day).days + 1 > MAX_RANGE_DAYS:
            raise ValueError("date range cannot exceed 62 days")
        timezone_name, local_zone = _timezone()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    prefs = settings.load()
    enabled = bool(
        prefs.get("calendar_enabled")
        and str(prefs.get("calendar_ics_url") or "").strip()
    )
    base = {
        "enabled": enabled,
        "timezone": timezone_name,
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "last_refresh": prefs.get("calendar_last_refresh") or None,
        "last_error": str(prefs.get("calendar_last_error") or ""),
        "events": [],
    }
    if not enabled:
        return jsonify(base)

    range_start = datetime.combine(start_day, time.min, local_zone).astimezone(UTC)
    range_end = datetime.combine(
        end_day + timedelta(days=1), time.min, local_zone
    ).astimezone(UTC)
    include_cancelled = request.args.get("include_cancelled") == "1"
    live_session_id = request.args.get("live_session_id") or None
    now = _utcnow().astimezone(UTC)
    sessions = _sessions()
    live_session = next(
        (session for session in sessions if session["id"] == live_session_id), None
    )

    events = []
    for instance in calendar_feed.cached_instances():
        event_start = calendar_feed.as_utc(instance.start)
        event_end = calendar_feed.as_utc(instance.end)
        if event_start is None or event_end is None or event_end <= event_start:
            continue
        if event_start >= range_end or event_end <= range_start:
            continue
        if calendar_feed.is_ghost_title(instance.summary):
            continue
        status = _event_status(instance)
        if status == "cancelled" and not include_cancelled:
            continue
        matched = _find_session(instance, sessions)
        live_overlap = (
            live_session is not None
            and _overlap_ratio(live_session, event_start, event_end) >= 0.5
        )
        if live_session is not None and (
            live_overlap or (matched and matched["id"] == live_session_id)
        ):
            matched = live_session
            state = "recording"
        elif matched is not None:
            state = "recorded"
        elif instance.all_day:
            # All-day items are context, not meetings: never "missed".
            state = "past" if event_end < now else "upcoming"
        elif event_end < now - timedelta(minutes=15):
            state = "missed"
        else:
            state = "upcoming"
        events.append({
            "key": _opaque_key(instance),
            "title": "Private appointment" if instance.is_private else instance.summary,
            "start": event_start.replace(microsecond=0).isoformat(),
            "end": event_end.replace(microsecond=0).isoformat(),
            "all_day": bool(instance.all_day),
            "private": bool(instance.is_private),
            "status": status,
            "day": event_start.astimezone(local_zone).date().isoformat(),
            "session_id": matched["id"] if matched else None,
            "session_title": matched["title"] if matched else None,
            "state": state,
        })
    events.sort(key=lambda item: (item["start"], item["key"]))
    base["events"] = events
    return jsonify(base)
