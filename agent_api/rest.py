"""Agent REST API - the machine-facing interface to Meeting Assistant.

Mounted at ``/api/agent/v1``. Designed for AI agents (Claude Desktop, Claude
Code, Codex, custom scripts) rather than the browser UI:

- Self-describing: GET /            -> index with endpoint catalog
                   GET /docs        -> full markdown guide
                   GET /openapi.json-> OpenAPI 3.1 spec
- Read the library: meetings, transcripts (5 formats), summaries, notes,
  chapters, chat history, speakers, folders, media, video frames, audio clips.
- Search: hybrid keyword+semantic, plus raw substring scan.
- Organise: rename/move folders, move meetings in bulk, rename meetings.
- Speakers: the queue of meetings with unnamed speakers, an evidence pack per
  meeting (quotes, calendar attendees, voice-library matches, in-meeting
  proximity, frame moments), frames while a speaker talks, labelling (name,
  profile link, same-voice merge, noise, reset), per-line reattribution,
  voice-library profiles (detail, rename, confirmed merge, health) and the
  plan / confirm / apply bulk relabel.
- Operate: settings (schema'd + validated), logs, system info/stats/health,
  live-meeting tailing, opt-in recording control.

Conventions:
- All responses are JSON unless the endpoint serves media or an explicit
  text format (transcripts, docs).
- Errors: {"error": "...", ...} with a 4xx/5xx status, never HTML.
- Timestamps in query params accept seconds (90.5) or clock strings (1:30).
- The server binds to 127.0.0.1 only. An optional bearer token
  (settings key ``agent_api_token``) locks the API down further.
"""
from __future__ import annotations

import base64
import hmac
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, g, jsonify, request, send_file

from agent_api import API_VERSION
from agent_api import helpers
from agent_api import speakers as speaker_evidence
from agent_api.context import AgentContext
from ai import speaker_relabel
from capture_video import capture_live_frame, extract_frame, find_ffmpeg
from capture_video.ffmpeg_util import subprocess_no_window_flag
from core import attention, calendar_feed, config, log, paths, recording_request, settings, storage
from core import media as media
from ml import text_embeddings

bp = Blueprint("agent_api", __name__, url_prefix="/api/agent/v1")

_ctx: AgentContext | None = None
_PREFIX = "/api/agent/v1"

# Paths reachable without a bearer token (they expose no library data).
_OPEN_SUBPATHS = {"", "/", "/docs", "/openapi.json", "/system/health"}
# Paths whose requests are not echoed to the app log (polling noise).
_QUIET_SUBPATHS = {"/system/health", "/system/logs", "/live", "/docs",
                   "/openapi.json"}


def register_agent_api(app, ctx: AgentContext):
    """Attach the Agent API blueprint to the Flask app."""
    global _ctx
    _ctx = ctx
    app.register_blueprint(bp)
    log.info("agent", f"Agent API v{API_VERSION} mounted at {_PREFIX} "
                      f"(docs: {ctx.server_url}{_PREFIX}/docs)")
    return bp


# ── Request gate + logging ────────────────────────────────────────────────────

def _subpath() -> str:
    return request.path[len(_PREFIX):] or "/"


@bp.before_request
def _gate():
    g._agent_t0 = time.time()
    sub = _subpath()
    if not settings.get("agent_api_enabled", True):
        if sub == "/system/health":
            return None  # health always answers, reporting the disabled state
        return jsonify({
            "error": "The Agent API is disabled.",
            "how_to_enable": "Toggle it in Settings > Agent API inside the app, "
                             "or set \"agent_api_enabled\": true in settings.json.",
        }), 503
    token = (settings.get("agent_api_token") or "").strip()
    if token and sub not in _OPEN_SUBPATHS:
        auth = request.headers.get("Authorization", "")
        supplied = auth[7:].strip() if auth.startswith("Bearer ") else \
            (request.args.get("token") or "")
        if not hmac.compare_digest(supplied, token):
            return jsonify({
                "error": "Missing or invalid bearer token.",
                "hint": "Send 'Authorization: Bearer <token>' using the token "
                        "from Settings > Agent API.",
            }), 401
    return None


@bp.after_request
def _log_request(resp):
    sub = _subpath()
    if not any(sub.startswith(q) for q in _QUIET_SUBPATHS) and sub != "/":
        ms = int((time.time() - getattr(g, "_agent_t0", time.time())) * 1000)
        log.info("agent", f"{request.method} {sub} -> {resp.status_code} ({ms}ms)")
    return resp


# ── Small internals ───────────────────────────────────────────────────────────

def _err(message: str, status: int = 400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def _params() -> dict:
    """Merged view of query args and JSON body (body wins)."""
    merged = {k: v for k, v in request.args.items()}
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        merged.update(body)
    return merged


def _filters_input(source: dict) -> dict:
    """Shape folder/date/speaker params for app's shared scope resolver."""
    return {
        "folder": source.get("folder") or "",
        "include_subfolders": helpers.parse_bool(source.get("include_subfolders"), True),
        "speaker": source.get("speaker") or "",
        "within_days": source.get("within_days") or 0,
        "start_date": source.get("start_date") or "",
        "end_date": source.get("end_date") or "",
    }


def _as_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _folder_error(filters):
    payload = dict(filters["error"])
    payload.setdefault("hint", "Call GET /folders to list valid folders.")
    return jsonify(payload), 404 if "candidates" not in payload else 409


def _session_or_none(session_id: str):
    return storage.get_session(session_id)


def _meeting_item(meta: dict, labels: dict, notes_set: set[str],
                  summary_chars: int = 300) -> dict:
    entry = _ctx.describe_session(meta, labels, summary_chars=summary_chars)
    entry.update(helpers.meeting_flags(meta["session_id"]))
    entry["has_notes"] = meta["session_id"] in notes_set
    return entry


def _resolved_speakers(session_id: str) -> list[dict]:
    stats = storage.speaker_time_stats(session_id)
    for s in stats:
        if not s["name"]:
            s["name"] = _ctx.source_labels.get(s["speaker_key"], s["speaker_key"])
    return stats


_git_info_cache: dict | None = None


def _git_info() -> dict:
    global _git_info_cache
    if _git_info_cache is not None:
        return _git_info_cache
    root = Path(__file__).parent.parent
    info = {"commit": None, "commit_date": None, "subject": None, "branch": None}
    try:
        r = subprocess.run(["git", "log", "-1", "--format=%h%x1f%cI%x1f%s"],
                           cwd=str(root), capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            commit, date, subject = (r.stdout.strip().split("\x1f") + ["", ""])[:3]
            info.update({"commit": commit, "commit_date": date, "subject": subject})
        b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=str(root), capture_output=True, text=True, timeout=5)
        if b.returncode == 0:
            info["branch"] = b.stdout.strip()
    except Exception:
        pass
    _git_info_cache = info
    return info


# ── Discovery ─────────────────────────────────────────────────────────────────

def _mcp_connection_info() -> dict:
    """MCP server location plus ready-to-paste client config snippets."""
    script = str(Path(__file__).parent.parent / "mcp_server.py")
    python = sys.executable
    desktop = json.dumps({
        "mcpServers": {"meeting-assistant": {"command": python,
                                             "args": [script]}}
    }, indent=2)
    codex = (
        "[mcp_servers.meeting-assistant]\n"
        f"command = {json.dumps(python)}\n"
        f"args = [{json.dumps(script)}]"
    )
    claude_code = f'claude mcp add meeting-assistant -- "{python}" "{script}"'
    return {
        "server_script": script,
        "python": python,
        "selftest": f'"{python}" "{script}" --selftest',
        "hint": "Spawn the script over stdio from any MCP client; it proxies "
                "to this REST API (see /docs, section 'MCP setup').",
        "configs": {
            "claude_desktop": desktop,
            "claude_code": claude_code,
            "codex": codex,
        },
    }


@bp.route("")
@bp.route("/")
def index():
    """API index: who am I, where are the docs, what can you call."""
    status = _ctx.status_payload()
    return jsonify({
        "name": "Meeting Assistant Agent API",
        "api_version": API_VERSION,
        "base_url": f"{_ctx.server_url}{_PREFIX}",
        "description": (
            "Local REST interface to the user's Meeting Assistant: recorded "
            "meetings with diarized transcripts, AI summaries, notes, "
            "chapters, screen-recording frames, search, settings, and logs."
        ),
        "docs": {
            "guide": f"{_ctx.server_url}{_PREFIX}/docs",
            "openapi": f"{_ctx.server_url}{_PREFIX}/openapi.json",
        },
        "mcp": _mcp_connection_info(),
        "auth": {
            "token_required": bool((settings.get("agent_api_token") or "").strip()),
            "scheme": "Authorization: Bearer <token> (configure in Settings > Agent API)",
        },
        "live": {"recording": status.get("recording"),
                 "session_id": status.get("session_id")},
        "endpoints": {
            "discovery": ["GET /", "GET /docs", "GET /openapi.json"],
            "system": ["GET /system/health", "GET /system/info", "GET /system/status",
                       "GET /system/stats", "GET /system/logs", "GET /system/logs/files",
                       "GET /system/logs/files/{name}", "GET /system/changelog"],
            "meetings": ["GET /meetings", "GET /meetings/{id}", "PATCH /meetings/{id}",
                         "POST /meetings/move",
                         "GET /meetings/{id}/transcript", "GET /meetings/{id}/summary",
                         "GET /meetings/{id}/notes", "POST /meetings/{id}/notes/append",
                         "GET /meetings/{id}/chapters", "POST /meetings/{id}/chapters",
                         "GET /meetings/{id}/chat", "GET /meetings/{id}/speakers",
                         "GET /meetings/{id}/speakers/review",
                         "GET /meetings/{id}/speakers/{speaker_key}/frames",
                         "POST /meetings/{id}/speakers/label",
                         "POST /meetings/{id}/segments/{segment_id}/speaker",
                         "GET /meetings/{id}/media", "GET /meetings/{id}/frame",
                         "GET /meetings/{id}/frames", "GET /meetings/{id}/audio",
                         "GET /meetings/{id}/audio/clip",
                         "GET /meetings/{id}/screenshots",
                         "GET /meetings/{id}/screenshots/{name}",
                         "GET /meetings/{id}/export"],
            "search": ["GET|POST /search", "GET /search/text"],
            "folders": ["GET /folders", "POST /folders", "PATCH /folders/{id}",
                        "GET /folders/resolve"],
            "speakers": ["GET /speakers", "GET /speakers/queue",
                         "GET /speakers/{id_or_name}", "PATCH /speakers/{id}",
                         "POST /speakers/{id}/merge",
                         "GET /speakers/{id_or_name}/meetings",
                         "GET /speakers/library/health",
                         "POST /speakers/relabel/plan", "POST /speakers/relabel/apply",
                         "POST /speakers/relabel/cancel"],
            "chats": ["GET /chats", "GET /chats/{conversation_id}"],
            "settings": ["GET /settings", "GET /settings/schema", "PATCH /settings"],
            "live": ["GET /live"],
            "recording": ["POST /recording/start", "POST /recording/stop"],
        },
        "conventions": {
            "timestamps": "Params named t/start/end accept seconds (90.5) or "
                          "clock strings ('1:30', '01:02:03').",
            "filters": "Meeting-scoped endpoints share folder / "
                       "include_subfolders / speaker / within_days / "
                       "start_date / end_date filter params.",
            "errors": "Always JSON: {\"error\": \"...\"} with a 4xx/5xx status.",
        },
    })


# ── One-click MCP client setup ────────────────────────────────────────────────
# Writes/updates the MCP client config for this machine so the user doesn't
# have to copy/paste. Inputs are fixed (our python + mcp_server.py); nothing
# user-controlled reaches the files, and existing configs are merged, never
# clobbered (a .bak copy is written before any modification).

def _mcp_command() -> tuple[str, str]:
    return sys.executable, str(Path(__file__).parent.parent / "mcp_server.py")


def _claude_desktop_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / \
            "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _backup_file(path: Path) -> str | None:
    if not path.exists():
        return None
    bak = path.with_name(path.name + ".bak")
    try:
        shutil.copy2(path, bak)
        return str(bak)
    except OSError:
        return None


def _setup_claude_desktop() -> tuple[dict, int]:
    python, script = _mcp_command()
    path = _claude_desktop_config_path()
    app_installed = path.parent.exists()
    cfg: dict = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(cfg, dict):
                raise ValueError("top level is not an object")
        except (ValueError, OSError) as e:
            return {"ok": False,
                    "error": f"Existing config could not be parsed ({e}). "
                             "Fix it manually or paste the snippet instead.",
                    "path": str(path)}, 409
    servers = cfg.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return {"ok": False, "error": "Existing config has a non-object "
                                      "'mcpServers'; fix it manually.",
                "path": str(path)}, 409
    existed = "meeting-assistant" in servers
    servers["meeting-assistant"] = {"command": python, "args": [script]}
    backup = _backup_file(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"Could not write {path}: {e}"}, 500
    note = "Restart Claude Desktop to pick up the new server."
    if not app_installed:
        note = ("Claude Desktop did not look installed (its config folder was "
                "missing); the config was created anyway and will be used "
                "once the app is installed. " + note)
    return {"ok": True, "action": "updated" if existed else "created",
            "path": str(path), "backup": backup, "note": note}, 200


def _setup_claude_code() -> tuple[dict, int]:
    python, script = _mcp_command()
    claude = shutil.which("claude")
    if not claude:
        return {"ok": False,
                "error": "The 'claude' CLI was not found on PATH. Install "
                         "Claude Code first, or paste the command from the "
                         "snippet into any terminal."}, 404
    base = ["cmd", "/c", claude] if claude.lower().endswith((".cmd", ".bat")) \
        else [claude]

    def run(args: list) -> subprocess.CompletedProcess:
        return subprocess.run(base + args, capture_output=True, text=True,
                              timeout=60,
                              creationflags=subprocess_no_window_flag())

    add_args = ["mcp", "add", "--scope", "user", "meeting-assistant", "--",
                python, script]
    try:
        result = run(add_args)
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 and "already exists" in combined:
            # Refresh the registration so path changes take effect.
            run(["mcp", "remove", "--scope", "user", "meeting-assistant"])
            result = run(add_args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:400]
            return {"ok": False,
                    "error": f"'claude mcp add' failed: {detail}"}, 500
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "'claude mcp add' timed out."}, 504
    except OSError as e:
        return {"ok": False, "error": f"Could not run the claude CLI: {e}"}, 500
    return {"ok": True, "action": "registered",
            "path": "claude CLI (user scope)",
            "note": "Registered for your user across all projects. New "
                    "Claude Code sessions will see the meeting-assistant "
                    "tools."}, 200


def _setup_codex() -> tuple[dict, int]:
    python, script = _mcp_command()
    path = Path.home() / ".codex" / "config.toml"
    codex_installed = path.parent.exists()
    block = ("[mcp_servers.meeting-assistant]\n"
             f"command = {json.dumps(python)}\n"
             f"args = [{json.dumps(script)}]\n")
    existed = False
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": f"Could not read {path}: {e}"}, 500
        # Replace our section in place if present (every line up to the next
        # [section] header at line start), otherwise append. Pure text surgery
        # so the rest of the user's TOML (comments included) is preserved.
        # The line-anchored (?!\[) guard matters: value-side brackets like
        # args = ["..."] must not terminate the section match.
        import re as _re
        pattern = _re.compile(
            r"^\[mcp_servers\.(?:\"meeting-assistant\"|meeting-assistant)\]"
            r"[ \t]*\n(?:(?!\[).*\n?)*", _re.MULTILINE)
        if pattern.search(text):
            existed = True
            new_text = pattern.sub(block + "\n", text, count=1).rstrip() + "\n"
        else:
            sep = "" if (not text or text.endswith("\n\n")) else \
                ("\n" if text.endswith("\n") else "\n\n")
            new_text = text + sep + block
    else:
        new_text = block
    backup = _backup_file(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"Could not write {path}: {e}"}, 500
    note = "New Codex sessions will see the meeting-assistant tools."
    if not codex_installed:
        note = ("Codex did not look installed (~/.codex was missing); the "
                "config was created anyway and will be used once it is. " + note)
    return {"ok": True, "action": "updated" if existed else "created",
            "path": str(path), "backup": backup, "note": note}, 200


@bp.route("/setup/<client>", methods=["POST"])
def setup_client(client: str):
    """Write this machine's MCP client config for us (merge + backup)."""
    handlers = {"claude_desktop": _setup_claude_desktop,
                "claude_code": _setup_claude_code,
                "codex": _setup_codex}
    handler = handlers.get(client)
    if not handler:
        return _err(f"Unknown client '{client}'. "
                    f"Use one of: {', '.join(handlers)}.", 404)
    payload, status = handler()
    if payload.get("ok"):
        log.info("agent", f"MCP setup ran for {client}: "
                          f"{payload.get('action')} {payload.get('path')}")
    return jsonify(payload), status


@bp.route("/docs")
def docs():
    """The full agent guide as markdown (source: docs/AGENT_API.md)."""
    doc_path = Path(__file__).parent.parent / "docs" / "AGENT_API.md"
    try:
        text = doc_path.read_text(encoding="utf-8")
    except OSError:
        return _err("Documentation file missing (docs/AGENT_API.md).", 500)
    text = text.replace("http://localhost:6969", _ctx.server_url)
    return Response(text, mimetype="text/markdown; charset=utf-8")


@bp.route("/openapi.json")
def openapi():
    from agent_api.openapi import build_spec
    return jsonify(build_spec(_ctx.server_url))


# ── System ────────────────────────────────────────────────────────────────────

@bp.route("/system/health")
def system_health():
    enabled = bool(settings.get("agent_api_enabled", True))
    status = _ctx.status_payload() if enabled else {}
    return jsonify({
        "ok": True,
        "app": "Meeting Assistant",
        "api_version": API_VERSION,
        "agent_api_enabled": enabled,
        "time_utc": datetime.utcnow().isoformat(timespec="seconds"),
        "recording": status.get("recording", False),
    })


@bp.route("/system/status")
def system_status():
    payload = _ctx.status_payload()
    payload.update(_ctx.live_extras())
    return jsonify(payload)


@bp.route("/system/info")
def system_info():
    data_dir = paths.data_dir()
    db_path = paths.db_path()
    try:
        db_size = db_path.stat().st_size
    except OSError:
        db_size = None
    counts = storage.agent_counts()
    port = int(os.getenv("PORT", 6969))
    return jsonify({
        "app": "Meeting Assistant",
        "api_version": API_VERSION,
        "version": _git_info(),
        "platform": {
            "os": platform.system(),
            "os_version": platform.platform(),
            "python": sys.version.split()[0],
            "machine": platform.machine(),
        },
        "process": {
            "pid": os.getpid(),
            "started_at": datetime.utcfromtimestamp(_ctx.app_started_at)
                                  .isoformat(timespec="seconds"),
            "uptime_sec": round(time.time() - _ctx.app_started_at, 1),
        },
        "server": {"url": _ctx.server_url, "port": port, "binds": "127.0.0.1"},
        "data": {
            "dir": str(data_dir),
            "dir_overridden": paths.is_overridden(),
            "db_path": str(db_path),
            "db_size_bytes": db_size,
        },
        "library": counts,
        "models": _ctx.model_snapshot(),
        "ai": _ctx.ai_snapshot(),
        "semantic_search": {
            "ready": text_embeddings.is_ready(),
            "loading": text_embeddings.is_loading(),
            "embedded_sessions": counts.get("sessions_embedded"),
            "total_sessions": counts.get("sessions"),
        },
        "ffmpeg_available": find_ffmpeg() is not None,
        "agent_api": {
            "enabled": bool(settings.get("agent_api_enabled", True)),
            "token_required": bool((settings.get("agent_api_token") or "").strip()),
            "recording_control_allowed":
                bool(settings.get("agent_api_allow_recording_control", False)),
        },
    })


@bp.route("/system/stats")
def system_stats():
    analytics = storage.get_dashboard_analytics()
    data_dir = paths.data_dir()
    usage = {}
    for name in ("audio", "video", "screenshots", "attachments", "notes",
                 "backups", "logs", "audio_profiles", "tmp"):
        p = data_dir / name
        usage[name] = helpers.dir_size_bytes(p) if p.exists() else 0
    try:
        usage["database"] = paths.db_path().stat().st_size
    except OSError:
        usage["database"] = 0
    usage["total"] = sum(v for v in usage.values() if v)
    try:
        du = shutil.disk_usage(str(data_dir))
        disk = {"total_bytes": du.total, "free_bytes": du.free}
    except OSError:
        disk = {}
    return jsonify({
        "analytics": analytics,
        "counts": storage.agent_counts(),
        "storage_usage_bytes": usage,
        "disk": disk,
    })


@bp.route("/system/logs")
def system_logs():
    entries = log.recent(
        limit=max(1, min(1000, _as_int(request.args.get("limit"), 200))),
        level=request.args.get("level"),
        tag=request.args.get("tag"),
        contains=request.args.get("contains"),
        after_id=_as_int(request.args.get("after_id"), 0) or None,
    )
    return jsonify({
        "count": len(entries),
        "entries": entries,
        "note": "In-memory ring buffer since app start. Older history: "
                "GET /system/logs/files.",
    })


@bp.route("/system/logs/files")
def system_log_files():
    return jsonify({"files": log.log_files(),
                    "dir": str(paths.data_dir() / "logs")})


@bp.route("/system/logs/files/<name>")
def system_log_file(name: str):
    lines = max(1, min(5000, _as_int(request.args.get("lines"), 500)))
    content = log.read_log_file(name, lines=lines)
    if content is None:
        return _err(f"Unknown log file '{name}'. See GET /system/logs/files.", 404)
    return Response(content, mimetype="text/plain; charset=utf-8")


@bp.route("/system/changelog")
def system_changelog():
    limit = max(1, min(100, _as_int(request.args.get("limit"), 15)))
    try:
        entries = _ctx.changelog(limit)
    except Exception as e:
        return _err(f"Could not read changelog: {e}", 500)
    return jsonify({"count": len(entries), "entries": entries})


# ── Folders ───────────────────────────────────────────────────────────────────

@bp.route("/folders")
def folders_tree():
    return jsonify({"folders": storage.folder_tree()})


@bp.route("/folders", methods=["POST"])
def folders_create():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return _err("A folder 'name' is required.")
    parent_id = body.get("parent_id")
    parent_spec = (body.get("parent") or "").strip()
    if parent_spec and not parent_id:
        filters = _ctx.scope_filters({"folder": parent_spec})
        if filters["error"]:
            return _folder_error(filters)
        parent_id = filters["folder_ids"][0]
    fid = storage.create_folder(name, parent_id=parent_id)
    entry = next((f for f in storage.folder_tree() if f["id"] == fid), None)
    return jsonify({"ok": True, "folder": entry or {"id": fid, "name": name}}), 201


@bp.route("/folders/resolve")
def folders_resolve():
    spec = (request.args.get("q") or request.args.get("name") or "").strip()
    if not spec:
        return _err("Pass ?q=<folder id, name, or path>.")
    filters = _ctx.scope_filters({
        "folder": spec,
        "include_subfolders": helpers.parse_bool(
            request.args.get("include_subfolders"), True),
    })
    if filters["error"]:
        payload = dict(filters["error"])
        payload["resolved"] = False
        return jsonify(payload), 200  # disambiguation info, not a failure
    fid = filters["folder_ids"][0]
    entry = next((f for f in filters["folders"] if f["id"] == fid), None)
    return jsonify({
        "resolved": True,
        "folder": entry,
        "folder_ids_in_scope": filters["folder_ids"],
    })


# ── Meetings: browse + bundle ────────────────────────────────────────────────

@bp.route("/meetings")
def meetings_list():
    args = _params()
    filters = _ctx.scope_filters(_filters_input(args))
    if filters["error"]:
        return _folder_error(filters)
    all_ids = storage.list_session_ids(
        folder_ids=filters["folder_ids"], start=filters["start"],
        end=filters["end"], speaker=filters["speaker"],
    )
    limit = max(1, min(500, _as_int(args.get("limit"), 50)))
    offset = max(0, _as_int(args.get("offset"), 0))
    page_ids = all_ids[offset:offset + limit]

    labels = _ctx.folder_labels(filters["folders"])
    metas = storage.get_sessions_meta(page_ids)
    notes_set = storage.sessions_have_notes(page_ids)
    meetings = [_meeting_item(metas[sid], labels, notes_set)
                for sid in page_ids if sid in metas]
    return jsonify({
        "total": len(all_ids),
        "offset": offset,
        "limit": limit,
        "count": len(meetings),
        "scope": filters["desc"].removeprefix(" in ") or "all meetings",
        "meetings": meetings,
    })


_BUNDLE_DEFAULT = ("summary", "chapters", "speakers", "notes", "media")
_BUNDLE_ALL = ("summary", "chapters", "speakers", "notes", "media",
               "transcript", "chat", "summary_history", "calendar", "attention")


@bp.route("/meetings/<session_id>")
def meeting_detail(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    raw_include = (request.args.get("include") or "").strip()
    if raw_include == "all":
        include = set(_BUNDLE_ALL)
    elif raw_include:
        include = {p.strip() for p in raw_include.split(",") if p.strip()}
    else:
        include = set(_BUNDLE_DEFAULT)

    labels = _ctx.folder_labels()
    folder = labels.get(sess.get("folder_id")) if sess.get("folder_id") else None
    segs = sess.get("segments", [])
    duration = max((s.get("end_time") or 0.0) for s in segs) if segs else 0.0
    base = f"{_ctx.server_url}{_PREFIX}/meetings/{session_id}"

    out = {
        "session_id": sess["id"],
        "title": sess.get("title"),
        "started_at": sess.get("started_at"),
        "ended_at": sess.get("ended_at"),
        "folder_id": sess.get("folder_id"),
        "folder_path": folder["path"] if folder else None,
        "duration_sec": round(duration, 1),
        "duration_hms": helpers.fmt_mmss(duration),
        "segment_count": len(segs),
        **helpers.meeting_flags(session_id),
        "included": sorted(include),
        "links": {
            "transcript": f"{base}/transcript",
            "export": f"{base}/export",
            "frame": f"{base}/frame?t=<seconds>",
            "media": f"{base}/media",
        },
    }
    if "summary" in include:
        out["summary"] = sess.get("summary", "")
    if "summary_history" in include:
        out["summary_history"] = storage.get_summary_history(session_id)
    if "chapters" in include:
        out["chapters"] = sess.get("chapters", [])
    if "speakers" in include:
        out["speakers"] = _resolved_speakers(session_id)
    if "notes" in include:
        notes = sess.get("notes")
        out["notes"] = {
            "markdown": helpers.delta_to_markdown(notes.get("delta")) if notes else "",
            "updated_at": notes.get("updated_at") if notes else None,
        }
    if "media" in include:
        out["video_offset_sec"] = settings.get_video_offset(session_id)
    if "transcript" in include:
        out["transcript"] = helpers.transcript_rows(
            segs, sess.get("speaker_labels"), _ctx.source_labels)
    if "chat" in include:
        out["chat_messages"] = _parse_chat_rows(sess.get("chat_messages", []))
    if "calendar" in include:
        rows = _speaker_rows(session_id, sess)
        out["calendar"] = speaker_evidence.calendar_context(session_id, rows)
    if "attention" in include:
        out["attention"] = storage.get_session_attention(session_id)
    return jsonify(out)


@bp.route("/meetings/<session_id>", methods=["PATCH"])
def meeting_update(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    body = request.get_json(silent=True) or {}
    changed = {}

    if "title" in body:
        title = (body.get("title") or "").strip()
        if not title:
            return _err("Title must be a non-empty string.")
        storage.update_session_title(session_id, title, user_set=True)
        _ctx.push_event("session_title", {"session_id": session_id, "title": title})
        changed["title"] = title

    if "folder" in body:
        spec = body.get("folder")
        if spec in (None, ""):
            storage.set_session_folder(session_id, None)
            changed["folder_id"] = None
        else:
            filters = _ctx.scope_filters({"folder": str(spec),
                                          "include_subfolders": False})
            if filters["error"]:
                return _folder_error(filters)
            fid = filters["folder_ids"][0]
            storage.set_session_folder(session_id, fid)
            changed["folder_id"] = fid
            changed["folder_path"] = filters["label"]

    if not changed:
        return _err("Nothing to update. Supported fields: title, folder "
                    "(folder id, name, path, or null to unfile).")
    return jsonify({"ok": True, "session_id": session_id, "changed": changed})


# ── Meetings: content ─────────────────────────────────────────────────────────

@bp.route("/meetings/<session_id>/transcript")
def meeting_transcript(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    args = request.args
    fmt = (args.get("format") or "json").lower()
    if fmt not in helpers.TRANSCRIPT_FORMATS:
        return _err(f"Unknown format '{fmt}'. "
                    f"Use one of: {', '.join(helpers.TRANSCRIPT_FORMATS)}.")

    segs = sess.get("segments", [])
    total = len(segs)
    labels = sess.get("speaker_labels") or {}

    start = helpers.parse_timestamp(args.get("start"))
    end = helpers.parse_timestamp(args.get("end"))
    if start is not None or end is not None:
        s0, e0 = start or 0.0, end if end is not None else float("inf")
        segs = [s for s in segs
                if (s.get("end_time") or 0.0) >= s0
                and (s.get("start_time") or 0.0) <= e0]
    after_id = _as_int(args.get("after_segment_id"), 0)
    if after_id:
        segs = [s for s in segs if (s.get("id") or 0) > after_id]
    want_speaker = (args.get("speaker") or "").strip().lower()
    if want_speaker:
        # Match EITHER the resolved display name ("Alex Chen") OR the raw
        # diarizer key stored on each segment ("Speaker 5"). A caller checking
        # one diarizer cluster asks for a transcript sample by speaker_key,
        # but several keys often share one resolved name (e.g. keys 4/5/8 all
        # became "Other participant"), so a resolved-name-only filter returned
        # nothing and the panel showed "No transcript sample available".
        def _speaker_match(s):
            resolved = helpers.resolve_speaker(
                s, labels, _ctx.source_labels).lower()
            src = str(s.get("source") or "").strip().lower()
            return want_speaker in resolved or want_speaker == src
        segs = [s for s in segs if _speaker_match(s)]

    offset = max(0, _as_int(args.get("offset"), 0))
    limit = _as_int(args.get("limit"), 0)
    filtered_total = len(segs)
    if offset:
        segs = segs[offset:]
    if limit > 0:
        segs = segs[:limit]

    timestamps = helpers.parse_bool(args.get("timestamps"), True)
    speakers = helpers.parse_bool(args.get("speakers"), True)

    if fmt == "json":
        return jsonify({
            "session_id": session_id,
            "title": sess.get("title"),
            "format": "json",
            "total_segments": total,
            "matched_segments": filtered_total,
            "returned": len(segs),
            "offset": offset,
            "has_more": offset + len(segs) < filtered_total,
            "segments": helpers.transcript_rows(segs, labels, _ctx.source_labels),
        })

    if fmt == "text":
        content = helpers.transcript_text(segs, labels, _ctx.source_labels,
                                          timestamps=timestamps, speakers=speakers)
        mime = "text/plain; charset=utf-8"
    elif fmt == "markdown":
        chapters = sess.get("chapters") if helpers.parse_bool(
            args.get("chapters"), True) else None
        content = helpers.transcript_markdown(
            segs, labels, _ctx.source_labels, chapters=chapters,
            title=sess.get("title") or "", timestamps=timestamps)
        mime = "text/markdown; charset=utf-8"
    elif fmt == "srt":
        content = helpers.transcript_srt(segs, labels, _ctx.source_labels,
                                         speakers=speakers)
        mime = "text/plain; charset=utf-8"
    else:  # vtt
        content = helpers.transcript_vtt(segs, labels, _ctx.source_labels,
                                         speakers=speakers)
        mime = "text/vtt; charset=utf-8"

    if helpers.parse_bool(args.get("envelope"), False):
        return jsonify({"session_id": session_id, "format": fmt,
                        "returned": len(segs), "content": content})
    resp = Response(content, mimetype=mime)
    resp.headers["X-Session-Title"] = (sess.get("title") or "")[:200]
    return resp


@bp.route("/meetings/<session_id>/summary")
def meeting_summary(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    history = storage.get_summary_history(session_id)
    return jsonify({
        "session_id": session_id,
        "title": sess.get("title"),
        "summary": sess.get("summary", ""),
        "updated_at": history[-1]["created_at"] if history else None,
        "revisions": len(history),
    })


@bp.route("/meetings/<session_id>/notes")
def meeting_notes(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    notes = sess.get("notes")
    delta = notes.get("delta") if notes else None
    attach_dir = paths.data_dir() / "notes" / session_id
    attachments = [
        {**f, "url": f"{_ctx.server_url}/api/sessions/{session_id}"
                     f"/notes/attachments/{f['name']}"}
        for f in helpers.list_dir_files(attach_dir)
    ] if attach_dir.exists() else []
    payload = {
        "session_id": session_id,
        "has_notes": bool(delta),
        "markdown": helpers.delta_to_markdown(delta),
        "text": helpers.delta_to_text(delta).strip(),
        "updated_at": notes.get("updated_at") if notes else None,
        "attachments": attachments,
    }
    if helpers.parse_bool(request.args.get("raw"), False):
        payload["delta"] = delta
    return jsonify(payload)


@bp.route("/meetings/<session_id>/notes/append", methods=["POST"])
def meeting_notes_append(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return _err("A non-empty 'text' field is required.")
    if len(text) > 50_000:
        return _err("Note text too large (max 50,000 characters).", 413)
    heading = (body.get("heading") or "").strip() or None

    notes = sess.get("notes") or {}
    existing = notes.get("delta")
    ops = []
    if isinstance(existing, dict):
        ops = list(existing.get("ops") or [])
    elif isinstance(existing, list):
        ops = list(existing)
    ops.extend(helpers.build_note_append_ops(text, heading))
    storage.set_session_notes(session_id, {"ops": ops})
    _ctx.push_event("notes_updated", {"session_id": session_id})
    return jsonify({"ok": True, "session_id": session_id,
                    "appended_chars": len(text), "heading": heading})


@bp.route("/meetings/<session_id>/chapters")
def meeting_chapters(session_id: str):
    if not _session_or_none(session_id):
        return _err(f"Meeting '{session_id}' not found.", 404)
    return jsonify({"session_id": session_id,
                    "chapters": storage.get_chapters(session_id)})


@bp.route("/meetings/<session_id>/chapters", methods=["POST"])
def meeting_chapters_add(session_id: str):
    if not _session_or_none(session_id):
        return _err(f"Meeting '{session_id}' not found.", 404)
    body = request.get_json(silent=True) or {}
    title = (body.get("title") or "").strip()
    if not title:
        return _err("A chapter 'title' is required.")
    start = helpers.parse_timestamp(body.get("start_time"))
    if start is None:
        return _err("A valid 'start_time' is required (seconds or 'M:SS').")
    chapter = storage.add_chapter(session_id, start, title)
    _ctx.push_event("chapters_updated", {
        "session_id": session_id,
        "chapters": storage.get_chapters(session_id),
    })
    return jsonify({"ok": True, "chapter": chapter}), 201


def _parse_chat_rows(rows: list[dict]) -> list[dict]:
    out = []
    for m in rows:
        entry = {"role": m.get("role"), "content": m.get("content"),
                 "created_at": m.get("created_at")}
        for key in ("attachments", "tool_calls"):
            raw = m.get(key)
            if raw:
                try:
                    entry[key] = json.loads(raw)
                except (TypeError, ValueError):
                    entry[key] = raw
        out.append(entry)
    return out


@bp.route("/meetings/<session_id>/chat")
def meeting_chat(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    messages = _parse_chat_rows(sess.get("chat_messages", []))
    return jsonify({"session_id": session_id, "count": len(messages),
                    "messages": messages})


# ── Meetings: media ───────────────────────────────────────────────────────────

@bp.route("/meetings/<session_id>/media")
def meeting_media(session_id: str):
    if not _session_or_none(session_id):
        return _err(f"Meeting '{session_id}' not found.", 404)
    audio = media.audio_path(session_id)
    mp4, live = _frame_sources(session_id)
    base = f"{_ctx.server_url}{_PREFIX}/meetings/{session_id}"
    shots_dir = paths.screenshots_dir() / session_id
    notes_dir = paths.data_dir() / "notes" / session_id

    live_recording = bool(live and live.get("live_video_path"))
    video = None
    if mp4.exists():
        video = {
            **(helpers.video_info(mp4, find_ffmpeg(),
                                  subprocess_no_window_flag()) or {}),
            "live": live_recording,
            "video_offset_sec": settings.get_video_offset(session_id),
            "offset_note": "Video starts at this many seconds into the "
                           "meeting timeline. GET /frame handles the "
                           "conversion automatically.",
            "frame_url": f"{base}/frame?t=<seconds>",
        }
    elif live_recording:
        video = {
            "live": True,
            "elapsed_sec": live.get("elapsed_sec"),
            "video_offset_sec": settings.get_video_offset(session_id),
            "note": "Screen recording in progress: frames are extractable "
                    "right now (t accepts 'now'); the final MP4 appears when "
                    "the recording stops.",
            "frame_url": f"{base}/frame?t=now",
        }
    return jsonify({
        "session_id": session_id,
        "audio": ({**(media.audio_info(audio, ffmpeg=find_ffmpeg()) or {}),
                   "url": f"{base}/audio"}
                  if audio is not None else None),
        "video": video,
        "screenshots": [
            {**f, "url": f"{base}/screenshots/{f['name']}"}
            for f in helpers.list_dir_files(shots_dir)
        ] if shots_dir.exists() else [],
        "note_attachments": [
            {**f, "url": f"{_ctx.server_url}/api/sessions/{session_id}"
                         f"/notes/attachments/{f['name']}"}
            for f in helpers.list_dir_files(notes_dir)
        ] if notes_dir.exists() else [],
    })


_NOW_WORDS = ("now", "live", "current")

# When live-file extraction fails for a moment this close to the live head,
# the frames simply haven't been flushed to disk yet; a screenshot of the
# recorded display is the honest answer. Older moments never silently degrade
# to a current screenshot.
_LIVE_HEAD_WINDOW_SEC = 12.0


def _live_media_for(session_id: str) -> dict | None:
    """Live media info, but only when ``session_id`` is the session being
    recorded right now (a live file never answers for a different meeting)."""
    lm = _ctx.live_media()
    if lm.get("recording") and lm.get("session_id") == session_id:
        return lm
    return None


def _frame_sources(session_id: str) -> tuple[Path, dict | None]:
    mp4 = paths.video_dir() / f"{session_id}.mp4"
    return mp4, _live_media_for(session_id)


def _frame_at(session_id: str, t: float, width: int,
              raw: bool) -> tuple[bytes | None, float, str]:
    """Extract a frame at meeting-timeline second ``t``.

    Returns (jpeg, video_t, source) where source is:
      - "video":       the finished session MP4
      - "live_file":   the in-progress fragmented MP4 (meeting still recording)
      - "live_screen": a screenshot of the recorded display, used only when
                       the requested moment is at the live head and the file
                       hasn't flushed it yet
    """
    mp4, live = _frame_sources(session_id)
    offset = 0.0 if raw else settings.get_video_offset(session_id)
    video_t = max(0.0, t - offset)

    if live and live.get("live_video_path"):
        if not raw and mp4.exists() and t < offset:
            # Resumed session: the moment predates the current live file, so
            # the earlier finished video is the right source for it.
            return extract_frame(str(mp4), t, max_width=width), t, "video"
        jpeg = extract_frame(live["live_video_path"], video_t, max_width=width)
        if jpeg:
            return jpeg, video_t, "live_file"
        elapsed = live.get("elapsed_sec")
        if elapsed is None or t >= elapsed - _LIVE_HEAD_WINDOW_SEC:
            jpeg = capture_live_frame(
                display_index=int(settings.get("screen_display", 0)),
                max_width=width)
            if jpeg:
                return jpeg, video_t, "live_screen"
        return None, video_t, "live_file"

    return extract_frame(str(mp4), video_t, max_width=width), video_t, "video"


def _frame_availability(session_id: str):
    """(mp4, live) when frames are extractable, else a 404 response."""
    mp4, live = _frame_sources(session_id)
    if mp4.exists() or (live and live.get("live_video_path")):
        return mp4, live, None
    flags = helpers.meeting_flags(session_id)
    if live:  # recording now, but screen recording is not producing a file
        return mp4, live, _err(
            "This meeting is recording, but screen recording is not running, "
            "so there is no video to extract frames from.", 404,
            has_audio=flags["has_audio"])
    return mp4, live, _err("No screen recording exists for this meeting.",
                           404, has_audio=flags["has_audio"])


def _parse_frame_t(raw_value, live: dict | None) -> tuple[float | None, str | None]:
    """Parse a frame timestamp, allowing 'now' during a live recording.

    Returns (t, error_message)."""
    if isinstance(raw_value, str) and raw_value.strip().lower() in _NOW_WORDS:
        if not live:
            return None, ("'now' is only valid while this meeting is being "
                          "recorded. Pass a numeric timestamp instead.")
        elapsed = live.get("elapsed_sec")
        if elapsed is None:
            elapsed = settings.get_video_offset(
                live.get("session_id") or "") + 1.0
        return max(0.0, float(elapsed)), None
    t = helpers.parse_timestamp(raw_value)
    if t is None:
        return None, ("Pass ?t=<seconds or 'M:SS'> for the meeting-timeline "
                      "moment you want to see ('now' works during a live "
                      "recording).")
    return t, None


@bp.route("/meetings/<session_id>/frame")
def meeting_frame(session_id: str):
    mp4, live, unavailable = _frame_availability(session_id)
    if unavailable:
        return unavailable
    args = request.args
    t, terr = _parse_frame_t(args.get("t") or args.get("ts")
                             or args.get("timestamp"), live)
    if terr:
        return _err(terr)
    width = max(160, min(1920, _as_int(args.get("width"), 1280)))
    raw = helpers.parse_bool(args.get("raw"), False)
    jpeg, video_t, source = _frame_at(session_id, t, width, raw)
    if not jpeg:
        if live and live.get("live_video_path"):
            return _err(
                "Could not extract a frame at that time from the live "
                "recording.", 422, requested_t=round(t, 2),
                video_t=round(video_t, 2),
                elapsed_sec=live.get("elapsed_sec"),
                hint="Moments within the last couple of seconds may not be "
                     "flushed to disk yet; try t=now or a slightly earlier "
                     "timestamp.",
            )
        info = helpers.video_info(mp4, find_ffmpeg(), subprocess_no_window_flag()) or {}
        return _err(
            "Could not extract a frame at that time.", 422,
            requested_t=round(t, 2), video_t=round(video_t, 2),
            video_duration_sec=info.get("duration_sec"),
            hint="The timestamp may be beyond the end of the recording, or "
                 "ffmpeg may be unavailable.",
        )
    fmt = (args.get("format") or "jpeg").lower()
    if fmt in ("base64", "json", "data_uri"):
        b64 = base64.b64encode(jpeg).decode()
        payload = {
            "session_id": session_id, "t": round(t, 2),
            "video_t": round(video_t, 2), "source": source,
            "width": width, "mime": "image/jpeg", "bytes": len(jpeg),
        }
        if source == "live_screen":
            payload["note"] = ("Current screen capture; the live recording "
                               "file had not flushed this moment yet.")
        if fmt == "data_uri":
            payload["data_uri"] = f"data:image/jpeg;base64,{b64}"
        else:
            payload["jpeg_base64"] = b64
        return jsonify(payload)
    resp = Response(jpeg, mimetype="image/jpeg")
    resp.headers["X-Meeting-T"] = f"{t:.2f}"
    resp.headers["X-Video-T"] = f"{video_t:.2f}"
    resp.headers["X-Frame-Source"] = source
    return resp


@bp.route("/meetings/<session_id>/frames")
def meeting_frames(session_id: str):
    """Batch frame extraction: explicit timestamps or an evenly spaced sweep.

    Works on finished recordings and, for the actively-recording meeting, on
    the live file (a sweep then covers recording start through the live head).
    """
    mp4, live, unavailable = _frame_availability(session_id)
    if unavailable:
        return unavailable
    args = request.args
    width = max(160, min(1280, _as_int(args.get("width"), 640)))
    raw = helpers.parse_bool(args.get("raw"), False)

    stamps: list[float] = []
    if args.get("at"):
        for part in str(args.get("at")).split(","):
            ts, terr = _parse_frame_t(part.strip(), live)
            if not terr and ts is not None:
                stamps.append(ts)
        if not stamps:
            return _err("No parseable timestamps in 'at'. "
                        "Example: at=30,1:30,240 ('now' allowed while "
                        "recording).")
    else:
        count = max(2, min(12, _as_int(args.get("count"), 6)))
        offset = 0.0 if raw else settings.get_video_offset(session_id)
        span_start = helpers.parse_timestamp(args.get("start")) or offset
        span_end, terr = (None, None)
        if args.get("end"):
            span_end, terr = _parse_frame_t(args.get("end"), live)
            if terr:
                return _err(terr)
        if span_end is None:
            if live and live.get("live_video_path"):
                # Sweep up to just behind the live head (the newest second or
                # two may not be flushed to the frag file yet).
                elapsed = live.get("elapsed_sec")
                span_end = max(span_start + 1, (elapsed or span_start + 61) - 2)
            else:
                info = helpers.video_info(mp4, find_ffmpeg(),
                                          subprocess_no_window_flag()) or {}
                duration = info.get("duration_sec")
                span_end = (duration + offset - 0.5) if duration \
                    else span_start + 60
        if span_end <= span_start:
            return _err("'end' must be after 'start'.")
        step = (span_end - span_start) / (count - 1)
        stamps = [round(span_start + i * step, 2) for i in range(count)]

    stamps = stamps[:12]
    frames = []
    for t in stamps:
        jpeg, video_t, source = _frame_at(session_id, t, width, raw)
        frames.append({
            "t": t, "video_t": round(video_t, 2), "source": source,
            "jpeg_base64": base64.b64encode(jpeg).decode() if jpeg else None,
            "ok": bool(jpeg),
        })
    return jsonify({"session_id": session_id, "width": width,
                    "live": bool(live and live.get("live_video_path")),
                    "count": len(frames), "frames": frames})


@bp.route("/meetings/<session_id>/audio")
def meeting_audio(session_id: str):
    audio = media.audio_path(session_id)
    if audio is None:
        return _err("No audio recording exists for this meeting.", 404)
    return send_file(str(audio), mimetype=media.audio_mime(audio), conditional=True)


@bp.route("/meetings/<session_id>/audio/clip")
def meeting_audio_clip(session_id: str):
    if not media.has_audio(session_id):
        return _err("No audio recording exists for this meeting.", 404)
    # Clips are cut from PCM: the recorder's WAV, or a decode of an Opus session.
    wav = media.pcm_wav_path(session_id)
    if wav is None:
        return _err("The recording's audio could not be decoded for clipping.", 500)
    args = request.args
    start = helpers.parse_timestamp(args.get("start"))
    if start is None:
        return _err("Pass ?start=<seconds or 'M:SS'> (and optionally end=).")
    end = helpers.parse_timestamp(args.get("end"))
    if end is None:
        end = start + 60.0
    if end <= start:
        return _err("'end' must be after 'start'.")
    if end - start > 900:
        return _err("Clip too long: maximum 900 seconds (15 minutes).", 413)
    clip = helpers.extract_wav_clip(wav, start, end)
    if clip is None:
        return _err("Could not read the WAV file.", 500)

    fmt = (args.get("format") or "wav").lower()
    fname = f"{session_id[:8]}_{int(start)}s-{int(end)}s"
    if fmt == "mp3":
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return _err("MP3 export needs ffmpeg, which was not found. "
                        "Use format=wav instead.", 501)
        try:
            result = subprocess.run(
                [ffmpeg, "-f", "wav", "-i", "pipe:0", "-f", "mp3",
                 "-b:a", "64k", "pipe:1"],
                input=clip, capture_output=True, timeout=60,
                creationflags=subprocess_no_window_flag(),
            )
            if result.returncode != 0 or not result.stdout:
                return _err("ffmpeg could not encode MP3 "
                            "(the bundled build may lack an MP3 encoder). "
                            "Use format=wav instead.", 501)
            clip, mime, fname = result.stdout, "audio/mpeg", fname + ".mp3"
        except Exception as e:
            return _err(f"MP3 encode failed: {e}. Use format=wav instead.", 500)
    else:
        mime, fname = "audio/wav", fname + ".wav"

    return send_file(io.BytesIO(clip), mimetype=mime,
                     as_attachment=helpers.parse_bool(args.get("download"), False),
                     download_name=fname)


@bp.route("/meetings/<session_id>/screenshots")
def meeting_screenshots(session_id: str):
    shots_dir = paths.screenshots_dir() / session_id
    base = f"{_ctx.server_url}{_PREFIX}/meetings/{session_id}"
    files = helpers.list_dir_files(shots_dir) if shots_dir.exists() else []
    return jsonify({"session_id": session_id, "count": len(files),
                    "screenshots": [{**f, "url": f"{base}/screenshots/{f['name']}"}
                                    for f in files]})


@bp.route("/meetings/<session_id>/screenshots/<name>")
def meeting_screenshot_file(session_id: str, name: str):
    safe = Path(name).name
    path = paths.screenshots_dir() / session_id / safe
    if not path.exists():
        return _err("Screenshot not found.", 404)
    return send_file(str(path), mimetype="image/jpeg")


@bp.route("/meetings/<session_id>/export")
def meeting_export(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    fmt = (request.args.get("format") or "markdown").lower()
    labels = _ctx.folder_labels()
    folder = labels.get(sess.get("folder_id")) if sess.get("folder_id") else None
    folder_path = folder["path"] if folder else None

    if fmt == "json":
        pkg = storage.export_session_data(session_id)
        pkg["folder_path"] = folder_path
        return jsonify(pkg)
    if fmt != "markdown":
        return _err("Unknown format. Use 'markdown' (default) or 'json'.")

    md = helpers.export_markdown(sess, _ctx.source_labels, folder_path=folder_path)
    if helpers.parse_bool(request.args.get("save_to_file"), False):
        out_dir = paths.tmp_dir() / "agent_exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_title = "".join(c if c.isalnum() or c in " -_" else "_"
                             for c in (sess.get("title") or "meeting"))[:60].strip()
        out_path = out_dir / f"{safe_title or 'meeting'}_{session_id[:8]}.md"
        out_path.write_text(md, encoding="utf-8")
        return jsonify({"ok": True, "path": str(out_path),
                        "bytes": len(md.encode("utf-8"))})
    return Response(md, mimetype="text/markdown; charset=utf-8")


# ── Search ────────────────────────────────────────────────────────────────────

def _enrich_results(ordered: list[dict], folders: list[dict]) -> list[dict]:
    """Attach shared session metadata to search results (any mode)."""
    labels = _ctx.folder_labels(folders)
    ids = [r["session_id"] for r in ordered]
    metas = storage.get_sessions_meta(ids)
    notes_set = storage.sessions_have_notes(ids)
    out = []
    for r in ordered:
        meta = metas.get(r["session_id"])
        if not meta:
            continue
        entry = _meeting_item(meta, labels, notes_set, summary_chars=300)
        for key in ("matches", "semantic_score", "keyword_rank", "score"):
            if key in r:
                entry[key] = r[key]
        out.append(entry)
    return out


def _keyword_hits(q: str, match: str, limit: int,
                  scoped: "set[str] | None", max_snippets: int) -> list[dict]:
    results = storage.search_sessions(
        q, limit=limit, match=match,
        session_ids=list(scoped) if scoped is not None else None,
        max_snippets=max_snippets)
    merged: dict[str, dict] = {r["session_id"]: r for r in results}
    for sr in storage.search_speakers(q, limit=limit):
        sid = sr["session_id"]
        if scoped is not None and sid not in scoped:
            continue
        if sid in merged:
            merged[sid]["matches"] = sr["matches"] + merged[sid]["matches"]
        else:
            merged[sid] = sr
    out = []
    for pos, r in enumerate(merged.values()):
        out.append({"session_id": r["session_id"],
                    "matches": r.get("matches", []), "keyword_rank": pos + 1})
    return out


def _semantic_hits(q: str, limit: int, min_score: float,
                   scoped: "set[str] | None") -> list[dict] | None:
    if not text_embeddings.is_ready():
        return None
    vec = text_embeddings.encode(q)
    if vec is None:
        return None
    scored = []
    for row in storage.get_all_session_embeddings():
        if scoped is not None and row["session_id"] not in scoped:
            continue
        emb = text_embeddings.bytes_to_embedding(row["embedding_bytes"])
        score = text_embeddings.cosine_similarity(vec, emb)
        if score >= min_score:
            scored.append((score, row["session_id"]))
    scored.sort(reverse=True)
    return [{"session_id": sid, "semantic_score": round(score, 4)}
            for score, sid in scored[:limit]]


@bp.route("/search", methods=["GET", "POST"])
def search():
    args = _params()
    q = (args.get("q") or args.get("query") or "").strip()
    if not q:
        return _err("Pass a search query as ?q= (GET) or {\"q\": ...} (POST).")
    mode = (args.get("mode") or "hybrid").lower()
    if mode not in ("hybrid", "keyword", "semantic"):
        return _err("Unknown mode. Use hybrid (default), keyword, or semantic.")
    match = (args.get("match") or "all").lower()
    if match not in storage.MATCH_MODES:
        return _err(f"Unknown match mode. Use one of: "
                    f"{', '.join(storage.MATCH_MODES)}.")
    limit = max(1, min(50, _as_int(args.get("limit"), 10)))
    min_score = max(0.0, min(1.0, _as_float(args.get("min_score"), 0.25)))
    max_snippets = max(1, min(10, _as_int(args.get("max_snippets"), 3)))

    filters = _ctx.scope_filters(_filters_input(args))
    if filters["error"]:
        return _folder_error(filters)
    scoped_list = _ctx.scoped_session_ids(filters)
    scoped = set(scoped_list) if scoped_list is not None else None

    semantic_ready = text_embeddings.is_ready()
    kw = _keyword_hits(q, match, limit, scoped, max_snippets) \
        if mode in ("hybrid", "keyword") else []
    sem = _semantic_hits(q, limit, min_score, scoped) \
        if mode in ("hybrid", "semantic") else []
    if sem is None:
        sem = []

    if mode == "keyword":
        ordered = kw[:limit]
    elif mode == "semantic":
        if not semantic_ready:
            return _err("The semantic search model is still loading. Retry "
                        "shortly, or use mode=keyword.", 503)
        ordered = sem[:limit]
    else:
        # Reciprocal-rank fusion of the two ranked lists.
        fused: dict[str, dict] = {}
        for pos, r in enumerate(kw):
            e = fused.setdefault(r["session_id"], {"session_id": r["session_id"],
                                                   "score": 0.0})
            e["score"] += 1.0 / (60 + pos)
            e["matches"] = r.get("matches", [])
            e["keyword_rank"] = r["keyword_rank"]
        for pos, r in enumerate(sem):
            e = fused.setdefault(r["session_id"], {"session_id": r["session_id"],
                                                   "score": 0.0})
            e["score"] += 1.0 / (60 + pos)
            e["semantic_score"] = r["semantic_score"]
        ordered = sorted(fused.values(), key=lambda e: -e["score"])[:limit]
        for e in ordered:
            e["score"] = round(e["score"], 5)

    results = _enrich_results(ordered, filters["folders"])
    return jsonify({
        "query": q,
        "mode": mode,
        "match": match if mode != "semantic" else None,
        "scope": filters["desc"].removeprefix(" in ") or "all meetings",
        "semantic_ready": semantic_ready,
        "count": len(results),
        "results": results,
    })


@bp.route("/search/text")
def search_text():
    args = _params()
    needle = (args.get("contains") or args.get("q") or "").strip()
    if not needle:
        return _err("Pass ?contains=<exact substring>. Unlike /search this "
                    "matches raw text (punctuation, partial words) and also "
                    "scans summaries, notes, and chat.")
    scopes = None
    if args.get("scope"):
        scopes = [s.strip() for s in str(args["scope"]).split(",") if s.strip()]
        bad = [s for s in scopes if s not in storage.SUBSTRING_SCOPES]
        if bad:
            return _err(f"Unknown scope(s): {', '.join(bad)}. Valid: "
                        f"{', '.join(storage.SUBSTRING_SCOPES)}.")
    filters = _ctx.scope_filters(_filters_input(args))
    if filters["error"]:
        return _folder_error(filters)
    scoped = _ctx.scoped_session_ids(filters)

    rows = storage.substring_search(
        needle,
        scopes=scopes,
        session_ids=scoped,
        case_sensitive=helpers.parse_bool(args.get("case_sensitive"), False),
        limit=max(1, min(500, _as_int(args.get("limit"), 100))),
        context_chars=max(20, min(400, _as_int(args.get("context_chars"), 90))),
    )
    # Annotate with session titles/folders in one batch.
    ids = {r["session_id"] for r in rows if r.get("session_id")}
    metas = storage.get_sessions_meta(ids)
    labels = _ctx.folder_labels(filters["folders"])
    for r in rows:
        meta = metas.get(r.get("session_id"))
        if meta:
            r["title"] = meta["title"]
            r["started_at"] = meta["started_at"]
            info = labels.get(meta["folder_id"]) if meta["folder_id"] else None
            r["folder_path"] = info["path"] if info else None
    return jsonify({"contains": needle, "count": len(rows),
                    "scopes": scopes or list(storage.SUBSTRING_SCOPES),
                    "results": rows})


# ── Speakers ──────────────────────────────────────────────────────────────────

@bp.route("/speakers")
def speakers_list():
    speakers = _ctx.list_global_speakers()
    counts = storage.global_speaker_session_counts()
    out = []
    for sp in speakers:
        stats = counts.get(sp["id"], {})
        out.append({
            "id": sp["id"],
            "name": sp["name"],
            "color": sp.get("color"),
            "session_count": stats.get("session_count", 0),
            "last_seen": stats.get("last_seen"),
        })
    out.sort(key=lambda s: -(s["session_count"] or 0))
    return jsonify({"count": len(out), "speakers": out})


@bp.route("/speakers/<spec>/meetings")
def speaker_meetings(spec: str):
    sp, failed = _resolve_profile(spec)
    if failed:
        return failed
    sessions = _ctx.get_profile_sessions(sp["id"])
    labels = _ctx.folder_labels()
    metas = storage.get_sessions_meta([s["session_id"] for s in sessions])
    notes_set = storage.sessions_have_notes([s["session_id"] for s in sessions])
    meetings = []
    for info in sessions:
        meta = metas.get(info["session_id"])
        if meta:
            entry = _meeting_item(meta, labels, notes_set)
            entry["segments_by_speaker"] = info.get("seg_count")
            meetings.append(entry)
    return jsonify({"speaker": {"id": sp["id"], "name": sp["name"],
                                "color": sp.get("color")},
                    "count": len(meetings), "meetings": meetings})


# ── Organisation: folder edits and bulk moves ─────────────────────────────────

def _folder_entry(folder_id: str) -> dict | None:
    return next((f for f in storage.folder_tree() if f["id"] == folder_id), None)


def _resolve_folder_spec(spec) -> tuple:
    """(folder_id, path, error_response) for a folder id / name / path.
    None, '', 'root' and '/' mean the top level (no folder)."""
    if spec in (None, "", "root", "/"):
        return None, None, None
    filters = _ctx.scope_filters({"folder": str(spec), "include_subfolders": False})
    if filters["error"]:
        return None, None, _folder_error(filters)
    return filters["folder_ids"][0], filters["label"], None


@bp.route("/folders/<folder_id>", methods=["PATCH"])
def folders_update(folder_id: str):
    """Rename a folder and/or move it under another folder (or to the top)."""
    folder = storage.get_folder(folder_id)
    if not folder:
        return _err(f"Folder '{folder_id}' not found. Call GET /folders for ids.", 404)
    body = request.get_json(silent=True) or {}
    changed: dict = {}
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name:
            return _err("Folder name must be a non-empty string.")
        storage.rename_folder(folder_id, name)
        changed["name"] = name
    if "parent" in body or "parent_id" in body:
        spec = body.get("parent_id") if "parent_id" in body else body.get("parent")
        new_parent, parent_path, failed = _resolve_folder_spec(spec)
        if failed:
            return failed
        if new_parent == folder_id or (
                new_parent and new_parent in storage.folder_with_descendants(folder_id)):
            return _err("A folder cannot be moved into itself or into one of its own "
                        "sub-folders.", 409)
        if new_parent != folder.get("parent_id"):
            storage.set_folder_parent(folder_id, new_parent)
        changed["parent_id"] = new_parent
        changed["parent_path"] = parent_path
    if not changed:
        return _err("Nothing to update. Supported fields: name, parent (a folder id, "
                    "name or path, or null for the top level).")
    _ctx.push_event("library_changed", {"reason": "folder_updated", "folder_id": folder_id})
    log.info("agent", f"Folder {folder_id[:8]} updated via Agent API: "
                      f"{', '.join(changed)}")
    return jsonify({"ok": True, "folder": _folder_entry(folder_id), "changed": changed})


@bp.route("/meetings/move", methods=["POST"])
def meetings_move():
    """Move many meetings into one folder (or out of every folder) at once."""
    body = request.get_json(silent=True) or {}
    raw_ids = body.get("meeting_ids") or body.get("session_ids") or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    ids = list(dict.fromkeys(str(i).strip() for i in raw_ids if str(i).strip()))
    if not ids:
        return _err("Pass meeting_ids: a list of session ids to move.")
    if len(ids) > 500:
        return _err("Move at most 500 meetings per call.", 413)
    if "folder" not in body and "folder_id" not in body:
        return _err("Pass folder: a folder id, name or path, or null to unfile "
                    "the meetings.")
    spec = body.get("folder_id") if "folder_id" in body else body.get("folder")
    fid, path, failed = _resolve_folder_spec(spec)
    if failed:
        return failed
    metas = storage.get_sessions_meta(ids)
    known = [i for i in ids if i in metas]
    missing = [i for i in ids if i not in metas]
    if not known:
        return _err("None of those meeting ids exist.", 404, missing=missing)
    already = [i for i in known if metas[i].get("folder_id") == fid]
    to_move = [i for i in known if i not in already]
    storage.bulk_set_folder(to_move, fid)
    if to_move:
        _ctx.push_event("library_changed", {"reason": "meetings_moved",
                                            "count": len(to_move), "folder_id": fid})
        log.info("agent", f"Moved {len(to_move)} meeting(s) to "
                          f"{path or 'no folder'} via Agent API")
    return jsonify({
        "ok": True,
        "folder_id": fid,
        "folder_path": path,
        "count": len(to_move),
        "moved": to_move,
        "already_there": already,
        "missing": missing,
    })


# ── Speakers: identify and label ──────────────────────────────────────────────
# Read side: the queue of meetings with unnamed speakers, and one meeting's
# evidence pack (agent_api/speakers.py). Write side: every change goes through
# the callable app.py wired in, which is the UI's own code path for the same
# action, so an agent's label is byte for byte a user's label.

def _library():
    return _ctx.voice_library


def _library_ready() -> bool:
    lib = _library()
    return bool(lib is not None and getattr(lib, "ready", False))


def _me_id() -> str | None:
    try:
        return _ctx.me_profile_id() if _ctx.me_profile_id else None
    except Exception:
        return None


def _needs(capability: str, fn):
    """A 501 when app.py did not wire a write capability (a partial context)."""
    if fn is None:
        return _err(f"This server did not wire '{capability}', so the operation is "
                    "unavailable here.", 501)
    return None


def _session_busy(session_id: str) -> str | None:
    """'recording' or 'reanalyzing' when the app is working on this session."""
    st = _ctx.status_payload() or {}
    if st.get("session_id") != session_id:
        return None
    if st.get("is_reanalyzing"):
        return "reanalyzing"
    if st.get("recording"):
        return "recording"
    return None


def _speaker_rows(session_id: str, sess: dict) -> list[dict]:
    return speaker_evidence.speaker_rows(
        session_id, sess.get("segments", []), _ctx.source_labels, _me_id())


_ME_REFUSAL = ("That speaker is the owner's own microphone (Me). It is never relabelled "
               "through the Agent API; the owner's name is set in Settings.")


@bp.route("/speakers/queue")
def speakers_queue():
    """Meetings that still need speaker work, newest first.

    A meeting is listed while it has an unnamed speaker with real talk time or
    its speaker count disagrees with the calendar's attendee count; naming or
    merging speakers removes it. The shared folder / date / speaker filters
    apply, so an agent can work one folder or one week at a time.
    """
    args = _params()
    filters = _ctx.scope_filters(_filters_input(args))
    if filters["error"]:
        return _folder_error(filters)
    ids = storage.list_session_ids(
        folder_ids=filters["folder_ids"], start=filters["start"],
        end=filters["end"], speaker=filters["speaker"])
    attention_map = storage.attention_by_session()
    reason = (args.get("reason") or "any").strip().lower()
    if reason not in ("any", "unresolved", "mismatch"):
        return _err("reason must be any (default), unresolved, or mismatch.")
    picked = []
    for sid in ids:
        att = attention_map.get(sid)
        if not att or not att.get("needs"):
            continue
        if reason == "unresolved" and not att.get("unresolved"):
            continue
        if reason == "mismatch" and "speaker_count_mismatch" not in (att.get("reasons") or []):
            continue
        picked.append(sid)
    limit = max(1, min(200, _as_int(args.get("limit"), 25)))
    offset = max(0, _as_int(args.get("offset"), 0))
    page = picked[offset:offset + limit]
    labels = _ctx.folder_labels(filters["folders"])
    metas = storage.get_sessions_meta(page)
    notes_set = storage.sessions_have_notes(page)
    items = []
    for sid in page:
        if sid not in metas:
            continue
        item = _meeting_item(metas[sid], labels, notes_set, summary_chars=160)
        item["attention"] = attention_map[sid]
        items.append(item)
    return jsonify({
        "total": len(picked),
        "offset": offset,
        "limit": limit,
        "count": len(items),
        "scope": filters["desc"].removeprefix(" in ") or "all meetings",
        "library_ready": _library_ready(),
        "meetings": items,
        "next_step": "For each meeting: GET /meetings/{id}/speakers/review, weigh the "
                     "evidence, then POST /meetings/{id}/speakers/label. Report "
                     "anything you could not settle instead of guessing.",
    })


@bp.route("/meetings/<session_id>/speakers/review")
def meeting_speakers_review(session_id: str):
    """The evidence pack for naming a meeting's speakers."""
    if not storage.get_session_times(session_id):
        return _err(f"Meeting '{session_id}' not found.", 404)
    args = request.args
    key = (args.get("speaker_key") or "").strip() or None
    include_matches = helpers.parse_bool(args.get("matches"), True)
    quote_count = max(1, min(12, _as_int(args.get("quotes"), 4)))
    top_k = max(1, min(10, _as_int(args.get("top_k"), 5)))
    detail = (args.get("detail") or "unnamed").strip().lower()
    if detail not in ("unnamed", "all"):
        return _err("detail must be unnamed (default) or all.")
    lib = _library()
    wav = None
    if include_matches and _library_ready() and media.has_audio(session_id):
        # The voice backfill reads PCM. For an Opus meeting this decodes it once
        # into the cache (a minute or two for a long recording, then free).
        wav = media.pcm_wav_path(session_id)
    mp4, live = _frame_sources(session_id)
    has_video = mp4.exists() or bool(live and live.get("live_video_path"))
    out = speaker_evidence.review(
        session_id, library=lib, source_labels=_ctx.source_labels, me_id=_me_id(),
        speaker_key=key, include_matches=include_matches, quote_count=quote_count,
        top_k=top_k, wav_path=wav, has_video=has_video, detail=detail)
    if out is None:
        return _err(f"Meeting '{session_id}' not found.", 404)
    if out.get("error") == "unknown_speaker":
        return _err(f"No speaker '{key}' in this meeting.", 404, speakers=out["speakers"])
    base = f"{_ctx.server_url}{_PREFIX}/meetings/{session_id}"
    out["links"] = {
        "frames": f"{base}/speakers/<speaker_key>/frames",
        "label": f"{base}/speakers/label",
        "transcript_for_speaker": f"{base}/transcript?speaker=<speaker_key>&format=text",
        "audio_clip": f"{base}/audio/clip?start=<t>&end=<t>",
    }
    out["next_step"] = (
        "Decide per unnamed speaker: a self-introduction, a 'strong' or 'clear' library "
        "match, or a screen frame that names them is enough to label; 'possible' plus a "
        "consistent calendar attendee is enough when you say so in evidence; anything "
        "weaker goes back to the user as a question. Merge same-voice keys with same_as.")
    return jsonify(out)


@bp.route("/meetings/<session_id>/speakers/<speaker_key>/frames")
def meeting_speaker_frames(session_id: str, speaker_key: str):
    """Screen frames from moments this speaker was talking."""
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    mp4, live, unavailable = _frame_availability(session_id)
    if unavailable:
        return unavailable
    by_key = speaker_evidence.segments_by_key(sess.get("segments", []))
    segs = by_key.get(speaker_key)
    if not segs:
        return _err(f"No speaker '{speaker_key}' in this meeting.", 404,
                    speakers=sorted(by_key))
    args = request.args
    count = max(1, min(6, _as_int(args.get("count"), 3)))
    width = max(160, min(1280, _as_int(args.get("width"), 768)))
    offset = settings.get_video_offset(session_id)
    frames = []
    for m in speaker_evidence.moments(segs, count):
        entry = dict(m)
        if m["t"] < offset:
            entry.update(ok=False, jpeg_base64=None, source=None, video_t=None,
                         note="Before the screen recording started.")
            frames.append(entry)
            continue
        jpeg, video_t, source = _frame_at(session_id, m["t"], width, False)
        entry.update(ok=bool(jpeg), video_t=round(video_t, 2), source=source,
                     jpeg_base64=base64.b64encode(jpeg).decode() if jpeg else None)
        frames.append(entry)
    labels = sess.get("speaker_labels") or {}
    return jsonify({
        "session_id": session_id,
        "speaker_key": speaker_key,
        "speaker_name": labels.get(speaker_key) or _ctx.source_labels.get(speaker_key, speaker_key),
        "width": width,
        "video_offset_sec": offset,
        "count": len(frames),
        "frames": frames,
        "how_to_read": "Each frame is the screen a moment into one of this speaker's "
                       "longer turns. Look for the highlighted or outlined tile, a "
                       "'Name is speaking' banner, or a presenter name. Highlights can "
                       "lag the audio by a second or two, so weigh several frames, and "
                       "fetch frames for an already named speaker to learn the layout.",
    })


@bp.route("/meetings/<session_id>/speakers", methods=["GET"])
def meeting_speakers(session_id: str):
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    rows = _speaker_rows(session_id, sess)
    return jsonify({"session_id": session_id, "speakers": rows,
                    "unnamed": sum(1 for r in rows if r["status"] == "unnamed"),
                    "review_url": f"{_ctx.server_url}{_PREFIX}/meetings/{session_id}"
                                  f"/speakers/review"})


@bp.route("/meetings/<session_id>/speakers/label", methods=["POST"])
def meeting_speakers_label(session_id: str):
    """Name a speaker, link a voice profile, merge split keys, flag noise, or
    reset to the diarizer's default. One action per call."""
    sess = _session_or_none(session_id)
    if not sess:
        return _err(f"Meeting '{session_id}' not found.", 404)
    body = request.get_json(silent=True) or {}
    raw_keys = body.get("speaker_keys")
    if raw_keys is None:
        raw_keys = [body.get("speaker_key")]
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    keys = list(dict.fromkeys(str(k).strip() for k in (raw_keys or []) if k and str(k).strip()))
    if not keys:
        return _err("Pass speaker_key or speaker_keys: the key(s) to label, from "
                    "GET /meetings/{id}/speakers/review.")
    rows = _speaker_rows(session_id, sess)
    by_key = {r["speaker_key"]: r for r in rows}
    unknown = [k for k in keys if k not in by_key]
    if unknown:
        return _err(f"Unknown speaker key(s): {', '.join(unknown)}.", 404,
                    speakers=[{"speaker_key": r["speaker_key"], "name": r["name"],
                               "status": r["status"]} for r in rows])
    if any(by_key[k]["is_me"] for k in keys):
        return _err(_ME_REFUSAL, 403)
    if _session_busy(session_id) == "reanalyzing":
        return _err("This meeting is being reanalysed; its speakers are about to be "
                    "rebuilt. Try again when it finishes.", 409)

    kinds = set()
    for field in ("name", "global_id", "same_as", "noise", "reset"):
        if body.get(field):
            kinds.add("assign" if field in ("name", "global_id") else field)
    if len(kinds) != 1:
        return _err("Choose exactly one action: name and/or global_id (assign a person), "
                    "same_as (the same voice as another speaker in this meeting), "
                    "noise: true, or reset: true.")
    action = kinds.pop()
    reinforce = helpers.parse_bool(body.get("reinforce"), False)
    evidence = (body.get("evidence") or "").strip()[:500]
    lib = _library()
    lib_ready = _library_ready()
    me_id = _me_id()
    profile: dict | None = None
    profile_created = False
    result: dict = {}

    if action in ("noise", "reset"):
        blocked = _needs("apply_speaker_corrections", _ctx.apply_speaker_corrections)
        if blocked:
            return blocked
        if action == "noise":
            result = _ctx.apply_speaker_corrections(session_id, [], keys) or {}
        else:
            result = _ctx.apply_speaker_corrections(
                session_id, [{"global_id": None, "member_keys": keys}], []) or {}
    else:
        blocked = _needs("label_speaker", _ctx.label_speaker)
        if blocked:
            return blocked
        name = color = gid = None
        if action == "same_as":
            other = str(body.get("same_as") or "").strip()
            if other not in by_key:
                return _err(f"same_as names an unknown speaker key '{other}'.", 404,
                            speakers=sorted(by_key))
            if other in keys:
                return _err("same_as must name a different speaker than the one(s) "
                            "being labelled.")
            target = by_key[other]
            if target["is_me"]:
                return _err(_ME_REFUSAL, 403)
            if target["is_generic"] or target["status"] == "noise":
                return _err(f"Speaker '{other}' has no name yet ({target['name']}). Name "
                            "it first, or label both keys with the same name in one "
                            "call.", 409)
            name, color, gid = target["name"], target.get("color"), target.get("global_id")
            if gid and lib is not None:
                profile = lib.get_global_speaker(gid) or None
        else:
            gid = (str(body.get("global_id") or "")).strip() or None
            name = (str(body.get("name") or "")).strip() or None
            if gid:
                if lib is None:
                    return _needs("voice_library", None)
                profile = lib.get_global_speaker(gid)
                if not profile:
                    return _err(f"No voice-library profile '{gid}'. See GET /speakers.", 404)
                if me_id and gid == me_id:
                    return _err("That is the owner's own voice profile; desktop speakers "
                                "are never linked to it here.", 403)
                if name and speaker_evidence.norm_name(name) != speaker_evidence.norm_name(profile["name"]):
                    return _err(f"name '{name}' does not match profile '{gid}' "
                                f"('{profile['name']}'). Pass one or the other.", 409)
                name, color = profile["name"], profile.get("color")
            else:
                if attention.is_generic_speaker_name(name):
                    return _err(f"'{name}' is a placeholder, not a person. Use reset: true "
                                "to return a speaker to its diarizer default.")
                if lib_ready:
                    existing = lib.find_by_name(name)
                    if existing and me_id and existing["id"] == me_id:
                        return _err("That is the owner's own name and voice profile; "
                                    "desktop speakers are never linked to it here.", 403)
                    if existing:
                        profile, gid = existing, existing["id"]
                    else:
                        gid = lib.create_global_speaker(name)
                        profile = lib.get_global_speaker(gid)
                        profile_created = True
        updated = _ctx.label_speaker(session_id, keys, name, color, gid, reinforce) or []
        result = {"labels": updated}

    log.info("agent", f"Speaker {action} via Agent API in {session_id[:8]}: "
                      f"{', '.join(keys)}" + (f" ({evidence})" if evidence else ""))
    after = {r["speaker_key"]: r for r in _speaker_rows(session_id, sess)}
    notes = []
    if action == "assign" or action == "same_as":
        if not lib_ready:
            notes.append("The voice library is not loaded, so the name applies to this "
                         "meeting only and no profile was linked.")
        elif reinforce:
            notes.append("reinforce was set: this speaker's audio is being added to the "
                         "profile in the background.")
        else:
            notes.append("The profile is linked without training on this audio; pass "
                         "reinforce: true only when the identity is certain.")
        if _session_busy(session_id) == "recording":
            notes.append("This meeting is recording; the label applies live as well.")
    if action == "noise":
        notes.append("Noise speakers are hidden from the transcript's speaker list and "
                     "no longer count as unnamed.")
    if action == "reset":
        notes.append("The speaker is back to its diarizer default and unlinked from "
                     "any profile; its voice samples stay with the meeting.")
    return jsonify({
        "ok": True,
        "session_id": session_id,
        "action": action,
        "speaker_keys": keys,
        "speakers": [after[k] for k in keys if k in after],
        "profile": ({"global_id": profile.get("id"), "name": profile.get("name"),
                     "created": profile_created} if profile else None),
        "reinforce": reinforce if action in ("assign", "same_as") else None,
        "evidence": evidence or None,
        "attention": storage.get_session_attention(session_id),
        "result": result,
        "notes": notes,
    })


@bp.route("/meetings/<session_id>/segments/<int:segment_id>/speaker", methods=["POST"])
def meeting_segment_speaker(session_id: str, segment_id: int):
    """Reattribute one transcript line to another speaker in the meeting, or
    give it a one-off label. For the odd misattributed line, not for renaming
    a speaker (that is POST .../speakers/label)."""
    seg = storage.get_segment(segment_id)
    if not seg or seg.get("session_id") != session_id:
        return _err(f"Segment {segment_id} is not part of meeting '{session_id}'.", 404)
    blocked = _needs("relabel_segment", _ctx.relabel_segment)
    if blocked:
        return blocked
    sess = _session_or_none(session_id)
    body = request.get_json(silent=True) or {}
    target_key = (str(body.get("speaker_key") or "")).strip() or None
    name = (str(body.get("name") or "")).strip() or None
    if not target_key and not name:
        return _err("Pass speaker_key (an existing speaker in this meeting) or name "
                    "(a one-off label for this line).")
    rows = _speaker_rows(session_id, sess)
    by_key = {r["speaker_key"]: r for r in rows}
    if target_key:
        if target_key not in by_key:
            return _err(f"Unknown speaker key '{target_key}'.", 404, speakers=sorted(by_key))
        label = by_key[target_key]["name"]
    else:
        label = name
    reinforce = helpers.parse_bool(body.get("reinforce"), False)
    train = bool(reinforce and not attention.is_generic_speaker_name(label)
                 and not (target_key and by_key[target_key]["is_me"]))
    row = _ctx.relabel_segment(segment_id, label, target_key, train=train)
    if not row:
        return _err("The segment vanished while it was being updated.", 409)
    log.info("agent", f"Segment {segment_id} reattributed via Agent API to "
                      f"{target_key or label!r} in {session_id[:8]}")
    return jsonify({
        "ok": True,
        "session_id": session_id,
        "segment": helpers.transcript_rows([row], sess.get("speaker_labels"),
                                           _ctx.source_labels)[0],
        "reinforce": train,
        "note": "Only this line changed. Its speaker label wins over the speaker's "
                "name, so a later rename of the speaker leaves it as set here.",
    })


# ── Speakers: the voice library ───────────────────────────────────────────────

def _resolve_profile(spec: str):
    """(profile, error_response) for a voice-library id or (partial) name."""
    speakers = _ctx.list_global_speakers()
    spec_l = spec.strip().lower()
    matched = [s for s in speakers if s["id"] == spec]
    if not matched:
        matched = [s for s in speakers if s["name"].lower() == spec_l]
    if not matched:
        matched = [s for s in speakers if spec_l in s["name"].lower()]
    if not matched:
        return None, _err(f"No voice-library speaker matches '{spec}'. "
                          "See GET /speakers for the roster.", 404)
    if len(matched) > 1:
        counts = storage.global_speaker_session_counts()
        return None, _err(
            f"'{spec}' matches {len(matched)} voice-library profiles. "
            "Retry with one of the ids below (session_count shows which is "
            "the active profile).", 409,
            candidates=[{"id": s["id"], "name": s["name"],
                         "session_count": counts.get(s["id"], {}).get("session_count", 0)}
                        for s in matched])
    return matched[0], None


@bp.route("/speakers/library/health")
def speakers_library_health():
    """Duplicate, confusable and polluted profiles: the same report as the
    Voice Library's maintenance pass, read-only."""
    lib = _library()
    if lib is None:
        return _needs("voice_library", None)
    try:
        report = lib.library_health()
    except Exception as e:
        return _err(f"Could not compute the library report: {e}", 500)
    report["read_only"] = True
    report["note"] = ("Nothing here was changed. Same-name duplicates are merged by the "
                      "app's own weekly maintenance; use POST /speakers/{id}/merge for "
                      "a pair the user confirms, and treat 'confusable' pairs as a "
                      "reason to doubt a voice match between those two people.")
    return jsonify(report)


@bp.route("/speakers/<spec>")
def speaker_profile(spec: str):
    """One voice-library profile in depth."""
    sp, failed = _resolve_profile(spec)
    if failed:
        return failed
    lib = _library()
    full = (lib.get_global_speaker(sp["id"]) if lib is not None else None) or sp
    counts = storage.global_speaker_session_counts().get(sp["id"], {})
    me_id = _me_id()
    sessions = _ctx.get_profile_sessions(sp["id"]) or []
    metas = storage.get_sessions_meta([s["session_id"] for s in sessions[:12]])
    recent = []
    for info in sessions[:12]:
        meta = metas.get(info["session_id"])
        if meta:
            recent.append({"session_id": info["session_id"], "title": meta["title"],
                           "started_at": meta["started_at"],
                           "speaker_keys": info.get("speaker_keys"),
                           "segments": info.get("seg_count")})
    confusable = []
    if lib is not None and getattr(lib, "ready", False) and sp["id"] != me_id:
        try:
            cent = lib.get_centroid(sp["id"])
            if cent is not None:
                for m in lib.find_matches(cent, exclude_global_ids={sp["id"]}, top_k=5,
                                          min_similarity=speaker_evidence.CONFUSABLE_SIM):
                    confusable.append({"global_id": m["global_id"], "name": m["name"],
                                       "similarity": m["similarity"]})
        except Exception:
            confusable = []
    same_name = [r for r in storage.find_speaker_labels_by_name(sp["name"], match="exact")
                 if r.get("global_id") != sp["id"]]
    return jsonify({
        "profile": {"global_id": sp["id"], "name": sp["name"], "color": sp.get("color"),
                    "created_at": full.get("created_at"), "updated_at": full.get("updated_at")},
        "is_me": sp["id"] == me_id,
        "voice_samples": full.get("emb_count", 0),
        "session_count": counts.get("session_count", 0),
        "last_seen": counts.get("last_seen"),
        "recent_meetings": recent,
        "confusable_with": confusable,
        "labels_with_this_name_not_linked": len(same_name),
        "links": {"meetings": f"{_ctx.server_url}{_PREFIX}/speakers/{sp['id']}/meetings"},
        "note": ("confusable_with lists profiles whose voice is close to this one; a "
                 "match to either is uncertain between them. labels_with_this_name_not_"
                 "linked counts meeting labels spelled like this profile but linked to "
                 "another or no profile; POST /speakers/relabel/plan can unify them."),
    })


@bp.route("/speakers/<global_id>", methods=["PATCH"])
def speaker_profile_update(global_id: str):
    """Rename a voice-library profile; every meeting label linked to it follows."""
    lib = _library()
    if lib is None:
        return _needs("voice_library", None)
    blocked = _needs("rename_profile", _ctx.rename_profile)
    if blocked:
        return blocked
    profile = lib.get_global_speaker(global_id)
    if not profile:
        return _err(f"No voice-library profile '{global_id}'. See GET /speakers.", 404)
    if _me_id() and global_id == _me_id():
        return _err("That is the owner's own profile; their name is changed in Settings, "
                    "under the Me speaker.", 403)
    body = request.get_json(silent=True) or {}
    name = (str(body.get("name") or "")).strip()
    if not name:
        return _err("Pass name: the profile's new name.")
    if attention.is_generic_speaker_name(name):
        return _err(f"'{name}' is a placeholder, not a name.")
    clash = lib.find_by_name(name)
    if clash and clash["id"] != global_id:
        return _err(f"A profile named '{name}' already exists ({clash['id']}). Merge the "
                    "two with POST /speakers/{keep_id}/merge instead of creating a "
                    "duplicate by renaming.", 409, existing_profile_id=clash["id"])
    before = profile["name"]
    resolved = _ctx.rename_profile(global_id, name=name) or {}
    linked = lib.get_linked_labels(global_id) or []
    log.info("agent", f"Profile {global_id[:8]} renamed via Agent API: "
                      f"{before!r} to {name!r} ({len(linked)} label(s))")
    return jsonify({"ok": True, "profile": {"global_id": global_id,
                                            "name": resolved.get("name", name),
                                            "color": resolved.get("color")},
                    "previous_name": before, "labels_updated": len(linked),
                    "meetings_affected": len({r["session_id"] for r in linked})})


@bp.route("/speakers/<global_id>/merge", methods=["POST"])
def speaker_profile_merge(global_id: str):
    """Fold one voice profile into another. Irreversible; confirm is required."""
    lib = _library()
    if lib is None:
        return _needs("voice_library", None)
    blocked = _needs("merge_profiles", _ctx.merge_profiles)
    if blocked:
        return blocked
    if not getattr(lib, "ready", False):
        return _err("The voice library model is not loaded; merges wait until it is.", 503)
    body = request.get_json(silent=True) or {}
    source = (str(body.get("source_id") or body.get("merge_id") or "")).strip()
    if not source:
        return _err("Pass source_id: the profile to merge into this one.")
    keep = lib.get_global_speaker(global_id)
    merge = lib.get_global_speaker(source)
    if not keep:
        return _err(f"No voice-library profile '{global_id}'.", 404)
    if not merge:
        return _err(f"No voice-library profile '{source}'.", 404)
    if source == global_id:
        return _err("source_id must differ from the profile being kept.")
    me_id = _me_id()
    if me_id and me_id in (global_id, source):
        return _err("The owner's own profile is never merged through the Agent API.", 403)
    if not body.get("confirm"):
        return _err(
            f"Merging moves {merge.get('emb_count', 0)} voice sample(s) and every meeting "
            f"label from '{merge['name']}' into '{keep['name']}' and removes the "
            f"'{merge['name']}' profile. This cannot be undone from the app. Show that "
            "to the user and pass confirm: true once they agree.", 400,
            keep={"global_id": global_id, "name": keep["name"],
                  "voice_samples": keep.get("emb_count", 0)},
            merge={"global_id": source, "name": merge["name"],
                   "voice_samples": merge.get("emb_count", 0)})
    resolved = _ctx.merge_profiles(global_id, source) or {}
    after = lib.get_global_speaker(global_id) or keep
    linked = lib.get_linked_labels(global_id) or []
    log.info("agent", f"Profiles merged via Agent API: {merge['name']!r} ({source[:8]}) "
                      f"into {keep['name']!r} ({global_id[:8]})")
    return jsonify({"ok": True,
                    "kept": {"global_id": global_id, "name": resolved.get("name", after.get("name")),
                             "voice_samples": after.get("emb_count", 0)},
                    "merged_away": {"global_id": source, "name": merge["name"]},
                    "labels_now_linked": len(linked)})


# ── Speakers: bulk relabel (plan, confirm, apply) ─────────────────────────────

@bp.route("/speakers/relabel/plan", methods=["POST"])
def speakers_relabel_plan():
    """Describe a library-wide (or one-meeting) rename without changing anything."""
    blocked = _needs("relabel_deps", _ctx.relabel_deps)
    if blocked:
        return blocked
    body = _params()
    from_name = (str(body.get("from_name") or "")).strip()
    to_name = (str(body.get("to_name") or "")).strip()
    if not from_name or not to_name:
        return _err("Pass from_name and to_name.")
    match = (str(body.get("match") or "exact")).strip().lower()
    if match not in speaker_relabel.MATCH_MODES:
        return _err(f"match must be one of: {', '.join(speaker_relabel.MATCH_MODES)}.")
    scope = (str(body.get("scope") or "library")).strip().lower()
    if scope not in speaker_relabel.SCOPES:
        return _err(f"scope must be one of: {', '.join(speaker_relabel.SCOPES)}.")
    if scope == "session":
        sid = (str(body.get("session_id") or body.get("meeting_id") or "")).strip()
        if not sid:
            return _err("scope 'session' needs session_id.")
        if not storage.get_session_times(sid):
            return _err(f"Meeting '{sid}' not found.", 404)
        session_ids = [sid]
    else:
        filters = _ctx.scope_filters(_filters_input(body))
        if filters["error"]:
            return _folder_error(filters)
        session_ids = _ctx.scoped_session_ids(filters)
    try:
        plan = speaker_relabel.build_plan(from_name, to_name, scope, session_ids, match,
                                          deps=_ctx.relabel_deps())
    except ValueError as e:
        return _err(str(e), 400)
    if not plan["sessions"] and not plan.get("profile_only"):
        return jsonify({"matched": 0, "token": None, "summary": plan["summary"],
                        "warnings": plan["warnings"],
                        "next_step": "Nothing matched. Check the spelling against "
                                     "GET /speakers before trying again; do not guess."})
    token = speaker_relabel.mint_token(plan, None)
    card = speaker_relabel.plan_card(plan, token)
    card["matched"] = plan["key_count"]
    card["expires_in_sec"] = speaker_relabel.TOKEN_TTL_SEC
    card["next_step"] = ("Nothing has changed. Show the summary and every warning to the "
                         "user; once they confirm, POST /speakers/relabel/apply with this "
                         "token and confirm: true (the token is single use and expires).")
    return jsonify(card)


@bp.route("/speakers/relabel/apply", methods=["POST"])
def speakers_relabel_apply():
    blocked = _needs("relabel_deps", _ctx.relabel_deps)
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    token = (str(body.get("token") or "")).strip()
    if not token:
        return _err("Pass the token from POST /speakers/relabel/plan.")
    if not body.get("confirm"):
        return _err("Pass confirm: true, and only after the user approved this exact "
                    "plan.")
    try:
        result = speaker_relabel.apply_plan(token, current_request_id=None,
                                            confirmed_by="agent_api",
                                            deps=_ctx.relabel_deps())
    except ValueError as e:
        return _err(str(e), 409,
                    applied_session_ids=list(getattr(e, "applied_session_ids", ()) or ()))
    log.info("agent", f'Relabel applied via Agent API: "{result["from_name"]}" to '
                      f'"{result["to_name"]}" ({result["key_count"]} label(s) in '
                      f'{result["session_count"]} meeting(s), {result["strategy"]})')
    _ctx.push_event("library_changed", {"reason": "speakers_relabelled",
                                        "count": result.get("session_count", 0)})
    return jsonify({"ok": True, **result})


@bp.route("/speakers/relabel/cancel", methods=["POST"])
def speakers_relabel_cancel():
    body = request.get_json(silent=True) or {}
    token = (str(body.get("token") or "")).strip()
    cancelled = bool(token) and speaker_relabel.cancel(token)
    return jsonify({"ok": True, "cancelled": cancelled,
                    "note": "Plan dropped; nothing was changed." if cancelled else
                            "No pending plan matched that token (already applied, "
                            "cancelled, or expired); nothing was changed."})


# ── Global AI chats ───────────────────────────────────────────────────────────

@bp.route("/chats")
def chats_list():
    convos = storage.list_global_conversations()
    return jsonify({"count": len(convos), "conversations": convos})


@bp.route("/chats/<conversation_id>")
def chats_detail(conversation_id: str):
    convo = storage.get_global_conversation(conversation_id)
    if not convo:
        return _err(f"Conversation '{conversation_id}' not found.", 404)
    convo["messages"] = _parse_chat_rows(convo.get("messages", []))
    return jsonify(convo)


# ── Settings ──────────────────────────────────────────────────────────────────

def _masked_settings_values() -> dict:
    values = settings.load()
    if values.get("agent_api_token"):
        values["agent_api_token"] = "********"
    # The published-calendar link is a credential: anyone holding it can read
    # the owner's calendar. Only the masked form ever leaves the process.
    if values.get("calendar_ics_url"):
        values["calendar_ics_url"] = calendar_feed.mask_url(values["calendar_ics_url"])
    # video_offsets is a per-session bookkeeping map (one entry per recording).
    # It is read-only here and dwarfs every other setting once the library
    # grows, so summarise it instead of dumping thousands of tokens of UUIDs.
    # A single meeting's offset is served by GET /meetings/<id> and /media.
    offsets = values.get("video_offsets")
    if isinstance(offsets, dict):
        values["video_offsets"] = {
            "_summary": "Elided: per-session video offsets, not writable here. "
                        "See video_offset_sec on GET /meetings/<id>.",
            "count": len(offsets),
            "nonzero_count": sum(1 for v in offsets.values() if v),
        }
    return values


@bp.route("/settings")
def settings_get():
    return jsonify({
        "values": _masked_settings_values(),
        "api_keys": helpers.mask_api_keys(config.get_key_status()),
        "data_folder": {"current": str(paths.data_dir()),
                        "default": str(paths.default_dir()),
                        "overridden": paths.is_overridden()},
        "schema_url": f"{_ctx.server_url}{_PREFIX}/settings/schema",
        "notes": [
            "PATCH /settings with {\"updates\": {key: value}} to change values.",
            "API keys are managed in the app UI only and are never exposed "
            "or writable here.",
        ],
    })


@bp.route("/settings/schema")
def settings_schema():
    return jsonify({"settings": helpers.settings_schema()})


@bp.route("/settings", methods=["PATCH", "PUT"])
def settings_patch():
    body = request.get_json(silent=True) or {}
    updates = body.get("updates") if isinstance(body.get("updates"), dict) else body
    if not isinstance(updates, dict) or not updates:
        return _err("Send {\"updates\": {\"key\": value, ...}}. "
                    "See GET /settings/schema for valid keys.")

    recording = bool(_ctx.status_payload().get("recording"))
    applied: dict = {}
    skipped: dict = {}
    restart_required: list[str] = []
    ai_change: dict = {}

    for key, value in updates.items():
        if key in helpers.SETTINGS_WRITE_DENYLIST:
            skipped[key] = "Internal key, not writable via the Agent API."
            continue
        ok, result = helpers.coerce_setting(key, value)
        if not ok:
            skipped[key] = result
            continue
        if recording and key in helpers.RECORDING_LOCKED_KEYS:
            skipped[key] = "Locked while a recording is running."
            continue
        if key in ("ai_provider", "ai_model"):
            ai_change[key] = result
            continue
        applied[key] = result
        if key in helpers.RESTART_REQUIRED_KEYS:
            restart_required.append(key)

    if applied:
        settings.update(applied)
    if ai_change:
        outcome = _ctx.apply_ai_settings(ai_change.get("ai_provider"),
                                         ai_change.get("ai_model"))
        applied.update({k: v for k, v in ai_change.items()})
        applied["ai_selection_now"] = outcome
    if applied:
        _ctx.push_status()
        log.info("agent", f"Settings updated via Agent API: "
                          f"{', '.join(k for k in applied if k != 'ai_selection_now')}")

    status = 200 if applied else 400
    return jsonify({
        "ok": bool(applied),
        "applied": applied,
        "skipped": skipped,
        "restart_required": restart_required,
    }), status


# ── Live recording ────────────────────────────────────────────────────────────

@bp.route("/live")
def live():
    status = _ctx.status_payload()
    if not status.get("recording"):
        recent_ids = storage.list_session_ids(limit=1)
        return jsonify({
            "recording": False,
            "latest_session_id": recent_ids[0] if recent_ids else None,
            "model_ready": status.get("model_ready"),
            "note": "No recording is running. The latest completed meeting "
                    "is linked above.",
        })
    sid = status.get("session_id")
    sess = storage.get_session(sid) or {}
    segs = sess.get("segments", [])
    after_id = _as_int(request.args.get("after_segment_id"), 0)
    fresh = [s for s in segs if (s.get("id") or 0) > after_id] if after_id else segs
    tail_limit = max(1, min(500, _as_int(request.args.get("limit"), 50)))
    fresh = fresh[-tail_limit:]
    elapsed = max((s.get("end_time") or 0.0) for s in segs) if segs else 0.0
    live_video = bool((_ctx.live_media() or {}).get("live_video_path"))
    payload = {
        "recording": True,
        "session_id": sid,
        "title": sess.get("title"),
        "started_at": sess.get("started_at"),
        "elapsed_sec": round(elapsed, 1),
        "segment_count": len(segs),
        "segments": helpers.transcript_rows(
            fresh, sess.get("speaker_labels"), _ctx.source_labels),
        "last_segment_id": max((s.get("id") or 0) for s in segs) if segs else 0,
        "chapters": sess.get("chapters", []),
        "live_video": live_video,
        **_ctx.live_extras(),
    }
    if live_video:
        payload["frame_url"] = (f"{_ctx.server_url}{_PREFIX}/meetings/{sid}"
                                f"/frame?t=now")
    if helpers.parse_bool(request.args.get("include_summary"), True):
        payload["summary"] = sess.get("summary", "")
    return jsonify(payload)


# ── Recording control (opt-in) ────────────────────────────────────────────────

def _recording_control_gate():
    if not settings.get("agent_api_allow_recording_control", False):
        return _err(
            "Recording control by agents is disabled (default). The user can "
            "enable it in Settings > Agent API ('Allow recording control'), "
            "which sets agent_api_allow_recording_control=true.", 403)
    return None


@bp.route("/recording/start", methods=["POST"])
def recording_start():
    denied = _recording_control_gate()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return _err("Pass {\"confirm\": true} to start a recording. This "
                    "opens the app's session page which begins capturing "
                    "audio (and screen, if configured) on this machine.", 400)
    if _ctx.status_payload().get("recording"):
        return _err("A recording is already running.", 409)
    # The session page performs the start (that is where device selection and
    # the readiness gate live). The coordinator offers the command to the app
    # window that is already open before opening one, and falls back to the old
    # autostart window if nothing takes it. This mirrors the tray and
    # meeting-detect flows exactly. See core/recording_request.py.
    if recording_request.get_default() is None:
        return _err("The app is not ready to start recordings.", 503)
    recording_request.request_start_async("agent_api", "requested via Agent API")
    log.info("agent", "Recording start requested via Agent API "
                      "(handed to the start coordinator).")
    return jsonify({
        "ok": True,
        "initiated": "start_request",
        "note": "A start command was sent to the app window (a window is "
                "opened only if none takes it). Poll GET /live to confirm the "
                "recording is running.",
    })


@bp.route("/recording/stop", methods=["POST"])
def recording_stop():
    denied = _recording_control_gate()
    if denied:
        return denied
    body = request.get_json(silent=True) or {}
    if not body.get("confirm"):
        return _err("Pass {\"confirm\": true} to stop the active recording.")
    if not _ctx.status_payload().get("recording"):
        return _err("No recording is running.", 409)
    log.info("agent", "Recording stop requested via Agent API.")
    return _ctx.stop_recording()
