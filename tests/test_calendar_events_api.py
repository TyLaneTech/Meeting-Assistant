"""Tests for the cached and redacted Calendar events API."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from flask import Flask

from core import (calendar_events_api, calendar_feed, meeting_links, paths,
                  settings, storage)

UTC = timezone.utc
FROZEN_NOW = datetime(2026, 9, 3, 20, 0, tzinfo=UTC)
FEED_URL = "https://outlook.office365.com/private/calendar.ics"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(calendar_events_api, "_utcnow", lambda: FROZEN_NOW)
    monkeypatch.setattr(calendar_feed, "cached_instances", lambda: [])
    storage.init_db()
    settings.update({
        "calendar_enabled": True,
        "calendar_ics_url": FEED_URL,
        "calendar_timezone": "America/Chicago",
        "calendar_last_refresh": "2026-09-03T18:00:00+00:00",
        "calendar_last_error": "",
    })
    app = Flask(__name__)
    app.register_blueprint(calendar_events_api.bp)
    return app.test_client()


def _instance(
    uid: str,
    title: str,
    start: str,
    end: str,
    **kwargs,
) -> calendar_feed.Instance:
    return calendar_feed.Instance(
        uid=uid,
        summary=title,
        start=calendar_feed.parse_iso_utc(start),
        end=calendar_feed.parse_iso_utc(end),
        **kwargs,
    )


@pytest.mark.parametrize(
    "query,error_part",
    [
        ("?end=2026-09-30", "start is required"),
        ("?start=2026-09-01", "end is required"),
        ("?start=09-01-2026&end=2026-09-30", "start must use"),
        ("?start=2026-09-03&end=2026-09-02", "end must be on or after"),
        ("?start=2026-09-01&end=2026-11-02", "cannot exceed 62 days"),
        ("?start=2026-09-01&end=2026-09-30&tz=Not/AZone", "valid IANA"),
    ],
)
def test_range_validation(client, query, error_part):
    response = client.get("/api/calendar/events" + query)
    assert response.status_code == 400
    assert error_part in response.get_json()["error"]


def test_sixty_two_day_range_is_allowed(client):
    response = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-11-01"
    )
    assert response.status_code == 200


def test_redaction_filters_and_private_title(client, monkeypatch):
    fixtures = [
        _instance(
            "private-uid@example.com",
            "Secret title",
            "2026-09-03T02:30:00Z",
            "2026-09-03T03:00:00Z",
            description="Join https://secret.example and mail person@example.com",
            location="https://room.example",
            organizer={"email": "boss@example.com"},
            attendees=[{"email": "guest@example.com"}],
            is_private=True,
            tz_note="guessed from http://notes.example",
        ),
        _instance(
            "ghost",
            "Cancelled: old meeting",
            "2026-09-03T15:00:00Z",
            "2026-09-03T16:00:00Z",
        ),
        _instance(
            "cancelled",
            "Actually cancelled",
            "2026-09-03T17:00:00Z",
            "2026-09-03T18:00:00Z",
            status="CANCELLED",
        ),
    ]
    monkeypatch.setattr(calendar_feed, "cached_instances", lambda: fixtures)

    response = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-09-30"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert len(payload["events"]) == 1
    event = payload["events"][0]
    assert event["title"] == "Private appointment"
    assert event["private"] is True
    assert event["day"] == "2026-09-02"
    assert set(event) == {
        "key", "title", "start", "end", "all_day", "private", "status",
        "day", "session_id", "session_title", "state", "join", "join_label",
    }
    serialized = json.dumps(payload).lower()
    for forbidden in (
        "attendees", "organizer", "description", "location", "tz_note",
        "@", "http", FEED_URL.lower(),
    ):
        assert forbidden not in serialized

    included = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-09-30&include_cancelled=1"
    ).get_json()["events"]
    assert [item["status"] for item in included] == ["confirmed", "cancelled"]


TEAMS_JOIN = (
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc@thread.v2/"
    "0?context=%7b%22Tid%22%3a%22aaa%22%7d"
)


def test_a_joinable_meeting_exposes_its_provider_and_never_its_url(client, monkeypatch):
    """The provider slug is presentation; the URL is a credential. Anyone
    holding it can walk into the meeting, so it stays server-side and
    POST /api/calendar/join resolves it out of the cache."""
    upcoming = _instance(
        "joinable", "Renewal strategy",
        "2026-09-03T21:00:00Z", "2026-09-03T22:00:00Z",
        join_url=TEAMS_JOIN, location="Microsoft Teams Meeting",
    )
    private = _instance(
        "hidden", "Oncology results",
        "2026-09-03T22:30:00Z", "2026-09-03T23:00:00Z",
        join_url=TEAMS_JOIN, is_private=True,
    )
    # Ended over the grace period ago: joining it is meaningless, so it gets
    # no provider at all and its row cannot open anything.
    stale = _instance(
        "stale", "This morning", "2026-09-03T12:00:00Z", "2026-09-03T13:00:00Z",
        join_url=TEAMS_JOIN,
    )
    monkeypatch.setattr(
        calendar_feed, "cached_instances", lambda: [upcoming, private, stale]
    )

    payload = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-09-30"
    ).get_json()
    by_title = {item["title"]: item for item in payload["events"]}

    assert by_title["Renewal strategy"]["join"] == "teams"
    assert by_title["Renewal strategy"]["join_label"] == "Teams"
    # A private appointment is still the owner's own meeting, so Join works;
    # only its subject is redacted, exactly as before.
    assert by_title["Private appointment"]["join"] == "teams"
    assert by_title["This morning"]["join"] == ""
    assert by_title["This morning"]["join_label"] == ""
    assert "teams.microsoft.com" not in json.dumps(payload)


def test_an_event_key_resolves_back_to_its_occurrence(client, monkeypatch):
    """What the join route stands on: the key the client already holds finds
    the cached occurrence again, and a stale or forged one finds nothing."""
    instance = _instance(
        "joinable", "Renewal strategy",
        "2026-09-03T21:00:00Z", "2026-09-03T22:00:00Z", join_url=TEAMS_JOIN,
    )
    monkeypatch.setattr(calendar_feed, "cached_instances", lambda: [instance])

    key = calendar_events_api.event_key(instance)
    found = calendar_events_api.find_instance(key)
    assert found is not None
    assert meeting_links.find_link(found)["url"] == TEAMS_JOIN
    for miss in ("", None, "0" * 24, "not-a-key"):
        assert calendar_events_api.find_instance(miss) is None


def test_an_all_day_block_is_never_joinable(client, monkeypatch):
    block = calendar_feed.Instance(
        uid="ooo", summary="Remote",
        start=calendar_feed.parse_iso_utc("2026-09-04T05:00:00Z"),
        end=calendar_feed.parse_iso_utc("2026-09-05T05:00:00Z"),
        all_day=True, join_url=TEAMS_JOIN,
    )
    monkeypatch.setattr(calendar_feed, "cached_instances", lambda: [block])
    payload = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-09-30"
    ).get_json()
    assert payload["events"][0]["join"] == ""


def test_recorded_recording_missed_and_upcoming_states(client, monkeypatch):
    recorded_event = _instance(
        "recorded", "Recorded event", "2026-09-03T15:00:00Z", "2026-09-03T16:00:00Z"
    )
    live_event = _instance(
        "live", "Live event", "2026-09-03T17:00:00Z", "2026-09-03T18:00:00Z"
    )
    missed_event = _instance(
        "missed", "Missed event", "2026-09-03T12:00:00Z", "2026-09-03T13:00:00Z"
    )
    upcoming_event = _instance(
        "upcoming", "Upcoming event", "2026-09-04T15:00:00Z", "2026-09-04T16:00:00Z"
    )
    monkeypatch.setattr(
        calendar_feed,
        "cached_instances",
        lambda: [upcoming_event, live_event, missed_event, recorded_event],
    )
    recorded_id = storage.create_session(
        "Recorded session",
        started_at="2026-09-03T15:05:00",
        ended_at="2026-09-03T15:10:00",
    )
    storage.set_calendar_match(recorded_id, {"uid": "recorded"})
    live_id = storage.create_session(
        "Live session", started_at="2026-09-03T17:00:00"
    )
    storage.save_segment(live_id, "still live", "speaker_1", 0, 3600)

    payload = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-09-30"
        f"&live_session_id={live_id}"
    ).get_json()

    assert [item["start"] for item in payload["events"]] == sorted(
        item["start"] for item in payload["events"]
    )
    by_title = {item["title"]: item for item in payload["events"]}
    assert by_title["Recorded event"]["state"] == "recorded"
    assert by_title["Recorded event"]["session_id"] == recorded_id
    assert by_title["Recorded event"]["session_title"] == "Recorded session"
    assert by_title["Live event"]["state"] == "recording"
    assert by_title["Live event"]["session_id"] == live_id
    assert by_title["Missed event"]["state"] == "missed"
    assert by_title["Missed event"]["session_id"] is None
    assert by_title["Upcoming event"]["state"] == "upcoming"


def test_overlap_fallback_uses_transcript_duration(client, monkeypatch):
    event = _instance(
        "fallback", "Fallback", "2026-09-03T15:00:00Z", "2026-09-03T16:00:00Z"
    )
    monkeypatch.setattr(calendar_feed, "cached_instances", lambda: [event])
    session_id = storage.create_session(
        "Transcript wins",
        started_at="2026-09-03T15:00:00",
        ended_at="2026-09-04T15:00:00",
    )
    storage.save_segment(session_id, "enough overlap", "speaker_1", 0, 1800)

    event_payload = client.get(
        "/api/calendar/events?start=2026-09-03&end=2026-09-03"
    ).get_json()["events"][0]

    assert event_payload["state"] == "recorded"
    assert event_payload["session_id"] == session_id


def test_enabled_false_returns_empty_events(client, monkeypatch):
    monkeypatch.setattr(
        calendar_feed,
        "cached_instances",
        lambda: [_instance(
            "hidden", "Hidden", "2026-09-03T15:00:00Z", "2026-09-03T16:00:00Z"
        )],
    )
    settings.update({"calendar_enabled": False})

    payload = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-09-30"
    ).get_json()

    assert payload == {
        "enabled": False,
        "timezone": "America/Chicago",
        "start": "2026-09-01",
        "end": "2026-09-30",
        "last_refresh": "2026-09-03T18:00:00+00:00",
        "last_error": "",
        "events": [],
    }


def test_all_day_items_never_match_by_overlap_and_are_never_missed(client, monkeypatch):
    past_block = _instance(
        "remote-past", "Remote", "2026-09-01T05:00:00Z", "2026-09-02T05:00:00Z",
        all_day=True,
    )
    future_block = _instance(
        "remote-future", "Remote", "2026-09-08T05:00:00Z", "2026-09-09T05:00:00Z",
        all_day=True,
    )
    monkeypatch.setattr(
        calendar_feed, "cached_instances", lambda: [past_block, future_block]
    )
    session_id = storage.create_session(
        "Call that day", started_at="2026-09-01T18:00:00", ended_at="2026-09-01T18:30:00"
    )
    storage.save_segment(session_id, "hello", "speaker_1", 0, 1200)

    payload = client.get(
        "/api/calendar/events?start=2026-09-01&end=2026-09-10"
    ).get_json()
    by_key = {item["title"] + item["day"]: item for item in payload["events"]}

    past = by_key["Remote2026-09-01"]
    assert past["all_day"] is True
    assert past["session_id"] is None
    assert past["state"] == "past"
    future = by_key["Remote2026-09-08"]
    assert future["state"] == "upcoming"
