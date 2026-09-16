"""widen promotion compatibility model version

Revision ID: 015_promotion_model_version_width
Revises: 014_promotion_shadow_deployment
Create Date: 2026-08-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "015_promotion_model_version_width"
down_revision: Union[str, None] = "014_promotion_shadow_deployment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _model_version_length() -> int | None:
    inspector = sa.inspect(op.get_bind())
    if "promotion_prediction_record" not in inspector.get_table_names():
        return None
    for column in inspector.get_columns("promotion_prediction_record"):
        if column["name"] == "model_version":
            return getattr(column["type"], "length", None)
    return None


def upgrade() -> None:
    length = _model_version_length()
    if length is None or length >= 80:
        return
    with op.batch_alter_table("promotion_prediction_record") as batch_op:
        batch_op.alter_column(
            "model_version",
            existing_type=sa.String(length),
            type_=sa.String(80),
            existing_nullable=True,
        )


def downgrade() -> None:
    length = _model_version_length()
    if length is None or length <= 40:
        return
    with op.batch_alter_table("promotion_prediction_record") as batch_op:
        batch_op.alter_column(
            "model_version",
            existing_type=sa.String(length),
            type_=sa.String(40),
            existing_nullable=True,
        )
