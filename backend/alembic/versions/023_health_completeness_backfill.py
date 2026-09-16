"""backfill legacy failed-source completeness

Revision ID: 023_health_completeness
Revises: 022_close_quality_strategy
Create Date: 2026-09-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "023_health_completeness"
down_revision: Union[str, None] = "022_close_quality_strategy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "data_source_health" not in set(inspector.get_table_names()):
        return

    # 旧版失败记录继承了字段默认值1.0，导致“down / 100%完整”的矛盾展示。
    # 只修正带错误信息且仍为100%或NULL的失败记录；真实的部分完整率保持不变。
    op.execute(
        sa.text(
            """
            UPDATE data_source_health
               SET completeness = 0.0
             WHERE status IN ('down', 'degraded')
               AND COALESCE(error_msg, '') <> ''
               AND (completeness IS NULL OR completeness >= 0.999999)
            """
        )
    )


def downgrade() -> None:
    # 这是历史数据纠偏，旧的伪100%值无法可靠恢复。
    pass
