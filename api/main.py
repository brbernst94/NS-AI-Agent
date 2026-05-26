"""FastAPI application entry point for NS-AI-Agent."""

from __future__ import annotations

import logging
import os

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
        """Initialize the agent and knowledge base on startup."""
        import threading
        from agent.core import NSMigrationAgent
        from knowledge_base.crawler import NetSuiteCrawler

        # Default to no agent / no error so the health endpoint is always safe.
        app.state.agent = None
        app.state.startup_error = None

        logger.info("Initializing NS-AI-Agent...")
        try:
            agent = NSMigrationAgent()
            app.state.agent = agent
            logger.info(
                "NS-AI-Agent ready. Knowledge base: %d documents.",
                agent.knowledge_index.count(),
            )
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            app.state.startup_error = error_msg
            logger.error(
                "NS-AI-Agent failed to initialize — API will start in degraded mode. "
                "Error: %s",
                error_msg,
                exc_info=True,
            )
            # Do NOT re-raise: let the process keep running so Railway sees a
            # bound port and we can diagnose via /health instead of a 502.
            return

        # Crawl NetSuite docs in background so startup isn't blocked
        def run_crawler() -> None:
            try:
                crawler = NetSuiteCrawler(
                    knowledge_index=agent.knowledge_index,
                    db_path="/tmp/crawler.db",
                )
                new_chunks = crawler.crawl(max_pages=200)
                if new_chunks > 0:
                    logger.info("NetSuite docs crawl added %d new chunks", new_chunks)
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
        startup_error = getattr(app.state, "startup_error", None)
        if agent is not None:
            try:
                kb_count = agent.knowledge_index.count()
            except Exception as exc:
                kb_count = -1
                if startup_error is None:
                    startup_error = f"knowledge_index.count() failed: {exc}"
        else:
            kb_count = -1
        return {
            "status": "degraded" if startup_error else "ok",
            "knowledge_base_documents": kb_count,
            "startup_error": startup_error,
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
