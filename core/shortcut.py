"""Windows shortcut (.lnk) helpers shared by the launcher and the app.

The launcher keeps a Start Menu shortcut pointing at launch.bat; the app keeps
that shortcut's icon in step with the active icon set (core/icons.py). Both
read and write .lnk files the same way, through WScript.Shell in PowerShell,
so the logic lives here once. Standard library only: launch.py imports this
before any dependency is installed.

Every function is a no-op that returns None or False off Windows.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SHORTCUT_NAME = "Meeting Assistant.lnk"

_PS = ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
_SEP = "---"


def _q(value) -> str:
    """Quote a value for a single-quoted PowerShell string."""
    return "'" + str(value).replace("'", "''") + "'"


def start_menu_shortcut() -> Path | None:
    """Where the launcher's Start Menu shortcut lives (Windows only)."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME


def pinned_taskbar_dir() -> Path | None:
    """Where Windows keeps the shortcuts behind taskbar pins."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Internet Explorer" / "Quick Launch" / "User Pinned" / "TaskBar"


def read(lnk: Path) -> dict | None:
    """Target, arguments, working directory and icon of *lnk*, or None."""
    if sys.platform != "win32" or not Path(lnk).exists():
        return None
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({_q(lnk)}); "
        f"Write-Output $s.TargetPath; Write-Output '{_SEP}'; "
        f"Write-Output $s.Arguments; Write-Output '{_SEP}'; "
        f"Write-Output $s.WorkingDirectory; Write-Output '{_SEP}'; "
        "Write-Output $s.IconLocation"
    )
    try:
        r = subprocess.run(_PS + [script], capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    parts = [p.strip() for p in r.stdout.split(_SEP)]
    if len(parts) < 4:
        return None
    icon = parts[3]
    icon_file = icon.split(",", 1)[0].strip() if icon else ""
    return {
        "target": parts[0],
        "arguments": parts[1],
        "workdir": parts[2],
        "icon": icon,
        "icon_file": icon_file,
    }


def write(lnk: Path, target: str, arguments: str, workdir: str,
          icon: Path | str | None, window_style: int | None = None) -> bool:
    """Create or overwrite *lnk*. Returns True when PowerShell reported success."""
    if sys.platform != "win32":
        return False
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({_q(lnk)}); "
        f"$s.TargetPath = {_q(target)}; "
        f"$s.Arguments = {_q(arguments)}; "
        f"$s.WorkingDirectory = {_q(workdir)}; "
    )
    if window_style is not None:
        script += f"$s.WindowStyle = {int(window_style)}; "
    if icon:
        script += f"$s.IconLocation = {_q(str(icon) + ', 0')}; "
    script += "$s.Save()"
    try:
        r = subprocess.run(_PS + [script], capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    return r.returncode == 0


def set_icon(lnk: Path, icon: Path | str) -> bool:
    """Point an existing shortcut at *icon*, keeping everything else as it is."""
    if sys.platform != "win32" or not Path(lnk).exists():
        return False
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({_q(lnk)}); "
        f"$s.IconLocation = {_q(str(icon) + ', 0')}; "
        "$s.Save()"
    )
    try:
        r = subprocess.run(_PS + [script], capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    return r.returncode == 0


def same_path(a: str | Path, b: str | Path) -> bool:
    """Case-insensitive, separator-insensitive path equality."""
    try:
        return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))
    except Exception:
        return str(a).lower() == str(b).lower()


def points_at(info: dict | None, bat_path: Path) -> bool:
    """True when a shortcut read by read() launches *bat_path*."""
    if not info:
        return False
    args = info.get("arguments", "") or ""
    return "cmd.exe" in (info.get("target", "") or "").lower() and str(bat_path).lower() in args.lower()


def our_shortcuts(bat_path: Path) -> list[Path]:
    """Every shortcut we know of that launches *bat_path*: the Start Menu one
    and any taskbar pin. Reading each one costs a PowerShell call."""
    found: list[Path] = []
    sm = start_menu_shortcut()
    if sm and sm.exists() and points_at(read(sm), bat_path):
        found.append(sm)
    pins = pinned_taskbar_dir()
    if pins and pins.is_dir():
        for lnk in pins.glob("*.lnk"):
            try:
                if points_at(read(lnk), bat_path):
                    found.append(lnk)
            except Exception:
                continue
    return found
