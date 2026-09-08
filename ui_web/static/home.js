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

/** A talk-time total, as days, hours and minutes: "8d 2h 22m".
 *
 *  These are eight-week aggregates, so they run to hundreds of hours, and
 *  "194h 22m" leaves the reader doing the division. Zero components are
 *  dropped, so eight whole days reads "8d" and not "8d 0h 0m", and a total
 *  under a minute reads in seconds rather than rounding down to a bare "0m".
 */
function _formatDuration(seconds) {
  const total = Math.floor(Number(seconds) || 0);
  if (total <= 0) return '0m';
  if (total < 60) return `${total}s`;
  const parts = [];
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (m) parts.push(`${m}m`);
  return parts.join(' ');
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
  // The Free up space job reports as it goes; the dialog and the Storage card
  // both follow it from here.
  src.addEventListener('storage_job', e => {
    try { _toolOnJobEvent(JSON.parse(e.data)); } catch {}
  });
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
  _renderActivity(booting);
  _renderOverview(booting);
  _renderNext();
  _renderAttention(needsAttention, count, empty, booting);
  _renderStorage(booting);
  _renderPeople((data.people && data.people.length) ? data.people : _dashDerivedPeople());
  _renderReferencePanels(empty);
}

/* ── Repaint the pixel-sized charts when the dashboard changes size ──────────
 * The activity and storage SVGs are drawn at their box's real size so labels
 * never scale. One observer on the dashboard root repaints them (and rebuilds
 * the heatmap, whose label density depends on cell width) after a resize. */
let _dashResizeObs = null;
let _dashResizeTimer = null;

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
  if (root.querySelector('.act-svg')) _renderActivity(false);
  if (root.querySelector('.ov-heat-grid')) _renderOverview(false);
  if (root.querySelector('.sto-svg')) _renderStorage(false);
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

/* ── Knobs: small segmented controls whose choice is remembered ───────────────
 * Shared by Activity and Storage. A knob is a radiogroup of buttons; the
 * chosen value lives in a state object that is written to localStorage on
 * every change and read back (validated against the allowed values, so a
 * stale or hand-edited entry can never wedge the chart) on load. */

function _homeLoadKnobs(key, defaults, allowed) {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { saved = null; }
  const out = { ...defaults };
  if (saved && typeof saved === 'object') {
    for (const k of Object.keys(defaults)) {
      const options = allowed[k];
      const ok = options ? options.some(o => o[0] === saved[k])
                         : (typeof saved[k] === typeof defaults[k]);
      if (ok) out[k] = saved[k];
    }
  }
  return out;
}

function _homeSaveKnobs(key, state) {
  try { localStorage.setItem(key, JSON.stringify(state)); } catch (_) {}
}

/** One segmented control. ``options`` is [[value, label], ...]; ``disabled``
 *  a Set of values that cannot be chosen right now (still shown, so the row
 *  does not jump). */
function _homeKnobGroup(name, options, current, label, disabled) {
  const btns = options.map(([val, text]) => {
    const on = val === current;
    const off = disabled && disabled.has(val);
    return `<button type="button" class="dash-seg-btn${on ? ' is-on' : ''}" role="radio"`
      + ` aria-checked="${on}" data-val="${escapeHtml(String(val))}"${off ? ' disabled' : ''}>${escapeHtml(text)}</button>`;
  }).join('');
  return `<div class="dash-seg" role="radiogroup" aria-label="${escapeHtml(label)}" data-knob="${name}">${btns}</div>`;
}

/** A single on/off knob, for Details. */
function _homeKnobToggle(name, text, on, label) {
  return `<button type="button" class="dash-seg-btn dash-seg-toggle${on ? ' is-on' : ''}" aria-pressed="${on}"`
    + ` data-knob="${name}" data-val="${on ? '0' : '1'}" aria-label="${escapeHtml(label || text)}">${escapeHtml(text)}</button>`;
}

/** Delegated clicks for both knob bars; bound once per page. Arrow keys move
 *  between the buttons of a group, so the radiogroup role is honest. */
function _homeBindKnobs() {
  for (const [id, onChange] of [['home-activity-knobs', _actOnKnob], ['home-storage-knobs', _stoOnKnob]]) {
    const bar = document.getElementById(id);
    if (!bar || bar.dataset.bound) continue;
    bar.dataset.bound = '1';
    bar.addEventListener('click', e => {
      const btn = e.target.closest('.dash-seg-btn');
      if (!btn || btn.disabled) return;
      const group = btn.closest('.dash-seg');
      const knob = (group || btn).dataset.knob;
      onChange(knob, btn.dataset.val);
    });
    bar.addEventListener('keydown', e => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      const group = e.target.closest('.dash-seg');
      if (!group) return;
      const btns = [...group.querySelectorAll('.dash-seg-btn:not([disabled])')];
      const i = btns.indexOf(e.target);
      if (i < 0) return;
      e.preventDefault();
      const next = btns[(i + (e.key === 'ArrowRight' ? 1 : btns.length - 1)) % btns.length];
      next.focus();
      onChange(group.dataset.knob, next.dataset.val);
    });
  }
}

/* ── Activity: one chart, three knobs, remembered ─────────────────────────────
 * Meeting load (hours per week over 12 weeks) and Activity (minutes per day
 * over 14 days) were two views of the same numbers. This is both, and the
 * rest: what to measure (recorded time, meetings, average length), how far
 * back (two weeks to everything) and how to group (day, week, month, or
 * whatever fits the span). Everything derives from the sessions slice, so
 * turning a knob never touches the network. */

const _ACT_STORE_KEY = 'home-activity-v1';
const _ACT_MEASURES = [['time', 'Time'], ['count', 'Meetings'], ['avg', 'Avg length']];
const _ACT_SPANS = [['2w', '2w', 14], ['4w', '4w', 28], ['3m', '3m', 91], ['6m', '6m', 182],
                    ['1y', '1y', 365], ['all', 'All', 0]];
const _ACT_GROUPS = [['auto', 'Auto'], ['day', 'Day'], ['week', 'Week'], ['month', 'Month']];
// The old Meeting load view: recorded hours per week over about twelve weeks.
const _ACT_DEFAULTS = { measure: 'time', span: '3m', group: 'auto' };
// More bars than this and nothing can be read; a grouping that would need
// them steps up to the next unit instead.
const _ACT_MAX_BARS = 110;

let _actState = _homeLoadKnobs(_ACT_STORE_KEY, _ACT_DEFAULTS,
  { measure: _ACT_MEASURES, span: _ACT_SPANS, group: _ACT_GROUPS });

function _actOnKnob(knob, val) {
  if (!(knob in _ACT_DEFAULTS) || _actState[knob] === val) return;
  _actState = { ..._actState, [knob]: val };
  _homeSaveKnobs(_ACT_STORE_KEY, _actState);
  _renderActivity(false);
}

function _actBucketKey(d, unit) {
  if (unit === 'day') return d.toLocaleDateString('en-CA');
  if (unit === 'week') return _weekStartLocal(d).toLocaleDateString('en-CA');
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

/** Which unit ``group`` resolves to for a span of ``days``: Auto picks the
 *  finest that fits, an explicit choice steps up when it would not. */
function _actResolveUnit(group, days) {
  let unit = group === 'auto' ? (days <= 35 ? 'day' : (days <= 200 ? 'week' : 'month')) : group;
  if (unit === 'day' && days > _ACT_MAX_BARS) unit = 'week';
  if (unit === 'week' && days / 7 > _ACT_MAX_BARS) unit = 'month';
  return unit;
}

/** The chart's buckets for the current knobs, oldest first, gaps included, from
 *  the sessions slice. {buckets:[{start, key, count, seconds}], unit, days}. */
function _dashDerivedActivity(state) {
  state = state || _actState;
  const sessions = _dashSessions.filter(s => s.started_at);
  const now = new Date();
  const end = new Date(now);
  end.setHours(23, 59, 59, 999);
  const spanDef = _ACT_SPANS.find(o => o[0] === state.span) || _ACT_SPANS[2];
  let start = new Date(now);
  start.setHours(0, 0, 0, 0);
  if (spanDef[2] > 0) {
    start.setDate(start.getDate() - (spanDef[2] - 1));
  } else {
    let earliest = null;
    for (const s of sessions) {
      const d = new Date(s.started_at + 'Z');
      if (!Number.isNaN(d.getTime()) && (!earliest || d < earliest)) earliest = d;
    }
    if (earliest) { start = new Date(earliest); start.setHours(0, 0, 0, 0); }
  }
  const days = Math.max(1, Math.round((end - start) / 86400000));
  const unit = _actResolveUnit(state.group, days);
  if (unit === 'week') start = _weekStartLocal(start);
  if (unit === 'month') start = new Date(start.getFullYear(), start.getMonth(), 1);

  const buckets = [];
  const byKey = new Map();
  const cursor = new Date(start);
  while (cursor <= end && buckets.length < _ACT_MAX_BARS + 40) {
    const b = { start: new Date(cursor), key: _actBucketKey(cursor, unit), count: 0, seconds: 0 };
    buckets.push(b);
    byKey.set(b.key, b);
    if (unit === 'day') cursor.setDate(cursor.getDate() + 1);
    else if (unit === 'week') cursor.setDate(cursor.getDate() + 7);
    else cursor.setMonth(cursor.getMonth() + 1);
  }
  for (const s of sessions) {
    const d = new Date(s.started_at + 'Z');
    if (Number.isNaN(d.getTime()) || d < start || d > end) continue;
    const b = byKey.get(_actBucketKey(d, unit));
    if (!b) continue;
    b.count += 1;
    b.seconds += _dashDurationSec(s);
  }
  return { buckets, unit, days, spanLabel: spanDef[2] > 0 ? `last ${_actSpanWords(spanDef[0])}` : 'all time' };
}

function _actSpanWords(span) {
  return { '2w': 'two weeks', '4w': 'four weeks', '3m': 'three months',
           '6m': 'six months', '1y': 'twelve months' }[span] || span;
}

/** The value a bar stands for, in seconds for time and average length. */
function _actValue(b, measure) {
  if (measure === 'count') return b.count;
  if (measure === 'avg') return b.count ? b.seconds / b.count : 0;
  return b.seconds;
}

/** A round axis top for counts, so gridlines land on whole meetings. */
function _actNiceCount(max) {
  const steps = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150, 200];
  for (const st of steps) if (max <= st) return st;
  return Math.ceil(max / 100) * 100;
}

/** Axis top and label formatter for the measure: minutes while the top is
 *  under ninety minutes, hours above, whole meetings for counts. */
function _actScale(measure, maxValue) {
  if (measure === 'count') return { top: _actNiceCount(Math.max(maxValue, 1)), label: v => String(v) };
  const maxMin = maxValue / 60;
  if (maxMin <= 90) {
    const top = _homeNiceMinutes(Math.max(maxMin, 1));
    return { top: top * 60, label: v => _homeMinLabel(v / 60) };
  }
  const steps = [2, 3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 30, 40, 50, 60, 80, 100, 150, 200];
  const maxH = maxValue / 3600;
  let topH = steps.find(st => maxH <= st);
  if (!topH) topH = Math.ceil(maxH / 50) * 50;
  return { top: topH * 3600, label: v => `${Number.isInteger(v / 3600) ? v / 3600 : (v / 3600).toFixed(1)}h` };
}

function _actBarLabel(b, unit, isCurrent) {
  if (unit === 'day') return isCurrent ? 'Today' : _homeActShort(b.key);
  if (unit === 'week') return isCurrent ? 'This wk' : b.start.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  if (isCurrent) return 'This mo';
  const thisYear = new Date().getFullYear() === b.start.getFullYear();
  return b.start.toLocaleDateString(undefined, thisYear ? { month: 'short' } : { month: 'short', year: '2-digit' });
}

function _actBarTip(b, unit, measure) {
  const when = unit === 'day' ? _homeActLong(b.key)
    : unit === 'week' ? `Week of ${b.start.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })}`
    : b.start.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
  const meetings = `${b.count} meeting${b.count === 1 ? '' : 's'}`;
  const time = _dashHours(b.seconds) || '0m';
  if (measure === 'avg') {
    const avg = b.count ? (_dashHours(b.seconds / b.count) || '0m') : 'no meetings';
    return `${when} · ${meetings} · avg ${avg}`;
  }
  return `${when} · ${meetings} · ${time}`;
}

function _renderActivityKnobs() {
  const bar = document.getElementById('home-activity-knobs');
  if (!bar) return;
  // Groupings that would draw more bars than can be read are shown disabled;
  // Auto is always available.
  const spanDef = _ACT_SPANS.find(o => o[0] === _actState.span) || _ACT_SPANS[2];
  const days = spanDef[2] > 0 ? spanDef[2] : _dashDerivedActivity({ ..._actState, group: 'month' }).days;
  const disabled = new Set();
  if (days > _ACT_MAX_BARS) disabled.add('day');
  if (days / 7 > _ACT_MAX_BARS) disabled.add('week');
  const html = _homeKnobGroup('measure', _ACT_MEASURES, _actState.measure, 'Measure')
    + _homeKnobGroup('span', _ACT_SPANS.map(o => [o[0], o[1]]), _actState.span, 'How far back')
    + _homeKnobGroup('group', _ACT_GROUPS, _actState.group, 'Group by', disabled);
  if (bar.innerHTML !== html) _dashMorph(bar, html);
}

/** The hero chart: an inline SVG bar chart of the current knobs' buckets,
 *  drawn at the box's real pixel size so labels never scale. */
function _renderActivity(booting) {
  const chart = document.getElementById('home-activity-chart');
  const note = document.getElementById('home-activity-summary');
  const desc = document.getElementById('home-activity-desc');
  if (!chart) return;
  _renderActivityKnobs();
  if (booting) {
    chart.innerHTML = '<div class="home-activity-skel skeleton"></div>';
    if (note) note.textContent = '';
    return;
  }

  const { buckets, unit, spanLabel } = _dashDerivedActivity(_actState);
  const measure = _actState.measure;
  const totalCount = buckets.reduce((a, b) => a + b.count, 0);
  const totalSeconds = buckets.reduce((a, b) => a + b.seconds, 0);
  const unitWord = { day: 'day', week: 'week', month: 'month' }[unit];
  if (!totalCount) {
    chart.innerHTML = `<p class="home-activity-empty">No recordings in the ${spanLabel === 'all time' ? 'library yet' : spanLabel}. Widen the span, or record a meeting.</p>`;
    if (note) note.textContent = '';
    if (desc) desc.textContent = '';
    return;
  }
  if (note) {
    const what = measure === 'count' ? 'meetings' : measure === 'avg' ? 'average length' : 'recorded time';
    note.textContent = `${totalCount} meeting${totalCount === 1 ? '' : 's'} · ${_dashHours(totalSeconds) || '0m'} · ${what} per ${unitWord}, ${spanLabel}`;
  }

  const values = buckets.map(b => _actValue(b, measure));
  const scale = _actScale(measure, Math.max(...values, measure === 'count' ? 1 : 60));

  if (!chart.clientWidth) _dashRetryPaint();   // hidden right now: repaint once it has a size
  const W = chart.clientWidth || 720;
  const H = chart.clientHeight || 200;
  const padL = 40, padR = 12, padT = 18, padB = 26;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const n = buckets.length;
  const slot = innerW / n;
  const barW = Math.max(3, Math.min(48, slot * 0.62));
  const baseY = padT + innerH;
  const yFor = v => baseY - (v / scale.top) * innerH;
  // Wider slots earn a label under every bar and a value above each bar.
  const labelEvery = slot >= 58 ? 1 : (slot >= 34 ? 2 : (slot >= 18 ? 4 : 0));
  const showValues = slot >= 52;

  let grid = '';
  for (const gv of [scale.top, scale.top / 2]) {
    const y = yFor(gv);
    grid += `<line class="act-grid" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}"></line>`;
    grid += `<text class="act-ylabel" x="${padL - 6}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${escapeHtml(scale.label(gv))}</text>`;
  }
  grid += `<line class="act-baseline" x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}"></line>`;

  const midIdx = Math.floor(n / 2);
  let bars = '', xlabels = '';
  const descParts = [];
  buckets.forEach((b, i) => {
    const cx = padL + slot * i + slot / 2;
    const v = values[i];
    const h = v > 0 ? Math.max(2, (v / scale.top) * innerH) : 0;
    const isCurrent = i === n - 1;
    const tip = _actBarTip(b, unit, measure);
    descParts.push(tip);
    const cls = 'act-bar' + (v > 0 ? '' : ' act-bar-empty') + (isCurrent ? ' is-current' : '');
    const y = v > 0 ? baseY - h : baseY - 2;
    const drawH = v > 0 ? h : 2;
    bars += `<rect class="${cls}" x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${drawH.toFixed(1)}" rx="${barW >= 8 ? 3 : 1}" tabindex="0" role="img" data-tip="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}"></rect>`;
    if (showValues && v > 0) {
      const shown = measure === 'count' ? String(b.count) : _dashCompactHours(v);
      bars += `<text class="act-vlabel" x="${cx.toFixed(1)}" y="${(y - 5).toFixed(1)}" text-anchor="middle">${escapeHtml(shown)}</text>`;
    }
    const labelIt = labelEvery ? (i % labelEvery === 0 || isCurrent) : (i === 0 || i === midIdx || isCurrent);
    if (labelIt) {
      const anchor = i === 0 ? 'start' : (isCurrent ? 'end' : 'middle');
      const tx = i === 0 ? padL : (isCurrent ? W - padR : cx);
      xlabels += `<text class="act-xlabel" x="${tx.toFixed(1)}" y="${H - 6}" text-anchor="${anchor}">${escapeHtml(_actBarLabel(b, unit, isCurrent))}</text>`;
    }
  });

  const title = `${measure === 'count' ? 'Meetings' : measure === 'avg' ? 'Average meeting length' : 'Recorded time'} per ${unitWord}, ${spanLabel}`;
  chart.innerHTML =
    `<svg class="act-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="group" aria-label="${escapeHtml(title)}">`
    + grid + bars + xlabels + '</svg>';
  if (desc) desc.textContent = `${title}. ${descParts.join('. ')}.`;
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
 * Full width, straight under the hero row. Two parts, because the section
 * answers two different questions:
 *
 *   the focus strip  "what do I do right now" - whatever is running, else the
 *                    soonest thing still to come, with its countdown and its
 *                    Join button, on its own surface so it reads as an answer
 *                    rather than as another row
 *   three columns    "what does the shape of the next three days look like" -
 *                    today and the two days after it, each with its count and
 *                    its total, today carrying a line where the clock is
 *
 * Both read the one calendarEvents range the page already loads, so nothing
 * here fetches. The join URL is never on this page: rows carry the provider
 * slug and the opaque event key, and app.js's calendarJoinButton turns those
 * into the control (see "A join link never reaches the browser" in AGENT.md).
 * ─────────────────────────────────────────────────────────────────────────── */

const _NEXT_ROWS_PER_DAY = 5;
// Past this length an entry is a container, not a meeting ("Focus time",
// "Out of office"), so it never takes the focus strip. Same threshold the
// backend matcher uses for the same reason.
const _NEXT_BLOCK_MS = 4 * 3600 * 1000;
// Beyond this a countdown has stopped meaning anything and the strip says the
// day and the time instead.
const _NEXT_COUNTDOWN_MS = 12 * 3600 * 1000;
const _NEXT_TICK_MS = 30000;

function _homeNextDayLabel(dayKey, todayKey) {
  if (dayKey === todayKey) return 'Today';
  const tomorrow = new Date(todayKey + 'T12:00:00');
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (dayKey === _homeDayKey(tomorrow)) return 'Tomorrow';
  return new Date(dayKey + 'T12:00:00')
    .toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
}

/** "45m", "1h", "2h 30m". One formatter for a row, a day total and a
 *  countdown, so the three never disagree about how long an hour looks. */
function _homeDurLabel(mins) {
  if (!(mins > 0)) return '';
  if (mins < 60) return `${Math.round(mins)}m`;
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

function _nextStart(e) { return new Date(e.start).getTime(); }
function _nextEnd(e) {
  const end = e.end ? new Date(e.end).getTime() : NaN;
  return Number.isFinite(end) ? end : _nextStart(e);
}
function _nextMinutes(e) {
  if (e.all_day) return 0;
  const span = _nextEnd(e) - _nextStart(e);
  return span > 0 ? Math.round(span / 60000) : 0;
}
/** The events for the range Home already loaded. */
function _homeNextEvents(range) {
  const payload = AppData.get('calendarEvents', range.rangeKey);
  return (payload && payload.events) || [];
}
/** The Calendar view, on that month, with that day's panel open. */
function _homeNextDayHref(dayKey) {
  return `/calendar?month=${dayKey.slice(0, 7)}&amp;day=${dayKey}`;
}

/* ── The focus strip ──────────────────────────────────────────────────────── */

/** The one meeting worth a strip of its own: whatever is running now, else the
 *  soonest one still to come. All-day items and multi-hour blocks are skipped;
 *  a "Focus time" block that spans the afternoon would otherwise hold the strip
 *  all afternoon and hide the meeting you actually have to join. */
function _homeNextFocus(events, now) {
  const t = now.getTime();
  let running = null, upcoming = null;
  for (const e of events || []) {
    const start = _nextStart(e);
    if (e.all_day || !Number.isFinite(start)) continue;
    const end = _nextEnd(e);
    if (end - start >= _NEXT_BLOCK_MS) continue;
    if (start <= t && t < end) {
      // Two overlapping meetings: the one that started last is the one you are
      // most likely in.
      if (!running || start > _nextStart(running)) running = e;
    } else if (start > t) {
      if (!upcoming || start < _nextStart(upcoming)) upcoming = e;
    }
  }
  return running || upcoming || null;
}

function _homeFocusRunning(e, now) {
  const t = now.getTime();
  return _nextStart(e) <= t && t < _nextEnd(e);
}

/** Time until it starts, or how much of it is left. */
function _homeFocusCountdown(e, now, running) {
  const t = now.getTime();
  if (running) {
    const left = _nextEnd(e) - t;
    if (left <= 60000) return 'ending now';
    return `${_homeDurLabel(left / 60000)} left`;
  }
  const until = _nextStart(e) - t;
  if (until <= 60000) return 'starting now';
  if (until > _NEXT_COUNTDOWN_MS) return '';
  return `in ${_homeDurLabel(until / 60000)}`;
}

function _renderNextFocus(focus, range, now) {
  const box = document.getElementById('dash-next-focus');
  if (!box) return;
  if (!focus) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }

  const start = new Date(focus.start);
  const running = _homeFocusRunning(focus, now);
  const recording = focus.state === 'recording';
  // Urgency reads as colour: green while it is being recorded, accent while it
  // is happening, quiet while it is still ahead.
  let flag = 'Next up', cls = 'is-next';
  if (recording) { flag = 'Recording now'; cls = 'is-recording'; }
  else if (running) { flag = 'Happening now'; cls = 'is-now'; }

  const meta = [];
  const dayKey = _homeDayKey(start);
  if (dayKey !== range.todayKey) meta.push(_homeNextDayLabel(dayKey, range.todayKey));
  meta.push(focus.end
    ? `${_homeClock(start)} to ${_homeClock(new Date(focus.end))}`
    : _homeClock(start));
  const mins = _nextMinutes(focus);
  if (mins > 0) meta.push(_homeDurLabel(mins));
  if (focus.join_label) meta.push(focus.join_label);
  if (focus.status === 'tentative') meta.push('Tentative');

  const countdown = _homeFocusCountdown(focus, now, running);
  const acts = [calendarJoinButton(focus.key, focus.join, focus.join_label,
                                   'dash-next-join is-solid')];
  if (focus.session_id) {
    acts.push(`<a class="btn btn-secondary next-focus-open"`
      + ` href="/session?id=${encodeURIComponent(focus.session_id)}">Open recording</a>`);
  }

  box.hidden = false;
  _dashMorph(box, `
    <div class="next-focus ${cls}">
      <span class="next-focus-flag">
        <span class="next-focus-dot" aria-hidden="true"></span>${escapeHtml(flag)}
      </span>
      <span class="next-focus-main">
        <span class="next-focus-title">${escapeHtml(focus.title || 'Untitled')}</span>
        <span class="next-focus-meta">${escapeHtml(meta.join(' · '))}</span>
      </span>
      <span class="next-focus-count"${countdown ? '' : ' hidden'}>${escapeHtml(countdown)}</span>
      <span class="next-focus-act">${acts.filter(Boolean).join('')}</span>
    </div>`);
}

/* ── The three day columns ────────────────────────────────────────────────── */

function _homeNextRow(e, now) {
  const start = new Date(e.start);
  const t = now.getTime();
  const past = !e.all_day && _nextEnd(e) <= t;
  const current = !e.all_day && _nextStart(e) <= t && t < _nextEnd(e);

  let chip = '';
  if (e.state === 'recorded') chip = '<span class="next-state next-state-recorded">Recorded</span>';
  else if (e.state === 'recording') chip = '<span class="next-state next-state-live">Live</span>';
  else if (e.state === 'missed') chip = '<span class="next-state next-state-missed">Not recorded</span>';

  // What the meta line is for: which app Join will open, and any flag on the
  // invite. Empty for a plain meeting with no link, and then the row is one
  // line instead of two.
  const bits = [];
  if (e.join_label) bits.push(e.join_label);
  if (e.status === 'tentative') bits.push('Tentative');
  const meta = bits.length
    ? `<span class="next-meta">${escapeHtml(bits.join(' · '))}</span>` : '';

  const len = _homeDurLabel(_nextMinutes(e));
  const inner = '<span class="next-when">'
    + `<span class="next-at">${escapeHtml(e.all_day ? 'All day' : _homeClock(start))}</span>`
    + (len ? `<span class="next-len">${escapeHtml(len)}</span>` : '')
    + '</span>'
    + '<span class="next-main">'
    + `<span class="next-titlerow"><span class="next-title">${escapeHtml(e.title || 'Untitled')}</span>${chip}</span>`
    + meta
    + '</span>';

  const cls = ['dash-next-row'];
  if (past) cls.push('is-past');
  if (current) cls.push('is-current');
  const row = e.session_id
    ? `<a class="${cls.join(' ')} is-link" href="/session?id=${encodeURIComponent(e.session_id)}"`
      + ` title="Open this recording">${inner}</a>`
    : `<div class="${cls.join(' ')}">${inner}</div>`;
  // A button cannot live inside the row's own link, so a joinable meeting
  // gets a wrapper and everything else keeps the markup it always had.
  const join = calendarJoinButton(e.key, e.join, e.join_label, 'dash-next-join');
  return join ? `<div class="dash-next-item">${row}${join}</div>` : row;
}

function _homeNextDayColumn(dayKey, evs, range, now) {
  const isToday = dayKey === range.todayKey;
  const label = escapeHtml(_homeNextDayLabel(dayKey, range.todayKey));
  let head = '<div class="dash-next-dayhead">'
    + `<h3 class="dash-next-daylabel${isToday ? ' is-today' : ''}">${label}</h3>`;
  if (evs.length) {
    const parts = [`${evs.length} meeting${evs.length === 1 ? '' : 's'}`];
    const total = _homeDurLabel(evs.reduce((sum, e) => sum + _nextMinutes(e), 0));
    if (total) parts.push(total);
    head += `<span class="dash-next-daymeta">${escapeHtml(parts.join(' · '))}</span>`;
  }
  head += '</div>';

  if (!evs.length) {
    return `<div class="dash-next-day">${head}`
      + '<p class="dash-next-dayempty">Nothing scheduled</p></div>';
  }

  const shown = evs.slice(0, _NEXT_ROWS_PER_DAY);
  const t = now.getTime();
  let rows = '';
  // A hairline where the clock is, drawn only between something that has
  // finished and something that has not. At the top or the bottom of the
  // column it would mark nothing.
  let sawPast = false, drawn = !isToday;
  for (const e of shown) {
    const done = !e.all_day && _nextEnd(e) <= t;
    if (!drawn && sawPast && !done) {
      rows += '<div class="dash-next-now" aria-hidden="true"></div>';
      drawn = true;
    }
    rows += _homeNextRow(e, now);
    if (done) sawPast = true;
  }

  const hidden = evs.length - shown.length;
  const more = hidden > 0
    ? `<a class="dash-next-more" href="${_homeNextDayHref(dayKey)}">${hidden} more</a>`
    : '';
  return `<div class="dash-next-day">${head}${rows}${more}</div>`;
}

function _homeNextDaySkeleton(dayKey, todayKey) {
  const row = '<div class="dash-next-row dash-next-row-skel">'
    + '<span class="skeleton skeleton-chip"></span>'
    + '<span class="skeleton skeleton-line"></span></div>';
  return '<div class="dash-next-day"><div class="dash-next-dayhead">'
    + `<h3 class="dash-next-daylabel">${escapeHtml(_homeNextDayLabel(dayKey, todayKey))}</h3>`
    + '</div>' + row.repeat(3) + '</div>';
}

/* ── The section ──────────────────────────────────────────────────────────── */

function _renderNext() {
  const body = document.getElementById('dash-next-body');
  const note = document.getElementById('dash-next-note');
  const link = document.getElementById('dash-next-all');
  const focusBox = document.getElementById('dash-next-focus');
  if (!body) return;

  const range = _homeWeekRange();
  const status = AppData.get('calendarStatus');
  const enabled = !!(status && (status.enabled != null ? status.enabled : status.calendar_enabled));
  if (focusBox) { focusBox.hidden = true; focusBox.innerHTML = ''; }
  if (note) note.textContent = '';
  if (link) link.classList.toggle('hidden', !enabled);

  // One restrained line, not an empty three column layout.
  if (!enabled) {
    body.classList.add('is-flat');
    body.innerHTML = '<p class="dash-next-connect">Connect a published calendar and the next '
      + 'three days land here, each meeting one click from its own app. '
      + '<a href="/session?settings=1&amp;section=calendar">Connect your calendar</a></p>';
    return;
  }

  const events = _homeNextEvents(range);
  const days = [0, 1, 2].map(n => {
    const d = new Date(range.today);
    d.setDate(d.getDate() + n);
    return _homeDayKey(d);
  });

  const slice = AppData.status('calendarEvents', range.rangeKey);
  if (!events.length && slice === 'error') {
    body.classList.add('is-flat');
    body.innerHTML = '<p class="dash-next-empty">Could not load your calendar.</p>';
    return;
  }
  // First load of the range: three skeleton columns, so the section does not
  // flash "nothing scheduled" at a calendar that is still arriving.
  if (!events.length && slice !== 'ready') {
    body.classList.remove('is-flat');
    body.innerHTML = days.map(k => _homeNextDaySkeleton(k, range.todayKey)).join('');
    return;
  }

  const byDay = new Map(days.map(k => [k, []]));
  for (const e of events) {
    const key = _homeDayKey(new Date(e.start));
    if (byDay.has(key)) byDay.get(key).push(e);
  }
  // All-day items head their day, then everything else by the clock.
  for (const list of byDay.values()) {
    list.sort((a, b) => (!!a.all_day === !!b.all_day)
      ? String(a.start).localeCompare(String(b.start))
      : (a.all_day ? -1 : 1));
  }

  const total = days.reduce((n, k) => n + byDay.get(k).length, 0);
  if (note) {
    note.textContent = total
      ? `${total} meeting${total === 1 ? '' : 's'} over three days`
      : 'Today and the next two days';
  }
  if (!total) {
    body.classList.add('is-flat');
    body.innerHTML = '<p class="dash-next-empty">Nothing scheduled for the next three days.</p>';
    return;
  }

  const now = new Date();
  _renderNextFocus(_homeNextFocus(events, now), range, now);
  body.classList.remove('is-flat');
  // Keyed update: the countdown reruns this every half minute and a wholesale
  // innerHTML swap would drop focus off a Join button mid-tab.
  _dashMorph(body, days.map(k => _homeNextDayColumn(k, byDay.get(k), range, now)).join(''));
}

/* ── The clock ────────────────────────────────────────────────────────────────
 * The countdown, the "happening now" flag and today's now line all move on
 * their own, so Home keeps a slow clock for as long as it is the visible view
 * and drops it the moment it is not.
 * ─────────────────────────────────────────────────────────────────────────── */

let _homeNextTimer = null;

function _homeStartNextClock() {
  if (_homeNextTimer) return;
  _homeNextTimer = setInterval(() => {
    if (Views.current !== 'home') return;
    // Past midnight the three days, and so the cache key, are different ones.
    // A ready or in-flight range makes this a no-op; a brand new one gets
    // loaded, which is what stops the section sitting on skeletons.
    AppData.load('calendarEvents', { key: _homeWeekRange().rangeKey });
    _renderNext();
  }, _NEXT_TICK_MS);
}

function _homeStopNextClock() {
  if (!_homeNextTimer) return;
  clearInterval(_homeNextTimer);
  _homeNextTimer = null;
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

/* ── Storage: what the recordings cost on disk ────────────────────────────────
 * Drawn from the storage slice (/api/dashboard/storage, core/disk_usage.py):
 * bytes per kind, per meeting, per backup folder, and what belongs to no
 * meeting any more. Four views (by type, by meeting, by month, by folder), a
 * span, a top-N and a details table, all remembered in localStorage like the
 * Activity knobs. The Free up space button opens the compression tool below. */

const _STO_STORE_KEY = 'home-storage-v1';
const _STO_VIEWS = [['type', 'By type'], ['meeting', 'By meeting'], ['month', 'By month'], ['folder', 'By folder']];
const _STO_SPANS = [['all', 'All'], ['1y', '1y'], ['6m', '6m'], ['3m', '3m']];
const _STO_SPAN_DAYS = { all: 0, '1y': 365, '6m': 182, '3m': 91 };
const _STO_TOPS = [['8', 'Top 8'], ['15', 'Top 15'], ['30', 'Top 30']];
const _STO_DEFAULTS = { view: 'type', span: 'all', top: '8', detail: false };
const _STO_KIND_LABELS = { audio: 'Audio', video: 'Video', frames: 'Frames', backups: 'Backups',
                           other: 'Other', unused: 'Unused' };
const _STO_KIND_ORDER = ['audio', 'video', 'frames', 'backups', 'other', 'unused'];

let _stoState = _homeLoadKnobs(_STO_STORE_KEY, _STO_DEFAULTS,
  { view: _STO_VIEWS, span: _STO_SPANS, top: _STO_TOPS });

function _stoOnKnob(knob, val) {
  if (knob === 'detail') {
    _stoState = { ..._stoState, detail: val === '1' };
  } else if (knob in _STO_DEFAULTS && _stoState[knob] !== val) {
    _stoState = { ..._stoState, [knob]: val };
  } else {
    return;
  }
  _homeSaveKnobs(_STO_STORE_KEY, _stoState);
  _renderStorage(false);
}

/** "1.4 GB", "57.2 GB", "181 GB": 1024-based like the file manager, one
 *  decimal until a hundred, none above. */
function _fmtBytes(n) {
  n = Number(n) || 0;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = n, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const shown = i === 0 ? String(Math.round(v)) : (v >= 100 ? String(Math.round(v)) : v.toFixed(1));
  return `${shown} ${units[i]}`;
}

function _stoDate(rec) {
  const d = new Date(String(rec.started_at || '').replace(' ', 'T') + 'Z');
  return Number.isNaN(d.getTime()) ? null : d;
}

function _stoInSpan(rec, span) {
  const days = _STO_SPAN_DAYS[span] || 0;
  if (!days) return true;
  const d = _stoDate(rec);
  return !!d && d.getTime() >= Date.now() - days * 86400000;
}

/** A meeting's bytes by kind. Tracks count as audio; fragments as other. */
function _stoKinds(rec) {
  return {
    audio: (rec.audio_bytes || 0) + (rec.audio_tracks_bytes || 0),
    video: rec.video_bytes || 0,
    frames: rec.frames_bytes || 0,
    backups: rec.backups_bytes || 0,
    other: (rec.other_bytes || 0) + (rec.leftover_bytes || 0),
  };
}

function _stoSum(kinds) {
  return Object.values(kinds).reduce((a, b) => a + b, 0);
}

/** Bytes by kind for the whole folder (span "all"): the kinds the scan counted,
 *  with orphaned files pulled out of their directory kind into "unused". */
function _stoFolderKinds(report) {
  const bk = report.by_kind || {};
  const orphanBy = {};
  for (const o of (report.orphans && report.orphans.items) || []) {
    orphanBy[o.kind] = (orphanBy[o.kind] || 0) + (o.bytes || 0);
  }
  const at = k => ((bk[k] && bk[k].bytes) || 0);
  return {
    audio: Math.max(0, at('audio') - (orphanBy.audio || 0)),
    video: Math.max(0, at('video') - (orphanBy.video || 0)),
    frames: Math.max(0, at('frames') - (orphanBy.frames || 0)),
    backups: at('backups'),
    other: at('attachments') + at('database') + at('profiles') + at('tmp') + at('other'),
    unused: (report.orphans && report.orphans.bytes) || 0,
  };
}

function _stoSegments(kinds, total, tipPrefix) {
  return _STO_KIND_ORDER.filter(k => kinds[k] > 0).map(k => {
    const share = total ? kinds[k] / total : 0;
    const tip = `${tipPrefix ? tipPrefix + ' · ' : ''}${_STO_KIND_LABELS[k]} · ${_fmtBytes(kinds[k])} · ${Math.round(share * 100)}%`;
    return `<span class="sto-seg sto-k-${k}" style="flex-basis:${(share * 100).toFixed(2)}%" data-tip="${escapeHtml(tip)}" role="img" aria-label="${escapeHtml(tip)}"></span>`;
  }).join('');
}

function _stoLegend(kinds, total, notes) {
  const rows = _STO_KIND_ORDER.filter(k => kinds[k] > 0)
    .sort((a, b) => kinds[b] - kinds[a])
    .map(k => `<li class="sto-legend-row"><span class="sto-dot sto-k-${k}"></span>`
      + `<span class="sto-legend-name">${_STO_KIND_LABELS[k]}</span>`
      + `<span class="sto-legend-val">${_fmtBytes(kinds[k])}</span>`
      + `<span class="sto-legend-share">${total ? Math.round(kinds[k] / total * 100) : 0}%</span>`
      + (notes && notes[k] ? `<span class="sto-legend-note">${escapeHtml(notes[k])}</span>` : '')
      + '</li>').join('');
  return `<ul class="sto-legend">${rows}</ul>`;
}

/** The root folder a folder belongs to (subfolders fold into it). */
function _stoRootOf(folderId, byId) {
  let cur = byId.get(folderId);
  let guard = 0;
  while (cur && cur.parent_id && byId.has(cur.parent_id) && guard++ < 50) cur = byId.get(cur.parent_id);
  return cur || null;
}

function _stoMonthKey(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; }

/** A round axis top in bytes, on 1, 2, 5 steps of MB, GB or TB. */
function _stoNiceBytes(max) {
  if (max <= 0) return 1024 * 1024;
  const steps = [1, 2, 5];
  let unit = 1024 * 1024;
  while (unit * 1000 < max) unit *= 1024;
  for (let mult = 1; mult <= 1000; mult *= 10) {
    for (const st of steps) {
      if (max <= st * mult * unit) return st * mult * unit;
    }
  }
  return max;
}

function _renderStorageKnobs(hasData) {
  const bar = document.getElementById('home-storage-knobs');
  if (!bar) return;
  if (!hasData) { if (bar.innerHTML) bar.innerHTML = ''; return; }
  let html = _homeKnobGroup('view', _STO_VIEWS, _stoState.view, 'View');
  html += _homeKnobGroup('span', _STO_SPANS, _stoState.span, 'Meetings from');
  if (_stoState.view === 'meeting') html += _homeKnobGroup('top', _STO_TOPS, _stoState.top, 'How many');
  html += _homeKnobToggle('detail', 'Details', !!_stoState.detail, 'Show a table under the chart');
  if (bar.innerHTML !== html) _dashMorph(bar, html);
}

function _renderStorage(booting) {
  const chart = document.getElementById('home-storage-chart');
  const detail = document.getElementById('home-storage-detail');
  const note = document.getElementById('home-storage-note');
  const summary = document.getElementById('home-storage-summary');
  const button = document.getElementById('home-storage-free');
  const desc = document.getElementById('home-storage-desc');
  if (!chart) return;
  const report = AppData.get('storage');
  const status = AppData.status('storage');
  if (booting || (!report && status !== 'error')) {
    _renderStorageKnobs(false);
    chart.innerHTML = '<div class="home-storage-skel skeleton"></div>';
    if (detail) { detail.hidden = true; detail.innerHTML = ''; }
    if (note) note.textContent = '';
    if (summary) summary.textContent = '';
    if (button) button.hidden = true;
    return;
  }
  if (!report) {
    _renderStorageKnobs(false);
    chart.innerHTML = `<p class="home-storage-empty">Storage figures are unavailable right now${AppData.error('storage') ? ` (${escapeHtml(AppData.error('storage'))})` : ''}.</p>`;
    if (detail) { detail.hidden = true; detail.innerHTML = ''; }
    if (button) button.hidden = true;
    return;
  }
  _renderStorageKnobs(true);
  const view = _stoState.view;
  const span = _stoState.span;
  const sessions = (report.sessions || []).filter(r => _stoInSpan(r, span));
  const spanWords = span === 'all' ? 'all time' : `last ${_actSpanWords(span === '1y' ? '1y' : span)}`;
  let descText = '';

  if (view === 'type') descText = _stoRenderType(chart, detail, report, sessions, span);
  else if (view === 'meeting') descText = _stoRenderMeetings(chart, detail, report, sessions);
  else if (view === 'month') descText = _stoRenderMonths(chart, detail, report, sessions);
  else descText = _stoRenderFolders(chart, detail, report, sessions);

  if (detail) detail.hidden = !_stoState.detail;
  if (note) {
    const total = span === 'all' ? report.totals.bytes : sessions.reduce((a, r) => a + _stoSum(_stoKinds(r)), 0);
    const n = span === 'all' ? (report.sessions || []).length : sessions.length;
    note.textContent = `${_fmtBytes(total)} · ${n} meeting${n === 1 ? '' : 's'} · ${spanWords}`;
  }
  if (summary) {
    const parts = [];
    if (report.disk) parts.push(`${_fmtBytes(report.disk.free)} free of ${_fmtBytes(report.disk.total)} on this drive`);
    const wav = report.audio_formats && report.audio_formats.wav;
    if (wav && wav.files) parts.push(`${wav.files} recording${wav.files === 1 ? '' : 's'} still uncompressed`);
    if (report.orphans && report.orphans.bytes) parts.push(`${_fmtBytes(report.orphans.bytes)} in files that belong to no meeting`);
    summary.textContent = parts.join(' · ');
  }
  if (button) button.hidden = false;
  if (desc) desc.textContent = descText;
}

/* By type: one stacked bar of the whole folder and a legend. With a span it
 * becomes the in-span meetings' own bytes, so the two can be compared. */
function _stoRenderType(chart, detail, report, sessions, span) {
  const kinds = span === 'all' ? _stoFolderKinds(report)
    : sessions.reduce((acc, r) => { const k = _stoKinds(r); for (const key of Object.keys(k)) acc[key] = (acc[key] || 0) + k[key]; return acc; },
                      { audio: 0, video: 0, frames: 0, backups: 0, other: 0 });
  const total = _stoSum(kinds);
  const af = report.audio_formats || {};
  const tr = report.tracks || {};
  const notes = {
    audio: [af.wav && af.wav.files ? `${af.wav.files} WAV` : '', af.opus && af.opus.files ? `${af.opus.files} Opus` : '',
            tr.wav && tr.wav.files ? `${tr.wav.files} WAV track${tr.wav.files === 1 ? '' : 's'}` : ''].filter(Boolean).join(' · '),
    backups: (report.backups || []).length ? `${report.backups.length} trim and split cop${report.backups.length === 1 ? 'y' : 'ies'}` : '',
    unused: report.orphans && report.orphans.files ? `${report.orphans.files} file${report.orphans.files === 1 ? '' : 's'} from deleted meetings` : '',
    other: 'database, attachments, caches',
  };
  if (span !== 'all') { delete notes.unused; }
  if (!total) {
    chart.innerHTML = '<p class="home-storage-empty">Nothing on disk for this span.</p>';
    if (detail) detail.innerHTML = '';
    return 'No storage used in this span.';
  }
  chart.innerHTML = `<div class="sto-type">`
    + `<div class="sto-stack" role="group" aria-label="Disk use by type">${_stoSegments(kinds, total, '')}</div>`
    + _stoLegend(kinds, total, notes) + '</div>';
  if (detail) {
    const bk = report.by_kind || {};
    const files = k => k === 'unused' ? ((report.orphans && report.orphans.files) || 0)
      : k === 'other' ? ['attachments', 'database', 'profiles', 'tmp', 'other'].reduce((a, kk) => a + ((bk[kk] && bk[kk].files) || 0), 0)
      : ((bk[k] && bk[k].files) || 0);
    const rows = _STO_KIND_ORDER.filter(k => kinds[k] > 0).sort((a, b) => kinds[b] - kinds[a]).map(k =>
      `<tr><td><span class="sto-dot sto-k-${k}"></span>${_STO_KIND_LABELS[k]}</td>`
      + `<td class="num">${span === 'all' ? files(k) : ''}</td><td class="num">${_fmtBytes(kinds[k])}</td>`
      + `<td class="num">${Math.round(kinds[k] / total * 100)}%</td><td class="sto-td-note">${escapeHtml(notes[k] || '')}</td></tr>`).join('');
    detail.innerHTML = `<table class="sto-table"><thead><tr><th>Kind</th><th class="num">Files</th><th class="num">Size</th><th class="num">Share</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  }
  return 'Disk use by type. ' + _STO_KIND_ORDER.filter(k => kinds[k] > 0)
    .map(k => `${_STO_KIND_LABELS[k]} ${_fmtBytes(kinds[k])}`).join(', ') + '.';
}

function _stoAudioBadge(rec) {
  if (!rec.audio_bytes) return '';
  const fmt = rec.audio_format === 'opus' ? 'Opus' : 'WAV';
  const cls = rec.audio_format === 'opus' ? 'sto-badge is-small' : 'sto-badge';
  return `<span class="${cls}" title="${fmt === 'Opus' ? 'Compressed audio' : 'Uncompressed audio: Free up space can shrink it'}">${fmt}</span>`;
}

/* By meeting: the largest meetings, one stacked row each, linked. */
function _stoRenderMeetings(chart, detail, report, sessions) {
  const top = parseInt(_stoState.top, 10) || 8;
  const rows = sessions.map(r => ({ rec: r, kinds: _stoKinds(r), bytes: _stoSum(_stoKinds(r)) }))
    .filter(x => x.bytes > 0).sort((a, b) => b.bytes - a.bytes).slice(0, top);
  if (!rows.length) {
    chart.innerHTML = '<p class="home-storage-empty">No meeting in this span has media on disk.</p>';
    if (detail) detail.innerHTML = '';
    return 'No meetings with media in this span.';
  }
  const max = rows[0].bytes;
  chart.innerHTML = `<ol class="sto-rows">` + rows.map(({ rec, kinds, bytes }) => {
    const d = _stoDate(rec);
    const when = d ? d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: d.getFullYear() === new Date().getFullYear() ? undefined : 'numeric' }) : '';
    const title = rec.title || 'Meeting';
    const tip = `${title} · ${when} · ${_fmtBytes(bytes)}`;
    return `<li class="sto-row">`
      + `<a class="sto-row-name" href="/session?id=${encodeURIComponent(rec.id)}" data-nav title="${escapeHtml(title)}">${escapeHtml(title)}</a>`
      + `<span class="sto-row-when">${escapeHtml(when)}</span>`
      + `<div class="sto-bar" style="width:${(bytes / max * 100).toFixed(1)}%">${_stoSegments(kinds, bytes, tip)}</div>`
      + `<span class="sto-row-val">${_fmtBytes(bytes)}</span>${_stoAudioBadge(rec)}</li>`;
  }).join('') + '</ol>';
  if (detail) {
    detail.innerHTML = `<table class="sto-table"><thead><tr><th>Meeting</th><th>Recorded</th><th class="num">Audio</th><th class="num">Video</th><th class="num">Frames</th><th class="num">Backups</th><th class="num">Total</th></tr></thead><tbody>`
      + rows.map(({ rec, kinds, bytes }) => {
        const d = _stoDate(rec);
        return `<tr><td class="sto-td-name"><a href="/session?id=${encodeURIComponent(rec.id)}" data-nav>${escapeHtml(rec.title || 'Meeting')}</a></td>`
          + `<td>${d ? escapeHtml(d.toLocaleDateString()) : ''}</td>`
          + `<td class="num">${_fmtBytes(kinds.audio)} ${_stoAudioBadge(rec)}</td><td class="num">${_fmtBytes(kinds.video)}</td>`
          + `<td class="num">${_fmtBytes(kinds.frames)}</td><td class="num">${_fmtBytes(kinds.backups)}</td><td class="num">${_fmtBytes(bytes)}</td></tr>`;
      }).join('') + '</tbody></table>';
  }
  return `The ${rows.length} largest meetings. ` + rows.map(x => `${x.rec.title || 'Meeting'} ${_fmtBytes(x.bytes)}`).join(', ') + '.';
}

/* By month: an inline SVG of stacked bars, one per month recorded, pixel-sized
 * like the Activity chart and repainted by the same observer. */
function _stoRenderMonths(chart, detail, report, sessions) {
  const dated = sessions.map(r => ({ rec: r, d: _stoDate(r), kinds: _stoKinds(r) })).filter(x => x.d && _stoSum(x.kinds) > 0);
  if (!dated.length) {
    chart.innerHTML = '<p class="home-storage-empty">No meeting in this span has media on disk.</p>';
    if (detail) detail.innerHTML = '';
    return 'No meetings with media in this span.';
  }
  let first = new Date(Math.min(...dated.map(x => x.d.getTime())));
  first = new Date(first.getFullYear(), first.getMonth(), 1);
  const now = new Date();
  const months = [];
  const byKey = new Map();
  const cursor = new Date(first);
  while (cursor <= now && months.length < 240) {
    const m = { start: new Date(cursor), key: _stoMonthKey(cursor), count: 0,
                kinds: { audio: 0, video: 0, frames: 0, backups: 0, other: 0 } };
    months.push(m); byKey.set(m.key, m);
    cursor.setMonth(cursor.getMonth() + 1);
  }
  for (const x of dated) {
    const m = byKey.get(_stoMonthKey(x.d));
    if (!m) continue;
    m.count += 1;
    for (const k of Object.keys(x.kinds)) m.kinds[k] += x.kinds[k];
  }
  const totals = months.map(m => _stoSum(m.kinds));
  const top = _stoNiceBytes(Math.max(...totals, 1));

  if (!chart.clientWidth) _dashRetryPaint();
  const W = chart.clientWidth || 560;
  const H = chart.clientHeight || 190;
  const padL = 46, padR = 10, padT = 14, padB = 24;
  const innerW = W - padL - padR, innerH = H - padT - padB;
  const n = months.length, slot = innerW / n;
  const barW = Math.max(3, Math.min(40, slot * 0.64));
  const baseY = padT + innerH;
  const yFor = v => baseY - (v / top) * innerH;
  const labelEvery = slot >= 44 ? 1 : (slot >= 24 ? 2 : (slot >= 14 ? 3 : 6));
  let grid = '';
  for (const gv of [top, top / 2]) {
    const y = yFor(gv);
    grid += `<line class="sto-grid" x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}"></line>`;
    grid += `<text class="sto-ylabel" x="${padL - 6}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${escapeHtml(_fmtBytes(gv))}</text>`;
  }
  grid += `<line class="sto-baseline" x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}"></line>`;
  let bars = '', xlabels = '';
  months.forEach((m, i) => {
    const cx = padL + slot * i + slot / 2;
    const total = totals[i];
    const label = m.start.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    const tip = `${label} · ${m.count} meeting${m.count === 1 ? '' : 's'} · ${_fmtBytes(total)}` +
      (total ? ' · ' + _STO_KIND_ORDER.filter(k => m.kinds[k] > 0).map(k => `${_STO_KIND_LABELS[k]} ${_fmtBytes(m.kinds[k])}`).join(', ') : '');
    let y = baseY;
    if (total > 0) {
      for (const k of _STO_KIND_ORDER) {
        const v = m.kinds[k] || 0;
        if (!v) continue;
        const h = Math.max(1, (v / top) * innerH);
        y -= h;
        bars += `<rect class="sto-bar-seg sto-k-${k}" x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}"></rect>`;
      }
      bars += `<rect class="sto-hit" x="${(cx - slot / 2).toFixed(1)}" y="${padT}" width="${slot.toFixed(1)}" height="${innerH}" tabindex="0" role="img" data-tip="${escapeHtml(tip)}" aria-label="${escapeHtml(tip)}"></rect>`;
    } else {
      bars += `<rect class="sto-bar-seg sto-bar-empty" x="${(cx - barW / 2).toFixed(1)}" y="${baseY - 2}" width="${barW.toFixed(1)}" height="2"></rect>`;
    }
    if (i % labelEvery === 0 || i === n - 1) {
      const anchor = i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle');
      const tx = i === 0 ? padL : (i === n - 1 ? W - padR : cx);
      const thisYear = m.start.getFullYear() === now.getFullYear();
      const txt = m.start.toLocaleDateString(undefined, thisYear ? { month: 'short' } : { month: 'short', year: '2-digit' });
      xlabels += `<text class="sto-xlabel" x="${tx.toFixed(1)}" y="${H - 6}" text-anchor="${anchor}">${escapeHtml(txt)}</text>`;
    }
  });
  chart.innerHTML = `<svg class="sto-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="group" aria-label="Disk use per month recorded">${grid}${bars}${xlabels}</svg>`;
  if (detail) {
    detail.innerHTML = `<table class="sto-table"><thead><tr><th>Month</th><th class="num">Meetings</th><th class="num">Audio</th><th class="num">Video</th><th class="num">Other</th><th class="num">Total</th></tr></thead><tbody>`
      + months.slice().reverse().filter(m => m.count).map((m, i) =>
        `<tr><td>${escapeHtml(m.start.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }))}</td><td class="num">${m.count}</td>`
        + `<td class="num">${_fmtBytes(m.kinds.audio)}</td><td class="num">${_fmtBytes(m.kinds.video)}</td>`
        + `<td class="num">${_fmtBytes(m.kinds.frames + m.kinds.backups + m.kinds.other)}</td><td class="num">${_fmtBytes(_stoSum(m.kinds))}</td></tr>`).join('')
      + '</tbody></table>';
  }
  return 'Disk use per month recorded. ' + months.filter(m => m.count).map(m =>
    `${m.start.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })} ${_fmtBytes(_stoSum(m.kinds))}`).join(', ') + '.';
}

/* By folder: root folders (subfolders fold into their root) and Unfiled. */
function _stoRenderFolders(chart, detail, report, sessions) {
  const byId = new Map((report.folders || []).map(f => [f.id, f]));
  const groups = new Map();
  for (const r of sessions) {
    const kinds = _stoKinds(r);
    const bytes = _stoSum(kinds);
    const root = r.folder_id ? _stoRootOf(r.folder_id, byId) : null;
    const key = root ? root.id : '__unfiled';
    const g = groups.get(key) || { name: root ? root.name : 'Unfiled', count: 0, bytes: 0,
                                    kinds: { audio: 0, video: 0, frames: 0, backups: 0, other: 0 } };
    g.count += 1;
    g.bytes += bytes;
    for (const k of Object.keys(kinds)) g.kinds[k] += kinds[k];
    groups.set(key, g);
  }
  const rows = [...groups.values()].filter(g => g.bytes > 0).sort((a, b) => b.bytes - a.bytes);
  if (!rows.length) {
    chart.innerHTML = '<p class="home-storage-empty">No meeting in this span has media on disk.</p>';
    if (detail) detail.innerHTML = '';
    return 'No meetings with media in this span.';
  }
  const max = rows[0].bytes;
  chart.innerHTML = `<ol class="sto-rows sto-rows-folders">` + rows.map(g => {
    const tip = `${g.name} · ${g.count} meeting${g.count === 1 ? '' : 's'} · ${_fmtBytes(g.bytes)}`;
    return `<li class="sto-row"><span class="sto-row-name" title="${escapeHtml(g.name)}">${escapeHtml(g.name)}</span>`
      + `<span class="sto-row-when">${g.count} mtg${g.count === 1 ? '' : 's'}</span>`
      + `<div class="sto-bar" style="width:${(g.bytes / max * 100).toFixed(1)}%">${_stoSegments(g.kinds, g.bytes, tip)}</div>`
      + `<span class="sto-row-val">${_fmtBytes(g.bytes)}</span></li>`;
  }).join('') + '</ol>';
  if (detail) {
    detail.innerHTML = `<table class="sto-table"><thead><tr><th>Folder</th><th class="num">Meetings</th><th class="num">Audio</th><th class="num">Video</th><th class="num">Other</th><th class="num">Total</th></tr></thead><tbody>`
      + rows.map(g => `<tr><td>${escapeHtml(g.name)}</td><td class="num">${g.count}</td><td class="num">${_fmtBytes(g.kinds.audio)}</td>`
        + `<td class="num">${_fmtBytes(g.kinds.video)}</td><td class="num">${_fmtBytes(g.kinds.frames + g.kinds.backups + g.kinds.other)}</td>`
        + `<td class="num">${_fmtBytes(g.bytes)}</td></tr>`).join('') + '</tbody></table>';
  }
  return 'Disk use by folder. ' + rows.map(g => `${g.name} ${_fmtBytes(g.bytes)}`).join(', ') + '.';
}

/* ── Free up space: the compression tool ──────────────────────────────────────
 * A dialog over Home (the overlay lives in index.html) that prices a run
 * against /api/storage/plan as the user changes scope and formats, starts it
 * with /api/storage/compress, and follows it through the storage_job SSE
 * event. The format choices are remembered; the scope is not, because "all
 * meetings" is the safe thing to start from every time. */

const _TOOL_STORE_KEY = 'home-storage-tool-v1';
const _TOOL_DEFAULT_OPTIONS = {
  audio: { enabled: true, preset: 'voice_32', tracks: true },
  video: { enabled: false, preset: 'av1_balanced', downscale: false, hardware: true },
  backups: { enabled: true },
  orphans: { enabled: false },
  list: { sort: 'date' },        // the Chosen meetings list: 'date' (newest first) or 'size'
};
const _TOOL_LIST_SORTS = [['date', 'Newest'], ['size', 'Largest']];
const _TOOL_AUDIO_PRESETS = [
  ['voice_24', 'Smallest, 24 kbps'], ['voice_32', 'Recommended, 32 kbps'],
  ['voice_48', 'Higher, 48 kbps'], ['voice_64', 'Highest, 64 kbps'],
];
const _TOOL_VIDEO_PRESETS = [
  ['av1_small', 'AV1, smallest', 'av1'], ['av1_balanced', 'AV1, balanced', 'av1'],
  ['hevc', 'HEVC (H.265)', 'hevc'], ['h264', 'H.264, most compatible', 'h264'],
];
const _TOOL_SCOPES = [['all', 'All meetings'], ['older', 'Older than'], ['range', 'Date range'],
                      ['sessions', 'Chosen meetings'], ['folders', 'Folders']];
const _TOOL_KIND_WORDS = { audio: 'Audio', video: 'Video', backup: 'Backups', orphan: 'Unused files' };

function _toolLoadOptions() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(_TOOL_STORE_KEY) || 'null'); } catch (_) { saved = null; }
  const out = {};
  for (const group of Object.keys(_TOOL_DEFAULT_OPTIONS)) {
    out[group] = { ..._TOOL_DEFAULT_OPTIONS[group], ...((saved && saved[group]) || {}) };
  }
  if (!_TOOL_AUDIO_PRESETS.some(p => p[0] === out.audio.preset)) out.audio.preset = 'voice_32';
  if (!_TOOL_VIDEO_PRESETS.some(p => p[0] === out.video.preset)) out.video.preset = 'av1_balanced';
  if (!_TOOL_LIST_SORTS.some(o => o[0] === out.list.sort)) out.list.sort = 'date';
  // Deletion is never remembered as on: it is a decision for each run.
  out.orphans.enabled = false;
  return out;
}

const _tool = {
  open: false,
  scope: { mode: 'all', days: 90, start: '', end: '', session_ids: new Set(), folder_ids: new Set() },
  options: _toolLoadOptions(),
  plan: null, planning: false, planSeq: 0, planTimer: null,
  job: null, lastFull: 0, filter: '',
};

function _toolSaveOptions() {
  try { localStorage.setItem(_TOOL_STORE_KEY, JSON.stringify(_tool.options)); } catch (_) {}
}

function _toolBody() { return document.getElementById('storage-tool-body'); }

function _toolScopePayload() {
  const sc = _tool.scope;
  const out = { mode: sc.mode };
  if (sc.mode === 'older') out.days = Number(sc.days) || 90;
  if (sc.mode === 'range') { out.start = sc.start; out.end = sc.end; }
  if (sc.mode === 'sessions') out.session_ids = [...sc.session_ids];
  if (sc.mode === 'folders') out.folder_ids = _toolExpandFolders([...sc.folder_ids]);
  return out;
}

/** Chosen folders plus every folder under them. */
function _toolExpandFolders(ids) {
  const folders = AppData.get('folders') || [];
  const children = new Map();
  for (const f of folders) {
    if (!children.has(f.parent_id)) children.set(f.parent_id, []);
    children.get(f.parent_id).push(f.id);
  }
  const out = new Set();
  const stack = [...ids];
  while (stack.length) {
    const id = stack.pop();
    if (out.has(id)) continue;
    out.add(id);
    for (const c of children.get(id) || []) stack.push(c);
  }
  return [...out];
}

function _toolRequestBody() {
  const o = _tool.options;
  // The list's sort order is a dialog preference, not part of the run.
  return { scope: _toolScopePayload(), audio: o.audio, video: o.video,
           backups: o.backups, orphans: o.orphans };
}

function openStorageTool() {
  const overlay = document.getElementById('storage-tool-overlay');
  if (!overlay) return;
  _tool.open = true;
  overlay.classList.remove('hidden');
  document.addEventListener('keydown', _toolOnKey);
  _toolFetchJob().then(() => {
    if (_tool.job && _tool.job.state === 'running') _toolRenderRun();
    else { _toolRenderSetup(); _toolRequestPlan(); }
  });
}

function closeStorageTool() {
  const overlay = document.getElementById('storage-tool-overlay');
  if (overlay) overlay.classList.add('hidden');
  _tool.open = false;
  document.removeEventListener('keydown', _toolOnKey);
}

function _toolOnKey(e) {
  if (e.key === 'Escape') { e.preventDefault(); closeStorageTool(); }
}

async function _toolFetchJob() {
  try {
    const res = await fetch('/api/storage/compress', { cache: 'no-store' });
    if (!res.ok) return;
    const data = await res.json();
    _tool.job = data.job;
    _tool.caps = data.capabilities || _tool.caps;
    _tool.lastFull = Date.now();
  } catch (_) {}
}

/* ── the setup form ── */

function _toolRenderSetup() {
  const body = _toolBody();
  if (!body) return;
  const sc = _tool.scope;
  const o = _tool.options;
  const caps = _tool.caps || {};
  const hw = caps.hardware || {};
  const anyHw = Object.values(hw).some(Boolean);
  const videoOk = caps.video || {};
  const scopeBtns = _TOOL_SCOPES.map(([val, text]) =>
    `<button type="button" class="dash-seg-btn${sc.mode === val ? ' is-on' : ''}" role="radio" aria-checked="${sc.mode === val}" onclick="_toolScopeMode('${val}')">${text}</button>`).join('');
  body.innerHTML = `
    <div class="tool-section">
      <div class="tool-label">Which meetings</div>
      <div class="dash-seg tool-scope" role="radiogroup" aria-label="Which meetings">${scopeBtns}</div>
      <div class="tool-scope-sub" id="storage-tool-scope-sub">${_toolScopeSubHtml()}</div>
    </div>
    <div class="tool-section">
      <div class="tool-label">What to do</div>
      <div class="tool-what">
        <label class="tool-row">
          <input type="checkbox" ${o.audio.enabled ? 'checked' : ''} onchange="_toolOption('audio', 'enabled', this.checked)">
          <span class="tool-row-main"><span class="tool-row-title">Re-encode audio to Opus</span>
            <span class="tool-row-desc">The recorder keeps audio as WAV, about 345 MB an hour. Opus for speech is about 14 MB an hour and sounds the same. Transcripts, chapters and speakers are untouched.</span></span>
          <select class="tool-select" ${o.audio.enabled ? '' : 'disabled'} onchange="_toolOption('audio', 'preset', this.value)" aria-label="Audio quality">
            ${_TOOL_AUDIO_PRESETS.map(([v, t]) => `<option value="${v}"${o.audio.preset === v ? ' selected' : ''}>${t}</option>`).join('')}
          </select>
        </label>
        <label class="tool-row tool-row-sub">
          <input type="checkbox" ${o.audio.tracks ? 'checked' : ''} ${o.audio.enabled ? '' : 'disabled'} onchange="_toolOption('audio', 'tracks', this.checked)">
          <span class="tool-row-main"><span class="tool-row-title">Include the separate mic and desktop tracks</span></span>
        </label>
        <label class="tool-row">
          <input type="checkbox" ${o.video.enabled ? 'checked' : ''} ${caps.ffmpeg === false ? 'disabled' : ''} onchange="_toolOption('video', 'enabled', this.checked)">
          <span class="tool-row-main"><span class="tool-row-title">Re-encode screen recordings</span>
            <span class="tool-row-desc">The video is H.264 already, so the gain is smaller: AV1 roughly halves it, HEVC saves about a third. The file keeps its name; only the codec inside changes.</span></span>
          <select class="tool-select" ${o.video.enabled ? '' : 'disabled'} onchange="_toolOption('video', 'preset', this.value)" aria-label="Video format">
            ${_TOOL_VIDEO_PRESETS.map(([v, t, codec]) => `<option value="${v}"${o.video.preset === v ? ' selected' : ''}${videoOk[codec] === false ? ' disabled' : ''}>${t}</option>`).join('')}
          </select>
        </label>
        <div class="tool-row tool-row-sub tool-row-inline">
          <label class="tool-inline"><input type="checkbox" ${o.video.hardware ? 'checked' : ''} ${o.video.enabled && anyHw ? '' : 'disabled'} onchange="_toolOption('video', 'hardware', this.checked)"> Use the graphics card${anyHw ? '' : ' (not available here)'}</label>
          <label class="tool-inline"><input type="checkbox" ${o.video.downscale ? 'checked' : ''} ${o.video.enabled ? '' : 'disabled'} onchange="_toolOption('video', 'downscale', this.checked)"> Reduce 4K to 1440p</label>
        </div>
        <label class="tool-row">
          <input type="checkbox" ${o.backups.enabled ? 'checked' : ''} onchange="_toolOption('backups', 'enabled', this.checked)">
          <span class="tool-row-main"><span class="tool-row-title">Re-encode trim and split backup copies</span>
            <span class="tool-row-desc">The original audio kept for undo, in the same Opus quality. Undo still works.</span></span>
        </label>
        <label class="tool-row">
          <input type="checkbox" ${o.orphans.enabled ? 'checked' : ''} onchange="_toolOption('orphans', 'enabled', this.checked)">
          <span class="tool-row-main"><span class="tool-row-title">Remove files that belong to no meeting</span>
            <span class="tool-row-desc">Media left behind by deleted meetings and encoder fragments, when they are more than six hours old. This deletes files; it is off unless you turn it on, every time.</span></span>
        </label>
      </div>
    </div>
    <div class="tool-section tool-estimate" id="storage-tool-estimate" aria-live="polite">${_toolEstimateHtml()}</div>
    <div class="tool-actions">
      <button type="button" class="btn btn-secondary" onclick="closeStorageTool()">Cancel</button>
      <button type="button" class="btn btn-primary tool-run-btn" id="storage-tool-run" onclick="runStorageTool()" ${_toolRunDisabled() ? 'disabled' : ''}>${_toolRunLabel()}</button>
    </div>`;
}

function _toolScopeSubHtml() {
  const sc = _tool.scope;
  if (sc.mode === 'older') {
    return `<label class="tool-inline">Meetings recorded more than
      <select class="tool-select" onchange="_toolScopeField('days', this.value)" aria-label="Older than">
        ${[30, 60, 90, 180, 365].map(d => `<option value="${d}"${Number(sc.days) === d ? ' selected' : ''}>${d} days</option>`).join('')}
      </select> ago</label>`;
  }
  if (sc.mode === 'range') {
    return `<div class="tool-inline-row">
      <label class="tool-inline">From <input type="date" class="tool-date" value="${escapeHtml(sc.start)}" onchange="_toolScopeField('start', this.value)"></label>
      <label class="tool-inline">To <input type="date" class="tool-date" value="${escapeHtml(sc.end)}" onchange="_toolScopeField('end', this.value)"></label>
      <span class="tool-hint">Leave one empty for no bound.</span></div>`;
  }
  if (sc.mode === 'sessions') {
    const sort = _tool.options.list.sort;
    const sortBtns = _TOOL_LIST_SORTS.map(([val, text]) =>
      `<button type="button" class="dash-seg-btn${sort === val ? ' is-on' : ''}" role="radio" aria-checked="${sort === val}" onclick="_toolListSort('${val}')">${text}</button>`).join('');
    return `<div class="tool-list-head">
        <input type="search" class="tool-search" placeholder="Filter meetings" value="${escapeHtml(_tool.filter)}" oninput="_toolFilter(this.value)" aria-label="Filter meetings">
        <div class="dash-seg tool-sort" role="radiogroup" aria-label="Sort meetings by">${sortBtns}</div>
        <button type="button" class="tool-link" onclick="_toolSelectShown(true)">Select shown</button>
        <button type="button" class="tool-link" onclick="_toolSelectShown(false)">Clear</button>
        <span class="tool-hint" id="storage-tool-picked">${sc.session_ids.size} chosen</span>
      </div>
      <ol class="tool-list" id="storage-tool-list">${_toolSessionRows()}</ol>`;
  }
  if (sc.mode === 'folders') {
    const folders = (AppData.get('folders') || []).slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
    if (!folders.length) return '<p class="tool-hint">No folders yet.</p>';
    const depth = new Map();
    const byId = new Map(folders.map(f => [f.id, f]));
    const depthOf = f => { let d = 0, cur = f; while (cur && cur.parent_id && byId.has(cur.parent_id) && d < 20) { cur = byId.get(cur.parent_id); d++; } return d; };
    folders.forEach(f => depth.set(f.id, depthOf(f)));
    return `<ol class="tool-list">${folders.map(f =>
      `<li class="tool-item" style="padding-left:${8 + depth.get(f.id) * 16}px"><label><input type="checkbox" ${sc.folder_ids.has(f.id) ? 'checked' : ''} onchange="_toolToggleFolder('${escapeHtml(f.id)}', this.checked)"> ${escapeHtml(f.name)}</label></li>`).join('')}</ol>
      <p class="tool-hint">A folder includes the folders inside it.</p>`;
  }
  return '<p class="tool-hint">Every meeting in the library.</p>';
}

function _toolSessionRows() {
  const report = AppData.get('storage');
  const sizes = new Map(((report && report.sessions) || []).map(r => [r.id, _stoSum(_stoKinds(r))]));
  const q = _tool.filter.trim().toLowerCase();
  const rows = (_dashSessions || []).filter(s => s.started_at && (!q || String(s.title || '').toLowerCase().includes(q)));
  if (!rows.length) return '<li class="tool-hint">No meetings match.</li>';
  // The sessions slice is newest first already; "Largest" puts the meetings
  // with the most on disk at the top, ties and unknown sizes by date.
  if (_tool.options.list.sort === 'size') {
    rows.sort((a, b) => (sizes.get(b.id) || 0) - (sizes.get(a.id) || 0));
  }
  return rows.map(s => {
    const d = new Date(s.started_at + 'Z');
    const when = Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString();
    return `<li class="tool-item"><label><input type="checkbox" ${_tool.scope.session_ids.has(s.id) ? 'checked' : ''} onchange="_toolToggleSession('${escapeHtml(s.id)}', this.checked)">`
      + `<span class="tool-item-title">${escapeHtml(s.title || 'Meeting')}</span><span class="tool-item-when">${escapeHtml(when)}</span>`
      + `<span class="tool-item-size">${sizes.has(s.id) ? _fmtBytes(sizes.get(s.id)) : ''}</span></label></li>`;
  }).join('');
}

function _toolScopeMode(mode) {
  if (_tool.scope.mode === mode) return;
  _tool.scope.mode = mode;
  _toolRenderSetup();
  _toolRequestPlan();
}
function _toolScopeField(field, value) {
  _tool.scope[field] = value;
  _toolRequestPlan();
}
function _toolToggleSession(id, on) {
  if (on) _tool.scope.session_ids.add(id); else _tool.scope.session_ids.delete(id);
  const picked = document.getElementById('storage-tool-picked');
  if (picked) picked.textContent = `${_tool.scope.session_ids.size} chosen`;
  _toolRequestPlan();
}
function _toolSelectShown(on) {
  const q = _tool.filter.trim().toLowerCase();
  for (const s of _dashSessions || []) {
    if (!s.started_at || (q && !String(s.title || '').toLowerCase().includes(q))) continue;
    if (on) _tool.scope.session_ids.add(s.id); else _tool.scope.session_ids.delete(s.id);
  }
  const list = document.getElementById('storage-tool-list');
  if (list) list.innerHTML = _toolSessionRows();
  const picked = document.getElementById('storage-tool-picked');
  if (picked) picked.textContent = `${_tool.scope.session_ids.size} chosen`;
  _toolRequestPlan();
}
function _toolToggleFolder(id, on) {
  if (on) _tool.scope.folder_ids.add(id); else _tool.scope.folder_ids.delete(id);
  _toolRequestPlan();
}
function _toolFilter(text) {
  _tool.filter = text || '';
  const list = document.getElementById('storage-tool-list');
  if (list) list.innerHTML = _toolSessionRows();
}
function _toolListSort(sort) {
  if (!_TOOL_LIST_SORTS.some(o => o[0] === sort) || _tool.options.list.sort === sort) return;
  _tool.options.list.sort = sort;
  _toolSaveOptions();
  const sub = document.getElementById('storage-tool-scope-sub');
  if (sub) sub.innerHTML = _toolScopeSubHtml();
}
function _toolOption(group, key, value) {
  _tool.options[group][key] = value;
  _toolSaveOptions();
  _toolRenderSetup();     // dependent controls enable and disable with their parent
  _toolRequestPlan();
}

/* ── pricing ── */

function _toolRequestPlan() {
  clearTimeout(_tool.planTimer);
  _tool.planTimer = setTimeout(_toolFetchPlan, 250);
}

async function _toolFetchPlan() {
  const seq = ++_tool.planSeq;
  _tool.planning = true;
  _toolRenderEstimate();
  try {
    const res = await fetch('/api/storage/plan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_toolRequestBody()),
    });
    const data = await res.json();
    if (seq !== _tool.planSeq) return;
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    _tool.plan = data;
    _tool.caps = data.capabilities || _tool.caps;
  } catch (e) {
    if (seq !== _tool.planSeq) return;
    _tool.plan = { error: (e && e.message) || 'could not price the run' };
  } finally {
    if (seq === _tool.planSeq) { _tool.planning = false; _toolRenderEstimate(); }
  }
}

function _toolRunDisabled() {
  const p = _tool.plan;
  return _tool.planning || !p || p.error || !(p.totals && p.totals.files) || p.running
    || (_tool.caps && _tool.caps.ffmpeg === false);
}

function _toolRunLabel() {
  const p = _tool.plan;
  if (_tool.caps && _tool.caps.ffmpeg === false) return 'ffmpeg is not available';
  if (!p || p.error) return 'Free up space';
  if (p.running) return 'A run is in progress';
  if (!(p.totals && p.totals.files)) return 'Nothing to do';
  return `Free up about ${_fmtBytes(p.totals.saved)}`;
}

function _toolEstimateHtml() {
  const p = _tool.plan;
  if (_tool.planning && !p) return '<p class="tool-hint">Working out what this would save…</p>';
  if (!p) return '';
  if (p.error) return `<p class="tool-warn">Could not price the run: ${escapeHtml(p.error)}</p>`;
  const t = p.totals || {};
  if (!t.files) {
    const why = p.skipped && p.skipped.already ? 'Everything in this scope is compressed already.' : 'Nothing in this scope matches what is switched on.';
    return `<p class="tool-hint">${why}</p>`;
  }
  const kinds = Object.entries(p.by_kind || {}).map(([k, v]) => {
    const what = _TOOL_KIND_WORDS[k] || k;
    const unit = k === 'audio' || k === 'video' ? 'meeting' : (k === 'backup' ? 'folder' : 'file');
    const after = k === 'orphan' ? 'removed' : `about ${_fmtBytes(v.after)} after`;
    return `<li><span class="tool-est-kind">${what}</span><span>${v.files} ${unit}${v.files === 1 ? '' : 's'} · ${_fmtBytes(v.before)} now · ${after}</span></li>`;
  }).join('');
  const notes = [];
  if (p.skipped && p.skipped.busy) notes.push(`${p.skipped.busy} in use right now and skipped`);
  if (p.skipped && p.skipped.young) notes.push(`${p.skipped.young} too recent to remove safely`);
  if (p.skipped && p.skipped.already) notes.push(`${p.skipped.already} already compressed`);
  const o = _tool.options;
  const hw = (_tool.caps && _tool.caps.hardware) || {};
  const codec = (_TOOL_VIDEO_PRESETS.find(v => v[0] === o.video.preset) || [])[2];
  if (o.video.enabled && p.by_kind && p.by_kind.video) {
    notes.push(o.video.hardware && hw[codec] ? 'Video uses the graphics card, so it is quick'
      : 'Video is encoded in software, which is slow: expect roughly real time for 4K screen video');
  }
  return `<div class="tool-est-main"><strong>Frees about ${_fmtBytes(t.saved)}</strong>`
    + `<span>${t.files} file${t.files === 1 ? '' : 's'} · ${_fmtBytes(t.before)} now · about ${_fmtBytes(t.after)} after</span></div>`
    + `<ul class="tool-est-kinds">${kinds}</ul>`
    + (notes.length ? `<p class="tool-hint">${escapeHtml(notes.join(' · '))}</p>` : '')
    + (_tool.planning ? '<p class="tool-hint">Updating…</p>' : '');
}

function _toolRenderEstimate() {
  const est = document.getElementById('storage-tool-estimate');
  if (est) est.innerHTML = _toolEstimateHtml();
  const run = document.getElementById('storage-tool-run');
  if (run) { run.disabled = _toolRunDisabled(); run.textContent = _toolRunLabel(); }
}

/* ── running ── */

async function runStorageTool() {
  const run = document.getElementById('storage-tool-run');
  if (run) run.disabled = true;
  try {
    const res = await fetch('/api/storage/compress', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_toolRequestBody()),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    _tool.job = data.job;
    _tool.lastFull = Date.now();
    _tool.scope.session_ids.clear();
    _toolRenderRun();
  } catch (e) {
    uiToast({ message: `Could not start: ${(e && e.message) || 'unknown error'}`, kind: 'error' });
    if (run) run.disabled = _toolRunDisabled();
  }
}

async function cancelStorageTool() {
  try {
    await fetch('/api/storage/compress/cancel', { method: 'POST' });
  } catch (_) {}
  await _toolFetchJob();
  if (_tool.open) _toolRenderRun();
}

function _toolItemRow(i) {
  const icon = { done: 'fa-check', running: 'fa-spinner fa-spin', failed: 'fa-triangle-exclamation',
                 skipped: 'fa-minus', cancelled: 'fa-ban', pending: 'fa-clock' }[i.status] || 'fa-clock';
  const what = _TOOL_KIND_WORDS[i.kind] || i.kind;
  let right = '';
  if (i.status === 'done') right = i.kind === 'orphan' ? `${_fmtBytes(i.before)} removed` : `${_fmtBytes(i.before)} → ${_fmtBytes(i.after)}`;
  else if (i.status === 'running') right = `${Math.round((i.progress || 0) * 100)}%`;
  else if (i.error) right = i.error;
  else right = _fmtBytes(i.before);
  return `<li class="tool-job-item is-${i.status}"><i class="fa-solid ${icon}" aria-hidden="true"></i>`
    + `<span class="tool-job-kind">${what}</span><span class="tool-job-title" title="${escapeHtml(i.title || '')}">${escapeHtml(i.title || '')}</span>`
    + `<span class="tool-job-right">${escapeHtml(right)}</span></li>`;
}

function _toolRenderRun() {
  const body = _toolBody();
  const job = _tool.job;
  if (!body || !job) return;
  const running = job.state === 'running';
  const total = job.total || 0;
  const done = job.done || 0;
  const cur = job.current;
  const frac = total ? (done + (cur ? (cur.progress || 0) : 0)) / total : 0;
  const head = running ? 'Freeing space…'
    : job.state === 'done' ? `Freed ${_fmtBytes(job.saved)}`
    : job.state === 'cancelled' ? `Stopped after freeing ${_fmtBytes(job.saved)}`
    : `Stopped by a problem${job.error ? `: ${job.error}` : ''}`;
  const sub = running
    ? `${done} of ${total} file${total === 1 ? '' : 's'} · ${_fmtBytes(job.saved)} saved so far`
    : `${done} of ${total} done${job.failed ? ` · ${job.failed} could not be changed` : ''}`;
  const current = cur && running
    ? `<div class="tool-current">${_TOOL_KIND_WORDS[cur.kind] || cur.kind} · ${escapeHtml(cur.title || '')} · ${Math.round((cur.progress || 0) * 100)}%</div>` : '';
  const items = (job.items || []).map(_toolItemRow).join('');
  body.innerHTML = `
    <div class="tool-run">
      <div class="tool-run-head"><strong>${escapeHtml(head)}</strong><span>${escapeHtml(sub)}</span></div>
      <div class="tool-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(frac * 100)}">
        <div class="tool-progress-fill${running ? '' : ' is-final'}" style="width:${(frac * 100).toFixed(1)}%"></div>
      </div>
      ${current}
      <ol class="tool-job-items">${items || '<li class="tool-hint">Nothing was queued.</li>'}</ol>
      <div class="tool-actions">
        ${running ? '<button type="button" class="btn btn-secondary" onclick="cancelStorageTool()">Stop after this file</button>' : ''}
        <button type="button" class="btn ${running ? 'btn-secondary' : 'btn-primary'}" onclick="${running ? 'closeStorageTool()' : '_toolBackToSetup()'}">${running ? 'Close, keep going' : 'Done'}</button>
      </div>
    </div>`;
  const list = body.querySelector('.tool-job-items');
  const active = list && list.querySelector('.is-running');
  if (active && typeof active.scrollIntoView === 'function') active.scrollIntoView({ block: 'nearest' });
}

function _toolBackToSetup() {
  _tool.job = null;
  closeStorageTool();
}

/** storage_job SSE: a compact snapshot while running, then the final one.
 *  Every couple of seconds (and at the end) the full item list is re-read. */
function _toolOnJobEvent(snap) {
  if (!snap || !snap.id) return;
  const prev = _tool.job && _tool.job.id === snap.id ? _tool.job : null;
  _tool.job = { ...(prev || {}), ...snap, items: (prev && prev.items) || (snap.items || []) };
  const finished = snap.state !== 'running';
  if (finished || Date.now() - _tool.lastFull > 2000) {
    _toolFetchJob().then(() => { if (_tool.open) _toolRenderRun(); });
  } else if (_tool.open) {
    _toolRenderRun();
  }
  if (finished) {
    AppData.invalidate(['storage'], 'storage_job');
    if (!_tool.open && snap.state === 'done' && typeof uiToast === 'function') {
      uiToast({ message: `Free up space finished: ${_fmtBytes(snap.saved)} freed.`, kind: 'success' });
    }
  }
}

/* ── The Home view's lifecycle ────────────────────────────────────────────── */

Views.register('home', {
  activate() {
    // Renders from the store; only an idle slice reaches the network.
    AppData.load('analytics');
    AppData.load('calendarStatus');
    AppData.load('storage');
    AppData.load('calendarEvents', { key: _homeWeekRange().rangeKey });
    _homeBindTips();
    _homeBindKnobs();
    _homeStartNextClock();
    loadAnalytics();
  },
  deactivate() {
    _homeHideTip();
    _homeStopNextClock();
  },
});

AppData.subscribe(['analytics', 'sessions', 'attention', 'calendarStatus', 'calendarEvents', 'storage'], () => {
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
