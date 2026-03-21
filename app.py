"""
Meeting Assistant — Flask web server.
Run: python app.py
Opens http://127.0.0.1:5000 automatically.
"""
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context
import flask.cli

# ── Suppress Flask / werkzeug console noise ────────────────────────────────────
# Kill the startup banner ("Serving Flask app", "Running on ...", CTRL+C hint)
# and all request logs. We print our own startup message instead.
flask.cli.show_server_banner = lambda *a, **kw: None
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import numpy as np

import log

import config
import settings
import storage
from ai_assistant import AIAssistant
from audio_capture import AudioCapture, enumerate_audio_devices
from speaker_db import SpeakerFingerprintDB
from transcriber import (
    CUDA_AVAILABLE,
    DIARIZER_OPTIONS,
    WHISPER_PRESETS,
    Transcriber,
)

config.ensure_env()
storage.init_db()

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0   # disable static file caching
app.config["TEMPLATES_AUTO_RELOAD"]     = True  # re-read templates on every request

# Fingerprint DB stub — __init__ is called in _load_fingerprint_db() after
# all module-level globals (_state, _on_fingerprint_audio) are defined.
fingerprint_db = SpeakerFingerprintDB.__new__(SpeakerFingerprintDB)
fingerprint_db._db_path   = storage.DB_PATH
fingerprint_db._ready     = False
fingerprint_db._inference = None

# ── Global singletons ─────────────────────────────────────────────────────────

# Load preferences first so we can initialise the AI assistant with the
# saved provider/model rather than hardcoded defaults.
_saved_prefs = settings.load()

ai = AIAssistant(
    provider=_saved_prefs.get("ai_provider", "anthropic"),
    model=_saved_prefs.get("ai_model", "claude-sonnet-4-6"),
)
log.info("ai", f"Provider: {ai.provider}, model: {ai.model}")

_audio_queue: queue.Queue = queue.Queue()
_transcriber = Transcriber(
    _audio_queue,
    lambda text, source, st=0.0, et=0.0: _on_segment(text, source, st, et),
)

# Apply saved model preferences
_saved_whisper = _saved_prefs.get("whisper_preset", "")
if _saved_whisper:
    _wp = next((p for p in WHISPER_PRESETS if p["id"] == _saved_whisper), None)
    if _wp and (not _wp["requires_cuda"] or CUDA_AVAILABLE):
        _transcriber.device = _wp["device"]
        _transcriber.compute_type = _wp["compute_type"]
        _transcriber.model_size = _wp["model_size"]
        log.info("settings", f"Restored whisper preset: {_saved_whisper}")
_transcriber.diarization_enabled = _saved_prefs.get("diarization_enabled", True)
del _saved_prefs, _saved_whisper

# SSE: one queue per connected browser tab
_client_queues: dict[str, queue.Queue] = {}
_cq_lock = threading.Lock()

# Mutable session state — always access under _state_lock
_state: dict = {
    "session_id": None,
    "is_recording": False,
    "segments": [],          # list[{text, source}] — in-memory copy for current session
    "summary": "",
    "chat_history": [],      # list[{role, content}]
    "pending_segments": 0,       # segments since last auto-summary
    "summarized_seg_count": 0,   # segments included in the current summary
    "audio_capture": None,
    "test_capture": None,    # lightweight capture used only for visualizer testing
    "is_testing": False,
    "model_ready": False,
    "model_info": "",
    "diarizer_ready": False,
    "speaker_labels": {},   # speaker_key → display name for the active session
    "custom_prompt": "",    # user-supplied context appended to the summary system prompt
    "is_reanalyzing": False,
    "summary_generating": False,   # True while any _run_summary call is executing
    "summary_manual_pending": False,  # True when /api/summarize was triggered; clears when it runs
    "speaker_audio_accum":    {},  # speaker_key → {"audio": np.ndarray, "total_sec": float}
    "speaker_emb_counts":     {},  # speaker_key → int (embeddings extracted this session)
    "fingerprint_dismissals": {},  # speaker_key → set[global_id]
}
_state_lock = threading.Lock()
_summary_lock = threading.Lock()  # serializes summary runs; prevents auto/manual overlap
_tray = None  # MeetingTray instance (set in main(), None if no tray)

AUTO_SUMMARY_EVERY = 6  # trigger summary after this many new segments
_CUSTOM_SPEAKER_PREFIX = "custom:"


def _refresh_tray() -> None:
    """Update tray icon/menu if a tray is running. Safe to call from any thread."""
    if _tray is not None:
        _tray.refresh()


def _is_custom_speaker_key(speaker_key: str) -> bool:
    return speaker_key.startswith(_CUSTOM_SPEAKER_PREFIX)


_DEFAULT_SPEAKER_RE = re.compile(r"^speaker\s+\d+$", re.IGNORECASE)

def _is_default_speaker_name(name: str) -> bool:
    """Returns True for auto-generated names like 'Speaker 1', 'Speaker 12', etc."""
    return bool(_DEFAULT_SPEAKER_RE.match(name.strip()))


def _normalize_speaker_color(color: str | None) -> str | None:
    if color is None:
        return None
    color = color.strip()
    if not color:
        return None
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("color must be a hex value like #58a6ff")
    return color


def _speaker_summary_update_context(rename_changes: list[tuple[str, str]]) -> str:
    """Describe speaker-label edits in plain language for summary patching."""
    if not rename_changes:
        return ""
    lines = ["Speaker label updates:"]
    for previous_name, current_name in rename_changes:
        lines.append(f'- "{previous_name}" was updated to "{current_name}".')
    lines.append("Update speaker attributions in the summary to match these labels.")
    return "\n".join(lines)


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _push(event: str, data: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _cq_lock:
        dead = []
        for cid, q in _client_queues.items():
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(cid)
        for cid in dead:
            _client_queues.pop(cid, None)
    # Keep tray icon in sync with status changes
    if event == "status":
        _refresh_tray()


# ── Transcript helpers ────────────────────────────────────────────────────────

_SOURCE_LABELS = {
    "loopback": "Desktop",
    "mic":      "Mic",
    "both":     "Desktop+Mic",
}

def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"

def _fmt_segment(seg: dict, speaker_labels: dict | None = None) -> str:
    """Format a {text, source} segment dict as a labelled line for AI context."""
    source = seg["source"]
    if speaker_labels and source in speaker_labels:
        label = speaker_labels[source]
    else:
        label = _SOURCE_LABELS.get(source, source)
    start = seg.get("start_time", 0) or 0
    end = seg.get("end_time", 0) or 0
    if start > 0 or end > 0:
        return f"[{_fmt_time(start)}] [{label}] {seg['text']}"
    return f"[{label}] {seg['text']}"

def _build_transcript(segments: list[dict], speaker_labels: dict | None = None) -> str:
    """Join annotated segments into a single transcript string."""
    return "\n".join(_fmt_segment(s, speaker_labels) for s in segments)


def _build_session_meta(
    segments: list[dict],
    speaker_labels: dict | None = None,
    session_title: str = "",
    is_live: bool = False,
    started_at: str = "",
    ended_at: str = "",
    custom_prompt: str = "",
    current_summary: str = "",
) -> dict:
    """Gather rich metadata about the session for AI context."""
    # Compute speaker roster
    sources = set()
    for s in segments:
        src = s.get("source", "loopback")
        sources.add(src)
    speakers = []
    for src in sorted(sources):
        if speaker_labels and src in speaker_labels:
            display = speaker_labels[src]
            if display != src:
                speakers.append(f"{display} (raw key: {src})")
            else:
                speakers.append(src)
        elif src in _SOURCE_LABELS:
            speakers.append(f"{_SOURCE_LABELS[src]} (audio: {src})")
        else:
            speakers.append(src)

    # Duration
    times = [s.get("start_time", 0) or 0 for s in segments] + [s.get("end_time", 0) or 0 for s in segments]
    max_time = max(times) if times else 0
    duration_str = _fmt_time(max_time) if max_time > 0 else "unknown"

    # Audio source breakdown
    source_types = set()
    for s in segments:
        src = s.get("source", "loopback")
        if src in _SOURCE_LABELS:
            source_types.add(src)

    return {
        "title": session_title,
        "is_live": is_live,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": duration_str,
        "segment_count": len(segments),
        "speakers": speakers,
        "has_desktop_audio": "loopback" in source_types or "both" in source_types,
        "has_mic_audio": "mic" in source_types or "both" in source_types,
        "custom_prompt": custom_prompt,
        "current_summary": current_summary,
    }


# ── Transcription callback ────────────────────────────────────────────────────

def _on_segment(
    text: str,
    source: str = "loopback",
    start_time: float = 0.0,
    end_time: float = 0.0,
) -> None:
    merged = False
    merge_seg_id = None

    with _state_lock:
        sid = _state["session_id"]
        if not sid:
            return

        segments = _state["segments"]

        # Merge with previous segment if same speaker, short gap, and
        # previous text didn't end with sentence-ending punctuation.
        if segments:
            prev = segments[-1]
            same_speaker = prev["source"] == source
            gap = (start_time - prev["end_time"]
                   if start_time > 0 and prev.get("end_time", 0) > 0
                   else float("inf"))
            prev_text = prev["text"].rstrip()
            prev_incomplete = prev_text and prev_text[-1] not in ".?!"

            if same_speaker and gap < 2.0 and prev_incomplete:
                prev["text"] = prev["text"] + " " + text
                prev["end_time"] = end_time
                merge_seg_id = prev.get("_seg_id")
                merged = True
                # Use full merged text for DB / SSE
                text = prev["text"]
                start_time = prev["start_time"]

        if not merged:
            segments.append({
                "text": text, "source": source,
                "start_time": start_time, "end_time": end_time,
                "_seg_id": None,  # filled after DB insert
            })

        _state["pending_segments"] += 1
        should_summarize = (
            settings.get("auto_summary", True)
            and _state["pending_segments"] >= AUTO_SUMMARY_EVERY
            and not _state["is_reanalyzing"]
            and not _state["summary_generating"]
            and not _state["summary_manual_pending"]
        )
        if should_summarize:
            _state["pending_segments"] = 0
            existing_summary = _state["summary"]
            new_seg_count = len(_state["segments"])
            new_transcript = _build_transcript(
                _state["segments"], _state["speaker_labels"]
            )
            custom_prompt = _state["custom_prompt"]
            meta = _build_session_meta(
                _state["segments"],
                _state["speaker_labels"],
                is_live=True,
                custom_prompt=custom_prompt,
                current_summary=existing_summary,
            )

    if merged and merge_seg_id is not None:
        storage.update_segment(merge_seg_id, text, end_time)
        _push("transcript_update", {
            "seg_id": merge_seg_id, "text": text, "end_time": end_time,
        })
    else:
        seg_id = storage.save_segment(sid, text, source, start_time, end_time)
        if not merged:
            # Store DB id for future merges
            with _state_lock:
                if segments:
                    segments[-1]["_seg_id"] = seg_id
        _push("transcript", {
            "text": text, "source": source, "session_id": sid,
            "start_time": start_time, "end_time": end_time,
            "seg_id": seg_id,
        })

    if should_summarize:
        threading.Thread(
            target=_run_summary,
            args=(sid, existing_summary, new_transcript, new_seg_count, custom_prompt, meta),
            daemon=True,
        ).start()


def _run_summary(
    session_id: str,
    existing_summary: str,
    transcript: str,
    seg_count: int,
    custom_prompt: str = "",
    meta: dict | None = None,
    update_context: str = "",
    is_auto: bool = True,
    clears_pending: bool = False,
) -> None:
    """Run a summary update and broadcast the result via SSE.

    Serialized via _summary_lock so auto and manual runs never overlap.

    is_auto=True  (segment-triggered): skips if a manual is pending; re-reads
                  existing_summary after acquiring the lock so it always bases
                  off the latest state even if it queued behind another run.
    is_auto=False (manual / speaker-rename / reanalysis): always runs.
    clears_pending=True: clear summary_manual_pending when we start (only for
                  the direct /api/summarize trigger).

    First summary: streams token-by-token via summary_start/chunk/done.
    Subsequent:   calls patch_summary() and pushes summary_replace.
    """
    with _summary_lock:
        with _state_lock:
            if _state["session_id"] != session_id:
                return
            if clears_pending:
                _state["summary_manual_pending"] = False
            elif is_auto:
                # Bail if a manual is queued — it will run as soon as we finish
                if _state["summary_manual_pending"]:
                    return
                # Re-read in case a prior run updated the summary while we waited
                existing_summary = _state["summary"]
            _state["summary_generating"] = True

        mode = "generating" if not existing_summary else "updating"
        _push("summary_busy", {"busy": True, "mode": mode, "session_id": session_id})

        try:
            def _persist(content: str) -> None:
                with _state_lock:
                    # Auto: discard result if a manual was requested during our run
                    if is_auto and _state.get("summary_manual_pending"):
                        return
                    if _state["session_id"] == session_id:
                        _state["summary"] = content
                        _state["summarized_seg_count"] = seg_count
                storage.save_summary(session_id, content)

            if existing_summary:
                # ── Incremental patch — check for preemption before the AI call ─
                with _state_lock:
                    if is_auto and _state.get("summary_manual_pending"):
                        return
                content = ai.patch_summary(
                    existing_summary,
                    transcript,
                    custom_prompt,
                    meta=meta,
                    update_context=update_context,
                )
                # Check again after the (potentially slow) AI call
                with _state_lock:
                    if is_auto and _state.get("summary_manual_pending"):
                        return
                _persist(content)
                _push("summary_replace", {"content": content, "session_id": session_id})
            else:
                # ── First summary — stream it so the user sees it appear ──────
                _push("summary_start", {"session_id": session_id})
                chunks: list[str] = []

                def on_token(t: str) -> None:
                    chunks.append(t)
                    _push("summary_chunk", {"text": t})

                def on_done() -> None:
                    _persist("".join(chunks))
                    _push("summary_done", {})

                ai.summarize(transcript, on_token, on_done, custom_prompt=custom_prompt, meta=meta)
        finally:
            with _state_lock:
                _state["summary_generating"] = False
            _push("summary_busy", {"busy": False, "session_id": session_id})


def _queue_speaker_summary_refresh(session_id: str, update_context: str) -> None:
    """Patch the current summary after speaker-label changes."""
    if not update_context.strip():
        return

    with _state_lock:
        if _state["session_id"] == session_id:
            existing_summary = _state["summary"]
            if not existing_summary:
                return
            segments = list(_state["segments"])
            labels = dict(_state["speaker_labels"])
            transcript = _build_transcript(segments, labels)
            seg_count = len(segments)
            custom_prompt = _state["custom_prompt"]
            meta = _build_session_meta(
                segments,
                labels,
                is_live=_state["is_recording"],
                custom_prompt=custom_prompt,
                current_summary=existing_summary,
            )
        else:
            existing_summary = ""
            transcript = ""
            seg_count = 0
            custom_prompt = ""
            meta = None

    if not existing_summary:
        sess = storage.get_session(session_id)
        if not sess:
            return
        existing_summary = sess.get("summary", "")
        if not existing_summary:
            return
        labels = sess.get("speaker_labels") or {}
        transcript = _build_transcript(sess["segments"], labels)
        seg_count = len(sess["segments"])
        meta = _build_session_meta(
            sess["segments"],
            labels,
            session_title=sess.get("title", ""),
            is_live=False,
            started_at=sess.get("started_at", ""),
            ended_at=sess.get("ended_at", ""),
            current_summary=existing_summary,
        )

    threading.Thread(
        target=_run_summary,
        args=(session_id, existing_summary, transcript, seg_count, custom_prompt, meta, update_context),
        kwargs={"is_auto": False},
        daemon=True,
    ).start()


# ── Model loading ─────────────────────────────────────────────────────────────

def _load_model() -> None:
    try:
        _transcriber.load_model()
        info = _transcriber.device_info
        with _state_lock:
            _state["model_ready"] = True
            _state["model_info"] = info
        _push("status", {"model_ready": True, "model_info": info})
    except Exception as e:
        log.error("whisper", f"Error loading model: {e}")
        _push("status", {"model_ready": False, "model_info": f"Error: {e}"})


def _load_diarizer() -> None:
    hf_token = os.getenv("HUGGING_FACE_KEY")
    if not hf_token:
        log.warn("diarizer", "HUGGING_FACE_KEY not set — speaker diarization disabled.")
        return
    try:
        saved_device = settings.get("diarizer_device", "")
        if saved_device and (saved_device != "cuda" or CUDA_AVAILABLE):
            log.info("settings", f"Restored diarizer device: {saved_device}")
            _transcriber.load_diarizer(hf_token, device=saved_device)
        else:
            _transcriber.load_diarizer(hf_token)
        with _state_lock:
            _state["diarizer_ready"] = True
        _push("status", {"diarizer_ready": True})
        log.info("diarizer", "Speaker diarization ready.")
        if fingerprint_db.ready:
            _transcriber.fingerprint_callback = _on_fingerprint_audio
    except Exception as e:
        log.error("diarizer", f"Error loading models: {e}")
        log.warn("diarizer", "Transcription will continue without speaker labels.")
        _push("status", {"diarizer_ready": False})


def _load_fingerprint_db() -> None:
    """Load the speaker embedding model. Called after all module globals are set."""
    fingerprint_db.__init__(storage.DB_PATH, os.getenv("HUGGING_FACE_KEY", ""))
    # Wire callback if diarizer already finished loading before we did
    with _state_lock:
        diarizer_ready = _state.get("diarizer_ready", False)
    if fingerprint_db.ready and diarizer_ready:
        _transcriber.fingerprint_callback = _on_fingerprint_audio


threading.Thread(target=_load_model, daemon=True).start()
threading.Thread(target=_load_diarizer, daemon=True).start()
threading.Thread(target=_load_fingerprint_db, daemon=True).start()


def _level_push_loop() -> None:
    """Push audio levels to all SSE clients at ~12 fps while recording or testing."""
    while True:
        time.sleep(0.08)
        with _state_lock:
            is_rec  = _state["is_recording"]
            is_test = _state["is_testing"]
            capture = _state["audio_capture"] if is_rec else _state["test_capture"]
        if capture and (is_rec or is_test):
            _push("audio_level", {
                "loopback":    round(capture.loopback_level, 4),
                "mic":         round(capture.mic_level, 4),
                "has_mic":     capture._has_mic,
                "lb_spectrum": capture.compute_spectrum(capture._lb_fft_buf),
                "mic_spectrum":capture.compute_spectrum(capture._mic_fft_buf),
                "lb_gain":     capture.loopback_gain,
                "mic_gain":    capture.mic_gain,
            })


threading.Thread(target=_level_push_loop, daemon=True).start()


# ── Speaker fingerprint helpers ───────────────────────────────────────────────

def _auto_apply_fingerprint(speaker_key: str, match: dict, emb: np.ndarray, session_id: str) -> None:
    """Silently apply a high-confidence fingerprint match: link, rename, push SSEs."""
    global_id = match["global_id"]
    name  = match["name"]
    color = match.get("color")
    fingerprint_db.add_embedding(global_id, session_id, speaker_key, emb, 0.0)
    fingerprint_db.link_session_speaker(session_id, speaker_key, global_id)
    storage.save_speaker_label(session_id, speaker_key, name=name, color=color)
    with _state_lock:
        if _state["session_id"] == session_id:
            _state["speaker_labels"][speaker_key] = name
    _push("speaker_label", {"session_id": session_id, "speaker_key": speaker_key,
                             "name": name, "color": color})
    _push("fingerprint_auto_applied", {"session_id": session_id, "speaker_key": speaker_key,
                                       "global_id": global_id, "name": name,
                                       "similarity": match["similarity"]})
    log.info("fingerprint", f"Auto-applied {name!r} → {speaker_key} (sim={match['similarity']:.2f})")


def _on_fingerprint_audio(speaker_key: str, audio: np.ndarray, abs_start: float, abs_end: float) -> None:
    """Called from the transcriber thread for each recognized speaker segment.
    Accumulates audio per speaker_key; extracts embeddings once MIN_DURATION_SEC reached.
    """
    if not fingerprint_db.ready:
        return
    duration = abs_end - abs_start
    if duration <= 0 or audio is None or len(audio) == 0:
        return

    with _state_lock:
        sid = _state.get("session_id")
        if not sid:
            return
        counts = _state["speaker_emb_counts"]
        if counts.get(speaker_key, 0) >= 3:
            return  # enough embeddings already extracted this session
        accum = _state["speaker_audio_accum"]
        if speaker_key not in accum:
            accum[speaker_key] = {"audio": audio.copy(), "total_sec": duration}
        else:
            accum[speaker_key]["audio"] = np.concatenate([accum[speaker_key]["audio"], audio])
            accum[speaker_key]["total_sec"] += duration
        if accum[speaker_key]["total_sec"] < fingerprint_db.MIN_DURATION_SEC:
            return

        # Snapshot and reset accumulator (keep last 0.5 s for continuity)
        seg_audio  = accum[speaker_key]["audio"].copy()
        tail_len   = min(int(0.5 * 16_000), len(accum[speaker_key]["audio"]))
        accum[speaker_key] = {"audio": accum[speaker_key]["audio"][-tail_len:], "total_sec": 0.5}
        counts[speaker_key] = counts.get(speaker_key, 0) + 1
        dismissals = {k: set(v) for k, v in _state["fingerprint_dismissals"].items()}

    # Check if already linked (strengthen profile)
    existing_link = fingerprint_db.get_link(sid, speaker_key)

    def _extract_and_match() -> None:
        emb = fingerprint_db.extract_embedding(seg_audio)
        if emb is None:
            return
        if existing_link:
            fingerprint_db.add_embedding(existing_link, sid, speaker_key, emb, duration)
            return
        excluded = dismissals.get(speaker_key, set())
        matches = fingerprint_db.find_matches(emb, exclude_global_ids=excluded)
        if not matches:
            return
        top = matches[0]
        if top["auto_apply"]:
            _auto_apply_fingerprint(speaker_key, top, emb, sid)
        else:
            with _state_lock:
                current_name = _state["speaker_labels"].get(speaker_key, speaker_key)
            _push("fingerprint_match", {"session_id": sid, "speaker_key": speaker_key,
                                        "current_name": current_name, "matches": matches})

    threading.Thread(target=_extract_and_match, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/events")
def events():
    """SSE endpoint — streams all real-time events to the browser."""
    cid = str(uuid.uuid4())
    q: queue.Queue = queue.Queue(maxsize=200)
    with _cq_lock:
        _client_queues[cid] = q

    # Send initial state so a freshly-loaded page knows what's happening
    with _state_lock:
        init = {
            "recording": _state["is_recording"],
            "session_id": _state["session_id"],
            "model_ready": _state["model_ready"],
            "model_info": _state["model_info"],
            "diarizer_ready": _state["diarizer_ready"],
        }
        active_sid = _state["session_id"] if _state["is_recording"] else None
    q.put(f"event: status\ndata: {json.dumps(init)}\n\n")

    # Replay active session so reconnecting clients catch up instantly
    if active_sid:
        after_seg_id = request.args.get("after_seg_id", 0, type=int)
        try:
            sess = storage.get_session(active_sid)
            if sess:
                segs = [s for s in sess.get("segments", [])
                        if s.get("id", 0) > after_seg_id]
                replay_payload = {
                    "session_id":      active_sid,
                    "segments":        segs,
                    "speaker_profiles": sess.get("speaker_profiles", []),
                    "summary":         sess.get("summary", "") or "",
                }
                q.put(f"event: replay\ndata: {json.dumps(replay_payload)}\n\n")
        except Exception:
            pass  # non-fatal — client will simply have a partial transcript

    def generate():
        try:
            while True:
                try:
                    yield q.get(timeout=25)
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with _cq_lock:
                _client_queues.pop(cid, None)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/status")
def get_status():
    with _state_lock:
        return jsonify({
            "recording": _state["is_recording"],
            "session_id": _state["session_id"],
            "model_ready": _state["model_ready"],
            "model_info": _state["model_info"],
            "diarizer_ready": _state["diarizer_ready"],
        })


@app.route("/api/audio/devices")
def get_audio_devices():
    try:
        return jsonify(enumerate_audio_devices())
    except Exception as e:
        return jsonify({"error": str(e), "loopback": [], "input": []}), 500


@app.route("/api/audio/gain", methods=["POST"])
def set_audio_gain():
    """Set loopback and/or mic gain on the active (or test) audio capture."""
    data = request.get_json(silent=True) or {}
    with _state_lock:
        capture = _state["audio_capture"] or _state["test_capture"]
    if capture is None:
        return jsonify({"ok": False, "error": "No active capture"}), 400
    if "lb_gain" in data:
        capture.loopback_gain = float(max(0.0, min(16.0, data["lb_gain"])))
    if "mic_gain" in data:
        capture.mic_gain = float(max(0.0, min(16.0, data["mic_gain"])))
    return jsonify({"ok": True})


@app.route("/api/sessions")
def list_sessions():
    return jsonify(storage.list_sessions())


@app.route("/api/sessions/<session_id>")
def get_session(session_id: str):
    data = storage.get_session(session_id)
    if not data:
        return jsonify({"error": "Not found"}), 404
    wav_path = Path(__file__).parent / "data" / "audio" / f"{session_id}.wav"
    data["has_audio"] = wav_path.exists()
    return jsonify(data)


@app.route("/api/audio/mic-chunk", methods=["POST"])
def mic_chunk():
    """Receive a raw mono Int16 PCM chunk from the browser mic and inject it
    into the currently active capture (recording or test)."""
    data = request.get_data()
    if data:
        with _state_lock:
            capture = (
                _state["audio_capture"] if _state["is_recording"]
                else _state["test_capture"] if _state["is_testing"]
                else None
            )
        if capture:
            capture.inject_mic_data(data)
    return ("", 204)


@app.route("/api/audio/test/start", methods=["POST"])
def start_audio_test():
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot test while recording"}), 400
        if _state["is_testing"]:
            return jsonify({"error": "Already testing"}), 400

    body = request.get_json(silent=True) or {}
    loopback_device = body.get("loopback_device")
    mic_device      = body.get("mic_device")

    # A dummy queue — the mixer writes into it but nothing reads it.
    # We only care about the live loopback_level / mic_level attributes.
    test_queue: queue.Queue = queue.Queue(maxsize=100)
    capture = AudioCapture(test_queue)
    try:
        capture.start(loopback_index=loopback_device, mic_index=mic_device)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    with _state_lock:
        _state["test_capture"] = capture
        _state["is_testing"]   = True

    _push("audio_test_status", {"testing": True})
    return jsonify({"ok": True})


@app.route("/api/audio/test/stop", methods=["POST"])
def stop_audio_test():
    with _state_lock:
        if not _state["is_testing"]:
            return jsonify({"error": "Not testing"}), 400
        capture = _state["test_capture"]
        _state["test_capture"] = None
        _state["is_testing"]   = False

    def _cleanup() -> None:
        if capture:
            capture.stop()
        _push("audio_test_status", {"testing": False})

    threading.Thread(target=_cleanup, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/recording/start", methods=["POST"])
def start_recording():
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Already recording"}), 400
        if not _state["model_ready"]:
            return jsonify({"error": "Transcription model not loaded yet"}), 503
        # Stop any active audio test so it doesn't conflict with the real capture
        test_cap = _state["test_capture"]
        _state["test_capture"] = None
        _state["is_testing"]   = False

    if test_cap:
        threading.Thread(target=test_cap.stop, daemon=True).start()
        _push("audio_test_status", {"testing": False})

    # Drain stale audio from a previous session
    while not _audio_queue.empty():
        try:
            _audio_queue.get_nowait()
        except queue.Empty:
            break

    body = request.get_json(silent=True) or {}
    title             = body.get("title")
    loopback_device   = body.get("loopback_device")   # int | None
    mic_device        = body.get("mic_device")         # int | None | -1
    resume_session_id = body.get("resume_session_id")  # str | None

    # ── Resume an existing session ──────────────────────────────────────────
    if resume_session_id:
        sess = storage.get_session(resume_session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        session_id = resume_session_id
        storage.resume_session(session_id)
        existing_segments = [
            {"text": s["text"], "source": s["source"],
             "start_time": s["start_time"], "end_time": s["end_time"]}
            for s in sess.get("segments", [])
        ]
        existing_summary   = sess.get("summary", "")
        existing_chat      = [{"role": m["role"], "content": m["content"]}
                               for m in sess.get("chat_messages", [])]
        existing_labels    = {p["speaker_key"]: p["name"]
                               for p in sess.get("speaker_profiles", [])}
        existing_seg_count = len(existing_segments)
    else:
        session_id         = storage.create_session(title)
        existing_segments  = []
        existing_summary   = ""
        existing_chat      = []
        existing_labels    = {}
        existing_seg_count = 0

    capture = AudioCapture(_audio_queue)
    if not resume_session_id:
        # Set up WAV recording before starting capture (sample_rate resolved in start())
        wav_dir = Path(__file__).parent / "data" / "audio"
        capture.start_wav(str(wav_dir / f"{session_id}.wav"))
    try:
        capture.start(
            loopback_index=loopback_device,
            mic_index=mic_device,
        )
    except Exception as e:
        capture.stop_wav()
        if not resume_session_id:
            storage.end_session(session_id)
        return jsonify({"error": str(e)}), 500

    _transcriber.start(capture.sample_rate, capture.channels)

    with _state_lock:
        _state.update({
            "is_recording": True,
            "session_id": session_id,
            "segments": existing_segments,
            "summary": existing_summary,
            "chat_history": existing_chat,
            "pending_segments": 0,
            "summarized_seg_count": existing_seg_count,
            "audio_capture": capture,
            "speaker_labels": existing_labels,
            "speaker_audio_accum":    {},
            "speaker_emb_counts":     {},
            "fingerprint_dismissals": {},
        })

    verb = "Resumed" if resume_session_id else "Started"
    log.info("recording", f"{verb} — session {session_id}")
    _push("status", {"recording": True, "session_id": session_id,
                     "resumed": bool(resume_session_id)})
    return jsonify({"session_id": session_id})


@app.route("/api/recording/stop", methods=["POST"])
def stop_recording():
    with _state_lock:
        if not _state["is_recording"]:
            return jsonify({"error": "Not recording"}), 400
        sid = _state["session_id"]
        capture: AudioCapture = _state["audio_capture"]
        # Snapshot transcript now — state may change before cleanup thread runs
        # plain_snapshot is used for title generation (no source labels needed)
        plain_snapshot = " ".join(s["text"] for s in _state["segments"])
        transcript_snapshot = _build_transcript(_state["segments"], _state["speaker_labels"])
        _state["is_recording"] = False
        _state["audio_capture"] = None

    # Return immediately — cleanup blocks for up to 12 s (thread join) so we
    # must not do it on the Flask request handler thread or the server hangs.
    def _cleanup() -> None:
        if capture:
            capture.stop_wav()   # finalize WAV header before stopping streams
            capture.stop()
        _transcriber.stop()
        if sid:
            storage.end_session(sid)
            seg_count = len(_state.get("segments", []))
            log.info("recording", f"Stopped — session {sid} ({seg_count} segments)")
        _push("status", {"recording": False, "session_id": sid})
        # Auto-title: use full formatted transcript (with speaker labels) for better context
        if sid and (transcript_snapshot or plain_snapshot).strip():
            title = ai.generate_title(transcript_snapshot or plain_snapshot)
            if title:
                storage.update_session_title(sid, title)
                _push("session_title", {"session_id": sid, "title": title})

    threading.Thread(target=_cleanup, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/summarize", methods=["POST"])
def summarize():
    """Manually trigger a full summary regeneration for the given session."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")

    # Get full transcript for this session
    with _state_lock:
        active_sid = _state["session_id"]
        if session_id == active_sid:
            segments = list(_state["segments"])
            labels = dict(_state["speaker_labels"])
            transcript = _build_transcript(segments, labels)
            seg_count = len(segments)
            custom_prompt = _state["custom_prompt"]
            meta = _build_session_meta(
                segments, labels,
                is_live=_state["is_recording"],
                custom_prompt=custom_prompt,
            )
        else:
            transcript = None
            seg_count = None
            custom_prompt = ""
            meta = None

    if transcript is None:
        # Load from DB
        sess = storage.get_session(session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        labels = sess.get("speaker_labels") or {}
        transcript = _build_transcript(sess["segments"], labels)
        seg_count = len(sess["segments"])
        meta = _build_session_meta(
            sess["segments"], labels,
            session_title=sess.get("title", ""),
            is_live=False,
            started_at=sess.get("started_at", ""),
            ended_at=sess.get("ended_at", ""),
        )

    # Signal any running auto-summary to discard its result, then regenerate from scratch.
    with _state_lock:
        _state["summary_manual_pending"] = True
    threading.Thread(
        target=_run_summary,
        args=(session_id, "", transcript, seg_count, custom_prompt, meta),
        kwargs={"is_auto": False, "clears_pending": True},
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/custom-prompt", methods=["GET", "POST"])
def custom_prompt_endpoint():
    """Get or set the custom summary prompt for the current session."""
    if request.method == "GET":
        with _state_lock:
            return jsonify({"custom_prompt": _state["custom_prompt"]})
    data = request.get_json(silent=True) or {}
    with _state_lock:
        _state["custom_prompt"] = data.get("custom_prompt", "")
    return jsonify({"ok": True})


@app.route("/api/settings/keys", methods=["GET"])
def get_keys():
    """Return masked key values and status."""
    return jsonify(config.get_key_status())


@app.route("/api/settings/keys", methods=["POST"])
def set_keys():
    """Save one or more API keys. Triggers side-effects (client reload, etc)."""
    data = request.get_json(silent=True) or {}
    changed = []

    for key_name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HUGGING_FACE_KEY"):
        val = data.get(key_name)
        if val is not None:
            config.save_key(key_name, val)
            changed.append(key_name)

    # Reload AI client if the active provider's key changed
    active_provider = settings.get("ai_provider", "anthropic")
    provider_key = "OPENAI_API_KEY" if active_provider == "openai" else "ANTHROPIC_API_KEY"
    if provider_key in changed:
        ai.reload_client()

    # If HF key was just set and diarizer isn't loaded, start loading it
    if "HUGGING_FACE_KEY" in changed and data.get("HUGGING_FACE_KEY", "").strip():
        with _state_lock:
            need_diarizer = not _state["diarizer_ready"]
        if need_diarizer:
            threading.Thread(target=_load_diarizer, daemon=True).start()

    # Refresh tray icon if present
    _refresh_tray()

    return jsonify({"ok": True, "keys": config.get_key_status()})


@app.route("/api/settings/status")
def settings_status():
    """Combined status for the settings page: keys, CUDA, setup state."""
    provider = settings.get("ai_provider", "anthropic")
    return jsonify({
        "needs_setup": config.needs_setup(provider),
        "cuda_available": CUDA_AVAILABLE,
        "keys": config.get_key_status(),
    })


# ── AI provider / model settings ──────────────────────────────────────────────

# Available models per provider (ordered: most capable first)
_AI_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4-6",           "label": "Claude Opus 4.6 — most capable"},
        {"id": "claude-sonnet-4-6",          "label": "Claude Sonnet 4.6 — recommended"},
        {"id": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5 — fastest"},
    ],
    "openai": [
        {"id": "gpt-5.4",              "label": "GPT-5.4"},
        {"id": "gpt-5.3-chat-latest",  "label": "GPT-5.3"},
        {"id": "gpt-4o",       "label": "GPT-4o"},
        {"id": "gpt-4o-mini",  "label": "GPT-4o mini"},
    ],
}

_DEFAULT_MODEL = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.3-chat-latest",
}

# OpenAI model filtering
_OPENAI_CHAT_PREFIXES = ("gpt-5", "gpt-4", "gpt-3.5-turbo", "o1", "o3", "o4", "chatgpt-4o")
_OPENAI_EXCLUDE = (
    "realtime", "-audio-", "-transcribe", "-tts", "whisper", "dall-e",
    "embedding", "davinci", "babbage", "curie", "ada", "-search-",
    "instruct", "moderation",
)


def _models_for_provider(provider: str) -> list[dict]:
    """Return the configured model list for a provider."""
    return _AI_MODELS.get(provider, _AI_MODELS["anthropic"])


def _normalize_ai_selection(provider: str, model: str | None) -> tuple[str, str]:
    """Ensure provider/model are valid and aligned with each other."""
    provider = provider if provider in _AI_MODELS else "anthropic"
    valid_ids = {m["id"] for m in _models_for_provider(provider)}
    if model in valid_ids:
        return provider, model
    return provider, _DEFAULT_MODEL.get(provider, next(iter(valid_ids), ""))


def _fetch_anthropic_models() -> list[dict]:
    """Fetch Claude models from the Anthropic API. Falls back to static list."""
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return _AI_MODELS["anthropic"]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        page = client.models.list()
        result = [
            {"id": m.id, "label": getattr(m, "display_name", m.id)}
            for m in page.data
        ]
        return result or _AI_MODELS["anthropic"]
    except Exception as e:
        log.warn("ai", f"Failed to fetch Anthropic models: {e}")
        return _AI_MODELS["anthropic"]


def _fetch_openai_models() -> list[dict]:
    """Fetch chat-capable models from the OpenAI API. Falls back to static list."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return _AI_MODELS["openai"]
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        all_models = list(client.models.list())

        def _is_chat(mid: str) -> bool:
            m = mid.lower()
            if any(exc in m for exc in _OPENAI_EXCLUDE):
                return False
            return any(m.startswith(p) for p in _OPENAI_CHAT_PREFIXES)

        filtered = [m for m in all_models if _is_chat(m.id)]
        filtered.sort(key=lambda m: (-m.created, m.id))
        result = [{"id": m.id, "label": m.id} for m in filtered]
        return result or _AI_MODELS["openai"]
    except Exception as e:
        log.warn("ai", f"Failed to fetch OpenAI models: {e}")
        return _AI_MODELS["openai"]


@app.route("/api/ai_settings/models")
def get_ai_settings_models():
    """Return available models for a provider, fetched live from the API."""
    provider = request.args.get("provider", ai.provider)
    if provider == "openai":
        models = _fetch_openai_models()
    else:
        models = _fetch_anthropic_models()
    return jsonify({"provider": provider, "models": models})


@app.route("/api/ai_settings", methods=["GET"])
def get_ai_settings():
    """Return current AI provider, model, and available options."""
    provider, model = _normalize_ai_selection(ai.provider, ai.model)
    return jsonify({
        "provider": provider,
        "model": model,
        "models": _AI_MODELS,
    })


@app.route("/api/ai_settings", methods=["POST"])
def set_ai_settings():
    """Update AI provider and/or model. Reloads the client immediately."""
    data = request.get_json(silent=True) or {}
    new_provider = data.get("provider")
    new_model = data.get("model")
    target_provider = new_provider or ai.provider
    target_model = new_model if new_model is not None else ai.model
    target_provider, target_model = _normalize_ai_selection(target_provider, target_model)

    updates = {}
    if target_provider != ai.provider:
        updates["ai_provider"] = target_provider
    if target_model != ai.model:
        updates["ai_model"] = target_model
    if updates:
        settings.update(updates)
        ai.reload_client(
            provider=target_provider,
            model=target_model,
        )

    return jsonify({"ok": True, "provider": ai.provider, "model": ai.model})


@app.route("/api/preferences", methods=["GET"])
def get_preferences():
    """Return all saved user preferences."""
    return jsonify(settings.load())


@app.route("/api/preferences", methods=["PUT"])
def set_preferences():
    """Update one or more user preferences."""
    data = request.get_json(silent=True) or {}
    updated = settings.update(data)
    return jsonify(updated)


@app.route("/api/models", methods=["GET"])
def get_models():
    """Return current model config and available presets."""
    has_hf_key = bool(os.getenv("HUGGING_FACE_KEY"))
    diarizer_device = _transcriber.diarizer_device
    with _state_lock:
        diarizer_ready = _state["diarizer_ready"]

    # If the diarizer hasn't loaded yet but an HF key exists, infer the
    # device from CUDA availability so the dropdown shows the right value
    # instead of "Disabled".
    if diarizer_device is None and has_hf_key:
        diarizer_device = "cuda" if CUDA_AVAILABLE else "cpu"

    return jsonify({
        "cuda_available": CUDA_AVAILABLE,
        "whisper": {
            "current": _transcriber.whisper_preset_id,
            "presets": [
                {**p, "available": not p["requires_cuda"] or CUDA_AVAILABLE}
                for p in WHISPER_PRESETS
            ],
        },
        "diarizer": {
            "current": diarizer_device,
            "has_key": has_hf_key,
            "ready": diarizer_ready,
            "enabled": _transcriber.diarization_enabled,
            "options": [
                {**o, "available": not o["requires_cuda"] or CUDA_AVAILABLE}
                for o in DIARIZER_OPTIONS
            ],
        },
    })


@app.route("/api/models/whisper", methods=["POST"])
def set_whisper_model():
    """Change the Whisper model. Cannot change while recording."""
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot change model while recording"}), 400

    data = request.get_json(silent=True) or {}
    preset_id = data.get("preset_id", "").strip()
    preset = next((p for p in WHISPER_PRESETS if p["id"] == preset_id), None)
    if not preset:
        return jsonify({"error": "Unknown preset"}), 400
    if preset["requires_cuda"] and not CUDA_AVAILABLE:
        return jsonify({"error": "CUDA not available"}), 400

    # Already on this preset?
    if preset_id == _transcriber.whisper_preset_id:
        return jsonify({"ok": True, "info": _transcriber.device_info})

    _push("status", {"model_ready": False, "model_info": f"Loading {preset['label']}…"})
    with _state_lock:
        _state["model_ready"] = False
        _state["model_info"] = f"Loading {preset['label']}…"

    def _reload():
        try:
            _transcriber.reload_model(preset["device"], preset["compute_type"], preset["model_size"])
            settings.put("whisper_preset", preset_id)
            info = _transcriber.device_info
            with _state_lock:
                _state["model_ready"] = True
                _state["model_info"] = info
            _push("status", {"model_ready": True, "model_info": info})
        except Exception as e:
            log.error("whisper", f"Error reloading model: {e}")
            with _state_lock:
                _state["model_ready"] = False
                _state["model_info"] = f"Error: {e}"
            _push("status", {"model_ready": False, "model_info": f"Error: {e}"})

    threading.Thread(target=_reload, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/models/diarizer/enabled", methods=["POST"])
def set_diarizer_enabled():
    """Toggle speaker diarization on/off without unloading the model."""
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    _transcriber.diarization_enabled = enabled
    settings.put("diarization_enabled", enabled)
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/models/diarizer", methods=["POST"])
def set_diarizer_model():
    """Change the diarizer device. Cannot change while recording."""
    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot change model while recording"}), 400

    data = request.get_json(silent=True) or {}
    device = data.get("device", "").strip()
    option = next((o for o in DIARIZER_OPTIONS if o["id"] == device), None)
    if not option:
        return jsonify({"error": "Unknown device option"}), 400
    if option["requires_cuda"] and not CUDA_AVAILABLE:
        return jsonify({"error": "CUDA not available"}), 400

    if device == _transcriber.diarizer_device:
        return jsonify({"ok": True})

    hf_token = os.getenv("HUGGING_FACE_KEY")
    if not hf_token:
        return jsonify({"error": "HUGGING_FACE_KEY not set"}), 400

    _push("status", {"diarizer_ready": False})
    with _state_lock:
        _state["diarizer_ready"] = False

    def _reload():
        try:
            _transcriber.reload_diarizer(hf_token, device)
            settings.put("diarizer_device", device)
            with _state_lock:
                _state["diarizer_ready"] = True
            _push("status", {"diarizer_ready": True})
        except Exception as e:
            log.error("diarizer", f"Error reloading: {e}")
            _push("status", {"diarizer_ready": False})

    threading.Thread(target=_reload, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    """Send a chat message. Response is streamed via SSE."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id")
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    request_id = str(uuid.uuid4())

    # Determine transcript, chat history, and metadata for context
    with _state_lock:
        active_sid = _state["session_id"]
        if session_id == active_sid:
            segments = list(_state["segments"])
            labels = dict(_state["speaker_labels"])
            transcript = _build_transcript(segments, labels)
            chat_history = list(_state["chat_history"])
            meta = _build_session_meta(
                segments, labels,
                is_live=_state["is_recording"],
                custom_prompt=_state["custom_prompt"],
                current_summary=_state["summary"],
            )
        else:
            transcript = None
            chat_history = []
            meta = None

    if transcript is None:
        sess = storage.get_session(session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        labels = sess.get("speaker_labels") or {}
        transcript = _build_transcript(sess["segments"], labels)
        chat_history = [
            {"role": m["role"], "content": m["content"]}
            for m in sess["chat_messages"]
        ]
        meta = _build_session_meta(
            sess["segments"], labels,
            session_title=sess.get("title", ""),
            is_live=False,
            started_at=sess.get("started_at", ""),
            ended_at=sess.get("ended_at", ""),
            current_summary=sess.get("summary", ""),
        )

    # Append the new question
    chat_history.append({"role": "user", "content": question})

    # Persist user message
    storage.save_chat_message(session_id, "user", question)

    # Update in-memory history if this is the active session
    with _state_lock:
        if session_id == _state["session_id"]:
            _state["chat_history"].append({"role": "user", "content": question})

    def run_chat():
        _push("chat_start", {"request_id": request_id, "question": question})
        response_chunks: list[str] = []

        def on_token(t: str) -> None:
            response_chunks.append(t)
            _push("chat_chunk", {"request_id": request_id, "text": t})

        def on_done() -> None:
            full = "".join(response_chunks)
            storage.save_chat_message(session_id, "assistant", full)
            with _state_lock:
                if session_id == _state["session_id"]:
                    _state["chat_history"].append({"role": "assistant", "content": full})
            _push("chat_done", {"request_id": request_id})

        ai.ask(transcript, chat_history, on_token, on_done, meta=meta)

    threading.Thread(target=run_chat, daemon=True).start()
    return jsonify({"request_id": request_id})


@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def delete_session(session_id: str):
    storage.delete_session(session_id)
    return jsonify({"ok": True})


@app.route("/api/segments/<int:seg_id>/label", methods=["PATCH"])
def update_segment_label(seg_id: int):
    """Set a per-segment label override (one-off rename)."""
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "label is required"}), 400
    storage.save_segment_label_override(seg_id, label)
    return jsonify({"ok": True})


@app.route("/api/sessions/<session_id>/speakers", methods=["GET"])
def list_speaker_profiles(session_id: str):
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404
    return jsonify({"speakers": storage.list_speaker_profiles(session_id)})


@app.route("/api/sessions/<session_id>/speakers", methods=["POST"])
def create_speaker_profile(session_id: str):
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    try:
        color = _normalize_speaker_color(data.get("color"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    speaker_key = f"{_CUSTOM_SPEAKER_PREFIX}{uuid.uuid4().hex[:8]}"
    speaker = storage.save_speaker_label(session_id, speaker_key, name=name, color=color)
    return jsonify({"ok": True, "speaker": speaker}), 201


@app.route("/api/sessions/<session_id>/speakers", methods=["PATCH"])
def update_speaker_label(session_id: str):
    sess = storage.get_session(session_id)
    if not sess:
        return jsonify({"error": "Session not found"}), 404

    data = request.get_json(silent=True) or {}
    raw_keys = data.get("speaker_keys")
    if raw_keys is None:
        speaker_key = (data.get("speaker_key") or "").strip()
        speaker_keys = [speaker_key] if speaker_key else []
    else:
        speaker_keys = [
            str(k).strip() for k in raw_keys
            if str(k).strip()
        ]
    if not speaker_keys:
        return jsonify({"error": "speaker_key or speaker_keys required"}), 400

    name = data.get("name")
    if name is not None:
        name = str(name).strip()
        if not name:
            return jsonify({"error": "name cannot be blank"}), 400

    try:
        color = _normalize_speaker_color(data.get("color"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if name is None and color is None:
        return jsonify({"error": "name and/or color required"}), 400

    updated_speakers = []
    rename_changes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for speaker_key in speaker_keys:
        if speaker_key in seen:
            continue
        seen.add(speaker_key)
        previous = storage.get_speaker_profile(session_id, speaker_key) or {}
        updated = storage.save_speaker_label(session_id, speaker_key, name=name, color=color)
        updated_speakers.append(updated)
        previous_name = (previous.get("name") or speaker_key).strip()
        if name is not None and previous_name != updated["name"] and not _is_custom_speaker_key(speaker_key):
            rename_changes.append((previous_name, updated["name"]))

    with _state_lock:
        if _state["session_id"] == session_id:
            for speaker in updated_speakers:
                speaker_key = speaker["speaker_key"]
                speaker_name = speaker["name"]

                # Detect merge: another diarized speaker key already has this display name.
                existing_key = next(
                    (
                        k for k, v in _state["speaker_labels"].items()
                        if k != speaker_key and v.lower() == speaker_name.lower() and not _is_custom_speaker_key(k)
                    ),
                    None,
                )
                if not _is_custom_speaker_key(speaker_key):
                    _state["speaker_labels"][speaker_key] = speaker_name
                    if existing_key and _transcriber.diarizer is not None:
                        _transcriber.diarizer.merge_speakers(existing_key, speaker_key)

    for speaker in updated_speakers:
        _push("speaker_label", {
            "session_id": session_id,
            "speaker_key": speaker["speaker_key"],
            "name": speaker["name"],
            "color": speaker["color"],
        })

    update_context = _speaker_summary_update_context(rename_changes)
    if update_context:
        _queue_speaker_summary_refresh(session_id, update_context)

    # ── Auto-create or link global voice profile ───────────────────────────────
    # For every speaker key that now has a user-assigned name (not a default
    # "Speaker N"), ensure a global profile exists and the key is linked to it.
    if fingerprint_db._ready and name and not _is_default_speaker_name(name):
        def _sync_voice_profile(sid, keys, label, col):
            profile = fingerprint_db.find_by_name(label)
            if profile is None:
                gid = fingerprint_db.create_global_speaker(label, col)
                log.info("fingerprint", f"Auto-created profile {label!r} from session label")
            else:
                gid = profile["id"]
                # If the profile exists but has a differently-cased name, keep
                # the stored casing (don't rename just because of case difference).
            for k in keys:
                existing = fingerprint_db.get_link(sid, k)
                if existing != gid:
                    fingerprint_db.link_session_speaker(sid, k, gid)
        threading.Thread(
            target=_sync_voice_profile,
            args=(session_id, [s["speaker_key"] for s in updated_speakers],
                  name, color),
            daemon=True,
        ).start()
    # ── End auto-link ──────────────────────────────────────────────────────────

    return jsonify({"ok": True, "speakers": updated_speakers})


@app.route("/api/sessions/<session_id>/audio")
def session_audio(session_id: str):
    """Serve the recorded WAV file for browser playback."""
    wav_path = Path(__file__).parent / "data" / "audio" / f"{session_id}.wav"
    if not wav_path.exists():
        return jsonify({"error": "No audio recording for this session"}), 404
    return send_file(str(wav_path), mimetype="audio/wav", conditional=True)


def _run_reanalysis(session_id: str, wav_path: str, custom_prompt: str) -> None:
    """Worker: clear DB data, retranscribe the WAV, then regenerate summary."""
    try:
        # Clear stored data (preserves session title/timestamps)
        storage.reset_session_transcript(session_id)

        # Reset in-memory state for this session
        with _state_lock:
            if _state["session_id"] == session_id:
                _state["segments"] = []
                _state["summary"] = ""
                _state["chat_history"] = []
                _state["pending_segments"] = 0
                _state["summarized_seg_count"] = 0
                _state["speaker_labels"] = {}

        _push("reanalysis_start", {"session_id": session_id})
        _push("transcript_reset", {"session_id": session_id})

        # Run transcription synchronously (blocks until complete)
        _transcriber.process_wav_file(wav_path)

        # Trigger a fresh summary from the new transcript
        with _state_lock:
            if _state["session_id"] == session_id:
                segments = list(_state["segments"])
                labels = dict(_state["speaker_labels"])
            else:
                segments = []
                labels = {}

        if not segments:
            # Load from DB in case segments were not kept in memory
            sess = storage.get_session(session_id)
            if sess:
                segments = sess["segments"]
                labels = sess.get("speaker_labels") or {}

        if segments:
            transcript = _build_transcript(segments, labels)
            meta = _build_session_meta(
                segments, labels,
                is_live=False,
                custom_prompt=custom_prompt,
            )
            _run_summary(session_id, "", transcript, len(segments), custom_prompt, meta,
                         is_auto=False)

        _push("reanalysis_done", {"session_id": session_id})
    except Exception as e:
        log.error("reanalysis", f"{e}")
        import traceback; traceback.print_exc()
        _push("reanalysis_error", {"session_id": session_id, "error": str(e)})
    finally:
        with _state_lock:
            if _state["session_id"] == session_id:
                _state["is_reanalyzing"] = False


@app.route("/api/sessions/<session_id>/reanalyze", methods=["POST"])
def reanalyze_session(session_id: str):
    """Re-transcribe + re-summarize a session from its saved WAV file."""
    wav_path = Path(__file__).parent / "data" / "audio" / f"{session_id}.wav"
    if not wav_path.exists():
        return jsonify({"error": "No audio recording for this session"}), 404

    with _state_lock:
        if _state["is_recording"]:
            return jsonify({"error": "Cannot reanalyze while recording"}), 400
        if _state.get("is_reanalyzing"):
            return jsonify({"error": "Reanalysis already in progress"}), 400
        if not _state["model_ready"]:
            return jsonify({"error": "Transcription model not loaded yet"}), 503
        # Load the session into active state so _on_segment callbacks work
        sess = storage.get_session(session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        _state["session_id"] = session_id
        _state["is_reanalyzing"] = True
        _state["segments"] = []
        _state["summary"] = ""
        _state["chat_history"] = []
        _state["pending_segments"] = 0
        _state["summarized_seg_count"] = 0
        _state["speaker_labels"] = {}

    body = request.get_json(silent=True) or {}
    custom_prompt = body.get("custom_prompt", "")

    threading.Thread(
        target=_run_reanalysis,
        args=(session_id, str(wav_path), custom_prompt),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/sessions/<session_id>", methods=["PATCH"])
def patch_session(session_id: str):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    storage.update_session_title(session_id, title)
    return jsonify({"ok": True})


# ── Folder endpoints ──────────────────────────────────────────────────────────

@app.route("/api/folders", methods=["GET"])
def list_folders():
    return jsonify(storage.list_folders())


@app.route("/api/folders", methods=["POST"])
def create_folder():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    fid = storage.create_folder(name)
    return jsonify({"ok": True, "id": fid}), 201


@app.route("/api/folders/<folder_id>", methods=["PATCH"])
def rename_folder(folder_id: str):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    storage.rename_folder(folder_id, name)
    return jsonify({"ok": True})


@app.route("/api/folders/<folder_id>", methods=["DELETE"])
def delete_folder(folder_id: str):
    storage.delete_folder(folder_id)
    return jsonify({"ok": True})


# ── Bulk session operations ────────────────────────────────────────────────────

@app.route("/api/sessions/bulk", methods=["POST"])
def bulk_sessions():
    """Bulk operations: delete, retitle, or move sessions to a folder."""
    data = request.get_json(silent=True) or {}
    action      = (data.get("action") or "").strip()
    session_ids = [str(s) for s in (data.get("session_ids") or []) if s]
    if not session_ids:
        return jsonify({"error": "session_ids required"}), 400

    if action == "delete":
        for sid in session_ids:
            storage.delete_session(sid)
            # Clear active session state if it was one of the deleted sessions
            with _state_lock:
                if _state["session_id"] == sid and not _state["is_recording"]:
                    _state["session_id"] = None
        return jsonify({"ok": True, "deleted": len(session_ids)})

    elif action == "retitle":
        results = []
        for sid in session_ids:
            sess = storage.get_session(sid)
            if not sess:
                continue
            labels  = sess.get("speaker_labels") or {}
            segs    = sess.get("segments") or []
            if not segs:
                continue
            transcript = _build_transcript(segs, labels)
            title = ai.generate_title(transcript or " ".join(s["text"] for s in segs))
            if title:
                storage.update_session_title(sid, title)
                _push("session_title", {"session_id": sid, "title": title})
                results.append({"session_id": sid, "title": title})
        return jsonify({"ok": True, "updated": results})

    elif action == "move":
        folder_id = data.get("folder_id")  # None = uncategorize
        storage.bulk_set_folder(session_ids, folder_id or None)
        return jsonify({"ok": True})

    else:
        return jsonify({"error": f"Unknown action: {action!r}"}), 400


# ── Fingerprint / Voice Library endpoints ─────────────────────────────────────

def _fp_unavailable():
    return jsonify({"error": "Voice library not available (no HF key or model load failed)"}), 503


@app.route("/api/fingerprint/speakers", methods=["GET"])
def fp_list_speakers():
    return jsonify(fingerprint_db.list_global_speakers())


@app.route("/api/fingerprint/speakers", methods=["POST"])
def fp_create_speaker():
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        color = _normalize_speaker_color(data.get("color"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    gid = fingerprint_db.create_global_speaker(name, color)
    return jsonify({"ok": True, "global_id": gid}), 201


@app.route("/api/fingerprint/speakers/<global_id>", methods=["PATCH"])
def fp_update_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if name is not None:
        name = str(name).strip()
    try:
        color = _normalize_speaker_color(data.get("color")) if "color" in data else ...
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    fingerprint_db.rename_global_speaker(global_id, name=name or None, color=color)
    return jsonify({"ok": True})


@app.route("/api/fingerprint/speakers/<global_id>", methods=["DELETE"])
def fp_delete_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    fingerprint_db.delete_global_speaker(global_id)
    return jsonify({"ok": True})


@app.route("/api/fingerprint/speakers/<global_id>/merge", methods=["POST"])
def fp_merge_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    source_id = (data.get("source_id") or "").strip()
    if not source_id:
        return jsonify({"error": "source_id is required"}), 400
    fingerprint_db.merge_global_speakers(keep_id=global_id, merge_id=source_id)
    return jsonify({"ok": True})


@app.route("/api/fingerprint/speakers/<global_id>/optimize", methods=["POST"])
def fp_optimize_speaker(global_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    fingerprint_db.prune_embeddings(global_id, keep_newest=30)
    return jsonify({"ok": True})


@app.route("/api/fingerprint/speakers/<global_id>/sessions", methods=["GET"])
def fp_speaker_sessions(global_id: str):
    sessions = fingerprint_db.get_profile_sessions(global_id)
    return jsonify(sessions)


@app.route("/api/fingerprint/confirm", methods=["POST"])
def fp_confirm():
    """User accepted a fingerprint match suggestion."""
    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    speaker_key = (data.get("speaker_key") or "").strip()
    global_id = (data.get("global_id") or "").strip()
    if not session_id or not speaker_key or not global_id:
        return jsonify({"error": "session_id, speaker_key, global_id required"}), 400

    profile = fingerprint_db.get_global_speaker(global_id)
    if not profile:
        return jsonify({"error": "Global speaker not found"}), 404

    name  = profile["name"]
    color = profile.get("color")

    # Link all speaker_keys in the active session that share the same display name
    with _state_lock:
        sid = _state.get("session_id")
        labels = dict(_state.get("speaker_labels", {}))

    current_name = labels.get(speaker_key, speaker_key)
    keys_to_link = [k for k, n in labels.items()
                    if n.lower() == current_name.lower() and not _is_custom_speaker_key(k)]
    if speaker_key not in keys_to_link:
        keys_to_link.append(speaker_key)

    for key in keys_to_link:
        fingerprint_db.link_session_speaker(session_id, key, global_id)
        storage.save_speaker_label(session_id, key, name=name, color=color)
        if sid == session_id:
            with _state_lock:
                _state["speaker_labels"][key] = name
        _push("speaker_label", {"session_id": session_id, "speaker_key": key,
                                 "name": name, "color": color})

    # Add embedding for this speaker_key from the latest stored embedding
    latest = fingerprint_db.get_latest_embedding(global_id, session_id, speaker_key)
    if latest is None:
        # Try to get one from accumulator if this is the active session
        with _state_lock:
            accum = _state.get("speaker_audio_accum", {})
            seg_audio = accum.get(speaker_key, {}).get("audio")

        if seg_audio is not None and len(seg_audio) > 0:
            def _add_emb():
                emb = fingerprint_db.extract_embedding(seg_audio)
                if emb is not None:
                    fingerprint_db.add_embedding(global_id, session_id, speaker_key, emb, 0.0)
            threading.Thread(target=_add_emb, daemon=True).start()

    log.info("fingerprint", f"Confirmed {name!r} for {speaker_key} in session {session_id[:8]}")
    return jsonify({"ok": True})


@app.route("/api/fingerprint/dismiss", methods=["POST"])
def fp_dismiss():
    """User dismissed a fingerprint match — suppress it for this session."""
    data = request.get_json(silent=True) or {}
    session_id  = (data.get("session_id") or "").strip()
    speaker_key = (data.get("speaker_key") or "").strip()
    global_id   = (data.get("global_id") or "").strip()  # optional
    if not session_id or not speaker_key:
        return jsonify({"error": "session_id and speaker_key required"}), 400

    with _state_lock:
        if _state.get("session_id") == session_id:
            dismissals = _state["fingerprint_dismissals"]
            if speaker_key not in dismissals:
                dismissals[speaker_key] = set()
            if global_id:
                dismissals[speaker_key].add(global_id)

    return jsonify({"ok": True})


@app.route("/api/fingerprint/sessions/<session_id>/links", methods=["GET"])
def fp_session_links(session_id: str):
    links = fingerprint_db.get_session_links(session_id)
    return jsonify(links)


@app.route("/api/fingerprint/sessions/<session_id>/link", methods=["POST"])
def fp_link_session_speaker(session_id: str):
    if not fingerprint_db.ready:
        return _fp_unavailable()
    data = request.get_json(silent=True) or {}
    speaker_key = (data.get("speaker_key") or "").strip()
    global_id   = (data.get("global_id") or "").strip()
    if not speaker_key or not global_id:
        return jsonify({"error": "speaker_key and global_id required"}), 400

    profile = fingerprint_db.get_global_speaker(global_id)
    if not profile:
        return jsonify({"error": "Global speaker not found"}), 404

    fingerprint_db.link_session_speaker(session_id, speaker_key, global_id)
    # Optionally apply the global name/color to this session speaker
    if data.get("apply_name"):
        storage.save_speaker_label(session_id, speaker_key, name=profile["name"], color=profile.get("color"))
        with _state_lock:
            if _state.get("session_id") == session_id:
                _state["speaker_labels"][speaker_key] = profile["name"]
        _push("speaker_label", {"session_id": session_id, "speaker_key": speaker_key,
                                 "name": profile["name"], "color": profile.get("color")})

    return jsonify({"ok": True})


@app.route("/api/fingerprint/sessions/<session_id>/link/<speaker_key>", methods=["DELETE"])
def fp_unlink_session_speaker(session_id: str, speaker_key: str):
    fingerprint_db.unlink_session_speaker(session_id, speaker_key)
    return jsonify({"ok": True})


@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    """Gracefully stop recording (if active), remove tray, then exit."""
    def _do_shutdown() -> None:
        global _tray
        # Stop any active recording / test first
        with _state_lock:
            sid         = _state["session_id"]
            capture     = _state["audio_capture"]
            test_cap    = _state["test_capture"]
            _state["is_recording"] = False
            _state["is_testing"]   = False
            _state["audio_capture"] = None
            _state["test_capture"]  = None
        if test_cap:
            test_cap.stop()
        if capture:
            capture.stop()
        _transcriber.stop()
        if sid:
            storage.end_session(sid)
        time.sleep(0.4)   # give the HTTP response time to reach the browser
        # Stop tray cleanly before exiting
        if _tray is not None:
            _tray.stop()
            _tray = None
        os._exit(0)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return jsonify({"ok": True})


# ── Update / self-update ──────────────────────────────────────────────────────

@app.route("/api/update/check")
def update_check():
    """Fetch from origin and report whether the remote main branch is ahead."""
    root = Path(__file__).parent
    try:
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(root), capture_output=True, text=True, timeout=20,
        )
        if fetch.returncode != 0:
            return jsonify({"error": fetch.stderr.strip() or "git fetch failed"}), 500

        count_r = subprocess.run(
            ["git", "rev-list", "HEAD..origin/main", "--count"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        if count_r.returncode != 0:
            return jsonify({"error": "Could not compare branches"}), 500

        count = int(count_r.stdout.strip() or "0")
        return jsonify({"up_to_date": count == 0, "commits_behind": count})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out — check your connection"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/update/apply", methods=["POST"])
def update_apply():
    """Pull latest changes then restart via the Start Menu shortcut."""
    root = Path(__file__).parent
    pull = subprocess.run(
        ["git", "pull", "origin", "main"],
        cwd=str(root), capture_output=True, text=True, timeout=120,
    )
    if pull.returncode != 0:
        return jsonify({"error": pull.stderr.strip() or "git pull failed"}), 500

    def _restart() -> None:
        global _tray
        # Stop any active recording / test first (mirrors _do_shutdown)
        with _state_lock:
            sid      = _state["session_id"]
            capture  = _state["audio_capture"]
            test_cap = _state["test_capture"]
            _state["is_recording"] = False
            _state["is_testing"]   = False
            _state["audio_capture"] = None
            _state["test_capture"]  = None
        if test_cap:
            test_cap.stop()
        if capture:
            capture.stop()
        _transcriber.stop()
        if sid:
            storage.end_session(sid)
        time.sleep(0.5)  # let the HTTP response reach the browser

        # Launch via Start Menu shortcut so the experience matches a normal start
        lnk_path = (
            Path(os.environ.get("APPDATA", ""))
            / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            / "Meeting Assistant.lnk"
        )
        if lnk_path.exists():
            os.startfile(str(lnk_path))
        else:
            # Fallback: run launch.bat directly
            bat = root / "launch.bat"
            if bat.exists():
                subprocess.Popen(
                    ["cmd.exe", "/c", str(bat)],
                    cwd=str(root),
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_CONSOLE,
                )

        if _tray is not None:
            _tray.stop()
            _tray = None
        os._exit(0)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global _tray

    port = int(os.getenv("PORT", 6969))
    url = f"http://127.0.0.1:{port}"

    _active_provider = settings.get("ai_provider", "anthropic")

    if config.needs_setup(_active_provider):
        log.warn("app", "First-run setup required — browser will open to configure API keys.")
    log.info("app", f"Meeting Assistant starting at {url}")

    # Start Flask in a daemon thread so the main thread is free for the tray
    flask_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, threaded=True),
        daemon=True,
    )
    flask_thread.start()

    # Wait for Flask to bind
    import urllib.request
    for _ in range(40):
        try:
            urllib.request.urlopen(f"{url}/api/status", timeout=1)
            break
        except Exception:
            time.sleep(0.15)

    # Open browser — go to settings page if keys are missing
    if config.needs_setup(_active_provider):
        webbrowser.open(f"{url}?settings=1")
    #else: webbrowser.open(url)

    # Try to start system tray on the main thread
    try:
        from tray import TRAY_AVAILABLE, MeetingTray
        if not TRAY_AVAILABLE:
            raise ImportError("pystray or Pillow not installed")

        def _state_snapshot() -> dict:
            with _state_lock:
                return {**_state}

        def _on_tray_quit(icon) -> None:
            # Same as /api/shutdown but called from the tray
            if icon:
                try:
                    icon.stop()
                except Exception:
                    pass
            with _state_lock:
                capture = _state["audio_capture"]
                test_cap = _state["test_capture"]
                sid = _state["session_id"]
                _state["is_recording"] = False
                _state["is_testing"] = False
                _state["audio_capture"] = None
                _state["test_capture"] = None
            if test_cap:
                test_cap.stop()
            if capture:
                capture.stop()
            _transcriber.stop()
            if sid:
                storage.end_session(sid)
            os._exit(0)

        _tray = MeetingTray(url, _state_snapshot, _on_tray_quit)
        log.info("tray", "System tray active — right-click for menu.")
        _tray.run()  # blocks until quit

    except ImportError:
        log.warn("tray", "pystray/Pillow not installed — running without system tray.")
        log.warn("tray", "Install with: pip install pystray Pillow")
        try:
            flask_thread.join()
        except KeyboardInterrupt:
            os._exit(0)


if __name__ == "__main__":
    main()
