"""Microphone stretches with no speech are not transcribed.

Regression 2026-10-07: on a 1 h 49 m meeting with the owner's mic muted most of
the time, energy segmentation passed breathing and room noise to Whisper, which
filled it with 181 made-up lines ("Thank you.", "I'll see you next time.").
BatchTranscriber._keep_spoken now drops mic spans the speech detector hears no
speech in.

Run: .venv/Scripts/python -m pytest tests/test_mic_speech_check.py
"""
from __future__ import annotations

import numpy as np
import pytest

from capture_audio.params import get_reanalysis_defaults
from ml import batch_transcriber as bt
from ml.batch_transcriber import BatchTranscriber, speech_fraction

SR = bt.TARGET_RATE


def test_speech_fraction():
    regions = [(1.0, 2.0), (3.0, 3.5)]
    assert speech_fraction(0.0, 4.0, regions) == pytest.approx(0.375)
    assert speech_fraction(2.0, 3.0, regions) == 0.0
    assert speech_fraction(1.2, 1.8, regions) == 1.0
    assert speech_fraction(5.0, 5.0, regions) == 0.0


def test_spans_without_speech_are_dropped(monkeypatch):
    import faster_whisper.vad as vad
    # Speech from 10 s to 12 s only.
    monkeypatch.setattr(vad, "get_speech_timestamps",
                        lambda audio, opts, sampling_rate=SR: [{"start": 10 * SR, "end": 12 * SR}])
    spans = [(1.0, 2.0), (9.5, 12.5), (11.9, 14.0), (20.0, 21.0)]
    kept = BatchTranscriber._keep_spoken(np.zeros(30 * SR, dtype=np.float32), spans)
    assert kept == [(9.5, 12.5)], "a span is kept when speech covers 10% or more of it"


def test_a_missing_detector_keeps_every_span(monkeypatch):
    import faster_whisper.vad as vad

    def boom(*a, **k):
        raise RuntimeError("no model")
    monkeypatch.setattr(vad, "get_speech_timestamps", boom)
    spans = [(1.0, 2.0), (3.0, 4.0)]
    assert BatchTranscriber._keep_spoken(np.zeros(5 * SR, dtype=np.float32), spans) == spans


def test_the_real_detector_hears_no_speech_in_room_noise():
    pytest.importorskip("faster_whisper")
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(6 * SR) * 0.01).astype(np.float32)   # above the energy gate
    spans = BatchTranscriber._energy_segments(noise, 0.008)
    assert spans, "the energy gate alone passes this noise to Whisper"
    assert BatchTranscriber._keep_spoken(noise, spans) == []


def test_the_check_is_on_by_default():
    assert get_reanalysis_defaults()["reanalysis_mic_speech_check"] == 1
