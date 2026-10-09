"""Frames from a meeting's screen recording, decoded in this process.

The one place that answers "what was on screen at meeting second t" for a
meeting. It knows which file holds that moment (the finished MP4, the
fragmented file still being recorded, or a part kept from before a resume)
and where in that file, through the session's video offsets in
``core.settings``. Meeting seconds are the transcript's timeline (the WAV
writer's clock); video time is meeting time minus where the file starts.

Frames are decoded with PyAV, cropped and resized, and JPEG-encoded in
memory. Measured 2026-10-08 on a 2560x1440, 10 fps recording: about 25 ms a
frame on one core (seek and decode 16 ms, swscale resize and JPEG 9 ms) and 60
frames in 1.4 s on 4 threads, against 86 ms a frame for an ffmpeg process per
frame. ``extract_frame`` (an ffmpeg process per frame) stays the fallback when
PyAV is missing (on Windows it comes with faster-whisper) or cannot read the
file.
"""
from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from core import log, paths, settings

try:
    import av  # PyAV
except Exception:  # noqa: BLE001 - optional; the ffmpeg fallback covers it
    av = None

# A moment this close to the live head may not be flushed to the live file
# yet (fragments land every 2 s); a screenshot of the recorded display is then
# the honest answer. Older moments never silently become a current screenshot.
LIVE_HEAD_WINDOW_SEC = 12.0

# Open containers kept per thread for finished files (never for the live
# file, which grows: a container opened earlier does not see new fragments).
_MAX_OPEN_PER_THREAD = 4


@dataclass(frozen=True)
class Source:
    """One video file of a meeting and the meeting second its video time 0
    sits at."""
    path: Path
    start: float
    live: bool = False


def _duration(path: Path) -> float:
    if av is None:
        return 0.0
    try:
        with av.open(str(path)) as c:
            if c.duration:
                return float(c.duration) / av.time_base
            st = c.streams.video[0]
            return float(st.duration * st.time_base) if st.duration else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def sources(session_id: str, live: dict | None = None) -> list[Source]:
    """The files holding this meeting's screen, in time order.

    ``live`` is the app's live-media snapshot ({"session_id",
    "live_video_path", "elapsed_sec"}); the live file only ever answers for the
    session being recorded."""
    vdir = paths.video_dir()
    out: list[Source] = []
    starts = settings.get_video_part_offsets(session_id)
    n = 0
    running = 0.0
    while True:
        part = vdir / f"{session_id}_part{n}.mp4"
        if not part.exists():
            break
        # A part from before part offsets were recorded starts where the
        # previous one ended: the WAV runs on through a resume, so its parts
        # follow each other on the meeting timeline.
        start = starts.get(n, running)
        out.append(Source(part, start))
        running = start + _duration(part)
        n += 1
    offset = settings.get_video_offset(session_id)
    live_path = None
    if live and live.get("session_id") == session_id:
        live_path = live.get("live_video_path")
    if live_path and Path(live_path).exists():
        out.append(Source(Path(live_path), offset, live=True))
    else:
        final = vdir / f"{session_id}.mp4"
        if final.exists():
            out.append(Source(final, offset))
    return out


def available(session_id: str, live: dict | None = None) -> bool:
    return bool(sources(session_id, live))


def locate(session_id: str, t: float, live: dict | None = None) -> tuple[Source, float] | None:
    """(file, video time) for meeting second ``t``, or None without video.
    A moment before the first file starts maps to that file's first frame."""
    srcs = sources(session_id, live)
    if not srcs:
        return None
    chosen = srcs[0]
    for s in srcs:
        if s.start <= t + 1e-6:
            chosen = s
    return chosen, max(0.0, t - chosen.start)


# ── Decoding ────────────────────────────────────────────────────────────────

class _Open(threading.local):
    def __init__(self) -> None:
        self.containers: dict[str, object] = {}


_open = _Open()


def _container(path: Path):
    key = str(path)
    c = _open.containers.get(key)
    if c is None:
        if len(_open.containers) >= _MAX_OPEN_PER_THREAD:
            old_key = next(iter(_open.containers))
            try:
                _open.containers.pop(old_key).close()
            except Exception:  # noqa: BLE001
                pass
        c = av.open(key)
        _open.containers[key] = c
    return c


def _decode(src: Source, t_video: float):
    """The frame shown at ``t_video`` (the last one at or before it), or None."""
    c = av.open(str(src.path)) if src.live else _container(src.path)
    try:
        st = c.streams.video[0]
        c.seek(max(0, int(t_video / float(st.time_base))), stream=st,
               backward=True, any_frame=False)
        last = None
        for frame in c.decode(st):
            if frame.time is None:
                continue
            if frame.time > t_video + 0.05:
                return last or frame
            last = frame
        return last
    finally:
        if src.live:
            c.close()


def _clamp_box(box, w: int, h: int) -> tuple[int, int, int, int] | None:
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0 = max(0, min(w, x0)), max(0, min(h, y0))
    x1, y1 = max(0, min(w, x1)), max(0, min(h, y1))
    return (x0, y0, x1, y1) if x1 - x0 >= 8 and y1 - y0 >= 8 else None


def _image(frame, width: int | None, crop) -> "Image.Image":
    from PIL import Image
    if crop:
        box = _clamp_box(crop, frame.width, frame.height)
        img = frame.to_image()
        if box:
            img = img.crop(box)
        if width and img.width > width:
            img = img.resize((width, max(1, round(img.height * width / img.width))),
                             Image.BILINEAR)
        return img
    if width and frame.width > width:
        h = max(2, round(frame.height * width / frame.width))
        return frame.reformat(width=width, height=h, format="rgb24").to_image()
    return frame.to_image()


def _jpeg(img, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _fallback_image(src: Source, t_video: float, width: int | None, crop):
    """The same frame through an ffmpeg process, for when PyAV cannot help."""
    from PIL import Image
    from capture_video import extract_frame
    jpeg = extract_frame(str(src.path), t_video, max_width=100000 if crop else (width or 100000))
    if not jpeg:
        return None
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    if crop:
        box = _clamp_box(crop, img.width, img.height)
        if box:
            img = img.crop(box)
    if width and img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.BILINEAR)
    return img


def image_at(src: Source, t_video: float, *, width: int | None = None, crop=None):
    """PIL image of the frame at ``t_video`` in ``src``, cropped (video
    pixels, [x0, y0, x1, y1]) and scaled down to ``width``; None if unreadable."""
    if av is not None:
        try:
            frame = _decode(src, t_video)
            if frame is not None:
                return _image(frame, width, crop)
        except Exception as e:  # noqa: BLE001 - fall back to ffmpeg below
            log.warn("screen", f"PyAV could not read {src.path.name} at {t_video:.1f}s: {e}")
    try:
        return _fallback_image(src, t_video, width, crop)
    except Exception as e:  # noqa: BLE001
        log.warn("screen", f"Frame extraction failed for {src.path.name}: {e}")
        return None


def image(session_id: str, t: float, *, width: int | None = 1280, crop=None,
          live: dict | None = None, allow_screenshot: bool = True):
    """PIL image of meeting second ``t``, or None. For the session being
    recorded, a moment at the live head not yet in the file falls back to a
    screenshot of the recorded display (``allow_screenshot``)."""
    located = locate(session_id, t, live)
    if located is None:
        return None
    src, vt = located
    img = image_at(src, vt, width=width, crop=crop)
    if img is None and src.live and allow_screenshot:
        elapsed = (live or {}).get("elapsed_sec")
        if elapsed is None or t >= float(elapsed) - LIVE_HEAD_WINDOW_SEC:
            from PIL import Image
            from capture_video import capture_live_frame
            jpeg = capture_live_frame(display_index=int(settings.get("screen_display", 0)),
                                      max_width=width or 1280)
            if jpeg:
                img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    return img


def grab_at(src: Source, t_video: float, *, width: int | None = 1280, crop=None,
            quality: int = 85) -> bytes | None:
    """JPEG of ``t_video`` in one located file (see ``locate``), or None."""
    img = image_at(src, t_video, width=width, crop=crop)
    return _jpeg(img, quality) if img is not None else None


def grab(session_id: str, t: float, *, width: int | None = 1280, crop=None,
         quality: int = 85, live: dict | None = None,
         allow_screenshot: bool = True) -> bytes | None:
    """JPEG of meeting second ``t`` (see ``image``), or None."""
    img = image(session_id, t, width=width, crop=crop, live=live,
                allow_screenshot=allow_screenshot)
    return _jpeg(img, quality) if img is not None else None


def grab_many(session_id: str, times: list[float], *, width: int | None = 1280,
              crop=None, quality: int = 85, live: dict | None = None,
              workers: int = 4) -> list[bytes | None]:
    """JPEGs for many meeting seconds, decoded on ``workers`` threads, each
    working through its share in time order. Results follow ``times``."""
    if not times:
        return []
    order = sorted(range(len(times)), key=lambda i: times[i])
    workers = max(1, min(workers, len(times)))
    shares = [order[k::workers] for k in range(workers)]
    out: list[bytes | None] = [None] * len(times)

    def run(share: list[int]) -> None:
        for i in share:
            try:
                out[i] = grab(session_id, times[i], width=width, crop=crop,
                              quality=quality, live=live, allow_screenshot=False)
            except Exception as e:  # noqa: BLE001 - one bad frame must not stop the rest
                log.warn("screen", f"Frame at {times[i]:.1f}s failed: {e}")

    if workers == 1:
        run(shares[0])
    else:
        with ThreadPoolExecutor(workers, thread_name_prefix="frames") as ex:
            list(ex.map(run, shares))
    return out
