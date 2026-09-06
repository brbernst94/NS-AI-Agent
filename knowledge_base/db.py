"""Shared PostgreSQL connection pool and schema initialisation."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extensions
import psycopg2.pool

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()
# Physical connections on which the pgvector type adapter is already registered.
_vector_registered: set[int] = set()


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is not set")
    # Railway sometimes emits postgres:// — psycopg2 requires postgresql://
    return url.replace("postgres://", "postgresql://", 1)


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                maxconn = int(os.getenv("DB_POOL_MAX", "12"))
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=maxconn, dsn=database_url()
                )
    return _pool


def get_conn(register_vector_type: bool = True) -> psycopg2.extensions.connection:
    conn = get_pool().getconn()
    if register_vector_type and id(conn) not in _vector_registered:
        from pgvector.psycopg2 import register_vector

        register_vector(conn)
        _vector_registered.add(id(conn))
    return conn


def put_conn(conn: psycopg2.extensions.connection) -> None:
    get_pool().putconn(conn)


@contextmanager
def connection(register_vector_type: bool = True) -> Iterator[psycopg2.extensions.connection]:
    """Yield a pooled connection; commit on success, rollback on error."""
    conn = get_conn(register_vector_type)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_conn(conn)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_KNOWLEDGE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS knowledge_documents (
        id          UUID        PRIMARY KEY,
        source      TEXT        NOT NULL,
        text        TEXT        NOT NULL,
        embedding   vector(384),
        metadata    JSONB       DEFAULT '{}'::jsonb,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS system    TEXT DEFAULT 'netsuite'",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS module    TEXT",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS doc_type  TEXT",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS version   TEXT",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS title     TEXT",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS url       TEXT",
    "ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS tenant_id TEXT",
    "CREATE INDEX IF NOT EXISTS knowledge_source_idx   ON knowledge_documents(source)",
    "CREATE INDEX IF NOT EXISTS knowledge_system_idx   ON knowledge_documents(system, module)",
    "CREATE INDEX IF NOT EXISTS knowledge_doc_type_idx ON knowledge_documents(doc_type)",
    "CREATE INDEX IF NOT EXISTS knowledge_tenant_idx   ON knowledge_documents(tenant_id)",
]

# tenant_id '' means shared/standard knowledge. Kept NOT NULL so it can sit in
# composite primary keys (NULLs are never equal in PK comparisons).
_CATALOG_DDL = [
    """
    CREATE TABLE IF NOT EXISTS ns_record_types (
        id          TEXT        NOT NULL,
        tenant_id   TEXT        NOT NULL DEFAULT '',
        label       TEXT,
        category    TEXT,
        description TEXT,
        url         TEXT,
        source      TEXT,
        version     TEXT,
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (id, tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ns_fields (
        record_type   TEXT    NOT NULL,
        field_id      TEXT    NOT NULL,
        tenant_id     TEXT    NOT NULL DEFAULT '',
        label         TEXT,
        type          TEXT,
        required      BOOLEAN DEFAULT FALSE,
        read_only     BOOLEAN DEFAULT FALSE,
        is_custom     BOOLEAN DEFAULT FALSE,
        select_record TEXT,
        enum_values   JSONB,
        max_length    INTEGER,
        help          TEXT,
        source        TEXT,
        updated_at    TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (record_type, field_id, tenant_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ns_fields_record_idx ON ns_fields(record_type)",
    "CREATE INDEX IF NOT EXISTS ns_fields_label_idx  ON ns_fields(lower(label))",
    """
    CREATE TABLE IF NOT EXISTS ns_sublists (
        record_type TEXT NOT NULL,
        sublist_id  TEXT NOT NULL,
        tenant_id   TEXT NOT NULL DEFAULT '',
        label       TEXT,
        source      TEXT,
        PRIMARY KEY (record_type, sublist_id, tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ns_sublist_fields (
        record_type   TEXT    NOT NULL,
        sublist_id    TEXT    NOT NULL,
        field_id      TEXT    NOT NULL,
        tenant_id     TEXT    NOT NULL DEFAULT '',
        label         TEXT,
        type          TEXT,
        required      BOOLEAN DEFAULT FALSE,
        is_custom     BOOLEAN DEFAULT FALSE,
        select_record TEXT,
        help          TEXT,
        source        TEXT,
        PRIMARY KEY (record_type, sublist_id, field_id, tenant_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS crawl_urls (
        url          TEXT PRIMARY KEY,
        crawler      TEXT,
        status       TEXT,
        chunks_added INTEGER DEFAULT 0,
        error        TEXT,
        crawled_at   TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    "CREATE INDEX IF NOT EXISTS crawl_urls_crawler_idx ON crawl_urls(crawler, status)",
]


def _ensure_vector_extension(conn: psycopg2.extensions.connection) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
    except Exception as ext_err:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            present = cur.fetchone() is not None
        conn.commit()
        if not present:
            raise RuntimeError(
                "pgvector extension is not available on this PostgreSQL instance "
                f"and could not be created. Original error: {ext_err}"
            ) from ext_err
        logger.info("pgvector extension already present; CREATE EXTENSION not permitted for this role.")


def _ensure_vector_index(conn: psycopg2.extensions.connection) -> None:
    attempts = [
        (
            "hnsw",
            "CREATE INDEX IF NOT EXISTS knowledge_embedding_hnsw_idx "
            "ON knowledge_documents USING hnsw (embedding vector_cosine_ops)",
        ),
        (
            "ivfflat",
            "CREATE INDEX IF NOT EXISTS knowledge_embedding_idx "
            "ON knowledge_documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)",
        ),
    ]
    for name, ddl in attempts:
        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            return
        except Exception as exc:
            conn.rollback()
            logger.warning("%s index creation failed: %s", name, exc)
    logger.warning("No vector index available — searches will use sequential scan.")


_schema_ready = False


def init_schema() -> None:
    """Create/upgrade every knowledge-related table. Idempotent."""
    global _schema_ready
    if _schema_ready:
        return
    # register_vector needs the type to exist, so use a raw connection here.
    conn = get_conn(register_vector_type=False)
    try:
        _ensure_vector_extension(conn)
        with conn.cursor() as cur:
            for ddl in _KNOWLEDGE_DDL + _CATALOG_DDL:
                cur.execute(ddl)
        conn.commit()
        _ensure_vector_index(conn)
    finally:
        put_conn(conn)
    _schema_ready = True
    logger.info("Knowledge schema ready.")
