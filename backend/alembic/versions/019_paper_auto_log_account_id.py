"""add account_id to paper_auto_trade_log for multi-strategy parallel accounts

Revision ID: 019_paper_auto_log_account_id
Revises: 018_review_gpt_report
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "019_paper_auto_log_account_id"
down_revision: Union[str, None] = "018_review_gpt_report"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "paper_auto_trade_log" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("paper_auto_trade_log")}
    if "account_id" not in columns:
        op.add_column("paper_auto_trade_log", sa.Column("account_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_paper_auto_trade_log_account_id",
        "paper_auto_trade_log",
        ["account_id"],
        if_not_exists=True,
    )
    # 双策略并行: paper_account 增加 strategy 标识 (default/promotion)
    account_columns = {
        col["name"] for col in inspector.get_columns("paper_account")
    } if "paper_account" in inspector.get_table_names() else set()
    if "paper_account" in inspector.get_table_names() and "strategy" not in account_columns:
        op.add_column(
            "paper_account",
            sa.Column("strategy", sa.String(30), nullable=True, server_default="default"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "paper_auto_trade_log" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("paper_auto_trade_log")}
    if "account_id" in columns:
        op.drop_column("paper_auto_trade_log", "account_id")
    if "paper_account" in inspector.get_table_names():
        account_columns = {col["name"] for col in inspector.get_columns("paper_account")}
        if "strategy" in account_columns:
            op.drop_column("paper_account", "strategy")
