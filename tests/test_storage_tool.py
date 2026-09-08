"""The Storage card's data and the Free up space tool.

core/media.py (where a meeting's audio is, in either format), core/disk_usage.py
(the scan the card draws), core/media_compress.py (pricing and running a
re-encode) and core/storage_api.py (its routes). Everything runs in a temporary
data folder. The tests that need ffmpeg skip when the bundled binary is absent
(CI), so the encode path is exercised on a developer machine and the planning
and resolver paths everywhere.
"""
from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import time
import uuid
import wave
from pathlib import Path

import pytest
from flask import Flask

from core import dashboard_api, disk_usage, media, media_compress, paths, storage, storage_api

ROOT = Path(__file__).parents[1]
FFMPEG = media.ffmpeg_bin()
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not available")


# ── Fixtures ────────────────────────────────────────────────────────────────

def _wav(path: Path, seconds: float = 1.0, rate: int = 48_000) -> Path:
    """A real mono 16-bit WAV with a quiet tone, so Opus has something to do."""
    path.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        frames += struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(frames))
    return path


def _blob(path: Path, size: int = 5000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _age(path: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(media_compress, "_job", None)
    storage.init_db()
    yield tmp_path


def _library(data: Path) -> dict:
    """Two meetings with media, one with none, plus the debris a library
    accumulates: an orphan, a fragment, a trim backup and a split backup."""
    s1 = storage.create_session("Design review", started_at="2026-09-01T14:00:00")
    s2 = storage.create_session("Standup", started_at="2026-08-01T09:00:00")
    s3 = storage.create_session("Empty", started_at="2026-07-01T09:00:00")
    _wav(data / "audio" / f"{s1}.wav", 1.0)
    _wav(data / "audio" / f"{s1}_mic.wav", 1.0)
    _blob(data / "audio" / f"{s1}_desktop.opus", 800)
    _blob(data / "video" / f"{s1}.mp4", 9000)
    _age(_blob(data / "video" / f"{s1}.mp4.frag.mp4", 700), 48)
    _blob(data / "video" / f"{s1}.mp4.part", 300)            # fresh: left alone
    _blob(data / "screenshots" / s1 / "100.0s.jpg", 300)
    _wav(data / "backups" / s1 / "audio-original.wav", 1.0)
    _blob(data / "backups" / s1 / "session-original.json", 50)
    _blob(data / "audio" / f"{s2}.opus", 1200)
    _wav(data / "backups" / "split-abc" / "audio-original.wav", 1.0)
    orphan = _wav(data / "audio" / f"{uuid.uuid4()}.wav", 1.0)
    _age(orphan, 48)
    young = _wav(data / "audio" / f"{uuid.uuid4()}.wav", 1.0)
    return {"s1": s1, "s2": s2, "s3": s3, "orphan": orphan, "young": young}


def _meta() -> dict:
    return {row["id"]: {"title": row["title"], "started_at": row["started_at"],
                        "folder_id": None, "seconds": 60.0}
            for row in [storage.get_session_times(s["id"]) for s in storage.list_sessions()]}


# ── core/disk_usage.py ──────────────────────────────────────────────────────

def test_classify_audio_tells_mixed_tracks_and_debris_apart():
    sid = str(uuid.uuid4())
    assert disk_usage.classify_audio(f"{sid}.wav") == ("mixed", "wav")
    assert disk_usage.classify_audio(f"{sid}.opus") == ("mixed", "opus")
    assert disk_usage.classify_audio(f"{sid}_mic.wav") == ("track", "wav")
    assert disk_usage.classify_audio(f"{sid}_desktop.opus") == ("track", "opus")
    assert disk_usage.classify_audio(f"{sid}.opus.part") == ("leftover", None)
    assert disk_usage.classify_audio("notes.txt") == ("leftover", None)


def test_scan_attributes_every_file_to_a_kind_and_a_meeting(data):
    ids = _library(data)
    report = disk_usage.scan(_meta())
    kinds = report["by_kind"]
    assert kinds["audio"]["files"] == 6           # s1 wav, mic, desktop; s2 opus; two orphans
    assert kinds["video"]["files"] == 3
    assert kinds["frames"]["files"] == 1
    assert kinds["backups"]["files"] == 3
    assert kinds["database"]["files"] >= 1 and kinds["database"]["bytes"] > 0
    assert report["totals"]["bytes"] == sum(k["bytes"] for k in kinds.values())
    assert report["audio_formats"]["wav"]["files"] == 1   # the mixed tracks only
    assert report["audio_formats"]["opus"]["files"] == 1
    assert report["tracks"]["wav"]["files"] == 1 and report["tracks"]["opus"]["files"] == 1

    by_id = {s["id"]: s for s in report["sessions"]}
    s1 = by_id[ids["s1"]]
    assert s1["audio_format"] == "wav" and s1["audio_bytes"] > 44
    assert s1["audio_tracks_wav_bytes"] > 0 and s1["audio_tracks_bytes"] > s1["audio_tracks_wav_bytes"]
    assert s1["video_files"] == 1 and s1["video_bytes"] == 9000
    assert s1["frames_files"] == 1 and s1["backups_bytes"] > 0
    assert s1["leftover_bytes"] == 1000
    lefts = {left["path"]: left for left in s1["leftovers"]}
    assert lefts[f"video/{ids['s1']}.mp4.frag.mp4"]["age_hours"] >= 47
    assert lefts[f"video/{ids['s1']}.mp4.part"]["age_hours"] < 1
    assert by_id[ids["s2"]]["audio_format"] == "opus"
    assert by_id[ids["s3"]]["bytes"] == 0, "a meeting with no media still appears"
    # Newest first.
    assert [s["id"] for s in report["sessions"]] == [ids["s1"], ids["s2"], ids["s3"]]

    orphans = report["orphans"]
    assert orphans["files"] == 2 and orphans["bytes"] > 0
    ages = {Path(o["path"]).name: o["age_hours"] for o in orphans["items"]}
    assert ages[ids["orphan"].name] >= 47 and ages[ids["young"].name] < 1
    assert all(o["kind"] == "audio" for o in orphans["items"])

    backups = {b["dir"]: b for b in report["backups"]}
    trim = backups[f"backups/{ids['s1']}"]
    assert trim["kind"] == "trim" and trim["session_id"] == ids["s1"] and trim["files"] == 2
    assert trim["audio_wav_bytes"] > 44
    split = backups["backups/split-abc"]
    assert split["kind"] == "split" and split["session_id"] is None and not split["orphaned"]
    assert report["leftovers"] == {"bytes": 1000, "files": 2}
    assert report["disk"] is None or report["disk"]["free"] > 0


def test_the_ledger_only_counts_while_the_file_still_matches_it(data):
    ids = _library(data)
    row = {"codec": "opus", "preset": "voice_32", "before_bytes": 10, "after_bytes": 1,
           "duration_sec": 1.0, "encoded_at": "2026-09-08T00:00:00Z"}
    report = disk_usage.scan(_meta(), encodes={(ids["s1"], "audio"): row, (ids["s2"], "audio"): row})
    by_id = {s["id"]: s for s in report["sessions"]}
    assert "audio" not in by_id[ids["s1"]]["encodes"], "s1 is a WAV again: not compressed now"
    assert by_id[ids["s2"]]["encodes"]["audio"]["preset"] == "voice_32"


# ── core/media.py ───────────────────────────────────────────────────────────

def test_the_resolver_finds_either_format_and_prefers_a_fresh_wav(data):
    sid = storage.create_session("Both")
    wav = _wav(data / "audio" / f"{sid}.wav")
    opus = _blob(data / "audio" / f"{sid}.opus", 500)
    assert media.audio_path(sid) == wav, "a WAV beside an Opus is a trim not yet re-encoded"
    wav.unlink()
    assert media.audio_path(sid) == opus
    assert media.audio_mime(opus) == "audio/ogg" and media.audio_mime(wav) == "audio/wav"
    assert media.audio_format(opus) == "opus" and media.audio_format(None) is None
    assert media.has_audio(sid) and not media.has_audio(str(uuid.uuid4()))
    assert media.session_id_of(opus) == sid and media.session_id_of("readme.txt") is None
    assert media.tracks_root(sid).endswith(sid)


def test_pcm_for_a_wav_session_is_the_wav_itself(data):
    sid = storage.create_session("Plain")
    wav = _wav(data / "audio" / f"{sid}.wav")
    assert media.pcm_wav_path(sid) == wav
    assert media.audio_duration(wav) == pytest.approx(1.0, abs=0.01)
    info = media.audio_info(wav)
    assert info["format"] == "wav" and info["sample_rate"] == 48_000 and info["size_bytes"] > 44


def test_replace_audio_retires_the_other_format(data):
    sid = storage.create_session("Swap")
    _wav(data / "audio" / f"{sid}.wav")
    staged = _blob(data / "tmp" / f"{sid}.opus", 600)
    final = media.replace_audio(sid, staged)
    assert final == data / "audio" / f"{sid}.opus" and final.exists()
    assert not (data / "audio" / f"{sid}.wav").exists() and not staged.exists()
    staged = _wav(data / "tmp" / f"{sid}.trim.wav")
    final = media.replace_audio(sid, staged)
    assert final.suffix == ".wav" and not (data / "audio" / f"{sid}.opus").exists()
    with pytest.raises(ValueError):
        media.replace_audio(sid, _blob(data / "tmp" / "x.mp3"))


def test_delete_session_media_takes_every_format_track_and_fragment(data):
    ids = _library(data)
    sid = ids["s1"]
    (data / "tmp" / "pcm").mkdir(parents=True)
    _blob(data / "tmp" / "pcm" / f"{sid}.wav", 10)
    removed = media.delete_session_media(sid)
    names = sorted(p.name for p in removed)
    assert names == sorted([f"{sid}.wav", f"{sid}_desktop.opus", f"{sid}_mic.wav",
                            f"{sid}.mp4", f"{sid}.mp4.frag.mp4", f"{sid}.mp4.part"])
    assert not (data / "tmp" / "pcm" / f"{sid}.wav").exists()
    # The storage layer routes deletes through it.
    storage.delete_session(ids["s2"])
    assert not (data / "audio" / f"{ids['s2']}.opus").exists()


def test_backup_copies_keep_their_format(data):
    sid = storage.create_session("Backup me")
    _blob(data / "audio" / f"{sid}.opus", 900)
    dst = media.copy_audio_as(sid, data / "backups" / sid)
    assert dst.name == "audio-original.opus"
    assert media.find_backup_audio(data / "backups" / sid) == dst
    assert media.copy_audio_as(str(uuid.uuid4()), data / "backups" / "none") is None


# ── core/media_compress.py: planning ────────────────────────────────────────

def test_options_have_safe_defaults():
    opts = media_compress.normalize_options({})
    assert opts["audio"] == {"enabled": True, "preset": "voice_32", "tracks": True}
    assert opts["video"]["enabled"] is False and opts["video"]["preset"] == "av1_balanced"
    assert opts["backups"]["enabled"] is True
    assert opts["orphans"] == {"enabled": False, "min_age_hours": 6.0}
    # Unknown presets fall back rather than failing the run.
    assert media_compress.normalize_options({"audio": {"preset": "mp3"}})["audio"]["preset"] == "voice_32"


def test_capabilities_without_ffmpeg_offer_nothing(monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_bin", lambda: None)
    caps = media_compress.capabilities()
    assert caps["ffmpeg"] is False and caps["audio"] is False
    assert not any(caps["video"].values()) and not any(caps["hardware"].values())


def test_plan_prices_every_kind_and_only_what_was_asked(data):
    ids = _library(data)
    report = disk_usage.scan(_meta())
    plan = media_compress.plan(report, {"mode": "all"}, {})
    kinds = {i["kind"] for i in plan["items"]}
    assert kinds == {"audio", "backup"}, "audio and backups on by default, video and orphans off"
    audio = next(i for i in plan["items"] if i["kind"] == "audio")
    assert audio["session_id"] == ids["s1"] and audio["status"] == "ready"
    assert 0 < audio["estimate"] < audio["before"]
    assert plan["skipped"]["already"] == 1, "s2 is Opus already"
    dirs = {i["dir"] for i in plan["items"] if i["kind"] == "backup"}
    assert dirs == {f"backups/{ids['s1']}", "backups/split-abc"}
    assert plan["totals"]["saved"] == plan["totals"]["before"] - plan["totals"]["after"] > 0
    assert set(plan["by_kind"]) == {"audio", "backup"}

    everything = media_compress.plan(report, {"mode": "all"}, {
        "video": {"enabled": True, "preset": "av1_balanced"},
        "orphans": {"enabled": True, "min_age_hours": 6},
    })
    video = next(i for i in everything["items"] if i["kind"] == "video")
    assert video["before"] == 9000 and video["estimate"] == int(9000 * 0.55)
    orphans = [i for i in everything["items"] if i["kind"] == "orphan"]
    paths_ = {i["path"] for i in orphans}
    assert f"audio/{ids['orphan'].name}" in paths_, "the old orphan is in"
    assert f"audio/{ids['young'].name}" not in paths_
    assert f"video/{ids['s1']}.mp4.frag.mp4" in paths_, "an old fragment beside a meeting is debris too"
    assert f"video/{ids['s1']}.mp4.part" not in paths_, "a fresh one may still be being written"
    assert everything["skipped"]["young"] == 2
    assert all(i["estimate"] == 0 for i in orphans)

    down = media_compress.plan(report, {"mode": "all"},
                               {"video": {"enabled": True, "downscale": True}})
    assert next(i for i in down["items"] if i["kind"] == "video")["estimate"] == int(9000 * 0.55 * 0.55)


def test_plan_scopes_by_meeting_age_range_and_folder(data):
    ids = _library(data)
    folder = storage.create_folder("Reviews")
    storage.set_session_folder(ids["s1"], folder)
    meta = _meta()
    meta[ids["s1"]]["folder_id"] = folder
    report = disk_usage.scan(meta)

    def audio_ids(scope):
        p = media_compress.plan(report, scope, {"backups": {"enabled": False}})
        return {i["session_id"] for i in p["items"] if i["kind"] == "audio"}

    assert audio_ids({"mode": "sessions", "session_ids": [ids["s1"]]}) == {ids["s1"]}
    assert audio_ids({"mode": "sessions", "session_ids": [ids["s2"]]}) == set()
    assert audio_ids({"mode": "folders", "folder_ids": [folder]}) == {ids["s1"]}
    assert audio_ids({"mode": "folders", "folder_ids": ["nope"]}) == set()
    assert audio_ids({"mode": "range", "start": "2026-09-01", "end": "2026-09-30"}) == {ids["s1"]}
    assert audio_ids({"mode": "range", "start": "2026-01-01", "end": "2026-08-31"}) == set()
    assert audio_ids({"mode": "older", "days": 1}) == {ids["s1"]}
    assert audio_ids({"mode": "older", "days": 36500}) == set()
    # A trim backup follows its meeting; a split backup only comes with "all".
    scoped = media_compress.plan(report, {"mode": "sessions", "session_ids": [ids["s1"]]}, {})
    assert {i["dir"] for i in scoped["items"] if i["kind"] == "backup"} == {f"backups/{ids['s1']}"}


def test_a_meeting_in_use_is_priced_but_never_queued(data):
    ids = _library(data)
    report = disk_usage.scan(_meta())
    plan = media_compress.plan(report, {"mode": "all"}, {"video": {"enabled": True}}, busy={ids["s1"]})
    statuses = {(i["kind"], i["status"]) for i in plan["items"] if i.get("session_id") == ids["s1"]}
    assert statuses == {("audio", "busy"), ("video", "busy")}
    assert plan["skipped"]["busy"] >= 3          # audio, video and its trim backup
    assert plan["totals"]["files"] == 1, "only the split backup is left to do"


# ── core/media_compress.py: running (needs ffmpeg) ──────────────────────────

def _wait(job, timeout=180.0):
    deadline = time.time() + timeout
    while job.state == "running" and time.time() < deadline:
        time.sleep(0.1)
    assert job.state != "running", "the job did not finish in time"


@needs_ffmpeg
def test_pcm_for_an_opus_session_is_decoded_once_and_reused(data, monkeypatch):
    sid = storage.create_session("Opus")
    wav = _wav(data / "tmp" / "src.wav", 1.0)
    opus = data / "audio" / f"{sid}.opus"
    opus.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "libopus",
                    "-b:a", "32k", "-f", "ogg", str(opus)], check=True,
                   creationflags=media.no_window_flag())
    pcm = media.pcm_wav_path(sid)
    assert pcm == data / "tmp" / "pcm" / f"{sid}.wav" and pcm.exists()
    header = media.wav_header(pcm)
    assert header["sample_rate"] == media.PCM_RATE and header["channels"] == 1
    assert header["duration_sec"] == pytest.approx(1.0, abs=0.1)
    assert media.audio_duration(opus) == pytest.approx(1.0, abs=0.1)
    # The second call must not decode again: make ffmpeg unavailable and ask.
    monkeypatch.setattr(media, "ffmpeg_bin", lambda: None)
    assert media.pcm_wav_path(sid) == pcm
    media.release_pcm(sid)
    assert not pcm.exists()
    assert media.pcm_wav_path(sid) is None, "no ffmpeg and no cache: honest None"


@needs_ffmpeg
def test_the_job_compresses_audio_tracks_and_backups_and_removes_debris(data):
    ids = _library(data)
    events: list[dict] = []
    media_compress.configure(push=lambda name, payload: events.append((name, payload)),
                             busy=lambda: set())
    report = disk_usage.scan(_meta())
    plan = media_compress.plan(report, {"mode": "all"},
                               {"orphans": {"enabled": True, "min_age_hours": 6}})
    job = media_compress.start(plan["items"], plan["options"])
    _wait(job)
    snap = job.snapshot()
    failed = [i for i in snap["items"] if i["status"] == "failed"]
    assert not failed, failed
    assert job.state == "done" and snap["done"] == snap["total"]
    s1 = ids["s1"]
    assert (data / "audio" / f"{s1}.opus").exists() and not (data / "audio" / f"{s1}.wav").exists()
    assert (data / "audio" / f"{s1}_mic.opus").exists() and not (data / "audio" / f"{s1}_mic.wav").exists()
    assert (data / "backups" / s1 / "audio-original.opus").exists()
    assert not (data / "backups" / s1 / "audio-original.wav").exists()
    assert (data / "backups" / "split-abc" / "audio-original.opus").exists()
    assert not ids["orphan"].exists() and ids["young"].exists(), "the young orphan was never planned"
    assert not (data / "video" / f"{s1}.mp4.frag.mp4").exists()
    assert (data / "video" / f"{s1}.mp4.part").exists(), "too fresh to be sure: left alone"
    assert (data / "video" / f"{s1}.mp4").exists(), "video was not asked for"
    assert snap["saved"] > 0 and snap["before"] > snap["after"]
    assert media.audio_duration(data / "audio" / f"{s1}.opus") == pytest.approx(1.0, abs=0.1)
    ledger = storage.media_encodes()
    assert ledger[(s1, "audio")]["codec"] == "opus" and ledger[(s1, "audio")]["preset"] == "voice_32"
    assert any(name == "storage_job" and p["state"] == "done" for name, p in events)
    # The scan now sees a compressed meeting, and a second plan has nothing to do.
    again = disk_usage.scan(_meta(), encodes=ledger)
    rec = next(s for s in again["sessions"] if s["id"] == s1)
    assert rec["audio_format"] == "opus" and rec["encodes"]["audio"]["codec"] == "opus"
    assert rec["audio_tracks_wav_bytes"] == 0 and rec["leftover_bytes"] == 300
    replan = media_compress.plan(again, {"mode": "all"}, {})
    assert not replan["items"] and replan["skipped"]["already"] == 2
    # The restore path reads the Opus backup back into place.
    from capture_video import media_edit
    media_edit.restore_original_media(s1)
    assert media.audio_path(s1) == data / "audio" / f"{s1}.opus"


def _synthetic_mp4(path: Path, quality_args: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=10", "-t", "2",
                    "-c:v", "libx264", *quality_args, "-pix_fmt", "yuv420p", str(path)],
                   check=True, creationflags=media.no_window_flag())
    return path


@needs_ffmpeg
def test_the_job_reencodes_video_and_keeps_an_original_that_was_already_smaller(data):
    media_compress.configure(push=None, busy=lambda: set())
    big = storage.create_session("Lossless", started_at="2026-09-01T10:00:00")
    small = storage.create_session("Tiny", started_at="2026-09-01T11:00:00")
    _synthetic_mp4(data / "video" / f"{big}.mp4", ["-qp", "0"])
    _synthetic_mp4(data / "video" / f"{small}.mp4", ["-crf", "51"])
    before_big = (data / "video" / f"{big}.mp4").stat().st_size
    before_small = (data / "video" / f"{small}.mp4").stat().st_size
    report = disk_usage.scan(_meta())
    options = {"audio": {"enabled": False}, "backups": {"enabled": False},
               "video": {"enabled": True, "preset": "h264", "hardware": False}}
    plan = media_compress.plan(report, {"mode": "all"}, options)
    assert {i["session_id"] for i in plan["items"]} == {big, small}
    job = media_compress.start(plan["items"], plan["options"])
    _wait(job)
    items = {i["session_id"]: i for i in job.snapshot()["items"]}
    assert items[big]["status"] == "done" and items[big]["after"] < before_big
    assert (data / "video" / f"{big}.mp4").stat().st_size == items[big]["after"]
    assert items[small]["status"] == "skipped" and "not smaller" in items[small]["error"]
    assert (data / "video" / f"{small}.mp4").stat().st_size == before_small
    ledger = storage.media_encodes()
    assert ledger[(big, "video")]["codec"] == "libx264" and ledger[(big, "video")]["preset"] == "h264"
    assert (small, "video") not in ledger
    # Same preset again: nothing to do for the one that was done.
    replan = media_compress.plan(disk_usage.scan(_meta(), encodes=ledger), {"mode": "all"}, options)
    assert {i["session_id"] for i in replan["items"]} == {small}
    assert replan["skipped"]["already"] == 1


# ── core/storage_api.py and the dashboard route ─────────────────────────────

@pytest.fixture()
def client(data):
    app = Flask(__name__)
    app.register_blueprint(dashboard_api.bp)
    app.register_blueprint(storage_api.bp)
    return app.test_client()


def test_the_storage_routes_report_plan_and_job(client, data):
    ids = _library(data)
    report = client.get("/api/dashboard/storage").get_json()
    for key in ("disk", "totals", "by_kind", "audio_formats", "tracks", "backups",
                "leftovers", "sessions", "orphans", "folders", "generated_at"):
        assert key in report, key
    assert {s["id"] for s in report["sessions"]} == {ids["s1"], ids["s2"], ids["s3"]}
    assert report["sessions"][0]["title"] == "Design review"

    plan = client.post("/api/storage/plan", json={
        "scope": {"mode": "all"}, "audio": {"enabled": True}, "video": {"enabled": False},
    }).get_json()
    assert plan["totals"]["files"] == 3 and plan["running"] is False
    assert "capabilities" in plan and plan["options"]["video"]["enabled"] is False

    job = client.get("/api/storage/compress").get_json()
    assert job["job"] is None and "capabilities" in job
    cancelled = client.post("/api/storage/compress/cancel").get_json()
    assert cancelled["cancelled"] is False


# ── Readers never build the WAV path themselves any more ────────────────────

def test_only_writers_still_build_a_wav_path():
    """core/media.py is the one place that knows a meeting's audio may be Opus.
    The recorder, the upload importer and the bundle importer write WAVs, so
    they may name one; nothing that reads may."""
    builders = re.compile(r'f"\{[a-z_\[\]\'"]+\}\.wav"')
    app = (ROOT / "app.py").read_text(encoding="utf-8").splitlines()
    hits = [line.strip() for line in app if builders.search(line)]
    assert hits == [
        'wav_path = str(wav_dir / f"{session_id}.wav")',
        'wav_path = audio_dir / f"{session_id}.wav"',
        'wav_path = audio_dir / f"{new_session_id}.wav"',
    ], hits
    for name in ("core/storage.py", "agent_api/rest.py", "agent_api/helpers.py"):
        assert not builders.search((ROOT / name).read_text(encoding="utf-8")), name
    edit = (ROOT / "capture_video/media_edit.py").read_text(encoding="utf-8")
    assert edit.count('audio_dir() / f"{session_id}.wav"') == 1, "only wav_path() builds it"
    assert edit.index("media.pcm_wav_path(session_id)") < edit.index("_read_wav_mono_float(path)")
    for reader in ("session_audio(", "def meeting_audio(", "def meeting_audio_clip("):
        assert reader in "".join(app) + (ROOT / "agent_api/rest.py").read_text(encoding="utf-8")
