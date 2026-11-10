"""Meeting reminders, a Join that records, and a Stop that stays put.

- core/meeting_reminders.py decides which reminder is due from the cached
  calendar (pure, driven here with a fixed clock) and remembers what it
  showed and what was silenced (a file in a temp folder here).
- The reminder notification offers Join when the meeting has a link and
  Silence while another reminder is still to come.
- Joining from the app can start a recording (calendar_join_autostart, off
  by default), through the start coordinator so the page does the start.
- Stopping from the tray or the "Still in the meeting?" notification stops
  the recording and leaves the app window alone.

Run: .venv/Scripts/python -m pytest tests/test_meeting_reminders.py
"""
from __future__ import annotations

import io
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_api import helpers
from core import calendar_feed, settings
from core import meeting_reminders as mr
from ui_desktop import notifications

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
SETTINGS_HTML = (ROOT / "ui_web/templates/_settings.html").read_text(encoding="utf-8")

UTC = timezone.utc
START = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
SERVER = "http://127.0.0.1:6969"


def _meeting(uid="standup", title="Stand-up", start=START, minutes=30, **kw):
    return calendar_feed.Instance(uid=uid, summary=title, start=start,
                                  end=start + timedelta(minutes=minutes), **kw)


def _key(instance):
    return instance.uid


def _link(instance):
    return {"provider": "teams", "label": "Teams"} if instance.join_url else None


def _due(instances, minutes, now, **kw):
    kw.setdefault("key_for", _key)
    kw.setdefault("link_for", _link)
    return mr.due(instances, minutes, now, **kw)


def _fn(src, start, end):
    return src[src.index(start):src.index(end)]


# ── When a reminder is due ────────────────────────────────────────────────────

def test_the_saved_timing_is_cleaned_not_trusted():
    assert mr.normalize_minutes(None) == [5, 0]
    assert mr.normalize_minutes([0, 5, 5, "2", 7, True, None, "x"]) == [5, 2, 0]
    assert mr.normalize_minutes("15") == [15]
    assert mr.normalize_minutes([]) == []


def test_a_reminder_shows_at_its_moment_and_not_before():
    m = _meeting()
    assert _due([m], [5], START - timedelta(minutes=5, seconds=1)) == ([], {})
    shown, marks = _due([m], [5], START - timedelta(minutes=5))
    assert [(r.key, r.minutes, r.title) for r in shown] == [("standup", 5, "Stand-up")]
    assert marks == {"standup:5": m.end}


def test_each_moment_shows_once_and_the_last_has_nothing_after_it():
    m = _meeting()
    sent: dict = {}
    first, marks = _due([m], [5, 0], START - timedelta(minutes=5) + timedelta(seconds=3))
    sent.update(marks)
    assert [r.minutes for r in first] == [5]
    assert first[0].later is True
    again, _ = _due([m], [5, 0], START - timedelta(minutes=4), sent=sent)
    assert again == []
    last, marks = _due([m], [5, 0], START + timedelta(seconds=4), sent=sent)
    assert [r.minutes for r in last] == [0]
    assert last[0].later is False


def test_a_moment_missed_by_more_than_the_grace_is_settled_without_showing():
    """The app was asleep through the five-minute mark: "in 5 minutes" read
    two minutes before the start is wrong, so it is dropped, not shown late."""
    m = _meeting()
    shown, marks = _due([m], [5, 0], START - timedelta(minutes=2))
    assert shown == []
    assert "standup:5" in marks
    at_start, _ = _due([m], [5, 0], START + timedelta(seconds=5), sent=marks)
    assert [r.minutes for r in at_start] == [0]


def test_moments_due_together_show_only_the_latest():
    m = _meeting()
    shown, marks = _due([m], [2, 1], START - timedelta(seconds=55))
    assert [r.minutes for r in shown] == [1]
    assert set(marks) == {"standup:2", "standup:1"}


def test_a_silenced_meeting_gets_nothing_more():
    m = _meeting()
    assert _due([m], [5, 0], START, silenced={"standup"}) == ([], {})


def test_only_real_meetings_are_reminded():
    ok = _meeting("ok", "Planning")
    skipped = [
        _meeting("allday", "Remote", all_day=True),
        _meeting("cancelled", "Sync", status="CANCELLED"),
        _meeting("ghost", "Canceled: Sync"),
        _meeting("block", "Focus time", minutes=5 * 60),
        calendar_feed.Instance(uid="timeless", summary="No times"),
    ]
    shown, _ = _due([ok, *skipped], [0], START)
    assert [r.key for r in shown] == ["ok"]


def test_a_private_appointment_keeps_its_subject_to_itself():
    shown, _ = _due([_meeting(title="Oncology results", is_private=True)], [0], START)
    assert shown[0].title == "Private appointment"


def test_join_is_offered_only_with_a_join_link():
    linked = _meeting("linked", join_url="https://teams.microsoft.com/l/meetup-join/x")
    plain = _meeting("plain", start=START)
    shown, _ = _due([linked, plain], [0], START)
    by_key = {r.key: r for r in shown}
    assert (by_key["linked"].provider, by_key["linked"].provider_label) == ("teams", "Teams")
    assert by_key["plain"].provider == ""


def test_the_real_event_key_is_the_one_the_calendar_view_uses():
    from core import calendar_events_api
    m = _meeting()
    shown, _ = mr.due([m], [0], START)
    assert shown[0].key == calendar_events_api.event_key(m)


def test_the_text_says_when():
    r = mr.Reminder(key="k", title="t", start=START, end=START + timedelta(minutes=30),
                    minutes=5, later=True)
    assert r.when(START - timedelta(minutes=5) + timedelta(seconds=8)) == "Starts in 5 minutes"
    assert r.when(START - timedelta(seconds=60)) == "Starts in 1 minute"
    assert r.when(START + timedelta(seconds=10)) == "Starting now"
    assert r.when(START + timedelta(minutes=2)) == "Started 2 minutes ago"
    assert re.fullmatch(r"Starts in 5 minutes \(\d{1,2}:\d{2}( [AP]M)? to \d{1,2}:\d{2} [AP]M\)",
                        r.body(START - timedelta(minutes=5)))


# ── What is remembered ───────────────────────────────────────────────────────

def test_a_restart_does_not_repeat_a_reminder(tmp_path):
    path = tmp_path / "meeting_reminders.json"
    m = _meeting()
    now = START - timedelta(minutes=5)
    first = mr.ReminderBook(path).take_due([m], [5, 0], now, key_for=_key, link_for=_link)
    assert [r.minutes for r in first] == [5]
    after_restart = mr.ReminderBook(path).take_due([m], [5, 0], now + timedelta(seconds=10),
                                                   key_for=_key, link_for=_link)
    assert after_restart == []


def test_silence_outlives_a_restart_and_only_covers_that_meeting(tmp_path):
    path = tmp_path / "meeting_reminders.json"
    today = _meeting()
    tomorrow = _meeting("standup-tomorrow", start=START + timedelta(days=1))
    mr.ReminderBook(path).silence("standup", today.end)
    book = mr.ReminderBook(path)
    assert book.is_silenced("standup")
    assert book.take_due([today], [0], START, key_for=_key, link_for=_link) == []
    shown = book.take_due([tomorrow], [0], START + timedelta(days=1), key_for=_key, link_for=_link)
    assert [r.key for r in shown] == ["standup-tomorrow"]


def test_old_entries_are_dropped(tmp_path):
    path = tmp_path / "meeting_reminders.json"
    stale = (START - timedelta(days=3)).isoformat()
    path.write_text(json.dumps({"sent": {"old:5": stale}, "silenced": {"old": stale}}),
                    encoding="utf-8")
    mr.ReminderBook(path).take_due([], [5], START)
    assert json.loads(path.read_text(encoding="utf-8")) == {"sent": {}, "silenced": {}}


def test_a_damaged_file_means_no_memory_not_a_crash(tmp_path):
    path = tmp_path / "meeting_reminders.json"
    path.write_text("{not json", encoding="utf-8")
    shown = mr.ReminderBook(path).take_due([_meeting()], [0], START, key_for=_key, link_for=_link)
    assert [r.minutes for r in shown] == [0]


# ── Settings ─────────────────────────────────────────────────────────────────

def test_reminders_and_recording_on_join_are_off_by_default():
    assert settings.DEFAULTS["meeting_reminders_enabled"] is False
    assert settings.DEFAULTS["calendar_join_autostart"] is False
    assert mr.normalize_minutes(settings.DEFAULTS["meeting_reminder_minutes"]) == [5, 0]


def test_an_agent_can_set_the_timing_only_to_real_choices():
    ok, value = helpers.coerce_setting("meeting_reminder_minutes", [0, 15])
    assert ok and value == [15, 0]
    for bad in ([7], ["5"], 5, [True], None, "5,0"):
        ok, reason = helpers.coerce_setting("meeting_reminder_minutes", bad)
        assert not ok and "minutes" in reason, bad


def test_settings_offers_every_choice_and_the_join_switch():
    chips = re.findall(r'class="reminder-time" data-minutes="(\d+)"', SETTINGS_HTML)
    assert [int(c) for c in chips] == list(mr.CHOICES)
    assert "const _REMINDER_CHOICES = [30, 15, 10, 5, 2, 1, 0];" in APP_JS
    assert 'id="meeting-reminders-enabled"' in SETTINGS_HTML
    assert 'id="calendar-join-autostart"' in SETTINGS_HTML
    save = _fn(APP_JS, "function saveCalendarSettings(", "async function saveCalendarLink(")
    assert "calendar_join_autostart:" in save


# ── The reminder notification ────────────────────────────────────────────────

def _capture(monkeypatch):
    calls = []
    monkeypatch.setattr(notifications, "notify",
                        lambda *a, **kw: calls.append((a, kw)) or True)
    return calls


def _reminder(**kw):
    base = dict(key="k1", title="Stand-up", start=START, end=START + timedelta(minutes=30),
                minutes=5, later=True, provider="teams", provider_label="Teams")
    base.update(kw)
    return mr.Reminder(**base)


def test_the_reminder_offers_join_and_silence(monkeypatch):
    calls = _capture(monkeypatch)
    silenced = []
    notifications.send_meeting_reminder_toast(
        _reminder(), SERVER, on_join=lambda _a: None, on_silence=silenced.append,
        now=START - timedelta(minutes=5))
    (title, body), kw = calls[0]
    assert title == "Stand-up"
    assert body.startswith("Starts in 5 minutes")
    assert [a["label"] for a in kw["actions"]] == ["Join", "Silence this meeting"]
    assert kw["tag"] == mr.tag_for("k1")
    assert kw["timeout"] >= 5 * 60          # up until after the start
    kw["actions"][1]["on_click"]("silence")
    assert silenced == ["silence"]


def test_an_early_reminder_does_not_sit_there_for_half_an_hour(monkeypatch):
    calls = _capture(monkeypatch)
    notifications.send_meeting_reminder_toast(
        _reminder(minutes=30), SERVER, now=START - timedelta(minutes=30))
    assert calls[0][1]["timeout"] == 15 * 60


def test_join_says_it_records_when_it_will(monkeypatch):
    calls = _capture(monkeypatch)
    notifications.send_meeting_reminder_toast(
        _reminder(), SERVER, on_join=lambda _a: None, on_silence=lambda _a: None,
        records=True, now=START - timedelta(minutes=5))
    assert calls[0][1]["actions"][0]["label"] == "Join and record"


def test_the_last_reminder_has_nothing_to_silence_and_no_link_means_no_join(monkeypatch):
    calls = _capture(monkeypatch)
    notifications.send_meeting_reminder_toast(
        _reminder(later=False, provider="", provider_label="", minutes=0), SERVER,
        on_join=lambda _a: None, on_silence=lambda _a: None, now=START)
    assert calls[0][1]["actions"] == []
    assert calls[0][0][1].startswith("Starting now")


def test_the_loop_is_off_unless_reminders_and_the_calendar_are_on():
    active = _fn(APP_PY, "def _reminders_active(", "def _show_meeting_reminder(")
    for needed in ('"meeting_reminders_enabled"', '"calendar_enabled"', '"calendar_ics_url"'):
        assert needed in active
    loop = _fn(APP_PY, "def _meeting_reminder_loop(", "threading.Thread(target=_meeting_reminder_loop")
    assert "if not _reminders_active(cfg):" in loop
    assert "except Exception as e:" in loop          # a bad feed never kills the loop
    shown = _fn(APP_PY, "def _show_meeting_reminder(", "def _meeting_reminder_loop(")
    assert "reminder.title" not in shown.split("log.info", 1)[1], "no subject in the log"


# ── Joining starts a recording, the page's way ───────────────────────────────

def test_joining_records_only_when_asked_and_through_the_coordinator():
    join = _fn(APP_PY, "def _join_calendar_event(", '@app.route("/api/calendar/join"')
    assert 'settings.get("calendar_join_autostart", False)' in join
    assert "recording_request.request_start_async(" in join
    # The page performs every start (the ?autostart rule): nothing here opens a
    # capture itself.
    assert "/api/recording/start" not in join
    assert "start_recording(" not in join
    # A recording already running is left alone.
    assert 'recording = "already"' in join
    # Joining silences the rest of that meeting's reminders.
    assert "_reminder_book.silence(" in join
    route = _fn(APP_PY, '@app.route("/api/calendar/join"', "# ── Meeting reminders")
    assert '_join_calendar_event(data.get("key"), "page")' in route


def test_the_page_says_a_recording_is_starting_and_leaves_the_start_to_the_command():
    join = _fn(APP_JS, "async function joinCalendarMeeting(", "// ── Calendar (published ICS feed)")
    assert "data.recording === 'starting'" in join
    assert "data.recording === 'already'" in join
    assert "toggleRecording" not in join and "/api/recording/start" not in join


# ── Stop stays put ───────────────────────────────────────────────────────────

def test_stopping_from_the_quiet_notification_leaves_the_window_alone(monkeypatch):
    calls = _capture(monkeypatch)
    shown, posted = [], []
    monkeypatch.setattr(notifications.app_window, "show", lambda *a, **kw: shown.append(a))
    monkeypatch.setattr(notifications, "_post_stop",
                        lambda url, source: posted.append((url, source)))
    notifications.send_quiet_recording_toast("sid", SERVER)
    kw = calls[0][1]
    stop = next(a for a in kw["actions"] if a.get("arg") == "stop")
    stop["on_click"]("stop")
    assert posted == [(f"{SERVER}/api/recording/stop", "toast:quiet")]
    assert shown == []
    kw["on_click"]("")                       # the body still opens the meeting
    assert len(shown) == 1


def test_stopping_from_the_tray_leaves_the_window_alone(monkeypatch):
    from ui_desktop import tray

    posted, shown, started = [], [], []
    done = threading.Event()

    def _urlopen(req, timeout=None):
        posted.append((req.full_url, req.data))
        done.set()
        return io.BytesIO(b"{}")

    monkeypatch.setattr(tray.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(tray.app_window, "show", lambda *a, **kw: shown.append(a))
    monkeypatch.setattr(tray.recording_request, "request_start_async",
                        lambda *a, **kw: started.append(a))
    icon = tray.MeetingTray(SERVER, lambda: {"is_recording": True}, lambda _icon: None)
    icon._toggle_recording()
    assert done.wait(5)
    assert posted == [(f"{SERVER}/api/recording/stop", b'{"source": "tray"}')]
    assert shown == [] and started == []


def test_starting_from_the_tray_asks_for_no_window(monkeypatch):
    from ui_desktop import tray

    shown, started = [], []
    monkeypatch.setattr(tray.app_window, "show", lambda *a, **kw: shown.append(a))
    monkeypatch.setattr(tray.recording_request, "request_start_async",
                        lambda *a, **kw: started.append((a, kw)))
    icon = tray.MeetingTray(SERVER, lambda: {"is_recording": False}, lambda _icon: None)
    icon._toggle_recording()
    assert started == [(("tray",), {"show_window": False})]
    assert shown == []
    # The coordinator can only honour that with a hidden page to hand it.
    assert "open_hidden=browser.open_hidden_page," in APP_PY


def test_the_stop_log_names_where_it_came_from():
    stop = _fn(APP_PY, "def stop_recording():", "# ── Deferred tail")
    assert 'stop_source = str((request.get_json(silent=True) or {}).get("source") or "")[:40]' in stop
    assert 'tail += f", from {stop_source}"' in stop
    assert "app_window.show(" not in stop
