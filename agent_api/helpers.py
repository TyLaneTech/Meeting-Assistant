"""Pure formatting / conversion helpers for the Agent API.

Everything here is side-effect free (except media probing, which shells out
to ffmpeg) and independent of Flask, so it can be unit-tested and reused by
both the REST layer and any future transport.
"""
from __future__ import annotations

import re
import subprocess
import wave
from datetime import datetime
from pathlib import Path

from core import paths, settings

# ── Timestamps ────────────────────────────────────────────────────────────────

_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def parse_timestamp(value) -> float | None:
    """Parse a timestamp into seconds.

    Accepts numbers (90, 90.5, "90.5") and clock strings ("1:30",
    "01:02:03.5"). Returns None when unparseable or negative.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        num = float(text)
        return num if num >= 0 else None
    except ValueError:
        pass
    m = _TS_RE.match(text)
    if not m:
        return None
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2))
    seconds = float(m.group(3))
    return hours * 3600 + minutes * 60 + seconds


def fmt_mmss(seconds: float) -> str:
    m, s = divmod(int(max(0, seconds)), 60)
    return f"{m}:{s:02d}"


def _fmt_clock(seconds: float, ms_sep: str) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:  # float rounding at the second boundary
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d}{ms_sep}{ms:03d}"


def fmt_srt_time(seconds: float) -> str:
    return _fmt_clock(seconds, ",")


def fmt_vtt_time(seconds: float) -> str:
    return _fmt_clock(seconds, ".")


def parse_bool(value, default: bool = False) -> bool:
    """Parse a query-string boolean. Accepts 1/0, true/false, yes/no."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# ── Speaker resolution ────────────────────────────────────────────────────────

def resolve_speaker(seg: dict, speaker_labels: dict | None,
                    source_labels: dict) -> str:
    """Resolve a segment's display speaker.

    Mirrors app._fmt_segment precedence exactly: per-segment label override,
    then the session label for the (possibly reassigned) source key, then the
    built-in source label ('Desktop'/'Mic'/'Me'), then the raw key.
    """
    label_override = seg.get("label_override")
    if label_override:
        return label_override
    source = seg.get("source_override") or seg.get("source", "loopback")
    if speaker_labels and source in speaker_labels:
        return speaker_labels[source]
    return source_labels.get(source, source)


# ── Transcript rendering ─────────────────────────────────────────────────────

def transcript_rows(segments: list[dict], speaker_labels: dict | None,
                    source_labels: dict) -> list[dict]:
    """Segments as agent-friendly JSON rows with resolved speakers."""
    return [
        {
            "id": s.get("id"),
            "start": round(s.get("start_time") or 0.0, 2),
            "end": round(s.get("end_time") or 0.0, 2),
            "start_hms": fmt_mmss(s.get("start_time") or 0.0),
            "speaker": resolve_speaker(s, speaker_labels, source_labels),
            "source": s.get("source_override") or s.get("source", "loopback"),
            "text": s.get("text", ""),
        }
        for s in segments
    ]


def transcript_text(segments: list[dict], speaker_labels: dict | None,
                    source_labels: dict, *, timestamps: bool = True,
                    speakers: bool = True) -> str:
    lines = []
    for s in segments:
        parts = []
        if timestamps:
            parts.append(f"[{fmt_mmss(s.get('start_time') or 0.0)}]")
        if speakers:
            parts.append(f"[{resolve_speaker(s, speaker_labels, source_labels)}]")
        parts.append(s.get("text", ""))
        lines.append(" ".join(parts))
    return "\n".join(lines)


def transcript_markdown(segments: list[dict], speaker_labels: dict | None,
                        source_labels: dict, *, chapters: list[dict] | None = None,
                        title: str = "", timestamps: bool = True) -> str:
    """Markdown transcript with chapter headings interleaved at their offsets."""
    out: list[str] = []
    if title:
        out.append(f"# {title}\n")
    pending = sorted(chapters or [], key=lambda c: c.get("start_time") or 0.0)
    ci = 0
    for s in segments:
        start = s.get("start_time") or 0.0
        while ci < len(pending) and (pending[ci].get("start_time") or 0.0) <= start:
            out.append(f"\n## [{fmt_mmss(pending[ci].get('start_time') or 0.0)}] "
                       f"{pending[ci].get('title', '')}\n")
            ci += 1
        speaker = resolve_speaker(s, speaker_labels, source_labels)
        stamp = f"`[{fmt_mmss(start)}]` " if timestamps else ""
        out.append(f"{stamp}**{speaker}:** {s.get('text', '')}")
    while ci < len(pending):  # trailing chapters past the last segment
        out.append(f"\n## [{fmt_mmss(pending[ci].get('start_time') or 0.0)}] "
                   f"{pending[ci].get('title', '')}\n")
        ci += 1
    return "\n".join(out)


def transcript_srt(segments: list[dict], speaker_labels: dict | None,
                   source_labels: dict, *, speakers: bool = True) -> str:
    blocks = []
    for i, s in enumerate(segments, 1):
        start = s.get("start_time") or 0.0
        end = max(start + 0.5, s.get("end_time") or 0.0)
        text = s.get("text", "")
        if speakers:
            text = f"{resolve_speaker(s, speaker_labels, source_labels)}: {text}"
        blocks.append(f"{i}\n{fmt_srt_time(start)} --> {fmt_srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


def transcript_vtt(segments: list[dict], speaker_labels: dict | None,
                   source_labels: dict, *, speakers: bool = True) -> str:
    blocks = ["WEBVTT\n"]
    for s in segments:
        start = s.get("start_time") or 0.0
        end = max(start + 0.5, s.get("end_time") or 0.0)
        text = s.get("text", "")
        if speakers:
            text = f"<v {resolve_speaker(s, speaker_labels, source_labels)}>{text}"
        blocks.append(f"{fmt_vtt_time(start)} --> {fmt_vtt_time(end)}\n{text}\n")
    return "\n".join(blocks)


TRANSCRIPT_FORMATS = ("json", "text", "markdown", "srt", "vtt")


# ── Quill Delta (rich-text notes) conversion ─────────────────────────────────

def _delta_ops(delta) -> list[dict]:
    """Normalize a stored Delta (dict with ops, bare list, or None) to ops."""
    if delta is None:
        return []
    if isinstance(delta, dict):
        ops = delta.get("ops", [])
    else:
        ops = delta
    return [op for op in ops if isinstance(op, dict)]


def delta_to_text(delta) -> str:
    """Plain-text rendering of a Quill Delta (embeds become placeholders)."""
    out = []
    for op in _delta_ops(delta):
        ins = op.get("insert")
        if isinstance(ins, str):
            out.append(ins)
        elif isinstance(ins, dict):
            if "image" in ins:
                out.append("[image]")
            else:
                out.append("[embed]")
    return "".join(out)


def _md_inline(text: str, attrs: dict) -> str:
    """Apply inline markdown formatting for a Quill text run."""
    if not attrs:
        return text
    if attrs.get("code"):
        return f"`{text}`"
    if attrs.get("bold"):
        text = f"**{text}**"
    if attrs.get("italic"):
        text = f"*{text}*"
    if attrs.get("strike"):
        text = f"~~{text}~~"
    link = attrs.get("link")
    if link:
        text = f"[{text}]({link})"
    return text


def delta_to_markdown(delta) -> str:
    """Convert a Quill Delta to GitHub-flavoured markdown.

    Quill attaches line-level attributes (header, list, blockquote,
    code-block) to the newline op that terminates the line, so we accumulate
    runs into a current line and format it when the newline arrives.
    """
    lines: list[str] = []
    current: list[str] = []

    def flush(line_attrs: dict | None) -> None:
        text = "".join(current)
        current.clear()
        a = line_attrs or {}
        header = a.get("header")
        if header:
            lines.append(f"{'#' * min(6, int(header))} {text}")
        elif a.get("list") == "bullet":
            lines.append(f"- {text}")
        elif a.get("list") == "ordered":
            lines.append(f"1. {text}")
        elif a.get("blockquote"):
            lines.append(f"> {text}")
        elif a.get("code-block"):
            lines.append(f"    {text}")
        else:
            lines.append(text)

    for op in _delta_ops(delta):
        ins = op.get("insert")
        attrs = op.get("attributes") or {}
        if isinstance(ins, dict):
            if "image" in ins:
                current.append(f"![image]({ins['image']})")
            else:
                current.append("[embed]")
            continue
        if not isinstance(ins, str):
            continue
        parts = ins.split("\n")
        for i, part in enumerate(parts):
            if part:
                current.append(_md_inline(part, {k: v for k, v in attrs.items()
                                                 if k in ("bold", "italic", "strike",
                                                          "code", "link")}))
            if i < len(parts) - 1:  # a newline followed this part
                flush(attrs)
    if current:
        flush(None)
    return "\n".join(lines).strip()


def build_note_append_ops(text: str, heading: str | None) -> list[dict]:
    """Ops to append a plain-text note block (optional H3 heading) to a Delta.

    Additive only: callers concatenate these onto the existing ops so agent
    writes can never mangle user-authored formatting.
    """
    ops: list[dict] = [{"insert": "\n"}]
    if heading:
        ops.append({"insert": heading})
        ops.append({"insert": "\n", "attributes": {"header": 3}})
    ops.append({"insert": (text or "").rstrip() + "\n"})
    return ops


# ── Meeting bundle / export ──────────────────────────────────────────────────

def meeting_flags(session_id: str) -> dict:
    """Cheap media / notes availability flags for one session."""
    return {
        "has_audio": (paths.audio_dir() / f"{session_id}.wav").exists(),
        "has_video": (paths.video_dir() / f"{session_id}.mp4").exists(),
    }


def export_markdown(sess: dict, source_labels: dict, *,
                    folder_path: str | None = None) -> str:
    """Render one meeting as a single self-contained markdown document."""
    labels = sess.get("speaker_labels") or {}
    lines = [f"# {sess.get('title') or 'Untitled meeting'}", ""]
    lines.append(f"- **Session ID:** `{sess['id']}`")
    lines.append(f"- **Started:** {sess.get('started_at') or 'unknown'}")
    if sess.get("ended_at"):
        lines.append(f"- **Ended:** {sess['ended_at']}")
    segs = sess.get("segments", [])
    if segs:
        dur = max((s.get("end_time") or 0.0) for s in segs)
        lines.append(f"- **Duration:** {fmt_mmss(dur)} ({len(segs)} segments)")
    if folder_path:
        lines.append(f"- **Folder:** {folder_path}")
    speakers = sorted({resolve_speaker(s, labels, source_labels) for s in segs})
    if speakers:
        lines.append(f"- **Speakers:** {', '.join(speakers)}")
    lines.append("")

    if sess.get("summary"):
        lines += ["## Summary", "", sess["summary"], ""]

    chapters = sess.get("chapters") or []
    if chapters:
        lines += ["## Chapters", ""]
        lines += [f"- [{fmt_mmss(c.get('start_time') or 0.0)}] {c.get('title', '')}"
                  for c in chapters]
        lines.append("")

    notes_md = delta_to_markdown((sess.get("notes") or {}).get("delta"))
    if notes_md:
        lines += ["## Notes", "", notes_md, ""]

    if segs:
        lines += ["## Transcript", ""]
        lines.append(transcript_markdown(segs, labels, source_labels,
                                         chapters=chapters))
        lines.append("")

    chat = sess.get("chat_messages") or []
    if chat:
        lines += ["## AI chat history", ""]
        for m in chat:
            who = "User" if m.get("role") == "user" else "Assistant"
            lines.append(f"**{who}:** {m.get('content', '')}")
            lines.append("")
    return "\n".join(lines)


# ── Media probing ─────────────────────────────────────────────────────────────

def wav_info(path: Path) -> dict | None:
    """Duration / rate / size of a WAV file via the stdlib wave module."""
    if not path.exists():
        return None
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            info = {
                "duration_sec": round(frames / rate, 2),
                "sample_rate": rate,
                "channels": wf.getnchannels(),
            }
    except (wave.Error, OSError):
        info = {}
    try:
        info["size_bytes"] = path.stat().st_size
    except OSError:
        pass
    return info or None


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def video_info(path: Path, ffmpeg_bin: str | None,
               no_window_flag: int = 0) -> dict | None:
    """Duration / size of an MP4. ffmpeg prints stream info to stderr even
    without an output file, so we parse its banner rather than requiring
    ffprobe (which the bundled toolchain doesn't ship)."""
    if not path.exists():
        return None
    info: dict = {}
    try:
        info["size_bytes"] = path.stat().st_size
    except OSError:
        pass
    if ffmpeg_bin:
        try:
            result = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-i", str(path)],
                capture_output=True, text=True, timeout=10,
                creationflags=no_window_flag,
            )
            m = _DURATION_RE.search(result.stderr or "")
            if m:
                h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                info["duration_sec"] = round(h * 3600 + mn * 60 + s, 2)
        except Exception:
            pass
    return info or None


def extract_wav_clip(path: Path, start: float, end: float) -> bytes | None:
    """Slice [start, end] seconds out of a WAV into a standalone WAV blob."""
    import io
    try:
        with wave.open(str(path), "rb") as src:
            rate = src.getframerate()
            total = src.getnframes()
            f0 = max(0, min(total, int(start * rate)))
            f1 = max(f0, min(total, int(end * rate)))
            src.setpos(f0)
            frames = src.readframes(f1 - f0)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as dst:
                dst.setnchannels(src.getnchannels())
                dst.setsampwidth(src.getsampwidth())
                dst.setframerate(rate)
                dst.writeframes(frames)
            return buf.getvalue()
    except (wave.Error, OSError, ValueError):
        return None


def dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def list_dir_files(path: Path) -> list[dict]:
    """Non-recursive file listing with sizes, newest first."""
    out = []
    try:
        for p in sorted(path.iterdir()):
            if p.is_file():
                try:
                    st = p.stat()
                    out.append({
                        "name": p.name,
                        "size_bytes": st.st_size,
                        "modified": datetime.utcfromtimestamp(st.st_mtime)
                                            .isoformat(timespec="seconds"),
                    })
                except OSError:
                    continue
    except OSError:
        pass
    out.sort(key=lambda f: f["modified"], reverse=True)
    return out


# ── Settings schema ───────────────────────────────────────────────────────────

# Human/agent-readable descriptions for the settings keys most useful to an
# agent. Keys without an entry still appear in the schema with type + default.
SETTINGS_DESCRIPTIONS: dict[str, str] = {
    "loopback_device": "PyAudio index of the system-audio (loopback) capture device. Empty = system default.",
    "loopback_device_name": "Friendly name paired with loopback_device; used to re-resolve the device if indices shift.",
    "mic_device": "Microphone device index. -1 = none, -2 = browser microphone (default).",
    "whisper_preset": "Whisper model preset id (see /api/models). Empty = auto-detect at startup. Restart or use the UI to hot-swap.",
    "diarizer_device": "Speaker-diarization device: 'cuda', 'cpu', or '' for auto.",
    "diarization_enabled": "Whether speaker diarization runs during recordings.",
    "auto_summary": "Regenerate the AI summary automatically as a meeting progresses.",
    "mic_is_me_enabled": "Attribute all microphone audio to the 'Me' speaker instead of diarizing it.",
    "me_speaker_global_id": "Voice-library profile id used as the 'Me' speaker.",
    "ai_provider": "Primary AI provider: 'anthropic' or 'openai'. Changing it reloads the client immediately.",
    "ai_model": "Primary AI model id for summaries/chat/titles.",
    "summary_provider": "Per-tool provider override for summaries (null = use primary).",
    "summary_model": "Per-tool model override for summaries (null = use primary).",
    "chat_provider": "Per-tool provider override for session chat (null = use primary).",
    "chat_model": "Per-tool model override for session chat (null = use primary).",
    "chapters_provider": "Per-tool provider override for AI chapters (null = use primary).",
    "chapters_model": "Per-tool model override for AI chapters (null = use primary).",
    "chapters_auto": "Auto-generate chapter markers during recordings.",
    "chapters_granularity": "Chapter density: 'coarse', 'balanced', or 'fine'.",
    "screen_display": "Display index recorded by screen capture.",
    "screen_preset": "Screen recording quality preset (minimal/performance/balanced/quality/maximum/custom).",
    "quiet_prompt_enabled": "Remind the user when a recording has captured near-silence for a while.",
    "meeting_detect_enabled": "Watch for Zoom/Teams meetings and offer to record (opt-in).",
    "meeting_detect_autostart": "Auto-start recording on meeting detection instead of prompting.",
    "warp_toggle_enabled": "Briefly disconnect Cloudflare WARP around network-heavy operations (rarely needed).",
    "sidebar_open": "UI: whether the session sidebar is expanded.",
    "playback_speed": "UI: default playback speed for recordings.",
    "agent_api_enabled": "Master switch for this Agent API. When false every /api/agent/v1 endpoint returns 503.",
    "agent_api_token": "Optional bearer token required on Agent API requests when non-empty.",
    "agent_api_allow_recording_control": "Allow agents to start/stop recordings (off by default).",
}

# Settings an agent may not change while a recording is running (they steer
# the live capture/model pipeline; mid-recording writes would desync state).
RECORDING_LOCKED_KEYS = {
    "loopback_device", "loopback_device_name", "mic_device",
    "whisper_preset", "diarizer_device", "diarization_enabled",
    "screen_display", "screen_preset", "mic_is_me_enabled",
    "me_speaker_global_id",
}

# Keys persisted but only applied on the next app start (no hot-reload path
# is wired for agents; the UI uses dedicated endpoints for these).
RESTART_REQUIRED_KEYS = {
    "whisper_preset", "diarizer_device", "loopback_device",
    "loopback_device_name", "mic_device",
}

# Internal bookkeeping keys the agent should not write directly.
SETTINGS_WRITE_DENYLIST = {"video_offsets"}


def settings_schema() -> list[dict]:
    """Describe every known settings key: name, type, default, description."""
    out = []
    for key, default in settings.DEFAULTS.items():
        if default is None:
            typ = "string|null"
        elif isinstance(default, bool):
            typ = "boolean"
        elif isinstance(default, int):
            typ = "integer"
        elif isinstance(default, float):
            typ = "number"
        elif isinstance(default, dict):
            typ = "object"
        else:
            typ = "string"
        out.append({
            "key": key,
            "type": typ,
            "default": default,
            "description": SETTINGS_DESCRIPTIONS.get(key, ""),
            "writable": key not in SETTINGS_WRITE_DENYLIST,
            "locked_while_recording": key in RECORDING_LOCKED_KEYS,
            "restart_required": key in RESTART_REQUIRED_KEYS,
        })
    return out


def coerce_setting(key: str, value):
    """Validate/coerce a proposed settings value against the default's type.

    Returns (ok, coerced_or_reason). None is accepted for keys whose default
    is None (the per-tool override pattern).
    """
    if key not in settings.DEFAULTS:
        return False, f"Unknown settings key '{key}'. See GET /settings/schema."
    default = settings.DEFAULTS[key]
    if default is None:
        if value is None or isinstance(value, str):
            return True, value
        return False, "Expected a string or null."
    if isinstance(default, bool):
        if isinstance(value, bool):
            return True, value
        if isinstance(value, str) and value.strip().lower() in (
                "true", "false", "1", "0", "yes", "no", "on", "off"):
            return True, value.strip().lower() in ("true", "1", "yes", "on")
        return False, "Expected a boolean."
    if isinstance(default, int):
        try:
            return True, int(float(value))
        except (TypeError, ValueError):
            return False, "Expected an integer."
    if isinstance(default, float):
        try:
            return True, float(value)
        except (TypeError, ValueError):
            return False, "Expected a number."
    if isinstance(default, dict):
        if isinstance(value, dict):
            return True, value
        return False, "Expected an object."
    if isinstance(value, (str, int, float)):
        return True, str(value)
    return False, "Expected a string."


def mask_api_keys(key_status: dict) -> dict:
    """Strip raw secret values from config.get_key_status() output."""
    out = {}
    for name, info in (key_status or {}).items():
        out[name] = {k: v for k, v in info.items() if k != "value"}
    return out
