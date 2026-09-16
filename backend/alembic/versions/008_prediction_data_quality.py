"""prediction data quality audit tables

Revision ID: 008_prediction_data_quality
Revises: 007_promotion_snapshot_identity
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "008_prediction_data_quality"
down_revision: Union[str, None] = "007_promotion_snapshot_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "data_quality_run" not in tables:
        op.create_table(
            "data_quality_run",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_type", sa.String(30), nullable=False, server_default="prediction_gate"),
            sa.Column("trade_date", sa.Date(), nullable=True),
            sa.Column("snapshot_context", sa.String(40), nullable=False, server_default=""),
            sa.Column("status", sa.String(20), nullable=False, server_default="running"),
            sa.Column("gate_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("issue_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("blocking_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_data_quality_run_trade_date", "data_quality_run", ["trade_date"])
        op.create_index(
            "ix_data_quality_run_lookup",
            "data_quality_run",
            ["trade_date", "run_type", "started_at"],
        )

    tables = _tables()
    if "data_quality_issue" not in tables:
        op.create_table(
            "data_quality_issue",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("data_quality_run.id"), nullable=False),
            sa.Column("severity", sa.String(20), nullable=False, server_default="warning"),
            sa.Column("dataset", sa.String(40), nullable=False),
            sa.Column("issue_type", sa.String(60), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=True),
            sa.Column("code", sa.String(10), nullable=True),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_data_quality_issue_trade_date", "data_quality_issue", ["trade_date"])
        op.create_index("ix_data_quality_issue_code", "data_quality_issue", ["code"])
        op.create_index(
            "ix_data_quality_issue_lookup",
            "data_quality_issue",
            ["issue_type", "trade_date", "severity"],
        )
        op.create_index(
            "ix_data_quality_issue_run",
            "data_quality_issue",
            ["run_id", "severity"],
        )

    tables = _tables()
    if "data_watermark" not in tables:
        op.create_table(
            "data_watermark",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("dataset", sa.String(40), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("max_available_at", sa.DateTime(), nullable=True),
            sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expected_count", sa.Integer(), nullable=True),
            sa.Column("completeness", sa.Float(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="missing"),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.UniqueConstraint("dataset", "trade_date", name="uq_data_watermark_dataset_date"),
        )
        op.create_index(
            "ix_data_watermark_status",
            "data_watermark",
            ["status", "trade_date"],
        )


def downgrade() -> None:
    tables = _tables()
    if "data_watermark" in tables:
        op.drop_table("data_watermark")
    tables = _tables()
    if "data_quality_issue" in tables:
        op.drop_table("data_quality_issue")
    tables = _tables()
    if "data_quality_run" in tables:
        op.drop_table("data_quality_run")
