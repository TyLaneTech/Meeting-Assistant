"""Simple WAV file writer that tracks sample offsets for timestamp syncing."""
import os
import struct
import wave


class WavWriter:
    """Write mono Int16 PCM to a WAV file, tracking position for sync.

    When ``append=True`` and the file already exists, new audio is appended
    and the RIFF/data headers are patched on close so the WAV remains valid.
    """

    def __init__(self, path: str, sample_rate: int, append: bool = False) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._path = path
        self._sample_rate = sample_rate
        self._wf = None   # wave.open handle (new files only)
        self._raw = None   # raw binary handle (append mode only)

        if append and os.path.isfile(path):
            try:
                with wave.open(path, "rb") as wf:
                    self._total_samples = wf.getnframes()
            except Exception:
                self._total_samples = 0
            # Open for raw binary append — write PCM after existing data
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
    def elapsed_seconds(self) -> float:
        return self._total_samples / self._sample_rate

    def write(self, int16_bytes: bytes) -> int:
        """Write PCM data. Returns the sample offset *before* this write."""
        if self._wf is None and self._raw is None:
            return -1
        offset = self._total_samples
        if self._wf is not None:
            self._wf.writeframes(int16_bytes)
        else:
            self._raw.write(int16_bytes)
        # Each sample is 2 bytes (Int16 mono)
        self._total_samples += len(int16_bytes) // 2
        return offset

    def close(self) -> None:
        if self._wf is not None:
            self._wf.close()
            self._wf = None
        if self._raw is not None:
            # Patch RIFF and data chunk sizes so the WAV is valid
            self._raw.flush()
            data_size = self._total_samples * 2  # 16-bit mono = 2 bytes/sample
            riff_size = data_size + 36  # 36 = header bytes after RIFF size field
            self._raw.seek(4)
            self._raw.write(struct.pack("<I", riff_size))
            self._raw.seek(40)
            self._raw.write(struct.pack("<I", data_size))
            self._raw.close()
            self._raw = None
