"""Preserve independent auction source frames; never backfill old provenance."""
from alembic import op
import sqlalchemy as sa

revision = "036_auction_source_frames"
down_revision = "035_shared_paper_portfolio"
branch_labels = None
depends_on = None

OLD = "uq_auction_code_date_time"
NEW = "uq_auction_source_frame_key"


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("auction_data")}
    unique = {item["name"]: item["column_names"]
              for item in inspector.get_unique_constraints("auction_data")}
    if "source_frame_key" in columns:
        indexes = {item["name"]: item["column_names"]
                   for item in inspector.get_indexes("auction_data")}
        if (unique.get(NEW) == ["source_frame_key"] and OLD not in unique
                and indexes.get("ix_auction_date_code_time") == ["trade_date", "code", "auction_time"]):
            return
        raise RuntimeError("unverified existing auction source-frame schema")
    if unique.get(OLD) != ["code", "trade_date", "auction_time"]:
        raise RuntimeError("036 requires the verified pre-source-frame auction schema")
    # SQLite batch recreation copies every existing column and row as-is.
    # The new nullable identity is deliberately NULL for all legacy evidence.
    with op.batch_alter_table("auction_data") as batch:
        batch.add_column(sa.Column("source_frame_key", sa.String(64), nullable=True))
        batch.drop_constraint(OLD, type_="unique")
        batch.create_unique_constraint(NEW, ["source_frame_key"])
    op.create_index("ix_auction_date_code_time", "auction_data", ["trade_date", "code", "auction_time"])


def downgrade():
    bind = op.get_bind()
    # Never discard prospective frame identity, even when no collision happens
    # to be present yet. Downgrade requires explicit offline evidence handling.
    if bind.execute(sa.text(
        "SELECT count(*) FROM auction_data WHERE source_frame_key IS NOT NULL"
    )).scalar():
        raise RuntimeError("auction source frames exist; lossy downgrade prohibited")
    if bind.execute(sa.text(
        "SELECT count(*) FROM (SELECT 1 FROM auction_data "
        "GROUP BY code, trade_date, auction_time HAVING count(*) > 1) collisions"
    )).scalar():
        raise RuntimeError("same-second legacy rows exist; lossy downgrade prohibited")
    op.drop_index("ix_auction_date_code_time", table_name="auction_data")
    with op.batch_alter_table("auction_data") as batch:
        batch.drop_constraint(NEW, type_="unique")
        batch.drop_column("source_frame_key")
        batch.create_unique_constraint(OLD, ["code", "trade_date", "auction_time"])
