"""registrations wait for approval; "who are you" text

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-13 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import efficharge.db.models

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("approved_at", efficharge.db.models.UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("about", sa.Text(), nullable=True))
    # everyone who exists already was added by the admin
    op.execute("UPDATE users SET approved_at = created_at")


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("about")
        batch_op.drop_column("approved_at")
