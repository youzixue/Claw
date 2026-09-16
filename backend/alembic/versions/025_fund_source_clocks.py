"""Preserve fund source and receipt clocks without backfilling legacy evidence.

Revision ID: 025_fund_source_clocks
Revises: 024_quote_round_execution
"""
from alembic import op
import sqlalchemy as sa

revision = "025_fund_source_clocks"
down_revision = "024_quote_round_execution"
branch_labels = None
depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("fund_flow")}
    for name in ("source_quote_at", "received_at"):
        if name not in existing:
            op.add_column("fund_flow", sa.Column(name, sa.DateTime(), nullable=True))


def downgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("fund_flow")}
    with op.batch_alter_table("fund_flow") as batch:
        for name in ("received_at", "source_quote_at"):
            if name in existing:
                batch.drop_column(name)
