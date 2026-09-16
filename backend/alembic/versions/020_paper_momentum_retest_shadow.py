"""add append-only paper momentum-retest shadow ledgers

Revision ID: 020_paper_momentum_retest_shadow
Revises: 019_paper_auto_log_account_id
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "020_paper_momentum_retest_shadow"
down_revision: Union[str, None] = "019_paper_auto_log_account_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "paper_shadow_event" not in tables:
        op.create_table(
            "paper_shadow_event",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("event_key", sa.String(length=96), nullable=False),
            sa.Column("route_id", sa.String(length=40), nullable=False),
            sa.Column("route_version", sa.String(length=40), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("code", sa.String(length=10), nullable=False),
            sa.Column("name", sa.String(length=20), nullable=True),
            sa.Column("event_type", sa.String(length=24), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False),
            sa.Column("price", sa.Float(), nullable=True),
            sa.Column("assumed_fill_price", sa.Float(), nullable=True),
            sa.Column("change_pct", sa.Float(), nullable=True),
            sa.Column("snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_key", name="uq_paper_shadow_event_key"),
        )
        op.create_index("ix_paper_shadow_event_trade_date", "paper_shadow_event", ["trade_date"])
        op.create_index("ix_paper_shadow_event_observed_at", "paper_shadow_event", ["observed_at"])
        op.create_index("ix_paper_shadow_event_code", "paper_shadow_event", ["code"])
        op.create_index("ix_paper_shadow_event_event_type", "paper_shadow_event", ["event_type"])
        op.create_index(
            "ix_paper_shadow_event_route_date",
            "paper_shadow_event",
            ["route_id", "route_version", "trade_date", "event_type"],
        )

    if "paper_shadow_evaluation" not in tables:
        op.create_table(
            "paper_shadow_evaluation",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("signal_event_key", sa.String(length=96), nullable=False),
            sa.Column("route_id", sa.String(length=40), nullable=False),
            sa.Column("route_version", sa.String(length=40), nullable=False),
            sa.Column("code", sa.String(length=10), nullable=False),
            sa.Column("signal_trade_date", sa.Date(), nullable=False),
            sa.Column("signal_time", sa.DateTime(), nullable=False),
            sa.Column("horizon_days", sa.Integer(), nullable=False),
            sa.Column("exit_trade_date", sa.Date(), nullable=False),
            sa.Column("signal_price", sa.Float(), nullable=False),
            sa.Column("exit_price", sa.Float(), nullable=False),
            sa.Column("gross_return_pct", sa.Float(), nullable=True),
            sa.Column("net_return_pct", sa.Float(), nullable=True),
            sa.Column("benchmark_return_pct", sa.Float(), nullable=True),
            sa.Column("excess_return_pct", sa.Float(), nullable=True),
            sa.Column("max_favorable_pct", sa.Float(), nullable=True),
            sa.Column("max_adverse_pct", sa.Float(), nullable=True),
            sa.Column("is_positive", sa.Boolean(), nullable=True),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "signal_event_key",
                "horizon_days",
                name="uq_paper_shadow_evaluation_signal_horizon",
            ),
        )
        op.create_index(
            "ix_paper_shadow_evaluation_signal_event_key",
            "paper_shadow_evaluation",
            ["signal_event_key"],
        )
        op.create_index("ix_paper_shadow_evaluation_code", "paper_shadow_evaluation", ["code"])
        op.create_index(
            "ix_paper_shadow_evaluation_signal_trade_date",
            "paper_shadow_evaluation",
            ["signal_trade_date"],
        )
        op.create_index(
            "ix_paper_shadow_evaluation_route_horizon",
            "paper_shadow_evaluation",
            ["route_id", "route_version", "horizon_days", "signal_trade_date"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "paper_shadow_evaluation" in tables:
        op.drop_table("paper_shadow_evaluation")
    if "paper_shadow_event" in tables:
        op.drop_table("paper_shadow_event")
