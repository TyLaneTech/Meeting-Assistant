"""Tests for join-link parsing and the opt-in calendar naming of a recording.

Offline and side-effect free: nothing here fetches a feed and nothing calls the
OS launcher. The bodies below are the shapes Outlook and Zoom actually put in a
published ICS, help links and all, because rejecting those is most of the job.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core import calendar_feed, calendar_sync, meeting_links, paths, settings

UTC = timezone.utc
FEED_URL = "https://outlook.office365.com/owa/calendar/guid@example.com/guid/calendar.ics"

TEAMS_JOIN = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_NzRjZTk2@thread.v2/"
    "0?context=%7b%22Tid%22%3a%22aaa%22%2c%22Oid%22%3a%22bbb%22%7d"
)

# The whole invite footer, so the help and "Meeting options" links are in play.
TEAMS_BODY = (
    "Microsoft Teams meeting\n"
    "Join on your computer, mobile app or room device\n"
    f"Click here to join the meeting<{TEAMS_JOIN}>\n"
    "Meeting ID: 274 552 331 999\n"
    "Download Teams<https://www.microsoft.com/en-us/microsoft-teams/download-app>\n"
    "Learn more<https://aka.ms/JoinTeamsMeeting> | "
    "Meeting options<https://teams.microsoft.com/meetingOptions/?organizerId=x>\n"
)

ZOOM_JOIN = "https://us02web.zoom.us/j/84123456789?pwd=Qk9UWmxsZDR3"

ZOOM_BODY = (
    "Ty Lane is inviting you to a scheduled Zoom meeting.\n\n"
    "Join Zoom Meeting\n"
    f"{ZOOM_JOIN}\n\n"
    "Meeting ID: 841 2345 6789\n"
    "One tap mobile\n"
    "+13462487799,,84123456789#,,,,*553311# US (Houston)\n"
    "Find your local number: https://us02web.zoom.us/u/kbWmR8Kkc\n"
)


def _instance(**kwargs) -> calendar_feed.Instance:
    return calendar_feed.Instance(uid="uid", summary="Meeting", **kwargs)


# ── Finding the link ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("instance,provider,url", [
    # The Exchange property wins outright: it is the link, not a guess.
    (_instance(join_url=TEAMS_JOIN, location="Microsoft Teams Meeting",
               description=ZOOM_BODY), "teams", TEAMS_JOIN),
    (_instance(description=TEAMS_BODY), "teams", TEAMS_JOIN),
    (_instance(location=ZOOM_JOIN), "zoom", ZOOM_JOIN),
    (_instance(description=ZOOM_BODY), "zoom", ZOOM_JOIN),
    (_instance(description="https://teams.live.com/meet/9351234567890?p=AbCdEf"),
     "teams", "https://teams.live.com/meet/9351234567890?p=AbCdEf"),
    (_instance(description="Join at https://zoom.us/my/tylane."),
     "zoom", "https://zoom.us/my/tylane"),
    (_instance(description="Join: https://meet.google.com/abc-defg-hij (US)"),
     "meet", "https://meet.google.com/abc-defg-hij"),
    (_instance(description="https://acme.webex.com/acme/j.php?MTID=m1a2b3c4d5"),
     "webex", "https://acme.webex.com/acme/j.php?MTID=m1a2b3c4d5"),
])
def test_a_join_link_is_found_and_trimmed(instance, provider, url):
    link = meeting_links.find_link(instance)
    assert link is not None
    assert link["provider"] == provider
    assert link["url"] == url


@pytest.mark.parametrize("instance", [
    _instance(),
    _instance(location="Room 4", description="Bring the deck."),
    # Same hosts, none of them the meeting. Matching on the host alone would
    # open a download page or the organizer's Meeting options.
    _instance(description="Learn more<https://aka.ms/JoinTeamsMeeting> and "
                          "Meeting options<https://teams.microsoft.com/meetingOptions/?x=1>"),
    _instance(description="Download it from https://zoom.us/download first"),
    _instance(description="Docs at https://meet.google.com/lookup/abcdefg"),
    _instance(description="Notes: https://example.com/deck"),
])
def test_events_with_no_join_link_report_none(instance):
    assert meeting_links.find_link(instance) is None
    assert meeting_links.provider_for(instance) == ""


def test_the_x_property_is_parsed_off_the_feed():
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:teams-event\r\n"
        "SUMMARY:Renewal strategy\r\n"
        "DTSTART:20260903T151000Z\r\n"
        "DTEND:20260903T161000Z\r\n"
        "LOCATION:Microsoft Teams Meeting\r\n"
        f"X-MICROSOFT-SKYPETEAMSMEETINGURL:{TEAMS_JOIN}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    event = calendar_feed.parse_ics(ics)[0]
    assert event.join_url == TEAMS_JOIN
    # And it survives the cache round-trip the Calendar view reads from.
    instance = calendar_feed.Instance.from_dict(
        calendar_feed._instance_from_event(event, event.start, event.end).to_dict()
    )
    assert meeting_links.provider_for(instance) == "teams"


# ── The desktop client's own scheme ──────────────────────────────────────────

def test_teams_and_zoom_get_a_deep_link():
    teams = meeting_links.find_link(_instance(join_url=TEAMS_JOIN))
    assert teams["deep_link"].startswith("msteams:/l/meetup-join/")
    assert "//" not in teams["deep_link"][len("msteams:"):len("msteams:") + 2]

    zoom = meeting_links.find_link(_instance(location=ZOOM_JOIN))
    assert zoom["deep_link"] == (
        "zoommtg://us02web.zoom.us/join?confno=84123456789&pwd=Qk9UWmxsZDR3"
    )


@pytest.mark.parametrize("instance", [
    # Personal Teams, a Zoom vanity link, Meet and Webex all have no scheme
    # worth building, so Join uses https and their own pages hand off.
    _instance(description="https://teams.live.com/meet/9351234567890?p=AbCdEf"),
    _instance(description="https://zoom.us/my/tylane"),
    _instance(description="https://meet.google.com/abc-defg-hij"),
    _instance(description="https://acme.webex.com/acme/j.php?MTID=m1"),
])
def test_providers_without_a_scheme_stay_on_https(instance):
    link = meeting_links.find_link(instance)
    assert link["deep_link"] == ""
    assert link["url"].startswith("https://")


def test_open_link_prefers_the_app_then_falls_back(monkeypatch):
    tried = []

    def fake_launch(target):
        tried.append(target)
        return target.startswith("https://")

    monkeypatch.setattr(meeting_links, "_launch", fake_launch)
    link = meeting_links.find_link(_instance(location=ZOOM_JOIN))
    assert meeting_links.open_link(link) == {"ok": True, "opened": "browser"}
    assert tried == [link["deep_link"], link["url"]]


def test_open_link_reports_failure_when_nothing_handles_it(monkeypatch):
    monkeypatch.setattr(meeting_links, "_launch", lambda target: False)
    link = meeting_links.find_link(_instance(location=ZOOM_JOIN))
    assert meeting_links.open_link(link)["ok"] is False
    assert meeting_links.open_link({})["ok"] is False


# ── Naming a recording after its meeting ─────────────────────────────────────

TITLE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:renewal\r\n"
    "SUMMARY:Renewal strategy\r\n"
    "DTSTART:20260903T150000Z\r\n"
    "DTEND:20260903T160000Z\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:hidden\r\n"
    "SUMMARY:Oncology results\r\n"
    "CLASS:PRIVATE\r\n"
    "DTSTART:20260904T150000Z\r\n"
    "DTEND:20260904T160000Z\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:focus\r\n"
    "SUMMARY:Focus time\r\n"
    "DTSTART:20260905T130000Z\r\n"
    "DTEND:20260905T230000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture()
def cached_feed(tmp_path, monkeypatch):
    """The parsed fixture in the cache, with the feature switched on."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    settings.update({
        "calendar_enabled": True,
        "calendar_ics_url": FEED_URL,
        "calendar_title_from_event": True,
    })
    events = calendar_feed.parse_ics(TITLE_ICS)
    instances = calendar_feed.expand(
        events, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)
    )
    calendar_feed.save_cache({"instances": [i.to_dict() for i in instances]})
    return instances


def test_a_recording_inside_a_meeting_takes_its_name(cached_feed):
    assert calendar_sync.title_for_start("2026-09-03T15:00:30") == "Renewal strategy"
    # Started a few minutes early: still the same meeting.
    assert calendar_sync.title_for_start("2026-09-03T14:52:00") == "Renewal strategy"


@pytest.mark.parametrize("started,why", [
    ("2026-09-03T09:00:00", "nothing on the calendar then"),
    ("2026-09-04T15:00:30", "a private appointment keeps the default name"),
    ("2026-09-05T18:00:00", "a ten-hour block is a container, not a meeting"),
])
def test_no_confident_meeting_means_the_default_name(cached_feed, started, why):
    assert calendar_sync.title_for_start(started) == "", why


@pytest.mark.parametrize("prefs", [
    {"calendar_title_from_event": False},
    {"calendar_enabled": False},
    {"calendar_ics_url": ""},
])
def test_naming_is_opt_in_and_needs_a_live_calendar(cached_feed, prefs):
    settings.update(prefs)
    assert calendar_sync.title_for_start("2026-09-03T15:00:30") == ""


def test_a_broken_cache_never_blocks_a_start(cached_feed, monkeypatch):
    def boom():
        raise RuntimeError("cache is gone")

    monkeypatch.setattr(calendar_feed, "cached_instances", boom)
    assert calendar_sync.title_for_start("2026-09-03T15:00:30") == ""


def test_status_reports_the_naming_setting(cached_feed):
    assert calendar_sync.status()["title_from_event"] is True
    settings.update({"calendar_title_from_event": False})
    assert calendar_sync.status()["title_from_event"] is False
