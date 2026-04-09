"""
Network utilities — Cloudflare WARP management and cache-first model loading.

Corporate WARP uses TLS inspection which breaks pip/uv downloads.
Git operations require WARP connected for routing.  HuggingFace model
downloads may fail with WARP in either state depending on timing.

Strategy:
- launch.py toggles WARP off for pip, back on after.
- Runtime model loads use _load_hf_pipeline() which tries the local cache
  first, then toggles WARP off to attempt a fresh download if needed.
"""
import shutil
import subprocess

import log

_warp_cli: str | None = None  # cached path


def _find_warp_cli() -> str:
    global _warp_cli
    if _warp_cli is None:
        _warp_cli = shutil.which("warp-cli") or ""
    return _warp_cli


def _is_connected() -> bool | None:
    """Return True if connected, False if disconnected, None if unknown."""
    cli = _find_warp_cli()
    if not cli:
        return None
    try:
        r = subprocess.run([cli, "status"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        if "Disconnected" in r.stdout:
            return False
        if "Connected" in r.stdout:
            return True
        return None
    except Exception:
        return None


def warp_disconnect() -> bool:
    """Disconnect WARP. Returns True on success or if already disconnected."""
    cli = _find_warp_cli()
    if not cli:
        return True
    if _is_connected() is not True:
        return True
    try:
        subprocess.run([cli, "disconnect"], capture_output=True, timeout=10)
        log.info("network", "Cloudflare WARP disconnected (TLS inspection breaks pip)")
        return True
    except Exception as e:
        log.warn("network", f"Failed to disconnect WARP: {e}")
        return False


def warp_reconnect() -> bool:
    """Reconnect WARP. Returns True on success or if already connected."""
    cli = _find_warp_cli()
    if not cli:
        return True
    if _is_connected() is not False:
        return True
    try:
        subprocess.run([cli, "connect"], capture_output=True, timeout=10)
        log.info("network", "Cloudflare WARP reconnected")
        return True
    except Exception as e:
        log.warn("network", f"Failed to reconnect WARP: {e}")
        return False


def _load_hf_pipeline(model_id: str, hf_token: str):
    """Load a pyannote Pipeline, trying local cache first.

    1. Try local_files_only (no network needed — WARP state irrelevant)
    2. On cache miss, disconnect WARP and try a fresh download
    3. If that also fails, reconnect WARP and try once more

    Returns the Pipeline object or None on failure.
    """
    from pyannote.audio import Pipeline as PyannotePipeline

    # Attempt 1: local cache only — no network, no WARP issues
    try:
        return PyannotePipeline.from_pretrained(
            model_id,
            use_auth_token=hf_token,
            local_files_only=True,
        )
    except Exception:
        log.info("network", f"Model '{model_id}' not in cache, downloading...")

    # Attempt 2: download with WARP off
    warp_disconnect()
    try:
        return PyannotePipeline.from_pretrained(
            model_id,
            use_auth_token=hf_token,
        )
    except Exception:
        pass

    # Attempt 3: download with WARP on
    warp_reconnect()
    try:
        return PyannotePipeline.from_pretrained(
            model_id,
            use_auth_token=hf_token,
        )
    except Exception as e:
        log.error("network", f"Failed to load '{model_id}' (tried cache, WARP off, WARP on): {e}")
        return None
