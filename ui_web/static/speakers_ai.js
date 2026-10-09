/* AI speaker detection on the meeting page (ai/speaker_detect on the server).
 *
 * The Identify tab of the Speakers dialog, top to bottom:
 *  - the composer: the user's own words for a run, and the run's settings as
 *    menus that save straight to Settings > Speakers;
 *  - while a run works, what it is doing: its steps, and every frame it sends
 *    to the model, which then shows who was read as speaking and where;
 *  - after a run, what it did, with Apply all and Undo all;
 *  - who's who: every speaker in the meeting, grouped by person the way
 *    Cleanup groups them, named or not, with what the screen showed for each,
 *    the run's suggestions in place, and quick fixes for the unnamed;
 *  - the meeting's speaker history, with undo for every change.
 * Plus the frame lightbox, Settings > Speakers, and the amber dot on the
 * Speakers button when suggestions are waiting.
 *
 * Talks to /api/sessions/<id>/speakers/{identify,insights,evidence/<obs>.jpg},
 * /api/speaker-runs/<id>/{frames,cancel,undo} and /api/speaker-changes. Live
 * state arrives over SSE (speaker_run_start, speaker_run_progress,
 * speaker_run_frames, speaker_run_done, speakers_updated), wired in app.js.
 */
(function () {
  'use strict';

  const S = {
    sid: null,          // the meeting the tab shows
    insights: null,     // last /insights payload
    running: null,      // {run_id, stage, label, done, sent, cached, started} while a run works here
    frames: new Map(),  // preview id -> {id, t, kind, state, w, h, speaking, visible, obs}
    framesRun: null,    // the run those frames belong to
    framesShown: false, // a finished run's frames opened from its summary
    pane: 'cleanup',    // 'ai' | 'cleanup'
    open: {},           // change id -> its evidence shown
    people: {},         // roster group id -> its details shown
    historyOpen: false,
    naming: null,       // speaker key whose name field is open
    library: null,      // Voice Library profiles, read once per page for the name field
    loading: false,
  };
  let roster = null;    // the last buildRoster(), for the click handlers

  const esc = s => (typeof escapeHtml === 'function' ? escapeHtml(s)
    : String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])));
  const $ = id => document.getElementById(id);
  // app.js keeps these as top-level let/const, which other scripts reach by
  // name but not as window properties.
  const prefs = () => (typeof _prefs !== 'undefined' && _prefs) || {};
  const curSid = () => (typeof state !== 'undefined' && state ? state.sessionId : null);
  const recording = () => !!(typeof state !== 'undefined' && state && state.isRecording);
  const enabled = () => !!prefs().speaker_ai_enabled;
  const toast = (message, kind = 'info', id) => {
    if (typeof uiToast === 'function') uiToast({ message, kind, id });
  };
  const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;
  const norm = s => String(s || '').trim().toLowerCase();

  function ago(iso) {
    if (!iso) return '';
    const t = Date.parse(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
    if (!isFinite(t)) return '';
    const s = Math.max(0, (Date.now() - t) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function clock(sec) {
    const t = Math.max(0, Math.floor(Number(sec) || 0));
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  }

  const pct = c => (c == null ? '' : `${Math.round(c * 100)}%`);
  const safeColor = c => (/^#[0-9a-f]{3,8}$/i.test(String(c || '')) ? c : null);

  function generic(name, key) {
    const n = String(name || '').trim();
    if (!n || n === key) return true;
    if (typeof _GENERIC_SPEAKER_RE !== 'undefined') return _GENERIC_SPEAKER_RE.test(n);
    return /^speaker\s*\d+$/i.test(n);
  }

  // The transcript's colour for a speaker, so a person looks the same here,
  // in Cleanup and in the transcript.
  function keyColor(key, stored) {
    try {
      if (typeof speakerColor === 'function') {
        const c = safeColor(speakerColor(key));
        if (c) return c;
      }
    } catch (_) { /* fall back to the stored colour */ }
    return safeColor(stored) || '#6e7681';
  }

  function nameColor(name) {
    const g = roster && roster.groups.find(x => x.named && norm(x.name) === norm(name));
    return g ? g.color : null;
  }

  const TYPE_ICON = {
    name: 'fa-user-pen', move: 'fa-arrow-right-arrow-left', split: 'fa-code-branch',
  };
  const ACTOR = { ai: 'AI', user: 'You', agent: 'Agent', chat: 'Chat', voice_auto: 'Voice library' };
  const NAMED_BY = {
    user: 'named by you', ai: 'named by AI', voice_auto: 'named by the voice library',
    agent: 'named by an agent', chat: 'named in chat',
  };

  // ── The run's settings, as the composer's menus show them ──────────────────

  const OPTIONS = [
    {
      id: 'autonomy', pref: 'speaker_ai_autonomy', def: 'apply_confident', title: 'On its own',
      choices: [
        { v: 'suggest', icon: 'fa-hand', short: 'Suggest only', desc: 'Nothing changes until you apply it.' },
        { v: 'apply_confident', icon: 'fa-wand-magic-sparkles', short: 'Apply when sure', desc: 'Applies what it is sure of and suggests the rest.' },
        { v: 'act_fully', icon: 'fa-bolt', short: 'Act fully', desc: 'Applies everything it is fairly sure of. All of it can be undone.' },
      ],
    },
    {
      id: 'library', pref: 'speaker_ai_library_writes', def: 'follow_autonomy', title: 'Voice profiles',
      choices: [
        { v: 'follow_autonomy', icon: 'fa-waveform-lines', short: 'Teach voices', desc: 'Names it is sure of teach the Voice Library, so later meetings know them by voice.' },
        { v: 'on_accept', icon: 'fa-hand-pointer', short: 'Teach on accept', desc: 'Only the suggestions you apply teach the Voice Library.' },
        { v: 'never', icon: 'fa-ban', short: 'Voices untouched', desc: 'Leaves the Voice Library as it is.' },
      ],
    },
    {
      id: 'depth', pref: 'speaker_ai_depth', def: 'standard', title: 'How closely it looks',
      choices: [
        { v: 'quick', icon: 'fa-gauge-simple', short: 'Quick', desc: 'The fewest frames: fastest and cheapest.' },
        { v: 'standard', icon: 'fa-gauge', short: 'Standard', desc: 'A few looks at every speaker.' },
        { v: 'thorough', icon: 'fa-gauge-high', short: 'Thorough', desc: 'Many more looks. Slower, and costs more.' },
      ],
    },
  ];

  function optionValue(opt) {
    const v = prefs()[opt.pref];
    return opt.choices.some(c => c.v === v) ? v : opt.def;
  }

  function optionsHtml() {
    const busy = !!S.running;
    const pills = OPTIONS.map(opt => {
      const c = opt.choices.find(x => x.v === optionValue(opt));
      return `<button type="button" class="spai-opt" data-menu="${opt.id}" aria-haspopup="menu"
                ${busy ? 'disabled' : ''} title="${esc(busy ? 'Changes apply to the next run' : `${opt.title}: ${c.desc}`)}">
                <i class="fa-solid ${c.icon}" aria-hidden="true"></i><span>${esc(c.short)}</span>
                <i class="fa-solid fa-chevron-down spai-opt-caret" aria-hidden="true"></i></button>`;
    }).join('');
    return pills + `<button type="button" class="spai-opt spai-opt-more" data-menu="more" aria-haspopup="menu"
              ${busy ? 'disabled' : ''} title="More settings" aria-label="More settings">
              <i class="fa-solid fa-sliders" aria-hidden="true"></i></button>`;
  }

  // ── Data ──────────────────────────────────────────────────────────────────

  async function load(sid) {
    if (!sid) return;
    S.sid = sid;
    S.loading = true;
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/speakers/insights`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      if (S.sid !== sid) return;
      S.insights = data;
      const run = data.run;
      if (run && (run.status === 'running' || run.status === 'queued')) {
        if (!S.running || S.running.run_id !== run.id) {
          S.running = runningFrom(run);
          await loadFrames(run.id);
        }
      } else if (S.running && run && S.running.run_id === run.id) {
        S.running = null;
      }
    } catch (e) {
      S.insights = { error: e.message };
    } finally {
      S.loading = false;
    }
    render();
    badge(sid);
  }

  let loadTimer = 0;
  function loadSoon(sid) {
    clearTimeout(loadTimer);
    loadTimer = setTimeout(() => load(sid || S.sid), 250);
  }

  function runningFrom(run) {
    const p = run.progress || {};
    const started = Date.parse(run.started_at ? (/Z|[+-]\d\d:\d\d$/.test(run.started_at) ? run.started_at : run.started_at + 'Z') : '');
    return {
      run_id: run.id, stage: p.stage || 'reading', label: p.label || 'Looking at the screen recording',
      done: p.frames_done || 0, sent: p.frames_sent || 0, cached: p.frames_cached || 0,
      started: isFinite(started) ? started : Date.now(),
    };
  }

  // The previews a run keeps in memory (null once they are gone).
  async function loadFrames(runId) {
    if (S.framesRun !== runId) { S.frames = new Map(); S.framesRun = runId; }
    try {
      const r = await fetch(`/api/speaker-runs/${encodeURIComponent(runId)}/frames`);
      const data = r.ok ? await r.json() : {};
      if (S.framesRun !== runId) return false;
      if (!Array.isArray(data.frames)) return false;
      for (const f of data.frames) S.frames.set(f.id, f);
      return true;
    } catch (_) {
      return false;
    }
  }

  function badge(sid) {
    const btn = $('speakers-btn');
    if (!btn || sid !== curSid()) return;
    const n = (S.sid === sid && S.insights && S.insights.suggestions) ? S.insights.suggestions.length : 0;
    btn.classList.toggle('has-ai-suggestions', enabled() && n > 0);
    btn.title = n > 0 ? `Manage speakers (${n} AI suggestion${n === 1 ? '' : 's'} waiting)` : 'Manage speakers';
  }

  function sessionReport(run) {
    const sessions = (run && run.report && run.report.sessions) || [];
    return sessions.find(s => s.session_id === S.sid) || null;
  }

  // ── Who's who: the meeting's speakers grouped by person ────────────────────

  function buildRoster(ins) {
    const rep = S.running ? null : sessionReport(ins.run);
    const decisions = (rep && rep.decisions) || {};
    const groups = [];
    const byId = new Map();
    const byKey = new Map();
    const noise = [];
    for (const sp of ins.speakers || []) {
      if (sp.owner) continue;
      if (sp.is_noise) { noise.push(sp); continue; }
      const named = !generic(sp.name, sp.key);
      const id = named ? `n:${norm(sp.name)}` : `k:${sp.key}`;
      let g = byId.get(id);
      if (!g) {
        g = { id, named, name: named ? String(sp.name).trim() : sp.key, members: [], lines: 0,
              seconds: 0, linked: false, by: new Set(), suggestions: [], findings: [] };
        byId.set(id, g);
        groups.push(g);
      }
      g.members.push(sp);
      g.lines += sp.lines;
      g.seconds += sp.seconds;
      if (sp.global_id) g.linked = true;
      if (sp.set_by) g.by.add(sp.set_by);
      byKey.set(sp.key, g);
    }
    const orphans = [];
    const rowOf = new Map((ins.speakers || []).map(s => [s.key, s]));
    for (const c of ins.suggestions || []) {
      // A name its speakers already carry has been answered.
      const keys = c.keys || [];
      if (c.type === 'name' && keys.length && keys.every(k => rowOf.has(k) && norm(rowOf.get(k).name) === norm(c.name))) continue;
      const g = byKey.get(keys[0]);
      (g ? g.suggestions : orphans).push(c);
    }
    const loose = [];
    for (const f of (rep && rep.findings) || []) {
      const g = (f.keys || []).length ? byKey.get(f.keys[0]) : null;
      if (g && f.kind !== 'edited_meanwhile') g.findings.push(f);
      else loose.push(f);
    }
    for (const g of groups) {
      g.members.sort((a, b) => b.seconds - a.seconds);
      g.color = keyColor(g.members[0].key, g.members[0].color);
      g.status = statusOf(g, rep);
    }
    groups.sort((a, b) => (b.suggestions.length > 0) - (a.suggestions.length > 0)
      || (b.named - a.named) || (b.seconds - a.seconds));
    const waiting = groups.flatMap(g => g.suggestions).concat(orphans);
    return { groups, noise, orphans, loose, byKey, decisions, waiting };
  }

  function statusOf(g, rep) {
    if (!rep) return null;
    const decisions = rep.decisions || {};
    const ds = g.members.map(m => decisions[m.key]).filter(Boolean);
    // What the screen showed for these voices, including looks set aside (a
    // turn whose voice disagreed, or a tile shown for several voices).
    const raw = {};
    for (const m of g.members) {
      for (const [p, n] of Object.entries((rep.seen || {})[m.key] || {})) raw[p] = (raw[p] || 0) + n;
    }
    const rawTop = Object.keys(raw).sort((a, b) => raw[b] - raw[a])[0];
    const rawNote = rawTop && (rep.non_specific || []).includes(rawTop)
      ? `${rawTop}'s tile was not reliable in this meeting, so it decided nothing`
      : 'Set aside: the voice did not match the screen';
    if (g.named) {
      const same = ds.filter(d => d.person && norm(d.person) === norm(g.name) && !d.tentative);
      if (same.length) {
        return { kind: 'seen', icon: 'fa-circle-check', text: 'Seen on screen',
                 conf: Math.max(...same.map(d => d.confidence || 0)) };
      }
      const other = ds.find(d => d.person && norm(d.person) !== norm(g.name) && !d.tentative
                                 && (d.confidence || 0) >= 0.7);
      if (other) return { kind: 'disagrees', icon: 'fa-triangle-exclamation', text: `Screen shows ${other.person}` };
      // Seen too seldom to decide: say who, since it may not be this name.
      const d = ds.find(x => Object.keys(x.votes || {}).length);
      const glimpsed = d && Object.keys(d.votes)[0];
      const times = d && d.turns > 1 ? `${d.turns} times` : 'once';
      if (glimpsed && norm(glimpsed) === norm(g.name)) return { kind: 'unsure', icon: 'fa-eye', text: `Seen ${times} on screen` };
      if (glimpsed) return { kind: 'unsure', icon: 'fa-circle-question', text: `Screen showed ${glimpsed} ${times}` };
      if (rawTop && norm(rawTop) !== norm(g.name)) {
        return { kind: 'unsure', icon: 'fa-circle-question', text: `Screen showed ${rawTop}`, note: rawNote };
      }
      return { kind: 'unseen', icon: 'fa-eye-slash', text: 'Not seen on screen' };
    }
    const guess = ds.find(d => d.person);
    if (guess) return { kind: 'guess', icon: 'fa-sparkles', text: `Looks like ${guess.person}`, person: guess.person };
    const glimpse = ds.map(d => Object.keys(d.votes || {})[0]).find(Boolean);
    if (glimpse) return { kind: 'glimpse', icon: 'fa-eye', text: `Screen showed ${glimpse}`, person: glimpse };
    if (rawTop) return { kind: 'glimpse', icon: 'fa-eye', text: `Screen showed ${rawTop}`, person: rawTop, note: rawNote };
    const longest = Math.max(0, ...g.members.map(m => m.lines ? m.seconds / m.lines : 0));
    if (g.seconds < 2.5 || longest < 0.8) return { kind: 'short', icon: 'fa-stopwatch', text: 'Too short to identify' };
    return { kind: 'unseen', icon: 'fa-eye-slash', text: 'Not seen speaking on screen' };
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  // ``mine``: the user's own click asked for it. Anything else (a run's
  // progress, a reload) waits while a name is being typed, so it is not lost.
  function render(mine = false) {
    const root = $('speaker-pane-ai');
    if (!root) return;
    const body = $('spai-body');
    const compose = $('spai-compose');
    if (!body || !compose) return;
    if (!mine && S.naming && body.querySelector('.spai-name-field')) return;
    syncComposer();
    if (!enabled()) {
      compose.hidden = true;
      body.innerHTML = offHtml();
      return;
    }
    const ins = S.insights;
    if (!ins) {
      compose.hidden = false;
      body.innerHTML = '<div class="spai-loading"><div class="cleanup-spinner"></div></div>';
      return;
    }
    if (ins.error) {
      compose.hidden = false;
      body.innerHTML = `<div class="spai-note warn"><i class="fa-solid fa-triangle-exclamation"></i> Could not load speaker detection: ${esc(ins.error)}</div>`;
      return;
    }
    compose.hidden = !ins.has_video;
    roster = buildRoster(ins);
    const top = body.scrollTop;
    body.innerHTML = [
      ins.has_video ? (S.running ? liveHtml() : summaryHtml(ins)) : noVideoHtml(),
      rosterHtml(ins),
      historyHtml(ins),
    ].join('');
    body.scrollTop = top;
    wire();
    mountNameField();
    tick();
  }

  function offHtml() {
    return `
      <div class="spai-empty">
        <div class="spai-empty-art"><i class="fa-solid fa-users-viewfinder"></i></div>
        <div class="spai-empty-title">AI speaker detection is off</div>
        <div class="spai-empty-text">When it is on, the app reads who Teams, Zoom or Meet showed as speaking in the screen recording, checks each reading against the voices, and names the speakers. Frames from the recording are sent to your AI provider.</div>
        <button class="speaker-manager-btn speaker-manager-btn-primary" data-act="turn-on"><i class="fa-solid fa-power-off"></i> Turn it on</button>
      </div>`;
  }

  function noVideoHtml() {
    return `
      <div class="spai-intro is-muted">
        <div class="spai-intro-art"><i class="fa-solid fa-video-slash"></i></div>
        <div class="spai-intro-main">
          <div class="spai-intro-title">No screen recording for this meeting</div>
          <div class="spai-intro-text">AI detection reads the meeting window from the screen recording. Turn on screen recording in Settings for future meetings. You can still name speakers below, or in Cleanup.</div>
        </div>
      </div>`;
  }

  function syncComposer() {
    const btn = $('spai-run');
    const input = $('spai-input');
    const opts = $('spai-options');
    if (btn) {
      if (S.running) {
        btn.innerHTML = '<i class="fa-solid fa-stop"></i> Stop';
        btn.classList.remove('speaker-manager-btn-primary');
        btn.classList.add('speaker-manager-btn-secondary');
        btn.title = 'Stop reading. Nothing is changed by a run that is stopped.';
        btn.onclick = cancel;
      } else {
        btn.innerHTML = '<i class="fa-solid fa-sparkles"></i> Identify speakers';
        btn.classList.add('speaker-manager-btn-primary');
        btn.classList.remove('speaker-manager-btn-secondary');
        btn.title = 'Read the screen recording and work out who is who (Enter)';
        btn.onclick = identify;
      }
    }
    if (input) {
      input.disabled = !!S.running;
      autogrow(input);
    }
    if (opts) opts.innerHTML = optionsHtml();
  }

  // One line until there is more to show. Measured only with text in it and
  // on screen: a box measured before the dialog has its width wraps the
  // placeholder a word a line and sticks at the maximum.
  function autogrow(input) {
    if (!input || input.tagName !== 'TEXTAREA') return;
    if (!input.value || !input.clientWidth) { input.style.height = ''; return; }
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 96)}px`;
  }

  // ── While a run works ─────────────────────────────────────────────────────

  const STEPS = [
    { id: 'reading', label: 'Reading the screen', icon: 'fa-eye' },
    { id: 'voices', label: 'Checking the voices', icon: 'fa-waveform-lines' },
    { id: 'deciding', label: 'Deciding who is who', icon: 'fa-users-viewfinder' },
  ];
  const STEP_OF = { planning: 0, reading: 0, voices: 1, deciding: 2, applying: 2 };

  function stepsHtml() {
    const at = STEP_OF[(S.running && S.running.stage) || 'reading'] || 0;
    return STEPS.map((s, i) => {
      const cls = i < at ? 'is-done' : (i === at ? 'is-active' : '');
      const icon = i < at ? 'fa-check' : s.icon;
      return `<div class="spai-step ${cls}"><span class="spai-step-dot"><i class="fa-solid ${icon}"></i></span><span class="spai-step-label">${esc(s.label)}</span></div>`;
    }).join('<span class="spai-step-line" aria-hidden="true"></span>');
  }

  function liveMetaHtml() {
    const r = S.running || {};
    const frames = [...S.frames.values()];
    const read = frames.filter(f => f.state === 'read').length;
    const out = frames.filter(f => f.state === 'looking').length;
    const parts = [];
    parts.push(read ? plural(read, 'frame read', 'frames read') : (out ? 'Reading the first frames' : esc(r.label || 'Looking at the screen recording')));
    if (out && read) parts.push(`${out} on the way`);
    if (r.cached) parts.push(`${plural(r.cached, 'frame', 'frames')} from earlier runs`);
    return `${parts.join(' · ')} · <span id="spai-elapsed">${clock((Date.now() - (r.started || Date.now())) / 1000)}</span>`;
  }

  function liveHtml() {
    return `
      <section class="spai-live" aria-label="Speaker detection in progress">
        <div class="spai-steps" id="spai-steps" role="status" aria-live="polite">${stepsHtml()}</div>
        <div class="spai-live-meta" id="spai-live-meta">${liveMetaHtml()}</div>
        <div class="spai-wall" id="spai-wall">${wallHtml()}</div>
        <div class="spai-seen" id="spai-seen">${seenHtml()}</div>
      </section>`;
  }

  function orderedFrames() {
    return [...S.frames.values()].sort((a, b) => b.id - a.id);
  }

  function wallHtml() {
    const list = orderedFrames();
    if (!list.length) {
      return Array.from({ length: 6 }, () => '<div class="spai-shot is-ghost" aria-hidden="true"><span class="spai-shot-frame"></span></div>').join('');
    }
    return list.map(shotHtml).join('');
  }

  const frac = v => `${(Math.max(0, Math.min(1, Number(v) || 0)) * 100).toFixed(2)}%`;

  function shotHtml(f) {
    const ar = f.w && f.h ? f.w / f.h : 16 / 9;
    const fit = ar >= 16 / 9 ? 'width:100%' : 'height:100%';
    const who = (f.speaking || []).filter(s => !s.self && s.name);
    const boxes = who.filter(s => Array.isArray(s.box) && s.box.length === 4).map(s => {
      const [x0, y0, x1, y1] = s.box;
      const c = nameColor(s.name) || 'var(--yellow)';
      return `<span class="spai-shot-box" style="left:${frac(x0)};top:${frac(y0)};width:${frac(x1 - x0)};height:${frac(y1 - y0)};--c:${c}"></span>`;
    }).join('');
    let caption;
    if (f.state === 'looking') caption = '<span class="spai-shot-status"><i class="fa-solid fa-circle-notch fa-spin"></i> Reading</span>';
    else if (f.state === 'failed') caption = '<span class="spai-shot-status">Not read</span>';
    else if (f.visible === false) caption = '<span class="spai-shot-status">Meeting not on screen</span>';
    else if (!who.length) caption = '<span class="spai-shot-status">No one highlighted</span>';
    else {
      caption = who.slice(0, 2).map(s => `<span class="spai-shot-name" style="--c:${nameColor(s.name) || 'var(--fg-muted)'}">${esc(s.name)}</span>`).join('')
        + (who.length > 2 ? `<span class="spai-shot-status">+${who.length - 2}</span>` : '');
    }
    const label = `${clock(f.t)}${f.kind === 'scout' ? ', finding the meeting window' : ''}`;
    return `
      <button type="button" class="spai-shot is-${esc(f.state)}" data-act="shot" data-fid="${f.id}" title="${esc(label)}">
        <span class="spai-shot-frame"><span class="spai-shot-pic" style="aspect-ratio:${ar.toFixed(4)};${fit}">
          <img src="/api/speaker-runs/${encodeURIComponent(S.framesRun)}/frames/${f.id}.jpg" alt="" draggable="false">
          ${boxes}<span class="spai-shot-scan" aria-hidden="true"></span>
        </span></span>
        <span class="spai-shot-time">${clock(f.t)}</span>${f.kind === 'scout' ? '<span class="spai-shot-kind">Layout</span>' : ''}
        <span class="spai-shot-who">${caption}</span>
      </button>`;
  }

  function seenHtml() {
    const counts = new Map();
    for (const f of S.frames.values()) {
      for (const s of f.speaking || []) {
        if (s.self || !s.name) continue;
        counts.set(s.name, (counts.get(s.name) || 0) + 1);
      }
    }
    if (!counts.size) return '';
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10);
    return `<span class="spai-seen-label">Seen speaking</span>` + top.map(([name, n]) =>
      `<span class="spai-seen-chip"><span class="spai-seen-dot" style="background:${nameColor(name) || 'var(--fg-subtle)'}"></span>${esc(name)}<b>${n}</b></span>`).join('');
  }

  // New and updated frames, painted into the wall where it is.
  function paintFrames(metas) {
    const wall = $('spai-wall');
    if (!wall) return;
    if (wall.querySelector('.is-ghost')) wall.innerHTML = '';
    for (const f of metas) {
      const html = shotHtml(S.frames.get(f.id) || f).trim();
      const old = wall.querySelector(`[data-fid="${f.id}"]`);
      if (old) {
        // Keep the loaded image: only the frame's state and caption change.
        const tmp = document.createElement('div');
        tmp.innerHTML = html;
        const next = tmp.firstElementChild;
        old.className = next.className;
        old.title = next.title;
        old.querySelector('.spai-shot-who').innerHTML = next.querySelector('.spai-shot-who').innerHTML;
        old.querySelectorAll('.spai-shot-box').forEach(b => b.remove());
        const pic = old.querySelector('.spai-shot-pic');
        next.querySelectorAll('.spai-shot-box').forEach(b => pic.insertBefore(b, pic.querySelector('.spai-shot-scan')));
      } else {
        wall.insertAdjacentHTML('afterbegin', html);
      }
    }
    const seen = $('spai-seen');
    if (seen) seen.innerHTML = seenHtml();
    const meta = $('spai-live-meta');
    if (meta && S.running) meta.innerHTML = liveMetaHtml();
  }

  let ticker = 0;
  function tick() {
    clearInterval(ticker);
    if (!S.running) return;
    ticker = setInterval(() => {
      const el = $('spai-elapsed');
      if (!S.running || !el) { clearInterval(ticker); return; }
      el.textContent = clock((Date.now() - S.running.started) / 1000);
    }, 1000);
  }

  // ── After a run ───────────────────────────────────────────────────────────

  function introHtml(ins) {
    const unnamed = roster ? roster.groups.find(g => !g.named) : null;
    const tries = [
      ['Just suggest', 'Just suggest, don\u2019t change anything'],
      unnamed ? [`Who is ${unnamed.name}?`, `Who is ${unnamed.name}?`] : null,
      ['Recheck my names', 'Recheck the names I set too'],
    ].filter(Boolean);
    return `
      <div class="spai-intro">
        <div class="spai-intro-art"><i class="fa-solid fa-users-viewfinder"></i></div>
        <div class="spai-intro-main">
          <div class="spai-intro-title">Work out who's who from the screen recording</div>
          <div class="spai-intro-text">It reads who the meeting app shows speaking, checks every reading against that moment's voice, and names the speakers. You decide how much it may do on its own, and every change can be undone.</div>
          <div class="spai-try"><span>Try</span>${tries.map(([label, text]) =>
            `<button type="button" class="spai-try-chip" data-act="try" data-text="${esc(text)}">${esc(label)}</button>`).join('')}</div>
        </div>
      </div>`;
  }

  function summaryHtml(ins) {
    const run = ins.run;
    if (!run) return introHtml(ins);
    const rep = sessionReport(run);
    if (run.status === 'failed') {
      return `<div class="spai-note warn"><i class="fa-solid fa-triangle-exclamation"></i> The last run failed: ${esc(run.error || 'unknown error')}</div>` + introHtml(ins);
    }
    const stopped = run.status === 'cancelled' || (rep && rep.status === 'cancelled');
    if (!rep || (stopped && !(rep.applied || []).length)) {
      return stopped ? '<div class="spai-note"><i class="fa-solid fa-circle-stop"></i> The last run was stopped before it changed anything.</div>' + introHtml(ins)
        : introHtml(ins);
    }
    if (rep.status === 'no_video') return '';
    const applied = rep.applied || [];
    const kinds = { name: 0, move: 0, split: 0 };
    let lines = 0;
    for (const a of applied) {
      const t = (a.op && a.op.type) || 'name';
      kinds[t] = (kinds[t] || 0) + 1;
      if (t !== 'name') lines += ((a.op && a.op.segment_ids) || []).length;
    }
    const parts = [];
    if (kinds.name) parts.push(`named ${plural(kinds.name, 'speaker')}`);
    if (lines) parts.push(`moved ${plural(lines, 'line')} to the right person`);
    if (kinds.split) parts.push(`split off ${plural(kinds.split, 'speaker')}`);
    const waiting = roster ? roster.waiting.length : (ins.suggestions || []).length;
    let text = parts.length ? parts.join(', ') : 'nothing needed changing';
    if (stopped) text = `stopped part way: ${text}`;
    text = text.charAt(0).toUpperCase() + text.slice(1);
    const meta = [];
    if (rep.frames) meta.push(plural(rep.frames, 'frame') + ' read');
    if (run.stats && run.stats.seconds != null) meta.push(`${run.stats.seconds} s`);
    if (rep.misreads) meta.push(`${plural(rep.misreads, 'screen misread')} caught by voice`);
    if (run.finished_at) meta.push(ago(run.finished_at));
    const undoable = applied.length && (ins.history || []).some(h => h.run_id === run.id && h.state === 'applied');
    const note = (run.instructions || '').trim();
    const chips = (run.chips || []).filter(Boolean);
    const framesBtn = S.framesRun === run.id && S.frames.size
      ? `<button class="speaker-manager-btn speaker-manager-btn-ghost" data-act="frames" aria-expanded="${S.framesShown}"><i class="fa-solid fa-images"></i> ${S.framesShown ? 'Hide frames' : `Frames (${S.frames.size})`}</button>`
      : (rep.frames ? `<button class="speaker-manager-btn speaker-manager-btn-ghost" data-act="frames" data-run="${esc(run.id)}"><i class="fa-solid fa-images"></i> Frames</button>` : '');
    const loose = (roster && roster.loose) || [];
    return `
      <section class="spai-summary ${waiting ? 'has-waiting' : ''}">
        <div class="spai-summary-main">
          <span class="spai-summary-icon"><i class="fa-solid ${waiting ? 'fa-circle-exclamation' : 'fa-circle-check'}"></i></span>
          <div class="spai-summary-text">
            <div class="spai-summary-title">${esc(text)}.${waiting ? ` <span class="spai-amber">${plural(waiting, 'suggestion')} below.</span>` : ''}</div>
            <div class="spai-summary-meta">${esc(meta.join(' · '))}</div>
            ${note ? `<div class="spai-summary-note"><i class="fa-regular fa-comment"></i> \u201c${esc(note)}\u201d${chips.length ? ` <span class="spai-summary-read">read as ${esc(chips.join(', '))}</span>` : ''}</div>` : ''}
          </div>
        </div>
        <div class="spai-summary-actions">
          ${framesBtn}
          ${waiting > 1 ? `<button class="speaker-manager-btn speaker-manager-btn-primary" data-act="apply-all"><i class="fa-solid fa-check-double"></i> Apply all ${waiting}</button>` : ''}
          ${undoable ? `<button class="speaker-manager-btn speaker-manager-btn-ghost" data-act="undo-run" data-run="${esc(run.id)}" title="Put back everything this run changed, voice samples included"><i class="fa-solid fa-rotate-left"></i> Undo all</button>` : ''}
        </div>
        ${loose.length ? `<div class="spai-summary-flags">${loose.map(f => `<div class="spai-finding"><i class="fa-regular fa-flag"></i><span>${esc(f.summary || '')}</span></div>`).join('')}</div>` : ''}
        ${S.framesShown && S.framesRun === run.id ? `<div class="spai-wall is-done" id="spai-wall">${wallHtml()}</div><div class="spai-seen" id="spai-seen">${seenHtml()}</div>` : ''}
      </section>`;
  }

  // ── Who's who ─────────────────────────────────────────────────────────────

  function rosterHtml(ins) {
    const R = roster;
    if (!R || (!R.groups.length && !R.noise.length)) return '';
    const named = R.groups.filter(g => g.named).length;
    const unnamed = R.groups.length - named;
    const voices = R.groups.reduce((n, g) => n + g.members.length, 0);
    const sum = [`<b>${named}</b> named`];
    if (unnamed) sum.push(`<button class="spai-link is-warn" data-act="jump-unnamed"><b>${unnamed}</b> unnamed</button>`);
    sum.push(`<b>${voices}</b> ${voices === 1 ? 'voice' : 'voices'}`);
    if (R.noise.length) sum.push(`<b>${R.noise.length}</b> noise`);
    const orphans = R.orphans.length ? `<div class="spai-person-suggests">${R.orphans.map(c => suggestHtml(c, null)).join('')}</div>` : '';
    return `
      <section class="spai-section spai-roster">
        <div class="spai-section-head">
          <h4>Who's who</h4>
          <span class="spai-roster-sum">${sum.join(' · ')}</span>
          <button class="spai-link" data-act="cleanup" title="Merge, split or relink groups by voice">Edit groups in Cleanup <i class="fa-solid fa-arrow-right"></i></button>
        </div>
        ${orphans}
        <div class="spai-people">${R.groups.map(personHtml).join('')}${noiseHtml(R.noise)}</div>
      </section>`;
  }

  function personHtml(g) {
    const open = !!S.people[g.id];
    const single = g.members.length === 1;
    const meta = [];
    meta.push(single ? esc(g.members[0].key) : `${g.members.length} voices`);
    meta.push(plural(g.lines, 'line'));
    meta.push(clock(g.seconds));
    const by = ['user', 'ai', 'voice_auto', 'agent', 'chat'].find(b => g.by.has(b));
    if (g.named && by) meta.push(NAMED_BY[by]);
    const st = g.status;
    const status = st ? `<span class="spai-status is-${st.kind}" title="${esc(st.note || st.text)}"><i class="fa-solid ${st.icon}"></i><span>${esc(st.text)}</span>${st.conf ? `<b>${pct(st.conf)}</b>` : ''}</span>` : '';
    const name = g.named
      ? `<span class="spai-person-label">${esc(g.name)}</span>${g.linked ? '<i class="fa-solid fa-link spai-person-link" title="Linked to a Voice Library profile"></i>' : ''}`
      : `<span class="spai-person-label is-placeholder">${esc(g.name)}</span><span class="spai-pill is-warn">Unnamed</span>`;
    const why = g.members.some(m => (roster.decisions[m.key] || {}).reason) || g.members.length > 1;
    const attention = g.suggestions.length || g.findings.length || !g.named;
    // A closer look at one speaker, where the last run was not sure.
    const look = g.named && canLook() && ['unsure', 'unseen', 'disagrees'].includes((st || {}).kind)
      ? `<button type="button" class="spai-icon-btn" data-act="look" data-group="${esc(g.id)}" title="Look again, more closely, at ${esc(g.name)}" aria-label="Look again at ${esc(g.name)}"><i class="fa-solid fa-magnifying-glass"></i></button>`
      : '';
    return `
      <div class="spai-person ${g.named ? '' : 'is-unnamed'} ${attention ? 'has-attention' : ''} ${open ? 'is-open' : ''}" data-group="${esc(g.id)}" style="--person:${g.color}">
        <div class="spai-person-head" ${why ? 'data-act="person"' : ''}>
          <span class="spai-swatch" style="background:${g.color}"></span>
          <div class="spai-person-main">
            <div class="spai-person-name">${name}</div>
            <div class="spai-person-meta">${meta.join(' · ')}</div>
          </div>
          ${status}
          <div class="spai-person-actions">
            ${look}
            <button type="button" class="spai-icon-btn" data-act="play" data-group="${esc(g.id)}" title="Play a sample of this voice" aria-label="Play ${esc(g.name)}"><i class="fa-solid fa-play"></i></button>
            ${why ? `<button type="button" class="spai-icon-btn spai-caret" data-act="person" aria-expanded="${open}" title="${open ? 'Hide' : 'Show'} what the screen showed" aria-label="Details"><i class="fa-solid fa-chevron-down"></i></button>` : ''}
          </div>
        </div>
        ${g.suggestions.length ? `<div class="spai-person-suggests">${g.suggestions.map(c => suggestHtml(c, g)).join('')}</div>` : ''}
        ${g.findings.map(f => `<div class="spai-finding"><i class="fa-regular fa-flag"></i><span>${esc(f.summary || '')}</span></div>`).join('')}
        ${g.named ? '' : fixesHtml(g)}
        ${open ? detailHtml(g) : ''}
      </div>`;
  }

  function fixesHtml(g) {
    const key = g.members[0].key;
    if (S.naming === key) {
      return `<div class="spai-fixes is-naming"><span class="spai-name-field" data-key="${esc(key)}"></span>
        <button type="button" class="speaker-manager-btn speaker-manager-btn-primary" data-act="name-save" data-key="${esc(key)}">Save</button>
        <button type="button" class="speaker-manager-btn speaker-manager-btn-ghost" data-act="name-cancel">Cancel</button></div>`;
    }
    const st = g.status || {};
    const hinted = st.person && !g.suggestions.some(c => c.type === 'name');
    return `
      <div class="spai-fixes">
        ${hinted ? `<button type="button" class="spai-fix is-hint" data-act="name-as" data-key="${esc(key)}" data-name="${esc(st.person)}" title="${esc(st.note || `The screen showed ${st.person}, not often enough to be sure`)}"><i class="fa-solid fa-user-check"></i> It's ${esc(st.person)}</button>` : ''}
        <button type="button" class="spai-fix" data-act="name" data-key="${esc(key)}"><i class="fa-solid fa-user-pen"></i> Name\u2026</button>
        ${canLook() && st.kind !== 'short' ? `<button type="button" class="spai-fix" data-act="look" data-group="${esc(g.id)}" title="Read more of the screen recording for this speaker"><i class="fa-solid fa-magnifying-glass"></i> Look again</button>` : ''}
        <button type="button" class="spai-fix" data-act="noise" data-key="${esc(key)}" title="Not a person: hide these lines as noise"><i class="fa-solid fa-volume-xmark"></i> Noise</button>
      </div>`;
  }

  const canLook = () => !S.running && !!(S.insights && S.insights.has_video && !S.insights.recording);

  function detailHtml(g) {
    return `<div class="spai-voices">${g.members.map(m => {
      const d = roster.decisions[m.key];
      let why = 'The screen was not read while this voice spoke.';
      if (d && d.person) why = d.reason.charAt(0).toUpperCase() + d.reason.slice(1) + '.';
      else if (d) {
        const top = Object.keys(d.votes || {})[0];
        why = top ? `The screen showed ${top} in ${plural(d.turns || 1, 'of its turns', 'of its turns')}, not enough to be sure.`
          : 'No one was highlighted on screen while this voice spoke.';
      }
      const named = !generic(m.name, m.key) && norm(m.name) !== norm(g.name) ? ` (${esc(m.name)})` : '';
      const by = NAMED_BY[m.set_by] ? ` · ${NAMED_BY[m.set_by]}` : '';
      const ev = d && (d.evidence || []).length ? filmHtml(d.evidence) : '';
      return `
        <div class="spai-voice">
          <div class="spai-voice-head"><span class="spai-voice-key">${esc(m.key)}${named}</span><span class="spai-voice-meta">${plural(m.lines, 'line')} · ${clock(m.seconds)}${by}</span></div>
          <div class="spai-voice-why">${esc(why)}</div>
          ${ev}
        </div>`;
    }).join('')}</div>`;
  }

  function noiseHtml(list) {
    if (!list.length) return '';
    const lines = list.reduce((n, s) => n + s.lines, 0);
    return `<div class="spai-person is-noise"><div class="spai-person-head">
      <span class="spai-swatch"></span>
      <div class="spai-person-main"><div class="spai-person-name"><span class="spai-person-label is-placeholder">Noise</span></div>
      <div class="spai-person-meta">${plural(lines, 'line')} marked as noise, hidden in the transcript</div></div></div></div>`;
  }

  function proposal(c, g) {
    const secs = (/\((\d+) s\)/.exec(c.summary || '') || [])[1];
    const lines = `<b>${plural(c.lines || 0, 'line')}</b>${secs ? ` (${secs} s)` : ''}`;
    const from = c.keys && c.keys.length ? ` from ${esc(c.keys.join(', '))}` : '';
    if (c.type === 'move') return { text: `${lines}${from} sound like <b>${esc(c.name)}</b>`, verb: `Move to ${c.name}` };
    if (c.type === 'split') return { text: `${lines}${from} sound like <b>${esc(c.name)}</b>, who has no speaker of their own yet`, verb: 'Split off' };
    const rename = g && g.named;
    return rename
      ? { text: `The screen shows <b>${esc(c.name)}</b>, not ${esc(g.name)}`, verb: `Rename to ${c.name}` }
      : { text: `${g ? 'Probably' : esc((c.keys || []).join(', ')) + ' is probably'} <b>${esc(c.name)}</b>`, verb: `Name ${c.name}` };
  }

  function suggestHtml(c, g) {
    const p = proposal(c, g);
    const ev = (c.evidence || []).length;
    const why = c.reason ? c.reason.charAt(0).toUpperCase() + c.reason.slice(1) : '';
    const teaches = c.train && c.type === 'name' && prefs().speaker_ai_library_writes !== 'never'
      ? ' · applying it teaches the voice' : '';
    return `
      <div class="spai-suggest" data-id="${c.id}">
        <i class="fa-solid ${TYPE_ICON[c.type] || 'fa-pen'} spai-suggest-icon"></i>
        <div class="spai-suggest-main">
          <div class="spai-suggest-text">${p.text}</div>
          <div class="spai-suggest-why">${why ? esc(why) + ' · ' : ''}${pct(c.confidence)} sure${teaches}</div>
          ${S.open[c.id] && ev ? filmHtml(c.evidence) : ''}
        </div>
        <div class="spai-suggest-actions">
          ${ev ? `<button type="button" class="spai-icon-btn ${S.open[c.id] ? 'is-on' : ''}" data-act="why" data-id="${c.id}" title="Show the frames this was read from" aria-label="Show evidence"><i class="fa-solid fa-images"></i></button>` : ''}
          <button type="button" class="speaker-manager-btn speaker-manager-btn-ghost" data-act="dismiss" data-id="${c.id}" title="Not right: don't suggest it again">Dismiss</button>
          <button type="button" class="speaker-manager-btn speaker-manager-btn-primary" data-act="apply" data-id="${c.id}">${esc(p.verb)}</button>
        </div>
      </div>`;
  }

  function filmHtml(ids) {
    const list = (ids || []).slice(0, 6).map(o => parseInt(o, 10)).filter(isFinite);
    if (!list.length) return '';
    return `<div class="spai-film">${list.map((o, i) =>
      `<button type="button" class="spai-frame" data-act="evidence" data-obs="${o}" data-set="${list.join(',')}" data-i="${i}" title="Open the full frame">
         <img loading="lazy" alt="Frame the screen was read from" onerror="this.parentElement.classList.add('is-missing')"
              src="/api/sessions/${encodeURIComponent(S.sid)}/speakers/evidence/${o}.jpg">
       </button>`).join('')}</div>`;
  }

  // ── History ───────────────────────────────────────────────────────────────

  function changeRow(c) {
    const icon = TYPE_ICON[c.type] || 'fa-pen';
    const conf = c.confidence != null ? `<span class="spai-conf" title="How sure the run was">${pct(c.confidence)}</span>` : '';
    const who = c.actor && c.actor !== 'ai' ? `<span class="spai-tag">${esc(ACTOR[c.actor] || c.actor)}</span>` : '';
    const trained = c.trained ? '<span class="spai-tag" title="Voice samples were added to the profile; undo removes them"><i class="fa-solid fa-waveform-lines"></i> trained</span>' : '';
    const undone = c.state === 'undone' ? '<span class="spai-tag">undone</span>' : '';
    const ev = (c.evidence || []).length;
    return `
      <div class="spai-row ${c.state === 'undone' ? 'is-undone' : ''}" data-id="${c.id}">
        <i class="fa-solid ${icon} spai-row-icon"></i>
        <div class="spai-row-main">
          <div class="spai-row-text">${esc(c.summary || '')}</div>
          <div class="spai-row-meta">${conf}${who}${trained}${undone}<span>${esc(ago(c.undone_at || c.applied_at || c.created_at))}</span></div>
          ${S.open[c.id] && ev ? filmHtml(c.evidence) : ''}
        </div>
        <div class="spai-row-actions">
          ${ev ? `<button type="button" class="spai-icon-btn ${S.open[c.id] ? 'is-on' : ''}" data-act="why" data-id="${c.id}" title="Show the frames this was read from" aria-label="Show evidence"><i class="fa-solid fa-images"></i></button>` : ''}
          ${c.state === 'applied' ? `<button type="button" class="speaker-manager-btn speaker-manager-btn-ghost" data-act="undo" data-id="${c.id}" title="Put this back as it was">Undo</button>` : ''}
        </div>
      </div>`;
  }

  function historyHtml(ins) {
    const list = ins.history || [];
    if (!list.length) return '';
    const live = list.filter(c => c.state === 'applied').length;
    return `
      <section class="spai-section spai-history ${S.historyOpen ? 'is-open' : ''}">
        <button type="button" class="spai-history-toggle" data-act="history" aria-expanded="${S.historyOpen}">
          <i class="fa-solid fa-clock-rotate-left"></i><span class="spai-history-title">History</span>
          <span class="spai-count">${list.length}</span>
          <span class="spai-history-sum">${live ? `${plural(live, 'change')} in place, undo any of them here` : 'everything was undone'}</span>
          <i class="fa-solid fa-chevron-down spai-history-caret"></i>
        </button>
        ${S.historyOpen ? `<div class="spai-history-list">${list.slice(0, 60).map(changeRow).join('')}</div>` : ''}
      </section>`;
  }

  // ── Clicks ────────────────────────────────────────────────────────────────

  function wire() {
    const body = $('spai-body');
    if (body && !body.dataset.wired) {
      body.dataset.wired = '1';
      body.addEventListener('click', onClick);
    }
    const compose = $('spai-compose');
    if (compose && !compose.dataset.wired) {
      compose.dataset.wired = '1';
      compose.addEventListener('click', e => {
        const b = e.target.closest('[data-menu]');
        if (b && !b.disabled) openMenu(b, b.dataset.menu);
      });
      const input = $('spai-input');
      if (input) input.addEventListener('input', () => autogrow(input));
    }
  }

  function onClick(e) {
    const b = e.target.closest('[data-act]');
    if (!b || b.disabled) return;
    const id = b.dataset.id ? parseInt(b.dataset.id, 10) : null;
    switch (b.dataset.act) {
      case 'apply': return act(`/api/speaker-changes/${id}/apply`, b, 'Applied.');
      case 'dismiss': return act(`/api/speaker-changes/${id}/dismiss`, b, 'Dismissed. It won\u2019t be suggested again.');
      case 'undo': return undo(id, b);
      case 'undo-run': return undoRun(b.dataset.run, b);
      case 'apply-all': return applyAll(b);
      case 'why': S.open[id] = !S.open[id]; return render(true);
      case 'evidence': return openEvidence(b);
      case 'shot': return openShot(parseInt(b.dataset.fid, 10));
      case 'frames': return toggleFrames(b);
      case 'history': S.historyOpen = !S.historyOpen; return render(true);
      case 'person': {
        const card = b.closest('[data-group]');
        if (!card || e.target.closest('button:not([data-act="person"])')) return;
        S.people[card.dataset.group] = !S.people[card.dataset.group];
        return render(true);
      }
      case 'play': return play(b);
      case 'look': {
        const g = roster && roster.groups.find(x => x.id === b.dataset.group);
        if (g) identify({ targets: g.members.map(m => m.key), depth: 'thorough' });
        return;
      }
      case 'name': S.naming = b.dataset.key; return render(true);
      case 'name-cancel': S.naming = null; return render(true);
      case 'name-save': return saveName(b.dataset.key, nameFieldValue());
      case 'name-as': return saveName(b.dataset.key, b.dataset.name);
      case 'noise': return markNoise(b.dataset.key, b);
      case 'try': return tryText(b.dataset.text);
      case 'cleanup': return window.showSpeakerModalPane && window.showSpeakerModalPane('cleanup');
      case 'jump-unnamed': {
        const el = document.querySelector('#spai-body .spai-person.is-unnamed');
        if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); el.classList.add('is-flash'); setTimeout(() => el.classList.remove('is-flash'), 1200); }
        return;
      }
      case 'turn-on': return setEnabled(true);
    }
  }

  function tryText(text) {
    const input = $('spai-input');
    if (!input || input.disabled) return;
    input.value = text || '';
    autogrow(input);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }

  // ── Naming an unnamed speaker in place ────────────────────────────────────

  let nameCombo = null;

  async function libraryPeople() {
    if (S.library) return S.library;
    try {
      const r = await fetch('/api/fingerprint/speakers');
      const list = r.ok ? await r.json() : [];
      const me = typeof window._meSpeakerGlobalId === 'string' ? window._meSpeakerGlobalId : null;
      S.library = (Array.isArray(list) ? list : []).filter(p => p.name && p.id !== me);
    } catch (_) {
      S.library = [];
    }
    return S.library;
  }

  async function mountNameField() {
    const slot = document.querySelector('#spai-body .spai-name-field');
    if (!slot || typeof uiCombobox !== 'function') { nameCombo = null; return; }
    const key = slot.dataset.key;
    const here = (roster ? roster.groups : []).filter(g => g.named)
      .map(g => ({ id: g.id, label: g.name, sublabel: 'In this meeting', color: g.color }));
    // Enter takes the highlighted match, so a half-typed name finds the
    // person; with no match, or with Save, it is the name as typed.
    nameCombo = uiCombobox({
      mount: slot, items: here, placeholder: 'Who is this? Type a name', ariaLabel: `Name ${key}`,
      emptyText: 'A new name: press Enter to use it',
      onSelect: item => saveName(key, item.label),
    });
    nameCombo.input.addEventListener('keydown', ev => {
      if (ev.defaultPrevented) return;
      if (ev.key === 'Enter') { ev.preventDefault(); saveName(key, nameCombo.getValue()); }
      if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); S.naming = null; render(true); }
    });
    nameCombo.focus();
    const lib = await libraryPeople();
    if (!nameCombo || S.naming !== key) return;
    const seen = new Set(here.map(i => norm(i.label)));
    nameCombo.setItems(here.concat(lib.filter(p => !seen.has(norm(p.name)))
      .map(p => ({ id: p.id, label: p.name, sublabel: 'Voice Library', color: safeColor(p.color) || undefined }))));
  }

  const nameFieldValue = () => (nameCombo ? nameCombo.getValue() : '');

  async function saveName(key, name) {
    name = String(name || '').trim();
    if (!key || !name) return;
    if (generic(name, key)) { toast('Type the person\u2019s name.', 'warn', 'spai'); return; }
    S.naming = null;
    try {
      if (typeof applySpeakerProfileUpdate === 'function') applySpeakerProfileUpdate({ speaker_key: key, name });
      if (typeof persistSpeakerLabel === 'function') await persistSpeakerLabel(key, name);
      toast(`Named ${key} ${name}.`, 'success', 'spai');
    } catch (e) {
      toast(e.message || 'Could not name the speaker.', 'error', 'spai');
    }
    load(S.sid);
  }

  async function markNoise(key, btn) {
    if (!key || typeof _markSpeakerAsNoise !== 'function') return;
    btn.disabled = true;
    await _markSpeakerAsNoise(key);
    toast(`${key} is marked as noise. Its lines are hidden from the transcript.`, 'success', 'spai');
    setTimeout(() => load(S.sid), 400);
  }

  function play(btn) {
    const g = roster && roster.groups.find(x => x.id === btn.dataset.group);
    if (!g || typeof window.playSpeakerVoice !== 'function') return;
    window.playSpeakerVoice({ sessionId: S.sid, speakerKeys: g.members.map(m => m.key), button: btn });
  }

  // ── Actions ───────────────────────────────────────────────────────────────

  async function identify() {
    const sid = S.sid || curSid();
    if (!sid || S.running) return;
    const input = $('spai-input');
    const instructions = (input && input.value || '').trim();
    const btn = $('spai-run');
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/speakers/identify`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instructions }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      begin(data.run.id, data.run);
      if (input) { input.value = ''; autogrow(input); }
    } catch (e) {
      toast(e.message, 'error', 'spai');
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function begin(runId, run) {
    if (S.running && S.running.run_id === runId) return;
    S.running = runningFrom(Object.assign({ id: runId, started_at: new Date().toISOString() }, run || {}));
    S.frames = new Map();
    S.framesRun = runId;
    S.framesShown = false;
    S.naming = null;
    if (S.insights) S.insights.run = Object.assign({}, run || {}, { id: runId, status: 'running' });
    render();
  }

  async function cancel() {
    if (!S.running) return;
    const btn = $('spai-run');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> Stopping'; }
    await fetch(`/api/speaker-runs/${encodeURIComponent(S.running.run_id)}/cancel`, { method: 'POST' }).catch(() => {});
    setTimeout(() => { if (btn) btn.disabled = false; }, 1500);
  }

  async function act(url, btn, done) {
    btn.disabled = true;
    const card = btn.closest('.spai-suggest');
    if (card) card.classList.add('is-busy');
    try {
      const r = await fetch(url, { method: 'POST' });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      toast(done, 'success', 'spai');
    } catch (e) {
      toast(e.message, 'error', 'spai');
    }
    load(S.sid);
  }

  async function applyAll(btn) {
    // In the order the run made them: a name before the lines that move to it.
    const ids = (roster ? roster.waiting : []).map(c => c.id).sort((a, b) => a - b);
    document.querySelectorAll('#spai-body [data-act="apply"], #spai-body [data-act="dismiss"]')
      .forEach(b => { b.disabled = true; });
    btn.disabled = true;
    let ok = 0;
    let firstError = '';
    for (const id of ids) {
      const r = await fetch(`/api/speaker-changes/${id}/apply`, { method: 'POST' }).catch(() => null);
      if (r && r.ok) { ok++; continue; }
      if (!firstError && r) firstError = ((await r.json().catch(() => ({}))).error) || '';
    }
    toast(`Applied ${ok} of ${ids.length}.` + (firstError ? ` ${firstError}` : ''),
      ok === ids.length ? 'success' : 'warn', 'spai');
    load(S.sid);
  }

  async function undo(id, btn, force = false) {
    btn.disabled = true;
    const r = await fetch(`/api/speaker-changes/${id}/undo`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ force }),
    }).catch(() => null);
    const data = r ? await r.json().catch(() => ({})) : {};
    if (r && r.ok) {
      toast('Put back as it was.', 'success', 'spai');
    } else if (data.conflict && typeof uiConfirm === 'function') {
      const go = await uiConfirm({
        title: 'Changed again since',
        message: 'Those speakers were edited after this change. Undoing it anyway puts them back to how they were before it, losing the later edit.',
        confirmLabel: 'Undo anyway', danger: true,
      });
      if (go) return undo(id, btn, true);
    } else {
      toast(data.error || 'Undo failed.', 'error', 'spai');
    }
    load(S.sid);
  }

  async function undoRun(runId, btn) {
    btn.disabled = true;
    const r = await fetch(`/api/speaker-runs/${encodeURIComponent(runId)}/undo`, { method: 'POST' }).catch(() => null);
    const data = r ? await r.json().catch(() => ({})) : {};
    if (r && r.ok) {
      const n = (data.undone || []).length;
      const skipped = (data.conflicts || []).length;
      toast(`Undid ${plural(n, 'change')} from this run.` +
        (skipped ? ` ${skipped} were edited again since and were left as they are.` : ''),
        skipped ? 'warn' : 'success', 'spai');
    } else {
      toast(data.error || 'Undo failed.', 'error', 'spai');
    }
    load(S.sid);
  }

  async function toggleFrames(btn) {
    const run = S.insights && S.insights.run;
    if (!run) return;
    if (S.framesShown) { S.framesShown = false; return render(true); }
    if (S.framesRun !== run.id || !S.frames.size) {
      btn.disabled = true;
      const kept = await loadFrames(run.id);
      if (!kept || !S.frames.size) {
        toast('The frames of that run are no longer kept. Open a change\u2019s frames to see what it was read from.', 'info', 'spai');
        btn.disabled = false;
        return;
      }
    }
    S.framesShown = true;
    render(true);
  }

  // ── The lightbox: a frame at full size, with its neighbours a key away ────

  let box = null;   // {items: [{src, caption}], i}

  function openShot(fid) {
    const list = orderedFrames();
    const items = list.map(f => {
      const who = (f.speaking || []).filter(s => !s.self && s.name).map(s => s.name);
      const what = f.state === 'looking' ? 'being read' : f.state === 'failed' ? 'not read'
        : f.visible === false ? 'meeting not on screen' : who.length ? `${who.join(' and ')} speaking` : 'no one highlighted';
      return {
        src: f.obs && f.state === 'read' ? `/api/sessions/${encodeURIComponent(S.sid)}/speakers/evidence/${f.obs}.jpg?full=1`
          : `/api/speaker-runs/${encodeURIComponent(S.framesRun)}/frames/${f.id}.jpg`,
        caption: `${clock(f.t)} · ${what}`,
      };
    });
    const i = Math.max(0, list.findIndex(f => f.id === fid));
    lightbox(items, i);
  }

  function openEvidence(b) {
    if (b.classList.contains('is-missing')) return;
    const ids = (b.dataset.set || b.dataset.obs).split(',');
    const items = ids.map(o => ({
      src: `/api/sessions/${encodeURIComponent(S.sid)}/speakers/evidence/${parseInt(o, 10)}.jpg?full=1`,
      caption: 'The tile read as speaking is boxed in yellow',
    }));
    lightbox(items, parseInt(b.dataset.i, 10) || 0);
  }

  function lightbox(items, i) {
    if (!items.length) return;
    let el = $('spai-lightbox');
    if (!el) {
      el = document.createElement('div');
      el.id = 'spai-lightbox';
      el.className = 'spai-lightbox';
      el.setAttribute('role', 'dialog');
      el.setAttribute('aria-label', 'Frame');
      el.innerHTML = `
        <figure class="spai-lightbox-fig"><img alt="Frame from the screen recording"><figcaption></figcaption></figure>
        <button class="spai-lightbox-nav is-prev" data-nav="-1" aria-label="Previous frame"><i class="fa-solid fa-chevron-left"></i></button>
        <button class="spai-lightbox-nav is-next" data-nav="1" aria-label="Next frame"><i class="fa-solid fa-chevron-right"></i></button>
        <button class="spai-lightbox-close" aria-label="Close"><i class="fa-solid fa-xmark"></i></button>`;
      el.addEventListener('click', e => {
        const nav = e.target.closest('[data-nav]');
        if (nav) { e.stopPropagation(); return step(parseInt(nav.dataset.nav, 10)); }
        if (e.target.closest('img')) return;
        closeLightbox();
      });
      document.body.appendChild(el);
    }
    box = { items, i };
    show();
    el.hidden = false;
  }

  function show() {
    const el = $('spai-lightbox');
    if (!el || !box) return;
    const it = box.items[box.i];
    el.querySelector('img').src = it.src;
    el.querySelector('figcaption').textContent = box.items.length > 1
      ? `${it.caption}  (${box.i + 1} of ${box.items.length})` : it.caption;
    el.querySelectorAll('[data-nav]').forEach(b => { b.hidden = box.items.length < 2; });
  }

  function step(d) {
    if (!box) return;
    box.i = (box.i + d + box.items.length) % box.items.length;
    show();
  }

  function closeLightbox() {
    const el = $('spai-lightbox');
    if (el) el.hidden = true;
    box = null;
  }

  // ── The composer's menus ──────────────────────────────────────────────────

  let menu = null;    // {el, anchor, kind}

  function menuHtml(kind) {
    if (kind === 'more') {
      const p = prefs();
      const sw = (pref, on, label, desc) => `
        <button type="button" class="spai-menu-item is-switch" role="menuitemcheckbox" aria-checked="${on}" data-pref="${pref}">
          <span class="spai-menu-main"><span class="spai-menu-title">${esc(label)}</span><span class="spai-menu-desc">${esc(desc)}</span></span>
          <span class="spai-switch" aria-hidden="true"></span>
        </button>`;
      return `
        <div class="spai-menu-head">More settings</div>
        ${sw('speaker_ai_after_meeting', p.speaker_ai_after_meeting !== false, 'Run after each meeting', 'Names the speakers once the transcript is final, and after a reanalysis.')}
        ${sw('speaker_ai_respect_user_labels', p.speaker_ai_respect_user_labels !== false, 'Leave names I set alone', 'A name you typed is only questioned, never changed, unless you ask.')}
        <div class="spai-menu-rule"></div>
        <button type="button" class="spai-menu-item" role="menuitem" data-go="settings">
          <i class="fa-solid fa-gear spai-menu-icon"></i>
          <span class="spai-menu-main"><span class="spai-menu-title">Models and all settings</span><span class="spai-menu-desc">Settings &gt; Speakers</span></span>
        </button>`;
    }
    const opt = OPTIONS.find(o => o.id === kind);
    const cur = optionValue(opt);
    return `<div class="spai-menu-head">${esc(opt.title)}</div>` + opt.choices.map(c => `
      <button type="button" class="spai-menu-item" role="menuitemradio" aria-checked="${c.v === cur}" data-pick="${c.v}">
        <i class="fa-solid ${c.icon} spai-menu-icon"></i>
        <span class="spai-menu-main"><span class="spai-menu-title">${esc(c.short)}</span><span class="spai-menu-desc">${esc(c.desc)}</span></span>
        <i class="fa-solid fa-check spai-menu-check"></i>
      </button>`).join('') + '<div class="spai-menu-foot">Saved to Settings. Your words for a run can still ask for more or less.</div>';
  }

  function openMenu(anchor, kind) {
    if (menu && menu.kind === kind) { closeMenu(); return; }
    closeMenu();
    const el = document.createElement('div');
    el.className = 'spai-menu';
    el.setAttribute('role', 'menu');
    el.innerHTML = menuHtml(kind);
    document.body.appendChild(el);
    menu = { el, anchor, kind };
    anchor.setAttribute('aria-expanded', 'true');
    place(el, anchor);
    el.addEventListener('click', e => {
      const item = e.target.closest('.spai-menu-item');
      if (!item) return;
      if (item.dataset.pick) return pick(kind, item.dataset.pick);
      if (item.dataset.pref) {
        const on = item.getAttribute('aria-checked') !== 'true';
        savePrefValue(item.dataset.pref, on);
        item.setAttribute('aria-checked', String(on));
        return;
      }
      if (item.dataset.go === 'settings') { closeMenu(); openSettingsPanel(); }
    });
    const first = el.querySelector('[aria-checked="true"]') || el.querySelector('.spai-menu-item');
    if (first) first.focus({ preventScroll: true });
  }

  function place(el, anchor) {
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    const below = window.innerHeight - r.bottom - 8;
    const top = below >= h || below >= r.top ? r.bottom + 6 : r.top - h - 6;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    el.style.top = `${Math.max(8, top)}px`;
    el.style.left = `${left}px`;
  }

  function closeMenu() {
    if (!menu) return;
    menu.anchor.setAttribute('aria-expanded', 'false');
    menu.el.remove();
    menu = null;
  }

  function pick(kind, value) {
    const opt = OPTIONS.find(o => o.id === kind);
    savePrefValue(opt.pref, value);
    closeMenu();
    syncComposer();
    const c = opt.choices.find(x => x.v === value);
    toast(`${c.short}: saved to Settings.`, 'success', 'spai-setting');
  }

  function savePrefValue(pref, value) {
    if (typeof savePref === 'function') savePref(pref, value);
    syncSettings();
  }

  document.addEventListener('mousedown', e => {
    if (menu && !menu.el.contains(e.target) && !menu.anchor.contains(e.target)) closeMenu();
  }, true);
  window.addEventListener('resize', closeMenu);
  document.addEventListener('scroll', e => { if (menu && !menu.el.contains(e.target)) closeMenu(); }, true);

  // Escape closes the innermost thing first: the lightbox, then a menu, then
  // the name field. Capture phase, so the dialog's own Escape never sees it.
  document.addEventListener('keydown', e => {
    const lb = $('spai-lightbox');
    if (lb && !lb.hidden) {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeLightbox(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); step(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); step(1); }
      return;
    }
    if (menu && e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); const a = menu.anchor; closeMenu(); a.focus(); return; }
    if (menu && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      const items = [...menu.el.querySelectorAll('.spai-menu-item')];
      const at = items.indexOf(document.activeElement);
      const next = items[(at + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length];
      if (next) { e.preventDefault(); next.focus(); }
    }
  }, true);

  // ── Dialog tabs ───────────────────────────────────────────────────────────

  function paneFor(requested) {
    if (requested === 'cleanup' || requested === 'ai') return enabled() || requested === 'cleanup' ? requested : 'cleanup';
    return enabled() ? 'ai' : 'cleanup';
  }

  // ``fromTab``: switched by the user; openSpeakerManager loads Cleanup itself.
  function showPane(pane, fromTab = false) {
    S.pane = pane;
    closeMenu();
    const tabs = $('speaker-modal-modes');
    if (tabs) tabs.hidden = !enabled();
    const ai = $('speaker-pane-ai');
    const cleanup = $('speaker-pane-cleanup');
    if (ai) ai.hidden = pane !== 'ai';
    if (cleanup) cleanup.hidden = pane !== 'cleanup';
    document.querySelectorAll('#speaker-modal-modes [role="tab"]').forEach(t => {
      const on = t.dataset.pane === pane;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (pane === 'ai') {
      const sid = curSid();
      if (sid && (S.sid !== sid || !S.insights)) { S.insights = null; S.sid = sid; render(); load(sid); }
      else { render(); if (sid) load(sid); }
      setTimeout(() => { const i = $('spai-input'); if (i && !i.disabled && !S.naming) i.focus(); }, 30);
    } else if (fromTab && typeof loadSpeakerClusters === 'function' &&
               (typeof _cleanupState === 'undefined' || !_cleanupState ||
                _cleanupState.sessionId !== curSid())) {
      loadSpeakerClusters();
    }
  }

  window.showSpeakerModalPane = pane => showPane(paneFor(pane), true);

  // ── SSE ───────────────────────────────────────────────────────────────────

  function onRunStart(d) {
    if (!d || !(d.sessions || []).includes(S.sid)) return;
    begin(d.run_id, { id: d.run_id, chips: d.chips, status: 'running', started_at: new Date().toISOString() });
  }

  function onRunProgress(d) {
    if (!d || !S.running || d.run_id !== S.running.run_id) return;
    if (d.session_id && d.session_id !== S.sid) return;
    const r = S.running;
    const stage = d.stage || r.stage;
    const moved = stage !== r.stage;
    Object.assign(r, {
      stage, label: d.label || r.label,
      done: d.frames_done != null ? d.frames_done : r.done,
      sent: d.frames_sent != null ? d.frames_sent : r.sent,
      cached: d.frames_cached != null ? d.frames_cached : r.cached,
    });
    if (moved) { const s = $('spai-steps'); if (s) s.innerHTML = stepsHtml(); }
    const meta = $('spai-live-meta');
    if (meta) meta.innerHTML = liveMetaHtml();
  }

  function onRunFrames(d) {
    if (!d || d.session_id !== S.sid || !(d.frames || []).length) return;
    if (S.framesRun !== d.run_id) {
      if (!S.running || S.running.run_id !== d.run_id) return;
      S.frames = new Map();
      S.framesRun = d.run_id;
    }
    for (const f of d.frames) S.frames.set(f.id, Object.assign(S.frames.get(f.id) || {}, f));
    if (S.pane === 'ai' && _managerOpen()) paintFrames(d.frames);
  }

  function onRunDone(d) {
    if (!d) return;
    const mine = (d.sessions || []).includes(S.sid);
    if (S.running && d.run_id === S.running.run_id) S.running = null;
    for (const f of S.frames.values()) if (f.state === 'looking') f.state = 'failed';
    const current = curSid();
    // Say so when the page is not showing the result: another meeting, or
    // the dialog closed. Suggestions waiting are the part worth a toast.
    const sessions = (d.report && d.report.sessions) || [];
    const waiting = sessions.reduce((n, s) => n + (s.suggested || []).length, 0);
    const applied = sessions.reduce((n, s) => n + (s.applied || []).length, 0);
    const looking = mine && S.pane === 'ai' && _managerOpen();
    if (!looking && d.status === 'done' && (waiting || applied)) {
      const where = sessions.length === 1 && sessions[0].session_id !== current ? ' in another meeting' : '';
      toast(`Speakers updated${where}: ${applied} change${applied === 1 ? '' : 's'} applied` +
        (waiting ? `, ${waiting} need${waiting === 1 ? 's' : ''} a look.` : '.'), waiting ? 'warn' : 'success', 'spai-done');
    } else if (d.status === 'failed' && mine) {
      toast(`Speaker detection failed: ${d.error || 'unknown error'}`, 'error', 'spai-done');
    }
    if (mine || (current && (d.sessions || []).includes(current))) load(current || S.sid);
  }

  function onSpeakersUpdated(d) {
    if (!d || d.session_id !== curSid()) return;
    // The lines that moved repaint where they are; nothing reloads the
    // meeting, which would close this dialog. Never during a recording.
    if (!recording() && typeof window.onSpeakersChangedElsewhere === 'function') {
      window.onSpeakersChangedElsewhere(d.session_id, d.segments || []);
    }
    if (enabled() && _managerOpen() && S.pane === 'ai') loadSoon(d.session_id);
    else if (enabled()) badgeSoon(d.session_id);
  }

  let badgeTimer = 0;
  function badgeSoon(sid) {
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(() => refreshBadge(sid), 400);
  }

  function _managerOpen() {
    const o = $('speaker-manager-overlay');
    return !!o && !o.classList.contains('hidden');
  }

  // ── Settings > Speakers ───────────────────────────────────────────────────

  function syncSettings() {
    const p = prefs();
    const set = (id, v) => { const el = $(id); if (!el) return; if (el.type === 'checkbox') el.checked = !!v; else el.value = v; };
    set('spai-enabled', p.speaker_ai_enabled);
    set('spai-after', p.speaker_ai_after_meeting !== false);
    set('spai-autonomy', p.speaker_ai_autonomy || 'apply_confident');
    set('spai-library', p.speaker_ai_library_writes || 'follow_autonomy');
    set('spai-respect', p.speaker_ai_respect_user_labels !== false);
    set('spai-depth', p.speaker_ai_depth || 'standard');
    set('spai-model', p.speaker_ai_model || '');
    set('spai-strong-model', p.speaker_ai_strong_model || '');
    const dep = $('spai-settings-deps');
    if (dep) dep.classList.toggle('is-off', !p.speaker_ai_enabled);
    // The composer's menus show the same values.
    if (!S.running) { const opts = $('spai-options'); if (opts) opts.innerHTML = optionsHtml(); }
  }

  function setEnabled(on) {
    if (typeof savePref === 'function') savePref('speaker_ai_enabled', !!on);
    syncSettings();
    const sid = curSid();
    if (sid) {
      badge(sid);
      if (S.pane === 'ai') { if (on) load(sid); else render(); }
    }
    // The dialog's tabs appear and go with the feature.
    const tabs = $('speaker-modal-modes');
    if (tabs) tabs.hidden = !enabled();
  }

  function openSettingsPanel() {
    // app.js's openSettings(section) opens the dialog on Settings > Speakers.
    if (typeof openSettings === 'function') openSettings('speakers');
  }

  // ── Session lifecycle ─────────────────────────────────────────────────────

  async function refreshBadge(sid) {
    try {
      const r = await fetch(`/api/sessions/${encodeURIComponent(sid)}/speakers/insights`);
      if (!r.ok) return;
      const data = await r.json();
      if (S.sid !== sid) return;
      S.insights = data;
      badge(sid);
    } catch (_) { /* the dot is a convenience */ }
  }

  async function onSessionLoaded(sid) {
    S.open = {};
    S.people = {};
    S.naming = null;
    if (S.sid !== sid) {
      S.insights = null; S.running = null; S.frames = new Map(); S.framesRun = null; S.framesShown = false;
    }
    S.sid = sid;
    const btn = $('speakers-btn');
    if (btn) btn.classList.remove('has-ai-suggestions');
    if (!enabled()) return;
    if (_managerOpen() && S.pane === 'ai') return load(sid);
    // Just the dot: a cheap read.
    refreshBadge(sid);
  }

  window.SpeakerAI = {
    load, render, identify, paneFor, showPane, onRunStart, onRunProgress, onRunFrames, onRunDone,
    onSpeakersUpdated, onSessionLoaded, syncSettings, setEnabled,
    openSettings: openSettingsPanel, enabled,
  };

  // Enter starts a run; Shift+Enter is a new line.
  document.addEventListener('keydown', e => {
    if (e.key === 'Enter' && e.target && e.target.id === 'spai-input' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      if (!S.running) identify();
    }
  });
})();
