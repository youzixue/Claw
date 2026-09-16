"""Real SQLite rejection races after atomic fill rollback; no production access.

Reuse explicitly mocked risk/calendar fixture, not a production strategy test.
Independent sessions write at the conditional UPDATE, not at a prior read.
"""
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from app.api.v1 import paper
from app.models.trading import TradeOrder
from app.models.paper import PaperAccount
from app.trading import service, paper_authorization as authorization
from app.trading.broker import PaperBrokerAdapter
from test_quote_round_execution import quote_execution_env
from test_paper_atomic_execution_20260914 import (
    isolated_transaction_policy_and_calendar, prepare, snapshot,
)

PATHS = ["immediate-buy", "immediate-sell", "deferred-buy", "deferred-sell"]


@pytest.fixture(autouse=True)
def local_event_loop_lock(monkeypatch):
    # pytest uses a new event loop per test; do not inherit a contended old loop.
    monkeypatch.setattr(paper, "_TRADE_LOCK", asyncio.Lock())


async def order_snapshot(factory):
    async with factory() as db:
        return [tuple(row) for row in (await db.execute(
            select(*TradeOrder.__table__.columns).order_by(TradeOrder.id))).all()]


def reject_after_real_book(monkeypatch):
    real = PaperBrokerAdapter.place_order
    hits = []
    async def fail(self, db, req):
        receipt = await real(self, db, req)
        assert receipt.fills
        hits.append(receipt.fills[0].broker_trade_id)
        raise ValueError("known broker rejection after rolled back book")
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", fail)
    return hits


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("mutation", [
    {"status": "canceled"},
    {"quantity": 700},
    {"signal_id": "new-foreign-signal"},
    {"risk_json": '{"foreign_evidence":true}'},
    {"strategy_version": "foreign-version"},
])
async def test_conditional_update_never_clobbers_independent_writer(
    quote_execution_env, monkeypatch, path, mutation,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, path)
    before = await snapshot(factory)
    booked = reject_after_real_book(monkeypatch)
    real_execute = AsyncSession.execute
    foreign = []
    async def execute(db, statement, *args, **kwargs):
        if (isinstance(statement, Update) and statement.table.name == "trade_order"
                and paper._TRADE_LOCK.locked() and not foreign):
            # Unit has rolled back. Race immediately before actual CAS SQL,
            # with an independent SQLite writer that does not use Python lock.
            foreign.append(None)
            async with factory() as other:
                await real_execute(other, TradeOrder.__table__.update().values(**mutation))
                await other.commit()
            foreign[0] = await order_snapshot(factory)
        return await real_execute(db, statement, *args, **kwargs)
    monkeypatch.setattr(AsyncSession, "execute", execute)
    with pytest.raises(HTTPException) as caught:
        await invoke()
    assert caught.value.status_code == 409
    assert "禁止覆盖" in caught.value.detail
    assert authorization.paper_execution_requires_reconciliation(caught.value)
    assert len(booked) == 1 and len(foreign) == 1
    assert await order_snapshot(factory) == foreign[0]
    assert await snapshot(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("prior_partial", [False, True])
async def test_competing_real_partial_fill_survives_late_rejection(
    quote_execution_env, monkeypatch, side, prior_partial,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, "deferred-"+side)
    if prior_partial:
        assert (await invoke(1))[0]["event"] == "partial"
    real_book = PaperBrokerAdapter.place_order
    attempts = []
    async def book(self, db, req):
        receipt = await real_book(self, db, req)
        assert receipt.fills
        attempts.append(req.order_id)
        if len(attempts) == 1:
            raise ValueError("first worker failed after real book")
        return receipt
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", book)
    real_reject = service._reject_unchanged_paper_order
    durable = []
    async def reject(db, order, checkpoint, **kwargs):
        # A different task/session executes a later round before late rejection.
        competing = await asyncio.create_task(invoke(3))
        assert competing[0]["event"] == "partial"
        durable.extend([await snapshot(factory), await order_snapshot(factory)])
        return await real_reject(db, order, checkpoint, **kwargs)
    monkeypatch.setattr(service, "_reject_unchanged_paper_order", reject)
    with pytest.raises(HTTPException) as caught:
        await invoke(2)
    assert authorization.paper_execution_requires_reconciliation(caught.value)
    assert "禁止覆盖" in caught.value.detail and len(attempts) == 2
    assert await snapshot(factory) == durable[0]
    assert await order_snapshot(factory) == durable[1]
    assert await invoke(3) == []  # actual competing round already filled once


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("fault", ["commit", "ack", "cancel", "refresh"])
async def test_rejection_persistence_failure_propagates_without_guessing(
    quote_execution_env, monkeypatch, path, fault,
):
    factory = quote_execution_env
    invoke = await prepare(factory, monkeypatch, path)
    before = await snapshot(factory)
    booked = reject_after_real_book(monkeypatch)
    real_reject = service._reject_unchanged_paper_order
    real_commit, real_refresh = AsyncSession.commit, AsyncSession.refresh
    raised = []
    async def reject(db, order, checkpoint, **kwargs):
        async def commit(session):
            if session is db:
                if fault in {"ack", "refresh"}:
                    await real_commit(session)
                if fault == "refresh":
                    return
                exc = (asyncio.CancelledError("CAS cancelled") if fault == "cancel"
                       else ValueError("CAS commit outcome unknown"))
                raised.append(exc)
                raise exc
            return await real_commit(session)
        async def refresh(session, *args, **kwargs):
            if session is db and fault == "refresh":
                exc = ValueError("CAS acknowledgement refresh lost")
                raised.append(exc)
                raise exc
            return await real_refresh(session, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(AsyncSession, "commit", commit)
            patch.setattr(AsyncSession, "refresh", refresh)
            return await real_reject(db, order, checkpoint, **kwargs)
    monkeypatch.setattr(service, "_reject_unchanged_paper_order", reject)
    with pytest.raises(asyncio.CancelledError if fault == "cancel" else ValueError) as caught:
        await invoke()
    assert caught.value is raised[0]
    assert authorization.paper_execution_requires_reconciliation(caught.value)
    assert len(booked) == 1
    assert await snapshot(factory) == before
    async with factory() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        assert (order.status == "rejected") is (fault in {"ack", "refresh"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no_checkpoint", "dirty", "cancel_wait", "rollback_failure"])
async def test_rejection_guard_never_commits_unowned_state(
    quote_execution_env, monkeypatch, mode,
):
    factory = quote_execution_env
    await prepare(factory, monkeypatch, "deferred-buy")
    before = await snapshot(factory)
    original_order = await order_snapshot(factory)
    async with factory() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        checkpoint = authorization.paper_order_checkpoint(order)
        if mode == "no_checkpoint":
            checkpoint = None
        if mode in {"dirty", "rollback_failure"}:
            account = (await db.scalars(select(PaperAccount))).one()
            account.current_capital += 99999
        async def attempt():
            await service._reject_unchanged_paper_order(
                db, order, checkpoint, reason="test only", risk_json="{}")
        if mode == "cancel_wait":
            await db.rollback()
            async with paper._TRADE_LOCK:
                task = asyncio.create_task(attempt())
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await task
        elif mode == "rollback_failure":
            real_rollback = db.rollback
            async def fail_rollback():
                await real_rollback()
                raise RuntimeError("CAS rollback acknowledgement lost")
            with monkeypatch.context() as patch:
                patch.setattr(db, "rollback", fail_rollback)
                with pytest.raises(RuntimeError, match="rollback acknowledgement") as caught:
                    await attempt()
        else:
            with pytest.raises(HTTPException) as caught:
                await attempt()
        assert authorization.paper_execution_requires_reconciliation(caught.value)
    assert await snapshot(factory) == before
    assert await order_snapshot(factory) == original_order
