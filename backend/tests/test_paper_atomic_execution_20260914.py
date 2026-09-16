"""Fault injection against real SQLite paper books; never production risk or APIs.

Risk/entry-signal mocks isolate transaction mechanics only. Quote validation,
book/accounting, ORM receipts, SQLite commits and fresh-session reads remain real.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import paper
from app.core.trade_calendar import TradeCalendar
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog, PaperSaleAccounting
from app.models.trading import TradeFill, TradeOrder
from app.trading import service, paper_authorization as authorization
from app.trading.broker import PaperBrokerAdapter, BrokerOrderRequest
from paper_immediate_fixture import seed_immediate_quote
from test_quote_round_execution import quote_execution_env
from test_paper_orphan_fill_guard import AT, seed, place_deferred, reconcile, identity, quote

PATHS = ["immediate-buy", "immediate-sell", "deferred-buy", "deferred-sell", "queue-buy"]


@pytest.fixture(autouse=True)
def isolated_transaction_policy_and_calendar(monkeypatch):
    # NOT a risk acceptance test: real production risk is untouched.
    monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value={
        "final_level": "pass", "block_reasons": [], "warnings": []}))
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda order: False)
    real_paper_now = paper._paper_now
    def fixture_now():
        # Keep production fill/quote context precedence; freeze only wall-clock fallback.
        if paper._PAPER_FILL_CONTEXT.get() or paper._QUOTE_ROUND_CONTEXT.get():
            return real_paper_now()
        return AT
    monkeypatch.setattr(paper, "_paper_now", fixture_now)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        AT.date()-timedelta(days=i): (AT.date()-timedelta(days=i)).weekday()<5
        for i in range(40)})
    async def fixture_loaded(self, year):
        assert self is paper.trade_calendar and year == 2026
        assert self._cache[AT.date()] is True
    monkeypatch.setattr(TradeCalendar, "_ensure_loaded", fixture_loaded)
    network = AsyncMock(side_effect=AssertionError("atomic fixture forbids network calendar"))
    monkeypatch.setattr(TradeCalendar, "_sync_from_source", network)
    yield
    network.assert_not_awaited()


async def snapshot(factory):
    """Read persisted economics, not identity-map state; preflight NAV is excluded."""
    async with factory() as db:
        result = {}
        for model in (PaperTradeLog, PaperSaleAccounting, TradeFill):
            rows = (await db.execute(select(*model.__table__.columns).order_by(model.id))).all()
            result[model.__tablename__] = [tuple(row) for row in rows]
        result["cash"] = list((await db.execute(select(
            PaperAccount.id, PaperAccount.current_capital).order_by(PaperAccount.id))).all())
        result["positions"] = list((await db.execute(select(
            PaperPosition.id, PaperPosition.code, PaperPosition.buy_price,
            PaperPosition.buy_amount, PaperPosition.buy_time, PaperPosition.is_closed,
            PaperPosition.strategy_version).order_by(PaperPosition.id))).all())
        return result


async def cumulative(factory):
    async with factory() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        return (order.filled_quantity, order.avg_fill_price, order.last_fill_round_id)


async def prepare(factory, monkeypatch, path):
    mode, side = path.split("-")
    account = paper.PAPER_ACCOUNT_DEFAULT
    if mode == "immediate":
        async with factory() as db:
            await seed(db, account, side)
            round_id = await seed_immediate_quote(db, monkeypatch, code="600001",
                at=AT, price=10, side=side)
            account_row = await paper._get_or_create_account(db, account)
            await paper._refresh_account(db, account_row)
            await db.commit()
        command = service.SubmitOrderCommand(code="600001", side=side,
            price=10, quantity=100, account_id=account, decision_at=AT,
            decision_round_id=round_id, as_of_at=AT-timedelta(seconds=3),
            signal_id="atomic-immediate-"+side, idempotency_key="atomic-immediate-"+side)
        async def invoke(round_number=1):
            async with factory() as db:
                return await service.submit_order(db, command)
        return invoke
    await place_deferred(factory, account, side, queued=mode=="queue")
    async with factory() as db:
        row = await paper._get_or_create_account(db, account)
        await paper._refresh_account(db, row)
        await db.commit()
    async def invoke(round_number=1):
        return await reconcile(factory, account, "atomic-fill-"+str(round_number),
            AT+timedelta(seconds=30*round_number), queued=mode=="queue")
    return invoke


def injected_error(kind):
    if kind == "cancel":
        return asyncio.CancelledError("atomic injected cancellation")
    if kind == "http":
        return HTTPException(409, "atomic injected HTTP failure")
    return ValueError("atomic injected failure")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("kind", ["cancel", "value", "http"])
async def test_broker_return_then_failure_leaves_no_new_economics(
    quote_execution_env, monkeypatch, path, kind,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, path)
    before = await snapshot(factory)
    real = PaperBrokerAdapter.place_order
    booked = []
    async def fail_after_book(self, db, req):
        receipt = await real(self, db, req)
        assert receipt.fills, "fault must be after a REAL successful book"
        booked.append(receipt.fills[0].broker_trade_id)
        raise injected_error(kind)
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", fail_after_book)
    if kind == "cancel" or path == "queue-buy":
        with pytest.raises(type(injected_error(kind))):
            await invoke()
    else:
        result = await invoke()
        result = result[0] if isinstance(result, list) else result
        assert result["order"]["status"] == "rejected"
        assert result["fills"] == []
    assert len(booked) == 1
    assert await snapshot(factory) == before
    assert (await cumulative(factory))[0] == 0


@asynccontextmanager
async def persistence_fault(monkeypatch, factory, kind):
    fired = []
    if kind == "sql":
        async with factory() as db:
            await db.execute(text("CREATE TRIGGER atomic_reject_fill BEFORE INSERT ON trade_fill "
                "BEGIN SELECT RAISE(ABORT, 'atomic receipt insert rejected'); END"))
            await db.commit()
        try:
            yield fired
        finally:
            async with factory() as db:
                await db.execute(text("DROP TRIGGER atomic_reject_fill"))
                await db.commit()
        return
    real_add, real_flush, real_commit = AsyncSession.add, AsyncSession.flush, AsyncSession.commit
    def add(db, obj, *args, **kwargs):
        if kind == "add" and isinstance(obj, TradeFill):
            fired.append("add")
            raise ValueError("atomic receipt add")
        return real_add(db, obj, *args, **kwargs)
    async def flush(db, *args, **kwargs):
        if kind == "flush" and any(isinstance(row, TradeFill) for row in db.new):
            fired.append("flush")
            raise ValueError("atomic receipt flush")
        return await real_flush(db, *args, **kwargs)
    async def commit(db):
        active = authorization.paper_transaction_active(db)
        if active:
            # Ensure explicit AsyncSession.flush injection also catches the
            # final commit path (SQLAlchemy otherwise uses sync_session.flush).
            if kind == "flush":
                await db.flush()
            if kind in {"commit", "ack"}:
                fired.append(kind)
                if kind == "ack":
                    await real_commit(db)
                raise ValueError("atomic final commit " + kind)
        return await real_commit(db)
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "add", add)
        patch.setattr(AsyncSession, "flush", flush)
        patch.setattr(AsyncSession, "commit", commit)
        yield fired
    assert fired, "injection was not reached"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("kind", ["add", "flush", "sql", "commit"])
async def test_receipt_or_final_commit_failure_rolls_back_all(
    quote_execution_env, monkeypatch, path, kind,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, path)
    before = await snapshot(factory)
    async with persistence_fault(monkeypatch, factory, kind):
        with pytest.raises(IntegrityError if kind == "sql" else ValueError,
                           match="atomic"):
            await invoke()
    assert await snapshot(factory) == before
    assert (await cumulative(factory))[0] == 0
    async with factory() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.status != "rejected", "receipt/commit failures must not rewrite rejection"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("kind", ["add", "flush", "sql", "commit"])
async def test_failed_second_partial_preserves_exact_first_fill(
    quote_execution_env, monkeypatch, side, kind,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, "deferred-"+side)
    first = await invoke(1)
    assert first[0]["event"] == "partial"
    assert first[0]["order"]["filled_quantity"] == 100
    before, order_before = await snapshot(factory), await cumulative(factory)
    async with persistence_fault(monkeypatch, factory, kind):
        with pytest.raises(IntegrityError if kind == "sql" else ValueError, match="atomic"):
            await invoke(2)
    assert await snapshot(factory) == before
    assert await cumulative(factory) == order_before
    async with factory() as db:
        assert (await db.scalars(select(TradeOrder))).one().status == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_successful_commit_lost_ack_retains_complete_fill_and_replay(
    quote_execution_env, monkeypatch, path,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, path)
    before = await snapshot(factory)
    async with persistence_fault(monkeypatch, factory, "ack"):
        with pytest.raises(ValueError, match="atomic final commit ack"):
            await invoke()
    durable = await snapshot(factory)
    assert len(durable["paper_trade_log"]) == len(before["paper_trade_log"])+1
    assert len(durable["trade_fill"]) == 1
    assert durable["cash"] != before["cash"]
    async with factory() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        trade = await db.get(PaperTradeLog, int(fill.broker_trade_id))
        assert (fill.quantity, fill.price, fill.commission, fill.tax, fill.filled_at) == (
            trade.amount, trade.price, trade.commission, trade.tax, trade.trade_time)
        original_fill = fill.fill_id
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.status == ("partial" if path.startswith("deferred") else "filled")
        assert order.filled_quantity == fill.quantity
        if path.endswith("sell"):
            assert len(durable["paper_sale_accounting"]) == 1
        replay = await service.submit_order(db, service.SubmitOrderCommand(
            code=order.code, side=order.side, price=order.price, quantity=order.quantity,
            account_id=order.account_id, idempotency_key=order.idempotency_key))
        assert replay["idempotent_replay"]
        assert [item["fill_id"] for item in replay["fills"]] == [original_fill]
    if not path.startswith("immediate"):
        assert await invoke(1) == []  # same round/key must not trade twice
    assert await snapshot(factory) == durable


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_lock_covers_receipt_add_and_final_commit(
    quote_execution_env, monkeypatch, path,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, path)
    real_add, real_commit = AsyncSession.add, AsyncSession.commit
    stages = []
    contender_entered = asyncio.Event()
    async def contender():
        async with paper._TRADE_LOCK:
            contender_entered.set()
    def add(db, obj, *args, **kwargs):
        if isinstance(obj, TradeFill):
            assert authorization.paper_transaction_active(db)
            assert paper._TRADE_LOCK.locked()
            stages.append("receipt")
        return real_add(db, obj, *args, **kwargs)
    async def commit(db):
        if authorization.paper_transaction_active(db):
            assert stages == ["receipt"]
            assert paper._TRADE_LOCK.locked()
            task = asyncio.create_task(contender())
            try:
                await asyncio.sleep(0)
                assert not contender_entered.is_set()
                await real_commit(db)
                assert paper._TRADE_LOCK.locked()
                assert not contender_entered.is_set()
                stages.append("commit")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        else:
            await real_commit(db)
    monkeypatch.setattr(AsyncSession, "add", add)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    await invoke()
    assert stages == ["receipt", "commit"]
    assert not paper._TRADE_LOCK.locked()


@pytest.mark.asyncio
async def test_transaction_scope_rejects_inherited_task_and_foreign_db(quote_execution_env):
    async with quote_execution_env() as db, quote_execution_env() as other:
        assert not authorization.paper_transaction_active(db)
        async with authorization._paper_order_transaction(db):
            assert authorization.paper_transaction_active(db)
            with pytest.raises(HTTPException) as foreign:
                authorization.paper_transaction_active(other)
            assert foreign.value.status_code == 403
            async def child():
                with pytest.raises(HTTPException) as inherited:
                    authorization.paper_transaction_active(db)
                assert inherited.value.status_code == 403
            await asyncio.create_task(child())
        assert not authorization.paper_transaction_active(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_private_book_requires_transaction_even_with_authorized_dispatch(
    quote_execution_env, side,
):
    async with quote_execution_env() as db:
        await seed(db, "default", side)
        request = BrokerOrderRequest(order_id="atomic-no-unit", code="600001",
            side=side, price=10, quantity=100)
        before = await snapshot(quote_execution_env)
        with authorization._paper_execution_scope(db, request):
            with pytest.raises(HTTPException) as error:
                await PaperBrokerAdapter().place_order(db, request)
        assert error.value.status_code == 403
        await db.rollback()
    assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_deferred_batch_continues_after_first_book_rollback(
    quote_execution_env, monkeypatch, side,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, "deferred-"+side)
    strategy, source, signal = identity("default", side)
    async with factory() as db:
        token = paper._QUOTE_ROUND_CONTEXT.set(quote("decision", AT))
        try:
            second = await service.submit_order(db, service.SubmitOrderCommand(
                code="600001", side=side, price=10, quantity=300, account_id="default",
                strategy_id=strategy, source=source, signal_id=signal+"-second",
                idempotency_key="atomic-batch-second-"+side, decision_at=AT,
                defer_until_next_round=True, deferred_metadata={"block_warn": side=="buy"}))
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert second["order"]["status"] == "submitted"
    before = await snapshot(factory)
    real = PaperBrokerAdapter.place_order
    calls = []
    async def first_book_fails(self, db, req):
        receipt = await real(self, db, req)
        assert receipt.fills
        calls.append(req.order_id)
        if len(calls) == 1:
            raise ValueError("atomic first batch book failed")
        return receipt
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", first_book_fails)
    result = await invoke()
    assert len(calls) == 2 and calls[0] != calls[1]
    assert [item["event"] for item in result] == ["rejected", "partial"]
    assert result[0]["fills"] == []
    assert result[1]["order"]["order_id"] == second["order"]["order_id"]
    async with factory() as db:
        orders = list((await db.scalars(select(TradeOrder).order_by(TradeOrder.id))).all())
        assert [(o.status, o.filled_quantity) for o in orders] == [
            ("rejected", 0), ("partial", 100)]
        fill = (await db.scalars(select(TradeFill))).one()
        assert fill.order_id == orders[1].order_id and fill.quantity == 100
        trade = await db.get(PaperTradeLog, int(fill.broker_trade_id))
        assert trade.signal_id == calls[1]
        account = (await db.scalars(select(PaperAccount))).one()
        delta = fill.price*fill.quantity*(1 if side=="sell" else -1)-fill.commission-fill.tax
        assert account.current_capital == pytest.approx(before["cash"][0][1]+delta)
        position = (await db.scalars(select(PaperPosition))).one()
        assert position.buy_amount == (100 if side=="buy" else 400)
    after = await snapshot(factory)
    assert len(after["paper_trade_log"]) == len(before["paper_trade_log"])+1
    assert len(after["paper_sale_accounting"]) == (1 if side=="sell" else 0)


class WaitingTransactionLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.waiting = asyncio.Event()

    async def __aenter__(self):
        self.waiting.set()
        await self.lock.acquire()
        return self

    async def __aexit__(self, *args):
        self.lock.release()

    def locked(self):
        return self.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("field,value", [
    ("status", "canceled"), ("filled_quantity", 100),
    ("last_fill_round_id", "concurrent-fill"), ("quantity", 200),
    ("price", 9.88), ("side", "foreign-side"), ("code", "600002"),
    ("broker", "foreign-broker"), ("account_id", "foreign-account"),
    ("strategy_id", "foreign-strategy"), ("strategy_version", "foreign-version"),
    ("signal_id", "foreign-signal"), ("source", "foreign-source"),
    ("risk_json", '{"concurrent_evidence":true}'), ("idempotency_key", "foreign-key"),
])
async def test_lock_wait_revalidates_order_and_never_overwrites_concurrent_change(
    quote_execution_env, monkeypatch, path, field, value,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, path)
    before = await snapshot(factory)
    lock = WaitingTransactionLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    async def forbidden_dispatch(*args, **kwargs):
        pytest.fail("changed order reached broker with stale matching evidence")
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", forbidden_dispatch)
    await lock.lock.acquire()
    task = asyncio.create_task(invoke())
    try:
        # Reached only after checkpoint; independent SQLite writer must now work.
        await asyncio.wait_for(lock.waiting.wait(), 5)
        async with factory() as db:
            order = (await db.scalars(select(TradeOrder))).one()
            assert getattr(order, field) != value
            setattr(order, field, value)
            await db.commit()
            changed = tuple((await db.execute(select(*TradeOrder.__table__.columns))).one())
        lock.lock.release()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(task, 5)
        assert error.value.status_code == 409
        assert "委托已变化" in str(error.value.detail)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if lock.lock.locked():
            lock.lock.release()
    async with factory() as db:
        persisted = tuple((await db.execute(select(*TradeOrder.__table__.columns))).one())
        assert persisted == changed  # no rejection rewrite or stale commit
    assert await snapshot(factory) == before
