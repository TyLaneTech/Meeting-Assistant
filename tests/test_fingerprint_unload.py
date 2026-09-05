"""Standalone test: SpeakerFingerprintDB.unload() drops the embedding model but
keeps the library usable; extract_embedding() reloads the model on demand.
Run: .venv/Scripts/python tests/test_fingerprint_unload.py  (or pytest)
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.speaker_db import SpeakerFingerprintDB


def _bare_db():
    # Bypass __init__ (which touches the DB / HF) - exercise the model
    # lifecycle in isolation.
    db = SpeakerFingerprintDB.__new__(SpeakerFingerprintDB)
    db._inference = object()   # stand in for a loaded Inference model
    db._ready = True
    db._hf_token = "token"
    db._device = "cpu"
    return db


def test_unload_drops_model_but_keeps_library_ready():
    db = _bare_db()
    db.unload()
    assert db._inference is None, "_inference should be None after unload"
    assert db.ready is True, "library stays usable; only the model is dropped"
    assert db.model_loaded is False
    db.unload()  # idempotent


def test_extract_reloads_model_on_demand():
    db = _bare_db()
    db.unload()
    loads = []

    class FakeInference:
        def __call__(self, inp):
            return np.ones(256, dtype=np.float32)

    def fake_load():
        loads.append(1)
        db._inference = FakeInference()
        return True

    db._load_inference = fake_load
    emb = db.extract_embedding(np.zeros(16000, dtype=np.float32))
    assert emb is not None and emb.shape == (256,)
    assert loads == [1], "the model reloads exactly once, on first use"
    db.extract_embedding(np.zeros(16000, dtype=np.float32))
    assert loads == [1], "a loaded model is reused, not reloaded"


def test_extract_degrades_to_none_when_reload_fails():
    db = _bare_db()
    db.unload()
    db._load_inference = lambda: False
    assert db.extract_embedding(np.zeros(16000, dtype=np.float32)) is None


def test_ensure_model_is_noop_when_loaded():
    db = _bare_db()
    db._load_inference = lambda: (_ for _ in ()).throw(AssertionError("must not reload"))
    assert db.ensure_model() is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK test_fingerprint_unload")
