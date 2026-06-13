"""
FastAPI application entry point.

Startup sequence:
1. Validate all settings (pydantic-settings raises if required vars are missing).
2. Register routers.
3. Set up structured logging.
"""

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import invoices
from app.core.config import settings

# Configure structlog for JSON output in production, pretty-print in dev.
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
        if settings.environment == "production"
        else structlog.dev.ConsoleRenderer(),
    ]
)

log = structlog.get_logger()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Invoice Automation Pipeline",
        description=(
            "Event-driven invoice processing: ingest → extract → decide → "
            "approve → pay → audit."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tighten in production
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ────────────────────────────────────────────────────────
    app.include_router(invoices.router)

    from app.api import slack
    app.include_router(slack.router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "environment": settings.environment}

    @app.on_event("startup")
    async def on_startup():
        from app.core.tracing import configure_tracing
        configure_tracing()
        log.info("app_starting", environment=settings.environment, llm_model=settings.llm_model)

    return app


app = create_app()
