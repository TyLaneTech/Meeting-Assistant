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
            "ALTER TABLE chat_messages ADD COLUMN attachments TEXT DEFAULT NULL",
            "ALTER TABLE chat_messages ADD COLUMN tool_calls TEXT DEFAULT NULL",
            # Full-text search on session titles and transcript segments
            """CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                session_id UNINDEXED,
                kind UNINDEXED,
                text,
                tokenize='porter unicode61'
            )""",
            # v2: recreate FTS with source_id column for segment-level linking
            "DROP TABLE IF EXISTS search_fts",
            """CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
                session_id UNINDEXED,
                source_id UNINDEXED,
                kind UNINDEXED,
                text,
                tokenize='porter unicode61'
            )""",
            # Semantic search embeddings per session
            """CREATE TABLE IF NOT EXISTS session_embeddings (
                session_id TEXT PRIMARY KEY,
                embedding  BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            # Global chat (cross-session AI conversations)
            """CREATE TABLE IF NOT EXISTS global_chat_conversations (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT 'New Chat',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS global_chat_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES global_chat_conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                attachments     TEXT DEFAULT NULL,
                tool_calls      TEXT DEFAULT NULL
            )""",
        ]:
            try:
                conn.execute(migration)
            except Exception:
                pass  # already exists

        # Populate FTS index if it's empty (first migration or rebuild)
        fts_count = conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
        if fts_count == 0:
            _rebuild_fts(conn)


def _rebuild_fts(conn) -> None:
    """Populate the FTS index from existing sessions and transcript segments."""
    conn.execute("DELETE FROM search_fts")
    # Index session titles
    conn.execute(
        "INSERT INTO search_fts (session_id, source_id, kind, text) "
        "SELECT id, NULL, 'title', title FROM sessions WHERE title IS NOT NULL AND title != ''"
    )
    # Index transcript segments (source_id = segment rowid)
    conn.execute(
        "INSERT INTO search_fts (session_id, source_id, kind, text) "
        "SELECT session_id, id, 'segment', text FROM transcript_segments WHERE text IS NOT NULL AND text != ''"
    )


def fts_index_session_title(session_id: str, title: str) -> None:
    """Add or update a session title in the FTS index."""
    with _conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE session_id = ? AND kind = 'title'",
                     (session_id,))
        if title and title.strip():
            conn.execute("INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, NULL, 'title', ?)",
                         (session_id, title))


def fts_index_segment(session_id: str, text: str, segment_id: int | None = None) -> None:
    """Add a transcript segment to the FTS index."""
    if not text or not text.strip():
        return
    with _conn() as conn:
        conn.execute("INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, ?, 'segment', ?)",
                     (session_id, segment_id, text))


def fts_remove_session(session_id: str) -> None:
    """Remove all FTS entries for a session."""
    with _conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE session_id = ?", (session_id,))


def search_sessions(query: str, limit: int = 50) -> list[dict]:
    """Search sessions by title and transcript content using FTS5.

    Returns a list of {session_id, title, matches: [{kind, snippet}]} dicts,
    ordered by relevance.
    """
    if not query or not query.strip():
        return []
    # Escape FTS5 special characters and build a prefix query
    terms = query.strip().split()
    fts_query = " ".join(f'"{t}"*' for t in terms if t)
    if not fts_query:
        return []

    with _conn() as conn:
        rows = conn.execute(
            "SELECT f.session_id, f.source_id, f.kind,"
            "       snippet(search_fts, 3, '<mark>', '</mark>', '…', 40) AS snippet,"
            "       rank"
            " FROM search_fts f"
            " WHERE search_fts MATCH ?"
            " ORDER BY rank"
            " LIMIT ?",
            (fts_query, limit * 3),  # over-fetch so we can group
        ).fetchall()

        # Group by session, collect match snippets
        from collections import OrderedDict
        sessions: OrderedDict[str, dict] = OrderedDict()
        for r in rows:
            sid = r["session_id"]
            if sid not in sessions:
                # Look up session title
                title_row = conn.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
                sessions[sid] = {
                    "session_id": sid,
                    "title": title_row["title"] if title_row else "",
                    "matches": [],
                    "best_rank": r["rank"],
                }
            if len(sessions[sid]["matches"]) < 3:  # max 3 snippets per session
                match = {
                    "kind": r["kind"],
                    "snippet": r["snippet"],
                }
                if r["source_id"] is not None:
                    match["segment_id"] = r["source_id"]
                sessions[sid]["matches"].append(match)

    results = list(sessions.values())[:limit]
    return results


def search_speakers(query: str, limit: int = 20) -> list[dict]:
    """Search speaker_labels by name. Returns sessions grouped by speaker match,
    each with a 'participant' kind match entry."""
    if not query or not query.strip():
        return []
    pattern = f"%{query.strip()}%"
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT sl.session_id, sl.name AS speaker_name,
                   s.title, s.started_at
            FROM speaker_labels sl
            JOIN sessions s ON s.id = sl.session_id
            WHERE sl.name LIKE ? COLLATE NOCASE
              AND sl.name != ''
            GROUP BY sl.session_id, lower(sl.name)
            ORDER BY s.started_at DESC
            LIMIT ?
            """,
            (pattern, limit * 3),
        ).fetchall()

    from collections import OrderedDict
    sessions: OrderedDict[str, dict] = OrderedDict()
    for r in rows:
        sid = r["session_id"]
        name = r["speaker_name"]
        # Highlight the matching portion in the speaker name
        idx = name.lower().find(query.strip().lower())
        if idx >= 0:
            snippet = (name[:idx] + "<mark>" + name[idx:idx+len(query.strip())]
                       + "</mark>" + name[idx+len(query.strip()):])
        else:
            snippet = name
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "title": r["title"] or "",
                "matches": [],
            }
        if len(sessions[sid]["matches"]) < 3:
            sessions[sid]["matches"].append({
                "kind": "participant",
                "snippet": snippet,
            })

    return list(sessions.values())[:limit]


# ── Semantic embeddings ──────────────────────────────────────────────────────

def save_session_embedding(session_id: str, embedding_bytes: bytes) -> None:
    """Store (or update) the semantic embedding for a session."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO session_embeddings (session_id, embedding, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET embedding=excluded.embedding, updated_at=excluded.updated_at",
            (session_id, embedding_bytes, datetime.utcnow().isoformat()),
        )


def get_all_session_embeddings() -> list[dict]:
    """Return all session embeddings: [{session_id, title, embedding_bytes}]."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT e.session_id, s.title, e.embedding "
            "FROM session_embeddings e "
            "JOIN sessions s ON s.id = e.session_id"
        ).fetchall()
    return [{"session_id": r["session_id"], "title": r["title"],
             "embedding_bytes": bytes(r["embedding"])} for r in rows]


def get_session_text_for_embedding(session_id: str) -> str | None:
    """Get concatenated title + transcript text for computing an embedding."""
    with _conn() as conn:
        session = conn.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not session:
            return None
        segments = conn.execute(
            "SELECT text FROM transcript_segments WHERE session_id = ? ORDER BY start_time",
            (session_id,),
        ).fetchall()
    title = session["title"] or ""
    transcript = " ".join(r["text"] for r in segments if r["text"])
    if not transcript and not title:
        return None
    return f"{title}. {transcript}" if transcript else title


def delete_session_embedding(session_id: str) -> None:
    """Remove the embedding for a session."""
    with _conn() as conn:
        conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (session_id,))


def get_unembedded_session_ids() -> list[str]:
    """Return session IDs that have transcript segments but no embedding yet."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT s.id FROM sessions s "
            "JOIN transcript_segments ts ON ts.session_id = s.id "
            "WHERE s.id NOT IN (SELECT session_id FROM session_embeddings)"
        ).fetchall()
    return [r["id"] for r in rows]


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
    fts_index_session_title(session_id, title)


def delete_session(session_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM search_fts WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_embeddings WHERE session_id = ?", (session_id,))
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
            "SELECT s.id, s.title, s.started_at, s.ended_at,"
            "       s.folder_id, s.sort_order,"
            "       (SELECT MAX(ts.end_time) FROM transcript_segments ts"
            "        WHERE ts.session_id = s.id) AS last_segment_time"
            " FROM sessions s ORDER BY s.started_at DESC"
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
            "SELECT role, content, created_at, attachments, tool_calls FROM chat_messages WHERE session_id = ? ORDER BY id",
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
        seg_id = cur.lastrowid
        # Index in FTS for cross-session search
        if text and text.strip():
            try:
                conn.execute(
                    "INSERT INTO search_fts (session_id, source_id, kind, text) VALUES (?, ?, 'segment', ?)",
                    (session_id, seg_id, text),
                )
            except Exception:
                pass
        return seg_id


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


def clear_chat_messages(session_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))


def save_chat_message(session_id: str, role: str, content: str,
                      attachments: str | None = None,
                      tool_calls: str | None = None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages (session_id, role, content, created_at, attachments, tool_calls)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, content, _now(), attachments, tool_calls),
        )


# ── Global Chat Conversations ───────────────────────────────────────────────

def create_global_conversation(title: str = "New Chat") -> str:
    cid = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO global_chat_conversations (id, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (cid, title, now, now),
        )
    return cid


def list_global_conversations() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT c.id, c.title, c.created_at, c.updated_at,"
            "       (SELECT COUNT(*) FROM global_chat_messages m"
            "        WHERE m.conversation_id = c.id) AS message_count"
            " FROM global_chat_conversations c"
            " ORDER BY c.updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_global_conversation(conversation_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, title, created_at, updated_at"
            " FROM global_chat_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            return None
        msgs = conn.execute(
            "SELECT id, role, content, created_at, attachments, tool_calls"
            " FROM global_chat_messages"
            " WHERE conversation_id = ?"
            " ORDER BY id ASC",
            (conversation_id,),
        ).fetchall()
    result = dict(row)
    result["messages"] = [dict(m) for m in msgs]
    return result


def save_global_chat_message(conversation_id: str, role: str, content: str,
                             attachments: str | None = None,
                             tool_calls: str | None = None) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO global_chat_messages"
            " (conversation_id, role, content, created_at, attachments, tool_calls)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (conversation_id, role, content, now, attachments, tool_calls),
        )
        conn.execute(
            "UPDATE global_chat_conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )


def delete_global_conversation(conversation_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM global_chat_messages WHERE conversation_id = ?",
                     (conversation_id,))
        conn.execute("DELETE FROM global_chat_conversations WHERE id = ?",
                     (conversation_id,))


def rename_global_conversation(conversation_id: str, title: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE global_chat_conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title.strip(), _now(), conversation_id),
        )


def clear_global_chat_messages(conversation_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM global_chat_messages WHERE conversation_id = ?",
                     (conversation_id,))
        conn.execute(
            "UPDATE global_chat_conversations SET updated_at = ? WHERE id = ?",
            (_now(), conversation_id),
        )


def get_dashboard_analytics() -> dict:
    from datetime import timedelta
    with _conn() as conn:
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        # Total recording time (sum of max end_time per session, in seconds)
        total_time_row = conn.execute(
            "SELECT COALESCE(SUM(max_time), 0) FROM"
            " (SELECT MAX(end_time) AS max_time FROM transcript_segments"
            "  GROUP BY session_id)"
        ).fetchone()
        total_seconds = total_time_row[0] or 0

        total_segments = conn.execute(
            "SELECT COUNT(*) FROM transcript_segments"
        ).fetchone()[0]

        # Total word count across all transcripts
        total_words_row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(text) - LENGTH(REPLACE(text, ' ', '')) + 1), 0)"
            " FROM transcript_segments WHERE text != ''"
        ).fetchone()
        total_words = total_words_row[0] or 0

        speaker_count = conn.execute(
            "SELECT COUNT(*) FROM global_speakers"
        ).fetchone()[0]

        # Sessions this week (last 7 days)
        week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
        sessions_this_week = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE started_at >= ?",
            (week_ago,),
        ).fetchone()[0]

        # Average session duration
        avg_duration_row = conn.execute(
            "SELECT AVG(max_time) FROM"
            " (SELECT MAX(end_time) AS max_time FROM transcript_segments"
            "  GROUP BY session_id)"
        ).fetchone()
        avg_duration_seconds = avg_duration_row[0] or 0

        # Weekly activity (sessions per day for last 14 days)
        two_weeks_ago = (datetime.utcnow() - timedelta(days=13)).strftime("%Y-%m-%d")
        weekly_activity = conn.execute(
            "SELECT DATE(started_at) AS day, COUNT(*) AS count"
            " FROM sessions WHERE DATE(started_at) >= ?"
            " GROUP BY DATE(started_at)"
            " ORDER BY day ASC",
            (two_weeks_ago,),
        ).fetchall()

        # Most active speakers (by number of sessions they appear in)
        # Include total talk-time per speaker
        top_speakers = conn.execute(
            "SELECT gs.name, gs.color,"
            "  COUNT(DISTINCT sl.session_id) AS session_count,"
            "  COALESCE(SUM(ts.end_time - ts.start_time), 0) AS talk_seconds"
            " FROM global_speakers gs"
            " JOIN speaker_labels sl ON sl.global_id = gs.id"
            " LEFT JOIN transcript_segments ts"
            "   ON ts.session_id = sl.session_id AND ts.source = sl.speaker_key"
            " GROUP BY gs.id"
            " ORDER BY session_count DESC"
            " LIMIT 8"
        ).fetchall()

        # Recent sessions with more detail (for the widget)
        recent_sessions = conn.execute(
            "SELECT s.id, s.title, s.started_at,"
            "  (SELECT MAX(ts.end_time) FROM transcript_segments ts"
            "   WHERE ts.session_id = s.id) AS duration_seconds,"
            "  (SELECT COUNT(DISTINCT ts.source) FROM transcript_segments ts"
            "   WHERE ts.session_id = s.id) AS speaker_count"
            " FROM sessions s ORDER BY s.started_at DESC LIMIT 10"
        ).fetchall()

    # Build activity heatmap (fill in missing days)
    activity_map = {r["day"]: r["count"] for r in weekly_activity}
    activity_data = []
    for i in range(14):
        day = (datetime.utcnow() - timedelta(days=13 - i)).strftime("%Y-%m-%d")
        activity_data.append({"day": day, "count": activity_map.get(day, 0)})

    return {
        "total_sessions": total_sessions,
        "total_seconds": total_seconds,
        "total_segments": total_segments,
        "total_words": total_words,
        "speaker_count": speaker_count,
        "sessions_this_week": sessions_this_week,
        "avg_duration_seconds": avg_duration_seconds,
        "activity": activity_data,
        "top_speakers": [dict(r) for r in top_speakers],
        "recent_sessions": [dict(r) for r in recent_sessions],
    }
