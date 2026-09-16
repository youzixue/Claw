"""Append-only actual-return IC runs; preserve legacy factor evaluation rows.

Revision ID: 028_factor_evaluation_runs
Revises: 027_fund_order_breakdown
"""
from alembic import op
import sqlalchemy as sa

revision = "028_factor_evaluation_runs"
down_revision = "027_fund_order_breakdown"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("factor_evaluation_run"):
        return
    op.create_table(
        "factor_evaluation_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("factor_name", sa.String(30), nullable=False),
        sa.Column("eval_date", sa.Date(), nullable=False),
        sa.Column("as_of_at", sa.DateTime(), nullable=False),
        sa.Column("protocol_version", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("factor_name", "protocol_version", "input_hash", name="uq_factor_eval_run_input"),
    )
    op.create_index("ix_factor_evaluation_run_factor_name", "factor_evaluation_run", ["factor_name"])
    op.create_index("ix_factor_evaluation_run_eval_date", "factor_evaluation_run", ["eval_date"])


def downgrade():
    if sa.inspect(op.get_bind()).has_table("factor_evaluation_run"):
        op.drop_table("factor_evaluation_run")
