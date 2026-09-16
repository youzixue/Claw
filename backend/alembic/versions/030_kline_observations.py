"""Append unreviewed K-line observations without changing historical prices.

Revision ID: 030_kline_observations
Revises: 029_news_evidence_versions
"""
from alembic import op
import sqlalchemy as sa

revision = "030_kline_observations"
down_revision = "029_news_evidence_versions"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("stock_kline_observation"):
        op.create_table(
            "stock_kline_observation",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("code", sa.String(10), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("origin", sa.String(30), nullable=False),
            sa.Column("disposition", sa.String(30), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.Column("available_at", sa.DateTime(), nullable=True),
            sa.Column("source_version", sa.String(50), nullable=False),
            sa.Column("price_basis", sa.String(40), nullable=False),
            sa.Column("quality_issues_json", sa.Text(), nullable=False),
            sa.Column("protocol_version", sa.String(40), nullable=False),
            sa.UniqueConstraint("code", "trade_date", "origin", "disposition", "payload_hash",
                                name="uq_kline_observation_content"),
        )
        op.create_index("ix_kline_observation_code_date", "stock_kline_observation", ["code", "trade_date"])
        op.create_index("ix_kline_observation_recorded_at", "stock_kline_observation", ["recorded_at"])
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(
                f"CREATE TRIGGER IF NOT EXISTS stock_kline_observation_no_{action.lower()} "
                f"BEFORE {action} ON stock_kline_observation "
                "BEGIN SELECT RAISE(ABORT, 'K-line evidence is append-only'); END"
            ))


def downgrade():
    # Evidence retention is intentional. Roll back consumers, not captured bytes.
    raise RuntimeError("K-line observation downgrade requires an explicit evidence-retention plan")
