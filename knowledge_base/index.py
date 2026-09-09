"""PostgreSQL + pgvector knowledge index for the NetSuite knowledge base."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from psycopg2.extras import execute_values
from sentence_transformers import SentenceTransformer

from knowledge_base import db

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"

# Columns promoted out of the metadata blob so they can be filtered/indexed.
TAG_COLUMNS = ("system", "module", "doc_type", "version", "title", "url", "tenant_id")

# Rank boosts, subtracted from cosine distance so lower is better.
# A tenant's own documents outrank shared knowledge, and a distilled guide
# outranks the raw fragments it was written from.
_TENANT_BOOST = 0.05
_GUIDE_BOOST = 0.06


class KnowledgeIndex:
    """Document storage and semantic search via PostgreSQL + pgvector."""

    def __init__(self, data_dir: str | None = None) -> None:
        # data_dir kept for API compatibility; DATABASE_URL is the only source.
        db.init_schema()
        self._model = SentenceTransformer(_MODEL_NAME)
        logger.info("KnowledgeIndex initialized. Total documents: %d", self.count())

    def embed(self, texts: list[str]) -> list:
        return list(self._model.encode(texts, batch_size=32, show_progress_bar=False))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_documents(self, docs: list[dict[str, Any]]) -> int:
        """Add documents. Each dict: text, source, metadata (tags read from metadata)."""
        if not docs:
            return 0

        rows: list[tuple] = []
        texts: list[str] = []
        for doc in docs:
            text = (doc.get("text") or "").strip()
            if not text:
                continue
            source = doc.get("source", "unknown")
            meta = dict(doc.get("metadata") or {})
            meta["source"] = source
            tags = {col: meta.get(col) for col in TAG_COLUMNS}
            if not tags["system"]:
                tags["system"] = "netsuite"
            rows.append((str(uuid.uuid4()), source, text, json.dumps(meta), tags))
            texts.append(text)

        if not rows:
            return 0

        embeddings = self.embed(texts)
        values = [
            (
                doc_id, source, text, emb, meta_json,
                tags["system"], tags["module"], tags["doc_type"], tags["version"],
                tags["title"], tags["url"], tags["tenant_id"],
            )
            for (doc_id, source, text, meta_json, tags), emb in zip(rows, embeddings)
        ]

        with db.connection() as conn, conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO knowledge_documents
                    (id, source, text, embedding, metadata,
                     system, module, doc_type, version, title, url, tenant_id)
                VALUES %s
                """,
                values,
                page_size=200,
            )

        logger.info("Added %d documents to knowledge index.", len(values))
        return len(values)

    def delete_source(self, source_name: str, tenant_id: str | None = None) -> int:
        with db.connection() as conn, conn.cursor() as cur:
            if tenant_id is None:
                cur.execute("DELETE FROM knowledge_documents WHERE source = %s", (source_name,))
            else:
                cur.execute(
                    "DELETE FROM knowledge_documents WHERE source = %s AND tenant_id = %s",
                    (source_name, tenant_id),
                )
            deleted = cur.rowcount
        logger.info("Deleted %d documents from source '%s'.", deleted, source_name)
        return deleted

    # ------------------------------------------------------------------
    # Read / Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        n_results: int = 5,
        where: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search returning {text, source, score, metadata}.

        `where` may filter on any TAG column (system, module, doc_type, version).
        Shared documents (tenant_id IS NULL) are always visible; if `tenant_id`
        is given, that tenant's documents are also visible and ranked slightly higher.
        """
        query_emb = self.embed([query])[0]

        clauses = ["(tenant_id IS NULL OR tenant_id = %s)"]
        params: list[Any] = [tenant_id or ""]
        for col, val in (where or {}).items():
            if col in TAG_COLUMNS and val not in (None, ""):
                if isinstance(val, (list, tuple, set)):
                    clauses.append(f"{col} = ANY(%s)")
                    params.append(list(val))
                else:
                    clauses.append(f"{col} = %s")
                    params.append(val)

        sql = f"""
            SELECT text, source, metadata, module, doc_type, title, url, tenant_id,
                   1 - (embedding <=> %s) AS score
            FROM knowledge_documents
            WHERE {' AND '.join(clauses)}
            ORDER BY (embedding <=> %s)
                     - CASE WHEN tenant_id = %s THEN {_TENANT_BOOST} ELSE 0 END
                     - CASE WHEN doc_type = 'guide' THEN {_GUIDE_BOOST} ELSE 0 END
            LIMIT %s
        """
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, [query_emb, *params, query_emb, tenant_id or "", n_results])
            rows = cur.fetchall()

        hits = []
        for text, source, meta, module, doc_type, title, url, tid, score in rows:
            meta = dict(meta or {})
            meta.update({k: v for k, v in {
                "module": module, "doc_type": doc_type, "title": title, "url": url,
            }.items() if v})
            hits.append({
                "text": text,
                "source": source,
                "score": round(float(score), 4),
                "metadata": meta,
                "tenant_id": tid,
            })
        return hits

    def get_stats(self) -> dict[str, Any]:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM knowledge_documents")
            total = cur.fetchone()[0]
            if total == 0:
                return {"total_documents": 0, "sources": {}, "doc_types": {}, "modules": {}}
            cur.execute("SELECT source, COUNT(*) FROM knowledge_documents GROUP BY source")
            sources = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(
                "SELECT COALESCE(doc_type,'untagged'), COUNT(*) FROM knowledge_documents GROUP BY 1"
            )
            doc_types = {r[0]: r[1] for r in cur.fetchall()}
            cur.execute(
                "SELECT COALESCE(module,'untagged'), COUNT(*) FROM knowledge_documents GROUP BY 1"
            )
            modules = {r[0]: r[1] for r in cur.fetchall()}
        return {
            "total_documents": total,
            "sources": sources,
            "doc_types": doc_types,
            "modules": modules,
        }

    def list_sources(self, prefix: str | None = None) -> list[str]:
        with db.connection() as conn, conn.cursor() as cur:
            if prefix:
                cur.execute(
                    "SELECT DISTINCT source FROM knowledge_documents WHERE source LIKE %s",
                    (prefix + "%",),
                )
            else:
                cur.execute("SELECT DISTINCT source FROM knowledge_documents")
            return sorted(r[0] for r in cur.fetchall())

    def has_source(self, source_name: str) -> bool:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM knowledge_documents WHERE source = %s LIMIT 1", (source_name,)
            )
            return cur.fetchone() is not None

    def count(self) -> int:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM knowledge_documents")
            return cur.fetchone()[0]
