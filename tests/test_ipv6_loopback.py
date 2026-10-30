"""The app answers on the IPv6 loopback as well as 127.0.0.1.

The page lives at http://localhost, and Windows resolves localhost to ::1
first. With nothing listening there, Chromium waits out its 300 ms fallback
before it tries 127.0.0.1, and Werkzeug closes the connection after every
response, so every request the page made paid it again: measured 2026-10-30,
301 ms to connect against 3 ms to answer, on every click in the Speakers
dialog and everywhere else. _serve_ipv6_loopback() runs the same app on
[::1]. It is best effort, and Werkzeug's make_server() reports a failed bind
with sys.exit(1), so a taken port must end in a log line, never an exit.

The function runs for real here, out of app.py's source (importing app.py
pulls in the whole app), against a small WSGI app on a spare port.
"""
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import werkzeug.serving

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _fn(src: str, start: str, end: str) -> str:
    body = src[src.index(start):]
    return body[:body.index(end)]


def _ipv6_loopback_available() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not _ipv6_loopback_available(), reason="no IPv6 loopback")


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, _tag, message):
        self.lines.append(message)


def _wsgi(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [f"hello {environ['REMOTE_ADDR']}".encode()]


def _serve_fn(log):
    namespace = {"app": _wsgi, "log": log}
    exec(_fn(APP_PY, "def _serve_ipv6_loopback(", "\ndef main("), namespace)
    return namespace["_serve_ipv6_loopback"]


def _spare_ipv6_port() -> int:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        s.bind(("::1", 0))
        return s.getsockname()[1]


@needs_ipv6
def test_the_app_answers_on_the_ipv6_loopback(monkeypatch):
    made = []
    real = werkzeug.serving.make_server

    def capture(*args, **kwargs):
        server = real(*args, **kwargs)
        made.append(server)
        return server

    monkeypatch.setattr(werkzeug.serving, "make_server", capture)
    log = _Log()
    port = _spare_ipv6_port()
    thread = threading.Thread(target=_serve_fn(log), args=(port,), daemon=True)
    thread.start()
    try:
        body = None
        for _ in range(100):
            try:
                body = OPENER.open(f"http://[::1]:{port}/", timeout=2).read()
                break
            except OSError:
                time.sleep(0.05)
        assert body == b"hello ::1", (body, log.lines)
        assert not log.lines
        # Loopback only, like the IPv4 bind: nothing on the wildcard address.
        assert made and made[0].server_address[0] == "::1"
    finally:
        if made:
            made[0].shutdown()
            made[0].server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


@needs_ipv6
def test_a_taken_ipv6_port_is_logged_not_fatal():
    log = _Log()
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as taken:
        taken.bind(("::1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        _serve_fn(log)(port)   # returns; make_server's sys.exit never runs
    assert len(log.lines) == 1 and "falls back to 127.0.0.1" in log.lines[0]


def test_main_starts_it_beside_the_ipv4_server():
    main = _fn(APP_PY, "\ndef main(", '\nif __name__ == "__main__":')
    v4 = main.index('app.run(host="127.0.0.1"')
    v6 = main.index("target=_serve_ipv6_loopback")
    assert v4 < v6
    # Its own daemon thread: a failure there must not touch the app's start.
    assert "daemon=True" in main[v6:v6 + 200]
    serve = _fn(APP_PY, "def _serve_ipv6_loopback(", "\ndef main(")
    assert 'make_server("::1"' in serve
    assert '"::"' not in serve and "0.0.0.0" not in serve
