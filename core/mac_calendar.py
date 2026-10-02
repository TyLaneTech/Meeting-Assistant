"""Reads the macOS Calendar app as a private alternative to a published feed.

A published Outlook calendar is an unauthenticated link: anyone holding it can
read every meeting. On a Mac the same meetings are usually already in the
Calendar app (System Settings > Internet Accounts > the work account, with
Calendars on), and EventKit can read them locally with nothing published.

The saved "link" is a pseudo-URL instead of an https:// one:

    macos-calendar://exchange       only calendars from Microsoft Exchange
                                    accounts: the work Microsoft 365 calendar,
                                    without personal Google or iCloud ones.
                                    This is what Settings saves.
    macos-calendar://all            every event calendar except birthdays and
                                    subscribed (holiday) calendars
    macos-calendar://<text>         only calendars whose name or account name
                                    contains <text>, e.g. macos-calendar://work

``export_ics`` turns the matching events into ICS text, so everything
downstream (parse, expand, match, name, attendee candidates) runs unchanged
on exactly the same code path as a published feed. EventKit has already
expanded recurrences, so every occurrence is written as a standalone event
with its own UID.

macOS asks once for Calendar access the first time this runs. The prompt is
attributed to the Meeting Assistant app bundle, whose Info.plist carries the
usage string (see launch.py).
"""
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone

from core import log as log
from core.calendar_feed import CalendarFeedError

UTC = timezone.utc

SCHEME = "macos-calendar://"
ALL = "all"
EXCHANGE = "exchange"

# How long a Calendar permission prompt may wait for the user to answer.
ACCESS_PROMPT_TIMEOUT = 120.0

# EventKit enum values. Spelled out so the pure helpers below can be tested
# without PyObjC installed.
_EK_STATUS_NOT_DETERMINED = 0
_EK_STATUS_RESTRICTED = 1
_EK_STATUS_DENIED = 2
_EK_STATUS_FULL_ACCESS = 3      # also EKAuthorizationStatusAuthorized before macOS 14
_EK_STATUS_WRITE_ONLY = 4

_EK_CALENDAR_EXCHANGE = 2
_EK_CALENDAR_SUBSCRIPTION = 3
_EK_CALENDAR_BIRTHDAY = 4

_EK_EVENT_TENTATIVE = 2
_EK_EVENT_CANCELED = 3

_PARTSTAT = {1: "NEEDS-ACTION", 2: "ACCEPTED", 3: "DECLINED", 4: "TENTATIVE",
             5: "DELEGATED", 6: "COMPLETED", 7: "IN-PROCESS"}
_ROLE = {1: "REQ-PARTICIPANT", 2: "OPT-PARTICIPANT", 3: "CHAIR", 4: "NON-PARTICIPANT"}
_CUTYPE = {1: "INDIVIDUAL", 2: "ROOM", 3: "RESOURCE", 4: "GROUP"}

_ACCESS_HELP = (
    "Open System Settings > Privacy & Security > Calendars and turn on "
    "Meeting Assistant, then try again."
)


def is_mac_calendar_url(url) -> bool:
    return (url or "").strip().lower().startswith(SCHEME)


def calendar_filter(url) -> str:
    """The text after the scheme, lower-cased; "" means every calendar."""
    rest = (url or "").strip()[len(SCHEME):].strip().strip("/")
    return "" if rest.lower() in ("", ALL) else rest.lower()


# ── ICS writing (pure, testable without EventKit) ────────────────────────────

def _escape_text(value) -> str:
    text = str(value or "")
    return (text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
                .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n"))


def _param(value) -> str:
    """A parameter value: quoted, with the characters ICS cannot carry removed."""
    text = str(value or "").replace('"', "").replace("\r", " ").replace("\n", " ")
    return f'"{text}"'


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _person_line(name: str, person: dict) -> str:
    params = []
    if person.get("name"):
        params.append(f"CN={_param(person['name'])}")
    for key, ics_key in (("cutype", "CUTYPE"), ("role", "ROLE"), ("partstat", "PARTSTAT")):
        if person.get(key):
            params.append(f"{ics_key}={person[key]}")
    email = person.get("email") or ""
    value = f"mailto:{email}" if email else "invalid:nomail"
    return name + "".join(f";{p}" for p in params) + f":{value}"


def events_to_ics(events: list) -> str:
    """Serialise event dicts (the shape ``_event_dict`` returns) to ICS text.

    Timed events are written in UTC. All-day events carry local DATE values,
    which the parser reads in the configured default zone, the same as an
    Outlook feed's all-day entries.
    """
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//Meeting Assistant//macOS Calendar//EN", "CALSCALE:GREGORIAN"]
    for ev in events:
        start, end = ev.get("start"), ev.get("end")
        if start is None:
            continue
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{_escape_text(ev.get('uid') or '')}")
        if ev.get("all_day"):
            first = start.date()
            last = end.date() if end and end.date() > first else first + timedelta(days=1)
            lines.append(f"DTSTART;VALUE=DATE:{first.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{last.strftime('%Y%m%d')}")
        else:
            lines.append(f"DTSTART:{_utc_stamp(start)}")
            if end is not None and end > start:
                lines.append(f"DTEND:{_utc_stamp(end)}")
        lines.append(f"SUMMARY:{_escape_text(ev.get('summary'))}")
        if ev.get("location"):
            lines.append(f"LOCATION:{_escape_text(ev['location'])}")
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{_escape_text(ev['description'])}")
        if ev.get("url"):
            lines.append(f"URL:{_escape_text(ev['url'])}")
        if ev.get("status"):
            lines.append(f"STATUS:{ev['status']}")
        if ev.get("organizer"):
            lines.append(_person_line("ORGANIZER", ev["organizer"]))
        for attendee in ev.get("attendees") or []:
            lines.append(_person_line("ATTENDEE", attendee))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ── EventKit ─────────────────────────────────────────────────────────────────

def _nsdate_to_utc(nsdate) -> datetime | None:
    if nsdate is None:
        return None
    return datetime.fromtimestamp(float(nsdate.timeIntervalSince1970()), tz=UTC)


def _nsdate_to_local_naive(nsdate) -> datetime | None:
    """For all-day events: the wall-clock date the Calendar app shows."""
    if nsdate is None:
        return None
    return datetime.fromtimestamp(float(nsdate.timeIntervalSince1970()))


def _str(value) -> str:
    return "" if value is None else str(value)


def _participant(p) -> dict:
    email = ""
    try:
        url = p.URL()
        spec = _str(url.resourceSpecifier()) if url is not None else ""
        email = spec[7:] if spec.lower().startswith("mailto:") else spec
        if "@" not in email:
            email = ""
    except Exception:
        email = ""
    return {
        "name": _str(p.name()),
        "email": email.strip().lower(),
        "partstat": _PARTSTAT.get(int(p.participantStatus()), ""),
        "role": _ROLE.get(int(p.participantRole()), ""),
        "cutype": _CUTYPE.get(int(p.participantType()), ""),
    }


def _event_dict(ev) -> dict:
    all_day = bool(ev.isAllDay())
    if all_day:
        start = _nsdate_to_local_naive(ev.startDate())
        # EventKit ends an all-day event at 23:59:59 on its last day; ICS wants
        # the exclusive next day.
        end_last = _nsdate_to_local_naive(ev.endDate())
        end = (end_last.replace(hour=0, minute=0, second=0, microsecond=0)
               + timedelta(days=1)) if end_last else None
    else:
        start = _nsdate_to_utc(ev.startDate())
        end = _nsdate_to_utc(ev.endDate())

    base_uid = _str(ev.calendarItemExternalIdentifier()) or _str(ev.eventIdentifier())
    uid = base_uid
    try:
        recurring = bool(ev.hasRecurrenceRules())
    except Exception:
        recurring = False
    if recurring and start is not None:
        # Occurrences share an identifier; the start makes each one stable and unique.
        uid = f"{base_uid}_{start.strftime('%Y%m%dT%H%M%S')}"

    status_code = int(ev.status())
    status = ("CANCELLED" if status_code == _EK_EVENT_CANCELED
              else "TENTATIVE" if status_code == _EK_EVENT_TENTATIVE else "")

    url = ""
    try:
        nsurl = ev.URL()
        url = _str(nsurl.absoluteString()) if nsurl is not None else ""
    except Exception:
        url = ""

    organizer = None
    try:
        if ev.organizer() is not None:
            organizer = _participant(ev.organizer())
    except Exception:
        organizer = None

    attendees = []
    try:
        for p in (ev.attendees() or []):
            attendees.append(_participant(p))
    except Exception:
        attendees = []

    return {
        "uid": uid,
        "summary": _str(ev.title()),
        "location": _str(ev.location()),
        "description": _str(ev.notes()),
        "url": url,
        "status": status,
        "start": start,
        "end": end,
        "all_day": all_day,
        "organizer": organizer,
        "attendees": attendees,
    }


def _require_eventkit():
    if sys.platform != "darwin":
        raise CalendarFeedError("macos-calendar:// only works on a Mac.")
    try:
        import EventKit  # noqa: F401 - PyObjC binding, macOS only
    except ImportError as exc:
        raise CalendarFeedError(
            "The macOS Calendar bridge is not installed (pyobjc-framework-EventKit). "
            "Restart Meeting Assistant so the launcher installs it."
        ) from exc
    return EventKit


def _ensure_access(EventKit) -> None:
    """Block until Calendar access is granted, asking once if never asked."""
    status = int(EventKit.EKEventStore.authorizationStatusForEntityType_(EventKit.EKEntityTypeEvent))
    if status == _EK_STATUS_FULL_ACCESS:
        return
    if status == _EK_STATUS_WRITE_ONLY:
        raise CalendarFeedError(
            "Meeting Assistant has add-only Calendar access, which cannot read events. "
            + _ACCESS_HELP)
    if status in (_EK_STATUS_DENIED, _EK_STATUS_RESTRICTED):
        raise CalendarFeedError("Calendar access is turned off for Meeting Assistant. " + _ACCESS_HELP)

    store = EventKit.EKEventStore.alloc().init()
    done = threading.Event()
    result = {"granted": False}

    def _completion(granted, _error):
        result["granted"] = bool(granted)
        done.set()

    log.info("calendar", "Asking macOS for Calendar access.")
    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(_completion)
    else:  # macOS 13 and earlier
        store.requestAccessToEntityType_completion_(EventKit.EKEntityTypeEvent, _completion)
    if not done.wait(ACCESS_PROMPT_TIMEOUT):
        raise CalendarFeedError("No answer to the Calendar access prompt. Try again and click Allow.")
    if not result["granted"]:
        raise CalendarFeedError("Calendar access was not allowed. " + _ACCESS_HELP)


def _matching_calendars(store, EventKit, wanted: str) -> list:
    chosen = []
    for cal in store.calendarsForEntityType_(EventKit.EKEntityTypeEvent) or []:
        kind = int(cal.type())
        title = _str(cal.title())
        source = _str(cal.source().title()) if cal.source() is not None else ""
        if kind == _EK_CALENDAR_BIRTHDAY:
            continue
        if wanted == EXCHANGE:
            if kind == _EK_CALENDAR_EXCHANGE:
                chosen.append(cal)
        elif wanted:
            if wanted in title.lower() or wanted in source.lower():
                chosen.append(cal)
        elif kind not in (_EK_CALENDAR_SUBSCRIPTION, _EK_CALENDAR_BIRTHDAY):
            chosen.append(cal)
    return chosen


def read_events(url: str, window_start: datetime, window_end: datetime) -> list:
    """Event dicts for every occurrence in the window, from the matching calendars."""
    EventKit = _require_eventkit()
    _ensure_access(EventKit)
    import Foundation

    # A store created before access was granted can stay empty; use a fresh one.
    store = EventKit.EKEventStore.alloc().init()
    wanted = calendar_filter(url)
    calendars = _matching_calendars(store, EventKit, wanted)
    if not calendars:
        if wanted == EXCHANGE:
            raise CalendarFeedError(
                "No work (Microsoft Exchange) calendar in the macOS Calendar app. Add your "
                "work account in System Settings > Internet Accounts > Microsoft Exchange "
                "with Calendars turned on.")
        if wanted:
            raise CalendarFeedError(
                f"No calendar in the macOS Calendar app has \"{wanted}\" in its name or "
                "account. Check the account is added in System Settings > Internet "
                "Accounts with Calendars turned on.")
        raise CalendarFeedError(
            "The macOS Calendar app has no calendars. Add your work account in "
            "System Settings > Internet Accounts with Calendars turned on.")

    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        Foundation.NSDate.dateWithTimeIntervalSince1970_(window_start.timestamp()),
        Foundation.NSDate.dateWithTimeIntervalSince1970_(window_end.timestamp()),
        calendars,
    )
    events = []
    for ev in store.eventsMatchingPredicate_(predicate) or []:
        try:
            events.append(_event_dict(ev))
        except Exception as exc:  # one odd event must not sink the whole read
            log.warn("calendar", f"Skipped a macOS Calendar event ({type(exc).__name__}).")
    names = ", ".join(sorted({f"{_str(c.title())} ({_str(c.source().title()) if c.source() is not None else '?'})"
                              for c in calendars}))
    log.info("calendar", f"Read {len(events)} event(s) from {len(calendars)} macOS calendar(s): {names}")
    return events


def export_ics(url: str, window_start: datetime, window_end: datetime) -> str:
    """The macOS Calendar events in the window, as ICS text for parse_ics.

    Raises only CalendarFeedError, which is what every caller handles: a PyObjC
    failure would otherwise escape the refresh loop.
    """
    try:
        events = read_events(url, window_start, window_end)
    except CalendarFeedError:
        raise
    except Exception as exc:
        log.warn("calendar", f"macOS Calendar read failed: {type(exc).__name__}: {exc}")
        raise CalendarFeedError(
            f"Could not read the macOS Calendar ({type(exc).__name__}).") from exc
    return events_to_ics(events)
