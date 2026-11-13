"""The transcript toolbar and the speaker pickers answer at once on long meetings.

Report 2026-11-13: the Filter button lagged, and the speaker picker on a
transcript badge took a long time to appear, worst on Ctrl/Shift+click. On a
2,613-line meeting with 117 speaker keys (headless Chrome on the owner's
machine, click to next paint, medians of three runs) Filter took 648 ms,
Analytics 664 ms, the rename picker 584 ms and the first Ctrl+click 1,000 ms;
afterwards 128, 168, 80 and 32 ms.

The cause was one helper: _getSortedSpeakerProfiles sorted by line count and
counted inside the comparator, walking every line on every comparison (about
400 ms there). The filter pills, analytics, both pickers and the similarity
ranking each called it, some more than once per click. Around it: speaker
events refetched the 400 KB recordings list once per event, the slide panels
animated max-height (a full transcript layout per frame, plus a second one
whenever the scrollbar was re-decided), a filter re-placed every chapter
heading, re-shown lines replayed their entrance fade, and analytics ran a timer
per timeline segment.

app.js cannot be loaded under node (browser globals at module scope), so the
counting functions are lifted out and run against stand-in lines; the rest is
held by source checks.

Run: .venv/Scripts/python -m pytest tests/test_transcript_toolbar_speed.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "ui_web" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
STYLE = (STATIC / "style.css").read_text(encoding="utf-8")
_NODE = shutil.which("node")


def _fn(name: str) -> str:
    """The source of one top-level function in app.js (up to its closing brace
    at column 0; either line ending)."""
    start = APP_JS.index(f"function {name}(")
    end = re.compile(r"\n\}\r?\n").search(APP_JS, start).end()
    return APP_JS[start:end]


def _rule(selector: str) -> str:
    """The body of the first CSS rule whose selector list is exactly this."""
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", STYLE)
    assert m, selector
    return m.group(1)


_SORT_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/\r\n/g, '\n');
function lift(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error('FAIL: ' + name + ' not found');
  const end = src.indexOf('\n}\n', start);
  return src.slice(start, end + 2);
}
const _NOISE_LABEL = '[Noise]';
let _speakerProfiles = {}, _speakerLabels = {};
const _speakerColors = {};
let queries = 0;
function seg(key) {
  const badge = key ? { dataset: { speakerKey: key } } : null;
  return { querySelector(sel) { queries++; return sel.includes('src-speaker') ? badge : null; } };
}
eval(['_isCustomSpeakerKey', '_speakerDisplayName', '_upsertSpeakerProfile',
      '_ensureSpeakerProfile', '_speakerBadgeCounts', '_speakerBadgeCount',
      '_getSortedSpeakerProfiles'].map(lift).join('\n'));

// 40 keys, line counts 1..40, plus mic lines with no speaker badge.
let _segmentRegistry = [];
for (let k = 1; k <= 40; k++) for (let i = 0; i < k; i++) _segmentRegistry.push(seg('Speaker ' + k));
for (let i = 0; i < 25; i++) _segmentRegistry.push(seg(null));
_speakerLabels['custom:1'] = 'Zed';          // a speaker with no lines yet
_speakerProfiles['custom:1'] = { speaker_key: 'custom:1', name: 'Zed', custom: true };
_speakerLabels['Speaker 99'] = 'Speaker 99';   // a label with no lines

queries = 0;
const sorted = _getSortedSpeakerProfiles();
const n = _segmentRegistry.length;
if (queries > n) throw new Error(`FAIL: one sort queried ${queries} times for ${n} lines`);
const keys = sorted.map(p => p.speaker_key);
if (keys[0] !== 'Speaker 40' || keys[39] !== 'Speaker 1') throw new Error('FAIL: not by line count: ' + keys.slice(0, 3));
// No lines: the non-custom label first, the custom speaker last.
if (keys[40] !== 'Speaker 99' || keys[41] !== 'custom:1') throw new Error('FAIL: tail ' + keys.slice(40));

// Counts handed in are used as they are (no second pass).
const counts = _speakerBadgeCounts();
queries = 0;
_getSortedSpeakerProfiles(counts);
if (queries !== 0) throw new Error('FAIL: re-counted with counts supplied');
if (counts.get('Speaker 7') !== 7 || _speakerBadgeCount('Speaker 12') !== 12) throw new Error('FAIL: counts');
console.log('OK');
"""


@pytest.mark.skipif(_NODE is None, reason="node is not on PATH")
def test_sorting_speakers_counts_each_line_once():
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "sort.js"
        harness.write_text(_SORT_HARNESS, encoding="utf-8")
        result = subprocess.run(
            [_NODE, str(harness), str(STATIC / "app.js").replace("\\", "/")],
            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OK" in result.stdout


def test_no_comparator_or_loop_counts_lines_per_key():
    # A count inside a sort or a per-group reduce is the quadratic walk.
    assert "_speakerBadgeCount(b.speaker_key)" not in APP_JS
    for name in ("_tnRenderSpeakerPills", "_renderAnalytics", "editSpeakerLabel",
                 "_buildPickerSpeakerOptions"):
        assert "_speakerBadgeCount(" not in _fn(name), name


def test_pills_redraw_once_a_frame_and_only_while_the_filter_is_open():
    refresh = _fn("_tnRefreshSpeakerPills")
    assert "requestAnimationFrame" in refresh
    assert "classList.contains('collapsed')" in refresh
    assert "_refreshAnalytics()" in refresh
    # Opening the panel draws them before it shows.
    opener = _fn("openTranscriptFilter")
    assert opener.index("_tnRenderSpeakerPills()") < opener.index("panel.classList.remove('collapsed')")


def test_speaker_events_coalesce_the_recordings_list_refetch():
    for event in ("speaker_label", "speaker_linked", "fingerprint_auto_applied"):
        assert f"AppData.invalidateSoon(['sessions', 'attention', 'analytics'], '{event}')" in APP_JS, event
        assert f"AppData.invalidate(['sessions', 'attention', 'analytics'], '{event}')" not in APP_JS, event
    assert "invalidateSoon(names, reason) {" in APP_JS


def test_the_pickers_reuse_the_voice_library_list():
    builder = _fn("_buildPickerSpeakerOptions")
    assert "fetch('/api/fingerprint/speakers')" not in builder
    assert "_refreshPickerLibrary()" in builder
    assert "if (_pickerLibrary) renderLibrary(_pickerLibrary);" in builder
    # One name lookup for the list, not a scan of every speaker per option.
    assert "_speakerNameKey(" not in builder


def test_a_selection_click_touches_only_the_lines_that_changed():
    paint = _fn("_updateTranscriptSelectionUI")
    assert "_segmentRegistry" not in paint
    assert "_transcriptSelPainted" in paint
    assert "_transcriptSelPainted = new Set();" in _fn("_clearSegmentRegistry")


def test_a_filter_change_does_not_replace_the_chapter_headings():
    assert "_refreshTranscriptChapterHeadings();" in _fn("applyTranscriptFilter")
    assert "renderTranscriptChapterHeadings();" not in _fn("applyTranscriptFilter")
    refresh = _fn("_refreshTranscriptChapterHeadings")
    assert "p.chapters === _chapters && p.registry === _segmentRegistry" in refresh


def test_lines_shown_again_do_not_replay_their_entrance():
    assert "seg.classList.add('is-reshown')" in _fn("_applyFilterToSeg")
    reshown = STYLE.index(".transcript-segment.is-reshown { animation: none; }")
    # Before the search flash, which has the same specificity and must win.
    assert reshown < STYLE.index(".transcript-segment.search-flash {")


def test_slide_panels_do_not_animate_their_height():
    for selector in (".transcript-navigator", ".fp-notif-panel", ".analytics-panel"):
        body = _rule(selector)
        assert "transition" not in body or "max-height" not in body, selector
    for selector in (".transcript-navigator.collapsed", ".fp-notif-panel.collapsed"):
        assert "max-height" not in STYLE[STYLE.index(selector):STYLE.index("}", STYLE.index(selector))]
    assert ".analytics-panel.collapsed { display: none; }" in STYLE
    assert "transition: max-height" not in STYLE[STYLE.index(".fp-notif-panel {"):STYLE.index(".analytics-panel::-webkit-scrollbar")]


def test_the_transcript_reserves_its_scrollbar():
    assert "#transcript { scrollbar-gutter: stable; }" in STYLE


def test_analytics_animates_without_a_timer_per_segment_or_a_width_transition():
    render = _fn("_renderAnalytics")
    assert "seg.classList.add('visible')" not in render
    assert "bar.style.width" not in render
    assert "bar.classList.add('is-in')" in render
    fill = _rule(".analytics-bar-fill")
    assert "transition: transform" in fill and "width" not in fill.replace("min-width", "")
    assert "transform: scaleX(1);" in _rule(".analytics-tl-track")
