"""收盘快照未就绪时必须给出"缺的是谁"，而不是只给一个比值。

事故（2026-09-17 20:00–21:30）
------------------------------
`promotion_2000` 正式批次被连续拒绝 **142 次**，每次只报
`canonical=5182/5240 completeness=0.988931`，`ready=False`。
判定要求 `completeness >= 0.99` → 需要 5188/5240，实际 5182，
**差 6 只**。运维拿到这个比值无法知道缺的是谁，只能翻日志猜。
（实测缺口中 3 只是 `000638 / 300029 / 600355`，全部 is_delisting 且
*整个历史都没有过一根日K*，spot 价 0.12~0.89 的退市整理期标的。）

修法
----
**只追加诊断，不改判定口径、不改阈值、不放行任何东西**：
`uncovered_count` / `uncovered_codes`（有界样本）/ `uncovered_reasons`
（粗分类：delisting_flagged / suspended / board_* / unexplained_missing_close）。
`unexplained_missing_close` 才是真正需要追查的那一类。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.data.scheduler import DataScheduler
from app.db.session import Base
from app.models.stock import StockKline, StockTag


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'close.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


DAY = date(2026, 9, 17)


def _tag(code, name, board_tag="tradeable", **kw):
    return StockTag(
        code=code, name=name, board_type="main_sh", board_tag=board_tag,
        is_st=kw.get("is_st", False), is_delisting=kw.get("is_delisting", False),
        is_suspended=kw.get("is_suspended", False),
    )


def _bar(code, trade_date=None, source="tencent_close"):
    # 第二个参数是**日期**，不是 source —— 曾把它当 source 传，导致
    # "5 天前的K线"其实写成了当天，把边界用例悄悄变成恒真。
    return StockKline(
        code=code, trade_date=trade_date or DAY, open=10.0, close=10.5,
        high=10.6, low=9.9, volume=1000, amount=10_000.0, source=source,
    )


@pytest.mark.asyncio
async def test_uncovered_codes_are_named_with_reasons(tmp_path):
    engine, maker = await _maker(tmp_path)
    try:
        # 注意：自 2026-09-18 起，"回看窗口内从未有过任何K线"的代码会被
        # **移出分母**（结构性不可观测，见
        # tests/test_close_snapshot_denominator_20260918.py）。所以本用例要测
        # "未覆盖分类"，必须让这三只**曾经有过K线**（在窗口内）才会留在分母里。
        older = DAY - timedelta(days=5)
        async with maker() as db:
            db.add_all([
                _tag("600000", "浦发银行"),
                _tag("600001", "观察池股", board_tag="observe_only"),
                # 有当日收盘：不应出现在未覆盖名单
                _bar("600000"), _bar("600001"),
                # 退市标记 + 窗口内曾有K线、今天没有 → delisting_flagged
                _tag("600355", "*ST精伦", board_tag="blocked", is_st=True, is_delisting=True),
                _bar("600355", older),
                # 板块标记异常但 ST 兜底进 scope → board_blocked
                _tag("600002", "齐鲁石化", board_tag="blocked", is_st=True),
                _bar("600002", older),
                # scope 内、无明显标记、就是今天没有收盘 → 需要追查
                _tag("600003", "正常股"),
                _bar("600003", older),
            ])
            await db.commit()
            health = await DataScheduler()._close_snapshot_health(db, DAY)

        assert health["ready"] is False
        # uncovered = scope 内但当日无任何K线的代码（与 canonical 差额语义不同）
        assert health["structurally_unobservable_count"] == 0
        assert health["uncovered_count"] == 3
        assert health["uncovered_count"] == len(health["uncovered_codes"])
        codes = set(health["uncovered_codes"])
        assert codes == {"600002", "600003", "600355"}
        assert "600000" not in codes and "600001" not in codes
        assert health["uncovered_reasons"]["delisting_flagged"] == 1      # 600355
        assert health["uncovered_reasons"]["board_blocked"] == 1          # 600002
        assert health["uncovered_reasons"]["unexplained_missing_close"] == 1  # 600003
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_diagnostics_do_not_change_the_readiness_verdict(tmp_path):
    """诊断字段是纯附加：ready 的真假只由原有条件决定。"""
    engine, maker = await _maker(tmp_path)
    try:
        async with maker() as db:
            db.add_all([_tag("600000", "浦发银行"), _bar("600000")])
            await db.commit()
            health = await DataScheduler()._close_snapshot_health(db, DAY)
        assert health["expected_count"] == 1
        # 该代码有当日K线，所以不算 uncovered；canonical 另需 spot 新鲜（本夹具未造）
        assert health["canonical_count"] == 0
        assert health["uncovered_count"] == 0
        assert health["uncovered_codes"] == []
        assert health["uncovered_reasons"] == {}
        # 单只标的不足 0.99 的样本量判定仍按原条件走，这里只断言字段存在
        for key in ("ready", "status", "fallback_count", "stale_tencent_close_count",
                    "conflicting_close_count", "invalid_formal_ohlc_count",
                    "unsupported_source_count", "completeness"):
            assert key in health
    finally:
        await engine.dispose()


def test_sample_is_bounded_and_does_not_change_thresholds():
    """名单必须有界（避免大缺口时把日志撑爆），且不改 0.99 阈值。"""
    import inspect

    body = inspect.getsource(DataScheduler._close_snapshot_health)
    assert "uncovered[:20]" in body
    assert "completeness >= 0.99" in body
    # 诊断不得参与 ready 判定
    ready_block = body[body.index("ready = bool("):]
    ready_block = ready_block[:ready_block.index("\n        )")]
    assert "uncovered" not in ready_block
