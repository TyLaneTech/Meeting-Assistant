"""The desktop (loopback) audio is captured by a helper process that scans the
devices Windows has right now.

2026-10-06: a Teams call played on "Headphones (Realtek(R) Audio)". The render
probe heard it, but the app's PortAudio device list (scanned once per process,
and reference counted, so a "fresh" PyAudio() returned the same cached list)
only held the speakers. The switch found no matching device and the whole call
was recorded as Pat alone. Every recording and every switch now starts a new
helper, whose scan is what the device is resolved against.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("WASAPI loopback capture is Windows-only", allow_module_level=True)
windows = pytest.importorskip("capture_audio.windows")

SPEAKERS_OUT = {"index": 5, "name": "Speakers (Realtek(R) Audio)", "hostApi": 2,
                "maxInputChannels": 0, "isLoopbackDevice": False,
                "defaultSampleRate": 48000.0}
SPEAKERS_LB = {"index": 13, "name": "Speakers (Realtek(R) Audio) [Loopback]", "hostApi": 2,
               "maxInputChannels": 2, "isLoopbackDevice": True,
               "defaultSampleRate": 48000.0}
HEADPHONES_OUT = {"index": 6, "name": "Headphones (Realtek(R) Audio)", "hostApi": 2,
                  "maxInputChannels": 0, "isLoopbackDevice": False,
                  "defaultSampleRate": 48000.0}
HEADPHONES_LB = {"index": 16, "name": "Headphones (Realtek(R) Audio) [Loopback]", "hostApi": 2,
                 "maxInputChannels": 2, "isLoopbackDevice": True,
                 "defaultSampleRate": 48000.0}
MME_SPEAKERS = {"index": 1, "name": "Speakers (Realtek(R) Audio)", "hostApi": 0,
                "maxInputChannels": 0, "isLoopbackDevice": False,
                "defaultSampleRate": 44100.0}


def _report(devices, default_output):
    return {"ok": True,
            "wasapi": {"index": 2, "defaultOutputDevice": default_output,
                       "defaultInputDevice": -1},
            "devices": devices}


def test_snapshot_answers_like_pyaudio():
    snap = windows._DeviceSnapshot(_report(
        [MME_SPEAKERS, SPEAKERS_OUT, HEADPHONES_OUT, SPEAKERS_LB, HEADPHONES_LB], 6))
    assert snap.get_host_api_info_by_type(None)["defaultOutputDevice"] == 6
    assert snap.get_device_info_by_index(6)["name"] == HEADPHONES_OUT["name"]
    assert [d["name"] for d in snap.get_loopback_device_info_generator()] == [
        SPEAKERS_LB["name"], HEADPHONES_LB["name"]]
    with pytest.raises(IOError):
        snap.get_device_info_by_index(99)


class _FakeChild(windows._LoopbackChild):
    """Stands in for a helper process: a device scan plus an open() that records
    what it was asked to capture."""
    instances: list = []

    def __init__(self, report):  # no process
        self._dead = False
        self.devices = windows._DeviceSnapshot(report)
        self.opened = None
        self.closed = False
        self.channels = 1
        _FakeChild.instances.append(self)

    def open(self, info, channels, rate, frames_per_buffer, chunk):
        self.opened = info["name"]
        self.channels = channels

    def get_read_available(self):
        return 0

    def read(self, frames, exception_on_overflow=False):
        threading.Event().wait(0.01)
        return b"\x00" * frames * self.channels * 2

    @property
    def dead(self):
        return self.closed

    def close(self):
        self.closed = True
        self._dead = True


def _recording(stale_devices):
    """An AudioCapture mid-recording on the speakers, whose own device list
    predates the headphones."""
    cap = windows.AudioCapture.__new__(windows.AudioCapture)
    cap.is_running = True
    cap._loopback_restart_lock = threading.Lock()
    cap._devices = windows._DeviceSnapshot(stale_devices)
    cap._loopback_device_name = SPEAKERS_LB["name"]
    cap._loopback_channels = 2
    cap.sample_rate = 48000
    cap._loopback_stream = _FakeChild(stale_devices)
    cap._loopback_thread = None
    cap._loopback_q = __import__("queue").Queue(maxsize=200)
    cap._loopback_verified = True
    cap.loopback_had_signal = False
    cap.loopback_level = 0.0
    cap.loopback_peak = 0.0
    cap._loopback_pa = None
    cap._pa = None
    # Read by the capture loop in builds with the microphone supervisor.
    cap.mic_health = __import__("types").SimpleNamespace(generation=0)
    cap._selected_mic_name = None   # the mic part of the watchdog has nothing to do
    cap.CHUNK_SIZE = 512
    return cap


def test_switch_finds_an_output_the_recording_never_knew(monkeypatch):
    stale = _report([SPEAKERS_OUT, SPEAKERS_LB], 5)
    fresh = _report([SPEAKERS_OUT, HEADPHONES_OUT, SPEAKERS_LB, HEADPHONES_LB], 6)
    cap = _recording(stale)
    old = cap._loopback_stream
    # The recording's own list has no headphones: the old lookup failed here.
    assert cap._match_loopback_by_name("Headphones (Realtek(R) Audio)", strict=True) is None

    monkeypatch.setattr(cap, "_new_loopback_child", lambda: _FakeChild(fresh))
    assert cap.restart_loopback(target_name="Headphones (Realtek(R) Audio)") is True
    try:
        assert cap._loopback_device_name == HEADPHONES_LB["name"]
        assert cap._loopback_stream.opened == HEADPHONES_LB["name"]
        assert old.closed, "the old helper must be killed once the new one captures"
        # The watchdog's cheap check now sees the headphones too.
        assert cap._match_loopback_by_name("Headphones (Realtek(R) Audio)", strict=True)
    finally:
        cap.is_running = False


def test_switch_with_no_matching_output_stays_put_and_kills_the_helper(monkeypatch):
    stale = _report([SPEAKERS_OUT, SPEAKERS_LB], 5)
    cap = _recording(stale)
    old = cap._loopback_stream
    _FakeChild.instances.clear()
    monkeypatch.setattr(cap, "_new_loopback_child", lambda: _FakeChild(stale))
    assert cap.restart_loopback(target_name="Headphones (Realtek(R) Audio)") is False
    assert cap._loopback_stream is old and not old.closed
    assert _FakeChild.instances[-1].closed


def test_reopen_same_restarts_a_dead_helper_on_the_same_output(monkeypatch):
    stale = _report([SPEAKERS_OUT, SPEAKERS_LB], 5)
    cap = _recording(stale)
    old = cap._loopback_stream
    old.closed = True   # the helper died
    monkeypatch.setattr(cap, "_new_loopback_child", lambda: _FakeChild(stale))
    assert cap.restart_loopback(target_name="Speakers (Realtek(R) Audio)") is False
    try:
        assert cap.restart_loopback(target_name="Speakers (Realtek(R) Audio)",
                                    reopen_same=True) is True
        assert cap._loopback_stream is not old
        assert cap._loopback_stream.opened == SPEAKERS_LB["name"]
    finally:
        cap.is_running = False


# ── Back to the selected device ─────────────────────────────────────────────
# The default output stands in for the device the user selected only while
# that device is unavailable (missing at start, or gone mid-recording); the
# capture goes back to it as soon as it exists again (2026-10-07, review of
# PR 1086: the stand-in used to be kept for the rest of the recording).

def _resolver(devices):
    cap = windows.AudioCapture.__new__(windows.AudioCapture)
    cap._devices = windows._DeviceSnapshot(devices)
    return cap


def test_start_remembers_the_selected_device(monkeypatch):
    monkeypatch.setattr(windows, "_follow_output_enabled", lambda: False)
    present = _resolver(_report([SPEAKERS_OUT, HEADPHONES_OUT, SPEAKERS_LB, HEADPHONES_LB], 5))
    assert present._resolve_loopback(16, HEADPHONES_LB["name"])["name"] == HEADPHONES_LB["name"]
    assert present._selected_loopback_name == HEADPHONES_LB["name"]
    # Missing at start: the default output stands in, and the saved name is
    # what the watchdog waits for.
    missing = _resolver(_report([SPEAKERS_OUT, SPEAKERS_LB], 5))
    assert missing._resolve_loopback(16, HEADPHONES_LB["name"])["name"] == SPEAKERS_LB["name"]
    assert missing._selected_loopback_name == HEADPHONES_LB["name"]
    # No selection at all: nothing to return to.
    missing._resolve_loopback(None, None)
    assert missing._selected_loopback_name is None


def test_follow_call_audio_leaves_the_device_to_following(monkeypatch):
    monkeypatch.setattr(windows, "_follow_output_enabled", lambda: True)
    cap = _resolver(_report([SPEAKERS_OUT, SPEAKERS_LB], 5))
    cap._resolve_loopback(16, HEADPHONES_LB["name"])
    assert cap._selected_loopback_name is None


def test_capture_returns_to_the_selected_device_once_it_is_back(monkeypatch):
    without = _report([SPEAKERS_OUT, SPEAKERS_LB], 5)
    back = _report([SPEAKERS_OUT, HEADPHONES_OUT, SPEAKERS_LB, HEADPHONES_LB], 6)
    cap = _recording(without)          # on the speakers, standing in
    cap._selected_loopback_name = HEADPHONES_LB["name"]
    old = cap._loopback_stream
    scans = [None, windows._DeviceSnapshot(without), windows._DeviceSnapshot(back)]
    monkeypatch.setattr(cap, "_scan_devices_now", lambda: scans.pop(0))
    monkeypatch.setattr(cap, "_new_loopback_child", lambda: _FakeChild(back))
    try:
        assert cap._return_to_selected_device() is None   # no scan available yet
        assert cap._return_to_selected_device() is None   # still unplugged
        assert cap._loopback_stream is old and not old.closed
        assert cap._return_to_selected_device() is True   # plugged back in
        assert cap._loopback_device_name == HEADPHONES_LB["name"]
        assert cap._loopback_stream.opened == HEADPHONES_LB["name"]
        assert old.closed, "the stand-in's helper is retired once the selected one captures"
        assert cap._return_to_selected_device() is None   # nothing left to do
    finally:
        cap.is_running = False


def test_the_watchdog_checks_for_the_selected_device():
    import inspect
    body = inspect.getsource(windows.AudioCapture._loopback_silence_watchdog)
    call = body[body.index("self._return_to_selected_device()"):]
    switched = call[:call.index("continue")]
    # A switch back gives the device its own grace, like every other switch.
    assert "last_signal_ts = last_recover_ts = grace_base" in switched
    assert "mic_active_for = 0.0" in switched


# ── Helpers never outlive the capture ───────────────────────────────────────

def test_a_failed_start_closes_its_helper(monkeypatch):
    cap = windows.AudioCapture.__new__(windows.AudioCapture)
    cap._pa = None
    child = _FakeChild(_report([], -1))   # no default output, no loopback device
    monkeypatch.setattr(cap, "_new_loopback_child", lambda: child)
    with pytest.raises(Exception):
        cap._open_start_loopback(None, None)
    assert child.closed, "the helper used to wait for a command until the app exited"


def test_a_switch_that_lands_after_stop_is_not_published(monkeypatch):
    stale = _report([SPEAKERS_OUT, SPEAKERS_LB], 5)
    fresh = _report([SPEAKERS_OUT, HEADPHONES_OUT, SPEAKERS_LB, HEADPHONES_LB], 6)
    cap = _recording(stale)
    old = cap._loopback_stream

    class _StoppedWhileOpening(_FakeChild):
        def open(self, *args, **kwargs):
            super().open(*args, **kwargs)
            cap.is_running = False   # stop() ran while this helper was starting

    child = _StoppedWhileOpening(fresh)
    monkeypatch.setattr(cap, "_new_loopback_child", lambda: child)
    assert cap.restart_loopback(target_name="Headphones (Realtek(R) Audio)") is False
    assert child.closed
    assert cap._loopback_stream is old


# ── The pipe keeps up ───────────────────────────────────────────────────────

FAKE_PORTAUDIO = Path(__file__).parent / "fake_portaudio"


def test_the_helper_catches_up_after_the_app_stalls(monkeypatch):
    """The pipe holds one pending write at a time. With one chunk per write an
    8-channel 96 kHz output drained at barely over real time, and a one second
    stall in the app left it 0.6 s behind the mic eight seconds later. The
    helper now sends its backlog in one write."""
    monkeypatch.setenv("PYTHONPATH", str(FAKE_PORTAUDIO))
    monkeypatch.setenv("FAKE_RATE", "96000")
    monkeypatch.setenv("FAKE_CH", "8")
    child = windows._LoopbackChild()
    try:
        info = next(child.devices.get_loopback_device_info_generator())
        child.open(info, channels=8, rate=96000, frames_per_buffer=2048, chunk=512)
        t0 = time.monotonic()
        got = 0

        def drain(seconds):   # the capture loop's own pattern
            nonlocal got
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                avail = child.get_read_available()
                if avail < 512:
                    time.sleep(0.005)
                    continue
                got += len(child.read(min(avail, 2048))) // 16

        drain(1.0)
        time.sleep(1.0)        # the app is busy and reads nothing
        drain(1.5)
        behind = (time.monotonic() - t0) - got / 96000
        assert behind < 0.3, f"still {behind:.2f} s behind 1.5 s after a 1 s stall"
    finally:
        child.close()


# ── Real hardware ───────────────────────────────────────────────────────────

def _real_default_loopback():
    snap = windows._fresh_device_snapshot()
    if snap is None:
        pytest.skip("the helper could not scan devices")
    loopbacks = list(snap.get_loopback_device_info_generator())
    if not loopbacks:
        pytest.skip("no WASAPI loopback device on this machine")
    return snap


def test_helper_scan_matches_an_in_process_scan():
    snap = _real_default_loopback()
    pa = windows.pyaudio.PyAudio()
    try:
        here = sorted(d["name"] for d in pa.get_loopback_device_info_generator())
    finally:
        pa.terminate()
    assert sorted(d["name"] for d in snap.get_loopback_device_info_generator()) == here


def test_helper_streams_pcm_from_the_default_output_and_dies_on_close():
    _real_default_loopback()
    child = windows._LoopbackChild()
    cap = windows.AudioCapture.__new__(windows.AudioCapture)
    cap._devices = child.devices
    info = cap._find_loopback_device()
    ch = max(1, info["maxInputChannels"])
    child.open(info, channels=ch, rate=int(info["defaultSampleRate"]),
               frames_per_buffer=2048, chunk=512)
    try:
        # A loopback on an idle output may deliver nothing, so read on a thread.
        got = []
        t = threading.Thread(target=lambda: got.append(child.read(512)), daemon=True)
        t.start()
        t.join(5)
        if got:
            assert len(got[0]) == 512 * ch * 2
        assert not child.dead
    finally:
        child.close()
    assert child.dead
    with pytest.raises(IOError):
        child.read(512)


def test_warm_spare_rescans_and_opens_without_a_new_process():
    _real_default_loopback()
    windows.prewarm_loopback_helper()
    import time
    deadline = time.monotonic() + 30
    while windows._spare is None and time.monotonic() < deadline:
        time.sleep(0.1)
    spare = windows._spare
    assert spare is not None, "the spare helper never started"
    pid = spare._proc.pid
    child = windows._take_loopback_child()
    try:
        assert child._proc.pid == pid, "the warm spare should be used, not a new process"
        info = next(iter(child.devices.get_loopback_device_info_generator()))
        child.open(info, channels=max(1, info["maxInputChannels"]),
                   rate=int(info["defaultSampleRate"]), frames_per_buffer=2048, chunk=512)
        assert not child.dead
    finally:
        child.close()
