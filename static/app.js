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
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
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
      <button class="folder-toggle" onclick="_toggleFolder('${folder.id}')">${collapsed ? '▸' : '▾'}</button>
      <span class="folder-icon">📁</span>
      <span class="folder-name" onclick="_toggleFolder('${folder.id}')">${escapeHtml(folder.name)}</span>
      <button class="folder-action" title="Rename" onclick="renameFolderInline(event,'${folder.id}','${escapeHtml(folder.name).replace(/'/g, "\\'")}')">✎</button>
      <button class="folder-action" title="Delete folder" onclick="deleteFolder(event,'${folder.id}')">✕</button>
      <span class="folder-count">${folderSessions.length}</span>`;
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
  menuBtn.textContent = '⋮';
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
    rea.textContent = '↺  Reanalyze';
    rea.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); reanalyzeSession(ev, s.id); });
    menu.appendChild(rea);
  }

  const ren = document.createElement('div');
  ren.className = 'session-menu-item';
  ren.textContent = '✎  Rename';
  ren.addEventListener('click', ev => { ev.stopPropagation(); _closeSessionMenu(); startEditTitle(ev, s.id, s.title); });
  menu.appendChild(ren);

  const del = document.createElement('div');
  del.className = 'session-menu-item session-menu-item-danger';
  del.textContent = '✕  Delete';
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
  const nameEl = e.target.closest('.folder-header')?.querySelector('.folder-name');
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
  if (btn) { btn.textContent = '⟳ …'; btn.disabled = true; }
  await fetch('/api/sessions/bulk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'retitle', session_ids: ids }),
  });
  if (btn) { btn.textContent = '⟳ Titles'; btn.disabled = false; }
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
    if (d.seg_id) _lastLiveSegId = Math.max(_lastLiveSegId, d.seg_id);
    if (!state.isViewingPast) appendTranscript(d.text, d.source || 'loopback', d.start_time, d.end_time, d.seg_id);
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
      // speaker_label SSE already fired to rename — just show a brief log-style notice
      console.info(`[fingerprint] Auto-applied "${d.name}" → ${d.speaker_key} (${d.similarity})`);
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
    btn.innerHTML = '<span class="btn-icon">⏹</span> Stop Recording';
    btn.classList.add('recording');
    btn.classList.remove('resuming');
  } else if (state.isViewingPast) {
    btn.innerHTML = '<span class="btn-icon">▶</span> Resume Session';
    btn.classList.remove('recording');
    btn.classList.add('resuming');
  } else {
    btn.innerHTML = '<span class="btn-icon">▶</span> Start Recording';
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
    btn.textContent = '⏹ Stop Test';
    btn.classList.add('testing');
  } else {
    btn.textContent = '▶ Test Audio';
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
let _transcriptFilter = { search: '', speakers: new Set() };

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
const _SPEAKER_PALETTE = [
  '#58a6ff', // blue
  '#3fb950', // green
  '#f78166', // red-orange
  '#d2a8ff', // purple
  '#ffa657', // orange
  '#79c0ff', // light blue
  '#56d364', // light green
  '#ff7b72', // coral
  '#bc8cff', // violet
  '#e3b341', // yellow
];
let _speakerColorIdx = 0;

function _isCustomSpeakerKey(speakerKey) {
  return typeof speakerKey === 'string' && speakerKey.startsWith('custom:');
}

function _speakerDisplayName(speakerKey) {
  return _speakerProfiles[speakerKey]?.name || _speakerLabels[speakerKey] || speakerKey;
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
    _selectedSpeakerKeys = [...group.speakerKeys];
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
    _selectedSpeakerKeys = [speakerKey];
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

async function openFingerprintPanel() {
  document.getElementById('fingerprint-panel-overlay').classList.remove('hidden');
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
  const listEl = document.getElementById('fingerprint-profile-list');
  if (!_fpProfiles.length) {
    listEl.innerHTML = '<div class="fp-panel-empty">No voice profiles yet. Use the "+ New Profile" button to create one.</div>';
    return;
  }
  listEl.innerHTML = '';
  _fpProfiles.forEach(p => {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'fp-profile-row' + (_fpSelectedId === p.id ? ' selected' : '');
    row.addEventListener('click', () => _fpSelectProfile(p.id));

    const swatch = document.createElement('span');
    swatch.className = 'speaker-row-swatch';
    swatch.style.backgroundColor = p.color || '#58a6ff';

    const main = document.createElement('div');
    main.className = 'fp-profile-row-main';
    main.innerHTML = `<div class="fp-profile-name">${p.name}</div>
      <div class="fp-profile-meta">${p.emb_count} sample${p.emb_count === 1 ? '' : 's'}</div>`;

    row.appendChild(swatch);
    row.appendChild(main);
    listEl.appendChild(row);
  });
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

  const profiles = _getSortedSpeakerProfiles();
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
    btn.addEventListener('click', () => {
      _speakerDraftColor = color;
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
    badge.dataset.speakerKey = source;
    if (segId != null) badge.dataset.segId = segId;
    if (labelOverride) badge.dataset.override = '1';
    badge.title = 'Click to rename';
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
  _applyFilterToSeg(seg);
  _highlightSelectedSpeakerBadges();
  if (!document.getElementById('speaker-manager-overlay')?.classList.contains('hidden')) {
    renderSpeakerManager();
  }
  if (_autoScroll && !_pickerOpen) el.scrollTop = el.scrollHeight;
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

/* ── Transcript filter ───────────────────────────────────────────────────── */

function _transcriptFilterActive() {
  return _transcriptFilter.search.length > 0 || _transcriptFilter.speakers.size > 0;
}

function _applyFilterToSeg(seg) {
  if (!_transcriptFilterActive()) { seg.style.display = ''; return; }
  const source  = seg.dataset.transcriptSource || '';
  const search  = _transcriptFilter.search.toLowerCase().trim();
  const speakers = _transcriptFilter.speakers;
  // Speaker filter applies only to diarized speaker segments
  if (speakers.size > 0 && !(source in SOURCE_META) && !speakers.has(source)) {
    seg.style.display = 'none'; return;
  }
  // Search filter: match against full visible text
  if (search && !seg.textContent.toLowerCase().includes(search)) {
    seg.style.display = 'none'; return;
  }
  seg.style.display = '';
}

function applyTranscriptFilter() {
  document.querySelectorAll('#transcript .transcript-segment').forEach(_applyFilterToSeg);
}

function _updateFilterBtnState() {
  document.getElementById('transcript-filter-btn')
    ?.classList.toggle('active', _transcriptFilterActive());
}

function openTranscriptFilter(btn) {
  // Toggle off if already open
  if (document.getElementById('transcript-filter-popout')) {
    _closeTranscriptFilter(); return;
  }

  // Group by name so diart fragments of the same speaker appear as one row
  const groups = _groupProfilesByName(_getSortedSpeakerProfiles());

  const popout = document.createElement('div');
  popout.id = 'transcript-filter-popout';
  popout.className = 'transcript-filter-popout';
  // Prevent outside-click handler from firing immediately on the button click
  popout.addEventListener('click', e => e.stopPropagation());

  // ── Search row ──────────────────────────────────────────────────────────
  const searchWrap = document.createElement('div');
  searchWrap.className = 'tf-search-row';
  const searchIcon = document.createElement('span');
  searchIcon.className = 'tf-search-icon';
  searchIcon.textContent = '🔎';
  const searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.className = 'tf-search-input';
  searchInput.placeholder = 'Search transcript…';
  searchInput.value = _transcriptFilter.search;
  searchInput.addEventListener('input', () => {
    _transcriptFilter.search = searchInput.value;
    applyTranscriptFilter();
    _updateFilterBtnState();
  });
  searchWrap.appendChild(searchIcon);
  searchWrap.appendChild(searchInput);
  popout.appendChild(searchWrap);

  // ── Speaker filter ──────────────────────────────────────────────────────
  if (groups.length > 0) {
    const heading = document.createElement('div');
    heading.className = 'tf-section-heading';
    heading.textContent = 'Speakers';
    popout.appendChild(heading);

    const totalGroups = groups.length;

    groups.forEach(g => {
      const row = document.createElement('label');
      row.className = 'tf-speaker-row';
      // Store all keys for this group so the change handler can expand them
      row.dataset.speakerKeys = JSON.stringify(g.speakerKeys);

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'tf-speaker-cb';
      // Checked = visible. Empty filter (no restriction) means all checked.
      cb.checked = _transcriptFilter.speakers.size === 0
        || g.speakerKeys.some(k => _transcriptFilter.speakers.has(k));

      const dot = document.createElement('span');
      dot.className = 'tf-speaker-dot';
      dot.style.background = g.color || speakerColor(g.speakerKeys[0]);

      const nameEl = document.createElement('span');
      nameEl.className = 'tf-speaker-name';
      nameEl.textContent = g.name;

      row.appendChild(cb);
      row.appendChild(dot);
      row.appendChild(nameEl);
      popout.appendChild(row);

      cb.addEventListener('change', () => {
        // Rebuild filter from current checkbox states, expanding groups → individual keys
        const allRows = [...popout.querySelectorAll('.tf-speaker-row')];
        const checkedKeys = new Set();
        let checkedCount = 0;
        allRows.forEach(r => {
          if (r.querySelector('input').checked) {
            JSON.parse(r.dataset.speakerKeys || '[]').forEach(k => checkedKeys.add(k));
            checkedCount++;
          }
        });
        // All or none checked → no filter (show everything)
        _transcriptFilter.speakers = (checkedCount === 0 || checkedCount === totalGroups)
          ? new Set()
          : checkedKeys;
        applyTranscriptFilter();
        _updateFilterBtnState();
      });
    });
  }

  // ── Footer ──────────────────────────────────────────────────────────────
  const footer = document.createElement('div');
  footer.className = 'tf-footer';
  const clearBtn = document.createElement('button');
  clearBtn.className = 'tf-clear-btn';
  clearBtn.textContent = 'Clear filters';
  clearBtn.addEventListener('click', () => {
    _transcriptFilter.search = '';
    _transcriptFilter.speakers.clear();
    applyTranscriptFilter();
    _updateFilterBtnState();
    _closeTranscriptFilter();
  });
  footer.appendChild(clearBtn);
  popout.appendChild(footer);

  document.body.appendChild(popout);

  // Position below the button, right-aligned
  const rect = btn.getBoundingClientRect();
  popout.style.top   = (rect.bottom + 6) + 'px';
  const popW = 240;
  let left = rect.right - popW;
  if (left < 8) left = 8;
  popout.style.left  = left + 'px';
  popout.style.width = popW + 'px';

  searchInput.focus();
  setTimeout(() => document.addEventListener('click', _closeTranscriptFilter, { once: true }), 0);
}

function _closeTranscriptFilter() {
  document.getElementById('transcript-filter-popout')?.remove();
}

function clearTranscript() {
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
    document.getElementById('playback-time').textContent = fmtTime(t);
    document.getElementById('playback-seek').value = t;
    highlightPlayingSegment(t);
  };

  _playbackAudio.onended = () => {
    document.getElementById('playback-play').textContent = '▶';
    clearPlayingHighlight();
  };
}

function destroyPlayback() {
  _playbackAudio.pause();
  _playbackAudio.removeAttribute('src');
  _playbackActive = false;
  document.getElementById('playback-bar').classList.add('hidden');
  document.getElementById('playback-play').textContent = '▶';
  document.getElementById('playback-time').textContent = '0:00';
  document.getElementById('playback-duration').textContent = '0:00';
  document.getElementById('playback-seek').value = 0;
  clearPlayingHighlight();
}

function togglePlayback() {
  if (!_playbackActive) return;
  if (_playbackAudio.paused) {
    _playbackAudio.play();
    document.getElementById('playback-play').textContent = '⏸';
  } else {
    _playbackAudio.pause();
    document.getElementById('playback-play').textContent = '▶';
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
    document.getElementById('playback-play').textContent = '⏸';
  }
}

function setPlaybackSpeed(val) {
  _playbackAudio.playbackRate = parseFloat(val);
  savePref('playback_speed', val);
}

let _currentPlayingSeg = null;
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
    found.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
  arrow.textContent = hidden ? '▸' : '▾';
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
    const el = document.getElementById('transcript');
    el.scrollTop = el.scrollHeight;
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

  // Highlight active provider's key field
  const anthField = document.getElementById('key-anthropic-field');
  const oaiField  = document.getElementById('key-openai-field');
  anthField.style.opacity = provider === 'anthropic' ? '1' : '0.5';
  oaiField.style.opacity  = provider === 'openai'    ? '1' : '0.5';
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
}

function closeSettingsOnOverlay(e) {
  if (e.target === e.currentTarget) closeSettings();
}

function toggleKeyVis(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
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

// Disable auto-scroll when the user scrolls up in the transcript; re-enable
// when they scroll back to the bottom.
document.getElementById('transcript').addEventListener('scroll', () => {
  const el = document.getElementById('transcript');
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  if (_autoScroll !== atBottom) {
    _autoScroll = atBottom;
    updateAutoScrollBtn();
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
    // Defer until sidebar + status have loaded so the UI is ready
    setTimeout(() => loadSession(params.get('session')), 0);
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
