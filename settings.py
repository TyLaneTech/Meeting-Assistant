"""Simple JSON file-based settings store for Meeting Assistant.

Stores user preferences (device selections, model choices, UI state, etc.)
in a human-readable JSON file under data/settings.json.

Thread-safe: all reads/writes are protected by a module-level lock.
This module has NO imports from other project files to avoid circular deps.
"""
import json
import threading
from pathlib import Path

_SETTINGS_PATH = Path(__file__).parent / "data" / "settings.json"
_lock = threading.Lock()

# Default values for all known settings.  Any key not present in the
# saved file will be filled in from here on load.
DEFAULTS: dict = {
    # Audio devices (indices or special values like -1=none, -2=browser mic)
    "loopback_device": "",       # "" = system default
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

    # AI provider and model
    "ai_provider": "openai",
    "ai_model": "gpt-5.4",

    # Screen recording
    "screen_display": 0,
    "screen_preset": "performance",
}


def _ensure_dir() -> None:
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    """Load settings from disk, merged with defaults for any missing keys."""
    with _lock:
        settings = dict(DEFAULTS)
        if _SETTINGS_PATH.exists():
            try:
                with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass  # corrupted file — fall back to defaults
        return settings


def save(settings: dict) -> None:
    """Write the full settings dict to disk."""
    with _lock:
        _ensure_dir()
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)


def get(key: str, default=None):
    """Get a single setting value."""
    settings = load()
    return settings.get(key, default if default is not None else DEFAULTS.get(key))


def put(key: str, value) -> None:
    """Update a single setting and persist."""
    with _lock:
        settings = dict(DEFAULTS)
        if _SETTINGS_PATH.exists():
            try:
                with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass
        settings[key] = value
        _ensure_dir()
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)


def update(updates: dict) -> dict:
    """Merge multiple key-value pairs into settings and persist. Returns full settings."""
    with _lock:
        settings = dict(DEFAULTS)
        if _SETTINGS_PATH.exists():
            try:
                with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved, dict):
                    settings.update(saved)
            except (json.JSONDecodeError, OSError):
                pass
        settings.update(updates)
        _ensure_dir()
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return dict(settings)
