"""Meeting reminders: a notification before each meeting on the calendar.

Opt-in (``meeting_reminders_enabled``, default off, and only while the
calendar itself is on). The reminders read the same cached calendar the
Calendar view shows, so nothing here fetches anything: app.py's
``_meeting_reminder_loop`` asks :meth:`ReminderBook.take_due` every few
seconds and shows what comes back.

The user picks when (``meeting_reminder_minutes``): any combination of
:data:`CHOICES`, minutes before the start, 0 being the start itself. Every
reminder for one meeting carries the same toast tag, so the next one replaces
the last rather than stacking.

Two things are remembered on disk (``<data>/meeting_reminders.json``), so a
restart neither repeats a reminder nor forgets a choice:

- ``sent``: which reminder of which meeting has had its moment, shown or not.
- ``silenced``: meetings whose remaining reminders the user turned off (the
  toast's Silence button), or joined from the app, which says the same thing.

Both are kept per occurrence (the calendar's opaque event key), never per
series: silencing today's stand-up leaves tomorrow's alone. Entries are
dropped a day after their meeting ends.

A reminder whose moment passed while the app was not looking (asleep, not
running, just switched on) is dropped once it is :data:`LATE_GRACE` old
rather than shown late: "in 5 minutes" read at the start is wrong, and a
stack of stale reminders after a wake is noise.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

from core import calendar_feed

UTC = timezone.utc

# Minutes before a meeting a reminder can be set for; 0 is "at the start".
# Settings > Reminders offers exactly these.
CHOICES = (30, 15, 10, 5, 2, 1, 0)
DEFAULT_MINUTES = (5, 0)
# How late a reminder may still be shown after its moment.
LATE_GRACE = timedelta(minutes=2)
# Blocks this long are not meetings ("Focus time", a travel day): the Home
# focus strip skips them for the same reason.
BLOCK_SECONDS = 4 * 3600
# How long a shown or silenced occurrence is remembered after it ends.
KEEP_AFTER_END = timedelta(days=1)
# How often app.py looks. A reminder lands at most this late.
POLL_SEC = 10.0

TAG_PREFIX = "meeting-reminder:"


def tag_for(key: str) -> str:
    """The toast tag every reminder for one meeting shares."""
    return TAG_PREFIX + str(key or "")


def normalize_minutes(value) -> list[int]:
    """The saved timing as a clean list: known choices only, earliest first.

    ``None`` (never saved) is the default. Anything that is not a list of
    numbers from :data:`CHOICES` is dropped item by item, so a bad value can
    only ever mean fewer reminders, never a crash in the loop.
    """
    if value is None:
        return list(DEFAULT_MINUTES)
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    picked: set[int] = set()
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            minutes = int(float(item))
        except (TypeError, ValueError):
            continue
        if minutes in CHOICES:
            picked.add(minutes)
    return sorted(picked, reverse=True)


@dataclass(frozen=True)
class Reminder:
    """One reminder to show now."""
    key: str                 # the calendar's opaque event key
    title: str               # what the toast calls the meeting
    start: datetime          # aware UTC
    end: datetime            # aware UTC
    minutes: int             # the timing that fired (0 = at the start)
    later: bool              # another reminder for this meeting is still to come
    provider: str = ""       # join link provider slug, "" when there is none
    provider_label: str = ""

    @property
    def tag(self) -> str:
        return tag_for(self.key)

    def when(self, now: datetime) -> str:
        """"Starts in 5 minutes", "Starting now" or "Started 2 minutes ago"."""
        left = (self.start - now).total_seconds()
        if left > 45:
            n = max(1, int(round(left / 60)))
            return f"Starts in {n} minute{'' if n == 1 else 's'}"
        if left > -60:
            return "Starting now"
        n = max(1, int(round(-left / 60)))
        return f"Started {n} minute{'' if n == 1 else 's'} ago"

    def body(self, now: datetime) -> str:
        """The toast's second line: when it starts, and its time on this computer's clock."""
        return f"{self.when(now)} ({time_range(self.start, self.end)})"


def _clock(value: datetime) -> tuple[str, str]:
    """("10:30", "AM") on this computer's clock. Built by hand: Windows'
    strftime has no portable way to drop the leading zero of the hour."""
    local = value.astimezone()
    hour = local.hour % 12 or 12
    return f"{hour}:{local.minute:02d}", "AM" if local.hour < 12 else "PM"


def time_range(start: datetime, end: datetime) -> str:
    """"10:30 to 11:00 AM", or "11:30 AM to 12:30 PM" across noon."""
    s, s_half = _clock(start)
    e, e_half = _clock(end)
    if s_half == e_half:
        return f"{s} to {e} {e_half}"
    return f"{s} {s_half} to {e} {e_half}"


def _remindable(instance) -> bool:
    """A meeting worth a reminder: timed, not cancelled, not an all-day item
    or a long block."""
    start = calendar_feed.as_utc(instance.start)
    end = calendar_feed.as_utc(instance.end)
    if start is None or end is None or end <= start:
        return False
    if instance.all_day:
        return False
    if str(instance.status or "").strip().lower() == "cancelled":
        return False
    if calendar_feed.is_ghost_title(instance.summary):
        return False
    return (end - start).total_seconds() < BLOCK_SECONDS


def _default_key(instance) -> str:
    from core import calendar_events_api
    return calendar_events_api.event_key(instance)


def _default_link(instance) -> Optional[dict]:
    from core import meeting_links
    return meeting_links.find_link(instance)


def _mark(key: str, minutes: int) -> str:
    return f"{key}:{int(minutes)}"


def due(instances: Iterable, minutes, now: datetime, *, sent: Iterable[str] = (),
        silenced: Iterable[str] = (),
        key_for: Callable = _default_key,
        link_for: Callable = _default_link) -> tuple[list[Reminder], dict[str, datetime]]:
    """The reminders to show at ``now``, and the marks they settle.

    Pure: nothing is read or written. Returns ``(reminders, marks)`` where
    ``marks`` maps each settled ``"<key>:<minutes>"`` to the end of its
    meeting, so the caller can remember it until then. A moment that passed
    more than :data:`LATE_GRACE` ago is settled without being shown, and when
    several moments of one meeting are due together (the app started a
    minute before it, with 5 and 2 picked) only the latest is shown.
    """
    picked = normalize_minutes(minutes)
    if not picked:
        return [], {}
    now = calendar_feed.as_utc(now)
    sent = set(sent)
    silenced = set(silenced)
    earliest = max(picked)
    reminders: list[Reminder] = []
    marks: dict[str, datetime] = {}
    for instance in instances:
        if not _remindable(instance):
            continue
        start = calendar_feed.as_utc(instance.start)
        end = calendar_feed.as_utc(instance.end)
        # Outside this window there is nothing to show or settle, which keeps
        # the remembered marks to meetings around now.
        if now < start - timedelta(minutes=earliest) or now > start + LATE_GRACE:
            continue
        key = key_for(instance)
        if not key or key in silenced:
            continue
        moments = [(m, start - timedelta(minutes=m)) for m in picked]
        passed = [(m, at) for m, at in moments
                  if at <= now and _mark(key, m) not in sent]
        if not passed:
            continue
        for m, _at in passed:
            marks[_mark(key, m)] = end
        fresh = [(m, at) for m, at in passed if now - at <= LATE_GRACE]
        if not fresh:
            continue
        m, _at = min(fresh, key=lambda pair: pair[0])
        later = any(at > now for _m, at in moments)
        try:
            link = link_for(instance) or {}
        except Exception:
            link = {}
        title = ("Private appointment" if instance.is_private
                 else " ".join(str(instance.summary or "").split()) or "Meeting")
        reminders.append(Reminder(
            key=key, title=title, start=start, end=end, minutes=m, later=later,
            provider=str(link.get("provider") or ""),
            provider_label=str(link.get("label") or ""),
        ))
    reminders.sort(key=lambda r: (r.start, r.key))
    return reminders, marks


class ReminderBook:
    """What has been shown and what was silenced, kept on disk.

    Thread-safe: the reminder loop takes what is due while a toast button,
    on its own thread, silences a meeting.
    """

    def __init__(self, path: Union[Path, Callable[[], Path]]) -> None:
        self._path_source = path
        self._lock = threading.Lock()

    def _path(self) -> Path:
        source = self._path_source
        return Path(source() if callable(source) else source)

    # -- disk ----------------------------------------------------------------

    def _load(self) -> dict:
        try:
            with self._path().open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {"sent": {}, "silenced": {}}
        if not isinstance(data, dict):
            data = {}
        return {
            "sent": data.get("sent") if isinstance(data.get("sent"), dict) else {},
            "silenced": data.get("silenced") if isinstance(data.get("silenced"), dict) else {},
        }

    def _save(self, data: dict) -> None:
        path = self._path()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(tmp, path)
        except OSError:
            # A reminder that repeats after a restart is the worst this can do.
            try:
                tmp.unlink()
            except OSError:
                pass

    @staticmethod
    def _prune(data: dict, now: datetime) -> bool:
        changed = False
        for section in ("sent", "silenced"):
            entries = data[section]
            for key in list(entries):
                until = calendar_feed.parse_iso_utc(entries[key])
                if until is None or until < now:
                    del entries[key]
                    changed = True
        return changed

    # -- public --------------------------------------------------------------

    def take_due(self, instances: Iterable, minutes, now: datetime, **kwargs) -> list[Reminder]:
        """The reminders to show now, each remembered so it is shown once."""
        now = calendar_feed.as_utc(now)
        with self._lock:
            data = self._load()
            changed = self._prune(data, now)
            reminders, marks = due(instances, minutes, now, sent=data["sent"].keys(),
                                   silenced=data["silenced"].keys(), **kwargs)
            for mark, end in marks.items():
                data["sent"][mark] = (end + KEEP_AFTER_END).isoformat()
                changed = True
            if changed:
                self._save(data)
        return reminders

    def silence(self, key: str, end: Optional[datetime]) -> None:
        """No more reminders for this occurrence (it ends at ``end``)."""
        key = str(key or "").strip()
        if not key:
            return
        until = calendar_feed.as_utc(end) if end else datetime.now(UTC) + timedelta(hours=6)
        with self._lock:
            data = self._load()
            data["silenced"][key] = (until + KEEP_AFTER_END).isoformat()
            self._save(data)

    def is_silenced(self, key: str) -> bool:
        with self._lock:
            return str(key or "") in self._load()["silenced"]
