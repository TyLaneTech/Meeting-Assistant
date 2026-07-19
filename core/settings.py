"""Simple JSON file-based settings store for Meeting Assistant.

Stores user preferences (device selections, model choices, UI state, etc.)
in a human-readable JSON file under the active data folder, resolved by
``paths.settings_path()``.

Thread-safe: all reads/writes are protected by a module-level lock.
"""
import json
import threading

from core import paths as paths

_lock = threading.Lock()


def _path():
    """Return the current settings.json path. Resolved on every call so
    a runtime data-folder migration takes effect immediately."""
    return paths.settings_path()

# Default values for all known settings.  Any key not present in the
# saved file will be filled in from here on load.
DEFAULTS: dict = {
    # Audio devices (indices or special values like -1=none, -2=browser mic)
    "loopback_device": "",       # "" = system default
    # Friendly name paired with loopback_device. PyAudio indices are positional
    # and get renumbered when the device list changes (headset plugged in,
    # meeting app adds a virtual endpoint, driver update, reboot), so the index
    # alone silently drifts onto the wrong device. The name lets capture
    # re-resolve the same physical device, the way mic_device already does.
    "loopback_device_name": "",
    "mic_device": "-2",          # -2 = browser mic

    # Whisper model preset id (e.g. "cuda-large-v3", "cpu-small")
    "whisper_preset": "",        # "" = auto-detect on startup

    # Diarizer device ("cuda", "cpu", or "" for auto)
    "diarizer_device": "",

    # UI preferences
    "sidebar_open": True,
    "sidebar_width": 252,
    "col_proportions": None,   # [f1, f2, f3] fractions; null = use default 1:1.1:1.1
    "playback_speed": "1",

    # Feature toggles
    "diarization_enabled": True,
    "auto_summary": True,

    # "Me" speaker / source-aware diarization. Microphone audio is always
    # attributed to a single "Me" speaker (the person running the app) and is
    # never diarized; only desktop/loopback audio is diarized. me_speaker_global_id
    # points at the chosen global_speakers profile (its voice embeddings are
    # purged so it can never be matched against desktop speakers). The first-run
    # popup is non-blocking; me_speaker_prompt_dismissed remembers a dismissal.
    "mic_is_me_enabled": True,
    "me_speaker_global_id": None,
    "me_speaker_prompt_dismissed": False,

    # AI provider and model (primary / fallback)
    "ai_provider": "openai",
    "ai_model": "gpt-5.4",

    # Per-tool overrides (null = use primary)
    "summary_provider": None,
    "summary_model": None,
    "chat_provider": None,
    "chat_model": None,
    "chapters_provider": None,
    "chapters_model": None,

    # AI Chapters. Auto-generate high-level topic markers as a meeting
    # progresses (gated so they aren't added too frequently). Granularity tunes
    # how coarse/fine the chapter breaks are. The system prompt is tunable in
    # the same 3-tier way as summary/chat (built-in default < global < session).
    "chapters_auto": True,
    "chapters_granularity": "balanced",   # "coarse" | "balanced" | "fine"
    "chapters_system_prompt": "",

    # Screen recording
    "screen_display": 0,
    "screen_preset": "performance",

    # Quiet recording reminder
    "quiet_prompt_enabled": True,
    "quiet_prompt_threshold_sec": 30,
    "quiet_prompt_audio_rms_threshold": 0.006,
    "quiet_prompt_require_no_transcript": True,
    "quiet_prompt_cooldown_sec": 120,

    # Meeting auto-detect (Zoom/Teams). Opt-in: when on, the app watches for a
    # live meeting (mic held by Zoom/Teams, or a Zoom meeting window) and, if
    # nothing is recording, toasts an offer to record. Default OFF.
    "meeting_detect_enabled": False,
    "meeting_detect_debounce": 2,          # consecutive ~2s polls before prompting
    "meeting_detect_cooldown_sec": 90,     # min seconds between meeting prompts
    # When ON (and auto-detect is enabled), skip the "want to record?" prompt and
    # start the recording automatically on detection, confirming with a toast.
    # Default OFF. Has no effect unless meeting_detect_enabled is also on.
    "meeting_detect_autostart": False,

    # Cloudflare WARP auto-toggle. When ON, the app briefly disconnects WARP
    # around package installs, model downloads, AI provider calls, and the
    # update check (WARP's TLS inspection historically broke those). Default
    # OFF: TLS is now verified against the OS trust store via truststore
    # (core.config), so toggling is unnecessary and would clobber the user's
    # VPN state. While OFF, core.network runs no warp-cli commands at all.
    "warp_toggle_enabled": False,

    # Per-session video offsets (start time in screen recording aligned with
    # transcript t=0). Keyed by session_id. Replaces the legacy
    # `video_offset_<session_id>` flat-key scheme — `_migrate_video_offsets()`
    # auto-migrates older settings files at load time.
    "video_offsets": {},
}


# ── Migrations ──────────────────────────────────────────────────────────────

def _migrate_video_offsets(settings: dict) -> bool:
    """Fold legacy ``video_offset_<session_id>`` keys into ``video_offsets``.

    Mutates ``settings`` in place. Returns True if anything changed (caller
    can use this to decide whether to persist).
    """
    offsets = settings.get("video_offsets")
    if not isinstance(offsets, dict):
        offsets = {}
        settings["video_offsets"] = offsets
    changed = False
    legacy_keys = [k for k in settings if k.startswith("video_offset_")]
    for k in legacy_keys:
        sid = k[len("video_offset_"):]
        if sid and sid not in offsets:
            offsets[sid] = settings[k]
        del settings[k]
        changed = True
    return changed


def _ensure_dir() -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    """Load settings from disk, merged with defaults for any missing keys."""
    with _lock:
        settings = dict(DEFAULTS)
        p = _path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass  # corrupted file - fall back to defaults
        _migrate_video_offsets(settings)
        return settings


def save(settings: dict) -> None:
    """Write the full settings dict to disk."""
    with _lock:
        _ensure_dir()
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)


def get(key: str, default=None):
    """Get a single setting value."""
    settings = load()
    return settings.get(key, default if default is not None else DEFAULTS.get(key))


def put(key: str, value) -> None:
    """Update a single setting and persist."""
    with _lock:
        settings = dict(DEFAULTS)
        p = _path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass
        _migrate_video_offsets(settings)
        settings[key] = value
        _ensure_dir()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)


def update(updates: dict) -> dict:
    """Merge multiple key-value pairs into settings and persist. Returns full settings."""
    with _lock:
        settings = dict(DEFAULTS)
        p = _path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass
        _migrate_video_offsets(settings)
        settings.update(updates)
        _ensure_dir()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)


# ── Per-session video offset helpers ────────────────────────────────────────

def get_video_offset(session_id: str, default: float = 0.0) -> float:
    """Return the persisted video offset for ``session_id`` (seconds)."""
    settings = load()
    offsets = settings.get("video_offsets") or {}
    raw = offsets.get(session_id, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def put_video_offset(session_id: str, value: float | None) -> None:
    """Persist a session's video offset. Pass ``None`` to delete the entry."""
    with _lock:
        settings = dict(DEFAULTS)
        p = _path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass
        _migrate_video_offsets(settings)
        offsets = settings.setdefault("video_offsets", {})
        if not isinstance(offsets, dict):
            offsets = {}
            settings["video_offsets"] = offsets
        if value is None:
            offsets.pop(session_id, None)
        else:
            offsets[session_id] = float(value)
        _ensure_dir()
        with open(p, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return dict(settings)
