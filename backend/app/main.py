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
    """Application lifespan: initialize the database on startup."""
    logger.info("Starting RAG Knowledge Base API")
    init_db()
    yield


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

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    # --- Router extension point -------------------------------------------
    # Feature routers are wired here as the modules become available. Search,
    # chat, and admin routers are added in later phases.
    from app.api import admin, auth, chat, documents, search

    app.include_router(auth.router)
    # Documents: public router (anonymous upload) + admin router (management).
    app.include_router(documents.public_router)
    app.include_router(documents.admin_router)
    app.include_router(search.router)
    app.include_router(chat.router)
    app.include_router(admin.router)

    return app


app = create_app()
