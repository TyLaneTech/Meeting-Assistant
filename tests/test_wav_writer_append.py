"""WavWriter append mode must keep a WAV valid whatever header it started with.

The resume path rebuilds the per-source temp WAV by decoding the Opus part with
ffmpeg, whose WAV carries a LIST chunk before "data". Patching the size at the
canonical offset 40 on close corrupted that file, and the final Opus encode then
failed on both tracks (2026-09-05).
"""
import struct
import wave

import pytest

pytest.importorskip("scipy")   # wav_writer imports resample_poly
from capture_audio.wav_writer import WavWriter   # noqa: E402


def _wav_with_list_chunk(path, frames: bytes, rate: int = 48000) -> None:
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    info = b"ISFT" + struct.pack("<I", 12) + b"Lavf61.1.100"
    list_chunk = b"LIST" + struct.pack("<I", 4 + len(info)) + b"INFO" + info
    body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + list_chunk + b"data" + struct.pack("<I", len(frames)) + frames)
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_append_after_an_ffmpeg_style_header_keeps_the_wav_valid(tmp_path):
    path = tmp_path / "part_mic.wav"
    _wav_with_list_chunk(path, b"\x01\x00" * 1000)
    with wave.open(str(path), "rb") as wf:
        assert wf.getnframes() == 1000

    w = WavWriter(str(path), 48000, append=True)
    w.write(b"\x02\x00" * 500)
    w.close()

    with wave.open(str(path), "rb") as wf:
        assert wf.getnframes() == 1500
        assert wf.getframerate() == 48000
        wf.setpos(1000)
        assert wf.readframes(1) == b"\x02\x00"
    raw = path.read_bytes()
    i = raw.index(b"LIST")
    # The LIST chunk's own size field was left alone.
    assert struct.unpack("<I", raw[i + 4:i + 8])[0] == 24
    assert struct.unpack("<I", raw[4:8])[0] == len(raw) - 8


def test_append_to_a_canonical_wav_still_works(tmp_path):
    path = tmp_path / "plain.wav"
    w = WavWriter(str(path), 16000)
    w.write(b"\x01\x00" * 300)
    w.close()
    w = WavWriter(str(path), 16000, append=True)
    w.write(b"\x03\x00" * 200)
    w.close()
    with wave.open(str(path), "rb") as wf:
        assert wf.getnframes() == 500
