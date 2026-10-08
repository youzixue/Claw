"""退出规则读取冻结参数和当轮行情，不混入日终/前一日数据。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.api.v1 import paper
from app.paper import account_policy
from app.paper.experiment import execution_version, standard_execution_version
from app.models.stock import StockKline, StockSpot
from test_paper_api import paper_client

AT = datetime(2026, 9, 18, 10)


def position():
    return SimpleNamespace(code="600001", buy_price=10, current_price=10,
        buy_time=AT-timedelta(days=1), stop_loss_price=9, entry_sector_code=None,
        entry_sector_name=None)


def params():
    return dict(volume_negative_ratio=3, weak_exit_min_evidence=2,
        next_day_min_profit_pct=-99, max_hold_days=5)


@pytest.mark.parametrize("global_ratio", [0.5, 1.2, 5.0])
def test_frozen_volume_threshold_owns_weak_exit_evidence(monkeypatch, global_ratio):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_VOLUME_NEGATIVE_RATIO", global_ratio)
    ctx = dict(price=10, open=9.9, high=10.01, ma5=10.1, change_pct=-0.1,
               volume_ratio=2, min5_change=0, orderbook_imbalance=0)
    # 仅MA5一项；不能拿全局较低量比阈值凑够两项并触发全退。
    assert paper._short_sell_reason(position(), ctx, 0, 1, AT.date(), AT, params()) == ""
    ctx["volume_ratio"] = 3
    assert paper._short_sell_reason(position(), ctx, 0, 1, AT.date(), AT, params()).startswith("跌破5日线")


@pytest.mark.parametrize("global_ratio", [0.5, 5.0])
def test_frozen_volume_threshold_also_owns_open_stop_exception(monkeypatch, global_ratio):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_VOLUME_NEGATIVE_RATIO", global_ratio)
    pos = position()
    pos.stop_loss_price = 9.8
    ctx = dict(price=9.7, open=9.9, high=10, ma5=9.8, change_pct=-1,
               volume_ratio=2, min5_change=0, orderbook_imbalance=0)
    policy = params() | dict(open_noise_stop_min_evidence=2, open_severe_stop_loss_pct=6.5)
    early = AT.replace(hour=9, minute=35)
    assert paper._short_sell_reason(pos, ctx, -3, 1, AT.date(), early, policy) == ""
    ctx["volume_ratio"] = 3
    assert paper._short_sell_reason(pos, ctx, -3, 1, AT.date(), early, policy).startswith("触发持仓止损价")
    # 深度亏损与窗外硬止损均不依赖弱证据，不扩大止损豁免。
    ctx["volume_ratio"] = None
    assert paper._short_sell_reason(pos, ctx, -7, 1, AT.date(), early, policy).startswith("触发持仓止损价")
    assert paper._short_sell_reason(pos, ctx, -3, 1, AT.date(), AT, policy).startswith("触发持仓止损价")


def test_sector_evidence_and_detail_use_the_same_frozen_threshold(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_VOLUME_NEGATIVE_RATIO", 0.5)
    ctx = dict(price=10, open=9.9, high=10.01, ma5=9.8, avg_price=10.1,
        change_pct=-0.1, volume_ratio=2, sector_retreat_reason="板块退潮：测试")
    assert paper._short_sell_reason(position(), ctx, 0, 1, AT.date(), AT, params()) == ""
    ctx["volume_ratio"] = 3
    reason = paper._short_sell_reason(position(), ctx, 0, 1, AT.date(), AT, params())
    assert reason.startswith("板块退潮") and "放量下跌" in reason


async def seed(db, **spot_overrides):
    # 包含旧日放量阴线，以及决策当天已被日终覆盖和未来日的K线。
    for offset in range(-21, 2):
        db.add(StockKline(code="600001", trade_date=(AT+timedelta(days=offset)).date(),
            open=20 if offset >= 0 else 10.5,
            high=22 if offset >= 0 else 10.6, low=9,
            close=20 if offset >= 0 else 10, change_pct=-2,
            volume=10000000 if offset == -1 else 1000000))
    values = dict(code="600001", price=10, open=10.1, high=10.2, low=9.8,
        change_pct=0, volume_ratio=None, avg_price=10, min5_change=0,
        orderbook_imbalance=0, source_quote_at=AT-timedelta(seconds=2),
        received_at=AT-timedelta(seconds=1), updated_at=AT)
    values.update(spot_overrides)
    db.add(StockSpot(**values))
    await db.flush()


@pytest.mark.asyncio
async def test_zero_change_and_missing_live_volume_do_not_inherit_daily_bar(paper_client, monkeypatch):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    async with maker() as db:
        await seed(db)
        ctx = await paper._build_short_sell_context(db, position(), AT.date())
        assert ctx["change_pct"] == 0
        assert ctx["volume_ratio"] is None
        assert ctx["ma5"] == ctx["ma10"] == ctx["ma20"] == 10
        assert ctx["prev_close"] == 10
        assert paper._short_sell_reason(position(), ctx, 0, 1, AT.date(), AT, params()) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), -1, 0])
async def test_missing_or_invalid_intraday_ohlc_never_uses_daily_bar(paper_client, monkeypatch, missing):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    async with maker() as db:
        await seed(db, open=missing, high=missing, low=missing, change_pct=None)
        ctx = await paper._build_short_sell_context(db, position(), AT.date())
        assert ctx["open"] is ctx["high"] is ctx["low"] is None
        assert ctx["change_pct"] is ctx["close_position"] is None


@pytest.mark.asyncio
async def test_ma_uses_current_quote_once_without_same_day_or_future_close(paper_client, monkeypatch):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    async with maker() as db:
        await seed(db, price=10.2, high=10.3)
        ctx = await paper._build_short_sell_context(db, position(), AT.date())
        assert ctx["ma5"] == 10.04
        assert ctx["ma10"] == 10.02
        assert ctx["ma20"] == 10.01


@pytest.mark.asyncio
async def test_empty_quote_and_history_preserve_missing_evidence(paper_client, monkeypatch):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    async with maker() as db:
        ctx = await paper._build_short_sell_context(db, position(), AT.date())
        for key in ("open", "high", "low", "change_pct", "volume_ratio", "ma5", "ma10", "ma20"):
            assert ctx[key] is None
        assert not ctx["prev_was_limit_up"]
        assert paper._short_sell_reason(position(), ctx, 0, 1, AT.date(), AT, params()) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_close", [None, 0, -1, float("inf")])
async def test_missing_close_is_not_replaced_with_an_older_session(paper_client, monkeypatch, bad_close):
    from sqlalchemy import update
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    async with maker() as db:
        await seed(db)
        await db.execute(update(StockKline).where(
            StockKline.code == "600001",
            StockKline.trade_date == (AT-timedelta(days=2)).date(),
        ).values(close=bad_close))
        ctx = await paper._build_short_sell_context(db, position(), AT.date())
        assert ctx["ma5"] is ctx["ma10"] is ctx["ma20"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("lag", [-120, 30])
async def test_stale_or_future_quote_cannot_enter_ma(paper_client, monkeypatch, lag):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    async with maker() as db:
        await seed(db, price=10.2, source_quote_at=AT+timedelta(seconds=lag))
        ctx = await paper._build_short_sell_context(db, position(), AT.date())
        assert ctx["ma5"] == ctx["ma10"] == ctx["ma20"] == 10
        assert ctx["exit_input_basis"]["ma_basis"] == "prior_daily_closes_only"


@pytest.mark.asyncio
async def test_context_consumes_owned_round_not_later_spot(paper_client, monkeypatch):
    _, maker = paper_client
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    frozen = dict(code="600001", price=10.2, open=10.1, high=10.3, low=9.9,
        change_pct=0, volume_ratio=0, source_quote_at=AT-timedelta(seconds=2),
        received_at=AT-timedelta(seconds=1), updated_at=AT, quote_round_id="owned")
    async with maker() as db:
        await seed(db, price=12, high=13, change_pct=20, volume_ratio=5)
        token = paper._QUOTE_ROUND_CONTEXT.set(dict(round_id="owned", records=[frozen]))
        try:
            ctx = await paper._build_short_sell_context(db, position(), AT.date())
            assert ctx["price"] == 10.2 and ctx["ma5"] == 10.04
            assert ctx["high"] == 10.3
            assert ctx["change_pct"] == ctx["volume_ratio"] == 0
            assert ctx["exit_input_contract"] == account_policy.EXIT_INPUT_CONTRACT_VERSION
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)


def test_exit_input_correction_rotates_versions_without_changing_frozen_thresholds(monkeypatch):
    for version in (execution_version, standard_execution_version):
        before = {name: version("base", name) for name in account_policy.ACCOUNT_NAMES}
        policies = {name: account_policy.account_sell_params(name) for name in account_policy.ACCOUNT_NAMES}
        with monkeypatch.context() as patch:
            patch.setattr(account_policy, "EXIT_INPUT_CONTRACT_VERSION", "old-input-contract")
            for name in account_policy.ACCOUNT_NAMES:
                assert version("base", name) != before[name]
                assert account_policy.account_sell_params(name) == policies[name]
