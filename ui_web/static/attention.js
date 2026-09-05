/* ── Needs attention ──────────────────────────────────────────────────────────
 * The speaker work queue at /attention (brief section 3.7): every recording
 * that still needs a person, newest first, with one action per row.
 *
 * It renders from the sessions slice, so the row count and the sidebar badge
 * can never disagree, and switching to this view does not touch the network.
 * ─────────────────────────────────────────────────────────────────────────── */

const ATTN_AVATARS = 5;   // speaker chips per row before the overflow count

/** Local Date for a session start. Session timestamps are naive UTC. */
function _attnStart(session) {
  return session.started_at ? new Date(session.started_at + 'Z') : null;
}

function _attnDurationSec(session) {
  if (session.last_segment_time != null && session.last_segment_time > 0) {
    return session.last_segment_time;
  }
  if (session.started_at && session.ended_at) {
    return Math.max(0, (new Date(session.ended_at + 'Z') - _attnStart(session)) / 1000);
  }
  return 0;
}

function _attnDuration(seconds) {
  if (!seconds || seconds <= 0) return '';
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${Math.max(m, 1)}m`;
}

/** Why this recording is in the queue, in the same words the rest of the app
 *  uses. Colour is never the only signal: the reason is always spelled out. */
function _attnReason(attention) {
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

function _attnInitials(name) {
  return String(name || '?').trim().split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase();
}

function _attnAvatars(speakers) {
  const people = (speakers || []).filter(sp => sp && sp.name);
  if (!people.length) return '';
  const shown = people.slice(0, ATTN_AVATARS).map(sp => `
    <span class="attn-avatar" style="background:${escapeHtml(sp.color || 'var(--surface3)')}"
          title="${escapeHtml(sp.name)}">${escapeHtml(_attnInitials(sp.name))}</span>`).join('');
  const rest = people.length - ATTN_AVATARS;
  const more = rest > 0 ? `<span class="attn-avatar attn-avatar-more">+${rest}</span>` : '';
  return `<div class="attn-avatars" aria-label="${escapeHtml(people.map(sp => sp.name).join(', '))}">${shown}${more}</div>`;
}

function renderAttentionView() {
  const list = document.getElementById('attn-list');
  const empty = document.getElementById('attn-empty');
  if (!list || !empty) return;

  const sessions = AppData.get('sessions') || [];
  const rows = sessions
    .filter(s => s.attention && s.attention.needs)
    .sort((a, b) => String(b.started_at || '').localeCompare(String(a.started_at || '')));

  const count = rows.length;
  Views.setTitle('attention', 'Needs attention', count === 0
    ? 'Nothing waiting on you'
    : `${count} recording${count === 1 ? '' : 's'} need speaker work`);

  empty.classList.toggle('hidden', count > 0);
  list.classList.toggle('hidden', count === 0);
  if (!count) { list.innerHTML = ''; return; }

  const html = rows.map(s => {
    const start = _attnStart(s);
    const when = start
      ? start.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
      : '';
    const meta = [when, _attnDuration(_attnDurationSec(s))].filter(Boolean).join(' · ');
    const href = `/session?id=${encodeURIComponent(s.id)}&speakers=cleanup`;
    return `
      <li class="attn-row">
        <div class="attn-row-text">
          <a class="attn-row-title" href="/session?id=${encodeURIComponent(s.id)}">${escapeHtml(s.title || s.id)}</a>
          <span class="attn-row-meta">${escapeHtml(meta)}</span>
          <span class="attn-row-reason">${escapeHtml(_attnReason(s.attention))}</span>
        </div>
        ${_attnAvatars(s.speakers)}
        <a class="btn btn-primary attn-action" href="${href}">Clean up</a>
      </li>`;
  }).join('');

  // Keyed update: the queue is a live list the user works down, so a wholesale
  // innerHTML swap would drop focus every time a rename lands.
  if (window.morphdom) {
    const next = list.cloneNode(false);
    next.innerHTML = html;
    morphdom(list, next, { childrenOnly: true });
  } else {
    list.innerHTML = html;
  }
}

Views.register('attention', {
  activate() {
    AppData.load('sessions');
    AppData.load('attention');
    renderAttentionView();
  },
});

AppData.subscribe(['sessions', 'attention'], () => {
  if (Views.current === 'attention') renderAttentionView();
});
