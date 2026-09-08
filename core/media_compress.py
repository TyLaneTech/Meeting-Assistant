"""Re-encode recordings into smaller formats: the engine behind Free up space.

What it does, per meeting the user picked:

- **Audio.** The recorder keeps the mixed track as 48 kHz mono 16-bit WAV, about
  345 MB an hour. Speech at 32 kbps Opus is about 14 MB an hour and, for a
  meeting, indistinguishable. The WAV becomes ``audio/{sid}.opus`` and every
  reader finds it through core/media.py (the browser plays it directly; the
  transcriber, the fingerprint extractor, the waveform and the clip endpoint
  get a cached PCM decode).
- **Video.** The screen recording is H.264 already, so the gain is smaller:
  AV1 at a screen-content quality roughly halves it, HEVC saves a third, a
  tighter H.264 pass saves a fifth. The MP4 keeps its name and container, only
  the codec inside changes, so nothing that plays or extracts frames has to
  learn anything. NVENC (the graphics card) is offered when ffmpeg has it and a
  probe encode succeeds; software AV1 on a two-hour 4K screen recording is
  slow, and the estimate says so.

How it is safe:

- The plan is priced first and shown to the user; nothing is touched until
  they press Run, and the job reports every file as it goes.
- Each file is encoded to ``tmp/compress/`` and checked (it exists, it is not
  tiny, its duration matches the source within a second or half a percent)
  before it replaces anything. The replacement is an ``os.replace`` and the
  old file goes only after the new one is in place.
- A meeting that is recording or being reanalysed is skipped, not queued.
- Removing files that belong to no meeting (media of deleted meetings, encoder
  fragments) is a separate switch, off unless the user turns it on, and only
  touches files older than a few hours, re-checked at the moment of deletion.
- Trim and split backups are re-encoded the same way, never removed: the
  restore paths read Opus as happily as WAV.
- A file the browser is streaming cannot be replaced on Windows; that file is
  reported "in use" and left as it was, and the user can run again later.
- Every replacement is written to the ``media_encodes`` ledger so the Storage
  card can say what is compressed and the next plan skips it.

One job at a time, on one worker thread, one ffmpeg at a time: the point is
to give space back, not to make the machine unusable while it happens.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from core import log as log
from core import media as media
from core import paths as paths
from core import storage as storage

# ── Presets ─────────────────────────────────────────────────────────────────

AUDIO_PRESETS: dict[str, dict] = {
    "voice_24": {"label": "Smallest", "kbps": 24,
                 "note": "Speech stays clear; music and room sound flatten"},
    "voice_32": {"label": "Recommended", "kbps": 32,
                 "note": "What the export bundle uses; transparent for speech"},
    "voice_48": {"label": "Higher", "kbps": 48,
                 "note": "Headroom for music or several people talking at once"},
    "voice_64": {"label": "Highest", "kbps": 64,
                 "note": "Still ten times smaller than the recording"},
}
DEFAULT_AUDIO_PRESET = "voice_32"

# ``factor`` is the size expected after the pass, as a share of the H.264 the
# recorder wrote, for the estimate. ``crf`` is the software encoder's quality
# knob; the NVENC ``cq`` is derived from it. Screen video has flat colour and
# crisp text, which every one of these codecs handles far better than camera
# footage, so the CRFs sit higher than a video-editing default would.
VIDEO_PRESETS: dict[str, dict] = {
    "av1_small":    {"label": "AV1, smallest", "codec": "av1", "crf": 50, "factor": 0.45,
                     "note": "Plays in current Chrome, Edge and Firefox"},
    "av1_balanced": {"label": "AV1, balanced", "codec": "av1", "crf": 44, "factor": 0.55,
                     "note": "Crisper text than smallest, still about half the size"},
    "hevc":         {"label": "HEVC (H.265)", "codec": "hevc", "crf": 32, "factor": 0.62,
                     "note": "Needs a browser with HEVC support (Edge, or Chrome with hardware decode)"},
    "h264":         {"label": "H.264, most compatible", "codec": "h264", "crf": 34, "factor": 0.8,
                     "note": "A tighter pass of the format the recorder uses"},
}
DEFAULT_VIDEO_PRESET = "av1_balanced"

# Reduce to at most this width when the user asks for it. 4K screen video
# is most of the library; 1440p keeps UI text readable at a third of the pixels.
DOWNSCALE_MAX_WIDTH = 2560
DOWNSCALE_FACTOR = 0.55

_SOFTWARE = {"av1": "libsvtav1", "hevc": "libx265", "h264": "libx264"}
_HARDWARE = {"av1": "av1_nvenc", "hevc": "hevc_nvenc", "h264": "h264_nvenc"}

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
_SIZE_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")

# ── Encoder discovery ───────────────────────────────────────────────────────

_encoders_cache: dict | None = None
_hardware_cache: dict[str, bool] = {}
_probe_lock = threading.Lock()


def available_encoders(ffmpeg: str | None = None) -> set[str]:
    """Names of the encoders this ffmpeg was built with (cached)."""
    global _encoders_cache
    with _probe_lock:
        if _encoders_cache is not None and _encoders_cache.get("bin") == ffmpeg:
            return _encoders_cache["names"]
    names: set[str] = set()
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                timeout=20, creationflags=media.no_window_flag(),
            )
            for line in (result.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] and parts[0][0] in "VAS" and len(parts[0]) == 6:
                    names.add(parts[1])
        except Exception:
            pass
    with _probe_lock:
        _encoders_cache = {"bin": ffmpeg, "names": names}
    return names


def hardware_available(codec: str, ffmpeg: str | None = None) -> bool:
    """True when the NVENC encoder for ``codec`` exists *and* a probe encode
    succeeds. A build can carry av1_nvenc for a machine whose card cannot do
    it; a fifth of a second of black frames settles it. Cached per process."""
    name = _HARDWARE.get(codec)
    if not name or not ffmpeg or name not in available_encoders(ffmpeg):
        return False
    with _probe_lock:
        if name in _hardware_cache:
            return _hardware_cache[name]
    ok = False
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=c=black:s=256x256:r=10:d=0.2",
             "-c:v", name, "-f", "null", "-"],
            capture_output=True, text=True, timeout=30,
            creationflags=media.no_window_flag(),
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    with _probe_lock:
        _hardware_cache[name] = ok
    return ok


def capabilities(ffmpeg: str | None = None) -> dict:
    """What the dialog can offer on this machine."""
    ffmpeg = ffmpeg or media.ffmpeg_bin()
    names = available_encoders(ffmpeg)
    return {
        "ffmpeg": bool(ffmpeg),
        "audio": "libopus" in names,
        "video": {codec: (enc in names) for codec, enc in _SOFTWARE.items()},
        "hardware": {codec: hardware_available(codec, ffmpeg) for codec in _HARDWARE},
    }


# ── Planning ────────────────────────────────────────────────────────────────

def _in_scope(rec: dict, scope: dict, now: float) -> bool:
    mode = (scope or {}).get("mode") or "all"
    started = rec.get("started_at")
    if mode == "all":
        return True
    if mode == "sessions":
        return rec["id"] in set(scope.get("session_ids") or [])
    if mode == "folders":
        return (rec.get("folder_id") or "") in set(scope.get("folder_ids") or [])
    if mode == "older":
        days = float(scope.get("days") or 0)
        if not started:
            return False
        return _to_epoch(started) < now - days * 86400
    if mode == "range":
        if not started:
            return False
        start = scope.get("start") or ""
        end = scope.get("end") or ""
        day = started[:10]
        return (not start or day >= start[:10]) and (not end or day <= end[:10])
    return False


def _to_epoch(iso: str) -> float:
    """Sessions store naive UTC ('YYYY-MM-DD HH:MM:SS' or ISO). Good enough for
    an "older than" comparison at day granularity."""
    from datetime import datetime, timezone
    text = iso.replace("T", " ")[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


def _wav_seconds(path, fallback_bytes: int) -> float:
    """Seconds in a WAV from its header, or from the recorder's 96 000 bytes a
    second when the header cannot be read."""
    header = media.wav_header(path) if path is not None else None
    if header and header["duration_sec"] > 0:
        return header["duration_sec"]
    return fallback_bytes / 96_000


def _audio_estimate(rec: dict, kbps: int, tracks: bool) -> tuple[int, int]:
    """(before, after) for a meeting's audio: the mixed WAV, plus the per-source
    WAV tracks when asked. Durations come from the WAV headers, which are exact
    and cheap; the transcript's length is not used because a recording left
    running past the meeting is exactly the one worth compressing."""
    before = 0
    after = 0
    src = media.audio_path(rec["id"])
    if rec.get("audio_format") == "wav" and src is not None:
        size = rec.get("audio_bytes", 0)
        before += size
        after += int(min(size, max(_wav_seconds(src, size) * kbps * 125, 4096)))
    if tracks and rec.get("audio_tracks_wav_bytes"):
        size = rec["audio_tracks_wav_bytes"]
        before += size
        after += int(min(size, max((size / 96_000) * kbps * 125, 4096)))
    return before, after


def normalize_options(options: dict) -> dict:
    """Fill in defaults and drop unknown presets, so the plan and the job read
    one shape. Audio and backups on, video and orphans off: the first two are
    pure gain, video is slow, and orphans is a deletion."""
    options = options or {}
    audio = options.get("audio") or {}
    video = options.get("video") or {}
    backups = options.get("backups") or {}
    orphans = options.get("orphans") or {}
    audio_preset = audio.get("preset") if audio.get("preset") in AUDIO_PRESETS else DEFAULT_AUDIO_PRESET
    video_preset = video.get("preset") if video.get("preset") in VIDEO_PRESETS else DEFAULT_VIDEO_PRESET
    try:
        min_age = float(orphans.get("min_age_hours", ORPHAN_MIN_AGE_HOURS))
    except (TypeError, ValueError):
        min_age = ORPHAN_MIN_AGE_HOURS
    return {
        "audio": {"enabled": bool(audio.get("enabled", True)), "preset": audio_preset,
                  "tracks": bool(audio.get("tracks", True))},
        "video": {"enabled": bool(video.get("enabled", False)), "preset": video_preset,
                  "downscale": bool(video.get("downscale", False)),
                  "hardware": bool(video.get("hardware", True))},
        "backups": {"enabled": bool(backups.get("enabled", True))},
        "orphans": {"enabled": bool(orphans.get("enabled", False)),
                    "min_age_hours": max(0.0, min_age)},
    }


# Files that belong to no meeting are deleted only once they are this old, so
# an import or a split still writing its files is never mistaken for rubbish.
ORPHAN_MIN_AGE_HOURS = 6.0


def plan(report: dict, scope: dict, options: dict, busy: set[str] | None = None) -> dict:
    """Price a run without touching anything.

    ``report`` is ``disk_usage.scan()``'s output. Returns the items the run
    would process, each with its size now and after, what is skipped and why
    (already compressed, in use), and the options as the job will read them.
    Item kinds: ``audio`` (a meeting's mixed WAV and its per-source WAV tracks),
    ``video`` (its MP4), ``backup`` (one trim or split backup folder's WAV) and
    ``orphan`` (one file that belongs to no meeting, or a leftover fragment).
    """
    busy = busy or set()
    opts = normalize_options(options)
    kbps = AUDIO_PRESETS[opts["audio"]["preset"]]["kbps"]
    vfactor = VIDEO_PRESETS[opts["video"]["preset"]]["factor"]
    if opts["video"]["downscale"]:
        vfactor *= DOWNSCALE_FACTOR
    mode = (scope or {}).get("mode") or "all"

    now = time.time()
    items: list[dict] = []
    skipped = {"already": 0, "busy": 0, "young": 0}
    in_scope: set[str] = set()
    for rec in report.get("sessions", []):
        if not _in_scope(rec, scope, now):
            continue
        in_scope.add(rec["id"])
        base = {"session_id": rec["id"], "title": rec.get("title") or "Meeting",
                "started_at": rec.get("started_at"), "seconds": rec.get("seconds") or 0}
        is_busy = rec["id"] in busy
        if opts["audio"]["enabled"]:
            compressible = rec.get("audio_format") == "wav" or (
                opts["audio"]["tracks"] and rec.get("audio_tracks_wav_bytes"))
            if rec.get("audio_bytes") and rec.get("audio_format") == "opus" and not compressible:
                skipped["already"] += 1
            elif compressible:
                before, after = _audio_estimate(rec, kbps, opts["audio"]["tracks"])
                if is_busy:
                    skipped["busy"] += 1
                    items.append({**base, "kind": "audio", "before": before,
                                  "estimate": before, "status": "busy"})
                else:
                    items.append({**base, "kind": "audio", "before": before, "estimate": after,
                                  "status": "ready", "preset": opts["audio"]["preset"],
                                  "tracks": opts["audio"]["tracks"]})
        if opts["video"]["enabled"] and rec.get("video_bytes"):
            ledger_preset = opts["video"]["preset"] + (":1440p" if opts["video"]["downscale"] else "")
            done = (rec.get("encodes") or {}).get("video")
            if done and done.get("preset") == ledger_preset:
                skipped["already"] += 1
            elif is_busy:
                skipped["busy"] += 1
                items.append({**base, "kind": "video", "before": rec["video_bytes"],
                              "estimate": rec["video_bytes"], "status": "busy"})
            else:
                items.append({**base, "kind": "video", "before": rec["video_bytes"],
                              "estimate": int(rec["video_bytes"] * vfactor), "status": "ready",
                              "preset": opts["video"]["preset"],
                              "downscale": opts["video"]["downscale"]})
        if opts["orphans"]["enabled"] and rec.get("leftovers") and not is_busy:
            # A fragment beside a meeting's media is debris, but a recording
            # that stopped a moment ago may still be muxing it: same age rule.
            for left in rec["leftovers"]:
                if float(left.get("age_hours") or 0) < opts["orphans"]["min_age_hours"]:
                    skipped["young"] += 1
                    continue
                items.append({**base, "kind": "orphan", "path": left["path"],
                              "before": left["bytes"], "estimate": 0, "status": "ready",
                              "leftover": True})

    if opts["backups"]["enabled"]:
        for entry in report.get("backups", []):
            if not entry.get("audio_wav_bytes"):
                continue
            owner = entry.get("session_id")
            # A trim backup follows its meeting's scope; a split backup (keyed
            # by a group, its source meeting gone) is only in "all".
            if owner is not None and owner not in in_scope:
                continue
            if owner is None and mode != "all" and entry.get("kind") == "split":
                continue
            if owner is None and entry.get("kind") == "trim" and mode != "all":
                continue
            if owner in busy:
                skipped["busy"] += 1
                continue
            size = entry["audio_wav_bytes"]
            items.append({"kind": "backup", "dir": entry["dir"], "session_id": owner,
                          "title": entry["dir"], "before": size,
                          "estimate": int(min(size, max((size / 96_000) * kbps * 125, 4096))),
                          "status": "ready", "preset": opts["audio"]["preset"]})

    if opts["orphans"]["enabled"]:
        min_age = opts["orphans"]["min_age_hours"]
        for o in (report.get("orphans") or {}).get("items", []):
            if float(o.get("age_hours") or 0) < min_age:
                skipped["young"] += 1
                continue
            items.append({"kind": "orphan", "path": o["path"], "session_id": o.get("session_id"),
                          "title": o["path"], "before": o["bytes"], "estimate": 0,
                          "status": "ready", "leftover": False})

    ready = [i for i in items if i["status"] == "ready"]
    before = sum(i["before"] for i in ready)
    after = sum(i["estimate"] for i in ready)
    by_kind: dict[str, dict] = {}
    for i in ready:
        k = by_kind.setdefault(i["kind"], {"files": 0, "before": 0, "after": 0})
        k["files"] += 1
        k["before"] += i["before"]
        k["after"] += i["estimate"]
    return {
        "items": items,
        "in_scope": len(in_scope),
        "skipped": skipped,
        "by_kind": by_kind,
        "totals": {"files": len(ready), "before": before, "after": after,
                   "saved": max(0, before - after)},
        "options": opts,
    }


# ── The job ─────────────────────────────────────────────────────────────────

_hooks: dict = {"push": None, "busy": lambda: set()}
_job: "Job | None" = None
_job_lock = threading.Lock()


def configure(push=None, busy=None) -> None:
    """app.py hands in its SSE push and its "who is recording or reanalysing"
    callback. Both optional, so the module imports and tests without Flask."""
    if push is not None:
        _hooks["push"] = push
    if busy is not None:
        _hooks["busy"] = busy


class Job:
    def __init__(self, items: list[dict], options: dict, ffmpeg: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.items = [dict(i, status="pending", after=None, error=None, progress=0.0)
                      for i in items if i.get("status") == "ready"]
        self.options = options
        self.ffmpeg = ffmpeg
        self.state = "running"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.current: int | None = None
        self.cancel = threading.Event()
        self.proc: subprocess.Popen | None = None
        self._last_emit = 0.0
        self.error: str | None = None

    # ── reporting ──
    def snapshot(self, full: bool = True) -> dict:
        done = [i for i in self.items if i["status"] == "done"]
        before = sum(i["before"] for i in done)
        after = sum(i["after"] or 0 for i in done)
        cur = self.items[self.current] if self.current is not None and self.current < len(self.items) else None
        snap = {
            "id": self.id,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": len(self.items),
            "done": len(done),
            "failed": sum(1 for i in self.items if i["status"] == "failed"),
            "before": before,
            "after": after,
            "saved": max(0, before - after),
            "current": ({"session_id": cur["session_id"], "title": cur["title"],
                         "kind": cur["kind"], "progress": cur["progress"],
                         "index": self.current} if cur and cur["status"] == "running" else None),
            "error": self.error,
        }
        if full:
            snap["items"] = [
                {k: i.get(k) for k in ("session_id", "title", "kind", "before",
                                       "estimate", "after", "status", "error", "progress")}
                for i in self.items
            ]
        return snap

    def emit(self, force: bool = False) -> None:
        push = _hooks.get("push")
        now = time.time()
        if not push or (not force and now - self._last_emit < 0.5):
            return
        self._last_emit = now
        try:
            push("storage_job", self.snapshot(full=False))
        except Exception:
            pass

    # ── running ──
    def run(self) -> None:
        try:
            for index, item in enumerate(self.items):
                if self.cancel.is_set():
                    break
                self.current = index
                busy = set()
                try:
                    busy = set(_hooks["busy"]() or ())
                except Exception:
                    busy = set()
                if item["session_id"] in busy:
                    item["status"] = "skipped"
                    item["error"] = "in use (recording or reanalysing)"
                    self.emit(force=True)
                    continue
                item["status"] = "running"
                self.emit(force=True)
                try:
                    if item["kind"] == "audio":
                        self._encode_audio(item)
                    elif item["kind"] == "video":
                        self._encode_video(item)
                    elif item["kind"] == "backup":
                        self._encode_backup(item)
                    elif item["kind"] == "orphan":
                        self._delete_orphan(item)
                    else:
                        raise RuntimeError(f"unknown item kind {item['kind']!r}")
                except _Cancelled:
                    item["status"] = "cancelled"
                    item["error"] = None
                    break
                except PermissionError:
                    item["status"] = "failed"
                    item["error"] = "in use: the file is open in a player; try again later"
                    log.warn("storage", f"{item['kind']} for {item['session_id'][:8]} is in use; left as it was")
                except Exception as e:  # noqa: BLE001
                    item["status"] = "failed"
                    item["error"] = str(e)[:300]
                    log.warn("storage", f"{item['kind']} for {item['session_id'][:8]} failed: {e}")
                self.emit(force=True)
            self.state = "cancelled" if self.cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = str(e)[:300]
            log.error("storage", f"Compression job failed: {e}")
        finally:
            self.current = None
            self.finished_at = time.time()
            for item in self.items:
                if item["status"] == "pending":
                    item["status"] = "cancelled" if self.cancel.is_set() else "skipped"
            try:
                media.prune_pcm_cache()
            except Exception:
                pass
            snap = self.snapshot(full=False)
            log.info("storage", f"Compression {self.state}: {snap['done']}/{snap['total']} files, "
                                f"saved {snap['saved'] // 1_000_000} MB")
            self.emit(force=True)

    # ── one file ──
    def _scratch(self, name: str) -> Path:
        d = paths.tmp_dir() / "compress"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def _run_ffmpeg(self, args: list[str], item: dict, duration: float | None) -> None:
        """Run ffmpeg with ``-progress`` on stdout, updating the item as it goes.
        Raises on a non-zero exit, and _Cancelled when the job was cancelled."""
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-nostats", "-progress", "pipe:1", *args]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", creationflags=media.no_window_flag(),
        )
        stderr_chunks: list[str] = []

        def _drain_err():
            try:
                for line in self.proc.stderr:
                    stderr_chunks.append(line)
            except Exception:
                pass
        threading.Thread(target=_drain_err, daemon=True).start()
        try:
            for line in self.proc.stdout:
                if self.cancel.is_set():
                    self.proc.kill()
                    raise _Cancelled()
                if duration and line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1].strip())
                        item["progress"] = max(0.0, min(0.999, (us / 1_000_000) / duration))
                        self.emit()
                    except ValueError:
                        pass
            code = self.proc.wait()
        finally:
            proc, self.proc = self.proc, None
            if proc and proc.poll() is None:
                proc.kill()
        if code != 0:
            tail = "".join(stderr_chunks).strip()[-400:]
            raise RuntimeError(f"ffmpeg exited {code}: {tail or 'no detail'}")

    def _banner(self, path: Path) -> tuple[float | None, float | None, tuple[int, int] | None]:
        """(duration, fps, (w, h)) from ffmpeg's -i banner."""
        try:
            result = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-i", str(path)],
                capture_output=True, text=True, timeout=30,
                creationflags=media.no_window_flag(),
            )
        except Exception:
            return None, None, None
        err = result.stderr or ""
        duration = None
        m = _DURATION_RE.search(err)
        if m:
            duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        fps = None
        m = _FPS_RE.search(err)
        if m:
            fps = float(m.group(1))
        size = None
        video_line = next((ln for ln in err.splitlines() if "Video:" in ln), "")
        m = _SIZE_RE.search(video_line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
        return duration, fps, size

    @staticmethod
    def _durations_match(src: float | None, out: float | None) -> bool:
        if src is None or out is None:
            return False
        return abs(src - out) <= max(1.0, src * 0.005)

    def _opus_from_wav(self, src: Path, part: Path, item: dict, kbps: int) -> float | None:
        """Encode ``src`` (WAV) to ``part`` (Ogg Opus) and check the result.
        Returns the source duration. Raises when the result does not check out."""
        header = media.wav_header(src)
        duration = header["duration_sec"] if header else None
        part.unlink(missing_ok=True)
        self._run_ffmpeg(
            ["-i", str(src), "-vn", "-ac", "1", "-c:a", "libopus",
             "-b:a", f"{kbps}k", "-vbr", "on", "-application", "voip",
             "-f", "ogg", str(part)],
            item, duration,
        )
        out_duration = media.audio_duration(part, ffmpeg=self.ffmpeg)
        if not part.exists() or part.stat().st_size < 1024 or not self._durations_match(duration, out_duration):
            part.unlink(missing_ok=True)
            raise RuntimeError(f"the encoded audio did not check out (source {duration}s, result {out_duration}s)")
        return duration

    def _encode_audio(self, item: dict) -> None:
        """A meeting's mixed WAV to Opus, and its per-source WAV tracks too when
        asked. Each file is swapped only after it passed its check."""
        sid = item["session_id"]
        preset_key = item.get("preset") or DEFAULT_AUDIO_PRESET
        kbps = AUDIO_PRESETS.get(preset_key, AUDIO_PRESETS[DEFAULT_AUDIO_PRESET])["kbps"]
        before = 0
        after = 0
        did_anything = False
        src = media.audio_path(sid)
        if src is not None and src.suffix.lower() == ".wav":
            part = self._scratch(f"{sid}.opus.part")
            final_tmp = self._scratch(f"{sid}.opus")
            duration = self._opus_from_wav(src, part, item, kbps)
            size_before = src.stat().st_size
            os.replace(part, final_tmp)
            try:
                final = media.replace_audio(sid, final_tmp)     # moves in, retires the WAV
            except PermissionError:
                final_tmp.unlink(missing_ok=True)
                raise
            size_after = final.stat().st_size
            before += size_before
            after += size_after
            did_anything = True
            storage.record_media_encode(sid, "audio", "opus", preset_key, size_before, size_after, duration)
            log.info("storage", f"Audio {sid[:8]}: {size_before // 1_000_000} MB WAV to {size_after // 1_000_000} MB Opus")
        if item.get("tracks", True):
            root = Path(media.tracks_root(sid))
            for side in ("desktop", "mic"):
                wav = root.with_name(f"{root.name}_{side}.wav")
                if not wav.exists():
                    continue
                part = self._scratch(f"{sid}_{side}.opus.part")
                self._opus_from_wav(wav, part, item, kbps)
                target = wav.with_suffix(".opus")
                size_before = wav.stat().st_size
                media._replace_with_retry(part, target)
                media._unlink_with_retry(wav)
                before += size_before
                after += target.stat().st_size
                did_anything = True
                log.info("storage", f"Track {sid[:8]}_{side}: {size_before // 1_000_000} MB WAV to Opus")
        if not did_anything:
            item["status"] = "skipped"
            item["error"] = "already compressed" if src is not None else "no audio"
            return
        item.update(before=before, after=after, status="done", progress=1.0)

    def _encode_backup(self, item: dict) -> None:
        """A trim or split backup's ``audio-original.wav`` to Opus. The restore
        paths (capture_video/media_edit.py) read whichever format is there."""
        folder = paths.data_dir() / item["dir"]
        wav = folder / "audio-original.wav"
        if not wav.exists():
            item["status"] = "skipped"
            item["error"] = "already compressed"
            return
        preset_key = item.get("preset") or DEFAULT_AUDIO_PRESET
        kbps = AUDIO_PRESETS.get(preset_key, AUDIO_PRESETS[DEFAULT_AUDIO_PRESET])["kbps"]
        part = self._scratch(f"backup-{folder.name}.opus.part")
        self._opus_from_wav(wav, part, item, kbps)
        before = wav.stat().st_size
        target = folder / "audio-original.opus"
        media._replace_with_retry(part, target)
        media._unlink_with_retry(wav)
        item.update(before=before, after=target.stat().st_size, status="done", progress=1.0)
        log.info("storage", f"Backup {folder.name[:14]}: {before // 1_000_000} MB WAV to Opus")

    def _delete_orphan(self, item: dict) -> None:
        """Remove one file that belongs to no meeting, or a leftover fragment,
        after checking again that it still qualifies: it is still old enough,
        and no meeting with its id has appeared since the plan was made."""
        path = paths.data_dir() / item["path"]
        if not path.exists():
            item["status"] = "skipped"
            item["error"] = "already gone"
            return
        min_age = float(self.options.get("orphans", {}).get("min_age_hours", ORPHAN_MIN_AGE_HOURS))
        age_hours = max(0.0, time.time() - path.stat().st_mtime) / 3600
        if age_hours < min_age:
            item["status"] = "skipped"
            item["error"] = "too recent to be sure it is unused"
            return
        sid = item.get("session_id") or media.session_id_of(path)
        if not item.get("leftover") and sid and storage.get_session_times(sid):
            item["status"] = "skipped"
            item["error"] = "a meeting with this id exists now"
            return
        size = path.stat().st_size
        media._unlink_with_retry(path)
        item.update(before=size, after=0, status="done", progress=1.0)

    def _encode_video(self, item: dict) -> None:
        sid = item["session_id"]
        src = media.video_path(sid)
        if not src.exists():
            item["status"] = "skipped"
            item["error"] = "no video"
            return
        preset_key = item.get("preset") or DEFAULT_VIDEO_PRESET
        preset = VIDEO_PRESETS.get(preset_key, VIDEO_PRESETS[DEFAULT_VIDEO_PRESET])
        downscale = bool(item.get("downscale"))
        codec = preset["codec"]
        use_hw = bool(self.options.get("video", {}).get("hardware", True)) and hardware_available(codec, self.ffmpeg)
        encoder = _HARDWARE[codec] if use_hw else _SOFTWARE[codec]
        duration, fps, size = self._banner(src)
        gop = str(max(1, round((fps or 10) * 2)))
        video_args = ["-c:v", encoder, "-pix_fmt", "yuv420p", "-g", gop]
        crf = int(preset["crf"])
        if encoder == "libsvtav1":
            video_args += ["-preset", "8", "-crf", str(crf)]
        elif encoder == "libx265":
            video_args += ["-preset", "medium", "-crf", str(crf), "-tag:v", "hvc1"]
        elif encoder == "libx264":
            video_args += ["-preset", "medium", "-crf", str(crf)]
        elif encoder == "av1_nvenc":
            video_args += ["-preset", "p5", "-rc", "vbr", "-cq", str(max(1, crf - 6)), "-b:v", "0"]
        elif encoder == "hevc_nvenc":
            video_args += ["-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0", "-tag:v", "hvc1"]
        elif encoder == "h264_nvenc":
            video_args += ["-preset", "p5", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
        filters = []
        if downscale and size and size[0] > DOWNSCALE_MAX_WIDTH:
            filters.append(f"scale={DOWNSCALE_MAX_WIDTH}:-2")
        if filters:
            video_args += ["-vf", ",".join(filters)]
        part = self._scratch(f"{sid}.mp4.part")
        part.unlink(missing_ok=True)
        self._run_ffmpeg(
            ["-i", str(src), *video_args, "-c:a", "copy", "-movflags", "+faststart",
             "-f", "mp4", str(part)],
            item, duration,
        )
        out_duration, _, _ = self._banner(part)
        if not part.exists() or part.stat().st_size < 4096 or not self._durations_match(duration, out_duration):
            part.unlink(missing_ok=True)
            raise RuntimeError(f"the encoded video did not check out (source {duration}s, result {out_duration}s)")
        before = src.stat().st_size
        after = part.stat().st_size
        if after >= before:
            # Not a saving: keep what the recorder wrote, and say so.
            part.unlink(missing_ok=True)
            item["status"] = "skipped"
            item["error"] = "the re-encode was not smaller; kept the original"
            item["after"] = before
            return
        try:
            media._replace_with_retry(part, src)
        except PermissionError:
            part.unlink(missing_ok=True)
            raise
        item.update(after=after, status="done", progress=1.0)
        ledger_preset = preset_key + (":1440p" if downscale else "")
        storage.record_media_encode(sid, "video", encoder, ledger_preset, before, after, duration)
        log.info("storage", f"Video {sid[:8]}: {before // 1_000_000} MB to {after // 1_000_000} MB ({encoder})")


class _Cancelled(Exception):
    pass


def current_job() -> Job | None:
    return _job


def start(items: list[dict], options: dict) -> Job:
    """Start a job for the plan's ready items. Raises RuntimeError when a job
    is already running or ffmpeg is missing."""
    global _job
    ffmpeg = media.ffmpeg_bin()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not available, so nothing can be re-encoded")
    with _job_lock:
        if _job is not None and _job.state == "running":
            raise RuntimeError("a compression job is already running")
        job = Job(items, options, ffmpeg)
        _job = job
    if not job.items:
        job.state = "done"
        job.finished_at = time.time()
        job.emit(force=True)
        return job
    threading.Thread(target=job.run, daemon=True, name="media-compress").start()
    log.info("storage", f"Compression started: {len(job.items)} files")
    return job


def cancel() -> bool:
    job = _job
    if job is None or job.state != "running":
        return False
    job.cancel.set()
    proc = job.proc
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    return True


def status() -> dict | None:
    job = _job
    return job.snapshot(full=True) if job else None
