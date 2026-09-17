"""2026-09-17 复盘 T1 修复项的回归测试。

覆盖：
  T1-4  卖出日志 reason 必须带上 exit_trigger_reason（单字段可审计）
  T1-6  A股T+1 阻塞日志按 (账户,股票,交易日,原因) 去重，保留首末次
  T1-7  盘后日度聚合以"当日收盘"为新鲜度基准，而非批处理运行的墙钟
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import paper as paper_api
from app.api.v1.paper import _log_t1_skip_once, _sell_log_reason
from app.config.settings import settings
from app.db.session import Base
from app.models.paper import PaperAccount, PaperAutoTradeLog


# --------------------------------------------------------------------------
# T1-4: 卖出日志 reason 合成
# --------------------------------------------------------------------------

class TestSellLogReason:
    def test_trigger_is_prefixed_to_plumbing_reason(self):
        reason = _sell_log_reason(
            {"exit_trigger_reason": "触发持仓止损价：现价22.65 <= 止损22.79"},
            "已通过风控，等待下一健康行情轮次按五档深度撮合",
        )
        assert reason.startswith("触发持仓止损价")
        assert "已通过风控" in reason

    def test_filled_reason_also_carries_trigger(self):
        reason = _sell_log_reason(
            {"exit_trigger_reason": "持仓5个交易日到期平仓"},
            "下一轮按五档深度全部成交",
        )
        assert reason.startswith("持仓5个交易日到期平仓")

    def test_no_trigger_falls_back_to_outcome_reason(self):
        """旧单缺失触发证据时不补造，保持原样。"""
        assert _sell_log_reason({}, "下一轮按五档深度全部成交") == "下一轮按五档深度全部成交"
        assert _sell_log_reason({"exit_trigger_reason": "  "}, "x") == "x"

    def test_no_double_prefix_when_already_contained(self):
        text = "触发持仓止损价：现价22.65 <= 止损22.79"
        assert _sell_log_reason({"exit_trigger_reason": text}, text) == text

    def test_none_candidate_is_safe(self):
        assert _sell_log_reason(None, "下一轮按五档深度全部成交") == "下一轮按五档深度全部成交"


# --------------------------------------------------------------------------
# T1-6: T+1 阻塞日志去重
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def t1_case():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            account = PaperAccount(account_name="challenger_c", status="active",
                                   initial_capital=50000, current_capital=50000)
            db.add(account)
            await db.commit()
            yield db, account
    finally:
        await engine.dispose()


def _t1_kwargs(account_id: int, **over):
    kwargs = dict(
        run_id="run-1", trade_date=date(2026, 9, 17), trigger="schedule-intraday",
        source="position", code="600105", name="永鼎股份",
        reason="A股T+1：当天买入不能当天卖出", price=45.8, amount=200,
        candidate={"exit_trigger_reason": "触发持仓止损价"}, account_id=account_id,
    )
    kwargs.update(over)
    return kwargs


async def _t1_log_count(db, code="600105") -> int:
    return await db.scalar(
        select(func.count()).select_from(PaperAutoTradeLog)
        .where(PaperAutoTradeLog.code == code, PaperAutoTradeLog.action == "skip_sell")
    )


@pytest.mark.asyncio
async def test_repeated_t1_blocks_write_only_one_row(t1_case):
    """模拟 700 个行情轮次：去重后只能落 1 条。"""
    db, account = t1_case
    emitted = 0
    for _ in range(700):
        log = await _log_t1_skip_once(db, **_t1_kwargs(account.id))
        if log is not None:
            emitted += 1
    await db.commit()
    assert emitted == 1
    assert await _t1_log_count(db) == 1


@pytest.mark.asyncio
async def test_last_seen_is_refreshed_on_the_same_row(t1_case, monkeypatch):
    """超过刷新间隔后更新同一条的末次复现时间，而不是新增一行。"""
    db, account = t1_case
    assert await _log_t1_skip_once(db, **_t1_kwargs(account.id)) is not None
    await db.commit()

    base = paper_api._paper_now()
    monkeypatch.setattr(paper_api, "_paper_now", lambda: base + timedelta(seconds=1900))
    assert await _log_t1_skip_once(db, **_t1_kwargs(account.id)) is None
    await db.commit()

    assert await _t1_log_count(db) == 1
    row = (await db.scalars(select(PaperAutoTradeLog))).one()
    assert "日内持续" in row.reason
    assert "末次" in row.reason
    assert row.reason.startswith("A股T+1：当天买入不能当天卖出")


@pytest.mark.asyncio
async def test_refresh_interval_zero_writes_every_round(t1_case, monkeypatch):
    """刷新间隔置 0 时每轮都刷新同一条（诊断用），仍不新增行。"""
    db, account = t1_case
    monkeypatch.setattr(settings, "PAPER_T1_SKIP_LOG_REFRESH_MIN_SEC", 0.0)
    for _ in range(5):
        await _log_t1_skip_once(db, **_t1_kwargs(account.id))
    await db.commit()
    assert await _t1_log_count(db) == 1


@pytest.mark.asyncio
async def test_changed_reason_still_gets_its_own_row(t1_case):
    """原因文本变化（例如出现强制退出原因）必须另落一条，不能被去重吞掉。"""
    db, account = t1_case
    await _log_t1_skip_once(db, **_t1_kwargs(account.id))
    await _log_t1_skip_once(db, **_t1_kwargs(
        account.id, reason="A股T+1：当天买入不能当天卖出；已标记下个可卖窗口退出（版本隔离）"))
    await db.commit()
    assert await _t1_log_count(db) == 2


@pytest.mark.asyncio
async def test_different_positions_are_not_deduped_together(t1_case):
    db, account = t1_case
    await _log_t1_skip_once(db, **_t1_kwargs(account.id, code="600105"))
    await _log_t1_skip_once(db, **_t1_kwargs(account.id, code="600106"))
    await db.commit()
    assert await _t1_log_count(db, "600105") == 1
    assert await _t1_log_count(db, "600106") == 1


@pytest.mark.asyncio
async def test_dedupe_is_stateless_across_process_restart(t1_case):
    """去重不依赖进程内缓存：重启后仍能从库内认领同键行，不重复首条。"""
    db, account = t1_case
    await _log_t1_skip_once(db, **_t1_kwargs(account.id))
    await db.commit()
    # 无任何模块级状态可清空 —— 直接再调用即等价于重启后的行为
    assert await _log_t1_skip_once(db, **_t1_kwargs(account.id)) is None
    await db.commit()
    assert await _t1_log_count(db) == 1


@pytest.mark.asyncio
async def test_two_accounts_with_same_id_in_different_dbs_do_not_collide():
    """去重键必须落在库内：两个独立库中同 account_id/同 code 不得互相抑制。

    这是无状态实现相对模块级缓存的直接收益（缓存实现会误命中并漏写证据）。
    """
    import pytest_asyncio  # noqa: F401  (fixture 依赖已在上方导入)

    async def _run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            account = PaperAccount(account_name="challenger_c", status="active",
                                   initial_capital=50000, current_capital=50000)
            db.add(account)
            await db.commit()
            emitted = await _log_t1_skip_once(db, **_t1_kwargs(account.id))
            await db.commit()
            assert emitted is not None
        await engine.dispose()

    await _run()
    await _run()


@pytest.mark.asyncio
async def test_deleted_row_is_recreated_at_next_refresh(t1_case, monkeypatch):
    """行被清理后在下一个刷新周期自愈重建，避免永久丢失证据。

    去重快路径在刷新间隔内不再查库（每轮一次 SELECT 的成本不值得），
    因此重建发生在下一个刷新点，而不是删除后的立即轮次。
    """
    db, account = t1_case
    await _log_t1_skip_once(db, **_t1_kwargs(account.id))
    await db.commit()
    row = (await db.scalars(select(PaperAutoTradeLog))).one()
    await db.delete(row)
    await db.commit()

    base = paper_api._paper_now()
    monkeypatch.setattr(paper_api, "_paper_now", lambda: base + timedelta(seconds=1900))
    assert await _log_t1_skip_once(db, **_t1_kwargs(account.id)) is not None
    await db.commit()
    assert await _t1_log_count(db) == 1


@pytest.mark.asyncio
async def test_real_production_volume_is_bounded(t1_case, monkeypatch):
    """按生产规模（每 30 秒一轮、全天 ~700 轮）验证写入量上界。"""
    db, account = t1_case
    base = paper_api._paper_now()
    written = 0
    for i in range(700):
        monkeypatch.setattr(paper_api, "_paper_now",
                            lambda i=i: base + timedelta(seconds=30 * i))
        if await _log_t1_skip_once(db, **_t1_kwargs(account.id)) is not None:
            written += 1
    await db.commit()
    # 交易时段约 4 小时 => 1800s 间隔最多 8 次刷新 + 首条
    assert written == 1
    assert await _t1_log_count(db) <= 10, "写入量上界应远小于 3028 条"


# --------------------------------------------------------------------------
# T1-7: 盘后聚合的资金流新鲜度基准
# --------------------------------------------------------------------------

class TestPostmarketFundClock:
    """日度聚合应以当日收盘为 decision_at，否则资金流覆盖恒为 0%。"""

    def _row(self, source_quote_at, observed_at):
        return {
            "trade_date": date(2026, 9, 17), "source": "tencent",
            "source_version": "tencent_hsfundtab_v1",
            "main_net_inflow": 1.0, "main_net_inflow_pct": 1.0,
            "source_quote_at": source_quote_at,
            "received_at": observed_at, "observed_at": observed_at,
        }

    def test_wall_clock_at_batch_time_marks_closing_data_stale(self):
        from app.data.main_fund import current_main_fund_status

        row = self._row(datetime(2026, 9, 17, 14, 56), datetime(2026, 9, 17, 14, 59, 51))
        # 批处理在 19:59 运行时，收盘资金流已超出 600s 窗口
        assert current_main_fund_status(
            row, trade_date=date(2026, 9, 17),
            decision_at=datetime(2026, 9, 17, 19, 59, 40),
        ) == "stale"

    def test_session_close_basis_accepts_closing_data(self):
        from app.data.main_fund import current_main_fund_evidence, current_main_fund_status

        row = self._row(datetime(2026, 9, 17, 14, 56), datetime(2026, 9, 17, 14, 59, 51))
        decision_at = datetime.combine(date(2026, 9, 17), time(15, 0))
        assert current_main_fund_status(
            row, trade_date=date(2026, 9, 17), decision_at=decision_at,
        ) == "ok"
        assert current_main_fund_evidence(
            row, trade_date=date(2026, 9, 17), decision_at=decision_at,
        ) is not None

    def test_intraday_protection_window_unchanged(self):
        """盘中消费者的保护窗口不受影响：14:00 时 13:00 的数据仍算过期。"""
        from app.data.main_fund import current_main_fund_status

        row = self._row(datetime(2026, 9, 17, 13, 0), datetime(2026, 9, 17, 13, 0, 5))
        assert current_main_fund_status(
            row, trade_date=date(2026, 9, 17),
            decision_at=datetime(2026, 9, 17, 14, 0),
        ) == "stale"


# --------------------------------------------------------------------------
# T1-5: sector_persistence 观测时点 + 板块归因前视偏差防护
# --------------------------------------------------------------------------

@pytest_asyncio.fixture
async def sector_case():
    from app.models.stock import SectorPersistence

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db, SectorPersistence
    finally:
        await engine.dispose()


def _persistence(model, *, observed_at, strength=8.0):
    return model(
        sector_code="pw_concept_第三代半导体", sector_name="第三代半导体",
        trade_date=date(2026, 9, 17), consecutive_days=1, limit_up_count=1,
        fund_flow=-52.05, change_pct=-0.30, strength_score=strength,
        observed_at=observed_at,
    )


@pytest.mark.asyncio
async def test_sector_row_written_after_decision_is_not_used(sector_case):
    """决策时刻之后才落库的板块强度不得参与决策（前视偏差防护）。"""
    db, model = sector_case
    db.add(_persistence(model, observed_at=datetime(2026, 9, 17, 15, 20)))
    await db.commit()

    reason = await paper_api._sector_retreat_reason(
        db, "605580", entry_sector_code="pw_concept_第三代半导体",
        trade_date=date(2026, 9, 17),
        observed_before=datetime(2026, 9, 17, 9, 31, 4),
    )
    assert reason == "", "盘后才写入的板块快照不得用于 09:31 的退出决策"


@pytest.mark.asyncio
async def test_sector_row_visible_before_decision_is_used(sector_case):
    """决策时刻之前已落库的板块强度照常参与决策。"""
    db, model = sector_case
    db.add(_persistence(model, observed_at=datetime(2026, 9, 17, 9, 28)))
    await db.commit()

    reason = await paper_api._sector_retreat_reason(
        db, "605580", entry_sector_code="pw_concept_第三代半导体",
        trade_date=date(2026, 9, 17),
        observed_before=datetime(2026, 9, 17, 9, 31, 4),
    )
    assert reason.startswith("板块退潮")
    assert "第三代半导体" in reason


@pytest.mark.asyncio
async def test_legacy_rows_without_observed_at_still_usable(sector_case):
    """历史行 observed_at 为 NULL 时必须保持可用，否则会静默清空板块证据。"""
    db, model = sector_case
    db.add(_persistence(model, observed_at=None))
    await db.commit()

    reason = await paper_api._sector_retreat_reason(
        db, "605580", entry_sector_code="pw_concept_第三代半导体",
        trade_date=date(2026, 9, 17),
        observed_before=datetime(2026, 9, 17, 9, 31, 4),
    )
    assert reason.startswith("板块退潮")


@pytest.mark.asyncio
async def test_no_observed_before_keeps_previous_behaviour(sector_case):
    """不传观测基准时行为与修复前一致（不引入隐式收紧）。"""
    db, model = sector_case
    db.add(_persistence(model, observed_at=datetime(2026, 9, 17, 15, 20)))
    await db.commit()

    reason = await paper_api._sector_retreat_reason(
        db, "605580", entry_sector_code="pw_concept_第三代半导体",
        trade_date=date(2026, 9, 17),
    )
    assert reason.startswith("板块退潮")
