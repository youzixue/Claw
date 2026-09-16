"""次账户跨轮执行的硬边界：预算预占、可恢复数据等待与真实信号失效。"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog, PaperNav
from app.models.stock import StockSpot
from app.models.trading import TradeOrder
from app.paper.strategy_iteration_challenger import (
    _live_route_confirmation_valid, _route_auto_order_enabled,
    run_strategy_iteration_challenger_accounts,
)
from test_strategy_iteration_challenger import challenger_env, _seed_confirmed, ROUTE_B
from challenger_execution_fixture import qualified_challenger_execution


@pytest.mark.asyncio
@pytest.mark.parametrize("daily_limit,position_limit,quantity", [(1, 10, 100), (10, 1, 100), (10, 10, 4900)])
async def test_pending_buy_reserves_daily_slot_position_and_cash(
        challenger_env, monkeypatch, daily_limit, position_limit, quantity):
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda _: (daily_limit, position_limit))
    monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={"final_level": "pass"}))
    at = datetime(2026, 9, 8, 10)
    async with challenger_env() as db:
        await _seed_confirmed(db, code="600221", route_id=ROUTE_B, now=at)
        account = await paper._get_or_create_account(db, "challenger_b")
        account.initial_capital = account.current_capital = account.total_assets = 50000
        db.add(TradeOrder(order_id="reserved", broker="paper", account_id="challenger_b",
            code="600222", side="buy", order_type="limit", price=10, quantity=quantity,
            filled_quantity=0, status="submitted", trade_date=at.date(), created_at=at))
        await db.commit()
        result = await run_strategy_iteration_challenger_accounts(db, now=at)
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.code == "600221"))).all())
    assert result["entries"] == 0
    assert len(logs) == 1 and logs[0].decision == "wait"
    assert ("预算" if quantity == 4900 else "最大持仓数") in logs[0].reason


@pytest.mark.asyncio
@pytest.mark.parametrize("temporarily_missing", [True, False])
async def test_missing_vwap_recovers_but_broken_confirmation_is_terminal(
        challenger_env, qualified_challenger_execution, monkeypatch, temporarily_missing):
    monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={"final_level": "pass"}))
    at = datetime(2026, 9, 8, 10)
    async with challenger_env() as db:
        await _seed_confirmed(db, code="600223", route_id=ROUTE_B, now=at)
        spot = await db.get(StockSpot, "600223")
        spot.avg_price = None if temporarily_missing else 10.10
        # ORM onupdate uses wall-clock; keep the explicit synthetic quote clock.
        from sqlalchemy.orm.attributes import flag_modified
        spot.updated_at = at
        flag_modified(spot, "updated_at")
        await db.commit()
        first = await qualified_challenger_execution(db, now=at)
        spot.avg_price = 10.02
        spot.updated_at = at + timedelta(seconds=1)
        await db.commit()
        second = await qualified_challenger_execution(db, now=at + timedelta(seconds=1))
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.code == "600223").order_by(PaperAutoTradeLog.id))).all())
    assert first["entries"] == 0
    assert second["entries"] == int(temporarily_missing), [(row.action, row.decision, row.reason) for row in logs]
    assert logs[0].decision == ("wait" if temporarily_missing else "skipped")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,restored", [
    ("amount", 50_000_000), ("withdrawal_ratio", .1),
    ("volume_ratio", 1.5), ("orderbook_imbalance", .2),
])
async def test_a2_missing_liquidity_waits_and_only_retries_a_new_quote(
    challenger_env, qualified_challenger_execution, monkeypatch, field, restored,
):
    import json
    from sqlalchemy.orm.attributes import flag_modified

    monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={"final_level": "pass"}))
    at = datetime(2026, 9, 8, 10)
    async with challenger_env() as db:
        await _seed_confirmed(db, code="600225", route_id="momentum_first_retest", now=at,
                              price=10.4, ask=10.41)
        spot = await db.get(StockSpot, "600225")
        setattr(spot, field, None)
        spot.updated_at = at
        flag_modified(spot, "updated_at")
        await db.commit()
        first = await qualified_challenger_execution(db, now=at)
        same_round = await qualified_challenger_execution(db, now=at)
        assert first["entries"] == 0 and same_round["entries"] == 0
        setattr(spot, field, restored)
        spot.updated_at = at + timedelta(seconds=30)
        await db.commit()
        second = await qualified_challenger_execution(db, now=at + timedelta(seconds=30))
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.code == "600225",
            PaperAutoTradeLog.action.notin_(("buy_signal", "signal_push")),
        ).order_by(PaperAutoTradeLog.id))).all())
    assert second["entries"] == 1
    assert logs[0].decision == "wait"
    evidence = json.loads(logs[0].candidate_json)
    assert evidence["execution_block_class"] == "recoverable_data_wait"
    assert evidence["execution_liquidity_issues"][0]["code"] == f"missing_{field}"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,bad_value", [
    ("volume_ratio", 5.16), ("volume_ratio", None), ("amount", None),
    ("orderbook_imbalance", None), ("withdrawal_ratio", None),
    ("withdrawal_ratio", .51),
])
async def test_a2_invalid_original_confirmation_cannot_borrow_later_liquidity(
    challenger_env, monkeypatch, field, bad_value,
):
    import json
    from app.models.paper import PaperShadowEvent

    risk_check = AsyncMock(return_value={"final_level": "pass"})
    monkeypatch.setattr(paper, "_risk_check_for_buy", risk_check)
    at = datetime(2026, 9, 8, 10)
    async with challenger_env() as db:
        event = await _seed_confirmed(db, code="600226", route_id="momentum_first_retest",
                                      now=at, price=10.4, ask=10.41)
        source = json.loads(event.snapshot_json)
        source["quote"][field] = bad_value
        original = event.snapshot_json = json.dumps(source, ensure_ascii=False)
        event_id = event.id
        await db.commit()
        # 当前行情完整合格，但不能修复已经冻结的原确认。
        first = await run_strategy_iteration_challenger_accounts(db, now=at)
        spot = await db.get(StockSpot, "600226")
        spot.updated_at = at + timedelta(seconds=30)
        await db.commit()
        second = await run_strategy_iteration_challenger_accounts(db, now=at + timedelta(seconds=30))
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.code == "600226",
        ))).all())
        await db.refresh(event)
        assert (await db.get(PaperShadowEvent, event_id)).snapshot_json == original
    assert first["entries"] == second["entries"] == 0
    assert len(logs) == 1 and logs[0].action == "skip_terminal"
    evidence = json.loads(logs[0].candidate_json)
    assert evidence["execution_block_class"] == "invalid_confirmation_evidence"
    assert evidence["confirmation_liquidity_issues"][0]["code"] == (
        f"missing_{field}" if bad_value is None else f"out_of_range_{field}"
    )
    risk_check.assert_not_awaited()


def test_a2_revalidation_does_not_inherit_iteration_or_whole_day_high_gates(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_MIN_VOLUME_RATIO", 9)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_MIN_ORDERBOOK_IMBALANCE", 9)
    event = SimpleNamespace(route_id="momentum_first_retest")
    snapshot = {"state": {"peak_price": 10.4}, "rule_snapshot": {
        "min_volume_ratio": .8, "max_volume_ratio": 5, "min_orderbook_imbalance": -.2,
        "candidate_min_change_pct": 3, "candidate_max_change_pct": 6, "max_peak_gap_pct": .8,
    }}
    spot = SimpleNamespace(price=10.4, prev_close=10, avg_price=10.3, high=11,
        ask1_price=10.41, ask1_volume=10, volume_ratio=1.5, orderbook_imbalance=0,
        withdrawal_ratio=.1, amount=50_000_000)
    assert _live_route_confirmation_valid(event, snapshot, spot) == (True, "")
    snapshot["state"]["peak_price"] = 10.6
    assert not _live_route_confirmation_valid(event, snapshot, spot)[0]


@pytest.mark.asyncio
async def test_comparison_preserves_drawdown_but_does_not_claim_experiment_is_blocked(challenger_env, monkeypatch):
    from app.paper.strategy_iteration_challenger import build_strategy_iteration_challenger_comparison
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-01-01")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-01-01T00:00:00")
    async with challenger_env() as db:
        account = await paper._get_or_create_account(db, "challenger_b")
        db.add_all([
            PaperNav(account_id=account.id, trade_date=date(2026, 9, 3), nav=1, daily_return=0),
            PaperNav(account_id=account.id, trade_date=date(2026, 9, 4), nav=.7, daily_return=-30),
        ])
        await db.commit()
        report = await build_strategy_iteration_challenger_comparison(db, account_name="promotion")
    evidence = report["pairs"][0]["evidence"]
    guard = evidence["execution_guardrails"]
    assert guard["tag_only"] is True
    assert guard["account_max_drawdown_pct"] == 30
    assert guard["account_drawdown_acceptable"] is False
    assert evidence["execution_guardrails_passed"] is True


@pytest.mark.asyncio
async def test_confirmed_event_created_after_round_waits_without_consuming_identity(challenger_env, qualified_challenger_execution, monkeypatch):
    monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={"final_level":"pass"}))
    at = datetime(2026, 9, 8, 10)
    async with challenger_env() as db:
        event = await _seed_confirmed(db, code="600224", route_id=ROUTE_B, now=at)
        event.created_at = at+timedelta(seconds=30)
        await db.commit()
        first = await qualified_challenger_execution(db, now=at)
        assert first["entries"] == 0
        assert list((await db.scalars(select(TradeOrder).where(TradeOrder.code=="600224"))).all()) == []
        spot = await db.get(StockSpot, "600224")
        spot.updated_at = at+timedelta(seconds=30)
        await db.commit()
        second = await qualified_challenger_execution(db, now=at+timedelta(seconds=30))
        assert second["entries"] == 1


def test_global_manual_maintenance_switch_still_prevents_challenger_buys(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_AUTO_TRADE_ENABLED", False)
    assert not _route_auto_order_enabled(ROUTE_B, now=datetime(2026, 9, 8, 10))
