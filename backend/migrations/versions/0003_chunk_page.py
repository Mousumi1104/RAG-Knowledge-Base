"""Add per-chunk source page tracking.

Adds a nullable ``page`` integer column to the ``chunks`` table holding the
1-based source page (e.g. PDF page) a chunk was extracted from. Nullable so
pre-existing rows (indexed before page tracking) and page-less formats
(txt/md/docx) keep ``NULL``; a later phase reindexes to backfill PDF pages.

Column name / type / nullability match ``Chunk.page`` in
``app/models/chunk.py`` exactly, so this migration and
``Base.metadata.create_all`` agree (no drift).

Revision ID: 0003_chunk_page
Revises: 0002_conversations_messages
Create Date: 2026-06-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_chunk_page"
down_revision = "0002_conversations_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("page", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "page")
