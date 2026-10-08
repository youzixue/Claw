"""Same route decision for entry and pending orders; temp DB, never live orders."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog, PaperShadowEvent
from app.models.trading import TradeFill, TradeOrder
from app.paper import strategy_iteration_challenger as challenger
from app.trading import service
from test_pending_buy_validity import AT, quote
from paper_pending_fixture import accepted_frame
from test_paper_deferred_exit_provenance import memory_session
from test_strategy_iteration_challenger import challenger_env, _seed_confirmed, ROUTE_B
from challenger_execution_fixture import qualified_challenger_execution

MOMENTUM = "momentum_first_retest"


def momentum_snapshot():
    return {"rule_snapshot": {
        "candidate_min_change_pct": 3., "candidate_max_change_pct": 6.,
        "min_volume_ratio": 1., "max_volume_ratio": 5., "min_amount": 30_000_000.,
        "min_orderbook_imbalance": -.2, "max_withdrawal_ratio": .5,
        "max_peak_gap_pct": 1.2,
    }, "state": {"peak_price": 31.3}}


def momentum_quote(at, **fields):
    return quote(at, **{**dict(price=31.2, high=31.3, avg_price=31.0,
        prev_close=30., ask1_price=31.2, volume_ratio=2.,
        amount=50_000_000., withdrawal_ratio=0., orderbook_imbalance=0.), **fields})


@pytest.mark.parametrize("fields,recoverable", [
    ({}, None), ({"volume_ratio": None}, True), ({"volume_ratio": float("nan")}, True),
    ({"orderbook_imbalance": -.9}, False),
    ({"volume_ratio": None, "orderbook_imbalance": -.9}, False),
    ({"avg_price": None}, True), ({"price": 30.9}, False),
    ({"ask1_volume": 0}, True), ({"withdrawal_ratio": 1.}, False),
])
def test_live_gate_has_structured_recoverability(fields, recoverable):
    audit = {}
    valid, reason = challenger._live_route_confirmation_valid(
        SimpleNamespace(route_id=MOMENTUM), momentum_snapshot(), momentum_quote(AT, **fields),
        audit=audit)
    assert valid is (recoverable is None), reason
    assert audit["execution_confirmation_status"] == (
        "valid" if valid else "waiting" if recoverable else "invalidated")
    assert audit["execution_confirmation_recoverable"] is recoverable
    if not valid:
        assert audit["execution_confirmation_issue"]


async def momentum_order(db, monkeypatch, *, quantity=100):
    monkeypatch.setattr(service, "experiment_active", lambda *_a, **_kw: False)
    risk = AsyncMock(return_value={"final_level": "pass", "block_reasons": [], "warnings": []})
    broker = AsyncMock()
    async def fill(_db, request):
        return SimpleNamespace(accepted=True, external_order_id="fixture", fills=[
            SimpleNamespace(fill_id=f"fill-{request.order_id}", price=request.price,
                quantity=request.quantity, commission=5., tax=0., realized_pnl=0.,
                broker_trade_id="1", filled_at=request.filled_at, raw={})])
    broker.place_order.side_effect = fill
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda _: broker)
    from app.config.settings import settings
    monkeypatch.setattr(settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", 1.)
    event = PaperShadowEvent(event_key="mixed-liquidity-confirmed", route_id=MOMENTUM,
        route_version=challenger._route_shadow_version(MOMENTUM), trade_date=AT.date(),
        observed_at=AT, created_at=AT, code="002988", name="fixture",
        event_type="confirmed", status="confirmed", price=31.2,
        snapshot_json=json.dumps(momentum_snapshot()))
    db.add(event)
    await db.commit()
    frozen = challenger._event_candidate(event, momentum_snapshot())
    cmd = service.SubmitOrderCommand(code=event.code, side="buy", price=31.4,
        quantity=quantity, account_id="challenger_a", strategy_id="paper-challenger-forward",
        strategy_version=paper._strategy_version("challenger_a"), source=MOMENTUM,
        signal_id=challenger._signal_token(event.event_key, MOMENTUM),
        decision_at=AT+timedelta(seconds=28), as_of_at=AT, decision_round_id="decision",
        defer_until_next_round=True, deferred_metadata={"candidate": frozen, "block_warn": True})
    result = await service.submit_order(db, cmd)
    assert result["order"]["status"] == "submitted"
    order = await db.scalar(select(TradeOrder))
    assert json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]["status"] == "valid"
    return order, event, risk, broker


async def reconcile(db, monkeypatch, *, at, spot, round_id="fill", qualified=False):
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=spot))
    payload = {"round_id": round_id, "quality_status": "ok", "committed_at": at, "as_of_at": at}
    if qualified:
        payload.update(config_version="validity-fixture", code_version="validity-fixture",
            records=[vars(spot)])
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        return await service.reconcile_paper_deferred_orders(
            db, account_id="challenger_a", now=at, round_id=round_id)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,expected", [
    ({"volume_ratio": None}, "waiting"),
    ({"orderbook_imbalance": -.9}, "canceled"),
    ({"volume_ratio": None, "orderbook_imbalance": -.9}, "canceled"),
])
async def test_pending_uses_all_liquidity_issues(memory_session, monkeypatch, fields, expected):
    db = memory_session
    order, event, risk, broker = await momentum_order(db, monkeypatch)
    at = AT + timedelta(seconds=60)
    result = await reconcile(db, monkeypatch, at=at, spot=momentum_quote(at, **fields))
    assert result[0]["event"] == expected
    assert broker.place_order.await_count == 0
    audit = json.loads(order.risk_json)["paper_deferred_order"]["execution_confirmation_audit"]
    assert audit["execution_confirmation_recoverable"] is (expected == "waiting")
    assert audit["execution_liquidity_issues"]


@pytest.mark.parametrize("route", [MOMENTUM, "b_weak_open_second_board", "c_recent_limit_relaunch",
                                   "d_auction_recovery", "f2_highboard_break_reclaim"])
@pytest.mark.parametrize("missing", ["volume_ratio", "ask1_volume", "avg_price", "prev_close"])
def test_independent_known_failure_dominates_missing_field_on_every_shared_route(route, missing):
    snapshot = momentum_snapshot()
    snapshot["prior_structure"] = {"auction_volume_path_verified": True, "cancel_phase_verified": True}
    audit = {}
    ok, _ = challenger._live_route_confirmation_valid(
        SimpleNamespace(route_id=route), snapshot,
        momentum_quote(AT, **{missing: None, "orderbook_imbalance": -.9}), audit=audit)
    assert not ok
    assert audit["execution_confirmation_recoverable"] is False
    assert any("orderbook" in issue["code"] and not issue["recoverable"]
               for issue in audit["execution_confirmation_issues"])


@pytest.mark.parametrize("reverse", [False, True])
def test_mixed_issues_are_order_independent(monkeypatch, reverse):
    issues = [
        {"code": "missing_volume_ratio", "reason": "data unavailable", "recoverable": True},
        {"code": "out_of_range_orderbook_imbalance", "reason": "invalid orderbook", "recoverable": False},
    ]
    monkeypatch.setattr(challenger, "momentum_liquidity_gate_issues",
                        lambda *_: list(reversed(issues)) if reverse else issues)
    audit = {}
    ok, reason = challenger._live_route_confirmation_valid(
        SimpleNamespace(route_id=MOMENTUM), momentum_snapshot(), momentum_quote(AT), audit=audit)
    assert not ok and reason == "invalid orderbook"
    assert audit["execution_confirmation_recoverable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("recoverable,wording,expected", [
    (True, "英文缺字段说明", "waiting"),
    (False, "暂缺字样不再决定状态", "canceled"),
])
async def test_pending_status_never_depends_on_human_wording(
        memory_session, monkeypatch, recoverable, wording, expected):
    db = memory_session
    order, _, _, broker = await momentum_order(db, monkeypatch)
    def gate(_event, _snapshot, _spot, *, audit):
        audit["execution_confirmation_recoverable"] = recoverable
        return False, wording
    monkeypatch.setattr(challenger, "_live_route_confirmation_valid", gate)
    at = AT + timedelta(seconds=60)
    result = await reconcile(db, monkeypatch, at=at, spot=momentum_quote(at))
    assert result[0]["event"] == expected
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
async def test_partial_fill_is_retained_when_remainder_has_mixed_failure(memory_session, monkeypatch):
    db = memory_session
    order, _, _, broker = await momentum_order(db, monkeypatch, quantity=300)
    at = AT + timedelta(seconds=60)
    result = await reconcile(db, monkeypatch, at=at,
        spot=momentum_quote(at, ask1_volume=1), qualified=True)
    assert result[0]["event"] == "partial" and order.filled_quantity == 100
    fill = await db.scalar(select(TradeFill))
    before = (fill.price, fill.quantity, fill.filled_at, fill.commission)
    at += timedelta(seconds=30)
    result = await reconcile(db, monkeypatch, at=at, round_id="next",
        spot=momentum_quote(at, volume_ratio=None, orderbook_imbalance=-.9, quote_round_id="next"))
    assert result[0]["event"] == "canceled" and order.filled_quantity == 100
    assert (fill.price, fill.quantity, fill.filled_at, fill.commission) == before
    assert len(list((await db.scalars(select(TradeFill))).all())) == 1
    assert broker.place_order.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [MOMENTUM, "b_weak_open_second_board", "c_recent_limit_relaunch",
                                   "d_auction_recovery", "f2_highboard_break_reclaim"])
async def test_entry_no_offer_cannot_hide_known_invalid_orderbook(
        challenger_env, qualified_challenger_execution, route):
    from datetime import datetime
    from app.models.stock import StockSpot
    at = datetime(2026, 9, 2, 10, 20)
    async with challenger_env() as db:
        price = 10.4 if route == MOMENTUM else 10.05
        event = await _seed_confirmed(db, code="600100", route_id=route, now=at,
                                     price=price, ask=price+.01)
        spot = await db.scalar(select(StockSpot).where(StockSpot.code == event.code))
        spot.ask1_volume = 0
        spot.orderbook_imbalance = -.9
        spot.updated_at = at + timedelta(seconds=1)  # A distinct value forces SQL UPDATE instead of wall-clock onupdate.
        await db.commit()
        result = await qualified_challenger_execution(db, now=at + timedelta(seconds=1))
        assert result["entries"] == result["deferred"] == 0
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.code == event.code,
            PaperAutoTradeLog.run_id == challenger._run_id(event.event_key)))).all())
        assert len(logs) == 1
        assert logs[0].action == "skip_terminal" and logs[0].decision == "skipped", [(row.action, row.reason) for row in logs]
        assert json.loads(logs[0].candidate_json)["execution_confirmation_recoverable"] is False
        assert not list((await db.scalars(select(TradeOrder))).all())


@pytest.mark.asyncio
async def test_submit_risk_block_is_terminal_in_both_log_and_next_round(
        challenger_env, qualified_challenger_execution, monkeypatch):
    from datetime import datetime
    at = datetime(2026, 9, 2, 10, 20)
    monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={
        "final_level": "pass", "block_reasons": [], "warnings": []}))
    monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value={
        "final_level": "block", "block_reasons": [{"message": "isolated submit-time block"}], "warnings": []}))
    submit = AsyncMock(wraps=service.submit_order)
    monkeypatch.setattr(service, "submit_order", submit)
    async with challenger_env() as db:
        event = await _seed_confirmed(db, code="600100", route_id=ROUTE_B, now=at)
        await qualified_challenger_execution(db, now=at)
        order = await db.scalar(select(TradeOrder).where(TradeOrder.code == event.code))
        assert order.status == "risk_blocked"
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.code == event.code,
            PaperAutoTradeLog.run_id == challenger._run_id(event.event_key),
        ))).all())
        assert any(row.action == "skip_terminal" and row.decision == "skipped" for row in logs)
        assert not any(row.action == "wait_buy" for row in logs)
        await qualified_challenger_execution(db, now=at + timedelta(seconds=30))
        assert submit.await_count == 1
        assert len(list((await db.scalars(select(TradeOrder))).all())) == 1
