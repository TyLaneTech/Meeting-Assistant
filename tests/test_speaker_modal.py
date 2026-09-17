"""Static assertions for the one-surface Speakers modal (Cleanup).

The modal used to be two tabs. Manage was retired once the only job Cleanup
could not already do, picking a speaker's colour, moved onto the colour square
in each group header, so there is one surface, one header, one commit model and
one playback helper. These are file-level checks on purpose: the modal is
vanilla JS in a 20k-line file, so a unit harness would cost more than it
catches.
"""
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ui_web/templates/index.html").read_text(encoding="utf-8")
COMBO_JS = (ROOT / "ui_web/static/ui-combobox.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")
SPEAKER_DB = (ROOT / "ml/speaker_db.py").read_text(encoding="utf-8")


def _modal_markup() -> str:
    start = INDEX.index('id="speaker-manager-overlay"')
    end = INDEX.index('id="fp-match-toast"')
    return INDEX[start:end]


# ── A. one surface, one header ──────────────────────────────────────────────

def test_header_states_the_meeting_and_the_speaker_stats():
    markup = _modal_markup()
    for element_id in ("speaker-manager-meeting", "speaker-manager-status"):
        assert f'id="{element_id}"' in markup


def test_the_tab_strip_and_the_manage_pane_are_gone():
    markup = _modal_markup()
    assert 'role="tablist"' not in markup
    assert 'role="tab"' not in markup
    assert 'role="tabpanel"' not in markup
    assert "speaker-manager-tabs" not in INDEX
    assert "data-tab-view" not in INDEX
    # The Manage pane's own markup, all of it.
    for gone in ("speaker-pane-manage", "speaker-manager-list", "speaker-color-grid\"",
                 "speaker-name-combo", "speaker-save-btn", "speaker-add-btn",
                 "speaker-unlink-btn", "speaker-editor-hint", "speaker-editor-unsaved",
                 "speaker-cleanup-badge", "speaker-cleanup-dirty"):
        assert gone not in INDEX, gone
    # The pane that is left is not a tab panel any more.
    assert 'class="speaker-cleanup-body" id="speaker-pane-cleanup">' in markup


def test_no_manage_only_javascript_survives():
    for retired in ("renderSpeakerManager", "switchSpeakerManagerTab",
                    "_speakerManagerInitialTab", "_speakerModalLastTab",
                    "_cleanupActiveTab", "applySpeakerEditor", "createSpeakerProfile",
                    "unlinkSelectedSpeakers", "linkSelectedSpeakersToProfile",
                    "_selectedSpeakerKeys", "_speakerDraftColor", "_mgrEnsureNameCombo",
                    "_mgrNameCombo", "playManageSpeakerVoice", "_cleanupUpdateBadge",
                    "_paintSpeakerTabBadges", "_pendingSpeakerProfiles"):
        assert retired not in APP_JS, retired


def test_no_manage_only_styles_survive():
    for retired in (".speaker-manager-tabs", ".speaker-manager-tab-badge",
                    ".speaker-manager-tab-dot", ".speaker-manager-legend",
                    ".speaker-editor", ".speaker-manager-list", ".speaker-row-select",
                    ".speaker-selected", ".fp-lib-btn"):
        assert retired not in CSS, retired
    # The Voice Library list still uses the shared swatch and palette classes.
    assert ".fp-profile-row .speaker-row-swatch" in CSS
    assert ".fp-detail-color-grid .speaker-color-btn" in CSS


def test_status_line_uses_the_shared_attention_definition():
    # Same product definition as core/attention.py: material content is
    # talk_seconds >= min-seconds OR word_count >= min-words, and a generic
    # speaker under both thresholds is a low-content fragment.
    assert "function _computeSpeakerAttention" in APP_JS
    assert "obsidian_gate_min_seconds" in APP_JS
    assert "obsidian_gate_min_words" in APP_JS
    assert "low-content fragment" in APP_JS
    thresholds = APP_JS[APP_JS.index("function _speakerAttentionThresholds"):]
    thresholds = thresholds[:thresholds.index("\n}")]
    assert "15" in thresholds and "25" in thresholds


# ── B. opening lands on the one surface ─────────────────────────────────────

def test_open_takes_no_tab_and_loads_the_clusters():
    block = APP_JS[APP_JS.index("function openSpeakerManager()"):]
    block = block[:block.index("\n}")]
    assert "loadSpeakerClusters()" in block
    assert "_cleanupSyncFooter()" in block
    assert "refreshSpeakerModalHeader()" in block
    # Old callers still pass 'cleanup'; the signature ignores it rather than
    # branching on a tab that no longer exists.
    assert "openSpeakerManager('cleanup')" in APP_JS


def test_auto_open_after_recording_still_opens_the_modal():
    block = APP_JS[APP_JS.index("async function _maybeAutoOpenResolution"):]
    block = block[:block.index("\n}")]
    assert "openSpeakerManager('cleanup')" in block


def test_cleanup_offers_the_calendar_invite():
    """Resolve was folded into Cleanup (2026-09-04): the cluster loader fetches
    the calendar candidates alongside the clusters, the picker lists the
    invite's attendees ahead of the Voice Library, and the toolbar shows the
    invite's attendee count."""
    loader = APP_JS[APP_JS.index("async function loadSpeakerClusters"):]
    loader = loader[:loader.index("async function reloadSpeakerClusters")]
    assert "resolution_candidates" in loader
    assert "_cleanupReadCandidates" in loader
    picker = APP_JS[APP_JS.index("function _cleanupOpenPicker"):]
    picker = picker[:picker.index("function _cleanupPickerChooseProfile")]
    assert "On the calendar invite" in picker
    assert "_cleanupPickerChooseProfile(target, profile)" in picker
    assert "_cleanupPickerChooseNew(target, c.name)" in picker
    assert 'id="cleanup-calendar-note"' in INDEX
    assert "resolution-panel" not in APP_JS


def test_voice_library_is_still_one_click_from_the_modal():
    # The shortcut lived on the Manage tab; Cleanup is where library profiles
    # get linked, so it moved into that toolbar rather than being dropped.
    markup = _modal_markup()
    assert 'id="cleanup-library-btn"' in markup
    assert 'onclick="openFingerprintPanel()"' in markup


# ── C. one staged commit model ──────────────────────────────────────────────

def test_the_modal_states_its_commit_model_once():
    markup = _modal_markup()
    assert "Changes here are staged. Nothing is written until you click Apply." in markup
    # The retired tab's direct-edit promise went with it.
    assert "Edits here save as soon as you make them" not in INDEX


def test_cleanup_apply_lives_in_a_sticky_footer_with_a_count():
    markup = _modal_markup()
    assert 'class="cleanup-footer"' in markup
    assert 'id="cleanup-footer-status"' in markup
    assert 'id="cleanup-apply-btn"' in markup
    assert 'id="cleanup-reset-btn"' in markup
    assert "function _cleanupPendingChangeCount" in APP_JS
    assert "position: sticky" in CSS[CSS.index(".cleanup-footer {"):]


def test_cleanup_dirty_guards_go_through_uiconfirm():
    guards = re.findall(r"_cleanupState\.dirty\) \{", APP_JS)
    assert guards, "expected at least one cleanup dirty guard"
    assert "uiConfirm({" in APP_JS
    assert "Close without applying?" in APP_JS
    assert "Discard staged cleanup changes?" in APP_JS


def test_verbs_are_aligned():
    # "Link" binds a Voice Library identity, "Merge into" combines keys.
    # No stray "Auto-assign"/"Unassign"/"New group".
    assert "Auto-link" in INDEX
    assert "Merge into new group" in INDEX
    assert "Auto-assign" not in INDEX
    assert "Unassign (make unnamed)" not in APP_JS
    assert "Unlink from profile" in APP_JS


# ── D. the selection bar never covers Apply ─────────────────────────────────

def test_selection_bar_is_anchored_to_the_scroll_area_not_the_pane():
    """The bar used to be absolutely positioned against the whole pane, so it
    sat on top of the Apply footer whenever a speaker was selected. Its
    positioning context is now a wrapper that ends above the help text."""
    markup = _modal_markup()
    wrap = markup.index('class="cleanup-scroll-wrap"')
    bar = markup.index('id="cleanup-selbar"')
    close = markup.index("<!-- .cleanup-scroll-wrap -->")
    help_block = markup.index('class="cleanup-help"')
    footer = markup.index('class="cleanup-footer"')
    assert wrap < bar < close < help_block < footer

    rule = CSS[CSS.index(".cleanup-scroll-wrap {"):]
    rule = rule[:rule.index("}")]
    assert "position: relative" in rule

    bar_rule = CSS[CSS.index(".cleanup-selbar {"):]
    bar_rule = bar_rule[:bar_rule.index("}")]
    assert "position: absolute" in bar_rule


# ── E. the colour square is the colour picker ───────────────────────────────

def test_group_header_swatch_opens_a_colour_picker():
    card = APP_JS[APP_JS.index("function _cleanupRenderCluster"):]
    card = card[:card.index("function _cleanupTalkTime")]
    assert "cleanup-swatch-btn" in card
    assert "_cleanupOpenColorPicker(swatch, cluster)" in card
    # The swatch is a real sibling button in the header, never nested in one.
    assert "swatch = document.createElement('button')" in card
    # A group with no identity has nothing to persist a colour on, so it says so
    # instead of offering a picker that Apply would silently drop.
    assert "swatch.disabled = true" in card

    picker = APP_JS[APP_JS.index("function _cleanupOpenColorPicker"):]
    picker = picker[:picker.index("function _cleanupSetClusterColor")]
    assert "_SPEAKER_PALETTE.forEach" in picker
    assert "_cleanupSetClusterColor(cluster, color)" in picker
    # Reuses the one popover slot, so Escape and outside-click already work.
    assert "_cleanupPicker = pop" in picker
    assert "_cleanupPositionPicker(pop, anchorEl)" in picker
    assert "_cleanupPickerOutside" in picker
    # The picker is a body-level popover, so it needs unscoped palette styles.
    assert ".cleanup-color-grid .speaker-color-btn {" in CSS


def test_a_colour_change_is_staged_and_counted():
    setter = APP_JS[APP_JS.index("function _cleanupSetClusterColor"):]
    setter = setter[:setter.index("\n}")]
    assert "_cleanupMarkDirty()" in setter
    assert "renderSpeakerClusters()" in setter
    # No write here: Apply is still the only thing that writes.
    assert "fetch(" not in setter

    build = APP_JS[APP_JS.index("// Per-cluster identity snapshot."):]
    build = build[:build.index("return {")]
    assert "color:" in build
    count = APP_JS[APP_JS.index("function _cleanupPendingChangeCount"):]
    count = count[:count.index("\n}")]
    assert "identityBefore.color !== identityNow.color" in count


def test_apply_writes_the_staged_colour_to_the_profile():
    # The payload already carried a colour, but apply_cluster_corrections read
    # the colour back out of the profile row, so a staged recolour was dropped.
    apply_fn = SPEAKER_DB[SPEAKER_DB.index("def apply_cluster_corrections"):]
    apply_fn = apply_fn[:apply_fn.index("# Pass 2:")]
    assert "rename_global_speaker(gid, color=color)" in apply_fn
    assert "touched_profiles.add(gid)" in apply_fn
    # It runs before pass 2 opens its own connection.
    assert apply_fn.index("rename_global_speaker") < len(apply_fn)
    payload = APP_JS[APP_JS.index("async function applySpeakerCleanup"):]
    payload = payload[:payload.index("/* ── Cleanup video popup")]
    assert "color: c.color || null" in payload


# ── F. one playback helper ──────────────────────────────────────────────────

def test_single_voice_player_is_shared():
    assert "async function playSpeakerVoice" in APP_JS
    assert "window.playSpeakerVoice = playSpeakerVoice" in APP_JS
    assert "window.stopSpeakerVoice = stopSpeakerVoice" in APP_JS
    # The Manage-only implementation is gone.
    for retired in ("_mgrSampleAudio", "_mgrFetchSegs", "_mgrPlaySeq", "_mgrAudioEl"):
        assert retired not in APP_JS


def test_only_one_sample_plays_at_a_time():
    # Starting a voice sample stops the cleanup segment queue and vice versa.
    play = APP_JS[APP_JS.index("async function playSpeakerVoice"):]
    play = play[:play.index("window.playSpeakerVoice = playSpeakerVoice")]
    assert "_cleanupStopPlayback" in play
    queue = APP_JS[APP_JS.index("function _cleanupPlayQueue"):]
    queue = queue[:queue.index("function _cleanupPlayCurrent")]
    assert "stopSpeakerVoice()" in queue
    # Closing the modal stops it too.
    close = APP_JS[APP_JS.index("function closeSpeakerManager()"):]
    close = close[:close.index("\n}")]
    assert "stopSpeakerVoice()" in close


def test_clip_budget_is_shared_and_capped():
    budget = APP_JS[APP_JS.index("_VOICE_CLIP_DEFAULTS = "):]
    budget = budget[:budget.index("\n")]
    assert "maxTotalSec: 9" in budget
    assert "maxClipSec: 6" in budget
    assert "function _pickVoiceClips" in APP_JS


# ── G. polish ───────────────────────────────────────────────────────────────

def test_escape_closes_and_focus_lands_in_the_dialog():
    assert "function _speakerModalIsOpen" in APP_JS
    escape = APP_JS[APP_JS.index("function _speakerModalIsOpen"):]
    escape = escape[:escape.index("/** Move focus into the dialog")]
    assert "'Escape'" in escape and "closeSpeakerManager()" in escape
    # No tab strip to land on, so focus goes to the dialog itself.
    focus = APP_JS[APP_JS.index("function _speakerModalFocus()"):]
    focus = focus[:focus.index("\n}")]
    assert ".speaker-manager-dialog')?.focus()" in focus
    assert 'tabindex="-1"' in _modal_markup()
    assert 'role="dialog"' in _modal_markup()
    # The dirty guard still runs: closeSpeakerManager is the wrapped version.
    assert "closeSpeakerManager = async function (force)" in APP_JS


def test_picker_escape_does_not_close_the_whole_modal():
    # Without stopPropagation the modal's bubble handler also fired.
    handler = APP_JS[APP_JS.index("function _cleanupPickerKey"):]
    handler = handler[:handler.index("\n}")]
    assert "e.stopPropagation()" in handler
    assert "_cleanupClosePicker()" in handler


def test_pending_count_sees_staged_identity_changes():
    # Membership-only diffing reported zero for every staged link, unlink or
    # rename; the footer only said "1" because of the Math.max floor.
    assert "clusterSnapshot" in APP_JS
    build = APP_JS[APP_JS.index("// Per-cluster identity snapshot."):]
    build = build[:build.index("return {")]
    for field in ("global_id", "name", "new_name", "color"):
        assert field in build
    count = APP_JS[APP_JS.index("function _cleanupPendingChangeCount"):]
    count = count[:count.index("\n}")]
    assert "clusterSnap" in count
    assert "identityBefore" in count and "identityNow" in count


def test_staged_toast_rechecks_before_applying_and_is_dismissed_on_close():
    # "Close and discard" only clears the dirty flag, so a live toast could
    # still write edits the user had thrown away.
    assert "function _dismissCleanupStagedToast" in APP_JS
    close = APP_JS[APP_JS.index("function closeSpeakerManager()"):]
    close = close[:close.index("\n}")]
    assert "_dismissCleanupStagedToast()" in close


def test_no_api_surface_was_invented():
    # Every endpoint the modal touches already exists in app.py.
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '"/api/fingerprint/speakers", methods=["GET"]' in app_py
    assert '/api/fingerprint/sessions/<session_id>/link"' in app_py
    assert '/api/fingerprint/sessions/<session_id>/link/<speaker_key>"' in app_py
    assert '/speaker_clusters/apply", methods=["POST"]' in app_py


def test_new_styles_use_theme_tokens_and_respect_reduced_motion():
    section = CSS[CSS.index("/* ── Speakers modal shell"):]
    for token in ("--surface", "--border", "--fg", "--fg-muted", "--fg-subtle",
                  "--accent", "--yellow", "--radius-sm", "--font-ui"):
        assert f"var({token})" in section
    assert "prefers-reduced-motion" in section


def test_no_native_dialogs_in_the_modal_scripts():
    for source in (APP_JS, COMBO_JS):
        assert not re.search(r"\b(?:window\.)?(?:alert|confirm|prompt)\(", source)


def _css_depth(text: str) -> int:
    """Nesting depth after the whole sheet, ignoring comments and quoted values."""
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    stripped = re.sub(r"""(['"])[^'"\n]*\1""", '""', stripped)
    depth = 0
    for char in stripped:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return depth


def test_stylesheets_are_brace_balanced():
    # A dropped closing brace silently swallows every rule that follows it, which
    # is exactly how the dialog styles and this batch's styles both went dead.
    for name in ("ui_web/static/style.css",):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert _css_depth(text) == 0, f"{name} has an unclosed rule"


def test_no_em_or_en_dashes_in_the_new_files():
    # House style bans U+2014 and U+2013. Referenced by codepoint so this file
    # does not itself trip the rule it enforces.
    banned = (chr(0x2014), chr(0x2013))
    for path in ("ui_web/static/ui-combobox.js", "tests/test_speaker_modal.py"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert not any(ch in text for ch in banned), path
