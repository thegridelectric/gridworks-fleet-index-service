"""auth_events: instance_id and run nullable, for a malformed request

Revision ID: c4d9e2f7a1b3
Revises: a7c3e1f9b2d4
Create Date: 2026-10-08 19:20:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d9e2f7a1b3"
down_revision: str | Sequence[str] | None = "a7c3e1f9b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "auth_events", "instance_id", existing_type=sa.String(), nullable=True
    )
    op.alter_column("auth_events", "run", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM auth_events WHERE instance_id IS NULL OR run IS NULL")
    op.alter_column("auth_events", "run", existing_type=sa.String(), nullable=False)
    op.alter_column(
        "auth_events", "instance_id", existing_type=sa.String(), nullable=False
    )
