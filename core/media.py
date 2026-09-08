"""Where a recording's media lives, whatever format it is in.

The recorder writes one mixed track per session, ``audio/{sid}.wav`` (48 kHz
mono 16-bit), plus ``video/{sid}.mp4`` when the screen was captured. For years
that WAV was the only form the audio ever took, so about thirty places built the
path themselves and read it with the stdlib ``wave`` module. The Free up space
tool (core/media_compress.py) changed that: a session's audio may now be
``audio/{sid}.opus`` instead, twenty-odd times smaller, and every reader has to
cope. This module is the one place that knows the rule.

Two questions readers ask:

- :func:`audio_path`: the file to serve or copy. Opus if it exists, otherwise
  the WAV, otherwise None. Use with :func:`audio_mime` for the browser.
- :func:`pcm_wav_path`: a WAV to open with ``wave``. The original when the
  session still has one; otherwise the Opus is decoded once into
  ``tmp/pcm/{sid}.wav`` and reused until the source changes. Reanalysis, the
  speaker fingerprint extractor, the waveform profile, trimming and the agent
  API's clip endpoint all go through this rather than learning to decode.

Nothing here writes into ``audio/`` or ``video/``; the recorder, the importer,
trim/split and the compressor do that, and they are the only ones who should.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

from core import log as log
from core import paths as paths

# Preferred first. A session with both (a trim that wrote a fresh WAV over an
# Opus session leaves this state for a moment) resolves to the WAV, because a
# newer WAV always means an edit that has not been re-encoded yet.
AUDIO_SUFFIXES = (".wav", ".opus")
AUDIO_MIME = {".opus": "audio/ogg", ".wav": "audio/wav"}

# The recorder's format, and what the decode reproduces, so a reader that
# learned its sample rate from the header is none the wiser.
PCM_RATE = 48_000

_PCM_SUBDIR = "pcm"
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

_decode_locks: dict[str, threading.Lock] = {}
_decode_locks_guard = threading.Lock()


# ── Locating ────────────────────────────────────────────────────────────────

def audio_candidates(session_id: str) -> list[Path]:
    """Every path the mixed audio could be at, preferred first."""
    audio_dir = paths.audio_dir()
    return [audio_dir / f"{session_id}{suffix}" for suffix in AUDIO_SUFFIXES]


def audio_path(session_id: str) -> Path | None:
    """The session's mixed audio file, or None when it has none."""
    for candidate in audio_candidates(session_id):
        if candidate.exists():
            return candidate
    return None


def has_audio(session_id: str) -> bool:
    return audio_path(session_id) is not None


def audio_mime(path: Path | str) -> str:
    return AUDIO_MIME.get(Path(path).suffix.lower(), "application/octet-stream")


def audio_format(path: Path | str | None) -> str | None:
    """'wav', 'opus', or None. What the storage view and the compressor key on."""
    if path is None:
        return None
    suffix = Path(path).suffix.lower()
    return suffix[1:] if suffix in AUDIO_MIME else None


def video_path(session_id: str) -> Path:
    """Where the screen recording is (or would be). Not guaranteed to exist."""
    return paths.video_dir() / f"{session_id}.mp4"


def has_video(session_id: str) -> bool:
    return video_path(session_id).exists()


def tracks_root(session_id: str) -> str:
    """``audio/{sid}`` without a suffix: the stem the per-source tracks hang off
    (``{sid}_desktop.opus`` / ``{sid}_mic.opus``). The batch transcriber used to
    derive this from the WAV path, which stopped working the moment the WAV
    could be a decode living in tmp/."""
    return str(paths.audio_dir() / session_id)


def per_source_tracks(session_id: str) -> tuple[Path, Path] | None:
    """(desktop, mic) per-source tracks when both exist, else None."""
    root = Path(tracks_root(session_id))
    for suffix in (".opus", ".wav"):
        desktop = root.with_name(root.name + "_desktop" + suffix)
        mic = root.with_name(root.name + "_mic" + suffix)
        if desktop.exists() and mic.exists():
            return desktop, mic
    return None


def session_id_of(path: Path | str) -> str | None:
    """The session a media file belongs to, from its name, or None."""
    m = _UUID_RE.match(Path(path).name)
    return m.group(0) if m else None


# ── ffmpeg ──────────────────────────────────────────────────────────────────

def ffmpeg_bin() -> str | None:
    from capture_video.ffmpeg_util import find_ffmpeg
    return find_ffmpeg()


def no_window_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _banner_duration(path: Path, ffmpeg: str) -> float | None:
    """Duration from ffmpeg's ``-i`` banner. The bundled toolchain has no
    ffprobe, and this is what the agent API already does for video."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, timeout=20,
            creationflags=no_window_flag(),
        )
    except Exception:
        return None
    m = _DURATION_RE.search(result.stderr or "")
    if not m:
        return None
    h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return round(h * 3600 + mn * 60 + s, 2)


def wav_header(path: Path) -> dict | None:
    """Rate, channels, sample width and duration from a WAV header, or None."""
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate() or 1
            frames = wf.getnframes()
            return {
                "duration_sec": round(frames / rate, 2),
                "sample_rate": rate,
                "channels": wf.getnchannels(),
                "sample_width": wf.getsampwidth(),
            }
    except (wave.Error, OSError, EOFError):
        return None


def audio_duration(path: Path | str, *, ffmpeg: str | None = None) -> float | None:
    """Seconds of audio in a WAV (header) or anything else ffmpeg reads."""
    path = Path(path)
    if not path.exists():
        return None
    if path.suffix.lower() == ".wav":
        header = wav_header(path)
        if header:
            return header["duration_sec"]
    ffmpeg = ffmpeg or ffmpeg_bin()
    return _banner_duration(path, ffmpeg) if ffmpeg else None


def audio_info(path: Path | str, *, ffmpeg: str | None = None) -> dict | None:
    """What the agent API reports about a session's audio: duration, format,
    size, and for a WAV its rate and channels. None when the file is missing."""
    path = Path(path)
    if not path.exists():
        return None
    info: dict = {"format": audio_format(path)}
    if path.suffix.lower() == ".wav":
        header = wav_header(path)
        if header:
            info.update({k: header[k] for k in ("duration_sec", "sample_rate", "channels")})
    else:
        duration = audio_duration(path, ffmpeg=ffmpeg)
        if duration is not None:
            info["duration_sec"] = duration
    try:
        info["size_bytes"] = path.stat().st_size
    except OSError:
        pass
    return info


# ── PCM for readers that open a WAV ─────────────────────────────────────────

def pcm_cache_dir() -> Path:
    p = paths.tmp_dir() / _PCM_SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pcm_target(session_id: str) -> Path:
    return pcm_cache_dir() / f"{session_id}.wav"


def _stamp(path: Path) -> str:
    st = path.stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


def _lock_for(session_id: str) -> threading.Lock:
    with _decode_locks_guard:
        lock = _decode_locks.get(session_id)
        if lock is None:
            lock = _decode_locks[session_id] = threading.Lock()
        return lock


def pcm_wav_path(session_id: str, *, ffmpeg: str | None = None,
                 timeout: float = 1800.0) -> Path | None:
    """A WAV holding the session's mixed audio, for ``wave``-based readers.

    The recorder's own WAV when the session still has one. Otherwise the Opus
    is decoded to ``tmp/pcm/{sid}.wav`` at the recorder's format (48 kHz mono
    16-bit) and that file is returned, and returned again on later calls until
    the Opus changes (a sidecar carries the source's mtime and size). A decode
    of a two-hour meeting takes a few seconds; two threads asking for the same
    session share one decode. None when the session has no audio, ffmpeg is
    missing, or the decode failed (logged).
    """
    source = audio_path(session_id)
    if source is None:
        return None
    if source.suffix.lower() == ".wav":
        return source

    target = _pcm_target(session_id)
    sidecar = target.with_suffix(".src")
    try:
        want = _stamp(source)
    except OSError:
        return None
    with _lock_for(session_id):
        try:
            if target.exists() and sidecar.read_text(encoding="utf-8") == want:
                os.utime(target, None)     # recently used: prune leaves it alone
                return target
        except OSError:
            pass
        ffmpeg = ffmpeg or ffmpeg_bin()
        if not ffmpeg:
            log.warn("media", f"ffmpeg not found: cannot decode {source.name} for a reader that needs PCM")
            return None
        part = target.with_suffix(".part")
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-i", str(source), "-vn", "-ac", "1", "-ar", str(PCM_RATE),
                 "-c:a", "pcm_s16le", "-f", "wav", str(part)],
                capture_output=True, text=True, timeout=timeout,
                creationflags=no_window_flag(),
            )
            if result.returncode != 0 or not part.exists() or part.stat().st_size <= 44:
                log.warn("media", f"Decoding {source.name} failed: {(result.stderr or '').strip()[-300:]}")
                part.unlink(missing_ok=True)
                return None
            os.replace(part, target)
            sidecar.write_text(want, encoding="utf-8")
            log.info("media", f"Decoded {source.name} to PCM for a reader ({target.stat().st_size // 1_000_000} MB)")
            return target
        except Exception as e:
            log.warn("media", f"Decoding {source.name} failed: {e}")
            part.unlink(missing_ok=True)
            return None


def release_pcm(session_id: str) -> None:
    """Drop the decoded copy, if any. Called when the session or its audio goes."""
    target = _pcm_target(session_id)
    for p in (target, target.with_suffix(".src"), target.with_suffix(".part")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def prune_pcm_cache(max_age_sec: float = 24 * 3600) -> int:
    """Delete decodes nobody has touched for a day. Returns how many went.
    Cheap and idempotent; the app runs it at startup and after a compression
    job, so the cache never quietly grows into the space the job just freed."""
    removed = 0
    cutoff = time.time() - max_age_sec
    try:
        entries = list(pcm_cache_dir().iterdir())
    except OSError:
        return 0
    for p in entries:
        try:
            if p.suffix in (".wav", ".part") and p.stat().st_mtime < cutoff:
                p.unlink()
                p.with_suffix(".src").unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    return removed


# ── Removal ─────────────────────────────────────────────────────────────────

def delete_session_media(session_id: str) -> list[Path]:
    """Remove every media file a session owns: the mixed audio in any format,
    the per-source tracks, the video and any encoder fragment left beside it,
    and the PCM decode. Returns what was removed. Never raises."""
    removed: list[Path] = []
    candidates: list[Path] = list(audio_candidates(session_id))
    audio_dir = paths.audio_dir()
    for suffix in (".opus", ".wav"):
        candidates.append(audio_dir / f"{session_id}_desktop{suffix}")
        candidates.append(audio_dir / f"{session_id}_mic{suffix}")
    video = video_path(session_id)
    candidates.append(video)
    try:
        candidates.extend(p for p in video.parent.glob(f"{session_id}.mp4.*"))
    except OSError:
        pass
    for p in candidates:
        try:
            if p.exists():
                p.unlink()
                removed.append(p)
        except OSError:
            pass
    release_pcm(session_id)
    return removed


def replace_audio(session_id: str, new_file: Path) -> Path:
    """Make ``new_file`` the session's mixed audio, retiring whatever was there.

    Moves it to ``audio/{sid}<suffix>`` and removes the other format, so a
    session never keeps two mixed tracks. Used by the compressor (WAV to Opus)
    and by trim, which writes a WAV and must retire a stale Opus. The old file
    is removed after the new one is in place; on Windows a file the browser is
    streaming cannot be replaced or removed, so callers must be ready for
    ``PermissionError`` and treat it as "in use, try later"."""
    suffix = new_file.suffix.lower()
    if suffix not in AUDIO_MIME:
        raise ValueError(f"not an audio format this app serves: {new_file.name}")
    target = paths.audio_dir() / f"{session_id}{suffix}"
    if new_file.resolve() != target.resolve():
        _replace_with_retry(new_file, target)
    for other in audio_candidates(session_id):
        if other != target and other.exists():
            _unlink_with_retry(other)
    release_pcm(session_id)
    return target


def _replace_with_retry(src: Path, dst: Path, attempts: int = 6) -> None:
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.4 * (i + 1))


def _unlink_with_retry(path: Path, attempts: int = 6) -> None:
    for i in range(attempts):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.4 * (i + 1))


def copy_audio_as(session_id: str, dst_dir: Path, stem: str = "audio-original") -> Path | None:
    """Copy the session's mixed audio into ``dst_dir`` as ``<stem><suffix>``,
    keeping its format. What the trim and split backups use, so an Opus session
    is not inflated back to WAV just to be kept safe."""
    src = audio_path(session_id)
    if src is None:
        return None
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{stem}{src.suffix.lower()}"
    if not dst.exists():
        shutil.copy2(src, dst)
    return dst


def find_backup_audio(dst_dir: Path, stem: str = "audio-original") -> Path | None:
    """The backup written by :func:`copy_audio_as`, in whichever format."""
    for suffix in AUDIO_SUFFIXES:
        p = dst_dir / f"{stem}{suffix}"
        if p.exists():
            return p
    return None
