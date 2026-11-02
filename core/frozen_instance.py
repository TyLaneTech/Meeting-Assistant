"""Find and end an instance of this app that froze while holding its port.

On 2026-11-02 a GPU driver update installed under a running instance, and the
first GPU work of its next recording wedged the process: one thread left,
spinning in its exit, with 127.0.0.1:6969 and [::1]:6969 still listening.
Every launch after that met a port that accepted connections and never
answered. The handshake read the timeout as "nothing is running", Werkzeug's
SO_REUSEADDR let the new server bind beside the dead one on Windows, and the
Start Menu launcher's checks kept landing on the dead socket until it gave up
three minutes later.

The heartbeat (core/heartbeat.py) is the evidence that what holds the port is
ours and has stopped: the instance that owns the port rewrites it every 8 s,
so a heartbeat that names the process listening on the port and is
FROZEN_AFTER_SEC old means that process's threads are no longer running.
Nothing else is ended. An instance whose heartbeat still advances is alive
(busy, or with a dead server thread) and may be recording, and a process that
never wrote our heartbeat is not ours to end.
"""
import os
import signal
import socket
import subprocess
import sys
import time

from core import heartbeat

# The heartbeat is rewritten every 8 s, so this is about four missed beats.
FROZEN_AFTER_SEC = 30.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def port_is_free(port: int) -> bool:
    """True when 127.0.0.1:port can be bound the way the app's server binds it.

    A bind, not a connect: a frozen listener whose backlog has filled refuses
    connections just as an empty port does, but it still holds the address.
    Connections left in TIME_WAIT by an instance that quit do not count: on
    Windows a plain bind succeeds beside them (measured 2026-11-02), and on
    POSIX SO_REUSEADDR lets the bind past them but never past a listener.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def listening_pids(port: int) -> set[int]:
    """Pids with an IPv4 TCP socket listening on `port`. Empty if unknown."""
    pids: set[int] = set()
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                timeout=10, creationflags=_NO_WINDOW,
            ).stdout
            for line in out.splitlines():
                # "TCP  127.0.0.1:6969  0.0.0.0:0  LISTENING  270604". The state
                # word is localized; a listener is the row with no remote end.
                parts = line.split()
                if (len(parts) == 5 and parts[0] == "TCP" and parts[2] == "0.0.0.0:0"
                        and parts[1].rsplit(":", 1)[-1] == str(port) and parts[4].isdigit()):
                    pids.add(int(parts[4]))
        else:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            pids.update(int(p) for p in out.split() if p.isdigit())
    except Exception:
        pass
    return pids


def frozen_holder(port: int, now: float | None = None) -> tuple[int, float] | None:
    """(pid, seconds since its last heartbeat) for an instance of this app that
    listens on `port` and has stopped beating; None when there is none."""
    hb = heartbeat.read()
    if not hb:
        return None
    try:
        pid = int(hb.get("pid") or 0)
        hb_port = int(hb.get("port") or 0)
        ts = float(hb.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    if not pid or pid == os.getpid() or hb_port != port:
        return None
    age = (time.time() if now is None else now) - ts
    if age < FROZEN_AFTER_SEC:
        return None
    # A heartbeat left by a crash can name a pid Windows has since handed to
    # something else. Only the process actually holding the port is ended.
    if pid not in listening_pids(port):
        return None
    return pid, age


def end(pid: int) -> None:
    """End `pid` and the processes it started (desktop audio helpers, ffmpeg).

    The children go first, and on 2026-11-02 that is what let the frozen
    process finish exiting: taskkill could not terminate it directly.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(int(pid))],
                capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
            )
        else:
            subprocess.run(["pkill", "-KILL", "-P", str(int(pid))],
                           capture_output=True, timeout=10)
            os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass
