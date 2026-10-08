"""Real service/API on isolated DB; aggregate feeds must never book fixed-price fills."""
import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1 import paper
from app.data.after_hours import observation, append_observations
from app.models.governance import TradeCalendarModel
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.stock import MarketSentiment, StockSpot, StockTag
from app.models.trading import TradeOrder, TradeFill
from app.trading import service
from app.trading.paper_after_hours_execution import MODE, CONTRACT, clock_valid, limit_compatible, waiting_plan
from app.trading.paper_authorization import (
    _paper_execution_scope, authorize_broker_request, authorize_ledger_request, validate_ledger_clock,
)
from app.trading.broker import BrokerOrderRequest
from test_trading_api import trading_client

AT = datetime(2026, 9, 30, 15, 10)

@pytest_asyncio.fixture
async def environment(trading_client, monkeypatch):
    client, maker = trading_client
    clock = [AT]
    # Each pytest function owns a fresh loop and lock; do not reuse a lock
    # whose first contention bound it to an already-closed test loop.
    monkeypatch.setattr(paper, "_TRADE_LOCK", asyncio.Lock())
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    monkeypatch.setattr(paper, "_paper_now", lambda: clock[0])
    # Only fixture calendar data; no Akshare calendar loading/fallback network.
    monkeypatch.setattr(type(paper.trade_calendar), "_ensure_loaded", AsyncMock())
    monkeypatch.setattr(type(paper.trade_calendar), "_sync_from_source",
                        AsyncMock(side_effect=AssertionError("no network calendar")))
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        AT.date()-timedelta(days=i): (AT.date()-timedelta(days=i)).weekday() < 5
        for i in range(35)})
    async with maker() as db:
        sentiment = await db.scalar(select(MarketSentiment))
        sentiment.trade_date = AT.date()
        sentiment.observed_at = AT-timedelta(seconds=30)
        db.add(TradeCalendarModel(trade_date=AT.date(), is_trade_day=True, session_type="full"))
        db.add(PaperAccount(account_name="default", initial_capital=50000, current_capital=50000,
                            total_assets=50000, total_return=0, max_drawdown=0, status="active"))
        db.add(StockSpot(code="000001", name="平安银行", price=11.57,
            volume=100000000, amount=1000000000, ask1_price=11.57, ask1_volume=999999,
            bid1_price=11.57, bid1_volume=999999))
        baseline_at = AT.replace(minute=1)
        row = observation(code="000001", day=AT.date(), stage="regular_close", source="tencent_close",
            source_version="tencent_pre_fixed_price_baseline_v1", received_at=baseline_at,
            accepted_at=baseline_at, source_quote_at=AT.replace(minute=0),
            values={"close_price_1500": 11.57, "regular_volume_shares": 100000000,
                    "price_basis": "tencent_unadjusted_quote"})
        await append_observations(db, [row])
        await db.commit()
    # If either ordinary matching path is touched the test fails; no broker dispatch allowed.
    forbidden = AsyncMock(side_effect=AssertionError("no ordinary matcher/broker for fixed-price intent"))
    monkeypatch.setattr(service, "_dispatch_for_service", forbidden)
    monkeypatch.setattr(service, "account_round_depth_evidence", forbidden)
    return SimpleNamespace(client=client, maker=maker, clock=clock, forbidden=forbidden)

def request(**changes):
    value = {"code": "000001", "side": "buy", "price": 11.6, "quantity": 100,
             "order_type": MODE, "source": "manual", "idempotency_key": ""}
    value.update(changes)
    return value

async def submit(env, **changes):
    response = await env.client.post("/trading/orders", json=request(**changes))
    assert response.status_code == 200, response.text
    return response.json()

async def economics(env):
    async with env.maker() as db:
        account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == "default"))
        positions = list((await db.scalars(select(PaperPosition))).all())
        return (account.current_capital, [(p.id, p.buy_amount, p.buy_time, p.is_closed) for p in positions],
                await db.scalar(select(func.count(PaperTradeLog.id))),
                await db.scalar(select(func.count(TradeFill.id))))

@pytest.mark.asyncio
async def test_explicit_mode_waits_with_fixed_close_reference_and_no_economics(environment):
    before = await economics(environment)
    result = await submit(environment)
    assert result["order"]["status"] == "submitted"
    proof = result["risk"]["paper_after_hours_intent"]
    assert proof["status"] == "waiting" and proof["fillable"] is False
    assert proof["external_queue_ahead_shares"] is None
    assert proof["closing_reference"]["price"] == 11.57
    assert proof["closing_reference"]["execution_authority"] is False
    assert not proof["cash_reserved"]
    assert result["fills"] == [] and await economics(environment) == before
    environment.forbidden.assert_not_awaited()

@pytest.mark.asyncio
async def test_manual_provenance_is_frozen_and_never_inherits_old_tencent_round(environment, monkeypatch):
    version = ["fixed-original.v1"]
    monkeypatch.setattr(paper, "_strategy_version", lambda account: version[0])
    # This ordinary round is deliberately prior-day and must not be inherited.
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id": "ordinary.stale",
        "committed_at": AT - timedelta(days=1), "as_of_at": AT - timedelta(days=1)})
    try:
        first = await submit(environment, idempotency_key="original-provenance")
        version[0] = "fixed-next.v2"
        environment.clock[0] += timedelta(seconds=1)
        replay = await submit(environment, idempotency_key="original-provenance")
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert first["order"]["strategy_version"] == replay["order"]["strategy_version"] == "fixed-original.v1"
    assert first["order"]["decision_round_id"] == "after-hours-intent-2026-09-30"
    assert replay["order"]["decision_round_id"] == first["order"]["decision_round_id"]
    async with environment.maker() as db:
        order = await db.scalar(select(TradeOrder))
        assert order.decision_at == AT and order.as_of_at == AT
        proof = json.loads(order.risk_json)["paper_after_hours_intent"]
        assert proof["requested_at"] == AT.isoformat()
        assert proof["numeric_account_id"] == await db.scalar(select(PaperAccount.id))
    assert first["fills"] == replay["fills"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("at", [AT.replace(minute=4, second=59), AT.replace(minute=30),
                                AT.replace(hour=16), datetime(2026, 10, 1, 15, 10),
                                datetime(2026, 10, 10, 15, 10)])
async def test_clock_and_holidays_never_create_fill(environment, at):
    environment.clock[0] = at
    before = await economics(environment)
    result = await submit(environment)
    assert result["order"]["status"] in {"rejected", "risk_blocked"}
    assert result["fills"] == [] and await economics(environment) == before

@pytest.mark.parametrize("at, valid", [
    (AT.replace(minute=5), True), (AT.replace(minute=29, second=59, microsecond=999999), True),
    (AT.replace(minute=30), False), (datetime(2026, 7, 3, 15, 10), False),
    (datetime(2026, 7, 6, 15, 5), True),
])
def test_exact_rule_and_session_subset_boundaries(at, valid):
    assert clock_valid(at) is valid

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "closed", "half_day"])
async def test_stored_calendar_fail_closed(environment, kind):
    async with environment.maker() as db:
        row = await db.get(TradeCalendarModel, AT.date())
        if kind == "missing":
            await db.delete(row)
        elif kind == "closed":
            row.is_trade_day = False
        else:
            row.session_type = "half_day"
        await db.commit()
    result = await submit(environment)
    assert result["order"]["status"] == "rejected" and result["fills"] == []

@pytest.mark.asyncio
async def test_price_condition_never_uses_user_limit_as_fill_price(environment):
    result = await submit(environment, price=11.56)
    assert result["order"]["status"] == "rejected"
    assert result["risk"]["paper_after_hours_intent"]["reason"] == "limit_incompatible_with_observed_close_reference"
    assert result["fills"] == []

@pytest.mark.parametrize("side, limit, expected", [("buy", 11.57, True), ("buy", 11.58, True),
    ("buy", 11.56, False), ("sell", 11.57, True), ("sell", 11.56, True), ("sell", 11.58, False)])
def test_fixed_price_limit_protection(side, limit, expected):
    assert limit_compatible(side, limit, 11.57) is expected

@pytest.mark.asyncio
async def test_retries_keep_fifo_and_mode_conflicts_fail(environment):
    first = await submit(environment, idempotency_key="first")
    environment.clock[0] += timedelta(seconds=1)
    second = await submit(environment, idempotency_key="second")
    replay = await submit(environment, idempotency_key="first")
    assert replay["idempotent_replay"] and replay["order"]["id"] == first["order"]["id"]
    assert replay["risk"]["paper_after_hours_intent"]["accepted_at"] == first["risk"]["paper_after_hours_intent"]["accepted_at"]
    async with environment.maker() as db:
        orders = list((await db.scalars(select(TradeOrder))).all())
        diagnostic = waiting_plan(list(reversed(orders)))
    assert diagnostic[1]["local_fifo_ahead"] == [first["order"]["order_id"]]
    assert diagnostic[1]["external_fifo_ahead"] is None
    for changes in ({"price": 11.7}, {"quantity": 200}, {"order_type": "limit"}):
        response = await environment.client.post("/trading/orders", json=request(idempotency_key="first", **changes))
        assert response.status_code == 409
    assert second["fills"] == []

@pytest.mark.asyncio
async def test_later_intent_cannot_jump_fifo_after_between_request_clock_rollback(environment):
    environment.clock[0] = AT.replace(minute=20)
    first = await submit(environment, idempotency_key="earlier-acceptance")
    environment.clock[0] = AT
    second = await submit(environment, idempotency_key="later-acceptance")
    async with environment.maker() as db:
        orders = list((await db.scalars(select(TradeOrder))).all())
        plan = waiting_plan(orders)
    assert [item["order_id"] for item in plan] == [first["order"]["order_id"], second["order"]["order_id"]]
    assert plan[1]["local_fifo_ahead"] == [first["order"]["order_id"]]
    assert plan[1]["external_fifo_ahead"] is None and plan[1]["fills"] == []


@pytest.mark.asyncio
async def test_aggregate_increase_and_depth_never_convert_waiting_to_filled(environment):
    result = await submit(environment)
    before = await economics(environment)
    async with environment.maker() as db:
        spot = await db.get(StockSpot, "000001")
        spot.volume *= 10
        await db.commit()
        state = await service.reconcile_paper_after_hours_intents(db)
    assert state["fills"] == [] and state["waiting"][0]["status"] == "waiting"
    assert state["source_capabilities"]["order_level_provider_count"] == 0
    assert state["source_capabilities"]["ledger_fill_contract_enabled"] is False
    assert state["source_capabilities"]["execution_authorized"] is False
    assert await economics(environment) == before
    environment.forbidden.assert_not_awaited()

@pytest.mark.asyncio
async def test_user_cancel_and_session_end_preserve_no_fill_state(environment):
    first = await submit(environment)
    before = await economics(environment)
    response = await environment.client.post("/trading/orders/" + first["order"]["order_id"] + "/cancel")
    assert response.json()["status"] == "canceled"
    assert await economics(environment) == before
    second = await submit(environment)
    environment.clock[0] = AT.replace(minute=30)
    async with environment.maker() as db:
        state = await service.reconcile_paper_after_hours_intents(db)
        row = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == second["order"]["order_id"]))
        proof = json.loads(row.risk_json)["paper_after_hours_cancel"]
    assert state["canceled"] == [second["order"]["order_id"]]
    assert proof["canceled_at"] == environment.clock[0].isoformat()
    assert proof["exchange_cancel_receipt"] is None
    assert await economics(environment) == before

@pytest.mark.asyncio
async def test_concurrent_cancels_are_idempotent_not_economic(environment):
    result = await submit(environment)
    before = await economics(environment)
    url = "/trading/orders/" + result["order"]["order_id"] + "/cancel"
    responses = await asyncio.gather(environment.client.post(url), environment.client.post(url))
    assert [response.status_code for response in responses] == [200, 200]
    assert sorted(response.json()["status"] for response in responses) == ["canceled", "unchanged"]
    assert await economics(environment) == before

@pytest.mark.asyncio
async def test_t_plus_1_current_day_buy_cannot_be_after_hours_sold(environment):
    async with environment.maker() as db:
        account = await db.scalar(select(PaperAccount))
        # Seed a ledger-consistent pre-existing purchase; risk refresh must not repair the fixture.
        account.current_capital = 50000 - 11.57 * 100 - 5
        db.add(PaperPosition(account_id=account.id, code="000001", name="平安银行",
            buy_price=11.57, buy_amount=100, buy_time=AT.replace(hour=10), is_closed=False,
            current_price=11.57, strategy_version=paper._strategy_version("default")))
        db.add(PaperTradeLog(account_id=account.id, code="000001", trade_type="buy", price=11.57,
            amount=100, trade_time=AT.replace(hour=10), commission=5))
        await db.commit()
    before = await economics(environment)
    result = await submit(environment, side="sell", price=11.57)
    assert result["order"]["status"] == "rejected"
    assert "T_plus_1" in result["risk"]["paper_after_hours_intent"]["reason"]
    assert await economics(environment) == before

@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["st", "suspended", "observe_only", "missing_identity"])
async def test_original_stock_risk_boundary_not_bypassed(environment, fault):
    async with environment.maker() as db:
        tag = await db.get(StockTag, "000001")
        if fault == "missing_identity":
            await db.delete(tag)
        elif fault == "observe_only":
            tag.board_tag = "observe_only"
        elif fault == "st":
            tag.is_st = True
        else:
            tag.is_suspended = True
        await db.commit()
    result = await submit(environment)
    assert result["order"]["status"] == "risk_blocked"
    assert result["fills"] == []

@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"after_hours_manual_intent": False}, {"broker": "real"},
    {"queue_if_limit_up": True}, {"defer_until_next_round": True}, {"order_type": "unknown"},
    {"account_id": "challenger_a"}, {"account_id": "shared_50k"}, {"account_id": "not_an_account"}])
async def test_explicit_authority_broker_and_incompatible_modes(environment, changes):
    cmd = service.SubmitOrderCommand(code="000001", side="buy", price=11.6, quantity=100,
        order_type=MODE, after_hours_manual_intent=True)
    cmd = replace(cmd, **changes)
    async with environment.maker() as db:
        with pytest.raises(HTTPException):
            await service.submit_order(db, cmd)
    environment.forbidden.assert_not_awaited()

@pytest.mark.asyncio
async def test_lock_wait_crossing_1530_cannot_register_backdated_order(environment, monkeypatch):
    from test_paper_ledger_clock_20260914 import ObservedLock
    lock = ObservedLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    await lock.lock.acquire()
    task = asyncio.create_task(submit(environment))
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        environment.clock[0] = AT.replace(minute=30)
        lock.lock.release()
        result = await asyncio.wait_for(task, 5)
        assert result["order"]["status"] == "rejected" and result["fills"] == []
    finally:
        if not task.done():
            task.cancel()
        if lock.lock.locked():
            lock.lock.release()
        await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_new_mode_has_no_book_authority_even_with_old_contract(monkeypatch):
    at = AT.replace(hour=10)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    db = object()
    req = BrokerOrderRequest(order_id="fixed", code="000001", side="buy", price=11.57,
                             quantity=100, order_type=MODE, filled_at=at)
    with _paper_execution_scope(db, req, immediate_evidence_json=json.dumps({
            "contract_version": "immediate_paper_fill_v2_20260914"})):
        authorize_broker_request(db, req)
        book = paper.SimBuyRequest(code=req.code, price=req.price, amount=req.quantity, signal_id=req.order_id)
        authorize_ledger_request(db, book, side="buy", account_name=req.account_name)
        with pytest.raises(HTTPException, match="ordinary_fill_contract_requires_limit_order"):
            validate_ledger_clock(db, phase="lock_acquired")


@pytest.mark.asyncio
async def test_existing_closed_or_missing_account_never_reopens(environment):
    async with environment.maker() as db:
        account = await db.scalar(select(PaperAccount))
        account.status = "closed"
        await db.commit()
    response = await environment.client.post("/trading/orders", json=request())
    assert response.status_code == 403
    async with environment.maker() as db:
        assert await db.scalar(select(func.count(PaperAccount.id))) == 1
        assert await db.scalar(select(func.count(TradeOrder.id))) == 0
    response = await environment.client.post("/trading/orders", json=request(account_id="promotion"))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_waiting_for_lock_cancellation_leaves_no_bare_pending(environment, monkeypatch):
    from test_paper_ledger_clock_20260914 import ObservedLock
    lock = ObservedLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    await lock.lock.acquire()
    task = asyncio.create_task(environment.client.post("/trading/orders", json=request(idempotency_key="cancel-wait")))
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        lock.lock.release()
    async with environment.maker() as db:
        assert await db.scalar(select(func.count(TradeOrder.id))) == 0
    result = await submit(environment, idempotency_key="cancel-wait")
    assert result["order"]["status"] == "submitted"


@pytest.mark.asyncio
async def test_concurrent_same_key_registers_one_stable_intent(environment):
    responses = await asyncio.gather(
        environment.client.post("/trading/orders", json=request(idempotency_key="concurrent")),
        environment.client.post("/trading/orders", json=request(idempotency_key="concurrent")))
    assert [item.status_code for item in responses] == [200, 200]
    assert len({item.json()["order"]["order_id"] for item in responses}) == 1
    async with environment.maker() as db:
        assert await db.scalar(select(func.count(TradeOrder.id))) == 1
        assert await db.scalar(select(func.count(TradeFill.id))) == 0


@pytest.mark.asyncio
async def test_bad_first_expiry_does_not_abort_next_good_or_ordinary_finalize_override(environment):
    bad = await submit(environment, idempotency_key="bad")
    good = await submit(environment, idempotency_key="good")
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == bad["order"]["order_id"]))
        row.filled_quantity = 1  # Deliberate inconsistent fixture: preserve for investigation.
        await db.commit()
    environment.clock[0] = AT.replace(minute=30)
    async with environment.maker() as db:
        result = await service.reconcile_paper_after_hours_intents(db)
    assert result["errors"] == [{"order_id": bad["order"]["order_id"], "http_status": 409}]
    assert result["canceled"] == [good["order"]["order_id"]]
    # Real existing finalizer may write control/outcome records in this fixture DB,
    # but must not apply its ordinary-depth cancellation to the exceptional intent.
    async with environment.maker() as db:
        await paper.finalize_paper_daily_outcomes(db, trade_date=AT.date(),
                                                 observed_at=AT.replace(minute=45))
        row = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == bad["order"]["order_id"]))
        assert row.status == "submitted" and row.filled_quantity == 1
        assert "close_finalization" not in json.loads(row.risk_json)


@pytest.mark.asyncio
async def test_clock_rollbacks_request_and_terminal_fail_closed(environment, monkeypatch):
    from test_paper_ledger_clock_20260914 import ObservedLock
    lock = ObservedLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    environment.clock[0] = AT.replace(minute=20)
    await lock.lock.acquire()
    task = asyncio.create_task(submit(environment))
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        environment.clock[0] = AT
        lock.lock.release()
        result = await asyncio.wait_for(task, 5)
        assert result["order"]["status"] == "rejected"
    finally:
        if not task.done():
            task.cancel()
        if lock.lock.locked():
            lock.lock.release()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_terminal_clock_cannot_roll_back_after_validation(environment, monkeypatch):
    import app.trading.paper_after_hours_execution as execution
    real_evidence = execution.intent_evidence
    async def advancing_validation(db, cmd, *, accepted_at, validated_at):
        result = await real_evidence(db, cmd, accepted_at=accepted_at, validated_at=AT.replace(minute=20))
        environment.clock[0] = AT.replace(minute=15)
        return result
    monkeypatch.setattr(execution, "intent_evidence", advancing_validation)
    result = await submit(environment)
    assert result["order"]["status"] == "rejected" and result["fills"] == []
    assert result["risk"]["paper_after_hours_intent"]["reason"] == "registration_crossed_session_end_or_clock_rollback"
    async with environment.maker() as db:
        assert await db.scalar(select(func.count(TradeFill.id))) == 0


@pytest.mark.asyncio
async def test_cancel_clock_cannot_precede_registration_terminal(environment):
    result = await submit(environment)
    # Accepted at 15:10, final registration completed at 15:20. A later local
    # clock of 15:15 is not early relative to acceptance, but IS a rollback.
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        risk = json.loads(row.risk_json)
        risk["paper_after_hours_intent"]["terminal_validated_at"] = AT.replace(minute=20).isoformat()
        row.risk_json = json.dumps(risk)
        await db.commit()
    environment.clock[0] = AT.replace(minute=15)
    response = await environment.client.post("/trading/orders/" + result["order"]["order_id"] + "/cancel")
    assert response.status_code == 409
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        assert row.status == "submitted"


@pytest.mark.asyncio
async def test_public_normal_key_conflict_is_not_replayed_across_identity(environment):
    first = await submit(environment, order_type="limit", idempotency_key="normal-key")
    assert first["order"]["status"] == "rejected"  # Still outside ordinary matching hours.
    for changes in ({"code": "000003"}, {"price": 11.7}, {"quantity": 200}, {"account_id": "promotion"}):
        response = await environment.client.post("/trading/orders",
                                                json=request(order_type="limit", idempotency_key="normal-key", **changes))
        assert response.status_code == 409
