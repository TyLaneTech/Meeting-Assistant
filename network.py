"""
Network utilities — Cloudflare WARP management.

Call ensure_warp_disconnected() before any operation that downloads models
or accesses HuggingFace.  Safe to call repeatedly; no-ops when WARP is
not installed or already disconnected.
"""
import shutil
import subprocess

import log


def ensure_warp_disconnected() -> bool:
    """Disconnect Cloudflare WARP if it is currently connected.

    Returns True if WARP was disconnected (or was already off), False on error.
    """
    warp_cli = shutil.which("warp-cli")
    if not warp_cli:
        return True  # not installed — nothing to do

    try:
        status = subprocess.run(
            [warp_cli, "status"],
            capture_output=True, text=True, timeout=5,
        )
        if status.returncode != 0 or "Disconnected" in status.stdout:
            return True  # already disconnected

        subprocess.run(
            [warp_cli, "disconnect"],
            capture_output=True, timeout=10,
        )
        log.info("network", "Cloudflare WARP disconnected (blocks model downloads)")
        return True
    except Exception as e:
        log.warn("network", f"Failed to disconnect WARP: {e}")
        return False
