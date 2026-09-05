"""Tests for the published-calendar (ICS) reader.

Everything here is offline: the fixtures are inline ICS documents and the one
fetch test drives a fake urlopen.
"""
import gzip
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message

import pytest

from core import calendar_feed, paths

UTC = timezone.utc


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _wrap(body: str) -> str:
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN\r\n"
        f"{body}"
        "END:VCALENDAR\r\n"
    )


# ── Times and zones ──────────────────────────────────────────────────────────

def test_utc_z_times_and_folded_lines():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:utc-1\r\n"
        "SUMMARY:Quarterly review with a very long subject line that Outlook \r\n"
        " folded across two lines\r\n"
        "DTSTART:20260903T150000Z\r\n"
        "DTEND:20260903T160000Z\r\n"
        "END:VEVENT\r\n"
    )
    events = calendar_feed.parse_ics(text)
    assert len(events) == 1
    event = events[0]
    assert event.summary == (
        "Quarterly review with a very long subject line that Outlook folded "
        "across two lines"
    )
    assert event.start == _utc(2026, 9, 3, 15)
    assert event.end == _utc(2026, 9, 3, 16)
    assert event.all_day is False


def test_windows_tzid_without_vtimezone_crosses_dst():
    """"Central Standard Time" is a Windows display name, not a fixed offset.

    January is CST (UTC-6) and July is CDT (UTC-5), so a naive fixed offset
    would put the summer meeting an hour wrong.
    """
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:winter\r\n"
        "SUMMARY:Winter standup\r\n"
        "DTSTART;TZID=Central Standard Time:20260115T090000\r\n"
        "DTEND;TZID=Central Standard Time:20260115T093000\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:summer\r\n"
        "SUMMARY:Summer standup\r\n"
        "DTSTART;TZID=Central Standard Time:20260715T090000\r\n"
        "DTEND;TZID=Central Standard Time:20260715T093000\r\n"
        "END:VEVENT\r\n"
    )
    notes = []
    events = calendar_feed.parse_ics(text, notes=notes)
    starts = {event.uid: event.start for event in events}
    assert starts["winter"] == _utc(2026, 1, 15, 15)
    assert starts["summer"] == _utc(2026, 7, 15, 14)
    assert "TZID Central Standard Time mapped to America/Chicago" in notes


def test_tzid_resolved_from_vtimezone_block():
    text = _wrap(
        "BEGIN:VTIMEZONE\r\n"
        "TZID:Customized Time Zone\r\n"
        "BEGIN:STANDARD\r\n"
        "DTSTART:16011104T020000\r\n"
        "RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=11\r\n"
        "TZOFFSETFROM:-0500\r\n"
        "TZOFFSETTO:-0600\r\n"
        "END:STANDARD\r\n"
        "BEGIN:DAYLIGHT\r\n"
        "DTSTART:16010311T020000\r\n"
        "RRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3\r\n"
        "TZOFFSETFROM:-0600\r\n"
        "TZOFFSETTO:-0500\r\n"
        "END:DAYLIGHT\r\n"
        "END:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:custom-tz\r\n"
        "SUMMARY:Custom zone meeting\r\n"
        "DTSTART;TZID=Customized Time Zone:20260115T090000\r\n"
        "DTEND;TZID=Customized Time Zone:20260115T100000\r\n"
        "END:VEVENT\r\n"
    )
    notes = []
    events = calendar_feed.parse_ics(text, notes=notes)
    assert events[0].start == _utc(2026, 1, 15, 15)
    assert events[0].end == _utc(2026, 1, 15, 16)
    assert any("VTIMEZONE" in note for note in notes)


def test_floating_time_uses_the_default_zone():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:floating\r\n"
        "SUMMARY:Floating meeting\r\n"
        "DTSTART:20260715T090000\r\n"
        "DURATION:PT45M\r\n"
        "END:VEVENT\r\n"
    )
    notes = []
    events = calendar_feed.parse_ics(text, notes=notes)
    assert events[0].start == _utc(2026, 7, 15, 14)
    assert events[0].end == _utc(2026, 7, 15, 14, 45)
    assert any("Floating time" in note for note in notes)

    denver = calendar_feed.parse_ics(text, default_tz="America/Denver")
    assert denver[0].start == _utc(2026, 7, 15, 15)


def test_parse_duration_forms():
    assert calendar_feed.parse_duration("PT30M") == timedelta(minutes=30)
    assert calendar_feed.parse_duration("P1DT2H30M") == timedelta(days=1, hours=2, minutes=30)
    assert calendar_feed.parse_duration("P2W") == timedelta(weeks=2)
    assert calendar_feed.parse_duration("nonsense") is None


# ── Attendees ────────────────────────────────────────────────────────────────

ATTENDEE_ICS = _wrap(
    "BEGIN:VEVENT\r\n"
    "UID:attendees-1\r\n"
    "SUMMARY:Renewal strategy\r\n"
    "DTSTART:20260903T150000Z\r\n"
    "DTEND:20260903T160000Z\r\n"
    "ORGANIZER;CN=Alex Chen:mailto:achen@example.com\r\n"
    "ATTENDEE;CN=Jordan Blake;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
    "PARTSTAT=ACCEPTED:mailto:jordan@example.com\r\n"
    "ATTENDEE;CN=Sam Rivera;PARTSTAT=TENTATIVE:mailto:sam@example.com\r\n"
    "ATTENDEE;CN=Skipping This One;PARTSTAT=DECLINED:mailto:nope@example.com\r\n"
    "ATTENDEE;CN=Conference Room 3;CUTYPE=RESOURCE;"
    "PARTSTAT=ACCEPTED:mailto:room3@example.com\r\n"
    "ATTENDEE;CN=Alex Chen:mailto:achen@example.com\r\n"
    "END:VEVENT\r\n"
)


def test_attendee_parsing_and_expected_count():
    event = calendar_feed.parse_ics(ATTENDEE_ICS)[0]
    assert event.organizer == {"name": "Alex Chen", "email": "achen@example.com"}
    by_email = {person["email"]: person for person in event.attendees}
    assert by_email["jordan@example.com"]["partstat"] == "ACCEPTED"
    assert by_email["jordan@example.com"]["role"] == "REQ-PARTICIPANT"
    assert by_email["room3@example.com"]["is_resource"] is True
    assert by_email["room3@example.com"]["cutype"] == "RESOURCE"

    # Alex (organizer, also listed as an attendee) + Jordan + Sam. The room,
    # the decliner and the duplicate do not count.
    assert calendar_feed.expected_count(event) == 3


def test_candidate_people_skips_rooms_and_decliners():
    event = calendar_feed.parse_ics(ATTENDEE_ICS)[0]
    people = calendar_feed.candidate_people(event)
    assert [person["name"] for person in people] == [
        "Alex Chen", "Jordan Blake", "Sam Rivera",
    ]
    assert people[0]["role"] == "organizer"
    assert all(person["source"] == "calendar" for person in people)


def test_event_without_attendees_has_no_expected_count():
    """The Microsoft regression: ATTENDEE lines silently vanish from the feed."""
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:no-attendees\r\n"
        "SUMMARY:Stripped meeting\r\n"
        "DTSTART:20260903T150000Z\r\n"
        "DTEND:20260903T160000Z\r\n"
        "END:VEVENT\r\n"
    )
    event = calendar_feed.parse_ics(text)[0]
    assert event.attendees == []
    assert calendar_feed.expected_count(event) is None


def test_private_items_are_flagged():
    text = _wrap(
        "BEGIN:VEVENT\r\nUID:p1\r\nSUMMARY:Dentist\r\nCLASS:PRIVATE\r\n"
        "DTSTART:20260903T150000Z\r\nDTEND:20260903T160000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:p2\r\nSUMMARY:Private Appointment\r\n"
        "DTSTART:20260903T170000Z\r\nDTEND:20260903T180000Z\r\nEND:VEVENT\r\n"
    )
    events = calendar_feed.parse_ics(text)
    assert all(event.is_private for event in events)


# ── Expansion ────────────────────────────────────────────────────────────────

RECURRING_ICS = _wrap(
    "BEGIN:VEVENT\r\n"
    "UID:weekly-1\r\n"
    "SUMMARY:Weekly sync\r\n"
    "DTSTART;TZID=Central Standard Time:20260903T090000\r\n"
    "DTEND;TZID=Central Standard Time:20260903T093000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=4\r\n"
    "EXDATE;TZID=Central Standard Time:20260910T090000\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:weekly-1\r\n"
    "SUMMARY:Weekly sync (moved)\r\n"
    "RECURRENCE-ID;TZID=Central Standard Time:20260917T090000\r\n"
    "DTSTART;TZID=Central Standard Time:20260917T100000\r\n"
    "DTEND;TZID=Central Standard Time:20260917T103000\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:cancelled-1\r\n"
    "SUMMARY:Cancelled budget call\r\n"
    "STATUS:CANCELLED\r\n"
    "DTSTART:20260904T150000Z\r\n"
    "DTEND:20260904T160000Z\r\n"
    "END:VEVENT\r\n"
)


def test_expand_applies_rrule_exdate_override_and_cancellation():
    events = calendar_feed.parse_ics(RECURRING_ICS)
    instances = calendar_feed.expand(events, _utc(2026, 9, 1), _utc(2026, 10, 1))

    assert [inst.start for inst in instances] == [
        _utc(2026, 9, 3, 14),    # first occurrence, CDT
        _utc(2026, 9, 17, 15),   # RECURRENCE-ID override, moved an hour later
        _utc(2026, 9, 24, 14),
    ]
    # The EXDATE occurrence is gone and the cancelled event never appears.
    assert _utc(2026, 9, 10, 14) not in [inst.start for inst in instances]
    assert all("Cancelled" not in inst.summary for inst in instances)
    assert instances[1].summary == "Weekly sync (moved)"


def test_expand_caps_instances_per_series():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:daily\r\n"
        "SUMMARY:Daily standup\r\n"
        "DTSTART:20260101T150000Z\r\n"
        "DTEND:20260101T151500Z\r\n"
        "RRULE:FREQ=DAILY\r\n"
        "END:VEVENT\r\n"
    )
    events = calendar_feed.parse_ics(text)
    instances = calendar_feed.expand(
        events, _utc(2026, 1, 1), _utc(2027, 1, 1), max_per_series=10
    )
    # The cap must keep the NEWEST occurrences: a recording being matched is far
    # more likely to belong to a recent instance than to the series' first day.
    assert len(instances) == 10
    assert instances[-1].start == _utc(2026, 12, 31, 15)
    assert instances[0].start == _utc(2026, 12, 22, 15)


def test_expand_handles_until_without_zulu():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:until-naive\r\n"
        "SUMMARY:Bounded series\r\n"
        "DTSTART:20260903T150000Z\r\n"
        "DTEND:20260903T153000Z\r\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20260917T150000\r\n"
        "END:VEVENT\r\n"
    )
    events = calendar_feed.parse_ics(text)
    instances = calendar_feed.expand(events, _utc(2026, 9, 1), _utc(2026, 10, 1))
    assert [inst.start for inst in instances] == [
        _utc(2026, 9, 3, 15), _utc(2026, 9, 10, 15), _utc(2026, 9, 17, 15),
    ]


# ── Matching ─────────────────────────────────────────────────────────────────

MATCH_ICS = _wrap(
    "BEGIN:VEVENT\r\n"
    "UID:overlapping\r\n"
    "SUMMARY:Renewal strategy\r\n"
    "DTSTART:20260903T151000Z\r\n"
    "DTEND:20260903T161000Z\r\n"
    "ATTENDEE;CN=Jordan Blake:mailto:jordan@example.com\r\n"
    "ATTENDEE;CN=Sam Rivera:mailto:sam@example.com\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:nearer-start\r\n"
    "SUMMARY:Quick check-in\r\n"
    "DTSTART:20260903T145000Z\r\n"
    "DTEND:20260903T150000Z\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:all-day\r\n"
    "SUMMARY:Out of office\r\n"
    "DTSTART;VALUE=DATE:20260903\r\n"
    "DTEND;VALUE=DATE:20260904\r\n"
    "END:VEVENT\r\n"
)


def _match_instances():
    events = calendar_feed.parse_ics(MATCH_ICS)
    return calendar_feed.expand(events, _utc(2026, 9, 1), _utc(2026, 9, 10))


def test_match_prefers_overlap_over_a_nearer_start():
    instances = _match_instances()
    assert any(inst.all_day for inst in instances)

    result = calendar_feed.match_session(
        instances, _utc(2026, 9, 3, 15), _utc(2026, 9, 3, 15, 50)
    )
    best = result["best"]
    assert best["instance"].uid == "overlapping"
    assert best["score"] >= 0.8
    assert best["overlap_seconds"] == 40 * 60
    assert "overlap" in best["reason"]
    # The all-day item is ignored entirely; only the nearer-start event remains.
    assert [alt["instance"].uid for alt in result["alternatives"]] == ["nearer-start"]
    assert result["alternatives"][0]["score"] < 0.5


def test_match_accepts_naive_utc_session_timestamps():
    """Session timestamps are naive UTC ISO strings, straight from storage."""
    instances = _match_instances()
    result = calendar_feed.match_session(
        instances, "2026-09-03T15:00:00", "2026-09-03T15:50:00"
    )
    assert result["best"]["instance"].uid == "overlapping"
    assert calendar_feed.expected_count(result["best"]["instance"]) == 2


def test_match_returns_nothing_for_a_distant_recording():
    instances = _match_instances()
    result = calendar_feed.match_session(
        instances, _utc(2026, 9, 3, 22), _utc(2026, 9, 3, 22, 30)
    )
    assert result == {"best": None, "alternatives": []}


def test_match_ignores_all_day_items_even_when_they_span_the_recording():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:all-day-only\r\n"
        "SUMMARY:Conference day\r\n"
        "DTSTART;VALUE=DATE:20260903\r\n"
        "DTEND;VALUE=DATE:20260904\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 9, 1), _utc(2026, 9, 10)
    )
    assert instances and instances[0].all_day is True
    result = calendar_feed.match_session(
        instances, _utc(2026, 9, 3, 16), _utc(2026, 9, 3, 17)
    )
    assert result["best"] is None


# ── Title normalization and count memory ─────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ops Sync - Week of 9/1", "ops sync"),
        ("Ops Sync (9/8)", "ops sync"),
        ("Ops Sync 2026-09-15", "ops sync"),
        ("Ops Sync #12", "ops sync"),
        ("Ops Sync - Sept 22", "ops sync"),
        ("  OPS   sync  ", "ops sync"),
        ("", ""),
    ],
)
def test_normalize_title(raw, expected):
    assert calendar_feed.normalize_title(raw) == expected


def test_expected_count_memory_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    key = calendar_feed.normalize_title("Ops Sync - Week of 9/1")

    assert calendar_feed.recall_expected_count(key) is None
    calendar_feed.remember_expected_count(key, 5)
    assert calendar_feed.recall_expected_count(key) == 5

    # Junk never lands in the memory.
    calendar_feed.remember_expected_count(key, 0)
    calendar_feed.remember_expected_count("", 9)
    calendar_feed.remember_expected_count(key, True)
    assert calendar_feed.recall_expected_count(key) == 5
    assert calendar_feed.recall_expected_count("never seen") is None


def test_cache_round_trip_preserves_instances(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    instances = _match_instances()
    calendar_feed.save_cache({
        "fetched_at": "2026-09-03T12:00:00+00:00",
        "instances": [inst.to_dict() for inst in instances],
    })
    calendar_feed.remember_expected_count("ops sync", 4)

    restored = calendar_feed.cached_instances()
    assert [inst.uid for inst in restored] == [inst.uid for inst in instances]
    assert restored[0].start == instances[0].start
    assert restored[0].start.tzinfo is not None
    # Saving the counts must not drop the cached instances.
    assert calendar_feed.load_cache()["fetched_at"] == "2026-09-03T12:00:00+00:00"
    assert calendar_feed.recall_expected_count("ops sync") == 4


def test_attendee_coverage():
    instances = _match_instances()
    assert calendar_feed.attendee_coverage(instances) == round(1 / 3, 3)
    assert calendar_feed.attendee_coverage([]) == 0.0


# ── Masking ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://outlook.office365.com/owa/calendar/abc123@example.com/"
            "def4567890/calendar.ics",
            "https://outlook.office365.com/...ar.ics",
        ),
        ("https://outlook.office365.com", "https://outlook.office365.com"),
        ("", ""),
        ("not-a-url-secret", "...secret"),
    ],
)
def test_mask_url(url, expected):
    assert calendar_feed.mask_url(url) == expected


# ── Fetching ─────────────────────────────────────────────────────────────────

FEED_URL = "https://outlook.office365.com/owa/calendar/guid@example.com/guid/calendar.ics"


class _FakeResponse:
    def __init__(self, body, content_encoding=None):
        self._body = body
        self.headers = Message()
        self.headers["Content-Type"] = "text/calendar; charset=utf-8"
        if content_encoding:
            self.headers["Content-Encoding"] = content_encoding

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _http_error(code):
    return urllib.error.HTTPError(FEED_URL, code, "boom", Message(), None)


def test_fetch_retries_once_after_a_500():
    """Exchange answers some non-browser agents with a 500 on the first try."""
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        if len(calls) == 1:
            raise _http_error(500)
        return _FakeResponse(ATTENDEE_ICS.encode("utf-8"))

    text = calendar_feed.fetch_ics(FEED_URL, opener=opener)
    assert "BEGIN:VCALENDAR" in text
    assert len(calls) == 2
    assert calls[0].get_header("User-agent").startswith("Mozilla/5.0")
    assert "text/calendar" in calls[0].get_header("Accept")


def test_fetch_gives_up_after_a_second_500_without_leaking_the_url():
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        raise _http_error(503)

    with pytest.raises(calendar_feed.CalendarFeedError) as excinfo:
        calendar_feed.fetch_ics(FEED_URL, opener=opener)
    assert len(calls) == 2
    assert "503" in str(excinfo.value)
    assert FEED_URL not in str(excinfo.value)


def test_fetch_does_not_retry_a_404():
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        raise _http_error(404)

    with pytest.raises(calendar_feed.CalendarFeedError) as excinfo:
        calendar_feed.fetch_ics(FEED_URL, opener=opener)
    assert len(calls) == 1
    assert "404" in str(excinfo.value)


def test_fetch_retries_a_transport_failure_and_decompresses_gzip():
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.URLError("connection reset")
        return _FakeResponse(gzip.compress(ATTENDEE_ICS.encode("utf-8")), "gzip")

    text = calendar_feed.fetch_ics(FEED_URL, opener=opener)
    assert "Renewal strategy" in text
    assert len(calls) == 2


def test_fetch_rejects_a_non_calendar_response():
    def opener(request, timeout=None):
        return _FakeResponse(b"<html>sign in</html>")

    with pytest.raises(calendar_feed.CalendarFeedError) as excinfo:
        calendar_feed.fetch_ics(FEED_URL, opener=opener)
    assert "did not return a calendar feed" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    ["", "   ", "ftp://example.com/cal.ics", "http://outlook.office365.com/cal.ics"],
)
def test_fetch_rejects_unusable_urls(url):
    def opener(request, timeout=None):  # pragma: no cover - must not be called
        raise AssertionError("network must not be touched")

    with pytest.raises(calendar_feed.CalendarFeedError):
        calendar_feed.fetch_ics(url, opener=opener)


# ── Recurrence across a daylight saving change ───────────────────────────────

DST_SERIES_ICS = _wrap(
    "BEGIN:VEVENT\r\n"
    "UID:weekly-dst\r\n"
    "SUMMARY:Weekly ops\r\n"
    "DTSTART;TZID=Central Standard Time:20260210T090000\r\n"
    "DTEND;TZID=Central Standard Time:20260210T093000\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=8\r\n"
    "EXDATE;TZID=Central Standard Time:20260317T090000\r\n"
    "END:VEVENT\r\n"
)


def test_recurrence_keeps_the_local_hour_across_dst():
    """A 09:00 Central weekly meeting stays at 09:00 Central after March 8.

    Expanding on the UTC instant instead would repeat 15:00Z forever, putting
    every spring occurrence an hour late and leaving TZID EXDATEs unmatched.
    """
    events = calendar_feed.parse_ics(DST_SERIES_ICS)
    assert events[0].start == _utc(2026, 2, 10, 15)
    assert events[0].start_local.hour == 9

    instances = calendar_feed.expand(events, _utc(2026, 2, 1), _utc(2026, 5, 1))
    starts = [inst.start for inst in instances]

    assert _utc(2026, 2, 10, 15) in starts      # CST, UTC-6
    assert _utc(2026, 3, 10, 14) in starts      # CDT, UTC-5
    assert _utc(2026, 3, 24, 14) in starts
    # The EXDATE is written in local time and must still cancel its occurrence.
    assert _utc(2026, 3, 17, 14) not in starts
    assert _utc(2026, 3, 17, 15) not in starts


def test_exdate_in_zulu_form_also_cancels():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:weekly-z\r\n"
        "SUMMARY:Weekly ops\r\n"
        "DTSTART:20260903T150000Z\r\n"
        "DTEND:20260903T153000Z\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
        "EXDATE:20260910T150000Z\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 9, 1), _utc(2026, 10, 1)
    )
    assert [inst.start for inst in instances] == [
        _utc(2026, 9, 3, 15), _utc(2026, 9, 17, 15),
    ]


def test_cancelled_override_removes_only_that_occurrence():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:weekly-cancel\r\n"
        "SUMMARY:Weekly ops\r\n"
        "DTSTART:20260903T150000Z\r\n"
        "DTEND:20260903T153000Z\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:weekly-cancel\r\n"
        "SUMMARY:Weekly ops\r\n"
        "STATUS:CANCELLED\r\n"
        "RECURRENCE-ID:20260910T150000Z\r\n"
        "DTSTART:20260910T150000Z\r\n"
        "DTEND:20260910T153000Z\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 9, 1), _utc(2026, 10, 1)
    )
    assert [inst.start for inst in instances] == [
        _utc(2026, 9, 3, 15), _utc(2026, 9, 17, 15),
    ]


def test_this_and_future_override_does_not_crash():
    """RANGE=THISANDFUTURE is treated as a single-instance override, not a crash."""
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:weekly-range\r\n"
        "SUMMARY:Weekly ops\r\n"
        "DTSTART:20260903T150000Z\r\n"
        "DTEND:20260903T153000Z\r\n"
        "RRULE:FREQ=WEEKLY;COUNT=3\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:weekly-range\r\n"
        "SUMMARY:Weekly ops (moved)\r\n"
        "RECURRENCE-ID;RANGE=THISANDFUTURE:20260910T150000Z\r\n"
        "DTSTART:20260910T160000Z\r\n"
        "DTEND:20260910T163000Z\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 9, 1), _utc(2026, 10, 1)
    )
    assert [inst.start for inst in instances] == [
        _utc(2026, 9, 3, 15), _utc(2026, 9, 10, 16), _utc(2026, 9, 17, 15),
    ]


def test_naive_until_is_read_in_the_events_zone():
    """UNTIL 17:00 Central is 22:00Z; reading it as UTC would drop 03-17."""
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:until-local\r\n"
        "SUMMARY:Bounded local series\r\n"
        "DTSTART;TZID=Central Standard Time:20260303T170000\r\n"
        "DTEND;TZID=Central Standard Time:20260303T173000\r\n"
        "RRULE:FREQ=WEEKLY;UNTIL=20260317T170000\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 3, 1), _utc(2026, 4, 1)
    )
    assert [inst.start for inst in instances] == [
        _utc(2026, 3, 3, 23),    # CST
        _utc(2026, 3, 10, 22),   # CDT
        _utc(2026, 3, 17, 22),
    ]


# ── Zones beyond the US ──────────────────────────────────────────────────────

def test_non_us_windows_zone_is_mapped():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:tokyo\r\n"
        "SUMMARY:Tokyo sync\r\n"
        "DTSTART;TZID=Tokyo Standard Time:20260903T090000\r\n"
        "DTEND;TZID=Tokyo Standard Time:20260903T093000\r\n"
        "END:VEVENT\r\n"
    )
    notes = []
    event = calendar_feed.parse_ics(text, notes=notes)[0]
    assert event.start == _utc(2026, 9, 3, 0)     # JST is UTC+9, no DST
    assert "TZID Tokyo Standard Time mapped to Asia/Tokyo" in notes
    assert event.tz_note == ""


def test_utc_offset_style_tzid():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:offset\r\n"
        "SUMMARY:Offset meeting\r\n"
        "DTSTART;TZID=(UTC-05) Bogota:20260903T090000\r\n"
        "DTEND;TZID=(UTC-05) Bogota:20260903T093000\r\n"
        "END:VEVENT\r\n"
    )
    event = calendar_feed.parse_ics(text)[0]
    assert event.start == _utc(2026, 9, 3, 14)


def test_unknown_zone_records_a_note_on_the_event():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:unknown-zone\r\n"
        "SUMMARY:Mystery zone\r\n"
        "DTSTART;TZID=Middle Earth Standard Time:20260903T090000\r\n"
        "DTEND;TZID=Middle Earth Standard Time:20260903T093000\r\n"
        "END:VEVENT\r\n"
    )
    notes = []
    event = calendar_feed.parse_ics(text, notes=notes)[0]
    assert event.start == _utc(2026, 9, 3, 14)   # fell back to Central
    assert "unknown timezone" in event.tz_note
    assert "America/Chicago" in event.tz_note
    assert any("is unknown" in note for note in notes)


# ── Parsing oddities Outlook actually emits ──────────────────────────────────

def test_quoted_cn_with_commas_and_tab_folding():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:quoted-cn\r\n"
        "SUMMARY:Board review\r\n"
        "DTSTART:20260903T150000Z\r\n"
        "DTEND:20260903T160000Z\r\n"
        'ATTENDEE;CN="Rivera, Sam A., CPCU";PARTSTAT=ACCEPTED:mailto:sam@example.com\r\n'
        'ATTENDEE;CN="Blake, Jordan":mailto:jordan@example.com\r\n'
        "DESCRIPTION:First half of a description that Outlook wrapped\r\n"
        "\tand continued after a tab\r\n"
        "END:VEVENT\r\n"
    )
    event = calendar_feed.parse_ics(text)[0]
    names = [person["name"] for person in event.attendees]
    assert names == ["Rivera, Sam A., CPCU", "Blake, Jordan"]
    assert event.attendees[0]["email"] == "sam@example.com"
    assert event.description == (
        "First half of a description that Outlook wrappedand continued after a tab"
    )
    assert calendar_feed.expected_count(event) == 2


# ── Matching: a long block must not outrank a real meeting ───────────────────

def test_long_block_containing_the_recording_is_capped():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:focus\r\n"
        "SUMMARY:Focus time\r\n"
        "DTSTART:20260903T140000Z\r\n"
        "DTEND:20260903T220000Z\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:real-meeting\r\n"
        "SUMMARY:Renewal strategy\r\n"
        "DTSTART:20260903T190000Z\r\n"
        "DTEND:20260903T192000Z\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 9, 1), _utc(2026, 9, 10)
    )
    result = calendar_feed.match_session(
        instances, _utc(2026, 9, 3, 19), _utc(2026, 9, 3, 19, 20)
    )
    assert result["best"]["instance"].uid == "real-meeting"
    assert result["best"]["score"] >= 0.8
    block = next(
        alt for alt in result["alternatives"] if alt["instance"].uid == "focus"
    )
    assert block["score"] <= 0.75

    # With no meeting to compete against, an 8-hour block stays below the score
    # calendar_sync will store, so its attendee count never becomes a ceiling.
    only_block = [inst for inst in instances if inst.uid == "focus"]
    alone = calendar_feed.match_session(
        only_block, _utc(2026, 9, 3, 19), _utc(2026, 9, 3, 19, 20)
    )
    assert alone["best"]["score"] == calendar_feed.BLOCK_SCORE_CAP
    assert alone["best"]["score"] < 0.5


# ── Title normalization must not eat real words ──────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Mayfield 12 Oaks renewal", "mayfield 12 oaks renewal"),
        ("March 3 Group planning", "march 3 group planning"),
        ("24/7 Support review", "24 7 support review"),
        ("Sprint #14 review", "sprint 14 review"),
    ],
)
def test_normalize_title_keeps_mid_subject_numbers(raw, expected):
    assert calendar_feed.normalize_title(raw) == expected


def test_normalize_title_collisions_stay_distinct():
    keys = {
        calendar_feed.normalize_title(title)
        for title in (
            "Mayfield 12 Oaks renewal",
            "Oaks renewal",
            "March 3 Group planning",
            "Group planning",
            "24/7 Support review",
            "Support review",
            "Sprint #14 review",
            "Review",
        )
    }
    assert len(keys) == 8


# ── Cache hygiene ────────────────────────────────────────────────────────────

def test_cached_instances_skips_junk_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    calendar_feed.save_cache({"instances": [
        {"uid": "good", "start": "2026-09-03T15:00:00+00:00"},
        "not a dict",
        None,
        42,
    ]})
    restored = calendar_feed.cached_instances()
    assert [inst.uid for inst in restored] == ["good"]

    calendar_feed.save_cache({"instances": "not a list"})
    assert calendar_feed.cached_instances() == []


def test_save_cache_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    calendar_feed.save_cache({"fetched_at": "one"})
    calendar_feed.save_cache({"fetched_at": "two", "event_count": 3})

    assert calendar_feed.load_cache() == {"fetched_at": "two", "event_count": 3}
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


def test_save_cache_is_serialized_across_threads(tmp_path, monkeypatch):
    """Concurrent writers must not lose each other's keys or collide on a temp file."""
    import threading

    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    calendar_feed.save_cache({"instances": []})
    errors = []

    def writer(index):
        try:
            for _ in range(10):
                calendar_feed.save_cache({f"key{index}": index})
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    cache = calendar_feed.load_cache()
    assert [cache.get(f"key{i}") for i in range(4)] == [0, 1, 2, 3]
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


# ── Round 2: offset-prefixed TZIDs ───────────────────────────────────────────

CENTRAL_TZID = "(UTC-06:00) Central Time (US & Canada)"

CENTRAL_VTIMEZONE = (
    "BEGIN:VTIMEZONE\r\n"
    f"TZID:{CENTRAL_TZID}\r\n"
    "BEGIN:STANDARD\r\n"
    "DTSTART:16011104T020000\r\n"
    "RRULE:FREQ=YEARLY;BYDAY=1SU;BYMONTH=11\r\n"
    "TZOFFSETFROM:-0500\r\n"
    "TZOFFSETTO:-0600\r\n"
    "END:STANDARD\r\n"
    "BEGIN:DAYLIGHT\r\n"
    "DTSTART:16010311T020000\r\n"
    "RRULE:FREQ=YEARLY;BYDAY=2SU;BYMONTH=3\r\n"
    "TZOFFSETFROM:-0600\r\n"
    "TZOFFSETTO:-0500\r\n"
    "END:DAYLIGHT\r\n"
    "END:VTIMEZONE\r\n"
)

CENTRAL_EVENT = (
    "BEGIN:VEVENT\r\n"
    "UID:summer-central\r\n"
    "SUMMARY:Summer standup\r\n"
    f'DTSTART;TZID="{CENTRAL_TZID}":20260715T090000\r\n'
    f'DTEND;TZID="{CENTRAL_TZID}":20260715T093000\r\n'
    "END:VEVENT\r\n"
)


def test_offset_prefixed_tzid_uses_the_feeds_vtimezone():
    """The prefix is decoration. Flattening it to a fixed offset loses DST.

    July is CDT (UTC-5), so 09:00 local is 14:00Z. Reading "(UTC-06:00)" as a
    fixed offset would give 15:00Z and match the wrong meeting.
    """
    notes = []
    event = calendar_feed.parse_ics(_wrap(CENTRAL_VTIMEZONE + CENTRAL_EVENT), notes=notes)[0]
    assert event.start == _utc(2026, 7, 15, 14)
    assert event.tz_note == ""
    assert any("VTIMEZONE" in note for note in notes)


def test_offset_prefixed_tzid_falls_back_to_the_name_without_a_vtimezone():
    notes = []
    event = calendar_feed.parse_ics(_wrap(CENTRAL_EVENT), notes=notes)[0]
    assert event.start == _utc(2026, 7, 15, 14)
    assert event.tz_note == ""
    assert f"TZID {CENTRAL_TZID} mapped to America/Chicago" in notes


def test_unquoted_offset_prefixed_tzid_still_yields_the_event():
    """Outlook writes this parameter unquoted; the colon inside must not split it."""
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:unquoted-tzid\r\n"
        "SUMMARY:Unquoted zone\r\n"
        f"DTSTART;TZID={CENTRAL_TZID}:20260715T090000\r\n"
        f"DTEND;TZID={CENTRAL_TZID}:20260715T093000\r\n"
        "END:VEVENT\r\n"
    )
    events = calendar_feed.parse_ics(text)
    assert len(events) == 1
    assert events[0].summary == "Unquoted zone"
    assert events[0].start == _utc(2026, 7, 15, 14)


@pytest.mark.parametrize(
    ("tzid", "expected_hour"),
    [
        ("(UTC-05) Bogota", 14),   # no such IANA name: the offset is all there is
        ("UTC-05", 14),            # offset-only name
        ("(UTC+09:00) Osaka, Sapporo, Tokyo", 0),
    ],
)
def test_offset_only_and_prefixed_names_resolve(tzid, expected_hour):
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:offset\r\n"
        "SUMMARY:Offset meeting\r\n"
        f'DTSTART;TZID="{tzid}":20260715T090000\r\n'
        f'DTEND;TZID="{tzid}":20260715T093000\r\n'
        "END:VEVENT\r\n"
    )
    event = calendar_feed.parse_ics(text)[0]
    assert event.start == _utc(2026, 7, 15, expected_hour)


def test_utc_offset_zone_only_matches_an_offset_only_name():
    assert calendar_feed._utc_offset_zone("UTC-05") == "Etc/GMT+5"
    assert calendar_feed._utc_offset_zone("(UTC+03:00)") == "Etc/GMT-3"
    assert calendar_feed._utc_offset_zone(CENTRAL_TZID) is None
    assert calendar_feed._utc_offset_zone("(UTC+05:30)") is None   # no Etc zone
    assert calendar_feed._utc_offset_zone("Central Standard Time") is None


# ── Round 2: multi-hour blocks ───────────────────────────────────────────────

def test_multi_hour_block_scores_below_the_storable_threshold():
    """An 8-hour "Focus time" contains recordings; it does not describe them."""
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:focus-day\r\n"
        "SUMMARY:Focus time\r\n"
        "DTSTART:20260903T140000Z\r\n"
        "DTEND:20260903T220000Z\r\n"
        "ATTENDEE;CN=Alex Chen:mailto:achen@example.com\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 9, 1), _utc(2026, 9, 10)
    )

    # Starting with the block does not rescue it: a 30-minute recording covers
    # almost none of it.
    aligned = calendar_feed.match_session(
        instances, _utc(2026, 9, 3, 14), _utc(2026, 9, 3, 14, 30)
    )
    assert aligned["best"]["score"] <= calendar_feed.BLOCK_SCORE_CAP
    assert aligned["best"]["score"] < 0.5


def test_a_recording_that_fills_a_long_block_still_matches_it():
    text = _wrap(
        "BEGIN:VEVENT\r\n"
        "UID:workshop\r\n"
        "SUMMARY:All-day workshop\r\n"
        "DTSTART:20260903T140000Z\r\n"
        "DTEND:20260903T220000Z\r\n"
        "END:VEVENT\r\n"
    )
    instances = calendar_feed.expand(
        calendar_feed.parse_ics(text), _utc(2026, 9, 1), _utc(2026, 9, 10)
    )
    result = calendar_feed.match_session(
        instances, _utc(2026, 9, 3, 14), _utc(2026, 9, 3, 21)
    )
    assert result["best"]["score"] >= 0.8


def test_ghost_titles_never_match():
    from datetime import datetime, timezone
    from core import calendar_feed as cf
    ics = "\n".join([
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT", "UID:g1", "SUMMARY:Canceled: PMO Huddle",
        "DTSTART:20260828T163000Z", "DTEND:20260828T170000Z", "END:VEVENT",
        "BEGIN:VEVENT", "UID:g2", "SUMMARY:RESCHEDULE: Claire Welcome Back",
        "DTSTART:20260828T163000Z", "DTEND:20260828T170000Z", "END:VEVENT",
        "BEGIN:VEVENT", "UID:r1", "SUMMARY:AI Questions",
        "DTSTART:20260828T163000Z", "DTEND:20260828T170000Z", "END:VEVENT",
        "END:VCALENDAR",
    ])
    events = cf.parse_ics(ics)
    instances = cf.expand(events, datetime(2026, 8, 1, tzinfo=timezone.utc),
                          datetime(2026, 9, 30, tzinfo=timezone.utc))
    res = cf.match_session(instances, "2026-08-28T16:30:00", "2026-08-28T17:00:00")
    assert res["best"] is not None
    assert res["best"]["instance"].summary == "AI Questions"
    assert all(not cf.is_ghost_title(a["instance"].summary) for a in res["alternatives"])
    assert cf.is_ghost_title("Cancelled: x") and cf.is_ghost_title("Rescheduled: y")
    assert not cf.is_ghost_title("Weekly PMO")


def test_a_recording_that_runs_long_still_matches_its_short_meeting():
    from datetime import datetime, timezone
    from core import calendar_feed as cf
    ics = "\n".join([
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT", "UID:m1", "SUMMARY:Intro call",
        "DTSTART:20260828T163000Z", "DTEND:20260828T170000Z", "END:VEVENT",
        "END:VCALENDAR",
    ])
    events = cf.parse_ics(ics)
    instances = cf.expand(events, datetime(2026, 8, 1, tzinfo=timezone.utc),
                          datetime(2026, 9, 30, tzinfo=timezone.utc))
    # An 85-minute recording of a 30-minute slot, starting together.
    res = cf.match_session(instances, "2026-08-28T16:30:00", "2026-08-28T17:55:00")
    assert res["best"] is not None and res["best"]["score"] >= 0.8
