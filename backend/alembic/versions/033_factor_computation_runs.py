"""Append real computation captures; never reconstruct historical factor evidence."""
from alembic import op
import sqlalchemy as sa

revision = "033_factor_computation_runs"
down_revision = "032_anomaly_candidate_evidence"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("factor_computation_run"):
        op.create_table("factor_computation_run",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("capture_id", sa.String(40), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("read_started_at", sa.DateTime(), nullable=False),
            sa.Column("captured_at", sa.DateTime(), nullable=False),
            sa.Column("protocol_version", sa.String(40), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("capture_id", name="uq_factor_computation_capture"))
    else:
        columns = {col["name"]: col for col in inspector.get_columns("factor_computation_run")}
        expected = {"id", "capture_id", "trade_date", "read_started_at", "captured_at",
                    "protocol_version", "payload_hash", "payload_json"}
        uniques = inspector.get_unique_constraints("factor_computation_run")
        if (set(columns) != expected
                or any(columns[key]["nullable"] for key in expected - {"id"})
                or inspector.get_pk_constraint("factor_computation_run")["constrained_columns"] != ["id"]
                or not any(item["column_names"] == ["capture_id"] for item in uniques)):
            raise RuntimeError("existing factor computation schema incompatible; preserve evidence for review")
    indexes = sa.inspect(bind).get_indexes("factor_computation_run")
    if not any(item["name"] == "ix_factor_computation_day_time" for item in indexes):
        op.create_index("ix_factor_computation_day_time", "factor_computation_run",
                        ["trade_date", "captured_at"])
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(
                f"CREATE TRIGGER IF NOT EXISTS factor_computation_run_no_{action.lower()} "
                f"BEFORE {action} ON factor_computation_run "
                "BEGIN SELECT RAISE(ABORT, 'factor computation evidence is append-only'); END"))
        op.execute(sa.text(
            "CREATE TRIGGER IF NOT EXISTS factor_computation_run_no_replace "
            "BEFORE INSERT ON factor_computation_run WHEN EXISTS "
            "(SELECT 1 FROM factor_computation_run WHERE id=NEW.id OR capture_id=NEW.capture_id) "
            "BEGIN SELECT RAISE(ABORT, 'factor computation evidence is append-only'); END"))


def downgrade():
    raise RuntimeError("Factor captures must be retained; roll back consumers, not history")
