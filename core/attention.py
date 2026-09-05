"""Product definition for recordings that need speaker attention."""
import re

from core import settings


# Under-counts are only flagged when the expected count is this small or less.
UNDERCOUNT_MAX_EXPECTED = 6

GENERIC_NAME_RE = re.compile(
    r"^(speaker\s*\d+|other participant(\s*\d+)?|unknown|unidentified|guest|participant\s*\d+)$",
    re.IGNORECASE,
)


def is_generic_speaker_name(name) -> bool:
    """Return whether a display name is empty or a known placeholder."""
    return not str(name or "").strip() or bool(GENERIC_NAME_RE.fullmatch(str(name).strip()))


def get_attention_thresholds() -> tuple[float, int]:
    """Load the shared material-content thresholds from preferences."""
    preferences = settings.load()
    try:
        seconds = float(preferences.get("obsidian_gate_min_seconds") or 15)
    except (TypeError, ValueError):
        seconds = 15.0
    try:
        words = int(preferences.get("obsidian_gate_min_words") or 25)
    except (TypeError, ValueError):
        words = 25
    return (seconds, words)


def compute_attention(
    speakers: list[dict],
    expected_speaker_count,
    *,
    thresholds: tuple[float, int] | None = None,
) -> dict:
    """Compute the canonical needs-attention state for one recording.

    A non-noise speaker has material content when talk time is at least the
    configured ``obsidian_gate_min_seconds`` threshold, or word count is at
    least ``obsidian_gate_min_words``. The defaults are 15 seconds and 25
    words. Material speakers with generic names are unresolved. Generic
    speakers below both thresholds are diarizer phantoms, recorded as
    ``below_threshold`` but do not flag the recording. ``found`` counts every
    distinct material, non-noise effective speaker key represented by the
    input list. A positive expected count flags a mismatch when ``found``
    exceeds it (over-split voices), or falls short of it while the expected
    count is at most ``UNDERCOUNT_MAX_EXPECTED`` (likely under-diarization); a
    large invite where fewer people spoke is not flagged. A recording needs
    attention for any unresolved material speaker or expected-count mismatch.
    """
    min_seconds, min_words = thresholds or get_attention_thresholds()
    unresolved = 0
    below_threshold = 0
    found = 0

    for speaker in speakers or []:
        if speaker.get("is_noise"):
            continue
        material = (
            float(speaker.get("talk_seconds") or 0) >= min_seconds
            or int(speaker.get("word_count") or 0) >= min_words
        )
        generic = is_generic_speaker_name(speaker.get("name"))
        if material:
            found += 1
            if generic:
                unresolved += 1
        elif generic:
            below_threshold += 1

    expected = (
        expected_speaker_count
        if isinstance(expected_speaker_count, int) and not isinstance(expected_speaker_count, bool)
        else None
    )
    # An over-count (more voices than people) always needs a merge. An
    # under-count only means under-diarization when the invite was small; a big
    # invite where fewer people spoke is normal and nothing can be done about it.
    mismatch = (
        expected is not None
        and expected > 0
        and (found > expected or (found < expected and expected <= UNDERCOUNT_MAX_EXPECTED))
    )
    reasons = []
    if unresolved:
        reasons.append("unresolved_speakers")
    if mismatch:
        reasons.append("speaker_count_mismatch")
    return {
        "needs": bool(reasons),
        "reasons": reasons,
        "unresolved": unresolved,
        "below_threshold": below_threshold,
        "found": found,
        "expected": expected,
    }
