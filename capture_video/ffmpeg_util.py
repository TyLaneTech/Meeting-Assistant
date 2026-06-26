"""Cross-platform ffmpeg location, download, and process management.

Used by both the screen recorder backends and audio_capture's ffmpeg-mic
fallback (device code -3). Kept separate so platform-specific recorder
modules can import it without circular dependencies.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from core import log as log


_LOCAL_FFMPEG_DIR = Path(__file__).parent.parent / "storage" / "tools"
_FFMPEG_BIN_NAME = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
_LOCAL_FFMPEG = _LOCAL_FFMPEG_DIR / _FFMPEG_BIN_NAME

# Pin per-OS static builds. macOS arm64 build comes from evermeet.cx (signed,
# notarized, statically-linked); Homebrew's ffmpeg also works if the user has
# it installed (find_ffmpeg() falls through to PATH).
_FFMPEG_DOWNLOAD_URLS = {
    "win32":  "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "darwin": "https://evermeet.cx/ffmpeg/getrelease/zip",
}

FFMPEG_DOWNLOAD_URL = _FFMPEG_DOWNLOAD_URLS.get(sys.platform, _FFMPEG_DOWNLOAD_URLS["win32"])


def find_ffmpeg() -> str | None:
    """Return path to ffmpeg binary, preferring the project-local copy."""
    if _LOCAL_FFMPEG.exists():
        return str(_LOCAL_FFMPEG)
    return shutil.which("ffmpeg")


def download_ffmpeg(progress_cb=None) -> str:
    """Download a static ffmpeg build into tools/. Returns the binary path.

    progress_cb(message: str) is called for status updates.

    On macOS Apple Silicon we refuse to auto-download because the bundled
    URL (evermeet.cx) serves an x86_64 binary that runs through Rosetta, a
    silent perf footgun. We raise with a `brew install ffmpeg` instruction;
    the brew binary is native arm64 and is picked up by find_ffmpeg() via
    PATH on the next launch.
    """
    import urllib.request
    import platform as _platform

    if sys.platform == "darwin" and _platform.machine() == "arm64":
        raise RuntimeError(
            "Auto-downloading ffmpeg on Apple Silicon would install an x86_64 "
            "binary (evermeet.cx default). Run `brew install ffmpeg` once for "
            "a native arm64 build; the launcher will pick it up automatically."
        )

    _LOCAL_FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _LOCAL_FFMPEG_DIR / "ffmpeg-download.zip"

    if progress_cb:
        progress_cb("Downloading ffmpeg...")
    urllib.request.urlretrieve(FFMPEG_DOWNLOAD_URL, str(zip_path))

    if progress_cb:
        progress_cb("Extracting ffmpeg...")

    target_basenames = {"ffmpeg.exe"} if sys.platform == "win32" else {"ffmpeg"}

    with zipfile.ZipFile(zip_path, "r") as zf:
        match = None
        for name in zf.namelist():
            basename = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if basename in target_basenames:
                match = name
                break
        if match is None:
            zip_path.unlink(missing_ok=True)
            raise FileNotFoundError(
                f"{_FFMPEG_BIN_NAME} not found in downloaded archive"
            )
        _LOCAL_FFMPEG.write_bytes(zf.read(match))

    zip_path.unlink(missing_ok=True)

    if sys.platform != "win32":
        try:
            _LOCAL_FFMPEG.chmod(0o755)
        except Exception as e:
            log.warn("ffmpeg", f"Could not chmod ffmpeg binary: {e}")

    if progress_cb:
        progress_cb("ffmpeg ready")
    return str(_LOCAL_FFMPEG)


def kill_stale_ffmpeg() -> int:
    """Kill orphaned ffmpeg processes left over from a previous session.

    Returns the count killed. Note: this is process-wide — it will also kill
    any unrelated ffmpeg processes the user has running. Safe at startup
    because we always spawn fresh ffmpeg instances per session.
    """
    killed = 0
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/F", "/IM", "ffmpeg.exe"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if "SUCCESS" in line:
                    killed += 1
        else:
            # -x matches the exact process name (like taskkill /IM above);
            # -f would match the full command line and could kill innocent
            # bystanders such as `tail -f ffmpeg.log` or `vim ffmpeg_util.py`.
            r = subprocess.run(
                ["pkill", "-x", "ffmpeg"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                killed = 1  # pkill doesn't report a count
        if killed:
            log.info("ffmpeg", f"Killed {killed} stale ffmpeg process(es)")
    except Exception as e:
        log.warn("ffmpeg", f"Could not check for stale ffmpeg: {e}")
    return killed


def subprocess_no_window_flag() -> int:
    """CREATE_NO_WINDOW on Windows, 0 elsewhere — for subprocess.Popen flags."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
