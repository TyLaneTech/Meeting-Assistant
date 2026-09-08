"""The post-meeting chapter pass: one authoritative rebuild when a stop lands.

A live auto-run is handed the chapters already placed and asked to keep them,
deliberately, so nothing renames a chapter under the user mid-meeting. The cost
is that the opening chapters were chosen when the meeting was a minute old and
nothing ever revisits them. This pass is what fixes that, and it is opt-in
(default on) because it spends a model call on every recording.

Source assertions plus the settings default, which is the one part that can be
imported without pulling in the ML stack.
"""
from pathlib import Path

from core import settings

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ui_web/templates/index.html").read_text(encoding="utf-8")
STYLE = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")


def _fn(src: str, start: str, end: str) -> str:
    body = src[src.index(start):]
    return body[:body.index(end)]


# ── The setting ──────────────────────────────────────────────────────────────

def test_it_is_on_by_default():
    assert settings.DEFAULTS["chapters_regen_after_meeting"] is True


def test_the_agent_api_documents_it():
    helpers = (ROOT / "agent_api/helpers.py").read_text(encoding="utf-8")
    assert "chapters_regen_after_meeting" in helpers
    # Not a key an agent is barred from writing.
    assert "chapters_regen_after_meeting" not in helpers[
        helpers.index("SETTINGS_WRITE_DENYLIST"):]


# ── The pass ─────────────────────────────────────────────────────────────────

def test_the_pass_is_opt_in_and_reads_the_setting_at_run_time():
    """Read when it runs, not when the recording started, so a toggle during a
    meeting still decides what happens at the end of that meeting."""
    body = _fn(APP_PY, "def _final_chapters_pass(", "def _defer_summary_during_recording(")
    assert 'if not settings.get("chapters_regen_after_meeting", True):' in body
    assert "return" in body


def test_the_pass_is_a_full_rebuild_with_current_settings():
    """is_auto=False is the manual path: no existing chapters handed to the
    model, and the result is authoritative."""
    body = _fn(APP_PY, "def _final_chapters_pass(", "def _defer_summary_during_recording(")
    assert "_run_chapters(session_id, transcript, seg_times, meta, is_auto=False)" in body


def test_the_pass_never_raises_into_the_stop_tail():
    body = _fn(APP_PY, "def _final_chapters_pass(", "def _defer_summary_during_recording(")
    assert "except Exception as e:" in body
    assert 'log.warn("chapters"' in body


def test_it_runs_last_in_the_stop_tail():
    """It is the only thing in the tail that waits on a model with no deadline.
    Anything after it would be held up by a slow or hanging provider."""
    tail = _fn(APP_PY, "# ── Deferred tail:", "        except Exception:")
    assert "_final_chapters_pass(sid)" in tail
    assert tail.index("obsidian.export_session(sid)") < tail.index("_final_chapters_pass(sid)")
    assert tail.index("ai.generate_title(") < tail.index("_final_chapters_pass(sid)")
    # And the tail runs on the stop's own thread, after the gate that lets a
    # new recording start, so this never delays pressing Record again.
    assert "threading.Thread(target=_cleanup, daemon=True).start()" in APP_PY


def test_the_pass_and_a_hand_pressed_regenerate_see_the_same_input():
    """One builder for both, or the button and the automatic run drift apart."""
    assert APP_PY.count("def _chapters_args_from_storage(") == 1
    helper = _fn(APP_PY, "def _chapters_args_from_storage(", "def _final_chapters_pass(")
    assert "_build_transcript(sess[\"segments\"], labels)" in helper
    assert "_segment_times(sess[\"segments\"])" in helper
    assert "_build_session_meta(" in helper
    # A blank transcript is the helper's own "nothing to do" answer, so both
    # callers get it the same way.
    assert "if not transcript.strip():" in helper
    route = _fn(APP_PY, "def api_generate_chapters(", "@app.route(\"/api/sessions/<sid>/chapters\"")
    assert "_chapters_args_from_storage(session_id)" in route


def test_a_missing_session_is_still_a_404_not_an_empty_transcript():
    route = _fn(APP_PY, "def api_generate_chapters(", "@app.route(\"/api/sessions/<sid>/chapters\"")
    assert 'if not storage.get_session_times(session_id):' in route
    assert '"Session not found"' in route


# ── The checkbox ─────────────────────────────────────────────────────────────

def test_the_checkbox_sits_under_the_description_and_above_the_tabs():
    header = _fn(INDEX, 'class="chapters-header"', 'class="chapters-body"')
    subtitle = header.index('class="chapters-subtitle"')
    toggle = header.index('id="chapters-regen-after-toggle"')
    tabs = header.index('class="chapters-tabs"')
    assert subtitle < toggle < tabs, "the toggle governs both tabs, so it sits above them"
    assert "Regenerate chapters after meeting" in header
    assert 'onchange="_onChaptersRegenAfterToggle(this.checked)"' in header
    # It says what it will do, since the effect is not visible until later.
    assert 'title="When a recording stops' in header
    assert ".chapters-header-toggle" in STYLE


def test_the_dialog_loads_the_checkbox_with_default_on():
    """An absent key means on: settings.py holds the default and a client that
    has never seen the key has to agree with it."""
    body = _fn(APP_JS, "function openChaptersManager(", "function closeChaptersManager(")
    assert "_prefs.chapters_regen_after_meeting !== false" in body
    # Loaded with the dialog, not with either tab, because it belongs to both.
    assert "function loadChaptersTuning(" not in body.split("_chaptersSwitchTab")[0]


def test_toggling_it_saves_only_that_key():
    body = _fn(APP_JS, "function _onChaptersRegenAfterToggle(", "\n}")
    assert "savePref('chapters_regen_after_meeting', !!checked);" in body
