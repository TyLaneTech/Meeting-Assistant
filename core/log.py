"""
Shared console logging helpers with ANSI colour.
Imported by app.py, transcriber.py, diarizer.py, etc.

Besides printing to the console (unchanged behaviour), every log line is now
also kept in an in-memory ring buffer and appended to a rotating log file
under ``<data_dir>/logs/``. The Agent API (``/api/agent/v1/system/logs``)
reads both, so external agents can inspect what the app has been doing
without access to the console.

The capture layer must never break logging: file-write failures are swallowed
and the console print always happens first.
"""
import ctypes
import io
import sys
import threading
import time
from collections import deque
from datetime import datetime


def _enable_ansi() -> None:
    if sys.platform == "win32":
        try:
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            pass


_enable_ansi()

_R   = "\033[0m"
_RED = "\033[91m"
_GRN = "\033[92m"
_YLW = "\033[93m"
_BLU = "\033[94m"
_MAG = "\033[95m"
_CYN = "\033[96m"
_GRY = "\033[90m"

_TAG_COLORS: dict[str, str] = {
    "whisper":     _CYN,
    "transcriber": _CYN,
    "diarizer":    _MAG,
    "ai":          _GRN,
    "summary":     _GRN,
    "recording":   _BLU,
    "reanalysis":  _BLU,
    "settings":    _GRY,
    "fingerprint": _YLW,
    "audio":       _GRY,
    "tray":        _GRY,
    "storage":     _GRY,
    "app":         _GRY,
    "agent":       _BLU,
}


def _fmt_tag(tag: str) -> str:
    color = _TAG_COLORS.get(tag.lower(), _GRY)
    return f"{color}[{tag}]{_R}"


# ── Capture layer (ring buffer + rotating file) ───────────────────────────────

# Wall-clock moment this process started logging - effectively app startup.
STARTED_AT = time.time()

_RING_MAX = 4000
_ring: deque = deque(maxlen=_RING_MAX)
_ring_seq = 0                      # monotonically increasing entry id
_capture_lock = threading.Lock()

_LOG_FILE_NAME = "app.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024   # rotate at 5 MB
_LOG_KEEP = 3                      # app.log.1 … app.log.3


def _logs_dir():
    """Resolve <data_dir>/logs lazily; core.paths has no internal imports so
    this is safe even though log.py is one of the first modules loaded."""
    from core import paths as _paths
    p = _paths.data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _rotate_locked(path) -> None:
    """Shift app.log → app.log.1 → … under the capture lock."""
    try:
        for i in range(_LOG_KEEP, 0, -1):
            older = path.with_name(f"{_LOG_FILE_NAME}.{i}")
            newer = (path if i == 1
                     else path.with_name(f"{_LOG_FILE_NAME}.{i - 1}"))
            if newer.exists():
                if older.exists():
                    older.unlink()
                newer.rename(older)
    except OSError:
        pass  # rotation is best-effort; keep appending to the current file


def _capture(level: str, tag: str, msg: str) -> None:
    """Record a log line in the ring buffer and the on-disk log file."""
    global _ring_seq
    entry = {
        "ts": datetime.utcnow().isoformat(timespec="milliseconds"),
        "level": level,
        "tag": tag,
        "msg": msg,
    }
    with _capture_lock:
        _ring_seq += 1
        entry["id"] = _ring_seq
        _ring.append(entry)
        try:
            path = _logs_dir() / _LOG_FILE_NAME
            if path.exists() and path.stat().st_size >= _LOG_MAX_BYTES:
                _rotate_locked(path)
            with io.open(path, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"{entry['ts']} [{level.upper():5s}] [{tag}] {msg}\n")
        except Exception:
            pass  # disk trouble must never take logging down


def recent(
    limit: int = 200,
    *,
    level: str | None = None,
    tag: str | None = None,
    contains: str | None = None,
    after_id: int | None = None,
) -> list[dict]:
    """Return the newest captured log entries, oldest-first.

    ``level`` filters at-or-above severity (info < warn < error). ``tag`` is an
    exact tag match (case-insensitive), ``contains`` a case-insensitive
    substring of the message, and ``after_id`` returns only entries newer than
    a previously seen entry id (for incremental polling).
    """
    order = {"info": 0, "warn": 1, "error": 2}
    min_rank = order.get((level or "").lower(), 0)
    needle = (contains or "").lower()
    want_tag = (tag or "").lower()

    with _capture_lock:
        snapshot = list(_ring)

    out = []
    for e in snapshot:
        if after_id is not None and e["id"] <= after_id:
            continue
        if min_rank and order.get(e["level"], 0) < min_rank:
            continue
        if want_tag and e["tag"].lower() != want_tag:
            continue
        if needle and needle not in e["msg"].lower():
            continue
        out.append(e)
    return out[-max(1, limit):]


def log_files() -> list[dict]:
    """List the persisted log files (name, size, modified time)."""
    try:
        files = sorted(_logs_dir().glob(f"{_LOG_FILE_NAME}*"))
    except Exception:
        return []
    out = []
    for p in files:
        try:
            st = p.stat()
            out.append({
                "name": p.name,
                "size_bytes": st.st_size,
                "modified": datetime.utcfromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
        except OSError:
            continue
    return out


def read_log_file(name: str, lines: int = 500) -> str | None:
    """Return the last ``lines`` lines of a persisted log file, or None if the
    name doesn't correspond to one of ours (guards path traversal)."""
    allowed = {f["name"] for f in log_files()}
    if name not in allowed:
        return None
    try:
        text = (_logs_dir() / name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    tail = text.splitlines()[-max(1, lines):]
    return "\n".join(tail)


# ── Public logging API (console behaviour unchanged) ─────────────────────────

def info(tag: str, msg: str) -> None:
    print(f"  {_fmt_tag(tag)}  {msg}")
    _capture("info", tag, msg)


def warn(tag: str, msg: str) -> None:
    print(f"  {_YLW}[{tag}]{_R}  {_YLW}{msg}{_R}")
    _capture("warn", tag, msg)


def error(tag: str, msg: str) -> None:
    print(f"  {_RED}[{tag}]{_R}  {_RED}{msg}{_R}")
    _capture("error", tag, msg)
