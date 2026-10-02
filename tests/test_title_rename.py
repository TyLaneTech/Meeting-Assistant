"""Renaming a meeting by double-clicking the workspace title (2026-10-02).

The heading becomes editable in place; Enter or clicking away saves, Escape
cancels. The save is optimistic: the name changes everywhere at once and the
write follows, and a failed write puts the old name back. The harness runs the
real functions (the rename, updateTopbarSessionTitle, Views.setTitle and
applyTitle, and the event wiring) against a fake heading and a fetch that is
answered by hand.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
APP_JS_PATH = ROOT / "ui_web/static/app.js"
APP_JS = APP_JS_PATH.read_text(encoding="utf-8")
CSS = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('\\n(?:async )?function ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { throw new Error('FAIL: ' + name + ' not found in app.js'); }
  return m[0];
}
function grabMethod(name) {
  const m = src.match(new RegExp('\\n  ' + name + '\\(([^)]*)\\) \\{[\\s\\S]*?\\n  \\},'));
  if (!m) { throw new Error('FAIL: Views.' + name + ' not found in app.js'); }
  return m[0].trim().replace(/,$/, '').replace(name + '(', 'function ' + name + '(');
}
const wiring = src.match(/\n\(function _wireTitleRename\(\) \{[\s\S]*?\n\}\)\(\);/)[0];

// The heading, as far as the code can tell.
function heading() {
  const listeners = {};
  const classes = new Set();
  let editable = 'inherit';
  return {
    textContent: 'Meeting Assistant', title: '', spellcheck: true, scrollLeft: 0, attrs: {},
    classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) },
    get contentEditable() { return editable; },
    set contentEditable(v) { editable = String(v); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; if (k === 'contenteditable') editable = 'inherit'; },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    _fire(ev, extra) {
      const e = Object.assign({ type: ev, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; },
                                stopPropagation() {} }, extra || {});
      for (const fn of listeners[ev] || []) fn(e);
      return e;
    },
    focus() { page.document.activeElement = this; },
    blur() { if (page.document.activeElement === this) { page.document.activeElement = null; this._fire('blur'); } },
  };
}
const titleEl = heading();
const subtitleEl = { textContent: '', classList: { toggle() {} } };
let requests = [], toasts = [];
const sessions = [{ id: 'm1', title: 'Old name' }, { id: 'm2', title: 'Other meeting' }];
const inert = new Proxy(function () {}, {
  get: (t, k) => (typeof k === 'symbol' || k === 'then') ? undefined
    : (k === 'toString' || k === 'valueOf') ? () => '' : inert,
  apply: () => inert,
  set: () => true,
});
const page = {
  document: {
    activeElement: null,
    title: '',
    getElementById: id => ({ 'topbar-session-title': titleEl, 'view-subtitle': subtitleEl })[id] || null,
    createRange: () => ({ selectNodeContents() {} }),
    // insertText replaces the selection, and the whole title is selected.
    execCommand: (cmd, ui, text) => { if (cmd === 'insertText') titleEl.textContent = text; return true; },
  },
  window: { getSelection: () => ({ removeAllRanges() {}, addRange() {} }) },
  state: { sessionId: 'm1', isRecording: false },
  _sidebarAllSessions: sessions,
  _sessionDurationSec: null,
  fetch: (url, opts) => new Promise((resolve, reject) => requests.push({ url, body: opts && opts.body, resolve, reject })),
  uiToast: t => toasts.push(t.message),
  // The slice's subscriber (_onSidebarSlices) repaints the header from it.
  AppData: { patch(name, fn) { fn(sessions); page.updateTopbarSessionTitle(); } },
};
const scope = new Proxy(page, {
  has: (t, k) => typeof k === 'string' && (k in t || !(k in globalThis)),
  get: (t, k) => (typeof k === 'symbol' ? undefined : (k in t ? t[k] : inert)),
  set: (t, k, v) => { t[k] = v; return true; },
});
with (scope) {
  globalThis.__compileInPage = function (__code) { return eval('(' + __code + ')'); };
  globalThis.__runInPage = function (__code) { return eval(__code); };
}
const compileInPage = globalThis.__compileInPage, runInPage = globalThis.__runInPage;
delete globalThis.__compileInPage; delete globalThis.__runInPage;
page.Views = { current: 'session', _titles: {} };
page.Views.setTitle = compileInPage(grabMethod('setTitle'));
page.Views.applyTitle = compileInPage(grabMethod('applyTitle'));
for (const name of ['updateTopbarSessionTitle', 'startTitleRename', '_endTitleRename',
                    '_renameSession', '_setSessionTitleLocally']) {
  page[name] = compileInPage(grab(name));
}
page._titleEdit = null;
page._titleSaves = new Map();
runInPage(wiring);

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
const shown = () => ({ header: titleEl.textContent, sidebar: sessions[0].title,
                       editing: titleEl.classList.contains('is-editing'),
                       editable: titleEl.contentEditable, requests: requests.length, toasts: toasts.slice() });
const typeAndLeave = (text, how) => {
  titleEl._fire('dblclick');
  titleEl.textContent = text;
  if (how === 'escape') titleEl._fire('keydown', { key: 'Escape' });
  else if (how === 'enter') titleEl._fire('keydown', { key: 'Enter' });
  else titleEl.blur();
};
async function answer(i, ok, status) {
  const r = requests[i];
  if (ok === null) r.reject(new TypeError('Failed to fetch'));
  else r.resolve({ ok, status: status || (ok ? 200 : 500), json: async () => (ok ? { ok: true } : { error: 'Disk is full' }) });
  await settle();
}

(async () => {
  const s = {};
  page.updateTopbarSessionTitle();
  s.idle = Object.assign(shown(), { tooltip: titleEl.title });
  titleEl._fire('dblclick');
  s.editing = Object.assign(shown(), { focused: page.document.activeElement === titleEl, role: titleEl.attrs.role });
  // A status push lands mid-edit and must not replace what is being typed.
  titleEl.textContent = 'Typed so f';
  page.updateTopbarSessionTitle();
  s.pushWhileEditing = shown();
  // Clicking away saves, at once, with the spacing tidied.
  titleEl.textContent = '  Budget   review \n';
  titleEl.blur();
  s.saved = Object.assign(shown(), { body: requests[0] && requests[0].body, url: requests[0] && requests[0].url });
  await answer(0, true);
  s.confirmed = shown();
  // Enter saves too; this write fails and the old name comes back.
  typeAndLeave('Broken name', 'enter');
  s.optimistic = shown();
  await answer(1, null);
  s.failed = shown();
  // A refusal says why.
  typeAndLeave('Refused name');
  await answer(2, false, 500);
  s.refused = shown();
  // Escape, no change, and an empty title send nothing.
  typeAndLeave('Discard me', 'escape');
  s.escaped = shown();
  typeAndLeave('Budget review');
  s.unchanged = shown();
  typeAndLeave('   ');
  s.emptied = shown();
  // A failure that a newer rename has replaced changes nothing.
  typeAndLeave('First');
  typeAndLeave('Second');
  await answer(4, true);
  await answer(3, false, 500);
  s.superseded = shown();
  // A pasted name is one plain line.
  titleEl._fire('dblclick');
  const pasted = titleEl._fire('paste', { clipboardData: { getData: () => 'Line one\nLine   two' } });
  s.paste = { text: titleEl.textContent, prevented: pasted.defaultPrevented };
  titleEl._fire('keydown', { key: 'Escape' });
  // Only the workspace's own title, and only for a meeting.
  page.Views.current = 'home';
  titleEl._fire('dblclick');
  s.otherView = shown();
  page.Views.current = 'session';
  page.state.sessionId = null;
  titleEl._fire('dblclick');
  s.noMeeting = shown();
  console.log(JSON.stringify(s));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_double_click_renames_the_meeting_optimistically():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "title_rename.cjs"
        path.write_text(_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(APP_JS_PATH)],
                             capture_output=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    s = json.loads(out.stdout.strip().splitlines()[-1])

    assert s["idle"]["header"] == "Old name"
    assert s["idle"]["tooltip"] == "Old name\nDouble-click to rename"
    editing = s["editing"]
    assert editing["editing"] and editing["editable"] == "plaintext-only", editing
    assert editing["focused"] and editing["role"] == "textbox"
    # What is being typed survives a status push.
    assert s["pushWhileEditing"]["header"] == "Typed so f"
    # Clicking away: shown everywhere before the server has answered.
    saved = s["saved"]
    assert saved["header"] == saved["sidebar"] == "Budget review", saved
    assert not saved["editing"] and saved["editable"] == "inherit"
    assert saved["requests"] == 1 and json.loads(saved["body"]) == {"title": "Budget review"}
    assert saved["url"] == "/api/sessions/m1"
    assert s["confirmed"]["header"] == "Budget review" and not s["confirmed"]["toasts"]
    # A failed write puts the old name back and says so.
    assert s["optimistic"]["header"] == "Broken name"
    failed = s["failed"]
    assert failed["header"] == failed["sidebar"] == "Budget review", failed
    assert failed["toasts"] == ["Could not rename the meeting: Meeting Assistant is not responding"]
    assert s["refused"]["header"] == "Budget review"
    assert s["refused"]["toasts"][-1] == "Could not rename the meeting: Disk is full"
    # Nothing to save: no request, and the title is what it was.
    for case in ("escaped", "unchanged", "emptied"):
        assert s[case]["requests"] == 3 and s[case]["header"] == "Budget review", (case, s[case])
        assert not s[case]["editing"]
    # A superseded failure leaves the newer name alone, quietly.
    superseded = s["superseded"]
    assert superseded["header"] == "Second" and len(superseded["toasts"]) == 2, superseded
    assert s["paste"] == {"text": "Line one Line two", "prevented": True}
    assert not s["otherView"]["editing"] and not s["noMeeting"]["editing"]


def test_the_title_rename_reuses_the_locking_patch_and_is_styled_in_place():
    rename = APP_JS[APP_JS.index("async function _renameSession("):]
    rename = rename[:rename.index("\n}\n")]
    assert "method: 'PATCH'" in rename and "JSON.stringify({ title })" in rename
    # The heading is the field: no input swapped in, so nothing shifts.
    start = APP_JS[APP_JS.index("function startTitleRename("):]
    start = start[:start.index("\n}\n")]
    assert "createElement('input')" not in start
    rule = CSS[CSS.index("#topbar-session-title.is-editing {"):]
    rule = rule[:rule.index("}")]
    for prop in ("padding", "margin", "font-size", "border:", "height"):
        assert prop not in rule, prop
