"""Whisper's first run on the GPU in the app process.

Regression 2026-10-07: with the GPU check moved to a child process, nothing
loaded cuBLAS into the app process any more. CTranslate2 then failed to load
cublas64_12.dll on its own during the warm-up, which swallowed the error, and
the first real transcription hung for good: the mic was recorded and metered,
but nothing was transcribed live.

Run: .venv/Scripts/python -m pytest tests/test_whisper_gpu_start.py
"""
import os
import queue
import subprocess
import sys
import textwrap
import time

import pytest

from core import gpu_probe, log
import ml.transcriber as transcriber
import ml.transcriber_engine as engine
from ml.transcriber import Transcriber

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CUBLAS_ERROR = "Library cublas64_12.dll is not found or cannot be loaded"


@pytest.fixture(autouse=True)
def _keep_out_of_the_app_log(monkeypatch):
    # These tests log fake GPU failures; keep them out of the live app.log.
    monkeypatch.setattr(log, "_capture", lambda *a, **k: None)


# ── Loading cuBLAS ────────────────────────────────────────────────────────────

def test_load_cublas_keeps_the_first_version_that_loads(monkeypatch):
    import ctypes
    tried = []

    def _cdll(name):
        tried.append(name)
        if "12" in name:
            raise OSError(f"{name} not found")
        return object()

    monkeypatch.setattr(gpu_probe, "_cublas", None)
    monkeypatch.setattr(gpu_probe, "_register_nvidia_dll_dirs", lambda: None)
    monkeypatch.setattr(ctypes, "CDLL", _cdll)
    assert gpu_probe.load_cublas() is True
    assert len(tried) == 2 and "13" in tried[1], tried
    assert gpu_probe.load_cublas() is True
    assert len(tried) == 2, "a loaded library is kept, not loaded again"


def test_load_cublas_says_no_when_nothing_loads(monkeypatch):
    import ctypes

    def _cdll(name):
        raise OSError(f"{name} not found")

    monkeypatch.setattr(gpu_probe, "_cublas", None)
    monkeypatch.setattr(gpu_probe, "_register_nvidia_dll_dirs", lambda: None)
    monkeypatch.setattr(ctypes, "CDLL", _cdll)
    assert gpu_probe.load_cublas() is False


def test_the_gpu_check_answers_with_the_same_loader(monkeypatch):
    """The child's "whisper CUDA=yes" holds for the app process only if both
    load cuBLAS the same way."""
    ctranslate2 = pytest.importorskip("ctranslate2")
    calls = []
    monkeypatch.setattr(ctranslate2, "get_supported_compute_types", lambda device: {"float16"})
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(gpu_probe, "load_cublas", lambda: calls.append("cublas") or False)
    assert gpu_probe._ct2_cuda_inprocess() is False
    assert calls == ["cublas"]


@pytest.mark.skipif(sys.platform != "win32", reason="the DLL search is a Windows problem")
@pytest.mark.parametrize("device, cublas_loads, expected", [
    ("cuda", True, ["cublas", "model"]),
    ("cpu", True, ["model"]),
    ("cuda", False, ["cublas"]),
])
def test_the_engine_loads_cublas_before_a_gpu_model_exists(monkeypatch, device,
                                                           cublas_loads, expected):
    faster_whisper = pytest.importorskip("faster_whisper")
    events = []

    class _Model:
        def __init__(self, *a, **k):
            events.append("model")

    def _load():
        events.append("cublas")
        return cublas_loads

    monkeypatch.setattr(faster_whisper, "WhisperModel", _Model)
    monkeypatch.setattr(gpu_probe, "load_cublas", _load)
    if cublas_loads:
        engine.FasterWhisperEngine("large-v3", device, "float16")
    else:
        with pytest.raises(RuntimeError, match="cuBLAS"):
            engine.FasterWhisperEngine("large-v3", device, "float16")
    assert events == expected


# ── A failed first run on the GPU ─────────────────────────────────────────────

class _Engine:
    def __init__(self, size, device, fail_warmup):
        self.size, self.device, self._fail = size, device, fail_warmup

    def transcribe(self, audio, **kwargs):
        if self._fail:
            raise RuntimeError(CUBLAS_ERROR)
        return iter(()), {}


def _fake_make_engine(made, *, gpu_fails=True, small_on_disk=False):
    def _make(size, device, compute_type):
        made.append((size, device))
        if device == "cpu" and size == "small" and not small_on_disk:
            raise RuntimeError("Cannot find an appropriate cached snapshot folder")
        return _Engine(size, device, fail_warmup=(device == "cuda" and gpu_fails))
    return _make


def _gpu_transcriber():
    t = Transcriber(queue.Queue(), lambda *a, **k: None)
    t._auto_model_config = False
    t.device, t.compute_type, t.model_size = "cuda", "float16", "large-v3"
    return t


def test_a_failed_first_gpu_run_moves_whisper_to_the_cpu(monkeypatch):
    """The warm-up swallowed this error, so the next call, the first real one,
    hung and live transcription stopped with no error."""
    made = []
    monkeypatch.setattr(transcriber, "_GPU_FAILED", False)
    monkeypatch.setattr(engine, "make_engine", _fake_make_engine(made))
    t = _gpu_transcriber()
    t.load_model()
    assert (t.device, t.compute_type) == ("cpu", "int8")
    assert t.model.device == "cpu"
    # "small" is not downloaded (the launcher fetches only large-v3), so the
    # fallback uses the model that was already on disk.
    assert t.model_size == "large-v3"
    assert made == [("large-v3", "cuda"), ("small", "cpu"), ("large-v3", "cpu")]


def test_later_loads_in_the_same_run_stay_on_the_cpu(monkeypatch):
    """After a failed first GPU run, CTranslate2's GPU calls hang even on a new
    model, so a reload (a wake after the idle unload, a preset change) must not
    try the GPU again until the app restarts."""
    made = []
    monkeypatch.setattr(transcriber, "_GPU_FAILED", False)
    monkeypatch.setattr(engine, "make_engine", _fake_make_engine(made, small_on_disk=True))
    t = _gpu_transcriber()
    t.load_model()
    made.clear()
    t.reload_model("cuda", "float16", "large-v3")
    assert ("large-v3", "cuda") not in made, made
    assert (t.device, t.model.device) == ("cpu", "cpu")
    assert t.model_size == "small", "a downloaded small model is still preferred"


def test_a_good_first_gpu_run_stays_on_the_gpu(monkeypatch):
    made = []
    monkeypatch.setattr(transcriber, "_GPU_FAILED", False)
    monkeypatch.setattr(engine, "make_engine", _fake_make_engine(made, gpu_fails=False))
    t = _gpu_transcriber()
    t.load_model()
    assert (t.device, t.model.device) == ("cuda", "cuda")
    assert made == [("large-v3", "cuda")]
    assert transcriber._GPU_FAILED is False


# ── The real thing ────────────────────────────────────────────────────────────

_FRESH_APP_PROCESS = textwrap.dedent("""
    import pathlib, sys, tempfile, time
    sys.path.insert(0, {root!r})
    from core import paths
    paths._cached = pathlib.Path(tempfile.mkdtemp())  # keep out of the app log
    import numpy as np
    from ml.transcriber import Transcriber
    t = Transcriber(None, lambda *a, **k: None)
    t.load_model()                     # the GPU check runs in a child, as in the app
    print("device", t.device, flush=True)
    noise = (np.random.default_rng(0).standard_normal(16000 * 5) * 0.1).astype("float32")
    start = time.monotonic()
    segs, _ = t.model.transcribe(noise, language="en", vad_filter=False)
    list(segs)
    print(f"second call returned in {{time.monotonic() - start:.1f}}s", flush=True)
""")


def _cuda_whisper_available() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


@pytest.mark.skipif(sys.platform != "win32" or not _cuda_whisper_available(),
                    reason="needs Windows and an NVIDIA GPU")
def test_whisper_runs_on_the_gpu_in_a_fresh_app_process():
    model_dir = os.path.join(ROOT, "storage", "models", "hub",
                             "models--Systran--faster-whisper-large-v3")
    if not os.path.isdir(model_dir):
        pytest.skip("faster-whisper large-v3 is not downloaded")
    env = dict(os.environ, HF_HOME=os.path.join(ROOT, "storage", "models"),
               HF_HUB_OFFLINE="1")
    env.pop("MA_GPU_PROBE_INPROCESS", None)
    proc = subprocess.Popen([sys.executable, "-c", _FRESH_APP_PROCESS.format(root=ROOT)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        out, _ = proc.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        # The venv's python.exe starts the real interpreter as a child, so end
        # the whole tree; a hung CUDA process would otherwise hold the GPU.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        out, _ = proc.communicate()
        pytest.fail("Whisper hung on the GPU in a fresh process:\n" + out[-2000:])
    assert proc.returncode == 0, out[-2000:]
    assert "device cuda" in out, "Whisper fell back to the CPU:\n" + out[-2000:]
    assert "second call returned" in out, out[-2000:]
