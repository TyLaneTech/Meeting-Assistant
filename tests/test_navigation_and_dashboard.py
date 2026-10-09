"""The app shell: one template, five views, one store, one navigation.

These are source-and-render checks only. Nothing here starts the server, opens
the database, or touches the network. They pin the structure the overhaul
depends on (context/ui-overhaul-2026-09.md sections 3.1 to 3.4).
"""
import json
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

VIEWS = ["home", "calendar", "attention", "speakers", "session"]


@pytest.fixture(scope="module")
def env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
    )


@pytest.fixture(scope="module")
def rendered(env):
    """index.html rendered once per route the server can serve."""
    return {view: env.get_template("index.html").render(initial_view=view)
            for view in VIEWS}


def _read(path):
    return path.read_text(encoding="utf-8")


# ── One template renders every route ─────────────────────────────────────────

def test_the_per_page_templates_are_gone():
    """/, /calendar and /session were three server pages that each reloaded
    everything. There is one shell now."""
    for name in ("home.html", "calendar.html", "_nav.html", "_voice_library.html"):
        assert not (TEMPLATES / name).exists(), f"{name} still on disk"


def test_the_shell_declares_the_rendered_view():
    html = _read(TEMPLATES / "index.html")
    assert '<body data-view="{{ initial_view }}">' in html
    assert 'window.MA_INITIAL_VIEW = "{{ initial_view }}";' in html
    # The router needs the view before app.js runs.
    assert html.index("MA_INITIAL_VIEW") < html.index("/static/app.js")


def test_every_view_root_is_a_sibling_of_the_others(rendered):
    for view in VIEWS:
        html = rendered[view]
        for name in VIEWS:
            assert f'data-view="{name}" id="view-{name}"' in html, (view, name)


def test_exactly_one_view_is_active_per_route(rendered):
    for view in VIEWS:
        html = rendered[view]
        # The server pre-activates exactly the requested view so a script error
        # before boot cannot leave a blank page; the router still owns is-active
        # after boot and reconciles it on every navigation.
        assert html.count("is-active") == 1, (
            f"{view}: the server marks exactly one view active")
        assert f'class="view is-active" data-view="{view}"' in html, (
            f"{view}: the active section is the requested one")
    # The router still owns is-active after boot.
    js = _read(STATIC / "app.js")
    assert "el.classList.toggle('is-active', view === name)" in js


def test_each_view_has_a_focusable_heading(rendered):
    for view in VIEWS:
        assert f'id="view-{view}-heading"' in rendered["home"], view
    assert rendered["home"].count('class="view-heading') == len(VIEWS)


# ── Element ids app.js drives without null guards ────────────────────────────

SHELL_IDS = [
    # navigation and the recordings rail
    "sidebar", "sidebar-resize-handle", "session-list", "sidebar-search-input",
    "sidebar-filter-btn", "sidebar-filter-popover", "sidebar-bulk-bar",
    "attention-control", "attention-count", "recordings-menu", "app-menu",
    # the header
    "view-title", "view-subtitle", "topbar-session-title", "record-btn",
    "record-menu", "refresh-btn", "ask-toggle", "layout-control", "header-search",
    "home-search-input", "home-search-clear", "home-search-results",
    "pane-toggle-transcript", "pane-toggle-summary", "pane-toggle-chat",
    "pane-toggle-notes",
    # capture
    "capture-meters", "capture-meter-desktop", "capture-meter-mic",
    "capture-warning",
    "status-pill", "status-dot", "status-text", "recording-duration",
    "capture-setup-panes", "model-config", "audio-viz-pane",
    "screen-capture-section", "pane-body-audio", "pane-arrow-models",
    "brand-viz-canvas", "upload-audio-btn", "upload-audio-input",
    # the ask rail and the views
    "ask-rail", "global-chat-input", "global-chat-messages", "global-send-btn",
    "home-conv-list", "dash", "dash-attention-list", "cal-grid", "attn-list",
    "fingerprint-profile-list", "fp-tab-profiles",
]


@pytest.mark.parametrize("element_id", SHELL_IDS)
def test_shell_ids_are_present_exactly_once(rendered, element_id):
    for view in VIEWS:
        assert rendered[view].count(f'id="{element_id}"') == 1, \
            f"{element_id} is not present exactly once on /{view}"


def test_no_element_id_is_duplicated_anywhere(rendered):
    ids = re.findall(r'\bid="([^"]+)"', rendered["home"])
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate ids: {sorted(duplicates)}"


# ── The header owns actions; the sidebar owns navigation ─────────────────────

def test_the_header_has_no_power_button(rendered):
    """App lifecycle is not a page action. It lives in the sidebar App menu."""
    for view in VIEWS:
        html = rendered[view]
        assert 'id="power-menu"' not in html, view
        assert "togglePowerMenu()" not in html, view
    header = _read(TEMPLATES / "_header.html")
    assert "fa-power-off" not in header
    assert "power-menu" not in header


def test_app_lifecycle_lives_in_the_sidebar_app_menu(rendered):
    html = rendered["home"]
    assert 'id="app-menu-btn"' in html
    for label in ("Check for updates", "What's new",
                  "Restart Meeting Assistant", "Quit Meeting Assistant"):
        assert label in html, label
    # The update-available state survives under its new id.
    js = _read(STATIC / "app.js")
    assert "getElementById('app-update-item')" in js
    assert "getElementById('app-menu-dot')" in js
    assert "topbar-update-btn" not in js
    assert "power-menu" not in js


def test_the_primary_nav_is_four_routed_links(rendered):
    html = rendered["home"]
    for href in ("/", "/calendar", "/attention", "/speakers"):
        assert f'data-nav href="{href}"' in html, href
    # Needs attention keeps its id and carries the word, not only the count.
    assert 'href="/attention" id="attention-control"' in html
    assert "Needs attention" in html


def test_sidebar_folders_default_to_closed():
    """The persisted set is the folders the user opened; anything else is
    closed. It used to be the other way round, so a fresh browser (or a new
    folder) started fully unfolded."""
    js = _read(STATIC / "app.js")
    assert "const _FOLDER_STATE_KEY = 'ma-folder-open';" in js
    assert "const collapsed = !_sidebarExpanded.has(folder.id);" in js
    assert "_sidebarCollapsed" not in js
    # The old key is dropped, not converted: its folders are closed anyway.
    assert "localStorage.removeItem(_FOLDER_STATE_KEY_LEGACY)" in js
    # Opening a recording still unfolds its folder chain, and a new subfolder
    # opens its parent; both persist through the one save helper.
    reveal = js[js.index("function _revealSessionInSidebar("):js.index("async function createFolder(")]
    assert "_sidebarExpanded.add(cursor.id)" in reveal and "_saveFolderState()" in reveal
    sub = js[js.index("async function createSubfolder("):js.index("refreshSidebar();", js.index("async function createSubfolder("))]
    assert "_sidebarExpanded.add(parentId)" in sub
    assert js.count("_saveFolderState();") == 4


# ── Sidebar date headers coarsen with age ────────────────────────────────────

_GROUP_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('\\nfunction ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { throw new Error('FAIL: ' + name + ' not found in app.js'); }
  return m[0];
}
eval(grab('calendarDaysAgo'));
eval(grab('sessionGroupLabel'));

// Tuesday 8 September 2026, mid-afternoon.
const now = new Date(2026, 8, 8, 15, 20);
const back = n => new Date(2026, 8, 8 - n, 10, 30);

const labels = [];
for (let i = 0; i <= 20; i++) labels.push(sessionGroupLabel(back(i), now));

// A weekday header names the day it holds, and the seven of them are seven
// different days, so no name is used twice.
const weekdays = [];
for (let i = 2; i <= 8; i++) {
  weekdays.push([sessionGroupLabel(back(i), now),
                 back(i).toLocaleDateString(undefined, { weekday: 'long' })]);
}

// The boundaries are calendar days, not 24 hour blocks.
const edges = {
  midnightToday: sessionGroupLabel(new Date(2026, 8, 8, 0, 1), now),
  lateYesterday: sessionGroupLabel(new Date(2026, 8, 7, 23, 59), now),
  future: sessionGroupLabel(new Date(2026, 8, 9, 8, 0), now),
};

// Spring forward: two calendar days that are 47 hours apart still read as two.
const springNow = new Date(2026, 2, 10, 12, 0);
const springThen = new Date(2026, 2, 8, 12, 0);
const dst = {
  shifts: springThen.getTimezoneOffset() !== springNow.getTimezoneOffset(),
  label: sessionGroupLabel(springThen, springNow),
  expect: springThen.toLocaleDateString(undefined, { weekday: 'long' }),
};

const older = new Date(2025, 11, 14, 10, 0);
console.log(JSON.stringify({
  labels, weekdays, edges, dst,
  priorYear: sessionGroupLabel(older, now),
  priorYearBareMonth: older.toLocaleDateString(undefined, { month: 'long' }),
  priorYearWithYear: older.toLocaleDateString(undefined,
    { month: 'long', year: 'numeric' }),
  sameYear: sessionGroupLabel(new Date(2026, 6, 14, 10, 0), now),
  sameYearBareMonth: new Date(2026, 6, 14, 10, 0)
    .toLocaleDateString(undefined, { month: 'long' }),
}));
"""


def test_sidebar_date_headers_go_today_yesterday_weekdays_last_week_months():
    """Today, Yesterday, then a header per day for the seven days before those,
    then Last Week, then a header per month. Months in the current year are
    bare; earlier ones carry the year, which is all that separates one August
    from the next."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "groups.js"
        path.write_text(_GROUP_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(STATIC / "app.js")],
                             capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])

    labels = got["labels"]
    assert labels[0] == "Today"
    assert labels[1] == "Yesterday"
    # Seven consecutive days, seven distinct weekday names, each naming its day.
    assert len(set(labels[2:9])) == 7
    for label, weekday in got["weekdays"]:
        assert label == weekday, (label, weekday)
    assert "Today" not in labels[2:] and "Yesterday" not in labels[2:]
    # The week before that is one group, and then the months start.
    assert labels[9:16] == ["Last Week"] * 7
    assert set(labels[16:]) == {"August"}

    edges = got["edges"]
    assert edges["midnightToday"] == "Today"
    assert edges["lateYesterday"] == "Yesterday"
    # A clock-skewed future stamp sits with today rather than off the top.
    assert edges["future"] == "Today"

    if got["dst"]["shifts"]:
        assert got["dst"]["label"] == got["dst"]["expect"]

    assert got["sameYear"] == got["sameYearBareMonth"]
    assert got["priorYear"] == got["priorYearWithYear"]
    assert got["priorYear"] != got["priorYearBareMonth"]


_ORDER_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('\\nfunction ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { throw new Error('FAIL: ' + name + ' not found in app.js'); }
  return m[0];
}
eval(grab('calendarDaysAgo'));
eval(grab('sessionGroupLabel'));
eval(grab('groupByDate'));

// Four recordings, one to a header, handed over in an order no date sort
// would ever produce (this is what "Longest first" or "Title A to Z" does).
const now = new Date();
const at = back => {
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - back, 12, 0);
  return { id: 'd' + back, started_at: d.toISOString().replace('Z', '') };
};
const jumbled = [at(12), at(45), at(0), at(3)];

const headers = list => [...list.keys()];
console.log(JSON.stringify({
  newestFirst: headers(groupByDate(jumbled)),
  oldestFirst: headers(groupByDate(jumbled, { oldestFirst: true })),
  insideKept: [...groupByDate([at(3), at(0), { id: 'also-today',
    started_at: new Date(now.getFullYear(), now.getMonth(), now.getDate(), 8, 0)
      .toISOString().replace('Z', '') }]).values()]
    .map(items => items.map(s => s.id)),
}));
"""


def test_sidebar_date_headers_stay_in_date_order_under_any_sort():
    """A filter sorted by title or by length hands the recordings over in an
    order that has nothing to do with dates. The headers still have to read
    Today, then the days, then the months."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "order.js"
        path.write_text(_ORDER_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(STATIC / "app.js")],
                             capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])

    assert got["newestFirst"][0] == "Today"
    assert got["newestFirst"][2] == "Last Week"
    assert len(got["newestFirst"]) == 4
    assert got["oldestFirst"] == got["newestFirst"][::-1]
    # Only the headers are reordered. Inside one, the incoming order stands.
    assert got["insideKept"] == [["d0", "also-today"], ["d3"]]

    js = _read(STATIC / "app.js")
    assert "oldestFirst: filterActive && _sidebarFilter.sortBy === 'date_asc'" in js


def test_the_sidebar_groups_ungrouped_recordings_through_one_labeller():
    js = _read(STATIC / "app.js")
    # One place decides a header, and the sidebar goes through it.
    assert js.count("function sessionGroupLabel(") == 1
    assert js.count("sessionGroupLabel(") == 2   # the definition and one caller
    assert "const label = sessionGroupLabel(d, now);" in js
    assert "const groups = groupByDate(ungrouped, {" in js
    # The old three-bucket grouping (Today, Yesterday, This Week, month) is gone.
    assert "'This Week'" not in js
    assert "function dateKey(" not in js


def test_the_sidebar_says_recordings_not_sessions(rendered):
    html = rendered["home"]
    assert ">Recordings</h2>" in html
    assert 'placeholder="Filter recordings' in html
    assert ">Sessions<" not in html


def test_the_recordings_overflow_holds_the_retired_rail_buttons(rendered):
    html = rendered["home"]
    for label in ("New workspace", "New folder", "Select recordings",
                  "Import audio or video", "Import meeting package"):
        assert label in html, label
    assert "Ctrl+N" in html


def test_the_record_button_exists_once_and_lives_in_the_header(rendered):
    header = _read(TEMPLATES / "_header.html")
    assert 'id="record-btn" class="btn btn-record"' in header
    for view in VIEWS:
        assert rendered[view].count('id="record-btn"') == 1, view


def test_record_button_states_are_the_three_the_brief_names():
    js = _read(STATIC / "app.js")
    body = js[js.index("function updateRecordBtn()"):]
    body = body[:body.index("function updateTestBtn()")]
    assert "Preparing recorder" in body
    assert "Stop · " in body
    assert "</span> Record'" in body
    # The dashboard/calendar nav entry is gone, so its branch must be too.
    assert "app-nav-record" not in js
    # Record never resumes by accident.
    start = js[js.index("async function startNewRecording()"):]
    start = start[:start.index("/** The chevron")]
    assert "await newSession();" in start
    resume = js[js.index("async function resumeRecording()"):]
    resume = resume[:resume.index("/* ── App menu")]
    assert "resume: true" in resume


_RECORD_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function need(re, what) {
  const m = src.match(re);
  if (!m) { throw new Error('FAIL: ' + what + ' not found in app.js'); }
  return m;
}
const grab = name => need(new RegExp('\\n(?:async )?function ' + name + '\\([\\s\\S]*?\\n\\}'), name)[0];
const stateLiteral = need(/\nconst state = (\{[\s\S]*?\n\});/, 'state')[1];
const patience = Number(need(/\nconst RECORD_START_PATIENCE_MS = (\d+);/, 'RECORD_START_PATIENCE_MS')[1]);

// The page: enough of each element for the code to run, and what a person
// would see on the button.
function fakeEl() {
  const classes = new Set();
  return {
    innerHTML: '', textContent: '', title: '', value: '', disabled: false, style: {},
    classList: {
      add: (...c) => c.forEach(x => classes.add(x)),
      remove: (...c) => c.forEach(x => classes.delete(x)),
      toggle: (c, force) => {
        const on = force === undefined ? !classes.has(c) : !!force;
        if (on) classes.add(c); else classes.delete(c);
        return on;
      },
      contains: c => classes.has(c),
    },
    setAttribute() {}, removeAttribute() {}, blur() {},
    get parentElement() { return fakeEl(); },
  };
}
let els, requests, toasts, statusLog, timers, now;

// A clock that only moves when told to.
function setTimeoutFake(fn, ms) {
  const id = timers.length + 1;
  timers.push({ id, due: now + (ms || 0), fn });
  return id;
}
function clearTimeoutFake(id) { timers = timers.filter(t => t.id !== id); }
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
async function advance(ms) {
  const until = now + ms;
  for (;;) {
    const t = timers.filter(x => x.due <= until).sort((a, b) => a.due - b.due)[0];
    if (!t) break;
    clearTimeoutFake(t.id);
    now = t.due;
    t.fn();
    await settle();
  }
  now = until;
}

// The server, answered by hand.
function fetchFake(url) {
  return new Promise((resolve, reject) => requests.push({ url: String(url), resolve, reject }));
}
const waiting = url => requests.find(r => r.url === url && !r.done);
async function answer(url, status, body) {
  const r = waiting(url);
  if (!r) { throw new Error('FAIL: nothing is waiting on ' + url); }
  r.done = true;
  r.resolve({ ok: status < 400, status, json: async () => body });
  await settle();
}
async function drop(url) {
  const r = waiting(url);
  if (!r) { throw new Error('FAIL: nothing is waiting on ' + url); }
  r.done = true;
  r.reject(new TypeError('Failed to fetch'));
  await settle();
}

// Every other name the real code reaches for (the meters, the notes editor,
// the router) is a stand-in that does nothing: this page is only the button.
const inert = new Proxy(function () {}, {
  get: (t, k) => (typeof k === 'symbol' || k === 'then') ? undefined
    : (k === 'toString' || k === 'valueOf') ? () => '' : inert,
  apply: () => inert,
  set: () => true,
});
const page = {
  document: { getElementById: id => (els[id] = els[id] || fakeEl()) },
  fetch: fetchFake, setTimeout: setTimeoutFake, clearTimeout: clearTimeoutFake,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  uiToast: t => toasts.push(t.message),
  parseMicSelection: () => ({}),
  RECORD_START_PATIENCE_MS: patience,
};
const scope = new Proxy(page, {
  has: (t, k) => typeof k === 'string' && (k in t || !(k in globalThis)),
  get: (t, k) => (typeof k === 'symbol' ? undefined : (k in t ? t[k] : inert)),
  set: (t, k, v) => { t[k] = v; return true; },
});
// The real functions are compiled inside that scope. Only global names get
// past it, so the compiler leaves through globalThis.
with (scope) {
  globalThis.__compileInPage = function (__code) { return eval('(' + __code + ')'); };
}
const compileInPage = globalThis.__compileInPage;
delete globalThis.__compileInPage;
for (const name of ['updateRecordBtn', '_syncRecordBtnDisabled', '_reanalysisHoldsRecord',
                    '_setRecordStarting', '_reconcileRecordStart', 'toggleRecording',
                    'startNewRecording']) {
  page[name] = compileInPage(grab(name));
}
const realOnStatus = compileInPage(grab('onStatus'));
page.onStatus = d => { statusLog.push(d); return realOnStatus(d); };

// A ready app with nothing on screen, the way a press of Record finds it.
function fresh() {
  els = {}; requests = []; toasts = []; statusLog = []; timers = []; now = 0;
  Object.assign(page, {
    state: eval('(' + stateLiteral + ')'),
    _recordStartTimer: null, _recordStartId: 0, _recordingStartTime: null,
    _durationInterval: null, _notesSessionBound: null, _quietPromptLanding: null,
    _sessionLinks: {},
  });
  page.state.modelReady = true;
  page.state.recordingReady = true;
  page.updateRecordBtn();
}
async function press() { page.toggleRecording(); await settle(); }
function snap() {
  const btn = els['record-btn'];
  return {
    label: btn.innerHTML.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim(),
    disabled: !!btn.disabled,
    red: btn.classList.contains('recording'),
    busy: btn.classList.contains('is-loading'),
    starts: requests.filter(r => r.url === '/api/recording/start').length,
    toasts: toasts.slice(),
  };
}

(async () => {
  const s = {};

  // A slow start that ends the usual way, with the server's status event.
  fresh();
  s.idle = snap();
  await press();
  s.pressed = snap();
  // Every push says recording: false until the devices are open; this is the
  // diarizer finishing its load halfway through the start.
  page.onStatus({ recording: false, model_ready: true, recording_ready: true });
  s.midStartPush = snap();
  await press();
  page.toggleRecording({ start: true });
  await settle();
  s.pressedAgain = snap();
  page.onStatus({ recording: true, session_id: 's1' });
  s.confirmed = snap();
  const pushes = statusLog.length;
  await answer('/api/recording/start', 200, { session_id: 's1', screen_recording: false });
  s.answeredAfterPush = snap();
  s.answerRepeatedThePush = statusLog.length !== pushes;
  s.timersLeft = timers.length;

  // The status event never comes: the answer says the same thing.
  fresh();
  await press();
  await answer('/api/recording/start', 200, { session_id: 's2', screen_recording: true });
  s.answerOnly = snap();
  s.answerOnlyStatus = statusLog[statusLog.length - 1];

  // The server refuses.
  fresh();
  await press();
  await answer('/api/recording/start', 500, { error: 'Could not open the microphone' });
  s.refused = snap();

  // Another window got there first and is already recording.
  fresh();
  await press();
  await answer('/api/recording/start', 400, { error: 'Already recording' });
  s.otherRecordingAsks = !!waiting('/api/status');
  await answer('/api/status', 200, { recording: true, session_id: 's4' });
  s.otherRecording = snap();

  // Another window's start is still running: wait for it, with the patience.
  fresh();
  await press();
  await answer('/api/recording/start', 400, { error: 'Already starting' });
  s.otherStarting = snap();
  await advance(patience - 1);
  s.otherStartingNearlyOut = snap();
  s.askedEarly = !!waiting('/api/status');
  await advance(1);
  await answer('/api/status', 200, { recording: false });
  s.otherStartingGaveUp = snap();

  // A start that never answers gives Record back, and its late answer cannot
  // settle the press that follows.
  fresh();
  await press();
  await advance(patience);
  await answer('/api/status', 200, { recording: false });
  s.hung = snap();
  await press();
  s.retried = snap();
  await answer('/api/recording/start', 500, { error: 'Device wedged' });
  s.staleAnswer = snap();
  await answer('/api/recording/start', 200, { session_id: 's6', screen_recording: false });
  s.retryStarted = snap();

  // No connection at all, and the status check cannot get through either.
  fresh();
  await press();
  await drop('/api/recording/start');
  await drop('/api/status');
  s.unreachable = snap();

  console.log(JSON.stringify(s));
})().catch(e => { console.error((e && e.stack) || e); process.exit(1); });
"""


def test_record_answers_the_press_before_the_server_does():
    """A start runs for seconds on a slow machine (it waits for the last
    meeting's cleanup, then opens the devices), and the button sat on Record
    for all of it: a press looked lost, so people pressed again, and a press
    that landed after the start went in as a Stop. It says Starting... now,
    disabled, from the moment the request goes out, through the status pushes
    that say recording: false until the devices are open, and the request's
    own answer settles it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "record.cjs"
        path.write_text(_RECORD_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(STATIC / "app.js")],
                             capture_output=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    s = json.loads(out.stdout.strip().splitlines()[-1])

    def looks(snap):
        return {k: snap[k] for k in ("label", "disabled", "red", "busy")}

    record = {"label": "Record", "disabled": False, "red": False, "busy": False}
    starting = {"label": "Starting…", "disabled": True, "red": True, "busy": True}
    stop = {"label": "Stop · 0:00", "disabled": False, "red": True, "busy": False}

    assert looks(s["idle"]) == record
    # Answered before the server has said anything.
    assert looks(s["pressed"]) == starting and s["pressed"]["starts"] == 1
    assert looks(s["midStartPush"]) == starting, "a recording: false push put Record back mid-start"
    assert s["pressedAgain"]["starts"] == 1, "a second press sent a second start"
    assert looks(s["confirmed"]) == stop
    assert looks(s["answeredAfterPush"]) == stop and not s["answerRepeatedThePush"]
    assert s["timersLeft"] == 0, "the patience timer outlived the start"
    # The status event was lost: the answer alone gets the button to Stop.
    assert looks(s["answerOnly"]) == stop
    assert s["answerOnlyStatus"] == {"recording": True, "session_id": "s2",
                                     "resumed": False, "screen_recording": True}
    # Refused: Record again, with the server's reason.
    assert looks(s["refused"]) == record
    assert s["refused"]["toasts"] == ["Could not open the microphone"]
    # Another window won the race, and is recording or still starting.
    assert s["otherRecordingAsks"] and looks(s["otherRecording"]) == stop
    assert not s["otherRecording"]["toasts"]
    assert looks(s["otherStarting"]) == starting
    assert looks(s["otherStartingNearlyOut"]) == starting and not s["askedEarly"]
    assert looks(s["otherStartingGaveUp"]) == record
    assert len(s["otherStartingGaveUp"]["toasts"]) == 1
    # Never answered: Record comes back on the server's patience, and the late
    # answer cannot settle the next press.
    assert looks(s["hung"]) == record and len(s["hung"]["toasts"]) == 1
    assert looks(s["retried"]) == starting and s["retried"]["starts"] == 2
    assert looks(s["staleAnswer"]) == starting
    assert s["staleAnswer"]["toasts"] == s["hung"]["toasts"]
    assert looks(s["retryStarted"]) == stop
    # Nothing answers at all.
    assert looks(s["unreachable"]) == record
    assert "not responding" in s["unreachable"]["toasts"][-1]
    # The busy cursor the button vocabulary promises; Record's own disabled
    # rule would otherwise say not-allowed.
    shell = _shell_css()
    rule = shell[shell.index(".btn-record.is-loading:disabled {"):]
    assert "cursor: progress" in rule[:rule.index("}")]


def test_the_capture_meters_live_in_the_header_and_the_strip_is_gone():
    """One bar, not two. Everything the strip carried apart from the meters was
    already on screen: the title is the header title, the clock and Stop are
    the Record button, and "Recording" is the subtitle plus that button's dot."""
    header = _read(TEMPLATES / "_header.html")
    assert 'id="capture-meters"' in header
    assert "capture-strip" not in header
    for gone in ("capture-title", "capture-time", "capture-stop-btn",
                 "capture-live", "capture-dot"):
        assert gone not in header, gone
    # Beside the title, not stranded next to the actions: the title stops
    # growing and the actions take the right edge instead.
    title_at = header.index('class="app-header-title"')
    meters_at = header.index('id="capture-meters"')
    actions_at = header.index('class="app-header-actions"')
    assert title_at < meters_at < actions_at
    shell = _shell_css()
    assert "flex: 0 1 auto" in shell[shell.index(".app-header-title {"):]
    actions = shell[shell.index(".app-header-actions {"):]
    assert "margin-left: auto" in actions[:actions.index("}")]
    # The alert the strip carried is a real one, so it survives the move.
    assert 'id="capture-warning"' in header
    js = _read(STATIC / "app.js")
    assert "capture-strip" not in js
    assert "function _syncCaptureMeters()" in js


def test_the_header_turns_red_while_recording():
    """The strip's red is the header's own now, and every surface and accent
    inside the row is re-mixed against it so nothing reads as a leftover."""
    js = _read(STATIC / "app.js")
    meters = js[js.index("function _syncCaptureMeters()"):]
    meters = meters[:meters.index("function _syncCaptureWarning()")]
    assert "document.body.classList.toggle('is-recording', live)" in meters
    assert "meters.hidden = !live" in meters
    shell = _shell_css()
    recording = shell[shell.index("body.is-recording .app-header {"):]
    recording = recording[:recording.index("/* \u2500\u2500 Ask rail")]
    assert "var(--red)" in recording
    # No accent-blue glyph left stranded on a red row.
    for element in (".pane-toggle-btn", ".home-search-ai", ".home-search-wrap",
                    ".record-group"):
        assert element in recording, element
    assert "var(--accent)" not in recording


def test_notices_sit_in_the_page_instead_of_over_it():
    """Both banners were fixed over the window: the backlog bar across the
    bottom, on top of the chat input, and the capture alert across the top, on
    top of the header and its Stop button."""
    header = _read(TEMPLATES / "_header.html")
    lag_at = header.index('id="transcript-lag"')
    assert header.index('class="app-header-title"') < lag_at < header.index('class="app-header-actions"')
    assert 'onclick="_dismissTranscriptionBacklog()"' in header
    js = _read(STATIC / "app.js")
    assert "transcription-backlog-bar" not in js
    for start, end in (("function _showTranscriptionBacklog(", "function _clearTranscriptionBacklog("),
                       ("function _showCaptureAlert(", "function _clearCaptureAlert(")):
        body = js[js.index(start):js.index(end)]
        assert "position:fixed" not in body, start
        assert "z-index" not in body, start
    alert = js[js.index("function _showCaptureAlert("):js.index("function _clearCaptureAlert(")]
    assert "header.after(bar)" in alert
    # The pill gives way in the row instead of pushing Record off it.
    shell = _shell_css()
    pill = shell[shell.index(".transcript-lag {"):]
    pill = pill[:pill.index("}")]
    assert "flex: 0 1 auto" in pill and "min-width: 0" in pill


_BACKLOG_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('\\nfunction ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { throw new Error('FAIL: ' + name + ' not found in app.js'); }
  return m[0];
}

// The pill and its text: all of the page these functions touch.
function fakeEl() {
  const classes = new Set(['hidden']);
  return {
    textContent: '', title: '',
    classList: { add: c => classes.add(c), remove: c => classes.delete(c),
                 contains: c => classes.has(c) },
  };
}
const els = { 'transcript-lag': fakeEl(), 'transcript-lag-text': fakeEl() };
const document = { getElementById: id => els[id] || null };
const state = { sessionId: 's1', isDrainingBacklog: false };
const _backlogDismissed = new Map();
let _backlogShown = null;
eval(grab('_showTranscriptionBacklog'));
eval(grab('_clearTranscriptionBacklog'));
eval(grab('_dismissTranscriptionBacklog'));

const snap = () => ({
  shown: !els['transcript-lag'].classList.contains('hidden'),
  text: els['transcript-lag-text'].textContent,
  draining: state.isDrainingBacklog,
});
const push = d => { _showTranscriptionBacklog(d); return snap(); };
const dismiss = () => { _dismissTranscriptionBacklog(); return snap(); };

const s = {};
s.behind = push({ session_id: 's1', pending_sec: 130, draining: false });
s.dismissed = dismiss();
s.behindAgain = push({ session_id: 's1', pending_sec: 250, draining: false });
s.dropped = push({ session_id: 's1', pending_sec: 300, draining: false, dropped_chunks: 4 });
s.droppedDismissed = dismiss();
s.droppedAgain = push({ session_id: 's1', pending_sec: 320, draining: false, dropped_chunks: 9 });
s.drainingAfterDismiss = push({ session_id: 's1', pending_sec: 200, draining: true });
s.nextMeeting = push({ session_id: 's2', pending_sec: 95, draining: false });
s.caughtUp = push({ session_id: 's2', pending_sec: 0, draining: false });
s.draining = push({ session_id: 's2', pending_sec: 200, draining: true });
s.finishing = push({ session_id: 's2', pending_sec: 0, draining: true });
s.done = push({ session_id: 's2', pending_sec: 0, draining: false });
console.log(JSON.stringify(s));
"""


def test_the_transcription_backlog_notice_stays_dismissed():
    """The server re-sends the backlog every five seconds, and every push put
    the old bar back, so its x hid it for five seconds at a time. A dismissal
    holds for the rest of that meeting now, the finish after Stop included.
    Only the step up to skipped audio, a new problem, brings it back."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "backlog.js"
        path.write_text(_BACKLOG_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(STATIC / "app.js")],
                             capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    s = json.loads(out.stdout.strip().splitlines()[-1])

    assert s["behind"] == {"shown": True, "text": "Transcript 2 min behind", "draining": False}
    assert not s["dismissed"]["shown"]
    assert not s["behindAgain"]["shown"], "the next push put a dismissed notice back"
    assert s["dropped"]["shown"] and s["dropped"]["text"] == "Transcript is skipping audio"
    assert not s["droppedDismissed"]["shown"]
    assert not s["droppedAgain"]["shown"]
    # Hidden, but the drain is still tracked: live segments depend on it.
    assert not s["drainingAfterDismiss"]["shown"] and s["drainingAfterDismiss"]["draining"]
    # A dismissal belongs to its meeting.
    assert s["nextMeeting"]["shown"] and s["nextMeeting"]["text"] == "Transcript 2 min behind"
    assert not s["caughtUp"]["shown"]
    assert s["draining"] == {"shown": True, "text": "Transcribing the last 3 min", "draining": True}
    assert s["finishing"]["shown"] and s["finishing"]["text"] == "Finishing the transcript"
    assert not s["done"]["shown"] and not s["done"]["draining"]


def test_the_layout_control_keeps_the_pane_toggle_ids():
    header = _read(TEMPLATES / "_header.html")
    for idx, name in enumerate(("transcript", "summary", "chat", "notes")):
        assert f'id="pane-toggle-{name}" onclick="togglePane({idx})"' in header, name


def test_the_column_toggles_are_buttons_not_a_menu():
    """Four always-visible toggles, one per column, instead of a dropdown."""
    header = _read(TEMPLATES / "_header.html")
    assert 'id="layout-menu"' not in header
    assert 'id="layout-btn"' not in header
    group = header[header.index('id="layout-control"'):header.index('id="header-search"')]
    assert group.count('class="pane-toggle-btn') == 4
    assert group.count("aria-pressed=") == 4
    css = _read(STATIC / "style.css")
    assert ".pane-toggle-btn" in css
    js = _read(STATIC / "app.js")
    assert "btn.setAttribute('aria-pressed'" in js
    assert "menu-item-label" not in js[js.index("function _syncToggleButtons"):js.index("function togglePane(")]


# ── The router ───────────────────────────────────────────────────────────────

def test_the_router_and_the_store_are_defined_in_app_js():
    js = _read(STATIC / "app.js")
    assert "const Views = {" in js
    assert "function navigateTo(url, opts)" in js
    assert "const AppData = {" in js
    for hook in ("register(", "show(name, opts)", "current"):
        assert hook in js, hook
    for api in ("get(name, key)", "load(name, opts)", "invalidate(names, reason)",
                "patch(name, fn, key)", "subscribe(names, fn)", "unsubscribe(fn)",
                "lastUpdated(name, key)", "refreshActiveView()"):
        assert api in js, api


def test_every_slice_the_brief_names_exists():
    js = _read(STATIC / "app.js")
    for slice_name in ("sessions", "folders", "analytics", "attention",
                       "calendarStatus", "calendarEvents"):
        assert f"{slice_name}:" in js, slice_name
    for endpoint in ("/api/sessions", "/api/folders", "/api/dashboard",
                     "/api/attention/summary", "/api/calendar/status"):
        assert endpoint in js, endpoint
    assert "/api/calendar/events?start=" in js


def test_a_late_response_can_never_commit_over_a_newer_one():
    js = _read(STATIC / "app.js")
    load = js[js.index("  load(name, opts) {"):js.index("  /** Mark slices out of date.")]
    assert "const token = ++s.token;" in load
    assert "if (token !== s.token) return this.get(name, o.key);" in load
    # A failure keeps the last good data instead of rendering zeros.
    assert "s.status = 'error';" in load
    assert "s.lastGood = payload;" in load


def test_shared_reads_go_through_the_store():
    """A direct fetch of a cached resource is how "switching views reloads
    everything" comes back."""
    js = _read(STATIC / "app.js")
    for name in ("app.js", "home.js", "calendar.js", "attention.js"):
        source = _read(STATIC / name)
        if name == "app.js":
            # The store itself builds the URL from _SLICE_ENDPOINTS.
            source = source.replace("_SLICE_ENDPOINTS = {", "STORE_ENDPOINTS = {")
            body = source[source.index("STORE_ENDPOINTS = {"):]
            body = body[:body.index("}")]
            assert "/api/sessions" in body
            source = source.replace(body, "")
        assert "fetch('/api/sessions')" not in source, name
        assert "fetch('/api/folders')" not in source, name
        assert "fetch('/api/analytics')" not in source, name
    assert "async function refreshSidebar()" in js
    assert "AppData.invalidate(['sessions', 'folders'], 'sidebar')" in js


def test_the_page_flag_that_split_the_app_in_two_is_gone():
    for name in ("app.js", "home.js", "calendar.js", "attention.js"):
        assert "_isHomePage" not in _read(STATIC / name), name
    for template in TEMPLATES.glob("*.html"):
        assert "_isHomePage" not in _read(template), template.name


def test_navigation_never_reloads_the_page():
    """window.location.href on a route is a page load, which is the whole
    problem the shell exists to fix."""
    js = _read(STATIC / "app.js")
    assert "window.location.href = '/session" not in js
    assert "window.location.href = `/session" not in js


def test_only_unmodified_primary_clicks_are_intercepted():
    js = _read(STATIC / "app.js")
    body = js[js.index("function _initRouteLinks()"):]
    body = body[:body.index("/* ── Header:")]
    assert "e.button !== 0" in body
    assert "e.metaKey || e.ctrlKey || e.shiftKey || e.altKey" in body
    assert "a.target" in body and "data-external" in body.replace("dataset.external", "data-external")
    assert "window.addEventListener('popstate'" in body


def test_query_actions_are_consumed_once():
    js = _read(STATIC / "app.js")
    body = js[js.index("function _applyRouteQuery("):]
    body = body[:body.index("/** Intercept only unmodified")]
    for param in ("attention", "settings", "fingerprint", "autostart",
                  "speakers", "quiet_prompt", "workspace"):
        assert f"'{param}'" in body, param
    # _consumeParams keeps every parameter it was not asked to drop.
    consume = js[js.index("function _consumeParams("):]
    consume = consume[:consume.index("function _applyRouteQuery(")]
    assert "new URLSearchParams(location.search)" in consume
    assert "next.delete(k)" in consume


def test_the_recording_state_machine_is_implemented():
    js = _read(STATIC / "app.js")
    # Opening another recording while live asks once, and names the recording.
    load = js[js.index("async function loadSession(sessionId)"):]
    load = load[:load.index("const gen = ++_loadGeneration;")]
    assert "Stop the current recording and open ${label}?" in load
    # Opening the live session returns to its workspace instead of reloading it.
    assert "if (sessionId === state.sessionId) {" in load
    # Stop from any view leaves the view alone and offers a way in.
    saved = js[js.index("function _announceRecordingSaved(sessionId)"):]
    saved = saved[:saved.index("// Auto-open the Cleanup tab")]
    assert "label: 'Open recording'" in saved
    assert "AppData.invalidate(['sessions', 'analytics', 'attention'], 'recording_stop')" in js
    # Popstate never clears a live session.
    query = js[js.index("function _applyRouteQuery("):]
    assert "if (o.popstate && !state.isRecording && state.sessionId) {" in query
    # Reload and reconnect reconcile against the server first.
    assert "fetch('/api/status').then(r => r.json()).then(st => {" in js
    assert "function _reconcileAfterGap(reason)" in js


def test_the_view_switch_is_an_opacity_crossfade_that_can_be_skipped():
    js = _read(STATIC / "app.js")
    show = js[js.index("  show(name, opts) {"):js.index("  _writeHistory(")]
    # Skipped for Back and a repeated selection only, never for the OS's
    # reduced-motion setting: animations always play (the user's call).
    assert "matchMedia" not in show
    assert "if (!repeat && !o.popstate && !o.noFade)" in show
    assert "opacity 90ms linear" in show
    assert "translateY" not in show
    assert "transform:" not in show


def test_the_document_title_names_the_view():
    js = _read(STATIC / "app.js")
    body = js[js.index("  applyTitle(name) {"):js.index("function _syncNavCurrent(")]
    for label in ("Home", "Calendar", "Needs attention", "Speakers"):
        assert f"'{label}'" in body or f": '{label}'" in body, label
    assert "· Meeting Assistant" in body


# ── The views render from the store ──────────────────────────────────────────

def test_each_view_registers_a_lifecycle():
    registrations = {
        "app.js": ["speakers", "session"],
        "home.js": ["home"],
        "calendar.js": ["calendar"],
        "attention.js": ["attention"],
    }
    for name, views in registrations.items():
        js = _read(STATIC / name)
        for view in views:
            assert f"Views.register('{view}'" in js, (name, view)


def test_the_dashboard_renders_from_slices_and_never_fetches_them():
    js = _read(STATIC / "home.js")
    body = js[js.index("function loadAnalytics()"):js.index("function _dashObserveResize()")]
    assert "AppData.get('analytics')" in body
    assert "AppData.get('sessions')" in body
    assert "fetch(" not in body
    # A failed aggregate must not read as "you have nothing".
    assert "if (analytics) empty = (Number(data.total_sessions) || 0) === 0;" in body
    assert "AppData.status('sessions') === 'ready'" in body


def test_the_dashboard_keeps_focus_and_scroll_on_re_render():
    js = _read(STATIC / "home.js")
    assert "morphdom(el, next, { childrenOnly: true })" in js
    assert "_dashMorph(list, html)" in js


def test_the_banned_dashboard_furniture_is_gone():
    """No KPI tiles, and no Recent meetings list duplicating the rail."""
    html = _read(TEMPLATES / "_view_home.html")
    for element_id in ("dash-figures", "dash-figures-note", "stat-sessions",
                       "stat-time", "stat-speakers", "stat-week", "stat-attention",
                       "dash-recent-panel", "home-recent-list"):
        assert element_id not in html, element_id
    js = _read(STATIC / "home.js")
    for fn in ("_renderFigures", "_renderRecentSessions", "_formatCompactNumber"):
        assert fn not in js, fn
    css = _read(STATIC / "style.css")
    for selector in (".dash-figure", ".home-recent-", ".home-widget", ".home-hero"):
        assert selector not in css, selector


def test_the_library_summary_is_a_sentence_in_the_header():
    js = _read(STATIC / "home.js")
    body = js[js.index("function _dashSubtitle(analytics)"):]
    body = body[:body.index("/** Render Home from")]
    assert "meeting${total === 1 ? '' : 's'}" in body
    assert "recorded" in body
    assert "this week." in body
    assert "Views.setTitle('home'" in js


def test_the_attention_queue_and_the_badge_share_one_source():
    js = _read(STATIC / "attention.js")
    assert "AppData.get('sessions')" in js
    assert "s.attention && s.attention.needs" in js
    assert "Views.setTitle('attention'" in js
    assert "speakers=cleanup" in js
    app = _read(STATIC / "app.js")
    assert "function attentionCount()" in app
    assert "AppData.get('attention')" in app


def test_the_calendar_reads_its_range_from_the_store():
    js = _read(STATIC / "calendar.js")
    assert "AppData.load('calendarEvents', { key: range })" in js
    assert "calendarRangeKey(" in js
    assert "new EventSource" not in js
    # The external sync is named differently from the header's Refresh.
    assert "Sync calendar" in js
    assert "'/api/calendar/refresh'" in js


def test_the_calendar_still_converts_naive_utc_the_way_the_sidebar_does():
    js = _read(STATIC / "calendar.js")
    assert "new Date(session.started_at + 'Z')" in js
    assert "new Date(session.ended_at + 'Z')" in js


def test_the_calendar_binds_month_paging_and_escape():
    js = _read(STATIC / "calendar.js")
    assert "'ArrowLeft'" in js and "'ArrowRight'" in js
    assert "'Escape'" in js
    assert "_calShiftMonth" in js
    # Its keyboard handler is inert while another view is on screen.
    assert "if (Views.current !== 'calendar') return;" in js


def test_the_calendar_day_is_addressable():
    js = _read(STATIC / "calendar.js")
    assert "function _calApplyRoute(month, day)" in js
    assert "params.set('month', month)" in js
    assert "params.set('day', day)" in js


def test_the_calendar_grid_does_not_claim_a_role_it_does_not_implement():
    html = _read(TEMPLATES / "_view_calendar.html")
    assert 'role="grid"' not in html
    js = _read(STATIC / "calendar.js")
    assert 'role="gridcell"' not in js
    assert 'aria-label="${escapeHtml(label)}"' in js


def test_the_speakers_view_is_the_voice_library_lifted_out_of_its_overlay():
    html = _read(TEMPLATES / "_view_speakers.html")
    assert 'class="overlay' not in html
    assert 'id="fingerprint-panel-overlay"' not in html
    for element_id in ("fp-tab-profiles", "fp-tab-match", "fp-tab-health",
                       "fp-search-input", "fp-profile-scroll"):
        assert f'id="{element_id}"' in html, element_id
    js = _read(STATIC / "app.js")
    assert "function openFingerprintPanel() {\n  navigateTo('/speakers');" in js
    assert "Views.register('speakers'" in js


def test_one_escaper_and_no_native_dialogs():
    """home.js used to shadow app.js's helpers; in one shell that would take
    over the workspace's own chat and transcript rendering."""
    app = _read(STATIC / "app.js")
    for name in ("home.js", "calendar.js", "attention.js"):
        js = _read(STATIC / name)
        assert "function escapeHtml(" not in js, name
        assert not re.search(r"\b(?:window\.)?(?:alert|confirm|prompt)\(", js), name
    escaper = app[app.index("function escapeHtml(s)"):][:400]
    assert "&quot;" in escaper
    assert "String(s == null ? '' : s)" in escaper


# ── Front door ───────────────────────────────────────────────────────────────

def test_manifest_starts_at_the_dashboard():
    manifest = json.loads(_read(STATIC / "manifest.webmanifest"))
    assert manifest["start_url"] == "/"
    assert {s["url"] for s in manifest["shortcuts"]} == {
        "/session?autostart=1", "/calendar", "/session?attention=needs"}


def test_launcher_opens_the_dashboard():
    vbs = _read(ROOT / "app_launcher.vbs")
    assert 'appUrl    = "http://localhost:" & port & "/"' in vbs
    assert 'EnvPort = "6969"' in vbs
    assert "/session" not in vbs


# ── Theming ──────────────────────────────────────────────────────────────────

SHELL_CSS_MARKER = "   The app shell\n"


def _shell_css():
    css = _read(STATIC / "style.css")
    return css[css.index(SHELL_CSS_MARKER):]


def test_the_semantic_ink_tokens_are_defined_for_dark_and_light():
    css = _read(STATIC / "style.css")
    dark = css[css.index(':root,\n:root[data-theme-mode="dark"] {'):]
    dark = dark[:dark.index("}")]
    light = css[css.index(':root[data-theme-mode="light"] {'):]
    light = light[:light.index("}")]
    for token in ("--on-accent", "--on-green", "--on-red", "--focus-ring"):
        assert token in dark, f"{token} missing from dark"
        assert token in light, f"{token} missing from light"
    # Every accent palette that redefines --accent redefines them too.
    blocks = re.findall(r"(?m)^(:root[^{]*)\{([^}]*)\}", css)
    for selector, body in blocks:
        if "--accent:" not in body:
            continue
        for token in ("--on-accent", "--on-green", "--on-red", "--focus-ring"):
            assert token in body, f"{token} missing from {selector.strip()}"


def test_the_shell_styles_use_tokens_only():
    shell = _shell_css()
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", shell))
    assert not literals, literals
    assert "rgba(" not in shell
    for token in ("--surface", "--surface2", "--border", "--fg", "--fg-muted",
                  "--accent", "--green", "--red", "--yellow", "--font-ui",
                  "--on-accent", "--on-green", "--on-red", "--focus-ring"):
        assert f"var({token})" in shell, token


def test_the_shell_never_dims_text_with_opacity():
    """Text opacity is banned (brief section 4): contrast comes from tokens.
    Opacity on a whole disabled control is the one allowed use."""
    shell = _shell_css()
    for match in re.finditer(r"opacity:\s*([\d.]+)", shell):
        block_start = shell.rfind("{", 0, match.start())
        selector = shell[shell.rfind("}", 0, block_start) + 1:block_start]
        allowed = ("disabled", "view", "@keyframes", "%")
        assert any(token in selector for token in allowed), \
            f"opacity outside a disabled, view or keyframe rule: {selector.strip()!r}"


def test_the_button_vocabulary_has_every_state():
    shell = _shell_css()
    for variant in (".btn-primary", ".btn-secondary", ".btn-quiet",
                    ".btn-record", ".btn-danger"):
        assert variant in shell, variant
    assert ".btn:focus-visible" in shell
    assert ".btn:disabled" in shell
    assert ".btn.is-loading" in shell
    for variant in (".btn-primary", ".btn-secondary", ".btn-quiet", ".btn-record"):
        assert f"{variant}:hover" in shell, f"{variant} has no hover state"


def test_menus_escape_their_container():
    shell = _shell_css()
    menu = shell[shell.index(".menu {"):shell.index(".menu-item {")]
    assert "position: fixed" in menu
    js = _read(STATIC / "app.js")
    assert "function closeMenu(opts)" in js
    assert "document.addEventListener('mousedown', _onMenuOutside, true)" in js
    assert "if (e.key === 'Escape')" in js
    assert "ArrowDown" in js and "ArrowUp" in js
    assert "closeMenu({ restoreFocus: true })" in js


def test_dead_rules_are_gone():
    """Markup these styled no longer exists."""
    css = _read(STATIC / "style.css")
    for selector in (".topbar", ".app-nav", ".power-menu", ".status-pill",
                     ".sidebar-header", ".home-nav-item", ".dash-panel",
                     ".dash-btn", ".home-layout",
                     ".home-chat-area", ".home-recent-indicator",
                     ".home-activity-day", ".upload-nav-btn", ".cal-month"):
        assert selector not in css, selector
    assert "_formatCompactNumber" not in _read(STATIC / "home.js")


def test_stylesheet_braces_balance():
    """A dropped closing brace swallows every rule after it."""
    css = re.sub(r"/\*.*?\*/", "", _read(STATIC / "style.css"), flags=re.S)
    assert css.count("{") == css.count("}"), \
        f"unbalanced braces: {css.count('{')} open, {css.count('}')} close"


def test_the_header_priority_rules_are_container_queries():
    """Breakpoints on the window ignore the resizable sidebar and the Ask rail
    (review finding 15)."""
    shell = _shell_css()
    assert "container: maincol / inline-size" in shell
    assert "@container maincol (max-width: 1249px)" in shell
    assert "@container maincol (max-width: 999px)" in shell
    assert "container: view / inline-size" in shell
    # Record is never one of the controls that collapse.
    for block in re.findall(r"@container maincol[^{]*\{(.*?)\n\}", shell, re.S):
        assert "record" not in block.lower(), block


def test_animations_never_wait_on_the_reduced_motion_setting():
    """Animations always play (the user's call, 2026-10-09): nothing in the
    app switches one off for the OS's reduced-motion setting, and that
    includes the bundled Font Awesome, whose spinners stopped under it, and
    the desktop toasts, which read Windows' "Show animations"."""
    pages = list(TEMPLATES.glob("*.html")) + list(STATIC.glob("*.js")) + list(STATIC.glob("*.css"))
    pages += list((STATIC / "fontAwesome" / "css").glob("*.css"))
    for path in pages:
        text = _read(path)
        assert "prefers-reduced-motion" not in text, path.name
        assert "reduced-motion: reduce" not in text, path.name
    toast_host = _read(STATIC.parents[1] / "ui_desktop" / "toast" / "win32.py")
    animations = toast_host[toast_host.index("    def animations(self)"):]
    animations = animations[:animations.index("\n    def ")]
    assert "return True" in animations and "SystemParametersInfoW" not in animations


def test_no_em_or_en_dashes_in_the_ui():
    for path in list(TEMPLATES.glob("*.html")) + list(STATIC.glob("*.js")):
        text = _read(path)
        assert "\u2013" not in text, f"en dash in {path.name}"
        assert "\u2014" not in text, f"em dash in {path.name}"
