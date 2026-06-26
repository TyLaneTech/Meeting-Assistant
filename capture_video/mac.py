"""macOS screen recorder backend: FFmpeg avfoundation + Quartz CGGetActiveDisplayList.

Public API mirrors screen_recorder_win.py exactly so the dispatcher can
import either backend transparently.

Display indexing model:
- Quartz returns CGDirectDisplayIDs that we list in deterministic order
  (main display first, then by display ID).
- avfoundation indexes displays separately ("Capture screen 0", "Capture
  screen 1", ...). On a single-display Mac these align; on multi-display
  setups we map our display_index to the avfoundation index by querying
  the avfoundation device list at start time.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from core import log as log
from capture_video.ffmpeg_util import find_ffmpeg


# ── Display enumeration via Quartz ──────────────────────────────────────────

def _list_quartz_displays() -> list[dict]:
    """Return display info using Quartz (CoreGraphics) APIs via PyObjC."""
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGGetActiveDisplayList,
            CGDisplayBounds,
            CGDisplayCopyDisplayMode,
            CGDisplayModeGetPixelWidth,
            CGDisplayModeGetPixelHeight,
            CGMainDisplayID,
        )
    except ImportError:
        log.warn("screen", "pyobjc-framework-Quartz not installed; cannot enumerate displays")
        return []

    # Query active display IDs (max 32 displays, plenty in practice)
    err, display_ids, count = CGGetActiveDisplayList(32, None, None)
    if err != 0 or not display_ids:
        return []

    main_id = CGMainDisplayID()
    # Main display first, then the rest by ID for stable ordering.
    ordered = [main_id] + [d for d in display_ids if d != main_id]

    displays: list[dict] = []
    for idx, did in enumerate(ordered):
        bounds = CGDisplayBounds(did)
        # Logical (point) coordinates from CGDisplayBounds origin/size.
        logical_w = int(bounds.size.width)
        logical_h = int(bounds.size.height)
        logical_x = int(bounds.origin.x)
        logical_y = int(bounds.origin.y)
        # True framebuffer pixels from the current display mode. (Do NOT use
        # CGDisplayPixelsWide/High here - on Retina HiDPI scaled modes those
        # return the logical point size, making scale always read 1.0.)
        mode = CGDisplayCopyDisplayMode(did)
        if mode is not None:
            phys_w = int(CGDisplayModeGetPixelWidth(mode))
            phys_h = int(CGDisplayModeGetPixelHeight(mode))
        else:
            phys_w, phys_h = logical_w, logical_h
        scale = (phys_w / logical_w) if logical_w else 1.0

        is_primary = (did == main_id)
        suffix = " (Primary)" if is_primary else ""

        displays.append({
            "index": idx,
            "name": f"Display {idx + 1}",
            "x": logical_x,
            "y": logical_y,
            "width": phys_w,
            "height": phys_h,
            "logical_x": logical_x,
            "logical_y": logical_y,
            "logical_width": logical_w,
            "logical_height": logical_h,
            "dpi": int(96 * scale),
            "scale": float(scale),
            "primary": is_primary,
            "label": f"Display {idx + 1}: {phys_w}x{phys_h}{suffix}",
            # macOS-only: CG display ID, used to map to avfoundation index.
            "_cg_display_id": int(did),
        })
    return displays


def enumerate_displays() -> list[dict]:
    """Public API: list displays in dispatcher-compatible dict shape."""
    return _list_quartz_displays()


# ── avfoundation device-index discovery ─────────────────────────────────────

def _avfoundation_screen_indexes() -> dict[int, int]:
    """Map Quartz display order index -> avfoundation video device index.

    ffmpeg's `-f avfoundation -list_devices true` enumerates "Capture screen 0",
    "Capture screen 1", etc. The order matches Quartz's display order on every
    macOS version we've tested, so we just identity-map by enumeration position.
    Returned dict keys are our display_index values, values are the avfoundation
    device indices to pass after `-i`.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return {}

    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-f", "avfoundation",
             "-list_devices", "true", "-i", ""],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log.warn("screen", f"avfoundation device list failed: {e}")
        return {}

    # ffmpeg writes the device list to stderr.
    text = r.stderr or ""
    screens: list[int] = []
    in_video = False
    for line in text.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
            continue
        if "AVFoundation audio devices" in line:
            in_video = False
            continue
        if not in_video:
            continue
        m = re.search(r"\[(\d+)\]\s*Capture screen\s*(\d+)", line)
        if m:
            screens.append(int(m.group(1)))

    return {our_idx: av_idx for our_idx, av_idx in enumerate(screens)}


def _resolve_av_screen_index(display_index: int) -> int:
    """Resolve our display_index to a positively-matched avfoundation index.

    Never guess: avfoundation puts webcams at the low device indices (on a
    typical Mac [0] is a camera, screens start at [3]), so falling back to
    the raw display_index would record the camera instead of the screen.
    The listing subprocess can fail transiently, so retry once before
    raising.
    """
    for attempt in (1, 2):
        av_map = _avfoundation_screen_indexes()
        if display_index in av_map:
            return av_map[display_index]
        log.warn("screen", f"avfoundation listing attempt {attempt}: no screen "
                 f"device matched for display {display_index}")
    raise RuntimeError(
        f"Could not map display {display_index} to an avfoundation screen "
        "device - ffmpeg device listing failed or reported no 'Capture screen' "
        "entries. Refusing to guess a device index (low indices are webcams)."
    )


# ── Screen Recording (TCC) permission preflight ──────────────────────────────

def _preflight_screen_capture_access() -> None:
    """Raise early if the Screen Recording (TCC) permission is not granted.

    Without the permission, ffmpeg's avfoundation capture either exits
    immediately or silently records wallpaper-only frames with no window
    content. CGRequestScreenCaptureAccess() registers the app in the TCC
    list / triggers the system prompt, but the grant only takes effect on
    restart, so we still fail loudly.
    """
    try:
        from Quartz import (  # type: ignore[import-not-found]
            CGPreflightScreenCaptureAccess,
            CGRequestScreenCaptureAccess,
        )
    except ImportError:
        log.warn("screen", "Quartz unavailable - skipping Screen Recording permission preflight")
        return

    if CGPreflightScreenCaptureAccess():
        return

    try:
        CGRequestScreenCaptureAccess()
    except Exception:
        pass
    raise RuntimeError(
        "Screen Recording permission is not granted. Open System Settings → "
        "Privacy & Security → Screen Recording, enable it for the app/terminal "
        "that launched Meeting Assistant, then restart."
    )


# ── flash_display_border (no-op on Mac for v1) ──────────────────────────────

def flash_display_border(display_index: int, duration_ms: int = 1500, thickness: int = 6):
    """Flash a colored border around a display.

    macOS implementation deferred — would require an NSWindow overlay with
    transparent borderless styleMask, which is non-trivial to set up from
    a subprocess. The display picker UI works without this hint on Mac.
    """
    log.info("screen", "flash_display_border: not implemented on macOS (no-op)")


# ── Screen recorder ─────────────────────────────────────────────────────────

class ScreenRecorder:
    """Manages an ffmpeg avfoundation subprocess for screen capture."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._output_path: str | None = None
        self._frag_path: str | None = None
        self._lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        # Ring buffer of recent ffmpeg stderr lines, surfaced in error paths.
        self._stderr_tail: deque[str] = deque(maxlen=50)

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def output_path(self) -> str | None:
        return self._output_path

    @property
    def live_video_path(self) -> str | None:
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
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            # No auto-download hint: ffmpeg_util refuses to auto-download on
            # Apple Silicon (would fetch an x86_64 binary), so brew is the path.
            raise RuntimeError(
                "ffmpeg not found - run: brew install ffmpeg, then restart "
                "Meeting Assistant"
            )

        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise RuntimeError("Already recording")

        # Fail fast (and loudly) if the Screen Recording TCC right is missing;
        # otherwise ffmpeg dies instantly or records wallpaper-only frames.
        _preflight_screen_capture_access()

        displays = enumerate_displays()
        if not displays:
            raise RuntimeError("No displays detected")
        if display_index < 0 or display_index >= len(displays):
            display_index = 0
        disp = displays[display_index]

        # Map our display index to avfoundation's screen device index.
        # Raises rather than guessing - a wrong index selects a webcam.
        av_idx = _resolve_av_screen_index(display_index)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        self._output_path = output_path

        log.info("screen", f"Display {display_index}: {disp['width']}x{disp['height']} "
                 f"(scale={disp['scale']:.2f}, av_idx={av_idx})")

        # avfoundation: "<video_idx>:<audio_idx>" — use 'none' for audio so we
        # only capture video; audio goes through audio_capture.py separately.
        cmd = [
            ffmpeg,
            "-y",
            "-f", "avfoundation",
            "-framerate", str(framerate),
            "-capture_cursor", "1",
            "-i", f"{av_idx}:none",
        ]

        vf_parts = []
        if scale:
            vf_parts.append(f"scale={scale}")
        if vf_parts:
            cmd.extend(["-vf", ",".join(vf_parts)])

        self._frag_path = output_path + ".frag.mp4"

        # Force a keyframe at least every ~2 seconds. libx264's default GOP is
        # 250 *frames*, which at this low capture fps puts keyframes tens of
        # seconds apart and makes the playback video slow and coarse to seek
        # (the browser has to decode from a far-away keyframe). -force_key_frames
        # is time-based, so it holds even when frames are delivered unevenly.
        # This underpins the cross-platform video soft-sync playback; keep it.
        keyint = max(1, round(framerate * 2))

        cmd.extend([
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-g", str(keyint),
            "-keyint_min", str(max(1, round(framerate))),
            "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-an",
            "-movflags", "frag_keyframe+empty_moov",
            self._frag_path,
        ])

        log.info("screen", f"Starting: {' '.join(cmd)}")

        self._stderr_tail = deque(maxlen=50)
        with self._lock:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        proc = self._proc

        self._monitor_thread = threading.Thread(
            target=self._monitor, args=(proc, self._stderr_tail), daemon=True
        )
        self._monitor_thread.start()

        # ── Startup health check ─────────────────────────────────────────
        # A TCC denial or device error makes ffmpeg exit immediately (or run
        # without ever producing frames). Verify the process stays alive and
        # the fragmented output starts growing before declaring success, so
        # app.py's except branch surfaces the failure instead of reporting a
        # phantom recording.
        frag = Path(self._frag_path)
        deadline = time.monotonic() + 6.0
        wrote_data = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                if frag.stat().st_size > 0:
                    wrote_data = True
                    break
            except OSError:
                pass
            time.sleep(0.25)

        died = proc.poll() is not None
        if died or not wrote_data:
            if not died:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            if self._monitor_thread:
                self._monitor_thread.join(timeout=2)
            tail = self._stderr_tail_text()
            with self._lock:
                self._proc = None
            frag.unlink(missing_ok=True)
            self._frag_path = None
            self._output_path = None
            reason = (
                f"ffmpeg exited with code {proc.returncode} during startup"
                if died else
                "ffmpeg produced no video data within 6s of starting"
            )
            raise RuntimeError(f"Screen recording failed to start: {reason}. "
                               f"ffmpeg output: {tail}")

        log.info("screen", f"Recording display {display_index} → {output_path}")

    def _monitor(self, proc: subprocess.Popen, tail: deque):
        """Drain ffmpeg stderr into a bounded ring buffer for error reporting."""
        if not proc.stderr:
            return
        buf = b""
        try:
            while True:
                # read1: return as soon as bytes arrive (read() would block
                # until a full 4096 accumulate, starving the health check).
                chunk = proc.stderr.read1(4096)
                if not chunk:
                    break
                buf += chunk
                # ffmpeg separates progress updates with \r, messages with \n.
                *lines, buf = re.split(rb"[\r\n]", buf)
                for raw in lines:
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        tail.append(line)
        except Exception:
            pass
        if buf.strip():
            tail.append(buf.decode("utf-8", "replace").strip())
        # stop() clears self._proc before quitting ffmpeg, so if we're still
        # the active recorder here the process died unexpectedly mid-session.
        if self._proc is proc and proc.poll() is not None:
            log.warn("screen", f"ffmpeg exited unexpectedly "
                     f"(code {proc.returncode}): {self._stderr_tail_text()}")

    def _stderr_tail_text(self, limit: int = 8) -> str:
        """Last few captured ffmpeg stderr lines, for error messages."""
        lines = list(self._stderr_tail)[-limit:]
        return " | ".join(lines) if lines else "(no ffmpeg output captured)"

    def stop(self) -> str | None:
        with self._lock:
            proc = self._proc
            self._proc = None

        if not proc:
            return None

        try:
            proc.stdin.write(b"q")
            proc.stdin.flush()
        except (OSError, BrokenPipeError):
            pass

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.warn("screen", "ffmpeg did not exit in time - killing")
            proc.kill()
            proc.wait(timeout=5)

        final_path = self._output_path
        frag_path = getattr(self, "_frag_path", None)

        if not frag_path or not Path(frag_path).exists() or Path(frag_path).stat().st_size == 0:
            log.warn("screen", f"Recording file is missing or empty. "
                     f"ffmpeg output: {self._stderr_tail_text()}")
            return None

        ffmpeg = find_ffmpeg()
        if ffmpeg and final_path:
            try:
                remux = subprocess.run(
                    [ffmpeg, "-y", "-i", frag_path,
                     "-c", "copy", "-movflags", "+faststart", final_path],
                    capture_output=True, timeout=60,
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

        try:
            Path(frag_path).rename(final_path)
        except OSError:
            final_path = frag_path
        size_mb = Path(final_path).stat().st_size / (1024 * 1024)
        log.info("screen", f"Saved: {final_path} ({size_mb:.1f} MB)")
        return final_path


def capture_live_frame(display_index: int = 0, max_width: int = 960) -> bytes | None:
    """Capture a single JPEG screenshot from the specified display.

    Uses the native /usr/sbin/screencapture tool, NOT ffmpeg/avfoundation:
    a second avfoundation capture of a display that is already being
    recorded blocks indefinitely waiting for the device, which made the
    live preview panel 500 during every recording — exactly when the user
    wants to see it. screencapture snapshots happily alongside an active
    capture and inherits the app's Screen Recording TCC grant.
    """
    displays = enumerate_displays()
    if not displays:
        return None
    if display_index < 0 or display_index >= len(displays):
        display_index = 0

    frame = _capture_frame_screencapture(display_index, max_width)
    if frame is not None:
        return frame

    # Fallback: ffmpeg/avfoundation single-frame grab. Only safe when no
    # recording holds the display; the 5 s timeout bounds the hang.
    return _capture_frame_avfoundation(display_index, max_width)


def _capture_frame_screencapture(display_index: int, max_width: int) -> bytes | None:
    """Native macOS screenshot: works during an active screen recording."""
    import tempfile

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tmp_path = tf.name
        # -x: no sound, -C: include cursor, -D: 1-based display number
        r = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-C",
             "-D", str(display_index + 1), "-t", "jpg", tmp_path],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        # Downscale in place for the preview panel (no-op if already smaller).
        subprocess.run(
            ["/usr/bin/sips", "--resampleWidth", str(max_width), tmp_path],
            capture_output=True, timeout=5,
        )
        data = Path(tmp_path).read_bytes()
        return data or None
    except Exception:
        return None
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass


def _capture_frame_avfoundation(display_index: int, max_width: int) -> bytes | None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None

    # Same strict mapping as start() - never guess a device index (webcams
    # occupy the low indices). This helper returns None on any failure.
    try:
        av_idx = _resolve_av_screen_index(display_index)
    except RuntimeError:
        return None

    cmd = [
        ffmpeg,
        "-f", "avfoundation",
        "-framerate", "1",
        "-capture_cursor", "1",
        "-i", f"{av_idx}:none",
        "-frames:v", "1",
        "-vf", f"scale='min({max_width},iw)':-2",
        "-q:v", "5",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=5)
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except Exception:
        pass
    return None
