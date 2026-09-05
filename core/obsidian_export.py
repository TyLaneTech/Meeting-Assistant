"""Drop each finalized session into an Obsidian vault folder as markdown.

Kept in its own module on purpose. The export used to live inline in app.py,
which meant every sync from upstream collided with it. Nothing here imports
app.py, so upstream can rewrite app.py freely and this file survives untouched;
app.py only needs the small wiring block described in WIRING below.

WIRING (app.py):
    from core import obsidian_export as obsidian
    obsidian.configure(_SOURCE_LABELS)          # after _SOURCE_LABELS is defined
    obsidian.export_session(sid)                # when a session is finalized
    obsidian.queue_export(session_id)           # after any transcript edit

The file is kept current when the transcript is edited afterwards (speaker
renames, segment reassignments, cleanup, reanalysis, retitles).
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from core import log as log
from core import settings as settings
from core import storage as storage

# Display names for segment sources. Injected by app.py via configure() rather
# than imported, so this module never depends on app.py's namespace.
_source_labels: dict = {}

_timers: dict[str, threading.Timer] = {}
_timers_lock = threading.Lock()


def configure(source_labels: dict | None) -> None:
    """Hand the module app.py's _SOURCE_LABELS map."""
    global _source_labels
    _source_labels = dict(source_labels or {})


def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS. Local copy so the module stays standalone."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    return name[:max_len].rstrip() or "Untitled"


def _session_local_dt(iso: str) -> datetime:
    """Session timestamps are naive UTC (storage._now); convert to local."""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).astimezone()


def export_dir() -> Path | None:
    """Resolve the configured vault folder, or None if export is off/broken."""
    if not settings.get("obsidian_export_enabled"):
        return None
    raw = str(settings.get("obsidian_export_dir") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warn("obsidian", f"Export dir unavailable: {e}")
        return None
    return p


def build_markdown(sess: dict) -> str | None:
    """Render a session as a vault-ready markdown doc. None = nothing worth exporting."""
    speaker_labels = sess.get("speaker_labels") or {}
    lines: list[str] = []
    speakers_seen: list[str] = []
    for seg in sess.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Mirror _fmt_segment's precedence: per-segment override, then
        # speaker-key reassignment, then the session speaker name.
        label = seg.get("label_override")
        if not label:
            source = seg.get("source_override") or seg["source"]
            label = speaker_labels.get(source) or _source_labels.get(source, source)
        if label.strip().strip("[]").lower() == "noise":
            continue  # hidden by default in the UI; keep the vault doc clean
        if label not in speakers_seen:
            speakers_seen.append(label)
        start = seg.get("start_time", 0) or 0
        lines.append(f"**[{_fmt_time(start)}] {label}:** {text}")
    if not lines:
        return None

    title = sess.get("title") or "Untitled meeting"
    started = sess.get("started_at") or ""
    ended = sess.get("ended_at") or ""
    date = started[:10]
    started_local = started
    duration = ""
    try:
        if started:
            local = _session_local_dt(started)
            date = local.date().isoformat()
            started_local = local.isoformat(timespec="seconds")
        if started and ended:
            secs = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
            duration = _fmt_time(max(0.0, secs))
    except ValueError:
        pass

    front = [
        "---",
        f'title: "{title.replace(chr(34), chr(39))}"',
        "type: meeting-transcript",
        f"date: {date}",
        f"started: {started_local}",
        f"duration: {duration}",
        "speakers: [" + ", ".join(f'"{s}"' for s in speakers_seen) + "]",
        f"session_id: {sess['id']}",
        "source: Meeting Assistant",
        f"exported: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    body: list[str] = []
    summary = (sess.get("summary") or "").strip()
    if summary:
        body += ["## Summary", "", summary, ""]
    body += ["## Transcript", ""]
    return "\n".join(front) + "\n" + "\n".join(body) + "\n" + "\n\n".join(lines) + "\n"


_GENERIC_RE = re.compile(r"^Speaker\s+\d+$", re.IGNORECASE)


def _resolution_status(sess: dict) -> dict:
    """Classify a finalized session's speakers for the export gate.

    A speaker is "generic" when its displayed label is still a raw diarization
    key (``Speaker N``) that nobody has named. Phantom over-splits (a key with
    almost no speech) are ignored: only generic labels carrying real content -
    at least ``obsidian_gate_min_seconds`` of talk time OR
    ``obsidian_gate_min_words`` words - count as unresolved, so one stray
    segment can't hold an otherwise-named meeting hostage.

    Returns ``{"resolved": bool, "generic": [{"label","seconds","words"}],
    "speaker_count": int}`` (generic list heaviest first).
    """
    speaker_labels = sess.get("speaker_labels") or {}
    per_label: dict[str, dict] = {}
    for seg in sess.get("segments", []):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Mirror build_markdown's label precedence exactly so the gate judges
        # the same names the exported file would show.
        label = seg.get("label_override")
        if not label:
            source = seg.get("source_override") or seg["source"]
            label = speaker_labels.get(source) or _source_labels.get(source, source)
        if label.strip().strip("[]").lower() == "noise":
            continue
        agg = per_label.setdefault(label, {"seconds": 0.0, "words": 0})
        start = seg.get("start_time", 0) or 0
        end = seg.get("end_time", start) or start
        agg["seconds"] += max(0.0, float(end) - float(start))
        agg["words"] += len(text.split())

    min_s = float(settings.get("obsidian_gate_min_seconds") or 0)
    min_w = int(settings.get("obsidian_gate_min_words") or 0)
    generic = [
        {"label": label, "seconds": round(agg["seconds"], 1), "words": agg["words"]}
        for label, agg in per_label.items()
        if _GENERIC_RE.match(label.strip())
        and (agg["seconds"] >= min_s or agg["words"] >= min_w)
    ]
    generic.sort(key=lambda g: g["words"], reverse=True)
    return {"resolved": not generic, "generic": generic, "speaker_count": len(per_label)}


def export_session(session_id: str) -> None:
    try:
        out_dir = export_dir()
        if out_dir is None:
            return
        sess = storage.get_session(session_id)
        if not sess or not sess.get("ended_at"):
            return  # only export finalized sessions
        # Speaker-resolution gate: withhold the export until a human has named
        # the meeting's content-bearing speakers, unless the gate is off or this
        # session was explicitly force-exported. Only NEW writes are withheld; an
        # already-exported file is never removed here.
        if settings.get("obsidian_gate_enabled") and session_id not in set(
            settings.get("obsidian_export_force_ids") or []
        ):
            status = _resolution_status(sess)
            if not status["resolved"]:
                held = ", ".join(g["label"] for g in status["generic"])
                log.info("obsidian", f"Held {session_id[:8]} from export: "
                                     f"unresolved speaker(s) {held}. Name them or "
                                     f"force-export to release.")
                return
        md = build_markdown(sess)
        if md is None:
            return  # empty / all-noise sessions don't pollute the vault
        short = session_id[:8]
        started = sess.get("started_at") or ""
        try:
            date = _session_local_dt(started).date().isoformat() if started else ""
        except ValueError:
            date = started[:10]
        fname = f"{date} {_safe_filename(sess.get('title') or 'Untitled meeting')} [{short}].md"
        target = out_dir / fname
        # A retitle changes the filename; replace the previous export for
        # this session rather than leaving a stale duplicate behind.
        for old in out_dir.glob(f"* [{short}].md"):
            if old != target:
                try:
                    old.unlink()
                except OSError:
                    pass
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(target)
        log.info("obsidian", f"Exported session {short} -> {target.name}")
    except Exception as e:
        log.warn("obsidian", f"Export failed for {session_id}: {e}")


def queue_export(session_id: str, delay: float = 4.0) -> None:
    """Debounced re-export: bulk edits (multi-segment reassigns, cleanup
    apply, merge cascades) collapse into a single file write."""
    if not settings.get("obsidian_export_enabled"):
        return
    with _timers_lock:
        prev = _timers.pop(session_id, None)
        if prev:
            prev.cancel()
        timer = threading.Timer(delay, export_session, args=(session_id,))
        timer.daemon = True
        _timers[session_id] = timer
        timer.start()


def export_all() -> dict:
    """One-shot backfill: export every finalized session with content."""
    out_dir = export_dir()
    if out_dir is None:
        return {"error": "Obsidian export is not enabled / configured"}
    exported = skipped = 0
    for s in storage.list_sessions():
        before = len(list(out_dir.glob(f"* [{s['id'][:8]}].md")))
        export_session(s["id"])
        after = len(list(out_dir.glob(f"* [{s['id'][:8]}].md")))
        if after:
            exported += 1
        elif not before:
            skipped += 1
    return {"ok": True, "exported": exported, "skipped": skipped, "dir": str(out_dir)}


def held_sessions() -> list[dict]:
    """Finalized sessions the speaker gate is currently withholding from export.

    Read-only; drives the in-app resolution panel. Each entry carries its
    unresolved-speaker breakdown. Empty when export or the gate is disabled.
    """
    if not settings.get("obsidian_export_enabled") or not settings.get("obsidian_gate_enabled"):
        return []
    forced = set(settings.get("obsidian_export_force_ids") or [])
    out: list[dict] = []
    for s in storage.list_sessions():
        sid = s["id"]
        if sid in forced:
            continue
        sess = storage.get_session(sid)
        if not sess or not sess.get("ended_at"):
            continue
        status = _resolution_status(sess)
        if not status["resolved"]:
            out.append({
                "session_id": sid,
                "title": sess.get("title") or "Untitled meeting",
                "started_at": sess.get("started_at"),
                "generic": status["generic"],
                "speaker_count": status["speaker_count"],
            })
    return out
