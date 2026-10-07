"""The microphone follows the same rule as the desktop device: the mic the user
selected is the one recorded, the default mic stands in only while it is
unavailable (missing at start, or unplugged mid-recording), and the capture
goes back to it as soon as it is there again.

Before 2026-10-07 a selected mic missing at start was replaced by the default
mic for the whole recording, and a mic lost mid-recording stayed silent until
the next one. No hardware: a fake ffmpeg (a Python child) streams silence the
way `ffmpeg -f dshow` does, or exits at once like ffmpeg on a missing device.
"""
import inspect
import queue
import subprocess
import sys
import threading
import time

import pytest

if sys.platform != "win32":
    pytest.skip("DirectShow mic capture is Windows-only", allow_module_level=True)
windows = pytest.importorskip("capture_audio.windows")

SELECTED = "Microphone (Arctis Nova Pro Wireless)"
LAPTOP = "Microphone Array (Realtek(R) Audio)"

FAKE_FFMPEG = r"""
import sys, time
if sys.argv[1] == "fail":
    sys.stderr.write("Could not find audio only device with name\n")
    sys.exit(1)
out = sys.stdout.buffer
chunk = b"\x01\x00" * 480          # 10 ms of 48 kHz mono s16le
due = time.perf_counter()
while True:
    out.write(chunk)
    out.flush()
    due += 0.01
    wait = due - time.perf_counter()
    if wait > 0:
        time.sleep(wait)
"""


def _fake_ffmpeg(mode: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", FAKE_FFMPEG, mode],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW)


class _FakeMicStream:
    """A WASAPI mic stream: always has a chunk ready, like a live mic."""

    def __init__(self):
        self.closed = False

    def get_read_available(self):
        time.sleep(0.01)
        return windows.AudioCapture.CHUNK_SIZE

    def read(self, frames, exception_on_overflow=False):
        return b"\x00\x00" * frames

    def close(self):
        self.closed = True


@pytest.fixture
def cap(monkeypatch):
    import capture_video
    monkeypatch.setattr(capture_video, "find_ffmpeg", lambda: "ffmpeg")
    c = windows.AudioCapture(queue.Queue(maxsize=100))
    c.sample_rate = 48000
    c.is_running = True
    c._selected_mic_name = SELECTED
    yield c
    c.is_running = False
    for proc in (c._ffmpeg_proc,):
        if proc is not None and proc.poll() is None:
            proc.kill()


def _start_wasapi_stand_in(c):
    """What a start with the selected mic missing leaves: the default mic,
    captured in-process (the existing fallback in _open_devices)."""
    stream = _FakeMicStream()
    c._mic_stream = stream
    c._mic_on_selected = False
    c._set_mic_format(44100, 2)
    c._mic_last_data_ts = time.monotonic()
    c._mic_thread = threading.Thread(target=c._capture_loop, args=(stream, c._mic_q),
                                     daemon=True)
    c._mic_thread.start()
    return stream


# ── The decision ─────────────────────────────────────────────────────────────

def test_the_selected_mic_is_picked_back_up_when_it_is_available(cap, monkeypatch):
    _start_wasapi_stand_in(cap)
    available = {"now": False}
    monkeypatch.setattr(windows, "resolve_dshow_mic_name",
                        lambda name: (SELECTED, "exact") if available["now"] else (None, "no-match"))
    calls = []
    monkeypatch.setattr(cap, "_switch_mic_to_dshow",
                        lambda name, selected, reason: calls.append((name, selected)) or True)
    assert cap._check_selected_mic() is None, "the stand-in is live: wait for the selected mic"
    assert calls == []
    available["now"] = True
    assert cap._check_selected_mic() is True
    assert calls == [(SELECTED, True)]


def test_a_mic_lost_mid_recording_gets_the_default_one_standing_in(cap, monkeypatch):
    dead = _fake_ffmpeg("fail")
    dead.wait(5)
    cap._ffmpeg_proc = dead              # the selected mic's ffmpeg exited: unplugged
    cap._mic_on_selected = True
    monkeypatch.setattr(windows, "resolve_dshow_mic_name", lambda name: (None, "no-match"))
    monkeypatch.setattr(cap, "_default_mic_dshow_name", lambda: LAPTOP)
    calls = []
    monkeypatch.setattr(cap, "_switch_mic_to_dshow",
                        lambda name, selected, reason: calls.append((name, selected)) or True)
    assert not cap._mic_is_on_selected()
    assert cap._check_selected_mic() is True
    assert calls == [(LAPTOP, False)]


def test_a_mic_that_stops_sending_counts_as_lost(cap):
    proc = _fake_ffmpeg("ok")            # still running, but nothing arrives
    try:
        cap._ffmpeg_proc = proc
        cap._mic_on_selected = True
        cap._mic_last_data_ts = time.monotonic()
        assert cap._mic_is_on_selected()
        cap._mic_last_data_ts = time.monotonic() - cap.MIC_DEAD_AFTER_SEC - 1
        assert not cap._mic_is_on_selected()
    finally:
        proc.kill()


def test_nothing_happens_without_a_selected_mic(cap, monkeypatch):
    cap._selected_mic_name = None        # the default mic, None, or a legacy choice
    monkeypatch.setattr(windows, "resolve_dshow_mic_name",
                        lambda name: pytest.fail("must not look for a mic"))
    assert cap._check_selected_mic() is None


# ── The switch ───────────────────────────────────────────────────────────────

def test_switching_replaces_the_source_and_the_new_mic_is_recorded(cap, monkeypatch):
    old = _start_wasapi_stand_in(cap)
    old_reader = cap._mic_thread
    monkeypatch.setattr(cap, "_spawn_ffmpeg_mic", lambda path, name: _fake_ffmpeg("ok"))
    assert cap._switch_mic_to_dshow(SELECTED, selected=True, reason="the selected microphone")
    assert cap._ffmpeg_proc is not None and cap._ffmpeg_proc.poll() is None
    assert cap._mic_stream is None and old.closed, "the stand-in is retired"
    assert not old_reader.is_alive()
    assert (cap._mic_rate, cap._mic_channels) == (48000, 1)
    assert (cap._resample_up, cap._resample_down) == (1, 1)
    assert cap._mic_on_selected and cap._mic_device_name == SELECTED
    # Audio from the new mic reaches the mixer's queue.
    while not cap._mic_q.empty():
        cap._mic_q.get_nowait()
    got = cap._mic_q.get(timeout=2)
    assert got == b"\x01\x00" * (len(got) // 2)
    assert cap._mic_is_on_selected()


def test_a_mic_that_will_not_open_leaves_the_old_source_alone(cap, monkeypatch):
    old = _start_wasapi_stand_in(cap)
    monkeypatch.setattr(cap, "_spawn_ffmpeg_mic", lambda path, name: _fake_ffmpeg("fail"))
    assert cap._switch_mic_to_dshow(SELECTED, selected=True, reason="x") is False
    assert cap._mic_stream is old and not old.closed
    assert cap._mic_thread.is_alive()
    assert cap._ffmpeg_proc is None
    assert (cap._mic_rate, cap._mic_channels) == (44100, 2)


def test_stop_ends_a_switched_mic(cap, monkeypatch):
    _start_wasapi_stand_in(cap)
    monkeypatch.setattr(cap, "_spawn_ffmpeg_mic", lambda path, name: _fake_ffmpeg("ok"))
    assert cap._switch_mic_to_dshow(SELECTED, selected=True, reason="x")
    proc = cap._ffmpeg_proc
    cap.stop(encode_per_source=False)
    assert proc.wait(timeout=5) is not None, "the switched-to ffmpeg must end with the capture"


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_start_remembers_the_selected_mic_and_the_watchdog_checks_it():
    opening = inspect.getsource(windows.AudioCapture._open_devices)
    dshow = opening[opening.index("if mic_index == -3:"):opening.index("elif mic_index == -2:")]
    assert "self._selected_mic_name = ffmpeg_mic_name or None" in dshow
    assert "self._mic_on_selected = True" in dshow
    watchdog = inspect.getsource(windows.AudioCapture._loopback_silence_watchdog)
    assert "self._mic_is_on_selected()" in watchdog
    assert "self._check_selected_mic()" in watchdog
