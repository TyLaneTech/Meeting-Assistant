"""Idle clock policy for the ML stack: when do the models get dropped?
Run: .venv/Scripts/python tests/test_ml_idle.py  (or pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ml_idle import IdleClock


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_not_due_before_threshold():
    fc = FakeClock()
    clock = IdleClock(now=fc)
    fc.t += 19 * 60
    assert not clock.unload_due(20, busy=False, ready=True, waking=False)


def test_due_at_threshold():
    fc = FakeClock()
    clock = IdleClock(now=fc)
    fc.t += 20 * 60
    assert clock.unload_due(20, busy=False, ready=True, waking=False)


def test_busy_resets_clock_and_never_due():
    fc = FakeClock()
    clock = IdleClock(now=fc)
    fc.t += 60 * 60
    assert not clock.unload_due(20, busy=True, ready=True, waking=False)
    # the busy tick stamped the clock: a minute later we are 1 min idle, not 61
    fc.t += 60
    assert round(clock.idle_seconds()) == 60
    assert not clock.unload_due(20, busy=False, ready=True, waking=False)


def test_zero_or_negative_minutes_disables():
    fc = FakeClock()
    clock = IdleClock(now=fc)
    fc.t += 24 * 3600
    assert not clock.unload_due(0, busy=False, ready=True, waking=False)
    assert not clock.unload_due(-5, busy=False, ready=True, waking=False)


def test_not_due_when_models_not_ready_or_waking():
    fc = FakeClock()
    clock = IdleClock(now=fc)
    fc.t += 60 * 60
    assert not clock.unload_due(20, busy=False, ready=False, waking=False)
    assert not clock.unload_due(20, busy=False, ready=True, waking=True)


def test_touch_resets():
    fc = FakeClock()
    clock = IdleClock(now=fc)
    fc.t += 30 * 60
    clock.touch()
    assert clock.idle_seconds() == 0
    assert not clock.unload_due(20, busy=False, ready=True, waking=False)


def test_idle_minutes_rounding():
    fc = FakeClock()
    clock = IdleClock(now=fc)
    fc.t += 21 * 60 + 20
    assert clock.idle_minutes() == 21


# ── app.py wiring (source-level: importing app.py loads the models) ──────────
# The unload costs a recording the seconds its reload takes (the capture opens
# once the models are back), so it is opt-in, and a failed reload must never
# leave Record disabled until a restart (review of PR 1086, 2026-10-07).

import re  # noqa: E402

APP_PY = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "app.py"), encoding="utf-8").read()


def _function_body(name: str) -> str:
    m = re.search(rf"^def {name}\(.*?\n(.*?)(?=^def |^\S|\Z)", APP_PY, re.M | re.S)
    assert m, f"{name} not found"
    return m.group(1)


def test_idle_unload_is_opt_in():
    from core import settings
    assert settings.DEFAULTS["ml_idle_unload_minutes"] == 0
    assert 'settings.get("ml_idle_unload_minutes", 0)' in _function_body("_idle_unload_tick")


def test_a_failed_wake_leaves_the_models_asleep_and_record_live():
    wake = _function_body("_wake_ml")
    failed = wake[wake.index('if _state["model_ready"]:'):]
    assert '_state["ml_sleeping"] = True' in failed
    prereqs = _function_body("_recording_prereqs_locked")
    asleep = prereqs[prereqs.index('if _state["ml_sleeping"] and not _state["model_ready"]:'):]
    assert asleep.index("return True") < asleep.index('if not _state["model_ready"]:')


def test_a_wake_answers_before_the_browser_and_coordinator_give_up():
    timeout = float(re.search(r"^_ML_WAKE_TIMEOUT_SEC = ([\d.]+)", APP_PY, re.M).group(1))
    assert timeout < 45, "the start coordinator opens a second window after 45 s"
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "ui_web", "static", "app.js"), encoding="utf-8").read()
    patience = int(re.search(r"RECORD_START_PATIENCE_MS = (\d+)", js).group(1))
    assert timeout * 1000 < patience


def test_the_sweep_rechecks_the_idle_clock_under_the_lock():
    tick = _function_body("_idle_unload_tick")
    locked = tick[tick.index("with _state_lock:", tick.index("idle_min =")):]
    before_flip = locked[:locked.index('_state["model_ready"] = False')]
    assert "_ml_busy_locked()" in before_flip and "_ml_idle.unload_due(" in before_flip


def test_a_wake_reloads_the_model_chosen_since_startup():
    assert 'settings.get("whisper_preset"' in _function_body("_load_model")
    assert "_saved_whisper_preset" not in APP_PY


def test_meeting_detection_wakes_the_models_once_per_meeting():
    loop = _function_body("_meeting_detect_loop")
    wake = loop.index('_wake_ml("meeting detected")')
    assert "if consecutive == 1 and not _transcribe_after_enabled():" in loop[wake - 400:wake]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK test_ml_idle")
