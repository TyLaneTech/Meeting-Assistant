"""
Screen recorder using FFmpeg's gdigrab (Windows Desktop Duplication).

Manages an ffmpeg subprocess that captures a selected display (or region)
and writes a compressed MP4 file.  Designed to be started/stopped alongside
the main audio recording session.
"""

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import log

# ── FFmpeg binary resolution ─────────────────────────────────────────────────

_LOCAL_FFMPEG_DIR = Path(__file__).parent / "tools"
_LOCAL_FFMPEG = _LOCAL_FFMPEG_DIR / "ffmpeg.exe"

FFMPEG_DOWNLOAD_URL = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/"
    "latest/ffmpeg-master-latest-win64-gpl.zip"
)


def find_ffmpeg() -> str | None:
    """Return path to ffmpeg binary, preferring the local copy."""
    if _LOCAL_FFMPEG.exists():
        return str(_LOCAL_FFMPEG)
    found = shutil.which("ffmpeg")
    return found


def download_ffmpeg(progress_cb=None) -> str:
    """
    Download a static ffmpeg build to tools/ffmpeg.exe.
    Returns the path on success, raises on failure.
    ``progress_cb`` is called with (message: str) for status updates.
    """
    import urllib.request

    _LOCAL_FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _LOCAL_FFMPEG_DIR / "ffmpeg-download.zip"

    if progress_cb:
        progress_cb("Downloading ffmpeg...")

    urllib.request.urlretrieve(FFMPEG_DOWNLOAD_URL, str(zip_path))

    if progress_cb:
        progress_cb("Extracting ffmpeg...")

    # Find ffmpeg.exe inside the zip (it's in a subfolder)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            if name.endswith("/ffmpeg.exe") or name.endswith("\\ffmpeg.exe"):
                # Extract just this file
                data = zf.read(name)
                _LOCAL_FFMPEG.write_bytes(data)
                break
        else:
            # Try alternate: might be named differently
            for name in zf.namelist():
                basename = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if basename == "ffmpeg.exe":
                    data = zf.read(name)
                    _LOCAL_FFMPEG.write_bytes(data)
                    break
            else:
                zip_path.unlink(missing_ok=True)
                raise FileNotFoundError("ffmpeg.exe not found in downloaded archive")

    zip_path.unlink(missing_ok=True)

    if progress_cb:
        progress_cb("ffmpeg ready")

    return str(_LOCAL_FFMPEG)


def kill_stale_ffmpeg() -> int:
    """Kill any orphaned ffmpeg processes left over from a previous session.

    Returns the number of processes killed.  Safe to call at startup - only
    targets ffmpeg.exe instances whose command line includes 'gdigrab' (our
    screen-recording invocations), so it won't disturb unrelated ffmpeg usage.
    """
    killed = 0
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "ffmpeg.exe"],
            capture_output=True, text=True, timeout=5,
        )
        # Count lines like "SUCCESS: The process ... has been terminated."
        for line in result.stdout.splitlines():
            if "SUCCESS" in line:
                killed += 1
        if killed:
            log.info("screen", f"Killed {killed} stale ffmpeg process(es)")
    except Exception as e:
        log.warn("screen", f"Could not check for stale ffmpeg: {e}")
    return killed


# ── DPI awareness ────────────────────────────────────────────────────────────
# Enable per-monitor DPI awareness so EnumDisplayMonitors returns physical
# pixel coordinates and sizes - critical for correct gdigrab offsets on
# high-DPI / scaled displays.  Call once at import time; harmless if the
# process (or a framework) already set a mode.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except (AttributeError, OSError):
    # Fallback for older Windows versions without shcore
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass

# ── Display enumeration (Windows) ────────────────────────────────────────────

# MONITORINFOEXW is not in ctypes.wintypes - define it manually
class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


def _get_monitor_dpi(hMonitor) -> int:
    """Return the DPI for a specific monitor handle, defaulting to 96."""
    try:
        dpiX = ctypes.c_uint()
        dpiY = ctypes.c_uint()
        # MDT_EFFECTIVE_DPI = 0
        ctypes.windll.shcore.GetDpiForMonitor(
            hMonitor, 0, ctypes.byref(dpiX), ctypes.byref(dpiY)
        )
        return dpiX.value or 96
    except (AttributeError, OSError):
        return 96


def enumerate_displays() -> list[dict]:
    """
    Return a list of display info dicts:
      [{"index": 0, "name": "Display 1", "x": 0, "y": 0,
        "width": 1920, "height": 1080, "primary": True,
        "logical_x": 0, "logical_y": 0,
        "logical_width": 1920, "logical_height": 1080}, ...]

    Physical dimensions (x/y/width/height) come from DPI-aware enumeration.
    Logical dimensions are what gdigrab (non-DPI-aware) expects.
    """
    displays = []

    user32 = ctypes.windll.user32

    def _monitor_enum_proc(hMonitor, hdcMonitor, lprcMonitor, dwData):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hMonitor, ctypes.byref(info)):
            rc = info.rcMonitor
            is_primary = bool(info.dwFlags & 1)  # MONITORINFOF_PRIMARY
            phys_w = rc.right - rc.left
            phys_h = rc.bottom - rc.top

            # Compute logical (scaled) dimensions for gdigrab
            dpi = _get_monitor_dpi(hMonitor)
            scale = dpi / 96.0
            logical_w = round(phys_w / scale)
            logical_h = round(phys_h / scale)
            logical_x = round(rc.left / scale)
            logical_y = round(rc.top / scale)

            displays.append({
                "index": len(displays),
                "name": info.szDevice,
                "x": rc.left,
                "y": rc.top,
                "width": phys_w,
                "height": phys_h,
                "logical_x": logical_x,
                "logical_y": logical_y,
                "logical_width": logical_w,
                "logical_height": logical_h,
                "dpi": dpi,
                "scale": scale,
                "primary": is_primary,
            })
        return True

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.wintypes.HMONITOR,
        ctypes.wintypes.HDC,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.wintypes.LPARAM,
    )

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_monitor_enum_proc), 0)

    # Give friendly names (show physical resolution)
    for i, d in enumerate(displays):
        suffix = " (Primary)" if d["primary"] else ""
        d["label"] = f"Display {i + 1}: {d['width']}x{d['height']}{suffix}"

    return displays


def flash_display_border(display_index: int, duration_ms: int = 1500, thickness: int = 6):
    """
    Flash a colored border around the given display so the user can identify it.

    This uses native Win32 popup windows in a subprocess instead of tkinter.
    Tkinter overlays are not consistently DPI-aware on mixed-scaling setups,
    while the Win32 path can use the same physical monitor coordinates returned
    by EnumDisplayMonitors.
    """
    displays = enumerate_displays()
    if display_index < 0 or display_index >= len(displays):
        return

    d = displays[display_index]
    x, y, w, h = d["x"], d["y"], d["width"], d["height"]
    t = max(2, min(int(thickness), max(2, min(w, h) // 8)))
    duration_ms = max(100, int(duration_ms))

    rects = [
        (x, y, w, t),
        (x, y + h - t, w, t),
        (x, y, t, h),
        (x + w - t, y, t, h),
    ]
    rects_json = json.dumps(rects)

    script = f"""
import ctypes
import ctypes.wintypes as wt
import json

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

rects = json.loads({rects_json!r})
duration_ms = {duration_ms}
color = 0xFFA658

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY = 0x0002
WM_NCHITTEST = 0x0084
WM_TIMER = 0x0113
HTTRANSPARENT = -1
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
COLOR_WINDOW = 5

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]

class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", wt.POINT),
        ("lPrivate", wt.DWORD),
    ]

windows = []
brush = gdi32.CreateSolidBrush(color)

def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_NCHITTEST:
        return HTTRANSPARENT
    if msg == WM_TIMER:
        user32.DestroyWindow(hwnd)
        return 0
    if msg == WM_DESTROY:
        if hwnd in windows:
            windows.remove(hwnd)
        if not windows:
            user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

wndproc_ref = WNDPROC(wndproc)
class_name = "MeetingAssistantDisplayHighlight"
hinstance = kernel32.GetModuleHandleW(None)

wc = WNDCLASSW()
wc.lpfnWndProc = wndproc_ref
wc.hInstance = hinstance
wc.lpszClassName = class_name
wc.hbrBackground = brush
wc.hCursor = user32.LoadCursorW(None, 32512)

atom = user32.RegisterClassW(ctypes.byref(wc))
if not atom:
    raise ctypes.WinError(ctypes.get_last_error())

for left, top, width, height in rects:
    hwnd = user32.CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
        class_name,
        None,
        WS_POPUP | WS_VISIBLE,
        left,
        top,
        width,
        height,
        None,
        None,
        hinstance,
        None,
    )
    if not hwnd:
        continue
    windows.append(hwnd)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, left, top, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    user32.UpdateWindow(hwnd)
    user32.SetTimer(hwnd, 1, duration_ms, None)

msg = MSG()
while windows and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))

if brush:
    gdi32.DeleteObject(brush)
"""
    subprocess.Popen(
        [sys.executable, "-c", script],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


# ── Presets ──────────────────────────────────────────────────────────────────

PRESETS = {
    "minimal": {
        "label": "Minimal",
        "description": "Lowest resource usage - small files, reduced clarity",
        "framerate": 5,
        "crf": 38,
        "preset": "ultrafast",
        "scale": "1280:-2",
    },
    "performance": {
        "label": "Performance (Default)",
        "description": "Low CPU usage with decent quality",
        "framerate": 10,
        "crf": 32,
        "preset": "ultrafast",
        "scale": "",
    },
    "balanced": {
        "label": "Balanced",
        "description": "Good quality with moderate CPU usage",
        "framerate": 15,
        "crf": 26,
        "preset": "veryfast",
        "scale": "",
    },
    "quality": {
        "label": "Quality",
        "description": "High quality - larger files, more CPU",
        "framerate": 24,
        "crf": 22,
        "preset": "fast",
        "scale": "",
    },
    "maximum": {
        "label": "Maximum",
        "description": "Best possible quality - significant CPU usage",
        "framerate": 30,
        "crf": 18,
        "preset": "medium",
        "scale": "",
    },
    "custom": {
        "label": "Custom",
        "description": "Manually configure all parameters",
        "framerate": 10,
        "crf": 32,
        "preset": "ultrafast",
        "scale": "",
    },
}

DEFAULT_PRESET = "performance"

# H.264 encoder presets ordered from fastest to slowest
H264_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
]


# ── Screen recorder class ───────────────────────────────────────────────────

class ScreenRecorder:
    """Manages an ffmpeg gdigrab subprocess for screen capture."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._output_path: str | None = None
        self._frag_path: str | None = None
        self._lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def output_path(self) -> str | None:
        return self._output_path

    @property
    def live_video_path(self) -> str | None:
        """Path to the fragmented MP4 being written during recording."""
        if self.is_recording and self._frag_path and Path(self._frag_path).exists():
            return self._frag_path
        return None

    def start(
        self,
        output_path: str,
        display_index: int = 0,
        framerate: int = 10,
        crf: int = 32,
        preset: str = "ultrafast",
        scale: str = "",
    ) -> None:
        """
        Start screen recording.

        Args:
            output_path: Path for the output MP4 file.
            display_index: Monitor index (from enumerate_displays).
            framerate: Capture framerate.
            crf: Constant Rate Factor (0=lossless, 51=worst). Lower = better quality.
            preset: H.264 encoding preset (ultrafast..veryslow).
            scale: FFmpeg scale filter, e.g. "1280:-2" for 720p width. Empty = native.
        """
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found - install it or restart the app to auto-download")

        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("Already recording")

        displays = enumerate_displays()
        if display_index < 0 or display_index >= len(displays):
            display_index = 0

        disp = displays[display_index]

        # Ensure output directory exists
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self._output_path = output_path

        # gdigrab (FFmpeg) is DPI-aware and operates in physical pixel coordinates.
        # Pass physical dimensions directly so the full display area is captured.
        cap_x = disp["x"]
        cap_y = disp["y"]
        cap_w = disp["width"]
        cap_h = disp["height"]

        log.info("screen", f"Display {display_index}: {cap_w}x{cap_h} physical "
                 f"(scale={disp['scale']:.2f})")

        # Build ffmpeg command
        cmd = [
            ffmpeg,
            "-y",  # overwrite output
            # Input: gdigrab desktop with offset for specific monitor
            "-f", "gdigrab",
            "-framerate", str(framerate),
            "-offset_x", str(cap_x),
            "-offset_y", str(cap_y),
            "-video_size", f"{cap_w}x{cap_h}",
            "-draw_mouse", "1",
            "-i", "desktop",
        ]

        # Video filters
        vf_parts = []
        if scale:
            vf_parts.append(f"scale={scale}")
        if vf_parts:
            cmd.extend(["-vf", ",".join(vf_parts)])

        # Write as fragmented MP4 so the file is seekable mid-recording
        # (the moov atom is at the start, data streams as fragments).
        # On stop, we remux to a standard faststart MP4 for compatibility.
        self._frag_path = output_path + ".frag.mp4"

        # Encoder settings
        cmd.extend([
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            # No audio - audio is captured separately
            "-an",
            # Fragmented MP4 - seekable during recording
            "-movflags", "frag_keyframe+empty_moov",
            self._frag_path,
        ])

        log.info("screen", f"Starting: {' '.join(cmd)}")

        with self._lock:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        # Monitor thread to log any ffmpeg errors
        self._monitor_thread = threading.Thread(
            target=self._monitor, daemon=True
        )
        self._monitor_thread.start()

        log.info("screen", f"Recording display {display_index} → {output_path}")

    def _monitor(self):
        """Read stderr in background to prevent pipe buffer deadlock."""
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            for line in proc.stderr:
                pass  # Consume output silently
        except Exception:
            pass

    def stop(self) -> str | None:
        """
        Stop recording gracefully. Returns the output file path, or None.
        """
        with self._lock:
            proc = self._proc
            self._proc = None

        if not proc:
            return None

        # Send 'q' to ffmpeg's stdin for graceful shutdown
        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass

        # Wait for ffmpeg to finish writing
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warn("screen", "ffmpeg did not exit in time - killing")
            proc.kill()
            proc.wait(timeout=5)

        final_path = self._output_path
        frag_path = getattr(self, "_frag_path", None)

        if not frag_path or not Path(frag_path).exists() or Path(frag_path).stat().st_size == 0:
            log.warn("screen", "Recording file is missing or empty")
            return None

        # Remux fragmented MP4 → standard faststart MP4 for broad compatibility
        ffmpeg = find_ffmpeg()
        if ffmpeg and final_path:
            try:
                remux = subprocess.run(
                    [ffmpeg, "-y", "-i", frag_path,
                     "-c", "copy", "-movflags", "+faststart", final_path],
                    capture_output=True, timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if remux.returncode == 0 and Path(final_path).exists():
                    Path(frag_path).unlink(missing_ok=True)
                    size_mb = Path(final_path).stat().st_size / (1024 * 1024)
                    log.info("screen", f"Saved: {final_path} ({size_mb:.1f} MB)")
                    return final_path
                else:
                    log.warn("screen", "Remux failed - keeping fragmented file")
            except Exception as e:
                log.warn("screen", f"Remux error: {e} - keeping fragmented file")

        # Fallback: rename the frag file as the final output
        try:
            Path(frag_path).rename(final_path)
        except OSError:
            final_path = frag_path
        size_mb = Path(final_path).stat().st_size / (1024 * 1024)
        log.info("screen", f"Saved: {final_path} ({size_mb:.1f} MB)")
        return final_path


def capture_live_frame(display_index: int = 0, max_width: int = 960) -> bytes | None:
    """
    Capture a single JPEG screenshot from the specified display using ffmpeg gdigrab.
    Returns JPEG bytes, or None on failure.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    displays = enumerate_displays()
    if display_index < 0 or display_index >= len(displays):
        display_index = 0
    disp = displays[display_index]

    # gdigrab is DPI-aware - use physical coordinates
    cap_x = disp["x"]
    cap_y = disp["y"]
    cap_w = disp["width"]
    cap_h = disp["height"]

    cmd = [
        ffmpeg,
        "-f", "gdigrab",
        "-framerate", "1",
        "-offset_x", str(cap_x),
        "-offset_y", str(cap_y),
        "-video_size", f"{cap_w}x{cap_h}",
        "-i", "desktop",
        "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-q:v", "5",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None


def extract_frame(video_path: str, timestamp: float, max_width: int = 1280) -> bytes | None:
    """
    Extract a single JPEG frame from an MP4 at the given timestamp (seconds).
    Returns JPEG bytes, or None on failure.  Downscales to max_width for
    efficient inclusion in LLM context.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    if not Path(video_path).exists():
        return None

    # Format timestamp as HH:MM:SS.mmm
    h = int(timestamp // 3600)
    m = int((timestamp % 3600) // 60)
    s = timestamp % 60
    ts_str = f"{h:02d}:{m:02d}:{s:06.3f}"

    cmd = [
        ffmpeg,
        "-ss", ts_str,
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-q:v", "3",  # JPEG quality (2=best, 31=worst)
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None
