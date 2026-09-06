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
    # Sidebar navigation (Settings > System > Sidebar): which page links show
    # at the top of the sidebar, and whether they fold into small icons beside
    # the app name. Needs attention is off by default: the Home dashboard
    # already lists what needs speaker work, and the sidebar's height is
    # better spent on the recordings list.
    "sidebar_nav_compact": False,
    "sidebar_nav_items": {"home": True, "calendar": True, "attention": False, "speakers": True},
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
    # When ON, stop an auto-started recording once the meeting has been gone for
    # ~45s. Without this an auto-started recording runs forever, the session
    # never finalizes, and no title / summary / Obsidian export is ever produced.
    # Only ever stops recordings that auto-start began, never a manual one.
    "meeting_detect_autostop": True,
    # Chrome extension id of the installed Meeting Assistant PWA (the app the
    # taskbar pin launches, via app_launcher.vbs). When set, an auto-detected
    # meeting focuses THAT window instead of opening a second one: core/browser
    # launches chrome_proxy.exe --app-id=<this>. Blank it to always use a plain
    # --app= window.
    "pwa_app_id": "",

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

    # Obsidian export: drop finalized transcripts as markdown into a vault
    # folder, and keep the file current when the transcript is edited after.
    "obsidian_export_enabled": False,
    "obsidian_export_dir": "",
    # Speaker-resolution gate for the Obsidian export. When on, a finalized
    # meeting is withheld from the vault until its content-bearing speakers are
    # named (no lingering "Speaker N"). Phantom over-splits under BOTH thresholds
    # are ignored, so a stray segment can't hold a named meeting hostage. Force a
    # specific held session out by adding its id to obsidian_export_force_ids.
    # Existing exports are never removed; this only withholds new writes.
    "obsidian_gate_enabled": True,
    "obsidian_gate_min_seconds": 15.0,
    "obsidian_gate_min_words": 25,
    "obsidian_export_force_ids": [],

    # Freeze watchdog (watchdog.py). When ON, launch.py starts a separate hidden
    # process that restarts the app if the whole process stops responding.
    # Opt-in: it is an extra moving part, and a stuck app on a machine that
    # does not need it is rarer than a surprise relaunch. Read by launch.py
    # directly from settings.json, so it takes effect on the next launch.
    "freeze_watchdog_enabled": False,

    # Loopback follows the audio (Windows). OFF (the default) records the
    # desktop device the user selected, re-resolved by name, exactly as chosen;
    # only the silence alarm runs. ON binds to the current Windows default
    # output at start and lets the silence watchdog ask the out-of-process
    # render probe which output device is actually playing, re-binding the
    # desktop capture to it mid-recording (call apps often render to the
    # Communications-role device). Needs pycaw + comtypes; with them missing
    # the probe is a no-op.
    "loopback_follow_output": False,

    # The icon set in use (Settings > Icons): "default" (the owner's logo),
    # "wave" (Pat Gordon's), or the id of a custom set under <data>/icons/sets.
    # It drives the sidebar, the tab, the installed app, the tray and the
    # Start Menu shortcut together. Written only by core.icons.
    "icon_set": "default",

    # Voice-library automated maintenance. When enabled, the app periodically
    # (every library_maintenance_days, while idle) runs the same hygiene pass
    # exposed at POST /api/fingerprint/library/maintenance: merge same-name
    # duplicate profiles, remove embeddings that clearly belong to another
    # person's voice, purge pollution modes, prune, and recompute centroids.
    # last_run is machine-managed (UTC ISO timestamp of the last applied run).
    "library_maintenance_enabled": True,
    "library_maintenance_days": 7,
    "library_maintenance_last_run": "",

    # Calendar (published Outlook ICS feed). The owner publishes his calendar
    # from Outlook on the web and pastes the ICS link here; the app matches each
    # recording to an event by time, uses the attendee count as a ceiling for
    # reanalysis, and offers attendee names in the Speakers Cleanup picker. Nothing is
    # renamed automatically: names land on voices only via the Voice Library.
    # calendar_ics_url is a credential (anyone holding it can read the calendar)
    # so it is masked in every API response and never logged.
    # calendar_last_refresh / calendar_last_error are machine-managed.
    "calendar_enabled": False,
    "calendar_ics_url": "",
    "calendar_timezone": "America/Chicago",
    "calendar_refresh_minutes": 60,
    "calendar_match_window_minutes": 20,
    "calendar_last_refresh": "",
    "calendar_last_error": "",

    # Agent API (REST + MCP interface for external AI agents, agent_api/).
    # The server only ever binds to 127.0.0.1, so exposure is local-only.
    # agent_api_token, when non-empty, additionally requires a Bearer token on
    # every /api/agent/v1 request (the bundled MCP server picks it up
    # automatically from this file). Recording control via agents is opt-in
    # and OFF by default - reading data never is.
    "agent_api_enabled": True,
    "agent_api_token": "",
    "agent_api_allow_recording_control": False,
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
