"""The MCP server must not outlive the client that spawned it.

``serve()`` exits on a clean stdin EOF, which is how a well-behaved MCP client
shuts a stdio server down. On Windows that is not enough: when the client is
force-killed, a sibling process that inherited the write end of the server's
stdin pipe keeps it open, the pipe never breaks, ``readline()`` blocks forever
and the server is left running with a dead parent until the machine reboots.

Before 2026-09-10 that is exactly what happened, and stale ``mcp_server.py``
processes piled up across days of editor and terminal sessions.
"""
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SERVER = ROOT / "mcp_server.py"

_spec = importlib.util.spec_from_file_location("mcp_server_under_test", SERVER)
mcp_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_server)

windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the inherited-pipe leak and the process-handle wait are Windows-only")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _handshake(proc) -> dict:
    """Run initialize against a live server and return the parsed reply."""
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "lifecycle-test", "version": "0"}}}
    proc.stdin.write(json.dumps(req).encode() + b"\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline().decode())


def _alive(pid: int) -> bool:
    """Windows liveness check. os.kill(pid, 0) is not usable here: on Windows
    Python maps a non-CTRL signal onto TerminateProcess, which would kill it."""
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x00000102
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)


def _force_kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def _fake_client(server: str, pidfile: str) -> None:
    """Stand-in for an MCP client that is about to be force-killed.

    Hands the server's stdin pipe to a long-lived sibling, which is what keeps
    the pipe from breaking when this process dies.
    """
    proc = subprocess.Popen([sys.executable, server], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    _handshake(proc)
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=proc.stdin, close_fds=False)
    Path(pidfile).write_text(f"{proc.pid} {sibling.pid}", encoding="utf-8")
    time.sleep(120)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_serve_starts_the_client_watchdog():
    source = SERVER.read_text(encoding="utf-8")
    serve = source[source.index("def serve() -> None:"):]
    assert "_start_client_watchdog()" in serve
    # It must not be able to take the server down on its own.
    assert "daemon=True" in source


def test_clean_stdin_eof_still_exits():
    """The ordinary shutdown path must stay intact."""
    proc = subprocess.Popen([sys.executable, str(SERVER)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    assert _handshake(proc)["result"]["serverInfo"]["name"] == "meeting-assistant"
    proc.stdin.close()
    assert proc.wait(timeout=15) == 0


@windows_only
def test_watchdog_watches_the_client_not_the_uv_trampoline():
    """A uv venv on Windows runs the real interpreter under a launcher, so the
    client is the grandparent. Both are watched; nothing above them is."""
    pids = mcp_server._client_pids()
    assert pids, "expected at least the immediate parent"
    assert len(pids) <= 2
    parent_of, _ = mcp_server._process_table()
    assert pids[0] == parent_of[mcp_server.os.getpid()]
    if mcp_server._launched_by_trampoline() and len(pids) == 2:
        assert pids[1] == parent_of[pids[0]]


@windows_only
def test_server_exits_when_its_client_is_force_killed(tmp_path):
    pidfile = tmp_path / "pids.txt"
    client = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--fake-client",
         str(SERVER), str(pidfile)])

    deadline = time.time() + 30
    while time.time() < deadline and not pidfile.exists():
        time.sleep(0.2)
    assert pidfile.exists(), "the fake client never completed its handshake"

    launcher_pid, sibling_pid = map(int, pidfile.read_text().split())
    # Under the uv trampoline the process actually running mcp_server.py is the
    # launcher's child, so watch the whole little tree.
    parent_of, _ = mcp_server._process_table()
    server_pids = [launcher_pid] + [p for p, par in parent_of.items()
                                    if par == launcher_pid]
    try:
        _force_kill(client.pid)

        deadline = time.time() + 20
        while time.time() < deadline and any(_alive(p) for p in server_pids):
            time.sleep(0.25)

        leaked = [p for p in server_pids if _alive(p)]
        assert not leaked, (
            f"mcp_server.py leaked after its client was killed: {leaked}. "
            f"The sibling holding the stdin pipe open is pid {sibling_pid}.")
    finally:
        for pid in server_pids + [sibling_pid, client.pid]:
            if _alive(pid):
                _force_kill(pid)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--fake-client":
        _fake_client(sys.argv[2], sys.argv[3])
