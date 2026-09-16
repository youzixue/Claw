"""Isolated actual ledger clock tests; entry risk mocks isolate timing, not alpha."""
import asyncio
import hashlib
import json
from datetime import timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1 import paper
from app.models.governance import TradeCalendarModel
from app.models.stock import QuoteRound
from app.models.paper import PaperTradeLog, PaperPosition
from app.models.trading import TradeFill, TradeOrder
from app.paper import entry_fee_allocation
from app.trading import service
from app.trading import paper_authorization as authorization
from test_quote_round_execution import quote_execution_env
from test_paper_atomic_execution_20260914 import (
    isolated_transaction_policy_and_calendar, snapshot,
)
from test_paper_orphan_fill_guard import AT, place_deferred, quote
from test_paper_ledger_clock_20260914 import ObservedLock

PATHS = ["deferred-buy", "deferred-sell", "queue-open", "queue-sealed"]
FILL_AT = AT + timedelta(seconds=30)


from paper_pending_fixture import accepted_frame


async def prepare(factory, monkeypatch, path, at=FILL_AT):
    queued = path.startswith("queue")
    side = "sell" if path.endswith("sell") else "buy"
    order_id = await place_deferred(factory, "default", side, queued=queued)
    payload = quote("accepted-next", at, queued=path == "queue-sealed")
    if path == "queue-open":
        # Timing tests enter the real book only after whole-order depth qualifies.
        payload["records"][0].update(ask1_volume=3, bid1_price=9.99)
    if path == "queue-sealed":
        payload["records"][0]["volume"] = 1020  # original ahead=10 + own=3 hands
    async with factory() as db:
        await accepted_frame(db, payload)
    clock = [at]
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    async def invoke():
        async with factory() as db:
            token = paper._QUOTE_ROUND_CONTEXT.set(payload)
            try:
                if queued:
                    return await service.reconcile_paper_limit_up_orders(db, account_id="default", now=at)
                return await service.reconcile_paper_deferred_orders(
                    db, account_id="default", round_id=payload["round_id"], now=at)
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)
    return invoke, clock, payload, order_id


async def rejected_result(invoke, path, phase):
    try:
        result = (await invoke())[0]
    except HTTPException as error:
        assert path.startswith("queue")
        assert error.status_code == 409
        assert phase in str(error.detail)
        return
    if result["event"] == "risk_blocked":
        assert phase == "lock_acquired"
        assert result["risk"]["final_level"] == "block"
        assert result["risk"]["evaluation_status"] == "incomplete"
        assert any(r["rule"] == "paper_risk_clock" for r in result["risk"]["block_reasons"])
    else:
        assert not path.startswith("queue")
        assert result["event"] == "rejected", result
        assert phase in result["reason"]
    assert result["fills"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("at,later", [
    (FILL_AT, FILL_AT + timedelta(seconds=90, microseconds=1)),
    (AT.replace(hour=11, minute=29, second=59), AT.replace(hour=11, minute=30)),
    (FILL_AT, FILL_AT + timedelta(days=1)),
    (FILL_AT, FILL_AT - timedelta(microseconds=1)),
    (FILL_AT, None), (FILL_AT, FILL_AT.replace(tzinfo=timezone.utc)),
])
async def test_real_lock_wait_rejects_expired_or_invalid_clock(
    quote_execution_env, monkeypatch, path, at, later,
):
    invoke, clock, _, _ = await prepare(quote_execution_env, monkeypatch, path, at)
    before = await snapshot(quote_execution_env)
    lock = ObservedLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    await lock.lock.acquire()
    task = asyncio.create_task(rejected_result(invoke, path, "lock_acquired"))
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        clock[0] = later
        lock.lock.release()
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()
        if lock.lock.locked():
            lock.lock.release()
        await asyncio.gather(task, return_exceptions=True)
    assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stage", ["account_refresh", "last_preparation"])
async def test_final_book_await_cannot_backdate_fill(quote_execution_env, monkeypatch, path, stage):
    invoke, clock, _, _ = await prepare(quote_execution_env, monkeypatch, path)
    before = await snapshot(quote_execution_env)
    owner, name = (paper, "_refresh_account") if stage == "account_refresh" else (
        (entry_fee_allocation, "load_entry_fee_plan") if path.endswith("sell") else (paper, "_stock_info"))
    real = getattr(owner, name)
    hit = []
    async def delayed(*args, **kwargs):
        result = await real(*args, **kwargs)
        scope = authorization._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            hit.append(True)
            clock[0] = FILL_AT + timedelta(seconds=91)
        return result
    monkeypatch.setattr(owner, name, delayed)
    await rejected_result(invoke, path, "before_mutation")
    assert hit
    assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_success_uses_final_logical_clock_and_retains_original_decision(
    quote_execution_env, monkeypatch, path,
):
    invoke, clock, payload, order_id = await prepare(quote_execution_env, monkeypatch, path)
    real = paper._refresh_account
    async def preparation(*args, **kwargs):
        result = await real(*args, **kwargs)
        scope = authorization._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            clock[0] = FILL_AT + timedelta(seconds=2)
        return result
    monkeypatch.setattr(paper, "_refresh_account", preparation)
    result = (await invoke())[0]
    assert result["event"] == ("filled" if path.startswith("queue") else "partial"), result
    async with quote_execution_env() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        trade = await db.get(PaperTradeLog, int(fill.broker_trade_id))
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == order_id))
        assert fill.filled_at == trade.trade_time == FILL_AT + timedelta(seconds=2)
        assert order.decision_at == AT and order.decision_round_id == "decision"
        assert fill.decision_round_id == "decision" and fill.fill_round_id == "accepted-next"
        raw = json.loads(fill.raw_json)
        timing = raw["ledger_execution_timing"]
        frozen = raw["pending_execution_timing"]
        assert frozen["decision_at"] == AT.isoformat()
        assert frozen["quote_committed_at"] == FILL_AT.isoformat()
        assert frozen["request_id"] == trade.signal_id
        assert timing["input_sha256"] == hashlib.sha256(service._json_dumps(frozen).encode()).hexdigest()
        assert timing["physical_commit_at"] is None
        assert timing["before_mutation_checked_at"] == clock[0].isoformat()
        assert timing["dispatch_validated_at"] == FILL_AT.isoformat()
        assert payload["committed_at"] == FILL_AT
        if path.startswith("queue"):
            assert result["queue"]["filled_at"] == clock[0].isoformat(sep=" ")
        elif path.endswith("buy"):
            assert (await db.scalars(select(PaperPosition))).one().buy_time == clock[0]


async def set_original_deadline(factory, monkeypatch, *, queued, signal_ttl=None, cancel_time=None):
    """Construct explicit ORIGINAL fixture contract, never patch the clock guard."""
    async with factory() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        risk = json.loads(order.risk_json)
        meta = risk["paper_limit_up_queue" if queued else "paper_deferred_order"]
        if cancel_time is not None:
            meta["cancel_time"] = cancel_time
        if signal_ttl is not None:
            meta["buy_validity"] = {
                "schema": "pending_buy_validity_v1", "status": "valid",
                "account_name": order.account_id, "code": order.code, "source": order.source,
                "signal_id": order.signal_id, "strategy_version": order.strategy_version,
                "confirmed_at": AT.isoformat(),
                "expires_at": (AT + timedelta(seconds=signal_ttl)).isoformat(),
                "max_execution_delay_sec": signal_ttl}
            monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda o: o.side == "buy")
            # Clock boundary, not a synthetic proof of current strategy/route alpha.
            monkeypatch.setattr(service, "_pending_buy_current_status", AsyncMock(return_value=("valid", "")))
        order.risk_json = json.dumps(risk)
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["deferred-buy", "queue-open", "queue-sealed"])
@pytest.mark.parametrize("extra,allowed", [(0, True), (0.000001, False)])
@pytest.mark.parametrize("deadline", ["quote", "signal"])
async def test_original_inclusive_deadline_not_extended_by_live_settings(
    quote_execution_env, monkeypatch, path, extra, allowed, deadline,
):
    invoke, clock, _, _ = await prepare(quote_execution_env, monkeypatch, path)
    if deadline == "signal":
        await set_original_deadline(quote_execution_env, monkeypatch,
            queued=path.startswith("queue"), signal_ttl=40)
    target = (FILL_AT + timedelta(seconds=90) if deadline == "quote" else AT + timedelta(seconds=40))
    before = await snapshot(quote_execution_env)
    real = paper._refresh_account
    async def preparation(*args, **kwargs):
        result = await real(*args, **kwargs)
        scope = authorization._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            clock[0] = target + timedelta(seconds=extra)
            monkeypatch.setattr(paper.settings, "PAPER_EXECUTION_QUOTE_MAX_AGE_SEC", 900)
            monkeypatch.setattr(paper.settings, "PAPER_PENDING_BUY_MAX_AGE_SEC", 900)
        return result
    monkeypatch.setattr(paper, "_refresh_account", preparation)
    if allowed:
        result = (await invoke())[0]
        assert result["fills"] and result["fills"][0]["filled_at"] == clock[0].isoformat(sep=" ")
    else:
        await rejected_result(invoke, path, "before_mutation")
        assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["queue-open", "queue-sealed"])
@pytest.mark.parametrize("extra,allowed", [(0, True), (0.000001, False)])
async def test_queue_original_cutoff_inclusive_at_both_ledger_phases(
    quote_execution_env, monkeypatch, path, extra, allowed,
):
    at = AT.replace(hour=14, minute=49, second=59)
    invoke, clock, _, _ = await prepare(quote_execution_env, monkeypatch, path, at)
    before = await snapshot(quote_execution_env)
    real = paper._refresh_account
    async def preparation(*args, **kwargs):
        result = await real(*args, **kwargs)
        scope = authorization._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            clock[0] = at.replace(minute=50, second=0) + timedelta(seconds=extra)
            monkeypatch.setattr(paper.settings, "PAPER_HIGHBOARD_QUEUE_CANCEL_TIME", "14:59")
        return result
    monkeypatch.setattr(paper, "_refresh_account", preparation)
    if allowed:
        result = (await invoke())[0]
        assert result["event"] == "filled"
    else:
        await rejected_result(invoke, path, "before_mutation")
        assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_closing_session_is_half_open_even_when_queue_cutoff_is_later(
    quote_execution_env, monkeypatch, path,
):
    at = AT.replace(hour=14, minute=56, second=59)
    invoke, clock, _, _ = await prepare(quote_execution_env, monkeypatch, path, at)
    if path.startswith("queue"):
        await set_original_deadline(quote_execution_env, monkeypatch, queued=True, cancel_time="14:59")
    before = await snapshot(quote_execution_env)
    real = paper._refresh_account
    async def preparation(*args, **kwargs):
        result = await real(*args, **kwargs)
        scope = authorization._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            clock[0] = at.replace(minute=57, second=0)
        return result
    monkeypatch.setattr(paper, "_refresh_account", preparation)
    await rejected_result(invoke, path, "before_mutation")
    assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("invalid", [
    "calendar", "round", "unhealthy", "round_clock", "version", "source_future",
    "received_future", "clock_missing", "decision_future", "decision_missing",
])
async def test_missing_or_contradictory_pending_preflight_cannot_book(
    quote_execution_env, monkeypatch, path, invalid,
):
    invoke, _, payload, _ = await prepare(quote_execution_env, monkeypatch, path)
    async with quote_execution_env() as db:
        row = (await db.scalars(select(QuoteRound))).one()
        if invalid == "calendar":
            await db.delete(await db.get(TradeCalendarModel, FILL_AT.date()))
        elif invalid == "round":
            await db.delete(row)
        elif invalid == "unhealthy":
            row.quality_status = "degraded"
        elif invalid == "round_clock":
            row.committed_at += timedelta(seconds=1)
        elif invalid == "version":
            row.config_version = "different"
        elif invalid in ("decision_future", "decision_missing"):
            order = (await db.scalars(select(TradeOrder))).one()
            if invalid == "decision_future":
                order.decision_at = FILL_AT + timedelta(seconds=1)
            else:
                order.decision_round_id = None
        elif invalid == "clock_missing":
            payload["records"][0]["received_at"] = None
        else:
            key = "source_quote_at" if invalid == "source_future" else "received_at"
            payload["records"][0][key] = FILL_AT + timedelta(seconds=1)
        await db.commit()
    before = await snapshot(quote_execution_env)
    result = await invoke()
    assert not any(item["fills"] for item in result)
    assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_query_wait_cannot_borrow_afternoon_session_from_morning(
    quote_execution_env, monkeypatch, path,
):
    at = AT.replace(hour=11, minute=29, second=59)
    # Freeze this artificial large TTL before creating the strategy-versioned
    # fixture order; changing it later correctly trips the original version gate.
    monkeypatch.setattr(paper.settings, "PAPER_EXECUTION_QUOTE_MAX_AGE_SEC", 9000)
    invoke, clock, _, _ = await prepare(quote_execution_env, monkeypatch, path, at)
    before = await snapshot(quote_execution_env)
    real = service._pre_trade_risk_check
    async def risk_wait(*args, **kwargs):
        result = await real(*args, **kwargs)
        clock[0] = at.replace(hour=13, minute=0, second=0)
        return result
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk_wait)
    result = await invoke()
    assert result[0]["event"] == "waiting", result
    assert "连续竞价" in result[0]["reason"]
    assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["deferred-buy", "deferred-sell"])
async def test_new_partial_keeps_previous_fill_timing_proof_immutable(
    quote_execution_env, monkeypatch, path,
):
    from test_paper_orphan_fill_guard import reconcile
    invoke, _, _, order_id = await prepare(quote_execution_env, monkeypatch, path)
    assert (await invoke())[0]["event"] == "partial"
    async with quote_execution_env() as db:
        first = (await db.scalars(select(TradeFill))).one()
        frozen = tuple((await db.execute(select(*TradeFill.__table__.columns)
            .where(TradeFill.id == first.id))).one())
    later = FILL_AT + timedelta(seconds=30)
    result = await reconcile(quote_execution_env, "default", "later", later)
    assert result[0]["event"] == "partial"
    async with quote_execution_env() as db:
        rows = list((await db.scalars(select(TradeFill).order_by(TradeFill.id))).all())
        assert len(rows) == 2
        assert tuple((await db.execute(select(*TradeFill.__table__.columns)
            .where(TradeFill.id == rows[0].id))).one()) == frozen
        assert json.loads(rows[0].raw_json)["pending_execution_timing"]["quote_round_id"] == "accepted-next"
        assert json.loads(rows[1].raw_json)["pending_execution_timing"]["quote_round_id"] == "later"
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == order_id))
        assert order.decision_at == AT and order.filled_quantity == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["queue-open", "queue-sealed"])
@pytest.mark.parametrize("fault", ["not_accepted", "no_receipt"])
async def test_queue_missing_acceptance_or_receipt_rolls_back_real_book(
    quote_execution_env, monkeypatch, path, fault,
):
    from app.trading.broker import PaperBrokerAdapter
    invoke, _, _, _ = await prepare(quote_execution_env, monkeypatch, path)
    before = await snapshot(quote_execution_env)
    real = PaperBrokerAdapter.place_order
    booked = []
    async def damaged(self, db, req):
        result = await real(self, db, req)
        assert result.accepted and result.fills
        booked.append(result.fills[0].broker_trade_id)
        if fault == "not_accepted":
            result.accepted = False
        else:
            result.fills = []
        return result
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", damaged)
    with pytest.raises(ValueError, match="排队撮合缺少真实成交回报"):
        await invoke()
    assert len(booked) == 1
    assert await snapshot(quote_execution_env) == before
