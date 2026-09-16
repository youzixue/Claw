"""Append raw news and analysis evidence without backfilling legacy clocks.

Revision ID: 029_news_evidence_versions
Revises: 028_factor_evaluation_runs
"""
from alembic import op
import sqlalchemy as sa

revision = "029_news_evidence_versions"
down_revision = "028_factor_evaluation_runs"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("news_content_version"):
        op.create_table(
            "news_content_version",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("news_id", sa.Integer(), sa.ForeignKey("finance_news.id"), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("source", sa.String(20), nullable=False),
            sa.Column("publish_time", sa.DateTime(), nullable=True),
            sa.Column("first_received_at", sa.DateTime(), nullable=True),
            sa.Column("received_at", sa.DateTime(), nullable=True),
            sa.Column("content_available_at", sa.DateTime(), nullable=True),
            sa.Column("recorded_at", sa.DateTime(), nullable=False),
            sa.Column("origin", sa.String(30), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("entity_evidence_json", sa.Text(), nullable=False),
            sa.Column("entity_verified_at", sa.DateTime(), nullable=False),
            sa.Column("protocol_version", sa.String(40), nullable=False),
        )
        for field in ("news_id", "publish_time", "content_available_at"):
            op.create_index(f"ix_news_content_version_{field}", "news_content_version", [field])
    if not sa.inspect(bind).has_table("news_analysis_version"):
        op.create_table(
            "news_analysis_version",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("content_version_id", sa.Integer(), sa.ForeignKey("news_content_version.id"), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("analysis_completed_at", sa.DateTime(), nullable=False),
            sa.Column("available_at", sa.DateTime(), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("result_hash", sa.String(64), nullable=False),
            sa.Column("protocol_version", sa.String(40), nullable=False),
        )
        for field in ("content_version_id", "available_at"):
            op.create_index(f"ix_news_analysis_version_{field}", "news_analysis_version", [field])
    if bind.dialect.name == "sqlite":
        for table in ("news_content_version", "news_analysis_version"):
            for action in ("UPDATE", "DELETE"):
                op.execute(sa.text(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'news evidence is append-only'); END"
                ))


def downgrade():
    for table in ("news_analysis_version", "news_content_version"):
        if sa.inspect(op.get_bind()).has_table(table):
            op.drop_table(table)
