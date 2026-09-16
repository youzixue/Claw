"""append-only GPT review reports

Revision ID: 018_review_gpt_report
Revises: 017_fundamental_daily_snapshot
Create Date: 2026-08-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "018_review_gpt_report"
down_revision: Union[str, None] = "017_fundamental_daily_snapshot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "review_gpt_report" in _tables():
        return
    op.create_table(
        "review_gpt_report",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("report_key", sa.String(64), nullable=False),
        sa.Column("review_date", sa.Date(), nullable=False),
        sa.Column(
            "review_snapshot_id",
            sa.Integer(),
            sa.ForeignKey("daily_review_snapshot.id"),
        ),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False, server_default=""),
        sa.Column("model", sa.String(80), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="generated"),
        sa.Column("content_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime()),
        sa.UniqueConstraint(
            "review_date", "phase", name="uq_review_gpt_report_date_phase"
        ),
    )
    op.create_index(
        "ix_review_gpt_report_lookup",
        "review_gpt_report",
        ["review_date", "phase", "created_at"],
    )


def downgrade() -> None:
    if "review_gpt_report" in _tables():
        op.drop_table("review_gpt_report")
