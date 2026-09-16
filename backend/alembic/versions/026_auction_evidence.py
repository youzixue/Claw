"""Persist auction provenance without backfilling legacy evidence.

Revision ID: 026_auction_evidence
Revises: 025_fund_source_clocks
"""
from alembic import op
import sqlalchemy as sa

revision = "026_auction_evidence"
down_revision = "025_fund_source_clocks"
branch_labels = None
depends_on = None

COLUMNS = {
    "source": sa.String(20),
    "source_version": sa.String(64),
    "source_quote_at": sa.DateTime(),
    "received_at": sa.DateTime(),
    "observed_at": sa.DateTime(),
    "price_basis": sa.String(32),
    "volume_basis": sa.String(32),
    "volume_unit": sa.String(16),
    "amount_unit": sa.String(16),
}


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("auction_data")}
    for name, type_ in COLUMNS.items():
        if name not in existing:
            op.add_column("auction_data", sa.Column(name, type_, nullable=True))


def downgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("auction_data")}
    with op.batch_alter_table("auction_data") as batch:
        for name in reversed(COLUMNS):
            if name in existing:
                batch.drop_column(name)
