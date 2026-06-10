"""Persisted chat history schema (conversations + messages).

Adds the two tables backing persisted chat conversations:

- ``conversations`` with the common mixin fields (id BIGSERIAL PK, status
  SMALLINT default 1, added_time / last_modified timestamptz default now()) plus
  a nullable ``title``,
- ``messages`` with the common mixin fields plus ``conversation_id`` (BIGINT FK
  -> conversations.id, indexed), ``role`` ('user'|'assistant'), ``content`` and a
  nullable ``sources`` JSONB column.

Column names / types / nullability / server defaults match the ORM models in
``app/models/conversation.py`` and ``app/models/message.py`` exactly, so this
migration and ``Base.metadata.create_all`` agree (no drift).

Revision ID: 0002_conversations_messages
Revises: 0001_initial_pg
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0002_conversations_messages"
down_revision = "0001_initial_pg"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. conversations -------------------------------------------------------
    op.create_table(
        "conversations",
        # --- common TimestampStatusMixin fields ---
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
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
        # --- conversation-specific fields ---
        sa.Column("title", sa.String(length=512), nullable=True),
    )
    # Helpful index for active-only reads (services filter on status == ACTIVE).
    op.create_index("ix_conversations_status", "conversations", ["status"])

    # 2. messages ------------------------------------------------------------
    op.create_table(
        "messages",
        # --- common TimestampStatusMixin fields ---
        sa.Column(
            "id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
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
        # --- message-specific fields ---
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_messages_conversation_id_conversations",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_messages_role",
        ),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index("ix_messages_status", "messages", ["status"])


def downgrade() -> None:
    op.drop_index("ix_messages_status", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_conversations_status", table_name="conversations")
    op.drop_table("conversations")
