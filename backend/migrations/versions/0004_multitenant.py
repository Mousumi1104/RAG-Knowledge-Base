"""Multi-tenant data isolation: users table + document/conversation ownership.

Converts the single-admin app into a multi-tenant app:

- enables the ``citext`` extension (must precede the CITEXT ``email`` column,
  same ordering pattern as ``0001``'s ``vector`` extension),
- creates ``users`` with the common mixin fields (id BIGSERIAL PK, status
  SMALLINT default 1, added_time / last_modified timestamptz default now()) plus
  ``email`` (CITEXT UNIQUE), ``password_hash``, ``role`` (server_default 'user',
  CHECK role IN ('user','admin')), ``is_active`` (server_default true),
- seeds one admin ``User`` from ``settings.ADMIN_EMAIL`` / ``ADMIN_PASSWORD``
  (bcrypt-hashed via passlib inside the migration),
- adds ``documents.owner_id`` (nullable FK -> users.id, indexed) and
  ``documents.is_global`` (Boolean, server_default false), then backfills every
  pre-existing doc to ``is_global = true, owner_id = NULL`` (admin-global),
- adds ``conversations.user_id`` (nullable first, backfilled to the seeded admin,
  then altered to NOT NULL) with FK -> users.id and an index.

Column names / types / nullability / server defaults match the ORM models in
``app/models/user.py``, ``app/models/document.py`` and
``app/models/conversation.py`` exactly, so this migration and
``Base.metadata.create_all`` agree (no drift).

Revision ID: 0004_multitenant
Revises: 0003_chunk_page
Create Date: 2026-06-10
"""

from __future__ import annotations

import bcrypt
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT

from app.core.config import settings

# revision identifiers, used by Alembic.
revision = "0004_multitenant"
down_revision = "0003_chunk_page"
branch_labels = None
depends_on = None


def _hash_password(password: str) -> str:
    """bcrypt-hash a password (used to seed the admin user).

    Uses the ``bcrypt`` library directly rather than passlib's bcrypt backend,
    which is incompatible with bcrypt >= 5.x. bcrypt truncates secrets to 72
    bytes; encode to UTF-8 first so the stored hash is deterministic.
    """
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def _admin_email() -> str:
    """Resolve the seeded admin login email.

    Use ADMIN_EMAIL if set; otherwise derive from ADMIN_USERNAME (verbatim if it
    already looks like an email, else ``<username>@local``).
    """
    email = (settings.ADMIN_EMAIL or "").strip()
    if email:
        return email
    username = (settings.ADMIN_USERNAME or "admin").strip()
    return username if "@" in username else f"{username}@local"


def upgrade() -> None:
    # 1. CITEXT extension MUST exist before the citext email column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # 2. users ---------------------------------------------------------------
    op.create_table(
        "users",
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
        # --- user-specific fields ---
        sa.Column("email", CITEXT(), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            server_default="user",
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.CheckConstraint(
            "role IN ('user', 'admin')",
            name="ck_users_role",
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # 3. Seed the admin user. Capture its id (RETURNING) for the conversations
    #    backfill below. Password is bcrypt-hashed here (migration may import
    #    app code; env.py already imports app.*).
    admin_email = _admin_email()
    admin_hash = _hash_password(settings.ADMIN_PASSWORD)
    conn = op.get_bind()
    admin_id = conn.execute(
        sa.text(
            "INSERT INTO users (email, password_hash, role, is_active) "
            "VALUES (:email, :pw, 'admin', true) RETURNING id"
        ),
        {"email": admin_email, "pw": admin_hash},
    ).scalar_one()

    # 4. documents: owner_id (nullable FK + index) + is_global (server_default false).
    op.add_column(
        "documents",
        sa.Column("owner_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column(
            "is_global",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_foreign_key(
        "fk_documents_owner_id_users",
        "documents",
        "users",
        ["owner_id"],
        ["id"],
    )
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"])

    # 5. Backfill: every pre-existing doc becomes admin-global (per decision).
    op.execute("UPDATE documents SET is_global = true, owner_id = NULL")

    # 6. conversations: user_id NULLABLE first, backfill to admin, then NOT NULL.
    op.add_column(
        "conversations",
        sa.Column("user_id", sa.BigInteger(), nullable=True),
    )
    op.execute(
        sa.text("UPDATE conversations SET user_id = :admin_id").bindparams(
            admin_id=admin_id
        )
    )
    op.alter_column("conversations", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_conversations_user_id_users",
        "conversations",
        "users",
        ["user_id"],
        ["id"],
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])


def downgrade() -> None:
    # Reverse order: conversations.user_id, documents columns, users table.
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_constraint(
        "fk_conversations_user_id_users", "conversations", type_="foreignkey"
    )
    op.drop_column("conversations", "user_id")

    op.drop_index("ix_documents_owner_id", table_name="documents")
    op.drop_constraint(
        "fk_documents_owner_id_users", "documents", type_="foreignkey"
    )
    op.drop_column("documents", "is_global")
    op.drop_column("documents", "owner_id")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")

    # The ``citext`` extension is intentionally NOT dropped (mirrors 0001 leaving
    # ``vector`` in place): other objects or databases may depend on it.
