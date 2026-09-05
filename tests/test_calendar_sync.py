"""Tests for calendar matching, the candidates merge, and the cleanup plan.

Offline: the feed loader is stubbed, so nothing here touches the network. Each
test gets its own data folder, database and settings file.
"""
import json
from datetime import datetime, timezone

import pytest

from core import calendar_feed, calendar_sync, paths, settings, storage

UTC = timezone.utc

FEED_URL = "https://outlook.office365.com/owa/calendar/guid@example.com/guid/calendar.ics"

MATCH_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
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
    "END:VCALENDAR\r\n"
)

STRIPPED_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:stripped\r\n"
    "SUMMARY:Renewal strategy (9/10)\r\n"
    "DTSTART:20260910T150000Z\r\n"
    "DTEND:20260910T160000Z\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


# Every fixture in this file is dated early September 2026. refresh() only
# considers recordings inside now-120d..now+45d, so a real clock would quietly
# expire this suite; the module's single clock is frozen instead.
FROZEN_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """An isolated data folder, an empty database, saved settings, frozen clock."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(calendar_sync, "_utcnow", lambda: FROZEN_NOW)
    storage.init_db()
    settings.update({
        "calendar_enabled": True,
        "calendar_ics_url": FEED_URL,
        "calendar_last_refresh": "",
        "calendar_last_error": "",
    })
    return tmp_path


def _stub_feed(monkeypatch, ics_text=MATCH_ICS):
    events = calendar_feed.parse_ics(ics_text)
    instances = calendar_feed.expand(
        events,
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(calendar_sync, "load_feed", lambda url: {
        "events": events,
        "instances": instances,
        "notes": [],
        "event_count": len(events),
        "instance_count": len(instances),
        "has_attendees_ratio": calendar_feed.attendee_coverage(instances),
    })
    return instances


def _session(title="Recording", started="2026-09-03T15:00:00", ended="2026-09-03T15:50:00"):
    return storage.create_session(title, started_at=started, ended_at=ended)


def _row(session_id):
    return {row["id"]: row for row in storage.list_sessions()}[session_id]


def test_refresh_matches_a_session_and_writes_candidates(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()

    summary = calendar_sync.refresh(force=True)

    assert summary["ok"] is True
    assert summary["matched"] == 1
    match = storage.get_calendar_match(session_id)
    assert match["uid"] == "overlapping"
    assert match["attendee_count"] == 2
    assert match["attendee_count_source"] == "calendar"
    assert match["confirmed"] is False
    assert match["score"] >= 0.5
    assert [alt["uid"] for alt in match["alternatives"]] == ["nearer-start"]

    row = _row(session_id)
    assert row["expected_speaker_count"] == 2
    assert row["expected_speaker_source"] == "calendar"

    payload = json.loads(
        (data_dir / "resolution_candidates" / f"{session_id}.json").read_text(encoding="utf-8")
    )
    assert payload["meeting"]["calendar_subject"] == "Renewal strategy"
    assert payload["meeting"]["calendar_uid"] == "overlapping"
    assert payload["meeting"]["attendee_count"] == 2
    assert [person["name"] for person in payload["candidates"]] == [
        "Jordan Blake", "Sam Rivera",
    ]


def test_refresh_skips_the_recording_session_and_confirmed_matches(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    live = _session("Live")
    pinned = _session("Pinned")
    storage.set_calendar_match(pinned, {
        "uid": "hand-picked",
        "title": "Chosen by hand",
        "attendee_count": 7,
        "confirmed": True,
    })

    calendar_sync.refresh(force=True, active_session_id=live)

    assert storage.get_calendar_match(live) is None
    assert storage.get_calendar_match(pinned)["uid"] == "hand-picked"


def test_refresh_clears_a_stale_machine_match(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session("Orphan", "2026-09-03T22:00:00", "2026-09-03T22:40:00")
    storage.set_calendar_match(session_id, {
        "uid": "gone",
        "title": "Deleted meeting",
        "attendee_count": 4,
        "confirmed": False,
    })
    assert _row(session_id)["expected_speaker_count"] == 4

    summary = calendar_sync.refresh(force=True)

    assert summary["cleared"] == 1
    assert storage.get_calendar_match(session_id) is None
    assert _row(session_id)["expected_speaker_count"] is None


def test_refresh_leaves_a_user_count_alone_when_clearing(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session("Orphan", "2026-09-03T22:00:00", "2026-09-03T22:40:00")
    storage.set_calendar_match(session_id, {"uid": "gone", "title": "Gone", "confirmed": False})
    storage.set_expected_speaker_count(session_id, 6, "user")

    calendar_sync.refresh(force=True)

    assert storage.get_calendar_match(session_id) is None
    assert _row(session_id)["expected_speaker_count"] == 6
    assert _row(session_id)["expected_speaker_source"] == "user"


def test_refresh_is_skipped_while_disabled(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    settings.update({"calendar_enabled": False})
    _session()

    summary = calendar_sync.refresh()

    assert summary["skipped"] == "disabled"
    assert summary["matched"] == 0


def test_refresh_reports_a_feed_error_without_raising(data_dir, monkeypatch):
    def boom(url):
        raise calendar_feed.CalendarFeedError("The calendar server returned HTTP 503.")

    monkeypatch.setattr(calendar_sync, "load_feed", boom)

    summary = calendar_sync.refresh(force=True)

    assert summary["ok"] is False
    assert "503" in summary["error"]
    assert settings.get("calendar_last_error") == summary["error"]


def test_merge_candidates_keeps_hand_fed_entries(data_dir, monkeypatch):
    instances = _stub_feed(monkeypatch)
    session_id = _session()
    path = data_dir / "resolution_candidates" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "meeting": {"title": "Hand-written title", "date": "2026-09-03"},
        "candidates": [{"name": "Hand Fed", "email": "hand@example.com", "source": "roster"}],
        "speaker_hints": [{"speaker_key": "speaker_1", "name": "Hand Fed"}],
    }), encoding="utf-8")

    instance = next(inst for inst in instances if inst.uid == "overlapping")
    match = calendar_sync.build_match({
        "instance": instance,
        "score": 0.9,
        "overlap_seconds": 2400,
        "start_delta_seconds": 600,
        "reason": "80% overlap",
    }, [])
    assert calendar_sync.merge_candidates(session_id, instance, match) is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["meeting"]["title"] == "Hand-written title"
    assert payload["meeting"]["date"] == "2026-09-03"
    assert payload["meeting"]["calendar_subject"] == "Renewal strategy"
    assert payload["speaker_hints"] == [{"speaker_key": "speaker_1", "name": "Hand Fed"}]
    names = [person["name"] for person in payload["candidates"]]
    assert names == ["Hand Fed", "Jordan Blake", "Sam Rivera"]

    # A second merge replaces only the calendar-sourced entries.
    calendar_sync.merge_candidates(session_id, instance, match)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [person["name"] for person in payload["candidates"]] == names


def test_remerge_candidates_uses_the_stored_match(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)
    path = data_dir / "resolution_candidates" / f"{session_id}.json"
    path.unlink()

    assert calendar_sync.remerge_candidates(session_id) is True
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [person["name"] for person in payload["candidates"]] == [
        "Jordan Blake", "Sam Rivera",
    ]


def test_confirm_and_clear_match(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)

    result = calendar_sync.confirm_match(session_id, "nearer-start")

    assert result["ok"] is True
    assert result["match"]["confirmed"] is True
    assert result["match"]["uid"] == "nearer-start"

    # A confirmed match survives the next refresh.
    calendar_sync.refresh(force=True)
    assert storage.get_calendar_match(session_id)["uid"] == "nearer-start"

    cleared = calendar_sync.clear_match(session_id)
    assert cleared["ok"] is True
    assert calendar_sync.get_match(session_id)["match"] is None
    assert calendar_sync.get_match(session_id)["cleared"] is True


def test_confirm_match_rejects_an_unknown_event(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)

    result = calendar_sync.confirm_match(session_id, "never-heard-of-it")

    assert result["ok"] is False
    assert "no longer in the feed" in result["error"]


def test_confirm_match_remembers_the_count_for_the_title(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)

    calendar_sync.confirm_match(session_id, "overlapping")

    row = _row(session_id)
    assert row["expected_speaker_count"] == 2
    # Confirming the MEETING is not the same as typing a count: the source stays
    # "calendar" so clearing the match can still clear the ceiling it set.
    assert row["expected_speaker_source"] == "calendar"
    assert calendar_feed.recall_expected_count("renewal strategy") == 2


def test_title_memory_supplies_a_count_when_attendees_are_missing(data_dir, monkeypatch):
    _stub_feed(monkeypatch, STRIPPED_ICS)
    calendar_feed.remember_expected_count("renewal strategy", 3)
    session_id = _session("Recording", "2026-09-10T15:00:00", "2026-09-10T15:50:00")

    calendar_sync.refresh(force=True)

    match = storage.get_calendar_match(session_id)
    assert match["attendee_count"] == 3
    assert match["attendee_count_source"] == "memory"
    assert _row(session_id)["expected_speaker_count"] == 3


def test_build_plan_actions(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    # One material speaker, still generically named, against a calendar that
    # expects two people.
    storage.save_speaker_label(session_id, "speaker_1", name="Speaker 1")
    storage.save_segment(session_id, "one two three four five", "speaker_1", 0, 40)
    calendar_sync.refresh(force=True)

    plan = calendar_sync.build_plan(session_id)
    assert plan["action"] == "reanalyze"
    assert plan["expected"] == 2
    assert plan["found"] == 1
    assert plan["max_speakers"] == 2
    assert plan["library_matches_expected"] == "unknown"
    assert plan["calendar"]["title"] == "Renewal strategy"
    assert [person["name"] for person in plan["candidates"]] == [
        "Jordan Blake", "Sam Rivera",
    ]

    # Name the speaker and set the count to what was found: nothing to do.
    storage.save_speaker_label(session_id, "speaker_1", name="Jordan Blake")
    storage.set_expected_speaker_count(session_id, 1, "user")
    assert calendar_sync.build_plan(session_id)["action"] == "none"

    # Make it generic again: the count matches but the speaker is unnamed.
    storage.save_speaker_label(session_id, "speaker_1", name="Speaker 1")
    plan = calendar_sync.build_plan(session_id)
    assert plan["action"] == "resolve_only"
    assert plan["unresolved"] == 1
    assert plan["max_speakers"] == 1


def test_build_plan_rejects_an_unknown_session(data_dir):
    assert "error" in calendar_sync.build_plan("does-not-exist")


def test_status_masks_the_url(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    _session()
    calendar_sync.refresh(force=True)

    status = calendar_sync.status()

    assert status["enabled"] is True
    assert status["has_url"] is True
    assert status["url_masked"] == calendar_feed.mask_url(FEED_URL)
    assert FEED_URL not in json.dumps(status)
    assert status["instance_count"] == 3
    assert status["matched_sessions"] == 1
    assert status["last_error"] == ""
    assert status["next_refresh_due"]


def test_refresh_due_follows_the_interval(data_dir):
    settings.update({"calendar_last_refresh": "", "calendar_refresh_minutes": 60})
    assert calendar_sync.refresh_due() is True

    settings.update({
        "calendar_last_refresh": FROZEN_NOW.replace(microsecond=0).isoformat(),
    })
    assert calendar_sync.refresh_due() is False


# ── Fixes from review round 1 ────────────────────────────────────────────────

RECURRING_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:weekly-1\r\n"
    "SUMMARY:Weekly ops\r\n"
    "DTSTART:20260805T150000Z\r\n"
    "DTEND:20260805T153000Z\r\n"
    "RRULE:FREQ=WEEKLY;COUNT=8\r\n"
    "ATTENDEE;CN=Jordan Blake:mailto:jordan@example.com\r\n"
    "ATTENDEE;CN=Sam Rivera:mailto:sam@example.com\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)

UNKNOWN_ZONE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:mystery\r\n"
    "SUMMARY:Mystery zone meeting\r\n"
    "DTSTART;TZID=Middle Earth Standard Time:20260903T100000\r\n"
    "DTEND;TZID=Middle Earth Standard Time:20260903T110000\r\n"
    "ATTENDEE;CN=Jordan Blake:mailto:jordan@example.com\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def test_refresh_leaves_recordings_outside_the_expansion_window_alone(data_dir, monkeypatch):
    """An old recording has no candidate instances, which is not "deleted".

    The expansion covers now-120d to now+45d. A recording from two years ago can
    never match, so clearing its stored match every hour would silently strip the
    expected count that drives the reanalysis ceiling.
    """
    _stub_feed(monkeypatch)
    old_id = _session("Ancient", "2023-05-01T15:00:00", "2023-05-01T15:50:00")
    storage.set_calendar_match(old_id, {
        "uid": "long-ago",
        "title": "Old planning call",
        "attendee_count": 5,
        "confirmed": False,
    })
    assert _row(old_id)["expected_speaker_count"] == 5

    summary = calendar_sync.refresh(force=True)

    assert summary["cleared"] == 0
    assert storage.get_calendar_match(old_id)["uid"] == "long-ago"
    assert _row(old_id)["expected_speaker_count"] == 5


def test_refresh_drops_a_stale_attendee_count_on_a_re_match(data_dir, monkeypatch):
    """Re-matching onto an attendee-less event must not keep the old count."""
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)
    path = data_dir / "resolution_candidates" / f"{session_id}.json"
    assert json.loads(path.read_text(encoding="utf-8"))["meeting"]["attendee_count"] == 2

    # The meeting is replaced by one with no attendees at the same time.
    _stub_feed(monkeypatch, (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:replacement\r\n"
        "SUMMARY:Something else entirely\r\n"
        "DTSTART:20260903T151000Z\r\n"
        "DTEND:20260903T161000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    ))
    calendar_sync.refresh(force=True)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["meeting"]["calendar_uid"] == "replacement"
    assert payload["meeting"]["attendee_count"] is None
    assert storage.get_calendar_match(session_id)["attendee_count"] is None
    # The ceiling that drives reanalysis goes with it.
    assert _row(session_id)["expected_speaker_count"] is None


def test_merge_never_replaces_an_unreadable_candidates_file(data_dir, monkeypatch):
    """A truncated file may be half-written hand-fed data. Leave it exactly as is."""
    instances = _stub_feed(monkeypatch)
    session_id = _session()
    path = data_dir / "resolution_candidates" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    truncated = '{"meeting": {"title": "Hand-written"}, "candidates": [{"name": "Hand'
    path.write_text(truncated, encoding="utf-8")
    before = path.read_bytes()

    instance = next(inst for inst in instances if inst.uid == "overlapping")
    match = calendar_sync.build_match({
        "instance": instance, "score": 0.9, "overlap_seconds": 2400,
        "start_delta_seconds": 600, "reason": "80% overlap",
    }, [])

    assert calendar_sync.merge_candidates(session_id, instance, match) is False
    assert path.read_bytes() == before

    # A full refresh must not touch it either.
    calendar_sync.refresh(force=True)
    assert path.read_bytes() == before


def test_merge_writes_atomically(data_dir, monkeypatch):
    """A write that dies mid-flight leaves the previous file intact."""
    instances = _stub_feed(monkeypatch)
    session_id = _session()
    path = data_dir / "resolution_candidates" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"meeting": {"title": "Previous"}, "candidates": []})
    path.write_text(original, encoding="utf-8")

    instance = next(inst for inst in instances if inst.uid == "overlapping")
    match = calendar_sync.build_match({
        "instance": instance, "score": 0.9, "overlap_seconds": 2400,
        "start_delta_seconds": 600, "reason": "80% overlap",
    }, [])

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(calendar_sync.json, "dump", explode)
    assert calendar_sync.merge_candidates(session_id, instance, match) is False
    assert path.read_text(encoding="utf-8") == original
    assert list(path.parent.glob("*.tmp")) == []

    monkeypatch.undo()
    assert calendar_sync.merge_candidates(session_id, instance, match) is True
    assert json.loads(path.read_text(encoding="utf-8"))["meeting"]["title"] == "Previous"
    assert list(path.parent.glob("*.tmp")) == []


def test_confirm_by_uid_picks_the_occurrence_nearest_the_recording(data_dir, monkeypatch):
    """A uid alone matches every occurrence of a series; the nearest one wins."""
    _stub_feed(monkeypatch, RECURRING_ICS)
    session_id = _session("Recording", "2026-09-09T15:00:00", "2026-09-09T15:30:00")
    calendar_sync.refresh(force=True)

    result = calendar_sync.confirm_match(session_id, "weekly-1")

    assert result["ok"] is True
    assert result["match"]["start"].startswith("2026-09-09T15:00")


def test_confirm_then_clear_releases_the_calendar_count(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)

    calendar_sync.confirm_match(session_id, "overlapping")
    assert _row(session_id)["expected_speaker_count"] == 2

    cleared = calendar_sync.clear_match(session_id)

    assert cleared["ok"] is True
    assert calendar_sync.get_match(session_id)["match"] is None
    assert _row(session_id)["expected_speaker_count"] is None


def test_clear_match_keeps_a_count_the_user_typed(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)
    storage.set_expected_speaker_count(session_id, 9, "user")

    calendar_sync.clear_match(session_id)

    assert _row(session_id)["expected_speaker_count"] == 9
    assert _row(session_id)["expected_speaker_source"] == "user"


def test_clear_match_reports_a_missing_session(data_dir):
    result = calendar_sync.clear_match("does-not-exist")
    assert result["ok"] is False
    assert result["reason"] == "no_session"


def test_confirm_match_reports_a_stale_event(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)

    result = calendar_sync.confirm_match(session_id, "never-heard-of-it")

    assert result["ok"] is False
    assert result["reason"] == "stale_event"


def test_guessed_timezone_surfaces_in_the_match_reason(data_dir, monkeypatch):
    """An hours-wrong match is invisible unless the guess travels with it."""
    _stub_feed(monkeypatch, UNKNOWN_ZONE_ICS)
    session_id = _session("Recording", "2026-09-03T15:00:00", "2026-09-03T15:50:00")

    calendar_sync.refresh(force=True)

    match = storage.get_calendar_match(session_id)
    assert match["uid"] == "mystery"
    assert "unknown timezone" in match["reason"]
    assert "America/Chicago" in match["reason"]


# ── Round 2 ──────────────────────────────────────────────────────────────────

def test_not_a_calendar_meeting_survives_the_next_refresh(data_dir, monkeypatch):
    """Clearing writes a tombstone, not a NULL.

    A NULL is indistinguishable from "never matched", so the next hourly
    refresh would re-attach the very meeting the user just rejected, along with
    its attendee count and the reanalysis ceiling that count sets.
    """
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)
    assert storage.get_calendar_match(session_id)["uid"] == "overlapping"

    calendar_sync.clear_match(session_id)
    summary = calendar_sync.refresh(force=True)

    stored = storage.get_calendar_match(session_id)
    assert stored["cleared"] is True
    assert stored["confirmed"] is True
    assert stored["cleared_at"]
    assert calendar_sync.get_match(session_id) == {
        "match": None, "alternatives": [], "cleared": True,
    }
    assert _row(session_id)["expected_speaker_count"] is None
    # A tombstone is a decision, not a match.
    assert summary["matched"] == 0
    assert summary["unmatched"] == 1


def test_list_sessions_renders_a_tombstone_as_cleared(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    session_id = _session()
    calendar_sync.refresh(force=True)
    calendar_sync.clear_match(session_id)

    summary = _row(session_id)["calendar_match"]

    assert summary["cleared"] is True
    assert summary["confirmed"] is True
    assert summary["title"] is None
    assert summary["attendee_count"] is None


def test_frozen_clock_keeps_the_window_stable(data_dir, monkeypatch):
    """The fixtures are dated September 2026; the module clock is frozen there.

    Without this the suite would start failing once the real date drifted more
    than 120 days past the fixtures.
    """
    assert calendar_sync._utcnow() == FROZEN_NOW
    _stub_feed(monkeypatch)
    session_id = _session()

    assert calendar_sync.refresh(force=True)["matched"] == 1
    assert storage.get_calendar_match(session_id)["uid"] == "overlapping"


def test_plan_detail_does_not_promise_more_speakers(data_dir, monkeypatch):
    """A ceiling cannot force the diarizer to find more voices."""
    _stub_feed(monkeypatch)
    session_id = _session()
    storage.save_speaker_label(session_id, "speaker_1", name="Speaker 1")
    storage.save_segment(session_id, "one two three four five", "speaker_1", 0, 40)
    calendar_sync.refresh(force=True)

    plan = calendar_sync.build_plan(session_id)

    assert plan["action"] == "reanalyze"
    assert "up to 2 speakers" in plan["detail"]
    assert "ceiling" not in plan["detail"]
    assert "too similar to separate" in plan["detail"]


def test_set_link_requires_https(data_dir):
    for bad in ("", "   ", "http://outlook.office365.com/cal.ics", "ftp://x/cal.ics"):
        result = calendar_sync.set_link(bad)
        assert result["ok"] is False
        assert "https" in result["error"]
    # The stored link is untouched by a rejected write.
    assert settings.get("calendar_ics_url") == FEED_URL


def test_set_link_stores_and_masks(data_dir):
    fresh = "https://outlook.office365.com/owa/calendar/other@example.com/other/calendar.ics"
    settings.update({"calendar_last_error": "an old failure"})

    result = calendar_sync.set_link(f"  {fresh}  ")

    assert result["ok"] is True
    assert settings.get("calendar_ics_url") == fresh
    assert result["url_masked"] == calendar_feed.mask_url(fresh)
    assert fresh not in result["url_masked"]
    assert settings.get("calendar_last_error") == ""


def test_clear_link_forgets_everything(data_dir, monkeypatch):
    _stub_feed(monkeypatch)
    _session()
    calendar_sync.refresh(force=True)
    assert calendar_feed.load_cache()["instances"]

    result = calendar_sync.clear_link()

    assert result == {"ok": True, "cleared": True, "url_masked": ""}
    assert settings.get("calendar_ics_url") == ""
    assert settings.get("calendar_enabled") is False
    assert calendar_feed.load_cache()["instances"] == []
    assert calendar_sync.status()["has_url"] is False


def test_generic_preferences_never_write_the_link(data_dir):
    """A stale tab holds a mask (or a blank) for this key and must not win."""
    stale = {
        "calendar_ics_url": calendar_feed.mask_url("https://example.com/old/cal.ics"),
        "sidebar_open": False,
    }

    cleaned = calendar_sync.sanitize_preferences(stale)

    assert "calendar_ics_url" not in cleaned
    assert cleaned == {"sidebar_open": False}
    settings.update(cleaned)
    assert settings.get("calendar_ics_url") == FEED_URL

    assert calendar_sync.sanitize_preferences({"calendar_ics_url": ""}) == {}
    assert calendar_sync.sanitize_preferences(None) == {}


def test_session_span_ignores_a_healed_ended_at_days_later():
    from core import calendar_sync
    start, end = calendar_sync._session_span({
        "started_at": "2026-08-28T15:31:00",
        "ended_at": "2026-09-01T00:20:00",   # a crash-healed value
        "last_segment_time": 2220.0,          # 37 minutes of transcript
    })
    assert (end - start).total_seconds() == 2220.0
    start, end = calendar_sync._session_span({
        "started_at": "2026-08-27T11:00:00",
        "ended_at": "2026-09-03T23:55:00",
        "last_segment_time": None,
    })
    assert (end - start).total_seconds() == calendar_sync.FALLBACK_RECORDING_MINUTES * 60


def test_session_span_trusts_the_transcript_when_ended_at_overshoots():
    from core import calendar_sync
    # 6 hours of ended_at against 63 minutes of transcript: a healed value.
    start, end = calendar_sync._session_span({
        "started_at": "2026-06-25T13:02:00",
        "ended_at": "2026-06-25T19:02:00",
        "last_segment_time": 63 * 60.0,
    })
    assert (end - start).total_seconds() == 63 * 60.0
    # A few minutes of trailing silence is normal and keeps ended_at.
    start, end = calendar_sync._session_span({
        "started_at": "2026-06-25T13:02:00",
        "ended_at": "2026-06-25T14:10:00",
        "last_segment_time": 63 * 60.0,
    })
    assert (end - start).total_seconds() == 68 * 60.0
    # A quiet 50-minute meeting with 40 seconds of transcript keeps ended_at too.
    start, end = calendar_sync._session_span({
        "started_at": "2026-09-03T15:00:00",
        "ended_at": "2026-09-03T15:50:00",
        "last_segment_time": 40.0,
    })
    assert (end - start).total_seconds() == 50 * 60.0
    # A transcript-derived span never shrinks below fifteen minutes.
    start, end = calendar_sync._session_span({
        "started_at": "2026-09-03T15:00:00",
        "ended_at": "2026-09-04T15:00:00",
        "last_segment_time": 40.0,
    })
    assert (end - start).total_seconds() == 15 * 60.0
