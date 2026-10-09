"""The voice library's side of the evidence.

Asked of the library object the app owns (duck-typed, so the evaluation
harness and the tests pass their own), with the Agent API's evidence helpers
so a key's verdict here is the one the review pack and the live pipeline
would reach:

- per key: its mean voice and the library's best profile for it;
- per turn, only for keys that look like two people: one vector each, so a
  split can follow the voices.
"""
from __future__ import annotations

from collections import defaultdict

from agent_api import speakers as evidence


def key_voices(library, session_id: str, keys: list[str], labels: dict, segments: list[dict], *,
               wav_path=None, exclude: set[str] | None = None) -> tuple[dict, dict]:
    """``({key: {name, global_id, verdict, similarity}}, {key: centroid})``.

    Stored embeddings first; with ``wav_path`` a key that has none gets one
    extracted from its longest lines and kept, as the Cleanup tab does.
    ``exclude`` holds profiles never to match (the owner's)."""
    matches: dict = {}
    cents: dict = {}
    if library is None or not getattr(library, "ready", False):
        return matches, cents
    lines: dict[str, list[dict]] = defaultdict(list)
    for s in segments:
        lines[s["key"]].append(s)
    for k in keys:
        gid = (labels.get(k) or {}).get("global_id")
        cent, _info = evidence.speaker_centroid(library, session_id, k, gid, lines.get(k, []),
                                                wav_path, allow_backfill=wav_path is not None)
        if cent is None:
            continue
        cents[k] = cent
        m = evidence.library_matches(library, cent, exclude=set(exclude or ()))
        best = m.get("best")
        if best:
            matches[k] = {"name": best["name"], "global_id": best["global_id"],
                          "verdict": m["verdict"], "similarity": best["similarity"]}
    return matches, cents


def turn_vectors(library, wav_path, spans: list[tuple[int, float, float]]) -> dict:
    """One voice vector per ``(turn idx, start, end)`` long enough to carry one."""
    if library is None or wav_path is None or not spans:
        return {}
    return library.embed_spans(str(wav_path), spans)
