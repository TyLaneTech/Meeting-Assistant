"""Browsing the app while a meeting records.

A recording does not need its meeting on screen. Opening a past meeting used to
stop the recording ("Stop the current recording?"), because the page had one
slot for "the current meeting" and every status push put the live meeting back
into it. The workspace now shows any meeting while the recording carries on in
the background, and the header's Back to live pill returns to it.

These run the page's own functions in Node against stand-ins for the DOM and
the network, the way test_playback_status.py runs onStatus:

  - status pushes bind the workspace to the recording only when it already
    shows it, was empty, or this window started it;
  - the end of a recording acts on the meeting that ended, not the one on
    screen;
  - opening a meeting never stops the recording, posts its summary
    instructions for that meeting only, and the live meeting reopens as the
    live view, with the lines said while it loaded replayed once, in order;
  - chat replies for another meeting stay out of the chat on screen;
  - sound played while recording asks first, once per recording;
  - a window opened with "Open in new window" leaves starts to the app window
    and becomes the app window once it is the last one.

The server half (per-meeting summary instructions, chat events that name
their meeting) is checked against app.py's own code at the bottom.
"""
import ast
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "ui_web/static/app.js"
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")

_PRELUDE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('\\n(?:async )?function ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { throw new Error('FAIL: ' + name + ' not found in app.js'); }
  return m[0];
}
const stateLiteral = src.match(/\nconst state = (\{[\s\S]*?\n\});/)[1];

function fakeEl(id) {
  const classes = new Set();
  return {
    id, innerHTML: '', textContent: '', title: '', className: '', disabled: false,
    hidden: false, style: {}, dataset: {}, value: '',
    classList: { add: (...c) => c.forEach(x => classes.add(x)), remove: (...c) => c.forEach(x => classes.delete(x)),
                 toggle: (c, f) => { const on = f === undefined ? !classes.has(c) : !!f; if (on) classes.add(c); else classes.delete(c); return on; },
                 contains: c => classes.has(c) },
    setAttribute() {}, removeAttribute() {}, blur() {}, appendChild() {}, querySelector() { return null; },
    get parentElement() { return fakeEl(); },
  };
}
// Everything a function reaches for that a test does not provide is a
// stand-in that does nothing.
const inert = new Proxy(function () {}, {
  get: (t, k) => (typeof k === 'symbol' || k === 'then') ? undefined
    : (k === 'toString' || k === 'valueOf') ? () => '' : inert,
  apply: () => inert,
  set: () => true,
});
class FakeMessageEvent { constructor(type, init) { this.type = type; this.data = (init || {}).data; } }

function makePage(extra) {
  const els = {};
  const page = Object.assign({
    document: { getElementById: id => (els[id] = els[id] || fakeEl(id)), body: fakeEl('body') },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    history: { replaceState() {}, pushState() {} },
    MessageEvent: FakeMessageEvent,
    calls: [], requests: [], els,
  }, extra || {});
  if (!page.Views) {
    page.Views = {
      current: 'session',
      show(name, o) { page.Views.current = name; page.calls.push('show:' + ((o && o.url) || name)); },
      applyTitle() {},
    };
  }
  page.state = eval('(' + stateLiteral + ')');
  const scope = new Proxy(page, {
    has: (t, k) => typeof k === 'string' && (k in t || !(k in globalThis)),
    get: (t, k) => (typeof k === 'symbol' ? undefined : (k in t ? t[k] : inert)),
    set: (t, k, v) => { t[k] = v; return true; },
  });
  // Assigned through globalThis: inside `with`, a bare name would land on the page.
  with (scope) { globalThis.__compileInPage = function (__code) { return eval('(' + __code + ')'); }; }
  const compile = globalThis.__compileInPage;
  delete globalThis.__compileInPage;
  page.compile = name => { page[name] = compile(grab(name)); return page[name]; };
  return page;
}
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r)); };
const out = {};
"""


def _run(harness: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "harness.js"
        path.write_text(_PRELUDE + harness, encoding="utf-8")
        res = subprocess.run([node, str(path), str(APP_JS)],
                             capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


# ── Status pushes while a past meeting is open ─────────────────────────────

_STATUS_HARNESS = r"""
function statusPage() {
  const page = makePage({
    _setRecordStarting: on => { page.state.isStartingRecording = !!on; },
    _notesApplyForSession: sid => page.calls.push('notes:' + sid),
    destroyPlayback: () => page.calls.push('destroyPlayback'),
    initPlayback: sid => { page.calls.push('play:' + sid); page._playbackActive = true; },
    initVideo: () => {},
    _announceRecordingSaved: sid => page.calls.push('saved:' + sid),
    _maybeAutoOpenResolution: sid => page.calls.push('resolve:' + sid),
    _playbackActive: false, _durationInterval: null, _notesSessionBound: null,
    _sessionLinks: {}, _liveExtras: { meSpeaker: null, screenRecording: false },
    _soundAllowedFor: null, _quietPromptLanding: null,
    fetch: () => Promise.resolve({ json: async () => ({}) }),
  });
  ['onStatus', '_showingLiveMeeting', '_syncLiveReturnPill'].forEach(page.compile);
  return page;
}
const rec = (sid, extra) => Object.assign({ recording: true, session_id: sid, elapsed_sec: 5 }, extra || {});
const idle = (sid, extra) => Object.assign({ recording: false, session_id: sid, model_ready: true, recording_ready: true }, extra || {});
const pill = page => page.els['live-return-btn'] ? page.els['live-return-btn'].hidden : null;
const snap = page => ({ shown: page.state.sessionId, live: page.state.liveSessionId,
                        past: page.state.isViewingPast, calls: page.calls.slice(), pillHidden: pill(page) });

(async () => {
  // Reading a past meeting when a recording starts elsewhere (auto-detect,
  // another window), then the pushes that follow all through the recording.
  let p = statusPage();
  Object.assign(p.state, { sessionId: 'p1', isViewingPast: true });
  p.onStatus(rec('live'));
  out.pastAtStart = snap(p);
  p.calls = [];
  p.onStatus(rec('live'));
  p.onStatus(rec('live', { elapsed_sec: 60 }));
  out.pastLater = snap(p);
  // The recording stops while the past meeting is still on screen.
  p.calls = [];
  p.onStatus(idle('live', { media_ready: true }));
  await settle();
  out.pastAtStop = snap(p);

  // An empty workspace (a fresh window, or Record pressed: startNewRecording
  // blanks it first) takes the recording.
  p = statusPage();
  p.onStatus(rec('live'));
  out.emptyFollows = snap(p);
  // ...and when it stops on screen, everything happens over it.
  p.calls = [];
  p.onStatus(idle('live', { media_ready: true }));
  await settle();
  out.liveAtStop = snap(p);

  // Resume: the meeting on screen is the one that starts recording again.
  p = statusPage();
  Object.assign(p.state, { sessionId: 'p1', isViewingPast: true, isStartingRecording: true });
  p.onStatus(rec('p1', { resumed: true }));
  out.resumeFollows = snap(p);

  // On Home with a recording running: the pill shows there too.
  p = statusPage();
  p.Views.current = 'home';
  p.onStatus(rec('live'));
  out.homePill = snap(p);
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_status_pushes_leave_an_open_meeting_on_screen():
    got = _run(_STATUS_HARNESS)

    start = got["pastAtStart"]
    assert start["shown"] == "p1" and start["past"] is True
    assert start["live"] == "live"
    assert "destroyPlayback" not in start["calls"]
    assert "notes:live" not in start["calls"]
    assert start["pillHidden"] is False, "Back to live shows while another meeting is open"

    later = got["pastLater"]
    assert later["shown"] == "p1" and later["past"] is True and later["live"] == "live"
    assert later["calls"] == []

    stop = got["pastAtStop"]
    assert stop["shown"] == "p1" and stop["live"] is None
    assert "saved:live" in stop["calls"], "the stop is announced for the meeting that ended"
    assert not any(c.startswith("resolve:") for c in stop["calls"]), \
        "speaker clean-up never opens over the meeting being read"
    assert "play:p1" not in stop["calls"]
    assert stop["pillHidden"] is True


def test_an_empty_workspace_or_this_windows_start_takes_the_recording():
    got = _run(_STATUS_HARNESS)

    empty = got["emptyFollows"]
    assert empty["shown"] == "live" and empty["past"] is False and empty["live"] == "live"
    assert "notes:live" in empty["calls"] and "destroyPlayback" in empty["calls"]
    assert empty["pillHidden"] is True

    stop = got["liveAtStop"]
    assert stop["shown"] == "live" and stop["past"] is True and stop["live"] is None
    assert "saved:live" in stop["calls"]
    assert "resolve:live" in stop["calls"]
    assert "play:live" in stop["calls"]

    resume = got["resumeFollows"]
    assert resume["shown"] == "p1" and resume["past"] is False and resume["live"] == "p1"

    home = got["homePill"]
    assert home["live"] == "live" and home["pillHidden"] is False


# ── Opening meetings while recording, and going back to the live one ───────

_LOAD_HARNESS = r"""
const listeners = {};
class FakeEventSource {
  constructor(url) { this.url = url; }
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); }
  dispatchEvent(ev) { (listeners[ev.type] || []).forEach(fn => fn(ev)); return true; }
  close() {}
}
const db = {
  p1: { segments: [{ id: 1, text: 'old', start_time: 0, end_time: 1 }], has_audio: true, has_video: false, chat_messages: [] },
  live: { segments: [1, 2, 3].map(i => ({ id: i, text: 's' + i, start_time: i, end_time: i + 1 })),
          has_audio: true, has_video: false, chat_messages: [] },
};
let release = null;
const page = makePage({
  EventSource: FakeEventSource,
  _sseSource: null,
  _loadGeneration: 0, _liveCatchup: null, _lastLiveSegId: 12,
  _cleanupState: null, _notesDirty: false, _notesSaveTimer: null,
  _sidebarAllSessions: [], _summaryStreams: {}, _sessionLinks: {},
  _pendingSearchHighlight: null, _bulkLoading: false,
  _liveExtras: { meSpeaker: { global_id: 'g-me', name: 'Ty' }, screenRecording: false },
  _bindRecordingCommand: () => {},
  clearAll: () => { page._lastLiveSegId = 0; page.calls.push('clearAll'); },
  appendTranscript: (text, source, st, et, id) => page.calls.push('line:' + id),
  initPlayback: sid => page.calls.push('play:' + sid),
  initVideo: sid => page.calls.push('video:' + sid),
  createAssistantBubble: () => { page.calls.push('bubble'); return null; },
  _notesApplyForSession: sid => page.calls.push('notes:' + sid),
  _stopScreenPreview: () => page.calls.push('stopPreview'),
  fetch: (url, opts) => {
    page.requests.push({ url, method: (opts && opts.method) || 'GET', body: (opts && opts.body) || null });
    const m = String(url).match(/^\/api\/sessions\/([^/]+)$/);
    if (m) {
      const id = m[1];
      const ready = id === 'live' ? new Promise(r => { release = r; }) : Promise.resolve();
      return ready.then(() => ({ json: async () => db[id] || { error: 'missing' } }));
    }
    return Promise.resolve({ json: async () => ({}) });
  },
});
['connectSSE', 'loadSession', 'showLiveMeeting', '_flushLiveCatchup',
 '_showingLiveMeeting', '_syncLiveReturnPill'].forEach(page.compile);
page.connectSSE(0);
const fire = (type, d) => (listeners[type] || []).forEach(fn => fn({ data: JSON.stringify(d) }));

(async () => {
  // Recording, with the live meeting on screen.
  Object.assign(page.state, { isRecording: true, liveSessionId: 'live', sessionId: 'live', isViewingPast: false });

  // Open a past meeting.
  await page.loadSession('p1');
  await settle();
  out.openPast = {
    shown: page.state.sessionId, past: page.state.isViewingPast, live: page.state.liveSessionId,
    stopped: page.requests.some(r => r.url.includes('/api/recording/stop')),
    prompt: page.requests.filter(r => r.url === '/api/custom-prompt').map(r => JSON.parse(r.body)),
    calls: page.calls.slice(),
  };
  // A live line arrives while the past meeting is open: it stays out of it.
  page.calls = [];
  fire('transcript', { session_id: 'live', seg_id: 13, text: 'live talk', source: 'Speaker 1' });
  // A chat reply for the live meeting streams meanwhile: not into this chat.
  fire('chat_start', { session_id: 'live', request_id: 'r1', question: 'q' });
  out.whilePast = page.calls.slice();
  page.calls = [];
  fire('chat_start', { session_id: 'p1', request_id: 'r2', question: 'q' });
  out.ownChat = page.calls.slice();

  // Back to live, with lines arriving while it loads: 3 is already in the
  // database read, 4 is new, and line 2's text is revised.
  page.calls = []; page.requests = [];
  const back = page.showLiveMeeting();
  await settle();
  fire('transcript', { session_id: 'live', seg_id: 3, text: 's3', source: 'Speaker 1' });
  fire('transcript', { session_id: 'live', seg_id: 4, text: 's4', source: 'Speaker 1' });
  fire('transcript_update', { session_id: 'live', seg_id: 2, text: 's2 revised' });
  out.heldDuringLoad = page.calls.filter(c => c.startsWith('line:'));
  release();
  await back;
  await settle();
  out.backToLive = {
    shown: page.state.sessionId, past: page.state.isViewingPast,
    lines: page.calls.filter(c => c.startsWith('line:')),
    played: page.calls.filter(c => c.startsWith('play:') || c.startsWith('video:')),
    lastId: page._lastLiveSegId, held: page._liveCatchup,
    me: page._sessionLinks.me || null,
  };
  // A live line after that appends as normal.
  page.calls = [];
  fire('transcript', { session_id: 'live', seg_id: 5, text: 's5', source: 'Speaker 1' });
  out.afterBack = page.calls.filter(c => c.startsWith('line:'));

  // Opening the live meeting from a list goes through the live view too.
  await page.loadSession('p1');
  await settle();
  page.calls = [];
  const viaList = page.loadSession('live');
  await settle();
  release();
  await viaList;
  await settle();
  out.viaList = { shown: page.state.sessionId, past: page.state.isViewingPast,
                  played: page.calls.filter(c => c.startsWith('play:')) };
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_opening_a_meeting_never_stops_the_recording():
    got = _run(_LOAD_HARNESS)

    past = got["openPast"]
    assert past["stopped"] is False
    assert past["shown"] == "p1" and past["past"] is True and past["live"] == "live"
    assert "play:p1" in past["calls"]
    # Its summary instructions are posted for it alone, never as the
    # recording's (app.py keeps them per meeting).
    assert past["prompt"] and all(p.get("session_id") == "p1" for p in past["prompt"])

    assert got["whilePast"] == [], "neither live lines nor the live meeting's chat land in it"
    assert got["ownChat"] == ["bubble"]


def test_back_to_live_replays_what_was_said_while_it_loaded():
    got = _run(_LOAD_HARNESS)

    assert got["heldDuringLoad"] == [], "nothing is drawn before the transcript is"
    back = got["backToLive"]
    assert back["shown"] == "live" and back["past"] is False
    # The database read, then only the line it did not have, once.
    assert back["lines"] == ["line:1", "line:2", "line:3", "line:4"]
    assert back["played"] == [], "a meeting still recording gets no playback"
    assert back["lastId"] == 4
    assert back["held"] is None
    assert back["me"] == {"global_id": "g-me", "name": "Ty"}

    assert got["afterBack"] == ["line:5"]
    via = got["viaList"]
    assert via["shown"] == "live" and via["past"] is False and via["played"] == []


# ── Sound while recording ──────────────────────────────────────────────────

_SOUND_HARNESS = r"""
function fakeAudio(id) {
  const handlers = {};
  return {
    id, dataset: {}, plays: 0, pauses: 0,
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
    play() { this.plays += 1; (handlers.play || []).forEach(fn => fn()); return Promise.resolve(); },
    pause() { this.pauses += 1; },
  };
}
let answers = [];
let asked = 0;
const page = makePage({
  _soundAllowedFor: null, _soundAsk: null,
  uiConfirm: () => { asked += 1; return Promise.resolve(answers.shift()); },
});
page.compile('_guardSoundWhileRecording');

(async () => {
  const bar = fakeAudio('playback-audio');
  const sample = fakeAudio('');
  page._guardSoundWhileRecording(bar);
  page._guardSoundWhileRecording(bar);       // a second guard is not added
  page._guardSoundWhileRecording(sample);

  bar.play();
  out.idle = { plays: bar.plays, pauses: bar.pauses, asked };

  Object.assign(page.state, { isRecording: true, liveSessionId: 'live' });
  answers = [false];
  bar.play(); bar.play();                     // two presses, one question
  await settle();
  out.declined = { plays: bar.plays, pauses: bar.pauses, asked };

  answers = [true];
  bar.play();
  await settle();
  out.allowed = { plays: bar.plays, pauses: bar.pauses, asked, allowedFor: page._soundAllowedFor };
  sample.play();
  out.sampleAfterYes = { plays: sample.plays, pauses: sample.pauses, asked };

  // The next recording asks again.
  page.state.liveSessionId = 'live2';
  answers = [true];
  sample.play();
  await settle();
  out.nextRecording = { plays: sample.plays, pauses: sample.pauses, asked };
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_sound_while_recording_asks_once_per_recording():
    got = _run(_SOUND_HARNESS)

    assert got["idle"] == {"plays": 1, "pauses": 0, "asked": 0}
    declined = got["declined"]
    assert declined["asked"] == 1, "two presses ask once"
    assert declined["pauses"] == 2 and declined["plays"] == 3

    allowed = got["allowed"]
    assert allowed["asked"] == 2 and allowed["allowedFor"] == "live"
    # Paused while asking, then played again after the yes.
    assert allowed["pauses"] == 3 and allowed["plays"] == 5

    assert got["sampleAfterYes"] == {"plays": 1, "pauses": 0, "asked": 2}
    nxt = got["nextRecording"]
    assert nxt["asked"] == 3
    # A voice sample is not restarted for it: its own queue ended with the pause.
    assert nxt["plays"] == 2 and nxt["pauses"] == 1


# ── Windows opened with "Open in new window" ───────────────────────────────

_VIEWER_HARNESS = r"""
const posted = [];
let now = 1000;
const store = { 'ma-viewer-window': '1' };
const page = makePage({
  _viewerWindow: true,
  _windowChannel: { postMessage: m => posted.push(m.kind) },
  _appWindowHeardAt: 0, _appWindowAskedAt: 0, _appWindowMisses: 0,
  _VIEWER_KEY: 'ma-viewer-window',
  sessionStorage: { getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; } },
  Date: { now: () => now },
  _lastRecordingCommandNonce: null,
});
['_checkForAppWindow', '_becomeAppWindow', '_announceAppWindow', '_bindRecordingCommand'].forEach(page.compile);

// The app window answers every ask.
for (let i = 0; i < 4; i++) {
  page._checkForAppWindow();
  now += 50; page._appWindowHeardAt = now;   // its answer arrives
  now += 3000;
}
out.answered = { viewer: page._viewerWindow, asks: posted.filter(k => k === 'who-is-there').length };

// A start command while the app window is there: no ack from a viewer.
const listeners = {};
const fakeSrc = { addEventListener: (t, fn) => { listeners[t] = fn; } };
page.fetch = (url) => { page.requests.push(url); return Promise.resolve({ json: async () => ({ ok: true }) }); };
page._bindRecordingCommand(fakeSrc);
listeners['recording_command']({ data: JSON.stringify({ action: 'start', nonce: 'n1' }) });
out.viewerAcks = page.requests.filter(u => String(u).includes('ack_command')).length;

// The app window closes: two unanswered asks and the viewer takes over.
page._checkForAppWindow(); now += 3000;
out.afterOneMiss = page._viewerWindow;
page._checkForAppWindow(); now += 3000;
page._checkForAppWindow(); now += 3000;
out.promoted = { viewer: page._viewerWindow, flag: store['ma-viewer-window'] || null,
                 announced: posted.includes('app-window') };
listeners['recording_command']({ data: JSON.stringify({ action: 'start', nonce: 'n2' }) });
out.appAcks = page.requests.filter(u => String(u).includes('ack_command')).length;
console.log(JSON.stringify(out));
"""


def test_a_new_window_leaves_the_apps_jobs_to_the_app_window_until_it_is_the_last():
    got = _run(_VIEWER_HARNESS)
    assert got["answered"]["viewer"] is True
    assert got["answered"]["asks"] == 4
    assert got["viewerAcks"] == 0
    assert got["afterOneMiss"] is True, "one missed answer is not enough"
    promoted = got["promoted"]
    assert promoted["viewer"] is False and promoted["flag"] is None
    assert promoted["announced"] is True
    assert got["appAcks"] == 1


def test_a_new_windows_title_and_routes_keep_it_out_of_the_app_windows_way():
    js = APP_JS.read_text(encoding="utf-8")
    apply = js[js.index("  applyTitle(name) {"):js.index("_syncRefreshTooltip", js.index("  applyTitle(name) {"))]
    assert "document.title += ' (new window)'" in apply
    nav = js[js.index("src.addEventListener('navigate'"):js.index("src.addEventListener('capture_alert'")]
    assert "if (_viewerWindow) return;" in nav
    menu = js[js.index("function _openSessionMenu("):js.index("function _openSessionMenu(") + 1200]
    assert "Open in new window" in menu and "openMeetingInNewWindow(s.id)" in menu
    opener = js[js.index("function openMeetingInNewWindow("):js.index("function openMeetingInNewWindow(") + 900]
    assert "&window=viewer" in opener


def test_the_new_windows_title_is_not_taken_for_the_app_window():
    from core import window_focus
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert not window_focus.window_matches(
        "Weekly sync \u00b7 Meeting Assistant (new window)", "Chrome_WidgetWin_1", chrome)
    assert window_focus.window_matches(
        "Weekly sync \u00b7 Meeting Assistant", "Chrome_WidgetWin_1", chrome)


# ── The recording's notices end with it ────────────────────────────────────

_WARNING_HARNESS = r"""
let now = 10 * 60 * 1000;
const timers = [];
const page = makePage({
  Date: { now: () => now },
  setInterval: (fn, ms) => { timers.push(fn); return timers.length; },
  clearInterval: id => { timers[id - 1] = null; },
  _captureWarnTimer: null, _captureLastDesktopAudio: 0, _sidebarAllSessions: [],
});
['_syncCaptureMeters', '_syncCaptureWarning'].forEach(page.compile);
const warn = () => { const el = page.els['capture-warning']; return { text: el.textContent, hidden: el.classList.contains('hidden') }; };

Object.assign(page.state, { isRecording: true, liveSessionId: 'live', sessionId: 'live' });
page._syncCaptureMeters();
now += 90 * 1000;                      // a silent desktop side for a minute and a half
timers.forEach(fn => fn && fn());
out.during = warn();

page.state.isRecording = false;        // the recording stops
page._syncCaptureMeters();
out.afterStop = warn();
out.timerLeft = timers.filter(Boolean).length;
console.log(JSON.stringify(out));
"""


def test_the_no_desktop_audio_notice_goes_when_the_recording_stops():
    got = _run(_WARNING_HARNESS)
    assert got["during"] == {"text": "No desktop audio for a minute", "hidden": False}
    assert got["afterStop"]["hidden"] is True
    assert got["timerLeft"] == 0


# ── Server: per-meeting summary instructions, chat events name their meeting ─

class _Req:
    def __init__(self, method, body=None, args=None):
        self.method = method
        self._body = body or {}
        self.args = args or {}

    def get_json(self, silent=False):
        return self._body


def _prompt_scope():
    tree = ast.parse(APP_PY)
    wanted = {"_custom_prompt_for", "custom_prompt_endpoint"}
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted:
            n.decorator_list = []
            nodes.append(n)
    assert {n.name for n in nodes} == wanted
    scope = {"_state": {"custom_prompt": ""}, "_state_lock": threading.Lock(),
             "_custom_prompts": {}, "jsonify": lambda d: d}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<app.custom_prompt>", "exec"), scope)
    return scope


def test_summary_instructions_are_kept_per_meeting():
    s = _prompt_scope()
    ep = s["custom_prompt_endpoint"]

    # Typed before a recording starts (a blank workspace): the default.
    s["request"] = _Req("POST", {"custom_prompt": "Focus on decisions"})
    ep()
    assert s["_custom_prompt_for"]("live") == "Focus on decisions"
    # The live meeting's own, then a past meeting opened while it records.
    s["request"] = _Req("POST", {"custom_prompt": "Live notes", "session_id": "live"})
    ep()
    s["request"] = _Req("POST", {"custom_prompt": "Old meeting's", "session_id": "p1"})
    ep()
    assert s["_custom_prompt_for"]("live") == "Live notes"
    assert s["_custom_prompt_for"]("p1") == "Old meeting's"
    assert s["_custom_prompt_for"](None) == "Focus on decisions"
    s["request"] = _Req("GET", args={"session_id": "p1"})
    assert ep() == {"custom_prompt": "Old meeting's"}


def test_every_summary_and_chat_call_reads_its_own_meetings_instructions():
    code = [ln for ln in APP_PY.splitlines() if not ln.lstrip().startswith("#")]
    uses = [ln.strip() for ln in code if '_state["custom_prompt"]' in ln]
    # _custom_prompt_for's fallback, and the endpoint setting the default.
    assert uses == ['return _state["custom_prompt"]', '_state["custom_prompt"] = text'], uses


def test_chat_events_name_their_meeting():
    tree = ast.parse(APP_PY)
    chat = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "chat")
    pushes = []
    for node in ast.walk(chat):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_push"
                and node.args and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).startswith("chat_")):
            keys = {k.value for k in node.args[1].keys if isinstance(k, ast.Constant)}
            pushes.append((node.args[0].value, keys))
    names = {p[0] for p in pushes}
    assert {"chat_start", "chat_chunk", "chat_tool_event", "chat_done"} <= names
    for name, keys in pushes:
        assert "session_id" in keys, name
