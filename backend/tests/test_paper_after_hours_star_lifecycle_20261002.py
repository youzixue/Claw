"""STAR original-200 / fragment-100 temporary storage and cancellation boundaries.

Rows/fees/cash/positions below are fabricated by the existing kernel fixture,
NOT created by broker/book/risk or a live provider. Real service cancellation,
stored-root recognition and terminal replay are exercised on those rows.
Normal STAR buy risk remains observe-only and is tested separately, unchanged.
"""
import asyncio
from contextlib import contextmanager
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, event

from app.api.v1 import paper
from app.data.after_hours import _json
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.stock import StockSpot, StockTag
from app.models.trading import TradeOrder, TradeFill
from app.paper.position_policy import buy_order_evidence
from app.trading import service, paper_after_hours_execution as execution
from app.trading import paper_after_hours_resources as resources, paper_authorization as auth
from app.trading.broker import PaperBrokerAdapter
from app.trading.paper_execution_integrity import account_execution_integrity_evidence

from test_trading_api import trading_client
from test_paper_after_hours_20261002 import environment, economics, request
from test_paper_after_hours_allocation_20261002 import AT, NOW, audited_fixture
from test_paper_after_hours_partial_contract_20261002 import original, freeze
from test_paper_after_hours_partial_allocation_20261002 import capacity, append_offer
from test_paper_after_hours_partial_book_20261002 import book
from test_paper_after_hours_partial_fees_20261002 import fee_model
from test_paper_after_hours_partial_resources_20261002 import database, persist, State, Receipt, ORDER_FIELDS
from test_paper_after_hours_consumers_20261002 import damage_fixture


CODES = ["688256", "689009"]


def star_material(code):
    order, payload = original(quantity=200), capacity(100)
    order.code = code
    risk = json.loads(order.risk_json)
    intent = risk["paper_after_hours_intent"]
    intent.update(code=code, declaration_parameters=execution.original_declaration_parameters(
        code, order.side, order.price, order.quantity))
    order.risk_json = _json(risk)
    payload.update(code=code, exchange="SSE", session_id="SSE." + code + ".2026-09-30")
    payload["official_close"].update(code=code, exchange="SSE")
    return order, payload


@pytest_asyncio.fixture
async def store(database, monkeypatch):
    maker, engine = database
    clock = [NOW]
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    monkeypatch.setattr(paper, "_TRADE_LOCK", asyncio.Lock())
    forbidden = AsyncMock(side_effect=AssertionError("fixture storage is not broker/risk authorization"))
    monkeypatch.setattr(PaperBrokerAdapter, "place_order", forbidden)
    monkeypatch.setattr(PaperBrokerAdapter, "cancel_order", forbidden)
    monkeypatch.setattr(service, "_locked_paper_risk_evidence", forbidden)
    yield SimpleNamespace(maker=maker, engine=engine, clock=clock, forbidden=forbidden)
    forbidden.assert_not_awaited()


async def save(store, item):
    store.clock[0] = item[4].trade_time
    async with store.maker() as db, db.begin():
        view = item[2]
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == view.order_id))
        if order is None:
            # The parent fixture omits manual provenance; initialize it BEFORE
            # any receipt so terminal replay reaches the intended service gate.
            db.add(TradeOrder(**{key: getattr(view, key) for key in ORDER_FIELDS}, source="manual"))
            await db.flush()
        out = await persist(db, item)
        # Synthetic equivalent of the service's final projection, in the same
        # temporary transaction. The fixture does not prove real risk/book fees.
        order = await db.scalar(select(TradeOrder).execution_options(populate_existing=True))
        risk = json.loads(order.risk_json)
        risk["paper_after_hours_consumption"] = out
        order.risk_json, order.updated_at = _json(risk), store.clock[0]
    return out


async def first_fragment(store, code):
    order, payload = star_material(code)
    item = book(freeze(payload, order))
    out = await save(store, item)
    assert out["revision"] == 1 and out["execution_authorized"] is False
    return item, payload


async def economic_snapshot(store, *, include_order=False):
    # Full values of these SMALL temporary rows, not production/unbounded reads.
    # Valid cancellation changes the order only; terminal replay changes nothing.
    result = []
    models = (PaperAccount, PaperPosition, PaperTradeLog, TradeFill, State, Receipt)
    if include_order:
        models += (TradeOrder,)
    async with store.maker() as db:
        for model in models:
            table = model.__table__
            rows = await db.execute(select(*table.columns).order_by(*table.primary_key.columns))
            result.append((table.name, [tuple(row) for row in rows]))
    return result


@contextmanager
def sql_read_only(store):
    # A snapshot after session close can miss flushed writes that rolled back.
    # Catch DML as it is emitted, including raw SQL and compiled ORM statements.
    writes = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if (verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
                or any(getattr(context, key, False) for key in ("isinsert", "isupdate", "isdelete"))):
            writes.append(verb)
    event.listen(store.engine.sync_engine, "before_cursor_execute", capture)
    try:
        yield
    finally:
        event.remove(store.engine.sync_engine, "before_cursor_execute", capture)
    assert not writes, f"terminal read-only path emitted {'/'.join(writes)}"


async def recognize_and_consume(store, code, cutoff):
    # Integrity takes its own public clock, so advance it as well as the explicit
    # historical cutoff; a next-day assertion must really read on the next day.
    store.clock[0] = cutoff
    async with store.maker() as db:
        bindings = await resources._verified_partial_receipt_bindings(db,
            account_numeric_id=7, code=code, trade_date=AT.date(), cutoff=cutoff)
        trades = list((await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all())
        token = auth._PAPER_TRANSACTION.set((db, asyncio.current_task()))
        try:
            following = SimpleNamespace(order_id="following", account_id="default",
                code=code, side="buy", trade_date=AT.date())
            integrity = await account_execution_integrity_evidence(
                db, following, {"quote_round_id": "following.round"})
        finally:
            auth._PAPER_TRANSACTION.reset(token)
        quota = await buy_order_evidence(db, account_id=7, trades=trades, as_of=cutoff)
        assert not db.new and not db.dirty and not db.deleted
        return bindings, integrity, quota


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
async def test_star_stored_fragment_uses_original_quantity_after_source_ttl(
        store, audited_fixture, fee_model, code):
    first, _ = await first_fragment(store, code)
    before = await economic_snapshot(store)
    # Historical verification doesn't require a fresh quote or authorize a new fill.
    bindings, integrity, quota = await recognize_and_consume(store, code, AT + timedelta(days=1))
    assert set(bindings) == {first[3].fill_id}
    assert next(iter(bindings.values()))["quantity"] == 100
    assert integrity["status"] == "validated"
    assert list(quota.values()) == [{"status": "verified", "order_id": "head", "scale_in": False}]
    async with store.maker() as db:
        order = await db.scalar(select(TradeOrder))
        assert (order.quantity, order.filled_quantity, order.status) == (200, 100, "partial")
        assert await db.scalar(select(State.revision)) == 1
    assert await economic_snapshot(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
async def test_star_second_hundred_completes_one_original_and_terminal_replay_is_read_only(
        store, audited_fixture, fee_model, code):
    first, payload = await first_fragment(store, code)
    more = append_offer(payload, identifier="second.offer", shares=100,
        at=AT + timedelta(seconds=2), frame="star.frame.2")
    dispatch = AT + timedelta(seconds=3)
    async with store.maker() as db:
        order = await db.scalar(select(TradeOrder))
        state, account = await db.scalar(select(State)), await db.get(PaperAccount, 7)
        priors = await resources._durable_partial_priors(db, state.scope_key, account, cutoff=dispatch)
        candidate = freeze(more, order, dispatch=dispatch, priors=priors)
    second = book(candidate, sequence=2)
    out = await save(store, second)
    assert out["revision"] == 2
    bindings, integrity, quota = await recognize_and_consume(store, code, dispatch)
    assert set(bindings) == {first[3].fill_id, second[3].fill_id}
    assert sum(value["quantity"] for value in bindings.values()) == 200
    assert {value["order_id"] for value in quota.values()} == {"head"}
    assert all(value["status"] == "verified" and value["scale_in"] is False for value in quota.values())
    assert integrity["checked_trade_count"] == integrity["checked_receipt_count"] == 2
    async with store.maker() as db:
        order = await db.scalar(select(TradeOrder))
        assert (order.quantity, order.filled_quantity, order.status) == (200, 200, "filled")
        assert await db.scalar(select(PaperPosition.buy_amount)) == 200
        trades = list((await db.scalars(select(PaperTradeLog))).all())
        assert [trade.commission for trade in trades] == [5.0, 5.0]
    before = await economic_snapshot(store, include_order=True)
    with sql_read_only(store):
        async with store.maker() as db:
            assert (await db.scalar(select(TradeOrder.source))) == "manual"
            replay = await service._execute_paper_after_hours_partial_order(db, "head", feed={})
            assert replay["idempotent_replay"] is True
            assert len(replay["fills"]) == 2
            assert not db.new and not db.dirty and not db.deleted
            assert (await service.cancel_order(db, "head"))["status"] == "unchanged"
            assert not db.new and not db.dirty and not db.deleted
    assert await economic_snapshot(store, include_order=True) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("reason", ["user", "session_end", "later_closed_day"])
async def test_star_cancel_or_expiry_preserves_hundred_consumed_and_only_cancels_hundred_remaining(
        store, audited_fixture, fee_model, code, reason):
    first, _ = await first_fragment(store, code)
    before = await economic_snapshot(store)
    store.clock[0] = (NOW + timedelta(milliseconds=1) if reason == "user"
        else AT.replace(minute=30) if reason == "session_end" else AT + timedelta(days=1))
    async with store.maker() as db:
        if reason == "user":
            result = await service.cancel_order(db, "head")
            assert result["status"] == "canceled"
        else:
            result = await service.reconcile_paper_after_hours_intents(db)
            assert result["canceled"] == ["head"] and result["errors"] == []
        order = await db.scalar(select(TradeOrder))
        proof = json.loads(order.risk_json)["paper_after_hours_cancel"]
        assert (order.quantity, order.filled_quantity, order.avg_fill_price, order.status) == (200, 100, 10, "canceled")
        assert proof["original_quantity"] == 200 and proof["unfilled_quantity"] == 100
        assert proof["filled_quantity_preserved"] == 100
        assert proof["verified_fill_ids"] == [first[3].fill_id]
        assert proof["exchange_cancel_receipt"] is None
        assert proof["reason"] == ("user_cancel" if reason == "user" else "session_end")
    assert await economic_snapshot(store) == before
    _, integrity, quota = await recognize_and_consume(store, code, store.clock[0])
    assert integrity["status"] == "validated" and all(v["status"] == "verified" for v in quota.values())
    terminal_before = await economic_snapshot(store, include_order=True)
    with sql_read_only(store):
        async with store.maker() as db:
            assert (await service.cancel_order(db, "head"))["status"] == "unchanged"
            assert not db.new and not db.dirty and not db.deleted
            with pytest.raises(HTTPException) as exc:
                await service._execute_paper_after_hours_partial_order(db, "head", feed={})
            assert exc.value.status_code == 409  # Canceled state, not missing manual provenance.
            assert not db.new and not db.dirty and not db.deleted
    assert await economic_snapshot(store, include_order=True) == terminal_before
    assert await economic_snapshot(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
async def test_read_only_oracle_catches_flushed_order_write_even_if_session_rolls_it_back(
        store, audited_fixture, fee_model, code):
    await first_fragment(store, code)
    before = await economic_snapshot(store, include_order=True)
    with pytest.raises(AssertionError, match="terminal read-only path emitted UPDATE"):
        with sql_read_only(store):
            async with store.maker() as db:
                order = await db.scalar(select(TradeOrder))
                order.error_message = "fixture mutation to verify the read-only oracle"
                await db.flush()
                assert not db.new and not db.dirty and not db.deleted
                # Session close rolls back this temporary write. Post-close
                # snapshots alone would therefore falsely report no mutation.
    assert await economic_snapshot(store, include_order=True) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
async def test_bad_star_original_minimum_cannot_be_recognized_or_release_consumption(
        store, audited_fixture, fee_model, code):
    await first_fragment(store, code)
    before = await economic_snapshot(store)
    async with store.maker() as db:
        async def corrupt(session):
            order = await session.scalar(select(TradeOrder))
            risk = json.loads(order.risk_json)
            risk["paper_after_hours_intent"]["original_quantity"] = 100
            order.quantity, order.risk_json = 100, _json(risk)
            await session.flush()
        await damage_fixture(db, corrupt)  # TEMPORARY corrupt storage only; restore all guards.
        await db.commit()
    async with store.maker() as db:
        with pytest.raises(ValueError, match="unsupported original fixed-price declaration quantity"):
            await resources._verified_partial_receipt_bindings(db,
                account_numeric_id=7, code=code, trade_date=AT.date(), cutoff=NOW)
        with pytest.raises(HTTPException) as exc:
            await service.cancel_order(db, "head")
        assert exc.value.status_code == 409
    assert await economic_snapshot(store) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("code", CODES)
async def test_legal_star_declaration_does_not_override_existing_observe_only_buy_risk(environment, code):
    async with environment.maker() as db:
        name = "科创股票" if code.startswith("688") else "科创存托"
        db.add(StockSpot(code=code, name=name, price=11.57, volume=100000, amount=1157000))
        db.add(StockTag(code=code, name=name, board_type="star", board_tag="observe_only"))
        await db.commit()
    before = await economics(environment)
    response = await environment.client.post("/trading/orders", json=request(code=code, quantity=200))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["order"]["status"] == "risk_blocked"
    assert result["risk"]["stock_status"]["board_type"] == "star"
    assert result["risk"]["stock_status"]["board_tag"] == "observe_only"
    assert any(d["rule"] == "observe_only" and d["level"] == "block" for d in result["risk"]["decisions"])
    assert result["fills"] == [] and await economics(environment) == before
    environment.forbidden.assert_not_awaited()
