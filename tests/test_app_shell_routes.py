"""Every page route renders the one shell with the right initial view.

Source-level only: nothing here imports app.py or starts the server (importing
it would load the transcription model). The five routes are the contract the
client router boots from, so a route that renders a different template, or
forgets its initial_view, breaks the shell before any JS runs.
"""
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")

ROUTES = {
    "/": "home",
    "/session": "session",
    "/calendar": "calendar",
    "/attention": "attention",
    "/speakers": "speakers",
}


def test_every_page_route_renders_the_shell():
    for route, view in ROUTES.items():
        assert f'@app.route("{route}")' in APP_PY, route
        assert f'render_template("index.html", initial_view="{view}")' in APP_PY, view


def test_no_page_route_renders_a_retired_template():
    for name in ("home.html", "calendar.html"):
        assert f'render_template("{name}"' not in APP_PY, name


def test_each_initial_view_is_rendered_by_exactly_one_route():
    for view in ROUTES.values():
        occurrences = APP_PY.count(f'initial_view="{view}"')
        assert occurrences == 1, f"initial_view={view} rendered by {occurrences} routes"


def test_the_speakers_route_is_a_view_not_a_modal():
    body = APP_PY[APP_PY.index('@app.route("/speakers")'):]
    body = body[:body.index("@app.route", 10)]
    assert "def speakers_view():" in body
    assert 'initial_view="speakers"' in body


def test_a_bare_session_still_lands_on_the_dashboard_unless_recording():
    body = APP_PY[APP_PY.index('@app.route("/session")'):]
    body = body[:body.index("@app.route", 10)]
    assert "if not request.args:" in body
    assert 'redirect("/")' in body
    assert 'if not recording:' in body


def test_the_settings_deep_link_keeps_its_section():
    assert 'redirect(f"/session?settings=1&section={quote(section)}")' in APP_PY


def test_the_dashboard_and_calendar_event_endpoints_are_mounted():
    assert "app.register_blueprint(dashboard_api.bp)" in APP_PY
    assert "app.register_blueprint(calendar_events_api.bp)" in APP_PY


def test_no_em_or_en_dashes_in_the_routes():
    routes = re.findall(r'@app\.route\("/[a-z]*"\)\n(?:.*\n)*?    return render_template[^\n]*\n', APP_PY)
    joined = "".join(routes)
    assert "\u2013" not in joined
    assert "\u2014" not in joined
