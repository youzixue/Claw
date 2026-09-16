"""Real isolated books/receipts: a different order cannot ignore a durable orphan.
Risk/signal policy mocks isolate reconciliation; original clock/depth/atomic/T+1
and accounting stay real. Deliberate damage touches ONLY temporary SQLite rows.
"""
import asyncio
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.trading import service
from app.trading.paper_authorization import paper_transaction_active
from app.trading.paper_execution_integrity import account_execution_integrity_evidence
from app.trading.broker import PaperBrokerAdapter
from test_quote_round_execution import quote_execution_env
from test_paper_atomic_execution_20260914 import (
    isolated_transaction_policy_and_calendar, snapshot, WaitingTransactionLock,
)
from test_paper_account_round_capacity_20260914 import prepare as depth_prepare, fill_count, FILL_AT
from paper_pending_fixture import accepted_frame

PATHS = ["immediate-buy", "immediate-sell", "deferred-buy", "deferred-sell", "queue-buy", "queue-sealed"]


async def prepare(factory, monkeypatch, path, *, other_account=False):
    side = "sell" if path.endswith("sell") else "buy"
    invoke, payload, commands = await depth_prepare(factory, monkeypatch,
        ["immediate-"+side, "queue-buy" if path=="queue-sealed" else path],
        hands=10, accounts=["promotion" if other_account else "default", "default"])
    async def second():
        at = FILL_AT
        if path == "queue-sealed":
            at += timedelta(seconds=30)
            payload.update(round_id="sealed-next", committed_at=at, as_of_at=at)
            payload["records"][0].update(price=11, ask1_price=0, bid1_price=11, bid1_volume=10,
                volume=1020, quote_round_id="sealed-next", source_quote_at=at, received_at=at, updated_at=at)
            async with factory() as db:
                await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
        async with factory() as db:
            token = paper._QUOTE_ROUND_CONTEXT.set(payload)
            try:
                if path.startswith("immediate"):
                    return [await service.submit_order(db, commands[1])]
                if path.startswith("queue"):
                    return await service.reconcile_paper_limit_up_orders(db, account_id="default", now=at)
                return await service.reconcile_paper_deferred_orders(
                    db, account_id="default", now=at, round_id=payload["round_id"])
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)
    return lambda:invoke(0), second


async def remove_first_receipt(factory):
    async with factory() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        await db.delete(fill)
        await db.commit()


async def assert_blocked_unchanged(factory, second):
    before = await snapshot(factory)
    result = await second()
    assert fill_count(result) == 0, result
    assert result[-1]["order"]["status"] == "risk_blocked", result
    async with factory() as db:
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id==result[-1]["order"]["order_id"]))
        evidence = json.loads(order.risk_json)["paper_account_execution_integrity"]
        assert evidence["status"] == "blocked"
        assert evidence["scope"] == "account_trade_day_code_side"
        assert evidence["automatic_repair_allowed"] is False
        assert evidence["reason_code"] in {
            "ledger_without_unique_receipt", "receipt_without_ledger",
            "ledger_receipt_identity_conflict", "unattributed_receipt"}
    assert await snapshot(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_deleted_whole_receipt_cannot_be_hidden_by_another_order(quote_execution_env, monkeypatch, path):
    first, second = await prepare(quote_execution_env, monkeypatch, path)
    assert fill_count(await first()) == 1
    await remove_first_receipt(quote_execution_env)
    await assert_blocked_unchanged(quote_execution_env, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_legacy_book_committed_without_receipt_blocks_other_order(quote_execution_env, monkeypatch, path):
    first, second = await prepare(quote_execution_env, monkeypatch, path)
    real = PaperBrokerAdapter.place_order
    hit = []
    async def legacy_split(self, db, req):
        result = await real(self, db, req)
        assert result.fills
        await db.commit()  # Explicit fixture for an OLD split-commit deployment.
        hit.append(True)
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(PaperBrokerAdapter, "place_order", legacy_split)
        with pytest.raises(asyncio.CancelledError):
            await first()
    assert hit
    await assert_blocked_unchanged(quote_execution_env, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [
    "trade_deleted", "receipt_trade_id", "receipt_fee", "receipt_tax", "trade_price",
    "trade_quantity", "trade_signal", "trade_round", "trade_clock", "trade_account",
    "duplicate_receipt", "unkeyed_same_day_ledger", "raw_trade_signal", "raw_guard_sha", "raw_guard_missing",
])
async def test_cross_table_identity_or_economic_mismatch_blocks_new_order(
    quote_execution_env, monkeypatch, damage,
):
    factory = quote_execution_env
    first, second = await prepare(factory, monkeypatch, "immediate-buy")
    assert fill_count(await first()) == 1
    async with factory() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        trade = await db.get(PaperTradeLog, int(fill.broker_trade_id))
        if damage == "trade_deleted":
            await db.delete(trade)
        elif damage == "receipt_trade_id":
            fill.broker_trade_id = "000"+fill.broker_trade_id
        elif damage == "receipt_fee":
            fill.commission += 1
        elif damage == "receipt_tax":
            fill.tax += 1
        elif damage == "trade_price":
            trade.price += 0.01
        elif damage == "trade_quantity":
            trade.amount += 100
        elif damage == "trade_signal":
            trade.signal_id = "unrelated-signal"
        elif damage == "trade_round":
            trade.fill_round_id = "unrelated-round"
        elif damage == "trade_clock":
            trade.trade_time += timedelta(seconds=1)
        elif damage == "trade_account":
            other = await paper._get_or_create_account(db, "promotion")
            trade.account_id = other.id
        elif damage.startswith("raw_"):
            raw = json.loads(fill.raw_json)
            if damage == "raw_trade_signal":
                raw["signal_id"] = "unrelated-raw-signal"
            elif damage == "raw_guard_sha":
                raw["ledger_execution_timing"]["input_sha256"] = "damaged"
            else:
                raw.pop("ledger_execution_timing")
            fill.raw_json = service._json_dumps(raw)
        elif damage == "unkeyed_same_day_ledger":
            trade.fill_round_id = None
            await db.delete(fill)
        elif damage == "duplicate_receipt":
            values = {c.name:getattr(fill,c.name) for c in TradeFill.__table__.columns if c.name!="id"}
            values["fill_id"] = "duplicate-fixture"
            db.add(TradeFill(**values))
        await db.commit()
    await assert_blocked_unchanged(factory, second)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_valid_prior_pair_keeps_routes_executable_and_freezes_check(quote_execution_env, monkeypatch, path):
    factory = quote_execution_env
    first, second = await prepare(factory, monkeypatch, path)
    assert fill_count(await first()) == 1
    result = await second()
    assert fill_count(result) == 1
    async with factory() as db:
        fill = await db.scalar(select(TradeFill).where(TradeFill.order_id==result[0]["order"]["order_id"]))
        raw = json.loads(fill.raw_json)
        p = raw.get("immediate_execution_evidence") or raw["pending_execution_timing"]
        proof = p["account_execution_integrity"]
        assert proof["status"] == "validated"
        assert proof["checked_trade_count"] == proof["checked_receipt_count"] == 1
        assert len(proof["matched_pairs_sha256"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_other_counterfactual_account_orphan_does_not_freeze_this_account(
    quote_execution_env, monkeypatch, path,
):
    first, second = await prepare(quote_execution_env, monkeypatch, path, other_account=True)
    assert fill_count(await first()) == 1
    await remove_first_receipt(quote_execution_env)
    assert fill_count(await second()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["immediate-buy", "immediate-sell"])
async def test_atomic_rollback_does_not_create_an_orphan_barrier(quote_execution_env, monkeypatch, path):
    first, second = await prepare(quote_execution_env, monkeypatch, path)
    real = PaperBrokerAdapter.place_order
    async def abort(self, db, req):
        result = await real(self, db, req)
        assert result.fills
        raise ValueError("fixture rollback after genuine book")
    with monkeypatch.context() as patch:
        patch.setattr(PaperBrokerAdapter, "place_order", abort)
        assert fill_count(await first()) == 0
    assert fill_count(await second()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_receipt_deleted_while_waiting_for_fill_lock_is_read_fresh(
    quote_execution_env, monkeypatch, path,
):
    factory = quote_execution_env
    first, second = await prepare(factory, monkeypatch, path)
    assert fill_count(await first()) == 1
    lock = WaitingTransactionLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    await lock.lock.acquire()
    task = asyncio.create_task(second())
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        # Independent DB writer after the order checkpoint, before lock acquisition.
        await remove_first_receipt(factory)
        before = await snapshot(factory)
        lock.lock.release()
        result = await asyncio.wait_for(task, 5)
        assert fill_count(result) == 0
        assert result[0]["order"]["status"] == "risk_blocked"
        assert await snapshot(factory) == before
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if lock.lock.locked():
            lock.lock.release()
    assert not lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("kind", ["database", "cancel"])
async def test_integrity_query_failure_is_not_a_business_rejection_or_permission(
    quote_execution_env, monkeypatch, path, kind,
):
    factory = quote_execution_env
    first, second = await prepare(factory, monkeypatch, path)
    prior = await first()
    assert fill_count(prior) == 1
    prior_order_id = prior[0]["order"]["order_id"]
    before, fired = await snapshot(factory), []
    real = AsyncSession.execute
    failure = (OperationalError("integrity fixture SELECT", {}, RuntimeError("read failed"))
               if kind == "database" else asyncio.CancelledError())
    async def execute(db, stmt, *args, **kwargs):
        if paper_transaction_active(db) and "t_id" in getattr(stmt, "selected_columns", {}):
            fired.append(True)
            assert paper._TRADE_LOCK.locked()
            raise failure
        return await real(db, stmt, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", execute)
        with pytest.raises(type(failure)) as error:
            await second()
        assert error.value is failure
    assert fired and not paper._TRADE_LOCK.locked()
    assert await snapshot(factory) == before
    async with factory() as db:
        orders = (await db.scalars(select(TradeOrder).order_by(TradeOrder.id))).all()
        assert len(orders) == 2
        current = next(order for order in orders if order.order_id != prior_order_id)
        assert current.status not in {"rejected", "risk_blocked", "filled"}
        assert current.filled_quantity == 0


@pytest.mark.asyncio
async def test_integrity_check_rejects_use_outside_current_atomic_unit(quote_execution_env):
    async with quote_execution_env() as db:
        with pytest.raises(HTTPException) as error:
            await account_execution_integrity_evidence(db, None, {})
        assert error.value.status_code == 403
