"""Knowledge base API routes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class IngestTextRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Text content to ingest")
    source_name: str = Field(..., min_length=1, max_length=255, description="Name/label for this content")


class IngestUrlRequest(BaseModel):
    url: str = Field(..., description="URL to fetch and ingest")


class IngestResponse(BaseModel):
    source: str
    chunks_added: int
    message: str


class SearchResult(BaseModel):
    text: str
    source: str
    score: float
    metadata: dict[str, Any]


class KnowledgeStats(BaseModel):
    total_documents: int
    sources: dict[str, int]


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def get_agent(request: Request) -> Any:
    return request.app.state.agent


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/ingest/text", response_model=IngestResponse)
async def ingest_text(
    body: IngestTextRequest,
    agent: Any = Depends(get_agent),
) -> IngestResponse:
    """Ingest plain text into the knowledge base."""
    try:
        chunks = agent.ingest_text(body.text, body.source_name)
        return IngestResponse(
            source=body.source_name,
            chunks_added=chunks,
            message=f"Successfully ingested '{body.source_name}': {chunks} chunks added.",
        )
    except Exception as exc:
        logger.exception("Text ingest error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/ingest/url", response_model=IngestResponse)
async def ingest_url(
    body: IngestUrlRequest,
    agent: Any = Depends(get_agent),
) -> IngestResponse:
    """Fetch a URL and ingest its content into the knowledge base."""
    try:
        chunks = agent.ingest_url(str(body.url))
        return IngestResponse(
            source=str(body.url)[:100],
            chunks_added=chunks,
            message=f"Successfully ingested URL: {chunks} chunks added.",
        )
    except Exception as exc:
        logger.exception("URL ingest error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/ingest/file", response_model=IngestResponse)
async def ingest_file(
    file: UploadFile = File(...),
    agent: Any = Depends(get_agent),
) -> IngestResponse:
    """Upload and ingest a file (PDF, Word .docx, CSV, Excel .xlsx, or plain text)."""
    if file.filename is None:
        raise HTTPException(status_code=400, detail="File must have a filename.")

    allowed_extensions = {".pdf", ".docx", ".doc", ".csv", ".xlsx", ".xls", ".txt", ".md"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(allowed_extensions))}",
        )

    try:
        content = await file.read()
        chunks = agent.ingest_document(file.filename, content)
        return IngestResponse(
            source=file.filename,
            chunks_added=chunks,
            message=f"Successfully ingested '{file.filename}': {chunks} chunks added.",
        )
    except Exception as exc:
        logger.exception("File ingest error for %s", file.filename)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/search", response_model=list[SearchResult])
async def search_knowledge(
    q: str = Query(..., min_length=1, description="Search query"),
    n: int = Query(5, ge=1, le=20, description="Number of results"),
    agent: Any = Depends(get_agent),
) -> list[SearchResult]:
    """Search the knowledge base semantically."""
    try:
        results = agent.knowledge_index.search(q, n_results=n)
        return [
            SearchResult(
                text=r["text"],
                source=r["source"],
                score=r["score"],
                metadata=r["metadata"],
            )
            for r in results
        ]
    except Exception as exc:
        logger.exception("Knowledge search error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/stats", response_model=KnowledgeStats)
async def get_stats(agent: Any = Depends(get_agent)) -> KnowledgeStats:
    """Get knowledge base statistics: total documents and per-source counts."""
    try:
        stats = agent.get_knowledge_stats()
        return KnowledgeStats(
            total_documents=stats["total_documents"],
            sources=stats.get("sources", {}),
        )
    except Exception as exc:
        logger.exception("Knowledge stats error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/source/{source_name}")
async def delete_source(source_name: str, agent: Any = Depends(get_agent)) -> dict[str, Any]:
    """Remove all documents from a given source from the knowledge base."""
    try:
        deleted = agent.knowledge_index.delete_source(source_name)
        return {"source": source_name, "deleted_chunks": deleted}
    except Exception as exc:
        logger.exception("Delete source error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
