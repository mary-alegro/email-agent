"""Service entry point: the REST API with the ERP MCP server mounted at /mcp.

Development:  make run    (uvicorn with --reload)
Service:      make serve  (see README, "Run as a standalone service")
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastmcp.utilities.lifespan import combine_lifespans

from app.api import emails, lead_times
from app.api.errors import register_error_handlers
from app.middleware import RequestLoggingMiddleware
from src.config import settings
from src.database.engine import engine, init_db
from src.erp.mcp_server import mcp
from src.utils.logging import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def app_lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db(engine)
    log.info(
        "Service started",
        extra={
            # Which database is in use (password masked, for non-SQLite URLs).
            "database": engine.url.render_as_string(hide_password=True),
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model or "default",
            "erp_mcp": settings.erp_mcp_url or "in-process",
        },
    )
    yield
    log.info("Service stopped")


def create_app() -> FastAPI:
    setup_logging(settings.log_level, settings.log_format)
    # Stateless: no per-client MCP session lives in server memory, so any uvicorn worker
    # (or replica behind a load balancer) can serve any request. The tools don't need
    # session state; each call opens its own DB session.
    mcp_app = mcp.http_app(path="/mcp", stateless_http=True)

    # The MCP app has its own lifespan (session manager); it must run alongside ours.
    app = FastAPI(
        title="Supply Chain Email Agent",
        lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan),
    )
    register_error_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(emails.router)
    app.include_router(lead_times.router)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Mounted at the root because mcp_app already serves at /mcp. Mount last, so the
    # REST routes above take precedence.
    app.mount("/", mcp_app)
    return app


app = create_app()
