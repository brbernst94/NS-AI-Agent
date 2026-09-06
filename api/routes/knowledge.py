"""Knowledge base API routes: ingestion, search, NetSuite catalog, crawler control."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge", tags=["knowledge"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class IngestTextRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source_name: str = Field(..., min_length=1, max_length=255)
    module: str | None = None
    doc_type: str | None = None


class IngestUrlRequest(BaseModel):
    url: str
    module: str | None = None


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
    doc_types: dict[str, int] = {}
    modules: dict[str, int] = {}


class CrawlStartRequest(BaseModel):
    target: str = Field("all", description="docs | records_browser | all")
    max_pages: int | None = Field(None, ge=1)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def get_agent(request: Request) -> Any:
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        err = getattr(request.app.state, "startup_error", "unknown error")
        raise HTTPException(status_code=503, detail=f"Agent not initialized: {err}")
    return agent


def get_crawl_manager(request: Request) -> Any:
    cm = getattr(request.app.state, "crawl_manager", None)
    if cm is None:
        raise HTTPException(status_code=503, detail="Crawl manager not initialized")
    return cm


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

@router.post("/ingest/text", response_model=IngestResponse)
async def ingest_text(body: IngestTextRequest, agent: Any = Depends(get_agent)) -> IngestResponse:
    try:
        from knowledge_base.ingest import DocumentIngester

        chunks = DocumentIngester(agent.knowledge_index).ingest_text(
            body.text, body.source_name,
            tags={"module": body.module, "doc_type": body.doc_type or "upload", "title": body.source_name},
        )
        return IngestResponse(source=body.source_name, chunks_added=chunks,
                              message=f"Ingested '{body.source_name}': {chunks} chunks added.")
    except Exception as exc:
        logger.exception("Text ingest error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/ingest/url", response_model=IngestResponse)
async def ingest_url(body: IngestUrlRequest, agent: Any = Depends(get_agent)) -> IngestResponse:
    try:
        from knowledge_base.ingest import DocumentIngester

        chunks = DocumentIngester(agent.knowledge_index).ingest_url(str(body.url), tags={"module": body.module})
        return IngestResponse(source=str(body.url)[:100], chunks_added=chunks,
                              message=f"Ingested URL: {chunks} chunks added.")
    except Exception as exc:
        logger.exception("URL ingest error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/ingest/file", response_model=IngestResponse)
async def ingest_file(
    file: UploadFile = File(...),
    module: str | None = Query(None),
    agent: Any = Depends(get_agent),
) -> IngestResponse:
    if file.filename is None:
        raise HTTPException(status_code=400, detail="File must have a filename.")
    allowed = {".pdf", ".docx", ".doc", ".csv", ".xlsx", ".xls", ".txt", ".md"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext}'. Allowed: {', '.join(sorted(allowed))}")
    try:
        from knowledge_base.ingest import DocumentIngester

        content = await file.read()
        chunks = DocumentIngester(agent.knowledge_index).ingest_file(file.filename, content, tags={"module": module})
        return IngestResponse(source=file.filename, chunks_added=chunks,
                              message=f"Ingested '{file.filename}': {chunks} chunks added.")
    except Exception as exc:
        logger.exception("File ingest error for %s", file.filename)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Search / stats
# ---------------------------------------------------------------------------

@router.get("/search", response_model=list[SearchResult])
async def search_knowledge(
    q: str = Query(..., min_length=1),
    n: int = Query(5, ge=1, le=20),
    module: str | None = Query(None),
    doc_type: str | None = Query(None),
    agent: Any = Depends(get_agent),
) -> list[SearchResult]:
    try:
        where = {k: v for k, v in {"module": module, "doc_type": doc_type}.items() if v}
        results = agent.knowledge_index.search(q, n_results=n, where=where or None)
        return [SearchResult(text=r["text"], source=r["source"], score=r["score"], metadata=r["metadata"]) for r in results]
    except Exception as exc:
        logger.exception("Knowledge search error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/stats", response_model=KnowledgeStats)
async def get_stats(agent: Any = Depends(get_agent)) -> KnowledgeStats:
    try:
        stats = agent.get_knowledge_stats()
        return KnowledgeStats(
            total_documents=stats["total_documents"],
            sources=stats.get("sources", {}),
            doc_types=stats.get("doc_types", {}),
            modules=stats.get("modules", {}),
        )
    except Exception as exc:
        logger.exception("Knowledge stats error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/source/{source_name:path}")
async def delete_source(source_name: str, agent: Any = Depends(get_agent)) -> dict[str, Any]:
    try:
        deleted = agent.knowledge_index.delete_source(source_name)
        return {"source": source_name, "deleted_chunks": deleted}
    except Exception as exc:
        logger.exception("Delete source error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# NetSuite catalog
# ---------------------------------------------------------------------------

@router.get("/catalog/stats")
async def catalog_stats(agent: Any = Depends(get_agent)) -> dict[str, Any]:
    try:
        return agent.catalog.stats()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/catalog/records")
async def catalog_records(category: str | None = Query(None), agent: Any = Depends(get_agent)) -> list[dict[str, Any]]:
    return agent.catalog.list_record_types(category)


@router.get("/catalog/record/{record_id}")
async def catalog_record(record_id: str, agent: Any = Depends(get_agent)) -> dict[str, Any]:
    resolved = agent.catalog.resolve_record_type(record_id) or record_id.lower()
    record = agent.catalog.get_record(resolved)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Record type '{record_id}' not in catalog")
    return record


@router.get("/catalog/fields")
async def catalog_fields(
    q: str = Query(..., min_length=1),
    record_type: str | None = Query(None),
    agent: Any = Depends(get_agent),
) -> list[dict[str, Any]]:
    rid = agent.catalog.resolve_record_type(record_type) if record_type else None
    return agent.catalog.find_fields(q, rid)


@router.post("/catalog/import")
async def catalog_import(
    file: UploadFile = File(...),
    version: str | None = Query(None, description="NetSuite release, e.g. 2026.1"),
    agent: Any = Depends(get_agent),
) -> dict[str, Any]:
    """Import a zip produced by tools/netsuite_extract.py (REST metadata catalog)."""
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload the .zip produced by tools/netsuite_extract.py")
    try:
        from knowledge_base.metadata_import import MetadataImporter

        data = await file.read()
        importer = MetadataImporter(agent.catalog, version=version)
        importer.load_zip(data)
        report = importer.run()
        report["schemas_loaded"] = len(importer.schemas)
        report["catalog"] = agent.catalog.stats()
        return report
    except Exception as exc:
        logger.exception("Catalog import error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Crawler control
# ---------------------------------------------------------------------------

@router.post("/crawl/start")
async def crawl_start(body: CrawlStartRequest, cm: Any = Depends(get_crawl_manager)) -> dict[str, Any]:
    try:
        return cm.start(body.target, body.max_pages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/crawl/status")
async def crawl_status(cm: Any = Depends(get_crawl_manager)) -> dict[str, Any]:
    return cm.status()
