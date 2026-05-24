"""Knowledge base: document ingestion and ChromaDB vector index."""

from knowledge_base.index import KnowledgeIndex
from knowledge_base.ingest import DocumentIngester

__all__ = ["KnowledgeIndex", "DocumentIngester"]
