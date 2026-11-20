"""rules: "ready within N h" rules and the fallback rule

Revision ID: a1f3c2d4e5b6
Revises: 5bb66851efec
Create Date: 2026-11-06 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1f3c2d4e5b6"
down_revision: str | None = "5bb66851efec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("deadline_rules", schema=None) as batch_op:
        batch_op.add_column(sa.Column("within_min", sa.Integer(), nullable=True))
        batch_op.alter_column("ready_by", existing_type=sa.String(length=5), nullable=True)
    with op.batch_alter_table("device_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("fallback_within_min", sa.Integer(), nullable=False, server_default="720")
        )


def downgrade() -> None:
    with op.batch_alter_table("device_settings", schema=None) as batch_op:
        batch_op.drop_column("fallback_within_min")
    op.execute("DELETE FROM deadline_rules WHERE ready_by IS NULL")
    with op.batch_alter_table("deadline_rules", schema=None) as batch_op:
        batch_op.alter_column("ready_by", existing_type=sa.String(length=5), nullable=False)
        batch_op.drop_column("within_min")
