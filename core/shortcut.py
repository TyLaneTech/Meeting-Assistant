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


def startup_shortcut() -> Path | None:
    """The sign-in autostart shortcut (Startup folder), Windows only."""
    if sys.platform != "win32":
        return None
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / SHORTCUT_NAME


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


# ── Windows' startup approval ────────────────────────────────────────────────
# A shortcut in the Startup folder is only half the story. Task Manager's
# Startup apps tab, Settings > Apps > Startup and every "speed up my PC" tool
# switch entries off by writing a disable flag here, and Windows then ignores
# the shortcut completely. Checking only that the .lnk exists reports the
# feature as on while nothing launches at sign-in, which is exactly what it did
# (2026-09-08). The value name is the shortcut's file name; the data is a 12
# byte blob whose first byte carries the flag (bit 0 set means disabled).
_APPROVAL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"


def startup_approval(name: str) -> str:
    """Whether Windows will run the Startup entry *name*.

    "enabled"  - approved, or no opinion recorded (the default)
    "disabled" - switched off outside this app; the shortcut will not run
    "unknown"  - not Windows, or the registry could not be read
    """
    if sys.platform != "win32":
        return "unknown"
    try:
        import winreg
    except ImportError:
        return "unknown"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPROVAL_KEY) as key:
            blob, _kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return "enabled"      # no entry means Windows has no objection
    except OSError:
        return "unknown"
    if not blob:
        return "enabled"
    return "disabled" if blob[0] & 0x01 else "enabled"


def approve_startup(name: str) -> bool:
    """Let Windows run the Startup entry *name* again.

    Deletes the disable flag rather than writing an enabled one: absence is
    Windows' own default for "approved", so there is no blob format to get
    wrong. Only ever called when the user turns the setting on, so it cannot
    quietly undo a choice they made in Task Manager.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APPROVAL_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return True           # nothing to clear
    except OSError:
        return False


def same_path(a: str | Path, b: str | Path) -> bool:
    """Case-insensitive, separator-insensitive path equality."""
    try:
        return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))
    except Exception:
        return str(a).lower() == str(b).lower()


LAUNCHER_FILES = ("launch.bat", "app_launcher.vbs", "launch_hidden.vbs")


def points_at(info: dict | None, bat_path: Path) -> bool:
    """True when a shortcut read by read() launches the checkout that owns
    *bat_path*: cmd running its launch.bat (the old form) or wscript running one
    of its .vbs launchers (the Start Menu and sign-in shortcuts since the silent
    launch)."""
    if not info:
        return False
    target = (info.get("target", "") or "").lower()
    args = (info.get("arguments", "") or "").lower()
    if "cmd.exe" in target and str(bat_path).lower() in args:
        return True
    if "wscript" in target:
        root = str(Path(bat_path).parent).lower()
        return any((root + os.sep + name) in args for name in LAUNCHER_FILES)
    return False


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
