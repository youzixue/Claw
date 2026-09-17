"""Keep the pre-upsert watermark state so a past gate decision stays retraceable.

`data_watermark` is unique on (dataset, trade_date) and upserted in place, so the
state a quality gate actually saw at a past moment was destroyed by the next run.
This migration adds an append-only revision table. It does NOT reconstruct
history: only the current rows are seeded, labelled `initial_seed`, because those
are the only states this process has actually observed.
"""
from alembic import op
import sqlalchemy as sa

revision = "034_data_watermark_revisions"
down_revision = "033_factor_computation_runs"
branch_labels = None
depends_on = None

def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("data_watermark_revision"):
        op.create_table(
            "data_watermark_revision",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("dataset", sa.String(40), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("max_available_at", sa.DateTime()),
            sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("expected_count", sa.Integer()),
            sa.Column("completeness", sa.Float(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="missing"),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("replaced_at", sa.DateTime(), nullable=False),
            sa.Column("replacement_kind", sa.String(20), nullable=False,
                      server_default="superseded"),
        )
    else:
        columns = {col["name"] for col in inspector.get_columns("data_watermark_revision")}
        expected = {"dataset", "trade_date", "observed_at", "max_available_at",
                    "record_count", "expected_count", "completeness", "status",
                    "details_json", "id", "replaced_at", "replacement_kind"}
        if columns != expected:
            raise RuntimeError("existing watermark revision schema incompatible; preserve evidence")
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("data_watermark_revision")}
    if "ix_data_watermark_revision_lookup" not in indexes:
        op.create_index(
            "ix_data_watermark_revision_lookup", "data_watermark_revision",
            ["dataset", "trade_date", "observed_at"],
        )
    if bind.dialect.name == "sqlite":
        # 与 factor_computation_run 同一套 append-only 保护：水位历史只增不改。
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(
                f"CREATE TRIGGER IF NOT EXISTS data_watermark_revision_no_{action.lower()} "
                f"BEFORE {action} ON data_watermark_revision "
                "BEGIN SELECT RAISE(ABORT, 'watermark history is append-only'); END"))
    # Seed the states that exist right now; nothing earlier can be recovered.
    # INSERT ... WHERE NOT EXISTS keeps this idempotent: running the migration
    # twice (or after init_db's create_all already made the table) must not
    # duplicate the anchor row.
    bind.execute(sa.text(
        "INSERT INTO data_watermark_revision (dataset, trade_date, observed_at, "
        "max_available_at, record_count, expected_count, completeness, status, "
        "details_json, replaced_at, replacement_kind) "
        "SELECT dataset, trade_date, observed_at, max_available_at, record_count, "
        "expected_count, completeness, status, details_json, observed_at, 'initial_seed' "
        "FROM data_watermark AS w WHERE NOT EXISTS ("
        "SELECT 1 FROM data_watermark_revision AS r "
        "WHERE r.dataset = w.dataset AND r.trade_date = w.trade_date)"
    ))


def downgrade():
    raise RuntimeError("Watermark history must be retained; roll back consumers, not history")
