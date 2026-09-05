import json

import pytest

from core import paths, storage
from core.attention import compute_attention, is_generic_speaker_name


@pytest.mark.parametrize(
    "name",
    [
        "Speaker 1",
        "speaker12",
        "Other participant",
        "other participant 2",
        "UNKNOWN",
        "unidentified",
        "Guest",
        "participant9",
        "",
        None,
    ],
)
def test_generic_speaker_names(name):
    assert is_generic_speaker_name(name)


@pytest.mark.parametrize("name", ["Alice", "Speaker", "Guest 2", "participant one"])
def test_non_generic_speaker_names(name):
    assert not is_generic_speaker_name(name)


def _speaker(seconds=0, words=0, name="Speaker 1", is_noise=False):
    return {
        "name": name,
        "is_noise": is_noise,
        "talk_seconds": seconds,
        "word_count": words,
    }


def test_compute_attention_content_thresholds():
    below = compute_attention([_speaker(seconds=14, words=24)], None)
    assert below == {
        "needs": False,
        "reasons": [],
        "unresolved": 0,
        "below_threshold": 1,
        "found": 0,
        "expected": None,
    }
    assert compute_attention([_speaker(seconds=15)], None)["unresolved"] == 1
    assert compute_attention([_speaker(words=25)], None)["unresolved"] == 1
    assert compute_attention([_speaker(seconds=30, is_noise=True)], None)["found"] == 0


@pytest.mark.parametrize(
    ("expected", "needs"),
    [(None, False), (0, False), (1, False), (2, True)],
)
def test_compute_attention_expected_count(expected, needs):
    result = compute_attention([_speaker(seconds=20, name="Alice")], expected)
    assert result["needs"] is needs
    assert ("speaker_count_mismatch" in result["reasons"]) is needs
    assert result["expected"] == expected


def test_storage_attention_calendar_and_candidate_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()

    first = storage.create_session("First")
    storage.save_speaker_label(first, "speaker_1", name="Speaker 1")
    storage.save_segment(first, "one two three", "speaker_1", 0, 15)
    storage.set_calendar_match(first, {
        "uid": "event-1",
        "title": "Planning",
        "start": "2026-09-03T10:00:00",
        "end": "2026-09-03T10:30:00",
        "attendee_count": 2,
        "confirmed": True,
        "ignored_extra": "not returned",
    })

    rows = {row["id"]: row for row in storage.list_sessions()}
    assert rows[first]["expected_speaker_count"] == 2
    assert rows[first]["expected_speaker_source"] == "calendar"
    assert rows[first]["attention"]["unresolved"] == 1
    assert rows[first]["attention"]["needs"] is True
    assert rows[first]["calendar_match"] == {
        "uid": "event-1",
        "title": "Planning",
        "start": "2026-09-03T10:00:00",
        "end": "2026-09-03T10:30:00",
        "attendee_count": 2,
        "confirmed": True,
        # Set by the "Not a calendar meeting" tombstone; absent here.
        "cleared": None,
    }
    assert storage.get_calendar_match(first)["ignored_extra"] == "not returned"

    storage.set_expected_speaker_count(first, 1, "user")
    storage.set_calendar_match(first, {"attendee_count": 5})
    assert storage.list_sessions()[0]["expected_speaker_count"] == 1
    assert storage.list_sessions()[0]["expected_speaker_source"] == "user"

    second = storage.create_session("Second")
    candidates = tmp_path / "resolution_candidates"
    candidates.mkdir()
    (candidates / f"{second}.json").write_text(
        json.dumps({"meeting": {"attendee_count": 3}}), encoding="utf-8"
    )
    assert storage.backfill_expected_counts(candidates) == 1
    second_row = next(row for row in storage.list_sessions() if row["id"] == second)
    assert second_row["expected_speaker_count"] == 3
    assert second_row["expected_speaker_source"] == "candidates"

    summary = storage.attention_summary()
    assert summary == {
        "needs_attention": 2,
        "unresolved_speakers": 1,
        "mismatched": 1,
    }
    analytics = storage.get_dashboard_analytics()
    assert analytics["needs_attention_count"] == 2
    assert analytics["unresolved_speaker_total"] == 1


def test_backfill_ignores_malformed_candidate_files(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    storage.init_db()
    ids = [storage.create_session(f"S{i}") for i in range(4)]
    candidates = tmp_path / "resolution_candidates"
    candidates.mkdir()
    (candidates / f"{ids[0]}.json").write_text('{"meeting": "oops"}', encoding="utf-8")
    (candidates / f"{ids[1]}.json").write_text("[1, 2]", encoding="utf-8")
    (candidates / f"{ids[2]}.json").write_text("null", encoding="utf-8")
    (candidates / f"{ids[3]}.json").write_text(
        '{"meeting": {"attendee_count": "4"}}', encoding="utf-8"
    )
    # None of these may raise, and none may set a count.
    assert storage.backfill_expected_counts(candidates) == 0
    for row in storage.list_sessions():
        assert row["expected_speaker_count"] is None
    # A missing directory is also fine.
    assert storage.backfill_expected_counts(tmp_path / "missing") == 0


@pytest.mark.parametrize(
    ("found", "expected", "needs"),
    [(11, 14, False), (5, 3, True), (1, 4, True), (6, 6, False), (7, 6, True), (3, 7, False)],
)
def test_compute_attention_mismatch_rules(found, expected, needs):
    speakers = [_speaker(seconds=30, name=f"Person {i}") for i in range(found)]
    result = compute_attention(speakers, expected)
    assert result["needs"] is needs
    assert ("speaker_count_mismatch" in result["reasons"]) is needs
