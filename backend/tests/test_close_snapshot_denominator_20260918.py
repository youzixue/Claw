"""收盘快照分母：只排除**结构性不可观测**的代码，绝不掩盖真实覆盖丢失。

事故（2026-09-17）
------------------
`canonical=5182/5240 completeness=0.988931`，判定要求 `>=0.99`（需 5188），
差 6 只 → `promotion_2000` 正式批次被连拒 142 次、0 个 completed
→ 次日各路线按 `desc(as_of_at)` 选中 blocked 占位行 → 全策略无候选。
缺口里 3 只（`000638 / 300029 / 600355`）是退市整理期 *ST：
`is_delisting=1`、board_tag=blocked、**整个历史一根日K都没有**，
行情源结构性覆盖不到，却一直占着分母，使这条闸门天生少 3 只余量。

**判据已撤回（2026-09-18）**
----------------------------
本文件原先锁的是"把结构性不可观测代码**从分母里扣除**"。该做法被撤回，
原因是它无法区分两件事：
  * 源结构性覆盖不到（3 只 tag 行过期的退市 *ST，K线总数为 0）；
  * **今天真的漏采**（同样是"回看窗口内没有K线"）。
两者在数据上不可区分，扣除会把真实缺口正好从分母里抹掉 ——
对 200 只主板代码、缺 3 只的场景，`0.985 → 1.000`，真缺口被判成 `ready`。
`tests/test_research_universe_scope_20260917.py::test_main_board_gap_still_blocks`
正是为拦住这个而写（它在本轮全量回归里抓到了这个缺陷）。

**现在的判据**：分母 = 完整 scope，不做任何扣除；
那 3 只如实报进 `uncovered_codes` / `structurally_unobservable_codes`，
由既有 `completeness >= 0.99` 的容差吸收。
生产实测（2026-09-17）：`canonical=5182 / expected=5185 = 0.999421`
→ `ready=True`，缺口 3 只，余量充足。**阈值、判定式一律未动。**

关于"结构性不可观测"为何不能用 `is_delisting` 精确排除：实测本库
`is_delisting=1` 且落在旧 scope 内的有 130 只，其中 **127 只在 2026-09-17
有正常K线**（002647 仁东控股、600107 *ST尔雅、600228 返利科技…）。
该过滤被收盘快照判定、腾讯行情采集、盘后K线采集**共用**，
加上它会一次性把 127 只活跃标的踢出采集宇宙。
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
async def test_never_observed_codes_stay_in_the_denominator(maker):
    """从未有K线的代码**仍然计入分母**，只作诊断报出。

    撤回扣除的理由：这类代码既可能是"源结构性覆盖不到"，也可能是"今天真的
    漏采"，数据上无法区分。扣除会让真实缺口消失（0.667 → 1.000）。
    生产里那 3 只靠 0.99 容差吸收，而不是靠抹分母。
    """
    async with maker() as db:
        db.add_all([
            _tag("600000"), _tag("600001"),
            # 生产里的真实形态：退市 *ST、整段历史一根日K都没有
            _tag("600355", "*ST精伦", board_tag="blocked", is_st=True, is_delisting=True),
            _bar("600000", DAY), _bar("600001", DAY),
            _spot("600000"), _spot("600001"),
        ])
        await db.commit()
        health = await DataScheduler()._close_snapshot_health(db, DAY)

    assert health["expected_count_raw"] == 3
    assert health["expected_count"] == 3, "分母不得扣除，否则真缺口会被抹掉"
    assert health["structurally_unobservable_count"] == 1
    assert health["structurally_unobservable_codes"] == ["600355"]
    assert health["canonical_count"] == 2
    assert health["completeness"] == pytest.approx(2 / 3)
    assert health["ready"] is False, "小宇宙里缺 1/3 必须拦住"
    assert health["uncovered_count"] == 1, "缺口必须如实报出"
    assert health["uncovered_codes"] == ["600355"]
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
async def test_lookback_window_only_affects_diagnostic_not_denominator(maker):
    """回看窗口只影响**诊断分类**，不再影响分母。

    窗口外只有过 K 线的代码会被标成结构性不可观测（诊断），
    但分母、completeness、ready 一律与它无关。
    """
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
    assert health["expected_count"] == 3, "诊断分类不得改变分母"
    # 600003（窗口内有K线、今天缺）与 600004（窗口外才有）都要计入缺口
    assert health["uncovered_codes"] == ["600003", "600004"]
    assert health["canonical_count"] == 1
    assert health["completeness"] == pytest.approx(1 / 3)
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
