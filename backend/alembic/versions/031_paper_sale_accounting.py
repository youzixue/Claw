"""Append-only prospective paper sale fee allocation; never backfill old trades."""
from alembic import op
import sqlalchemy as sa

revision = "031_paper_sale_accounting"
down_revision = "030_kline_observations"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("paper_sale_accounting"):
        op.create_table("paper_sale_accounting",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("trade_id", sa.Integer(), sa.ForeignKey("paper_trade_log.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("code", sa.String(10), nullable=False),
            sa.Column("version", sa.String(40), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("trade_id"),
        )
        op.create_index("ix_paper_sale_accounting_account_code", "paper_sale_accounting",
                        ["account_id", "code"])
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(
                f"CREATE TRIGGER IF NOT EXISTS paper_sale_accounting_no_{action.lower()} "
                f"BEFORE {action} ON paper_sale_accounting "
                "BEGIN SELECT RAISE(ABORT, 'sale accounting evidence is append-only'); END"
            ))

        op.execute(sa.text(
            "CREATE TRIGGER IF NOT EXISTS paper_sale_accounting_no_replace "
            "BEFORE INSERT ON paper_sale_accounting WHEN EXISTS "
            "(SELECT 1 FROM paper_sale_accounting WHERE trade_id=NEW.trade_id OR id=NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'sale accounting evidence is append-only'); END"
        ))


def downgrade():
    raise RuntimeError("Sale accounting evidence must be retained; roll back consumers, not history")
