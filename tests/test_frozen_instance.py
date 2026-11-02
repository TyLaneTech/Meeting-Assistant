"""A launch replaces an instance that froze holding the port, and only that.

On 2026-11-02 a GPU driver update installed under a running instance, and the
first GPU work of its next recording wedged it: one thread left, spinning in
its exit, still listening on 6969. Every launch after that met a port that
accepted connections and never answered. The handshake read the timeout as
"nothing is running", Werkzeug's SO_REUSEADDR let the new server bind beside
the dead one on Windows, and the Start Menu launcher gave up three minutes
later with "taking longer than usual to start".

core/frozen_instance.py runs for real here, against real sockets and a real
process tree. app.py is never imported (that loads the models):
_settle_silent_port and _LocalServer are lifted out of its source and run
against stand-ins.

Run: .venv/Scripts/python.exe -m pytest tests/test_frozen_instance.py -q
"""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import werkzeug.serving

from core import frozen_instance, heartbeat

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fn(src: str, start: str, end: str) -> str:
    body = src[src.index(start):]
    return body[:body.index(end)]


def _main_body() -> str:
    body = APP_PY[APP_PY.index("def main() -> None:"):]
    return body[:body.index('if __name__ == "__main__":\n    main()')]


def _spare_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listener(port: int = 0) -> socket.socket:
    s = socket.socket()
    s.bind(("127.0.0.1", port))
    s.listen()
    return s


def _wsgi(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"hello"]


# ── The port ──────────────────────────────────────────────────────────────────

def test_port_is_free_tells_a_listener_from_an_empty_port():
    port = _spare_port()
    assert frozen_instance.port_is_free(port)
    with _listener(port):
        assert not frozen_instance.port_is_free(port)
    assert frozen_instance.port_is_free(port)


def test_an_instance_that_just_quit_leaves_the_port_free():
    """Werkzeug closes each connection itself, so an instance that quit leaves
    its side of them in TIME_WAIT. Reading that as held would make every quick
    relaunch wait out the patience and then abort."""
    srv = _listener()
    port = srv.getsockname()[1]
    for _ in range(3):
        c = socket.create_connection(("127.0.0.1", port))
        a, _ = srv.accept()
        a.close()              # the server closes first, as Werkzeug does
        c.recv(1)
        c.close()
    srv.close()
    assert frozen_instance.port_is_free(port)


def test_listening_pids_finds_the_listener_and_only_the_listener():
    with _listener() as srv:
        port = srv.getsockname()[1]
        assert os.getpid() in frozen_instance.listening_pids(port)
        c = socket.create_connection(("127.0.0.1", port))
        a, _ = srv.accept()
    try:
        # Still connected on that port, but nothing listens on it now.
        assert os.getpid() not in frozen_instance.listening_pids(port)
    finally:
        a.close()
        c.close()


# ── Which instance counts as frozen ───────────────────────────────────────────

@pytest.fixture
def beat(monkeypatch):
    """A stand-in heartbeat file and port table."""
    state = {"hb": None, "listening": set()}
    monkeypatch.setattr(frozen_instance.heartbeat, "read", lambda: state["hb"])
    monkeypatch.setattr(frozen_instance, "listening_pids", lambda port: state["listening"])
    return state


def _hb(pid=4242, port=6969, age=300.0, now=10_000.0):
    return {"pid": pid, "ts": now - age, "port": port, "recording": False}


def test_a_stale_heartbeat_that_names_the_listener_is_frozen(beat):
    beat["hb"], beat["listening"] = _hb(age=300.0), {4242}
    assert frozen_instance.frozen_holder(6969, now=10_000.0) == (4242, 300.0)


def test_a_heartbeat_that_still_advances_is_alive(beat):
    """Busy, or its server thread died; it may be recording. Never ended."""
    beat["hb"], beat["listening"] = _hb(age=frozen_instance.FROZEN_AFTER_SEC - 1), {4242}
    assert frozen_instance.frozen_holder(6969, now=10_000.0) is None


def test_a_pid_that_does_not_hold_the_port_is_never_ended(beat):
    """A heartbeat left by a crash names a pid Windows may have handed out again."""
    beat["hb"], beat["listening"] = _hb(age=300.0), {9999}
    assert frozen_instance.frozen_holder(6969, now=10_000.0) is None


@pytest.mark.parametrize("hb", [
    None,
    _hb(port=6970),                           # an instance on another port
    _hb(pid=os.getpid()),                     # this process
    {"pid": "x", "ts": 0, "port": 6969},      # unreadable
    {"ts": 0, "port": 6969},                  # no pid
])
def test_anything_else_is_not_ours_to_end(beat, hb):
    beat["hb"], beat["listening"] = hb, {4242, os.getpid()}
    assert frozen_instance.frozen_holder(6969, now=10_000.0) is None


# ── Ending it ─────────────────────────────────────────────────────────────────

_INSTANCE = r"""
import socket, subprocess, sys, time
port, helper_port = int(sys.argv[1]), int(sys.argv[2])
s = socket.socket(); s.bind(("127.0.0.1", port)); s.listen()
helper = subprocess.Popen(
    [sys.executable, "-c",
     "import socket, sys, time; s = socket.socket(); "
     "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(); print('up', flush=True); time.sleep(120)",
     str(helper_port)],
    stdout=subprocess.PIPE, text=True)
helper.stdout.readline()
print("ready", flush=True)
time.sleep(120)
"""


def test_end_takes_the_instance_and_its_helpers():
    """The desktop audio helper and ffmpeg are the frozen instance's children,
    and on 2026-11-02 ending them is what let the instance finish exiting."""
    port, helper_port = _spare_port(), _spare_port()
    instance = subprocess.Popen([sys.executable, "-c", _INSTANCE, str(port), str(helper_port)],
                                stdout=subprocess.PIPE, text=True)
    try:
        assert instance.stdout.readline().strip() == "ready"
        assert not frozen_instance.port_is_free(port)
        assert not frozen_instance.port_is_free(helper_port)
        frozen_instance.end(instance.pid)
        instance.wait(timeout=15)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
                frozen_instance.port_is_free(port) and frozen_instance.port_is_free(helper_port)):
            time.sleep(0.1)
        assert frozen_instance.port_is_free(port), "the instance still holds its port"
        assert frozen_instance.port_is_free(helper_port), "its helper outlived it"
    finally:
        if instance.poll() is None:
            instance.kill()


# ── The launch's decision (app._settle_silent_port) ───────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


class _Machine:
    """The port, what the heartbeat says about its holder, and what was ended."""

    def __init__(self, free=False, holders=()):
        self.free = free
        self.holders = list(holders)   # frozen_holder()'s answers, one per call; None after
        self.ended = []

    def port_is_free(self, port):
        return self.free

    def frozen_holder(self, port):
        return self.holders.pop(0) if self.holders else None

    def end(self, pid):
        self.ended.append(pid)
        self.free = True

    def listening_pids(self, port):
        return {4242}


class _Opener:
    def __init__(self, answers_on=None):
        self.calls = 0
        self.answers_on = answers_on

    def open(self, url, timeout=None):
        self.calls += 1
        if self.answers_on is not None and self.calls >= self.answers_on:
            return object()
        raise OSError("timed out")


class _Log:
    def __init__(self):
        self.lines = []

    def __getattr__(self, level):
        return lambda tag, message: self.lines.append((level, message))


class _Heartbeat:
    def __init__(self):
        self.cleared = False

    def clear(self):
        self.cleared = True


def _settle(machine, opener=None):
    clock, log, hb = _Clock(), _Log(), _Heartbeat()
    opener = opener or _Opener()
    namespace = {
        "frozen_instance": machine, "heartbeat": hb, "log": log, "time": clock,
        "_local_opener": lambda: opener, "print": lambda *a, **k: None,
        "_SILENT_PORT_PATIENCE_SEC": frozen_instance.FROZEN_AFTER_SEC + 15,
    }
    exec(_fn(APP_PY, "def _settle_silent_port(", "\ndef _keepalive_loop("), namespace)
    outcome = namespace["_settle_silent_port"]("http://localhost:6969")
    return outcome, clock.t - 1000.0, log.lines, hb.cleared, opener.calls


def test_a_cold_start_goes_straight_on():
    machine = _Machine(free=True)
    outcome, waited, lines, cleared, calls = _settle(machine)
    assert (outcome, waited, lines, cleared, calls) == ("free", 0, [], False, 0)


def test_a_frozen_instance_is_ended_and_the_launch_goes_on():
    """The 2026-11-02 case: frozen for minutes, so it goes at the first look."""
    machine = _Machine(holders=[(4242, 300.0)])
    outcome, waited, lines, cleared, _ = _settle(machine)
    assert outcome == "free"
    assert machine.ended == [4242]
    assert cleared, "the watchdog would count the dead pid as a crash and relaunch"
    assert waited == 0
    assert any(level == "warn" and "4242" in msg for level, msg in lines)


def test_an_instance_that_has_only_just_frozen_is_waited_out_then_ended():
    machine = _Machine(holders=[None] * 20 + [(4242, frozen_instance.FROZEN_AFTER_SEC + 1)])
    outcome, waited, _, _, _ = _settle(machine)
    assert (outcome, machine.ended) == ("free", [4242])
    assert waited < frozen_instance.FROZEN_AFTER_SEC + 15


def test_a_busy_instance_that_answers_is_negotiated_with_not_ended():
    machine = _Machine()
    outcome, _, _, _, _ = _settle(machine, _Opener(answers_on=3))
    assert (outcome, machine.ended) == ("answering", [])


def test_a_live_instance_that_never_answers_is_left_alone():
    """Its heartbeat still advances, so it may be recording: abort, end nothing."""
    machine = _Machine()
    outcome, waited, lines, cleared, _ = _settle(machine)
    assert (outcome, machine.ended, cleared) == ("held", [], False)
    assert waited >= frozen_instance.FROZEN_AFTER_SEC + 15
    assert any(level == "error" and "4242" in msg for level, msg in lines)


_FROZEN = r"""
import json, os, socket, sys, time
port, hb_path = int(sys.argv[1]), sys.argv[2]
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # as Werkzeug's server sets it
s.bind(("127.0.0.1", port))
s.listen()
with open(hb_path, "w") as f:   # its last heartbeat, five minutes ago
    json.dump({"pid": os.getpid(), "ts": time.time() - 300, "port": port, "recording": True}, f)
print("ready", flush=True)
time.sleep(120)                 # connections queue in the backlog; nothing answers
"""


def test_a_real_frozen_instance_is_found_ended_and_replaced(monkeypatch, tmp_path):
    """End to end, with the real port table, process tree and heartbeat file
    (in a temp folder, so a running app's own heartbeat is never touched)."""
    hb_file = tmp_path / "heartbeat.json"
    monkeypatch.setattr(heartbeat, "path", lambda: hb_file)
    port = _spare_port()
    frozen = subprocess.Popen([sys.executable, "-c", _FROZEN, str(port), str(hb_file)],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert frozen.stdout.readline().strip() == "ready"
        log = _Log()
        namespace = {
            "frozen_instance": frozen_instance, "heartbeat": heartbeat, "log": log,
            "time": time, "_local_opener": lambda: OPENER, "print": lambda *a, **k: None,
            "_SILENT_PORT_PATIENCE_SEC": frozen_instance.FROZEN_AFTER_SEC + 15,
        }
        exec(_fn(APP_PY, "def _settle_silent_port(", "\ndef _keepalive_loop("), namespace)
        started = time.monotonic()
        assert namespace["_settle_silent_port"](f"http://localhost:{port}") == "free", log.lines
        assert time.monotonic() - started < 15
        frozen.wait(timeout=15)
        assert not hb_file.exists()
        assert any(level == "warn" and "ending it" in msg for level, msg in log.lines)
    finally:
        if frozen.poll() is None:
            frozen.kill()


# ── Wiring in app.py ──────────────────────────────────────────────────────────

def test_the_handshake_settles_a_silent_port_instead_of_assuming_it_is_free():
    body = _fn(APP_PY, "def _handshake_existing_instance(", "\ndef _settle_silent_port(")
    no_answer = body[body.index("except Exception:"):body.index('if data.get("busy")')]
    assert "_settle_silent_port(url)" in no_answer
    assert 'return settled == "free"' in no_answer


def test_main_binds_on_its_own_thread_and_only_then_writes_the_heartbeat():
    main = _main_body()
    assert "app.run(host=" not in main
    handshake = main.index("_handshake_existing_instance(url)")
    bind = main.index('server = _LocalServer("127.0.0.1", port, app)')
    beat = main.index("target=_heartbeat_loop")
    serve = main.index("target=server.serve_forever")
    assert handshake < bind < beat < serve
    # Not at import, where a launch deciding on a takeover would overwrite the
    # heartbeat it is about to read.
    assert not [line for line in APP_PY.splitlines()
                if line.startswith("threading.Thread(target=_heartbeat_loop")]


def _local_server():
    namespace = {"ThreadedWSGIServer": werkzeug.serving.ThreadedWSGIServer, "sys": sys}
    exec(_fn(APP_PY, "class _LocalServer(", "\ndef _serve_ipv6_loopback("), namespace)
    return namespace["_LocalServer"]


@pytest.mark.skipif(sys.platform != "win32",
                    reason="only Windows lets SO_REUSEADDR bind past a listener")
def test_the_server_cannot_come_up_beside_a_frozen_one():
    """Windows lets a SO_REUSEADDR bind past a listener that set the flag too,
    which is what both instances' Werkzeug servers did."""
    frozen = werkzeug.serving.ThreadedWSGIServer("127.0.0.1", 0, _wsgi)
    port = frozen.server_address[1]
    try:
        # What app.run() built binds right beside it.
        twin = werkzeug.serving.ThreadedWSGIServer("127.0.0.1", port, _wsgi)
        twin.server_close()
        with pytest.raises(SystemExit):
            _local_server()("127.0.0.1", port, _wsgi)
    finally:
        frozen.server_close()


@pytest.mark.skipif(sys.platform != "win32",
                    reason="only Windows lets SO_REUSEADDR bind past a listener")
def test_nothing_can_come_up_beside_the_server_either():
    """Without the flag, Windows refuses a SO_REUSEADDR bind beside it, so an
    older build launched later cannot start next to this one."""
    ours = _local_server()("127.0.0.1", 0, _wsgi)
    try:
        with pytest.raises(SystemExit):
            werkzeug.serving.ThreadedWSGIServer("127.0.0.1", ours.server_address[1], _wsgi)
    finally:
        ours.server_close()


def test_a_takeover_binds_the_port_the_moment_the_old_server_closes():
    """The server closes every connection itself, so the old instance leaves
    TIME_WAIT behind. Without SO_REUSEADDR the new bind must still succeed, or
    every restart would fail."""
    cls = _local_server()
    first = cls("127.0.0.1", 0, _wsgi)
    port = first.server_address[1]
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    try:
        for _ in range(3):
            assert OPENER.open(f"http://127.0.0.1:{port}/", timeout=5).read() == b"hello"
    finally:
        first.shutdown()
        first.server_close()
        thread.join(timeout=5)
    second = cls("127.0.0.1", port, _wsgi)   # a SystemExit here breaks every restart
    second.server_close()
