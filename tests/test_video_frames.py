"""capture_video.frames: the frame shown at a meeting second, from the right file.

Regressions this guards (2026-10-08):
- The meeting chat's screenshot tool read whatever was recording, so a chat
  about an older meeting during a call was shown the call, and it ignored the
  video offset of a resumed meeting.
- The Agent API returned the live file's first frame for a moment from before
  a resume (that moment is in a part kept aside, {sid}_partN.mp4).
- The video offset assumed video second 0 was meeting second 0; the WAV starts
  first (_settle_video_offset), and a resume without screen recording moved
  the offset of the earlier video.

The videos are generated here: one solid colour per second, a keyframe every
2 s like the recorder's.

Run: .venv/Scripts/python -m pytest tests/test_video_frames.py
"""
from __future__ import annotations

import ast
import io
import shutil
import time
from pathlib import Path

import pytest

av = pytest.importorskip("av")
from PIL import Image  # noqa: E402

from capture_video import frames  # noqa: E402
from core import paths, settings  # noqa: E402

ROOT = Path(__file__).parents[1]
COLOURS = [(220, 40, 40), (40, 200, 40), (40, 40, 220), (230, 230, 40),
           (230, 40, 230), (40, 230, 230)]


def _video(path: Path, seconds: int, first_colour: int = 0) -> Path:
    """A 160x90, 10 fps video: second k is COLOURS[(first_colour + k) % 6]."""
    with av.open(str(path), "w") as out:
        st = out.add_stream("libx264", rate=10)
        st.width, st.height, st.pix_fmt = 160, 90, "yuv420p"
        st.codec_context.gop_size = 20
        for n in range(seconds * 10):
            img = Image.new("RGB", (160, 90), COLOURS[(first_colour + n // 10) % len(COLOURS)])
            frame = av.VideoFrame.from_image(img)
            frame.pts = n
            for packet in st.encode(frame):
                out.mux(packet)
        for packet in st.encode():
            out.mux(packet)
    return path


def _colour(jpeg: bytes) -> int:
    """Index of the COLOURS entry closest to the centre pixel."""
    px = Image.open(io.BytesIO(jpeg)).convert("RGB").getpixel((40, 22))
    return min(range(len(COLOURS)),
               key=lambda i: sum((a - b) ** 2 for a, b in zip(px, COLOURS[i])))


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    (tmp_path / "video").mkdir()
    return tmp_path


def test_a_finished_meeting_reads_its_own_video_through_its_offset(data):
    _video(data / "video" / "A.mp4", 6)
    settings.put_video_offset("A", 1.0)          # the video began 1 s into the meeting
    src, vt = frames.locate("A", 3.5)
    assert src.path.name == "A.mp4" and vt == pytest.approx(2.5)
    assert _colour(frames.grab("A", 3.5, width=None)) == 2


def test_another_meeting_recording_now_never_answers(data):
    # The chat bug: a call recording now must not answer for meeting A.
    _video(data / "video" / "A.mp4", 6)
    _video(data / "video" / "B.mp4.frag.mp4", 6, first_colour=3)
    live = {"recording": True, "session_id": "B",
            "live_video_path": str(data / "video" / "B.mp4.frag.mp4"), "elapsed_sec": 6.0}
    assert _colour(frames.grab("A", 1.5, width=None, live=live)) == 1
    assert _colour(frames.grab("B", 1.5, width=None, live=live)) == 4


def test_a_moment_from_before_a_resume_comes_from_the_kept_part(data):
    _video(data / "video" / "R_part0.mp4", 4)                       # meeting 0-4 s
    _video(data / "video" / "R.mp4.frag.mp4", 4, first_colour=4)    # resumed at 4 s
    settings.put_video_part_offset("R", 0, 0.0)
    settings.put_video_offset("R", 4.0)
    live = {"recording": True, "session_id": "R",
            "live_video_path": str(data / "video" / "R.mp4.frag.mp4"), "elapsed_sec": 8.0}
    src, vt = frames.locate("R", 2.5, live)
    assert src.path.name == "R_part0.mp4" and vt == pytest.approx(2.5)
    assert _colour(frames.grab("R", 2.5, width=None, live=live)) == 2
    assert _colour(frames.grab("R", 5.5, width=None, live=live)) == 5   # live file, 1.5 s in


def test_a_part_without_a_recorded_start_follows_the_one_before(data):
    _video(data / "video" / "P_part0.mp4", 3)
    _video(data / "video" / "P_part1.mp4", 3, first_colour=3)
    srcs = frames.sources("P")
    assert [s.path.name for s in srcs] == ["P_part0.mp4", "P_part1.mp4"]
    assert srcs[1].start == pytest.approx(3.0, abs=0.15)


def test_grab_many_keeps_the_order_asked_for(data):
    _video(data / "video" / "M.mp4", 6)
    times = [5.5, 0.5, 3.5, 1.5]
    got = frames.grab_many("M", times, width=None, workers=3)
    assert [_colour(j) for j in got] == [5, 0, 3, 1]


def test_crop_and_width_shape_the_image(data):
    _video(data / "video" / "C.mp4", 2)
    img = frames.image("C", 0.5, width=40, crop=[80, 0, 160, 90])
    assert img.size == (40, 45)


def test_no_video_means_none(data):
    assert frames.locate("nothing", 3.0) is None
    assert frames.grab("nothing", 3.0) is None
    assert not frames.available("nothing")


# ── Where the video starts on the meeting timeline ──────────────────────────

def test_ffmpeg_reports_the_first_frame_wall_clock():
    from capture_video.windows import _INPUT_START_RE
    line = "  Duration: N/A, start: 1791481075.754982, bitrate: 10518532 kb/s"
    assert float(_INPUT_START_RE.search(line).group(1)) == pytest.approx(1791481075.754982)


def _settle(epoch, wav_now: float, wav_at_start: float):
    """Run _settle_video_offset; ``epoch`` is a callable giving the recorder's
    first-frame stamp when asked (None: ffmpeg never said)."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_settle_video_offset")
    stored = {}
    scope = {
        "_screen_recorder": type("R", (), {"wait_first_frame": staticmethod(
            lambda t: epoch() if epoch else None)})(),
        "settings": type("S", (), {"put_video_offset": staticmethod(
            lambda sid, v: stored.__setitem__(sid, v))}),
        "time": time,
        "log": type("L", (), {"info": staticmethod(lambda *a: None)}),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<app._settle_video_offset>", "exec"),
         scope)
    capture = type("C", (), {"wav_writer": type("W", (), {"elapsed_seconds": wav_now})()})()
    scope["_settle_video_offset"]("S1", capture, wav_at_start)
    return stored["S1"]


def test_the_offset_is_where_the_first_frame_fell_in_the_wav():
    # First frame 0.5 s ago; the WAV is at 10.8 s now, so the video starts at 10.3 s.
    assert _settle(lambda: time.time() - 0.5, 10.8, 10.6) == pytest.approx(10.3, abs=0.05)


def test_without_a_wall_clock_stamp_the_recorder_start_stands_in():
    assert _settle(None, 10.8, 10.6) == pytest.approx(10.6)
    assert _settle(lambda: 12.0, 10.8, 10.6) == pytest.approx(10.6), \
        "stream time is not wall clock"


def test_a_resume_without_screen_recording_leaves_the_offset_alone():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    start = src.index("# ── Video offset ─")
    block = src[start:src.index("# ── Screen recording (optional)", start)]
    assert "if not resume_session_id:" in block
    assert "elapsed_seconds" not in block, "the resume offset is measured, not assumed"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg on PATH")
def test_joining_parts_keeps_the_first_part_start(data, monkeypatch):
    _video(data / "video" / "J_part0.mp4", 2)
    _video(data / "video" / "J.mp4", 2, first_colour=2)
    settings.put_video_part_offset("J", 0, 0.4)
    settings.put_video_offset("J", 9.0)

    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_concat_video_parts")
    scope = {"paths": paths, "settings": settings, "Path": Path,
             "find_ffmpeg": lambda: shutil.which("ffmpeg"),
             "log": type("L", (), {"info": staticmethod(lambda *a: None),
                                   "warn": staticmethod(lambda *a: None)})}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<app._concat_video_parts>", "exec"),
         scope)
    scope["_concat_video_parts"]("J")
    assert settings.get_video_offset("J") == pytest.approx(0.4)
    assert settings.get_video_part_offsets("J") == {}
    assert not (data / "video" / "J_part0.mp4").exists()
