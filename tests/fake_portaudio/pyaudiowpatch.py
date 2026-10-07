"""A stand-in for PyAudioWPatch, for running capture_audio/loopback_child.py
without audio hardware (tests/test_loopback_helper.py puts this folder on the
helper's PYTHONPATH, never on the test process's own path).

One WASAPI loopback device whose rate and channel count come from FAKE_RATE and
FAKE_CH. stream.read(n) returns after n / rate seconds of wall clock, like a
device delivering audio in real time.
"""
import os
import time

paWASAPI = 13
paInt16 = 8

_RATE = int(os.environ.get("FAKE_RATE", "48000"))
_CH = int(os.environ.get("FAKE_CH", "2"))

_DEVICE = {
    "index": 0, "structVersion": 2, "name": "Fake Output [Loopback]", "hostApi": 0,
    "maxInputChannels": _CH, "maxOutputChannels": 0,
    "defaultLowInputLatency": 0.003, "defaultLowOutputLatency": 0.0,
    "defaultHighInputLatency": 0.01, "defaultHighOutputLatency": 0.0,
    "defaultSampleRate": float(_RATE), "isLoopbackDevice": True,
}


class _Stream:
    def __init__(self, channels, rate):
        self._ch = channels
        self._rate = rate
        self._next = time.perf_counter()

    def read(self, frames, exception_on_overflow=False):
        self._next += frames / self._rate
        delay = self._next - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        return b"\x11\x22" * frames * self._ch


class PyAudio:
    def get_host_api_info_by_type(self, _api):
        return {"index": 0, "defaultOutputDevice": 0, "defaultInputDevice": -1}

    def get_device_count(self):
        return 1

    def get_device_info_by_index(self, _index):
        return dict(_DEVICE)

    def open(self, format, channels, rate, input, input_device_index, frames_per_buffer):
        return _Stream(channels, rate)

    def terminate(self):
        pass
