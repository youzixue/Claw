"""Prospective immutable fixed-price-session research; no historical backfill."""
from alembic import op
import sqlalchemy as sa
from app.data.after_hours_schema import (
    SQLITE_INSERT_GUARD, POSTGRES_GUARD_FUNCTION, POSTGRES_GUARD_TRIGGER,
)

revision = "038_after_hours_research"
down_revision = "037_limit_pool_source_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stock_after_hours_observation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("source_quote_at", sa.DateTime(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("quality_status", sa.String(24), nullable=False),
        sa.Column("protocol_version", sa.String(48), nullable=False),
        sa.UniqueConstraint("code", "trade_date", "stage", "source", "source_version",
                            "content_hash", name="uq_after_hours_content"),
    )
    op.create_index("ix_after_hours_date_available", "stock_after_hours_observation",
                    ["trade_date", "available_at"])
    op.create_index("ix_after_hours_code_date", "stock_after_hours_observation",
                    ["code", "trade_date"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(
                f"CREATE TRIGGER after_hours_no_{action.lower()} BEFORE {action} "
                "ON stock_after_hours_observation "
                "BEGIN SELECT RAISE(ABORT, 'after-hours evidence is append-only'); END"))
        op.execute(sa.text(SQLITE_INSERT_GUARD))
    elif op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(POSTGRES_GUARD_FUNCTION))
        op.execute(sa.text(POSTGRES_GUARD_TRIGGER))


def downgrade():
    if op.get_bind().execute(sa.text(
            "SELECT count(*) FROM stock_after_hours_observation")).scalar():
        raise RuntimeError("after-hours evidence exists; lossy downgrade prohibited")
    op.drop_table("stock_after_hours_observation")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP FUNCTION IF EXISTS claw_after_hours_forbid_mutation()"))
