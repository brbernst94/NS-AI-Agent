"""Knowledge base: PostgreSQL/pgvector index, NetSuite catalog, crawlers, ingestion.

Submodules are imported lazily so that lightweight users (DB pool, catalog,
crawlers) don't pay for loading sentence-transformers/torch.
"""

from __future__ import annotations

__all__ = ["KnowledgeIndex", "DocumentIngester", "NetSuiteCatalog"]


def __getattr__(name: str):
    if name == "KnowledgeIndex":
        from knowledge_base.index import KnowledgeIndex

        return KnowledgeIndex
    if name == "DocumentIngester":
        from knowledge_base.ingest import DocumentIngester

        return DocumentIngester
    if name == "NetSuiteCatalog":
        from knowledge_base.catalog import NetSuiteCatalog

        return NetSuiteCatalog
    raise AttributeError(name)
