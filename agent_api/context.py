"""Dependency-injection context for the Agent API.

app.py owns live state (recording status, loaded models, SSE push, the AI
client) and a set of battle-tested helpers (scope filters, session
describers). Rather than importing app.py from here (circular) or duplicating
that logic, app.py hands the blueprint an ``AgentContext`` of callables at
registration time. Everything storage/paths/settings-shaped is imported
directly by the agent modules; only app-owned behaviour goes through here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AgentContext:
    # Live app state
    status_payload: Callable[[], dict]
    """Snapshot of recording/model state (same payload as /api/status)."""

    live_extras: Callable[[], dict]
    """Extra live-only readings: audio levels, screen recorder state."""

    live_media: Callable[[], dict]
    """Live-recording media state: {recording, session_id, live_video_path,
    elapsed_sec}. live_video_path is the in-progress fragmented MP4 the screen
    recorder is writing (None when idle or screen recording is off)."""

    # Search / browse plumbing shared with Global Chat tools
    scope_filters: Callable[[dict], dict]
    """app._scope_filters: resolve folder/date/speaker args to a filter set."""

    scoped_session_ids: Callable[[dict], "list[str] | None"]
    """app._scoped_session_ids: filter set -> in-scope session ids (None = all)."""

    folder_labels: Callable[..., dict]
    """app._folder_labels: folder id -> folder tree entry."""

    describe_session: Callable[..., dict]
    """app._describe_session(meta, labels, *, summary_chars) -> result entry."""

    source_labels: dict
    """Raw capture-source key -> display label ('loopback' -> 'Desktop', ...)."""

    # Models / AI
    model_snapshot: Callable[[], dict]
    """Whisper preset, diarizer state, CUDA availability."""

    ai_snapshot: Callable[[], dict]
    """Active AI provider/model plus per-tool overrides."""

    apply_ai_settings: Callable[[Any, Any], dict]
    """Apply provider/model changes through the same path as the settings UI
    (persists, reloads the client). Returns the resulting selection."""

    # Voice library
    list_global_speakers: Callable[[], list]
    get_profile_sessions: Callable[[str], list]

    # System
    changelog: Callable[[int], list]
    """Newest CHANGELOG.md entries first: id, date, title, body (markdown), category."""

    stop_recording: Callable[[], Any]
    """The /api/recording/stop view function (called in-request-context)."""

    push_status: Callable[[], None]
    """Broadcast a status SSE event so open UI tabs refresh."""

    push_event: Callable[[str, dict], None]
    """app._push: broadcast an arbitrary SSE event to connected UI tabs."""

    server_url: str = "http://localhost:6969"
    app_started_at: float = 0.0
    extra: dict = field(default_factory=dict)

    # ── Speakers and organisation (optional: a context without them still
    #    mounts the read-only API; the routes that need one answer 501) ──────
    voice_library: Any = None
    """app's SpeakerFingerprintDB (ml/speaker_db.py). ``ready`` says whether
    the embedding model is loaded; its SQL-only methods work regardless."""

    label_speaker: "Callable[..., list] | None" = None
    """(session_id, speaker_keys, name, color, global_id, train) -> updated
    speaker dicts. app._patch_session_speakers: the UI's own rename path
    (live merge detection, SSE, summary refresh, voice-profile link)."""

    apply_speaker_corrections: "Callable[[str, list, list], dict] | None" = None
    """(session_id, clusters, noise_keys) -> result. The Cleanup tab's Save:
    relink, unlink back to 'Speaker N', or flag as noise."""

    relabel_segment: "Callable[..., Any] | None" = None
    """(segment_id, label, source_override, train=bool) -> segment row.
    Pins one transcript line to a speaker, as clicking it in the UI does."""

    rename_profile: "Callable[..., dict] | None" = None
    """(global_id, name=..., color=...) -> {name, color}. Renames a voice
    profile and every label linked to it."""

    merge_profiles: "Callable[[str, str], dict] | None" = None
    """(keep_id, merge_id) -> {name, color}. app._apply_profile_merge."""

    relabel_deps: "Callable[[], Any] | None" = None
    """() -> ai.speaker_relabel.RelabelDeps wired to the real storage and
    voice library, for the plan / apply bulk relabel pair."""

    me_profile_id: "Callable[[], Any] | None" = None
    """() -> the owner's own voice-profile id, or None."""
