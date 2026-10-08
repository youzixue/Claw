"""Real actual partial fills then local cancellation/expiry; temporary SQLite only."""
import asyncio
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import paper
from app.models.trading import TradeOrder, TradeFill
from app.trading import service, paper_authorization as auth, paper_after_hours_resources as resources
from app.trading import paper_after_hours_allocation as allocation
from test_trading_api import trading_client
from test_paper_after_hours_20261002 import environment, AT
from test_paper_after_hours_allocation_20261002 import audited_fixture
from test_paper_after_hours_atomic_20261002 import pending, frame, snapshot
from test_paper_after_hours_partial_atomic_20261002 import invoke, next_frame
from test_paper_after_hours_consumers_20261002 import read_evidence


async def cancel(env, identifier):
    async with env.maker() as db:
        return await service.cancel_order(db, identifier)


async def staged(env, quantity=300):
    identifier = await pending(env, quantity=quantity)
    payload = frame(quantity=150)
    await invoke(env, identifier, payload)
    return identifier, payload


@pytest.mark.asyncio
async def test_actual_partial_user_cancel_preserves_all_economics_and_consumers(environment, audited_fixture):
    identifier, payload = await staged(environment)
    before = await snapshot(environment)
    response = await environment.client.post("/trading/orders/"+identifier+"/cancel")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "canceled"
    assert result["order"]["filled_quantity"] == 100
    assert result["order"]["avg_fill_price"] == 11.57
    assert await snapshot(environment) == before
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == identifier))
        proof = json.loads(row.risk_json)["paper_after_hours_cancel"]
        assert proof["contract_version"] == allocation.PARTIAL_CANCEL_OBSERVATION_PROTOCOL
        assert proof["order_id"] == identifier and proof["original_quantity"] == 300
        assert proof["filled_quantity_preserved"] == 100 and proof["unfilled_quantity"] == 200
        assert proof["verified_fill_ids"] == [await db.scalar(select(TradeFill.fill_id))]
        assert proof["exchange_cancel_receipt"] is None and proof["reason"] == "user_cancel"
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "validated"
        assert all(v["status"] == "verified" and v["scale_in"] is False for v in quota.values())
    assert (await cancel(environment, identifier))["status"] == "unchanged"
    with pytest.raises(HTTPException):
        await invoke(environment, identifier, next_frame(payload))
    assert await snapshot(environment) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", [0, 100])
async def test_canceled_head_no_longer_blocks_but_never_returns_consumed_capacity(environment, audited_fixture, filled):
    first = await pending(environment, quantity=300)
    environment.clock[0] = AT
    later = await pending(environment, quantity=100)
    payload = frame(quantity=150)
    if filled:
        await invoke(environment, first, payload)
    assert (await cancel(environment, first))["status"] == "canceled"
    if filled:
        old = await snapshot(environment)
        result = await invoke(environment, later, payload)
        assert result["fills"] == [] and await snapshot(environment) == old
        payload = next_frame(payload)
        environment.clock[0] = AT+timedelta(seconds=3)
    result = await invoke(environment, later, payload)
    assert result["order"]["status"] == "filled", result
    assert result["order"]["filled_quantity"] == 100
    async with environment.maker() as db:
        assert await db.scalar(select(resources.State.revision)) == (2 if filled else 1)
        assert await db.scalar(select(func.sum(TradeFill.quantity))) == 100+filled


@pytest.mark.asyncio
@pytest.mark.parametrize("when", [AT.replace(minute=30), AT.replace(minute=31),
                                  AT+timedelta(days=1, hours=17)])
async def test_expiry_cancels_only_actual_partial_remainder_even_on_later_day(environment, audited_fixture, when):
    identifier, _ = await staged(environment)
    before = await snapshot(environment)
    environment.clock[0] = when
    async with environment.maker() as db:
        result = await service.reconcile_paper_after_hours_intents(db)
        row = await db.scalar(select(TradeOrder))
        proof = json.loads(row.risk_json)["paper_after_hours_cancel"]
        assert row.status == "canceled" and row.filled_quantity == 100
        assert proof["reason"] == "session_end" and proof["unfilled_quantity"] == 200
        assert proof["canceled_at"] == when.isoformat()
        # Recognition needs no NEW quote or current fee policy after close.
        await resources._verified_partial_receipt_bindings(db,
            account_numeric_id=json.loads(row.risk_json)["paper_after_hours_intent"]["numeric_account_id"],
            code=row.code, trade_date=row.trade_date, cutoff=when)
    assert result["canceled"] == [identifier] and result["errors"] == []
    assert result["fills"] == [] and result["automatic_execution"] is False
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_pre_end_reconcile_reports_remaining_not_original_quantity(environment, audited_fixture):
    identifier, _ = await staged(environment)
    before = await snapshot(environment)
    environment.clock[0] = AT.replace(minute=29)
    async with environment.maker() as db:
        result = await service.reconcile_paper_after_hours_intents(db)
    assert result["canceled"] == [] and result["waiting"][0]["order_id"] == identifier
    assert result["waiting"][0]["filled_quantity_preserved"] == 100
    assert result["waiting"][0]["unfilled_quantity"] == 200
    assert await snapshot(environment) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["projection", "raw", "extra_fill", "previous_clock"])
async def test_unverified_partial_facts_or_previous_clock_cannot_be_canceled(environment, audited_fixture, damage):
    identifier, _ = await staged(environment)
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        if damage == "projection":
            row.filled_quantity = 200
        elif damage == "raw":
            # Damaged storage fixture only; actual book guards otherwise forbid it.
            from app.trading.paper_after_hours_resource_schema import sqlite_guards
            await db.execute(text("DROP TRIGGER af_bound_trade_fill_no_update"))
            await db.execute(text("UPDATE trade_fill SET raw_json='{}'"))
            statement = next(s for s in sqlite_guards() if "af_bound_trade_fill_no_update" in s)
            await db.execute(text(statement))
        elif damage == "extra_fill":
            db.add(TradeFill(fill_id="orphan", order_id=identifier, broker="paper", code="000001",
                side="buy", price=11.57, quantity=100, commission=5, tax=0, filled_at=AT))
        else:
            risk = json.loads(row.risk_json)
            risk["paper_after_hours_consumption"]["terminal_checked_at"] = (AT+timedelta(seconds=2)).isoformat()
            row.risk_json = json.dumps(risk)
        await db.commit()
    before = await snapshot(environment)
    with pytest.raises(HTTPException) as error:
        await cancel(environment, identifier)
    assert error.value.status_code == 409 and await snapshot(environment) == before
    async with environment.maker() as db:
        assert await db.scalar(select(TradeOrder.status)) == "partial"


@pytest.mark.asyncio
async def test_bad_partial_expiry_does_not_abort_next_zero_remainder(environment, audited_fixture):
    identifier, _ = await staged(environment)
    environment.clock[0] = AT+timedelta(seconds=2)
    good = await pending(environment)
    async with environment.maker() as db:
        await db.execute(text("UPDATE trade_order SET filled_quantity=200 WHERE order_id=:id"), {"id":identifier})
        await db.commit()
    environment.clock[0] = AT.replace(minute=31)
    before = await snapshot(environment)
    async with environment.maker() as db:
        result = await service.reconcile_paper_after_hours_intents(db)
        assert await db.scalar(select(TradeOrder.status).where(TradeOrder.order_id == identifier)) == "partial"
        assert await db.scalar(select(TradeOrder.status).where(TradeOrder.order_id == good)) == "canceled"
    assert result["canceled"] == [good]
    assert result["errors"] == [{"order_id":identifier,"http_status":409}]
    assert await snapshot(environment) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["after_history", "after_cas"])
async def test_cancel_clock_rollback_rolls_back_only_cancel_projection(environment, audited_fixture, monkeypatch, phase):
    identifier, _ = await staged(environment)
    before = await snapshot(environment)
    if phase == "after_history":
        original = resources._verified_partial_receipt_bindings
        async def backwards(*args, **kwargs):
            result = await original(*args, **kwargs)
            environment.clock[0] = AT
            return result
        monkeypatch.setattr(resources, "_verified_partial_receipt_bindings", backwards)
    else:
        original = AsyncSession.refresh
        async def backwards(self, instance, *args, **kwargs):
            await original(self, instance, *args, **kwargs)
            if isinstance(instance, TradeOrder) and instance.status == "canceled":
                environment.clock[0] = AT
        monkeypatch.setattr(AsyncSession, "refresh", backwards)
    with pytest.raises(HTTPException):
        await cancel(environment, identifier)
    assert await snapshot(environment) == before
    async with environment.maker() as db:
        assert await db.scalar(select(TradeOrder.status)) == "partial"


@pytest.mark.asyncio
async def test_lost_cancel_commit_ack_preserves_durable_economics_and_canceled_state(environment, audited_fixture, monkeypatch):
    identifier, _ = await staged(environment)
    before = await snapshot(environment)
    original = AsyncSession.commit
    fired = []
    async def lost_ack(self):
        armed = auth.paper_transaction_active(self)
        await original(self)
        if armed and not fired:
            fired.append(True)
            raise RuntimeError("lost cancel commit acknowledgment")
    monkeypatch.setattr(AsyncSession, "commit", lost_ack)
    with pytest.raises(RuntimeError) as error:
        await cancel(environment, identifier)
    assert auth.paper_execution_requires_reconciliation(error.value)
    assert await snapshot(environment) == before
    assert (await cancel(environment, identifier))["status"] == "unchanged"
    async with environment.maker() as db:
        assert await db.scalar(select(TradeOrder.status)) == "canceled"


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["cancel", "fill"])
async def test_same_lock_serializes_partial_fill_and_cancel_without_erasing_fills(environment, audited_fixture, first):
    identifier, payload = await staged(environment)
    payload = next_frame(payload)
    environment.clock[0] = AT+timedelta(seconds=3)
    await paper._TRADE_LOCK.acquire()
    try:
        a = asyncio.create_task(cancel(environment, identifier) if first == "cancel"
                                else invoke(environment, identifier, payload))
        await asyncio.sleep(0.03)
        b = asyncio.create_task(invoke(environment, identifier, payload) if first == "cancel"
                                else cancel(environment, identifier))
        await asyncio.sleep(0.03)
    finally:
        paper._TRADE_LOCK.release()
    results = await asyncio.gather(a, b, return_exceptions=True)
    if first == "cancel":
        assert results[0]["status"] == "canceled" and isinstance(results[1], HTTPException)
        expected = 100
    else:
        assert results[0]["order"]["filled_quantity"] == 200 and results[1]["status"] == "canceled"
        expected = 200
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        assert row.status == "canceled" and row.filled_quantity == expected
        assert await db.scalar(select(func.sum(TradeFill.quantity))) == expected
        assert await db.scalar(select(resources.State.revision)) == expected//100
        assert json.loads(row.risk_json)["paper_after_hours_cancel"]["filled_quantity_preserved"] == expected
