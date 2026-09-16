"""forward daily fundamental snapshots

Revision ID: 017_fundamental_daily_snapshot
Revises: 016_limit_up_quarantine
Create Date: 2026-08-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "017_fundamental_daily_snapshot"
down_revision: Union[str, None] = "016_limit_up_quarantine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "stock_fundamental_daily" in _tables():
        return
    op.create_table(
        "stock_fundamental_daily",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("name", sa.String(20)),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("pe_ttm", sa.Float()),
        sa.Column("pb", sa.Float()),
        sa.Column("net_profit_growth", sa.Float()),
        sa.Column("circ_market_cap", sa.Float()),
        sa.Column("source", sa.String(20), server_default="tencent_spot"),
        sa.Column("created_at", sa.DateTime()),
        sa.UniqueConstraint("code", "trade_date", name="uq_fundamental_daily_code_date"),
    )
    op.create_index("ix_fundamental_daily_date", "stock_fundamental_daily", ["trade_date"])
    op.create_index("ix_fundamental_daily_code", "stock_fundamental_daily", ["code"])


def downgrade() -> None:
    if "stock_fundamental_daily" in _tables():
        op.drop_table("stock_fundamental_daily")
