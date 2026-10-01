"""Notification sounds, synthesised here rather than shipped as files.

Every sound is a short motif (two to four notes) played on one of three
instruments, so a whole set shares a voice and the motifs tell the kinds
apart: a rising fourth asks a question, a quick upward pair confirms a start,
a falling pair settles a stop, a repeated minor third warns, and the error
is low and firm. The instrument is the user's choice (Settings > Reminders);
the motif is the app's, picked by the kind of notification.

Synthesis is plain numpy: additive partials with per-partial decay for the
bell and the marimba, a detuned chorus of partials for the synth, and a
small comb-and-allpass reverb for air. Rendering a motif takes a few tens
of milliseconds and the result is cached, so the first notification of a
session pays once.

``play()`` hands the WAV image to winmm's PlaySound asynchronously, which
goes out through the default output device like any other system sound.
"""
from __future__ import annotations

import io
import math
import sys
import threading
import wave
from pathlib import Path
from typing import Callable

import numpy as np

SR = 48000

# ── Sets ──────────────────────────────────────────────────────────────────────
# id -> what the Settings picker shows.
SETS: dict[str, dict] = {
    "glass": {"label": "Glass", "description": "Clear bell tones with a little air"},
    "wood":  {"label": "Wood",  "description": "Warm marimba, short and soft"},
    "pulse": {"label": "Pulse", "description": "Rounded synth plucks with a slow chorus"},
    "chime": {"label": "Chime", "description": "Bright music-box bells, quick and sparkling"},
    "felt":  {"label": "Felt",  "description": "Deep, rounded bloops with a soft echo"},
}
DEFAULT_SET = "felt"

# Each motif: (semitones from the set's base note, onset s, length s, velocity).
MOTIFS: dict[str, list[tuple[float, float, float, float]]] = {
    # A question: a rising fourth, the second note lingering.
    "ask":     [(0, 0.00, 0.90, 0.85), (5, 0.21, 1.10, 0.95)],
    # A start: a quick upward pair, bright and done.
    "started": [(0, 0.00, 0.35, 0.80), (7, 0.12, 0.90, 1.00)],
    # A stop: the same pair the other way round, settling.
    "stopped": [(7, 0.00, 0.60, 0.90), (0, 0.20, 1.00, 0.70)],
    # Success: a short major arpeggio.
    "success": [(0, 0.00, 0.50, 0.80), (4, 0.10, 0.50, 0.85), (7, 0.20, 1.00, 0.95)],
    # Information: one soft note with a faint octave above it.
    "info":    [(0, 0.00, 0.80, 0.75), (12, 0.04, 0.50, 0.25)],
    # A warning: a minor third, twice.
    "warning": [(3, 0.00, 0.45, 0.90), (0, 0.17, 0.50, 0.85), (3, 0.46, 0.45, 0.90), (0, 0.63, 0.80, 0.85)],
    # An error: low, a falling fourth, with weight under each note.
    "error":   [(-12, 0.00, 0.90, 1.00), (-17, 0.26, 1.20, 0.90)],
}
MOTIF_ORDER = ("ask", "started", "stopped", "success", "info", "warning", "error")

_BASE_HZ = {"glass": 880.0, "wood": 659.26, "pulse": 440.0, "chime": 1046.5, "felt": 293.66}
_WET = {"glass": 0.22, "wood": 0.14, "pulse": 0.20, "chime": 0.26, "felt": 0.18}
_TAIL_SEC = 0.45


# ── Instruments ───────────────────────────────────────────────────────────────

def _time(dur: float) -> np.ndarray:
    return np.arange(int(SR * dur)) / SR


def _attack(n: int, ms: float) -> np.ndarray:
    env = np.ones(n)
    k = min(n, max(1, int(SR * ms / 1000)))
    env[:k] = np.linspace(0.0, 1.0, k)
    return env


def _glass(freq: float, dur: float, vel: float) -> np.ndarray:
    """A bell: a stretched partial series, the high partials dying first, a
    touch of pitch bloom at the strike and a slow shimmer."""
    t = _time(dur)
    partials = [(1.0, 1.0, 1.0), (2.0, 0.42, 0.8), (3.0, 0.2, 0.6),
                (4.16, 0.11, 0.45), (5.43, 0.07, 0.35), (6.79, 0.03, 0.3)]
    tau = 0.42 * (660.0 / freq) ** 0.35
    bloom = 1.0 + 0.003 * np.exp(-t / 0.03)
    out = np.zeros_like(t)
    for ratio, amp, decay in partials:
        f = freq * ratio
        if f > 18000:
            continue
        phase = 2 * np.pi * np.cumsum(f * bloom) / SR
        out += amp * np.sin(phase) * np.exp(-t / (tau * decay))
    out *= 1.0 + 0.02 * np.sin(2 * np.pi * 5.5 * t)
    out *= _attack(len(t), 2)
    return np.tanh(out * vel * 1.2) * 0.8


def _wood(freq: float, dur: float, vel: float) -> np.ndarray:
    """A marimba bar: the fundamental with its ~4:1 and ~10:1 modes, fast
    decays, and a soft mallet click."""
    t = _time(dur)
    partials = [(1.0, 1.0, 1.0), (3.93, 0.45, 0.35), (9.1, 0.12, 0.2), (2.0, 0.08, 0.5)]
    tau = 0.22 * (440.0 / freq) ** 0.4
    out = np.zeros_like(t)
    for ratio, amp, decay in partials:
        f = freq * ratio
        if f > 18000:
            continue
        out += amp * np.sin(2 * np.pi * f * t) * np.exp(-t / (tau * decay))
    rng = np.random.default_rng(7)
    click = rng.normal(0.0, 1.0, len(t)) * np.exp(-t / 0.004)
    click = np.convolve(click, np.ones(24) / 24.0, mode="same")
    out += 0.12 * click
    out *= _attack(len(t), 1)
    return np.tanh(out * vel * 1.3) * 0.8


def _pulse(freq: float, dur: float, vel: float) -> np.ndarray:
    """A rounded synth pluck: three detuned voices of a soft sawtooth whose
    upper partials fade first, over a sine an octave down."""
    t = _time(dur)
    tau = 0.34
    out = np.zeros_like(t)
    for det in (-0.004, 0.0, 0.004):
        for k in range(1, 7):
            amp = 1.0 / k ** 1.3
            decay = 1.0 / k ** 0.5
            f = freq * (1.0 + det) * k
            if f > 18000:
                continue
            out += amp * np.sin(2 * np.pi * f * t + 0.37 * k) * np.exp(-t / (tau * decay))
    out /= 3.0
    out += 0.3 * np.sin(2 * np.pi * freq / 2 * t) * np.exp(-t / tau)
    out *= _attack(len(t), 4)
    return np.tanh(out * vel * 1.1) * 0.8


def _chime(freq: float, dur: float, vel: float) -> np.ndarray:
    """A celesta: a bright, slightly stretched partial series with the octave
    strong, struck hard and quick, the way a music box speaks."""
    t = _time(dur)
    partials = [(1.0, 1.0, 1.0), (2.0, 0.55, 0.9), (2.76, 0.3, 0.5), (5.4, 0.18, 0.3), (8.9, 0.06, 0.2)]
    tau = 0.3 * (880.0 / freq) ** 0.3
    out = np.zeros_like(t)
    for k, (ratio, amp, decay) in enumerate(partials):
        f = freq * ratio
        if f > 18000:
            continue
        out += amp * np.sin(2 * np.pi * f * t + 0.5 * k) * np.exp(-t / (tau * decay))
    out *= _attack(len(t), 1)
    return np.tanh(out * vel * 1.15) * 0.8


def _felt(freq: float, dur: float, vel: float) -> np.ndarray:
    """A deep, rounded synth bloop in three layers.

    The body is a soft triangle-like tone whose pitch starts a third high and
    settles within 50 ms, which is what makes it a bloop rather than a beep.
    Under it a sine an octave down gives the depth; over it a faint pair of
    detuned partials two octaves up, with a slow vibrato, gives the edge. One
    darkened echo follows, for space without a wash.
    """
    t = _time(dur)
    n = len(t)
    glide = 1.0 + 0.32 * np.exp(-t / 0.045)
    phase = 2 * np.pi * np.cumsum(freq * glide) / SR
    body = np.zeros(n)
    for k, amp in ((1, 1.0), (3, 0.11), (5, 0.035)):
        body += amp * np.sin(k * phase) * np.exp(-t / (0.3 / k ** 0.6))
    sub = 0.55 * np.sin(2 * np.pi * (freq / 2) * t) * np.exp(-t / 0.38)
    vib = 1.0 + 0.004 * np.sin(2 * np.pi * 5.0 * t)
    sheen = 0.07 * (np.sin(2 * np.pi * freq * 4.0 * vib * t) + np.sin(2 * np.pi * freq * 4.01 * t))
    sheen *= np.exp(-t / 0.18)
    out = (body + sub + sheen) * _attack(n, 5)
    d = int(SR * 0.11)
    echo = np.zeros(n)
    echo[d:] = out[:n - d] * 0.22
    out = out + np.convolve(echo, np.ones(12) / 12.0, mode="same")
    return np.tanh(out * vel * 1.25) * 0.8


def _thud(dur: float) -> np.ndarray:
    """Weight under a low note: a sine that drops in pitch as it dies."""
    t = _time(dur)
    f = 55.0 + 40.0 * np.exp(-t / 0.02)
    phase = 2 * np.pi * np.cumsum(f) / SR
    return 0.6 * np.sin(phase) * np.exp(-t / 0.09)


_VOICES: dict[str, Callable[[float, float, float], np.ndarray]] = {
    "glass": _glass, "wood": _wood, "pulse": _pulse, "chime": _chime, "felt": _felt,
}


# ── Space ─────────────────────────────────────────────────────────────────────

def _comb(x: np.ndarray, delay: int, gain: float) -> np.ndarray:
    """y[i] = x[i] + gain * y[i - delay], a block at a time."""
    y = x.copy()
    for start in range(delay, len(x), delay):
        end = min(start + delay, len(x))
        y[start:end] += gain * y[start - delay:end - delay]
    return y


def _allpass(x: np.ndarray, delay: int, gain: float) -> np.ndarray:
    """y[i] = -gain * x[i] + x[i - delay] + gain * y[i - delay]."""
    y = -gain * x
    for start in range(delay, len(x), delay):
        end = min(start + delay, len(x))
        y[start:end] += x[start - delay:end - delay] + gain * y[start - delay:end - delay]
    return y


def _reverb(x: np.ndarray, stretch: float) -> np.ndarray:
    combs = [(0.0297, 0.62), (0.0371, 0.58), (0.0411, 0.55), (0.0437, 0.52)]
    y = np.zeros_like(x)
    for d, g in combs:
        y += _comb(x, max(1, int(d * stretch * SR)), g)
    y /= len(combs)
    return _allpass(y, max(1, int(0.005 * SR)), 0.5)


def _space(mono: np.ndarray, wet: float) -> np.ndarray:
    """Stereo: the dry sound in the middle, a differently timed reverb on
    each side so the tail has width."""
    left = mono + wet * _reverb(mono, 1.0)
    right = mono + wet * _reverb(mono, 1.07)
    return np.stack([left, right], axis=1)


# ── Rendering ─────────────────────────────────────────────────────────────────

_render_cache: dict[tuple[str, str], np.ndarray] = {}
_wav_cache: dict[tuple[str, str, int], bytes] = {}
_cache_lock = threading.Lock()


def render(set_id: str, motif: str) -> np.ndarray:
    """The motif as float32 stereo at SR, peak-normalised to -6 dBFS."""
    set_id = set_id if set_id in SETS else DEFAULT_SET
    motif = motif if motif in MOTIFS else "info"
    key = (set_id, motif)
    with _cache_lock:
        cached = _render_cache.get(key)
    if cached is not None:
        return cached
    voice, base = _VOICES[set_id], _BASE_HZ[set_id]
    notes = MOTIFS[motif]
    total = max(onset + length for _, onset, length, _ in notes) + _TAIL_SEC
    n = int(SR * total)
    mono = np.zeros(n)
    for semis, onset, length, vel in notes:
        x = voice(base * 2.0 ** (semis / 12.0), length, vel)
        if motif == "error":
            x = x + _thud(length)[:len(x)]
        i = int(onset * SR)
        x = x[:n - i]
        mono[i:i + len(x)] += x
    stereo = _space(mono, _WET[set_id])
    peak = float(np.max(np.abs(stereo))) or 1.0
    stereo *= 0.5 / peak
    fade = min(n, int(SR * 0.02))
    stereo[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]
    out = stereo.astype(np.float32)
    with _cache_lock:
        _render_cache[key] = out
    return out


def amplitude(volume: float) -> float:
    """A 0..100 volume setting as a gain. The curve is steeper than linear
    so the slider feels even: half way sounds about half as loud."""
    try:
        v = max(0.0, min(100.0, float(volume))) / 100.0
    except (TypeError, ValueError):
        v = 0.7
    return v ** 1.7


def wav_bytes(set_id: str, motif: str, gain: float = 1.0) -> bytes:
    """A 16-bit stereo WAV image of the motif at ``gain`` (0..1)."""
    gain = max(0.0, min(1.0, float(gain)))
    key = (set_id, motif, int(round(gain * 100)))
    with _cache_lock:
        cached = _wav_cache.get(key)
    if cached is not None:
        return cached
    pcm = np.clip(render(set_id, motif) * gain, -1.0, 1.0)
    data = (pcm * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data)
    out = buf.getvalue()
    with _cache_lock:
        _wav_cache[key] = out
    return out


def duration(set_id: str, motif: str) -> float:
    return len(render(set_id, motif)) / SR


# ── Playback (Windows) ────────────────────────────────────────────────────────

_SND_ASYNC = 0x0001
_SND_NODEFAULT = 0x0002
_SND_MEMORY = 0x0004
_SND_PURGE = 0x0040
_keep_alive: bytes | None = None   # PlaySound reads the image while it plays


def play(set_id: str, motif: str, gain: float = 1.0) -> bool:
    """Play the motif without blocking. True when winmm accepted it."""
    global _keep_alive
    if sys.platform != "win32" or gain <= 0.0:
        return False
    try:
        import ctypes
        data = wav_bytes(set_id, motif, gain)
        winmm = ctypes.WinDLL("winmm")
        winmm.PlaySoundW.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint]
        winmm.PlaySoundW.restype = ctypes.c_int
        _keep_alive = data
        return bool(winmm.PlaySoundW(data, None, _SND_MEMORY | _SND_ASYNC | _SND_NODEFAULT))
    except Exception:
        return False


def stop() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        winmm = ctypes.WinDLL("winmm")
        winmm.PlaySoundW.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint]
        winmm.PlaySoundW(None, None, _SND_PURGE)
    except Exception:
        pass


# ── Files, for listening outside the app ──────────────────────────────────────

def export(out_dir: Path, gain: float = 1.0) -> list[Path]:
    """Every set's motifs as WAV files, plus a ``tour-<set>.wav`` per set that
    plays them in MOTIF_ORDER with a pause between."""
    out_dir = Path(out_dir)
    written: list[Path] = []
    gap = np.zeros((int(SR * 0.7), 2), dtype=np.float32)
    for set_id in SETS:
        d = out_dir / set_id
        d.mkdir(parents=True, exist_ok=True)
        pieces = []
        for motif in MOTIF_ORDER:
            p = d / f"{motif}.wav"
            p.write_bytes(wav_bytes(set_id, motif, gain))
            written.append(p)
            pieces.append(render(set_id, motif))
            pieces.append(gap)
        tour = np.concatenate(pieces[:-1])
        pcm = (np.clip(tour * gain, -1, 1) * 32767.0).astype("<i2").tobytes()
        p = out_dir / f"tour-{set_id}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm)
        written.append(p)
    return written


if __name__ == "__main__":  # pragma: no cover
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("toast-sounds")
    for path in export(target):
        print(path)
