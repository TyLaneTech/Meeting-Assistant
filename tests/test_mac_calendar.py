"""core.mac_calendar: the macOS Calendar events -> ICS bridge.

EventKit itself needs a Mac and a Calendar grant, so these tests cover the
part that decides correctness: the ICS it writes must come back through the
real parse_ics / expand / match_session unchanged.
"""
from datetime import datetime, timedelta, timezone

from core import calendar_feed, mac_calendar

UTC = timezone.utc


def _ev(**overrides):
    base = {
        "uid": "AAMk-1",
        "summary": "Renewal review, Q4; \"final\" pass",
        "location": "Teams",
        "description": "Line one\nJoin: https://teams.microsoft.com/l/meetup-join/abc",
        "url": "",
        "status": "",
        "start": datetime(2026, 10, 5, 15, 0, tzinfo=UTC),
        "end": datetime(2026, 10, 5, 16, 0, tzinfo=UTC),
        "all_day": False,
        "organizer": {"name": "Ryan Humphries", "email": "ryan@example.com",
                      "partstat": "ACCEPTED", "role": "CHAIR", "cutype": "INDIVIDUAL"},
        "attendees": [
            {"name": "Doe, Jane", "email": "jane@example.com", "partstat": "ACCEPTED",
             "role": "REQ-PARTICIPANT", "cutype": "INDIVIDUAL"},
            {"name": "Conf Room 4", "email": "room4@example.com", "partstat": "ACCEPTED",
             "role": "NON-PARTICIPANT", "cutype": "ROOM"},
        ],
    }
    base.update(overrides)
    return base


def _parse(events):
    return calendar_feed.parse_ics(mac_calendar.events_to_ics(events), default_tz="America/Chicago")


def test_url_helpers():
    assert mac_calendar.is_mac_calendar_url("macos-calendar://exchange")
    assert mac_calendar.is_mac_calendar_url("  MACOS-CALENDAR://Work ")
    assert not mac_calendar.is_mac_calendar_url("https://outlook.office365.com/x.ics")
    assert mac_calendar.calendar_filter("macos-calendar://all") == ""
    assert mac_calendar.calendar_filter("macos-calendar://") == ""
    assert mac_calendar.calendar_filter("macos-calendar://Exchange/") == "exchange"
    assert mac_calendar.calendar_filter("macos-calendar://Higginbotham") == "higginbotham"


def test_mask_shows_the_pseudo_url_whole():
    # Not a credential: the filter is safe to show, and the UI keys off the scheme.
    assert calendar_feed.mask_url("macos-calendar://exchange") == "macos-calendar://exchange"


def test_timed_event_round_trips_with_escaping_and_people():
    (event,) = _parse([_ev()])
    assert event.uid == "AAMk-1"
    assert event.summary == "Renewal review, Q4; \"final\" pass"
    assert event.location == "Teams"
    assert "meetup-join/abc" in event.description
    assert event.start == datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    assert event.end == datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
    assert not event.all_day
    assert event.organizer["email"] == "ryan@example.com"
    names = {a["name"]: a for a in event.attendees}
    assert names["Doe, Jane"]["email"] == "jane@example.com"
    assert names["Doe, Jane"]["partstat"] == "ACCEPTED"
    assert names["Conf Room 4"]["is_resource"] is True


def test_all_day_event_uses_dates():
    (event,) = _parse([_ev(all_day=True,
                           start=datetime(2026, 11, 26), end=datetime(2026, 11, 27),
                           attendees=[], organizer=None)])
    assert event.all_day
    assert event.start_local.date().isoformat() == "2026-11-26"


def test_cancelled_and_tentative_status():
    cancelled, tentative = _parse([_ev(uid="c", status="CANCELLED"),
                                   _ev(uid="t", status="TENTATIVE")])
    assert cancelled.status == "CANCELLED"
    assert tentative.status == "TENTATIVE"


def test_expanded_occurrences_survive_expand_and_match():
    start = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
    occurrences = [
        _ev(uid=f"weekly_{(start + timedelta(days=7 * i)).strftime('%Y%m%dT%H%M%S')}",
            summary="Weekly ops", start=start + timedelta(days=7 * i),
            end=start + timedelta(days=7 * i, hours=1))
        for i in range(3)
    ]
    events = _parse(occurrences)
    instances = calendar_feed.expand(events, start - timedelta(days=1), start + timedelta(days=30))
    assert len(instances) == 3
    assert len({i.uid for i in instances}) == 3

    # A recording that ran 15:02-15:55 on the second week matches that occurrence.
    rec_start = start + timedelta(days=7, minutes=2)
    result = calendar_feed.match_session(instances, rec_start, rec_start + timedelta(minutes=53), 20)
    assert result["best"] is not None
    assert result["best"]["instance"].start == start + timedelta(days=7)


def test_events_without_start_are_skipped():
    assert _parse([_ev(start=None)]) == []
