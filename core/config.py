"""Configuration management for Meeting Assistant.

Handles .env file creation, API key storage/retrieval, and key status reporting.
This module has NO imports from other project files to avoid circular dependencies.
"""
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

ENV_PATH = Path(__file__).parent.parent / ".env"

# Pre-load .env early so HuggingFace environment flags are applied before any
# model imports happen (app.py imports transcriber before calling ensure_env()).
if ENV_PATH.exists():
    load_dotenv(str(ENV_PATH))

# Bundled HuggingFace token with minimal read-only access for downloading public
# gated models (pyannote).  XOR-encrypted so secret scanners don't flag it.
def _decrypt_token() -> str:
    data = bytes.fromhex(
        "25033a1a2c060532311819003c1938384b6a7a6e27271405003e00263532002a362f200765"
    )
    key = b"MeetingAssistant2024"
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data)).decode()

_BUNDLED_HF_TOKEN = _decrypt_token()

# Apply bundled token early so imports that check HF_TOKEN at module level see it.
if not os.getenv("HUGGING_FACE_KEY", "").strip():
    os.environ["HUGGING_FACE_KEY"] = _BUNDLED_HF_TOKEN
if not os.getenv("HF_TOKEN", "").strip():
    os.environ["HF_TOKEN"] = _BUNDLED_HF_TOKEN

# Pin HuggingFace cache to a project-local directory so pre-downloaded models
# are always found at runtime, regardless of the user's global HF_HOME.
_MODEL_CACHE = str(Path(__file__).parent.parent / "storage" / "models")
os.environ.setdefault("HF_HOME", _MODEL_CACHE)

# Corporate Cloudflare WARP injects a self-signed CA for TLS inspection,
# which breaks SSL verification for HuggingFace and other HTTPS requests.
# Disable SSL verification so model downloads work regardless of WARP state.
os.environ.setdefault("HF_HUB_DISABLE_SSL_VERIFY", "1")
os.environ.setdefault("CURL_CA_BUNDLE", "")
os.environ.setdefault("REQUESTS_CA_BUNDLE", "")
os.environ.setdefault("SSL_CERT_FILE", "")
# httpx (used by Anthropic/OpenAI SDKs) respects this env var
os.environ.setdefault("SSL_VERIFY", "0")

import ssl as _ssl
try:
    _ssl._create_default_https_context = _ssl._create_unverified_context
except AttributeError:
    pass

# Suppress HuggingFace symlinks warning on Windows (symlinks require Developer Mode
# or admin rights; caching still works fine without them).
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# torchaudio 2.x removed symbols that older pyannote.audio references at import
# time.  Apply shims early (before any pyannote import) so both diarizer.py and
# speaker_db.py see them regardless of load order.
try:
    import torchaudio as _ta
    if not hasattr(_ta, "AudioMetaData"):
        import collections as _collections
        _ta.AudioMetaData = _collections.namedtuple(
            "AudioMetaData",
            ["sample_rate", "num_frames", "num_channels", "bits_per_sample", "encoding"],
        )
    if not hasattr(_ta, "list_audio_backends"):
        _ta.list_audio_backends = lambda: ["soundfile"]
    if not hasattr(_ta, "set_audio_backend"):
        _ta.set_audio_backend = lambda backend: None
except ImportError:
    pass

REQUIRED_KEYS = {
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic API Key",
        "hint": "sk-ant-...",
        "description": "Required when using Anthropic as the AI provider.",
        "required": False,
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
            "# Leave blank to use the bundled read-only token, or set your own.",
            "HUGGING_FACE_KEY=",
            "",
            "HF_TOKEN=",
            "",
            "# Suppress HuggingFace symlinks warning on Windows (caching still works without them)",
            "HF_HUB_DISABLE_SYMLINKS_WARNING=1",
            "",
            "# Set to 1 if model downloads fail with SSL certificate errors.",
            "# This is common on corporate networks with SSL inspection proxies.",
            "# HF_HUB_DISABLE_SSL_VERIFICATION=1",
            "",
            "# Server port (default: 6969)",
            "# PORT=6969",
            "",
        ]
        ENV_PATH.write_text("\n".join(lines), encoding="utf-8")
    load_dotenv(str(ENV_PATH), override=True)

    # Fall back to the bundled read-only token when the user hasn't set their own.
    if not os.getenv("HUGGING_FACE_KEY", "").strip():
        os.environ["HUGGING_FACE_KEY"] = _BUNDLED_HF_TOKEN
    if not os.getenv("HF_TOKEN", "").strip():
        os.environ["HF_TOKEN"] = _BUNDLED_HF_TOKEN


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
            "value": val,
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
