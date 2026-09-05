"""
Meeting Assistant launcher.
Handles dependency installation then starts the app.
Run via launch.bat (double-click) or directly: python launch.py
"""
import ctypes
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Under the tray launcher (launch_hidden.vbs) stdout is a file in the console
# code page, which cannot encode the arrows in a few progress lines; a
# UnicodeEncodeError there would abort the launch. Replace, never raise.
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream is not None and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(errors="replace")
    except Exception:
        pass

# ── ANSI colour setup ─────────────────────────────────────────────────────────

def _enable_ansi():
    if sys.platform == "win32":
        try:
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass

_enable_ansi()

R   = "\033[0m"
B   = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GRN = "\033[92m"
YLW = "\033[93m"
CYN = "\033[96m"
GRY = "\033[90m"

SEP_HEAVY = f"{CYN}{B}  {'=' * 54}{R}"
SEP_LIGHT = f"{GRY}  {'-' * 52}{R}"


def _ok(msg):   print(f"{GRN}  [OK] {msg}{R}")
def _info(msg): print(f"{GRY}       {msg}{R}")
def _warn(msg): print(f"{YLW}  [!!] {msg}{R}")
def _err(msg):  print(f"{RED}  [XX] {msg}{R}")

def _section(title):
    print()
    print(f"{B}  {title}{R}")
    print(SEP_LIGHT)

def _fatal(msg):
    print()
    _err(msg)
    print()
    input("Press Enter to exit...")
    sys.exit(1)

# ── uv helpers ────────────────────────────────────────────────────────────────

UV = ""  # resolved in main()

def _find_uv() -> str:
    """Locate the uv binary."""
    found = shutil.which("uv")
    if found:
        return found
    # Common install locations on Windows
    for candidate in [
        Path.home() / ".local" / "bin" / "uv.exe",
        Path.home() / ".local" / "bin" / "uv",
    ]:
        if candidate.exists():
            return str(candidate)
    return ""


def _uv_target() -> list[str]:
    """The interpreter uv should install into: the project venv, named explicitly.

    Passing ``--python <venv python>`` instead of relying on the active
    ``VIRTUAL_ENV`` matters on macOS: the stock python.org interpreter is a
    universal2 framework build whose sysconfig platform is
    ``macosx-10.9-universal2``. When uv discovers that interpreter via the
    environment (no ``--python``), it derives an *x86_64* wheel-tag set from
    that universal2 platform and then can't satisfy arm64-only wheels such as
    ``mlx`` (Apple-silicon-only), failing the whole install. An explicit
    ``--python`` makes uv probe the interpreter directly and resolve arm64
    wheels correctly. We keep the universal2 framework build (not a single-arch
    managed python) because the menu-bar tray needs a framework interpreter.
    """
    return ["--python", str(_venv_python())]


def _uv(*args, show_output=False):
    """Run uv pip install with the given args. Returns True on success."""
    cmd = [UV, "pip", "install"] + _uv_target()
    if not show_output:
        cmd.append("--quiet")
    cmd.extend(args)
    return subprocess.run(cmd).returncode == 0


def _uv_streaming(*args):
    """
    Run uv pip install and print filtered progress lines in real time.
    """
    cmd = [UV, "pip", "install"] + _uv_target() + list(args)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        lo = line.lower()
        if any(k in lo for k in ("downloading", "downloaded", "prepared",
                                   "resolved", "installed", "updated",
                                   "uninstalled")):
            print(f"{GRY}         {line}{R}", flush=True)
        elif "error" in lo:
            print(f"{RED}         {line}{R}", flush=True)
        elif line.startswith("+") or line.startswith("-"):
            print(f"{GRY}         {line}{R}", flush=True)
    proc.wait()
    return proc.returncode == 0


# ── Venv helpers ──────────────────────────────────────────────────────────────

VENV_DIR = Path(__file__).parent / ".venv"


def _venv_python() -> Path:
    """Return the expected Python executable path inside the venv."""
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _running_in_venv() -> bool:
    """True when this process IS the venv Python."""
    try:
        return Path(sys.executable).resolve() == _venv_python().resolve()
    except Exception:
        return False


def _venv_python_version() -> tuple[int, int] | None:
    """Return (major, minor) of the Python that created the venv, or None."""
    cfg = VENV_DIR / "pyvenv.cfg"
    if not cfg.exists():
        return None
    try:
        text = cfg.read_text()
        m = re.search(r"version\s*=\s*(\d+)\.(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return None


def _ensure_venv():
    """
    If not already running inside the venv, create (or rebuild) it, then
    re-exec this script using the venv Python.  Does not return on success.
    """
    if _running_in_venv():
        return  # already inside the venv - nothing to do

    need_create = not _venv_python().exists()

    # Staleness check: venv built with a different Python minor version
    if not need_create:
        venv_ver = _venv_python_version()
        cur_ver  = (sys.version_info.major, sys.version_info.minor)
        if venv_ver and venv_ver != cur_ver:
            print()
            _warn(
                f"Environment was built with Python {venv_ver[0]}.{venv_ver[1]} "
                f"but you're running {cur_ver[0]}.{cur_ver[1]}."
            )
            answer = input("      Rebuild environment? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes"):
                _info("Removing old environment...")
                shutil.rmtree(VENV_DIR, ignore_errors=True)
                need_create = True
            else:
                _warn("Skipping rebuild - things may not work correctly.")

    if need_create:
        _info("Creating Python environment (one-time setup)...")
        uv = _find_uv()
        if uv:
            r = subprocess.run([uv, "venv", str(VENV_DIR), "--python", "3.12", "--seed"])
        else:
            r = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if r.returncode != 0:
            _fatal("Failed to create virtual environment.")

    # Re-exec using venv Python
    os.execv(str(_venv_python()), [str(_venv_python())] + sys.argv)


# ── Start Menu shortcut ───────────────────────────────────────────────────────

_SHORTCUT_NAME = "Meeting Assistant.lnk"


def _warn_if_cloud_storage_path():
    """Warn when the project lives on a macOS File Provider volume
    (OneDrive, iCloud Drive, etc. under ~/Library/CloudStorage).

    Dataless-evicted files make Python imports and SQLite writes
    pathologically slow (observed: a single .pyc read blocking for minutes),
    and sync can corrupt the live database or recordings mid-write. This is a
    warning only and never blocks the launch. It is a no-op on Windows, whose
    paths never contain "/Library/CloudStorage/".
    """
    try:
        root = str(Path(__file__).resolve().parent)
    except Exception:
        return
    if "/Library/CloudStorage/" not in root:
        return
    _warn("Project folder is inside a cloud-synced volume (OneDrive/File Provider):")
    _info(root)
    _warn("Imports and database writes can be pathologically slow, and recordings")
    _warn("or the meetings database can corrupt when files sync mid-write.")
    _warn("Recommended: move this folder to a local path, e.g. ~/meeting-assistant")


def _other_launch_bat(arguments: str, ours: Path) -> Path | None:
    """The launch.bat a shortcut runs when it is a DIFFERENT, still-present
    checkout than *ours*; None when it is ours, gone, or unreadable."""
    m = re.search(r'([A-Za-z]:\\[^"]*?launch\.bat)', arguments or "", re.IGNORECASE)
    if not m:
        return None
    other = Path(m.group(1))
    try:
        if other.resolve() == ours.resolve():
            return None
    except Exception:
        if str(other).lower() == str(ours).lower():
            return None
    return other if other.exists() else None


def _create_start_menu_shortcut():
    """
    Ensures a Start Menu shortcut exists and points to the current launch.bat
    with the right working directory and icon. Reads the existing shortcut's
    properties via PowerShell and only re-saves when something is missing or
    stale, so we don't churn the .lnk on every launch. Self-heals shortcuts
    that were created before files moved (e.g. logo.ico relocating into
    ui_web/static/images/ during the package reorganization).
    No-ops silently on non-Windows.
    """
    if sys.platform != "win32":
        return

    root       = Path(__file__).parent
    bat_path   = root / "launch.bat"
    icon_path  = root / "ui_web" / "static" / "images" / "logo.ico"
    # The active icon set (Settings > Icons) supplies the shortcut's icon; the
    # default set is the bundled logo.ico above, so a first run never needs
    # more than the standard library here.
    try:
        from core import icons as _icons
        icon_path = Path(_icons.shortcut_icon_path())
    except Exception:
        pass
    start_menu = (
        Path(os.environ.get("APPDATA", ""))
        / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    )
    lnk_path = start_menu / _SHORTCUT_NAME

    if not bat_path.exists():
        return

    def _norm(p: str) -> str:
        try:
            return str(Path(p)).lower()
        except Exception:
            return (p or "").lower()

    # ── Check whether the existing shortcut already points here ──────────────
    already_correct = False
    if lnk_path.exists():
        try:
            # Read all four fields we care about. Sentinel between fields keeps
            # us safe even if one of them is empty.
            ps_read = (
                "$ws = New-Object -ComObject WScript.Shell; "
                f"$s  = $ws.CreateShortcut('{lnk_path}'); "
                "Write-Output $s.TargetPath; Write-Output '---'; "
                "Write-Output $s.Arguments; Write-Output '---'; "
                "Write-Output $s.WorkingDirectory; Write-Output '---'; "
                "Write-Output $s.IconLocation"
            )
            check = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_read],
                capture_output=True, text=True,
            )
            if check.returncode == 0:
                parts = [p.strip() for p in check.stdout.split("---")]
                if len(parts) >= 4:
                    cur_target, cur_args, cur_wd, cur_icon = parts[:4]
                    cur_icon_file = cur_icon.split(",", 1)[0].strip() if cur_icon else ""
                    target_ok = "cmd.exe" in cur_target.lower()
                    # Another checkout owns this shortcut (a second clone, a git
                    # worktree, a colleague's fork run from the same account):
                    # leave it alone while that checkout's launch.bat still
                    # exists. A sandbox launch must not re-point the user's
                    # Start Menu entry at itself (2026-09-05). A moved or
                    # deleted install still hands the entry over.
                    other = _other_launch_bat(cur_args, bat_path)
                    if other is not None:
                        print(f"{GRY}   Start Menu shortcut belongs to {other.parent}; left unchanged{R}")
                        return
                    args_ok   = str(bat_path) in cur_args
                    wd_ok     = _norm(cur_wd) == _norm(str(root))
                    if icon_path.exists():
                        # Icon must point at the right path AND that path must
                        # still resolve — catches the case where the icon was
                        # moved out from under a previously-valid shortcut.
                        icon_ok = (
                            _norm(cur_icon_file) == _norm(str(icon_path))
                            and Path(cur_icon_file).exists()
                        )
                    else:
                        # No icon to install — leave whatever is there alone.
                        icon_ok = True
                    if target_ok and args_ok and wd_ok and icon_ok:
                        already_correct = True
        except Exception:
            pass

    # Re-save the shortcut if the icon file is newer than the .lnk, so icon
    # updates to logo.ico actually propagate (Windows keys its icon cache off
    # the .lnk's mtime).
    if already_correct and icon_path.exists():
        try:
            if icon_path.stat().st_mtime > lnk_path.stat().st_mtime:
                already_correct = False
        except Exception:
            pass

    if already_correct:
        return

    # ── Create or update the shortcut ────────────────────────────────────────
    was_existing = lnk_path.exists()
    ps_script = (
        f"$ws = New-Object -ComObject WScript.Shell; "
        f"$s  = $ws.CreateShortcut('{lnk_path}'); "
        f"$s.TargetPath       = 'cmd.exe'; "
        f"$s.Arguments        = '/c \"\"{bat_path}\"\"'; "
        f"$s.WorkingDirectory = '{root}'; "
        f"$s.WindowStyle      = 7; "          # 7 = minimised
        + (f"$s.IconLocation = '{icon_path}, 0'; " if icon_path.exists() else "")
        + "$s.Save()"
    )

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            verb = "updated" if was_existing else "created"
            _ok(f"Start Menu shortcut {verb}")
        else:
            _warn("Could not update Start Menu shortcut (non-fatal)")
    except Exception:
        _warn("Could not update Start Menu shortcut (non-fatal)")


def _create_macos_app_shortcut():
    """Create (or refresh) a launcher app bundle in ~/Applications on macOS,
    mirroring the Windows Start Menu shortcut.

    The bundle makes Meeting Assistant appear in Launchpad / Spotlight / Finder
    and, importantly, gives macOS a stable app identity to attach the Screen
    Recording and Microphone (TCC) permissions to, instead of attributing them
    to Terminal or the python binary. It re-writes only when missing or when its
    launcher points at a different checkout (e.g. the folder was moved), so it
    does not churn on every launch. No-ops silently on non-macOS.
    """
    if sys.platform != "darwin":
        return

    root = Path(__file__).parent
    launcher = root / "launch.command"
    if not launcher.exists():
        return

    # Prefer the system /Applications (where Launchpad / the macOS Apps grid
    # looks) when it is writable by this user; otherwise fall back to the per-user
    # ~/Applications, which never needs admin rights.
    _sys_apps  = Path("/Applications")
    _apps_root = _sys_apps if os.access(_sys_apps, os.W_OK) else (Path.home() / "Applications")
    app_dir    = _apps_root / "Meeting Assistant.app"
    macos_dir  = app_dir / "Contents" / "MacOS"
    res_dir    = app_dir / "Contents" / "Resources"
    exe_path   = macos_dir / "MeetingAssistant"
    plist_path = app_dir / "Contents" / "Info.plist"

    # The bundle's executable just cd's into THIS checkout and hands off to
    # launch.command (uv + venv + app). We widen PATH first so a Finder/Launchpad
    # launch (which gets a minimal PATH) still finds Homebrew's ffmpeg and uv.
    exe_script = (
        "#!/bin/bash\n"
        'export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"\n'
        f'cd "{root}" || exit 1\n'
        'exec "./launch.command"\n'
    )

    # Self-heal: only rewrite when missing or when the embedded checkout path
    # has changed (the script content carries the root path).
    if exe_path.exists() and plist_path.exists():
        try:
            if exe_path.read_text() == exe_script:
                return
        except Exception:
            pass

    try:
        macos_dir.mkdir(parents=True, exist_ok=True)
        res_dir.mkdir(parents=True, exist_ok=True)

        exe_path.write_text(exe_script)
        exe_path.chmod(0o755)

        # App icon: build a proper multi-resolution .icns from the logo PNG via an
        # iconset + iconutil (a plain `sips -s format icns` often yields nothing on
        # a non-square / wrong-DPI source, which is why the icon was blank before).
        # Prefer the macOS-styled tile (squircle, margins, depth) over the bare
        # web logo, which looks unfinished full-bleed in Launchpad / the Dock.
        _img_dir = root / "ui_web" / "static" / "images"
        _macos_icon = _img_dir / "app_icon_macos.png"
        icon_src  = _macos_icon if _macos_icon.exists() else (_img_dir / "logo.png")
        icon_name = ""
        if icon_src.exists():
            try:
                import tempfile
                with tempfile.TemporaryDirectory() as _td:
                    iconset = Path(_td) / "icon.iconset"
                    iconset.mkdir()
                    specs = [
                        ("icon_16x16.png", 16),    ("icon_16x16@2x.png", 32),
                        ("icon_32x32.png", 32),    ("icon_32x32@2x.png", 64),
                        ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
                        ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
                        ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
                    ]
                    built = True
                    for fname, px in specs:
                        r = subprocess.run(
                            ["sips", "-z", str(px), str(px), str(icon_src),
                             "--out", str(iconset / fname)],
                            capture_output=True, timeout=20,
                        )
                        if r.returncode != 0:
                            built = False
                            break
                    if built:
                        subprocess.run(
                            ["iconutil", "-c", "icns", str(iconset),
                             "-o", str(res_dir / "AppIcon.icns")],
                            capture_output=True, timeout=20,
                        )
                if (res_dir / "AppIcon.icns").exists():
                    icon_name = "AppIcon"
            except Exception:
                pass

        icon_plist = (
            f"    <key>CFBundleIconFile</key>\n    <string>{icon_name}</string>\n"
            if icon_name else ""
        )
        plist_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "    <key>CFBundleName</key>\n    <string>Meeting Assistant</string>\n"
            "    <key>CFBundleDisplayName</key>\n    <string>Meeting Assistant</string>\n"
            "    <key>CFBundleIdentifier</key>\n"
            "    <string>com.higg.meetingassistant.launcher</string>\n"
            "    <key>CFBundleVersion</key>\n    <string>1.0</string>\n"
            "    <key>CFBundlePackageType</key>\n    <string>APPL</string>\n"
            "    <key>CFBundleExecutable</key>\n    <string>MeetingAssistant</string>\n"
            + icon_plist +
            "    <key>NSHighResolutionCapable</key>\n    <true/>\n"
            "    <key>LSMinimumSystemVersion</key>\n    <string>13.0</string>\n"
            "    <key>NSMicrophoneUsageDescription</key>\n"
            "    <string>Meeting Assistant records your microphone to transcribe meetings.</string>\n"
            "</dict>\n"
            "</plist>\n"
        )

        # Ad-hoc codesign so the bundle has a stable identity; macOS then keeps
        # its TCC grants across launches instead of re-prompting. Best-effort.
        try:
            subprocess.run(
                ["codesign", "--force", "--sign", "-", str(app_dir)],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass

        # Remove a stale copy in the other Applications location so the app does
        # not show up twice (e.g. an old ~/Applications bundle after we move to
        # /Applications), then make sure Launch Services indexes the new one.
        _other_root = (Path.home() / "Applications") if _apps_root == _sys_apps else _sys_apps
        _other = _other_root / "Meeting Assistant.app"
        if _other != app_dir and _other.exists():
            shutil.rmtree(_other, ignore_errors=True)
        try:
            subprocess.run(
                ["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                 "LaunchServices.framework/Support/lsregister", "-f", str(app_dir)],
                capture_output=True, timeout=20,
            )
        except Exception:
            pass

        _ok(f"Applications shortcut ready  {GRY}({app_dir}){R}")
    except Exception:
        _warn("Could not create the macOS Applications shortcut (non-fatal)")


# ── Storage layout migration ──────────────────────────────────────────────────

def _migrate_legacy_layout():
    """One-shot migration to the storage/ folder layout.

    Moves legacy project-root directories into ``storage/``:
      <project>/tools/   → <project>/storage/tools/
      <project>/models/  → <project>/storage/models/
      <project>/data/    → <project>/storage/data/   (only if at default location)

    The data folder is only migrated when it's still at the original default
    location — i.e. either ``.data_location`` is absent, or it points to
    ``<project>/data``. If the user has relocated their data via the System
    settings (pointer points elsewhere), we leave both the data folder and
    the pointer alone.

    After moving the default-located data folder, the pointer file is
    removed so ``core.paths.data_dir()`` resolves to the new default
    (``storage/data/``) without an explicit pointer.

    Idempotent — safe to call on every launch. Silent when there's nothing
    to do.
    """
    project_root = Path(__file__).parent
    storage_dir = project_root / "storage"
    pointer_file = project_root / ".data_location"

    moved: list[str] = []

    # tools/ and models/ are unconditional — always inside the project root.
    for name in ("tools", "models"):
        src = project_root / name
        dst = storage_dir / name
        if src.is_dir() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                moved.append(f"{name}/ -> storage/{name}/")
            except OSError as exc:
                _warn(f"Could not migrate {name}/ to storage/: {exc}")

    # data/ requires the pointer check.
    src_data = project_root / "data"
    dst_data = storage_dir / "data"
    if src_data.is_dir() and not dst_data.exists():
        # Mirror core.paths._read_pointer: empty / non-absolute / unreadable
        # pointer files are treated as "no pointer" — i.e. data is at the
        # default location and should be migrated. This keeps migration in
        # lockstep with how paths.py itself resolves the data dir.
        pointer_target: Path | None = None
        if pointer_file.exists():
            try:
                text = pointer_file.read_text(encoding="utf-8").strip()
                if text:
                    p = Path(text)
                    if p.is_absolute():
                        pointer_target = p
            except (OSError, UnicodeDecodeError, ValueError):
                # Unreadable / corrupt pointer file — treat as "no pointer".
                # Mirrors paths._read_pointer's defensive posture.
                pointer_target = None

        try:
            is_default_location = (
                pointer_target is None
                or pointer_target.resolve() == src_data.resolve()
            )
        except OSError:
            # If the pointer can't be resolved (broken path on a missing
            # volume, etc.), assume the user has data elsewhere and play
            # it safe — leave both data/ and the pointer alone.
            is_default_location = False

        if is_default_location:
            try:
                dst_data.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_data), str(dst_data))
                moved.append("data/ -> storage/data/")
            except OSError as exc:
                _warn(f"Could not migrate data/ to storage/data/: {exc}")
            else:
                # Migration succeeded: the new default IS storage/data/, so
                # the pointer becomes redundant. Best-effort cleanup — failing
                # to delete the pointer (e.g. it's a corrupt directory) does
                # NOT undo the successful data move and shouldn't surface as
                # a migration error to the user.
                try:
                    pointer_file.unlink()
                except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
                    pass

    if moved:
        _section("STORAGE LAYOUT")
        for line in moved:
            _ok(f"Migrated  {GRY}{line}{R}")


# ── GPU detection ─────────────────────────────────────────────────────────────

TORCH_INDEX = "https://download.pytorch.org/whl/"

def _detect_gpu():
    """Return (wheel_tag, gpu_name, cuda_ver_str) or ('cpu', '', '').

    On macOS the result is ('mps', <chip>, '') if Apple Silicon Metal is
    available, else ('cpu', '', ''). The 'mps' tag is purely informational —
    we don't pin a torch wheel on Mac because the default arm64 wheel is
    Metal-enabled out of the box.
    """
    if sys.platform == "darwin":
        # Cheap check: arm64 Apple Silicon. We don't probe torch here because
        # this runs before the venv has torch installed.
        try:
            arch = subprocess.run(
                ["uname", "-m"], capture_output=True, text=True
            ).stdout.strip()
            if arch == "arm64":
                # Try to read the chip name from sysctl. Falls back to a
                # generic label if the sysctl key isn't present.
                try:
                    chip = subprocess.run(
                        ["sysctl", "-n", "machdep.cpu.brand_string"],
                        capture_output=True, text=True,
                    ).stdout.strip() or "Apple Silicon"
                except Exception:
                    chip = "Apple Silicon"
                return "mps", chip, ""
        except Exception:
            pass
        return "cpu", "", ""

    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True)
        if r.returncode != 0:
            return "cpu", "", ""

        # nvidia-smi's header label changed with newer drivers: older builds
        # print "CUDA Version: 12.4", while driver 610+ prints
        # "CUDA UMD Version: 13.3". Match both so a driver update doesn't make
        # GPU detection silently fall back to CPU.
        m = re.search(r"CUDA(?:\s+UMD)?\s+Version:\s*(\d+)\.(\d+)", r.stdout)
        if not m:
            return "cpu", "", ""

        major, minor = int(m.group(1)), int(m.group(2))

        nr = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True,
        )
        gpu_name = (
            nr.stdout.strip().splitlines()[0].strip()
            if nr.returncode == 0 else "NVIDIA GPU"
        )

        if   major >= 13:                  whl = "cu130"
        elif major == 12 and minor >= 8:   whl = "cu128"
        elif major == 12 and minor >= 6:   whl = "cu126"
        elif major == 12 and minor >= 4:   whl = "cu124"
        elif major == 12 and minor >= 1:   whl = "cu121"
        elif major == 11 and minor >= 8:   whl = "cu118"
        else:                              whl = "cpu"

        return whl, gpu_name, f"{major}.{minor}"

    except FileNotFoundError:
        return "cpu", "", ""


def _torch_build() -> str:
    """
    Return the installed torch build variant, e.g. 'cu126', 'cpu', or ''
    if torch is not installed.  Reads torch.__version__ directly so it works
    regardless of the index URL used to install it.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import torch; print(torch.__version__)"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return ""
        ver = r.stdout.strip()          # e.g. "2.10.0+cu126", "2.10.0+cpu", "2.10.0"
        if "+" in ver:
            return ver.split("+", 1)[1] # "cu126" or "cpu"
        if ver:
            # Plain PyPI wheel with no local-version suffix (the macOS arm64
            # Metal build). Treat as installed so macOS doesn't reinstall torch
            # on every launch. "pypi" never equals a CUDA tag or "cpu", so the
            # Windows comparisons below (== whl, == "cpu") behave as before.
            return "pypi"
        return ""                       # torch not installed
    except Exception:
        return ""

# ── Model pre-download ───────────────────────────────────────────────────────

# Models that need to be cached before the app starts.
# Each entry: (display_name, download_function_name)
# The download functions are defined inside _predownload_models to avoid
# importing heavy libraries at module level.

def _hf_hub_cache_root() -> Path:
    """The HF hub cache the offline runtime reads non-pyannote models from.

    Mirrors huggingface_hub's own resolution (HF_HUB_CACHE > HF_HOME/hub) and
    core.config's HF_HOME default of the project-local storage/models.  Used for
    faster-whisper, transformers and sentence-transformers.
    """
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit)
    hf_home = os.environ.get("HF_HOME") or str(Path(__file__).parent / "storage" / "models")
    return Path(hf_home) / "hub"


def _pyannote_cache_roots() -> list:
    """Where pyannote/diart read their models — a SEPARATE cache from HF_HOME.

    pyannote.audio caches under PYANNOTE_CACHE (default ~/.cache/torch/pyannote),
    not HF_HOME, so that is where the offline runtime resolves pyannote models.
    Same models--org--name/refs/snapshots layout as the HF hub cache, just a
    different root. We return every plausible location.
    """
    roots = []
    env = os.environ.get("PYANNOTE_CACHE")
    if env:
        roots.append(Path(env))
    roots.append(Path.home() / ".cache" / "torch" / "pyannote")
    return roots


def _cache_roots_for(hf_model_id: str) -> list:
    """The cache root(s) the offline runtime actually reads this model from.

    pyannote models live in the pyannote/torch cache; everything else lives in
    HF_HOME/hub. We deliberately do NOT consult the user's global
    ~/.cache/huggingface: the app runs HF_HUB_OFFLINE=1 pinned to HF_HOME and
    will never load a non-pyannote model from there, so a copy sitting in the
    global cache must not count as 'present'.
    """
    if hf_model_id.startswith("pyannote/"):
        return _pyannote_cache_roots()
    return [_hf_hub_cache_root()]


def _is_model_cached(hf_model_id: str) -> bool:
    """True only if a snapshot the offline runtime can load is fully present.

    The runtime sets HF_HUB_OFFLINE=1 / local_files_only=True, so it reads only
    its cache and cannot repair a gap.  A loose "the folder has some file in it"
    check therefore lies: an interrupted download, an AV-quarantined blob, or a
    OneDrive Files-On-Demand placeholder all leave a directory that exists but
    has no loadable snapshot.  We verify what the loader actually needs:
    refs/<branch> resolving to a snapshots/<commit>/ whose files are materialised
    on disk, in the exact cache root the runtime reads this model from.
    """
    target = "models--" + hf_model_id.replace("/", "--")
    for root in _cache_roots_for(hf_model_id):
        model_dir = root / target
        if model_dir.is_dir() and _snapshot_complete(model_dir):
            return True
    return False


def _snapshot_complete(model_dir: Path) -> bool:
    """Verify model_dir has at least one fully-materialised snapshot.

    huggingface_hub resolves a model name to a commit via refs/<branch>, then
    loads snapshots/<commit>/.  Offline, a missing ref or an empty/partial
    snapshot is exactly the "cannot find an appropriate cached snapshot folder
    for the specified revision" failure, so we require a ref that points at a
    snapshot whose every file resolves to real, non-zero, on-disk content.
    """
    refs_dir = model_dir / "refs"
    snapshots_dir = model_dir / "snapshots"
    if not refs_dir.is_dir() or not snapshots_dir.is_dir():
        return False
    revisions = set()
    try:
        for ref in refs_dir.rglob("*"):
            if not ref.is_file():
                continue
            try:
                rev = ref.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if rev:
                revisions.add(rev)
    except (PermissionError, OSError):
        return False
    for rev in revisions:
        snap = snapshots_dir / rev
        if not snap.is_dir():
            continue
        files = [f for f in snap.rglob("*") if f.is_file() or f.is_symlink()]
        if files and all(_file_materialised(f) for f in files):
            return True
    return False


def _file_materialised(path: Path) -> bool:
    """True if path resolves to real on-disk content, not a stub or placeholder.

    Snapshot entries are symlinks into ../../blobs where the OS allows them and
    plain copies on Windows without Developer Mode; either way we resolve and
    stat.  A dangling link or 0-byte file fails.  On Windows we also reject cloud
    "online-only" placeholders (OneDrive Files-On-Demand): they report a logical
    size but aren't on disk, so the offline loader can't read them.
    """
    try:
        st = path.resolve(strict=True).stat()
    except (OSError, RuntimeError):
        return False
    if st.st_size <= 0:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE — set on cloud placeholders
    if attrs & (0x00400000 | 0x00040000 | 0x00001000):
        return False
    return True


# Map task IDs → HuggingFace model IDs for cache checking.
# On macOS the streaming Whisper engine is mlx-whisper (faster-whisper has no
# Metal backend), so we predownload the MLX-quantized large-v3 repo instead.
_MODEL_IDS = {
    "faster-whisper":        (
        "mlx-community/whisper-large-v3-mlx"
        if sys.platform == "darwin"
        else "Systran/faster-whisper-large-v3"
    ),
    "pyannote-segmentation": "pyannote/segmentation-3.0",
    "pyannote-embedding":    "pyannote/wespeaker-voxceleb-resnet34-LM",
    "pyannote-pipeline":     "pyannote/speaker-diarization-3.1",
    "whisper-turbo":         "openai/whisper-large-v3-turbo",
    "sentence-transformers": "sentence-transformers/all-MiniLM-L6-v2",
}


def _predownload_models():
    """Ensure all HuggingFace models are cached locally.

    First does a fast filesystem check for each model.  Only spawns the
    heavy download subprocess for models that aren't cached yet.
    """
    script_template = '''
import sys, os, warnings
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
warnings.filterwarnings("ignore")

# Load config which sets HF_TOKEN from .env or bundled token
from core import config as config
hf_token = os.environ.get("HF_TOKEN", "")

task = sys.argv[1]

if task == "faster-whisper":
    if sys.platform == "darwin":
        # mlx-whisper pulls from MLX-quantized repos under mlx-community/.
        from huggingface_hub import snapshot_download
        snapshot_download("mlx-community/whisper-large-v3-mlx")
    else:
        from faster_whisper import WhisperModel
        WhisperModel("large-v3", device="cpu", compute_type="int8")

elif task == "pyannote-segmentation":
    # Import diarizer to get all the torchaudio/speechbrain shims
    from ml import diarizer as diarizer  # noqa: F401
    from diart.models import SegmentationModel
    SegmentationModel.from_pretrained("pyannote/segmentation-3.0", use_hf_token=hf_token)

elif task == "pyannote-embedding":
    from ml import diarizer as diarizer  # noqa: F401
    from pyannote.audio import Model
    Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM", use_auth_token=hf_token)

elif task == "pyannote-pipeline":
    from ml import diarizer as diarizer  # noqa: F401
    from pyannote.audio import Pipeline
    Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)

elif task == "whisper-turbo":
    from transformers import AutoFeatureExtractor, AutoModelForSpeechSeq2Seq
    AutoFeatureExtractor.from_pretrained("openai/whisper-large-v3-turbo")
    AutoModelForSpeechSeq2Seq.from_pretrained("openai/whisper-large-v3-turbo")

elif task == "sentence-transformers":
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
'''

    models = [
        ("faster-whisper",        "Whisper large-v3 (streaming)"),
        ("pyannote-segmentation", "Pyannote segmentation"),
        ("pyannote-embedding",    "Pyannote speaker embedding"),
        ("pyannote-pipeline",     "Pyannote diarization pipeline"),
        ("whisper-turbo",         "Whisper large-v3-turbo (reanalysis)"),
        ("sentence-transformers", "Sentence embeddings"),
    ]

    # Fast check: skip models already in the local cache
    need_download = []
    for task_id, display_name in models:
        hf_id = _MODEL_IDS.get(task_id)
        if hf_id and _is_model_cached(hf_id):
            _ok(f"{display_name}")
        else:
            need_download.append((task_id, display_name))

    if not need_download:
        _ok("All models cached")
        return

    # Only write the download script if we actually need to download something
    script_path = Path(__file__).parent / ".model_download.py"
    script_path.write_text(script_template)

    max_attempts = 3
    all_ok = True
    for task_id, display_name in need_download:
        hf_id = _MODEL_IDS.get(task_id)
        success = False
        for attempt in range(1, max_attempts + 1):
            r = subprocess.run(
                [sys.executable, str(script_path), task_id],
                capture_output=True, text=True, timeout=600,
            )
            # Trust the cache, not the exit code. The runtime loads offline from
            # HF_HOME, so "downloaded" means a complete snapshot is actually
            # present there: a clean exit over a half-written cache is still a
            # failure, and a noisy non-zero exit over a good cache is still fine.
            if hf_id and _is_model_cached(hf_id):
                success = True
                break
            if r.returncode == 0 and not hf_id:
                success = True
                break
            if attempt < max_attempts:
                last = (r.stderr.strip().splitlines() or [""])[-1][:160]
                suffix = f"  {GRY}{last}{R}" if last else ""
                _info(f"{display_name} - retrying ({attempt}/{max_attempts})...{suffix}")
        if success:
            _ok(f"{display_name}")
        else:
            _warn(f"{display_name} - download failed after {max_attempts} attempts")
            all_ok = False

    # Clean up
    try:
        script_path.unlink()
    except OSError:
        pass

    if all_ok:
        _ok("All models cached")


# ── Main ──────────────────────────────────────────────────────────────────────

def _reexec_native_arm64():
    """On Apple Silicon, re-exec natively as arm64 if running x86_64 under Rosetta.

    The python.org interpreter is a universal2 build, so inside a Rosetta-translated
    process (e.g. the Terminal is set to "Open using Rosetta") it runs its x86_64
    slice. uv then resolves an x86_64 wheel-tag set: mlx ships arm64-only wheels and
    becomes unsatisfiable, and torch falls back to its last x86_64 macOS wheel.
    Forcing arm64 makes uv resolve arm64 and the app run native. An env var guards
    against a re-exec loop; a genuine Intel Mac (not translated) is left alone.

    launch.command applies the same guard before this script runs; this covers the
    direct ``python launch.py`` path and the in-app relaunch as a safety net.
    """
    if sys.platform != "darwin":
        return
    if os.environ.get("_MA_NATIVE_ARM64") == "1":
        return
    if os.uname().machine != "x86_64":
        return                       # already running arm64
    try:
        translated = subprocess.run(
            ["sysctl", "-n", "sysctl.proc_translated"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        translated = ""
    if translated != "1":
        return                       # genuine Intel Mac: an arm64 slice cannot run
    os.environ["_MA_NATIVE_ARM64"] = "1"
    try:
        os.execvp("arch", ["arch", "-arm64", sys.executable, *sys.argv])
    except Exception:
        pass                         # best effort: fall through if arch is missing


class _WatchdogDisabled(Exception):
    """Raised inside the watchdog start block when the preference is off."""


def _freeze_watchdog_enabled() -> bool:
    """Read freeze_watchdog_enabled straight from settings.json.

    Deliberately avoids importing core.settings (and its DEFAULTS) here: the
    launcher should not pull app modules in just to read one flag. The default
    is off, matching core/settings.py.
    """
    try:
        import json as _json
        from core import paths as _paths   # no project-internal imports of its own
        cfg = _paths.data_dir() / "settings.json"
        if not cfg.exists():
            return False
        return bool(_json.loads(cfg.read_text(encoding="utf-8")).get("freeze_watchdog_enabled", False))
    except Exception:
        return False


def main():
    global UV

    _reexec_native_arm64()   # ensure native arm64 before any venv / uv work
    os.chdir(Path(__file__).parent)
    _ensure_venv()   # re-execs into .venv if not already running inside it

    # Migrate legacy <project>/{tools,models,data}/ into storage/ on update.
    # Runs before any module imports that resolve those paths (HF_HOME,
    # ffmpeg lookup, settings/paths bootstrap).
    _migrate_legacy_layout()

    # Ensure uv can find the active venv
    os.environ["VIRTUAL_ENV"] = str(VENV_DIR)

    UV = _find_uv()
    if not UV:
        _fatal("uv not found. Please run launch.bat or install uv: https://docs.astral.sh/uv/")

    os.system("cls" if sys.platform == "win32" else "clear")

    # Banner
    print()
    print(SEP_HEAVY)
    print(f"{CYN}{B}   MEETING ASSISTANT{R}")
    print(f"{GRY}   Real-time transcription + AI  |  local  |  private{R}")
    print(SEP_HEAVY)

    # ── System ────────────────────────────────────────────────────────────────
    _section("SYSTEM")

    # Python version gate
    vi = sys.version_info
    if vi < (3, 10):
        _err(f"Python {vi.major}.{vi.minor}.{vi.micro} -- 3.10+ required")
        print()
        print("      Upgrade from: https://www.python.org/downloads/")
        _fatal("Python version too old")
    _ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")

    # Non-blocking warning if the project sits on a cloud-synced volume
    # (macOS OneDrive/iCloud File Provider). No-op on Windows.
    _warn_if_cloud_storage_path()

    # Launcher shortcut (first run / self-heal): Start Menu on Windows, an
    # Applications app bundle on macOS. Each no-ops on the other platform.
    _create_start_menu_shortcut()
    _create_macos_app_shortcut()

    # GPU
    whl, gpu_name, cuda_ver = _detect_gpu()
    if whl == "cpu":
        print(f"{GRY}  [--] No accelerator detected -- CPU mode{R}")
    elif whl == "mps":
        print(f"{GRN}  [OK] {gpu_name}  {GRY}|  Metal (MPS){R}")
    else:
        print(f"{GRN}  [OK] {gpu_name}  {GRY}|  CUDA {cuda_ver}{R}")

    # uv version
    try:
        uv_ver = subprocess.run([UV, "--version"], capture_output=True, text=True)
        _ok(f"uv  {GRY}{uv_ver.stdout.strip()}{R}")
    except Exception:
        _ok("uv")

    # ── Packages ──────────────────────────────────────────────────────────────
    _section("PACKAGES")

    # Cloudflare WARP's TLS inspection breaks pip/uv (untrusted CA).
    # Disconnect before package installs, reconnect after (git/HF need it).
    from core.network import warp_disconnect
    warp_disconnect()

    if sys.platform == "darwin":
        # Apple Silicon source-build hygiene. The stock python.org interpreter
        # is a universal2 framework build, so setuptools otherwise attempts a
        # fat x86_64+arm64 compile that can't link arm64-only artefacts. Pin
        # builds to arm64.
        os.environ["ARCHFLAGS"] = "-arch arm64"
        # aec-audio-processing compiles webrtc-audio-processing from source via
        # meson and finds abseil through pkg-config. On an Apple Silicon machine
        # that also carries an Intel (x86_64) Homebrew under /usr/local, meson
        # links the host's x86_64 abseil and the arm64 build fails. Point
        # pkg-config at an empty dir so meson falls back to abseil's bundled
        # arm64 subproject. Harmless on clean machines (subproject either way);
        # only source builds consult pkg-config, and the wheels above ignore it.
        _empty_pc = Path(__file__).parent / "storage" / "tools" / "empty-pkgconfig"
        _empty_pc.mkdir(parents=True, exist_ok=True)
        os.environ["PKG_CONFIG_LIBDIR"] = str(_empty_pc)
        os.environ["PKG_CONFIG_PATH"] = ""

        # Clear any x86_64-poisoned uv interpreter cache (once per venv). uv caches
        # each interpreter's wheel-tag set keyed by the (universal2) framework
        # python. A prior x86_64 run - e.g. under Rosetta before the arm64 re-exec
        # in launch.command existed - caches an x86_64 tag set that then persists
        # globally (it survives deleting this folder, since it is keyed by the
        # shared framework python) and makes uv pick x86_64 wheels: mlx (arm64-only)
        # goes unsatisfiable and torch drops to its last x86_64 build. Clearing it
        # forces uv to re-probe the interpreter natively as arm64. Cheap (re-probe
        # is instant) and the downloaded wheels in the archive cache are untouched.
        _cache_marker = VENV_DIR / ".uv-interp-cache-cleared"
        if not _cache_marker.exists():
            try:
                _uv_cache = subprocess.run(
                    [UV, "cache", "dir"], capture_output=True, text=True, timeout=10,
                ).stdout.strip()
                if _uv_cache:
                    for _interp in Path(_uv_cache).glob("interpreter-*"):
                        shutil.rmtree(_interp, ignore_errors=True)
            except Exception:
                pass
            try:
                _cache_marker.write_text("done\n")
            except Exception:
                pass

    # PyTorch - only install/replace when the installed variant doesn't match
    installed_build = _torch_build()   # e.g. "cu126", "cpu", or "" (not installed)

    if sys.platform == "darwin":
        # macOS: the default PyPI arm64 torch wheel ships Metal/MPS support.
        # No special index URL, no reinstall dance.
        if installed_build:
            _ok(f"PyTorch  {GRY}[arm64 | Metal/MPS]{R}")
        else:
            _info(f"PyTorch [arm64]  {GRY}(downloading...){R}")
            if not _uv_streaming("torch", "torchaudio"):
                _fatal("PyTorch install failed -- check your connection and retry")
            _ok(f"PyTorch  {GRY}[arm64 | Metal/MPS]{R}")
    elif whl == "cpu":
        if installed_build == "cpu":
            _ok(f"PyTorch  {GRY}[CPU]{R}")
        else:
            _info(f"PyTorch [CPU]  {GRY}(downloading...){R}")
            if not _uv_streaming("torch", "torchaudio", "--index-url", TORCH_INDEX + "cpu"):
                _fatal("PyTorch install failed -- check your connection and retry")
            _ok(f"PyTorch  {GRY}[CPU]{R}")
    else:
        if installed_build == whl:
            _ok(f"PyTorch  {GRY}[{whl} | GPU-accelerated]{R}")
        else:
            if installed_build:
                _info(f"PyTorch  {GRY}replacing {installed_build} → {whl}{R}")
            else:
                _info(f"PyTorch [{whl}]  {GRY}(downloading...){R}")
            # --reinstall-package is more targeted than pip's --force-reinstall
            if _uv_streaming("torch", "torchaudio",
                             "--reinstall-package", "torch",
                             "--reinstall-package", "torchaudio",
                             "--index-url", TORCH_INDEX + whl):
                _ok(f"PyTorch  {GRY}[{whl} | GPU-accelerated]{R}")
            else:
                _warn(f"GPU build failed -- falling back to CPU...")
                if not _uv_streaming("torch", "torchaudio", "--index-url", TORCH_INDEX + "cpu"):
                    _fatal("PyTorch install failed -- check your connection and retry")
                _ok(f"PyTorch  {GRY}[CPU | fallback]{R}")

    # Pre-install matplotlib from a binary wheel so diart's transitive pull
    # never triggers a source build (which requires MSVC on Windows).
    _uv("matplotlib>=3.8.0", "--only-binary", "matplotlib")

    # All other deps
    _info("Dependencies...")
    req_file = "requirements-macos.txt" if sys.platform == "darwin" else "requirements.txt"
    if not _uv_streaming("-r", req_file):
        _warn("Some packages failed -- retrying with full output...")
        print()
        if not _uv("-r", req_file, show_output=True):
            _fatal("Dependency install failed -- see errors above")
    _ok("All packages ready")

    # ── Pre-download models ──────────────────────────────────────────────────
    # Download all HuggingFace models now, while WARP is off and we control
    # the network state.  At runtime the cache-first approach means no network
    # calls are needed.  This step is fast when models are already cached.
    _section("MODELS")
    _predownload_models()

    # Leave WARP disconnected through app startup. Models are already cached
    # (above), so the app needs no network to start, and skipping WARP's TLS
    # inspection shaves a little off startup. If a corporate always-on policy
    # reconnects WARP anyway, that's fine now: core.config trusts WARP's CA via
    # the OS store (truststore), so HTTPS still verifies. The update-check
    # endpoint reconnects on demand via core.network.warp_reconnect().

    # ── FFmpeg ────────────────────────────────────────────────────────────────
    _section("FFMPEG")

    from capture_video import find_ffmpeg, download_ffmpeg, _LOCAL_FFMPEG

    ffmpeg_path = find_ffmpeg()
    if ffmpeg_path:
        # Get version string
        try:
            fv = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True, text=True, timeout=5,
            )
            # Keep this line short: take just the version token (drop the
            # "Copyright ... the FFmpeg developers" tail) and show the path
            # relative to the project root for the bundled binary.
            first = fv.stdout.split("\n")[0].strip() if fv.returncode == 0 else ""
            vm = re.search(r"ffmpeg version (\S+)", first)
            ver = f"ffmpeg {vm.group(1)}" if vm else "ffmpeg"
            try:
                disp = str(Path(ffmpeg_path).relative_to(Path(__file__).parent))
            except ValueError:
                disp = ffmpeg_path
            _ok(f"{ver}  {GRY}({disp}){R}")
        except Exception:
            _ok(f"ffmpeg  {GRY}({ffmpeg_path}){R}")
    else:
        _info(f"ffmpeg not found - downloading...  {GRY}(needed for screen recording){R}")
        try:
            download_ffmpeg(progress_cb=lambda msg: _info(msg))
            try:
                disp = str(Path(_LOCAL_FFMPEG).relative_to(Path(__file__).parent))
            except ValueError:
                disp = str(_LOCAL_FFMPEG)
            _ok(f"ffmpeg  {GRY}({disp}){R}")
        except Exception as e:
            _warn(f"Could not download ffmpeg: {e}")
            _warn("Screen recording will be unavailable. Install ffmpeg manually to enable it.")

    # ── macOS audio: ScreenCaptureKit needs no install-time setup ─────────
    # System audio is captured via Apple's ScreenCaptureKit (macOS 13+), so
    # there is no BlackHole driver to install and no aggregate device to wire.
    # The only requirement is the Screen & System Audio Recording permission,
    # granted on first capture.
    if sys.platform == "darwin":
        _section("macOS AUDIO")
        _info("System audio uses ScreenCaptureKit (macOS 13+); no virtual audio driver is needed.")
        _info("On first Record/Test, grant Screen & System Audio Recording, then")
        _info("restart Meeting Assistant once (macOS caches the permission per process).")

    # ── Launch ────────────────────────────────────────────────────────────────
    print()
    print(SEP_HEAVY)
    print(f"{B}   Launching...{R}")
    print(f"{GRY}   Your browser will open automatically.{R}")
    print(f"{GRY}   Close this window or use the tray icon to exit.{R}")
    print(SEP_HEAVY)
    print()

    # Force HF Hub offline so runtime model loaders (pyannote, transformers,
    # faster-whisper) load straight from the cache the pre-download populated,
    # skipping online revision checks entirely. This is a speed/robustness
    # fast-path; truststore (core.config) is what actually makes online TLS
    # work when a model isn't cached or WARP reconnects.
    child_env = os.environ.copy()
    child_env["HF_HUB_OFFLINE"] = "1"
    child_env["TRANSFORMERS_OFFLINE"] = "1"

    # Start the external freeze watchdog (watchdog.py) in a separate, hidden
    # process before launching the app. It survives a whole-process app freeze
    # (which the in-process code cannot catch) and restarts the app. It is a
    # singleton, so a watchdog-driven relaunch that re-enters this path will not
    # stack watchdogs. Opt-in via Settings > System; never let a watchdog
    # problem block the app launch.
    try:
        if not _freeze_watchdog_enabled():
            raise _WatchdogDisabled()
        subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "watchdog.py")],
            cwd=str(Path(__file__).parent), env=child_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except _WatchdogDisabled:
        pass
    except Exception as e:
        print(f"{GRY}   (freeze watchdog not started: {e}){R}")

    result = subprocess.run(
        [sys.executable, "-u", "-X", "faulthandler", "app.py"],
        stderr=subprocess.PIPE, text=True, env=child_env,
    )
    # Always leave a trace of how the app ended. Under the hidden launcher this
    # log is the only record, and a quiet exit code 0 with nothing else written
    # is otherwise indistinguishable from a launcher that never ran the app.
    print(f"{GRY}   Meeting Assistant exited (code {result.returncode}).{R}")
    if result.returncode == 0 and result.stderr and result.stderr.strip():
        for line in result.stderr.strip().splitlines()[-12:]:
            print(f"  {GRY}{line}{R}")
    if result.returncode != 0:
        print()
        _err(f"Meeting Assistant exited with an error (code {result.returncode}).")
        if result.stderr:
            # Show the last portion of stderr (the traceback or fault info)
            lines = result.stderr.strip().splitlines()
            # Find the last traceback or Fatal Python error block
            tb_start = 0
            for i, line in enumerate(lines):
                if line.startswith("Traceback") or line.startswith("Fatal Python error"):
                    tb_start = i
            relevant = lines[tb_start:]
            if len(relevant) > 40:
                relevant = relevant[-40:]
            print()
            for line in relevant:
                print(f"  {RED}{line}{R}")
        else:
            print()
            print(f"  {RED}No error output captured. The process may have crashed in native code.{R}")
            print(f"  {RED}Exit code: {result.returncode}{R}")
        print()
        # Only wait for a keypress when there's an interactive console. Under the
        # hidden tray launcher (launch_hidden.vbs) stdin is not a tty, so this
        # would block invisibly forever; the watchdog kills the app on a freeze,
        # and a blocking input() here would leave a stuck hidden process behind
        # on every recovery. Exit cleanly instead so the watchdog's relaunch is
        # tidy.
        try:
            interactive = bool(sys.stdin and sys.stdin.isatty())
        except Exception:
            interactive = False
        if interactive:
            input("Press Enter to exit...")


if __name__ == "__main__":
    main()
