"""The Home view (brief section 3.5): This week, Needs attention, Next, People,
Activity, plus the first-run and loading states.

Source-and-render checks only, in the shape of tests/test_navigation_and_dashboard.py:
nothing here starts the server, opens the database, or touches the network.
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


def _read(path):
    return path.read_text(encoding="utf-8")


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    return node


@pytest.fixture(scope="module")
def home_html():
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    return env.get_template("index.html").render(initial_view="home")


@pytest.fixture(scope="module")
def home_js():
    return _read(STATIC / "home.js")


@pytest.fixture(scope="module")
def home_css():
    return _read(STATIC / "home.css")


# ── The partial carries every section root and its accessible list ───────────

SECTION_ROOTS = [
    "dash", "dash-firstrun", "dash-grid",
    "dash-overview", "dash-overview-metrics", "dash-overview-heat",
    "dash-overview-desc",
    "dash-attention", "dash-attention-list", "dash-attention-all",
    "dash-next", "dash-next-body", "dash-next-focus", "dash-next-note",
    "dash-next-all",
    "home-speakers-list",
    "dash-activity", "home-activity-knobs", "home-activity-chart",
    "home-activity-summary", "home-activity-desc",
    "dash-storage", "home-storage-knobs", "home-storage-note", "home-storage-chart",
    "home-storage-detail", "home-storage-summary", "home-storage-free", "home-storage-desc",
]


@pytest.mark.parametrize("element_id", SECTION_ROOTS)
def test_the_partial_has_every_section_root(home_html, element_id):
    assert home_html.count(f'id="{element_id}"') == 1, element_id


def test_the_focusable_heading_survives(home_html):
    assert 'id="view-home-heading"' in home_html


def test_the_overview_and_activity_expose_accessible_text(home_html):
    # The heatmap carries a visually hidden text summary, and so does the
    # histogram (review finding 21).
    assert 'class="visually-hidden" id="dash-overview-desc"' in home_html
    assert 'id="home-activity-desc"' in home_html


def test_the_home_stylesheet_is_linked_once_after_the_shell(home_html):
    assert home_html.count('href="/static/home.css"') == 1
    assert home_html.index("/static/style.css") < home_html.index("/static/home.css")


def test_no_kpi_tiles_or_stat_cards_return(home_html):
    partial = _read(TEMPLATES / "_view_home.html")
    for banned in ("dash-figures", "stat-sessions", "stat-week", "stat-attention",
                   "home-recent-list", "kpi", "hero-number", "dash-low"):
        assert banned not in partial, banned


# ── home.js writes only to ids that exist ────────────────────────────────────

def test_home_js_dashboard_targets_all_exist(home_html, home_js):
    targets = set(re.findall(
        r"getElementById\('((?:dash-|home-activity|home-speakers|home-storage)[^']*)'\)", home_js))
    assert targets, "expected dashboard getElementById targets"
    for element_id in targets:
        assert home_html.count(f'id="{element_id}"') >= 1, element_id


# ── The view renders from the store, never straight from the network ─────────

def test_home_reads_slices_through_appdata(home_js):
    assert "AppData.get('analytics')" in home_js
    assert "AppData.get('sessions')" in home_js
    assert "AppData.get('calendarStatus')" in home_js
    assert "AppData.get('calendarEvents'" in home_js
    assert "AppData.get('storage')" in home_js


def test_home_never_fetches_the_cached_slice_endpoints(home_js):
    for bad in ("fetch('/api/sessions')", "fetch('/api/analytics')",
                "fetch('/api/dashboard')", "fetch('/api/attention",
                "fetch('/api/calendar/events", "fetch('/api/calendar/status"):
        assert bad not in home_js, bad


def test_the_next_agenda_loads_its_range_from_the_store(home_js):
    assert "AppData.load('calendarEvents', { key: _homeWeekRange().rangeKey })" in home_js
    assert "calendarRangeKey(" in home_js
    assert "function _renderNext(" in home_js
    # Next reads today and the next two days from the same one loaded range.
    assert "[0, 1, 2].map(" in home_js


# ── Next: the focus strip, the three columns, the clock ──────────────────────

def test_next_sits_directly_under_the_stat_cards(home_html):
    """Full width, first band after the figures: Next is the only part of the
    page you act on, and everything below it is what already happened."""
    partial = _read(TEMPLATES / "_view_home.html")
    cards = partial.index('id="dash-overview"')
    nxt = partial.index('id="dash-next"')
    hero = partial.index('id="dash-grid"')
    mid = partial.index('id="dash-mid"')
    assert cards < nxt < hero < mid
    # Its own card, not a cell inside a band, which is what makes it full width.
    assert 'class="dash-cell dash-next"' in partial[nxt - 60:nxt]
    assert "dash-low" not in partial


# ── People: talk time reads in days once it passes one ───────────────────────

_NODE = shutil.which("node")


def home_js_text() -> str:
    return _read(STATIC / "home.js")

_DURATION_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const found = src.match(/function _formatDuration\(seconds\) \{[\s\S]*?\n\}/);
if (!found) { throw new Error('FAIL: _formatDuration not found in home.js'); }
eval(found[0]);

function eq(secs, want) {
  const got = _formatDuration(secs);
  if (got !== want) { throw new Error(`FAIL: ${secs}s -> "${got}", wanted "${want}"`); }
}

// The eight-week aggregates that prompted this: 194h 22m of talk time is
// eight days of it, and saying so beats making the reader divide.
eq(194 * 3600 + 22 * 60, '8d 2h 22m');
eq(438 * 3600 + 17 * 60, '18d 6h 17m');

// Under a day is untouched.
eq(23 * 3600 + 42 * 60, '23h 42m');
eq(2 * 3600 + 55 * 60, '2h 55m');
eq(47 * 60, '47m');

// Zero components drop out rather than padding the string.
eq(8 * 86400, '8d');
eq(86400 + 22 * 60, '1d 22m');
eq(7200, '2h');

// A total under a minute reads in seconds instead of a bare "0m", which was
// the one case the old formatter reported as nothing at all.
eq(40, '40s');
eq(1, '1s');

// Nothing is still nothing, and junk does not throw.
eq(0, '0m');
eq(null, '0m');
eq(undefined, '0m');
eq(-5, '0m');

console.log('OK');
"""


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH")
def test_talk_time_breaks_into_days_under_node():
    """_formatDuration is pure, so it is unit tested rather than asserted at.
    home.js cannot be required under node (it touches browser globals at
    module scope), so the harness extracts just this function."""
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "duration.js"
        harness.write_text(_DURATION_HARNESS, encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(harness), str(STATIC / "home.js").replace("\\", "/")],
            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"stdout={result.stdout}\nstderr={result.stderr}")
    assert "OK" in result.stdout


def test_only_the_people_list_uses_that_formatter():
    """Changing it must not have moved another widget's numbers. The charts and
    the agenda have their own formatters with their own rounding."""
    assert home_js_text().count("_formatDuration(") == 2   # the definition + one call
    call = home_js_text()[home_js_text().index("function _renderPeople("):]
    assert "_formatDuration(sp.talk_seconds)" in call[:call.index("/* \u2500\u2500 A. This week")]


def test_a_joinable_meeting_gets_a_join_button(home_js, home_css):
    """A button cannot live inside the row's own link, so a joinable meeting
    wraps the row and everything else keeps the markup it always had."""
    row = home_js[home_js.index("function _homeNextRow("):]
    row = row[:row.index("function _homeNextDayColumn(")]
    assert "calendarJoinButton(e.key, e.join, e.join_label, 'dash-next-join')" in row
    assert 'join ? `<div class="dash-next-item">${row}${join}</div>` : row' in row
    # The join URL is never on this page; only the provider slug arrives.
    assert "join_url" not in home_js
    for selector in (".dash-next-item", ".dash-next-join",
                     ".dash-next-join:hover", ".dash-next-join:focus-visible",
                     ".dash-next-join.is-solid"):
        assert selector in home_css, selector


def test_the_focus_strip_takes_whatever_is_running_then_whatever_is_soonest(home_js):
    """The strip answers "what do I do right now", so a meeting in progress
    beats one still to come, and a multi-hour block never qualifies: a "Focus
    time" spanning the afternoon would hold the strip all afternoon and hide
    the meeting you actually have to join."""
    body = home_js[home_js.index("function _homeNextFocus("):]
    body = body[:body.index("function _homeFocusRunning(")]
    assert "if (e.all_day || !Number.isFinite(start)) continue;" in body
    assert "if (end - start >= _NEXT_BLOCK_MS) continue;" in body
    assert "return running || upcoming || null;" in body
    # Two overlapping meetings: the later start is the one you are in.
    assert "if (!running || start > _nextStart(running)) running = e;" in body
    assert "if (!upcoming || start < _nextStart(upcoming)) upcoming = e;" in body
    assert "_NEXT_BLOCK_MS = 4 * 3600 * 1000" in home_js


def test_the_focus_strip_reads_its_urgency_off_the_event(home_js, home_css):
    focus = home_js[home_js.index("function _renderNextFocus("):]
    focus = focus[:focus.index("function _homeNextRow(")]
    assert "if (recording) { flag = 'Recording now'; cls = 'is-recording'; }" in focus
    assert "else if (running) { flag = 'Happening now'; cls = 'is-now'; }" in focus
    assert "calendarJoinButton(focus.key, focus.join, focus.join_label," in focus
    assert "'dash-next-join is-solid'" in focus
    # A recording behind the meeting is reachable from the strip.
    assert "Open recording" in focus
    for selector in (".next-focus.is-now", ".next-focus.is-recording",
                     ".next-focus-flag", ".next-focus-count"):
        assert selector in home_css, selector


def test_a_countdown_stops_when_it_stops_meaning_anything(home_js):
    body = home_js[home_js.index("function _homeFocusCountdown("):]
    body = body[:body.index("function _renderNextFocus(")]
    assert "return 'ending now';" in body
    assert "return 'starting now';" in body
    assert "if (until > _NEXT_COUNTDOWN_MS) return '';" in body
    assert "_NEXT_COUNTDOWN_MS = 12 * 3600 * 1000" in home_js


def test_every_one_of_the_three_days_gets_a_column(home_js, home_css):
    """Three columns even when a day is empty: skipping one left a ragged
    section that read as broken rather than as a free day."""
    body = home_js[home_js.index("function _renderNext("):]
    body = body[:body.index("/* ── The clock ─")]
    assert "const byDay = new Map(days.map(k => [k, []]));" in body
    assert "days.map(k => _homeNextDayColumn(k, byDay.get(k), range, now))" in body
    col = home_js[home_js.index("function _homeNextDayColumn("):]
    col = col[:col.index("function _homeNextDaySkeleton(")]
    assert "Nothing scheduled" in col
    # The day head carries the count and the total, and today reads louder.
    assert "meeting${evs.length === 1 ? '' : 's'}" in col
    assert "_nextMinutes(e)" in col
    assert "isToday ? ' is-today' : ''" in col
    assert ".dash-next-daymeta" in home_css
    assert ".dash-next-daylabel.is-today" in home_css


def test_the_now_line_only_marks_a_real_boundary(home_js, home_css):
    """A hairline between something finished and something not. At the top or
    the bottom of the column it would mark nothing, and it belongs to today."""
    col = home_js[home_js.index("function _homeNextDayColumn("):]
    col = col[:col.index("function _homeNextDaySkeleton(")]
    assert "let sawPast = false, drawn = !isToday;" in col
    assert "if (!drawn && sawPast && !done) {" in col
    assert ".dash-next-now" in home_css


def test_a_long_day_is_capped_and_hands_off_to_the_calendar(home_js):
    col = home_js[home_js.index("function _homeNextDayColumn("):]
    col = col[:col.index("function _homeNextDaySkeleton(")]
    assert "evs.slice(0, _NEXT_ROWS_PER_DAY)" in col
    assert "_homeNextDayHref(dayKey)" in col
    href = home_js[home_js.index("function _homeNextDayHref("):]
    href = href[:href.index("\n}")]
    # Month and day both, so the panel opens on the right month.
    assert "/calendar?month=" in href and "&amp;day=" in href


def test_next_keeps_a_clock_only_while_home_is_visible(home_js):
    """The countdown, the live flag and the now line all move on their own."""
    start = home_js[home_js.index("function _homeStartNextClock("):]
    start = start[:start.index("function _homeStopNextClock(")]
    assert "if (_homeNextTimer) return;" in start
    assert "if (Views.current !== 'home') return;" in start
    # Past midnight the three days, and the cache key, are different ones.
    assert "AppData.load('calendarEvents', { key: _homeWeekRange().rangeKey });" in start
    life = home_js[home_js.index("Views.register('home', {"):]
    life = life[:life.index("AppData.subscribe(")]
    assert "_homeStartNextClock();" in life
    assert "_homeStopNextClock();" in life


def test_the_agenda_never_flashes_empty_at_a_loading_calendar(home_js):
    body = home_js[home_js.index("function _renderNext("):]
    body = body[:body.index("/* ── The clock ─")]
    assert "_homeNextDaySkeleton(k, range.todayKey)" in body
    assert "if (!events.length && slice === 'error')" in body
    assert "Could not load your calendar." in body
    # A repaint every half minute must not drop focus off a Join button.
    assert "_dashMorph(body," in body


def test_the_agenda_repaint_is_a_keyed_update(home_js):
    assert "_dashMorph(box," in home_js
    assert "_dashMorph(body," in home_js


def test_the_overview_is_derived_from_the_sessions_slice(home_js):
    # The Overview stats and heatmap read the sessions we already hold, never a
    # fresh fetch, and render the weekday x hour grid.
    assert "function _renderOverview(" in home_js
    assert "ov-heat-grid" in home_js
    assert "ov-metric" in home_js


# ── Activity: one chart with knobs that are remembered ───────────────────────

def test_meeting_load_and_activity_are_one_chart(home_html, home_js, home_css):
    """Recorded hours per week over twelve weeks and recorded minutes per day
    over fourteen days were two views of the same numbers. One chart carries
    both, plus meetings and average length, any span, any grouping."""
    assert '<svg class="act-svg"' in home_js
    assert "function _renderActivity(" in home_js
    assert "function _dashDerivedActivity(state)" in home_js
    for gone in ("_renderCadence", "_renderActivityChart", "home-cadence", "cad-svg"):
        assert gone not in home_js, gone
        assert gone not in home_html, gone
        assert gone not in home_css, gone
    # The default view is the old Meeting load: recorded time by week, three months.
    assert "_ACT_DEFAULTS = { measure: 'time', span: '3m', group: 'auto' }" in home_js
    assert "_ACT_MEASURES = [['time', 'Time'], ['count', 'Meetings'], ['avg', 'Avg length']]" in home_js
    assert "['all', 'All', 0]" in home_js and "['month', 'Month']" in home_js
    # It is one card in the hero row, where Meeting load was.
    partial = home_html
    assert partial.index('id="dash-activity"') < partial.index('id="dash-attention"')
    assert partial.index('id="dash-grid"') < partial.index('id="dash-activity"')


def test_the_knobs_persist_in_local_storage_and_survive_bad_values(home_js):
    assert "_ACT_STORE_KEY = 'home-activity-v1'" in home_js
    assert "_STO_STORE_KEY = 'home-storage-v1'" in home_js
    body = home_js[home_js.index("function _homeLoadKnobs("):home_js.index("function _homeSaveKnobs(")]
    assert "localStorage.getItem(key)" in body
    assert "options.some(o => o[0] === saved[k])" in body, "an unknown saved value falls back"
    save = home_js[home_js.index("function _homeSaveKnobs("):home_js.index("/** One segmented control.")]
    assert "localStorage.setItem(key, JSON.stringify(state))" in save
    assert "try {" in save, "storage can throw in a private window; the chart must not"
    # Turning a knob saves and repaints; it never fetches.
    act = home_js[home_js.index("function _actOnKnob("):home_js.index("function _actBucketKey(")]
    assert "_homeSaveKnobs(_ACT_STORE_KEY, _actState)" in act and "fetch(" not in act


def test_a_grouping_that_cannot_be_read_steps_up(home_js):
    body = home_js[home_js.index("function _actResolveUnit("):home_js.index("/** The chart's buckets")]
    assert "_ACT_MAX_BARS" in body
    assert "if (unit === 'day' && days > _ACT_MAX_BARS) unit = 'week';" in body
    assert "if (unit === 'week' && days / 7 > _ACT_MAX_BARS) unit = 'month';" in body


def test_activity_buckets_under_node():
    """The bucketing is pure, so it runs under node against synthetic sessions:
    a week span groups by day with gaps kept, an all-time span with a year of
    data groups by month, and an explicit Day over a year steps up."""
    node = _node()
    js = _read(STATIC / "home.js")
    start = js.index("const _ACT_STORE_KEY")
    end = js.index("function _actValue(")
    helpers = (js[js.index("function _weekStartLocal("):js.index("/** The last `n` weeks")]
               + js[js.index("function _dashDurationSec("):js.index("function _dashHours(")]
               + js[js.index("function _homeLoadKnobs("):js.index("/** Delegated clicks")])
    harness = """
const localStorage = { getItem() { return null; }, setItem() {} };
const escapeHtml = s => String(s);
let _dashSessions = [];
%s
%s
function _renderActivity() {}
const day = 86400000;
const iso = d => new Date(d).toISOString().slice(0, 19);
const now = Date.now();
_dashSessions = [
  { started_at: iso(now - 1 * day), ended_at: iso(now - 1 * day + 3600000) },
  { started_at: iso(now - 1 * day + 1000), ended_at: iso(now - 1 * day + 1800000) },
  { started_at: iso(now - 6 * day), ended_at: iso(now - 6 * day + 1800000) },
  { started_at: iso(now - 300 * day), ended_at: iso(now - 300 * day + 3600000) },
];
const week = _dashDerivedActivity({ measure: 'time', span: '2w', group: 'auto' });
const all = _dashDerivedActivity({ measure: 'time', span: 'all', group: 'auto' });
const forced = _dashDerivedActivity({ measure: 'time', span: 'all', group: 'day' });
const counts = week.buckets.map(b => b.count);
console.log(JSON.stringify({
  weekUnit: week.unit, weekLen: week.buckets.length, weekTotal: counts.reduce((a, b) => a + b, 0),
  yesterday: counts[counts.length - 2], allUnit: all.unit, allCount: all.buckets.reduce((a, b) => a + b.count, 0),
  forcedUnit: forced.unit, firstHasOld: all.buckets[0].count,
}));
""" % (helpers, js[start:end])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "act.js"
        path.write_text(harness, encoding="utf-8")
        out = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    import json
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["weekUnit"] == "day" and got["weekLen"] == 14
    assert got["weekTotal"] == 3 and got["yesterday"] == 2
    assert got["allUnit"] == "month" and got["allCount"] == 4 and got["firstHasOld"] == 1
    assert got["forcedUnit"] == "week", "a day grouping over a year steps up to weeks"


# ── Storage: the card, its knobs and the tool ─────────────────────────────────

def test_storage_sits_where_the_old_activity_histogram_was(home_html):
    partial = _read(TEMPLATES / "_view_home.html")
    mid = partial.index('id="dash-mid"')
    heat = partial.index('id="dash-overview-heat"')
    storage = partial.index('id="dash-storage"')
    people = partial.index('id="home-speakers-widget"')
    assert mid < heat < storage < people
    assert 'onclick="openStorageTool()"' in partial
    assert 'id="storage-tool-overlay"' in home_html and 'id="storage-tool-body"' in home_html


def test_storage_renders_from_its_slice_with_four_views(home_js):
    body = home_js[home_js.index("function _renderStorage("):home_js.index("function _stoRenderType(")]
    assert "AppData.get('storage')" in body and "AppData.status('storage')" in body
    assert "fetch(" not in body
    assert "_STO_VIEWS = [['type', 'By type'], ['meeting', 'By meeting'], ['month', 'By month'], ['folder', 'By folder']]" in home_js
    for fn in ("_stoRenderType", "_stoRenderMeetings", "_stoRenderMonths", "_stoRenderFolders"):
        assert f"function {fn}(" in home_js, fn
    # Details is a toggle knob, remembered with the rest.
    assert "_STO_DEFAULTS = { view: 'type', span: 'all', top: '8', detail: false }" in home_js
    assert "_homeKnobToggle('detail'" in home_js


def test_the_tool_prices_before_it_runs_and_follows_the_job(home_js):
    assert "function openStorageTool(" in home_js and "function closeStorageTool(" in home_js
    assert "fetch('/api/storage/plan'" in home_js
    run = home_js[home_js.index("async function runStorageTool("):home_js.index("async function cancelStorageTool(")]
    assert "fetch('/api/storage/compress'" in run and "method: 'POST'" in run
    assert "src.addEventListener('storage_job'" in home_js
    done = home_js[home_js.index("function _toolOnJobEvent("):]
    assert "AppData.invalidate(['storage'], 'storage_job')" in done
    # Deleting is a decision for each run: never remembered as on.
    assert "out.orphans.enabled = false;" in home_js
    assert "orphans: { enabled: false }" in home_js


def test_the_chosen_meetings_list_sorts_by_newest_or_largest(home_js):
    assert "_TOOL_LIST_SORTS = [['date', 'Newest'], ['size', 'Largest']]" in home_js
    assert "list: { sort: 'date' }" in home_js
    rows = home_js[home_js.index("function _toolSessionRows("):home_js.index("function _toolScopeMode(")]
    assert "if (_tool.options.list.sort === 'size')" in rows
    assert "(sizes.get(b.id) || 0) - (sizes.get(a.id) || 0)" in rows
    assert "function _toolListSort(" in home_js
    # A dialog preference, remembered with the format choices but never sent.
    body = home_js[home_js.index("function _toolRequestBody("):home_js.index("function openStorageTool(")]
    assert "..._tool.options" not in body and "orphans: o.orphans" in body


# ── home.css: tokens only, balanced braces, no dashes ────────────────────────

def test_home_css_uses_tokens_only(home_css):
    stripped = re.sub(r"/\*.*?\*/", "", home_css, flags=re.S)
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", stripped)
    assert not hexes, hexes
    assert "rgba(" not in stripped
    assert "rgb(" not in stripped
    for token in ("--accent", "--fg", "--fg-muted", "--surface2", "--border",
                  "--focus-ring", "--radius-sm"):
        assert f"var({token})" in stripped, token


def test_home_css_braces_balance(home_css):
    stripped = re.sub(r"/\*.*?\*/", "", home_css, flags=re.S)
    assert stripped.count("{") == stripped.count("}"), \
        f"unbalanced braces: {stripped.count('{')} open, {stripped.count('}')} close"


def test_no_em_or_en_dashes_in_the_home_files():
    en_dash, em_dash = chr(0x2013), chr(0x2014)
    for path in (TEMPLATES / "_view_home.html", STATIC / "home.js",
                 STATIC / "home.css"):
        text = _read(path)
        assert en_dash not in text, f"en dash in {path.name}"
        assert em_dash not in text, f"em dash in {path.name}"
