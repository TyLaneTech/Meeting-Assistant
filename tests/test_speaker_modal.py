"""Static assertions for the one-shell Speakers modal (Manage/Cleanup).

These guard the contract the three tabs now share: one header, one status line,
one set of verbs, one playback helper, and a Voice Library combobox that loads
before app.js. They are file-level checks on purpose: the modal is vanilla JS in
a 20k-line file, so a unit harness would cost more than it catches.
"""
import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "ui_web/templates/index.html").read_text(encoding="utf-8")
COMBO_JS = (ROOT / "ui_web/static/ui-combobox.js").read_text(encoding="utf-8")
CSS = (ROOT / "ui_web/static/style.css").read_text(encoding="utf-8")


def _modal_markup() -> str:
    start = INDEX.index('id="speaker-manager-overlay"')
    end = INDEX.index('id="fp-match-toast"')
    return INDEX[start:end]


# ── A. one header, status line, legend, tab badges ──────────────────────────

def test_header_has_meeting_status_and_legend():
    markup = _modal_markup()
    for element_id in ("speaker-manager-meeting", "speaker-manager-status",
                       "speaker-manager-legend"):
        assert f'id="{element_id}"' in markup
    # The legend names both tabs so the modal explains itself.
    for tab in ("Manage", "Cleanup"):
        assert f"<strong>{tab}</strong>" in markup


def test_tabs_carry_badges_and_aria_roles():
    markup = _modal_markup()
    assert 'role="tablist"' in markup
    assert markup.count('role="tab"') == 2
    assert markup.count('role="tabpanel"') == 2
    for element_id in ("speaker-cleanup-badge", "speaker-cleanup-dirty"):
        assert f'id="{element_id}"' in markup
    for pane in ("manage", "cleanup"):
        assert f'id="speaker-tab-{pane}"' in markup
        assert f'aria-controls="speaker-pane-{pane}"' in markup
        assert f'aria-labelledby="speaker-tab-{pane}"' in markup


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


# ── B. deterministic landing ────────────────────────────────────────────────

def test_landing_defaults_to_cleanup():
    """The owner's rule (2026-09-04): with no explicit tab and nothing
    remembered for this session view, the Speaker Manager opens on Cleanup.
    Merging diarizer fragments is the first job on almost every recording, and
    the calendar's attendees are offered there; Manage is reached by choice."""
    block = APP_JS[APP_JS.index("function _speakerManagerInitialTab"):]
    block = block[:block.index("\nfunction openSpeakerManager")]
    assert "return explicitTab" in block
    assert "return _speakerModalLastTab" in block
    defaults = block[block.index("return _speakerModalLastTab"):]
    assert "return 'cleanup'" in defaults
    # The stats-driven jump to the old Resolve tab is gone: the landing tab
    # never changes underneath the user once it is showing.
    assert "return 'resolve'" not in defaults
    assert "return 'manage'" not in defaults


def test_auto_open_after_recording_lands_on_cleanup():
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


# ── C. commit models stated per tab ─────────────────────────────────────────

def test_each_tab_states_its_commit_model():
    markup = _modal_markup()
    assert "Edits here save as soon as you make them" in markup
    assert "Changes here are staged. Nothing is written until you click Apply." in markup


def test_manage_is_direct_edit_with_undo():
    assert 'id="speaker-save-btn"' in INDEX and ">Save changes<" in INDEX
    assert 'id="speaker-editor-unsaved"' in INDEX
    assert "function _mgrToastSaved" in APP_JS
    assert "label: 'Undo'" in APP_JS
    # The colour swatch writes through and offers the same Undo.
    colour = APP_JS[APP_JS.index("_SPEAKER_PALETTE.forEach(color =>"):]
    colour = colour[:colour.index("colorGridEl.appendChild(btn);")]
    assert "_mgrToastSaved(" in colour


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


def test_verbs_are_aligned_across_tabs():
    # "Assign name" sets a display name, "Link" binds a Voice Library identity,
    # "Merge into" combines keys. No stray "Auto-assign"/"Unassign"/"New group".
    assert "Auto-link" in INDEX
    assert "Merge into new group" in INDEX
    assert "Auto-assign" not in INDEX
    assert "Unassign (make unnamed)" not in APP_JS
    assert "Unlink from profile" in APP_JS


# ── D. Voice Library combobox ───────────────────────────────────────────────

def test_combobox_script_loads_before_app_js():
    assert INDEX.index("/static/ui-dialog.js") < INDEX.index("/static/ui-combobox.js")
    assert INDEX.index("/static/ui-combobox.js") < INDEX.index("/static/app.js")


def test_combobox_is_keyboard_navigable_and_exposed():
    assert "window.uiCombobox" in COMBO_JS
    for key in ("ArrowDown", "ArrowUp", "Home", "End", "Enter", "Escape"):
        assert f"case '{key}'" in COMBO_JS
    assert 'role="combobox"' in COMBO_JS or "'combobox'" in COMBO_JS
    assert "aria-activedescendant" in COMBO_JS
    assert "'listbox'" in COMBO_JS


def test_manage_replaces_the_datalist_with_the_combobox():
    assert 'id="speaker-name-combo"' in INDEX
    assert "speaker-name-options" not in INDEX     # the old datalist is gone
    assert 'list="speaker-name-options"' not in INDEX
    assert "_mgrEnsureNameCombo" in APP_JS
    assert "allowTyped: true" in APP_JS
    assert "typedLabel: 'Use typed name'" in APP_JS


def test_manage_links_and_unlinks_voice_library_profiles():
    assert "async function linkSelectedSpeakersToProfile" in APP_JS
    assert "async function unlinkSelectedSpeakers" in APP_JS
    link = APP_JS[APP_JS.index("async function linkSelectedSpeakersToProfile"):]
    link = link[:link.index("/** Drop the Voice Library binding")]
    assert "/link`" in link and "method: 'POST'" in link
    # apply_name makes the server propagate the profile name and colour.
    assert "apply_name: true" in link
    unlink = APP_JS[APP_JS.index("async function unlinkSelectedSpeakers"):]
    unlink = unlink[:unlink.index("/* ── Modal chrome")]
    assert "method: 'DELETE'" in unlink
    assert 'id="speaker-unlink-btn"' in INDEX


def test_no_api_surface_was_invented():
    # Every endpoint the modal shell touches already exists in app.py.
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '"/api/fingerprint/speakers", methods=["GET"]' in app_py
    assert '/api/fingerprint/sessions/<session_id>/link"' in app_py
    assert '/api/fingerprint/sessions/<session_id>/link/<speaker_key>"' in app_py


# ── E. one playback helper ──────────────────────────────────────────────────

def test_single_voice_player_is_shared():
    assert "async function playSpeakerVoice" in APP_JS
    assert "window.playSpeakerVoice = playSpeakerVoice" in APP_JS
    assert "window.stopSpeakerVoice = stopSpeakerVoice" in APP_JS
    # The Manage-only implementation is gone.
    for retired in ("_mgrSampleAudio", "_mgrFetchSegs", "_mgrPlaySeq", "_mgrAudioEl"):
        assert retired not in APP_JS
    # Manage routes through it.
    wrapper = APP_JS[APP_JS.index("function playManageSpeakerVoice"):]
    wrapper = wrapper[:wrapper.index("\n}")]
    assert "playSpeakerVoice({" in wrapper


def test_only_one_sample_plays_at_a_time():
    # Starting a voice sample stops the cleanup segment queue and vice versa.
    play = APP_JS[APP_JS.index("async function playSpeakerVoice"):]
    play = play[:play.index("window.playSpeakerVoice = playSpeakerVoice")]
    assert "_cleanupStopPlayback" in play
    queue = APP_JS[APP_JS.index("function _cleanupPlayQueue"):]
    queue = queue[:queue.index("function _cleanupPlayCurrent")]
    assert "stopSpeakerVoice()" in queue
    # Modal close and tab switch both stop it.
    close = APP_JS[APP_JS.index("function closeSpeakerManager()"):]
    close = close[:close.index("\n}")]
    assert "stopSpeakerVoice()" in close
    switch = APP_JS[APP_JS.index("function switchSpeakerManagerTab"):]
    switch = switch[:switch.index("function openSpeakerCleanupTab")]
    assert "stopSpeakerVoice()" in switch


def test_clip_budget_is_shared_and_capped():
    budget = APP_JS[APP_JS.index("_VOICE_CLIP_DEFAULTS = "):]
    budget = budget[:budget.index("\n")]
    assert "maxTotalSec: 9" in budget
    assert "maxClipSec: 6" in budget
    assert "function _pickVoiceClips" in APP_JS


# ── F. polish ───────────────────────────────────────────────────────────────

def test_escape_closes_and_focus_lands_on_open():
    assert "function _speakerModalIsOpen" in APP_JS
    assert "function _speakerModalFocusTab" in APP_JS
    escape = APP_JS[APP_JS.index("function _speakerModalIsOpen"):]
    escape = escape[:escape.index("/** Put the caret")]
    assert "'Escape'" in escape and "closeSpeakerManager()" in escape
    # The dirty guard still runs: closeSpeakerManager is the wrapped version.
    assert "closeSpeakerManager = async function (force)" in APP_JS


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


# ── Fix round 1: regressions found by driving the harness ───────────────────

def test_background_render_does_not_clobber_an_in_progress_edit():
    # P1-1. renderSpeakerManager runs on every transcript segment and on every
    # speaker_label / fingerprint event while the modal is open. Resyncing the
    # field there moved _mgrCommittedName up to the uncommitted draft, so the
    # change guard short-circuited and the rename was silently dropped.
    assert "function _mgrNameFieldIsFocused" in APP_JS
    block = APP_JS[APP_JS.index("const combo = _mgrEnsureNameCombo();"):]
    block = block[:block.index("colorGridEl.innerHTML")]
    assert "!_mgrNameFieldIsFocused()" in block
    assert "_mgrCommittedName = _speakerDraftName" in block


def test_enter_on_the_typed_row_commits_in_one_press():
    # P1-2. The combobox preventDefaults Enter, so the native change event never
    # fires; the typed branch has to commit itself or it takes two presses.
    assert "function _mgrCommitTypedName" in APP_JS
    select = APP_JS[APP_JS.index("onSelect: (item, meta) => {"):]
    select = select[:select.index("linkSelectedSpeakersToProfile(item.id")]
    assert "_mgrCommitTypedName()" in select
    assert "_mgrNameCombo.input.addEventListener('change', _mgrCommitTypedName)" in APP_JS
    commit = APP_JS[APP_JS.index("function _mgrCommitTypedName"):]
    commit = commit[:commit.index("\n}")]
    assert "applySpeakerEditor()" in commit


def test_combobox_does_not_open_its_list_on_focus():
    # P1-3. Focusing on open dropped a list taller than the dialog over the
    # editor's action row. It opens on typing, arrows, or the toggle only.
    assert "addEventListener('focus'" not in COMBO_JS
    assert "input.addEventListener('blur'" in COMBO_JS
    keydown = COMBO_JS[COMBO_JS.index("input.addEventListener('keydown'"):]
    assert "move(1)" in keydown and "move(-1)" in keydown
    toggle = COMBO_JS[COMBO_JS.index("toggle.addEventListener('mousedown'"):]
    toggle = toggle[:toggle.index("\n    });")]
    assert "openList(false)" in toggle


def test_setvalue_refreshes_an_open_list():
    setter = COMBO_JS[COMBO_JS.index("setValue(value) {"):]
    setter = setter[:setter.index("getValue()")]
    assert "if (open) openList(true);" in setter


def test_picker_escape_does_not_close_the_whole_modal():
    # P2-4. Without stopPropagation the modal's bubble handler also fired.
    handler = APP_JS[APP_JS.index("function _cleanupPickerKey"):]
    handler = handler[:handler.index("\n}")]
    assert "e.stopPropagation()" in handler
    assert "_cleanupClosePicker()" in handler


def test_pending_count_sees_staged_identity_changes():
    # P2-5. Membership-only diffing reported zero for every staged link,
    # unlink or rename; the footer only said "1" because of the Math.max floor.
    assert "clusterSnapshot" in APP_JS
    build = APP_JS[APP_JS.index("// Per-cluster identity snapshot."):]
    build = build[:build.index("return {")]
    for field in ("global_id", "name", "new_name"):
        assert field in build
    count = APP_JS[APP_JS.index("function _cleanupPendingChangeCount"):]
    count = count[:count.index("\n}")]
    assert "clusterSnap" in count
    assert "identityBefore" in count and "identityNow" in count


def test_cleanup_badge_always_means_group_count():
    # P2-6. The placeholder used to count unlinked speakers, so the badge
    # changed meaning the moment the clusters loaded.
    quick = APP_JS[APP_JS.index("function _cleanupPaintQuickBadge"):]
    quick = quick[:quick.index("\n}")]
    assert "badge.hidden = true" in quick
    assert "_countUnlabeledSpeakers" not in quick
    # The retired helpers are gone entirely.
    assert "_countUnlabeledSpeakers" not in APP_JS
    assert "_hasUnlabeledSpeakers" not in APP_JS
    loaded = APP_JS[APP_JS.index("function _cleanupUpdateBadge"):]
    loaded = loaded[:loaded.index("\n}")]
    assert "speaker group" in loaded


def test_undo_restores_a_previous_voice_library_binding():
    # P2-7. Re-linking an already-linked speaker skipped the DELETE, so Undo
    # reverted the name but left the binding on the new profile.
    link = APP_JS[APP_JS.index("async function linkSelectedSpeakersToProfile"):]
    link = link[:link.index("/** Drop the Voice Library binding")]
    assert "linksBefore" in link
    assert "linkedBefore" not in link
    undo = link[link.index("label: 'Undo'"):]
    assert "method: 'DELETE'" in undo
    assert "row.link.global_id" in undo
    assert "method: 'POST'" in undo


def test_link_applies_the_profile_colour_client_side():
    link = APP_JS[APP_JS.index("async function linkSelectedSpeakersToProfile"):]
    link = link[:link.index("/** Drop the Voice Library binding")]
    assert "profileColor" in link
    assert "update.color = profileColor" in link


def test_staged_toast_rechecks_before_applying_and_is_dismissed_on_close():
    # P2-8. "Close and discard" only clears the dirty flag, so a live toast
    # could still write edits the user had thrown away.
    toast = APP_JS[APP_JS.index("_cleanupStagedToast = uiToast({"):]
    toast = toast[:toast.index("_cleanupActiveTab = tab;")]
    assert "!_cleanupState || !_cleanupState.dirty" in toast
    assert "function _dismissCleanupStagedToast" in APP_JS
    close = APP_JS[APP_JS.index("function closeSpeakerManager()"):]
    close = close[:close.index("\n}")]
    assert "_dismissCleanupStagedToast()" in close


def test_speaker_row_has_no_interactive_element_nested_in_a_button():
    row = APP_JS[APP_JS.index("groups.forEach(group => {"):]
    row = row[:row.index("listEl.appendChild(row);")]
    assert "const row = document.createElement('div');" in row
    assert "speaker-row-select" in row
    play = row[row.index("speaker-row-play"):]
    assert "role', 'button'" not in play
    assert "tabIndex" not in play
    # The play control is created as a real button, not a span.
    created = row[row.index("// Play this speaker"):]
    assert "createElement('button')" in created
    assert "#speaker-manager-overlay .speaker-row-select" in CSS


def test_voice_audio_listeners_are_bound_once():
    # A per-play {once:true} error listener only detaches when it fires, so on a
    # healthy file they accumulated one per click.
    el = APP_JS[APP_JS.index("function _voiceAudioEl"):]
    el = el[:el.index("\n}")]
    assert "addEventListener('error'" in el
    play = APP_JS[APP_JS.index("async function playSpeakerVoice"):]
    play = play[:play.index("window.playSpeakerVoice = playSpeakerVoice")]
    assert "{ once: true }" not in play
    assert "addEventListener('error'" not in play
    assert "_voiceMetaHandler" in play


def test_no_dead_duplicate_editor_row_rule():
    # #speaker-manager-overlay .speaker-editor-row already sets this and wins.
    assert CSS.count(".speaker-editor-row {") == 1
    assert "#speaker-manager-overlay .speaker-editor-row {" in CSS


def test_no_em_or_en_dashes_in_the_new_files():
    # House style bans U+2014 and U+2013. Referenced by codepoint so this file
    # does not itself trip the rule it enforces.
    banned = (chr(0x2014), chr(0x2013))
    for path in ("ui_web/static/ui-combobox.js", "tests/test_speaker_modal.py"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert not any(ch in text for ch in banned), path
