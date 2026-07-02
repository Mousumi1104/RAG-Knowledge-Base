"""Collab-reminder feature schema (device_tokens, collaboration_days, reminders_sent, employees).

Adds four tables backing the Employee-Zone collaboration-day reminder feature.
Every table reuses the shared ``TimestampStatusMixin`` columns (id BIGSERIAL PK,
status SMALLINT default 1, added_time / last_modified timestamptz default now())
plus its feature columns:

- ``device_tokens`` — one active push token per owner: ``owner`` (UNIQUE owner
  key, X-Employee-Id else email), ``token`` (Text), nullable ``platform`` and an
  ``updated_at`` timestamptz (server_default now()). Logout sets status=DELETED.
- ``collaboration_days`` — a scheduled on-site day: ``employee_name`` (indexed),
  ``date``, nullable ``weekday`` / ``ws`` / ``room`` and a composite index
  ``(employee_name, date)``.
- ``reminders_sent`` — dedup ledger: ``owner``, ``collab_date``, nullable
  ``channel`` and a ``(owner, collab_date)`` UNIQUE constraint.
- ``employees`` — directory: ``name`` (indexed, matches
  collaboration_days.employee_name), nullable ``email`` and a UNIQUE
  ``employee_id`` owner key. Ships UNSEEDED.

Column names / types / nullability / server defaults match the ORM models in
``app/models/device_token.py``, ``app/models/collaboration_day.py``,
``app/models/reminder_sent.py`` and ``app/models/employee.py`` exactly, so this
migration and ``Base.metadata.create_all`` agree (no drift).

Revision ID: 0006_collab_reminders
Revises: 0005_employee_chat_history
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_collab_reminders"
down_revision = "0005_employee_chat_history"
branch_labels = None
depends_on = None


def _mixin_columns() -> list[sa.Column]:
    """The shared TimestampStatusMixin columns, matching every other table."""
    return [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "added_time",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_modified",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    ]


def upgrade() -> None:
    # 1. device_tokens -------------------------------------------------------
    op.create_table(
        "device_tokens",
        *_mixin_columns(),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("owner", name="uq_device_tokens_owner"),
    )

    # 2. collaboration_days --------------------------------------------------
    op.create_table(
        "collaboration_days",
        *_mixin_columns(),
        sa.Column("employee_name", sa.String(length=255), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("weekday", sa.String(length=16), nullable=True),
        sa.Column("ws", sa.String(length=32), nullable=True),
        sa.Column("room", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_collaboration_days_employee_name", "collaboration_days", ["employee_name"]
    )
    op.create_index(
        "ix_collaboration_days_name_date",
        "collaboration_days",
        ["employee_name", "date"],
    )

    # 3. reminders_sent ------------------------------------------------------
    op.create_table(
        "reminders_sent",
        *_mixin_columns(),
        sa.Column("owner", sa.String(length=255), nullable=False),
        sa.Column("collab_date", sa.Date(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=True),
        sa.UniqueConstraint(
            "owner", "collab_date", name="uq_reminders_sent_owner_date"
        ),
    )

    # 4. employees -----------------------------------------------------------
    op.create_table(
        "employees",
        *_mixin_columns(),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("employee_id", sa.String(length=255), nullable=False),
        sa.UniqueConstraint("employee_id", name="uq_employees_employee_id"),
    )
    op.create_index("ix_employees_name", "employees", ["name"])


def downgrade() -> None:
    op.drop_index("ix_employees_name", table_name="employees")
    op.drop_table("employees")

    op.drop_table("reminders_sent")

    op.drop_index("ix_collaboration_days_name_date", table_name="collaboration_days")
    op.drop_index(
        "ix_collaboration_days_employee_name", table_name="collaboration_days"
    )
    op.drop_table("collaboration_days")

    op.drop_table("device_tokens")
