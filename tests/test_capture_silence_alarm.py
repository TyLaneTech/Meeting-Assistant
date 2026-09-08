"""The loopback silence alarm: loud enough for a real fault, quiet otherwise.

The alarm exists because a call can play to a device nobody is capturing and
the recording comes back one-sided with nothing having looked wrong (the
2026-09-01 dead-loopback failure). It has to keep firing for that.

What it must not do is fire during a normal conversation, which it did: the
watchdog read one instantaneous RMS every two seconds, so it kept sampling the
gaps between words and calling a live call silent. The numbers below are the
guard on both halves of that.

Most of this reads the source rather than importing it, so it runs off Windows
too: `capture_audio.windows` needs PyAudioWPatch.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SRC = (Path(__file__).parents[1] / "capture_audio/windows.py").read_text(encoding="utf-8")


def _watchdog() -> str:
    body = SRC[SRC.index("def _loopback_silence_watchdog("):]
    return body[:body.index("def _emit_loopback_recovered(")]


def _const(name: str) -> float:
    found = re.search(rf"^\s+{name} = ([\d.]+)\s*(?:#.*)?$", _watchdog(), re.M)
    assert found, f"{name} is not a constant in the watchdog any more"
    return float(found.group(1))


# ── The mechanism: a window's peak, not a spot reading ───────────────────────

def test_the_watchdog_reads_peaks_not_instantaneous_levels():
    """A spot RMS read every two seconds lands between two words routinely.
    The question is "did this device produce anything in the interval", and
    only a peak over the whole interval answers it."""
    body = _watchdog()
    assert "lb_peak, mic_peak = self.take_peaks()" in body
    assert "if lb_peak > SILENT_FLOOR:" in body
    assert "elif mic_peak > MIC_ACTIVE:" in body
    # The spot readings must not creep back into the alarm path.
    assert "self.loopback_level >" not in body
    assert "self.mic_level >" not in body


@pytest.mark.skipif(sys.platform != "win32", reason="WASAPI loopback is Windows-only")
def test_take_peaks_returns_the_window_maximum_and_resets():
    windows = pytest.importorskip("capture_audio.windows")
    cap = windows.AudioCapture.__new__(windows.AudioCapture)
    cap.loopback_peak = 0.0
    cap.mic_peak = 0.0

    # One window of a talking-then-quiet stretch: the peak is what survives.
    for lb, mic in ((0.0004, 0.0), (0.12, 0.03), (0.0003, 0.0)):
        cap.loopback_peak = max(cap.loopback_peak, lb)
        cap.mic_peak = max(cap.mic_peak, mic)
    assert cap.take_peaks() == (0.12, 0.03)
    # Read and reset, so consecutive windows partition the timeline with no
    # sample counted twice and none falling through the gap.
    assert cap.take_peaks() == (0.0, 0.0)


# ── The thresholds, against a real call ──────────────────────────────────────

# One minute of a live two-way Teams call, sampled about every 1.5 s
# (2026-09-08). This is the trace that was firing the alarm every couple of
# minutes while both sides were recording perfectly.
LIVE_CALL_LOOPBACK = [
    0.0004, 0.0004, 0.0311, 0.0004, 0.0004, 0.0004, 0.0182, 0.0421, 0.0004,
    0.0003, 0.0004, 0.0004, 0.0004, 0.1500, 0.0620, 0.0004, 0.0004, 0.0004,
    0.0091, 0.0270, 0.0004, 0.0004, 0.0004, 0.0004, 0.0053, 0.0004, 0.0004,
    0.0004, 0.0135, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004, 0.0004,
    0.0004, 0.0004, 0.0004, 0.0004,
]
SAMPLE_GAP = 1.5


def _longest_silent_run(levels, floor) -> float:
    best = run = 0
    for value in levels:
        run = run + 1 if value <= floor else 0
        best = max(best, run)
    return best * SAMPLE_GAP


def test_a_real_call_never_looks_silent_for_long_enough_to_alarm():
    """The regression, in numbers. Against the old 0.003 threshold this call
    read as silent for 16 s at a stretch, and a 25 s alarm was one gap away.
    The measured floor keeps the same minute under five."""
    floor = _const("SILENT_FLOOR")
    drop_after = _const("DROP_AFTER")
    assert _longest_silent_run(LIVE_CALL_LOOPBACK, 0.003) >= 15, \
        "the trace should still show why the old threshold failed"
    quiet = _longest_silent_run(LIVE_CALL_LOOPBACK, floor)
    assert quiet < drop_after / 3, (
        f"a real call reads silent for {quiet}s against a {drop_after}s alarm; "
        "the margin is too thin to trust"
    )


def test_the_floor_still_catches_a_dead_loopback():
    """A loopback nobody is rendering to delivers digital silence, and the
    device's own idle noise floor measured 0.0003. Both stay under."""
    floor = _const("SILENT_FLOOR")
    assert floor > 0.0003, "the idle noise floor would read as signal"
    assert _longest_silent_run([0.0] * 120, floor) == 120 * SAMPLE_GAP
    assert _longest_silent_run([0.0003] * 120, floor) == 120 * SAMPLE_GAP
    # And well under the speech threshold, or a lull reads as a fault again.
    assert floor < 0.003


def test_the_alarm_needs_a_conversation_not_just_a_quiet_room():
    """Silence on the desktop side only means something is wrong if someone is
    talking into this end. Otherwise an idle desk between meetings, or a
    recording left running, looks exactly like a one-sided call."""
    body = _watchdog()
    assert "mic_active_for >= MIC_ACTIVE_NEEDED" in body
    assert "silent_for > DROP_AFTER" in body
    assert _const("MIC_ACTIVE_NEEDED") >= 30
    assert _const("DROP_AFTER") >= 60
    # Accrued only while the desktop side is quiet, and reset the moment it
    # is not, so the requirement always describes one silent stretch.
    assert "mic_active_for = 0.0" in body
    assert "mic_active_for += 2.0" in body


def test_a_switched_device_starts_its_accounting_over():
    body = _watchdog()
    switch = body[body.index("if switched:"):]
    switch = switch[:switch.index("continue")]
    assert "last_signal_ts = grace_base = now" in switch
    assert "mic_active_for = 0.0" in switch


# ── Recovery: the warning does not outlive the fault ─────────────────────────

def test_the_loopback_reports_recovering():
    body = _watchdog()
    assert "if alarm_showing:" in body
    assert "self._emit_loopback_recovered()" in body
    assert "self.on_loopback_recovered" in SRC
    emit = SRC[SRC.index("def _emit_loopback_recovered("):]
    emit = emit[:emit.index("def _emit_loopback_silent(")]
    assert "cb(self._loopback_device_name)" in emit
    # Never raises into the capture thread, like its alarm counterpart.
    assert "except Exception as e:" in emit


def test_the_app_turns_recovery_into_a_banner_clear():
    app = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert "def _alert_loopback_recovered(" in app
    assert "capture.on_loopback_recovered = (" in app
    route = app[app.index("def _alert_loopback_recovered("):]
    route = route[:route.index("def _recording_prereqs_locked(")]
    assert '"cleared": True' in route
    # Only for the session that is actually recording, like the alarm.
    assert '_state.get("session_id") != session_id' in route
    # No toast: "it is fine again" is not worth interrupting anyone for.
    assert "notifications.notify" not in route


def test_the_client_clears_the_banner_by_itself():
    js = (Path(__file__).parents[1] / "ui_web/static/app.js").read_text(encoding="utf-8")
    show = js[js.index("function _showCaptureAlert("):]
    show = show[:show.index("function _clearCaptureAlert(")]
    assert "d.cleared || d.level === 'clear'" in show
    # Desktop audio in the meters is the most direct proof there is.
    meters = js[js.index("function updateLevelMeters("):]
    meters = meters[:meters.index("function startVizLoop(")]
    assert "_clearCaptureAlert();" in meters
    # And a stop takes it down, whatever the loopback was doing.
    assert "if (_wasRecording) _clearCaptureAlert();" in js


# ── The elapsed clock survives a reload ──────────────────────────────────────

def test_the_server_owns_the_elapsed_clock():
    app = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    payload = app[app.index("def _status_payload("):]
    payload = payload[:payload.index("def _push_status(")]
    assert 'payload["elapsed_sec"] = None' in payload
    # The WAV writer's clock is the meeting timeline the transcript is stamped
    # against, with the monotonic start as the fallback before it exists.
    assert "capture.wav_writer.elapsed_seconds" in payload
    assert 'time.monotonic() - started_mono' in payload

    js = (Path(__file__).parents[1] / "ui_web/static/app.js").read_text(encoding="utf-8")
    start = js[js.index("function startDurationCounter("):]
    start = start[:start.index("function _syncDurationCounter(")]
    assert "function startDurationCounter(elapsedSec)" in start
    assert "_recordingStartTime = Date.now() - (behind > 0 ? behind * 1000 : 0);" in start
    assert "startDurationCounter(d.elapsed_sec);" in js


def test_a_running_clock_re_anchors_only_on_a_real_gap():
    """A reconnect or a machine that slept snaps back; ordinary ticks must not
    jitter the readout by a second every status event."""
    js = (Path(__file__).parents[1] / "ui_web/static/app.js").read_text(encoding="utf-8")
    body = js[js.index("function _syncDurationCounter("):]
    body = body[:body.index("\n}") + 2]
    assert "if (!_durationInterval || !(behind > 0)) return;" in body
    assert "if (Math.abs(local - behind) < 2) return;" in body
    assert "_syncDurationCounter(d.elapsed_sec);" in js
