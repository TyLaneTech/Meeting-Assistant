"""The Calendar view: one vocabulary for scheduled meetings and recordings.

Source-and-render checks plus one node unit test of the pairing fold. Nothing
here starts the server or touches the network. These pin the contract in
context/ui-overhaul-2026-09.md section 3.6: the grid renders BOTH recordings and
calendar events, a matched pair renders once, every state carries a label, and
the day panel is a chronological agenda with one action per row.

The existing calendar guards in test_navigation_and_dashboard.py stay; this file
adds the checks the events rebuild needs.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import jinja2
import pytest


ROOT = Path(__file__).parents[1]
TEMPLATES = ROOT / "ui_web/templates"
STATIC = ROOT / "ui_web/static"

CAL_JS = STATIC / "calendar.js"
CAL_CSS = STATIC / "calendar.css"
CAL_HTML = TEMPLATES / "_view_calendar.html"


def _read(path):
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cal_js():
    return _read(CAL_JS)


@pytest.fixture(scope="module")
def cal_css():
    return _read(CAL_CSS)


@pytest.fixture(scope="module")
def rendered():
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    return env.get_template("index.html").render(initial_view="calendar")


# ── The partial carries the roots the view needs ─────────────────────────────

def test_the_partial_has_grid_legend_banner_and_panel_roots():
    html = _read(CAL_HTML)
    for element_id in ("cal-grid", "cal-banner", "cal-sync", "cal-sync-note",
                       "cal-prev", "cal-next", "cal-today",
                       "cal-detail", "cal-detail-title", "cal-detail-body",
                       "cal-detail-close"):
        assert f'id="{element_id}"' in html, element_id
    # The stale-sync banner is a status region that keeps the last data visible.
    assert 'id="cal-banner"' in html and 'role="status"' in html


def test_the_legend_names_the_states_with_real_chip_swatches():
    html = _read(CAL_HTML)
    assert 'class="cal-legend"' in html
    for label in ("Scheduled", "Recorded", "Live", "Not recorded"):
        assert label in html, label
    # The swatches are the actual chip classes, not a separate colour set.
    for variant in ("cal-chip--scheduled", "cal-chip--recorded",
                    "cal-chip--live", "cal-chip--missed"):
        assert variant in html, variant
    # Recorded reads with the recording glyph even in the legend.
    assert "fa-waveform-lines" in html


def test_the_controls_row_is_prev_today_next_and_sync(rendered):
    for element_id in ("cal-prev", "cal-today", "cal-next", "cal-sync"):
        assert f'id="{element_id}"' in rendered, element_id
    assert "Sync calendar" in rendered
    assert "Today" in rendered


# ── calendar.js reads the store, never fetches a cached read ─────────────────

def test_the_calendar_reads_slices_through_the_store(cal_js):
    assert "AppData.load('calendarEvents', { key: range })" in cal_js
    assert "AppData.get('sessions')" in cal_js
    assert "AppData.get('calendarEvents'" in cal_js
    assert "AppData.get('calendarStatus')" in cal_js
    assert "calendarRangeKey(" in cal_js
    # No direct read of a cached resource: that is how "switching views reloads
    # everything" comes back.
    assert "fetch('/api/sessions')" not in cal_js
    assert "fetch('/api/calendar/events" not in cal_js
    assert "new EventSource" not in cal_js
    # The one write that hits the network is the external feed sync.
    assert "'/api/calendar/refresh'" in cal_js
    assert "Sync calendar" in cal_js


def test_the_calendar_converts_naive_utc_the_way_the_sidebar_does(cal_js):
    assert "new Date(session.started_at + 'Z')" in cal_js
    assert "new Date(session.ended_at + 'Z')" in cal_js


def test_the_grid_renders_events_not_only_recordings(cal_js):
    # The bug this rebuild fixes: byDay was built from sessions only. Now the
    # fold takes both, keyed by day, and every event state has a chip.
    assert "calendarBuildItems(" in cal_js
    for kind in ("scheduled", "recorded", "recording", "live", "missed", "allday"):
        assert f"'{kind}'" in cal_js, kind
    # The labels a reader sees, so state never reads by colour alone.
    for label in ("Not recorded", "Recording", "Live", "Tentative"):
        assert label in cal_js, label
    assert "fa-waveform-lines" in cal_js          # the recording glyph
    assert "Private appointment" not in cal_js    # the API supplies that text


def test_the_matched_pair_and_attention_and_allday_are_modelled(cal_js):
    # A secondary amber dot for attention, never the primary encoding.
    assert 'aria-label="Needs attention"' in cal_js
    assert "cal-chip-dot" in cal_js
    # Up to three items, then the "+N more" WITH the plus sign.
    assert "+${hidden} more" in cal_js
    # All-day items sit in their own band.
    assert "cal-day-allday" in cal_js
    assert "cal-allday-chip" in cal_js


def test_the_day_panel_is_an_agenda_with_one_action_per_row(cal_js):
    assert "Nothing recorded or scheduled" in cal_js
    for action in ("Open recording", "Clean up speakers", "Show in Settings"):
        assert action in cal_js, action
    # The three actions are mutually exclusive: the recording ones gate on a
    # session id, the calendar one is the fallback.
    body = cal_js[cal_js.index("function _calAgendaAction("):]
    body = body[:body.index("function _calAgendaRowHtml(")]
    assert "Clean up speakers" in body and "Open recording" in body
    assert "Show in Settings" in body


def test_the_sync_state_and_stale_banner_read_from_calendar_status(cal_js):
    assert "Calendar could not sync:" in cal_js
    assert "Showing the last successful sync." in cal_js
    assert "Connect your calendar" in cal_js
    assert "_timeAgo(" in cal_js


# ── Keyboard and deep links ──────────────────────────────────────────────────

def test_the_calendar_binds_month_paging_and_escape(cal_js):
    assert "'ArrowLeft'" in cal_js and "'ArrowRight'" in cal_js
    assert "'Escape'" in cal_js
    assert "_calShiftMonth" in cal_js
    assert "if (Views.current !== 'calendar') return;" in cal_js


def test_the_calendar_day_is_addressable_through_the_router(cal_js):
    assert "function _calApplyRoute(month, day)" in cal_js
    assert "params.set('month', month)" in cal_js
    assert "params.set('day', day)" in cal_js
    # History is written through the shell router helper, not a raw pushState.
    assert "Views._writeHistory('calendar'" in cal_js
    assert "history.pushState" not in cal_js


def test_the_grid_names_each_day_without_claiming_a_grid_role(cal_js):
    assert 'aria-label="${escapeHtml(label)}"' in cal_js
    assert 'role="gridcell"' not in cal_js
    html = _read(CAL_HTML)
    assert 'role="grid"' not in html


def test_calendar_js_defines_no_native_dialogs_or_second_escaper(cal_js):
    assert "function escapeHtml(" not in cal_js
    assert not re.search(r"\b(?:window\.)?(?:alert|confirm|prompt)\(", cal_js)


# ── calendar.css is tokens only, balanced, and dash free ─────────────────────

def test_calendar_css_uses_tokens_only(cal_css):
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", cal_css))
    assert not literals, f"raw hex in calendar.css: {literals}"
    assert "rgba(" not in cal_css
    assert "rgb(" not in cal_css
    for token in ("--surface2", "--border", "--fg", "--fg-muted", "--green",
                  "--yellow", "--on-green", "--focus-ring", "--radius-sm"):
        assert f"var({token})" in cal_css, token


def test_calendar_css_never_dims_text_with_opacity(cal_css):
    # Text opacity is banned (brief section 4); contrast comes from tokens.
    assert not re.search(r"opacity:\s*0?\.\d+", cal_css)


def test_calendar_css_braces_balance(cal_css):
    stripped = re.sub(r"/\*.*?\*/", "", cal_css, flags=re.S)
    assert stripped.count("{") == stripped.count("}"), (
        f"unbalanced braces: {stripped.count('{')} open, {stripped.count('}')} close")


def test_calendar_css_defines_the_chip_vocabulary(cal_css):
    for selector in (".cal-legend", ".cal-chip--scheduled", ".cal-chip--recorded",
                     ".cal-chip--live", ".cal-chip--missed", ".cal-allday-chip",
                     ".cal-agenda-row", ".cal-agenda-state"):
        assert selector in cal_css, selector


def test_no_dashes_in_the_calendar_files():
    for path in (CAL_JS, CAL_CSS, CAL_HTML):
        text = _read(path)
        assert chr(0x2013) not in text, f"en dash in {path.name}"
        assert chr(0x2014) not in text, f"em dash in {path.name}"


# ── The pairing fold, unit tested under node ─────────────────────────────────

_NODE = shutil.which("node")

_HARNESS = r"""
const path = process.argv[2];
const { calendarBuildItems } = require(path);

function assert(cond, msg) { if (!cond) { throw new Error('FAIL: ' + msg); } }

// A matched pair renders once, as the recorded item; the session is not doubled.
{
  const sessions = [{ id: 's1', title: 'Rec', started_at: '2026-09-03T18:00:00',
                      last_segment_time: 3600, attention: { needs: false } }];
  const events = [{ key: 'e1', title: 'Developer Chat',
                    start: '2026-09-03T18:00:00+00:00', end: '2026-09-03T19:00:00+00:00',
                    all_day: false, private: false, status: 'confirmed',
                    day: '2026-09-03', session_id: 's1', session_title: 'Rec',
                    state: 'recorded' }];
  const items = calendarBuildItems(sessions, events, { enabled: true });
  assert(items.length === 1, 'matched pair yields one item, got ' + items.length);
  assert(items[0].kind === 'recorded', 'pair kind recorded, got ' + items[0].kind);
  assert(items[0].sessionId === 's1', 'pair keeps the session id');
}

// An unmatched session is a Recording item.
{
  const sessions = [{ id: 's2', title: 'Solo', started_at: '2026-09-02T15:00:00',
                      last_segment_time: 600, attention: { needs: false } }];
  const items = calendarBuildItems(sessions, [], { enabled: true });
  assert(items.length === 1, 'one item for a lone session');
  assert(items[0].kind === 'recording', 'lone session is recording, got ' + items[0].kind);
}

// A past event with no recording is Not recorded (missed).
{
  const events = [{ key: 'e2', title: 'Call', start: '2026-08-31T19:30:00+00:00',
                    end: '2026-08-31T20:00:00+00:00', all_day: false, status: 'confirmed',
                    day: '2026-08-31', session_id: null, state: 'missed' }];
  const items = calendarBuildItems([], events, { enabled: true });
  assert(items.length === 1 && items[0].kind === 'missed',
         'past event is missed, got ' + (items[0] && items[0].kind));
}

// A future event is Scheduled.
{
  const events = [{ key: 'e3', title: 'Sync', start: '2026-10-01T14:00:00+00:00',
                    end: '2026-10-01T15:00:00+00:00', all_day: false, status: 'confirmed',
                    day: '2026-10-01', session_id: null, state: 'upcoming' }];
  const items = calendarBuildItems([], events, { enabled: true });
  assert(items.length === 1 && items[0].kind === 'scheduled',
         'future event is scheduled, got ' + (items[0] && items[0].kind));
}

// The currently recording session reads Live.
{
  const sessions = [{ id: 's3', title: 'Now', started_at: '2026-09-03T20:00:00',
                      last_segment_time: 0, attention: { needs: false } }];
  const items = calendarBuildItems(sessions, [], { enabled: true, liveSessionId: 's3' });
  assert(items.length === 1 && items[0].kind === 'live',
         'live session is live, got ' + (items[0] && items[0].kind));
}

// A tentative future event carries the flag; a private event keeps its text.
{
  const events = [{ key: 'e4', title: 'Private appointment', start: '2026-10-02T14:00:00+00:00',
                    end: '2026-10-02T15:00:00+00:00', all_day: false, private: true,
                    status: 'tentative', day: '2026-10-02', session_id: null, state: 'upcoming' }];
  const items = calendarBuildItems([], events, { enabled: true });
  assert(items[0].tentative === true, 'tentative flag set');
  assert(items[0].private === true && items[0].title === 'Private appointment',
         'private title preserved');
}

// With the calendar off, events drop out and only recordings render.
{
  const sessions = [{ id: 's5', title: 'Off', started_at: '2026-09-02T15:00:00',
                      last_segment_time: 600, attention: { needs: false } }];
  const events = [{ key: 'e5', title: 'Hidden', start: '2026-09-02T16:00:00+00:00',
                    end: '2026-09-02T17:00:00+00:00', all_day: false, status: 'confirmed',
                    day: '2026-09-02', session_id: null, state: 'upcoming' }];
  const items = calendarBuildItems(sessions, events, { enabled: false });
  assert(items.length === 1 && items[0].kind === 'recording',
         'calendar off shows recordings only');
}

console.log('OK');
"""


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH")
def test_the_pairing_fold_under_node():
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(_HARNESS, encoding="utf-8")
        js_path = str(CAL_JS).replace("\\", "/")
        result = subprocess.run(
            [_NODE, str(harness), js_path],
            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"node pairing test failed:\nstdout={result.stdout}\nstderr={result.stderr}")
    assert "OK" in result.stdout
