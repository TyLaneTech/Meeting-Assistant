"""Plan, confirm and apply bulk speaker reassignments.

The chat agent never renames speakers directly. It calls :func:`build_plan` to
describe exactly what would change, shows that plan to the user in prose, and
only :func:`apply_plan` performs the write, using the opaque token the plan was
pinned under. A token is single use, expires after ten minutes, and refuses to
apply in the same chat turn that minted it, so an apply always follows a real
user confirmation.

Every database touch goes through a :class:`RelabelDeps` bundle of callables,
so app.py wires in the real storage and fingerprint database while tests inject
fakes. Nothing in this module imports storage.
"""
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

# How long a minted plan token stays usable. Overridden in tests.
TOKEN_TTL_SEC = 600.0

MATCH_MODES = ("exact", "contains")
SCOPES = ("session", "library")

# How many per-session lines the human summary spells out before it collapses
# the rest into "and N more".
_SUMMARY_SESSION_CAP = 8

# The owner's own voice profile and microphone key are load-bearing: merging
# the profile away would orphan every "is this me" check, and repointing the
# mic key would hand their own audio to someone else's profile.
_ME_REFUSAL = (
    "That is the owner's own voice. Their own name is changed in Settings, "
    "under the Me speaker, not through a bulk reassignment. Tell them that "
    "and do not try again with a different scope."
)


@dataclass
class RelabelDeps:
    """Everything the planner and applier are allowed to touch.

    ``find_labels(name, match, session_ids)`` returns speaker_labels rows as
    dicts with session_id, speaker_key, name, color, global_id, is_noise,
    title and started_at. ``session_ids`` of None means the whole library.
    """

    find_labels: Callable[[str, str, "list[str] | None"], list]
    speaker_time_stats: Callable[[str], list]
    count_label_overrides: Callable[[str, str, "list[str] | None"], dict]
    find_profile_by_name: Callable[[str], "dict | None"]
    create_profile: Callable[[str], str]
    bulk_link: Callable[[str, str], dict]
    merge_profiles: Callable[[str, str], dict]
    patch_session: Callable[[str, list, str], dict]
    # global_id -> the speaker_labels rows currently linked to that profile
    linked_labels: Callable[[str], list] = field(default=lambda global_id: [])
    # session ids -> {session_id: {title, started_at}}
    session_info: Callable[[list], dict] = field(default=lambda session_ids: {})
    # Voice Library profile id reserved for the owner's own voice, or None
    me_profile_id: Callable[[], "str | None"] = field(default=lambda: None)
    # Reserved per-session speaker key for the owner's microphone
    me_key: str = "me"
    # False when the Voice Library is not loaded: no profile writes are allowed
    library_ready: Callable[[], bool] = field(default=lambda: True)
    # (session_ids, from_name, to_name) -> number of summary refreshes queued
    queue_summaries: Callable[[list, str, str], int] = field(
        default=lambda session_ids, from_name, to_name: 0
    )


def normalize(name: str) -> str:
    """Comparison form of a speaker name: trimmed and case-folded."""
    return (name or "").strip().casefold()


# -- Token store --------------------------------------------------------------

class _TokenStore:
    """Single-use, TTL-bounded plan tokens.

    The plan pinned under a token is what apply executes. The model can never
    re-describe a plan at apply time, only hand back the token it was given.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, e in self._entries.items() if now >= e["expires_at"]]:
            self._entries.pop(token, None)

    def mint(self, plan: dict, request_id: "str | None") -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._purge()
            self._entries[token] = {
                "plan": plan,
                "request_id": request_id,
                "expires_at": self._clock() + TOKEN_TTL_SEC,
            }
        return token

    def peek(self, token: str) -> "dict | None":
        with self._lock:
            self._purge()
            return self._entries.get(token)

    def consume(self, token: str) -> "dict | None":
        with self._lock:
            self._purge()
            return self._entries.pop(token, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_TOKENS = _TokenStore()


def mint_token(plan: dict, request_id: "str | None" = None) -> str:
    """Pin a plan under a fresh single-use token and return the token."""
    return _TOKENS.mint(plan, request_id)


def get_plan(token: str) -> "dict | None":
    """Return the plan pinned under a token without consuming it."""
    entry = _TOKENS.peek(token)
    return entry["plan"] if entry else None


def consume(token: str) -> "dict | None":
    """Consume a token and return its plan, or None when it is not usable."""
    entry = _TOKENS.consume(token)
    return entry["plan"] if entry else None


def cancel(token: str) -> bool:
    """Drop a token so its plan can never be applied. True when one was held."""
    return _TOKENS.consume(token) is not None


# -- Planning -----------------------------------------------------------------

def _matches(row_name: str, from_name: str, match: str) -> bool:
    """Whether a stored label name matches what the user asked to replace."""
    row = normalize(row_name)
    target = normalize(from_name)
    return target in row if match == "contains" else row == target


def _fmt_minutes(seconds: float) -> str:
    return f"{(seconds or 0) / 60.0:.1f} min"


def _linked_note(count: int, from_name: str, to_name: str) -> str:
    """The one line describing labels a profile merge renames indirectly."""
    return (
        f'{count} more label(s) are linked to the "{from_name}" or '
        f'"{to_name}" voice profile under other names and will be renamed too.'
    )


def build_plan(
    from_name: str,
    to_name: str,
    scope: str,
    session_ids: "list[str] | None",
    match: str = "exact",
    *,
    deps: RelabelDeps,
) -> dict:
    """Describe every speaker label that would be renamed, and how.

    ``session_ids`` is the caller-resolved scope: exactly one id for scope
    "session", the filtered id list (or None for the whole library) for scope
    "library". No write happens here.
    """
    from_name = (from_name or "").strip()
    to_name = (to_name or "").strip()
    if not from_name:
        raise ValueError("from_name is required")
    if not to_name:
        raise ValueError("to_name is required")
    if match not in MATCH_MODES:
        raise ValueError(f"match must be one of {MATCH_MODES}")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if scope == "session" and (not session_ids or len(session_ids) != 1):
        raise ValueError("scope 'session' needs exactly one session id")

    scoped_ids = list(session_ids) if session_ids is not None else None
    # The name test is re-applied here so an "exact" plan can never widen into
    # a partial one, whatever the lookup behind find_labels does.
    scoped_raw = deps.find_labels(from_name, match, scoped_ids)
    rows = [
        r for r in scoped_raw
        if not r.get("is_noise") and _matches(r.get("name") or "", from_name, match)
    ]

    library_ready = bool(deps.library_ready())
    to_profile = deps.find_profile_by_name(to_name) if library_ready else None
    from_profile = deps.find_profile_by_name(from_name) if library_ready else None
    to_gid = to_profile["id"] if to_profile else None
    from_gid = from_profile["id"] if from_profile else None
    from_embeddings = int((from_profile or {}).get("emb_count") or 0)

    # -- The owner's own identity is never reassigned from here ---------------
    me_key = deps.me_key or "me"
    me_id = deps.me_profile_id() if library_ready else None
    if me_id and from_gid and from_gid == me_id:
        raise ValueError(_ME_REFUSAL)
    if any(r["speaker_key"] == me_key for r in rows):
        raise ValueError(_ME_REFUSAL)

    # -- Blast radius of the library-wide primitives --------------------------
    # bulk_link_by_name and merge_global_speakers are unscoped UPDATEs: they
    # rewrite EVERY speaker_labels row carrying the name, noise rows included,
    # and bulk_link then trains the target voice profile from that audio. They
    # are only safe when the pinned set is exactly the library set, so the
    # comparison below deliberately counts noise rows as extras.
    pinned_pairs = {(r["session_id"], r["speaker_key"]) for r in rows}
    if scope == "library":
        library_raw = (scoped_raw if scoped_ids is None
                       else deps.find_labels(from_name, match, None))
        library_rows = [
            r for r in library_raw
            if _matches(r.get("name") or "", from_name, match)
        ]
        extras = [r for r in library_rows
                  if (r["session_id"], r["speaker_key"]) not in pinned_pairs]
        noise_extra = sum(1 for r in extras if r.get("is_noise"))
        out_of_scope = len(extras) - noise_extra
        full_coverage = not extras
    else:
        noise_extra = 0
        out_of_scope = 0
        full_coverage = True

    # The target being the owner's own profile is not refused (renaming a
    # mislabelled speaker to the owner is a legitimate fix), but it must never
    # go through bulk_link: that trains the owner's voice profile on whatever
    # audio the matched labels carry.
    to_is_me = bool(me_id and to_gid and to_gid == me_id)

    if scope == "session":
        strategy = "session_patch"
    elif not library_ready:
        strategy = "session_patch"
    elif match != "exact" or not full_coverage:
        strategy = "session_patch"
    elif to_is_me:
        strategy = "session_patch"
    elif to_gid and from_gid and to_gid != from_gid and from_embeddings > 0:
        strategy = "merge_profiles"
    else:
        strategy = "bulk_link"

    # -- A Voice Library profile with no labels of its own ---------------------
    # "Reassign every Justin to Jennifer Davis" can mean the Voice Library
    # holds a "Justin" identity (voice samples, maybe labels under other names)
    # while no transcript label is literally named Justin. That is still a
    # real merge: the samples move to the target profile and the source entry
    # goes away. It needs both profiles, an exact library-wide request, and a
    # source that actually carries voice samples.
    profile_only = bool(
        not rows and scope == "library" and match == "exact" and library_ready
        and from_gid and to_gid and to_gid != from_gid
        and from_embeddings > 0 and not to_is_me
    )
    if profile_only:
        strategy = "merge_profiles"

    # -- Labels the merge would rename without ever matching the name ---------
    # merge_global_speakers repoints global_id and then rewrites the name of
    # every label on the kept profile, so a key auto-linked to the source
    # profile as "Speaker 3" gets renamed too. Those rows join the plan so the
    # user sees them, or the merge is abandoned if they cannot be described.
    touched = [dict(r, linked_via_profile=False) for r in rows]
    undescribable = 0
    linked_me_rows = 0
    if strategy == "merge_profiles":
        # Rows on the source profile are renamed whatever they are called now,
        # and so are rows already on the target profile that carry a different
        # display name: the merge ends by rewriting the name of every label on
        # the kept profile, and link_session_speaker sets global_id without
        # touching the name, so such rows do exist.
        extra_linked = []
        seen_extra: set = set()
        for gid, keep_if_named in ((from_gid, None), (to_gid, to_name)):
            for row in (deps.linked_labels(gid) or []):
                pair = (row.get("session_id"), row.get("speaker_key"))
                if pair in pinned_pairs or pair in seen_extra:
                    continue
                if keep_if_named is not None and \
                        normalize(row.get("name") or "") == normalize(keep_if_named):
                    continue  # already shows the target name, nothing changes
                seen_extra.add(pair)
                extra_linked.append(dict(row))

        # The owner's own microphone key riding on one of these profiles is not
        # a reason to refuse the whole request, only a reason not to merge.
        linked_me_rows = sum(1 for row in extra_linked
                             if row.get("speaker_key") == me_key)
        if linked_me_rows:
            strategy = "session_patch"
        else:
            info = deps.session_info([row["session_id"] for row in extra_linked]) or {}
            undescribable = sum(1 for row in extra_linked
                                if row["session_id"] not in info)
            if undescribable:
                strategy = "session_patch"
            else:
                for row in extra_linked:
                    meta = info[row["session_id"]]
                    row["title"] = meta.get("title") or "(untitled)"
                    row["started_at"] = meta.get("started_at") or ""
                    row["linked_via_profile"] = True
                    touched.append(row)

    # Group by session, attaching real per-key talk stats. speaker_time_stats
    # honours source_override, so a key whose segments were reassigned reports
    # the count the transcript actually shows.
    stats_cache: dict[str, dict] = {}
    by_session: dict[str, dict] = {}
    order: list[str] = []
    for row in touched:
        sid = row["session_id"]
        if sid not in stats_cache:
            stats_cache[sid] = {
                s["speaker_key"]: s for s in (deps.speaker_time_stats(sid) or [])
            }
        stat = stats_cache[sid].get(row["speaker_key"], {})
        if sid not in by_session:
            order.append(sid)
            by_session[sid] = {
                "session_id": sid,
                "title": row.get("title") or "(untitled)",
                "started_at": row.get("started_at") or "",
                "keys": [],
                "segment_count": 0,
                "talk_seconds": 0.0,
            }
        entry = by_session[sid]
        entry["keys"].append({
            "speaker_key": row["speaker_key"],
            "current_name": row.get("name") or row["speaker_key"],
            "global_id": row.get("global_id"),
            "linked_via_profile": bool(row.get("linked_via_profile")),
            "segment_count": int(stat.get("segment_count") or 0),
            "talk_seconds": round(float(stat.get("talk_seconds") or 0.0), 1),
        })
        entry["segment_count"] += int(stat.get("segment_count") or 0)
        entry["talk_seconds"] = round(
            entry["talk_seconds"] + float(stat.get("talk_seconds") or 0.0), 1
        )

    sessions = [by_session[sid] for sid in order]
    key_count = sum(len(s["keys"]) for s in sessions)
    linked_count = sum(1 for s in sessions for k in s["keys"]
                       if k["linked_via_profile"])
    segment_total = sum(s["segment_count"] for s in sessions)
    talk_total = round(sum(s["talk_seconds"] for s in sessions), 1)

    warnings: list[str] = []
    overrides = deps.count_label_overrides(from_name, match, scoped_ids) or {}
    override_total = sum(int(v or 0) for v in overrides.values())
    if override_total:
        warnings.append(
            f'{override_total} transcript line(s) carry a per-line label of '
            f'"{from_name}" set by hand. Those lines keep their own label and '
            f'will not change.'
        )
    if not library_ready:
        warnings.append(
            "Voice Library is not loaded; names are changed per meeting only."
        )
    if to_is_me:
        warnings.append(
            f'"{to_name}" is your own voice profile. Each renamed speaker\'s '
            f'audio is added to your own voice profile, as a manual rename '
            f'would do; use the Voice Library cleanup if a wrong voice ends up '
            f'there.'
        )
    if linked_me_rows:
        warnings.append(
            f'One label linked to that profile is your own voice; it is left '
            f'alone, so the voice profiles are not merged and each meeting is '
            f'renamed on its own.'
            if linked_me_rows == 1 else
            f'{linked_me_rows} labels linked to that profile are your own '
            f'voice; they are left alone, so the voice profiles are not merged '
            f'and each meeting is renamed on its own.'
        )
    if out_of_scope or noise_extra:
        reasons = []
        if out_of_scope:
            reasons.append(f"{out_of_scope} outside the current filter")
        if noise_extra:
            reasons.append(f"{noise_extra} marked as noise")
        warnings.append(
            f'{out_of_scope + noise_extra} other speaker label(s) named '
            f'"{from_name}" will be left alone ({", ".join(reasons)}), so each '
            f'meeting is renamed on its own rather than library-wide.'
        )
    if undescribable:
        warnings.append(
            f'{undescribable} label(s) linked to the "{from_name}" or '
            f'"{to_name}" voice profile belong to meetings that could not be '
            f'read, so the voice profiles will not be merged and each meeting '
            f'is renamed on its own.'
        )
    if linked_count:
        warnings.append(_linked_note(linked_count, from_name, to_name))
    if strategy == "bulk_link":
        warnings.append("Renamed labels take the profile's colour.")
    if normalize(from_name) == normalize(to_name):
        warnings.append(
            "The old and new names differ only in spacing or capitalisation, so "
            "this is a spelling fix rather than a reassignment."
        )
    if strategy == "merge_profiles":
        warnings.append(
            f'"{from_name}" and "{to_name}" both exist in the Voice Library. '
            f'The two voice profiles will be merged into "{to_name}", which '
            f'cannot be undone from the app.'
        )
    if not rows and profile_only:
        warnings.append(
            f'No transcript speaker is named "{from_name}". The Voice Library '
            f'has a "{from_name}" profile with {from_embeddings} voice '
            f'sample(s); merging it into "{to_name}" adds those samples to that '
            f'profile and removes the "{from_name}" entry.'
        )
    elif not rows:
        warnings.append(
            f'No speaker is named "{from_name}" in this scope. Check the '
            f'spelling with list_speakers before trying again.'
        )

    plan = {
        "from_name": from_name,
        "to_name": to_name,
        "scope": scope,
        "match": match,
        "session_ids": scoped_ids,
        "sessions": sessions,
        "session_count": len(sessions),
        "key_count": key_count,
        "segment_total": segment_total,
        "talk_seconds": talk_total,
        "strategy": strategy,
        "to_profile_id": to_gid,
        "from_profile_id": from_gid,
        "creates_profile": strategy == "bulk_link" and not to_gid,
        "linked_via_profile_count": linked_count,
        "library_ready": library_ready,
        "target_is_me": to_is_me,
        "profile_only": profile_only,
        "warnings": warnings,
    }
    plan["summary"] = _plan_summary(plan)
    return plan


_STRATEGY_NOTE = {
    "session_patch": "renaming each speaker in place, meeting by meeting",
    "bulk_link": "renaming every matching label and linking them to one voice profile",
    "merge_profiles": "merging the two voice profiles, then renaming every matching label",
}


def _plan_summary(plan: dict) -> str:
    """One-paragraph, human-readable statement of what apply would do."""
    if not plan["sessions"] and plan.get("profile_only"):
        return (
            f'No transcript speaker is named "{plan["from_name"]}". Merge the '
            f'"{plan["from_name"]}" Voice Library profile into '
            f'"{plan["to_name"]}": its voice samples move over and the '
            f'"{plan["from_name"]}" entry is removed. No transcript label changes.'
        )
    if not plan["sessions"]:
        return (
            f'Nothing to change: no speaker named "{plan["from_name"]}" was '
            f'found in this scope.'
        )
    where = "this meeting" if plan["scope"] == "session" else (
        f"{plan['session_count']} meeting"
        + ("s" if plan["session_count"] != 1 else "")
    )
    lines = [
        f'Rename {plan["key_count"]} speaker label'
        + ("s" if plan["key_count"] != 1 else "")
        + f' from "{plan["from_name"]}" to "{plan["to_name"]}" across {where} '
        f'({plan["segment_total"]} transcript segments, '
        f'{_fmt_minutes(plan["talk_seconds"])} of talk time), '
        f'{_STRATEGY_NOTE[plan["strategy"]]}.'
    ]
    linked = plan.get("linked_via_profile_count") or 0
    if linked:
        lines.append(_linked_note(linked, plan["from_name"], plan["to_name"]))
    shown = plan["sessions"][:_SUMMARY_SESSION_CAP]
    for sess in shown:
        extra = sum(1 for k in sess["keys"] if k.get("linked_via_profile"))
        via = f", {extra} via the voice profile" if extra else ""
        lines.append(
            f"- {sess['title']} ({sess['started_at'][:10]}): "
            f"{len(sess['keys'])} label(s){via}, {sess['segment_count']} segments"
        )
    remaining = len(plan["sessions"]) - len(shown)
    if remaining > 0:
        lines.append(f"- and {remaining} more meeting(s)")
    return "\n".join(lines)


# -- Applying -----------------------------------------------------------------

class RelabelRefused(ValueError):
    """A plan could not be applied. The message is safe to show the user."""

    # Session ids that were written before the failure. Replaced per instance;
    # the class-level default is immutable so it can never be shared or grown.
    applied_session_ids: "tuple | list" = ()


def _reverify_coverage(plan: dict, deps: RelabelDeps) -> None:
    """Refuse a library-wide write if the library grew since planning.

    bulk_link and merge_profiles rewrite every row carrying the name, so a
    label that appeared during the confirmation window would be changed without
    the user ever having seen it.
    """
    pinned = {(s["session_id"], k["speaker_key"])
              for s in plan["sessions"] for k in s["keys"]}
    current = deps.find_labels(plan["from_name"], plan["match"], None) or []
    fresh = {
        (r["session_id"], r["speaker_key"]) for r in current
        if _matches(r.get("name") or "", plan["from_name"], plan["match"])
    } - pinned
    if fresh:
        raise RelabelRefused(
            f'{len(fresh)} new speaker label(s) named "{plan["from_name"]}" '
            f'appeared since this plan was made, and a library-wide rename '
            f'would change them too. Nothing was changed. Run '
            f'plan_speaker_relabel again and show the user the fresh plan.'
        )


def _resolve_target_profile(plan: dict, to_name: str, deps: RelabelDeps) -> str:
    """Voice profile the labels should end up on, resolved at apply time.

    A profile with the target name can appear during the confirmation window
    (a background auto-link creates one), so look it up again rather than
    trusting the id the plan was built with and creating a duplicate.
    """
    current = deps.find_profile_by_name(to_name)
    if current:
        return current["id"]
    return plan["to_profile_id"] or deps.create_profile(to_name)


def apply_plan(
    token: str,
    current_request_id: "str | None" = None,
    confirmed_by: str = "chat",
    *,
    deps: RelabelDeps,
) -> dict:
    """Apply the plan pinned under ``token``.

    ``current_request_id`` is the chat turn asking to apply. When it equals the
    turn that minted the plan the apply is refused: a plan can never be applied
    in the same turn that produced it, which is what makes the confirmation
    structural rather than a promise from the model. The UI path passes None,
    because there the user clicked the button themselves.
    """
    entry = _TOKENS.peek(token)
    if entry is None:
        raise RelabelRefused(
            "That confirmation token is unknown, already used, or expired "
            "(plans last 10 minutes). Call plan_speaker_relabel again and show "
            "the user the fresh plan."
        )
    if current_request_id is not None and entry["request_id"] == current_request_id:
        raise RelabelRefused(
            "This plan was created in the current turn, so it cannot be applied "
            "yet. Present the plan to the user, wait for their explicit "
            "confirmation, then call apply_speaker_relabel with this token in "
            "your next turn."
        )

    entry = _TOKENS.consume(token)
    if entry is None:
        raise RelabelRefused(
            "That confirmation token was already used. Call "
            "plan_speaker_relabel again if the user wants to redo this."
        )

    plan = entry["plan"]
    if not plan["sessions"] and not plan.get("profile_only"):
        raise RelabelRefused(
            f'Nothing to apply: the plan matched no speaker named '
            f'"{plan["from_name"]}".'
        )

    strategy = plan["strategy"]
    from_name = plan["from_name"]
    to_name = plan["to_name"]

    if strategy == "session_patch":
        # One profile for the whole fan-out. Each patched session hands its
        # rename to a background worker that would otherwise look the profile
        # up and create it, and two workers missing the lookup at the same
        # moment produce two profiles with the same name.
        if deps.library_ready() and not plan.get("target_is_me"):
            _resolve_target_profile(plan, to_name, deps)
        # The token is already spent, so a failure part way through is not
        # resumable. Say exactly how far it got instead of leaving the user to
        # guess which meetings changed.
        applied_ids: list[str] = []
        for index, sess in enumerate(plan["sessions"]):
            try:
                deps.patch_session(
                    sess["session_id"],
                    [k["speaker_key"] for k in sess["keys"]],
                    to_name,
                )
            except Exception as e:
                failure = RelabelRefused(
                    f"Applied {index} of {len(plan['sessions'])} meetings, then "
                    f"failed on {sess['title']}: {e}; the remaining meetings "
                    f"were not changed."
                )
                failure.applied_session_ids = applied_ids
                raise failure
            applied_ids.append(sess["session_id"])
    elif strategy == "bulk_link":
        _reverify_coverage(plan, deps)
        to_gid = _resolve_target_profile(plan, to_name, deps)
        deps.bulk_link(from_name, to_gid)
    elif strategy == "merge_profiles":
        # The merge moves embeddings and renames labels linked to the old
        # profile. The bulk link afterwards catches matching labels that were
        # never linked to any profile, which is the common case.
        _reverify_coverage(plan, deps)
        to_gid = _resolve_target_profile(plan, to_name, deps)
        # If the two ids collided during the confirmation window (the source
        # profile was renamed to the target name), there is nothing to merge.
        if to_gid != plan["from_profile_id"]:
            deps.merge_profiles(to_gid, plan["from_profile_id"])
        deps.bulk_link(from_name, to_gid)
    else:
        raise RelabelRefused(f"Unknown relabel strategy: {strategy}")

    session_ids = [s["session_id"] for s in plan["sessions"]]
    try:
        summaries_queued = int(
            deps.queue_summaries(session_ids, from_name, to_name) or 0
        )
    except Exception:
        summaries_queued = 0

    return {
        "applied": True,
        # The plan card in the chat widget is keyed by token, so the result has
        # to name the plan it just applied.
        "token": token,
        "strategy": strategy,
        "confirmed_by": confirmed_by,
        "from_name": from_name,
        "to_name": to_name,
        "sessions": [
            {
                "session_id": s["session_id"],
                "title": s["title"],
                "key_count": len(s["keys"]),
                "segment_count": s["segment_count"],
            }
            for s in plan["sessions"]
        ],
        "session_count": len(plan["sessions"]),
        "key_count": plan["key_count"],
        "segment_total": plan["segment_total"],
        "summaries_queued": summaries_queued,
    }


def plan_card(plan: dict, token: "str | None") -> dict:
    """Compact form of a plan for the chat tool widget."""
    return {
        "token": token,
        "from_name": plan["from_name"],
        "to_name": plan["to_name"],
        "scope": plan["scope"],
        "match": plan["match"],
        "strategy": plan["strategy"],
        "summary": plan["summary"],
        "session_count": plan["session_count"],
        "key_count": plan["key_count"],
        "segment_total": plan["segment_total"],
        "linked_via_profile_count": plan.get("linked_via_profile_count") or 0,
        "warnings": plan["warnings"],
        "sessions": [
            {
                "session_id": s["session_id"],
                "title": s["title"],
                "started_at": s["started_at"],
                "key_count": len(s["keys"]),
                "segment_count": s["segment_count"],
            }
            for s in plan["sessions"]
        ],
    }
