"""The client's generic preference saver must send only what changed.

savePref() once sent the whole _prefs object. Every open page holds its own
copy, loaded when that page did, so the copy in an older tab overwrote any
change made elsewhere as soon as that tab saved anything: enabling the
calendar in Settings and then resizing the sidebar on another page switched
the calendar off again (2026-09-05).
"""
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
SETTINGS_HTML = (ROOT / "ui_web/templates/_settings.html").read_text(encoding="utf-8")


def test_save_pref_sends_only_the_changed_keys():
    start = APP_JS.index("function savePref(")
    block = APP_JS[start:APP_JS.index("window.addEventListener('pagehide'", start)]
    assert "_prefsPending[key] = value" in block
    assert "JSON.stringify(_prefsPending)" in block
    assert "JSON.stringify(_prefs)" not in block


def test_no_preference_writer_sends_the_whole_object():
    assert "JSON.stringify(_prefs)" not in APP_JS


def test_pending_changes_flush_when_the_tab_closes():
    assert "window.addEventListener('pagehide', () => _flushPrefs(true))" in APP_JS


def test_calendar_link_row_uses_the_key_layout():
    """The ICS link is a credential, so its row follows the API-key layout: the
    reveal button sits in the same input row as the field, and the buttons
    share one line beside it instead of stacking in a right-hand column."""
    row = SETTINGS_HTML[SETTINGS_HTML.index('class="settings-key-row calendar-link-row"'):]
    row = row[:row.index('id="calendar-test-result"')]
    input_row = row[row.index('class="key-input-row calendar-link-input"'):]
    input_row = input_row[:input_row.index('class="calendar-link-buttons"')]
    assert 'id="calendar-ics-url"' in input_row
    assert 'id="calendar-reveal-btn"' in input_row
    for element_id in ("calendar-save-btn", "calendar-test-btn", "calendar-forget-btn"):
        assert f'id="{element_id}"' in row
    assert "Resolve" not in SETTINGS_HTML
