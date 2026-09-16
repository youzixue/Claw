"""添加SectorKline指标字段 + SectorLifecycle K线因子字段

SectorKline新增:
- change_pct: 涨跌幅%
- amplitude: 振幅%
- ma5/ma10/ma20: 均线
- ma5_vol: 5日成交量均线
- trend_state: 趋势状态
- vol_ratio: 量比
- support_price: 支撑位
- resistance_price: 压力位

SectorLifecycle新增:
- kline_trend: K线趋势
- kline_vol_ratio: 量比
- kline_support: 支撑位
- kline_resistance: 压力位
- kline_ma5: 5日均线
- kline_ma20: 20日均线
- kline_close: 当日收盘指数

注: 这些字段在模型中已定义, 此迁移用于已有数据库补列

Revision ID: 002_kline_indicators
Revises: 001_init
Create Date: 2026-04-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_kline_indicators"
down_revision: Union[str, None] = "001_init"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SectorKline — 添加指标字段(IF NOT EXISTS 通过 try/except 实现)
    _add_column_if_not_exists("sector_kline", sa.Column("change_pct", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("amplitude", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("ma5", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("ma10", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("ma20", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("ma5_vol", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("trend_state", sa.String(20), nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("vol_ratio", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("support_price", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_kline", sa.Column("resistance_price", sa.Float, nullable=True))

    # SectorLifecycle — 添加K线因子字段
    _add_column_if_not_exists("sector_lifecycle", sa.Column("kline_trend", sa.String(20), nullable=True))
    _add_column_if_not_exists("sector_lifecycle", sa.Column("kline_vol_ratio", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_lifecycle", sa.Column("kline_support", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_lifecycle", sa.Column("kline_resistance", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_lifecycle", sa.Column("kline_ma5", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_lifecycle", sa.Column("kline_ma20", sa.Float, nullable=True))
    _add_column_if_not_exists("sector_lifecycle", sa.Column("kline_close", sa.Float, nullable=True))


def downgrade() -> None:
    # SectorKline
    _drop_column_if_exists("sector_kline", "resistance_price")
    _drop_column_if_exists("sector_kline", "support_price")
    _drop_column_if_exists("sector_kline", "vol_ratio")
    _drop_column_if_exists("sector_kline", "trend_state")
    _drop_column_if_exists("sector_kline", "ma5_vol")
    _drop_column_if_exists("sector_kline", "ma20")
    _drop_column_if_exists("sector_kline", "ma10")
    _drop_column_if_exists("sector_kline", "ma5")
    _drop_column_if_exists("sector_kline", "amplitude")
    _drop_column_if_exists("sector_kline", "change_pct")

    # SectorLifecycle
    _drop_column_if_exists("sector_lifecycle", "kline_close")
    _drop_column_if_exists("sector_lifecycle", "kline_ma20")
    _drop_column_if_exists("sector_lifecycle", "kline_ma5")
    _drop_column_if_exists("sector_lifecycle", "kline_resistance")
    _drop_column_if_exists("sector_lifecycle", "kline_support")
    _drop_column_if_exists("sector_lifecycle", "kline_vol_ratio")
    _drop_column_if_exists("sector_lifecycle", "kline_trend")


def _add_column_if_not_exists(table: str, column: sa.Column) -> None:
    """安全添加列, 如已存在则跳过"""
    conn = op.get_bind()
    col_name = column.name
    # SQLite: PRAGMA table_info
    result = conn.execute(sa.text(f"PRAGMA table_info({table})"))
    existing_cols = {row[1] for row in result}
    if col_name not in existing_cols:
        op.add_column(table, column)


def _drop_column_if_exists(table: str, col_name: str) -> None:
    """安全删除列, 如不存在则跳过"""
    conn = op.get_bind()
    result = conn.execute(sa.text(f"PRAGMA table_info({table})"))
    existing_cols = {row[1] for row in result}
    if col_name in existing_cols:
        op.drop_column(table, col_name)
