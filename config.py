"""Configuration management for Meeting Assistant.

Handles .env file creation, API key storage/retrieval, and key status reporting.
This module has NO imports from other project files to avoid circular dependencies.
"""
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

ENV_PATH = Path(__file__).parent / ".env"

REQUIRED_KEYS = {
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic API Key",
        "hint": "sk-ant-...",
        "description": "Required when using Anthropic as the AI provider.",
        "required": True,
    },
    "OPENAI_API_KEY": {
        "label": "OpenAI API Key",
        "hint": "sk-...",
        "description": "Required when using OpenAI as the AI provider.",
        "required": True,
    },
    "HUGGING_FACE_KEY": {
        "label": "HuggingFace Token",
        "hint": "hf_...",
        "description": "Required for speaker diarization. Get one at huggingface.co/settings/tokens",
        "required": False,
    },
}


def ensure_env() -> None:
    """Create .env from template if it doesn't exist, then load it."""
    if not ENV_PATH.exists():
        lines = [
            "# Meeting Assistant Configuration",
            "# Get your Anthropic key at: https://console.anthropic.com/settings/keys",
            "ANTHROPIC_API_KEY=",
            "",
            "# Get your OpenAI key at: https://platform.openai.com/api-keys",
            "OPENAI_API_KEY=",
            "",
            "# Get your HuggingFace token at: https://huggingface.co/settings/tokens",
            "# (Optional - needed for speaker diarization)",
            "HUGGING_FACE_KEY=",
            "",
            "# Server port (default: 6969)",
            "# PORT=6969",
            "",
        ]
        ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    load_dotenv(str(ENV_PATH), override=True)


def get_key_status() -> dict:
    """Return status of all config keys (masked values, set/unset)."""
    result = {}
    for name, info in REQUIRED_KEYS.items():
        val = os.getenv(name, "").strip()
        result[name] = {
            "label": info["label"],
            "hint": info["hint"],
            "description": info["description"],
            "required": info["required"],
            "is_set": bool(val),
            "masked": _mask_key(val),
        }
    return result


def save_key(key_name: str, value: str) -> None:
    """Save a single key to .env and update the running environment."""
    if key_name not in REQUIRED_KEYS:
        raise ValueError(f"Unknown key: {key_name}")
    ensure_env()
    value = value.strip()
    set_key(str(ENV_PATH), key_name, value)
    os.environ[key_name] = value


def needs_setup(provider: str = "anthropic") -> bool:
    """True if the key for the active provider is missing."""
    if provider == "openai":
        return not bool(os.getenv("OPENAI_API_KEY", "").strip())
    return not bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _mask_key(key: str) -> str:
    """Mask a key for display: show first 6 and last 4 chars."""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "..." + key[-2:]
    return key[:6] + "..." + key[-4:]
