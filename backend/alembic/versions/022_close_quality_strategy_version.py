"""add close-quality provenance and strategy versions

Revision ID: 022_close_quality_strategy
Revises: 021_point_time_candidate_sector
Create Date: 2026-09-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "022_close_quality_strategy"
down_revision: Union[str, None] = "021_point_time_candidate_sector"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {str(item["name"]) for item in inspector.get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {str(item["name"]) for item in inspector.get_indexes(table_name)}


def _add_missing_columns(table_name: str, columns: dict[str, sa.Column]) -> None:
    existing = _column_names(table_name)
    for name, column in columns.items():
        if name not in existing:
            op.add_column(table_name, column)


def upgrade() -> None:
    _add_missing_columns(
        "fund_flow",
        {
            "source": sa.Column("source", sa.String(length=20), nullable=True),
            "source_version": sa.Column("source_version", sa.String(length=40), nullable=True),
            "observed_at": sa.Column("observed_at", sa.DateTime(), nullable=True),
        },
    )
    _add_missing_columns(
        "stock_sector_mapping",
        {
            "source_version": sa.Column("source_version", sa.String(length=40), nullable=True),
            "observed_at": sa.Column("observed_at", sa.DateTime(), nullable=True),
        },
    )
    _add_missing_columns(
        "broken_limit_pool",
        {
            "limit_up_price": sa.Column("limit_up_price", sa.Float(), nullable=True),
            "close_price": sa.Column("close_price", sa.Float(), nullable=True),
            "close_at_limit": sa.Column("close_at_limit", sa.Boolean(), nullable=True),
            "final_state": sa.Column("final_state", sa.String(length=24), nullable=True),
        },
    )
    _add_missing_columns(
        "market_sentiment",
        {
            "sentiment_score": sa.Column("sentiment_score", sa.Float(), nullable=True),
            "quality_status": sa.Column("quality_status", sa.String(length=16), nullable=True),
            "quality_reason": sa.Column("quality_reason", sa.Text(), nullable=True),
            "breadth_sample_count": sa.Column("breadth_sample_count", sa.Integer(), nullable=True),
            "breadth_coverage": sa.Column("breadth_coverage", sa.Float(), nullable=True),
            "index_avg_change_pct": sa.Column("index_avg_change_pct", sa.Float(), nullable=True),
            "calculation_version": sa.Column("calculation_version", sa.String(length=32), nullable=True),
            "observed_at": sa.Column("observed_at", sa.DateTime(), nullable=True),
        },
    )
    for table_name in ("paper_position", "paper_trade_log", "paper_auto_trade_log"):
        _add_missing_columns(
            table_name,
            {"strategy_version": sa.Column("strategy_version", sa.String(length=64), nullable=True)},
        )

    # SQLite/PostgreSQL both support this partial unique index. Closed legacy accounts
    # remain queryable while concurrent initialisation can no longer create two active rows.
    if "uq_paper_account_active_name" not in _index_names("paper_account"):
        op.create_index(
            "uq_paper_account_active_name",
            "paper_account",
            ["account_name"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )


def downgrade() -> None:
    if "uq_paper_account_active_name" in _index_names("paper_account"):
        op.drop_index("uq_paper_account_active_name", table_name="paper_account")

    for table_name in ("paper_auto_trade_log", "paper_trade_log", "paper_position"):
        columns = _column_names(table_name)
        if "strategy_version" in columns:
            with op.batch_alter_table(table_name) as batch_op:
                batch_op.drop_column("strategy_version")

    drop_columns = {
        "market_sentiment": (
            "observed_at", "calculation_version", "index_avg_change_pct",
            "breadth_coverage", "breadth_sample_count", "quality_reason",
            "quality_status", "sentiment_score",
        ),
        "broken_limit_pool": ("final_state", "close_at_limit", "close_price", "limit_up_price"),
        "stock_sector_mapping": ("observed_at", "source_version"),
        "fund_flow": ("observed_at", "source_version", "source"),
    }
    for table_name, names in drop_columns.items():
        existing = _column_names(table_name)
        with op.batch_alter_table(table_name) as batch_op:
            for name in names:
                if name in existing:
                    batch_op.drop_column(name)
