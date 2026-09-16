"""immutable market regime snapshots

Revision ID: 011_market_regime_snapshots
Revises: 010_promotion_training_runs
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "011_market_regime_snapshots"
down_revision: Union[str, None] = "010_promotion_training_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "market_regime_snapshot" in _tables():
        return
    op.create_table(
        "market_regime_snapshot",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_key", sa.String(64), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("as_of_at", sa.DateTime(), nullable=False),
        sa.Column("snapshot_context", sa.String(30), nullable=False, server_default="postmarket"),
        sa.Column("regime_version", sa.String(80), nullable=False),
        sa.Column("data_version", sa.String(80), nullable=False),
        sa.Column("primary_regime", sa.String(40), nullable=False),
        sa.Column("secondary_regime", sa.String(40)),
        sa.Column("previous_regime", sa.String(40)),
        sa.Column("transition_type", sa.String(30), nullable=False, server_default="initial"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("quality_status", sa.String(20), nullable=False, server_default="partial"),
        sa.Column("input_coverage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("universe_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scores_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("features_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("snapshot_key", name="uq_market_regime_snapshot_key"),
    )
    op.create_index(
        "ix_market_regime_snapshot_snapshot_key",
        "market_regime_snapshot",
        ["snapshot_key"],
        unique=True,
    )
    op.create_index(
        "ix_market_regime_snapshot_trade_date",
        "market_regime_snapshot",
        ["trade_date"],
    )
    op.create_index(
        "ix_market_regime_snapshot_primary_regime",
        "market_regime_snapshot",
        ["primary_regime"],
    )
    op.create_index(
        "ix_market_regime_snapshot_lookup",
        "market_regime_snapshot",
        ["trade_date", "snapshot_context", "created_at"],
    )
    op.create_index(
        "ix_market_regime_snapshot_regime",
        "market_regime_snapshot",
        ["primary_regime", "trade_date"],
    )


def downgrade() -> None:
    if "market_regime_snapshot" in _tables():
        op.drop_table("market_regime_snapshot")
