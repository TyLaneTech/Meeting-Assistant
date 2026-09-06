"""Standalone test: SpeakerFingerprintDB.unload() releases the embedding model.
Run: .venv/bin/python tests/test_fingerprint_unload.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.speaker_db import SpeakerFingerprintDB


def test_unload_releases_inference():
    # Bypass __init__ (which touches the DB / HF) - exercise unload() in isolation.
    db = SpeakerFingerprintDB.__new__(SpeakerFingerprintDB)
    db._inference = object()   # stand in for a loaded Inference model
    db._ready = True
    db.unload()
    assert db._inference is None, "_inference should be None after unload"
    assert db._ready is False, "_ready should be False after unload"
    db.unload()  # idempotent


if __name__ == "__main__":
    test_unload_releases_inference()
    print("OK test_fingerprint_unload")
