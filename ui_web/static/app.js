/* ── marked.js setup ─────────────────────────────────────────────────────── */
marked.use({ breaks: true, gfm: true });

// Open every rendered markdown link in a new tab. Skip links that already
// declare a `target` *or* have an `onclick` handler - the latter is how the
// timestamp pills are wired (`href="#" onclick="seekPlayback(…)"`), and
// adding target="_blank" to them would open a blank tab on middle-click.
marked.use({
  hooks: {
    postprocess(html) {
      return html.replace(
        /<a (?![^>]*\b(?:target|onclick)=)([^>]*?)>/g,
        '<a target="_blank" rel="noopener noreferrer" $1>',
      );
    },
  },
});

function renderMd(text) {
  return marked.parse(text || '');
}

/**
 * Typing-cursor manager.
 * One cursor span is created and reused - after each render it's moved to the
 * deepest last inline position. Chunk arrivals add a .streaming class that
 * lights the cursor up; a debounce timer dims it back when chunks stop.
 */
let _typingCursor = null;
let _typingCursorContainer = null;
let _streamingTimer = null;
const _STREAMING_TIMEOUT = 250;

// Void / non-text elements the cursor must never descend into or land after
const _CURSOR_SKIP = new Set([
  'IMG','BR','HR','INPUT','SVG','VIDEO','AUDIO','CANVAS','IFRAME',
  'COL','COLGROUP','SOURCE','TRACK','WBR','AREA','EMBED','OBJECT',
]);

function _deepestLastLeaf(el) {
  let target = el;
  outer:
  while (true) {
    // Walk backwards through children to skip void/non-text elements
    const children = target.children;
    for (let i = children.length - 1; i >= 0; i--) {
      const child = children[i];
      // Skip the cursor itself so we don't nest inside it
      if (child === _typingCursor) continue;
      if (_CURSOR_SKIP.has(child.tagName)) continue;
      target = child;
      continue outer;
    }
    break;
  }
  return target;
}

function _ensureTypingCursor(container) {
  _typingCursorContainer = container;
  if (!_typingCursor) {
    _typingCursor = document.createElement('span');
    _typingCursor.className = 'typing-cursor-span';
  }
  // Detach first so _deepestLastLeaf never sees the old position
  if (_typingCursor.parentNode) _typingCursor.remove();
  _deepestLastLeaf(container).appendChild(_typingCursor);
}

function _chunkArrived() {
  if (!_typingCursor) return;
  // If morphdom detached the cursor, re-anchor it
  if (!_typingCursor.isConnected && _typingCursorContainer) {
    _ensureTypingCursor(_typingCursorContainer);
  }
  _typingCursor.classList.add('streaming');
  clearTimeout(_streamingTimer);
  _streamingTimer = setTimeout(() => {
    if (_typingCursor) _typingCursor.classList.remove('streaming');
  }, _STREAMING_TIMEOUT);
}

function _removeTypingCursor() {
  clearTimeout(_streamingTimer);
  if (_typingCursor) {
    _typingCursor.remove();
    _typingCursor = null;
  }
  _typingCursorContainer = null;
}

/**
 * Diff-update a chat body element using morphdom to avoid re-creating
 * existing DOM nodes (which causes images to flash/reload).
 * Also wires up image onload handlers to fix auto-scroll when images
 * load asynchronously and change the scroll height.
 */
// Replace [M:SS] timestamps in raw markdown text with HTML spans BEFORE
// passing to marked.parse(). This prevents marked from interpreting the
// brackets as link reference syntax and ensures pills render during streaming.
const _tsMdRe = /\[(\d{1,2}:\d{2})(?:[\u2013\u2014\-](\d{1,2}:\d{2}))?\]/g;
function _linkifyTimestampsInMd(md) {
  return md.replace(_tsMdRe, (full, start, end) => {
    const [m, s] = start.split(':').map(Number);
    const sec = m * 60 + s;
    const label = end ? `${start} - ${end}` : start;
    const title = end ? `Jump to ${start} - ${end}` : `Jump to ${start}`;
    return `<a class="timestamp-link" href="#" title="${title}" onclick="event.preventDefault();seekPlayback(${sec})">${label}</a>`;
  });
}

function _morphChatBody(el, mdText) {
  // Linkify timestamps in the raw markdown before marked parses it
  let newHtml = renderMd(_linkifyTimestampsInMd(mdText));

  // Fast path for the common image-free reply: skip all three img scans. marked
  // escapes literal <img inside code blocks to &lt;img, so this only short-
  // circuits when the rendered output genuinely has no images to preserve.
  const hasImg = newHtml.indexOf('<img') !== -1;

  // Preserve existing loaded images - detach them before morphdom runs,
  // then restore them after. This prevents flashing when morphdom
  // recreates parent <p> elements around unchanged images.
  const existingImgs = new Map();
  if (hasImg) {
    el.querySelectorAll('img[src]').forEach(img => {
      existingImgs.set(img.getAttribute('src'), img);
    });
  }

  const tmp = document.createElement('div');
  tmp.innerHTML = newHtml;
  morphdom(el, tmp, { childrenOnly: true });

  if (!hasImg) return;

  // Restore preserved images by replacing their fresh (unloaded) clones
  if (existingImgs.size > 0) {
    el.querySelectorAll('img[src]').forEach(freshImg => {
      const src = freshImg.getAttribute('src');
      const cached = existingImgs.get(src);
      if (cached && cached !== freshImg && cached.complete) {
        freshImg.replaceWith(cached);
      }
    });
  }

  // Wire image load handlers for scroll correction
  el.querySelectorAll('img:not([data-scroll-wired])').forEach(img => {
    img.dataset.scrollWired = '1';
    img.addEventListener('load', () => scrollChatToBottom(), { once: true });
  });
}

// Live summary tokens arrive one at a time; coalesce rendering to one markdown
// parse + morph per animation frame instead of rebuilding the whole summary DOM
// per token (which was O(N^2) parsing + a full teardown + a forced reflow each).
let _summaryRenderRAF = null;
let _pendingChatRaf = 0;   // same coalescing for the chat stream
function _flushSummaryRender() {
  _summaryRenderRAF = null;
  if (!state.summaryCursor) return;
  _morphChatBody(state.summaryCursor, state.summaryBuffer);
  if (_summaryAtBottom) state.summaryCursor.scrollTop = state.summaryCursor.scrollHeight;
}

/**
 * Post-process rendered summary HTML to make timestamps clickable pills.
 * Matches single timestamps [M:SS] and ranges [M:SS-M:SS] (en-dash, em-dash,
 * or plain hyphen as separator). Clicking seeks to the start of the range.
 */
function linkifyTimestamps(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  // Group 1: start time. Group 2 (optional): end time after - or -
  const timestampRe = /\[(\d{1,2}:\d{2})(?:[\u2013\u2014\-](\d{1,2}:\d{2}))?\]/g;
  const nodesToReplace = [];

  let node;
  while ((node = walker.nextNode())) {
    if (timestampRe.test(node.textContent)) {
      nodesToReplace.push(node);
    }
    timestampRe.lastIndex = 0;
  }

  for (const textNode of nodesToReplace) {
    const frag = document.createDocumentFragment();
    let lastIdx = 0;
    let match;
    timestampRe.lastIndex = 0;
    const text = textNode.textContent;

    while ((match = timestampRe.exec(text)) !== null) {
      if (match.index > lastIdx) {
        frag.appendChild(document.createTextNode(text.slice(lastIdx, match.index)));
      }

      const [startM, startS] = match[1].split(':').map(Number);
      const startSec = startM * 60 + startS;
      // Use en-dash (-) as canonical separator in the displayed label
      const label = match[2] ? `${match[1]} - ${match[2]}` : match[1];
      const title = match[2]
        ? `Jump to ${match[1]} to ${match[2]}`
        : `Jump to ${match[1]}`;

      const link = document.createElement('a');
      link.className = 'timestamp-link';
      link.textContent = label;
      link.title = title;
      link.href = '#';
      link.addEventListener('click', ((t) => (e) => {
        e.preventDefault();
        jumpToTimestamp(t);
      })(startSec));
      frag.appendChild(link);
      lastIdx = timestampRe.lastIndex;
    }

    if (lastIdx < text.length) {
      frag.appendChild(document.createTextNode(text.slice(lastIdx)));
    }
    textNode.parentNode.replaceChild(frag, textNode);
  }
}

/* ── localStorage layout cache ───────────────────────────────────────────── */
// Stores layout values locally so they can be applied synchronously on load,
// eliminating the flash caused by the async /api/preferences fetch.
const _LAYOUT_CACHE_KEY = 'ma-layout';
const _FOLDER_STATE_KEY = 'ma-folder-state';

function _getLayoutCache() {
  try { return JSON.parse(localStorage.getItem(_LAYOUT_CACHE_KEY) || '{}'); } catch (_) { return {}; }
}
function _saveLayoutCache(updates) {
  try {
    localStorage.setItem(_LAYOUT_CACHE_KEY, JSON.stringify({ ..._getLayoutCache(), ...updates }));
  } catch (_) {}
}

/* ══════════════════════════════════════════════════════════════════════════
   The app shell: one store, one router, five views
   ══════════════════════════════════════════════════════════════════════════
   Every route (/, /calendar, /attention, /speakers, /session) renders the same
   template. The five view roots are DOM siblings inside <main class="views">;
   exactly one carries .is-active. The workspace is hidden, never destroyed, so
   a live recording keeps streaming into it while another view is on screen.
   See context/ui-overhaul-2026-09.md sections 3.1 and 3.2.
   ══════════════════════════════════════════════════════════════════════════ */

const VIEW_NAMES = ['home', 'calendar', 'attention', 'speakers', 'session'];

/* ── AppData: the store for shared GET reads ──────────────────────────────────
 * Search, chat, commands and uploads are deliberately NOT cached here.
 * Each slice: data (last committed payload), lastGood (kept through a failure),
 * status, error, updatedAt, revision, token. A response commits only while its
 * token is the slice's current one, so a slow reply can never overwrite a newer
 * one, and a failure keeps the last good data instead of rendering zeros.
 * ─────────────────────────────────────────────────────────────────────────── */

const _SLICE_ENDPOINTS = {
  sessions:       '/api/sessions',
  folders:        '/api/folders',
  analytics:      '/api/dashboard',
  attention:      '/api/attention/summary',
  calendarStatus: '/api/calendar/status',
};

// Slices the whole shell depends on (the sidebar list, the attention badge).
// The rest are reloaded lazily, when the view that needs them is active.
const _EAGER_SLICES = new Set(['sessions', 'folders', 'attention']);

// Which slices each view renders from. Also drives the Refresh control.
const VIEW_SLICES = {
  home:      ['analytics', 'sessions', 'attention', 'calendarStatus'],
  calendar:  ['sessions', 'calendarStatus', 'calendarEvents'],
  attention: ['sessions', 'attention'],
  speakers:  [],
  session:   ['sessions'],
};

function _newSlice(fallback) {
  return {
    data: null, lastGood: fallback, status: 'idle', error: null,
    updatedAt: 0, revision: 0, token: 0, pending: null,
  };
}

/** 'YYYY-MM-DD..YYYY-MM-DD' - the calendarEvents cache key is the range. */
function calendarRangeKey(start, end) { return `${start}..${end}`; }

const AppData = {
  slices: {
    sessions:       _newSlice([]),
    folders:        _newSlice([]),
    analytics:      _newSlice(null),
    attention:      _newSlice(null),
    calendarStatus: _newSlice(null),
    calendarEvents: {},          // rangeKey -> slice, lazy, one load per range
  },
  _subs: [],

  _slice(name, key) {
    if (name !== 'calendarEvents') return this.slices[name] || null;
    if (!key) return null;
    if (!this.slices.calendarEvents[key]) this.slices.calendarEvents[key] = _newSlice([]);
    return this.slices.calendarEvents[key];
  },

  /** The data to render: the committed payload, or the last good one. */
  get(name, key) {
    const s = this._slice(name, key);
    if (!s) return null;
    return s.data != null ? s.data : s.lastGood;
  },
  status(name, key) { const s = this._slice(name, key); return s ? s.status : 'idle'; },
  error(name, key) { const s = this._slice(name, key); return s ? s.error : null; },
  lastUpdated(name, key) { const s = this._slice(name, key); return s ? s.updatedAt : 0; },
  revision(name, key) { const s = this._slice(name, key); return s ? s.revision : 0; },

  /** True while the view is rendering last-good data after a failed reload. */
  isStale(name, key) {
    const s = this._slice(name, key);
    return !!(s && s.status === 'error' && s.data != null);
  },

  _url(name, key) {
    if (name !== 'calendarEvents') return _SLICE_ENDPOINTS[name];
    const [start, end] = String(key).split('..');
    return `/api/calendar/events?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`;
  },

  load(name, opts) {
    const o = opts || {};
    const s = this._slice(name, o.key);
    if (!s) return Promise.resolve(null);
    // Switching views must not fetch unless the slice is idle. Concurrent
    // requests for one slice coalesce into the one already in flight.
    if (!o.force && (s.status === 'ready' || s.status === 'loading')) {
      return s.pending || Promise.resolve(this.get(name, o.key));
    }
    const token = ++s.token;         // any older reply can no longer commit
    s.status = 'loading';
    this._emit(name, o.key);
    const settle = () => { if (token === s.token) s.pending = null; };
    s.pending = fetch(this._url(name, o.key), { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))))
      .then(payload => {
        if (token !== s.token) return this.get(name, o.key);
        s.data = payload;
        s.lastGood = payload;
        s.error = null;
        s.status = 'ready';
        s.updatedAt = Date.now();
        s.revision++;
        settle();
        this._emit(name, o.key);
        return payload;
      })
      .catch(err => {
        if (token !== s.token) return this.get(name, o.key);
        s.error = (err && err.message) || 'request failed';
        s.status = 'error';
        settle();
        this._emit(name, o.key);
        return this.get(name, o.key);
      });
    return s.pending;
  },

  /** Mark slices out of date. Shell-wide slices reload now; view-only slices
   *  reload when their view is on screen, and otherwise on next activate. */
  invalidate(names, reason) {
    const list = Array.isArray(names) ? names : [names];
    const active = VIEW_SLICES[Views.current] || [];
    const jobs = [];
    for (const name of list) {
      if (name === 'calendarEvents') {
        for (const key of Object.keys(this.slices.calendarEvents)) {
          const s = this.slices.calendarEvents[key];
          s.status = 'idle';
          if (active.includes('calendarEvents')) jobs.push(this.load(name, { force: true, key }));
          else this._emit(name, key);
        }
        continue;
      }
      const s = this._slice(name);
      if (!s) continue;
      s.status = 'idle';
      if (_EAGER_SLICES.has(name) || active.includes(name)) {
        jobs.push(this.load(name, { force: true }));
      } else {
        this._emit(name);
      }
    }
    return Promise.all(jobs);
  },

  /** In-place edit for an event that carries enough data to patch (a retitle),
   *  so the list does not have to be refetched to look right. */
  patch(name, fn, key) {
    const s = this._slice(name, key);
    if (!s) return;
    const next = fn(s.data != null ? s.data : s.lastGood);
    if (next !== undefined) { s.data = next; s.lastGood = next; }
    s.revision++;
    s.updatedAt = Date.now();
    this._emit(name, key);
  },

  subscribe(names, fn) {
    this._subs.push({ names: Array.isArray(names) ? names : [names], fn });
    return fn;
  },
  unsubscribe(fn) {
    this._subs = this._subs.filter(sub => sub.fn !== fn);
  },
  _emit(name, key) {
    for (const sub of this._subs.slice()) {
      if (!sub.names.includes(name)) continue;
      try { sub.fn(name, key); } catch (e) { console.error('[AppData] subscriber failed', e); }
    }
  },

  /** Header Refresh: the active view's dependencies only, never everything. */
  refreshActiveView() {
    const names = (VIEW_SLICES[Views.current] || []).slice();
    if (!names.length) names.push('sessions');
    const jobs = [];
    for (const name of names) {
      if (name === 'calendarEvents') {
        const keys = Object.keys(this.slices.calendarEvents);
        keys.forEach(key => jobs.push(this.load(name, { force: true, key })));
        continue;
      }
      jobs.push(this.load(name, { force: true }));
    }
    // The sidebar list is on screen in every view, so it refreshes with them.
    if (!names.includes('sessions')) jobs.push(this.load('sessions', { force: true }));
    if (!names.includes('folders')) jobs.push(this.load('folders', { force: true }));
    return Promise.all(jobs).then(() => {
      const failed = names.filter(n => n !== 'calendarEvents' && this.status(n) === 'error');
      if (failed.length) {
        const reason = this.error(failed[0]) || 'request failed';
        throw new Error(reason);
      }
    });
  },
};

/** refreshSidebar() is the old name for "the recordings list changed". */
async function refreshSidebar() {
  await AppData.invalidate(['sessions', 'folders'], 'sidebar');
}

/* ── Popovers and menus ──────────────────────────────────────────────────────
 * Menus are position: fixed so they escape every scroll container, close on
 * outside click, Escape and navigation, and are arrow-key reachable with focus
 * returning to the trigger.
 * ─────────────────────────────────────────────────────────────────────────── */

let _openMenuId = null;
let _openMenuTrigger = null;

function _menuItems(menu) {
  return [...menu.querySelectorAll('.menu-item')].filter(
    el => !el.disabled && !el.classList.contains('hidden') && el.offsetParent !== null);
}

function _positionMenu(menu, trigger) {
  const r = trigger.getBoundingClientRect();
  menu.style.position = 'fixed';
  menu.style.visibility = 'hidden';
  menu.style.top = '0px';
  menu.style.left = '0px';
  const m = menu.getBoundingClientRect();
  const gap = 6;
  let top = menu.classList.contains('menu-up') ? r.top - m.height - gap : r.bottom + gap;
  let left = menu.classList.contains('menu-right') ? r.right - m.width : r.left;
  top = Math.max(8, Math.min(top, window.innerHeight - m.height - 8));
  left = Math.max(8, Math.min(left, window.innerWidth - m.width - 8));
  menu.style.top = top + 'px';
  menu.style.left = left + 'px';
  menu.style.visibility = '';
}

function closeMenu(opts) {
  if (!_openMenuId) return;
  const menu = document.getElementById(_openMenuId);
  const trigger = _openMenuTrigger;
  _openMenuId = null;
  _openMenuTrigger = null;
  if (menu) menu.classList.add('hidden');
  if (trigger) {
    trigger.setAttribute('aria-expanded', 'false');
    if (opts && opts.restoreFocus && trigger.isConnected) trigger.focus();
  }
  document.removeEventListener('mousedown', _onMenuOutside, true);
  document.removeEventListener('keydown', _onMenuKey, true);
  window.removeEventListener('resize', _onMenuReflow);
  window.removeEventListener('scroll', _onMenuReflow, true);
}

function _onMenuOutside(e) {
  const menu = _openMenuId && document.getElementById(_openMenuId);
  if (!menu) return;
  if (menu.contains(e.target) || (_openMenuTrigger && _openMenuTrigger.contains(e.target))) return;
  closeMenu();
}

function _onMenuReflow() {
  const menu = _openMenuId && document.getElementById(_openMenuId);
  if (menu && _openMenuTrigger) _positionMenu(menu, _openMenuTrigger);
}

function _onMenuKey(e) {
  const menu = _openMenuId && document.getElementById(_openMenuId);
  if (!menu) return;
  const items = _menuItems(menu);
  const at = items.indexOf(document.activeElement);
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu({ restoreFocus: true }); return; }
  if (e.key === 'Tab') { closeMenu(); return; }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    if (!items.length) return;
    const next = e.key === 'ArrowDown'
      ? (at < 0 ? 0 : (at + 1) % items.length)
      : (at <= 0 ? items.length - 1 : at - 1);
    items[next].focus();
    return;
  }
  if (e.key === 'Home' && items.length) { e.preventDefault(); items[0].focus(); }
  if (e.key === 'End' && items.length) { e.preventDefault(); items[items.length - 1].focus(); }
}

function openMenu(menuId, trigger) {
  const menu = document.getElementById(menuId);
  if (!menu || !trigger) return;
  closeMenu();
  _openMenuId = menuId;
  _openMenuTrigger = trigger;
  menu.classList.remove('hidden');
  _positionMenu(menu, trigger);
  trigger.setAttribute('aria-expanded', 'true');
  document.addEventListener('mousedown', _onMenuOutside, true);
  document.addEventListener('keydown', _onMenuKey, true);
  window.addEventListener('resize', _onMenuReflow);
  window.addEventListener('scroll', _onMenuReflow, true);
  // Keyboard activation lands on the first item; a mouse click does not steal
  // the pointer's position.
  if (trigger.dataset.menuKeyboard === '1') {
    delete trigger.dataset.menuKeyboard;
    const items = _menuItems(menu);
    if (items.length) items[0].focus();
  }
}

/** Menu triggers call this from onclick. A keyboard-activated click reports
 *  detail 0, which is how the first item gets focus. */
function _toggleMenu(menuId, trigger) {
  if (_openMenuId === menuId) { closeMenu({ restoreFocus: true }); return; }
  if (window.event && window.event.detail === 0) trigger.dataset.menuKeyboard = '1';
  openMenu(menuId, trigger);
}

/* ── Views: the client router ────────────────────────────────────────────── */

const Views = {
  current: null,
  _defs: new Map(),
  _scroll: {},
  _titles: {},

  register(name, hooks) { this._defs.set(name, hooks || {}); },

  /** name: one of VIEW_NAMES. opts: { url, replace, popstate, focus, state } */
  show(name, opts) {
    const o = opts || {};
    if (!VIEW_NAMES.includes(name)) name = 'home';
    const repeat = this.current === name;
    const prev = this.current;

    if (!repeat && prev) {
      const el = document.getElementById('view-' + prev);
      if (el) this._scroll[prev] = el.scrollTop;
      const def = this._defs.get(prev);
      if (def && def.deactivate) {
        try { def.deactivate(); } catch (e) { console.error('[Views] deactivate failed', e); }
      }
    }

    closeMenu();
    this.current = name;
    document.body.dataset.view = name;
    for (const view of VIEW_NAMES) {
      const el = document.getElementById('view-' + view);
      if (el) el.classList.toggle('is-active', view === name);
    }
    _syncNavCurrent(name);
    _syncAskRailForView(name);
    _syncHeaderForView(name);

    const el = document.getElementById('view-' + name);
    if (el) {
      const restore = o.popstate && o.state && typeof o.state.scroll === 'number'
        ? o.state.scroll
        : (this._scroll[name] || 0);
      el.scrollTop = restore;
      // 90 ms opacity crossfade. Skipped for Back, repeated selection and
      // reduced motion; nothing translates.
      const reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (!repeat && !o.popstate && !o.noFade && !reduce) {
        el.style.opacity = '0';
        requestAnimationFrame(() => {
          el.style.transition = 'opacity 90ms linear';
          el.style.opacity = '1';
          setTimeout(() => { el.style.transition = ''; el.style.opacity = ''; }, 160);
        });
      }
    }

    if (o.url) this._writeHistory(name, o.url, !!o.replace || repeat, o.popstate);
    this.applyTitle(name);

    const def = this._defs.get(name);
    if (def && def.activate) {
      try { def.activate({ repeat, popstate: !!o.popstate }); }
      catch (e) { console.error('[Views] activate failed', e); }
    }

    if (o.focus && el) {
      const heading = el.querySelector('.view-heading');
      if (heading) heading.focus({ preventScroll: true });
    }
    _syncRefreshTooltip();
    return name;
  },

  _writeHistory(name, url, replace, popstate) {
    if (popstate) return;                        // the browser already moved
    const state = { view: name, url, scroll: this._scroll[name] || 0 };
    if (name === 'calendar' && typeof _calState !== 'undefined') {
      state.calendarMonth = _calMonthParam();
      state.calendarDay = _calState.selectedKey ? _calDayParam(_calState.selectedKey) : null;
    }
    const here = location.pathname + location.search;
    if (replace || here === url) history.replaceState(state, '', url);
    else history.pushState(state, '', url);
  },

  /** Views own their header title and subtitle; the router sets the default. */
  setTitle(name, title, subtitle) {
    this._titles[name] = { title, subtitle };
    if (this.current === name) this.applyTitle(name);
  },

  applyTitle(name) {
    const custom = this._titles[name] || {};
    const fallback = {
      home: 'Home', calendar: 'Calendar', attention: 'Needs attention',
      speakers: 'Speakers', session: 'Meeting Assistant',
    }[name];
    const title = custom.title || fallback;
    if (name === 'session') {
      const el = document.getElementById('topbar-session-title');
      if (el) el.textContent = title;
      document.title = title === 'Meeting Assistant' ? 'Meeting Assistant' : `${title} · Meeting Assistant`;
    } else {
      const el = document.getElementById('view-title');
      if (el) el.textContent = title;
      document.title = `${fallback} · Meeting Assistant`;
    }
    const sub = document.getElementById('view-subtitle');
    if (sub) {
      sub.textContent = custom.subtitle || '';
      sub.classList.toggle('hidden', !custom.subtitle);
    }
  },
};

function _syncNavCurrent(name) {
  document.querySelectorAll('#sidebar .nav-row[data-nav]').forEach(row => {
    const target = _routeOf(row.getAttribute('href'));
    const active = target === name;
    row.classList.toggle('active', active);
    if (active) row.setAttribute('aria-current', 'page');
    else row.removeAttribute('aria-current');
  });
}

/** The view a same-origin path belongs to, or null when it is not a route. */
function _routeOf(href) {
  if (!href) return null;
  let path;
  try { path = new URL(href, location.origin).pathname; } catch (_) { return null; }
  if (path === '/') return 'home';
  if (path === '/calendar') return 'calendar';
  if (path === '/attention') return 'attention';
  if (path === '/speakers') return 'speakers';
  if (path === '/session') return 'session';
  return null;
}

/* ── navigateTo: one path in and out of every view ───────────────────────── */

function navigateTo(url, opts) {
  const o = opts || {};
  let parsed;
  try { parsed = new URL(url, location.origin); } catch (_) { return false; }
  if (parsed.origin !== location.origin) return false;
  const view = _routeOf(parsed.pathname);
  if (!view) return false;

  const params = new URLSearchParams(parsed.search);
  // A bare /session with no query is the blank workspace; every other route
  // keeps its parameters so the actions below can consume them one at a time.
  Views.show(view, {
    url: parsed.pathname + (parsed.search || ''),
    replace: o.replace,
    popstate: o.popstate,
    state: o.state,
    focus: o.focus,
  });
  _applyRouteQuery(view, params, o);
  return true;
}

/** Strip the parameters we have acted on, keeping the rest of the URL. */
function _consumeParams(...keys) {
  const next = new URLSearchParams(location.search);
  let touched = false;
  keys.forEach(k => { if (next.has(k)) { next.delete(k); touched = true; } });
  if (!touched) return;
  const qs = next.toString();
  const url = qs ? `${location.pathname}?${qs}` : location.pathname;
  history.replaceState({ ...(history.state || {}), url }, '', url);
}

function _applyRouteQuery(view, params, opts) {
  const o = opts || {};

  if (params.has('workspace')) _consumeParams('workspace');

  // ?attention=needs still filters the recordings list (the criterion lives in
  // the filter popover); the queue itself is the /attention route.
  if (params.get('attention') === 'needs') {
    _setAttentionFilter(true);
    if (typeof _prefsReady !== 'undefined') _prefsReady.then(() => _setAttentionFilter(true)).catch(() => {});
    _consumeParams('attention');
  }

  if (params.has('settings') || params.has('setup')) {
    openSettings(params.get('section') || null);
    _consumeParams('settings', 'setup', 'section');
  }

  if (params.has('fingerprint')) {
    _consumeParams('fingerprint');
    if (view !== 'speakers') { navigateTo('/speakers', { replace: true }); return; }
  }

  if (view === 'calendar') {
    const month = params.get('month');
    const day = params.get('day');
    if (typeof _calApplyRoute === 'function') _calApplyRoute(month, day);
  }

  if (view !== 'session') return;

  if (params.has('quiet_prompt')) {
    _quietPromptLanding = params.get('id');
    _consumeParams('quiet_prompt');
  }

  // ?speakers=cleanup opens the Speakers dialog on Cleanup once the session
  // is bound. The older ?speakers=resolve is honoured the same way: Resolve
  // was folded into Cleanup, and links to it may still be out there.
  const wantsSpeakers = ['cleanup', 'resolve'].includes(params.get('speakers'));
  const openSpeakersDialog = () => {
    if (!wantsSpeakers) return;
    openSpeakerManager('cleanup');
    _consumeParams('speakers');
  };

  if (params.has('autostart')) {
    _consumeParams('autostart');
    _waitForRecordReady().then(() => {
      if (_recordingCommandLost) return;      // another window won this start
      if (!state.isRecording) startNewRecording();
    });
    return;
  }

  const id = params.get('id');
  if (id) {
    // Reconcile against /api/status before binding the workspace: if this is
    // the live session, SSE status and replay own it and loadSession must not
    // re-render underneath them.
    fetch('/api/status').then(r => r.json()).then(st => {
      if (st.recording && st.session_id === id) {
        state.sessionId = id;
        state.isViewingPast = false;
        if (_quietPromptLanding === id) {
          setTimeout(() => showQuietStopConfirm(id), 250);
          _quietPromptLanding = null;
        }
        openSpeakersDialog();
        return;
      }
      return loadSession(id).then(openSpeakersDialog);
    }).catch(() => loadSession(id).then(openSpeakersDialog));
    return;
  }

  // /session with nothing to show. Only a Back navigation clears the loaded
  // session; a live recording is never cleared, replaced or stopped.
  if (o.popstate && !state.isRecording && state.sessionId) {
    state.sessionId = null;
    state.isViewingPast = false;
    clearAll();
    updateRecordBtn();
    _updateActiveFolderHighlights();
  }
}

/** Intercept only unmodified primary clicks on same-origin route links. */
function _initRouteLinks() {
  document.addEventListener('click', e => {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest('a[href]');
    if (!a || a.target || a.hasAttribute('download') || a.dataset.external !== undefined) return;
    if (a.getAttribute('href').startsWith('#')) return;
    const view = _routeOf(a.getAttribute('href'));
    if (!view) return;
    e.preventDefault();
    navigateTo(a.getAttribute('href'), { focus: e.detail === 0 });
  });

  window.addEventListener('popstate', e => {
    navigateTo(location.pathname + location.search, { popstate: true, state: e.state });
  });
}

/* ── Header: Ask, Layout, Search, Refresh ────────────────────────────────── */

const ASK_DOCK_MIN_PX = 1200;   // main column width the docked rail needs

function _askIsOpen() {
  const cache = _getLayoutCache();
  return cache.ask_open === true;
}

function toggleAskRail(force) {
  const open = typeof force === 'boolean' ? force : !_askIsOpen();
  _saveLayoutCache({ ask_open: open });
  _syncAskRailForView(Views.current);
  if (open) {
    const input = document.getElementById('global-chat-input');
    if (input) input.focus();
  }
}

function _syncAskRailForView(view) {
  const rail = document.getElementById('ask-rail');
  const toggle = document.getElementById('ask-toggle');
  if (!rail) return;
  // The workspace has its own Chat column behind the Layout control, so Ask
  // (the global library assistant) is not offered there.
  const allowed = view !== 'session';
  const open = allowed && _askIsOpen();
  rail.hidden = !open;
  document.body.classList.toggle('ask-open', open);
  if (toggle) {
    toggle.classList.toggle('hidden', !allowed);
    toggle.setAttribute('aria-pressed', String(open));
    toggle.title = open ? 'Hide the assistant' : 'Ask your meetings';
  }
  _syncAskRailMode();
}

/** Docked while the main column keeps ASK_DOCK_MIN_PX, otherwise an overlay. */
function _syncAskRailMode() {
  const rail = document.getElementById('ask-rail');
  const main = document.getElementById('main-area');
  if (!rail || !main || rail.hidden) return;
  const railW = rail.offsetWidth || 380;
  const docked = (main.clientWidth - railW) >= ASK_DOCK_MIN_PX;
  rail.classList.toggle('ask-overlay', !docked);
  document.body.classList.toggle('ask-overlaid', !docked);
}

function _syncHeaderForView(view) {
  const layout = document.getElementById('layout-control');
  if (layout) layout.classList.toggle('hidden', view !== 'session');
}

function _expandHeaderSearch() {
  const search = document.getElementById('header-search');
  if (search) search.classList.add('is-expanded');
  const input = document.getElementById('home-search-input');
  if (input) input.focus();
}

function _collapseHeaderSearch() {
  const search = document.getElementById('header-search');
  if (search) search.classList.remove('is-expanded');
}

let _refreshInFlight = false;

function _onRefreshClick() {
  if (_refreshInFlight) return;
  _refreshInFlight = true;
  const btn = document.getElementById('refresh-btn');
  if (btn) { btn.classList.add('is-spinning'); btn.disabled = true; }
  AppData.refreshActiveView()
    .catch(err => uiToast({ message: `Could not refresh: ${(err && err.message) || 'request failed'}`, kind: 'error' }))
    .then(() => {
      _refreshInFlight = false;
      if (btn) { btn.classList.remove('is-spinning'); btn.disabled = false; }
      _syncRefreshTooltip();
    });
}

function _relativeMinutes(ts) {
  if (!ts) return '';
  const secs = Math.max(0, (Date.now() - ts) / 1000);
  if (secs < 45) return 'just now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  return hours === 1 ? '1 hour ago' : `${hours} hours ago`;
}

function _syncRefreshTooltip() {
  const btn = document.getElementById('refresh-btn');
  if (!btn) return;
  const names = (VIEW_SLICES[Views.current] || ['sessions']).filter(n => n !== 'calendarEvents');
  const newest = names.reduce((acc, n) => Math.max(acc, AppData.lastUpdated(n)), 0);
  const when = _relativeMinutes(newest);
  btn.title = when ? `Refresh data · updated ${when}` : 'Refresh data';
  const stale = names.some(n => AppData.isStale(n));
  document.getElementById('header-stale')?.classList.toggle('hidden', !stale);
}

/* ── Capture setup row (the old status pill) ─────────────────────────────── */

function toggleCaptureSetup(force) {
  const panes = document.getElementById('capture-setup-panes');
  const row = document.getElementById('status-pill');
  if (!panes) return;
  const open = typeof force === 'boolean' ? force : panes.classList.contains('hidden');
  panes.classList.toggle('hidden', !open);
  if (row) row.setAttribute('aria-expanded', String(open));
  _saveLayoutCache({ capture_setup_open: open });
}

function _restoreCaptureSetup() {
  toggleCaptureSetup(_getLayoutCache().capture_setup_open === true);
}

/* ── Capture strip ───────────────────────────────────────────────────────── */

let _captureLastDesktopAudio = 0;
let _captureWarnTimer = null;

function _syncCaptureStrip() {
  const strip = document.getElementById('capture-strip');
  if (!strip) return;
  const live = !!state.isRecording;
  strip.hidden = !live;
  document.body.classList.toggle('is-recording', live);
  if (!live) {
    if (_captureWarnTimer) { clearInterval(_captureWarnTimer); _captureWarnTimer = null; }
    return;
  }
  const title = document.getElementById('capture-title');
  if (title) {
    const entry = _sidebarAllSessions.find(s => s.id === state.sessionId);
    title.textContent = (entry && entry.title) || 'New recording';
  }
  if (!_captureWarnTimer) {
    _captureLastDesktopAudio = Date.now();
    _captureWarnTimer = setInterval(_syncCaptureWarning, 1000);
    _syncCaptureWarning();
  }
}

function _syncCaptureWarning() {
  const el = document.getElementById('capture-warning');
  if (!el) return;
  if (!state.isRecording) { el.classList.add('hidden'); return; }
  const messages = [];
  if (Date.now() - _captureLastDesktopAudio > 20000) messages.push('No desktop audio for 20 s');
  const micSel = document.getElementById('viz-mic-sel');
  if (micSel && String(micSel.value) === '-1') messages.push('Mic muted');
  el.textContent = messages.join(' · ');
  el.classList.toggle('hidden', messages.length === 0);
}

/* ── Record button and its chevron ───────────────────────────────────────── */

/** Record always starts a NEW recording: a workspace showing a past meeting is
 *  blanked first so the server can never be asked to append to it by accident. */
async function startNewRecording() {
  if (state.isRecording) return;
  if (state.sessionId || state.isViewingPast) {
    await newSession();
    if (state.sessionId) return;      // the user cancelled out of newSession()
  }
  Views.show('session', { url: '/session' });
  await toggleRecording({ start: true });
}

/** The chevron's explicit "append new audio to this recording" action. */
async function resumeRecording() {
  if (state.isRecording || !state.isViewingPast || !state.sessionId) return;
  Views.show('session', { url: '/session?id=' + state.sessionId });
  await toggleRecording({ start: true, resume: true });
}

/* ── App menu ────────────────────────────────────────────────────────────── */

async function appUpdateItemClick() {
  closeMenu();
  const item = document.getElementById('app-update-item');
  if (item && item.dataset.available === '1') { confirmUpdateRestart(); return; }
  uiToast({ message: 'Checking for updates…', id: 'update-check', duration: 2500 });
  try {
    const data = await fetch('/api/update/check').then(r => r.json());
    if (data.error) { uiToast({ message: `Could not check for updates: ${data.error}`, kind: 'error', id: 'update-check' }); return; }
    if (!data.up_to_date && data.commits_behind > 0) {
      _showTopbarUpdate(data.commits_behind);
      uiToast({ message: `${data.commits_behind} update${data.commits_behind === 1 ? '' : 's'} available.`, kind: 'info', id: 'update-check',
                action: { label: 'Install and restart', onClick: () => confirmUpdateRestart() } });
    } else {
      uiToast({ message: 'Meeting Assistant is up to date.', kind: 'success', id: 'update-check' });
    }
  } catch (_) {
    uiToast({ message: 'Could not check for updates. Are you offline?', kind: 'error', id: 'update-check' });
  }
}

/** What's new: the same overlay the post-update popup uses, on demand. */
async function openWhatsNew() {
  closeMenu();
  try {
    const data = await fetch('/api/changelog').then(r => r.json());
    if (data && Array.isArray(data.entries) && data.entries.length) {
      _showWhatsNewPopup(data.entries[0]);
      return;
    }
  } catch (_) {}
  uiToast({ message: 'No release notes available yet.', kind: 'info' });
}

/* ── Pane toggle & column ordering ────────────────────────────────────────── */
// Indexed by column: [transcript, summary, chat, notes]. Notes is opt-in
// (off by default) so existing layouts continue to render three columns.
const _PANE_COUNT = 4;
// [transcript, summary, chat, notes]. Chat and Notes are off by default: chat
// crowds the workspace and is rarely used (re-enable it per session with the
// header toggle; the choice persists). Migration below hides chat in any
// pre-existing saved layout once.
let _paneVisible = [true, true, false, false];
const _COL_NAMES = ['Transcript', 'Summary', 'Chat', 'Notes'];

// One-time migration: turn the Chat pane (index 2) off in every saved layout,
// so the new hidden-by-default applies to existing users too. Runs once; after
// it, the user's own per-session chat toggles persist normally.
(function _migrateHideChatDefault() {
  try {
    if (localStorage.getItem('ma-hide-chat-v1')) return;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith('ma-panes:')) continue;
      try {
        const arr = JSON.parse(localStorage.getItem(k));
        if (Array.isArray(arr) && arr.length >= 3) {
          arr[2] = false;
          localStorage.setItem(k, JSON.stringify(arr));
        }
      } catch (_) {}
    }
    localStorage.setItem('ma-hide-chat-v1', '1');
  } catch (_) {}
})();

// Migrate legacy 3-element arrays from cache/localStorage into the 4-element shape.
function _normalizePaneArr(arr, fillTail) {
  if (!Array.isArray(arr)) return null;
  const out = arr.slice(0, _PANE_COUNT);
  while (out.length < _PANE_COUNT) out.push(fillTail);
  return out;
}

// Visual column order - maps position (left→right) to column index.
// Seeded from localStorage cache so the first paint uses the saved order.
let _colOrder = (() => {
  const lc = _getLayoutCache();
  const stored = _normalizePaneArr(lc.col_order, null);
  if (stored && stored.every(v => typeof v === 'number')) {
    // Ensure all column indices present (auto-append missing ones at the end)
    const seen = new Set(stored);
    for (let i = 0; i < _PANE_COUNT; i++) if (!seen.has(i)) stored.push(i);
    return stored.slice(0, _PANE_COUNT);
  }
  return [0, 1, 2, 3];
})();

// The three "positional" toggle buttons (transcript-shaped left rect,
// summary-shaped middle rect, chat-shaped right rect) keep a FIXED visual
// order. Each one targets whichever non-notes column is currently at the
// matching relative position (left/middle/right). The Notes button is the
// only one that floats - it slots in at whatever position the notes column
// occupies in _colOrder.
const _POSITIONAL_TOGGLE_BTN_IDS = [
  'pane-toggle-transcript',  // leftmost non-notes column
  'pane-toggle-summary',     // middle non-notes column
  'pane-toggle-chat',        // rightmost non-notes column
];
const _NOTES_TOGGLE_BTN_ID = 'pane-toggle-notes';
const _NOTES_COL_IDX = 3;

function _syncToggleButtons() {
  // Where does the notes column sit in the overall column order?
  const notesPos = _colOrder.indexOf(_NOTES_COL_IDX);
  // Slots available to the three positional buttons: all 4 toggle slots
  // minus the one occupied by the notes button.
  const positionalSlots = [0, 1, 2, 3].filter(p => p !== notesPos);
  // Non-notes columns in their current visual order - drives which actual
  // column each positional button controls + the dynamic tooltip.
  const nonNotesOrder = _colOrder.filter(c => c !== _NOTES_COL_IDX);

  const applyToggle = (btn, colIdx, slot) => {
    if (!btn) return;
    btn.style.order = String(slot);
    btn.onclick = () => togglePane(colIdx);
    btn.title = _COL_NAMES[colIdx];
    btn.setAttribute('aria-label', _COL_NAMES[colIdx]);
    btn.classList.toggle('active', _paneVisible[colIdx]);
    btn.setAttribute('aria-pressed', String(!!_paneVisible[colIdx]));
  };

  _POSITIONAL_TOGGLE_BTN_IDS.forEach((id, i) => {
    applyToggle(document.getElementById(id), nonNotesOrder[i], positionalSlots[i]);
  });
  applyToggle(document.getElementById(_NOTES_TOGGLE_BTN_ID), _NOTES_COL_IDX, notesPos);
}

function togglePane(idx) {
  // Don't allow hiding the last visible pane
  const visibleCount = _paneVisible.filter(Boolean).length;
  if (_paneVisible[idx] && visibleCount <= 1) return;

  _paneVisible[idx] = !_paneVisible[idx];
  _syncToggleButtons();
  _applyPaneLayout();
  _savePaneVisible();

  // Notes pane needs a one-shot init the first time it becomes visible
  if (idx === 3 && _paneVisible[3]) {
    _ensureNotesEditor();
    // Quill measures geometry on attach; if the column was display:none
    // during construction the toolbar/editor heights can be wrong.
    requestAnimationFrame(() => {
      try { if (_quill) _quill.update('silent'); } catch (_) {}
    });
  }
}

function _savePaneVisible() {
  const sid = state.sessionId;
  if (sid) {
    try { localStorage.setItem(`ma-panes:${sid}`, JSON.stringify(_paneVisible)); } catch (_) {}
  }
  // Also save as global default for new sessions
  try { localStorage.setItem('ma-panes:default', JSON.stringify(_paneVisible)); } catch (_) {}
}

function _loadPaneVisible(sessionId) {
  // Try session-specific first, then global default
  try {
    const raw = localStorage.getItem(`ma-panes:${sessionId}`)
             || localStorage.getItem('ma-panes:default');
    if (raw) {
      const arr = _normalizePaneArr(JSON.parse(raw), false);
      if (arr && arr.some(Boolean)) {
        _paneVisible = arr;
        _syncToggleButtons();
        _applyPaneLayout();
        if (_paneVisible[3]) _ensureNotesEditor();
        return;
      }
    }
  } catch (_) {}
  // Fallback: show transcript+summary, hide chat + notes (default layout)
  _paneVisible = [true, true, false, false];
  _syncToggleButtons();
  _applyPaneLayout();
}

function _applyPaneLayout() {
  const HANDLE_PX = 4;
  const MIN_COL_PX = 160;
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;

  // Stable column references (DOM order = column index, never changes)
  const colEls = [
    workspace.querySelector('.col-transcript'),
    workspace.querySelector('.col-summary'),
    workspace.querySelector('.col-chat'),
    workspace.querySelector('.col-notes'),
  ].filter(Boolean);
  const handles = Array.from(workspace.querySelectorAll('.col-resize-handle'));

  // Visible columns in visual (left→right) order
  const visOrder = _colOrder.filter(ci => _paneVisible[ci]);

  // Show/hide columns
  colEls.forEach((col, ci) => { col.style.display = _paneVisible[ci] ? '' : 'none'; });

  // Hide all handles, then show the ones needed between visible columns
  handles.forEach(h => { h.style.display = 'none'; });
  const shownHandles = [];
  for (let i = 0; i < visOrder.length - 1 && i < handles.length; i++) {
    handles[i].style.display = '';
    handles[i].dataset.left  = String(visOrder[i]);
    handles[i].dataset.right = String(visOrder[i + 1]);
    shownHandles.push(handles[i]);
  }

  // Assign CSS order so grid items match visual positions
  let ord = 0;
  visOrder.forEach((ci, i) => {
    colEls[ci].style.order = ord++;
    if (i < shownHandles.length) shownHandles[i].style.order = ord++;
  });
  // Push hidden columns out of the way
  colEls.forEach((col, ci) => { if (!_paneVisible[ci]) col.style.order = 99; });
  handles.forEach(h => { if (h.style.display === 'none') h.style.order = 99; });

  // Build grid template in visual order
  const total = workspace.offsetWidth - HANDLE_PX * shownHandles.length;
  const visFracs = visOrder.map(ci => _colProportions[ci]);
  const fracSum  = visFracs.reduce((a, b) => a + b, 0);
  const widths   = visFracs.map(f => Math.max(MIN_COL_PX, Math.round(total * f / fracSum)));

  const parts = [];
  for (let i = 0; i < widths.length; i++) {
    if (i > 0) parts.push(`${HANDLE_PX}px`);
    parts.push(`${widths[i]}px`);
  }
  workspace.style.gridTemplateColumns = parts.join(' ');
}

/* ── Resizable columns ────────────────────────────────────────────────────── */
// Relative column proportions - updated when user drags; loaded from settings on init.
// Seeded from localStorage cache immediately so the IIFE below uses the right values.
let _colProportions = (() => {
  const lc = _getLayoutCache();
  const stored = _normalizePaneArr(lc.col_proportions, 1.0);
  if (stored && stored.every(v => typeof v === 'number' && v > 0)) return stored;
  return [1, 1.1, 1.1, 1.0];
})();

function recalcColWidths() {
  _applyPaneLayout();
}

(function initResizableCols() {
  const HANDLE_PX  = 4;
  const MIN_COL_PX = 160;

  const workspace = document.querySelector('.workspace');
  if (!workspace) return;
  const handles   = Array.from(workspace.querySelectorAll('.col-resize-handle'));
  const numCols   = workspace.querySelectorAll('.col').length;
  if (!numCols || !handles.length) return;

  function getVisibleIndices() {
    return _colOrder.filter(ci => _paneVisible[ci]);
  }

  function getPixelWidths() {
    const vis = getVisibleIndices();
    const visHandles = Math.max(0, vis.length - 1);
    const total = workspace.offsetWidth - HANDLE_PX * visHandles;
    const visFracs = vis.map(i => _colProportions[i] || 1);
    const fracSum = visFracs.reduce((a, b) => a + b, 0);
    // Return full per-column array; hidden columns get 0
    const result = new Array(_PANE_COUNT).fill(0);
    vis.forEach((ci, vi) => {
      result[ci] = Math.max(MIN_COL_PX, Math.round(total * visFracs[vi] / fracSum));
    });
    return result;
  }

  function applyWidths(widths) {
    const vis = getVisibleIndices();
    const parts = [];
    vis.forEach((ci, vi) => {
      if (vi > 0) parts.push(`${HANDLE_PX}px`);
      parts.push(`${widths[ci]}px`);
    });
    workspace.style.gridTemplateColumns = parts.join(' ');
  }

  applyWidths(getPixelWidths());

  handles.forEach(handle => {
    handle.addEventListener('mousedown', e => {
      e.preventDefault();
      // data-left/data-right always store original column indices (0,1,2)
      const li = parseInt(handle.dataset.left, 10);
      const ri = parseInt(handle.dataset.right, 10);

      let widths       = getPixelWidths();
      const startX     = e.clientX;
      const startLeft  = widths[li];
      const startRight = widths[ri];

      handle.classList.add('dragging');
      document.body.style.cursor     = 'col-resize';
      document.body.style.userSelect = 'none';

      function onMove(ev) {
        const delta = ev.clientX - startX;
        let newLeft  = startLeft  + delta;
        let newRight = startRight - delta;
        if (newLeft  < MIN_COL_PX) { newLeft  = MIN_COL_PX; newRight = startLeft + startRight - MIN_COL_PX; }
        if (newRight < MIN_COL_PX) { newRight = MIN_COL_PX; newLeft  = startLeft + startRight - MIN_COL_PX; }
        widths[li] = Math.round(newLeft);
        widths[ri] = Math.round(newRight);
        applyWidths(widths);
      }

      function onUp() {
        handle.classList.remove('dragging');
        document.body.style.cursor     = '';
        document.body.style.userSelect = '';
        // Convert current pixel widths → proportions and save to settings + cache
        const vis = getVisibleIndices();
        const visWidths = vis.map(i => widths[i]);
        const total = visWidths.reduce((a, b) => a + b, 0);
        vis.forEach((ci, vi) => { _colProportions[ci] = visWidths[vi] / total; });
        if (typeof savePref === 'function') savePref('col_proportions', _colProportions);
        _saveLayoutCache({ col_proportions: _colProportions });
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup',   onUp);
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup',   onUp);
    });
  });

  // Coalesce resize bursts to one layout recompute per frame. recalcColWidths
  // reads workspace.offsetWidth and rewrites gridTemplateColumns, so running it
  // per raw resize event thrashes layout during a window drag.
  let _colResizeRaf = 0;
  window.addEventListener('resize', () => {
    if (_colResizeRaf) return;
    _colResizeRaf = requestAnimationFrame(() => {
      _colResizeRaf = 0;
      recalcColWidths();
    });
  });
})();

/* ── Column drag-to-reorder ──────────────────────────────────────────────── */
(function initColumnDragReorder() {
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;

  const colEls = [
    workspace.querySelector('.col-transcript'),
    workspace.querySelector('.col-summary'),
    workspace.querySelector('.col-chat'),
    workspace.querySelector('.col-notes'),
  ].filter(Boolean);
  if (!colEls.length) return;

  // Reusable floating ghost element
  const ghost = document.createElement('div');
  ghost.className = 'col-drag-ghost';
  document.body.appendChild(ghost);

  function positionGhost(x, y) {
    ghost.style.left = x + 12 + 'px';
    ghost.style.top  = y - 14 + 'px';
  }

  colEls.forEach((col, colIdx) => {
    const header = col.querySelector('.col-header');
    if (!header) return;

    header.addEventListener('mousedown', e => {
      // Don't hijack clicks on interactive elements
      if (e.target.closest('button, input, select, textarea, a, .badge')) return;

      const startX = e.clientX;
      const startY = e.clientY;
      let isDragging = false;

      function onMove(ev) {
        if (!isDragging && Math.abs(ev.clientX - startX) > 5) {
          isDragging = true;
          col.classList.add('col-dragging');
          document.body.style.cursor     = 'grabbing';
          document.body.style.userSelect = 'none';

          // Show ghost with column name
          ghost.textContent = _COL_NAMES[colIdx];
          positionGhost(ev.clientX, ev.clientY);
          // Force reflow before adding .visible so the transition plays
          ghost.offsetHeight;
          ghost.classList.add('visible');
        }
        if (!isDragging) return;

        positionGhost(ev.clientX, ev.clientY);

        // Highlight the column the cursor is over
        colEls.forEach((c, ci) => {
          if (ci === colIdx || !_paneVisible[ci]) {
            c.classList.remove('col-drag-over');
            return;
          }
          const r = c.getBoundingClientRect();
          c.classList.toggle('col-drag-over', ev.clientX >= r.left && ev.clientX <= r.right);
        });
      }

      function onUp(ev) {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup',   onUp);
        if (!isDragging) return;

        col.classList.remove('col-dragging');
        ghost.classList.remove('visible');
        document.body.style.cursor     = '';
        document.body.style.userSelect = '';

        // Find drop target
        let dropIdx = -1;
        colEls.forEach((c, ci) => {
          c.classList.remove('col-drag-over');
          if (ci !== colIdx && _paneVisible[ci]) {
            const r = c.getBoundingClientRect();
            if (ev.clientX >= r.left && ev.clientX <= r.right) dropIdx = ci;
          }
        });

        if (dropIdx >= 0) {
          // Swap positions in _colOrder
          const fromPos = _colOrder.indexOf(colIdx);
          const toPos   = _colOrder.indexOf(dropIdx);
          _colOrder[fromPos] = dropIdx;
          _colOrder[toPos]   = colIdx;

          _syncToggleButtons();
          _applyPaneLayout();
          savePref('col_order', [..._colOrder]);
          _saveLayoutCache({ col_order: [..._colOrder] });
        }
      }

      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup',   onUp);
    });
  });

  // Apply initial order (from cache/defaults)
  _syncToggleButtons();
  _applyPaneLayout();
})();

/* ── Sidebar resize handle ──────────────────────────────────────────────────
   The handle is the sidebar's one collapse control. Drag it inward past
   SIDEBAR_COLLAPSE_AT and the sidebar folds to the 48 px icon rail; drag the
   rail's edge back out and it expands. Double-click, or Enter / Space with the
   handle focused, toggles between the two. The open width is remembered across
   a collapse, so expanding returns to where it was. */
const SIDEBAR_MIN_W       = 280;
const SIDEBAR_MAX_W       = 440;
const SIDEBAR_RAIL_W      = 48;
const SIDEBAR_COLLAPSE_AT = 240;   // dragged narrower than this = collapse

(function initSidebarResize() {
  const sidebar = document.getElementById('sidebar');
  const handle  = document.getElementById('sidebar-resize-handle');
  if (!sidebar || !handle) return;

  const clampW = w => Math.max(SIDEBAR_MIN_W, Math.min(SIDEBAR_MAX_W, w));

  handle.addEventListener('mousedown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX;
    const startOpen = !sidebar.classList.contains('collapsed');
    // Measure from the rail's edge when it started collapsed, so the first
    // pixels of an outward drag are not a jump to full width.
    const startW = startOpen ? sidebar.offsetWidth : SIDEBAR_RAIL_W;
    let open = startOpen;
    let moved = false;

    handle.classList.add('dragging');
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';

    function onMove(ev) {
      const dx = ev.clientX - startX;
      if (Math.abs(dx) > 2) moved = true;
      const raw = startW + dx;
      const wantOpen = raw >= SIDEBAR_COLLAPSE_AT;
      if (wantOpen !== open) {
        open = wantOpen;
        sidebar.classList.toggle('collapsed', !open);
        if (!open) sidebar.style.width = '';   // the rail rule sizes it now
      }
      if (open) sidebar.style.width = clampW(raw) + 'px';
    }

    function onUp() {
      handle.classList.remove('dragging');
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
      if (!moved) return;   // a plain click, or one half of a double-click
      setSidebarOpen(open, { width: open ? sidebar.offsetWidth : null });
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
  });

  handle.addEventListener('dblclick', e => { e.preventDefault(); toggleSidebar(); });

  handle.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleSidebar(); return; }
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    const open = !sidebar.classList.contains('collapsed');
    if (!open) { if (e.key === 'ArrowRight') setSidebarOpen(true); return; }
    const next = sidebar.offsetWidth + (e.key === 'ArrowRight' ? 16 : -16);
    if (next < SIDEBAR_COLLAPSE_AT) setSidebarOpen(false);
    else setSidebarOpen(true, { width: clampW(next) });
  });

  // The collapsed rail expands on a click anywhere that is not a control of
  // its own: the logo, the empty run below the pages, the gaps between rows.
  // The page icons, the capture row and the footer keep their own jobs.
  const brandWrap = document.getElementById('brand-icon-wrap');
  sidebar.addEventListener('click', e => {
    if (!sidebar.classList.contains('collapsed')) return;
    if (brandWrap && brandWrap.contains(e.target)) { setSidebarOpen(true); return; }
    if (e.target.closest('a, button, input, select, textarea, label, [role="menuitem"]')) return;
    setSidebarOpen(true);
  });
  if (brandWrap) {
    brandWrap.addEventListener('keydown', e => {
      if (!sidebar.classList.contains('collapsed')) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSidebarOpen(true); }
    });
  }
  _syncBrandExpandAffordance();
})();

/** In rail mode the logo is a button that expands the sidebar; when open it is
 *  just the logo again. Keeps the title, role and tab stop in step. */
function _syncBrandExpandAffordance() {
  const sidebar = document.getElementById('sidebar');
  const wrap = document.getElementById('brand-icon-wrap');
  if (!sidebar || !wrap) return;
  if (sidebar.classList.contains('collapsed')) {
    wrap.title = 'Expand sidebar';
    wrap.setAttribute('role', 'button');
    wrap.setAttribute('aria-label', 'Expand sidebar');
    wrap.setAttribute('tabindex', '0');
  } else {
    wrap.removeAttribute('title');
    wrap.removeAttribute('role');
    wrap.removeAttribute('aria-label');
    wrap.removeAttribute('tabindex');
  }
}

function fmtDuration(secs) {
  secs = Math.floor(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

let _recordingStartTime = null;
let _durationInterval   = null;

/** One clock, three readouts: the capture setup row, the capture strip and
 *  the Record button's "Stop · mm:ss". */
function _writeElapsed(text) {
  const row = document.getElementById('recording-duration');
  if (row) row.textContent = text;
  const strip = document.getElementById('capture-time');
  if (strip) strip.textContent = text;
  const btn = document.getElementById('record-elapsed');
  if (btn) btn.textContent = text;
}

function startDurationCounter() {
  _recordingStartTime = Date.now();
  document.getElementById('recording-duration')?.classList.remove('hidden');
  _writeElapsed('0:00');
  _durationInterval = setInterval(() => {
    _writeElapsed(fmtDuration((Date.now() - _recordingStartTime) / 1000));
  }, 1000);
}

function stopDurationCounter() {
  clearInterval(_durationInterval);
  _durationInterval = null;
  _recordingStartTime = null;
  document.getElementById('recording-duration')?.classList.add('hidden');
  _writeElapsed('');
}

function jumpToTimestamp(seconds) {
  // Only seek audio when playback is active (recording finished, audio available)
  if (_playbackActive) {
    seekToTime(seconds);
  }
  // Scroll the matching transcript segment into view (works during recording too)
  const segs = [...document.querySelectorAll('.transcript-segment[data-start]')];
  if (!segs.length) return;
  // Prefer a segment whose range contains the timestamp; fall back to closest start
  let target = segs.find(seg =>
    seconds >= parseFloat(seg.dataset.start) && seconds < parseFloat(seg.dataset.end)
  );
  if (!target) {
    target = segs.reduce((best, seg) => {
      const d  = Math.abs(parseFloat(seg.dataset.start) - seconds);
      const bd = Math.abs(parseFloat(best.dataset.start) - seconds);
      return d < bd ? seg : best;
    });
  }
  if (target) {
    _doProgrammaticScroll(target, { behavior: 'smooth', block: 'center' });
    target.classList.add('playing');
    setTimeout(() => target.classList.remove('playing'), 2000);
  }
}

/* ── App state ───────────────────────────────────────────────────────────── */
const state = {
  sessionId:      null,
  isRecording:    false,
  isTesting:      false,
  isViewingPast:  false,
  isReanalyzing:  false,
  sessionHasAudio: false,
  aiChatBusy:     false,
  modelReady:     false,
  diarizerReady:  false,
  recordingReady: false,
  recordingReadyReason: 'Loading transcription model...',
  modelInfo:      '',
  chatCursor:     null,
  chatBuffer:     '',
  chatToolCalls:  [],
  summaryBuffer:    '',
  summaryCursor:    null,
  summaryStreaming: false,
  sidebarOpen:    true,
};

// Per-session summary stream tracking: { [sessionId]: { buffer, streaming, mode } }
const _summaryStreams = {};

// Apply sidebar layout from cache synchronously - eliminates flash before async prefs load
{
  const _lc = _getLayoutCache();
  const _sb = document.getElementById('sidebar');
  if (_sb) {
    if (_lc.sidebar_width) _sb.style.width = _lc.sidebar_width + 'px';
    if (_lc.sidebar_open === false) {
      _sb.classList.add('collapsed');
      _sb.style.width = '';   // let the CSS icon-rail width take over
      state.sidebarOpen = false;
    }
  }
}

/* ── Preferences (server-persisted) ─────────────────────────────────────── */
let _prefs = {};   // populated on init from /api/preferences
let _prefsSaveTimer = null;
let _prefsPending = {};   // keys changed since the last flush; only these are sent

async function loadPreferences() {
  try {
    _prefs = await fetch('/api/preferences').then(r => r.json());
  } catch { _prefs = {}; }

  // Update localStorage cache with authoritative server values so future
  // page loads can apply them synchronously (no flash).
  const cacheUpdate = {};
  if (Array.isArray(_prefs.col_proportions))    cacheUpdate.col_proportions = _prefs.col_proportions;
  if (Array.isArray(_prefs.col_order))          cacheUpdate.col_order       = _prefs.col_order;
  if (_prefs.sidebar_width)                      cacheUpdate.sidebar_width   = _prefs.sidebar_width;
  if (typeof _prefs.sidebar_open === 'boolean')  cacheUpdate.sidebar_open    = _prefs.sidebar_open;
  if (Object.keys(cacheUpdate).length) _saveLayoutCache(cacheUpdate);

  // Apply sidebar width (server value may differ from cached, e.g. on another device)
  if (_prefs.sidebar_width) {
    const sb = document.getElementById('sidebar');
    if (sb && state.sidebarOpen) sb.style.width = _prefs.sidebar_width + 'px';
  }
  // Apply column proportions and order. Migrate legacy 3-element arrays
  // saved before the Notes pane shipped by appending sensible defaults.
  if (Array.isArray(_prefs.col_proportions)) {
    const norm = _normalizePaneArr(_prefs.col_proportions, 1.0);
    if (norm) _colProportions = norm;
  }
  if (Array.isArray(_prefs.col_order)) {
    const norm = _normalizePaneArr(_prefs.col_order, null);
    if (norm) {
      // Append any column indices missing from the saved order
      const seen = new Set(norm.filter(v => typeof v === 'number'));
      const out = norm.filter(v => typeof v === 'number');
      for (let i = 0; i < _PANE_COUNT; i++) if (!seen.has(i)) out.push(i);
      _colOrder = out.slice(0, _PANE_COUNT);
      _syncToggleButtons();
    }
  }
  // Apply sidebar collapsed state on load.
  const sidebar = document.getElementById('sidebar');
  if (_prefs.sidebar_open === false && state.sidebarOpen) {
    state.sidebarOpen = true;
    toggleSidebar();
  } else if (_prefs.sidebar_open !== false && !state.sidebarOpen) {
    state.sidebarOpen = false;
    toggleSidebar();
  }
  recalcColWidths();
  applySidebarNavPrefs();
  // Apply auto-summary toggle
  const autoBtn = document.getElementById('auto-summary-btn');
  if (autoBtn) {
    const enabled = _prefs.auto_summary !== false;
    autoBtn.classList.toggle('active', enabled);
  }
  // Reconcile server-authoritative theme with what we applied pre-paint from
  // localStorage. If the server has values, apply them (may differ if changed
  // on another device). Otherwise fall back to the local cache so the UI
  // still reflects the user's last choice even if a save hadn't flushed yet.
  if (_prefs.theme_mode || _prefs.theme_accent || _prefs.theme_custom) {
    applyTheme(_prefs.theme_mode || 'system', _prefs.theme_accent || 'blue');
    const cache = {
      theme_mode:   _prefs.theme_mode   || 'system',
      theme_accent: _prefs.theme_accent || 'blue',
    };
    if (_prefs.theme_custom) cache.theme_custom = _prefs.theme_custom;
    _saveLayoutCache(cache);
  } else {
    const lc = _getLayoutCache();
    if (lc.theme_mode)    _prefs.theme_mode    = lc.theme_mode;
    if (lc.theme_accent)  _prefs.theme_accent  = lc.theme_accent;
    if (lc.theme_custom)  _prefs.theme_custom  = lc.theme_custom;
  }
  _syncThemeUI();
  // Populate the global chat/summary/title system-prompt textareas (if rendered)
  _syncGlobalChatPromptUI();
  _syncGlobalSummaryPromptUI();
  _syncGlobalTitlePromptUI();
  // Apply saved sidebar filter default (if any) on top of session list
  try { _loadSidebarFilterDefault(); } catch (_) {}
  // Refresh session-override badge (no-op on home page)
  if (state.sessionId) refreshSessionChatPromptBadge();
}

/**
 * Persist one preference. Only the keys changed since the last flush are sent;
 * the server merges them into what it has stored. This once sent the whole
 * _prefs object, and every open page holds its own copy from whenever that page
 * loaded, so an older tab undid any change made elsewhere the moment it saved
 * anything: enabling the calendar in Settings and then resizing the sidebar on
 * another page switched the calendar off again.
 */
function savePref(key, value) {
  _prefs[key] = value;
  _prefsPending[key] = value;
  // Debounce writes so rapid changes don't flood the server
  clearTimeout(_prefsSaveTimer);
  _prefsSaveTimer = setTimeout(_flushPrefs, 400);
}

function _flushPrefs(keepalive = false) {
  clearTimeout(_prefsSaveTimer);
  _prefsSaveTimer = null;
  if (!Object.keys(_prefsPending).length) return;
  const body = JSON.stringify(_prefsPending);
  _prefsPending = {};
  fetch('/api/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body,
    keepalive: keepalive === true,
  }).catch(() => {});
}

// A change made in the last 400 ms before the tab closes still lands.
window.addEventListener('pagehide', () => _flushPrefs(true));

/* ── Theme (light/dark + accent) ──────────────────────────────────────────── */
const THEME_MODES   = ['system', 'light', 'dark'];
const THEME_ACCENTS = ['blue', 'ocean', 'forest', 'sunset', 'rose', 'violet', 'amber', 'crimson', 'mono', 'custom'];
const HLJS_DARK  = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css';
const HLJS_LIGHT = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css';
const THEME_CUSTOM_DEFAULT = { accent: '#58a6ff', strength: 30 };

// Base surface palettes used as the starting point for custom theme blending
const THEME_BASE = {
  dark: {
    bg: '#0d1117', surface: '#161b22', surface2: '#21262d', surface3: '#2d333b',
    surface4: '#0a0d10', sub_panel_bg: '#0c0e10', border: '#484f58',
  },
  light: {
    bg: '#ffffff', surface: '#f6f8fa', surface2: '#eaeef2', surface3: '#d8dee4',
    surface4: '#ffffff', sub_panel_bg: '#f6f8fa', border: '#d0d7de',
  },
};
const THEME_CUSTOM_VARS = [
  '--accent', '--accent-dim', '--accent-dim2', '--vertical-sep-color',
  '--bg', '--surface', '--surface2', '--surface3', '--surface4',
  '--sub-panel-bg', '--sub-panel-bg-trans', '--border', '--border-sub',
];

function _effectiveThemeMode(mode) {
  if (mode === 'system') {
    return (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
  }
  return mode === 'light' ? 'light' : 'dark';
}

// Small color utils - all work in sRGB space, good enough for UI tinting.
function _hexToRgb(hex) {
  hex = (hex || '').trim().replace(/^#/, '');
  if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
  if (!/^[0-9a-fA-F]{6}$/.test(hex)) return null;
  return { r: parseInt(hex.slice(0,2),16), g: parseInt(hex.slice(2,4),16), b: parseInt(hex.slice(4,6),16) };
}
function _rgbToHex(r, g, b) {
  const c = n => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0');
  return '#' + c(r) + c(g) + c(b);
}
function _blendHex(a, b, t) {
  const A = _hexToRgb(a), B = _hexToRgb(b);
  if (!A || !B) return a;
  return _rgbToHex(A.r + (B.r - A.r) * t, A.g + (B.g - A.g) * t, A.b + (B.b - A.b) * t);
}
function _normalizeHex(s) {
  s = (s || '').trim();
  if (!s.startsWith('#')) s = '#' + s;
  if (s.length === 4) s = '#' + s.slice(1).split('').map(c => c + c).join('');
  return /^#[0-9a-fA-F]{6}$/.test(s) ? s.toLowerCase() : null;
}

// Derive the full custom palette (returns a dict of CSS var name → value).
function _deriveCustomPalette(hex, strength, mode) {
  hex = _normalizeHex(hex) || THEME_CUSTOM_DEFAULT.accent;
  const t = Math.max(0, Math.min(1, (strength == null ? 30 : strength) / 100));
  const base = THEME_BASE[mode === 'light' ? 'light' : 'dark'];
  const isDark = mode !== 'light';
  const mixAmt = t * 0.13;  // cap surface blending at ~13% (very subtle at max)
  // Accent derivatives
  const accent     = hex;
  const accentDim  = _blendHex(hex, '#000000', 0.28);
  const accentDim2 = isDark ? _blendHex(hex, '#000000', 0.82) : _blendHex(hex, '#ffffff', 0.88);
  const out = {
    '--accent': accent,
    '--accent-dim': accentDim,
    '--accent-dim2': accentDim2,
    '--vertical-sep-color': hex + (isDark ? 'ad' : '73'),
    '--bg':           _blendHex(base.bg,           hex, mixAmt),
    '--surface':      _blendHex(base.surface,      hex, mixAmt),
    '--surface2':     _blendHex(base.surface2,     hex, mixAmt),
    '--surface3':     _blendHex(base.surface3,     hex, mixAmt),
    '--surface4':     _blendHex(base.surface4,     hex, mixAmt),
    '--sub-panel-bg': _blendHex(base.sub_panel_bg, hex, mixAmt),
    '--border-sub':   _blendHex(base.surface2,     hex, mixAmt),
  };
  // Border keeps the base 36% alpha (5c) in dark, solid in light
  const borderHex = _blendHex(base.border, hex, mixAmt);
  out['--border'] = isDark ? borderHex + '5c' : borderHex;
  // Semi-transparent sub-panel (bd in dark = ~74% alpha, d9 in light = ~85%)
  out['--sub-panel-bg-trans'] = out['--sub-panel-bg'] + (isDark ? 'bd' : 'd9');
  return out;
}

function _applyCustomPalette(vars) {
  const root = document.documentElement;
  for (const [k, v] of Object.entries(vars)) root.style.setProperty(k, v);
}
function _clearCustomPalette() {
  const root = document.documentElement;
  for (const k of THEME_CUSTOM_VARS) root.style.removeProperty(k);
}

function applyTheme(mode, accent) {
  if (!THEME_MODES.includes(mode))   mode   = 'system';
  if (!THEME_ACCENTS.includes(accent)) accent = 'blue';
  const effective = _effectiveThemeMode(mode);
  document.documentElement.dataset.themeMode = effective;
  if (accent === 'blue') {
    delete document.documentElement.dataset.accent;
  } else {
    document.documentElement.dataset.accent = accent;
  }
  // Clear any prior inline custom vars unless we're about to re-apply them
  if (accent !== 'custom') _clearCustomPalette();
  // Swap hljs stylesheet to match
  const link = document.getElementById('hljs-theme');
  if (link) {
    const target = effective === 'light' ? HLJS_LIGHT : HLJS_DARK;
    if (link.href !== target) link.href = target;
  }
  // If custom, re-derive and apply (mode-sensitive)
  if (accent === 'custom') {
    const cfg = _prefs.theme_custom || THEME_CUSTOM_DEFAULT;
    const palette = _deriveCustomPalette(cfg.accent, cfg.strength, effective);
    _applyCustomPalette(palette);
    // Cache computed palette per-mode so pre-paint has zero flash next load
    _saveLayoutCache({ ['theme_custom_' + effective]: palette });
  }
}

function setThemeMode(mode) {
  if (!THEME_MODES.includes(mode)) return;
  const accent = _prefs.theme_accent || 'blue';
  applyTheme(mode, accent);
  _saveLayoutCache({ theme_mode: mode });
  savePref('theme_mode', mode);
  _syncThemeUI();
}

function setThemeAccent(accent) {
  if (!THEME_ACCENTS.includes(accent)) return;
  const mode = _prefs.theme_mode || 'system';
  applyTheme(mode, accent);
  _saveLayoutCache({ theme_accent: accent });
  savePref('theme_accent', accent);
  _syncThemeUI();
}

/* ── Custom accent picker handlers ────────────────────────────────────────── */
function _getCustomCfg() {
  return { ...THEME_CUSTOM_DEFAULT, ...(_prefs.theme_custom || {}) };
}

function updateCustomAccent(hex) {
  const clean = _normalizeHex(hex);
  if (!clean) return;
  const cfg = _getCustomCfg();
  cfg.accent = clean;
  _prefs.theme_custom = cfg;
  const hexInput = document.getElementById('theme-custom-accent-hex');
  if (hexInput && hexInput.value.toLowerCase() !== clean) hexInput.value = clean;
  // If custom isn't the active accent yet, switching to it applies automatically
  if (_prefs.theme_accent !== 'custom') {
    setThemeAccent('custom');
  } else {
    applyTheme(_prefs.theme_mode || 'system', 'custom');
  }
  _saveLayoutCache({ theme_custom: cfg });
  savePref('theme_custom', cfg);
}

function updateCustomAccentFromHex(value) {
  const clean = _normalizeHex(value);
  if (!clean) return;  // wait for a valid 6-digit hex
  const picker = document.getElementById('theme-custom-accent-picker');
  if (picker) picker.value = clean;
  updateCustomAccent(clean);
}

function updateCustomStrength(value) {
  const n = Math.max(0, Math.min(100, parseInt(value, 10) || 0));
  const cfg = _getCustomCfg();
  cfg.strength = n;
  _prefs.theme_custom = cfg;
  const lbl = document.getElementById('theme-custom-strength-val');
  if (lbl) lbl.textContent = n + '%';
  if (_prefs.theme_accent === 'custom') {
    applyTheme(_prefs.theme_mode || 'system', 'custom');
  }
  _saveLayoutCache({ theme_custom: cfg });
  savePref('theme_custom', cfg);
}

function resetCustomTheme() {
  const cfg = { ...THEME_CUSTOM_DEFAULT };
  _prefs.theme_custom = cfg;
  const picker = document.getElementById('theme-custom-accent-picker');
  const hexIn  = document.getElementById('theme-custom-accent-hex');
  const slider = document.getElementById('theme-custom-strength');
  const lbl    = document.getElementById('theme-custom-strength-val');
  if (picker) picker.value = cfg.accent;
  if (hexIn)  hexIn.value  = cfg.accent;
  if (slider) slider.value = cfg.strength;
  if (lbl)    lbl.textContent = cfg.strength + '%';
  if (_prefs.theme_accent === 'custom') {
    applyTheme(_prefs.theme_mode || 'system', 'custom');
  }
  _saveLayoutCache({ theme_custom: cfg });
  savePref('theme_custom', cfg);
}

function _syncThemeUI() {
  const mode   = _prefs.theme_mode   || 'system';
  const accent = _prefs.theme_accent || 'blue';
  document.querySelectorAll('.theme-mode-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });
  document.querySelectorAll('.theme-accent-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.accent === accent);
  });
  // Show/hide custom picker panel
  const panel = document.getElementById('theme-custom-panel');
  if (panel) panel.classList.toggle('hidden', accent !== 'custom');
  // Seed picker inputs with current custom config
  const cfg = _getCustomCfg();
  const picker = document.getElementById('theme-custom-accent-picker');
  const hexIn  = document.getElementById('theme-custom-accent-hex');
  const slider = document.getElementById('theme-custom-strength');
  const lbl    = document.getElementById('theme-custom-strength-val');
  if (picker && picker.value !== cfg.accent) picker.value = cfg.accent;
  if (hexIn  && hexIn.value  !== cfg.accent) hexIn.value  = cfg.accent;
  if (slider && +slider.value !== cfg.strength) slider.value = cfg.strength;
  if (lbl) lbl.textContent = cfg.strength + '%';
}

// React to OS-level light/dark changes while in "system" mode
if (window.matchMedia) {
  try {
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if ((_prefs.theme_mode || 'system') === 'system') {
        applyTheme('system', _prefs.theme_accent || 'blue');
      }
    });
  } catch (_) { /* Safari <14 lacks addEventListener on MQL; non-critical */ }
}

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
/** Open or collapse the sidebar, persist it, and re-flow what depends on it.
 *  opts.width (px) becomes the remembered open width when given. */
function setSidebarOpen(open, opts) {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const width = opts && opts.width;
  state.sidebarOpen = !!open;
  if (open) {
    sidebar.classList.remove('collapsed');
    const w = width || _prefs.sidebar_width;
    if (w) sidebar.style.width = w + 'px';   // the remembered width beats the CSS default
  } else {
    sidebar.classList.add('collapsed');
    sidebar.style.width = '';                // the 48 px rail rule takes over
  }
  savePref('sidebar_open', state.sidebarOpen);
  const cache = { sidebar_open: state.sidebarOpen };
  if (open && width) {
    _prefs.sidebar_width = width;
    savePref('sidebar_width', width);
    cache.sidebar_width = width;
  }
  _saveLayoutCache(cache);
  recalcColWidths();
  _syncBrandExpandAffordance();
  // The rail always shows the pages; the open sidebar honours the fold.
  if (typeof _prefs !== 'undefined') applySidebarNavPrefs();
}

function toggleSidebar() { setSidebarOpen(!state.sidebarOpen); }

/* ── Sidebar navigation: which pages show, and whether they fold into the brand
 *    row. Both are preferences. Needs attention is off by default: the Home
 *    dashboard already lists what needs speaker work, and the sidebar's height
 *    is better spent on the recordings list. ───────────────────────────────── */
const _NAV_KEYS = ['home', 'calendar', 'attention', 'speakers'];
const _NAV_DEFAULT_ITEMS = { home: true, calendar: true, attention: false, speakers: true };

function _navItems() {
  const saved = (_prefs.sidebar_nav_items && typeof _prefs.sidebar_nav_items === 'object')
    ? _prefs.sidebar_nav_items : {};
  return Object.assign({}, _NAV_DEFAULT_ITEMS, saved);
}

function applySidebarNavPrefs() {
  const sidebar = document.getElementById('sidebar');
  const nav = sidebar && sidebar.querySelector('.sidebar-nav');
  const brandNav = document.getElementById('brand-nav');
  if (!sidebar || !nav || !brandNav) return;
  const items = _navItems();
  const compact = !!_prefs.sidebar_nav_compact;
  // Folding is about the open sidebar only: the icon rail always shows the
  // pages, so a collapsed sidebar keeps them in the nav column.
  const collapsed = sidebar.classList.contains('collapsed');
  const rows = [...sidebar.querySelectorAll('.nav-row[data-nav-key]')];
  const host = (compact && !collapsed) ? brandNav : nav;
  // The same anchors either way, moved rather than duplicated, so every id
  // app.js binds (#attention-control, #attention-count) keeps working in both
  // homes and the router keeps marking the current page.
  _NAV_KEYS.forEach(key => {
    const row = rows.find(r => r.dataset.navKey === key);
    if (!row) return;
    row.classList.toggle('nav-hidden', items[key] === false);
    const label = row.querySelector('.nav-label');
    if ((compact || collapsed) && label) row.title = label.textContent.trim();
    else row.removeAttribute('title');
    if (row.parentElement !== host) host.appendChild(row);
  });
  // The open sidebar lays the pages out as tiles, two across. An odd last
  // page stretches across both columns, so four pages make a 2 x 2 block,
  // three make a row plus a full-width row, and two make one row.
  const shown = _NAV_KEYS
    .map(key => rows.find(r => r.dataset.navKey === key))
    .filter(row => row && items[row.dataset.navKey] !== false);
  rows.forEach(row => row.classList.remove('nav-span'));
  if (shown.length % 2 === 1) shown[shown.length - 1].classList.add('nav-span');
  sidebar.classList.toggle('nav-compact', compact);
  const handle = document.getElementById('nav-fold-handle');
  if (handle) {
    handle.title = compact ? 'Expand the navigation' : 'Fold the navigation into the header';
    handle.setAttribute('aria-label', handle.title);
    handle.setAttribute('aria-expanded', compact ? 'false' : 'true');
    const g = handle.querySelector('.nav-fold-glyph');
    if (g) g.className = 'nav-fold-glyph fa-solid ' + (compact ? 'fa-chevron-down' : 'fa-chevron-up');
  }
  // A narrow sidebar in compact mode keeps the icons and drops the wordmark.
  const narrow = () => sidebar.classList.toggle('brand-narrow', sidebar.offsetWidth > 0 && sidebar.offsetWidth < 330);
  if (!applySidebarNavPrefs._obs && typeof ResizeObserver !== 'undefined') {
    applySidebarNavPrefs._obs = new ResizeObserver(narrow);
    applySidebarNavPrefs._obs.observe(sidebar);
  }
  narrow();
  _syncNavEditMenu();
  _syncSettingsNavUI();
}

/** The page picker's ticks follow the preference. */
function _syncNavEditMenu() {
  const items = _navItems();
  _NAV_KEYS.forEach(key => {
    const el = document.getElementById('nav-edit-' + key);
    if (el) el.setAttribute('aria-checked', items[key] !== false ? 'true' : 'false');
  });
}

/** A page picker item: flip that page. The menu stays open for the next tick. */
function toggleNavItem(key) {
  const items = _navItems();
  setNavItemVisible(key, items[key] === false);
}

function toggleNavCompact() { setNavCompact(!_prefs.sidebar_nav_compact); }

function setNavCompact(on) {
  // The picker's button goes away while folded, so the picker goes with it.
  if (on && typeof closeMenu === 'function') closeMenu();
  savePref('sidebar_nav_compact', !!on);
  applySidebarNavPrefs();
}

function setNavItemVisible(key, on) {
  if (!_NAV_KEYS.includes(key)) return;
  const items = _navItems();
  items[key] = !!on;
  savePref('sidebar_nav_items', items);
  applySidebarNavPrefs();
}

function _syncSettingsNavUI() {
  const compact = document.getElementById('nav-compact-toggle');
  if (compact) compact.checked = !!_prefs.sidebar_nav_compact;
  const items = _navItems();
  _NAV_KEYS.forEach(key => {
    const cb = document.getElementById('nav-item-' + key);
    if (cb) cb.checked = items[key] !== false;
  });
}

// ── Sidebar state ─────────────────────────────────────────────────────────────
let _sidebarSelected    = new Set();      // selected session IDs
let _sidebarMultiselect = false;          // multiselect mode on/off
let _sidebarCollapsed   = (() => {        // collapsed folder IDs - persisted in localStorage
  try { return new Set(JSON.parse(localStorage.getItem(_FOLDER_STATE_KEY) || '[]')); }
  catch (_) { return new Set(); }
})();
let _sidebarAllSessions = [];             // last fetch result
let _sidebarFolders     = [];             // last fetch result
let _sidebarDragIds     = [];             // IDs being dragged
let _sidebarDragType    = 'session';      // 'session' | 'folder'
let _dragIndicator      = null;           // reusable drop indicator element
let _dragDescendants    = new Set();      // descendants of dragged folder (cycle prevention)
let _sidebarSearchQuery = '';             // current search text
let _sidebarSearchResults = null;         // null = not searching, Map<sessionId, {matches}>
let _sidebarSearchTimer = null;           // debounce timer
let _semanticSearchReady = false;         // true once backend model is loaded
let _semanticSearchPending = false;       // true while a semantic request is in flight
let _ftsSearchPending = false;            // true while FTS request is in flight
let _pendingSearchHighlight = null;       // {segmentId, query} - scroll+highlight after session load

// A speaker label that still needs a real identity: a diarizer placeholder
// ("Speaker 3"), an import stand-in ("Other participant"), or an explicit
// unknown.
const _GENERIC_SPEAKER_RE =
  /^(speaker\s*\d+|other participant(\s*\d+)?|unknown|unidentified|guest|participant\s*\d+)$/i;

// ── Sidebar filter state ──────────────────────────────────────────────────────
const _SIDEBAR_FILTER_DEFAULTS = Object.freeze({
  datePreset: 'any',          // any | today | yesterday | 7d | 30d | thisMonth | thisYear | custom
  dateFrom: '',               // ISO yyyy-mm-dd (only with datePreset==='custom')
  dateTo: '',
  durationPreset: 'any',      // any | lt5 | 5to15 | 15to30 | 30to60 | gt60 | custom
  durMin: '',                 // minutes
  durMax: '',
  folders: [],                // folder IDs; special tokens: '__uncat__'
  speakers: [],               // speaker names (lowercase)
  hasAudio: 'any',            // any | yes | no
  hasTranscript: 'any',       // any | yes | no
  status: 'any',              // any | done | inprog
  splitGroup: 'any',          // any | yes | no
  speakersResolved: 'any',    // any | unresolved (has generic labels) | resolved (all named)
  attention: 'any',           // any | needs
  spkCountMin: '',
  spkCountMax: '',
  sortBy: 'date_desc',        // date_desc | date_asc | title_asc | title_desc | duration_desc | duration_asc | speakers_desc
});

let _sidebarFilter = { ..._SIDEBAR_FILTER_DEFAULTS };
let _sidebarFilterDefault = { ..._SIDEBAR_FILTER_DEFAULTS };
let _sidebarFilterPopoverOpen = false;

// Collapse state for collapsible sections (variable-length lists). Persisted
// to localStorage so refreshes preserve the user's preference. Default is
// collapsed for every collapsible section on first run.
const _SF_COLLAPSE_KEY = 'ma-sidebar-filter-open-sections';
const _SF_COLLAPSIBLE = new Set(['folders', 'speakers']);
let _sidebarFilterOpenSections = (() => {
  try { return new Set(JSON.parse(localStorage.getItem(_SF_COLLAPSE_KEY) || '[]')); }
  catch (_) { return new Set(); }
})();

// ResizeObserver that re-anchors the popover when the sidebar is resized
let _sidebarFilterResizeObserver = null;

/** The count beside the "Recordings" header, from the same slice as the list. */
function _syncRecordingsCount() {
  const el = document.getElementById('recordings-count');
  if (!el) return;
  const n = _sidebarAllSessions.length;
  el.textContent = n ? String(n) : '';
}

/** The sessions and folders slices landed: re-read them and repaint. The two
 *  aliases stay so search, filtering, drag and drop and multiselect are
 *  untouched by the store. */
function _onSidebarSlices() {
  _sidebarAllSessions = AppData.get('sessions') || [];
  _sidebarFolders = AppData.get('folders') || [];
  _renderSidebar();
  // Bootstrap race: if a session was opened via URL before the list arrived,
  // expand its ancestors now that we know the folder tree.
  if (typeof state !== 'undefined' && state.sessionId) {
    _revealSessionInSidebar(state.sessionId);
  }
  // The workspace title comes from this slice, so it lands with it.
  updateTopbarSessionTitle();
  _syncCaptureStrip();
}

/* ── Sidebar search ───────────────────────────────────────────────────────── */
function _pulseSearchGlow() {
  const body = document.getElementById('session-list');
  if (!body) return;
  body.classList.remove('search-glow');
  void body.offsetWidth;          // force reflow - restarts animation instantly
  body.classList.add('search-glow');
}

function _onSidebarSearch(value) {
  _sidebarSearchQuery = value.trim();
  const clearBtn = document.getElementById('sidebar-search-clear');
  if (clearBtn) clearBtn.classList.toggle('hidden', !_sidebarSearchQuery);

  if (!_sidebarSearchQuery) {
    _sidebarSearchResults = null;
    _semanticSearchPending = false;
    _ftsSearchPending = false;
    _renderSidebar();
    return;
  }

  // Fire a subtle glow at the top of the results pane for keystroke feedback
  _pulseSearchGlow();

  // Instant client-side title filter
  const q = _sidebarSearchQuery.toLowerCase();
  const titleMatches = new Map();
  for (const s of _sidebarAllSessions) {
    if (s.title && s.title.toLowerCase().includes(q)) {
      titleMatches.set(s.id, { matches: [{ kind: 'title', snippet: _highlightSnippet(s.title, q) }] });
    }
  }

  const prevSize = _sidebarSearchResults ? _sidebarSearchResults.size : -1;
  _sidebarSearchResults = titleMatches;
  _ftsSearchPending = true;
  if (_semanticSearchReady) _semanticSearchPending = true;

  // Skip full re-render if we're already showing "Searching…" with no results
  // - avoids restarting the dots animation on every keystroke
  const stillEmpty = prevSize === 0 && titleMatches.size === 0;
  if (!stillEmpty) _renderSidebar();

  // Debounced backend FTS + semantic search
  clearTimeout(_sidebarSearchTimer);
  _sidebarSearchTimer = setTimeout(() => {
    _runBackendSearch(_sidebarSearchQuery);
    if (_semanticSearchReady) _runSemanticSearch(_sidebarSearchQuery);
  }, 250);
}

async function _runBackendSearch(query) {
  if (query !== _sidebarSearchQuery) return;  // stale
  try {
    const results = await fetch(`/api/search?q=${encodeURIComponent(query)}`).then(r => r.json());
    if (query !== _sidebarSearchQuery) return;  // stale
    // Merge with existing title matches
    const merged = new Map(_sidebarSearchResults || []);
    for (const r of results) {
      if (merged.has(r.session_id)) {
        const existing = merged.get(r.session_id);
        const contentMatches = r.matches.filter(m => m.kind !== 'title');
        existing.matches = [...existing.matches, ...contentMatches].slice(0, 3);
      } else {
        merged.set(r.session_id, { matches: r.matches });
      }
    }
    _ftsSearchPending = false;
    _sidebarSearchResults = merged;
    _renderSidebar();
  } catch {
    _ftsSearchPending = false;
  }
}

async function _runSemanticSearch(query) {
  if (query !== _sidebarSearchQuery) return;
  try {
    const resp = await fetch(`/api/search/semantic?q=${encodeURIComponent(query)}`);
    if (query !== _sidebarSearchQuery) return;
    if (!resp.ok) {
      _semanticSearchPending = false;
      _renderSidebar();
      return;
    }
    const results = await resp.json();
    if (query !== _sidebarSearchQuery) return;
    // Merge semantic results into existing results
    const merged = new Map(_sidebarSearchResults || []);
    for (const r of results) {
      if (merged.has(r.session_id)) {
        const existing = merged.get(r.session_id);
        // Add semantic matches + score, avoid duplicates
        const semMatches = (r.matches || []).filter(m => m.kind === 'semantic');
        existing.matches = [...existing.matches, ...semMatches].slice(0, 3);
        existing.score = Math.max(existing.score || 0, r.score || 0);
      } else {
        merged.set(r.session_id, {
          matches: r.matches || [],
          score: r.score,
        });
      }
    }
    _semanticSearchPending = false;
    _sidebarSearchResults = merged;
    _renderSidebar();
  } catch {
    _semanticSearchPending = false;
  }
}

function _clearSidebarSearch() {
  const input = document.getElementById('sidebar-search-input');
  if (input) input.value = '';
  _onSidebarSearch('');
}

/* ── Sidebar filter ───────────────────────────────────────────────────────── */

function _filterIsActive(f) {
  const d = _SIDEBAR_FILTER_DEFAULTS;
  for (const k of Object.keys(d)) {
    const dv = d[k], v = f[k];
    if (Array.isArray(dv)) { if ((v || []).length) return true; }
    else if (v !== dv && v !== '' && v != null) return true;
  }
  return false;
}

function _activeFilterCount(f) {
  let n = 0;
  if (f.datePreset !== 'any') n++;
  if (f.durationPreset !== 'any') n++;
  if ((f.folders || []).length) n++;
  if ((f.speakers || []).length) n++;
  if (f.hasAudio !== 'any') n++;
  if (f.hasTranscript !== 'any') n++;
  if (f.status !== 'any') n++;
  if (f.splitGroup !== 'any') n++;
  if (f.speakersResolved !== 'any') n++;
  if (f.attention !== 'any') n++;
  if (f.spkCountMin !== '' || f.spkCountMax !== '') n++;
  if (f.sortBy !== 'date_desc') n++;
  return n;
}

function _filtersEqual(a, b) {
  const keys = Object.keys(_SIDEBAR_FILTER_DEFAULTS);
  for (const k of keys) {
    const av = a[k], bv = b[k];
    if (Array.isArray(av) || Array.isArray(bv)) {
      const aa = av || [], bb = bv || [];
      if (aa.length !== bb.length) return false;
      const sa = [...aa].sort(), sb = [...bb].sort();
      for (let i = 0; i < sa.length; i++) if (sa[i] !== sb[i]) return false;
    } else if ((av ?? '') !== (bv ?? '')) return false;
  }
  return true;
}

function _sessionDurationSec(s) {
  if (s.last_segment_time != null && s.last_segment_time > 0) return s.last_segment_time;
  if (s.ended_at) {
    const start = new Date(s.started_at + 'Z');
    const end   = new Date(s.ended_at + 'Z');
    return Math.max(0, (end - start) / 1000);
  }
  return 0;
}

function _sessionDateMatchesPreset(s, f) {
  if (f.datePreset === 'any') return true;
  const start = new Date(s.started_at + 'Z');
  const now = new Date();
  if (f.datePreset === 'today') {
    return start.toDateString() === now.toDateString();
  }
  if (f.datePreset === 'yesterday') {
    const y = new Date(now); y.setDate(now.getDate() - 1);
    return start.toDateString() === y.toDateString();
  }
  if (f.datePreset === '7d') {
    const cutoff = new Date(now); cutoff.setDate(now.getDate() - 7);
    return start >= cutoff;
  }
  if (f.datePreset === '30d') {
    const cutoff = new Date(now); cutoff.setDate(now.getDate() - 30);
    return start >= cutoff;
  }
  if (f.datePreset === 'thisMonth') {
    return start.getFullYear() === now.getFullYear() && start.getMonth() === now.getMonth();
  }
  if (f.datePreset === 'thisYear') {
    return start.getFullYear() === now.getFullYear();
  }
  if (f.datePreset === 'custom') {
    if (f.dateFrom) {
      const from = new Date(f.dateFrom + 'T00:00:00');
      if (start < from) return false;
    }
    if (f.dateTo) {
      const to = new Date(f.dateTo + 'T23:59:59');
      if (start > to) return false;
    }
    return true;
  }
  return true;
}

function _sessionDurationMatches(s, f) {
  if (f.durationPreset === 'any') return true;
  const min = _sessionDurationSec(s) / 60;
  switch (f.durationPreset) {
    case 'lt5':    return min < 5;
    case '5to15':  return min >= 5 && min < 15;
    case '15to30': return min >= 15 && min < 30;
    case '30to60': return min >= 30 && min < 60;
    case 'gt60':   return min >= 60;
    case 'custom': {
      const lo = f.durMin === '' ? -Infinity : parseFloat(f.durMin);
      const hi = f.durMax === '' ? Infinity  : parseFloat(f.durMax);
      return min >= lo && min <= hi;
    }
  }
  return true;
}

function _sessionHasUnresolvedSpeakers(s) {
  if (s.attention && typeof s.attention.unresolved === 'number') return s.attention.unresolved > 0;
  return (s.speakers || []).some(
    sp => _GENERIC_SPEAKER_RE.test(String(sp.name || '').trim())
  );
}

function _sessionNeedsAttention(s) {
  if (s.attention && typeof s.attention.needs === 'boolean') return s.attention.needs;
  return (s.speakers || []).some(
    sp => _GENERIC_SPEAKER_RE.test(String(sp.name || '').trim())
  );
}

function _sessionMatchesFilter(s, f, knownFolderIds) {
  if (!_sessionDateMatchesPreset(s, f)) return false;
  if (!_sessionDurationMatches(s, f)) return false;

  if ((f.folders || []).length) {
    const inUncat = f.folders.includes('__uncat__');
    const isUncat = !s.folder_id || !knownFolderIds.has(s.folder_id);
    const inSpec  = s.folder_id && f.folders.includes(s.folder_id);
    if (!(inSpec || (inUncat && isUncat))) return false;
  }

  if ((f.speakers || []).length) {
    const names = new Set((s.speakers || []).map(sp => (sp.name || '').toLowerCase()));
    if (!f.speakers.some(n => names.has(n))) return false;
  }

  if (f.hasAudio === 'yes' && !s.has_audio) return false;
  if (f.hasAudio === 'no'  &&  s.has_audio) return false;

  const hasT = !!(s.last_segment_time && s.last_segment_time > 0);
  if (f.hasTranscript === 'yes' && !hasT) return false;
  if (f.hasTranscript === 'no'  &&  hasT) return false;

  const isLive = s.id === state.sessionId && state.isRecording;
  const isDone = !!s.ended_at && !isLive;
  if (f.status === 'done'   && !isDone) return false;
  if (f.status === 'inprog' && !isLive) return false;

  if (f.splitGroup === 'yes' && !s.split_group_id) return false;
  if (f.splitGroup === 'no'  &&  s.split_group_id) return false;
  if (f.attention === 'needs' && !_sessionNeedsAttention(s)) return false;

  if (f.speakersResolved && f.speakersResolved !== 'any') {
    // Prefer the server definition, which includes material generic speakers
    // and expected-count mismatches. Older payloads fall back to generic names.
    // "resolved" also requires at least one speaker.
    const spk = s.speakers || [];
    // "Speaker IDs" keeps its original meaning (generic names only); the
    // broader expected-count mismatch lives under the separate attention key.
    const hasGeneric = _sessionHasUnresolvedSpeakers(s);
    if (f.speakersResolved === 'unresolved' && !hasGeneric) return false;
    if (f.speakersResolved === 'resolved' && (hasGeneric || spk.length === 0)) return false;
  }

  if (f.spkCountMin !== '' || f.spkCountMax !== '') {
    const named = (s.speakers || []).filter(sp => sp.name && !/^Speaker \d+$/i.test(sp.name)).length;
    if (f.spkCountMin !== '' && named < parseInt(f.spkCountMin, 10)) return false;
    if (f.spkCountMax !== '' && named > parseInt(f.spkCountMax, 10)) return false;
  }
  return true;
}

function _applySidebarFilterToSessions(sessions) {
  const f = _sidebarFilter;
  const folderIds = new Set(_sidebarFolders.map(fl => fl.id));
  let out = sessions;
  if (_filterIsActive(f)) {
    out = sessions.filter(s => _sessionMatchesFilter(s, f, folderIds));
  }
  // Sorting only applies a non-default order when explicitly chosen - the
  // normal-mode renderer handles its own folder/date grouping when sortBy is
  // 'date_desc', so we leave the array as-is in that case.
  if (f.sortBy && f.sortBy !== 'date_desc') {
    out = [...out];
    const dur = s => _sessionDurationSec(s);
    const spk = s => (s.speakers || []).filter(sp => sp.name && !/^Speaker \d+$/i.test(sp.name)).length;
    const cmp = {
      date_asc:      (a, b) => a.started_at.localeCompare(b.started_at),
      title_asc:     (a, b) => (a.title || '').localeCompare(b.title || ''),
      title_desc:    (a, b) => (b.title || '').localeCompare(a.title || ''),
      duration_desc: (a, b) => dur(b) - dur(a),
      duration_asc:  (a, b) => dur(a) - dur(b),
      speakers_desc: (a, b) => spk(b) - spk(a),
      unresolved_first: (a, b) => {
        const gen = s => _sessionNeedsAttention(s) ? 0 : 1;
        const ga = gen(a), gb = gen(b);
        if (ga !== gb) return ga - gb;                 // needs-IDs sessions first
        return (b.started_at || '').localeCompare(a.started_at || '');  // newest within group
      },
    }[f.sortBy];
    if (cmp) out.sort(cmp);
  }
  return out;
}

function _updateSidebarFilterBtnState() {
  const btn = document.getElementById('sidebar-filter-btn');
  if (!btn) return;
  btn.classList.toggle('active', _filterIsActive(_sidebarFilter));
  const n = _activeFilterCount(_sidebarFilter);
  btn.title = n ? `${n} filter${n === 1 ? '' : 's'} applied · click to edit` : 'Filter sessions';
}

function _toggleSidebarFilter(ev) {
  if (ev) ev.stopPropagation();
  if (_sidebarFilterPopoverOpen) { _closeSidebarFilter(); return; }
  _openSidebarFilter();
}

function _openSidebarFilter() {
  const pop = document.getElementById('sidebar-filter-popover');
  const btn = document.getElementById('sidebar-filter-btn');
  if (!pop || !btn) return;
  _sidebarFilterPopoverOpen = true;
  btn.classList.add('open');
  _renderSidebarFilterPopover();
  pop.classList.remove('hidden');
  _positionSidebarFilterPopover();
  // Track sidebar resize (drag handle, collapse/expand) so the popover follows.
  const sidebar = document.getElementById('sidebar');
  if (sidebar && typeof ResizeObserver !== 'undefined') {
    _sidebarFilterResizeObserver = new ResizeObserver(_positionSidebarFilterPopover);
    _sidebarFilterResizeObserver.observe(sidebar);
  }
  // Defer listener attach so the click that opened us doesn't immediately close
  setTimeout(() => {
    document.addEventListener('mousedown', _onFilterDocClick, true);
    document.addEventListener('keydown', _onFilterEsc, true);
    window.addEventListener('resize', _positionSidebarFilterPopover);
    window.addEventListener('scroll', _positionSidebarFilterPopover, true);
  }, 0);
}

function _closeSidebarFilter() {
  const pop = document.getElementById('sidebar-filter-popover');
  const btn = document.getElementById('sidebar-filter-btn');
  if (pop) pop.classList.add('hidden');
  if (btn) btn.classList.remove('open');
  _sidebarFilterPopoverOpen = false;
  document.removeEventListener('mousedown', _onFilterDocClick, true);
  document.removeEventListener('keydown', _onFilterEsc, true);
  window.removeEventListener('resize', _positionSidebarFilterPopover);
  window.removeEventListener('scroll', _positionSidebarFilterPopover, true);
  if (_sidebarFilterResizeObserver) {
    _sidebarFilterResizeObserver.disconnect();
    _sidebarFilterResizeObserver = null;
  }
  // If the user closes the popover while the active filter is empty AND a
  // saved default exists, drop the saved default too. "Clear + Done" is the
  // natural way to fully reset, so we shouldn't keep silently re-applying
  // the old default the next time the app loads.
  if (!_filterIsActive(_sidebarFilter) && _filterIsActive(_sidebarFilterDefault)) {
    _sidebarFilterDefault = { ..._SIDEBAR_FILTER_DEFAULTS };
    savePref('sidebar_filter_default', null);
  }
}

function _onFilterDocClick(e) {
  const pop = document.getElementById('sidebar-filter-popover');
  const btn = document.getElementById('sidebar-filter-btn');
  if (!pop || !btn) return;
  if (pop.contains(e.target) || btn.contains(e.target)) return;
  _closeSidebarFilter();
}

function _onFilterEsc(e) {
  if (e.key === 'Escape') { e.stopPropagation(); _closeSidebarFilter(); }
}

function _positionSidebarFilterPopover() {
  const pop = document.getElementById('sidebar-filter-popover');
  const btn = document.getElementById('sidebar-filter-btn');
  if (!pop || !btn || pop.classList.contains('hidden')) return;
  const sidebar = document.getElementById('sidebar');
  const margin = 8;
  const gap = 8;
  const popW = pop.offsetWidth || 340;
  const popH = pop.offsetHeight || 480;

  // Anchor to the right of the sidebar so the popover never overlaps the
  // session list it's filtering. Falls back to the filter button rect when
  // the sidebar is collapsed.
  const sbRect = sidebar ? sidebar.getBoundingClientRect() : null;
  const sbVisible = sbRect && sbRect.width > 4 && !sidebar.classList.contains('collapsed');
  const anchorRight = sbVisible ? sbRect.right : btn.getBoundingClientRect().right;

  let left = anchorRight + gap;
  // If there's no horizontal room to the right (narrow viewport), flip to
  // the left of the sidebar; if that doesn't fit either, clamp to viewport.
  if (left + popW > window.innerWidth - margin) {
    const flipped = (sbVisible ? sbRect.left : btn.getBoundingClientRect().left) - gap - popW;
    if (flipped >= margin) left = flipped;
    else left = Math.max(margin, window.innerWidth - popW - margin);
  }

  // Vertically align with the filter button's row, clamped to viewport.
  const btnRect = btn.getBoundingClientRect();
  let top = btnRect.top - 4;
  if (top + popH > window.innerHeight - margin) top = Math.max(margin, window.innerHeight - popH - margin);
  if (top < margin) top = margin;

  pop.style.left = left + 'px';
  pop.style.top  = top + 'px';
}

function _onFilterChange() {
  if (_sidebarFilter.attention === 'needs') _sidebarFilter.sortBy = 'unresolved_first';
  _updateSidebarFilterBtnState();
  _syncAttentionControlState();
  _renderSidebarFilterPopover();    // refresh chip states + count
  _renderSidebar();                 // re-render session list with new filter
}

function _resetSidebarFilter() {
  _sidebarFilter = { ..._SIDEBAR_FILTER_DEFAULTS };
  _onFilterChange();
}

function _setSidebarFilterAsDefault() {
  _sidebarFilterDefault = { ..._sidebarFilter, folders: [...(_sidebarFilter.folders || [])], speakers: [...(_sidebarFilter.speakers || [])] };
  // Persist via existing prefs API - `null` clears the default when no filters
  // are active.
  const payload = _filterIsActive(_sidebarFilterDefault) ? _sidebarFilterDefault : null;
  savePref('sidebar_filter_default', payload);
  // Visual confirmation in the footer
  const note = document.getElementById('sf-default-saved');
  if (note) {
    note.classList.add('show');
    clearTimeout(_setSidebarFilterAsDefault._t);
    _setSidebarFilterAsDefault._t = setTimeout(() => note.classList.remove('show'), 1800);
  }
}

function _loadSidebarFilterDefault() {
  // Called once after preferences load. Apply saved default (if any) as the
  // active filter, so the list opens pre-filtered the way the user wants.
  const saved = _prefs && _prefs.sidebar_filter_default;
  if (saved && typeof saved === 'object') {
    _sidebarFilterDefault = { ..._SIDEBAR_FILTER_DEFAULTS, ...saved };
    _sidebarFilter = { ..._sidebarFilterDefault,
                       folders: [...(_sidebarFilterDefault.folders || [])],
                       speakers: [...(_sidebarFilterDefault.speakers || [])] };
  }
  if (_sidebarFilter.attention === 'needs') _sidebarFilter.sortBy = 'unresolved_first';
  _updateSidebarFilterBtnState();
  _syncAttentionControlState();
}

function _renderSidebarFilterPopover() {
  const pop = document.getElementById('sidebar-filter-popover');
  if (!pop) return;
  const f = _sidebarFilter;
  const n = _activeFilterCount(f);

  // Collect distinct named speakers across all sessions
  const speakerMap = new Map();   // lcname -> {name, color, count}
  for (const s of _sidebarAllSessions) {
    for (const sp of (s.speakers || [])) {
      if (!sp.name || /^Speaker \d+$/i.test(sp.name)) continue;
      const key = sp.name.toLowerCase();
      const e = speakerMap.get(key) || { name: sp.name, color: sp.color, count: 0 };
      e.count++;
      if (sp.color && !e.color) e.color = sp.color;
      speakerMap.set(key, e);
    }
  }
  const speakerList = [...speakerMap.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));

  const folders = (_sidebarFolders || []).slice().sort((a, b) =>
    (a.name || '').localeCompare(b.name || '')
  );

  const chip = (label, active, onClick, extra = '') =>
    `<button type="button" class="sf-chip${active ? ' selected' : ''}" data-act="${onClick}" ${extra}>${label}</button>`;

  const sectionActive = {
    date:     f.datePreset !== 'any',
    duration: f.durationPreset !== 'any',
    folders:  (f.folders || []).length > 0,
    speakers: (f.speakers || []).length > 0,
    flags:    f.hasAudio !== 'any' || f.hasTranscript !== 'any' || f.status !== 'any' || f.splitGroup !== 'any' || f.speakersResolved !== 'any' || f.attention !== 'any',
    spkCount: f.spkCountMin !== '' || f.spkCountMax !== '',
    sort:     f.sortBy !== 'date_desc',
  };
  const sectClass = (k) => `sf-section${sectionActive[k] ? ' has-active' : ''}`;

  const tri = (key, val, opts) => `<div class="sf-tri" data-tri="${key}">` +
    opts.map(([v, label]) => `<button type="button" data-tri-val="${v}" class="${val === v ? 'active' : ''}">${label}</button>`).join('') + '</div>';

  // Section header for collapsible sections - clicking the whole row toggles
  // open/closed. Active filter dot is preserved.
  const collapsibleHeader = (id, iconHtml, title, count) => {
    const isOpen = _sidebarFilterOpenSections.has(id);
    const badge = count > 0
      ? `<span class="sf-section-count">${count}</span>`
      : '';
    return `<button type="button" class="sf-section-label sf-section-toggle" data-toggle-section="${id}">
      ${iconHtml} ${title} ${badge} <span class="sf-active-dot"></span>
      <span class="sf-section-chevron"><i class="fa-solid fa-chevron-${isOpen ? 'down' : 'right'}"></i></span>
    </button>`;
  };
  const isOpen = id => _sidebarFilterOpenSections.has(id);

  pop.innerHTML = `
    <div class="sf-header">
      <div class="sf-header-title">
        <i class="fa-solid fa-filter"></i> Filter sessions
        <span class="sf-header-count${n ? ' has-filters' : ''}">${n ? `${n} active` : 'none'}</span>
      </div>
      <button type="button" class="sf-close-btn" data-act="close" title="Close"><i class="fa-solid fa-xmark"></i></button>
    </div>
    <div class="sf-body">

      <div class="${sectClass('sort')}" data-section="sort">
        <div class="sf-section-label"><i class="fa-solid fa-arrow-down-wide-short"></i> Sort by <span class="sf-active-dot"></span></div>
        <select class="sf-select" id="sf-sort">
          <option value="date_desc"      ${f.sortBy === 'date_desc' ? 'selected' : ''}>Newest first (default)</option>
          <option value="date_asc"       ${f.sortBy === 'date_asc' ? 'selected' : ''}>Oldest first</option>
          <option value="title_asc"      ${f.sortBy === 'title_asc' ? 'selected' : ''}>Title A → Z</option>
          <option value="title_desc"     ${f.sortBy === 'title_desc' ? 'selected' : ''}>Title Z → A</option>
          <option value="duration_desc"  ${f.sortBy === 'duration_desc' ? 'selected' : ''}>Longest first</option>
          <option value="duration_asc"   ${f.sortBy === 'duration_asc' ? 'selected' : ''}>Shortest first</option>
          <option value="speakers_desc"  ${f.sortBy === 'speakers_desc' ? 'selected' : ''}>Most speakers first</option>
          <option value="unresolved_first" ${f.sortBy === 'unresolved_first' ? 'selected' : ''}>Needs speaker IDs first</option>
        </select>
      </div>

      <div class="${sectClass('date')}" data-section="date">
        <div class="sf-section-label"><i class="fa-regular fa-calendar"></i> Date <span class="sf-active-dot"></span></div>
        <div class="sf-chip-row">
          ${chip('Any',       f.datePreset === 'any',       'date:any')}
          ${chip('Today',     f.datePreset === 'today',     'date:today')}
          ${chip('Yesterday', f.datePreset === 'yesterday', 'date:yesterday')}
          ${chip('Last 7d',   f.datePreset === '7d',        'date:7d')}
          ${chip('Last 30d',  f.datePreset === '30d',       'date:30d')}
          ${chip('This month',f.datePreset === 'thisMonth', 'date:thisMonth')}
          ${chip('This year', f.datePreset === 'thisYear',  'date:thisYear')}
          ${chip('Custom…',   f.datePreset === 'custom',    'date:custom')}
        </div>
        ${f.datePreset === 'custom' ? `
        <div class="sf-range" style="margin-top:8px">
          <div class="sf-range-inputs">
            <input type="date" id="sf-date-from" value="${f.dateFrom || ''}" aria-label="From date">
            <span class="sf-range-sep">→</span>
            <input type="date" id="sf-date-to"   value="${f.dateTo   || ''}" aria-label="To date">
          </div>
        </div>` : ''}
      </div>

      <div class="${sectClass('duration')}" data-section="duration">
        <div class="sf-section-label"><i class="fa-regular fa-clock"></i> Duration <span class="sf-active-dot"></span></div>
        <div class="sf-chip-row">
          ${chip('Any',         f.durationPreset === 'any',    'dur:any')}
          ${chip('< 5 min',     f.durationPreset === 'lt5',    'dur:lt5')}
          ${chip('5 to 15 min',    f.durationPreset === '5to15',  'dur:5to15')}
          ${chip('15 to 30 min',   f.durationPreset === '15to30', 'dur:15to30')}
          ${chip('30 to 60 min',   f.durationPreset === '30to60', 'dur:30to60')}
          ${chip('> 60 min',    f.durationPreset === 'gt60',   'dur:gt60')}
          ${chip('Custom…',     f.durationPreset === 'custom', 'dur:custom')}
        </div>
        ${f.durationPreset === 'custom' ? `
        <div class="sf-range" style="margin-top:8px">
          <div class="sf-range-inputs">
            <input type="number" min="0" step="0.5" id="sf-dur-min" placeholder="min" value="${f.durMin}">
            <span class="sf-range-sep">to</span>
            <input type="number" min="0" step="0.5" id="sf-dur-max" placeholder="max" value="${f.durMax}">
            <span class="sf-range-suffix">min</span>
          </div>
        </div>` : ''}
      </div>

      <div class="${sectClass('folders')}${isOpen('folders') ? ' open' : ''}" data-section="folders">
        ${collapsibleHeader('folders',
            '<i class="fa-regular fa-folder"></i>', 'Folder',
            (f.folders || []).length)}
        <div class="sf-section-body">
          <div class="sf-chip-row">
            ${chip('<i class="fa-solid fa-inbox"></i> Uncategorized',
                   (f.folders || []).includes('__uncat__'),
                   'folder:__uncat__')}
            ${folders.length === 0 ? '<span class="sf-empty">No folders yet.</span>' :
              folders.map(fl => chip(escapeHtml(fl.name || 'Untitled'),
                                     (f.folders || []).includes(fl.id),
                                     'folder:' + fl.id)).join('')}
          </div>
        </div>
      </div>

      <div class="${sectClass('speakers')}${isOpen('speakers') ? ' open' : ''}" data-section="speakers">
        ${collapsibleHeader('speakers',
            '<i class="fa-regular fa-user"></i>', 'Speakers',
            (f.speakers || []).length)}
        <div class="sf-section-body">
          <div class="sf-chip-row">
            ${speakerList.length === 0 ? '<span class="sf-empty">No named speakers yet.</span>' :
              speakerList.slice(0, 80).map(sp => {
                const sel = (f.speakers || []).includes(sp.name.toLowerCase());
                const dot = sp.color
                  ? `<span class="sf-chip-dot" style="background:${sp.color}"></span>`
                  : '';
                return `<button type="button" class="sf-chip${sel ? ' selected' : ''}" data-act="speaker:${escapeHtml(sp.name.toLowerCase())}">${dot}${escapeHtml(sp.name)}</button>`;
              }).join('')}
          </div>
        </div>
      </div>

      <div class="${sectClass('flags')}" data-section="flags">
        <div class="sf-section-label"><i class="fa-solid fa-toggle-on"></i> Has & Status <span class="sf-active-dot"></span></div>
        <div class="sf-toggles-grid">
          <div class="sf-toggle-row">
            <span class="sf-toggle-label"><i class="fa-solid fa-volume-high"></i> Audio</span>
            ${tri('hasAudio', f.hasAudio, [['any','Any'],['yes','Yes'],['no','No']])}
          </div>
          <div class="sf-toggle-row">
            <span class="sf-toggle-label"><i class="fa-solid fa-align-left"></i> Transcript</span>
            ${tri('hasTranscript', f.hasTranscript, [['any','Any'],['yes','Yes'],['no','No']])}
          </div>
          <div class="sf-toggle-row">
            <span class="sf-toggle-label"><i class="fa-solid fa-circle-check"></i> Status</span>
            ${tri('status', f.status, [['any','Any'],['done','Done'],['inprog','Live']])}
          </div>
          <div class="sf-toggle-row">
            <span class="sf-toggle-label"><i class="fa-solid fa-code-branch"></i> Split</span>
            ${tri('splitGroup', f.splitGroup, [['any','Any'],['yes','Yes'],['no','No']])}
          </div>
          <div class="sf-toggle-row">
            <span class="sf-toggle-label"><i class="fa-solid fa-user-tag"></i> Speaker IDs</span>
            ${tri('speakersResolved', f.speakersResolved, [['any','Any'],['unresolved','Needs IDs'],['resolved','All named']])}
          </div>
          <div class="sf-toggle-row">
            <span class="sf-toggle-label"><i class="fa-solid fa-circle-exclamation"></i> Attention</span>
            ${tri('attention', f.attention, [['any','Any'],['needs','Needs']])}
          </div>
        </div>
      </div>

      <div class="${sectClass('spkCount')}" data-section="spkCount">
        <div class="sf-section-label"><i class="fa-solid fa-people-group"></i> Speaker count <span class="sf-active-dot"></span></div>
        <div class="sf-range">
          <div class="sf-range-inputs">
            <input type="number" min="0" step="1" id="sf-spk-min" placeholder="min" value="${f.spkCountMin}">
            <span class="sf-range-sep">to</span>
            <input type="number" min="0" step="1" id="sf-spk-max" placeholder="max" value="${f.spkCountMax}">
            <span class="sf-range-suffix">named</span>
          </div>
        </div>
      </div>

    </div>
    <div class="sf-footer">
      <div class="sf-footer-left">
        <button type="button" class="sf-btn subtle" data-act="reset" ${n ? '' : 'disabled'} title="Clear all active filters">
          <i class="fa-solid fa-xmark"></i> Clear
        </button>
      </div>
      <div class="sf-footer-right">
        <span class="sf-default-saved" id="sf-default-saved"><i class="fa-solid fa-check"></i> Saved</span>
        <button type="button" class="sf-btn" data-act="setDefault" title="Save current filter as default for future sessions">
          <i class="fa-regular fa-bookmark"></i> Set as default
        </button>
        <button type="button" class="sf-btn primary" data-act="close" title="Done">Done</button>
      </div>
    </div>
  `;

  // ── Wire interactions ──
  pop.querySelectorAll('[data-act]').forEach(el => {
    el.addEventListener('click', e => {
      e.stopPropagation();
      const act = el.getAttribute('data-act');
      _handleFilterAct(act);
    });
  });
  pop.querySelectorAll('[data-toggle-section]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const id = btn.getAttribute('data-toggle-section');
      _toggleFilterSection(id);
    });
  });
  pop.querySelectorAll('[data-tri]').forEach(group => {
    const key = group.getAttribute('data-tri');
    group.querySelectorAll('[data-tri-val]').forEach(b => {
      b.addEventListener('click', e => {
        e.stopPropagation();
        const value = b.getAttribute('data-tri-val');
        if (key === 'attention') _setAttentionFilter(value === 'needs');
        else {
          _sidebarFilter[key] = value;
          _onFilterChange();
        }
      });
    });
  });
  const dFrom = pop.querySelector('#sf-date-from');
  const dTo   = pop.querySelector('#sf-date-to');
  if (dFrom) dFrom.addEventListener('change', () => { _sidebarFilter.dateFrom = dFrom.value; _onFilterChange(); });
  if (dTo)   dTo.addEventListener('change',   () => { _sidebarFilter.dateTo   = dTo.value;   _onFilterChange(); });
  const dMin = pop.querySelector('#sf-dur-min');
  const dMax = pop.querySelector('#sf-dur-max');
  if (dMin) dMin.addEventListener('change', () => { _sidebarFilter.durMin = dMin.value; _onFilterChange(); });
  if (dMax) dMax.addEventListener('change', () => { _sidebarFilter.durMax = dMax.value; _onFilterChange(); });
  const sMin = pop.querySelector('#sf-spk-min');
  const sMax = pop.querySelector('#sf-spk-max');
  if (sMin) sMin.addEventListener('change', () => { _sidebarFilter.spkCountMin = sMin.value; _onFilterChange(); });
  if (sMax) sMax.addEventListener('change', () => { _sidebarFilter.spkCountMax = sMax.value; _onFilterChange(); });
  const sortSel = pop.querySelector('#sf-sort');
  if (sortSel) sortSel.addEventListener('change', () => { _sidebarFilter.sortBy = sortSel.value; _onFilterChange(); });

  _positionSidebarFilterPopover();
}

function _toggleFilterSection(id) {
  if (!_SF_COLLAPSIBLE.has(id)) return;
  if (_sidebarFilterOpenSections.has(id)) _sidebarFilterOpenSections.delete(id);
  else                                    _sidebarFilterOpenSections.add(id);
  try {
    localStorage.setItem(_SF_COLLAPSE_KEY, JSON.stringify([..._sidebarFilterOpenSections]));
  } catch (_) {}
  _renderSidebarFilterPopover();
}

function _handleFilterAct(act) {
  if (act === 'close')      { _closeSidebarFilter(); return; }
  if (act === 'reset')      { _resetSidebarFilter(); return; }
  if (act === 'setDefault') { _setSidebarFilterAsDefault(); return; }

  const colon = act.indexOf(':');
  const kind = colon < 0 ? act : act.slice(0, colon);
  const val  = colon < 0 ? ''  : act.slice(colon + 1);
  if (kind === 'date') {
    _sidebarFilter.datePreset = val;
    if (val !== 'custom') { _sidebarFilter.dateFrom = ''; _sidebarFilter.dateTo = ''; }
  } else if (kind === 'dur') {
    _sidebarFilter.durationPreset = val;
    if (val !== 'custom') { _sidebarFilter.durMin = ''; _sidebarFilter.durMax = ''; }
  } else if (kind === 'folder') {
    const list = new Set(_sidebarFilter.folders || []);
    list.has(val) ? list.delete(val) : list.add(val);
    _sidebarFilter.folders = [...list];
  } else if (kind === 'speaker') {
    const list = new Set(_sidebarFilter.speakers || []);
    list.has(val) ? list.delete(val) : list.add(val);
    _sidebarFilter.speakers = [...list];
  }
  _onFilterChange();
}

function _checkSemanticSearchReady() {
  fetch('/api/search/semantic/status').then(r => r.json()).then(data => {
    _semanticSearchReady = data.ready;
    const badge = document.getElementById('sidebar-search-ai');
    if (badge) {
      if (data.ready) {
        badge.classList.add('ready');
        badge.classList.remove('loading', 'unavailable');
        badge.title = 'AI-powered semantic search active';
      } else if (data.loading) {
        badge.classList.add('loading');
        badge.classList.remove('ready', 'unavailable');
        badge.title = 'AI search model loading…';
      } else {
        badge.classList.add('unavailable');
        badge.classList.remove('ready', 'loading');
        badge.title = 'AI search unavailable';
      }
    }
    if (data.loading) setTimeout(_checkSemanticSearchReady, 5000);
  }).catch(() => {});
}

function _highlightSnippet(text, query) {
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return escapeHtml(text);
  const before = text.slice(0, idx);
  const match = text.slice(idx, idx + query.length);
  const after = text.slice(idx + query.length);
  return escapeHtml(before) + '<mark>' + escapeHtml(match) + '</mark>' + escapeHtml(after);
}

function _executeSearchHighlight(hl) {
  const transcriptEl = document.getElementById('transcript');
  if (!transcriptEl) return;
  let target = null;

  // Strategy 1: find by segment ID
  if (hl.segmentId != null) {
    target = transcriptEl.querySelector(`.transcript-segment[data-seg-id="${hl.segmentId}"]`);
  }

  // Strategy 2: text search fallback - find segments containing the query
  if (!target && hl.query) {
    const q = hl.query.toLowerCase();
    const segs = transcriptEl.querySelectorAll('.transcript-segment');
    for (const seg of segs) {
      if (seg.textContent.toLowerCase().includes(q)) {
        target = seg;
        break;
      }
    }
  }

  if (!target) return;

  // Scroll into view and flash highlight
  _doProgrammaticScroll(target, { behavior: 'smooth', block: 'center' });
  target.classList.add('search-flash');
  setTimeout(() => target.classList.remove('search-flash'), 2200);
}

// ── Folder tree helpers ───────────────────────────────────────────────────────

/** Build a map: parentId → child folders (sorted by sort_order). */
function _buildChildMap(folders) {
  const map = new Map();  // key = parent_id (null for top-level)
  for (const f of folders) {
    const key = f.parent_id || null;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(f);
  }
  return map;
}

/** Collect all descendant folder IDs of a given folder. */
function _getDescendantIds(folderId, childMap) {
  const result = new Set();
  const stack = [folderId];
  while (stack.length) {
    const id = stack.pop();
    const children = childMap.get(id) || [];
    for (const c of children) {
      result.add(c.id);
      stack.push(c.id);
    }
  }
  return result;
}

/** Count sessions recursively (folder + all sub-folders). */
function _countSessionsRecursive(folderId, childMap, sessionsByFolder) {
  let count = (sessionsByFolder.get(folderId) || []).length;
  for (const child of (childMap.get(folderId) || [])) {
    count += _countSessionsRecursive(child.id, childMap, sessionsByFolder);
  }
  return count;
}

// ── Drag-and-drop helpers ─────────────────────────────────────────────────────

function _ensureDragIndicator() {
  if (!_dragIndicator) {
    _dragIndicator = document.createElement('div');
    _dragIndicator.className = 'drop-indicator';
  }
  return _dragIndicator;
}

function _removeDragIndicator() {
  if (_dragIndicator && _dragIndicator.parentNode) {
    _dragIndicator.remove();
  }
}

/** Determine drop zone: 'before', 'after', or 'center' (only for folders). */
function _getDropZone(e, el, isFolder) {
  const rect = el.getBoundingClientRect();
  const y = e.clientY - rect.top;
  const h = rect.height;
  if (isFolder) {
    if (y < h * 0.28) return 'before';
    if (y > h * 0.72) return 'after';
    return 'center';
  }
  return y < h * 0.5 ? 'before' : 'after';
}

/** Show the drop indicator line before or after an element. */
function _showDropIndicator(el, position) {
  const ind = _ensureDragIndicator();
  if (position === 'before') {
    el.parentNode.insertBefore(ind, el);
  } else {
    el.parentNode.insertBefore(ind, el.nextSibling);
  }
}

/** Attach drag-over / drop handlers to a session element for reordering. */
function _attachSessionDragHandlers(el, s) {
  el.addEventListener('dragover', e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    // Only show reorder indicator for sessions inside a folder
    if (!s.folder_id) return;
    if (_sidebarDragIds.includes(s.id) && _sidebarDragType === 'session') return;
    const zone = _getDropZone(e, el, false);
    _removeDragIndicator();
    _showDropIndicator(el, zone);
  });
  el.addEventListener('dragleave', e => {
    if (!el.contains(e.relatedTarget)) _removeDragIndicator();
  });
  el.addEventListener('drop', e => {
    _removeDragIndicator();
    // Ungrouped sessions: let the event bubble to the ungrouped zone container
    if (!s.folder_id) return;
    e.preventDefault();
    e.stopPropagation();
    const zone = _getDropZone(e, el, false);
    _handleDrop(s.id, 'session', zone, s.folder_id);
  });
}

/** Check if a folder drop target is invalid (self or descendant of dragged folder). */
function _isFolderDropBlocked(folderId) {
  return _sidebarDragType === 'folder'
    && (_sidebarDragIds.includes(folderId) || _dragDescendants.has(folderId));
}

/** Attach drag-over / drop handlers to a folder header for reorder + nest. */
function _attachFolderDragHandlers(headerEl, folderEl, folder) {
  headerEl.addEventListener('dragover', e => {
    // Block self/descendant drops - don't call preventDefault so browser rejects the drop
    if (_isFolderDropBlocked(folder.id)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    const zone = _getDropZone(e, headerEl, true);
    _removeDragIndicator();
    folderEl.classList.remove('drag-over');
    if (zone === 'center') {
      folderEl.classList.add('drag-over');
    } else {
      _showDropIndicator(folderEl, zone);
    }
  });
  headerEl.addEventListener('dragleave', e => {
    if (!headerEl.contains(e.relatedTarget)) {
      folderEl.classList.remove('drag-over');
      _removeDragIndicator();
    }
  });
  headerEl.addEventListener('drop', e => {
    e.preventDefault();
    e.stopPropagation();
    folderEl.classList.remove('drag-over');
    _removeDragIndicator();
    if (_isFolderDropBlocked(folder.id)) return;
    const zone = _getDropZone(e, headerEl, true);
    if (zone === 'center') {
      _handleDropIntoFolder(folder.id);
    } else {
      _handleDrop(folder.id, 'folder', zone, folder.parent_id);
    }
  });
}

// ── Render sidebar ────────────────────────────────────────────────────────────

function _renderSidebar() {
  _syncRecordingsCount();
  // Apply the active filter set first so every code path below operates on the
  // filtered subset. While searching, the rendered set is the matched sessions
  // in relevance order, intersected with any active filter.
  const filterActive = _filterIsActive(_sidebarFilter);
  const searching    = _sidebarSearchResults !== null;
  const folders      = _sidebarFolders;
  const list         = document.getElementById('session-list');

  _updateSidebarFilterBtnState();

  // While searching, hide the model/device/screen panes below the list so the
  // results get the sidebar's full height (see .sidebar.sidebar-searching CSS).
  document.getElementById('sidebar')?.classList.toggle('sidebar-searching', searching);

  let sessions;
  if (searching) {
    const byId = new Map(_applySidebarFilterToSessions(_sidebarAllSessions).map(s => [s.id, s]));
    // Keep the search's relevance order; drop anything the active filter removed.
    sessions = [..._sidebarSearchResults.keys()].map(id => byId.get(id)).filter(Boolean);
  } else {
    sessions = _applySidebarFilterToSessions(_sidebarAllSessions);
  }
  const hasAny = sessions.length > 0 || (folders.length > 0 && !filterActive);

  // ── Search: empty / pending state. Actual matches render in the folder tree
  //    below, grouped into the folders they belong to. ──
  if (searching && sessions.length === 0) {
    const anyPending = _ftsSearchPending || _semanticSearchPending;
    if (_sidebarSearchQuery && anyPending) {
      list.innerHTML =
        '<div class="search-empty-state">' +
          '<div class="search-dots"><span></span><span></span><span></span></div>' +
          '<p>Searching…</p>' +
        '</div>';
    } else if (filterActive) {
      list.innerHTML =
        '<div class="search-empty-state">' +
          '<div class="search-empty-icon"><i class="fa-solid fa-filter"></i></div>' +
          '<p>No matches with the current filter</p>' +
        '</div>';
    } else {
      list.innerHTML =
        '<div class="search-empty-state">' +
          '<div class="search-empty-icon"><i class="fa-solid fa-magnifying-glass"></i></div>' +
          '<p>No matching sessions</p>' +
        '</div>';
    }
    _updateBulkBar();
    return;
  }

  // ── Normal mode: folder hierarchy + date groups ──
  if (!hasAny) {
    if (filterActive) {
      list.innerHTML =
        '<div class="search-empty-state">' +
          '<div class="search-empty-icon"><i class="fa-solid fa-filter"></i></div>' +
          '<p>No sessions match the current filter</p>' +
          '<button type="button" class="sf-btn subtle" onclick="_resetSidebarFilter()" style="margin-top:6px"><i class="fa-solid fa-xmark"></i> Clear filters</button>' +
        '</div>';
    } else {
      list.innerHTML = '<p class="sidebar-empty">No past sessions yet.</p>';
    }
    _updateBulkBar();
    return;
  }

  // Search results are grouped into the folders they belong to, exactly like an
  // active filter: prune folders with no matches, and keep the incoming
  // (relevance / sort) order inside each folder. `onSession` decorates each
  // matched row with its score bar, snippets, and jump-to-segment behavior.
  const treeFilterActive = filterActive || searching;
  const onSession = searching
    ? (el, s) => { const d = _sidebarSearchResults.get(s.id); if (d) _decorateSearchResult(el, s, d); }
    : null;

  // Build lookup structures. The session set is already narrowed, so any folder
  // whose subtree contains zero of these sessions gets pruned during render.
  const childMap = _buildChildMap(folders);
  const sessionsByFolder = new Map();
  for (const s of sessions) {
    const key = s.folder_id || null;
    if (!sessionsByFolder.has(key)) sessionsByFolder.set(key, []);
    sessionsByFolder.get(key).push(s);
  }
  // Within-folder ordering: honor the user's manual sort_order only when neither
  // a filter nor a search is narrowing the list (those arrive pre-ordered, by
  // sortBy or by relevance respectively).
  if (!treeFilterActive) {
    for (const [, arr] of sessionsByFolder) {
      arr.sort((a, b) => a.sort_order - b.sort_order);
    }
  }

  const folderIds = new Set(folders.map(f => f.id));
  const fragment = document.createDocumentFragment();

  // Render folder tree recursively from top-level. treeFilterActive prunes
  // empty branches; onSession decorates matched rows while searching.
  _renderFolderSubtree(null, 0, fragment, childMap, sessionsByFolder, folderIds, treeFilterActive, onSession);

  // Ungrouped sessions (no folder or deleted folder) - also acts as a drop
  // target to remove sessions from folders.
  const ungroupedZone = document.createElement('div');
  ungroupedZone.className = 'sidebar-ungrouped-zone';

  const ungrouped = sessions.filter(s => !s.folder_id || !folderIds.has(s.folder_id));
  if (ungrouped.length) {
    if (searching) {
      // Flat, relevance-ordered (no date groups) so the ranking stays meaningful.
      ungrouped.forEach(s => {
        const el = _makeSessionEl(s);
        _attachSessionDragHandlers(el, s);
        onSession?.(el, s);
        ungroupedZone.appendChild(el);
      });
    } else {
      // Preserve filter sort order when a filter is active; otherwise default
      // to newest-first like the rest of the sidebar always has.
      if (!filterActive) {
        ungrouped.sort((a, b) => b.started_at.localeCompare(a.started_at));
      }
      const groups = groupByDate(ungrouped);
      for (const [label, items] of groups) {
        const groupEl = document.createElement('div');
        groupEl.className = 'session-group';
        groupEl.textContent = label;
        ungroupedZone.appendChild(groupEl);
        items.forEach(s => {
          const el = _makeSessionEl(s);
          _attachSessionDragHandlers(el, s);
          ungroupedZone.appendChild(el);
        });
      }
    }
  }

  // Drag-over / drop on the entire ungrouped zone to uncategorize
  ungroupedZone.addEventListener('dragover', e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    ungroupedZone.classList.add('drag-over');
  });
  ungroupedZone.addEventListener('dragleave', e => {
    if (!ungroupedZone.contains(e.relatedTarget)) {
      ungroupedZone.classList.remove('drag-over');
    }
  });
  ungroupedZone.addEventListener('drop', e => {
    e.preventDefault();
    ungroupedZone.classList.remove('drag-over');
    if (_sidebarDragType === 'session') {
      _handleDropIntoFolder(null);
    } else if (_sidebarDragType === 'folder') {
      _handleDropFolderToTopLevel();
    }
  });

  fragment.appendChild(ungroupedZone);

  list.innerHTML = '';
  list.appendChild(fragment);

  // "Refining with AI…" hint while a semantic pass is still in flight.
  if (searching && _semanticSearchPending) {
    const refining = document.createElement('div');
    refining.className = 'search-refining';
    refining.innerHTML = '<div class="search-dots sm"><span></span><span></span><span></span></div> Refining with AI…';
    list.appendChild(refining);
  }

  _updateBulkBar();
  _updateActiveFolderHighlights();
}

// Decorate a rendered search-result row (in the folder tree) with its
// similarity score bar, match snippets, and click-to-jump-to-segment behavior.
// `s` is the session, `data` its entry from _sidebarSearchResults.
function _decorateSearchResult(el, s, data) {
  const sid = s.id;
  const info = el.querySelector('.session-info');
  if (info) {
    if (data.score != null) {
      const scoreEl = document.createElement('div');
      scoreEl.className = 'session-search-score';
      const pct = Math.round(data.score * 100);
      scoreEl.innerHTML = `<span class="score-bar"><span class="score-fill" style="width:${pct}%"></span></span><span class="score-label">${pct}%</span>`;
      info.appendChild(scoreEl);
    }
    if (data.matches?.length) {
      const matchesEl = document.createElement('div');
      matchesEl.className = 'session-search-matches';
      for (const m of data.matches.slice(0, 2)) {
        const snip = document.createElement('div');
        snip.className = 'session-search-snippet';
        if (m.segment_id != null || m.kind === 'segment') snip.classList.add('clickable');
        const kindLabel = m.kind === 'title' ? ''
          : m.kind === 'semantic' ? ''
          : m.kind === 'participant' ? '<span class="search-match-kind search-match-participant"><i class="fa-solid fa-user"></i> participant</span>'
          : `<span class="search-match-kind">${escapeHtml(m.kind)}</span>`;
        snip.innerHTML = kindLabel + m.snippet;
        if (m.segment_id != null) {
          snip.addEventListener('click', e => {
            e.stopPropagation();
            _pendingSearchHighlight = { segmentId: m.segment_id, query: _sidebarSearchQuery };
            loadSession(sid);
          });
        } else if (m.kind === 'segment') {
          // FTS match without a segment id - fall back to text search.
          snip.addEventListener('click', e => {
            e.stopPropagation();
            _pendingSearchHighlight = { query: _sidebarSearchQuery };
            loadSession(sid);
          });
        }
        matchesEl.appendChild(snip);
      }
      info.appendChild(matchesEl);
    }
  }
  // Default click (anywhere on the row) still primes the in-session highlight.
  el.addEventListener('click', () => {
    if (data.matches?.some(m => m.segment_id != null || m.kind === 'segment')) {
      const first = data.matches.find(m => m.segment_id != null);
      _pendingSearchHighlight = first
        ? { segmentId: first.segment_id, query: _sidebarSearchQuery }
        : { query: _sidebarSearchQuery };
    }
  }, true);  // capture phase - runs before the loadSession click
}

// Returns the set of folder IDs in the active session's ancestor chain
// (immediate folder + every parent up to root). Empty set when no session
// is active or the active session isn't filed in any folder.
function _getActiveSessionAncestorFolderIds() {
  const out = new Set();
  const sid = (typeof state !== 'undefined') ? state.sessionId : null;
  if (!sid) return out;
  const sess = _sidebarAllSessions.find(s => s.id === sid);
  if (!sess || !sess.folder_id) return out;
  const folderById = new Map(_sidebarFolders.map(f => [f.id, f]));
  let cursor = folderById.get(sess.folder_id);
  const seen = new Set();
  while (cursor && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    out.add(cursor.id);
    cursor = cursor.parent_id ? folderById.get(cursor.parent_id) : null;
  }
  return out;
}

// The immediate (leaf) folder containing the active session, or null.
function _getActiveSessionImmediateFolderId() {
  const sid = (typeof state !== 'undefined') ? state.sessionId : null;
  if (!sid) return null;
  const sess = _sidebarAllSessions.find(s => s.id === sid);
  return (sess && sess.folder_id) || null;
}

// Toggle two classes on every .sidebar-folder element:
//   - `folder-active`          : the immediate folder of the active session
//   - `folder-active-ancestor` : every folder that transitively contains it
//                                (immediate folder + every parent)
// Use `.folder-active` for a leaf-only highlight, `.folder-active-ancestor`
// for a breadcrumb/trail effect. Caveat: descendant selectors like
// `.folder-active-ancestor .folder-header` will also match nested
// sibling folders' headers (e.g. a sibling subfolder under the same
// parent). Use `>` child combinator (`.folder-active-ancestor > .folder-header`)
// or target `.folder-active` directly.
function _updateActiveFolderHighlights() {
  const ancestors = _getActiveSessionAncestorFolderIds();
  const immediate = _getActiveSessionImmediateFolderId();
  document.querySelectorAll('.sidebar-folder').forEach(el => {
    const id = el.dataset.folderId;
    el.classList.toggle('folder-active-ancestor', !!id && ancestors.has(id));
    el.classList.toggle('folder-active', !!id && id === immediate);
  });
}

function _renderFolderSubtree(parentId, depth, container, childMap, sessionsByFolder, folderIds, filterActive, onSession = null) {
  const children = childMap.get(parentId) || [];
  for (const folder of children) {
    const folderSessions = sessionsByFolder.get(folder.id) || [];
    const totalCount = _countSessionsRecursive(folder.id, childMap, sessionsByFolder);
    // Prune empty branches under an active filter or search: a folder whose
    // entire subtree was narrowed out shouldn't take up space. With neither,
    // empty folders render normally (with the "Drop sessions here" hint).
    if (filterActive && totalCount === 0) continue;
    // Always honor the user's saved expand/collapse state; filters/search never
    // force-expand a folder. Empty folders are pruned above, so a collapsed
    // folder only appears when it actually contains matches.
    const collapsed = _sidebarCollapsed.has(folder.id);

    const folderEl = document.createElement('div');
    folderEl.className = `sidebar-folder ${collapsed ? 'collapsed' : 'expanded'}`;
    folderEl.dataset.folderId = folder.id;


    // Folder header
    const header = document.createElement('div');
    header.className = 'folder-header';
    header.draggable = true;

    // Drag start for folder
    header.addEventListener('dragstart', e => {
      e.stopPropagation();
      _internalDragActive = true;
      _sidebarDragType = 'folder';
      _sidebarDragIds = [folder.id];
      _dragDescendants = _getDescendantIds(folder.id, childMap);
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', JSON.stringify([folder.id]));
      folderEl.classList.add('dragging');
    });
    header.addEventListener('dragend', () => {
      _internalDragActive = false;
      folderEl.classList.remove('dragging');
      _removeDragIndicator();
      _dragDescendants.clear();
    });

    header.innerHTML = `
      <button class="folder-toggle"><i class="fa-solid fa-chevron-${collapsed ? 'right' : 'down'}"></i></button>
      <span class="folder-icon"><i class="fa-solid fa-folder${collapsed ? '' : '-open'}"></i></span>
      <span class="folder-name">${escapeHtml(folder.name)}</span>
      <span class="folder-count">${totalCount}</span>`;

    //const folderMenuBtn = document.createElement('button');
    //folderMenuBtn.className = 'folder-menu-btn';
    //folderMenuBtn.title = 'More options';
    //folderMenuBtn.innerHTML = '<i class="fa-solid fa-ellipsis-vertical"></i>';
    header.addEventListener('click', e => { _toggleFolder(`${folder.id}`); });
    //folderMenuBtn.addEventListener('click', e => { e.stopPropagation(); _openFolderMenu(e, folder); });
    header.addEventListener('contextmenu', e => {
      e.preventDefault();
      e.stopPropagation();
      _openFolderMenu(e, folder, { x: e.pageX, y: e.pageY });
    });
    //header.appendChild(folderMenuBtn);
    folderEl.appendChild(header);

    _attachFolderDragHandlers(header, folderEl, folder);

    if (!collapsed) {
      const body = document.createElement('div');
      body.className = 'folder-body';

      // Render child folders first
      _renderFolderSubtree(folder.id, depth + 1, body, childMap, sessionsByFolder, folderIds, filterActive, onSession);

      if (folderSessions.length === 0 && !(childMap.get(folder.id) || []).length) {
        body.innerHTML += '<div class="folder-empty">Drop sessions here</div>';
      } else {
        for (const s of folderSessions) {
          const el = _makeSessionEl(s);
          _attachSessionDragHandlers(el, s);
          if (onSession) onSession(el, s);
          body.appendChild(el);
        }
      }

      // Drop zone for empty area inside folder body
      body.addEventListener('dragover', e => {
        if (_isFolderDropBlocked(folder.id)) return;
        if (e.target === body || e.target.classList.contains('folder-empty')) {
          e.preventDefault();
          e.dataTransfer.dropEffect = 'move';
          folderEl.classList.add('drag-over');
        }
      });
      body.addEventListener('dragleave', e => {
        if (!body.contains(e.relatedTarget)) folderEl.classList.remove('drag-over');
      });
      body.addEventListener('drop', e => {
        if (_isFolderDropBlocked(folder.id)) return;
        if (e.target === body || e.target.classList.contains('folder-empty')) {
          e.preventDefault();
          e.stopPropagation();
          folderEl.classList.remove('drag-over');
          _handleDropIntoFolder(folder.id);
        }
      });

      folderEl.appendChild(body);
    }

    container.appendChild(folderEl);
  }
}

function _makeSessionEl(s) {
  const isActive   = s.id === state.sessionId;
  const isLive     = isActive && state.isRecording;
  const isSelected = _sidebarSelected.has(s.id);

  const el = document.createElement('div');
  el.className = `session-item${isActive ? ' active' : ''}${isSelected ? ' selected' : ''}`;
  el.dataset.id = s.id;
  el.draggable  = true;

  el.addEventListener('dragstart', e => {
    _internalDragActive = true;
    _sidebarDragType = 'session';
    _sidebarDragIds = isSelected && _sidebarSelected.size > 1
      ? [..._sidebarSelected]
      : [s.id];
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', JSON.stringify(_sidebarDragIds));
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => { _internalDragActive = false; el.classList.remove('dragging'); _removeDragIndicator(); });

  el.addEventListener('click', e => {
    if (e.ctrlKey || e.metaKey || _sidebarMultiselect) {
      e.stopPropagation();
      _toggleSidebarSelect(s.id);
      return;
    }
    // Snap the active class onto this row immediately so the click feels
    // responsive - loadSession is async (fetch + render), and waiting for
    // it to finish before flipping the highlight makes the click feel
    // dead. The next sidebar render reapplies it idempotently.
    document.querySelectorAll('.session-item.active').forEach(n => n.classList.remove('active'));
    el.classList.add('active');
    loadSession(s.id);
  });

  const dot = document.createElement('div');
  dot.className = `session-dot${isLive ? ' live' : ''}`;

  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'session-checkbox';
  cb.checked = isSelected;
  cb.addEventListener('click', e => { e.stopPropagation(); _toggleSidebarSelect(s.id); });

  const info = document.createElement('div');
  info.className = 'session-info';
  const nameEl = document.createElement('div');
  nameEl.className = 'session-name';
  nameEl.textContent = s.title;
  // Subtle spinner suffix while an AI title regeneration is in flight
  if (_retitleInFlight && _retitleInFlight.has(s.id)) {
    const spin = document.createElement('i');
    spin.className = 'fa-solid fa-wand-magic-sparkles fa-fade session-name-retitle';
    spin.title = 'Regenerating title…';
    nameEl.appendChild(document.createTextNode(' '));
    nameEl.appendChild(spin);
  }
  const metaEl = document.createElement('div');
  metaEl.className = 'session-meta';
  metaEl.innerHTML = formatSessionMeta(s);
  // Speaker initial icons after duration
  if (s.speakers?.length) {
    const filtered = s.speakers.filter(sp => sp.name && !/^Speaker \d+$/i.test(sp.name) && sp.name.toLowerCase() !== _NOISE_LABEL.toLowerCase());
    if (filtered.length) {
      const sep = document.createElement('span');
      sep.className = 'session-meta-sep';
      sep.textContent = '|';
      metaEl.appendChild(sep);
      const wrap = document.createElement('span');
      wrap.className = 'session-speaker-icons';
      for (const sp of filtered) {
        const initials = sp.name.split(/\s+/).map(w => w[0]).join('').toUpperCase().slice(0, 2);
        const icon = document.createElement('span');
        icon.className = 'session-speaker-icon';
        icon.textContent = initials;
        icon.title = sp.name;
        if (sp.color) {
          icon.style.backgroundColor = sp.color + '30';
          icon.style.color = sp.color;
          icon.style.borderColor = sp.color + '50';
        }
        wrap.appendChild(icon);
      }
      metaEl.appendChild(wrap);
    }
  }
  info.appendChild(nameEl);
  info.appendChild(metaEl);

  el.appendChild(cb);
  el.appendChild(dot);
  el.appendChild(info);

  //const menuBtn = document.createElement('button');
  //menuBtn.className = 'session-menu-btn';
  //menuBtn.title = 'More options';
  //menuBtn.innerHTML = '<i class="fa-solid fa-ellipsis-vertical"></i>';
  //menuBtn.addEventListener('click', e => { e.stopPropagation(); _openSessionMenu(e, s); });
  //el.appendChild(menuBtn);

  // Right-click context menu
  el.addEventListener('contextmenu', e => {
    e.preventDefault();
    e.stopPropagation();
    _openSessionMenu(e, s, { x: e.pageX, y: e.pageY });
  });

  return el;
}

// ── Sidebar selection ─────────────────────────────────────────────────────────

function _toggleSidebarSelect(sessionId) {
  if (_sidebarSelected.has(sessionId)) {
    _sidebarSelected.delete(sessionId);
  } else {
    _sidebarSelected.add(sessionId);
  }
  _renderSidebar();
}

function clearSidebarSelection() {
  _sidebarSelected.clear();
  _sidebarMultiselect = false;
  const btn     = document.getElementById('sidebar-multiselect-btn');
  const sidebar = document.getElementById('sidebar');
  if (btn)     btn.classList.remove('active');
  if (sidebar) sidebar.classList.remove('multiselect');
  _renderSidebar();
}

function toggleMultiselect() {
  _sidebarMultiselect = !_sidebarMultiselect;
  if (!_sidebarMultiselect) _sidebarSelected.clear();
  const btn     = document.getElementById('sidebar-multiselect-btn');
  const sidebar = document.getElementById('sidebar');
  if (btn)     btn.classList.toggle('active', _sidebarMultiselect);
  if (sidebar) sidebar.classList.toggle('multiselect', _sidebarMultiselect);
  _renderSidebar();
}

function _updateBulkBar() {
  const bar   = document.getElementById('sidebar-bulk-bar');
  const count = document.getElementById('sidebar-bulk-count');
  const n     = _sidebarSelected.size;
  bar.classList.toggle('hidden', n === 0);
  if (count) count.textContent = `${n} selected`;
}

// ── Session context menu ───────────────────────────────────────────────────────

function _openSessionMenu(e, s, pos) {
  _closeSessionMenu();
  _closeFolderMenu();

  const menu = document.createElement('div');
  menu.className = 'session-menu';
  menu.id = 'session-menu-popup';

  if (s.has_audio) {
    const rea = document.createElement('div');
    rea.className = 'session-menu-item';
    rea.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i>  Reanalyze…';
    rea.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); openReanalyzeDialog(s.id); });
    menu.appendChild(rea);
  }

  const exp = document.createElement('div');
  exp.className = 'session-menu-item';
  exp.innerHTML = '<i class="fa-duotone fa-file-export"></i>  Export';
  exp.addEventListener('click', ev => {
    ev.stopPropagation(); _closeSessionMenu();
    // Load the session first if not already active, then open export
    if (state.sessionId !== s.id) loadSession(s.id);
    // Brief delay to let session load before opening modal
    setTimeout(() => openExportModal(s.id), state.sessionId === s.id ? 0 : 300);
  });
  menu.appendChild(exp);

  const ren = document.createElement('div');
  ren.className = 'session-menu-item';
  ren.innerHTML = '<i class="fa-solid fa-pen"></i>  Rename';
  ren.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); startEditTitle(ev, s.id, s.title); });
  menu.appendChild(ren);

  const wand = document.createElement('div');
  wand.className = 'session-menu-item';
  wand.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i>  Update Title';
  wand.addEventListener('click', ev => {
    ev.stopPropagation(); _closeSessionMenu();
    retitleSessions([s.id], { label: 'title' });
  });
  menu.appendChild(wand);

  // The id is what the agent API and the MCP tools take, so it is one click
  // away here rather than something to dig out of the address bar.
  const copyId = document.createElement('div');
  copyId.className = 'session-menu-item';
  copyId.innerHTML = '<i class="fa-solid fa-fingerprint"></i>  Copy ID';
  copyId.addEventListener('click', ev => {
    ev.stopPropagation(); _closeSessionMenu();
    _copySessionId(s.id);
  });
  menu.appendChild(copyId);

  // Only surface "Undo Split" when this row is part of a split group whose
  // backup is still available. We ask the server on click (cheap) instead of
  // decorating every session with the flag up front.
  if (s.split_group_id) {
    const undo = document.createElement('div');
    undo.className = 'session-menu-item';
    undo.innerHTML = '<i class="fa-solid fa-rotate-left"></i>  Undo Split…';
    undo.addEventListener('click', ev => {
      ev.stopPropagation(); _closeSessionMenu();
      openSplitRestoreDialog(s.id);
    });
    menu.appendChild(undo);
  }

  const del = document.createElement('div');
  del.className = 'session-menu-item session-menu-item-danger';
  del.innerHTML = '<i class="fa-solid fa-trash"></i>  Delete';
  del.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); deleteSession(ev, s.id); });
  menu.appendChild(del);

  document.body.appendChild(menu);

  // Position: use explicit pos (right-click) or fall back to button rect
  let top, left;
  if (pos) {
    top = pos.y;
    left = pos.x;
  } else {
    const rect = e.currentTarget.getBoundingClientRect();
    top = rect.bottom + window.scrollY;
    left = rect.left + window.scrollX;
  }
  if (left + 160 > window.innerWidth) left = window.innerWidth - 164;
  // Clamp vertically so menu doesn't overflow bottom
  const menuRect = menu.getBoundingClientRect();
  if (top + menuRect.height > window.innerHeight + window.scrollY) {
    top = window.innerHeight + window.scrollY - menuRect.height - 8;
  }
  menu.style.top  = top  + 'px';
  menu.style.left = left + 'px';

  setTimeout(() => document.addEventListener('click', _closeSessionMenu, { once: true }), 0);
}

function _closeSessionMenu() {
  const m = document.getElementById('session-menu-popup');
  if (m) m.remove();
}

/** Put a recording's id on the clipboard and say so. */
function _copySessionId(id) {
  const text = String(id || '');
  if (!text) return;
  const done = () => uiToast({ message: 'Recording ID copied', kind: 'success', duration: 2500 });
  const fail = () => uiToast({ message: `Could not copy. The ID is ${text}`, kind: 'warn', duration: 8000 });
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, fail);
  } else {
    fail();
  }
}

// ── Folder context menu ───────────────────────────────────────────────────────

function _openFolderMenu(e, folder, pos) {
  _closeFolderMenu();
  _closeSessionMenu();

  const menu = document.createElement('div');
  menu.className = 'session-menu';
  menu.id = 'folder-menu-popup';

  const sub = document.createElement('div');
  sub.className = 'session-menu-item';
  sub.innerHTML = '<i class="fa-solid fa-folder-plus"></i>  New subfolder';
  sub.addEventListener('click', ev => {
    ev.stopPropagation(); _closeFolderMenu();
    createSubfolder(folder.id);
  });
  menu.appendChild(sub);

  const ren = document.createElement('div');
  ren.className = 'session-menu-item';
  ren.innerHTML = '<i class="fa-solid fa-pen"></i>  Rename';
  ren.addEventListener('click', ev => {
    ev.stopPropagation(); _closeFolderMenu();
    renameFolderInline(ev, folder.id, folder.name);
  });
  menu.appendChild(ren);

  const wand = document.createElement('div');
  wand.className = 'session-menu-item';
  wand.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i>  Update Titles';
  wand.addEventListener('click', ev => {
    ev.stopPropagation(); _closeFolderMenu();
    retitleFolder(folder.id, folder.name);
  });
  menu.appendChild(wand);

  const del = document.createElement('div');
  del.className = 'session-menu-item session-menu-item-danger';
  del.innerHTML = '<i class="fa-solid fa-trash"></i>  Delete';
  del.addEventListener('click', ev => {
    ev.stopPropagation(); _closeFolderMenu();
    deleteFolder(ev, folder.id);
  });
  menu.appendChild(del);

  document.body.appendChild(menu);

  // Position: use explicit pos (right-click) or fall back to button rect
  let top, left;
  if (pos) {
    top = pos.y;
    left = pos.x;
  } else {
    const rect = e.currentTarget.getBoundingClientRect();
    top = rect.bottom + window.scrollY;
    left = rect.left + window.scrollX;
  }
  if (left + 160 > window.innerWidth) left = window.innerWidth - 164;
  const menuRect = menu.getBoundingClientRect();
  if (top + menuRect.height > window.innerHeight + window.scrollY) {
    top = window.innerHeight + window.scrollY - menuRect.height - 8;
  }
  menu.style.top  = top  + 'px';
  menu.style.left = left + 'px';

  setTimeout(() => document.addEventListener('click', _closeFolderMenu, { once: true }), 0);
}

function _closeFolderMenu() {
  const m = document.getElementById('folder-menu-popup');
  if (m) m.remove();
}

// ── Folder actions ────────────────────────────────────────────────────────────

function _toggleFolder(folderId) {
  if (_sidebarCollapsed.has(folderId)) _sidebarCollapsed.delete(folderId);
  else _sidebarCollapsed.add(folderId);
  try { localStorage.setItem(_FOLDER_STATE_KEY, JSON.stringify([..._sidebarCollapsed])); } catch (_) {}
  _renderSidebar();
}

// Expand every ancestor folder of the given session so the active session
// is visible in the sidebar. Persists the new collapsed-set to localStorage
// and re-renders. No-op if the session isn't in any folder.
function _revealSessionInSidebar(sessionId) {
  if (!sessionId) return;
  const sess = _sidebarAllSessions.find(s => s.id === sessionId);
  if (!sess || !sess.folder_id) return;
  const folderById = new Map(_sidebarFolders.map(f => [f.id, f]));
  let changed = false;
  let cursor = folderById.get(sess.folder_id);
  // Walk up the parent chain; guard against cycles with a seen-set.
  const seen = new Set();
  while (cursor && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    if (_sidebarCollapsed.delete(cursor.id)) changed = true;
    cursor = cursor.parent_id ? folderById.get(cursor.parent_id) : null;
  }
  if (changed) {
    try { localStorage.setItem(_FOLDER_STATE_KEY, JSON.stringify([..._sidebarCollapsed])); } catch (_) {}
    _renderSidebar();
  }
}

async function createFolder() {
  const name = await uiPrompt({ title: 'New folder', message: 'Folder name:', validate: v => v.trim() ? null : 'Enter a folder name.' });
  if (name === null) return;
  await fetch('/api/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim() }),
  });
  refreshSidebar();
}

async function createSubfolder(parentId) {
  const name = await uiPrompt({ title: 'New subfolder', message: 'Subfolder name:', validate: v => v.trim() ? null : 'Enter a subfolder name.' });
  if (name === null) return;
  // Expand the parent folder so the new subfolder is visible
  _sidebarCollapsed.delete(parentId);
  try { localStorage.setItem(_FOLDER_STATE_KEY, JSON.stringify([..._sidebarCollapsed])); } catch (_) {}
  await fetch('/api/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim(), parent_id: parentId }),
  });
  refreshSidebar();
}

async function deleteFolder(e, folderId) {
  e.stopPropagation();
  const folder = _sidebarFolders.find(f => f.id === folderId);
  const folderName = folder?.name || folderId;

  // Count all sessions and subfolders recursively
  const childMap = _buildChildMap(_sidebarFolders);
  const allFolderIds = new Set();
  const stack = [folderId];
  while (stack.length) {
    const id = stack.pop();
    allFolderIds.add(id);
    for (const c of (childMap.get(id) || [])) stack.push(c.id);
  }
  const sessionCount = _sidebarAllSessions.filter(s => allFolderIds.has(s.folder_id)).length;
  const subfolderCount = allFolderIds.size - 1; // exclude the folder itself

  // Build a descriptive warning
  const parts = [];
  if (sessionCount) parts.push(`${sessionCount} session${sessionCount > 1 ? 's' : ''}`);
  if (subfolderCount) parts.push(`${subfolderCount} subfolder${subfolderCount > 1 ? 's' : ''}`);
  const contentsDesc = parts.length ? parts.join(' and ') : null;

  let deleteContents = false;
  if (contentsDesc) {
    const msg = `Delete folder "${folderName}"?\n\n`
      + `This folder contains ${contentsDesc}.\n\n`
      + `• OK = permanently delete the folder and all its contents\n`
      + `• Cancel = keep everything`;
    if (!await uiConfirm({ title: `Delete "${folderName}"?`, message: `This folder contains ${contentsDesc}.`, details: ['Permanently delete the folder and all its contents'], confirmLabel: 'Delete', danger: true })) return;
    deleteContents = true;
  } else {
    if (!await uiConfirm({ title: `Delete empty folder "${folderName}"?`, confirmLabel: 'Delete', danger: true })) return;
  }

  await fetch(`/api/folders/${folderId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ delete_contents: deleteContents }),
  });
  // Clean up collapsed state for this folder and any subfolders
  for (const id of allFolderIds) _sidebarCollapsed.delete(id);
  try { localStorage.setItem(_FOLDER_STATE_KEY, JSON.stringify([..._sidebarCollapsed])); } catch (_) {}
  refreshSidebar();
}

function renameFolderInline(e, folderId, currentName) {
  e.stopPropagation();
  const folderEl = document.querySelector(`.sidebar-folder[data-folder-id="${folderId}"]`);
  const nameEl = folderEl?.querySelector('.folder-name');
  if (!nameEl) return;

  const input = document.createElement('input');
  input.className = 'folder-name-input';
  input.value = currentName;
  nameEl.replaceWith(input);
  input.focus(); input.select();

  let done = false;
  const commit = async () => {
    if (done) return; done = true;
    const newName = input.value.trim();
    const restored = document.createElement('span');
    restored.className = 'folder-name';
    restored.textContent = newName || currentName;
    input.replaceWith(restored);
    if (newName && newName !== currentName) {
      await fetch(`/api/folders/${folderId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newName }),
      });
      refreshSidebar();
    }
  };
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', ev => {
    if (ev.key === 'Enter')  { ev.preventDefault(); input.blur(); }
    if (ev.key === 'Escape') { ev.preventDefault(); done = true; input.replaceWith(nameEl); }
  });
}

/* ── Optimistic sidebar moves ──────────────────────────────────────────────
 * A drop used to wait for the write AND a full sidebar refetch before the item
 * appeared in its new place, so every move visibly snapped back first. The new
 * arrangement is already computed on the client (it is exactly what gets sent),
 * so apply it to the local model, repaint at once, and only fall back to the
 * server if the write fails.
 */
function _applyReorderLocally({ sessions, folders }) {
  if (sessions && sessions.length) {
    const byId = new Map(_sidebarAllSessions.map(s => [s.id, s]));
    for (const p of sessions) {
      const s = byId.get(p.id);
      if (!s) continue;
      s.sort_order = p.sort_order;
      if (p.folder_id !== undefined) s.folder_id = p.folder_id;
    }
  }
  if (folders && folders.length) {
    const byId = new Map(_sidebarFolders.map(f => [f.id, f]));
    for (const p of folders) {
      const f = byId.get(p.id);
      if (!f) continue;
      f.sort_order = p.sort_order;
      if (p.parent_id !== undefined) f.parent_id = p.parent_id;
    }
    // Folders render in array order within each parent, and the server hands
    // them back ordered by sort_order then created_at. Match that here so the
    // optimistic tree and the next refresh agree.
    _sidebarFolders.sort((a, b) =>
      (a.sort_order || 0) - (b.sort_order || 0) ||
      String(a.created_at || '').localeCompare(String(b.created_at || '')));
  }
}

/** Paint `payload` immediately, then persist it. `request` returns the fetch
 *  promise; any failure resyncs from the server so the sidebar can never keep
 *  showing a move the database rejected. */
function _commitSidebarMove(payload, request) {
  _applyReorderLocally(payload);
  _sidebarSelected.clear();
  _renderSidebar();
  return request()
    .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); })
    .catch(() => {
      flashStatus('Could not save the new order');
      refreshSidebar();
    });
}

function _postJson(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** Mirror of storage.bulk_set_folder: moved sessions land at the end of the
 *  target folder, numbered from its current highest sort_order. */
function _sessionMovePayload(ids, folderId) {
  const maxOrder = folderId
    ? _sidebarAllSessions.reduce(
        (m, s) => (s.folder_id === folderId ? Math.max(m, s.sort_order || 0) : m), 0)
    : 0;
  return ids.map((id, i) => ({ id, sort_order: maxOrder + 1 + i, folder_id: folderId || null }));
}

// ── Drop handlers ─────────────────────────────────────────────────────────────

function _handleDropIntoFolder(folderId) {
  const ids = _sidebarDragIds.length ? _sidebarDragIds : [];
  if (!ids.length) return;

  if (_sidebarDragType === 'folder') {
    // Safety: never drop a folder into itself or its own descendant
    if (ids.includes(folderId)) return;
    if (ids.some(id => _dragDescendants.has(folderId))) return;
    // Move folder(s) into another folder as sub-folders
    const payload = ids.map((id, i) => ({ id, sort_order: i, parent_id: folderId }));
    return _commitSidebarMove({ folders: payload },
      () => _postJson('/api/reorder', { folders: payload }));
  } else {
    // Move session(s) into folder (or out of one, when folderId is null)
    const payload = _sessionMovePayload(ids, folderId);
    return _commitSidebarMove({ sessions: payload },
      () => _postJson('/api/sessions/bulk',
                      { action: 'move', session_ids: ids, folder_id: folderId }));
  }
}

function _handleDropFolderToTopLevel() {
  const ids = _sidebarDragIds.length ? _sidebarDragIds : [];
  if (!ids.length || _sidebarDragType !== 'folder') return;
  // Move to top level at the end
  const topFolders = _sidebarFolders.filter(f => !f.parent_id);
  const maxOrder = topFolders.reduce((m, f) => Math.max(m, f.sort_order || 0), 0);
  const payload = ids.map((id, i) => ({ id, sort_order: maxOrder + 1 + i, parent_id: null }));
  return _commitSidebarMove({ folders: payload },
    () => _postJson('/api/reorder', { folders: payload }));
}

function _handleDrop(targetId, targetType, zone, parentContext) {
  // parentContext = folder_id for sessions, parent_id for folders
  if (_sidebarDragType === 'session' && targetType === 'session') {
    _reorderSessions(targetId, zone, parentContext);
  } else if (_sidebarDragType === 'folder' && targetType === 'folder') {
    _reorderFolders(targetId, zone);
  } else if (_sidebarDragType === 'session' && targetType === 'folder') {
    // Session dropped on edge of a folder - treat as drop into the folder
    _handleDropIntoFolder(targetId);
  } else if (_sidebarDragType === 'folder' && targetType === 'session') {
    // Folder dropped on a session edge - ignore (doesn't make sense)
    return;
  }
}

function _reorderSessions(targetSessionId, zone, folderId) {
  if (!_sidebarDragIds.length) return;
  // Only reorder within the same folder
  const targetSession = _sidebarAllSessions.find(s => s.id === targetSessionId);
  if (!targetSession) return;
  const inFolder = targetSession.folder_id;

  // Get sibling sessions in this folder, sorted by current sort_order
  const siblings = _sidebarAllSessions
    .filter(s => s.folder_id === inFolder)
    .sort((a, b) => a.sort_order - b.sort_order);

  // Remove dragged items from the list
  const dragSet = new Set(_sidebarDragIds);
  const remaining = siblings.filter(s => !dragSet.has(s.id));
  const dragged = siblings.filter(s => dragSet.has(s.id));

  // Also handle cross-folder moves: sessions being dragged from another folder
  const draggedAll = _sidebarDragIds.map(id =>
    _sidebarAllSessions.find(s => s.id === id)
  ).filter(Boolean);

  // Find insertion index
  const targetIdx = remaining.findIndex(s => s.id === targetSessionId);
  const insertIdx = zone === 'before' ? targetIdx : targetIdx + 1;

  // Insert dragged sessions at the new position
  remaining.splice(insertIdx, 0, ...draggedAll);

  // Assign sequential sort_order and ensure folder_id is correct
  const payload = remaining.map((s, i) => ({
    id: s.id,
    sort_order: i,
    folder_id: inFolder,
  }));

  return _commitSidebarMove({ sessions: payload },
    () => _postJson('/api/reorder', { sessions: payload }));
}

function _reorderFolders(targetFolderId, zone) {
  if (!_sidebarDragIds.length) return;
  const targetFolder = _sidebarFolders.find(f => f.id === targetFolderId);
  if (!targetFolder) return;
  const parentId = targetFolder.parent_id || null;

  // Get sibling folders under the same parent
  const siblings = _sidebarFolders
    .filter(f => (f.parent_id || null) === parentId)
    .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));

  const dragSet = new Set(_sidebarDragIds);
  const remaining = siblings.filter(f => !dragSet.has(f.id));
  const draggedAll = _sidebarDragIds.map(id =>
    _sidebarFolders.find(f => f.id === id)
  ).filter(Boolean);

  const targetIdx = remaining.findIndex(f => f.id === targetFolderId);
  const insertIdx = zone === 'before' ? targetIdx : targetIdx + 1;
  remaining.splice(insertIdx, 0, ...draggedAll);

  const payload = remaining.map((f, i) => ({
    id: f.id,
    sort_order: i,
    parent_id: parentId,
  }));

  return _commitSidebarMove({ folders: payload },
    () => _postJson('/api/reorder', { folders: payload }));
}

// Legacy alias for any remaining references
function _dropIntoFolder(folderId) { _handleDropIntoFolder(folderId); }

// ── Bulk actions ──────────────────────────────────────────────────────────────

async function bulkDelete() {
  const ids = [..._sidebarSelected];
  if (!ids.length) return;
  if (!await uiConfirm({ title: 'Delete sessions?', message: `Delete ${ids.length} session${ids.length === 1 ? '' : 's'} and all their data?`, confirmLabel: 'Delete', danger: true })) return;
  await fetch('/api/sessions/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'delete', session_ids: ids }),
  });
  _sidebarSelected.clear();
  if (ids.includes(state.sessionId) && !state.isRecording) {
    newSession();
    return;
  }
  refreshSidebar();
}

async function bulkRetitle() {
  const ids = [..._sidebarSelected];
  if (!ids.length) return;
  const btn = document.getElementById('sidebar-bulk-retitle');
  if (btn) { btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> …'; btn.disabled = true; }
  try {
    await retitleSessions(ids, { label: ids.length === 1 ? 'title' : 'titles', silent: true });
  } finally {
    if (btn) { btn.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i> Titles'; btn.disabled = false; }
    refreshSidebar();
  }
}

/* ── Generic retitle helpers (used by sidebar context menus) ──────────────── */

// Tracks active retitle batches so the sidebar can show per-row spinners
// while workers are still processing. Set is cleared on retitle_done.
const _retitleInFlight = new Set();

async function retitleSessions(sessionIds, opts = {}) {
  const ids = (sessionIds || []).filter(Boolean);
  if (!ids.length) return { updated: [] };
  const label = opts.label || 'titles';
  // Mark them as in-flight for sidebar visual feedback
  ids.forEach(id => _retitleInFlight.add(id));
  _renderSidebar();
  const startMsg = ids.length === 1 ? `Updating ${label}…` : `Updating ${ids.length} ${label}…`;
  if (!opts.silent) flashStatus(startMsg);
  try {
    const r = await fetch('/api/sessions/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'retitle',
        session_ids: opts.folderId ? undefined : ids,
        folder_id:   opts.folderId,
      }),
    }).then(r => r.json());
    if (r.error) throw new Error(r.error);
    if (!opts.silent) {
      const n = (r.updated || []).length;
      flashStatus(n === 1 ? 'Title updated' : `${n} titles updated`);
    }
    return r;
  } catch (e) {
    flashStatus(`Update failed: ${e.message || e}`);
    throw e;
  } finally {
    ids.forEach(id => _retitleInFlight.delete(id));
    _renderSidebar();
  }
}

async function retitleFolder(folderId, folderName) {
  // Probe count via a quick session-list filter on the cached sidebar state.
  // (A folder may also contain sub-folders; we surface the local count for
  // the confirm dialog but defer authoritative recursion to the server.)
  const directCount = _sidebarAllSessions.filter(s => s.folder_id === folderId).length;
  const fname = folderName || 'this folder';
  const msg = directCount > 0
    ? `Regenerate AI titles for all sessions in "${fname}" (and any subfolders)?\n\n` +
      `At least ${directCount} session${directCount === 1 ? '' : 's'} in this folder will be re-named.`
    : `Regenerate AI titles for all sessions in "${fname}" and its subfolders?`;
  if (!await uiConfirm({ title: 'Regenerate AI titles?', message: msg, confirmLabel: 'Regenerate' })) return;
  // For folder mode the server resolves the IDs (recursive walk); we still
  // pass folderId through to retitleSessions so it bypasses the in-flight
  // visual cache (we don't have the IDs upfront).
  const fakeIds = [`__folder:${folderId}`];
  // Use a dedicated path so the in-flight set isn't polluted with a fake id
  flashStatus(`Updating titles in "${fname}"…`);
  try {
    const r = await fetch('/api/sessions/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'retitle', folder_id: folderId }),
    }).then(r => r.json());
    if (r.error) throw new Error(r.error);
    const n = (r.updated || []).length;
    const req = r.requested || n;
    if (req === 0) flashStatus('No sessions to retitle');
    else if (n === req) flashStatus(`${n} title${n === 1 ? '' : 's'} updated`);
    else flashStatus(`${n}/${req} title${req === 1 ? '' : 's'} updated`);
  } catch (e) {
    flashStatus(`Update failed: ${e.message || e}`);
  }
}

function groupByDate(sessions) {
  const now   = new Date();
  const today = dateKey(now);
  const yest  = dateKey(new Date(now - 864e5));
  const weekAgo = new Date(now - 7 * 864e5);

  const map = new Map();
  for (const s of sessions) {
    const d   = new Date(s.started_at + 'Z');
    const key = dateKey(d);
    let label;
    if (key === today)       label = 'Today';
    else if (key === yest)   label = 'Yesterday';
    else if (d >= weekAgo)   label = 'This Week';
    else                     label = d.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

    if (!map.has(label)) map.set(label, []);
    map.get(label).push(s);
  }
  return map;
}

function dateKey(d) {
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

function formatSessionMeta(s) {
  const start = new Date(s.started_at + 'Z');
  const now   = new Date();
  const isToday = start.toDateString() === now.toDateString();
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  const isYesterday = start.toDateString() === yesterday.toDateString();
  const datePart = isToday ? 'Today'
    : isYesterday ? 'Yesterday'
    : start.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: start.getFullYear() !== now.getFullYear() ? 'numeric' : undefined });
  const timePart = start.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  const time = `${datePart}, ${timePart}`;
  // Only call a session "In progress" when it actually is the active
  // recording. Stale ended_at=NULL rows from app crashes / aborted splits
  // would otherwise mislead the sidebar - fall through and compute the
  // duration from last_segment_time instead.
  const isActiveRecording = state.sessionId === s.id && state.isRecording;
  if (!s.ended_at && isActiveRecording) {
    return `${time} <span class="session-meta-sep">|</span> In progress`;
  }
  // Use actual transcript duration (last segment end_time) when available,
  // falling back to wall-clock duration between start/end timestamps.
  let secs = s.last_segment_time;
  if (secs == null || secs <= 0) {
    if (s.ended_at) {
      const end = new Date(s.ended_at + 'Z');
      secs = (end - start) / 1000;
    } else {
      // No ended_at and no segments - show just the date/time.
      return time;
    }
  }
  return `${time} <span class="session-meta-sep">|</span> ${fmtDuration(secs)}`;
}

async function deleteSession(e, sessionId) {
  e.stopPropagation();
  await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
  if (sessionId === state.sessionId && !state.isRecording) {
    newSession();
    return;
  }
  refreshSidebar();
}

/**
 * Make a session the active one before a reanalysis starts on it.
 *
 * Every reanalysis_* and transcript_reset handler ignores events whose
 * session_id is not state.sessionId, so a reanalysis launched from the sidebar
 * for some other meeting would leave state.isReanalyzing false and the record
 * button live: the app could then start a recording into a session the batch
 * pipeline is already writing. Returns false when the session cannot be loaded.
 */
async function _adoptSessionForReanalysis(sessionId) {
  if (sessionId !== state.sessionId) {
    const data = await fetch(`/api/sessions/${sessionId}`).then(r => r.json()).catch(() => ({ error: 'Could not load that meeting.' }));
    if (!data || data.error) {
      uiToast({ message: (data && data.error) || 'Could not load that meeting.', kind: 'error' });
      return false;
    }
    state.sessionId = sessionId;
    state.isViewingPast = false;
    const btn = document.getElementById('record-btn');
    if (btn) btn.disabled = true;
    if (data.speaker_profiles?.length) {
      data.speaker_profiles.forEach(p => applySpeakerProfileUpdate(p));
    }
  } else {
    state.isViewingPast = false;
    const btn = document.getElementById('record-btn');
    if (btn) btn.disabled = true;
  }

  // Clear only the transcript display - keep chat and summary intact
  const transcriptEl = document.getElementById('transcript');
  if (transcriptEl) transcriptEl.innerHTML = '';

  // Keep playback available during reanalysis - the WAV file still exists
  initPlayback(sessionId);
  return true;
}

async function reanalyzeSession(e, sessionId, opts) {
  if (e) e.stopPropagation();
  if (state.isRecording) { uiToast({ message: 'Cannot reanalyze while recording.', kind: 'warn' }); return; }
  if (state.isReanalyzing) { uiToast({ message: 'Reanalysis already in progress.', kind: 'warn' }); return; }
  opts = opts || {};

  if (!await _adoptSessionForReanalysis(sessionId)) return;

  const customPrompt = document.getElementById('summary-custom-prompt')?.value || '';
  const reqBody = { custom_prompt: customPrompt };
  // Per-meeting speaker-count dial (from the Reanalyze dialog). Omitted = auto.
  // "exactly N" forces the count; "up to N" (the default whenever the number
  // came from the calendar) only caps it, which is what the dialog says.
  if (opts.numSpeakers) {
    if (opts.mode === 'exact') reqBody.num_speakers = opts.numSpeakers;
    else reqBody.max_speakers = opts.numSpeakers;
  }
  const resp = await fetch(`/api/sessions/${sessionId}/reanalyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(reqBody),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    uiToast({ message: err.error || 'Failed to start reanalysis', kind: 'error' });
  }
}

// Small per-meeting Reanalyze dialog with a speaker-count "dial". Opening this
// (rather than reanalyzing straight away) lets the user tell the diarizer how
// many people were in the room, which fixes the common under/over-split problem.
function openReanalyzeDialog(sessionId) {
  if (state.isRecording) { uiToast({ message: 'Cannot reanalyze while recording.', kind: 'warn' }); return; }
  if (state.isReanalyzing) { uiToast({ message: 'Reanalysis already in progress.', kind: 'warn' }); return; }
  if (!sessionId) return;
  document.getElementById('reanalyze-dialog')?.remove();

  const ov = document.createElement('div');
  ov.id = 'reanalyze-dialog';
  ov.className = 'overlay';
  ov.innerHTML = `
    <div class="dialog reanalyze-dialog" role="dialog" aria-modal="true" aria-label="Reanalyze meeting">
      <h2 class="reanalyze-title">Reanalyze meeting</h2>
      <p class="reanalyze-sub">Re-transcribes and re-detects speakers from the original audio. If the last pass split speakers wrong, tell it how many people were in the meeting.</p>
      <p class="reanalyze-hint" id="reanalyze-calendar" style="display:none"></p>
      <div class="reanalyze-field" id="reanalyze-match-field" style="display:none">
        <label for="reanalyze-match">Calendar meeting</label>
        <select id="reanalyze-match"></select>
        <button type="button" class="reanalyze-btn" id="reanalyze-confirm-match" style="display:none">Confirm</button>
        <span class="reanalyze-hint" id="reanalyze-match-hint">Wrong meeting? Pick the right one, or say it was not a calendar meeting. Confirming pins it and remembers the attendee count.</span>
      </div>
      <div class="reanalyze-field">
        <label for="reanalyze-speakers">How many people were in this meeting?</label>
        <select id="reanalyze-mode">
          <option value="max">Up to</option>
          <option value="exact">Exactly</option>
        </select>
        <select id="reanalyze-speakers"></select>
        <span class="reanalyze-hint" id="reanalyze-hint">Auto lets the detector decide. Pick a number if it split speakers wrong last time.</span>
      </div>
      <div class="reanalyze-actions">
        <button type="button" class="reanalyze-btn" id="reanalyze-cancel">Cancel</button>
        <button type="button" class="reanalyze-btn" id="reanalyze-smart" style="display:none"><i class="fa-solid fa-wand-magic-sparkles"></i> Smart cleanup</button>
        <button type="button" class="reanalyze-btn reanalyze-btn-primary" id="reanalyze-go"><i class="fa-solid fa-arrows-rotate"></i> Reanalyze</button>
      </div>
    </div>`;
  document.body.appendChild(ov);

  const sel = ov.querySelector('#reanalyze-speakers');
  const auto = document.createElement('option');
  auto.value = ''; auto.textContent = 'Auto (let it decide)';
  sel.appendChild(auto);
  for (let n = 1; n <= 12; n++) {
    const o = document.createElement('option');
    o.value = String(n);
    o.textContent = `${n} speaker${n > 1 ? 's' : ''}`;
    sel.appendChild(o);
  }

  // Track whether the user has deliberately touched the dropdown, so a late
  // attendee-count pre-fill never overrides their choice, including an explicit
  // re-selection of "Auto" (which leaves sel.value === '', indistinguishable
  // from untouched without this flag).
  let userTouched = false;
  const modeSel = ov.querySelector('#reanalyze-mode');
  sel.addEventListener('change', () => { userTouched = true; });
  modeSel.addEventListener('change', () => { userTouched = true; });

  const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = e => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  ov.addEventListener('click', e => { if (e.target === ov) close(); });
  ov.querySelector('#reanalyze-cancel').addEventListener('click', close);
  ov.querySelector('#reanalyze-go').addEventListener('click', () => {
    const n = parseInt(sel.value, 10);
    const mode = modeSel.value === 'exact' ? 'exact' : 'max';
    close();
    reanalyzeSession(null, sessionId, {
      numSpeakers: Number.isFinite(n) ? n : null,
      mode,
    });
  });
  sel.focus();

  ov.querySelector('#reanalyze-smart').addEventListener('click', () => {
    close();
    runSmartCleanup(sessionId);
  });

  // Show the calendar match, let the user correct it, and pre-fill the count.
  // The calendar match is authoritative; the resolve-step candidates file is
  // the fallback for meetings matched before the calendar link existed.
  // Best-effort and non-blocking: skip silently when there is no data, and
  // never override the dialog once it is gone or the user has touched the dial.
  const matchField = ov.querySelector('#reanalyze-match-field');
  const matchSel   = ov.querySelector('#reanalyze-match');
  const calLine    = ov.querySelector('#reanalyze-calendar');
  const hintEl     = ov.querySelector('#reanalyze-hint');

  const confirmBtn = ov.querySelector('#reanalyze-confirm-match');

  function _matchLabel(entry) {
    const title = entry.title || 'Untitled meeting';
    const when = entry.start ? _fmtCalendarTime(entry.start) : '';
    const count = Number(entry.attendee_count);
    const people = Number.isFinite(count) && count >= 1
      ? `, ${count} attendee${count > 1 ? 's' : ''}`
      : '';
    return when ? `${title} (${when}${people})` : title;
  }

  // True while the dial holds a number the calendar supplied.
  let _calendarPrefilled = false;

  // The mode control is meaningless while the dial reads Auto.
  function _syncModeControl() {
    modeSel.disabled = !sel.value;
    modeSel.style.opacity = sel.value ? '' : '0.5';
  }
  sel.addEventListener('change', () => {
    // A hand-picked number is an exact count, the way the dial always behaved.
    // Only a calendar-supplied number defaults to a ceiling.
    if (sel.value && !_calendarPrefilled) modeSel.value = 'exact';
    _syncModeControl();
  });
  _syncModeControl();

  async function loadMatch() {
    let match = null;
    let alternatives = [];
    let cleared = false;
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/calendar_match`);
      if (r.ok) {
        const data = await r.json();
        match = (data && data.match) || null;
        alternatives = (data && data.alternatives) || [];
        cleared = !!(data && data.cleared);
      }
    } catch (_) {}
    if (!ov.isConnected) return;

    let count = null;
    if (match) {
      const n = Number(match.attendee_count);
      if (Number.isFinite(n) && n >= 1) count = n;
      const title = match.title || 'Untitled meeting';
      let line = count
        ? `Calendar: ${title}, ${count} attendee${count > 1 ? 's' : ''}.`
        : `Calendar: ${title}, attendees not shared by the calendar.`;
      if (match.confirmed) line += ' Confirmed by you.';
      else if (match.reason) line += ` Matched by time (${match.reason}).`;
      calLine.textContent = line;
      ov.querySelector('#reanalyze-smart').style.display = '';
    } else {
      calLine.textContent = cleared
        ? 'Marked as not a calendar meeting.'
        : 'No calendar meeting matched this recording.';
      ov.querySelector('#reanalyze-smart').style.display = 'none';
    }
    calLine.style.display = '';
    if (confirmBtn) {
      confirmBtn.style.display = (match && !match.confirmed) ? '' : 'none';
    }

    // The picker only appears when there is something to pick between.
    matchSel.innerHTML = '';
    if (match || alternatives.length || cleared) {
      if (match) {
        const opt = document.createElement('option');
        opt.value = 'current';
        opt.textContent = _matchLabel(match);
        matchSel.appendChild(opt);
      }
      alternatives.forEach((alt, i) => {
        const opt = document.createElement('option');
        opt.value = `alt:${i}`;
        opt.textContent = _matchLabel(alt);
        matchSel.appendChild(opt);
      });
      const none = document.createElement('option');
      none.value = 'none';
      none.textContent = 'Not a calendar meeting';
      matchSel.appendChild(none);
      matchSel.value = match ? 'current' : 'none';
      matchField.style.display = '';
      matchSel.dataset.alts = JSON.stringify(alternatives);
      matchSel.dataset.current = JSON.stringify(match || null);
    } else {
      matchField.style.display = 'none';
    }

    if (count === null) {
      try {
        const r = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/resolution_candidates`);
        if (r.ok) {
          const data = await r.json();
          const n = data && data.meeting && Number(data.meeting.attendee_count);
          if (Number.isFinite(n) && n >= 1) count = n;
        }
      } catch (_) {}
    }
    if (!ov.isConnected || userTouched) return;
    if (count !== null && count <= 12) {
      _calendarPrefilled = true;
      sel.value = String(count);
      modeSel.value = 'max';
      hintEl.textContent = `The calendar shows ${count} attendee${count > 1 ? 's' : ''}. "Up to ${count}" lets the detector find fewer; switch to "Exactly" only if you are sure.`;
    } else {
      _calendarPrefilled = false;
      sel.value = '';
      hintEl.textContent = 'Auto lets the detector decide. Pick a number if it split speakers wrong last time.';
    }
    _syncModeControl();
  }

  async function putMatch(body) {
    matchSel.disabled = true;
    if (confirmBtn) confirmBtn.disabled = true;
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/calendar_match`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        uiToast({ message: data.error || 'Could not change the calendar match.', kind: 'error' });
      }
    } catch (_) {
      uiToast({ message: 'Could not change the calendar match.', kind: 'error' });
    } finally {
      matchSel.disabled = false;
      if (confirmBtn) confirmBtn.disabled = false;
    }
    // The stored count may have changed, so re-read rather than guess. A dial
    // value the user set by hand is theirs and stays (userTouched is not reset).
    await loadMatch();
  }

  matchSel.addEventListener('change', () => {
    const choice = matchSel.value;
    let body = null;
    if (choice === 'none') {
      body = { clear: true };
    } else if (choice.startsWith('alt:')) {
      const alts = JSON.parse(matchSel.dataset.alts || '[]');
      const alt = alts[parseInt(choice.slice(4), 10)];
      if (alt) body = { uid: alt.uid, recurrence_id: alt.recurrence_id || null };
    } else {
      const current = JSON.parse(matchSel.dataset.current || 'null');
      if (current) body = { uid: current.uid, recurrence_id: current.recurrence_id || null };
    }
    if (body) putMatch(body);
  });

  // A correct machine match is pre-selected, so the picker never fires a change
  // event for it. Without this button the common case can never be confirmed,
  // and the attendee count for a recurring title is never remembered.
  if (confirmBtn) {
    confirmBtn.addEventListener('click', () => {
      const current = JSON.parse(matchSel.dataset.current || 'null');
      if (!current) return;
      putMatch({ uid: current.uid, recurrence_id: current.recurrence_id || null });
    });
  }

  loadMatch();
}

/**
 * Calendar-guided cleanup: show the plan the server computed, then run it only
 * if the user says so. The server never renames a speaker from the attendee
 * list; names appear only where the Voice Library recognises the voice during
 * the reanalysis the plan starts.
 */
async function runSmartCleanup(sessionId) {
  if (!sessionId) return;
  let result;
  try {
    result = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/smart_cleanup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apply: false }),
    }).then(r => r.json());
  } catch (_) {
    uiToast({ message: 'Could not build a cleanup plan.', kind: 'error' });
    return;
  }
  const plan = result && result.plan;
  if (!plan || result.error) {
    uiToast({ message: (result && result.error) || 'No cleanup plan available.', kind: 'error' });
    return;
  }

  const details = [];
  if (plan.expected != null) details.push(`Calendar expects ${plan.expected} in the room`);
  details.push(`Found ${plan.found} speaker${plan.found === 1 ? '' : 's'} with real talk time`);
  if (plan.unresolved) details.push(`${plan.unresolved} still unnamed`);
  if (plan.candidates && plan.candidates.length) {
    details.push(`Resolve candidates: ${plan.candidates.map(c => c.name).join(', ')}`);
  }
  details.push('Names are applied only where the Voice Library recognises the voice.');

  if (plan.action !== 'reanalyze') {
    await uiAlert({
      title: 'Smart cleanup',
      message: plan.detail || 'Nothing to clean up.',
      details,
    });
    return;
  }

  const ok = await uiConfirm({
    title: 'Run smart cleanup?',
    message: `${plan.detail} This clears the current transcript and speaker labels for this meeting.`,
    details,
    confirmLabel: 'Run cleanup',
    danger: true,
  });
  if (!ok) return;

  // Adopt the session first: the reanalysis SSE handlers only act on the
  // active meeting, and until they do the record button stays live.
  if (!await _adoptSessionForReanalysis(sessionId)) return;

  try {
    const applied = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/smart_cleanup`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apply: true }),
    }).then(r => r.json());
    if (applied.error) {
      uiToast({ message: applied.error, kind: 'error' });
      return;
    }
    if (!applied.applied) {
      uiToast({ message: applied.reason || 'Nothing to clean up.', kind: 'info' });
      return;
    }
    uiToast({
      message: applied.max_speakers
        ? `Cleanup started, capped at ${applied.max_speakers} speakers.`
        : 'Cleanup started.',
      kind: 'success',
    });
  } catch (_) {
    uiToast({ message: 'Could not start the cleanup.', kind: 'error' });
  }
}

async function reanalyzeCurrentSession() {
  if (!state.sessionId) return;
  await reanalyzeSession(null, state.sessionId);
}

async function newSession() {
  if (state.isRecording) return;
  if (_cleanupState && _cleanupState.dirty) {
    if (!await uiConfirm({ title: 'Discard staged cleanup changes?', message: 'You have unsaved speaker cleanup changes in this meeting. Switching meetings discards them.', confirmLabel: 'Discard and switch', danger: true })) return;
    _cleanupState.dirty = false;
  }
  state.sessionId    = null;
  state.isViewingPast = false;
  clearAll();
  _updateActiveFolderHighlights();
  Views.show('session', { url: '/session' });
  // Re-seed the Custom Instructions / per-session system prompt the same way
  // a fresh page load would (localStorage > default-instructions pref > "").
  loadSummaryPrompt();
  updateRecordBtn();
  _renderSidebar();
  _syncUploadBtn();
}

/* ── Audio/Video upload ──────────────────────────────────────────────────── */

/** Show upload button only when on a blank/new session (no recording, no past session). */
function _syncUploadBtn() {
  const btn = document.getElementById('upload-audio-btn');
  if (!btn) return;
  const show = !state.sessionId && !state.isRecording && !state.isViewingPast && !state.isReanalyzing;
  btn.classList.toggle('hidden', !show);
}

async function handleAudioUpload(input) {
  const file = input.files?.[0];
  input.value = '';  // reset so the same file can be re-selected
  if (!file) return;

  // Immediate visual feedback
  const btn = document.getElementById('upload-audio-btn');
  if (btn) { btn.disabled = true; btn.style.opacity = '.35'; }
  flashStatus('Uploading…');

  const form = new FormData();
  form.append('file', file);

  try {
    const resp = await fetch('/api/sessions/upload', { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok) { uiToast({ message: data.error || 'Upload failed', kind: 'error' }); return; }

    // The backend created a session and started reanalysis - load it
    const sessionId = data.session_id;
    state.sessionId     = sessionId;
    state.isViewingPast = false;
    state.isReanalyzing = true;
    history.pushState({}, '', '/session?id=' + sessionId);

    // Clear display for incoming transcript
    clearAll();
    state.sessionId = sessionId;
    _loadChatContextFoldersForSession(sessionId);

    const transcriptEl = document.getElementById('transcript');
    if (transcriptEl) transcriptEl.innerHTML = '';

    document.getElementById('record-btn').disabled = true;
    refreshSidebar();
    _syncUploadBtn();
  } catch (e) {
    uiToast({ message: 'Upload failed: ' + e.message, kind: 'error' });
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = ''; }
  }
}

function startEditTitle(e, sessionId, currentTitle) {
  e.stopPropagation();

  // Find the .session-name element for this item
  const item = document.querySelector(`.session-item[data-id="${sessionId}"]`);
  if (!item) return;
  const nameEl = item.querySelector('.session-name');
  if (!nameEl) return;

  // Replace name text with an inline input
  const input = document.createElement('input');
  input.type      = 'text';
  input.className = 'session-name-input';
  input.value     = currentTitle;

  nameEl.replaceWith(input);
  input.focus();
  input.select();

  let committed = false;

  const commit = async () => {
    if (committed) return;
    committed = true;
    const newTitle = input.value.trim();
    // Restore the name element regardless
    const restored = document.createElement('div');
    restored.className = 'session-name';
    restored.textContent = newTitle || currentTitle;
    input.replaceWith(restored);

    if (newTitle && newTitle !== currentTitle) {
      await fetch(`/api/sessions/${sessionId}`, {
        method:  'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ title: newTitle }),
      });
      // Update active session display if needed, then re-render
      refreshSidebar();
    }
  };

  const cancel = () => {
    if (committed) return;
    committed = true;
    const restored = document.createElement('div');
    restored.className = 'session-name';
    restored.textContent = currentTitle;
    input.replaceWith(restored);
  };

  input.addEventListener('blur',   commit);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); input.blur(); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
}

/* ── SSE connection ──────────────────────────────────────────────────────── */
function connectSSE(afterSegId = 0) {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }

  const url = `/api/events?after_seg_id=${afterSegId}`;
  const src = new EventSource(url);
  _sseSource = src;

  src.addEventListener('status', e => onStatus(JSON.parse(e.data)));

  // Loud, persistent banner when the desktop/call audio is not being captured
  // (dead loopback). This must never pass unnoticed again (2026-09-01).
  src.addEventListener('capture_alert', e => { try { _showCaptureAlert(JSON.parse(e.data)); } catch (_) {} });

  src.addEventListener('transcript', e => {
    const d = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    if (d.seg_id) _lastLiveSegId = Math.max(_lastLiveSegId, d.seg_id);
    if (!state.isViewingPast || state.isReanalyzing) {
      // source_override arrives when a manual reassignment is sticking to
      // this diarizer key (see source_redirect); render as the target speaker.
      appendTranscript(d.text, d.source_override || d.source || 'loopback',
                       d.start_time, d.end_time, d.seg_id, null,
                       d.source_override ? d.source : null);
    }
  });

  src.addEventListener('transcript_update', e => {
    const d = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    if ((!state.isViewingPast || state.isReanalyzing) && d.seg_id) {
      const seg = document.querySelector(`.transcript-segment[data-seg-id="${d.seg_id}"]`);
      if (seg) {
        // Source changed (e.g. noise reclaimed as real speaker) - full re-render
        if (d.source && d.source !== seg.dataset.transcriptSource) {
          seg.dataset.transcriptSource = d.source;
          seg.classList.remove('noise-segment');
          seg.innerHTML = '';
          // Re-render badge and text using the appendTranscript path
          const source = d.source;
          if (source in SOURCE_META) {
            const { label, cls } = SOURCE_META[source];
            seg.innerHTML = `<span class="src-badge ${cls}">${label}</span>${escapeHtml(d.text)}`;
          } else if (source === _NOISE_LABEL) {
            seg.classList.add('noise-segment');
            seg.style.setProperty('--seg-color', _NOISE_COLOR);
            const badge = document.createElement('span');
            badge.className = 'src-badge src-speaker src-noise';
            badge.dataset.speakerKey = source;
            badge.dataset.segId = d.seg_id;
            badge.textContent = 'Noise';
            badge.style.backgroundColor = _NOISE_COLOR + '20';
            badge.style.color = _NOISE_COLOR;
            badge.style.borderColor = _NOISE_COLOR + '40';
            badge.title = 'Click to reassign';
            badge.addEventListener('click', e => {
              if (e.ctrlKey || e.metaKey || e.shiftKey) {
                e.preventDefault(); e.stopPropagation();
                _toggleTranscriptSegSelection(seg, { range: e.shiftKey });
                return;
              }
              _editNoiseBadge(badge, seg);
            });
            seg.appendChild(badge);
            seg.appendChild(document.createTextNode(d.text));
          } else {
            _ensureSpeakerProfile(source);
            const color = speakerColor(source);
            seg.style.setProperty('--seg-color', color);
            const badge = document.createElement('span');
            badge.className = 'src-badge src-speaker';
            if (_sessionLinks[source]) badge.classList.add('speaker-linked');
            badge.dataset.speakerKey = source;
            badge.dataset.segId = d.seg_id;
            badge.title = 'Click to rename';
            badge.textContent = _speakerDisplayName(source) || source;
            badge.style.backgroundColor = color + '26';
            badge.style.color = color;
            badge.style.borderColor = color + '60';
            badge.addEventListener('click', ev => {
              if (ev.ctrlKey || ev.metaKey || ev.shiftKey) {
                ev.preventDefault(); ev.stopPropagation();
                _toggleTranscriptSegSelection(seg, { range: ev.shiftKey });
                return;
              }
              editSpeakerLabel(badge, source);
            });
            seg.appendChild(badge);
            seg.appendChild(document.createTextNode(d.text));
          }
          _applyFilterToSeg(seg);
        } else {
          // Text/time update only - preserve the badge
          const badge = seg.querySelector('.src-badge');
          if (badge) {
            while (badge.nextSibling) badge.nextSibling.remove();
            seg.appendChild(document.createTextNode(d.text));
          } else {
            const badgeHtml = seg.querySelector('.src-badge')?.outerHTML || '';
            seg.innerHTML = badgeHtml + escapeHtml(d.text);
          }
        }
        if (d.end_time) seg.dataset.end = d.end_time;
        if (_autoScroll && !_pickerOpen) {
          _programmaticScrollCount++;
          const el = document.getElementById('transcript');
          el.scrollTop = el.scrollHeight;
          setTimeout(() => { _programmaticScrollCount = Math.max(0, _programmaticScrollCount - 1); }, 100);
        }
      }
    }
  });

  src.addEventListener('replay', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    // Apply speaker profiles first so badges render with the right names/colors
    (d.speaker_profiles || []).forEach(p => applySpeakerProfileUpdate(p));
    // Append only segments we don't already have (deduplicates on brief reconnects)
    (d.segments || []).forEach(s => {
      if (s.id && s.id <= _lastLiveSegId) return;
      appendTranscript(s.text, s.source_override || s.source || 'loopback', s.start_time, s.end_time, s.id, s.label_override, s.source_override ? s.source : null);
      if (s.id) _lastLiveSegId = Math.max(_lastLiveSegId, s.id);
    });
    // Restore summary if we don't already have one rendered
    if (d.summary) {
      const sumEl = document.getElementById('summary');
      if (!sumEl.textContent.trim()) {
        sumEl.innerHTML = renderMd(d.summary);
        highlightCode('#summary');
        linkifyTimestamps(sumEl);
      }
    }
    if (d.chapters) setSessionChapters(d.chapters);
  });

  src.addEventListener('summary_busy', e => {
    const d = JSON.parse(e.data);
    const sid = d.session_id;
    if (sid) {
      // Track busy state per session
      if (!_summaryStreams[sid]) _summaryStreams[sid] = { buffer: '', streaming: false, mode: '' };
      _summaryStreams[sid].mode = d.busy ? (d.mode || 'generating') : '';
      if (!d.busy) _summaryStreams[sid].streaming = false;
    }
    if (sid && sid !== state.sessionId) return;
    const badge = document.getElementById('summary-badge');
    if (d.busy) {
      badge.textContent = d.mode === 'generating' ? 'generating…' : 'updating…';
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  });

  src.addEventListener('chapters_updated', e => {
    const d = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    setSessionChapters(d.chapters || []);
  });

  src.addEventListener('chapters_busy', e => {
    const d = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    _setChaptersBusy(!!d.busy);
  });

  src.addEventListener('summary_start', e => {
    const d = JSON.parse(e.data);
    const sid = d.session_id || state.sessionId;
    _summaryStreams[sid] = { buffer: '', streaming: true, mode: 'generating' };
    if (sid !== state.sessionId) return;
    // Drop any leftover render frame so it can't morph into the cleared element.
    if (_summaryRenderRAF !== null) { cancelAnimationFrame(_summaryRenderRAF); _summaryRenderRAF = null; }
    state.summaryStreaming = true;
    state.summaryBuffer = '';
    const el = document.getElementById('summary');
    el.innerHTML = '';
    state.summaryCursor = el;
  });

  src.addEventListener('summary_chunk', e => {
    const d = JSON.parse(e.data);
    const sid = d.session_id || state.sessionId;
    // Always accumulate into the per-session buffer
    if (_summaryStreams[sid]) _summaryStreams[sid].buffer += d.text;
    // Only update DOM if this is the active session
    if (sid !== state.sessionId) return;
    state.summaryBuffer += d.text;
    if (state.summaryCursor && _summaryRenderRAF === null) {
      _summaryRenderRAF = requestAnimationFrame(_flushSummaryRender);
    }
  });

  src.addEventListener('summary_done', e => {
    const d = JSON.parse(e.data);
    const sid = d.session_id || state.sessionId;
    if (_summaryStreams[sid]) {
      _summaryStreams[sid].streaming = false;
      _summaryStreams[sid].mode = '';
    }
    if (sid !== state.sessionId) return;
    // Flush the last buffered tokens synchronously before the final passes.
    if (_summaryRenderRAF !== null) { cancelAnimationFrame(_summaryRenderRAF); _summaryRenderRAF = null; }
    if (state.summaryCursor) _morphChatBody(state.summaryCursor, state.summaryBuffer);
    state.summaryStreaming = false;
    state.summaryCursor = null;
    highlightCode('#summary');
    linkifyTimestamps(document.getElementById('summary'));
  });

  src.addEventListener('summary_replace', e => {
    // Ignore auto-patch results while a manual stream is in progress
    if (state.summaryStreaming) return;
    const d  = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    const el = document.getElementById('summary');
    el.innerHTML = renderMd(d.content);
    highlightCode('#summary');
    linkifyTimestamps(el);
  });

  src.addEventListener('chat_start', () => {
    state.chatBuffer  = '';
    state.chatToolCalls = [];
    state.chatCursor  = createAssistantBubble();
    // Show "Thinking" indicator until first text chunk arrives
    const wrap = state.chatCursor?.closest('.chat-msg');
    if (wrap) _setAssistantProcessing(wrap, true, 'Thinking');
    scrollChatToBottom(true);
  });

  src.addEventListener('chat_tool_event', e => {
    const d = JSON.parse(e.data);
    if (!state.chatCursor) return;
    const wrap = state.chatCursor.closest('.chat-msg');
    if (!wrap) return;
    if (d.type === 'tool_call') {
      state.chatToolCalls.push({ id: d.id, name: d.name, input: d.input, result: null });
      _renderToolWidget(wrap, state.chatToolCalls);
      _setAssistantProcessing(wrap, true, 'Using ' + _toolDisplayName(d.name) + '…');
    } else if (d.type === 'tool_result') {
      // Match the result to its call by id - required when tools execute in
      // parallel and results return out of order. Fall back to the first
      // still-pending call if no id is present (backward compat).
      let target = null;
      if (d.id != null) {
        target = state.chatToolCalls.find(tc => tc.id === d.id && !tc.result);
      }
      if (!target) {
        target = state.chatToolCalls.find(tc => !tc.result);
      }
      if (target) target.result = {
        success: d.success, summary: d.summary, image: d.image || null,
        // Carries the speaker-relabel plan so the widget can offer Confirm/Cancel.
        relabel: d.relabel_plan || null,
      };
      _renderToolWidget(wrap, state.chatToolCalls);
      _syncRelabelCardFromTool(d);
    }
    scrollChatToBottom();
  });

  src.addEventListener('chat_chunk', e => {
    state.chatBuffer += JSON.parse(e.data).text;
    if (state.chatCursor) {
      const wrap = state.chatCursor.closest('.chat-msg');
      if (wrap) {
        _setAssistantProcessing(wrap, false);
        // On first chunk: collapse the auto-expanded tool widget
        const tw = wrap.querySelector('.chat-tool-widget.streaming');
        if (tw) tw.classList.remove('open', 'streaming');
        // Reveal body and actions on first content
        state.chatCursor.style.display = '';
        const actions = wrap.querySelector('.chat-msg-actions');
        if (actions) actions.style.display = '';
      }
      _chunkArrived();
      // Coalesce the markdown re-parse + morphdom diff + scroll into one frame
      // regardless of token rate (was running the full O(n) parse per token).
      if (!_pendingChatRaf) {
        _pendingChatRaf = requestAnimationFrame(() => {
          _pendingChatRaf = 0;
          if (state.chatCursor) {
            _morphChatBody(state.chatCursor, state.chatBuffer);
            _ensureTypingCursor(state.chatCursor);
            scrollChatToBottom();
          }
        });
      }
    }
  });

  src.addEventListener('chat_done', () => {
    if (state.chatCursor) {
      // Flush any pending coalesced render so the final tokens are never dropped.
      if (_pendingChatRaf) {
        cancelAnimationFrame(_pendingChatRaf);
        _pendingChatRaf = 0;
        _morphChatBody(state.chatCursor, state.chatBuffer);
      }
      const wrap = state.chatCursor.closest('.chat-msg');
      if (wrap) _setAssistantProcessing(wrap, false);
      linkifyTimestamps(state.chatCursor);
      highlightCode('#chat-messages');
      _addCodeCopyButtons(state.chatCursor);
      _removeTypingCursor();
      state.chatCursor = null;
    }
    state.chatToolCalls = [];
    state.aiChatBusy = false;
    _setChatBusy(false);
  });

  src.addEventListener('audio_level', e => {
    const d = JSON.parse(e.data);
    vizLbTarget  = d.loopback || 0;
    vizMicTarget = d.mic      || 0;
    vizHasMic    = !!d.has_mic;
    if (d.lb_spectrum)  vizLbSpec  = d.lb_spectrum;
    if (d.mic_spectrum) vizMicSpec = d.mic_spectrum;
    // Fresh levels arrived - wake the (possibly parked) visualizer loops.
    _startVizLoop();
    _startBrandVizLoop();
    // Sync gain sliders if server reports different values (e.g. after reconnect)
    if (d.lb_gain  != null) _syncGainSlider('lb',  d.lb_gain);
    if (d.mic_gain != null) _syncGainSlider('mic', d.mic_gain);
    updateLevelMeters(vizLbTarget, vizMicTarget, vizHasMic);
    _updateAgcDebug(d.agc);
  });

  src.addEventListener('audio_test_status', e => {
    const d = JSON.parse(e.data);
    state.isTesting = !!d.testing;
    updateTestBtn();
    // Zero out levels when test ends (and we're not recording)
    if (!d.testing && !state.isRecording) {
      vizLbTarget  = 0;
      vizMicTarget = 0;
      vizLbSpec    = [];
      vizMicSpec   = [];
      updateLevelMeters(0, 0, false);
      _updateAgcDebug(null);
    }
  });

  src.addEventListener('session_title', e => {
    const d = JSON.parse(e.data);
    // Worker finished for this session → drop its in-flight badge
    _retitleInFlight.delete(d.session_id);
    // The event carries the new title, so patch the slice rather than refetch.
    let known = false;
    AppData.patch('sessions', list => {
      const entry = (list || []).find(s => s.id === d.session_id);
      if (entry) { entry.title = d.title; known = true; }
      return list;
    });
    if (!known) AppData.invalidate(['sessions'], 'session_title');
    AppData.invalidate(['analytics'], 'session_title');
    if (d.session_id === state.sessionId) updateTopbarSessionTitle();
  });

  // Folder-mode retitle: server resolves the IDs, then announces them up-front
  // so the sidebar can show per-row spinners during the parallel batch.
  src.addEventListener('retitle_start', e => {
    const d = JSON.parse(e.data);
    (d.session_ids || []).forEach(id => _retitleInFlight.add(id));
    _renderSidebar();
  });
  src.addEventListener('retitle_done', e => {
    // Defensive sweep - clear anything still flagged so a stuck row can't
    // spin forever if a worker crashed before emitting session_title.
    if (_retitleInFlight.size) {
      _retitleInFlight.clear();
      _renderSidebar();
    }
    AppData.invalidate(['analytics'], 'retitle_done');
  });

  src.addEventListener('speaker_label', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) applySpeakerProfileUpdate(d);
    // Update sidebar speaker icons
    const entry = _sidebarAllSessions.find(s => s.id === d.session_id);
    if (entry && d.name && !/^Speaker \d+$/i.test(d.name)) {
      if (!entry.speakers) entry.speakers = [];
      const existing = entry.speakers.find(sp => sp.name.toLowerCase() === d.name.toLowerCase());
      if (!existing) {
        entry.speakers.push({ name: d.name, color: d.color || null });
        _renderSidebar();
      }
    }
    AppData.invalidate(['sessions', 'attention', 'analytics'], 'speaker_label');
  });

  src.addEventListener('attention_changed', () => {
    AppData.invalidate(['sessions', 'attention', 'analytics'], 'attention_changed');
  });

  // A calendar refresh rewrites matches and expected counts across the whole
  // library, so the sidebar badges and the attention count both go stale.
  src.addEventListener('calendar_refresh_done', e => {
    let d = {};
    try { d = JSON.parse(e.data); } catch (_) {}
    if (d.matched || d.cleared || d.updated) {
      AppData.invalidate(['calendarStatus', 'calendarEvents', 'sessions', 'attention'],
                         'calendar_refresh_done');
    } else {
      AppData.invalidate(['calendarStatus'], 'calendar_refresh_done');
    }
    const panel = document.getElementById('section-calendar');
    if (panel && panel.classList.contains('active')) loadCalendarStatus();
  });

  src.addEventListener('calendar_match_changed', () => {
    // A confirmation or a clear moves the expected count, which is what the
    // attention badge and the sidebar warning triangles are computed from.
    AppData.invalidate(['calendarStatus', 'calendarEvents', 'sessions', 'attention'],
                       'calendar_match_changed');
  });

  src.addEventListener('smart_cleanup_done', e => {
    let d = {};
    try { d = JSON.parse(e.data); } catch (_) {}
    AppData.invalidate(['sessions', 'attention', 'analytics'], 'smart_cleanup_done');
    if (d.ok === false) {
      uiToast({ message: d.error || 'Smart cleanup failed.', kind: 'error' });
      return;
    }
    const found = d.attention && d.attention.found;
    const unresolved = d.attention && d.attention.unresolved;
    let message = 'Smart cleanup finished.';
    if (typeof found === 'number') {
      message = `Smart cleanup finished: ${found} speaker${found === 1 ? '' : 's'}`;
      message += unresolved
        ? `, ${unresolved} still unnamed.`
        : ', all named.';
    }
    uiToast({ message, kind: unresolved ? 'warn' : 'success' });
  });

  src.addEventListener('fingerprint_match', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) _fpEnqueueToast(d);
  });

  src.addEventListener('source_redirect', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    // The user just adjudicated this key; drop any pending suggestion for it.
    if (d.action === 'set') _fpRemoveFromQueue(d.source);
    _flashLiveRedirect(d);
  });

  src.addEventListener('fingerprint_auto_applied', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) {
      console.info(`[fingerprint] Auto-applied "${d.name}" → ${d.speaker_key} (${d.similarity})`);
      _fpFlashAutoApply(d.speaker_key, d.name);
      // Remove from notification queue if it was pending
      _fpRemoveFromQueue(d.speaker_key);
    }
    _fpLoaded = false;  // the voice library changed; the Speakers view must refetch on its next visit
    AppData.invalidate(['sessions', 'attention', 'analytics'], 'fingerprint_auto_applied');
  });

  src.addEventListener('speaker_linked', e => {
    const d = JSON.parse(e.data);
    _fpLoaded = false;  // linking can seed a new profile; the Speakers view must refetch
    if (d.session_id === state.sessionId) {
      _sessionLinks[d.speaker_key] = { global_id: d.global_id, name: d.name };
      _updateLinkedBadges();
      // Clean up notification queue - this speaker is now identified
      _fpRemoveFromQueue(d.speaker_key);
      _fpUpdateInlineIcons();
    }
    AppData.invalidate(['sessions', 'attention', 'analytics'], 'speaker_linked');
  });

  src.addEventListener('transcript_reset', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    _clearSegmentRegistry();
    // Drop stale speaker state - reanalysis re-derives speakers from scratch,
    // and old pills/profiles would otherwise linger with count=0 until refresh.
    _speakerLabels = {};
    _speakerProfiles = {};
    _selectedSpeakerKeys = [];
    _speakerSelectionAnchor = null;
    Object.keys(_speakerColors).forEach(k => delete _speakerColors[k]);
    _speakerColorIdx = 0;
    _manualNoiseKeys = new Set();
    _tnRefreshSpeakerPills();
    // Clear notification queue on transcript reset (reanalysis)
    _fpNotifQueue = [];
    _fpRejected = new Set();
    _fpToastActive = null;
    if (_fpToastTimer) { clearTimeout(_fpToastTimer); _fpToastTimer = null; }
    _fpUpdateBell();
    _fpRenderNotifPanel();
    document.getElementById('transcript').innerHTML =
      '<p class="empty-hint">Reanalyzing audio…</p>';
    // Keep summary and chat intact - only the transcript is retranscribed
    // Keep playback active - the WAV file still exists during reanalysis
  });

  src.addEventListener('reanalysis_start', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    state.isReanalyzing = true;
    state.isViewingPast = false;  // Allow live transcript updates during reanalysis
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className    = 'status-dot recording';
    text.textContent = 'Reanalyzing…';
    // Ensure playback is available during reanalysis
    if (!_playbackActive && state.sessionId) initPlayback(state.sessionId);
    _syncRecordBtnDisabled();
    _syncUploadBtn();
    AppData.invalidate(['sessions'], 'reanalysis_start');
  });

  src.addEventListener('reanalysis_progress', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    const pct = Math.round((d.progress || 0) * 100);
    const text = document.getElementById('status-text');
    if (text) text.textContent = `Reanalyzing… ${pct}%`;
  });

  src.addEventListener('reanalysis_done', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    state.isReanalyzing   = false;
    state.isViewingPast   = true;  // Back to viewing past session
    state.sessionHasAudio = true;
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className    = 'status-dot ready';
    text.textContent = state.modelInfo || 'Ready';
    initPlayback(state.sessionId);
    // Check if a screen recording exists and init video playback
    fetch(`/api/sessions/${state.sessionId}`).then(r => r.json()).then(s => {
      if (s.has_video) initVideo(state.sessionId, s.video_offset);
    }).catch(() => {});
    _syncRecordBtnDisabled();
    _syncUploadBtn();
    AppData.invalidate(['sessions', 'attention', 'analytics'], 'reanalysis_done');
  });

  src.addEventListener('reanalysis_error', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    state.isReanalyzing = false;
    state.isViewingPast = true;
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className    = 'status-dot ready';
    text.textContent = state.modelInfo || 'Ready';
    uiToast({ message: 'Reanalysis failed: ' + (d.error || 'unknown error'), kind: 'error' });
    _syncRecordBtnDisabled();
    _syncUploadBtn();
    AppData.invalidate(['sessions'], 'reanalysis_error');
  });

  _bindRecordingCommand(src);

  src.addEventListener('open', () => {
    // Reconnect handshake: reconcile status and the shared reads, so a blip
    // can never leave the shell rendering a world that has moved on.
    if (_sseEverConnected) _reconcileAfterGap('sse_reconnect');
    _sseEverConnected = true;
  });

  src.onerror = () => {
    src.close();
    _sseSource = null;
    // Reconnect after a short delay; pass last known seg_id so we only receive
    // segments that arrived while we were disconnected (handles brief blips and
    // full page-refresh reconnects identically).
    setTimeout(() => connectSSE(_lastLiveSegId), 3000);
  };
}

/* ── Start commands from the server ────────────────────────────────────────── */
// Nonce of the last start command this page acted on. A reconnect replays a
// still-pending command over the SSE handshake, so without this a blip could
// start a second recording.
let _lastRecordingCommandNonce = null;
// Set when this page acked a command and LOST the election. The ?autostart
// handler checks it before starting, so a page opened by the fallback window
// cannot start a second capture alongside the window that won.
let _recordingCommandLost = false;
// Read at parse time, before the ?autostart handler strips the query string. A
// page loaded with ?autostart starts the recording itself, so on that page this
// listener only acks and stays out of the way (no double start).
const _pageLoadedWithAutostart =
  new URLSearchParams(window.location.search).has('autostart');
// Identifies this window in the server log ("Start command acked by ..."), so
// which window took a command is visible after the fact.
const _windowClientId = 'w-' + Math.random().toString(36).slice(2, 8);

/** Act on a "start recording" command pushed by the server.
 *
 *  This is what keeps an auto-detected meeting in the window that is ALREADY
 *  open: the server offers the command here first and only opens a window if
 *  nothing acks it. The start still goes through toggleRecording(), the same
 *  path a click takes. See core/recording_request.py. */
function _bindRecordingCommand(src) {
  src.addEventListener('recording_command', e => {
    let d;
    try { d = JSON.parse(e.data); } catch (_) { return; }
    if (!d || d.action !== 'start' || !d.nonce) return;
    if (d.nonce === _lastRecordingCommandNonce) return;
    _lastRecordingCommandNonce = d.nonce;
    if (state.isRecording) return;
    // Ack first, start second. The ack stops the server escalating to a new
    // window, and it elects a single starter: every open window gets the
    // command, but only the first ack of a nonce is accepted. An explicit
    // {ok:false} means another window has it, so this one stands down. Any
    // other outcome (network error, unparseable body) starts anyway: a
    // duplicate start is rejected by the server, a missed one loses a meeting.
    fetch('/api/recording/ack_command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nonce: d.nonce, client_id: _windowClientId }),
    }).then(r => r.json()).then(res => !(res && res.ok === false)).catch(() => true)
      .then(won => {
        if (!won) {
          // Another window took it. Remember that: on a page loaded with
          // ?autostart this is what stops its own handler from starting a
          // second capture next to the window that won.
          _recordingCommandLost = true;
          return;
        }
        if (_pageLoadedWithAutostart) return;  // the ?autostart handler starts it
        if (state.isRecording) return;
        // startNewRecording() blanks a workspace showing a past meeting before
        // it starts, so the server can never be asked to append the new
        // meeting's audio to an old session. No page load either way.
        _waitForRecordReady().then(() => {
          if (!state.isRecording) startNewRecording();
        });
      });
  });
}

/* ── Branding ────────────────────────────────────────────────────────────── */
// Bumped whenever an icon slot changes so the tab and sidebar refetch it.
let _iconVersion = '';

function _updateBrandIcons(recording) {
  // The tab and the sidebar logo follow the "app_idle" / "app_recording"
  // slots (Settings > Icons), so a recording turns the icon red the way it
  // always has, and both images are the user's own if they replaced them.
  const slot = recording ? 'app_recording' : 'app_idle';
  const favicon = document.getElementById('favicon');
  // The first token comes from the server (the link is rendered with it), so
  // the tab keeps the URL Chrome already fetched; a change in Settings bumps it.
  if (!_iconVersion && favicon && favicon.dataset.version) _iconVersion = favicon.dataset.version;
  const q = _iconVersion ? `?v=${encodeURIComponent(_iconVersion)}` : '';
  const icon = document.getElementById('brand-icon');
  if (icon) icon.src = `/api/icons/${slot}${q}`;
  // The tab gets the ICO: exact 16 and 32 px frames rather than a 256 px PNG
  // the browser scales down, and the same bitmap Chrome hands the taskbar.
  if (favicon) favicon.href = `/api/icons/${slot}.ico${q}`;
}

/* ── Settings: Icons tab ────────────────────────────────────────────────── */
// The last state the server sent: every set, the active one, and its slots.
let _iconState = null;

function _iconPreviewUrl(slot, setId, size, version) {
  return `/api/icons/${slot}?set=${encodeURIComponent(setId)}&size=${size}&v=${encodeURIComponent(version || '')}`;
}

function _iconSetById(id) {
  return _iconState && _iconState.sets ? _iconState.sets.find(s => s.id === id) : null;
}

async function loadIconSettings() {
  const list = document.getElementById('icon-set-list');
  const grid = document.getElementById('icon-settings-grid');
  if (!list || !grid) return;
  let st;
  try {
    const r = await fetch('/api/icons');
    st = await r.json();
    if (!r.ok) throw new Error(st.error || 'Could not load the icon sets.');
  } catch (e) {
    list.innerHTML = `<p class="icon-settings-error">${escapeHtml(e.message || 'Could not load the icon sets.')}</p>`;
    grid.innerHTML = '';
    return;
  }
  _iconState = st;
  _renderIconSets(st);
  _renderIconSlots(st);
  _renderIconSetPicker(st);
}

/** The "start from" picker on the New custom set row lists every set. */
function _renderIconSetPicker(st) {
  const sel = document.getElementById('icon-set-new-base');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = st.sets.map(s =>
    `<option value="${escapeHtml(s.id)}">Start from ${escapeHtml(s.name)}</option>`).join('');
  sel.value = st.sets.some(s => s.id === keep) ? keep : st.active;
}

/** The New custom set row: a copy of the picked set under the typed name. */
async function createIconSet() {
  const nameEl = document.getElementById('icon-set-new-name');
  const baseEl = document.getElementById('icon-set-new-base');
  const name = (nameEl && nameEl.value || '').trim();
  if (!name) {
    uiToast({ message: 'Give the new set a name first.', kind: 'warn' });
    if (nameEl) nameEl.focus();
    return;
  }
  const data = await _iconRequest('/api/icons/sets', {
    method: 'POST', headers: _JSON_HEADERS,
    body: JSON.stringify({ name, base: baseEl && baseEl.value || undefined }),
  }, `${name} is in use. Replace any icon below.`);
  if (data && nameEl) nameEl.value = '';
}

/** One row per set: the app icon, three tray states, and what you can do with it. */
function _renderIconSets(st) {
  const list = document.getElementById('icon-set-list');
  if (!list) return;
  list.innerHTML = st.sets.map(s => {
    const id = escapeHtml(s.id);
    const badges = (s.active ? '<span class="icon-set-badge is-active">In use</span>' : '')
      + `<span class="icon-set-badge">${s.builtin ? 'Built in' : 'Custom'}</span>`;
    const tray = ['tray_ready', 'tray_recording', 'tray_loading']
      .map(slot => `<img src="${_iconPreviewUrl(slot, s.id, 40, s.version)}" alt="" width="20" height="20">`)
      .join('');
    return `
      <div class="icon-set-row${s.active ? ' is-active' : ''}" data-set="${id}">
        <img class="icon-set-preview" src="${_iconPreviewUrl('app_idle', s.id, 96, s.version)}" alt="" width="44" height="44">
        <div class="icon-set-tray" title="Tray: ready, recording, loading">${tray}</div>
        <div class="settings-row-info">
          <div class="settings-row-label">${escapeHtml(s.name)}${badges}</div>
          <div class="settings-row-desc">${escapeHtml(s.desc)}</div>
        </div>
        <div class="icon-set-actions">
          ${s.active ? '' : `<button type="button" class="btn btn-secondary" onclick="activateIconSet('${id}')">Use</button>`}
          ${s.builtin
            ? `<button type="button" class="btn btn-quiet" onclick="copyIconSet('${id}')" title="Make a custom set that starts from this one">Customize…</button>`
            : `<button type="button" class="btn btn-quiet" onclick="copyIconSet('${id}')" title="Make another custom set that starts from this one">Duplicate…</button>
          <button type="button" class="btn btn-quiet" onclick="renameIconSet('${id}')">Rename</button>
          <button type="button" class="btn btn-quiet icon-set-delete" onclick="deleteIconSet('${id}')">Delete</button>`}
        </div>
      </div>`;
  }).join('');
}

/** The active set's slots. Replace and Reset only exist on a custom set. */
function _renderIconSlots(st) {
  const grid = document.getElementById('icon-settings-grid');
  const title = document.getElementById('icon-slots-title');
  const intro = document.getElementById('icon-slots-intro');
  if (!grid) return;
  const active = _iconSetById(st.active) || { name: st.active };
  if (title) title.textContent = `Icons in ${active.name}`;
  if (intro) {
    intro.textContent = st.editable
      ? 'Replace any icon with your own image. PNG or ICO works best, square images look right in the tray, and anything large is scaled down.'
      : 'Built-in sets cannot be changed. Click Customize on a set, or create a custom set above, and every icon in it can be replaced.';
  }
  const groups = [];
  for (const s of st.slots) {
    let g = groups.find(x => x.name === s.group);
    if (!g) { g = { name: s.group, slots: [] }; groups.push(g); }
    g.slots.push(s);
  }
  grid.innerHTML = groups.map(g => `
    <div class="icon-slot-group">${escapeHtml(g.name)}</div>
    ${g.slots.map(s => `
      <div class="icon-slot-row" data-slot="${s.slot}">
        <img class="icon-slot-preview" src="${_iconPreviewUrl(s.slot, st.active, 72, s.version)}" alt="" width="36" height="36">
        <div class="settings-row-info">
          <div class="settings-row-label">${escapeHtml(s.label)}${s.replaced ? '<span class="icon-slot-badge">replaced</span>' : ''}</div>
          <div class="settings-row-desc">${escapeHtml(s.desc)}</div>
        </div>
        ${st.editable ? `<div class="icon-slot-actions">
          <label class="btn btn-secondary icon-slot-upload" title="Choose an image for this state">
            <i class="fa-solid fa-upload" aria-hidden="true"></i> Replace
            <input type="file" class="visually-hidden-input"
                   accept="image/png,image/jpeg,image/webp,image/gif,image/bmp,image/x-icon,image/vnd.microsoft.icon,.ico"
                   aria-label="Replace the ${escapeHtml(s.label)} icon"
                   onchange="uploadIconSlot('${s.slot}', this)">
          </label>
          <button type="button" class="btn btn-quiet" ${s.replaced ? '' : 'disabled'}
                  onclick="resetIconSlot('${s.slot}')" title="Go back to the image this set started with">Reset</button>
        </div>` : ''}
      </div>`).join('')}`).join('');
}

/** Send a change, keep the new state, and refresh every icon on the page. */
async function _iconRequest(url, opts, okMessage) {
  try {
    const r = await fetch(url, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || 'Something went wrong.');
    _iconState = data;
    _iconsChanged();
    if (okMessage) uiToast({ message: okMessage, kind: 'success' });
    return data;
  } catch (e) {
    uiToast({ message: e.message || 'Something went wrong.', kind: 'error' });
    return null;
  }
}

const _JSON_HEADERS = { 'Content-Type': 'application/json' };

async function activateIconSet(id) {
  const s = _iconSetById(id);
  await _iconRequest(`/api/icons/sets/${encodeURIComponent(id)}/activate`, { method: 'POST' },
                     s ? `Using the ${s.name} icons` : 'Icon set changed');
}

async function copyIconSet(id) {
  const base = _iconSetById(id);
  const name = await uiPrompt({
    title: 'New custom set',
    message: `A custom set that starts as a copy of ${base ? base.name : 'this set'}. You can then replace any icon in it. Name it:`,
    value: base ? `${base.name} (custom)` : 'My icons',
    placeholder: 'Name',
    confirmLabel: 'Create',
  });
  if (name == null || !name.trim()) return;
  await _iconRequest('/api/icons/sets', {
    method: 'POST', headers: _JSON_HEADERS, body: JSON.stringify({ name: name.trim(), base: id }),
  }, 'Set created and in use. Replace any icon below.');
}

async function renameIconSet(id) {
  const s = _iconSetById(id);
  const name = await uiPrompt({
    title: 'Rename icon set', value: s ? s.name : '', placeholder: 'Name', confirmLabel: 'Rename',
  });
  if (name == null || !name.trim()) return;
  await _iconRequest(`/api/icons/sets/${encodeURIComponent(id)}`, {
    method: 'PATCH', headers: _JSON_HEADERS, body: JSON.stringify({ name: name.trim() }),
  });
}

async function deleteIconSet(id) {
  const s = _iconSetById(id);
  const ok = await uiConfirm({
    title: 'Delete icon set',
    message: `Delete ${s ? s.name : 'this set'}? Its images are removed.`
      + (s && s.active ? ' The Meeting Assistant set takes over.' : ''),
    confirmLabel: 'Delete',
    danger: true,
  });
  if (!ok) return;
  await _iconRequest(`/api/icons/sets/${encodeURIComponent(id)}`, { method: 'DELETE' }, 'Icon set deleted');
}

async function uploadIconSlot(slot, input) {
  const file = input.files && input.files[0];
  if (!file) return;
  input.value = '';
  const body = new FormData();
  body.append('file', file);
  await _iconRequest(`/api/icons/${slot}`, { method: 'POST', body }, 'Icon replaced');
}

async function resetIconSlot(slot) {
  await _iconRequest(`/api/icons/${slot}`, { method: 'DELETE' });
}

function _iconsChanged() {
  _iconVersion = String(Date.now());
  _updateBrandIcons(!!state.isRecording);
  loadIconSettings();
}

/* ── Settings: System > Recording reliability ───────────────────────────── */

function _syncReliabilityToggles() {
  const follow = document.getElementById('loopback-follow-toggle');
  if (follow) follow.checked = !!_prefs.loopback_follow_output;
  const watchdog = document.getElementById('freeze-watchdog-toggle');
  if (watchdog) watchdog.checked = !!_prefs.freeze_watchdog_enabled;
  _syncSettingsNavUI();
}

/* ── Status ──────────────────────────────────────────────────────────────── */
function _syncRecordBtnDisabled() {
  const btn = document.getElementById('record-btn');
  if (!btn) return;
  btn.disabled = !state.isRecording && (state.isReanalyzing || !state.recordingReady);
}

/** Returns a promise that resolves once the record button is enabled
 *  (model loaded) AND audio devices have been enumerated. */
function _waitForRecordReady() {
  return (_devicesReady || Promise.resolve()).then(() => {
    if (state.recordingReady) return;
    return new Promise(resolve => {
      const id = setInterval(() => {
        if (state.recordingReady) { clearInterval(id); resolve(); }
      }, 200);
    });
  });
}

let _quietPromptLanding = null;
let _quietPromptShown = false;

async function showQuietStopConfirm(sessionId) {
  if (_quietPromptShown || !sessionId) return;
  _quietPromptShown = true;
  const stop = await uiConfirm({ title: 'Stop recording?', message: 'Things have gone quiet. Stop this recording?', confirmLabel: 'Stop' });
  if (stop) {
    await fetch('/api/recording/stop', { method: 'POST' }).catch(() => {});
  } else {
    await fetch('/api/recording/quiet-prompt/dismiss', { method: 'POST' }).catch(() => {});
  }
}

function onStatus(d) {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  if (d.model_ready !== undefined) state.modelReady = !!d.model_ready;
  if (d.diarizer_ready !== undefined) state.diarizerReady = !!d.diarizer_ready;
  if (d.model_info !== undefined) state.modelInfo = d.model_info || '';
  if (d.recording_ready !== undefined) state.recordingReady = !!d.recording_ready;
  if (d.recording_ready_reason !== undefined) {
    state.recordingReadyReason = d.recording_ready_reason || 'Loading transcription model...';
  }

  // "Me" speaker (microphone = app user). Cache the global id locally so the
  // transcript can show a "(You)" badge ONLY for this instance's own mic
  // segments (keyed on global id, never on the reserved "me" key, so imported
  // foreign mic segments are never mislabeled as the local user).
  if (d.me_speaker !== undefined) {
    window._meSpeakerGlobalId = d.me_speaker ? d.me_speaker.global_id : null;
    window._meSpeakerName     = d.me_speaker ? d.me_speaker.name : null;
  }
  if (d.me_prompt_pending) _maybeShowMeSpeakerPopup();

  if (d.recording !== undefined) {
    const _wasRecording = state.isRecording;
    state.isRecording = d.recording;
    updateRecordBtn();

    if (d.recording && d.session_id) {
      // Migrate any pre-recording prompt saved under the 'new' key (new sessions only)
      if (!d.resumed) {
        const pendingPrompt = localStorage.getItem('summary-prompt:new');
        if (pendingPrompt !== null) {
          localStorage.setItem('summary-prompt:' + d.session_id, pendingPrompt);
          localStorage.removeItem('summary-prompt:new');
        }
      }
      // Bind the Notes editor to the new session.  For a brand-new recording
      // there are no saved notes yet; for a resumed session we fetch them so
      // the user can keep adding to what they had before.
      if (_notesSessionBound !== d.session_id) {
        if (d.resumed) {
          fetch(`/api/sessions/${d.session_id}/notes`)
            .then(r => r.ok ? r.json() : null)
            .then(p => _notesApplyForSession(d.session_id, p && p.delta ? p : null))
            .catch(() => _notesApplyForSession(d.session_id, null));
        } else {
          _notesApplyForSession(d.session_id, null);
        }
      }
      // Only the workspace shows a session, so the URL is rewritten only when
      // the workspace is the active view. Recording from Home stays on Home.
      if (Views.current === 'session') {
        history.replaceState({ view: 'session', url: '/session?id=' + d.session_id },
                             '', '/session?id=' + d.session_id);
      }
      state.sessionId     = d.session_id;
      state.isViewingPast = false;
      _loadChatContextFoldersForSession(d.session_id);
      dot.className       = 'status-dot recording';
      text.textContent    = 'Recording';
      _loadPaneVisible(d.session_id);
      refreshSessionChatPromptBadge();
      destroyPlayback();
      if (!_durationInterval) {
        startDurationCounter();
        // Push stored gain values now - AudioCapture is guaranteed to exist
        initGainSliders();
      }
      _updateBrandIcons(true);
      if (d.screen_recording) { _updateScreenRecordingStatus(true); _showScreenPreviewToggle(true); }
      if (_pendingSpeakerProfiles.length) _flushPendingSpeakers(d.session_id);
      // During a live recording the reserved "me" key is THIS instance's own
      // mic audio, so link it locally for the "(You)" badge. (When viewing a
      // past/imported session, links come from the server instead, so foreign
      // mic segments stay unbadged.)
      if (d.me_speaker) {
        _sessionLinks['me'] = { global_id: d.me_speaker.global_id, name: d.me_speaker.name };
      }
      AppData.invalidate(['sessions'], 'recording_start');
      _syncCaptureStrip();
      if (_quietPromptLanding === d.session_id) {
        setTimeout(() => showQuietStopConfirm(d.session_id), 150);
        _quietPromptLanding = null;
      }
    } else if (!d.recording) {
      // Smoothly fade the audio visualizers out rather than freezing on the last
      // frame: no more audio_level events will arrive to drive them down, so zero
      // the targets and re-kick the loops, which decay the bars/meters to zero and
      // then park.
      vizLbTarget = 0;
      vizMicTarget = 0;
      vizLbSpec    = [];
      vizMicSpec   = [];
      updateLevelMeters(0, 0, false);
      _startVizLoop();
      _startBrandVizLoop();
      stopDurationCounter();
      _updateBrandIcons(false);
      _updateScreenRecordingStatus(false);
      _stopScreenPreview();
      // Transition to "viewing past" so Resume Session button appears.
      // Don't flip during reanalysis - the transcript SSE listener uses
      // isViewingPast to decide whether to live-append incoming segments.
      if (state.sessionId && !state.isReanalyzing) state.isViewingPast = true;
      updateRecordBtn();
      // A stop finalises in place, from whichever view the user is on.
      AppData.invalidate(['sessions', 'analytics', 'attention'], 'recording_stop');
      if (_wasRecording && state.sessionId) _announceRecordingSaved(state.sessionId);
      // The WAV is finalized before this event fires, so playback is available
      // immediately - no need to reload the page or click the session.
      if (state.isViewingPast && state.sessionId) {
        initPlayback(state.sessionId);
        // Check if a screen recording was saved for this session
        fetch(`/api/sessions/${state.sessionId}`).then(r => r.json()).then(s => {
          if (s.has_video) initVideo(state.sessionId, s.video_offset);
        }).catch(() => {});
      }
      // On the recording→stopped edge, surface speaker resolution while the
      // meeting is fresh: auto-open the Resolve Speakers panel when generic
      // speakers remain (was manual-only before 2026-09-01). Claude-fed
      // candidates, if present, populate the hints; otherwise it shows the
      // dropdowns for manual assignment.
      if (_wasRecording && state.sessionId) _maybeAutoOpenResolution(state.sessionId);
      if (_wasRecording) _clearCaptureAlert();  // recording ended; drop any capture warning
    }
  }

  if (!state.isRecording && dot && text) {
    const row = dot.parentElement;
    if (state.isReanalyzing) {
      dot.className = 'status-dot recording';
      text.textContent = 'Reanalyzing a meeting';
      row.removeAttribute('title');
    } else if (!state.recordingReady) {
      dot.className = 'status-dot loading';
      const why = state.recordingReadyReason || 'loading model';
      const msg = `Preparing recorder · ${why}`;
      text.textContent = msg;
      row.setAttribute('title', msg);
    } else {
      dot.className = 'status-dot ready';
      text.textContent = state.modelInfo ? `Ready · ${state.modelInfo}` : 'Ready';
      row.removeAttribute('title');
    }
  }

  _syncRecordBtnDisabled();
}

/** Stop from any view: the view stays, and the toast is the way in. */
function _announceRecordingSaved(sessionId) {
  const onSession = Views.current === 'session' && state.sessionId === sessionId;
  uiToast({
    id: 'recording-saved',
    kind: 'success',
    message: 'Recording saved.',
    action: onSession ? null : { label: 'Open recording', onClick: () => loadSession(sessionId) },
  });
}

// Auto-open the Cleanup tab once, on the meeting-end edge, when the just-finished
// session still has generic "Speaker N" labels. Voice-library auto-apply may
// already have named everyone (then this is a no-op). Cleanup is where fragments
// get merged and named, with the calendar invite's attendees on offer.
let _autoResolvedSession = null;
async function _maybeAutoOpenResolution(sessionId) {
  if (!sessionId || _autoResolvedSession === sessionId) return;
  _autoResolvedSession = sessionId;
  try {
    const r = await fetch(`/api/agent/v1/meetings/${encodeURIComponent(sessionId)}/speakers`);
    if (!r.ok) return;
    const payload = await r.json();
    const speakers = Array.isArray(payload) ? payload : (payload.speakers || []);
    // A diarizer placeholder ("Speaker 3"), an import stand-in ("Other
    // participant"), or an explicit unknown all still need a real identity.
    const needsResolution = /^(speaker\s*\d+|other participant(\s*\d+)?|unknown|unidentified|guest|participant\s*\d+)$/i;
    const hasGeneric = speakers.some(s => needsResolution.test(String(s.name || '').trim()));
    if (!hasGeneric) return;   // everyone already labeled; nothing to do
    openSpeakerManager('cleanup');
  } catch (_) { /* non-fatal: the dialog remains reachable manually */ }
}

/** The workspace's header title and its date-and-duration subtitle. A blank
 *  workspace is titled "Meeting Assistant", which is what its document title
 *  says too. */
function updateTopbarSessionTitle() {
  if (!state.sessionId) {
    Views.setTitle('session', 'Meeting Assistant', '');
    return;
  }
  const entry = _sidebarAllSessions.find(s => s.id === state.sessionId);
  const title = (entry && entry.title) || 'Untitled recording';
  const parts = [];
  if (entry && entry.started_at) {
    parts.push(new Date(entry.started_at + 'Z').toLocaleDateString(
      undefined, { month: 'long', day: 'numeric', year: 'numeric' }));
  }
  if (entry) {
    const secs = _sessionDurationSec ? _sessionDurationSec(entry) : 0;
    if (secs > 0) parts.push(fmtDuration(secs));
  }
  if (state.isRecording) parts.push('Recording');
  Views.setTitle('session', title, parts.join(' · '));
}

function updateRecordBtn() {
  const btn = document.getElementById('record-btn');
  if (!btn) return;
  // Clear any inline "Stopping…" overrides
  btn.style.background = '';
  btn.style.color = '';
  btn.disabled = false;
  updateTopbarSessionTitle();
  // Three states, and the button never lies about which one it is in.
  // "Preparing recorder" is a real disabled button; the reason lives in the
  // capture setup row, and there is no invented time estimate.
  const preparing = !state.isRecording && (state.isReanalyzing || !state.recordingReady);
  if (state.isRecording) {
    const elapsed = _recordingStartTime ? fmtDuration((Date.now() - _recordingStartTime) / 1000) : '0:00';
    btn.innerHTML = '<span class="record-pulse" aria-hidden="true"></span> Stop · '
      + `<span class="record-elapsed" id="record-elapsed">${elapsed}</span>`;
    btn.classList.add('recording');
  } else if (preparing) {
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-hourglass-half"></i></span> Preparing recorder';
    btn.classList.remove('recording');
  } else {
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-play"></i></span> Record';
    btn.classList.remove('recording');
  }
  // The chevron only offers "Resume this recording" while a past recording is
  // on screen, and it says what resuming does.
  const resumeItem = document.getElementById('record-menu-resume');
  if (resumeItem) {
    resumeItem.classList.toggle(
      'hidden', state.isRecording || !state.isViewingPast || !state.sessionId);
  }
  _syncCaptureStrip();
  // Disable device/model selectors while recording
  const lbSel  = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  const wSel   = document.getElementById('whisper-preset-sel');
  const dSel   = document.getElementById('diarizer-device-sel');
  if (lbSel)  lbSel.disabled  = state.isRecording;
  if (micSel) micSel.disabled = state.isRecording;
  if (wSel)   wSel.disabled   = state.isRecording;
  if (dSel)   dSel.disabled   = state.isRecording;
  // Disable screen recording toggle during recording
  const scrToggle = document.getElementById('screen-record-toggle');
  if (scrToggle) scrToggle.disabled = state.isRecording;
  _syncRecordBtnDisabled();
  updateTestBtn();
  _syncUploadBtn();
}

function updateTestBtn() {
  const btn = document.getElementById('viz-test-btn');
  if (!btn) return;
  btn.disabled = state.isRecording;
  if (state.isTesting) {
    btn.innerHTML = '<i class="fa-solid fa-stop"></i> Stop Test';
    btn.classList.add('testing');
  } else {
    btn.innerHTML = '<i class="fa-solid fa-play"></i> Test Audio';
    btn.classList.remove('testing');
  }
}

/* ── Recording ───────────────────────────────────────────────────────────── */
// Reconcile the record button when a Stop can't be confirmed - called when the
// stop request is rejected (backend gone) or when the SSE stop-confirmation has
// not arrived within the grace period. Prevents the "Stopping…" spinner from
// hanging forever against a frozen/dead backend (2026-09-01 freeze incident).
async function _handleStopUnresponsive() {
  if (!state.isRecording) return;   // already reconciled by a real status event
  try {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 5000);
    const r = await fetch('/api/status', { cache: 'no-store', signal: ac.signal });
    clearTimeout(t);
    if (r.ok) {
      const d = await r.json();
      onStatus(d);                  // if it truly stopped, this clears the button
      if (!d.recording) return;
    }
  } catch (_) { /* backend not answering; fall through to a forced reset */ }
  state.isRecording = false;
  const btn = document.getElementById('record-btn');
  if (btn) { btn.disabled = false; btn.style.background = ''; btn.style.color = ''; }
  updateRecordBtn();
  flashStatus('App not responding. The recording may not have stopped; check the Meeting Assistant window.');
}

/** opts.start skips the "Record starts a new recording" guard (the callers
 *  that own it, startNewRecording and resumeRecording, have already decided).
 *  opts.resume appends to the recording currently on screen. */
async function toggleRecording(opts) {
  const o = opts || {};
  // Drop keyboard focus from the record button. Browsers activate the focused
  // <button> when Space is pressed, so without this a spacebar tap after a
  // recording starts would "click" the still-focused button and stop it.
  document.getElementById('record-btn')?.blur();
  if (!state.isRecording && !o.start) {
    // A bare Record press always starts a NEW recording, from any view.
    return startNewRecording();
  }
  if (state.isRecording) {
    // Immediate visual feedback while the server tears down streams
    const btn = document.getElementById('record-btn');
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-spinner fa-spin"></i></span> Stopping\u2026';
    btn.style.background = 'var(--yellow)';
    btn.style.color = 'var(--on-accent)';
    btn.disabled = true;
    // Safety net (2026-09-01 freeze): the button only clears when the SSE
    // 'status' event with recording=false arrives. If the backend is frozen or
    // dead that confirmation never comes and the spinner hangs on "Stopping…"
    // forever. Reconcile against /api/status after a grace period, and reset the
    // UI if the stop can't be confirmed, instead of spinning indefinitely.
    fetch('/api/recording/stop', { method: 'POST' }).catch(() => _handleStopUnresponsive());
    setTimeout(() => { if (state.isRecording) _handleStopUnresponsive(); }, 12000);
  } else {
    // Read selected device indices from the dropdowns
    const lbSel  = document.getElementById('viz-loopback-sel');
    const lbVal  = lbSel?.value ?? '';
    const micVal = document.getElementById('viz-mic-sel')?.value ?? '';
    const body = {};
    if (lbVal  !== '' && lbVal  !== null && lbVal  !== undefined) {
      body.loopback_device = parseInt(lbVal, 10);
      // Send the device name too so the backend can re-find the same physical
      // device if PyAudio has renumbered the index since it was saved.
      const lbName = lbSel?.selectedOptions?.[0]?.textContent;
      if (lbName) body.loopback_device_name = lbName;
    }
    Object.assign(body, parseMicSelection(micVal));

    if (o.resume && state.isViewingPast && state.sessionId) {
      // Append to the recording on screen. Only resumeRecording() asks for it.
      body.resume_session_id = state.sessionId;
    }

    const resp = await fetch('/api/recording/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      // A second window that lost the start election can still race the winner
      // to the server; the reservation rejects it, and that is not an error the
      // user needs to see.
      if (err.error === 'Already starting' || err.error === 'Already recording') return;
      uiToast({ message: err.error || 'Failed to start recording', kind: 'error' });
    }
  }
}

/* ── Transcript ──────────────────────────────────────────────────────────── */
const SOURCE_META = {
  loopback: { label: 'Desktop', cls: 'src-loopback' },
  mic:      { label: 'Mic',     cls: 'src-mic'      },
  both:     { label: 'Both',    cls: 'src-both'      },
};

// Auto-scroll state for the transcript panel
let _autoScroll = true;

// Transcript filter state
let _transcriptFilter = { search: '', speakers: new Set(), timeMin: 0, timeMax: Infinity };
let _showNoise = false;       // noise segments hidden by default
let _noiseSolo = false;       // true when noise is the only visible group
let _showOriginalKeys = false; // show original speaker keys instead of display names
let _manualNoiseKeys = new Set(); // speaker_keys manually marked as noise
let _navState = { matches: [], currentIdx: -1 };

// Set while the speaker picker dropdown is open - suppresses auto-scroll
// so the transcript doesn't jump away while the user is typing a name.
let _pickerOpen = false;

// Set during bulk session loading to skip expensive per-segment operations.
// Deferred work (filters, highlights, speaker manager) runs once after the load.
let _bulkLoading = false;
let _loadGeneration = 0;  // increments on each loadSession call to cancel stale renders

// ── Performance: in-memory transcript index ──────────────────────────────────
// Maintained in appendTranscript / _clearSegmentRegistry.  Avoids repeated
// document.querySelectorAll calls in hot paths (playback, filter, highlights).
let _segmentRegistry  = [];     // every .transcript-segment element, in insertion order
let _segmentTimes     = [];     // {start, end, el} for timed segs - sorted by start
let _visibleRangesCache = null; // cached _getVisibleTimeRanges(); null means stale

function _clearSegmentRegistry() {
  _segmentRegistry  = [];
  _segmentTimes     = [];
  _visibleRangesCache = null;
  // Reset collapse state
  _collapseActive = false;
  const colBtn = document.getElementById('transcript-collapse-toggle');
  if (colBtn) { colBtn.classList.add('hidden'); colBtn.classList.remove('active'); }
  _removeCollapse();
  // Reset minimap state
  _minimapActive = false;
  _minimapDataCache = null;
  _minimapDirty = true;
  if (_minimapDebounceTimer) { clearTimeout(_minimapDebounceTimer); _minimapDebounceTimer = 0; }
  const mmBtn = document.getElementById('transcript-minimap-toggle');
  if (mmBtn) { mmBtn.classList.add('hidden'); mmBtn.classList.remove('active'); }
  const mmEl = document.getElementById('transcript-minimap');
  if (mmEl) mmEl.classList.add('hidden');
  if (_minimapPlayheadEl) { _minimapPlayheadEl.style.display = 'none'; }
}

// speaker_key → display name for the session currently in view
let _speakerLabels = {};

// speaker_key → accent color (CSS color string), auto-assigned on first appearance
const _speakerColors = {};
let _speakerProfiles = {};
let _lastLiveSegId   = 0;   // highest seg_id received from live transcript events
var _sseSource       = null;  // var so home.js can access it
let _selectedSpeakerKeys = [];
let _speakerSelectionAnchor = null;
let _speakerDraftName = '';
let _speakerDraftColor = '';

// Transcript segment multi-select (Ctrl/Shift+click on badges)
let _transcriptSelectedSegs = new Set(); // Set of .transcript-segment DOM elements
let _transcriptSelectionAnchor = null;

// Speakers added before a session exists; flushed to the API on session start
let _pendingSpeakerProfiles = [];
const _NOISE_LABEL = '[Noise]';
const _NOISE_COLOR = '#6e7681';   // muted gray

const _SPEAKER_PALETTE = [
  '#58a6ff', // blue
  '#f47067', // red
  '#00b464', // green
  '#d2a8ff', // lavender
  '#f0883e', // orange
  '#db61a2', // pink
  '#e3b341', // yellow
  '#2dd4bf', // teal
  '#a78bfa', // violet
  '#79c0ff', // sky
  '#ef6e4e', // tangerine
  '#86e89d', // mint
  '#f6c177', // peach
  '#6cb6ff', // cornflower
  '#ff9bce', // rose
  '#768390', // slate
];
let _speakerColorIdx = 0;

// Voice library: speaker_key → { global_id, name } for the active session
let _sessionLinks = {};

function _isCustomSpeakerKey(speakerKey) {
  return typeof speakerKey === 'string' && speakerKey.startsWith('custom:');
}

function _speakerDisplayName(speakerKey) {
  return _speakerProfiles[speakerKey]?.name || _speakerLabels[speakerKey] || speakerKey;
}

/** Scan all speaker badges and add/remove the 'speaker-linked' class. */
function _updateLinkedBadges() {
  document.querySelectorAll('.src-badge.src-speaker').forEach(badge => {
    const key = badge.dataset.speakerKey;
    if (!key) return;
    const link = _sessionLinks[key];
    if (link) {
      badge.classList.add('speaker-linked');
      badge.title = `Saved voice profile: ${link.name || key}`;
    } else {
      badge.classList.remove('speaker-linked');
      badge.title = 'Click to rename';
    }
  });
}

function _speakerNameKey(name, excludeKey = '') {
  return Object.keys(_speakerProfiles).find(
    key => key !== excludeKey && _speakerDisplayName(key) === name
  ) || '';
}

/* ── "Me" speaker (microphone = app user) ─────────────────────────────────── */

/** True only for THIS instance's own mic segments. Keyed on the linked global
 *  id matching the local Me id (never on the reserved "me" key) so imported
 *  foreign mic segments are never badged as the local user. */
function _isMeSpeaker(speakerKey) {
  const meId = window._meSpeakerGlobalId;
  if (!meId) return false;
  const link = _sessionLinks[speakerKey];
  return !!(link && link.global_id === meId);
}

let _meSpeakerPopupShown = false;
function _maybeShowMeSpeakerPopup() {
  if (_meSpeakerPopupShown) return;
  if (document.querySelector('.me-speaker-overlay')) return;
  _meSpeakerPopupShown = true;
  _showMeSpeakerPopup();
}

/** Inject the shared "me speaker" dialog styles once. Used by both the
 *  onboarding popup and the export/import name prompt. */
function _ensureMeSpeakerStyles() {
  if (document.getElementById('me-speaker-style')) return;
  const st = document.createElement('style');
  st.id = 'me-speaker-style';
  st.textContent = `
      .me-speaker-overlay{position:fixed;inset:0;z-index:10000;display:flex;align-items:center;
        justify-content:center;background:rgba(0,0,0,.5);opacity:0;transition:opacity .18s ease;}
      .me-speaker-overlay.visible{opacity:1;}
      .me-speaker-dialog{position:relative;width:min(480px,92vw);background:var(--bg-elevated,#1c2128);
        color:var(--text,#e6edf3);border:1px solid var(--border,#30363d);border-radius:14px;
        padding:22px 22px 18px;box-shadow:0 18px 60px rgba(0,0,0,.5);
        transform:scale(.97);transition:transform .18s ease;}
      .me-speaker-overlay.visible .me-speaker-dialog{transform:scale(1);}
      .me-speaker-x{position:absolute;top:12px;right:12px;background:none;border:none;color:var(--text-muted,#8b949e);
        font-size:18px;cursor:pointer;padding:4px;border-radius:6px;}
      .me-speaker-x:hover{background:var(--bg-subtle,#262c36);color:var(--text,#e6edf3);}
      .me-speaker-head{display:flex;gap:12px;align-items:center;margin-bottom:10px;}
      .me-speaker-head i{font-size:30px;color:var(--accent,#58a6ff);}
      .me-speaker-eyebrow{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--text-muted,#8b949e);}
      .me-speaker-title{font-size:19px;font-weight:650;}
      .me-speaker-sub{font-size:13px;line-height:1.5;color:var(--text-muted,#8b949e);margin:4px 0 16px;}
      .me-speaker-label{font-size:12px;font-weight:600;color:var(--text-muted,#8b949e);display:block;margin-bottom:6px;}
      .me-speaker-row{display:flex;gap:8px;margin-bottom:10px;}
      .me-speaker-input{flex:1;min-width:0;background:var(--bg-subtle,#0d1117);color:var(--text,#e6edf3);
        border:1px solid var(--border,#30363d);border-radius:8px;padding:9px 11px;font-size:14px;}
      .me-speaker-primary,.me-speaker-secondary{border:none;border-radius:8px;padding:9px 14px;font-size:13px;
        font-weight:600;cursor:pointer;white-space:nowrap;}
      .me-speaker-primary{background:var(--accent,#2f81f7);color:#fff;}
      .me-speaker-secondary{background:var(--bg-subtle,#262c36);color:var(--text,#e6edf3);border:1px solid var(--border,#30363d);}
      .me-speaker-or{text-align:center;font-size:12px;color:var(--text-muted,#8b949e);margin:6px 0 10px;}
      .me-speaker-warn{font-size:12px;color:var(--warn,#d29922);margin:0 0 8px;}
      .me-speaker-actions{display:flex;justify-content:flex-end;margin-top:6px;}
      .me-speaker-skip{background:none;border:none;color:var(--text-muted,#8b949e);font-size:13px;cursor:pointer;padding:6px 8px;}
      .me-speaker-skip:hover{color:var(--text,#e6edf3);text-decoration:underline;}`;
  document.head.appendChild(st);
}

/** Promise-based name prompt for the microphone ("me") speaker. Resolves to
 *  the entered name (string), '' when the user skips, or null when cancelled
 *  (Escape / close / backdrop). Shared by the export and import flows.
 *  Pass opts.librarySpeakers ([{name, emb_count}], pre-filtered to exclude the
 *  local "Me" profile) to add a "pick a saved speaker" dropdown that fills the
 *  name field. */
function _promptMeName(opts) {
  const o = opts || {};
  return new Promise(resolve => {
    _ensureMeSpeakerStyles();
    document.querySelectorAll('.me-name-overlay').forEach(el => el.remove());
    const overlay = document.createElement('div');
    overlay.className = 'me-speaker-overlay me-name-overlay';
    overlay.setAttribute('role', 'presentation');
    overlay.innerHTML = `
      <div class="me-speaker-dialog" role="dialog" aria-modal="true">
        <button class="me-speaker-x" type="button" aria-label="Close"><i class="fa-solid fa-xmark"></i></button>
        <div class="me-speaker-head">
          <i class="fa-solid fa-circle-user"></i>
          <div>
            <div class="me-speaker-eyebrow">${escapeHtml(o.eyebrow || 'Speaker name')}</div>
            <div class="me-speaker-title">${escapeHtml(o.title || 'Name this speaker')}</div>
          </div>
        </div>
        <p class="me-speaker-sub">${escapeHtml(o.sub || '')}</p>
        <label class="me-speaker-label">Name</label>
        <div class="me-speaker-row">
          <input id="me-name-input" type="text" class="me-speaker-input"
                 placeholder="${escapeHtml(o.placeholder || 'e.g. Alex Rivera')}"
                 value="${escapeHtml(o.value || '')}">
          <button id="me-name-save" class="me-speaker-primary" type="button">${escapeHtml(o.primaryLabel || 'Save name')}</button>
        </div>
        ${(o.librarySpeakers && o.librarySpeakers.length) ? `
        <div class="me-speaker-or">or pick from your saved speakers</div>
        <div class="me-speaker-row">
          <select id="me-name-select" class="me-speaker-input">
            <option value="">Select a saved speaker…</option>
            ${o.librarySpeakers.map(s => `<option value="${escapeHtml(s.name)}">${escapeHtml(s.name)}${s.emb_count ? ` (${s.emb_count} voice samples)` : ''}</option>`).join('')}
          </select>
        </div>` : ''}
        <div class="me-speaker-actions">
          ${o.allowSkip ? `<button id="me-name-skip" class="me-speaker-skip" type="button">${escapeHtml(o.skipLabel || 'Skip')}</button>` : ''}
        </div>
      </div>`;
    document.body.appendChild(overlay);

    let done = false;
    const finish = (val) => {
      if (done) return;
      done = true;
      overlay.classList.remove('visible');
      setTimeout(() => overlay.remove(), 180);
      document.removeEventListener('keydown', onKey);
      resolve(val);
    };
    const input = overlay.querySelector('#me-name-input');
    const save = () => {
      const v = input.value.trim();
      if (!v) { input.focus(); return; }
      finish(v);
    };
    const onKey = e => {
      if (e.key === 'Escape') { e.stopPropagation(); finish(null); }
      else if (e.key === 'Enter' && document.activeElement === input) { e.preventDefault(); save(); }
    };
    overlay.querySelector('#me-name-save').addEventListener('click', save);
    overlay.querySelector('.me-speaker-x').addEventListener('click', () => finish(null));
    overlay.addEventListener('click', e => { if (e.target === overlay) finish(null); });
    const skipBtn = overlay.querySelector('#me-name-skip');
    if (skipBtn) skipBtn.addEventListener('click', () => finish(''));
    // Picking a saved speaker just fills the name field (the recipient can
    // still tweak it); saving writes the name like any typed entry.
    const selEl = overlay.querySelector('#me-name-select');
    if (selEl) selEl.addEventListener('change', () => {
      if (selEl.value) { input.value = selEl.value; input.focus(); }
    });
    document.addEventListener('keydown', onKey);
    requestAnimationFrame(() => { overlay.classList.add('visible'); input.focus(); });
  });
}

/** POST a real name for a session's microphone ("me") speaker. Returns true on
 *  success. The backend renames the linked global profile retroactively for the
 *  user's own recordings, or updates just this session's label for imported
 *  foreign sessions. */
async function _applyMeName(sessionId, name) {
  try {
    const r = await fetch(`/api/sessions/${sessionId}/me-name`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    return r.ok;
  } catch (_) { return false; }
}

/** First-run / change-identity dialog for the microphone "Me" speaker.
 *  Non-blocking overlay modelled on _showWhatsNewPopup. */
async function _showMeSpeakerPopup() {
  document.querySelectorAll('.me-speaker-overlay').forEach(el => el.remove());

  let speakers = [];
  try {
    speakers = await fetch('/api/fingerprint/speakers').then(r => r.json());
  } catch (_) { speakers = []; }
  const meId = window._meSpeakerGlobalId || null;
  const current = meId ? speakers.find(s => s.id === meId) : null;
  const others = speakers.filter(s => s.id !== meId);

  const overlay = document.createElement('div');
  overlay.className = 'me-speaker-overlay';
  overlay.setAttribute('role', 'presentation');
  overlay.innerHTML = `
    <div class="me-speaker-dialog" role="dialog" aria-modal="true" aria-labelledby="me-speaker-title">
      <button class="me-speaker-x" type="button" aria-label="Close"><i class="fa-solid fa-xmark"></i></button>
      <div class="me-speaker-head">
        <i class="fa-solid fa-circle-user"></i>
        <div>
          <div class="me-speaker-eyebrow">Who's on the microphone?</div>
          <div class="me-speaker-title" id="me-speaker-title">Set your speaker</div>
        </div>
      </div>
      <p class="me-speaker-sub">Microphone audio is always attributed to you and is never mixed in
        with the diarized desktop speakers. Pick the speaker that's you, or enter a name.</p>

      <label class="me-speaker-label">Your name</label>
      <div class="me-speaker-row">
        <input id="me-speaker-name" type="text" class="me-speaker-input"
               placeholder="e.g. ${escapeHtml((window._meSpeakerName) || 'Ty')}"
               value="${escapeHtml(current ? current.name : (window._meSpeakerName || ''))}">
        <button id="me-speaker-save-name" class="me-speaker-primary" type="button">Use this name</button>
      </div>

      ${others.length ? `
      <div class="me-speaker-or">or pick an existing speaker</div>
      <div class="me-speaker-row">
        <select id="me-speaker-select" class="me-speaker-input">
          <option value="">Select a saved speaker…</option>
          ${others.map(s => `<option value="${escapeHtml(s.id)}" data-emb="${s.emb_count || 0}">${escapeHtml(s.name)}${s.emb_count ? ` (${s.emb_count} voice samples)` : ''}</option>`).join('')}
        </select>
        <button id="me-speaker-use-existing" class="me-speaker-secondary" type="button">That's me</button>
      </div>
      <p class="me-speaker-warn" id="me-speaker-warn" hidden></p>` : ''}

      <div class="me-speaker-actions">
        <button id="me-speaker-skip" class="me-speaker-skip" type="button">Not now</button>
      </div>
    </div>`;

  _ensureMeSpeakerStyles();

  document.body.appendChild(overlay);

  const close = () => {
    overlay.classList.remove('visible');
    setTimeout(() => overlay.remove(), 180);
    document.removeEventListener('keydown', onKey);
  };
  const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); doSkip(); } };

  const post = (body) => fetch('/api/onboarding/me-speaker', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(r => r.json());

  const applied = (resp) => {
    if (resp && resp.me_speaker) {
      window._meSpeakerGlobalId = resp.me_speaker.global_id;
      window._meSpeakerName = resp.me_speaker.name;
      if (typeof flashStatus === 'function') flashStatus(`You: ${resp.me_speaker.name}`);
    }
    close();
  };

  const doSkip = () => {
    fetch('/api/onboarding/skip', { method: 'POST' }).catch(() => {});
    close();
  };

  overlay.querySelector('.me-speaker-x').addEventListener('click', doSkip);
  overlay.querySelector('#me-speaker-skip').addEventListener('click', doSkip);
  overlay.addEventListener('click', e => { if (e.target === overlay) doSkip(); });
  document.addEventListener('keydown', onKey);

  overlay.querySelector('#me-speaker-save-name').addEventListener('click', async () => {
    const name = overlay.querySelector('#me-speaker-name').value.trim();
    if (!name) return;
    // Rename the existing Me profile in place (retroactive everywhere) when one
    // exists; otherwise create/select by name.
    if (meId) {
      try {
        await fetch(`/api/fingerprint/speakers/${meId}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        window._meSpeakerName = name;
        if (typeof flashStatus === 'function') flashStatus(`You: ${name}`);
        close();
      } catch (_) { close(); }
    } else {
      applied(await post({ mode: 'name', name }));
    }
  });

  const selEl = overlay.querySelector('#me-speaker-select');
  const warnEl = overlay.querySelector('#me-speaker-warn');
  if (selEl) {
    selEl.addEventListener('change', () => {
      const opt = selEl.selectedOptions[0];
      const emb = opt ? parseInt(opt.dataset.emb || '0', 10) : 0;
      if (emb > 0) {
        warnEl.hidden = false;
        warnEl.textContent = `Heads up: this clears ${opt.textContent.replace(/\s*\(\d+ voice samples\)$/, '')}'s saved voice samples so the profile is only used for your mic.`;
      } else {
        warnEl.hidden = true;
      }
    });
    overlay.querySelector('#me-speaker-use-existing').addEventListener('click', async () => {
      const gid = selEl.value;
      if (!gid) return;
      applied(await post({ mode: 'existing', global_id: gid }));
    });
  }

  requestAnimationFrame(() => overlay.classList.add('visible'));
}
window.showMeSpeakerPopup = _showMeSpeakerPopup;

function _upsertSpeakerProfile(data) {
  const speakerKey = data.speaker_key || data.speakerKey;
  if (!speakerKey) return null;

  const profile = _speakerProfiles[speakerKey] || {
    speaker_key: speakerKey,
    name: _speakerLabels[speakerKey] || speakerKey,
    color: _speakerColors[speakerKey] || null,
    custom: _isCustomSpeakerKey(speakerKey),
  };

  if (Object.prototype.hasOwnProperty.call(data, 'name') && data.name) {
    profile.name = data.name;
    _speakerLabels[speakerKey] = data.name;
  } else if (!_speakerLabels[speakerKey]) {
    _speakerLabels[speakerKey] = profile.name;
  }

  if (Object.prototype.hasOwnProperty.call(data, 'color') && data.color) {
    profile.color = data.color;
    _speakerColors[speakerKey] = data.color;
  } else if (_speakerColors[speakerKey]) {
    profile.color = _speakerColors[speakerKey];
  }

  _speakerProfiles[speakerKey] = profile;
  return profile;
}

function _ensureSpeakerProfile(speakerKey, data = {}) {
  return _upsertSpeakerProfile({
    speaker_key: speakerKey,
    name: data.name || _speakerDisplayName(speakerKey) || speakerKey,
    color: data.color || _speakerColors[speakerKey] || _speakerProfiles[speakerKey]?.color || null,
  });
}

function _speakerBadgeCount(speakerKey) {
  let count = 0;
  for (const seg of _segmentRegistry) {
    const badge = seg.querySelector('.src-badge.src-speaker');
    if (badge && badge.dataset.speakerKey === speakerKey) count++;
  }
  return count;
}

function speakerColor(speakerKey) {
  if (speakerKey === _NOISE_LABEL) return _NOISE_COLOR;
  if (!_speakerColors[speakerKey]) {
    const myName = _speakerDisplayName(speakerKey);
    if (myName) {
      const siblingKey = Object.keys(_speakerColors).find(
        key => key !== speakerKey && _speakerDisplayName(key) === myName
      );
      if (siblingKey) {
        _speakerColors[speakerKey] = _speakerColors[siblingKey];
        _upsertSpeakerProfile({ speaker_key: speakerKey, color: _speakerColors[siblingKey] });
        return _speakerColors[speakerKey];
      }
    }
    _speakerColors[speakerKey] = _SPEAKER_PALETTE[_speakerColorIdx % _SPEAKER_PALETTE.length];
    _speakerColorIdx++;
    _upsertSpeakerProfile({ speaker_key: speakerKey, color: _speakerColors[speakerKey] });
  }
  return _speakerColors[speakerKey];
}

function _getSortedSpeakerProfiles() {
  const keys = new Set([...Object.keys(_speakerProfiles), ...Object.keys(_speakerLabels)]);
  for (const seg of _segmentRegistry) {
    const badge = seg.querySelector('.src-badge.src-speaker');
    if (badge) keys.add(badge.dataset.speakerKey);
  }

  return [...keys]
    .map(key => _ensureSpeakerProfile(key))
    .sort((a, b) => {
      const countDiff = _speakerBadgeCount(b.speaker_key) - _speakerBadgeCount(a.speaker_key);
      if (countDiff !== 0) return countDiff;
      if (a.custom !== b.custom) return a.custom ? 1 : -1;
      return (a.name || a.speaker_key).localeCompare(b.name || b.speaker_key);
    });
}

function _speakerOptionNames(currentName = '', excludeKey = '') {
  const seen = new Set();
  const names = [];
  _getSortedSpeakerProfiles().forEach(profile => {
    const name = (profile.name || '').trim();
    if (!name || name === currentName || profile.speaker_key === excludeKey) return;
    if (!_isCustomSpeakerKey(profile.speaker_key) && (name === profile.speaker_key || _isDefaultName(name))) {
      return;
    }
    if (!seen.has(name)) {
      seen.add(name);
      names.push(name);
    }
  });
  return names;
}

function _highlightSelectedSpeakerBadges() {
  const selected = new Set(_selectedSpeakerKeys);
  for (const seg of _segmentRegistry) {
    const badge = seg.querySelector('.src-badge.src-speaker');
    if (badge) badge.classList.toggle('speaker-selected', selected.has(badge.dataset.speakerKey));
  }
}

function _syncSpeakerDraftFromSelection() {
  if (_selectedSpeakerKeys.length === 0) {
    _speakerDraftName = '';
    _speakerDraftColor = '';
    return;
  }

  const profiles = _selectedSpeakerKeys.map(key => _ensureSpeakerProfile(key)).filter(Boolean);
  if (!profiles.length) {
    _speakerDraftName = '';
    _speakerDraftColor = '';
    return;
  }

  const firstName = profiles[0].name || '';
  const firstColor = profiles[0].color || speakerColor(profiles[0].speaker_key);
  _speakerDraftName = profiles.every(p => (p.name || '') === firstName) ? firstName : '';
  _speakerDraftColor = profiles.every(p => (p.color || speakerColor(p.speaker_key)) === firstColor) ? firstColor : '';
}

// Group speaker profiles by display name so that diart fragments of the same
// physical person collapse into a single manager row.
function _groupProfilesByName(profiles) {
  const groups = new Map(); // nameKey → group object
  for (const p of profiles) {
    const rawName = (p.name || '').trim();
    const nameKey = rawName.toLowerCase() || ('__key__' + p.speaker_key);
    if (!groups.has(nameKey)) {
      groups.set(nameKey, {
        name:        rawName || p.speaker_key,
        color:       p.color || null,
        speakerKeys: [],
        custom:      p.custom || false,
      });
    }
    const g = groups.get(nameKey);
    g.speakerKeys.push(p.speaker_key);
    if (!g.color && p.color) g.color = p.color;
    if (p.custom) g.custom = true;
  }
  return [...groups.values()];
}

// Noise is a PER-KEY property: a key is noise if it's the reserved noise label
// or the user flagged it (_manualNoiseKeys). It must never be decided at the
// group level. A named speaker whose name-group happens to contain a single
// noise-flagged fragment key is still that speaker, and only the noise key's
// segments belong to the noise bucket. (Deciding by group with `.some()` is the
// bug that hid named speakers like "Elise Lippe" from the filter/analytics.)
function _isNoiseKey(key) {
  return key === _NOISE_LABEL || _manualNoiseKeys.has(key);
}

// Split name-grouped profiles into named-speaker groups + a flat list of noise
// keys, applying noise per-key. Shared by every speaker surface (filter chips,
// analytics, …) so they always agree on who is a speaker vs noise. A group with
// only noise keys drops out of `speakerGroups` entirely (it's pure noise);
// a mixed group keeps its non-noise keys and contributes its noise keys to the
// noise bucket.
function _partitionSpeakerGroupsByNoise(groups) {
  const speakerGroups = [];
  const noiseKeys = [];
  for (const g of groups) {
    const named = g.speakerKeys.filter(k => !_isNoiseKey(k));
    for (const k of g.speakerKeys) if (_isNoiseKey(k)) noiseKeys.push(k);
    if (named.length) speakerGroups.push({ ...g, speakerKeys: named });
  }
  return { speakerGroups, noiseKeys };
}

// Select all speaker_keys belonging to a group, with range/toggle support.
function _setGroupSelection(group, { toggle = false, range = false } = {}) {
  const groups = _groupProfilesByName(_getSortedSpeakerProfiles());
  const anchorGroupIdx = groups.findIndex(g => g.speakerKeys.includes(_speakerSelectionAnchor));
  const clickedGroupIdx = groups.findIndex(g => g.speakerKeys[0] === group.speakerKeys[0]);

  if (range && anchorGroupIdx !== -1 && clickedGroupIdx !== -1) {
    const [from, to] = anchorGroupIdx < clickedGroupIdx
      ? [anchorGroupIdx, clickedGroupIdx]
      : [clickedGroupIdx, anchorGroupIdx];
    _selectedSpeakerKeys = groups.slice(from, to + 1).flatMap(g => g.speakerKeys);
  } else if (toggle) {
    const allSelected = group.speakerKeys.every(k => _selectedSpeakerKeys.includes(k));
    if (allSelected) {
      _selectedSpeakerKeys = _selectedSpeakerKeys.filter(k => !group.speakerKeys.includes(k));
    } else {
      const newKeys = group.speakerKeys.filter(k => !_selectedSpeakerKeys.includes(k));
      _selectedSpeakerKeys = [..._selectedSpeakerKeys, ...newKeys];
    }
    _speakerSelectionAnchor = group.speakerKeys[0];
  } else {
    // Plain click: toggle if already the sole selection, otherwise select
    const allSelected = group.speakerKeys.every(k => _selectedSpeakerKeys.includes(k));
    if (allSelected && _selectedSpeakerKeys.length === group.speakerKeys.length) {
      _selectedSpeakerKeys = [];
    } else {
      _selectedSpeakerKeys = [...group.speakerKeys];
    }
    _speakerSelectionAnchor = group.speakerKeys[0];
  }

  if (!range) _speakerSelectionAnchor = group.speakerKeys[0];
  if (range && anchorGroupIdx === -1) _speakerSelectionAnchor = group.speakerKeys[0];
  _syncSpeakerDraftFromSelection();
  _highlightSelectedSpeakerBadges();
  renderSpeakerManager();
}

function _setSpeakerSelection(speakerKey, { toggle = false, range = false } = {}) {
  const orderedKeys = _getSortedSpeakerProfiles().map(profile => profile.speaker_key);

  if (range && _speakerSelectionAnchor) {
    const start = orderedKeys.indexOf(_speakerSelectionAnchor);
    const end = orderedKeys.indexOf(speakerKey);
    if (start !== -1 && end !== -1) {
      const [from, to] = start < end ? [start, end] : [end, start];
      _selectedSpeakerKeys = orderedKeys.slice(from, to + 1);
    } else {
      _selectedSpeakerKeys = [speakerKey];
    }
  } else if (toggle) {
    if (_selectedSpeakerKeys.includes(speakerKey)) {
      _selectedSpeakerKeys = _selectedSpeakerKeys.filter(key => key !== speakerKey);
    } else {
      _selectedSpeakerKeys = [..._selectedSpeakerKeys, speakerKey];
    }
    _speakerSelectionAnchor = speakerKey;
  } else {
    // Plain click: toggle if already selected
    if (_selectedSpeakerKeys.length === 1 && _selectedSpeakerKeys[0] === speakerKey) {
      _selectedSpeakerKeys = [];
    } else {
      _selectedSpeakerKeys = [speakerKey];
    }
    _speakerSelectionAnchor = speakerKey;
  }

  if (!range && !toggle) _speakerSelectionAnchor = speakerKey;
  if (range && !_speakerSelectionAnchor) _speakerSelectionAnchor = speakerKey;
  _syncSpeakerDraftFromSelection();
  _highlightSelectedSpeakerBadges();
  renderSpeakerManager();
}

/* Deterministic landing. Cleanup is a bulk-repair surface and is never the
 * surprise destination: open on Resolve when something actually needs a name,
 * otherwise Manage. An explicit tab argument always wins, and within one
 * session view we return to whatever tab the user last chose. */
function _speakerManagerInitialTab(explicitTab) {
  if (explicitTab) return explicitTab;
  if (_speakerModalLastTab && _speakerModalStatsSession === state.sessionId) return _speakerModalLastTab;
  // Cleanup is the landing tab: merging diarizer fragments is the first job
  // on almost every recording, and Resolve only makes sense once it is done.
  // A caller that wants another tab passes it explicitly.
  return 'cleanup';
}

function openSpeakerManager(tabArg) {
  // Tolerate being wired straight to an event handler.
  // 'resolve' is accepted for older callers: that tab was folded into Cleanup.
  const tab = ['manage', 'cleanup'].includes(tabArg) ? tabArg : (tabArg === 'resolve' ? 'cleanup' : null);
  document.getElementById('speaker-manager-overlay').classList.remove('hidden');
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
  const landing = _speakerManagerInitialTab(tab);
  switchSpeakerManagerTab(landing, { remember: !!tab });
  _speakerModalFocusTab(landing);
  // Stats arrive after the first paint and feed the tab badges. The landing
  // tab is never changed underneath the user once it is showing.
  refreshSpeakerModalHeader();
}

// The badge means ONE thing: how many speaker groups the Cleanup tab holds.
// Until the clusters load there is no honest number, so the badge stays hidden
// rather than showing an unlinked-speaker count that changes meaning a moment
// later. _cleanupUpdateBadge owns it from then on.
function _cleanupPaintQuickBadge() {
  const badge = document.getElementById('speaker-cleanup-badge');
  if (!badge) return;
  if (_cleanupState && _cleanupState.sessionId === state.sessionId) { _cleanupUpdateBadge(); return; }
  badge.hidden = true;
  badge.textContent = '';
  badge.title = '';
}

function closeSpeakerManager() {
  stopSpeakerVoice();   // don't leave a voice sample playing after the panel closes
  _dismissCleanupStagedToast();
  document.getElementById('speaker-manager-overlay').classList.add('hidden');
}

function closeSpeakerManagerOnOverlay(event) {
  if (event.target.id === 'speaker-manager-overlay') closeSpeakerManager();
}

/* ── Speaker cleanup view ─────────────────────────────────────────────────────
 * Drag-and-drop interface for bulk speaker re-labeling. Pulls clusters from
 * /api/sessions/{sid}/speaker_clusters, lets the user rearrange members
 * between cards (or create new clusters via the +New zone), then POSTs the
 * final layout to /apply which retrains affected library profiles.
 *
 * State is kept entirely client-side until Apply is hit. We carry per-member
 * embeddings (256 floats, base64) and the full library (with centroids) so we
 * can recompute suggestions instantly on every drop without round-tripping.
 * ─────────────────────────────────────────────────────────────────────────── */

let _cleanupState = null;
// { sessionId, clusters: [...], noiseKeys: Set, library: [...], thresholds, originalSnapshot, dirty }
let _cleanupActiveTab = 'manage';
let _cleanupDragKeys = [];            // speaker_keys currently being dragged (multi-select aware)
let _cleanupExpandedKeys = new Set();
let _cleanupNoiseExpanded = false;
let _cleanupSelectedKeys = new Set(); // multi-select: speaker_keys highlighted for bulk ops
let _cleanupSelAnchor = null;         // anchor key for Shift-range selection
let _cleanupKeyOrder = [];            // visual order of member keys, rebuilt each render
let _cleanupShowHeatmap = false;      // similarity heatmap view toggle
let _cleanupPlayQueueState = null;    // sequential audio/video player: { btn, segs, idx, key, timer }
let _cleanupPicker = null;            // open assignment popover element, or null
let _cleanupStagedToast = null;       // "changes still staged" reminder, so it can be dismissed

// The staged-changes reminder must not outlive the modal: its Apply action
// would otherwise write edits the user has since discarded.
function _dismissCleanupStagedToast() {
  if (_cleanupStagedToast && typeof _cleanupStagedToast.dismiss === 'function') {
    try { _cleanupStagedToast.dismiss(); } catch (_) {}
  }
  _cleanupStagedToast = null;
}

function switchSpeakerManagerTab(tab, opts) {
  const remember = !opts || opts.remember !== false;
  // Leaving Cleanup does NOT discard staged edits (they live in _cleanupState
  // until Apply), so a blocking confirm here would be a lie. Say plainly that
  // they are still unwritten and offer the commit.
  if (_cleanupActiveTab === 'cleanup' && tab !== 'cleanup' && _cleanupState && _cleanupState.dirty) {
    const pending = Math.max(_cleanupPendingChangeCount(), 1);
    _cleanupStagedToast = uiToast({
      message: `${_plural(pending, 'cleanup change is', 'cleanup changes are')} still staged. Nothing is written until you click Apply.`,
      kind: 'warn',
      id: 'cleanup-staged-reminder',
      action: {
        label: 'Apply now',
        onClick: () => {
          // Recheck: "Close and discard" clears the dirty flag but leaves the
          // staged state in place, and this toast can outlive that click.
          if (!_cleanupState || !_cleanupState.dirty) {
            uiToast({ message: 'Those cleanup changes were discarded.', kind: 'info', id: 'cleanup-staged-reminder' });
            return;
          }
          switchSpeakerManagerTab('cleanup');
          applySpeakerCleanup();
        },
      },
    });
  }
  _cleanupActiveTab = tab;
  if (remember) _speakerModalLastTab = tab;
  // Leaving the current tab: stop any voice sample it was playing.
  stopSpeakerVoice();
  document.querySelectorAll('.speaker-manager-tab').forEach(b => {
    const on = b.dataset.tab === tab;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('[data-tab-view]').forEach(el => {
    el.hidden = el.dataset.tabView !== tab;
  });
  // Cleanup is card-heavy and needs far more room than the compact Manage
  // list: widen the dialog (and let it grow taller) for it.
  const dialog = document.querySelector('#speaker-manager-overlay .speaker-manager-dialog');
  if (dialog) dialog.classList.toggle('cleanup-active', tab === 'cleanup');
  if (tab === 'cleanup') {
    // Load (or reload) whenever there's no state or it's stale for another session.
    if (!_cleanupState || _cleanupState.sessionId !== state.sessionId) loadSpeakerClusters();
    _cleanupVideoSyncToggleBtn();
    _cleanupSyncFooter();
  } else {
    _cleanupClosePicker();
  }
}

function openSpeakerCleanupTab() {
  openSpeakerManager('cleanup');
}

async function loadSpeakerClusters(force = false) {
  const sid = state.sessionId;
  if (!sid) return;
  if (_cleanupState && !force && _cleanupState.sessionId === sid) {
    renderSpeakerClusters();
    return;
  }
  const loading = document.getElementById('cleanup-loading');
  const grid = document.getElementById('cleanup-grid');
  const noiseSection = document.getElementById('cleanup-noise-section');
  if (loading) { loading.hidden = false; loading.querySelector('#cleanup-loading-text').textContent = 'Analyzing speakers…'; }
  if (grid) grid.innerHTML = '';
  if (noiseSection) noiseSection.hidden = true;
  try {
    // The calendar's view of the meeting rides along: it feeds the picker's
    // attendee list and the invite's attendee count. Never fatal.
    const [resp, candResp] = await Promise.all([
      fetch(`/api/sessions/${sid}/speaker_clusters`),
      fetch(`/api/sessions/${sid}/resolution_candidates`).catch(() => null),
    ]);
    const data = await resp.json();
    if (!resp.ok) {
      grid.innerHTML = `<div class="cleanup-help">Couldn't load clusters: ${data.error || resp.status}</div>`;
      loading.hidden = true;
      return;
    }
    _cleanupState = _cleanupBuildState(data);
    _cleanupState.calendar = await _cleanupReadCandidates(candResp);
    if (loading) loading.hidden = true;
    renderSpeakerClusters();
    _cleanupUpdateBadge();
    _cleanupSyncFooter();
  } catch (e) {
    grid.innerHTML = `<div class="cleanup-help">Couldn't load clusters: ${e.message}</div>`;
    if (loading) loading.hidden = true;
  }
}

/** The calendar's view of this meeting, for Cleanup: the invite's attendees
 *  (with job title and line of business when the feed carries them), any agent
 *  hints keyed by speaker, and the attendee count. Shaped empty when the
 *  meeting has no calendar match or the calendar is off. */
async function _cleanupReadCandidates(resp) {
  const empty = { meeting: {}, candidates: [], hints: [] };
  if (!resp || !resp.ok) return empty;
  try {
    const blob = await resp.json();
    return {
      meeting: blob.meeting || {},
      candidates: (Array.isArray(blob.candidates) ? blob.candidates : []).filter(c => c && c.name),
      hints: Array.isArray(blob.speaker_hints) ? blob.speaker_hints : [],
    };
  } catch (_) {
    return empty;
  }
}

async function reloadSpeakerClusters() {
  if (_cleanupState && _cleanupState.dirty) {
    const pending = Math.max(_cleanupPendingChangeCount(), 1);
    if (!await uiConfirm({
      title: 'Discard staged cleanup changes?',
      message: `Reloading rereads the groups from disk and throws away ${_plural(pending, 'staged change', 'staged changes')}.`,
      confirmLabel: 'Discard and reload', danger: true,
    })) return;
  }
  loadSpeakerClusters(true);
}

function _cleanupBuildState(payload) {
  // Fresh load - drop any stale multi-selection / open popover.
  _cleanupSelectedKeys = new Set();
  _cleanupSelAnchor = null;
  _cleanupClosePicker();
  // The picker similarity index derives from this state; force it to refresh.
  _simIndex = null;
  // Decode all centroids once. We keep both labeled + unlabeled clusters in
  // one homogenous list, plus a separate noise bucket.
  const decode = b64 => {
    if (!b64) return null;
    const bin = atob(b64);
    const buf = new ArrayBuffer(bin.length);
    const view = new Uint8Array(buf);
    for (let i = 0; i < bin.length; i++) view[i] = bin.charCodeAt(i);
    return new Float32Array(buf);
  };

  const buildMember = m => ({
    speaker_key:   m.speaker_key,
    name:          m.name,
    color:         m.color,
    segments:      m.segments || [],
    segment_count: m.segment_count || (m.segments || []).length,
    emb_count:     m.emb_count || 0,
    centroid:      decode(m.centroid),
    is_noise:      !!m.is_noise,
    original_global_id: m.global_id || null,
  });

  const buildCluster = (c, kind) => ({
    cluster_id: c.cluster_id,
    kind,
    global_id:  c.global_id || null,
    new_name:   '',
    name:       c.name || '',
    color:      c.color || null,
    members:    (c.members || []).map(buildMember),
    suggestion: c.suggestion || null,
    _dropped_suggestions: new Set(),  // global_ids the user rejected for this cluster
  });

  const labeled = (payload.labeled_clusters || []).map(c => buildCluster(c, 'labeled'));
  const unlabeled = (payload.unlabeled_clusters || []).map(c => buildCluster(c, 'unlabeled'));
  const clusters = [...labeled, ...unlabeled];

  const noiseKeys = new Set();
  const noiseMembers = new Map();  // speaker_key → member (kept separate so noise pills can be rendered)
  (payload.noise_cluster?.members || []).forEach(m => {
    noiseKeys.add(m.speaker_key);
    noiseMembers.set(m.speaker_key, buildMember(m));
  });

  const library = (payload.library || []).map(g => ({
    ...g,
    centroid: decode(g.centroid),
  }));

  // Snapshot of original assignment for diffing on Apply.
  const snapshot = {};
  clusters.forEach(c => {
    c.members.forEach(m => {
      snapshot[m.speaker_key] = { cluster_id: c.cluster_id, is_noise: false };
    });
  });
  noiseKeys.forEach(k => { snapshot[k] = { cluster_id: 'noise', is_noise: true }; });

  // Per-cluster identity snapshot. Membership alone does not capture a staged
  // link, unlink or rename, so the pending-change count has to diff this too.
  const clusterSnapshot = {};
  clusters.forEach(c => {
    clusterSnapshot[c.cluster_id] = {
      global_id: c.global_id || null,
      name:      c.name || '',
      new_name:  c.new_name || '',
    };
  });

  return {
    sessionId:  payload.session_id,
    clusters,
    noiseKeys,
    clusterSnapshot,
    noiseMembers,
    library,
    thresholds: payload.thresholds || { cluster: 0.7, suggest: 0.65, auto: 0.82 },
    stats:      payload.stats || {},
    originalSnapshot: snapshot,
    dirty: false,
  };
}

// The Cleanup badge counts GROUPS, matching what the tab actually shows.
function _cleanupUpdateBadge() {
  const badge = document.getElementById('speaker-cleanup-badge');
  if (!badge || !_cleanupState) return;
  const groups = _cleanupState.clusters.filter(c => c.members.length).length;
  badge.hidden = groups === 0;
  badge.textContent = String(groups);
  badge.title = `${_plural(groups, 'speaker group', 'speaker groups')} in this meeting`;
}

function _cleanupMarkDirty() {
  if (!_cleanupState) return;
  _cleanupState.dirty = true;
  _cleanupSyncFooter();
}

function _cleanupRecomputeClusterCentroid(cluster) {
  // Weighted by emb_count, L2-normalized.
  const members = cluster.members.filter(m => m.centroid && !_cleanupState.noiseKeys.has(m.speaker_key));
  if (!members.length) { cluster._centroid = null; return; }
  const dim = members[0].centroid.length;
  const sum = new Float32Array(dim);
  let totalW = 0;
  for (const m of members) {
    const w = Math.max(m.emb_count, 1);
    totalW += w;
    for (let i = 0; i < dim; i++) sum[i] += m.centroid[i] * w;
  }
  if (totalW === 0) { cluster._centroid = null; return; }
  let norm = 0;
  for (let i = 0; i < dim; i++) norm += sum[i] * sum[i];
  norm = Math.sqrt(norm) || 1;
  for (let i = 0; i < dim; i++) sum[i] /= norm;
  cluster._centroid = sum;
}

function _cleanupBestLibraryMatch(centroid, excludeGlobalIds) {
  if (!centroid || !_cleanupState) return null;
  let best = null;
  for (const g of _cleanupState.library) {
    if (!g.centroid) continue;
    if (excludeGlobalIds.has(g.global_id)) continue;
    let sim = 0;
    for (let i = 0; i < centroid.length; i++) sim += centroid[i] * g.centroid[i];
    if (!best || sim > best.similarity) {
      best = {
        global_id: g.global_id,
        name: g.name,
        color: g.color,
        similarity: +sim.toFixed(3),
        auto_apply: sim >= _cleanupState.thresholds.auto,
      };
    }
  }
  if (!best || best.similarity < _cleanupState.thresholds.suggest) return null;
  return best;
}

function _cleanupUpdateSuggestion(cluster) {
  // Profiles already used by OTHER labeled clusters in this session are not
  // candidates - Antonio shouldn't be re-suggested for a cluster that's not
  // already his.
  const taken = new Set();
  for (const c of _cleanupState.clusters) {
    if (c.cluster_id !== cluster.cluster_id && c.global_id) taken.add(c.global_id);
  }
  if (cluster.global_id) taken.add(cluster.global_id);
  cluster._dropped_suggestions.forEach(g => taken.add(g));
  _cleanupRecomputeClusterCentroid(cluster);
  cluster.suggestion = _cleanupBestLibraryMatch(cluster._centroid, taken);
}

function renderSpeakerClusters() {
  const grid = document.getElementById('cleanup-grid');
  const statsEl = document.getElementById('cleanup-stats');
  const heatWrap = document.getElementById('cleanup-heatmap-wrap');
  if (!grid || !_cleanupState) return;

  // The invite's attendee count is the ceiling the clusters should land under.
  const calNote = document.getElementById('cleanup-calendar-note');
  if (calNote) {
    const cal = _cleanupState.calendar || {};
    const meeting = cal.meeting || {};
    const n = Number(meeting.attendee_count) || 0;
    if (n) {
      const subject = meeting.calendar_subject && meeting.calendar_subject !== meeting.title
        ? ` · ${escapeHtml(meeting.calendar_subject)}` : '';
      calNote.innerHTML = `<i class="fa-solid fa-calendar-days" aria-hidden="true"></i> ${n} on the invite${subject}`;
      calNote.hidden = false;
    } else {
      calNote.hidden = true;
    }
  }

  // Recompute centroids + suggestions before render so drag reorderings are
  // reflected in similarity suggestions.
  _cleanupState.clusters.forEach(_cleanupUpdateSuggestion);

  // ── Heatmap vs cluster-grid view ──
  const heatBtn = document.getElementById('cleanup-heatmap-toggle');
  if (heatBtn) heatBtn.classList.toggle('active', _cleanupShowHeatmap);
  // Use explicit display values (not '') - the heatmap wrap still carries the
  // [hidden] attribute, so '' would fall back to the UA display:none rule.
  grid.style.display = _cleanupShowHeatmap ? 'none' : 'grid';
  if (heatWrap) heatWrap.style.display = _cleanupShowHeatmap ? 'flex' : 'none';

  if (_cleanupShowHeatmap) {
    _cleanupRenderHeatmap(heatWrap);
  } else {
    grid.innerHTML = '';
    _cleanupKeyOrder = [];

    // Sort: labeled first (by segment count desc), then unlabeled (seg count desc).
    const sorted = [..._cleanupState.clusters].sort((a, b) => {
      if (a.kind !== b.kind) return a.kind === 'labeled' ? -1 : 1;
      const segCount = c => c.members.reduce((s, m) => s + m.segment_count, 0);
      return segCount(b) - segCount(a);
    });
    for (const cluster of sorted) grid.appendChild(_cleanupRenderCluster(cluster));

    // "+ New group" drop zone (always last in the grid).
    const newZone = document.createElement('div');
    newZone.className = 'cleanup-new-cluster';
    newZone.innerHTML = '<i class="fa-solid fa-plus"></i> Drop here to merge into a new group';
    _cleanupWireDropZone(newZone, () => _cleanupMoveKeysToNewCluster(_cleanupDragKeys));
    grid.appendChild(newZone);
  }

  // ── Noise drop zone (shown beneath either view) ──
  _cleanupRenderNoiseSection();

  // ── Stats ──
  if (statsEl) {
    const labeledCount = _cleanupState.clusters.filter(c => c.kind === 'labeled' && c.members.length).length;
    const unlabeledCount = _cleanupState.clusters.filter(c => c.kind === 'unlabeled' && c.members.length).length;
    const total = _cleanupState.stats.speakers_total || 0;
    const noiseCount = _cleanupState.noiseKeys.size;
    statsEl.innerHTML = `<strong>${labeledCount}</strong> named · <strong>${unlabeledCount}</strong> unnamed · <strong>${total}</strong> voices${noiseCount ? ` · <strong>${noiseCount}</strong> noise` : ''}`;
  }

  // Enable Auto-link when any unlabeled cluster has a high-confidence match.
  const confidentBtn = document.getElementById('cleanup-confident-btn');
  if (confidentBtn) {
    const hasConfident = _cleanupState.clusters.some(
      c => !c.global_id && c.suggestion && c.suggestion.similarity >= _cleanupState.thresholds.auto,
    );
    confidentBtn.disabled = !hasConfident;
  }

  _cleanupRenderSelectionBar();
  _cleanupUpdateBadge();
  _cleanupSyncFooter();
  _cleanupWireGridAutoscroll();
}

function _cleanupRenderNoiseSection() {
  const noiseSection = document.getElementById('cleanup-noise-section');
  const noiseCountEl = document.getElementById('cleanup-noise-count');
  const noiseMembersEl = document.getElementById('cleanup-noise-members');
  if (!noiseSection) return;
  const noiseList = Array.from(_cleanupState.noiseKeys)
    .map(k => _cleanupGetMember(k))
    .filter(Boolean);
  // Always a drop target so users can drag speakers here to silence them.
  // Wire once - this element is persistent (lives in the template), so re-wiring
  // each render would stack duplicate listeners.
  if (!noiseSection._dropWired) {
    noiseSection._dropWired = true;
    _cleanupWireDropZone(noiseSection, () => _cleanupMarkKeysNoise(_cleanupDragKeys));
  }
  if (noiseList.length === 0) {
    noiseSection.hidden = false;
    noiseSection.classList.add('empty');
    noiseSection.classList.remove('expanded');
    if (noiseCountEl) noiseCountEl.textContent = '0';
    if (noiseMembersEl) { noiseMembersEl.hidden = true; noiseMembersEl.innerHTML = ''; }
    return;
  }
  noiseSection.hidden = false;
  noiseSection.classList.remove('empty');
  noiseSection.classList.toggle('expanded', _cleanupNoiseExpanded);
  if (noiseCountEl) noiseCountEl.textContent = String(noiseList.length);
  if (noiseMembersEl) {
    noiseMembersEl.hidden = !_cleanupNoiseExpanded;
    noiseMembersEl.innerHTML = '';
    noiseList.forEach(m => noiseMembersEl.appendChild(_cleanupRenderMember(m, null, /*inNoise*/ true)));
  }
}

/* ── Multi-select model ───────────────────────────────────────────────────
 * Members can be selected (click / Ctrl-click / Shift-range) and then dragged
 * or bulk-acted on via the floating selection bar. This removes the need to
 * drag pills one-at-a-time across a tall widget. */

function _cleanupSelectPill(key, opts) {
  opts = opts || {};
  if (opts.range && _cleanupSelAnchor) {
    const order = _cleanupKeyOrder;
    const a = order.indexOf(_cleanupSelAnchor);
    const b = order.indexOf(key);
    if (a >= 0 && b >= 0) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      if (!opts.toggle) _cleanupSelectedKeys = new Set();
      for (let i = lo; i <= hi; i++) _cleanupSelectedKeys.add(order[i]);
    } else {
      _cleanupSelectedKeys.add(key);
    }
  } else if (opts.toggle) {
    if (_cleanupSelectedKeys.has(key)) _cleanupSelectedKeys.delete(key);
    else _cleanupSelectedKeys.add(key);
    _cleanupSelAnchor = key;
  } else {
    // Plain click: toggle a singleton selection (click again to deselect).
    if (_cleanupSelectedKeys.size === 1 && _cleanupSelectedKeys.has(key)) {
      _cleanupSelectedKeys = new Set();
      _cleanupSelAnchor = null;
    } else {
      _cleanupSelectedKeys = new Set([key]);
      _cleanupSelAnchor = key;
    }
  }
  _cleanupRefreshSelectionUI();
}

// Cheap UI refresh that doesn't rebuild the whole grid - just toggles the
// `selected` class on pills and re-renders the selection bar.
function _cleanupRefreshSelectionUI() {
  document.querySelectorAll('#cleanup-grid .cleanup-member, #cleanup-noise-members .cleanup-member')
    .forEach(pill => {
      pill.classList.toggle('selected', _cleanupSelectedKeys.has(pill.dataset.speakerKey));
    });
  _cleanupRenderSelectionBar();
}

function _cleanupClearSelection() {
  _cleanupSelectedKeys = new Set();
  _cleanupSelAnchor = null;
  _cleanupRefreshSelectionUI();
}

function _cleanupRenderSelectionBar() {
  const bar = document.getElementById('cleanup-selbar');
  const countEl = document.getElementById('cleanup-selbar-count');
  if (!bar) return;
  const n = _cleanupSelectedKeys.size;
  const body = document.querySelector('#speaker-manager-overlay .speaker-cleanup-body');
  if (body) body.classList.toggle('has-selection', n > 0);
  if (n === 0) { bar.hidden = true; return; }
  bar.hidden = false;
  if (countEl) countEl.textContent = `${n} selected`;
  // The Noise button doubles as Restore when every selected pill is noise.
  const allNoise = Array.from(_cleanupSelectedKeys).every(k => _cleanupState.noiseKeys.has(k));
  const noiseBtn = document.getElementById('cleanup-selbar-noise');
  if (noiseBtn) {
    noiseBtn.innerHTML = allNoise
      ? '<i class="fa-solid fa-rotate-left"></i> Restore'
      : '<i class="fa-solid fa-volume-xmark"></i> Noise';
  }
}

function _cleanupAllMembers() {
  // Yields { key, member, cluster } for every (non-noise) cluster member.
  const out = [];
  for (const c of _cleanupState.clusters) {
    for (const m of c.members) out.push({ key: m.speaker_key, member: m, cluster: c });
  }
  return out;
}

function _cleanupGetMember(speakerKey) {
  // Return a member object regardless of where it lives (cluster or noise bucket).
  const found = _cleanupFindMember(speakerKey);
  if (found) return found.member;
  return _cleanupState.noiseMembers.get(speakerKey) || null;
}

// Wire an element as a drag drop-target for the current multi-selection.
function _cleanupWireDropZone(el, onDrop) {
  el.addEventListener('dragover', e => {
    if (_cleanupDragKeys.length) { e.preventDefault(); el.classList.add('drop-target'); }
  });
  el.addEventListener('dragleave', e => {
    if (e.target === el || !el.contains(e.relatedTarget)) el.classList.remove('drop-target');
  });
  el.addEventListener('drop', e => {
    e.preventDefault();
    el.classList.remove('drop-target');
    if (_cleanupDragKeys.length) onDrop();
  });
}

function _cleanupRenderCluster(cluster) {
  const card = document.createElement('div');
  card.className = `cleanup-cluster kind-${cluster.kind}`;
  card.dataset.clusterId = cluster.cluster_id;
  const visibleMembers = cluster.members.filter(m => !_cleanupState.noiseKeys.has(m.speaker_key));
  if (!visibleMembers.length) card.classList.add('cluster-empty');

  const accent = cluster.color || (visibleMembers[0] && visibleMembers[0].color) || '#6e7681';
  card.style.setProperty('--cluster-accent', accent);

  // ── Header ──
  const header = document.createElement('div');
  header.className = 'cleanup-cluster-header';

  const swatch = document.createElement('span');
  swatch.className = 'cleanup-cluster-swatch';
  swatch.style.background = accent;
  header.appendChild(swatch);

  const nameWrap = document.createElement('div');
  nameWrap.className = 'cleanup-cluster-name';
  const label = document.createElement('span');
  label.className = 'cleanup-cluster-label';
  if (cluster.kind === 'labeled') {
    label.textContent = cluster.name || '(unnamed)';
    nameWrap.appendChild(label);
    const linked = document.createElement('span');
    linked.className = 'cleanup-cluster-linked';
    linked.innerHTML = '<i class="fa-solid fa-link"></i>';
    linked.title = 'Linked to a voice-library profile';
    nameWrap.appendChild(linked);
  } else {
    label.classList.add('placeholder');
    label.textContent = cluster.new_name ? cluster.new_name : 'Unnamed group';
    nameWrap.appendChild(label);
  }
  header.appendChild(nameWrap);

  const count = document.createElement('span');
  count.className = 'cleanup-cluster-count';
  const segTotal = visibleMembers.reduce((s, m) => s + m.segment_count, 0);
  count.textContent = `${segTotal} seg`;
  header.appendChild(count);

  // Assign / change button - opens the voice-library picker popover.
  const assignBtn = document.createElement('button');
  assignBtn.className = 'cleanup-assign-btn' + (cluster.kind === 'labeled' ? ' is-set' : '');
  assignBtn.innerHTML = cluster.kind === 'labeled'
    ? '<i class="fa-solid fa-pen"></i>'
    : '<i class="fa-solid fa-link"></i> Link';
  assignBtn.title = cluster.kind === 'labeled'
    ? 'Change the linked voice-library profile'
    : 'Link this speaker to a voice-library profile';
  assignBtn.addEventListener('click', e => {
    e.stopPropagation();
    _cleanupOpenPicker(assignBtn, { cluster });
  });
  header.appendChild(assignBtn);
  card.appendChild(header);

  // ── One-click suggestion chip (unlabeled clusters only) ──
  if (cluster.suggestion && !cluster.global_id && cluster.kind === 'unlabeled') {
    const row = document.createElement('div');
    row.className = 'cleanup-suggestion-row';
    const sugg = document.createElement('button');
    sugg.className = 'cleanup-suggestion';
    sugg.title = `Link to ${cluster.suggestion.name}`;
    const conf = cluster.suggestion.similarity >= _cleanupState.thresholds.auto ? ' high' : '';
    sugg.innerHTML =
      `<span class="cleanup-cluster-swatch sm" style="background:${cluster.suggestion.color || '#58a6ff'}"></span>` +
      `<span class="txt">Sounds like <strong>${escapeHtml(cluster.suggestion.name)}</strong></span>` +
      `<span class="sim${conf}">${Math.round(cluster.suggestion.similarity * 100)}%</span>` +
      `<span class="cleanup-suggestion-go"><i class="fa-solid fa-check"></i> Link</span>`;
    sugg.addEventListener('click', e => {
      e.stopPropagation();
      _cleanupAssignClusterToProfile(cluster, cluster.suggestion);
    });
    const reject = document.createElement('button');
    reject.className = 'cleanup-suggestion-reject';
    reject.innerHTML = '<i class="fa-solid fa-xmark"></i>';
    reject.title = 'Not this person';
    reject.addEventListener('click', e => {
      e.stopPropagation();
      cluster._dropped_suggestions.add(cluster.suggestion.global_id);
      cluster.suggestion = null;
      renderSpeakerClusters();
    });
    row.appendChild(sugg);
    row.appendChild(reject);
    card.appendChild(row);
  }

  // ── Members ──
  const memberRow = document.createElement('div');
  memberRow.className = 'cleanup-members';
  memberRow.dataset.clusterId = cluster.cluster_id;
  // Whole card is the drop target so short drags near the header still land.
  _cleanupWireDropZone(card, () => _cleanupMoveKeysToCluster(_cleanupDragKeys, cluster.cluster_id));
  visibleMembers.forEach(m => {
    _cleanupKeyOrder.push(m.speaker_key);
    memberRow.appendChild(_cleanupRenderMember(m, cluster, false));
  });
  card.appendChild(memberRow);

  return card;
}

function _cleanupTalkTime(member) {
  return (member.segments || []).reduce((s, seg) => s + Math.max(0, seg.end - seg.start), 0);
}

// Furthest segment end across the whole session - used to scale mini-timelines.
function _cleanupSessionSpan() {
  if (_cleanupState._span) return _cleanupState._span;
  let max = 0;
  const scan = m => { for (const s of m.segments) if (s.end > max) max = s.end; };
  _cleanupState.clusters.forEach(c => c.members.forEach(scan));
  if (_cleanupState.noiseMembers) _cleanupState.noiseMembers.forEach(scan);
  _cleanupState._span = max || 1;
  return _cleanupState._span;
}

function _cleanupBuildTimeline(member) {
  const span = _cleanupSessionSpan();
  const tl = document.createElement('div');
  tl.className = 'cleanup-timeline';
  tl.title = 'When this speaker talks across the meeting (click a tick to play)';
  member.segments.forEach(seg => {
    const tick = document.createElement('span');
    tick.className = 'cleanup-tl-tick';
    tick.style.left = `${(seg.start / span) * 100}%`;
    tick.style.width = `${Math.max(0.5, ((seg.end - seg.start) / span) * 100)}%`;
    tick.style.background = member.color || 'var(--accent, #58a6ff)';
    tick.title = `${_fmtTime(seg.start)} · ${(seg.end - seg.start).toFixed(1)}s`;
    tick.addEventListener('click', e => {
      e.stopPropagation();
      _cleanupPlayQueue([seg], null, { key: `seg:${seg.id}` });
    });
    tl.appendChild(tick);
  });
  return tl;
}

function _cleanupBuildSegRow(seg, member) {
  const r = document.createElement('div');
  r.className = 'cleanup-seg-row';
  const play = document.createElement('button');
  play.className = 'cleanup-seg-play';
  play.dataset.segKey = `seg:${seg.id}`;
  play.innerHTML = '<i class="fa-solid fa-play"></i>';
  play.title = 'Play segment';
  play.addEventListener('click', e => { e.stopPropagation(); _cleanupPlayQueue([seg], play, { key: `seg:${seg.id}` }); });
  r.appendChild(play);
  const t = document.createElement('span');
  t.className = 'cleanup-seg-time';
  t.textContent = _fmtTime(seg.start);
  r.appendChild(t);
  const txt = document.createElement('span');
  txt.className = 'cleanup-seg-text';
  if (seg.text) { txt.textContent = seg.text; }
  else { txt.textContent = '(no transcript)'; txt.classList.add('empty'); }
  r.appendChild(txt);
  return r;
}

// Custom drag image when moving multiple speakers at once.
function _cleanupSetDragImage(e, n) {
  if (n <= 1 || !e.dataTransfer || !e.dataTransfer.setDragImage) return;
  const ghost = document.createElement('div');
  ghost.className = 'cleanup-drag-ghost';
  ghost.textContent = `${n} speakers`;
  document.body.appendChild(ghost);
  try { e.dataTransfer.setDragImage(ghost, 12, 12); } catch (_) {}
  setTimeout(() => ghost.remove(), 0);
}

function _cleanupRenderMember(member, cluster, inNoise) {
  const pill = document.createElement('div');
  pill.className = 'cleanup-member';
  pill.dataset.speakerKey = member.speaker_key;
  if (inNoise) pill.classList.add('is-noise');
  if (_cleanupSelectedKeys.has(member.speaker_key)) pill.classList.add('selected');
  const expanded = _cleanupExpandedKeys.has(member.speaker_key);
  if (expanded) pill.classList.add('expanded');
  pill.draggable = true;

  pill.addEventListener('dragstart', e => {
    // Drag the whole selection if this pill is part of it; otherwise drag just
    // this one (and make it the selection so what moves matches what's lit up).
    if (!_cleanupSelectedKeys.has(member.speaker_key)) {
      _cleanupSelectedKeys = new Set([member.speaker_key]);
      _cleanupSelAnchor = member.speaker_key;
      _cleanupRefreshSelectionUI();
    }
    _cleanupDragKeys = Array.from(_cleanupSelectedKeys);
    document.querySelectorAll('.cleanup-member').forEach(p => {
      if (_cleanupDragKeys.includes(p.dataset.speakerKey)) p.classList.add('dragging');
    });
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', member.speaker_key); } catch (_) {}
    _cleanupSetDragImage(e, _cleanupDragKeys.length);
  });
  pill.addEventListener('dragend', () => {
    _cleanupDragKeys = [];
    _cleanupStopAutoscroll();
    document.querySelectorAll('.cleanup-member.dragging').forEach(p => p.classList.remove('dragging'));
    document.querySelectorAll('.drop-target').forEach(el => el.classList.remove('drop-target'));
  });

  // ── Always-visible row ──
  const row = document.createElement('div');
  row.className = 'cleanup-member-row';
  row.addEventListener('click', e => {
    if (e.target.closest('button')) return;  // buttons handle their own clicks
    _cleanupSelectPill(member.speaker_key, { toggle: e.ctrlKey || e.metaKey, range: e.shiftKey });
  });

  const grip = document.createElement('span');
  grip.className = 'cleanup-member-grip';
  grip.innerHTML = '<i class="fa-solid fa-grip-vertical"></i>';
  row.appendChild(grip);

  const dot = document.createElement('span');
  dot.className = 'cleanup-member-dot';
  dot.style.background = member.color || (cluster && cluster.color) || '#6e7681';
  row.appendChild(dot);

  const main = document.createElement('span');
  main.className = 'cleanup-member-main';
  const key = document.createElement('span');
  key.className = 'cleanup-member-key';
  key.textContent = (member.name && member.name !== member.speaker_key) ? member.name : member.speaker_key;
  main.appendChild(key);
  const meta = document.createElement('span');
  meta.className = 'cleanup-member-meta';
  meta.textContent = `${member.segment_count} seg · ${_fmtTime(_cleanupTalkTime(member))}`;
  main.appendChild(meta);
  row.appendChild(main);

  // Play-all button (this is the button that replaced "mark as noise").
  const playBtn = document.createElement('button');
  playBtn.className = 'cleanup-play-btn';
  playBtn.dataset.segKey = `member:${member.speaker_key}`;
  playBtn.title = 'Play this speaker’s segments';
  playBtn.innerHTML = '<i class="fa-solid fa-play"></i>';
  playBtn.addEventListener('click', e => { e.stopPropagation(); _cleanupPlayMember(member, playBtn); });
  row.appendChild(playBtn);

  if (member.segments.length > 0) {
    const expandBtn = document.createElement('button');
    expandBtn.className = 'cleanup-member-expand-btn';
    expandBtn.title = expanded ? 'Hide segments' : 'Show segments';
    expandBtn.innerHTML = '<i class="fa-solid fa-chevron-down"></i>';
    expandBtn.addEventListener('click', e => {
      e.stopPropagation();
      if (_cleanupExpandedKeys.has(member.speaker_key)) _cleanupExpandedKeys.delete(member.speaker_key);
      else _cleanupExpandedKeys.add(member.speaker_key);
      const nowExp = _cleanupExpandedKeys.has(member.speaker_key);
      pill.classList.toggle('expanded', nowExp);
      expandBtn.title = nowExp ? 'Hide segments' : 'Show segments';
    });
    row.appendChild(expandBtn);
  }

  pill.appendChild(row);

  // ── Expandable detail: mini-timeline + transcript-backed segment list ──
  if (member.segments.length > 0) {
    const detail = document.createElement('div');
    detail.className = 'cleanup-member-detail';
    const inner = document.createElement('div');
    inner.className = 'cleanup-member-detail-inner';
    inner.appendChild(_cleanupBuildTimeline(member));
    const list = document.createElement('div');
    list.className = 'cleanup-seg-list';
    const ranked = [...member.segments]
      .sort((a, b) => (b.end - b.start) - (a.end - a.start))
      .slice(0, 8);
    ranked.forEach(seg => list.appendChild(_cleanupBuildSegRow(seg, member)));
    if (member.segments.length > ranked.length) {
      const more = document.createElement('div');
      more.className = 'cleanup-seg-more';
      more.textContent = `+${member.segments.length - ranked.length} more`;
      list.appendChild(more);
    }
    inner.appendChild(list);
    detail.appendChild(inner);
    pill.appendChild(detail);
  }

  return pill;
}

function _cleanupPlayMember(member, btn) {
  if (!member.segments || !member.segments.length) return;
  _cleanupPlayQueue(member.segments, btn, { key: `member:${member.speaker_key}` });
}

function _fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/* ── Sequential segment player ─────────────────────────────────────────────
 * Plays one or more segments back-to-back on the shared playback-audio and
 * mirrors each onto the floating video popup when it's open. Re-invoking with
 * the same `key` toggles off; starting a new queue stops the previous one. */

function _cleanupClearPlayingButtons() {
  document.querySelectorAll('.cleanup-play-btn.playing, .cleanup-seg-play.playing, .cleanup-selbar-btn.playing')
    .forEach(b => b.classList.remove('playing'));
}

function _cleanupStopPlayback() {
  const audio = document.getElementById('playback-audio');
  if (_cleanupPlayQueueState && _cleanupPlayQueueState.timer) clearTimeout(_cleanupPlayQueueState.timer);
  _cleanupPlayQueueState = null;
  if (audio) {
    if (audio._cleanupStopAt) { audio.removeEventListener('timeupdate', audio._cleanupStopAt); audio._cleanupStopAt = null; }
    try { audio.pause(); } catch (_) {}
    audio.dataset.cleanupActive = '';
  }
  _cleanupClearPlayingButtons();
  _cvStopPreview();   // park the floating video popup
}

// Light every button mapped to the active queue key (segment + member buttons
// share data-seg-key values so the right control glows).
function _cleanupSyncPlayButtons(key) {
  _cleanupClearPlayingButtons();
  if (!key) return;
  document.querySelectorAll('[data-seg-key]').forEach(b => {
    if (b.dataset.segKey === key) b.classList.add('playing');
  });
}

function _cleanupPlayQueue(segs, btn, opts) {
  opts = opts || {};
  const audio = document.getElementById('playback-audio');
  if (!audio || !_cleanupState || !segs || !segs.length) return;
  const key = opts.key || segs.map(s => s.id).join(',');
  // Toggle off if this exact queue is already playing.
  if (_cleanupPlayQueueState && _cleanupPlayQueueState.key === key) {
    _cleanupStopPlayback();
    return;
  }
  _cleanupStopPlayback();
  stopSpeakerVoice();   // only one thing is ever audible in this modal
  const ordered = [...segs].sort((a, b) => a.start - b.start);
  _cleanupPlayQueueState = { btn: btn || null, segs: ordered, idx: 0, key, timer: 0 };
  // Light matching data-seg-key controls first, then the explicit button (which
  // may have no data-seg-key, e.g. the selection-bar Play button).
  _cleanupSyncPlayButtons(key);
  if (btn) btn.classList.add('playing');
  const src = `/api/sessions/${_cleanupState.sessionId}/audio`;
  if (audio.src.indexOf(src) === -1) audio.src = src;
  const onErr = () => _cleanupStopPlayback();   // don't leave the button stuck on a 404/decode error
  const begin = () => { audio.removeEventListener('error', onErr); _cleanupPlayCurrent(); };
  if (isFinite(audio.duration) && audio.duration > 0) begin();
  else {
    audio.addEventListener('loadedmetadata', begin, { once: true });
    audio.addEventListener('error', onErr, { once: true });
    audio.load();
  }
}

function _cleanupPlayCurrent() {
  const st = _cleanupPlayQueueState;
  const audio = document.getElementById('playback-audio');
  if (!st || !audio) return;
  const seg = st.segs[st.idx];
  if (!seg) { _cleanupStopPlayback(); return; }
  if (audio._cleanupStopAt) { audio.removeEventListener('timeupdate', audio._cleanupStopAt); audio._cleanupStopAt = null; }
  if (st.timer) { clearTimeout(st.timer); st.timer = 0; }
  const start = seg.start, end = seg.end;
  // Set the active flag BEFORE driving the video so its sync loop matches.
  audio.dataset.cleanupActive = String(start);
  try { audio.currentTime = start; } catch (_) {}
  audio.play().catch(() => { _cleanupStopPlayback(); });

  const advance = () => {
    if (_cleanupPlayQueueState !== st) return;   // superseded by a newer queue
    if (audio._cleanupStopAt) { audio.removeEventListener('timeupdate', audio._cleanupStopAt); audio._cleanupStopAt = null; }
    if (st.timer) { clearTimeout(st.timer); st.timer = 0; }
    st.idx += 1;
    if (st.idx >= st.segs.length) _cleanupStopPlayback();
    else _cleanupPlayCurrent();
  };
  const stopAt = () => { if (audio.currentTime >= end) advance(); };
  audio._cleanupStopAt = stopAt;
  audio.addEventListener('timeupdate', stopAt);
  // Safety net in case 'timeupdate' stops firing (tab blur, decode stall).
  st.timer = setTimeout(advance, (end - start + 0.7) * 1000);

  // Mirror onto the video popup when it's open.
  const popup = _cleanupVideoPopupEl();
  if (popup && !popup.hidden && _cleanupVideoAvailable()) _cleanupVideoPlaySegment(seg);
}

function _cleanupFindMember(speakerKey) {
  for (const c of _cleanupState.clusters) {
    const idx = c.members.findIndex(m => m.speaker_key === speakerKey);
    if (idx >= 0) return { cluster: c, idx, member: c.members[idx] };
  }
  return null;
}

function _cleanupGarbageCollectClusters() {
  // Drop unlabeled clusters that are empty AND weren't originally labeled -
  // labeled clusters with zero members are still meaningful (they signal
  // "no longer assign anyone to this profile in this session").
  _cleanupState.clusters = _cleanupState.clusters.filter(c => {
    if (c.members.length > 0) return true;
    if (c.kind === 'labeled') return true;  // keep so we can show "unassigned profile" affordance
    return false;
  });
}

function _cleanupBlankCluster(members, tag) {
  return {
    cluster_id: `unlabeled:${tag}:${Date.now()}:${Math.random().toString(36).slice(2, 6)}`,
    kind: 'unlabeled', global_id: null, new_name: '', name: '', color: null,
    members: members || [], suggestion: null, _dropped_suggestions: new Set(),
  };
}

// Pull a member out of wherever it lives (cluster or noise bucket) and return
// it, clearing the noise flag. Returns null if the key is unknown.
function _cleanupDetachMember(key) {
  const found = _cleanupFindMember(key);
  let member = null;
  if (found) { [member] = found.cluster.members.splice(found.idx, 1); }
  else if (_cleanupState.noiseMembers.has(key)) {
    member = _cleanupState.noiseMembers.get(key);
    _cleanupState.noiseMembers.delete(key);
  }
  _cleanupState.noiseKeys.delete(key);
  return member;
}

function _cleanupNextColor() {
  const used = new Set(_cleanupState.clusters.map(c => c.color).filter(Boolean));
  for (const col of _SPEAKER_PALETTE) if (!used.has(col)) return col;
  return _SPEAKER_PALETTE[Math.floor(Math.random() * _SPEAKER_PALETTE.length)];
}

// ── Bulk moves (multi-select aware) ────────────────────────────────────────

function _cleanupMoveKeysToCluster(keys, destClusterId) {
  const dest = _cleanupState.clusters.find(c => c.cluster_id === destClusterId);
  if (!dest) return;
  let moved = 0;
  keys.forEach(k => {
    const found = _cleanupFindMember(k);
    if (found && found.cluster.cluster_id === destClusterId) return;  // already here
    const member = _cleanupDetachMember(k);
    if (member) { dest.members.push(member); moved++; }
  });
  if (!moved) return;
  _cleanupGarbageCollectClusters();
  _cleanupClearSelection();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

function _cleanupMoveKeysToNewCluster(keys) {
  const members = keys.map(k => _cleanupDetachMember(k)).filter(Boolean);
  if (!members.length) return;
  _cleanupState.clusters.push(_cleanupBlankCluster(members, 'new'));
  _cleanupGarbageCollectClusters();
  _cleanupClearSelection();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

function _cleanupMarkKeysNoise(keys) {
  let changed = 0;
  keys.forEach(k => {
    if (_cleanupState.noiseKeys.has(k)) return;
    const found = _cleanupFindMember(k);
    if (found) {
      const [member] = found.cluster.members.splice(found.idx, 1);
      _cleanupState.noiseMembers.set(k, member);
    }
    _cleanupState.noiseKeys.add(k);
    changed++;
  });
  if (!changed) return;
  _cleanupGarbageCollectClusters();
  _cleanupClearSelection();
  _cleanupNoiseExpanded = true;
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

function _cleanupRestoreKeys(keys) {
  const restored = [];
  keys.forEach(k => {
    if (!_cleanupState.noiseKeys.has(k)) return;
    const m = _cleanupDetachMember(k);
    if (m) restored.push(m);
  });
  if (!restored.length) return;
  // Each restored speaker starts in its own group (preserves prior behaviour).
  restored.forEach(m => _cleanupState.clusters.push(_cleanupBlankCluster([m], 'restored')));
  _cleanupClearSelection();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

// ── Selection-bar actions ───────────────────────────────────────────────────

function _cleanupSelectionNewCluster() {
  if (_cleanupSelectedKeys.size) _cleanupMoveKeysToNewCluster(Array.from(_cleanupSelectedKeys));
}

function _cleanupSelectionToggleNoise() {
  const keys = Array.from(_cleanupSelectedKeys);
  if (!keys.length) return;
  if (keys.every(k => _cleanupState.noiseKeys.has(k))) _cleanupRestoreKeys(keys);
  else _cleanupMarkKeysNoise(keys);
}

function _cleanupSelectionPlay() {
  const keys = Array.from(_cleanupSelectedKeys);
  if (!keys.length) return;
  const segs = [];
  keys.forEach(k => { const m = _cleanupGetMember(k); if (m) segs.push(...m.segments); });
  if (!segs.length) return;
  const btn = document.getElementById('cleanup-selbar-play');
  _cleanupPlayQueue(segs, btn, { key: `sel:${keys.slice().sort().join(',')}` });
}

function _cleanupSelectionAssign(e) {
  if (e) e.stopPropagation();
  if (!_cleanupSelectedKeys.size) return;
  _cleanupOpenPicker(document.getElementById('cleanup-selbar-assign'), { keys: Array.from(_cleanupSelectedKeys) });
}

// ── Assignment (cluster + key-set targets) ──────────────────────────────────

function _cleanupAssignClusterToProfile(cluster, profile) {
  const existing = _cleanupState.clusters.find(c => c !== cluster && c.global_id === profile.global_id);
  if (existing) {
    existing.members.push(...cluster.members);
    cluster.members = [];
    _cleanupGarbageCollectClusters();
  } else {
    cluster.global_id = profile.global_id;
    cluster.name = profile.name;
    cluster.color = profile.color;
    cluster.kind = 'labeled';
    cluster.new_name = '';
    cluster._dropped_suggestions = new Set();
  }
  _cleanupClosePicker();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

function _cleanupAssignClusterToNewName(cluster, name, color) {
  cluster.global_id = null;
  cluster.kind = 'unlabeled';
  cluster.new_name = name;
  cluster.name = name;
  cluster.color = color || cluster.color || _cleanupNextColor();
  _cleanupClosePicker();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

function _cleanupUnassignCluster(cluster) {
  cluster.global_id = null;
  cluster.kind = 'unlabeled';
  cluster.name = '';
  cluster.new_name = '';
  cluster.color = null;
  cluster._dropped_suggestions = new Set();
  _cleanupClosePicker();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

function _cleanupAssignKeysToProfile(keys, profile) {
  const members = keys.map(k => _cleanupDetachMember(k)).filter(Boolean);
  if (!members.length) { _cleanupClosePicker(); return; }
  let target = _cleanupState.clusters.find(c => c.global_id === profile.global_id);
  if (!target) {
    target = _cleanupBlankCluster([], 'profile');
    target.global_id = profile.global_id;
    target.name = profile.name;
    target.color = profile.color;
    target.kind = 'labeled';
    _cleanupState.clusters.push(target);
  }
  target.members.push(...members);
  _cleanupGarbageCollectClusters();
  _cleanupClosePicker();
  _cleanupClearSelection();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

function _cleanupAssignKeysToNewName(keys, name, color) {
  const members = keys.map(k => _cleanupDetachMember(k)).filter(Boolean);
  if (!members.length) { _cleanupClosePicker(); return; }
  const cl = _cleanupBlankCluster(members, 'new');
  cl.new_name = name;
  cl.name = name;
  cl.color = color || _cleanupNextColor();
  _cleanupState.clusters.push(cl);
  _cleanupGarbageCollectClusters();
  _cleanupClosePicker();
  _cleanupClearSelection();
  _cleanupMarkDirty();
  renderSpeakerClusters();
}

// ── Voice-library picker popover ────────────────────────────────────────────

function _cleanupCombinedCentroid(keys) {
  let sum = null, totalW = 0;
  keys.forEach(k => {
    const m = _cleanupGetMember(k);
    if (!m || !m.centroid) return;
    const w = Math.max(m.emb_count, 1);
    if (!sum) sum = new Float32Array(m.centroid.length);
    for (let i = 0; i < sum.length; i++) sum[i] += m.centroid[i] * w;
    totalW += w;
  });
  if (!sum || !totalW) return null;
  let norm = 0;
  for (let i = 0; i < sum.length; i++) norm += sum[i] * sum[i];
  norm = Math.sqrt(norm) || 1;
  for (let i = 0; i < sum.length; i++) sum[i] /= norm;
  return sum;
}

function _cleanupRankLibrary(centroid) {
  const out = _cleanupState.library.map(g => {
    let sim = null;
    if (centroid && g.centroid) {
      sim = 0;
      for (let i = 0; i < centroid.length; i++) sim += centroid[i] * g.centroid[i];
    }
    return { global_id: g.global_id, name: g.name, color: g.color, emb_count: g.emb_count, similarity: sim };
  });
  out.sort((a, b) => {
    if (a.similarity == null && b.similarity == null) return a.name.localeCompare(b.name);
    if (a.similarity == null) return 1;
    if (b.similarity == null) return -1;
    return b.similarity - a.similarity;
  });
  return out;
}

function _cleanupClosePicker() {
  if (_cleanupPicker) { _cleanupPicker.remove(); _cleanupPicker = null; }
  document.removeEventListener('mousedown', _cleanupPickerOutside, true);
  document.removeEventListener('keydown', _cleanupPickerKey, true);
}
function _cleanupPickerOutside(e) {
  if (_cleanupPicker && !_cleanupPicker.contains(e.target)) _cleanupClosePicker();
}
function _cleanupPickerKey(e) {
  // stopPropagation matters: without it the modal's own Escape handler also
  // fires and closes the whole dialog behind the picker. Same contract as
  // ui-combobox.js, where Escape only dismisses the list.
  if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); _cleanupClosePicker(); }
}

function _cleanupOpenPicker(anchorEl, target) {
  _cleanupClosePicker();
  const cluster = target.cluster || null;
  const keys = target.keys || (cluster ? cluster.members.map(m => m.speaker_key) : []);
  let centroid;
  if (cluster) { _cleanupRecomputeClusterCentroid(cluster); centroid = cluster._centroid; }
  else centroid = _cleanupCombinedCentroid(keys);
  const currentGid = cluster ? cluster.global_id : null;

  const pop = document.createElement('div');
  pop.className = 'cleanup-picker';

  const search = document.createElement('input');
  search.className = 'cleanup-picker-search';
  search.type = 'text';
  search.placeholder = 'Search profiles or type a new name…';
  pop.appendChild(search);

  // No embeddings → no similarity ranking. Say so rather than silently dropping
  // the percentages, so the user knows why matches aren't ranked.
  if (!centroid) {
    const sub = document.createElement('div');
    sub.className = 'cleanup-picker-subtitle';
    sub.innerHTML = '<i class="fa-solid fa-circle-info"></i> No voice samples here: profiles aren’t ranked by similarity.';
    pop.appendChild(sub);
  }

  // What the calendar knows about this meeting: the attendees, and any agent
  // hint aimed at one of the keys being assigned.
  const cal = (_cleanupState && _cleanupState.calendar) || { candidates: [], hints: [] };
  const hint = (cal.hints || []).find(h => h && h.guess && keys.includes(h.speaker_key)) || null;
  if (hint) {
    const sub = document.createElement('div');
    sub.className = 'cleanup-picker-subtitle';
    const conf = hint.confidence ? ` · ${escapeHtml(String(hint.confidence))} confidence` : '';
    sub.innerHTML = `<i class="fa-solid fa-wand-magic-sparkles"></i> Suggested from the invite: <b>${escapeHtml(hint.guess)}</b>${conf}`;
    if (hint.rationale) sub.title = hint.rationale;
    pop.appendChild(sub);
  }

  const listWrap = document.createElement('div');
  listWrap.className = 'cleanup-picker-list';
  pop.appendChild(listWrap);

  const choose = (chooser) => chooser();
  const render = () => {
    const q = search.value.trim();
    const ql = q.toLowerCase();
    listWrap.innerHTML = '';
    const ranked = _cleanupRankLibrary(centroid).filter(p => !ql || p.name.toLowerCase().includes(ql));
    const exact = ranked.find(p => p.name.toLowerCase() === ql);

    // Attendees first: the invite is the strongest evidence of who was in the
    // room. One that already has a Voice Library profile links to it; one that
    // does not becomes a new name, exactly as if it had been typed. The hinted
    // attendee, if any, leads.
    const matchesQ = c => !ql || c.name.toLowerCase().includes(ql)
      || String(c.title || c.role || '').toLowerCase().includes(ql)
      || String(c.lob || '').toLowerCase().includes(ql);
    const attendees = (cal.candidates || []).filter(matchesQ).sort((x, y) =>
      (hint && y.name === hint.guess ? 1 : 0) - (hint && x.name === hint.guess ? 1 : 0));
    const newColor = _cleanupNextColor();
    const groupHead = (icon, label) => {
      const head = document.createElement('div');
      head.className = 'cleanup-picker-group';
      head.innerHTML = `<i class="fa-solid ${icon}" aria-hidden="true"></i> ${label}`;
      return head;
    };
    if (attendees.length) {
      listWrap.appendChild(groupHead('fa-calendar-days', 'On the calendar invite'));
      attendees.forEach(c => {
        const profile = _cleanupState.library.find(p => p.name.toLowerCase() === c.name.toLowerCase()) || null;
        const item = document.createElement('button');
        item.className = 'cleanup-picker-item calendar' + (profile && profile.global_id === currentGid ? ' current' : '');
        const context = [c.title || c.role, c.lob].filter(Boolean).join(' / ');
        item.innerHTML =
          `<span class="cleanup-picker-dot" style="background:${profile ? (profile.color || '#6e7681') : newColor}"></span>` +
          `<span class="nm">${escapeHtml(c.name)}${context ? `<span class="cleanup-picker-ctx">${escapeHtml(context)}</span>` : ''}</span>` +
          (profile
            ? `<span class="cleanup-picker-embs" title="voice samples on file">${profile.emb_count || 0}</span>`
            : `<span class="cleanup-picker-go"><i class="fa-solid fa-plus"></i> New</span>`);
        item.addEventListener('click', () => choose(() => profile
          ? _cleanupPickerChooseProfile(target, profile)
          : _cleanupPickerChooseNew(target, c.name)));
        listWrap.appendChild(item);
      });
      if (ranked.length || (q && !exact)) listWrap.appendChild(groupHead('fa-waveform-lines', 'Voice Library'));
    }
    if (q && !exact) {
      const create = document.createElement('button');
      create.className = 'cleanup-picker-item create';
      create.innerHTML =
        `<span class="cleanup-picker-dot" style="background:${_cleanupNextColor()}"></span>` +
        `<span class="nm">Create “${escapeHtml(q)}”</span>` +
        `<span class="cleanup-picker-go"><i class="fa-solid fa-plus"></i> New</span>`;
      create.addEventListener('click', () => choose(() => _cleanupPickerChooseNew(target, q)));
      listWrap.appendChild(create);
    }
    if (!ranked.length && !q) {
      const empty = document.createElement('div');
      empty.className = 'cleanup-picker-empty';
      empty.textContent = 'No saved voice profiles yet: type a name to create one.';
      listWrap.appendChild(empty);
    }
    ranked.forEach(p => {
      const item = document.createElement('button');
      item.className = 'cleanup-picker-item' + (p.global_id === currentGid ? ' current' : '');
      const simHtml = p.similarity != null
        ? `<span class="cleanup-picker-sim${p.similarity >= _cleanupState.thresholds.auto ? ' high' : (p.similarity >= _cleanupState.thresholds.suggest ? ' mid' : '')}">${Math.round(p.similarity * 100)}%</span>`
        : '';
      item.innerHTML =
        `<span class="cleanup-picker-dot" style="background:${p.color || '#6e7681'}"></span>` +
        `<span class="nm">${escapeHtml(p.name)}</span>` +
        `<span class="cleanup-picker-embs" title="voice samples on file">${p.emb_count || 0}</span>` +
        simHtml;
      item.addEventListener('click', () => choose(() => _cleanupPickerChooseProfile(target, p)));
      listWrap.appendChild(item);
    });
  };

  if (cluster && cluster.global_id) {
    const foot = document.createElement('div');
    foot.className = 'cleanup-picker-foot';
    const un = document.createElement('button');
    un.className = 'cleanup-picker-unassign';
    un.innerHTML = '<i class="fa-solid fa-link-slash"></i> Unlink from profile';
    un.addEventListener('click', () => _cleanupUnassignCluster(cluster));
    foot.appendChild(un);
    pop.appendChild(foot);
  }

  search.addEventListener('input', render);
  search.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const first = listWrap.querySelector('.cleanup-picker-item');
      if (first) first.click();
    }
  });

  document.body.appendChild(pop);
  _cleanupPicker = pop;
  _cleanupPositionPicker(pop, anchorEl);
  render();
  setTimeout(() => search.focus(), 0);
  document.addEventListener('mousedown', _cleanupPickerOutside, true);
  document.addEventListener('keydown', _cleanupPickerKey, true);
}

function _cleanupPickerChooseProfile(target, profile) {
  if (target.cluster) _cleanupAssignClusterToProfile(target.cluster, profile);
  else _cleanupAssignKeysToProfile(target.keys, profile);
}
function _cleanupPickerChooseNew(target, name) {
  const color = _cleanupNextColor();
  if (target.cluster) _cleanupAssignClusterToNewName(target.cluster, name, color);
  else _cleanupAssignKeysToNewName(target.keys, name, color);
}

function _cleanupPositionPicker(pop, anchorEl) {
  const r = anchorEl ? anchorEl.getBoundingClientRect()
                     : { left: window.innerWidth / 2 - 140, right: 0, top: 120, bottom: 120 };
  const pw = pop.offsetWidth || 300;
  const ph = pop.offsetHeight || 340;
  let left = r.left;
  let top = r.bottom + 6;
  if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
  if (left < 8) left = 8;
  if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

// ── Similarity heatmap ──────────────────────────────────────────────────────

function _cleanupTrunc(s, n) { return s.length > n ? s.slice(0, n - 1) + '…' : s; }

function _cleanupHeatColor(s) {
  // Map similarity 0.4..1.0 onto a dim→hot ramp; below 0.4 stays cool.
  const t = Math.max(0, Math.min(1, (s - 0.4) / 0.6));
  const r = Math.round(40 + t * 200);
  const g = Math.round(55 + t * 70);
  const b = Math.round(85 + (1 - t) * 95);
  const a = 0.16 + t * 0.72;
  return `rgba(${r},${g},${b},${a})`;
}

function _cleanupHeatCell(cls) {
  const d = document.createElement('div');
  d.className = `cleanup-heat-${cls}`;
  return d;
}

function _cleanupRenderHeatmap(wrap) {
  wrap.innerHTML = '';
  const entities = _cleanupState.clusters
    .filter(c => c.members.length)
    .map(c => {
      _cleanupRecomputeClusterCentroid(c);
      const label = c.kind === 'labeled' ? (c.name || '(unnamed)')
                                         : (c.new_name || c.members[0].speaker_key);
      const color = c.color || (c.members[0] && c.members[0].color) || '#6e7681';
      return { cluster: c, label, color, centroid: c._centroid };
    })
    .filter(e => e.centroid);

  const head = document.createElement('div');
  head.className = 'cleanup-heatmap-head';
  head.innerHTML = '<i class="fa-solid fa-circle-info"></i> Voice similarity between groups. Hot off-diagonal cells are likely the same person: click one to select both groups and merge them.';
  wrap.appendChild(head);

  if (entities.length < 2) {
    const note = document.createElement('div');
    note.className = 'cleanup-help';
    note.textContent = 'Need at least two groups with voice embeddings to compare.';
    wrap.appendChild(note);
    return;
  }

  const n = entities.length;
  const dot = (a, b) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * b[i]; return s; };

  const table = document.createElement('div');
  table.className = 'cleanup-heatmap';
  table.style.setProperty('--n', n);

  table.appendChild(_cleanupHeatCell('corner'));
  entities.forEach(e => {
    const c = _cleanupHeatCell('colhead');
    c.innerHTML = `<span class="cleanup-heat-collabel" title="${escapeHtml(e.label)}">${escapeHtml(_cleanupTrunc(e.label, 12))}</span>`;
    c.style.setProperty('--c', e.color);
    table.appendChild(c);
  });

  entities.forEach((er, i) => {
    const rh = _cleanupHeatCell('rowhead');
    rh.innerHTML = `<span class="cleanup-heat-dot" style="background:${er.color}"></span><span class="cleanup-heat-rowlabel" title="${escapeHtml(er.label)}">${escapeHtml(_cleanupTrunc(er.label, 16))}</span>`;
    table.appendChild(rh);
    entities.forEach((ec, j) => {
      const s = i === j ? 1 : dot(er.centroid, ec.centroid);
      const cell = _cleanupHeatCell('cell');
      cell.style.background = i === j ? 'var(--surface3)' : _cleanupHeatColor(s);
      cell.textContent = i === j ? '·' : String(Math.round(s * 100));
      cell.title = `${er.label} ↔ ${ec.label}: ${(s * 100).toFixed(0)}%`;
      if (i !== j) {
        cell.classList.add('clickable');
        if (s >= _cleanupState.thresholds.cluster) cell.classList.add('hot');
        cell.addEventListener('click', () => {
          _cleanupShowHeatmap = false;
          _cleanupSelectedKeys = new Set([
            ...er.cluster.members.map(m => m.speaker_key),
            ...ec.cluster.members.map(m => m.speaker_key),
          ]);
          _cleanupSelAnchor = null;
          renderSpeakerClusters();
          const first = document.querySelector('#cleanup-grid .cleanup-member.selected');
          if (first) first.scrollIntoView({ block: 'center', behavior: 'smooth' });
        });
      }
      table.appendChild(cell);
    });
  });
  wrap.appendChild(table);
}

function toggleCleanupHeatmap() {
  _cleanupShowHeatmap = !_cleanupShowHeatmap;
  _cleanupClosePicker();
  renderSpeakerClusters();
}

// ── Drag-autoscroll (so you can drag across a tall widget) ───────────────────

let _cleanupAutoscrollRAF = 0;
let _cleanupAutoscrollDir = 0;

function _cleanupWireGridAutoscroll() {
  const scroll = document.getElementById('cleanup-scroll');
  if (!scroll || scroll._autoscrollWired) return;
  scroll._autoscrollWired = true;
  scroll.addEventListener('dragover', e => {
    if (!_cleanupDragKeys.length) { _cleanupStopAutoscroll(); return; }
    const r = scroll.getBoundingClientRect();
    const edge = 56;
    if (e.clientY < r.top + edge) _cleanupAutoscrollDir = -1;
    else if (e.clientY > r.bottom - edge) _cleanupAutoscrollDir = 1;
    else _cleanupAutoscrollDir = 0;
    if (_cleanupAutoscrollDir && !_cleanupAutoscrollRAF) _cleanupAutoscrollStep();
  });
  scroll.addEventListener('drop', _cleanupStopAutoscroll);
}

function _cleanupAutoscrollStep() {
  const scroll = document.getElementById('cleanup-scroll');
  if (!scroll || !_cleanupDragKeys.length || !_cleanupAutoscrollDir) { _cleanupAutoscrollRAF = 0; return; }
  scroll.scrollTop += _cleanupAutoscrollDir * 16;
  _cleanupAutoscrollRAF = requestAnimationFrame(_cleanupAutoscrollStep);
}

function _cleanupStopAutoscroll() {
  _cleanupAutoscrollDir = 0;
  if (_cleanupAutoscrollRAF) { cancelAnimationFrame(_cleanupAutoscrollRAF); _cleanupAutoscrollRAF = 0; }
}

function toggleCleanupNoiseExpanded() {
  _cleanupNoiseExpanded = !_cleanupNoiseExpanded;
  renderSpeakerClusters();
}

function applyConfidentCleanupMatches() {
  if (!_cleanupState) return;
  const auto = _cleanupState.thresholds.auto;
  let applied = 0;
  let guard = 0;
  // Re-evaluate suggestions between assignments so a profile that just got
  // claimed is excluded from the next cluster's candidates (no duplicates).
  while (guard++ < 200) {
    _cleanupState.clusters.forEach(_cleanupUpdateSuggestion);
    const c = _cleanupState.clusters.find(
      x => x.kind === 'unlabeled' && !x.global_id && x.members.length &&
           x.suggestion && x.suggestion.similarity >= auto,
    );
    if (!c) break;
    const profile = c.suggestion;
    const existing = _cleanupState.clusters.find(x => x !== c && x.global_id === profile.global_id);
    if (existing) {
      existing.members.push(...c.members);
      c.members = [];
      _cleanupGarbageCollectClusters();
    } else {
      c.global_id = profile.global_id;
      c.name = profile.name;
      c.color = profile.color;
      c.kind = 'labeled';
      c.new_name = '';
    }
    applied++;
  }
  if (applied) {
    _cleanupMarkDirty();
    renderSpeakerClusters();
  }
}

async function resetSpeakerCleanup() {
  const pending = Math.max(_cleanupPendingChangeCount(), 1);
  if (!await uiConfirm({
    title: 'Discard staged cleanup changes?',
    message: `${_plural(pending, 'staged change', 'staged changes')} in this meeting will be thrown away and the groups reloaded from disk. Nothing that was already applied is affected.`,
    confirmLabel: 'Discard changes', danger: true,
  })) return;
  loadSpeakerClusters(true);
}

async function applySpeakerCleanup() {
  if (!_cleanupState || !_cleanupState.dirty) return;
  const sid = _cleanupState.sessionId;
  const snap = _cleanupState.originalSnapshot || {};
  const visibleKeys = c => c.members
    .filter(m => !_cleanupState.noiseKeys.has(m.speaker_key))
    .map(m => m.speaker_key);

  // Guard: a multi-speaker group with no name (and no library profile) can't be
  // persisted as a merge - the backend keys identity on a profile, so an unnamed
  // group would be silently split back into individual speakers. Make the user
  // name it (or assign a profile) instead of dropping their grouping.
  const unnamedMerges = _cleanupState.clusters.filter(
    c => !c.global_id && !(c.new_name || '').trim() && visibleKeys(c).length > 1,
  );
  if (unnamedMerges.length) {
    await uiAlert({
      title: 'Name grouped speakers',
      message: 'A merged group needs a name (or an existing voice profile) to be saved. Use Link on each group to name it.',
      details: unnamedMerges.map(c => visibleKeys(c).join(' + ')),
      kind: 'warn',
    });
    return;
  }

  const applyBtn = document.getElementById('cleanup-apply-btn');
  if (applyBtn) { applyBtn.dataset.busy = '1'; applyBtn.disabled = true; applyBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Applying…'; }

  // Only unlabeled/unnamed members that were ORIGINALLY linked to a profile or
  // marked noise need to be sent (so the backend unlinks / un-noises them).
  // Members that were already plain "Speaker N" are left untouched - sending
  // them would reset a manual label and churn the DB for no reason.
  const wasProfileLinked = k => {
    const o = snap[k];
    return !!(o && typeof o.cluster_id === 'string' && o.cluster_id.indexOf('profile:') === 0);
  };
  const wasNoise = k => { const o = snap[k]; return !!(o && o.is_noise); };

  const proposed = [];
  for (const c of _cleanupState.clusters) {
    const visible = visibleKeys(c);
    if (c.global_id) {
      // Existing profile → relink every member to it.
      proposed.push({ global_id: c.global_id, new_name: null, color: c.color || null, member_keys: visible });
    } else if ((c.new_name || '').trim()) {
      // New profile from a typed name.
      proposed.push({ global_id: null, new_name: c.new_name.trim(), color: c.color || null, member_keys: visible });
    } else {
      // Unlabeled + unnamed → send only the members that need a DB change.
      const toReset = visible.filter(k => wasProfileLinked(k) || wasNoise(k));
      if (toReset.length) {
        proposed.push({ global_id: null, new_name: null, color: null, member_keys: toReset });
      }
    }
  }
  const noise_keys = Array.from(_cleanupState.noiseKeys);

  try {
    const resp = await fetch(`/api/sessions/${sid}/speaker_clusters/apply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clusters: proposed, noise_keys }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      uiToast({ message: `Apply failed: ${data.error || resp.status}`, kind: 'error' });
      if (applyBtn) { delete applyBtn.dataset.busy; }
      _cleanupSyncFooter();
      return;
    }
    // Refresh from server to pick up canonical names/links + show the new state.
    _cleanupState = null;
    if (applyBtn) delete applyBtn.dataset.busy;
    await loadSpeakerClusters(true);
    _cleanupSyncFooter();
    onSpeakerDataChanged();
    uiToast({ message: 'Cleanup applied.', kind: 'success', id: 'cleanup-apply' });
    // Refresh transcript / sidebar speaker pills so they reflect new labels.
    try {
      if (typeof loadSession === 'function' && state.sessionId) await loadSession(state.sessionId);
    } catch (_) {}
    try {
      if (typeof _tnRefreshSpeakerPills === 'function') _tnRefreshSpeakerPills();
    } catch (_) {}
  } catch (e) {
    uiToast({ message: `Apply failed: ${e.message}`, kind: 'error' });
    if (applyBtn) { delete applyBtn.dataset.busy; }
    _cleanupSyncFooter();
  }
}

/* ── Cleanup video popup ────────────────────────────────────────────────────
 * Independent draggable mini-player that floats above the speaker modal.
 * Reuses the session's /api/sessions/{sid}/video endpoint but keeps a
 * dedicated <video> element so we don't fight with the main video viewer.
 * ─────────────────────────────────────────────────────────────────────────── */

let _cleanupVideoLoadedFor = null;  // sessionId the popup's <video> is bound to
let _cleanupVideoUserClosed = false;  // user explicitly closed → don't auto-reopen this session
let _cleanupVideoPlayingFor = null; // segment id currently driving playback

// ── Cleanup floating-player sync state ──────────────────────────────────────
// The popup's muted <video> is slaved to the SAME master audio (playback-audio)
// that the cleanup segment preview drives, using the same soft-sync rules as
// the main viewer: rate nudge for drift, guarded + throttled seeks with a
// watchdog, and never play() while a seek is in flight. The audio's
// cleanupActive flag alone bounds the segment, so the two stay locked and the
// video can't stutter or replay a snippet on its own.
let _cvPreviewing   = false; // a segment preview currently owns the popup video
let _cvSegStartKey  = '';    // String(seg.start); matches audio.dataset.cleanupActive
let _cvSeekPending  = false; // between currentTime= and its 'seeked'
let _cvSeekWatchdog = 0;     // watchdog timer id for a stuck pending seek
let _cvRAF          = 0;     // requestAnimationFrame id for the sync loop
let _cvLastSeekAt   = 0;     // perf clock of the last throttled seek

function _cleanupVideoEl() { return document.getElementById('cleanup-video'); }
function _cleanupVideoPopupEl() { return document.getElementById('cleanup-video-popup'); }

function _cleanupVideoAvailable() {
  return typeof _videoAvailable !== 'undefined' && _videoAvailable && !!state.sessionId;
}

function _cleanupVideoEnsureLoaded() {
  const video = _cleanupVideoEl();
  if (!video || !_cleanupVideoAvailable()) return false;
  if (_cleanupVideoLoadedFor !== state.sessionId) {
    // Buffer ahead so seeking between segments rarely stalls. Same-origin, same
    // URL as the main viewer, so the browser cache largely shares the bytes.
    video.preload = 'auto';
    video.src = `/api/sessions/${state.sessionId}/video`;
    video.load();
    _cleanupVideoLoadedFor = state.sessionId;
    // Stable named handlers, so re-adding on a later session swap is a no-op.
    video.addEventListener('timeupdate', _cleanupVideoUpdateTime);
    video.addEventListener('seeked', _cvOnSeeked);
    video.addEventListener('error', _cvOnError);
  }
  return true;
}

function _cleanupVideoUpdateTime() {
  const video = _cleanupVideoEl();
  const lbl = document.getElementById('cleanup-video-time');
  if (!video || !lbl) return;
  const t = video.currentTime + (typeof _videoOffset === 'number' ? _videoOffset : 0);
  lbl.textContent = _fmtTime(t);
}

function _cleanupVideoApplySavedPosition() {
  const popup = _cleanupVideoPopupEl();
  if (!popup) return;
  const popupW = popup.offsetWidth  || 360;
  const popupH = popup.offsetHeight || 240;
  const pos = (typeof _prefs !== 'undefined' && _prefs.cleanup_video_pos) || null;
  if (pos && typeof pos === 'object'
      && Number.isFinite(pos.left) && Number.isFinite(pos.top)) {
    // Clamp the restored geometry into the *current* viewport. A position
    // saved on a larger monitor (or before the window shrank) used to land
    // off-screen, so the popup "opened" but was never visible - the reported
    // bug. Clamp size first, then the top-left, so it's always reachable.
    let w = Math.min(Number.isFinite(pos.width)  ? pos.width  : popupW, window.innerWidth  - 16);
    let h = Math.min(Number.isFinite(pos.height) ? pos.height : popupH, window.innerHeight - 16);
    const left = Math.max(8, Math.min(pos.left, window.innerWidth  - w - 8));
    const top  = Math.max(8, Math.min(pos.top,  window.innerHeight - h - 8));
    popup.style.width  = `${w}px`;
    popup.style.height = `${h}px`;
    popup.style.left   = `${left}px`;
    popup.style.top    = `${top}px`;
    popup.style.right  = 'auto';
    return;
  }
  // No saved position - anchor to the LEFT of the speaker manager dialog so
  // the user can see both panes at once. Fall back to the right side, then to
  // an explicit top-right corner.
  const dialog = document.querySelector('#speaker-manager-overlay .speaker-manager-dialog');
  if (dialog) {
    const dr = dialog.getBoundingClientRect();
    const gap = 16;
    const desiredLeft = dr.left - popupW - gap;
    if (desiredLeft >= 8) {
      popup.style.left  = `${desiredLeft}px`;
      popup.style.top   = `${Math.max(8, dr.top)}px`;
      popup.style.right = 'auto';
      return;
    }
    const rightLeft = dr.right + gap;
    if (rightLeft + popupW <= window.innerWidth - 8) {
      popup.style.left  = `${rightLeft}px`;
      popup.style.top   = `${Math.max(8, dr.top)}px`;
      popup.style.right = 'auto';
      return;
    }
  }
  // Default: explicit top-right (don't rely on the CSS rule alone - a prior
  // inline left/top could otherwise leave it parked off-screen).
  popup.style.left  = 'auto';
  popup.style.right = '24px';
  popup.style.top   = '80px';
}

function _cleanupVideoSavePosition() {
  const popup = _cleanupVideoPopupEl();
  if (!popup) return;
  const r = popup.getBoundingClientRect();
  if (typeof savePref === 'function') {
    savePref('cleanup_video_pos', {
      left: r.left, top: r.top, width: r.width, height: r.height,
    });
  }
}

function _cleanupVideoSyncToggleBtn() {
  const btn = document.getElementById('cleanup-video-toggle');
  if (!btn) return;
  const popup = _cleanupVideoPopupEl();
  const shown = popup && !popup.hidden;
  btn.classList.toggle('active', !!shown);
  btn.disabled = !_cleanupVideoAvailable();
  if (!_cleanupVideoAvailable()) {
    btn.title = 'No screen recording for this session';
  } else {
    btn.title = shown ? 'Hide recording preview' : 'Show recording preview';
  }
}

function showCleanupVideoPopup() {
  if (!_cleanupVideoAvailable()) {
    if (typeof flashStatus === 'function') flashStatus('No screen recording for this session');
    return;
  }
  const popup = _cleanupVideoPopupEl();
  if (!popup) return;
  if (!_cleanupVideoEnsureLoaded()) return;
  popup.hidden = false;  // unhide first so offsetWidth/Height read correctly
  _cleanupVideoApplySavedPosition();
  _cleanupVideoEnsureDragWired();
  _cleanupVideoUserClosed = false;
  if (typeof savePref === 'function') savePref('cleanup_video_open', true);
  _cleanupVideoSyncToggleBtn();
}

function closeCleanupVideoPopup() {
  const popup = _cleanupVideoPopupEl();
  if (!popup) return;
  popup.hidden = true;
  _cvStopPreview();
  _cvResetZoom();
  _cleanupVideoUserClosed = true;
  if (typeof savePref === 'function') savePref('cleanup_video_open', false);
  _cleanupVideoSyncToggleBtn();
}

function toggleCleanupVideoPopup() {
  const popup = _cleanupVideoPopupEl();
  if (!popup) return;
  if (popup.hidden) showCleanupVideoPopup();
  else closeCleanupVideoPopup();
}

function _cvVideoTime(audioTime) {
  const off = typeof _videoOffset === 'number' ? _videoOffset : 0;
  return audioTime - off;
}

function _cvClampTarget(t) {
  const v = _cleanupVideoEl();
  t = Math.max(0, t);
  const d = v ? v.duration : NaN;
  if (isFinite(d) && d > 0) t = Math.min(t, d - 0.05);
  return t;
}

function _cvOnSeeked() {
  clearTimeout(_cvSeekWatchdog);
  _cvSeekWatchdog = 0;
  _cvSeekPending = false;
  _cvAfterSeek();
}
function _cvOnError() {
  clearTimeout(_cvSeekWatchdog);
  _cvSeekWatchdog = 0;
  _cvSeekPending = false;
}

// Resume the popup video once a seek has landed (real 'seeked', a no-op seek,
// or the watchdog) - but only if the preview is still live and audio is playing.
function _cvAfterSeek() {
  const v = _cleanupVideoEl();
  const audio = document.getElementById('playback-audio');
  if (!v || !audio) return;
  if (_cvPreviewing && !audio.paused && v.paused) v.play().catch(() => {});
}

// Guarded seek for the popup video. `force` bypasses the anti-spam throttle
// (use for the initial segment seek); drift seeks pass force=false so they can
// never machine-gun the decoder. Reuses the main controller's _VS tolerances.
function _cvHardSeek(target, force) {
  const v = _cleanupVideoEl();
  if (!v) return false;
  target = _cvClampTarget(target);
  const now = _vsNow();
  if (!force && (now - _cvLastSeekAt) < _VS.SEEK_MIN_MS) return false;
  if (Math.abs(v.currentTime - target) < _VS.NOOP) { _cvAfterSeek(); return true; }
  _cvLastSeekAt = now;
  if (Math.abs(v.playbackRate - 1) > 1e-3) v.playbackRate = 1;
  _cvSeekPending = true;
  clearTimeout(_cvSeekWatchdog);
  _cvSeekWatchdog = setTimeout(() => {
    _cvSeekWatchdog = 0; _cvSeekPending = false; _cvAfterSeek();
  }, _VS.WATCHDOG_MS);
  try {
    v.currentTime = target;
  } catch (_) {
    _cvSeekPending = false;
    clearTimeout(_cvSeekWatchdog);
    _cvSeekWatchdog = 0;
  }
  return true;
}

// Stop the current preview and park the popup video.
function _cvStopPreview() {
  _cvPreviewing = false;
  _cvSegStartKey = '';
  if (_cvRAF) { cancelAnimationFrame(_cvRAF); _cvRAF = 0; }
  clearTimeout(_cvSeekWatchdog);
  _cvSeekWatchdog = 0;
  _cvSeekPending = false;
  const v = _cleanupVideoEl();
  if (v) {
    try { v.pause(); } catch (_) {}
    if (Math.abs(v.playbackRate - 1) > 1e-3) v.playbackRate = 1;
  }
  // Restore the idle prompt so the popup never looks like a dead black box.
  const idle = document.getElementById('cleanup-video-idle');
  if (idle) idle.hidden = false;
}

// Per-frame sync: track the master audio clock. The audio's cleanupActive flag
// (set/cleared by the WAV preview) is the single source of truth for when the
// segment is over, so audio and video start and stop locked together.
function _cvSyncLoop() {
  _cvRAF = 0;
  if (!_cvPreviewing) return;
  const v = _cleanupVideoEl();
  const audio = document.getElementById('playback-audio');
  if (!v || !audio) { _cvStopPreview(); return; }
  const popup = _cleanupVideoPopupEl();
  const stillPreview = popup && !popup.hidden
    && audio.dataset.cleanupActive === _cvSegStartKey;
  if (!stillPreview) { _cvStopPreview(); return; }  // ended / toggled off / switched

  if (!_cvSeekPending) {
    const base = audio.playbackRate || 1;
    const expected = _cvClampTarget(_cvVideoTime(audio.currentTime));
    if (audio.paused) {
      // Mid-preview pause (e.g. still waiting on audio metadata): hold, but
      // keep the loop alive so we resume in lockstep when audio starts.
      if (!v.paused) v.pause();
    } else {
      const signed = v.currentTime - expected;     // + ahead of audio, - behind
      const adrift = Math.abs(signed);
      if (adrift >= _VS.HARD_DRIFT) {
        if (_cvHardSeek(expected, false)) { if (_cvRAF === 0) _cvRAF = requestAnimationFrame(_cvSyncLoop); return; }
      }
      if (v.paused) v.play().catch(() => {});
      let corr = 0;
      if (adrift > _VS.IN_SYNC) {
        corr = Math.max(-_VS.RATE_MAX,
                        Math.min(_VS.RATE_MAX, (-signed / _VS.HARD_DRIFT) * _VS.RATE_MAX));
      }
      const want = base * (1 + corr);
      if (Math.abs(v.playbackRate - want) > 1e-3) v.playbackRate = want;
    }
  }
  // Guard against a concurrent restart having already scheduled a frame.
  if (_cvRAF === 0) _cvRAF = requestAnimationFrame(_cvSyncLoop);
}

function _cleanupVideoPlaySegment(seg) {
  if (!_cleanupVideoEnsureLoaded()) return false;
  const popup = _cleanupVideoPopupEl();
  if (popup.hidden) showCleanupVideoPopup();
  const video = _cleanupVideoEl();
  const noseek = document.getElementById('cleanup-video-noseek');
  const idle = document.getElementById('cleanup-video-idle');
  const offset = typeof _videoOffset === 'number' ? _videoOffset : 0;
  const vStart = seg.start - offset;
  const vEnd = seg.end - offset;
  if (vEnd <= 0 || (isFinite(video.duration) && vStart >= video.duration)) {
    _cvStopPreview();              // parks video + re-shows idle prompt
    if (idle) idle.hidden = true;  // ...but the no-video notice takes over here
    if (noseek) noseek.hidden = false;
    return true;  // we handled it (even if we couldn't seek)
  }
  if (noseek) noseek.hidden = true;
  if (idle) idle.hidden = true;

  // Retire any legacy per-video stop handler still attached from older builds;
  // the audio clock bounds the segment now, so a video-time stop would fight it.
  if (video._cvStopAt) {
    video.removeEventListener('timeupdate', video._cvStopAt);
    video._cvStopAt = null;
  }

  _cvSegStartKey = String(seg.start);
  _cvPreviewing = true;
  _cvLastSeekAt = 0;
  _cleanupVideoPlayingFor = `${seg.id}:${seg.start}`;

  const target = Math.max(0, vStart);
  const startSync = () => {
    if (!_cvPreviewing) return;   // preview was cancelled before metadata arrived
    // Seek first; the persistent 'seeked' handler starts playback only once the
    // frame has actually decoded (no play()-during-seek freeze), then the loop
    // keeps the video locked to the audio clock.
    _cvHardSeek(target, true);
    if (_cvRAF) cancelAnimationFrame(_cvRAF);
    _cvRAF = requestAnimationFrame(_cvSyncLoop);
  };
  if (isFinite(video.duration) && video.duration > 0) startSync();
  else video.addEventListener('loadedmetadata', startSync, { once: true });
  return true;
}

// ── Drag + resize + zoom wiring (run once on first popup show) ──
let _cleanupVideoDragWired = false;
let _cvZoom = { scale: 1, tx: 0, ty: 0 };

function _cvApplyZoom() {
  const v = _cleanupVideoEl();
  if (!v) return;
  v.style.transformOrigin = '0 0';
  v.style.transform = `translate(${_cvZoom.tx.toFixed(2)}px, ${_cvZoom.ty.toFixed(2)}px) scale(${_cvZoom.scale.toFixed(4)})`;
  const body = v.closest('.cleanup-video-body');
  if (body) body.classList.toggle('zoomed', _cvZoom.scale > 1.001);
}

function _cvClampPan() {
  const v = _cleanupVideoEl();
  const body = v?.closest('.cleanup-video-body');
  if (!v || !body) return;
  const br = body.getBoundingClientRect();
  const scaledW = v.clientWidth  * _cvZoom.scale;
  const scaledH = v.clientHeight * _cvZoom.scale;
  const minTx = Math.min(0, br.width  - scaledW);
  const minTy = Math.min(0, br.height - scaledH);
  _cvZoom.tx = Math.max(minTx, Math.min(0, _cvZoom.tx));
  _cvZoom.ty = Math.max(minTy, Math.min(0, _cvZoom.ty));
}

function _cvResetZoom() {
  _cvZoom = { scale: 1, tx: 0, ty: 0 };
  _cvApplyZoom();
}

function _cleanupVideoEnsureDragWired() {
  if (_cleanupVideoDragWired) return;
  _cleanupVideoDragWired = true;

  const popup = _cleanupVideoPopupEl();
  const header = document.getElementById('cleanup-video-header');
  const resize = document.getElementById('cleanup-video-resize');
  const video = _cleanupVideoEl();
  const body = video?.closest('.cleanup-video-body');
  if (!popup || !header || !resize || !video || !body) return;

  // ── Drag-to-move popup ──
  let dragStart = null;
  header.addEventListener('mousedown', (e) => {
    if (e.target.closest('.cleanup-video-close')) return;
    if (e.button !== 0) return;
    const r = popup.getBoundingClientRect();
    dragStart = { x: e.clientX, y: e.clientY, left: r.left, top: r.top };
    popup.classList.add('dragging');
    e.preventDefault();
  });

  // ── Drag-to-resize from bottom-right corner ──
  let resizeStart = null;
  resize.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    const r = popup.getBoundingClientRect();
    resizeStart = { x: e.clientX, y: e.clientY, width: r.width, height: r.height };
    e.preventDefault();
    e.stopPropagation();
  });

  // ── Drag-to-pan when zoomed (left-click on video body) ──
  let panStart = null;
  body.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (_cvZoom.scale <= 1.001) return;  // no pan when not zoomed
    panStart = { x: e.clientX, y: e.clientY, tx: _cvZoom.tx, ty: _cvZoom.ty };
    body.classList.add('panning');
    e.preventDefault();
  });

  // Shared mousemove
  document.addEventListener('mousemove', (e) => {
    if (dragStart) {
      const dx = e.clientX - dragStart.x;
      const dy = e.clientY - dragStart.y;
      const left = Math.max(0, Math.min(window.innerWidth  - 80, dragStart.left + dx));
      const top  = Math.max(0, Math.min(window.innerHeight - 40, dragStart.top  + dy));
      popup.style.left  = `${left}px`;
      popup.style.top   = `${top}px`;
      popup.style.right = 'auto';
    } else if (resizeStart) {
      const w = Math.max(240, Math.min(window.innerWidth  - 40, resizeStart.width  + (e.clientX - resizeStart.x)));
      const h = Math.max(180, Math.min(window.innerHeight - 40, resizeStart.height + (e.clientY - resizeStart.y)));
      popup.style.width  = `${w}px`;
      popup.style.height = `${h}px`;
      _cvClampPan();
      _cvApplyZoom();
    } else if (panStart) {
      _cvZoom.tx = panStart.tx + (e.clientX - panStart.x);
      _cvZoom.ty = panStart.ty + (e.clientY - panStart.y);
      _cvClampPan();
      _cvApplyZoom();
    }
  });
  document.addEventListener('mouseup', () => {
    if (dragStart) {
      dragStart = null;
      popup.classList.remove('dragging');
      _cleanupVideoSavePosition();
    }
    if (resizeStart) {
      resizeStart = null;
      _cleanupVideoSavePosition();
    }
    if (panStart) {
      panStart = null;
      body.classList.remove('panning');
    }
  });

  // ── Mouse-wheel zoom centered at cursor ──
  body.addEventListener('wheel', (e) => {
    if (popup.hidden) return;
    e.preventDefault();
    const rect = body.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const oldScale = _cvZoom.scale;
    // Exponential feel: ~5% per notch. Negative deltaY = zoom in.
    const factor = Math.exp(-e.deltaY * 0.0015);
    const newScale = Math.max(1, Math.min(8, oldScale * factor));
    if (Math.abs(newScale - oldScale) < 1e-4) return;
    const ratio = newScale / oldScale;
    // Keep the content under the cursor fixed.
    _cvZoom.tx = mx - (mx - _cvZoom.tx) * ratio;
    _cvZoom.ty = my - (my - _cvZoom.ty) * ratio;
    _cvZoom.scale = newScale;
    _cvClampPan();
    _cvApplyZoom();
  }, { passive: false });

  // Double-click resets zoom.
  body.addEventListener('dblclick', (e) => {
    if (_cvZoom.scale > 1.001) { e.preventDefault(); _cvResetZoom(); }
  });
}

// NOTE: video mirroring is now driven directly inside the sequential player
// (`_cleanupPlayCurrent`), which seeks the muted popup video alongside the WAV
// whenever the popup is open. Screen recordings frequently have no audio track,
// so the WAV is always the authoritative clock. (The old _cleanupPlaySegment
// monkey-patch and the switchSpeakerManagerTab wrapper were folded in there and
// into switchSpeakerManagerTab respectively.)

// Dirty guard - both for page unload and for the modal close handler.
window.addEventListener('beforeunload', e => {
  if (_cleanupState && _cleanupState.dirty) {
    e.preventDefault();
    e.returnValue = '';
    return '';
  }
});

// Backspace outside an editable field can trigger a browser "back" navigation in
// the chromeless app window, which then trips the unsaved-changes prompt above
// ("leave site?"). Never let a stray Backspace navigate: only allow it when the
// user is actually editing text. Capture phase so it beats the default action.
window.addEventListener('keydown', e => {
  if (e.key !== 'Backspace') return;
  const t = e.target;
  const tag = t && t.tagName;
  const editable = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' ||
                   (t && t.isContentEditable);
  if (!editable) e.preventDefault();
}, true);

// Wrap existing close handler to prompt on unsaved changes and to close
// the floating video popup alongside the modal.
const _origCloseSpeakerManager = closeSpeakerManager;
closeSpeakerManager = async function (force) {
  // force: the session is being switched or cleared, so staged cleanup edits
  // cannot be kept anyway; callers that switch sessions ask BEFORE switching.
  if (!force && _cleanupState && _cleanupState.dirty) {
    const pending = Math.max(_cleanupPendingChangeCount(), 1);
    if (!await uiConfirm({
      title: 'Close without applying?',
      message: `${_plural(pending, 'cleanup change is', 'cleanup changes are')} staged and have not been written. Closing discards them.`,
      confirmLabel: 'Close and discard', cancelLabel: 'Keep editing', danger: true,
    })) return;
    _cleanupState.dirty = false;
  }
  // Tear down the floating workspace bits: popup, picker, any preview audio.
  try {
    _cleanupClosePicker();
    _cleanupStopPlayback();
    const popup = document.getElementById('cleanup-video-popup');
    if (popup && !popup.hidden) closeCleanupVideoPopup();
  } catch (_) {}
  _origCloseSpeakerManager();
};

// ── Fingerprint match toast ───────────────────────────────────────────────────

/* ── Fingerprint notification queue ─────────────────────────────────────────
 * Replaces the old one-shot toast with a persistent notification queue.
 * Suggestions accumulate in _fpNotifQueue and are shown in both:
 *   1. The bell panel (always available for review)
 *   2. A brief toast (fires once for attention, then auto-hides)
 * ────────────────────────────────────────────────────────────────────────── */
let _fpNotifQueue = [];          // persistent queue: [{session_id, speaker_key, current_name, matches, candidates}, ...]
let _fpToastActive = null;
let _fpToastTimer  = null;
let _fpRejected    = new Set();  // global_ids the user rejected ("not in this meeting") this session
let _fpOpenPopout  = null;       // the notif-panel candidate popout currently open (fixed-positioned), or null

// True if a suggestion is redundant - the speaker is already labeled with
// the same name as the top match (e.g. "Jason Palmer → Jason Palmer").
function _fpIsRedundantSuggestion(data) {
  const top = data?.matches?.[0];
  if (!top) return true;
  const cur = (data.current_name || '').trim().toLowerCase();
  if (!cur) return false;
  return cur === (top.name || '').trim().toLowerCase();
}

function _fpEnqueueToast(data) {
  if (_fpIsRedundantSuggestion(data)) return;
  // Replace any existing entry for the same speaker_key
  _fpNotifQueue = _fpNotifQueue.filter(d => d.speaker_key !== data.speaker_key);
  _fpNotifQueue.push(data);
  _fpUpdateBell();
  _fpRenderNotifPanel();
  _fpUpdateInlineIcons();
  // Show a brief toast for the new item
  if (!_fpToastActive) _fpShowNextToast();
}

function _fpRemoveFromQueue(speakerKey) {
  _fpNotifQueue = _fpNotifQueue.filter(d => d.speaker_key !== speakerKey);
  _fpUpdateBell();
  _fpRenderNotifPanel();
  _fpUpdateInlineIcons();
  // Auto-collapse the panel once all suggestions are processed
  _fpAutoCollapseIfEmpty();
}

function _fpAutoCollapseIfEmpty() {
  if (_fpNotifQueue.length > 0) return;
  const panel = document.getElementById('fp-notif-panel');
  if (!panel || panel.classList.contains('collapsed')) return;
  // Short delay so the user sees "No pending suggestions" before it collapses
  setTimeout(() => {
    if (_fpNotifQueue.length === 0 && !panel.classList.contains('collapsed')) {
      panel.classList.add('collapsed');
      const btn = document.getElementById('fp-bell-btn');
      if (btn) btn.classList.remove('open');
      _syncPanelBottomRadius();
    }
  }, 1200);
}

function _fpGetSuggestion(speakerKey) {
  const d = _fpNotifQueue.find(d => d.speaker_key === speakerKey) || null;
  if (d && _fpIsRedundantSuggestion(d)) return null;
  return d;
}

// Scroll the transcript to the first segment from the given speaker key.
function _fpJumpToSpeaker(speakerKey) {
  const target = _segmentRegistry.find(seg =>
    seg.dataset.transcriptSource === speakerKey
    || seg.dataset.originalSource === speakerKey
  );
  if (!target) return;
  _doProgrammaticScroll(target, { block: 'center', behavior: 'smooth' });
  target.classList.add('playing');
  setTimeout(() => target.classList.remove('playing'), 1500);
}

// ── Bell badge ────────────────────────────────────────────────────────────
function _fpUpdateBell() {
  const btn = document.getElementById('fp-bell-btn');
  const badge = document.getElementById('fp-bell-badge');
  if (!btn || !badge) return;
  const count = _fpNotifQueue.filter(d => !_fpIsRedundantSuggestion(d)).length;
  if (count > 0) {
    btn.classList.remove('hidden');
    btn.classList.add('has-notifications');
    badge.textContent = count;
  } else {
    btn.classList.remove('has-notifications');
    // Keep visible briefly so user sees it go to 0, then hide
    setTimeout(() => {
      if (_fpNotifQueue.length === 0) btn.classList.add('hidden');
    }, 2000);
  }
}

// ── Bottom-radius sync ───────────────────────────────────────────────────
// The transcript column has a stack of collapsible/hideable panels above
// the scroll area. Only the bottom-most visible element should carry the
// bottom border-radius so it visually closes the header block.
const _PANEL_BOTTOM_RADIUS_CLS = 'panel-bottom-radius';
const _PANEL_STACK_IDS = [
  'transcript-selection-bar',
  'playback-bar',
  'screen-preview',
  'video-viewer',
  'transcript-navigator',
  'analytics-panel',
  'fp-notif-panel',
];
function _syncPanelBottomRadius() {
  const col = document.querySelector('.col-transcript');
  if (!col) return;
  // Remove from all candidates
  const header = col.querySelector('.col-header');
  if (header) header.classList.remove(_PANEL_BOTTOM_RADIUS_CLS);
  for (const id of _PANEL_STACK_IDS) {
    document.getElementById(id)?.classList.remove(_PANEL_BOTTOM_RADIUS_CLS);
  }
  // Find the bottom-most visible panel (first in our bottom-to-top list)
  for (const id of _PANEL_STACK_IDS) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (el.classList.contains('hidden') || el.classList.contains('collapsed')) continue;
    el.classList.add(_PANEL_BOTTOM_RADIUS_CLS);
    return;
  }
  // No panels visible - col-header is the bottom element
  if (header) header.classList.add(_PANEL_BOTTOM_RADIUS_CLS);
}

function _syncSummaryBottomRadius() {
  const col = document.querySelector('.col-summary');
  if (!col) return;
  const header = col.querySelector('.col-header');
  const area = document.getElementById('summary-prompt-area');
  if (header) header.classList.remove(_PANEL_BOTTOM_RADIUS_CLS);
  if (area)   area.classList.remove(_PANEL_BOTTOM_RADIUS_CLS);
  if (area && !area.classList.contains('hidden')) {
    area.classList.add(_PANEL_BOTTOM_RADIUS_CLS);
  } else if (header) {
    header.classList.add(_PANEL_BOTTOM_RADIUS_CLS);
  }
}

// ── Notification panel ────────────────────────────────────────────────────
function toggleFpNotifPanel() {
  const panel = document.getElementById('fp-notif-panel');
  if (!panel) return;
  panel.classList.toggle('collapsed');
  const btn = document.getElementById('fp-bell-btn');
  if (btn) btn.classList.toggle('open', !panel.classList.contains('collapsed'));
  _syncPanelBottomRadius();
}

function _fpRenderNotifPanel() {
  const list = document.getElementById('fp-notif-list');
  if (!list) return;
  _fpCloseCandPopouts();   // any open candidate popout points at a card we're about to discard
  list.innerHTML = '';

  for (const item of _fpNotifQueue) {
    const top = item.matches[0];
    if (!top) continue;
    if (_fpIsRedundantSuggestion(item)) continue;
    if (_fpRejected.has(top.global_id)) continue;   // user said this profile isn't here

    const card = document.createElement('div');
    card.className = 'fp-notif-card';
    card.dataset.speakerKey = item.speaker_key;
    card.title = 'Click to jump to first occurrence';
    card.style.cursor = 'pointer';
    card.addEventListener('click', e => {
      if (e.target.closest('.fp-notif-actions')) return;
      _fpJumpToSpeaker(item.speaker_key);
    });

    const speaker = document.createElement('span');
    speaker.className = 'fp-notif-speaker';
    speaker.textContent = item.current_name || item.speaker_key;

    const arrow = document.createElement('i');
    arrow.className = 'fa-solid fa-arrow-right fp-notif-arrow';

    const match = document.createElement('span');
    match.className = 'fp-notif-match';
    match.textContent = top.name;

    const sim = document.createElement('span');
    sim.className = 'fp-notif-sim';
    sim.textContent = `${Math.round(top.similarity * 100)}%`;

    const actions = document.createElement('div');
    actions.className = 'fp-notif-actions';

    const applyBtn = document.createElement('button');
    applyBtn.className = 'fp-notif-btn fp-notif-apply';
    applyBtn.textContent = 'Apply';
    applyBtn.addEventListener('click', () => _fpNotifConfirm(item, top.global_id));
    actions.appendChild(applyBtn);

    // Similarity-ranked picker of alternatives (defaults to the top match).
    const otherList = document.createElement('div');
    otherList.className = 'fp-notif-other-list hidden';
    const nCands = _fpPopulateCandidates(otherList, item,
      gid => { _fpCloseCandPopouts(); _fpNotifConfirm(item, gid); });
    if (nCands > 1) {
      applyBtn.classList.add('has-alts');   // flatten Apply's right edge for the split button
      const otherWrap = document.createElement('div');
      otherWrap.className = 'fp-notif-other-wrap';
      const otherBtn = document.createElement('button');
      otherBtn.className = 'fp-notif-btn fp-notif-other-toggle';
      otherBtn.innerHTML = '<i class="fa-solid fa-chevron-down"></i>';
      otherBtn.title = 'Pick a different speaker';
      otherBtn.addEventListener('click', e => {
        e.stopPropagation();
        if (_fpOpenPopout === otherList) _fpCloseCandPopouts();
        else _fpOpenCandPopout(otherList, otherBtn);
      });
      otherWrap.appendChild(otherBtn);
      otherWrap.appendChild(otherList);
      actions.appendChild(otherWrap);
    }

    // "No": this profile isn't in the meeting; stop suggesting it everywhere.
    const noBtn = document.createElement('button');
    noBtn.className = 'fp-notif-btn fp-notif-no';
    noBtn.textContent = 'No';
    noBtn.title = `Not ${top.name}: stop suggesting them in this meeting`;
    noBtn.addEventListener('click', () => _fpNotifReject(item, top.global_id));
    actions.appendChild(noBtn);

    const skipBtn = document.createElement('button');
    skipBtn.className = 'fp-notif-btn fp-notif-skip';
    skipBtn.textContent = 'Skip';
    skipBtn.title = 'Dismiss this suggestion for now';
    skipBtn.addEventListener('click', () => _fpNotifDismiss(item));
    actions.appendChild(skipBtn);

    card.appendChild(speaker);
    card.appendChild(arrow);
    card.appendChild(match);
    card.appendChild(sim);
    card.appendChild(actions);
    list.appendChild(card);
  }
}

// Fill a dropdown with a similarity-ranked list of candidate profiles for a
// suggestion (top match first). Reuses the speaker-picker option styling, with
// a % badge per row. onPick(globalId, name) confirms that profile.
function _fpPopulateCandidates(listEl, item, onPick) {
  listEl.innerHTML = '';
  const cands = (item.candidates && item.candidates.length ? item.candidates : item.matches) || [];
  const topGid = item.matches?.[0]?.global_id;
  let shown = 0;
  cands.forEach(c => {
    if (!c.global_id || _fpRejected.has(c.global_id)) return;
    const opt = document.createElement('button');
    opt.className = 'speaker-picker-opt fp-cand-opt' + (c.global_id === topGid ? ' fp-cand-top' : '');
    opt.style.borderColor = (c.color ? c.color + '60' : 'var(--border)');
    if (c.color) opt.style.color = c.color;
    const nm = document.createElement('span');
    nm.className = 'fp-cand-name';
    nm.textContent = c.name;
    opt.appendChild(nm);
    _setOptSim(opt, c.similarity, { auto: 0.82, suggest: 0.70 });
    opt.addEventListener('mousedown', e => { e.preventDefault(); onPick(c.global_id, c.name); });
    listEl.appendChild(opt);
    shown++;
  });
  if (!shown) {
    const empty = document.createElement('div');
    empty.className = 'fp-cand-empty';
    empty.textContent = 'No other matches';
    listEl.appendChild(empty);
  }
  return shown;
}

// The notif panel lives inside an overflow-clipped, scrollable container, so an
// absolutely-positioned candidate popout gets cut off. Open it as a viewport-
// fixed layer positioned against its toggle button instead, so it escapes the
// clipping. (The toast picker is already a fixed-position layer, so it keeps
// its own CSS positioning.)
function _fpOpenCandPopout(listEl, btnEl) {
  _fpCloseCandPopouts();
  listEl.classList.remove('hidden');
  // Fixed positioning, measured against the button. Reset the CSS anchors so
  // our inline top/left win.
  Object.assign(listEl.style, { position: 'fixed', bottom: 'auto', right: 'auto', zIndex: '1000' });
  const br = btnEl.getBoundingClientRect();
  const lr = listEl.getBoundingClientRect();
  // Prefer opening upward (above the button); flip below if there isn't room.
  let top = br.top - lr.height - 4;
  if (top < 8) top = br.bottom + 4;
  // Right-align to the button, clamped into the viewport.
  let left = Math.min(br.right - lr.width, window.innerWidth - lr.width - 8);
  left = Math.max(8, left);
  listEl.style.top  = `${Math.round(top)}px`;
  listEl.style.left = `${Math.round(left)}px`;
  btnEl.classList.add('open');
  _fpOpenPopout = listEl;
  // Defer global listeners so the opening click doesn't immediately close it.
  setTimeout(() => {
    document.addEventListener('mousedown', _fpPopoutDocClick, true);
    window.addEventListener('scroll', _fpCloseCandPopouts, true);
    window.addEventListener('resize', _fpCloseCandPopouts);
  }, 0);
}

function _fpCloseCandPopouts() {
  if (_fpOpenPopout) {
    _fpOpenPopout.classList.add('hidden');
    // Drop the inline overrides so the element reverts to its stylesheet state.
    for (const p of ['position', 'top', 'left', 'bottom', 'right', 'zIndex']) {
      _fpOpenPopout.style[p] = '';
    }
    const wrap = _fpOpenPopout.closest('.fp-notif-other-wrap');
    wrap?.querySelector('.fp-notif-other-toggle')?.classList.remove('open');
    _fpOpenPopout = null;
  }
  document.removeEventListener('mousedown', _fpPopoutDocClick, true);
  window.removeEventListener('scroll', _fpCloseCandPopouts, true);
  window.removeEventListener('resize', _fpCloseCandPopouts);
}

function _fpPopoutDocClick(e) {
  if (!_fpOpenPopout) return;
  if (_fpOpenPopout.contains(e.target) || e.target.closest('.fp-notif-other-toggle')) return;
  _fpCloseCandPopouts();
}

// "No": the user says this profile isn't in the meeting. Suppress it for every
// speaker (server-side, session-wide) and drop all cards pointing at it.
async function _fpNotifReject(item, globalId) {
  if (!globalId) { _fpNotifDismiss(item); return; }
  _fpRejected.add(globalId);
  _fpNotifQueue = _fpNotifQueue.filter(d => d.matches?.[0]?.global_id !== globalId);
  _fpUpdateBell();
  _fpRenderNotifPanel();
  _fpUpdateInlineIcons();
  _fpAutoCollapseIfEmpty();
  if (_fpToastActive && (_fpToastActive.speaker_key === item.speaker_key ||
                         _fpToastActive.matches?.[0]?.global_id === globalId)) {
    _fpHideToast();
  }
  try {
    await fetch('/api/fingerprint/reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: item.session_id, global_id: globalId }),
    });
  } catch (e) { console.warn('fp reject failed', e); }
}

async function _fpNotifConfirm(item, globalId) {
  _fpRemoveFromQueue(item.speaker_key);
  try {
    await fetch('/api/fingerprint/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id:  item.session_id,
        speaker_key: item.speaker_key,
        global_id:   globalId,
      }),
    });
  } catch (e) { console.warn('fp confirm failed', e); }
  // If this was the active toast, advance
  if (_fpToastActive?.speaker_key === item.speaker_key) _fpHideToast();
}

async function _fpNotifDismiss(item) {
  _fpRemoveFromQueue(item.speaker_key);
  try {
    await fetch('/api/fingerprint/dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id:  item.session_id,
        speaker_key: item.speaker_key,
        global_id:   item.matches[0]?.global_id || '',
      }),
    });
  } catch (e) { console.warn('fp dismiss failed', e); }
  if (_fpToastActive?.speaker_key === item.speaker_key) _fpHideToast();
}

function fpNotifDismissAll() {
  const items = [..._fpNotifQueue];
  _fpNotifQueue = [];
  _fpUpdateBell();
  _fpRenderNotifPanel();
  _fpUpdateInlineIcons();
  _fpAutoCollapseIfEmpty();
  if (_fpToastActive) _fpHideToast();
  for (const item of items) {
    fetch('/api/fingerprint/dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id:  item.session_id,
        speaker_key: item.speaker_key,
        global_id:   item.matches[0]?.global_id || '',
      }),
    }).catch(() => {});
  }
}

// ── Load suggestions from server (for page refresh persistence) ───────────
async function _fpLoadSuggestions() {
  try {
    const res = await fetch('/api/fingerprint/suggestions').then(r => r.json());
    if (!res.suggestions?.length) return;
    if (res.session_id !== state.sessionId) return;
    for (const s of res.suggestions) {
      if (_fpIsRedundantSuggestion(s)) continue;
      // Only add if not already in queue
      if (!_fpNotifQueue.some(q => q.speaker_key === s.speaker_key)) {
        _fpNotifQueue.push(s);
      }
    }
    _fpUpdateBell();
    _fpRenderNotifPanel();
    _fpUpdateInlineIcons();
  } catch (_) {}
}

// ── Inline identify icons on speaker badges ───────────────────────────────
function _fpUpdateInlineIcons() {
  document.querySelectorAll('.speaker-identify-icon').forEach(icon => {
    const key = icon.closest('.src-speaker')?.dataset.speakerKey;
    if (!key) return;
    const suggestion = _fpGetSuggestion(key);
    if (suggestion) {
      icon.classList.add('has-suggestion');
      icon.title = `Sounds like ${suggestion.matches[0].name} (${Math.round(suggestion.matches[0].similarity * 100)}%)`;
    } else {
      icon.classList.remove('has-suggestion');
      icon.title = 'Identify speaker';
    }
  });
}

// ── Sticky reassignment feedback ──────────────────────────────────────────
// Brief status-bar notice when a manual reassignment starts (or stops)
// sticking to a live diarizer key (source_redirect SSE; the backend registers
// one when recent segments of a live speaker get reassigned to someone else).
function _flashLiveRedirect(d) {
  const target = d.target ? (_speakerDisplayName(d.target) || d.target_name || d.target) : null;
  const srcName = d.source_name || d.source;
  const msg = d.action === 'set'
    ? `New "${srcName}" lines will be labeled ${target}`
    : `New lines from that voice return to "${srcName}"`;
  console.info(`[speakers] ${msg} (${d.source})`);
  const text = document.getElementById('status-text');
  const prev = text?.textContent;
  if (text) {
    text.textContent = msg;
    setTimeout(() => { if (text.textContent === msg) text.textContent = prev; }, 4000);
  }
}

// ── Auto-apply flash feedback ─────────────────────────────────────────────
function _fpFlashAutoApply(speakerKey, name) {
  document.querySelectorAll(`.src-speaker[data-speaker-key="${speakerKey}"]`).forEach(badge => {
    badge.classList.add('fp-auto-applied');
    badge.addEventListener('animationend', () => badge.classList.remove('fp-auto-applied'), { once: true });
  });
  // Brief status-bar message
  const text = document.getElementById('status-text');
  const prev = text?.textContent;
  if (text) {
    text.textContent = `Identified ${speakerKey} as ${name}`;
    setTimeout(() => { if (text.textContent.startsWith('Identified')) text.textContent = prev; }, 3000);
  }
}

// ── Toast (brief attention-getter, backed by notification queue) ──────────
function _fpShowNextToast() {
  // Find next item in queue that hasn't been toasted yet
  if (!_fpNotifQueue.length) return;
  // Show the most recent item
  _fpToastActive = _fpNotifQueue[_fpNotifQueue.length - 1];
  const toast = document.getElementById('fp-match-toast');
  const top   = _fpToastActive.matches[0];

  document.getElementById('fp-toast-label').innerHTML =
    `${_fpToastActive.current_name || _fpToastActive.speaker_key} sounds like <strong id="fp-toast-name">${top.name}</strong>`;
  document.getElementById('fp-toast-sim').textContent = `${Math.round(top.similarity * 100)}%`;

  const otherList = document.getElementById('fp-toast-other-list');
  otherList.classList.add('hidden');
  const nCands = _fpPopulateCandidates(otherList, _fpToastActive, gid => _fpNotifConfirm(_fpToastActive, gid));
  // Show the "Others" picker only when there's an alternative beyond the top match.
  document.getElementById('fp-toast-other').style.display = nCands > 1 ? '' : 'none';

  toast.classList.remove('hidden');
  toast.style.animation = 'none';
  toast.offsetHeight;
  toast.style.animation = '';

  if (_fpToastTimer) clearTimeout(_fpToastTimer);
  _fpToastTimer = setTimeout(() => fpToastSkip(), 8000);
}

function fpToastApply() {
  if (!_fpToastActive) return;
  const top = _fpToastActive.matches[0];
  _fpNotifConfirm(_fpToastActive, top.global_id);
}

function fpToastToggleOther() {
  document.getElementById('fp-toast-other-list').classList.toggle('hidden');
}

function fpToastNo() {
  if (!_fpToastActive) return;
  const top = _fpToastActive.matches[0];
  if (top) _fpNotifReject(_fpToastActive, top.global_id);  // removes from queue + hides toast
  else fpToastSkip();
}

function _fpAnimateOut(cb) {
  const toast = document.getElementById('fp-match-toast');
  document.getElementById('fp-toast-other-list')?.classList.add('hidden');
  toast.classList.add('fp-toast-out');
  toast.addEventListener('animationend', function handler() {
    toast.removeEventListener('animationend', handler);
    toast.classList.remove('fp-toast-out');
    toast.classList.add('hidden');
    if (cb) cb();
  }, { once: true });
}

function fpToastSkip() {
  if (!_fpToastActive) return;
  _fpToastActive = null;
  if (_fpToastTimer) { clearTimeout(_fpToastTimer); _fpToastTimer = null; }
  _fpAnimateOut();
  // Don't dismiss from queue - it stays in the bell panel for later review
}

function _fpHideToast() {
  _fpToastActive = null;
  if (_fpToastTimer) { clearTimeout(_fpToastTimer); _fpToastTimer = null; }
  _fpAnimateOut();
}

async function _fpConfirm(toastData, globalId) {
  _fpNotifConfirm(toastData, globalId);
}

async function _fpDismiss(toastData) {
  _fpNotifDismiss(toastData);
}

// ── Voice Library panel ───────────────────────────────────────────────────────

let _fpProfiles     = [];   // global speaker list
let _fpLoaded       = false; // true once the profiles have been fetched at least once
let _fpSelectedId   = null; // currently selected global_id
let _fpDetailColor  = '';
let _fpSelectMode   = false;
let _fpSelected     = new Set();  // selected global_ids for bulk ops
let _fpSearchTerm   = '';

/** The Voice Library is the /speakers view now (brief 3.8). Navigating there
 *  is what "open" means; the view's activate() runs the loader. */
function openFingerprintPanel() {
  navigateTo('/speakers');
}

/** Kept for the call sites that leave the library for a recording: the
 *  navigation itself is what replaces the panel, so there is nothing to close. */
function closeFingerprintPanel() {}

/** Reset search and selection, then load. The Speakers view calls this. */
async function loadFingerprintPanel(opts) {
  const o = opts || {};
  _fpSearchTerm = '';
  _fpSelectMode = false;
  _fpSelected.clear();
  const searchInput = document.getElementById('fp-search-input');
  if (searchInput) searchInput.value = '';
  const selectToggle = document.getElementById('fp-select-toggle');
  if (selectToggle) selectToggle.classList.remove('active');
  document.getElementById('fp-select-bar')?.classList.add('hidden');
  // The Voice Library behaves like a cached slice: fetch once, then reuse on
  // revisit. Mutations (rename, merge, delete, link) call _fpLoadProfiles()
  // themselves, so the cache stays fresh, and navigating back to /speakers no
  // longer refetches every time (the zero-fetch-on-revisit contract).
  if (o.cached && _fpLoaded) {
    _fpRenderProfileList();
    if (_fpSelectedId) {
      const still = _fpProfiles.find(p => p.id === _fpSelectedId);
      if (still) _fpSelectProfile(still.id); else _fpClearDetail();
    }
  } else {
    await _fpLoadProfiles();
  }
  Views.setTitle('speakers', 'Speakers', _fpSubtitle());
}

function _fpSubtitle() {
  const n = _fpProfiles.length;
  if (!n) return 'No voice profiles yet';
  return `${n} voice profile${n === 1 ? '' : 's'}`;
}

async function _fpLoadProfiles() {
  try {
    const resp = await fetch('/api/fingerprint/speakers');
    _fpProfiles = await resp.json();
    _fpLoaded = true;
  } catch (e) {
    _fpProfiles = [];
  }
  // Sort by sample count descending
  _fpProfiles.sort((a, b) => (b.emb_count || 0) - (a.emb_count || 0));
  _fpRenderProfileList();
  if (_fpSelectedId) {
    const still = _fpProfiles.find(p => p.id === _fpSelectedId);
    if (still) _fpSelectProfile(still.id); else _fpClearDetail();
  }
}

function _fpRenderProfileList() {
  const scrollEl = document.getElementById('fp-profile-scroll');
  const listEl = document.getElementById('fingerprint-profile-list');

  // Apply select mode class
  if (_fpSelectMode) listEl.classList.add('fp-select-mode');
  else listEl.classList.remove('fp-select-mode');

  // Filter by search
  const term = _fpSearchTerm.toLowerCase();
  const filtered = term
    ? _fpProfiles.filter(p => p.name.toLowerCase().includes(term))
    : _fpProfiles;

  if (!filtered.length) {
    scrollEl.innerHTML = `<div class="fp-panel-empty">${_fpProfiles.length ? 'No matching profiles.' : 'No voice profiles yet. Use "New profile" to create one.'}</div>`;
    _fpUpdateBulkUI();
    return;
  }
  scrollEl.innerHTML = '';
  filtered.forEach(p => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'fp-profile-row' + (_fpSelectedId === p.id ? ' selected' : '');

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'fp-row-checkbox';
    cb.checked = _fpSelected.has(p.id);
    cb.addEventListener('click', e => { e.stopPropagation(); _fpToggleSelect(p.id, cb.checked); });

    row.addEventListener('click', () => {
      if (_fpSelectMode) { cb.checked = !cb.checked; _fpToggleSelect(p.id, cb.checked); }
      else _fpSelectProfile(p.id);
    });

    const swatch = document.createElement('span');
    swatch.className = 'speaker-row-swatch';
    swatch.style.backgroundColor = p.color || '#58a6ff';

    const main = document.createElement('div');
    main.className = 'fp-profile-row-main';
    main.innerHTML = `<div class="fp-profile-name">${p.name}</div>
      <div class="fp-profile-meta">${p.emb_count} sample${p.emb_count === 1 ? '' : 's'}</div>`;

    row.appendChild(cb);
    row.appendChild(swatch);
    row.appendChild(main);
    scrollEl.appendChild(row);
  });
  _fpUpdateBulkUI();
}

async function _fpSelectProfile(globalId) {
  _fpSelectedId = globalId;
  _fpRenderProfileList();

  const profile = _fpProfiles.find(p => p.id === globalId);
  if (!profile) return;

  _fpDetailColor = profile.color || '';

  const detail = document.getElementById('fingerprint-profile-detail');
  detail.classList.remove('hidden');
  document.getElementById('fingerprint-panel-new').style.display = 'none';

  document.getElementById('fp-detail-name').value = profile.name;
  document.getElementById('fp-detail-meta').textContent =
    `${profile.emb_count} voice sample${profile.emb_count === 1 ? '' : 's'}`;

  // Color grid
  const grid = document.getElementById('fp-detail-color-grid');
  grid.innerHTML = '';
  _SPEAKER_PALETTE.forEach(color => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'speaker-color-btn' + (_fpDetailColor === color ? ' active' : '');
    btn.style.backgroundColor = color;
    btn.dataset.color = color;
    btn.addEventListener('click', () => {
      _fpDetailColor = color;
      grid.querySelectorAll('.speaker-color-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.color === color));
      // Auto-save color change
      if (_fpSelectedId) {
        fetch(`/api/fingerprint/speakers/${_fpSelectedId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ color }),
        }).then(() => _fpLoadProfiles());
      }
    });
    grid.appendChild(btn);
  });

  // Merge dropdown
  const mergeSel = document.getElementById('fp-detail-merge-sel');
  mergeSel.innerHTML = '<option value="">Merge into…</option>';
  _fpProfiles.filter(p => p.id !== globalId).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    mergeSel.appendChild(opt);
  });

  // Sessions
  try {
    const sessions = await fetch(`/api/fingerprint/speakers/${globalId}/sessions`).then(r => r.json());
    const sessEl = document.getElementById('fp-detail-sessions');
    if (!sessions.length) {
      sessEl.innerHTML = '<div class="fp-detail-sessions-empty">No sessions yet.</div>';
    } else {
      sessEl.innerHTML = sessions.map(s => {
        const date = s.started_at ? new Date(s.started_at).toLocaleDateString() : '';
        const keys = (s.speaker_keys || []).join(', ');
        return `<button class="fp-session-row" onclick="loadSession('${s.session_id}'); closeFingerprintPanel();">
          <span class="fp-session-title">${s.title || 'Untitled'}</span>
          <span class="fp-session-meta">${date}${keys ? ' <span class="session-meta-sep">|</span> ' + keys : ''} <span class="session-meta-sep">|</span> ${s.seg_count} segs</span>
        </button>`;
      }).join('');
    }
  } catch (e) {
    document.getElementById('fp-detail-sessions').innerHTML = '';
  }
}

// ── Bulk selection helpers ───────────────────────────────────────────────────

function _fpFilterList() {
  _fpSearchTerm = (document.getElementById('fp-search-input').value || '').trim();
  _fpRenderProfileList();
}

function _fpToggleSelectMode() {
  _fpSelectMode = !_fpSelectMode;
  const btn = document.getElementById('fp-select-toggle');
  btn.classList.toggle('active', _fpSelectMode);
  document.getElementById('fp-select-bar').classList.toggle('hidden', !_fpSelectMode);
  if (!_fpSelectMode) { _fpSelected.clear(); }
  _fpRenderProfileList();
}

function _fpToggleSelect(id, checked) {
  if (checked) _fpSelected.add(id);
  else _fpSelected.delete(id);
  _fpUpdateBulkUI();
}

function _fpToggleSelectAll(checked) {
  const term = _fpSearchTerm.toLowerCase();
  const visible = term ? _fpProfiles.filter(p => p.name.toLowerCase().includes(term)) : _fpProfiles;
  if (checked) visible.forEach(p => _fpSelected.add(p.id));
  else visible.forEach(p => _fpSelected.delete(p.id));
  _fpRenderProfileList();
}

function _fpUpdateBulkUI() {
  const n = _fpSelected.size;
  const countEl = document.getElementById('fp-select-count');
  if (countEl) countEl.textContent = `${n} selected`;
  const bulkEl = document.getElementById('fp-bulk-actions');
  if (bulkEl) bulkEl.classList.toggle('hidden', !_fpSelectMode || n === 0);
  const allCb = document.getElementById('fp-select-all');
  if (allCb) {
    const term = _fpSearchTerm.toLowerCase();
    const visible = term ? _fpProfiles.filter(p => p.name.toLowerCase().includes(term)) : _fpProfiles;
    allCb.checked = visible.length > 0 && visible.every(p => _fpSelected.has(p.id));
  }
}

async function _fpBulkDelete() {
  const ids = [..._fpSelected];
  if (!ids.length) return;
  const names = ids.map(id => _fpProfiles.find(p => p.id === id)?.name || id);
  if (!await uiConfirm({ title: `Delete ${ids.length} profile${ids.length > 1 ? 's' : ''}?`, message: 'This cannot be undone.', details: names, confirmLabel: 'Delete', danger: true })) return;
  await fetch('/api/fingerprint/speakers/bulk', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  _fpSelected.clear();
  if (ids.includes(_fpSelectedId)) _fpClearDetail();
  await _fpLoadProfiles();
}

async function _fpBulkMerge() {
  const ids = [..._fpSelected];
  if (ids.length < 2) { uiToast({ message: 'Select at least 2 profiles to merge.', kind: 'warn' }); return; }
  const names = ids.map(id => _fpProfiles.find(p => p.id === id)?.name || id);
  const keepName = names[0];
  if (!await uiConfirm({ title: `Merge ${ids.length} profiles into "${keepName}"?`, message: 'All voice samples will be combined. This cannot be undone.', details: names, confirmLabel: 'Merge', danger: true })) return;
  const keepId = ids[0];
  for (let i = 1; i < ids.length; i++) {
    await fetch(`/api/fingerprint/speakers/${keepId}/merge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_id: ids[i] }),
    });
  }
  _fpSelected.clear();
  _fpSelectedId = keepId;
  await _fpLoadProfiles();
}

async function _fpBulkOptimize() {
  const ids = [..._fpSelected];
  if (!ids.length) return;
  if (!await uiConfirm({ title: `Optimize ${ids.length} profile${ids.length > 1 ? 's' : ''}?`, message: 'This prunes redundant voice samples.', confirmLabel: 'Optimize' })) return;
  await fetch('/api/fingerprint/speakers/bulk/optimize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  });
  await _fpLoadProfiles();
  if (_fpSelectedId) _fpSelectProfile(_fpSelectedId);
}

function _fpClearDetail() {
  _fpSelectedId = null;
  document.getElementById('fingerprint-profile-detail').classList.add('hidden');
}

async function fpDetailSave() {
  if (!_fpSelectedId) return;
  const name = document.getElementById('fp-detail-name').value.trim();
  if (!name) { uiToast({ message: 'Name is required.', kind: 'warn' }); return; }
  await fetch(`/api/fingerprint/speakers/${_fpSelectedId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, color: _fpDetailColor || null }),
  });
  await _fpLoadProfiles();
}

async function fpDetailOptimize() {
  if (!_fpSelectedId) return;
  await fetch(`/api/fingerprint/speakers/${_fpSelectedId}/optimize`, { method: 'POST' });
  await _fpLoadProfiles();
  if (_fpSelectedId) _fpSelectProfile(_fpSelectedId);
}

async function fpDetailMerge() {
  const sel = document.getElementById('fp-detail-merge-sel');
  const targetId = sel.value;
  if (!targetId || !_fpSelectedId) return;
  if (!await uiConfirm({ title: 'Merge profiles?', message: `Merge "${document.getElementById('fp-detail-name').value}" into the selected profile? This cannot be undone.`, confirmLabel: 'Merge', danger: true })) return;
  await fetch(`/api/fingerprint/speakers/${targetId}/merge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_id: _fpSelectedId }),
  });
  _fpSelectedId = targetId;
  await _fpLoadProfiles();
}

async function fpDetailDelete() {
  if (!_fpSelectedId) return;
  const name = document.getElementById('fp-detail-name').value;
  if (!await uiConfirm({ title: `Delete "${name}"?`, message: 'Delete this profile and all its voice samples? This cannot be undone.', confirmLabel: 'Delete', danger: true })) return;
  await fetch(`/api/fingerprint/speakers/${_fpSelectedId}`, { method: 'DELETE' });
  _fpClearDetail();
  await _fpLoadProfiles();
}

function fpShowNew() {
  document.getElementById('fingerprint-panel-new').style.display = 'flex';
  document.getElementById('fp-new-name').value = '';
  document.getElementById('fp-new-name').focus();
  document.getElementById('fingerprint-profile-detail').classList.add('hidden');
  _fpSelectedId = null;
  _fpRenderProfileList();
}

function fpCancelNew() {
  document.getElementById('fingerprint-panel-new').style.display = 'none';
}

async function fpCreateProfile() {
  const name = document.getElementById('fp-new-name').value.trim();
  if (!name) { uiToast({ message: 'Enter a name.', kind: 'warn' }); return; }
  const resp = await fetch('/api/fingerprint/speakers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  const data = await resp.json();
  fpCancelNew();
  await _fpLoadProfiles();
  if (data.global_id) _fpSelectProfile(data.global_id);
}

/* ── Voice Library: Match Speakers tab ──────────────────────────────────── */

let _fpMatchGroups = [];
let _fpMatchProfiles = [];

function fpSwitchTab(tab) {
  document.querySelectorAll('.fp-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === tab));
  const profilesTab = document.getElementById('fp-tab-profiles');
  const matchTab = document.getElementById('fp-tab-match');
  const healthTab = document.getElementById('fp-tab-health');
  const footer = document.getElementById('fp-footer-profiles');
  if (profilesTab) profilesTab.classList.toggle('hidden', tab !== 'profiles');
  if (matchTab) matchTab.classList.toggle('hidden', tab !== 'match');
  if (healthTab) healthTab.classList.toggle('hidden', tab !== 'health');
  if (footer) footer.style.display = tab === 'profiles' ? '' : 'none';
  if (tab === 'match') fpLoadUnlinked();
  if (tab === 'health') fpLoadHealth();
}

// ── Library health tab ───────────────────────────────────────────────────────

let _fpHealth = null;

async function fpLoadHealth() {
  const scroll = document.getElementById('fp-health-scroll');
  scroll.innerHTML = '<div class="fp-panel-empty">Checking library health…</div>';
  try {
    const resp = await fetch('/api/fingerprint/library/health');
    if (!resp.ok) throw new Error(await resp.text());
    _fpHealth = await resp.json();
    _fpRenderHealth();
  } catch (e) {
    scroll.innerHTML = '<div class="fp-panel-empty">Could not check library health.</div>';
  }
}

function _fpRenderHealth() {
  const h = _fpHealth;
  if (!h) return;
  const scroll = document.getElementById('fp-health-scroll');
  const esc = s => String(s ?? '').replace(/[&<>"]/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const parts = [];

  parts.push(`<div class="fp-health-summary">
    <span class="fp-health-chip">${h.profiles} profiles</span>
    <span class="fp-health-chip">${h.embeddings.toLocaleString()} voice samples</span>
  </div>`);

  const dupsAuto = h.duplicates.filter(d => d.auto);
  const dupsReview = h.duplicates.filter(d => !d.auto);
  const foreignN = h.foreign.removed_total;
  const purgeable = h.splits.filter(s => s.class === 'pollution');
  const cleanable = dupsAuto.length + foreignN + purgeable.length;

  if (!cleanable && !dupsReview.length && !h.splits.length && !h.confusable.length) {
    parts.push('<div class="fp-health-clean"><i class="fa-solid fa-circle-check"></i> Library looks healthy. Nothing to clean.</div>');
  }

  if (dupsAuto.length || dupsReview.length) {
    parts.push('<div class="fp-health-sec">Duplicate profiles</div>');
    for (const d of dupsAuto) {
      parts.push(`<div class="fp-health-row">
        <span class="fp-health-badge fix">will merge</span>
        <span>${esc(d.merge_name)} (${d.merge_count}) &rarr; ${esc(d.keep_name)} (${d.keep_count})${d.similarity != null ? ` &middot; voice match ${Math.round(d.similarity * 100)}%` : ''}</span>
      </div>`);
    }
    for (const d of dupsReview) {
      parts.push(`<div class="fp-health-row">
        <span class="fp-health-badge review">same voice</span>
        <span>${esc(d.merge_name)} and ${esc(d.keep_name)} &middot; voice match ${Math.round(d.similarity * 100)}%</span>
        <button class="speaker-manager-btn speaker-manager-btn-secondary fp-health-mini-btn"
          onclick="fpHealthMerge('${d.keep_id}', '${d.merge_id}', '${esc(d.merge_name)}', '${esc(d.keep_name)}')">Merge</button>
      </div>`);
    }
  }

  if (foreignN || h.foreign.flagged.length) {
    parts.push('<div class="fp-health-sec">Misfiled voice samples</div>');
    for (const [gid, p] of Object.entries(h.foreign.profiles)) {
      const from = Object.entries(p.absorbed_from)
        .map(([n, c]) => `${esc(n)} (${c})`).join(', ');
      parts.push(`<div class="fp-health-row">
        <span class="fp-health-badge fix">will remove</span>
        <span>${esc(p.name)}: ${p.removed} of ${p.count} samples sound like ${from}</span>
      </div>`);
    }
    for (const f of h.foreign.flagged) {
      parts.push(`<div class="fp-health-row">
        <span class="fp-health-badge review">check</span>
        <span>${esc(f.name)}: most samples (${f.foreign}/${f.count}) sound like someone else. Open the profile and check who this really is.</span>
      </div>`);
    }
  }

  if (h.splits.length) {
    parts.push('<div class="fp-health-sec">Profiles containing two voices</div>');
    for (const s of h.splits) {
      const fix = s.class === 'pollution';
      parts.push(`<div class="fp-health-row">
        <span class="fp-health-badge ${fix ? 'fix' : 'review'}">${fix ? 'will fix' : 'check'}</span>
        <span>${esc(s.name)}: ${s.minority_count} of ${s.count} samples are a second voice${s.minority_matches ? `, closest to ${esc(s.minority_matches)} (${Math.round(s.minority_match_sim * 100)}%)` : ''}</span>
      </div>`);
    }
  }

  if (h.confusable.length) {
    parts.push('<div class="fp-health-sec">Similar-sounding people</div>');
    parts.push('<div class="fp-health-note">These voices are close; automatic naming can occasionally swap them. Nothing to clean, just good to know.</div>');
    for (const c of h.confusable) {
      parts.push(`<div class="fp-health-row">
        <span class="fp-health-badge info">${Math.round(c.similarity * 100)}%</span>
        <span>${esc(c.a)} and ${esc(c.b)}</span>
      </div>`);
    }
  }

  scroll.innerHTML = parts.join('');

  const cb = document.getElementById('fp-health-auto-cb');
  if (cb && h.auto) cb.checked = !!h.auto.enabled;
  const last = document.getElementById('fp-health-lastrun');
  if (last && h.auto) {
    last.textContent = h.auto.last_run
      ? `Last cleanup: ${new Date(h.auto.last_run + 'Z').toLocaleDateString()}`
      : 'Never cleaned yet';
  }
  const runBtn = document.getElementById('fp-health-run-btn');
  if (runBtn) runBtn.disabled = !cleanable;
}

async function fpHealthMerge(keepId, mergeId, mergeName, keepName) {
  if (!await uiConfirm({ title: 'Merge profiles?', message: `Merge "${mergeName}" into "${keepName}"? All voice samples will be combined. This cannot be undone.`, confirmLabel: 'Merge', danger: true })) return;
  await fetch(`/api/fingerprint/speakers/${keepId}/merge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source_id: mergeId }),
  });
  await _fpLoadProfiles();
  await fpLoadHealth();
}

async function fpHealthRun() {
  const btn = document.getElementById('fp-health-run-btn');
  if (!await uiConfirm({ title: 'Run library cleanup now?', message: 'This merges duplicate profiles, removes voice samples that belong to someone else, and re-tunes every profile. Review items are left alone.', confirmLabel: 'Run cleanup', danger: true })) return;
  btn.disabled = true;
  btn.textContent = 'Cleaning…';
  try {
    const resp = await fetch('/api/fingerprint/library/maintenance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dry_run: false }),
    });
    const r = await resp.json();
    if (!resp.ok) throw new Error(r.error || 'cleanup failed');
    _fpHealthStatus(`Cleaned: ${r.merges.length} merged, `
      + `${r.foreign.removed_total} misfiled samples removed`);
  } catch (e) {
    _fpHealthStatus(e.message || 'Library cleanup failed', true);
  } finally {
    btn.textContent = 'Run Cleanup Now';
    await _fpLoadProfiles();
    await fpLoadHealth();
  }
}

async function fpHealthToggleAuto(enabled) {
  await fetch('/api/fingerprint/library/auto', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
}

function _fpHealthStatus(msg, isError) {
  const el = document.getElementById('fp-health-lastrun');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
}

async function fpLoadUnlinked() {
  const resp = await fetch('/api/fingerprint/unlinked-labels');
  if (!resp.ok) return;
  const data = await resp.json();
  _fpMatchGroups = data.groups || [];
  _fpMatchProfiles = data.profiles || [];
  _fpRenderMatchTab();
}

function _fpRenderMatchTab() {
  const scroll = document.getElementById('fp-match-scroll');
  const empty = document.getElementById('fp-match-empty');
  const actions = document.getElementById('fp-match-actions');
  if (!scroll) return;

  scroll.innerHTML = '';

  if (_fpMatchGroups.length === 0) {
    if (empty) empty.classList.remove('hidden');
    if (actions) actions.classList.add('hidden');
    return;
  }

  if (empty) empty.classList.add('hidden');
  if (actions) actions.classList.remove('hidden');

  // Sort: unmatched (no auto-selected profile) first, then matched
  const sorted = [..._fpMatchGroups].sort((a, b) => {
    const aMatch = _fpMatchProfiles.some(p => p.name.toLowerCase() === a.name.toLowerCase());
    const bMatch = _fpMatchProfiles.some(p => p.name.toLowerCase() === b.name.toLowerCase());
    if (aMatch === bMatch) return 0;
    return aMatch ? 1 : -1;
  });

  sorted.forEach(group => {
    const row = document.createElement('div');
    row.className = 'fp-match-row';
    row.dataset.name = group.name;

    const matchingProfile = _fpMatchProfiles.find(p =>
      p.name.toLowerCase() === group.name.toLowerCase());
    const isUnmatched = !matchingProfile;

    const info = document.createElement('div');
    info.className = 'fp-match-info';

    const nameLink = document.createElement('button');
    nameLink.className = 'fp-match-name fp-match-name-link';
    nameLink.textContent = group.name;
    nameLink.title = 'Jump to session';
    nameLink.addEventListener('click', (e) => {
      e.stopPropagation();
      _fpMatchGoToSessions(group.name, nameLink);
    });
    info.appendChild(nameLink);

    if (isUnmatched) {
      const badge = document.createElement('span');
      badge.className = 'fp-match-badge-unmatched';
      badge.textContent = 'Unmatched';
      info.appendChild(badge);
    }

    const countSpan = document.createElement('span');
    countSpan.className = 'fp-match-count';
    countSpan.textContent = `${group.session_count} session${group.session_count !== 1 ? 's' : ''}`
      + (group.label_count > 1 ? ` · ${group.label_count} label${group.label_count !== 1 ? 's' : ''}` : '');
    info.appendChild(countSpan);

    const sel = document.createElement('select');
    sel.className = 'fp-match-select';
    sel.innerHTML = '<option value="">-- Select profile --</option>'
      + '<option value="__new__">+ Create New Profile</option>'
      + _fpMatchProfiles.map(p =>
        `<option value="${p.id}">${escapeHtml(p.name)}</option>`
      ).join('');

    if (matchingProfile) sel.value = matchingProfile.id;

    // Update badge on selection change
    sel.addEventListener('change', () => {
      const badge = row.querySelector('.fp-match-badge-unmatched');
      if (sel.value) {
        if (badge) badge.remove();
        row.classList.remove('fp-match-row-unmatched');
      } else {
        if (!badge) {
          const b = document.createElement('span');
          b.className = 'fp-match-badge-unmatched';
          b.textContent = 'Unmatched';
          info.querySelector('.fp-match-name').after(b);
        }
        row.classList.add('fp-match-row-unmatched');
      }
    });

    if (isUnmatched) row.classList.add('fp-match-row-unmatched');

    const btn = document.createElement('button');
    btn.className = 'speaker-manager-btn speaker-manager-btn-ghost fp-match-link-btn';
    btn.textContent = 'Link';
    btn.addEventListener('click', () => fpLinkOne(row));

    row.appendChild(info);
    row.appendChild(sel);
    row.appendChild(btn);
    scroll.appendChild(row);
  });
}

async function fpLinkOne(row) {
  const name = row.dataset.name;
  const sel = row.querySelector('.fp-match-select');
  const value = sel ? sel.value : '';
  if (!value) { uiToast({ message: 'Select a profile or "Create New".', kind: 'warn' }); return; }

  const body = value === '__new__'
    ? { name, create_new: true }
    : { name, global_id: value };

  const btn = row.querySelector('.fp-match-link-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Linking...'; }

  const resp = await fetch('/api/fingerprint/bulk-link', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (resp.ok) {
    _fpLoaded = false;  // a profile was linked or created; refetch the Speakers view next visit
    row.remove();
    _fpMatchGroups = _fpMatchGroups.filter(g => g.name.toLowerCase() !== name.toLowerCase());
    if (_fpMatchGroups.length === 0) {
      const empty = document.getElementById('fp-match-empty');
      const actions = document.getElementById('fp-match-actions');
      if (empty) empty.classList.remove('hidden');
      if (actions) actions.classList.add('hidden');
    }
  } else {
    if (btn) { btn.disabled = false; btn.textContent = 'Link'; }
  }
}

async function fpBulkLinkAll() {
  const rows = document.querySelectorAll('#fp-match-scroll .fp-match-row');
  const mappings = [];
  rows.forEach(row => {
    const name = row.dataset.name;
    const sel = row.querySelector('.fp-match-select');
    const value = sel ? sel.value : '';
    if (!value) return;
    if (value === '__new__') {
      mappings.push({ name, create_new: true });
    } else {
      mappings.push({ name, global_id: value });
    }
  });

  if (mappings.length === 0) {
    uiToast({ message: 'Select at least one profile mapping to apply.', kind: 'warn' });
    return;
  }

  const btn = document.querySelector('#fp-match-actions button');
  if (btn) { btn.disabled = true; btn.textContent = 'Applying...'; }

  const resp = await fetch('/api/fingerprint/bulk-link-all', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mappings }),
  });

  if (resp.ok) {
    _fpLoaded = false;  // profiles were linked or created; refetch the Speakers view next visit
    await fpLoadUnlinked();
  }
  if (btn) { btn.disabled = false; btn.textContent = 'Apply All'; }
}

async function _fpMatchGoToSessions(speakerName, anchorEl) {
  // Dismiss any existing popup
  document.querySelectorAll('.fp-match-session-popup').forEach(el => el.remove());

  const resp = await fetch(`/api/fingerprint/unlinked-sessions?name=${encodeURIComponent(speakerName)}`);
  if (!resp.ok) return;
  const data = await resp.json();
  const sessions = data.sessions || [];
  if (!sessions.length) return;

  // Single session - jump directly
  if (sessions.length === 1) {
    closeFingerprintPanel();
    loadSession(sessions[0].session_id);
    return;
  }

  // Multiple sessions - show popup anchored to the name
  const popup = document.createElement('div');
  popup.className = 'fp-match-session-popup';

  sessions.forEach(s => {
    const btn = document.createElement('button');
    btn.className = 'fp-match-session-item';
    const date = s.started_at ? new Date(s.started_at).toLocaleDateString() : '';
    btn.innerHTML = `<span class="fp-match-session-title">${escapeHtml(s.title || 'Untitled')}</span>`
      + (date ? `<span class="fp-match-session-date">${date}</span>` : '');
    btn.addEventListener('click', () => {
      popup.remove();
      closeFingerprintPanel();
      loadSession(s.session_id);
    });
    popup.appendChild(btn);
  });

  // Position near the anchor
  const row = anchorEl.closest('.fp-match-row');
  row.style.position = 'relative';
  row.appendChild(popup);

  // Close on outside click
  const dismiss = (e) => {
    if (!popup.contains(e.target) && e.target !== anchorEl) {
      popup.remove();
      document.removeEventListener('mousedown', dismiss);
    }
  };
  setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
}

function clearSpeakerSelection() {
  _selectedSpeakerKeys = [];
  _speakerSelectionAnchor = null;
  _syncSpeakerDraftFromSelection();
  _highlightSelectedSpeakerBadges();
  renderSpeakerManager();
}

/* ── Transcript segment multi-select ─────────────────────────────────────── */

function _toggleTranscriptSegSelection(segEl, { range = false } = {}) {
  if (range && _transcriptSelectionAnchor) {
    const allSegs = _segmentRegistry;
    const fromIdx = allSegs.indexOf(_transcriptSelectionAnchor);
    const toIdx   = allSegs.indexOf(segEl);
    if (fromIdx !== -1 && toIdx !== -1) {
      const [start, end] = fromIdx <= toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
      allSegs.slice(start, end + 1).forEach(el => {
        if (el.style.display !== 'none') _transcriptSelectedSegs.add(el);
      });
    }
  } else if (_transcriptSelectedSegs.has(segEl)) {
    _transcriptSelectedSegs.delete(segEl);
  } else {
    _transcriptSelectedSegs.add(segEl);
  }
  if (!range) _transcriptSelectionAnchor = segEl;
  _updateTranscriptSelectionUI();
  // Reassignment is driven by the same speaker-picker widget used for renaming,
  // anchored to the right of the just-clicked pill so the column underneath
  // stays clickable for growing the selection.
  if (_transcriptSelectedSegs.size > 0) _openBulkSpeakerPicker(segEl);
}

function _updateTranscriptSelectionUI() {
  _segmentRegistry.forEach(seg => {
    seg.classList.toggle('transcript-seg-selected', _transcriptSelectedSegs.has(seg));
  });
  if (_transcriptSelectedSegs.size > 0) {
    _bulkPickerUpdateCount(_transcriptSelectedSegs.size);
  } else {
    _closeBulkSpeakerPicker();
  }
  _syncPanelBottomRadius();
}

/* ── Bulk-reassign speaker picker ───────────────────────────────────────────
 * Opened on ctrl/⌘/shift-click of a transcript speaker pill. Reuses the same
 * .speaker-picker widget as single-segment renaming (similarity-ranked meeting
 * speakers + Voice Library + Mark as Noise), but committing reassigns every
 * currently-selected segment. It pops out to the right of the clicked pill and
 * stays open while the user keeps ctrl/⌘/shift-clicking more pills.
 */
let _bulkPicker = null;   // { el, input, hint, anchorKey } or null

function _openBulkSpeakerPicker(anchorSeg) {
  const badge = anchorSeg.querySelector('.src-badge');
  if (!badge) return;
  // Already open → just follow the latest click and refresh the count.
  if (_bulkPicker) {
    _bulkPickerUpdateCount(_transcriptSelectedSegs.size);
    _positionPicker(_bulkPicker.el, badge.getBoundingClientRect(), 'right');
    return;
  }
  document.querySelector('.speaker-picker')?.remove();  // close any single-rename picker

  const anchorKey = badge.dataset.speakerKey || anchorSeg.dataset.transcriptSource || '';
  const color = _speakerColors[anchorKey] || speakerColor(anchorKey) || '#58a6ff';

  const picker = document.createElement('div');
  picker.className = 'speaker-picker speaker-picker-bulk';
  picker.style.borderColor = color + '80';

  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'speaker-picker-input';
  input.placeholder = 'Reassign to…';
  input.style.borderColor = color + '60';
  input.style.color = color;
  picker.appendChild(input);

  const optionsWrap = document.createElement('div');
  optionsWrap.className = 'speaker-picker-options';
  picker.appendChild(optionsWrap);

  const commit = name => {
    const val = (name || '').trim();
    if (!val) return;
    _closeBulkSpeakerPicker();
    _bulkReassignSelectedTo(val);  // clears the selection when done
  };

  const { filterOpts } = _buildPickerSpeakerOptions(optionsWrap, {
    currentName: '', excludeKey: '', srcKey: anchorKey, baseColor: color, onPick: commit,
  });

  const noiseBtn = document.createElement('button');
  noiseBtn.className = 'speaker-picker-noise-btn';
  noiseBtn.innerHTML = '<i class="fa-solid fa-volume-xmark"></i> Mark as Noise';
  noiseBtn.addEventListener('mousedown', e => { e.preventDefault(); commit(_NOISE_LABEL); });
  picker.appendChild(noiseBtn);

  const hint = document.createElement('div');
  hint.className = 'speaker-picker-hint';
  picker.appendChild(hint);

  _bulkPicker = { el: picker, input, hint, anchorKey };
  _pickerOpen = true;
  document.body.appendChild(picker);
  _bulkPickerUpdateCount(_transcriptSelectedSegs.size);
  _positionPicker(picker, badge.getBoundingClientRect(), 'right');
  input.focus();

  input.addEventListener('input', () => filterOpts(input.value.trim().toLowerCase()));
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); commit(input.value); }
    if (e.key === 'Escape') { e.preventDefault(); clearTranscriptSelection(); }
  });

  // Defer wiring the outside-click guard so the click that opened us doesn't
  // immediately close it.
  requestAnimationFrame(() => {
    document.addEventListener('mousedown', _bulkPickerOutside, true);
    document.addEventListener('keydown', _bulkPickerKey, true);
  });
}

function _bulkPickerUpdateCount(count) {
  if (_bulkPicker) _bulkPicker.hint.textContent = `Reassign ${count} segment${count === 1 ? '' : 's'}`;
}

function _closeBulkSpeakerPicker() {
  document.removeEventListener('mousedown', _bulkPickerOutside, true);
  document.removeEventListener('keydown', _bulkPickerKey, true);
  if (_bulkPicker) {
    _bulkPicker.el.remove();
    _bulkPicker = null;
    _pickerOpen = false;
  }
}

function _bulkPickerOutside(e) {
  if (!_bulkPicker) return;
  if (_bulkPicker.el.contains(e.target)) return;
  // Keep the picker open while the user grows the selection by
  // ctrl/⌘/shift-clicking more pills; only a plain click elsewhere cancels.
  if (e.target.closest('.src-badge') && (e.ctrlKey || e.metaKey || e.shiftKey)) return;
  clearTranscriptSelection();  // clears selection → closes the picker
}

function _bulkPickerKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); clearTranscriptSelection(); }
}

let _tsbVoiceLibraryCache = null;
let _tsbVoiceLibraryFetching = false;

function _tsbEnsureVoiceLibrary() {
  if (_tsbVoiceLibraryCache !== null || _tsbVoiceLibraryFetching) return;
  _tsbVoiceLibraryFetching = true;
  fetch('/api/fingerprint/speakers')
    .then(r => r.json())
    .then(speakers => {
      _tsbVoiceLibraryCache = (speakers || []).map(sp => ({
        name: (sp.name || '').trim(),
        color: sp.color || 'var(--fg-muted)',
        isVoiceLib: true,
      })).filter(s => s.name);
      // Re-trigger autocomplete if input is focused
      if (document.activeElement === document.getElementById('tsb-input')) {
        _tsbFilterAutocomplete();
      }
    })
    .catch(() => { _tsbVoiceLibraryCache = []; })
    .finally(() => { _tsbVoiceLibraryFetching = false; });
}

function _tsbGetSpeakerNames() {
  // Session speakers (highest priority)
  const meeting = [];
  const seen = new Set();
  _getSortedSpeakerProfiles().forEach(p => {
    const name = (p.name || '').trim();
    if (!name || seen.has(name.toLowerCase())) return;
    if (!p.custom && _isDefaultName(name)) return;
    seen.add(name.toLowerCase());
    const color = p.color || _speakerColors[p.speaker_key] || speakerColor(p.speaker_key);
    meeting.push({ name, color, section: 'meeting' });
  });
  meeting.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));

  // Voice Library speakers (not already in meeting)
  const voiceLib = [];
  if (_tsbVoiceLibraryCache) {
    _tsbVoiceLibraryCache.forEach(sp => {
      if (seen.has(sp.name.toLowerCase())) return;
      voiceLib.push({ name: sp.name, color: sp.color, section: 'voicelib' });
    });
    voiceLib.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  }

  return { meeting, voiceLib };
}

function _tsbFilterAutocomplete() {
  const input = document.getElementById('tsb-input');
  const list = document.getElementById('tsb-autocomplete');
  if (!input || !list) return;

  const query = input.value.trim().toLowerCase();
  const { meeting, voiceLib } = _tsbGetSpeakerNames();

  const filterFn = n => !query || n.name.toLowerCase().includes(query);
  const filteredMeeting = meeting.filter(filterFn);
  const filteredVL = voiceLib.filter(filterFn);
  const noiseMatch = !query || 'noise'.includes(query);

  list.innerHTML = '';
  if (filteredMeeting.length === 0 && filteredVL.length === 0 && !noiseMatch) {
    list.classList.add('hidden');
    return;
  }

  // Meeting speakers section
  if (filteredMeeting.length > 0) {
    const header = document.createElement('div');
    header.className = 'tsb-ac-section';
    header.textContent = 'Meeting Speakers';
    list.appendChild(header);
    filteredMeeting.forEach(entry => {
      list.appendChild(_tsbCreateOpt(entry, input, list));
    });
  }

  // Voice Library section
  if (filteredVL.length > 0) {
    const header = document.createElement('div');
    header.className = 'tsb-ac-section';
    header.textContent = 'Voice Library';
    list.appendChild(header);
    filteredVL.forEach(entry => {
      list.appendChild(_tsbCreateOpt(entry, input, list));
    });
  }

  // Noise option
  if (noiseMatch) {
    if (filteredMeeting.length > 0 || filteredVL.length > 0) {
      const sep = document.createElement('div');
      sep.className = 'tsb-ac-sep';
      list.appendChild(sep);
    }
    const noiseOpt = document.createElement('button');
    noiseOpt.className = 'tsb-ac-opt tsb-ac-noise';
    noiseOpt.innerHTML = `<i class="fa-solid fa-volume-xmark tsb-ac-noise-icon"></i>Mark as Noise`;
    noiseOpt.addEventListener('mousedown', e => {
      e.preventDefault();
      input.value = _NOISE_LABEL;
      list.classList.add('hidden');
    });
    list.appendChild(noiseOpt);
  }

  list.classList.remove('hidden');
}

function _tsbCreateOpt(entry, input, list) {
  const opt = document.createElement('button');
  opt.className = 'tsb-ac-opt';
  opt.innerHTML = `<span class="tsb-ac-dot" style="background:${entry.color}"></span>${escapeHtml(entry.name)}`;
  opt.style.color = entry.color;
  opt.addEventListener('mousedown', e => {
    e.preventDefault();
    input.value = entry.name;
    list.classList.add('hidden');
  });
  return opt;
}

// Wire up autocomplete events (called once on page load)
function _tsbInitAutocomplete() {
  const input = document.getElementById('tsb-input');
  if (!input) return;
  input.addEventListener('input', _tsbFilterAutocomplete);
  input.addEventListener('focus', _tsbFilterAutocomplete);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); applyTranscriptBulkReassign(); }
    if (e.key === 'Escape') { document.getElementById('tsb-autocomplete')?.classList.add('hidden'); }
  });
  // Close on outside click
  document.addEventListener('mousedown', e => {
    if (!document.getElementById('tsb-input-wrap')?.contains(e.target)) {
      document.getElementById('tsb-autocomplete')?.classList.add('hidden');
    }
  });
}

function clearTranscriptSelection() {
  _transcriptSelectedSegs.clear();
  _transcriptSelectionAnchor = null;
  _updateTranscriptSelectionUI();
}

function applyTranscriptBulkReassign() {
  const input = document.getElementById('tsb-input');
  document.getElementById('tsb-autocomplete')?.classList.add('hidden');
  _bulkReassignSelectedTo((input?.value || '').trim());
}

// Reassign every currently-selected segment to `name` (or mark them as Noise
// when name is the noise sentinel). Shared by the inline bulk picker and the
// legacy selection bar.
function _bulkReassignSelectedTo(name) {
  if (!name) return;

  // Resolve the target speaker_key for the given display name.
  // If a speaker with this name already exists, reuse their key so
  // the reassigned segments group properly in filters/analytics.
  // If the name is brand new, create a custom speaker key + profile.
  let targetKey = _speakerNameKey(name)
    || _getSortedSpeakerProfiles().find(p =>
        (_speakerDisplayName(p.speaker_key) || p.speaker_key).toLowerCase() === name.toLowerCase()
      )?.speaker_key
    || null;

  if (!targetKey && name !== _NOISE_LABEL) {
    targetKey = `custom:${Date.now()}`;
    applySpeakerProfileUpdate({ speaker_key: targetKey, name });
    if (_speakerProfiles[targetKey]) _speakerProfiles[targetKey].custom = true;
    persistSpeakerLabel(targetKey, name).catch(() => {});
  }

  for (const segEl of _transcriptSelectedSegs) {
    const source = segEl.dataset.transcriptSource;
    if (!source || source in SOURCE_META) continue;

    const badge = segEl.querySelector('.src-badge');
    if (!badge) continue;
    const segId = badge.dataset.segId || segEl.dataset.segId;

    if (name === _NOISE_LABEL) {
      _manualNoiseKeys.add(source);
      if (badge) _applyNoiseStyle(segEl, badge, segId);
      if (segId) persistSegmentOverride(segId, _NOISE_LABEL).catch(() => {});
      continue;
    }

    // Per-segment reassignment: update DOM source attribution + visual
    const newKey = targetKey || source;  // fall back to original key if no match
    if (newKey !== source) segEl.dataset.originalSource = source;
    segEl.dataset.transcriptSource = newKey;
    _ensureSpeakerProfile(newKey);
    const color = speakerColor(newKey);
    segEl.style.setProperty('--seg-color', color);

    // If this was a noise segment, restore normal styling
    if (segEl.classList.contains('noise-segment')) {
      if (_manualNoiseKeys.has(source)) {
        const remaining = document.querySelectorAll(
          `#transcript .transcript-segment[data-transcript-source="${source}"] .src-noise`
        ).length;
        if (remaining <= 1) _manualNoiseKeys.delete(source);
      }
      segEl.classList.remove('noise-segment');
    }

    badge.className = 'src-badge src-speaker';
    badge.textContent = name;
    badge.dataset.speakerKey = newKey;
    badge.dataset.override = '1';
    badge.title = 'Click to rename';
    badge.style.backgroundColor = color + '26';
    badge.style.color = color;
    badge.style.borderColor = color + '60';

    // Re-wire badge click handler (clone to clear old listeners)
    const fresh = badge.cloneNode(true);
    fresh.addEventListener('click', (function(k) {
      return function(e) {
        if (e.ctrlKey || e.metaKey || e.shiftKey) {
          e.preventDefault(); e.stopPropagation();
          _toggleTranscriptSegSelection(segEl, { range: e.shiftKey });
          return;
        }
        editSpeakerLabel(fresh, k);
      };
    })(newKey));
    badge.replaceWith(fresh);

    if (segId) persistSegmentOverride(segId, name, newKey !== source ? newKey : null).catch(() => {});
  }

  clearTranscriptSelection();
  applyTranscriptFilter();
  _tnRefreshSpeakerPills();
  _tnRefreshReassignDropdowns();
}

/* === SPEAKER-MODAL-SHELL START ===========================================
 * One shell for the Speakers modal: a shared header (meeting + status line +
 * tab legend), tab badges, deterministic landing, and the single voice-sample
 * player every tab routes through. Everything below is scoped to the modal.
 * ========================================================================= */

// ── One voice-sample player for the whole modal ─────────────────────────────
// Manage, Cleanup and Resolve used to each carry their own <audio> and their
// own play/stop bookkeeping, so two samples could overlap. playSpeakerVoice is
// now the single owner: one private <audio>, one cancellation token, one
// "playing" button at a time.
let _voiceSampleAudio = null;
let _voicePlayToken = 0;
let _voicePlayButton = null;
let _voicePlayRender = null;
let _voiceMetaHandler = null;       // pending loadedmetadata listener, so it is never doubled

const _VOICE_CLIP_DEFAULTS = { maxTotalSec: 9, maxClipSec: 6, maxClips: 4 };

function _voiceDefaultRender(btn, playing) {
  btn.classList.toggle('playing', playing);
  btn.innerHTML = playing ? '<i class="fa-solid fa-stop"></i>' : '<i class="fa-solid fa-play"></i>';
}

function _voiceSetButton(btn, playing, render) {
  if (!btn) return;
  (render || _voiceDefaultRender)(btn, playing);
}

/** Stop whatever voice sample is playing, wherever it was started from. */
function stopSpeakerVoice() {
  _voicePlayToken += 1;
  if (_voiceSampleAudio) {
    try { _voiceSampleAudio.pause(); } catch (_) {}
    // A cancelled play must not leave its metadata listener attached.
    if (_voiceMetaHandler) {
      _voiceSampleAudio.removeEventListener('loadedmetadata', _voiceMetaHandler);
      _voiceMetaHandler = null;
    }
  }
  if (_voicePlayButton) _voiceSetButton(_voicePlayButton, false, _voicePlayRender);
  _voicePlayButton = null;
  _voicePlayRender = null;
  // Belt and braces: any stale "playing" affordance left by an earlier render.
  document.querySelectorAll('.speaker-row-play.playing').forEach(b => _voiceDefaultRender(b, false));
}

// Kept as the historical name used elsewhere in this file.
function _mgrStopVoice() { stopSpeakerVoice(); }

function _voiceAudioEl(sessionId) {
  if (!_voiceSampleAudio) {
    _voiceSampleAudio = new Audio();
    _voiceSampleAudio.preload = 'auto';
    // ONE permanent error handler. A per-play {once:true} listener only detaches
    // when it actually fires, so on a healthy file they piled up one per click.
    _voiceSampleAudio.addEventListener('error', () => stopSpeakerVoice());
  }
  const src = `/api/sessions/${encodeURIComponent(sessionId)}/audio`;
  if (!_voiceSampleAudio.src || _voiceSampleAudio.src.indexOf(src) === -1) _voiceSampleAudio.src = src;
  return _voiceSampleAudio;
}

/** Pick the longest few clips that add up to a short, representative sample. */
function _pickVoiceClips(segments, opts) {
  const o = Object.assign({}, _VOICE_CLIP_DEFAULTS, opts || {});
  const usable = (segments || [])
    .map(s => ({ start: Number(s.start), end: Number(s.end) }))
    .filter(s => isFinite(s.start) && isFinite(s.end) && s.end - s.start > 0.15)
    .sort((a, b) => (b.end - b.start) - (a.end - a.start));
  const picked = [];
  let total = 0;
  for (const s of usable) {
    const dur = Math.min(s.end - s.start, o.maxClipSec);
    picked.push({ start: s.start, end: s.start + dur });
    total += dur;
    if (total >= o.maxTotalSec || picked.length >= o.maxClips) break;
  }
  picked.sort((a, b) => a.start - b.start);
  return picked;
}

/* Fetch a speaker's segments by raw diarizer KEY, never by display name: the
 * agent transcript matcher does a name-substring match, so querying by name
 * ("Alex") would wrongly pull another speaker's audio ("Alex Chen"). A Manage
 * row can cover several keys (a name-group), so gather across all of them and
 * keep only segments whose source really is one of those keys. */
async function _fetchVoiceSegments(sessionId, speakerKeys) {
  const wanted = new Set((speakerKeys || []).map(k => String(k).trim().toLowerCase()));
  const segs = [];
  for (const key of (speakerKeys || [])) {
    const url = `/api/agent/v1/meetings/${encodeURIComponent(sessionId)}/transcript` +
      `?speaker=${encodeURIComponent(key)}&format=json&limit=60`;
    let data;
    try { const r = await fetch(url); if (!r.ok) continue; data = await r.json(); }
    catch (_) { continue; }
    for (const s of (data.segments || [])) {
      const src = String(s.source || '').trim().toLowerCase();
      if (src && !wanted.has(src)) continue;   // matcher can leak on name-substring
      segs.push(s);
    }
  }
  return segs;
}

function _voicePlaySeq(audio, clips, idx, token) {
  if (_voicePlayToken !== token) return;
  if (idx >= clips.length) { stopSpeakerVoice(); return; }
  const clip = clips[idx];
  try { audio.currentTime = clip.start; } catch (_) {}
  audio.play().catch(() => { if (_voicePlayToken === token) stopSpeakerVoice(); });
  let timer = 0;
  const onTU = () => {
    if (_voicePlayToken !== token) { audio.removeEventListener('timeupdate', onTU); return; }
    if (audio.currentTime >= clip.end) {
      audio.removeEventListener('timeupdate', onTU);
      if (timer) clearTimeout(timer);
      _voicePlaySeq(audio, clips, idx + 1, token);
    }
  };
  audio.addEventListener('timeupdate', onTU);
  // Safety net if timeupdate stalls (tab blur / decode hiccup).
  timer = setTimeout(() => {
    audio.removeEventListener('timeupdate', onTU);
    _voicePlaySeq(audio, clips, idx + 1, token);
  }, (clip.end - clip.start + 0.7) * 1000);
}

/**
 * Play a short voice sample for one speaker (or one name-group of keys).
 *
 * opts: { sessionId, speakerKeys[], segments?, maxTotalSec, maxClipSec,
 *         button, render(btn, playing) }
 * Clicking the same button again stops. Starting a sample stops the Cleanup
 * segment queue too, so only one thing is ever audible in the modal.
 */
async function playSpeakerVoice(opts) {
  const o = opts || {};
  const btn = o.button || null;
  const render = o.render || _voiceDefaultRender;
  const sessionId = o.sessionId || state.sessionId;
  const keys = (o.speakerKeys || []).filter(Boolean);

  const alreadyPlaying = btn && btn === _voicePlayButton;
  stopSpeakerVoice();
  if (typeof _cleanupStopPlayback === 'function') { try { _cleanupStopPlayback(); } catch (_) {} }
  if (alreadyPlaying) return;                       // click again to stop
  if (!sessionId || (!keys.length && !o.segments)) return;

  const token = ++_voicePlayToken;
  _voicePlayButton = btn;
  _voicePlayRender = render;
  _voiceSetButton(btn, true, render);

  let raw = o.segments;
  if (!raw) {
    try { raw = await _fetchVoiceSegments(sessionId, keys); }
    catch (_) { if (_voicePlayToken === token) stopSpeakerVoice(); return; }
  }
  if (_voicePlayToken !== token) return;             // toggled/superseded while fetching

  const clips = _pickVoiceClips(raw, { maxTotalSec: o.maxTotalSec, maxClipSec: o.maxClipSec });
  if (!clips.length) { stopSpeakerVoice(); return; }

  const audio = _voiceAudioEl(sessionId);
  const begin = () => _voicePlaySeq(audio, clips, 0, token);
  if (isFinite(audio.duration) && audio.duration > 0) begin();
  else {
    // Drop any metadata handler a superseded call left behind before adding ours.
    if (_voiceMetaHandler) audio.removeEventListener('loadedmetadata', _voiceMetaHandler);
    _voiceMetaHandler = () => {
      audio.removeEventListener('loadedmetadata', _voiceMetaHandler);
      _voiceMetaHandler = null;
      if (_voicePlayToken === token) begin();
    };
    audio.addEventListener('loadedmetadata', _voiceMetaHandler);
    audio.load();
  }
}

// Exposed on window for scripts loaded separately from this file.
window.playSpeakerVoice = playSpeakerVoice;
window.stopSpeakerVoice = stopSpeakerVoice;

function playManageSpeakerVoice(keys, btn, ev) {
  if (ev) ev.stopPropagation();
  return playSpeakerVoice({ speakerKeys: keys, button: btn });
}


/* ── Shared modal header, status line and tab badges ────────────────────────
 * All three tabs describe the same set of speakers, so the header states that
 * set once using the product definition of "needs attention" (core/attention.py):
 * a non-noise speaker has material content when talk time >= min-seconds OR
 * word count >= min-words; a material speaker with a generic name is
 * unresolved; a generic speaker below both thresholds is a diarizer phantom
 * (a low-content fragment) and never flags the meeting.
 * ------------------------------------------------------------------------- */

let _speakerModalStats = null;      // { total, named, unresolved, fragments }
let _speakerModalStatsSession = null;
let _speakerModalLastTab = null;    // remembered per session view
let _speakerModalStatsToken = 0;

function _speakerAttentionThresholds() {
  const prefs = (typeof _prefs === 'object' && _prefs) ? _prefs : {};
  const seconds = Number(prefs.obsidian_gate_min_seconds);
  const words = Number(prefs.obsidian_gate_min_words);
  return {
    minSeconds: isFinite(seconds) && seconds > 0 ? seconds : 15,
    minWords:   isFinite(words) && words > 0 ? words : 25,
  };
}

/** Same shape core/attention.py computes, from a /speakers payload. */
function _computeSpeakerAttention(speakers) {
  const { minSeconds, minWords } = _speakerAttentionThresholds();
  let named = 0, unresolved = 0, fragments = 0;
  for (const sp of (speakers || [])) {
    if (sp.is_noise) continue;
    const key = String(sp.speaker_key || '');
    if (key === _NOISE_LABEL || _isNoiseKey(key)) continue;
    const material = (Number(sp.talk_seconds) || 0) >= minSeconds
                  || (Number(sp.word_count) || 0) >= minWords;
    const label = String(sp.name || '').trim();
    const generic = !label || _GENERIC_SPEAKER_RE.test(label);
    if (material) {
      if (generic) unresolved += 1; else named += 1;
    } else if (generic) {
      fragments += 1;
    }
  }
  return { total: named + unresolved, named, unresolved, fragments };
}

function _plural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }

function _speakerStatusText(stats) {
  if (!stats) return 'Reading speaker stats…';
  if (!stats.total && !stats.fragments) return 'No diarized speakers in this meeting yet.';
  const parts = [];
  if (stats.named) parts.push(`${stats.named} named`);
  if (stats.unresolved) parts.push(`${_plural(stats.unresolved, 'needs', 'need')} attention`);
  if (stats.fragments) parts.push(_plural(stats.fragments, 'low-content fragment', 'low-content fragments'));
  const head = _plural(stats.total, 'speaker', 'speakers');
  return parts.length ? `${head}: ${parts.join(', ')}` : head;
}

/** Repaint the meeting title, the status line and both tab badges. */
function _paintSpeakerModalHeader() {
  const meetingEl = document.getElementById('speaker-manager-meeting');
  if (meetingEl) {
    // The topbar title is only painted for a live recording; a past meeting's
    // title comes from the session list the sidebar already holds.
    let title = (document.getElementById('topbar-session-title')?.textContent || '').trim();
    if (!title && state.sessionId && Array.isArray(_sidebarAllSessions)) {
      title = ((_sidebarAllSessions.find(s => s.id === state.sessionId) || {}).title || '').trim();
    }
    meetingEl.textContent = title;
    meetingEl.hidden = !title;
  }
  const statusEl = document.getElementById('speaker-manager-status');
  if (statusEl) {
    statusEl.textContent = _speakerStatusText(_speakerModalStats);
    statusEl.classList.toggle('has-attention', !!(_speakerModalStats && _speakerModalStats.unresolved));
  }
  _paintSpeakerTabBadges();
}

function _paintSpeakerTabBadges() {
  _cleanupPaintQuickBadge();
  const dirtyDot = document.getElementById('speaker-cleanup-dirty');
  if (dirtyDot) dirtyDot.hidden = !(_cleanupState && _cleanupState.dirty);
}

/** Reload the shared stats for the current session, then repaint. */
async function refreshSpeakerModalHeader(force = false) {
  _paintSpeakerModalHeader();
  const sid = state.sessionId;
  if (!sid) { _speakerModalStats = null; _speakerModalStatsSession = null; _paintSpeakerModalHeader(); return; }
  if (!force && _speakerModalStatsSession === sid && _speakerModalStats) return;
  const token = ++_speakerModalStatsToken;
  try {
    const r = await fetch(`/api/agent/v1/meetings/${encodeURIComponent(sid)}/speakers`);
    if (!r.ok) return;
    const payload = await r.json();
    if (token !== _speakerModalStatsToken) return;
    const speakers = Array.isArray(payload) ? payload : (payload.speakers || []);
    _speakerModalStats = _computeSpeakerAttention(speakers);
    _speakerModalStatsSession = sid;
  } catch (_) { /* header degrades to the placeholder text */ }
  _paintSpeakerModalHeader();
}

/** Any tab that writes a speaker name calls this so the shared header agrees. */
function onSpeakerDataChanged() {
  refreshSpeakerModalHeader(true);
}
window.onSpeakerDataChanged = onSpeakerDataChanged;

/* ── Cleanup: pending-change count for the sticky footer ─────────────────── */

// Three kinds of staged edit have to be counted, not just one: a member moved
// between groups or into noise, and a group whose IDENTITY changed (linked to a
// profile, unlinked, or given a new typed name). Counting membership alone made
// every rename read as zero pending changes.
function _cleanupPendingChangeCount() {
  if (!_cleanupState) return 0;
  const snap = _cleanupState.originalSnapshot || {};
  const clusterSnap = _cleanupState.clusterSnapshot || {};
  let changed = 0;
  for (const cluster of _cleanupState.clusters) {
    for (const m of cluster.members) {
      const before = snap[m.speaker_key];
      const wasNoise = !!(before && before.is_noise);
      const nowNoise = _cleanupState.noiseKeys.has(m.speaker_key);
      if (!before || before.cluster_id !== cluster.cluster_id || wasNoise !== nowNoise) changed += 1;
    }
    const identityBefore = clusterSnap[cluster.cluster_id];
    const identityNow = {
      global_id: cluster.global_id || null,
      name:      cluster.name || '',
      new_name:  cluster.new_name || '',
    };
    if (!identityBefore) {
      // A group created during this edit only counts on its own when it has
      // been given an identity; otherwise its members already counted above.
      if (identityNow.global_id || identityNow.new_name) changed += 1;
    } else if (identityBefore.global_id !== identityNow.global_id
            || identityBefore.name !== identityNow.name
            || identityBefore.new_name !== identityNow.new_name) {
      changed += 1;
    }
  }
  // Keys that live only in the noise bucket now but were not noise before.
  if (_cleanupState.noiseMembers) {
    for (const key of _cleanupState.noiseKeys) {
      if (!_cleanupState.noiseMembers.has(key)) continue;
      const before = snap[key];
      if (!before || !before.is_noise) changed += 1;
    }
  }
  return changed;
}

function _cleanupSyncFooter() {
  const statusEl = document.getElementById('cleanup-footer-status');
  const applyBtn = document.getElementById('cleanup-apply-btn');
  const resetBtn = document.getElementById('cleanup-reset-btn');
  const dirty = !!(_cleanupState && _cleanupState.dirty);
  const pending = dirty ? Math.max(_cleanupPendingChangeCount(), 1) : 0;
  if (statusEl) {
    statusEl.textContent = dirty
      ? `${_plural(pending, 'pending change', 'pending changes')}, not written yet`
      : 'No pending changes';
    statusEl.classList.toggle('is-dirty', dirty);
  }
  if (applyBtn && !applyBtn.dataset.busy) {
    applyBtn.disabled = !dirty;
    applyBtn.innerHTML = dirty
      ? `<i class="fa-solid fa-check"></i> Apply ${_plural(pending, 'change', 'changes')}`
      : '<i class="fa-solid fa-check"></i> Apply';
  }
  if (resetBtn) resetBtn.disabled = !dirty;
  const dirtyDot = document.getElementById('speaker-cleanup-dirty');
  if (dirtyDot) dirtyDot.hidden = !dirty;
}

/* ── Voice Library combobox (Manage tab) ─────────────────────────────────── */

let _voiceProfiles = [];            // [{ id, name, color, emb_count }]
let _voiceProfilesLoaded = false;
let _mgrNameCombo = null;           // uiCombobox controller for the Manage editor
let _mgrCommittedName = '';         // last value written to the server

async function _loadVoiceProfiles(force = false) {
  if (_voiceProfilesLoaded && !force) return _voiceProfiles;
  try {
    const r = await fetch('/api/fingerprint/speakers');
    const data = await r.json();
    _voiceProfiles = Array.isArray(data) ? data : [];
    _voiceProfilesLoaded = true;
  } catch (_) { _voiceProfiles = []; }
  if (_mgrNameCombo) _mgrNameCombo.setItems(_voiceComboItems());
  return _voiceProfiles;
}

function _voiceComboItems() {
  // list_global_speakers exposes emb_count (voice samples on file), not a
  // meeting count, so the row says what the number actually is.
  return _voiceProfiles
    .filter(p => p && p.name)
    .map(p => ({
      id: p.id,
      label: p.name,
      color: p.color || null,
      sublabel: _plural(Number(p.emb_count) || 0, 'voice sample', 'voice samples'),
    }));
}

function _mgrSetUnsaved(dirty) {
  const el = document.getElementById('speaker-editor-unsaved');
  if (el) el.hidden = !dirty;
  const save = document.getElementById('speaker-save-btn');
  if (save) save.classList.toggle('is-dirty', !!dirty);
}

function _mgrRefreshUnsaved() {
  const typed = (_mgrNameCombo ? _mgrNameCombo.getValue() : '').trim();
  _mgrSetUnsaved(!!_selectedSpeakerKeys.length && typed !== (_mgrCommittedName || '').trim());
}

// True while the user is actually typing in the Manage name field. Background
// re-renders (a new transcript segment, a speaker_label or fingerprint event)
// call renderSpeakerManager, and blindly resyncing the field there would move
// _mgrCommittedName up to the uncommitted draft, which silently swallows the
// pending rename on the next change event.
function _mgrNameFieldIsFocused() {
  return !!(_mgrNameCombo && document.activeElement === _mgrNameCombo.input);
}

/** Build (once) the Manage name combobox and keep its value in sync. */
function _mgrEnsureNameCombo() {
  const mount = document.getElementById('speaker-name-combo');
  if (!mount) return null;
  if (_mgrNameCombo) return _mgrNameCombo;
  if (typeof window.uiCombobox !== 'function') return null;
  _mgrNameCombo = uiCombobox({
    mount,
    placeholder: 'Speaker name, or pick a Voice Library profile',
    ariaLabel: 'Speaker name or Voice Library profile',
    emptyText: 'No Voice Library profiles yet. Type a name to use it.',
    allowTyped: true,
    typedLabel: 'Use typed name',
    items: _voiceComboItems(),
    onInput: value => { _speakerDraftName = value; _mgrRefreshUnsaved(); },
    onSelect: (item, meta) => {
      _speakerDraftName = item.label;
      if (meta.typed) {
        // Enter on "Use typed name" is a commit, not a stage. The combobox
        // preventDefaults that Enter, so the native change event never fires
        // and a second press would otherwise be needed to save.
        _mgrCommitTypedName();
        return;
      }
      linkSelectedSpeakersToProfile(item.id, item.label);
    },
  });
  // Enter and blur-after-change both commit, so Manage behaves like Resolve:
  // an edit you finish is an edit that is saved (with an Undo toast).
  _mgrNameCombo.input.addEventListener('change', _mgrCommitTypedName);
  _loadVoiceProfiles();
  return _mgrNameCombo;
}

/** Write the typed name through, or just refresh the indicator when there is
 *  nothing new to save. Shared by Enter, blur-after-change and Save changes. */
function _mgrCommitTypedName() {
  const typed = (_mgrNameCombo ? _mgrNameCombo.getValue() : '').trim();
  if (!typed || !_selectedSpeakerKeys.length || typed === (_mgrCommittedName || '').trim()) {
    _mgrRefreshUnsaved();
    return;
  }
  applySpeakerEditor();
}

/** Snapshot the selected rows so an Undo toast can put them back. */
function _mgrSnapshotSelection() {
  return _selectedSpeakerKeys.map(key => {
    const p = _speakerProfiles[key] || {};
    return { speaker_key: key, name: p.name || '', color: p.color || speakerColor(key) };
  });
}

async function _mgrRestoreSnapshot(snapshot) {
  if (!state.sessionId || !snapshot || !snapshot.length) return;
  for (const row of snapshot) {
    try {
      const resp = await fetch(`/api/sessions/${state.sessionId}/speakers`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speaker_keys: [row.speaker_key], name: row.name, color: row.color }),
      });
      const data = await resp.json();
      if (resp.ok) (data.speakers || []).forEach(applySpeakerProfileUpdate);
    } catch (_) { /* best effort */ }
  }
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
  onSpeakerDataChanged();
}

function _mgrToastSaved(message, snapshot) {
  uiToast({
    message,
    kind: 'success',
    id: 'speaker-manage-save',
    action: { label: 'Undo', onClick: () => _mgrRestoreSnapshot(snapshot) },
  });
}

/**
 * Link every selected speaker key to a Voice Library profile, then take the
 * profile's name. The link endpoint applies the profile name and colour itself
 * when apply_name is set (app.py fp_link_session_speaker), so no extra PATCH.
 */
async function linkSelectedSpeakersToProfile(globalId, profileName) {
  if (!state.sessionId) { uiToast({ message: 'Load a meeting first.', kind: 'warn' }); return; }
  if (!_selectedSpeakerKeys.length) {
    uiToast({ message: 'Select at least one speaker row first.', kind: 'warn' });
    return;
  }
  const snapshot = _mgrSnapshotSelection();
  const keys = [..._selectedSpeakerKeys];
  // Full prior binding per key, not just "was it linked": re-linking a speaker
  // that already pointed at another profile has to be undoable back to THAT
  // profile, otherwise Undo restores the name but leaves the voice binding on
  // the new one.
  const linksBefore = keys.map(k => ({ key: k, link: _sessionLinks[k] || null }));
  const profileColor = (_voiceProfiles.find(p => p.id === globalId) || {}).color || null;
  let failed = 0;
  for (const key of keys) {
    try {
      const resp = await fetch(`/api/fingerprint/sessions/${encodeURIComponent(state.sessionId)}/link`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ speaker_key: key, global_id: globalId, apply_name: true }),
      });
      if (!resp.ok) { failed += 1; continue; }
      _sessionLinks[key] = { global_id: globalId, name: profileName };
      // apply_name makes the server write the profile's name AND colour
      // (app.py fp_link_session_speaker); mirror both locally so the row does
      // not keep the old swatch until the next reload.
      const update = { speaker_key: key, name: profileName };
      if (profileColor) update.color = profileColor;
      applySpeakerProfileUpdate(update);
    } catch (_) { failed += 1; }
  }
  _updateLinkedBadges();
  _mgrCommittedName = profileName;
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
  onSpeakerDataChanged();
  if (failed) {
    uiToast({ message: `Could not link ${_plural(failed, 'speaker', 'speakers')} to ${profileName}.`, kind: 'error' });
    return;
  }
  uiToast({
    message: `Linked ${_plural(keys.length, 'speaker', 'speakers')} to ${profileName}.`,
    kind: 'success',
    id: 'speaker-manage-save',
    action: {
      label: 'Undo',
      onClick: async () => {
        for (const row of linksBefore) {
          try {
            await fetch(`/api/fingerprint/sessions/${encodeURIComponent(state.sessionId)}/link/${encodeURIComponent(row.key)}`, { method: 'DELETE' });
          } catch (_) {}
          delete _sessionLinks[row.key];
          if (!row.link || !row.link.global_id) continue;
          // Was bound to a different profile before: put that binding back.
          try {
            await fetch(`/api/fingerprint/sessions/${encodeURIComponent(state.sessionId)}/link`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ speaker_key: row.key, global_id: row.link.global_id }),
            });
            _sessionLinks[row.key] = row.link;
          } catch (_) {}
        }
        _updateLinkedBadges();
        await _mgrRestoreSnapshot(snapshot);
      },
    },
  });
}

/** Drop the Voice Library binding for the selected rows (names are kept). */
async function unlinkSelectedSpeakers() {
  if (!state.sessionId) return;
  const keys = _selectedSpeakerKeys.filter(k => _sessionLinks[k]);
  if (!keys.length) { uiToast({ message: 'No linked speaker selected.', kind: 'warn' }); return; }
  const ok = await uiConfirm({
    title: 'Unlink from the Voice Library?',
    message: `${_plural(keys.length, 'speaker', 'speakers')} will keep the current name but stop being recognised by voice in future meetings.`,
    details: keys,
    confirmLabel: 'Unlink',
  });
  if (!ok) return;
  const previous = keys.map(k => ({ key: k, link: _sessionLinks[k] }));
  for (const key of keys) {
    try {
      await fetch(`/api/fingerprint/sessions/${encodeURIComponent(state.sessionId)}/link/${encodeURIComponent(key)}`, { method: 'DELETE' });
      delete _sessionLinks[key];
    } catch (_) { /* leave the badge in place if it failed */ }
  }
  _updateLinkedBadges();
  renderSpeakerManager();
  uiToast({
    message: `Unlinked ${_plural(keys.length, 'speaker', 'speakers')}.`,
    kind: 'success',
    id: 'speaker-manage-save',
    action: {
      label: 'Undo',
      onClick: async () => {
        for (const row of previous) {
          if (!row.link || !row.link.global_id) continue;
          try {
            await fetch(`/api/fingerprint/sessions/${encodeURIComponent(state.sessionId)}/link`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ speaker_key: row.key, global_id: row.link.global_id }),
            });
            _sessionLinks[row.key] = row.link;
          } catch (_) {}
        }
        _updateLinkedBadges();
        renderSpeakerManager();
      },
    },
  });
}

/* ── Modal chrome: Escape to close, focus on open ────────────────────────── */

function _speakerModalIsOpen() {
  const ov = document.getElementById('speaker-manager-overlay');
  return !!ov && !ov.classList.contains('hidden');
}

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape' || !_speakerModalIsOpen()) return;
  // A cleanup picker popover handles its own Escape before this fires.
  if (_cleanupPicker) return;
  if (document.querySelector('.ui-dialog-overlay')) return;   // a confirm is up
  e.preventDefault();
  closeSpeakerManager();
});

/** Put the caret somewhere useful the moment the modal lands on a tab. */
function _speakerModalFocusTab(tab) {
  window.setTimeout(() => {
    if (!_speakerModalIsOpen()) return;
    if (tab === 'manage') {
      const combo = document.querySelector('#speaker-name-combo .ui-combobox-input');
      if (combo) { combo.focus(); return; }
    }
    document.getElementById(`speaker-tab-${tab}`)?.focus();
  }, 0);
}

/* === SPEAKER-MODAL-SHELL END ============================================= */

function renderSpeakerManager() {
  const listEl = document.getElementById('speaker-manager-list');
  const colorGridEl = document.getElementById('speaker-color-grid');
  const hintEl = document.getElementById('speaker-editor-hint');
  if (!listEl || !colorGridEl || !hintEl) return;

  const profiles = _getSortedSpeakerProfiles().filter(p => p.speaker_key !== _NOISE_LABEL);
  const groups = _groupProfilesByName(profiles);
  const selectedGroupCount = groups.filter(g => g.speakerKeys.some(k => _selectedSpeakerKeys.includes(k))).length;

  // Name field is a combobox over the Voice Library, not a bare datalist: the
  // first row uses whatever you typed, the rest bind this speaker to a saved
  // voice profile so it is recognised in future meetings.
  const combo = _mgrEnsureNameCombo();
  if (combo) {
    // Only overwrite the field when the user is NOT mid-edit.
    // renderSpeakerManager runs on every new transcript segment and on every
    // speaker_label / fingerprint event while the modal is open; resyncing
    // there would discard whatever is being typed. The indicator itself is
    // derived from (typed vs committed), so it is always safe to recompute.
    if (!_mgrNameFieldIsFocused()) {
      combo.setValue(_speakerDraftName);
      _mgrCommittedName = _speakerDraftName;
    }
    _mgrRefreshUnsaved();
  }

  colorGridEl.innerHTML = '';
  _SPEAKER_PALETTE.forEach(color => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'speaker-color-btn' + (_speakerDraftColor === color ? ' active' : '');
    btn.title = `Use ${color}`;
    btn.setAttribute('aria-label', `Use colour ${color}`);
    btn.style.backgroundColor = color;
    btn.addEventListener('click', async () => {
      _speakerDraftColor = color;
      // Manage is a direct-edit surface: the colour writes straight through,
      // exactly like the name field, and the toast carries the Undo.
      if (_selectedSpeakerKeys.length && state.sessionId) {
        const snapshot = _mgrSnapshotSelection();
        const resp = await fetch(`/api/sessions/${state.sessionId}/speakers`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ speaker_keys: _selectedSpeakerKeys, color }),
        });
        const data = await resp.json();
        if (resp.ok) {
          (data.speakers || []).forEach(applySpeakerProfileUpdate);
          _mgrToastSaved(`Colour saved for ${_plural(snapshot.length, 'speaker', 'speakers')}.`, snapshot);
        } else {
          uiToast({ message: data.error || 'Could not save the colour.', kind: 'error' });
        }
      }
      renderSpeakerManager();
    });
    colorGridEl.appendChild(btn);
  });

  const unlinkBtn = document.getElementById('speaker-unlink-btn');
  if (unlinkBtn) unlinkBtn.hidden = !_selectedSpeakerKeys.some(k => _sessionLinks[k]);

  if (selectedGroupCount === 0) {
    hintEl.textContent = 'Click a speaker row to edit it. Ctrl/Cmd-click or Shift-click for multi-select.';
  } else if (selectedGroupCount === 1) {
    hintEl.textContent = 'Assign a name, or pick a Voice Library profile to link this speaker to.';
  } else {
    hintEl.textContent = `Editing ${selectedGroupCount} speakers. Every change applies to all of them.`;
  }

  _paintSpeakerModalHeader();

  listEl.innerHTML = '';
  if (!groups.length) {
    listEl.innerHTML = '<div class="speaker-manager-empty">Speaker rows will appear here once diarized speakers show up in the transcript.</div>';
    return;
  }

  groups.forEach(group => {
    // The row is a plain container with real <button> children. It used to be a
    // <button> with an interactive span inside it, which is invalid HTML and
    // gives screen readers one unusable control instead of two.
    const row = document.createElement('div');
    const isSelected = group.speakerKeys.some(k => _selectedSpeakerKeys.includes(k));
    row.className = 'speaker-row' + (isSelected ? ' selected' : '');
    row.dataset.speakerKeys = JSON.stringify(group.speakerKeys);

    const selectBtn = document.createElement('button');
    selectBtn.type = 'button';
    selectBtn.className = 'speaker-row-select';
    selectBtn.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
    selectBtn.addEventListener('click', e => {
      _setGroupSelection(group, {
        toggle: e.ctrlKey || e.metaKey,
        range: e.shiftKey,
      });
    });

    const swatch = document.createElement('span');
    swatch.className = 'speaker-row-swatch';
    swatch.style.backgroundColor = group.color || speakerColor(group.speakerKeys[0]);

    const main = document.createElement('div');
    main.className = 'speaker-row-main';

    const nameEl = document.createElement('div');
    nameEl.className = 'speaker-row-name';
    nameEl.textContent = group.name;

    // Count active (non-noise) segments only, so this matches the filter chips
    // and analytics. The key list below still shows every fragment key.
    const count = group.speakerKeys.reduce((sum, k) => sum + (_isNoiseKey(k) ? 0 : _speakerBadgeCount(k)), 0);
    const meta = document.createElement('div');
    meta.className = 'speaker-row-meta';
    if (group.custom && !count) {
      meta.textContent = 'Saved participant';
    } else if (group.speakerKeys.length === 1) {
      const k = group.speakerKeys[0];
      meta.innerHTML = `${k}${count ? ` <span class="session-meta-sep">|</span> ${count} segment${count === 1 ? '' : 's'}` : ''}`;
    } else {
      // Multiple diarizer fragments - show key list as muted subtext
      const displayed = group.speakerKeys.slice(0, 3).join(', ');
      const extra = group.speakerKeys.length > 3 ? ` +${group.speakerKeys.length - 3}` : '';
      meta.innerHTML = `${displayed}${extra}${count ? ` <span class="session-meta-sep">|</span> ${count} segments` : ''}`;
      meta.title = group.speakerKeys.join(', ');
    }

    const countEl = document.createElement('div');
    countEl.className = 'speaker-row-count';
    countEl.textContent = count ? `${count}` : 'saved';

    main.appendChild(nameEl);
    main.appendChild(meta);
    selectBtn.appendChild(swatch);
    selectBtn.appendChild(main);
    row.appendChild(selectBtn);
    // Play this speaker's voice (only when there are real segments to hear).
    // A real sibling button: Enter and Space come free, no nesting.
    if (count > 0) {
      const playCtl = document.createElement('button');
      playCtl.type = 'button';
      playCtl.className = 'speaker-row-play';
      playCtl.title = 'Play this speaker’s voice';
      playCtl.setAttribute('aria-label', `Play ${group.name}`);
      playCtl.innerHTML = '<i class="fa-solid fa-play"></i>';
      playCtl.addEventListener('click', ev => playManageSpeakerVoice(group.speakerKeys, playCtl, ev));
      row.appendChild(playCtl);
    }
    // Show linked indicator if any key in this group is linked to a global profile
    const isLinked = group.speakerKeys.some(k => _sessionLinks[k]);
    if (isLinked) {
      const linkBadge = document.createElement('span');
      linkBadge.className = 'speaker-row-linked';
      linkBadge.innerHTML = '<i class="fa-solid fa-link"></i> Linked';
      linkBadge.title = 'Linked to a voice library profile';
      row.appendChild(linkBadge);
    }
    row.appendChild(countEl);
    listEl.appendChild(row);
  });
}

async function createSpeakerProfile() {
  const name = (_mgrNameCombo ? _mgrNameCombo.getValue() : _speakerDraftName || '').trim();
  if (!name) {
    uiToast({ message: 'Assign a name first.', kind: 'warn' });
    return;
  }

  if (!state.sessionId) {
    // No session yet - store locally and flush when recording starts
    const tempKey = `pre:${Date.now()}`;
    const color = _speakerDraftColor || _SPEAKER_PALETTE[_speakerColorIdx % _SPEAKER_PALETTE.length];
    _pendingSpeakerProfiles.push({ tempKey, name, color });
    applySpeakerProfileUpdate({ speaker_key: tempKey, name, color });
    if (_speakerProfiles[tempKey]) _speakerProfiles[tempKey].custom = true;
    _selectedSpeakerKeys = [tempKey];
    _speakerSelectionAnchor = tempKey;
    _syncSpeakerDraftFromSelection();
    renderSpeakerManager();
    return;
  }

  const resp = await fetch(`/api/sessions/${state.sessionId}/speakers`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name,
      color: _speakerDraftColor || null,
    }),
  });
  const data = await resp.json();
  if (!resp.ok) {
    uiToast({ message: data.error || 'Failed to add speaker', kind: 'error' });
    return;
  }

  applySpeakerProfileUpdate(data.speaker);
  _selectedSpeakerKeys = [data.speaker.speaker_key];
  _speakerSelectionAnchor = data.speaker.speaker_key;
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
  onSpeakerDataChanged();
  uiToast({ message: `Added participant "${name}".`, kind: 'success', id: 'speaker-manage-save' });
}

async function _flushPendingSpeakers(sessionId) {
  if (!_pendingSpeakerProfiles.length) return;
  const toFlush = [..._pendingSpeakerProfiles];
  _pendingSpeakerProfiles = [];
  for (const pending of toFlush) {
    try {
      const resp = await fetch(`/api/sessions/${sessionId}/speakers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: pending.name, color: pending.color }),
      });
      const data = await resp.json();
      if (resp.ok && data.speaker) {
        // Replace the temp profile with the real one
        delete _speakerProfiles[pending.tempKey];
        delete _speakerColors[pending.tempKey];
        if (_speakerLabels[pending.tempKey]) delete _speakerLabels[pending.tempKey];
        _selectedSpeakerKeys = _selectedSpeakerKeys.filter(k => k !== pending.tempKey);
        applySpeakerProfileUpdate(data.speaker);
      }
    } catch (e) {
      console.warn('Failed to flush pending speaker:', pending.name, e);
    }
  }
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
}

async function applySpeakerEditor() {
  if (!state.sessionId) return;
  if (!_selectedSpeakerKeys.length) {
    uiToast({ message: 'Select at least one speaker row first.', kind: 'warn' });
    return;
  }

  const name = (_mgrNameCombo ? _mgrNameCombo.getValue() : _speakerDraftName || '').trim();
  const body = { speaker_keys: _selectedSpeakerKeys };
  if (name) body.name = name;
  if (_speakerDraftColor) body.color = _speakerDraftColor;
  if (!body.name && !body.color) {
    uiToast({ message: 'Assign a name or choose a colour first.', kind: 'warn' });
    return;
  }

  const snapshot = _mgrSnapshotSelection();
  const resp = await fetch(`/api/sessions/${state.sessionId}/speakers`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) {
    uiToast({ message: data.error || 'Could not save the speaker.', kind: 'error' });
    return;
  }

  (data.speakers || []).forEach(applySpeakerProfileUpdate);
  _mgrCommittedName = name;
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
  onSpeakerDataChanged();
  _mgrToastSaved(
    name ? `Saved "${name}" for ${_plural(snapshot.length, 'speaker', 'speakers')}.`
         : `Saved ${_plural(snapshot.length, 'speaker', 'speakers')}.`,
    snapshot,
  );
}

function appendTranscript(text, source, startTime, endTime, segId, labelOverride, originalSource) {
  const el = document.getElementById('transcript');
  el.querySelector('.empty-hint')?.remove();

  const seg = document.createElement('div');
  seg.className = 'transcript-segment';
  seg.dataset.transcriptSource = source;  // used by filter
  if (originalSource) seg.dataset.originalSource = originalSource;  // original diarizer key before reassignment

  // Store segment DB id for per-segment overrides
  if (segId != null) seg.dataset.segId = segId;

  // Store timestamps for playback sync
  if (startTime != null && startTime > 0) {
    seg.dataset.start = startTime;
    seg.dataset.end   = endTime;
    seg.addEventListener('click', e => {
      // Don't seek if the click is on a speaker badge (rename picker)
      if (e.target.closest('.src-badge, .speaker-picker')) return;
      seekToTime(startTime);
    });
  }

  if (source in SOURCE_META) {
    const { label, cls } = SOURCE_META[source];
    seg.innerHTML = `<span class="src-badge ${cls}">${label}</span>${escapeHtml(text)}`;
  } else if (source === _NOISE_LABEL || labelOverride === _NOISE_LABEL) {
    // Noise/filler segment - muted styling, click to reassign
    if (labelOverride === _NOISE_LABEL) _manualNoiseKeys.add(source);
    seg.classList.add('noise-segment');
    seg.style.setProperty('--seg-color', _NOISE_COLOR);
    const badge = document.createElement('span');
    badge.className = 'src-badge src-speaker src-noise';
    badge.dataset.speakerKey = source;
    if (segId != null) badge.dataset.segId = segId;
    if (_showOriginalKeys && source !== _NOISE_LABEL) {
      badge.textContent = source;
      const alias = document.createElement('span');
      alias.className = 'badge-alias';
      alias.textContent = 'Noise';
      badge.appendChild(alias);
    } else {
      badge.textContent = 'Noise';
    }
    badge.style.backgroundColor = _NOISE_COLOR + '20';
    badge.style.color = _NOISE_COLOR;
    badge.style.borderColor = _NOISE_COLOR + '40';
    badge.title = 'Click to reassign';
    badge.addEventListener('click', e => {
      if (e.ctrlKey || e.metaKey || e.shiftKey) {
        e.preventDefault(); e.stopPropagation();
        _toggleTranscriptSegSelection(seg, { range: e.shiftKey });
        return;
      }
      _editNoiseBadge(badge, seg);
    });
    seg.appendChild(badge);
    seg.appendChild(document.createTextNode(text));
  } else {
    // Speaker label - assign accent color, make badge click-to-rename
    _ensureSpeakerProfile(source);
    const displayName = labelOverride || _speakerDisplayName(source) || source;
    const color = speakerColor(source);
    //seg.style.setProperty('border-color', color, 'important');
    //seg.style.borderLeftColor = color;
    seg.style.setProperty('--seg-color', color);
    const badge = document.createElement('span');
    badge.className = 'src-badge src-speaker';
    if (_sessionLinks[source]) badge.classList.add('speaker-linked');
    badge.dataset.speakerKey = source;
    if (segId != null) badge.dataset.segId = segId;
    if (labelOverride) badge.dataset.override = '1';
    badge.title = _sessionLinks[source]
      ? `Saved voice profile: ${_sessionLinks[source].name || source}`
      : 'Click to rename';
    // Show original key (with alias) when toggle is active, unless per-segment override
    const isMe = _isMeSpeaker(source);
    if (_showOriginalKeys && !labelOverride) {
      _setBadgeLabel(badge, source);
    } else {
      badge.textContent = displayName;
    }
    badge.style.backgroundColor = color + '26'; // ~15% opacity tint
    badge.style.color = color;
    badge.style.borderColor = color + '60';

    if (isMe) {
      // Local-only "(You)" indicator. Never baked into the stored/exported name
      // - keyed on the local Me id so imported foreign mic segments aren't badged.
      badge.classList.add('src-me');
      const you = document.createElement('span');
      you.className = 'badge-you';
      you.textContent = ' (You)';
      you.style.opacity = '.7';
      you.style.fontWeight = '500';
      badge.appendChild(you);
      badge.title = 'Your microphone audio. Click to change your speaker name.';
    } else {
      // Inline identify icon for unlinked speakers (never for the Me speaker -
      // mic audio is always you and is never fingerprinted/identified).
      const idIcon = document.createElement('i');
      idIcon.className = 'fa-solid fa-fingerprint speaker-identify-icon';
      const suggestion = _fpGetSuggestion(source);
      if (suggestion) {
        idIcon.classList.add('has-suggestion');
        idIcon.title = `Sounds like ${suggestion.matches[0].name} (${Math.round(suggestion.matches[0].similarity * 100)}%)`;
      } else {
        idIcon.title = 'Identify speaker';
      }
      badge.appendChild(idIcon);
    }

    badge.addEventListener('click', e => {
      if (e.ctrlKey || e.metaKey || e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        _toggleTranscriptSegSelection(seg, { range: e.shiftKey });
        return;
      }
      // The Me speaker opens the identity dialog (rename is retroactive).
      if (isMe) { e.stopPropagation(); _showMeSpeakerPopup(); return; }
      // If clicking the identify icon and there's a suggestion, open the panel
      if (e.target.closest('.speaker-identify-icon') && _fpGetSuggestion(source)) {
        const panel = document.getElementById('fp-notif-panel');
        if (panel?.classList.contains('collapsed')) toggleFpNotifPanel();
        return;
      }
      editSpeakerLabel(badge, source);
    });
    seg.appendChild(badge);
    seg.appendChild(document.createTextNode(text));
  }

  el.appendChild(seg);

  // Register in the in-memory index used by playback and filter hot paths.
  _segmentRegistry.push(seg);
  if (startTime != null && startTime > 0) {
    _segmentTimes.push({ start: startTime, end: endTime ?? startTime, el: seg });
  }
  _visibleRangesCache = null;  // new segment may change visible ranges

  // During bulk load, skip expensive per-segment work - it runs once after the load.
  if (_bulkLoading) return;

  // Extend time range slider if navigator is open (before filtering, so pinned max stays Infinity)
  _tnExtendTimeRange();
  _applyFilterToSeg(seg);
  // Highlight search matches in new segment if search is active
  if (_transcriptFilter.search.trim() && seg.style.display !== 'none') {
    _tnHighlightInSeg(seg);
  }
  // Only check this new segment's badge - no need to re-scan all segments.
  if (_selectedSpeakerKeys.length) {
    const badge = seg.querySelector('.src-badge.src-speaker');
    if (badge) badge.classList.toggle('speaker-selected', _selectedSpeakerKeys.includes(badge.dataset.speakerKey));
  }
  if (!document.getElementById('speaker-manager-overlay')?.classList.contains('hidden')) {
    renderSpeakerManager();
  }
  if (_autoScroll && !_pickerOpen) {
    _programmaticScrollCount++;
    el.scrollTop = el.scrollHeight;
    setTimeout(() => { _programmaticScrollCount = Math.max(0, _programmaticScrollCount - 1); }, 100);
  }
  _updateCollapseFabVisibility();
  _updateMinimapFabVisibility();
  _refreshMinimap();
}

// Is this a default auto-generated speaker name? (e.g. "Speaker 1")
function _isDefaultName(name) {
  return /^Speaker \d+$/i.test(name);
}

/* ── Voice-similarity ranking for the speaker pickers ───────────────────────
 * The pickers rank suggestions by how close each candidate's voice is to the
 * clicked speaker's voice. Centroids come from the same payload the cleanup
 * tab uses (/api/sessions/<id>/speaker_clusters); we cache a flat per-session
 * index of { speaker_key → centroid, library:[…] } so opening a picker doesn't
 * re-hit the server once it's warm. When no centroid is available (model not
 * ready, no embeddings yet) the pickers silently fall back to their default
 * ordering.
 */
let _simIndex = null;          // { sessionId, keyCentroid: Map<key,Float32Array>, library:[…] }
let _simIndexPromise = null;   // { sid, p }: in-flight load, deduped per session

function _decodeCentroidB64(b64) {
  if (!b64) return null;
  const bin = atob(b64);
  const view = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) view[i] = bin.charCodeAt(i);
  return new Float32Array(view.buffer);
}

// Derive the flat index from an already-loaded cleanup state (centroids there
// are pre-decoded) so we avoid a redundant fetch when the user has the cleanup
// tab open.
function _simIndexFromCleanupState(cs) {
  const keyCentroid = new Map();
  cs.clusters.forEach(cl => cl.members.forEach(m => keyCentroid.set(m.speaker_key, m.centroid || null)));
  cs.noiseMembers.forEach((m, k) => keyCentroid.set(k, m.centroid || null));
  const library = (cs.library || []).map(g => ({
    global_id: g.global_id, name: g.name, color: g.color, emb_count: g.emb_count, centroid: g.centroid || null,
  }));
  return { sessionId: cs.sessionId, keyCentroid, library, thresholds: cs.thresholds };
}

function _buildSimIndexFromClusters(payload) {
  const keyCentroid = new Map();
  const add = cl => (cl?.members || []).forEach(m => keyCentroid.set(m.speaker_key, _decodeCentroidB64(m.centroid)));
  (payload.labeled_clusters || []).forEach(add);
  (payload.unlabeled_clusters || []).forEach(add);
  add(payload.noise_cluster);
  const library = (payload.library || []).map(g => ({
    global_id: g.global_id, name: g.name, color: g.color, emb_count: g.emb_count, centroid: _decodeCentroidB64(g.centroid),
  }));
  return { sessionId: payload.session_id, keyCentroid, library, thresholds: payload.thresholds };
}

async function _ensureSimIndex() {
  const sid = state.sessionId;
  if (!sid) return null;
  if (_simIndex && _simIndex.sessionId === sid) return _simIndex;
  if (_cleanupState && _cleanupState.sessionId === sid) {
    _simIndex = _simIndexFromCleanupState(_cleanupState);
    return _simIndex;
  }
  if (_simIndexPromise && _simIndexPromise.sid === sid) return _simIndexPromise.p;
  const p = fetch(`/api/sessions/${sid}/speaker_clusters`)
    .then(r => (r.ok ? r.json() : null))
    .then(data => {
      if (!data || data.error) return null;
      _simIndex = _buildSimIndexFromClusters(data);
      return _simIndex;
    })
    .catch(() => null)
    .finally(() => { if (_simIndexPromise && _simIndexPromise.sid === sid) _simIndexPromise = null; });
  _simIndexPromise = { sid, p };
  return p;
}

// Cosine similarity of two L2-normalized centroids (a plain dot product), or
// null when either side is missing.
function _cosineSim(a, b) {
  if (!a || !b || a.length !== b.length) return null;
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

// Build similarity scorers for the source speaker_key against the given index.
// meetingScore(displayName) ranks a meeting speaker (taking the best match over
// the speaker_keys that share that display name); libScore(gid, name) ranks a
// Voice Library profile. Both return a cosine in [-1,1] or null.
function _speakerSimScorers(srcKey, idx) {
  const src = (idx && srcKey) ? (idx.keyCentroid.get(srcKey) || null) : null;
  const nameKeys = new Map();   // lower(name) → [speaker_key,…]
  _groupProfilesByName(_getSortedSpeakerProfiles()).forEach(g => {
    nameKeys.set((g.name || '').toLowerCase(), g.speakerKeys);
  });
  const libByGid = new Map();
  if (idx) idx.library.forEach(g => libByGid.set(g.global_id, g));
  const best = centroids => {
    let b = null;
    centroids.forEach(c => { const s = _cosineSim(src, c); if (s != null && (b == null || s > b)) b = s; });
    return b;
  };
  return {
    available: !!src,
    meetingScore(name) {
      if (!src) return null;
      const keys = nameKeys.get((name || '').toLowerCase()) || [];
      return best(keys.map(k => idx.keyCentroid.get(k)));
    },
    libScore(gid, name) {
      if (!src) return null;
      const g = gid && libByGid.get(gid);
      if (g) return _cosineSim(src, g.centroid);
      // The full VL list can include profiles missing from the centroid index;
      // fall back to matching by name so they still rank when possible.
      if (!name) return null;
      return best(idx.library.filter(x => (x.name || '').toLowerCase() === name.toLowerCase()).map(x => x.centroid));
    },
  };
}

// Add/update the small similarity badge on a picker option. score is a cosine
// in [-1,1], or null to remove the badge. Color tiers mirror the cleanup
// picker (muted < suggest ≤ accent < auto ≤ green).
function _setOptSim(el, score, thresholds) {
  let badge = el.querySelector('.speaker-picker-sim');
  if (score == null) { if (badge) badge.remove(); return; }
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'speaker-picker-sim';
    el.appendChild(badge);
  }
  badge.textContent = Math.round(score * 100) + '%';
  const auto = (thresholds && thresholds.auto) || 0.82;
  const suggest = (thresholds && thresholds.suggest) || 0.65;
  badge.classList.toggle('high', score >= auto);
  badge.classList.toggle('mid', score >= suggest && score < auto);
}

// Sort comparator: higher similarity first, unscored entries last, then a
// natural-name tiebreak so ordering stays stable.
function _simComparator(scoreOf, nameOf) {
  return (a, b) => {
    const sa = scoreOf(a), sb = scoreOf(b);
    if (sa == null && sb == null) return nameOf(a).localeCompare(nameOf(b), undefined, { numeric: true });
    if (sa == null) return 1;
    if (sb == null) return -1;
    if (sb !== sa) return sb - sa;
    return nameOf(a).localeCompare(nameOf(b), undefined, { numeric: true });
  };
}

// Position a floating picker relative to an anchor rect. 'below' is the legacy
// drop-down placement; 'right' pops the picker out beside the anchor so a
// vertical column of pills underneath stays visible and clickable.
function _positionPicker(picker, anchorRect, placement = 'below') {
  const pw = picker.offsetWidth, ph = picker.offsetHeight, margin = 8;
  let top, left;
  if (placement === 'right') {
    left = anchorRect.right + 6;
    if (left + pw > window.innerWidth - margin) left = anchorRect.left - pw - 6;  // flip to the left edge if no room
    top = anchorRect.top;
    if (top + ph > window.innerHeight - margin) top = window.innerHeight - ph - margin;
  } else {
    const spaceBelow = window.innerHeight - anchorRect.bottom - margin;
    const spaceAbove = anchorRect.top - margin;
    top = (spaceBelow >= ph || spaceBelow >= spaceAbove) ? anchorRect.bottom + 2 : anchorRect.top - ph - 2;
    left = Math.min(anchorRect.left, window.innerWidth - pw - margin);
  }
  picker.style.top = Math.max(margin, top) + 'px';
  picker.style.left = Math.max(margin, left) + 'px';
}

/* Populate a picker's options container with two voice-similarity-ranked
 * groups: current meeting speakers (top) and saved Voice Library profiles
 * (below). Shared by the rename picker (editSpeakerLabel) and the bulk-reassign
 * picker so both rank and look identical. onPick(name) is called when an option
 * is chosen. Returns { filterOpts } for the picker's text input.
 */
function _buildPickerSpeakerOptions(optionsWrap, { currentName = '', excludeKey = '', srcKey = '', baseColor = '#58a6ff', onPick }) {
  const meetingEntries = [];   // { name, el }
  const vlEntries = [];        // { name, gid, el }

  // ── Meeting speakers ──
  const existingNames = _speakerOptionNames(currentName, excludeKey);
  const meetingNameSet = new Set(existingNames.map(n => n.toLowerCase()));
  const meetingHeader = document.createElement('div');
  meetingHeader.className = 'speaker-picker-section';
  meetingHeader.textContent = 'Meeting speakers';
  meetingHeader.style.display = existingNames.length ? '' : 'none';
  optionsWrap.appendChild(meetingHeader);
  existingNames.forEach(name => {
    const optKey = _speakerNameKey(name, excludeKey);
    const optColor = (optKey && (_speakerColors[optKey] || speakerColor(optKey))) || baseColor;
    const opt = document.createElement('button');
    opt.className = 'speaker-picker-opt';
    opt.dataset.optName = name.toLowerCase();
    opt.textContent = name;
    opt.style.borderColor = optColor + '60';
    opt.style.color = optColor;
    opt.addEventListener('mousedown', e => { e.preventDefault(); onPick(name); });
    optionsWrap.appendChild(opt);
    meetingEntries.push({ name, el: opt });
  });

  // ── Voice Library (populated asynchronously) ──
  const vlHeader = document.createElement('div');
  vlHeader.className = 'speaker-picker-section speaker-picker-vl-section';
  vlHeader.textContent = 'Voice Library';
  vlHeader.style.display = 'none';
  optionsWrap.appendChild(vlHeader);

  function filterOpts(query) {
    let meetingVisible = 0, vlVisible = 0;
    optionsWrap.querySelectorAll('.speaker-picker-opt').forEach(opt => {
      const name = opt.dataset.optName || '';
      const show = !query || name.includes(query);
      opt.style.display = show ? '' : 'none';
      if (show) { opt.classList.contains('speaker-picker-vl-opt') ? vlVisible++ : meetingVisible++; }
    });
    meetingHeader.style.display = meetingVisible > 0 ? '' : 'none';
    vlHeader.style.display = vlVisible > 0 ? '' : 'none';
  }

  // Re-order both groups by voice similarity once the centroid index resolves,
  // tagging each option with a small similarity badge. Idempotent; safe to
  // call again when the VL list finishes loading.
  function applySimSort(idx) {
    const scorers = _speakerSimScorers(srcKey, idx);
    if (!scorers.available) return;
    const th = idx.thresholds;
    meetingEntries.forEach(e => { e.score = scorers.meetingScore(e.name); _setOptSim(e.el, e.score, th); });
    vlEntries.forEach(e => { e.score = scorers.libScore(e.gid, e.name); _setOptSim(e.el, e.score, th); });
    meetingEntries.sort(_simComparator(e => e.score, e => e.name));
    vlEntries.sort(_simComparator(e => e.score, e => e.name));
    meetingEntries.forEach(e => optionsWrap.insertBefore(e.el, vlHeader));  // keep between the two headers
    vlEntries.forEach(e => optionsWrap.appendChild(e.el));                  // after the VL header
  }

  fetch('/api/fingerprint/speakers').then(r => r.json()).then(speakers => {
    if (!speakers || !speakers.length) return;
    speakers.forEach(sp => {
      const name = (sp.name || '').trim();
      if (!name || meetingNameSet.has(name.toLowerCase())) return;
      if (currentName && name.toLowerCase() === currentName.toLowerCase()) return;
      // Never offer the "You" (Me) profile as a label for a desktop speaker;
      // mic audio is the only thing that is ever you.
      if (window._meSpeakerGlobalId && sp.id === window._meSpeakerGlobalId) return;
      const vlColor = sp.color || 'var(--fg-muted)';
      const opt = document.createElement('button');
      opt.className = 'speaker-picker-opt speaker-picker-vl-opt';
      opt.dataset.optName = name.toLowerCase();
      opt.textContent = name;
      opt.style.borderColor = vlColor + '60';
      opt.style.color = vlColor;
      opt.addEventListener('mousedown', e => { e.preventDefault(); onPick(name); });
      optionsWrap.appendChild(opt);
      vlEntries.push({ name, gid: sp.id, el: opt });
    });
    if (vlEntries.length) {
      vlHeader.style.display = '';
      if (_simIndex && _simIndex.sessionId === state.sessionId) applySimSort(_simIndex);
    }
  }).catch(() => {});

  // Kick off (or reuse) the centroid index; re-sort when it's ready.
  _ensureSimIndex().then(idx => { if (idx) applySimSort(idx); });

  return { filterOpts };
}

function editSpeakerLabel(badge, speakerKey) {
  // Remove any existing picker first (including an in-progress bulk selection)
  _closeBulkSpeakerPicker();
  document.querySelector('.speaker-picker')?.remove();

  const currentName = badge.textContent;
  const color = _speakerColors[speakerKey] || speakerColor(speakerKey) || '#58a6ff';
  const segId = badge.dataset.segId;  // may be undefined for live segments without DB id

  // Determine edit mode:
  // - "oneoff" only if the badge is already a per-segment override
  // - "global" for everything else - first-touch edits always rename all segments
  const editMode = badge.dataset.override ? 'oneoff' : 'global';
  const isDefault = _isDefaultName(currentName) || currentName === speakerKey;

  // Build the dropdown picker
  const picker = document.createElement('div');
  picker.className = 'speaker-picker';
  picker.style.borderColor = color + '80';

  // Free-text input at the top
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'speaker-picker-input';
  input.placeholder = editMode === 'global' ? 'Name this speaker…' : 'Override this segment…';
  input.value = currentName;
  input.style.borderColor = color + '60';
  input.style.color = color;
  picker.appendChild(input);

  // Scrollable options container
  const optionsWrap = document.createElement('div');
  optionsWrap.className = 'speaker-picker-options';
  picker.appendChild(optionsWrap);

  // Meeting speakers + Voice Library options, both ranked by voice similarity
  // to the clicked speaker. commit is referenced lazily (defined below).
  const { filterOpts } = _buildPickerSpeakerOptions(optionsWrap, {
    currentName, excludeKey: speakerKey, srcKey: speakerKey, baseColor: color,
    onPick: name => commit(name),
  });

  // Highlight all matching badges when in global mode
  const _highlighted = [];
  if (editMode === 'global') {
    document.querySelectorAll(`[data-speaker-key="${speakerKey}"]`).forEach(el => {
      if (el !== badge && el.tagName === 'SPAN' && !el.dataset.override) {
        el.classList.add('label-highlight');
        const seg = el.closest('.transcript-segment');
        if (seg) seg.classList.add('label-highlight-seg');
        _highlighted.push(el);
      }
    });
  }

  function _clearHighlights() {
    _highlighted.forEach(el => {
      el.classList.remove('label-highlight');
      const seg = el.closest('.transcript-segment');
      if (seg) seg.classList.remove('label-highlight-seg');
    });
  }

  // "Mark as Noise" button - suppresses segment and hides it with noise pill
  const noiseBtn = document.createElement('button');
  noiseBtn.className = 'speaker-picker-noise-btn';
  noiseBtn.innerHTML = '<i class="fa-solid fa-volume-xmark"></i> Mark as Noise';
  noiseBtn.addEventListener('mousedown', e => {
    e.preventDefault();
    if (committed) return;
    committed = true;
    _pickerOpen = false;
    _clearHighlights();
    picker.remove();
    if (editMode === 'global') {
      _markSpeakerAsNoise(speakerKey);
    } else {
      const targetSeg = badge.closest('.transcript-segment');
      if (targetSeg) _markSegAsNoise(targetSeg);
    }
  });
  picker.appendChild(noiseBtn);

  // Mode hint at the bottom
  const hint = document.createElement('div');
  hint.className = 'speaker-picker-hint';
  if (editMode === 'global') {
    const total = _highlighted.length + 1;  // +1 for the clicked badge
    hint.textContent = isDefault
      ? `Renames all ${speakerKey} segments (${total})`
      : `Renames all ${total} segments for "${currentName}"`;
  } else {
    hint.textContent = `This segment only (overrides "${currentName}")`;
  }
  picker.appendChild(hint);

  let committed = false;
  const commit = (name) => {
    if (committed) return;
    committed = true;
    _pickerOpen = false;
    _clearHighlights();
    const newName = (name || '').trim() || speakerKey;
    picker.remove();
    if (newName === currentName) return;

    if (editMode === 'global') {
      // Global rename: update all badges with this speaker_key
      applySpeakerProfileUpdate({ speaker_key: speakerKey, name: newName });
      persistSpeakerLabel(speakerKey, newName).catch(() => {});
    } else {
      // One-off: update only this badge
      badge.textContent = newName;
      badge.dataset.override = '1';
      if (segId) persistSegmentOverride(segId, newName);
    }
  };

  const cancel = () => {
    if (committed) return;
    committed = true;
    _pickerOpen = false;
    _clearHighlights();
    picker.remove();
  };

  // Append first so we can measure the picker's rendered size, then drop it
  // below/above the badge depending on available space.
  _pickerOpen = true;
  document.body.appendChild(picker);
  _positionPicker(picker, badge.getBoundingClientRect(), 'below');
  input.focus();
  input.select();

  // Live filter + merge hint on input
  input.addEventListener('input', () => {
    const typed = input.value.trim().toLowerCase();
    // Filter option buttons
    filterOpts(typed);

    // In global mode, show a live merge hint when the typed name matches an existing speaker
    if (editMode === 'global') {
      if (!typed || typed === currentName.toLowerCase()) {
        hint.textContent = isDefault
          ? `Renames all ${speakerKey} segments (${_highlighted.length + 1})`
          : `Renames all ${_highlighted.length + 1} segments for "${currentName}"`;
        hint.style.color = '';
        return;
      }
      const groups = _groupProfilesByName(_getSortedSpeakerProfiles());
      const match = groups.find(g =>
        g.speakerKeys[0] !== speakerKey &&
        !g.speakerKeys.includes(speakerKey) &&
        g.name.toLowerCase() === typed
      );
      if (match) {
        const mergeCount = match.speakerKeys.reduce((s, k) => s + _speakerBadgeCount(k), 0);
        hint.textContent = `Will merge with "${match.name}" (${mergeCount} seg${mergeCount === 1 ? '' : 's'})`;
        hint.style.color = 'var(--accent)';
      } else {
        hint.textContent = isDefault
          ? `Renames all ${speakerKey} segments (${_highlighted.length + 1})`
          : `Renames all ${_highlighted.length + 1} segments for "${currentName}"`;
        hint.style.color = '';
      }
    }
  });

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter')  { e.preventDefault(); commit(input.value); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });

  // Close on outside click
  const onOutside = e => {
    if (!picker.contains(e.target) && e.target !== badge) {
      document.removeEventListener('mousedown', onOutside, true);
      commit(input.value);
    }
  };
  requestAnimationFrame(() => {
    document.addEventListener('mousedown', onOutside, true);
  });
}

async function persistSegmentOverride(segId, label, sourceOverride = null) {
  const body = { label };
  if (sourceOverride) body.source_override = sourceOverride;
  await fetch(`/api/segments/${segId}/label`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  });
}

// Apply noise DOM styling to a single badge+seg, wiring up the reassign click handler.
function _applyNoiseStyle(seg, badge, segId) {
  seg.classList.add('noise-segment');
  seg.style.setProperty('--seg-color', _NOISE_COLOR);
  const speakerKey = badge.dataset.speakerKey || seg.dataset.transcriptSource || '';
  badge.className = 'src-badge src-speaker src-noise';
  if (_showOriginalKeys && speakerKey) {
    badge.textContent = speakerKey;
    const alias = document.createElement('span');
    alias.className = 'badge-alias';
    alias.textContent = 'Noise';
    badge.appendChild(alias);
  } else {
    badge.textContent = 'Noise';
  }
  badge.style.backgroundColor = _NOISE_COLOR + '20';
  badge.style.color = _NOISE_COLOR;
  badge.style.borderColor = _NOISE_COLOR + '40';
  badge.title = 'Click to reassign';
  badge.dataset.override = '1';
  if (segId) badge.dataset.segId = segId;
  // Replace element to clear old listeners, then re-add the noise click handler
  const fresh = badge.cloneNode(true);
  fresh.addEventListener('click', e => {
    if (e.ctrlKey || e.metaKey || e.shiftKey) {
      e.preventDefault(); e.stopPropagation();
      _toggleTranscriptSegSelection(seg, { range: e.shiftKey });
      return;
    }
    _editNoiseBadge(fresh, seg);
  });
  badge.replaceWith(fresh);
}

// Mark all DOM segments for a speaker_key as noise and persist overrides.
async function _markSpeakerAsNoise(speakerKey) {
  _manualNoiseKeys.add(speakerKey);
  const segs = [...document.querySelectorAll(`#transcript .transcript-segment[data-transcript-source="${speakerKey}"]`)];
  for (const seg of segs) {
    const badge = seg.querySelector('.src-badge');
    const segId = seg.dataset.segId || badge?.dataset.segId;
    if (badge) _applyNoiseStyle(seg, badge, segId);
    if (segId) persistSegmentOverride(segId, _NOISE_LABEL).catch(() => {});
  }
  applyTranscriptFilter();
  _tnRefreshSpeakerPills();
  _tnRefreshReassignDropdowns();
}

// Mark a single segment as noise and persist the override.
async function _markSegAsNoise(seg) {
  const source = seg.dataset.transcriptSource;
  if (source) _manualNoiseKeys.add(source);
  const badge = seg.querySelector('.src-badge');
  const segId = seg.dataset.segId;
  if (badge) _applyNoiseStyle(seg, badge, segId);
  if (segId) persistSegmentOverride(segId, _NOISE_LABEL).catch(() => {});
  _applyFilterToSeg(seg);
  _tnRefreshSpeakerPills();
  _tnRefreshReassignDropdowns();
}

// Open a picker on a noise badge so the user can reassign the segment to a real speaker.
function _editNoiseBadge(badge, seg) {
  document.querySelector('.speaker-picker')?.remove();
  const segId = seg.dataset.segId;
  const oldSource = seg.dataset.transcriptSource;

  const picker = document.createElement('div');
  picker.className = 'speaker-picker';
  picker.style.borderColor = _NOISE_COLOR + '60';

  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'speaker-picker-input';
  input.placeholder = 'Assign to speaker…';
  input.style.borderColor = _NOISE_COLOR + '40';
  input.style.color = _NOISE_COLOR;
  picker.appendChild(input);

  // Options: all non-noise speakers, deduplicated by display name
  const profiles = _getSortedSpeakerProfiles().filter(p => p.speaker_key !== _NOISE_LABEL && !_manualNoiseKeys.has(p.speaker_key));
  const seenNames = new Set();
  profiles.forEach(p => {
    const name = _speakerDisplayName(p.speaker_key) || p.speaker_key;
    const nameLower = name.toLowerCase();
    if (seenNames.has(nameLower)) return;
    seenNames.add(nameLower);
    const color = _speakerColors[p.speaker_key] || speakerColor(p.speaker_key);
    const opt = document.createElement('button');
    opt.className = 'speaker-picker-opt';
    opt.textContent = name;
    opt.style.borderColor = color + '60';
    opt.style.color = color;
    opt.addEventListener('mousedown', e => { e.preventDefault(); commit(name, p.speaker_key); });
    picker.appendChild(opt);
  });

  const hint = document.createElement('div');
  hint.className = 'speaker-picker-hint';
  hint.textContent = 'Un-noise: reassign this segment';
  picker.appendChild(hint);

  let committed = false;
  const commit = (name, knownKey) => {
    if (committed) return;
    committed = true;
    _pickerOpen = false;
    picker.remove();
    if (!name?.trim()) return;
    _unNoiseSegment(seg, badge, name.trim(), segId, oldSource, knownKey);
  };
  const cancel = () => {
    if (committed) return;
    committed = true;
    _pickerOpen = false;
    picker.remove();
  };

  _pickerOpen = true;
  document.body.appendChild(picker);
  const rect = badge.getBoundingClientRect();
  const pickerH = picker.offsetHeight;
  const pickerW = picker.offsetWidth;
  const spaceBelow = window.innerHeight - rect.bottom - 8;
  const spaceAbove = rect.top - 8;
  const top = (spaceBelow >= pickerH || spaceBelow >= spaceAbove) ? rect.bottom + 2 : rect.top - pickerH - 2;
  const left = Math.min(rect.left, window.innerWidth - pickerW - 8);
  picker.style.top = top + 'px';
  picker.style.left = left + 'px';
  input.focus();
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') commit(input.value);
    if (e.key === 'Escape') cancel();
  });
  document.addEventListener('mousedown', function onOut(e) {
    if (!picker.contains(e.target)) { cancel(); document.removeEventListener('mousedown', onOut); }
  });
}

// Restore a noise segment back to a real speaker.
function _unNoiseSegment(seg, badge, newName, segId, oldSource, knownKey) {
  // Determine remaining noise count for oldSource BEFORE modifying badge
  if (oldSource && _manualNoiseKeys.has(oldSource)) {
    const remaining = document.querySelectorAll(
      `#transcript .transcript-segment[data-transcript-source="${oldSource}"] .src-noise`
    ).length;
    if (remaining <= 1) _manualNoiseKeys.delete(oldSource);
  }

  // Resolve speaker key
  const newKey = knownKey
    || _getSortedSpeakerProfiles().find(p =>
        (_speakerDisplayName(p.speaker_key) || p.speaker_key).toLowerCase() === newName.toLowerCase()
      )?.speaker_key
    || oldSource
    || newName;

  seg.dataset.transcriptSource = newKey;
  seg.classList.remove('noise-segment');
  _ensureSpeakerProfile(newKey);
  const color = speakerColor(newKey);
  seg.style.setProperty('--seg-color', color);

  badge.className = 'src-badge src-speaker';
  badge.textContent = newName;
  badge.dataset.speakerKey = newKey;
  badge.dataset.override = '1';
  if (segId) badge.dataset.segId = segId;
  badge.title = 'Click to rename';
  badge.style.backgroundColor = color + '26';
  badge.style.color = color;
  badge.style.borderColor = color + '60';
  badge.addEventListener('click', e => {
    if (e.ctrlKey || e.metaKey || e.shiftKey) {
      e.preventDefault(); e.stopPropagation();
      _toggleTranscriptSegSelection(seg, { range: e.shiftKey });
      return;
    }
    editSpeakerLabel(badge, newKey);
  });

  if (segId) persistSegmentOverride(segId, newName, newKey !== oldSource ? newKey : null).catch(() => {});
  _applyFilterToSeg(seg);
  _tnRefreshSpeakerPills();
  _tnRefreshReassignDropdowns();
}

function applySpeakerProfileUpdate(update) {
  const speakerKey = update.speaker_key || update.speakerKey;
  if (!speakerKey) return;

  const nextName = update.name || _speakerDisplayName(speakerKey) || speakerKey;

  // Auto-clear speaker suggestion when the speaker gets a real name
  // (manual labeling, SSE label event, merge, etc.)
  if (!_isDefaultName(nextName) && nextName !== speakerKey) {
    const pending = _fpGetSuggestion(speakerKey);
    if (pending) {
      _fpRemoveFromQueue(speakerKey);
      // Dismiss on server so it doesn't reappear on reload
      fetch('/api/fingerprint/dismiss', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id:  pending.session_id,
          speaker_key: speakerKey,
          global_id:   pending.matches[0]?.global_id || '',
        }),
      }).catch(() => {});
      // If this was the active toast, hide it
      if (_fpToastActive?.speaker_key === speakerKey) _fpHideToast();
    }
  }

  const existingKey = _speakerNameKey(nextName, speakerKey);
  if (existingKey && _speakerColors[existingKey]) {
    _speakerColors[speakerKey] = _speakerColors[existingKey];
  }

  _upsertSpeakerProfile({
    speaker_key: speakerKey,
    name: nextName,
    color: update.color || _speakerColors[speakerKey] || _speakerProfiles[speakerKey]?.color || null,
  });
  if (!_speakerColors[speakerKey]) speakerColor(speakerKey);

  document.querySelectorAll(`[data-speaker-key="${speakerKey}"]`).forEach(el => {
    if (el.tagName === 'SPAN' && !el.dataset.override) {
      _setBadgeLabel(el, speakerKey);
    }
  });
  _applySpeakerColor(speakerKey, _speakerColors[speakerKey]);
  _highlightSelectedSpeakerBadges();
  if (!document.getElementById('speaker-manager-overlay')?.classList.contains('hidden')) {
    renderSpeakerManager();
  }
  _tnRefreshSpeakerPills();
  _refreshMinimap(true);
}

function _applySpeakerColor(speakerKey, color) {
  if (!color) return;
  _speakerColors[speakerKey] = color;
  _upsertSpeakerProfile({ speaker_key: speakerKey, color });
  document.querySelectorAll(`[data-speaker-key="${speakerKey}"]`).forEach(badge => {
    if (badge.tagName !== 'SPAN') return;
    badge.style.backgroundColor = color + '26';
    badge.style.color            = color;
    badge.style.borderColor      = color + '60';
    const seg = badge.closest('.transcript-segment');
    if (seg) {
      seg.style.setProperty('--seg-color', color);
      //seg.style.borderLeftColor = color;
    }
  });
}

async function persistSpeakerLabel(speakerKey, name, color = null) {
  if (!state.sessionId) return null;
  const body = { speaker_key: speakerKey, name };
  if (color) body.color = color;
  const resp = await fetch(`/api/sessions/${state.sessionId}/speakers`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || 'Failed to update speaker');
  return data;
}

function copyTranscript() {
  const segs = _segmentRegistry;
  const lines = [];
  segs.forEach(seg => {
    if (seg.style.display === 'none') return; // respect active filter
    const badge = seg.querySelector('.src-badge');
    const label = badge ? badge.textContent.trim() : '';
    const start = seg.dataset.start != null ? parseFloat(seg.dataset.start) : null;
    const timeStr = (start !== null && start >= 0) ? ` [${fmtDuration(start)}]` : '';
    // Plain text only - exclude the badge node
    const text = [...seg.childNodes]
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent)
      .join('')
      .trim();
    if (!text) return;
    lines.push(`${label}${timeStr}`);
    lines.push(text);
    lines.push('');
  });
  while (lines.length && !lines[lines.length - 1]) lines.pop();
  const result = lines.join('\n');
  if (result) navigator.clipboard.writeText(result).then(() => {
    flashStatus('Copied!');
    const btn = document.getElementById('btn-copy-transcript');
    if (btn) {
      const icon = btn.querySelector('i');
      if (icon) {
        icon.className = 'fa-solid fa-check';
        icon.style.color = '#00b464';
        clearTimeout(btn._copyTimer);
        btn._copyTimer = setTimeout(() => {
          icon.className = 'fa-solid fa-clipboard';
          icon.style.color = '';
        }, 2000);
      }
    }
  });
}

/* ── Transcript Navigator ───────────────────────────────────────────────── */

function _transcriptFilterActive() {
  return _transcriptFilter.search.length > 0
    || _transcriptFilter.speakers.size > 0
    || _transcriptFilter.timeMin > 0
    || _transcriptFilter.timeMax < Infinity;
}

function _applyFilterToSeg(seg) {
  const source  = seg.dataset.transcriptSource || '';
  // Always hide noise unless toggled visible (or in original-key mode where noise shows as regular pills)
  if ((source === _NOISE_LABEL || _manualNoiseKeys.has(source)) && !_showNoise && !_showOriginalKeys) { seg.style.display = 'none'; return; }
  if (!_transcriptFilterActive()) { seg.style.display = ''; return; }
  const speakers = _transcriptFilter.speakers;
  // In original-key mode, noise segments are treated as regular speakers for filtering.
  // In normal mode, noise has its own toggle so we exempt it from the speaker filter.
  const isNoise = source === _NOISE_LABEL || _manualNoiseKeys.has(source);
  const exemptNoise = isNoise && !_showOriginalKeys;
  if (speakers.size > 0 && !(source in SOURCE_META) && !speakers.has(source) && !exemptNoise) {
    seg.style.display = 'none'; return;
  }
  // Time range filter
  if (_transcriptFilter.timeMin > 0 || _transcriptFilter.timeMax < Infinity) {
    const segStart = parseFloat(seg.dataset.start || 0);
    const segEnd   = parseFloat(seg.dataset.end || Infinity);
    if (segEnd < _transcriptFilter.timeMin || segStart > _transcriptFilter.timeMax) {
      seg.style.display = 'none'; return;
    }
  }
  // Search filter: match against visible text (skip badge text for accuracy)
  const search = _transcriptFilter.search.toLowerCase().trim();
  if (search) {
    // Get text content excluding badge labels
    const textNodes = [];
    seg.childNodes.forEach(n => {
      if (n.nodeType === 3) textNodes.push(n.textContent);
      else if (!n.classList?.contains('src-badge')) textNodes.push(n.textContent);
    });
    if (!textNodes.join('').toLowerCase().includes(search)) {
      seg.style.display = 'none'; return;
    }
  }
  seg.style.display = '';
}

function applyTranscriptFilter() {
  _segmentRegistry.forEach(_applyFilterToSeg);
  _visibleRangesCache = null;  // filter changed - invalidate cached ranges
  _tnHighlightMatches();
  _refreshMinimap(true);
  renderTranscriptChapterHeadings();  // re-place headings + hide empty sections
}

// The time-range slider fires dozens-to-hundreds of 'input' events per drag, and
// each applyTranscriptFilter() does three O(N) passes over the transcript. Coalesce
// the heavy work behind one trailing animation frame so a whole drag costs one
// filter pass, not 3N per pixel. Module-scoped (not inside _tnRefreshTimeRange,
// which clones+rebinds the sliders) so a rebind can't leave competing timers.
let _tnFilterRaf = 0;
function _tnScheduleFilter() {
  if (_tnFilterRaf) return;
  _tnFilterRaf = requestAnimationFrame(() => {
    _tnFilterRaf = 0;
    applyTranscriptFilter();
    _updateFilterBtnState();
  });
}

function _updateFilterBtnState() {
  document.getElementById('transcript-filter-btn')
    ?.classList.toggle('active', _transcriptFilterActive());
}

// ── Panel toggle ──────────────────────────────────────────────────────────────

function openTranscriptFilter() {
  const filter_btn = document.getElementById('transcript-filter-btn');
  const panel = document.getElementById('transcript-navigator');
  if (!panel) return;
  const isOpen = !panel.classList.contains('collapsed');
  if (isOpen) {
    filter_btn?.classList.remove('open');
    panel.classList.add('collapsed');
    _syncPanelBottomRadius();
    return;
  }
  filter_btn?.classList.add('open');
  panel.classList.remove('collapsed');
  _syncPanelBottomRadius();
  _tnRefreshSpeakerPills();
  _tnRefreshReassignDropdowns();
  _tnRefreshTimeRange();
  _tnRefreshStats();
  const searchInput = document.getElementById('tn-search-input');
  if (searchInput) {
    searchInput.value = _transcriptFilter.search;
    searchInput.focus();
  }
}

// Wire up search input (called once on page load)
function _tnInitSearch() {
  const input = document.getElementById('tn-search-input');
  if (!input) return;
  let _debounce = null;
  input.addEventListener('input', () => {
    clearTimeout(_debounce);
    _debounce = setTimeout(() => {
      _transcriptFilter.search = input.value;
      applyTranscriptFilter();
      _updateFilterBtnState();
    }, 120);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      e.shiftKey ? tnPrevMatch() : tnNextMatch();
    }
    if (e.key === 'Escape') {
      input.value = '';
      _transcriptFilter.search = '';
      applyTranscriptFilter();
      _updateFilterBtnState();
    }
  });
}

// ── Search match highlighting ─────────────────────────────────────────────────

function _tnStripMarks() {
  document.querySelectorAll('#transcript .transcript-segment mark').forEach(mark => {
    const parent = mark.parentNode;
    mark.replaceWith(document.createTextNode(mark.textContent));
    parent.normalize();
  });
}

function _tnHighlightMatches() {
  _tnStripMarks();
  _navState.matches = [];
  _navState.currentIdx = -1;

  const search = _transcriptFilter.search.trim();
  if (!search) {
    _tnUpdateMatchCount();
    return;
  }

  const escaped = search.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(escaped, 'gi');

  _segmentRegistry.forEach(seg => {
    if (seg.style.display === 'none') return;
    // Only highlight in text nodes that are NOT inside a badge
    const textNodes = [];
    seg.childNodes.forEach(n => {
      if (n.nodeType === 3) textNodes.push(n);
      else if (!n.classList?.contains('src-badge') && !n.classList?.contains('speaker-picker')) {
        // Walk into child elements (like <mark> remnants after normalize)
        const walker = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
        let tn;
        while ((tn = walker.nextNode())) textNodes.push(tn);
      }
    });

    for (const textNode of textNodes) {
      const text = textNode.textContent;
      const parts = [];
      let lastIdx = 0;
      let match;
      re.lastIndex = 0;
      while ((match = re.exec(text)) !== null) {
        if (match.index > lastIdx) {
          parts.push(document.createTextNode(text.slice(lastIdx, match.index)));
        }
        const mark = document.createElement('mark');
        mark.textContent = match[0];
        _navState.matches.push(mark);
        parts.push(mark);
        lastIdx = re.lastIndex;
      }
      if (parts.length > 0) {
        if (lastIdx < text.length) {
          parts.push(document.createTextNode(text.slice(lastIdx)));
        }
        const frag = document.createDocumentFragment();
        parts.forEach(p => frag.appendChild(p));
        textNode.replaceWith(frag);
      }
    }
  });

  if (_navState.matches.length > 0) _navState.currentIdx = 0;
  _tnUpdateMatchCount();
  _tnScrollToCurrentMatch();
}

function _tnUpdateMatchCount() {
  const el = document.getElementById('tn-match-count');
  if (!el) return;
  const n = _navState.matches.length;
  if (n === 0 && !_transcriptFilter.search.trim()) {
    el.textContent = '';
  } else if (n === 0) {
    el.textContent = 'No matches';
  } else {
    el.textContent = `${_navState.currentIdx + 1} of ${n}`;
  }
}

function _tnScrollToCurrentMatch() {
  document.querySelectorAll('#transcript mark.tn-current-match').forEach(m => m.classList.remove('tn-current-match'));
  if (_navState.currentIdx < 0 || _navState.currentIdx >= _navState.matches.length) return;
  const mark = _navState.matches[_navState.currentIdx];
  mark.classList.add('tn-current-match');
  _doProgrammaticScroll(mark, { block: 'center', behavior: 'smooth' });
}

// Highlight search matches in a single segment (used for live-added segments)
function _tnHighlightInSeg(seg) {
  const search = _transcriptFilter.search.trim();
  if (!search) return;
  const escaped = search.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const re = new RegExp(escaped, 'gi');

  const textNodes = [];
  seg.childNodes.forEach(n => {
    if (n.nodeType === 3) textNodes.push(n);
    else if (!n.classList?.contains('src-badge') && !n.classList?.contains('speaker-picker')) {
      const walker = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
      let tn;
      while ((tn = walker.nextNode())) textNodes.push(tn);
    }
  });

  for (const textNode of textNodes) {
    const text = textNode.textContent;
    const parts = [];
    let lastIdx = 0;
    let match;
    re.lastIndex = 0;
    while ((match = re.exec(text)) !== null) {
      if (match.index > lastIdx) parts.push(document.createTextNode(text.slice(lastIdx, match.index)));
      const mark = document.createElement('mark');
      mark.textContent = match[0];
      _navState.matches.push(mark);
      parts.push(mark);
      lastIdx = re.lastIndex;
    }
    if (parts.length > 0) {
      if (lastIdx < text.length) parts.push(document.createTextNode(text.slice(lastIdx)));
      const frag = document.createDocumentFragment();
      parts.forEach(p => frag.appendChild(p));
      textNode.replaceWith(frag);
    }
  }
  _tnUpdateMatchCount();
}

function tnNextMatch() {
  if (_navState.matches.length === 0) return;
  _navState.currentIdx = (_navState.currentIdx + 1) % _navState.matches.length;
  _tnUpdateMatchCount();
  _tnScrollToCurrentMatch();
}

function tnPrevMatch() {
  if (_navState.matches.length === 0) return;
  _navState.currentIdx = (_navState.currentIdx - 1 + _navState.matches.length) % _navState.matches.length;
  _tnUpdateMatchCount();
  _tnScrollToCurrentMatch();
}

// ── Speaker pills ─────────────────────────────────────────────────────────────

function tnToggleKeyLabels() {
  _showOriginalKeys = !_showOriginalKeys;
  const btn = document.getElementById('tn-pill-keys-toggle');
  if (btn) btn.classList.toggle('active', _showOriginalKeys);
  _tnRefreshSpeakerPills();
  _tnRefreshTranscriptBadges();
  applyTranscriptFilter();
}

// Update all transcript segment badges to show either original speaker keys
// or display names, depending on _showOriginalKeys state.
function _tnRefreshTranscriptBadges() {
  _segmentRegistry.forEach(seg => {
    const badge = seg.querySelector('.src-badge.src-speaker');
    if (!badge) return;
    const speakerKey = badge.dataset.speakerKey;
    if (!speakerKey) return;

    const isNoise = badge.classList.contains('src-noise');
    if (isNoise) {
      // Noise badges: show original key with "Noise" alias in original-key mode
      badge.querySelector('.badge-alias')?.remove();
      if (_showOriginalKeys) {
        badge.textContent = speakerKey;
        const alias = document.createElement('span');
        alias.className = 'badge-alias';
        alias.textContent = 'Noise';
        badge.appendChild(alias);
      } else {
        badge.textContent = 'Noise';
      }
      return;
    }

    if (badge.dataset.override) return;  // per-segment overrides keep their custom text
    _setBadgeLabel(badge, speakerKey);
  });
}

// Set badge text content, adding an alias subtitle when in original-key mode
// and the speaker has a display name different from the key.
function _setBadgeLabel(badge, speakerKey) {
  const displayName = _speakerDisplayName(speakerKey) || speakerKey;
  // Remove any existing alias span
  badge.querySelector('.badge-alias')?.remove();

  if (_showOriginalKeys) {
    badge.childNodes.forEach(n => { if (n.nodeType === 3) n.remove(); });
    badge.textContent = speakerKey;
    if (displayName !== speakerKey) {
      const alias = document.createElement('span');
      alias.className = 'badge-alias';
      alias.textContent = displayName;
      badge.appendChild(alias);
    }
  } else {
    badge.textContent = displayName;
  }
}

function _tnRefreshSpeakerPills() {
  const container = document.getElementById('tn-speaker-pills');
  if (!container) return;
  container.innerHTML = '';

  const profiles = _getSortedSpeakerProfiles();
  // In original-key mode, each speaker key is its own group (no name-based merging)
  const groups = _showOriginalKeys
    ? profiles.map(p => ({
        name:        p.speaker_key,
        displayName: p.name || p.speaker_key,
        color:       p.color || null,
        speakerKeys: [p.speaker_key],
        custom:      p.custom || false,
      }))
    : _groupProfilesByName(profiles);

  // Split speakers vs noise PER KEY (see _partitionSpeakerGroupsByNoise) so a
  // named speaker with a noise-flagged fragment key still shows. In original-key
  // mode every key is its own pill with nothing folded.
  let speakerGroups, noiseKeys;
  if (_showOriginalKeys) {
    speakerGroups = groups;
    noiseKeys = [];
  } else {
    ({ speakerGroups, noiseKeys } = _partitionSpeakerGroupsByNoise(groups));
  }

  // Toggleable keys = the named speakers' keys (noise toggles via its own pill).
  const allKeys = new Set();
  speakerGroups.forEach(g => g.speakerKeys.forEach(k => allKeys.add(k)));

  // Sort: labeled speakers first (alphabetical), then unlabeled (alphabetical)
  speakerGroups.sort((a, b) => {
    const aDefault = _isDefaultName(a.name);
    const bDefault = _isDefaultName(b.name);
    if (aDefault !== bDefault) return aDefault ? 1 : -1;
    return a.name.localeCompare(b.name, undefined, { numeric: true });
  });

  speakerGroups.forEach(g => {
    const color = g.color || speakerColor(g.speakerKeys[0]);
    const count = g.speakerKeys.reduce((sum, k) => sum + _speakerBadgeCount(k), 0);
    const isOn = _transcriptFilter.speakers.size === 0
      || g.speakerKeys.some(k => _transcriptFilter.speakers.has(k));

    const pill = document.createElement('button');
    pill.className = 'tn-pill' + (isOn ? '' : ' tn-pill-off');
    pill.style.backgroundColor = color + '33';
    pill.style.color = color;
    pill.style.borderColor = color + '60';
    pill.dataset.speakerKeys = JSON.stringify(g.speakerKeys);

    // In original-key mode, show key name with display name subtitle if different
    const pillLabel = _showOriginalKeys && g.displayName && g.displayName !== g.name
      ? `${escapeHtml(g.name)} <span class="tn-pill-alias">${escapeHtml(g.displayName)}</span>`
      : escapeHtml(g.name);
    pill.innerHTML = `${pillLabel} <span class="tn-pill-count">${count}</span>`;
    pill.title = _showOriginalKeys && g.displayName && g.displayName !== g.name
      ? `${g.name} → ${g.displayName} - ${count} segment${count !== 1 ? 's' : ''}\nRight-click: jump to next`
      : `${g.name} - ${count} segment${count !== 1 ? 's' : ''}\nRight-click: jump to next`;

    pill.addEventListener('click', () => {
      _tnToggleSpeakerPill(g.speakerKeys, allKeys);
    });

    pill.addEventListener('contextmenu', e => {
      e.preventDefault();
      _tnJumpToNextSpeaker(g.speakerKeys, 1);
    });

    container.appendChild(pill);
  });

  // Single merged noise pill - every noise-flagged key combined
  const totalNoiseCount = noiseKeys.reduce((sum, k) => sum + _speakerBadgeCount(k), 0);
  if (totalNoiseCount > 0) {
    const pill = document.createElement('button');
    const active = _showNoise || _noiseSolo;
    pill.className = 'tn-pill tn-pill-noise' + (active ? (_noiseSolo ? ' tn-pill-solo' : '') : ' tn-pill-off');
    pill.style.backgroundColor = _NOISE_COLOR + '33';
    pill.style.color = _NOISE_COLOR;
    pill.style.borderColor = _NOISE_COLOR + '60';
    pill.innerHTML = `<i class="fa-solid fa-volume-xmark"></i> Noise <span class="tn-pill-count">${totalNoiseCount}</span>`;
    pill.title = `${totalNoiseCount} noise/filler segment${totalNoiseCount !== 1 ? 's' : ''}\nClick to solo · Right-click to jump`;
    pill.addEventListener('click', () => {
      if (_noiseSolo) {
        // Un-solo → back to normal (noise hidden)
        _noiseSolo = false;
        _showNoise = false;
        _transcriptFilter.speakers.clear();
      } else if (_transcriptFilter.speakers.size > 0) {
        // In speaker filter mode: toggle noise visibility alongside
        _showNoise = !_showNoise;
      } else if (_showNoise) {
        // Noise visible, no filter → hide noise
        _showNoise = false;
      } else {
        // Noise hidden, no filter → solo noise
        _noiseSolo = true;
        _showNoise = true;
        _transcriptFilter.speakers = new Set(['__none__']);
      }
      applyTranscriptFilter();
      _tnRefreshSpeakerPills();
      _updateFilterBtnState();
    });
    pill.addEventListener('contextmenu', e => {
      e.preventDefault();
      _tnJumpToNextSpeaker(noiseKeys, 1);
    });
    container.appendChild(pill);
  }

  // Keep the analytics panel in lock-step with the chips: it reads the same
  // speaker data, so any change that refreshes the pills must refresh it too.
  // (_refreshAnalytics is a no-op while the panel is collapsed.)
  _refreshAnalytics();
}

function _tnToggleSpeakerPill(keys, allKeys) {
  // Exit noise-solo mode when clicking a speaker pill
  if (_noiseSolo) {
    _noiseSolo = false;
    _showNoise = false;
    _transcriptFilter.speakers.clear();
  }
  const wasShowingAll = _transcriptFilter.speakers.size === 0;

  if (wasShowingAll) {
    // First click when all are showing: solo this speaker (hide all others)
    _transcriptFilter.speakers = new Set(keys);
  } else {
    // Check if this group is currently visible
    const isOn = keys.some(k => _transcriptFilter.speakers.has(k));
    if (isOn) {
      keys.forEach(k => _transcriptFilter.speakers.delete(k));
      // If none left, show all
      if (_transcriptFilter.speakers.size === 0) {
        // all off → show all
      }
    } else {
      keys.forEach(k => _transcriptFilter.speakers.add(k));
      // If all are now on, clear filter
      if (allKeys && _transcriptFilter.speakers.size >= allKeys.size) {
        _transcriptFilter.speakers.clear();
      }
    }
  }

  applyTranscriptFilter();
  _updateFilterBtnState();
  _tnRefreshSpeakerPills();
}

function tnToggleAllSpeakers(showAll) {
  if (showAll) {
    _transcriptFilter.speakers.clear();
  } else {
    // Add ALL speaker keys to hide everything
    const groups = _groupProfilesByName(_getSortedSpeakerProfiles());
    const allKeys = new Set();
    groups.forEach(g => g.speakerKeys.forEach(k => allKeys.add(k)));
    // Set speakers to a set with a sentinel to trigger filtering
    // But the filter logic says: if speakers.size > 0 and source NOT in set → hide
    // So we need the set to contain NO real keys → use a dummy key
    _transcriptFilter.speakers = new Set(['__none__']);
  }
  applyTranscriptFilter();
  _updateFilterBtnState();
  _tnRefreshSpeakerPills();
}

function _tnJumpToNextSpeaker(speakerKeys, direction) {
  const keysSet = new Set(speakerKeys);
  const allSegs = _segmentRegistry;
  const transcriptEl = document.getElementById('transcript');
  const scrollTop = transcriptEl.scrollTop;
  const containerTop = transcriptEl.getBoundingClientRect().top;

  // Find segments matching these speaker keys
  const matching = allSegs.filter(seg =>
    seg.style.display !== 'none' && keysSet.has(seg.dataset.transcriptSource)
  );
  if (matching.length === 0) return;

  // Find first segment below current viewport center
  const viewCenter = scrollTop + transcriptEl.clientHeight / 2;
  let target = null;
  if (direction > 0) {
    target = matching.find(seg => seg.offsetTop > viewCenter + 10);
    if (!target) target = matching[0]; // wrap around
  } else {
    for (let i = matching.length - 1; i >= 0; i--) {
      if (matching[i].offsetTop < viewCenter - 10) { target = matching[i]; break; }
    }
    if (!target) target = matching[matching.length - 1]; // wrap around
  }

  if (target) {
    _doProgrammaticScroll(target, { block: 'center', behavior: 'smooth' });
    target.classList.add('playing');
    setTimeout(() => target.classList.remove('playing'), 1500);
  }
}

// ── Quick reassign ────────────────────────────────────────────────────────────

function _tnRefreshReassignDropdowns() {
  const fromSel = document.getElementById('tn-reassign-from');
  const toSel = document.getElementById('tn-reassign-to');
  if (!fromSel || !toSel) return;

  const groups = _groupProfilesByName(_getSortedSpeakerProfiles());
  const names = [];
  groups.forEach(g => {
    const name = g.name;
    if (name && !names.includes(name)) names.push(name);
  });

  // Rebuild "from" dropdown
  fromSel.innerHTML = '<option value="" disabled selected>from…</option>';
  names.forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    fromSel.appendChild(opt);
  });

  // Rebuild "to" dropdown - includes all names, plus [Noise] option
  toSel.innerHTML = '<option value="" disabled selected>to…</option>';
  names.forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    toSel.appendChild(opt);
  });
  const noiseSep = document.createElement('option');
  noiseSep.disabled = true;
  noiseSep.textContent = '──────────';
  toSel.appendChild(noiseSep);
  const noiseOpt = document.createElement('option');
  noiseOpt.value = _NOISE_LABEL;
  noiseOpt.textContent = '🔇 Mark as Noise';
  toSel.appendChild(noiseOpt);
}

async function tnApplyReassign() {
  const fromName = document.getElementById('tn-reassign-from')?.value;
  const toName   = document.getElementById('tn-reassign-to')?.value;
  if (!fromName || !toName || fromName === toName) return;

  const visibleOnly = document.getElementById('tn-reassign-visible-only')?.checked;
  const allSegs = _segmentRegistry;
  const targets = allSegs.filter(seg => {
    if (visibleOnly && seg.style.display === 'none') return false;
    const badge = seg.querySelector('.src-speaker');
    return badge && badge.textContent.trim().toLowerCase() === fromName.toLowerCase();
  });

  if (targets.length === 0) return;

  const toLabel = toName === _NOISE_LABEL ? 'Noise' : `"${toName}"`;
  if (!await uiConfirm({ title: 'Reassign segments?', message: `Reassign ${targets.length} segment${targets.length !== 1 ? 's' : ''} from "${fromName}" to ${toLabel}?`, confirmLabel: 'Reassign' })) return;

  if (toName === _NOISE_LABEL) {
    // Collect unique speaker_keys from target segments and mark them as noise
    const keys = new Set(targets.map(s => s.dataset.transcriptSource).filter(Boolean));
    for (const k of keys) await _markSpeakerAsNoise(k);
  } else {
    for (const seg of targets) {
      const badge = seg.querySelector('.src-speaker');
      if (!badge) continue;
      badge.textContent = toName;
      badge.dataset.override = '1';
      const segId = badge.dataset.segId || seg.dataset.segId;
      if (segId) persistSegmentOverride(segId, toName).catch(() => {});
    }
    // Refresh the panel
    _tnRefreshSpeakerPills();
    _tnRefreshReassignDropdowns();
    _tnRefreshStats();
  }
}

// ── Time range filter ─────────────────────────────────────────────────────────

let _tnRangeMaxPinned = true; // true = max handle tracks the live end of the timeline

function _tnGetTimelineBounds() {
  const allSegs = document.querySelectorAll('#transcript .transcript-segment[data-start]');
  let minT = Infinity, maxT = 0;
  allSegs.forEach(seg => {
    const s = parseFloat(seg.dataset.start || 0);
    const e = parseFloat(seg.dataset.end || 0);
    if (s < minT) minT = s;
    if (e > maxT) maxT = e;
  });
  if (minT === Infinity) { minT = 0; maxT = 0; }
  return { minT, maxT };
}

function _tnRefreshTimeRange() {
  const { minT, maxT } = _tnGetTimelineBounds();

  const rangeMin = document.getElementById('tn-range-min');
  const rangeMax = document.getElementById('tn-range-max');
  if (!rangeMin || !rangeMax) return;

  rangeMin.min = rangeMax.min = 0;
  rangeMin.max = rangeMax.max = maxT || 100;
  rangeMin.value = _transcriptFilter.timeMin || 0;

  if (_tnRangeMaxPinned || _transcriptFilter.timeMax === Infinity) {
    rangeMax.value = maxT;
    _transcriptFilter.timeMax = Infinity;
    _tnRangeMaxPinned = true;
  } else {
    rangeMax.value = Math.min(_transcriptFilter.timeMax, maxT);
  }

  _tnUpdateRangeFill();
  _tnUpdateTimeLabels();

  // Remove old listeners by replacing elements
  const newMin = rangeMin.cloneNode(true);
  const newMax = rangeMax.cloneNode(true);
  rangeMin.replaceWith(newMin);
  rangeMax.replaceWith(newMax);

  newMin.addEventListener('input', () => {
    if (parseFloat(newMin.value) > parseFloat(newMax.value)) newMin.value = newMax.value;
    _transcriptFilter.timeMin = parseFloat(newMin.value);
    // Cheap visuals stay synchronous so the thumb/fill/labels track at 60fps…
    _tnUpdateRangeFill();
    _tnUpdateTimeLabels();
    // …only the heavy O(N) filter is coalesced to one frame.
    _tnScheduleFilter();
  });
  newMax.addEventListener('input', () => {
    if (parseFloat(newMax.value) < parseFloat(newMin.value)) newMax.value = newMin.value;
    const maxVal = parseFloat(newMax.max);
    const atEnd = parseFloat(newMax.value) >= maxVal - 0.5;
    _tnRangeMaxPinned = atEnd;
    _transcriptFilter.timeMax = atEnd ? Infinity : parseFloat(newMax.value);
    _tnUpdateRangeFill();
    _tnUpdateTimeLabels();
    _tnScheduleFilter();
  });
  // Pointer-up flush so the final position always applies even if the last
  // 'input' frame was already in flight.
  newMin.addEventListener('change', _tnScheduleFilter);
  newMax.addEventListener('change', _tnScheduleFilter);
}

// Called when new segments arrive during live recording to extend the slider
function _tnExtendTimeRange() {
  const panel = document.getElementById('transcript-navigator');
  if (!panel || panel.classList.contains('collapsed')) return;

  const { maxT } = _tnGetTimelineBounds();
  const rangeMin = document.getElementById('tn-range-min');
  const rangeMax = document.getElementById('tn-range-max');
  if (!rangeMin || !rangeMax) return;

  // Extend the slider max to cover new segments
  rangeMin.max = rangeMax.max = maxT || 100;

  // If pinned to the end, keep the max handle at the right edge
  if (_tnRangeMaxPinned) {
    rangeMax.value = maxT;
    _transcriptFilter.timeMax = Infinity;
  }

  _tnUpdateRangeFill();
  _tnUpdateTimeLabels();
}

function _tnUpdateRangeFill() {
  const fill = document.getElementById('tn-range-fill');
  const rangeMin = document.getElementById('tn-range-min');
  const rangeMax = document.getElementById('tn-range-max');
  if (!fill || !rangeMin || !rangeMax) return;
  const max = parseFloat(rangeMin.max) || 100;
  const lo = parseFloat(rangeMin.value) / max * 100;
  const hi = parseFloat(rangeMax.value) / max * 100;
  fill.style.left = lo + '%';
  fill.style.right = (100 - hi) + '%';
}

function _tnUpdateTimeLabels() {
  const rangeMin = document.getElementById('tn-range-min');
  const rangeMax = document.getElementById('tn-range-max');
  const labelStart = document.getElementById('tn-time-label-start');
  const labelEnd = document.getElementById('tn-time-label-end');
  if (labelStart && rangeMin) labelStart.textContent = fmtDuration(parseFloat(rangeMin.value));
  if (labelEnd && rangeMax) labelEnd.textContent = fmtDuration(parseFloat(rangeMax.value));
}

// ── Speaker statistics ────────────────────────────────────────────────────────

// ── Analytics Panel ──────────────────────────────────────────────────────────

let _analyticsBarObserver = null;
let _analyticsTlObserver = null;

function toggleAnalyticsPanel() {
  const panel = document.getElementById('analytics-panel');
  if (!panel) return;
  const btn = document.getElementById('analytics-btn');
  const isOpen = !panel.classList.contains('collapsed');
  panel.classList.toggle('collapsed');
  if (btn) btn.classList.toggle('active', !isOpen);
  if (!isOpen) _refreshAnalytics();
  _syncPanelBottomRadius();
}

function _refreshAnalytics() {
  const panel = document.getElementById('analytics-panel');
  if (!panel || panel.classList.contains('collapsed')) return;

  const groups = _groupProfilesByName(_getSortedSpeakerProfiles());
  const allSegs = _segmentRegistry;

  // Gather per-speaker data
  const speakerData = [];
  let totalSegCount = 0;
  let totalSpeakTime = 0;
  let totalWords = 0;
  let sessionStart = Infinity, sessionEnd = 0;

  // Aggregate noise data separately
  let noiseData = { name: 'Noise', color: _NOISE_COLOR, segCount: 0, speakTime: 0, words: 0, segments: [] };

  // Tally segments/time/words for a set of speaker keys, extending the session
  // span as a side effect.
  const tallyKeys = (keys) => {
    const keysSet = new Set(keys);
    let segCount = 0, speakTime = 0, words = 0;
    const segments = [];
    allSegs.forEach(seg => {
      if (!keysSet.has(seg.dataset.transcriptSource)) return;
      segCount++;
      const s = parseFloat(seg.dataset.start || 0);
      const e = parseFloat(seg.dataset.end || 0);
      if (e > s) {
        speakTime += e - s;
        segments.push({ start: s, end: e });
        if (s < sessionStart) sessionStart = s;
        if (e > sessionEnd) sessionEnd = e;
      }
      // Count words from text content (skip badge)
      const badge = seg.querySelector('.src-badge');
      let text = '';
      for (let n = badge ? badge.nextSibling : seg.firstChild; n; n = n.nextSibling)
        text += n.textContent || '';
      words += text.trim().split(/\s+/).filter(w => w).length;
    });
    return { segCount, speakTime, words, segments };
  };

  // Split speakers vs noise PER KEY (see _partitionSpeakerGroupsByNoise) so a
  // named speaker with a noise-flagged fragment key still appears here.
  const { speakerGroups, noiseKeys } = _partitionSpeakerGroupsByNoise(groups);

  speakerGroups.forEach(g => {
    const { segCount, speakTime, words, segments } = tallyKeys(g.speakerKeys);
    if (segCount === 0) return;
    const color = g.color || speakerColor(g.speakerKeys[0]);
    speakerData.push({ name: g.name, color, segCount, speakTime, words, segments });
    totalSegCount += segCount;
    totalSpeakTime += speakTime;
    totalWords += words;
  });

  if (noiseKeys.length) {
    const { segCount, speakTime, words, segments } = tallyKeys(noiseKeys);
    noiseData.segCount += segCount;
    noiseData.speakTime += speakTime;
    noiseData.words += words;
    noiseData.segments.push(...segments);
  }

  // Sort by speaking time descending
  speakerData.sort((a, b) => b.speakTime - a.speakTime);

  const sessionDuration = sessionEnd > sessionStart ? sessionEnd - sessionStart : 0;
  const wpm = totalSpeakTime > 0 ? Math.round(totalWords / (totalSpeakTime / 60)) : 0;

  // ── KPIs ─────────────────────────────────────────
  const kpiEl = document.getElementById('analytics-kpis');
  kpiEl.innerHTML = '';

  const kpis = [
    { value: fmtDuration(sessionDuration), label: 'Duration' },
    { value: speakerData.length, label: 'Speakers' },
    { value: totalSegCount, label: 'Segments' },
    { value: wpm, label: 'Avg WPM' },
  ];
  // Donut (left half)
  const donutKpi = document.createElement('div');
  donutKpi.className = 'analytics-kpi analytics-kpi-donut';
  donutKpi.innerHTML = _buildDonutSVG(speakerData, 110);
  kpiEl.appendChild(donutKpi);

  // KPI grid (right half)
  const kpiGrid = document.createElement('div');
  kpiGrid.className = 'analytics-kpi-grid';
  kpis.forEach(k => {
    const card = document.createElement('div');
    card.className = 'analytics-kpi';
    card.innerHTML = `<span class="analytics-kpi-value">${k.value}</span><span class="analytics-kpi-label">${k.label}</span>`;
    kpiGrid.appendChild(card);
  });
  kpiEl.appendChild(kpiGrid);

  // ── Speaking Time Bars ───────────────────────────
  const maxTime = speakerData.reduce((m, d) => Math.max(m, d.speakTime), 0);
  const timeBars = document.getElementById('analytics-time-bars');
  timeBars.innerHTML = '';
  speakerData.forEach(d => {
    const pct = maxTime > 0 ? (d.speakTime / maxTime) * 100 : 0;
    const sharePct = totalSpeakTime > 0 ? Math.round((d.speakTime / totalSpeakTime) * 100) : 0;
    const row = document.createElement('div');
    row.className = 'analytics-bar-row';
    row.innerHTML = `
      <span class="analytics-bar-label"><span class="analytics-bar-dot" style="background:${d.color}"></span>${escapeHtml(d.name)}</span>
      <span class="analytics-bar-track"><span class="analytics-bar-fill" data-pct="${pct}" style="width:0%;background:${d.color}"></span></span>
      <span class="analytics-bar-value">${fmtDuration(d.speakTime)} (${sharePct}%)</span>
    `;
    timeBars.appendChild(row);
  });
  if (noiseData.segCount > 0) {
    const pct = maxTime > 0 ? (noiseData.speakTime / maxTime) * 100 : 0;
    const row = document.createElement('div');
    row.className = 'analytics-bar-row analytics-bar-noise';
    row.innerHTML = `
      <span class="analytics-bar-label"><span class="analytics-bar-dot" style="background:${_NOISE_COLOR}"></span>Noise</span>
      <span class="analytics-bar-track"><span class="analytics-bar-fill" data-pct="${pct}" style="width:0%;background:${_NOISE_COLOR}"></span></span>
      <span class="analytics-bar-value">${fmtDuration(noiseData.speakTime)}</span>
    `;
    timeBars.appendChild(row);
  }

  // ── Segment Count Bars ───────────────────────────
  const maxSegs = speakerData.reduce((m, d) => Math.max(m, d.segCount), 0);
  const segBars = document.getElementById('analytics-seg-bars');
  segBars.innerHTML = '';
  speakerData.forEach(d => {
    const pct = maxSegs > 0 ? (d.segCount / maxSegs) * 100 : 0;
    const row = document.createElement('div');
    row.className = 'analytics-bar-row';
    row.innerHTML = `
      <span class="analytics-bar-label"><span class="analytics-bar-dot" style="background:${d.color}"></span>${escapeHtml(d.name)}</span>
      <span class="analytics-bar-track"><span class="analytics-bar-fill" data-pct="${pct}" style="width:0%;background:${d.color}"></span></span>
      <span class="analytics-bar-value">${d.segCount} seg${d.segCount !== 1 ? 's' : ''}</span>
    `;
    segBars.appendChild(row);
  });
  if (noiseData.segCount > 0) {
    const pct = maxSegs > 0 ? (noiseData.segCount / maxSegs) * 100 : 0;
    const row = document.createElement('div');
    row.className = 'analytics-bar-row analytics-bar-noise';
    row.innerHTML = `
      <span class="analytics-bar-label"><span class="analytics-bar-dot" style="background:${_NOISE_COLOR}"></span>Noise</span>
      <span class="analytics-bar-track"><span class="analytics-bar-fill" data-pct="${pct}" style="width:0%;background:${_NOISE_COLOR}"></span></span>
      <span class="analytics-bar-value">${noiseData.segCount} seg${noiseData.segCount !== 1 ? 's' : ''}</span>
    `;
    segBars.appendChild(row);
  }

  // ── Timeline ─────────────────────────────────────
  const tlEl = document.getElementById('analytics-timeline');
  tlEl.innerHTML = '';
  if (sessionDuration > 0) {
    let rowIdx = 0;
    speakerData.forEach(d => {
      const row = document.createElement('div');
      row.className = 'analytics-tl-row';
      let segsHtml = '';
      d.segments.forEach(s => {
        const left = ((s.start - sessionStart) / sessionDuration) * 100;
        const width = Math.max(((s.end - s.start) / sessionDuration) * 100, 0.5);
        segsHtml += `<span class="analytics-tl-seg" style="left:${left}%;width:${width}%;background:${d.color}"></span>`;
      });
      row.innerHTML = `
        <span class="analytics-tl-label">${escapeHtml(d.name)}</span>
        <span class="analytics-tl-track">${segsHtml}</span>
      `;
      row.dataset.rowIdx = rowIdx++;
      tlEl.appendChild(row);
    });

    // Noise timeline row
    if (noiseData.segCount > 0) {
      const row = document.createElement('div');
      row.className = 'analytics-tl-row analytics-tl-noise';
      let segsHtml = '';
      noiseData.segments.forEach(s => {
        const left = ((s.start - sessionStart) / sessionDuration) * 100;
        const width = Math.max(((s.end - s.start) / sessionDuration) * 100, 0.5);
        segsHtml += `<span class="analytics-tl-seg" style="left:${left}%;width:${width}%;background:${_NOISE_COLOR}"></span>`;
      });
      row.innerHTML = `
        <span class="analytics-tl-label">Noise</span>
        <span class="analytics-tl-track">${segsHtml}</span>
      `;
      row.dataset.rowIdx = rowIdx++;
      tlEl.appendChild(row);
    }

    // Animate timeline rows in with stagger
    if (_analyticsTlObserver) _analyticsTlObserver.disconnect();
    _analyticsTlObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          const row = entry.target;
          const delay = parseInt(row.dataset.rowIdx) * 50;
          setTimeout(() => {
            row.classList.add('visible');
            row.querySelectorAll('.analytics-tl-seg').forEach((seg, i) => {
              setTimeout(() => seg.classList.add('visible'), i * 8);
            });
          }, delay);
          _analyticsTlObserver.unobserve(row);
        }
      });
    }, { root: panel, threshold: 0.1 });
    tlEl.querySelectorAll('.analytics-tl-row').forEach(row => {
      _analyticsTlObserver.observe(row);
    });
  }

  // Empty state
  if (speakerData.length === 0) {
    kpiEl.innerHTML = '<div class="analytics-kpi" style="flex:1;align-items:center;padding:20px"><span class="analytics-kpi-label">No speaker data yet</span></div>';
    timeBars.innerHTML = '';
    segBars.innerHTML = '';
    tlEl.innerHTML = '';
    return;
  }

  // Animate bars as they scroll into view
  if (_analyticsBarObserver) _analyticsBarObserver.disconnect();
  _analyticsBarObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        const bar = entry.target.querySelector('.analytics-bar-fill');
        if (bar) bar.style.width = bar.dataset.pct + '%';
        _analyticsBarObserver.unobserve(entry.target);
      }
    });
  }, { root: panel, threshold: 0.1 });
  panel.querySelectorAll('.analytics-bar-row').forEach(row => {
    _analyticsBarObserver.observe(row);
  });
}

function _buildDonutSVG(speakerData, size) {
  const total = speakerData.reduce((s, d) => s + d.speakTime, 0);
  if (total === 0 || speakerData.length === 0) {
    return `<div class="analytics-donut-wrap"><svg width="${size}" height="${size}" viewBox="0 0 36 36">
      <circle cx="18" cy="18" r="13" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="7"/>
    </svg></div>`;
  }
  const r = 13, c = 2 * Math.PI * r;
  let offset = 0;
  let arcs = '';
  speakerData.forEach(d => {
    const pct = d.speakTime / total;
    const dash = pct * c;
    const gap = c - dash;
    arcs += `<circle cx="18" cy="18" r="${r}" fill="none" stroke="${d.color}" stroke-width="7"
      stroke-dasharray="${dash} ${gap}" stroke-dashoffset="${-offset}"
      transform="rotate(-90 18 18)" style="opacity:0.85"/>`;
    offset += dash;
  });
  return `<div class="analytics-donut-wrap"><svg width="${size}" height="${size}" viewBox="0 0 36 36">${arcs}</svg></div>`;
}

// Keep tnToggleStats as a no-op for backwards compatibility
function tnToggleStats() {}
function _tnRefreshStats() {}

// ── Clear all filters ─────────────────────────────────────────────────────────

function tnClearAll() {
  _transcriptFilter.search = '';
  _transcriptFilter.speakers.clear();
  _transcriptFilter.timeMin = 0;
  _transcriptFilter.timeMax = Infinity;
  _tnRangeMaxPinned = true;
  _navState.matches = [];
  _navState.currentIdx = -1;
  _tnStripMarks();
  applyTranscriptFilter();
  _updateFilterBtnState();

  // Reset UI
  const searchInput = document.getElementById('tn-search-input');
  if (searchInput) searchInput.value = '';
  _tnRefreshSpeakerPills();
  _tnRefreshTimeRange();
  _tnRefreshStats();
}

async function clearTranscript() {
  if (!await uiConfirm({ title: 'Clear transcript?', message: 'The transcript will need to be reanalyzed for speaker labeling.', confirmLabel: 'Clear', danger: true })) return;
  document.getElementById('transcript').innerHTML =
    '<p class="empty-hint">Transcript cleared.</p>';
}

/* ── Summary ─────────────────────────────────────────────────────────────── */
function showSummaryBadge(show) {
  document.getElementById('summary-badge').classList.toggle('hidden', !show);
}

async function triggerSummary() {
  if (!state.sessionId) return;
  await fetch('/api/summarize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId }),
  });
}

async function copySummary(stripTimestamps = false) {
  const el = document.getElementById('summary');
  if (!el || !el.textContent.trim()) return;

  // For the "without timestamps" variant, read from a clone with the
  // .timestamp-link pills removed. innerText needs a rendered layout to emit
  // newlines between blocks, so the clone is briefly mounted off-screen (keeping
  // its #summary id so it renders identically) and torn down afterwards.
  let src = el;
  let detach = null;
  if (stripTimestamps) {
    const clone = el.cloneNode(true);
    clone.querySelectorAll('.timestamp-link').forEach(a => a.remove());
    clone.setAttribute('aria-hidden', 'true');
    clone.style.cssText = 'position:fixed;left:-99999px;top:0';
    document.body.appendChild(clone);
    src = clone;
    detach = () => clone.remove();
  }

  try {
    // Copy as rich text (HTML) so headings/lists/formatting are preserved on paste
    const html = src.innerHTML;
    const plain = src.innerText;
    const blob = new Blob([html], { type: 'text/html' });
    const blobPlain = new Blob([plain], { type: 'text/plain' });
    await navigator.clipboard.write([
      new ClipboardItem({
        'text/html': blob,
        'text/plain': blobPlain,
      }),
    ]);
    _flashSummaryCopied();
  } catch {
    // Fallback: plain text
    await navigator.clipboard.writeText(src.innerText);
  } finally {
    if (detach) detach();
  }
}

// Brief green-check feedback on the summary copy button after a successful copy.
function _flashSummaryCopied() {
  const btn = document.getElementById('btn-copy-summary');
  const icon = btn?.querySelector('i');
  if (!icon) return;
  icon.className = 'fa-solid fa-check';
  icon.style.color = '#00b464';
  clearTimeout(btn._copyTimer);
  btn._copyTimer = setTimeout(() => {
    icon.className = 'fa-duotone fa-copy';
    icon.style.color = '';
  }, 1500);
}

// Popout shown when the summary copy button is clicked, letting the user choose
// whether to keep the [M:SS] timestamp pills. Reuses the .session-menu styling -
// the app's shared "anchored dropdown" look.
function openCopySummaryMenu(btn) {
  // Clicking the button again toggles the menu closed.
  if (document.getElementById('copy-summary-menu')) {
    _closeCopySummaryMenu();
    return;
  }

  const menu = document.createElement('div');
  menu.className = 'session-menu';
  menu.id = 'copy-summary-menu';

  const withTs = document.createElement('div');
  withTs.className = 'session-menu-item';
  withTs.innerHTML = '<i class="fa-duotone fa-copy"></i>  Copy with timestamps';
  withTs.addEventListener('click', ev => { ev.stopPropagation(); _closeCopySummaryMenu(); copySummary(false); });
  menu.appendChild(withTs);

  const withoutTs = document.createElement('div');
  withoutTs.className = 'session-menu-item';
  withoutTs.innerHTML = '<i class="fa-duotone fa-copy"></i>  Copy without timestamps';
  withoutTs.addEventListener('click', ev => { ev.stopPropagation(); _closeCopySummaryMenu(); copySummary(true); });
  menu.appendChild(withoutTs);

  document.body.appendChild(menu);

  // Drop down from the button, right-aligned, clamped to the viewport.
  const rect = btn.getBoundingClientRect();
  const menuRect = menu.getBoundingClientRect();
  let top  = rect.bottom + window.scrollY + 4;
  let left = rect.right + window.scrollX - menuRect.width;
  if (left < 4) left = 4;
  if (top + menuRect.height > window.innerHeight + window.scrollY) {
    top = rect.top + window.scrollY - menuRect.height - 4;  // flip above if no room below
  }
  menu.style.top  = top  + 'px';
  menu.style.left = left + 'px';

  setTimeout(() => document.addEventListener('click', _closeCopySummaryMenu, { once: true }), 0);
}

function _closeCopySummaryMenu() {
  const m = document.getElementById('copy-summary-menu');
  if (m) m.remove();
}

function toggleSummaryPrompt() {
  const area = document.getElementById('summary-prompt-area');
  const btn  = document.getElementById('summary-prompt-toggle');
  const hidden = area.classList.toggle('hidden');
  btn.classList.toggle('active', !hidden);
  localStorage.setItem('summary-prompt-open', hidden ? '' : '1');
  if (!hidden) {
    // Focus whichever pane is active
    const activeTab = area.querySelector('.sp-tab.active')?.dataset.spTab || 'instructions';
    const focusId = activeTab === 'system' ? 'summary-system-prompt' : 'summary-custom-prompt';
    document.getElementById(focusId)?.focus();
    // Refresh the system-prompt source chip when the panel opens
    if (activeTab === 'system') _refreshSummarySystemPromptUI();
  }
  _syncSummaryBottomRadius();
}

function _spSwitchTab(name) {
  const area = document.getElementById('summary-prompt-area');
  if (!area) return;
  area.querySelectorAll('.sp-tab').forEach(t => {
    const active = t.dataset.spTab === name;
    t.classList.toggle('active', active);
    t.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  area.querySelectorAll('.sp-pane').forEach(p => {
    p.classList.toggle('hidden', p.dataset.spPane !== name);
  });
  if (name === 'system') _refreshSummarySystemPromptUI();
  // Focus the visible textarea
  const focusId = name === 'system' ? 'summary-system-prompt' : 'summary-custom-prompt';
  document.getElementById(focusId)?.focus();
}

let _promptSaveTimer = null;
function saveSummaryPrompt() {
  // "Save" the custom instructions: persist locally + sync to active backend state.
  // This is auto-saved per-session because instructions are session-scoped scratchpad.
  clearTimeout(_promptSaveTimer);
  _promptSaveTimer = setTimeout(async () => {
    const text = document.getElementById('summary-custom-prompt').value;
    const key = 'summary-prompt:' + (state.sessionId || 'new');
    localStorage.setItem(key, text);
    await fetch('/api/custom-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ custom_prompt: text }),
    });
    // Show transient saved indicator
    const st = document.getElementById('summary-instr-status');
    if (st) {
      st.textContent = 'Saved';
      st.classList.add('saved');
      setTimeout(() => { st.textContent = ''; st.classList.remove('saved'); }, 1500);
    }
    // If "use as default" is checked, also persist global default
    const tog = document.getElementById('summary-instr-default-toggle');
    if (tog && tog.checked) {
      _saveSummaryDefaultInstructions(text);
    }
  }, 600);
}

function _onSummaryInstrDefaultToggle(checked) {
  // When toggled ON: save the current text as the global default.
  // When toggled OFF: clear the global default (but keep the session text).
  const text = document.getElementById('summary-custom-prompt').value || '';
  if (checked) {
    _saveSummaryDefaultInstructions(text);
  } else {
    _saveSummaryDefaultInstructions('');
  }
}

function _saveSummaryDefaultInstructions(text) {
  savePref('summary_default_instructions', text);
  const tog = document.getElementById('summary-instr-default-toggle');
  if (tog) tog.checked = !!(text && text.length);
}

function _applyPromptText(text) {
  const ta = document.getElementById('summary-custom-prompt');
  if (ta) ta.value = text || '';
  // Reflect "default" toggle state from prefs
  const tog = document.getElementById('summary-instr-default-toggle');
  if (tog) {
    const def = (_prefs.summary_default_instructions || '').trim();
    // The toggle reads as ON when a non-empty default exists AND it matches what's
    // currently in the textarea (so the user can see whether the textarea content
    // *is* the default). It's still controllable manually via the checkbox.
    tog.checked = !!def && def === (text || '').trim();
  }
  const show = localStorage.getItem('summary-prompt-open') === '1';
  document.getElementById('summary-prompt-area').classList.toggle('hidden', !show);
  document.getElementById('summary-prompt-toggle').classList.toggle('active', show);
  _syncSummaryBottomRadius();
}

async function loadSummaryPrompt() {
  const key = 'summary-prompt:' + (state.sessionId || 'new');
  const stored = localStorage.getItem(key);
  let initialText = '';
  if (stored !== null) {
    initialText = stored;
  } else {
    // No per-session entry yet: seed from the user's "default instructions" pref
    // (if set), otherwise from whatever the backend already has.
    const def = (_prefs.summary_default_instructions || '');
    if (def) {
      initialText = def;
      localStorage.setItem(key, def);
    } else {
      try {
        const r = await fetch('/api/custom-prompt');
        const data = await r.json();
        initialText = data.custom_prompt || '';
      } catch (_) {}
    }
  }
  _applyPromptText(initialText);
  // Always sync to backend so active session picks it up
  fetch('/api/custom-prompt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ custom_prompt: initialText }),
  }).catch(() => {});
  // Also load the per-session summary system prompt state
  loadSessionSummarySystemPrompt();
}

/* ── Playback ────────────────────────────────────────────────────────────── */
const _playbackAudio = document.getElementById('playback-audio');
let _playbackActive = false;

// Playback output level (0..1), persisted to localStorage. Recordings are
// mastered hot (AGC normalises each source, then both are summed), so default
// to half volume rather than blasting at full scale.
const _VOLUME_KEY = 'ma-playback-volume';
let _preMuteVolume = 0.5;   // restored when un-muting from the speaker icon
function _getSavedVolume() {
  const raw = parseFloat(localStorage.getItem(_VOLUME_KEY));
  if (!isFinite(raw)) return 0.5;
  return Math.min(1, Math.max(0, raw));
}
function _updateVolumeIcon(v) {
  const icon = document.getElementById('playback-volume-icon');
  if (!icon) return;
  let glyph = 'fa-volume-high';
  if (v <= 0.001)   glyph = 'fa-volume-xmark';
  else if (v < 0.5) glyph = 'fa-volume-low';
  icon.className = `fa-solid ${glyph}`;
}

function fmtTime(s) {
  if (!isFinite(s)) return '0:00';
  return fmtDuration(s);
}

function initPlayback(sessionId) {
  // Cache-bust: after stop-resume-stop the WAV is appended on disk but the
  // URL is unchanged, so the browser would replay its cached copy with the
  // pre-resume length. Force a fresh fetch each call.
  _playbackAudio.src = `/api/sessions/${sessionId}/audio?t=${Date.now()}`;
  _playbackAudio.load();
  _playbackActive = true;
  document.getElementById('playback-bar').classList.remove('hidden');
  _syncPanelBottomRadius();

  // Restore saved playback speed
  const savedSpeed = _prefs.playback_speed || '1';
  const speedSel = document.getElementById('playback-speed');
  if (speedSel) speedSel.value = savedSpeed;
  _playbackAudio.playbackRate = parseFloat(savedSpeed);

  // Restore saved output level (defaults to 0.5 so playback isn't blasting)
  const savedVol = _getSavedVolume();
  if (savedVol > 0.001) _preMuteVolume = savedVol;
  _playbackAudio.volume = savedVol;
  const volSlider = document.getElementById('playback-volume-slider');
  if (volSlider) volSlider.value = savedVol;
  _updateVolumeIcon(savedVol);

  _playbackAudio.onloadedmetadata = () => {
    document.getElementById('playback-duration').textContent = fmtTime(_playbackAudio.duration);
    document.getElementById('playback-seek').max = _playbackAudio.duration || 100;
    renderChapterTicks();  // now that duration is known, place the chapter ticks
  };

  _playbackAudio.ontimeupdate = () => {
    const t = _playbackAudio.currentTime;
    // Skip filtered-out segments during playback. But never while a Speaker
    // Cleanup preview owns the audio - skipping would yank the playhead (and
    // the slaved popup video) off the segment the user is auditioning.
    if (!_playbackAudio.paused && _transcriptFilterActive()
        && !_playbackAudio.dataset.cleanupActive) {
      _skipFilteredAudio(t);
    }
    document.getElementById('playback-time').textContent = fmtTime(t);
    document.getElementById('playback-seek').value = _playbackAudio.currentTime;
    highlightPlayingSegment(_playbackAudio.currentTime);
    _updateMinimapPlayhead(t);
    if (_sessionEditor?.profile) renderSessionEditorCanvas();
  };

  _playbackAudio.onended = () => {
    document.getElementById('playback-play').innerHTML = '<i class="fa-solid fa-play"></i>';
    clearPlayingHighlight();
  };
}

function destroyPlayback() {
  _playbackAudio.pause();
  _playbackAudio.removeAttribute('src');
  _playbackActive = false;
  document.getElementById('playback-bar').classList.add('hidden');
  _syncPanelBottomRadius();
  document.getElementById('playback-play').innerHTML = '<i class="fa-solid fa-play"></i>';
  document.getElementById('playback-time').textContent = '0:00';
  document.getElementById('playback-duration').textContent = '0:00';
  document.getElementById('playback-seek').value = 0;
  clearPlayingHighlight();
  renderChapterTicks();  // _playbackActive is now false, so this clears the ticks
  destroyVideo();
}

function togglePlayback() {
  if (!_playbackActive) return;
  if (_playbackAudio.paused) {
    _playbackAudio.play();
    document.getElementById('playback-play').innerHTML = '<i class="fa-solid fa-pause"></i>';
  } else {
    _playbackAudio.pause();
    document.getElementById('playback-play').innerHTML = '<i class="fa-solid fa-play"></i>';
  }
}

function seekPlayback(val) {
  if (!_playbackActive) return;
  _playbackAudio.currentTime = parseFloat(val);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _sessionEditor) {
    e.preventDefault();
    closeSessionEditor();
    return;
  }
  if (e.code === 'Space' && _playbackActive
      && !e.target.closest('input, textarea, select, [contenteditable]')) {
    e.preventDefault();
    togglePlayback();
  }
});

function seekToTime(t) {
  if (!_playbackActive) return;
  _playbackAudio.currentTime = t;
  if (_playbackAudio.paused) {
    _playbackAudio.play();
    document.getElementById('playback-play').innerHTML = '<i class="fa-solid fa-pause"></i>';
  }
}

function setPlaybackSpeed(val) {
  _playbackAudio.playbackRate = parseFloat(val);
  savePref('playback_speed', val);
}

function setPlaybackVolume(val) {
  const v = Math.min(1, Math.max(0, parseFloat(val) || 0));
  _playbackAudio.volume = v;
  if (v > 0.001) _preMuteVolume = v;
  const slider = document.getElementById('playback-volume-slider');
  if (slider && parseFloat(slider.value) !== v) slider.value = v;
  _updateVolumeIcon(v);
  try { localStorage.setItem(_VOLUME_KEY, String(v)); } catch (_) {}
}

// Clicking the speaker icon toggles mute, restoring the prior level.
function togglePlaybackMute() {
  setPlaybackVolume(_playbackAudio.volume > 0.001 ? 0 : (_preMuteVolume || 0.5));
}

// Build a sorted list of visible time ranges from transcript segments
function _getVisibleTimeRanges() {
  if (_visibleRangesCache) return _visibleRangesCache;
  const ranges = [];
  for (const { start, end, el } of _segmentTimes) {
    if (el.style.display === 'none') {
      // Noise segments are hidden by default but their audio should still play.
      // Only skip segments hidden by an active speaker/search filter.
      const source = el.dataset.transcriptSource || '';
      const isNoise = source === _NOISE_LABEL || _manualNoiseKeys.has(source);
      if (!isNoise) continue;
    }
    ranges.push({ start, end });
  }
  // _segmentTimes is insertion-ordered (chronological), but sort defensively.
  ranges.sort((a, b) => a.start - b.start);
  _visibleRangesCache = ranges;
  return ranges;
}

let _lastSkipTime = -1;
function _skipFilteredAudio(t) {
  // Avoid repeated skipping at the same position
  if (Math.abs(t - _lastSkipTime) < 0.3) return;

  const ranges = _getVisibleTimeRanges();
  if (ranges.length === 0) return;

  // Check if current time is inside any visible range
  for (const r of ranges) {
    if (t >= r.start && t < r.end) return; // playing a visible segment, all good
  }

  // Current time is in a hidden gap - find the next visible range
  for (const r of ranges) {
    if (r.start > t) {
      _lastSkipTime = r.start;
      _playbackAudio.currentTime = r.start;
      // Nudge the video toward the skip target with a THROTTLED seek. Dense
      // filtering can jump the audio across many small gaps in quick
      // succession; a throttled seek (plus the sync loop's rate correction)
      // keeps the video close without machine-gunning the decoder, which is
      // what produced the old "video loops a short snippet" symptom.
      if (_videoAvailable && _videoVisible) {
        _hardSeek(_audioToVideoTime(r.start), false);
      }
      return;
    }
  }

  // Past all visible segments - let playback end naturally
}

let _currentPlayingSeg = null;
let _programmaticScrollCount = 0; // incremented before programmatic scrolls, decremented on scroll event

function _doProgrammaticScroll(el, opts) {
  _programmaticScrollCount++;
  const container = el.closest('.col-body');
  if (!container) {
    el.scrollIntoView({ ...opts, behavior: 'instant' });
    setTimeout(() => { _programmaticScrollCount = Math.max(0, _programmaticScrollCount - 1); }, 100);
    return;
  }

  // Calculate target scroll position
  const elRect = el.getBoundingClientRect();
  const cRect = container.getBoundingClientRect();
  const elCenter = elRect.top + elRect.height / 2 - cRect.top + container.scrollTop;
  const target = elCenter - container.clientHeight / 2;
  const start = container.scrollTop;
  const delta = Math.max(0, Math.min(target, container.scrollHeight - container.clientHeight)) - start;

  if (Math.abs(delta) < 2) {
    _programmaticScrollCount = Math.max(0, _programmaticScrollCount - 1);
    return;
  }

  // Fast ease-out animation (~150ms)
  const duration = 150;
  const t0 = performance.now();
  function step(now) {
    const p = Math.min((now - t0) / duration, 1);
    const ease = 1 - (1 - p) * (1 - p); // quadratic ease-out
    container.scrollTop = start + delta * ease;
    if (p < 1) {
      requestAnimationFrame(step);
    } else {
      setTimeout(() => { _programmaticScrollCount = Math.max(0, _programmaticScrollCount - 1); }, 50);
    }
  }
  requestAnimationFrame(step);
}

function highlightPlayingSegment(t) {
  // Binary search on _segmentTimes (sorted by start) - O(log n) vs O(n) querySelectorAll.
  let lo = 0, hi = _segmentTimes.length - 1, idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (_segmentTimes[mid].start <= t) { idx = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  const found = (idx >= 0 && _segmentTimes[idx].end > t) ? _segmentTimes[idx].el : null;
  if (found === _currentPlayingSeg) return;
  // Remove playing from previous segment and its group
  if (_currentPlayingSeg) {
    _currentPlayingSeg.classList.remove('playing');
    if (_currentPlayingSeg._groupSummary) {
      _currentPlayingSeg._groupSummary.classList.remove('playing');
    }
  }
  _currentPlayingSeg = found;
  if (found) {
    found.classList.add('playing');
    // Propagate playing state to the parent group summary
    if (found._groupSummary) {
      found._groupSummary.classList.add('playing');
    }
    if (_autoScroll) {
      // If segment is hidden inside a collapsed group, scroll to the group summary instead
      const scrollTarget = (found.style.display === 'none' && found._groupSummary)
        ? found._groupSummary : found;
      _doProgrammaticScroll(scrollTarget, { behavior: 'smooth', block: 'center' });
    }
  }
}

function clearPlayingHighlight() {
  if (_currentPlayingSeg) {
    _currentPlayingSeg.classList.remove('playing');
    if (_currentPlayingSeg._groupSummary) {
      _currentPlayingSeg._groupSummary.classList.remove('playing');
    }
    _currentPlayingSeg = null;
  }
}

/* ── Session trim/split editor ───────────────────────────────────────────── */
let _sessionEditor = null;
let _sessionEditorDrag = null;
let _sessionEditorSuppressClick = false;
let _sessionHasTrimBackup  = false;
let _sessionHasSplitBackup = false;
let _sessionSplitGroupId   = null;

async function openSessionEditor() {
  if (!state.sessionId || state.isRecording || !_playbackActive) return;
  const overlay = document.getElementById('session-editor-overlay');
  overlay.classList.remove('hidden');
  // Pull the current title from the active sidebar row so split parts can
  // default Part 1 to the original title and Part 2+ to "<title> Part N".
  const _activeRow = document.querySelector(`.session-item[data-id="${state.sessionId}"] .session-name`);
  const _sourceTitle = (_activeRow?.textContent || '').trim() || 'Meeting';
  _sessionEditor = {
    sessionId: state.sessionId,
    sourceTitle: _sourceTitle,
    mode: 'trim',
    profile: null,
    start: 0,
    end: 0,
    splitPoints: [],
    titles: [],
    viewStart: 0,
    viewEnd: 0,
    speakerFilter: new Set(),
    speakerFilterCollapsed: false,
    hasTrimBackup:  _sessionHasTrimBackup,
    hasSplitBackup: _sessionHasSplitBackup,
    splitGroupId:   _sessionSplitGroupId,
  };
  document.getElementById('session-editor-subtitle').textContent = 'Loading audio profile...';
  document.getElementById('session-editor-hint').textContent = 'Loading audio profile...';
  setSessionEditorMode('trim');
  try {
    const profile = await fetch(`/api/sessions/${state.sessionId}/audio-profile?bins=1400`).then(r => r.json());
    if (profile.error) throw new Error(profile.error);
    _sessionEditor.profile = profile;
    _sessionEditor.start = 0;
    _sessionEditor.end = profile.duration || 0;
    _sessionEditor.viewStart = 0;
    _sessionEditor.viewEnd = profile.duration || 0;
    const tail = sessionEditorQuietSuggestion(profile);
    if (tail) _sessionEditor.end = Math.max(0, tail.start);
    document.getElementById('session-editor-subtitle').textContent = `${fmtTime(profile.duration)} total`;
    document.getElementById('session-editor-hint').textContent = 'Space bar play/pause. Wheel to zoom. Drag empty timeline to pan. Drag handles or split markers to adjust.';
    sessionEditorSyncInputs();
    sessionEditorUpdateSuggestionButton();
    sessionEditorUpdateRestoreButton();
    sessionEditorRenderSpeakerPills();
    renderSessionEditor();
  } catch (e) {
    document.getElementById('session-editor-hint').textContent = e.message || 'Could not load audio profile';
  }
}

function closeSessionEditor() {
  document.getElementById('session-editor-overlay')?.classList.add('hidden');
  _sessionEditor = null;
  _sessionEditorDrag = null;
}

function _setPlaybackEditTrimmed(isTrimmed) {
  _sessionHasTrimBackup = !!isTrimmed;
  _updatePlaybackEditIndicator();
}
function _setSessionSplitBackup(hasBackup, groupId) {
  _sessionHasSplitBackup = !!hasBackup;
  _sessionSplitGroupId   = groupId || null;
  _updatePlaybackEditIndicator();
}
function _updatePlaybackEditIndicator() {
  const btn = document.getElementById('playback-edit-btn');
  if (!btn) return;
  btn.classList.toggle('trimmed',       _sessionHasTrimBackup || _sessionHasSplitBackup);
  btn.classList.toggle('has-split-undo', _sessionHasSplitBackup);
}

async function reloadSession(sessionId) {
  if (state.sessionId === sessionId) state.sessionId = null;
  return loadSession(sessionId);
}

function setSessionEditorMode(mode) {
  if (!_sessionEditor) return;
  _sessionEditor.mode = mode;
  document.getElementById('session-editor-mode-trim')?.classList.toggle('active', mode === 'trim');
  document.getElementById('session-editor-mode-split')?.classList.toggle('active', mode === 'split');
  document.getElementById('session-editor-apply').textContent = mode === 'trim' ? 'Apply Trim' : 'Create Splits';
  document.querySelector('.session-editor-fields')?.classList.toggle('split-mode', mode === 'split');
  sessionEditorUpdateRestoreButton();
  renderSessionEditor();
}

function sessionEditorSyncInputs() {
  if (!_sessionEditor) return;
  document.getElementById('session-editor-start').value = _sessionEditor.start.toFixed(1);
  document.getElementById('session-editor-end').value = _sessionEditor.end.toFixed(1);
}

function sessionEditorUpdateTrimInputs() {
  if (!_sessionEditor?.profile) return;
  const dur = _sessionEditor.profile.duration || 0;
  const start = parseFloat(document.getElementById('session-editor-start').value || '0');
  const end = parseFloat(document.getElementById('session-editor-end').value || String(dur));
  _sessionEditor.start = Math.max(0, Math.min(start, dur - 0.1));
  _sessionEditor.end = Math.max(_sessionEditor.start + 0.1, Math.min(end, dur));
  sessionEditorSyncInputs();
  renderSessionEditor();
}

function _sessionEditorClampView() {
  const ed = _sessionEditor;
  if (!ed?.profile) return;
  const dur = ed.profile.duration || 0;
  const minSpan = Math.min(dur || 1, 5);
  let span = Math.max(minSpan, (ed.viewEnd || dur) - (ed.viewStart || 0));
  span = Math.min(span, dur || span);
  let start = Math.max(0, Math.min(ed.viewStart || 0, Math.max(0, dur - span)));
  ed.viewStart = start;
  ed.viewEnd = Math.min(dur, start + span);
}

function sessionEditorFit() {
  if (!_sessionEditor?.profile) return;
  _sessionEditor.viewStart = 0;
  _sessionEditor.viewEnd = _sessionEditor.profile.duration || 0;
  renderSessionEditor();
}

function sessionEditorZoom(factor, centerTime = null) {
  const ed = _sessionEditor;
  if (!ed?.profile) return;
  const dur = ed.profile.duration || 0;
  const oldStart = ed.viewStart || 0;
  const oldEnd = ed.viewEnd || dur;
  const oldSpan = Math.max(0.1, oldEnd - oldStart);
  const newSpan = Math.max(Math.min(dur, 5), Math.min(dur, oldSpan * factor));
  const center = centerTime ?? ((oldStart + oldEnd) / 2);
  const pct = oldSpan > 0 ? (center - oldStart) / oldSpan : 0.5;
  ed.viewStart = center - newSpan * pct;
  ed.viewEnd = ed.viewStart + newSpan;
  _sessionEditorClampView();
  renderSessionEditor();
}

function sessionEditorZoomIn() {
  sessionEditorZoom(0.65, _playbackAudio.currentTime || null);
}

function sessionEditorZoomOut() {
  sessionEditorZoom(1.5, _playbackAudio.currentTime || null);
}

function sessionEditorPan(deltaSec) {
  if (!_sessionEditor?.profile) return;
  _sessionEditor.viewStart += deltaSec;
  _sessionEditor.viewEnd += deltaSec;
  _sessionEditorClampView();
  renderSessionEditor();
}

function sessionEditorQuietSuggestion(profile = _sessionEditor?.profile) {
  if (!profile) return null;
  const dur = profile.duration || 0;
  const spans = (profile.quiet_spans || [])
    .map(s => ({ ...s, len: (s.end || 0) - (s.start || 0) }))
    .filter(s => s.len >= 3);
  if (!spans.length) return null;

  // Prefer true trailing silence, but accept the last substantial quiet span
  // near the end because some recordings have a small click/noise after silence.
  const nearEnd = Math.max(5, dur * 0.03);
  const trailing = spans
    .filter(s => s.end >= dur - nearEnd)
    .sort((a, b) => b.len - a.len)[0];
  if (trailing) return trailing;

  return spans
    .filter(s => s.start >= dur * 0.55)
    .sort((a, b) => b.start - a.start || b.len - a.len)[0] || null;
}

function sessionEditorUpdateSuggestionButton() {
  const btn = document.getElementById('session-editor-suggestion-btn');
  if (!btn) return;
  const suggestion = sessionEditorQuietSuggestion();
  btn.disabled = !suggestion;
  btn.classList.toggle('disabled', !suggestion);
  btn.title = suggestion
    ? `Use quiet span from ${fmtTime(suggestion.start)} to ${fmtTime(suggestion.end)}`
    : 'No quiet span detected near the end of this session';
}

function sessionEditorUseSuggestion() {
  if (!_sessionEditor?.profile) return;
  const suggestion = sessionEditorQuietSuggestion();
  const hint = document.getElementById('session-editor-hint');
  if (!suggestion) {
    if (hint) hint.textContent = 'No long quiet span was detected near the end of this session.';
    return;
  }
  if (_sessionEditor.mode === 'trim') {
    _sessionEditor.end = Math.max(_sessionEditor.start + 0.1, suggestion.start);
    sessionEditorSyncInputs();
    if (hint) hint.textContent = `Trim end moved to ${fmtTime(suggestion.start)}.`;
  } else {
    sessionEditorAddSplit(suggestion.start);
    if (hint) hint.textContent = `Split point added at ${fmtTime(suggestion.start)}.`;
  }
  const span = Math.max(10, (_sessionEditor.viewEnd - _sessionEditor.viewStart) || 30);
  _sessionEditor.viewStart = Math.max(0, suggestion.start - span * 0.25);
  _sessionEditor.viewEnd = Math.min(_sessionEditor.profile.duration || 0, _sessionEditor.viewStart + span);
  _sessionEditorClampView();
  renderSessionEditor();
}

function sessionEditorAddSplitAtPlayhead() {
  if (!_sessionEditor?.profile) return;
  const t = Math.max(0, Math.min(_playbackAudio.currentTime || 0, _sessionEditor.profile.duration || 0));
  sessionEditorAddSplit(t);
}

function sessionEditorAddSplit(t) {
  if (!_sessionEditor?.profile) return;
  const dur = _sessionEditor.profile.duration || 0;
  if (t <= 1 || t >= dur - 1) return;
  if (_sessionEditor.splitPoints.some(p => Math.abs(p - t) < 1)) return;
  _sessionEditor.splitPoints.push(t);
  _sessionEditor.splitPoints.sort((a, b) => a - b);
  renderSessionEditor();
}

function sessionEditorRanges() {
  if (!_sessionEditor?.profile) return [];
  const dur = _sessionEditor.profile.duration || 0;
  if (_sessionEditor.mode === 'trim') {
    return [{ start: _sessionEditor.start, end: _sessionEditor.end, title: '' }];
  }
  const pts = [0, ..._sessionEditor.splitPoints, dur];
  const src = _sessionEditor.sourceTitle || 'Meeting';
  const ranges = [];
  for (let i = 0; i < pts.length - 1; i++) {
    if (pts[i + 1] - pts[i] > 1) {
      // Part 1 inherits the source title; Part 2+ get "<title> Part N".
      const fallback = i === 0 ? src : `${src} Part ${i + 1}`;
      ranges.push({ start: pts[i], end: pts[i + 1], title: _sessionEditor.titles[i] || fallback });
    }
  }
  return ranges;
}

function sessionEditorSpeakerGroups() {
  const ed = _sessionEditor;
  if (!ed?.profile) return [];
  const groups = new Map();
  for (const segment of ed.profile.segments || []) {
    const speakerKey = segment.speaker || segment.label || 'Unknown';
    const label = (segment.label || speakerKey || 'Unknown').trim() || 'Unknown';
    const groupKey = label.toLowerCase();
    const current = groups.get(groupKey) || {
      key: groupKey,
      keys: [],
      label,
      color: segment.color || _sessionEditorSpeakerColor(label),
      count: 0,
      duration: 0,
    };
    if (!current.keys.includes(speakerKey)) current.keys.push(speakerKey);
    current.count += 1;
    current.duration += Math.max(0, (segment.end || 0) - (segment.start || 0));
    if (segment.color) current.color = segment.color;
    groups.set(groupKey, current);
  }
  return [...groups.values()].sort((a, b) => b.duration - a.duration || a.label.localeCompare(b.label));
}

function _sessionEditorSpeakerColor(key) {
  const palette = typeof _SPEAKER_PALETTE !== 'undefined' && _SPEAKER_PALETTE.length
    ? _SPEAKER_PALETTE
    : ['#58a6ff', '#7ee787', '#f2cc60', '#ff7b72', '#bc8cff', '#39c5cf'];
  let hash = 0;
  for (let i = 0; i < String(key).length; i++) hash = ((hash << 5) - hash) + String(key).charCodeAt(i);
  return palette[Math.abs(hash) % palette.length];
}

function _sessionEditorSpeakerVisible(key) {
  const filter = _sessionEditor?.speakerFilter;
  return !filter || filter.size === 0 || filter.has(key);
}

function _sessionEditorGroupVisible(group) {
  const filter = _sessionEditor?.speakerFilter;
  return !filter || filter.size === 0 || group.keys.some(key => filter.has(key));
}

function sessionEditorRenderSpeakerPills() {
  const row = document.getElementById('session-editor-speakers');
  const wrap = document.getElementById('session-editor-speaker-pills');
  if (!row || !wrap) return;
  const groups = sessionEditorSpeakerGroups();
  row.classList.toggle('hidden', groups.length === 0);
  row.classList.toggle('collapsed', _sessionEditor?.speakerFilterCollapsed === true);
  const filter = _sessionEditor?.speakerFilter || new Set();
  const visibleGroups = groups.filter(group => _sessionEditorGroupVisible(group)).length;
  const summary = document.getElementById('session-editor-speaker-summary');
  if (summary) {
    summary.textContent = filter.has('__none__')
      ? `Speakers (${groups.length}) hidden`
      : filter.size > 0
        ? `Speakers (${visibleGroups}/${groups.length})`
        : `Speakers (${groups.length})`;
  }
  const chevron = document.getElementById('session-editor-speaker-chevron');
  if (chevron) {
    chevron.classList.toggle('fa-chevron-down', _sessionEditor?.speakerFilterCollapsed !== true);
    chevron.classList.toggle('fa-chevron-right', _sessionEditor?.speakerFilterCollapsed === true);
  }
  document.getElementById('session-editor-speakers-all')?.classList.toggle('active', filter.size === 0);
  document.getElementById('session-editor-speakers-none')?.classList.toggle('active', filter.has('__none__'));
  wrap.innerHTML = '';
  groups.forEach(group => {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'session-editor-speaker-pill';
    pill.classList.toggle('off', !_sessionEditorGroupVisible(group));
    pill.style.setProperty('--speaker-color', group.color);
    const sourceCount = group.keys.length;
    pill.title = `${group.label}: ${group.count} segment${group.count === 1 ? '' : 's'}, ${fmtTime(group.duration)}${sourceCount > 1 ? `, ${sourceCount} speaker sources` : ''}`;

    const label = document.createElement('span');
    label.className = 'session-editor-speaker-label';
    label.textContent = group.label;
    pill.appendChild(label);

    const count = document.createElement('span');
    count.className = 'session-editor-speaker-count';
    count.textContent = String(group.count);
    pill.appendChild(count);

    pill.addEventListener('click', () => sessionEditorToggleSpeaker(group.keys));
    wrap.appendChild(pill);
  });
}

function sessionEditorToggleSpeaker(keys) {
  const groups = sessionEditorSpeakerGroups();
  const speakerKeys = Array.isArray(keys) ? keys : [keys];
  const allKeys = new Set(groups.flatMap(g => g.keys));
  if (!_sessionEditor.speakerFilter || _sessionEditor.speakerFilter.size === 0) {
    _sessionEditor.speakerFilter = new Set(speakerKeys);
  } else if (speakerKeys.some(key => _sessionEditor.speakerFilter.has(key))) {
    speakerKeys.forEach(key => _sessionEditor.speakerFilter.delete(key));
    if (_sessionEditor.speakerFilter.size === 0) {
      _sessionEditor.speakerFilter = new Set(['__none__']);
    }
  } else {
    speakerKeys.forEach(key => _sessionEditor.speakerFilter.add(key));
    _sessionEditor.speakerFilter.delete('__none__');
    if (_sessionEditor.speakerFilter.size >= allKeys.size) _sessionEditor.speakerFilter.clear();
  }
  sessionEditorRenderSpeakerPills();
  renderSessionEditorCanvas();
}

function sessionEditorToggleAllSpeakers(show) {
  if (!_sessionEditor) return;
  _sessionEditor.speakerFilter = show ? new Set() : new Set(['__none__']);
  sessionEditorRenderSpeakerPills();
  renderSessionEditorCanvas();
}

function sessionEditorToggleSpeakerPanel() {
  if (!_sessionEditor) return;
  _sessionEditor.speakerFilterCollapsed = !_sessionEditor.speakerFilterCollapsed;
  sessionEditorRenderSpeakerPills();
}

function sessionEditorUpdateRestoreButton() {
  const btn = document.getElementById('session-editor-restore');
  if (!btn) return;
  // Trim backup only matters in trim mode. Split backup is a session-level
  // property - offer it in either mode so users can always find the undo.
  const ed = _sessionEditor;
  if (!ed) { btn.classList.add('hidden'); btn.disabled = true; return; }
  const hasTrim  = ed.mode === 'trim' && ed.hasTrimBackup;
  const hasSplit = !!ed.hasSplitBackup;
  btn.classList.toggle('hidden', !(hasTrim || hasSplit));
  btn.disabled = !(hasTrim || hasSplit);
  // Label reflects which restore will be offered. Split wins if both are
  // somehow true (shouldn't normally happen - the original session was
  // deleted during the split, taking its trim backup with it).
  if (hasSplit) btn.textContent = 'Undo Split…';
  else if (hasTrim) btn.textContent = 'Restore Original';
}

function renderSessionEditor() {
  renderSessionEditorCanvas();
  renderSessionEditorRanges();
}

function renderSessionEditorRanges() {
  const wrap = document.getElementById('session-editor-ranges');
  if (!wrap || !_sessionEditor?.profile) return;
  wrap.innerHTML = '';
  const ranges = sessionEditorRanges();
  ranges.forEach((range, i) => {
    const row = document.createElement('div');
    row.className = 'session-editor-range';
    const meta = document.createElement('span');
    meta.className = 'session-editor-range-time';
    meta.textContent = `${fmtTime(range.start)} - ${fmtTime(range.end)}`;
    row.appendChild(meta);
    if (_sessionEditor.mode === 'split') {
      const input = document.createElement('input');
      input.value = range.title;
      const src = _sessionEditor.sourceTitle || 'Meeting';
      input.placeholder = i === 0 ? src : `${src} Part ${i + 1}`;
      input.oninput = () => { _sessionEditor.titles[i] = input.value; };
      row.appendChild(input);
      if (i > 0) {
        const btn = document.createElement('button');
        btn.className = 'session-editor-range-remove';
        btn.innerHTML = '<i class="fa-solid fa-xmark"></i>';
        btn.onclick = () => {
          _sessionEditor.splitPoints.splice(i - 1, 1);
          renderSessionEditor();
        };
        row.appendChild(btn);
      }
    }
    wrap.appendChild(row);
  });
}

function renderSessionEditorCanvas() {
  const canvas = document.getElementById('session-editor-canvas');
  const ed = _sessionEditor;
  if (!canvas || !ed?.profile) return;
  _sessionEditorClampView();
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(300, Math.floor(rect.width));
  const h = Math.max(160, Math.floor(rect.height));
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const dur = ed.profile.duration || 1;
  const viewStart = ed.viewStart || 0;
  const viewEnd = ed.viewEnd || dur;
  const viewSpan = Math.max(0.1, viewEnd - viewStart);
  const xFor = t => ((t - viewStart) / viewSpan) * w;
  const inView = (s, e) => e >= viewStart && s <= viewEnd;
  const grd = ctx.createLinearGradient(0, 0, 0, h);
  grd.addColorStop(0, '#161b22');
  grd.addColorStop(1, '#0d1117');
  ctx.fillStyle = grd;
  ctx.fillRect(0, 0, w, h);

  const timeStep = _sessionEditorTickStep(viewSpan);
  ctx.font = '11px sans-serif';
  ctx.textBaseline = 'top';
  for (let t = Math.ceil(viewStart / timeStep) * timeStep; t <= viewEnd; t += timeStep) {
    const x = xFor(t);
    ctx.strokeStyle = t % (timeStep * 2) === 0 ? 'rgba(139,148,158,0.22)' : 'rgba(139,148,158,0.12)';
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h - 28);
    ctx.stroke();
    ctx.fillStyle = 'rgba(201,209,217,0.62)';
    ctx.fillText(fmtTime(t), x + 4, 8);
  }

  ctx.strokeStyle = 'rgba(139,148,158,0.28)';
  ctx.beginPath();
  ctx.moveTo(0, h / 2);
  ctx.lineTo(w, h / 2);
  ctx.stroke();

  ctx.fillStyle = 'rgba(110, 118, 129, 0.22)';
  for (const s of ed.profile.quiet_spans || []) {
    if (!inView(s.start, s.end)) continue;
    ctx.fillRect(Math.max(0, xFor(s.start)), 0, Math.min(w, xFor(s.end)) - Math.max(0, xFor(s.start)), h - 28);
  }

  for (const b of ed.profile.bins || []) {
    if (!inView(b.t0, b.t1)) continue;
    const x0 = xFor(b.t0);
    const x1 = Math.max(x0 + 1, xFor(b.t1));
    const peak = Math.max(1, Math.min(h * 0.34, (b.peak || 0) * h * 1.7));
    const rms = Math.max(1, Math.min(h * 0.24, (b.rms || 0) * h * 2.6));
    ctx.fillStyle = 'rgba(88, 166, 255, 0.28)';
    ctx.fillRect(x0, h / 2 - peak, x1 - x0, peak * 2);
    ctx.fillStyle = 'rgba(126, 231, 135, 0.58)';
    ctx.fillRect(x0, h / 2 - rms, x1 - x0, rms * 2);
  }

  ctx.fillStyle = 'rgba(13,17,23,0.56)';
  ctx.fillRect(0, h - 35, w, 15);
  ctx.strokeStyle = 'rgba(139,148,158,0.22)';
  ctx.beginPath();
  ctx.moveTo(0, h - 35);
  ctx.lineTo(w, h - 35);
  ctx.stroke();

  for (const s of ed.profile.segments || []) {
    const speakerKey = s.speaker || s.label || 'Unknown';
    if (!_sessionEditorSpeakerVisible(speakerKey) || !inView(s.start, s.end)) continue;
    ctx.fillStyle = s.color || _sessionEditorSpeakerColor(speakerKey);
    ctx.fillRect(xFor(s.start), h - 31, Math.max(2, xFor(s.end) - xFor(s.start)), 9);
  }

  const ranges = sessionEditorRanges();
  ctx.lineWidth = 2;
  ranges.forEach(r => {
    if (!inView(r.start, r.end)) return;
    const x0 = Math.max(0, xFor(r.start));
    const x1 = Math.min(w, xFor(r.end));
    ctx.fillStyle = 'rgba(126, 231, 135, 0.08)';
    ctx.fillRect(x0, 28, Math.max(2, x1 - x0), h - 62);
    ctx.strokeStyle = 'rgba(126, 231, 135, 0.85)';
    ctx.strokeRect(x0, 28, Math.max(2, x1 - x0), h - 62);
  });

  if (ed.mode === 'trim') {
    _sessionEditorDrawHandle(ctx, xFor(ed.start), h, '#7ee787', 'Start');
    _sessionEditorDrawHandle(ctx, xFor(ed.end), h, '#7ee787', 'End');
  } else {
    ctx.fillStyle = '#f2cc60';
    ed.splitPoints.forEach((p, i) => {
      if (p < viewStart || p > viewEnd) return;
      const x = xFor(p);
      ctx.fillRect(x - 2, 0, 4, h - 28);
      ctx.fillStyle = '#f2cc60';
      ctx.beginPath();
      ctx.arc(x, 28, 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = 'rgba(242,204,96,0.95)';
      ctx.fillText(String(i + 1), x + 7, 20);
    });
  }
  const playX = xFor(_playbackAudio.currentTime || 0);
  if (playX >= 0 && playX <= w) {
    ctx.fillStyle = '#f85149';
    ctx.fillRect(playX - 1, 0, 2, h);
  }

  ctx.fillStyle = 'rgba(201,209,217,0.72)';
  ctx.fillText(`${fmtTime(viewStart)} - ${fmtTime(viewEnd)}`, 10, h - 20);
  _sessionEditorRenderOverview();
}

function _sessionEditorTickStep(span) {
  const targetTicks = 8;
  const raw = span / targetTicks;
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];
  return steps.find(s => s >= raw) || 3600;
}

function _sessionEditorDrawHandle(ctx, x, h, color, label) {
  if (x < -20 || x > ctx.canvas.width + 20) return;
  ctx.fillStyle = color;
  ctx.fillRect(x - 2, 0, 4, h - 28);
  ctx.beginPath();
  ctx.moveTo(x, 18);
  ctx.lineTo(x - 8, 6);
  ctx.lineTo(x + 8, 6);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = 'rgba(201,209,217,0.88)';
  ctx.font = '11px sans-serif';
  ctx.fillText(label, x + 8, 8);
}

function _sessionEditorRenderOverview() {
  const ed = _sessionEditor;
  const win = document.getElementById('session-editor-overview-window');
  if (!ed?.profile || !win) return;
  const dur = ed.profile.duration || 1;
  const left = Math.max(0, Math.min(100, (ed.viewStart / dur) * 100));
  const width = Math.max(2, Math.min(100 - left, ((ed.viewEnd - ed.viewStart) / dur) * 100));
  win.style.left = left + '%';
  win.style.width = width + '%';
}

function _sessionEditorMoveViewTo(start) {
  const ed = _sessionEditor;
  if (!ed?.profile) return;
  const span = Math.max(0.1, (ed.viewEnd || 0) - (ed.viewStart || 0));
  ed.viewStart = start;
  ed.viewEnd = start + span;
  _sessionEditorClampView();
  renderSessionEditor();
}

function _sessionEditorOverviewTimeFromEvent(e) {
  const overview = document.getElementById('session-editor-overview');
  const rect = overview?.getBoundingClientRect();
  const dur = _sessionEditor?.profile?.duration || 0;
  if (!rect?.width || !dur) return 0;
  const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  return pct * dur;
}

function _sessionEditorTimeFromEvent(e) {
  const canvas = document.getElementById('session-editor-canvas');
  const rect = canvas.getBoundingClientRect();
  const ed = _sessionEditor;
  const dur = ed?.profile?.duration || 0;
  const viewStart = ed?.viewStart || 0;
  const viewEnd = ed?.viewEnd || dur;
  return Math.max(0, Math.min(dur, viewStart + ((e.clientX - rect.left) / rect.width) * (viewEnd - viewStart)));
}

function _sessionEditorXForTime(t) {
  const canvas = document.getElementById('session-editor-canvas');
  const rect = canvas.getBoundingClientRect();
  const ed = _sessionEditor;
  if (!rect.width || !ed?.profile) return 0;
  const viewStart = ed.viewStart || 0;
  const viewEnd = ed.viewEnd || ed.profile.duration || 1;
  return ((t - viewStart) / Math.max(0.1, viewEnd - viewStart)) * rect.width;
}

function _sessionEditorNearestSplit(clientX) {
  const canvas = document.getElementById('session-editor-canvas');
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  let bestIdx = -1;
  let bestDist = Infinity;
  (_sessionEditor?.splitPoints || []).forEach((p, i) => {
    const d = Math.abs(_sessionEditorXForTime(p) - x);
    if (d < bestDist) {
      bestDist = d;
      bestIdx = i;
    }
  });
  return bestDist <= 12 ? bestIdx : -1;
}

{
  const canvas = document.getElementById('session-editor-canvas');
  const overview = document.getElementById('session-editor-overview');
  if (canvas) {
    canvas.addEventListener('mousedown', e => {
      if (!_sessionEditor?.profile) return;
      const t = _sessionEditorTimeFromEvent(e);
      const x = e.clientX - canvas.getBoundingClientRect().left;
      if (_sessionEditor.mode === 'trim') {
        const startDist = Math.abs(_sessionEditorXForTime(_sessionEditor.start) - x);
        const endDist = Math.abs(_sessionEditorXForTime(_sessionEditor.end) - x);
        if (Math.min(startDist, endDist) <= 14) {
          _sessionEditorDrag = { type: startDist < endDist ? 'start' : 'end' };
        } else {
          _sessionEditorDrag = { type: 'pan', x: e.clientX, viewStart: _sessionEditor.viewStart, viewEnd: _sessionEditor.viewEnd, moved: false };
        }
      } else {
        const idx = _sessionEditorNearestSplit(e.clientX);
        if (idx >= 0) {
          _sessionEditorDrag = { type: 'split', index: idx };
        } else {
          _sessionEditorDrag = { type: 'pan', x: e.clientX, viewStart: _sessionEditor.viewStart, viewEnd: _sessionEditor.viewEnd, moved: false };
        }
      }
      canvas.classList.add('dragging');
    });
    canvas.addEventListener('dblclick', e => {
      if (_sessionEditor?.mode === 'split') sessionEditorAddSplit(_sessionEditorTimeFromEvent(e));
    });
    canvas.addEventListener('click', e => {
      if (!_sessionEditor?.profile || _sessionEditorSuppressClick) return;
      _playbackAudio.currentTime = _sessionEditorTimeFromEvent(e);
      renderSessionEditorCanvas();
    });
    canvas.addEventListener('wheel', e => {
      if (!_sessionEditor?.profile) return;
      e.preventDefault();
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        const rect = canvas.getBoundingClientRect();
        const span = (_sessionEditor.viewEnd || 0) - (_sessionEditor.viewStart || 0);
        sessionEditorPan((e.deltaX / Math.max(1, rect.width)) * span);
      } else {
        const center = _sessionEditorTimeFromEvent(e);
        sessionEditorZoom(e.deltaY < 0 ? 0.82 : 1.22, center);
      }
    }, { passive: false });
    window.addEventListener('mousemove', e => {
      if (!_sessionEditorDrag || !_sessionEditor?.profile) return;
      if (_sessionEditorDrag.type === 'overview') {
        const rect = overview?.getBoundingClientRect();
        const dur = _sessionEditor.profile.duration || 0;
        if (!rect?.width || !dur) return;
        const dx = e.clientX - _sessionEditorDrag.x;
        if (Math.abs(dx) > 2) _sessionEditorDrag.moved = true;
        const delta = (dx / rect.width) * dur;
        _sessionEditor.viewStart = _sessionEditorDrag.viewStart + delta;
        _sessionEditor.viewEnd = _sessionEditorDrag.viewEnd + delta;
        _sessionEditorClampView();
        renderSessionEditor();
        return;
      }
      const t = _sessionEditorTimeFromEvent(e);
      if (_sessionEditorDrag.type === 'start') _sessionEditor.start = Math.min(t, _sessionEditor.end - 0.1);
      if (_sessionEditorDrag.type === 'end') _sessionEditor.end = Math.max(t, _sessionEditor.start + 0.1);
      if (_sessionEditorDrag.type === 'split') {
        const idx = _sessionEditorDrag.index;
        const prev = idx > 0 ? _sessionEditor.splitPoints[idx - 1] + 1 : 1;
        const next = idx < _sessionEditor.splitPoints.length - 1
          ? _sessionEditor.splitPoints[idx + 1] - 1
          : (_sessionEditor.profile.duration || 0) - 1;
        _sessionEditor.splitPoints[idx] = Math.max(prev, Math.min(next, t));
        _sessionEditor.splitPoints.sort((a, b) => a - b);
      }
      if (_sessionEditorDrag.type === 'pan') {
        const rect = canvas.getBoundingClientRect();
        const span = _sessionEditorDrag.viewEnd - _sessionEditorDrag.viewStart;
        const dx = e.clientX - _sessionEditorDrag.x;
        if (Math.abs(dx) > 2) _sessionEditorDrag.moved = true;
        _sessionEditor.viewStart = _sessionEditorDrag.viewStart - (dx / rect.width) * span;
        _sessionEditor.viewEnd = _sessionEditor.viewStart + span;
        _sessionEditorClampView();
      }
      sessionEditorSyncInputs();
      renderSessionEditor();
    });
    window.addEventListener('mouseup', () => {
      canvas.classList.remove('dragging');
      overview?.classList.remove('dragging');
      if (_sessionEditorDrag?.moved) {
        _sessionEditorSuppressClick = true;
        setTimeout(() => { _sessionEditorSuppressClick = false; }, 0);
      }
      _sessionEditorDrag = null;
    });
  }
  if (overview) {
    overview.addEventListener('mousedown', e => {
      if (!_sessionEditor?.profile) return;
      e.preventDefault();
      const clickedTime = _sessionEditorOverviewTimeFromEvent(e);
      const span = Math.max(0.1, (_sessionEditor.viewEnd || 0) - (_sessionEditor.viewStart || 0));
      const insideWindow = clickedTime >= _sessionEditor.viewStart && clickedTime <= _sessionEditor.viewEnd;
      if (!insideWindow) {
        _sessionEditorMoveViewTo(clickedTime - span / 2);
      }
      _sessionEditorDrag = {
        type: 'overview',
        x: e.clientX,
        viewStart: _sessionEditor.viewStart,
        viewEnd: _sessionEditor.viewEnd,
        moved: false,
      };
      overview.classList.add('dragging');
    });
  }
}

async function applySessionEditor() {
  if (!_sessionEditor?.profile) return;
  const ed = _sessionEditor;
  const btn = document.getElementById('session-editor-apply');
  btn.disabled = true;
  btn.textContent = ed.mode === 'trim' ? 'Trimming...' : 'Splitting...';
  try {
    let data;
    if (ed.mode === 'trim') {
      data = await fetch(`/api/sessions/${ed.sessionId}/trim`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: ed.start, end: ed.end }),
      }).then(r => r.json());
      if (data.error) throw new Error(data.error);
      closeSessionEditor();
      await reloadSession(ed.sessionId);
    } else {
      const ranges = sessionEditorRanges().map((r, i) => ({
        start: r.start,
        end: r.end,
        title: _sessionEditor.titles[i] || r.title,
      }));
      data = await fetch(`/api/sessions/${ed.sessionId}/split`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // Splitting one meeting into N parts produces N sessions, not N+1.
        // The source row + media is replaced by its split parts.
        body: JSON.stringify({ ranges, delete_original: true }),
      }).then(r => r.json());
      if (data.error) throw new Error(data.error);
      closeSessionEditor();
      refreshSidebar();
      if (data.sessions?.[0]?.session_id) await loadSession(data.sessions[0].session_id);
    }
  } catch (e) {
    uiToast({ message: e.message || 'Session edit failed', kind: 'error' });
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = ed.mode === 'trim' ? 'Apply Trim' : 'Create Splits';
    }
  }
}

async function restoreSessionEditorOriginal() {
  const ed = _sessionEditor;
  if (!ed) return;
  // Split rollback takes priority - the original session was deleted at split
  // time, so the only thing to restore is the pre-split snapshot. The split
  // restore has its own modal (lets the user choose which parts to delete).
  if (ed.hasSplitBackup) {
    openSplitRestoreDialog(ed.sessionId);
    return;
  }
  if (!ed.hasTrimBackup) return;
  if (!await uiConfirm({ title: 'Restore original session?', message: 'Restore the original audio, video, transcript, and speaker labels for this session?', confirmLabel: 'Restore', danger: true })) return;
  const btn = document.getElementById('session-editor-restore');
  const applyBtn = document.getElementById('session-editor-apply');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Restoring...';
  }
  if (applyBtn) applyBtn.disabled = true;
  try {
    const data = await fetch(`/api/sessions/${ed.sessionId}/restore`, {
      method: 'POST',
    }).then(r => r.json());
    if (data.error) throw new Error(data.error);
    closeSessionEditor();
    await reloadSession(ed.sessionId);
  } catch (e) {
    uiToast({ message: e.message || 'Restore failed', kind: 'error' });
    sessionEditorUpdateRestoreButton();
  } finally {
    if (btn) {
      btn.textContent = 'Restore Original';
      btn.disabled = false;
    }
    if (applyBtn) applyBtn.disabled = false;
  }
}

/* ── Split rollback (Undo Split) ─────────────────────────────────────────── */

async function openSplitRestoreDialog(sessionId) {
  const sid = sessionId || state.sessionId;
  if (!sid) return;
  let info;
  try {
    info = await fetch(`/api/sessions/${sid}/split-info`).then(r => r.json());
  } catch (e) {
    uiToast({ message: 'Could not load split info: ' + (e.message || e), kind: 'error' });
    return;
  }
  if (!info.has_backup) {
    uiToast({ message: 'No split backup available for this session.', kind: 'warn' });
    return;
  }

  // Build the modal DOM on demand (one per page; reused across opens)
  let overlay = document.getElementById('split-restore-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.className = 'overlay hidden';
    overlay.id = 'split-restore-overlay';
    overlay.innerHTML = `
      <div class="dialog split-restore-dialog">
        <div class="split-restore-header">
          <div class="split-restore-header-left">
            <div class="split-restore-icon"><i class="fa-solid fa-rotate-left"></i></div>
            <div>
              <div class="split-restore-title">Undo Split</div>
              <div class="split-restore-subtitle" id="split-restore-subtitle"></div>
            </div>
          </div>
          <button class="icon-btn" onclick="closeSplitRestoreDialog()"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <div class="split-restore-body">
          <p class="split-restore-desc">
            Recreates the original meeting from its pre-split backup.
            Choose which split parts to delete along with the restore.
            <b>Unchecked parts will be kept as standalone sessions.</b>
          </p>
          <div class="split-restore-parts" id="split-restore-parts"></div>
        </div>
        <div class="split-restore-actions">
          <button class="split-restore-secondary" onclick="closeSplitRestoreDialog()">Cancel</button>
          <button class="split-restore-primary" id="split-restore-confirm">Restore Original</button>
        </div>
      </div>`;
    overlay.addEventListener('click', ev => { if (ev.target === overlay) closeSplitRestoreDialog(); });
    document.body.appendChild(overlay);
  }

  // Populate the header and member checkboxes
  const orig = info.original || {};
  const subtitle = document.getElementById('split-restore-subtitle');
  const whenTxt = orig.started_at ? _formatSplitRestoreDate(orig.started_at) : '';
  subtitle.textContent = orig.title ? `"${orig.title}"${whenTxt ? ' · ' + whenTxt : ''}` : (whenTxt || '');

  const list = document.getElementById('split-restore-parts');
  list.innerHTML = '';
  const members = info.members || [];
  if (!members.length) {
    list.innerHTML = '<p class="empty-hint">No split parts remain: restore will simply recreate the original.</p>';
  } else {
    members.forEach(m => {
      const row = document.createElement('label');
      row.className = 'split-restore-part' + (m.id === sid ? ' split-restore-part--self' : '');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = true;
      cb.dataset.partId = m.id;
      const title = document.createElement('span');
      title.className = 'split-restore-part-title';
      title.textContent = m.title || 'Untitled';
      const meta = document.createElement('span');
      meta.className = 'split-restore-part-meta';
      const parts = [];
      if (m.started_at) parts.push(_formatSplitRestoreDate(m.started_at));
      if (m.id === sid) parts.push('current');
      if (m.title_user_set) parts.push('renamed');
      meta.textContent = parts.join(' · ');
      row.appendChild(cb);
      row.appendChild(title);
      row.appendChild(meta);
      list.appendChild(row);
    });
  }

  // Wire the primary button fresh each open so `sid` is captured correctly
  const confirm = document.getElementById('split-restore-confirm');
  confirm.onclick = () => _doSplitRestore(sid);

  overlay.classList.remove('hidden');
}

function closeSplitRestoreDialog() {
  document.getElementById('split-restore-overlay')?.classList.add('hidden');
}

// Esc closes the split-restore dialog (registered once)
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const ov = document.getElementById('split-restore-overlay');
    if (ov && !ov.classList.contains('hidden')) closeSplitRestoreDialog();
  }
});

function _formatSplitRestoreDate(iso) {
  try {
    const d = new Date(iso + 'Z');
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch { return iso || ''; }
}

async function _doSplitRestore(sessionId) {
  const confirmBtn = document.getElementById('split-restore-confirm');
  const deleteIds = [...document.querySelectorAll('#split-restore-parts input[type=checkbox]')]
    .filter(cb => cb.checked)
    .map(cb => cb.dataset.partId);

  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = 'Restoring…'; }
  try {
    const r = await fetch(`/api/sessions/${sessionId}/restore-split`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delete_session_ids: deleteIds }),
    }).then(r => r.json());
    if (r.error) throw new Error(r.error);
    closeSplitRestoreDialog();
    // Close the session editor if open - the session it was editing may no
    // longer exist (e.g. user checked "delete this part")
    const ed = document.getElementById('session-editor-overlay');
    if (ed && !ed.classList.contains('hidden')) closeSessionEditor();
    await refreshSidebar();
    if (r.restored_session_id) await loadSession(r.restored_session_id);
    flashStatus('Original meeting restored');
  } catch (e) {
    uiToast({ message: 'Restore failed: ' + (e.message || e), kind: 'error' });
  } finally {
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Restore Original'; }
  }
}

/* ── Video viewer ────────────────────────────────────────────────────────── */
let _videoAvailable = false;
let _videoVisible   = false;
let _videoOffset    = 0; // audio seconds where the video file starts (>0 on resumed sessions)
const _playbackVideo = document.getElementById('playback-video');

function initVideo(sessionId, offset) {
  _videoOffset = offset || 0;
  const video = _playbackVideo;
  // Buffer ahead so forward playback and seeks rarely stall waiting on disk.
  video.preload = 'auto';
  video.src = `/api/sessions/${sessionId}/video`;
  video.load();
  _videoAvailable = true;
  _lastHardSeekAt = 0;
  _resumeAfterSeek = false;
  _videoScrubbing = false;
  _videoSeekPending = false;

  // Show the toggle button in the playback bar
  document.getElementById('playback-video-toggle').classList.remove('hidden');

  // Sync playback rate with audio
  video.playbackRate = _playbackAudio.playbackRate;

  // When video metadata loads, ensure time is synced
  video.onloadedmetadata = () => {
    _syncVideoToAudio();
  };

  // Restore video viewer visibility from saved preference
  if (_prefs.video_viewer_open) {
    _videoVisible = true;
    document.getElementById('video-viewer').classList.remove('hidden');
    _syncPanelBottomRadius();
    document.getElementById('playback-video-toggle').classList.add('active');
    video.onloadedmetadata = () => {
      _syncVideoToAudio();
      if (!_playbackAudio.paused) video.play().catch(() => {});
    };
  }
}

function destroyVideo() {
  _stopVideoSyncLoop();
  _playbackVideo.pause();
  _playbackVideo.removeAttribute('src');
  _playbackVideo.load();
  _playbackVideo.playbackRate = 1;
  _videoAvailable = false;
  _videoVisible = false;
  _videoOffset = 0;
  _videoSeekPending = false;
  _videoScrubbing = false;
  _resumeAfterSeek = false;
  clearTimeout(_videoSeekWatchdog);
  _videoSeekWatchdog = 0;
  _cancelVideoSeek();
  // Reset cleanup popup so it doesn't keep stale video from the previous session.
  try {
    _cvStopPreview();
    const cv = document.getElementById('cleanup-video');
    if (cv) { cv.pause(); cv.removeAttribute('src'); cv.load(); }
    const popup = document.getElementById('cleanup-video-popup');
    if (popup && !popup.hidden) popup.hidden = true;
    _cleanupVideoLoadedFor = null;
    _cleanupVideoUserClosed = false;
    _cleanupVideoSyncToggleBtn();
  } catch (_) {}
  // Exit fullscreen if we were in it
  if (_videoMode === 'fullscreen') setVideoMode('compact');
  resetVideoZoom();
  document.getElementById('video-viewer').classList.add('hidden');
  document.getElementById('playback-video-toggle').classList.add('hidden');
  const btn = document.getElementById('playback-video-toggle');
  btn.classList.remove('active');
  _syncPanelBottomRadius();
}

function toggleVideoViewer() {
  if (!_videoAvailable) return;
  _videoVisible = !_videoVisible;
  document.getElementById('video-viewer').classList.toggle('hidden', !_videoVisible);
  document.getElementById('playback-video-toggle').classList.toggle('active', _videoVisible);
  _syncPanelBottomRadius();
  savePref('video_viewer_open', _videoVisible);
  if (_videoVisible) {
    // Sync video to current audio position
    _syncVideoToAudio();
    // 'seeked' listener resumes if _syncVideoToAudio kicked off a seek
    if (!_playbackAudio.paused && !_videoSeekPending) {
      _playbackVideo.play().catch(() => {});
    }
  } else {
    _playbackVideo.pause();
    // If we were in fullscreen, leave that mode too
    if (_videoMode === 'fullscreen') setVideoMode('compact');
  }
}

/* ── Video mode (compact / fill / fullscreen) ─────────────────────────────── */
let _videoMode = 'compact';   // 'compact' | 'fill' | 'fullscreen'
const _VIDEO_MODE_CLASSES = {
  compact:    '',
  fill:       'video-viewer--fill',
  fullscreen: 'video-viewer--fullscreen',
};

function setVideoMode(mode) {
  if (!_VIDEO_MODE_CLASSES.hasOwnProperty(mode)) return;
  const viewer = document.getElementById('video-viewer');
  if (!viewer) return;
  // If the viewer was hidden and user activated fill/fullscreen, open it first
  if (viewer.classList.contains('hidden') && mode !== 'compact') {
    if (_videoAvailable) {
      _videoVisible = true;
      viewer.classList.remove('hidden');
      document.getElementById('playback-video-toggle')?.classList.add('active');
      savePref('video_viewer_open', true);
      _syncVideoToAudio();
      // 'seeked' listener will resume if a seek is in flight
      if (!_playbackAudio.paused && !_videoSeekPending) {
        _playbackVideo.play().catch(() => {});
      }
    } else {
      return;
    }
  }
  // Toggle off if clicking the already-active non-compact mode
  if (mode === _videoMode && mode !== 'compact') mode = 'compact';

  _videoMode = mode;
  viewer.dataset.videoMode = mode;
  // Apply mode class
  for (const [m, cls] of Object.entries(_VIDEO_MODE_CLASSES)) {
    if (cls) viewer.classList.toggle(cls, m === mode);
  }
  // Body flag for fullscreen (used to float playback-bar over video)
  document.body.classList.toggle('video-fullscreen', mode === 'fullscreen');

  // Sync toolbar active state
  const btnMap = { compact: 'video-btn-compact', fill: 'video-btn-fill', fullscreen: 'video-btn-fullscreen' };
  for (const [m, id] of Object.entries(btnMap)) {
    document.getElementById(id)?.classList.toggle('active', m === mode);
  }
  // Reset zoom on mode change (geometry changed, old translate is meaningless)
  resetVideoZoom();
  _syncPanelBottomRadius();
  savePref('video_mode', mode);
}

/* ── Zoom / pan (wheel to zoom at cursor, drag to pan when zoomed) ────────── */
let _videoZoom = { scale: 1, tx: 0, ty: 0 };
let _videoZoomHintTimer = 0;

function _videoClampTranslate() {
  // Keep the video element within its viewport (don't allow scrolling past edges)
  const vp = document.getElementById('video-viewport');
  if (!vp) return;
  const vpRect = vp.getBoundingClientRect();
  const s = _videoZoom.scale;
  const vidW = vpRect.width;   // video element is width:100% of viewport
  const vidH = _playbackVideo.clientHeight || vpRect.height;
  const scaledW = vidW * s;
  const scaledH = vidH * s;
  const minTx = Math.min(0, vpRect.width  - scaledW);
  const minTy = Math.min(0, vpRect.height - scaledH);
  _videoZoom.tx = Math.max(minTx, Math.min(0, _videoZoom.tx));
  _videoZoom.ty = Math.max(minTy, Math.min(0, _videoZoom.ty));
}

function _videoApplyZoom() {
  const v = _playbackVideo;
  if (!v) return;
  v.style.setProperty('--vz-scale', _videoZoom.scale.toFixed(4));
  v.style.setProperty('--vz-tx', _videoZoom.tx.toFixed(2) + 'px');
  v.style.setProperty('--vz-ty', _videoZoom.ty.toFixed(2) + 'px');
  const vp = document.getElementById('video-viewport');
  if (vp) vp.classList.toggle('zoomed', _videoZoom.scale > 1.001);
  // Update hint
  const hint = document.getElementById('video-zoom-hint');
  if (hint) {
    hint.textContent = Math.round(_videoZoom.scale * 100) + '%';
    hint.classList.remove('hidden');
    clearTimeout(_videoZoomHintTimer);
    // Only auto-hide when back at 100%
    if (_videoZoom.scale <= 1.001) {
      _videoZoomHintTimer = setTimeout(() => hint.classList.add('hidden'), 900);
    }
  }
}

function resetVideoZoom() {
  _videoZoom = { scale: 1, tx: 0, ty: 0 };
  _videoApplyZoom();
}

function _videoZoomAt(viewportX, viewportY, factor) {
  const oldScale = _videoZoom.scale;
  let newScale = oldScale * factor;
  newScale = Math.max(1, Math.min(8, newScale));
  if (Math.abs(newScale - oldScale) < 1e-4) return;
  // Keep the content point under the cursor fixed: tx' = mx - (mx - tx) * (newScale / oldScale)
  const ratio = newScale / oldScale;
  _videoZoom.tx = viewportX - (viewportX - _videoZoom.tx) * ratio;
  _videoZoom.ty = viewportY - (viewportY - _videoZoom.ty) * ratio;
  _videoZoom.scale = newScale;
  _videoClampTranslate();
  _videoApplyZoom();
}

function _initVideoZoomControls() {
  const vp = document.getElementById('video-viewport');
  if (!vp || vp._zoomWired) return;
  vp._zoomWired = true;

  // Wheel → zoom at cursor
  vp.addEventListener('wheel', (e) => {
    // Only intercept when viewer is visible
    const viewer = document.getElementById('video-viewer');
    if (!viewer || viewer.classList.contains('hidden')) return;
    e.preventDefault();
    const rect = vp.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    // Exponential zoom feels natural; tune sensitivity via 0.0015
    const factor = Math.exp(-e.deltaY * 0.0015);
    _videoZoomAt(mx, my, factor);
  }, { passive: false });

  // Double-click → reset
  vp.addEventListener('dblclick', (e) => {
    e.preventDefault();
    resetVideoZoom();
  });

  // Drag → pan (only when zoomed)
  let dragging = false;
  let lastX = 0, lastY = 0;
  vp.addEventListener('pointerdown', (e) => {
    if (_videoZoom.scale <= 1.001) return;
    // Don't start pan from the toolbar
    if (e.target.closest('.video-toolbar')) return;
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
    vp.classList.add('panning');
    vp.setPointerCapture?.(e.pointerId);
  });
  vp.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    lastX = e.clientX;
    lastY = e.clientY;
    _videoZoom.tx += dx;
    _videoZoom.ty += dy;
    _videoClampTranslate();
    _videoApplyZoom();
  });
  const endPan = (e) => {
    if (!dragging) return;
    dragging = false;
    vp.classList.remove('panning');
    try { vp.releasePointerCapture?.(e.pointerId); } catch {}
  };
  vp.addEventListener('pointerup', endPan);
  vp.addEventListener('pointercancel', endPan);
  vp.addEventListener('pointerleave', endPan);

  // Esc exits fullscreen
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _videoMode === 'fullscreen') {
      setVideoMode('compact');
    }
  });

  // Re-clamp translate on resize (viewport dimensions changed)
  window.addEventListener('resize', () => {
    if (_videoZoom.scale > 1.001) {
      _videoClampTranslate();
      _videoApplyZoom();
    }
  });
}

// Wire up zoom controls as soon as the DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initVideoZoomControls);
} else {
  _initVideoZoomControls();
}

function _audioToVideoTime(audioTime) {
  return Math.max(0, audioTime - _videoOffset);
}

/* ── Video/audio soft-sync controller ──────────────────────────────────────
   The screen recording (muted <video>) is slaved to the meeting audio
   (<audio>, the master clock). They are two independent media elements, and
   the recording is a low-fps screencap whose keyframes can be many seconds
   apart, so seeking it is slow and lands coarsely. Correcting ordinary drift
   by re-seeking therefore oscillates: the decoder snaps back to a keyframe,
   plays a moment, drifts, re-seeks... which is the "replays a short snippet
   forever" symptom.

   So instead we:
     - let the video free-run and nudge its playbackRate to converge on the
       audio clock for small / medium drift (no decode jump, perfectly smooth);
     - hard-seek ONLY across genuine discontinuities (user seek/scrub, filter
       skip, large stall), throttled so seeks can never spam the decoder;
     - never strand a seek "pending" forever: every hard-seek arms a watchdog.
   Tolerances are deliberately generous because the video is muted and purely
   a visual reference, so sub-second offset is invisible. */
const _VS = {
  IN_SYNC:     0.15,   // |drift| under this -> run at exact master rate
                       // (kept above the low-fps frame interval so the rate
                       //  does not wobble as each decoded frame ticks)
  HARD_DRIFT:  0.75,   // |drift| at/above this -> hard-seek (unless throttled)
  RATE_MAX:    0.18,   // max +/-18% playbackRate nudge while converging
  SEEK_MIN_MS: 350,    // min wall-time between throttled (drift) hard-seeks
  WATCHDOG_MS: 2000,   // force-clear a stuck pending seek after this long
  NOOP:        0.04,   // a seek within this of current time is a no-op
};
let _videoScrubbing        = false; // true while the user drags the seek bar
let _videoSeekPending      = false; // between currentTime= and its 'seeked'
let _videoSeekDebounce     = 0;     // debounce timer id (scrub preview seeks)
let _videoSeekWatchdog     = 0;     // watchdog timer id for a pending seek
let _videoRAF              = 0;     // requestAnimationFrame id for sync loop
let _wasPlayingBeforeScrub = false;
let _resumeAfterSeek       = false; // resume audio+video once next seek lands
let _lastHardSeekAt        = 0;     // perf clock of last drift-correction seek

function _vsNow() {
  return (typeof performance !== 'undefined' && performance.now)
    ? performance.now() : Date.now();
}

function _setPlayBtn(playing) {
  const b = document.getElementById('playback-play');
  if (b) b.innerHTML = playing
    ? '<i class="fa-solid fa-pause"></i>'
    : '<i class="fa-solid fa-play"></i>';
}

// Clamp a desired video time into the element's valid, seekable span.
function _clampVideoTarget(t) {
  t = Math.max(0, t);
  const d = _playbackVideo ? _playbackVideo.duration : NaN;
  if (isFinite(d) && d > 0) t = Math.min(t, d - 0.05);
  return t;
}

function _cancelVideoSeek() {
  clearTimeout(_videoSeekDebounce);
  _videoSeekDebounce = 0;
}

// Issue a real seek. `force` bypasses the anti-spam throttle (use for
// user-driven seeks); drift-correction seeks pass force=false so they can
// never fire faster than SEEK_MIN_MS. Returns true if the request was handled
// (seek issued OR already on target), false if throttled (the caller then
// falls back to rate correction).
function _hardSeek(target, force) {
  if (!_playbackVideo) return false;
  target = _clampVideoTarget(target);
  const now = _vsNow();
  if (!force && (now - _lastHardSeekAt) < _VS.SEEK_MIN_MS) return false;
  if (Math.abs(_playbackVideo.currentTime - target) < _VS.NOOP) {
    // Already there: the browser will not fire 'seeked', so settle now. (This
    // no-op case is what used to strand playback after releasing the scrubber.)
    _afterSeekLanded();
    return true;
  }
  _lastHardSeekAt = now;
  // Reset any catch-up nudge so we resume at a clean master rate after landing.
  const base = _playbackAudio.playbackRate || 1;
  if (Math.abs(_playbackVideo.playbackRate - base) > 1e-3) _playbackVideo.playbackRate = base;
  _videoSeekPending = true;
  clearTimeout(_videoSeekWatchdog);
  _videoSeekWatchdog = setTimeout(_videoSeekTimedOut, _VS.WATCHDOG_MS);
  try {
    _playbackVideo.currentTime = target;
  } catch (_) {
    _videoSeekPending = false;
    clearTimeout(_videoSeekWatchdog);
    _videoSeekWatchdog = 0;
  }
  return true;
}

// Back-compat alias still called from _skipFilteredAudio and elsewhere.
function _seekVideoImmediate(targetTime) { _hardSeek(targetTime, true); }

// Debounced seek used for scrub preview: show frames as the user drags
// without firing a decode for every pixel of pointer movement.
function _seekVideoDebounced(targetTime, delayMs) {
  _cancelVideoSeek();
  _videoSeekDebounce = setTimeout(() => { _hardSeek(targetTime, true); }, delayMs);
}

function _videoSeekTimedOut() {
  _videoSeekWatchdog = 0;
  _videoSeekPending = false;
  _afterSeekLanded();
}

// Called whenever a seek has actually landed (real 'seeked', a no-op seek, or
// the watchdog). Resumes transport if something was waiting on the landing.
function _afterSeekLanded() {
  if (_resumeAfterSeek) {
    _resumeAfterSeek = false;
    if (_playbackActive) { _playbackAudio.play().catch(() => {}); _setPlayBtn(true); }
  }
  if (_videoScrubbing) return;
  if (!_videoAvailable || !_videoVisible) return;
  if (!_playbackAudio.paused) {
    if (_playbackVideo.paused) _playbackVideo.play().catch(() => {});
    _startVideoSyncLoop();
  }
}

// Home page has no playback-video element, so guard the listener attach.
if (_playbackVideo) {
  _playbackVideo.addEventListener('seeked', () => {
    clearTimeout(_videoSeekWatchdog);
    _videoSeekWatchdog = 0;
    _videoSeekPending = false;
    _afterSeekLanded();
  });
  // A decode/seek error must not leave the pending flag stuck forever.
  _playbackVideo.addEventListener('error', () => {
    clearTimeout(_videoSeekWatchdog);
    _videoSeekWatchdog = 0;
    _videoSeekPending = false;
  });
}

// One correction step: mirror play/pause, hard-seek across discontinuities,
// otherwise nudge playbackRate to converge on the audio clock.
function _videoSyncOnce() {
  if (!_videoAvailable || !_videoVisible) return;
  if (_videoScrubbing || _videoSeekPending) return;
  const v = _playbackVideo, a = _playbackAudio;
  const base = a.playbackRate || 1;
  const raw = a.currentTime - _videoOffset;   // unclamped video time
  const dur = v.duration;
  const beforeStart = raw < -0.05;            // audio is before the video began
  const afterEnd    = isFinite(dur) && dur > 0 && raw >= dur - 0.05;

  // Dead zones (audio paused, or audio outside the video's span): hold video.
  if (a.paused || beforeStart || afterEnd) {
    if (!v.paused) v.pause();
    if (a.paused && !beforeStart && !afterEnd) {
      // Keep the visible frame aligned to the audio playhead while paused.
      const exp = _clampVideoTarget(raw);
      if (Math.abs(v.currentTime - exp) > 0.34) _hardSeek(exp, true);
    } else if (beforeStart && v.currentTime > 0.05) {
      _hardSeek(0, true);
    }
    return;
  }

  // Playing, and inside the video span.
  const expected = _clampVideoTarget(raw);
  const signed = v.currentTime - expected;    // + ahead of audio, - behind
  const adrift = Math.abs(signed);

  if (adrift >= _VS.HARD_DRIFT) {
    // Big gap (filter skip / explicit seek / long stall). Try a throttled
    // hard-seek; if throttled, fall through to a max rate nudge for now.
    if (_hardSeek(expected, false)) return;
  }

  if (v.paused) v.play().catch(() => {});

  let corr = 0;
  if (adrift > _VS.IN_SYNC) {
    // behind (signed < 0) -> speed up; ahead -> slow down. Proportional, capped.
    corr = Math.max(-_VS.RATE_MAX,
                    Math.min(_VS.RATE_MAX, (-signed / _VS.HARD_DRIFT) * _VS.RATE_MAX));
  }
  const want = base * (1 + corr);
  if (Math.abs(v.playbackRate - want) > 1e-3) v.playbackRate = want;
}

// rAF-driven loop: smooth per-frame convergence, independent of the media
// element's irregular 'timeupdate' cadence. Self-stops when audio pauses or
// the viewer hides; (re)started on play / viewer-open / as a timeupdate safety.
function _videoSyncLoop() {
  _videoRAF = 0;
  _videoSyncOnce();
  if (_videoAvailable && _videoVisible && !_playbackAudio.paused) {
    _videoRAF = requestAnimationFrame(_videoSyncLoop);
  }
}
function _startVideoSyncLoop() {
  if (!_videoRAF && _videoAvailable && _videoVisible) {
    _videoRAF = requestAnimationFrame(_videoSyncLoop);
  }
}
function _stopVideoSyncLoop() {
  if (_videoRAF) { cancelAnimationFrame(_videoRAF); _videoRAF = 0; }
}

// Back-compat name used by initVideo / toggleVideoViewer / setVideoMode:
// realign immediately and make sure the loop runs if we are playing.
function _syncVideoToAudio() {
  _videoSyncOnce();
  if (_videoAvailable && _videoVisible && !_playbackAudio.paused) _startVideoSyncLoop();
}

// ── Scrub (dragging the seek bar) ──────────────────────────────────────────
{
  const seekBar = document.getElementById('playback-seek');
  if (seekBar) {
    const startScrub = () => {
      if (_videoScrubbing) return;
      _videoScrubbing = true;
      _cancelVideoSeek();
      _stopVideoSyncLoop();
      _wasPlayingBeforeScrub = !_playbackAudio.paused;
      if (_wasPlayingBeforeScrub) { _playbackAudio.pause(); _setPlayBtn(true); }
      if (_videoAvailable && !_playbackVideo.paused) _playbackVideo.pause();
    };
    const endScrub = () => {
      if (!_videoScrubbing) return;
      _videoScrubbing = false;
      _cancelVideoSeek();
      _resumeAfterSeek = _wasPlayingBeforeScrub;
      _wasPlayingBeforeScrub = false;
      if (_videoAvailable) {
        // _hardSeek settles the resume via _afterSeekLanded, including the
        // no-op case that previously left playback stuck after release.
        _hardSeek(_audioToVideoTime(_playbackAudio.currentTime), true);
      } else if (_resumeAfterSeek) {
        _resumeAfterSeek = false;
        _playbackAudio.play().catch(() => {});
        _setPlayBtn(true);
      }
    };
    // Pointer events cover mouse, pen and touch in one path.
    seekBar.addEventListener('pointerdown', startScrub);
    window.addEventListener('pointerup', endScrub);
    window.addEventListener('pointercancel', endScrub);
    // Keyboard / programmatic value changes that never produced a pointerdown.
    seekBar.addEventListener('change', () => {
      if (_videoScrubbing || !_videoAvailable) return;
      _hardSeek(_audioToVideoTime(parseFloat(seekBar.value)), true);
    });
  }
}

// ── Keep the public playback controls in sync with the video ───────────────
const _origTogglePlayback = togglePlayback;
togglePlayback = function() {
  _origTogglePlayback();          // flips audio + play button
  if (!_videoAvailable || !_videoVisible) return;
  if (_playbackAudio.paused) { _stopVideoSyncLoop(); _playbackVideo.pause(); }
  else { _videoSyncOnce(); _startVideoSyncLoop(); }
};

const _origSeekPlayback = seekPlayback;
seekPlayback = function(val) {
  _origSeekPlayback(val);         // moves the audio (master) clock
  if (!_videoAvailable) return;
  const target = _audioToVideoTime(parseFloat(val));
  if (_videoScrubbing) _seekVideoDebounced(target, 120);  // preview while dragging
  else _hardSeek(target, true);                           // click / programmatic
};

const _origSeekToTime = seekToTime;
seekToTime = function(t) {
  _origSeekToTime(t);             // moves audio + starts playing (segment click)
  if (!_videoAvailable) return;
  _hardSeek(_audioToVideoTime(t), true);
  if (_videoVisible && !_playbackAudio.paused) _startVideoSyncLoop();
};

const _origSetPlaybackSpeed = setPlaybackSpeed;
setPlaybackSpeed = function(val) {
  _origSetPlaybackSpeed(val);     // sets audio (master) rate
  // New base rate; the sync loop nudges around it. Set directly so a paused
  // video also adopts the new rate immediately.
  if (_videoAvailable) _playbackVideo.playbackRate = parseFloat(val);
};

// ── Mirror master (audio) transport onto the video ─────────────────────────
_playbackAudio.addEventListener('play', () => {
  if (!_videoAvailable || !_videoVisible) return;
  _videoSyncOnce();
  _startVideoSyncLoop();
});
_playbackAudio.addEventListener('pause', () => {
  _stopVideoSyncLoop();
  if (_videoAvailable) _playbackVideo.pause();
});
_playbackAudio.addEventListener('ended', () => {
  _stopVideoSyncLoop();
  if (_videoAvailable) _playbackVideo.pause();
});
// Safety net: if the rAF loop is somehow not running while we should be
// syncing (e.g. it was cancelled by a tab switch), restart it. Cheap because
// it only schedules a frame when none is pending.
_playbackAudio.addEventListener('timeupdate', () => {
  if (_videoAvailable && _videoVisible && !_playbackAudio.paused
      && !_videoScrubbing && !_videoRAF) {
    _startVideoSyncLoop();
  }
});

/* ── Live screen preview ─────────────────────────────────────────────────── */
let _screenPreviewVisible = false;
let _screenPreviewRunning = false;
const _SCREEN_PREVIEW_DELAY = 500; // ms between frames (after previous completes)

function toggleScreenPreview() {
  _screenPreviewVisible = !_screenPreviewVisible;
  const panel = document.getElementById('screen-preview');
  const btn   = document.getElementById('screen-preview-toggle');
  if (panel) panel.classList.toggle('hidden', !_screenPreviewVisible);
  if (btn)   btn.classList.toggle('active', _screenPreviewVisible);
  _syncPanelBottomRadius();
  if (_screenPreviewVisible && !_screenPreviewRunning) {
    _screenPreviewLoop();
  }
}

async function _screenPreviewLoop() {
  _screenPreviewRunning = true;
  const img = document.getElementById('screen-preview-img');
  while (_screenPreviewVisible && img) {
    try {
      const resp = await fetch('/api/screen/preview?_=' + Date.now());
      if (!_screenPreviewVisible) break;
      if (resp.ok) {
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const prev = img.src;
        img.src = url;
        if (!img.dataset.loaded) img.dataset.loaded = '1';
        // Revoke old blob URL to avoid memory leaks
        if (prev && prev.startsWith('blob:')) URL.revokeObjectURL(prev);
      }
    } catch (_) {}
    // Wait before next frame - ensures sequential, never piling up
    await new Promise(r => setTimeout(r, _SCREEN_PREVIEW_DELAY));
  }
  _screenPreviewRunning = false;
}

function _showScreenPreviewToggle(show) {
  const btn = document.getElementById('screen-preview-toggle');
  if (btn) btn.classList.toggle('hidden', !show);
}

function _stopScreenPreview() {
  _screenPreviewVisible = false;
  const panel = document.getElementById('screen-preview');
  const btn   = document.getElementById('screen-preview-toggle');
  const img   = document.getElementById('screen-preview-img');
  if (panel) panel.classList.add('hidden');
  if (btn)   { btn.classList.add('hidden'); btn.classList.remove('active'); }
  if (img)   delete img.dataset.loaded;
  _syncPanelBottomRadius();
}

/* ── Transcript collapse (consecutive speaker runs) ──────────────────────── */
const _COLLAPSE_THRESHOLD = 20;  // min segments before showing the FAB
const _COLLAPSE_RUN_MIN   = 2;   // min consecutive same-speaker segments to group
let _collapseActive = false;

function toggleTranscriptCollapse() {
  _collapseActive = !_collapseActive;
  const btn = document.getElementById('transcript-collapse-toggle');
  if (btn) btn.classList.toggle('active', _collapseActive);
  if (_collapseActive) {
    _applyCollapse();
  } else {
    _removeCollapse();
  }
}

/** Build consecutive same-speaker runs and collapse them.
 *  Groups by the resolved display name (final label), NOT the raw speaker key,
 *  so renamed/linked speakers are grouped correctly even if they have different
 *  underlying keys (e.g. "Speaker 1" and "Speaker 3" both renamed to "Joe Rogan").
 */
function _applyCollapse() {
  const el = document.getElementById('transcript');
  if (!el) return;
  // Remove any existing group summaries first
  _removeCollapse();

  // Resolve the display label for a segment's speaker
  function _resolveLabel(seg) {
    const badge = seg.querySelector('.src-badge');
    if (!badge) return seg.dataset.transcriptSource || '';
    // Use the visible text content (which reflects renames/links)
    // but strip any inline icon text (fingerprint icon etc.)
    const clone = badge.cloneNode(true);
    clone.querySelectorAll('i, .badge-alias, .speaker-identify-icon').forEach(el => el.remove());
    return clone.textContent.trim() || badge.dataset.speakerKey || '';
  }

  // Build strict runs of consecutive segments by the same display label
  const segs = Array.from(el.querySelectorAll('.transcript-segment'));
  if (!segs.length) return;

  let strictRuns = [];
  let currentRun = null;

  for (const seg of segs) {
    if (seg.style.display === 'none') continue; // filtered out
    const label = _resolveLabel(seg);
    if (currentRun && currentRun.key === label) {
      currentRun.segs.push(seg);
    } else {
      if (currentRun) strictRuns.push(currentRun);
      currentRun = { key: label, segs: [seg] };
    }
  }
  if (currentRun) strictRuns.push(currentRun);

  // Merge pass: merge adjacent runs from the same speaker (no interstitial absorption)
  const merged = [strictRuns[0]];
  for (let i = 1; i < strictRuns.length; i++) {
    const prev = merged[merged.length - 1];
    const curr = strictRuns[i];
    if (curr.key === prev.key) {
      prev.segs.push(...curr.segs);
    } else {
      merged.push(curr);
    }
  }

  // Collapse runs that meet the minimum count
  for (const run of merged) {
    if (run.segs.length < _COLLAPSE_RUN_MIN) continue;

    const first = run.segs[0];
    const last  = run.segs[run.segs.length - 1];
    const badge = first.querySelector('.src-badge');
    const name  = badge?.textContent?.trim() || run.key;
    const color = first.style.getPropertyValue('--seg-color') || 'var(--accent-dim)';

    // Time range
    const startT = parseFloat(first.dataset.start || '0');
    const endT   = parseFloat(last.dataset.end || last.dataset.start || '0');

    // Create summary row
    const summary = document.createElement('div');
    summary.className = 'transcript-group-summary';
    summary.style.setProperty('--seg-color', color);
    summary.dataset.collapseGroup = '1';

    const chevron = document.createElement('i');
    chevron.className = 'fa-solid fa-chevron-right group-chevron';
    summary.appendChild(chevron);

    // Speaker badge clone
    const badgeClone = badge.cloneNode(true);
    badgeClone.style.cursor = 'default';
    summary.appendChild(badgeClone);

    // Time span
    if (endT > 0) {
      const timeSpan = document.createElement('span');
      timeSpan.className = 'group-time';
      timeSpan.textContent = `${fmtTime(startT)} to ${fmtTime(endT)}`;
      summary.appendChild(timeSpan);
    }

    // Count
    const countSpan = document.createElement('span');
    countSpan.className = 'group-count';
    countSpan.textContent = `${run.segs.length} segments`;
    summary.appendChild(countSpan);

    // Click to expand/collapse the group
    summary._groupSegs = run.segs;
    summary.addEventListener('click', () => {
      const expanded = summary.classList.toggle('expanded');
      for (const seg of summary._groupSegs) {
        seg.style.display = expanded ? '' : 'none';
        seg.dataset.collapsedHidden = expanded ? '' : '1';
        seg.classList.toggle('in-group', expanded);
      }
      _refreshMinimap(true);
    });

    // Insert summary before first segment, hide all segments
    // Link each segment back to its parent group for playback highlighting
    first.parentNode.insertBefore(summary, first);
    for (const seg of run.segs) {
      seg.style.display = 'none';
      seg.dataset.collapsedHidden = '1';
      seg._groupSummary = summary;
    }
  }
}

/** Remove all collapse summaries and restore segment visibility. */
function _removeCollapse() {
  const el = document.getElementById('transcript');
  if (!el) return;
  // Restore segments hidden by collapse (not by filter)
  el.querySelectorAll('[data-collapsed-hidden]').forEach(seg => {
    delete seg.dataset.collapsedHidden;
    seg.style.display = '';
    seg.classList.remove('in-group');
    delete seg._groupSummary;
  });
  el.querySelectorAll('.transcript-group-summary').forEach(s => s.remove());
  // Re-apply transcript filter in case some segments should still be hidden
  if (typeof applyTranscriptFilter === 'function') applyTranscriptFilter();
}

/** Show or hide the collapse FAB based on segment count. */
function _updateCollapseFabVisibility() {
  const btn = document.getElementById('transcript-collapse-toggle');
  if (!btn) return;
  const show = _segmentRegistry.length >= _COLLAPSE_THRESHOLD;
  btn.classList.toggle('hidden', !show);
  if (!show && _collapseActive) {
    _collapseActive = false;
    btn.classList.remove('active');
    _removeCollapse();
  }
}

/* ── Transcript minimap ──────────────────────────────────────────────────── */
const _MINIMAP_THRESHOLD  = 10;     // min segments before FAB appears
const _MINIMAP_SEG_GAP    = 1;      // px gap between rendered blocks
let _minimapActive        = false;
let _minimapDragging      = false;
let _minimapRafPending    = false;
let _minimapPlayheadEl    = null;    // lazily created playhead line

// ── Minimap data cache ─────────────────────────────────────────────────────
// Avoids re-querying every segment's offsetHeight on each redraw.
// Invalidated explicitly when the segment list or visibility changes.
let _minimapDataCache     = null;    // cached result of _minimapSegmentData()
let _minimapDirty         = true;    // true → cache must be rebuilt before next render
let _minimapDebounceTimer = 0;       // debounce timer for live-recording redraws
const _MINIMAP_DEBOUNCE_MS = 300;    // coalesce rapid segment appends

function toggleTranscriptMinimap() {
  _minimapActive = !_minimapActive;
  const btn  = document.getElementById('transcript-minimap-toggle');
  const wrap = document.getElementById('transcript-minimap');
  if (btn)  btn.classList.toggle('active', _minimapActive);
  if (wrap) wrap.classList.toggle('hidden', !_minimapActive);
  if (_minimapActive && wrap) {
    // The minimap container transitions from width:0 via CSS. Wait for the
    // transition to finish so clientWidth/clientHeight are final before rendering.
    let rendered = false;
    const onReady = () => {
      if (rendered) return;
      rendered = true;
      _invalidateMinimapCache();
      _renderMinimap();
      _updateMinimapViewport();
    };
    wrap.addEventListener('transitionend', function handler(e) {
      if (e.propertyName === 'width') {
        wrap.removeEventListener('transitionend', handler);
        onReady();
      }
    });
    // Fallback if transition doesn't fire (e.g., reduced motion or instant)
    setTimeout(onReady, 250);
  }
}

/** Gather segment data for the minimap: color + proportional height.
 *  Returns a cached array unless _minimapDirty is set. */
function _minimapSegmentData() {
  if (!_minimapDirty && _minimapDataCache) return _minimapDataCache;
  const transcript = document.getElementById('transcript');
  if (!transcript) return [];
  const segs = transcript.querySelectorAll('.transcript-segment');
  const data = [];
  for (const seg of segs) {
    if (seg.style.display === 'none') continue;
    const color = seg.style.getPropertyValue('--seg-color') || '#8b949e';
    // Use element height for accurate proportions
    const h = seg.offsetHeight || 40;
    data.push({ color, height: h, el: seg });
  }
  _minimapDataCache = data;
  _minimapDirty = false;
  return data;
}

/** Mark minimap data as stale - next render will rebuild. */
function _invalidateMinimapCache() { _minimapDirty = true; }

/** Render the minimap canvas with colored blocks per segment. */
function _renderMinimap() {
  if (!_minimapActive) return;
  const canvas = document.getElementById('minimap-canvas');
  const container = document.getElementById('transcript-minimap');
  if (!canvas || !container) return;

  const dpr = window.devicePixelRatio || 1;
  const cw = container.clientWidth;
  const ch = container.clientHeight;

  canvas.width  = cw * dpr;
  canvas.height = ch * dpr;
  canvas.style.width  = cw + 'px';
  canvas.style.height = ch + 'px';

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, cw, ch);

  const segData = _minimapSegmentData();
  if (!segData.length) return;

  // Calculate total content height for scaling
  const totalHeight = segData.reduce((sum, s) => sum + s.height, 0);
  const scale = ch / totalHeight;
  const padding = 3;  // horizontal padding
  const blockWidth = cw - padding * 2;
  const minBlockH = 2;  // minimum visible block height
  const gap = _MINIMAP_SEG_GAP * scale;

  let y = 0;
  for (const seg of segData) {
    const blockH = Math.max(minBlockH, seg.height * scale - gap);
    // Parse hex color and draw with slight transparency for depth
    ctx.fillStyle = seg.color;
    ctx.globalAlpha = 0.55;
    // Rounded rect
    const r = Math.min(2, blockH / 2);
    _roundRect(ctx, padding, y, blockWidth, blockH, r);
    ctx.fill();
    ctx.globalAlpha = 1.0;
    y += blockH + gap;
  }
  renderMinimapChapters();  // overlay chapter markers on top of the segment blocks
}

/** Draw a rounded rectangle path. */
function _roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

/** Update the viewport indicator position to match transcript scroll. */
function _updateMinimapViewport() {
  if (!_minimapActive) return;
  const transcript = document.getElementById('transcript');
  const viewport   = document.getElementById('minimap-viewport');
  const container  = document.getElementById('transcript-minimap');
  if (!transcript || !viewport || !container) return;

  const scrollH   = transcript.scrollHeight;
  const clientH   = transcript.clientHeight;
  const scrollTop = transcript.scrollTop;
  const mapH      = container.clientHeight;

  if (scrollH <= clientH) {
    // Everything fits - viewport covers full minimap
    viewport.style.top    = '0px';
    viewport.style.height = mapH + 'px';
    return;
  }

  const ratio      = mapH / scrollH;
  const vpHeight   = Math.max(12, clientH * ratio);
  const vpTop      = (scrollTop / scrollH) * mapH;

  viewport.style.top    = Math.min(vpTop, mapH - vpHeight) + 'px';
  viewport.style.height = vpHeight + 'px';
}

/** Update playhead position on the minimap during playback. */
function _updateMinimapPlayhead(audioTime) {
  if (!_minimapActive || !_playbackActive) return;
  const container = document.getElementById('transcript-minimap');
  if (!container) return;

  // Find the segment closest to current playback time
  if (!_segmentTimes.length) return;
  let idx = -1;
  for (let i = 0; i < _segmentTimes.length; i++) {
    if (_segmentTimes[i].start <= audioTime) idx = i;
    else break;
  }
  if (idx < 0) {
    if (_minimapPlayheadEl) _minimapPlayheadEl.style.display = 'none';
    return;
  }

  // Map segment position to minimap Y coordinate
  const transcript = document.getElementById('transcript');
  if (!transcript) return;
  const segEl     = _segmentTimes[idx].el;
  const segTop    = segEl.offsetTop;
  const scrollH   = transcript.scrollHeight;
  const mapH      = container.clientHeight;

  if (scrollH <= 0) return;
  const yPos = (segTop / scrollH) * mapH;

  // Lazily create playhead element
  if (!_minimapPlayheadEl) {
    _minimapPlayheadEl = document.createElement('div');
    _minimapPlayheadEl.className = 'minimap-playhead';
    container.appendChild(_minimapPlayheadEl);
  }
  _minimapPlayheadEl.style.display = '';
  _minimapPlayheadEl.style.top = yPos + 'px';
}

/** Scroll the transcript based on a click/drag Y position on the minimap. */
function _minimapScrollTo(clientY) {
  const container  = document.getElementById('transcript-minimap');
  const transcript = document.getElementById('transcript');
  if (!container || !transcript) return;

  const rect = container.getBoundingClientRect();
  const yRatio = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));

  const maxScroll = transcript.scrollHeight - transcript.clientHeight;
  transcript.scrollTop = yRatio * maxScroll;
}

// Minimap click and drag handlers
{
  const minimapEl = document.getElementById('transcript-minimap');
  if (minimapEl) {
    minimapEl.addEventListener('mousedown', e => {
      e.preventDefault();
      _minimapDragging = true;
      _minimapScrollTo(e.clientY);
    });

    window.addEventListener('mousemove', e => {
      if (!_minimapDragging) return;
      e.preventDefault();
      _minimapScrollTo(e.clientY);
    });

    window.addEventListener('mouseup', () => {
      _minimapDragging = false;
    });
  }
}

// Sync minimap viewport on transcript scroll
{
  const transcript = document.getElementById('transcript');
  if (transcript) {
    transcript.addEventListener('scroll', () => {
      if (_minimapRafPending) return;
      _minimapRafPending = true;
      requestAnimationFrame(() => {
        _minimapRafPending = false;
        _updateMinimapViewport();
      });
    });
  }
}

// Re-render minimap on window resize
window.addEventListener('resize', () => {
  if (_minimapActive) {
    _invalidateMinimapCache();
    _renderMinimap();
    _updateMinimapViewport();
  }
});

/** Show or hide the minimap FAB based on segment count. */
function _updateMinimapFabVisibility() {
  const btn = document.getElementById('transcript-minimap-toggle');
  if (!btn) return;
  const show = _segmentRegistry.length >= _MINIMAP_THRESHOLD;
  btn.classList.toggle('hidden', !show);
  if (!show && _minimapActive) {
    _minimapActive = false;
    btn.classList.remove('active');
    document.getElementById('transcript-minimap')?.classList.add('hidden');
  }
}

/** Full minimap refresh - re-render canvas + viewport.
 *  Debounces during live recording to avoid per-segment redraws.
 *  Immediate when called from bulk actions (filter, speaker rename, etc.). */
let _minimapRefreshTimer = 0;
function _refreshMinimap(immediate = false) {
  if (!_minimapActive) return;
  _invalidateMinimapCache();

  // Cancel any pending debounced refresh
  if (_minimapDebounceTimer) { clearTimeout(_minimapDebounceTimer); _minimapDebounceTimer = 0; }
  if (_minimapRefreshTimer)  { cancelAnimationFrame(_minimapRefreshTimer); _minimapRefreshTimer = 0; }

  if (!immediate && state.isRecording) {
    // During live recording, debounce - segments arrive every ~0.5 s
    _minimapDebounceTimer = setTimeout(() => {
      _minimapDebounceTimer = 0;
      _minimapRefreshTimer = requestAnimationFrame(() => {
        _minimapRefreshTimer = 0;
        _renderMinimap();
        _updateMinimapViewport();
      });
    }, _MINIMAP_DEBOUNCE_MS);
  } else {
    // Immediate (one rAF) for user-driven actions
    _minimapRefreshTimer = requestAnimationFrame(() => {
      _minimapRefreshTimer = 0;
      _renderMinimap();
      _updateMinimapViewport();
    });
  }
}

/* ── Chat ────────────────────────────────────────────────────────────────── */
// Whether each pane is scrolled to (or near) the bottom.
// Auto-scroll is suppressed when the user has scrolled up; resumes on scroll-to-bottom.
let _chatAtBottom    = true;
let _summaryAtBottom = true;
const _SCROLL_BOTTOM_THRESHOLD = 60; // px tolerance

function _paneIsAtBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < _SCROLL_BOTTOM_THRESHOLD;
}

// Wire up scroll listeners once the DOM is ready.
(function _initPaneScrollTracking() {
  const chat    = document.getElementById('chat-messages');
  const summary = document.getElementById('summary');
  if (chat)    chat.addEventListener('scroll',    () => { _chatAtBottom    = _paneIsAtBottom(chat);    }, { passive: true });
  if (summary) summary.addEventListener('scroll', () => { _summaryAtBottom = _paneIsAtBottom(summary); }, { passive: true });
})();

function createAssistantBubble() {
  const el = document.getElementById('chat-messages');
  el.querySelector('.empty-hint')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg assistant';
  wrap.innerHTML = `
    <div class="chat-msg-header">
      <div class="chat-avatar assistant">AI</div>
      <span class="chat-role">Assistant</span>
    </div>
    <div class="chat-processing">
      <div class="chat-processing-dots">
        <span></span><span></span><span></span>
      </div>
      <span class="chat-processing-label">Thinking</span>
    </div>
    <div class="chat-msg-body markdown-body" style="display:none"></div>
    <div class="chat-msg-actions" style="display:none">
      <button class="chat-msg-action-btn" title="Copy response" onclick="_copyChatMsg(this)">
        <i class="fa-regular fa-copy"></i> Copy
      </button>
    </div>`;
  el.appendChild(wrap);
  scrollChatToBottom();  // response is starting - always scroll
  return wrap.querySelector('.chat-msg-body');
}

/* ── Tool-call collapsible widget ────────────────────────────────────────── */
function _renderToolWidget(msgWrap, toolCalls, isFinal = false) {
  let widget = msgWrap.querySelector('.chat-tool-widget');
  if (!widget) {
    widget = document.createElement('div');
    widget.className = 'chat-tool-widget';
    const body = msgWrap.querySelector('.chat-msg-body');
    body.parentNode.insertBefore(widget, body);
  }
  const count = toolCalls.length;
  const doneCount = toolCalls.filter(tc => tc.result).length;
  // isFinal=true is used by the hydration path (loading saved messages from
  // the DB). The response has already completed, so any tool entry whose
  // result wasn't persisted (older sessions saved before the parallel-tool
  // pairing fix) must still render as "completed" - the spinner state would
  // be permanently stuck otherwise.
  const allDone = isFinal || doneCount === count;
  const isOpen = widget.classList.contains('open');

  let itemsHtml = '';
  // Relabel plan cards live outside the collapsible detail list: they carry
  // action buttons, so they must stay visible once the widget collapses.
  let cardsHtml = '';
  for (const tc of toolCalls) {
    const hasResult = !!tc.result;
    let icon, iconCls, detail;
    if (hasResult) {
      icon = tc.result.success ? '✓' : '✗';
      iconCls = tc.result.success ? 'success' : 'error';
      detail = tc.result.summary;
    } else if (isFinal) {
      // Response completed but this entry's result was never persisted.
      icon = '✓';
      iconCls = 'success';
      detail = '(no details saved)';
    } else {
      icon = '⏳';
      iconCls = 'pending';
      detail = _toolInputSummary(tc.name, tc.input);
    }
    const label = _toolDisplayName(tc.name);
    const thumb = tc.result?.image
      ? `<img class="chat-tool-thumb" src="data:image/jpeg;base64,${tc.result.image}" alt="screenshot thumbnail">`
      : '';
    itemsHtml += `<div class="chat-tool-item">
      <div class="chat-tool-left">
        <div class="row1">
          <span class="chat-tool-icon ${iconCls}">${icon}</span>
          <span class="chat-tool-label">${escapeHtml(label)}</span>
        </div>
        <span class="chat-tool-detail">${escapeHtml(detail)}</span>
      </div>
      ${thumb}
    </div>`;
    cardsHtml += _relabelCardHtml(tc);
  }

  const statusIcon = allDone ? '<i class="fa-solid fa-wrench"></i>' : '<span class="chat-tool-spinner"></span>';
  const statusText = allDone
    ? `${count} tool use${count > 1 ? 's' : ''}`
    : `Using tools (${doneCount}/${count})`;

  widget.innerHTML = `
    <button class="chat-tool-toggle" onclick="this.closest('.chat-tool-widget').classList.toggle('open')">
      ${statusIcon}
      <span>${statusText}</span>
      <i class="fa-solid fa-chevron-right chat-tool-chevron"></i>
    </button>
    <div class="chat-tool-details">${itemsHtml}</div>
    ${cardsHtml}`;

  // Auto-expand while tools are in progress, preserve manual toggle otherwise.
  // Keep 'streaming' even after all tools complete - it's only removed on
  // first chat_chunk so the collapse fires at the right time.
  // Hydrated (isFinal) widgets skip the streaming class entirely - they're
  // rendered after the response completed and should stay collapsed unless
  // the user expands them.
  if (isFinal) {
    if (isOpen) widget.classList.add('open');
  } else if (!allDone) {
    widget.classList.add('open', 'streaming');
  } else if (widget.classList.contains('streaming')) {
    widget.classList.add('open');
  } else if (isOpen) {
    widget.classList.add('open');
  }
}

function _toolDisplayName(name) {
  const map = {
    get_screenshot: 'Screenshot',
    search_transcripts: 'Search Transcripts',
    semantic_search: 'Semantic Search',
    get_session_detail: 'Load Session',
    list_speakers: 'List Speakers',
    get_speaker_history: 'Speaker History',
    inspect_context_codebase: 'Inspect Codebase',
    list_context_files: 'List Files',
    read_context_file: 'Read File',
    search_context_files: 'Search Files',
    get_context_file_info: 'File Info',
    run_context_shell: 'Shell',
    web_search: 'Web Search',
    plan_speaker_relabel: 'Planning speaker reassignment',
    apply_speaker_relabel: 'Applying speaker reassignment',
    cancel_speaker_relabel: 'Cancelling speaker reassignment',
  };
  return map[name] || name;
}

/* ── Speaker-reassignment plan card ──────────────────────────────────────────
   plan_speaker_relabel writes nothing: it returns a plan plus a single-use
   token. The card below is the user's confirmation step, and posting the token
   back applies exactly the plan that was shown, never a re-described one. */

function _relabelSummaryLine(card) {
  const keys = card.key_count || 0;
  const sessions = card.session_count || 0;
  return `${keys} speaker${keys === 1 ? '' : 's'} in ${sessions} `
       + `meeting${sessions === 1 ? '' : 's'}, ${card.segment_total || 0} segments`;
}

function _relabelCardHtml(tc) {
  const card = tc.result?.relabel;
  if (!card || !card.token) return '';
  const resolved = tc.result.relabelState || '';
  const sessions = (card.sessions || []).slice(0, 8);
  const more = (card.sessions || []).length - sessions.length;
  let list = '';
  for (const s of sessions) {
    const when = (s.started_at || '').slice(0, 10);
    list += `<li>${escapeHtml(s.title || 'Untitled')}`
          + (when ? ` <span class="relabel-when">${escapeHtml(when)}</span>` : '')
          + ` <span class="relabel-when">${s.key_count} label${s.key_count === 1 ? '' : 's'}, ${s.segment_count} segments</span></li>`;
  }
  if (more > 0) list += `<li class="relabel-when">and ${more} more meeting${more === 1 ? '' : 's'}</li>`;

  let warns = '';
  for (const w of card.warnings || []) {
    warns += `<div class="relabel-warn">${escapeHtml(w)}</div>`;
  }

  const resolvedNote = tc.result.relabelNote
    ? `<div class="relabel-note">${escapeHtml(tc.result.relabelNote)}</div>`
    : '';
  const actions = resolved
    ? `<div class="relabel-status ${resolved}">${escapeHtml(tc.result.relabelMessage || '')}</div>${resolvedNote}`
    : `<div class="relabel-actions">
         <button class="relabel-btn primary" onclick="_relabelConfirm(this)">Confirm</button>
         <button class="relabel-btn" onclick="_relabelCancel(this)">Cancel</button>
       </div>
       <div class="relabel-status"></div>`;

  return `<div class="relabel-card" data-token="${escapeHtml(card.token)}">
    <div class="relabel-head">Reassign ${escapeHtml(card.from_name)} to ${escapeHtml(card.to_name)}</div>
    <div class="relabel-sub">${escapeHtml(_relabelSummaryLine(card))}</div>
    <ul class="relabel-list">${list}</ul>
    ${warns}
    ${actions}
  </div>`;
}

/* When the model applies or cancels a plan itself (the user confirmed in chat),
   the earlier plan card must stop offering Confirm/Cancel and show the outcome. */
function _syncRelabelCardFromTool(d) {
  const applied = d && d.relabel_applied;
  if (applied && applied.token) {
    const keys = applied.key_count || 0, sessions = applied.session_count || 0;
    const note = (!applied.summaries_queued && sessions > 0)
      ? 'Summaries were not refreshed (a recording was active or summaries '
        + 'are disabled); regenerate them from the meeting when convenient.'
      : '';
    _relabelResolve(applied.token, 'applied',
      `Applied from chat: ${keys} speaker${keys === 1 ? '' : 's'} across `
      + `${sessions} meeting${sessions === 1 ? '' : 's'}`, note);
  }
  const cancelled = d && d.relabel_cancelled;
  if (cancelled && cancelled.token) {
    _relabelResolve(cancelled.token, 'cancelled', 'Cancelled in chat; nothing was changed');
  }
}

function _relabelResolve(token, stateName, message, note) {
  for (const tc of (state.chatToolCalls || [])) {
    if (tc.result?.relabel?.token === token) {
      tc.result.relabelState = stateName;
      tc.result.relabelMessage = message;
      tc.result.relabelNote = note || '';
    }
  }
  const card = document.querySelector(`.relabel-card[data-token="${token}"]`);
  if (!card) return;
  card.querySelectorAll('button').forEach(b => { b.disabled = true; });
  card.querySelector('.relabel-actions')?.remove();
  const status = card.querySelector('.relabel-status');
  if (status) {
    status.className = `relabel-status ${stateName}`;
    status.textContent = message;
  }
  card.querySelector('.relabel-note')?.remove();
  if (note && status) {
    const el = document.createElement('div');
    el.className = 'relabel-note';
    el.textContent = note;
    status.after(el);
  }
}

async function _relabelConfirm(btn) {
  const card = btn.closest('.relabel-card');
  const token = card?.dataset.token;
  if (!token) return;
  card.querySelectorAll('button').forEach(b => { b.disabled = true; });
  const status = card.querySelector('.relabel-status');
  if (status) status.textContent = 'Applying...';
  try {
    const res = await fetch('/api/speakers/relabel/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || 'Could not apply the reassignment');
    const keys = d.key_count || 0;
    const sessions = d.session_count || 0;
    // Summaries are skipped while a recording runs or when auto-summary
    // is off, so say so rather than leaving stale speaker names in them.
    const note = (!d.summaries_queued && sessions > 0)
      ? 'Summaries were not refreshed (a recording was active or summaries '
        + 'are disabled); regenerate them from the meeting when convenient.'
      : '';
    _relabelResolve(token, 'applied',
      `Applied: ${keys} speaker${keys === 1 ? '' : 's'} across `
      + `${sessions} meeting${sessions === 1 ? '' : 's'}`, note);
  } catch (e) {
    _relabelResolve(token, 'failed', e.message || 'Could not apply the reassignment');
  }
}

async function _relabelCancel(btn) {
  const card = btn.closest('.relabel-card');
  const token = card?.dataset.token;
  if (!token) return;
  card.querySelectorAll('button').forEach(b => { b.disabled = true; });
  try {
    await fetch('/api/speakers/relabel/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
  } catch {}
  _relabelResolve(token, 'cancelled', 'Cancelled. Nothing was changed.');
}

function _toolInputSummary(name, input) {
  if (name === 'get_screenshot' && input?.timestamp != null) {
    return `at ${Number(input.timestamp).toFixed(1)}s`;
  }
  if (name === 'search_transcripts' && input?.query) return `"${input.query}"`;
  if (name === 'semantic_search' && input?.query) return `"${input.query}"`;
  if (name === 'get_session_detail' && input?.session_id) return input.session_id.substring(0, 8) + '...';
  if (name === 'list_speakers') return 'Voice Library';
  if (name === 'get_speaker_history' && input?.speaker_name) return `"${input.speaker_name}"`;
  if (name === 'inspect_context_codebase') return input?.path || input?.root_id || 'selected folders';
  if (name === 'list_context_files') return input?.path || input?.root_id || 'selected folders';
  if (name === 'read_context_file') return input?.path || 'file';
  if (name === 'search_context_files') return input?.query ? `"${input.query}"` : 'searching files';
  if (name === 'get_context_file_info') return input?.path || 'path';
  if (name === 'run_context_shell') return input?.command || 'command';
  if (name === 'web_search' && input?.query) return `"${input.query}"`;
  if (name === 'web_search') return 'searching…';
  if (name === 'plan_speaker_relabel') {
    const scope = input?.scope === 'library' ? 'whole library' : 'this meeting';
    return `"${input?.from_name || '?'}" to "${input?.to_name || '?'}" (${scope})`;
  }
  if (name === 'apply_speaker_relabel') return 'after your confirmation';
  if (name === 'cancel_speaker_relabel') return 'plan token';
  return JSON.stringify(input || {});
}

function _setAssistantProcessing(msgWrap, active, label) {
  const proc = msgWrap.querySelector('.chat-processing');
  if (!proc) return;
  if (active && label) {
    proc.querySelector('.chat-processing-label').textContent = label;
  }
  proc.classList.toggle('active', active);
}

function appendUserBubble(text, attachments, contextFolders) {
  const el = document.getElementById('chat-messages');
  el.querySelector('.empty-hint')?.remove();
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg user';
  wrap.innerHTML = `
    <div class="chat-msg-header">
      <div class="chat-avatar user">You</div>
      <span class="chat-role">You</span>
    </div>
    <div class="chat-msg-body">${escapeHtml(text)}</div>`;
  el.appendChild(wrap);
  if (attachments?.length) {
    _renderBubbleAttachments(wrap.querySelector('.chat-msg-body'), attachments);
  }
  if (contextFolders?.length) {
    _renderBubbleContextFolders(wrap.querySelector('.chat-msg-body'), contextFolders);
  }
  // User sent a message - reset flag and force-scroll so the response is visible.
  _chatAtBottom = true;
  scrollChatToBottom();
}

function scrollChatToBottom(force = false) {
  if (!force && !_chatAtBottom) return;
  const el = document.getElementById('chat-messages');
  el.scrollTop = el.scrollHeight;
}

/* ── Image lightbox ────────────────────────────────────────────────────────── */
document.addEventListener('click', e => {
  const img = e.target.closest('.chat-msg-body img');
  if (!img) return;
  _openImageLightbox(img.src);
});

function _openImageLightbox(src) {
  const overlay = document.createElement('div');
  overlay.className = 'img-lightbox';
  overlay.innerHTML = `
    <button class="img-lightbox-close" title="Close">&times;</button>
    <img src="${src}" alt="Screenshot preview" draggable="false">`;
  document.body.appendChild(overlay);

  const img = overlay.querySelector('img');
  let scale = 1, tx = 0, ty = 0;
  let dragState = null;

  function _applyTransform() {
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    img.classList.toggle('zoomed', scale > 1.05);
  }

  // Click backdrop to close
  overlay.addEventListener('click', e => {
    if (e.target === overlay) { _cleanup(); overlay.remove(); }
  });
  overlay.querySelector('.img-lightbox-close').addEventListener('click', () => {
    _cleanup(); overlay.remove();
  });

  // Double-click to toggle between fit and 1:1
  img.addEventListener('dblclick', e => {
    e.stopPropagation();
    if (scale > 1.05) {
      scale = 1; tx = 0; ty = 0;
    } else {
      scale = 2;
    }
    _applyTransform();
  });

  // Mouse wheel to zoom in/out
  overlay.addEventListener('wheel', e => {
    e.preventDefault();
    const delta = e.deltaY > 0 ? 0.9 : 1.1;
    const newScale = Math.min(10, Math.max(0.5, scale * delta));
    // Zoom toward cursor position
    const rect = img.getBoundingClientRect();
    const cx = e.clientX - rect.left - rect.width / 2;
    const cy = e.clientY - rect.top - rect.height / 2;
    tx += cx * (1 - delta);
    ty += cy * (1 - delta);
    scale = newScale;
    _applyTransform();
  }, { passive: false });

  // Drag to pan
  img.addEventListener('mousedown', e => {
    if (scale <= 1.05) return;
    e.preventDefault();
    dragState = { startX: e.clientX, startY: e.clientY, tx, ty };
  });
  const _onMove = e => {
    if (!dragState) return;
    tx = dragState.tx + (e.clientX - dragState.startX);
    ty = dragState.ty + (e.clientY - dragState.startY);
    _applyTransform();
  };
  const _onUp = () => { dragState = null; };
  document.addEventListener('mousemove', _onMove);
  document.addEventListener('mouseup', _onUp);

  // Escape to close
  const _onKey = e => { if (e.key === 'Escape') { _cleanup(); overlay.remove(); } };
  document.addEventListener('keydown', _onKey);

  function _cleanup() {
    document.removeEventListener('mousemove', _onMove);
    document.removeEventListener('mouseup', _onUp);
    document.removeEventListener('keydown', _onKey);
  }
}

async function clearChat() {
  if (!state.sessionId) return;
  // Cancel any in-flight response
  if (state.aiChatBusy) {
    await stopChatGeneration();
    state.aiChatBusy = false;
    _setChatBusy(false);
  }
  state.chatCursor = null;
  state.chatBuffer = '';
  state.chatToolCalls = [];
  document.getElementById('chat-messages').innerHTML =
    '<p class="empty-hint">Chat cleared.</p>';
  await fetch('/api/chat/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId }),
  }).catch(() => {});
}

/* ── Chat / Summary / Title system prompts (global + per-session overrides) ── */
let _builtinChatPrompt = '';        // fetched once; used for "Reset to original"
let _builtinSummaryPrompt = '';
let _builtinTitlePrompt = '';
let _sessionChatPrompt = null;      // current session's chat override (null = none)
let _sessionSummaryPrompt = null;   // current session's summary override (null = none)

async function _fetchBuiltinChatPrompt() {
  if (_builtinChatPrompt) return _builtinChatPrompt;
  try {
    const r = await fetch('/api/chat/default-prompt').then(r => r.json());
    _builtinChatPrompt = r.prompt || '';
  } catch { _builtinChatPrompt = ''; }
  return _builtinChatPrompt;
}

async function _fetchBuiltinSummaryPrompt() {
  if (_builtinSummaryPrompt) return _builtinSummaryPrompt;
  try {
    const r = await fetch('/api/summary/default-prompt').then(r => r.json());
    _builtinSummaryPrompt = r.prompt || '';
  } catch { _builtinSummaryPrompt = ''; }
  return _builtinSummaryPrompt;
}

async function _fetchBuiltinTitlePrompt() {
  if (_builtinTitlePrompt) return _builtinTitlePrompt;
  try {
    const r = await fetch('/api/title/default-prompt').then(r => r.json());
    _builtinTitlePrompt = r.prompt || '';
  } catch { _builtinTitlePrompt = ''; }
  return _builtinTitlePrompt;
}

// Pre-populate the System Prompts textareas: if the user has saved a custom
// version, show it; otherwise show the built-in so the textarea is never blank.
async function _syncGlobalChatPromptUI() {
  const ta = document.getElementById('global-chat-prompt');
  if (!ta) return;
  const saved = _prefs.chat_system_prompt;
  if (typeof saved === 'string' && saved.length) {
    ta.value = saved;
  } else {
    ta.value = await _fetchBuiltinChatPrompt();
  }
  _refreshPromptSectionTags();
}

async function _syncGlobalSummaryPromptUI() {
  const ta = document.getElementById('global-summary-prompt');
  if (!ta) return;
  const saved = _prefs.summary_system_prompt;
  if (typeof saved === 'string' && saved.length) {
    ta.value = saved;
  } else {
    ta.value = await _fetchBuiltinSummaryPrompt();
  }
  _refreshPromptSectionTags();
}

async function _syncGlobalTitlePromptUI() {
  const ta = document.getElementById('global-title-prompt');
  if (!ta) return;
  const saved = _prefs.title_system_prompt;
  if (typeof saved === 'string' && saved.length) {
    ta.value = saved;
  } else {
    ta.value = await _fetchBuiltinTitlePrompt();
  }
  _refreshPromptSectionTags();
}

async function resetGlobalChatPrompt() {
  const ta = document.getElementById('global-chat-prompt');
  if (!ta) return;
  ta.value = await _fetchBuiltinChatPrompt();
  _markPromptsDirty();
}

async function resetGlobalSummaryPrompt() {
  const ta = document.getElementById('global-summary-prompt');
  if (!ta) return;
  ta.value = await _fetchBuiltinSummaryPrompt();
  _markPromptsDirty();
}

async function resetGlobalTitlePrompt() {
  const ta = document.getElementById('global-title-prompt');
  if (!ta) return;
  ta.value = await _fetchBuiltinTitlePrompt();
  _markPromptsDirty();
}

/* Tag each collapsed prompt section with a "Custom" chip when the saved
 * value differs from the built-in, so users can see at a glance which
 * sections they've customized without having to expand each one. */
function _refreshPromptSectionTags() {
  const entries = [
    ['global-chat-prompt-tag',    _prefs.chat_system_prompt],
    ['global-summary-prompt-tag', _prefs.summary_system_prompt],
    ['global-title-prompt-tag',   _prefs.title_system_prompt],
  ];
  for (const [id, val] of entries) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (typeof val === 'string' && val.length) {
      el.textContent = 'Custom';
      el.classList.add('custom');
    } else {
      el.textContent = '';
      el.classList.remove('custom');
    }
  }
}

function _markPromptsDirty() {
  const st = document.getElementById('prompts-save-status');
  if (st) { st.textContent = 'Unsaved changes'; st.classList.add('dirty'); }
}

async function saveSystemPrompts() {
  const chatTa    = document.getElementById('global-chat-prompt');
  const summaryTa = document.getElementById('global-summary-prompt');
  const titleTa   = document.getElementById('global-title-prompt');
  const chatRaw    = chatTa    ? chatTa.value    : '';
  const summaryRaw = summaryTa ? summaryTa.value : '';
  const titleRaw   = titleTa   ? titleTa.value   : '';

  // If the textarea matches the built-in verbatim, persist an empty string
  // so the backend keeps using the latest built-in (in case it ever changes).
  const builtinChat    = await _fetchBuiltinChatPrompt();
  const builtinSummary = await _fetchBuiltinSummaryPrompt();
  const builtinTitle   = await _fetchBuiltinTitlePrompt();
  const chatVal    = (chatRaw    === builtinChat)    ? '' : chatRaw;
  const summaryVal = (summaryRaw === builtinSummary) ? '' : summaryRaw;
  const titleVal   = (titleRaw   === builtinTitle)   ? '' : titleRaw;

  _prefs.chat_system_prompt    = chatVal;
  _prefs.summary_system_prompt = summaryVal;
  _prefs.title_system_prompt   = titleVal;

  const btn = document.getElementById('prompts-save-btn');
  const st  = document.getElementById('prompts-save-status');
  if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
  try {
    await fetch('/api/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chat_system_prompt:    chatVal,
        summary_system_prompt: summaryVal,
        title_system_prompt:   titleVal,
      }),
    });
    _syncGlobalChatPromptUI();
    _syncGlobalSummaryPromptUI();
    _syncGlobalTitlePromptUI();
    if (st) { st.textContent = 'Saved'; st.classList.remove('dirty'); st.classList.add('saved'); }
    setTimeout(() => { if (st) { st.textContent = ''; st.classList.remove('saved'); } }, 1800);
  } catch (e) {
    if (st) { st.textContent = 'Save failed'; st.classList.add('dirty'); }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
  }
}

// Wire dirty-tracking on the System Prompts tab textareas (auto-save was removed).
document.addEventListener('DOMContentLoaded', () => {
  ['global-chat-prompt', 'global-summary-prompt', 'global-title-prompt'].forEach(id => {
    const ta = document.getElementById(id);
    if (ta) ta.addEventListener('input', _markPromptsDirty);
  });
});

/* ── Session-level summary system prompt (inline widget) ──────────────────── */

async function loadSessionSummarySystemPrompt() {
  if (!state.sessionId) {
    _sessionSummaryPrompt = null;
    const ta = document.getElementById('summary-system-prompt');
    if (ta) ta.value = '';
    _refreshSummarySystemPromptUI();
    return;
  }
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/summary-prompt`).then(r => r.json());
    _sessionSummaryPrompt = r.session_prompt || null;
    const ta = document.getElementById('summary-system-prompt');
    if (ta) ta.value = _sessionSummaryPrompt || '';
    // Cache the effective default text (custom default if set, else built-in)
    // for the "Load default" button.
    const loadBtn = document.getElementById('summary-prompt-load-default-btn');
    if (loadBtn) {
      const customDefault = (r.global_prompt || '').trim();
      loadBtn._cachedDefault = customDefault || (r.default_prompt || '');
    }
    _refreshSummarySystemPromptUI();
  } catch {
    _sessionSummaryPrompt = null;
    _refreshSummarySystemPromptUI();
  }
}

function _refreshSummarySystemPromptUI() {
  const chip = document.getElementById('summary-prompt-source-chip');
  const toggleBtn = document.getElementById('summary-prompt-toggle');
  // Highlight the gear icon when a per-session summary override is active
  if (toggleBtn) {
    toggleBtn.classList.toggle('has-override', !!_sessionSummaryPrompt);
  }
  if (!chip) return;
  if (_sessionSummaryPrompt) {
    chip.textContent = 'Session override';
    chip.classList.add('custom');
  } else {
    chip.textContent = 'Default';
    chip.classList.remove('custom');
  }
}

async function loadDefaultIntoSessionSummary() {
  const ta = document.getElementById('summary-system-prompt');
  if (!ta) return;
  const btn = document.getElementById('summary-prompt-load-default-btn');
  let text = (btn && btn._cachedDefault) || (_prefs.summary_system_prompt || '');
  if (!text) text = await _fetchBuiltinSummaryPrompt();
  ta.value = text;
}

async function saveSessionSummaryPrompt() {
  if (!state.sessionId) return;
  const ta = document.getElementById('summary-system-prompt');
  const value = ta ? ta.value : '';
  try {
    await fetch(`/api/sessions/${state.sessionId}/summary-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: value }),
    });
    _sessionSummaryPrompt = value.trim() ? value : null;
    _refreshSummarySystemPromptUI();
    // Brief confirmation flash on the chip
    const chip = document.getElementById('summary-prompt-source-chip');
    if (chip) {
      const orig = chip.textContent;
      chip.textContent = 'Saved';
      chip.classList.add('saved-flash');
      setTimeout(() => {
        chip.classList.remove('saved-flash');
        _refreshSummarySystemPromptUI();
      }, 1200);
    }
  } catch (e) {
    console.error('Failed to save session summary prompt', e);
  }
}

async function clearSessionSummaryPrompt() {
  if (!state.sessionId) return;
  try {
    await fetch(`/api/sessions/${state.sessionId}/summary-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: null }),
    });
    _sessionSummaryPrompt = null;
    const ta = document.getElementById('summary-system-prompt');
    if (ta) ta.value = '';
    _refreshSummarySystemPromptUI();
  } catch (e) {
    console.error('Failed to clear session summary prompt', e);
  }
}

/* ── Chapters (AI topic markers) ─────────────────────────────────────────── */

let _chapters = [];                 // current session's chapters [{id, start_time, title}]
let _chaptersModalOpen = false;
let _chaptersBusy = false;
let _chapterTicksWired = false;
let _chapterTipEl = null;

/** Set the current session's chapters and refresh every surface. */
function setSessionChapters(list) {
  _chapters = (Array.isArray(list) ? list.slice() : [])
    .sort((a, b) => (a.start_time || 0) - (b.start_time || 0));
  // Update each surface independently and isolate failures: a throw in one
  // (e.g. the minimap when collapsed, or ticks before audio metadata loads)
  // must never stop the transcript headings from re-rendering in real time.
  try { renderTranscriptChapterHeadings(); } catch (e) { console.warn('[chapters] headings render failed', e); }
  try { renderChapterTicks(); }              catch (e) { console.warn('[chapters] ticks render failed', e); }
  try { renderMinimapChapters(); }           catch (e) { console.warn('[chapters] minimap render failed', e); }
  if (_chaptersModalOpen) {
    try { renderChaptersList(); } catch (e) { console.warn('[chapters] list render failed', e); }
  }
}

/* ── Modal ─────────────────────────────────────────────────────────────── */

function openChaptersManager() {
  _chaptersModalOpen = true;
  document.getElementById('chapters-overlay').classList.remove('hidden');
  _chaptersSwitchTab('list');
  renderChaptersList();
  loadChaptersTuning();
}

function closeChaptersManager() {
  _chaptersModalOpen = false;
  document.getElementById('chapters-overlay').classList.add('hidden');
}

function _chaptersSwitchTab(name) {
  const overlay = document.getElementById('chapters-overlay');
  if (!overlay) return;
  overlay.querySelectorAll('.chapters-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.chTab === name));
  overlay.querySelectorAll('.chapters-body').forEach(b => {
    b.hidden = (b.dataset.chView !== name);
  });
}

function renderChaptersList() {
  const list = document.getElementById('chapters-list');
  if (!list) return;
  const countEl = document.getElementById('chapters-count');
  if (countEl) countEl.textContent = _chapters.length
    ? `${_chapters.length} chapter${_chapters.length === 1 ? '' : 's'}` : '';
  const addBtn = document.getElementById('chapters-add-btn');
  if (addBtn) {
    addBtn.disabled = !_playbackActive;
    addBtn.title = _playbackActive
      ? 'Add a chapter at the current playback position'
      : 'Play the recording to add a chapter at a position';
  }
  if (!_chapters.length) {
    list.innerHTML = `
      <div class="chapters-empty">
        <i class="fa-solid fa-bookmark chapters-empty-icon"></i>
        <div class="chapters-empty-title">No chapters yet</div>
        <div class="chapters-empty-sub">Generate topic markers from the transcript, or add them manually.</div>
        <button type="button" class="chapters-btn chapters-btn-primary" onclick="regenerateChapters()">
          <i class="fa-solid fa-wand-magic-sparkles"></i> Generate chapters
        </button>
      </div>`;
    return;
  }
  list.innerHTML = _chapters.map(ch => `
    <div class="chapters-row" data-cid="${ch.id}">
      <button type="button" class="chapters-row-time" onclick="seekToTime(${ch.start_time})" title="Jump to this point">${escapeHtml(fmtTime(ch.start_time))}</button>
      <input class="chapters-row-title" type="text" value="${escapeHtml(ch.title || '')}"
             onblur="renameChapter(${ch.id}, this)"
             onkeydown="if(event.key==='Enter'){event.preventDefault();this.blur();}" />
      <button type="button" class="chapters-row-del icon-btn" onclick="deleteChapter(${ch.id})" title="Delete chapter"><i class="fa-solid fa-trash-can"></i></button>
    </div>`).join('');
}

async function regenerateChapters() {
  if (!state.sessionId) { flashStatus('Open a session first'); return; }
  _setChaptersBusy(true);
  try {
    const r = await fetch('/api/chapters/generate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId }),
    }).then(r => r.json());
    if (r.error) { _setChaptersBusy(false); flashStatus(r.error); }
    // Success arrives asynchronously via chapters_busy / chapters_updated SSE.
  } catch (_) {
    _setChaptersBusy(false);
    flashStatus('Chapter generation failed');
  }
}

async function addChapterAtPlayhead() {
  if (!state.sessionId || !_playbackActive) return;
  const t = _playbackAudio.currentTime || 0;
  const title = 'New chapter';
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/chapters`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_time: t, title }),
    }).then(r => r.json());
    if (r.chapters) {
      setSessionChapters(r.chapters);
      // Focus the freshly-added row so the user can rename it immediately.
      requestAnimationFrame(() => {
        const rows = document.querySelectorAll('#chapters-list .chapters-row');
        for (const row of rows) {
          const inp = row.querySelector('.chapters-row-title');
          if (inp && inp.value === title) { inp.focus(); inp.select(); break; }
        }
      });
    }
  } catch (_) { flashStatus('Could not add chapter'); }
}

async function renameChapter(cid, inputEl) {
  const ch = _chapters.find(c => c.id === cid);
  if (!ch) return;
  const title = (inputEl.value || '').trim();
  if (!title) { inputEl.value = ch.title || ''; return; }  // don't allow empty
  if (title === ch.title) return;                          // no change
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/chapters/${cid}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    }).then(r => r.json());
    if (r.chapters) setSessionChapters(r.chapters);
  } catch (_) { flashStatus('Could not rename chapter'); }
}

async function deleteChapter(cid) {
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/chapters/${cid}`, {
      method: 'DELETE',
    }).then(r => r.json());
    if (r.chapters) setSessionChapters(r.chapters);
  } catch (_) { flashStatus('Could not delete chapter'); }
}

function _setChaptersBusy(busy) {
  _chaptersBusy = busy;
  // Show the generating state on the Regenerate button itself so the header
  // (title + description) never reflows. The button has a fixed min-width.
  const regen = document.getElementById('chapters-regen-btn');
  if (regen) {
    regen.disabled = busy;
    regen.innerHTML = busy
      ? '<i class="fa-solid fa-spinner fa-spin"></i> Generating…'
      : '<i class="fa-solid fa-wand-magic-sparkles"></i> Regenerate';
  }
  document.getElementById('chapters-btn')?.classList.toggle('busy', busy);
}

/* ── Playback-bar ticks + tooltip ────────────────────────────────────────── */

function _ensureChapterTipWiring() {
  if (_chapterTicksWired) return;
  const layer = document.getElementById('playback-chapters');
  if (!layer) return;
  layer.addEventListener('mouseover', e => {
    const tick = e.target.closest('.chapter-tick');
    if (tick) _showChapterTip(tick, tick.dataset.label || '');
  });
  layer.addEventListener('mouseout', e => {
    if (e.target.closest('.chapter-tick')) _hideChapterTip();
  });
  layer.addEventListener('click', e => {
    const tick = e.target.closest('.chapter-tick');
    if (!tick) return;
    const t = parseFloat(tick.dataset.t);
    if (isFinite(t)) seekToTime(t);
  });
  _chapterTicksWired = true;
}

function renderChapterTicks() {
  const layer = document.getElementById('playback-chapters');
  if (!layer) return;
  const dur = _playbackAudio && _playbackAudio.duration;
  if (!_playbackActive || !isFinite(dur) || dur <= 0 || !_chapters.length) {
    layer.innerHTML = '';
    return;
  }
  _ensureChapterTipWiring();
  layer.innerHTML = _chapters.map(ch => {
    const frac = Math.max(0, Math.min(1, (ch.start_time || 0) / dur));
    // Align the tick to the slider-thumb centre: the thumb (12px) travels
    // inset by 6px on each side, so map the fraction across (100% - 12px).
    const label = `${fmtTime(ch.start_time)} · ${ch.title || ''}`;
    return `<span class="chapter-tick" style="left:calc(6px + ${frac.toFixed(5)} * (100% - 12px))"
              data-t="${ch.start_time}" data-label="${escapeHtml(label)}"></span>`;
  }).join('');
}

function _showChapterTip(tick, label) {
  if (!_chapterTipEl) {
    _chapterTipEl = document.createElement('div');
    _chapterTipEl.className = 'chapter-tip';
    document.body.appendChild(_chapterTipEl);
  }
  _chapterTipEl.textContent = label;
  _chapterTipEl.style.display = 'block';
  const r = tick.getBoundingClientRect();
  const tr = _chapterTipEl.getBoundingClientRect();
  let left = r.left + r.width / 2 - tr.width / 2;
  left = Math.max(6, Math.min(left, window.innerWidth - tr.width - 6));
  _chapterTipEl.style.left = left + 'px';
  _chapterTipEl.style.top = Math.max(6, r.top - tr.height - 8) + 'px';
}

function _hideChapterTip() {
  if (_chapterTipEl) _chapterTipEl.style.display = 'none';
}

/* ── Minimap markers ─────────────────────────────────────────────────────── */

function renderMinimapChapters() {
  const container = document.getElementById('transcript-minimap');
  if (!container) return;
  container.querySelectorAll('.minimap-chapter').forEach(el => el.remove());
  if (!_minimapActive || !_chapters.length || !_segmentTimes.length) return;
  const transcript = document.getElementById('transcript');
  if (!transcript) return;
  const scrollH = transcript.scrollHeight;
  const mapH = container.clientHeight;
  if (scrollH <= 0 || mapH <= 0) return;
  for (const ch of _chapters) {
    let idx = -1;
    for (let i = 0; i < _segmentTimes.length; i++) {
      if (_segmentTimes[i].start <= (ch.start_time || 0)) idx = i; else break;
    }
    if (idx < 0) idx = 0;
    const segEl = _segmentTimes[idx].el;
    if (!segEl) continue;
    const yPos = (segEl.offsetTop / scrollH) * mapH;
    const marker = document.createElement('div');
    marker.className = 'minimap-chapter';
    marker.style.top = yPos + 'px';
    marker.title = ch.title || '';
    container.appendChild(marker);
  }
}

/* ── Inline transcript headings ──────────────────────────────────────────── */

function _makeChapterHeading(ch) {
  const h = document.createElement('div');
  h.className = 'transcript-chapter-heading';
  h.dataset.chapterTime = ch.start_time;
  h.title = 'Jump to chapter';
  h.innerHTML = '<i class="fa-solid fa-bookmark tch-icon"></i>' +
                '<span class="tch-title"></span><span class="tch-time"></span>';
  h.querySelector('.tch-title').textContent = ch.title || '';
  h.querySelector('.tch-time').textContent = fmtTime(ch.start_time);
  h.addEventListener('click', () => seekToTime(ch.start_time));
  return h;
}

/**
 * Insert a pronounced heading into the transcript before the first segment at
 * or after each chapter's timestamp. Idempotent: clears and re-places on every
 * call, so it's safe to run after (re)render, chapter change, or filter change.
 */
function renderTranscriptChapterHeadings() {
  const container = document.getElementById('transcript');
  if (!container) return;
  container.querySelectorAll('.transcript-chapter-heading').forEach(el => el.remove());
  if (!_chapters.length) return;
  const segs = container.querySelectorAll('.transcript-segment[data-start]');
  if (!segs.length) return;

  let ci = 0;
  for (const seg of segs) {
    const st = parseFloat(seg.dataset.start);
    if (!isFinite(st)) continue;
    while (ci < _chapters.length && (_chapters[ci].start_time || 0) <= st + 0.05) {
      container.insertBefore(_makeChapterHeading(_chapters[ci]), seg);
      ci++;
    }
    if (ci >= _chapters.length) break;
  }
  while (ci < _chapters.length) {   // chapters after the last timed segment
    container.appendChild(_makeChapterHeading(_chapters[ci]));
    ci++;
  }

  // Under an active filter, hide any heading whose section has no visible
  // segment (segments are hidden with style.display='none').
  container.querySelectorAll('.transcript-chapter-heading').forEach(h => {
    let node = h.nextElementSibling, visible = false;
    while (node && !node.classList.contains('transcript-chapter-heading')) {
      if (node.classList.contains('transcript-segment') && node.style.display !== 'none') {
        visible = true; break;
      }
      node = node.nextElementSibling;
    }
    h.style.display = visible ? '' : 'none';
  });
}

/* ── Tuning tab ──────────────────────────────────────────────────────────── */

async function loadChaptersTuning() {
  const autoTog = document.getElementById('chapters-auto-toggle');
  if (autoTog) autoTog.checked = _prefs.chapters_auto !== false;
  _highlightGranularity(_prefs.chapters_granularity || 'balanced');

  const ta = document.getElementById('chapters-system-prompt');
  const chip = document.getElementById('chapters-prompt-source-chip');
  const loadBtn = document.getElementById('chapters-prompt-load-default-btn');
  const defTog = document.getElementById('chapters-prompt-default-toggle');
  if (!state.sessionId) {
    if (ta) ta.value = '';
    if (chip) { chip.textContent = 'Default'; chip.classList.remove('custom'); }
    if (defTog) defTog.checked = false;
    return;
  }
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/chapters-prompt`).then(r => r.json());
    if (ta) ta.value = r.session_prompt || '';
    if (chip) {
      const isOverride = !!r.session_prompt;
      chip.textContent = isOverride ? 'Session override' : 'Default';
      chip.classList.toggle('custom', isOverride);
    }
    if (loadBtn) loadBtn._cachedDefault = (r.global_prompt || '').trim() || (r.default_prompt || '');
    if (defTog) defTog.checked = !!(r.global_prompt || '').trim();
  } catch (_) {}
}

function _highlightGranularity(g) {
  document.querySelectorAll('#chapters-granularity .chapters-gran-opt').forEach(b =>
    b.classList.toggle('active', b.dataset.gran === g));
}

function _setChaptersGranularity(g) {
  _highlightGranularity(g);
  savePref('chapters_granularity', g);
}

function _onChaptersAutoToggle(checked) {
  savePref('chapters_auto', !!checked);
}

async function saveChaptersSystemPrompt() {
  if (!state.sessionId) return;
  const ta = document.getElementById('chapters-system-prompt');
  const value = ta ? ta.value : '';
  try {
    await fetch(`/api/sessions/${state.sessionId}/chapters-prompt`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: value }),
    });
    // If "use as default" is checked, also persist the global default.
    const defTog = document.getElementById('chapters-prompt-default-toggle');
    if (defTog && defTog.checked) savePref('chapters_system_prompt', value);
    const chip = document.getElementById('chapters-prompt-source-chip');
    if (chip) {
      chip.textContent = 'Saved';
      chip.classList.add('saved-flash');
      setTimeout(() => { chip.classList.remove('saved-flash'); loadChaptersTuning(); }, 1200);
    }
  } catch (_) { flashStatus('Could not save chapters prompt'); }
}

async function clearChaptersSystemPrompt() {
  if (!state.sessionId) return;
  try {
    await fetch(`/api/sessions/${state.sessionId}/chapters-prompt`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: null }),
    });
    const ta = document.getElementById('chapters-system-prompt');
    if (ta) ta.value = '';
    loadChaptersTuning();
  } catch (_) {}
}

async function loadDefaultIntoChaptersPrompt() {
  const ta = document.getElementById('chapters-system-prompt');
  if (!ta) return;
  const btn = document.getElementById('chapters-prompt-load-default-btn');
  let text = (btn && btn._cachedDefault) || '';
  if (!text) {
    try { text = (await fetch('/api/chapters/default-prompt').then(r => r.json())).prompt || ''; }
    catch (_) {}
  }
  ta.value = text;
}

function _onChaptersPromptDefaultToggle(checked) {
  const ta = document.getElementById('chapters-system-prompt');
  savePref('chapters_system_prompt', checked ? (ta ? ta.value : '') : '');
}

/* ── Session-level override: chat-header gear icon + dialog ──────────────── */

async function refreshSessionChatPromptBadge() {
  // Keep the gear icon highlighted when an override is active.
  const btn = document.getElementById('chat-prompt-btn');
  if (!btn || !state.sessionId) { if (btn) btn.classList.remove('has-override'); _sessionChatPrompt = null; return; }
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/chat-prompt`).then(r => r.json());
    _sessionChatPrompt = r.session_prompt || null;
    btn.classList.toggle('has-override', !!_sessionChatPrompt);
    btn.title = _sessionChatPrompt ? 'Session system prompt (custom)' : 'Session system prompt';
  } catch {
    btn.classList.remove('has-override');
    _sessionChatPrompt = null;
  }
}

async function openChatPromptDialog() {
  if (!state.sessionId) return;
  const overlay = document.getElementById('chat-prompt-overlay');
  const ta      = document.getElementById('session-chat-prompt');
  const chip    = document.getElementById('chat-prompt-source-chip');
  const loadGlobalBtn = document.getElementById('chat-prompt-load-global-btn');
  if (!overlay || !ta) return;

  // Fetch all three layers in parallel
  await _fetchBuiltinChatPrompt();
  let r;
  try {
    r = await fetch(`/api/sessions/${state.sessionId}/chat-prompt`).then(r => r.json());
  } catch {
    r = { session_prompt: null, global_prompt: '', default_prompt: _builtinChatPrompt };
  }
  _sessionChatPrompt = r.session_prompt || null;

  // Seed the textarea with the active prompt (or blank if falling back)
  ta.value = _sessionChatPrompt || '';

  // Source indicator: session / global / built-in
  if (chip) {
    if (_sessionChatPrompt) {
      chip.textContent = 'Session override';
      chip.classList.add('custom');
    } else if ((r.global_prompt || '').trim()) {
      chip.textContent = 'Global default';
      chip.classList.remove('custom');
    } else {
      chip.textContent = 'Built-in default';
      chip.classList.remove('custom');
    }
  }
  // Disable the "Load global" button when there's no global to load
  if (loadGlobalBtn) {
    loadGlobalBtn.disabled = !(r.global_prompt || '').trim();
    loadGlobalBtn._cachedGlobal = r.global_prompt || '';
  }

  overlay.classList.remove('hidden');
  setTimeout(() => ta.focus(), 50);
}

function closeChatPromptDialog() {
  document.getElementById('chat-prompt-overlay')?.classList.add('hidden');
}

function loadBuiltinIntoSession() {
  const ta = document.getElementById('session-chat-prompt');
  if (ta) ta.value = _builtinChatPrompt || '';
}

function loadGlobalIntoSession() {
  const ta = document.getElementById('session-chat-prompt');
  const btn = document.getElementById('chat-prompt-load-global-btn');
  if (!ta) return;
  const txt = (btn && btn._cachedGlobal) || _prefs.chat_system_prompt || '';
  ta.value = txt;
}

async function saveSessionChatPrompt() {
  if (!state.sessionId) return;
  const ta = document.getElementById('session-chat-prompt');
  const value = ta ? ta.value : '';
  try {
    await fetch(`/api/sessions/${state.sessionId}/chat-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: value }),
    });
    _sessionChatPrompt = value.trim() ? value : null;
    closeChatPromptDialog();
    refreshSessionChatPromptBadge();
  } catch (e) {
    console.error('Failed to save session chat prompt', e);
  }
}

async function clearSessionChatPrompt() {
  if (!state.sessionId) return;
  try {
    await fetch(`/api/sessions/${state.sessionId}/chat-prompt`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt: null }),
    });
    _sessionChatPrompt = null;
    const ta = document.getElementById('session-chat-prompt');
    if (ta) ta.value = '';
    closeChatPromptDialog();
    refreshSessionChatPromptBadge();
  } catch (e) {
    console.error('Failed to clear session chat prompt', e);
  }
}

// Esc closes the dialog
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const ov = document.getElementById('chat-prompt-overlay');
    if (ov && !ov.classList.contains('hidden')) closeChatPromptDialog();
  }
});

/* ── Chat local context folders/toolbox ─────────────────────────────────── */
const _CHAT_CONTEXT_STORAGE_PREFIX = 'ma-chat-context-folders:';
let _chatContextFolders = [];  // [{id, name, path}]
let _chatContextRestorePromise = null;

function _chatContextStorageKey(sessionId = state.sessionId) {
  return sessionId ? `${_CHAT_CONTEXT_STORAGE_PREFIX}${sessionId}` : null;
}

function _contextFolderName(folder) {
  if (folder?.name) return folder.name;
  const path = folder?.path || '';
  return path.split(/[\\/]/).filter(Boolean).pop() || path || 'folder';
}

function _normalizeContextFolders(folders) {
  const out = [];
  const seen = new Set();
  for (const folder of folders || []) {
    if (!folder || !folder.path) continue;
    const key = folder.path.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      id: folder.id || '',
      name: _contextFolderName(folder),
      path: folder.path,
    });
    if (out.length >= 8) break;
  }
  return out;
}

function _saveChatContextFoldersForSession(sessionId = state.sessionId) {
  const key = _chatContextStorageKey(sessionId);
  if (!key) return;
  const payload = _chatContextFolders.map(f => ({ id: f.id, name: _contextFolderName(f), path: f.path }));
  try { localStorage.setItem(key, JSON.stringify(payload)); } catch (_) {}
}

function _setChatContextFolders(folders, { persist = true } = {}) {
  _chatContextFolders = _normalizeContextFolders(folders);
  _renderChatContextFolders();
  if (persist) _saveChatContextFoldersForSession();
}

function _syncChatToolboxState() {
  const btn = document.getElementById('chat-toolbox-btn');
  const fileBadge = document.getElementById('chat-file-count');
  const folderBadge = document.getElementById('chat-folder-count');
  let attachmentCount = 0;
  try { attachmentCount = _pendingAttachments?.length || 0; } catch (_) {}
  const folderCount = _chatContextFolders.length;
  if (btn) btn.classList.toggle('has-items', attachmentCount + folderCount > 0);
  if (fileBadge) {
    fileBadge.textContent = String(attachmentCount);
    fileBadge.classList.toggle('hidden', attachmentCount < 1);
  }
  if (folderBadge) {
    folderBadge.textContent = String(folderCount);
    folderBadge.classList.toggle('hidden', folderCount < 1);
  }
}

function _renderChatContextFolders() {
  const preview = document.getElementById('chat-context-preview');
  _syncChatToolboxState();
  if (!preview) return;
  preview.innerHTML = '';
  if (!_chatContextFolders.length) {
    preview.classList.add('hidden');
    return;
  }
  preview.classList.remove('hidden');
  for (const folder of _chatContextFolders) {
    const item = document.createElement('div');
    item.className = 'chat-context-item';
    item.title = folder.path || _contextFolderName(folder);
    item.innerHTML = `
      <i class="fa-solid fa-folder-open"></i>
      <span class="chat-context-name"></span>
      <button class="chat-context-remove" title="Remove folder" type="button">
        <i class="fa-solid fa-xmark"></i>
      </button>`;
    item.querySelector('.chat-context-name').textContent = _contextFolderName(folder);
    item.querySelector('.chat-context-remove').addEventListener('click', () => {
      _setChatContextFolders(_chatContextFolders.filter(f => f.path !== folder.path));
    });
    preview.appendChild(item);
  }
}

function _loadChatContextFoldersForSession(sessionId = state.sessionId) {
  if (!sessionId) {
    _chatContextRestorePromise = null;
    _setChatContextFolders([], { persist: false });
    return;
  }
  const key = _chatContextStorageKey(sessionId);
  let cached = [];
  try {
    const raw = key ? localStorage.getItem(key) : null;
    cached = raw ? _normalizeContextFolders(JSON.parse(raw)) : [];
  } catch (_) {
    cached = [];
  }
  _setChatContextFolders(cached, { persist: false });
  _chatContextRestorePromise = cached.length
    ? _restoreChatContextFoldersForSession(sessionId, cached)
    : null;
}

async function _restoreChatContextFoldersForSession(sessionId, folders) {
  try {
    const resp = await fetch('/api/chat/context-folder/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folders }),
    });
    const data = await resp.json().catch(() => ({}));
    if (sessionId !== state.sessionId) return;
    if (!resp.ok) {
      _setChatContextFolders([], { persist: true });
      return;
    }
    _setChatContextFolders(data.folders || [], { persist: true });
  } catch (_) {
    if (sessionId === state.sessionId) _syncChatToolboxState();
  }
}

function openChatToolboxMenu() {
  const btn = document.getElementById('chat-toolbox-btn');
  const menu = document.getElementById('chat-toolbox-menu');
  if (!btn || !menu) return;
  menu.classList.remove('hidden');
  btn.classList.add('open');
  btn.setAttribute('aria-expanded', 'true');
  _syncChatToolboxState();
}

function closeChatToolboxMenu() {
  const btn = document.getElementById('chat-toolbox-btn');
  const menu = document.getElementById('chat-toolbox-menu');
  if (!btn || !menu) return;
  menu.classList.add('hidden');
  btn.classList.remove('open');
  btn.setAttribute('aria-expanded', 'false');
}

function toggleChatToolboxMenu(e) {
  e?.preventDefault();
  e?.stopPropagation();
  const menu = document.getElementById('chat-toolbox-menu');
  if (!menu || menu.classList.contains('hidden')) openChatToolboxMenu();
  else closeChatToolboxMenu();
}

function chooseChatFiles() {
  closeChatToolboxMenu();
  document.getElementById('chat-file-input')?.click();
}

async function pickChatContextFolder() {
  closeChatToolboxMenu();
  const btn = document.getElementById('chat-toolbox-btn');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  try {
    const initial = _chatContextFolders[_chatContextFolders.length - 1]?.path || '';
    const resp = await fetch('/api/chat/context-folder/pick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      uiToast({ message: data.error || 'Could not add context folder.', kind: 'error' });
      return;
    }
    if (!data.selected) return;
    const selected = data.selected;
    if (_chatContextFolders.some(f => f.path === selected.path)) {
      flashStatus('Folder already added');
      return;
    }
    _setChatContextFolders([..._chatContextFolders, selected]);
  } catch (e) {
    uiToast({ message: `Error: ${e.message || e}`, kind: 'error' });
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('click', e => {
  if (!e.target.closest?.('.chat-toolbox-wrap')) closeChatToolboxMenu();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeChatToolboxMenu();
});

let _chatRequestId = null;  // tracks the active chat request for cancellation

async function sendMessage() {
  if (state.aiChatBusy || !state.sessionId) return;
  const input    = document.getElementById('chat-input');
  const question = input.value.trim();
  const attachments = [..._pendingAttachments];
  if (_chatContextRestorePromise) {
    await _chatContextRestorePromise.catch(() => {});
    _chatContextRestorePromise = null;
  }
  const contextFolders = [..._chatContextFolders];
  if (!question && !attachments.length) return;

  input.value = '';
  _autogrowChatInput();
  appendUserBubble(question, attachments, contextFolders);
  _clearAttachments();
  state.aiChatBusy = true;
  _setChatBusy(true);

  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      session_id: state.sessionId,
      question,
      attachments: attachments.map(a => ({id: a.id, filename: a.filename, mime: a.mime, size: a.size, stored: a.stored})),
      context_roots: contextFolders.map(f => f.id),
    }),
  });
  if (resp.ok) {
    const data = await resp.json();
    _chatRequestId = data.request_id;
  } else {
    const err = await resp.json().catch(() => ({}));
    const bubble = createAssistantBubble();
    bubble.style.display = '';
    bubble.textContent = `Error: ${err.error || 'Unknown error'}`;
    state.aiChatBusy = false;
    _setChatBusy(false);
  }
}

async function stopChatGeneration() {
  await fetch('/api/chat/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: _chatRequestId }),
  }).catch(() => {});
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function _setChatBusy(busy) {
  const sendBtn = document.getElementById('send-btn');
  const stopBtn = document.getElementById('stop-btn');
  sendBtn.disabled = busy;
  if (busy) {
    sendBtn.classList.add('hidden');
    stopBtn.classList.remove('hidden');
  } else {
    sendBtn.classList.remove('hidden');
    stopBtn.classList.add('hidden');
    _chatRequestId = null;
  }
}

/* ── Auto-grow textarea ───────────────────────────────────────────────────── */
function _autogrowChatInput() {
  const ta = document.getElementById('chat-input');
  ta.style.height = 'auto';
  ta.style.height = ta.scrollHeight + 'px';
  // If content exceeds max-height, allow scrolling; otherwise hide overflow
  ta.style.overflowY = ta.scrollHeight > ta.clientHeight ? 'auto' : 'hidden';
}

/* ── Copy helpers ─────────────────────────────────────────────────────────── */
function _copyChatMsg(btn) {
  const body = btn.closest('.chat-msg')?.querySelector('.chat-msg-body');
  if (!body) return;
  const html = body.innerHTML;
  const plain = body.innerText;
  navigator.clipboard.write([
    new ClipboardItem({
      'text/html': new Blob([html], { type: 'text/html' }),
      'text/plain': new Blob([plain], { type: 'text/plain' }),
    }),
  ]).catch(() => navigator.clipboard.writeText(plain)).then(() => {
    btn.classList.add('copied');
    btn.querySelector('i').className = 'fa-solid fa-check';
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.querySelector('i').className = 'fa-regular fa-copy';
    }, 1500);
  });
}

function _addCodeCopyButtons(container) {
  container.querySelectorAll('pre').forEach(pre => {
    if (pre.querySelector('.code-copy-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'code-copy-btn';
    btn.innerHTML = '<i class="fa-regular fa-copy"></i> Copy';
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const code = pre.querySelector('code')?.innerText || pre.innerText;
      navigator.clipboard.writeText(code).then(() => {
        btn.classList.add('copied');
        btn.innerHTML = '<i class="fa-solid fa-check"></i> Copied';
        setTimeout(() => {
          btn.classList.remove('copied');
          btn.innerHTML = '<i class="fa-regular fa-copy"></i> Copy';
        }, 1500);
      });
    });
    pre.appendChild(btn);
  });
}

// Backward-compat alias used by older callers
function setSendBusy(busy) { _setChatBusy(busy); }

/* ── Attachments ──────────────────────────────────────────────────────────── */
let _pendingAttachments = [];  // [{id, filename, mime, size, stored, localUrl?}]

const _IMAGE_MIMES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);

function _handleFileSelect(files) {
  closeChatToolboxMenu();
  for (const f of files) _uploadAttachment(f);
}

async function _uploadAttachment(file) {
  const preview = document.getElementById('chat-attach-preview');
  preview.classList.remove('hidden');

  // Create preview item
  const item = document.createElement('div');
  item.className = 'chat-attach-item uploading';
  const isImage = file.type.startsWith('image/');
  if (isImage) {
    const img = document.createElement('img');
    img.src = URL.createObjectURL(file);
    item.appendChild(img);
  } else {
    const icon = document.createElement('i');
    icon.className = 'fa-solid fa-file';
    icon.style.fontSize = '14px';
    item.appendChild(icon);
  }
  const nameSpan = document.createElement('span');
  nameSpan.className = 'attach-name';
  nameSpan.textContent = file.name;
  item.appendChild(nameSpan);
  preview.appendChild(item);

  // Upload
  const fd = new FormData();
  fd.append('file', file);
  try {
    const resp = await fetch('/api/chat/upload', { method: 'POST', body: fd });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      item.classList.add('upload-error');
      item.title = err.error || 'Upload failed';
      item.classList.remove('uploading');
      setTimeout(() => { item.remove(); _refreshAttachPreview(); }, 3000);
      return;
    }
    const meta = await resp.json();
    meta.localUrl = isImage ? URL.createObjectURL(file) : null;
    _pendingAttachments.push(meta);
    item.classList.remove('uploading');
    item.dataset.attachId = meta.id;
    _syncChatToolboxState();

    // Add remove button
    const removeBtn = document.createElement('button');
    removeBtn.className = 'attach-remove';
    removeBtn.innerHTML = '<i class="fa-solid fa-xmark"></i>';
    removeBtn.addEventListener('click', () => {
      _pendingAttachments = _pendingAttachments.filter(a => a.id !== meta.id);
      item.remove();
      _refreshAttachPreview();
    });
    item.appendChild(removeBtn);
  } catch {
    item.classList.add('upload-error');
    item.classList.remove('uploading');
    setTimeout(() => { item.remove(); _refreshAttachPreview(); }, 3000);
  }
}

function _refreshAttachPreview() {
  const preview = document.getElementById('chat-attach-preview');
  if (!preview.children.length) preview.classList.add('hidden');
  _syncChatToolboxState();
}

function _clearAttachments() {
  _pendingAttachments = [];
  const preview = document.getElementById('chat-attach-preview');
  preview.innerHTML = '';
  preview.classList.add('hidden');
  _syncChatToolboxState();
}

function _renderBubbleContextFolders(bodyEl, folders) {
  if (!folders || !folders.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'chat-bubble-context-folders';
  for (const folder of folders) {
    const chip = document.createElement('span');
    chip.className = 'chat-bubble-context-folder';
    chip.title = folder.path || _contextFolderName(folder);
    chip.innerHTML = '<i class="fa-solid fa-folder-open"></i> ';
    const name = document.createElement('span');
    name.textContent = _contextFolderName(folder);
    chip.appendChild(name);
    wrap.appendChild(chip);
  }
  bodyEl.insertBefore(wrap, bodyEl.firstChild);
}

/** Render attachment thumbnails/links inside a chat bubble body element. */
function _renderBubbleAttachments(bodyEl, attachments) {
  if (!attachments || !attachments.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'chat-bubble-attachments';
  for (const att of attachments) {
    const url = `/api/chat/attachment/${att.stored}`;
    if (_IMAGE_MIMES.has(att.mime) || (att.mime && att.mime.startsWith('image/'))) {
      const img = document.createElement('img');
      img.src = url;
      img.alt = att.filename;
      img.title = att.filename;
      img.addEventListener('click', () => window.open(url, '_blank'));
      wrap.appendChild(img);
    } else {
      const link = document.createElement('a');
      link.className = 'chat-bubble-attachment-file';
      link.href = url;
      link.target = '_blank';
      link.innerHTML = `<i class="fa-solid fa-file"></i> ${escapeHtml(att.filename)}`;
      wrap.appendChild(link);
    }
  }
  bodyEl.insertBefore(wrap, bodyEl.firstChild);
}

// ── Drag-and-drop overlay on the full chat pane ───────────────────────────────
{
  const chatCol = document.querySelector('.col-chat');
  const overlay = document.getElementById('chat-drop-overlay');
  const hint    = document.getElementById('chat-drop-hint');

  if (chatCol && overlay) {
    let dragCounter = 0;
    const NOTES_MIME = 'application/x-notes-embed';

    const hasFiles = e =>
      Array.from(e.dataTransfer?.types || []).includes('Files');
    const hasNotesEmbed = e =>
      Array.from(e.dataTransfer?.types || []).includes(NOTES_MIME);
    const isAttachable = e => hasFiles(e) || hasNotesEmbed(e);

    const showOverlay = (e) => {
      if (hasNotesEmbed(e) && !hasFiles(e)) {
        if (hint) hint.textContent = 'Drop to attach from notes';
      } else {
        const count = e.dataTransfer?.items?.length;
        if (hint && count) {
          hint.textContent = count === 1 ? '1 file ready to attach' : `${count} files ready to attach`;
        } else if (hint) {
          hint.textContent = 'Images · PDFs · text files';
        }
      }
      overlay.setAttribute('aria-hidden', 'false');
      overlay.classList.add('active');
    };

    const hideOverlay = () => {
      overlay.classList.remove('active');
      overlay.setAttribute('aria-hidden', 'true');
    };

    chatCol.addEventListener('dragenter', e => {
      if (!isAttachable(e)) return;
      e.preventDefault();
      if (++dragCounter === 1) showOverlay(e);
    });

    chatCol.addEventListener('dragleave', e => {
      if (!chatCol.contains(e.relatedTarget)) {
        dragCounter = 0;
        hideOverlay();
      }
    });

    chatCol.addEventListener('dragover', e => {
      if (isAttachable(e)) {
        e.preventDefault();
        if (e.dataTransfer) {
          e.dataTransfer.dropEffect = hasFiles(e) ? 'copy' : 'copy';
        }
      }
    });

    chatCol.addEventListener('drop', e => {
      // Notes embed drag takes priority - the dataTransfer carries our
      // internal MIME with URL/meta we can re-upload as a chat attachment.
      const notesRaw = (() => {
        try { return e.dataTransfer?.getData(NOTES_MIME) || ''; }
        catch (_) { return ''; }
      })();
      if (notesRaw) {
        e.preventDefault();
        dragCounter = 0;
        hideOverlay();
        let payload = null;
        try { payload = JSON.parse(notesRaw); } catch (_) {}
        if (payload) _attachNotesEmbedToChat(payload);
        return;
      }
      e.preventDefault();
      dragCounter = 0;
      hideOverlay();
      if (e.dataTransfer?.files?.length) _handleFileSelect(e.dataTransfer.files);
    });
  }
}

/* Re-upload a notes attachment (or inline image) as a chat attachment. The
 * notes pane stores files at /api/sessions/<sid>/notes/attachments/<stored>;
 * the chat pane needs its own copy under /api/chat/attachment/<stored>.
 * Fetching + re-uploading keeps the two systems decoupled and means each
 * chat message references a stable, independent server-side file. */
async function _attachNotesEmbedToChat(payload) {
  if (!state.sessionId) {
    flashStatus('Open a session first');
    return;
  }
  const url = payload?.url;
  if (!url) {
    flashStatus("Couldn't read attachment");
    return;
  }
  // Show an immediate "uploading" preview so the user gets feedback.
  const preview = document.getElementById('chat-attach-preview');
  preview?.classList.remove('hidden');
  const placeholder = document.createElement('div');
  placeholder.className = 'chat-attach-item uploading';
  const isImage = (payload.kind === 'image') ||
    (payload.mime && payload.mime.startsWith('image/'));
  if (isImage) {
    const img = document.createElement('img');
    // For server-stored attachments the URL works directly. For pasted-but-
    // unsaved blob URLs the image still renders since blobs survive across
    // panes within the same document.
    img.src = url;
    placeholder.appendChild(img);
  } else {
    const icon = document.createElement('i');
    icon.className = 'fa-solid fa-file';
    icon.style.fontSize = '14px';
    placeholder.appendChild(icon);
  }
  const nameSpan = document.createElement('span');
  nameSpan.className = 'attach-name';
  nameSpan.textContent = payload.filename || (isImage ? 'image' : 'file');
  placeholder.appendChild(nameSpan);
  preview?.appendChild(placeholder);

  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error('Fetch failed: ' + resp.status);
    const blob = await resp.blob();
    const filename = payload.filename || _filenameFromUrl(url) ||
      (isImage ? 'image.png' : 'file');
    const file = new File([blob], filename, {
      type: payload.mime || blob.type || 'application/octet-stream',
    });
    // Hand off to the existing upload path. It builds its own preview tile,
    // so we tear down the placeholder once it spawns its replacement.
    placeholder.remove();
    _refreshAttachPreview();
    _uploadAttachment(file);
  } catch (err) {
    console.error('Notes→chat attach failed', err);
    placeholder.classList.add('upload-error');
    placeholder.classList.remove('uploading');
    placeholder.title = 'Attach failed: ' + (err.message || 'unknown');
    setTimeout(() => { placeholder.remove(); _refreshAttachPreview(); }, 3000);
  }
}

function _filenameFromUrl(url) {
  try {
    const u = new URL(url, window.location.origin);
    const last = u.pathname.split('/').filter(Boolean).pop() || '';
    return decodeURIComponent(last);
  } catch (_) {
    return '';
  }
}

// ── Paste images from clipboard ──────────────────────────────────────────────
document.getElementById('chat-input')?.addEventListener('paste', e => {
  const items = e.clipboardData?.items;
  if (!items) return;
  let hasFile = false;
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      e.preventDefault();
      hasFile = true;
      const file = item.getAsFile();
      if (file) _uploadAttachment(file);
    }
  }
  // Trim leading/trailing whitespace from pasted text
  if (!hasFile) {
    const ta = e.target;
    setTimeout(() => { ta.value = ta.value.trim(); _autogrowChatInput(); }, 0);
  }
});

/* ── Past sessions ───────────────────────────────────────────────────────── */
async function loadSession(sessionId) {
  // Opening the session already loaded (the live one included) just brings the
  // workspace forward: it is never re-rendered underneath a live recording.
  if (sessionId === state.sessionId) {
    Views.show('session', { url: '/session?id=' + sessionId });
    return;
  }

  if (_cleanupState && _cleanupState.dirty) {
    if (!await uiConfirm({ title: 'Discard staged cleanup changes?', message: 'You have unsaved speaker cleanup changes in this meeting. Switching meetings discards them.', confirmLabel: 'Discard and switch', danger: true })) return;
    _cleanupState.dirty = false;
  }

  if (state.isRecording) {
    const entry = _sidebarAllSessions.find(s => s.id === sessionId);
    const label = (entry && entry.title) || 'this recording';
    const go = await uiConfirm({
      title: 'Stop the current recording?',
      message: `Stop the current recording and open ${label}?`,
      confirmLabel: 'Stop and open',
      danger: true,
    });
    if (!go) return;
    await fetch('/api/recording/stop', { method: 'POST' });
  }

  const gen = ++_loadGeneration;  // cancel any in-flight chunked render

  const data = await fetch(`/api/sessions/${sessionId}`).then(r => r.json());
  if (data.error) {
    // Session not found - clean up URL and show a brief status message
    history.replaceState(null, '', location.pathname);
    flashStatus('Session not found');
    return;
  }
  if (gen !== _loadGeneration) return;  // another load started while we were fetching

  clearAll();
  _setPlaybackEditTrimmed(!!data.has_trim_backup);
  _setSessionSplitBackup(!!data.has_split_backup, data.split_group_id || null);
  state.sessionId     = sessionId;
  state.isViewingPast = true;
  _loadChatContextFoldersForSession(sessionId);
  Views.show('session', { url: '/session?id=' + sessionId });
  updateRecordBtn();
  _loadPaneVisible(sessionId);
  refreshSessionChatPromptBadge();
  _revealSessionInSidebar(sessionId);
  // _revealSessionInSidebar only re-renders if it actually expanded
  // anything; refresh the highlight unconditionally so the new active
  // session's folders get the class even when nothing was collapsed.
  _updateActiveFolderHighlights();

  if (data.speaker_profiles?.length) {
    data.speaker_profiles.forEach(profile => applySpeakerProfileUpdate(profile));
  } else if (data.speaker_labels) {
    Object.entries(data.speaker_labels).forEach(([speakerKey, name]) => {
      applySpeakerProfileUpdate({ speaker_key: speakerKey, name });
    });
  }

  // Load voice library links for badge indicators
  _sessionLinks = {};
  fetch(`/api/fingerprint/sessions/${sessionId}/links`)
    .then(r => r.json())
    .then(links => { _sessionLinks = links || {}; _updateLinkedBadges(); })
    .catch(() => {});

  // Load pending speaker suggestions
  _fpLoadSuggestions();

  // Render the transcript WITHOUT blocking the lighter Summary/Chat panes below.
  // On a long session the chunked render spans many frames; awaiting it here used
  // to leave Summary and Chat empty until the whole transcript finished. Instead
  // we kick it off and let the cheap panes paint in this same synchronous tick,
  // so all three come alive together and the transcript fills in progressively.
  // (No `await` runs between the generation check above and the pane renders, so
  // the cancellation guard still holds - a newer load can't interleave here.)
  const segments = data.segments || [];
  const CHUNK = 150;  // segments per animation frame

  // The pending search highlight needs the transcript DOM, so it runs once the
  // transcript is actually rendered (covers both the async and sync branches).
  const _afterTranscriptRender = () => {
    if (_pendingSearchHighlight) {
      const hl = _pendingSearchHighlight;
      _pendingSearchHighlight = null;
      requestAnimationFrame(() => _executeSearchHighlight(hl));
    }
  };

  if (segments.length > CHUNK) {
    // Show loading hint and render in async chunks (does NOT block the panes below)
    const transcriptEl = document.getElementById('transcript');
    transcriptEl.innerHTML = '';
    const loadingHint = document.createElement('p');
    loadingHint.className = 'empty-hint loading-hint';
    loadingHint.textContent = `Loading ${segments.length} segments…`;
    transcriptEl.appendChild(loadingHint);

    _bulkLoading = true;
    _renderSegmentsChunked(segments, CHUNK, loadingHint, gen).then(completed => {
      _bulkLoading = false;
      if (!completed) return;  // load was cancelled by a newer loadSession call
      _finishBulkLoad();
      _afterTranscriptRender();
    });
  } else {
    // Small transcript - render synchronously (fast enough)
    segments.forEach(s =>
      appendTranscript(s.text, s.source_override || s.source || 'loopback', s.start_time, s.end_time,
                       s.id, s.label_override, s.source_override ? s.source : null)
    );
    _afterTranscriptRender();
  }

  // Restore summary prompt for this session (fire-and-forget so it can't wedge a
  // network round-trip in front of the Summary/Chat render).
  const storedPrompt = localStorage.getItem('summary-prompt:' + sessionId) || '';
  _applyPromptText(storedPrompt);
  fetch('/api/custom-prompt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ custom_prompt: storedPrompt }),
  }).catch(() => {});

  // Load this session's chapters (playback-bar ticks render once the audio
  // metadata loads; the modal list + minimap markers update immediately).
  setSessionChapters(data.chapters || []);

  // Show playback bar if audio is available
  if (data.has_audio) initPlayback(sessionId);
  if (data.has_video) initVideo(sessionId, data.video_offset);
  // Keep the cleanup video toggle in sync with the freshly-loaded session's
  // video availability (e.g. switching to a no-video session with the speaker
  // manager left open).
  if (typeof _cleanupVideoSyncToggleBtn === 'function') _cleanupVideoSyncToggleBtn();
  // Invalidate any cached cleanup clusters from the previous session - otherwise
  // reopening the Cleanup tab would show the old session's speakers.
  if (_cleanupState && _cleanupState.sessionId !== sessionId) {
    _cleanupState = null;
    try { _cleanupStopPlayback(); _cleanupClosePicker(); } catch (_) {}
    _cleanupSelectedKeys = new Set();
    _cleanupSelAnchor = null;
    _cleanupExpandedKeys = new Set();
    _cleanupShowHeatmap = false;
    const overlay = document.getElementById('speaker-manager-overlay');
    if (_cleanupActiveTab === 'cleanup' && overlay && !overlay.classList.contains('hidden')) {
      loadSpeakerClusters();  // manager is open on Cleanup - refetch for the new session now
    }
  }

  if (data.summary) {
    const sumEl = document.getElementById('summary');
    sumEl.innerHTML = renderMd(data.summary);
    highlightCode('#summary');
    linkifyTimestamps(sumEl);
  }

  // Resume if a summary is currently streaming for this session
  const activeStream = _summaryStreams[sessionId];
  if (activeStream && activeStream.streaming) {
    state.summaryStreaming = true;
    state.summaryBuffer = activeStream.buffer;
    const sumEl = document.getElementById('summary');
    if (activeStream.buffer) {
      sumEl.innerHTML = renderMd(_linkifyTimestampsInMd(activeStream.buffer));
    } else {
      sumEl.innerHTML = '';
    }
    state.summaryCursor = sumEl;
    const badge = document.getElementById('summary-badge');
    badge.textContent = activeStream.mode === 'updating' ? 'updating…' : 'generating…';
    badge.classList.remove('hidden');
  } else if (activeStream && activeStream.mode) {
    // Busy but not yet streaming (e.g. waiting for AI response)
    const badge = document.getElementById('summary-badge');
    badge.textContent = activeStream.mode === 'updating' ? 'updating…' : 'generating…';
    badge.classList.remove('hidden');
  }

  // Restore rich-text notes (Quill Delta). data.notes may be null/missing.
  _notesApplyForSession(sessionId, data.notes || null);

  if (data.chat_messages?.length) {
    document.getElementById('chat-messages').innerHTML = '';
    for (const m of data.chat_messages) {
      const atts = m.attachments ? (typeof m.attachments === 'string' ? JSON.parse(m.attachments) : m.attachments) : null;
      if (m.role === 'user') {
        appendUserBubble(m.content, atts);
      } else {
        const b = createAssistantBubble();
        // Restored messages: show body/actions, hide processing indicator
        const wrap = b.closest('.chat-msg');
        if (wrap) {
          const proc = wrap.querySelector('.chat-processing');
          if (proc) proc.classList.remove('active');
          const actions = wrap.querySelector('.chat-msg-actions');
          if (actions) actions.style.display = '';
        }
        b.style.display = '';
        b.innerHTML = renderMd(m.content);
        linkifyTimestamps(b);
        // Restore tool-call widget if present
        const tcRaw = m.tool_calls;
        if (tcRaw) {
          const tcs = typeof tcRaw === 'string' ? JSON.parse(tcRaw) : tcRaw;
          if (tcs?.length && wrap) _renderToolWidget(wrap, tcs, true);
        }
      }
    }
    highlightCode('#chat-messages');
    _addCodeCopyButtons(document.getElementById('chat-messages'));
  }

  refreshSidebar();  // re-render to highlight active item
}

/**
 * Render transcript segments in chunks, yielding to the browser between batches
 * so the UI stays responsive. Returns a promise that resolves when all segments
 * are rendered.
 */
function _renderSegmentsChunked(segments, chunkSize, loadingHint, gen) {
  return new Promise(resolve => {
    let i = 0;
    function renderChunk() {
      if (gen !== _loadGeneration) { resolve(false); return; }  // cancelled
      const end = Math.min(i + chunkSize, segments.length);
      for (; i < end; i++) {
        const s = segments[i];
        appendTranscript(s.text, s.source_override || s.source || 'loopback',
                         s.start_time, s.end_time, s.id,
                         s.label_override, s.source_override ? s.source : null);
      }
      if (loadingHint && loadingHint.parentNode) {
        loadingHint.textContent = `Loading… ${i} / ${segments.length}`;
      }
      if (i < segments.length) {
        requestAnimationFrame(renderChunk);
      } else {
        if (loadingHint && loadingHint.parentNode) loadingHint.remove();
        resolve(true);
      }
    }
    requestAnimationFrame(renderChunk);
  });
}

/**
 * Run deferred per-segment operations once after bulk loading finishes.
 */
function _finishBulkLoad() {
  _tnExtendTimeRange();
  applyTranscriptFilter();
  // On a fresh load nothing is selected (clearAll cleared it), so this whole
  // O(N) per-segment querySelector pass would just toggle a class off on every
  // badge that never had it. Skip it when the selection is empty. (Keep the
  // unconditional calls on the real select/deselect paths - they must run with
  // an empty set to clear stale highlights.)
  if (_selectedSpeakerKeys.length) _highlightSelectedSpeakerBadges();
  if (!document.getElementById('speaker-manager-overlay')?.classList.contains('hidden')) {
    renderSpeakerManager();
  }
  _updateCollapseFabVisibility();
  _updateMinimapFabVisibility();
  _refreshMinimap(true);
}

/* ── App lifecycle (the sidebar footer App menu) ─────────────────────────── */

async function confirmShutdown() {
  closeMenu();
  if (!state.isRecording) { doShutdown(); return; }
  const confirmed = await uiConfirm({ title: 'Quit Meeting Assistant?', message: 'A recording is in progress. Quitting stops it and closes Meeting Assistant.', confirmLabel: 'Stop and quit', danger: true });
  if (confirmed) doShutdown();
}

async function doShutdown() {
  await fetch('/api/shutdown', { method: 'POST' }).catch(() => {});
  const screen = _showTransitionScreen('Meeting Assistant has quit', 'You can close this tab.');
  // Freeze the animation after a moment for a calm stopped state
  setTimeout(() => screen.stop(), 3000);
}

async function confirmRestart() {
  closeMenu();
  if (!state.isRecording) { doRestart(); return; }
  const confirmed = await uiConfirm({ title: 'Restart Meeting Assistant?', message: 'A recording is in progress. Restarting stops it first.', confirmLabel: 'Stop and restart' });
  if (confirmed) doRestart();
}

async function confirmUpdateRestart() {
  closeMenu();
  if (!state.isRecording) { doUpdateRestart(); return; }
  const confirmed = await uiConfirm({ title: 'Install the update and restart?', message: 'A recording is in progress. This stops it, pulls the latest update, and restarts.', confirmLabel: 'Stop and update' });
  if (confirmed) doUpdateRestart();
}

async function doUpdateRestart() {
  const screen = _showTransitionScreen('Updating & Restarting\u2026', 'The page will reload when the server is back.');
  try {
    const res = await fetch('/api/update/apply', { method: 'POST' });
    const data = await res.json();
    if (data.error) {
      screen.titleEl.textContent = 'Update failed';
      screen.subtitleEl.textContent = data.error;
      return;
    }
  } catch {}
  let attempts = 0;
  const poll = setInterval(async () => {
    if (++attempts > 60) {   // give up after ~2 min so we don't poll forever
      clearInterval(poll);
      screen.subtitleEl.textContent = 'Still waiting. Refresh the page once the server is back.';
      return;
    }
    try {
      const r = await fetch('/api/status', { signal: AbortSignal.timeout(2000) });
      if (r.ok) { clearInterval(poll); location.reload(); }
    } catch {}
  }, 2000);
}

function _showTransitionScreen(title, subtitle) {
  document.body.innerHTML = '';
  document.body.style.cssText = 'margin:0;overflow:hidden;background:var(--surface4, #0a0d10)';

  // Inject styles
  if (!document.getElementById('_ts_style')) {
    const style = document.createElement('style');
    style.id = '_ts_style';
    style.textContent = `
      @keyframes _ts_breathe {
        0%, 100% { opacity: .25; transform: scale(1) }
        50%      { opacity: .45; transform: scale(1.08) }
      }
      @keyframes _ts_fadein {
        from { opacity: 0; transform: translateY(8px) }
        to   { opacity: 1; transform: translateY(0) }
      }
      ._ts_wrap {
        position: fixed; inset: 0;
        display: flex; flex-direction: column;
        align-items: center; justify-content: center;
        font-family: system-ui, -apple-system, sans-serif;
      }
      ._ts_glow {
        position: absolute;
        width: 280px; height: 280px;
        border-radius: 50%;
        background: radial-gradient(circle, rgba(88,166,255,.12) 0%, transparent 70%);
        animation: _ts_breathe 4s ease-in-out infinite;
        pointer-events: none;
      }
      ._ts_logo {
        position: relative;
        width: 60px; height: 60px;
        margin-bottom: 28px;
        animation: _ts_breathe 4s ease-in-out infinite;
        filter: drop-shadow(0 0 14px rgba(88,166,255,.25));
      }
      ._ts_bar_wrap {
        display: flex; gap: 4px; align-items: center;
        height: 20px; margin-bottom: 28px;
        animation: _ts_fadein .5s ease .2s both;
      }
      ._ts_bar {
        width: 3px; border-radius: 1.5px;
        background: rgba(88,166,255,.5);
        animation: _ts_eq var(--d, 1s) ease-in-out var(--delay, 0s) infinite alternate;
      }
      @keyframes _ts_eq {
        0%   { height: var(--lo, 4px) }
        100% { height: var(--hi, 16px) }
      }
      ._ts_title {
        font-size: 17px; font-weight: 600; color: #e6edf3;
        margin: 0 0 8px; letter-spacing: .2px;
        animation: _ts_fadein .5s ease .1s both;
      }
      ._ts_sub {
        font-size: 13px; color: #8b949e; margin: 0;
        animation: _ts_fadein .5s ease .25s both;
      }
    `;
    document.head.appendChild(style);
  }

  const wrap = document.createElement('div');
  wrap.className = '_ts_wrap';

  // Small EQ-style bars
  const N = 5;
  let bars = '';
  for (let i = 0; i < N; i++) {
    const d  = (.7 + Math.random() * .6).toFixed(2);
    const dl = (i * .08).toFixed(2);
    const lo = 3 + Math.floor(Math.random() * 3);
    const hi = 10 + Math.floor(Math.random() * 8);
    bars += `<span class="_ts_bar" style="--d:${d}s;--delay:${dl}s;--lo:${lo}px;--hi:${hi}px"></span>`;
  }

  wrap.innerHTML = `
    <div class="_ts_glow"></div>
    <img class="_ts_logo" src="/static/images/logo.png" alt="">
    <div class="_ts_bar_wrap">${bars}</div>
    <p class="_ts_title">${title}</p>
    <p class="_ts_sub">${subtitle}</p>
  `;
  document.body.appendChild(wrap);

  return {
    stop: () => {
      wrap.querySelectorAll('._ts_bar').forEach(b => b.style.animationPlayState = 'paused');
      wrap.querySelector('._ts_logo').style.animationPlayState = 'paused';
      wrap.querySelector('._ts_glow').style.animationPlayState = 'paused';
    },
    titleEl: wrap.querySelector('._ts_title'),
    subtitleEl: wrap.querySelector('._ts_sub'),
  };
}

async function doRestart() {
  await fetch('/api/restart', { method: 'POST' }).catch(() => {});
  const screen = _showTransitionScreen('Restarting\u2026', 'The page will reload when the server is back.');
  let attempts = 0;
  const poll = setInterval(async () => {
    if (++attempts > 60) {   // give up after ~2 min so we don't poll forever
      clearInterval(poll);
      screen.subtitleEl.textContent = 'Still waiting. Refresh the page once the server is back.';
      return;
    }
    try {
      const r = await fetch('/api/status', { signal: AbortSignal.timeout(2000) });
      if (r.ok) { clearInterval(poll); location.reload(); }
    } catch {}
  }, 2000);
}

/* ── Misc helpers ────────────────────────────────────────────────────────── */
function clearAll() {
  _mgrStopVoice();   // stop any speaker voice sample when switching/clearing sessions
  _lastLiveSegId = 0;
  _speakerLabels = {};
  _speakerProfiles = {};
  _selectedSpeakerKeys = [];
  _speakerSelectionAnchor = null;
  _speakerDraftName = '';
  _speakerDraftColor = '';
  Object.keys(_speakerColors).forEach(k => delete _speakerColors[k]);
  _speakerColorIdx = 0;
  _transcriptSelectedSegs.clear();
  _transcriptSelectionAnchor = null;
  _closeBulkSpeakerPicker();
  _simIndex = null;
  _simIndexPromise = null;
  _fpRejected = new Set();
  _pendingSpeakerProfiles = [];
  _sessionLinks = {};
  // The Speakers modal is per-meeting: drop the remembered tab and the shared
  // status line so the next open lands deterministically for the new session.
  _speakerModalLastTab = null;
  _speakerModalStats = null;
  _speakerModalStatsSession = null;
  _mgrCommittedName = '';
  _transcriptFilter = { search: '', speakers: new Set(), timeMin: 0, timeMax: Infinity };
  _showNoise = false;
  _noiseSolo = false;
  _manualNoiseKeys = new Set();
  _showOriginalKeys = false;
  _setPlaybackEditTrimmed(false);
  _setSessionSplitBackup(false, null);
  const keysToggleBtn = document.getElementById('tn-pill-keys-toggle');
  if (keysToggleBtn) keysToggleBtn.classList.remove('active');
  _navState = { matches: [], currentIdx: -1 };
  const tnSearch = document.getElementById('tn-search-input');
  if (tnSearch) tnSearch.value = '';
  document.getElementById('transcript-filter-btn')?.classList.remove('open');
  document.getElementById('fp-bell-btn')?.classList.remove('open');
  document.getElementById('fp-notif-panel')?.classList.add('collapsed');
  document.getElementById('transcript-navigator')?.classList.add('collapsed');
  document.getElementById('analytics-panel')?.classList.add('collapsed');
  document.getElementById('analytics-btn')?.classList.remove('active');
  _updateFilterBtnState();
  closeSpeakerManager(true);
  closeChaptersManager();
  _chapters = [];
  renderChapterTicks();
  renderMinimapChapters();
  _setChaptersBusy(false);
  const bar = document.getElementById('transcript-selection-bar');
  if (bar) bar.classList.add('hidden');
  _syncPanelBottomRadius();
  _clearSegmentRegistry();
  document.getElementById('transcript').innerHTML =
    '<p class="empty-hint">Transcript will appear here once recording starts.</p>';
  document.getElementById('summary').innerHTML =
    '<p class="empty-hint">An auto-updating summary will appear here as the meeting progresses.</p>';
  document.getElementById('chat-messages').innerHTML =
    '<p class="empty-hint">Ask questions about the meeting here.</p>';
  // Reset the Notes editor (no save - clearAll is for navigating away from a session)
  if (typeof _notesResetForSessionChange === 'function') _notesResetForSessionChange();
  state.aiChatBusy = false;
  _setChatBusy(false);
  _clearAttachments();
  _setChatContextFolders([], { persist: false });
  _chatContextRestorePromise = null;
  closeChatToolboxMenu();
  state.summaryBuffer    = '';
  state.summaryStreaming  = false;
  state.summaryCursor    = null;
  document.getElementById('summary-badge')?.classList.add('hidden');
  state.chatBuffer       = '';
  state.chatToolCalls    = [];
  destroyPlayback();
}

/* ── Notes pane: Quill rich-text editor + inline attachments ────────────── */
let _quill = null;
let _notesSessionBound = null;       // session_id whose contents are in the editor
let _notesDirty = false;
let _notesSaveTimer = null;
let _notesSuppressChange = false;    // skip autosave during programmatic updates
let _notesPendingPayload = null;     // delta arriving before editor exists
let _notesPlaceholderSeq = 0;
let _notesNeedsBindOnInit = false;

function _ensureNotesEditor() {
  if (_quill) return _quill;
  const editorEl  = document.getElementById('notes-editor');
  const toolbarEl = document.getElementById('notes-toolbar');
  if (!editorEl || !toolbarEl) return null;
  if (typeof Quill === 'undefined') return null;  // CDN load failed; gracefully no-op

  _registerNoteFileBlot();
  _allowBlobImageUrls();

  _quill = new Quill(editorEl, {
    theme: 'snow',
    modules: {
      toolbar: { container: toolbarEl },
      history: { delay: 750, maxStack: 200, userOnly: true },
    },
    formats: [
      'header', 'bold', 'italic', 'underline', 'strike',
      'color', 'background', 'list', 'indent', 'blockquote',
      'code-block', 'code', 'link', 'align', 'image', 'note-file',
    ],
  });

  _quill.on('text-change', (_delta, _old, source) => {
    _refreshNotesEmptyHint();
    if (_notesSuppressChange) return;
    if (source !== 'user') return;
    _notesDirty = true;
    _scheduleNotesSave();
  });

  _registerNoteFileClipboardMatcher(_quill);
  _wireNotesDropAndPaste(editorEl);

  // Track focus so the document-level drop router knows when to claim drags
  // away from the session-import overlay.
  _quill.on('selection-change', range => {
    _notesHasFocus = range !== null;
  });

  // Image interactions: single click selects + shows resize handles,
  // double-click opens the lightbox.
  editorEl.addEventListener('click', e => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.tagName === 'IMG' && t.closest('.ql-editor')) {
      e.preventDefault();
      e.stopPropagation();
      _selectNotesImage(t);
    }
  });
  editorEl.addEventListener('dblclick', e => {
    const t = e.target;
    if (!(t instanceof HTMLElement)) return;
    if (t.tagName === 'IMG' && t.closest('.ql-editor')) {
      e.preventDefault();
      e.stopPropagation();
      _deselectNotesImage();
      if (typeof _openImageLightbox === 'function') _openImageLightbox(t.src);
    }
  });

  // Apply any payload that arrived before the editor was ready
  if (_notesNeedsBindOnInit) {
    _notesNeedsBindOnInit = false;
    if (_notesPendingPayload !== null) {
      _applyNotesPayload(_notesPendingPayload);
      _notesPendingPayload = null;
    }
    _notesSessionBound = state.sessionId || null;
  }
  _refreshNotesEmptyHint();
  return _quill;
}

/* ── Image resize (click to select, drag a corner handle to resize) ──────── */
let _notesActiveImage = null;
let _notesResizeOverlay = null;
let _notesResizeState = null;
let _notesResizeRaf = 0;

function _ensureNotesResizeOverlay() {
  if (_notesResizeOverlay) return _notesResizeOverlay;
  const ov = document.createElement('div');
  ov.className = 'notes-img-resize-overlay';
  ov.innerHTML = `
    <div class="notes-img-handle" data-corner="tl"></div>
    <div class="notes-img-handle" data-corner="tr"></div>
    <div class="notes-img-handle" data-corner="bl"></div>
    <div class="notes-img-handle" data-corner="br"></div>
    <div class="notes-img-size-label" id="notes-img-size-label"></div>`;
  document.body.appendChild(ov);
  ov.addEventListener('mousedown', _onNotesResizeHandleDown);
  _notesResizeOverlay = ov;
  return ov;
}

function _selectNotesImage(img) {
  _notesActiveImage = img;
  const ov = _ensureNotesResizeOverlay();
  ov.classList.add('active');
  _positionNotesResizeOverlay();
  // While active, keep overlay glued to the image (cheap rAF loop).
  if (!_notesResizeRaf) {
    const tick = () => {
      if (!_notesActiveImage) { _notesResizeRaf = 0; return; }
      _positionNotesResizeOverlay();
      _notesResizeRaf = requestAnimationFrame(tick);
    };
    _notesResizeRaf = requestAnimationFrame(tick);
  }
}

function _deselectNotesImage() {
  _notesActiveImage = null;
  if (_notesResizeOverlay) _notesResizeOverlay.classList.remove('active');
  if (_notesResizeRaf) { cancelAnimationFrame(_notesResizeRaf); _notesResizeRaf = 0; }
}

function _positionNotesResizeOverlay() {
  if (!_notesActiveImage || !_notesResizeOverlay) return;
  const r = _notesActiveImage.getBoundingClientRect();
  const ov = _notesResizeOverlay;
  ov.style.left = (r.left + window.scrollX) + 'px';
  ov.style.top = (r.top + window.scrollY) + 'px';
  ov.style.width = r.width + 'px';
  ov.style.height = r.height + 'px';
}

function _onNotesResizeHandleDown(e) {
  if (!_notesActiveImage) return;
  const corner = e.target?.dataset?.corner;
  if (!corner) return;
  e.preventDefault();
  e.stopPropagation();
  const img = _notesActiveImage;
  const startWidth  = img.clientWidth;
  const startHeight = img.clientHeight;
  const aspect = startHeight > 0 ? (startWidth / startHeight) : 1;
  _notesResizeState = {
    img, corner, startWidth, startHeight, aspect,
    startX: e.clientX, startY: e.clientY,
  };
  document.body.classList.add('notes-img-resizing');
  document.addEventListener('mousemove', _onNotesResizeMove);
  document.addEventListener('mouseup', _onNotesResizeUp, { once: true });
}

function _onNotesResizeMove(e) {
  const s = _notesResizeState;
  if (!s) return;
  // Right-side handles (tr/br) grow with positive dx; left-side (tl/bl) with negative dx.
  const sign = (s.corner === 'tr' || s.corner === 'br') ? 1 : -1;
  const dx = (e.clientX - s.startX) * sign;
  const newWidth = Math.max(40, Math.round(s.startWidth + dx));
  const newHeight = Math.max(20, Math.round(newWidth / s.aspect));
  // Quill's image format whitelists width/height attributes, so setting them
  // directly persists in getContents() - no formatText call needed.
  s.img.setAttribute('width', String(newWidth));
  s.img.setAttribute('height', String(newHeight));
  const lbl = document.getElementById('notes-img-size-label');
  if (lbl) lbl.textContent = `${newWidth} × ${newHeight}`;
}

function _onNotesResizeUp() {
  document.removeEventListener('mousemove', _onNotesResizeMove);
  document.body.classList.remove('notes-img-resizing');
  if (_notesResizeState) {
    _notesDirty = true;
    _scheduleNotesSave();
  }
  _notesResizeState = null;
}

// Click outside / Escape deselects.
document.addEventListener('mousedown', e => {
  if (!_notesActiveImage) return;
  const ov = _notesResizeOverlay;
  if (ov && ov.contains(e.target)) return;
  if (e.target === _notesActiveImage) return;
  _deselectNotesImage();
}, true);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _notesActiveImage) _deselectNotesImage();
});

/* Quill's default Image blot sanitizes URLs to one of {http, https, data,
 * blob}, but in practice blob: URLs get rejected and replaced with the
 * "no-op" `//:0` placeholder - which renders as the browser's broken-image
 * icon. We use blob URLs as the temporary src while an upload is in flight,
 * so we override sanitize to pass them through. (data: and the server's
 * /api/... paths still work as before.) */
function _allowBlobImageUrls() {
  if (window._noteImageSanitizePatched) return;
  if (typeof Quill === 'undefined') return;
  try {
    const Image = Quill.import('formats/image');
    Image.sanitize = function (url) {
      if (typeof url !== 'string') return '//:0';
      // Permit anything with a usable protocol or a relative/absolute path.
      if (/^(https?:|data:|blob:|\/)/i.test(url)) return url;
      return '//:0';
    };
    window._noteImageSanitizePatched = true;
  } catch (_) {
    // CDN load failed or API changed - fall through; worst case is a
    // momentary broken-image icon, which we already had.
  }
}

/* Teach Quill's clipboard module to round-trip our custom note-file blot.
 * Without a matcher, copying a file chip (or a selection containing one)
 * and pasting it back into the editor would drop the chip on the floor. */
function _registerNoteFileClipboardMatcher(quill) {
  if (!quill || !quill.clipboard) return;
  if (typeof Quill === 'undefined') return;
  let DeltaCtor;
  try { DeltaCtor = Quill.import('delta'); } catch (_) { return; }
  if (!DeltaCtor) return;
  quill.clipboard.addMatcher('a.note-file', (node, delta) => {
    const meta = {
      id:       node.getAttribute('data-id') || '',
      url:      node.getAttribute('href') || '',
      filename: node.querySelector('.nf-name')?.textContent || '',
      mime:     node.getAttribute('data-mime') || '',
      size:     parseInt(node.getAttribute('data-size') || '0', 10) || 0,
    };
    return new DeltaCtor().insert({ 'note-file': meta });
  });
}

function _registerNoteFileBlot() {
  if (window._noteFileBlotRegistered) return;
  if (typeof Quill === 'undefined') return;
  const InlineEmbed = Quill.import('blots/embed');

  class NoteFile extends InlineEmbed {
    static create(value) {
      const node = super.create(value);
      const v = (value && typeof value === 'object') ? value : {};
      const url      = v.url || '#';
      const filename = v.filename || v.name || 'file';
      const mime     = v.mime || '';
      const size     = parseInt(v.size, 10) || 0;
      const id       = v.id || '';
      const kind     = _fileKindFor({ filename, mime });

      node.setAttribute('href', url);
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
      node.setAttribute('contenteditable', 'false');
      node.setAttribute('data-id', id);
      node.setAttribute('data-mime', mime);
      node.setAttribute('data-size', String(size));
      node.setAttribute('data-kind', kind);

      const iconSpan = document.createElement('span');
      iconSpan.className = 'nf-icon';
      iconSpan.innerHTML = `<i class="${_fileIconFor({ filename, mime, kind })}"></i>`;

      const nameSpan = document.createElement('span');
      nameSpan.className = 'nf-name';
      nameSpan.textContent = filename;

      const metaSpan = document.createElement('span');
      metaSpan.className = 'nf-meta';
      metaSpan.textContent = size > 0 ? _formatFileSize(size) : '';

      node.appendChild(iconSpan);
      node.appendChild(nameSpan);
      if (metaSpan.textContent) node.appendChild(metaSpan);
      return node;
    }

    static value(node) {
      return {
        id:       node.getAttribute('data-id') || '',
        url:      node.getAttribute('href') || '',
        filename: node.querySelector('.nf-name')?.textContent || '',
        mime:     node.getAttribute('data-mime') || '',
        size:     parseInt(node.getAttribute('data-size') || '0', 10) || 0,
      };
    }
  }
  NoteFile.blotName  = 'note-file';
  NoteFile.tagName   = 'a';
  NoteFile.className = 'note-file';
  Quill.register(NoteFile, true);
  window._noteFileBlotRegistered = true;
}

function _fileKindFor({ filename = '', mime = '' }) {
  const ext = (filename.split('.').pop() || '').toLowerCase();
  const m = (mime || '').toLowerCase();
  if (m.startsWith('image/'))             return 'image';
  if (m.startsWith('audio/'))             return 'audio';
  if (m.startsWith('video/'))             return 'video';
  if (m === 'application/pdf' || ext === 'pdf') return 'pdf';
  if (['doc', 'docx', 'odt', 'rtf'].includes(ext) ||
      m.includes('wordprocessingml') || m.includes('msword')) return 'word';
  if (['xls', 'xlsx', 'ods', 'csv', 'tsv', 'numbers'].includes(ext) ||
      m.includes('spreadsheetml') || m.includes('ms-excel')) return 'excel';
  if (['ppt', 'pptx', 'odp', 'key'].includes(ext) ||
      m.includes('presentationml') || m.includes('powerpoint')) return 'ppt';
  if (['zip', '7z', 'rar', 'tar', 'gz', 'bz2', 'xz'].includes(ext)) return 'archive';
  if (['py', 'js', 'ts', 'tsx', 'jsx', 'java', 'c', 'h', 'cpp', 'cs', 'go', 'rs',
       'rb', 'php', 'swift', 'kt', 'sh', 'bash', 'ps1', 'sql', 'html', 'htm',
       'css', 'scss', 'sass', 'less', 'vue', 'svelte', 'r', 'lua', 'pl'].includes(ext)) return 'code';
  if (['json', 'yml', 'yaml', 'toml', 'ini', 'env', 'xml'].includes(ext) ||
      m === 'application/json' || m === 'application/xml') return 'data';
  if (['txt', 'md', 'markdown', 'log'].includes(ext) ||
      m.startsWith('text/')) return 'text';
  return 'text';
}

function _fileIconFor({ filename = '', mime = '', kind } = {}) {
  const k = kind || _fileKindFor({ filename, mime });
  switch (k) {
    case 'pdf':     return 'fa-solid fa-file-pdf';
    case 'word':    return 'fa-solid fa-file-word';
    case 'excel':   return 'fa-solid fa-file-excel';
    case 'ppt':     return 'fa-solid fa-file-powerpoint';
    case 'archive': return 'fa-solid fa-file-zipper';
    case 'audio':   return 'fa-solid fa-file-audio';
    case 'video':   return 'fa-solid fa-file-video';
    case 'image':   return 'fa-regular fa-image';
    case 'code':    return 'fa-solid fa-file-code';
    case 'data':    return 'fa-solid fa-database';
    case 'text':    return 'fa-solid fa-file-lines';
  }
  return 'fa-solid fa-file';
}

function _formatFileSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024)               return `${bytes} B`;
  if (bytes < 1024 * 1024)        return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function _refreshNotesEmptyHint() {
  const hint = document.getElementById('notes-empty-hint');
  if (!hint || !_quill) return;
  // Quill applies .ql-blank when contents are empty. We also want to hide the
  // hint as soon as a placeholder/upload is in progress so it doesn't compete
  // visually with the spinner.
  const isBlank = _quill.root.classList.contains('ql-blank') &&
                  !_quill.root.querySelector('.note-file, img');
  hint.classList.toggle('visible', isBlank);
}

/* Apply a server-loaded payload (or null) to the editor for `sessionId`. */
function _notesApplyForSession(sessionId, payload) {
  // Cancel any pending save for the previous session
  if (_notesSaveTimer) { clearTimeout(_notesSaveTimer); _notesSaveTimer = null; }
  _notesDirty = false;
  _notesSessionBound = sessionId || null;

  // If the user hasn't opened the Notes pane yet, defer construction.
  if (!_quill) {
    _notesPendingPayload = payload;
    _notesNeedsBindOnInit = true;
    return;
  }
  _applyNotesPayload(payload);
}

function _applyNotesPayload(payload) {
  if (!_quill) return;
  _notesSuppressChange = true;
  try {
    if (payload && payload.delta) {
      const ops = Array.isArray(payload.delta) ? payload.delta : payload.delta.ops;
      _quill.setContents({ ops: Array.isArray(ops) ? ops : [] }, 'silent');
    } else {
      _quill.setText('', 'silent');
    }
  } finally {
    _notesSuppressChange = false;
  }
  _quill.history.clear();
  _refreshNotesEmptyHint();
}

/* Reset the editor when the user navigates to a different session. The
 * caller (`clearAll`) is invoked before `loadSession` populates the new
 * payload, so we just blank the contents here.
 */
function _notesResetForSessionChange() {
  if (_notesSaveTimer) { clearTimeout(_notesSaveTimer); _notesSaveTimer = null; }
  _notesDirty = false;
  _notesSessionBound = null;
  _notesPendingPayload = null;
  if (!_quill) return;
  _notesSuppressChange = true;
  try { _quill.setText('', 'silent'); }
  finally { _notesSuppressChange = false; }
  _quill.history.clear();
  _refreshNotesEmptyHint();
  const badge = document.getElementById('notes-status-badge');
  if (badge) badge.classList.add('hidden');
}

function _scheduleNotesSave() {
  if (!state.sessionId) return;  // can't save without a session
  if (_notesSaveTimer) clearTimeout(_notesSaveTimer);
  const badge = document.getElementById('notes-status-badge');
  if (badge) {
    badge.textContent = 'saving…';
    badge.classList.remove('hidden', 'saved');
  }
  _notesSaveTimer = setTimeout(() => _notesFlushSave(false), 800);
}

async function _notesFlushSave(showImmediate) {
  if (!_quill || !state.sessionId) return;
  // Saving for the wrong session would clobber its data - bail.
  if (_notesSessionBound && _notesSessionBound !== state.sessionId) return;
  _notesSaveTimer = null;
  const delta = _quill.getContents();
  const isEmpty = !delta || !delta.ops || delta.ops.length === 0 ||
    (delta.ops.length === 1 && delta.ops[0].insert === '\n');
  const payload = isEmpty ? { delta: null } : { delta };
  try {
    await fetch(`/api/sessions/${state.sessionId}/notes`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    _notesDirty = false;
    const badge = document.getElementById('notes-status-badge');
    if (badge) {
      badge.textContent = 'saved';
      badge.classList.add('saved');
      setTimeout(() => badge?.classList.add('hidden'), 1100);
    }
  } catch (err) {
    console.error('Notes save failed', err);
    const badge = document.getElementById('notes-status-badge');
    if (badge) {
      badge.textContent = 'save failed';
      badge.classList.remove('saved');
    }
  }
}

// Flush any pending save when the page is about to unload
window.addEventListener('beforeunload', () => {
  if (_notesDirty && state.sessionId && _quill) {
    try {
      const delta = _quill.getContents();
      const blob = new Blob([JSON.stringify({ delta })], { type: 'application/json' });
      navigator.sendBeacon(`/api/sessions/${state.sessionId}/notes`, blob);
    } catch (_) {}
  }
});

/* Drag-and-drop + paste wiring ─────────────────────────────────────────── */
const _NOTES_INTERNAL_DRAG_MIME = 'application/x-notes-embed';
let _notesHasFocus = false;
let _notesGlobalDropInited = false;

/* Document-level drag router: while the notes editor has focus, claim file
 * drags from anywhere on the page so the session-import overlay doesn't
 * pop up and steal them. Listeners run in capture phase so they fire
 * BEFORE the import handler (which is bubble-phase). */
function _initNotesGlobalDropRouter() {
  if (_notesGlobalDropInited) return;
  _notesGlobalDropInited = true;
  const overlay = document.getElementById('notes-drop-overlay');

  const claim = () => _notesHasFocus && state.sessionId;

  document.addEventListener('dragenter', e => {
    if (!claim() || !_dtHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (overlay) overlay.classList.add('active');
  }, true);

  document.addEventListener('dragover', e => {
    if (!claim() || !_dtHasFiles(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  }, true);

  document.addEventListener('dragleave', e => {
    if (!claim()) return;
    // Only hide when the drag actually leaves the window (relatedTarget null).
    if (!e.relatedTarget) {
      if (overlay) overlay.classList.remove('active');
    }
  }, true);

  document.addEventListener('drop', e => {
    if (!claim()) return;
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    e.stopPropagation();
    if (overlay) overlay.classList.remove('active');
    _notesHandleFileSelect(e.dataTransfer.files, e);
  }, true);
}

function _wireNotesDropAndPaste(editorEl) {
  const col = document.querySelector('.col-notes');
  const overlay = document.getElementById('notes-drop-overlay');
  if (!col || !overlay) return;
  _initNotesGlobalDropRouter();

  let dragDepth = 0;
  const isInternalDrag = e =>
    Array.from(e.dataTransfer?.types || []).includes(_NOTES_INTERNAL_DRAG_MIME);

  // Stop propagation on every drag event so the document-level session-import
  // overlay doesn't pop up over the notes column and steal the drop. Treat
  // internal embed drags (image/file rearrange) the same way - same overlay
  // is fine, but no need for the file-types check since dataTransfer.types
  // won't carry "Files" for an internal drag.
  col.addEventListener('dragenter', e => {
    if (!_dtHasFiles(e) && !isInternalDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepth++;
    overlay.classList.add('active');
  });
  col.addEventListener('dragover', e => {
    if (!_dtHasFiles(e) && !isInternalDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer) {
      e.dataTransfer.dropEffect = isInternalDrag(e) ? 'move' : 'copy';
    }
  });
  col.addEventListener('dragleave', e => {
    e.stopPropagation();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) overlay.classList.remove('active');
  });
  col.addEventListener('drop', e => {
    e.stopPropagation();
    dragDepth = 0;
    overlay.classList.remove('active');
    // Internal drag = rearranging an existing embed; takes precedence.
    if (_handleInternalEmbedDrop(e)) return;
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    _notesHandleFileSelect(e.dataTransfer.files, e);
  });

  // Drag-to-rearrange: when an existing image/file chip starts dragging,
  // tag the dataTransfer with our internal MIME + the source index so the
  // drop handler knows to splice it into the new position.
  editorEl.addEventListener('dragstart', e => {
    const blotEl = _findEmbedRootInEditor(e.target);
    if (!blotEl) return;
    let blot;
    try { blot = Quill.find(blotEl); } catch (_) {}
    if (!blot || typeof _quill.getIndex !== 'function') return;
    const idx = _quill.getIndex(blot);
    if (idx < 0) return;
    // Pull the embed payload too - when the drop lands in the chat panel
    // we need the URL/metadata to re-upload the file as a chat attachment.
    const ops = _quill.getContents(idx, 1).ops || [];
    const op = ops[0];
    let payload = { index: idx };
    if (op?.insert?.image) {
      payload.kind = 'image';
      payload.url = op.insert.image;
      // Read the on-screen <img> for filename/dimensions when blob URLs hide it.
      if (blotEl.tagName === 'IMG') {
        const alt = blotEl.getAttribute('alt') || '';
        if (alt) payload.filename = alt;
      }
    } else if (op?.insert?.['note-file']) {
      payload.kind = 'file';
      Object.assign(payload, op.insert['note-file']);
    }
    try {
      e.dataTransfer.setData(_NOTES_INTERNAL_DRAG_MIME, JSON.stringify(payload));
      // copyMove so the chat panel can claim it as a copy while the notes
      // panel still treats an internal drop as a move (rearrange).
      e.dataTransfer.effectAllowed = 'copyMove';
    } catch (_) {}
    e.stopPropagation();
  });

  // Paste handler: intercept clipboard files BEFORE Quill so it doesn't
  // inline data URLs (which would also bloat the saved Delta).
  editorEl.addEventListener('paste', e => {
    if (!e.clipboardData) return;
    const files = [];
    for (const item of e.clipboardData.items || []) {
      if (item.kind === 'file') {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      _notesHandleFileSelect(files, e);
    }
  }, true);
}

/* Resolve a drag-event target to the embed root we want to move. */
function _findEmbedRootInEditor(t) {
  if (!(t instanceof HTMLElement)) return null;
  if (!t.closest('.ql-editor')) return null;
  if (t.tagName === 'IMG') return t;
  if (t.classList?.contains('note-file')) return t;
  const closest = t.closest?.('.note-file');
  return closest || null;
}

/* If the drop carries our internal embed-drag payload, splice the embed to
 * the new caret position. Returns true if it handled the drop. */
function _handleInternalEmbedDrop(e) {
  const raw = (() => {
    try { return e.dataTransfer?.getData(_NOTES_INTERNAL_DRAG_MIME) || ''; }
    catch (_) { return ''; }
  })();
  if (!raw) return false;
  e.preventDefault();
  let payload;
  try { payload = JSON.parse(raw); } catch (_) { return true; }
  const srcIdx = Number(payload?.index);
  if (!Number.isFinite(srcIdx) || srcIdx < 0) return true;
  const dropIdx = _quillIndexFromPoint(e.clientX, e.clientY);
  if (dropIdx == null) return true;
  // Same spot → no-op (dropIdx === srcIdx is "before self"; +1 is "after self").
  if (dropIdx === srcIdx || dropIdx === srcIdx + 1) return true;

  const ops = (_quill.getContents(srcIdx, 1).ops) || [];
  const op = ops[0];
  if (!op || !op.insert || typeof op.insert === 'string') return true;
  const embedKey = Object.keys(op.insert)[0];
  const embedValue = op.insert[embedKey];

  _notesSuppressChange = true;
  _quill.deleteText(srcIdx, 1, 'silent');
  const adjusted = (dropIdx > srcIdx) ? dropIdx - 1 : dropIdx;
  _quill.insertEmbed(adjusted, embedKey, embedValue, 'user');
  _quill.setSelection(adjusted + 1, 0, 'silent');
  _notesSuppressChange = false;
  _notesDirty = true;
  _scheduleNotesSave();
  return true;
}

/* Map screen coords (e.clientX, e.clientY) to a Quill insertion index by
 * asking the browser for the caret position at that point and reading it
 * back via Quill's selection module. Returns null if outside the editor. */
function _quillIndexFromPoint(x, y) {
  if (!_quill) return null;
  let range = null;
  if (document.caretRangeFromPoint) {
    range = document.caretRangeFromPoint(x, y);
  } else if (document.caretPositionFromPoint) {
    const pos = document.caretPositionFromPoint(x, y);
    if (pos) {
      range = document.createRange();
      range.setStart(pos.offsetNode, pos.offset);
      range.collapse(true);
    }
  }
  if (!range) return _quill.getLength();
  const editorRoot = _quill.root;
  if (!editorRoot.contains(range.startContainer)) {
    // Drop happened over the editor pane but outside the actual ql-editor
    // (e.g. the empty area below the last paragraph) - append at the end.
    return _quill.getLength();
  }
  const sel = window.getSelection();
  if (!sel) return _quill.getLength();
  sel.removeAllRanges();
  sel.addRange(range);
  const r = _quill.getSelection();
  return r ? r.index : _quill.getLength();
}

function _dtHasFiles(e) {
  const t = e.dataTransfer;
  if (!t) return false;
  const types = t.types;
  if (!types) return false;
  return Array.from(types).includes('Files');
}

/* File-selection entry point - used by drop, paste, and the toolbar button. */
async function _notesHandleFileSelect(files, originalEvent) {
  if (!files || !files.length) return;
  if (!state.sessionId) {
    flashStatus('Start a recording or open a session first');
    return;
  }
  _ensureNotesEditor();
  if (!_quill) return;

  // Determine the insertion index. For a drop, use the drop point so files
  // land where the user let go. Otherwise (paste, toolbar), fall back to the
  // current selection / end of doc.
  let insertIndex = null;
  const isDrop = originalEvent && originalEvent.type === 'drop'
    && typeof originalEvent.clientX === 'number';
  if (isDrop) {
    insertIndex = _quillIndexFromPoint(originalEvent.clientX, originalEvent.clientY);
  }
  if (insertIndex == null) {
    const sel = _quill.getSelection(true);
    insertIndex = sel ? sel.index : _quill.getLength();
  }

  for (const file of Array.from(files)) {
    const isImage = (file.type || '').startsWith('image/');
    const placeholderId = 'pending-' + (++_notesPlaceholderSeq);

    if (isImage) {
      // Show the image immediately via a blob URL, then swap to the server
      // URL once the upload completes. We track the embed by *index* (not by
      // URL string), because Quill's image blot round-trips src through
      // getAttribute() which may normalize the URL - leaving a Delta-side
      // string match unable to find the embed.
      const tempUrl = URL.createObjectURL(file);
      const imageIndex = insertIndex;
      _notesSuppressChange = true;
      _quill.insertEmbed(imageIndex, 'image', tempUrl, 'user');
      _quill.setSelection(imageIndex + 1, 0, 'silent');
      _notesSuppressChange = false;
      insertIndex += 1;

      try {
        const meta = await _notesUploadFile(file);
        _notesSwapImageAt(imageIndex, tempUrl, meta.url);
      } catch (err) {
        console.error('Notes image upload failed', err);
        flashStatus('Image upload failed');
        // Leave the temp blob in place so the user doesn't lose context;
        // mark the saved Delta as non-dirty since we never persisted it.
      }
    } else {
      // Insert a chip in "uploading" state, then update with real metadata
      const placeholderMeta = {
        id: placeholderId,
        filename: file.name,
        mime: file.type || 'application/octet-stream',
        size: file.size,
        url: '#',
      };
      _notesSuppressChange = true;
      _quill.insertEmbed(insertIndex, 'note-file', placeholderMeta, 'user');
      _quill.setSelection(insertIndex + 1, 0, 'silent');
      _notesSuppressChange = false;
      insertIndex += 1;
      const chipEl = _findFileChipDom(placeholderId);
      if (chipEl) chipEl.classList.add('uploading');

      try {
        const meta = await _notesUploadFile(file);
        _notesReplaceFileChip(placeholderId, meta);
      } catch (err) {
        console.error('Notes file upload failed', err);
        const failedChip = _findFileChipDom(placeholderId);
        if (failedChip) {
          failedChip.classList.remove('uploading');
          failedChip.classList.add('upload-error');
          failedChip.title = 'Upload failed: ' + (err.message || 'Network error');
        }
        flashStatus('Attachment upload failed');
      }
    }
  }
  _notesDirty = true;
  _scheduleNotesSave();
}

async function _notesUploadFile(file) {
  if (!state.sessionId) throw new Error('No active session');
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(`/api/sessions/${state.sessionId}/notes/attachments`, {
    method: 'POST',
    body: fd,
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || ('HTTP ' + r.status));
  }
  return r.json();
}

function _findFileChipDom(placeholderId) {
  if (!_quill) return null;
  return _quill.root.querySelector(`.note-file[data-id="${CSS.escape(placeholderId)}"]`);
}

/* Replace an in-document image at a known index. Mutating <img>.src directly
 * would bypass Quill's internal model so the next getContents() would still
 * serialize the old blob URL. We delete + re-insert at the same position so
 * the Delta and DOM stay in sync. The blob URL is revoked only AFTER the
 * server URL is in place, otherwise a failed swap would leave a revoked blob.
 */
function _notesSwapImageAt(index, oldBlobUrl, newSrc) {
  if (!_quill) return;
  const ops = _quill.getContents().ops || [];
  // Verify the embed at this index is still an image (it should be, but the
  // user may have edited the doc while the upload was in flight; in that
  // case we fall back to a Delta-string match).
  let walked = 0;
  let foundIndex = -1;
  for (const op of ops) {
    if (typeof op.insert === 'string') {
      walked += op.insert.length;
    } else if (op.insert && typeof op.insert === 'object') {
      if (walked === index && 'image' in op.insert) { foundIndex = walked; break; }
      walked += 1;
    }
  }
  if (foundIndex < 0) {
    // Index drifted - fall back to URL match (best-effort).
    foundIndex = _findEmbedIndex(op => op?.insert?.image === oldBlobUrl);
  }
  if (foundIndex < 0) return;
  _notesSuppressChange = true;
  _quill.deleteText(foundIndex, 1, 'silent');
  _quill.insertEmbed(foundIndex, 'image', newSrc, 'silent');
  _notesSuppressChange = false;
  // Now safe to release the blob - the DOM no longer references it.
  try { URL.revokeObjectURL(oldBlobUrl); } catch (_) {}
}

function _notesReplaceFileChip(placeholderId, meta) {
  if (!_quill) return;
  const idx = _findEmbedIndex(op => op?.insert?.['note-file']?.id === placeholderId);
  if (idx < 0) return;
  _notesSuppressChange = true;
  _quill.deleteText(idx, 1, 'silent');
  _quill.insertEmbed(idx, 'note-file', {
    id: meta.id,
    url: meta.url,
    filename: meta.filename,
    mime: meta.mime,
    size: meta.size,
  }, 'silent');
  _notesSuppressChange = false;
}

function _findEmbedIndex(predicate) {
  if (!_quill) return -1;
  const ops = _quill.getContents().ops || [];
  let index = 0;
  for (const op of ops) {
    if (typeof op.insert === 'string') {
      index += op.insert.length;
    } else if (op.insert && typeof op.insert === 'object') {
      if (predicate(op)) return index;
      index += 1;
    }
  }
  return -1;
}

/* ── Toolbar action helpers ─────────────────────────────────────────────── */
function copyNotesPlainText() {
  if (!_quill) return;
  const text = _quill.getText().trim();
  if (!text) { flashStatus('Notes are empty'); return; }
  navigator.clipboard.writeText(text).then(
    () => flashStatus('Notes copied'),
    () => flashStatus('Copy failed')
  );
}

function downloadNotesHtml() {
  if (!_quill) return;
  // Get the HTML directly from the editor's root so file chips and images
  // come along verbatim (semantic anchors are a portable file format).
  const inner = _quill.root.innerHTML;
  if (!inner || _quill.getText().trim().length === 0 && !inner.includes('<img') && !inner.includes('note-file')) {
    flashStatus('Notes are empty'); return;
  }
  const title = (document.getElementById('topbar-session-title')?.textContent || 'Notes').trim() || 'Notes';
  const safeTitle = title.replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 80);
  const fullHtml = `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(title)}</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.6;color:#222;max-width:800px;margin:32px auto;padding:0 16px}
img{max-width:100%;border-radius:6px}
a.note-file{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;background:#f1f3f5;border:1px solid #ced4da;border-radius:999px;color:#212529;text-decoration:none;font-size:13px;margin:2px}
a.note-file:hover{background:#e9ecef}
.nf-icon{display:inline-flex;width:22px;height:22px;background:#fff;border-radius:50%;border:1px solid #ced4da;align-items:center;justify-content:center}
blockquote{border-left:3px solid #4c6ef5;background:#eef2ff;margin:8px 0;padding:6px 12px;border-radius:0 4px 4px 0}
pre{background:#f8f9fa;border:1px solid #ced4da;border-radius:4px;padding:10px 12px;overflow:auto}
code{background:#f1f3f5;border:1px solid #dee2e6;border-radius:3px;padding:0 4px;font-size:0.9em}
</style>
</head><body><h1>${escapeHtml(title)}</h1>${inner}</body></html>`;
  const blob = new Blob([fullHtml], { type: 'text/html' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${safeTitle || 'notes'}.html`;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  flashStatus('Notes exported');
}

async function clearNotes() {
  if (!_quill) return;
  if (_quill.getText().trim().length === 0 &&
      !_quill.root.querySelector('.note-file, img')) return;
  if (!await uiConfirm({ title: 'Clear all notes?', message: 'This cannot be undone.', confirmLabel: 'Clear', danger: true })) return;
  _quill.setContents({ ops: [] }, 'user');
  _quill.history.clear();
  _refreshNotesEmptyHint();
  _notesDirty = true;
  _notesFlushSave(true);
}

function highlightCode(sel) {
  document.querySelectorAll(`${sel} pre code`).forEach(el => {
    if (!el.dataset.highlighted) hljs.highlightElement(el);
  });
}

function escapeHtml(s) {
  // Strings land in title="..." and data-tooltip="..." attributes all over the
  // app, so quotes have to be escaped too. Null-safe: a missing title is '',
  // never a thrown TypeError halfway through a render.
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function flashStatus(msg) {
  // Mirror to the console so users debugging via DevTools can read full
  // messages even when they're truncated in the small status pill, and
  // catch ones that were too brief to read at all.
  try {
    const text = (msg && msg.toString) ? msg.toString() : String(msg);
    if (/\b(error|fail|failed|denied|invalid|not found|timeout)\b/i.test(text)) {
      console.error('[status]', text);
    } else {
      console.log('[status]', text);
    }
  } catch (_) {}
  const el = document.getElementById('status-text');
  if (!el) return;
  const prev = el.textContent;
  el.textContent = msg;
  setTimeout(() => { el.textContent = prev; }, 1800);
}

// Persistent top banner warning that call/desktop audio is not being captured.
// Driven by the server's capture_alert SSE event; dismissable, and cleared when
// a recording stops (see onStatus).
function _showCaptureAlert(d) {
  const msg = (d && d.message) || 'Call/desktop audio is not being captured.';
  let bar = document.getElementById('capture-alert-bar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'capture-alert-bar';
    bar.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:99999',
      'background:var(--red,#b62324)', 'color:#fff', 'padding:11px 16px',
      'font:600 14px/1.45 system-ui,-apple-system,sans-serif', 'display:flex',
      'align-items:center', 'gap:12px', 'box-shadow:0 2px 12px rgba(0,0,0,.45)',
    ].join(';');
    const icon = document.createElement('span');
    icon.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i>';
    const txt = document.createElement('span');
    txt.id = 'capture-alert-text';
    txt.style.flex = '1';
    const x = document.createElement('button');
    x.textContent = '×';
    x.setAttribute('aria-label', 'Dismiss');
    x.style.cssText = 'background:transparent;border:0;color:#fff;font-size:22px;cursor:pointer;line-height:1;padding:0 4px';
    x.addEventListener('click', _clearCaptureAlert);
    bar.append(icon, txt, x);
    document.body.appendChild(bar);
  }
  document.getElementById('capture-alert-text').textContent = msg;
  bar.style.display = 'flex';
}

function _clearCaptureAlert() {
  const bar = document.getElementById('capture-alert-bar');
  if (bar) bar.style.display = 'none';
}

/* ── Audio device selection ──────────────────────────────────────────────── */
async function loadAudioDevices() {
  const lbSel  = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  if (!lbSel || !micSel) return;

  // Saved choices from server prefs (with localStorage fallback for migration)
  const savedLb      = _prefs.loopback_device ?? localStorage.getItem('viz-loopback-idx') ?? '';
  const savedLbName  = _prefs.loopback_device_name ?? '';
  const savedMic     = _prefs.mic_device      ?? localStorage.getItem('viz-mic-idx')      ?? '';

  lbSel.innerHTML  = '<option value="">- loading -</option>';
  micSel.innerHTML = '<option value="-1">None</option>';

  let data;
  try {
    data = await fetch('/api/audio/devices').then(r => r.json());
  } catch {
    lbSel.innerHTML = '<option value="">- error -</option>';
    return;
  }

  // Populate loopback selector
  lbSel.innerHTML = '';
  if (!data.loopback?.length) {
    lbSel.innerHTML = '<option value="">- none found -</option>';
  } else {
    for (const d of data.loopback) {
      const opt = document.createElement('option');
      opt.value       = d.index;
      opt.textContent = d.name;
      lbSel.appendChild(opt);
    }
    // Re-select the saved device, preferring its NAME over the stored index.
    // PyAudio indices are positional and drift when the device list changes
    // (headset plugged in, meeting app adds a virtual endpoint, driver update,
    // reboot), so a matching index can point at a different device now.
    const optByName = savedLbName
      ? [...lbSel.options].find(o => o.textContent === savedLbName)
      : null;
    const idxMatches = savedLb && [...lbSel.options].some(o => o.value === String(savedLb));
    if (optByName) {
      lbSel.value = optByName.value;
      // Index shifted since we saved it: rewrite it so the backend fast path
      // and any caller that only sends the index stay correct.
      if (String(optByName.value) !== String(savedLb)) savePref('loopback_device', optByName.value);
    } else if (idxMatches) {
      lbSel.value = savedLb;
      // Legacy pref that predates name-saving: back-fill the name now so the
      // selection can self-heal next time the list is renumbered.
      if (!savedLbName && lbSel.selectedOptions[0])
        savePref('loopback_device_name', lbSel.selectedOptions[0].textContent);
    } else if (savedLbName) {
      // Saved device is gone entirely; loose fuzzy match as a last resort.
      const fuzzy = [...lbSel.options].find(o =>
        o.textContent.includes(savedLbName) || savedLbName.includes(o.textContent));
      if (fuzzy) {
        lbSel.value = fuzzy.value;
        savePref('loopback_device', fuzzy.value);
      }
    }
  }

  // Populate mic selector - FFmpeg (dshow) devices + None.
  // Browser mic (-2) and WASAPI mic (device index) options are disabled in favor
  // of FFmpeg subprocess capture which is far more reliable on Windows.  Both
  // Browser and WASAPI suffered from choppy/distorted audio caused by shared-mode
  // WASAPI contention and Chrome getUserMedia processing.  The backend code for
  // both paths is retained (mic_index=-2 for browser, positive index for WASAPI)
  // in case we need to reverse course.
  micSel.innerHTML = '';
  if (data.dshow?.length) {
    for (const d of data.dshow) {
      const opt = document.createElement('option');
      opt.value       = 'ffmpeg:' + d.name;
      opt.textContent = d.name;
      micSel.appendChild(opt);
    }
  }
  {
    const none = document.createElement('option');
    none.value = '-1'; none.textContent = 'None';
    micSel.appendChild(none);
  }
  if (savedMic && [...micSel.options].some(o => o.value === String(savedMic))) {
    micSel.value = savedMic;
  } else if (savedMic && savedMic !== '-1' && !String(savedMic).startsWith('ffmpeg:')) {
    // Legacy saved value (WASAPI index or browser mic "-2") - try to match by
    // device name.  WASAPI and dshow names for the same physical mic are usually
    // identical, so find the WASAPI name from data.input and look for a matching
    // ffmpeg option.
    let legacyName = null;
    if (savedMic === '-2') {
      // Browser mic has no name to match - just fall through to first dshow device
    } else {
      const idx = parseInt(savedMic, 10);
      const wasapiDev = (data.input || []).find(d => d.index === idx);
      if (wasapiDev) legacyName = wasapiDev.name;
    }
    if (legacyName) {
      // Fuzzy match: score each dshow option by how many words overlap with the
      // legacy WASAPI name.  Longest overlap wins.  This handles truncation,
      // different suffixes, and reordering between WASAPI and dshow names.
      const legacyWords = legacyName.toLowerCase().split(/[\s\-_()]+/).filter(w => w.length >= 3);
      let bestMatch = null, bestScore = 0;
      for (const o of micSel.options) {
        if (!o.value.startsWith('ffmpeg:')) continue;
        const dshowWords = o.textContent.toLowerCase().split(/[\s\-_()]+/).filter(w => w.length >= 3);
        const score = legacyWords.filter(w => dshowWords.some(dw => dw.includes(w) || w.includes(dw))).length;
        if (score > bestScore) { bestScore = score; bestMatch = o; }
      }
      if (bestMatch && bestScore >= 1) {
        micSel.value = bestMatch.value;
        savePref('mic_device', bestMatch.value);
      }
    }
  }

  // Re-apply disabled state if currently recording
  lbSel.disabled  = state.isRecording;
  micSel.disabled = state.isRecording;

  // Persist the resolved selection so the backend fallback (home-page / tray
  // starts, which send no device IDs) reuses the same device.  Save the name
  // alongside the index; an index on its own drifts when the list is
  // renumbered, which is what caused the desktop device to reset.
  if (lbSel.value && !_prefs.loopback_device) {
    savePref('loopback_device', lbSel.value);
    if (lbSel.selectedOptions[0]) savePref('loopback_device_name', lbSel.selectedOptions[0].textContent);
  }
  if (micSel.value && !_prefs.mic_device)     savePref('mic_device',      micSel.value);
}

function saveDeviceSelection() {
  const lbSel  = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  if (lbSel) {
    savePref('loopback_device', lbSel.value);
    // Persist the friendly name alongside the index so the selection can be
    // re-resolved by name after the audio-device list is renumbered.
    savePref('loopback_device_name', lbSel.selectedOptions?.[0]?.textContent || '');
  }
  if (micSel) savePref('mic_device', micSel.value);
}

async function toggleAudioTest() {
  if (state.isTesting) {
    try {
      await fetch('/api/audio/test/stop', { method: 'POST' });
    } catch (_) { /* network error */ }
    // Eagerly release browser mic regardless of server response -
    // don't wait for SSE event which may be delayed or lost.
    state.isTesting = false;
    updateTestBtn();
  } else {
    const lbSel  = document.getElementById('viz-loopback-sel');
    const lbVal  = lbSel?.value;
    const micVal = document.getElementById('viz-mic-sel')?.value;
    const body   = {};
    if (lbVal  !== '' && lbVal  != null) {
      body.loopback_device = parseInt(lbVal,  10);
      const lbName = lbSel?.selectedOptions?.[0]?.textContent;
      if (lbName) body.loopback_device_name = lbName;
    }
    Object.assign(body, parseMicSelection(micVal));

    const resp = await fetch('/api/audio/test/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      uiToast({ message: err.error || 'Failed to start audio test', kind: 'error' });
    }
  }
}

async function autoDetectDevices() {
  const btn = document.getElementById('viz-autodetect-btn');
  const testBtn = document.getElementById('viz-test-btn');
  const lbSel = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  if (!btn) return;

  // Save current options so we can restore them after
  const lbOpts = lbSel ? lbSel.innerHTML : '';
  const micOpts = micSel ? micSel.innerHTML : '';

  btn.disabled = true;
  btn.classList.add('detecting');
  btn.innerHTML = '<i class="fa-duotone fa-spinner fa-spin"></i>';
  if (testBtn) testBtn.disabled = true;
  if (lbSel)  { lbSel.innerHTML  = '<option>Analysing\u2026</option>'; lbSel.disabled  = true; }
  if (micSel) { micSel.innerHTML = '<option>Analysing\u2026</option>'; micSel.disabled = true; }

  try {
    const resp = await fetch('/api/audio/auto-detect', { method: 'POST' });
    const data = await resp.json();

    // Restore original options before selecting
    if (lbSel)  lbSel.innerHTML  = lbOpts;
    if (micSel) micSel.innerHTML = micOpts;

    if (!resp.ok) {
      uiToast({ message: data.error || 'Auto-detect failed', kind: 'error' });
      return;
    }

    let changed = false;
    if (data.best_loopback && lbSel) {
      const idx = String(data.best_loopback.index);
      if ([...lbSel.options].some(o => o.value === idx)) {
        lbSel.value = idx;
        changed = true;
      }
    }

    if (data.best_mic && micSel) {
      const val = 'ffmpeg:' + data.best_mic.name;
      if ([...micSel.options].some(o => o.value === val)) {
        micSel.value = val;
        changed = true;
      }
    }

    if (changed) saveDeviceSelection();
  } catch (e) {
    // Restore options on error too
    if (lbSel)  lbSel.innerHTML  = lbOpts;
    if (micSel) micSel.innerHTML = micOpts;
    uiToast({ message: 'Auto-detect failed: ' + e.message, kind: 'error' });
  } finally {
    btn.disabled = false;
    btn.classList.remove('detecting');
    btn.innerHTML = '<i class="fa-duotone fa-wand-magic-sparkles"></i>';
    if (testBtn) testBtn.disabled = false;
    if (lbSel)  lbSel.disabled  = false;
    if (micSel) micSel.disabled = false;
  }
}

/** Update the AGC debug panel in the sidebar. */
function _updateAgcDebug(agc) {
  const el = document.getElementById('agc-debug');
  if (!el) return;
  if (!agc) { el.style.display = 'none'; return; }
  el.style.display = '';

  const fmt = (v) => v < 0.001 ? v.toExponential(1) : v.toFixed(4);
  const renderCol = (id, label, cssClass, enabled, gain, env, gated, target, bypassed) => {
    const col = document.getElementById(id);
    if (!col) return;
    if (!enabled) {
      col.innerHTML = `<div class="agc-src ${cssClass}">${label}</div><div class="agc-idle">disabled</div>`;
      return;
    }
    const status = bypassed ? '<span class="agc-idle">bypassed (cleaning)</span>'
                  : gated ? '<span class="agc-gated">GATED</span>'
                  : gain > 1.01 ? `<span class="agc-boosting">BOOST ${gain.toFixed(1)}\u00d7</span>`
                  : '<span class="agc-idle">1.0\u00d7</span>';
    col.innerHTML = `<div class="agc-src ${cssClass}">${label}</div>`
      + `<div class="agc-val"><span class="agc-lbl">Status</span> ${status}</div>`
      + `<div class="agc-val"><span class="agc-lbl">Gain</span> ${gain.toFixed(2)}\u00d7</div>`
      + `<div class="agc-val"><span class="agc-lbl">Env</span> ${fmt(env)}</div>`
      + `<div class="agc-val"><span class="agc-lbl">Target</span> ${fmt(target)}</div>`;
  };
  renderCol('agc-debug-lb',  'Desktop', 'lb',  agc.lb_enabled,  agc.lb_gain,  agc.lb_env,  agc.lb_gated,  agc.target, false);
  renderCol('agc-debug-mic', 'Mic',     'mic', agc.mic_enabled, agc.mic_gain, agc.mic_env, agc.mic_gated, agc.target, agc.mic_bypassed);
}

/** Parse the mic selector value into {mic_device, ffmpeg_mic_name} for the API. */
function parseMicSelection(micVal) {
  if (micVal == null || micVal === '') return {};
  if (typeof micVal === 'string' && micVal.startsWith('ffmpeg:')) {
    return { mic_device: -3, ffmpeg_mic_name: micVal.slice(7) };
  }
  return { mic_device: parseInt(micVal, 10) };
}

/* ── Audio visualizer ────────────────────────────────────────────────────── */
const N_BARS = 32;
let vizLbTarget = 0, vizMicTarget = 0;
let vizLb = 0,       vizMic = 0;
let vizHasMic  = false;
let vizLbSpec  = [];   // frequency spectrum from server (N_BARS values, 0-1)
let vizMicSpec = [];
// Smoothed per-band values for animation (fast attack, slow decay)
const vizLbBars  = new Float32Array(N_BARS);
const vizMicBars = new Float32Array(N_BARS);

// Each visualizer loop self-suspends once levels have fully settled to zero so
// it isn't burning a canvas redraw every frame while idle. The rAF id is 0 when
// parked; the kick helpers re-arm it idempotently when fresh audio arrives.
let _vizRAF = 0, _bvRAF = 0;
let _vizFrame = null, _bvFrame = null;
function _startVizLoop()      { if (!_vizRAF && _vizFrame) _vizRAF = requestAnimationFrame(_vizFrame); }
function _startBrandVizLoop() { if (!_bvRAF && _bvFrame) _bvRAF = requestAnimationFrame(_bvFrame); }

function updateLevelMeters(lb, mic, hasMic) {
  const toH = v => Math.round(Math.min(100, Math.log1p(v * 60) / Math.log1p(60) * 100));
  const lbEl  = document.getElementById('viz-meter-lb');
  const micEl = document.getElementById('viz-meter-mic');
  if (lbEl) {
    lbEl.style.height = toH(lb) + '%';
    lbEl.classList.toggle('peak', lb > 0.55);
  }
  if (micEl) {
    micEl.style.height = hasMic ? toH(mic) + '%' : '0%';
    micEl.classList.toggle('peak', hasMic && mic > 0.55);
  }
  // The capture strip runs on the same numbers, so proof that audio is
  // arriving is on screen in every view, not only in the input pane.
  const stripLb  = document.getElementById('capture-meter-desktop');
  const stripMic = document.getElementById('capture-meter-mic');
  if (stripLb) {
    stripLb.style.width = toH(lb) + '%';
    stripLb.classList.toggle('peak', lb > 0.55);
  }
  if (stripMic) {
    stripMic.style.width = hasMic ? toH(mic) + '%' : '0%';
    stripMic.classList.toggle('peak', hasMic && mic > 0.55);
  }
  if (lb > 0.01) _captureLastDesktopAudio = Date.now();
}

function startVizLoop() {
  const canvas = document.getElementById('viz-canvas');
  if (!canvas) return;

  const dpr = window.devicePixelRatio || 1;
  const ctx = canvas.getContext('2d');
  const resize = () => {
    canvas.width  = canvas.offsetWidth  * dpr;
    canvas.height = canvas.offsetHeight * dpr;
  };
  resize();
  // Re-kick after a resize so a parked (idle) loop repaints into the new bitmap.
  new ResizeObserver(() => { resize(); _startVizLoop(); }).observe(canvas);

  _vizFrame = function vizFrame() {
    _vizRAF = 0;

    const w   = canvas.width  / dpr;
    const h   = canvas.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    vizLb  += (vizLbTarget  > vizLb  ? 0.45 : 0.07) * (vizLbTarget  - vizLb);
    vizMic += (vizMicTarget > vizMic ? 0.45 : 0.07) * (vizMicTarget - vizMic);

    const midY  = h / 2;
    const barW  = w / N_BARS;
    const pad   = 1.2;
    const lbActive  = vizLb  > 0.002;
    const micActive = vizHasMic && vizMic > 0.002;

    // Advance smoothed bars toward latest spectrum values (fast attack, slow decay)
    for (let i = 0; i < N_BARS; i++) {
      const lt = vizLbSpec[i]  || 0;
      const mt = vizMicSpec[i] || 0;
      vizLbBars[i]  += (lt > vizLbBars[i]  ? 0.55 : 0.10) * (lt - vizLbBars[i]);
      vizMicBars[i] += (mt > vizMicBars[i] ? 0.55 : 0.10) * (mt - vizMicBars[i]);
    }

    // ── EQ bars - desktop fills upward from midline, mic fills downward ───
    for (let i = 0; i < N_BARS; i++) {
      const x = i * barW + pad;
      const bw = barW - pad * 2;

      // Desktop bar (top half, grows up from midline)
      const lbV = vizLbBars[i];
      const lbH = Math.max(1, lbV * (midY - 3));
      const lbAlpha = lbActive ? 0.25 + 0.75 * lbV : 0.12;
      const lbGrad = ctx.createLinearGradient(0, midY, 0, midY - lbH);
      lbGrad.addColorStop(0, `rgba(88,166,255,${lbAlpha.toFixed(2)})`);
      // Subtle lighten toward tip - ~25% shift, not full white
      const lbT = Math.min(1, lbV * 1.2) * 0.25;
      const lbR = Math.round(88  + (255 - 88)  * lbT);
      const lbG = Math.round(166 + (255 - 166) * lbT);
      lbGrad.addColorStop(1, `rgba(${lbR},${lbG},255,${Math.min(1, lbAlpha + 0.1 * lbT).toFixed(2)})`);
      ctx.fillStyle = lbGrad;
      ctx.fillRect(x, midY - lbH, bw, lbH);

      // Mic bar (bottom half, grows down from midline)
      if (vizHasMic) {
        const micV = vizMicBars[i];
        const micH = Math.max(1, micV * (midY - 3));
        const micAlpha = micActive ? 0.25 + 0.75 * micV : 0.12;
        const micGrad = ctx.createLinearGradient(0, midY + 2, 0, midY + 2 + micH);
        micGrad.addColorStop(0, `rgba(0,180,100,${micAlpha.toFixed(2)})`);
        const micT = Math.min(1, micV * 1.2) * 0.25;
        const micR = Math.round(0   + 255 * micT);
        const micG = Math.round(180 + (255 - 180) * micT);
        const micB = Math.round(100 + (255 - 100) * micT);
        micGrad.addColorStop(1, `rgba(${micR},${micG},${micB},${Math.min(1, micAlpha + 0.1 * micT).toFixed(2)})`);
        ctx.fillStyle = micGrad;
        ctx.fillRect(x, midY + 2, bw, micH);
      }
    }

    // Dividing line
    ctx.strokeStyle = 'rgba(48,54,61,0.9)';
    ctx.lineWidth   = 1;
    ctx.beginPath();
    ctx.moveTo(0, midY);
    ctx.lineTo(w, midY);
    ctx.stroke();

    // Re-arm only while something is still moving: levels, their targets, or the
    // smoothed bars mid-decay. Checking the targets too keeps a freshly-arrived
    // signal alive until the bars visibly fall to zero (never freeze mid-decay).
    let settled = vizLbTarget < 0.002 && vizMicTarget < 0.002 &&
                  vizLb < 0.002 && vizMic < 0.002;
    if (settled) {
      for (let i = 0; i < N_BARS; i++) {
        if (vizLbBars[i] > 0.004 || vizMicBars[i] > 0.004) { settled = false; break; }
      }
    }
    if (!settled && !document.hidden) _vizRAF = requestAnimationFrame(_vizFrame);
  };
  _startVizLoop();   // kick once so any initial decay settles, then it parks
}

/* ── Brand horizontal visualizer (bars extend left/right from logo) ──────── */
function startBrandVizLoop() {
  const canvas = document.getElementById('brand-viz-canvas');
  if (!canvas) return;

  const dpr = window.devicePixelRatio || 1;
  const ctx = canvas.getContext('2d');
  const resize = () => {
    canvas.width  = canvas.offsetWidth  * dpr;
    canvas.height = canvas.offsetHeight * dpr;
  };
  resize();
  // Re-kick after a resize so a parked (idle) loop repaints into the new bitmap.
  new ResizeObserver(() => { resize(); _startBrandVizLoop(); }).observe(canvas);

  // Separate smoothed bars so brand viz can animate independently
  const bvLbBars  = new Float32Array(N_BARS);
  const bvMicBars = new Float32Array(N_BARS);

  _bvFrame = function bvFrame() {
    _bvRAF = 0;

    const w   = canvas.width  / dpr;
    const h   = canvas.height / dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const cx = w / 2;
    const cy = h / 2;
    const logoHalfW = 20;  // tuck bars closer to logo center
    const nBars     = 16;  // bars stacked vertically along logo edge
    const barGap    = 1.0;
    const maxBarW   = cx - logoHalfW - 125; // shorter max so left bars stay on screen
    const barRegionH = h * 0.75;          // vertical region bars span
    const barH      = barRegionH / nBars; // height of each bar
    const topY      = cy - barRegionH / 2; // top of bar stack

    // Smooth toward latest spectrum
    const binsPerBar = N_BARS / nBars;
    for (let i = 0; i < N_BARS; i++) {
      const lt = vizLbSpec[i]  || 0;
      const mt = vizMicSpec[i] || 0;
      bvLbBars[i]  += (lt > bvLbBars[i]  ? 0.55 : 0.10) * (lt - bvLbBars[i]);
      bvMicBars[i] += (mt > bvMicBars[i] ? 0.55 : 0.10) * (mt - bvMicBars[i]);
    }

    const lbActive  = vizLb  > 0.002;
    const micActive = vizHasMic && vizMic > 0.002;

    // Helper: average a range of smoothed bars into one value
    function avgBand(bars, bandIdx) {
      let sum = 0;
      const s = Math.floor(bandIdx * binsPerBar);
      const e = Math.floor((bandIdx + 1) * binsPerBar);
      for (let j = s; j < e; j++) sum += bars[j];
      return sum / (e - s);
    }

    // ── Desktop bars (left side) ──
    // Vertical bars stacked top-to-bottom, each extends horizontally LEFT
    for (let i = 0; i < nBars; i++) {
      const val   = avgBand(bvLbBars, i);
      const y     = topY + i * barH + barGap;
      const bh    = barH - barGap * 2;
      const bw    = Math.max(1.5, val * maxBarW);
      const alpha = lbActive ? 0.18 + 0.60 * val : 0.06;
      ctx.fillStyle = `rgba(88,166,255,${alpha.toFixed(2)})`;
      ctx.fillRect(cx - logoHalfW - bw, y, bw, bh);
    }

    // ── Mic bars (right side) ──
    // Vertical bars stacked top-to-bottom, each extends horizontally RIGHT
    if (vizHasMic) {
      for (let i = 0; i < nBars; i++) {
        const val   = avgBand(bvMicBars, i);
        const y     = topY + i * barH + barGap;
        const bh    = barH - barGap * 2;
        const bw    = Math.max(1.5, val * maxBarW);
        const alpha = micActive ? 0.18 + 0.60 * val : 0.06;
        ctx.fillStyle = `rgba(0,180,100,${alpha.toFixed(2)})`;
        ctx.fillRect(cx + logoHalfW, y, bw, bh);
      }
    }

    // Park once levels, targets, and the smoothed bars have all settled to zero.
    let settled = vizLbTarget < 0.002 && vizMicTarget < 0.002 &&
                  vizLb < 0.002 && vizMic < 0.002;
    if (settled) {
      for (let i = 0; i < N_BARS; i++) {
        if (bvLbBars[i] > 0.004 || bvMicBars[i] > 0.004) { settled = false; break; }
      }
    }
    if (!settled && !document.hidden) _bvRAF = requestAnimationFrame(_bvFrame);
  };
  _startBrandVizLoop();   // kick once so any initial decay settles, then it parks
}

/* ── Gain controls ───────────────────────────────────────────────────────── */
let _gainSendTimer = null;
let _gainLastInput = 0;   // timestamp of last user interaction - suppresses SSE sync

function onGainInput(channel, val) {
  _gainLastInput = Date.now();
  const v = parseFloat(val);
  const label = v < 10 ? v.toFixed(2).replace(/\.?0+$/, '') + '×' : Math.round(v) + '×';
  document.getElementById(`viz-${channel === 'lb' ? 'lb' : 'mic'}-gain-val`).textContent = label;
  localStorage.setItem(`gain-${channel}`, val);
  // Debounce the API call so we don't flood on slider drag
  clearTimeout(_gainSendTimer);
  _gainSendTimer = setTimeout(() => {
    fetch('/api/audio/gain', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(channel === 'lb' ? { lb_gain: v } : { mic_gain: v }),
    }).catch(() => {});
  }, 80);
}

function _syncGainSlider(channel, val) {
  // Don't override the slider while the user is actively adjusting it
  if (Date.now() - _gainLastInput < 800) return;
  const id  = `viz-${channel}-gain`;
  const el  = document.getElementById(id);
  if (!el || Math.abs(parseFloat(el.value) - val) < 0.01) return;
  el.value = val;
  onGainInput(channel, String(val));
}

function initGainSliders() {
  for (const ch of ['lb', 'mic']) {
    const stored = localStorage.getItem(`gain-${ch}`);
    const val    = stored ? parseFloat(stored) : 1.0;
    const el     = document.getElementById(`viz-${ch}-gain`);
    if (!el) continue;
    el.value = val;
    onGainInput(ch, String(val));
    // Push stored value to server immediately (capture may already be live)
    fetch('/api/audio/gain', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(ch === 'lb' ? { lb_gain: val } : { mic_gain: val }),
    }).catch(() => {});
  }
}

/* ── Model config ────────────────────────────────────────────────────────── */
function toggleSidebarPane(key) {
  const body  = document.getElementById('pane-body-' + key);
  const arrow = document.getElementById('pane-arrow-' + key);
  if (!body) return;
  const hidden = body.classList.toggle('hidden');
  if (arrow) arrow.innerHTML = hidden
    ? '<i class="fa-solid fa-chevron-right"></i>'
    : '<i class="fa-solid fa-chevron-down"></i>';
  // Persist collapsed state
  try {
    const collapsed = JSON.parse(localStorage.getItem('sidebar-panes') || '{}');
    collapsed[key] = hidden;
    localStorage.setItem('sidebar-panes', JSON.stringify(collapsed));
  } catch (_) {}
}

function _restoreSidebarPanes() {
  try {
    const collapsed = JSON.parse(localStorage.getItem('sidebar-panes') || '{}');
    for (const [key, isCollapsed] of Object.entries(collapsed)) {
      const body  = document.getElementById('pane-body-' + key);
      const arrow = document.getElementById('pane-arrow-' + key);
      if (!body) continue;
      body.classList.toggle('hidden', isCollapsed);
      if (arrow) arrow.innerHTML = isCollapsed
        ? '<i class="fa-solid fa-chevron-right"></i>'
        : '<i class="fa-solid fa-chevron-down"></i>';
    }
  } catch (_) {}
}

async function loadModelConfig() {
  try {
    const data = await fetch('/api/models').then(r => r.json());

    const wSel = document.getElementById('whisper-preset-sel');
    wSel.innerHTML = '';
    for (const p of data.whisper.presets) {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.label;
      opt.disabled = !p.available;
      if (p.id === data.whisper.current) opt.selected = true;
      wSel.appendChild(opt);
    }

    const dSel = document.getElementById('diarizer-device-sel');
    dSel.innerHTML = '';
    const enabledRow = document.getElementById('diarization-enabled-row');
    const enabledBtn = document.getElementById('diarization-toggle-btn');
    if (!data.diarizer.has_key) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'Disabled (no HF key)';
      opt.disabled = true;
      opt.selected = true;
      dSel.appendChild(opt);
      dSel.disabled = true;
      if (enabledRow) enabledRow.classList.add('hidden');
    } else {
      dSel.disabled = false;
      for (const o of data.diarizer.options) {
        const opt = document.createElement('option');
        opt.value = o.id;
        opt.textContent = o.label;
        opt.disabled = !o.available;
        if (o.id === data.diarizer.current) opt.selected = true;
        dSel.appendChild(opt);
      }
      if (enabledRow) enabledRow.classList.remove('hidden');
      if (enabledBtn) {
        const on = data.diarizer.enabled !== false;
        enabledBtn.textContent = on ? 'On' : 'Off';
        enabledBtn.classList.toggle('active', on);
      }
    }
  } catch (_) {}
}

async function changeWhisperPreset(presetId) {
  const sel = document.getElementById('whisper-preset-sel');
  sel.disabled = true;
  try {
    const resp = await fetch('/api/models/whisper', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset_id: presetId }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      uiToast({ message: err.error || 'Failed to change model', kind: 'error' });
      loadModelConfig();  // revert selection
    }
  } catch (e) {
    uiToast({ message: 'Failed to change model', kind: 'error' });
    loadModelConfig();
  } finally {
    sel.disabled = false;
  }
}

async function changeDiarizerDevice(device) {
  const sel = document.getElementById('diarizer-device-sel');
  sel.disabled = true;
  try {
    const resp = await fetch('/api/models/diarizer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      uiToast({ message: err.error || 'Failed to change diarizer', kind: 'error' });
      loadModelConfig();
    }
  } catch (e) {
    uiToast({ message: 'Failed to change diarizer', kind: 'error' });
    loadModelConfig();
  } finally {
    sel.disabled = false;
  }
}

async function toggleDiarizationEnabled() {
  const btn = document.getElementById('diarization-toggle-btn');
  const newEnabled = !btn.classList.contains('active');
  try {
    await fetch('/api/models/diarizer/enabled', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: newEnabled }),
    });
    btn.textContent = newEnabled ? 'On' : 'Off';
    btn.classList.toggle('active', newEnabled);
  } catch (_) {
    uiToast({ message: 'Failed to toggle diarization', kind: 'error' });
  }
}

function toggleAutoSummary() {
  const btn = document.getElementById('auto-summary-btn');
  const newEnabled = !btn.classList.contains('active');
  btn.classList.toggle('active', newEnabled);
  savePref('auto_summary', newEnabled);
}

function updateAutoScrollBtn() {
  const btn = document.getElementById('auto-scroll-btn');
  if (btn) btn.classList.toggle('active', _autoScroll);
}

function toggleAutoScroll() {
  _autoScroll = !_autoScroll;
  updateAutoScrollBtn();
  if (_autoScroll) {
    if (_playbackActive && _currentPlayingSeg) {
      _doProgrammaticScroll(_currentPlayingSeg, { behavior: 'smooth', block: 'center' });
    } else {
      const el = document.getElementById('transcript');
      el.scrollTop = el.scrollHeight;
    }
  }
}

/* ── Settings modal ──────────────────────────────────────────────────────── */

// Fallback model lists - used only if the backend hasn't returned its live
// /models fetch yet (first paint before /api/ai_settings resolves). The
// authoritative list lives on the server and auto-updates as providers ship
// new versions; new Claude / GPT releases appear here without any code change.
const AI_MODELS = {
  anthropic: [
    { id: 'claude-opus-4-6',           label: 'Opus 4.6' },
    { id: 'claude-sonnet-4-6',         label: 'Sonnet 4.6' },
    { id: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5' },
  ],
  openai: [
    { id: 'gpt-5.4',              label: 'GPT-5.4' },
    { id: 'gpt-5.3-chat-latest',  label: 'GPT-5.3 chat (latest)' },
    { id: 'gpt-4o',               label: 'GPT-4o' },
    { id: 'gpt-4o-mini',          label: 'GPT-4o mini' },
  ],
};
let currentAiModels = { ...AI_MODELS };

function _getAiModels(cfgModels) {
  return cfgModels && typeof cfgModels === 'object' ? cfgModels : AI_MODELS;
}

function _providerLabel(provider) {
  return provider === 'openai' ? 'OpenAI' : 'Anthropic';
}

function _modelLabel(provider, model, modelsByProvider = currentAiModels) {
  const models = modelsByProvider[provider] || [];
  return models.find(m => m.id === model)?.label || model || '';
}

function updateChatModelLabel(provider, model, modelsByProvider = currentAiModels) {
  const el = document.getElementById('chat-model-label');
  if (!el) return;
  const modelText = _modelLabel(provider, model, modelsByProvider);
  el.textContent = modelText
    ? `${_providerLabel(provider)} - ${modelText}`
    : _providerLabel(provider);
}

async function openSettings(section) {
  closeMenu();
  const overlay = document.getElementById('settings-overlay');
  overlay.classList.remove('hidden');
  if (section) {
    const navBtn = document.querySelector(`.settings-nav-item[data-target="section-${section}"]`);
    if (navBtn) switchSettingsSection(navBtn);
  }

  try {
    const [status, aiCfg] = await Promise.all([
      fetch('/api/settings/status').then(r => r.json()),
      fetch('/api/ai_settings').then(r => r.json()),
    ]);

    // CUDA status
    const cudaEl = document.getElementById('settings-cuda-status');
    cudaEl.textContent = status.cuda_available ? 'Available' : 'Not available';
    cudaEl.className = 'settings-info-val ' + (status.cuda_available ? 'val-ok' : 'val-warn');

    // Show setup hint on first run
    document.getElementById('settings-setup-hint').style.display =
      status.needs_setup ? '' : 'none';

    // Key statuses
    _renderKeyStatus('ANTHROPIC_API_KEY', 'key-anthropic', status.keys);
    _renderKeyStatus('OPENAI_API_KEY',    'key-openai',    status.keys);
    _renderKeyStatus('HUGGING_FACE_KEY',  'key-huggingface', status.keys);

    // AI provider + model
    currentAiModels = { ...AI_MODELS, ..._getAiModels(aiCfg.models) };
    _currentAiProvider = aiCfg.provider;
    _currentAiModel = aiCfg.model;
    _applyAiConfig(aiCfg.provider, aiCfg.model, currentAiModels);

    // Per-tool overrides
    _toolOverrides.summary_provider = aiCfg.summary_provider || null;
    _toolOverrides.summary_model = aiCfg.summary_model || null;
    _toolOverrides.chat_provider = aiCfg.chat_provider || null;
    _toolOverrides.chat_model = aiCfg.chat_model || null;
    _toolOverrides.global_chat_provider = aiCfg.global_chat_provider || null;
    _toolOverrides.global_chat_model = aiCfg.global_chat_model || null;

    const anthSet = !!(status.keys?.ANTHROPIC_API_KEY?.is_set);
    const oaiSet = !!(status.keys?.OPENAI_API_KEY?.is_set);
    _bothKeysSet = anthSet && oaiSet;
    _applyToolOverrides();
    _updateSessionModelLabels();
    _renderQuietReminderSettings();
    _renderMeetingDetectSettings();
    _renderWarpSettings();
    _renderMicIsMeSettings();
  } catch (_) {}

  // Startup toggle (Windows only - hidden on unsupported platforms)
  try {
    const startup = await fetch('/api/settings/startup').then(r => r.json());
    const row = document.getElementById('startup-row');
    if (startup.supported) {
      row.style.display = '';
      document.getElementById('startup-toggle').checked = startup.enabled;
    } else {
      row.style.display = 'none';
    }
  } catch (_) {}

  // Audio params - load eagerly so panels are ready when clicked
  _apRefresh().then(() => _syncScreenToggle());

  // Presets for all sections
  loadTranscriptionPresets();
  loadDiarizationPresets();
  loadScreenPresets();
  loadScreenDisplays();
  loadDataFolder();
  loadObsidianSettings();
}

function _renderQuietReminderSettings() {
  const enabled = document.getElementById('quiet-prompt-enabled');
  if (!enabled) return;
  enabled.checked = _prefs.quiet_prompt_enabled !== false;
  document.getElementById('quiet-prompt-threshold').value = _prefs.quiet_prompt_threshold_sec ?? 30;
  document.getElementById('quiet-prompt-rms').value = _prefs.quiet_prompt_audio_rms_threshold ?? 0.006;
  document.getElementById('quiet-prompt-transcript').checked = _prefs.quiet_prompt_require_no_transcript !== false;
  document.getElementById('quiet-prompt-cooldown').value = _prefs.quiet_prompt_cooldown_sec ?? 120;
}

function saveQuietReminderSettings() {
  const updates = {
    quiet_prompt_enabled: document.getElementById('quiet-prompt-enabled')?.checked !== false,
    quiet_prompt_threshold_sec: parseFloat(document.getElementById('quiet-prompt-threshold')?.value || '30'),
    quiet_prompt_audio_rms_threshold: parseFloat(document.getElementById('quiet-prompt-rms')?.value || '0.006'),
    quiet_prompt_require_no_transcript: document.getElementById('quiet-prompt-transcript')?.checked !== false,
    quiet_prompt_cooldown_sec: parseFloat(document.getElementById('quiet-prompt-cooldown')?.value || '120'),
  };
  Object.assign(_prefs, updates);
  fetch('/api/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  }).catch(() => {});
}

function _renderMeetingDetectSettings() {
  const enabled = document.getElementById('meeting-detect-enabled');
  if (!enabled) return;
  const on = _prefs.meeting_detect_enabled === true;
  enabled.checked = on;
  const cd = document.getElementById('meeting-detect-cooldown');
  if (cd) cd.value = _prefs.meeting_detect_cooldown_sec ?? 90;
  // Auto-start only does anything while auto-detect is on, so disable it then.
  const auto = document.getElementById('meeting-detect-autostart');
  if (auto) {
    auto.checked = _prefs.meeting_detect_autostart === true;
    auto.disabled = !on;
  }
}

function saveMeetingDetectSettings() {
  const updates = {
    meeting_detect_enabled: document.getElementById('meeting-detect-enabled')?.checked === true,
    meeting_detect_autostart: document.getElementById('meeting-detect-autostart')?.checked === true,
    meeting_detect_cooldown_sec: parseFloat(document.getElementById('meeting-detect-cooldown')?.value || '90'),
  };
  Object.assign(_prefs, updates);
  _renderMeetingDetectSettings();   // reflect the enabled/disabled dependency immediately
  fetch('/api/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  }).catch(() => {});
}

function _renderWarpSettings() {
  const enabled = document.getElementById('warp-toggle-enabled');
  if (!enabled) return;
  enabled.checked = _prefs.warp_toggle_enabled === true;
}

function saveWarpSettings() {
  const updates = {
    warp_toggle_enabled: document.getElementById('warp-toggle-enabled')?.checked === true,
  };
  Object.assign(_prefs, updates);
  fetch('/api/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  }).catch(() => {});
}

function _renderMicIsMeSettings() {
  const enabled = document.getElementById('mic-is-me-enabled');
  if (enabled) enabled.checked = _prefs.mic_is_me_enabled !== false;  // default on
  const cur = document.getElementById('me-speaker-current');
  if (cur) {
    cur.textContent = window._meSpeakerName
      ? `You: ${window._meSpeakerName}`
      : 'Not set yet: your mic uses a default "You" label until you choose.';
  }
}

function saveMicIsMeSettings() {
  const updates = {
    mic_is_me_enabled: document.getElementById('mic-is-me-enabled')?.checked === true,
  };
  Object.assign(_prefs, updates);
  fetch('/api/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  }).catch(() => {});
}

/** Sync provider toggle buttons and model dropdown to the given values. */
function _applyAiConfig(provider, model, modelsByProvider = AI_MODELS) {
  // Provider buttons
  document.getElementById('provider-btn-anthropic').classList.toggle('active', provider === 'anthropic');
  document.getElementById('provider-btn-openai').classList.toggle('active', provider === 'openai');

  // Rebuild model dropdown for this provider
  const sel = document.getElementById('ai-model-sel');
  const models = modelsByProvider[provider] || [];
  const selectedModel = models.some(m => m.id === model)
    ? model
    : (models[0]?.id || '');
  sel.innerHTML = '';
  models.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m.id;
    opt.textContent = m.label;
    if (m.id === selectedModel) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.disabled = models.length === 0;

}

async function setAiProvider(provider) {
  try {
    const data = await fetch('/api/ai_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider }),
    }).then(r => r.json());
    const modelsData = await fetch(`/api/ai_settings/models?provider=${encodeURIComponent(data.provider)}`)
      .then(r => r.json());
    currentAiModels = {
      ...currentAiModels,
      [modelsData.provider]: modelsData.models || [],
    };
    _currentAiProvider = data.provider;
    _currentAiModel = data.model;
    _applyAiConfig(data.provider, data.model, currentAiModels);
    _applyToolOverrides();
    _updateSessionModelLabels();
  } catch (_) {}
}

async function setAiModel(model) {
  try {
    const data = await fetch('/api/ai_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    }).then(r => r.json());
    _currentAiProvider = data.provider;
    _currentAiModel = data.model;
    _applyAiConfig(data.provider, data.model, currentAiModels);
    _applyToolOverrides();
    _updateSessionModelLabels();
  } catch (_) {}
}

async function refreshAiModels() {
  // Force the backend to drop its /models cache and re-fetch both providers.
  // Shows a spinner on the refresh button while we wait.
  const btn = document.getElementById('ai-model-refresh-btn');
  const icon = btn?.querySelector('i');
  const prevClass = icon?.className;
  if (btn) btn.disabled = true;
  if (icon) icon.className = 'fa-solid fa-rotate fa-spin';
  try {
    const r = await fetch('/api/ai_settings/models/refresh', { method: 'POST' })
      .then(r => r.json());
    if (r.models) {
      currentAiModels = { ...AI_MODELS, ...r.models };
      // Re-render the model dropdown. If the user's current pick has been
      // replaced by a newer alias (e.g. 4-6 → 4-7), we persist the new one.
      const provider = _currentAiProvider;
      const validIds = (currentAiModels[provider] || []).map(m => m.id);
      let model = _currentAiModel;
      if (!validIds.includes(model) && validIds.length) model = validIds[0];
      _applyAiConfig(provider, model, currentAiModels);
      if (model !== _currentAiModel) {
        // Persist the new default to the server
        await setAiModel(model);
      }
      _applyToolOverrides();
      _updateSessionModelLabels();
      flashStatus('Model list refreshed');
    }
  } catch (e) {
    flashStatus('Refresh failed: ' + (e.message || e));
  } finally {
    if (btn) btn.disabled = false;
    if (icon) icon.className = prevClass || 'fa-solid fa-rotate';
  }
}

/* ── Per-tool provider/model overrides ──────────────────────────────── */

let _toolOverrides = {
  summary_provider: null, summary_model: null,
  chat_provider: null, chat_model: null,
  global_chat_provider: null, global_chat_model: null,
};
let _bothKeysSet = false;

function _effectiveProvider(tool) {
  return _toolOverrides[tool + '_provider'] || _currentAiProvider;
}
function _effectiveModel(tool) {
  const p = _effectiveProvider(tool);
  const m = _toolOverrides[tool + '_model'];
  if (m) {
    const models = currentAiModels[p] || [];
    if (models.some(x => x.id === m)) return m;
  }
  if (p === _currentAiProvider) return _currentAiModel;
  const models = currentAiModels[p] || AI_MODELS[p] || [];
  return models[0]?.id || '';
}

let _currentAiProvider = 'openai';
let _currentAiModel = '';

async function setToolProvider(tool, provider) {
  try {
    const data = await fetch('/api/ai_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool, provider }),
    }).then(r => r.json());
    _toolOverrides.summary_provider = data.summary_provider;
    _toolOverrides.summary_model = data.summary_model;
    _toolOverrides.chat_provider = data.chat_provider;
    _toolOverrides.chat_model = data.chat_model;
    _toolOverrides.global_chat_provider = data.global_chat_provider;
    _toolOverrides.global_chat_model = data.global_chat_model;
    _applyToolOverrides();
    _updateSessionModelLabels();
  } catch (_) {}
}

async function setToolModel(tool, model) {
  try {
    const data = await fetch('/api/ai_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool, model }),
    }).then(r => r.json());
    _toolOverrides.summary_provider = data.summary_provider;
    _toolOverrides.summary_model = data.summary_model;
    _toolOverrides.chat_provider = data.chat_provider;
    _toolOverrides.chat_model = data.chat_model;
    _toolOverrides.global_chat_provider = data.global_chat_provider;
    _toolOverrides.global_chat_model = data.global_chat_model;
    _applyToolOverrides();
    _updateSessionModelLabels();
  } catch (_) {}
}

function _applyToolOverrides() {
  const group = document.getElementById('tool-overrides-group');
  if (!group) return;

  if (_bothKeysSet) {
    group.classList.remove('disabled');
    const existingHint = group.querySelector('.tool-overrides-hint');
    if (existingHint) existingHint.remove();
  } else {
    group.classList.add('disabled');
    if (!group.querySelector('.tool-overrides-hint')) {
      const hint = document.createElement('div');
      hint.className = 'tool-overrides-hint';
      hint.textContent = 'Set both Anthropic and OpenAI keys to enable per-tool overrides';
      group.appendChild(hint);
    }
  }

  for (const tool of ['summary', 'chat']) {
    const prov = _toolOverrides[tool + '_provider'];
    for (const p of ['default', 'anthropic', 'openai']) {
      const btn = document.getElementById(`${tool}-provider-btn-${p}`);
      if (btn) btn.classList.toggle('active',
        p === 'default' ? !prov : prov === p);
    }

    const sel = document.getElementById(`${tool}-model-sel`);
    if (!sel) continue;
    const effectiveProv = prov || _currentAiProvider;
    const models = currentAiModels[effectiveProv] || AI_MODELS[effectiveProv] || [];
    const currentModel = _effectiveModel(tool);
    sel.innerHTML = '';
    if (!prov) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '(Use primary model)';
      opt.selected = true;
      sel.appendChild(opt);
      sel.disabled = true;
    } else {
      sel.disabled = false;
      models.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m.id;
        opt.textContent = m.label;
        if (m.id === currentModel) opt.selected = true;
        sel.appendChild(opt);
      });
    }
  }
}

function _updateSessionModelLabels() {
  const sp = _effectiveProvider('summary');
  const sm = _effectiveModel('summary');
  const summaryLabel = document.getElementById('summary-model-picker-label');
  if (summaryLabel) {
    summaryLabel.textContent = _modelLabel(sp, sm);
  }

  const cp = _effectiveProvider('chat');
  const cm = _effectiveModel('chat');
  updateChatModelLabel(cp, cm, currentAiModels);

  const gLabel = document.getElementById('global-chat-model-label');
  if (gLabel) {
    const gp = _effectiveProvider('global_chat');
    const gm = _effectiveModel('global_chat');
    const modelText = _modelLabel(gp, gm, currentAiModels);
    gLabel.textContent = modelText
      ? `${_providerLabel(gp)} - ${modelText}`
      : _providerLabel(gp);
  }
}

/* ── Model picker popout (session page) ────────────────────────────── */

function _modelPickerIds(tool) {
  if (tool === 'global_chat') {
    return { btn: 'global-chat-model-btn', panel: 'global-chat-model-picker' };
  }
  return { btn: `${tool}-model-btn`, panel: `${tool}-model-picker` };
}

function toggleModelPicker(tool) {
  const { btn: btnId, panel: panelId } = _modelPickerIds(tool);
  const panel = document.getElementById(panelId);
  if (!panel) return;
  const wasHidden = panel.classList.contains('hidden');
  document.querySelectorAll('.model-picker-panel').forEach(p => p.classList.add('hidden'));
  if (wasHidden) {
    _buildModelPickerPanel(tool);
    // Reparent to <body> so ``position: fixed`` always resolves against the
    // viewport. Some ancestors (e.g. ``.home-chat-panel``) use ``transform``
    // for animations, which promotes them to the containing block for fixed
    // descendants - that shifts our coordinates and the panel lands in the
    // wrong place.
    if (panel.parentElement !== document.body) {
      document.body.appendChild(panel);
    }
    panel.classList.remove('hidden');
    _positionModelPicker(tool, panel);
    const close = (e) => {
      if (!panel.contains(e.target) && !e.target.closest('#' + btnId)) {
        panel.classList.add('hidden');
        document.removeEventListener('pointerdown', close);
      }
    };
    setTimeout(() => document.addEventListener('pointerdown', close), 0);
  }
}

function _positionModelPicker(tool, panel) {
  const { btn: btnId } = _modelPickerIds(tool);
  const btn = document.getElementById(btnId);
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const pw = panel.offsetWidth || 220;
  const ph = panel.offsetHeight || 220;

  // Vertical: chat-style pickers open upward from the button; the Summary
  // picker opens downward since it lives in the top of its column.
  let top;
  if (tool === 'chat' || tool === 'global_chat') {
    top = r.top - ph - 4;
  } else {
    top = r.bottom + 4;
  }

  // Horizontal: prefer right-aligning the panel with the button, then clamp
  // so it can't escape the viewport. Clamping matters when the button sits
  // near the left edge - e.g. Global Chat on the home page - where naive
  // right-anchoring would push the panel far off-screen.
  let left = r.right - pw;

  top  = Math.max(4, Math.min(top,  vh - ph - 4));
  left = Math.max(4, Math.min(left, vw - pw - 4));

  panel.style.top = top + 'px';
  panel.style.left = left + 'px';
  panel.style.right = 'auto';
  panel.style.bottom = 'auto';
}

function _buildModelPickerPanel(tool) {
  const { panel: panelId } = _modelPickerIds(tool);
  const panel = document.getElementById(panelId);
  if (!panel) return;
  panel.innerHTML = '';

  const currentProv = _effectiveProvider(tool);
  const currentModel = _effectiveModel(tool);

  for (const prov of ['anthropic', 'openai']) {
    const models = currentAiModels[prov] || AI_MODELS[prov] || [];
    if (!models.length) continue;
    const section = document.createElement('div');
    section.className = 'model-picker-section';
    const label = document.createElement('div');
    label.className = 'model-picker-section-label';
    label.textContent = _providerLabel(prov);
    section.appendChild(label);
    for (const m of models) {
      const item = document.createElement('div');
      item.className = 'model-picker-item';
      const isSelected = prov === currentProv && m.id === currentModel;
      if (isSelected) item.classList.add('selected');
      item.innerHTML =
        `<span class="mp-check">${isSelected ? '<i class="fa-solid fa-check"></i>' : ''}</span>` +
        `<span>${m.label}</span>`;
      item.addEventListener('click', () => {
        _selectModelFromPicker(tool, prov, m.id);
        panel.classList.add('hidden');
      });
      section.appendChild(item);
    }
    panel.appendChild(section);
  }
}

async function _selectModelFromPicker(tool, provider, model) {
  try {
    // Each inline picker sets its own per-tool override so Summary,
    // Session Chat, and Global Chat can each use a different model.
    const data = await fetch('/api/ai_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tool, provider, model }),
    }).then(r => r.json());
    _toolOverrides.summary_provider = data.summary_provider;
    _toolOverrides.summary_model = data.summary_model;
    _toolOverrides.chat_provider = data.chat_provider;
    _toolOverrides.chat_model = data.chat_model;
    _toolOverrides.global_chat_provider = data.global_chat_provider;
    _toolOverrides.global_chat_model = data.global_chat_model;
    if (typeof _applyToolOverrides === 'function') _applyToolOverrides();
    _updateSessionModelLabels();
  } catch (_) {}
}

function _renderKeyStatus(keyName, inputId, keys) {
  const info = keys[keyName] || {};
  const statusEl = document.getElementById(inputId + '-status');
  const inputEl  = document.getElementById(inputId);
  if (!statusEl || !inputEl) return;

  // Update the req/opt badge
  const reqEl = document.getElementById(inputId + '-req');
  const optEl = document.getElementById(inputId.replace('key-', 'key-') + '-req');
  const keyRow = inputEl.closest('.settings-key-row');
  const linkEl = keyRow?.querySelector('.settings-key-link');

  if (info.is_set) {
    statusEl.textContent = '';
    statusEl.className = 'key-status key-set';
    // Show full key in the input field (concealed as password dots)
    inputEl.value = info.value;
    inputEl.type = 'password';
    inputEl.placeholder = info.hint || '';
    _origKeyValues[inputId] = info.value;
    // Update badge to "Provided"
    if (reqEl) {
      reqEl.textContent = 'provided';
      reqEl.className = 'settings-req key-provided';
    }
    // Hide "Get a key" link
    if (linkEl) linkEl.style.display = 'none';
  } else {
    statusEl.textContent = info.required ? 'Not set' : 'Not set - optional';
    statusEl.className = 'key-status ' + (info.required ? 'key-missing' : 'key-optional');
    inputEl.value = '';
    inputEl.placeholder = info.hint || '';
    // Restore badge
    if (reqEl) {
      reqEl.textContent = info.required ? 'required' : 'optional';
      reqEl.className = info.required ? 'settings-req' : 'settings-opt';
    }
    // Show "Get a key" link
    if (linkEl) linkEl.style.display = '';
  }
}

function closeSettings() {
  document.getElementById('settings-overlay').classList.add('hidden');
  // Reset password fields to hidden state
  ['key-anthropic', 'key-openai', 'key-huggingface'].forEach(id => {
    const el = document.getElementById(id);
    el.type = 'password';
    const btn = el.parentElement.querySelector('.key-vis-btn');
    if (btn) btn.innerHTML = '<i class="fa-solid fa-eye"></i>';
  });
  // Reset update button
  const btn = document.getElementById('check-update-btn');
  btn.disabled = false;
  btn.textContent = 'Check for Updates';
  btn.onclick = checkForUpdates;
  document.getElementById('settings-update-status').textContent = '';
  document.getElementById('settings-update-status').className = 'settings-info-val';
}

async function checkForUpdates() {
  const btn = document.getElementById('check-update-btn');
  const statusEl = document.getElementById('settings-update-status');
  btn.disabled = true;
  btn.textContent = 'Checking...';
  statusEl.textContent = '';
  statusEl.className = 'settings-info-val';

  try {
    const res = await fetch('/api/update/check');
    const data = await res.json();

    if (data.error) {
      statusEl.textContent = data.error;
      statusEl.className = 'settings-info-val val-warn';
      btn.disabled = false;
      btn.textContent = 'Check for Updates';
      return;
    }

    if (data.up_to_date) {
      statusEl.textContent = 'Up to date';
      statusEl.className = 'settings-info-val val-ok';
      btn.disabled = false;
      btn.textContent = 'Check for Updates';
      // Clear the update-available state in the App menu
      _clearAppMenuUpdate();
    } else {
      statusEl.textContent = `${data.commits_behind} update${data.commits_behind !== 1 ? 's' : ''} available`;
      statusEl.className = 'settings-info-val val-warn';
      btn.disabled = false;
      btn.textContent = 'Update & Restart';
      btn.onclick = applyUpdate;
      // Also show topbar update button
      _showTopbarUpdate(data.commits_behind);
    }
  } catch (_) {
    statusEl.textContent = 'Check failed';
    statusEl.className = 'settings-info-val val-warn';
    btn.disabled = false;
    btn.textContent = 'Check for Updates';
  }
}

async function applyUpdate() {
  const btn = document.getElementById('check-update-btn');
  const statusEl = document.getElementById('settings-update-status');
  btn.disabled = true;
  btn.textContent = 'Updating...';
  statusEl.textContent = 'Pulling latest changes...';
  statusEl.className = 'settings-info-val';

  // The App menu item is the same action; keep the two in step
  const tbBtn = document.getElementById('app-update-item');
  const tbLabel = tbBtn && tbBtn.querySelector('.menu-item-label');
  if (tbBtn) tbBtn.disabled = true;
  if (tbLabel) tbLabel.textContent = 'Installing the update…';

  try {
    const res = await fetch('/api/update/apply', { method: 'POST' });
    const data = await res.json();

    if (data.error) {
      statusEl.textContent = data.error;
      statusEl.className = 'settings-info-val val-warn';
      btn.disabled = false;
      btn.textContent = 'Retry Update';
      if (tbBtn) tbBtn.disabled = false;
      if (tbLabel) tbLabel.textContent = 'Retry the update';
    } else {
      statusEl.textContent = 'Restarting...';
      btn.textContent = 'Restarting...';
      if (tbLabel) tbLabel.textContent = 'Restarting…';
      // The server re-reads CHANGELOG.md whenever the file changes; flip
      // the in-memory guard so the Changelog tab refetches after the restart.
      _changelogLoaded = false;
      _pollUntilBack();
    }
  } catch (_) {
    statusEl.textContent = 'Update failed';
    statusEl.className = 'settings-info-val val-warn';
    btn.disabled = false;
    btn.textContent = 'Retry Update';
    if (tbBtn) { tbBtn.disabled = false; tbBtn.innerHTML = '<i class="fa-solid fa-download"></i> Retry'; }
  }
}

function _pollUntilBack() {
  // Give the server a moment to begin shutting down before we start polling.
  // Once the server is back, refresh the page to pick up any new code.
  setTimeout(async () => {
    for (;;) {
      await new Promise(r => setTimeout(r, 1500));
      try {
        const r = await fetch('/api/settings/status');
        if (r.ok) {
          location.reload();
          return;
        }
      } catch (_) { /* server still down, keep polling */ }
    }
  }, 2000);
}

// ── Topbar update indicator ──────────────────────────────────────────────

/** Back to "Check for updates" once there is nothing to install. */
function _clearAppMenuUpdate() {
  const item = document.getElementById('app-update-item');
  if (!item) return;
  delete item.dataset.available;
  item.removeAttribute('title');
  const label = item.querySelector('.menu-item-label');
  if (label) label.textContent = 'Check for updates';
  document.getElementById('app-menu-dot')?.classList.add('hidden');
}

/** The App menu item carries the update-available state, and the footer button
 *  carries a dot so the news is visible without opening the menu. */
function _showTopbarUpdate(commitsBehind) {
  const item = document.getElementById('app-update-item');
  if (!item) return;
  const s = commitsBehind !== 1 ? 's' : '';
  item.dataset.available = '1';
  item.title = `${commitsBehind} update${s} available`;
  const label = item.querySelector('.menu-item-label');
  if (label) label.textContent = `Install ${commitsBehind} update${s} and restart`;
  document.getElementById('app-menu-dot')?.classList.remove('hidden');
}

async function topbarApplyUpdate() {
  const item = document.getElementById('app-update-item');
  const label = item && item.querySelector('.menu-item-label');
  if (item) item.disabled = true;
  if (label) label.textContent = 'Installing the update…';

  try {
    const res = await fetch('/api/update/apply', { method: 'POST' });
    const data = await res.json();

    if (data.error) {
      if (item) { item.disabled = false; item.title = `Update failed: ${data.error}`; }
      if (label) label.textContent = 'Retry the update';
      uiToast({ message: `Update failed: ${data.error}`, kind: 'error' });
    } else {
      if (label) label.textContent = 'Restarting…';
      _changelogLoaded = false;
      _pollUntilBack();
    }
  } catch (_) {
    if (item) { item.disabled = false; item.title = 'Update failed. Try again.'; }
    if (label) label.textContent = 'Retry the update';
    uiToast({ message: 'Update failed. Try again.', kind: 'error' });
  }
}

// Silent update check - shows the topbar button only if updates are found.
// Errors are silently ignored.
async function _silentUpdateCheck() {
  try {
    const res = await fetch('/api/update/check');
    const data = await res.json();
    if (!data.error && !data.up_to_date && data.commits_behind > 0) {
      _showTopbarUpdate(data.commits_behind);
    }
  } catch (_) { /* silent - don't bother the user if offline */ }
}

// Periodic update check - runs every 15 minutes, but only when idle
// (no recording in progress).  Stops once an update is found.
let _updateCheckInterval = null;
function _startPeriodicUpdateCheck() {
  // Run once on startup
  _silentUpdateCheck();
  // Then every 15 minutes while idle
  _updateCheckInterval = setInterval(() => {
    // Skip once an update is already announced, or while recording
    if (document.getElementById('app-update-item')?.dataset.available === '1') return;
    if (state.isRecording) return;
    _silentUpdateCheck();
  }, 15 * 60 * 1000);
}

function switchSettingsSection(btn) {
  document.querySelectorAll('.settings-nav-item').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(btn.dataset.target).classList.add('active');
  // Lazy-load the Changelog tab the first time it's opened so the git-log
  // shell-out doesn't run on app startup or affect other tabs.
  if (btn.dataset.target === 'section-changelog' && !_changelogLoaded) {
    loadChangelog(false);
  }
  // Lazy-load the Agent API tab (fetches connection info + config snippets)
  if (btn.dataset.target === 'section-agent-api') {
    loadAgentApiPanel();
  }
  // Lazy-load the Calendar tab (feed status; the link itself stays masked)
  if (btn.dataset.target === 'section-calendar') {
    loadCalendarStatus();
  }
  if (btn.dataset.target === 'section-icons') {
    loadIconSettings();
  }
  if (btn.dataset.target === 'section-system') {
    _syncReliabilityToggles();
  }
}

/* ── Settings: Agent API tab ──────────────────────────────────────────────── */

let _agentApiInfo = null;

async function loadAgentApiPanel() {
  const enabledToggle = document.getElementById('agent-api-enabled');
  const recToggle     = document.getElementById('agent-api-rec-control');
  if (enabledToggle) enabledToggle.checked = _prefs.agent_api_enabled !== false;
  if (recToggle)     recToggle.checked     = !!_prefs.agent_api_allow_recording_control;
  _syncAgentApiTokenUI();

  const urlEl = document.getElementById('agent-api-url');
  try {
    if (!_agentApiInfo) {
      _agentApiInfo = await fetch('/api/agent/v1/').then(r => r.json());
    }
    const info = _agentApiInfo;
    if (urlEl) urlEl.textContent = info.base_url;
    const selftest = document.getElementById('agent-api-selftest');
    if (selftest && info.mcp) {
      selftest.textContent = 'Self-test: ' + (info.mcp.selftest || '');
    }
    document.querySelectorAll('.agent-config-code').forEach(pre => {
      const snippet = info.mcp && info.mcp.configs && info.mcp.configs[pre.dataset.key];
      pre.textContent = snippet || 'Unavailable';
    });
  } catch {
    if (urlEl) urlEl.textContent = 'Could not reach the Agent API.';
  }
}

function _syncAgentApiTokenUI() {
  const stateEl  = document.getElementById('agent-api-token-state');
  const clearBtn = document.getElementById('agent-api-token-clear');
  const genBtn   = document.getElementById('agent-api-token-gen');
  const token = (_prefs.agent_api_token || '').trim();
  if (stateEl) {
    stateEl.textContent = token
      ? `Token required: ${token.slice(0, 8)}…  (agents send it as a Bearer header)`
      : 'No token: any local process may connect.';
  }
  if (clearBtn) clearBtn.style.display = token ? '' : 'none';
  if (genBtn)   genBtn.textContent = token ? 'Regenerate' : 'Generate';
}

function agentApiSetEnabled(on) {
  savePref('agent_api_enabled', !!on);
}

function agentApiSetRecControl(on) {
  savePref('agent_api_allow_recording_control', !!on);
}

function agentApiGenerateToken() {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  const token = 'ma_' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  savePref('agent_api_token', token);
  _syncAgentApiTokenUI();
  navigator.clipboard?.writeText(token).catch(() => {});
  const stateEl = document.getElementById('agent-api-token-state');
  if (stateEl) stateEl.textContent = `Token set and copied to clipboard: ${token}`;
}

function agentApiClearToken() {
  savePref('agent_api_token', '');
  _syncAgentApiTokenUI();
}

function agentApiOpenDocs() {
  window.open('/api/agent/v1/docs', '_blank');
}

async function agentApiCopy(key, btn) {
  const info = _agentApiInfo;
  const snippet = info && info.mcp && info.mcp.configs && info.mcp.configs[key];
  if (!snippet) return;
  try {
    await navigator.clipboard.writeText(snippet);
    const orig = btn.innerHTML;
    btn.innerHTML = '<i class="fa-solid fa-check"></i>';
    setTimeout(() => { btn.innerHTML = orig; }, 1500);
  } catch {}
}

function _agentApiHeaders() {
  const token = (_prefs.agent_api_token || '').trim();
  return token ? { 'Authorization': `Bearer ${token}` } : {};
}

async function agentApiRunSetup(client, btn) {
  const resultEl = document.getElementById(`agent-setup-result-${client}`);
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Running…';
  if (resultEl) { resultEl.textContent = ''; resultEl.className = 'agent-config-result'; }
  try {
    const r = await fetch(`/api/agent/v1/setup/${client}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._agentApiHeaders() },
    });
    const data = await r.json();
    if (resultEl) {
      if (data.ok) {
        resultEl.classList.add('agent-config-result-ok');
        const parts = [`✓ Config ${data.action}`];
        if (data.path) parts.push(data.path);
        resultEl.textContent = parts.join(': ') + (data.note ? ` · ${data.note}` : '');
        if (data.backup) resultEl.textContent += ` (backup: ${data.backup})`;
      } else {
        resultEl.classList.add('agent-config-result-err');
        resultEl.textContent = `✗ ${data.error || 'Setup failed.'}`;
      }
    }
    btn.innerHTML = data.ok ? '<i class="fa-solid fa-check"></i> Done'
                            : '<i class="fa-solid fa-bolt"></i> Retry';
  } catch (e) {
    if (resultEl) {
      resultEl.classList.add('agent-config-result-err');
      resultEl.textContent = `✗ Could not reach the app: ${e}`;
    }
    btn.innerHTML = '<i class="fa-solid fa-bolt"></i> Retry';
  }
  btn.disabled = false;
  setTimeout(() => { btn.innerHTML = orig; }, 4000);
}

async function agentApiTest() {
  const btn = document.getElementById('agent-api-test-btn');
  if (btn) btn.textContent = 'Testing…';
  try {
    const h = await fetch('/api/agent/v1/system/health').then(r => r.json());
    if (btn) btn.textContent = h.ok ? (h.agent_api_enabled ? 'OK ✓' : 'Disabled') : 'Failed';
  } catch {
    if (btn) btn.textContent = 'Failed';
  }
  setTimeout(() => { const b = document.getElementById('agent-api-test-btn');
                     if (b) b.textContent = 'Test'; }, 2500);
}

let _changelogLoaded = false;

async function loadChangelog(force) {
  const body = document.getElementById('changelog-body');
  const meta = document.getElementById('changelog-meta');
  const btn  = document.getElementById('changelog-refresh-btn');
  if (!body) return;
  if (btn) { btn.disabled = true; }
  if (force) body.innerHTML = '<div class="changelog-empty">Refreshing…</div>';

  try {
    const url = '/api/changelog' + (force ? '?refresh=1' : '');
    const resp = await fetch(url);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    _changelogLoaded = true;
    _renderChangelog(data);
    if (meta) {
      meta.innerHTML = '';
      if (data.modified || data.generated_at) {
        const when = document.createElement('span');
        when.className = 'changelog-meta-when';
        when.textContent = `Updated ${_formatChangelogDate(data.modified || data.generated_at)}`;
        meta.appendChild(when);
      }
      if (typeof data.count === 'number') {
        const n = document.createElement('span');
        n.className = 'changelog-meta-hash';
        n.textContent = `${data.count} ${data.count === 1 ? 'entry' : 'entries'}`;
        meta.appendChild(n);
      }
      const status = document.createElement('span');
      status.className = 'changelog-meta-status' + (data.fresh ? ' fresh' : '');
      status.innerHTML = `<i class="fa-solid fa-circle"></i> ${data.fresh ? 'Just refreshed' : 'Cached'}`;
      meta.appendChild(status);
    }
  } catch (e) {
    body.innerHTML = `<div class="changelog-error">${_escHtml(e.message || 'Failed to load changelog')}</div>`;
    if (meta) meta.textContent = '';
  } finally {
    if (btn) { btn.disabled = false; }
  }
}

function refreshChangelog() {
  loadChangelog(true);
}

const _CHANGELOG_CAT_ICONS = {
  feature:     'fa-solid fa-plus',
  fix:         'fa-solid fa-wrench',
  improvement: 'fa-solid fa-arrow-up',
  refactor:    'fa-solid fa-shuffle',
  removal:     'fa-solid fa-minus',
  other:       'fa-solid fa-circle-dot',
};

function _renderChangelog(data) {
  const body = document.getElementById('changelog-body');
  const entries = (data && Array.isArray(data.entries)) ? data.entries : [];
  if (!entries.length) {
    const why = data && data.missing ? 'CHANGELOG.md was not found in the app folder.' : 'No release notes yet.';
    body.innerHTML = `<div class="changelog-empty">${_escHtml(why)}</div>`;
    return;
  }
  // Group consecutive entries by date so the user gets a date heading per
  // chunk without rendering one per row.
  const frag = document.createDocumentFragment();
  let lastDate = null;
  for (const c of entries) {
    if (c.date !== lastDate) {
      const h = document.createElement('div');
      h.className = 'changelog-day';
      h.textContent = c.date ? _formatChangelogDate(c.date) : 'Earlier';
      frag.appendChild(h);
      lastDate = c.date;
    }
    frag.appendChild(_renderChangelogEntry(c));
  }
  body.replaceChildren(frag);
}

function _renderChangelogEntry(c) {
  const row = document.createElement('div');
  row.className = 'changelog-entry';
  row.dataset.cat = c.category || 'other';

  const icon = document.createElement('span');
  icon.className = 'changelog-entry-icon';
  icon.innerHTML = `<i class="${_CHANGELOG_CAT_ICONS[c.category] || _CHANGELOG_CAT_ICONS.other}"></i>`;

  const content = document.createElement('div');
  content.className = 'changelog-entry-content';

  const subj = document.createElement('div');
  subj.className = 'changelog-entry-subject';
  subj.textContent = c.title || '';
  content.appendChild(subj);

  const bodyText = (c.body || '').trim();
  if (bodyText) {
    const b = _renderChangelogBody(bodyText);
    content.appendChild(b);
    // Show the toggle only when the body actually overflows the collapsed
    // height. Defer the measurement to the next paint so layout is final.
    requestAnimationFrame(() => {
      if (b.scrollHeight > b.clientHeight + 1) {
        const t = document.createElement('button');
        t.type = 'button';
        t.className = 'changelog-entry-toggle';
        t.textContent = 'Show more';
        t.onclick = () => {
          const expanded = row.classList.toggle('expanded');
          t.textContent = expanded ? 'Show less' : 'Show more';
        };
        content.appendChild(t);
      }
    });
  }

  row.appendChild(icon);
  row.appendChild(content);
  return row;
}

/* Render one entry's notes. CHANGELOG.md is the project's own file, written
 * by the project, so its markdown is rendered as such: "### Area" headings
 * become the small section headings, "- " lines bullets, the rest paragraphs.
 * Links open in a new tab so the app window is never navigated away. */
function _renderChangelogBody(markdown) {
  const wrap = document.createElement('div');
  wrap.className = 'changelog-entry-body';
  let html = '';
  try { html = renderMd(markdown || ''); } catch (_) { html = ''; }
  if (!html.trim()) {
    const p = document.createElement('p');
    p.className = 'changelog-para';
    p.textContent = markdown || '';
    wrap.appendChild(p);
    return wrap;
  }
  wrap.innerHTML = html;
  wrap.querySelectorAll('a[href]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  return wrap;
}

/* ── What's New popup ─────────────────────────────────────────────────────
 * Shown once when CHANGELOG.md has a newer entry than the one last seen in
 * this browser (the user just updated). On a fresh install the newest entry
 * is anchored silently so nothing pops up on first launch. The body is the
 * entry's markdown rendered by the same _renderChangelogBody() the Changelog
 * tab uses, so styling stays shared.
 */
const _WHATS_NEW_SEEN_KEY = 'ma:lastSeenChangelogEntry';
const _WHATS_NEW_LEGACY_KEY = 'ma:lastSeenChangelogHead';   // builds that read git history

async function _checkWhatsNew() {
  // Don't surprise the user mid-recording.
  if (typeof state !== 'undefined' && state && state.isRecording) return;
  let data;
  try {
    data = await fetch('/api/changelog').then(r => r.json());
  } catch { return; }
  if (!data || !Array.isArray(data.entries) || !data.entries.length) return;
  const latest = data.latest || data.entries[0].id || '';
  if (!latest) return;
  let lastSeen = null, legacy = null;
  try {
    lastSeen = localStorage.getItem(_WHATS_NEW_SEEN_KEY);
    legacy = localStorage.getItem(_WHATS_NEW_LEGACY_KEY);
  } catch (_) {}
  if (!lastSeen && !legacy) {
    // First load on this browser: anchor silently.
    try { localStorage.setItem(_WHATS_NEW_SEEN_KEY, latest); } catch (_) {}
    return;
  }
  // A browser that tracked the git-based changelog has just updated to a build
  // with CHANGELOG.md: it sees the newest entry once, then tracks by entry id.
  if (lastSeen === latest) return;
  _showWhatsNewPopup(data.entries[0]);
  try {
    localStorage.setItem(_WHATS_NEW_SEEN_KEY, latest);
    localStorage.removeItem(_WHATS_NEW_LEGACY_KEY);
  } catch (_) {}
}

function _showWhatsNewPopup(entry) {
  const commit = entry;   // one CHANGELOG.md entry: title, date, body (markdown), category
  if (!commit) return;
  // Tear down any prior instance (e.g. preview button reopened).
  document.querySelectorAll('.whats-new-overlay').forEach(el => el.remove());

  const overlay = document.createElement('div');
  overlay.className = 'whats-new-overlay';
  overlay.setAttribute('role', 'presentation');

  const cat = commit.category || 'other';
  const dateLabel = _formatChangelogDate(commit.date);

  overlay.innerHTML = `
    <div class="whats-new-dialog" role="dialog" aria-modal="true" aria-labelledby="whats-new-title">
      <div class="whats-new-hero" data-cat="${escapeHtml(cat)}">
        <button class="whats-new-x" type="button" aria-label="Close">
          <i class="fa-solid fa-xmark"></i>
        </button>
        <div class="whats-new-icon">
          <img src="/static/images/logo.png" alt="" class="whats-new-logo">
          <div class="whats-new-eyebrow">What's new in this update</div>
        </div>
        <div class="whats-new-subject" id="whats-new-title">${escapeHtml(commit.title || '')}</div>
        <div class="whats-new-meta">
          <span class="whats-new-cat-tag">${escapeHtml(cat)}</span>
          <span>${escapeHtml(dateLabel)}</span>
        </div>
      </div>
      <div class="whats-new-body" id="whats-new-body"></div>
      <div class="whats-new-actions">
        <button class="whats-new-secondary" id="whats-new-changelog-btn" type="button">
          <i class="fa-solid fa-clock-rotate-left"></i> View full changelog
        </button>
        <button class="whats-new-primary" id="whats-new-close-btn" type="button">Got it</button>
      </div>
    </div>`;

  // Render the notes through the shared markdown renderer so heading/bullet
  // styling matches the Changelog tab. An entry with no notes gets a fallback.
  const bodyEl = overlay.querySelector('#whats-new-body');
  const bodyText = (commit.body || '').trim();
  if (bodyText) {
    bodyEl.appendChild(_renderChangelogBody(bodyText));
  } else {
    const p = document.createElement('p');
    p.className = 'whats-new-empty';
    p.textContent = 'Small under-the-hood changes: no detailed notes for this update.';
    bodyEl.appendChild(p);
  }

  document.body.appendChild(overlay);

  const close = () => {
    overlay.classList.remove('visible');
    overlay.classList.add('closing');
    setTimeout(() => overlay.remove(), 200);
    document.removeEventListener('keydown', onKey);
  };
  const onKey = e => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  // Click outside the dialog dismisses; clicks inside the dialog don't bubble here.
  overlay.addEventListener('click', e => { if (e.target === overlay) close(); });
  overlay.querySelector('.whats-new-x').addEventListener('click', close);
  overlay.querySelector('#whats-new-close-btn').addEventListener('click', close);
  overlay.querySelector('#whats-new-changelog-btn').addEventListener('click', () => {
    close();
    if (typeof openSettings === 'function') {
      openSettings();
      // Wait for the settings panels to mount, then jump to the Changelog tab.
      setTimeout(() => {
        const navBtn = document.querySelector('.settings-nav-item[data-target="section-changelog"]');
        if (navBtn) navBtn.click();
      }, 60);
    }
  });
  document.addEventListener('keydown', onKey);
  // Trigger fade/scale-in on next paint.
  requestAnimationFrame(() => overlay.classList.add('visible'));
}

/* Public hook for the "Preview What's New" demo button + console use. */
window.previewWhatsNew = async function previewWhatsNew() {
  try {
    const data = await fetch('/api/changelog').then(r => r.json());
    const commit = data && data.entries && data.entries[0];
    if (!commit) {
      flashStatus('No release notes to preview');
      return;
    }
    _showWhatsNewPopup(commit);
  } catch (e) {
    flashStatus('Preview failed: ' + (e.message || e));
  }
};

function _formatChangelogDate(s) {
  // Accepts "YYYY-MM-DD" or full ISO. Render as "Mon DD, YYYY" so the
  // listing reads like a human changelog instead of a git log.
  if (!s) return '';
  // A bare YYYY-MM-DD is a calendar date, not an instant: build it as a local
  // date, otherwise new Date() reads it as UTC midnight and everyone west of
  // Greenwich sees the day before.
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s).trim());
  const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function _escHtml(s) {
  const d = document.createElement('div');
  d.textContent = String(s ?? '');
  return d.innerHTML;
}

async function setStartupLaunch(enabled) {
  try {
    await fetch('/api/settings/startup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
  } catch (_) {
    document.getElementById('startup-toggle').checked = !enabled;
  }
}

// ── Obsidian export ──────────────────────────────────────────────────────

async function loadObsidianSettings() {
  try {
    const st = await fetch('/api/obsidian/status').then(r => r.json());
    document.getElementById('obsidian-toggle').checked = !!st.enabled;
    document.getElementById('obsidian-dir').value = st.dir || '';
  } catch (_) {}
}

function saveObsidianSettings() {
  const updates = {
    obsidian_export_enabled: document.getElementById('obsidian-toggle')?.checked === true,
    obsidian_export_dir: document.getElementById('obsidian-dir')?.value || '',
  };
  Object.assign(_prefs, updates);
  fetch('/api/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  }).catch(() => {});
}

// ── Calendar (published ICS feed) ────────────────────────────────────────

/**
 * Render the Calendar tab from /api/calendar/status. The stored ICS link is a
 * credential and only ever arrives masked, so the input starts empty: typing a
 * value is the only way to replace the saved link.
 */
async function loadCalendarStatus() {
  const stateEl  = document.getElementById('calendar-link-state');
  const lineEl   = document.getElementById('calendar-status-line');
  const detailEl = document.getElementById('calendar-status-detail');
  if (!stateEl) return;
  let st;
  try {
    st = await fetch('/api/calendar/status').then(r => r.json());
  } catch (_) {
    lineEl.textContent = 'Could not read the calendar status.';
    return;
  }
  const toggle = document.getElementById('calendar-enabled');
  if (toggle) toggle.checked = !!st.enabled;
  const interval = document.getElementById('calendar-refresh-minutes');
  if (interval) interval.value = String(st.refresh_minutes || 60);
  const input = document.getElementById('calendar-ics-url');
  if (input) input.placeholder = st.has_url ? st.url_masked : 'Paste the ICS link';

  stateEl.textContent = st.has_url ? `Saved link: ${st.url_masked}` : 'No link saved yet.';
  const forgetBtn = document.getElementById('calendar-forget-btn');
  if (forgetBtn) forgetBtn.hidden = !st.has_url;

  if (st.last_error) {
    lineEl.textContent = st.last_error;
  } else if (st.last_refresh) {
    const pct = Math.round((st.has_attendees_ratio || 0) * 100);
    lineEl.textContent =
      `${st.instance_count} meeting${st.instance_count === 1 ? '' : 's'} in the window, ` +
      `${st.matched_sessions} recording${st.matched_sessions === 1 ? '' : 's'} matched, ` +
      `${pct}% carry attendees.`;
  } else {
    lineEl.textContent = st.has_url ? 'Not refreshed yet.' : 'Paste your ICS link to get started.';
  }
  detailEl.textContent = st.last_refresh
    ? `Last refresh ${_fmtCalendarTime(st.last_refresh)}` +
      (st.next_refresh_due ? `, next due ${_fmtCalendarTime(st.next_refresh_due)}` : '')
    : '';
}

function _fmtCalendarTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function toggleCalendarLinkReveal() {
  const input = document.getElementById('calendar-ics-url');
  const btn = document.getElementById('calendar-reveal-btn');
  if (!input) return;
  const hidden = input.type === 'password';
  input.type = hidden ? 'text' : 'password';
  if (btn) btn.innerHTML = `<i class="fa-solid fa-eye${hidden ? '-slash' : ''}"></i>`;
}

/** Toggle + interval only. The link has its own explicit Save. */
function saveCalendarSettings() {
  const updates = {
    calendar_enabled: document.getElementById('calendar-enabled')?.checked === true,
    calendar_refresh_minutes: parseInt(document.getElementById('calendar-refresh-minutes')?.value || '60', 10),
  };
  Object.assign(_prefs, updates);
  fetch('/api/preferences', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  }).then(() => loadCalendarStatus()).catch(() => {});
}

/** Save a newly typed ICS link, then refresh once so matches appear straight away. */
async function saveCalendarLink() {
  const input = document.getElementById('calendar-ics-url');
  const btn = document.getElementById('calendar-save-btn');
  if (!input || !btn || btn.disabled) return;
  const url = (input.value || '').trim();
  if (!url) {
    uiToast({ message: 'Paste the ICS link first.', kind: 'warn' });
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    // A dedicated route, not /api/preferences: that one round-trips a masked
    // copy of every setting from every open tab and must never write this key.
    const res = await fetch('/api/calendar/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      uiToast({ message: data.error || 'Could not save the calendar link.', kind: 'error' });
      return;
    }
    // The masked value must never round-trip back as the stored link.
    input.value = '';
    input.type = 'password';
    delete _prefs.calendar_ics_url;
    const toggle = document.getElementById('calendar-enabled');
    if (toggle && !toggle.checked) {
      toggle.checked = true;
      saveCalendarSettings();
    }
    _prefs.calendar_enabled = true;
    await refreshCalendarNow();
  } catch (_) {
    uiToast({ message: 'Could not save the calendar link.', kind: 'error' });
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save link';
    loadCalendarStatus();
  }
}

/** Remove the stored link entirely and switch the feature off. */
async function forgetCalendarLink() {
  const ok = await uiConfirm({
    title: 'Forget the calendar link?',
    message: 'The app stops matching recordings to meetings until you paste a link again. Matches already stored are kept.',
    confirmLabel: 'Forget link',
    danger: true,
  });
  if (!ok) return;
  try {
    await fetch('/api/calendar/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: true }),
    });
    delete _prefs.calendar_ics_url;
    _prefs.calendar_enabled = false;
    uiToast({ message: 'Calendar link removed.', kind: 'success' });
  } catch (_) {
    uiToast({ message: 'Could not remove the calendar link.', kind: 'error' });
  }
  loadCalendarStatus();
}

async function testCalendarLink() {
  const btn = document.getElementById('calendar-test-btn');
  const out = document.getElementById('calendar-test-result');
  if (!btn || btn.disabled) return;
  const typed = (document.getElementById('calendar-ics-url')?.value || '').trim();
  btn.disabled = true;
  btn.textContent = 'Testing…';
  out.textContent = '';
  try {
    const res = await fetch('/api/calendar/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: typed }),
    }).then(r => r.json());
    if (!res.ok) {
      out.textContent = res.error || 'The calendar could not be read.';
    } else {
      const pct = Math.round((res.has_attendees_ratio || 0) * 100);
      const parts = [
        `${res.event_count} event${res.event_count === 1 ? '' : 's'}, ` +
        `${res.instance_count} in the window`,
      ];
      if (res.first_start && res.last_start) {
        parts.push(`${_fmtCalendarTime(res.first_start)} to ${_fmtCalendarTime(res.last_start)}`);
      }
      parts.push(`${pct}% carry attendees`);
      if (res.sample_titles && res.sample_titles.length) {
        parts.push(`e.g. ${res.sample_titles.join(', ')}`);
      }
      if (res.timezone_notes && res.timezone_notes.length) {
        parts.push(res.timezone_notes.join('; '));
      }
      out.textContent = parts.join(' | ');
    }
  } catch (_) {
    out.textContent = 'The calendar could not be read.';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Test link';
  }
}

async function refreshCalendarNow() {
  const btn = document.getElementById('calendar-refresh-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Refreshing…'; }
  try {
    const res = await fetch('/api/calendar/refresh', { method: 'POST' }).then(r => r.json());
    if (res.error) {
      uiToast({ message: res.error, kind: 'error' });
    } else {
      uiToast({
        message: `Calendar refreshed: ${res.matched} recording${res.matched === 1 ? '' : 's'} matched.`,
        kind: 'success',
      });
    }
  } catch (_) {
    uiToast({ message: 'Calendar refresh failed.', kind: 'error' });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh now'; }
    loadCalendarStatus();
  }
}

async function obsidianExportAll() {
  const btn = document.getElementById('obsidian-export-all-btn');
  const status = document.getElementById('obsidian-export-status');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = 'Exporting…';
  try {
    const res = await fetch('/api/obsidian/export-all', { method: 'POST' }).then(r => r.json());
    if (res.error) {
      status.textContent = res.error;
    } else {
      status.textContent = `Exported ${res.exported} session${res.exported === 1 ? '' : 's'} (${res.skipped} empty skipped)`;
    }
  } catch (_) {
    status.textContent = 'Export failed - is the vault folder reachable?';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Export all now';
  }
}

// ── Data folder ──────────────────────────────────────────────────────────

async function loadDataFolder() {
  const pathEl = document.getElementById('data-folder-current');
  const resetBtn = document.getElementById('data-folder-reset-btn');
  if (!pathEl) return;
  try {
    const info = await fetch('/api/data_folder').then(r => r.json());
    pathEl.textContent = info.current;
    pathEl.title = info.overridden
      ? `Overridden · default is ${info.default}`
      : 'Default location';
    if (resetBtn) resetBtn.style.display = info.overridden ? '' : 'none';
  } catch (_) {
    pathEl.textContent = '(error reading data folder)';
  }
}

async function pickDataFolder() {
  const btn = document.getElementById('data-folder-pick-btn');
  if (!btn || btn.disabled) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Opening picker…';
  try {
    const cur = document.getElementById('data-folder-current')?.textContent || '';
    const res = await fetch('/api/data_folder/pick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initial: cur }),
    }).then(r => r.json());
    if (!res.selected) {
      // user cancelled
      return;
    }
    const ok = await uiConfirm({
      title: 'Move data folder?',
      message: 'This will copy every recording, database, and setting to the new location and switch over. The original folder is kept as a backup until you delete it manually. The app will need a restart afterwards.',
      details: [res.selected],
      confirmLabel: 'Move data',
      danger: true,
    });
    if (!ok) return;
    btn.textContent = 'Migrating…';
    const out = await fetch('/api/data_folder/migrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ destination: res.selected }),
    }).then(r => r.json());
    if (out.error) {
      await uiAlert({ title: 'Migration failed', message: out.error, kind: 'error' });
      return;
    }
    const mb = (out.bytes_copied / 1024 / 1024).toFixed(1);
    await uiAlert({
      title: 'Data folder migrated',
      message: `${out.files_copied} files + ${out.dbs_copied} databases (${mb} MB). Please close and reopen the app for the change to take full effect.`,
      details: [`The original folder is preserved at: ${out.src}`],
      kind: 'success',
    });
    loadDataFolder();
  } catch (e) {
    uiToast({ message: `Error: ${e.message || e}`, kind: 'error' });
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function resetDataFolder() {
  const ok = await uiConfirm({
    title: 'Revert to the default data folder?',
    message: 'This only changes which folder the app reads from on next startup. It does not move files. If you want your current data at the default location, copy it there manually first. The app will need a restart afterwards.',
    confirmLabel: 'Revert',
    danger: true,
  });
  if (!ok) return;
  try {
    await fetch('/api/data_folder/reset', { method: 'POST' }).then(r => r.json());
    await uiAlert({ title: 'Data folder reverted', message: 'Reverted to default data folder. Please close and reopen the app.', kind: 'success' });
    loadDataFolder();
  } catch (e) {
    uiToast({ message: `Error: ${e.message || e}`, kind: 'error' });
  }
}

function closeSettingsOnOverlay(e) {
  if (e.target === e.currentTarget) closeSettings();
}

function toggleKeyVis(inputId) {
  const el = document.getElementById(inputId);
  const showing = el.type === 'password';
  el.type = showing ? 'text' : 'password';
  const btn = el.parentElement.querySelector('.key-vis-btn');
  if (btn) btn.innerHTML = showing
    ? '<i class="fa-solid fa-eye-slash"></i>'
    : '<i class="fa-solid fa-eye"></i>';
}

// ── Audio Parameters ──────────────────────────────────────────────────────
let _apCache = null;  // cached audio params response

let _raCache = null; // reanalysis params cache (separate from audio params)

async function _apLoad() {
  try {
    _apCache = await fetch('/api/audio_params').then(r => r.json());
  } catch (_) {}
  try {
    _raCache = await fetch('/api/reanalysis_params').then(r => r.json());
  } catch (_) {}
}

function _apRenderSection(containerId, paramDefs, current) {
  const container = document.getElementById(containerId);
  if (!container || !paramDefs) return;
  container.innerHTML = '';

  // Find toggle key(s) that control enabled state of sibling params.
  // If multiple toggles exist, non-toggle params are disabled only when ALL toggles are off.
  const toggleKeys = [];
  let toggleInverted = false; // when true, ON disables siblings instead of enabling them
  for (const [k, s] of Object.entries(paramDefs)) {
    if (s.type === 'toggle') { toggleKeys.push(k); toggleInverted = !!s.inverts_siblings; }
  }
  const toggleMasterKey = toggleKeys[0] || null;
  const hasMultipleToggles = toggleKeys.length > 1;

  for (const [key, spec] of Object.entries(paramDefs)) {
    const val = current[key] ?? spec.value;
    const isDefault = Math.abs(val - spec.value) < 1e-9;
    const unit = spec.unit ? `<span class="ap-unit">${spec.unit}</span>` : '';
    const tooltip = spec.tooltip || spec.description;

    const param = document.createElement('div');
    param.className = 'ap-param';
    param.dataset.apKey = key;
    if (spec.independent) param.dataset.apIndependent = '1';

    if (spec.type === 'toggle') {
      // Render as a toggle switch
      const checked = parseInt(val) ? 'checked' : '';
      param.innerHTML = `
        <div class="ap-header">
          <span class="ap-label">${spec.label}</span>
          <span class="ap-desc">${spec.description}</span>
          <div class="ap-info-wrap">
            <button class="ap-info-btn" tabindex="-1"><i class="fa-solid fa-circle-info"></i></button>
            <div class="ap-tooltip">
              <div class="ap-tooltip-title"><i class="fa-solid fa-circle-info"></i> ${spec.label}</div>
              <div class="ap-tooltip-body">${tooltip}</div>
              <div class="ap-tooltip-default">Default: <span>Off</span></div>
            </div>
          </div>
        </div>
        <div class="ap-slider-row" style="justify-content:flex-start;gap:10px">
          <label class="toggle-switch">
            <input type="checkbox" id="ap-toggle-${key}" ${checked}>
            <span class="toggle-slider"></span>
          </label>
          <span class="ap-toggle-label" id="ap-toggle-label-${key}" style="font-size:12px;color:var(--fg-muted)">${checked ? 'Enabled' : 'Disabled'}</span>
        </div>`;
      container.appendChild(param);
      _apBindTooltip(param);

      const cb = param.querySelector(`#ap-toggle-${key}`);
      const lbl = param.querySelector(`#ap-toggle-label-${key}`);
      cb.addEventListener('change', () => {
        const v = cb.checked ? 1 : 0;
        lbl.textContent = cb.checked ? 'Enabled' : 'Disabled';
        const saveFn = containerId === 'ap-reanalysis-params' ? _raSave : _apSave;
        saveFn(key, v);
        // Enable/disable sibling params in this section.
        // With multiple toggles, non-toggle params are enabled if ANY toggle is on.
        if (hasMultipleToggles) {
          const anyOn = toggleKeys.some(tk => {
            const el = document.getElementById(`ap-toggle-${tk}`);
            return el ? el.checked : false;
          });
          _apSetSectionEnabled(containerId, toggleKeys, anyOn);
        } else {
          const siblingsEnabled = toggleInverted ? !cb.checked : cb.checked;
          _apSetSectionEnabled(containerId, [key], siblingsEnabled);
        }
      });
      continue;
    }

    if (spec.type === 'select') {
      // Render as a dropdown select
      const optionsHtml = spec.options.map(o =>
        `<option value="${o.id}"${val === o.id ? ' selected' : ''}>${o.label}</option>`
      ).join('');
      const isDefault = val === spec.value;
      param.innerHTML = `
        <div class="ap-header">
          <span class="ap-label">${spec.label}</span>
          <span class="ap-desc">${spec.description}</span>
          <div class="ap-info-wrap">
            <button class="ap-info-btn" tabindex="-1"><i class="fa-solid fa-circle-info"></i></button>
            <div class="ap-tooltip">
              <div class="ap-tooltip-title"><i class="fa-solid fa-circle-info"></i> ${spec.label}</div>
              <div class="ap-tooltip-body">${tooltip}</div>
              <div class="ap-tooltip-default">Default: <span>${spec.options.find(o => o.id === spec.value)?.label || spec.value}</span></div>
            </div>
          </div>
        </div>
        <div class="ap-slider-row" style="gap:8px">
          <select class="model-config-sel" id="ap-select-${key}" style="flex:1">${optionsHtml}</select>
          <button class="ap-reset${isDefault ? ' ap-reset-hidden' : ''}" id="ap-reset-${key}"
                  title="Reset to default"
                  onclick="_apResetOne('${key}')"
                  style="flex-shrink:0">
            <i class="fa-solid fa-rotate-right"></i>
          </button>
        </div>`;
      container.appendChild(param);
      _apBindTooltip(param);

      const sel = param.querySelector(`#ap-select-${key}`);
      sel.addEventListener('change', () => {
        const saveFn = containerId === 'ap-reanalysis-params' ? _raSave : _apSave;
        saveFn(key, sel.value);
        const resetBtn = document.getElementById(`ap-reset-${key}`);
        if (resetBtn) resetBtn.classList.toggle('ap-reset-hidden', sel.value === spec.value);
      });
      continue;
    }

    // Standard slider param
    const pct = ((val - spec.min) / (spec.max - spec.min)) * 100;
    const anyToggleOn = toggleKeys.some(tk => !!parseInt(current[tk] ?? 0));
    const isToggle = toggleKeys.includes(key);
    // Params flagged "independent" are never gated by the section's toggle (e.g.
    // the desktop-bleed gate works whether or not echo cancellation is enabled).
    const isDisabled = (toggleKeys.length > 0 && !isToggle && !spec.independent && (toggleInverted ? anyToggleOn : !anyToggleOn));

    param.innerHTML = `
      <div class="ap-header">
        <span class="ap-label">${spec.label}</span>${unit}
        <span class="ap-desc">${spec.description}</span>
        <div class="ap-info-wrap">
          <button class="ap-info-btn" tabindex="-1"><i class="fa-solid fa-circle-info"></i></button>
          <div class="ap-tooltip">
            <div class="ap-tooltip-title"><i class="fa-solid fa-circle-info"></i> ${spec.label}</div>
            <div class="ap-tooltip-body">${tooltip}</div>
            <div class="ap-tooltip-default">Default: <span>${spec.value}${spec.unit ? ' ' + spec.unit : ''}</span></div>
          </div>
        </div>
      </div>
      <div class="ap-slider-row">
        <input type="range" class="ap-slider" id="ap-slider-${key}"
               min="${spec.min}" max="${spec.max}" step="${spec.step}" value="${val}"
               style="background:linear-gradient(90deg,var(--accent) ${pct}%,var(--border) ${pct}%)"
               ${isDisabled ? 'disabled' : ''}>
        <input type="number" class="ap-val-input" id="ap-${key}"
               value="${val}" min="${spec.min}" max="${spec.max}" step="${spec.step}"
               ${isDisabled ? 'disabled' : ''}>
        <button class="ap-reset${isDefault ? ' ap-reset-hidden' : ''}" id="ap-reset-${key}"
                title="Reset to default (${spec.value})"
                onclick="_apResetOne('${key}')"
                ${isDisabled ? 'disabled' : ''}>
          <i class="fa-solid fa-rotate-right"></i>
        </button>
      </div>`;
    if (isDisabled) param.classList.add('ap-disabled');
    container.appendChild(param);

    // Bind tooltip to body for overflow escape
    _apBindTooltip(param);

    // Wire slider ↔ input sync
    const slider = param.querySelector('.ap-slider');
    const input  = param.querySelector('.ap-val-input');

    const saveFn = containerId === 'ap-reanalysis-params' ? _raSave : _apSave;
    slider.addEventListener('input', () => {
      input.value = slider.value;
      _apUpdateSliderFill(slider, spec);
    });
    slider.addEventListener('change', () => {
      saveFn(key, parseFloat(slider.value));
      _apToggleReset(key, parseFloat(slider.value), spec.value);
    });
    input.addEventListener('change', () => {
      let v = parseFloat(input.value);
      v = Math.min(spec.max, Math.max(spec.min, v));
      input.value = v;
      slider.value = v;
      _apUpdateSliderFill(slider, spec);
      saveFn(key, v);
      _apToggleReset(key, v, spec.value);
    });
  }
}

function _apSetSectionEnabled(containerId, skipKeys, enabled) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const skip = new Set(Array.isArray(skipKeys) ? skipKeys : [skipKeys]);
  for (const param of container.querySelectorAll('.ap-param')) {
    if (skip.has(param.dataset.apKey)) continue;
    if (param.dataset.apIndependent === '1') continue;   // never toggle-gated
    param.classList.toggle('ap-disabled', !enabled);
    for (const el of param.querySelectorAll('input, button')) {
      el.disabled = !enabled;
    }
  }
}

function _apBindTooltip(paramEl) {
  const btn = paramEl.querySelector('.ap-info-btn');
  const tip = paramEl.querySelector('.ap-tooltip');
  if (!btn || !tip) return;

  // Move tooltip to body so it escapes any overflow:hidden/auto ancestors
  document.body.appendChild(tip);

  btn.addEventListener('mouseenter', () => {
    const rect = btn.getBoundingClientRect();
    tip.classList.remove('ap-arrow-down', 'ap-arrow-up');
    tip.classList.add('ap-tooltip-visible');

    // Temporarily show to measure height
    const tipH = tip.offsetHeight;
    const spaceAbove = rect.top;
    const spaceBelow = window.innerHeight - rect.bottom;

    if (spaceAbove > tipH + 12) {
      // Show above
      tip.style.top = (rect.top - tipH - 10) + 'px';
      tip.classList.add('ap-arrow-down');
    } else {
      // Show below
      tip.style.top = (rect.bottom + 10) + 'px';
      tip.classList.add('ap-arrow-up');
    }
    // Align right edge to the button
    let left = rect.right - 290;
    if (left < 8) left = 8;
    tip.style.left = left + 'px';
  });

  btn.addEventListener('mouseleave', () => {
    tip.classList.remove('ap-tooltip-visible');
  });
}

function _apUpdateSliderFill(slider, spec) {
  const pct = ((slider.value - spec.min) / (spec.max - spec.min)) * 100;
  slider.style.background = `linear-gradient(90deg,var(--accent) ${pct}%,var(--border) ${pct}%)`;
}

function _apToggleReset(key, val, defaultVal) {
  const btn = document.getElementById(`ap-reset-${key}`);
  if (btn) btn.classList.toggle('ap-reset-hidden', Math.abs(val - defaultVal) < 1e-9);
}

async function _apRefresh() {
  await _apLoad();
  if (_apCache) {
    _apRenderSection('ap-transcription-params', _apCache.transcription, _apCache.current);
    _apRenderSection('ap-diarization-params',   _apCache.diarization,   _apCache.current);
    _apRenderSection('ap-agc-params',           _apCache.auto_gain,         _apCache.current);
    _apRenderSection('ap-echo-params',          _apCache.echo_cancellation, _apCache.current);
    _apRenderSection('ap-screen-params',        _apCache.screen_recording,  _apCache.current);
  }
  if (_raCache) {
    _apRenderSection('ap-reanalysis-params', _raCache.reanalysis, _raCache.current);
  }
}

async function _apSave(key, value) {
  try {
    const res = await fetch('/api/audio_params', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: value }),
    }).then(r => r.json());
    if (res.ok && _apCache) {
      _apCache.current = res.audio_params;
      // Update reset button visibility
      const spec = (_apCache.transcription[key] || _apCache.diarization[key] || (_apCache.auto_gain && _apCache.auto_gain[key]) || (_apCache.echo_cancellation && _apCache.echo_cancellation[key]) || (_apCache.screen_recording && _apCache.screen_recording[key]));
      const resetBtn = document.getElementById(`ap-reset-${key}`);
      if (resetBtn && spec) {
        const isDefault = Math.abs(value - spec.value) < 1e-9;
        resetBtn.classList.toggle('ap-reset-hidden', isDefault);
      }
      // Keep sidebar screen toggle in sync with settings panel
      if (key === 'screen_record_enabled') _syncScreenToggle();
      // Backend auto-flips the section's preset to "custom" when a
      // preset-controlled key is edited. Sync the dropdowns from the
      // server response so the UI matches the persisted state.
      _syncPresetDropdownsFromResponse(res);
    }
  } catch (_) {}
}

async function _raSave(key, value) {
  try {
    const res = await fetch('/api/reanalysis_params', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [key]: value }),
    }).then(r => r.json());
    if (res.ok && _raCache) {
      _raCache.current = res.reanalysis_params;
    }
  } catch (_) {}
}

async function resetReanalysisParams() {
  try {
    const res = await fetch('/api/reanalysis_params/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).then(r => r.json());
    if (res.ok && _raCache) {
      _raCache.current = res.reanalysis_params;
      _apRenderSection('ap-reanalysis-params', _raCache.reanalysis, _raCache.current);
    }
  } catch (_) {}
}

function _syncPresetDropdownsFromResponse(res) {
  // Sync the section preset dropdowns from /api/audio_params PUT response,
  // which echoes back the post-flip preset names. The server auto-flips the
  // relevant section to "custom" whenever a preset-controlled key is edited.
  const map = [
    ['transcription_preset', 'transcription-preset-sel', 'transcription-preset-desc'],
    ['diarization_preset',   'diarization-preset-sel',   'diarization-preset-desc'],
    ['screen_preset',        'screen-preset-sel',        'screen-preset-desc'],
  ];
  for (const [field, selId, descId] of map) {
    const newVal = res[field];
    if (!newVal) continue;
    const sel = document.getElementById(selId);
    if (sel && sel.value !== newVal) {
      sel.value = newVal;
      const desc = document.getElementById(descId);
      if (desc && newVal === 'custom') {
        desc.textContent = 'Manually configure all parameters';
      }
    }
  }
}

function _switchToCustomPreset(key) {
  if (_apCache?.transcription?.[key]) {
    const sel = document.getElementById('transcription-preset-sel');
    if (sel && sel.value !== 'custom') {
      sel.value = 'custom';
      const desc = document.getElementById('transcription-preset-desc');
      if (desc) desc.textContent = 'Manually configure all parameters';
      fetch('/api/transcription/presets', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preset: 'custom' }),
      }).catch(() => {});
    }
  } else if (_apCache?.diarization?.[key]) {
    const sel = document.getElementById('diarization-preset-sel');
    if (sel && sel.value !== 'custom') {
      sel.value = 'custom';
      const desc = document.getElementById('diarization-preset-desc');
      if (desc) desc.textContent = 'Manually configure all parameters';
      fetch('/api/diarization/presets', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preset: 'custom' }),
      }).catch(() => {});
    }
  } else if (_apCache?.screen_recording?.[key] && key !== 'screen_record_enabled') {
    const sel = document.getElementById('screen-preset-sel');
    if (sel && sel.value !== 'custom') {
      sel.value = 'custom';
      const desc = document.getElementById('screen-preset-desc');
      if (desc) desc.textContent = 'Manually configure all parameters';
      setScreenPreset('custom');
    }
  }
}

async function _apResetOne(key) {
  // Detect whether this is a reanalysis param or an audio param
  const isReanalysis = _raCache?.reanalysis?.[key];
  const endpoint = isReanalysis ? '/api/reanalysis_params/reset' : '/api/audio_params/reset';

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }).then(r => r.json());

    if (res.ok && isReanalysis && _raCache) {
      _raCache.current = res.reanalysis_params;
      const spec = _raCache.reanalysis[key];
      if (spec) {
        if (spec.type === 'select') {
          const sel = document.getElementById(`ap-select-${key}`);
          if (sel) sel.value = spec.value;
        } else {
          const input  = document.getElementById(`ap-${key}`);
          const slider = document.getElementById(`ap-slider-${key}`);
          if (input)  input.value  = spec.value;
          if (slider) { slider.value = spec.value; _apUpdateSliderFill(slider, spec); }
        }
      }
      const resetBtn = document.getElementById(`ap-reset-${key}`);
      if (resetBtn) resetBtn.classList.add('ap-reset-hidden');
    } else if (res.ok && _apCache) {
      _apCache.current = res.audio_params;
      const spec = (_apCache.transcription[key] || _apCache.diarization[key] || (_apCache.auto_gain && _apCache.auto_gain[key]) || (_apCache.echo_cancellation && _apCache.echo_cancellation[key]) || (_apCache.screen_recording && _apCache.screen_recording[key]));
      if (spec) {
        if (spec.type === 'toggle') {
          const cb = document.getElementById(`ap-toggle-${key}`);
          const lbl = document.getElementById(`ap-toggle-label-${key}`);
          if (cb) { cb.checked = !!spec.value; }
          if (lbl) { lbl.textContent = spec.value ? 'Enabled' : 'Disabled'; }
          const paramEl = cb?.closest('.ap-param');
          const container = paramEl?.parentElement;
          if (container) _apSetSectionEnabled(container.id, key, !!spec.value);
        } else {
          const input  = document.getElementById(`ap-${key}`);
          const slider = document.getElementById(`ap-slider-${key}`);
          if (input)  input.value  = spec.value;
          if (slider) {
            slider.value = spec.value;
            _apUpdateSliderFill(slider, spec);
          }
        }
      }
      const resetBtn = document.getElementById(`ap-reset-${key}`);
      if (resetBtn) resetBtn.classList.add('ap-reset-hidden');
    }
  } catch (_) {}
}

// ── Transcription & Diarization Presets ───────────────────────────────────

let _transcriptionPresetsData = null;
let _diarizationPresetsData = null;

async function loadTranscriptionPresets() {
  try {
    _transcriptionPresetsData = await fetch('/api/transcription/presets').then(r => r.json());
    _renderPresetDropdown('transcription', _transcriptionPresetsData);
  } catch (_) {}
}

async function loadDiarizationPresets() {
  try {
    _diarizationPresetsData = await fetch('/api/diarization/presets').then(r => r.json());
    _renderPresetDropdown('diarization', _diarizationPresetsData);
  } catch (_) {}
}

function _renderPresetDropdown(section, data) {
  const sel = document.getElementById(`${section}-preset-sel`);
  if (!sel || !data) return;
  sel.innerHTML = '';
  for (const [id, p] of Object.entries(data.presets)) {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = p.label;
    if (id === data.selected) opt.selected = true;
    sel.appendChild(opt);
  }
  const desc = document.getElementById(`${section}-preset-desc`);
  const preset = data.presets[data.selected];
  if (desc && preset) desc.textContent = preset.description;
}

async function setTranscriptionPreset(presetId) {
  try {
    const res = await fetch('/api/transcription/presets', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset: presetId }),
    }).then(r => r.json());
    if (res.ok && _apCache) {
      if (res.audio_params) _apCache.current = res.audio_params;
      _apRenderSection('ap-transcription-params', _apCache.transcription, _apCache.current);
    }
    const desc = document.getElementById('transcription-preset-desc');
    if (desc && _transcriptionPresetsData?.presets[presetId]) {
      desc.textContent = _transcriptionPresetsData.presets[presetId].description;
    }
  } catch (_) {}
}

async function setDiarizationPreset(presetId) {
  try {
    const res = await fetch('/api/diarization/presets', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset: presetId }),
    }).then(r => r.json());
    if (res.ok && _apCache) {
      if (res.audio_params) _apCache.current = res.audio_params;
      _apRenderSection('ap-diarization-params', _apCache.diarization, _apCache.current);
    }
    const desc = document.getElementById('diarization-preset-desc');
    if (desc && _diarizationPresetsData?.presets[presetId]) {
      desc.textContent = _diarizationPresetsData.presets[presetId].description;
    }
  } catch (_) {}
}

async function resetSection(section) {
  try {
    const res = await fetch('/api/audio_params/reset_section', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ section }),
    }).then(r => r.json());
    if (res.ok && _apCache) {
      _apCache.current = res.audio_params;
      // Re-render the appropriate section
      const sectionMap = {
        transcription: ['ap-transcription-params', 'transcription'],
        diarization: ['ap-diarization-params', 'diarization'],
        screen_recording: ['ap-screen-params', 'screen_recording'],
      };
      const [containerId, cacheKey] = sectionMap[section] || [];
      if (containerId && _apCache[cacheKey]) {
        _apRenderSection(containerId, _apCache[cacheKey], _apCache.current);
      }
      // Reset preset dropdown to default
      if (section === 'transcription') {
        _renderPresetDropdown('transcription', {
          ..._transcriptionPresetsData,
          selected: _transcriptionPresetsData?.default || 'balanced',
        });
      } else if (section === 'diarization') {
        _renderPresetDropdown('diarization', {
          ..._diarizationPresetsData,
          selected: _diarizationPresetsData?.default || 'balanced',
        });
      } else if (section === 'screen_recording') {
        _renderScreenPresetDropdown(_screenPresetsData?.default || 'performance');
      }
      // Sync screen toggle if needed
      if (section === 'screen_recording') _syncScreenToggle();
    }
  } catch (_) {}
}

// ── Screen Recording ──────────────────────────────────────────────────────

let _screenDisplays = [];
let _screenPresetsData = null;

async function loadScreenDisplays() {
  try {
    const data = await fetch('/api/screen/displays').then(r => r.json());
    _screenDisplays = data.displays || [];
    const selected = (data.selected < _screenDisplays.length) ? data.selected : 0;
    _renderDisplayGrid(selected);
    // Update ffmpeg status in settings
    const ffEl = document.getElementById('settings-ffmpeg-status');
    if (ffEl) {
      ffEl.textContent = data.ffmpeg_available ? 'Available' : 'Not installed';
      ffEl.className = 'settings-info-val ' + (data.ffmpeg_available ? 'val-ok' : 'val-warn');
    }
  } catch (_) {}
}

function _renderDisplayGrid(selectedIdx) {
  const grid = document.getElementById('screen-display-grid');
  if (!grid) return;
  grid.innerHTML = '';

  if (_screenDisplays.length === 0) {
    grid.innerHTML = '<div class="screen-display-empty">No displays detected</div>';
    return;
  }

  // Calculate scale for thumbnails - fit all monitors into the grid
  const allLeft   = Math.min(..._screenDisplays.map(d => d.x));
  const allTop    = Math.min(..._screenDisplays.map(d => d.y));
  const allRight  = Math.max(..._screenDisplays.map(d => d.x + d.width));
  const allBottom = Math.max(..._screenDisplays.map(d => d.y + d.height));
  const totalW = allRight - allLeft;
  const totalH = allBottom - allTop;

  // Grid is roughly 200px wide - scale to fit
  const gridW = 200;
  const scale = gridW / totalW;
  const gridH = totalH * scale;

  const container = document.createElement('div');
  container.className = 'screen-display-map';
  container.style.width = gridW + 'px';
  container.style.height = Math.max(gridH, 30) + 'px';
  container.style.position = 'relative';

  _screenDisplays.forEach((disp, i) => {
    const el = document.createElement('div');
    el.className = 'screen-display-thumb' + (i === selectedIdx ? ' selected' : '');
    el.style.left   = ((disp.x - allLeft) * scale) + 'px';
    el.style.top    = ((disp.y - allTop) * scale) + 'px';
    el.style.width  = (disp.width * scale) + 'px';
    el.style.height = (disp.height * scale) + 'px';
    el.title = disp.label;
    el.innerHTML = `<span class="screen-display-num">${i + 1}</span>`;
    el.onclick = () => selectScreenDisplay(i);
    container.appendChild(el);
  });

  grid.appendChild(container);

  // Label below
  if (_screenDisplays[selectedIdx]) {
    const label = document.createElement('div');
    label.className = 'screen-display-label';
    label.textContent = _screenDisplays[selectedIdx].label;
    grid.appendChild(label);
  }
}

async function selectScreenDisplay(idx) {
  try {
    const res = await fetch('/api/screen/displays', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display: idx }),
    }).then(r => r.json());
    // Re-render with the server-confirmed selection
    _renderDisplayGrid(res.selected ?? idx);
    // Flash a border on the physical display so the user can identify it
    fetch('/api/screen/identify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display: res.selected ?? idx }),
    }).catch(() => {});
  } catch (_) {}
}

async function toggleScreenRecordEnabled(enabled) {
  // Save via audio params system
  await _apSave('screen_record_enabled', enabled ? 1 : 0);
  // Verify the save took effect - revert the checkbox if it didn't
  _syncScreenToggle();
}

function _syncScreenToggle() {
  if (!_apCache) return;
  const enabled = parseInt(_apCache.current.screen_record_enabled || 0);
  const toggle = document.getElementById('screen-record-toggle');
  if (toggle) toggle.checked = !!enabled;
  // Toggle visual is handled by the pane collapse - no need to hide body here
}

async function loadScreenPresets() {
  try {
    _screenPresetsData = await fetch('/api/screen/presets').then(r => r.json());
    _renderScreenPresetDropdown(_screenPresetsData.selected);
  } catch (_) {}
}

function _renderScreenPresetDropdown(selectedId) {
  const sel = document.getElementById('screen-preset-sel');
  if (!sel || !_screenPresetsData) return;
  sel.innerHTML = '';
  for (const [id, p] of Object.entries(_screenPresetsData.presets)) {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = p.label;
    if (id === selectedId) opt.selected = true;
    sel.appendChild(opt);
  }
  // Update description
  const desc = document.getElementById('screen-preset-desc');
  const preset = _screenPresetsData.presets[selectedId];
  if (desc && preset) desc.textContent = preset.description;
}

async function setScreenPreset(presetId) {
  try {
    const res = await fetch('/api/screen/presets', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ preset: presetId }),
    }).then(r => r.json());
    if (res.ok && _apCache) {
      _apCache.current = res.audio_params;
      // Re-render the screen params sliders with new values
      _apRenderSection('ap-screen-params', _apCache.screen_recording, _apCache.current);
    }
    // Update description
    const desc = document.getElementById('screen-preset-desc');
    if (desc && _screenPresetsData?.presets[presetId]) {
      desc.textContent = _screenPresetsData.presets[presetId].description;
    }
  } catch (_) {}
}

// Update screen recording status indicator
function _updateScreenRecordingStatus(isRecording) {
  const statusEl = document.getElementById('screen-capture-status');
  if (!statusEl) return;
  if (isRecording) {
    statusEl.innerHTML = '<span class="screen-rec-indicator"><i class="fa-solid fa-circle"></i> Recording</span>';
  } else {
    statusEl.textContent = '';
  }
}

// Track original key values so we only save changed ones
let _origKeyValues = {};

async function saveApiKeys() {
  const anthKey = document.getElementById('key-anthropic').value.trim();
  const oaiKey  = document.getElementById('key-openai').value.trim();
  const hfKey   = document.getElementById('key-huggingface').value.trim();
  const body = {};
  // Only send keys that were actually changed by the user
  if (anthKey && anthKey !== _origKeyValues['key-anthropic']) body.ANTHROPIC_API_KEY = anthKey;
  if (oaiKey  && oaiKey  !== _origKeyValues['key-openai'])    body.OPENAI_API_KEY    = oaiKey;
  if (hfKey   && hfKey   !== _origKeyValues['key-huggingface']) body.HUGGING_FACE_KEY  = hfKey;

  if (!Object.keys(body).length) {
    closeSettings();
    return;
  }

  const btn = document.querySelector('.btn-save-keys');
  btn.disabled = true;
  btn.textContent = 'Saving...';

  try {
    const resp = await fetch('/api/settings/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    if (resp.ok) {
      _renderKeyStatus('ANTHROPIC_API_KEY', 'key-anthropic',   data.keys);
      _renderKeyStatus('OPENAI_API_KEY',    'key-openai',      data.keys);
      _renderKeyStatus('HUGGING_FACE_KEY',  'key-huggingface', data.keys);
      ['key-anthropic', 'key-openai', 'key-huggingface'].forEach(id => {
        document.getElementById(id).value = '';
      });
      btn.textContent = 'Saved!';
      setTimeout(() => { btn.textContent = 'Save Keys'; btn.disabled = false; }, 1500);
    } else {
      uiToast({ message: data.error || 'Failed to save keys', kind: 'error' });
      btn.textContent = 'Save Keys';
      btn.disabled = false;
    }
  } catch (e) {
    uiToast({ message: 'Failed to save keys', kind: 'error' });
    btn.textContent = 'Save Keys';
    btn.disabled = false;
  }
}

/* ── Import / Export ──────────────────────────────────────────────────────── */

let _exportSessionId = null;

const _EXPORT_STEP_LABELS = {
  metadata:           'Session metadata',
  transcription:      'Transcription',
  summary:            'Summary',
  chapters:           'Chapters',
  chat:               'Chat & screenshots',
  notes:              'Notes & attachments',
  speakers:           'Speaker labels',
  speaker_embeddings: 'Voice fingerprints',
  audio:              'Audio (Opus compression)',
  video:              'Video recording',
};

function openExportModal(sessionId) {
  _exportSessionId = sessionId || state.sessionId;
  if (!_exportSessionId) return;
  document.getElementById('export-overlay').classList.remove('hidden');
  document.getElementById('export-body-options').classList.remove('hidden');
  document.getElementById('export-body-progress').classList.add('hidden');
  document.getElementById('export-actions').classList.remove('hidden');
  document.getElementById('export-download-btn').disabled = false;
  document.getElementById('export-subtitle').textContent = 'Select data to include';
}

function closeExportModal() {
  document.getElementById('export-overlay').classList.add('hidden');
}

function _exportBuildSteps(cats) {
  const container = document.getElementById('export-steps');
  container.innerHTML = '';
  for (const cat of cats) {
    const label = _EXPORT_STEP_LABELS[cat] || cat;
    const step = document.createElement('div');
    step.className = 'export-step';
    step.id = 'export-step-' + cat;
    step.innerHTML = `<i class="fa-solid fa-circle export-step-dot"></i><span>${label}</span>`;
    container.appendChild(step);
  }
  // Final download step
  const dl = document.createElement('div');
  dl.className = 'export-step';
  dl.id = 'export-step-download';
  dl.innerHTML = '<i class="fa-solid fa-circle export-step-dot"></i><span>Download</span>';
  container.appendChild(dl);
}

function _exportSetStep(stepId, status) {
  // status: 'active' | 'done' | 'error'
  const el = document.getElementById('export-step-' + stepId);
  if (!el) return;
  el.classList.remove('active', 'done', 'error');
  el.classList.add(status);
  const dot = el.querySelector('.export-step-dot');
  if (!dot) return;
  if (status === 'active')  dot.className = 'fa-solid fa-spinner fa-spin export-step-dot';
  else if (status === 'done') dot.className = 'fa-solid fa-circle-check export-step-dot';
  else if (status === 'error') dot.className = 'fa-solid fa-circle-xmark export-step-dot';
}

async function startExport() {
  const sid = _exportSessionId || state.sessionId;
  if (!sid) return;

  const cats = [];
  ['metadata', 'transcription', 'summary', 'chapters', 'chat', 'notes', 'speakers', 'speaker_embeddings', 'audio', 'video']
    .forEach(cat => {
      const cb = document.getElementById('export-opt-' + cat);
      if (cb && cb.checked) cats.push(cat);
    });

  // Before exporting speaker labels, make sure the mic ("me") speaker has a
  // real name. A still-default "You" is meaningless to whoever receives the
  // file, so offer to name it (renamed retroactively) or export as-is.
  if (cats.includes('speakers')) {
    let meStatus = null;
    try { meStatus = await fetch('/api/sessions/' + sid + '/me-status').then(r => r.json()); }
    catch (_) { meStatus = null; }
    if (meStatus && meStatus.needs_name) {
      const name = await _promptMeName({
        eyebrow: 'Before you export',
        title: 'Add your name to the mic',
        sub: 'Your microphone audio is exported under the default "You" label. Add your name so whoever you share this recording with can see who was speaking.',
        placeholder: 'e.g. Alex Rivera',
        primaryLabel: 'Save & export',
        allowSkip: true,
        skipLabel: 'Export as "You"',
      });
      if (name === null) return;            // cancelled: keep the options view
      if (name) await _applyMeName(sid, name);
    }
  }

  // Switch to progress view
  document.getElementById('export-body-options').classList.add('hidden');
  document.getElementById('export-actions').classList.add('hidden');
  document.getElementById('export-body-progress').classList.remove('hidden');
  document.getElementById('export-subtitle').textContent = 'Exporting…';

  const fillEl = document.getElementById('export-progress-fill');
  const statusEl = document.getElementById('export-progress-status');
  fillEl.style.width = '0%';
  fillEl.style.background = '';

  _exportBuildSteps(cats);

  const totalSteps = cats.length + 1; // +1 for download
  let currentStep = 0;

  const advanceStep = (cat, label) => {
    // Mark previous as done
    if (currentStep > 0) _exportSetStep(cats[currentStep - 1] || 'download', 'done');
    _exportSetStep(cat, 'active');
    statusEl.textContent = label;
    currentStep++;
    fillEl.style.width = Math.round((currentStep / totalSteps) * 90) + '%';
  };

  try {
    // Animate through data collection steps quickly
    for (let i = 0; i < cats.length; i++) {
      advanceStep(cats[i], 'Collecting ' + (_EXPORT_STEP_LABELS[cats[i]] || cats[i]).toLowerCase() + '…');
      await new Promise(r => setTimeout(r, 120));
    }

    // Mark last data step done, start download
    if (cats.length > 0) _exportSetStep(cats[cats.length - 1], 'done');
    _exportSetStep('download', 'active');
    statusEl.textContent = 'Building package…';
    fillEl.style.width = '85%';

    const resp = await fetch('/api/sessions/' + sid + '/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ include: cats }),
    });

    statusEl.textContent = 'Downloading…';
    fillEl.style.width = '92%';

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || 'Export failed');
    }

    const blob = await resp.blob();
    _exportSetStep('download', 'done');
    fillEl.style.width = '100%';

    // Show size
    const sizeMB = (blob.size / (1024 * 1024)).toFixed(1);
    statusEl.textContent = `Complete · ${sizeMB} MB`;
    document.getElementById('export-subtitle').textContent = 'Export complete';

    // Trigger download
    const cd = resp.headers.get('Content-Disposition') || '';
    const fnMatch = cd.match(/filename="?([^"]+)"?/);
    const filename = fnMatch ? fnMatch[1] : 'meeting.zip';

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);

    setTimeout(() => closeExportModal(), 1200);
  } catch (e) {
    _exportSetStep('download', 'error');
    statusEl.textContent = e.message;
    fillEl.style.width = '100%';
    fillEl.style.background = 'var(--danger, #e5534b)';
    document.getElementById('export-subtitle').textContent = 'Export failed';

    // Show a retry button
    const actions = document.getElementById('export-actions');
    actions.classList.remove('hidden');
    actions.innerHTML = `
      <button class="btn export-btn-cancel" onclick="closeExportModal()">Close</button>
      <button class="btn export-btn-go" onclick="openExportModal(_exportSessionId)">
        <i class="fa-solid fa-rotate-right"></i> Retry
      </button>`;
  }
}

// ── Import ────────────────────────────────────────────────────────────────
let _importDragCount = 0;
let _internalDragActive = false;  // set by sidebar drag-start, cleared on dragend

// ── Auto-scroll the sidebar list while dragging near an edge ────────────────
// While an internal sidebar drag (session or folder) is in progress, holding
// the pointer near the top (or bottom) of the scrollable session list scrolls
// it smoothly so the user can reach folders/positions that are out of view.
// A requestAnimationFrame loop does the scrolling; dragover only updates the
// target velocity, so the scroll keeps going even when the pointer is held
// perfectly still inside the edge zone.
function _initSidebarDragAutoScroll() {
  const EDGE = 60;        // px from an edge that triggers auto-scroll
  const MAX_SPEED = 16;   // px/frame at the very edge, easing to ~1 at the zone's inner border
  let raf = null;
  let lastY = null;       // last pointer clientY while inside the zone (null = disengaged)
  let listEl = null;

  function velocity() {
    if (lastY == null || !listEl) return 0;
    if (listEl.scrollHeight <= listEl.clientHeight) return 0;   // nothing to scroll
    const r = listEl.getBoundingClientRect();
    const fromTop = lastY - r.top;
    const fromBot = r.bottom - lastY;
    if (fromTop < EDGE) {
      const ease = 1 - Math.max(0, Math.min(1, fromTop / EDGE));  // 1 at edge → 0 at inner border
      return -Math.max(1, Math.round(MAX_SPEED * ease));
    }
    if (fromBot < EDGE) {
      const ease = 1 - Math.max(0, Math.min(1, fromBot / EDGE));
      return Math.max(1, Math.round(MAX_SPEED * ease));
    }
    return 0;
  }

  function tick() {
    if (!_internalDragActive || lastY == null) { stop(); return; }
    const v = velocity();
    if (v) listEl.scrollTop += v;
    raf = requestAnimationFrame(tick);
  }
  function start() { if (raf == null) raf = requestAnimationFrame(tick); }
  function stop()  { if (raf != null) cancelAnimationFrame(raf); raf = null; lastY = null; }

  document.addEventListener('dragover', e => {
    if (!_internalDragActive) return;
    if (!listEl || !listEl.isConnected) listEl = document.getElementById('session-list');
    if (!listEl) return;
    const r = listEl.getBoundingClientRect();
    // Disengage when the pointer leaves the list horizontally or moves well
    // past its vertical bounds.
    if (e.clientX < r.left || e.clientX > r.right ||
        e.clientY < r.top - EDGE || e.clientY > r.bottom + EDGE) {
      lastY = null;
      return;
    }
    lastY = e.clientY;
    start();
  });
  document.addEventListener('dragend', stop, true);
  document.addEventListener('drop', stop, true);
}

function _initImportDragDrop() {
  const overlay = document.getElementById('import-drop-overlay');
  if (!overlay) return;

  document.addEventListener('dragenter', e => {
    // Ignore internal sidebar reorder drags
    if (_internalDragActive) return;
    if (!e.dataTransfer?.types?.includes('Files')) return;
    _importDragCount++;
    if (_importDragCount === 1) overlay.classList.remove('hidden');
  });

  document.addEventListener('dragleave', e => {
    if (_internalDragActive) return;
    _importDragCount--;
    if (_importDragCount <= 0) {
      _importDragCount = 0;
      overlay.classList.add('hidden');
    }
  });

  document.addEventListener('drop', e => {
    _importDragCount = 0;
    overlay.classList.add('hidden');
  });

  overlay.addEventListener('dragover', e => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });

  overlay.addEventListener('drop', e => {
    e.preventDefault();
    _importDragCount = 0;
    overlay.classList.add('hidden');
    const file = e.dataTransfer?.files?.[0];
    const fn = (file?.name || '').toLowerCase();
    if (file && (fn.endsWith('.mtga') || fn.endsWith('.zip'))) {
      _doImport(file);
    }
  });
}

async function _doImport(file) {
  const toast = document.getElementById('import-toast');
  const icon = document.getElementById('import-toast-icon');
  const text = document.getElementById('import-toast-text');

  toast.classList.remove('hidden');
  icon.className = 'fa-solid fa-spinner fa-spin import-toast-icon';
  text.textContent = 'Importing meeting…';

  const form = new FormData();
  form.append('file', file);

  try {
    const resp = await fetch('/api/sessions/import', { method: 'POST', body: form });
    const data = await resp.json();

    if (!resp.ok) {
      icon.className = 'fa-solid fa-circle-exclamation import-toast-icon';
      text.textContent = data.error || 'Import failed';
      setTimeout(() => toast.classList.add('hidden'), 4000);
      return;
    }

    icon.className = 'fa-solid fa-circle-check import-toast-icon';
    text.textContent = 'Imported: ' + (data.title || 'Meeting');

    // Refresh sidebar, then navigate to the imported session.
    refreshSidebar();
    if (data.session_id) {
      // The sender's mic ("me") speaker may still carry the default "You",
      // which is meaningless on our end. Offer to name it (the sender) before
      // opening the session. This only updates the imported session's label,
      // never our own local "Me" identity.
      let meStatus = null;
      try { meStatus = await fetch('/api/sessions/' + data.session_id + '/me-status').then(r => r.json()); }
      catch (_) { meStatus = null; }
      if (meStatus && meStatus.needs_name) {
        // Offer the saved-speaker library as a shortcut, minus our own "Me"
        // profile (the sender is never us, and tagging them as us would wrongly
        // give them a "(You)" badge).
        let lib = [];
        try {
          const all = await fetch('/api/fingerprint/speakers').then(r => r.json());
          const meId = window._meSpeakerGlobalId || null;
          lib = (Array.isArray(all) ? all : []).filter(s => s.id !== meId);
        } catch (_) { lib = []; }
        const name = await _promptMeName({
          eyebrow: 'Name the speaker',
          title: 'Who recorded this?',
          sub: 'This meeting\'s microphone speaker is labeled "You" (the sender\'s default). Enter their name, or pick them from your saved speakers, so the transcript reads correctly on your end.',
          placeholder: 'e.g. Antonio Debouse',
          primaryLabel: 'Save name',
          allowSkip: true,
          skipLabel: 'Keep as "You"',
          librarySpeakers: lib,
        });
        if (name) await _applyMeName(data.session_id, name);
      }

      loadSession(data.session_id);
    }

    setTimeout(() => toast.classList.add('hidden'), 3000);
  } catch (e) {
    icon.className = 'fa-solid fa-circle-exclamation import-toast-icon';
    text.textContent = 'Import failed: ' + e.message;
    setTimeout(() => toast.classList.add('hidden'), 4000);
  }
}

function openImportPicker() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.mtga,.zip';
  input.onchange = () => {
    if (input.files?.[0]) _doImport(input.files[0]);
  };
  input.click();
}

/* ── Init ────────────────────────────────────────────────────────────────── */

let _attentionPreviousSort = null;

/** The filter popover owns the Needs attention criterion; the nav row is a
 *  plain link to /attention, so its state is aria-current, set by the router. */
function _syncAttentionControlState() {
  const btn = document.getElementById('sidebar-filter-btn');
  if (!btn) return;
  btn.classList.toggle('attention-on', _sidebarFilter.attention === 'needs');
}

function _setAttentionFilter(active) {
  if (active) {
    if (_sidebarFilter.attention !== 'needs') _attentionPreviousSort = _sidebarFilter.sortBy;
    _sidebarFilter.attention = 'needs';
    _sidebarFilter.sortBy = 'unresolved_first';
  } else {
    _sidebarFilter.attention = 'any';
    if (_sidebarFilter.sortBy === 'unresolved_first') {
      _sidebarFilter.sortBy = _attentionPreviousSort || 'date_desc';
    }
    _attentionPreviousSort = null;
  }
  _onFilterChange();
}

/** The badge and the queue's row count come from the same store slice. */
function attentionCount() {
  // The sidebar badge and the Needs attention queue count the same thing from
  // the same slice: sessions rows flagged as needing work. The attention
  // summary is only a fallback before sessions loads, so the badge and the
  // queue subtitle never disagree.
  const sessions = AppData.get('sessions');
  // Once sessions has loaded (even to an empty list) it is authoritative; the
  // attention summary is only a fallback so the badge can show before that load.
  if (AppData.status('sessions') === 'ready' && Array.isArray(sessions)) {
    return sessions.filter(s => s.attention && s.attention.needs).length;
  }
  const summary = AppData.get('attention');
  return summary && summary.needs_attention != null
    ? Math.max(0, Number(summary.needs_attention) || 0)
    : 0;
}

function _renderAttentionBadge() {
  const badge = document.getElementById('attention-count');
  if (!badge) return;
  const count = attentionCount();
  badge.textContent = String(count);
  badge.classList.toggle('hidden', count === 0);
  const row = document.getElementById('attention-control');
  if (row) {
    row.title = count === 0
      ? 'Every recording has its speakers named'
      : `${count} recording${count === 1 ? '' : 's'} need speaker work`;
  }
}

function _initAttentionControl() {
  AppData.subscribe(['attention', 'sessions'], _renderAttentionBadge);
  _syncAttentionControlState();
  _renderAttentionBadge();
}

/* ── Boot ─────────────────────────────────────────────────────────────────
 * The workspace DOM is always present, so init-time wiring runs
 * unconditionally: there is no "this is not the workspace page" any more.
 * ─────────────────────────────────────────────────────────────────────── */

{
  // Auto-scroll behavior:
  // - Live recording: disable when user scrolls up, re-enable at bottom
  // - Playback: disable on user-initiated scroll only, re-enable via button click
  const _transcriptEl = document.getElementById('transcript');
  if (_transcriptEl) _transcriptEl.addEventListener('scroll', () => {
    // Ignore programmatic scrolls (from playback tracking, seek, button clicks, etc.)
    if (_programmaticScrollCount > 0) return;

    if (_playbackActive && !_playbackAudio.paused) {
      // During playback, only user-initiated scrolls disable auto-scroll
      if (_autoScroll) {
        _autoScroll = false;
        updateAutoScrollBtn();
      }
    } else {
      // Live mode: re-enable at bottom (reuse the captured element, no re-query)
      const atBottom = _transcriptEl.scrollHeight - _transcriptEl.scrollTop - _transcriptEl.clientHeight < 40;
      if (_autoScroll !== atBottom) {
        _autoScroll = atBottom;
        updateAutoScrollBtn();
      }
    }
  }, { passive: true });
}

// Import drag-and-drop init
_initImportDragDrop();
_initSidebarDragAutoScroll();
_initAttentionControl();

// Shared init (sidebar, SSE, status, devices, models)
connectSSE();

// Close SSE on page unload to prevent connection leaks when navigating
window.addEventListener('beforeunload', () => {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }
});

AppData.subscribe(['sessions', 'folders'], _onSidebarSlices);
AppData.load('sessions');
AppData.load('folders');
AppData.load('attention');
_checkSemanticSearchReady();
fetch('/api/status').then(r => r.json()).then(d => {
  // Stop any orphaned audio test left over from a previous page session
  // (e.g. user refreshed while testing). Must happen before onStatus.
  if (d.is_testing) {
    fetch('/api/audio/test/stop', { method: 'POST' }).catch(() => {});
  }
  onStatus(d);
});

fetch('/api/ai_settings')
  .then(r => r.json())
  .then(aiCfg => {
    currentAiModels = { ...AI_MODELS, ..._getAiModels(aiCfg.models) };
    _currentAiProvider = aiCfg.provider;
    _currentAiModel = aiCfg.model;
    _toolOverrides.summary_provider = aiCfg.summary_provider || null;
    _toolOverrides.summary_model = aiCfg.summary_model || null;
    _toolOverrides.chat_provider = aiCfg.chat_provider || null;
    _toolOverrides.chat_model = aiCfg.chat_model || null;
    _toolOverrides.global_chat_provider = aiCfg.global_chat_provider || null;
    _toolOverrides.global_chat_model = aiCfg.global_chat_model || null;
    _updateSessionModelLabels();
  })
  .catch(() => {});

startVizLoop();
startBrandVizLoop();
// Returning to a foreground tab re-kicks the parked loops; they re-evaluate the
// settled state on the first frame and immediately re-park if levels are zero.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    _startVizLoop();
    _startBrandVizLoop();
  }
});
initGainSliders();
_restoreSidebarPanes();

_tnInitSearch();
_tsbInitAutocomplete();
_syncPanelBottomRadius();
_syncSummaryBottomRadius();

// Load preferences first, then init components that depend on saved values.
// _prefsReady is kept separate from _devicesReady: preferences restore the saved
// sidebar filter, so anything that has to win over that default (a ?attention=
// link, for one) has to wait for preferences alone, not for device enumeration.
const _prefsReady = loadPreferences();
let _devicesReady = _prefsReady.then(() => {
  loadModelConfig();
  return loadAudioDevices();
});
// Screen recording: load displays + sync toggle
_apLoad().then(() => { try { _syncScreenToggle(); } catch {} });
try { loadScreenDisplays(); } catch {}

_startPeriodicUpdateCheck();

// Fire-and-forget: if HEAD has changed since the last visit, surface the
// What's New popup. Defer slightly so the page lands and renders first.
setTimeout(() => { _checkWhatsNew().catch(() => {}); }, 800);

loadSummaryPrompt();
_syncUploadBtn();

/* ── The views register their lifecycle, then the route is applied ────────── */

Views.register('speakers', {
  activate() { loadFingerprintPanel({ cached: true }); },
});

Views.register('session', {
  activate() { updateTopbarSessionTitle(); recalcColWidths(); },
});

_initRouteLinks();

// Ctrl+N: a new workspace from anywhere. Ctrl+K: the global search.
document.addEventListener('keydown', e => {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
  const key = (e.key || '').toLowerCase();
  if (key === 'n' && !e.shiftKey) {
    e.preventDefault();
    newSession();
  } else if (key === 'k') {
    e.preventDefault();
    _expandHeaderSearch();
  }
});

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const search = document.getElementById('header-search');
  if (search && search.classList.contains('is-expanded')) _collapseHeaderSearch();
});

// The Ask rail docks or overlays on the width actually left for the view.
if (window.ResizeObserver) {
  const mainArea = document.getElementById('main-area');
  if (mainArea) new ResizeObserver(() => _syncAskRailMode()).observe(mainArea);
}

// "updated 2 min ago" has to keep being true while the window sits open, and
// it has to be right the moment a slice lands.
setInterval(_syncRefreshTooltip, 30000);
AppData.subscribe(['sessions', 'folders', 'analytics', 'attention', 'calendarStatus', 'calendarEvents'],
                  _syncRefreshTooltip);

/* ── Reconciling after a gap ──────────────────────────────────────────────── */

let _sseEverConnected = false;
let _lastFocusAt = Date.now();

/** After an SSE reconnect, or a window that was away for more than a minute,
 *  re-read status and the shared slices instead of trusting what is on screen. */
function _reconcileAfterGap(reason) {
  fetch('/api/status', { cache: 'no-store' })
    .then(r => r.json())
    .then(onStatus)
    .catch(() => {});
  AppData.invalidate(['sessions', 'folders', 'attention'], reason);
  const active = VIEW_SLICES[Views.current] || [];
  if (active.includes('analytics')) AppData.invalidate(['analytics'], reason);
  if (active.includes('calendarStatus')) AppData.invalidate(['calendarStatus'], reason);
}

window.addEventListener('blur', () => { _lastFocusAt = Date.now(); });
window.addEventListener('focus', () => {
  if (Date.now() - _lastFocusAt > 60000) _reconcileAfterGap('window_focus');
  _lastFocusAt = Date.now();
});

/* ── The first route ──────────────────────────────────────────────────────── */

// Deferred to DOMContentLoaded so home.js, calendar.js and attention.js have
// registered their views (they are parsed after this file).
function _bootRoute() {
  _restoreCaptureSetup();
  _syncAskRailForView(window.MA_INITIAL_VIEW || 'home');
  const url = location.pathname + location.search;
  const initial = window.MA_INITIAL_VIEW || 'home';
  const parsed = _routeOf(location.pathname);
  // The server told us which view it rendered; the URL is the source of the
  // query actions. They only disagree if someone hand-edited the address. The
  // server already marked this view active, so skip the entry crossfade to
  // avoid a flash on the first paint.
  Views.show(parsed || initial, { url, replace: true, noFade: true });
  _applyRouteQuery(parsed || initial, new URLSearchParams(location.search), {});
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _bootRoute, { once: true });
} else {
  _bootRoute();
}
