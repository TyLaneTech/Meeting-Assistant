/* ── Calendar view ────────────────────────────────────────────────────────────
 * A month grid where the schedule and the capture are one vocabulary: scheduled
 * meetings, live recordings, recorded-and-matched pairs, "Not recorded" misses,
 * recordings with no meeting, tentative, private and all-day events all read on
 * the same grid, and the day panel is a chronological agenda over both.
 *
 * Session timestamps are naive UTC (storage._now uses datetime.utcnow), so the
 * sidebar convention applies here too: append 'Z' before new Date(), then read
 * the local calendar fields. Calendar event times already carry a real offset.
 *
 * app.js owns the shell, the store and the SSE stream; calendar.js owns the
 * grid, the day panel and this view's lifecycle. Nothing here fetches a cached
 * read: recordings come from the sessions slice and scheduled meetings from the
 * calendarEvents slice, keyed by the visible range and loaded once per range.
 * "Sync calendar" is the one exception, a POST that pulls the external feed.
 *
 * calendarBuildItems() is a pure fold of recordings and events into one list of
 * view items, one per matched pair. It touches no DOM and no store, so it runs
 * under node for the unit tests; everything after the browser guard is view.
 * ─────────────────────────────────────────────────────────────────────────── */

const CAL_WEEKS = 6;   // fixed height so paging months does not jog the page
// The fewest content rows a cell shows. The grid fills the window, so a tall
// window earns more chips per day: _calMeasureCapacity() reads the rendered
// cell and chip heights and raises this cap to whatever fits.
const CAL_ITEMS_PER_DAY = 3;
let _calItemsPerDay = CAL_ITEMS_PER_DAY;

const _calState = {
  year: null,
  month: null,          // 0-11
  enabled: false,
  sessions: [],
  events: [],           // the calendarEvents slice for the visible range
  range: null,          // that slice's cache key
  byDay: new Map(),     // 'YYYY-M-D' -> item[]
  selectedKey: null,
};

/* ── Pure helpers (safe under node, no DOM, no store) ─────────────────────── */

/* Escaping is app.js's escapeHtml, which is loaded first on this page and
   escapes quotes. A local text-node escaper would not, and every string below
   lands in a title="..." attribute. */

/** Local Date for a session start. Mirrors the sidebar's UTC-to-local rule. */
function _calStart(session) {
  return new Date(session.started_at + 'Z');
}

function _calDayKey(date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

/** An event's local ISO day ("YYYY-MM-DD") as the internal 'YYYY-M-D' key. */
function _calDayKeyFromISO(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  return `${Number(m[1])}-${Number(m[2]) - 1}-${Number(m[3])}`;
}

/** Seconds of recorded audio. Mirrors app.js _sessionDurationSec. */
function _calDurationSec(session) {
  if (session.last_segment_time != null && session.last_segment_time > 0) {
    return session.last_segment_time;
  }
  if (session.ended_at) {
    const start = _calStart(session);
    const end = new Date(session.ended_at + 'Z');
    return Math.max(0, (end - start) / 1000);
  }
  return 0;
}

function _calDuration(seconds) {
  if (!seconds || seconds <= 0) return '';
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${Math.max(m, 1)}m`;
}

function _calTime(date) {
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

/** The word for a kind, so colour is never the only signal. */
function _calStateLabel(kind) {
  return {
    scheduled: 'Scheduled', recorded: 'Recorded', recording: 'Recording',
    live: 'Live', missed: 'Not recorded', allday: 'All day',
  }[kind] || 'Scheduled';
}

function _calSessionsById(sessions) {
  const map = new Map();
  for (const s of (sessions || [])) map.set(s.id, s);
  return map;
}

/**
 * Fold recordings (sessions) and calendar events into one list of view items,
 * one per matched pair. An event that carries a session_id, and any session
 * matched to one, render once as the combined item; unmatched sessions become
 * Recording items; unmatched events read by their state.
 *
 * opts: { enabled, liveSessionId }. Pure: no DOM, no store.
 */
function calendarBuildItems(sessions, events, opts) {
  const o = opts || {};
  const liveId = o.liveSessionId || null;
  const byId = _calSessionsById(sessions);
  const matched = new Set();
  const items = [];

  if (o.enabled && Array.isArray(events)) {
    for (const e of events) {
      // An all-day block never claims a timed recording: ignore any session
      // match so the recording still renders as its own reachable chip.
      const sid = (e.all_day ? null : e.session_id) || null;
      let kind;
      if (e.all_day) kind = 'allday';
      else if (e.state === 'recording' || (sid && sid === liveId)) kind = 'live';
      else if (e.state === 'recorded') kind = 'recorded';
      else if (e.state === 'missed') kind = 'missed';
      else kind = 'scheduled';           // 'upcoming', and any timed fallback
      if (sid) matched.add(sid);
      const start = e.start ? new Date(e.start) : null;
      const end = e.end ? new Date(e.end) : null;
      const session = sid ? byId.get(sid) : null;
      const isRec = kind === 'recorded' || kind === 'recording' || kind === 'live';
      const durationSec = isRec && session
        ? _calDurationSec(session)
        : (start && end ? Math.max(0, (end - start) / 1000) : 0);
      items.push({
        id: sid || e.key,
        kind,
        title: e.title || (session && session.title) || 'Untitled',
        start, end,
        allDay: !!e.all_day,
        tentative: e.status === 'tentative',
        private: !!e.private,
        sessionId: sid,
        needsAttention: !!(session && session.attention && session.attention.needs),
        attention: session ? session.attention : null,
        durationSec,
        dayKey: e.all_day
          ? _calDayKeyFromISO(e.day)
          : (start ? _calDayKey(start) : _calDayKeyFromISO(e.day)),
      });
    }
  }

  for (const s of (sessions || [])) {
    if (!s.started_at || matched.has(s.id)) continue;
    const start = _calStart(s);
    const durationSec = _calDurationSec(s);
    const end = new Date(start.getTime() + Math.max(1, durationSec) * 1000);
    const live = !!(liveId && s.id === liveId);
    items.push({
      id: s.id,
      kind: live ? 'live' : 'recording',
      title: s.title || s.id,
      start, end,
      allDay: false,
      tentative: false,
      private: false,
      sessionId: s.id,
      needsAttention: !!(s.attention && s.attention.needs),
      attention: s.attention || null,
      durationSec,
      dayKey: _calDayKey(start),
    });
  }
  return items;
}

/** All-day first, then chronological. */
function _calItemOrder(a, b) {
  if (a.allDay !== b.allDay) return a.allDay ? -1 : 1;
  const at = a.start ? a.start.getTime() : 0;
  const bt = b.start ? b.start.getTime() : 0;
  return at - bt;
}

/** The needs-attention state as a phrase, matching the dashboard's wording. */
function _calAttentionReason(attention) {
  if (!attention || !attention.needs) return '';
  const parts = [];
  const unresolved = Number(attention.unresolved) || 0;
  if (unresolved > 0) {
    parts.push(`${unresolved} speaker${unresolved === 1 ? '' : 's'} unresolved`);
  }
  if ((attention.reasons || []).includes('speaker_count_mismatch')) {
    parts.push(`expected ${attention.expected}, found ${attention.found}`);
  }
  return parts.join(' · ') || 'Needs a look';
}

/** One line shared by the chip tooltip and the accessible label. */
function _calItemTip(it) {
  const parts = [];
  if (it.allDay) parts.push('All day');
  else if (it.start) parts.push(it.end ? `${_calTime(it.start)} to ${_calTime(it.end)}` : _calTime(it.start));
  parts.push(it.title);
  const dur = _calDuration(it.durationSec);
  if (dur) parts.push(dur);
  parts.push(_calStateLabel(it.kind));
  if (it.tentative && it.kind !== 'missed') parts.push('Tentative');
  return parts.join(' · ');
}

/* ── Data ─────────────────────────────────────────────────────────────────── */

/** The six-week range the grid shows, as the calendarEvents cache key. */
function _calVisibleRange() {
  const { year, month } = _calState;
  if (year == null) return null;
  const first = new Date(year, month, 1);
  const gridStart = new Date(year, month, 1 - first.getDay());
  const gridEnd = new Date(gridStart.getFullYear(), gridStart.getMonth(),
                           gridStart.getDate() + CAL_WEEKS * 7 - 1);
  const iso = d => d.toLocaleDateString('en-CA');
  return calendarRangeKey(iso(gridStart), iso(gridEnd));
}

/** Read the store and repaint. Revisiting a loaded month is synchronous. */
function _calLoad() {
  const range = _calVisibleRange();
  if (range) AppData.load('calendarEvents', { key: range });
  const sessions = AppData.get('sessions');
  const status = AppData.get('calendarStatus');
  const enabled = !!(status && (status.enabled != null ? status.enabled : status.calendar_enabled));
  const payload = range ? AppData.get('calendarEvents', range) : null;
  const events = (payload && payload.events) || [];
  const liveId = (typeof state !== 'undefined' && state && state.isRecording) ? state.sessionId : null;

  _calState.range = range;
  _calState.enabled = enabled;
  _calState.sessions = (Array.isArray(sessions) ? sessions : []).filter(s => s.started_at);
  _calState.events = events;

  const items = calendarBuildItems(_calState.sessions, events, { enabled, liveSessionId: liveId });
  _calState.byDay = new Map();
  for (const it of items) {
    if (!it.dayKey) continue;
    if (!_calState.byDay.has(it.dayKey)) _calState.byDay.set(it.dayKey, []);
    _calState.byDay.get(it.dayKey).push(it);
  }
  for (const list of _calState.byDay.values()) list.sort(_calItemOrder);
  _calRender();
}

/* ── Sync calendar: the external feed, not the app's cached reads ─────────── */

async function _calSync() {
  const btn = document.getElementById('cal-sync');
  if (btn) { btn.disabled = true; btn.classList.add('is-loading'); btn.innerHTML = '<i class="fa-solid fa-rotate fa-spin" aria-hidden="true"></i> Syncing'; }
  try {
    const res = await fetch('/api/calendar/refresh', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await AppData.invalidate(['calendarStatus', 'calendarEvents', 'sessions', 'attention'], 'calendar_sync');
    uiToast({ message: 'Calendar synced', kind: 'success' });
  } catch (e) {
    // A failed sync keeps the stale data on screen. The backend records the
    // reason on the status slice, so the banner reads it back below.
    await AppData.invalidate(['calendarStatus'], 'calendar_sync_failed');
    uiToast({ message: 'Could not sync the calendar', kind: 'error' });
  }
  if (btn) { btn.disabled = false; btn.classList.remove('is-loading'); btn.innerHTML = '<i class="fa-solid fa-rotate" aria-hidden="true"></i> Sync calendar'; }
  _calRenderSyncState();
}

/** The sync note ("Synced 12m ago") and the stale-sync banner, both from the
 *  calendarStatus slice. A failed refresh keeps the last data visible. */
function _calRenderSyncState() {
  const status = AppData.get('calendarStatus');
  const enabled = !!(status && (status.enabled != null ? status.enabled : status.calendar_enabled));

  const note = document.getElementById('cal-sync-note');
  if (note) {
    if (!status) {
      note.textContent = '';
    } else if (!enabled) {
      note.innerHTML = 'Calendar is not connected. '
        + '<a href="/session?settings=1&amp;section=calendar">Connect your calendar</a>';
    } else {
      const last = status.last_refresh || status.last_sync || status.last_refreshed_at;
      note.textContent = last ? `Synced ${_timeAgo(last)}` : 'Not synced yet';
    }
  }

  const banner = document.getElementById('cal-banner');
  if (banner) {
    const err = status ? String(status.last_error || '') : '';
    if (enabled && err) {
      banner.textContent = `Calendar could not sync: ${err}. Showing the last successful sync.`;
      banner.classList.remove('hidden');
    } else {
      banner.textContent = '';
      banner.classList.add('hidden');
    }
  }
}

/* ── Month grid ───────────────────────────────────────────────────────────── */

/** Chip inner markup. Two lines: time (with any tag and the attention dot) on
 *  top, a labelled or glyphed title below, so state never reads by colour only. */
function _calChipGlyph(kind) {
  if (kind === 'recorded' || kind === 'recording' || kind === 'live') return 'fa-solid fa-waveform-lines';
  if (kind === 'missed') return 'fa-regular fa-calendar-xmark';
  if (kind === 'scheduled') return 'fa-regular fa-calendar';
  return '';
}

function _calChipTag(it) {
  if (it.kind === 'live') return 'Live';
  if (it.kind === 'recording') return 'Recording';
  if (it.kind === 'missed') return 'Not recorded';
  if (it.tentative) return 'Tentative';
  return '';
}

function _calChipHtml(it) {
  const cls = ['cal-chip', 'cal-chip--' + it.kind];
  if (it.needsAttention) cls.push('needs-attention');
  const tip = escapeHtml(_calItemTip(it));
  const time = it.start ? escapeHtml(_calTime(it.start)) : '';
  const tag = _calChipTag(it);
  const glyph = _calChipGlyph(it.kind);
  const dot = it.needsAttention
    ? '<span class="cal-chip-dot" aria-label="Needs attention"></span>' : '';
  const line = (time || tag || dot)
    ? `<span class="cal-chip-line">`
      + (time ? `<span class="cal-chip-time">${time}</span>` : '')
      + (tag ? `<span class="cal-chip-tag">${escapeHtml(tag)}</span>` : '')
      + dot
      + `</span>`
    : '';
  const title = `<span class="cal-chip-title">`
    + (glyph ? `<i class="${glyph} cal-chip-glyph" aria-hidden="true"></i>` : '')
    + `<span class="cal-chip-titletext">${escapeHtml(it.title)}</span></span>`;
  const body = line + title;

  if (it.sessionId && (it.kind === 'recorded' || it.kind === 'recording' || it.kind === 'live')) {
    return `<a class="${cls.join(' ')}" href="/session?id=${encodeURIComponent(it.sessionId)}" title="${tip}">${body}</a>`;
  }
  return `<div class="${cls.join(' ')}" title="${tip}">${body}</div>`;
}

function _calAllDayChipHtml(it) {
  const tip = escapeHtml(_calItemTip(it));
  return `<div class="cal-allday-chip" title="${tip}">`
    + `<span class="cal-chip-titletext">${escapeHtml(it.title)}</span></div>`;
}

function _calRender() {
  const { year, month } = _calState;
  const first = new Date(year, month, 1);
  const gridStart = new Date(year, month, 1 - first.getDay());
  const todayKey = _calDayKey(new Date());

  let monthCount = 0;
  let monthSeconds = 0;
  for (const s of _calState.sessions) {
    const d = _calStart(s);
    if (d.getFullYear() === year && d.getMonth() === month) {
      monthCount++;
      monthSeconds += _calDurationSec(s);
    }
  }
  const countLine = monthCount === 0
    ? 'No recordings this month'
    : `${monthCount} recording${monthCount === 1 ? '' : 's'}, ${_calDuration(monthSeconds) || 'under a minute'}`;
  Views.setTitle('calendar',
    first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }), countLine);
  _calRenderSyncState();

  const cells = [];
  for (let i = 0; i < CAL_WEEKS * 7; i++) {
    const day = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
    const key = _calDayKey(day);
    const items = _calState.byDay.get(key) || [];
    const classes = ['cal-day'];
    if (day.getDay() === 0 || day.getDay() === 6) classes.push('is-weekend');
    if (day.getMonth() !== month) classes.push('is-outside');
    if (key === todayKey) classes.push('is-today');
    if (key === _calState.selectedKey) classes.push('is-selected');
    if (items.length) classes.push('has-items');

    // A cell shows as many items as fit its height (see _calMeasureCapacity),
    // never fewer than three rows. When there are more, the last row becomes
    // a "+N more" that opens the day panel, so nothing clips.
    const cap = _calItemsPerDay;
    const overCap = items.length > cap;
    const shown = overCap ? items.slice(0, cap - 1) : items;
    const allDayHtml = shown.filter(it => it.allDay).map(_calAllDayChipHtml).join('');
    const timedHtml = shown.filter(it => !it.allDay).map(_calChipHtml).join('');
    const hidden = items.length - shown.length;
    const moreHtml = hidden > 0
      ? `<span class="cal-more">+${hidden} more</span>` : '';

    const label = `${day.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}, `
      + (items.length ? `${items.length} item${items.length === 1 ? '' : 's'}` : 'nothing scheduled or recorded');

    cells.push(`
      <div class="${classes.join(' ')}" data-key="${key}"
           aria-label="${escapeHtml(label)}"
           tabindex="${items.length ? '0' : '-1'}">
        <span class="cal-day-num">${day.getDate()}</span>
        ${allDayHtml ? `<div class="cal-day-allday">${allDayHtml}</div>` : ''}
        <div class="cal-day-items">${timedHtml}${moreHtml}</div>
      </div>`);
  }

  document.getElementById('cal-grid').innerHTML = cells.join('');

  if (_calState.selectedKey) _calRenderDetail();
  _calFitCapacity();
}

/** How many chips a day cell can hold at the grid's current height.
 *  Reads the painted cell, day number and chip heights, so it follows the
 *  real type size and padding rather than a guess. */
function _calMeasureCapacity() {
  const grid = document.getElementById('cal-grid');
  const cell = grid && grid.querySelector('.cal-day');
  if (!cell) return _calItemsPerDay;
  const cs = getComputedStyle(cell);
  const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  const cellGap = parseFloat(cs.rowGap) || parseFloat(cs.gap) || 0;
  const num = cell.querySelector('.cal-day-num');
  const numH = num ? num.getBoundingClientRect().height : 16;
  const chip = grid.querySelector('.cal-chip') || grid.querySelector('.cal-allday-chip');
  const chipH = chip ? chip.getBoundingClientRect().height : 30;
  const items = grid.querySelector('.cal-day-items');
  const itemGap = items ? (parseFloat(getComputedStyle(items).rowGap) || 0) : 2;
  const avail = cell.getBoundingClientRect().height - padY - numH - cellGap;
  const n = Math.floor((avail + itemGap) / (chipH + itemGap));
  return Math.max(CAL_ITEMS_PER_DAY, Math.min(14, n));
}

let _calFitting = false;
function _calFitCapacity() {
  if (_calFitting) return;               // one corrective repaint per render
  const cap = _calMeasureCapacity();
  if (cap === _calItemsPerDay) return;
  _calItemsPerDay = cap;
  _calFitting = true;
  try { _calRender(); } finally { _calFitting = false; }
}

/* ── Day detail panel: a chronological agenda over recordings and events ───── */

function _calOpenDay(key) {
  _calState.selectedKey = key;
  document.getElementById('cal-detail').classList.remove('hidden');
  _calRender();   // repaints the grid selection and the detail body
  _calSyncUrl();
}

function _calCloseDetail() {
  _calState.selectedKey = null;
  document.getElementById('cal-detail').classList.add('hidden');
  _calRender();
  _calSyncUrl();
}

/** One action per row: Open recording, Clean up speakers, or Show in Settings. */
function _calAgendaAction(it) {
  const isRec = it.sessionId && (it.kind === 'recorded' || it.kind === 'recording' || it.kind === 'live');
  if (isRec && it.needsAttention) {
    return `<a class="btn btn-primary cal-agenda-action"`
      + ` href="/session?id=${encodeURIComponent(it.sessionId)}&amp;speakers=cleanup">Clean up speakers</a>`;
  }
  if (isRec) {
    return `<a class="btn btn-secondary cal-agenda-action"`
      + ` href="/session?id=${encodeURIComponent(it.sessionId)}">Open recording</a>`;
  }
  return `<a class="btn btn-quiet cal-agenda-action"`
    + ` href="/session?settings=1&amp;section=calendar">Show in Settings</a>`;
}

function _calAgendaRowHtml(it) {
  const time = it.allDay ? 'All day' : (it.start ? _calTime(it.start) : '');
  const dur = _calDuration(it.durationSec);
  const stateCls = 'cal-agenda-state cal-agenda-state--' + it.kind;
  const metaBits = [`<span class="${stateCls}">${escapeHtml(_calStateLabel(it.kind))}</span>`];
  if (dur) metaBits.push(`<span class="cal-agenda-dur">${escapeHtml(dur)}</span>`);
  if (it.tentative && it.kind !== 'missed') metaBits.push('<span class="cal-agenda-dur">Tentative</span>');
  const reason = _calAttentionReason(it.attention);
  const attn = it.needsAttention
    ? `<p class="cal-agenda-attn"><span class="cal-chip-dot" aria-label="Needs attention"></span>`
      + `${escapeHtml(reason)}</p>`
    : '';
  return `
    <article class="cal-agenda-row">
      <div class="cal-agenda-time">${escapeHtml(time)}</div>
      <div class="cal-agenda-main">
        <h4 class="cal-agenda-title">${escapeHtml(it.title)}</h4>
        <div class="cal-agenda-meta">${metaBits.join('<span class="cal-agenda-sep">&middot;</span>')}</div>
        ${attn}
      </div>
      <div class="cal-agenda-act">${_calAgendaAction(it)}</div>
    </article>`;
}

function _calRenderDetail() {
  const key = _calState.selectedKey;
  if (!key) return;
  const [y, m, d] = key.split('-').map(Number);
  const date = new Date(y, m, d);
  const items = _calState.byDay.get(key) || [];

  document.getElementById('cal-detail-title').textContent =
    date.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });

  const body = document.getElementById('cal-detail-body');
  if (!items.length) {
    body.innerHTML = '<p class="cal-detail-empty">Nothing recorded or scheduled.</p>';
    return;
  }
  body.innerHTML = items.map(_calAgendaRowHtml).join('');
}

/* ── Navigation ───────────────────────────────────────────────────────────── */

function _calShiftMonth(delta) {
  const next = new Date(_calState.year, _calState.month + delta, 1);
  _calState.year = next.getFullYear();
  _calState.month = next.getMonth();
  _calLoad();
  _calSyncUrl();
}

function _calGoToday() {
  const now = new Date();
  _calState.year = now.getFullYear();
  _calState.month = now.getMonth();
  _calLoad();
  _calSyncUrl();
}

/* ── Deep links: /calendar?month=YYYY-MM&day=YYYY-MM-DD ───────────────────── */

function _calMonthParam() {
  if (_calState.year == null) return null;
  return `${_calState.year}-${String(_calState.month + 1).padStart(2, '0')}`;
}

/** The internal day key is 'YYYY-M-D' (month 0-based); the URL is ISO. */
function _calDayParam(key) {
  if (!key) return null;
  const [y, m, d] = String(key).split('-').map(Number);
  return `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function _calKeyFromParam(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ''));
  if (!m) return null;
  return `${Number(m[1])}-${Number(m[2]) - 1}-${Number(m[3])}`;
}

/** Write the calendar's own URL through the shell router helper, not a raw
 *  pushState: paging months and opening a day replace, never stack history. */
function _calSyncUrl() {
  if (typeof Views === 'undefined' || Views.current !== 'calendar') return;
  const params = new URLSearchParams();
  const month = _calMonthParam();
  if (month) params.set('month', month);
  const day = _calDayParam(_calState.selectedKey);
  if (day) params.set('day', day);
  const qs = params.toString();
  const url = qs ? `/calendar?${qs}` : '/calendar';
  Views._writeHistory('calendar', url, true);
}

/** Applied by the router on activation and on Back or Forward. */
function _calApplyRoute(month, day) {
  const m = /^(\d{4})-(\d{2})$/.exec(String(month || ''));
  if (m) {
    _calState.year = Number(m[1]);
    _calState.month = Number(m[2]) - 1;
  } else if (_calState.year == null) {
    const now = new Date();
    _calState.year = now.getFullYear();
    _calState.month = now.getMonth();
  }
  const key = _calKeyFromParam(day);
  _calState.selectedKey = key;
  const detail = document.getElementById('cal-detail');
  if (detail) detail.classList.toggle('hidden', !key);
  _calLoad();
}

/* ── Init and lifecycle ───────────────────────────────────────────────────── */

let _calSyncTimer = null;

function _calTick() {
  if (typeof Views !== 'undefined' && Views.current === 'calendar') _calRenderSyncState();
}

function _calInit() {
  document.getElementById('cal-sync').addEventListener('click', _calSync);
  document.getElementById('cal-prev').addEventListener('click', () => _calShiftMonth(-1));
  document.getElementById('cal-next').addEventListener('click', () => _calShiftMonth(1));
  document.getElementById('cal-today').addEventListener('click', _calGoToday);
  document.getElementById('cal-detail-close').addEventListener('click', _calCloseDetail);

  // One delegated handler: the grid is rebuilt on every month change. Recording
  // chips are real links and reach the router; a click anywhere else in a cell
  // (a scheduled chip, "+N more", empty space) opens the day panel.
  const grid = document.getElementById('cal-grid');
  grid.addEventListener('click', e => {
    if (e.target.closest('a')) return;
    const cell = e.target.closest('.cal-day');
    if (cell) _calOpenDay(cell.dataset.key);
  });
  grid.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const cell = e.target.closest('.cal-day');
    if (!cell) return;
    e.preventDefault();
    _calOpenDay(cell.dataset.key);
  });

  document.addEventListener('keydown', _calOnKey);

  // The grid fills the window, so a resize changes how many chips fit a day.
  if (typeof ResizeObserver !== 'undefined') {
    let t = null;
    new ResizeObserver(() => {
      if (Views.current !== 'calendar') return;
      clearTimeout(t);
      t = setTimeout(_calFitCapacity, 80);
    }).observe(grid);
  }
}

/** Month paging keys belong to the calendar view only; every other view has
 *  the handler installed but inert. */
function _calOnKey(e) {
  if (Views.current !== 'calendar') return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  // Never steal keys from an open dialog (settings, uiConfirm) or menu.
  if (document.querySelector('.overlay:not(.hidden), .ui-dialog-overlay')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); _calShiftMonth(-1); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); _calShiftMonth(1); }
  else if (e.key === 'Escape' && _calState.selectedKey) { e.preventDefault(); _calCloseDetail(); }
}

if (typeof window !== 'undefined' && typeof Views !== 'undefined' && typeof AppData !== 'undefined') {
  Views.register('calendar', {
    activate() {
      AppData.load('calendarStatus');
      _calLoad();
      if (!_calSyncTimer) _calSyncTimer = setInterval(_calTick, 60000);
    },
    deactivate() {
      // Suspend the "Synced N ago" ticker; nothing else here runs while hidden.
      if (_calSyncTimer) { clearInterval(_calSyncTimer); _calSyncTimer = null; }
    },
  });

  // The grid is a store subscriber: resolving speakers or syncing the feed in
  // another view repaints it without a fetch of its own.
  AppData.subscribe(['sessions', 'calendarEvents', 'calendarStatus'], () => {
    if (Views.current === 'calendar') _calLoad();
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _calInit, { once: true });
  } else {
    _calInit();
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { calendarBuildItems, _calStateLabel, _calItemOrder };
}
