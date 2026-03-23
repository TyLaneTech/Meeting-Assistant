"""Simple WAV file writer that tracks sample offsets for timestamp syncing."""
import os
import wave


class WavWriter:
    """Write mono Int16 PCM to a WAV file, tracking position for sync."""

    def __init__(self, path: str, sample_rate: int) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._wf = wave.open(path, "wb")
        self._wf.setnchannels(1)
        self._wf.setsampwidth(2)  # 16-bit
        self._wf.setframerate(sample_rate)
        self._sample_rate = sample_rate
        self._total_samples = 0

    @property
    def total_samples(self) -> int:
        return self._total_samples

    @property
    def elapsed_seconds(self) -> float:
        return self._total_samples / self._sample_rate

    def write(self, int16_bytes: bytes) -> int:
        """Write PCM data. Returns the sample offset *before* this write."""
        if self._wf is None:
            return -1
        offset = self._total_samples
        self._wf.writeframes(int16_bytes)
        # Each sample is 2 bytes (Int16 mono)
        self._total_samples += len(int16_bytes) // 2
        return offset

    def close(self) -> None:
        if self._wf is not None:
            self._wf.close()
            self._wf = None
