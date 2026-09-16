"""daily review workbench snapshots and attributions

Revision ID: 012_daily_review_workbench
Revises: 011_market_regime_snapshots
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "012_daily_review_workbench"
down_revision: Union[str, None] = "011_market_regime_snapshots"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "daily_review_snapshot" not in tables:
        op.create_table(
            "daily_review_snapshot",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("review_key", sa.String(64), nullable=False, unique=True),
            sa.Column("review_date", sa.Date(), nullable=False),
            sa.Column("analysis_trade_date", sa.Date(), nullable=False),
            sa.Column("phase", sa.String(20), nullable=False),
            sa.Column("as_of_at", sa.DateTime(), nullable=False),
            sa.Column("schema_version", sa.String(80), nullable=False),
            sa.Column("data_version", sa.String(80), nullable=False),
            sa.Column("quality_status", sa.String(20), nullable=False, server_default="partial"),
            sa.Column("quality_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("market_regime", sa.String(40)),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_daily_review_snapshot_review_key", "daily_review_snapshot", ["review_key"], unique=True)
        op.create_index("ix_daily_review_snapshot_review_date", "daily_review_snapshot", ["review_date"])
        op.create_index("ix_daily_review_snapshot_analysis_trade_date", "daily_review_snapshot", ["analysis_trade_date"])
        op.create_index("ix_daily_review_snapshot_phase", "daily_review_snapshot", ["phase"])
        op.create_index("ix_daily_review_snapshot_lookup", "daily_review_snapshot", ["review_date", "phase", "created_at"])

    tables = _tables()
    if "promotion_review_attribution" not in tables:
        op.create_table(
            "promotion_review_attribution",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("attribution_key", sa.String(64), nullable=False, unique=True),
            sa.Column("review_snapshot_id", sa.Integer(), sa.ForeignKey("daily_review_snapshot.id", ondelete="CASCADE"), nullable=False),
            sa.Column("prediction_run_id", sa.Integer(), sa.ForeignKey("promotion_prediction_run.id")),
            sa.Column("prediction_snapshot_id", sa.Integer(), sa.ForeignKey("promotion_prediction_snapshot.id")),
            sa.Column("code", sa.String(10), nullable=False),
            sa.Column("name", sa.String(30)),
            sa.Column("target_board", sa.Integer(), nullable=False),
            sa.Column("prediction_trade_date", sa.Date()),
            sa.Column("outcome_trade_date", sa.Date(), nullable=False),
            sa.Column("model_version", sa.String(80)),
            sa.Column("market_regime", sa.String(40)),
            sa.Column("attribution_type", sa.String(40), nullable=False),
            sa.Column("primary_reason", sa.String(80), nullable=False),
            sa.Column("causal_status", sa.String(30), nullable=False, server_default="observed_not_causal"),
            sa.Column("predicted_probability", sa.Float()),
            sa.Column("rank_position", sa.Integer()),
            sa.Column("actionable", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_promotion_review_attribution_attribution_key", "promotion_review_attribution", ["attribution_key"], unique=True)
        op.create_index("ix_promotion_review_attribution_review_snapshot_id", "promotion_review_attribution", ["review_snapshot_id"])
        op.create_index("ix_promotion_review_attribution_code", "promotion_review_attribution", ["code"])
        op.create_index("ix_promotion_review_attribution_outcome_trade_date", "promotion_review_attribution", ["outcome_trade_date"])
        op.create_index("ix_promotion_review_attribution_lookup", "promotion_review_attribution", ["outcome_trade_date", "target_board", "attribution_type"])

    tables = _tables()
    if "daily_review_note" not in tables:
        op.create_table(
            "daily_review_note",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("review_date", sa.Date(), nullable=False),
            sa.Column("phase", sa.String(20), nullable=False),
            sa.Column("category", sa.String(30), nullable=False, server_default="observation"),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("tags_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("author", sa.String(40), nullable=False, server_default="user"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_daily_review_note_review_date", "daily_review_note", ["review_date"])
        op.create_index("ix_daily_review_note_phase", "daily_review_note", ["phase"])
        op.create_index("ix_daily_review_note_lookup", "daily_review_note", ["review_date", "phase", "created_at"])


def downgrade() -> None:
    tables = _tables()
    for table_name in ("daily_review_note", "promotion_review_attribution", "daily_review_snapshot"):
        if table_name in tables:
            op.drop_table(table_name)
