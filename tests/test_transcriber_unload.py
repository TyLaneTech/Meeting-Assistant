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
    calls = {"empty_cache": 0, "is_available": 0}
    orig_empty = torch.cuda.empty_cache
    orig_avail = torch.cuda.is_available
    orig_init = torch.cuda.is_initialized
    torch.cuda.empty_cache = lambda: calls.__setitem__("empty_cache", calls["empty_cache"] + 1)
    torch.cuda.is_available = lambda: calls.__setitem__("is_available", calls["is_available"] + 1) or True
    torch.cuda.is_initialized = lambda: True   # this process used CUDA
    try:
        t = Transcriber(queue.Queue(), lambda *a, **k: None)
        t.model = object()       # stand in for a loaded Whisper engine
        t.diarizer = object()    # stand in for a loaded StreamingDiarizer
        t.unload()
        assert t.model is None, "model should be None after unload"
        assert t.diarizer is None, "diarizer should be None after unload"
        assert calls["empty_cache"] >= 1, "empty_cache should be called when CUDA is in use"
        # idempotent: a second call must not raise
        t.unload()
    finally:
        torch.cuda.empty_cache = orig_empty
        torch.cuda.is_available = orig_avail
        torch.cuda.is_initialized = orig_init


def test_unload_never_initializes_cuda_in_a_cpu_process():
    """A process that never used CUDA must not touch it on unload: asking
    torch.cuda.is_available() initializes the driver, which can keep the
    NVIDIA card powered on a hybrid-graphics laptop."""
    calls = {"empty_cache": 0, "is_available": 0}
    orig_empty = torch.cuda.empty_cache
    orig_avail = torch.cuda.is_available
    orig_init = torch.cuda.is_initialized
    torch.cuda.empty_cache = lambda: calls.__setitem__("empty_cache", calls["empty_cache"] + 1)
    torch.cuda.is_available = lambda: calls.__setitem__("is_available", calls["is_available"] + 1) or True
    torch.cuda.is_initialized = lambda: False
    try:
        t = Transcriber(queue.Queue(), lambda *a, **k: None)
        t.model = object()
        t.diarizer = object()
        t.unload()
        assert calls == {"empty_cache": 0, "is_available": 0}, calls
    finally:
        torch.cuda.empty_cache = orig_empty
        torch.cuda.is_available = orig_avail
        torch.cuda.is_initialized = orig_init


def test_a_gpu_memory_error_never_deletes_the_model_cache():
    """With the idle unload the model loads on every wake, not only at startup,
    and any load error used to delete the model's cache before retrying. Out of
    GPU memory (another app took the VRAM the unload freed) says nothing about
    the files, and the offline runtime could not fetch them again."""
    import ml.transcriber_engine as engine
    orig_make = engine.make_engine
    cleared = []
    try:
        for message, should_clear in (
                ("CUDA failed with error out of memory", False),
                ("cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED", False),
                ("Unable to open file 'model.bin' in model 'large-v3'", True)):
            def _fail(*_a, _m=message, **_k):
                raise RuntimeError(_m)
            engine.make_engine = _fail
            t = Transcriber(queue.Queue(), lambda *a, **k: None)
            t._auto_model_config = False
            t._clear_bad_model_cache = lambda msg: cleared.append(msg) or False
            try:
                t.load_model()
            except RuntimeError:
                pass
            else:
                raise AssertionError("a failed load must raise")
            assert (message in cleared) == should_clear, (message, cleared)
    finally:
        engine.make_engine = orig_make


if __name__ == "__main__":
    test_unload_nulls_models_and_empties_cache()
    test_unload_never_initializes_cuda_in_a_cpu_process()
    test_a_gpu_memory_error_never_deletes_the_model_cache()
    print("OK test_transcriber_unload")
