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
  const HANDLE_PX = 4;
  const MIN_COL_PX = 160;
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;
  const handles = workspace.querySelectorAll('.col-resize-handle').length;
  const total = workspace.offsetWidth - HANDLE_PX * handles;
  const fracSum = _colProportions.reduce((a, b) => a + b, 0);
  const widths = _colProportions.map(f => Math.max(MIN_COL_PX, Math.round(total * f / fracSum)));
  const template = widths.map((w, i) =>
    i < widths.length - 1 ? `${w}px ${HANDLE_PX}px` : `${w}px`
  ).join(' ');
  workspace.style.gridTemplateColumns = template;
}

(function initResizableCols() {
  const HANDLE_PX  = 4;
  const MIN_COL_PX = 160;

  const workspace = document.querySelector('.workspace');
  const handles   = Array.from(workspace.querySelectorAll('.col-resize-handle'));
  const numCols   = workspace.querySelectorAll('.col').length;
  if (!numCols || !handles.length) return;

  function getPixelWidths() {
    const total   = workspace.offsetWidth - HANDLE_PX * handles.length;
    const fracSum = _colProportions.reduce((a, b) => a + b, 0);
    return _colProportions.map(f => Math.max(MIN_COL_PX, Math.round(total * f / fracSum)));
  }

  function applyWidths(widths) {
    const template = widths.map((w, i) =>
      i < widths.length - 1 ? `${w}px ${HANDLE_PX}px` : `${w}px`
    ).join(' ');
    workspace.style.gridTemplateColumns = template;
  }

  applyWidths(getPixelWidths());

  handles.forEach(handle => {
    handle.addEventListener('mousedown', e => {
      e.preventDefault();
      const li = parseInt(handle.dataset.left,  10);
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
        const total = widths.reduce((a, b) => a + b, 0);
        _colProportions = widths.map(w => w / total);
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
  modelInfo:      '',
  chatCursor:     null,
  chatBuffer:     '',
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
  if (_prefs.sidebar_width)                      cacheUpdate.sidebar_width   = _prefs.sidebar_width;
  if (typeof _prefs.sidebar_open === 'boolean')  cacheUpdate.sidebar_open    = _prefs.sidebar_open;
  if (Object.keys(cacheUpdate).length) _saveLayoutCache(cacheUpdate);

  // Apply sidebar width (server value may differ from cached, e.g. on another device)
  if (_prefs.sidebar_width) {
    const sb = document.getElementById('sidebar');
    if (sb && state.sidebarOpen) sb.style.width = _prefs.sidebar_width + 'px';
  }
  // Apply column proportions, then reflow
  if (Array.isArray(_prefs.col_proportions) && _prefs.col_proportions.length === 3) {
    _colProportions = _prefs.col_proportions;
  }
  recalcColWidths();
  // Apply sidebar collapsed state — only toggle if it disagrees with what cache already set
  if (_prefs.sidebar_open === false && state.sidebarOpen) {
    state.sidebarOpen = true;   // toggleSidebar flips it
    toggleSidebar();
  } else if (_prefs.sidebar_open !== false && !state.sidebarOpen) {
    state.sidebarOpen = false;  // toggleSidebar flips it
    toggleSidebar();
  }
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
  // Reflow workspace columns after the sidebar transition completes
  sidebar.addEventListener('transitionend', recalcColWidths, { once: true });
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

async function refreshSidebar() {
  const [sessions, folders] = await Promise.all([
    fetch('/api/sessions').then(r => r.json()),
    fetch('/api/folders').then(r => r.json()).catch(() => []),
  ]);
  _sidebarAllSessions = sessions;
  _sidebarFolders = folders;
  _renderSidebar();
}

function _renderSidebar() {
  const sessions = _sidebarAllSessions;
  const folders  = _sidebarFolders;
  const list     = document.getElementById('session-list');
  const hasAny   = sessions.length > 0;

  if (!hasAny) {
    list.innerHTML = '<p class="sidebar-empty">No past sessions yet.</p>';
    _updateBulkBar();
    return;
  }

  const fragment = document.createDocumentFragment();

  // ── Folder sections ───────────────────────────────────────────────────────
  for (const folder of folders) {
    const folderSessions = sessions.filter(s => s.folder_id === folder.id);
    const collapsed = _sidebarCollapsed.has(folder.id);

    const folderEl = document.createElement('div');
    folderEl.className = 'sidebar-folder';
    folderEl.dataset.folderId = folder.id;

    // Drop target behavior
    folderEl.addEventListener('dragover', e => { e.preventDefault(); folderEl.classList.add('drag-over'); });
    folderEl.addEventListener('dragleave', e => { if (!folderEl.contains(e.relatedTarget)) folderEl.classList.remove('drag-over'); });
    folderEl.addEventListener('drop', e => { e.preventDefault(); folderEl.classList.remove('drag-over'); _dropIntoFolder(folder.id); });

    // Folder header
    const header = document.createElement('div');
    header.className = 'folder-header';
    header.innerHTML = `
      <button class="folder-toggle" onclick="_toggleFolder('${folder.id}')"><i class="fa-solid fa-chevron-${collapsed ? 'right' : 'down'}"></i></button>
      <span class="folder-icon"><i class="fa-solid fa-folder"></i></span>
      <span class="folder-name" onclick="_toggleFolder('${folder.id}')">${escapeHtml(folder.name)}</span>
      <span class="folder-count">${folderSessions.length}</span>`;

    // ⋮ context menu button for folder
    const folderMenuBtn = document.createElement('button');
    folderMenuBtn.className = 'folder-menu-btn';
    folderMenuBtn.title = 'More options';
    folderMenuBtn.innerHTML = '<i class="fa-solid fa-ellipsis-vertical"></i>';
    folderMenuBtn.addEventListener('click', e => { e.stopPropagation(); _openFolderMenu(e, folder); });
    header.appendChild(folderMenuBtn);
    folderEl.appendChild(header);

    if (!collapsed) {
      const body = document.createElement('div');
      body.className = 'folder-body';
      if (folderSessions.length === 0) {
        body.innerHTML = '<div class="folder-empty">Drop sessions here</div>';
      } else {
        folderSessions.forEach(s => body.appendChild(_makeSessionEl(s)));
      }
      folderEl.appendChild(body);
    }

    fragment.appendChild(folderEl);
  }

  // ── Ungrouped sessions by date ────────────────────────────────────────────
  const ungrouped = sessions.filter(s => !s.folder_id || !folders.find(f => f.id === s.folder_id));
  if (ungrouped.length) {
    const groups = groupByDate(ungrouped);
    for (const [label, items] of groups) {
      const groupEl = document.createElement('div');
      groupEl.className = 'session-group';
      groupEl.textContent = label;
      fragment.appendChild(groupEl);
      items.forEach(s => fragment.appendChild(_makeSessionEl(s)));
    }
  }

  list.innerHTML = '';
  list.appendChild(fragment);
  _updateBulkBar();
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
    _sidebarDragIds = isSelected && _sidebarSelected.size > 1
      ? [..._sidebarSelected]
      : [s.id];
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', JSON.stringify(_sidebarDragIds));
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => el.classList.remove('dragging'));

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

  // Checkbox — only visible when sidebar is in multiselect mode (CSS-controlled)
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

  // ⋮ context menu button — replaces the individual reanalyze/rename/delete buttons
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

async function deleteFolder(e, folderId) {
  e.stopPropagation();
  const folder = _sidebarFolders.find(f => f.id === folderId);
  if (!confirm(`Delete folder "${folder?.name || folderId}"? Sessions will be uncategorized.`)) return;
  await fetch(`/api/folders/${folderId}`, { method: 'DELETE' });
  _sidebarCollapsed.delete(folderId);
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

function _dropIntoFolder(folderId) {
  const ids = _sidebarDragIds.length ? _sidebarDragIds : [];
  if (!ids.length) return;
  fetch('/api/sessions/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'move', session_ids: ids, folder_id: folderId }),
  }).then(() => {
    _sidebarSelected.clear();
    refreshSidebar();
  });
}

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
  if (ids.includes(state.sessionId) && !state.isRecording) {
    state.sessionId = null; state.isViewingPast = false;
    clearAll(); history.pushState({}, '', '/');
  }
  _sidebarSelected.clear();
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
  const time  = start.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  if (!s.ended_at) return `${time} · In progress`;
  const end  = new Date(s.ended_at + 'Z');
  const mins = Math.round((end - start) / 60000);
  return `${time} · ${mins < 1 ? '<1' : mins} min`;
}

async function deleteSession(e, sessionId) {
  e.stopPropagation();
  await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
  if (sessionId === state.sessionId && !state.isRecording) {
    state.sessionId = null;
    clearAll();
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
  history.pushState({}, '', '/');
  _applyPromptText('');
  updateRecordBtn();
  const btn = document.getElementById('record-btn');
  // Only re-enable if model is ready (status-dot will be 'ready')
  if (!document.getElementById('status-dot').classList.contains('ready')) {
    btn.disabled = true;
  }
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
      appendTranscript(s.text, s.source || 'loopback', s.start_time, s.end_time, s.id, s.label_override);
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
      state.summaryCursor.scrollTop = state.summaryCursor.scrollHeight;
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
    state.chatCursor  = createAssistantBubble();
  });

  src.addEventListener('chat_chunk', e => {
    state.chatBuffer += JSON.parse(e.data).text;
    if (state.chatCursor) {
      state.chatCursor.innerHTML = renderMd(state.chatBuffer);
      state.chatCursor.classList.add('typing-cursor');
      scrollChatToBottom();
    }
  });

  src.addEventListener('chat_done', () => {
    if (state.chatCursor) {
      linkifyTimestamps(state.chatCursor);
      state.chatCursor.classList.remove('typing-cursor');
      state.chatCursor = null;
    }
    highlightCode('#chat-messages');
    state.aiChatBusy = false;
    setSendBusy(false);
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
    }
  });

  src.addEventListener('speaker_linked', e => {
    const d = JSON.parse(e.data);
    if (d.session_id === state.sessionId) {
      _sessionLinks[d.speaker_key] = { global_id: d.global_id, name: d.name };
      _updateLinkedBadges();
    }
  });

  src.addEventListener('transcript_reset', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    document.getElementById('transcript').innerHTML =
      '<p class="empty-hint">Reanalyzing audio…</p>';
    document.getElementById('summary').innerHTML =
      '<p class="empty-hint">Summary will regenerate after reanalysis completes.</p>';
    document.getElementById('chat-messages').innerHTML =
      '<p class="empty-hint">Ask questions about the meeting here.</p>';
    destroyPlayback();
  });

  src.addEventListener('reanalysis_start', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    state.isReanalyzing = true;
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className    = 'status-dot recording';
    text.textContent = 'Reanalyzing…';
    refreshSidebar();
  });

  src.addEventListener('reanalysis_done', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    state.isReanalyzing  = false;
    state.sessionHasAudio = true;
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className    = 'status-dot ready';
    text.textContent = state.modelInfo || 'Ready';
    initPlayback(state.sessionId);
    refreshSidebar();
  });

  src.addEventListener('reanalysis_error', e => {
    const d = JSON.parse(e.data);
    if (d.session_id !== state.sessionId) return;
    state.isReanalyzing = false;
    const dot  = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    dot.className    = 'status-dot ready';
    text.textContent = state.modelInfo || 'Ready';
    alert('Reanalysis failed: ' + (d.error || 'unknown error'));
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
function onStatus(d) {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  const btn  = document.getElementById('record-btn');

  if (d.model_ready === false || (!d.model_ready && !state.modelInfo)) {
    dot.className   = 'status-dot loading';
    text.textContent = 'Loading model…';
    btn.disabled    = true;
    return;
  }

  if (d.model_info) state.modelInfo = d.model_info;

  if (d.model_ready === true) {
    if (!state.isRecording) {
      dot.className    = 'status-dot ready';
      text.textContent = state.modelInfo;
    }
    btn.disabled = false;
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
      history.replaceState({}, '', '?session=' + d.session_id);
      state.sessionId     = d.session_id;
      state.isViewingPast = false;
      dot.className       = 'status-dot recording';
      text.textContent    = 'Recording…';
      btn.disabled        = false;
      destroyPlayback();
      if (!_durationInterval) {
        startDurationCounter();
        // Push stored gain values now — AudioCapture is guaranteed to exist
        initGainSliders();
      }
      _updateBrandIcons(true);
      if (_pendingSpeakerProfiles.length) _flushPendingSpeakers(d.session_id);
    } else if (!d.recording) {
      stopDurationCounter();
      dot.className    = 'status-dot ready';
      text.textContent = state.modelInfo || 'Ready';
      _updateBrandIcons(false);
      refreshSidebar();
      // The WAV is finalized before this event fires, so playback is available
      // immediately - no need to reload the page or click the session.
      if (!state.isViewingPast && state.sessionId) {
        initPlayback(state.sessionId);
      }
    }
  }
}

function updateRecordBtn() {
  const btn = document.getElementById('record-btn');
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
let _manualNoiseKeys = new Set(); // speaker_keys manually marked as noise
let _navState = { matches: [], currentIdx: -1 };

// Set while the speaker picker dropdown is open - suppresses auto-scroll
// so the transcript doesn't jump away while the user is typing a name.
let _pickerOpen = false;

// speaker_key → display name for the session currently in view
let _speakerLabels = {};

// speaker_key → accent color (CSS color string), auto-assigned on first appearance
const _speakerColors = {};
let _speakerProfiles = {};
let _lastLiveSegId   = 0;   // highest seg_id received from live transcript events
let _sseSource       = null;
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
  '#3fb950', // green
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
  document.querySelectorAll('.src-speaker').forEach(badge => {
    if (badge.dataset.speakerKey === speakerKey) count++;
  });
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
  document.querySelectorAll('.src-speaker').forEach(badge => keys.add(badge.dataset.speakerKey));

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
  document.querySelectorAll('.src-speaker').forEach(badge => {
    badge.classList.toggle('speaker-selected', selected.has(badge.dataset.speakerKey));
  });
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

let _fpToastQueue = [];          // pending {session_id, speaker_key, current_name, matches}
let _fpToastActive = null;       // currently displayed toast data
let _fpToastTimer  = null;

function _fpEnqueueToast(data) {
  // Skip if the top match is the speaker's current name (already applied)
  const top = data.matches && data.matches[0];
  if (top && data.current_name && top.name === data.current_name) return;
  // Replace any existing entry for the same speaker_key in the queue
  _fpToastQueue = _fpToastQueue.filter(d => d.speaker_key !== data.speaker_key);
  _fpToastQueue.push(data);
  if (!_fpToastActive) _fpShowNextToast();
}

function _fpShowNextToast() {
  if (!_fpToastQueue.length) return;
  _fpToastActive = _fpToastQueue.shift();
  const toast    = document.getElementById('fp-match-toast');
  const top      = _fpToastActive.matches[0];

  document.getElementById('fp-toast-label').innerHTML =
    `${_fpToastActive.current_name || _fpToastActive.speaker_key} sounds like <strong id="fp-toast-name">${top.name}</strong>`;
  document.getElementById('fp-toast-sim').textContent = `${Math.round(top.similarity * 100)}%`;

  // Populate "Not this" list
  const otherList = document.getElementById('fp-toast-other-list');
  otherList.innerHTML = '';
  otherList.classList.add('hidden');
  const others = _fpToastActive.matches.slice(1);
  if (others.length) {
    others.forEach(m => {
      const btn = document.createElement('button');
      btn.className = 'fp-toast-opt';
      btn.textContent = `${m.name} (${Math.round(m.similarity * 100)}%)`;
      btn.addEventListener('mousedown', e => {
        e.preventDefault();
        _fpConfirm(_fpToastActive, m.global_id);
      });
      otherList.appendChild(btn);
    });
  } else {
    document.getElementById('fp-toast-other').style.display = 'none';
  }

  toast.classList.remove('hidden');

  if (_fpToastTimer) clearTimeout(_fpToastTimer);
  _fpToastTimer = setTimeout(() => fpToastSkip(), 12000);
}

function fpToastApply() {
  if (!_fpToastActive) return;
  const top = _fpToastActive.matches[0];
  _fpConfirm(_fpToastActive, top.global_id);
}

function fpToastToggleOther() {
  document.getElementById('fp-toast-other-list').classList.toggle('hidden');
}

function fpToastSkip() {
  if (!_fpToastActive) return;
  _fpDismiss(_fpToastActive);
  _fpToastActive = null;
  document.getElementById('fp-match-toast').classList.add('hidden');
  document.getElementById('fp-toast-other-list').classList.add('hidden');
  if (_fpToastTimer) { clearTimeout(_fpToastTimer); _fpToastTimer = null; }
  setTimeout(() => _fpShowNextToast(), 400);
}

function _fpHideToast() {
  _fpToastActive = null;
  document.getElementById('fp-match-toast').classList.add('hidden');
  document.getElementById('fp-toast-other-list').classList.add('hidden');
  if (_fpToastTimer) { clearTimeout(_fpToastTimer); _fpToastTimer = null; }
  setTimeout(() => _fpShowNextToast(), 400);
}

async function _fpConfirm(toastData, globalId) {
  _fpHideToast();
  try {
    await fetch('/api/fingerprint/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id:  toastData.session_id,
        speaker_key: toastData.speaker_key,
        global_id:   globalId,
      }),
    });
  } catch (e) { console.warn('fp confirm failed', e); }
}

async function _fpDismiss(toastData) {
  try {
    await fetch('/api/fingerprint/dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id:  toastData.session_id,
        speaker_key: toastData.speaker_key,
        global_id:   toastData.matches[0]?.global_id || '',
      }),
    });
  } catch (e) { console.warn('fp dismiss failed', e); }
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
    const allSegs = [...document.querySelectorAll('#transcript .transcript-segment')];
    const fromIdx = allSegs.indexOf(_transcriptSelectionAnchor);
    const toIdx   = allSegs.indexOf(segEl);
    if (fromIdx !== -1 && toIdx !== -1) {
      const [start, end] = fromIdx <= toIdx ? [fromIdx, toIdx] : [toIdx, fromIdx];
      allSegs.slice(start, end + 1).forEach(el => _transcriptSelectedSegs.add(el));
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
  document.querySelectorAll('#transcript .transcript-segment').forEach(seg => {
    seg.classList.toggle('transcript-seg-selected', _transcriptSelectedSegs.has(seg));
  });
  const bar = document.getElementById('transcript-selection-bar');
  if (!bar) return;
  const count = _transcriptSelectedSegs.size;
  if (count > 0) {
    bar.classList.remove('hidden');
    const countEl = document.getElementById('tsb-count');
    if (countEl) countEl.textContent = `${count} segment${count === 1 ? '' : 's'} selected`;
    const dl = document.getElementById('tsb-datalist');
    if (dl) {
      dl.innerHTML = '';
      const names = new Set();
      Object.values(_speakerProfiles).forEach(p => { if (p.name) names.add(p.name); });
      Object.values(_speakerLabels).forEach(n => { if (n) names.add(n); });
      names.forEach(name => {
        const opt = document.createElement('option');
        opt.value = name;
        dl.appendChild(opt);
      });
    }
  } else {
    bar.classList.add('hidden');
  }
}

function clearTranscriptSelection() {
  _transcriptSelectedSegs.clear();
  _transcriptSelectionAnchor = null;
  _updateTranscriptSelectionUI();
}

async function applyTranscriptBulkReassign() {
  const name = (document.getElementById('tsb-input')?.value || '').trim();
  if (!name) return;
  for (const segEl of _transcriptSelectedSegs) {
    const badge = segEl.querySelector('.src-speaker');
    if (!badge) continue;
    badge.textContent = name;
    badge.dataset.override = '1';
    const segId = badge.dataset.segId || segEl.dataset.segId;
    if (segId) persistSegmentOverride(segId, name).catch(() => {});
  }
  const input = document.getElementById('tsb-input');
  if (input) input.value = '';
  clearTranscriptSelection();
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

function appendTranscript(text, source, startTime, endTime, segId, labelOverride) {
  const el = document.getElementById('transcript');
  el.querySelector('.empty-hint')?.remove();

  const seg = document.createElement('div');
  seg.className = 'transcript-segment';
  seg.dataset.transcriptSource = source;  // used by filter

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
    badge.textContent = displayName;
    badge.style.backgroundColor = color + '26'; // ~15% opacity tint
    badge.style.color = color;
    badge.style.borderColor = color + '60';
    badge.addEventListener('click', e => {
      if (e.ctrlKey || e.metaKey || e.shiftKey) {
        e.preventDefault();
        e.stopPropagation();
        _toggleTranscriptSegSelection(seg, { range: e.shiftKey });
        return;
      }
      editSpeakerLabel(badge, source);
    });
    seg.appendChild(badge);
    seg.appendChild(document.createTextNode(text));
  }

  el.appendChild(seg);
  // Extend time range slider if navigator is open (before filtering, so pinned max stays Infinity)
  _tnExtendTimeRange();
  _applyFilterToSeg(seg);
  // Highlight search matches in new segment if search is active
  if (_transcriptFilter.search.trim() && seg.style.display !== 'none') {
    _tnHighlightInSeg(seg);
  }
  _highlightSelectedSpeakerBadges();
  if (!document.getElementById('speaker-manager-overlay')?.classList.contains('hidden')) {
    renderSpeakerManager();
  }
  if (_autoScroll && !_pickerOpen) {
    _programmaticScrollCount++;
    el.scrollTop = el.scrollHeight;
    setTimeout(() => { _programmaticScrollCount = Math.max(0, _programmaticScrollCount - 1); }, 100);
  }
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

  // Collect unique display names already assigned (excluding this key's current name)
  const existingNames = _speakerOptionNames(currentName, speakerKey);

  // Option buttons for existing labels
  existingNames.forEach(name => {
    const optKey = _speakerNameKey(name, speakerKey);
    const optColor = (optKey && (_speakerColors[optKey] || speakerColor(optKey))) || color;
    const opt = document.createElement('button');
    opt.className = 'speaker-picker-opt';
    opt.textContent = name;
    opt.style.borderColor = optColor + '60';
    opt.style.color = optColor;
    opt.addEventListener('mousedown', e => {
      e.preventDefault();
      commit(name);
    });
    picker.appendChild(opt);
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

  // In global mode, show a live merge hint when the typed name matches an existing speaker
  if (editMode === 'global') {
    input.addEventListener('input', () => {
      const typed = input.value.trim().toLowerCase();
      if (!typed || typed === currentName.toLowerCase()) {
        hint.textContent = isDefault
          ? `Renames all ${speakerKey} segments (${_highlighted.length + 1})`
          : `Renames all ${_highlighted.length + 1} segments for "${currentName}"`;
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
    });
  }

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

async function persistSegmentOverride(segId, label) {
  await fetch(`/api/segments/${segId}/label`, {
    method:  'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ label }),
  });
}

// Apply noise DOM styling to a single badge+seg, wiring up the reassign click handler.
function _applyNoiseStyle(seg, badge, segId) {
  seg.classList.add('noise-segment');
  seg.style.setProperty('--seg-color', _NOISE_COLOR);
  badge.className = 'src-badge src-speaker src-noise';
  badge.textContent = 'Noise';
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

  // Options: all non-noise speakers
  const profiles = _getSortedSpeakerProfiles().filter(p => p.speaker_key !== _NOISE_LABEL);
  profiles.forEach(p => {
    const name = _speakerDisplayName(p.speaker_key) || p.speaker_key;
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

  if (segId) persistSegmentOverride(segId, newName).catch(() => {});
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
    if (el.tagName === 'SPAN' && !el.dataset.override) el.textContent = nextName;
  });
  _applySpeakerColor(speakerKey, _speakerColors[speakerKey]);
  _highlightSelectedSpeakerBadges();
  if (!document.getElementById('speaker-manager-overlay')?.classList.contains('hidden')) {
    renderSpeakerManager();
  }
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
  const segs = document.querySelectorAll('#transcript .transcript-segment');
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
  if (result) navigator.clipboard.writeText(result).then(() => flashStatus('Copied!'));
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
  // Always hide noise unless toggled visible
  if ((source === _NOISE_LABEL || _manualNoiseKeys.has(source)) && !_showNoise) { seg.style.display = 'none'; return; }
  if (!_transcriptFilterActive()) { seg.style.display = ''; return; }
  const speakers = _transcriptFilter.speakers;
  // Speaker filter applies only to diarized speaker segments — never hides noise (noise has its own toggle)
  const isNoise = source === _NOISE_LABEL || _manualNoiseKeys.has(source);
  if (speakers.size > 0 && !(source in SOURCE_META) && !speakers.has(source) && !isNoise) {
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
  document.querySelectorAll('#transcript .transcript-segment').forEach(_applyFilterToSeg);
  _tnHighlightMatches();
}

function _updateFilterBtnState() {
  document.getElementById('transcript-filter-btn')
    ?.classList.toggle('active', _transcriptFilterActive());
}

// ── Panel toggle ──────────────────────────────────────────────────────────────

function openTranscriptFilter() {
  const panel = document.getElementById('transcript-navigator');
  if (!panel) return;
  const isOpen = !panel.classList.contains('collapsed');
  if (isOpen) {
    panel.classList.add('collapsed');
    return;
  }
  panel.classList.remove('collapsed');
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

  document.querySelectorAll('#transcript .transcript-segment').forEach(seg => {
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

function _tnRefreshSpeakerPills() {
  const container = document.getElementById('tn-speaker-pills');
  if (!container) return;
  container.innerHTML = '';
  const groups = _groupProfilesByName(_getSortedSpeakerProfiles());
  const allKeys = new Set();
  groups.forEach(g => g.speakerKeys.forEach(k => allKeys.add(k)));

  // Separate noise group from regular speakers
  const noiseGroups = [];
  const speakerGroups = [];
  groups.forEach(g => {
    if (g.speakerKeys.includes(_NOISE_LABEL) || g.speakerKeys.some(k => _manualNoiseKeys.has(k)))
      noiseGroups.push(g);
    else speakerGroups.push(g);
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
    pill.innerHTML = `${escapeHtml(g.name)} <span class="tn-pill-count">${count}</span>`;
    pill.title = `${g.name} — ${count} segment${count !== 1 ? 's' : ''}\nRight-click: jump to next`;

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
  const allSegs = [...document.querySelectorAll('#transcript .transcript-segment')];
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
  const allSegs = [...document.querySelectorAll('#transcript .transcript-segment')];
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
}

function _refreshAnalytics() {
  const panel = document.getElementById('analytics-panel');
  if (!panel || panel.classList.contains('collapsed')) return;

  const groups = _groupProfilesByName(_getSortedSpeakerProfiles());
  const allSegs = [...document.querySelectorAll('#transcript .transcript-segment')];

  // Gather per-speaker data
  const speakerData = [];
  let totalSegCount = 0;
  let totalSpeakTime = 0;
  let totalWords = 0;
  let sessionStart = Infinity, sessionEnd = 0;

  groups.forEach(g => {
    if (g.speakerKeys.includes(_NOISE_LABEL)) return;
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
    const color = g.color || speakerColor(g.speakerKeys[0]);
    speakerData.push({ name: g.name, color, segCount, speakTime, words, segments });
    totalSegCount += segCount;
    totalSpeakTime += speakTime;
    totalWords += words;
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
  if (!hidden) document.getElementById('summary-custom-prompt').focus();
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
  const hasPrompt = !!ta.value.trim();
  document.getElementById('summary-prompt-area').classList.toggle('hidden', !hasPrompt);
  document.getElementById('summary-prompt-toggle').classList.toggle('active', hasPrompt);
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
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}

function initPlayback(sessionId) {
  _playbackAudio.src = `/api/sessions/${sessionId}/audio`;
  _playbackAudio.load();
  _playbackActive = true;
  document.getElementById('playback-bar').classList.remove('hidden');

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
  document.getElementById('playback-play').innerHTML = '<i class="fa-solid fa-play"></i>';
  document.getElementById('playback-time').textContent = '0:00';
  document.getElementById('playback-duration').textContent = '0:00';
  document.getElementById('playback-seek').value = 0;
  clearPlayingHighlight();
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
  const ranges = [];
  document.querySelectorAll('#transcript .transcript-segment[data-start]').forEach(seg => {
    if (seg.style.display === 'none') return;
    ranges.push({
      start: parseFloat(seg.dataset.start),
      end:   parseFloat(seg.dataset.end),
    });
  });
  ranges.sort((a, b) => a.start - b.start);
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
  el.scrollIntoView(opts);
  // Scroll events fire asynchronously; decrement after they settle
  setTimeout(() => { _programmaticScrollCount = Math.max(0, _programmaticScrollCount - 1); }, 600);
}

function highlightPlayingSegment(t) {
  const segs = document.querySelectorAll('.transcript-segment[data-start]');
  let found = null;
  for (const seg of segs) {
    const start = parseFloat(seg.dataset.start);
    const end   = parseFloat(seg.dataset.end);
    if (t >= start && t < end) { found = seg; break; }
  }
  if (found === _currentPlayingSeg) return;
  if (_currentPlayingSeg) _currentPlayingSeg.classList.remove('playing');
  _currentPlayingSeg = found;
  if (found) {
    found.classList.add('playing');
    if (_autoScroll) {
      _doProgrammaticScroll(found, { behavior: 'smooth', block: 'center' });
    }
  }
}

function clearPlayingHighlight() {
  if (_currentPlayingSeg) {
    _currentPlayingSeg.classList.remove('playing');
    _currentPlayingSeg = null;
  }
}

/* ── Chat ────────────────────────────────────────────────────────────────── */
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
    <div class="chat-msg-body markdown-body"></div>`;
  el.appendChild(wrap);
  scrollChatToBottom();
  return wrap.querySelector('.chat-msg-body');
}

function appendUserBubble(text) {
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
  scrollChatToBottom();
}

function scrollChatToBottom() {
  const el = document.getElementById('chat-messages');
  el.scrollTop = el.scrollHeight;
}

function clearChat() {
  document.getElementById('chat-messages').innerHTML =
    '<p class="empty-hint">Chat cleared.</p>';
}

async function sendMessage() {
  if (state.aiChatBusy || !state.sessionId) return;
  const input    = document.getElementById('chat-input');
  const question = input.value.trim();
  if (!question) return;

  input.value = '';
  appendUserBubble(question);
  state.aiChatBusy = true;
  setSendBusy(true);

  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: state.sessionId, question }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    const bubble = createAssistantBubble();
    bubble.textContent = `Error: ${err.error || 'Unknown error'}`;
    state.aiChatBusy = false;
    setSendBusy(false);
  }
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}

function setSendBusy(busy) {
  const btn = document.getElementById('send-btn');
  btn.disabled    = busy;
  btn.textContent = busy ? '…' : 'Send';
}

/* ── Past sessions ───────────────────────────────────────────────────────── */
async function loadSession(sessionId) {
  if (sessionId === state.sessionId) return;

  if (state.isRecording) {
    if (!confirm('Stop the current recording and load this session?')) return;
    await fetch('/api/recording/stop', { method: 'POST' });
  }

  const data = await fetch(`/api/sessions/${sessionId}`).then(r => r.json());
  if (data.error) { alert(data.error); return; }

  clearAll();
  state.sessionId     = sessionId;
  state.isViewingPast = true;
  history.pushState({}, '', '?session=' + sessionId);
  updateRecordBtn();

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

  data.segments?.forEach(s =>
    appendTranscript(s.text, s.source || 'loopback', s.start_time, s.end_time,
                     s.id, s.label_override)
  );

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

  if (data.summary) {
    const sumEl = document.getElementById('summary');
    sumEl.innerHTML = renderMd(data.summary);
    highlightCode('#summary');
    linkifyTimestamps(sumEl);
  }

  if (data.chat_messages?.length) {
    document.getElementById('chat-messages').innerHTML = '';
    for (const m of data.chat_messages) {
      if (m.role === 'user') appendUserBubble(m.content);
      else {
        const b = createAssistantBubble();
        b.innerHTML = renderMd(m.content);
        linkifyTimestamps(b);
      }
    }
    highlightCode('#chat-messages');
  }

  refreshSidebar();  // re-render to highlight active item
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
  _navState = { matches: [], currentIdx: -1 };
  const tnSearch = document.getElementById('tn-search-input');
  if (tnSearch) tnSearch.value = '';
  document.getElementById('transcript-navigator')?.classList.add('collapsed');
  document.getElementById('analytics-panel')?.classList.add('collapsed');
  document.getElementById('analytics-btn')?.classList.remove('active');
  _updateFilterBtnState();
  closeSpeakerManager();
  const bar = document.getElementById('transcript-selection-bar');
  if (bar) bar.classList.add('hidden');
  document.getElementById('transcript').innerHTML =
    '<p class="empty-hint">Transcript will appear here once recording starts.</p>';
  document.getElementById('summary').innerHTML =
    '<p class="empty-hint">An auto-updating summary will appear here as the meeting progresses.</p>';
  document.getElementById('chat-messages').innerHTML =
    '<p class="empty-hint">Ask questions about the meeting here.</p>';
  state.summaryBuffer    = '';
  state.summaryStreaming  = false;
  state.chatBuffer       = '';
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
}

async function toggleAudioTest() {
  if (state.isTesting) {
    await fetch('/api/audio/test/stop', { method: 'POST' });
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
        ctx.fillStyle = `rgba(63,185,80,${micAlpha.toFixed(2)})`;
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
function toggleModelConfig() {
  const body  = document.getElementById('model-config-body');
  const arrow = document.getElementById('model-config-arrow');
  const hidden = body.classList.toggle('hidden');
  arrow.innerHTML = hidden ? '<i class="fa-solid fa-chevron-right"></i>' : '<i class="fa-solid fa-chevron-down"></i>';
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
  _apRefresh();
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
    } else {
      statusEl.textContent = `${data.commits_behind} update${data.commits_behind !== 1 ? 's' : ''} available`;
      statusEl.className = 'settings-info-val val-warn';
      btn.disabled = false;
      btn.textContent = 'Update & Restart';
      btn.onclick = applyUpdate;
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

  try {
    const res = await fetch('/api/update/apply', { method: 'POST' });
    const data = await res.json();

    if (data.error) {
      statusEl.textContent = data.error;
      statusEl.className = 'settings-info-val val-warn';
      btn.disabled = false;
      btn.textContent = 'Retry Update';
    } else {
      statusEl.textContent = 'Restarting...';
      btn.textContent = 'Restarting...';
      _pollUntilBack(btn, statusEl);
    }
  } catch (_) {
    statusEl.textContent = 'Update failed';
    statusEl.className = 'settings-info-val val-warn';
    btn.disabled = false;
    btn.textContent = 'Retry Update';
  }
}

function _pollUntilBack(btn, statusEl) {
  // Give the server a moment to begin shutting down before we start polling.
  setTimeout(async () => {
    for (;;) {
      await new Promise(r => setTimeout(r, 1500));
      try {
        const r = await fetch('/api/settings/status');
        if (r.ok) {
          statusEl.textContent = 'Updated successfully';
          statusEl.className = 'settings-info-val val-ok';
          btn.disabled = false;
          btn.textContent = 'Check for Updates';
          btn.onclick = checkForUpdates;
          return;
        }
      } catch (_) { /* server still down, keep polling */ }
    }
  }, 2000);
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

async function _apLoad() {
  try {
    _apCache = await fetch('/api/audio_params').then(r => r.json());
  } catch (_) {}
}

function _apRenderSection(containerId, paramDefs, current) {
  const container = document.getElementById(containerId);
  if (!container || !paramDefs) return;
  container.innerHTML = '';

  // Find any toggle master key in this section (controls enabled state of siblings)
  let toggleMasterKey = null;
  for (const [k, s] of Object.entries(paramDefs)) {
    if (s.type === 'toggle') { toggleMasterKey = k; break; }
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
        _apSave(key, v);
        // Enable/disable sibling params in this section
        _apSetSectionEnabled(containerId, key, cb.checked);
      });
      continue;
    }

    // Standard slider param
    const pct = ((val - spec.min) / (spec.max - spec.min)) * 100;
    const isDisabled = (toggleMasterKey && key !== toggleMasterKey && !parseInt(current[toggleMasterKey] ?? 0));

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

    slider.addEventListener('input', () => {
      input.value = slider.value;
      _apUpdateSliderFill(slider, spec);
    });
    slider.addEventListener('change', () => {
      _apSave(key, parseFloat(slider.value));
      _apToggleReset(key, parseFloat(slider.value), spec.value);
    });
    input.addEventListener('change', () => {
      let v = parseFloat(input.value);
      v = Math.min(spec.max, Math.max(spec.min, v));
      input.value = v;
      slider.value = v;
      _apUpdateSliderFill(slider, spec);
      _apSave(key, v);
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

// ── Echo Cancellation Presets ─────────────────────────────────────────────
const _ecPresets = {
  mild: {
    label: 'Mild',
    description: 'Light touch — small room, mic close to mouth, minor speaker bleed',
    icon: 'fa-volume-low',
    values: {
      echo_cancel_enabled: 1,
      echo_gate_ratio: 3.0,
      echo_silence_floor: 0.005,
      echo_spectral_sub: 0.3,
      echo_hold_ms: 80,
      echo_crossfade_ms: 30,
      echo_mic_suppress_db: -8,
    },
  },
  moderate: {
    label: 'Moderate',
    description: 'Balanced — typical webcam mic with nearby desktop speakers',
    icon: 'fa-volume',
    values: {
      echo_cancel_enabled: 1,
      echo_gate_ratio: 2.0,
      echo_silence_floor: 0.008,
      echo_spectral_sub: 0.6,
      echo_hold_ms: 150,
      echo_crossfade_ms: 30,
      echo_mic_suppress_db: -18,
    },
  },
  aggressive: {
    label: 'Aggressive',
    description: 'Strong suppression — loud speakers, mic far from mouth',
    icon: 'fa-volume-high',
    values: {
      echo_cancel_enabled: 1,
      echo_gate_ratio: 1.5,
      echo_silence_floor: 0.015,
      echo_spectral_sub: 1.0,
      echo_hold_ms: 250,
      echo_crossfade_ms: 20,
      echo_mic_suppress_db: -24,
    },
  },
  maximum: {
    label: 'Maximum',
    description: 'Nuclear option — heavy echo in an open room, priority is eliminating duplicates',
    icon: 'fa-shield-halved',
    values: {
      echo_cancel_enabled: 1,
      echo_gate_ratio: 1.2,
      echo_silence_floor: 0.025,
      echo_spectral_sub: 1.4,
      echo_hold_ms: 400,
      echo_crossfade_ms: 10,
      echo_mic_suppress_db: -30,
    },
  },
};

function _ecRenderPresets() {
  const bar = document.getElementById('echo-preset-bar');
  if (!bar) return;
  bar.innerHTML = '';
  for (const [id, preset] of Object.entries(_ecPresets)) {
    const btn = document.createElement('button');
    btn.className = 'echo-preset-btn';
    btn.id = `echo-preset-${id}`;
    btn.title = preset.description;
    btn.innerHTML = `<i class="fa-solid ${preset.icon}"></i> ${preset.label}`;
    btn.addEventListener('click', () => _ecApplyPreset(id));
    bar.appendChild(btn);
  }
  _ecHighlightActivePreset();
}

function _ecHighlightActivePreset() {
  if (!_apCache) return;
  const cur = _apCache.current;
  for (const [id, preset] of Object.entries(_ecPresets)) {
    const btn = document.getElementById(`echo-preset-${id}`);
    if (!btn) continue;
    const matches = Object.entries(preset.values).every(([k, v]) =>
      Math.abs((cur[k] ?? 0) - v) < 1e-6
    );
    btn.classList.toggle('active', matches);
  }
}

async function _ecApplyPreset(presetId) {
  const preset = _ecPresets[presetId];
  if (!preset) return;
  try {
    const res = await fetch('/api/audio_params', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(preset.values),
    }).then(r => r.json());
    if (res.ok && _apCache) {
      _apCache.current = res.audio_params;
      // Re-render the echo params section to reflect new values
      _apRenderSection('ap-echo-params', _apCache.echo_cancellation, _apCache.current);
      _ecHighlightActivePreset();
    }
  } catch (_) {}
}

async function _apRefresh() {
  await _apLoad();
  if (!_apCache) return;
  _apRenderSection('ap-transcription-params', _apCache.transcription, _apCache.current);
  _apRenderSection('ap-diarization-params',   _apCache.diarization,   _apCache.current);
  _apRenderSection('ap-echo-params',          _apCache.echo_cancellation, _apCache.current);
  _ecRenderPresets();
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
      const spec = (_apCache.transcription[key] || _apCache.diarization[key] || (_apCache.echo_cancellation && _apCache.echo_cancellation[key]));
      const resetBtn = document.getElementById(`ap-reset-${key}`);
      if (resetBtn && spec) {
        const isDefault = Math.abs(value - spec.value) < 1e-9;
        resetBtn.classList.toggle('ap-reset-hidden', isDefault);
      }
      // Update preset highlight if an echo param changed
      if (_apCache.echo_cancellation && key in _apCache.echo_cancellation) {
        _ecHighlightActivePreset();
      }
    }
  } catch (_) {}
}

async function _apResetOne(key) {
  try {
    const res = await fetch('/api/audio_params/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    }).then(r => r.json());
    if (res.ok && _apCache) {
      _apCache.current = res.audio_params;
      const spec = (_apCache.transcription[key] || _apCache.diarization[key] || (_apCache.echo_cancellation && _apCache.echo_cancellation[key]));
      if (spec) {
        if (spec.type === 'toggle') {
          const cb = document.getElementById(`ap-toggle-${key}`);
          const lbl = document.getElementById(`ap-toggle-label-${key}`);
          if (cb) { cb.checked = !!spec.value; }
          if (lbl) { lbl.textContent = spec.value ? 'Enabled' : 'Disabled'; }
          // Find which container this toggle belongs to and update siblings
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

connectSSE();
refreshSidebar();
fetch('/api/status').then(r => r.json()).then(onStatus);
fetch('/api/ai_settings')
  .then(r => r.json())
  .then(aiCfg => {
    currentAiModels = { ...AI_MODELS, ..._getAiModels(aiCfg.models) };
    updateChatModelLabel(aiCfg.provider, aiCfg.model, currentAiModels);
  })
  .catch(() => {});
startVizLoop();
initGainSliders();
_tnInitSearch();
// Load preferences first, then init components that depend on saved values
loadPreferences().then(() => {
  loadAudioDevices();
  loadModelConfig();
});
loadSummaryPrompt();

// Auto-open settings if ?settings=1 or ?setup=1 is in the URL
// Auto-load session if ?session=<id> is in the URL
{
  const params = new URLSearchParams(location.search);
  if (params.has('settings') || params.has('setup')) {
    openSettings();
    history.replaceState(null, '', location.pathname);
  } else if (params.has('session')) {
    // Defer until status has loaded — if the session is actively recording,
    // the SSE status+replay events handle everything; only call loadSession
    // for past (non-recording) sessions.
    const _pendingSessionId = params.get('session');
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
  const sid = params.get('session');
  if (sid) {
    loadSession(sid);
  } else if (!state.isRecording) {
    state.sessionId    = null;
    state.isViewingPast = false;
    clearAll();
    updateRecordBtn();
  }
});
