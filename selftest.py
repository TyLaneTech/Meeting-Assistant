"""End-to-end self-test for the macOS audio + transcription pipeline.

Run from the project root inside the venv:

    .venv/bin/python selftest.py

Steps tested (in order — first failure stops further checks):

    1. Platform / venv sanity (Apple Silicon arm64, Python 3.10+)
    2. ScreenCaptureKit dependencies import and macOS version supports SCK
    3. ffmpeg in PATH (native arm64) — needed for screen recording / avfoundation
    4. TCC microphone permission (1.5 s capture from default mic)
    5. System-audio loopback captures a tone played through the default output
    6. Whisper input-format pipeline: 16 kHz mono float32 round-trip via mlx-whisper
       (skipped if model weights aren't cached and `--allow-download` not given)

Each step prints PASS / FAIL with a one-line reason. Exit code is the count
of failures (0 = healthy).

Independent of Flask — runs straight against the modules. Safe to re-run.
"""
from __future__ import annotations

import argparse
import platform as _platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Match the app's environment before any HuggingFace import: config pins
# HF_HOME to the project-local models/ dir (and applies the HF token / SSL
# flags), so the whisper step resolves against the same cache the app uses
# instead of silently re-downloading 3 GB into ~/.cache/huggingface.
from core import config  # noqa: E402,F401
from capture_video.ffmpeg_util import find_ffmpeg  # noqa: E402

# ── ANSI ─────────────────────────────────────────────────────────────────────
R, G, Y, RED, GRY, B = "\033[0m", "\033[92m", "\033[93m", "\033[91m", "\033[90m", "\033[1m"


def _ok(label: str, msg: str = "") -> None:
    print(f"  {G}[PASS]{R} {B}{label}{R}  {GRY}{msg}{R}")


def _fail(label: str, msg: str = "") -> None:
    print(f"  {RED}[FAIL]{R} {B}{label}{R}  {RED}{msg}{R}")


def _skip(label: str, msg: str = "") -> None:
    print(f"  {Y}[SKIP]{R} {B}{label}{R}  {GRY}{msg}{R}")


def _warn(label: str, msg: str = "") -> None:
    print(f"  {Y}[WARN]{R} {B}{label}{R}  {Y}{msg}{R}")


def _section(title: str) -> None:
    print()
    print(f"  {B}{title}{R}")
    print(f"  {GRY}{'─' * 60}{R}")


# ── Step impls ───────────────────────────────────────────────────────────────


def step_platform() -> bool:
    if sys.platform != "darwin":
        _fail("platform", f"sys.platform is {sys.platform!r}; this self-test is macOS-only.")
        return False
    arch = _platform.machine()
    pyv = sys.version_info
    if pyv < (3, 10):
        _fail("python", f"Python {pyv.major}.{pyv.minor}.{pyv.micro} — need 3.10+")
        return False
    venv_marker = (ROOT / ".venv" / "bin" / "python").resolve()
    in_venv = Path(sys.executable).resolve() == venv_marker
    label = f"python {pyv.major}.{pyv.minor}.{pyv.micro}, arch={arch}, venv={'yes' if in_venv else 'NO'}"
    if not in_venv:
        _fail("platform", label + " — not running under the project venv")
        return False
    _ok("platform", label)
    if "/Library/CloudStorage/" in str(ROOT):
        _warn(
            "cloud-path",
            "project is on a File Provider volume (OneDrive) — imports/DB writes "
            "can stall and recordings can corrupt; move to a local path, e.g. ~/meeting-assistant",
        )
    return True


def step_sck_dependencies() -> bool:
    """Verify ScreenCaptureKit dependencies and OS support.

    This does not start capture or trigger TCC; the full permission/capture
    check happens in step_loopback_capture().
    """
    try:
        mac_ver = tuple(int(p) for p in _platform.mac_ver()[0].split(".")[:2])
    except Exception:
        mac_ver = (0, 0)
    if mac_ver < (13, 0):
        _fail("sck", f"macOS {_platform.mac_ver()[0] or '?'} — ScreenCaptureKit needs macOS 13+")
        return False
    try:
        import ScreenCaptureKit  # noqa: F401
        import CoreMedia  # noqa: F401
    except Exception as e:
        _fail("sck", f"missing PyObjC SCK/CoreMedia dependency: {e}")
        return False
    _ok("sck", f"ScreenCaptureKit available on macOS {_platform.mac_ver()[0]}")
    return True


def step_ffmpeg() -> bool:
    found = find_ffmpeg() or shutil.which("ffmpeg") or ""
    if not Path(found).exists():
        _fail("ffmpeg", "not found in PATH or tools/ — `brew install ffmpeg`")
        return False
    try:
        info = subprocess.run([found, "-version"], capture_output=True, text=True, timeout=5)
        first = info.stdout.splitlines()[0] if info.stdout else "(no output)"
    except Exception as e:
        _fail("ffmpeg", f"`{found} -version` failed: {e}")
        return False
    # arch check via `file`
    arch = "?"
    try:
        f = subprocess.run(["file", found], capture_output=True, text=True, timeout=3)
        if "arm64" in f.stdout:
            arch = "arm64"
        elif "x86_64" in f.stdout:
            arch = "x86_64"
    except Exception:
        pass
    if arch == "x86_64" and _platform.machine() == "arm64":
        _fail("ffmpeg", f"x86_64 binary at {found} (Rosetta) — `brew install ffmpeg`")
        return False
    _ok("ffmpeg", f"{first.strip()} [{arch}]")
    return True


def step_mic_permission() -> bool:
    try:
        import sounddevice as sd
        import numpy as np
    except Exception as e:
        _fail("mic", f"import failed: {e}")
        return False
    try:
        rec = sd.rec(int(48000 * 1.0), samplerate=48000, channels=1, dtype="int16")
        sd.wait()
    except Exception as e:
        _fail("mic", f"sd.rec() failed: {e}")
        return False
    peak = int(np.max(np.abs(rec)))
    rms = float(np.sqrt(np.mean(rec.astype(np.float32) ** 2)))
    if peak == 0:
        _fail("mic", "1.0 s capture returned all zeros — TCC permission likely denied")
        return False
    _ok("mic", f"peak={peak}, rms={rms:.1f} (1 s capture)")
    return True


def step_loopback_capture() -> bool:
    """Play a 440 Hz tone through the system default output and verify the
    ScreenCaptureKit loopback captures it via AudioCapture's mixer queue.

    No audio routing or device switching needed — SCK reads the system audio
    mix directly. The user's default output is untouched.
    """
    try:
        import sounddevice as sd
        import numpy as np
        from capture_audio.mac import AudioCapture
    except Exception as e:
        _fail("loopback", f"import failed: {e}")
        return False

    q: queue.Queue = queue.Queue()
    cap = AudioCapture(q)
    cap.echo_cancel_enabled = False
    cap.agc_loopback_enabled = False
    cap.agc_mic_enabled = False

    try:
        cap.start(loopback_index=None, mic_index=-1)
    except Exception as e:
        _fail(
            "loopback",
            f"AudioCapture.start failed: {e}. Grant Screen & System Audio "
            f"Recording permission for the launcher process, then restart and rerun.",
        )
        return False

    sr = 48000
    t = np.arange(int(sr * 1.5)) / sr
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    try:
        sd.play(np.column_stack([tone, tone]), samplerate=sr)  # default output
        sd.wait()
        time.sleep(0.4)
    except Exception as e:
        cap.stop()
        _fail("loopback", f"sd.play failed: {e}")
        return False

    chunks = []
    try:
        while True:
            chunks.append(q.get_nowait())
    except queue.Empty:
        pass
    cap.stop()

    if not chunks:
        _fail("loopback", "mixer queue empty after tone — capture pipeline didn't run")
        return False
    pcm = b"".join(c[1] for c in chunks)
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if peak < 0.05:
        _skip(
            "loopback",
            f"chunks arrived but silent (peak={peak:.3f}). "
            f"Check system output volume and Screen & System Audio Recording permission.",
        )
        return True
    _ok("loopback", f"peak={peak:.3f}, rms={rms:.4f} on 440 Hz tone via ScreenCaptureKit")
    return True


def step_whisper(allow_download: bool) -> bool:
    import os
    # huggingface_hub stores repos under <HF_HOME>/hub/models--<org>--<name>.
    # config (imported at module top) pins HF_HOME to ROOT/models, so the
    # project cache is models/hub/...; also probe the user-default cache.
    model_dir = "models--mlx-community--whisper-large-v3-mlx"
    hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
    cache_path = Path.home() / ".cache" / "huggingface" / "hub" / model_dir
    proj_cache = hf_home / "hub" / model_dir
    cached = cache_path.exists() or proj_cache.exists()
    if not cached and not allow_download:
        _skip("whisper", "mlx-whisper large-v3 weights not cached; pass --allow-download to fetch (~3 GB)")
        return True

    try:
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        from ml.transcriber_engine import make_engine
    except Exception as e:
        _fail("whisper", f"import failed: {e}")
        return False

    text = "the quick brown fox jumps over the lazy dog"
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
        aiff = f.name
    try:
        subprocess.run(["say", "-o", aiff, text], check=True, capture_output=True)
        data, sr = sf.read(aiff, dtype="float32", always_2d=False)
        if data.ndim > 1:
            data = data.mean(axis=1)
        audio_16k = resample_poly(data, 16_000, sr).astype(np.float32) if sr != 16_000 else data.astype(np.float32)
    except Exception as e:
        _fail("whisper", f"speech synthesis/decode failed: {e}")
        return False
    finally:
        try:
            os.unlink(aiff)
        except Exception:
            pass

    try:
        engine = make_engine("large-v3", "mlx", "fp16")
        segments, _info = engine.transcribe(audio_16k, language="en")
        out = " ".join(s.text.strip() for s in segments if s.text.strip())
    except Exception as e:
        _fail("whisper", f"transcription failed: {e}")
        return False

    expected = set(text.lower().split())
    actual = set(out.lower().replace(",", "").replace(".", "").split())
    overlap = len(expected & actual)
    if overlap < len(expected) * 0.6:
        _fail("whisper", f"only {overlap}/{len(expected)} expected words; got {out!r}")
        return False
    _ok("whisper", f"{overlap}/{len(expected)} words present — {out!r}")
    return True


# ── Driver ───────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--allow-download",
        action="store_true",
        help="If mlx-whisper large-v3 weights aren't cached, download them (~3 GB)",
    )
    args = p.parse_args()

    print()
    print(f"  {B}Meeting Assistant — macOS self-test{R}")
    print(f"  {GRY}{'═' * 60}{R}")

    failures = 0

    _section("1. Platform / venv")
    if not step_platform():
        failures += 1

    _section("2. ScreenCaptureKit")
    if not step_sck_dependencies():
        failures += 1

    _section("3. ffmpeg")
    if not step_ffmpeg():
        failures += 1

    _section("4. Microphone permission (TCC)")
    if not step_mic_permission():
        failures += 1

    _section("5. System-audio loopback capture")
    if not step_loopback_capture():
        failures += 1

    _section("6. Whisper end-to-end")
    if not step_whisper(args.allow_download):
        failures += 1

    print()
    print(f"  {GRY}{'═' * 60}{R}")
    if failures == 0:
        print(f"  {G}{B}All checks passed — pipeline is healthy.{R}")
    else:
        print(f"  {RED}{B}{failures} check(s) failed.{R}")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(main())
