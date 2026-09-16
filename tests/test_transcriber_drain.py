"""Standalone test: the transcriber finishes its backlog instead of dropping it.

A pipeline slower than real time builds a queue of captured audio nobody has
transcribed yet. Stop used to clear is_running, which made the consumer loop
exit and take the whole backlog with it, so the tail of a long meeting was
deleted while the log reported a healthy segment count.

Run: .venv/Scripts/python.exe tests/test_transcriber_drain.py
"""
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ml.transcriber import Transcriber, TranscriptionQueue, make_audio_queue


CHUNK = b"\x00\x01" * Transcriber.CHUNK_SIZE   # one CHUNK_SIZE frame of int16


def _rig(chunk_count, gate=None):
    """A transcriber whose _transcribe only records what it was given.

    sample_rate is set to CHUNK_SIZE so one chunk is one second: it makes the
    buffer thresholds below readable and keeps the test off any real timing.
    ``gate`` is an Event the first flush waits on, which is how a backlog is
    built deterministically rather than by racing a sleep.
    """
    q = TranscriptionQueue(maxsize=0)
    seen = []
    lock = threading.Lock()
    started = threading.Event()

    t = Transcriber(q, lambda *a, **k: None)
    t.silence_threshold = 0.0      # every chunk counts as voice
    t.silence_duration = 1.0
    t.min_buffer_seconds = 1.0
    t.max_buffer_seconds = 2.0     # flush every 2 chunks

    def _fake_transcribe(buffer, source, start_time=0.0, end_time=0.0, **kw):
        started.set()
        if gate is not None:
            gate.wait(timeout=20)
        with lock:
            seen.append(len(buffer))

    t._transcribe = _fake_transcribe
    for i in range(chunk_count):
        q.put_nowait(("loopback", CHUNK, i * Transcriber.CHUNK_SIZE))
    return t, seen, lock, started


def _total(seen, lock):
    with lock:
        return sum(seen)


def test_drain_transcribes_the_whole_backlog():
    """begin_drain + await_drain must consume every queued chunk.

    This is the regression: before the drain, begin_drain's job was done by
    clearing is_running, and everything still queued was thrown away.
    """
    chunks = 40
    gate = threading.Event()
    t, seen, lock, started = _rig(chunks, gate=gate)
    t.start(sample_rate=Transcriber.CHUNK_SIZE, channels=1)
    assert started.wait(timeout=10), "the loop never reached the first flush"

    assert t.pending_seconds > 0, "the rig should still have a backlog to drain"

    t.begin_drain()
    gate.set()
    assert t.await_drain(timeout=20), "drain did not finish"

    assert _total(seen, lock) == chunks, (
        f"drain lost audio: transcribed {_total(seen, lock)} of {chunks} chunks")
    assert t.audio_queue.qsize() == 0, "queue should be empty after a drain"
    assert not t.is_draining, "drain flag should clear when the loop exits"
    t.stop()


def test_stop_without_drain_still_discards():
    """stop() stays immediate: the force-quit and takeover paths need it."""
    gate = threading.Event()
    t, seen, lock, started = _rig(400, gate=gate)
    t.start(sample_rate=Transcriber.CHUNK_SIZE, channels=1)
    assert started.wait(timeout=10)
    gate.set()
    t.stop()
    assert t.audio_queue.qsize() > 0, "stop() is supposed to leave the backlog"
    assert _total(seen, lock) < 400, "stop() is supposed to drop the backlog"


def test_a_new_recording_cancels_a_drain():
    """start() must take the queue back rather than wait on the old meeting."""
    gate = threading.Event()
    t, _seen, _lock, started = _rig(400, gate=gate)
    t.start(sample_rate=Transcriber.CHUNK_SIZE, channels=1)
    assert started.wait(timeout=10)
    t.begin_drain()
    assert t.is_draining
    gate.set()

    t.start(sample_rate=Transcriber.CHUNK_SIZE, channels=1)
    assert not t.is_draining, "a new recording must cancel the previous drain"
    assert t.is_running, "the new session's loop should be running"
    t.stop()


def test_a_loop_that_dies_still_releases_await_drain():
    """await_drain blocks the stop's deferred tail (title, export, chapters).

    A loop killed by an unhandled exception must still release it, or that
    thread waits for the life of the process instead of just losing the tail.
    """
    q = TranscriptionQueue(maxsize=0)
    t = Transcriber(q, lambda *a, **k: None)

    def _explode(*_a, **_k):
        raise RuntimeError("whisper fell over")

    t._transcribe = _explode
    t.silence_threshold = 0.0
    t.min_buffer_seconds = 1.0
    t.max_buffer_seconds = 2.0
    for i in range(10):
        q.put_nowait(("loopback", CHUNK, i * Transcriber.CHUNK_SIZE))

    t.start(sample_rate=Transcriber.CHUNK_SIZE, channels=1)
    t.begin_drain()
    assert t.await_drain(timeout=10), "a dead loop left await_drain hanging"
    assert not t.is_draining


def test_await_drain_returns_at_once_when_nothing_is_draining():
    t, _seen, _lock, _started = _rig(0)
    assert t.await_drain(timeout=0.1), "no drain means nothing to wait for"
    assert t.begin_drain() == 0.0, "no consumer thread means no drain to start"


def test_queue_is_bounded_and_counts_drops():
    """An unbounded feed is what let the backlog grow invisibly."""
    q = make_audio_queue(sample_rate=48_000)
    assert q.maxsize > 0, "the transcription feed must be bounded"

    small = TranscriptionQueue(maxsize=2)
    small.put_nowait(1)
    small.put_nowait(2)
    for _ in range(3):
        try:
            small.put_nowait(3)
        except queue.Full:
            pass
    assert small.dropped == 3, f"expected 3 counted drops, got {small.dropped}"


def test_pending_seconds_reads_the_backlog():
    q = TranscriptionQueue(maxsize=0)
    t = Transcriber(q, lambda *a, **k: None)
    t.sample_rate = 48_000
    per_second = 48_000 / Transcriber.CHUNK_SIZE
    for _ in range(int(per_second * 3)):
        q.put_nowait(("loopback", CHUNK, -1))
    assert 2.5 < t.pending_seconds < 3.5, (
        f"3 s of chunks should read as ~3 s, got {t.pending_seconds:.2f}")


if __name__ == "__main__":
    test_drain_transcribes_the_whole_backlog()
    test_stop_without_drain_still_discards()
    test_a_new_recording_cancels_a_drain()
    test_a_loop_that_dies_still_releases_await_drain()
    test_await_drain_returns_at_once_when_nothing_is_draining()
    test_queue_is_bounded_and_counts_drops()
    test_pending_seconds_reads_the_backlog()
    print("OK test_transcriber_drain")
