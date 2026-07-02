"""FastAPI application factory.

Exposes ``create_app()`` and a module-level ``app`` so ``uvicorn app.main:app``
works from the ``backend/`` directory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.database.session import init_db
from app.utils.errors import register_exception_handlers
from app.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: initialize the database on startup.

    Also sweeps documents left in ``processing`` by an OOM-killed worker so they
    don't stay stuck forever (the SIGKILL can't be caught to mark them failed).
    """
    logger.info("Starting RAG Knowledge Base API")
    init_db()
    _seed_admin_user()
    _recover_stale_processing()
    yield


def _seed_admin_user() -> None:
    """Idempotently upsert the configured admin user (best-effort, self-healing).

    Runs on every boot so the live DB repairs itself on redeploy: the admin row
    is created if missing and otherwise reconciled (password re-hashed from
    ADMIN_PASSWORD, role forced to ``admin``, re-activated). A failure here must
    never crash boot (e.g. DB temporarily unavailable).
    """
    try:
        from app.database.session import SessionLocal
        from app.services.user_service import seed_admin_user

        session = SessionLocal()
        try:
            seed_admin_user(session)
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - admin seed must never crash boot
        logger.warning("Admin seed skipped: %s", exc)


def _recover_stale_processing() -> None:
    """Mark OOM-orphaned ``processing`` documents as failed (best-effort)."""
    try:
        from app.database.session import SessionLocal
        from app.services.indexing_service import fail_stale_processing

        session = SessionLocal()
        try:
            fail_stale_processing(session)
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - startup recovery must never crash boot
        logger.warning("Stale-processing recovery skipped: %s", exc)


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    configure_logging()

    app = FastAPI(
        title="RAG Knowledge Base API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    @app.api_route("/", methods=["GET", "HEAD"], tags=["health"])
    def root() -> dict[str, str]:
        """Root landing/liveness route (also satisfies uptime monitors hitting `/`)."""
        return {
            "status": "ok",
            "service": "RAG Knowledge Base API",
            "docs": "/docs",
            "health": "/health",
        }

    @app.api_route("/health", methods=["GET", "HEAD"], tags=["health"])
    def health() -> dict[str, str]:
        """Liveness probe (GET and HEAD; HEAD is the default for many uptime monitors)."""
        return {"status": "ok"}

    # --- Router extension point -------------------------------------------
    # Feature routers are wired here as the modules become available. Search,
    # chat, and admin routers are added in later phases.
    from app.api import admin, auth, chat, device, documents, employee, internal, search

    app.include_router(auth.router)
    # Documents: public router (anonymous upload) + admin router (management).
    app.include_router(documents.public_router)
    app.include_router(documents.admin_router)
    app.include_router(search.router)
    app.include_router(chat.router)
    app.include_router(employee.router)
    app.include_router(device.router)
    app.include_router(admin.router)
    # Internal, secret-gated ops endpoints (e.g. GitHub Actions cron trigger).
    app.include_router(internal.router)

    return app


app = create_app()
