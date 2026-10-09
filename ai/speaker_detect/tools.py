"""Chat tools for AI speaker detection (per-meeting chat and global chat).

How much a run may change on its own comes from the user's own latest
message (app.py passes it to instructions.compile_spec), never from what the
model puts in a tool call: autonomy is the user's to grant. The model only
picks the meeting and, optionally, which speakers to look at.
"""
from __future__ import annotations

NAMES = {"identify_speakers", "get_speaker_insights", "apply_speaker_changes",
         "undo_speaker_changes"}

_SESSION = {
    "type": "string",
    "description": ("The meeting. In a meeting's own chat this defaults to that meeting, so "
                    "you can leave it out."),
}

TOOLS = [
    {
        "name": "identify_speakers",
        "description": (
            "Work out who is speaking in a meeting from its screen recording: reads who the "
            "meeting app (Zoom, Teams, Meet, ...) showed as speaking, checks each reading "
            "against the voices, then names the meeting's speakers, corrects wrong names, "
            "and moves lines to the right person when one speaker holds two people. How much "
            "it changes by itself (suggest only, apply confident changes, or act fully) and "
            "whether voice profiles are updated follow the user's own words in their latest "
            "message and their settings; you cannot change that. Every change is journaled "
            "and can be undone. Takes about 10 to 60 seconds. Returns what was applied, what "
            "waits as suggestions (with ids for apply_speaker_changes), and anything that "
            "needs the user's eye. Only works for meetings with a screen recording."),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": _SESSION,
                "focus": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("Optional: speaker keys or names to look at (\"Speaker "
                                    "4\"). Leave empty to check every speaker."),
                },
            },
        },
    },
    {
        "name": "get_speaker_insights",
        "description": (
            "READ-ONLY. The latest speaker detection for a meeting: its status and report, "
            "suggestions still waiting (with ids), and the recent history of speaker changes "
            "with their ids for undo_speaker_changes."),
        "input_schema": {"type": "object", "properties": {"session_id": _SESSION}},
    },
    {
        "name": "apply_speaker_changes",
        "description": (
            "WRITES. Accept or dismiss suggestions from identify_speakers by id. Accepting "
            "applies them (undoable, never training voice profiles); dismissing drops them and "
            "is remembered so the same name is not suggested again. Only call this after "
            "the user has said which suggestions to accept or dismiss: suggestions from a run "
            "started in this same reply are refused until the user has seen them."),
        "input_schema": {
            "type": "object",
            "properties": {
                "change_ids": {"type": "array", "items": {"type": "integer"}},
                "action": {"type": "string", "enum": ["accept", "dismiss"]},
                "user_confirmed": {
                    "type": "boolean",
                    "description": "True only when the user asked for exactly this.",
                },
            },
            "required": ["change_ids", "action", "user_confirmed"],
        },
    },
    {
        "name": "undo_speaker_changes",
        "description": (
            "Undo speaker changes: a whole detection run (run_id) or single changes "
            "(change_ids). Puts names, lines and voice samples back exactly as they were. "
            "A change whose speakers were edited again since is reported and left alone."),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "change_ids": {"type": "array", "items": {"type": "integer"}},
            },
        },
    },
]

TOOLS_OAI = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["input_schema"]}}
             for t in TOOLS]

CONTRACT = (
    "\n\n## Speakers from the screen recording\n"
    "When the user asks who is speaking, or to name, fix or check the speakers of a "
    "meeting that has a screen recording, use `identify_speakers`. Their own words decide "
    "how much it changes by itself, so just call it; then say plainly what it changed, "
    "what it only suggests (offer to accept them), and anything it flagged. Mention that "
    "any change can be undone. Never claim a change it did not report."
)
