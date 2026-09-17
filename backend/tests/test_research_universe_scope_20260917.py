"""生产研究宇宙过滤与收盘快照覆盖门槛的回归测试（2026-09-17 复盘 T3-13）。

生产事实
--------
`promotion_2000`（盘后 20:00 正式批次）在 2026-09-17 连续重试 142 次全部
`blocked`，`quality_gate` 为空。日志给出的健康快照为：

    {'status': 'blocked', 'ready': False,
     'expected_count': 5240, 'canonical_count': 5182,
     'completeness': 0.988931, ...}

即 5,182 / 5,240 = 98.89%，而 `_close_snapshot_health` 的门槛是 `>= 0.99`
（需 5,188 只），**差 6 只**。

根因：分母纳入了两类"行情源结构性无法覆盖"的标的
  1. 北交所 51 只（`920xxx`）—— 在 stock_spot / stock_kline 中 0 行，
     腾讯实时源不提供；用户已明确不关注北交所
  2. 名称以"退"**结尾**的退市股 4 只（国华退/恒久退/赛隆退/立方退）——
     原过滤只排除 `退%` 前缀形式，漏掉 A 股实际使用的后缀形式
另有 3 只 `blocked` 标签 ST（*ST万方/*ST天龙/*ST精伦）本就落在 1% 容差内。

排除后 expected=5185 / canonical=5182 = 0.999421 → 门槛通过（余量 48 只）。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.data.scheduler import (
    _UNOBSERVABLE_CODE_PATTERNS,
    DataScheduler,
    _research_universe_filters,
)
from app.db.session import Base
from app.models.stock import StockKline, StockSpot, StockTag

TARGET = date(2026, 9, 17)
CLOSE_AT = datetime(2026, 9, 17, 15, 1, 0)


# --------------------------------------------------------------------------
# 1. 过滤条件本身
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", ["4%", "8%", "92%"])
def test_bse_patterns_cover_all_board_for_code_prefixes(pattern):
    """排除模式必须覆盖 price_limit_rules.board_for_code 判定为 bse 的全部前缀。"""
    from app.core.price_limit_rules import board_for_code

    sample = {"4%": "430047", "8%": "830799", "92%": "920001"}[pattern]
    assert board_for_code(sample) == "bse"
    assert sample.startswith(pattern.rstrip("%"))


def test_filter_tuple_shape_is_stable():
    """过滤条件是 6 个子句：宇宙范围 / 停牌 / 名称两种形式 / 三个代码段。"""
    assert len(_research_universe_filters()) == 6
    assert _UNOBSERVABLE_CODE_PATTERNS == ("4%", "8%", "92%")


# --------------------------------------------------------------------------
# 2. 端到端：_close_snapshot_health 的 expected 口径
# --------------------------------------------------------------------------

def _tag(code, name, *, board_tag="observe_only", is_st=False, is_suspended=False):
    from app.core.price_limit_rules import board_for_code

    return StockTag(code=code, name=name, board_type=board_for_code(code),
                    board_tag=board_tag, is_st=is_st, is_suspended=is_suspended)


def _covered(code):
    """构造"当日收盘已覆盖"的 K 线与终场报价。"""
    return (
        StockKline(code=code, trade_date=TARGET, open=10.0, close=10.5,
                   high=10.8, low=9.9, volume=1000, amount=1e7, source="tencent_close"),
        StockSpot(code=code, name=code, price=10.5, updated_at=CLOSE_AT),
    )


IN_SCOPE = [
    ("600519", "贵州茅台"),          # 沪市主板
    ("000001", "平安银行"),          # 深市主板
    ("002415", "海康威视"),          # 中小板（仍在主板口径内）
    ("300750", "宁德时代"),          # 创业板：保留在观测宇宙
    ("688111", "金山办公"),          # 科创板：保留在观测宇宙
]
EXCLUDED = [
    ("920001", "纬达光电"),          # 北交所 -> 结构性不可覆盖
    ("430047", "诺思兰德"),          # 北交所旧段
    ("830799", "艾融软件"),          # 北交所旧段
    ("000004", "国华退"),            # 退市：名称后缀形式
    ("002898", "赛隆退"),            # 退市：名称后缀形式
    ("600001", "退市示例"),          # 退市：名称前缀形式（原有行为，不得回退）
]


@pytest.mark.asyncio
async def test_expected_count_excludes_bse_and_delisted_names():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            for code, name in IN_SCOPE + EXCLUDED:
                db.add(_tag(code, name))
            for code, _ in IN_SCOPE:
                kline, spot = _covered(code)
                db.add(kline)
                db.add(spot)
            await db.commit()

            health = await DataScheduler._close_snapshot_health(db, TARGET)

        assert health["expected_count"] == len(IN_SCOPE), health
        assert health["canonical_count"] == len(IN_SCOPE), health
        assert health["completeness"] == 1.0
        assert health["ready"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_bse_absence_no_longer_blocks_a_complete_main_board():
    """回归本次生产事故：主板全覆盖 + 北交所全缺 -> 门槛必须通过。

    修复前该场景下 expected 会含北交所，completeness 被拉低到 0.99 以下。
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            main_board = [("600519", "贵州茅台"), ("600036", "招商银行"),
                          ("000001", "平安银行"), ("002415", "海康威视")]
            for code, name in main_board:
                db.add(_tag(code, name))
            # 100 只北交所，全部没有行情数据
            for i in range(100):
                db.add(_tag(f"92{i:04d}", f"北交所{i}"))
            for code, _ in main_board:
                kline, spot = _covered(code)
                db.add(kline)
                db.add(spot)
            await db.commit()

            health = await DataScheduler._close_snapshot_health(db, TARGET)

        assert health["expected_count"] == 4, "北交所不得进入分母"
        assert health["canonical_count"] == 4
        assert health["ready"] is True, health
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_main_board_gap_still_blocks():
    """修复不得放宽主板自身的覆盖要求：主板缺一只仍必须拦住。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            # 200 只主板，只有 197 只有数据 -> 0.985 < 0.99
            for i in range(200):
                db.add(_tag(f"60{i:04d}", f"主板{i}"))
            for i in range(197):
                kline, spot = _covered(f"60{i:04d}")
                db.add(kline)
                db.add(spot)
            await db.commit()

            health = await DataScheduler._close_snapshot_health(db, TARGET)

        assert health["expected_count"] == 200
        assert health["canonical_count"] == 197
        assert health["completeness"] < 0.99
        assert health["ready"] is False
        assert health["status"] == "blocked"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_suspended_and_delisted_prefix_still_excluded():
    """原有排除行为不得回退：停牌股与"退市XX"前缀形式继续不计入分母。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            db.add(_tag("600519", "贵州茅台"))
            db.add(_tag("600520", "停牌股", is_suspended=True))
            db.add(_tag("600521", "退市示例"))
            kline, spot = _covered("600519")
            db.add(kline)
            db.add(spot)
            await db.commit()

            health = await DataScheduler._close_snapshot_health(db, TARGET)

        assert health["expected_count"] == 1
        assert health["ready"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_st_stock_outside_board_tag_still_counted():
    """ST 股靠 is_st 进入宇宙，不受 board_tag 影响（用户明确要求覆盖 ST）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            db.add(_tag("600519", "贵州茅台"))
            db.add(_tag("600601", "*ST示例", board_tag="blocked", is_st=True))
            for code in ("600519", "600601"):
                kline, spot = _covered(code)
                db.add(kline)
                db.add(spot)
            await db.commit()

            health = await DataScheduler._close_snapshot_health(db, TARGET)

        assert health["expected_count"] == 2
        assert health["ready"] is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_empty_universe_is_not_ready():
    """空宇宙必须不 ready（避免 0/0 被当成满分）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine) as db:
            health = await DataScheduler._close_snapshot_health(db, TARGET)
        assert health["ready"] is False
    finally:
        await engine.dispose()
