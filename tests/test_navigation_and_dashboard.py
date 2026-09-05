"""The app shell: one template, five views, one store, one navigation.

These are source-and-render checks only. Nothing here starts the server, opens
the database, or touches the network. They pin the structure the overhaul
depends on (context/ui-overhaul-2026-09.md sections 3.1 to 3.4).
"""
import json
import re
from pathlib import Path

import jinja2
import pytest


ROOT = Path(__file__).parents[1]
TEMPLATES = ROOT / "ui_web/templates"
STATIC = ROOT / "ui_web/static"

VIEWS = ["home", "calendar", "attention", "speakers", "session"]


@pytest.fixture(scope="module")
def env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)),
        autoescape=True,
    )


@pytest.fixture(scope="module")
def rendered(env):
    """index.html rendered once per route the server can serve."""
    return {view: env.get_template("index.html").render(initial_view=view)
            for view in VIEWS}


def _read(path):
    return path.read_text(encoding="utf-8")


# ── One template renders every route ─────────────────────────────────────────

def test_the_per_page_templates_are_gone():
    """/, /calendar and /session were three server pages that each reloaded
    everything. There is one shell now."""
    for name in ("home.html", "calendar.html", "_nav.html", "_voice_library.html"):
        assert not (TEMPLATES / name).exists(), f"{name} still on disk"


def test_the_shell_declares_the_rendered_view():
    html = _read(TEMPLATES / "index.html")
    assert '<body data-view="{{ initial_view }}">' in html
    assert 'window.MA_INITIAL_VIEW = "{{ initial_view }}";' in html
    # The router needs the view before app.js runs.
    assert html.index("MA_INITIAL_VIEW") < html.index("/static/app.js")


def test_every_view_root_is_a_sibling_of_the_others(rendered):
    for view in VIEWS:
        html = rendered[view]
        for name in VIEWS:
            assert f'data-view="{name}" id="view-{name}"' in html, (view, name)


def test_exactly_one_view_is_active_per_route(rendered):
    for view in VIEWS:
        html = rendered[view]
        # The server pre-activates exactly the requested view so a script error
        # before boot cannot leave a blank page; the router still owns is-active
        # after boot and reconciles it on every navigation.
        assert html.count("is-active") == 1, (
            f"{view}: the server marks exactly one view active")
        assert f'class="view is-active" data-view="{view}"' in html, (
            f"{view}: the active section is the requested one")
    # The router still owns is-active after boot.
    js = _read(STATIC / "app.js")
    assert "el.classList.toggle('is-active', view === name)" in js


def test_each_view_has_a_focusable_heading(rendered):
    for view in VIEWS:
        assert f'id="view-{view}-heading"' in rendered["home"], view
    assert rendered["home"].count('class="view-heading') == len(VIEWS)


# ── Element ids app.js drives without null guards ────────────────────────────

SHELL_IDS = [
    # navigation and the recordings rail
    "sidebar", "sidebar-resize-handle", "session-list", "sidebar-search-input",
    "sidebar-filter-btn", "sidebar-filter-popover", "sidebar-bulk-bar",
    "attention-control", "attention-count", "recordings-menu", "app-menu",
    # the header
    "view-title", "view-subtitle", "topbar-session-title", "record-btn",
    "record-menu", "refresh-btn", "ask-toggle", "layout-control", "header-search",
    "home-search-input", "home-search-clear", "home-search-results",
    "pane-toggle-transcript", "pane-toggle-summary", "pane-toggle-chat",
    "pane-toggle-notes",
    # capture
    "capture-strip", "capture-title", "capture-time", "capture-meter-desktop",
    "capture-meter-mic", "capture-warning", "capture-stop-btn",
    "status-pill", "status-dot", "status-text", "recording-duration",
    "capture-setup-panes", "model-config", "audio-viz-pane",
    "screen-capture-section", "pane-body-audio", "pane-arrow-models",
    "brand-viz-canvas", "upload-audio-btn", "upload-audio-input",
    # the ask rail and the views
    "ask-rail", "global-chat-input", "global-chat-messages", "global-send-btn",
    "home-conv-list", "dash", "dash-attention-list", "cal-grid", "attn-list",
    "fingerprint-profile-list", "fp-tab-profiles",
]


@pytest.mark.parametrize("element_id", SHELL_IDS)
def test_shell_ids_are_present_exactly_once(rendered, element_id):
    for view in VIEWS:
        assert rendered[view].count(f'id="{element_id}"') == 1, \
            f"{element_id} is not present exactly once on /{view}"


def test_no_element_id_is_duplicated_anywhere(rendered):
    ids = re.findall(r'\bid="([^"]+)"', rendered["home"])
    duplicates = {i for i in ids if ids.count(i) > 1}
    assert not duplicates, f"duplicate ids: {sorted(duplicates)}"


# ── The header owns actions; the sidebar owns navigation ─────────────────────

def test_the_header_has_no_power_button(rendered):
    """App lifecycle is not a page action. It lives in the sidebar App menu."""
    for view in VIEWS:
        html = rendered[view]
        assert 'id="power-menu"' not in html, view
        assert "togglePowerMenu()" not in html, view
    header = _read(TEMPLATES / "_header.html")
    assert "fa-power-off" not in header
    assert "power-menu" not in header


def test_app_lifecycle_lives_in_the_sidebar_app_menu(rendered):
    html = rendered["home"]
    assert 'id="app-menu-btn"' in html
    for label in ("Check for updates", "What's new",
                  "Restart Meeting Assistant", "Quit Meeting Assistant"):
        assert label in html, label
    # The update-available state survives under its new id.
    js = _read(STATIC / "app.js")
    assert "getElementById('app-update-item')" in js
    assert "getElementById('app-menu-dot')" in js
    assert "topbar-update-btn" not in js
    assert "power-menu" not in js


def test_the_primary_nav_is_four_routed_links(rendered):
    html = rendered["home"]
    for href in ("/", "/calendar", "/attention", "/speakers"):
        assert f'data-nav href="{href}"' in html, href
    # Needs attention keeps its id and carries the word, not only the count.
    assert 'href="/attention" id="attention-control"' in html
    assert "Needs attention" in html


def test_the_sidebar_says_recordings_not_sessions(rendered):
    html = rendered["home"]
    assert ">Recordings</h2>" in html
    assert 'placeholder="Filter recordings' in html
    assert ">Sessions<" not in html


def test_the_recordings_overflow_holds_the_retired_rail_buttons(rendered):
    html = rendered["home"]
    for label in ("New workspace", "New folder", "Select recordings",
                  "Import audio or video", "Import meeting package"):
        assert label in html, label
    assert "Ctrl+N" in html


def test_the_record_button_exists_once_and_lives_in_the_header(rendered):
    header = _read(TEMPLATES / "_header.html")
    assert 'id="record-btn" class="btn btn-record"' in header
    for view in VIEWS:
        assert rendered[view].count('id="record-btn"') == 1, view


def test_record_button_states_are_the_three_the_brief_names():
    js = _read(STATIC / "app.js")
    body = js[js.index("function updateRecordBtn()"):]
    body = body[:body.index("function updateTestBtn()")]
    assert "Preparing recorder" in body
    assert "Stop · " in body
    assert "</span> Record'" in body
    # The dashboard/calendar nav entry is gone, so its branch must be too.
    assert "app-nav-record" not in js
    # Record never resumes by accident.
    start = js[js.index("async function startNewRecording()"):]
    start = start[:start.index("/** The chevron")]
    assert "await newSession();" in start
    resume = js[js.index("async function resumeRecording()"):]
    resume = resume[:resume.index("/* ── App menu")]
    assert "resume: true" in resume


def test_the_layout_control_keeps_the_pane_toggle_ids():
    header = _read(TEMPLATES / "_header.html")
    for idx, name in enumerate(("transcript", "summary", "chat", "notes")):
        assert f'id="pane-toggle-{name}" onclick="togglePane({idx})"' in header, name


def test_the_column_toggles_are_buttons_not_a_menu():
    """Four always-visible toggles, one per column, instead of a dropdown."""
    header = _read(TEMPLATES / "_header.html")
    assert 'id="layout-menu"' not in header
    assert 'id="layout-btn"' not in header
    group = header[header.index('id="layout-control"'):header.index('id="header-search"')]
    assert group.count('class="pane-toggle-btn') == 4
    assert group.count("aria-pressed=") == 4
    css = _read(STATIC / "style.css")
    assert ".pane-toggle-btn" in css
    js = _read(STATIC / "app.js")
    assert "btn.setAttribute('aria-pressed'" in js
    assert "menu-item-label" not in js[js.index("function _syncToggleButtons"):js.index("function togglePane(")]


# ── The router ───────────────────────────────────────────────────────────────

def test_the_router_and_the_store_are_defined_in_app_js():
    js = _read(STATIC / "app.js")
    assert "const Views = {" in js
    assert "function navigateTo(url, opts)" in js
    assert "const AppData = {" in js
    for hook in ("register(", "show(name, opts)", "current"):
        assert hook in js, hook
    for api in ("get(name, key)", "load(name, opts)", "invalidate(names, reason)",
                "patch(name, fn, key)", "subscribe(names, fn)", "unsubscribe(fn)",
                "lastUpdated(name, key)", "refreshActiveView()"):
        assert api in js, api


def test_every_slice_the_brief_names_exists():
    js = _read(STATIC / "app.js")
    for slice_name in ("sessions", "folders", "analytics", "attention",
                       "calendarStatus", "calendarEvents"):
        assert f"{slice_name}:" in js, slice_name
    for endpoint in ("/api/sessions", "/api/folders", "/api/dashboard",
                     "/api/attention/summary", "/api/calendar/status"):
        assert endpoint in js, endpoint
    assert "/api/calendar/events?start=" in js


def test_a_late_response_can_never_commit_over_a_newer_one():
    js = _read(STATIC / "app.js")
    load = js[js.index("  load(name, opts) {"):js.index("  /** Mark slices out of date.")]
    assert "const token = ++s.token;" in load
    assert "if (token !== s.token) return this.get(name, o.key);" in load
    # A failure keeps the last good data instead of rendering zeros.
    assert "s.status = 'error';" in load
    assert "s.lastGood = payload;" in load


def test_shared_reads_go_through_the_store():
    """A direct fetch of a cached resource is how "switching views reloads
    everything" comes back."""
    js = _read(STATIC / "app.js")
    for name in ("app.js", "home.js", "calendar.js", "attention.js"):
        source = _read(STATIC / name)
        if name == "app.js":
            # The store itself builds the URL from _SLICE_ENDPOINTS.
            source = source.replace("_SLICE_ENDPOINTS = {", "STORE_ENDPOINTS = {")
            body = source[source.index("STORE_ENDPOINTS = {"):]
            body = body[:body.index("}")]
            assert "/api/sessions" in body
            source = source.replace(body, "")
        assert "fetch('/api/sessions')" not in source, name
        assert "fetch('/api/folders')" not in source, name
        assert "fetch('/api/analytics')" not in source, name
    assert "async function refreshSidebar()" in js
    assert "AppData.invalidate(['sessions', 'folders'], 'sidebar')" in js


def test_the_page_flag_that_split_the_app_in_two_is_gone():
    for name in ("app.js", "home.js", "calendar.js", "attention.js"):
        assert "_isHomePage" not in _read(STATIC / name), name
    for template in TEMPLATES.glob("*.html"):
        assert "_isHomePage" not in _read(template), template.name


def test_navigation_never_reloads_the_page():
    """window.location.href on a route is a page load, which is the whole
    problem the shell exists to fix."""
    js = _read(STATIC / "app.js")
    assert "window.location.href = '/session" not in js
    assert "window.location.href = `/session" not in js


def test_only_unmodified_primary_clicks_are_intercepted():
    js = _read(STATIC / "app.js")
    body = js[js.index("function _initRouteLinks()"):]
    body = body[:body.index("/* ── Header:")]
    assert "e.button !== 0" in body
    assert "e.metaKey || e.ctrlKey || e.shiftKey || e.altKey" in body
    assert "a.target" in body and "data-external" in body.replace("dataset.external", "data-external")
    assert "window.addEventListener('popstate'" in body


def test_query_actions_are_consumed_once():
    js = _read(STATIC / "app.js")
    body = js[js.index("function _applyRouteQuery("):]
    body = body[:body.index("/** Intercept only unmodified")]
    for param in ("attention", "settings", "fingerprint", "autostart",
                  "speakers", "quiet_prompt", "workspace"):
        assert f"'{param}'" in body, param
    # _consumeParams keeps every parameter it was not asked to drop.
    consume = js[js.index("function _consumeParams("):]
    consume = consume[:consume.index("function _applyRouteQuery(")]
    assert "new URLSearchParams(location.search)" in consume
    assert "next.delete(k)" in consume


def test_the_recording_state_machine_is_implemented():
    js = _read(STATIC / "app.js")
    # Opening another recording while live asks once, and names the recording.
    load = js[js.index("async function loadSession(sessionId)"):]
    load = load[:load.index("const gen = ++_loadGeneration;")]
    assert "Stop the current recording and open ${label}?" in load
    # Opening the live session returns to its workspace instead of reloading it.
    assert "if (sessionId === state.sessionId) {" in load
    # Stop from any view leaves the view alone and offers a way in.
    saved = js[js.index("function _announceRecordingSaved(sessionId)"):]
    saved = saved[:saved.index("// Auto-open the Cleanup tab")]
    assert "label: 'Open recording'" in saved
    assert "AppData.invalidate(['sessions', 'analytics', 'attention'], 'recording_stop')" in js
    # Popstate never clears a live session.
    query = js[js.index("function _applyRouteQuery("):]
    assert "if (o.popstate && !state.isRecording && state.sessionId) {" in query
    # Reload and reconnect reconcile against the server first.
    assert "fetch('/api/status').then(r => r.json()).then(st => {" in js
    assert "function _reconcileAfterGap(reason)" in js


def test_the_view_switch_is_an_opacity_crossfade_that_can_be_skipped():
    js = _read(STATIC / "app.js")
    show = js[js.index("  show(name, opts) {"):js.index("  _writeHistory(")]
    assert "prefers-reduced-motion: reduce" in show
    assert "if (!repeat && !o.popstate && !o.noFade && !reduce)" in show
    assert "opacity 90ms linear" in show
    assert "translateY" not in show
    assert "transform:" not in show


def test_the_document_title_names_the_view():
    js = _read(STATIC / "app.js")
    body = js[js.index("  applyTitle(name) {"):js.index("function _syncNavCurrent(")]
    for label in ("Home", "Calendar", "Needs attention", "Speakers"):
        assert f"'{label}'" in body or f": '{label}'" in body, label
    assert "· Meeting Assistant" in body


# ── The views render from the store ──────────────────────────────────────────

def test_each_view_registers_a_lifecycle():
    registrations = {
        "app.js": ["speakers", "session"],
        "home.js": ["home"],
        "calendar.js": ["calendar"],
        "attention.js": ["attention"],
    }
    for name, views in registrations.items():
        js = _read(STATIC / name)
        for view in views:
            assert f"Views.register('{view}'" in js, (name, view)


def test_the_dashboard_renders_from_slices_and_never_fetches_them():
    js = _read(STATIC / "home.js")
    body = js[js.index("function loadAnalytics()"):js.index("function _dashDerivedActivity()")]
    assert "AppData.get('analytics')" in body
    assert "AppData.get('sessions')" in body
    assert "fetch(" not in body
    # A failed aggregate must not read as "you have nothing".
    assert "if (analytics) empty = (Number(data.total_sessions) || 0) === 0;" in body
    assert "AppData.status('sessions') === 'ready'" in body


def test_the_dashboard_keeps_focus_and_scroll_on_re_render():
    js = _read(STATIC / "home.js")
    assert "morphdom(el, next, { childrenOnly: true })" in js
    assert "_dashMorph(list, html)" in js


def test_the_banned_dashboard_furniture_is_gone():
    """No KPI tiles, and no Recent meetings list duplicating the rail."""
    html = _read(TEMPLATES / "_view_home.html")
    for element_id in ("dash-figures", "dash-figures-note", "stat-sessions",
                       "stat-time", "stat-speakers", "stat-week", "stat-attention",
                       "dash-recent-panel", "home-recent-list"):
        assert element_id not in html, element_id
    js = _read(STATIC / "home.js")
    for fn in ("_renderFigures", "_renderRecentSessions", "_formatCompactNumber"):
        assert fn not in js, fn
    css = _read(STATIC / "style.css")
    for selector in (".dash-figure", ".home-recent-", ".home-widget", ".home-hero"):
        assert selector not in css, selector


def test_the_library_summary_is_a_sentence_in_the_header():
    js = _read(STATIC / "home.js")
    body = js[js.index("function _dashSubtitle(analytics)"):]
    body = body[:body.index("/** Render Home from")]
    assert "meeting${total === 1 ? '' : 's'}" in body
    assert "recorded" in body
    assert "this week." in body
    assert "Views.setTitle('home'" in js


def test_the_attention_queue_and_the_badge_share_one_source():
    js = _read(STATIC / "attention.js")
    assert "AppData.get('sessions')" in js
    assert "s.attention && s.attention.needs" in js
    assert "Views.setTitle('attention'" in js
    assert "speakers=cleanup" in js
    app = _read(STATIC / "app.js")
    assert "function attentionCount()" in app
    assert "AppData.get('attention')" in app


def test_the_calendar_reads_its_range_from_the_store():
    js = _read(STATIC / "calendar.js")
    assert "AppData.load('calendarEvents', { key: range })" in js
    assert "calendarRangeKey(" in js
    assert "new EventSource" not in js
    # The external sync is named differently from the header's Refresh.
    assert "Sync calendar" in js
    assert "'/api/calendar/refresh'" in js


def test_the_calendar_still_converts_naive_utc_the_way_the_sidebar_does():
    js = _read(STATIC / "calendar.js")
    assert "new Date(session.started_at + 'Z')" in js
    assert "new Date(session.ended_at + 'Z')" in js


def test_the_calendar_binds_month_paging_and_escape():
    js = _read(STATIC / "calendar.js")
    assert "'ArrowLeft'" in js and "'ArrowRight'" in js
    assert "'Escape'" in js
    assert "_calShiftMonth" in js
    # Its keyboard handler is inert while another view is on screen.
    assert "if (Views.current !== 'calendar') return;" in js


def test_the_calendar_day_is_addressable():
    js = _read(STATIC / "calendar.js")
    assert "function _calApplyRoute(month, day)" in js
    assert "params.set('month', month)" in js
    assert "params.set('day', day)" in js


def test_the_calendar_grid_does_not_claim_a_role_it_does_not_implement():
    html = _read(TEMPLATES / "_view_calendar.html")
    assert 'role="grid"' not in html
    js = _read(STATIC / "calendar.js")
    assert 'role="gridcell"' not in js
    assert 'aria-label="${escapeHtml(label)}"' in js


def test_the_speakers_view_is_the_voice_library_lifted_out_of_its_overlay():
    html = _read(TEMPLATES / "_view_speakers.html")
    assert 'class="overlay' not in html
    assert 'id="fingerprint-panel-overlay"' not in html
    for element_id in ("fp-tab-profiles", "fp-tab-match", "fp-tab-health",
                       "fp-search-input", "fp-profile-scroll"):
        assert f'id="{element_id}"' in html, element_id
    js = _read(STATIC / "app.js")
    assert "function openFingerprintPanel() {\n  navigateTo('/speakers');" in js
    assert "Views.register('speakers'" in js


def test_one_escaper_and_no_native_dialogs():
    """home.js used to shadow app.js's helpers; in one shell that would take
    over the workspace's own chat and transcript rendering."""
    app = _read(STATIC / "app.js")
    for name in ("home.js", "calendar.js", "attention.js"):
        js = _read(STATIC / name)
        assert "function escapeHtml(" not in js, name
        assert not re.search(r"\b(?:window\.)?(?:alert|confirm|prompt)\(", js), name
    escaper = app[app.index("function escapeHtml(s)"):][:400]
    assert "&quot;" in escaper
    assert "String(s == null ? '' : s)" in escaper


# ── Front door ───────────────────────────────────────────────────────────────

def test_manifest_starts_at_the_dashboard():
    manifest = json.loads(_read(STATIC / "manifest.webmanifest"))
    assert manifest["start_url"] == "/"
    assert {s["url"] for s in manifest["shortcuts"]} == {
        "/session?autostart=1", "/calendar", "/session?attention=needs"}


def test_launcher_opens_the_dashboard():
    vbs = _read(ROOT / "app_launcher.vbs")
    assert 'appUrl    = "http://localhost:6969/"' in vbs
    assert "/session" not in vbs


# ── Theming ──────────────────────────────────────────────────────────────────

SHELL_CSS_MARKER = "   The app shell\n"


def _shell_css():
    css = _read(STATIC / "style.css")
    return css[css.index(SHELL_CSS_MARKER):]


def test_the_semantic_ink_tokens_are_defined_for_dark_and_light():
    css = _read(STATIC / "style.css")
    dark = css[css.index(':root,\n:root[data-theme-mode="dark"] {'):]
    dark = dark[:dark.index("}")]
    light = css[css.index(':root[data-theme-mode="light"] {'):]
    light = light[:light.index("}")]
    for token in ("--on-accent", "--on-green", "--on-red", "--focus-ring"):
        assert token in dark, f"{token} missing from dark"
        assert token in light, f"{token} missing from light"
    # Every accent palette that redefines --accent redefines them too.
    blocks = re.findall(r"(?m)^(:root[^{]*)\{([^}]*)\}", css)
    for selector, body in blocks:
        if "--accent:" not in body:
            continue
        for token in ("--on-accent", "--on-green", "--on-red", "--focus-ring"):
            assert token in body, f"{token} missing from {selector.strip()}"


def test_the_shell_styles_use_tokens_only():
    shell = _shell_css()
    literals = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", shell))
    assert not literals, literals
    assert "rgba(" not in shell
    for token in ("--surface", "--surface2", "--border", "--fg", "--fg-muted",
                  "--accent", "--green", "--red", "--yellow", "--font-ui",
                  "--on-accent", "--on-green", "--on-red", "--focus-ring"):
        assert f"var({token})" in shell, token


def test_the_shell_never_dims_text_with_opacity():
    """Text opacity is banned (brief section 4): contrast comes from tokens.
    Opacity on a whole disabled control is the one allowed use."""
    shell = _shell_css()
    for match in re.finditer(r"opacity:\s*([\d.]+)", shell):
        block_start = shell.rfind("{", 0, match.start())
        selector = shell[shell.rfind("}", 0, block_start) + 1:block_start]
        allowed = ("disabled", "view", "@keyframes", "%")
        assert any(token in selector for token in allowed), \
            f"opacity outside a disabled, view or keyframe rule: {selector.strip()!r}"


def test_the_button_vocabulary_has_every_state():
    shell = _shell_css()
    for variant in (".btn-primary", ".btn-secondary", ".btn-quiet",
                    ".btn-record", ".btn-danger"):
        assert variant in shell, variant
    assert ".btn:focus-visible" in shell
    assert ".btn:disabled" in shell
    assert ".btn.is-loading" in shell
    for variant in (".btn-primary", ".btn-secondary", ".btn-quiet", ".btn-record"):
        assert f"{variant}:hover" in shell, f"{variant} has no hover state"


def test_menus_escape_their_container():
    shell = _shell_css()
    menu = shell[shell.index(".menu {"):shell.index(".menu-item {")]
    assert "position: fixed" in menu
    js = _read(STATIC / "app.js")
    assert "function closeMenu(opts)" in js
    assert "document.addEventListener('mousedown', _onMenuOutside, true)" in js
    assert "if (e.key === 'Escape')" in js
    assert "ArrowDown" in js and "ArrowUp" in js
    assert "closeMenu({ restoreFocus: true })" in js


def test_dead_rules_are_gone():
    """Markup these styled no longer exists."""
    css = _read(STATIC / "style.css")
    for selector in (".topbar", ".app-nav", ".power-menu", ".status-pill",
                     ".sidebar-header", ".home-nav-item", ".dash-panel",
                     ".dash-btn", ".home-layout",
                     ".home-chat-area", ".home-recent-indicator",
                     ".home-activity-day", ".upload-nav-btn", ".cal-month"):
        assert selector not in css, selector
    assert "_formatCompactNumber" not in _read(STATIC / "home.js")


def test_stylesheet_braces_balance():
    """A dropped closing brace swallows every rule after it."""
    css = re.sub(r"/\*.*?\*/", "", _read(STATIC / "style.css"), flags=re.S)
    assert css.count("{") == css.count("}"), \
        f"unbalanced braces: {css.count('{')} open, {css.count('}')} close"


def test_the_header_priority_rules_are_container_queries():
    """Breakpoints on the window ignore the resizable sidebar and the Ask rail
    (review finding 15)."""
    shell = _shell_css()
    assert "container: maincol / inline-size" in shell
    assert "@container maincol (max-width: 1249px)" in shell
    assert "@container maincol (max-width: 999px)" in shell
    assert "container: view / inline-size" in shell
    # Record is never one of the controls that collapse.
    for block in re.findall(r"@container maincol[^{]*\{(.*?)\n\}", shell, re.S):
        assert "record" not in block.lower(), block


def test_reduced_motion_covers_the_shell_animations():
    css = _read(STATIC / "style.css")
    blocks = re.findall(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)
    joined = "\n".join(blocks)
    for selector in (".capture-dot", ".record-pulse", ".status-dot.recording"):
        assert selector in joined, selector


def test_no_em_or_en_dashes_in_the_ui():
    for path in list(TEMPLATES.glob("*.html")) + list(STATIC.glob("*.js")):
        text = _read(path)
        assert "\u2013" not in text, f"en dash in {path.name}"
        assert "\u2014" not in text, f"em dash in {path.name}"
