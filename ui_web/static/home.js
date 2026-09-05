/* ── Home Page - Global Chat & Dashboard ──────────────────────────────────── */

marked.use({ breaks: true, gfm: true });

/* ── State ────────────────────────────────────────────────────────────────── */

const _homeState = {
  conversationId: null,
  conversations: [],
  requestId: null,
  currentMsgWrap: null,
  currentChunks: [],
  currentToolCalls: [],
  busy: false,
};

let _sse = null;

/* ── Utilities ────────────────────────────────────────────────────────────── */

function _askToolDisplayName(name) {
  const map = {
    get_screenshot: 'Screenshot',
    search_transcripts: 'Search Transcripts',
    semantic_search: 'Semantic Search',
    get_session_detail: 'Load Session',
    list_speakers: 'List Speakers',
    get_speaker_history: 'Speaker History',
    list_recent_meetings: 'Recent Meetings',
    list_folders: 'List Folders',
    web_search: 'Web Search',
    plan_speaker_relabel: 'Planning speaker reassignment',
    apply_speaker_relabel: 'Applying speaker reassignment',
    cancel_speaker_relabel: 'Cancelling speaker reassignment',
  };
  return map[name] || name;
}

// Scope suffix, so a filtered search reads as: "kickoff" in Engineering, last 7 days
function _askScopeSuffix(input) {
  if (!input) return '';
  const parts = [];
  if (input.folder) {
    parts.push(input.folder + (input.include_subfolders === false ? ' (direct only)' : ''));
  }
  if (input.within_days) parts.push(`last ${input.within_days} day${input.within_days === 1 ? '' : 's'}`);
  else if (input.start_date && input.end_date) parts.push(`${input.start_date} to ${input.end_date}`);
  else if (input.start_date) parts.push(`since ${input.start_date}`);
  else if (input.end_date) parts.push(`until ${input.end_date}`);
  if (input.speaker) parts.push(`with ${input.speaker}`);
  return parts.length ? ` in ${parts.join(', ')}` : '';
}

function _askToolInputSummary(name, input) {
  if (name === 'list_folders') return 'All folders';
  if (name === 'list_recent_meetings') return _askScopeSuffix(input).replace(/^ in /, '') || 'all time';
  if (name === 'search_transcripts' && input?.query) {
    const mode = input.match && input.match !== 'all' ? ` (${input.match})` : '';
    return `"${input.query}"${mode}` + _askScopeSuffix(input);
  }
  if (name === 'semantic_search' && input?.query) return `"${input.query}"` + _askScopeSuffix(input);
  if (name === 'get_session_detail' && input?.session_id) return input.session_id.substring(0, 8) + '...';
  if (name === 'list_speakers') return 'Voice Library';
  if (name === 'get_speaker_history' && input?.speaker_name) return `"${input.speaker_name}"`;
  if (name === 'web_search' && input?.query) return `"${input.query}"`;
  if (name === 'web_search') return 'searching…';
  if (name === 'plan_speaker_relabel') {
    const scope = input?.scope === 'session' ? 'one meeting' : 'whole library';
    return `"${input?.from_name || '?'}" to "${input?.to_name || '?'}" (${scope})`;
  }
  if (name === 'apply_speaker_relabel') return 'after your confirmation';
  if (name === 'cancel_speaker_relabel') return 'plan token';
  return JSON.stringify(input || {});
}

/** Session timestamps are naive UTC (storage._now), but the calendar status
 *  carries a real offset. Only add the Z when there is nothing to say what
 *  zone the string is in, or the parse fails and every caller reads "Invalid
 *  Date". */
function _timeAgo(isoDate) {
  const raw = String(isoDate == null ? '' : isoDate);
  const zoned = /(Z|[+-]\d{2}:?\d{2})$/.test(raw);
  const d = new Date(zoned ? raw : raw + 'Z');
  if (isNaN(d.getTime())) return 'unknown';
  const now = Date.now();
  const diff = now - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString();
}

function _formatDuration(seconds) {
  if (!seconds || seconds <= 0) return '0m';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

/* ── Chat Rendering ───────────────────────────────────────────────────────── */

const _chatContainer = () => document.getElementById('global-chat-messages');

let _globalChatAtBottom = true;
const _GLOBAL_SCROLL_THRESHOLD = 60;

(function _initGlobalScrollTracking() {
  const el = _chatContainer();
  if (el) el.addEventListener('scroll', () => {
    _globalChatAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < _GLOBAL_SCROLL_THRESHOLD;
  }, { passive: true });
})();

function _scrollChatToBottom(force = false) {
  if (!force && !_globalChatAtBottom) return;
  const el = _chatContainer();
  if (el) el.scrollTop = el.scrollHeight;
}

function _hideWelcome() {
  const w = document.getElementById('home-chat-welcome');
  if (w) w.style.display = 'none';
}

function _showWelcome() {
  const w = document.getElementById('home-chat-welcome');
  if (w) w.style.display = '';
}

function _appendUserBubble(text) {
  _hideWelcome();
  const container = _chatContainer();
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg user';
  wrap.innerHTML = `
    <div class="chat-msg-header">
      <span class="chat-avatar user-avatar">U</span>
      <span class="chat-msg-role">You</span>
    </div>
    <div class="chat-msg-body">${escapeHtml(text)}</div>`;
  container.appendChild(wrap);
  // User sent a message - reset flag and force-scroll
  _globalChatAtBottom = true;
  _scrollChatToBottom();
}

function _createAssistantBubble() {
  _hideWelcome();
  const container = _chatContainer();
  const wrap = document.createElement('div');
  wrap.className = 'chat-msg assistant';
  wrap.innerHTML = `
    <div class="chat-msg-header">
      <span class="chat-avatar assistant-avatar"><i class="fa-solid fa-robot"></i></span>
      <span class="chat-msg-role">Assistant</span>
      <div class="chat-msg-actions">
        <button class="chat-msg-action-btn" title="Copy" onclick="_askCopyChatMsg(this)">
          <i class="fa-regular fa-copy"></i>
        </button>
      </div>
    </div>
    <div class="chat-msg-body markdown-body" style="display:none"></div>
    <div class="chat-processing">
      <span class="chat-processing-label">Thinking</span>
      <span class="chat-processing-dots"><span></span><span></span><span></span></span>
    </div>`;
  container.appendChild(wrap);
  _scrollChatToBottom();
  return wrap;
}

function _updateAssistantBody(msgWrap, text) {
  const body = msgWrap.querySelector('.chat-msg-body');
  if (!body) return;
  body.style.display = '';
  body.innerHTML = renderMd(text);
  body.querySelectorAll('pre code').forEach(block => {
    try { hljs.highlightElement(block); } catch {}
  });
  _addCodeCopyButtons(body);
}

function _askRenderToolWidget(msgWrap, toolCalls, isFinal = false) {
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
  // pairing fix) must still render as "completed" \u2014 the spinner state would
  // be permanently stuck otherwise.
  const allDone = isFinal || doneCount === count;
  const isOpen = widget.classList.contains('open');

  let itemsHtml = '';
  // Relabel plan cards sit outside the collapsible detail list so their
  // Confirm/Cancel buttons stay reachable once the widget collapses.
  let cardsHtml = '';
  for (const tc of toolCalls) {
    const hasResult = !!tc.result;
    let icon, iconCls, detail;
    if (hasResult) {
      icon = tc.result.success ? '\u2713' : '\u2717';
      iconCls = tc.result.success ? 'success' : 'error';
      detail = tc.result.summary;
    } else if (isFinal) {
      icon = '\u2713';
      iconCls = 'success';
      detail = '(no details saved)';
    } else {
      icon = '\u23F3';
      iconCls = 'pending';
      detail = _askToolInputSummary(tc.name, tc.input);
    }
    const label = _askToolDisplayName(tc.name);
    itemsHtml += `<div class="chat-tool-item">
      <div class="chat-tool-left">
        <div class="row1">
          <span class="chat-tool-icon ${iconCls}">${icon}</span>
          <span class="chat-tool-label">${escapeHtml(label)}</span>
        </div>
        <span class="chat-tool-detail">${escapeHtml(detail)}</span>
      </div>
    </div>`;
    cardsHtml += (typeof _relabelCardHtml === 'function') ? _relabelCardHtml(tc) : '';
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
  // Hydrated (isFinal) widgets skip the streaming class entirely \u2014 they're
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

function _askCopyChatMsg(btn) {
  const body = btn.closest('.chat-msg').querySelector('.chat-msg-body');
  if (!body) return;
  const html = body.innerHTML;
  const plain = body.innerText || '';
  navigator.clipboard.write([
    new ClipboardItem({
      'text/html': new Blob([html], { type: 'text/html' }),
      'text/plain': new Blob([plain], { type: 'text/plain' }),
    }),
  ]).catch(() => navigator.clipboard.writeText(plain)).then(() => {
    btn.classList.add('copied');
    btn.innerHTML = '<i class="fa-solid fa-check"></i>';
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.innerHTML = '<i class="fa-regular fa-copy"></i>';
    }, 1500);
  });
}

/* ── Chat Input ───────────────────────────────────────────────────────────── */

function handleGlobalChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendGlobalMessage();
  }
}

function autogrowGlobalInput() {
  const ta = document.getElementById('global-chat-input');
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
}

function _askSetChatBusy(busy) {
  _homeState.busy = busy;
  document.getElementById('global-send-btn').classList.toggle('hidden', busy);
  document.getElementById('global-stop-btn').classList.toggle('hidden', !busy);
  document.getElementById('global-chat-input').disabled = busy;
}

/* ── Send / Stop ──────────────────────────────────────────────────────────── */

async function sendGlobalMessage() {
  const input = document.getElementById('global-chat-input');
  const question = input.value.trim();
  if (!question || _homeState.busy) return;

  input.value = '';
  input.style.height = 'auto';
  _appendUserBubble(question);

  const msgWrap = _createAssistantBubble();
  _setAssistantProcessing(msgWrap, true, 'Thinking');
  _scrollChatToBottom();
  _homeState.currentMsgWrap = msgWrap;
  _homeState.currentChunks = [];
  _homeState.currentToolCalls = [];
  _askSetChatBusy(true);

  try {
    const res = await fetch('/api/global-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: _homeState.conversationId,
        question,
      }),
    });
    const data = await res.json();
    _homeState.requestId = data.request_id;
    if (!_homeState.conversationId && data.conversation_id) {
      _homeState.conversationId = data.conversation_id;
    }
  } catch (e) {
    _setAssistantProcessing(msgWrap, false);
    _updateAssistantBody(msgWrap, `*Error: ${e.message}*`);
    _askSetChatBusy(false);
  }
}

async function stopGlobalChat() {
  if (_homeState.requestId) {
    await fetch('/api/global-chat/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: _homeState.requestId }),
    });
  }
}

/* ── SSE Event Handlers ───────────────────────────────────────────────────── */

function _onGlobalChatChunk(data) {
  if (data.request_id !== _homeState.requestId) return;
  _homeState.currentChunks.push(data.text);
  const full = _homeState.currentChunks.join('');
  if (_homeState.currentMsgWrap) {
    _setAssistantProcessing(_homeState.currentMsgWrap, false);
    // Collapse tool widget when response starts streaming
    const tw = _homeState.currentMsgWrap.querySelector('.chat-tool-widget.streaming');
    if (tw) tw.classList.remove('open', 'streaming');
    _updateAssistantBody(_homeState.currentMsgWrap, full);
    const body = _homeState.currentMsgWrap.querySelector('.chat-msg-body');
    if (body) {
      _ensureTypingCursor(body);
      _chunkArrived();
    }
    _scrollChatToBottom();
  }
}

function _onGlobalToolEvent(data) {
  if (data.request_id !== _homeState.requestId) return;
  if (data.type === 'tool_call') {
    _homeState.currentToolCalls.push({
      id: data.id,
      name: data.name,
      input: data.input,
      result: null,
    });
    if (_homeState.currentMsgWrap) {
      _setAssistantProcessing(_homeState.currentMsgWrap, true, 'Using tools');
    }
  } else if (data.type === 'tool_result') {
    // Match the result to its call by id - required when tools execute in
    // parallel and results return out of order. Fall back to the first
    // still-pending call if no id is present (backward compat).
    let target = null;
    if (data.id != null) {
      target = _homeState.currentToolCalls.find(tc => tc.id === data.id && !tc.result);
    }
    if (!target) {
      target = _homeState.currentToolCalls.find(tc => !tc.result);
    }
    if (target) {
      target.result = {
        success: data.success, summary: data.summary,
        // Carries the speaker-relabel plan so the widget can offer Confirm/Cancel.
        relabel: data.relabel_plan || null,
      };
    }
    if (typeof _syncRelabelCardFromTool === 'function') _syncRelabelCardFromTool(data);
  }
  if (_homeState.currentMsgWrap) {
    _askRenderToolWidget(_homeState.currentMsgWrap, _homeState.currentToolCalls);
    _scrollChatToBottom();
  }
}

/* Home page override: app.js's _askRelabelResolve walks the per-session chat's
   tool-call list, which does not exist here. Same contract, global chat's
   list. app.js is loaded first, so this definition wins on this page. */
function _askRelabelResolve(token, stateName, message, note) {
  for (const tc of (_homeState.currentToolCalls || [])) {
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


function _onGlobalChatDone(data) {
  if (data.request_id !== _homeState.requestId) return;
  // Remove typing cursor from finished message
  if (_homeState.currentMsgWrap) {
    _removeTypingCursor();
  }
  _askSetChatBusy(false);
  _homeState.requestId = null;
  _homeState.currentMsgWrap = null;
  loadConversations();
}

function _onGlobalChatTitle(data) {
  if (data.conversation_id === _homeState.conversationId) {
    const el = document.getElementById('home-chat-title');
    if (el) el.textContent = data.title;
  }
  loadConversations();
}

/* ── SSE Setup ────────────────────────────────────────────────────────────── */

function _initSSE() {
  // Reuse app.js's SSE connection - never open a second one.
  const src = _sseSource || _sse;
  if (!src) return;  // should not happen; app.js always runs first
  _sse = src;

  src.addEventListener('global_chat_chunk', e => {
    try { _onGlobalChatChunk(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_tool_event', e => {
    try { _onGlobalToolEvent(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_done', e => {
    try { _onGlobalChatDone(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_title', e => {
    try { _onGlobalChatTitle(JSON.parse(e.data)); } catch {}
  });
  src.addEventListener('global_chat_start', () => {});
  // Resolving speakers anywhere in the app invalidates the attention slice in
  // app.js, and Home redraws from the store. Nothing to refetch here.
}

/* ── Conversation Management ──────────────────────────────────────────────── */

async function loadConversations() {
  try {
    const res = await fetch('/api/global-chat/conversations');
    _homeState.conversations = await res.json();
    _renderConversationList();
  } catch {}
}

function _renderConversationList() {
  const list = document.getElementById('home-conv-list');
  if (!_homeState.conversations.length) {
    list.innerHTML = '<p class="home-conv-empty">No conversations yet</p>';
    return;
  }

  let html = '';
  for (const conv of _homeState.conversations) {
    const active = conv.id === _homeState.conversationId ? ' active' : '';
    const msgCount = conv.message_count || 0;
    html += `
      <div class="home-conv-item${active}" data-id="${conv.id}"
           onclick="switchConversation('${conv.id}')"
           oncontextmenu="_convContextMenu(event, '${conv.id}')">
        <div class="home-conv-item-title">${escapeHtml(conv.title)}</div>
        <div class="home-conv-item-meta">
          <span>${msgCount} msg${msgCount !== 1 ? 's' : ''}</span>
          <span>${_timeAgo(conv.updated_at)}</span>
        </div>
      </div>`;
  }
  list.innerHTML = html;
}

async function switchConversation(convId) {
  if (_homeState.busy) return;
  _homeState.conversationId = convId;
  _renderConversationList();

  const container = _chatContainer();
  container.querySelectorAll('.chat-msg').forEach(el => el.remove());

  try {
    const res = await fetch(`/api/global-chat/conversations/${convId}`);
    const conv = await res.json();
    document.getElementById('home-chat-title').textContent = conv.title || 'Global Chat';

    if (!conv.messages || conv.messages.length === 0) {
      _showWelcome();
      return;
    }
    _hideWelcome();

    for (const msg of conv.messages) {
      if (msg.role === 'user') {
        _appendUserBubble(msg.content);
      } else {
        const wrap = _createAssistantBubble();
        _updateAssistantBody(wrap, msg.content);
        if (msg.tool_calls) {
          try {
            const tcs = typeof msg.tool_calls === 'string' ? JSON.parse(msg.tool_calls) : msg.tool_calls;
            if (tcs.length) _askRenderToolWidget(wrap, tcs, true);
          } catch {}
        }
      }
    }
    _globalChatAtBottom = true;
    _scrollChatToBottom();
  } catch {}
}

async function newGlobalConversation() {
  if (_homeState.busy) return;
  _homeState.conversationId = null;
  document.getElementById('home-chat-title').textContent = 'Global Chat';
  const container = _chatContainer();
  container.querySelectorAll('.chat-msg').forEach(el => el.remove());
  _showWelcome();
  _renderConversationList();
  document.getElementById('global-chat-input').focus();
}

async function clearGlobalChat() {
  // Cancel any in-flight response
  if (_homeState.busy) {
    await stopGlobalChat();
    _homeState.busy = false;
    _homeState.currentMsgWrap = null;
    _homeState.currentChunks = [];
    _homeState.currentToolCalls = [];
    _askSetChatBusy(false);
  }
  if (!_homeState.conversationId) {
    const container = _chatContainer();
    container.querySelectorAll('.chat-msg').forEach(el => el.remove());
    _showWelcome();
    return;
  }
  try {
    await fetch('/api/global-chat/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conversation_id: _homeState.conversationId }),
    });
  } catch {}
  const container = _chatContainer();
  container.querySelectorAll('.chat-msg').forEach(el => el.remove());
  _showWelcome();
}

function _convContextMenu(e, convId) {
  e.preventDefault();
  document.querySelectorAll('.home-conv-ctx').forEach(el => el.remove());

  const menu = document.createElement('div');
  menu.className = 'home-conv-ctx';
  menu.style.left = e.clientX + 'px';
  menu.style.top = e.clientY + 'px';
  menu.innerHTML = `
    <button onclick="_renameConversation('${convId}')"><i class="fa-solid fa-pen"></i> Rename</button>
    <button class="danger" onclick="_deleteConversation('${convId}')"><i class="fa-solid fa-trash"></i> Delete</button>`;
  document.body.appendChild(menu);

  const dismiss = () => { menu.remove(); document.removeEventListener('click', dismiss); };
  setTimeout(() => document.addEventListener('click', dismiss), 10);
}

async function _renameConversation(convId) {
  document.querySelectorAll('.home-conv-ctx').forEach(el => el.remove());
  const conv = _homeState.conversations.find(c => c.id === convId);
  const title = await window.uiPrompt({
    title: 'Rename conversation',
    placeholder: 'Conversation name',
    value: conv?.title || '',
  });
  if (!title || !title.trim()) return;
  try {
    await fetch(`/api/global-chat/conversations/${convId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title.trim() }),
    });
    if (convId === _homeState.conversationId) {
      document.getElementById('home-chat-title').textContent = title.trim();
    }
    loadConversations();
  } catch {}
}

async function _deleteConversation(convId) {
  document.querySelectorAll('.home-conv-ctx').forEach(el => el.remove());
  const ok = await window.uiConfirm({
    title: 'Delete this conversation?',
    message: 'The chat and its history are removed. Your meetings are not affected.',
    confirmLabel: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    await fetch(`/api/global-chat/conversations/${convId}`, { method: 'DELETE' });
    if (convId === _homeState.conversationId) {
      newGlobalConversation();
    }
    loadConversations();
  } catch {}
}

function useSuggestion(btn) {
  const input = document.getElementById('global-chat-input');
  input.value = btn.textContent;
  sendGlobalMessage();
}

// Trim pasted text
document.getElementById('global-chat-input')?.addEventListener('paste', e => {
  const ta = e.target;
  setTimeout(() => { ta.value = ta.value.trim(); }, 0);
});

/* ── Conversation Sidebar Toggle ──────────────────────────────────────────── */

function toggleConvSidebar() {
  const sidebar = document.getElementById('home-conv-sidebar');
  sidebar.classList.add('conv-animated');   // the user asked for it; animate
  sidebar.classList.toggle('collapsed');
  localStorage.setItem('home_conv_sidebar_collapsed', sidebar.classList.contains('collapsed') ? '1' : '');
}

function _restoreConvSidebar() {
  // Restoring the saved state is not a user action, so it must not animate.
  // The transition lives on .conv-animated, which only toggleConvSidebar adds.
  if (localStorage.getItem('home_conv_sidebar_collapsed') !== '0') {
    document.getElementById('home-conv-sidebar').classList.add('collapsed');
  }
}

/* ── Dashboard ────────────────────────────────────────────────────────────────
 * Home is operational, not analytical. It answers "what still needs me?" and
 * "what has been happening?", and it renders from the shared store: switching
 * to Calendar and back does not touch the network.
 *
 * The library summary is a sentence in the header subtitle, never a tile, and
 * the recordings rail already lists recent meetings, so neither lives here.
 * /api/dashboard is the aggregate source; while it is unimplemented the page
 * derives what it can from the sessions slice rather than rendering zeros.
 * ─────────────────────────────────────────────────────────────────────────── */


const _DASH_ATTENTION_ROWS = 5;
const _DASH_PEOPLE_ROWS = 30;

let _dashSessions = [];

/** Seconds of recorded audio for one session. Mirrors app.js. */
function _dashDurationSec(s) {
  if (s.last_segment_time != null && s.last_segment_time > 0) return s.last_segment_time;
  if (s.started_at && s.ended_at) {
    return Math.max(0, (new Date(s.ended_at + 'Z') - new Date(s.started_at + 'Z')) / 1000);
  }
  return 0;
}

function _dashHours(seconds) {
  const secs = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(secs / 3600);
  const m = Math.round((secs % 3600) / 60);
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

/** /api/dashboard nests its aggregates; flatten them into what Home reads,
 *  so a change of shape is one function rather than six call sites. */
function _dashNormalize(raw) {
  if (!raw) return null;
  const totals = raw.totals || {};
  const week = raw.this_week || {};
  const people = (raw.people && raw.people.items) || raw.top_speakers || [];
  return {
    total_sessions: totals.sessions != null ? totals.sessions : raw.total_sessions,
    total_seconds: totals.seconds != null ? totals.seconds : raw.total_seconds,
    speaker_count: totals.speakers != null ? totals.speakers : raw.speaker_count,
    first_session_at: totals.first_session_at || null,
    sessions_this_week: week.sessions != null ? week.sessions : raw.sessions_this_week,
    activity: raw.activity || [],
    people: people.map(sp => ({
      name: sp.name,
      color: sp.color,
      session_count: sp.meeting_count != null ? sp.meeting_count : sp.session_count,
      talk_seconds: sp.talk_seconds,
      segment_count: sp.segment_count != null ? sp.segment_count : null,
      is_me: !!sp.is_me,
    })),
  };
}

/** The header subtitle: the numbers, in a sentence. */
function _dashSubtitle(analytics) {
  const sessions = _dashSessions;
  if (!sessions.length && !analytics) return '';
  const total = analytics && analytics.total_sessions != null
    ? Number(analytics.total_sessions) : sessions.length;
  if (!total) return 'Nothing recorded yet';
  const seconds = analytics && analytics.total_seconds != null
    ? Number(analytics.total_seconds)
    : sessions.reduce((acc, s) => acc + _dashDurationSec(s), 0);

  const firstAt = analytics && analytics.first_session_at;
  const starts = sessions.map(s => s.started_at).filter(Boolean).sort();
  const firstDate = firstAt ? new Date(firstAt) : (starts.length ? new Date(starts[0] + 'Z') : null);
  const since = firstDate
    ? firstDate.toLocaleDateString(undefined, { month: 'long', day: 'numeric' })
    : '';

  const weekStart = new Date();
  weekStart.setHours(0, 0, 0, 0);
  weekStart.setDate(weekStart.getDate() - ((weekStart.getDay() + 6) % 7));
  const thisWeek = analytics && analytics.sessions_this_week != null
    ? Number(analytics.sessions_this_week)
    : sessions.filter(s => s.started_at && new Date(s.started_at + 'Z') >= weekStart).length;

  const first = `${total} meeting${total === 1 ? '' : 's'}, ${_dashHours(seconds)} recorded`
    + (since ? ` since ${since}.` : '.');
  return `${first} ${thisWeek} this week.`;
}

/** Render Home from whatever the store holds right now. No fetching here. */
function loadAnalytics() {
  _dashObserveResize();
  const analytics = _dashNormalize(AppData.get('analytics'));
  const sessions = AppData.get('sessions');
  const sessionsOk = Array.isArray(sessions);
  _dashSessions = sessionsOk ? sessions : [];

  const data = analytics || {};
  const needsAttention = _dashSessions.filter(s => s.attention && s.attention.needs);
  const count = attentionCount();

  // "Nothing recorded yet" is only true if something actually told us so.
  let empty = false;
  if (analytics) empty = (Number(data.total_sessions) || 0) === 0;
  else if (sessionsOk && AppData.status('sessions') === 'ready') empty = _dashSessions.length === 0;

  // First boot, before the recordings list has arrived: A and B show skeletons.
  const booting = !empty && !sessionsOk && AppData.status('sessions') !== 'ready';

  Views.setTitle('home', 'Home', _dashSubtitle(analytics));

  _renderFirstRun(analytics, empty);
  _renderStatCards(analytics, booting);
  _renderCadence(booting);
  _renderOverview(booting);
  _renderNext();
  _renderAttention(needsAttention, count, empty, booting);
  _renderActivityChart(
    (data.activity && data.activity.length) ? data.activity : _dashDerivedActivity());
  _renderPeople((data.people && data.people.length) ? data.people : _dashDerivedPeople());
  _renderReferencePanels(empty);
}

/* ── Repaint the pixel-sized charts when the dashboard changes size ──────────
 * The cadence and activity SVGs are drawn at their box's real size so labels
 * never scale. One observer on the dashboard root repaints them (and rebuilds
 * the heatmap, whose label density depends on cell width) after a resize. */
let _dashResizeObs = null;
let _dashResizeTimer = null;
let _dashLastActivity = null;

function _dashObserveResize() {
  if (_dashResizeObs || typeof ResizeObserver === 'undefined') return;
  const root = document.querySelector('.dash');
  if (!root) return;
  let lastW = -1, lastH = -1;
  _dashResizeObs = new ResizeObserver(entries => {
    const r = entries[0].contentRect;
    if (Math.abs(r.width - lastW) < 2 && Math.abs(r.height - lastH) < 2) return;
    lastW = r.width; lastH = r.height;
    clearTimeout(_dashResizeTimer);
    _dashResizeTimer = setTimeout(() => _dashRepaint(root), 90);
  });
  _dashResizeObs.observe(root);
  // A hidden tab defers resize notifications until it is shown again; a
  // repaint on return covers a window that was resized while it was away.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') _dashRepaint(root);
  });
}

/** Repaint the charts that are already painted, at the dashboard's current
 *  size. A skeleton or an empty state is left alone. Nothing happens while the
 *  dashboard has no width (another view is showing). */
function _dashRepaint(root) {
  root = root || document.querySelector('.dash');
  if (!root || !root.clientWidth) return;
  if (root.querySelector('.cad-svg')) _renderCadence(false);
  if (root.querySelector('.ov-heat-grid')) _renderOverview(false);
  if (_dashLastActivity && root.querySelector('.act-svg')) _renderActivityChart(_dashLastActivity);
}

/** A renderer that ran before its box had a width painted at a fallback size.
 *  Poll briefly for the box to appear and repaint once; the resize observer
 *  covers the case where the view is shown much later. Bounded, so a hidden
 *  dashboard never keeps a timer alive. */
let _dashRetryTimer = null;
function _dashRetryPaint(attempt) {
  attempt = attempt || 0;
  clearTimeout(_dashRetryTimer);
  if (attempt > 20) return;
  _dashRetryTimer = setTimeout(() => {
    const root = document.querySelector('.dash');
    if (!root || !root.clientWidth) { _dashRetryPaint(attempt + 1); return; }
    _dashRepaint(root);
  }, 120);
}

/** Recorded minutes per local day for the last 14 days, from the sessions we
 *  already hold. /api/dashboard will replace this when it lands. */
function _dashDerivedActivity() {
  const days = [];
  const byDay = new Map();
  for (const s of _dashSessions) {
    if (!s.started_at) continue;
    const key = new Date(s.started_at + 'Z').toLocaleDateString('en-CA');
    const cur = byDay.get(key) || { count: 0, seconds: 0 };
    cur.count += 1;
    cur.seconds += _dashDurationSec(s);
    byDay.set(key, cur);
  }
  for (let i = 13; i >= 0; i--) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    const key = d.toLocaleDateString('en-CA');
    const cur = byDay.get(key) || { count: 0, seconds: 0 };
    days.push({ day: key, count: cur.count, seconds: Math.round(cur.seconds) });
  }
  return days;
}

/** People by meeting count over the last eight weeks, unresolved speech
 *  excluded. One neutral bar each; the colour lives only in the avatar. */
function _dashDerivedPeople() {
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - 56);
  const generic = /^(speaker\s*\d+|other participant(\s*\d+)?|unknown|unidentified|guest|participant\s*\d+|background noise|noise)$/i;
  const byName = new Map();
  for (const s of _dashSessions) {
    if (!s.started_at || new Date(s.started_at + 'Z') < cutoff) continue;
    for (const sp of s.speakers || []) {
      const name = String(sp.name || '').trim();
      if (!name || generic.test(name)) continue;
      const entry = byName.get(name) || { name, color: sp.color || null, session_count: 0 };
      entry.session_count++;
      byName.set(name, entry);
    }
  }
  return [...byName.values()]
    .sort((a, b) => b.session_count - a.session_count)
    .slice(0, _DASH_PEOPLE_ROWS);
}

/** On a fresh install the get-started block is the whole page: empty reference
 *  panels below it are several ways of saying the same nothing. */
function _renderReferencePanels(empty) {
  // On a fresh install the get-started block is the whole page; every band of
  // cards below it is a different way of saying the same nothing.
  for (const id of ['dash-overview', 'dash-grid', 'dash-mid', 'dash-low']) {
    document.getElementById(id)?.classList.toggle('hidden', empty);
  }
}

/** Turn one recording's attention state into a sentence a person can act on. */
function _attentionReason(attention) {
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

/** Up to five speaker chips, initials on the speaker colour, then "+N". */
function _dashInitials(name) {
  return String(name || '').trim().split(/\s+/).map(w => w[0] || '').join('').slice(0, 2).toUpperCase();
}
function _dashAvatars(speakers) {
  const list = (speakers || []).filter(sp => sp && sp.name);
  if (!list.length) return '';
  const shown = list.slice(0, 5);
  const extra = list.length - shown.length;
  const avs = shown.map(sp => {
    const color = sp.color || 'var(--fg-muted)';
    return `<span class="dash-avatar" style="background:${escapeHtml(color)}" title="${escapeHtml(sp.name)}">${escapeHtml(_dashInitials(sp.name))}</span>`;
  }).join('');
  const more = extra > 0 ? `<span class="dash-avatar dash-avatar-more">+${extra}</span>` : '';
  return `<div class="dash-attention-avatars" aria-hidden="true">${avs}${more}</div>`;
}
function _dashSkeletonRows(n) {
  let html = '';
  for (let i = 0; i < n; i++) {
    html += '<li class="dash-attention-row dash-skeleton-row"><span class="skeleton skeleton-line"></span>'
      + '<span class="skeleton skeleton-pill"></span></li>';
  }
  return html;
}

function _renderAttention(needsAttention, attentionCountValue, empty, booting) {
  const headline = document.getElementById('dash-attention-headline');
  const list = document.getElementById('dash-attention-list');
  const all = document.getElementById('dash-attention-all');
  const section = document.getElementById('dash-attention');
  if (!headline || !list || !all || !section) return;

  if (booting) {
    headline.textContent = 'Needs attention';
    section.classList.remove('is-clear', 'hidden');
    all.classList.add('hidden');
    list.innerHTML = _dashSkeletonRows(3);
    return;
  }

  const count = attentionCountValue || needsAttention.length;
  section.classList.toggle('is-clear', count === 0);
  // On a fresh install the get-started block already says what to do; a second
  // line saying nothing needs attention is noise.
  section.classList.toggle('hidden', count === 0 && empty);

  if (count === 0) {
    headline.textContent = 'Every recording has its speakers named';
    list.innerHTML = '';
    all.classList.add('hidden');
    return;
  }

  headline.textContent = count === 1
    ? '1 recording needs speaker work'
    : `${count} recordings need speaker work`;

  const rows = needsAttention
    .slice()
    .sort((a, b) => String(b.started_at || '').localeCompare(String(a.started_at || '')))
    .slice(0, _DASH_ATTENTION_ROWS);

  // With no rows to show (the recordings list did not load) the link is the
  // only way through to the queue, so it has to stay.
  all.classList.toggle('hidden', rows.length > 0 && count <= _DASH_ATTENTION_ROWS);
  all.textContent = `See all ${count}`;

  if (!rows.length) {
    list.innerHTML =
      '<li class="dash-attention-row dash-attention-row-note">Could not load the list of recordings.</li>';
    return;
  }

  const html = rows.map(s => {
    const when = s.started_at
      ? new Date(s.started_at + 'Z').toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
      : '';
    return `
      <li class="dash-attention-row">
        <div class="dash-attention-row-text">
          <span class="dash-attention-row-title">${escapeHtml(s.title || s.id)}</span>
          <span class="dash-attention-row-meta">${escapeHtml(when)} · ${escapeHtml(_attentionReason(s.attention))}</span>
        </div>
        ${_dashAvatars(s.speakers)}
        <a class="btn btn-secondary" href="/session?id=${encodeURIComponent(s.id)}&amp;speakers=cleanup">Clean up</a>
      </li>`;
  }).join('');
  _dashMorph(list, html);
}

/** Keyed update: morphdom keeps focus, selection and scroll where they were.
 *  A wholesale innerHTML swap on a visible list would not. */
function _dashMorph(el, html) {
  if (!window.morphdom) { el.innerHTML = html; return; }
  const next = el.cloneNode(false);
  next.innerHTML = html;
  morphdom(el, next, { childrenOnly: true });
}

function _renderFirstRun(analytics, empty) {
  const firstRun = document.getElementById('dash-firstrun');
  const hint = document.getElementById('dash-library-hint');
  if (firstRun) firstRun.classList.toggle('hidden', !empty);
  // Recordings but no named voices: one line is enough. The work itself lives
  // in the Speakers view, not here. Only the aggregate knows the library size,
  // so stay quiet when it did not load.
  if (hint) {
    hint.classList.toggle(
      'hidden', empty || !analytics || (Number(analytics.speaker_count) || 0) > 0);
  }
}

/** A nice round axis top in minutes, so the gridlines land on readable values. */
function _homeNiceMinutes(maxMin) {
  const steps = [15, 30, 45, 60, 90, 120, 180, 240, 360, 480, 600, 720];
  for (const s of steps) if (maxMin <= s) return s;
  return Math.ceil(maxMin / 60) * 60;
}
function _homeMinLabel(min) {
  if (min >= 60) {
    const h = min / 60;
    return (Number.isInteger(h) ? h : h.toFixed(1)) + 'h';
  }
  return Math.round(min) + 'm';
}
function _homeActLong(dayKey) {
  return new Date(dayKey + 'T12:00:00')
    .toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}
function _homeActShort(dayKey) {
  return new Date(dayKey + 'T12:00:00')
    .toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** E. Activity, last 14 days: recorded minutes per local day as an inline SVG
 *  histogram. Supporting, so it sits last on the page. */
function _renderActivityChart(activity) {
  const chart = document.getElementById('home-activity-chart');
  const note = document.getElementById('home-activity-summary');
  const desc = document.getElementById('home-activity-desc');
  if (!chart) return;

  activity = activity || [];
  _dashLastActivity = activity;
  if (note) note.textContent = activity.length ? 'Last 14 days' : '';

  if (!activity.length) {
    chart.innerHTML = '<p class="home-activity-empty">No recordings in the last two weeks.</p>';
    if (desc) desc.textContent = '';
    return;
  }

  const today = new Date().toLocaleDateString('en-CA');
  const maxMin = Math.max(...activity.map(a => (a.seconds || 0) / 60), 1);
  const niceMax = _homeNiceMinutes(maxMin);

  // Drawn at the box's real pixel size (see _renderCadence), so the labels
  // stay 12 px whether the card is 500 px or 1500 px wide.
  if (!chart.clientWidth) _dashRetryPaint();   // hidden right now: repaint once it has a size
  const W = chart.clientWidth || 560;
  const H = chart.clientHeight || 160;
  const padL = 40, padR = 10, padT = 18, padB = 22;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const n = activity.length;
  const slot = innerW / n;
  const barW = Math.max(6, Math.min(34, slot * 0.6));
  const baseY = padT + innerH;
  const yFor = min => baseY - (min / niceMax) * innerH;
  // Wider slots earn a label under every day and a value above each bar.
  const labelEvery = slot >= 48 ? 1 : (slot >= 30 ? 2 : 0);
  const showValues = slot >= 44;

  let grid = '';
  for (const gm of [niceMax, niceMax / 2]) {
    const y = yFor(gm);
    grid += `<line class="act-grid" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}"></line>`;
    grid += `<text class="act-ylabel" x="${padL - 6}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${_homeMinLabel(gm)}</text>`;
  }
  grid += `<line class="act-baseline" x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}"></line>`;

  const midIdx = Math.floor(n / 2);
  let bars = '', xlabels = '';
  const descParts = [];
  let todayLabelled = false;
  activity.forEach((a, i) => {
    const cx = padL + slot * i + slot / 2;
    const mins = (a.seconds || 0) / 60;
    const h = mins > 0 ? Math.max(2, (mins / niceMax) * innerH) : 0;
    const isToday = a.day === today;
    const label = `${_homeActLong(a.day)} · ${a.count} meeting${a.count === 1 ? '' : 's'} · ${_dashHours(a.seconds || 0)}`;
    descParts.push(label);
    const cls = 'act-bar' + (mins > 0 ? '' : ' act-bar-empty') + (isToday ? ' is-today' : '');
    const y = mins > 0 ? baseY - h : baseY - 2;
    const drawH = mins > 0 ? h : 2;
    bars += `<rect class="${cls}" x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${drawH.toFixed(1)}" rx="2" tabindex="0" role="img" data-tip="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"></rect>`;
    if (showValues && mins > 0) {
      bars += `<text class="act-vlabel" x="${cx.toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle">${_dashCompactHours(mins * 60)}</text>`;
    }
    const labelIt = labelEvery ? (i % labelEvery === 0 || i === n - 1) : (i === 0 || i === n - 1 || i === midIdx);
    if (labelIt) {
      const anchor = i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle');
      const tx = i === 0 ? padL : (i === n - 1 ? W - padR : cx);
      const txt = isToday ? 'Today' : _homeActShort(a.day);
      if (isToday) todayLabelled = true;
      xlabels += `<text class="act-xlabel" x="${tx.toFixed(1)}" y="${H - 6}" text-anchor="${anchor}">${escapeHtml(txt)}</text>`;
    }
  });
  if (!todayLabelled) {
    const ti = activity.findIndex(a => a.day === today);
    if (ti >= 0) {
      const cx = padL + slot * ti + slot / 2;
      xlabels += `<text class="act-xlabel" x="${cx.toFixed(1)}" y="${H - 6}" text-anchor="middle">Today</text>`;
    }
  }

  chart.innerHTML =
    `<svg class="act-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="group" aria-label="Recorded time per day, last 14 days">`
    + grid + bars + xlabels + '</svg>';
  if (desc) desc.textContent = 'Recorded time per day, last 14 days. ' + descParts.join('. ') + '.';
}

/** "45m", "1.5h", "12h": a value short enough to sit above a bar. */
function _dashCompactHours(seconds) {
  const h = (seconds || 0) / 3600;
  if (h < 1) return `${Math.round((seconds || 0) / 60)}m`;
  const shown = h >= 10 ? Math.round(h) : Math.round(h * 10) / 10;
  return `${shown}h`;
}

/* ── Weekly aggregation, shared by the stat cards and the cadence chart ──────
 * Everything below derives from the sessions slice already in the store. A week
 * is Monday to Sunday in local time, matching the rest of Home. */

/** Local Monday for a date, at midnight. */
function _weekStartLocal(date) {
  const x = new Date(date);
  x.setHours(0, 0, 0, 0);
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));
  return x;
}

/** The last `n` weeks, oldest first, each {weekStart, key, count, seconds}. The
 *  final entry is the current week. */
function _dashWeekly(n) {
  const weeks = [];
  const byKey = new Map();
  const thisMon = _weekStartLocal(new Date());
  for (let i = n - 1; i >= 0; i--) {
    const ws = new Date(thisMon);
    ws.setDate(ws.getDate() - i * 7);
    const w = { weekStart: ws, key: ws.toLocaleDateString('en-CA'), count: 0, seconds: 0 };
    weeks.push(w);
    byKey.set(w.key, w);
  }
  const oldest = weeks[0].weekStart;
  for (const s of _dashSessions) {
    if (!s.started_at) continue;
    const d = new Date(s.started_at + 'Z');
    if (d < oldest) continue;
    const w = byKey.get(_weekStartLocal(d).toLocaleDateString('en-CA'));
    if (!w) continue;
    w.count += 1;
    w.seconds += _dashDurationSec(s);
  }
  return weeks;
}

/** A minimal area sparkline. Stroke stays crisp under a stretched viewBox via
 *  non-scaling-stroke, so the card can size it to any width. */
function _sparklineSvg(values) {
  const n = values.length;
  if (!n) return '';
  const W = 120, H = 30, pad = 2;
  const max = Math.max(...values, 1e-6);
  // One data point has no run to draw, so give it a flat two-point line across
  // the width rather than an invisible single vertex.
  const xs = n > 1 ? values.map((_, i) => pad + i * ((W - 2 * pad) / (n - 1)))
                   : [pad, W - pad];
  const ys = n > 1 ? values.map(v => H - pad - (v / max) * (H - 2 * pad))
                   : [H - pad - (values[0] / max) * (H - 2 * pad),
                      H - pad - (values[0] / max) * (H - 2 * pad)];
  const pts = xs.map((x, i) => `${x.toFixed(1)},${ys[i].toFixed(1)}`);
  const line = pts.join(' ');
  const lastXn = xs[xs.length - 1];
  const lastX = lastXn.toFixed(1);
  const area = `${pad},${(H - pad).toFixed(1)} ${line} ${lastX},${(H - pad).toFixed(1)}`;
  return `<svg class="ov-spark-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + `<polygon class="ov-spark-area" points="${area}"></polygon>`
    + `<polyline class="ov-spark-line" points="${line}"></polyline></svg>`;
}

/** A change chip against the prior week. Direction only, never coloured good or
 *  bad: more meetings is not inherently either. `fmt` receives the signed delta.
 *  The glyph is decorative; the direction is spelled out in an aria-label so a
 *  screen reader hears "down 7 from last week", not "triangle 7". */
function _deltaChip(cur, prev, fmt, eps) {
  const d = cur - prev;
  const e = eps || 0;
  if (d > e) {
    const t = fmt(d);
    return `<span class="ov-delta up" aria-label="up ${escapeHtml(t)} from last week">`
      + `<span aria-hidden="true">▲ ${escapeHtml(t)}</span></span>`;
  }
  if (d < -e) {
    const t = fmt(d);
    return `<span class="ov-delta down" aria-label="down ${escapeHtml(t)} from last week">`
      + `<span aria-hidden="true">▼ ${escapeHtml(t)}</span></span>`;
  }
  return `<span class="ov-delta flat" aria-label="no change from last week" title="No change from last week">`
    + `<span aria-hidden="true">·</span></span>`;
}

function _statCard(c) {
  return `<div class="ov-metric">`
    + `<div class="ov-metric-head"><span class="ov-metric-label">${escapeHtml(c.label)}</span>${c.delta || ''}</div>`
    + `<div class="ov-metric-value">${escapeHtml(c.value)}<span class="ov-metric-unit">${escapeHtml(c.unit)}</span></div>`
    + `<div class="ov-spark">${c.spark || ''}</div>`
    + `</div>`;
}

/** The backlog card is a route link to the attention queue, and its "spark" is
 *  a resolved-share bar instead of a trend. */
function _statBacklogCard(backlog, cleanShare) {
  const pct = Math.round(cleanShare * 100);
  const label = backlog > 0
    ? `${pct}% of your library resolved`
    : 'Every recording resolved';
  return `<a class="ov-metric ov-metric-link" data-nav href="/attention">`
    + `<div class="ov-metric-head"><span class="ov-metric-label">Needs attention</span>`
    + (backlog > 0 ? `<span class="ov-delta go" aria-hidden="true">→</span>` : '') + `</div>`
    + `<div class="ov-metric-value">${backlog}<span class="ov-metric-unit">${backlog === 1 ? 'recording' : 'recordings'}</span></div>`
    + `<div class="ov-spark ov-spark-prog">`
    + `<div class="ov-prog" role="img" aria-label="${escapeHtml(pct + ' percent of recordings resolved')}"><div class="ov-prog-fill" style="width:${pct}%"></div></div>`
    + `<span class="ov-prog-label">${escapeHtml(label)}</span>`
    + `</div></a>`;
}

/** Band 0: four cards, each a value this week plus its eight-week trend. */
function _renderStatCards(analytics, booting) {
  const el = document.getElementById('dash-overview-metrics');
  if (!el) return;
  if (booting) {
    el.innerHTML = Array.from({ length: 4 }, () =>
      '<div class="ov-metric ov-metric-skel"><span class="skeleton skeleton-line"></span>'
      + '<span class="skeleton skeleton-value"></span><span class="skeleton skeleton-spark"></span></div>').join('');
    return;
  }
  const sessions = (_dashSessions || []).filter(s => s.started_at);
  const weeks = _dashWeekly(8);
  const cur = weeks[weeks.length - 1] || { count: 0, seconds: 0 };
  const prev = weeks[weeks.length - 2] || { count: 0, seconds: 0 };
  const counts = weeks.map(w => w.count);
  const hours = weeks.map(w => w.seconds / 3600);
  const avgMins = weeks.map(w => w.count ? (w.seconds / w.count) / 60 : 0);
  const curAvgMin = cur.count ? (cur.seconds / cur.count) / 60 : 0;
  const prevAvgMin = prev.count ? (prev.seconds / prev.count) / 60 : 0;

  const total = analytics && analytics.total_sessions != null
    ? Number(analytics.total_sessions) : sessions.length;
  const backlog = (typeof attentionCount === 'function') ? attentionCount() : 0;
  const cleanShare = total > 0 ? Math.max(0, Math.min(1, (total - backlog) / total)) : 1;

  const allAvgSec = sessions.length
    ? sessions.reduce((a, s) => a + _dashDurationSec(s), 0) / sessions.length : 0;

  // The arrow carries direction, so the number is a magnitude only.
  const cards = [
    _statCard({
      label: 'Meetings', value: String(cur.count), unit: 'this week',
      delta: _deltaChip(cur.count, prev.count, d => String(Math.abs(d))),
      spark: _sparklineSvg(counts),
    }),
    _statCard({
      label: 'Recorded', value: _dashHours(cur.seconds) || '0m', unit: 'this week',
      delta: _deltaChip(cur.seconds / 3600, prev.seconds / 3600,
        d => (Math.round(Math.abs(d) * 10) / 10) + 'h', 0.05),
      spark: _sparklineSvg(hours),
    }),
    _statCard({
      label: 'Avg length',
      value: cur.count ? (_dashHours(cur.seconds / cur.count) || '0m') : (_dashHours(allAvgSec) || '0m'),
      unit: cur.count ? 'this week' : 'all time',
      delta: (cur.count && prev.count)
        ? _deltaChip(curAvgMin, prevAvgMin, d => Math.round(Math.abs(d)) + 'm', 0.5) : '',
      spark: _sparklineSvg(avgMins),
    }),
    _statBacklogCard(backlog, cleanShare),
  ];
  el.innerHTML = cards.join('');
}

/* ── The cadence chart: recorded hours per week over the last 12 weeks ────────
 * The hero visual. An inline SVG bar chart, one measure, current week picked
 * out, hover per bar through the shared #dash tooltip. */

function _homeNiceHours(maxH) {
  const steps = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 30, 40, 50];
  for (const s of steps) if (maxH <= s) return s;
  return Math.ceil(maxH / 10) * 10;
}
function _homeHourLabel(h) {
  return (Number.isInteger(h) ? h : h.toFixed(1)) + 'h';
}
function _weekLabelShort(ws) {
  return ws.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function _renderCadence(booting) {
  const chart = document.getElementById('home-cadence-chart');
  const note = document.getElementById('home-cadence-note');
  const desc = document.getElementById('home-cadence-desc');
  if (!chart) return;
  if (booting) {
    chart.innerHTML = '<div class="home-cadence-skel skeleton"></div>';
    if (note) note.textContent = '';
    return;
  }

  const weeks = _dashWeekly(12);
  const any = weeks.some(w => w.count > 0);
  if (note) note.textContent = any ? 'Recorded hours per week, last 12 weeks' : '';
  if (!any) {
    chart.innerHTML = '<p class="home-cadence-empty">Record a few meetings and your weekly load shows up here.</p>';
    if (desc) desc.textContent = '';
    return;
  }

  const hoursArr = weeks.map(w => w.seconds / 3600);
  const niceMax = _homeNiceHours(Math.max(...hoursArr, 0.5));

  // Drawn at the box's real pixel size, so an axis label is 11 px on a 500 px
  // card and 11 px on a 1500 px card. The SVG is absolutely positioned inside
  // the chart box, so it never feeds back into the height it measures;
  // _dashObserveResize repaints when the card changes size.
  if (!chart.clientWidth) _dashRetryPaint();   // hidden right now: repaint once it has a size
  const W = chart.clientWidth || 720;
  const H = chart.clientHeight || 200;
  const padL = 40, padR = 12, padT = 18, padB = 26;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const n = weeks.length;
  const slot = innerW / n;
  const barW = Math.max(10, Math.min(48, slot * 0.62));
  const baseY = padT + innerH;
  const yFor = h => baseY - (h / niceMax) * innerH;
  // Wider slots earn a label under every week and a value above each bar.
  const labelEvery = slot >= 58 ? 1 : (slot >= 34 ? 2 : 0);
  const showValues = slot >= 52;

  let grid = '';
  for (const gh of [niceMax, niceMax / 2]) {
    const y = yFor(gh);
    grid += `<line class="cad-grid" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}"></line>`;
    grid += `<text class="cad-ylabel" x="${padL - 6}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${_homeHourLabel(gh)}</text>`;
  }
  grid += `<line class="cad-baseline" x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}"></line>`;

  const midIdx = Math.floor(n / 2);
  let bars = '', xlabels = '';
  const descParts = [];
  weeks.forEach((w, i) => {
    const cx = padL + slot * i + slot / 2;
    const hrs = w.seconds / 3600;
    const h = hrs > 0 ? Math.max(2, (hrs / niceMax) * innerH) : 0;
    const isCurrent = i === n - 1;
    const label = `Week of ${_weekLabelShort(w.weekStart)} · ${w.count} meeting${w.count === 1 ? '' : 's'} · ${_dashHours(w.seconds) || '0m'}`;
    descParts.push(label);
    const cls = 'cad-bar' + (hrs > 0 ? '' : ' cad-bar-empty') + (isCurrent ? ' is-current' : '');
    const y = hrs > 0 ? baseY - h : baseY - 2;
    const drawH = hrs > 0 ? h : 2;
    bars += `<rect class="${cls}" x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${drawH.toFixed(1)}" rx="3" tabindex="0" role="img" data-tip="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"></rect>`;
    if (showValues && hrs > 0) {
      bars += `<text class="cad-vlabel" x="${cx.toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle">${_dashCompactHours(w.seconds)}</text>`;
    }
    const labelIt = labelEvery ? (i % labelEvery === 0 || isCurrent) : (i === 0 || i === midIdx || isCurrent);
    if (labelIt) {
      const anchor = i === 0 ? 'start' : (isCurrent ? 'end' : 'middle');
      const tx = i === 0 ? padL : (isCurrent ? W - padR : cx);
      const txt = isCurrent ? 'This wk' : _weekLabelShort(w.weekStart);
      xlabels += `<text class="cad-xlabel" x="${tx.toFixed(1)}" y="${H - 6}" text-anchor="${anchor}">${escapeHtml(txt)}</text>`;
    }
  });

  chart.innerHTML =
    `<svg class="cad-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="group" aria-label="Recorded hours per week, last 12 weeks">`
    + grid + bars + xlabels + '</svg>';
  if (desc) desc.textContent = 'Recorded hours per week, last 12 weeks. ' + descParts.join('. ') + '.';
}

/** Diarizer bookkeeping labels are not people, wherever the list came from. */
const _DASH_NOT_A_PERSON = /^(background noise|noise|unknown|unidentified|guest|speaker\s*\d+|other participant(\s*\d+)?|participant\s*\d+)$/i;

function _renderPeople(speakers) {
  const list = document.getElementById('home-speakers-list');
  const note = document.getElementById('home-speakers-note');
  if (!list) return;
  speakers = (speakers || []).filter(sp => sp && sp.name && !_DASH_NOT_A_PERSON.test(String(sp.name).trim()));
  if (note) note.textContent = speakers.length ? 'Last 8 weeks' : '';
  if (!speakers.length) {
    list.innerHTML = '<p class="home-speakers-empty">Name a speaker once and they appear here.</p>';
    return;
  }

  // Most active voices first: transcript segments (turns taken), then talk
  // time, then meetings. The "you" row sorts with everyone else.
  const ordered = speakers.slice().sort((a, b) =>
    ((b.segment_count || 0) - (a.segment_count || 0)) ||
    ((b.talk_seconds || 0) - (a.talk_seconds || 0)) ||
    ((b.session_count || 0) - (a.session_count || 0)) ||
    String(a.name).localeCompare(String(b.name)));
  const useSeg = ordered.some(s => (s.segment_count || 0) > 0);
  const useTalk = !useSeg && ordered.some(s => (s.talk_seconds || 0) > 0);
  const metric = s => useSeg ? (s.segment_count || 0) : (useTalk ? (s.talk_seconds || 0) : (s.session_count || 0));
  if (note) note.textContent = useSeg ? 'By segments, 8 weeks' : (useTalk ? 'By talk time, 8 weeks' : 'Last 8 weeks');
  const maxMetric = Math.max(...ordered.map(metric), 1);

  _dashMorph(list, ordered.slice(0, _DASH_PEOPLE_ROWS).map(sp => {
    const color = sp.color || 'var(--fg-muted)';
    const initials = sp.name.split(/\s+/).map(w => w[0]).join('').slice(0, 2);
    const barPct = (metric(sp) / maxMetric) * 100;
    const talkTime = sp.talk_seconds ? _formatDuration(sp.talk_seconds) : '';
    const segs = sp.segment_count != null ? `${Number(sp.segment_count).toLocaleString()} segment${sp.segment_count === 1 ? '' : 's'}` : '';
    const statsText = [
      `${sp.session_count} meeting${sp.session_count !== 1 ? 's' : ''}`,
      segs,
      talkTime,
    ].filter(Boolean).join(' · ');
    return `
      <div class="home-speaker-item">
        <div class="home-speaker-avatar" style="background:${escapeHtml(color)}">${escapeHtml(initials)}</div>
        <div class="home-speaker-info">
          <div class="home-speaker-name">${escapeHtml(sp.name)}${sp.is_me ? '<span class="home-speaker-you">you</span>' : ''}</div>
          <div class="home-speaker-stats">${escapeHtml(statsText)}</div>
        </div>
        <div class="home-speaker-bar-wrap">
          <div class="home-speaker-bar" style="width:${barPct}%"></div>
        </div>
      </div>`;
  }).join(''));
}

/* ── A. This week ────────────────────────────────────────────────────────────
 * The schedule and the capture on one time axis, Monday to Sunday. Scheduled
 * meetings are outlined spans, recordings are filled, a matched pair is one
 * combined span. The axis runs 7:00 to 19:00 and extends to fit anything that
 * falls outside. Every span is a route link, so it is keyboard reachable in
 * chronological order, and a visually hidden list repeats each item as text.
 * ─────────────────────────────────────────────────────────────────────────── */

/** Monday of the local week, and the range key that also covers today plus two
 *  days so the Next agenda reads from the same one loaded range. */
function _homeWeekRange() {
  const now = new Date();
  const weekStart = new Date(now);
  weekStart.setHours(0, 0, 0, 0);
  weekStart.setDate(weekStart.getDate() - ((weekStart.getDay() + 6) % 7));
  const weekSun = new Date(weekStart);
  weekSun.setDate(weekSun.getDate() + 6);
  const today = new Date(now);
  today.setHours(0, 0, 0, 0);
  const agendaEnd = new Date(today);
  agendaEnd.setDate(agendaEnd.getDate() + 2);
  const rangeEnd = agendaEnd > weekSun ? agendaEnd : weekSun;
  const iso = d => d.toLocaleDateString('en-CA');
  return {
    weekStart, weekSun, today,
    todayKey: iso(today),
    rangeKey: calendarRangeKey(iso(weekStart), iso(rangeEnd)),
  };
}

function _homeDayKey(date) { return date.toLocaleDateString('en-CA'); }
function _homeClock(date) {
  return date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

/* ── A. Overview: advanced stats and a "when you meet" heatmap ───────────────
 * Replaces the old week timeline; the Activity chart lower down already carries
 * the recent-days view. Everything is derived from the sessions slice we hold,
 * so there is no fetch here. */
const _OV_DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _OV_DAYS_FULL = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
  'Saturday', 'Sunday'];

function _ovHourLabel(h) {
  const h12 = ((h + 11) % 12) + 1;
  return `${h12}${h < 12 ? 'a' : 'p'}`;
}

function _ovHourClock(h) {
  const h12 = ((h + 11) % 12) + 1;
  return `${h12} ${h < 12 ? 'AM' : 'PM'}`;
}

function _renderOverview(booting) {
  const heatEl = document.getElementById('dash-overview-heat');
  const descEl = document.getElementById('dash-overview-desc');
  const noteEl = document.getElementById('dash-overview-note');
  if (!heatEl) return;

  if (booting) {
    heatEl.innerHTML = '<div class="ov-heat-skeleton skeleton"></div>';
    if (noteEl) noteEl.textContent = '';
    return;
  }

  const sessions = (_dashSessions || []).filter(s => s.started_at);
  if (!sessions.length) {
    heatEl.innerHTML = '<p class="ov-empty">Record a few meetings and your patterns show up here.</p>';
    if (descEl) descEl.textContent = '';
    if (noteEl) noteEl.textContent = '';
    return;
  }

  // Aggregate from local start times: per weekday, per hour, longest, and the
  // weekday x hour grid the heatmap draws. The caption stats that used to crowd
  // the top strip live under the grid, where the pattern gives them meaning.
  let longestSec = 0, dataMinH = 23, dataMaxH = 0, first = null;
  const dayCount = new Array(7).fill(0);
  const hourCount = new Array(24).fill(0);
  const grid = Array.from({ length: 7 }, () => new Array(24).fill(0));
  for (const s of sessions) {
    const d = new Date(s.started_at + 'Z');
    const wd = (d.getDay() + 6) % 7;
    const h = d.getHours();
    const dur = _dashDurationSec(s);
    if (dur > longestSec) longestSec = dur;
    dayCount[wd]++;
    hourCount[h]++;
    grid[wd][h]++;
    if (h < dataMinH) dataMinH = h;
    if (h > dataMaxH) dataMaxH = h;
    if (!first || d < first) first = d;
  }

  // A readable hour window: clamp to working hours, keep at least an 8h span.
  let minH = Math.max(7, Math.min(9, dataMinH));
  let maxH = Math.min(20, Math.max(18, dataMaxH));
  if (maxH - minH < 8) maxH = Math.min(21, minH + 8);
  const cols = maxH - minH + 1;

  // Fold anything outside the window into the edge columns so no meeting is lost.
  const disp = Array.from({ length: 7 }, () => new Array(cols).fill(0));
  for (let w = 0; w < 7; w++) {
    for (let h = 0; h < 24; h++) {
      const c = grid[w][h];
      if (!c) continue;
      const col = Math.max(0, Math.min(cols - 1, h - minH));
      disp[w][col] += c;
    }
  }
  let maxCell = 1;
  for (let w = 0; w < 7; w++) for (let col = 0; col < cols; col++) maxCell = Math.max(maxCell, disp[w][col]);

  const busiestWd = dayCount.indexOf(Math.max(...dayCount));
  const peakH = hourCount.indexOf(Math.max(...hourCount));
  const weeksSpan = first ? Math.max(1, (Date.now() - first.getTime()) / (7 * 864e5)) : 1;
  // An honest rate: do not floor a sparse history up to "1/week".
  const perWeekRate = sessions.length / weeksSpan;
  const cadenceSeg = perWeekRate >= 0.95
    ? `<span>About <b>${Math.round(perWeekRate)}/week</b></span>`
    : `<span><b>Under 1</b> a week</span>`;

  // Hour labels follow the cell width: every hour when there is room, every
  // second or third otherwise. The grid is rebuilt on resize.
  const heatW = heatEl.clientWidth || 0;
  if (!heatW) _dashRetryPaint();   // label density needs a real width
  const cellW = cols ? (heatW - 96) / cols : 0;
  const labelEvery = cellW >= 44 ? 1 : (cellW >= 28 ? 2 : 3);
  const todayWd = (new Date().getDay() + 6) % 7;
  const descParts = [];
  let cells = '<span class="ov-heat-corner" aria-hidden="true"></span>';
  for (let h = minH; h <= maxH; h++) {
    const show = (h - minH) % labelEvery === 0 || h === maxH;
    cells += `<span class="ov-heat-hour">${show ? escapeHtml(_ovHourLabel(h)) : ''}</span>`;
  }
  cells += '<span class="ov-heat-corner" aria-hidden="true"></span>';
  for (let w = 0; w < 7; w++) {
    cells += `<span class="ov-heat-day${w === todayWd ? ' is-today' : ''}">${_OV_DAYS[w]}</span>`;
    for (let col = 0; col < cols; col++) {
      const total = disp[w][col];
      const intensity = total ? 16 + Math.round((total / maxCell) * 74) : 0;
      const bg = total
        ? `color-mix(in srgb, var(--accent) ${intensity}%, var(--surface2))`
        : 'var(--surface2)';
      const peak = total === maxCell && maxCell > 1;
      const tip = total
        ? `${_OV_DAYS[w]} ${_ovHourClock(minH + col)} · ${total} meeting${total === 1 ? '' : 's'}`
        : '';
      if (total) descParts.push(tip);
      cells += `<span class="ov-heat-cell${total ? ' has' : ''}${peak ? ' is-peak' : ''}" style="background:${bg}"`
        + (tip
          ? ` data-tip="${escapeHtml(tip)}" title="${escapeHtml(tip)}" tabindex="0" role="img" aria-label="${escapeHtml(tip)}"`
          : ' aria-hidden="true"')
        + '></span>';
    }
    cells += `<span class="ov-heat-total" title="${dayCount[w]} meeting${dayCount[w] === 1 ? '' : 's'} on ${_OV_DAYS[w]}s">${dayCount[w] || ''}</span>`;
  }

  const caption =
    `<div class="ov-heat-caption">`
    + `<span>Busiest <b>${escapeHtml(_OV_DAYS_FULL[busiestWd])}</b></span>`
    + `<span>Peaks at <b>${escapeHtml(_ovHourClock(peakH))}</b></span>`
    + `<span>Longest <b>${escapeHtml(_dashHours(longestSec) || '0m')}</b></span>`
    + cadenceSeg
    + `</div>`;

  heatEl.innerHTML =
    `<div class="ov-heat-grid" style="--ov-cols:${cols}">${cells}</div>`
    + '<div class="ov-heat-scale"><span class="ov-heat-scale-label">Less</span>'
    + '<span class="ov-heat-swatch" style="background:color-mix(in srgb, var(--accent) 16%, var(--surface2))"></span>'
    + '<span class="ov-heat-swatch" style="background:color-mix(in srgb, var(--accent) 40%, var(--surface2))"></span>'
    + '<span class="ov-heat-swatch" style="background:color-mix(in srgb, var(--accent) 65%, var(--surface2))"></span>'
    + '<span class="ov-heat-swatch" style="background:color-mix(in srgb, var(--accent) 90%, var(--surface2))"></span>'
    + '<span class="ov-heat-scale-label">More</span></div>'
    + caption;

  if (noteEl) noteEl.textContent = 'Weekday by hour';
  if (descEl) {
    descEl.textContent = 'Meetings by weekday and hour. '
      + (descParts.join('. ') || 'No meetings yet') + '.';
  }
}

/* ── C. Next ─────────────────────────────────────────────────────────────────
 * Today and the next two days as a short agenda, from the same loaded range.
 * Not connected shows one restrained line, not an empty panel.
 * ─────────────────────────────────────────────────────────────────────────── */

function _homeNextDayLabel(dayKey, todayKey) {
  if (dayKey === todayKey) return 'Today';
  const tomorrow = new Date(todayKey + 'T12:00:00');
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (dayKey === _homeDayKey(tomorrow)) return 'Tomorrow';
  return new Date(dayKey + 'T12:00:00')
    .toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
}

function _homeNextRow(e) {
  const start = new Date(e.start);
  const time = e.all_day ? 'All day' : _homeClock(start);
  let stateHtml = '';
  if (e.state === 'recorded') stateHtml = '<span class="next-state next-state-recorded">Recorded</span>';
  else if (e.state === 'recording') stateHtml = '<span class="next-state next-state-live">Live</span>';
  else if (e.state === 'missed') stateHtml = '<span class="next-state next-state-missed">Not recorded</span>';
  const inner = `<span class="next-time">${escapeHtml(time)}</span>`
    + `<span class="next-title">${escapeHtml(e.title || 'Untitled')}</span>${stateHtml}`;
  if (e.session_id) {
    return `<a class="dash-next-row is-link" href="/session?id=${encodeURIComponent(e.session_id)}">${inner}</a>`;
  }
  return `<div class="dash-next-row">${inner}</div>`;
}

function _renderNext() {
  const body = document.getElementById('dash-next-body');
  if (!body) return;

  const range = _homeWeekRange();
  const status = AppData.get('calendarStatus');
  const enabled = !!(status && (status.enabled != null ? status.enabled : status.calendar_enabled));

  if (!enabled) {
    body.innerHTML = '<p class="dash-next-connect">Connect your calendar to see what is next. '
      + '<a href="/session?settings=1&amp;section=calendar">Connect your calendar</a></p>';
    return;
  }

  const payload = AppData.get('calendarEvents', range.rangeKey);
  const events = (payload && payload.events) || [];
  const days = [0, 1, 2].map(n => {
    const d = new Date(range.today);
    d.setDate(d.getDate() + n);
    return _homeDayKey(d);
  });
  const daySet = new Set(days);

  const byDay = new Map();
  for (const e of events) {
    const key = _homeDayKey(new Date(e.start));
    if (!daySet.has(key)) continue;
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key).push(e);
  }

  if (!days.some(k => (byDay.get(k) || []).length)) {
    body.innerHTML = '<p class="dash-next-empty">Nothing scheduled.</p>';
    return;
  }

  let html = '';
  for (const key of days) {
    const evs = (byDay.get(key) || []).slice().sort((a, b) => String(a.start).localeCompare(String(b.start)));
    if (!evs.length) continue;
    html += `<div class="dash-next-day"><h3 class="dash-next-daylabel">${escapeHtml(_homeNextDayLabel(key, range.todayKey))}</h3>`;
    for (const e of evs) html += _homeNextRow(e);
    html += '</div>';
  }
  body.innerHTML = html;
}

/* ── Timeline and histogram tooltip ──────────────────────────────────────────
 * One shared, body level tooltip so nothing is clipped by an overflow. It hides
 * on blur and when the view is deactivated.
 * ─────────────────────────────────────────────────────────────────────────── */

let _homeTipEl = null;
function _homeTip() {
  if (!_homeTipEl) {
    _homeTipEl = document.createElement('div');
    _homeTipEl.className = 'home-tip';
    _homeTipEl.setAttribute('role', 'presentation');
    document.body.appendChild(_homeTipEl);
  }
  return _homeTipEl;
}
function _homeShowTip(target) {
  const text = target.getAttribute('data-tip');
  if (!text) return;
  const tip = _homeTip();
  tip.textContent = text;
  tip.classList.add('show');
  const r = target.getBoundingClientRect();
  const tr = tip.getBoundingClientRect();
  let left = r.left + r.width / 2 - tr.width / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
  let top = r.top - tr.height - 8;
  if (top < 8) top = r.bottom + 8;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}
function _homeHideTip() { if (_homeTipEl) _homeTipEl.classList.remove('show'); }
function _homeBindTips() {
  const dash = document.getElementById('dash');
  if (!dash || dash._tipsBound) return;
  dash._tipsBound = true;
  const over = e => { const t = e.target.closest('[data-tip]'); if (t) _homeShowTip(t); };
  dash.addEventListener('mouseover', over);
  dash.addEventListener('mouseout', e => { if (e.target.closest('[data-tip]')) _homeHideTip(); });
  dash.addEventListener('focusin', over);
  dash.addEventListener('focusout', _homeHideTip);
}

/* ── The Home view's lifecycle ────────────────────────────────────────────── */

Views.register('home', {
  activate() {
    // Renders from the store; only an idle slice reaches the network.
    AppData.load('analytics');
    AppData.load('calendarStatus');
    AppData.load('calendarEvents', { key: _homeWeekRange().rangeKey });
    _homeBindTips();
    loadAnalytics();
  },
  deactivate() {
    _homeHideTip();
  },
});

AppData.subscribe(['analytics', 'sessions', 'attention', 'calendarStatus', 'calendarEvents'], () => {
  if (Views.current === 'home') loadAnalytics();
});

/* ── Search ────────────────────────────────────────────────────────────────── */

let _homeSearchDebounce = null;
let _homeSearchQuery = '';
let _homeSearchResults = new Map(); // session_id -> { title, matches[] }
let _homeSearchFtsPending = false;
let _homeSearchSemanticPending = false;
let _homeSemanticReady = false;

function _initSearch() {
  const input = document.getElementById('home-search-input');
  input.addEventListener('input', () => {
    const q = input.value.trim();
    document.getElementById('home-search-clear').classList.toggle('hidden', !q);
    _onHomeSearch(q);
  });

  // Refocus results on input focus if there's a query
  input.addEventListener('focus', () => {
    if (_homeSearchQuery && _homeSearchResults.size > 0) {
      _renderHomeSearchResults();
    }
  });

  document.addEventListener('click', e => {
    const results = document.getElementById('home-search-results');
    const searchWrap = document.querySelector('.home-search-wrap');
    if (!results) return;
    if (searchWrap?.contains(e.target) || results.contains(e.target)) return;
    results.classList.add('hidden');
  });

  // Check if semantic search is available
  _checkHomeSemanticReady();
}

async function _checkHomeSemanticReady() {
  const badge = document.getElementById('home-search-ai');
  let loading = false;
  try {
    const res = await fetch('/api/search/semantic/status');
    const data = await res.json();
    _homeSemanticReady = !!data.ready;
    loading = !!data.loading;
    if (badge) badge.classList.toggle('ready', _homeSemanticReady);
  } catch {}
  // Re-check only while the model is actively loading (matches app.js). This
  // terminates in every non-loading state - ready or unavailable - instead of
  // polling /api/search/semantic/status forever, and pauses while backgrounded.
  if (!_homeSemanticReady && loading && !document.hidden) {
    setTimeout(_checkHomeSemanticReady, 10000);
  }
}

function _onHomeSearch(value) {
  _homeSearchQuery = value;
  clearTimeout(_homeSearchDebounce);

  if (!_homeSearchQuery) {
    _homeSearchResults = new Map();
    _homeSearchFtsPending = false;
    _homeSearchSemanticPending = false;
    document.getElementById('home-search-results').classList.add('hidden');
    return;
  }

  // Pulse the glow
  _pulseHomeSearchGlow();

  // Instant client-side title filter (reuse sidebar's session data if available)
  const sessions = (typeof _sidebarAllSessions !== 'undefined') ? _sidebarAllSessions : [];
  const q = _homeSearchQuery.toLowerCase();
  const titleMatches = new Map();
  for (const s of sessions) {
    if (s.title && s.title.toLowerCase().includes(q)) {
      titleMatches.set(s.id, {
        title: s.title,
        matches: [{ kind: 'title', snippet: _homeHighlight(s.title, q) }],
      });
    }
  }

  _homeSearchResults = titleMatches;
  _homeSearchFtsPending = true;
  _homeSearchSemanticPending = _homeSemanticReady;
  _renderHomeSearchResults();

  // Debounced backend searches
  _homeSearchDebounce = setTimeout(() => {
    _runHomeFtsSearch(_homeSearchQuery);
    if (_homeSemanticReady) _runHomeSemanticSearch(_homeSearchQuery);
  }, 250);
}

async function _runHomeFtsSearch(query) {
  if (query !== _homeSearchQuery) return;
  try {
    const data = await fetch(`/api/search?q=${encodeURIComponent(query)}`).then(r => r.json());
    if (query !== _homeSearchQuery) return;
    const merged = new Map(_homeSearchResults);
    for (const r of data) {
      if (merged.has(r.session_id)) {
        const existing = merged.get(r.session_id);
        const contentMatches = r.matches.filter(m => m.kind !== 'title');
        existing.matches = [...existing.matches, ...contentMatches].slice(0, 3);
      } else {
        merged.set(r.session_id, { title: r.title, matches: r.matches });
      }
    }
    _homeSearchFtsPending = false;
    _homeSearchResults = merged;
    _renderHomeSearchResults();
  } catch {
    _homeSearchFtsPending = false;
  }
}

async function _runHomeSemanticSearch(query) {
  if (query !== _homeSearchQuery) return;
  try {
    const resp = await fetch(`/api/search/semantic?q=${encodeURIComponent(query)}`);
    if (query !== _homeSearchQuery) return;
    if (!resp.ok) { _homeSearchSemanticPending = false; _renderHomeSearchResults(); return; }
    const data = await resp.json();
    if (query !== _homeSearchQuery) return;
    const merged = new Map(_homeSearchResults);
    for (const r of data) {
      if (merged.has(r.session_id)) {
        const existing = merged.get(r.session_id);
        const semMatches = (r.matches || []).filter(m => m.kind === 'semantic');
        existing.matches = [...existing.matches, ...semMatches].slice(0, 3);
      } else {
        merged.set(r.session_id, { title: r.title, matches: r.matches || [] });
      }
    }
    _homeSearchSemanticPending = false;
    _homeSearchResults = merged;
    _renderHomeSearchResults();
  } catch {
    _homeSearchSemanticPending = false;
  }
}

function _renderHomeSearchResults() {
  const container = document.getElementById('home-search-results');
  const isPending = _homeSearchFtsPending || _homeSearchSemanticPending;

  if (_homeSearchResults.size === 0 && !isPending) {
    container.innerHTML = '<div class="home-search-empty">No results found</div>';
    container.classList.remove('hidden');
    return;
  }

  let html = '<div class="home-search-glow"></div>';

  if (isPending && _homeSearchResults.size === 0) {
    html += `<div class="home-search-loading">
      <div class="home-search-spinner"></div>
      <span>Searching${_homeSearchSemanticPending ? ' with AI' : ''}...</span>
    </div>`;
  }

  let count = 0;
  for (const [sid, entry] of _homeSearchResults) {
    if (count >= 8) break;
    const title = entry.title || sid;
    const matchHtml = (entry.matches || []).slice(0, 2).map(m => {
      const kindCls = m.kind || 'content';
      const kindLabel = kindCls === 'participant' ? '<i class="fa-solid fa-user"></i> participant'
        : kindCls === 'semantic' ? 'AI' : kindCls === 'title' ? 'title' : 'content';
      const snippet = m.snippet || '';
      return `<div class="home-search-result-snippet">
        <span class="home-search-result-kind ${kindCls}">${kindLabel}</span>${snippet}
      </div>`;
    }).join('');

    html += `
      <a href="/session?id=${sid}" class="home-search-result-item">
        <div class="home-search-result-title">${escapeHtml(title)}</div>
        ${matchHtml}
      </a>`;
    count++;
  }

  if (isPending && _homeSearchResults.size > 0) {
    html += `<div class="home-search-loading">
      <div class="home-search-spinner"></div>
      <span>${_homeSearchSemanticPending ? 'AI search' : 'Searching'}...</span>
    </div>`;
  }

  container.innerHTML = html;
  container.classList.remove('hidden');
}

function _pulseHomeSearchGlow() {
  const container = document.getElementById('home-search-results');
  const glow = container.querySelector('.home-search-glow');
  if (glow) { glow.remove(); }
}

function _homeHighlight(text, query) {
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return escapeHtml(text);
  const before = text.slice(0, idx);
  const match = text.slice(idx, idx + query.length);
  const after = text.slice(idx + query.length);
  return escapeHtml(before) + '<mark>' + escapeHtml(match) + '</mark>' + escapeHtml(after);
}

function clearHomeSearch() {
  _homeSearchQuery = '';
  _homeSearchResults = new Map();
  const input = document.getElementById('home-search-input');
  input.value = '';
  document.getElementById('home-search-clear').classList.add('hidden');
  document.getElementById('home-search-results').classList.add('hidden');
}

/* ── Initialization ───────────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  _restoreConvSidebar();
  _initSSE();
  _initSearch();
  loadConversations();
});
