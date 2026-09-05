"""The desktop (loopback) device the user chose is the device captured.

Windows keeps two output roles and PortAudio only reports the first, so "the
default output" is routinely not the device the user listens on. Following it
swapped a saved headset for idle speakers and recorded a whole call as silence
(2026-09-05). Following is opt-in (loopback_follow_output); off, the saved
device is authoritative and the default is only the fallback once it has gone.
"""
import sys

import pytest

if sys.platform != "win32":
    pytest.skip("WASAPI loopback capture is Windows-only", allow_module_level=True)
windows = pytest.importorskip("capture_audio.windows")


class _FakePA:
    """Just enough of PyAudioWPatch for device resolution."""
    def __init__(self, devices, default_output_index):
        self._devices = {d["index"]: d for d in devices}
        self._default = default_output_index

    def get_host_api_info_by_type(self, _api):
        return {"defaultOutputDevice": self._default}

    def get_device_info_by_index(self, index):
        return self._devices[index]

    def get_loopback_device_info_generator(self):
        return iter(d for d in self._devices.values() if d["name"].endswith("[Loopback]"))


HEADSET = {"index": 39, "name": "Headphones (Arctis Nova Pro Wireless) [Loopback]", "maxInputChannels": 2}
SPEAKERS = {"index": 41, "name": "Bose (High Definition Audio Device) [Loopback]", "maxInputChannels": 2}
BOSE_OUT = {"index": 7, "name": "Bose (High Definition Audio Device)", "maxInputChannels": 0}


def _capture(pa):
    cap = windows.AudioCapture.__new__(windows.AudioCapture)
    cap._pa = pa
    return cap


def test_saved_device_wins_when_following_is_off(monkeypatch):
    monkeypatch.setattr(windows, "_follow_output_enabled", lambda: False)
    pa = _FakePA([HEADSET, SPEAKERS, BOSE_OUT], default_output_index=7)
    assert _capture(pa)._resolve_loopback(39, HEADSET["name"])["name"] == HEADSET["name"]


def test_saved_device_is_re_resolved_by_name_when_its_index_drifted(monkeypatch):
    monkeypatch.setattr(windows, "_follow_output_enabled", lambda: False)
    pa = _FakePA([dict(SPEAKERS, index=39), dict(HEADSET, index=52), BOSE_OUT], default_output_index=7)
    assert _capture(pa)._resolve_loopback(39, HEADSET["name"])["index"] == 52


def test_default_output_is_only_the_fallback(monkeypatch):
    monkeypatch.setattr(windows, "_follow_output_enabled", lambda: False)
    pa = _FakePA([SPEAKERS, BOSE_OUT], default_output_index=7)   # headset unplugged
    assert _capture(pa)._resolve_loopback(39, HEADSET["name"])["name"] == SPEAKERS["name"]


def test_missing_headset_does_not_match_speakers_on_the_loopback_suffix():
    pa = _FakePA([SPEAKERS, BOSE_OUT], default_output_index=7)
    assert _capture(pa)._match_loopback_by_name(HEADSET["name"]) is None


def test_following_binds_the_default_output_when_switched_on(monkeypatch):
    monkeypatch.setattr(windows, "_follow_output_enabled", lambda: True)
    pa = _FakePA([HEADSET, SPEAKERS, BOSE_OUT], default_output_index=7)
    assert _capture(pa)._resolve_loopback(39, HEADSET["name"])["name"] == SPEAKERS["name"]
