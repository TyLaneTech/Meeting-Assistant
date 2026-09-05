"""Tests for the read-only Home dashboard API."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import pytest
from flask import Flask

from core import dashboard_api, paths, settings, storage

UTC = timezone.utc
FROZEN_NOW = datetime(2026, 9, 3, 18, 0, tzinfo=UTC)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(dashboard_api, "_utcnow", lambda: FROZEN_NOW)
    storage.init_db()
    settings.update({
        "calendar_timezone": "America/Chicago",
        "me_speaker_global_id": None,
    })
    app = Flask(__name__)
    app.register_blueprint(dashboard_api.bp)
    return app.test_client()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _global(global_id: str, name: str, color: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO global_speakers"
            " (id, name, color, centroid, emb_count, created_at, updated_at)"
            " VALUES (?, ?, ?, NULL, 0, ?, ?)",
            (global_id, name, color, "2026-01-01", "2026-01-01"),
        )


def _label(
    session_id: str,
    speaker_key: str,
    label_name: str,
    global_id: str | None,
) -> None:
    storage.save_speaker_label(session_id, speaker_key, name=label_name)
    with _db() as conn:
        conn.execute(
            "UPDATE speaker_labels SET global_id = ?"
            " WHERE session_id = ? AND speaker_key = ?",
            (global_id, session_id, speaker_key),
        )


def test_activity_local_days_duration_rule_and_week(client):
    segmented = storage.create_session(
        "Across midnight",
        started_at="2026-09-03T02:30:00",
        ended_at="2026-09-03T12:30:00",
    )
    storage.save_segment(segmented, "two words", "speaker_1", 10, 120)
    storage.create_session(
        "Healed without transcript",
        started_at="2026-09-02T14:00:00",
        ended_at="2026-09-02T21:00:00",
    )
    storage.create_session(
        "Normal fallback",
        started_at="2026-08-31T15:00:00",
        ended_at="2026-08-31T16:00:00",
    )

    payload = client.get("/api/dashboard").get_json()

    assert payload["timezone"] == "America/Chicago"
    assert payload["generated_at"] == "2026-09-03T18:00:00+00:00"
    assert len(payload["activity"]) == 14
    assert [item["day"] for item in payload["activity"]] == sorted(
        item["day"] for item in payload["activity"]
    )
    assert payload["activity"][0]["day"] == "2026-08-21"
    assert payload["activity"][-1]["day"] == "2026-09-03"
    by_day = {item["day"]: item for item in payload["activity"]}
    assert by_day["2026-09-02"] == {"day": "2026-09-02", "count": 2, "seconds": 120}
    assert by_day["2026-08-31"]["seconds"] == 3600
    assert payload["totals"]["seconds"] == 3720
    assert "words" not in payload["totals"]
    assert payload["this_week"] == {
        "sessions": 3,
        "seconds": 3720,
        "start": "2026-08-31",
        "end": "2026-09-06",
    }


def test_people_ranking_window_generic_exclusion_and_me(client):
    _global("jordan", "Jordan Zobel", "#db61a2")
    _global("owner", "Alex Chen", "#336699")
    _global("generic", "Should not appear", "#000000")
    _global("old", "Old Person", "#999999")
    settings.update({"me_speaker_global_id": "owner"})

    recent_one = storage.create_session(
        "One", started_at="2026-09-01T15:00:00", ended_at="2026-09-01T16:00:00"
    )
    recent_two = storage.create_session(
        "Two", started_at="2026-08-20T15:00:00", ended_at="2026-08-20T16:00:00"
    )
    boundary = storage.create_session(
        "Boundary", started_at="2026-07-09T06:00:00", ended_at="2026-07-09T07:00:00"
    )
    too_old = storage.create_session(
        "Old", started_at="2026-07-08T15:00:00", ended_at="2026-07-08T16:00:00"
    )
    _label(recent_one, "j1", "Jordan Zobel", "jordan")
    _label(recent_two, "j2", "Jordan Zobel", "jordan")
    _label(recent_one, "me", "Alex Chen", "owner")
    _label(boundary, "me", "Alex Chen", "owner")
    _label(recent_two, "g", "Speaker 2", "generic")
    _label(too_old, "old", "Old Person", "old")
    storage.save_segment(recent_one, "jordan", "j1", 0, 100)
    storage.save_segment(recent_two, "jordan", "j2", 0, 200)
    storage.save_segment(recent_one, "owner", "me", 0, 500)
    storage.save_segment(boundary, "owner", "me", 0, 50)
    storage.save_segment(recent_two, "generic", "g", 0, 900)
    storage.save_segment(too_old, "old", "old", 0, 900)

    people = client.get("/api/dashboard").get_json()["people"]

    assert people["weeks"] == 8
    assert people["since"] == "2026-07-09"
    assert [item["global_id"] for item in people["items"]] == ["owner", "jordan"]
    assert people["items"][0] == {
        "global_id": "owner",
        "name": "Alex Chen",
        "color": "#336699",
        "meeting_count": 2,
        "talk_seconds": 550,
        "segment_count": 2,
        "is_me": True,
    }
    assert people["items"][1]["meeting_count"] == 2
    assert people["items"][1]["talk_seconds"] == 300
    assert people["items"][1]["is_me"] is False


def test_dashboard_fixture_is_under_one_second(client):
    for index in range(54):
        session_id = storage.create_session(
            f"Fixture {index}",
            started_at=f"2026-09-{1 + index % 3:02d}T15:00:00",
            ended_at=f"2026-09-{1 + index % 3:02d}T15:30:00",
        )
        storage.save_segment(session_id, "a small fixture transcript", "speaker_1", 0, 1800)

    started = time.perf_counter()
    response = client.get("/api/dashboard")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.get_json()["totals"]["sessions"] == 54
    assert elapsed < 1.0
