"""soft-quarantine dirty limit-up pool rows

Revision ID: 016_limit_up_quarantine
Revises: 015_promotion_model_version_width
Create Date: 2026-08-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "016_limit_up_quarantine"
down_revision: Union[str, None] = "015_promotion_model_version_width"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "limit_up_pool" not in _tables():
        return
    columns = {
        row["name"]
        for row in sa.inspect(op.get_bind()).get_columns("limit_up_pool")
    }
    if "quarantined" in columns:
        return
    with op.batch_alter_table("limit_up_pool") as batch_op:
        batch_op.add_column(
            sa.Column(
                "quarantined",
                sa.Boolean(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.create_index(
            "ix_limit_up_pool_quarantined", ["quarantined"]
        )


def downgrade() -> None:
    if "limit_up_pool" not in _tables():
        return
    columns = {
        row["name"]
        for row in sa.inspect(op.get_bind()).get_columns("limit_up_pool")
    }
    if "quarantined" not in columns:
        return
    with op.batch_alter_table("limit_up_pool") as batch_op:
        batch_op.drop_index("ix_limit_up_pool_quarantined")
        batch_op.drop_column("quarantined")
