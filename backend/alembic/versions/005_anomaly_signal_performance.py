"""扩展异动信号绩效评估字段

Revision ID: 005_anomaly_signal_performance
Revises: 004_promotion_prediction_record
Create Date: 2026-07-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005_anomaly_signal_performance"
down_revision: Union[str, None] = "004_promotion_prediction_record"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    sa.Column("signal_variant", sa.String(40), nullable=True),
    sa.Column("setup_grade", sa.String(30), nullable=True),
    sa.Column("net_return_1d", sa.Float(), nullable=True),
    sa.Column("net_return_3d", sa.Float(), nullable=True),
    sa.Column("net_return_5d", sa.Float(), nullable=True),
    sa.Column("net_return_10d", sa.Float(), nullable=True),
    sa.Column("benchmark_return_1d", sa.Float(), nullable=True),
    sa.Column("benchmark_return_3d", sa.Float(), nullable=True),
    sa.Column("benchmark_return_5d", sa.Float(), nullable=True),
    sa.Column("benchmark_return_10d", sa.Float(), nullable=True),
    sa.Column("excess_return_1d", sa.Float(), nullable=True),
    sa.Column("excess_return_3d", sa.Float(), nullable=True),
    sa.Column("excess_return_5d", sa.Float(), nullable=True),
    sa.Column("excess_return_10d", sa.Float(), nullable=True),
    sa.Column("evaluation_version", sa.String(20), nullable=True),
)


def upgrade() -> None:
    conn = op.get_bind()
    existing = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(signal_performance)"))}
    for column in _COLUMNS:
        if column.name not in existing:
            op.add_column("signal_performance", column)


def downgrade() -> None:
    conn = op.get_bind()
    existing = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(signal_performance)"))}
    for column in reversed(_COLUMNS):
        if column.name in existing:
            op.drop_column("signal_performance", column.name)
