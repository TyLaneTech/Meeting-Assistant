"""The watchdog follows the call audio even when Windows' Communications
device is not where the call app plays.

2026-09-15: Teams rendered to the Realtek headphone jack while the Windows
default and Communications device was the Shure MV6. The loopback bound to
the Shure, every render probe saw the call playing on the Realtek jack, and
the unconditional "never leave the comms device" rule kept the capture on the
silent Shure for three whole calls. These tests replay that day's probe
sequence against the pure decision and pin the behaviour that would have
saved it, plus the far-end-pause case the rule was originally written for.
Run: .venv/Scripts/python -m pytest tests/test_follow_decision.py
"""
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("WASAPI loopback capture is Windows-only", allow_module_level=True)
windows = pytest.importorskip("capture_audio.windows")

SHURE = "Headphones (3- Shure MV6)"
SHURE_LB = SHURE + " [Loopback]"
REALTEK = "Headphones (Realtek(R) Audio)"
REALTEK_LB = REALTEK + " [Loopback]"


def _decide(**kw):
    base = dict(held=SHURE_LB, held_had_signal=False, silent_for=0.0,
                comms=SHURE, target=REALTEK)
    base.update(kw)
    return windows.follow_decision(**base)


# ── 2026-09-15: the call plays on a device that is not the comms default ──────

def test_comms_device_that_never_played_is_left_at_once():
    # Record start: the Shure has produced nothing, the probe hears the call on
    # the Realtek jack. There is nothing to be sticky about.
    action, why = _decide(held_had_signal=False, silent_for=3.0)
    assert action == "switch"
    assert "never produced signal" in why and REALTEK in why


def test_comms_device_silent_past_the_hold_is_left():
    # Mid-meeting: a notification chime hit the Shure minutes ago (so it "had
    # signal"), the call has been on the Realtek jack the whole time.
    action, _ = _decide(held_had_signal=True, silent_for=300.0)
    assert action == "switch"


def test_replaying_the_whole_day_switches_on_the_first_probe():
    # The exact probe results from the 2026-09-15 launcher log: every probe saw
    # the Realtek jack playing and the Shure at 0.0. Before the fix, all 200+
    # probes returned "hold"; now the first one moves the capture.
    peaks = [0.27454, 0.05124, 0.03748, 0.5325, 0.43964, 0.44879, 0.13065, 0.35751]
    decisions = [
        _decide(held_had_signal=(i > 0), silent_for=120.0 * (i + 1),
                target=REALTEK if peak >= 0.01 else None)[0]
        for i, peak in enumerate(peaks)
    ]
    assert decisions[0] == "switch"
    assert "hold" not in decisions


# ── The far-end pause the sticky rule exists for ─────────────────────────────

def test_brief_pause_on_the_call_device_holds_it():
    # The call on the Shure went quiet 20 s ago while music plays on the
    # Realtek jack: stay on the call.
    action, why = _decide(held_had_signal=True, silent_for=20.0)
    assert action == "hold"
    assert SHURE in why and REALTEK in why


def test_hold_ends_exactly_at_the_configured_window():
    assert _decide(held_had_signal=True, silent_for=89.9)[0] == "hold"
    assert _decide(held_had_signal=True, silent_for=90.0)[0] == "switch"
    assert _decide(held_had_signal=True, silent_for=10.0, sticky_hold=5.0)[0] == "switch"


# ── Everything else is unchanged ─────────────────────────────────────────────

def test_nothing_playing_never_moves_the_capture():
    action, _ = _decide(target=None, held_had_signal=True, silent_for=600.0)
    assert action == "none"


def test_already_on_the_playing_device_is_a_no_op():
    action, _ = _decide(held=REALTEK_LB, target=REALTEK, comms=SHURE, silent_for=30.0)
    assert action == "none"


def test_call_device_playing_is_followed_from_a_non_comms_device():
    # Back on the Realtek jack after leaving the Shure; the Shure is playing
    # again (the call came back to the comms device).
    action, _ = _decide(held=REALTEK_LB, target=SHURE, comms=SHURE,
                        held_had_signal=True, silent_for=15.0)
    assert action == "switch"


def test_no_comms_device_known_just_follows_the_audio():
    action, _ = _decide(comms=None, held_had_signal=True, silent_for=15.0)
    assert action == "switch"
