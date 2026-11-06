"""A page that falls behind reconnects, and a double click opens one test capture.

Report (2026-11-06, Mac): during a recording the window showed flat meters, a
blank transcript and "No desktop audio for a minute" while the tracks and the
database kept growing. _push had dropped the page's queue when it filled, but its
stream stayed open sending heartbeats, so EventSource never reported an error and
the page never reconnected.

In the same session a double click on Test opened two captures. The second
replaced the first in _state, so nothing could stop it, and its ffmpeg kept the
microphone open until the app quit.

Run: .venv/bin/python -m pytest tests/test_sse_slow_client_and_test_start.py
"""
from __future__ import annotations

import ast
import queue
import threading
import time
from pathlib import Path

import pytest
from flask import Flask, Response

from core import paths

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "ui_web/static/app.js").read_text(encoding="utf-8")


def _load(names: set[str], scope: dict) -> dict:
    """Run these top-level app.py functions, without their route decorators,
    in a scope of stand-ins, so the test needs no audio or ML stack."""
    fns = [n for n in ast.parse(APP_PY).body
           if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in fns} == names
    for fn in fns:
        fn.decorator_list = []
    exec(compile(ast.Module(body=fns, type_ignores=[]), "<app>", "exec"), scope)
    return scope


class _Log:
    def __init__(self):
        self.warnings = []

    def warn(self, tag, msg):
        self.warnings.append((tag, msg))


@pytest.fixture()
def sse():
    scope = {
        "queue": queue,
        "json": __import__("json"),
        "log": _Log(),
        "_client_queues": {},
        "_cq_lock": threading.Lock(),
        "_refresh_tray": lambda: None,
    }
    return _load({"_end_sse_stream", "_queue_for_client", "_sse_messages", "_push"}, scope)


def _fill(sse, cid="slow", maxsize=3) -> queue.Queue:
    q = queue.Queue(maxsize=maxsize)
    sse["_client_queues"][cid] = q
    for i in range(maxsize):
        sse["_push"]("audio_level", {"n": i})
    assert q.full() and cid in sse["_client_queues"]
    return q


def test_a_client_that_falls_behind_is_dropped_and_its_stream_ends(sse):
    q = _fill(sse)
    sse["_push"]("audio_level", {"n": 99})

    assert "slow" not in sse["_client_queues"]
    assert [tag for tag, _ in sse["log"].warnings] == ["sse"]
    # The stream finishes. Before the fix this looped on heartbeats and never
    # returned. Nothing still queued goes out: the page replays from the last
    # line it saw, so a later line sent after a dropped one would hide the gap.
    assert list(sse["_sse_messages"](q)) == []


def test_a_transcript_line_is_never_sent_after_an_earlier_one_was_dropped(sse):
    q = queue.Queue(maxsize=3)
    sse["_client_queues"]["slow"] = q
    sse["_push"]("transcript", {"seg_id": 100})
    sse["_push"]("transcript", {"seg_id": 101})
    sse["_push"]("transcript", {"seg_id": 102})
    sse["_push"]("transcript", {"seg_id": 103})    # overflows
    assert not any("seg_id" in m for m in sse["_sse_messages"](q))


def test_a_handshake_event_never_blocks_on_a_full_queue(sse):
    q = queue.Queue(maxsize=1)
    q.put_nowait("event: x\n\n")
    sse["_queue_for_client"](q, "event: y\n\n")    # would hang with q.put
    assert q.get_nowait() == "event: x\n\n"


def test_other_clients_keep_their_stream(sse):
    _fill(sse, maxsize=2)
    healthy = queue.Queue(maxsize=50)
    sse["_client_queues"]["healthy"] = healthy
    sse["_push"]("status", {"ok": True})

    assert "healthy" in sse["_client_queues"]
    assert healthy.get_nowait().startswith("event: status")
    assert healthy.empty()


def test_an_idle_stream_still_sends_heartbeats(sse):
    class _IdleQueue:
        def __init__(self):
            self.calls = 0

        def get(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise queue.Empty
            return None

    assert list(sse["_sse_messages"](_IdleQueue())) == [": heartbeat\n\n"]


def test_a_real_http_stream_closes_so_the_browser_can_reconnect(sse):
    """Over real HTTP: the response ends, which is what fires EventSource.onerror."""
    from werkzeug.serving import make_server
    import http.client

    q = queue.Queue(maxsize=3)
    sse["_client_queues"]["page"] = q
    # The real route queues a status event first; headers go out with it.
    q.put("event: status\ndata: {}\n\n")
    connected = threading.Event()
    app = Flask(__name__)

    @app.route("/events")
    def events():
        def generate():
            connected.set()
            yield from sse["_sse_messages"](q)
        return Response(generate(), mimetype="text/event-stream")

    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        conn.request("GET", "/events")
        resp = conn.getresponse()
        assert connected.wait(5)
        # Keep the queue full faster than the server can drain it, until _push
        # gives up on this client.
        deadline = time.monotonic() + 5
        while "page" in sse["_client_queues"] and time.monotonic() < deadline:
            sse["_push"]("audio_level", {"x": 1})
        assert "page" not in sse["_client_queues"]
        # read() only returns once the server has ended the response. Before the
        # fix it would still be waiting here on heartbeats when the client timed out.
        assert b"event: status" in resp.read()
        conn.close()
    finally:
        server.shutdown()


class _FakeCapture:
    created: list["_FakeCapture"] = []
    release_start: threading.Event | None = None
    entered_start: threading.Event | None = None
    start_error: Exception | None = None

    def __init__(self, q):
        self.stopped = []
        type(self).created.append(self)

    def start(self, **kwargs):
        cls = type(self)
        if cls.entered_start:
            cls.entered_start.set()
        if cls.release_start:
            assert cls.release_start.wait(5)
        if cls.start_error:
            raise cls.start_error

    def stop(self, encode_per_source=True):
        self.stopped.append(encode_per_source)


class _Request:
    @staticmethod
    def get_json(silent=False):
        return {}


@pytest.fixture()
def audio_test(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    _FakeCapture.created = []
    _FakeCapture.release_start = None
    _FakeCapture.entered_start = None
    _FakeCapture.start_error = None
    pushed: list[tuple] = []
    scope = {
        "queue": queue,
        "time": time,
        "log": _Log(),
        "request": _Request,
        "AudioCapture": _FakeCapture,
        "jsonify": lambda obj: obj,
        "_push": lambda event, data: pushed.append((event, data)),
        "_state_lock": threading.Lock(),
        "_state": {
            "is_recording": False, "is_testing": False, "is_starting": False,
            "test_capture": None, "test_start": None,
        },
        "pushed": pushed,
    }
    return _load({"start_audio_test", "_stop_unused_capture"}, scope)


def test_a_second_start_during_the_first_opens_no_second_capture(audio_test):
    _FakeCapture.entered_start = threading.Event()
    _FakeCapture.release_start = threading.Event()
    first: list = []
    t = threading.Thread(target=lambda: first.append(audio_test["start_audio_test"]()))
    t.start()
    assert _FakeCapture.entered_start.wait(5)

    second = audio_test["start_audio_test"]()
    assert second == ({"error": "Already testing"}, 400)
    assert len(_FakeCapture.created) == 1

    _FakeCapture.release_start.set()
    t.join(5)
    assert first == [{"ok": True}]
    assert audio_test["_state"]["test_capture"] is _FakeCapture.created[0]
    assert audio_test["_state"]["is_testing"] is True
    assert audio_test["_state"]["test_start"] is None


def test_a_failed_start_closes_the_capture_and_allows_another_try(audio_test):
    _FakeCapture.start_error = RuntimeError("no device")
    body, code = audio_test["start_audio_test"]()
    assert code == 500 and "no device" in body["error"]
    assert _FakeCapture.created[0].stopped == [False]
    assert audio_test["_state"]["test_capture"] is None
    assert audio_test["_state"]["is_testing"] is False
    assert audio_test["_state"]["test_start"] is None

    _FakeCapture.start_error = None
    assert audio_test["start_audio_test"]() == {"ok": True}


def test_a_recording_that_starts_meanwhile_closes_the_test_capture(audio_test):
    _FakeCapture.entered_start = threading.Event()
    _FakeCapture.release_start = threading.Event()
    result: list = []
    t = threading.Thread(target=lambda: result.append(audio_test["start_audio_test"]()))
    t.start()
    assert _FakeCapture.entered_start.wait(5)
    audio_test["_state"]["is_starting"] = True
    _FakeCapture.release_start.set()
    t.join(5)

    assert result == [({"error": "Cannot test while recording"}, 400)]
    assert _FakeCapture.created[0].stopped == [False]
    assert audio_test["_state"]["test_capture"] is None
    assert audio_test["_state"]["is_testing"] is False
    assert audio_test["pushed"] == []


def test_a_start_is_refused_while_a_recording_is_starting(audio_test):
    audio_test["_state"]["is_starting"] = True
    assert audio_test["start_audio_test"]() == ({"error": "Cannot test while recording"}, 400)
    assert _FakeCapture.created == []


def test_a_wedged_reservation_does_not_block_tests_forever(audio_test):
    audio_test["_state"]["test_start"] = {"at": time.monotonic() - 61.0}
    assert audio_test["start_audio_test"]() == {"ok": True}


def test_a_wedged_start_that_returns_late_cannot_replace_the_newer_test(audio_test):
    _FakeCapture.entered_start = threading.Event()
    _FakeCapture.release_start = threading.Event()
    old: list = []
    t = threading.Thread(target=lambda: old.append(audio_test["start_audio_test"]()))
    t.start()
    assert _FakeCapture.entered_start.wait(5)
    wedged = _FakeCapture.created[0]

    # A minute on, a new start takes over and goes live.
    audio_test["_state"]["test_start"]["at"] -= 61.0
    _FakeCapture.release_start.set()      # also lets the newer start through
    assert audio_test["start_audio_test"]() == {"ok": True}
    newer = _FakeCapture.created[1]
    t.join(5)

    assert old == [({"error": "Already testing"}, 400)]
    assert wedged.stopped == [False]
    assert newer.stopped == []
    assert audio_test["_state"]["test_capture"] is newer


def test_a_late_start_leaves_a_newer_reservation_in_place(audio_test):
    _FakeCapture.entered_start = threading.Event()
    _FakeCapture.release_start = threading.Event()
    t = threading.Thread(target=audio_test["start_audio_test"])
    t.start()
    assert _FakeCapture.entered_start.wait(5)
    newer = {"at": time.monotonic()}
    audio_test["_state"]["test_start"] = newer
    _FakeCapture.release_start.set()
    t.join(5)

    assert audio_test["_state"]["test_start"] is newer
    assert audio_test["_state"]["test_capture"] is None


def test_a_stop_that_raises_is_logged_not_raised(audio_test):
    class _Bad:
        def stop(self, encode_per_source=True):
            raise OSError("gone")

    audio_test["_stop_unused_capture"](_Bad())
    assert audio_test["log"].warnings[0][0] == "audio"


def test_the_start_path_stops_a_capture_it_opened_but_never_went_live_with():
    src = APP_PY[APP_PY.index("def start_recording():"):]
    src = src[:src.index("def _settle_video_offset")]
    assert src.index("opened_capture = None\n    try:") < src.index("capture.start(")
    assert src.index("capture.start(") < src.index("opened_capture = capture")
    assert src.index('"is_recording": True') < src.index("opened_capture = None  # the recording")
    finally_block = src[src.rindex("\n    finally:"):]
    assert "_stop_unused_capture(opened_capture)" in finally_block
    # Stopped before the reservation is released, and released even if stopping fails.
    assert finally_block.index("_stop_unused_capture") < finally_block.index('"is_starting"] = False')
    assert "        finally:" in finally_block


def test_the_handshake_cannot_block_on_the_clients_queue():
    src = APP_PY[APP_PY.index('@app.route("/api/events")'):]
    src = src[:src.index("def generate():")]
    assert "q.put(" not in src


def test_the_test_button_ignores_a_click_while_its_request_is_out():
    fn = APP_JS[APP_JS.index("async function toggleAudioTest()"):]
    fn = fn[:fn.index("async function _toggleAudioTestNow()")]
    assert "if (_audioTestBusy) return;" in fn
    assert "finally" in fn and "_audioTestBusy = false" in fn
    # A start that never answers must not leave the button dead for good.
    start = APP_JS[APP_JS.index("fetch('/api/audio/test/start'"):][:500]
    assert "AbortSignal.timeout(" in start
