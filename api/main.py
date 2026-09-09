"""FastAPI application entry point for NS-AI-Agent."""

from __future__ import annotations

import logging
import os
import threading

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


_init_lock = threading.Lock()
_MAX_INIT_BACKOFF = 300.0


def _initialize(app: FastAPI, attempt: int = 0) -> None:
    """Build the agent, retrying in the background until the database answers.

    A database that is briefly unreachable at boot (Railway's private DNS can
    lag container start) must not leave the service degraded until someone
    redeploys it, so failures reschedule themselves with backoff.
    """
    from agent.core import NSMigrationAgent
    from knowledge_base import db
    from knowledge_base.crawl_manager import CrawlManager

    with _init_lock:
        if getattr(app.state, "agent", None) is not None:
            return
        app.state.init_attempts = attempt + 1
        logger.info("Initializing NS-AI-Agent (attempt %d)...", attempt + 1)
        try:
            db.wait_for_database()
            agent = NSMigrationAgent()
        except Exception as exc:
            app.state.startup_error = f"{type(exc).__name__}: {exc}"
            delay = min(_MAX_INIT_BACKOFF, 20.0 * (2 ** attempt))
            logger.error(
                "Initialization failed (attempt %d): %s — retrying in %.0fs",
                attempt + 1, app.state.startup_error, delay,
                exc_info=(attempt == 0),
            )
            timer = threading.Timer(delay, _initialize, args=(app, attempt + 1))
            timer.daemon = True
            timer.start()
            return

        app.state.agent = agent
        app.state.catalog = agent.catalog
        app.state.crawl_manager = CrawlManager(agent.knowledge_index, agent.catalog)
        app.state.startup_error = None
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


_OPEN_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")


def _install_api_key_guard(app: FastAPI) -> None:
    """Require a shared key on every route when API_ACCESS_KEY is set.

    Without this the endpoints are open to anyone who knows the URL, and each
    /chat call spends Anthropic credits. Off by default so nothing breaks
    silently; set API_ACCESS_KEY to switch it on.
    """
    from fastapi.responses import JSONResponse

    @app.middleware("http")
    async def require_api_key(request, call_next):
        expected = os.getenv("API_ACCESS_KEY")
        path = request.url.path
        if expected and not path.startswith(_OPEN_PATHS) and request.method != "OPTIONS":
            supplied = request.headers.get("x-api-key") or ""
            # Compare in constant time so the key can't be guessed by timing.
            import hmac

            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "Missing or invalid X-API-Key"}, status_code=401)
        return await call_next(request)


def create_app() -> FastAPI:
    app = FastAPI(
        title="NS-AI-Agent API",
        description="NetSuite systems-expert agent REST API",
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    _install_api_key_guard(app)

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
        app.state.agent = None
        app.state.catalog = None
        app.state.crawl_manager = None
        app.state.startup_error = None
        app.state.init_attempts = 0
        # Initialize off the event loop so an unreachable database never stops
        # the port binding — /health must still be able to explain the problem.
        threading.Thread(
            target=_initialize, args=(app,), daemon=True, name="agent-init"
        ).start()

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
        initializing = agent is None and not startup_error
        return {
            "status": "starting" if initializing else ("degraded" if startup_error else "ok"),
            "knowledge_base_documents": kb_count,
            "catalog": catalog_stats,
            "startup_error": startup_error,
            "init_attempts": getattr(app.state, "init_attempts", 0),
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
