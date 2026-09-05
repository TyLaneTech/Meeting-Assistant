"""Liveness heartbeat, written by the running app and read by the external
watchdog (``watchdog.py``) so a FROZEN app can be told apart from one that was
quit on purpose.

The file lives at ``<data_dir>/heartbeat.json``. The app refreshes it every few
seconds while alive and REMOVES it on a clean shutdown. The watchdog then reads:

  - file present, pid alive, HTTP not responding for a while  -> FROZEN  (restart)
  - file present, pid dead                                     -> CRASHED (restart)
  - file absent                                                -> clean quit / not
                                                                  running (leave it)

Best-effort throughout: a heartbeat write or clear must never take the app down,
so every operation swallows its own errors.
"""
import json
import os
import time

from core import paths


def path():
    return paths.data_dir() / "heartbeat.json"


def write(recording: bool = False, session_id=None, port: int = 6969) -> None:
    """Refresh the heartbeat with the current pid, time, and recording state."""
    try:
        data = {
            "pid": os.getpid(),
            "ts": time.time(),
            "recording": bool(recording),
            "session_id": session_id,
            "port": int(port),
        }
        p = path()
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, p)  # atomic swap so a reader never sees a half-written file
    except Exception:
        pass


def clear() -> None:
    """Remove the heartbeat on a clean shutdown (signals 'quit on purpose')."""
    try:
        path().unlink(missing_ok=True)
    except Exception:
        pass


def read():
    """Return the parsed heartbeat dict, or None if missing/unreadable."""
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except Exception:
        return None
