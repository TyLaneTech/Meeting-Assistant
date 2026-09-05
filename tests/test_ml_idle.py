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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK test_ml_idle")
