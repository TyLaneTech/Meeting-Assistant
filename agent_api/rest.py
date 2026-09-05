"""Agent REST API - the machine-facing interface to Meeting Assistant.

Mounted at ``/api/agent/v1``. Designed for AI agents (Claude Desktop, Claude
Code, Codex, custom scripts) rather than the browser UI:

- Self-describing: GET /            -> index with endpoint catalog
                   GET /docs        -> full markdown guide
                   GET /openapi.json-> OpenAPI 3.1 spec
- Read the library: meetings, transcripts (5 formats), summaries, notes,
  chapters, chat history, speakers, folders, media, video frames, audio clips.
- Search: hybrid keyword+semantic, plus raw substring scan.
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
from agent_api.context import AgentContext
from capture_video import capture_live_frame, extract_frame, find_ffmpeg
from capture_video.ffmpeg_util import subprocess_no_window_flag
from core import calendar_feed, config, log, paths, recording_request, settings, storage
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
                         "GET /meetings/{id}/transcript", "GET /meetings/{id}/summary",
                         "GET /meetings/{id}/notes", "POST /meetings/{id}/notes/append",
                         "GET /meetings/{id}/chapters", "POST /meetings/{id}/chapters",
                         "GET /meetings/{id}/chat", "GET /meetings/{id}/speakers",
                         "GET /meetings/{id}/media", "GET /meetings/{id}/frame",
                         "GET /meetings/{id}/frames", "GET /meetings/{id}/audio",
                         "GET /meetings/{id}/audio/clip",
                         "GET /meetings/{id}/screenshots",
                         "GET /meetings/{id}/screenshots/{name}",
                         "GET /meetings/{id}/export"],
            "search": ["GET|POST /search", "GET /search/text"],
            "folders": ["GET /folders", "POST /folders", "GET /folders/resolve"],
            "speakers": ["GET /speakers", "GET /speakers/{id_or_name}/meetings"],
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
               "transcript", "chat", "summary_history")


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


@bp.route("/meetings/<session_id>/speakers")
def meeting_speakers(session_id: str):
    if not _session_or_none(session_id):
        return _err(f"Meeting '{session_id}' not found.", 404)
    return jsonify({"session_id": session_id,
                    "speakers": _resolved_speakers(session_id)})


# ── Meetings: media ───────────────────────────────────────────────────────────

@bp.route("/meetings/<session_id>/media")
def meeting_media(session_id: str):
    if not _session_or_none(session_id):
        return _err(f"Meeting '{session_id}' not found.", 404)
    wav = paths.audio_dir() / f"{session_id}.wav"
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
        "audio": ({**(helpers.wav_info(wav) or {}), "url": f"{base}/audio"}
                  if wav.exists() else None),
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
    wav = paths.audio_dir() / f"{session_id}.wav"
    if not wav.exists():
        return _err("No audio recording exists for this meeting.", 404)
    return send_file(str(wav), mimetype="audio/wav", conditional=True)


@bp.route("/meetings/<session_id>/audio/clip")
def meeting_audio_clip(session_id: str):
    wav = paths.audio_dir() / f"{session_id}.wav"
    if not wav.exists():
        return _err("No audio recording exists for this meeting.", 404)
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
    speakers = _ctx.list_global_speakers()
    spec_l = spec.strip().lower()
    matched = [s for s in speakers if s["id"] == spec]
    if not matched:
        matched = [s for s in speakers if s["name"].lower() == spec_l]
    if not matched:
        matched = [s for s in speakers if spec_l in s["name"].lower()]
    if not matched:
        return _err(f"No voice-library speaker matches '{spec}'. "
                    "See GET /speakers for the roster.", 404)
    if len(matched) > 1:
        counts = storage.global_speaker_session_counts()
        return _err(
            f"'{spec}' matches {len(matched)} voice-library profiles. "
            "Retry with one of the ids below (session_count shows which is "
            "the active profile).", 409,
            candidates=[{"id": s["id"], "name": s["name"],
                         "session_count": counts.get(s["id"], {})
                                                .get("session_count", 0)}
                        for s in matched])
    sp = matched[0]
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
