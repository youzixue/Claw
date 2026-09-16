"""初始空迁移

Revision ID: 001_init
Revises: None
Create Date: 2026-04-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "001_init"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 初始迁移 — 表由 SQLAlchemy Base.metadata.create_all 创建
    # 后续迁移在此添加 ALTER/DROP 等操作
    pass


def downgrade() -> None:
    pass
