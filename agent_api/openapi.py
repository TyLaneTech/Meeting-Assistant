"""OpenAPI 3.1 spec for the Agent REST API.

Built from a compact route table rather than introspection so descriptions
stay curated. Served at GET /api/agent/v1/openapi.json. Response schemas are
deliberately loose (documented JSON objects); the authoritative field-level
reference is the guide at /api/agent/v1/docs.
"""
from __future__ import annotations

from agent_api import API_VERSION


def _q(name: str, desc: str, typ: str = "string", **extra) -> dict:
    schema = {"type": typ}
    schema.update(extra.pop("schema_extra", {}))
    p = {"name": name, "in": "query", "description": desc, "schema": schema}
    p.update(extra)
    return p


def _p(name: str, desc: str) -> dict:
    return {"name": name, "in": "path", "required": True,
            "description": desc, "schema": {"type": "string"}}


_TS = " Accepts seconds (90.5) or a clock string ('1:30', '01:02:03')."

# Shared meeting-scope filters.
_FILTERS = [
    _q("folder", "Restrict to one folder: id, (partial) name, or path like "
                 "'Engineering / Backend'. Ambiguous values return candidates."),
    _q("include_subfolders", "Include sessions in sub-folders of the matched "
                             "folder (default true).", "boolean"),
    _q("speaker", "Restrict to meetings this person participated in "
                  "(case-insensitive partial name)."),
    _q("within_days", "Only meetings from the last N days.", "integer"),
    _q("start_date", "Earliest meeting date, ISO YYYY-MM-DD."),
    _q("end_date", "Latest meeting date, ISO YYYY-MM-DD (whole day included)."),
]

_SID = _p("session_id", "Meeting session id (UUID) from /meetings or /search.")


def _op(summary: str, desc: str = "", params: list | None = None,
        body: dict | None = None, tags: list | None = None) -> dict:
    op = {
        "summary": summary,
        "description": desc or summary,
        "tags": tags or [],
        "responses": {
            "200": {"description": "Success. JSON unless the endpoint serves "
                                   "media or an explicit text format."},
            "4XX": {"description": "Error as {\"error\": \"...\"} JSON."},
        },
    }
    if params:
        op["parameters"] = params
    if body:
        op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": body}},
        }
    return op


def build_spec(server_url: str) -> dict:
    paths: dict = {}

    def add(path: str, method: str, op: dict) -> None:
        paths.setdefault(path, {})[method] = op

    # Discovery / system
    add("/", "get", _op("API index with endpoint catalog and live status",
                        tags=["discovery"]))
    add("/docs", "get", _op("Full agent guide (markdown)", tags=["discovery"]))
    add("/openapi.json", "get", _op("This spec", tags=["discovery"]))
    add("/system/health", "get", _op("Liveness probe (always answers, even "
                                     "when the API is disabled)", tags=["system"]))
    add("/system/status", "get", _op("Live app state: recording flag, active "
                                     "session, model/diarizer readiness, audio "
                                     "levels", tags=["system"]))
    add("/system/info", "get", _op("Instance info: version, platform, uptime, "
                                   "data folder, models, AI config, library "
                                   "counts", tags=["system"]))
    add("/system/stats", "get", _op("Library analytics plus storage usage "
                                    "breakdown", tags=["system"]))
    add("/system/logs", "get", _op(
        "Recent application log entries",
        "In-memory ring buffer captured since app start.",
        [_q("limit", "Max entries (default 200, max 1000).", "integer"),
         _q("level", "Minimum severity: info, warn, or error."),
         _q("tag", "Exact component tag (e.g. recording, ai, whisper, agent)."),
         _q("contains", "Case-insensitive substring of the message."),
         _q("after_id", "Only entries newer than this id (incremental "
                        "polling).", "integer")],
        tags=["system"]))
    add("/system/logs/files", "get", _op("List persisted log files",
                                         tags=["system"]))
    add("/system/logs/files/{name}", "get", _op(
        "Tail a persisted log file (text/plain)", "",
        [_p("name", "File name from /system/logs/files."),
         _q("lines", "Trailing lines to return (default 500).", "integer")],
        tags=["system"]))
    add("/system/changelog", "get", _op(
        "Recent app updates parsed from git history", "",
        [_q("limit", "Max commits (default 15).", "integer")], tags=["system"]))

    # Folders
    add("/folders", "get", _op("Folder tree with paths and session counts",
                               tags=["folders"]))
    add("/folders", "post", _op(
        "Create a folder", "",
        body={"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"},
            "parent_id": {"type": "string"},
            "parent": {"type": "string",
                       "description": "Alternative to parent_id: name or path."},
        }}, tags=["folders"]))
    add("/folders/resolve", "get", _op(
        "Resolve a fuzzy folder reference to a real folder", "",
        [_q("q", "Folder id, (partial) name, or path.", required=True)],
        tags=["folders"]))

    # Meetings
    add("/meetings", "get", _op(
        "List meetings, newest first",
        "All filters combine. Includes per-meeting summary preview, speakers, "
        "duration, and media flags.",
        _FILTERS + [_q("limit", "Page size (default 50, max 500).", "integer"),
                    _q("offset", "Pagination offset.", "integer")],
        tags=["meetings"]))
    add("/meetings/{session_id}", "get", _op(
        "One meeting as a bundle",
        "Default parts: summary, chapters, speakers, notes, media. "
        "?include=all adds transcript, chat, summary_history.",
        [_SID, _q("include", "Comma list or 'all'.")], tags=["meetings"]))
    add("/meetings/{session_id}", "patch", _op(
        "Rename a meeting or move it between folders", "",
        [_SID],
        body={"type": "object", "properties": {
            "title": {"type": "string"},
            "folder": {"type": ["string", "null"],
                       "description": "Folder id/name/path, or null to unfile."},
        }}, tags=["meetings"]))
    add("/meetings/{session_id}/transcript", "get", _op(
        "Diarized transcript in five formats",
        "format=json returns segment rows with resolved speaker names; "
        "text/markdown/srt/vtt return the rendered document (markdown "
        "interleaves chapter headings).",
        [_SID,
         _q("format", "json (default), text, markdown, srt, vtt."),
         _q("start", "Only segments overlapping after this time." + _TS),
         _q("end", "Only segments overlapping before this time." + _TS),
         _q("speaker", "Only segments whose resolved speaker name contains this."),
         _q("after_segment_id", "Only segments newer than this id.", "integer"),
         _q("offset", "Skip N matched segments.", "integer"),
         _q("limit", "Max segments returned (0 = all).", "integer"),
         _q("timestamps", "Include [M:SS] stamps in text formats "
                          "(default true).", "boolean"),
         _q("speakers", "Include speaker names in text formats "
                        "(default true).", "boolean"),
         _q("chapters", "Interleave chapters in markdown (default true).",
            "boolean"),
         _q("envelope", "Wrap text formats in JSON instead of raw text.",
            "boolean")],
        tags=["meetings"]))
    add("/meetings/{session_id}/summary", "get",
        _op("Latest AI summary", "", [_SID], tags=["meetings"]))
    add("/meetings/{session_id}/notes", "get", _op(
        "User notes as markdown and plain text", "",
        [_SID, _q("raw", "Also include the raw Quill Delta.", "boolean")],
        tags=["meetings"]))
    add("/meetings/{session_id}/notes/append", "post", _op(
        "Append a note block (additive only, never edits existing notes)", "",
        [_SID],
        body={"type": "object", "required": ["text"], "properties": {
            "text": {"type": "string"},
            "heading": {"type": "string",
                        "description": "Optional H3 heading above the block."},
        }}, tags=["meetings"]))
    add("/meetings/{session_id}/chapters", "get",
        _op("Chapter markers", "", [_SID], tags=["meetings"]))
    add("/meetings/{session_id}/chapters", "post", _op(
        "Add a chapter marker", "",
        [_SID],
        body={"type": "object", "required": ["title", "start_time"],
              "properties": {"title": {"type": "string"},
                             "start_time": {"type": ["number", "string"],
                                            "description": "Seconds or 'M:SS'."}}},
        tags=["meetings"]))
    add("/meetings/{session_id}/chat", "get",
        _op("AI chat history for this meeting", "", [_SID], tags=["meetings"]))
    add("/meetings/{session_id}/speakers", "get", _op(
        "Per-speaker stats: talk time, segments, words, voice-library link",
        "", [_SID], tags=["meetings"]))
    add("/meetings/{session_id}/media", "get", _op(
        "Media inventory: audio/video tech info, screenshots, attachments",
        "", [_SID], tags=["media"]))
    add("/meetings/{session_id}/frame", "get", _op(
        "One video frame at a meeting-timeline moment (JPEG)",
        "Timestamps are on the transcript timeline; the stored video offset "
        "is applied automatically (disable with raw=1). Works during a live "
        "recording too: any timestamp up to the live head, or t=now for the "
        "current screen. The response's source field says where the frame "
        "came from (video, live_file, live_screen).",
        [_SID,
         _q("t", "The moment to capture." + _TS +
            " 'now' is valid while the meeting is recording.", required=True),
         _q("width", "Max width in px, 160-1920 (default 1280).", "integer"),
         _q("format", "jpeg (default, binary), base64, or data_uri (JSON)."),
         _q("raw", "Treat t as raw video-file time.", "boolean")],
        tags=["media"]))
    add("/meetings/{session_id}/frames", "get", _op(
        "Batch of frames (JSON with base64 JPEGs, max 12)",
        "Either explicit timestamps (at=) or an evenly spaced sweep (count=). "
        "During a live recording the sweep runs from recording start to just "
        "behind the live head.",
        [_SID,
         _q("at", "Comma-separated timestamps, e.g. at=30,1:30,240."),
         _q("count", "Evenly spaced frame count, 2-12 (default 6).", "integer"),
         _q("start", "Sweep start." + _TS),
         _q("end", "Sweep end." + _TS),
         _q("width", "Max width in px (default 640).", "integer"),
         _q("raw", "Timestamps are raw video-file time.", "boolean")],
        tags=["media"]))
    add("/meetings/{session_id}/audio", "get", _op(
        "Full session audio (WAV, supports range requests)", "", [_SID],
        tags=["media"]))
    add("/meetings/{session_id}/audio/clip", "get", _op(
        "A clip of the session audio",
        "Max 15 minutes per clip. format=mp3 requires ffmpeg.",
        [_SID,
         _q("start", "Clip start." + _TS, required=True),
         _q("end", "Clip end (default start+60s)." + _TS),
         _q("format", "wav (default) or mp3.")],
        tags=["media"]))
    add("/meetings/{session_id}/screenshots", "get", _op(
        "Screenshots saved from AI chat frame captures", "", [_SID],
        tags=["media"]))
    add("/meetings/{session_id}/screenshots/{name}", "get", _op(
        "One saved screenshot (JPEG)", "",
        [_SID, _p("name", "File name from the screenshots listing.")],
        tags=["media"]))
    add("/meetings/{session_id}/export", "get", _op(
        "Whole meeting as one document",
        "format=markdown (default) renders metadata + summary + chapters + "
        "notes + full transcript + chat. format=json returns the export "
        "package. save_to_file=1 writes the markdown into the app's tmp dir "
        "and returns the path (useful to grep large meetings).",
        [_SID, _q("format", "markdown (default) or json."),
         _q("save_to_file", "Write to disk and return the path.", "boolean")],
        tags=["meetings"]))

    # Search
    add("/search", "get", _op(
        "Search meetings (hybrid keyword + semantic)",
        "mode=hybrid (default) fuses full-text and embedding similarity via "
        "reciprocal-rank fusion; keyword matches carry snippets labelled "
        "with who said it and when.",
        [_q("q", "The query.", required=True),
         _q("mode", "hybrid (default), keyword, or semantic."),
         _q("match", "Keyword term combination: all (default), any, phrase."),
         _q("limit", "Max results (default 10, max 50).", "integer"),
         _q("min_score", "Semantic similarity floor 0-1 (default 0.25).",
            "number"),
         _q("max_snippets", "Snippets per meeting (default 3).", "integer"),
         *_FILTERS],
        tags=["search"]))
    add("/search/text", "get", _op(
        "Exact substring scan",
        "Finds raw substrings FTS cannot (punctuation, partial words) and "
        "also scans summaries, notes, and chat.",
        [_q("contains", "The exact substring.", required=True),
         _q("scope", "Comma list: transcript, titles, summaries, notes, "
                     "chat, global_chat (default all)."),
         _q("case_sensitive", "Default false.", "boolean"),
         _q("limit", "Max rows (default 100, max 500).", "integer"),
         *_FILTERS],
        tags=["search"]))

    # Speakers / chats
    add("/speakers", "get", _op("Voice library roster with session counts",
                                tags=["speakers"]))
    add("/speakers/{spec}/meetings", "get", _op(
        "Every meeting a speaker appears in", "",
        [_p("spec", "Speaker id or (partial) name.")], tags=["speakers"]))
    add("/chats", "get", _op("Global (cross-meeting) AI chat conversations",
                             tags=["chats"]))
    add("/chats/{conversation_id}", "get", _op(
        "One global chat conversation with messages", "",
        [_p("conversation_id", "Conversation id from /chats.")], tags=["chats"]))

    # Settings / live / recording
    add("/settings", "get", _op("All app settings (secrets masked) plus API "
                                "key status", tags=["settings"]))
    add("/settings/schema", "get", _op(
        "Schema for every settings key: type, default, description, "
        "write rules", tags=["settings"]))
    add("/settings", "patch", _op(
        "Update settings",
        "Validated against the schema. Device/model keys are locked while "
        "recording; some keys apply on next app start (reported as "
        "restart_required).",
        body={"type": "object", "properties": {
            "updates": {"type": "object",
                        "description": "Map of settings key to new value."},
        }}, tags=["settings"]))
    add("/live", "get", _op(
        "Live recording tail",
        "Poll while a meeting is being recorded: fresh segments after a "
        "cursor, elapsed time, current summary, audio levels. live_video "
        "reports whether frames are extractable right now (frame_url points "
        "at t=now).",
        [_q("after_segment_id", "Return only segments newer than this id.",
            "integer"),
         _q("limit", "Max segments (default 50).", "integer"),
         _q("include_summary", "Include the current summary (default true).",
            "boolean")],
        tags=["live"]))
    add("/recording/start", "post", _op(
        "Start a recording (opt-in)",
        "Requires the 'Allow recording control' setting. Opens the app's "
        "session page with autostart, mirroring the tray flow.",
        body={"type": "object", "required": ["confirm"],
              "properties": {"confirm": {"type": "boolean"}}},
        tags=["recording"]))
    add("/recording/stop", "post", _op(
        "Stop the active recording (opt-in)", "",
        body={"type": "object", "required": ["confirm"],
              "properties": {"confirm": {"type": "boolean"}}},
        tags=["recording"]))
    add("/setup/{client}", "post", _op(
        "One-click MCP client setup on this machine",
        "Writes/updates the named client's config to register this app's MCP "
        "server (claude_desktop merges claude_desktop_config.json, "
        "claude_code registers via the CLI at user scope, codex updates "
        "~/.codex/config.toml). Existing configs are merged and backed up, "
        "never clobbered. No request body.",
        [_p("client", "claude_desktop, claude_code, or codex.")],
        tags=["discovery"]))

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Meeting Assistant Agent API",
            "version": API_VERSION,
            "description": (
                "Local REST interface to Meeting Assistant for AI agents: "
                "meetings, diarized transcripts, summaries, notes, chapters, "
                "video frames, search, settings, logs, and live recording "
                "state. Full guide: GET /api/agent/v1/docs."
            ),
        },
        "servers": [{"url": f"{server_url}/api/agent/v1"}],
        "components": {
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer",
                           "description": "Only required when the user has "
                                          "set an Agent API token."},
            },
        },
        "security": [{"bearer": []}, {}],
        "paths": paths,
    }
