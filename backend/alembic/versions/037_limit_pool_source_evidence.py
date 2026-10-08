"""Prospective Tencent limit-state and Wencai metadata evidence; no backfill."""
from alembic import op
import sqlalchemy as sa

revision = "037_limit_pool_source_evidence"
down_revision = "036_auction_source_frames"
branch_labels = None
depends_on = None

TABLES = ("limit_up_pool", "limit_down_pool", "broken_limit_pool")


def upgrade():
    inspector = sa.inspect(op.get_bind())
    for table in TABLES:
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, kind in (("source_version", sa.String(40)),
                           ("source_quote_at", sa.DateTime()),
                           ("observed_at", sa.DateTime()),
                           ("evidence_json", sa.Text())):
            if name not in existing:
                op.add_column(table, sa.Column(name, kind, nullable=True))


def downgrade():
    bind = op.get_bind()
    for table in TABLES:
        if bind.execute(sa.text(f"SELECT count(*) FROM {table} WHERE source_version IS NOT NULL")).scalar():
            raise RuntimeError("limit-pool source evidence exists; lossy downgrade prohibited")
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            for name in ("evidence_json", "observed_at", "source_quote_at", "source_version"):
                batch.drop_column(name)
