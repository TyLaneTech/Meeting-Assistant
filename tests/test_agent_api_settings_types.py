"""The Agent API's settings: internal queues are read-only, lists stay lists.

post_meeting_pending (the after-meeting transcription queue) was listed as a
writable string, so an agent could drop a meeting from the queue, or replace
the list with a string that every reader then iterated as characters. The same
string coercion applied to obsidian_export_force_ids.

Run: .venv/Scripts/python -m pytest tests/test_agent_api_settings_types.py
"""
from agent_api import helpers


def _schema(key):
    return next(s for s in helpers.settings_schema() if s["key"] == key)


def test_the_after_meeting_queue_is_not_writable():
    entry = _schema("post_meeting_pending")
    assert entry["writable"] is False
    assert entry["type"] == "array"
    assert "Machine-managed" in entry["description"]
    assert "post_meeting_pending" in helpers.SETTINGS_WRITE_DENYLIST


def test_a_list_setting_takes_an_array_of_strings():
    assert _schema("obsidian_export_force_ids")["type"] == "array"
    assert _schema("obsidian_export_force_ids")["writable"] is True
    ok, value = helpers.coerce_setting("obsidian_export_force_ids", ["a1", "b2"])
    assert ok and value == ["a1", "b2"]


def test_a_list_setting_refuses_anything_else():
    for bad in ("a1", 5, ["a1", 5], {"a": 1}, None):
        ok, reason = helpers.coerce_setting("obsidian_export_force_ids", bad)
        assert not ok and "array of strings" in reason, bad
