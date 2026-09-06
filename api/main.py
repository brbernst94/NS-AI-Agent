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
    app = FastAPI(
        title="NS-AI-Agent API",
        description="NetSuite systems-expert agent REST API",
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

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
        from agent.core import NSMigrationAgent
        from knowledge_base.crawl_manager import CrawlManager

        app.state.agent = None
        app.state.catalog = None
        app.state.crawl_manager = None
        app.state.startup_error = None

        logger.info("Initializing NS-AI-Agent...")
        try:
            agent = NSMigrationAgent()
        except Exception as exc:
            app.state.startup_error = f"{type(exc).__name__}: {exc}"
            logger.error(
                "NS-AI-Agent failed to initialize — API running in degraded mode: %s",
                app.state.startup_error, exc_info=True,
            )
            # Keep the process alive so Railway sees a bound port and /health explains why.
            return

        app.state.agent = agent
        app.state.catalog = agent.catalog
        app.state.crawl_manager = CrawlManager(agent.knowledge_index, agent.catalog)
        logger.info(
            "NS-AI-Agent ready. Knowledge base: %d chunks. Catalog: %s",
            agent.knowledge_index.count(), agent.catalog.stats(),
        )

        try:
            from knowledge_base import crawl_health

            crawl_health.record_snapshot("startup", force=True)
        except Exception as exc:
            logger.warning("Could not record startup snapshot: %s", exc)

        if os.getenv("CRAWL_ON_STARTUP", "1") != "0":
            result = app.state.crawl_manager.start("all")
            logger.info("Startup crawl: %s", result.get("targets") or result.get("reason"))

    @app.on_event("shutdown")
    async def shutdown() -> None:
        logger.info("NS-AI-Agent API shutting down.")

    # ---------------------------------------------------------------------------
    # Health
    # ---------------------------------------------------------------------------

    @app.get("/health", tags=["health"])
    async def health_check() -> dict:
        agent = getattr(app.state, "agent", None)
        startup_error = getattr(app.state, "startup_error", None)
        kb_count = -1
        catalog_stats = None
        if agent is not None:
            try:
                kb_count = agent.knowledge_index.count()
                catalog_stats = agent.catalog.stats()
            except Exception as exc:
                startup_error = startup_error or f"database check failed: {exc}"
        return {
            "status": "degraded" if startup_error else "ok",
            "knowledge_base_documents": kb_count,
            "catalog": catalog_stats,
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
