"""Database engine, session factory, and FastAPI dependencies.

The engine is built from ``settings.DATABASE_URL`` and targets PostgreSQL via the
psycopg v3 driver (``postgresql+psycopg://``). ``pool_pre_ping`` recycles dead
connections so a restarted userspace Postgres does not leave stale pooled handles.
"""

from __future__ import annotations

import importlib
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.database.base import Base
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _build_engine() -> Engine:
    """Create the SQLAlchemy engine for PostgreSQL (psycopg v3)."""
    return create_engine(
        settings.DATABASE_URL,  # postgresql+psycopg://...
        pool_pre_ping=True,  # recycle dead connections (userspace PG restarts)
        future=True,
    )


engine: Engine = _build_engine()

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    future=True,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables for the registered models.

    Imports the models package defensively so that table classes register on
    ``Base.metadata`` before ``create_all`` runs. The models package may be empty
    in early phases; this must not break startup.

    NOTE: ``create_all`` is a dev convenience only. It cannot create the ``vector``
    extension or the pgvector ANN index — Alembic owns the real schema, so
    production should run ``alembic upgrade head`` instead of relying on this.
    """
    try:
        importlib.import_module("app.models")
    except Exception:  # noqa: BLE001 - models package may be empty/partial early on
        logger.warning("app.models could not be imported; continuing without ORM models")

    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized (tables created if missing)")
