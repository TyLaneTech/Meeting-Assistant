"""What the data folder holds, per kind and per meeting.

The Storage card on Home is drawn from one report of the whole data folder:
how many bytes each kind of file takes (the mixed audio, the per-source tracks,
the screen video, the screenshot frames, trim and split backups, the database,
and so on), the same broken down per meeting so the card can rank meetings and
group them by month or folder, every backup folder with what it holds, and what
belongs to no meeting any more (media of deleted meetings, encoder fragments).

Pure filesystem work over the metadata the caller already has, so it is cheap
to test: :func:`scan` never opens the database. The dashboard route
(core/dashboard_api.py) reads the sessions and the encode ledger and hands
them in. The owner's library (1 700 files, 180 GB) scans in about a tenth of
a second; nothing here is cached because nothing here needs to be.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from core import media as media
from core import paths as paths

# The kinds a file can be, in the order the card lists them. "frames" are the
# JPEG screenshots the chat tools capture; "profiles" are the cached waveform
# profiles; "tmp" includes the PCM decodes core/media.py keeps for readers.
KINDS = ("audio", "video", "frames", "backups", "attachments", "database",
         "profiles", "tmp", "other")

AUDIO_SUFFIXES = ("wav", "opus")


def _walk(root: Path):
    """Every file under ``root`` with its size and mtime. Missing roots yield
    nothing; a file that disappears mid-walk is skipped, not fatal."""
    if not root.exists():
        return
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    yield Path(entry.path), st.st_size, st.st_mtime
            except OSError:
                continue


def _new_session_record(session_id: str, meta: dict | None) -> dict:
    meta = meta or {}
    return {
        "id": session_id,
        "title": meta.get("title") or "",
        "started_at": meta.get("started_at"),
        "folder_id": meta.get("folder_id"),
        "seconds": float(meta.get("seconds") or 0.0),
        "bytes": 0,
        "audio_bytes": 0,            # the mixed track
        "audio_format": None,        # 'wav' | 'opus' | None
        "audio_tracks_bytes": 0,     # the per-source mic/desktop tracks, any format
        "audio_tracks_wav_bytes": 0, # ... of which still WAV (compressible)
        "video_bytes": 0,
        "video_files": 0,
        "frames_bytes": 0,
        "frames_files": 0,
        "backups_bytes": 0,
        "other_bytes": 0,            # profiles, notes attachments
        "leftover_bytes": 0,         # encoder fragments and partial files
        "leftovers": [],             # [{path, bytes, age_hours}], paths relative to the data folder
        "encodes": {},               # kind -> ledger row, when compressed
    }


def classify_audio(name: str) -> tuple[str, str | None]:
    """('mixed' | 'track' | 'leftover', format) for a file name in audio/.
    The format is 'wav' or 'opus' for mixed tracks and per-source tracks, and
    None for anything else (a .part, a stray decode, an unknown suffix)."""
    lower = name.lower()
    stem, dot, suffix = lower.rpartition(".")
    if not dot or suffix not in AUDIO_SUFFIXES:
        return "leftover", None
    if stem.endswith("_mic") or stem.endswith("_desktop"):
        return "track", suffix
    if media.session_id_of(lower) == stem:
        return "mixed", suffix
    return "leftover", None


def scan(sessions: dict[str, dict], encodes: dict | None = None,
         now: float | None = None) -> dict:
    """Measure the data folder.

    ``sessions`` maps a session id to ``{title, started_at, folder_id,
    seconds}``; ``encodes`` maps ``(session_id, kind)`` to the ledger row the
    compressor wrote (codec, preset, before_bytes, after_bytes, encoded_at).
    A ledger row is reported only while the file on disk still matches it: a
    trim that wrote a fresh WAV over an Opus session leaves the row behind,
    and that session is uncompressed again as far as the card is concerned.
    """
    encodes = encodes or {}
    now = now or time.time()
    data_dir = paths.data_dir()
    by_kind = {kind: {"bytes": 0, "files": 0} for kind in KINDS}
    per_session: dict[str, dict] = {}
    orphans: list[dict] = []
    audio_formats = {fmt: {"bytes": 0, "files": 0} for fmt in AUDIO_SUFFIXES}
    tracks = {fmt: {"bytes": 0, "files": 0} for fmt in AUDIO_SUFFIXES}
    backups: dict[str, dict] = {}
    leftover_total = {"bytes": 0, "files": 0}

    def rel(path: Path) -> str:
        try:
            return path.relative_to(data_dir).as_posix()
        except ValueError:
            return path.name

    def record(kind: str, size: int) -> None:
        by_kind[kind]["bytes"] += size
        by_kind[kind]["files"] += 1

    def session_for(path: Path) -> tuple[str | None, dict | None]:
        sid = media.session_id_of(path)
        if sid is None or sid not in sessions:
            return sid, None
        rec = per_session.get(sid)
        if rec is None:
            rec = per_session[sid] = _new_session_record(sid, sessions.get(sid))
        return sid, rec

    def record_for(owner: str) -> dict | None:
        rec = per_session.get(owner)
        if rec is None and owner in sessions:
            rec = per_session[owner] = _new_session_record(owner, sessions[owner])
        return rec

    def orphan(path: Path, size: int, mtime: float, kind: str, session_id: str | None) -> None:
        orphans.append({"path": rel(path), "bytes": size, "kind": kind,
                        "age_hours": round(max(0.0, now - mtime) / 3600, 1),
                        "session_id": session_id})

    def leftover(rec: dict, path: Path, size: int, mtime: float) -> None:
        rec["leftover_bytes"] += size
        rec["leftovers"].append({"path": rel(path), "bytes": size,
                                 "age_hours": round(max(0.0, now - mtime) / 3600, 1)})
        leftover_total["bytes"] += size
        leftover_total["files"] += 1

    # ── audio/ : the mixed track, the per-source tracks, encoder leftovers ──
    for path, size, mtime in _walk(data_dir / "audio"):
        record("audio", size)
        role, fmt = classify_audio(path.name)
        sid, rec = session_for(path)
        if rec is None:
            orphan(path, size, mtime, "audio", sid)
            continue
        rec["bytes"] += size
        if role == "mixed":
            rec["audio_bytes"] += size
            rec["audio_format"] = fmt
            audio_formats[fmt]["bytes"] += size
            audio_formats[fmt]["files"] += 1
        elif role == "track":
            rec["audio_tracks_bytes"] += size
            tracks[fmt]["bytes"] += size
            tracks[fmt]["files"] += 1
            if fmt == "wav":
                rec["audio_tracks_wav_bytes"] += size
        else:
            leftover(rec, path, size, mtime)

    # ── video/ : the screen recording and any fragment left beside it ──────
    for path, size, mtime in _walk(data_dir / "video"):
        record("video", size)
        sid, rec = session_for(path)
        if rec is None:
            orphan(path, size, mtime, "video", sid)
            continue
        rec["bytes"] += size
        if path.name.lower() == f"{sid}.mp4":
            rec["video_bytes"] += size
            rec["video_files"] += 1
        else:
            leftover(rec, path, size, mtime)

    # ── screenshots/<sid>/ : frames the chat tools captured ────────────────
    shots = data_dir / "screenshots"
    for path, size, mtime in _walk(shots):
        record("frames", size)
        try:
            owner = path.relative_to(shots).parts[0]
        except (ValueError, IndexError):
            owner = ""
        rec = record_for(owner)
        if rec is None:
            orphan(path, size, mtime, "frames", owner or None)
            continue
        rec["bytes"] += size
        rec["frames_bytes"] += size
        rec["frames_files"] += 1

    # ── backups/<sid>/ (trim) and backups/split-<group>/ (split) ───────────
    backups_dir = data_dir / "backups"
    for path, size, mtime in _walk(backups_dir):
        record("backups", size)
        try:
            top = path.relative_to(backups_dir).parts[0]
        except (ValueError, IndexError):
            top = path.name
        entry = backups.get(top)
        if entry is None:
            is_split = top.startswith("split-")
            owner = None if is_split else (top if top in sessions else None)
            entry = backups[top] = {
                "dir": f"backups/{top}",
                "kind": "split" if is_split else "trim",
                "session_id": owner,
                "orphaned": (not is_split) and owner is None,
                "bytes": 0, "files": 0,
                "audio_wav_bytes": 0,      # compressible: audio-original.wav
                "age_hours": 0.0,
            }
        entry["bytes"] += size
        entry["files"] += 1
        entry["age_hours"] = max(entry["age_hours"], round(max(0.0, now - mtime) / 3600, 1))
        if path.name.lower() == "audio-original.wav":
            entry["audio_wav_bytes"] += size
        rec = record_for(top)
        if rec is not None:
            rec["bytes"] += size
            rec["backups_bytes"] += size

    # ── attachments/ and notes/<sid>/ ──────────────────────────────────────
    for path, size, mtime in _walk(data_dir / "attachments"):
        record("attachments", size)
    notes = data_dir / "notes"
    for path, size, mtime in _walk(notes):
        record("attachments", size)
        try:
            owner = path.relative_to(notes).parts[0]
        except (ValueError, IndexError):
            owner = ""
        rec = record_for(owner)
        if rec is not None:
            rec["bytes"] += size
            rec["other_bytes"] += size

    # ── audio_profiles/ : cached waveform profiles, named by session ───────
    for path, size, mtime in _walk(data_dir / "audio_profiles"):
        record("profiles", size)
        sid, rec = session_for(path)
        if rec is not None:
            rec["bytes"] += size
            rec["other_bytes"] += size

    # ── tmp/ : decodes and encoder scratch ─────────────────────────────────
    for path, size, mtime in _walk(data_dir / "tmp"):
        record("tmp", size)

    # ── the top level: database, settings, anything unaccounted for ────────
    known_dirs = {"audio", "video", "screenshots", "backups", "attachments",
                  "notes", "audio_profiles", "tmp"}
    try:
        top_entries = list(os.scandir(data_dir))
    except OSError:
        top_entries = []
    for entry in top_entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in known_dirs:
                    continue
                for path, size, mtime in _walk(Path(entry.path)):
                    record("other", size)
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                if entry.name.lower().startswith("meetings.db"):
                    record("database", size)
                else:
                    record("other", size)
        except OSError:
            continue

    # ── the encode ledger, checked against what is on disk ─────────────────
    for (sid, kind), row in encodes.items():
        rec = per_session.get(sid)
        if rec is None:
            continue
        if kind == "audio" and rec["audio_format"] != "opus":
            continue                        # re-inflated by a trim: not compressed now
        if kind == "video" and not rec["video_files"]:
            continue
        rec["encodes"][kind] = dict(row)

    # Sessions with no files at all still appear (zero bytes), so a per-meeting
    # view can say "no media" instead of silently dropping them.
    for sid, meta in sessions.items():
        per_session.setdefault(sid, _new_session_record(sid, meta))

    total_bytes = sum(k["bytes"] for k in by_kind.values())
    total_files = sum(k["files"] for k in by_kind.values())
    try:
        usage = shutil.disk_usage(str(data_dir))
        disk = {"total": int(usage.total), "free": int(usage.free), "used": int(usage.used)}
    except OSError:
        disk = None

    orphans.sort(key=lambda item: -item["bytes"])
    ordered = sorted(per_session.values(),
                     key=lambda rec: (rec["started_at"] or ""), reverse=True)
    return {
        "root": str(data_dir),
        "disk": disk,
        "totals": {"bytes": total_bytes, "files": total_files},
        "by_kind": by_kind,
        "audio_formats": audio_formats,      # the mixed tracks by format
        "tracks": tracks,                    # the per-source tracks by format
        "backups": sorted(backups.values(), key=lambda b: -b["bytes"]),
        "leftovers": leftover_total,
        "sessions": ordered,
        "orphans": {
            "bytes": sum(o["bytes"] for o in orphans),
            "files": len(orphans),
            "items": orphans,
        },
    }
