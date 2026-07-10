"""Add the optional ``documents.external_ref`` stable-identity column.

The HR-admin portal's handbook-sync bridge (POST /internal/sync-global-doc)
pushes the HR Handbook PDF into the RAG global knowledge base whenever HR
updates it. To keep exactly one live copy — the system already suffers from
duplicate-PDF indexing polluting answers — the bridge tags the global doc with
a stable ``external_ref`` (e.g. ``"hr_handbook"``) so a re-upload can locate and
soft-delete the prior copy before indexing the new one.

Nullable: ordinary user/admin uploads leave it NULL. Indexed because the sync
endpoint looks up active globals by ``external_ref``.

Column name / type / nullability match the ORM model in
``app/models/document.py`` exactly, so this migration and
``Base.metadata.create_all`` agree (no drift).

Revision ID: 0008_document_external_ref
Revises: 0007_employee_schedule_name
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_document_external_ref"
down_revision = "0007_employee_schedule_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("external_ref", sa.String(length=128), nullable=True),
    )
    op.create_index("ix_documents_external_ref", "documents", ["external_ref"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_documents_external_ref", table_name="documents")
    op.drop_column("documents", "external_ref")
