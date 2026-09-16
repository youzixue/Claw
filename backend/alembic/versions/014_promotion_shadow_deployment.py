"""promotion shadow inference and manual deployment events

Revision ID: 014_promotion_shadow_deployment
Revises: 013_review_automation
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "014_promotion_shadow_deployment"
down_revision: Union[str, None] = "013_review_automation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "promotion_shadow_run" not in tables:
        op.create_table(
            "promotion_shadow_run",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("shadow_key", sa.String(64), nullable=False),
            sa.Column(
                "prediction_run_id",
                sa.Integer(),
                sa.ForeignKey("promotion_prediction_run.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "artifact_id",
                sa.Integer(),
                sa.ForeignKey("promotion_model_artifact.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("target_board", sa.Integer(), nullable=False),
            sa.Column("snapshot_context", sa.String(40), nullable=False),
            sa.Column("reference_trade_date", sa.Date(), nullable=False),
            sa.Column("as_of_at", sa.DateTime(), nullable=False),
            sa.Column("champion_model_version", sa.String(80), nullable=False),
            sa.Column("challenger_model_version", sa.String(80), nullable=False),
            sa.Column("feature_version", sa.String(80), nullable=False),
            sa.Column("data_version", sa.String(80), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
            sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("payload_hash", sa.String(64), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("shadow_key", name="uq_promotion_shadow_run_key"),
            sa.UniqueConstraint(
                "prediction_run_id",
                "artifact_id",
                "target_board",
                name="uq_promotion_shadow_run_scope",
            ),
        )
        op.create_index("ix_promotion_shadow_run_shadow_key", "promotion_shadow_run", ["shadow_key"], unique=True)
        op.create_index("ix_promotion_shadow_run_prediction_run_id", "promotion_shadow_run", ["prediction_run_id"])
        op.create_index("ix_promotion_shadow_run_artifact_id", "promotion_shadow_run", ["artifact_id"])
        op.create_index("ix_promotion_shadow_run_target_board", "promotion_shadow_run", ["target_board"])
        op.create_index("ix_promotion_shadow_run_reference_trade_date", "promotion_shadow_run", ["reference_trade_date"])
        op.create_index(
            "ix_promotion_shadow_run_lookup",
            "promotion_shadow_run",
            ["artifact_id", "target_board", "snapshot_context", "reference_trade_date"],
        )

    tables = _tables()
    if "promotion_shadow_prediction" not in tables:
        op.create_table(
            "promotion_shadow_prediction",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "shadow_run_id",
                sa.Integer(),
                sa.ForeignKey("promotion_shadow_run.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "prediction_snapshot_id",
                sa.Integer(),
                sa.ForeignKey("promotion_prediction_snapshot.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("code", sa.String(10), nullable=False),
            sa.Column("name", sa.String(30)),
            sa.Column("target_board", sa.Integer(), nullable=False),
            sa.Column("prediction_trade_date", sa.Date(), nullable=False),
            sa.Column("candidate_route", sa.String(60), nullable=False, server_default=""),
            sa.Column("market_regime", sa.String(40), nullable=False, server_default="unknown"),
            sa.Column("champion_probability", sa.Float(), nullable=False),
            sa.Column("challenger_raw_probability", sa.Float(), nullable=False),
            sa.Column("challenger_probability", sa.Float(), nullable=False),
            sa.Column("champion_rank_position", sa.Integer()),
            sa.Column("challenger_rank_position", sa.Integer(), nullable=False),
            sa.Column("features_hash", sa.String(64), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "shadow_run_id",
                "prediction_snapshot_id",
                name="uq_promotion_shadow_prediction_snapshot",
            ),
        )
        op.create_index("ix_promotion_shadow_prediction_shadow_run_id", "promotion_shadow_prediction", ["shadow_run_id"])
        op.create_index("ix_promotion_shadow_prediction_prediction_snapshot_id", "promotion_shadow_prediction", ["prediction_snapshot_id"])
        op.create_index("ix_promotion_shadow_prediction_code", "promotion_shadow_prediction", ["code"])
        op.create_index(
            "ix_promotion_shadow_prediction_lookup",
            "promotion_shadow_prediction",
            ["prediction_trade_date", "target_board", "code"],
        )
        op.create_index(
            "ix_promotion_shadow_prediction_rank",
            "promotion_shadow_prediction",
            ["shadow_run_id", "challenger_rank_position"],
        )

    tables = _tables()
    if "promotion_shadow_evaluation" not in tables:
        op.create_table(
            "promotion_shadow_evaluation",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("evaluation_key", sa.String(64), nullable=False),
            sa.Column(
                "artifact_id",
                sa.Integer(),
                sa.ForeignKey("promotion_model_artifact.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "trigger_shadow_run_id",
                sa.Integer(),
                sa.ForeignKey("promotion_shadow_run.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("target_board", sa.Integer(), nullable=False),
            sa.Column("snapshot_context", sa.String(40), nullable=False),
            sa.Column("challenger_model_version", sa.String(80), nullable=False),
            sa.Column("label_version", sa.String(80), nullable=False),
            sa.Column("outcome_end_date", sa.Date(), nullable=False),
            sa.Column("evaluated_shadow_run_count", sa.Integer(), nullable=False),
            sa.Column("trade_day_count", sa.Integer(), nullable=False),
            sa.Column("sample_count", sa.Integer(), nullable=False),
            sa.Column("positive_count", sa.Integer(), nullable=False),
            sa.Column("decision", sa.String(40), nullable=False),
            sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("acceptance_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("evaluation_key", name="uq_promotion_shadow_evaluation_key"),
        )
        op.create_index("ix_promotion_shadow_evaluation_evaluation_key", "promotion_shadow_evaluation", ["evaluation_key"], unique=True)
        op.create_index("ix_promotion_shadow_evaluation_artifact_id", "promotion_shadow_evaluation", ["artifact_id"])
        op.create_index("ix_promotion_shadow_evaluation_target_board", "promotion_shadow_evaluation", ["target_board"])
        op.create_index(
            "ix_promotion_shadow_evaluation_lookup",
            "promotion_shadow_evaluation",
            ["artifact_id", "target_board", "snapshot_context", "created_at"],
        )

    tables = _tables()
    if "promotion_deployment_event" not in tables:
        op.create_table(
            "promotion_deployment_event",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_key", sa.String(80), nullable=False),
            sa.Column("target_board", sa.Integer(), nullable=False),
            sa.Column("action", sa.String(20), nullable=False),
            sa.Column("deployment_mode", sa.String(40), nullable=False, server_default="probability_overlay"),
            sa.Column(
                "artifact_id",
                sa.Integer(),
                sa.ForeignKey("promotion_model_artifact.id", ondelete="RESTRICT"),
            ),
            sa.Column(
                "evidence_evaluation_id",
                sa.Integer(),
                sa.ForeignKey("promotion_shadow_evaluation.id", ondelete="RESTRICT"),
            ),
            sa.Column("from_model_version", sa.String(80), nullable=False),
            sa.Column("to_model_version", sa.String(80), nullable=False),
            sa.Column("operator", sa.String(80), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("confirmation_phrase", sa.String(40), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("event_key", name="uq_promotion_deployment_event_key"),
        )
        op.create_index("ix_promotion_deployment_event_event_key", "promotion_deployment_event", ["event_key"], unique=True)
        op.create_index("ix_promotion_deployment_event_target_board", "promotion_deployment_event", ["target_board"])
        op.create_index(
            "ix_promotion_deployment_event_current",
            "promotion_deployment_event",
            ["target_board", "created_at", "id"],
        )


def downgrade() -> None:
    tables = _tables()
    for table_name in (
        "promotion_deployment_event",
        "promotion_shadow_evaluation",
        "promotion_shadow_prediction",
        "promotion_shadow_run",
    ):
        if table_name in tables:
            op.drop_table(table_name)
