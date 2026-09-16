"""Upper sell boundary: real service/books/SQLite, never production APIs.

Risk and sell-context mocks isolate failure propagation, NOT risk/selection
acceptance. Quote, T+1, broker, fee allocation, receipts and replay stay real.
"""
import asyncio
import json
import sqlite3
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog, PaperTradeLog, PaperSaleAccounting
from app.models.trading import TradeFill, TradeOrder
from app.models.stock import StockSpot
from app.trading import service, paper_authorization as authorization
from test_quote_round_execution import quote_execution_env
from test_paper_atomic_execution_20260914 import (
    AT, isolated_transaction_policy_and_calendar, prepare, persistence_fault, snapshot,
)


async def prepare_auto(factory, monkeypatch):
    await prepare(factory, monkeypatch, "immediate-sell")
    monkeypatch.setattr(settings, "PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND", False)
    # Only avoid unrelated indicator/selection inputs; force an explicit exit.
    monkeypatch.setattr(paper, "_build_short_sell_context",
                        AsyncMock(return_value={"price": 10.0}))
    context = {
        "round_id": f"explicit-immediate:600001:{AT.isoformat()}",
        "as_of_at": AT - timedelta(seconds=3),
        "committed_at": AT - timedelta(seconds=1),
        "trade_date": AT.date(), "quality_status": "ok",
    }
    # Freeze the same seeded quote into the upper round payload; a round with
    # no records intentionally fails closed and would never reach the fault.
    async with factory() as db:
        record = dict((await db.execute(select(*StockSpot.__table__.columns)
            .where(StockSpot.code == "600001"))).mappings().one())
    context["records"] = [record]
    context["records_by_code"] = {"600001": record}
    async def invoke():
        async with factory() as db:
            account = await paper._get_or_create_account(db, paper.PAPER_ACCOUNT_DEFAULT)
            token = paper._QUOTE_ROUND_CONTEXT.set(context)
            try:
                logs = await paper._run_auto_sells(
                    db, account=account, run_id="uncertain-upper", trade_date=AT.date(),
                    trigger="isolated-test", execute=True, quote_now=AT,
                    forced_exit_reason_by_code={"600001": "测试隔离：强制退出"})
                await db.commit()
                return logs
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)
    return invoke


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["commit", "ack"])
async def test_real_auto_sell_commit_uncertainty_propagates_without_false_logs(
    quote_execution_env, monkeypatch, kind,
):
    factory = quote_execution_env
    invoke = await prepare_auto(factory, monkeypatch)
    before = await snapshot(factory)
    # Spy delegates to the REAL auto logger; no replacement submit/book call.
    auto_log = AsyncMock(wraps=paper._add_auto_log)
    runtime_error = Mock(wraps=paper.logger.error)
    monkeypatch.setattr(paper, "_add_auto_log", auto_log)
    monkeypatch.setattr(paper.logger, "error", runtime_error)
    async with persistence_fault(monkeypatch, factory, kind) as fired:
        with pytest.raises(ValueError, match="atomic final commit " + kind) as error:
            await invoke()
    assert fired == [kind], "must reach the real fill unit's final COMMIT"
    assert type(error.value) is ValueError
    assert authorization.paper_execution_requires_reconciliation(error.value)
    assert auto_log.await_count == 0
    assert runtime_error.call_count == 1
    assert not paper._TRADE_LOCK.locked()
    async with factory() as db:
        assert list((await db.scalars(select(PaperAutoTradeLog).where(
            (PaperAutoTradeLog.action == "skip_sell") |
            (PaperAutoTradeLog.decision == "blocked")))).all()) == []
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.status != "rejected"
        if kind == "commit":
            assert order.filled_quantity == 0
            assert await snapshot(factory) == before
            return
        fill = (await db.scalars(select(TradeFill))).one()
        trade = await db.get(PaperTradeLog, int(fill.broker_trade_id))
        accounting = (await db.scalars(select(PaperSaleAccounting))).one()
        assert accounting is not None
        assert trade.trade_type == "sell"
        assert (fill.quantity, fill.price, fill.commission, fill.tax, fill.filled_at) == (
            trade.amount, trade.price, trade.commission, trade.tax, trade.trade_time)
        assert fill.order_id == order.order_id
        assert order.status == "filled" and order.filled_quantity == fill.quantity == 500
        assert order.last_fill_round_id == fill.fill_round_id
        durable = await snapshot(factory)
        assert len(durable["paper_trade_log"]) == len(before["paper_trade_log"]) + 1
        assert len(durable["paper_sale_accounting"]) == len(durable["trade_fill"]) == 1
        assert durable["cash"] != before["cash"]
        assert durable["positions"] != before["positions"]
        replay = await service.submit_order(db, service.SubmitOrderCommand(
            code=order.code, side=order.side, price=order.price, quantity=order.quantity,
            account_id=order.account_id, idempotency_key=order.idempotency_key))
        assert replay["idempotent_replay"] is True
        assert replay["order"]["order_id"] == order.order_id
        assert [row["fill_id"] for row in replay["fills"]] == [fill.fill_id]
    assert await snapshot(factory) == durable


@pytest.mark.asyncio
async def test_real_broker_business_refusal_remains_explicit_blocked_log(
    quote_execution_env, monkeypatch,
):
    factory = quote_execution_env
    invoke = await prepare_auto(factory, monkeypatch)
    # Remove ONLY local fixture entry evidence: real broker/book fee guard refuses.
    async with factory() as db:
        await db.execute(delete(PaperTradeLog))
        await db.commit()
    before = await snapshot(factory)
    runtime_error = Mock(wraps=paper.logger.error)
    monkeypatch.setattr(paper.logger, "error", runtime_error)
    logs = await invoke()
    assert len(logs) == 1
    assert (logs[0].action, logs[0].decision) == ("skip_sell", "blocked")
    payload = json.loads(logs[0].candidate_json)
    assert payload["execution_block_code"] == "order_not_accepted"
    assert "买费分摊依据不完整" in logs[0].reason
    assert logs[0].executed_trade_id is None
    assert runtime_error.call_count == 0
    async with factory() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.status == "rejected" and order.filled_quantity == 0
        assert json.loads(order.risk_json)["broker_rejection_http_status"] == 409
        assert len(list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action == "skip_sell"))).all())) == 1
    assert await snapshot(factory) == before


@pytest.mark.parametrize("exc", [
    ValueError("plain"),
    asyncio.CancelledError("cancel"),
    OperationalError("fixture", {}, sqlite3.OperationalError("database is locked")),
])
def test_marker_preserves_exception_identity_and_type(exc):
    original_type = type(exc)
    assert not authorization.paper_execution_requires_reconciliation(exc)
    assert authorization.mark_paper_execution_uncertain(exc) is exc
    assert type(exc) is original_type
    assert authorization.paper_execution_requires_reconciliation(exc)
    assert not authorization.paper_execution_requires_reconciliation(ValueError("unmarked"))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["value", "cancel", "operational", "cleanup"])
async def test_transaction_propagates_original_type_and_marks_cleanup_failure(
    quote_execution_env, monkeypatch, kind,
):
    error = {
        "value": ValueError("unit failure"),
        "cancel": asyncio.CancelledError("unit cancel"),
        "operational": OperationalError("fixture", {}, sqlite3.OperationalError("locked")),
        "cleanup": RuntimeError("rollback failure"),
    }[kind]
    async with quote_execution_env() as db:
        if kind == "cleanup":
            rollback = AsyncMock(side_effect=error)
            monkeypatch.setattr(db, "rollback", rollback)
        with pytest.raises(type(error)) as raised:
            async with authorization._paper_order_transaction(db):
                assert authorization.paper_transaction_active(db)
                if kind == "cleanup":
                    raise ValueError("original body failure")
                raise error
        assert raised.value is error
        assert authorization.paper_execution_requires_reconciliation(raised.value)
        assert not authorization.paper_transaction_active(db)
        assert not paper._TRADE_LOCK.locked()
        if kind == "cleanup":
            assert rollback.await_count >= 1
