"""priority per user instead of per plug

Revision ID: c3d4e5f6a7b8
Revises: a1f3c2d4e5b6
Create Date: 2026-11-06 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "a1f3c2d4e5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("price_weight", sa.Float(), nullable=False, server_default="0.5")
        )
    # keep what the user had chosen (on their first plug)
    op.execute(
        """
        UPDATE users SET price_weight = (
            SELECT s.price_weight FROM device_settings s JOIN devices d ON d.id = s.device_id
            WHERE d.user_id = users.id ORDER BY d.id LIMIT 1
        )
        WHERE EXISTS (SELECT 1 FROM devices d WHERE d.user_id = users.id)
        """
    )
    with op.batch_alter_table("device_settings", schema=None) as batch_op:
        batch_op.drop_column("price_weight")


def downgrade() -> None:
    with op.batch_alter_table("device_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("price_weight", sa.Float(), nullable=False, server_default="0.5")
        )
    op.execute(
        "UPDATE device_settings SET price_weight = (SELECT u.price_weight FROM users u "
        "JOIN devices d ON d.user_id = u.id WHERE d.id = device_settings.device_id)"
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("price_weight")
