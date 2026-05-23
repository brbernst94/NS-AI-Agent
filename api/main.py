"""FastAPI application entry point for NS-AI-Agent."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="NS-AI-Agent API",
        description="NetSuite Data Migration AI Agent REST API",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ---------------------------------------------------------------------------
    # CORS
    # ---------------------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Tighten in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------------------------------------------------------------------------
    # Startup / Shutdown
    # ---------------------------------------------------------------------------

    @app.on_event("startup")
    async def startup() -> None:
        """Initialize the agent, database, and knowledge base on startup."""
        import asyncio
        import threading
        from agent.core import NSMigrationAgent
        from knowledge_base.crawler import NetSuiteCrawler

        data_dir = os.getenv("DATA_DIR", "./data")
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        # Restore knowledge base from GitHub before initializing
        from agent.github_sync import restore_from_github
        restore_from_github()

        logger.info("Initializing NS-AI-Agent...")
        agent = NSMigrationAgent(data_dir=data_dir)
        app.state.agent = agent
        logger.info("NS-AI-Agent ready. Knowledge base: %d documents.", agent.knowledge_index.count())

        # Crawl NetSuite docs in background thread so startup isn't blocked
        def run_crawler() -> None:
            try:
                crawler = NetSuiteCrawler(
                    knowledge_index=agent.knowledge_index,
                    db_path=os.path.join(data_dir, "crawler.db"),
                )
                new_chunks = crawler.crawl(max_pages=200)
                if new_chunks > 0:
                    logger.info("NetSuite docs crawl added %d new chunks", new_chunks)
                    from agent.github_sync import sync_to_github
                    sync_to_github("Auto-crawled NetSuite documentation")
            except Exception as exc:
                logger.warning("Doc crawler failed (non-fatal): %s", exc)

        thread = threading.Thread(target=run_crawler, daemon=True, name="netsuite-crawler")
        thread.start()
        logger.info("NetSuite documentation crawler started in background")

    @app.on_event("shutdown")
    async def shutdown() -> None:
        logger.info("NS-AI-Agent API shutting down.")

    # ---------------------------------------------------------------------------
    # Health check
    # ---------------------------------------------------------------------------

    @app.get("/health", tags=["health"])
    async def health_check() -> dict:
        """Health check endpoint."""
        agent = getattr(app.state, "agent", None)
        kb_count = agent.knowledge_index.count() if agent else -1
        return {
            "status": "ok",
            "knowledge_base_documents": kb_count,
        }

    # ---------------------------------------------------------------------------
    # Routers
    # ---------------------------------------------------------------------------

    from api.routes.chat import router as chat_router
    from api.routes.knowledge import router as knowledge_router
    from api.routes.projects import router as projects_router

    app.include_router(chat_router)
    app.include_router(knowledge_router)
    app.include_router(projects_router)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("api.main:app", host=host, port=port, reload=True)
