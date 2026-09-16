"""promotion model training runs

Revision ID: 010_promotion_training_runs
Revises: 009_promotion_prediction_ledger
Create Date: 2026-08-29
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "010_promotion_training_runs"
down_revision: Union[str, None] = "009_promotion_prediction_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "promotion_training_run" in _tables():
        return
    op.create_table(
        "promotion_training_run",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("training_key", sa.String(80), nullable=False),
        sa.Column("target_board", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("model_version", sa.String(80)),
        sa.Column("feature_version", sa.String(80), nullable=False),
        sa.Column("data_version", sa.String(80)),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("dataset_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("acceptance_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("artifact_id", sa.Integer(), sa.ForeignKey("promotion_model_artifact.id")),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("training_key", name="uq_promotion_training_run_key"),
    )
    op.create_index(
        "ix_promotion_training_run_training_key",
        "promotion_training_run",
        ["training_key"],
        unique=True,
    )
    op.create_index(
        "ix_promotion_training_run_target_board",
        "promotion_training_run",
        ["target_board"],
    )
    op.create_index(
        "ix_promotion_training_run_lookup",
        "promotion_training_run",
        ["target_board", "status", "started_at"],
    )


def downgrade() -> None:
    if "promotion_training_run" in _tables():
        op.drop_table("promotion_training_run")
