"""Crash safety for reanalysis, which deletes a transcript before it has one.

``_run_reanalysis`` clears the session's segments and speaker rows and then
rebuilds them incrementally from the audio. Anything that interrupts the
rebuild used to leave the meeting empty (killed during diarization) or
truncated (killed during transcription), with no way back: the delete was a
hard DELETE with no backup, and the reanalysis worker is a daemon thread that
dies instantly on ``os._exit``.

The guard is a file next to the meeting's other backups. It is written before
the delete and removed once the rebuild reports success, so its presence means
exactly one thing: a pass started and did not finish. Startup sweeps for
leftovers and rolls them back, which is what makes a hard kill survivable.

Deliberately separate from the trim/split snapshot (``session-original.json``):
that one backs a user-visible "restore original" affordance and must not be
consumed, overwritten or deleted by a reanalysis.
"""
import json
from pathlib import Path

from core import log as log
from core import paths


SNAPSHOT_NAME = "reanalysis-in-progress.json"


def snapshot_path(session_id: str) -> Path:
    return paths.backup_dir() / session_id / SNAPSHOT_NAME


def begin(session_id: str, snapshot: dict) -> bool:
    """Record the transcript about to be deleted. Returns whether it was saved.

    A failure here is reported and does not stop the reanalysis: refusing to
    run because a backup could not be written would be a worse outcome than the
    unprotected pass the user asked for. It is logged so the choice is visible.
    """
    try:
        target = snapshot_path(session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(snapshot), encoding="utf-8")
        return True
    except Exception as exc:  # noqa: BLE001 - never block the pass on the guard
        log.warn("reanalysis", f"Could not save the rollback snapshot: {exc}")
        return False


def load(session_id: str) -> dict | None:
    """The snapshot for an unfinished pass, or None when there is none."""
    path = snapshot_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a corrupt guard must not wedge startup
        log.warn("reanalysis", f"Rollback snapshot for {session_id[:8]} unreadable: {exc}")
        return None


def clear(session_id: str) -> None:
    """Drop the guard: the pass finished, or its snapshot has been restored."""
    try:
        snapshot_path(session_id).unlink(missing_ok=True)
    except OSError as exc:
        log.warn("reanalysis", f"Could not clear the rollback snapshot: {exc}")


def pending_session_ids() -> list[str]:
    """Sessions whose last reanalysis started and never reported success."""
    try:
        root = paths.backup_dir()
    except Exception:  # noqa: BLE001 - no data dir yet is not an error here
        return []
    found = []
    for entry in root.glob(f"*/{SNAPSHOT_NAME}"):
        if entry.parent.name:
            found.append(entry.parent.name)
    return found
