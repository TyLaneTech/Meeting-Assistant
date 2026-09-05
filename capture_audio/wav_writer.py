"""Simple WAV file writer that tracks sample offsets for timestamp syncing."""
import os
import struct
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly

from core import log as log


class WavWriter:
    """Write mono Int16 PCM to a WAV file, tracking position for sync.

    When ``append=True`` and the file already exists, new audio is appended
    and the RIFF/data headers are patched on close so the WAV remains valid.

    If the existing file's sample rate differs from the new capture rate,
    incoming audio is automatically resampled to match the file's rate so
    the WAV stays consistent (prevents chipmunk / slow-motion playback).
    """

    def __init__(self, path: str, sample_rate: int, append: bool = False) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._sample_rate = sample_rate   # rate of the WAV file on disk
        self._input_rate = sample_rate    # rate of incoming audio data
        self._wf = None    # wave.open handle (new files only)
        self._raw = None   # raw binary handle (append mode only)
        self._resample_up = 1
        self._resample_down = 1
        self._closed = False

        # Tracks position in the *caller's* sample rate so that sample
        # offsets returned by write() stay consistent with the transcriber's
        # clock regardless of any WAV-side resampling.
        self._input_samples_written = 0

        if append and os.path.isfile(path):
            existing_rate = sample_rate
            existing_channels = 1
            try:
                with wave.open(path, "rb") as wf:
                    self._total_samples = wf.getnframes()
                    existing_rate = wf.getframerate()
                    existing_channels = wf.getnchannels()
            except Exception:
                self._total_samples = 0

            if existing_channels != 1:
                log.warn("audio", f"WAV file has {existing_channels} channels, "
                                  f"expected mono - file may be corrupted on append")

            # If the existing file has a different sample rate, resample
            # incoming audio to match so playback speed stays correct.
            if existing_rate != sample_rate:
                self._sample_rate = existing_rate
                g = gcd(existing_rate, sample_rate)
                self._resample_up = existing_rate // g
                self._resample_down = sample_rate // g
                log.warn("audio",
                         f"WAV sample rate mismatch: file={existing_rate} Hz, "
                         f"capture={sample_rate} Hz - resampling to {existing_rate} Hz")

            # Convert existing WAV samples to equivalent input-rate samples
            # so offset tracking is continuous across recordings.
            if self._resample_up != self._resample_down:
                self._input_samples_written = int(
                    self._total_samples * self._resample_down / self._resample_up
                )
            else:
                self._input_samples_written = self._total_samples

            # Where the data chunk's size field lives: 40 in the canonical
            # 44-byte header this class writes, but a WAV decoded by ffmpeg (the
            # resume path rebuilds the per-source temp WAV that way) carries a
            # LIST chunk before "data". Patching offset 40 there corrupted the
            # file, ffmpeg refused it, and the whole track stayed as a broken
            # WAV (2026-09-05).
            self._data_size_pos = self._find_data_size_pos(path)
            # Open for raw binary append - write PCM after existing data
            self._raw = open(path, "r+b")
            self._raw.seek(0, 2)  # seek to end
        else:
            self._wf = wave.open(path, "wb")
            self._wf.setnchannels(1)
            self._wf.setsampwidth(2)  # 16-bit
            self._wf.setframerate(sample_rate)
            self._total_samples = 0

    @property
    def total_samples(self) -> int:
        return self._total_samples

    @property
    def sample_rate(self) -> int:
        """The sample rate of the WAV file on disk."""
        return self._sample_rate

    @property
    def elapsed_seconds(self) -> float:
        return self._total_samples / self._sample_rate

    def write(self, int16_bytes: bytes) -> int:
        """Write PCM data.  Returns the sample offset *before* this write,
        expressed in the input (capture) sample rate so the transcriber can
        compute wall-clock timestamps via ``offset / capture_rate``.
        """
        if self._closed or (self._wf is None and self._raw is None):
            return -1

        # Count input samples before any resampling
        input_sample_count = len(int16_bytes) // 2
        offset = self._input_samples_written

        # Resample if the capture rate differs from the WAV file rate
        if self._resample_up != 1 or self._resample_down != 1:
            samples = np.frombuffer(int16_bytes, dtype=np.int16).astype(np.float32)
            resampled = resample_poly(
                samples, self._resample_up, self._resample_down,
            ).astype(np.float32)
            int16_bytes = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

        if self._wf is not None:
            self._wf.writeframes(int16_bytes)
        else:
            self._raw.write(int16_bytes)

        # Track both WAV-file samples and input-rate samples
        self._total_samples += len(int16_bytes) // 2
        self._input_samples_written += input_sample_count
        return offset

    @staticmethod
    def _find_data_size_pos(path: str) -> int:
        """Byte offset of the data chunk's size field, found by walking the
        RIFF chunks. 40 (the canonical header) when the file cannot be read."""
        try:
            with open(path, "rb") as f:
                if f.read(4) != b"RIFF":
                    return 40
                f.seek(12)
                while True:
                    head = f.read(8)
                    if len(head) < 8:
                        return 40
                    chunk_id, size = head[:4], struct.unpack("<I", head[4:])[0]
                    if chunk_id == b"data":
                        return f.tell() - 4
                    f.seek(size + (size & 1), 1)   # chunks are word-aligned
        except Exception:
            return 40

    def close(self) -> None:
        """Finalize and close the WAV file.  Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True

        if self._wf is not None:
            self._wf.close()
            self._wf = None
        if self._raw is not None:
            # Patch the RIFF and data chunk sizes from what is actually on disk
            # so the WAV is valid whatever header layout it started with.
            self._raw.flush()
            self._raw.seek(0, 2)
            end = self._raw.tell()
            data_pos = getattr(self, "_data_size_pos", 40)
            self._raw.seek(4)
            self._raw.write(struct.pack("<I", max(0, end - 8)))
            self._raw.seek(data_pos)
            self._raw.write(struct.pack("<I", max(0, end - (data_pos + 4))))
            self._raw.close()
            self._raw = None
