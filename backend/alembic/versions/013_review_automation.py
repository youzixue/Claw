"""review automation runs and alerts

Revision ID: 013_review_automation
Revises: 012_daily_review_workbench
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "013_review_automation"
down_revision: Union[str, None] = "012_daily_review_workbench"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "review_automation_run" not in tables:
        op.create_table(
            "review_automation_run",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_key", sa.String(64), nullable=False, unique=True),
            sa.Column("logical_key", sa.String(64), nullable=False),
            sa.Column("job_name", sa.String(50), nullable=False),
            sa.Column("review_date", sa.Date(), nullable=False),
            sa.Column("phase", sa.String(20)),
            sa.Column("trigger", sa.String(20), nullable=False, server_default="schedule"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(30), nullable=False, server_default="running"),
            sa.Column("review_snapshot_id", sa.Integer(), sa.ForeignKey("daily_review_snapshot.id")),
            sa.Column("regime_snapshot_id", sa.Integer(), sa.ForeignKey("market_regime_snapshot.id")),
            sa.Column("quality_status", sa.String(20)),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("error_message", sa.Text()),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
        )
        op.create_index("ix_review_automation_run_run_key", "review_automation_run", ["run_key"], unique=True)
        op.create_index("ix_review_automation_run_logical_key", "review_automation_run", ["logical_key"])
        op.create_index("ix_review_automation_run_job_name", "review_automation_run", ["job_name"])
        op.create_index("ix_review_automation_run_review_date", "review_automation_run", ["review_date"])
        op.create_index("ix_review_automation_run_lookup", "review_automation_run", ["review_date", "job_name", "started_at"])
        op.create_index("ix_review_automation_run_logical", "review_automation_run", ["logical_key", "attempt"])

    tables = _tables()
    if "review_automation_alert" not in tables:
        op.create_table(
            "review_automation_alert",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("alert_key", sa.String(64), nullable=False, unique=True),
            sa.Column("automation_run_id", sa.Integer(), sa.ForeignKey("review_automation_run.id")),
            sa.Column("review_date", sa.Date(), nullable=False),
            sa.Column("phase", sa.String(20)),
            sa.Column("severity", sa.String(20), nullable=False),
            sa.Column("alert_type", sa.String(50), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_review_automation_alert_alert_key", "review_automation_alert", ["alert_key"], unique=True)
        op.create_index("ix_review_automation_alert_automation_run_id", "review_automation_alert", ["automation_run_id"])
        op.create_index("ix_review_automation_alert_review_date", "review_automation_alert", ["review_date"])
        op.create_index("ix_review_automation_alert_alert_type", "review_automation_alert", ["alert_type"])
        op.create_index("ix_review_automation_alert_lookup", "review_automation_alert", ["review_date", "severity", "created_at"])


def downgrade() -> None:
    tables = _tables()
    if "review_automation_alert" in tables:
        op.drop_table("review_automation_alert")
    if "review_automation_run" in tables:
        op.drop_table("review_automation_run")
