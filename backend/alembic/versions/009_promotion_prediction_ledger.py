"""append-only promotion prediction ledger and model registry

Revision ID: 009_promotion_prediction_ledger
Revises: 008_prediction_data_quality
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "009_promotion_prediction_ledger"
down_revision: Union[str, None] = "008_prediction_data_quality"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "promotion_model_artifact" not in tables:
        op.create_table(
            "promotion_model_artifact",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("model_version", sa.String(80), nullable=False),
            sa.Column("feature_version", sa.String(80), nullable=False),
            sa.Column("data_version", sa.String(80), nullable=False),
            sa.Column("algorithm", sa.String(60), nullable=False, server_default="legacy_rule_ensemble"),
            sa.Column("status", sa.String(20), nullable=False, server_default="registered"),
            sa.Column("artifact_uri", sa.Text()),
            sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("training_start_date", sa.Date()),
            sa.Column("training_end_date", sa.Date()),
            sa.Column("validation_start_date", sa.Date()),
            sa.Column("validation_end_date", sa.Date()),
            sa.Column("code_commit", sa.String(64)),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("activated_at", sa.DateTime()),
            sa.Column("retired_at", sa.DateTime()),
            sa.UniqueConstraint("model_version", name="uq_promotion_model_artifact_version"),
        )
        op.create_index(
            "ix_promotion_model_artifact_model_version",
            "promotion_model_artifact",
            ["model_version"],
            unique=True,
        )
        op.create_index(
            "ix_promotion_model_artifact_status",
            "promotion_model_artifact",
            ["status", "created_at"],
        )

    tables = _tables()
    if "promotion_prediction_run" not in tables:
        op.create_table(
            "promotion_prediction_run",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_key", sa.String(64), nullable=False),
            sa.Column("snapshot_batch_key", sa.String(255), nullable=False, server_default=""),
            sa.Column("reference_trade_date", sa.Date(), nullable=False),
            sa.Column("as_of_at", sa.DateTime(), nullable=False),
            sa.Column("snapshot_source", sa.String(20), nullable=False),
            sa.Column("snapshot_context", sa.String(40), nullable=False),
            sa.Column("model_version", sa.String(80), nullable=False),
            sa.Column("feature_version", sa.String(80), nullable=False),
            sa.Column("data_version", sa.String(80), nullable=False),
            sa.Column("runtime_mode", sa.String(20), nullable=False, server_default="legacy"),
            sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
            sa.Column("gate_passed", sa.Boolean()),
            sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ranked_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("actionable_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime()),
            sa.UniqueConstraint("run_key", name="uq_promotion_prediction_run_key"),
        )
        op.create_index(
            "ix_promotion_prediction_run_run_key",
            "promotion_prediction_run",
            ["run_key"],
            unique=True,
        )
        op.create_index(
            "ix_promotion_prediction_run_reference_trade_date",
            "promotion_prediction_run",
            ["reference_trade_date"],
        )
        op.create_index(
            "ix_promotion_prediction_run_model_version",
            "promotion_prediction_run",
            ["model_version"],
        )
        op.create_index(
            "ix_promotion_prediction_run_lookup",
            "promotion_prediction_run",
            ["reference_trade_date", "snapshot_context", "created_at"],
        )
        op.create_index(
            "ix_promotion_prediction_run_model",
            "promotion_prediction_run",
            ["model_version", "created_at"],
        )

    tables = _tables()
    if "promotion_prediction_snapshot" not in tables:
        op.create_table(
            "promotion_prediction_snapshot",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("promotion_prediction_run.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("record_key", sa.String(64), nullable=False),
            sa.Column("legacy_record_id", sa.Integer()),
            sa.Column("code", sa.String(10), nullable=False),
            sa.Column("name", sa.String(30)),
            sa.Column("target_board", sa.Integer(), nullable=False),
            sa.Column("prediction_trade_date", sa.Date(), nullable=False),
            sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("candidate_route", sa.String(60), nullable=False, server_default=""),
            sa.Column("learning_bucket", sa.String(100)),
            sa.Column("rank_scope", sa.String(30), nullable=False, server_default="pool_unranked"),
            sa.Column("pool_rank", sa.Integer()),
            sa.Column("rank_position", sa.Integer()),
            sa.Column("recall_rank_position", sa.Integer()),
            sa.Column("raw_probability", sa.Float()),
            sa.Column("calibrated_probability", sa.Float()),
            sa.Column("confidence_level", sa.String(20)),
            sa.Column("signal_status", sa.String(30)),
            sa.Column("trade_gate_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("actionable", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("watch_only", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("reason_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("features_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("run_id", "record_key", name="uq_promotion_snapshot_run_record"),
        )
        op.create_index(
            "ix_promotion_prediction_snapshot_run_id",
            "promotion_prediction_snapshot",
            ["run_id"],
        )
        op.create_index(
            "ix_promotion_prediction_snapshot_code",
            "promotion_prediction_snapshot",
            ["code"],
        )
        op.create_index(
            "ix_promotion_ledger_snapshot_lookup",
            "promotion_prediction_snapshot",
            ["prediction_trade_date", "target_board", "code"],
        )
        op.create_index(
            "ix_promotion_ledger_snapshot_rank",
            "promotion_prediction_snapshot",
            ["run_id", "rank_scope", "rank_position"],
        )

    tables = _tables()
    if "promotion_experiment" not in tables:
        op.create_table(
            "promotion_experiment",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("experiment_key", sa.String(100), nullable=False),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("champion_model_version", sa.String(80), nullable=False),
            sa.Column("challenger_model_version", sa.String(80), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("allocation_mode", sa.String(30), nullable=False, server_default="shadow"),
            sa.Column("acceptance_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("decision", sa.String(30)),
            sa.Column("decision_reason", sa.Text()),
            sa.Column("started_at", sa.DateTime()),
            sa.Column("ended_at", sa.DateTime()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("experiment_key", name="uq_promotion_experiment_key"),
        )
        op.create_index(
            "ix_promotion_experiment_experiment_key",
            "promotion_experiment",
            ["experiment_key"],
            unique=True,
        )
        op.create_index(
            "ix_promotion_experiment_status",
            "promotion_experiment",
            ["status", "started_at"],
        )


def downgrade() -> None:
    tables = _tables()
    for table_name in (
        "promotion_prediction_snapshot",
        "promotion_prediction_run",
        "promotion_experiment",
        "promotion_model_artifact",
    ):
        if table_name in tables:
            op.drop_table(table_name)
            tables = _tables()
