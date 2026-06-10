"""Initial PostgreSQL schema (documents + chunks) with pgvector.

Provisions the full schema on PostgreSQL:

- enables the ``vector`` extension (must precede the embedding column),
- creates ``documents`` and ``chunks`` with the common mixin fields
  (id BIGSERIAL PK, status SMALLINT default 1, added_time / last_modified
  timestamptz default now()),
- stores the chunk embedding as a pgvector ``Vector(384)`` column
  (dimension == ``settings.EMBEDDING_DIM``; hardcoded here because migrations
  are point-in-time / immutable),
- builds an HNSW ANN index on ``chunks.embedding`` with ``vector_cosine_ops``.

Column names / types / nullability / server defaults match the ORM models in
``app/models/document.py`` and ``app/models/chunk.py`` exactly, so this
migration and ``Base.metadata.create_all`` agree (no drift).

Revision ID: 0001_initial_pg
Revises:
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision = "0001_initial_pg"
down_revision = None
branch_labels = None
depends_on = None

# Embedding dimension. MUST equal settings.EMBEDDING_DIM (384). Hardcoded
# because a migration is an immutable point-in-time artifact; changing
# EMBEDDING_DIM requires a new migration (ALTER ... TYPE vector(M) + reindex).
EMBEDDING_DIM = 384


def upgrade() -> None:
    # 1. The pgvector extension MUST exist before the Vector column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 2. documents -----------------------------------------------------------
    op.create_table(
        "documents",
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
        # --- document-specific fields ---
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("document_type", sa.String(length=16), nullable=False),
        sa.Column(
            "uploaded_by",
            sa.String(length=255),
            nullable=True,
            server_default="admin",
        ),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column(
            "indexing_status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "chunk_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("error", sa.Text(), nullable=True),
    )
    # Helpful index for active-only reads (services filter on status == ACTIVE).
    op.create_index("ix_documents_status", "documents", ["status"])

    # 3. chunks --------------------------------------------------------------
    op.create_table(
        "chunks",
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
        # --- chunk-specific fields ---
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_id", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        # Embedding co-located on the chunk row; dimension == EMBEDDING_DIM.
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_chunks_document_id_documents",
        ),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_chunk_id", "chunks", ["chunk_id"], unique=True)
    op.create_index("ix_chunks_status", "chunks", ["status"])

    # 4. ANN index for cosine similarity search (matches the `<=>` operator).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw "
        "ON chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.drop_index("ix_chunks_status", table_name="chunks")
    op.drop_index("ix_chunks_chunk_id", table_name="chunks")
    op.drop_index("ix_chunks_document_id", table_name="chunks")
    op.drop_table("chunks")

    op.drop_index("ix_documents_status", table_name="documents")
    op.drop_table("documents")

    # The ``vector`` extension is intentionally NOT dropped: other objects or
    # databases may depend on it.
