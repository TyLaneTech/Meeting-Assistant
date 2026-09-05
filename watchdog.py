#!/usr/bin/env python3
"""External freeze watchdog for Meeting Assistant.

Runs as a SEPARATE process from app.py so a whole-process freeze is actually
caught. On 2026-09-01 the app deadlocked mid-recording (heavy background
summary/export load collided with the live recording pipeline): the HTTP server,
capture threads, and DB writes all stopped, but the process never exited, so the
in-process supervision never ran and ~38 minutes of a meeting were lost silently.
This watchdog polls the app's /api/status from outside the process and restarts
it when it stops responding.

It tells a freeze/crash (restart) from a clean quit (leave alone) via the
heartbeat file (core/heartbeat.py):

    heartbeat present, pid alive, HTTP unreachable >= GRACE  -> FROZEN  -> restart
    heartbeat present, pid dead                             -> CRASHED -> restart
    heartbeat absent                                        -> clean quit -> ignore

It only ever restarts an app it has seen healthy at least once, so it never
fights a normal startup or an app the user quit on purpose.

Modes:
    python watchdog.py            loop forever (default; started by launch.py)
    python watchdog.py --once     one check + act, then exit (for a scheduler)
    python watchdog.py --dry-run  never kill/relaunch; only log what it WOULD do
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import heartbeat, paths  # noqa: E402

PROJECT_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "6969"))
STATUS_URL = f"http://127.0.0.1:{PORT}/api/status"

POLL_SEC = 20          # how often to poll while healthy
HTTP_TIMEOUT = 5       # per-request timeout
GRACE_SEC = 75         # unreachable this long (after being seen healthy) = frozen
MAX_RESTARTS = 3       # within RESTART_WINDOW, then back off and alert only
RESTART_WINDOW = 600   # seconds

DRY_RUN = "--dry-run" in sys.argv
ONCE = "--once" in sys.argv


def _logs_dir() -> Path:
    d = paths.data_dir() / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [watchdog] {msg}"
    try:
        with open(_logs_dir() / "watchdog.log", "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _http_ok() -> bool:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=HTTP_TIMEOUT) as r:
            return 200 <= r.status < 500
    except Exception:
        return False


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return str(int(pid)) in out
    except Exception:
        return True  # if we cannot tell, assume alive (do not kill blindly)


def _toast(title: str, body: str) -> None:
    """Best-effort OS toast. Safe here: the watchdog is a clean separate process
    and never touches pycaw/COM, so the in-app WinRT toast crash does not apply."""
    if DRY_RUN:
        return
    try:
        from windows_toasts import Toast, WindowsToaster
        toaster = WindowsToaster("Meeting Assistant")
        t = Toast()
        t.text_fields = [title, body]
        toaster.show_toast(t)
    except Exception as e:
        _log(f"toast failed ({e})")


def _kill(pid: int) -> None:
    if not pid or DRY_RUN:
        _log(f"[dry-run] would kill pid {pid}" if DRY_RUN else "no pid to kill")
        return
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int(pid))],
                       capture_output=True, text=True, timeout=20)
        _log(f"killed pid {pid}")
    except Exception as e:
        _log(f"kill pid {pid} failed ({e})")


def _relaunch() -> None:
    vbs = PROJECT_DIR / "launch_hidden.vbs"
    bat = PROJECT_DIR / "launch.bat"
    if DRY_RUN:
        _log(f"[dry-run] would relaunch via {'launch_hidden.vbs' if vbs.exists() else 'launch.bat'}")
        return
    try:
        if vbs.exists():
            subprocess.Popen(["wscript.exe", str(vbs)], cwd=str(PROJECT_DIR), close_fds=True)
            _log("relaunched via launch_hidden.vbs")
        else:
            subprocess.Popen([str(bat)], cwd=str(PROJECT_DIR), close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            _log("relaunched via launch.bat")
    except Exception as e:
        _log(f"relaunch failed ({e})")


def _acquire_singleton() -> bool:
    """Ensure only one watchdog runs. Returns False if another live one holds it.

    Uses an atomic O_EXCL create so two watchdogs starting at the same time
    cannot both win (the check-then-write version raced and left duplicates). If
    an existing lock's holder is dead, the stale lock is cleared and retried."""
    lock = paths.data_dir() / "watchdog.lock"
    for _ in range(3):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                other = int(lock.read_text(encoding="utf-8").strip() or "0")
            except Exception:
                other = 0
            if other and other != os.getpid() and _pid_alive(other):
                _log(f"another watchdog is running (pid {other}); exiting")
                return False
            # Stale lock (holder dead or unreadable): clear it and retry.
            try:
                lock.unlink()
            except Exception:
                pass
        except Exception:
            return True  # never block the watchdog on a lock hiccup
    return True


def _decide_and_act(state: dict) -> None:
    """One evaluation. Mutates ``state`` (seen_healthy, restarts).

    Freeze detection uses the heartbeat's own timestamp: a frozen app stops
    advancing it, so ``now - heartbeat.ts >= GRACE`` while HTTP is unreachable is
    a solid freeze signal that works whether we poll in a loop or once. A dead pid
    (heartbeat still present) is a crash. An absent heartbeat means a clean quit.
    """
    if _http_ok():
        if not state.get("seen_healthy"):
            _log("app is healthy; watchdog armed")
        state["seen_healthy"] = True
        return

    # Unreachable. Only ever act on an app we have seen healthy at least once, so
    # we never fight a normal startup or an app the user quit on purpose.
    hb = heartbeat.read()
    if hb is None:
        return  # clean quit (heartbeat cleared) or not running
    if not state.get("seen_healthy"):
        return  # still coming up for the first time

    now = time.time()
    pid = int(hb.get("pid") or 0)
    ts = float(hb.get("ts") or 0)
    crashed = not _pid_alive(pid)
    frozen = (now - ts) >= GRACE_SEC  # heartbeat stopped advancing = wedged
    if not (crashed or frozen):
        _log("app not responding but heartbeat is fresh; watching…")
        return

    # Restart budget.
    hist = [t for t in state.get("restarts", []) if now - t < RESTART_WINDOW]
    if len(hist) >= MAX_RESTARTS:
        _log(f"restart budget exhausted ({len(hist)} in {RESTART_WINDOW}s); alerting only")
        _toast("Meeting Assistant keeps failing",
               "It has restarted several times and needs a look. Open the app.")
        state["restarts"] = hist
        state["seen_healthy"] = False
        return

    was_recording = bool(hb.get("recording"))
    reason = "crashed" if crashed else "froze"
    _log(f"app {reason} (pid {pid}, recording={was_recording}); restarting")
    if was_recording:
        _toast("Meeting Assistant recovered",
               "The app froze during a recording and was restarted. "
               "The last stretch may be missing; start a new recording to continue.")
    else:
        _toast("Meeting Assistant restarted", "The app stopped responding and was restarted.")

    _kill(pid)
    heartbeat.clear()  # avoid re-acting on the same stale heartbeat
    time.sleep(2)
    _relaunch()

    hist.append(now)
    state["restarts"] = hist
    state["seen_healthy"] = False  # wait for the new instance to come up before re-arming


def main() -> int:
    if not ONCE and not _acquire_singleton():
        return 0
    _log(f"watchdog start (pid {os.getpid()}, once={ONCE}, dry_run={DRY_RUN}, url={STATUS_URL})")
    state = {"seen_healthy": False, "restarts": []}
    if ONCE:
        # A scheduler invocation: seed seen_healthy from a fresh heartbeat so a
        # single check can still catch a crash/freeze between invocations.
        hb = heartbeat.read()
        if hb is not None:
            state["seen_healthy"] = True
        _decide_and_act(state)
        return 0
    while True:
        try:
            _decide_and_act(state)
        except Exception as e:
            _log(f"loop error ({e})")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
