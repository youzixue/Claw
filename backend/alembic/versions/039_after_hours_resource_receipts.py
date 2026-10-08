"""Future receipt-backed resource CAS; disabled until full typed execution is wired."""
from alembic import op
import sqlalchemy as sa
from app.trading.paper_after_hours_resource_schema import sqlite_guards

revision = "039_after_hours_resource_receipts"
down_revision = "038_after_hours_research"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "sqlite":
        raise RuntimeError("fixed-price resource consumption v1 requires verified SQLite guards")
    op.create_table("paper_after_hours_resource_scope",
        sa.Column("scope_key", sa.String(64), primary_key=True),
        sa.Column("account_numeric_id", sa.Integer(), sa.ForeignKey("paper_account.id"), nullable=False),
        sa.Column("account_name", sa.String(40), nullable=False),
        sa.Column("code", sa.String(10), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("terminal_sequence", sa.Integer(), nullable=False),
        sa.Column("lifecycle_prefix_hash", sa.String(64), nullable=False),
        sa.Column("source_quote_at", sa.DateTime(), nullable=False),
        sa.Column("source_available_at", sa.DateTime(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("revision >= 0 AND terminal_sequence >= 0", name="ck_af_resource_revision"),
        sa.UniqueConstraint("account_numeric_id", "code", "trade_date", name="uq_af_resource_account_security_day"))
    op.create_index("ix_af_resource_account_date", "paper_after_hours_resource_scope",
                    ["account_numeric_id", "trade_date", "code"])
    op.create_table("paper_after_hours_resource_receipt",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("allocation_id", sa.String(64), nullable=False, unique=True),
        sa.Column("scope_key", sa.String(64), sa.ForeignKey("paper_after_hours_resource_scope.scope_key"), nullable=False),
        sa.Column("trade_fill_id", sa.Integer(), sa.ForeignKey("trade_fill.id"), nullable=False, unique=True),
        sa.Column("paper_trade_id", sa.Integer(), sa.ForeignKey("paper_trade_log.id"), nullable=False, unique=True),
        sa.Column("order_id", sa.String(40), sa.ForeignKey("trade_order.order_id"), nullable=False),
        sa.Column("protocol_version", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("length(payload_json) <= 2097152", name="ck_af_resource_payload"))
    op.create_index("ix_paper_after_hours_resource_receipt_scope_key",
                    "paper_after_hours_resource_receipt", ["scope_key"])
    for statement in sqlite_guards(include_partial=False):
        op.execute(sa.text(statement))


def downgrade():
    for table in ("paper_after_hours_resource_receipt", "paper_after_hours_resource_scope"):
        if op.get_bind().execute(sa.text(f"SELECT count(*) FROM {table}")).scalar():
            raise RuntimeError("fixed-price resource evidence exists; lossy downgrade prohibited")
    # Some guards live on existing book tables, so they must be removed explicitly.
    for statement in sqlite_guards(include_partial=False):
        name = statement.split("IF NOT EXISTS ", 1)[1].split()[0]
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    op.drop_table("paper_after_hours_resource_receipt")
    op.drop_table("paper_after_hours_resource_scope")
