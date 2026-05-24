"""ChromaDB vector index management for the NetSuite migration knowledge base."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

logger = logging.getLogger(__name__)

_COLLECTION_NAME = "netsuite_knowledge"


class KnowledgeIndex:
    """Manages document storage and semantic search via ChromaDB + SentenceTransformers."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        if data_dir is None:
            data_dir = Path(os.getenv("DATA_DIR", "./data"))
        self.data_dir = Path(data_dir)
        self.chroma_dir = self.data_dir / "chroma_db"
        self.chroma_dir.mkdir(parents=True, exist_ok=True)

        self._embedding_fn = SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        self._client = chromadb.PersistentClient(path=str(self.chroma_dir))
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=self._embedding_fn,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info(
            "KnowledgeIndex initialized. Collection '%s' has %d documents.",
            _COLLECTION_NAME,
            self._collection.count(),
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add_documents(self, docs: list[dict[str, Any]]) -> int:
        """
        Add a list of documents to the index.

        Each doc dict must have:
            text     (str)  — the chunk text
            source   (str)  — file name or URL of origin
            metadata (dict) — arbitrary extra metadata (chunk_index, page, etc.)

        Returns the number of documents successfully added.
        """
        if not docs:
            return 0

        ids: list[str] = []
        texts: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for doc in docs:
            text = doc.get("text", "").strip()
            if not text:
                continue
            source = doc.get("source", "unknown")
            meta = doc.get("metadata", {}) or {}
            meta["source"] = source

            # Ensure all metadata values are chromadb-safe (str/int/float/bool)
            safe_meta: dict[str, Any] = {}
            for k, v in meta.items():
                if isinstance(v, (str, int, float, bool)):
                    safe_meta[k] = v
                else:
                    safe_meta[k] = str(v)

            ids.append(str(uuid.uuid4()))
            texts.append(text)
            metadatas.append(safe_meta)

        if not texts:
            return 0

        # Add in batches of 100 to avoid memory issues with large ingestions
        batch_size = 100
        added = 0
        for i in range(0, len(texts), batch_size):
            self._collection.add(
                ids=ids[i : i + batch_size],
                documents=texts[i : i + batch_size],
                metadatas=metadatas[i : i + batch_size],
            )
            added += len(texts[i : i + batch_size])

        logger.info("Added %d documents to knowledge index.", added)
        return added

    def delete_source(self, source_name: str) -> int:
        """Remove all documents from a given source. Returns count deleted."""
        results = self._collection.get(where={"source": source_name})
        if not results["ids"]:
            logger.info("No documents found for source '%s'.", source_name)
            return 0
        ids_to_delete = results["ids"]
        self._collection.delete(ids=ids_to_delete)
        logger.info(
            "Deleted %d documents from source '%s'.", len(ids_to_delete), source_name
        )
        return len(ids_to_delete)

    # ------------------------------------------------------------------
    # Read / Search
    # ------------------------------------------------------------------

    def search(
        self, query: str, n_results: int = 5, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """
        Semantic search over the knowledge base.

        Returns list of dicts with keys: text, source, score, metadata
        """
        if self._collection.count() == 0:
            return []

        kwargs: dict[str, Any] = {
            "query_texts": [query],
            "n_results": min(n_results, max(1, self._collection.count())),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            results = self._collection.query(**kwargs)
        except Exception as exc:
            logger.warning("ChromaDB query failed: %s", exc)
            return []

        hits: list[dict[str, Any]] = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            # Cosine distance → similarity score (1 = perfect match)
            score = round(1.0 - float(dist), 4)
            hits.append(
                {
                    "text": doc,
                    "source": meta.get("source", "unknown"),
                    "score": score,
                    "metadata": meta,
                }
            )
        return hits

    def get_stats(self) -> dict[str, Any]:
        """Return statistics about the knowledge base: total docs and per-source counts."""
        total = self._collection.count()
        if total == 0:
            return {"total_documents": 0, "sources": {}}

        # Retrieve all metadatas to aggregate per-source
        all_meta = self._collection.get(include=["metadatas"])["metadatas"]
        source_counts: dict[str, int] = {}
        for meta in all_meta:
            src = meta.get("source", "unknown") if meta else "unknown"
            source_counts[src] = source_counts.get(src, 0) + 1

        return {"total_documents": total, "sources": source_counts}

    def list_sources(self) -> list[str]:
        """Return a sorted list of unique source names in the index."""
        stats = self.get_stats()
        return sorted(stats.get("sources", {}).keys())

    def count(self) -> int:
        """Return total number of documents in the collection."""
        return self._collection.count()
