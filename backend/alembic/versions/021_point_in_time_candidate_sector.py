"""add quote clocks, anomaly candidate lifecycle, and entry-sector thesis

Revision ID: 021_point_time_candidate_sector
Revises: 020_paper_momentum_retest_shadow
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "021_point_time_candidate_sector"
down_revision: Union[str, None] = "020_paper_momentum_retest_shadow"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {str(item["name"]) for item in inspector.get_columns(table_name)}


def upgrade() -> None:
    spot_columns = _column_names("stock_spot")
    if "source_quote_at" not in spot_columns:
        op.add_column("stock_spot", sa.Column("source_quote_at", sa.DateTime(), nullable=True))
    if "received_at" not in spot_columns:
        op.add_column("stock_spot", sa.Column("received_at", sa.DateTime(), nullable=True))

    position_columns = _column_names("paper_position")
    if "entry_sector_code" not in position_columns:
        op.add_column("paper_position", sa.Column("entry_sector_code", sa.String(length=20), nullable=True))
    if "entry_sector_name" not in position_columns:
        op.add_column("paper_position", sa.Column("entry_sector_name", sa.String(length=30), nullable=True))

    inspector = sa.inspect(op.get_bind())
    if "anomaly_candidate_record" not in set(inspector.get_table_names()):
        op.create_table(
            "anomaly_candidate_record",
            sa.Column("record_id", sa.String(length=30), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("code", sa.String(length=10), nullable=False),
            sa.Column("identity", sa.Text(), nullable=False),
            sa.Column("name", sa.String(length=30), nullable=True),
            sa.Column("event_type", sa.String(length=30), nullable=False, server_default=""),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("last_score", sa.Float(), nullable=True),
            sa.Column("last_grade", sa.String(length=30), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="candidate"),
            sa.Column("reject_reasons_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("pushed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("pushed_at", sa.DateTime(), nullable=True),
            sa.Column("snapshot_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("record_id"),
        )
        op.create_index("ix_anomaly_candidate_record_trade_date", "anomaly_candidate_record", ["trade_date"])
        op.create_index("ix_anomaly_candidate_record_code", "anomaly_candidate_record", ["code"])
        op.create_index("ix_anomaly_candidate_date_status", "anomaly_candidate_record", ["trade_date", "status"])
        op.create_index("ix_anomaly_candidate_date_code", "anomaly_candidate_record", ["trade_date", "code"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "anomaly_candidate_record" in set(inspector.get_table_names()):
        op.drop_table("anomaly_candidate_record")

    position_columns = _column_names("paper_position")
    with op.batch_alter_table("paper_position") as batch_op:
        if "entry_sector_name" in position_columns:
            batch_op.drop_column("entry_sector_name")
        if "entry_sector_code" in position_columns:
            batch_op.drop_column("entry_sector_code")

    spot_columns = _column_names("stock_spot")
    with op.batch_alter_table("stock_spot") as batch_op:
        if "received_at" in spot_columns:
            batch_op.drop_column("received_at")
        if "source_quote_at" in spot_columns:
            batch_op.drop_column("source_quote_at")
