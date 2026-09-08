"""The Free up space tool's HTTP surface.

Four routes, all under ``/api/storage``: price a plan, start it, read the job,
cancel it. The Storage card's data itself is ``/api/dashboard/storage`` in
core/dashboard_api.py, because it is dashboard data and shares that module's
read-only database access; the planner here reads the same report.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from core import dashboard_api as dashboard_api
from core import media as media
from core import media_compress as media_compress

bp = Blueprint("storage_api", __name__)


def _options(body: dict) -> tuple[dict, dict]:
    scope = body.get("scope") or {"mode": "all"}
    if not isinstance(scope, dict):
        scope = {"mode": "all"}
    options = {
        "audio": body.get("audio") or {},
        "video": body.get("video") or {},
        "backups": body.get("backups") or {},
        "orphans": body.get("orphans") or {},
    }
    return scope, options


@bp.route("/api/storage/plan", methods=["POST"])
def storage_plan():
    """What a run with these choices would do, priced, without doing it."""
    body = request.get_json(silent=True) or {}
    scope, options = _options(body)
    report = dashboard_api.storage_report()
    try:
        busy = set(media_compress._hooks["busy"]() or ())
    except Exception:
        busy = set()
    result = media_compress.plan(report, scope, options, busy=busy)
    result["capabilities"] = media_compress.capabilities()
    running = media_compress.current_job()
    result["running"] = bool(running and running.state == "running")
    return jsonify(result)


@bp.route("/api/storage/compress", methods=["GET"])
def storage_job():
    """The current (or last) job, with every item."""
    snap = media_compress.status()
    return jsonify({"job": snap, "capabilities": media_compress.capabilities()})


@bp.route("/api/storage/compress", methods=["POST"])
def storage_compress():
    """Start the run the plan described. 409 while one is running."""
    body = request.get_json(silent=True) or {}
    scope, options = _options(body)
    if not media.ffmpeg_bin():
        return jsonify({"error": "ffmpeg is not available, so nothing can be re-encoded"}), 503
    report = dashboard_api.storage_report()
    try:
        busy = set(media_compress._hooks["busy"]() or ())
    except Exception:
        busy = set()
    planned = media_compress.plan(report, scope, options, busy=busy)
    try:
        job = media_compress.start(planned["items"], planned["options"])
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    return jsonify({"job": job.snapshot(full=True), "plan": planned["totals"]})


@bp.route("/api/storage/compress/cancel", methods=["POST"])
def storage_cancel():
    stopped = media_compress.cancel()
    return jsonify({"ok": True, "cancelled": stopped, "job": media_compress.status()})
