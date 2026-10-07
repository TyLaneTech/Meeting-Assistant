"""A post-meeting transcription pass stops at its next checkpoint once cancelled,
and never emits another segment afterwards.

The post-meeting worker (transcribe_after_meeting) hands the batch pipeline a
cancel event; start_recording sets it when a new meeting begins so the previous
meeting's transcript can never interleave with the new recording. Both the
progress checkpoint (between Whisper windows) and the segment callback must
honour it.
Run: .venv/Scripts/python -m pytest tests/test_batch_cancel.py
"""
import threading

import pytest

batch = pytest.importorskip("ml.batch_transcriber")


def _pipeline(cancel):
    emitted = []
    return batch.BatchTranscriber(
        on_text_callback=lambda text, spk, s, e: emitted.append((spk, text)),
        cancel_event=cancel,
    ), emitted


def test_runs_normally_until_cancelled():
    cancel = threading.Event()
    bt, emitted = _pipeline(cancel)
    bt._report_progress(0.4)
    bt.on_text_callback("hello", "Speaker 1", 0.0, 1.0)
    assert emitted == [("Speaker 1", "hello")]


def test_progress_checkpoint_raises_once_cancelled():
    cancel = threading.Event()
    bt, _ = _pipeline(cancel)
    cancel.set()
    with pytest.raises(batch.ReanalysisCancelled):
        bt._report_progress(0.5)


def test_no_segment_is_emitted_after_cancel():
    cancel = threading.Event()
    bt, emitted = _pipeline(cancel)
    bt.on_text_callback("before", "Speaker 1", 0.0, 1.0)
    cancel.set()
    with pytest.raises(batch.ReanalysisCancelled):
        bt.on_text_callback("after", "Speaker 1", 1.0, 2.0)
    assert emitted == [("Speaker 1", "before")]


def test_without_a_cancel_event_nothing_changes():
    bt, emitted = _pipeline(None)
    bt._report_progress(1.0)
    bt.on_text_callback("x", "Speaker 2", 0.0, 0.5)
    assert emitted == [("Speaker 2", "x")]


def test_fingerprint_callback_honours_cancel():
    # Review finding 2026-09-15: only the text callback was guarded, so a
    # cancelled pass kept attaching the previous meeting's voices to the
    # recording that replaced it.
    cancel = threading.Event()
    prints = []
    bt = batch.BatchTranscriber(
        on_text_callback=lambda *a: None,
        fingerprint_callback=lambda spk, audio, s, e: prints.append(spk),
        cancel_event=cancel,
    )
    bt.fingerprint_callback("Speaker 1", None, 0.0, 1.0)
    cancel.set()
    with pytest.raises(batch.ReanalysisCancelled):
        bt.fingerprint_callback("Speaker 1", None, 1.0, 2.0)
    assert prints == ["Speaker 1"]


def test_no_fingerprint_callback_stays_none():
    bt = batch.BatchTranscriber(on_text_callback=lambda *a: None)
    assert bt.fingerprint_callback is None


# ── app.py wiring (source-level: importing app.py loads the models) ──────────

def _app_function(name: str) -> str:
    import re
    from pathlib import Path
    app = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    return re.search(rf"^def {name}\(.*?\n(.*?)(?=^def |\Z)", app, re.M | re.S).group(1)


def test_the_live_fallback_never_rebuilds_with_no_model():
    # With the batch pipeline unimportable the reanalysis falls back to the
    # live transcriber, which emits nothing when its model is not loaded. The
    # pass then "succeeded" with an empty transcript after deleting the old
    # one; it must fail instead, so the rollback restores it.
    body = _app_function("_run_reanalysis")
    fallback = body[body.index("except ImportError as ie:"):]
    fallback = fallback[:fallback.index("_transcriber.process_wav_file(wav_path)")]
    assert "if _transcriber.model is None:" in fallback
    assert "raise RuntimeError(" in fallback[fallback.index("if _transcriber.model is None:"):]


def test_a_cancelled_record_only_pass_leaves_no_half_transcript():
    body = _app_function("_run_reanalysis")
    cancelled = body[body.index("except ReanalysisCancelled:"):]
    cancelled = cancelled[:cancelled.index("except Exception as e:")]
    assert "storage.reset_session_transcript(session_id)" in cancelled
    assert 'not before.get("segments")' in cancelled


def test_only_the_after_meeting_pass_lets_record_through():
    # A manual reanalysis has no cancel, so Record must wait for it even while
    # the after-meeting worker holds a session it picked.
    prereqs = _app_function("_recording_prereqs_locked")
    assert 'running == _state.get("session_id")' in prereqs
