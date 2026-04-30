/* ── marked.js setup ─────────────────────────────────────────────────────── */
marked.use({ breaks: true, gfm: true });

// Open every rendered markdown link in a new tab. Skip links that already
// declare a `target` *or* have an `onclick` handler — the latter is how the
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
 * One cursor span is created and reused — after each render it's moved to the
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

  // Preserve existing loaded images - detach them before morphdom runs,
  // then restore them after. This prevents flashing when morphdom
  // recreates parent <p> elements around unchanged images.
  const existingImgs = new Map();
  el.querySelectorAll('img[src]').forEach(img => {
    existingImgs.set(img.getAttribute('src'), img);
  });

  const tmp = document.createElement('div');
  tmp.innerHTML = newHtml;
  morphdom(el, tmp, { childrenOnly: true });

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

/**
 * Post-process rendered summary HTML to make timestamps clickable pills.
 * Matches single timestamps [M:SS] and ranges [M:SS–M:SS] (en-dash, em-dash,
 * or plain hyphen as separator). Clicking seeks to the start of the range.
 */
function linkifyTimestamps(container) {
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  // Group 1: start time. Group 2 (optional): end time after - or –
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

// Visual column order - maps position (left→right) to column index.
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
// Relative column proportions - updated when user drags; loaded from settings on init.
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

// Per-session summary stream tracking: { [sessionId]: { buffer, streaming, mode } }
const _summaryStreams = {};

// Apply sidebar layout from cache synchronously - eliminates flash before async prefs load
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
  // Populate the global chat-system-prompt textarea (if on the session page)
  _syncGlobalChatPromptUI();
  // Refresh session-override badge (no-op on home page)
  if (state.sessionId) refreshSessionChatPromptBadge();
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

// Small color utils — all work in sRGB space, good enough for UI tinting.
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

async function refreshSidebar() {
  const [sessions, folders] = await Promise.all([
    fetch('/api/sessions').then(r => r.json()),
    fetch('/api/folders').then(r => r.json()).catch(() => []),
  ]);
  _sidebarAllSessions = sessions;
  _sidebarFolders = folders;
  _renderSidebar();
  // Bootstrap race: if a session was opened via URL before this fetch
  // completed, expand its ancestors now that we know the folder tree.
  if (typeof state !== 'undefined' && state.sessionId) {
    _revealSessionInSidebar(state.sessionId);
  }
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
              : m.kind === 'participant' ? '<span class="search-match-kind search-match-participant"><i class="fa-solid fa-user"></i> participant</span>'
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
              // FTS match without segment_id - fall back to text search
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
      // Default click (no specific snippet) - still set query for text highlight
      const origClick = el.onclick;
      el.addEventListener('click', () => {
        if (data.matches?.some(m => m.segment_id != null || m.kind === 'segment')) {
          const first = data.matches.find(m => m.segment_id != null);
          _pendingSearchHighlight = first
            ? { segmentId: first.segment_id, query: _sidebarSearchQuery }
            : { query: _sidebarSearchQuery };
        }
      }, true);  // capture phase - runs before the loadSession click
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

  // Ungrouped sessions (no folder or deleted folder) - also acts as a drop
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
  _updateActiveFolderHighlights();
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

function _renderFolderSubtree(parentId, depth, container, childMap, sessionsByFolder, folderIds) {
  const children = childMap.get(parentId) || [];
  for (const folder of children) {
    const folderSessions = sessionsByFolder.get(folder.id) || [];
    const totalCount = _countSessionsRecursive(folder.id, childMap, sessionsByFolder);
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
    // responsive — loadSession is async (fetch + render), and waiting for
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
    const filtered = s.speakers.filter(sp => sp.name && !/^Speaker \d+$/i.test(sp.name));
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
    rea.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i>  Reanalyze';
    rea.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); reanalyzeSession(ev, s.id); });
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
  if (!confirm(msg)) return;
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
  // would otherwise mislead the sidebar — fall through and compute the
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
      // No ended_at and no segments — show just the date/time.
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

async function reanalyzeSession(e, sessionId) {
  if (e) e.stopPropagation();
  if (state.isRecording) { alert('Cannot reanalyze while recording.'); return; }
  if (state.isReanalyzing) { alert('Reanalysis already in progress.'); return; }

  // Load the session as active so incoming transcript SSE events land on screen
  if (sessionId !== state.sessionId) {
    const data = await fetch(`/api/sessions/${sessionId}`).then(r => r.json());
    if (data.error) { alert(data.error); return; }
    state.sessionId     = sessionId;
    state.isViewingPast = false;
    document.getElementById('record-btn').disabled = true;
    if (data.speaker_profiles?.length) {
      data.speaker_profiles.forEach(p => applySpeakerProfileUpdate(p));
    }
  } else {
    state.isViewingPast = false;
    document.getElementById('record-btn').disabled = true;
  }

  // Clear only the transcript display - keep chat and summary intact
  const transcriptEl = document.getElementById('transcript');
  if (transcriptEl) transcriptEl.innerHTML = '';

  // Keep playback available during reanalysis - the WAV file still exists
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

async function reanalyzeCurrentSession() {
  if (!state.sessionId) return;
  await reanalyzeSession(null, state.sessionId);
}

function newSession() {
  if (state.isRecording) return;
  state.sessionId    = null;
  state.isViewingPast = false;
  clearAll();
  _updateActiveFolderHighlights();
  history.pushState({}, '', '/session');
  _applyPromptText('');
  updateRecordBtn();
  refreshSidebar();
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
    if (!resp.ok) { alert(data.error || 'Upload failed'); return; }

    // The backend created a session and started reanalysis – load it
    const sessionId = data.session_id;
    state.sessionId     = sessionId;
    state.isViewingPast = false;
    state.isReanalyzing = true;
    history.pushState({}, '', '/session?id=' + sessionId);

    // Clear display for incoming transcript
    clearAll();
    state.sessionId = sessionId;

    const transcriptEl = document.getElementById('transcript');
    if (transcriptEl) transcriptEl.innerHTML = '';

    document.getElementById('record-btn').disabled = true;
    refreshSidebar();
    _syncUploadBtn();
  } catch (e) {
    alert('Upload failed: ' + e.message);
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

  src.addEventListener('summary_start', e => {
    const d = JSON.parse(e.data);
    const sid = d.session_id || state.sessionId;
    _summaryStreams[sid] = { buffer: '', streaming: true, mode: 'generating' };
    if (sid !== state.sessionId) return;
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
    if (state.summaryCursor) {
      const html = renderMd(_linkifyTimestampsInMd(state.summaryBuffer));
      state.summaryCursor.innerHTML = html;
      if (_summaryAtBottom) state.summaryCursor.scrollTop = state.summaryCursor.scrollHeight;
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
      // Use morphdom to diff-update instead of innerHTML to avoid image flashing
      _morphChatBody(state.chatCursor, state.chatBuffer);
      _ensureTypingCursor(state.chatCursor);
      _chunkArrived();
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
    // Update in-memory cache so re-render is instant, then refresh once
    const entry = _sidebarAllSessions.find(s => s.id === d.session_id);
    if (entry) { entry.title = d.title; }
    // Worker finished for this session → drop its in-flight badge
    _retitleInFlight.delete(d.session_id);
    if (entry) _renderSidebar();
    else refreshSidebar();
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
    // Defensive sweep — clear anything still flagged so a stuck row can't
    // spin forever if a worker crashed before emitting session_title.
    if (_retitleInFlight.size) {
      _retitleInFlight.clear();
      _renderSidebar();
    }
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
      // Clean up notification queue - this speaker is now identified
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
    // Check if a screen recording exists and init video playback
    fetch(`/api/sessions/${state.sessionId}`).then(r => r.json()).then(s => {
      if (s.has_video) initVideo(state.sessionId, s.video_offset);
    }).catch(() => {});
    _syncRecordBtnDisabled();
    _syncUploadBtn();
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
    _syncUploadBtn();
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
  const stop = confirm('Things have gone quiet. Stop this recording?');
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
      refreshSidebar();
      if (_quietPromptLanding === d.session_id) {
        setTimeout(() => showQuietStopConfirm(d.session_id), 150);
        _quietPromptLanding = null;
      }
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
    const pill = dot.parentElement;
    if (state.isReanalyzing) {
      dot.className = 'status-dot recording';
      text.textContent = 'Reanalyzing…';
      pill.removeAttribute('title');
    } else if (!state.recordingReady) {
      dot.className = 'status-dot loading';
      const msg = state.recordingReadyReason || 'Loading transcription model…';
      text.textContent = msg;
      pill.setAttribute('title', msg);
    } else {
      dot.className = 'status-dot ready';
      text.textContent = state.modelInfo || 'Ready';
      pill.removeAttribute('title');
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
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-stop"></i></span> Stop';
    btn.classList.add('recording');
    btn.classList.remove('resuming');
  } else if (state.isViewingPast) {
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-play"></i></span> Resume';
    btn.classList.remove('recording');
    btn.classList.add('resuming');
  } else {
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-play"></i></span> Start';
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
async function toggleRecording() {
  if (state.isRecording) {
    // Immediate visual feedback while the server tears down streams
    const btn = document.getElementById('record-btn');
    btn.innerHTML = '<span class="btn-icon"><i class="fa-solid fa-spinner fa-spin"></i></span> Stopping\u2026';
    btn.style.background = 'var(--yellow)';
    btn.style.color = '#0d1117';
    btn.disabled = true;
    await fetch('/api/recording/stop', { method: 'POST' });
  } else {
    // On the home page, redirect to session page and let it handle the recording
    // start.  This ensures every recording goes through the same audio path.
    if (window._isHomePage) {
      window.location.href = '/session?autostart=1';
      return;
    }

    // Read selected device indices from the dropdowns
    const lbVal  = document.getElementById('viz-loopback-sel')?.value ?? '';
    const micVal = document.getElementById('viz-mic-sel')?.value ?? '';
    const body = {};
    if (lbVal  !== '' && lbVal  !== null && lbVal  !== undefined) body.loopback_device = parseInt(lbVal, 10);
    Object.assign(body, parseMicSelection(micVal));

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

/* ── Voice Library: Match Speakers tab ──────────────────────────────────── */

let _fpMatchGroups = [];
let _fpMatchProfiles = [];

function fpSwitchTab(tab) {
  document.querySelectorAll('.fp-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === tab));
  const profilesTab = document.getElementById('fp-tab-profiles');
  const matchTab = document.getElementById('fp-tab-match');
  const footer = document.getElementById('fp-footer-profiles');
  if (profilesTab) profilesTab.classList.toggle('hidden', tab !== 'profiles');
  if (matchTab) matchTab.classList.toggle('hidden', tab !== 'match');
  if (footer) footer.style.display = tab === 'profiles' ? '' : 'none';
  if (tab === 'match') fpLoadUnlinked();
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
  if (!value) { alert('Select a profile or "Create New".'); return; }

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
    alert('Select at least one profile mapping to apply.');
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

  // Single session — jump directly
  if (sessions.length === 1) {
    closeFingerprintPanel();
    loadSession(sessions[0].session_id);
    return;
  }

  // Multiple sessions — show popup anchored to the name
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
    _tsbEnsureVoiceLibrary();
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

function editSpeakerLabel(badge, speakerKey) {
  // Remove any existing picker first
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

  // Voice Library section - populated asynchronously
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

  // Separate noise group from regular speakers (skip in original-key mode - show all individually)
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

  // Single merged noise pill - all noise groups combined
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

async function copySummary() {
  const el = document.getElementById('summary');
  if (!el || !el.textContent.trim()) return;
  try {
    // Copy as rich text (HTML) so headings/lists/formatting are preserved on paste
    const html = el.innerHTML;
    const plain = el.innerText;
    const blob = new Blob([html], { type: 'text/html' });
    const blobPlain = new Blob([plain], { type: 'text/plain' });
    await navigator.clipboard.write([
      new ClipboardItem({
        'text/html': blob,
        'text/plain': blobPlain,
      }),
    ]);
    // Brief visual feedback on the button
    const btn = el.closest('.col-summary').querySelector('[title="Copy summary"]');
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<i class="fa-solid fa-check"></i>';
      setTimeout(() => { btn.innerHTML = orig; }, 1500);
    }
  } catch {
    // Fallback: plain text
    const plain = el.innerText;
    await navigator.clipboard.writeText(plain);
  }
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
  const show = localStorage.getItem('summary-prompt-open') === '1';
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
      // Drive the video seek directly here rather than waiting for the
      // drift-detection path in _syncVideoToAudio to notice. With the filter
      // active the audio can jump faster than Chrome finishes a video seek,
      // which left _videoSeekPending stuck and produced a "video loops a
      // short snippet" symptom while audio kept skipping forward.
      if (_videoAvailable && _videoVisible) {
        _seekVideoImmediate(_audioToVideoTime(r.start));
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
  // property — offer it in either mode so users can always find the undo.
  const ed = _sessionEditor;
  if (!ed) { btn.classList.add('hidden'); btn.disabled = true; return; }
  const hasTrim  = ed.mode === 'trim' && ed.hasTrimBackup;
  const hasSplit = !!ed.hasSplitBackup;
  btn.classList.toggle('hidden', !(hasTrim || hasSplit));
  btn.disabled = !(hasTrim || hasSplit);
  // Label reflects which restore will be offered. Split wins if both are
  // somehow true (shouldn't normally happen — the original session was
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
    alert(e.message || 'Session edit failed');
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
  // Split rollback takes priority — the original session was deleted at split
  // time, so the only thing to restore is the pre-split snapshot. The split
  // restore has its own modal (lets the user choose which parts to delete).
  if (ed.hasSplitBackup) {
    openSplitRestoreDialog(ed.sessionId);
    return;
  }
  if (!ed.hasTrimBackup) return;
  if (!confirm('Restore the original audio, video, transcript, and speaker labels for this session?')) return;
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
    alert(e.message || 'Restore failed');
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
    alert('Could not load split info: ' + (e.message || e));
    return;
  }
  if (!info.has_backup) {
    alert('No split backup available for this session.');
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
    list.innerHTML = '<p class="empty-hint">No split parts remain — restore will simply recreate the original.</p>';
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
    // Close the session editor if open — the session it was editing may no
    // longer exist (e.g. user checked "delete this part")
    const ed = document.getElementById('session-editor-overlay');
    if (ed && !ed.classList.contains('hidden')) closeSessionEditor();
    await refreshSidebar();
    if (r.restored_session_id) await loadSession(r.restored_session_id);
    flashStatus('Original meeting restored');
  } catch (e) {
    alert('Restore failed: ' + (e.message || e));
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
  _videoSeekPending = false;
  _cancelVideoSeek();
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

// Video seek - cancels any in-flight seek before issuing a new one
let _videoScrubbing = false;    // true while the user is dragging the seek bar
let _videoSeekDebounce = 0;     // timeout id for debounced seek during scrub
let _videoSeekPending = false;  // true between currentTime= and 'seeked' event

function _cancelVideoSeek() {
  clearTimeout(_videoSeekDebounce);
  _videoSeekDebounce = 0;
  // The next currentTime assignment naturally supersedes any in-flight seek
  // — we don't need to do anything explicit here.
}

function _seekVideoImmediate(targetTime) {
  _cancelVideoSeek();
  // Skip no-op seeks: if we set currentTime to its current value, the
  // browser doesn't fire 'seeked', and our pending flag would stick.
  if (Math.abs(_playbackVideo.currentTime - targetTime) < 0.01) return;
  _videoSeekPending = true;
  _playbackVideo.currentTime = targetTime;
}

function _seekVideoDebounced(targetTime, delayMs) {
  _cancelVideoSeek();
  _videoSeekDebounce = setTimeout(() => {
    if (Math.abs(_playbackVideo.currentTime - targetTime) < 0.01) return;
    _videoSeekPending = true;
    _playbackVideo.currentTime = targetTime;
  }, delayMs);
}

// Single persistent 'seeked' listener — clears the pending flag and resumes
// playback after the decoder has actually landed on the new frame. This is
// the fix for the preview-freeze: calling .play() immediately after setting
// currentTime can land Chrome on a non-keyframe and freeze the rendered
// image even though currentTime reports the new value.
// Home page has no playback-video element, so guard the listener attach.
if (_playbackVideo) {
  _playbackVideo.addEventListener('seeked', () => {
    _videoSeekPending = false;
    if (_videoScrubbing) return;  // scrub logic manages play state itself
    if (!_videoAvailable || !_videoVisible) return;
    if (!_playbackAudio.paused && _playbackVideo.paused) {
      _playbackVideo.play().catch(() => {});
    }
  });
}

function _syncVideoToAudio() {
  if (!_videoAvailable || !_videoVisible) return;
  // Don't fight an in-flight seek — issuing a new currentTime mid-seek can
  // chain decode requests faster than Chrome can deliver frames, freezing
  // the preview on a stale frame.
  if (_videoSeekPending) return;
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
        // No video - just resume audio
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
    // The 'seeked' listener resumes play if _syncVideoToAudio kicked off
    // a seek; only start now if nothing is pending.
    if (!_videoSeekPending) _playbackVideo.play().catch(() => {});
  }
};

const _origSeekPlayback = seekPlayback;
seekPlayback = function(val) {
  _origSeekPlayback(val);
  if (_videoAvailable) {
    if (_videoScrubbing) {
      // During scrub: debounce - only seek after user pauses dragging for 100ms
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
    _seekVideoImmediate(_audioToVideoTime(t));
    // Play resumption is handled by the persistent 'seeked' listener so the
    // video isn't told to play() while still seeking — that race is what
    // freezes the preview on a stale frame after segment clicks.
    if (_videoVisible && _playbackAudio.paused && !_playbackVideo.paused) {
      _playbackVideo.pause();
    }
  }
};

const _origSetPlaybackSpeed = setPlaybackSpeed;
setPlaybackSpeed = function(val) {
  _origSetPlaybackSpeed(val);
  if (_videoAvailable) _playbackVideo.playbackRate = parseFloat(val);
};

// Periodic drift correction - runs on audio's timeupdate
_playbackAudio.addEventListener('timeupdate', () => {
  if (_videoAvailable && _videoVisible && !_playbackAudio.paused && !_videoScrubbing) {
    _syncVideoToAudio();
    // Keep play state in sync (filter skipping can pause/seek audio).
    // Skip while a seek is in flight — the 'seeked' listener will resume.
    if (!_videoSeekPending && _playbackVideo.paused) {
      _playbackVideo.play().catch(() => {});
    }
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
  if (!_videoAvailable || !_videoVisible) return;
  _syncVideoToAudio();
  // If a seek is in flight, leave the play() call to the 'seeked' listener
  // — calling .play() during a pending seek can lock the decoder onto a
  // non-keyframe and freeze the preview.
  if (!_videoSeekPending) _playbackVideo.play().catch(() => {});
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

  // Auto-expand while tools are in progress, preserve manual toggle otherwise.
  // Keep 'streaming' even after all tools complete - it's only removed on
  // first chat_chunk so the collapse fires at the right time.
  if (!allDone) {
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
    web_search: 'Web Search',
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
  if (name === 'get_speaker_history' && input?.speaker_name) return `"${input.speaker_name}"`;
  if (name === 'web_search' && input?.query) return `"${input.query}"`;
  if (name === 'web_search') return 'searching…';
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

/* ── Chat system prompt (global default + per-session override) ───────────── */
let _builtinChatPrompt = '';        // fetched once; used for "Load built-in"
let _sessionChatPrompt = null;      // current session's override (null when none)
let _globalChatPromptSaveTimer = 0;

async function _fetchBuiltinChatPrompt() {
  if (_builtinChatPrompt) return _builtinChatPrompt;
  try {
    const r = await fetch('/api/chat/default-prompt').then(r => r.json());
    _builtinChatPrompt = r.prompt || '';
  } catch { _builtinChatPrompt = ''; }
  return _builtinChatPrompt;
}

function _syncGlobalChatPromptUI() {
  const ta  = document.getElementById('global-chat-prompt');
  const st  = document.getElementById('global-chat-prompt-status');
  if (!ta) return;
  const val = (_prefs.chat_system_prompt || '').trim();
  if (ta.value !== (_prefs.chat_system_prompt || '')) {
    ta.value = _prefs.chat_system_prompt || '';
  }
  if (st) {
    if (val) { st.textContent = 'Custom global prompt active'; st.classList.add('custom'); }
    else     { st.textContent = 'Using built-in default';     st.classList.remove('custom'); }
  }
}

function _globalChatPromptChanged(value) {
  _prefs.chat_system_prompt = value;
  clearTimeout(_globalChatPromptSaveTimer);
  _globalChatPromptSaveTimer = setTimeout(() => {
    savePref('chat_system_prompt', value);
    _syncGlobalChatPromptUI();
  }, 500);
}

function resetGlobalChatPrompt() {
  const ta = document.getElementById('global-chat-prompt');
  if (ta) ta.value = '';
  _prefs.chat_system_prompt = '';
  savePref('chat_system_prompt', '');
  _syncGlobalChatPromptUI();
}

async function loadBuiltinIntoGlobal() {
  const text = await _fetchBuiltinChatPrompt();
  const ta = document.getElementById('global-chat-prompt');
  if (!ta) return;
  ta.value = text;
  _globalChatPromptChanged(text);
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
  // On the home page, navigate to the session page instead of loading inline
  if (window._isHomePage) {
    window.location.href = `/session?id=${sessionId}`;
    return;
  }
  if (sessionId === state.sessionId) return;

  if (state.isRecording) {
    if (!confirm('Stop the current recording and load this session?')) return;
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
  history.pushState({}, '', '/session?id=' + sessionId);
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
    // Small transcript - render synchronously (fast enough)
    segments.forEach(s =>
      appendTranscript(s.text, s.source_override || s.source || 'loopback', s.start_time, s.end_time,
                       s.id, s.label_override, s.source_override ? s.source : null)
    );
  }

  // Handle pending search highlight - scroll to and flash the matching segment
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
/* ── Power menu ────────────────────────────────────────────────────────── */

function togglePowerMenu() {
  const menu = document.getElementById('power-menu');
  menu.classList.toggle('hidden');
  if (!menu.classList.contains('hidden')) {
    // Close on outside click
    setTimeout(() => {
      document.addEventListener('click', _closePowerMenuOutside, { once: true });
    }, 0);
  }
}
function closePowerMenu() {
  document.getElementById('power-menu')?.classList.add('hidden');
}
function _closePowerMenuOutside(e) {
  const wrap = document.querySelector('.power-menu-wrap');
  if (wrap && !wrap.contains(e.target)) closePowerMenu();
  else if (!document.getElementById('power-menu')?.classList.contains('hidden')) {
    document.addEventListener('click', _closePowerMenuOutside, { once: true });
  }
}

function confirmShutdown() {
  if (!state.isRecording) { doShutdown(); return; }
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.innerHTML = `
    <div class="dialog">
      <h3>Shut down server?</h3>
      <p>A recording is in progress. This will stop it and close the Meeting Assistant server.</p>
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
  const screen = _showTransitionScreen('Shut Down', 'You can close this tab.');
  // Freeze the animation after a moment for a calm stopped state
  setTimeout(() => screen.stop(), 3000);
}

function confirmRestart() {
  if (!state.isRecording) { doRestart(); return; }
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.innerHTML = `
    <div class="dialog">
      <h3>Restart server?</h3>
      <p>A recording is in progress. This will stop it and restart the Meeting Assistant.</p>
      <div class="dialog-btns">
        <button class="btn" style="background:var(--accent);color:#fff" onclick="doRestart()">Restart</button>
        <button class="btn" style="background:var(--surface2);color:var(--fg)"
                onclick="this.closest('.overlay').remove()">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
}

function confirmUpdateRestart() {
  if (!state.isRecording) { doUpdateRestart(); return; }
  const overlay = document.createElement('div');
  overlay.className = 'overlay';
  overlay.innerHTML = `
    <div class="dialog">
      <h3>Update &amp; Restart?</h3>
      <p>A recording is in progress. This will stop it, pull the latest update, and restart.</p>
      <div class="dialog-btns">
        <button class="btn" style="background:var(--accent);color:#fff" onclick="doUpdateRestart()">Update &amp; Restart</button>
        <button class="btn" style="background:var(--surface2);color:var(--fg)"
                onclick="this.closest('.overlay').remove()">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
}

async function doUpdateRestart() {
  document.querySelector('.overlay')?.remove();
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
  const poll = setInterval(async () => {
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
  document.querySelector('.overlay')?.remove();
  await fetch('/api/restart', { method: 'POST' }).catch(() => {});
  _showTransitionScreen('Restarting\u2026', 'The page will reload when the server is back.');
  const poll = setInterval(async () => {
    try {
      const r = await fetch('/api/status', { signal: AbortSignal.timeout(2000) });
      if (r.ok) { clearInterval(poll); location.reload(); }
    } catch {}
  }, 2000);
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
  state.summaryCursor    = null;
  document.getElementById('summary-badge')?.classList.add('hidden');
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

/* ── Audio device selection ──────────────────────────────────────────────── */
async function loadAudioDevices() {
  const lbSel  = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  if (!lbSel || !micSel) return;

  // Saved choices from server prefs (with localStorage fallback for migration)
  const savedLb  = _prefs.loopback_device ?? localStorage.getItem('viz-loopback-idx') ?? '';
  const savedMic = _prefs.mic_device      ?? localStorage.getItem('viz-mic-idx')      ?? '';

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
    // Legacy saved value (WASAPI index or browser mic "-2") — try to match by
    // device name.  WASAPI and dshow names for the same physical mic are usually
    // identical, so find the WASAPI name from data.input and look for a matching
    // ffmpeg option.
    let legacyName = null;
    if (savedMic === '-2') {
      // Browser mic has no name to match — just fall through to first dshow device
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

  // Persist the resolved selection so pages without dropdowns (e.g. home page)
  // can send the same device IDs when starting a recording.
  if (lbSel.value && !_prefs.loopback_device) savePref('loopback_device', lbSel.value);
  if (micSel.value && !_prefs.mic_device)     savePref('mic_device',      micSel.value);
}

function saveDeviceSelection() {
  const lbSel  = document.getElementById('viz-loopback-sel');
  const micSel = document.getElementById('viz-mic-sel');
  if (lbSel)  savePref('loopback_device', lbSel.value);
  if (micSel) savePref('mic_device',      micSel.value);
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
    const lbVal  = document.getElementById('viz-loopback-sel')?.value;
    const micVal = document.getElementById('viz-mic-sel')?.value;
    const body   = {};
    if (lbVal  !== '' && lbVal  != null) body.loopback_device = parseInt(lbVal,  10);
    Object.assign(body, parseMicSelection(micVal));

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
      alert(data.error || 'Auto-detect failed');
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
    alert('Auto-detect failed: ' + e.message);
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
  const renderCol = (id, label, cssClass, enabled, gain, env, gated, target) => {
    const col = document.getElementById(id);
    if (!col) return;
    if (!enabled) {
      col.innerHTML = `<div class="agc-src ${cssClass}">${label}</div><div class="agc-idle">disabled</div>`;
      return;
    }
    const status = gated ? '<span class="agc-gated">GATED</span>'
                  : gain > 1.01 ? `<span class="agc-boosting">BOOST ${gain.toFixed(1)}\u00d7</span>`
                  : '<span class="agc-idle">1.0\u00d7</span>';
    col.innerHTML = `<div class="agc-src ${cssClass}">${label}</div>`
      + `<div class="agc-val"><span class="agc-lbl">Status</span> ${status}</div>`
      + `<div class="agc-val"><span class="agc-lbl">Gain</span> ${gain.toFixed(2)}\u00d7</div>`
      + `<div class="agc-val"><span class="agc-lbl">Env</span> ${fmt(env)}</div>`
      + `<div class="agc-val"><span class="agc-lbl">Target</span> ${fmt(target)}</div>`;
  };
  renderCol('agc-debug-lb',  'Desktop', 'lb',  agc.lb_enabled,  agc.lb_gain,  agc.lb_env,  agc.lb_gated,  agc.target);
  renderCol('agc-debug-mic', 'Mic',     'mic', agc.mic_enabled, agc.mic_gain, agc.mic_env, agc.mic_gated, agc.target);
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
let vizLbSpec  = [];   // frequency spectrum from server (N_BARS values, 0–1)
let vizMicSpec = [];
// Smoothed per-band values for animation (fast attack, slow decay)
const vizLbBars  = new Float32Array(N_BARS);
const vizMicBars = new Float32Array(N_BARS);

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
      // Subtle lighten toward tip — ~25% shift, not full white
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

// Fallback model lists — used only if the backend hasn't returned its live
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
    // descendants — that shifts our coordinates and the panel lands in the
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
  // near the left edge — e.g. Global Chat on the home page — where naive
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
  // Show the "Update & Restart" option in the power menu
  const pmUpdate = document.getElementById('power-menu-update');
  if (pmUpdate) pmUpdate.classList.remove('hidden');
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
    btn.title = 'Update failed - click to retry';
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

// ── Data folder ──────────────────────────────────────────────────────────

async function loadDataFolder() {
  const pathEl = document.getElementById('data-folder-current');
  const resetBtn = document.getElementById('data-folder-reset-btn');
  if (!pathEl) return;
  try {
    const info = await fetch('/api/data_folder').then(r => r.json());
    pathEl.textContent = info.current;
    pathEl.title = info.overridden
      ? `Overridden — default is ${info.default}`
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
    const ok = window.confirm(
      `Move data folder to:\n\n${res.selected}\n\n` +
      `This will copy every recording, database, and setting to the new ` +
      `location and switch over. The original folder is kept as a backup ` +
      `until you delete it manually.\n\n` +
      `The app will need a restart afterwards. Continue?`
    );
    if (!ok) return;
    btn.textContent = 'Migrating…';
    const out = await fetch('/api/data_folder/migrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ destination: res.selected }),
    }).then(r => r.json());
    if (out.error) {
      window.alert(`Migration failed:\n\n${out.error}`);
      return;
    }
    const mb = (out.bytes_copied / 1024 / 1024).toFixed(1);
    window.alert(
      `Data folder migrated.\n\n` +
      `${out.files_copied} files + ${out.dbs_copied} databases (${mb} MB)\n\n` +
      `Please close and reopen the app for the change to take full effect. ` +
      `The original folder is preserved at:\n${out.src}`
    );
    loadDataFolder();
  } catch (e) {
    window.alert(`Error: ${e.message || e}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function resetDataFolder() {
  const ok = window.confirm(
    `Revert to the default data folder?\n\n` +
    `This only changes which folder the app reads from on next startup — ` +
    `it does NOT move files. If you want your current data at the default ` +
    `location, copy it there manually first.\n\n` +
    `The app will need a restart afterwards. Continue?`
  );
  if (!ok) return;
  try {
    await fetch('/api/data_folder/reset', { method: 'POST' }).then(r => r.json());
    window.alert('Reverted to default data folder. Please close and reopen the app.');
    loadDataFolder();
  } catch (e) {
    window.alert(`Error: ${e.message || e}`);
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
    const isDisabled = (toggleKeys.length > 0 && !isToggle && (toggleInverted ? anyToggleOn : !anyToggleOn));

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

/* ── Import / Export ──────────────────────────────────────────────────────── */

let _exportSessionId = null;

const _EXPORT_STEP_LABELS = {
  metadata:           'Session metadata',
  transcription:      'Transcription',
  summary:            'Summary',
  chat:               'Chat & screenshots',
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
  ['metadata', 'transcription', 'summary', 'chat', 'speakers', 'speaker_embeddings', 'audio', 'video']
    .forEach(cat => {
      const cb = document.getElementById('export-opt-' + cat);
      if (cb && cb.checked) cats.push(cat);
    });

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
    statusEl.textContent = `Complete — ${sizeMB} MB`;
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

    // Refresh sidebar and navigate to the imported session
    refreshSidebar();
    if (data.session_id) {
      setTimeout(() => {
        if (_isHomePage) {
          window.location.href = '/session?id=' + data.session_id;
        } else {
          history.pushState({}, '', '/session?id=' + data.session_id);
          loadSession(data.session_id);
        }
      }, 600);
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

// Import drag-and-drop init
_initImportDragDrop();

// Shared init (sidebar, SSE, status, devices, models)
connectSSE();

// Close SSE on page unload to prevent connection leaks when navigating
window.addEventListener('beforeunload', () => {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }
});

refreshSidebar();
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
let _devicesReady = loadPreferences().then(() => {
  loadModelConfig();
  return loadAudioDevices();
});
// Screen recording: load displays + sync toggle
_apLoad().then(() => { try { _syncScreenToggle(); } catch {} });
try { loadScreenDisplays(); } catch {}

_startPeriodicUpdateCheck();

if (!_isHomePage) {
  loadSummaryPrompt();

  // Auto-open settings if ?settings=1 or ?setup=1 is in the URL
  // Auto-load session if ?session=<id> is in the URL
  {
    const params = new URLSearchParams(location.search);
    if (params.has('quiet_prompt')) _quietPromptLanding = params.get('id');
    if (params.has('settings') || params.has('setup')) {
      openSettings();
      const section = params.get('section');
      if (section) {
        const navBtn = document.querySelector(`.settings-nav-item[data-target="section-${section}"]`);
        if (navBtn) switchSettingsSection(navBtn);
      }
      history.replaceState(null, '', location.pathname);
    } else if (params.has('fingerprint')) {
      openFingerprintPanel();
      history.replaceState(null, '', location.pathname);
    } else if (params.has('autostart')) {
      // Auto-start recording once the model is ready.  Used by the home page
      // and system tray so every recording goes through the session page's
      // proven audio path.
      history.replaceState(null, '', '/session');
      _waitForRecordReady().then(() => {
        if (!state.isRecording) toggleRecording();
      });
    } else if (params.has('id')) {
      // Defer until status has loaded - if the session is actively recording,
      // the SSE status+replay events handle everything; only call loadSession
      // for past (non-recording) sessions.
      const _pendingSessionId = params.get('id');
      fetch('/api/status').then(r => r.json()).then(st => {
        if (st.recording && st.session_id === _pendingSessionId) {
          // Active recording - SSE status event will set state; don't call loadSession
          if (_quietPromptLanding === _pendingSessionId) {
            setTimeout(() => showQuietStopConfirm(_pendingSessionId), 250);
            _quietPromptLanding = null;
          }
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
      _updateActiveFolderHighlights();
    }
  });

  // Initial sync of upload button visibility
  _syncUploadBtn();
}
