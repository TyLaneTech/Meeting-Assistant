"""A stopped capture releases PortAudio, so the next one sees today's devices.

PortAudio enumerates devices only on the Pa_Initialize() that takes its
reference count from zero; every other PyAudio() adds a reference to the list
already there. Stopped captures used to be parked for the life of the process,
because closing a stream while a thread was blocked in read() on it killed the
process, and that froze the device list at the first recording. A headset
unplugged after it then failed to open with "[Errno -9992] Insufficient memory"
(PortAudio's WASAPI host reports a failed IMMDevice::Activate that way) until
the app was restarted (2026-09-24).

The capture loops now poll instead of waiting inside read(), so stop() can
close the streams and terminate PyAudio; only a stream whose reader is still
alive is parked. The fake stream below blocks in read() the way a silent WASAPI
loopback does, so a loop that waits inside read() fails these tests.
"""
import queue
import sys
import threading
import time

import pytest

if sys.platform != "win32":
    pytest.skip("WASAPI capture is Windows-only", allow_module_level=True)
windows = pytest.importorskip("capture_audio.windows")


class _FakeStream:
    """Enough of a PyAudio input stream. read() of more than is buffered waits,
    like a WASAPI loopback whose output is silent."""
    def __init__(self, avail: int = 0):
        self.avail = avail
        self.reads: list[int] = []
        self.waited_in_read = False
        self.closed = False
        self._never = threading.Event()

    def get_read_available(self) -> int:
        return self.avail

    def read(self, n, exception_on_overflow=True):
        if n > self.avail:
            self.waited_in_read = True
            self._never.wait(timeout=2)
        self.reads.append(n)
        self.avail = max(0, self.avail - n)
        return b"\x00\x00" * n

    def close(self):
        self.closed = True


class _FakePA:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


class _StuckThread:
    """A reader thread that never left read()."""
    def join(self, timeout=None):
        pass

    def is_alive(self):
        return True


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(windows, "_stream_graveyard", [])
    from core import log as core_log
    monkeypatch.setattr(core_log, "_logs_dir", lambda: tmp_path)


def _capture():
    return windows.AudioCapture(queue.Queue(maxsize=100))


# ── The capture loop never waits inside read() ───────────────────────────────

def test_the_capture_loop_leaves_promptly_while_the_output_is_silent():
    cap, stream = _capture(), _FakeStream(avail=0)
    cap._loopback_stream = stream
    cap.is_running = True
    reader = threading.Thread(target=cap._capture_loop, args=(stream, queue.Queue()),
                              kwargs={"is_loopback": True}, daemon=True)
    reader.start()
    time.sleep(0.05)
    cap.is_running = False
    reader.join(timeout=0.5)
    assert not reader.is_alive(), "the reader is stuck in read(); stop() could not close its stream"
    assert not stream.waited_in_read


def test_the_capture_loop_reads_only_what_is_buffered():
    chunk = windows.AudioCapture.CHUNK_SIZE
    cap, stream = _capture(), _FakeStream(avail=chunk // 2)
    out: queue.Queue = queue.Queue()
    cap.is_running = True
    # A mic reader serves the current mic stream and leaves once it is swapped
    # (a mic switch), like the loopback reader.
    cap._mic_stream = stream
    reader = threading.Thread(target=cap._capture_loop, args=(stream, out), daemon=True)
    reader.start()
    try:
        time.sleep(0.05)
        assert stream.reads == []            # under a chunk: nothing read yet
        stream.avail = 3 * chunk
        deadline = time.monotonic() + 1.0
        while not stream.reads and time.monotonic() < deadline:
            time.sleep(0.005)
        assert stream.reads == [3 * chunk]
        assert out.get_nowait() == b"\x00\x00" * (3 * chunk)
    finally:
        cap.is_running = False
        reader.join(timeout=0.5)
    assert not stream.waited_in_read


# ── Retiring a stream ────────────────────────────────────────────────────────

def test_a_stream_is_closed_once_its_reader_has_gone():
    stream, pa = _FakeStream(), _FakePA()
    done = threading.Thread(target=lambda: None)
    done.start(); done.join()
    windows._retire_stream(stream, done, pa)
    windows._terminate_quietly(pa)
    assert stream.closed and pa.terminated
    assert windows._stream_graveyard == []


def test_a_stream_still_being_read_is_parked_with_its_owner():
    stream, pa = _FakeStream(), _FakePA()
    windows._retire_stream(stream, _StuckThread(), pa)
    # terminate() would close every stream the owner opened, the parked one too
    windows._terminate_quietly(pa)
    assert not stream.closed and not pa.terminated
    assert any(o is stream for o in windows._stream_graveyard)
    assert any(o is pa for o in windows._stream_graveyard)


# ── stop() ───────────────────────────────────────────────────────────────────

def test_stop_closes_the_streams_and_releases_portaudio():
    cap, pa = _capture(), _FakePA()
    lb, mic = _FakeStream(), _FakeStream()
    cap._pa = cap._loopback_pa = pa
    cap._loopback_stream, cap._mic_stream = lb, mic
    cap.stop()
    assert lb.closed and mic.closed and pa.terminated
    assert windows._stream_graveyard == []
    assert cap._pa is None and cap._loopback_pa is None


def test_stop_after_a_device_switch_releases_both_pyaudio_instances():
    cap, main, switched = _capture(), _FakePA(), _FakePA()
    cap._pa, cap._loopback_pa = main, switched
    cap._loopback_stream = _FakeStream()
    cap.stop()
    assert main.terminated and switched.terminated


def test_stop_parks_a_stream_whose_reader_did_not_leave():
    cap, pa = _capture(), _FakePA()
    lb, mic = _FakeStream(), _FakeStream()
    cap._pa = cap._loopback_pa = pa
    cap._loopback_stream, cap._mic_stream = lb, mic
    cap._loopback_thread = _StuckThread()
    cap.stop()
    assert not lb.closed, "closing a stream under a blocked read() kills the process"
    assert mic.closed                     # its reader was gone
    assert not pa.terminated              # it owns the parked stream


# ── A device that will not open ──────────────────────────────────────────────

def test_a_device_that_will_not_open_releases_portaudio_and_says_why(monkeypatch):
    class _RefusingPA(_FakePA):
        def open(self, **kwargs):
            raise OSError(-9992, "Insufficient memory")   # a failed IMMDevice::Activate

    pa = _RefusingPA()
    monkeypatch.setattr(windows.pyaudio, "PyAudio", lambda: pa)
    headset = {"index": 23, "name": "Headphones (WH-1000XM4) [Loopback]",
               "defaultSampleRate": 48000, "maxInputChannels": 2}
    monkeypatch.setattr(windows.AudioCapture, "_resolve_loopback", lambda self, i, n: headset)

    cap = _capture()
    with pytest.raises(RuntimeError) as err:
        cap.start(loopback_index=23, mic_index=-1, loopback_name=headset["name"])

    message = str(err.value)
    assert "'Headphones (WH-1000XM4)'" in message
    assert "Insufficient memory" not in message
    assert "-9992" in message
    assert pa.terminated and cap._pa is None
    assert windows._stream_graveyard == []
