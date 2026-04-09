"""
Network utilities — Cloudflare WARP management.

Corporate WARP setups use TLS inspection which breaks pip/uv (untrusted CA),
but git and HuggingFace downloads require WARP to be connected for routing.

- Call warp_disconnect() before pip/uv installs
- Call warp_reconnect() after pip/uv, before git/HF operations
- Both are safe to call repeatedly and no-op when WARP isn't installed.
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
