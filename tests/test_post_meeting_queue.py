"""The post-meeting queue (Transcribe after the meeting) loses nothing and
redoes nothing.

Runs app.py's own _PostMeetingTranscription against stand-ins (importing app.py
loads the models), the way test_instance_handshake_busy.py runs _busy_reason.

Three gaps closed in review of PR 1086 (2026-10-07):
  - a record-only session was queued only at the end of the stop, so a quit,
    restart, update or crash before then lost its transcription for good;
  - deleting a meeting did not stop a pass running on it, which kept writing
    rows for the deleted id;
  - a queued meeting reanalyzed by hand was transcribed again later, wiping
    the speaker names given in between.
"""
import ast
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
APP_PY = (ROOT / "app.py").read_text(encoding="utf-8")


class _Settings:
    def __init__(self, data=None):
        self.data = data if data is not None else {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def put(self, key, value):
        self.data[key] = list(value) if isinstance(value, list) else value


class _Log:
    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Media:
    @staticmethod
    def audio_path(_sid):
        return Path("x.wav")


class _Storage:
    def __init__(self):
        self.deleted = set()

    def get_session(self, sid):
        return None if sid in self.deleted else {"expected_speaker_count": None}


class _Coordinator:
    @staticmethod
    def pending_command():
        return None


def _queue_class(settings, run_reanalysis, storage=None):
    tree = ast.parse(APP_PY)
    cls = next((n for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "_PostMeetingTranscription"), None)
    assert cls is not None, "app.py no longer defines _PostMeetingTranscription"
    scope = {
        "threading": threading, "time": time, "log": _Log(), "settings": settings,
        "_state": {"is_recording": False, "is_starting": False, "is_reanalyzing": False,
                   "session_id": None},
        "_state_lock": threading.Lock(),
        "_start_coordinator": _Coordinator(),
        "_plan_reanalysis_devices": lambda automatic=False: {
            "wait_for_charger": False, "whisper": "cpu", "diarizer": "cpu"},
        "media": _Media(), "storage": storage or _Storage(),
        "_run_reanalysis": run_reanalysis, "_push_status": lambda *a, **k: None,
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "<app._PostMeetingTranscription>",
                 "exec"), scope)
    return scope["_PostMeetingTranscription"]


def _done(*_a, **_k):
    return True


SID = "11111111-2222-3333-4444-555555555555"


def test_a_record_only_session_is_queued_when_its_recording_starts():
    settings = _Settings()
    q = _queue_class(settings, _done)()
    q.hold(SID)
    assert settings.data["post_meeting_pending"] == [SID], "persisted from the start"
    assert q._next_ready() is None, "held while recording and while the stop finishes"
    q.release(SID)
    assert q._next_ready() == SID


def test_a_quit_or_crash_before_the_stop_finished_leaves_it_queued():
    settings = _Settings()
    _queue_class(settings, _done)().hold(SID)   # the app dies here
    after_restart = _queue_class(settings, _done)()
    after_restart.restore()
    assert after_restart._next_ready() == SID, "the next run transcribes what was recorded"


def test_deleting_a_queued_meeting_takes_it_out_for_good():
    settings = _Settings()
    q = _queue_class(settings, _done)()
    q.enqueue(SID)
    q.forget(SID)
    assert q.pending() == [] and settings.data["post_meeting_pending"] == []
    q.release(SID)      # a stop finishing after the delete must not bring it back
    q.enqueue(SID)
    assert q.pending() == []


def test_deleting_the_meeting_being_transcribed_stops_its_pass():
    settings, storage = _Settings(), _Storage()
    started, calls = threading.Event(), []

    def _long_pass(sid, wav, prompt, num, maxs, cancel_event=None, post_meeting=False,
                   devices=None):
        calls.append(sid)
        started.set()
        cancel_event.wait(10)         # a long pass, until it is cancelled
        return False

    q = _queue_class(settings, _long_pass, storage)()
    followed_up = []
    q._follow_up = followed_up.append
    q.enqueue(SID)
    sid = q._next_ready()
    runner = threading.Thread(target=q._run, args=(sid,), daemon=True)
    runner.start()
    assert started.wait(5)
    t0 = time.monotonic()
    q.forget(SID)                     # what the delete routes call first
    assert time.monotonic() - t0 < 5, "forget waits for the pass to stop, not the timeout"
    runner.join(5)
    assert not runner.is_alive()
    assert q.running_session is None
    assert q.pending() == [], "a deleted meeting is not put back in the queue"
    assert followed_up == [], "no title or summary for a deleted meeting"


def test_a_manual_reanalysis_drops_the_queued_pass():
    settings = _Settings()
    q = _queue_class(settings, _done)()
    q.enqueue(SID)
    q.discard(SID)                    # _run_reanalysis calls this after a manual pass
    assert q.pending() == [] and settings.data["post_meeting_pending"] == []
    q.release(SID)                    # a stop that was still finishing
    assert q.pending() == []


def test_resuming_a_record_only_recording_queues_it_again():
    settings = _Settings()
    q = _queue_class(settings, _done)()
    q.discard(SID)
    q.hold(SID)                       # resumed with new audio to transcribe
    q.release(SID)
    assert q.pending() == [SID]


def test_app_wiring():
    def body(name):
        return re.search(rf"^def {name}\(.*?\n(.*?)(?=^def |\Z)", APP_PY, re.M | re.S).group(1)

    start = body("start_recording")
    deferred = start[start.index("threading.Thread(target=_drain_audio_queue"):]
    assert "_post_meeting.hold(session_id)" in deferred[:600]
    stop = body("stop_recording")
    assert "_post_meeting.release(sid)" in stop[stop.rindex("finally:"):]
    assert "_post_meeting.enqueue(" not in stop
    for route in ("delete_session", "bulk_sessions", "delete_folder"):
        b = body(route)
        assert b.index("_post_meeting.forget(") < b.index("storage.delete_"), route
    rean = body("_run_reanalysis")
    assert "_post_meeting.discard(session_id)" in rean


def _killable_pass(started, killed):
    """A pass that runs until the exit kills its worker process, which is not
    a cancel: the worker dies and the pass comes back unfinished."""
    def _pass(sid, wav, prompt, num, maxs, cancel_event=None, post_meeting=False,
              devices=None):
        started.set()
        killed.wait(10)
        return False
    return _pass


def test_a_restart_during_a_pass_keeps_the_meeting_queued():
    """2026-11-12: Restart pressed seconds after a meeting ended. The exit killed
    the worker, the pass read as failed and the meeting left the queue, so it
    never got a transcript."""
    settings = _Settings()
    started, killed = threading.Event(), threading.Event()
    q = _queue_class(settings, _killable_pass(started, killed))()
    q._follow_up = lambda sid: None
    q.enqueue(SID)
    sid = q._next_ready()
    runner = threading.Thread(target=q._run, args=(sid,), daemon=True)
    runner.start()
    assert started.wait(5)
    q.stop_for_exit()        # what _end_batch_workers does first
    killed.set()             # then batch_worker.kill_all()
    runner.join(5)
    assert not runner.is_alive()
    assert settings.data["post_meeting_pending"] == [SID]
    after_restart = _queue_class(settings, _done)()
    after_restart.restore()
    assert after_restart._next_ready() == SID, "the next run transcribes it"


def test_an_exit_with_nothing_running_changes_nothing():
    settings = _Settings()
    q = _queue_class(settings, _done)()
    q.enqueue(SID)
    q.stop_for_exit()
    assert not q._cancel.is_set()
    assert settings.data["post_meeting_pending"] == [SID]


def test_every_exit_keeps_the_pass_queued_before_killing_it():
    end = re.search(r"^def _end_batch_workers\(.*?\n(.*?)(?=^def |\Z)", APP_PY, re.M | re.S).group(1)
    assert end.index("_post_meeting.stop_for_exit()") < end.index("batch_worker.kill_all()")
    # Quit, Restart and Update all end the workers through it.
    assert APP_PY.count("_end_batch_workers()") >= 3


def test_a_pass_waits_for_the_voice_library_before_using_it():
    """2026-11-12: a pass resumed at startup began 3 s after launch, before the
    voice library loaded. It skipped clearing the meeting's old voice samples
    and passed no fingerprint callback, so nobody was named and the speaker
    suggestions came from the previous run's audio."""
    def body(name):
        return re.search(rf"^def {name}\(.*?\n(.*?)(?=^def |\Z)", APP_PY, re.M | re.S).group(1)

    rean = body("_run_reanalysis")
    wait = rean.index("_fingerprint_loaded.wait(FINGERPRINT_WAIT_SEC)")
    assert wait < rean.index("if fingerprint_db.ready:")
    assert wait < rean.index("if fingerprint_db.ready else None")
    loader = body("_load_fingerprint_db")
    assert loader.index("finally:") < loader.index("_fingerprint_loaded.set()"), \
        "set even when loading fails, so a pass never waits out the timeout for nothing"
