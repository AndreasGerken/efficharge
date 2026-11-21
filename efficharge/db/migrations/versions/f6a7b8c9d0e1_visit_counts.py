"""anonymous visit counts

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-21 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "visit_counts",
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("page", sa.String(length=64), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("visitors", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day", "page", name=op.f("pk_visit_counts")),
    )


def downgrade() -> None:
    op.drop_table("visit_counts")
