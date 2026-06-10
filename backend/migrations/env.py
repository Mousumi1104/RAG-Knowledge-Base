"""Alembic migration environment.

Wired to ``Base.metadata`` with all models imported (Document + Chunk). The
database URL is resolved from the ``DATABASE_URL`` env var (override) or
``settings.DATABASE_URL`` so migrations target the same PostgreSQL DB as the
app. ``pgvector.sqlalchemy`` is imported so the ``Vector`` type is registered
for autogenerate / type round-trips.
"""

from __future__ import annotations

import importlib
import os
from logging.config import fileConfig

import pgvector.sqlalchemy  # noqa: F401 - registers the Vector type for round-trips
from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.database.base import Base

# Import all models so their tables register on Base.metadata
# (Document + Chunk), giving autogenerate/create_all the full schema.
importlib.import_module("app.models")

# Resolve the database URL: an explicit DATABASE_URL env var wins so CI / tests
# can point at ``ragdb_test``; otherwise fall back to the configured default.
DATABASE_URL = os.environ.get("DATABASE_URL", settings.DATABASE_URL)

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a DBAPI connection)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (against a live connection)."""
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
