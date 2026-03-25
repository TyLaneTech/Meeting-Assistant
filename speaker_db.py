"""
Cross-session speaker fingerprint database.

Stores 256-dim L2-normalized embeddings for each global speaker profile and
maintains an incremental centroid for fast cosine-similarity matching.

Usage:
    db = SpeakerFingerprintDB(storage.DB_PATH, hf_token)
    global_id = db.create_global_speaker("Alice")
    emb = db.extract_embedding(audio_np)          # float32 mono 16 kHz
    db.add_embedding(global_id, session_id, speaker_key, emb, duration_sec)
    matches = db.find_matches(emb)                # [{global_id, name, similarity, ...}]
"""
import sqlite3
import traceback
import uuid
import warnings
from contextlib import contextmanager
from datetime import datetime
import logging
import sys
from pathlib import Path

import numpy as np

import log

_SUGGEST_THRESHOLD    = 0.70   # cosine sim → push fingerprint_match SSE
_AUTO_APPLY_THRESHOLD = 0.82   # cosine sim → apply silently
_MIN_DURATION_SEC     = 2.5    # minimum audio before extracting embedding
_EMB_DIM              = 256    # WeSpeaker embedding dimension

_SUPPRESSED_LOAD_SUBSTRINGS = (
    "Warning: You are sending unauthenticated requests to the HF Hub.",
    "Please set a HF_TOKEN to enable higher rate limits and faster downloads.",
    "BertModel LOAD REPORT",
    "embeddings.position_ids | UNEXPECTED",
    "UNEXPECTED    :can be ignored",
)


class _FilteredStream:
    def __init__(self, wrapped):
        self._wrapped = wrapped
        self._buffer = ""

    def write(self, data):
        if not data:
            return 0
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if not any(s in line for s in _SUPPRESSED_LOAD_SUBSTRINGS):
                self._wrapped.write(line + "\n")
        return len(data)

    def flush(self):
        if self._buffer and not any(s in self._buffer for s in _SUPPRESSED_LOAD_SUBSTRINGS):
            self._wrapped.write(self._buffer)
        self._buffer = ""
        self._wrapped.flush()

    def isatty(self):
        return getattr(self._wrapped, "isatty", lambda: False)()


@contextmanager
def _suppress_model_load_noise():
    import os

    warnings.filterwarnings("ignore", message=".*unauthenticated.*")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    for logger_name in ("huggingface_hub", "sentence_transformers", "transformers", "safetensors"):
        logging.getLogger(logger_name).setLevel(logging.ERROR)

    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = _FilteredStream(orig_stdout)
        sys.stderr = _FilteredStream(orig_stderr)
        yield
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout = orig_stdout
            sys.stderr = orig_stderr


@contextmanager
def _conn(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 3000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat()


def _emb_to_blob(emb: np.ndarray) -> bytes:
    return emb.astype(np.float32).tobytes()


def _blob_to_emb(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).copy()


def _normalize(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 1e-8 else v


class SpeakerFingerprintDB:
    """
    Persistent cross-session speaker identity database.

    Thread-safe: each method opens its own short-lived SQLite connection.
    All embedding vectors are L2-normalized float32 (256,).
    Cosine similarity == dot product for L2-normalized vectors.
    """

    SUGGEST_THRESHOLD    = _SUGGEST_THRESHOLD
    AUTO_APPLY_THRESHOLD = _AUTO_APPLY_THRESHOLD
    MIN_DURATION_SEC     = _MIN_DURATION_SEC

    def __init__(self, db_path: Path, hf_token: str, device: str = "cpu") -> None:
        self._db_path = db_path
        self._ready   = False
        self._inference = None

        if not hf_token:
            log.warn("fingerprint", "No HF token — voice library disabled.")
            return

        try:
            # PyTorch 2.6: patch torch.load to use weights_only=False for trusted
            # local pyannote checkpoints (many internal globals, not enumerable).
            import torch as _torch
            if not getattr(_torch.load, "_patched_weights_only", False):
                _orig = _torch.load
                def _patched(f, map_location=None, pickle_module=None,
                              weights_only=None, mmap=None, **kw):
                    if map_location is not None: kw["map_location"] = map_location
                    if pickle_module is not None: kw["pickle_module"] = pickle_module
                    if mmap is not None: kw["mmap"] = mmap
                    return _orig(f, weights_only=(False if weights_only is None else weights_only), **kw)
                _patched._patched_weights_only = True
                _torch.load = _patched
        except Exception:
            pass

        try:
            with _suppress_model_load_noise():
                from pyannote.audio import Inference, Model  # type: ignore
                log.info("fingerprint", "Loading embedding model…")
                model = Model.from_pretrained(
                    "pyannote/wespeaker-voxceleb-resnet34-LM",
                    use_auth_token=hf_token,
                )
                self._inference = Inference(model, window="whole")
            # Move to requested device
            if device and device != "cpu":
                import torch
                self._inference.model = self._inference.model.to(torch.device(device))
            self._ready = True
            log.info("fingerprint", f"Embedding model ready on {device}.")
        except Exception as e:
            log.warn("fingerprint", f"Could not load embedding model: {e}")

    # ── Public helpers ────────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Profile CRUD ──────────────────────────────────────────────────────────

    def create_global_speaker(self, name: str, color: str | None = None) -> str:
        """Create a new global profile. Returns global_id."""
        gid = uuid.uuid4().hex
        now = _now()
        with _conn(self._db_path) as c:
            c.execute(
                "INSERT INTO global_speakers (id, name, color, emb_count, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (gid, name.strip(), color, now, now),
            )
        log.info("fingerprint", f"Created global profile: {name!r} ({gid[:8]})")
        return gid

    def rename_global_speaker(
        self,
        global_id: str,
        name: str | None = None,
        color: str | None = ...,  # type: ignore[assignment]
    ) -> None:
        """Update name and/or color. Pass color=None to clear; omit to keep."""
        with _conn(self._db_path) as c:
            if name is not None:
                c.execute(
                    "UPDATE global_speakers SET name=?, updated_at=? WHERE id=?",
                    (name.strip(), _now(), global_id),
                )
            if color is not ...:
                c.execute(
                    "UPDATE global_speakers SET color=?, updated_at=? WHERE id=?",
                    (color, _now(), global_id),
                )

    def delete_global_speaker(self, global_id: str) -> None:
        """Delete profile and all embeddings. Nulls speaker_labels.global_id via FK cascade."""
        # Manually null the FK since SQLite ON DELETE CASCADE on speaker_labels
        # requires the FK to be set up on that table — we handle it explicitly.
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_labels SET global_id = NULL WHERE global_id = ?",
                (global_id,),
            )
            c.execute("DELETE FROM global_speakers WHERE id = ?", (global_id,))
        log.info("fingerprint", f"Deleted global profile {global_id[:8]}")

    def merge_global_speakers(self, keep_id: str, merge_id: str) -> None:
        """Move all embeddings from merge_id → keep_id, recompute centroid, delete merge_id."""
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_embeddings SET global_id = ? WHERE global_id = ?",
                (keep_id, merge_id),
            )
            c.execute(
                "UPDATE speaker_labels SET global_id = ? WHERE global_id = ?",
                (keep_id, merge_id),
            )
            c.execute("DELETE FROM global_speakers WHERE id = ?", (merge_id,))
        self.recompute_centroid(keep_id)
        log.info("fingerprint", f"Merged {merge_id[:8]} → {keep_id[:8]}")

    def list_global_speakers(self) -> list[dict]:
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT id, name, color, emb_count, updated_at "
                "FROM global_speakers ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_global_speaker(self, global_id: str) -> dict | None:
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT id, name, color, emb_count, created_at, updated_at "
                "FROM global_speakers WHERE id = ?",
                (global_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_by_name(self, name: str) -> dict | None:
        """Case-insensitive lookup of a global profile by name. Returns first match or None."""
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT id, name, color, emb_count, created_at, updated_at "
                "FROM global_speakers WHERE lower(name) = lower(?)",
                (name.strip(),),
            ).fetchone()
        return dict(row) if row else None

    def get_profile_sessions(self, global_id: str) -> list[dict]:
        """Return sessions where this speaker appeared, with speaker_keys and segment counts."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                """
                SELECT s.id AS session_id, s.title, s.started_at,
                       sl.speaker_key,
                       (SELECT COUNT(*) FROM transcript_segments ts
                        WHERE ts.session_id = s.id AND ts.source = sl.speaker_key) AS seg_count
                FROM speaker_labels sl
                JOIN sessions s ON s.id = sl.session_id
                WHERE sl.global_id = ?
                ORDER BY s.started_at DESC
                """,
                (global_id,),
            ).fetchall()

        # Group by session
        by_session: dict[str, dict] = {}
        for r in rows:
            sid = r["session_id"]
            if sid not in by_session:
                by_session[sid] = {
                    "session_id": sid,
                    "title":      r["title"],
                    "started_at": r["started_at"],
                    "speaker_keys": [],
                    "seg_count":  0,
                }
            by_session[sid]["speaker_keys"].append(r["speaker_key"])
            by_session[sid]["seg_count"] += r["seg_count"]

        return list(by_session.values())

    # ── Embedding extraction ──────────────────────────────────────────────────

    def extract_embedding(self, audio: np.ndarray) -> np.ndarray | None:
        """
        Extract a 256-dim L2-normalized speaker embedding.
        audio: float32 mono numpy array at 16 kHz.
        Returns None if not ready or extraction fails.
        """
        if not self._ready or self._inference is None:
            return None
        try:
            import torch
            # pyannote Inference expects {"waveform": (1, samples) tensor, "sample_rate": int}
            waveform = torch.from_numpy(audio).float().unsqueeze(0)
            result = self._inference({"waveform": waveform, "sample_rate": 16_000})
            emb = np.array(result).flatten().astype(np.float32)
            return _normalize(emb)
        except Exception:
            log.warn("fingerprint", "Embedding extraction failed:")
            traceback.print_exc()
            return None

    def extract_embedding_from_wav(
        self, wav_path: str, start_sec: float, end_sec: float
    ) -> np.ndarray | None:
        """Extract a speaker embedding from a time slice of a WAV file."""
        if not self._ready or end_sec - start_sec < _MIN_DURATION_SEC:
            return None
        try:
            import wave
            from scipy import signal as scipy_signal
            with wave.open(wav_path, "rb") as wf:
                rate = wf.getframerate()
                channels = wf.getnchannels()
                wf.setpos(int(start_sec * rate))
                n_frames = int((end_sec - start_sec) * rate)
                raw = wf.readframes(n_frames)
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                audio = audio.reshape(-1, channels).mean(axis=1)
            if rate != 16000:
                audio = scipy_signal.resample_poly(audio, 16000, rate)
            return self.extract_embedding(audio)
        except Exception:
            log.warn("fingerprint", f"WAV slice extraction failed ({start_sec:.1f}-{end_sec:.1f}s)")
            traceback.print_exc()
            return None

    # ── Embedding storage ─────────────────────────────────────────────────────

    def add_embedding(
        self,
        global_id: str,
        session_id: str,
        speaker_key: str,
        embedding: np.ndarray,
        duration_sec: float,
    ) -> None:
        """Store embedding and incrementally update centroid. All embeddings are kept."""
        now = _now()
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT centroid, emb_count FROM global_speakers WHERE id = ?",
                (global_id,),
            ).fetchone()
            if row is None:
                return

            old_count    = row["emb_count"]
            old_centroid = _blob_to_emb(row["centroid"]) if row["centroid"] else None

            c.execute(
                "INSERT INTO speaker_embeddings "
                "(global_id, session_id, speaker_key, embedding, duration_sec, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (global_id, session_id, speaker_key, _emb_to_blob(embedding), duration_sec, now),
            )

            new_count = old_count + 1

            # Incremental centroid update
            if old_centroid is not None:
                new_centroid = _normalize(old_centroid * old_count + embedding)
            else:
                new_centroid = _normalize(embedding)

            c.execute(
                "UPDATE global_speakers SET centroid=?, emb_count=?, updated_at=? WHERE id=?",
                (_emb_to_blob(new_centroid), new_count, now, global_id),
            )

    # ── Matching ──────────────────────────────────────────────────────────────

    def find_matches(
        self,
        embedding: np.ndarray,
        exclude_global_ids: set | None = None,
        top_k: int = 3,
    ) -> list[dict]:
        """
        Compare embedding against all global profile centroids.
        Returns list of matches >= SUGGEST_THRESHOLD, sorted by similarity desc.
        Each entry: {global_id, name, color, similarity, auto_apply}.
        """
        if not self._ready:
            return []

        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT id, name, color, centroid FROM global_speakers WHERE centroid IS NOT NULL"
            ).fetchall()

        if not rows:
            return []

        exclude = exclude_global_ids or set()
        results = []
        for r in rows:
            if r["id"] in exclude:
                continue
            centroid = _blob_to_emb(r["centroid"])
            # Cosine similarity = dot product for L2-normalized vectors
            sim = float(np.dot(embedding, centroid))
            if sim >= _SUGGEST_THRESHOLD:
                results.append({
                    "global_id":   r["id"],
                    "name":        r["name"],
                    "color":       r["color"],
                    "similarity":  round(sim, 3),
                    "auto_apply":  sim >= _AUTO_APPLY_THRESHOLD,
                })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:top_k]

    # ── Session linking ───────────────────────────────────────────────────────

    def link_session_speaker(
        self, session_id: str, speaker_key: str, global_id: str
    ) -> None:
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_labels SET global_id = ? "
                "WHERE session_id = ? AND speaker_key = ?",
                (global_id, session_id, speaker_key),
            )

    def unlink_session_speaker(self, session_id: str, speaker_key: str) -> None:
        with _conn(self._db_path) as c:
            c.execute(
                "UPDATE speaker_labels SET global_id = NULL "
                "WHERE session_id = ? AND speaker_key = ?",
                (session_id, speaker_key),
            )

    def get_link(self, session_id: str, speaker_key: str) -> str | None:
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT global_id FROM speaker_labels "
                "WHERE session_id = ? AND speaker_key = ?",
                (session_id, speaker_key),
            ).fetchone()
        return row["global_id"] if row else None

    def get_session_links(self, session_id: str) -> dict[str, str]:
        """Return {speaker_key: global_id} for all linked speakers in a session."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT speaker_key, global_id FROM speaker_labels "
                "WHERE session_id = ? AND global_id IS NOT NULL",
                (session_id,),
            ).fetchall()
        return {r["speaker_key"]: r["global_id"] for r in rows}

    # ── Maintenance ───────────────────────────────────────────────────────────

    def recompute_centroid(self, global_id: str) -> None:
        """Full centroid recompute from all stored embeddings (used after merge/prune)."""
        with _conn(self._db_path) as c:
            rows = c.execute(
                "SELECT embedding FROM speaker_embeddings WHERE global_id = ?",
                (global_id,),
            ).fetchall()
            if not rows:
                c.execute(
                    "UPDATE global_speakers SET centroid=NULL, emb_count=0, updated_at=? WHERE id=?",
                    (_now(), global_id),
                )
                return
            embs = np.stack([_blob_to_emb(r["embedding"]) for r in rows])
            centroid = _normalize(embs.mean(axis=0))
            c.execute(
                "UPDATE global_speakers SET centroid=?, emb_count=?, updated_at=? WHERE id=?",
                (_emb_to_blob(centroid), len(rows), _now(), global_id),
            )

    def prune_embeddings(self, global_id: str, keep_newest: int = 30) -> None:
        """Delete oldest embeddings beyond keep_newest, then recompute centroid."""
        with _conn(self._db_path) as c:
            count = c.execute(
                "SELECT COUNT(*) FROM speaker_embeddings WHERE global_id = ?",
                (global_id,),
            ).fetchone()[0]
            to_delete = count - keep_newest
            if to_delete > 0:
                ids = c.execute(
                    "SELECT id FROM speaker_embeddings WHERE global_id = ? "
                    "ORDER BY id ASC LIMIT ?",
                    (global_id, to_delete),
                ).fetchall()
                c.executemany(
                    "DELETE FROM speaker_embeddings WHERE id = ?",
                    [(r["id"],) for r in ids],
                )
        self.recompute_centroid(global_id)
        log.info("fingerprint", f"Pruned {global_id[:8]}: kept newest {keep_newest}")

    def get_latest_embedding(self, global_id: str, session_id: str, speaker_key: str) -> np.ndarray | None:
        """Return the most recently added embedding for a given (session, speaker_key, global_id)."""
        with _conn(self._db_path) as c:
            row = c.execute(
                "SELECT embedding FROM speaker_embeddings "
                "WHERE global_id = ? AND session_id = ? AND speaker_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (global_id, session_id, speaker_key),
            ).fetchone()
        return _blob_to_emb(row["embedding"]) if row else None
