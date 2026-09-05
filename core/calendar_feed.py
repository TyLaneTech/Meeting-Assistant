"""Published-calendar (ICS) reader for calendar-driven smart cleanup.

The app cannot reach Microsoft Graph, so the owner publishes his Outlook
calendar ("Can view all details") and pastes the resulting ICS link into
Settings > Calendar. This module fetches that feed, parses it, expands
recurring series, and matches recordings to calendar events by time.

Everything here is stdlib plus python-dateutil and zoneinfo. Nothing in this
module writes to the sessions database or starts any work; it only reads the
feed and answers questions about it. ``core.calendar_sync`` owns the side
effects.

Design notes worth remembering:

* Session timestamps are naive UTC (``storage._now`` uses ``utcnow``), so
  every calendar time is converted to an aware UTC datetime here and callers
  compare in UTC. No fixed offsets, ever.
* Exchange feeds are inconsistent: DTSTART may be UTC (Z), carry a TZID
  holding a Windows display name ("Central Standard Time") with no matching
  VTIMEZONE block, carry a TZID that only a VTIMEZONE block explains, or be
  floating. All four are handled.
* Microsoft has an open regression where ATTENDEE/ORGANIZER lines vanish from
  published feeds. Attendee absence is normal, not an error: it degrades to
  "expected count unknown".
* The feed URL is a credential. It is never logged and never returned by an
  API; ``mask_url`` is what callers show.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from dateutil.tz import gettz, tzical, tzrange, tzstr

from core import paths as paths

UTC = timezone.utc

DEFAULT_TIMEZONE = "America/Chicago"

# A current Chrome UA. Exchange answers some non-browser agents with a bare
# HTTP 500 on an otherwise valid published-calendar URL.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Windows display names (as Exchange writes them into TZID) mapped to IANA
# zones. The US zones matter most here, but a mis-mapped foreign zone is an
# hours-wrong match, so the common international ones are covered too. Anything
# still unknown falls through to dateutil, a VTIMEZONE block, then the default.
WINDOWS_TIMEZONE_MAP: dict[str, str] = {
    # North America
    "eastern standard time": "America/New_York",
    "eastern daylight time": "America/New_York",
    "us eastern standard time": "America/New_York",
    "central standard time": "America/Chicago",
    "central daylight time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "mountain daylight time": "America/Denver",
    "us mountain standard time": "America/Phoenix",
    "pacific standard time": "America/Los_Angeles",
    "pacific daylight time": "America/Los_Angeles",
    "alaskan standard time": "America/Anchorage",
    "alaskan daylight time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu",
    "atlantic standard time": "America/Halifax",
    "newfoundland standard time": "America/St_Johns",
    "canada central standard time": "America/Regina",
    # Europe
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "central european standard time": "Europe/Warsaw",
    "romance standard time": "Europe/Paris",
    "e. europe standard time": "Europe/Chisinau",
    "gtb standard time": "Europe/Bucharest",
    "fle standard time": "Europe/Kiev",
    "russian standard time": "Europe/Moscow",
    # Asia and Pacific
    "india standard time": "Asia/Kolkata",
    "china standard time": "Asia/Shanghai",
    "tokyo standard time": "Asia/Tokyo",
    "korea standard time": "Asia/Seoul",
    "singapore standard time": "Asia/Singapore",
    "se asia standard time": "Asia/Bangkok",
    "w. australia standard time": "Australia/Perth",
    "aus eastern standard time": "Australia/Sydney",
    "aus central standard time": "Australia/Darwin",
    "new zealand standard time": "Pacific/Auckland",
    # South America and Africa
    "sa pacific standard time": "America/Bogota",
    "sa eastern standard time": "America/Cayenne",
    "e. south america standard time": "America/Sao_Paulo",
    "south africa standard time": "Africa/Johannesburg",
    "egypt standard time": "Africa/Cairo",
    # Universal
    "utc": "UTC",
    "gmt": "UTC",
    "coordinated universal time": "UTC",
    # Outlook's long display names, as they appear after the parenthesised
    # offset in "(UTC-06:00) Central Time (US & Canada)".
    "central time (us & canada)": "America/Chicago",
    "eastern time (us & canada)": "America/New_York",
    "mountain time (us & canada)": "America/Denver",
    "pacific time (us & canada)": "America/Los_Angeles",
    "atlantic time (canada)": "America/Halifax",
    "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "arizona": "America/Phoenix",
    "saskatchewan": "America/Regina",
    "newfoundland": "America/St_Johns",
    "dublin, edinburgh, lisbon, london": "Europe/London",
    "amsterdam, berlin, bern, rome, stockholm, vienna": "Europe/Berlin",
    "paris": "Europe/Paris",
    "chennai, kolkata, mumbai, new delhi": "Asia/Kolkata",
    "beijing, chongqing, hong kong, urumqi": "Asia/Shanghai",
    "osaka, sapporo, tokyo": "Asia/Tokyo",
    "canberra, melbourne, sydney": "Australia/Sydney",
    "auckland, wellington": "Pacific/Auckland",
}


# An offset-only TZID, e.g. "UTC-05" or "(UTC+05:30)". Deliberately a full
# match: "(UTC-06:00) Central Time (US & Canada)" carries a real zone NAME and
# must not be flattened to a fixed offset, which would silently drop DST.
_OFFSET_ONLY_RE = re.compile(r"\(?\s*utc\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*\)?")
# The parenthesised offset prefix Outlook puts in front of a zone name.
_OFFSET_PREFIX_RE = re.compile(
    r"^\(\s*utc\s*[+-]?\s*\d{0,2}(?::?\d{2})?\s*\)\s*", re.IGNORECASE
)


def _utc_offset_zone(name: str) -> str | None:
    """Map an offset-ONLY TZID ("UTC-05", "(UTC+05:30)") to an Etc/GMT zone.

    Etc/GMT signs are inverted (Etc/GMT+5 is UTC-5), which is exactly the trap
    this helper exists to hide. Half-hour offsets have no Etc zone, so they
    return None and fall through to the normal resolution chain.
    """
    match = _OFFSET_ONLY_RE.fullmatch((name or "").strip().lower())
    if not match:
        return None
    sign, hours, minutes = match.group(1), int(match.group(2)), match.group(3)
    if minutes and int(minutes):
        return None
    if hours > 14:
        return None
    return f"Etc/GMT{'-' if sign == '+' else '+'}{hours}"


def _gettz_zone(name: str):
    """dateutil's gettz, minus its habit of inventing a zone from any string.

    gettz falls back to reading its argument as a POSIX TZ spec, so
    "(UTC-05) Bogota" comes back as a tzstr sitting at UTC+6. Only a real
    database zone is worth trusting here.
    """
    try:
        resolved = gettz(name)
    except (ValueError, TypeError):
        return None
    if resolved is None or isinstance(resolved, (tzstr, tzrange)):
        return None
    return resolved


def _strip_offset_prefix(name: str) -> str:
    """Return "(UTC-06:00) Central Time (US & Canada)" as "Central Time (US & Canada)"."""
    stripped = _OFFSET_PREFIX_RE.sub("", (name or "").strip(), count=1)
    return stripped.strip()

# Subjects Exchange substitutes for redacted items on a published feed.
PLACEHOLDER_SUBJECTS = {"private appointment", "busy", "private", "no title"}

# A recording with no end time, and an event with no end time, both need a
# fallback span, otherwise every overlap computation collapses to zero.
DEFAULT_EVENT_MINUTES = 30

MAX_INSTANCES_PER_SERIES = 500

# An instance longer than this is a block ("Focus time", "Out of office"), not a
# meeting: it contains recordings rather than describing them.
BLOCK_MINIMUM_SECONDS = 4 * 3600
# Deliberately below calendar_sync.MIN_MATCH_SCORE, so a block is never stored
# as a match on its own.
BLOCK_SCORE_CAP = 0.45

# Exchange leaves cancelled and rescheduled meetings in a published feed with
# a title prefix rather than STATUS:CANCELLED; such items never match.
_GHOST_TITLE_RE = re.compile(r"^\s*(canceled|cancelled|reschedule[d]?)\s*:", re.IGNORECASE)


def is_ghost_title(summary) -> bool:
    return bool(_GHOST_TITLE_RE.match(str(summary or "")))

# Guards the read-modify-write of calendar_cache.json. Reentrant so a helper
# that reads under the lock can still call save_cache.
_cache_lock = threading.RLock()


class CalendarFeedError(Exception):
    """Raised when the feed cannot be fetched or parsed. Never carries the URL."""


# ── Credential masking ───────────────────────────────────────────────────────

def mask_url(url: str) -> str:
    """Return a safe rendering of the feed URL: scheme, host, last 6 characters.

    The published-calendar URL is the credential (anyone holding it can read
    the calendar), so this is the only form that may reach a log, an API
    response, or the UI.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]+)(.*)$", raw)
    if not match:
        return "..." + raw[-6:]
    prefix, rest = match.group(1), match.group(2)
    if not rest:
        return prefix
    return f"{prefix}/...{rest[-6:]}"


# ── Fetch ────────────────────────────────────────────────────────────────────

def _read_response(response) -> str:
    raw = response.read()
    encoding = ""
    try:
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    except Exception:
        encoding = ""
    if "gzip" in encoding:
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    charset = "utf-8"
    try:
        charset = response.headers.get_content_charset() or "utf-8"
    except Exception:
        charset = "utf-8"
    if isinstance(raw, bytes):
        return raw.decode(charset, errors="replace")
    return str(raw)


def fetch_ics(url: str, timeout: int = 20, opener=None) -> str:
    """Download the ICS text. One retry on a 5xx or a transport failure.

    ``opener`` exists for tests: any callable with urlopen's signature.
    """
    target = (url or "").strip()
    if not target:
        raise CalendarFeedError("No calendar link saved.")
    if not target.lower().startswith("https://"):
        raise CalendarFeedError(
            "The calendar link must start with https:// . Copy the ICS link "
            "from Outlook's Publish a calendar page."
        )

    urlopen = opener or urllib.request.urlopen
    request = urllib.request.Request(target, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/calendar, text/plain;q=0.9, */*;q=0.5",
        # gzip only: advertising deflate would invite a body this reader
        # cannot decompress.
        "Accept-Encoding": "gzip",
    })

    last_error = ""
    for attempt in (1, 2):
        try:
            with urlopen(request, timeout=timeout) as response:
                text = _read_response(response)
            if "BEGIN:VCALENDAR" not in text.upper():
                raise CalendarFeedError(
                    "That link did not return a calendar feed. Copy the ICS "
                    "link from Outlook's Publish a calendar page."
                )
            return text
        except CalendarFeedError:
            raise
        except urllib.error.HTTPError as exc:
            code = getattr(exc, "code", 0)
            if code in (401, 403):
                last_error = (
                    f"The calendar server refused the request (HTTP {code}). "
                    "Re-publish the calendar and copy a fresh ICS link."
                )
            elif code == 404:
                last_error = "The calendar link was not found (HTTP 404)."
            else:
                last_error = f"The calendar server returned HTTP {code}."
            if code < 500 or attempt == 2:
                break
        except urllib.error.URLError as exc:
            last_error = f"Could not reach the calendar server ({exc.reason})."
            if attempt == 2:
                break
        except Exception as exc:  # noqa: BLE001 - the URL must never leak
            last_error = f"Calendar download failed ({type(exc).__name__})."
            break
    raise CalendarFeedError(last_error or "Calendar download failed.")


# ── Line-level parsing ───────────────────────────────────────────────────────

def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding. Continuation lines start with space or tab."""
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in normalized.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return [line for line in out if line.strip()]


def _split_unquoted(value: str, sep: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in value:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == sep and not quoted:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _parse_line(line: str) -> tuple[str, dict[str, str], str]:
    """Split one content line into (NAME, params, value).

    Outlook writes unquoted parameter values that contain a colon, notably
    ``TZID=(UTC-06:00) Central Time (US & Canada)``. Splitting at that colon
    would mangle the property and drop the whole event, so an open parenthesis
    suspends the split until its closing one.
    """
    quoted = False
    depth = 0
    idx = -1
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            idx = i
            break
    if idx < 0:
        return line.strip().upper(), {}, ""
    left, value = line[:idx], line[idx + 1:]
    pieces = _split_unquoted(left, ";")
    name = pieces[0].strip().upper()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" in piece:
            key, raw = piece.split("=", 1)
            params[key.strip().upper()] = raw.strip().strip('"')
        elif piece.strip():
            params[piece.strip().upper()] = ""
    return name, params, value


def _unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    escapes = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            out.append(escapes.get(value[i + 1], value[i + 1]))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ── Timezone resolution ──────────────────────────────────────────────────────

def _zoneinfo(name: str):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return None


def _vtimezone_blocks(lines: list[str]) -> str | None:
    """Return a minimal VCALENDAR holding only this feed's VTIMEZONE blocks."""
    collected: list[str] = []
    depth = 0
    for line in lines:
        upper = line.strip().upper()
        if upper == "BEGIN:VTIMEZONE":
            depth += 1
            collected.append(line.strip())
            continue
        if depth:
            collected.append(line.strip())
            if upper == "END:VTIMEZONE":
                depth -= 1
    if not collected:
        return None
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//meeting-assistant//calendar//EN\n"
        + "\n".join(collected)
        + "\nEND:VCALENDAR\n"
    )


class _TimezoneResolver:
    """Resolves a TZID to a tzinfo, remembering how it got there.

    Order: an IANA name zoneinfo accepts, a Windows display name from
    ``WINDOWS_TIMEZONE_MAP`` (including "UTC-05" style names), whatever
    ``dateutil.tz.gettz`` recognises, a VTIMEZONE block carried by the feed,
    then the configured default zone. Only that last step is a guess, and it is
    the one whose note travels with the event so the UI can show it.
    """

    def __init__(self, lines: list[str], default_tz_name: str = DEFAULT_TIMEZONE):
        self.default_tz_name = default_tz_name or DEFAULT_TIMEZONE
        self.default_tz = _zoneinfo(self.default_tz_name) or UTC
        self._cache: dict[str, object] = {}
        self._vtz = None
        self._vtz_source = _vtimezone_blocks(lines)
        self.notes: list[str] = []

    def note(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)

    def _vtimezone(self, tzid: str):
        if not self._vtz_source:
            return None
        if self._vtz is None:
            try:
                self._vtz = tzical(io.StringIO(self._vtz_source))
            except Exception:
                self._vtz = False
        if self._vtz is False:
            return None
        try:
            return self._vtz.get(tzid)
        except Exception:
            return None

    def resolve(self, tzid: str | None):
        return self.resolve_with_note(tzid)[0]

    def resolve_with_note(self, tzid: str | None):
        """Return (tzinfo, guess_note). The note is set only for a fallback."""
        if not tzid:
            return self.default_tz, ""
        key = tzid.strip()
        if key in self._cache:
            return self._cache[key]

        guess = ""
        # 1. An IANA name zoneinfo already knows.
        tz = _zoneinfo(key)
        # 2. A Windows or Outlook display name.
        if tz is None:
            mapped = WINDOWS_TIMEZONE_MAP.get(key.lower())
            if mapped:
                tz = _zoneinfo(mapped)
                if tz is not None:
                    self.note(f"TZID {key} mapped to {mapped}")
        # 3. The feed's own VTIMEZONE. It carries real DST rules, so it must be
        #    tried before any offset reading of the name.
        if tz is None:
            tz = self._vtimezone(key)
            if tz is not None:
                self.note(f"TZID {key} read from the feed's VTIMEZONE block")
        # 4. Anything dateutil recognises.
        if tz is None:
            tz = _gettz_zone(key)
            if tz is not None:
                self.note(f"TZID {key} resolved by dateutil")
        # 5. "(UTC-06:00) Central Time (US & Canada)": the offset prefix is
        #    decoration, the name after it is the real zone.
        if tz is None:
            bare = _strip_offset_prefix(key)
            if bare and bare.lower() != key.lower():
                mapped = WINDOWS_TIMEZONE_MAP.get(bare.lower())
                tz = _zoneinfo(mapped) if mapped else (_zoneinfo(bare) or _gettz_zone(bare))
                if tz is not None:
                    self.note(f"TZID {key} mapped to {mapped or bare}")
        # 6. An offset-only name, or a prefixed name whose zone nobody knows:
        #    the offset is the last usable signal, at the cost of DST.
        if tz is None:
            prefix = _OFFSET_PREFIX_RE.match(key)
            mapped = _utc_offset_zone(key)
            if not mapped and prefix:
                mapped = _utc_offset_zone(prefix.group(0))
            if mapped:
                tz = _zoneinfo(mapped)
                if tz is not None:
                    self.note(f"TZID {key} mapped to {mapped} (fixed offset, no DST)")
        if tz is None:
            tz = self.default_tz
            guess = f"unknown timezone {key}, read as {self.default_tz_name}"
            self.note(f"TZID {key} is unknown; treated as {self.default_tz_name}")
        self._cache[key] = (tz, guess)
        return tz, guess


# ── Value parsing ────────────────────────────────────────────────────────────

def _parse_datetime(value: str, params: dict[str, str], resolver: _TimezoneResolver):
    """Parse one DATE / DATE-TIME value.

    Returns ``(utc, local, is_date, tz_note)``. ``local`` keeps the original
    zone attached, which is what recurrence expansion must repeat: a weekly
    09:00 Central meeting stays at 09:00 Central across a DST change, whereas
    repeating the UTC instant would drift it by an hour.
    """
    raw = (value or "").strip()
    if not raw:
        return None, None, False, ""
    is_date = params.get("VALUE", "").upper() == "DATE" or (
        len(raw) == 8 and raw.isdigit()
    )
    if is_date:
        try:
            parsed = datetime.strptime(raw[:8], "%Y%m%d")
        except ValueError:
            return None, None, True, ""
        local = parsed.replace(tzinfo=resolver.default_tz)
        return local.astimezone(UTC), local, True, ""
    core = raw[:-1] if raw.endswith("Z") else raw
    try:
        parsed = datetime.strptime(core[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None, None, False, ""
    if raw.endswith("Z"):
        utc = parsed.replace(tzinfo=UTC)
        return utc, utc, False, ""
    tzid = params.get("TZID")
    if not tzid:
        resolver.note(
            f"Floating time with no zone; treated as {resolver.default_tz_name}"
        )
    tz, guess = resolver.resolve_with_note(tzid)
    local = parsed.replace(tzinfo=tz)
    return local.astimezone(UTC), local, False, guess


_DURATION_RE = re.compile(
    r"^(?P<sign>[+-])?P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_duration(value: str) -> timedelta | None:
    """Parse an RFC 5545 DURATION (P1DT2H30M and friends)."""
    match = _DURATION_RE.match((value or "").strip().upper())
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict().items() if k != "sign" and v}
    delta = timedelta(
        weeks=parts.get("weeks", 0),
        days=parts.get("days", 0),
        hours=parts.get("hours", 0),
        minutes=parts.get("minutes", 0),
        seconds=parts.get("seconds", 0),
    )
    return -delta if match.group("sign") == "-" else delta


def _parse_person(value: str, params: dict[str, str]) -> dict:
    raw = (value or "").strip()
    email = ""
    if raw.lower().startswith("mailto:"):
        email = raw[7:].strip().lower()
    elif "@" in raw:
        email = raw.lower()
    return {"name": _unescape_text(params.get("CN", "")).strip(), "email": email}


def _parse_attendee(value: str, params: dict[str, str]) -> dict:
    person = _parse_person(value, params)
    cutype = (params.get("CUTYPE", "") or "INDIVIDUAL").upper()
    return {
        "name": person["name"],
        "email": person["email"],
        "partstat": (params.get("PARTSTAT", "") or "").upper(),
        "role": (params.get("ROLE", "") or "").upper(),
        "cutype": cutype,
        "is_resource": cutype in ("RESOURCE", "ROOM"),
    }


# ── Events and instances ─────────────────────────────────────────────────────

@dataclass
class Event:
    uid: str = ""
    recurrence_id: datetime | None = None
    summary: str = ""
    description: str = ""
    location: str = ""
    organizer: dict | None = None
    attendees: list = field(default_factory=list)
    status: str = ""
    is_private: bool = False
    rrule: str = ""
    exdates: list = field(default_factory=list)
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    # The DTSTART with its original zone still attached. Recurrence expansion
    # runs on this, never on ``start``: repeating a UTC instant drifts a local
    # meeting by an hour the moment daylight saving changes.
    start_local: datetime | None = None
    # Set only when the zone had to be guessed, so the UI can say so.
    tz_note: str = ""

    @property
    def duration(self) -> timedelta:
        if self.start and self.end and self.end > self.start:
            return self.end - self.start
        return timedelta(days=1) if self.all_day else timedelta(minutes=DEFAULT_EVENT_MINUTES)


@dataclass
class Instance:
    uid: str = ""
    recurrence_id: datetime | None = None
    summary: str = ""
    description: str = ""
    location: str = ""
    organizer: dict | None = None
    attendees: list = field(default_factory=list)
    status: str = ""
    is_private: bool = False
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    tz_note: str = ""

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "recurrence_id": self.recurrence_id.isoformat() if self.recurrence_id else None,
            "summary": self.summary,
            "description": self.description,
            "location": self.location,
            "organizer": self.organizer,
            "attendees": self.attendees,
            "status": self.status,
            "is_private": self.is_private,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "all_day": self.all_day,
            "tz_note": self.tz_note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Instance":
        return cls(
            uid=data.get("uid") or "",
            recurrence_id=parse_iso_utc(data.get("recurrence_id")),
            summary=data.get("summary") or "",
            description=data.get("description") or "",
            location=data.get("location") or "",
            organizer=data.get("organizer"),
            attendees=list(data.get("attendees") or []),
            status=data.get("status") or "",
            is_private=bool(data.get("is_private")),
            start=parse_iso_utc(data.get("start")),
            end=parse_iso_utc(data.get("end")),
            all_day=bool(data.get("all_day")),
            tz_note=data.get("tz_note") or "",
        )


def parse_iso_utc(value) -> datetime | None:
    """Parse an ISO timestamp into aware UTC. Naive input is treated as UTC.

    Session timestamps are naive UTC strings, so this is also how a recording's
    started_at / ended_at become comparable with calendar times.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return as_utc(value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return as_utc(parsed)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _instance_from_event(event: Event, start: datetime, end: datetime) -> Instance:
    return Instance(
        uid=event.uid,
        recurrence_id=event.recurrence_id,
        summary=event.summary,
        description=event.description,
        location=event.location,
        organizer=event.organizer,
        attendees=list(event.attendees),
        status=event.status,
        is_private=event.is_private,
        start=start,
        end=end,
        all_day=event.all_day,
        tz_note=event.tz_note,
    )


def parse_ics(text: str, default_tz: str = DEFAULT_TIMEZONE,
              notes: list | None = None) -> list[Event]:
    """Parse an ICS document into events with aware-UTC start/end times."""
    lines = _unfold(text)
    if not lines:
        return []
    resolver = _TimezoneResolver(lines, default_tz)

    events: list[Event] = []
    stack: list[str] = []
    current: Event | None = None
    duration: timedelta | None = None
    has_dtend = False

    for line in lines:
        name, params, value = _parse_line(line)
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                current = Event()
                duration = None
                has_dtend = False
            continue
        if name == "END":
            ended = stack.pop() if stack else ""
            if ended == "VEVENT" and current is not None:
                if current.start:
                    if not has_dtend:
                        span = duration if duration else (
                            timedelta(days=1) if current.all_day
                            else timedelta(minutes=DEFAULT_EVENT_MINUTES)
                        )
                        current.end = current.start + span
                    if not current.end or current.end < current.start:
                        current.end = current.start + timedelta(minutes=DEFAULT_EVENT_MINUTES)
                    events.append(current)
                current = None
            continue

        if current is None or (stack and stack[-1] != "VEVENT"):
            continue

        if name == "UID":
            current.uid = value.strip()
        elif name == "SUMMARY":
            current.summary = _unescape_text(value).strip()
        elif name == "DESCRIPTION":
            current.description = _unescape_text(value).strip()
        elif name == "LOCATION":
            current.location = _unescape_text(value).strip()
        elif name == "STATUS":
            current.status = value.strip().upper()
        elif name == "CLASS":
            if value.strip().upper() in ("PRIVATE", "CONFIDENTIAL"):
                current.is_private = True
        elif name == "ORGANIZER":
            current.organizer = _parse_person(value, params)
        elif name == "ATTENDEE":
            current.attendees.append(_parse_attendee(value, params))
        elif name == "RRULE":
            current.rrule = value.strip()
        elif name == "EXDATE":
            for piece in value.split(","):
                parsed, _local, _is_date, _note = _parse_datetime(piece, params, resolver)
                if parsed:
                    current.exdates.append(parsed)
        elif name == "RECURRENCE-ID":
            parsed, _local, _is_date, _note = _parse_datetime(value, params, resolver)
            current.recurrence_id = parsed
        elif name == "DTSTART":
            parsed, local, is_date, note = _parse_datetime(value, params, resolver)
            current.start = parsed
            current.start_local = local
            current.all_day = is_date
            if note:
                current.tz_note = note
        elif name == "DTEND":
            parsed, _local, is_date, _note = _parse_datetime(value, params, resolver)
            if parsed:
                current.end = parsed
                has_dtend = True
                if is_date:
                    current.all_day = True
        elif name == "DURATION":
            duration = parse_duration(value)

    for event in events:
        if not event.is_private and event.summary.strip().lower() in PLACEHOLDER_SUBJECTS:
            event.is_private = True

    if notes is not None:
        notes.extend(resolver.notes)
    return events


def _fix_until(rule_text: str, dtstart: datetime) -> str:
    """Normalize UNTIL so dateutil accepts it against an aware DTSTART.

    A naive UNTIL belongs to the event's own zone (RFC 5545 says it must match
    DTSTART's form), so it is read in ``dtstart``'s zone and rewritten as UTC.
    Reading it as UTC instead would cut a Central series short by six hours.
    """
    if "UNTIL=" not in rule_text.upper():
        return rule_text
    tz = dtstart.tzinfo if dtstart and dtstart.tzinfo else UTC
    out_parts = []
    for part in rule_text.split(";"):
        key, _, val = part.partition("=")
        if key.strip().upper() != "UNTIL" or not val or val.endswith("Z"):
            out_parts.append(part)
            continue
        raw = val.strip()
        text = f"{raw}T235959" if len(raw) == 8 else raw[:15]
        try:
            local = datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
        except ValueError:
            out_parts.append(part)
            continue
        out_parts.append(f"{key}={local.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    return ";".join(out_parts)


def expand(events, window_start: datetime, window_end: datetime,
           max_per_series: int = MAX_INSTANCES_PER_SERIES) -> list[Instance]:
    """Expand events into concrete instances overlapping the window.

    Applies RRULE, drops EXDATE instances, lets a RECURRENCE-ID override
    replace the generated instance it points at, and skips cancellations.

    Recurrence is expanded on the event's LOCAL start (``start_local``) and each
    occurrence is converted to UTC afterwards. Expanding on the UTC instant
    would repeat the UTC wall clock, so every occurrence after a daylight
    saving change would land an hour off and EXDATEs would stop cancelling.

    Known limitation: a RECURRENCE-ID carrying RANGE=THISANDFUTURE is treated as
    a single-instance override (the rest of the series keeps the master's
    times) rather than as a rewrite of the remaining series.
    """
    window_start = as_utc(window_start)
    window_end = as_utc(window_end)

    masters: list[Event] = []
    overrides: dict[tuple, Event] = {}
    for event in events or []:
        if not event.start:
            continue
        if event.recurrence_id:
            overrides[(event.uid, as_utc(event.recurrence_id))] = event
        else:
            masters.append(event)

    used: set = set()
    out: list[Instance] = []

    def _overlaps(start: datetime, end: datetime) -> bool:
        return start < window_end and end > window_start

    def _emit(event: Event, start: datetime, end: datetime) -> None:
        if _overlaps(start, end):
            out.append(_instance_from_event(event, start, end))

    for master in masters:
        if master.status == "CANCELLED":
            continue
        duration = master.duration
        if not master.rrule:
            _emit(master, master.start, master.end or (master.start + duration))
            continue
        dtstart = master.start_local or master.start
        try:
            rule = rrulestr(_fix_until(master.rrule, dtstart), dtstart=dtstart)
            occurrences = list(rule.between(window_start - duration, window_end, inc=True))
        except Exception:
            _emit(master, master.start, master.end or (master.start + duration))
            continue
        exdates = {as_utc(x) for x in master.exdates}
        # Keep the NEWEST occurrences when a runaway series hits the cap: a
        # recording is far more likely to belong to a recent instance.
        for occurrence in occurrences[-max_per_series:]:
            occurrence = as_utc(occurrence)
            if occurrence in exdates:
                continue
            key = (master.uid, occurrence)
            override = overrides.get(key)
            if override:
                used.add(key)
                if override.status != "CANCELLED":
                    _emit(override, override.start,
                          override.end or (override.start + override.duration))
                continue
            _emit(master, occurrence, occurrence + duration)

    # Overrides whose generated occurrence never appeared (a detached exception,
    # or a series expanded outside this window) still belong in the result.
    for key, override in overrides.items():
        if key in used or override.status == "CANCELLED":
            continue
        _emit(override, override.start, override.end or (override.start + override.duration))

    out.sort(key=lambda inst: inst.start)
    return out


# ── Attendee counting ────────────────────────────────────────────────────────

def expected_count(instance) -> int | None:
    """People expected in the room: organizer plus attendees.

    Rooms and other resources do not speak, and a declined invitee is not
    there. Returns None when the item carries no attendees at all, which is
    the Microsoft regression case, not a zero-person meeting.
    """
    attendees = list(getattr(instance, "attendees", None) or [])
    if not attendees:
        return None
    seen: set = set()
    for person in attendees:
        cutype = (person.get("cutype") or "").upper()
        if person.get("is_resource") or cutype in ("RESOURCE", "ROOM"):
            continue
        if (person.get("partstat") or "").upper() == "DECLINED":
            continue
        key = (person.get("email") or "").strip().lower()
        if not key:
            name = (person.get("name") or "").strip().lower()
            if not name:
                continue
            key = "name:" + name
        seen.add(key)
    organizer = getattr(instance, "organizer", None) or {}
    org_key = (organizer.get("email") or "").strip().lower()
    if not org_key and (organizer.get("name") or "").strip():
        org_key = "name:" + organizer["name"].strip().lower()
    if org_key:
        seen.add(org_key)
    return len(seen) or None


def candidate_people(instance) -> list[dict]:
    """Attendee list rendered as speaker candidates (organizer first)."""
    out: list[dict] = []
    seen: set = set()

    def _add(person: dict, role: str) -> None:
        name = (person.get("name") or "").strip()
        email = (person.get("email") or "").strip().lower()
        if not name and email:
            name = email.split("@")[0].replace(".", " ").title()
        if not name:
            return
        key = email or name.lower()
        if key in seen:
            return
        seen.add(key)
        out.append({
            "name": name,
            "email": email,
            "title": "",
            "role": role,
            "lob": "",
            "note": "From the calendar invite",
            "source": "calendar",
        })

    organizer = getattr(instance, "organizer", None) or {}
    if organizer.get("name") or organizer.get("email"):
        _add(organizer, "organizer")
    for person in getattr(instance, "attendees", None) or []:
        if person.get("is_resource") or (person.get("cutype") or "").upper() in ("RESOURCE", "ROOM"):
            continue
        if (person.get("partstat") or "").upper() == "DECLINED":
            continue
        _add(person, "attendee")
    return out


# ── Matching ─────────────────────────────────────────────────────────────────

def _describe(overlap_ratio: float, start_delta: float, instance_start: datetime,
              recording_start: datetime) -> str:
    minutes = int(round(abs(start_delta) / 60.0))
    percent = int(round(overlap_ratio * 100))
    if minutes == 0:
        return f"{percent}% overlap, starts with the recording"
    side = "before" if instance_start <= recording_start else "after"
    return f"{percent}% overlap, starts {minutes} min {side} the recording"


def match_session(instances, started_at_utc, ended_at_utc,
                  window_minutes: int = 20) -> dict:
    """Pick the calendar instance a recording belongs to.

    Overlap dominates: the instance sharing the most wall-clock time with the
    recording wins, with start proximity as the tie-break. All-day items are
    ignored, and an item is only considered when it starts within
    ``window_minutes`` of the recording or overlaps at least half of it.
    """
    recording_start = parse_iso_utc(started_at_utc)
    if recording_start is None:
        return {"best": None, "alternatives": []}
    recording_end = parse_iso_utc(ended_at_utc)
    if recording_end is None or recording_end <= recording_start:
        recording_end = recording_start + timedelta(minutes=DEFAULT_EVENT_MINUTES)
    recording_seconds = (recording_end - recording_start).total_seconds()
    window_seconds = max(1, int(window_minutes)) * 60.0

    scored: list[dict] = []
    for instance in instances or []:
        if instance.all_day or not instance.start or not instance.end:
            continue
        if (instance.status or "").upper() == "CANCELLED":
            continue
        if is_ghost_title(instance.summary):
            # Exchange keeps cancelled and rescheduled meetings in a published
            # feed with a title prefix instead of STATUS:CANCELLED.
            continue
        overlap = max(0.0, (
            min(instance.end, recording_end) - max(instance.start, recording_start)
        ).total_seconds())
        instance_seconds = (instance.end - instance.start).total_seconds()
        # Measure the overlap against whichever side is shorter: a recording
        # that runs long past a 30-minute meeting still belongs to it, and a
        # short recording inside a meeting still belongs to that meeting.
        shorter = min(recording_seconds, instance_seconds) if instance_seconds > 0 else recording_seconds
        overlap_ratio = min(1.0, overlap / shorter) if shorter else 0.0
        start_delta = (instance.start - recording_start).total_seconds()
        near = abs(start_delta) <= window_seconds
        if not near and overlap_ratio < 0.5:
            continue
        proximity = max(0.0, 1.0 - (abs(start_delta) / (window_seconds * 2)))
        score = min(1.0, 0.8 * overlap_ratio + 0.2 * proximity)
        if near:
            if overlap_ratio >= 0.6:
                # Confident: most of the recording sits inside this event and
                # the event started when the recording did.
                score = max(score, 0.8)
        else:
            # An all-day-ish block ("Focus time", 09:00 to 17:00) contains any
            # recording made inside it, so containment alone must never look as
            # good as a meeting that actually started with the recording.
            score = min(score, 0.75)
        if instance_seconds > BLOCK_MINIMUM_SECONDS:
            # A multi-hour block is a container, not a meeting. Its attendee
            # count would become the reanalysis ceiling, so it stays below the
            # storable threshold unless the recording really fills it.
            covered = overlap / instance_seconds if instance_seconds else 0.0
            if covered < 0.5:
                score = min(score, BLOCK_SCORE_CAP)
        scored.append({
            "instance": instance,
            "score": round(score, 3),
            "overlap_seconds": int(overlap),
            "start_delta_seconds": int(start_delta),
            "reason": _describe(overlap_ratio, start_delta, instance.start, recording_start),
        })

    scored.sort(
        key=lambda item: (item["score"], item["overlap_seconds"],
                          -abs(item["start_delta_seconds"])),
        reverse=True,
    )
    if not scored:
        return {"best": None, "alternatives": []}
    return {"best": scored[0], "alternatives": scored[1:]}


# ── Title normalization and count memory ─────────────────────────────────────

# Every stripper below is anchored to the END of the subject and must be
# introduced by a separator. An unanchored date or month pattern eats real
# words: "Mayfield 12 Oaks renewal" would collapse to "oaks renewal" and
# collide with a different meeting's key, which then hands that meeting's
# attendee count to this one.
_SEP = r"[\s\-(\[,:;|]+"
_TAIL = r"\s*[)\]]?\s*$"
_WEEK_SUFFIX_RE = re.compile(r"\b(week|wk)\s+(of|ending)\b.*$", re.IGNORECASE)
_DATE_TAIL_RE = re.compile(
    _SEP + r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?)" + _TAIL
)
_MONTH_TAIL_RE = re.compile(
    _SEP + r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
    r"\s*\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{4})?" + _TAIL,
    re.IGNORECASE,
)
_NUMBER_TAIL_RE = re.compile(_SEP + r"#\s*\d+" + _TAIL)
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def normalize_title(summary: str) -> str:
    """Reduce a meeting subject to a stable key for the count memory.

    "Ops Sync - Week of 9/1" and "Ops Sync (9/8)" both become "ops sync", so a
    recurring series keeps one remembered attendee count. Only a trailing date,
    month-day, week suffix or "#12" is removed; anything mid-subject is part of
    the meeting's name and stays.
    """
    text = (summary or "").strip().lower()
    if not text:
        return ""
    text = _WEEK_SUFFIX_RE.sub(" ", text)
    for pattern in (_DATE_TAIL_RE, _MONTH_TAIL_RE, _NUMBER_TAIL_RE):
        text = pattern.sub(" ", text)
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# ── Cache ────────────────────────────────────────────────────────────────────

def cache_path():
    return paths.data_dir() / "calendar_cache.json"


def load_cache() -> dict:
    try:
        with cache_path().open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(updates: dict) -> dict:
    """Merge ``updates`` into the cache file and return the full cache.

    The read-modify-write is held under a lock, and the temp file carries a
    unique name: the background refresh loop and a user-triggered refresh can
    both land here, and on Windows a shared temp name makes one writer's
    ``replace`` fail on the other's open handle.
    """
    with _cache_lock:
        data = load_cache()
        data.update(updates or {})
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            tmp.replace(path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return data


def cached_instances() -> list[Instance]:
    raw = load_cache().get("instances")
    return [
        Instance.from_dict(item)
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict)
    ]


def remember_expected_count(title_key: str, count) -> None:
    """Remember a confirmed attendee count for a recurring meeting title."""
    key = (title_key or "").strip()
    if not key or isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return
    with _cache_lock:
        counts = load_cache().get("title_counts")
        if not isinstance(counts, dict):
            counts = {}
        counts[key] = int(count)
        # Inside the lock: another writer must not land between the read and
        # the write and lose a count. _cache_lock is reentrant.
        save_cache({"title_counts": counts})


def recall_expected_count(title_key: str) -> int | None:
    key = (title_key or "").strip()
    if not key:
        return None
    counts = load_cache().get("title_counts")
    if not isinstance(counts, dict):
        return None
    value = counts.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def attendee_coverage(instances) -> float:
    """Share of instances that actually carry attendees (0.0 to 1.0)."""
    items = list(instances or [])
    if not items:
        return 0.0
    return round(sum(1 for inst in items if inst.attendees) / len(items), 3)
