"""The Home view (brief section 3.5): This week, Needs attention, Next, People,
Activity, plus the first-run and loading states.

Source-and-render checks only, in the shape of tests/test_navigation_and_dashboard.py:
nothing here starts the server, opens the database, or touches the network.
"""
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest

ROOT = Path(__file__).parents[1]
TEMPLATES = ROOT / "ui_web/templates"
STATIC = ROOT / "ui_web/static"


def _read(path):
    return path.read_text(encoding="utf-8")


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    return node


@pytest.fixture(scope="module")
def home_html():
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)
    return env.get_template("index.html").render(initial_view="home")


@pytest.fixture(scope="module")
def home_js():
    return _read(STATIC / "home.js")


@pytest.fixture(scope="module")
def home_css():
    return _read(STATIC / "home.css")


# ── The partial carries every section root and its accessible list ───────────

SECTION_ROOTS = [
    "dash", "dash-firstrun", "dash-grid",
    "dash-overview", "dash-overview-metrics", "dash-overview-heat",
    "dash-overview-desc",
    "dash-attention", "dash-attention-list", "dash-attention-all",
    "dash-next", "dash-next-body",
    "home-speakers-list", "home-activity-chart", "home-activity-desc",
]


@pytest.mark.parametrize("element_id", SECTION_ROOTS)
def test_the_partial_has_every_section_root(home_html, element_id):
    assert home_html.count(f'id="{element_id}"') == 1, element_id


def test_the_focusable_heading_survives(home_html):
    assert 'id="view-home-heading"' in home_html


def test_the_overview_and_activity_expose_accessible_text(home_html):
    # The heatmap carries a visually hidden text summary, and so does the
    # histogram (review finding 21).
    assert 'class="visually-hidden" id="dash-overview-desc"' in home_html
    assert 'id="home-activity-desc"' in home_html


def test_the_home_stylesheet_is_linked_once_after_the_shell(home_html):
    assert home_html.count('href="/static/home.css"') == 1
    assert home_html.index("/static/style.css") < home_html.index("/static/home.css")


def test_no_kpi_tiles_or_stat_cards_return(home_html):
    partial = _read(TEMPLATES / "_view_home.html")
    for banned in ("dash-figures", "stat-sessions", "stat-week", "stat-attention",
                   "home-recent-list", "kpi", "hero-number"):
        assert banned not in partial, banned


# ── home.js writes only to ids that exist ────────────────────────────────────

def test_home_js_dashboard_targets_all_exist(home_html, home_js):
    targets = set(re.findall(
        r"getElementById\('((?:dash-|home-activity|home-speakers)[^']*)'\)", home_js))
    assert targets, "expected dashboard getElementById targets"
    for element_id in targets:
        assert home_html.count(f'id="{element_id}"') >= 1, element_id


# ── The view renders from the store, never straight from the network ─────────

def test_home_reads_slices_through_appdata(home_js):
    assert "AppData.get('analytics')" in home_js
    assert "AppData.get('sessions')" in home_js
    assert "AppData.get('calendarStatus')" in home_js
    assert "AppData.get('calendarEvents'" in home_js


def test_home_never_fetches_the_cached_slice_endpoints(home_js):
    for bad in ("fetch('/api/sessions')", "fetch('/api/analytics')",
                "fetch('/api/dashboard')", "fetch('/api/attention",
                "fetch('/api/calendar/events", "fetch('/api/calendar/status"):
        assert bad not in home_js, bad


def test_the_next_agenda_loads_its_range_from_the_store(home_js):
    assert "AppData.load('calendarEvents', { key: _homeWeekRange().rangeKey })" in home_js
    assert "calendarRangeKey(" in home_js
    assert "function _renderNext(" in home_js
    # Next reads today and the next two days from the same one loaded range.
    assert "[0, 1, 2].map(" in home_js


def test_the_overview_is_derived_from_the_sessions_slice(home_js):
    # The Overview stats and heatmap read the sessions we already hold, never a
    # fresh fetch, and render the weekday x hour grid.
    assert "function _renderOverview(" in home_js
    assert "ov-heat-grid" in home_js
    assert "ov-metric" in home_js


# ── E. Activity: an inline SVG histogram of recorded seconds ─────────────────

def test_activity_is_an_inline_svg_of_seconds(home_js):
    assert '<svg class="act-svg"' in home_js
    assert "(a.seconds || 0) / 60" in home_js
    # Accent at a mix, not a raw colour, is enforced in the stylesheet test.


# ── home.css: tokens only, balanced braces, no dashes ────────────────────────

def test_home_css_uses_tokens_only(home_css):
    stripped = re.sub(r"/\*.*?\*/", "", home_css, flags=re.S)
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", stripped)
    assert not hexes, hexes
    assert "rgba(" not in stripped
    assert "rgb(" not in stripped
    for token in ("--accent", "--fg", "--fg-muted", "--surface2", "--border",
                  "--focus-ring", "--radius-sm"):
        assert f"var({token})" in stripped, token


def test_home_css_braces_balance(home_css):
    stripped = re.sub(r"/\*.*?\*/", "", home_css, flags=re.S)
    assert stripped.count("{") == stripped.count("}"), \
        f"unbalanced braces: {stripped.count('{')} open, {stripped.count('}')} close"


def test_no_em_or_en_dashes_in_the_home_files():
    en_dash, em_dash = chr(0x2013), chr(0x2014)
    for path in (TEMPLATES / "_view_home.html", STATIC / "home.js",
                 STATIC / "home.css"):
        text = _read(path)
        assert en_dash not in text, f"en dash in {path.name}"
        assert em_dash not in text, f"em dash in {path.name}"
