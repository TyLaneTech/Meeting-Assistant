"""The Update buttons say why an update did not go in (reported 2026-10-02).

A refusal used to wipe the page for a dead-end "Update failed" screen, and an
answer that was not JSON (an unhandled server error) was taken for the restart,
so the page reloaded with nothing said at all. Both buttons now go through
_installUpdate(): the page stays usable until the server has the update, and a
refusal arrives as a toast (App menu) or the status line (Settings). The
harness runs the real functions against a fetch answered by hand.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
APP_JS_PATH = ROOT / "ui_web/static/app.js"

_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function grab(name) {
  const m = src.match(new RegExp('\\n(?:async )?function ' + name + '\\([\\s\\S]*?\\n\\}'));
  if (!m) { throw new Error('FAIL: ' + name + ' not found in app.js'); }
  return m[0];
}

function el(text) {
  return { textContent: text || '', className: '', disabled: false, onclick: null };
}
let menuItem, menuLabel, btn, statusEl, toasts, screens, polls, answer, requests;
function reset() {
  menuLabel = el('Install 2 updates and restart');
  menuItem = Object.assign(el(), { querySelector: () => menuLabel });
  btn = el('Update & Restart');
  statusEl = el('2 updates available');
  toasts = []; screens = []; polls = 0; requests = 0;
  page._installingUpdate = false;
}
const inert = new Proxy(function () {}, {
  get: (t, k) => (typeof k === 'symbol' || k === 'then') ? undefined
    : (k === 'toString' || k === 'valueOf') ? () => '' : inert,
  apply: () => inert,
  set: () => true,
});
const page = {
  document: {
    getElementById: id => ({ 'app-update-item': menuItem, 'check-update-btn': btn,
                             'settings-update-status': statusEl })[id] || null,
  },
  state: { isRecording: false },
  fetch: () => { requests++; return answer(); },
  uiToast: t => { toasts.push({ message: t.message, kind: t.kind || 'info', id: t.id }); return { dismiss() {} }; },
  _showTransitionScreen: title => { screens.push(title); return { titleEl: {}, subtitleEl: {}, stop() {} }; },
  _pollUntilBack: () => { polls++; },
  setInterval: () => { polls++; return 1; },
  _changelogLoaded: true,
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
for (const name of ['_installUpdate', 'doUpdateRestart', 'applyUpdate']) {
  page[name] = compileInPage(grab(name));
}

const json = (status, body) => () => Promise.resolve({
  ok: status >= 200 && status < 300, status, json: async () => body });
const html500 = () => Promise.resolve({
  ok: false, status: 500, json: async () => { throw new SyntaxError("Unexpected token '<'"); } });
const dropped = () => Promise.reject(new TypeError('Failed to fetch'));
const CLASH = 'Your edits to capture_audio/mac.py clash with this update. Commit or undo them, then try again.';
const seen = () => ({ toasts: toasts.slice(), screens: screens.slice(), polls, requests,
                      menu: menuLabel.textContent, menuDisabled: menuItem.disabled,
                      installing: page._installingUpdate, status: statusEl.textContent,
                      statusClass: statusEl.className, btn: btn.textContent, btnDisabled: btn.disabled });

(async () => {
  const s = {};
  for (const [name, reply] of [['refused', json(409, { error: CLASH })], ['notJson', html500],
                               ['ok', json(200, { ok: true })], ['dropped', dropped]]) {
    reset(); answer = reply;
    await page.doUpdateRestart();
    s['menu_' + name] = seen();
    reset(); answer = reply;
    await page.applyUpdate();
    s['settings_' + name] = seen();
  }
  // A second press while the first is out sends nothing.
  reset();
  const releases = [];
  answer = () => new Promise(r => releases.push(() => r({ ok: true, status: 200, json: async () => ({ ok: true }) })));
  const presses = [page.doUpdateRestart(), page.doUpdateRestart(), page.applyUpdate()];
  await new Promise(r => setImmediate(r));
  s.pressedTwice = { requests, toasts: toasts.length };
  releases.forEach(release => release());
  await Promise.all(presses);
  console.log(JSON.stringify(s));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture(scope="module")
def runs():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "update_button.cjs"
        path.write_text(_HARNESS, encoding="utf-8")
        out = subprocess.run([node, str(path), str(APP_JS_PATH)],
                             capture_output=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


CLASH = "Your edits to capture_audio/mac.py clash with this update. Commit or undo them, then try again."


def test_a_refusal_from_the_app_menu_is_a_toast_and_the_page_stays(runs):
    s = runs["menu_refused"]
    assert s["screens"] == [], "no dead-end screen"
    assert s["toasts"] == [
        {"message": "Installing the update…", "kind": "info", "id": "update-install"},
        {"message": f"Could not install the update: {CLASH}", "kind": "error", "id": "update-install"},
    ]
    # The menu item is back to what it offered, ready for another go.
    assert s["menu"] == "Install 2 updates and restart" and not s["menuDisabled"]
    assert not s["installing"] and s["polls"] == 0


def test_an_answer_that_is_not_json_still_says_something(runs):
    assert runs["menu_notJson"]["toasts"][-1]["message"] == "Could not install the update: The server answered 500."
    assert runs["menu_notJson"]["screens"] == []
    assert runs["settings_notJson"]["status"] == "The server answered 500."


def test_once_the_server_has_it_the_restart_screen_takes_over(runs):
    for case in ("menu_ok", "menu_dropped"):
        s = runs[case]
        assert s["screens"] == ["Updating & Restarting…"], case
        assert s["polls"] == 1 and s["menu"] == "Restarting…" and s["menuDisabled"], case
        assert s["installing"], case


def test_the_settings_button_shows_the_reason_in_its_status_line(runs):
    s = runs["settings_refused"]
    assert s["status"] == CLASH and s["statusClass"] == "settings-info-val val-warn"
    assert s["btn"] == "Retry Update" and not s["btnDisabled"]
    assert s["menu"] == "Install 2 updates and restart" and not s["menuDisabled"]
    assert s["toasts"] == [] and s["polls"] == 0
    ok = runs["settings_ok"]
    assert ok["status"] == "Restarting..." and ok["polls"] == 1 and ok["menu"] == "Restarting…"


def test_a_second_press_while_one_is_out_sends_nothing(runs):
    assert runs["pressedTwice"] == {"requests": 1, "toasts": 1}
