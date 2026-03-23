"""SQLite persistence for meeting sessions, transcripts, summaries, and chat."""
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "meetings.db"


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 3000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS transcript_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                text TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'loopback',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS speaker_labels (
                session_id TEXT NOT NULL,
                speaker_key TEXT NOT NULL,
                name TEXT NOT NULL,
                color TEXT,
                PRIMARY KEY (session_id, speaker_key)
            );
        """)
        # Live migrations: add columns / tables to databases created before these versions
        for migration in [
            "ALTER TABLE transcript_segments ADD COLUMN source TEXT NOT NULL DEFAULT 'loopback'",
            "ALTER TABLE transcript_segments ADD COLUMN start_time REAL NOT NULL DEFAULT 0",
            "ALTER TABLE transcript_segments ADD COLUMN end_time REAL NOT NULL DEFAULT 0",
            "ALTER TABLE transcript_segments ADD COLUMN label_override TEXT DEFAULT NULL",
            "ALTER TABLE transcript_segments ADD COLUMN source_override TEXT DEFAULT NULL",
            "ALTER TABLE speaker_labels ADD COLUMN color TEXT",
            "ALTER TABLE speaker_labels ADD COLUMN global_id TEXT DEFAULT NULL",
            # Global cross-session speaker identity tables
            """CREATE TABLE IF NOT EXISTS global_speakers (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                color       TEXT,
                centroid    BLOB,
                emb_count   INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS speaker_embeddings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                global_id    TEXT NOT NULL REFERENCES global_speakers(id) ON DELETE CASCADE,
                session_id   TEXT NOT NULL,
                speaker_key  TEXT NOT NULL,
                embedding    BLOB NOT NULL,
                duration_sec REAL NOT NULL,
                created_at   TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_emb_global  ON speaker_embeddings(global_id)",
            "CREATE INDEX IF NOT EXISTS idx_emb_session ON speaker_embeddings(session_id, speaker_key)",
            # Session folders
            """CREATE TABLE IF NOT EXISTS folders (
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "ALTER TABLE sessions ADD COLUMN folder_id TEXT DEFAULT NULL",
            "ALTER TABLE sessions ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE folders ADD COLUMN parent_id TEXT DEFAULT NULL",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass  # already exists


def _now() -> str:
    return datetime.utcnow().isoformat()


# ── Sessions ──────────────────────────────────────────────────────────────────

def create_session(title: str | None = None) -> str:
    sid = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
            (sid, title or f"Meeting {now[:16].replace('T', ' ')}", now),
        )
    return sid


def end_session(session_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (_now(), session_id),
        )


def resume_session(session_id: str) -> None:
    """Clear ended_at so a session can be appended to."""
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET ended_at = NULL WHERE id = ?",
            (session_id,),
        )


def update_session_title(session_id: str, title: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (title, session_id),
        )


def delete_session(session_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM transcript_segments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM speaker_labels WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    # Clean up WAV file if it exists
    wav_path = DB_PATH.parent / "audio" / f"{session_id}.wav"
    if wav_path.exists():
        try:
            wav_path.unlink()
        except OSError:
            pass


def list_sessions() -> list[dict]:
    audio_dir = DB_PATH.parent / "audio"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, title, started_at, ended_at, folder_id, sort_order"
            " FROM sessions ORDER BY started_at DESC"
        ).fetchall()
    return [
        {**dict(r), "has_audio": (audio_dir / f"{r['id']}.wav").exists()}
        for r in rows
    ]


# ── Folders ───────────────────────────────────────────────────────────────────

def list_folders() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, name, sort_order, parent_id, created_at"
            " FROM folders ORDER BY sort_order ASC, created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def create_folder(name: str, parent_id: str | None = None) -> str:
    fid = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM folders WHERE parent_id IS ?",
            (parent_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO folders (id, name, sort_order, parent_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (fid, name.strip(), max_order + 1, parent_id, now, now),
        )
    return fid


def rename_folder(folder_id: str, name: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE folders SET name=?, updated_at=? WHERE id=?",
            (name.strip(), _now(), folder_id),
        )


def delete_folder(folder_id: str, delete_contents: bool = False) -> list[str]:
    """Delete a folder.

    If *delete_contents* is True, recursively delete all child folders and
    their sessions (including WAV files).  Returns the list of deleted
    session IDs so the caller can clear active-session state if needed.

    If False, sessions are uncategorized and child folders are reparented.
    """
    deleted_session_ids: list[str] = []
    with _conn() as conn:
        if delete_contents:
            # Collect all folder IDs to delete (recursive)
            all_folder_ids = []
            stack = [folder_id]
            while stack:
                fid = stack.pop()
                all_folder_ids.append(fid)
                children = conn.execute(
                    "SELECT id FROM folders WHERE parent_id=?", (fid,)
                ).fetchall()
                stack.extend(r["id"] for r in children)

            # Collect and delete sessions in all those folders
            placeholders = ",".join("?" * len(all_folder_ids))
            rows = conn.execute(
                f"SELECT id FROM sessions WHERE folder_id IN ({placeholders})",
                all_folder_ids,
            ).fetchall()
            deleted_session_ids = [r["id"] for r in rows]

            for sid in deleted_session_ids:
                conn.execute("DELETE FROM transcript_segments WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM summaries WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM chat_messages WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM speaker_labels WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id=?", (sid,))

            # Delete all collected folders
            conn.execute(
                f"DELETE FROM folders WHERE id IN ({placeholders})",
                all_folder_ids,
            )
        else:
            parent = conn.execute(
                "SELECT parent_id FROM folders WHERE id=?", (folder_id,)
            ).fetchone()
            parent_id = parent["parent_id"] if parent else None
            conn.execute(
                "UPDATE folders SET parent_id=? WHERE parent_id=?",
                (parent_id, folder_id),
            )
            conn.execute("UPDATE sessions SET folder_id=NULL WHERE folder_id=?", (folder_id,))
            conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))

    # Clean up WAV files outside the transaction
    audio_dir = DB_PATH.parent / "audio"
    for sid in deleted_session_ids:
        wav_path = audio_dir / f"{sid}.wav"
        if wav_path.exists():
            try:
                wav_path.unlink()
            except OSError:
                pass

    return deleted_session_ids


def set_session_folder(session_id: str, folder_id: str | None) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE sessions SET folder_id=? WHERE id=?",
            (folder_id, session_id),
        )


def bulk_set_folder(session_ids: list[str], folder_id: str | None) -> None:
    if not session_ids:
        return
    with _conn() as conn:
        # Assign to folder with sort_order at the end
        max_order = 0
        if folder_id:
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM sessions WHERE folder_id=?",
                (folder_id,),
            ).fetchone()[0]
        conn.executemany(
            "UPDATE sessions SET folder_id=?, sort_order=? WHERE id=?",
            [(folder_id, max_order + 1 + i, sid) for i, sid in enumerate(session_ids)],
        )


def bulk_reorder(folders: list[dict] | None = None,
                 sessions: list[dict] | None = None) -> None:
    """Batch-update sort_order (and parent_id/folder_id) for folders and sessions.

    folders:  list of {id, sort_order, parent_id}
    sessions: list of {id, sort_order, folder_id}
    """
    with _conn() as conn:
        if folders:
            conn.executemany(
                "UPDATE folders SET sort_order=?, parent_id=?, updated_at=? WHERE id=?",
                [(f["sort_order"], f.get("parent_id"), _now(), f["id"]) for f in folders],
            )
        if sessions:
            conn.executemany(
                "UPDATE sessions SET sort_order=?, folder_id=? WHERE id=?",
                [(s["sort_order"], s.get("folder_id"), s["id"]) for s in sessions],
            )


def reset_session_transcript(session_id: str) -> None:
    """Delete all transcript, summary, chat, and speaker data for a session.
    The sessions row (title, timestamps) is preserved.
    """
    with _conn() as conn:
        conn.execute("DELETE FROM transcript_segments WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM speaker_labels WHERE session_id = ?", (session_id,))


def get_session(session_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, title, started_at, ended_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None

        segments = conn.execute(
            "SELECT id, text, source, start_time, end_time, label_override, source_override "
            "FROM transcript_segments WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

        summary_row = conn.execute(
            "SELECT content FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()

        messages = conn.execute(
            "SELECT role, content, created_at FROM chat_messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

        speaker_labels = conn.execute(
            "SELECT speaker_key, name, color FROM speaker_labels WHERE session_id = ?",
            (session_id,),
        ).fetchall()

    return {
        **dict(row),
        "segments": [
            {"id": r["id"], "text": r["text"], "source": r["source"],
             "start_time": r["start_time"], "end_time": r["end_time"],
             "label_override": r["label_override"],
             "source_override": r["source_override"]}
            for r in segments
        ],
        "summary": summary_row["content"] if summary_row else "",
        "chat_messages": [dict(m) for m in messages],
        "speaker_labels": {r["speaker_key"]: r["name"] for r in speaker_labels},
        "speaker_profiles": [
            {"speaker_key": r["speaker_key"], "name": r["name"], "color": r["color"]}
            for r in speaker_labels
        ],
    }


# ── Transcript ────────────────────────────────────────────────────────────────

def get_segment(segment_id: int) -> dict | None:
    """Retrieve a single transcript segment by ID."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, session_id, text, source, start_time, end_time, label_override, source_override "
            "FROM transcript_segments WHERE id = ?",
            (segment_id,),
        ).fetchone()
    return dict(row) if row else None


def get_segments_by_speaker(session_id: str, speaker_key: str) -> list[dict]:
    """Return all segments for a given speaker_key in a session, with timing info."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, start_time, end_time FROM transcript_segments "
            "WHERE session_id = ? AND source = ? ORDER BY id",
            (session_id, speaker_key),
        ).fetchall()
    return [dict(r) for r in rows]


def save_segment(
    session_id: str,
    text: str,
    source: str = "loopback",
    start_time: float = 0.0,
    end_time: float = 0.0,
) -> int:
    """Save a transcript segment. Returns the DB row id."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO transcript_segments "
            "(session_id, text, source, start_time, end_time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, text, source, start_time, end_time, _now()),
        )
        return cur.lastrowid


# ── Summary ───────────────────────────────────────────────────────────────────

def save_summary(session_id: str, content: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO summaries (session_id, content, created_at) VALUES (?, ?, ?)",
            (session_id, content, _now()),
        )


# ── Chat ──────────────────────────────────────────────────────────────────────

def update_segment(segment_id: int, text: str, end_time: float) -> None:
    """Update an existing segment's text and end_time (used for merging)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET text = ?, end_time = ? WHERE id = ?",
            (text, end_time, segment_id),
        )


def update_segment_source(segment_id: int, source: str) -> None:
    """Update a segment's source/speaker label."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET source = ? WHERE id = ?",
            (source, segment_id),
        )


def save_segment_label_override(segment_id: int, label: str | None) -> None:
    """Set or clear a per-segment label override."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET label_override = ? WHERE id = ?",
            (label, segment_id),
        )


def save_segment_source_override(segment_id: int, source_override: str | None) -> None:
    """Set or clear a per-segment speaker-key reassignment."""
    with _conn() as conn:
        conn.execute(
            "UPDATE transcript_segments SET source_override = ? WHERE id = ?",
            (source_override, segment_id),
        )


def get_speaker_profile(session_id: str, speaker_key: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT speaker_key, name, color FROM speaker_labels WHERE session_id = ? AND speaker_key = ?",
            (session_id, speaker_key),
        ).fetchone()
    return dict(row) if row else None


def list_speaker_profiles(session_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT speaker_key, name, color FROM speaker_labels "
            "WHERE session_id = ? ORDER BY lower(name), speaker_key",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_speaker_label(
    session_id: str,
    speaker_key: str,
    name: str | None = None,
    color: str | None = None,
) -> dict:
    existing = get_speaker_profile(session_id, speaker_key) or {}
    final_name = (name or existing.get("name") or speaker_key).strip()
    final_color = (color.strip() if isinstance(color, str) else existing.get("color"))
    with _conn() as conn:
        conn.execute(
            "INSERT INTO speaker_labels (session_id, speaker_key, name, color) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id, speaker_key) DO UPDATE SET name=excluded.name, color=excluded.color",
            (session_id, speaker_key, final_name, final_color),
        )
    return {
        "speaker_key": speaker_key,
        "name": final_name,
        "color": final_color,
    }


def save_chat_message(session_id: str, role: str, content: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, _now()),
        )
