"""The user's own words for a run, as a run spec.

"Fix the speakers and update the voice profiles" should simply happen;
"who is Speaker 4?" should change nothing. One small structured call reads
the request; a keyword reading covers the common phrasings when no model is
reachable and handles an empty request without a call at all.

Only the user's typed words ever reach this (the meeting page's box, their
chat message, an Agent API request's ``instructions``). Transcripts, titles
and anything read off the screen never do: autonomy is the user's to grant.
"""
from __future__ import annotations

import re
from dataclasses import replace

from core import log

from ai.speaker_detect.runs import RunSpec

AUTONOMY = ["suggest", "apply_confident", "act_fully"]
LIBRARY = ["follow_autonomy", "on_accept", "never"]
DEPTHS = ["quick", "standard", "thorough"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": ["identify", "question"]},
        "autonomy": {"anyOf": [{"type": "string", "enum": AUTONOMY}, {"type": "null"}]},
        "library_writes": {"anyOf": [{"type": "string", "enum": LIBRARY}, {"type": "null"}]},
        "depth": {"anyOf": [{"type": "string", "enum": DEPTHS}, {"type": "null"}]},
        "recheck_user_labels": {"type": "boolean"},
        "trust_screen": {"type": "boolean"},
        "targets": {"type": "array", "items": {"type": "string"}},
        "protect": {"type": "array", "items": {"type": "string"}},
        "camera_off": {"type": "array", "items": {"type": "string"}},
        "hints": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["intent", "autonomy", "library_writes", "depth", "recheck_user_labels",
                 "trust_screen", "targets", "protect", "camera_off", "hints"],
}

TOOL = {"name": "run_spec", "description": "The request as a run spec.",
        "input_schema": SCHEMA}

SYSTEM = """\
You turn a user's request about naming the speakers of their recorded \
meetings into settings for an automatic speaker-identification run. Only \
set a field when the request says so; otherwise use null or false or [].

- intent: "question" when they only ask who someone is or what you think; \
otherwise "identify".
- autonomy: "act_fully" when they tell you to just do it, not ask, fix \
everything; "suggest" when they want to review first or only want \
suggestions; "apply_confident" when they ask for confident changes only.
- library_writes: "follow_autonomy" when they want voice profiles updated or \
trained; "never" when they say not to touch or train the voice library; \
"on_accept" when they want profiles updated only for what they approve.
- depth: "thorough" for careful or deep, "quick" for a fast pass.
- recheck_user_labels: true only if they ask to recheck names they set \
themselves.
- trust_screen: true if they say to trust the screen or video over voice.
- targets: speaker keys or names they want looked at ("Speaker 4").
- protect: names or keys they say to leave alone.
- camera_off: people they say never turn their camera on.
- hints: visual or contextual hints, one short sentence each ("Bob Ruiz \
wears a green shirt").
"""


def _keywords(text: str) -> dict:
    t = text.lower()
    out: dict = {"intent": "identify"}
    if re.search(r"\b(who is|who's|whos|which one|is speaker \d+)\b", t) and \
            not re.search(r"\b(fix|name|rename|update|apply)\b", t):
        out["intent"] = "question"
    if re.search(r"(just (do|fix|go)|don'?t ask|without asking|go ahead|do it all|fix (them|it) all"
                 r"|fix everything|act fully)", t):
        out["autonomy"] = "act_fully"
    elif re.search(r"(only suggest|just suggest|suggestions? only|let me (review|check)|"
                   r"don'?t change)", t):
        out["autonomy"] = "suggest"
    if re.search(r"(don'?t|do not|never) (touch|train|update|change) (the )?(voice|profile)", t):
        out["library_writes"] = "never"
    elif re.search(r"(update|train|teach|improve) (the |my )?(voice )?(profiles?|library)", t):
        out["library_writes"] = "follow_autonomy"
    if re.search(r"\b(thorough|carefully|deep|every line)\b", t):
        out["depth"] = "thorough"
    elif re.search(r"\b(quick|fast)\b", t):
        out["depth"] = "quick"
    if re.search(r"recheck (my|the names i|names i)", t):
        out["recheck_user_labels"] = True
    if re.search(r"trust (the )?(screen|video)", t):
        out["trust_screen"] = True
    out["targets"] = re.findall(r"speaker \d+", text, re.I)
    return out


def cap(spec: RunSpec, ceiling: RunSpec) -> RunSpec:
    """``spec`` lowered to at most what ``ceiling`` (the user's Settings)
    allows: how much it may do on its own, how much it may teach the voice
    library, and whether names the user typed may change. For runs an agent
    starts: an agent can ask for less, never more."""
    if AUTONOMY.index(spec.autonomy) > AUTONOMY.index(ceiling.autonomy):
        spec.autonomy = ceiling.autonomy
    order = ["never", "on_accept", "follow_autonomy"]
    if order.index(spec.library_writes) > order.index(ceiling.library_writes):
        spec.library_writes = ceiling.library_writes
    spec.recheck_user_labels = spec.recheck_user_labels and ceiling.recheck_user_labels
    # Trusting the screen over the voice library lifts a penalty and so can
    # carry a change over the bar to apply: the user's to say, not an agent's.
    spec.trust_screen = spec.trust_screen and ceiling.trust_screen
    return spec


def compile_spec(text: str, defaults: RunSpec, complete=None) -> tuple[RunSpec, list[str]]:
    """(spec, chips): the run spec for ``text`` over the settings' defaults,
    and short labels saying how it was read (shown under the box).

    ``complete(system, prompt, tool, schema) -> dict`` is a structured model
    call; without it, or if it fails, the keyword reading is used."""
    text = (text or "").strip()
    found: dict = {}
    if text:
        found = _keywords(text)
        if complete is not None:
            try:
                got = complete(SYSTEM, f"Request: {text}", TOOL, SCHEMA) or {}
                if isinstance(got, dict) and got:
                    found = got
            except Exception as e:  # noqa: BLE001 - the keyword reading stands
                log.warn("speakers", f"Reading the request failed, using keywords: {e}")
    spec = replace(defaults)
    if found.get("intent") in ("identify", "question"):
        spec.intent = found["intent"]
    if found.get("autonomy") in AUTONOMY:
        spec.autonomy = found["autonomy"]
    if spec.intent == "question":
        spec.autonomy = "suggest"
    if found.get("library_writes") in LIBRARY:
        spec.library_writes = found["library_writes"]
    if found.get("depth") in DEPTHS:
        spec.depth = found["depth"]
    spec.recheck_user_labels = bool(found.get("recheck_user_labels")) or defaults.recheck_user_labels
    spec.trust_screen = bool(found.get("trust_screen")) or defaults.trust_screen
    # "speaker 4" is the key "Speaker 4".
    spec.targets = [re.sub(r"^speaker\s+(\d+)$", r"Speaker \1", str(x).strip(), flags=re.I)
                    for x in (found.get("targets") or [])][:20]
    spec.hints = list(defaults.hints) + [str(h) for h in (found.get("hints") or [])][:6]
    spec.constraints = list(defaults.constraints) + [
        {"kind": "protect", "subject": {"name": str(n)}} for n in (found.get("protect") or [])]
    camera_off = [str(n) for n in (found.get("camera_off") or [])]
    if camera_off:
        spec.hints.append("Never on camera: " + ", ".join(camera_off) + ".")

    chips = [{"suggest": "Suggest only", "apply_confident": "Apply confident changes",
              "act_fully": "Act fully"}[spec.autonomy],
             {"follow_autonomy": "Voice profiles: update", "on_accept": "Voice profiles: when I accept",
              "never": "Voice profiles: don't touch"}[spec.library_writes],
             {"quick": "Quick", "standard": "Standard", "thorough": "Thorough"}[spec.depth]]
    if spec.intent == "question":
        chips.insert(0, "Question")
    if spec.targets:
        chips.append("Looking at " + ", ".join(spec.targets))
    if spec.recheck_user_labels:
        chips.append("Rechecking names you set")
    if spec.trust_screen:
        chips.append("Trusting the screen")
    for c in spec.constraints:
        if c.get("kind") == "protect":
            chips.append(f"Leaving {c['subject'].get('name') or c['subject'].get('key')} alone")
    for h in found.get("hints") or []:
        chips.append(f"Hint: {h}")
    return spec, chips
