"""Standalone test: Transcriber.unload() releases models and empties CUDA cache.
Run: .venv/bin/python tests/test_transcriber_unload.py
"""
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from ml.transcriber import Transcriber


def test_unload_nulls_models_and_empties_cache():
    calls = {"empty_cache": 0}
    orig_empty = torch.cuda.empty_cache
    orig_avail = torch.cuda.is_available
    torch.cuda.empty_cache = lambda: calls.__setitem__("empty_cache", calls["empty_cache"] + 1)
    torch.cuda.is_available = lambda: True
    try:
        t = Transcriber(queue.Queue(), lambda *a, **k: None)
        t.model = object()       # stand in for a loaded Whisper engine
        t.diarizer = object()    # stand in for a loaded StreamingDiarizer
        t.unload()
        assert t.model is None, "model should be None after unload"
        assert t.diarizer is None, "diarizer should be None after unload"
        assert calls["empty_cache"] >= 1, "empty_cache should be called when CUDA available"
        # idempotent: a second call must not raise
        t.unload()
    finally:
        torch.cuda.empty_cache = orig_empty
        torch.cuda.is_available = orig_avail


if __name__ == "__main__":
    test_unload_nulls_models_and_empties_cache()
    print("OK test_transcriber_unload")
