"""添加晋级预测复盘记录表

Revision ID: 004_promotion_prediction_record
Revises: 003_news_nlp_status
Create Date: 2026-05-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004_promotion_prediction_record"
down_revision: Union[str, None] = "003_news_nlp_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "promotion_prediction_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=20), nullable=True),
        sa.Column("target_board", sa.Integer(), nullable=False),
        sa.Column("prediction_trade_date", sa.Date(), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=True),
        sa.Column("predicted_probability", sa.Float(), nullable=True),
        sa.Column("calibrated_probability", sa.Float(), nullable=True),
        sa.Column("model_adjustment", sa.Float(), nullable=True),
        sa.Column("confidence_level", sa.String(length=20), nullable=True),
        sa.Column("candidate_route", sa.String(length=40), nullable=False),
        sa.Column("learning_bucket", sa.String(length=80), nullable=True),
        sa.Column("signal_status", sa.String(length=30), nullable=True),
        sa.Column("reason_snapshot", sa.Text(), nullable=True),
        sa.Column("factors_json", sa.Text(), nullable=True),
        sa.Column("outcome_status", sa.String(length=20), nullable=True),
        sa.Column("outcome_trade_date", sa.Date(), nullable=True),
        sa.Column("actual_limit_up_date", sa.Date(), nullable=True),
        sa.Column("actual_max_change_pct", sa.Float(), nullable=True),
        sa.Column("actual_close_change_pct", sa.Float(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failure_tags_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "code",
            "target_board",
            "prediction_trade_date",
            "candidate_route",
            name="uq_promotion_prediction_code_target_date_route",
        ),
    )
    op.create_index("ix_promotion_prediction_record_code", "promotion_prediction_record", ["code"])
    op.create_index("ix_promotion_prediction_record_target_board", "promotion_prediction_record", ["target_board"])
    op.create_index("ix_promotion_prediction_record_prediction_trade_date", "promotion_prediction_record", ["prediction_trade_date"])
    op.create_index("ix_promotion_prediction_record_learning_bucket", "promotion_prediction_record", ["learning_bucket"])
    op.create_index("ix_promotion_prediction_record_outcome_status", "promotion_prediction_record", ["outcome_status"])
    op.create_index(
        "ix_promotion_prediction_learning",
        "promotion_prediction_record",
        ["target_board", "candidate_route", "outcome_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_promotion_prediction_learning", table_name="promotion_prediction_record")
    op.drop_index("ix_promotion_prediction_record_outcome_status", table_name="promotion_prediction_record")
    op.drop_index("ix_promotion_prediction_record_learning_bucket", table_name="promotion_prediction_record")
    op.drop_index("ix_promotion_prediction_record_prediction_trade_date", table_name="promotion_prediction_record")
    op.drop_index("ix_promotion_prediction_record_target_board", table_name="promotion_prediction_record")
    op.drop_index("ix_promotion_prediction_record_code", table_name="promotion_prediction_record")
    op.drop_table("promotion_prediction_record")
