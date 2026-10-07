"""The desktop (loopback) stream gets a ring buffer sized to the device period.

Opened with a 512-frame buffer and read with blocking 512-frame reads, the
WASAPI loopback silently lost about 3 percent of the far end on the Creative
BT-W6 and on the Realtek speakers (PortAudio's blocking read can wait 47 ms,
its ring buffer overflows and the excess is dropped without an error). The
mixer padded every missing chunk with 10.7 ms of digital silence, heard as
popping and crackling whenever the other side talked (2026-09-21). The mic
stream has carried a period-sized buffer for the same reason; the loopback
now uses the same rule with a higher floor.
"""
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("WASAPI loopback capture is Windows-only", allow_module_level=True)
windows = pytest.importorskip("capture_audio.windows")

BT_W6 = {"index": 16, "name": "Speakers (Creative BT-W6) [Loopback]", "maxInputChannels": 2,
         "defaultSampleRate": 48000.0, "defaultLowInputLatency": 0.003, "defaultHighInputLatency": 0.01}


def test_loopback_ring_buffer_is_at_least_2048_frames():
    assert windows.AudioCapture._compute_loopback_buffer_size(BT_W6) >= 2048


def test_loopback_ring_buffer_is_a_power_of_two_and_bounded():
    for latency in (0.003, 0.01, 0.05, 0.5):
        size = windows.AudioCapture._compute_loopback_buffer_size(dict(BT_W6, defaultHighInputLatency=latency))
        assert size & (size - 1) == 0
        assert 2048 <= size <= 8192


def test_loopback_ring_buffer_tolerates_a_device_without_latency_info():
    assert windows.AudioCapture._compute_loopback_buffer_size({"defaultSampleRate": 44100.0}) >= 2048
