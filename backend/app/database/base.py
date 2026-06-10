"""SQLAlchemy declarative base, status enum, and common-field mixin.

Every table reuses :class:`Base` plus :class:`TimestampStatusMixin` so that the
common fields (``id``, ``status``, ``added_time``, ``last_modified``) are defined
once for every PostgreSQL table.
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum

from sqlalchemy import BigInteger, DateTime, SmallInteger, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Status(IntEnum):
    """Soft-delete / lifecycle status for every row."""

    INACTIVE = 0
    ACTIVE = 1
    DELETED = 5


class TimestampStatusMixin:
    """Reusable common columns shared by every table.

    - ``id``: BIGINT primary key, autoincrement (BIGSERIAL on PostgreSQL).
    - ``status``: SMALLINT, NOT NULL, default ``ACTIVE`` (1).
    - ``added_time``: ``timestamptz``, NOT NULL, default ``now()``.
    - ``last_modified``: ``timestamptz``, NOT NULL, default ``now()``,
      auto-updated on modification at the ORM layer via ``onupdate``.
    """

    # Plain BigInteger PK; with autoincrement this emits BIGSERIAL on PostgreSQL.
    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    status: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=Status.ACTIVE.value,
        server_default=str(Status.ACTIVE.value),
    )
    added_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    last_modified: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )
