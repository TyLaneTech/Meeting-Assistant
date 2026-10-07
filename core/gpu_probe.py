"""Which accelerators does this machine have? Answered by a short-lived child
process, so the app process itself never initializes the NVIDIA driver.

A process that touches CUDA keeps a handle on the card until it exits, and on
a hybrid-graphics laptop that can keep the NVIDIA GPU powered all day. The
long-lived app asks this module instead: the first question starts
``python -m core.gpu_probe``, which runs the real checks, prints one JSON line
and exits. The answer is cached for the life of the app process.

The checks run in-process instead (no child) on macOS, where there is no
NVIDIA card to keep awake, and in any process started with
MA_GPU_PROBE_INPROCESS=1: the batch worker child sets it, because that
process is about to use the GPU anyway and exits when the job ends.

``load_cublas()`` is meant for the app process: Whisper's engine calls it
just before it loads on the GPU (see its docstring for why it must).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROBE_TIMEOUT_SEC = 120.0

_lock = threading.Lock()
_done = threading.Event()
_started = False
_result: dict | None = None

_cublas_lock = threading.Lock()
_cublas = None  # the ctypes handle; holding it keeps the library loaded


def _register_nvidia_dll_dirs() -> None:
    # nvidia-cublas-cu12 / nvidia-cudnn-cu12 put their DLLs under
    # site-packages/nvidia/*/bin, which is not on the DLL search path.
    if sys.platform != "win32":
        return
    import glob
    import site
    try:
        for sp in site.getsitepackages():
            for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
                if os.path.isdir(d):
                    os.add_dll_directory(d)
    except Exception:
        pass


def load_cublas() -> bool:
    """Load cuBLAS into this process. True once it is loaded.

    The app process must call this before Whisper's first GPU call.
    CTranslate2 loads cublas64_12.dll itself on that call, with a search that
    skips the site-packages/nvidia/*/bin folders, so on its own the load
    fails; after that failure every later Whisper call on the GPU in the
    process hangs, even with a new model. A library already loaded is found
    by name, and ctypes does search the folders os.add_dll_directory()
    registered. The in-process GPU check did this as a side effect, so moving
    the check to a child process left nothing doing it."""
    global _cublas
    with _cublas_lock:
        if _cublas is not None:
            return True
        _register_nvidia_dll_dirs()
        import ctypes
        for ver in ("12", "13", "11"):
            lib = f"cublas64_{ver}.dll" if sys.platform == "win32" else f"libcublas.so.{ver}"
            try:
                _cublas = ctypes.CDLL(lib)
                return True
            except OSError:
                continue
        return False


def _ct2_cuda_inprocess() -> bool:
    """Whether CUDA is usable for ctranslate2 (faster-whisper)."""
    try:
        import ctranslate2
        types = ctranslate2.get_supported_compute_types("cuda")
        if types and ctranslate2.get_cuda_device_count() > 0:
            return load_cublas()
    except Exception:
        pass
    return False


def _torch_device_inprocess() -> str:
    """The best torch device: 'cuda', 'mps', or 'cpu'."""
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _probe_inprocess() -> dict:
    _register_nvidia_dll_dirs()
    return {"ct2_cuda": _ct2_cuda_inprocess(), "torch_device": _torch_device_inprocess()}


def _inprocess_allowed() -> bool:
    return sys.platform == "darwin" or os.environ.get("MA_GPU_PROBE_INPROCESS") == "1"


def _probe_child() -> dict:
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    env = dict(os.environ)
    env["MA_GPU_PROBE_INPROCESS"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "core.gpu_probe"],
        cwd=str(_ROOT), env=env, capture_output=True, text=True,
        timeout=_PROBE_TIMEOUT_SEC, creationflags=flags,
    )
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            data = json.loads(line)
            return {"ct2_cuda": bool(data.get("ct2_cuda")),
                    "torch_device": str(data.get("torch_device") or "cpu")}
    raise RuntimeError(f"probe exited {proc.returncode}: {(proc.stderr or '').strip()[-400:]}")


def _run() -> None:
    global _result
    try:
        if _inprocess_allowed():
            res = _probe_inprocess()
        else:
            res = _probe_child()
        try:
            from core import log
            log.info("gpu", f"Accelerators: torch={res['torch_device']}, "
                            f"whisper CUDA={'yes' if res['ct2_cuda'] else 'no'}"
                            + ("" if _inprocess_allowed() else " (checked in a child process)"))
        except Exception:
            pass
    except Exception as e:
        # The child failed or timed out. Its answer is cached for the life of
        # the app, and "cpu" would put live Whisper on the processor for the
        # whole session, which is far worse than this process touching the
        # driver (it does anyway once Whisper loads on CUDA). Ask here instead.
        try:
            res = _probe_inprocess()
            note = (f"GPU check in a child process failed ({e}); checked in this "
                    f"process instead: torch={res['torch_device']}, "
                    f"whisper CUDA={'yes' if res['ct2_cuda'] else 'no'}")
        except Exception as e2:
            res = {"ct2_cuda": False, "torch_device": "cpu"}
            note = f"GPU check failed ({e}; {e2}), treating this machine as CPU only"
        try:
            from core import log
            log.warn("gpu", note)
        except Exception:
            pass
    _result = res
    _done.set()


def start() -> None:
    """Begin the check in the background (idempotent). The app calls this at
    startup so the first caller rarely waits."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_run, daemon=True, name="gpu-probe").start()


def result(timeout: float = _PROBE_TIMEOUT_SEC + 10) -> dict:
    """The cached answer, waiting for the first check if it is still running."""
    start()
    if not _done.wait(timeout):
        return {"ct2_cuda": False, "torch_device": "cpu"}
    return dict(_result or {"ct2_cuda": False, "torch_device": "cpu"})


def ct2_cuda() -> bool:
    return bool(result()["ct2_cuda"])


def torch_device() -> str:
    return str(result()["torch_device"])


def _reset_for_tests() -> None:
    global _started, _result
    with _lock:
        _started = False
        _result = None
        _done.clear()


if __name__ == "__main__":
    print(json.dumps(_probe_inprocess()), flush=True)
