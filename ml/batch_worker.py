"""Run the batch reanalysis pipeline in a child process that exits afterwards.

Why a child: a process that has used CUDA holds the NVIDIA card until it
exits (torch.cuda.empty_cache() does not release it), and the app process
lives all day. Running the post-meeting transcription and every reanalysis in
``python -m ml.batch_worker`` lets the card power down the moment the job is
done, and returns the batch models' memory to the system as well.

Protocol. The parent writes one pickled job dict to the child's stdin. The
child answers on its stdout with length-prefixed pickled frames:
    ("text", (text, speaker, start, end))      a transcript segment
    ("fp", (speaker, audio, start, end))       audio for voice fingerprinting
    ("progress", fraction)
    ("devices", {"diarizer", "whisper"})       the devices the child actually resolved
    ("log", (level, tag, message))             core.log lines, re-logged by the parent
    ("done", info) | ("error", (type_name, message))
Anything else the child prints (library warnings, tracebacks) goes to its
stderr, which the parent copies to its own stderr. The child points its
stdout at stderr before importing anything, so a stray print cannot corrupt
the frame stream.

Cancelling (a recording started while a post-meeting pass runs) kills the
child: no segment is delivered after the cancel event is set, and the GPU is
released at once instead of at the next checkpoint.
"""
from __future__ import annotations

import collections
import os
import pickle
import struct
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable

_HDR = struct.Struct("<I")
_ROOT = Path(__file__).resolve().parent.parent


def _write_frame(stream, kind: str, payload) -> None:
    data = pickle.dumps((kind, payload), protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(_HDR.pack(len(data)))
    stream.write(data)
    stream.flush()


def _read_exact(stream, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_frame(stream):
    hdr = _read_exact(stream, _HDR.size)
    if hdr is None:
        return None
    data = _read_exact(stream, _HDR.unpack(hdr)[0])
    if data is None:
        return None
    return pickle.loads(data)


# ── Parent side ──────────────────────────────────────────────────────────────

# Every worker running now. os._exit ends the app's own threads but not a child
# process, so without this a Quit, Restart or Update mid-pass left the worker
# running on (GPU or CPU, for as long as one pyannote call takes: minutes), and
# after a restart it competed for VRAM with the same pass started again.
_live: set = set()
_live_lock = threading.Lock()


def _kill_tree(proc: subprocess.Popen) -> None:
    """End a worker and everything it started. Never raises.

    On Windows proc is the venv launcher, the interpreter its child, and the
    ffmpeg that decodes a per-source track the interpreter's child. Killing the
    launcher takes the interpreter with it but not ffmpeg, which kept decoding
    into audio/ after a cancel; taskkill /T walks the whole tree."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def kill_all() -> int:
    """End every running batch worker and its children. Returns how many there
    were. The app calls this on every exit path, before it rolls back."""
    with _live_lock:
        procs = list(_live)
    for proc in procs:
        _kill_tree(proc)
    return len(procs)


def run_in_child(
    wav_path: str,
    params: dict,
    on_text: Callable,
    on_fingerprint: Callable | None,
    on_progress: Callable[[float], None] | None,
    cancel_event: "threading.Event | None" = None,
    cuda: bool = True,
    tracks_root: str | None = None,
    on_devices: Callable[[dict], None] | None = None,
) -> None:
    """Run BatchTranscriber.process_wav_file(wav_path, params, tracks_root)
    in a child process, delivering its callbacks on the calling thread.
    ``tracks_root`` is where the per-source tracks live (media.tracks_root):
    the WAV can be a decode in tmp/, so the tracks cannot be found from it.
    ``on_devices`` gets {"diarizer", "whisper"} once the child has resolved
    them; a GPU the plan asked for that the child cannot use shows up there
    as "cpu".

    cuda=False hides the NVIDIA card from the child (CUDA_VISIBLE_DEVICES=-1)
    for a job planned entirely on the CPU. Measured 2026-10-01: merely
    importing pyannote and transformers wakes the card for about 10 s, and the
    process exit wakes it again; with the card hidden it stays powered down.

    Raises ReanalysisCancelled once cancel_event is set (the child is killed),
    ImportError when the child lacks the batch dependencies (so the caller's
    real-time fallback still works), and RuntimeError for any other failure.
    """
    from core import log
    from ml.batch_transcriber import ReanalysisCancelled

    env = dict(os.environ)
    # The child is about to use the GPU (or not) and exits afterwards, so it
    # may check for CUDA in-process instead of spawning its own probe.
    env["MA_GPU_PROBE_INPROCESS"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if not cuda:
        env["CUDA_VISIBLE_DEVICES"] = "-1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "ml.batch_worker"],
        cwd=str(_ROOT), env=env, creationflags=flags,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    with _live_lock:
        _live.add(proc)
    log.info("reanalysis", f"Batch worker started (pid {proc.pid}"
                           + (")" if cuda else ", NVIDIA GPU hidden)"))

    tail: collections.deque[str] = collections.deque(maxlen=40)

    def _pump_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            tail.append(line)
            try:
                sys.stderr.write(f"  [batch-worker] {line}\n")
            except Exception:
                pass

    threading.Thread(target=_pump_stderr, daemon=True, name="batch-worker-stderr").start()

    finished = threading.Event()

    def _kill_on_cancel() -> None:
        # Job-scoped: lives only while this child runs.
        while not finished.is_set():
            if cancel_event.wait(1.0):
                _kill_tree(proc)
                return

    if cancel_event is not None:
        threading.Thread(target=_kill_on_cancel, daemon=True, name="batch-worker-cancel").start()

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    done_info = None
    error = None
    try:
        try:
            proc.stdin.write(pickle.dumps({"wav_path": wav_path, "params": params,
                                           "tracks_root": tracks_root,
                                           "fingerprints": on_fingerprint is not None}))
            proc.stdin.close()
        except OSError:
            pass  # the child died at startup; its stderr tail says why (below)
        while True:
            frame = _read_frame(proc.stdout)
            if frame is None:
                break
            kind, payload = frame
            if kind == "done":
                # Every segment has been delivered by now, so a cancel landing
                # at this point has nothing left to stop: the pass is complete.
                done_info = payload or {}
                continue
            if _cancelled():
                raise ReanalysisCancelled()
            if kind == "text":
                try:
                    on_text(*payload)
                except ReanalysisCancelled:
                    raise
                except Exception:
                    traceback.print_exc()
            elif kind == "fp":
                if on_fingerprint is not None:
                    try:
                        on_fingerprint(*payload)
                    except ReanalysisCancelled:
                        raise
                    except Exception as e:
                        log.warn("batch", f"Fingerprint callback failed for {payload[0]}: {e}")
            elif kind == "progress":
                if on_progress is not None:
                    try:
                        on_progress(payload)
                    except Exception:
                        pass
            elif kind == "devices":
                if on_devices is not None:
                    try:
                        on_devices(payload)
                    except Exception:
                        pass
            elif kind == "log":
                level, tag, msg = payload
                getattr(log, level if level in ("info", "warn", "error") else "info")(tag, msg)
            elif kind == "error":
                error = payload
    finally:
        finished.set()
        if proc.poll() is None and done_info is None:
            _kill_tree(proc)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=10)
        with _live_lock:
            _live.discard(proc)
        log.info("reanalysis", f"Batch worker exited (code {proc.returncode})")

    if _cancelled() and done_info is None:
        raise ReanalysisCancelled()
    if error is not None:
        etype, emsg = error
        if etype in ("ImportError", "ModuleNotFoundError"):
            raise ImportError(emsg)
        raise RuntimeError(f"{etype}: {emsg}")
    if done_info is None:
        detail = " | ".join(list(tail)[-6:])
        raise RuntimeError(f"Batch worker exited with code {proc.returncode} before "
                           f"finishing{': ' + detail if detail else ''}")
    if done_info.get("cuda_initialized") is not None:
        log.info("reanalysis", "Batch worker used the NVIDIA GPU: "
                               f"{'yes' if done_info['cuda_initialized'] else 'no'}")


# ── Child side ───────────────────────────────────────────────────────────────

def prepare_pipeline_env() -> None:
    """Give this process the library patches the app process has.

    The app imports ml.diarizer long before any reanalysis, and the batch
    pipeline silently relied on what that import does: torchaudio 2.x
    compatibility shims (pyannote fails to import without AudioMetaData), the
    torch.load weights_only patch for pyannote checkpoints, and the
    speechbrain stubs. A fresh child must set them up before pyannote loads."""
    import ml.diarizer as _diarizer
    _diarizer.neutralise_speechbrain_lazy_modules()


def _child_main() -> int:
    # Keep the frame stream private: fd 1 (and the Win32 stdout handle that
    # native code writes through) now lead to stderr.
    proto = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    if sys.platform == "win32":
        try:
            import ctypes
            import msvcrt
            ctypes.windll.kernel32.SetStdHandle(-11, msvcrt.get_osfhandle(2))
        except Exception:
            pass

    send_lock = threading.Lock()

    def send(kind: str, payload) -> None:
        with send_lock:
            _write_frame(proto, kind, payload)

    sys.path.insert(0, str(_ROOT))
    from core import log
    # Lines go to the parent, which writes them to the app log once; two
    # processes appending to (and rotating) app.log would collide.
    log._echo = lambda line: None
    log._capture = lambda level, tag, msg: send("log", (level, tag, msg))

    try:
        job = pickle.load(sys.stdin.buffer)
        impl = os.environ.get("MA_BATCH_WORKER_IMPL")  # tests: "module:Class"
        if impl:
            import importlib
            mod_name, cls_name = impl.split(":", 1)
            BatchTranscriber = getattr(importlib.import_module(mod_name), cls_name)
        else:
            prepare_pipeline_env()
            from ml.batch_transcriber import BatchTranscriber
        bt = BatchTranscriber(
            on_text_callback=lambda *a: send("text", a),
            fingerprint_callback=(lambda *a: send("fp", a)) if job.get("fingerprints") else None,
            hf_token=os.getenv("HUGGING_FACE_KEY", ""),
            on_progress_callback=lambda p: send("progress", p),
            on_devices_callback=lambda diar, whisper: send(
                "devices", {"diarizer": diar, "whisper": whisper}),
        )
        bt.process_wav_file(job["wav_path"], job["params"],
                            tracks_root=job.get("tracks_root"))
        info = {}
        try:
            import torch
            info["cuda_initialized"] = bool(torch.cuda.is_initialized())
        except Exception:
            pass
        send("done", info)
        code = 0
    except BaseException as e:  # noqa: BLE001 - every failure must reach the parent
        traceback.print_exc()
        try:
            send("error", (type(e).__name__, str(e)))
        except Exception:
            pass
        code = 1
    try:
        proto.flush()
    except Exception:
        pass
    # Skip interpreter teardown: unloading torch/CUDA at exit is slow and the
    # OS reclaims everything (including the GPU) when the process ends.
    os._exit(code)


if __name__ == "__main__":
    _child_main()
