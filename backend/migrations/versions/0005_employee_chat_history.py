"""Employee-Zone chat history schema (employee_conversations + employee_messages).

Adds two tables backing per-employee chat history, kept SEPARATE from the
admin/user ``conversations``/``messages`` tables so Employee-Zone threads are
owned by a string ``employee_id`` and can evolve independently:

- ``employee_conversations`` — common mixin fields (id BIGSERIAL PK, status
  SMALLINT default 1, added_time / last_modified timestamptz default now()) plus
  ``employee_id`` (owner, indexed), ``employee_email``, ``employee_name``,
  ``title`` and ``message_count`` (INT default 0). A composite index
  ``(employee_id, last_modified, id)`` serves the per-employee newest-first list.
- ``employee_messages`` — common mixin fields plus ``conversation_id`` (BIGINT FK
  -> employee_conversations.id, indexed), ``employee_id`` (denormalised, indexed),
  ``role``, ``content`` and a nullable ``sources`` JSONB column, with a
  ``(conversation_id, id)`` order index.

Column names / types / nullability / server defaults match the ORM models in
``app/models/employee_conversation.py`` and ``app/models/employee_message.py``
exactly, so this migration and ``Base.metadata.create_all`` agree (no drift).

Revision ID: 0005_employee_chat_history
Revises: 0004_multitenant
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision = "0005_employee_chat_history"
down_revision = "0004_multitenant"
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
    # 1. employee_conversations ---------------------------------------------
    op.create_table(
        "employee_conversations",
        *_mixin_columns(),
        sa.Column("employee_id", sa.String(length=255), nullable=False),
        sa.Column("employee_email", sa.String(length=320), nullable=True),
        sa.Column("employee_name", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_employee_conversations_employee_id", "employee_conversations", ["employee_id"]
    )
    op.create_index(
        "ix_employee_conversations_owner_recent",
        "employee_conversations",
        ["employee_id", "last_modified", "id"],
    )

    # 2. employee_messages ---------------------------------------------------
    op.create_table(
        "employee_messages",
        *_mixin_columns(),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sources", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["employee_conversations.id"],
            name="fk_employee_messages_conversation_id",
        ),
    )
    op.create_index(
        "ix_employee_messages_conversation_id", "employee_messages", ["conversation_id"]
    )
    op.create_index(
        "ix_employee_messages_employee_id", "employee_messages", ["employee_id"]
    )
    op.create_index(
        "ix_employee_messages_conv_order", "employee_messages", ["conversation_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_employee_messages_conv_order", table_name="employee_messages")
    op.drop_index("ix_employee_messages_employee_id", table_name="employee_messages")
    op.drop_index("ix_employee_messages_conversation_id", table_name="employee_messages")
    op.drop_table("employee_messages")
    op.drop_index(
        "ix_employee_conversations_owner_recent", table_name="employee_conversations"
    )
    op.drop_index(
        "ix_employee_conversations_employee_id", table_name="employee_conversations"
    )
    op.drop_table("employee_conversations")
