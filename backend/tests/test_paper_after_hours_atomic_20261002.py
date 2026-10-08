"""Actual service/broker/book/risk on temporary SQLite; full feed is fixture-only.

No production provider/strategy/DB is enabled. No artificial ledger scope or
manufactured TradeFill is used in positive execution tests.
"""
import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog, PaperSaleAccounting
from app.models.trading import TradeOrder, TradeFill
from app.trading import service, paper_authorization as auth, paper_after_hours_resources as resources
from app.trading.broker import PaperBrokerAdapter
from test_trading_api import trading_client
from test_paper_after_hours_20261002 import environment, submit, AT
from test_paper_after_hours_allocation_20261002 import audited_fixture, feed
from test_paper_after_hours_consumers_20261002 import read_evidence


def frame(*, side="sell", quantity=300):
    payload = feed()
    payload.update(code="000001", exchange="SZSE", session_id="SZSE.000001.2026-09-30")
    payload["official_close"].update(code="000001", exchange="SZSE", price=11.57)
    payload["initial"]["orders"][0].update(side=side, quantity=quantity, limit_price=11.57)
    return payload


async def invoke(env, order_id, payload=None):
    async with env.maker() as db:
        return await service._execute_paper_after_hours_order(db, order_id,
            feed=frame() if payload is None else payload)


async def snapshot(env):
    async with env.maker() as db:
        answer = {}
        for model in (PaperTradeLog, PaperSaleAccounting, TradeFill, resources.Receipt, resources.State):
            answer[model.__tablename__] = [tuple(row) for row in
                (await db.execute(select(*model.__table__.columns))).all()]
        answer["cash"] = list((await db.execute(select(PaperAccount.id, PaperAccount.current_capital))).all())
        answer["positions"] = list((await db.execute(select(PaperPosition.code, PaperPosition.buy_amount,
            PaperPosition.buy_price, PaperPosition.buy_time, PaperPosition.is_closed))).all())
        return answer


async def pending(env, **changes):
    result = await submit(env, **changes)
    assert result["order"]["status"] == "submitted", result
    env.clock[0] = AT + timedelta(seconds=1)
    return result["order"]["order_id"]


@pytest.mark.asyncio
async def test_real_buy_fixed_close_receipt_and_consumption_commit_together(environment, audited_fixture):
    identifier = await pending(environment)
    result = await invoke(environment, identifier)
    assert result["order"]["status"] == "filled" and result["order"]["price"] == 11.6
    assert result["order"]["avg_fill_price"] == result["fills"][0]["price"] == 11.57
    assert result["fills"][0]["quantity"] == 100
    environment.forbidden.assert_not_awaited()  # Neither ordinary five-depth nor ordinary dispatch.
    async with environment.maker() as db:
        account = await db.scalar(select(PaperAccount))
        trade = await db.scalar(select(PaperTradeLog))
        receipt = await db.scalar(select(resources.Receipt))
        root = await db.scalar(select(resources.State))
        assert account.current_capital == 50000 - 1157 - paper._commission(1157)
        assert trade.trade_time == environment.clock[0] and trade.price == 11.57
        assert trade.decision_round_id != trade.fill_round_id
        assert root.revision == 1 and receipt.paper_trade_id == trade.id
        assert json.loads(receipt.payload_json)["allocation"]["resources"][0]["shares"] == 100
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "validated" and quota[trade.id]["status"] == "verified"
        assert quota[trade.id]["scale_in"] is False
    before = await snapshot(environment)
    replay = await invoke(environment, identifier, {})  # No new quote needed to recognize a durable old fill.
    assert replay["idempotent_replay"] and await snapshot(environment) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sse_fixed_price", "szse_fixed_price", "ths_after_hours"])
async def test_production_aggregate_sources_cannot_use_the_new_book_branch(environment, source):
    from app.trading import paper_after_hours_allocation as allocator
    identifier = await pending(environment)
    payload = frame()
    payload.update(source=source, source_version=allocator._PROVIDERS[source].version,
        queue_verified=True, execution_authorized=True, capability="order_level")
    before = await snapshot(environment)
    result = await invoke(environment, identifier, payload)
    assert result["status"] == "waiting" and result["reason"] == "provider_aggregate_only"
    assert result["fills"] == [] and await snapshot(environment) == before


@pytest.mark.asyncio
async def test_full_fill_fifo_head_cannot_be_skipped_and_zero_fill_cancel_releases_local_head(environment, audited_fixture):
    first = await pending(environment, quantity=200)
    environment.clock[0] = AT
    second = await pending(environment, quantity=100)
    payload = frame(quantity=100)
    result = await invoke(environment, second, payload)
    assert result["reason"] == "earlier_local_full_fill_intent"
    assert (await invoke(environment, first, payload))["fills"] == []
    async with environment.maker() as db:
        canceled = await service.cancel_order(db, first)
        assert canceled["status"] == "canceled"
    assert (await invoke(environment, second, payload))["order"]["status"] == "filled"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["broker_after_book", "resource_after_cas", "order_cas", "receipt_insert"])
async def test_every_post_book_failure_rolls_back_real_economics_and_durable_consumption(
        environment, audited_fixture, monkeypatch, fault):
    identifier = await pending(environment)
    before = await snapshot(environment)
    fired = []
    if fault == "broker_after_book":
        original = PaperBrokerAdapter.place_order
        async def fail(self, db, req):
            result = await original(self, db, req)
            assert result.fills
            fired.append(True)
            raise ValueError("book complete but broker ack failed")
        monkeypatch.setattr(PaperBrokerAdapter, "place_order", fail)
    elif fault in {"resource_after_cas", "order_cas"}:
        original = resources._persist_fill_resources
        async def fail(*args, **kwargs):
            result = await original(*args, **kwargs)
            assert result["revision"] == 1
            fired.append(True)
            if fault == "order_cas":
                await args[0].execute(text("UPDATE trade_order SET status='canceled'"))
                return result
            raise ValueError("resource finalized but receipt unit must roll back")
        monkeypatch.setattr(resources, "_persist_fill_resources", fail)
    else:
        async with environment.maker() as db, db.begin():
            await db.execute(text("CREATE TRIGGER atomic_fixed_reject_fill BEFORE INSERT ON trade_fill "
                "BEGIN SELECT RAISE(ABORT, 'fixture fixed receipt rejected'); END"))
        original = PaperBrokerAdapter.place_order
        async def record(self, db, req):
            result = await original(self, db, req)
            fired.append(True)
            return result
        monkeypatch.setattr(PaperBrokerAdapter, "place_order", record)
    with pytest.raises(Exception) as exc:
        await invoke(environment, identifier)
    assert fired and auth.paper_execution_requires_reconciliation(exc.value)
    assert await snapshot(environment) == before
    async with environment.maker() as db:
        order = await db.scalar(select(TradeOrder))
        assert order.status == "submitted" and order.filled_quantity == 0  # Never fake a broker rejection.


@pytest.mark.asyncio
async def test_lost_commit_ack_preserves_durable_fill_and_replay_is_read_only(environment, audited_fixture, monkeypatch):
    identifier = await pending(environment)
    original = AsyncSession.commit
    fired = []
    async def lost_ack(db):
        owned = auth.paper_transaction_active(db)
        await original(db)
        if owned:
            fired.append(True)
            raise OSError("fixture durable COMMIT acknowledgement lost")
    monkeypatch.setattr(AsyncSession, "commit", lost_ack)
    with pytest.raises(OSError) as exc:
        await invoke(environment, identifier)
    assert fired and auth.paper_execution_requires_reconciliation(exc.value)
    after = await snapshot(environment)
    assert len(after["trade_fill"]) == len(after["paper_trade_log"]) == 1
    assert len(after["paper_after_hours_resource_receipt"]) == 1
    replay = await invoke(environment, identifier, {})
    assert replay["idempotent_replay"] and await snapshot(environment) == after


@pytest.mark.asyncio
async def test_book_await_crossing_session_end_never_mutates_money_or_inventory(environment, audited_fixture, monkeypatch):
    identifier = await pending(environment)
    before = await snapshot(environment)
    original = paper._stock_info
    async def cross(*args, **kwargs):
        value = await original(*args, **kwargs)
        environment.clock[0] = AT.replace(minute=30)
        return value
    monkeypatch.setattr(paper, "_stock_info", cross)
    with pytest.raises(HTTPException) as exc:
        await invoke(environment, identifier)
    assert exc.value.status_code == 409
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_locked_risk_still_blocks_identity_and_no_empty_rule_chain_bypass(environment, audited_fixture, monkeypatch):
    from app.risk.engine import risk_engine
    identifier = await pending(environment)
    before = await snapshot(environment)
    monkeypatch.setattr(risk_engine, "_rules", [])
    result = await invoke(environment, identifier)
    assert result["order"]["status"] == "risk_blocked" and not result["fills"]
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_buy_today_cannot_be_sold_in_fixed_price_session(environment, audited_fixture):
    identifier = await pending(environment)
    await invoke(environment, identifier)
    before = await snapshot(environment)
    sell = await submit(environment, side="sell", price=11.57)
    assert sell["order"]["status"] == "rejected"
    assert sell["risk"]["paper_after_hours_intent"]["reason"] == "T_plus_1_or_insufficient_sellable_quantity"
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_original_account_cannot_be_recreated_in_book_after_locked_risk(environment, audited_fixture, monkeypatch):
    identifier = await pending(environment)
    before = await snapshot(environment)
    original = PaperBrokerAdapter.place_order
    async def close(self, db, req):
        account = await db.scalar(select(PaperAccount))
        account.status = "closed"
        await db.flush()
        return await original(self, db, req)
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", close)
    with pytest.raises(HTTPException) as exc:
        await invoke(environment, identifier)
    assert exc.value.status_code == 409
    assert await snapshot(environment) == before
    async with environment.maker() as db:
        assert await db.scalar(select(func.count()).select_from(PaperAccount)) == 1
        assert await db.scalar(select(PaperAccount.status)) == "active"


@pytest.mark.asyncio
async def test_second_real_order_after_restart_uses_only_unconsumed_native_slices(environment, audited_fixture):
    first = await pending(environment)
    await invoke(environment, first)
    second = await pending(environment)  # Accepted later than the first observed source clock.
    later = AT + timedelta(seconds=2)
    payload = frame()
    payload.update(frame_id="frame.2", source_quote_at=later.isoformat(),
                   received_at=(later + timedelta(milliseconds=100)).isoformat(),
                   available_at=(later + timedelta(milliseconds=200)).isoformat())
    environment.clock[0] = AT + timedelta(seconds=3)
    result = await invoke(environment, second, payload)
    assert result["order"]["status"] == "filled"
    async with environment.maker() as db:
        receipts = list((await db.scalars(select(resources.Receipt).order_by(resources.Receipt.id))).all())
        pieces = [json.loads(r.payload_json)["allocation"]["resources"][0] for r in receipts]
        assert [(p["offset"], p["shares"]) for p in pieces] == [(0, 100), (100, 100)]
        assert await db.scalar(select(resources.State.revision)) == 2
        assert await db.scalar(select(PaperPosition.buy_amount)) == 200


async def seed_overnight(env, *, commission=True):
    yesterday = AT - timedelta(days=1)
    # This legacy projection helper owns its commit outside a paper fill unit.
    async with env.maker() as db:
        account = await db.scalar(select(PaperAccount))
        version = paper._strategy_version("default")
        db.add(PaperPosition(account_id=account.id, code="000001", buy_price=11.5, buy_amount=100,
            buy_time=yesterday, strategy_version=version, current_price=11.57, is_closed=False))
        db.add(PaperTradeLog(account_id=account.id, code="000001", trade_type="buy", price=11.5, amount=100,
            trade_time=yesterday, commission=paper._commission(1150) if commission else None,
            tax=0, strategy_version=version, signal_id="fixture.overnight.buy"))
        await db.flush()
        await paper._refresh_account(db, account)
    return version


@pytest.mark.asyncio
async def test_real_overnight_sale_retains_entry_fee_tax_pnl_and_fixed_price(environment, audited_fixture):
    version = await seed_overnight(environment)
    identifier = await pending(environment, side="sell", price=11.57)
    result = await invoke(environment, identifier, frame(side="buy"))
    assert result["order"]["status"] == "filled"
    actual = result["fills"][0]
    assert actual["price"] == 11.57 and actual["tax"] == paper._stamp_tax(1157)
    expected_pnl = round(7 - paper._commission(1150) - paper._commission(1157) - paper._stamp_tax(1157), 2)
    assert actual["realized_pnl"] == expected_pnl
    async with environment.maker() as db:
        sale = await db.scalar(select(PaperTradeLog).where(PaperTradeLog.trade_type == "sell"))
        accounting = await db.scalar(select(PaperSaleAccounting))
        assert sale.strategy_version == version and accounting is not None
        assert await db.scalar(select(PaperPosition.is_closed)) is True
        assert await db.scalar(select(PaperAccount.current_capital)) == round(
            50000 - 1150 - paper._commission(1150) + 1157 - paper._commission(1157) - paper._stamp_tax(1157), 2)


@pytest.mark.asyncio
async def test_unknown_entry_fee_never_becomes_zero_fee_sale(environment, audited_fixture):
    await seed_overnight(environment, commission=False)
    identifier = await pending(environment, side="sell", price=11.57)
    before = await snapshot(environment)
    with pytest.raises(HTTPException) as exc:
        await invoke(environment, identifier, frame(side="buy"))
    assert exc.value.status_code == 409
    assert "买费分摊" in str(exc.value.detail)
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_external_cancel_during_match_lock_wait_cannot_be_overwritten(environment, audited_fixture, monkeypatch):
    from test_paper_ledger_clock_20260914 import ObservedLock
    identifier = await pending(environment)
    before = await snapshot(environment)
    lock = ObservedLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    await lock.lock.acquire()
    task = asyncio.create_task(invoke(environment, identifier))
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        async with environment.maker() as db, db.begin():
            await db.execute(text("UPDATE trade_order SET status='canceled' WHERE order_id=:id"), {"id": identifier})
        lock.lock.release()
        with pytest.raises(HTTPException) as exc:
            await asyncio.wait_for(task, 5)
        assert exc.value.status_code == 409
        assert await snapshot(environment) == before
        async with environment.maker() as db:
            assert await db.scalar(select(TradeOrder.status)) == "canceled"
    finally:
        if not task.done():
            task.cancel()
        if lock.lock.locked():
            lock.lock.release()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["risk_dispatch", "resource_cas", "cas_terminal"])
async def test_cross_stage_clock_rollback_aborts_the_whole_fill(
        environment, audited_fixture, monkeypatch, phase):
    identifier = await pending(environment)
    before = await snapshot(environment)
    if phase == "risk_dispatch":
        original = service._locked_paper_risk_evidence
        async def rollback_clock(*args, **kwargs):
            environment.clock[0] = AT + timedelta(seconds=3)
            value = await original(*args, **kwargs)
            assert value["status"] == "validated"
            environment.clock[0] = AT + timedelta(seconds=2)
            return value
        monkeypatch.setattr(service, "_locked_paper_risk_evidence", rollback_clock)
    elif phase == "resource_cas":
        original = resources._persist_fill_resources
        async def rollback_clock(*args, **kwargs):
            environment.clock[0] = AT + timedelta(seconds=3)
            value = await original(*args, **kwargs)
            assert value["revision"] == 1
            environment.clock[0] = AT + timedelta(seconds=1)
            return value
        monkeypatch.setattr(resources, "_persist_fill_resources", rollback_clock)
    else:
        original = AsyncSession.refresh
        async def rollback_clock(db, instance, *args, **kwargs):
            value = await original(db, instance, *args, **kwargs)
            if (auth.paper_transaction_active(db) and isinstance(instance, TradeOrder)
                    and instance.status == "filled"):
                environment.clock[0] = AT + timedelta(milliseconds=500)
            return value
        monkeypatch.setattr(AsyncSession, "refresh", rollback_clock)
    with pytest.raises(HTTPException) as exc:
        await invoke(environment, identifier)
    assert exc.value.status_code == 409 and "时钟" in str(exc.value.detail)
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_proved_incompatible_limit_is_not_a_legal_fifo_head(environment, audited_fixture):
    first = await pending(environment, price=11.57)
    environment.clock[0] = AT
    second = await pending(environment, price=11.60)
    payload = frame()
    payload["official_close"]["price"] = 11.58
    payload["initial"]["orders"][0]["limit_price"] = 11.58
    result = await invoke(environment, second, payload)
    assert result["order"]["status"] == "filled" and result["fills"][0]["price"] == 11.58
    async with environment.maker() as db:
        assert await db.scalar(select(TradeOrder.status).where(TradeOrder.order_id == first)) == "submitted"


@pytest.mark.asyncio
async def test_bad_earlier_intent_cannot_be_skipped_even_if_its_limit_is_incompatible(
        environment, audited_fixture):
    first = await pending(environment, price=11.57)
    environment.clock[0] = AT
    second = await pending(environment, price=11.60)
    async with environment.maker() as db, db.begin():
        row = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == first))
        risk = json.loads(row.risk_json)
        risk["paper_after_hours_intent"]["accepted_at"] = (AT + timedelta(days=1)).isoformat()
        row.risk_json = json.dumps(risk)
    payload = frame()
    payload["official_close"]["price"] = 11.58
    payload["initial"]["orders"][0]["limit_price"] = 11.58
    before = await snapshot(environment)
    result = await invoke(environment, second, payload)
    assert result["reason"] == "earlier_local_full_fill_intent" and result["fills"] == []
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_last_synchronous_source_replay_cannot_borrow_pre_close_clock(
        environment, audited_fixture, monkeypatch):
    from app.trading import paper_after_hours_allocation as allocator
    identifier = await pending(environment)
    before = await snapshot(environment)
    original, fired = allocator._verify, []
    def slow_replay(*args, **kwargs):
        value = original(*args, **kwargs)
        scope = auth._SCOPE.get()
        if scope is not None and scope.resource_consumption_used:
            orders = [obj for obj in scope.db.sync_session.identity_map.values()
                      if isinstance(obj, TradeOrder) and obj.status == "filled"]
            if orders:
                fired.append(True)
                environment.clock[0] = AT.replace(minute=30)
        return value
    monkeypatch.setattr(allocator, "_verify", slow_replay)
    with pytest.raises(HTTPException) as exc:
        await invoke(environment, identifier)
    assert fired and exc.value.status_code == 409
    assert await snapshot(environment) == before
