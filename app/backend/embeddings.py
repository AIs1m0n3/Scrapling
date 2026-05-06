"""Embedding store backed by SQLite + numpy for semantic search.

Usage:
    from app.backend.embeddings import get_store

    store = get_store()
    store.add_records(records)                     # persist + embed
    results = store.semantic_search("consulenza IT Lombardia", top_k=10)
"""

import json
import logging
import sqlite3
from typing import Optional
from pathlib import Path

import httpx

from .config import DATA_DIR, OPENROUTER_EMBED_MODEL

log = logging.getLogger(__name__)

# Import numpy lazily so the rest of the app works even if it is not installed
try:
    import numpy as np
    _NUMPY_OK = True
except ImportError:
    np = None  # type: ignore[assignment]
    _NUMPY_OK = False
    log.warning("numpy not installed — semantic search will fall back to keyword matching")

_EMBED_URL = "https://openrouter.ai/api/v1/embeddings"


def _embed_headers() -> dict:
    import os
    key = os.getenv("OPENROUTER_API_KEY", "")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://scrapling-saas.local",
        "X-Title": "Scrapling SaaS",
    }


def get_embedding(text: str) -> Optional[list[float]]:
    """Call OpenRouter embeddings API and return the embedding vector (or None on error)."""
    import os
    if not os.getenv("OPENROUTER_API_KEY"):
        log.debug("OPENROUTER_API_KEY not set — embeddings disabled")
        return None

    payload = {"model": OPENROUTER_EMBED_MODEL, "input": text[:8192]}  # safety cap
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(_EMBED_URL, headers=_embed_headers(), json=payload)
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception as exc:
        log.error(f"Embedding API error ({OPENROUTER_EMBED_MODEL}): {exc}")
        return None


class EmbeddingStore:
    """SQLite-backed store for scraped records and their embeddings.

    Schema:
      records(id, text, embedding JSON, source_url, metadata JSON, created_at)
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DATA_DIR / "embeddings.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    text        TEXT    NOT NULL,
                    embedding   TEXT,
                    source_url  TEXT,
                    metadata    TEXT,
                    created_at  TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON records(source_url)")

    def _record_to_text(self, record: dict) -> str:
        """Convert a record dict to a plain-text representation for embedding."""
        skip = {"fonte", "source_url"}
        parts = []
        for k, v in record.items():
            if k in skip or not v:
                continue
            parts.append(f"{k}: {v}")
        return " | ".join(parts)

    def add_record(self, record: dict, compute_embedding: bool = True) -> int:
        """Persist a single record, optionally computing its embedding. Returns row id."""
        text = self._record_to_text(record)
        source_url = record.get("fonte", "")
        metadata = json.dumps(
            {k: v for k, v in record.items() if k != "fonte"},
            ensure_ascii=False,
        )

        embedding: Optional[str] = None
        if compute_embedding and text:
            vec = get_embedding(text)
            if vec:
                embedding = json.dumps(vec)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO records (text, embedding, source_url, metadata) VALUES (?, ?, ?, ?)",
                (text, embedding, source_url, metadata),
            )
            return cur.lastrowid

    def add_records(self, records: list[dict], compute_embeddings: bool = True) -> int:
        """Persist multiple records. Returns count added."""
        count = 0
        for rec in records:
            try:
                self.add_record(rec, compute_embedding=compute_embeddings)
                count += 1
            except Exception as exc:
                log.warning(f"Failed to store record: {exc}")
        log.info(f"Stored {count}/{len(records)} records in embedding store ({self.db_path})")
        return count

    def semantic_search(self, query: str, top_k: int = 10) -> list[dict]:
        """Return the top_k most relevant records for the query.

        Uses cosine similarity over stored embeddings if numpy is available,
        otherwise falls back to keyword matching.
        """
        if _NUMPY_OK:
            return self._cosine_search(query, top_k)
        return self._keyword_search(query, top_k)

    def _cosine_search(self, query: str, top_k: int) -> list[dict]:
        """Cosine-similarity search using numpy."""
        query_vec = get_embedding(query)
        if query_vec is None:
            log.warning("Could not embed query — falling back to keyword search")
            return self._keyword_search(query, top_k)

        q = np.array(query_vec, dtype=np.float32)
        norm = np.linalg.norm(q)
        if norm > 0:
            q /= norm

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, text, source_url, metadata, embedding "
                "FROM records WHERE embedding IS NOT NULL"
            ).fetchall()

        results = []
        for row_id, text, source_url, metadata, emb_json in rows:
            try:
                vec = np.array(json.loads(emb_json), dtype=np.float32)
                n = np.linalg.norm(vec)
                if n > 0:
                    vec /= n
                score = float(np.dot(q, vec))
                results.append(self._build_result(row_id, text, source_url, metadata, score))
            except Exception:
                continue

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def _keyword_search(self, query: str, top_k: int) -> list[dict]:
        """Simple keyword frequency search (fallback when numpy / embeddings unavailable)."""
        words = [w.lower() for w in query.split() if len(w) > 2]
        if not words:
            return []

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id, text, source_url, metadata FROM records"
            ).fetchall()

        results = []
        for row_id, text, source_url, metadata in rows:
            lower_text = text.lower()
            hits = sum(1 for w in words if w in lower_text)
            if hits:
                score = hits / len(words)
                results.append(self._build_result(row_id, text, source_url, metadata, score))

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    @staticmethod
    def _build_result(row_id, text, source_url, metadata, score) -> dict:
        try:
            record = json.loads(metadata) if metadata else {}
        except json.JSONDecodeError:
            record = {}
        return {
            "id": row_id,
            "score": round(score, 4),
            "text": text,
            "source_url": source_url,
            "record": record,
        }

    def count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def count_with_embeddings(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM records WHERE embedding IS NOT NULL"
            ).fetchone()[0]

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM records")
        log.info("Embedding store cleared")


# ── Module-level singleton ────────────────────────────────────────────────────

_store: Optional[EmbeddingStore] = None


def get_store() -> EmbeddingStore:
    """Return the global EmbeddingStore singleton (lazy init)."""
    global _store
    if _store is None:
        _store = EmbeddingStore()
    return _store
