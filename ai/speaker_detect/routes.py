"""HTTP routes for AI speaker detection (the meeting page and the history).

Kept out of app.py as a blueprint; app.py supplies everything app-owned
through ``Hooks`` (registered with ``register``), the way the Agent API gets
its context.

  POST /api/sessions/<sid>/speakers/identify      start a run (body: instructions,
                                                   optional autonomy, library_writes, depth,
                                                   and targets: the speaker keys to look at)
  GET  /api/sessions/<sid>/speakers/insights      the latest run, suggestions, history
  POST /api/sessions/<sid>/speakers/constraints   "Not Tom", "This is Tom", "Leave alone"
  GET  /api/sessions/<sid>/speakers/evidence/<obs_id>.jpg   the frame, tile boxed
  GET  /api/speaker-runs/<run_id>                 a run's state
  GET  /api/speaker-runs/<run_id>/frames          the frames it sent and what each read as
  GET  /api/speaker-runs/<run_id>/frames/<n>.jpg  one frame's preview
  POST /api/speaker-runs/<run_id>/cancel
  POST /api/speaker-runs/<run_id>/undo            undo everything the run applied
  POST /api/speaker-changes/<cid>/apply           accept a suggestion
  POST /api/speaker-changes/<cid>/dismiss
  POST /api/speaker-changes/<cid>/undo            (body: force)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from flask import Blueprint, Response, jsonify, request

from core import speaker_journal

bp = Blueprint("speaker_ai", __name__)


@dataclass
class Hooks:
    detector: object
    enabled: Callable[[], bool]
    session_exists: Callable[[str], bool]
    has_video: Callable[[str], bool]
    start: Callable[..., object]                 # (sid list, trigger, instructions, overrides)
    insights: Callable[[str], dict]
    undo_change: Callable[[int, bool], dict]
    undo_run: Callable[[str], dict]
    apply_change: Callable[[int], dict]
    dismiss_change: Callable[[int], dict]
    add_constraint: Callable[[str, str, dict, str | None], dict]
    evidence_jpeg: Callable[[str, int, bool], bytes | None]


_h: Hooks | None = None


def register(app, hooks: Hooks) -> None:
    global _h
    _h = hooks
    app.register_blueprint(bp)


def _run_json(run) -> dict:
    return run.as_dict() if hasattr(run, "as_dict") else dict(run)


@bp.route("/api/sessions/<session_id>/speakers/identify", methods=["POST"])
def identify(session_id: str):
    if not _h.session_exists(session_id):
        return jsonify({"error": "Meeting not found"}), 404
    if not _h.has_video(session_id):
        return jsonify({"error": "This meeting has no screen recording to read speakers from."}), 409
    data = request.get_json(silent=True) or {}
    overrides = {k: data[k] for k in ("autonomy", "library_writes", "depth")
                 if isinstance(data.get(k), str)}
    targets = [str(t).strip() for t in data.get("targets") or [] if str(t).strip()][:20] \
        if isinstance(data.get("targets"), list) else []
    if targets:
        overrides["targets"] = targets
    try:
        run = _h.start([session_id], "meeting_page", str(data.get("instructions") or ""),
                       overrides)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"ok": True, "run": _run_json(run)})


@bp.route("/api/sessions/<session_id>/speakers/insights", methods=["GET"])
def insights(session_id: str):
    if not _h.session_exists(session_id):
        return jsonify({"error": "Meeting not found"}), 404
    return jsonify(_h.insights(session_id))


@bp.route("/api/sessions/<session_id>/speakers/constraints", methods=["POST"])
def constraints(session_id: str):
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    key = (data.get("speaker_key") or "").strip()
    if kind not in ("is", "is_not", "protect") or not key:
        return jsonify({"error": "kind (is, is_not, protect) and speaker_key are required"}), 400
    value = (data.get("value") or "").strip() or None
    if kind != "protect" and not value:
        return jsonify({"error": "value (a name) is required"}), 400
    return jsonify(_h.add_constraint(session_id, kind, {"key": key}, value))


@bp.route("/api/sessions/<session_id>/speakers/evidence/<int:obs_id>.jpg", methods=["GET"])
def evidence(session_id: str, obs_id: int):
    jpeg = _h.evidence_jpeg(session_id, obs_id, request.args.get("full") == "1")
    if not jpeg:
        return jsonify({"error": "Frame unavailable"}), 404
    return Response(jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


@bp.route("/api/speaker-runs/<run_id>", methods=["GET"])
def get_run(run_id: str):
    run = _h.detector.get(run_id)
    if run is None:
        from core import storage
        saved = storage.get_speaker_ai_run(run_id)
        if not saved:
            return jsonify({"error": "Run not found"}), 404
        return jsonify({"run": saved})
    return jsonify({"run": _run_json(run)})


@bp.route("/api/speaker-runs/<run_id>/frames", methods=["GET"])
def run_frames(run_id: str):
    # null when the previews are gone (a later run or a restart); the
    # readings themselves stay as evidence on the changes.
    return jsonify({"frames": _h.detector.frames.list(run_id)})


@bp.route("/api/speaker-runs/<run_id>/frames/<int:fid>.jpg", methods=["GET"])
def run_frame(run_id: str, fid: int):
    jpeg = _h.detector.frames.jpeg(run_id, fid)
    if not jpeg:
        return jsonify({"error": "Frame preview unavailable"}), 404
    return Response(jpeg, mimetype="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@bp.route("/api/speaker-runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id: str):
    return jsonify({"ok": bool(_h.detector.cancel(run_id))})


@bp.route("/api/speaker-runs/<run_id>/undo", methods=["POST"])
def undo_run(run_id: str):
    try:
        return jsonify({"ok": True, **_h.undo_run(run_id)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@bp.route("/api/speaker-changes/<int:change_id>/apply", methods=["POST"])
def apply_change(change_id: int):
    try:
        return jsonify({"ok": True, **_h.apply_change(change_id)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@bp.route("/api/speaker-changes/<int:change_id>/dismiss", methods=["POST"])
def dismiss_change(change_id: int):
    try:
        return jsonify({"ok": True, **_h.dismiss_change(change_id)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@bp.route("/api/speaker-changes/<int:change_id>/undo", methods=["POST"])
def undo_change(change_id: int):
    data = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **_h.undo_change(change_id, bool(data.get("force")))})
    except speaker_journal.Conflict as c:
        return jsonify({"error": c.detail, "conflict": True}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 409
