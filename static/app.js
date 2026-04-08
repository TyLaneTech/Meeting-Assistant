/* ── marked.js setup ─────────────────────────────────────────────────────── */
marked.use({ breaks: true, gfm: true });

function renderMd(text) {
  return marked.parse(text || '');
}

/**
 * Post-process rendered summary HTML to make timestamps clickable pills.
 * Matches single timestamps [M:SS] and ranges [M:SS–M:SS] (en-dash, em-dash,
 * or plain hyphen as separator). Clicking seeks to the start of the range.
 */
function linkifyTimestamps(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  // Group 1: start time. Group 2 (optional): end time after –, —, or -
  const timestampRe = /\[(\d{1,2}:\d{2})(?:[–—-](\d{1,2}:\d{2}))?\]/g;
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
      // Use en-dash (–) as canonical separator in the displayed label
      const label = match[2] ? `${match[1]} - ${match[2]}` : match[1];
      const title = match[2]
        ? `Jump to ${match[1]} – ${match[2]}`
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

/* ── Pane toggle & column ordering ────────────────────────────────────────── */
let _paneVisible = [true, true, true]; // indexed by column: [transcript, summary, chat]
const _COL_NAMES = ['Transcript', 'Summary', 'Chat'];

// Visual column order — maps position (left→right) to column index.
// Seeded from localStorage cache so the first paint uses the saved order.
let _colOrder = (() => {
  const lc = _getLayoutCache();
  return (Array.isArray(lc.col_order) && lc.col_order.length === 3)
    ? lc.col_order
    : [0, 1, 2];
})();

function _syncToggleButtons() {
  const btns = document.querySelectorAll('.pane-toggle-group .pane-toggle-btn');
  _colOrder.forEach((colIdx, pos) => {
    if (!btns[pos]) return;
    btns[pos].onclick = () => togglePane(colIdx);
    btns[pos].title = _COL_NAMES[colIdx];
    btns[pos].classList.toggle('active', _paneVisible[colIdx]);
  });
}

function togglePane(idx) {
  // Don't allow hiding the last visible pane
  const visibleCount = _paneVisible.filter(Boolean).length;
  if (_paneVisible[idx] && visibleCount <= 1) return;

  _paneVisible[idx] = !_paneVisible[idx];
  _syncToggleButtons();
  _applyPaneLayout();
  _savePaneVisible();
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
      const arr = JSON.parse(raw);
      if (Array.isArray(arr) && arr.length === 3) {
        // Ensure at least one pane is visible
        if (arr.some(Boolean)) {
          _paneVisible = arr;
          _syncToggleButtons();
          _applyPaneLayout();
          return;
        }
      }
    }
  } catch (_) {}
  // Fallback: show all
  _paneVisible = [true, true, true];
  _syncToggleButtons();
  _applyPaneLayout();
}

function _applyPaneLayout() {
  if (window._isHomePage) return;
  const HANDLE_PX = 4;
  const MIN_COL_PX = 160;
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;

  // Stable column references (DOM order = column index, never changes)
  const colEls = [
    workspace.querySelector('.col-transcript'),
    workspace.querySelector('.col-summary'),
    workspace.querySelector('.col-chat'),
  ];
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
// Relative column proportions — updated when user drags; loaded from settings on init.
// Seeded from localStorage cache immediately so the IIFE below uses the right values.
let _colProportions = (() => {
  const lc = _getLayoutCache();
  return (Array.isArray(lc.col_proportions) && lc.col_proportions.length === 3)
    ? lc.col_proportions
    : [1, 1.1, 1.1];
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
    const visFracs = vis.map(i => _colProportions[i]);
    const fracSum = visFracs.reduce((a, b) => a + b, 0);
    // Return full 3-element array; hidden columns get 0
    const result = [0, 0, 0];
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

  window.addEventListener('resize', recalcColWidths);
})();

/* ── Column drag-to-reorder ──────────────────────────────────────────────── */
(function initColumnDragReorder() {
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;

  const colEls = [
    workspace.querySelector('.col-transcript'),
    workspace.querySelector('.col-summary'),
    workspace.querySelector('.col-chat'),
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

/* ── Sidebar resize handle ────────────────────────────────────────────────── */
(function initSidebarResize() {
  const sidebar = document.getElementById('sidebar');
  const handle  = document.getElementById('sidebar-resize-handle');
  if (!sidebar || !handle) return;

  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = sidebar.offsetWidth;

    handle.classList.add('dragging');
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';

    function onMove(ev) {
      const newW = Math.max(180, Math.min(520, startW + (ev.clientX - startX)));
      sidebar.style.width = newW + 'px';
    }

    function onUp() {
      handle.classList.remove('dragging');
      document.body.style.cursor     = '';
      document.body.style.userSelect = '';
      const w = sidebar.offsetWidth;
      if (typeof savePref === 'function') savePref('sidebar_width', w);
      _saveLayoutCache({ sidebar_width: w });
      recalcColWidths();
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup',   onUp);
    }

    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup',   onUp);
  });
})();

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

function startDurationCounter() {
  _recordingStartTime = Date.now();
  const el = document.getElementById('recording-duration');
  el.textContent = '0:00';
  el.classList.remove('hidden');
  _durationInterval = setInterval(() => {
    el.textContent = fmtDuration((Date.now() - _recordingStartTime) / 1000);
  }, 1000);
}

function stopDurationCounter() {
  clearInterval(_durationInterval);
  _durationInterval = null;
  _recordingStartTime = null;
  const el = document.getElementById('recording-duration');
  el.classList.add('hidden');
  el.textContent = '';
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

// Apply sidebar layout from cache synchronously — eliminates flash before async prefs load
{
  const _lc = _getLayoutCache();
  const _sb = document.getElementById('sidebar');
  const _ob = document.getElementById('sidebar-open-btn');
  if (_sb) {
    if (_lc.sidebar_width) _sb.style.width = _lc.sidebar_width + 'px';
    if (_lc.sidebar_open === false) {
      _sb.classList.add('collapsed');
      _sb.style.width = '';   // let CSS .collapsed { width:0 } take over
      state.sidebarOpen = false;
      if (_ob) _ob.style.display = '';
    }
  }
}

/* ── Preferences (server-persisted) ─────────────────────────────────────── */
let _prefs = {};   // populated on init from /api/preferences
let _prefsSaveTimer = null;

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
  // Apply column proportions and order
  if (Array.isArray(_prefs.col_proportions) && _prefs.col_proportions.length === 3) {
    _colProportions = _prefs.col_proportions;
  }
  if (Array.isArray(_prefs.col_order) && _prefs.col_order.length === 3) {
    _colOrder = _prefs.col_order;
    _syncToggleButtons();
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
  // Apply auto-summary toggle
  const autoBtn = document.getElementById('auto-summary-btn');
  if (autoBtn) {
    const enabled = _prefs.auto_summary !== false;
    autoBtn.classList.toggle('active', enabled);
  }
}

function savePref(key, value) {
  _prefs[key] = value;
  // Debounce writes so rapid changes don't flood the server
  clearTimeout(_prefsSaveTimer);
  _prefsSaveTimer = setTimeout(() => {
    fetch('/api/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_prefs),
    }).catch(() => {});
  }, 400);
}

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
function toggleSidebar() {
  state.sidebarOpen = !state.sidebarOpen;
  const sidebar = document.getElementById('sidebar');
  const openBtn  = document.getElementById('sidebar-open-btn');
  if (state.sidebarOpen) {
    sidebar.classList.remove('collapsed');
    // Restore custom width (if set by resize) so it overrides the CSS default
    if (_prefs.sidebar_width) sidebar.style.width = _prefs.sidebar_width + 'px';
    openBtn.style.display = 'none';
  } else {
    sidebar.classList.add('collapsed');
    // Clear inline width so CSS .collapsed { width: 0 } can take effect
    sidebar.style.width = '';
    openBtn.style.display = '';
  }
  savePref('sidebar_open', state.sidebarOpen);
  _saveLayoutCache({ sidebar_open: state.sidebarOpen });
  recalcColWidths();
}

// ── Sidebar state ─────────────────────────────────────────────────────────────
let _sidebarSelected    = new Set();      // selected session IDs
let _sidebarMultiselect = false;          // multiselect mode on/off
let _sidebarCollapsed   = (() => {        // collapsed folder IDs — persisted in localStorage
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
let _pendingSearchHighlight = null;       // {segmentId, query} — scroll+highlight after session load

async function refreshSidebar() {
  const [sessions, folders] = await Promise.all([
    fetch('/api/sessions').then(r => r.json()),
    fetch('/api/folders').then(r => r.json()).catch(() => []),
  ]);
  _sidebarAllSessions = sessions;
  _sidebarFolders = folders;
  _renderSidebar();
}

/* ── Sidebar search ───────────────────────────────────────────────────────── */
function _pulseSearchGlow() {
  const body = document.getElementById('session-list');
  if (!body) return;
  body.classList.remove('search-glow');
  void body.offsetWidth;          // force reflow — restarts animation instantly
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
  // — avoids restarting the dots animation on every keystroke
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

  // Strategy 2: text search fallback — find segments containing the query
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
    // Block self/descendant drops — don't call preventDefault so browser rejects the drop
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
  const sessions = _sidebarAllSessions;
  const folders  = _sidebarFolders;
  const list     = document.getElementById('session-list');
  const hasAny   = sessions.length > 0 || folders.length > 0;

  // ── Search mode: flat filtered list with snippets ──
  if (_sidebarSearchResults !== null) {
    list.innerHTML = '';
    const anyPending = _ftsSearchPending || _semanticSearchPending;
    if (_sidebarSearchResults.size === 0 && _sidebarSearchQuery) {
      if (anyPending) {
        list.innerHTML =
          '<div class="search-empty-state">' +
            '<div class="search-dots"><span></span><span></span><span></span></div>' +
            '<p>Searching…</p>' +
          '</div>';
      } else {
        list.innerHTML =
          '<div class="search-empty-state">' +
            '<div class="search-empty-icon">' +
              '<i class="fa-solid fa-magnifying-glass"></i>' +
            '</div>' +
            '<p>No matching sessions</p>' +
          '</div>';
      }
      return;
    }
    const sessionMap = new Map(sessions.map(s => [s.id, s]));
    const fragment = document.createDocumentFragment();
    for (const [sid, data] of _sidebarSearchResults) {
      const s = sessionMap.get(sid);
      if (!s) continue;
      const el = _makeSessionEl(s);
      const info = el.querySelector('.session-info');
      if (info) {
        // Show semantic similarity score bar
        if (data.score != null) {
          const scoreEl = document.createElement('div');
          scoreEl.className = 'session-search-score';
          const pct = Math.round(data.score * 100);
          scoreEl.innerHTML = `<span class="score-bar"><span class="score-fill" style="width:${pct}%"></span></span><span class="score-label">${pct}%</span>`;
          info.appendChild(scoreEl);
        }
        // Append match snippets as clickable elements
        if (data.matches?.length) {
          const matchesEl = document.createElement('div');
          matchesEl.className = 'session-search-matches';
          for (const m of data.matches.slice(0, 2)) {
            const snip = document.createElement('div');
            snip.className = 'session-search-snippet';
            if (m.segment_id != null || m.kind === 'segment') snip.classList.add('clickable');
            const kindLabel = m.kind === 'title' ? ''
              : m.kind === 'semantic' ? ''
              : `<span class="search-match-kind">${escapeHtml(m.kind)}</span>`;
            snip.innerHTML = kindLabel + m.snippet;
            // Click snippet → load session and jump to matching segment
            if (m.segment_id != null) {
              snip.addEventListener('click', e => {
                e.stopPropagation();
                _pendingSearchHighlight = { segmentId: m.segment_id, query: _sidebarSearchQuery };
                loadSession(sid);
              });
            } else if (m.kind === 'segment') {
              // FTS match without segment_id — fall back to text search
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
      // Default click (no specific snippet) — still set query for text highlight
      const origClick = el.onclick;
      el.addEventListener('click', () => {
        if (data.matches?.some(m => m.segment_id != null || m.kind === 'segment')) {
          const first = data.matches.find(m => m.segment_id != null);
          _pendingSearchHighlight = first
            ? { segmentId: first.segment_id, query: _sidebarSearchQuery }
            : { query: _sidebarSearchQuery };
        }
      }, true);  // capture phase — runs before the loadSession click
      fragment.appendChild(el);
    }
    list.appendChild(fragment);
    // Show refining indicator when semantic search is still running
    if (_semanticSearchPending && _sidebarSearchResults.size > 0) {
      const refining = document.createElement('div');
      refining.className = 'search-refining';
      refining.innerHTML = '<div class="search-dots sm"><span></span><span></span><span></span></div> Refining with AI…';
      list.appendChild(refining);
    }
    return;
  }

  // ── Normal mode: folder hierarchy + date groups ──
  if (!hasAny) {
    list.innerHTML = '<p class="sidebar-empty">No past sessions yet.</p>';
    _updateBulkBar();
    return;
  }

  // Build lookup structures
  const childMap = _buildChildMap(folders);
  const sessionsByFolder = new Map();
  for (const s of sessions) {
    const key = s.folder_id || null;
    if (!sessionsByFolder.has(key)) sessionsByFolder.set(key, []);
    sessionsByFolder.get(key).push(s);
  }
  // Sort sessions within each folder by sort_order
  for (const [, arr] of sessionsByFolder) {
    arr.sort((a, b) => a.sort_order - b.sort_order);
  }

  const folderIds = new Set(folders.map(f => f.id));
  const fragment = document.createDocumentFragment();

  // Render folder tree recursively from top-level
  _renderFolderSubtree(null, 0, fragment, childMap, sessionsByFolder, folderIds);

  // Ungrouped sessions (no folder or deleted folder) — also acts as a drop
  // target to remove sessions from folders.
  const ungroupedZone = document.createElement('div');
  ungroupedZone.className = 'sidebar-ungrouped-zone';

  const ungrouped = sessions.filter(s => !s.folder_id || !folderIds.has(s.folder_id));
  if (ungrouped.length) {
    ungrouped.sort((a, b) => b.started_at.localeCompare(a.started_at));
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
  _updateBulkBar();
}

function _renderFolderSubtree(parentId, depth, container, childMap, sessionsByFolder, folderIds) {
  const children = childMap.get(parentId) || [];
  for (const folder of children) {
    const folderSessions = sessionsByFolder.get(folder.id) || [];
    const totalCount = _countSessionsRecursive(folder.id, childMap, sessionsByFolder);
    const collapsed = _sidebarCollapsed.has(folder.id);

    const folderEl = document.createElement('div');
    folderEl.className = 'sidebar-folder';
    folderEl.dataset.folderId = folder.id;


    // Folder header
    const header = document.createElement('div');
    header.className = 'folder-header';
    header.draggable = true;

    // Drag start for folder
    header.addEventListener('dragstart', e => {
      e.stopPropagation();
      _sidebarDragType = 'folder';
      _sidebarDragIds = [folder.id];
      _dragDescendants = _getDescendantIds(folder.id, childMap);
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', JSON.stringify([folder.id]));
      folderEl.classList.add('dragging');
    });
    header.addEventListener('dragend', () => {
      folderEl.classList.remove('dragging');
      _removeDragIndicator();
      _dragDescendants.clear();
    });

    header.innerHTML = `
      <button class="folder-toggle"><i class="fa-solid fa-chevron-${collapsed ? 'right' : 'down'}"></i></button>
      <span class="folder-icon"><i class="fa-solid fa-folder${collapsed ? '' : '-open'}"></i></span>
      <span class="folder-name">${escapeHtml(folder.name)}</span>
      <span class="folder-count">${totalCount}</span>`;

    const folderMenuBtn = document.createElement('button');
    folderMenuBtn.className = 'folder-menu-btn';
    folderMenuBtn.title = 'More options';
    folderMenuBtn.innerHTML = '<i class="fa-solid fa-ellipsis-vertical"></i>';
    header.addEventListener('click', e => { _toggleFolder(`${folder.id}`); });
    folderMenuBtn.addEventListener('click', e => { e.stopPropagation(); _openFolderMenu(e, folder); });
    header.appendChild(folderMenuBtn);
    folderEl.appendChild(header);

    _attachFolderDragHandlers(header, folderEl, folder);

    if (!collapsed) {
      const body = document.createElement('div');
      body.className = 'folder-body';

      // Render child folders first
      _renderFolderSubtree(folder.id, depth + 1, body, childMap, sessionsByFolder, folderIds);

      if (folderSessions.length === 0 && !(childMap.get(folder.id) || []).length) {
        body.innerHTML += '<div class="folder-empty">Drop sessions here</div>';
      } else {
        for (const s of folderSessions) {
          const el = _makeSessionEl(s);
          _attachSessionDragHandlers(el, s);
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
    _sidebarDragType = 'session';
    _sidebarDragIds = isSelected && _sidebarSelected.size > 1
      ? [..._sidebarSelected]
      : [s.id];
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', JSON.stringify(_sidebarDragIds));
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => { el.classList.remove('dragging'); _removeDragIndicator(); });

  el.addEventListener('click', e => {
    if (e.ctrlKey || e.metaKey || _sidebarMultiselect) {
      e.stopPropagation();
      _toggleSidebarSelect(s.id);
      return;
    }
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
  const metaEl = document.createElement('div');
  metaEl.className = 'session-meta';
  metaEl.textContent = formatSessionMeta(s);
  info.appendChild(nameEl);
  info.appendChild(metaEl);

  el.appendChild(cb);
  el.appendChild(dot);
  el.appendChild(info);

  const menuBtn = document.createElement('button');
  menuBtn.className = 'session-menu-btn';
  menuBtn.title = 'More options';
  menuBtn.innerHTML = '<i class="fa-solid fa-ellipsis-vertical"></i>';
  menuBtn.addEventListener('click', e => { e.stopPropagation(); _openSessionMenu(e, s); });
  el.appendChild(menuBtn);

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

function _openSessionMenu(e, s) {
  _closeSessionMenu();

  const menu = document.createElement('div');
  menu.className = 'session-menu';
  menu.id = 'session-menu-popup';

  if (s.has_audio) {
    const rea = document.createElement('div');
    rea.className = 'session-menu-item';
    rea.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i>  Reanalyze';
    rea.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); reanalyzeSession(ev, s.id); });
    menu.appendChild(rea);
  }

  const ren = document.createElement('div');
  ren.className = 'session-menu-item';
  ren.innerHTML = '<i class="fa-solid fa-pen"></i>  Rename';
  ren.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); startEditTitle(ev, s.id, s.title); });
  menu.appendChild(ren);

  const del = document.createElement('div');
  del.className = 'session-menu-item session-menu-item-danger';
  del.innerHTML = '<i class="fa-solid fa-trash"></i>  Delete';
  del.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); deleteSession(ev, s.id); });
  menu.appendChild(del);

  document.body.appendChild(menu);

  // Position below the ⋮ button, clamp to viewport
  const rect = e.currentTarget.getBoundingClientRect();
  const menuH = 120; // rough estimate
  const top = rect.bottom + window.scrollY;
  let left = rect.left + window.scrollX;
  if (left + 160 > window.innerWidth) left = window.innerWidth - 164;
  menu.style.top  = top  + 'px';
  menu.style.left = left + 'px';

  setTimeout(() => document.addEventListener('click', _closeSessionMenu, { once: true }), 0);
}

function _closeSessionMenu() {
  const m = document.getElementById('session-menu-popup');
  if (m) m.remove();
}

// ── Folder context menu ───────────────────────────────────────────────────────

function _openFolderMenu(e, folder) {
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

  const del = document.createElement('div');
  del.className = 'session-menu-item session-menu-item-danger';
  del.innerHTML = '<i class="fa-solid fa-trash"></i>  Delete';
  del.addEventListener('click', ev => {
    ev.stopPropagation(); _closeFolderMenu();
    deleteFolder(ev, folder.id);
  });
  menu.appendChild(del);

  document.body.appendChild(menu);

  // Position below the ⋮ button, clamp to viewport
  const rect = e.currentTarget.getBoundingClientRect();
  let left = rect.left + window.scrollX;
  if (left + 160 > window.innerWidth) left = window.innerWidth - 164;
  menu.style.top  = (rect.bottom + window.scrollY) + 'px';
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

async function createFolder() {
  const name = prompt('Folder name:');
  if (!name?.trim()) return;
  await fetch('/api/folders', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name.trim() }),
  });
  refreshSidebar();
}

async function createSubfolder(parentId) {
  const name = prompt('Subfolder name:');
  if (!name?.trim()) return;
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
    if (!confirm(msg)) return;
    deleteContents = true;
  } else {
    if (!confirm(`Delete empty folder "${folderName}"?`)) return;
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
    fetch('/api/reorder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folders: payload }),
    }).then(() => { _sidebarSelected.clear(); refreshSidebar(); });
  } else {
    // Move session(s) into folder
    fetch('/api/sessions/bulk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'move', session_ids: ids, folder_id: folderId }),
    }).then(() => { _sidebarSelected.clear(); refreshSidebar(); });
  }
}

function _handleDropFolderToTopLevel() {
  const ids = _sidebarDragIds.length ? _sidebarDragIds : [];
  if (!ids.length || _sidebarDragType !== 'folder') return;
  // Move to top level at the end
  const topFolders = _sidebarFolders.filter(f => !f.parent_id);
  const maxOrder = topFolders.reduce((m, f) => Math.max(m, f.sort_order || 0), 0);
  const payload = ids.map((id, i) => ({ id, sort_order: maxOrder + 1 + i, parent_id: null }));
  fetch('/api/reorder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folders: payload }),
  }).then(() => refreshSidebar());
}

function _handleDrop(targetId, targetType, zone, parentContext) {
  // parentContext = folder_id for sessions, parent_id for folders
  if (_sidebarDragType === 'session' && targetType === 'session') {
    _reorderSessions(targetId, zone, parentContext);
  } else if (_sidebarDragType === 'folder' && targetType === 'folder') {
    _reorderFolders(targetId, zone);
  } else if (_sidebarDragType === 'session' && targetType === 'folder') {
    // Session dropped on edge of a folder — treat as drop into the folder
    _handleDropIntoFolder(targetId);
  } else if (_sidebarDragType === 'folder' && targetType === 'session') {
    // Folder dropped on a session edge — ignore (doesn't make sense)
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

  fetch('/api/reorder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessions: payload }),
  }).then(() => { _sidebarSelected.clear(); refreshSidebar(); });
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

  fetch('/api/reorder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folders: payload }),
  }).then(() => { _sidebarSelected.clear(); refreshSidebar(); });
}

// Legacy alias for any remaining references
function _dropIntoFolder(folderId) { _handleDropIntoFolder(folderId); }

// ── Bulk actions ──────────────────────────────────────────────────────────────

async function bulkDelete() {
  const ids = [..._sidebarSelected];
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} session${ids.length === 1 ? '' : 's'} and all their data?`)) return;
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
  await fetch('/api/sessions/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'retitle', session_ids: ids }),
  });
  if (btn) { btn.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i> Titles'; btn.disabled = false; }
  refreshSidebar();
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
  if (!s.ended_at) return `${time} · In progress`;
  // Use actual transcript duration (last segment end_time) when available,
  // falling back to wall-clock duration between start/end timestamps.
  let secs = s.last_segment_time;
  if (secs == null || secs <= 0) {
    const end = new Date(s.ended_at + 'Z');
    secs = (end - start) / 1000;
  }
  return `${time} · ${fmtDuration(secs)}`;
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

async function reanalyzeSession(e, sessionId) {
  e.stopPropagation();
  if (state.isRecording) { alert('Cannot reanalyze while recording.'); return; }
  if (state.isReanalyzing) { alert('Reanalysis already in progress.'); return; }
  if (!confirm('Reanalyze this session from its recorded audio?\n\nThis will clear the current transcript, summary, and chat, then retranscribe from scratch.')) return;

  // Load the session as active so incoming transcript SSE events land on screen
  if (sessionId !== state.sessionId) {
    const data = await fetch(`/api/sessions/${sessionId}`).then(r => r.json());
    if (data.error) { alert(data.error); return; }
    clearAll();
    state.sessionId     = sessionId;
    state.isViewingPast = false;
    document.getElementById('record-btn').disabled = true;
    if (data.speaker_profiles?.length) {
      data.speaker_profiles.forEach(p => applySpeakerProfileUpdate(p));
    }
  } else {
    // Already viewing — just clear the display
    clearAll();
    state.isViewingPast = false;
    document.getElementById('record-btn').disabled = true;
  }

  // Keep playback available during reanalysis — the WAV file still exists
  initPlayback(sessionId);

  const customPrompt = document.getElementById('summary-custom-prompt')?.value || '';
  const resp = await fetch(`/api/sessions/${sessionId}/reanalyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ custom_prompt: customPrompt }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    alert(err.error || 'Failed to start reanalysis');
  }
}

function newSession() {
  if (state.isRecording) return;
  state.sessionId    = null;
  state.isViewingPast = false;
  clearAll();
  history.pushState({}, '', '/session');
  _applyPromptText('');
  updateRecordBtn();
  refreshSidebar();
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

  src.addEventListener('transcript', e => {
    const d = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    if (d.seg_id) _lastLiveSegId = Math.max(_lastLiveSegId, d.seg_id);
    if (!state.isViewingPast) appendTranscript(d.text, d.source || 'loopback', d.start_time, d.end_time, d.seg_id);
  });

  src.addEventListener('transcript_update', e => {
    const d = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    if (!state.isViewingPast && d.seg_id) {
      const seg = document.querySelector(`.transcript-segment[data-seg-id="${d.seg_id}"]`);
      if (seg) {
        // Source changed (e.g. noise reclaimed as real speaker) — full re-render
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
          // Text/time update only — preserve the badge
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
  });

  src.addEventListener('summary_busy', e => {
    const d = JSON.parse(e.data);
    if (d.session_id && d.session_id !== state.sessionId) return;
    const badge = document.getElementById('summary-badge');
    if (d.busy) {
      badge.textContent = d.mode === 'generating' ? 'generating…' : 'updating…';
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  });

  src.addEventListener('summary_start', () => {
    state.summaryStreaming = true;
    state.summaryBuffer = '';
    const el = document.getElementById('summary');
    el.innerHTML = '';
    state.summaryCursor = el;
  });

  src.addEventListener('summary_chunk', e => {
    state.summaryBuffer += JSON.parse(e.data).text;
    if (state.summaryCursor) {
      state.summaryCursor.innerHTML = renderMd(state.summaryBuffer);
      if (_summaryAtBottom) state.summaryCursor.scrollTop = state.summaryCursor.scrollHeight;
    }
  });

  src.addEventListener('summary_done', () => {
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
  });

  src.addEventListener('chat_tool_event', e => {
    const d = JSON.parse(e.data);
    if (!state.chatCursor) return;
    const wrap = state.chatCursor.closest('.chat-msg');
    if (!wrap) return;
    if (d.type === 'tool_call') {
      state.chatToolCalls.push({ name: d.name, input: d.input, result: null });
      _renderToolWidget(wrap, state.chatToolCalls);
      _setAssistantProcessing(wrap, true, 'Using ' + _toolDisplayName(d.name) + '…');
    } else if (d.type === 'tool_result') {
      const last = state.chatToolCalls[state.chatToolCalls.length - 1];
      if (last) last.result = { success: d.success, summary: d.summary, image: d.image || null };
      _renderToolWidget(wrap, state.chatToolCalls);
    }
    scrollChatToBottom();
  });

  src.addEventListener('chat_chunk', e => {
    state.chatBuffer += JSON.parse(e.data).text;
    if (state.chatCursor) {
      const wrap = state.chatCursor.closest('.chat-msg');
      if (wrap) _setAssistantProcessing(wrap, false);
      state.chatCursor.innerHTML = renderMd(state.chatBuffer);
      state.chatCursor.classList.add('typing-cursor');
      scrollChatToBottom();
    }
  });

  src.addEventListener('chat_done', () => {
    if (state.chatCursor) {
      const wrap = state.chatCursor.closest('.chat-msg');
      if (wrap) _setAssistantProcessing(wrap, false);
      linkifyTimestamps(state.chatCursor);
      highlightCode('#chat-messages');
      _addCodeCopyButtons(state.chatCursor);
      state.chatCursor.classList.remove('typing-cursor');
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
    // Sync gain sliders if server reports different values (e.g. after reconnect)
    if (d.lb_gain  != null) _syncGainSlider('lb',  d.lb_gain);
    if (d.mic_gain != null) _syncGainSlider('mic', d.mic_gain);
    updateLevelMeters(vizLbTarget, vizMicTarget, vizHasMic);
  });

  src.addEventListener('audio_test_status', e => {
    const d = JSON.parse(e.data);
    state.isTesting = !!d.testing;
    updateTestBtn();
    syncBrowserMic();
    // Zero out levels when test ends (and we're not recording)
    if (!d.testing && !state.isRecording) {
      vizLbTarget  = 0;
      vizMicTarget = 0;
      vizLbSpec    = [];
      vizMicSpec   = [];
      updateLevelMeters(0, 0, false);
    }
  });

  src.addEventListener('session_title', e => {
    const d = JSON.parse(e.data);
    // Update in-memory cache so re-render is instant, then refresh once
    const entry = _sidebarAllSessions.find(s => s.id === d.session_id);
    if (entry) { entry.title = d.title; _renderSidebar(); }
    else refreshSidebar();
    if (d.session_id === state.sessionId) updateTopbarSessionTitle();
  });

  src.addEventListener('speaker_label', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) applySpeakerProfileUpdate(d);
  });

  src.addEventListener('fingerprint_match', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) _fpEnqueueToast(d);
  });

  src.addEventListener('fingerprint_auto_applied', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) {
      console.info(`[fingerprint] Auto-applied "${d.name}" → ${d.speaker_key} (${d.similarity})`);
      _fpFlashAutoApply(d.speaker_key, d.name);
      // Remove from notification queue if it was pending
      _fpRemoveFromQueue(d.speaker_key);
    }
  });

  src.addEventListener('speaker_linked', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) {
      _sessionLinks[d.speaker_key] = { global_id: d.global_id, name: d.name };
      _updateLinkedBadges();
      // Clean up notification queue — this speaker is now identified
      _fpRemoveFromQueue(d.speaker_key);
      _fpUpdateInlineIcons();
    }
  });

  src.addEventListener('transcript_reset', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    _clearSegmentRegistry();
    // Clear notification queue on transcript reset (reanalysis)
    _fpNotifQueue = [];
    _fpToastActive = null;
    if (_fpToastTimer) { clearTimeout(_fpToastTimer); _fpToastTimer = null; }
    _fpUpdateBell();
    _fpRenderNotifPanel();
    document.getElementById('transcript').innerHTML =
      '<p class="empty-hint">Reanalyzing audio…</p>';
    document.getElementById('summary').innerHTML =
      '<p class="empty-hint">Summary will regenerate after reanalysis completes.</p>';
    document.getElementById('chat-messages').innerHTML =
      '<p class="empty-hint">Ask questions about the meeting here.</p>';
    // Keep playback active — the WAV file still exists during reanalysis
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
    refreshSidebar();
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
    _syncRecordBtnDisabled();
    refreshSidebar();
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
    alert('Reanalysis failed: ' + (d.error || 'unknown error'));
    _syncRecordBtnDisabled();
    refreshSidebar();
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

/* ── Branding ────────────────────────────────────────────────────────────── */
function _updateBrandIcons(recording) {
  const src = recording
    ? '/static/images/logo_recording.png'
    : '/static/images/logo.png';
  const icon = document.getElementById('brand-icon');
  if (icon) icon.src = src;
  const favicon = document.getElementById('favicon');
  if (favicon) favicon.href = src;
}

/* ── Status ──────────────────────────────────────────────────────────────── */
function _syncRecordBtnDisabled() {
  const btn = document.getElementById('record-btn');
  if (!btn) return;
  btn.disabled = !state.isRecording && (state.isReanalyzing || !state.recordingReady);
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

  if (d.recording !== undefined) {
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
      // Update URL to reflect the active session
      history.replaceState({}, '', '/session?id=' + d.session_id);
      state.sessionId     = d.session_id;
      state.isViewingPast = false;
      dot.className       = 'status-dot recording';
      text.textContent    = 'Recording…';
      _loadPaneVisible(d.session_id);
      destroyPlayback();
      if (!_durationInterval) {
        startDurationCounter();
        // Push stored gain values now — AudioCapture is guaranteed to exist
        initGainSliders();
      }
      _updateBrandIcons(true);
      if (d.screen_recording) { _updateScreenRecordingStatus(true); _showScreenPreviewToggle(true); }
      if (_pendingSpeakerProfiles.length) _flushPendingSpeakers(d.session_id);
      refreshSidebar();
    } else if (!d.recording) {
      stopDurationCounter();
      _updateBrandIcons(false);
      _updateScreenRecordingStatus(false);
      _stopScreenPreview();
      // Transition to "viewing past" so Resume Session button appears
      if (state.sessionId) state.isViewingPast = true;
      updateRecordBtn();
      refreshSidebar();
      // The WAV is finalized before this event fires, so playback is available
      // immediately - no need to reload the page or click the session.
      if (state.isViewingPast && state.sessionId) {
        initPlayback(state.sessionId);
        // Check if a screen recording was saved for this session
        fetch(`/api/sessions/${state.sessionId}`).then(r => r.json()).then(s => {
          if (s.has_video) initVideo(state.sessionId, s.video_offset);
        }).catch(() => {});
      }
    }
  }

  if (!state.isRecording) {
    if (state.isReanalyzing) {
      dot.className = 'status-dot recording';
      text.textContent = 'Reanalyzing…';
    } else if (!state.recordingReady) {
      dot.className = 'status-dot loading';
      text.textContent = state.recordingReadyReason || 'Loading transcription model…';
    } else {
      dot.className = 'status-dot ready';
      text.textContent = state.modelInfo || 'Ready';
    }
  }

  _syncRecordBtnDisabled();
}

function updateTopbarSessionTitle() {
  const el = document.getElementById('topbar-session-title');
  if (!el) return;
  if (!state.sessionId) {
    el.classList.add('hidden');
    el.textContent = '';
    return;
  }
  const entry = _sidebarAllSessions.find(s => s.id === state.sessionId);
  const title = entry?.title || '';
  if (title) {
    el.textContent = title;
    el.classList.remove('hidden');
  } else {
    el.classList.add('hidden');
    el.textContent = '';
  }
}

function updateRecordBtn() {
  const btn = document.getElementById('record-btn');
  // Clear any inline "Stopping Recording…" overrides
  btn.style.background = '';
  btn.style.color = '';
  btn.disabled = false;
  updateTopbarSessionTitle();
  if (state.isRecording) {
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-stop"></i></span> Stop Recording';
    btn.classList.add('recording');
    btn.classList.remove('resuming');
  } else if (state.isViewingPast) {
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-play"></i></span> Resume Session';
    btn.classList.remove('recording');
    btn.classList.add('resuming');
  } else {
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-play"></i></span> Start Recording';
    btn.classList.remove('recording');
    btn.classList.remove('resuming');
  }
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
  syncBrowserMic();
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
async function toggleRecording() {
  if (state.isRecording) {
    // Immediate visual feedback while the server tears down streams
    const btn = document.getElementById('record-btn');
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-spinner fa-spin"></i></span> Stopping Recording\u2026';
    btn.style.background = 'var(--yellow)';
    btn.style.color = '#0d1117';
    btn.disabled = true;
    await fetch('/api/recording/stop', { method: 'POST' });
  } else {
    // Read selected device indices from the dropdowns
    const lbVal  = document.getElementById('viz-loopback-sel')?.value;
    const micVal = document.getElementById('viz-mic-sel')?.value;
    const body = {};
    if (lbVal  !== '' && lbVal  !== null && lbVal  !== undefined) body.loopback_device = parseInt(lbVal, 10);
    if (micVal !== '' && micVal !== null && micVal !== undefined) body.mic_device      = parseInt(micVal, 10);

    if (state.isViewingPast) {
      // Resume the currently-viewed session instead of starting a new one
      body.resume_session_id = state.sessionId;
    }

    const resp = await fetch('/api/recording/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(err.error || 'Failed to start recording');
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
let _segmentTimes     = [];     // {start, end, el} for timed segs — sorted by start
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

function openSpeakerManager() {
  document.getElementById('speaker-manager-overlay').classList.remove('hidden');
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
}

function closeSpeakerManager() {
  document.getElementById('speaker-manager-overlay').classList.add('hidden');
}

function closeSpeakerManagerOnOverlay(event) {
  if (event.target.id === 'speaker-manager-overlay') closeSpeakerManager();
}

// ── Fingerprint match toast ───────────────────────────────────────────────────

/* ── Fingerprint notification queue ─────────────────────────────────────────
 * Replaces the old one-shot toast with a persistent notification queue.
 * Suggestions accumulate in _fpNotifQueue and are shown in both:
 *   1. The bell panel (always available for review)
 *   2. A brief toast (fires once for attention, then auto-hides)
 * ────────────────────────────────────────────────────────────────────────── */
let _fpNotifQueue = [];          // persistent queue: [{session_id, speaker_key, current_name, matches}, ...]
let _fpToastActive = null;
let _fpToastTimer  = null;

function _fpEnqueueToast(data) {
  const top = data.matches && data.matches[0];
  if (top && data.current_name && top.name === data.current_name) return;
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
}

function _fpGetSuggestion(speakerKey) {
  return _fpNotifQueue.find(d => d.speaker_key === speakerKey) || null;
}

// ── Bell badge ────────────────────────────────────────────────────────────
function _fpUpdateBell() {
  const btn = document.getElementById('fp-bell-btn');
  const badge = document.getElementById('fp-bell-badge');
  if (!btn || !badge) return;
  const count = _fpNotifQueue.length;
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
  // No panels visible — col-header is the bottom element
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
  list.innerHTML = '';

  for (const item of _fpNotifQueue) {
    const top = item.matches[0];
    if (!top) continue;

    const card = document.createElement('div');
    card.className = 'fp-notif-card';
    card.dataset.speakerKey = item.speaker_key;

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

    const skipBtn = document.createElement('button');
    skipBtn.className = 'fp-notif-btn fp-notif-skip';
    skipBtn.textContent = 'Skip';
    skipBtn.addEventListener('click', () => _fpNotifDismiss(item));

    actions.appendChild(applyBtn);

    // "Other" dropdown if multiple matches
    if (item.matches.length > 1) {
      const otherWrap = document.createElement('div');
      otherWrap.className = 'fp-notif-other-wrap';
      const otherBtn = document.createElement('button');
      otherBtn.className = 'fp-notif-btn';
      otherBtn.innerHTML = '<i class="fa-solid fa-chevron-down" style="font-size:9px"></i>';
      otherBtn.title = 'Other matches';
      const otherList = document.createElement('div');
      otherList.className = 'fp-notif-other-list hidden';
      item.matches.slice(1).forEach(m => {
        const opt = document.createElement('button');
        opt.className = 'fp-notif-other-opt';
        opt.textContent = `${m.name} (${Math.round(m.similarity * 100)}%)`;
        opt.addEventListener('click', () => _fpNotifConfirm(item, m.global_id));
        otherList.appendChild(opt);
      });
      otherBtn.addEventListener('click', () => otherList.classList.toggle('hidden'));
      otherWrap.appendChild(otherBtn);
      otherWrap.appendChild(otherList);
      actions.appendChild(otherWrap);
    }

    actions.appendChild(skipBtn);

    card.appendChild(speaker);
    card.appendChild(arrow);
    card.appendChild(match);
    card.appendChild(sim);
    card.appendChild(actions);
    list.appendChild(card);
  }
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
  otherList.innerHTML = '';
  otherList.classList.add('hidden');
  const others = _fpToastActive.matches.slice(1);
  if (others.length) {
    document.getElementById('fp-toast-other').style.display = '';
    others.forEach(m => {
      const btn = document.createElement('button');
      btn.className = 'fp-toast-opt';
      btn.textContent = `${m.name} (${Math.round(m.similarity * 100)}%)`;
      btn.addEventListener('mousedown', e => {
        e.preventDefault();
        _fpNotifConfirm(_fpToastActive, m.global_id);
      });
      otherList.appendChild(btn);
    });
  } else {
    document.getElementById('fp-toast-other').style.display = 'none';
  }

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
  // Don't dismiss from queue — it stays in the bell panel for later review
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
let _fpSelectedId   = null; // currently selected global_id
let _fpDetailColor  = '';
let _fpSelectMode   = false;
let _fpSelected     = new Set();  // selected global_ids for bulk ops
let _fpSearchTerm   = '';

async function openFingerprintPanel() {
  document.getElementById('fingerprint-panel-overlay').classList.remove('hidden');
  // Reset search and select state
  _fpSearchTerm = '';
  _fpSelectMode = false;
  _fpSelected.clear();
  const searchInput = document.getElementById('fp-search-input');
  if (searchInput) searchInput.value = '';
  const selectToggle = document.getElementById('fp-select-toggle');
  if (selectToggle) selectToggle.classList.remove('active');
  document.getElementById('fp-select-bar')?.classList.add('hidden');
  await _fpLoadProfiles();
}

function closeFingerprintPanel() {
  document.getElementById('fingerprint-panel-overlay').classList.add('hidden');
}

function closeFingerprintPanelOnOverlay(event) {
  if (event.target.id === 'fingerprint-panel-overlay') closeFingerprintPanel();
}

async function _fpLoadProfiles() {
  try {
    const resp = await fetch('/api/fingerprint/speakers');
    _fpProfiles = await resp.json();
  } catch (e) {
    _fpProfiles = [];
  }
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
    scrollEl.innerHTML = `<div class="fp-panel-empty">${_fpProfiles.length ? 'No matching profiles.' : 'No voice profiles yet. Use the "+ New Profile" button to create one.'}</div>`;
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
    btn.addEventListener('click', () => { _fpDetailColor = color; _fpSelectProfile(globalId); });
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
          <span class="fp-session-meta">${date}${keys ? ' · ' + keys : ''} · ${s.seg_count} segs</span>
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
  const names = ids.map(id => _fpProfiles.find(p => p.id === id)?.name || id).join(', ');
  if (!confirm(`Delete ${ids.length} profile${ids.length > 1 ? 's' : ''}?\n\n${names}\n\nThis cannot be undone.`)) return;
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
  if (ids.length < 2) { alert('Select at least 2 profiles to merge.'); return; }
  const names = ids.map(id => _fpProfiles.find(p => p.id === id)?.name || id);
  const keepName = names[0];
  if (!confirm(`Merge ${ids.length} profiles into "${keepName}"?\n\n${names.join(', ')}\n\nAll voice samples will be combined. This cannot be undone.`)) return;
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
  if (!confirm(`Optimize ${ids.length} profile${ids.length > 1 ? 's' : ''}? This prunes redundant voice samples.`)) return;
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
  if (!name) { alert('Name is required.'); return; }
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
  if (!confirm(`Merge "${document.getElementById('fp-detail-name').value}" into the selected profile? This cannot be undone.`)) return;
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
  if (!confirm(`Delete "${name}" and all its voice samples? This cannot be undone.`)) return;
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
  if (!name) { alert('Enter a name.'); return; }
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
}

function _updateTranscriptSelectionUI() {
  _segmentRegistry.forEach(seg => {
    seg.classList.toggle('transcript-seg-selected', _transcriptSelectedSegs.has(seg));
  });
  const bar = document.getElementById('transcript-selection-bar');
  if (!bar) return;
  const count = _transcriptSelectedSegs.size;
  if (count > 0) {
    bar.classList.remove('hidden');
    const countEl = document.getElementById('tsb-count');
    if (countEl) countEl.textContent = `${count} segment${count === 1 ? '' : 's'} selected`;
    const input = document.getElementById('tsb-input');
    if (input) input.value = '';
  } else {
    bar.classList.add('hidden');
    document.getElementById('tsb-autocomplete')?.classList.add('hidden');
  }
  _syncPanelBottomRadius();
}

function _tsbGetSpeakerNames() {
  const names = [];
  const seen = new Set();
  _getSortedSpeakerProfiles().forEach(p => {
    const name = (p.name || '').trim();
    if (!name || seen.has(name)) return;
    if (!p.custom && _isDefaultName(name)) return;
    seen.add(name);
    const color = p.color || _speakerColors[p.speaker_key] || speakerColor(p.speaker_key);
    names.push({ name, color });
  });
  names.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  return names;
}

function _tsbFilterAutocomplete() {
  const input = document.getElementById('tsb-input');
  const list = document.getElementById('tsb-autocomplete');
  if (!input || !list) return;

  const query = input.value.trim().toLowerCase();
  const names = _tsbGetSpeakerNames();
  const filtered = query
    ? names.filter(n => n.name.toLowerCase().includes(query))
    : names;

  list.innerHTML = '';
  if (filtered.length === 0) {
    list.classList.add('hidden');
    return;
  }

  filtered.forEach(entry => {
    const opt = document.createElement('button');
    opt.className = 'tsb-ac-opt';
    opt.innerHTML = `<span class="tsb-ac-dot" style="background:${entry.color}"></span>${escapeHtml(entry.name)}`;
    opt.style.color = entry.color;
    opt.addEventListener('mousedown', e => {
      e.preventDefault();
      input.value = entry.name;
      list.classList.add('hidden');
    });
    list.appendChild(opt);
  });
  list.classList.remove('hidden');
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

async function applyTranscriptBulkReassign() {
  const input = document.getElementById('tsb-input');
  const name = (input?.value || '').trim();
  if (!name) return;
  document.getElementById('tsb-autocomplete')?.classList.add('hidden');

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

function renderSpeakerManager() {
  const listEl = document.getElementById('speaker-manager-list');
  const colorGridEl = document.getElementById('speaker-color-grid');
  const inputEl = document.getElementById('speaker-editor-name');
  const hintEl = document.getElementById('speaker-editor-hint');
  const subtitleEl = document.getElementById('speaker-manager-subtitle');
  const datalistEl = document.getElementById('speaker-name-options');
  if (!listEl || !colorGridEl || !inputEl || !hintEl || !subtitleEl || !datalistEl) return;

  const profiles = _getSortedSpeakerProfiles().filter(p => p.speaker_key !== _NOISE_LABEL);
  const groups = _groupProfilesByName(profiles);
  const selectedGroupCount = groups.filter(g => g.speakerKeys.some(k => _selectedSpeakerKeys.includes(k))).length;

  inputEl.value = _speakerDraftName;
  inputEl.oninput = e => { _speakerDraftName = e.target.value; };

  datalistEl.innerHTML = '';
  _speakerOptionNames().forEach(name => {
    const opt = document.createElement('option');
    opt.value = name;
    datalistEl.appendChild(opt);
  });

  colorGridEl.innerHTML = '';
  _SPEAKER_PALETTE.forEach(color => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'speaker-color-btn' + (_speakerDraftColor === color ? ' active' : '');
    btn.title = color;
    btn.style.backgroundColor = color;
    btn.addEventListener('click', async () => {
      _speakerDraftColor = color;
      // Auto-apply color immediately if speakers are selected
      if (_selectedSpeakerKeys.length && state.sessionId) {
        const resp = await fetch(`/api/sessions/${state.sessionId}/speakers`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ speaker_keys: _selectedSpeakerKeys, color }),
        });
        const data = await resp.json();
        if (resp.ok) (data.speakers || []).forEach(applySpeakerProfileUpdate);
      }
      renderSpeakerManager();
    });
    colorGridEl.appendChild(btn);
  });

  if (selectedGroupCount === 0) {
    subtitleEl.textContent = 'Manage speaker names, colors, and bulk assignments.';
    hintEl.textContent = 'Click a speaker row to edit it. Use Ctrl/Cmd-click or Shift-click for multi-select.';
  } else if (selectedGroupCount === 1) {
    subtitleEl.textContent = 'Editing 1 speaker.';
    hintEl.textContent = 'Change the name or color, or add a new participant for later assignment.';
  } else {
    subtitleEl.textContent = `Editing ${selectedGroupCount} speakers.`;
    hintEl.textContent = 'Bulk updates apply to every selected speaker row.';
  }

  listEl.innerHTML = '';
  if (!groups.length) {
    listEl.innerHTML = '<div class="speaker-manager-empty">Speaker rows will appear here once diarized speakers show up in the transcript.</div>';
    return;
  }

  groups.forEach(group => {
    const row = document.createElement('button');
    row.type = 'button';
    const isSelected = group.speakerKeys.some(k => _selectedSpeakerKeys.includes(k));
    row.className = 'speaker-row' + (isSelected ? ' selected' : '');
    row.dataset.speakerKeys = JSON.stringify(group.speakerKeys);
    row.addEventListener('click', e => {
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

    const count = group.speakerKeys.reduce((sum, k) => sum + _speakerBadgeCount(k), 0);
    const meta = document.createElement('div');
    meta.className = 'speaker-row-meta';
    if (group.custom && !count) {
      meta.textContent = 'Saved participant';
    } else if (group.speakerKeys.length === 1) {
      const k = group.speakerKeys[0];
      meta.textContent = `${k}${count ? ` · ${count} segment${count === 1 ? '' : 's'}` : ''}`;
    } else {
      // Multiple diarizer fragments — show key list as muted subtext
      const displayed = group.speakerKeys.slice(0, 3).join(', ');
      const extra = group.speakerKeys.length > 3 ? ` +${group.speakerKeys.length - 3}` : '';
      meta.textContent = `${displayed}${extra}${count ? ` · ${count} segments` : ''}`;
      meta.title = group.speakerKeys.join(', ');
    }

    const countEl = document.createElement('div');
    countEl.className = 'speaker-row-count';
    countEl.textContent = count ? `${count}` : 'saved';

    main.appendChild(nameEl);
    main.appendChild(meta);
    row.appendChild(swatch);
    row.appendChild(main);
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
  const name = (document.getElementById('speaker-editor-name')?.value || '').trim();
  if (!name) {
    alert('Enter a speaker name first.');
    return;
  }

  if (!state.sessionId) {
    // No session yet – store locally and flush when recording starts
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
    alert(data.error || 'Failed to add speaker');
    return;
  }

  applySpeakerProfileUpdate(data.speaker);
  _selectedSpeakerKeys = [data.speaker.speaker_key];
  _speakerSelectionAnchor = data.speaker.speaker_key;
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
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
    alert('Select at least one speaker row first.');
    return;
  }

  const name = (document.getElementById('speaker-editor-name')?.value || '').trim();
  const body = { speaker_keys: _selectedSpeakerKeys };
  if (name) body.name = name;
  if (_speakerDraftColor) body.color = _speakerDraftColor;
  if (!body.name && !body.color) {
    alert('Enter a name or choose a color first.');
    return;
  }

  const resp = await fetch(`/api/sessions/${state.sessionId}/speakers`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) {
    alert(data.error || 'Failed to update speakers');
    return;
  }

  (data.speakers || []).forEach(applySpeakerProfileUpdate);
  _syncSpeakerDraftFromSelection();
  renderSpeakerManager();
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
    // Noise/filler segment — muted styling, click to reassign
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
    if (_showOriginalKeys && !labelOverride) {
      _setBadgeLabel(badge, source);
    } else {
      badge.textContent = displayName;
    }
    badge.style.backgroundColor = color + '26'; // ~15% opacity tint
    badge.style.color = color;
    badge.style.borderColor = color + '60';

    // Inline identify icon for unlinked speakers
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

    badge.addEventListener('click', e => {
      if (e.ctrlKey || e.metaKey || e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        _toggleTranscriptSegSelection(seg, { range: e.shiftKey });
        return;
      }
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

  // During bulk load, skip expensive per-segment work — it runs once after the load.
  if (_bulkLoading) return;

  // Extend time range slider if navigator is open (before filtering, so pinned max stays Infinity)
  _tnExtendTimeRange();
  _applyFilterToSeg(seg);
  // Highlight search matches in new segment if search is active
  if (_transcriptFilter.search.trim() && seg.style.display !== 'none') {
    _tnHighlightInSeg(seg);
  }
  // Only check this new segment's badge — no need to re-scan all segments.
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

function editSpeakerLabel(badge, speakerKey) {
  // Remove any existing picker first
  document.querySelector('.speaker-picker')?.remove();

  const currentName = badge.textContent;
  const color = _speakerColors[speakerKey] || speakerColor(speakerKey) || '#58a6ff';
  const segId = badge.dataset.segId;  // may be undefined for live segments without DB id

  // Determine edit mode:
  // - "oneoff" only if the badge is already a per-segment override
  // - "global" for everything else — first-touch edits always rename all segments
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

  // Collect unique display names already assigned (excluding this key's current name)
  const existingNames = _speakerOptionNames(currentName, speakerKey);
  const meetingNameSet = new Set(existingNames.map(n => n.toLowerCase()));

  // Option buttons for existing meeting labels (section header)
  if (existingNames.length > 0) {
    const secLabel = document.createElement('div');
    secLabel.className = 'speaker-picker-section';
    secLabel.textContent = 'Meeting speakers';
    optionsWrap.appendChild(secLabel);
  }
  existingNames.forEach(name => {
    const optKey = _speakerNameKey(name, speakerKey);
    const optColor = (optKey && (_speakerColors[optKey] || speakerColor(optKey))) || color;
    const opt = document.createElement('button');
    opt.className = 'speaker-picker-opt';
    opt.dataset.optName = name.toLowerCase();
    opt.textContent = name;
    opt.style.borderColor = optColor + '60';
    opt.style.color = optColor;
    opt.addEventListener('mousedown', e => {
      e.preventDefault();
      commit(name);
    });
    optionsWrap.appendChild(opt);
  });

  // Voice Library section — populated asynchronously
  const vlSection = document.createElement('div');
  vlSection.className = 'speaker-picker-section speaker-picker-vl-section';
  vlSection.style.display = 'none';
  vlSection.textContent = 'Voice Library';
  optionsWrap.appendChild(vlSection);

  fetch('/api/fingerprint/speakers').then(r => r.json()).then(speakers => {
    if (!speakers || !speakers.length) return;
    const vlOpts = [];
    speakers.forEach(sp => {
      const name = (sp.name || '').trim();
      if (!name || meetingNameSet.has(name.toLowerCase())) return;
      if (name.toLowerCase() === currentName.toLowerCase()) return;
      const opt = document.createElement('button');
      opt.className = 'speaker-picker-opt speaker-picker-vl-opt';
      opt.dataset.optName = name.toLowerCase();
      opt.textContent = name;
      const vlColor = sp.color || 'var(--fg-muted)';
      opt.style.borderColor = vlColor + '60';
      opt.style.color = vlColor;
      opt.addEventListener('mousedown', e => {
        e.preventDefault();
        commit(name);
      });
      vlOpts.push(opt);
    });
    if (vlOpts.length > 0) {
      vlSection.style.display = '';
      vlOpts.forEach(o => optionsWrap.appendChild(o));
      // Apply current filter if user already typed something
      const typed = input.value.trim().toLowerCase();
      if (typed && typed !== currentName.toLowerCase()) _filterPickerOpts(typed);
    }
  }).catch(() => {});

  // Filter function for options
  function _filterPickerOpts(query) {
    let meetingVisible = 0, vlVisible = 0;
    optionsWrap.querySelectorAll('.speaker-picker-opt').forEach(opt => {
      const name = opt.dataset.optName || '';
      const show = !query || name.includes(query);
      opt.style.display = show ? '' : 'none';
      if (show) {
        if (opt.classList.contains('speaker-picker-vl-opt')) vlVisible++;
        else meetingVisible++;
      }
    });
    // Hide section headers when no items visible
    optionsWrap.querySelectorAll('.speaker-picker-section').forEach(sec => {
      if (sec.classList.contains('speaker-picker-vl-section')) {
        sec.style.display = vlVisible > 0 ? '' : 'none';
      } else {
        sec.style.display = meetingVisible > 0 ? '' : 'none';
      }
    });
  }

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

  // "Mark as Noise" button — suppresses segment and hides it with noise pill
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

  // Append first so we can measure the picker's rendered height,
  // then position above or below the badge depending on available space.
  _pickerOpen = true;
  document.body.appendChild(picker);
  const rect = badge.getBoundingClientRect();
  const pickerH = picker.offsetHeight;
  const pickerW = picker.offsetWidth;
  const spaceBelow = window.innerHeight - rect.bottom - 8;
  const spaceAbove = rect.top - 8;
  const top = (spaceBelow >= pickerH || spaceBelow >= spaceAbove)
    ? rect.bottom + 2
    : rect.top - pickerH - 2;
  const left = Math.min(rect.left, window.innerWidth - pickerW - 8);
  picker.style.top  = top + 'px';
  picker.style.left = left + 'px';
  input.focus();
  input.select();

  // Live filter + merge hint on input
  input.addEventListener('input', () => {
    const typed = input.value.trim().toLowerCase();
    // Filter option buttons
    _filterPickerOpts(typed);

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
    // Plain text only — exclude the badge node
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
  _visibleRangesCache = null;  // filter changed — invalidate cached ranges
  _tnHighlightMatches();
  _refreshMinimap(true);
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

  const allKeys = new Set();
  groups.forEach(g => g.speakerKeys.forEach(k => allKeys.add(k)));

  // Separate noise group from regular speakers (skip in original-key mode — show all individually)
  const noiseGroups = [];
  const speakerGroups = [];
  groups.forEach(g => {
    if (!_showOriginalKeys && (g.speakerKeys.includes(_NOISE_LABEL) || g.speakerKeys.some(k => _manualNoiseKeys.has(k))))
      noiseGroups.push(g);
    else speakerGroups.push(g);
  });

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
      ? `${g.name} → ${g.displayName} — ${count} segment${count !== 1 ? 's' : ''}\nRight-click: jump to next`
      : `${g.name} — ${count} segment${count !== 1 ? 's' : ''}\nRight-click: jump to next`;

    pill.addEventListener('click', () => {
      _tnToggleSpeakerPill(g.speakerKeys, allKeys);
    });

    pill.addEventListener('contextmenu', e => {
      e.preventDefault();
      _tnJumpToNextSpeaker(g.speakerKeys, 1);
    });

    container.appendChild(pill);
  });

  // Single merged noise pill — all noise groups combined
  const totalNoiseCount = noiseGroups.reduce(
    (sum, g) => sum + g.speakerKeys.reduce((s2, k) => s2 + _speakerBadgeCount(k), 0), 0);
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
      const noiseKeys = noiseGroups.flatMap(g => g.speakerKeys);
      _tnJumpToNextSpeaker(noiseKeys, 1);
    });
    container.appendChild(pill);
  }
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

  // Rebuild "to" dropdown — includes all names, plus [Noise] option
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
  if (!confirm(`Reassign ${targets.length} segment${targets.length !== 1 ? 's' : ''} from "${fromName}" to ${toLabel}?`)) return;

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
    _tnUpdateRangeFill();
    _tnUpdateTimeLabels();
    applyTranscriptFilter();
    _updateFilterBtnState();
  });
  newMax.addEventListener('input', () => {
    if (parseFloat(newMax.value) < parseFloat(newMin.value)) newMax.value = newMin.value;
    const maxVal = parseFloat(newMax.max);
    const atEnd = parseFloat(newMax.value) >= maxVal - 0.5;
    _tnRangeMaxPinned = atEnd;
    _transcriptFilter.timeMax = atEnd ? Infinity : parseFloat(newMax.value);
    _tnUpdateRangeFill();
    _tnUpdateTimeLabels();
    applyTranscriptFilter();
    _updateFilterBtnState();
  });
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

  groups.forEach(g => {
    const isNoise = g.speakerKeys.includes(_NOISE_LABEL) || g.speakerKeys.some(k => _manualNoiseKeys.has(k));
    const keysSet = new Set(g.speakerKeys);
    let segCount = 0, speakTime = 0, words = 0;
    const segments = [];
    allSegs.forEach(seg => {
      if (keysSet.has(seg.dataset.transcriptSource)) {
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
      }
    });
    if (segCount === 0) return;
    if (isNoise) {
      noiseData.segCount += segCount;
      noiseData.speakTime += speakTime;
      noiseData.words += words;
      noiseData.segments.push(...segments);
    } else {
      const color = g.color || speakerColor(g.speakerKeys[0]);
      speakerData.push({ name: g.name, color, segCount, speakTime, words, segments });
      totalSegCount += segCount;
      totalSpeakTime += speakTime;
      totalWords += words;
    }
  });

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

function clearTranscript() {
  if (!confirm('Clear the transcript? The transcript will need to be reanalyzed for speaker labeling.')) return;
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

function toggleSummaryPrompt() {
  const area = document.getElementById('summary-prompt-area');
  const btn  = document.getElementById('summary-prompt-toggle');
  const hidden = area.classList.toggle('hidden');
  btn.classList.toggle('active', !hidden);
  localStorage.setItem('summary-prompt-open', hidden ? '' : '1');
  if (!hidden) document.getElementById('summary-custom-prompt').focus();
  _syncSummaryBottomRadius();
}

let _promptSaveTimer = null;
function saveSummaryPrompt() {
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
  }, 600);
}

function _applyPromptText(text) {
  const ta = document.getElementById('summary-custom-prompt');
  ta.value = text || '';
  const show = !!ta.value.trim() || localStorage.getItem('summary-prompt-open') === '1';
  document.getElementById('summary-prompt-area').classList.toggle('hidden', !show);
  document.getElementById('summary-prompt-toggle').classList.toggle('active', show);
  _syncSummaryBottomRadius();
}

async function loadSummaryPrompt() {
  const key = 'summary-prompt:' + (state.sessionId || 'new');
  const stored = localStorage.getItem(key);
  if (stored !== null) {
    _applyPromptText(stored);
    // Sync to backend so active session picks it up
    await fetch('/api/custom-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ custom_prompt: stored }),
    }).catch(() => {});
    return;
  }
  try {
    const r = await fetch('/api/custom-prompt');
    const data = await r.json();
    _applyPromptText(data.custom_prompt || '');
  } catch (_) {}
}

/* ── Playback ────────────────────────────────────────────────────────────── */
const _playbackAudio = document.getElementById('playback-audio');
let _playbackActive = false;

function fmtTime(s) {
  if (!isFinite(s)) return '0:00';
  return fmtDuration(s);
}

function initPlayback(sessionId) {
  _playbackAudio.src = `/api/sessions/${sessionId}/audio`;
  _playbackAudio.load();
  _playbackActive = true;
  document.getElementById('playback-bar').classList.remove('hidden');
  _syncPanelBottomRadius();

  // Restore saved playback speed
  const savedSpeed = _prefs.playback_speed || '1';
  const speedSel = document.getElementById('playback-speed');
  if (speedSel) speedSel.value = savedSpeed;
  _playbackAudio.playbackRate = parseFloat(savedSpeed);

  _playbackAudio.onloadedmetadata = () => {
    document.getElementById('playback-duration').textContent = fmtTime(_playbackAudio.duration);
    document.getElementById('playback-seek').max = _playbackAudio.duration || 100;
  };

  _playbackAudio.ontimeupdate = () => {
    const t = _playbackAudio.currentTime;
    // Skip filtered-out segments during playback
    if (!_playbackAudio.paused && _transcriptFilterActive()) {
      _skipFilteredAudio(t);
    }
    document.getElementById('playback-time').textContent = fmtTime(t);
    document.getElementById('playback-seek').value = _playbackAudio.currentTime;
    highlightPlayingSegment(_playbackAudio.currentTime);
    _updateMinimapPlayhead(t);
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

  // Current time is in a hidden gap — find the next visible range
  for (const r of ranges) {
    if (r.start > t) {
      _lastSkipTime = r.start;
      _playbackAudio.currentTime = r.start;
      return;
    }
  }

  // Past all visible segments — let playback end naturally
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
  // Binary search on _segmentTimes (sorted by start) — O(log n) vs O(n) querySelectorAll.
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

/* ── Video viewer ────────────────────────────────────────────────────────── */
let _videoAvailable = false;
let _videoVisible   = false;
let _videoOffset    = 0; // audio seconds where the video file starts (>0 on resumed sessions)
const _playbackVideo = document.getElementById('playback-video');

function initVideo(sessionId, offset) {
  _videoOffset = offset || 0;
  const video = _playbackVideo;
  video.src = `/api/sessions/${sessionId}/video`;
  video.load();
  _videoAvailable = true;

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
  _playbackVideo.pause();
  _playbackVideo.removeAttribute('src');
  _playbackVideo.load();
  _videoAvailable = false;
  _videoVisible = false;
  _videoOffset = 0;
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
    if (!_playbackAudio.paused) _playbackVideo.play().catch(() => {});
  } else {
    _playbackVideo.pause();
  }
}

function _audioToVideoTime(audioTime) {
  return Math.max(0, audioTime - _videoOffset);
}

// Video seek — cancels any in-flight seek before issuing a new one
let _videoScrubbing = false;    // true while the user is dragging the seek bar
let _videoSeekDebounce = 0;     // timeout id for debounced seek during scrub

function _cancelVideoSeek() {
  clearTimeout(_videoSeekDebounce);
  _videoSeekDebounce = 0;
  // Abort any in-flight seek by forcing the video to stop loading the old frame
  if (_playbackVideo.seeking) {
    // Re-assign the same src won't help, but we can let the next
    // currentTime assignment naturally cancel the pending seek.
  }
}

function _seekVideoImmediate(targetTime) {
  _cancelVideoSeek();
  _playbackVideo.currentTime = targetTime;
}

function _seekVideoDebounced(targetTime, delayMs) {
  _cancelVideoSeek();
  _videoSeekDebounce = setTimeout(() => {
    _playbackVideo.currentTime = targetTime;
  }, delayMs);
}

function _syncVideoToAudio() {
  if (!_videoAvailable || !_videoVisible) return;
  const expected = _audioToVideoTime(_playbackAudio.currentTime);
  const drift = Math.abs(_playbackVideo.currentTime - expected);
  if (drift > 0.3) {
    _seekVideoImmediate(expected);
  }
}

// Wire up scrub detection on the seek bar
let _wasPlayingBeforeScrub = false;
{
  const seekBar = document.getElementById('playback-seek');
  if (seekBar) {
    seekBar.addEventListener('mousedown', () => {
      _videoScrubbing = true;
      _cancelVideoSeek();
      // Pause both audio and video during scrub
      _wasPlayingBeforeScrub = !_playbackAudio.paused;
      if (_wasPlayingBeforeScrub) {
        _playbackAudio.pause();
        document.getElementById('playback-play').innerHTML = '<i class="fa-solid fa-pause"></i>';
      }
      if (_videoAvailable && !_playbackVideo.paused) _playbackVideo.pause();
    });
    // Use window-level mouseup so we catch it even if cursor leaves the bar
    window.addEventListener('mouseup', () => {
      if (!_videoScrubbing) return;
      _videoScrubbing = false;
      _cancelVideoSeek();
      if (_videoAvailable && _videoVisible) {
        // Seek video to final position, wait for frame to decode, then resume both
        const target = _audioToVideoTime(_playbackAudio.currentTime);
        _playbackVideo.currentTime = target;
        if (_wasPlayingBeforeScrub) {
          _playbackVideo.addEventListener('seeked', function onSeeked() {
            _playbackVideo.removeEventListener('seeked', onSeeked);
            _playbackAudio.play();
            _playbackVideo.play().catch(() => {});
          });
        }
      } else if (_wasPlayingBeforeScrub) {
        // No video — just resume audio
        _playbackAudio.play();
      }
      _wasPlayingBeforeScrub = false;
    });
  }
}

// Patch existing playback functions to keep video in sync
const _origTogglePlayback = togglePlayback;
togglePlayback = function() {
  _origTogglePlayback();
  if (!_videoAvailable || !_videoVisible) return;
  if (_playbackAudio.paused) {
    _playbackVideo.pause();
  } else {
    _syncVideoToAudio();
    _playbackVideo.play().catch(() => {});
  }
};

const _origSeekPlayback = seekPlayback;
seekPlayback = function(val) {
  _origSeekPlayback(val);
  if (_videoAvailable) {
    if (_videoScrubbing) {
      // During scrub: debounce — only seek after user pauses dragging for 100ms
      _seekVideoDebounced(_audioToVideoTime(parseFloat(val)), 100);
    } else {
      // Direct seek (click on bar, or programmatic): immediate
      _seekVideoImmediate(_audioToVideoTime(parseFloat(val)));
    }
  }
};

const _origSeekToTime = seekToTime;
seekToTime = function(t) {
  _origSeekToTime(t);
  if (_videoAvailable) {
    _cancelVideoSeek();
    _seekVideoImmediate(_audioToVideoTime(t));
    if (_videoVisible && !_playbackVideo.paused !== !_playbackAudio.paused) {
      if (!_playbackAudio.paused) _playbackVideo.play().catch(() => {});
      else _playbackVideo.pause();
    }
  }
};

const _origSetPlaybackSpeed = setPlaybackSpeed;
setPlaybackSpeed = function(val) {
  _origSetPlaybackSpeed(val);
  if (_videoAvailable) _playbackVideo.playbackRate = parseFloat(val);
};

// Periodic drift correction — runs on audio's timeupdate
_playbackAudio.addEventListener('timeupdate', () => {
  if (_videoAvailable && _videoVisible && !_playbackAudio.paused && !_videoScrubbing) {
    _syncVideoToAudio();
    // Keep play state in sync (filter skipping can pause/seek audio)
    if (_playbackVideo.paused) _playbackVideo.play().catch(() => {});
  }
});

// When audio ends, stop video too
_playbackAudio.addEventListener('ended', () => {
  if (_videoAvailable) _playbackVideo.pause();
});

// When audio is paused externally, pause video
_playbackAudio.addEventListener('pause', () => {
  if (_videoAvailable && _videoVisible) _playbackVideo.pause();
});

// When audio plays, play video
_playbackAudio.addEventListener('play', () => {
  if (_videoAvailable && _videoVisible) {
    _syncVideoToAudio();
    _playbackVideo.play().catch(() => {});
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
    // Wait before next frame — ensures sequential, never piling up
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
      timeSpan.textContent = `${fmtTime(startT)} – ${fmtTime(endT)}`;
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

/** Mark minimap data as stale — next render will rebuild. */
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
    // Everything fits — viewport covers full minimap
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

/** Full minimap refresh — re-render canvas + viewport.
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
    // During live recording, debounce — segments arrive every ~0.5 s
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
    <div class="chat-msg-body markdown-body"></div>
    <div class="chat-msg-actions">
      <button class="chat-msg-action-btn" title="Copy response" onclick="_copyChatMsg(this)">
        <i class="fa-regular fa-copy"></i> Copy
      </button>
    </div>`;
  el.appendChild(wrap);
  scrollChatToBottom();  // response is starting — always scroll
  return wrap.querySelector('.chat-msg-body');
}

/* ── Tool-call collapsible widget ────────────────────────────────────────── */
function _renderToolWidget(msgWrap, toolCalls) {
  let widget = msgWrap.querySelector('.chat-tool-widget');
  if (!widget) {
    widget = document.createElement('div');
    widget.className = 'chat-tool-widget';
    const body = msgWrap.querySelector('.chat-msg-body');
    body.parentNode.insertBefore(widget, body);
  }
  const count = toolCalls.length;
  const doneCount = toolCalls.filter(tc => tc.result).length;
  const allDone = doneCount === count;
  const isOpen = widget.classList.contains('open');

  let itemsHtml = '';
  for (const tc of toolCalls) {
    const icon = !tc.result ? '⏳' : tc.result.success ? '✓' : '✗';
    const iconCls = !tc.result ? 'pending' : tc.result.success ? 'success' : 'error';
    const label = _toolDisplayName(tc.name);
    const detail = tc.result ? tc.result.summary : _toolInputSummary(tc.name, tc.input);
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
    <div class="chat-tool-details">${itemsHtml}</div>`;

  if (isOpen) widget.classList.add('open');
}

function _toolDisplayName(name) {
  const map = {
    get_screenshot: 'Screenshot',
    search_transcripts: 'Search Transcripts',
    semantic_search: 'Semantic Search',
    get_session_detail: 'Load Session',
    list_speakers: 'List Speakers',
  };
  return map[name] || name;
}

function _toolInputSummary(name, input) {
  if (name === 'get_screenshot' && input?.timestamp != null) {
    return `at ${Number(input.timestamp).toFixed(1)}s`;
  }
  if (name === 'search_transcripts' && input?.query) return `"${input.query}"`;
  if (name === 'semantic_search' && input?.query) return `"${input.query}"`;
  if (name === 'get_session_detail' && input?.session_id) return input.session_id.substring(0, 8) + '...';
  if (name === 'list_speakers') return 'Voice Library';
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

function appendUserBubble(text, attachments) {
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
  // User sent a message — reset flag and force-scroll so the response is visible.
  _chatAtBottom = true;
  scrollChatToBottom();
}

function scrollChatToBottom(force = false) {
  if (!force && !_chatAtBottom) return;
  const el = document.getElementById('chat-messages');
  el.scrollTop = el.scrollHeight;
}

async function clearChat() {
  if (!state.sessionId) return;
  document.getElementById('chat-messages').innerHTML =
    '<p class="empty-hint">Chat cleared.</p>';
  await fetch('/api/chat/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId }),
  }).catch(() => {});
}

let _chatRequestId = null;  // tracks the active chat request for cancellation

async function sendMessage() {
  if (state.aiChatBusy || !state.sessionId) return;
  const input    = document.getElementById('chat-input');
  const question = input.value.trim();
  const attachments = [..._pendingAttachments];
  if (!question && !attachments.length) return;

  input.value = '';
  _autogrowChatInput();
  appendUserBubble(question, attachments);
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
    }),
  });
  if (resp.ok) {
    const data = await resp.json();
    _chatRequestId = data.request_id;
  } else {
    const err = await resp.json().catch(() => ({}));
    const bubble = createAssistantBubble();
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
  navigator.clipboard.writeText(body.innerText).then(() => {
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
}

function _clearAttachments() {
  _pendingAttachments = [];
  const preview = document.getElementById('chat-attach-preview');
  preview.innerHTML = '';
  preview.classList.add('hidden');
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

    const showOverlay = (e) => {
      // Update hint with file count when the browser exposes it
      const count = e.dataTransfer?.items?.length;
      if (hint && count) {
        hint.textContent = count === 1 ? '1 file ready to attach' : `${count} files ready to attach`;
      } else if (hint) {
        hint.textContent = 'Images · PDFs · text files';
      }
      overlay.setAttribute('aria-hidden', 'false');
      overlay.classList.add('active');
    };

    const hideOverlay = () => {
      overlay.classList.remove('active');
      overlay.setAttribute('aria-hidden', 'true');
    };

    chatCol.addEventListener('dragenter', e => {
      if (!e.dataTransfer?.types?.includes('Files')) return;
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
      if (e.dataTransfer?.types?.includes('Files')) e.preventDefault();
    });

    chatCol.addEventListener('drop', e => {
      e.preventDefault();
      dragCounter = 0;
      hideOverlay();
      if (e.dataTransfer?.files?.length) _handleFileSelect(e.dataTransfer.files);
    });
  }
}

// ── Paste images from clipboard ──────────────────────────────────────────────
document.getElementById('chat-input')?.addEventListener('paste', e => {
  const items = e.clipboardData?.items;
  if (!items) return;
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      e.preventDefault();
      const file = item.getAsFile();
      if (file) _uploadAttachment(file);
    }
  }
});

/* ── Past sessions ───────────────────────────────────────────────────────── */
async function loadSession(sessionId) {
  if (sessionId === state.sessionId) return;

  if (state.isRecording) {
    if (!confirm('Stop the current recording and load this session?')) return;
    await fetch('/api/recording/stop', { method: 'POST' });
  }

  const gen = ++_loadGeneration;  // cancel any in-flight chunked render

  const data = await fetch(`/api/sessions/${sessionId}`).then(r => r.json());
  if (data.error) {
    // Session not found — clean up URL and show a brief status message
    history.replaceState(null, '', location.pathname);
    flashStatus('Session not found');
    return;
  }
  if (gen !== _loadGeneration) return;  // another load started while we were fetching

  clearAll();
  state.sessionId     = sessionId;
  state.isViewingPast = true;
  history.pushState({}, '', '/session?id=' + sessionId);
  updateRecordBtn();
  _loadPaneVisible(sessionId);

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

  // Render segments in chunks to keep the UI responsive on large transcripts.
  const segments = data.segments || [];
  const CHUNK = 150;  // segments per animation frame

  if (segments.length > CHUNK) {
    // Show loading hint and render in async chunks
    const transcriptEl = document.getElementById('transcript');
    transcriptEl.innerHTML = '';
    const loadingHint = document.createElement('p');
    loadingHint.className = 'empty-hint loading-hint';
    loadingHint.textContent = `Loading ${segments.length} segments…`;
    transcriptEl.appendChild(loadingHint);

    _bulkLoading = true;
    const completed = await _renderSegmentsChunked(segments, CHUNK, loadingHint, gen);
    _bulkLoading = false;
    if (!completed) return;  // load was cancelled by a newer loadSession call
    _finishBulkLoad();
  } else {
    // Small transcript — render synchronously (fast enough)
    segments.forEach(s =>
      appendTranscript(s.text, s.source_override || s.source || 'loopback', s.start_time, s.end_time,
                       s.id, s.label_override, s.source_override ? s.source : null)
    );
  }

  // Handle pending search highlight — scroll to and flash the matching segment
  if (_pendingSearchHighlight) {
    const hl = _pendingSearchHighlight;
    _pendingSearchHighlight = null;
    requestAnimationFrame(() => _executeSearchHighlight(hl));
  }

  // Restore summary prompt for this session
  const storedPrompt = localStorage.getItem('summary-prompt:' + sessionId) || '';
  _applyPromptText(storedPrompt);
  await fetch('/api/custom-prompt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ custom_prompt: storedPrompt }),
  }).catch(() => {});

  // Show playback bar if audio is available
  if (data.has_audio) initPlayback(sessionId);
  if (data.has_video) initVideo(sessionId, data.video_offset);

  if (data.summary) {
    const sumEl = document.getElementById('summary');
    sumEl.innerHTML = renderMd(data.summary);
    highlightCode('#summary');
    linkifyTimestamps(sumEl);
  }

  if (data.chat_messages?.length) {
    document.getElementById('chat-messages').innerHTML = '';
    for (const m of data.chat_messages) {
      const atts = m.attachments ? (typeof m.attachments === 'string' ? JSON.parse(m.attachments) : m.attachments) : null;
      if (m.role === 'user') {
        appendUserBubble(m.content, atts);
      } else {
        const b = createAssistantBubble();
        // Hide the processing indicator for restored messages
        const wrap = b.closest('.chat-msg');
        if (wrap) {
          const proc = wrap.querySelector('.chat-processing');
          if (proc) proc.classList.remove('active');
        }
        b.innerHTML = renderMd(m.content);
        linkifyTimestamps(b);
        // Restore tool-call widget if present
        const tcRaw = m.tool_calls;
        if (tcRaw) {
          const tcs = typeof tcRaw === 'string' ? JSON.parse(tcRaw) : tcRaw;
          if (tcs?.length && wrap) _renderToolWidget(wrap, tcs);
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
  _highlightSelectedSpeakerBadges();
  if (!document.getElementById('speaker-manager-overlay')?.classList.contains('hidden')) {
    renderSpeakerManager();
  }
  _updateCollapseFabVisibility();
  _updateMinimapFabVisibility();
  _refreshMinimap(true);
}

/* ── Shutdown ────────────────────────────────────────────────────────────── */
function confirmShutdown() {
  // Inline confirmation rather than browser alert (looks nicer)
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.innerHTML = `
    <div class="dialog">
      <h3>Shut down server?</h3>
      <p>This will stop recording (if active) and close the Meeting Assistant server.</p>
      <div class="dialog-btns">
        <button class="btn btn-danger" onclick="doShutdown()">Shut Down</button>
        <button class="btn" style="background:var(--surface2);color:var(--fg)"
                onclick="this.closest('.overlay').remove()">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
}

async function doShutdown() {
  document.querySelector('.overlay')?.remove();
  await fetch('/api/shutdown', { method: 'POST' }).catch(() => {});
  document.body.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:center;height:100vh;
                flex-direction:column;gap:12px;color:#8b949e;font-family:system-ui">
      <span style="font-size:40px"><img id="shutdown-icon" class="brand-icon shutdown-icon" src="/static/images/logo.png" alt=""></span>
      <p style="font-size:16px;font-weight:600;color:#e6edf3">Meeting Assistant shut down.</p>
      <p style="font-size:13px">You can close this tab.</p>
    </div>`;
}

/* ── Misc helpers ────────────────────────────────────────────────────────── */
function clearAll() {
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
  _pendingSpeakerProfiles = [];
  _sessionLinks = {};
  _transcriptFilter = { search: '', speakers: new Set(), timeMin: 0, timeMax: Infinity };
  _showNoise = false;
  _noiseSolo = false;
  _manualNoiseKeys = new Set();
  _showOriginalKeys = false;
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
  closeSpeakerManager();
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
  state.aiChatBusy = false;
  _setChatBusy(false);
  _clearAttachments();
  state.summaryBuffer    = '';
  state.summaryStreaming  = false;
  state.chatBuffer       = '';
  state.chatToolCalls    = [];
  destroyPlayback();
}

function highlightCode(sel) {
  document.querySelectorAll(`${sel} pre code`).forEach(el => {
    if (!el.dataset.highlighted) hljs.highlightElement(el);
  });
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function flashStatus(msg) {
  const el   = document.getElementById('status-text');
  const prev = el.textContent;
  el.textContent = msg;
  setTimeout(() => { el.textContent = prev; }, 1800);
}

/* ── Browser mic capture ─────────────────────────────────────────────────── */
let _bmStream    = null;   // MediaStream
let _bmCtx       = null;   // AudioContext
let _bmProcessor = null;   // ScriptProcessorNode

async function startBrowserMic() {
  if (_bmStream) return;   // already running
  try {
    _bmStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    // Request 48 kHz to match the server-side assumption in inject_mic_data
    _bmCtx = new AudioContext({ sampleRate: 48000 });
    const source = _bmCtx.createMediaStreamSource(_bmStream);
    // 4096-sample buffer ≈ 85 ms at 48 kHz - fine for transcription latency
    _bmProcessor = _bmCtx.createScriptProcessor(4096, 1, 1);
    _bmProcessor.onaudioprocess = e => {
      if (!state.isRecording && !state.isTesting) return;
      const f32 = e.inputBuffer.getChannelData(0);   // mono Float32
      // Convert to Int16
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        const s = Math.max(-1, Math.min(1, f32[i]));
        i16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      // Fire-and-forget - don't await; high-frequency short requests
      fetch('/api/audio/mic-chunk', {
        method:  'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body:    i16.buffer,
      }).catch(() => {});
    };
    source.connect(_bmProcessor);
    _bmProcessor.connect(_bmCtx.destination);
    console.log('[mic] Browser mic started @', _bmCtx.sampleRate, 'Hz');
  } catch (err) {
    console.error('[mic] getUserMedia failed:', err);
    alert('Could not access browser microphone:\n' + err.message);
    _bmStream = null;
  }
}

function stopBrowserMic() {
  if (_bmProcessor) { _bmProcessor.disconnect(); _bmProcessor = null; }
  if (_bmCtx)       { _bmCtx.close();            _bmCtx       = null; }
  if (_bmStream)    { _bmStream.getTracks().forEach(t => t.stop()); _bmStream = null; }
}

// Ensure browser mic is released when the page is closed or refreshed
window.addEventListener('beforeunload', () => stopBrowserMic());

function syncBrowserMic() {
  const micVal   = document.getElementById('viz-mic-sel')?.value;
  const needsMic = (state.isRecording || state.isTesting) && micVal === '-2';
  if (needsMic && !_bmStream)  startBrowserMic();
  if (!needsMic && _bmStream)  stopBrowserMic();
}

/* ── Audio device selection ──────────────────────────────────────────────── */
async function loadAudioDevices() {
  const lbSel  = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  if (!lbSel || !micSel) return;

  // Saved choices from server prefs (with localStorage fallback for migration)
  const savedLb  = _prefs.loopback_device ?? localStorage.getItem('viz-loopback-idx') ?? '';
  const savedMic = _prefs.mic_device      ?? localStorage.getItem('viz-mic-idx')      ?? '-2';

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
    if (savedLb && [...lbSel.options].some(o => o.value === String(savedLb))) {
      lbSel.value = savedLb;
    }
  }

  // Populate mic selector - Browser Mic is always first, then None, then WASAPI devices
  micSel.innerHTML = '<option value="-2">Browser Mic (this tab)</option><option value="-1">None</option>';
  for (const d of data.input) {
    const opt = document.createElement('option');
    opt.value       = d.index;
    opt.textContent = d.name;
    micSel.appendChild(opt);
  }
  if (savedMic && [...micSel.options].some(o => o.value === String(savedMic))) {
    micSel.value = savedMic;
  }

  // Re-apply disabled state if currently recording
  lbSel.disabled  = state.isRecording;
  micSel.disabled = state.isRecording;
}

function saveDeviceSelection() {
  const lbSel  = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  if (lbSel)  savePref('loopback_device', lbSel.value);
  if (micSel) savePref('mic_device',      micSel.value);
  // Release or acquire the browser getUserMedia stream immediately when the
  // mic selector changes — otherwise a stale getUserMedia lock on the physical
  // mic device causes WASAPI shared-mode contention and garbled audio.
  syncBrowserMic();
}

async function toggleAudioTest() {
  if (state.isTesting) {
    try {
      await fetch('/api/audio/test/stop', { method: 'POST' });
    } catch (_) { /* network error */ }
    // Eagerly release browser mic regardless of server response —
    // don't wait for SSE event which may be delayed or lost.
    state.isTesting = false;
    updateTestBtn();
    stopBrowserMic();
  } else {
    const lbVal  = document.getElementById('viz-loopback-sel')?.value;
    const micVal = document.getElementById('viz-mic-sel')?.value;
    const body   = {};
    if (lbVal  !== '' && lbVal  != null) body.loopback_device = parseInt(lbVal,  10);
    if (micVal !== '' && micVal != null) body.mic_device      = parseInt(micVal, 10);

    const resp = await fetch('/api/audio/test/start', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(err.error || 'Failed to start audio test');
    }
  }
}

/* ── Audio visualizer ────────────────────────────────────────────────────── */
const N_BARS = 32;
let vizLbTarget = 0, vizMicTarget = 0;
let vizLb = 0,       vizMic = 0;
let vizHasMic  = false;
let vizLbSpec  = [];   // frequency spectrum from server (N_BARS values, 0–1)
let vizMicSpec = [];
// Smoothed per-band values for animation (fast attack, slow decay)
const vizLbBars  = new Float32Array(N_BARS);
const vizMicBars = new Float32Array(N_BARS);

function updateLevelMeters(lb, mic, hasMic) {
  const toH = v => Math.round(Math.min(100, Math.log1p(v * 60) / Math.log1p(60) * 100));
  const lbEl  = document.getElementById('viz-meter-lb');
  const micEl = document.getElementById('viz-meter-mic');
  if (lbEl)  lbEl.style.height  = toH(lb) + '%';
  if (micEl) micEl.style.height = hasMic ? toH(mic) + '%' : '0%';
}

function startVizLoop() {
  const canvas = document.getElementById('viz-canvas');
  if (!canvas) return;

  const dpr = window.devicePixelRatio || 1;
  const resize = () => {
    canvas.width  = canvas.offsetWidth  * dpr;
    canvas.height = canvas.offsetHeight * dpr;
  };
  resize();
  new ResizeObserver(resize).observe(canvas);

  requestAnimationFrame(function loop() {
    requestAnimationFrame(loop);

    const ctx = canvas.getContext('2d');
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

    // ── EQ bars — desktop fills upward from midline, mic fills downward ───
    for (let i = 0; i < N_BARS; i++) {
      const x = i * barW + pad;
      const bw = barW - pad * 2;

      // Desktop bar (top half, grows up from midline)
      const lbH = Math.max(1, vizLbBars[i] * (midY - 3));
      const lbAlpha = lbActive ? 0.25 + 0.75 * vizLbBars[i] : 0.12;
      ctx.fillStyle = `rgba(88,166,255,${lbAlpha.toFixed(2)})`;
      ctx.fillRect(x, midY - lbH, bw, lbH);

      // Mic bar (bottom half, grows down from midline)
      if (vizHasMic) {
        const micH = Math.max(1, vizMicBars[i] * (midY - 3));
        const micAlpha = micActive ? 0.25 + 0.75 * vizMicBars[i] : 0.12;
        ctx.fillStyle = `rgba(0,180,100,${micAlpha.toFixed(2)})`;
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
  });
}

/* ── Brand horizontal visualizer (bars extend left/right from logo) ──────── */
function startBrandVizLoop() {
  const canvas = document.getElementById('brand-viz-canvas');
  if (!canvas) return;

  const dpr = window.devicePixelRatio || 1;
  const resize = () => {
    canvas.width  = canvas.offsetWidth  * dpr;
    canvas.height = canvas.offsetHeight * dpr;
  };
  resize();
  new ResizeObserver(resize).observe(canvas);

  // Separate smoothed bars so brand viz can animate independently
  const bvLbBars  = new Float32Array(N_BARS);
  const bvMicBars = new Float32Array(N_BARS);

  requestAnimationFrame(function loop() {
    requestAnimationFrame(loop);

    const ctx = canvas.getContext('2d');
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
  });
}

/* ── Gain controls ───────────────────────────────────────────────────────── */
let _gainSendTimer = null;
let _gainLastInput = 0;   // timestamp of last user interaction — suppresses SSE sync

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
      alert(err.error || 'Failed to change model');
      loadModelConfig();  // revert selection
    }
  } catch (e) {
    alert('Failed to change model');
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
      alert(err.error || 'Failed to change diarizer');
      loadModelConfig();
    }
  } catch (e) {
    alert('Failed to change diarizer');
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
    alert('Failed to toggle diarization');
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

const AI_MODELS = {
  anthropic: [
    { id: 'claude-opus-4-6',          label: 'Claude Opus 4.6 - most capable' },
    { id: 'claude-sonnet-4-6',         label: 'Claude Sonnet 4.6 - recommended' },
    { id: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5 - fastest' },
  ],
  openai: [
    { id: 'gpt-5.4',              label: 'GPT-5.4 - most capable' },
    { id: 'gpt-5.3-chat-latest',  label: 'GPT-5.3 - balance speed & smarts' },
    { id: 'gpt-4.1',              label: 'GPT-4.1 - strong fallback' },
    { id: 'gpt-4o',               label: 'GPT-4o - recommended' },
    { id: 'gpt-4o-mini',          label: 'GPT-4o mini - fastest' },
    { id: 'o4-mini',              label: 'o4-mini - reasoning' },
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

async function openSettings() {
  const overlay = document.getElementById('settings-overlay');
  overlay.classList.remove('hidden');

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
    _applyAiConfig(aiCfg.provider, aiCfg.model, currentAiModels);
    updateChatModelLabel(aiCfg.provider, aiCfg.model, currentAiModels);
  } catch (_) {}

  // Startup toggle (Windows only — hidden on unsupported platforms)
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

  // Audio params — load eagerly so panels are ready when clicked
  _apRefresh().then(() => _syncScreenToggle());

  // Presets for all sections
  loadTranscriptionPresets();
  loadDiarizationPresets();
  loadScreenPresets();
  loadScreenDisplays();
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

  // Show only the active provider's key field
  const anthField = document.getElementById('key-anthropic-field');
  const oaiField  = document.getElementById('key-openai-field');
  anthField.style.display = provider === 'anthropic' ? '' : 'none';
  oaiField.style.display  = provider === 'openai'    ? '' : 'none';
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
    _applyAiConfig(data.provider, data.model, currentAiModels);
    updateChatModelLabel(data.provider, data.model, currentAiModels);
  } catch (_) {}
}

async function setAiModel(model) {
  try {
    const data = await fetch('/api/ai_settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    }).then(r => r.json());
    _applyAiConfig(data.provider, data.model, currentAiModels);
    updateChatModelLabel(data.provider, data.model, currentAiModels);
  } catch (_) {}
}

function _renderKeyStatus(keyName, inputId, keys) {
  const info = keys[keyName] || {};
  const statusEl = document.getElementById(inputId + '-status');
  const inputEl  = document.getElementById(inputId);
  if (!statusEl || !inputEl) return;

  if (info.is_set) {
    statusEl.textContent = 'Configured: ' + info.masked;
    statusEl.className = 'key-status key-set';
    inputEl.placeholder = info.masked;
  } else {
    statusEl.textContent = info.required ? 'Not set' : 'Not set - optional';
    statusEl.className = 'key-status ' + (info.required ? 'key-missing' : 'key-optional');
  }
}

function closeSettings() {
  document.getElementById('settings-overlay').classList.add('hidden');
  // Clear password fields
  ['key-anthropic', 'key-openai', 'key-huggingface'].forEach(id => {
    document.getElementById(id).value = '';
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
      // Hide topbar update button if it was showing
      document.getElementById('topbar-update-btn')?.classList.add('hidden');
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

  // Also disable topbar button if visible
  const tbBtn = document.getElementById('topbar-update-btn');
  if (tbBtn) { tbBtn.disabled = true; tbBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Updating...'; }

  try {
    const res = await fetch('/api/update/apply', { method: 'POST' });
    const data = await res.json();

    if (data.error) {
      statusEl.textContent = data.error;
      statusEl.className = 'settings-info-val val-warn';
      btn.disabled = false;
      btn.textContent = 'Retry Update';
      if (tbBtn) { tbBtn.disabled = false; tbBtn.innerHTML = '<i class="fa-solid fa-download"></i> Retry'; }
    } else {
      statusEl.textContent = 'Restarting...';
      btn.textContent = 'Restarting...';
      if (tbBtn) { tbBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Restarting...'; }
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

function _showTopbarUpdate(commitsBehind) {
  const btn = document.getElementById('topbar-update-btn');
  if (!btn) return;
  btn.classList.remove('hidden');
  btn.disabled = false;
  const s = commitsBehind !== 1 ? 's' : '';
  btn.title = `${commitsBehind} update${s} available`;
  btn.innerHTML = `<i class="fa-solid fa-download"></i> Update`;
}

async function topbarApplyUpdate() {
  const btn = document.getElementById('topbar-update-btn');
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Updating...';

  try {
    const res = await fetch('/api/update/apply', { method: 'POST' });
    const data = await res.json();

    if (data.error) {
      btn.disabled = false;
      btn.innerHTML = '<i class="fa-solid fa-download"></i> Retry';
      btn.title = `Update failed: ${data.error}`;
    } else {
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Restarting...';
      _pollUntilBack();
    }
  } catch (_) {
    btn.disabled = false;
    btn.innerHTML = '<i class="fa-solid fa-download"></i> Retry';
    btn.title = 'Update failed — click to retry';
  }
}

// Silent update check — shows the topbar button only if updates are found.
// Errors are silently ignored.
async function _silentUpdateCheck() {
  try {
    const res = await fetch('/api/update/check');
    const data = await res.json();
    if (!data.error && !data.up_to_date && data.commits_behind > 0) {
      _showTopbarUpdate(data.commits_behind);
    }
  } catch (_) { /* silent — don't bother the user if offline */ }
}

// Periodic update check — runs every 15 minutes, but only when idle
// (no recording in progress).  Stops once an update is found.
let _updateCheckInterval = null;
function _startPeriodicUpdateCheck() {
  // Run once on startup
  _silentUpdateCheck();
  // Then every 15 minutes while idle
  _updateCheckInterval = setInterval(() => {
    // Skip if already showing update button or recording is active
    if (!document.getElementById('topbar-update-btn')?.classList.contains('hidden')) return;
    if (state.isRecording) return;
    _silentUpdateCheck();
  }, 15 * 60 * 1000);
}

function switchSettingsSection(btn) {
  document.querySelectorAll('.settings-nav-item').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.settings-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(btn.dataset.target).classList.add('active');
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

  // Find any toggle master key in this section (controls enabled state of siblings)
  let toggleMasterKey = null;
  let toggleInverted = false; // when true, ON disables siblings instead of enabling them
  for (const [k, s] of Object.entries(paramDefs)) {
    if (s.type === 'toggle') { toggleMasterKey = k; toggleInverted = !!s.inverts_siblings; break; }
  }

  for (const [key, spec] of Object.entries(paramDefs)) {
    const val = current[key] ?? spec.value;
    const isDefault = Math.abs(val - spec.value) < 1e-9;
    const unit = spec.unit ? `<span class="ap-unit">${spec.unit}</span>` : '';
    const tooltip = spec.tooltip || spec.description;

    const param = document.createElement('div');
    param.className = 'ap-param';
    param.dataset.apKey = key;

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
        // Enable/disable sibling params in this section
        const siblingsEnabled = toggleInverted ? !cb.checked : cb.checked;
        _apSetSectionEnabled(containerId, key, siblingsEnabled);
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
    const toggleOn = !!parseInt(current[toggleMasterKey] ?? 0);
    const isDisabled = (toggleMasterKey && key !== toggleMasterKey && (toggleInverted ? toggleOn : !toggleOn));

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

function _apSetSectionEnabled(containerId, toggleKey, enabled) {
  const container = document.getElementById(containerId);
  if (!container) return;
  for (const param of container.querySelectorAll('.ap-param')) {
    if (param.dataset.apKey === toggleKey) continue;
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
      const spec = (_apCache.transcription[key] || _apCache.diarization[key] || (_apCache.echo_cancellation && _apCache.echo_cancellation[key]) || (_apCache.screen_recording && _apCache.screen_recording[key]));
      const resetBtn = document.getElementById(`ap-reset-${key}`);
      if (resetBtn && spec) {
        const isDefault = Math.abs(value - spec.value) < 1e-9;
        resetBtn.classList.toggle('ap-reset-hidden', isDefault);
      }
      // Keep sidebar screen toggle in sync with settings panel
      if (key === 'screen_record_enabled') _syncScreenToggle();
      // Switch preset to "Custom" when a parameter is manually changed
      _switchToCustomPreset(key);
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
      const spec = (_apCache.transcription[key] || _apCache.diarization[key] || (_apCache.echo_cancellation && _apCache.echo_cancellation[key]) || (_apCache.screen_recording && _apCache.screen_recording[key]));
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

  // Calculate scale for thumbnails — fit all monitors into the grid
  const allLeft   = Math.min(..._screenDisplays.map(d => d.x));
  const allTop    = Math.min(..._screenDisplays.map(d => d.y));
  const allRight  = Math.max(..._screenDisplays.map(d => d.x + d.width));
  const allBottom = Math.max(..._screenDisplays.map(d => d.y + d.height));
  const totalW = allRight - allLeft;
  const totalH = allBottom - allTop;

  // Grid is roughly 200px wide — scale to fit
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
  // Verify the save took effect — revert the checkbox if it didn't
  _syncScreenToggle();
}

function _syncScreenToggle() {
  if (!_apCache) return;
  const enabled = parseInt(_apCache.current.screen_record_enabled || 0);
  const toggle = document.getElementById('screen-record-toggle');
  if (toggle) toggle.checked = !!enabled;
  // Toggle visual is handled by the pane collapse — no need to hide body here
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

async function saveApiKeys() {
  const anthKey = document.getElementById('key-anthropic').value.trim();
  const oaiKey  = document.getElementById('key-openai').value.trim();
  const hfKey   = document.getElementById('key-huggingface').value.trim();
  const body = {};
  if (anthKey) body.ANTHROPIC_API_KEY = anthKey;
  if (oaiKey)  body.OPENAI_API_KEY    = oaiKey;
  if (hfKey)   body.HUGGING_FACE_KEY  = hfKey;

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
      alert(data.error || 'Failed to save keys');
      btn.textContent = 'Save Keys';
      btn.disabled = false;
    }
  } catch (e) {
    alert('Failed to save keys');
    btn.textContent = 'Save Keys';
    btn.disabled = false;
  }
}

/* ── Init ────────────────────────────────────────────────────────────────── */

const _isHomePage = !!window._isHomePage;

// Session-page-specific init (transcript scroll, panels, etc.)
if (!_isHomePage) {
  // Auto-scroll behavior:
  // - Live recording: disable when user scrolls up, re-enable at bottom
  // - Playback: disable on user-initiated scroll only, re-enable via button click
  document.getElementById('transcript').addEventListener('scroll', () => {
    // Ignore programmatic scrolls (from playback tracking, seek, button clicks, etc.)
    if (_programmaticScrollCount > 0) return;

    if (_playbackActive && !_playbackAudio.paused) {
      // During playback, only user-initiated scrolls disable auto-scroll
      if (_autoScroll) {
        _autoScroll = false;
        updateAutoScrollBtn();
      }
    } else {
      // Live mode: re-enable at bottom
      const el = document.getElementById('transcript');
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      if (_autoScroll !== atBottom) {
        _autoScroll = atBottom;
        updateAutoScrollBtn();
      }
    }
  });
}

// Shared init (sidebar, SSE, status, devices, models)
connectSSE();

// Close SSE on page unload to prevent connection leaks when navigating
window.addEventListener('beforeunload', () => {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }
});

refreshSidebar();
_checkSemanticSearchReady();
fetch('/api/status').then(r => r.json()).then(onStatus);

if (!_isHomePage) {
  fetch('/api/ai_settings')
    .then(r => r.json())
    .then(aiCfg => {
      currentAiModels = { ...AI_MODELS, ..._getAiModels(aiCfg.models) };
      updateChatModelLabel(aiCfg.provider, aiCfg.model, currentAiModels);
    })
    .catch(() => {});
}

startVizLoop();
if (!_isHomePage) startBrandVizLoop();
initGainSliders();
_restoreSidebarPanes();

if (!_isHomePage) {
  _tnInitSearch();
  _tsbInitAutocomplete();
  _syncPanelBottomRadius();
  _syncSummaryBottomRadius();
}

// Load preferences first, then init components that depend on saved values
loadPreferences().then(() => {
  loadAudioDevices();
  loadModelConfig();
});
// Screen recording: load displays + sync toggle
_apLoad().then(() => { try { _syncScreenToggle(); } catch {} });
try { loadScreenDisplays(); } catch {}

if (!_isHomePage) {
  loadSummaryPrompt();
  _startPeriodicUpdateCheck();

  // Auto-open settings if ?settings=1 or ?setup=1 is in the URL
  // Auto-load session if ?session=<id> is in the URL
  {
    const params = new URLSearchParams(location.search);
    if (params.has('settings') || params.has('setup')) {
      openSettings();
      history.replaceState(null, '', location.pathname);
    } else if (params.has('fingerprint')) {
      openFingerprintPanel();
      history.replaceState(null, '', location.pathname);
    } else if (params.has('id')) {
      // Defer until status has loaded — if the session is actively recording,
      // the SSE status+replay events handle everything; only call loadSession
      // for past (non-recording) sessions.
      const _pendingSessionId = params.get('id');
      fetch('/api/status').then(r => r.json()).then(st => {
        if (st.recording && st.session_id === _pendingSessionId) {
          // Active recording — SSE status event will set state; don't call loadSession
          return;
        }
        loadSession(_pendingSessionId);
      }).catch(() => loadSession(_pendingSessionId));
    }
  }

  window.addEventListener('popstate', () => {
    const params = new URLSearchParams(location.search);
    const sid = params.get('id');
    if (sid) {
      loadSession(sid);
    } else if (!state.isRecording) {
      state.sessionId    = null;
      state.isViewingPast = false;
      clearAll();
      updateRecordBtn();
    }
  });
}
