"""添加新闻NLP状态字段

Revision ID: 003_news_nlp_status
Revises: 002_kline_indicators
Create Date: 2026-05-06
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_news_nlp_status"
down_revision: Union[str, None] = "002_kline_indicators"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _add_column_if_not_exists("finance_news", sa.Column("nlp_status", sa.String(20), nullable=True))
    _add_column_if_not_exists("finance_news", sa.Column("sentiment_method", sa.String(20), nullable=True))
    _add_column_if_not_exists("finance_news", sa.Column("events_method", sa.String(20), nullable=True))
    _add_column_if_not_exists("finance_news", sa.Column("nlp_error", sa.Text, nullable=True))
    _add_column_if_not_exists("finance_news", sa.Column("nlp_analyzed_at", sa.DateTime, nullable=True))
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE finance_news "
        "SET nlp_status = CASE "
        "WHEN COALESCE(bull_bear_confidence, 0) > 0 THEN 'analyzed' "
        "ELSE 'raw' END "
        "WHERE nlp_status IS NULL"
    ))


def downgrade() -> None:
    _drop_column_if_exists("finance_news", "nlp_analyzed_at")
    _drop_column_if_exists("finance_news", "nlp_error")
    _drop_column_if_exists("finance_news", "events_method")
    _drop_column_if_exists("finance_news", "sentiment_method")
    _drop_column_if_exists("finance_news", "nlp_status")


def _add_column_if_not_exists(table: str, column: sa.Column) -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text(f"PRAGMA table_info({table})"))
    existing_cols = {row[1] for row in result}
    if column.name not in existing_cols:
        op.add_column(table, column)


def _drop_column_if_exists(table: str, col_name: str) -> None:
    conn = op.get_bind()
    result = conn.execute(sa.text(f"PRAGMA table_info({table})"))
    existing_cols = {row[1] for row in result}
    if col_name in existing_cols:
        op.drop_column(table, col_name)
