"""为牛股雷达/预案快照增加组合查询索引

Revision ID: 006_dashboard_snapshot_lookup_index
Revises: 005_anomaly_signal_performance
Create Date: 2026-08-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006_dashboard_snapshot_lookup_index"
down_revision: Union[str, None] = "005_anomaly_signal_performance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INDEX_NAME = "ix_dashboard_snapshot_lookup"


def upgrade() -> None:
    conn = op.get_bind()
    indexes = {
        row[1]
        for row in conn.execute(sa.text("PRAGMA index_list('dashboard_snapshot')"))
    }
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "dashboard_snapshot",
            ["snapshot_key", "trade_date", "status", "snapshot_time"],
            unique=False,
        )


def downgrade() -> None:
    conn = op.get_bind()
    indexes = {
        row[1]
        for row in conn.execute(sa.text("PRAGMA index_list('dashboard_snapshot')"))
    }
    if INDEX_NAME in indexes:
        op.drop_index(INDEX_NAME, table_name="dashboard_snapshot")
