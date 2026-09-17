"""收盘快照分母：只排除**结构性不可观测**的代码，绝不掩盖真实覆盖丢失。

事故（2026-09-17）
------------------
`canonical=5182/5240 completeness=0.988931`，判定要求 `>=0.99`（需 5188），
差 6 只 → `promotion_2000` 正式批次被连拒 142 次、0 个 completed
→ 次日各路线按 `desc(as_of_at)` 选中 blocked 占位行 → 全策略无候选。
缺口里 3 只（`000638 / 300029 / 600355`）是退市整理期 *ST：
`is_delisting=1`、board_tag=blocked、**整个历史一根日K都没有**，
行情源结构性覆盖不到，却一直占着分母，使这条闸门天生少 3 只余量。

判据（必须能从数据自证、且不能掩盖真缺口）
------------------------------------------
在回看窗口（`CLOSE_SNAPSHOT_UNOBSERVED_LOOKBACK_DAYS`）内**从未出现过
任何 K 线**的 scope 代码 = 结构性不可观测 → 排除出分母。
**曾经有 K 线、只是今天缺的代码照样计入分母**（那是真缺口，必须继续拦）——
本文件第 2 个用例专门锁这一条。

阈值、判定式、其余条件一律未动；原有分母另以 `expected_count_raw` 报出。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.data.scheduler import (
    CLOSE_SNAPSHOT_UNOBSERVED_LOOKBACK_DAYS,
    DataScheduler,
)
from app.db.session import Base
from app.models.stock import StockKline, StockSpot, StockTag

DAY = date(2026, 9, 17)
LOOKBACK = CLOSE_SNAPSHOT_UNOBSERVED_LOOKBACK_DAYS


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _tag(code, name="正常股", board_tag="tradeable", **kw):
    return StockTag(code=code, name=name, board_type="main_sh", board_tag=board_tag,
                    is_st=kw.get("is_st", False), is_delisting=kw.get("is_delisting", False),
                    is_suspended=kw.get("is_suspended", False))


def _bar(code, day, source="tencent_close", close=10.0):
    return StockKline(code=code, trade_date=day, open=10.0, close=close, high=10.5,
                      low=9.5, volume=1000, amount=10_000.0, source=source)


def _spot(code, day=DAY):
    from datetime import datetime
    return StockSpot(code=code, name="正常股", price=10.0,
                     updated_at=datetime.combine(day, datetime.min.time()).replace(hour=15, minute=30))


@pytest.mark.asyncio
async def test_never_observed_codes_leave_the_denominator(maker):
    async with maker() as db:
        db.add_all([
            _tag("600000"), _tag("600001"),
            # 结构性不可观测：整个历史一根日K都没有 + 退市标记
            _tag("600355", "*ST精伦", board_tag="blocked", is_st=True, is_delisting=True),
            _bar("600000", DAY), _bar("600001", DAY),
            _spot("600000"), _spot("600001"),
        ])
        await db.commit()
        health = await DataScheduler()._close_snapshot_health(db, DAY)

    assert health["expected_count_raw"] == 3
    assert health["expected_count"] == 2                       # 分母已修正
    assert health["structurally_unobservable_count"] == 1
    assert health["structurally_unobservable_codes"] == ["600355"]
    assert health["canonical_count"] == 2
    assert health["completeness"] == 1.0
    assert health["uncovered_count"] == 0
    assert health["unobserved_lookback_days"] == LOOKBACK


@pytest.mark.asyncio
async def test_previously_covered_code_missing_today_still_fails(maker):
    """**边界护栏**：曾经有K线、只是今天缺的代码必须继续计入分母。

    否则这个"修正分母"就变成了"用排除法把闸门刷绿"，真实的覆盖丢失会被掩盖。
    """
    recent = DAY - timedelta(days=10)          # 回看窗口内出现过
    async with maker() as db:
        db.add_all([
            _tag("600000"), _tag("600002"),
            _bar("600000", DAY),
            _bar("600002", recent),            # 10 天前有，今天没有
            _spot("600000"),
        ])
        await db.commit()
        health = await DataScheduler()._close_snapshot_health(db, DAY)

    assert health["structurally_unobservable_count"] == 0
    assert health["expected_count"] == health["expected_count_raw"] == 2
    assert health["canonical_count"] == 1
    assert health["completeness"] == pytest.approx(0.5)
    assert health["ready"] is False
    assert health["uncovered_codes"] == ["600002"]
    assert health["uncovered_reasons"]["unexplained_missing_close"] == 1


@pytest.mark.asyncio
async def test_lookback_window_boundary(maker):
    """窗口内(59天前)出现过 → 仍算可观测；窗口外(61天前)只有过 → 算结构性不可观测。"""
    inside = DAY - timedelta(days=LOOKBACK - 1)
    outside = DAY - timedelta(days=LOOKBACK + 1)
    async with maker() as db:
        db.add_all([
            _tag("600000"), _tag("600003"), _tag("600004"),
            _bar("600000", DAY),
            _bar("600003", inside),            # 窗口内 → 计入分母
            _bar("600004", outside),           # 窗口外 → 不计入分母
            _spot("600000"), _spot("600003"),
        ])
        await db.commit()
        health = await DataScheduler()._close_snapshot_health(db, DAY)

    assert health["expected_count_raw"] == 3
    assert health["structurally_unobservable_codes"] == ["600004"]
    assert health["expected_count"] == 2
    # 600003 今天缺 → 仍是真缺口
    assert health["uncovered_codes"] == ["600003"]
    assert health["ready"] is False


@pytest.mark.asyncio
async def test_denominator_never_drops_below_canonical(maker):
    """分母被修正后不得小于已验证数量而伪造 100%。"""
    async with maker() as db:
        db.add_all([_tag("600000"), _bar("600000", DAY), _spot("600000")])
        await db.commit()
        health = await DataScheduler()._close_snapshot_health(db, DAY)
    assert health["expected_count"] >= health["canonical_count"]
    assert health["completeness"] <= 1.0


@pytest.mark.asyncio
async def test_empty_scope_still_not_ready(maker):
    async with maker() as db:
        health = await DataScheduler()._close_snapshot_health(db, DAY)
    assert health["expected_count"] == 0
    assert health["completeness"] == 0.0
    assert health["ready"] is False


@pytest.mark.asyncio
async def test_samples_are_bounded_and_threshold_unchanged(maker):
    import inspect

    body = inspect.getsource(DataScheduler._close_snapshot_health)
    assert "structurally_unobservable[:20]" in body
    assert "completeness >= 0.99" in body          # 阈值逐字未动
    assert "expected_raw" in body                  # 原分母保留可审计

    async with maker() as db:
        db.add_all([_tag("600000"), _bar("600000", DAY), _spot("600000")])
        for index in range(30):
            db.add(_tag(f"61{index:04d}"))
        await db.commit()
        health = await DataScheduler()._close_snapshot_health(db, DAY)
    assert len(health["structurally_unobservable_codes"]) == 20
    assert health["structurally_unobservable_count"] == 30
