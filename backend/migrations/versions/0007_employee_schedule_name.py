"""Add the optional ``employees.schedule_name`` collab-schedule alias column.

The collaboration schedule (doc 49) distinguishes two same-named people as
``"Sneha Mondal"`` and ``"Sneha Mondal (Intern)"``, but both roster rows carry
the plain display name ``"Sneha Mondal"``. Without an alias the reminder job
matches the schedule by the display name, so both employees match the plain
rows and the intern rows match nobody.

This adds a nullable ``schedule_name`` alias: the reminder job matches the
schedule by ``schedule_name or name`` while the email/FCM greeting keeps the
DISPLAY ``name``. NULL means the schedule uses the display name verbatim.

Column name / type / nullability match the ORM model in
``app/models/employee.py`` exactly, so this migration and
``Base.metadata.create_all`` agree (no drift).

Revision ID: 0007_employee_schedule_name
Revises: 0006_collab_reminders
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_employee_schedule_name"
down_revision = "0006_collab_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employees",
        sa.Column("schedule_name", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("employees", "schedule_name")
