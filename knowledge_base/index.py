"""PostgreSQL + pgvector knowledge index for the NetSuite migration knowledge base."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import numpy as np
import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"
_EMBED_DIM = 384


class KnowledgeIndex:
    """Manages document storage and semantic search via PostgreSQL + pgvector."""

    def __init__(self, data_dir: str | None = None) -> None:
        # data_dir kept for API compatibility but unused; DATABASE_URL is used.
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        # Railway sometimes emits postgres:// — psycopg2 requires postgresql://
        db_url = db_url.replace("postgres://", "postgresql://", 1)

        self._pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=db_url)
        self._model = SentenceTransformer(_MODEL_NAME)
        self._init_schema()
        logger.info("KnowledgeIndex initialized. Total documents: %d", self.count())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self):
        conn = self._pool.getconn()
        register_vector(conn)
        return conn

    def _put_conn(self, conn):
        self._pool.putconn(conn)

    def _init_schema(self) -> None:
        conn = self._get_conn()
        try:
            # ------------------------------------------------------------------
            # Step 1: Ensure pgvector extension exists.
            # psycopg2 aborts the current transaction on any error, so we need
            # to ROLLBACK before issuing further commands if this fails.
            # ------------------------------------------------------------------
            try:
                with conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.commit()
            except Exception as ext_err:
                conn.rollback()
                logger.warning("Could not CREATE EXTENSION vector: %s", ext_err)
                # Check whether the extension is already installed (e.g. it was
                # pre-installed by the platform but our role lacks CREATE
                # EXTENSION permission).
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT extname FROM pg_extension WHERE extname = 'vector'"
                    )
                    row = cur.fetchone()
                conn.commit()
                if row is None:
                    raise RuntimeError(
                        "pgvector extension is not available on this PostgreSQL "
                        "instance and could not be created. "
                        f"Original error: {ext_err}"
                    ) from ext_err
                logger.info(
                    "pgvector extension already exists — continuing despite "
                    "CREATE EXTENSION error."
                )

            # ------------------------------------------------------------------
            # Step 2: Create the documents table (safe; uses IF NOT EXISTS).
            # ------------------------------------------------------------------
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_documents (
                        id          UUID        PRIMARY KEY,
                        source      TEXT        NOT NULL,
                        text        TEXT        NOT NULL,
                        embedding   vector(384),
                        metadata    JSONB       DEFAULT '{}'::jsonb,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
            conn.commit()

            # ------------------------------------------------------------------
            # Step 3: Create HNSW index (requires pgvector >= 0.5.0).
            # Fall back to IVFFlat, then to no index (sequential scan works).
            # Each attempt is its own transaction so a failure doesn't poison
            # subsequent commands.
            # ------------------------------------------------------------------
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        CREATE INDEX IF NOT EXISTS knowledge_embedding_hnsw_idx
                        ON knowledge_documents
                        USING hnsw (embedding vector_cosine_ops)
                    """)
                conn.commit()
                logger.info("HNSW index created (or already exists).")
            except Exception as hnsw_err:
                conn.rollback()
                logger.warning(
                    "HNSW index creation failed (pgvector < 0.5.0?): %s — "
                    "trying IVFFlat fallback.",
                    hnsw_err,
                )
                try:
                    with conn.cursor() as cur:
                        cur.execute("""
                            CREATE INDEX IF NOT EXISTS knowledge_embedding_idx
                            ON knowledge_documents
                            USING ivfflat (embedding vector_cosine_ops)
                            WITH (lists = 100)
                        """)
                    conn.commit()
                    logger.info("IVFFlat index created as fallback.")
                except Exception as ivf_err:
                    conn.rollback()
                    logger.warning(
                        "IVFFlat index creation also failed: %s — "
                        "continuing without a vector index (sequential scan).",
                        ivf_err,
                    )
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_documents(self, docs: list[dict[str, Any]]) -> int:
        """Add a list of documents. Each dict needs: text, source, metadata."""
        if not docs:
            return 0

        rows = []
        for doc in docs:
            text = doc.get("text", "").strip()
            if not text:
                continue
            source = doc.get("source", "unknown")
            meta = dict(doc.get("metadata", {}) or {})
            meta["source"] = source
            rows.append((str(uuid.uuid4()), source, text, meta))

        if not rows:
            return 0

        texts = [r[2] for r in rows]
        embeddings = self._model.encode(texts, batch_size=32, show_progress_bar=False)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                for (doc_id, source, text, meta), emb in zip(rows, embeddings):
                    cur.execute(
                        """
                        INSERT INTO knowledge_documents (id, source, text, embedding, metadata)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (doc_id, source, text, emb, json.dumps(meta)),
                    )
            conn.commit()
        finally:
            self._put_conn(conn)

        logger.info("Added %d documents to knowledge index.", len(rows))
        return len(rows)

    def delete_source(self, source_name: str) -> int:
        """Remove all documents from a given source. Returns count deleted."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM knowledge_documents WHERE source = %s",
                    (source_name,),
                )
                deleted = cur.rowcount
            conn.commit()
        finally:
            self._put_conn(conn)
        logger.info("Deleted %d documents from source '%s'.", deleted, source_name)
        return deleted

    # ------------------------------------------------------------------
    # Read / Search
    # ------------------------------------------------------------------

    def search(
        self, query: str, n_results: int = 5, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Semantic search. Returns list of {text, source, score, metadata}."""
        if self.count() == 0:
            return []

        query_emb = self._model.encode([query], show_progress_bar=False)[0]

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT text, source, metadata,
                           1 - (embedding <=> %s) AS score
                    FROM knowledge_documents
                    ORDER BY embedding <=> %s
                    LIMIT %s
                    """,
                    (query_emb, query_emb, n_results),
                )
                rows = cur.fetchall()
        finally:
            self._put_conn(conn)

        return [
            {
                "text": text,
                "source": source,
                "score": round(float(score), 4),
                "metadata": meta or {},
            }
            for text, source, meta, score in rows
        ]

    def get_stats(self) -> dict[str, Any]:
        """Return total doc count and per-source counts."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM knowledge_documents")
                total = cur.fetchone()[0]
                if total == 0:
                    return {"total_documents": 0, "sources": {}}
                cur.execute(
                    "SELECT source, COUNT(*) FROM knowledge_documents GROUP BY source"
                )
                sources = {row[0]: row[1] for row in cur.fetchall()}
        finally:
            self._put_conn(conn)
        return {"total_documents": total, "sources": sources}

    def list_sources(self) -> list[str]:
        """Return sorted list of unique source names."""
        return sorted(self.get_stats().get("sources", {}).keys())

    def count(self) -> int:
        """Return total number of documents."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM knowledge_documents")
                return cur.fetchone()[0]
        finally:
            self._put_conn(conn)
