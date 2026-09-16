"""Retain measured order-size fund fields; never infer legacy breakdowns.

Revision ID: 027_fund_order_breakdown
Revises: 026_auction_evidence
"""
from alembic import op
import sqlalchemy as sa

revision = "027_fund_order_breakdown"
down_revision = "026_auction_evidence"
branch_labels = None
depends_on = None

COLUMNS = (
    "super_net_inflow",
    "super_net_inflow_pct",
    "big_net_inflow_pct",
    "mid_net_inflow_pct",
    "small_net_inflow_pct",
)


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("fund_flow")}
    for name in COLUMNS:
        if name not in existing:
            # Amounts are CNY; ratios are provider percentage points, not fractions.
            # Nullable without defaults: no main-minus-big or amount-based backfill.
            op.add_column("fund_flow", sa.Column(name, sa.Float(), nullable=True))


def downgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("fund_flow")}
    with op.batch_alter_table("fund_flow") as batch:
        for name in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
