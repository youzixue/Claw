"""Prospective anomaly candidate evidence; never reconstruct old reasons."""
from alembic import op
import sqlalchemy as sa

revision = "032_anomaly_candidate_evidence"
down_revision = "031_paper_sale_accounting"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("anomaly_candidate_evidence"):
        op.create_table("anomaly_candidate_evidence",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("capture_id", sa.String(40), nullable=False),
            sa.Column("record_id", sa.String(30), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("code", sa.String(10), nullable=False),
            sa.Column("captured_at", sa.DateTime(), nullable=False),
            sa.Column("protocol_version", sa.String(40), nullable=False),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.UniqueConstraint("capture_id", "record_id", name="uq_anomaly_evidence_capture_record"))
        op.create_index("ix_anomaly_evidence_record_time", "anomaly_candidate_evidence",
                        ["record_id", "captured_at"])
        op.create_index("ix_anomaly_evidence_day_code", "anomaly_candidate_evidence",
                        ["trade_date", "code"])
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(sa.text(
                f"CREATE TRIGGER IF NOT EXISTS anomaly_candidate_evidence_no_{action.lower()} "
                f"BEFORE {action} ON anomaly_candidate_evidence "
                "BEGIN SELECT RAISE(ABORT, 'anomaly candidate evidence is append-only'); END"))
        op.execute(sa.text(
            "CREATE TRIGGER IF NOT EXISTS anomaly_candidate_evidence_no_replace "
            "BEFORE INSERT ON anomaly_candidate_evidence WHEN EXISTS "
            "(SELECT 1 FROM anomaly_candidate_evidence WHERE id=NEW.id OR "
            "(capture_id=NEW.capture_id AND record_id=NEW.record_id)) "
            "BEGIN SELECT RAISE(ABORT, 'anomaly candidate evidence is append-only'); END"))


def downgrade():
    raise RuntimeError("Candidate evidence must be retained; roll back consumers, not history")
