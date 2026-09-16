"""Transaction ownership/lock-wait tests; no business orders or network calls."""
import asyncio
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1 import paper
from app.models.trading import TradeOrder
from app.trading.paper_authorization import (
    _paper_order_transaction, paper_transaction_active, finish_account_write,
)
from test_quote_round_execution import quote_execution_env


class ObservedLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.waiting = asyncio.Event()

    async def __aenter__(self):
        self.waiting.set()
        await self.lock.acquire()

    async def __aexit__(self, *_):
        self.lock.release()


async def seed_order(factory):
    async with factory() as db:
        row = TradeOrder(order_id="unit-fixture", broker="paper", account_id="default",
                         code="600001", side="buy", order_type="limit", price=10, quantity=300,
                         status="submitted", risk_json='{"original":true}',
                         trade_date=datetime(2026, 9, 14).date())
        db.add(row)
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("status", "canceled"), ("filled_quantity", 100),
    ("last_fill_round_id", "already-filled"), ("quantity", 200),
    ("price", 9.5), ("code", "600002"), ("account_id", "promotion"),
    ("strategy_version", "different-version"), ("risk_json", '{"new":true}'),
    ("order_id", "changed-key"), ("order_type", "market"), ("avg_fill_price", 10.1),
    ("reason", "changed-reason"), ("risk_level", "block"),
    ("decision_round_id", "changed-decision"), ("decision_at", datetime(2026, 9, 14, 11)),
    ("as_of_at", datetime(2026, 9, 14, 10)), ("trade_date", datetime(2026, 9, 15).date()),
    ("config_version", "changed-config"), ("code_version", "changed-code"),
])
async def test_lock_wait_cannot_reuse_changed_order(quote_execution_env, monkeypatch, field, value):
    factory = quote_execution_env
    await seed_order(factory)
    lock = ObservedLock()
    monkeypatch.setattr(paper, "_TRADE_LOCK", lock)
    await lock.lock.acquire()
    body_called = []

    async def waiter():
        async with factory() as db:
            order = await db.scalar(select(TradeOrder))
            async with _paper_order_transaction(db, order=order):
                body_called.append(True)

    task = asyncio.create_task(waiter())
    try:
        await asyncio.wait_for(lock.waiting.wait(), 5)
        # Checkpoint must release SQLite writer BEFORE waiting for the Python lock.
        async with factory() as db:
            order = await db.scalar(select(TradeOrder))
            setattr(order, field, value)
            await asyncio.wait_for(db.commit(), 3)
        lock.lock.release()
        with pytest.raises(HTTPException, match="等待成交锁期间") as exc:
            await asyncio.wait_for(task, 5)
        assert exc.value.status_code == 409
    finally:
        if not task.done():
            task.cancel()
        if lock.lock.locked():
            lock.lock.release()
        await asyncio.gather(task, return_exceptions=True)
    assert body_called == []
    async with factory() as db:
        order = await db.scalar(select(TradeOrder))
        assert getattr(order, field) == value


@pytest.mark.asyncio
async def test_transaction_cannot_be_nested_or_inherited_by_other_task_or_session(quote_execution_env):
    factory = quote_execution_env
    async with factory() as db, factory() as other:
        async with _paper_order_transaction(db):
            assert paper_transaction_active(db)
            with pytest.raises(HTTPException):
                paper_transaction_active(other)
            with pytest.raises(HTTPException):
                async with _paper_order_transaction(db):
                    pytest.fail("nested owner must fail before yield")
            async def child():
                with pytest.raises(HTTPException):
                    await finish_account_write(db)
            await asyncio.create_task(child())
        assert not paper_transaction_active(db)


@pytest.mark.asyncio
async def test_flush_helper_is_not_an_independent_commit(quote_execution_env):
    factory = quote_execution_env
    async with factory() as db:
        with pytest.raises(RuntimeError, match="rollback"):
            async with _paper_order_transaction(db):
                db.add(TradeOrder(order_id="not-durable", broker="paper", account_id="default",
                    code="600001", side="buy", order_type="limit", price=10, quantity=100, status="pending"))
                await finish_account_write(db)
                assert await db.scalar(select(func.count(TradeOrder.id))) == 1
                async with factory() as reader:
                    assert await reader.scalar(select(func.count(TradeOrder.id))) == 0
                raise RuntimeError("rollback")
    async with factory() as db:
        assert await db.scalar(select(func.count(TradeOrder.id))) == 0
