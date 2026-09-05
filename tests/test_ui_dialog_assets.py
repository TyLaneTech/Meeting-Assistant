import re
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_dialog_api_is_exposed_and_native_calls_are_retired():
    dialog_js = (ROOT / "ui_web/static/ui-dialog.js").read_text(encoding="utf-8")
    app_js = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")

    for helper in ("uiToast", "uiConfirm", "uiAlert", "uiPrompt"):
        assert f"window.{helper}" in dialog_js
    assert not re.search(r"\b(?:window\.)?(?:alert|confirm|prompt)\(", app_js)


def test_dialog_script_precedes_page_scripts():
    """One shell template now, and every view script loads after the helpers
    it calls: ui-dialog, then app.js, then the per-view renderers."""
    html = (ROOT / "ui_web/templates/index.html").read_text(encoding="utf-8")
    dialog_pos = html.index('/static/ui-dialog.js')
    assert dialog_pos < html.index('/static/app.js')
    for view_script in ('/static/home.js', '/static/calendar.js', '/static/attention.js'):
        assert html.index('/static/app.js') < html.index(view_script), view_script


def test_dialog_styles_use_theme_tokens():
    css = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")
    section = css[css.index("/* Shared dialogs and toasts */"):]
    for token in ("--surface", "--border", "--fg", "--fg-muted", "--accent",
                  "--red", "--yellow", "--green", "--radius", "--shadow-lg",
                  "--font-ui"):
        assert f"var({token})" in section
