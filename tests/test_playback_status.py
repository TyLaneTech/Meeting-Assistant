"""A status push must never restart playback that is already running.

onStatus() reloaded the meeting's audio and video (initPlayback, initVideo) on
every status event that said recording: false while a past meeting was open.
Status arrives for more than stops: the SSE stream replays it on every
reconnect, _reconcileAfterGap() fetches it when the window comes back into
focus after a minute away, and a settings change pushes it. Each one sent the
audio and the video back to 0:00 in the middle of playback, with the playhead
left where it was (2026-10-02). Playback now loads only for a meeting that has
just stopped on this page: on the server's push that says its media is final
(media_ready), or on a push that names it while playback is not set up yet.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
APP_JS = ROOT / "ui_web/static/app.js"
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('\\n(?:async )?function ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { throw new Error('FAIL: ' + name + ' not found in app.js'); }
  return m[0];
}
const stateLiteral = src.match(/\nconst state = (\{[\s\S]*?\n\});/)[1];

function fakeEl() {
  const classes = new Set();
  return {
    innerHTML: '', textContent: '', title: '', className: '', disabled: false, style: {}, dataset: {},
    classList: { add: (...c) => c.forEach(x => classes.add(x)), remove: (...c) => c.forEach(x => classes.delete(x)),
                 toggle: (c, f) => { const on = f === undefined ? !classes.has(c) : !!f; if (on) classes.add(c); else classes.delete(c); return on; },
                 contains: c => classes.has(c) },
    setAttribute() {}, removeAttribute() {}, blur() {},
    get parentElement() { return fakeEl(); },
  };
}
const els = {};
let calls = [];
// Everything else onStatus reaches for (meters, notes, the record button) is a
// stand-in that does nothing: what is being watched is playback.
const inert = new Proxy(function () {}, {
  get: (t, k) => (typeof k === 'symbol' || k === 'then') ? undefined
    : (k === 'toString' || k === 'valueOf') ? () => '' : inert,
  apply: () => inert,
  set: () => true,
});
const page = {
  document: { getElementById: id => (els[id] = els[id] || fakeEl()) },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  history: { replaceState() {} },
  Views: { current: 'session' },
  fetch: () => Promise.resolve({ json: async () => ({ has_video: true, video_offset: 0 }) }),
  initPlayback: sid => { calls.push('audio:' + sid); page._playbackActive = true; },
  initVideo: sid => { calls.push('video:' + sid); },
  destroyPlayback: () => { page._playbackActive = false; },
  _playbackActive: false,
};
const scope = new Proxy(page, {
  has: (t, k) => typeof k === 'string' && (k in t || !(k in globalThis)),
  get: (t, k) => (typeof k === 'symbol' ? undefined : (k in t ? t[k] : inert)),
  set: (t, k, v) => { t[k] = v; return true; },
});
with (scope) {
  globalThis.__compileInPage = function (__code) { return eval('(' + __code + ')'); };
}
const compileInPage = globalThis.__compileInPage;
delete globalThis.__compileInPage;
page.onStatus = compileInPage(grab('onStatus'));

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
function fresh(sessionId, playing, recording) {
  calls = [];
  page.state = eval('(' + stateLiteral + ')');
  Object.assign(page.state, { sessionId, isViewingPast: !recording, isRecording: !!recording });
  page._playbackActive = playing;
}
// What the server sends while nothing records: session_id is its last session.
const idle = (sid, extra) => Object.assign(
  { recording: false, session_id: sid, model_ready: true, recording_ready: true }, extra || {});

(async () => {
  const s = {};
  // Playing a past meeting that is also the server's last session, when status
  // arrives (the window refocused after a minute, an SSE reconnect).
  fresh('m1', true, false);
  page.onStatus(idle('m1'));
  await settle();
  s.pushSameMeeting = calls.slice();
  // The same, with the server's last session being some other meeting.
  fresh('m1', true, false);
  page.onStatus(idle('m0'));
  await settle();
  s.pushOtherMeeting = calls.slice();
  // Another window's recording stops while this one plays an old meeting.
  fresh('m1', true, false);
  page.onStatus(idle('m2', { media_ready: true }));
  await settle();
  s.otherWindowStopped = calls.slice();

  // This page shows the live recording, and it stops.
  fresh('live', false, true);
  page.onStatus({ recording: true, session_id: 'live', elapsed_sec: 12 });
  await settle();
  calls = [];
  page.onStatus(idle('live', { media_ready: true }));
  await settle();
  s.stoppedHere = calls.slice();
  // Playing it afterwards, the next status push leaves it alone.
  calls = [];
  page.onStatus(idle('live'));
  await settle();
  s.afterStop = calls.slice();

  // The stop's own push was missed: the next push that names the meeting sets
  // playback up, once.
  fresh('live2', false, false);
  page.onStatus(idle('live2'));
  await settle();
  s.missedStop = calls.slice();
  calls = [];
  page.onStatus(idle('live2'));
  await settle();
  s.missedStopThen = calls.slice();
  console.log(JSON.stringify(s));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_a_status_push_never_restarts_playback():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "playback_status.cjs"
        path.write_text(_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(APP_JS)],
                             capture_output=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    s = json.loads(out.stdout.strip().splitlines()[-1])

    # Playing a past meeting: no status push reloads it.
    assert s["pushSameMeeting"] == [], "a status push restarted playback"
    assert s["pushOtherMeeting"] == []
    assert s["otherWindowStopped"] == [], "another window's stop restarted this playback"
    # A stop on this page loads playback, once, and later pushes leave it be.
    assert s["stoppedHere"] == ["audio:live", "video:live"]
    assert s["afterStop"] == []
    # A missed stop push is made up by the next one, and only that one.
    assert s["missedStop"] == ["audio:live2", "video:live2"]
    assert s["missedStopThen"] == []


def test_the_stop_push_says_the_media_is_final():
    """The push sent once the WAV and the video are finalized is the one a page
    showing the meeting loads playback on."""
    stop = APP_PY[APP_PY.index("def stop_recording("):]
    stop = stop[:stop.index("_recording_cleanup_done.set()")]
    assert '_push_status({"recording": False, "session_id": sid, "media_ready": True})' in stop
    # After the video parts are joined and the session row is ended.
    assert stop.index("_concat_video_parts(sid)") < stop.index('"media_ready": True')
    assert stop.index("storage.end_session(sid)") < stop.index('"media_ready": True')
