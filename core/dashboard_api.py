"""Read-only dashboard data for the Home view."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, jsonify, request

from core import paths, settings, storage
from core.attention import is_generic_speaker_name
from core.calendar_feed import parse_iso_utc

UTC = timezone.utc
DEFAULT_TIMEZONE = "America/Chicago"
PEOPLE_WEEKS = 8
PEOPLE_LIMIT = 30      # rows the Home card can show; it fills its column and scrolls
ACTIVITY_DAYS = 14

bp = Blueprint("dashboard_api", __name__)


def _utcnow() -> datetime:
    """Return the current aware UTC time through the module's patchable clock."""
    return datetime.now(UTC)


def _timezone() -> tuple[str, ZoneInfo]:
    name = (request.args.get("tz") or settings.get("calendar_timezone")
            or DEFAULT_TIMEZONE).strip()
    if not name:
        name = DEFAULT_TIMEZONE
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("tz must be a valid IANA timezone") from exc


def _connect() -> sqlite3.Connection:
    """Open the current application database without write capability."""
    uri = paths.db_path().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


def _duration_seconds(row: sqlite3.Row | dict) -> float:
    """Apply the shared UI duration rule to a session query row."""
    segment_end = row["last_segment_time"]
    if segment_end is not None:
        try:
            return max(0.0, float(segment_end))
        except (TypeError, ValueError):
            return 0.0
    started = parse_iso_utc(row["started_at"])
    ended = parse_iso_utc(row["ended_at"])
    if started is None or ended is None:
        return 0.0
    span = (ended - started).total_seconds()
    return span if 0 < span < 6 * 60 * 60 else 0.0


def _session_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.id, s.started_at, s.ended_at,"
        " (SELECT MAX(ts.end_time) FROM transcript_segments ts"
        "  WHERE ts.session_id = s.id) AS last_segment_time"
        " FROM sessions s"
    ).fetchall()


def _people(
    conn: sqlite3.Connection,
    session_ids: set[str],
    me_global_id: str | None,
) -> list[dict]:
    if not session_ids:
        return []
    placeholders = ",".join("?" for _ in session_ids)
    params = tuple(session_ids)
    label_rows = conn.execute(
        "SELECT sl.session_id, sl.speaker_key, sl.name AS label_name,"
        " sl.global_id, gs.name, COALESCE(gs.color, sl.color) AS color"
        " FROM speaker_labels sl"
        " JOIN global_speakers gs ON gs.id = sl.global_id"
        f" WHERE sl.session_id IN ({placeholders}) AND sl.global_id IS NOT NULL",
        params,
    ).fetchall()
    valid: dict[tuple[str, str], sqlite3.Row] = {}
    for row in label_rows:
        if not is_generic_speaker_name(row["label_name"]):
            valid[(row["session_id"], row["speaker_key"])] = row
    if not valid:
        return []
    talk_rows = conn.execute(
        "SELECT ts.session_id,"
        " COALESCE(NULLIF(ts.source_override, ''), ts.source) AS speaker_key,"
        " COALESCE(SUM(MAX(ts.end_time - ts.start_time, 0)), 0) AS seconds,"
        " COUNT(*) AS segments"
        " FROM transcript_segments ts"
        f" WHERE ts.session_id IN ({placeholders})"
        " GROUP BY ts.session_id,"
        " COALESCE(NULLIF(ts.source_override, ''), ts.source)",
        params,
    ).fetchall()
    talk = {
        (row["session_id"], row["speaker_key"]): (float(row["seconds"] or 0), int(row["segments"] or 0))
        for row in talk_rows
    }
    aggregates: dict[str, dict] = {}
    for key, row in valid.items():
        item = aggregates.setdefault(row["global_id"], {
            "global_id": row["global_id"],
            "name": row["name"],
            "color": row["color"],
            "sessions": set(),
            "talk_seconds": 0.0,
            "segments": 0,
        })
        item["sessions"].add(row["session_id"])
        seconds, segments = talk.get(key, (0.0, 0))
        item["talk_seconds"] += seconds
        item["segments"] += segments
    # One row per person, not per profile. The Voice Library can hold two
    # profiles with the same name (a "Me" profile plus an older desktop-side
    # one, or a pre-merge duplicate), and the library's own maintenance pass
    # already treats same-name profiles as one person, so fold them here too.
    # When one of them is the "Me" profile the merged row is "you".
    merged: dict[str, dict] = {}
    for item in aggregates.values():
        key = (item["name"] or "").strip().casefold()
        is_me = bool(me_global_id and item["global_id"] == me_global_id)
        cur = merged.get(key)
        if cur is None:
            item["is_me"] = is_me
            merged[key] = item
            continue
        cur["sessions"] |= item["sessions"]
        cur["talk_seconds"] += item["talk_seconds"]
        cur["segments"] += item["segments"]
        if is_me and not cur["is_me"]:
            cur["is_me"] = True
            cur["global_id"] = item["global_id"]
            cur["color"] = item["color"]
    result = [{
        "global_id": item["global_id"],
        "name": item["name"],
        "color": item["color"],
        "meeting_count": len(item["sessions"]),
        "talk_seconds": round(item["talk_seconds"]),
        "segment_count": int(item["segments"]),
        "is_me": item["is_me"],
    } for item in merged.values()]
    # Most active voices first: transcript segments (turns taken), then talk
    # time, then how many meetings. The Home card fills its column with them.
    result.sort(key=lambda item: (
        -item["segment_count"], -item["talk_seconds"], -item["meeting_count"],
        item["name"].casefold(),
    ))
    return result[:PEOPLE_LIMIT]


@bp.route("/api/dashboard")
def get_dashboard():
    """Return totals, local activity, recent people, and attention counts."""
    try:
        timezone_name, local_zone = _timezone()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    now = _utcnow().astimezone(UTC)
    today = now.astimezone(local_zone).date()
    activity_start = today - timedelta(days=ACTIVITY_DAYS - 1)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    people_since = today - timedelta(weeks=PEOPLE_WEEKS)

    with _connect() as conn:
        session_rows = _session_rows(conn)
        speaker_count = conn.execute(
            "SELECT COUNT(*) FROM global_speakers"
        ).fetchone()[0]

        durations: dict[str, float] = {}
        activity: dict[date, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "seconds": 0.0}
        )
        first_started: datetime | None = None
        week_ids: set[str] = set()
        people_ids: set[str] = set()
        for row in session_rows:
            started = parse_iso_utc(row["started_at"])
            if started is None:
                continue
            duration = _duration_seconds(row)
            durations[row["id"]] = duration
            first_started = started if first_started is None else min(first_started, started)
            local_day = started.astimezone(local_zone).date()
            if activity_start <= local_day <= today:
                activity[local_day]["count"] += 1
                activity[local_day]["seconds"] += duration
            if week_start <= local_day <= week_end:
                week_ids.add(row["id"])
            if people_since <= local_day <= today:
                people_ids.add(row["id"])

        people = _people(
            conn, people_ids, settings.get("me_speaker_global_id")
        )

    activity_items = []
    for offset in range(ACTIVITY_DAYS):
        day = activity_start + timedelta(days=offset)
        item = activity[day]
        activity_items.append({
            "day": day.isoformat(),
            "count": int(item["count"]),
            "seconds": round(item["seconds"]),
        })
    attention = storage.attention_summary()
    return jsonify({
        "generated_at": now.replace(microsecond=0).isoformat(),
        "timezone": timezone_name,
        "totals": {
            "sessions": len(session_rows),
            "seconds": round(sum(durations.values())),
            "speakers": int(speaker_count),
            "first_session_at": (
                first_started.replace(microsecond=0).isoformat()
                if first_started else None
            ),
        },
        "this_week": {
            "sessions": len(week_ids),
            "seconds": round(sum(durations.get(sid, 0.0) for sid in week_ids)),
            "start": week_start.isoformat(),
            "end": week_end.isoformat(),
        },
        "activity": activity_items,
        "people": {
            "weeks": PEOPLE_WEEKS,
            "since": people_since.isoformat(),
            "items": people,
        },
        "attention": {
            "needs_attention": attention["needs_attention"],
            "unresolved_speakers": attention["unresolved_speakers"],
        },
    })
