#!/usr/bin/env python3
"""Meeting Assistant MCP server - stdio bridge to the local Agent REST API.

Gives MCP clients (Claude Desktop, Claude Code, Codex, and anything else that
speaks the Model Context Protocol) first-class tools over the user's Meeting
Assistant: meetings, diarized transcripts, summaries, notes, chapters, video
frames, audio clips, search, folders, speakers, settings, and logs, plus the
tools to organise the library (folders, bulk moves) and to identify and
label the speakers in recordings with voice, text, calendar and screen
evidence.

Zero dependencies: pure Python stdlib, so it runs with any Python 3.10+
interpreter (the app's venv is ideal but not required). It talks to the
running Meeting Assistant app over localhost HTTP; the app must be running
for tools to return data (tools explain how to start it when it isn't).

Wire-up examples (see docs/AGENT_API.md for the full guide):

  Claude Desktop  (claude_desktop_config.json):
    {"mcpServers": {"meeting-assistant": {
        "command": "C:\\\\path\\\\to\\\\Meeting Assistant\\\\.venv\\\\Scripts\\\\python.exe",
        "args": ["C:\\\\path\\\\to\\\\Meeting Assistant\\\\mcp_server.py"]}}}

  Claude Code:
    claude mcp add meeting-assistant -- <venv python> <this file>

  Codex  (~/.codex/config.toml):
    [mcp_servers.meeting-assistant]
    command = "<venv python>"
    args = ["<this file>"]

CLI:
  python mcp_server.py             run the stdio server (for MCP clients)
  python mcp_server.py --selftest  check connectivity and print a report
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SERVER_NAME = "meeting-assistant"
SERVER_VERSION = "1.1.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_ROOT = Path(__file__).parent

# ── App discovery ─────────────────────────────────────────────────────────────

def _read_env_port() -> int:
    """PORT from the app's .env file, else 6969."""
    env_path = _ROOT / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PORT=") and line[5:].strip().isdigit():
                return int(line[5:].strip())
    except OSError:
        pass
    return 6969


def _base_url() -> str:
    override = os.getenv("MEETING_ASSISTANT_URL", "").strip().rstrip("/")
    if override:
        return override
    return f"http://127.0.0.1:{_read_env_port()}"


def _data_dir() -> Path:
    """Resolve the app's data folder the same way core/paths.py does."""
    pointer = _ROOT / ".data_location"
    try:
        text = pointer.read_text(encoding="utf-8").strip()
        if text and Path(text).is_absolute():
            return Path(text)
    except OSError:
        pass
    return _ROOT / "storage" / "data"


def _api_token() -> str:
    """The user's agent_api_token from settings.json (empty = no auth)."""
    try:
        settings = json.loads((_data_dir() / "settings.json")
                              .read_text(encoding="utf-8"))
        return str(settings.get("agent_api_token") or "").strip()
    except (OSError, ValueError):
        return ""


BASE = _base_url()
API = f"{BASE}/api/agent/v1"


class AppNotRunning(Exception):
    pass


class ApiError(Exception):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status}")


def _http(method: str, path: str, params: dict | None = None,
          body: dict | None = None, timeout: float = 60.0):
    """Call the Agent REST API. Returns parsed JSON, text, or raw bytes."""
    url = f"{API}{path}"
    if params:
        clean = {k: (str(v).lower() if isinstance(v, bool) else v)
                 for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json, text/markdown, text/plain, */*")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    token = _api_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            payload = {"error": f"HTTP {e.code}"}
        raise ApiError(e.code, payload) from None
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise AppNotRunning(str(e)) from None
    if "application/json" in ctype:
        return json.loads(raw.decode("utf-8"))
    if ctype.startswith("text/"):
        return raw.decode("utf-8", "replace")
    return raw  # bytes (images, audio)


# ── Tool definitions ─────────────────────────────────────────────────────────

def _filter_props() -> dict:
    """Shared folder/date/speaker filter properties (combinable freely)."""
    return {
        "folder": {"type": "string", "description": (
            "Restrict to one folder: a folder id, (partial) name, or path "
            "like 'Engineering / Backend'. If ambiguous, the tool returns "
            "the candidate folders; call list_folders to browse them.")},
        "include_subfolders": {"type": "boolean", "default": True,
                               "description": "Also include sessions in "
                               "sub-folders of the matched folder."},
        "speaker": {"type": "string", "description": (
            "Only meetings this person participated in (case-insensitive "
            "partial name; see list_speakers).")},
        "within_days": {"type": "integer", "description":
                        "Only meetings from the last N days."},
        "start_date": {"type": "string", "description":
                       "Earliest meeting date, ISO YYYY-MM-DD."},
        "end_date": {"type": "string", "description":
                     "Latest meeting date, ISO YYYY-MM-DD (whole day included)."},
    }


_TS_DESC = "Accepts seconds (90.5) or a clock string ('1:30', '01:02:03')."
_MID = {"type": "string", "description":
        "The meeting's session id (UUID) from list_meetings or search_meetings."}


def _tool(name: str, description: str, props: dict | None = None,
          required: list | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props or {},
            **({"required": required} if required else {}),
        },
    }


TOOLS: list[dict] = [
    _tool(
        "get_started",
        "START HERE if this is your first time using Meeting Assistant tools. "
        "Returns an orientation guide: what this server is, the live status "
        "of the user's app, how the meeting library is organized (folders, "
        "speakers, counts), a map of every tool with recommended workflows, "
        "and where the full documentation lives. Cheap to call.",
    ),
    _tool(
        "list_meetings",
        "Browse the meeting library, newest first. Every meeting entry has "
        "session_id, title, start/end times, duration, folder path, speaker "
        "names, a summary preview, and media flags (has_audio, has_video, "
        "has_notes). All filters combine (e.g. one folder + last 7 days + "
        "one participant). Use for 'what meetings happened last week' style "
        "browsing; use search_meetings when looking for content.",
        {**_filter_props(),
         "limit": {"type": "integer", "default": 25,
                   "description": "Max meetings (1-500)."},
         "offset": {"type": "integer", "default": 0,
                    "description": "Pagination offset."}},
    ),
    _tool(
        "search_meetings",
        "Search across all meetings. mode='hybrid' (default) fuses full-text "
        "keyword search with semantic (meaning-based) similarity, so it "
        "works for exact phrases AND conceptual queries like 'discussions "
        "about hiring'. Keyword matches include snippets labelled with WHO "
        "said it and WHEN (segment start time), so quotes can be attributed "
        "and jumped to (e.g. pass the time to get_frame). Filters combine "
        "with the query. Use search_text instead for exact substrings with "
        "punctuation (error codes, version numbers).",
        {"query": {"type": "string", "description": "The search query."},
         "mode": {"type": "string", "enum": ["hybrid", "keyword", "semantic"],
                  "default": "hybrid"},
         "match": {"type": "string", "enum": ["all", "any", "phrase"],
                   "default": "all",
                   "description": "Keyword term combining: all terms, any "
                                  "term, or exact phrase."},
         "limit": {"type": "integer", "default": 10,
                   "description": "Max meetings returned (1-50)."},
         "min_score": {"type": "number", "default": 0.25,
                       "description": "Semantic similarity floor (0-1)."},
         **_filter_props()},
        ["query"],
    ),
    _tool(
        "search_text",
        "Exact substring scan across raw stored text. Finds what full-text "
        "search cannot: punctuation-bearing strings ('v2.3.1', 'foo_bar()'), "
        "partial words, and text outside the search index (AI summaries, "
        "user notes, chat history). Results carry the meeting, where the "
        "text lives (kind), an excerpt, and for transcript hits the speaker "
        "and timestamp.",
        {"contains": {"type": "string",
                      "description": "The exact substring to find."},
         "scope": {"type": "string", "description":
                   "Comma list of scopes to scan: transcript, titles, "
                   "summaries, notes, chat, global_chat. Default: all."},
         "case_sensitive": {"type": "boolean", "default": False},
         "limit": {"type": "integer", "default": 50},
         **_filter_props()},
        ["contains"],
    ),
    _tool(
        "get_meeting",
        "Load one meeting as a bundle: metadata, folder, duration, "
        "per-speaker talk-time stats, AI summary, chapter markers, user "
        "notes (as markdown), and media availability. Add "
        "include='transcript' or 'chat' (comma-separated, or 'all') for the "
        "full diarized transcript / AI chat history. For very long meetings "
        "prefer get_transcript (paginated) or export_meeting (file).",
        {"meeting_id": _MID,
         "include": {"type": "string", "description":
                     "Extra parts: 'transcript', 'chat', 'summary_history', "
                     "or 'all'. Default bundle: summary, chapters, speakers, "
                     "notes, media."}},
        ["meeting_id"],
    ),
    _tool(
        "get_transcript",
        "The diarized transcript of a meeting with resolved speaker names. "
        "format='text' (default here) returns '[M:SS] [Speaker] text' lines; "
        "'markdown' interleaves chapter headings; 'json' returns structured "
        "segment rows; 'srt'/'vtt' return subtitles. Slice by time window "
        "(start/end), speaker, or pagination (offset/limit) to keep results "
        "small. A 1-hour meeting is roughly 700 segments.",
        {"meeting_id": _MID,
         "format": {"type": "string",
                    "enum": ["text", "markdown", "json", "srt", "vtt"],
                    "default": "text"},
         "start": {"type": "string", "description": f"Window start. {_TS_DESC}"},
         "end": {"type": "string", "description": f"Window end. {_TS_DESC}"},
         "speaker": {"type": "string", "description":
                     "Only segments spoken by this (partial) speaker name."},
         "offset": {"type": "integer", "description": "Skip N segments."},
         "limit": {"type": "integer", "description":
                   "Max segments (0/omit = all)."}},
        ["meeting_id"],
    ),
    _tool(
        "export_meeting",
        "The whole meeting as one markdown document: metadata, summary, "
        "chapters, notes, full diarized transcript, and chat history. With "
        "save_to_file=true it is written to a local file and the PATH is "
        "returned instead of the content - ideal for long meetings: read or "
        "grep the file instead of pulling everything into context.",
        {"meeting_id": _MID,
         "save_to_file": {"type": "boolean", "default": False}},
        ["meeting_id"],
    ),
    _tool(
        "get_frame",
        "See what was on screen during a meeting: extracts one frame from "
        "the meeting's screen recording at a moment on the MEETING timeline "
        "(same clock as transcript timestamps; the stored video offset is "
        "handled automatically) and returns it as an image. Works on "
        "finished recordings AND live: while a meeting is being recorded, "
        "pass any timestamp up to the live head, or 'now' for what is on "
        "screen this instant. Use transcript or chapter timestamps to pick "
        "moments. Check has_video (or get_live_status.live_video) first.",
        {"meeting_id": _MID,
         "timestamp": {"type": "string",
                       "description": f"The moment to capture. {_TS_DESC} "
                                      "Pass 'now' during a live recording "
                                      "for the current screen."},
         "width": {"type": "integer", "default": 1024,
                   "description": "Max image width in px (160-1920)."}},
        ["meeting_id", "timestamp"],
    ),
    _tool(
        "get_frames",
        "A batch of frames from a meeting's screen recording, returned as "
        "images. Either pass explicit timestamps, or pass count to sweep "
        "evenly across the recording (a visual overview of the whole "
        "meeting). During a live recording the sweep covers recording start "
        "through the live head, and 'now' is a valid timestamp. Max 8 "
        "frames per call; use width<=640 to keep results light.",
        {"meeting_id": _MID,
         "timestamps": {"type": "array", "items": {"type": "string"},
                        "description": f"Explicit moments. {_TS_DESC}"},
         "count": {"type": "integer",
                   "description": "Evenly spaced frame count (2-8) when "
                                  "timestamps is omitted."},
         "start": {"type": "string", "description": "Sweep start (optional)."},
         "end": {"type": "string", "description": "Sweep end (optional)."},
         "width": {"type": "integer", "default": 512}},
        ["meeting_id"],
    ),
    _tool(
        "get_audio_clip",
        "Cut a clip from the meeting's audio recording and save it as a "
        "local WAV file. Returns the file path plus a URL - useful for "
        "handing a specific exchange to an external transcription/audio "
        "tool, or for the user to listen to. Max 15 minutes per clip.",
        {"meeting_id": _MID,
         "start": {"type": "string",
                   "description": f"Clip start. {_TS_DESC}"},
         "end": {"type": "string", "description":
                 f"Clip end (default start+60s). {_TS_DESC}"}},
        ["meeting_id", "start"],
    ),
    _tool(
        "get_meeting_media",
        "Media inventory for one meeting: audio (duration, sample rate, "
        "size), video (duration, size, the video offset explained), saved "
        "screenshots, and note attachments, each with fetchable URLs.",
        {"meeting_id": _MID},
        ["meeting_id"],
    ),
    _tool(
        "update_meeting",
        "Rename a meeting and/or move it to a folder. folder accepts an id, "
        "name, or path; null/empty unfiles it. Never deletes anything.",
        {"meeting_id": _MID,
         "title": {"type": "string", "description": "New title (optional)."},
         "folder": {"type": ["string", "null"], "description":
                    "Target folder id/name/path, or null to unfile."}},
        ["meeting_id"],
    ),
    _tool(
        "append_meeting_notes",
        "Append a block to a meeting's Notes pane (visible in the app). "
        "Strictly additive: existing user notes are never modified. Good "
        "for dropping action items, decisions, or analysis back into the "
        "meeting record.",
        {"meeting_id": _MID,
         "text": {"type": "string", "description": "Plain text to append."},
         "heading": {"type": "string", "description":
                     "Optional small heading above the block."}},
        ["meeting_id", "text"],
    ),
    _tool(
        "add_chapter",
        "Add a chapter marker (a titled timestamp) to a meeting. Chapters "
        "appear on the app's timeline and get interleaved into markdown "
        "transcripts.",
        {"meeting_id": _MID,
         "title": {"type": "string"},
         "start_time": {"type": "string",
                        "description": f"Where it starts. {_TS_DESC}"}},
        ["meeting_id", "title", "start_time"],
    ),
    _tool(
        "list_folders",
        "The user's folder tree: every folder with its id, full path (e.g. "
        "'Engineering / Backend'), parent, and session counts (direct and "
        "including sub-folders). Call this before filtering by folder when "
        "the user names a project/team/client approximately.",
    ),
    _tool(
        "create_folder",
        "Create a new folder, optionally nested under a parent (id, name, "
        "or path). Use move_meetings (many) or update_meeting (one) to file "
        "meetings into it.",
        {"name": {"type": "string"},
         "parent": {"type": "string", "description":
                    "Parent folder id/name/path (optional, top-level if omitted)."}},
        ["name"],
    ),
    _tool(
        "update_folder",
        "Rename a folder and/or move it under another folder (or to the top "
        "level). folder accepts an id, name or path; parent accepts the same, "
        "or null for the top level. A folder is never moved into its own "
        "sub-tree, and nothing is deleted. Use it with create_folder and "
        "move_meetings to reshape how the library is organised.",
        {"folder": {"type": "string",
                    "description": "The folder to change: id, name or path."},
         "name": {"type": "string", "description": "New name (optional)."},
         "parent": {"type": ["string", "null"], "description":
                    "New parent folder id/name/path, or null for the top level "
                    "(optional)."}},
        ["folder"],
    ),
    _tool(
        "move_meetings",
        "Move several meetings into one folder in a single call, or unfile "
        "them with folder=null. A meeting lives in at most one folder, so "
        "this is how a library gets organised: list_meetings or "
        "search_meetings to choose, create_folder if needed, then "
        "move_meetings. Reports which ids moved, which were already there and "
        "which do not exist. Never deletes.",
        {"meeting_ids": {"type": "array", "items": {"type": "string"},
                         "description": "Session ids to move (1-500)."},
         "folder": {"type": ["string", "null"], "description":
                    "Target folder id, name or path; null unfiles."}},
        ["meeting_ids", "folder"],
    ),
    _tool(
        "list_speakers",
        "The voice library roster: every known speaker with id, name, how "
        "many meetings they appear in, and when they were last heard. "
        "Names can repeat (old duplicate profiles); prefer the id with the "
        "highest session_count.",
    ),
    _tool(
        "get_speaker_meetings",
        "Every meeting a specific person appears in, with titles, dates, "
        "folders, summaries, and how many segments they spoke. Accepts a "
        "speaker id or (partial) name; if the name is ambiguous the "
        "candidates are returned with session counts.",
        {"speaker": {"type": "string",
                     "description": "Speaker id or (partial) name."}},
        ["speaker"],
    ),
    _tool(
        "list_meetings_needing_speakers",
        "The speaker work queue: meetings that still have unnamed speakers "
        "('Speaker 3') with real talk time, or whose speaker count disagrees "
        "with the calendar invite. Newest first, with the usual folder/date "
        "filters, so you can work through one folder or one week at a time. "
        "Each entry carries attention {unresolved, found, expected}. Start "
        "here when asked to name, fix or clean up speakers, then call "
        "review_meeting_speakers on each meeting.",
        {**_filter_props(),
         "reason": {"type": "string", "enum": ["any", "unresolved", "mismatch"],
                    "default": "any", "description":
                    "any: every meeting needing work; unresolved: unnamed "
                    "speakers only; mismatch: speaker count differs from the "
                    "calendar (over-split or under-split voices)."},
         "limit": {"type": "integer", "default": 25},
         "offset": {"type": "integer", "default": 0}},
    ),
    _tool(
        "review_meeting_speakers",
        "The evidence pack for naming a meeting's speakers, in one call. For "
        "every speaker key: status (named / unnamed / minor / noise / me), "
        "talk time and words; the lines most likely to identify them "
        "(self-introductions flagged); names other people call them "
        "('thanks, Dana'); the closest Voice Library profiles with similarity "
        "and a verdict (strong / clear / possible / weak / none, the app's own "
        "live thresholds); how close the voice is to the other keys in the "
        "meeting (likely_same_voice means the diarizer split one person); and "
        "the moments to pull screen frames from when there is a recording. "
        "Also the calendar's attendees (and which are already assigned), the "
        "attention state, and disclosures saying how far each kind of "
        "evidence can be trusted: read them before deciding. By default the "
        "unnamed speakers get the full evidence and everyone else one compact "
        "line under 'others' (with closest_other, the key their voice is "
        "nearest to); pass speaker_key to focus on one speaker, or "
        "include_named=true for the full evidence on every speaker. "
        "include_matches=false skips the voice work (fast). The first review "
        "of a compressed meeting can take a minute while its audio is decoded.",
        {"meeting_id": _MID,
         "speaker_key": {"type": "string", "description":
                         "Focus on one speaker key, e.g. 'Speaker 3' (optional)."},
         "include_matches": {"type": "boolean", "default": True,
                             "description": "Consult the voice library and "
                                            "compute in-meeting proximity."},
         "include_named": {"type": "boolean", "default": False,
                           "description": "Full evidence for already named "
                                          "speakers too (large on long meetings)."},
         "quotes": {"type": "integer", "default": 4,
                    "description": "Identifying lines per speaker (1-12)."}},
        ["meeting_id"],
    ),
    _tool(
        "get_speaker_frames",
        "Screen-recording frames from moments a given speaker was talking, "
        "returned as images captioned with what they were saying. Use them to "
        "read the meeting window: a highlighted or outlined tile, a 'Name is "
        "speaking' banner, or a presenter name says who the voice is. "
        "Highlights can lag the audio by a second or two, so weigh several "
        "frames, and fetch frames for an already named speaker as well to "
        "learn the layout. Needs the meeting to have a screen recording "
        "(has_video).",
        {"meeting_id": _MID,
         "speaker_key": {"type": "string",
                         "description": "Diarizer key from review_meeting_speakers."},
         "count": {"type": "integer", "default": 3,
                   "description": "Frames, 1-6."},
         "width": {"type": "integer", "default": 768,
                   "description": "Max image width in px (160-1280)."}},
        ["meeting_id", "speaker_key"],
    ),
    _tool(
        "label_speaker",
        "Apply a decision about one or more speaker keys in a meeting. Exactly "
        "one action per call: name (a person's name; links or creates their "
        "Voice Library profile), profile_id (link an existing profile by id, "
        "the safest form after a library match), same_as (this key is the "
        "same voice as another key in this meeting: merges them under that "
        "speaker's name), noise=true (not a person: crosstalk, music, a bad "
        "fragment), or reset=true (back to the diarizer default, unlinked). "
        "The change lands in this meeting only, shows in the app at once, and "
        "never touches other meetings. reinforce=true also trains the voice "
        "profile on this audio: set it only when the identity is certain (the "
        "user confirmed it, a self-introduction, a name on screen), never on "
        "a voice match alone. Always pass evidence: one sentence saying why; "
        "it is logged. Label only on strong evidence (see the review's "
        "disclosures); report weaker cases to the user instead of guessing. "
        "The owner's own microphone speaker cannot be relabelled.",
        {"meeting_id": _MID,
         "speaker_keys": {"type": "array", "items": {"type": "string"},
                          "description": "One or more keys, e.g. ['Speaker 3', "
                                         "'Speaker 7'] (from review_meeting_speakers)."},
         "name": {"type": "string", "description": "Assign this person."},
         "profile_id": {"type": "string", "description":
                        "Link this Voice Library profile (its name is used)."},
         "same_as": {"type": "string", "description":
                     "Another key in this meeting that is the same voice."},
         "noise": {"type": "boolean"},
         "reset": {"type": "boolean"},
         "reinforce": {"type": "boolean", "default": False},
         "evidence": {"type": "string", "description":
                      "Why you are confident, in one sentence."}},
        ["meeting_id", "speaker_keys"],
    ),
    _tool(
        "relabel_segment",
        "Reattribute one transcript line to another speaker in the same "
        "meeting (speaker_key), or give that single line a one-off name. For "
        "the odd line the diarizer handed to the wrong person; to rename a "
        "whole speaker use label_speaker. Segment ids come from "
        "get_transcript(format='json') or the quotes in "
        "review_meeting_speakers.",
        {"meeting_id": _MID,
         "segment_id": {"type": "integer"},
         "speaker_key": {"type": "string",
                         "description": "The speaker this line belongs to."},
         "name": {"type": "string", "description":
                  "A one-off label for the line instead of a key."},
         "reinforce": {"type": "boolean", "default": False}},
        ["meeting_id", "segment_id"],
    ),
    _tool(
        "get_speaker_profile",
        "One Voice Library profile in depth: name, how many voice samples it "
        "holds, the meetings it appears in (recent ones listed), whether it "
        "is the owner's own profile, other profiles whose voice is confusable "
        "with it, and how many meeting labels carry its name without being "
        "linked to it. Accepts an id or (partial) name; an ambiguous name "
        "returns candidates.",
        {"speaker": {"type": "string",
                     "description": "Profile id or (partial) name."}},
        ["speaker"],
    ),
    _tool(
        "rename_speaker_profile",
        "Rename a Voice Library profile. Every meeting label linked to it "
        "follows, so this fixes a misspelled name everywhere at once. Refuses "
        "a name another profile already has (merge them instead) and never "
        "renames the owner's own profile. Confirm with the user first.",
        {"profile_id": {"type": "string"},
         "name": {"type": "string", "description": "The corrected name."}},
        ["profile_id", "name"],
    ),
    _tool(
        "merge_speaker_profiles",
        "Fold one Voice Library profile into another when two profiles are "
        "the same person (a duplicate name, or a pair the user confirms). "
        "Voice samples and every meeting label move to keep_id; the other "
        "profile is removed. IRREVERSIBLE: call once without confirm to see "
        "what would move, show that to the user, and pass confirm=true only "
        "after they agree. The owner's own profile is never merged.",
        {"keep_id": {"type": "string", "description": "The profile to keep."},
         "merge_id": {"type": "string", "description": "The profile to fold in."},
         "confirm": {"type": "boolean", "default": False}},
        ["keep_id", "merge_id"],
    ),
    _tool(
        "get_voice_library_health",
        "Read-only report on the Voice Library: duplicate profiles (same "
        "name), profiles whose voice samples look like another person's, "
        "split profiles that hold two voices, and confusable pairs. Nothing "
        "is changed. Use it to explain a doubtful voice match or to propose "
        "merges (merge_speaker_profiles) to the user.",
    ),
    _tool(
        "plan_speaker_relabel",
        "Plan a bulk rename of a speaker name across meetings (a misspelling "
        "or 'Speaker 3' to a real name) WITHOUT changing anything. Returns a "
        "plan token, a summary of every meeting and label that would change, "
        "and warnings (voice-profile merges, hand-set lines that will not "
        "change). Show the plan to the user; after they confirm, call "
        "apply_speaker_relabel with the token. scope='session' with "
        "meeting_id limits it to one meeting; the folder/date filters limit "
        "a library-wide plan.",
        {"from_name": {"type": "string"},
         "to_name": {"type": "string"},
         "scope": {"type": "string", "enum": ["library", "session"],
                   "default": "library"},
         "meeting_id": {"type": "string",
                        "description": "Required when scope is 'session'."},
         "match": {"type": "string", "enum": ["exact", "contains"],
                   "default": "exact"},
         **_filter_props()},
        ["from_name", "to_name"],
    ),
    _tool(
        "apply_speaker_relabel",
        "Apply a plan from plan_speaker_relabel, only after the user has "
        "confirmed that exact plan. Tokens are single use and expire after "
        "ten minutes; the response lists what changed.",
        {"token": {"type": "string"},
         "confirm": {"type": "boolean", "description": "Must be true."}},
        ["token", "confirm"],
    ),
    _tool(
        "cancel_speaker_relabel",
        "Drop a planned relabel so its token can never be applied.",
        {"token": {"type": "string"}},
        ["token"],
    ),
    _tool(
        "get_ai_chats",
        "The user's saved AI conversations inside the app. Without "
        "conversation_id: lists the cross-meeting 'Global Chat' "
        "conversations. With one: returns that conversation's messages. "
        "Per-meeting chat history comes from get_meeting with "
        "include='chat'.",
        {"conversation_id": {"type": "string", "description":
                             "A conversation id from the listing (optional)."}},
    ),
    _tool(
        "get_live_status",
        "Live app state right now: whether a meeting is being recorded, "
        "which models are loaded, and - during a recording - the fresh "
        "transcript tail, elapsed time, current summary, and audio levels. "
        "Pass after_segment_id (from the previous call's last_segment_id) "
        "to poll incrementally while a meeting is happening. If live_video "
        "is true, get_frame(timestamp='now') shows the screen this instant.",
        {"after_segment_id": {"type": "integer", "description":
                              "Only transcript segments newer than this id."},
         "limit": {"type": "integer", "default": 50,
                   "description": "Max tail segments."}},
    ),
    _tool(
        "get_app_info",
        "Everything about this Meeting Assistant instance: app version and "
        "platform, uptime, data folder location, library counts (meetings, "
        "segments, folders, speakers), loaded models (Whisper preset, "
        "diarizer), AI provider/model config, storage usage, and recent "
        "activity analytics.",
        {"include_stats": {"type": "boolean", "default": True,
                           "description": "Also include analytics + storage "
                                          "usage (slightly slower)."}},
    ),
    _tool(
        "get_logs",
        "Application logs captured since app start (transcription, "
        "diarization, AI calls, recording lifecycle, agent requests). "
        "Filter by severity, component tag, or substring. Use to diagnose "
        "'why did X fail' questions about the app.",
        {"limit": {"type": "integer", "default": 100,
                   "description": "Max entries (newest kept)."},
         "level": {"type": "string", "enum": ["info", "warn", "error"],
                   "description": "Minimum severity."},
         "tag": {"type": "string", "description":
                 "Component tag: app, recording, whisper, transcriber, "
                 "diarizer, ai, summary, storage, agent, ..."},
         "contains": {"type": "string",
                      "description": "Substring of the message."}},
    ),
    _tool(
        "get_settings",
        "All app settings with current values (secrets masked), API-key "
        "status, the data folder location, and a full schema describing "
        "every key (type, default, description, write rules).",
    ),
    _tool(
        "update_settings",
        "Change app settings. Values are validated against the schema; "
        "device/model keys are locked while recording, and some keys apply "
        "on next app start (reported back as restart_required). AI "
        "provider/model changes take effect immediately. Confirm with the "
        "user before changing settings they did not ask you to change.",
        {"updates": {"type": "object", "description":
                     "Map of settings key to new value, e.g. "
                     "{\"auto_summary\": false}. Keys: see get_settings."}},
        ["updates"],
    ),
    _tool(
        "start_recording",
        "Start recording a meeting (OPT-IN: fails unless the user enabled "
        "'Allow recording control' in Settings > Agent API). Sends a start "
        "command to the app window, which begins capturing system audio, "
        "microphone, and (if configured) the screen. Only call when the user "
        "explicitly asks to record.",
        {"confirm": {"type": "boolean", "description":
                     "Must be true; acknowledges a recording will start."}},
        ["confirm"],
    ),
    _tool(
        "stop_recording",
        "Stop the active recording (OPT-IN: same setting as "
        "start_recording). The app finalizes the session afterwards "
        "(cleanup, auto-title) - poll get_live_status to see it complete.",
        {"confirm": {"type": "boolean"}},
        ["confirm"],
    ),
]


# ── Tool execution ────────────────────────────────────────────────────────────

def _text(content: str) -> list[dict]:
    return [{"type": "text", "text": content}]


def _json_text(payload) -> list[dict]:
    return _text(json.dumps(payload, indent=2, ensure_ascii=False))


def _filters(a: dict) -> dict:
    return {k: a.get(k) for k in ("folder", "include_subfolders", "speaker",
                                  "within_days", "start_date", "end_date")}


def _clips_dir() -> Path:
    p = _data_dir() / "tmp" / "agent_clips"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _get_started() -> list[dict]:
    lines = ["# Meeting Assistant - agent orientation\n"]
    try:
        info = _http("GET", "/system/info", timeout=15)
        lib = info.get("library", {})
        status = _http("GET", "/system/status", timeout=10)
        lines += [
            "The user's local meeting recorder/transcriber. Everything below "
            "runs on their machine.\n",
            f"- App version: {info.get('version', {}).get('commit')} "
            f"({info.get('version', {}).get('commit_date', '')[:10]}), "
            f"uptime {round(info.get('process', {}).get('uptime_sec', 0) / 60)} min",
            f"- Library: {lib.get('sessions')} meetings, {lib.get('folders')} "
            f"folders, {lib.get('global_speakers')} known speakers, "
            f"{lib.get('segments')} transcript segments",
            f"- Recording right now: {status.get('recording')}"
            + (f" (session {status.get('session_id')})" if status.get('recording') else ""),
            f"- Whisper: {info.get('models', {}).get('model_info') or 'loading'}; "
            f"semantic search ready: {info.get('semantic_search', {}).get('ready')}",
            f"- AI provider: {info.get('ai', {}).get('provider')} "
            f"({info.get('ai', {}).get('model')})",
            f"- REST docs (markdown): GET {API}/docs   |   OpenAPI: {API}/openapi.json\n",
        ]
    except AppNotRunning:
        lines += [_not_running_text(), ""]

    lines += [
        "## Tool map",
        "- Discover: list_meetings (browse), search_meetings (hybrid keyword+semantic),",
        "  search_text (exact substrings incl. notes/summaries/chat), list_folders,",
        "  list_speakers, get_speaker_meetings",
        "- Read one meeting: get_meeting (bundle) -> get_transcript (sliceable) ->",
        "  export_meeting (whole thing as markdown, optionally saved to a file)",
        "- See/hear it: get_frame / get_frames (screen recording images),",
        "  get_audio_clip (WAV file), get_meeting_media (inventory)",
        "- Write back (additive only): append_meeting_notes, add_chapter",
        "- Organise: update_meeting (rename/move one), move_meetings (many),",
        "  create_folder, update_folder (rename/move a folder). Nothing deletes.",
        "- Speakers: list_meetings_needing_speakers -> review_meeting_speakers",
        "  -> get_speaker_frames (see the screen) -> label_speaker / relabel_segment;",
        "  the library: get_speaker_profile, rename_speaker_profile,",
        "  merge_speaker_profiles (confirm), get_voice_library_health,",
        "  plan_speaker_relabel -> apply_speaker_relabel (confirm) / cancel",
        "- Operate the app: get_app_info, get_live_status (live transcript tail),",
        "  get_logs, get_settings / update_settings, get_ai_chats,",
        "  start_recording / stop_recording (opt-in)",
        "",
        "## Recommended workflows",
        "1. 'What did we decide about X?' -> search_meetings(query=X) -> "
        "get_meeting on the top hit -> quote snippets with speaker + timestamp.",
        "2. 'Catch me up on project Y' -> list_folders -> list_meetings("
        "folder=Y, within_days=14) -> get_meeting summaries.",
        "3. 'What was on screen when they showed the dashboard?' -> "
        "search_meetings for the moment -> get_frame(meeting_id, timestamp "
        "from the matching snippet).",
        "4. Deep-dive a long meeting without blowing context -> "
        "export_meeting(save_to_file=true) -> read/grep the file locally.",
        "5. Live meeting -> get_live_status, then poll with after_segment_id.",
        "6. 'Name the speakers in my recent meetings' -> list_meetings_needing_speakers",
        "   -> review_meeting_speakers per meeting -> for each unnamed key weigh the",
        "   evidence: a self-introduction, a 'strong'/'clear' library match, or a",
        "   screen frame that names them (get_speaker_frames) is enough to",
        "   label_speaker; 'possible' plus one consistent calendar attendee is enough",
        "   when you say so in evidence; keys with likely_same_voice get same_as;",
        "   anything weaker goes back to the user as a question, never a guess.",
        "   Set reinforce only when the identity is certain.",
        "7. 'Organise my meetings by client' -> list_folders -> create_folder /",
        "   update_folder for the structure -> search or list to pick meetings ->",
        "   move_meetings.",
        "",
        "## Conventions",
        "- meeting_id = session_id (UUID) returned by every listing/search tool.",
        "- Timestamps accept seconds (90.5) or clock strings ('1:30', '01:02:03')",
        "  and are on the meeting/transcript timeline; video offset is handled.",
        "- Folder/date/speaker filters combine on every browse/search tool.",
        "- Nothing here can delete meetings, folders, or notes. The one irreversible",
        "  action is merge_speaker_profiles, which needs confirm=true.",
        "- Speaker labels are per meeting and visible in the app at once; every",
        "  label_speaker call should carry evidence text.",
    ]
    return _text("\n".join(lines))


def _not_running_text() -> str:
    return (
        "Meeting Assistant does not appear to be running "
        f"(no response at {BASE}).\n"
        "Ask the user to start it: on Windows run launch.bat (or the "
        "'Meeting Assistant' Start Menu entry); on macOS run "
        "launch.command. If it runs on a non-default port, set the "
        "MEETING_ASSISTANT_URL environment variable for this MCP server."
    )


def call_tool(name: str, a: dict) -> tuple[list[dict], bool]:
    """Execute one tool. Returns (content, is_error)."""
    if name == "get_started":
        return _get_started(), False

    if name == "get_app_info":
        info = _http("GET", "/system/info")
        if a.get("include_stats", True):
            try:
                info["stats"] = _http("GET", "/system/stats", timeout=30)
            except ApiError:
                pass
        return _json_text(info), False

    if name == "get_live_status":
        params = {"after_segment_id": a.get("after_segment_id"),
                  "limit": a.get("limit")}
        return _json_text(_http("GET", "/live", params)), False

    if name == "list_folders":
        return _json_text(_http("GET", "/folders")), False

    if name == "create_folder":
        body = {"name": a.get("name"), "parent": a.get("parent")}
        return _json_text(_http("POST", "/folders", body=body)), False

    if name == "update_folder":
        resolved = _http("GET", "/folders/resolve", {"q": a.get("folder")})
        if not resolved.get("resolved"):
            return _json_text(resolved), True
        fid = resolved["folder"]["id"]
        body = {}
        if a.get("name") is not None:
            body["name"] = a["name"]
        if "parent" in a:
            body["parent"] = a["parent"]
        return _json_text(_http("PATCH", f"/folders/{fid}", body=body)), False

    if name == "move_meetings":
        body = {"meeting_ids": a.get("meeting_ids") or [], "folder": a.get("folder")}
        return _json_text(_http("POST", "/meetings/move", body=body)), False

    if name == "list_meetings_needing_speakers":
        params = {**_filters(a), "reason": a.get("reason"),
                  "limit": a.get("limit", 25), "offset": a.get("offset")}
        return _json_text(_http("GET", "/speakers/queue", params)), False

    if name == "review_meeting_speakers":
        mid = a.get("meeting_id")
        params = {"speaker_key": a.get("speaker_key"),
                  "matches": a.get("include_matches", True),
                  "quotes": a.get("quotes"),
                  "detail": "all" if a.get("include_named") else None}
        return _json_text(_http("GET", f"/meetings/{mid}/speakers/review", params,
                                timeout=600)), False

    if name == "get_speaker_frames":
        mid = a.get("meeting_id")
        key = urllib.parse.quote(str(a.get("speaker_key") or ""), safe="")
        params = {"count": max(1, min(6, int(a.get("count") or 3))),
                  "width": max(160, min(1280, int(a.get("width") or 768)))}
        result = _http("GET", f"/meetings/{mid}/speakers/{key}/frames", params,
                       timeout=120)
        content: list[dict] = [{"type": "text", "text": (
            f"Frames while {result.get('speaker_name')} ({result.get('speaker_key')}) "
            f"was talking. {result.get('how_to_read', '')}")}]
        ok_count = 0
        for fr in result.get("frames", []):
            said = fr.get("text") or ""
            if fr.get("jpeg_base64"):
                ok_count += 1
                content.append({"type": "image", "data": fr["jpeg_base64"],
                                "mimeType": "image/jpeg"})
                content.append({"type": "text", "text":
                                f"^ {fr['t']}s (turn {fr.get('start')}s to "
                                f"{fr.get('end')}s): \"{said}\""})
            else:
                content.append({"type": "text", "text":
                                f"(no frame at {fr['t']}s: {fr.get('note') or 'not extractable'})"})
        if not ok_count:
            return content + [{"type": "text", "text":
                               "No frames could be extracted for this speaker."}], True
        return content, False

    if name == "label_speaker":
        mid = a.get("meeting_id")
        body = {"speaker_keys": a.get("speaker_keys") or [],
                "reinforce": bool(a.get("reinforce")),
                "evidence": a.get("evidence")}
        for src, dst in (("name", "name"), ("profile_id", "global_id"),
                         ("same_as", "same_as"), ("noise", "noise"), ("reset", "reset")):
            if a.get(src):
                body[dst] = a[src]
        return _json_text(_http("POST", f"/meetings/{mid}/speakers/label",
                                body=body, timeout=120)), False

    if name == "relabel_segment":
        mid = a.get("meeting_id")
        seg = int(a.get("segment_id") or 0)
        body = {"speaker_key": a.get("speaker_key"), "name": a.get("name"),
                "reinforce": bool(a.get("reinforce"))}
        return _json_text(_http("POST", f"/meetings/{mid}/segments/{seg}/speaker",
                                body=body)), False

    if name == "get_speaker_profile":
        spec = urllib.parse.quote(str(a.get("speaker") or ""), safe="")
        return _json_text(_http("GET", f"/speakers/{spec}")), False

    if name == "rename_speaker_profile":
        pid = urllib.parse.quote(str(a.get("profile_id") or ""), safe="")
        return _json_text(_http("PATCH", f"/speakers/{pid}",
                                body={"name": a.get("name")})), False

    if name == "merge_speaker_profiles":
        keep = urllib.parse.quote(str(a.get("keep_id") or ""), safe="")
        body = {"source_id": a.get("merge_id"), "confirm": bool(a.get("confirm"))}
        return _json_text(_http("POST", f"/speakers/{keep}/merge", body=body,
                                timeout=120)), False

    if name == "get_voice_library_health":
        return _json_text(_http("GET", "/speakers/library/health", timeout=120)), False

    if name == "plan_speaker_relabel":
        body = {**_filters(a), "from_name": a.get("from_name"),
                "to_name": a.get("to_name"), "scope": a.get("scope") or "library",
                "session_id": a.get("meeting_id"), "match": a.get("match") or "exact"}
        body = {k: v for k, v in body.items() if v is not None}
        return _json_text(_http("POST", "/speakers/relabel/plan", body=body,
                                timeout=120)), False

    if name == "apply_speaker_relabel":
        body = {"token": a.get("token"), "confirm": bool(a.get("confirm"))}
        return _json_text(_http("POST", "/speakers/relabel/apply", body=body,
                                timeout=600)), False

    if name == "cancel_speaker_relabel":
        return _json_text(_http("POST", "/speakers/relabel/cancel",
                                body={"token": a.get("token")})), False

    if name == "list_meetings":
        params = {**_filters(a), "limit": a.get("limit", 25),
                  "offset": a.get("offset")}
        return _json_text(_http("GET", "/meetings", params)), False

    if name == "search_meetings":
        params = {**_filters(a), "q": a.get("query"), "mode": a.get("mode"),
                  "match": a.get("match"), "limit": a.get("limit"),
                  "min_score": a.get("min_score")}
        return _json_text(_http("GET", "/search", params)), False

    if name == "search_text":
        params = {**_filters(a), "contains": a.get("contains"),
                  "scope": a.get("scope"),
                  "case_sensitive": a.get("case_sensitive"),
                  "limit": a.get("limit")}
        return _json_text(_http("GET", "/search/text", params)), False

    if name == "get_meeting":
        mid = a.get("meeting_id")
        params = {"include": a.get("include")}
        return _json_text(_http("GET", f"/meetings/{mid}", params)), False

    if name == "get_transcript":
        mid = a.get("meeting_id")
        fmt = a.get("format") or "text"
        params = {"format": fmt, "start": a.get("start"), "end": a.get("end"),
                  "speaker": a.get("speaker"), "offset": a.get("offset"),
                  "limit": a.get("limit")}
        result = _http("GET", f"/meetings/{mid}/transcript", params)
        if isinstance(result, (dict, list)):
            return _json_text(result), False
        return _text(result if isinstance(result, str)
                     else result.decode("utf-8", "replace")), False

    if name == "export_meeting":
        mid = a.get("meeting_id")
        params = {"format": "markdown",
                  "save_to_file": bool(a.get("save_to_file"))}
        result = _http("GET", f"/meetings/{mid}/export", params, timeout=120)
        if isinstance(result, dict):  # save_to_file path payload
            return _json_text(result), False
        return _text(result if isinstance(result, str)
                     else result.decode("utf-8", "replace")), False

    if name == "get_frame":
        mid = a.get("meeting_id")
        width = max(160, min(1920, int(a.get("width") or 1024)))
        params = {"t": a.get("timestamp"), "width": width, "format": "base64"}
        result = _http("GET", f"/meetings/{mid}/frame", params, timeout=60)
        caption = (f"Frame at {result.get('t')}s on the meeting timeline "
                   f"(source: {result.get('source', 'video')}, "
                   f"{result.get('bytes')} bytes)")
        if result.get("note"):
            caption += f" - {result['note']}"
        return [
            {"type": "image", "data": result["jpeg_base64"],
             "mimeType": "image/jpeg"},
            {"type": "text", "text": caption},
        ], False

    if name == "get_frames":
        mid = a.get("meeting_id")
        width = max(160, min(1280, int(a.get("width") or 512)))
        params: dict = {"width": width}
        stamps = a.get("timestamps")
        if stamps:
            params["at"] = ",".join(str(t) for t in stamps[:8])
        else:
            params["count"] = max(2, min(8, int(a.get("count") or 6)))
            params["start"] = a.get("start")
            params["end"] = a.get("end")
        result = _http("GET", f"/meetings/{mid}/frames", params, timeout=120)
        content: list[dict] = []
        ok_count = 0
        for fr in result.get("frames", []):
            if fr.get("jpeg_base64"):
                ok_count += 1
                content.append({"type": "image", "data": fr["jpeg_base64"],
                                "mimeType": "image/jpeg"})
                content.append({"type": "text",
                                "text": f"^ frame at {fr['t']}s"})
            else:
                content.append({"type": "text",
                                "text": f"(no frame at {fr['t']}s)"})
        if not ok_count:
            return _text("No frames could be extracted for those times."), True
        return content, False

    if name == "get_audio_clip":
        mid = a.get("meeting_id")
        params = {"start": a.get("start"), "end": a.get("end"),
                  "format": "wav"}
        blob = _http("GET", f"/meetings/{mid}/audio/clip", params, timeout=120)
        if isinstance(blob, (str, dict)):
            return _json_text(blob), True
        safe_start = str(a.get("start")).replace(":", "-")
        out = _clips_dir() / f"{mid[:8]}_{safe_start}.wav"
        out.write_bytes(blob)
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v})
        return _json_text({
            "saved_to": str(out),
            "size_bytes": len(blob),
            "url": f"{API}/meetings/{mid}/audio/clip?{qs}",
            "note": "WAV file saved locally; give the path or URL to the "
                    "user or to an audio-capable tool.",
        }), False

    if name == "get_meeting_media":
        mid = a.get("meeting_id")
        return _json_text(_http("GET", f"/meetings/{mid}/media")), False

    if name == "update_meeting":
        mid = a.get("meeting_id")
        body = {}
        if a.get("title") is not None:
            body["title"] = a["title"]
        if "folder" in a:
            body["folder"] = a["folder"]
        return _json_text(_http("PATCH", f"/meetings/{mid}", body=body)), False

    if name == "append_meeting_notes":
        mid = a.get("meeting_id")
        body = {"text": a.get("text"), "heading": a.get("heading")}
        return _json_text(_http("POST", f"/meetings/{mid}/notes/append",
                                body=body)), False

    if name == "add_chapter":
        mid = a.get("meeting_id")
        body = {"title": a.get("title"), "start_time": a.get("start_time")}
        return _json_text(_http("POST", f"/meetings/{mid}/chapters",
                                body=body)), False

    if name == "list_speakers":
        return _json_text(_http("GET", "/speakers")), False

    if name == "get_speaker_meetings":
        spec = urllib.parse.quote(str(a.get("speaker") or ""), safe="")
        return _json_text(_http("GET", f"/speakers/{spec}/meetings")), False

    if name == "get_ai_chats":
        cid = a.get("conversation_id")
        path = f"/chats/{cid}" if cid else "/chats"
        return _json_text(_http("GET", path)), False

    if name == "get_logs":
        params = {"limit": a.get("limit", 100), "level": a.get("level"),
                  "tag": a.get("tag"), "contains": a.get("contains")}
        return _json_text(_http("GET", "/system/logs", params)), False

    if name == "get_settings":
        merged = _http("GET", "/settings")
        merged["schema"] = _http("GET", "/settings/schema").get("settings")
        return _json_text(merged), False

    if name == "update_settings":
        body = {"updates": a.get("updates") or {}}
        return _json_text(_http("PATCH", "/settings", body=body)), False

    if name == "start_recording":
        body = {"confirm": bool(a.get("confirm"))}
        return _json_text(_http("POST", "/recording/start", body=body)), False

    if name == "stop_recording":
        body = {"confirm": bool(a.get("confirm"))}
        return _json_text(_http("POST", "/recording/stop", body=body)), False

    return _text(f"Unknown tool: {name}"), True


# ── MCP resources (docs exposed as a readable resource) ──────────────────────

RESOURCES = [{
    "uri": "meeting-assistant://docs/agent-guide",
    "name": "agent-guide",
    "title": "Meeting Assistant agent guide",
    "description": "Full REST + MCP documentation (markdown).",
    "mimeType": "text/markdown",
}]


def read_resource(uri: str) -> dict:
    if uri == "meeting-assistant://docs/agent-guide":
        text = _http("GET", "/docs", timeout=15)
        if not isinstance(text, str):
            text = str(text)
        return {"contents": [{"uri": uri, "mimeType": "text/markdown",
                              "text": text}]}
    raise ApiError(404, {"error": f"Unknown resource: {uri}"})


# ── JSON-RPC over stdio ───────────────────────────────────────────────────────

_INSTRUCTIONS = (
    "Tools for the user's local Meeting Assistant: their recorded meetings "
    "with diarized transcripts, AI summaries, notes, chapters, screen-"
    "recording frames, plus app settings, logs, and live recording state. "
    "Call get_started first if you have not used these tools before. "
    "meeting_id values come from list_meetings/search_meetings. Timestamps "
    "accept seconds or 'M:SS' and follow the transcript timeline. All data "
    "is local and private to the user; nothing here can delete their data. "
    "To name speakers: list_meetings_needing_speakers, then "
    "review_meeting_speakers (read its disclosures), then label_speaker with "
    "evidence text; never label on a weak voice match alone."
)


def _write(msg: dict) -> None:
    sys.stdout.buffer.write(json.dumps(msg, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _reply(req_id, result=None, error=None) -> None:
    msg: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    _write(msg)


def _log(text: str) -> None:
    print(f"[meeting-assistant-mcp] {text}", file=sys.stderr, flush=True)


def handle(msg: dict) -> None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        client_proto = str(params.get("protocolVersion") or "")
        proto = client_proto if client_proto in PROTOCOL_VERSIONS \
            else PROTOCOL_VERSIONS[0]
        _reply(req_id, {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"listChanged": False,
                                           "subscribe": False}},
            "serverInfo": {"name": SERVER_NAME, "title": "Meeting Assistant",
                           "version": SERVER_VERSION},
            "instructions": _INSTRUCTIONS,
        })
        return

    if is_notification:
        return  # initialized / cancelled / progress etc. need no response

    if method == "ping":
        _reply(req_id, {})
        return

    if method == "tools/list":
        _reply(req_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            content, is_error = call_tool(name, args)
        except AppNotRunning:
            content, is_error = _text(_not_running_text()), True
        except ApiError as e:
            payload = e.payload if isinstance(e.payload, dict) else \
                {"error": str(e.payload)}
            payload.setdefault("http_status", e.status)
            content, is_error = _json_text(payload), True
        except Exception as e:  # never crash the server on one bad call
            content, is_error = _text(f"Tool '{name}' failed: {e}"), True
        _reply(req_id, {"content": content, "isError": is_error})
        return

    if method == "resources/list":
        _reply(req_id, {"resources": RESOURCES})
        return

    if method == "resources/read":
        try:
            _reply(req_id, read_resource(params.get("uri", "")))
        except AppNotRunning:
            _reply(req_id, error={"code": -32002,
                                  "message": _not_running_text()})
        except ApiError as e:
            _reply(req_id, error={"code": -32002,
                                  "message": json.dumps(e.payload)})
        return

    if method == "prompts/list":
        _reply(req_id, {"prompts": []})
        return

    _reply(req_id, error={"code": -32601, "message": f"Method not found: {method}"})


# ── Client-death watchdog ─────────────────────────────────────────────────────
#
# serve() exits on a clean stdin EOF, which is how a well-behaved MCP client
# shuts a stdio server down. That is not enough on Windows. When the client is
# force-killed (Task Manager, taskkill /F, a crashed editor host), a sibling
# process that inherited the write end of our stdin pipe holds it open, so the
# pipe never breaks, readline() blocks forever, and we are left running with a
# dead parent until the machine reboots. Waiting on the client's process handle
# closes that hole: the instant it goes, so do we.
#
# Under a uv-created venv on Windows, .venv\Scripts\python.exe is a trampoline
# that runs the real interpreter as its child, so the MCP client is our
# grandparent, not our parent. We detect that exactly (sys.executable is the
# trampoline, sys._base_executable the image actually running) and step up one
# level, rather than guessing our way up the tree past anything Python-shaped:
# an MCP client that is itself a Python process must be watched, not walked
# through.


def _process_table() -> tuple[dict[int, int], dict[int, str]]:
    """Snapshot of every process: {pid: parent_pid} and {pid: exe name}.

    Windows only; returns empty dicts if the snapshot is unavailable.
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x0002
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    MAX_PATH = 260

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * MAX_PATH)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32First.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32Next.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(PROCESSENTRY32)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return {}, {}
    parent_of: dict[int, int] = {}
    name_of: dict[int, str] = {}
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            parent_of[pid] = int(entry.th32ParentProcessID)
            name_of[pid] = entry.szExeFile.decode("mbcs", "replace").lower()
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return parent_of, name_of


def _launched_by_trampoline() -> bool:
    """True when sys.executable is a launcher that runs the real interpreter as
    a child process, which is how uv builds a venv on Windows. Our parent is
    then that launcher and the MCP client sits one level above it."""
    base = getattr(sys, "_base_executable", None)
    if not base:
        return False
    return os.path.normcase(base) != os.path.normcase(sys.executable)


def _client_pids() -> list[int]:
    """The PIDs whose death means our MCP client is gone. Windows only."""
    parent_of, name_of = _process_table()
    if not parent_of:
        return []
    # PID 0 and 4 are the idle and System processes: the top of the tree.
    ppid = parent_of.get(os.getpid(), 0)
    if ppid in (0, 4):
        return []
    pids = [ppid]
    launcher = os.path.basename(sys.executable).lower()
    if _launched_by_trampoline() and name_of.get(ppid, "") == launcher:
        grandparent = parent_of.get(ppid, 0)
        if grandparent not in (0, 4, ppid):
            pids.append(grandparent)
    return pids


def _wait_for_client_death_windows() -> None:
    """Block until any watched ancestor exits, then take the process down."""
    import ctypes
    from ctypes import wintypes

    SYNCHRONIZE = 0x00100000
    INFINITE = 0xFFFFFFFF

    pids = _client_pids()
    if not pids:
        return

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD,
                                           ctypes.POINTER(wintypes.HANDLE),
                                           wintypes.BOOL, wintypes.DWORD]
    handles = [h for h in (k32.OpenProcess(SYNCHRONIZE, False, pid)
                           for pid in pids) if h]
    if not handles:
        return
    block = (wintypes.HANDLE * len(handles))(*handles)
    k32.WaitForMultipleObjects(len(handles), block, False, INFINITE)
    _log("MCP client exited; shutting down")
    os._exit(0)


def _wait_for_client_death_posix() -> None:
    """Exit once we are reparented away from the client that spawned us.

    uv uses a symlinked interpreter rather than a trampoline on POSIX, so our
    immediate parent is the client. When it dies we are reparented to init or
    launchd and getppid() changes.
    """
    original = os.getppid()
    while True:
        time.sleep(5)
        if os.getppid() != original:
            _log("MCP client exited; shutting down")
            os._exit(0)


def _start_client_watchdog() -> None:
    """Start the watchdog in the background. Never fatal: a server that cannot
    watch its client is still a working server."""
    target = (_wait_for_client_death_windows if os.name == "nt"
              else _wait_for_client_death_posix)

    def run() -> None:
        try:
            target()
        except Exception as e:
            _log(f"client watchdog unavailable: {e}")

    threading.Thread(target=run, name="client-watchdog", daemon=True).start()


# ── Serve ─────────────────────────────────────────────────────────────────────

def serve() -> None:
    _log(f"serving stdio; app expected at {BASE}")
    _start_client_watchdog()
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            break  # client closed stdin
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _write({"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}})
            continue
        try:
            handle(msg)
        except Exception as e:
            _log(f"handler error: {e}")
            if "id" in msg:
                _reply(msg.get("id"),
                       error={"code": -32603, "message": f"Internal error: {e}"})


# ── Self-test ─────────────────────────────────────────────────────────────────

def selftest() -> int:
    print(f"Meeting Assistant MCP server v{SERVER_VERSION}")
    print(f"  app url:    {BASE}")
    print(f"  data dir:   {_data_dir()}")
    print(f"  auth token: {'set' if _api_token() else 'not set'}")
    print(f"  tools:      {len(TOOLS)}")
    try:
        health = _http("GET", "/system/health", timeout=5)
        print(f"  app health: OK (api v{health.get('api_version')}, "
              f"recording={health.get('recording')}, "
              f"enabled={health.get('agent_api_enabled')})")
    except AppNotRunning:
        print("  app health: NOT RUNNING")
        print(f"    {_not_running_text()}")
        return 1
    except ApiError as e:
        print(f"  app health: HTTP {e.status}: {e.payload}")
        return 1
    try:
        meetings = _http("GET", "/meetings", {"limit": 1}, timeout=10)
        print(f"  library:    {meetings.get('total')} meetings reachable")
    except (ApiError, AppNotRunning) as e:
        print(f"  library:    FAILED ({e})")
        return 1
    print("All good. Point your MCP client at this script (see --help).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Meeting Assistant MCP server (stdio). Configure your "
                    "MCP client to spawn this script; see docs/AGENT_API.md.")
    parser.add_argument("--selftest", action="store_true",
                        help="check connectivity to the app and exit")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(selftest())
    serve()


if __name__ == "__main__":
    main()
