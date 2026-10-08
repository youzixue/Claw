"""Real private service/risk/broker/book partial path, temporary DB and audited feed fixture only."""
import json
from datetime import timedelta

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
from test_paper_after_hours_allocation_20261002 import audited_fixture
from test_paper_after_hours_atomic_20261002 import frame, pending, snapshot
from test_paper_after_hours_partial_allocation_20261002 import append_offer


async def invoke(env, identifier, payload):
    async with env.maker() as db:
        return await service._execute_paper_after_hours_partial_order(db, identifier, feed=payload)


def next_frame(payload, *, shares=100, second=2, name="offer.2"):
    result = append_offer(payload, identifier=name, shares=shares,
        at=AT+timedelta(seconds=second), frame="frame."+str(second))
    result["events"][-1]["order"].update(limit_price=11.57,
        side=payload["initial"]["orders"][0]["side"])
    return result


@pytest.mark.asyncio
async def test_real_three_fragments_one_original_keep_signal_fees_and_durable_consumption(environment, audited_fixture):
    identifier = await pending(environment, quantity=300, signal_id="original.signal")
    payload = frame(quantity=150)
    first = await invoke(environment, identifier, payload)
    assert first["order"]["status"] == "partial", first
    assert first["order"]["filled_quantity"] == first["fills"][0]["quantity"] == 100
    payload = next_frame(payload)
    environment.clock[0] = AT + timedelta(seconds=3)
    second = await invoke(environment, identifier, payload)
    assert second["order"]["status"] == "partial", second
    assert second["order"]["filled_quantity"] == 200
    payload = next_frame(payload, shares=50, second=4, name="offer.3")
    environment.clock[0] = AT + timedelta(seconds=5)
    third = await invoke(environment, identifier, payload)
    assert third["order"]["status"] == "filled" and third["order"]["filled_quantity"] == 300
    environment.forbidden.assert_not_awaited()
    async with environment.maker() as db:
        trades = list((await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all())
        fills = list((await db.scalars(select(TradeFill).order_by(TradeFill.id))).all())
        assert len(trades) == len(fills) == 3
        assert {t.signal_id for t in trades} == {"original.signal"}
        assert {f.order_id for f in fills} == {identifier}
        assert len({f.fill_id for f in fills}) == 3
        assert all(len(f.fill_id) == 40 and f.fill_id.startswith("fill-afp-") for f in fills)
        assert [t.commission for t in trades] == [paper._commission(1157)] * 3
        assert all(t.price == 11.57 and t.amount == 100 for t in trades)
        assert await db.scalar(select(PaperPosition.buy_amount)) == 300
        assert await db.scalar(select(PaperAccount.current_capital)) == 50000-3*(1157+paper._commission(1157))
        assert await db.scalar(select(resources.State.revision)) == 3
        roots = await resources._verified_partial_receipt_bindings(db,
            account_numeric_id=trades[0].account_id, code="000001", trade_date=AT.date(),
            cutoff=environment.clock[0])
        assert set(roots) == {f.fill_id for f in fills}
    before = await snapshot(environment)
    replay = await invoke(environment, identifier, {})
    assert replay["idempotent_replay"] and await snapshot(environment) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sse_fixed_price", "szse_fixed_price", "ths_after_hours"])
async def test_formal_sources_still_cannot_dispatch_partial(environment, source):
    from app.trading import paper_after_hours_allocation as allocation
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    payload.update(source=source, source_version=allocation._PROVIDERS[source].version,
        capability="order_level", partial_fill_allowed=True, execution_authorized=True)
    before = await snapshot(environment)
    result = await invoke(environment, identifier, payload)
    assert result["reason"] == "provider_aggregate_only" and result["fills"] == []
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_smaller_later_order_cannot_jump_partially_filled_head(environment, audited_fixture):
    identifier = await pending(environment, quantity=300)
    environment.clock[0] = AT
    later = await pending(environment, quantity=100)
    payload = frame(quantity=150)
    assert (await invoke(environment, identifier, payload))["order"]["filled_quantity"] == 100
    before = await snapshot(environment)
    payload = next_frame(payload)
    environment.clock[0] = AT+timedelta(seconds=3)
    blocked = await invoke(environment, later, payload)
    assert blocked["reason"] == "earlier_local_partial_intent" and blocked["fills"] == []
    assert await snapshot(environment) == before
    assert (await invoke(environment, identifier, payload))["order"]["filled_quantity"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["broker_after_book", "resource_after_cas", "order_cas"])
async def test_second_fragment_failure_rolls_back_only_new_economics(environment, audited_fixture, monkeypatch, fault):
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    await invoke(environment, identifier, payload)
    before = await snapshot(environment)
    fired = []
    if fault == "broker_after_book":
        original = PaperBrokerAdapter.place_order
        async def fail(self, db, req):
            answer = await original(self, db, req)
            assert answer.fills
            fired.append(True)
            raise ValueError("new book ack failure")
        monkeypatch.setattr(PaperBrokerAdapter, "place_order", fail)
    else:
        original = resources._persist_fill_resources
        async def fail(*args, **kwargs):
            answer = await original(*args, **kwargs)
            assert answer["revision"] == 2
            fired.append(True)
            if fault == "order_cas":
                await args[0].execute(text("UPDATE trade_order SET status='canceled'"))
                return answer
            raise ValueError("new consumption failure")
        monkeypatch.setattr(resources, "_persist_fill_resources", fail)
    environment.clock[0] = AT+timedelta(seconds=3)
    with pytest.raises((ValueError, HTTPException)) as exc:
        await invoke(environment, identifier, next_frame(payload))
    assert fired and auth.paper_execution_requires_reconciliation(exc.value)
    assert await snapshot(environment) == before
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        assert row.status == "partial" and row.filled_quantity == 100


@pytest.mark.asyncio
async def test_risk_block_does_not_destroy_existing_partial_status(environment, audited_fixture, monkeypatch):
    from app.risk.engine import risk_engine
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    await invoke(environment, identifier, payload)
    before = await snapshot(environment)
    monkeypatch.setattr(risk_engine, "_rules", [])
    environment.clock[0] = AT+timedelta(seconds=3)
    with pytest.raises(HTTPException):
        await invoke(environment, identifier, next_frame(payload))
    assert await snapshot(environment) == before
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        assert row.status == "partial" and row.filled_quantity == 100


@pytest.mark.asyncio
async def test_partial_buy_today_remains_T_plus_1(environment, audited_fixture):
    identifier = await pending(environment, quantity=300)
    await invoke(environment, identifier, frame(quantity=150))
    before = await snapshot(environment)
    result = await submit(environment, side="sell", price=11.57)
    assert result["order"]["status"] == "rejected"
    assert result["risk"]["paper_after_hours_intent"]["reason"] == "T_plus_1_or_insufficient_sellable_quantity"
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_real_three_sales_reuse_entry_fee_allocation_and_tax_without_replaying_original_signal(
        environment, audited_fixture):
    yesterday = AT-timedelta(days=1)
    async with environment.maker() as db:
        account = await db.scalar(select(PaperAccount))
        version = paper._strategy_version("default")
        db.add(PaperPosition(account_id=account.id, code="000001", buy_price=11.5, buy_amount=300,
            buy_time=yesterday, strategy_version=version, current_price=11.57, is_closed=False))
        db.add(PaperTradeLog(account_id=account.id, code="000001", trade_type="buy", price=11.5, amount=300,
            trade_time=yesterday, commission=paper._commission(3450), tax=0,
            strategy_version=version, signal_id="overnight.300"))
        await db.flush()
        await paper._refresh_account(db, account)
    identifier = await pending(environment, side="sell", quantity=300, price=11.57, signal_id="original.sale")
    payload = frame(side="buy", quantity=150)
    first = await invoke(environment, identifier, payload)
    assert first["order"]["status"] == "partial"
    payload = next_frame(payload)
    environment.clock[0] = AT+timedelta(seconds=3)
    second = await invoke(environment, identifier, payload)
    payload = next_frame(payload, shares=50, second=4, name="offer.3")
    environment.clock[0] = AT+timedelta(seconds=5)
    third = await invoke(environment, identifier, payload)
    assert [first["order"]["filled_quantity"], second["order"]["filled_quantity"],
            third["order"]["filled_quantity"]] == [100, 200, 300]
    async with environment.maker() as db:
        sales = list((await db.scalars(select(PaperTradeLog).where(PaperTradeLog.trade_type=="sell"))).all())
        accounting = list((await db.scalars(select(PaperSaleAccounting))).all())
        assert len(sales) == len(accounting) == 3
        assert {s.signal_id for s in sales} == {"original.sale"}
        assert all(s.tax == paper._stamp_tax(1157) and s.commission == paper._commission(1157) for s in sales)
        assert round(sum(s.realized_pnl for s in sales), 2) == round(
            21-paper._commission(3450)-3*paper._commission(1157)-3*paper._stamp_tax(1157), 2)
        position = await db.scalar(select(PaperPosition))
        assert position.is_closed and position.buy_amount == 0
        roots = await resources._verified_partial_receipt_bindings(db, account_numeric_id=sales[0].account_id,
            code="000001", trade_date=AT.date(), cutoff=environment.clock[0])
        assert len(roots) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["risk_dispatch", "resource_cas", "cas_terminal", "cas_fee"])
async def test_second_fragment_cross_stage_clock_or_fee_drift_rolls_back(
        environment, audited_fixture, monkeypatch, phase):
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    await invoke(environment, identifier, payload)
    before = await snapshot(environment)
    environment.clock[0] = AT+timedelta(seconds=3)
    if phase == "risk_dispatch":
        original = service._locked_paper_risk_evidence
        async def drift(*args, **kwargs):
            environment.clock[0] = AT+timedelta(seconds=4)
            value = await original(*args, **kwargs)
            assert value["status"] == "validated"
            environment.clock[0] = AT+timedelta(seconds=3)
            return value
        monkeypatch.setattr(service, "_locked_paper_risk_evidence", drift)
    elif phase == "resource_cas":
        original = resources._persist_fill_resources
        async def drift(*args, **kwargs):
            environment.clock[0] = AT+timedelta(seconds=4)
            value = await original(*args, **kwargs)
            assert value["revision"] == 2
            environment.clock[0] = AT+timedelta(seconds=3)
            return value
        monkeypatch.setattr(resources, "_persist_fill_resources", drift)
    else:
        original = AsyncSession.refresh
        async def drift(db, instance, *args, **kwargs):
            value = await original(db, instance, *args, **kwargs)
            if (auth.paper_transaction_active(db) and isinstance(instance, TradeOrder)
                    and instance.filled_quantity == 200):
                if phase == "cas_terminal":
                    environment.clock[0] = AT+timedelta(seconds=2, milliseconds=500)
                else:
                    monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 6.0)
            return value
        monkeypatch.setattr(AsyncSession, "refresh", drift)
    with pytest.raises(HTTPException):
        await invoke(environment, identifier, next_frame(payload))
    assert await snapshot(environment) == before
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        assert row.status == "partial" and row.filled_quantity == 100


@pytest.mark.asyncio
async def test_real_book_await_expiry_preserves_prior_fragment(environment, audited_fixture, monkeypatch):
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    await invoke(environment, identifier, payload)
    before = await snapshot(environment)
    original = paper._stock_info
    async def cross(*args, **kwargs):
        result = await original(*args, **kwargs)
        scope = auth._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            environment.clock[0] = AT.replace(minute=30)
        return result
    monkeypatch.setattr(paper, "_stock_info", cross)
    environment.clock[0] = AT+timedelta(seconds=3)
    with pytest.raises(HTTPException):
        await invoke(environment, identifier, next_frame(payload))
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_lost_ack_never_undoes_durable_partial_and_same_frame_cannot_fill_again(
        environment, audited_fixture, monkeypatch):
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    original = AsyncSession.commit
    async def lost(db):
        owned = auth.paper_transaction_active(db)
        await original(db)
        if owned:
            raise OSError("durable partial COMMIT acknowledgement lost")
    monkeypatch.setattr(AsyncSession, "commit", lost)
    with pytest.raises(OSError) as exc:
        await invoke(environment, identifier, payload)
    assert auth.paper_execution_requires_reconciliation(exc.value)
    monkeypatch.setattr(AsyncSession, "commit", original)
    before = await snapshot(environment)
    assert len(before["trade_fill"]) == len(before["paper_trade_log"]) == 1
    replay = await invoke(environment, identifier, payload)
    assert replay["fills"] == [] and await snapshot(environment) == before
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        assert row.status == "partial" and row.filled_quantity == 100
    # This private call means next-fragment attempt, not retry-last-fragment.
    # A later valid feed may propose the next coordinate; no public retry API exists.


@pytest.mark.asyncio
async def test_next_fragment_clock_cannot_precede_previous_service_cas(environment, audited_fixture, monkeypatch):
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    original = resources._persist_fill_resources
    async def late_final(*args, **kwargs):
        answer = await original(*args, **kwargs)
        environment.clock[0] = AT+timedelta(seconds=5)
        return {**answer, "terminal_checked_at": environment.clock[0].isoformat()}
    monkeypatch.setattr(resources, "_persist_fill_resources", late_final)
    first = await invoke(environment, identifier, payload)
    assert first["order"]["filled_quantity"] == 100
    monkeypatch.setattr(resources, "_persist_fill_resources", original)
    before = await snapshot(environment)
    environment.clock[0] = AT+timedelta(seconds=3)
    with pytest.raises(HTTPException, match="时钟"):
        await invoke(environment, identifier, next_frame(payload))
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_typed_timing_conversion_cannot_borrow_pre_expiry_mutation_clock(
        environment, audited_fixture, monkeypatch):
    identifier = await pending(environment, quantity=300)
    before = await snapshot(environment)
    real_timing = resources._partial_resource_timing
    real_book = paper._book_paper_buy
    book_completed = []
    def slow_conversion(*args, **kwargs):
        answer = real_timing(*args, **kwargs)
        scope = auth._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            environment.clock[0] = AT.replace(minute=30)
        return answer
    async def observed_book(*args, **kwargs):
        answer = await real_book(*args, **kwargs)
        book_completed.append(True)
        return answer
    monkeypatch.setattr(resources, "_partial_resource_timing", slow_conversion)
    monkeypatch.setattr(paper, "_book_paper_buy", observed_book)
    with pytest.raises(HTTPException):
        await invoke(environment, identifier, frame(quantity=150))
    assert not book_completed  # Reject at the ledger guard, not after booked mutation.
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_default_full_entry_cannot_claim_partial_receipts(environment, audited_fixture):
    identifier = await pending(environment, quantity=300)
    payload = frame(quantity=150)
    await invoke(environment, identifier, payload)
    before = await snapshot(environment)
    async with environment.maker() as db:
        with pytest.raises(HTTPException):
            await service._execute_paper_after_hours_order(db, identifier, feed=next_frame(payload))
    assert await snapshot(environment) == before


@pytest.mark.asyncio
async def test_partial_entry_cannot_mix_existing_full_pool(environment, audited_fixture):
    first = await pending(environment)
    async with environment.maker() as db:
        assert (await service._execute_paper_after_hours_order(db, first, feed=frame()))["order"]["status"]=="filled"
    second = await pending(environment, quantity=300)
    before = await snapshot(environment)
    payload = next_frame(frame())
    environment.clock[0] = AT+timedelta(seconds=3)
    with pytest.raises(ValueError):
        await invoke(environment, second, payload)
    assert await snapshot(environment) == before
