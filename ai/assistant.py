"""AI assistant for Q&A and meeting summarization.

Supports Anthropic (Claude) and OpenAI (GPT) as interchangeable providers.
Provider and model are runtime-configurable via reload_client().
"""
import base64
import json
import re
import threading
import traceback
from typing import Callable

from core import log as log
# Importing config injects truststore (OS trust store) into Python's TLS stack,
# so provider clients created below trust corporate WARP's inspection CA. Kept
# explicit here so TLS works regardless of module import order.
from core import config as _config  # noqa: F401

Callback = Callable[[str], None]
ToolEventCallback = Callable[[str, dict], None]  # (event_type, payload) → None
FrameExtractor = Callable[[float], bytes | None]  # timestamp → JPEG bytes or None
# Generic tool executor: (tool_name, tool_input) → (content, is_error, summary, extra)
# content: str or list of blocks; is_error: bool; summary: str for UI; extra: optional dict (e.g. image)
ToolExecutor = Callable[[str, dict], tuple]

# Tool definition used for Anthropic structured patch output.
# Array-of-sections format so the model can create, rename, or restructure
# sections freely without being confined to a hardcoded set.
_PATCH_TOOL = {
    "name": "update_summary",
    "description": (
        "Update the meeting summary. Only return sections with genuinely new "
        "high-level content - do not update for minor details or topics already "
        "captured. Return an empty sections array if nothing significant changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": "Sections to create or update. Omit sections that need no changes.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Section heading (no ## prefix). May be an existing or new section name.",
                        },
                        "action": {
                            "type": "string",
                            "enum": ["append", "replace"],
                            "description": (
                                "'append': add new content to an existing section. "
                                "'replace': rewrite the section entirely, or create a new one."
                            ),
                        },
                        "content": {
                            "type": "string",
                            "description": (
                                "Markdown content for this section (no ## heading). "
                                "For 'append': only new content not already present. "
                                "For 'replace': the complete consolidated content. "
                                "Nesting and sub-bullets are encouraged for clarity. "
                                "Timestamps: append [M:SS] after a bullet when it anchors a specific "
                                "decision, commitment, or notable moment - e.g. '- Agreed to delay launch [12:04]'. "
                                "Use [M:SS–M:SS] ranges to mark the span of a key topic or discussion block. "
                                "Do NOT timestamp every bullet - only moments worth jumping to."
                            ),
                        },
                    },
                    "required": ["name", "action", "content"],
                },
            }
        },
        "required": ["sections"],
        "additionalProperties": False,
    },
}

# Tool definition for Anthropic structured chapter output. A flat list of
# time-anchored topic markers. Timestamps are returned as the transcript's own
# [M:SS]/[H:MM:SS] strings so the model never has to do second arithmetic; the
# caller parses + snaps them to real segment boundaries.
_CHAPTERS_TOOL = {
    "name": "set_chapters",
    "description": (
        "Return the ordered list of chapters (high-level topic markers) for the "
        "meeting. Each chapter marks where a distinct subject or agenda item "
        "begins. Return an empty list only if the transcript has no discernible "
        "structure yet."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chapters": {
                "type": "array",
                "description": "Chapters in chronological order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "timestamp": {
                            "type": "string",
                            "description": (
                                "The transcript timestamp where this topic begins, "
                                "copied verbatim from a transcript line, e.g. '4:12' "
                                "or '1:03:47'. Must be one of the timestamps that "
                                "actually appears in the transcript."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "A concise, descriptive chapter title in Title Case "
                                "(roughly 2-6 words) naming the subject discussed, "
                                "e.g. 'Q3 Roadmap Review' or 'Budget Concerns'."
                            ),
                        },
                    },
                    "required": ["timestamp", "title"],
                },
            }
        },
        "required": ["chapters"],
        "additionalProperties": False,
    },
}

# OpenAI Responses json_schema mirror of _CHAPTERS_TOOL.input_schema.
_CHAPTERS_OAI_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "timestamp": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["timestamp", "title"],
            },
        },
    },
    "required": ["chapters"],
}

_SCREENSHOT_TOOL = {
    "name": "get_screenshot",
    "description": (
        "Capture a screenshot from the meeting's screen recording at a specific "
        "timestamp. Use this to see what was on screen at a given moment - "
        "useful for reading slides, shared documents, UI content, code, diagrams, "
        "or anything visual that the transcript alone cannot convey. "
        "You may call this multiple times with different timestamps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "timestamp": {
                "type": "number",
                "description": (
                    "Time in seconds from the start of the recording. "
                    "Use timestamps from the transcript to target specific moments."
                ),
            },
        },
        "required": ["timestamp"],
    },
}

# OpenAI-compatible function definition for the same tool
_SCREENSHOT_FUNC_OAI = {
    "type": "function",
    "function": {
        "name": "get_screenshot",
        "description": _SCREENSHOT_TOOL["description"],
        "parameters": _SCREENSHOT_TOOL["input_schema"],
    },
}

# ── Native web search (server-side, executed by the provider) ────────────────
_WEB_SEARCH_ANTHROPIC = {"type": "web_search_20250305", "name": "web_search"}
_WEB_SEARCH_OAI       = {"type": "web_search_preview"}

# ── Global Chat tools ────────────────────────────────────────────────────────

# Shared `folder` / `include_subfolders` schema for the tools that support
# folder scoping. Spliced into each input_schema so the wording stays in sync.
_FOLDER_PARAM = {
    "type": "string",
    "description": (
        "Optional. Restrict results to one folder. Accepts a folder ID, a "
        "case-insensitive full or partial folder name, or a path like "
        "'Engineering / Backend'. Call `list_folders` first to resolve an "
        "approximate name the user mentioned — if the value is ambiguous this "
        "tool returns the candidate folders instead of guessing. Sessions that "
        "are not in any folder are excluded while this filter is active. Omit "
        "to search the entire library."
    ),
}

_INCLUDE_SUBFOLDERS_PARAM = {
    "type": "boolean",
    "description": (
        "Whether to also include sessions in sub-folders of the matched folder "
        "(default true). Set false to match only sessions filed directly in it. "
        "Ignored when `folder` is omitted."
    ),
    "default": True,
}

# Shared date-window and participant filters. Every search/browse tool accepts
# these, so they can be combined freely with `folder` in a single call.
_SPEAKER_PARAM = {
    "type": "string",
    "description": (
        "Optional. Restrict results to meetings this person took part in "
        "(case-insensitive, partial name match). Use `list_speakers` to see "
        "known names. This filters by participation, not by who said the "
        "matching line - `search_transcripts` labels each snippet with its "
        "speaker so you can tell exactly who said what."
    ),
}

_WITHIN_DAYS_PARAM = {
    "type": "integer",
    "description": (
        "Optional. Restrict to meetings from the last N days. Use for "
        "relative ranges (e.g. 7 for last week). Omit or 0 to use "
        "start_date/end_date instead."
    ),
}

_START_DATE_PARAM = {
    "type": "string",
    "description": (
        "Optional. ISO date (YYYY-MM-DD) for the earliest meeting to include. "
        "Combine with end_date for an explicit range."
    ),
}

_END_DATE_PARAM = {
    "type": "string",
    "description": (
        "Optional. ISO date (YYYY-MM-DD) for the latest meeting to include. "
        "A bare date includes that whole day."
    ),
}


def _filter_params(**extra) -> dict:
    """Build an input_schema properties block with the shared filters appended."""
    return {
        **extra,
        "folder": _FOLDER_PARAM,
        "include_subfolders": _INCLUDE_SUBFOLDERS_PARAM,
        "speaker": _SPEAKER_PARAM,
        "within_days": _WITHIN_DAYS_PARAM,
        "start_date": _START_DATE_PARAM,
        "end_date": _END_DATE_PARAM,
    }


_GLOBAL_TOOLS = [
    {
        "name": "list_folders",
        "description": (
            "List every folder in the meeting library, including nested "
            "sub-folders. Returns each folder's ID, name, parent folder ID "
            "(null for top-level), full path (e.g. 'Engineering / Backend / "
            "Sprint Planning'), and session counts — `session_count` for "
            "sessions filed directly in it and `total_session_count` including "
            "all descendants. "
            "Call this FIRST whenever the user names a folder, project, team, "
            "or client so you can resolve their approximate wording to a real "
            "folder before passing it to the `folder` parameter of "
            "`search_transcripts`, `semantic_search`, or `list_recent_meetings`. "
            "Also useful on its own to show how the library is organized."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "search_transcripts",
        "description": (
            "Search across all meeting transcripts using keyword/full-text search. "
            "Use this for specific words, phrases, or names. "
            "Each result carries the session's title, ID, folder, date, speakers "
            "and summary, plus up to 3 matching snippets — and every transcript "
            "snippet is labelled with WHO said it and how far into the meeting, "
            "so you can attribute quotes without loading the full transcript. "
            "All filters below combine: you can search one folder, one date "
            "window, and one participant in a single call."
        ),
        "input_schema": {
            "type": "object",
            "properties": _filter_params(
                query={"type": "string", "description": "Search query terms"},
                limit={"type": "integer", "description": "Max sessions to return (default 10, max 50)", "default": 10},
                match={
                    "type": "string",
                    "enum": ["all", "any", "phrase"],
                    "description": (
                        "How the query terms combine. 'all' (default): every term "
                        "must appear, prefix-matched — best for most searches. "
                        "'any': broaden to sessions matching any term. 'phrase': "
                        "the terms must appear together in that exact order — use "
                        "for quoted phrases or names like 'series B funding'."
                    ),
                    "default": "all",
                },
            ),
            "required": ["query"],
        },
    },
    {
        "name": "semantic_search",
        "description": (
            "Search meetings by meaning and topic similarity. Better for conceptual "
            "queries like 'discussions about project deadlines' or 'feedback on the design' "
            "rather than exact words. Returns ranked whole-session matches with "
            "folder, date, speakers and summary. Prefer `search_transcripts` when "
            "you need the exact line someone said; use this to find the right "
            "meetings when you don't know the vocabulary they used. "
            "Supports the same folder/date/speaker filters."
        ),
        "input_schema": {
            "type": "object",
            "properties": _filter_params(
                query={"type": "string", "description": "Conceptual search query"},
                limit={"type": "integer", "description": "Max results (default 5)", "default": 5},
                min_score={
                    "type": "number",
                    "description": (
                        "Minimum similarity score, 0-1 (default 0.25). Raise "
                        "toward 0.4+ for only strong topical matches; lower it "
                        "to cast a wider net when a search comes back empty."
                    ),
                    "default": 0.25,
                },
            ),
            "required": ["query"],
        },
    },
    {
        "name": "get_session_detail",
        "description": (
            "Load the full transcript and summary of a specific meeting session. "
            "Use this after searching to get detailed context from a particular meeting. "
            "The transcript may be truncated for very long sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session ID to load"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "list_speakers",
        "description": (
            "List all known speakers from the Voice Library with their names, "
            "colors, and the number of sessions they appear in. Use this when "
            "the user asks about participants or specific people."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_speaker_history",
        "description": (
            "Get all meetings a specific speaker appeared in, with session titles, "
            "dates, summaries, folder info, and how many segments they spoke. "
            "Use this when the user asks about a specific person's involvement, "
            "what someone has discussed across meetings, or to find meetings "
            "featuring a particular participant."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "speaker_name": {
                    "type": "string",
                    "description": "The speaker's name to look up (case-insensitive, partial match supported)",
                },
            },
            "required": ["speaker_name"],
        },
    },
    {
        "name": "list_recent_meetings",
        "description": (
            "List meetings from a time range, sorted by date (newest first). "
            "Use this to BROWSE the meeting library by date — e.g. 'meetings "
            "from last week', 'today's meetings', 'meetings between Apr 1 "
            "and Apr 14'. This is the right tool when the user wants an "
            "overview rather than a keyword search. Returns titles, IDs, "
            "dates, durations, speakers, folders, and truncated summaries. "
            "Pass `folder` to browse a single folder — e.g. 'what happened in "
            "the Backend project last week'; use `list_folders` first if you "
            "only have the user's approximate name for it. "
            "Follow up with `get_session_detail` to load the full transcript "
            "of any specific meeting from the list."
        ),
        "input_schema": {
            "type": "object",
            "properties": _filter_params(
                limit={
                    "type": "integer",
                    "description": "Max number of meetings to return (default 30, max 200).",
                    "default": 30,
                },
            ),
        },
    },
]

_GLOBAL_TOOLS_OAI = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in _GLOBAL_TOOLS
]

# ── Bulk speaker relabel (plan, then confirm, then apply) ─────────────────────
# The only tools in this file that write. They are deliberately split in two:
# planning is read-only and mints an opaque token, and applying accepts nothing
# but that token, so what gets written is always exactly what the user was
# shown. The backend refuses an apply that happens in the same chat turn as its
# plan, so the confirmation cannot be skipped by a confident model.

_RELABEL_TOOLS = [
    {
        "name": "plan_speaker_relabel",
        "description": (
            "READ-ONLY. Work out exactly what would change if every speaker "
            "named `from_name` were renamed to `to_name`, and return a plan: "
            "the meetings and speaker labels affected, segment and talk-time "
            "totals, the strategy that would be used, any warnings, and a "
            "single-use confirmation token.\n\n"
            "ALWAYS call this before `apply_speaker_relabel`. Nothing is "
            "written. Show the user the plan in plain prose - how many "
            "speakers in how many meetings, which meetings, and every warning "
            "- and ask them to confirm. If the plan matches nothing, say so "
            "and offer `list_speakers`; never guess at a different spelling "
            "on your own."
        ),
        "input_schema": {
            "type": "object",
            "properties": _filter_params(
                from_name={
                    "type": "string",
                    "description": (
                        "The speaker name to replace, exactly as it appears "
                        "today (e.g. 'Justin' or 'Speaker 3')."
                    ),
                },
                to_name={
                    "type": "string",
                    "description": "The name those speakers should carry instead.",
                },
                scope={
                    "type": "string",
                    "enum": ["session", "library"],
                    "description": (
                        "'session' changes one meeting only; 'library' changes "
                        "every matching speaker across the meeting library "
                        "(narrowed by the folder and date filters below when "
                        "you pass them)."
                    ),
                },
                match={
                    "type": "string",
                    "enum": ["exact", "contains"],
                    "description": (
                        "'exact' (default) matches the whole name, "
                        "case-insensitively. Use 'contains' ONLY when the user "
                        "explicitly asks for partial matching, e.g. 'anyone "
                        "whose label starts with Justin'."
                    ),
                    "default": "exact",
                },
                session_id={
                    "type": "string",
                    "description": (
                        "The meeting to change when scope is 'session'. In "
                        "per-meeting chat this defaults to the meeting you are "
                        "scoped to, so you can omit it."
                    ),
                },
            ),
            "required": ["from_name", "to_name"],
        },
    },
    {
        "name": "apply_speaker_relabel",
        "description": (
            "WRITES. Carry out a plan returned by `plan_speaker_relabel`, "
            "using its `token`. Renames speaker labels and can merge voice "
            "profiles, and is not undoable from the app.\n\n"
            "Only call this AFTER the user has replied with an explicit "
            "confirmation ('yes', 'do it', 'go ahead') in a LATER turn than "
            "the one the plan was made in - the backend rejects a same-turn "
            "apply. If the user changed anything about the request, call "
            "`plan_speaker_relabel` again instead and re-confirm the new plan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": (
                        "The confirmation token from the plan the user "
                        "approved. Single use, valid for 10 minutes."
                    ),
                },
                "user_confirmed": {
                    "type": "boolean",
                    "description": (
                        "True only when the user explicitly approved this exact "
                        "plan in a previous message. Never set it on their "
                        "behalf."
                    ),
                },
            },
            "required": ["token", "user_confirmed"],
        },
    },
    {
        "name": "cancel_speaker_relabel",
        "description": (
            "Retire a plan token from `plan_speaker_relabel` without applying "
            "it. Call this when the user declines the plan, changes their "
            "mind, or asks for something different, so the plan can never be "
            "applied later. Read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "token": {
                    "type": "string",
                    "description": "The token of the plan the user declined.",
                },
            },
            "required": ["token"],
        },
    },
]

_RELABEL_TOOLS_OAI = [
    {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
    for t in _RELABEL_TOOLS
]

# The global chat's full tool set: read-only research plus the relabel pair.
_GLOBAL_TOOLS_ALL = _GLOBAL_TOOLS + _RELABEL_TOOLS
_GLOBAL_TOOLS_ALL_OAI = _GLOBAL_TOOLS_OAI + _RELABEL_TOOLS_OAI

_RELABEL_CONTRACT_COMMON = (
    "## Bulk speaker reassignment (plan, confirm, apply)\n"
    "You can rename speakers in bulk, but only through a two-step contract:\n"
    "1. Call `plan_speaker_relabel` FIRST, every time. It writes nothing and "
    "returns a plan plus a single-use token.\n"
    "2. Report the plan to the user in plain prose: how many speaker labels, "
    "in how many meetings, which meetings (name a few), the segment totals, "
    "and EVERY warning the plan lists. Then ask them to confirm.\n"
    "3. Only after the user replies with an explicit yes IN A LATER MESSAGE, "
    "call `apply_speaker_relabel` with that token and user_confirmed=true. If the user declines or changes the request, call `cancel_speaker_relabel` with the token before doing anything else. "
    "The backend rejects an apply made in the same turn as its plan, so never "
    "chain the two calls together.\n"
    "- If the user changes the names, the scope, or the filters, throw the old "
    "plan away and plan again. Never reuse a token for a different request.\n"
    "- If the user says no, or says nothing about it, do not apply. Tokens "
    "expire after 10 minutes; if one has expired, just plan again.\n"
    "- Never guess a name's spelling. If `plan_speaker_relabel` matches "
    "nothing, say so and use `list_speakers` to show the real names.\n"
    "- Use match='contains' only when the user explicitly asks for partial "
    "matching. Exact matching is the default and is what they almost always "
    "mean.\n"
)

_RELABEL_CONTRACT_SESSION = (
    "\n\n" + _RELABEL_CONTRACT_COMMON
    + "- In this per-meeting chat, scope defaults to 'session': THIS meeting "
    "only. Use scope='library' only when the user says every meeting, the "
    "whole library, everywhere, or names a wider set of meetings.\n"
)

_RELABEL_CONTRACT_GLOBAL = (
    "\n\n" + _RELABEL_CONTRACT_COMMON
    + "- In this library-wide chat, scope defaults to 'library'. Pass "
    "scope='session' with a session_id when the user is clearly talking about "
    "one meeting.\n"
    "- The folder and date filters narrow a library-scope plan the same way "
    "they narrow a search, so 'fix every Justin in the Backend folder' is one "
    "plan call with folder='Backend'.\n"
)

# Models that support Anthropic extended thinking
_ANTHROPIC_THINKING_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250219",
    "claude-3-7-sonnet-20250219",
}


def _format_meta_block(meta: dict | None) -> str:
    """Build a human-readable metadata block from session metadata."""
    if not meta:
        return ""

    lines = ["Session metadata:"]

    if meta.get("title"):
        lines.append(f"  Title: {meta['title']}")

    if meta.get("is_live"):
        lines.append("  Status: LIVE - recording is in progress, transcript is growing in real time")
    else:
        lines.append("  Status: Completed recording")

    if meta.get("started_at"):
        lines.append(f"  Started: {meta['started_at']}")
    if meta.get("ended_at"):
        lines.append(f"  Ended: {meta['ended_at']}")

    if meta.get("duration"):
        lines.append(f"  Duration: {meta['duration']}")

    if meta.get("segment_count"):
        lines.append(f"  Transcript segments: {meta['segment_count']}")

    if meta.get("speakers"):
        lines.append(f"  Speakers ({len(meta['speakers'])}): {', '.join(meta['speakers'])}")

    source_parts = []
    if meta.get("has_desktop_audio"):
        source_parts.append("desktop/system audio")
    if meta.get("has_mic_audio"):
        source_parts.append("microphone")
    if source_parts:
        lines.append(f"  Audio sources: {', '.join(source_parts)}")

    if meta.get("custom_prompt"):
        lines.append(f"\n  User-provided context: {meta['custom_prompt']}")

    # Chapters: a high-level topic outline of the meeting, if any exist. Gives
    # the summary/chat models a scaffold of what was discussed when.
    chapters = meta.get("chapters")
    if chapters:
        lines.append("\n  Chapters (topic outline):")
        for ch in chapters:
            ts = ch.get("timestamp") or ""
            title = (ch.get("title") or "").strip()
            if not title:
                continue
            lines.append(f"    - [{ts}] {title}" if ts else f"    - {title}")

    return "\n".join(lines)


class AIAssistant:

    _SYSTEM_QA = (
        "You are an intelligent meeting assistant. You are scoped to a SINGLE meeting "
        "session - the transcript, metadata, and summary provided below are your only "
        "source of truth. Do not reference or speculate about other meetings.\n\n"
        "## What you know\n"
        "- The full transcript of THIS session with speaker labels and timestamps\n"
        "- Who the speakers are and what they discussed in THIS meeting\n"
        "- The timeline and flow of the conversation\n"
        "- The current auto-generated summary (if one exists)\n"
        "- Whether the recording is live or completed\n\n"
        "## Transcript format\n"
        "Each line follows: [M:SS] [Speaker Name] spoken text\n"
        "- Timestamps mark when each segment was spoken\n"
        "- Speaker labels may be auto-generated (\"Speaker 1\") or user-assigned names\n"
        "- The transcript is machine-generated from audio, so expect minor transcription "
        "errors, missing punctuation, or misheard words - interpret charitably\n\n"
        "## How to respond\n"
        "- Answer questions directly and concisely using markdown formatting\n"
        "- When quoting or referencing specific moments, include the timestamp as [M:SS] "
        "so the user can jump to that point in the recording\n"
        "- If the user asks about something not discussed in this meeting, say so clearly\n"
        "- You can cross-reference the summary and transcript - e.g. if asked to elaborate "
        "on a summary bullet point, find the relevant transcript section\n"
        "- If the recording is live, keep in mind more content may arrive after your answer\n"
        "- When speakers are identified by name, use their names naturally in your response\n"
        "- For questions about who said what, be precise about speaker attribution\n\n"
        "## Timestamps\n"
        "Timestamps are rendered as interactive pills that let users jump to that "
        "moment in the recording. They MUST be in square brackets to render correctly.\n"
        "- Format: [M:SS] for a single moment, [M:SS-M:SS] for a range\n"
        "- ALWAYS wrap timestamps in square brackets - bare timestamps like 2:50 "
        "will NOT render as clickable pills. Write [2:50] instead.\n"
        "- Place the timestamp after the referenced text, not before\n"
        "- For multiple timespans: [18:31-19:48] [27:17-27:26]\n"
        "- Only timestamp moments worth jumping to - avoid tagging every sentence\n"
        "- Use exact timestamps from the transcript only. No tildes or "
        "approximations like [~17:30].\n\n"
        "- Always respond in English regardless of any foreign words or phrases in the transcript\n\n"
        "## Web search\n"
        "You have access to a web search tool. Use it **sparingly** and only when "
        "a search would genuinely add value — for example, clarifying industry-specific "
        "terminology, looking up a product or company mentioned in the meeting, or "
        "providing context on an external event referenced by a speaker. Your primary "
        "focus should always remain on the meeting transcript itself."
    ) + _RELABEL_CONTRACT_SESSION

    _SYSTEM_SUMMARY = (
        "You are a meeting summarization assistant. You produce clear, well-structured "
        "summaries from audio transcripts.\n\n"
        "## Important context\n"
        "- The transcript may be partial, incomplete, or still in progress - the recording "
        "could be live and ongoing, or the audio may have been cut off mid-sentence\n"
        "- Work with whatever content is available; never refuse because the transcript "
        "seems short or incomplete\n\n"
        "## Transcript format\n"
        "Each line follows: [M:SS] [Speaker Name] spoken text\n"
        "- Timestamps mark when each segment was spoken\n"
        "- Speaker labels may be auto-generated (\"Speaker 1\") or user-assigned names\n"
        "- The transcript is machine-generated, so expect minor errors - interpret charitably\n\n"
        "## Output format\n"
        "- Choose section headings that fit the content and context - do not use a fixed "
        "structure. Let the transcript and any user instructions guide what sections to create.\n"
        "- Use markdown (## headings, bullets, **bold**, nesting) for a scannable hierarchy\n"
        "- Attribute key points and decisions to speakers by name when identified\n\n"
        "## Timestamps\n"
        "Timestamps let users jump directly to moments in the recording - use them surgically.\n"
        "- Format: `[M:SS]` for a moment, `[M:SS–M:SS]` for a span (e.g. a topic block)\n"
        "- Place AFTER the relevant bullet or phrase, not at the start: "
        "`- Team agreed to cut scope for v1 [8:14]`\n"
        "- Good candidates: decisions and commitments, action items assigned to someone, "
        "notable quotes or turning points, topic transitions, key disagreements resolved\n"
        "- Skip timestamps on: generic observations, filler content, bullets that are already "
        "obvious from context, or anywhere one per section is already enough\n"
        "- For multiple seperate timespan moments, group each range in it's own set of square brackets (e.g. [18:31–19:48] [27:17–27:26])\n"
        "- Aim for 1–3 timestamps per section - enough to orient, not so many they lose meaning\n\n"
        "## Quality bar\n"
        "- Keep every section as concise as possible - rich but tight\n"
        "- Do not pad with obvious or low-value bullets; every line should earn its place\n"
        "- Prefer nested structure over long flat lists when topics have sub-points\n"
        "- Always write in English regardless of any foreign words or phrases in the transcript"
    )

    _SYSTEM_CHAPTERS = (
        "You divide a meeting transcript into chapters: a short, ordered list of "
        "the high-level talking points or subjects discussed, each anchored to the "
        "moment it begins.\n\n"
        "## What a chapter is\n"
        "- A chapter marks where a distinct topic, agenda item, or phase of the "
        "conversation STARTS. Think of the table of contents a listener would want "
        "to jump around with.\n"
        "- Chapters are about SUBJECTS, not speakers or individual sentences. Do "
        "not create a chapter every time the speaker changes.\n\n"
        "## Transcript format\n"
        "Each line follows: [M:SS] [Speaker Name] spoken text\n"
        "- Timestamps mark when each segment was spoken (use these exact values)\n"
        "- The transcript is machine-generated, so expect minor errors - interpret charitably\n\n"
        "## How to choose chapters\n"
        "- Only break on a genuine shift in subject matter - a new agenda item, a "
        "clear pivot in discussion, a decision block, a Q&A turn, etc.\n"
        "- Do NOT over-segment. Resist adding a chapter just because time has "
        "passed; brief tangents and back-and-forth within one topic stay in one "
        "chapter.\n"
        "- The first chapter should start at or near the beginning (e.g. an "
        "intro/opening), unless the meeting opens mid-topic.\n"
        "- Space chapters out sensibly for the meeting's length. A short meeting "
        "may have just 2-4 chapters; a long one more - but quality over quantity.\n\n"
        "## Titles\n"
        "- Concise, descriptive, Title Case, roughly 2-6 words.\n"
        "- Name the subject, don't summarize the discussion: 'Q3 Roadmap Review', "
        "'Budget Concerns', 'Hiring Plan', 'Next Steps' - not full sentences.\n\n"
        "## Output\n"
        "- Return each chapter's timestamp copied verbatim from a real transcript "
        "line, plus its title. Chapters must be in chronological order.\n"
        "- Always write titles in English regardless of the transcript language."
    )

    _CHAPTERS_GRANULARITY_HINT = {
        "coarse": (
            "Granularity: COARSE. Favour a small number of broad chapters - only "
            "the major phases or agenda items. Merge related sub-topics together."
        ),
        "balanced": (
            "Granularity: BALANCED. Aim for a natural table of contents - one "
            "chapter per genuine topic, neither too sparse nor too granular."
        ),
        "fine": (
            "Granularity: FINE. Capture finer topic shifts and sub-topics, but "
            "still never break on mere speaker changes or single sentences."
        ),
    }

    def __init__(self, provider: str = "anthropic", model: str = "claude-sonnet-4-6") -> None:
        self.provider = provider
        self.model = model
        self.client = self._make_client(provider)
        self._clients: dict[str, object] = {provider: self.client}

    def _make_client(self, provider: str):
        """Create the API client.  Returns None gracefully if no key is set."""
        try:
            # TLS is verified against the OS trust store (truststore, injected by
            # core.config), so corporate WARP's inspection CA is honoured without
            # disabling certificate checks. The SDKs read the API key from the
            # environment (ANTHROPIC_API_KEY / OPENAI_API_KEY).
            if provider == "openai":
                from openai import OpenAI
                return OpenAI()
            import anthropic
            return anthropic.Anthropic()
        except Exception as e:
            print(f"[ai] Could not initialise {provider} client: {e}")
            return None

    def _get_client(self, provider: str):
        """Return a cached client for the given provider, creating if needed."""
        if provider not in self._clients or self._clients[provider] is None:
            self._clients[provider] = self._make_client(provider)
        return self._clients[provider]

    def reload_client(self, provider: str | None = None, model: str | None = None) -> None:
        """Re-create the client, optionally changing provider and/or model."""
        if provider is not None:
            self.provider = provider
        if model is not None:
            self.model = model
        self.client = self._make_client(self.provider)
        self._clients[self.provider] = self.client

    def _resolve(self, provider: str | None, model: str | None) -> tuple:
        """Return (client, provider, model) using overrides or defaults."""
        p = provider or self.provider
        m = model or self.model
        c = self._get_client(p) if p != self.provider else self.client
        return c, p, m

    def ask(
        self,
        transcript: str,
        chat_history: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        meta: dict | None = None,
        cancel: "threading.Event | None" = None,
        frame_extractor: FrameExtractor | None = None,
        on_tool_event: ToolEventCallback | None = None,
        tools_anthropic: list | None = None,
        tools_openai: list | None = None,
        tool_executor: "ToolExecutor | None" = None,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        system_context: str | None = None,
    ) -> None:
        """Stream an answer to the latest question in chat_history.

        If ``frame_extractor`` is provided, the model can call the
        ``get_screenshot`` tool to view what was on screen at a given
        timestamp.  The tool loop runs up to 5 iterations.
        """
        meta_block = _format_meta_block(meta)
        summary_block = ""
        if meta and meta.get("current_summary"):
            summary_block = (
                f"\n\nCurrent auto-generated summary:\n---\n"
                f"{meta['current_summary']}\n---"
            )

        # User-supplied override (per-session or global) takes precedence over
        # the built-in QA prompt. The transcript/meta/screen-recording blocks
        # are still appended below so the model always gets meeting context.
        system = (system_prompt.strip() if system_prompt and system_prompt.strip() else self._SYSTEM_QA)
        if system_context and system_context.strip():
            system += "\n\n" + system_context.strip()
        if frame_extractor:
            system += (
                "\n\n## Screen recording\n"
                "A screen recording is available for this session. You can call the "
                "`get_screenshot` tool with a timestamp (in seconds) to see what was "
                "on screen at that moment. Use this whenever the user asks about visual "
                "content (slides, code, UI, diagrams, shared screens, etc.) or when "
                "the transcript references something being shown on screen. You may "
                "call the tool multiple times with different timestamps to examine "
                "different moments.\n\n"
                "### Embedding screenshots in your response\n"
                "Each screenshot tool result includes a markdown image URL. **Always embed "
                "relevant screenshots inline in your response** using the provided markdown "
                "syntax: `![description](url)`. This lets the user see what you're "
                "describing without expanding the tool panel. Include screenshots at the "
                "point in your response where they're most relevant - e.g. right after "
                "describing what's shown on screen."
            )
        system += "\n\n"
        if meta_block:
            system += meta_block + "\n\n"
        system += (
            f"Meeting transcript:\n---\n"
            f"{transcript or '(No transcript yet - meeting may just be starting)'}"
            f"\n---"
            f"{summary_block}"
        )

        active_tools_anthropic = tools_anthropic
        active_tools_openai = tools_openai
        if tools_anthropic is not None or tools_openai is not None or tool_executor is not None:
            active_tools_anthropic = []
            active_tools_openai = []
            if frame_extractor:
                active_tools_anthropic.append(_SCREENSHOT_TOOL)
                active_tools_openai.append(_SCREENSHOT_FUNC_OAI)
            active_tools_anthropic.extend(tools_anthropic or [])
            active_tools_openai.extend(tools_openai or [])

        self._stream_with_tools(
            system, chat_history, on_token, on_done,
            cancel=cancel, frame_extractor=frame_extractor,
            on_tool_event=on_tool_event,
            tools_anthropic=active_tools_anthropic,
            tools_openai=active_tools_openai,
            tool_executor=tool_executor,
            provider=provider, model=model,
        )

    def summarize(
        self,
        transcript: str,
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        custom_prompt: str = "",
        meta: dict | None = None,
        provider: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> None:
        """Stream a structured meeting summary from a full transcript.

        ``system_prompt`` overrides the built-in ``_SYSTEM_SUMMARY`` when
        provided and non-empty. ``custom_prompt`` is still appended on top
        as additional user instructions.
        """
        if not transcript.strip():
            on_token("*No transcript available yet - start recording first.*")
            if on_done:
                on_done()
            return

        system = (
            system_prompt.strip()
            if system_prompt and system_prompt.strip()
            else self._SYSTEM_SUMMARY
        )
        meta_block = _format_meta_block(meta)
        if meta_block:
            system += f"\n\n{meta_block}"
        if custom_prompt.strip():
            system += f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"

        prompt = (
            "Summarize this transcript. Choose section headings that fit the content "
            "and any instructions above - do not use a fixed structure.\n\n"
            f"Transcript:\n---\n{transcript}\n---"
        )
        # Summarization routes OpenAI through the Responses API; Anthropic
        # keeps the existing messages.stream path via _stream().
        try:
            from core.network import warp_disconnect
            warp_disconnect()
            client, prov, mdl = self._resolve(provider, model)
            if client is None:
                on_token(
                    f"\n\n*Error: No {prov.title()} API key configured. "
                    f"Add it in Settings.*"
                )
                return
            messages = [{"role": "user", "content": prompt}]
            if prov == "openai":
                self._stream_openai_responses(system, messages, on_token,
                                              client=client, model=mdl)
            else:
                self._stream_anthropic(system, messages, on_token,
                                       client=client, model=mdl)
        except Exception as e:
            on_token(f"\n\n*Error: {e}*")
        finally:
            if on_done:
                on_done()

    def patch_summary(
        self,
        existing_summary: str,
        transcript: str,
        custom_prompt: str = "",
        meta: dict | None = None,
        update_context: str = "",
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """Incrementally update a summary using the full transcript.

        The model chooses per-section whether to append new bullets or replace
        the whole section (e.g. for consolidation/deduplication). Sections not
        returned are left untouched, so content can never be silently dropped.
        """
        if not transcript.strip() and not update_context.strip():
            return existing_summary

        meta_block = _format_meta_block(meta)
        meta_note = f"\n\n{meta_block}" if meta_block else ""
        custom_note = (
            f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"
            if custom_prompt.strip() else ""
        )
        update_note = (
            f"\n\nAdditional update context:\n{update_context.strip()}"
            if update_context.strip() else ""
        )

        system_prompt = (
            "You update structured meeting summaries incrementally. You receive the "
            "current summary and the full transcript.\n\n"
            "## When to update\n"
            "ONLY update when genuinely new high-level concepts, decisions, or topics "
            "have been discussed. Do not update for minor elaborations, repetition, or "
            "continued discussion of topics already captured. If nothing significant is "
            "new, return an empty sections array.\n\n"
            "## How to update\n"
            "- 'append': add new content to an existing section\n"
            "- 'replace': rewrite a section entirely (consolidation, deduplication, or "
            "restructuring), or create a new section\n"
            "- Section names are free-form - rename, merge, or create sections as the "
            "content warrants. Let the transcript and user instructions guide structure.\n\n"
            "## Quality bar\n"
            "- Keep all sections as concise as possible - rich but tight\n"
            "- Do not arbitrarily append bullets; update existing ones when appropriate\n"
            "- Use markdown hierarchy and nesting to keep things organised\n"
            "- Timestamps: [M:SS] format (e.g. [4:32]) inline for key moments only\n"
            "- Attribute decisions and points to speakers by name when identified\n"
            "- Always write in English regardless of any foreign words or phrases in the transcript"
        )
        if custom_prompt.strip():
            system_prompt += (
                f"\n\nAdditional user instructions:\n{custom_prompt.strip()}"
            )
        user_prompt = (
            f"Update the summary to reflect any significant new content in the transcript."
            f"{meta_note}{custom_note}{update_note}\n\n"
            f"Current summary:\n---\n{existing_summary}\n---\n\n"
            f"Full transcript:\n---\n{transcript}\n---\n\n"
            f"Return a sections array with only the sections that need changes. "
            f"Each entry: name, action ('append'/'replace'), content (markdown, no ## heading). "
            f"Omit unchanged sections. Return empty sections array if nothing is new."
        )

        try:
            raw = self._complete_structured(system_prompt, user_prompt, provider=provider, model=model)
        except Exception as e:
            log.warn("summary", f"patch failed ({e}) - keeping existing summary")
            return existing_summary

        section_updates = raw.get("sections", []) if isinstance(raw, dict) else []
        if not section_updates:
            return existing_summary

        sections = self._parse_sections(existing_summary)
        updated: list[str] = []
        for item in section_updates:
            if not isinstance(item, dict):
                continue
            name = item.get("name", "").strip()
            action = item.get("action", "append")
            content = str(item.get("content", "")).strip()
            if not name or not content:
                continue
            if action == "replace":
                sections[name] = content
            else:  # append
                existing = sections.get(name, "").strip()
                sections[name] = (existing + "\n\n" + content).strip() if existing else content
            updated.append(f"{name}({action})")

        if not updated:
            return existing_summary

        log.info("summary", f"Updated: {updated}")
        return self._build_summary(sections)

    def generate_chapters(
        self,
        transcript: str,
        meta: dict | None = None,
        system_prompt: str | None = None,
        existing: list[dict] | None = None,
        granularity: str = "balanced",
        provider: str | None = None,
        model: str | None = None,
    ) -> list[dict]:
        """Generate high-level topic chapters from a transcript.

        Returns ``[{"timestamp": "M:SS", "title": str}, ...]`` in chronological
        order. Timestamps are the transcript's own strings; the caller parses
        them and snaps to real segment boundaries. Returns [] on failure or an
        empty transcript.

        ``system_prompt`` overrides the built-in ``_SYSTEM_CHAPTERS`` when
        provided. ``existing`` (list of {timestamp, title}) is supplied on live
        auto-runs so the model keeps already-established early chapters stable.
        """
        if not transcript.strip():
            return []

        system = (
            system_prompt.strip()
            if system_prompt and system_prompt.strip()
            else self._SYSTEM_CHAPTERS
        )
        system += "\n\n" + self._CHAPTERS_GRANULARITY_HINT.get(
            granularity, self._CHAPTERS_GRANULARITY_HINT["balanced"]
        )
        meta_block = _format_meta_block(meta)
        if meta_block:
            system += f"\n\n{meta_block}"

        existing_note = ""
        if existing:
            listed = "\n".join(
                f"    - [{c.get('timestamp', '')}] {c.get('title', '')}"
                for c in existing if c.get("title")
            )
            if listed:
                existing_note = (
                    "\n\nChapters already established earlier in this live meeting - "
                    "keep these stable (same timestamps and titles) and only add or "
                    "refine chapters for the later part of the transcript:\n" + listed
                )

        prompt = (
            "Divide the following meeting transcript into chapters, following the "
            "instructions above. Use only timestamps that actually appear in the "
            f"transcript.{existing_note}\n\n"
            f"Transcript:\n---\n{transcript}\n---"
        )

        try:
            raw = self._complete_structured(
                system, prompt,
                provider=provider, model=model,
                tool=_CHAPTERS_TOOL,
                oai_schema=_CHAPTERS_OAI_SCHEMA,
                oai_schema_name="chapters",
            )
        except Exception as e:
            log.warn("chapters", f"generation failed ({e})")
            return []

        items = raw.get("chapters", []) if isinstance(raw, dict) else []
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            ts = str(it.get("timestamp", "")).strip().strip("[]")
            title = str(it.get("title", "")).strip()
            if title:
                out.append({"timestamp": ts, "title": title})
        return out

    _SYSTEM_GLOBAL_QA = (
        "You are an intelligent meeting assistant with access to a library of "
        "recorded meetings. You search across ALL sessions to answer questions - "
        "you are NOT scoped to any single meeting.\n\n"
        "## How to respond\n"
        "- Use your tools to find relevant information before answering - "
        "do not guess or make up content\n"
        "- **Cite every session you reference as a markdown link** so the user "
        "can open it in one click. Format: `[Meeting Title](/session?id=<session_id>)`. "
        "The `session_id` is included in every tool result. Example: \"In "
        "[Sprint Planning (Apr 7)](/session?id=abc-123-def), the team decided…\"\n"
        "- Reference speakers by name and note their involvement across sessions\n"
        "- Do NOT include [M:SS] timestamps - this is a cross-session view, "
        "not a single-recording player\n"
        "- If the user asks about something you can't find, say so clearly\n"
        "- Answer directly and concisely using markdown formatting\n"
        "- When multiple sessions are relevant, synthesize information across them "
        "and link to each one inline\n"
        "- For questions about who said what, be precise about speaker attribution "
        "and link to the meeting it was in\n"
        "- Always respond in English regardless of any foreign words or phrases "
        "in the transcripts\n\n"
        "## Tool usage strategy\n"
        "- Use `list_recent_meetings` when the user asks for a date-bounded "
        "browse (e.g. 'last week', 'today', 'this month', explicit dates) — "
        "this returns a chronological overview without needing keywords\n"
        "- Use `search_transcripts` for specific keywords or phrases\n"
        "- Use `semantic_search` for conceptual/thematic queries\n"
        "- Use `get_session_detail` to load full transcript + summary from "
        "a particular meeting (use after listing/searching to dig deeper)\n"
        "- Use `list_speakers` to see all known participants across all meetings\n"
        "- Use `get_speaker_history` to find all meetings a specific person "
        "appeared in, with their activity level in each\n"
        "- You may call tools multiple times to gather enough context. "
        "Combine tools freely — e.g. list recent meetings, then load "
        "details for the ones that look relevant.\n\n"
        "## Scoping to a folder\n"
        "Meetings are organized into folders, which can be nested (e.g. "
        "'Engineering / Backend / Sprint Planning').\n"
        "- When the user names a folder, project, team, or client, call "
        "`list_folders` FIRST to resolve their wording to a real folder, then "
        "pass it to the `folder` parameter of `search_transcripts`, "
        "`semantic_search`, or `list_recent_meetings`\n"
        "- Passing the folder ID is the most reliable; a name or path also works\n"
        "- If a tool reports the folder is ambiguous it returns the candidate "
        "folders with their full paths — ask the user which one they meant, or "
        "re-run with the ID if the context makes it obvious\n"
        "- `include_subfolders` defaults to true. Set it false only when the "
        "user clearly wants just that one folder and not the work nested under it\n"
        "- A folder filter excludes meetings that aren't in any folder, and an "
        "empty result means that folder genuinely has no matches — say so "
        "rather than silently widening the search\n"
        "- Omit `folder` entirely for library-wide questions\n\n"
        "## Filtering (folder / date / speaker)\n"
        "`search_transcripts`, `semantic_search` and `list_recent_meetings` all "
        "accept the same filters, and they stack in a single call — prefer one "
        "filtered call over fetching broadly and sifting manually.\n"
        "- `folder` + `include_subfolders` — scope to a project or team\n"
        "- `within_days`, or `start_date`/`end_date` — scope to a time window\n"
        "- `speaker` — only meetings that person took part in\n"
        "So \"what did Priya flag about billing last quarter\" is ONE call: "
        "`search_transcripts(query='billing', speaker='Priya', within_days=90)`\n"
        "- Search results already include each session's summary, speakers, "
        "date and folder — only call `get_session_detail` when you need the "
        "actual transcript, not just to identify a meeting\n"
        "- If a search returns nothing, loosen one filter at a time rather than "
        "dropping them all: try `match='any'`, a lower `min_score`, or a wider "
        "date range, and say what you had to broaden\n\n"
        "## Attributing quotes\n"
        "Every `search_transcripts` snippet is labelled with the speaker who "
        "said it and its offset into the meeting. Use that to attribute quotes "
        "precisely — say who said something, not just which meeting it was in. "
        "Note `speaker` filters by who ATTENDED; the snippet labels are what "
        "tell you who actually said a given line, so check them before "
        "attributing.\n\n"
        "## Context in results\n"
        "- Search results include session summaries (truncated) so you can often "
        "answer without loading the full transcript\n"
        "- Results include the folder name and its full path when sessions are "
        "organized into folders - use this to provide project/team context\n"
        "- Speaker history shows segment counts per session to indicate how "
        "active someone was in each meeting\n\n"
        "## Web search\n"
        "You also have access to a web search tool. Use it **sparingly** — only "
        "when a search would genuinely add value beyond the meeting data. Good "
        "uses: clarifying industry terms or acronyms mentioned in meetings, looking "
        "up a company or product referenced by a speaker, providing context on an "
        "external event. Your primary focus should always be the stored meetings."
    ) + _RELABEL_CONTRACT_GLOBAL

    def ask_global(
        self,
        chat_history: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None = None,
        cancel: "threading.Event | None" = None,
        on_tool_event: ToolEventCallback | None = None,
        tool_executor: "ToolExecutor | None" = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Stream an answer for the Global Chat (cross-session Q&A)."""
        self._stream_with_tools(
            self._SYSTEM_GLOBAL_QA,
            chat_history,
            on_token,
            on_done,
            cancel=cancel,
            on_tool_event=on_tool_event,
            tools_anthropic=_GLOBAL_TOOLS_ALL,
            tools_openai=_GLOBAL_TOOLS_ALL_OAI,
            tool_executor=tool_executor,
            provider=provider, model=model,
        )

    _SYSTEM_TITLE = (
        "You generate ultra-short meeting titles. "
        "Reply with ONLY 2-4 words in Title Case. "
        "No punctuation, no quotes, no explanation.\n\n"
        "Guidance:\n"
        "- The transcript is the primary signal for what the meeting was about.\n"
        "- If past meeting titles are provided, infer the user's naming style "
        "(e.g. \"Product Standup\", \"Design Review\", \"1:1 with Alice\") and "
        "match it when the signals indicate a recurring series.\n"
        "- Meetings with the same participants AND similar day/time are "
        "almost certainly the same recurring meeting — reuse or closely "
        "mirror the existing title.\n"
        "- One-off meetings with unfamiliar participants should get a fresh, "
        "content-specific title.\n"
        "- Prefer specificity over generic words like \"Meeting\" or \"Call\"."
    )

    def generate_title(
        self,
        transcript: str,
        *,
        context: dict | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Return a short title for the meeting, or '' on failure/no content.

        ``context`` (optional) may include:
          - ``started_at``: ISO timestamp for the current meeting.
          - ``participants``: ``[{'name': str, 'global_id': str|None}, ...]``
          - ``similar_past_meetings``: list of dicts with ``title``,
            ``shared_speakers``, ``same_dow``, ``hour_delta``. Already sorted
            by relevance (most-similar first).

        ``system_prompt`` (optional) overrides the built-in ``_SYSTEM_TITLE``.
        The participant / past-meeting context block is still appended to the
        user prompt regardless, so even custom prompts get the meeting context.

        When context is supplied the model is steered to match existing naming
        conventions for recurring meetings with the same participants / time.
        """
        if not transcript.strip():
            return ""
        snippet = transcript[:1000].strip()

        # ── Build the context block ─────────────────────────────────────────
        ctx_lines: list[str] = []
        if context:
            started_at = context.get("started_at")
            if started_at:
                try:
                    from datetime import datetime as _dt
                    dt = _dt.fromisoformat(started_at)
                    ctx_lines.append(
                        f"Meeting date: {dt.strftime('%A, %b %d %Y at %I:%M %p').lstrip('0')}"
                    )
                except Exception:
                    pass
            participants = context.get("participants") or []
            names = [p["name"] for p in participants if p.get("name")]
            if names:
                preview = ", ".join(names[:10])
                extra = f" (+{len(names) - 10} more)" if len(names) > 10 else ""
                ctx_lines.append(f"Participants: {preview}{extra}")

            past = context.get("similar_past_meetings") or []
            if past:
                ctx_lines.append("")
                ctx_lines.append(
                    "Past meeting titles from this user, most similar first "
                    "(similarity based on shared participants, same day-of-week, "
                    "and similar time-of-day):"
                )
                for m in past[:8]:
                    sig_parts = []
                    ss = int(m.get("shared_speakers") or 0)
                    if ss:
                        sig_parts.append(f"{ss} shared participant{'s' if ss != 1 else ''}")
                    if m.get("same_dow"):
                        sig_parts.append("same day-of-week")
                    hd = m.get("hour_delta")
                    if hd is not None:
                        if hd < 0.25:
                            sig_parts.append("same time-of-day")
                        else:
                            sig_parts.append(f"~{hd:.1f}h time offset")
                    tag = f"  [{'; '.join(sig_parts)}]" if sig_parts else ""
                    ctx_lines.append(f'- "{m["title"]}"{tag}')

        context_block = "\n".join(ctx_lines)

        # ── System + user messages ─────────────────────────────────────────
        system = (
            system_prompt.strip()
            if system_prompt and system_prompt.strip()
            else self._SYSTEM_TITLE
        )
        user_parts = []
        if context_block:
            user_parts.append(context_block)
            user_parts.append("")
        user_parts.append(f"Transcript excerpt:\n{snippet}")
        user_parts.append("")
        user_parts.append("Title:")
        user_msg = "\n".join(user_parts)

        try:
            raw = self._complete(system, user_msg)
            # Strip quotes / punctuation the model sometimes emits despite instructions
            cleaned = raw.strip().strip('"\'`').rstrip(".!?,:;")
            words = cleaned.split()[:4]
            return " ".join(words)
        except Exception:
            return ""

    # ── Anthropic prompt caching ─────────────────────────────────────────────

    @staticmethod
    def _build_cached_kwargs(
        system: str,
        messages: list[dict],
        model: str,
        max_tokens: int = 4096,
        tools: list | None = None,
        extra: dict | None = None,
    ) -> dict:
        """Build Anthropic request kwargs with prompt-caching breakpoints.

        Places two ``cache_control`` markers per request:

        1. End of the stable prefix — the last tool definition (or the system
           block when there are no tools).  Caches system + tool schemas.
        2. End of the message history — the last content block of the last
           message.  Creates a rolling cache of conversation context.

        Reads cost ~10% of input tokens; writes cost ~125%.  The 5-min TTL
        refreshes on each hit.
        """
        _CC = {"type": "ephemeral"}

        system_blocks = [{"type": "text", "text": system}]
        kwargs = {"model": model, "system": system_blocks, "max_tokens": max_tokens}
        if extra:
            kwargs.update(extra)

        # ── Breakpoint 1: stable prefix (tools or system) ──────────────
        if tools:
            cached_tools = [dict(t) for t in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": _CC}
            kwargs["tools"] = cached_tools
        else:
            system_blocks[-1] = {**system_blocks[-1], "cache_control": _CC}

        # ── Breakpoint 2: end of accumulated message history ───────────
        cached_msgs: list[dict] = []
        for msg in messages:
            new_msg = dict(msg)
            content = msg.get("content")
            if isinstance(content, list):
                new_msg["content"] = [
                    dict(b) if isinstance(b, dict) else b
                    for b in content
                ]
            cached_msgs.append(new_msg)

        for msg in reversed(cached_msgs):
            content = msg.get("content")
            if isinstance(content, list) and content:
                last = content[-1]
                if isinstance(last, dict):
                    content[-1] = {**last, "cache_control": _CC}
                break
            elif isinstance(content, str) and content:
                msg["content"] = [{"type": "text", "text": content, "cache_control": _CC}]
                break

        kwargs["messages"] = cached_msgs
        return kwargs

    # ── Internal ──────────────────────────────────────────────────────────────

    def _stream(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None,
        cancel: "threading.Event | None" = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Stream tokens from the active provider."""
        try:
            from core.network import warp_disconnect
            warp_disconnect()
            client, prov, mdl = self._resolve(provider, model)
            if client is None:
                on_token(
                    f"\n\n*Error: No {prov.title()} API key configured. "
                    f"Add it in Settings.*"
                )
                return
            if prov == "openai":
                self._stream_openai(system, messages, on_token, cancel, client=client, model=mdl)
            else:
                self._stream_anthropic(system, messages, on_token, cancel, client=client, model=mdl)
        except Exception as e:
            on_token(f"\n\n*Error: {e}*")
        finally:
            if on_done:
                on_done()

    def _stream_anthropic(self, system: str, messages: list[dict], on_token: Callback,
                           cancel: "threading.Event | None" = None,
                           client=None, model: str | None = None) -> None:
        import anthropic
        c = client or self.client
        m = model or self.model
        extra: dict = {}
        if m in _ANTHROPIC_THINKING_MODELS:
            extra["thinking"] = {"type": "adaptive"}
        api_kwargs = self._build_cached_kwargs(system, messages, m, extra=extra)
        try:
            with c.messages.stream(**api_kwargs) as stream:
                for text in stream.text_stream:
                    if cancel and cancel.is_set():
                        stream.close()
                        break
                    on_token(text)
        except anthropic.AuthenticationError:
            on_token("\n\n*Error: Invalid Anthropic API key. Check Settings.*")
        except anthropic.RateLimitError:
            on_token("\n\n*Error: Anthropic rate limit reached. Please wait and retry.*")

    @staticmethod
    def _to_openai_messages(messages: list[dict]) -> list[dict]:
        """Convert Anthropic-style content blocks to OpenAI vision format."""
        out = []
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                out.append(m)
                continue
            # content is a list of blocks - convert to OpenAI format
            parts: list[dict] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    parts.append({"type": "text", "text": block["text"]})
                elif btype == "image":
                    src = block.get("source", {})
                    mime = src.get("media_type", "image/png")
                    b64 = src.get("data", "")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
            out.append({"role": m["role"], "content": parts or content})
        return out

    @staticmethod
    def _to_responses_input(messages: list[dict]) -> list[dict]:
        """Convert internal message list to OpenAI Responses API input items."""
        out: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            text_type = "input_text" if role == "user" else "output_text"
            if isinstance(content, str):
                out.append({"role": role, "content": [{"type": text_type, "text": content}]})
                continue
            parts: list[dict] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    parts.append({"type": text_type, "text": block["text"]})
                elif btype == "image":
                    src = block.get("source", {})
                    mime = src.get("media_type", "image/png")
                    b64 = src.get("data", "")
                    parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
            out.append({"role": role, "content": parts})
        return out

    @staticmethod
    def _convert_tools_for_responses(tools: list[dict]) -> list[dict]:
        """Flatten chat.completions function tool shape to Responses API shape."""
        out: list[dict] = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                fn = t["function"]
                out.append({
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                })
            else:
                out.append(t)
        return out

    def _stream_openai(self, system: str, messages: list[dict], on_token: Callback,
                        cancel: "threading.Event | None" = None,
                        client=None, model: str | None = None) -> None:
        import openai
        c = client or self.client
        m = model or self.model
        converted = self._to_openai_messages(messages)
        full_messages = [{"role": "system", "content": system}] + converted
        try:
            stream = c.chat.completions.create(
                model=m,
                #max_tokens=4096,
                messages=full_messages,
                stream=True,
            )
            for chunk in stream:
                if cancel and cancel.is_set():
                    stream.close()
                    break
                content = chunk.choices[0].delta.content
                if content:
                    on_token(content)
        except openai.AuthenticationError:
            on_token("\n\n*Error: Invalid OpenAI API key. Check Settings.*")
        except openai.RateLimitError:
            on_token("\n\n*Error: OpenAI rate limit reached. Please wait and retry.*")

    def _stream_openai_responses(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        cancel: "threading.Event | None" = None,
        client=None,
        model: str | None = None,
    ) -> None:
        """Stream tokens from OpenAI's Responses API.

        Used by ``summarize()`` for OpenAI models so summarization runs go
        through ``/v1/responses`` instead of Chat Completions.
        """
        import openai
        c = client or self.client
        m = model or self.model
        try:
            stream_ctx = c.responses.stream(
                model=m,
                instructions=system,
                input=self._to_responses_input(messages),
            )
        except openai.AuthenticationError:
            on_token("\n\n*Error: Invalid OpenAI API key. Check Settings.*")
            return
        except openai.RateLimitError:
            on_token("\n\n*Error: OpenAI rate limit reached. Please wait and retry.*")
            return
        except Exception as e:
            log.error("ai", f"OpenAI responses.stream rejected request on model {m!r}: {e}")
            on_token(
                f"\n\n*OpenAI rejected the request: {e}. The model "
                f"({m}) may not support the Responses API. "
                f"Try a different model in Settings.*"
            )
            return

        try:
            with stream_ctx as stream:
                for event in stream:
                    if cancel and cancel.is_set():
                        stream.close()
                        break
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            on_token(delta)
        except openai.AuthenticationError:
            on_token("\n\n*Error: Invalid OpenAI API key. Check Settings.*")
        except openai.RateLimitError:
            on_token("\n\n*Error: OpenAI rate limit reached. Please wait and retry.*")

    def _complete(self, system: str, prompt: str, max_tokens: int = 1024,
                   provider: str | None = None, model: str | None = None) -> str:
        """Non-streaming single completion from the active provider."""
        from core.network import warp_disconnect
        warp_disconnect()
        client, prov, mdl = self._resolve(provider, model)
        if client is None:
            raise RuntimeError(
                f"No {prov.title()} API key configured. Add it in Settings."
            )
        if prov == "openai":
            response = client.chat.completions.create(
                model=mdl,
                #max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content.strip()
        else:
            response = client.messages.create(
                model=mdl,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()

    def _complete_structured(self, system: str, prompt: str,
                              provider: str | None = None, model: str | None = None,
                              tool: dict | None = None,
                              oai_schema: dict | None = None,
                              oai_schema_name: str = "summary_patch",
                              max_tokens: int = 1024) -> dict:
        """Structured completion returning a dict enforced by a tool / JSON schema.

        Anthropic: uses tool use so the SDK enforces the schema.
        OpenAI: uses the Responses API json_schema text format.
        Defaults to the summary-patch contract (``_PATCH_TOOL``); pass *tool* +
        *oai_schema* to reuse the same plumbing for other structured outputs
        (e.g. chapters). Returns {} on empty or unparseable responses.
        """
        if tool is None:
            tool = _PATCH_TOOL
        if oai_schema is None:
            # Strict json_schema mirror of _PATCH_TOOL.input_schema (needs
            # additionalProperties:false on every nested object for OpenAI).
            oai_schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name":    {"type": "string"},
                                "action":  {"type": "string", "enum": ["append", "replace"]},
                                "content": {"type": "string"},
                            },
                            "required": ["name", "action", "content"],
                        },
                    },
                },
                "required": ["sections"],
            }
        from core.network import warp_disconnect
        warp_disconnect()
        client, prov, mdl = self._resolve(provider, model)
        if client is None:
            raise RuntimeError(
                f"No {prov.title()} API key configured. Add it in Settings."
            )
        if prov == "openai":
            # Responses API with a JSON-schema text format that enforces the
            # requested contract (defaults to the summary patch schema).
            response = client.responses.create(
                model=mdl,
                instructions=system,
                input=[
                    {"role": "user",
                     "content": [{"type": "input_text", "text": prompt}]},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": oai_schema_name,
                        "strict": True,
                        "schema": oai_schema,
                    },
                },
            )
            text = (response.output_text or "").strip()
            return json.loads(text) if text else {}
        else:
            response = client.messages.create(
                model=mdl,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
            )
            for block in response.content:
                if block.type == "tool_use":
                    return block.input or {}
            return {}

    # ── Tool-use streaming ──────────────────────────────────────────────────────

    def _stream_with_tools(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        on_done: Callable[[], None] | None,
        cancel: "threading.Event | None" = None,
        frame_extractor: FrameExtractor | None = None,
        on_tool_event: ToolEventCallback | None = None,
        tools_anthropic: list | None = None,
        tools_openai: list | None = None,
        tool_executor: "ToolExecutor | None" = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Stream with tool-use loop (up to 50 iterations).

        Accepts either a ``frame_extractor`` (legacy screenshot-only mode)
        or generic ``tool_executor`` + tool lists for arbitrary tool handling.
        """
        try:
            from core.network import warp_disconnect
            warp_disconnect()
            client, prov, mdl = self._resolve(provider, model)
            if client is None:
                on_token(
                    f"\n\n*Error: No {prov.title()} API key configured. "
                    f"Add it in Settings.*"
                )
                return
            base_a_tools = tools_anthropic if tools_anthropic is not None else [_SCREENSHOT_TOOL]
            base_o_tools = tools_openai if tools_openai is not None else [_SCREENSHOT_FUNC_OAI]
            a_tools = base_a_tools + [_WEB_SEARCH_ANTHROPIC]
            o_tools = base_o_tools + [_WEB_SEARCH_OAI]
            if prov == "openai":
                self._tool_loop_openai(
                    system, messages, on_token, cancel, frame_extractor,
                    on_tool_event=on_tool_event,
                    tools=o_tools, tool_executor=tool_executor,
                    client=client, model=mdl,
                )
            else:
                self._tool_loop_anthropic(
                    system, messages, on_token, cancel, frame_extractor,
                    on_tool_event=on_tool_event,
                    tools=a_tools, tool_executor=tool_executor,
                    client=client, model=mdl,
                )
        except Exception as e:
            on_token(f"\n\n*Error: {e}*")
        finally:
            if on_done:
                on_done()

    def _execute_tool_anthropic(
        self,
        tu: dict,
        frame_extractor: FrameExtractor | None,
        tool_executor: "ToolExecutor | None",
        on_tool_event: ToolEventCallback | None,
    ) -> dict:
        """Execute a single tool call and return an Anthropic tool_result block."""
        # Notify frontend about the tool call.  The id is the Anthropic
        # tool_use_id, which the frontend uses to pair tool_result events with
        # their originating tool_call — required when tools execute in
        # parallel and results return out of order.
        tu_id = tu["id"]
        if on_tool_event:
            on_tool_event("tool_call", {"id": tu_id, "name": tu["name"], "input": tu["input"]})

        # Try generic executor first
        if tool_executor:
            try:
                content, is_error, summary, extra = tool_executor(tu["name"], tu["input"])
                result = {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": content,
                }
                if is_error:
                    result["is_error"] = True
                if on_tool_event:
                    payload = {"id": tu_id, "name": tu["name"], "success": not is_error, "summary": summary}
                    if extra:
                        payload.update(extra)
                    on_tool_event("tool_result", payload)
                return result
            except KeyError:
                pass  # fall through to built-in handlers
            except Exception as exec_err:
                log.error("ai", f"Tool executor failed for {tu['name']!r}: {exec_err}")
                traceback.print_exc()
                summary = f"Tool {tu['name']} failed"
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": tu_id,
                        "name": tu["name"],
                        "success": False,
                        "summary": summary,
                    })
                return {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": f"{summary}: {exec_err}",
                    "is_error": True,
                }

        # Built-in: get_screenshot
        if tu["name"] == "get_screenshot" and frame_extractor:
            ts = tu["input"].get("timestamp", 0)
            result = frame_extractor(float(ts))
            # frame_extractor returns (jpeg_bytes, url) or just jpeg_bytes for compat
            if result:
                if isinstance(result, tuple):
                    jpeg, url = result
                else:
                    jpeg, url = result, None
                b64 = base64.b64encode(jpeg).decode()
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": tu_id, "name": tu["name"], "success": True,
                        "summary": f"Captured screenshot at {ts:.1f}s", "image": b64,
                    })
                # Tell the model the image URL so it can embed it in markdown
                text_msg = f"Screenshot at {ts:.1f}s captured."
                if url:
                    text_msg += f" Embed in your response with: ![Screenshot at {ts:.1f}s]({url})"
                return {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": [
                        {"type": "text", "text": text_msg},
                        {"type": "image", "source": {
                            "type": "base64", "media_type": "image/jpeg", "data": b64,
                        }},
                    ],
                }
            else:
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": tu_id, "name": tu["name"], "success": False,
                        "summary": "Could not extract frame",
                    })
                return {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "content": "Could not extract frame - the timestamp may be out of range or no video is available.",
                    "is_error": True,
                }

        # Unknown tool
        if on_tool_event:
            on_tool_event("tool_result", {
                "id": tu_id, "name": tu["name"], "success": False,
                "summary": f"Unknown tool: {tu['name']}",
            })
        return {
            "type": "tool_result",
            "tool_use_id": tu["id"],
            "content": f"Unknown tool: {tu['name']}",
            "is_error": True,
        }

    def _tool_loop_anthropic(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        cancel: "threading.Event | None",
        frame_extractor: FrameExtractor | None,
        on_tool_event: ToolEventCallback | None = None,
        tools: list | None = None,
        tool_executor: "ToolExecutor | None" = None,
        client=None,
        model: str | None = None,
    ) -> None:
        c = client or self.client
        m = model or self.model
        msgs = list(messages)  # working copy
        max_rounds = 50
        a_tools = tools or [_SCREENSHOT_TOOL]

        had_text = False  # track if any text was emitted before tools
        for _ in range(max_rounds):
            if cancel and cancel.is_set():
                return

            extra: dict = {}
            if m in _ANTHROPIC_THINKING_MODELS:
                extra["thinking"] = {"type": "adaptive"}
            api_kwargs = self._build_cached_kwargs(
                system, msgs, m, tools=a_tools, extra=extra,
            )

            round_had_text = False
            with c.messages.stream(**api_kwargs) as stream:
                # Event-based iteration so we can detect server tools
                # (native web search) in real time alongside text.
                for event in stream:
                    if cancel and cancel.is_set():
                        stream.close()
                        return
                    if event.type == "text":
                        if not round_had_text and had_text:
                            on_token("\n\n---\n\n")
                        round_had_text = True
                        on_token(event.text)
                    elif event.type == "content_block_start":
                        cb = event.content_block
                        if getattr(cb, "type", "") == "server_tool_use" and on_tool_event:
                            on_tool_event("tool_call", {
                                "id": getattr(cb, "id", None),
                                "name": getattr(cb, "name", "web_search"),
                                "input": getattr(cb, "input", {}),
                            })
                response = stream.get_final_message()

            # Emit tool_result events for any server-side tools (web search).
            # The id pairs each result with its originating server_tool_use
            # block above — necessary when several searches run in parallel.
            if on_tool_event:
                for block in response.content:
                    if getattr(block, "type", "") == "web_search_tool_result":
                        content = getattr(block, "content", [])
                        n = sum(
                            1 for c in (content if isinstance(content, list) else [])
                            if getattr(c, "type", "") == "web_search_result"
                        )
                        on_tool_event("tool_result", {
                            "id": getattr(block, "tool_use_id", None),
                            "name": "web_search",
                            "success": True,
                            "summary": f"Found {n} result{'s' if n != 1 else ''}",
                        })

            if round_had_text:
                had_text = True

            tool_uses: list[dict] = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_uses.append({
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            if not tool_uses or response.stop_reason != "tool_use":
                return

            msgs.append({"role": "assistant", "content": response.content})

            tool_results = []
            for tu in tool_uses:
                tool_results.append(self._execute_tool_anthropic(
                    tu, frame_extractor, tool_executor, on_tool_event,
                ))

            msgs.append({"role": "user", "content": tool_results})

    def _execute_tool_openai(
        self,
        call_id: str,
        tc_name: str,
        tc_args_raw: str,
        msgs: list[dict],
        frame_extractor: FrameExtractor | None,
        tool_executor: "ToolExecutor | None",
        on_tool_event: ToolEventCallback | None,
    ) -> None:
        """Execute a single OpenAI tool call, appending Responses API items to msgs."""
        try:
            parsed_args = json.loads(tc_args_raw)
        except Exception as parse_err:
            log.warn("ai", f"OpenAI tool {tc_name!r} bad JSON args: {parse_err} -- raw={tc_args_raw!r}")
            parsed_args = {}

        if on_tool_event:
            on_tool_event("tool_call", {"id": call_id, "name": tc_name, "input": parsed_args})

        # Try generic executor first
        if tool_executor:
            try:
                content, is_error, summary, extra = tool_executor(tc_name, parsed_args)
                result_text = content if isinstance(content, str) else json.dumps(content)
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result_text,
                })
                if on_tool_event:
                    payload = {"id": call_id, "name": tc_name, "success": not is_error, "summary": summary}
                    if extra:
                        payload.update(extra)
                    on_tool_event("tool_result", payload)
                return
            except KeyError:
                pass  # not this executor's tool - fall through to built-ins
            except Exception as exec_err:
                # Don't silently swallow — surface the error to both the
                # model (so it can correct course) and the server log.
                log.error("ai", f"OpenAI tool {tc_name!r} executor raised: {exec_err}")
                traceback.print_exc()
                err_text = f"Tool {tc_name!r} failed: {exec_err}"
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": err_text,
                })
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": call_id, "name": tc_name, "success": False, "summary": err_text,
                    })
                return

        # Built-in: get_screenshot
        if tc_name == "get_screenshot" and frame_extractor:
            ts = float(parsed_args.get("timestamp", 0))
            result = frame_extractor(ts)
            if result:
                if isinstance(result, tuple):
                    jpeg, url = result
                else:
                    jpeg, url = result, None
                b64 = base64.b64encode(jpeg).decode()
                text_msg = f"Screenshot at {ts:.1f}s captured."
                if url:
                    text_msg += f" Embed in your response with: ![Screenshot at {ts:.1f}s]({url})"
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": text_msg,
                })
                # function_call_output.output is text-only; provide the image as a
                # follow-up user input so vision models can ground their answer.
                msgs.append({
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}"},
                    ],
                })
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": call_id, "name": tc_name, "success": True,
                        "summary": f"Captured screenshot at {ts:.1f}s", "image": b64,
                    })
            else:
                msgs.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "Could not extract frame - the timestamp may be out of range or no video is available.",
                })
                if on_tool_event:
                    on_tool_event("tool_result", {
                        "id": call_id, "name": tc_name, "success": False, "summary": "Could not extract frame",
                    })
            return

        # Unknown tool
        msgs.append({
            "type": "function_call_output",
            "call_id": call_id,
            "output": f"Unknown tool: {tc_name}",
        })
        if on_tool_event:
            on_tool_event("tool_result", {
                "id": call_id, "name": tc_name, "success": False, "summary": f"Unknown tool: {tc_name}",
            })

    def _tool_loop_openai(
        self,
        system: str,
        messages: list[dict],
        on_token: Callback,
        cancel: "threading.Event | None",
        frame_extractor: FrameExtractor | None,
        on_tool_event: ToolEventCallback | None = None,
        tools: list | None = None,
        tool_executor: "ToolExecutor | None" = None,
        client=None,
        model: str | None = None,
    ) -> None:
        c = client or self.client
        m = model or self.model
        o_tools = self._convert_tools_for_responses(tools or [_SCREENSHOT_FUNC_OAI])
        max_rounds = 50

        next_input = self._to_responses_input(messages)
        previous_response_id: str | None = None
        had_text = False
        tool_names = sorted({t.get("name") or t.get("type") for t in o_tools if t})
        log.info("ai", f"OpenAI tool loop on model={m!r}, tools={tool_names}")

        for round_idx in range(max_rounds):
            if cancel and cancel.is_set():
                return

            kwargs = {
                "model": m,
                "instructions": system,
                "input": next_input,
                "tools": o_tools,
            }
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id

            function_calls: list[dict] = []
            web_search_seen = False
            round_had_text = False
            cancelled = False

            try:
                stream_ctx = c.responses.stream(**kwargs)
            except Exception as e:
                log.error("ai", f"OpenAI responses.stream rejected request on model {m!r}: {e}")
                on_token(
                    f"\n\n*OpenAI rejected the request: {e}. The model "
                    f"({m}) may not support tool use via the Responses API. "
                    f"Try a different model in Settings.*"
                )
                return

            with stream_ctx as stream:
                for event in stream:
                    if cancel and cancel.is_set():
                        cancelled = True
                        stream.close()
                        break
                    etype = getattr(event, "type", "")
                    if etype == "response.output_text.delta":
                        delta = getattr(event, "delta", "") or ""
                        if delta:
                            if not round_had_text and had_text:
                                on_token("\n\n---\n\n")
                            round_had_text = True
                            on_token(delta)
                    elif etype == "response.output_item.done":
                        item = getattr(event, "item", None)
                        itype = getattr(item, "type", "") if item is not None else ""
                        if itype == "function_call":
                            function_calls.append({
                                "call_id": getattr(item, "call_id", "") or "",
                                "name": getattr(item, "name", "") or "",
                                "arguments": getattr(item, "arguments", "") or "",
                            })
                        elif itype == "web_search_call" and not web_search_seen and on_tool_event:
                            web_search_seen = True
                            ws_id = getattr(item, "id", "") or "openai-websearch"
                            on_tool_event("tool_call", {"id": ws_id, "name": "web_search", "input": {}})
                            on_tool_event("tool_result", {
                                "id": ws_id,
                                "name": "web_search",
                                "success": True,
                                "summary": "Web search performed",
                            })
                    elif etype == "response.error" or etype == "error":
                        err = getattr(event, "error", None) or getattr(event, "message", None)
                        log.error("ai", f"OpenAI Responses stream error event: {err}")

                if cancelled:
                    return
                final = stream.get_final_response()
                previous_response_id = getattr(final, "id", None) or previous_response_id

            if round_had_text:
                had_text = True

            log.info(
                "ai",
                f"OpenAI round {round_idx}: {len(function_calls)} function call(s), "
                f"text={'yes' if round_had_text else 'no'}",
            )

            if not function_calls:
                # Diagnostic: model gave a final answer without using tools at
                # all. If the user expected tool use this is the smoking gun.
                if round_idx == 0 and tool_executor and tool_names:
                    log.warn(
                        "ai",
                        f"OpenAI model {m!r} produced a response without "
                        f"calling any of the provided tools: {tool_names}. "
                        f"If tool use was expected, verify the model "
                        f"supports the Responses API + function calling.",
                    )
                return

            next_input = []
            for call in function_calls:
                self._execute_tool_openai(
                    call["call_id"], call["name"], call["arguments"], next_input,
                    frame_extractor, tool_executor, on_tool_event,
                )

    # ── Summary helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_sections(summary: str) -> dict[str, str]:
        """Parse a ## markdown summary into {section_name: content} dict."""
        sections: dict[str, str] = {}
        current: str | None = None
        lines: list[str] = []
        for line in summary.splitlines():
            m = re.match(r"^##\s+(.+)$", line)
            if m:
                if current is not None:
                    sections[current] = "\n".join(lines).strip()
                current = m.group(1).strip()
                lines = []
            elif current is not None:
                lines.append(line)
        if current is not None:
            sections[current] = "\n".join(lines).strip()
        return sections

    @staticmethod
    def _build_summary(sections: dict[str, str]) -> str:
        """Rebuild markdown from a sections dict, preserving insertion order."""
        return "\n\n".join(
            f"## {name}\n{content}"
            for name, content in sections.items()
            if content.strip()
        )
