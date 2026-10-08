"""Dormant consumer/schema tests with EXPLICIT fabricated future ledger fixtures.

These fixtures bypass NO production API: they set private future scope fields
only in the temporary test DB. They do not prove broker/book/risk integration or
actual provider capability, and never request a real source or write a live DB.
"""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
import pytest_asyncio
from sqlalchemy import select, text, update, func, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.sql.dml import Update

from app.db.session import Base
from app.models.paper import PaperAccount, PaperTradeLog, PaperPosition
from app.models.trading import TradeOrder, TradeFill, PaperAfterHoursResourceScope as State, PaperAfterHoursResourceReceipt as Receipt
from app.trading import paper_authorization as auth, paper_after_hours_allocation as allocator
from app.trading import paper_after_hours_resources as resources
from app.trading import paper_after_hours_execution as execution
from app.trading.broker import BrokerOrderRequest
from app.data.after_hours import _json
from app.trading.paper_after_hours_resource_schema import sqlite_guards
from test_paper_after_hours_allocation_20261002 import feed, local, external, SOURCE, AT, NOW


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'resources.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        await conn.execute(text("PRAGMA recursive_triggers=OFF"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        db.add(PaperAccount(account_name="default", strategy="default", status="active",
            initial_capital=50000, current_capital=50000, total_assets=50000))
        await db.commit()
    from app.api.v1 import paper
    monkeypatch.setattr(paper, "_public_order_clock", lambda: NOW)
    try:
        yield maker, engine
    finally:
        await engine.dispose()


@pytest.fixture
def full_fixture(monkeypatch):
    monkeypatch.setattr(allocator, "_PROVIDERS", {
        SOURCE: allocator._Provider("fixture.v1", "order_level", allocator.METHOD)})


@contextmanager
def future_scope(db, req, contract, timing):
    """Artificial future authorization, test-only; current guard cannot issue it."""
    token = auth._PAPER_TRANSACTION.set((db, asyncio.current_task()))
    try:
        with auth._paper_execution_scope(db, req, immediate_evidence_json=_json(contract)):
            scope = auth._SCOPE.get()
            scope.stage, scope.ledger_timing = "ledger", timing
            yield scope
    finally:
        auth._PAPER_TRANSACTION.reset(token)


async def bundle(db, *, name="intent.1", sequence=1, payload=None, changes=None, prior=(), use_candidate=False):
    """Manufacture a future ledger fixture (not a real paper broker/book call)."""
    payload = feed() if payload is None else payload
    raw_order = local(name, sequence=sequence)
    account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == "default"))
    initial = json.loads(raw_order.risk_json)
    initial["paper_after_hours_intent"]["numeric_account_id"] = account.id
    raw_order.risk_json = _json(initial)
    order = TradeOrder(**{k: getattr(raw_order, k) for k in ("id", "order_id", "order_type", "broker",
        "account_id", "code", "side", "price", "quantity", "status", "filled_quantity", "trade_date", "risk_json")},
        strategy_version="af.fixture.v1", signal_id="", decision_round_id="decision-" + name,
        decision_at=AT - timedelta(seconds=1), created_at=AT - timedelta(seconds=1))
    db.add(order)
    await db.flush()
    proposal = allocator.propose_allocations(payload, [order], now=NOW,
        scenario_account="default", prior_proposals=prior)["proposals"][0]
    snapshot = allocator._verify(payload, now=NOW)
    contract = {"contract_version": resources.FILL_VERSION, "status": "validated", "mandatory": True,
        "simulation_only": True, "code": order.code, "side": order.side, "account_id": "default",
        "account_numeric_id": account.id, "order_id": name, "request_id": name,
        "filled_quantity": order.quantity, "fill_price": 10.0, "allocation": proposal,
        "original_limit_price": order.price, "original_quantity": order.quantity,
        "quote_round_id": "fixture-round", "decision_at": order.decision_at.isoformat(),
        "dispatch_validated_at": NOW.isoformat(), "source_quote_at": payload["source_quote_at"],
        "received_at": payload["received_at"], "source_available_at": payload["available_at"],
        "source_max_age_seconds": 15, "quote_expires_at": snapshot.expires_at.isoformat(),
        "session_end_at": AT.replace(minute=30).isoformat()}
    timing = {"guard_version": resources.LEDGER_VERSION, "status": "validated",
        "input_contract_version": resources.FILL_VERSION, "input_sha256": hashlib.sha256(_json(contract).encode()).hexdigest(),
        "before_mutation_checked_at": NOW.isoformat(), "lock_acquired_checked_at": NOW.isoformat(),
        **{k: contract[k] for k in ("dispatch_validated_at", "quote_round_id", "quote_expires_at", "session_end_at")}}
    if use_candidate:
        candidate = execution._freeze_fill_candidate(payload, order, account_numeric_id=account.id,
            quote_round_id="fixture-round", dispatch_at=NOW, prior_proposals=prior)
        contract = json.loads(candidate.contract_json)
        frozen_req = BrokerOrderRequest(order_id=name, code=order.code, side=order.side,
            price=contract["fill_price"], quantity=order.quantity, order_type=execution.MODE,
            account_name=order.account_id, strategy_version=order.strategy_version,
            signal_id=name, decision_round_id=order.decision_round_id,
            fill_round_id="fixture-round", filled_at=NOW)
        lock, _ = execution._validate_candidate_clock(candidate, frozen_req, phase="lock_acquired", clock=lambda: NOW)
        _, timing = execution._validate_candidate_clock(candidate, frozen_req, phase="before_mutation",
            lock_checked_at=lock, clock=lambda: NOW)
    trade = PaperTradeLog(account_id=account.id, code=order.code, trade_type="buy", price=10, amount=100,
        trade_time=NOW, commission=5, tax=0, realized_pnl=None, signal_id=name,
        strategy_version=order.strategy_version, decision_round_id=order.decision_round_id, fill_round_id="fixture-round")
    db.add(trade)
    await db.flush()
    raw = {k: getattr(trade, k) for k in ("id", "code", "trade_type", "price", "amount", "commission", "tax",
        "realized_pnl", "signal_id", "strategy_version", "decision_round_id", "fill_round_id")}
    raw.update(trade_time=NOW.isoformat(), after_hours_fixed_execution=deepcopy(contract),
               ledger_execution_timing=deepcopy(timing))
    fill = TradeFill(fill_id="fill-" + name, order_id=name, broker="paper", code=order.code, side="buy",
        price=10, quantity=100, commission=5, tax=0, realized_pnl=None, broker_trade_id=str(trade.id),
        decision_round_id=order.decision_round_id, fill_round_id="fixture-round",
        trade_date=AT.date(), filled_at=NOW, raw_json=_json(raw))
    db.add(fill)
    # Fixture-only economics make rollback observable; this is NOT book/risk verification.
    account.current_capital -= 1005
    db.add(PaperPosition(account_id=account.id, code=order.code, buy_price=10, buy_amount=100,
        buy_time=NOW, is_closed=False))
    await db.flush()
    req = BrokerOrderRequest(order_id=name, code=order.code, side="buy", price=10, quantity=100,
        order_type="after_hours_fixed", account_name="default", strategy_version=order.strategy_version,
        signal_id=name, decision_round_id=order.decision_round_id,
        fill_round_id="fixture-round", filled_at=NOW)
    if changes:
        changes(order, fill, trade, contract, timing, req)
        timing["input_sha256"] = hashlib.sha256(_json(contract).encode()).hexdigest()
        raw = json.loads(fill.raw_json)
        raw["after_hours_fixed_execution"], raw["ledger_execution_timing"] = deepcopy(contract), deepcopy(timing)
        fill.raw_json = _json(raw)
    return order, fill, trade, contract, timing, req, payload


async def persist(db, item):
    order, fill, trade, contract, timing, req, payload = item
    with future_scope(db, req, contract, timing):
        return await resources._persist_fill_resources(db, order_id=order.order_id,
            fill_id=fill.fill_id, feed=payload)


@pytest.mark.asyncio
async def test_no_active_or_old_scope_cannot_consume(database):
    maker, _ = database
    async with maker() as db:
        with pytest.raises(HTTPException) as exc:
            await resources._persist_fill_resources(db, order_id="x", fill_id="x", feed=feed())
        assert getattr(exc.value, "status_code") == 403
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0


@pytest.mark.asyncio
async def test_production_aggregate_source_rejects_even_future_fixture_scope(database, full_fixture, monkeypatch):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        monkeypatch.setattr(allocator, "_PROVIDERS", {"sse_fixed_price": allocator._Provider("official")})
        item[-1].update(source="sse_fixed_price", source_version="official")
        with pytest.raises(HTTPException) as exc:
            await persist(db, item)
        assert getattr(exc.value, "status_code") == 409
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
        # Test owns rollback; no caller may commit this fabricated unbound fixture.
        await db.rollback()


@pytest.mark.asyncio
async def test_receipt_group_multi_resource_binding_restart_and_once(database, full_fixture):
    maker, _ = database
    payload = feed()
    payload["initial"]["orders"] = [external("offer.a", quantity=70, sequence=1),
                                    external("offer.b", quantity=230, sequence=2)]
    payload["initial"]["sequence"] = payload["last_sequence"] = 2
    async with maker() as db, db.begin():
        item = await bundle(db, payload=payload)
        with future_scope(db, item[5], item[3], item[4]):
            result = await resources._persist_fill_resources(db, order_id=item[0].order_id,
                fill_id=item[1].fill_id, feed=payload)
            with pytest.raises(HTTPException) as exc:
                await resources._persist_fill_resources(db, order_id=item[0].order_id,
                    fill_id=item[1].fill_id, feed=payload)
            assert getattr(exc.value, "status_code") == 403
        item[0].status, item[0].filled_quantity = "filled", 100
        first_key = result["scope_key"]
    # New session/root transaction reconstructs consumption from immutable headers.
    async with maker() as db, db.begin():
        account = await db.scalar(select(PaperAccount))
        prior = await resources._durable_priors(db, first_key, account, cutoff=NOW)
        item = await bundle(db, name="intent.2", sequence=2, payload=payload, prior=prior)
        result = await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
        assert result["revision"] == 2
        second = item[3]["allocation"]
        assert [(r["external_order_id"], r["offset"], r["shares"]) for r in second["resources"]] == [("offer.b", 30, 100)]
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 2
        assert await db.scalar(select(State.revision)) == 2


@pytest.mark.asyncio
async def test_cas_conflict_rolls_back_fixture_economics_and_all_resources(database, full_fixture, monkeypatch):
    maker, _ = database
    async with maker() as db:
        execute = db.execute
        async def lose_cas(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == State.__tablename__:
                return SimpleNamespace(rowcount=0)
            return await execute(statement, *args, **kwargs)
        monkeypatch.setattr(db, "execute", lose_cas)
        with pytest.raises(HTTPException) as exc:
            async with db.begin():
                item = await bundle(db)
                await persist(db, item)
        assert getattr(exc.value, "status_code") == 409
        assert await db.scalar(select(PaperAccount.current_capital)) == 50000
        for model in (Receipt, State, PaperTradeLog, TradeFill, PaperPosition, TradeOrder):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
async def test_old_root_transaction_cannot_reuse_scope(database, full_fixture):
    maker, _ = database
    async with maker() as db:
        item = await bundle(db)
        with future_scope(db, item[5], item[3], item[4]):
            await db.rollback()
            await db.execute(select(PaperAccount.id))
            with pytest.raises(HTTPException) as exc:
                resources._owned_scope(db)
            assert getattr(exc.value, "status_code") == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [False, True])
async def test_missing_trigger_cannot_authorize_consumption(database, full_fixture, tamper):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        await db.execute(text("DROP TRIGGER af_resource_receipt_no_replace"))
        if tamper:
            await db.execute(text("CREATE TRIGGER af_resource_receipt_no_replace BEFORE INSERT ON paper_after_hours_resource_receipt BEGIN SELECT 1; END"))
        with pytest.raises(HTTPException) as exc:
            await persist(db, item)
        assert getattr(exc.value, "status_code") == 403
        await db.rollback()


@pytest.mark.asyncio
async def test_two_sessions_database_cas_only_one_winner(database, full_fixture):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        result = await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
    async def compete():
        async with maker() as db, db.begin():
            changed = await db.execute(update(State).where(State.scope_key == result["scope_key"],
                State.revision == 1).values(revision=2))
            return changed.rowcount
    assert sorted(await asyncio.gather(compete(), compete())) == [0, 1]


@pytest.mark.asyncio
async def test_append_only_and_replace_guards_work_without_foreign_keys(database, full_fixture):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
    statements = [
        "DELETE FROM paper_after_hours_resource_receipt",
        "UPDATE paper_after_hours_resource_receipt SET content_hash='changed'",
        "INSERT OR REPLACE INTO paper_after_hours_resource_receipt SELECT * FROM paper_after_hours_resource_receipt",
        "DELETE FROM paper_after_hours_resource_scope",
        "INSERT OR REPLACE INTO paper_after_hours_resource_scope SELECT * FROM paper_after_hours_resource_scope",
        "UPDATE paper_after_hours_resource_scope SET revision=0",
        "DELETE FROM trade_fill", "DELETE FROM paper_trade_log", "DELETE FROM trade_order",
        "DELETE FROM paper_account",
        "UPDATE trade_fill SET quantity=200", "UPDATE paper_trade_log SET price=11",
        "UPDATE trade_order SET account_id='promotion'", "UPDATE paper_account SET account_name='promotion'",
        "INSERT OR REPLACE INTO trade_fill SELECT * FROM trade_fill",
        "INSERT OR REPLACE INTO paper_trade_log SELECT * FROM paper_trade_log",
        "INSERT OR REPLACE INTO trade_order SELECT * FROM trade_order",
        "INSERT OR REPLACE INTO paper_account SELECT * FROM paper_account",
    ]
    for statement in statements:
        async with maker() as db:
            assert await db.scalar(text("PRAGMA foreign_keys")) == 0
            assert await db.scalar(text("PRAGMA recursive_triggers")) == 0
            with pytest.raises(IntegrityError):
                await db.execute(text(statement))
            await db.rollback()
    # State bootstrap conflicts are rejected rather than upserted or reset.
    async with maker() as db, db.begin():
        assert await db.scalar(select(State.revision)) == 1
        # Non-economic annotations still work without rewriting the bound facts.
        await db.execute(update(PaperTradeLog).values(excluded_from_performance=True))


@pytest.mark.asyncio
async def test_header_requires_real_joined_book_without_foreign_keys(database):
    maker, _ = database
    async with maker() as db:
        db.add(Receipt(allocation_id="0"*64, scope_key="1"*64, trade_fill_id=999, paper_trade_id=999,
            order_id="invented", protocol_version=resources.CONSUMPTION_VERSION,
            payload_json="{}", content_hash="2"*64, recorded_at=NOW))
        with pytest.raises(IntegrityError):
            await db.flush()
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    lambda o,f,t,c,g,r: setattr(f, "quantity", 200),
    lambda o,f,t,c,g,r: setattr(f, "broker_trade_id", "999"),
    lambda o,f,t,c,g,r: setattr(t, "account_id", 999),
    lambda o,f,t,c,g,r: setattr(t, "commission", None),
    lambda o,f,t,c,g,r: c.update(original_limit_price=9),
    lambda o,f,t,c,g,r: c.update(decision_at=(NOW+timedelta(minutes=1)).isoformat()),
    lambda o,f,t,c,g,r: g.update(before_mutation_checked_at=(NOW+timedelta(seconds=1)).isoformat()),
    lambda o,f,t,c,g,r: c.update(contract_version="pending_paper_fill_timing_v1_20260914"),
])
async def test_inconsistent_actual_book_or_contract_rejected(database, full_fixture, change):
    maker, _ = database
    async with maker() as db:
        with pytest.raises(HTTPException):
            async with db.begin():
                item = await bundle(db, changes=change)
                await persist(db, item)
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
        assert await db.scalar(select(PaperAccount.current_capital)) == 50000


@pytest.mark.asyncio
async def test_finalize_crosses_close_rolls_back_every_new_fixture_fact(database, full_fixture, monkeypatch):
    maker, _ = database
    from app.api.v1 import paper
    clocks = iter([NOW, AT.replace(minute=30)])
    monkeypatch.setattr(paper, "_public_order_clock", lambda: next(clocks))
    async with maker() as db:
        with pytest.raises(HTTPException) as exc:
            async with db.begin():
                await persist(db, await bundle(db))
        assert exc.value.status_code == 409
        assert await db.scalar(select(PaperAccount.current_capital)) == 50000
        for model in (Receipt, State, PaperTradeLog, TradeFill, PaperPosition):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_receipt", "bad_hash", "bad_book"])
async def test_missing_or_corrupt_history_never_becomes_zero_consumption(database, full_fixture, damage):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        result = await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
    # Explicit administrative corruption fixture: remove one guard, damage history,
    # then RESTORE it. The normal INSERT/UPDATE/DELETE API cannot do this.
    name = {"missing_receipt": "af_resource_receipt_no_delete",
            "bad_hash": "af_resource_receipt_no_update",
            "bad_book": "af_bound_trade_fill_no_update"}[damage]
    sql = {"missing_receipt": "DELETE FROM paper_after_hours_resource_receipt",
           "bad_hash": "UPDATE paper_after_hours_resource_receipt SET content_hash='bad'",
           "bad_book": "UPDATE trade_fill SET broker_trade_id='999'"}[damage]
    async with maker() as db, db.begin():
        await db.execute(text("DROP TRIGGER " + name))
        await db.execute(text(sql))
        for statement in sqlite_guards():
            await db.execute(text(statement))
    async with maker() as db:
        with pytest.raises(HTTPException) as exc:
            async with db.begin():
                await persist(db, await bundle(db, name="intent.2", sequence=2))
        assert exc.value.status_code == 409
        assert await db.scalar(select(State.revision)) == 1
        assert await db.scalar(select(PaperAccount.current_capital)) == 48995
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("session_id","new.session"), ("source_version","fixture.v2")])
async def test_source_namespace_change_cannot_create_another_pool(database, full_fixture, monkeypatch, field, value):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
    revised = feed()
    revised[field] = value
    if field == "source_version":
        monkeypatch.setattr(allocator, "_PROVIDERS", {
            SOURCE: allocator._Provider(value, "order_level", allocator.METHOD)})
    async with maker() as db:
        with pytest.raises(HTTPException) as exc:
            async with db.begin():
                await persist(db, await bundle(db, name="intent.2", sequence=2, payload=revised))
        assert exc.value.status_code == 409
        assert await db.scalar(select(func.count()).select_from(State)) == 1
        assert await db.scalar(select(State.revision)) == 1


@pytest.mark.asyncio
async def test_duplicate_real_book_link_cannot_be_claimed(database, full_fixture):
    maker, _ = database
    async with maker() as db:
        with pytest.raises(HTTPException) as exc:
            async with db.begin():
                item = await bundle(db)
                fill = item[1]
                duplicate = TradeFill(fill_id="fill-duplicate", order_id=fill.order_id,
                    broker="paper", code=fill.code, side=fill.side, price=10, quantity=100,
                    broker_trade_id=fill.broker_trade_id, trade_date=fill.trade_date, raw_json=fill.raw_json)
                db.add(duplicate)
                await persist(db, item)
        assert exc.value.status_code == 409
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0


@pytest.mark.asyncio
async def test_durable_read_budget_cannot_drop_previous_consumption(database, full_fixture, monkeypatch):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        result = await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
    monkeypatch.setattr(resources, "MAX_RECEIPTS", 0)
    async with maker() as db:
        with pytest.raises(HTTPException) as exc:
            async with db.begin():
                await persist(db, await bundle(db, name="intent.2", sequence=2))
        assert exc.value.status_code == 409
        assert await db.scalar(select(State.revision)) == 1



@pytest.mark.asyncio
@pytest.mark.parametrize("table,column", [
    ("trade_fill", "id"), ("trade_fill", "fill_id"),
    ("paper_trade_log", "id"), ("trade_order", "id"),
    ("trade_order", "order_id"), ("trade_order", "idempotency_key"), ("paper_account", "id"),
])
async def test_update_or_replace_unbound_source_cannot_destroy_bound_destination(database, full_fixture, table, column):
    maker, _ = database
    async with maker() as db, db.begin():
        bound = await bundle(db)
        bound[0].idempotency_key = "bound-fixture-key"
        await persist(db, bound)
        bound[0].status, bound[0].filled_quantity = "filled", 100
        ordinary = await bundle(db, name="intent.unbound", sequence=2,
                                prior=[bound[3]["allocation"]])
        ordinary[0].idempotency_key = "unbound-fixture-key"
        unrelated_account = PaperAccount(account_name="promotion", strategy="promotion",
            status="active", initial_capital=50000, current_capital=50000, total_assets=50000)
        db.add(unrelated_account)
        await db.flush()
        account_id = await db.scalar(select(PaperAccount.id).where(PaperAccount.account_name == "default"))
        old_rows = {"trade_order": bound[0], "trade_fill": bound[1], "paper_trade_log": bound[2]}
        other_rows = {"trade_order": ordinary[0], "trade_fill": ordinary[1], "paper_trade_log": ordinary[2]}
        target = account_id if table == "paper_account" else getattr(old_rows[table], column)
        source_id = unrelated_account.id if table == "paper_account" else other_rows[table].id
        bound_id = account_id if table == "paper_account" else old_rows[table].id
    async with maker() as db:
        assert await db.scalar(text("PRAGMA recursive_triggers")) == 0
        assert await db.scalar(text("PRAGMA foreign_keys")) == 0
        with pytest.raises(IntegrityError):
            await db.execute(text(f"UPDATE OR REPLACE {table} SET {column}=:target WHERE id=:source"),
                             {"target": target, "source": source_id})
        await db.rollback()
        assert await db.scalar(text(f"SELECT {column} FROM {table} WHERE id=:bound"), {"bound": bound_id}) == target
        assert await db.scalar(text(f"SELECT count(*) FROM {table} WHERE id=:source"), {"source": source_id}) == 1
        assert await db.scalar(select(State.revision)) == 1
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 1
        if table == "trade_order" and column == "idempotency_key":
            # INSERT replacement can also collide via the OTHER unique key,
            # while both primary/order IDs are entirely new.
            with pytest.raises(IntegrityError):
                await db.execute(text("""INSERT OR REPLACE INTO trade_order
                    (id, order_id, broker, account_id, code, side, order_type, price,
                     quantity, status, trade_date, idempotency_key, created_at)
                    VALUES (999, 'unrelated.third', 'paper', 'default', '600000', 'buy',
                            'limit', 10, 100, 'submitted', :day, :target, :created)"""),
                    {"day": AT.date(), "target": target, "created": NOW})
            await db.rollback()
            assert await db.scalar(select(TradeOrder.id).where(TradeOrder.id == bound_id)) == bound_id


@pytest.mark.asyncio
async def test_history_budget_includes_fill_raw_and_order_risk_utf8(database, full_fixture, monkeypatch):
    maker, _ = database
    def extras(order, fill, *args):
        raw = json.loads(fill.raw_json)
        raw["ignored_annotation"] = "é" * 5000
        fill.raw_json = _json(raw)
        risk = json.loads(order.risk_json)
        risk["ignored_annotation"] = "字" * 3000
        order.risk_json = _json(risk)
    async with maker() as db, db.begin():
        item = await bundle(db, changes=extras)
        result = await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
    async with maker() as db:
        account = await db.scalar(select(PaperAccount))
        receipt = await db.scalar(select(Receipt))
        raw = await db.scalar(select(TradeFill.raw_json))
        risk = await db.scalar(select(TradeOrder.risk_json))
        payload_bytes = len(receipt.payload_json.encode())
        header_row = await resources._book_row(db, Receipt, Receipt.id == receipt.id)
        fill_row = await resources._book_row(db, TradeFill, TradeFill.id == item[1].id)
        order_row = await resources._book_row(db, TradeOrder, TradeOrder.id == item[0].id)
        trade_row = await resources._book_row(db, PaperTradeLog, PaperTradeLog.id == item[2].id)
        total_bytes = sum(row._read_text_bytes for row in (header_row, fill_row, order_row, trade_row))
        assert total_bytes > payload_bytes
        monkeypatch.setattr(resources, "MAX_READ_BYTES", total_bytes - 1)
        with pytest.raises(ValueError, match="byte_budget|read_budget"):
            await resources._durable_priors(db, result["scope_key"], account, cutoff=NOW)
        # Exact byte boundary, including all necessary book proof text, is valid.
        monkeypatch.setattr(resources, "MAX_READ_BYTES", total_bytes)
        prior = await resources._durable_priors(db, result["scope_key"], account, cutoff=NOW)
        assert prior == [item[3]["allocation"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("model,field", [(TradeFill, "raw_json"), (TradeOrder, "risk_json")])
async def test_book_projection_gates_utf8_bytes_in_sql_before_fetch(database, full_fixture, monkeypatch, model, field):
    maker, engine = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        row = item[1] if model is TradeFill else item[0]
        setattr(row, field, "字" * 200)  # 200 characters, 600 UTF-8 bytes.
        await db.flush()
        monkeypatch.setattr(resources, "MAX_PAYLOAD_BYTES", 256)
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            with pytest.raises(ValueError, match="resource_book_text_byte_budget"):
                await resources._book_row(db, model, model.id == row.id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert len(statements) == 1
        assert "CASE WHEN" in statements[0] and "AS BLOB" in statements[0]
        assert "trade_order.reason" not in statements[0]
        await db.rollback()



@pytest.mark.asyncio
async def test_oversized_mutable_status_cannot_bypass_sql_read_budget(database, full_fixture):
    maker, engine = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        result = await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
        order_id = item[0].order_id
    # Normal SQL, with all guards present: status may change, but its VARCHAR(20)
    # declaration alone does NOT bound SQLite storage or a SELECT result.
    async with maker() as db, db.begin():
        await db.execute(text("UPDATE trade_order SET status=:huge WHERE order_id=:id"),
                         {"huge": "s" * 8192, "id": order_id})
    async with maker() as db:
        with pytest.raises(ValueError, match="resource_book_text_byte_budget"):
            await resources._book_row(db, TradeOrder, TradeOrder.order_id == order_id,
                                      max_text_bytes=4096)
        account = await db.scalar(select(PaperAccount))
        with pytest.raises(ValueError, match="resource_book_text_byte_budget"):
            await resources._durable_priors(db, result["scope_key"], account, cutoff=NOW)
        assert await db.scalar(select(State.revision)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("model,column", [(TradeFill, "quantity"), (TradeOrder, "decision_at")])
async def test_sqlite_affinity_cannot_return_large_text_as_numeric_or_clock(database, full_fixture, model, column):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        row_id = item[1].id if model is TradeFill else item[0].id
        await db.execute(text(f"UPDATE {model.__tablename__} SET {column}=:huge WHERE id=:id"),
                         {"huge": "invalid" * 2000, "id": row_id})
        with pytest.raises(ValueError, match="resource_book_text_byte_budget"):
            await resources._book_row(db, model, model.id == row_id)
        await db.rollback()


@pytest.mark.asyncio
async def test_multiple_headers_are_not_prefetched_outside_charged_budget(database, full_fixture, monkeypatch):
    maker, _ = database
    def extra_raw(order, fill, *args):
        raw = json.loads(fill.raw_json)
        raw["ignored_note"] = "x" * 10000
        fill.raw_json = _json(raw)
    async with maker() as db, db.begin():
        item = await bundle(db, changes=extra_raw)
        result = await persist(db, item)
        item[0].status, item[0].filled_quantity = "filled", 100
        first = (item[0].id, item[1].id, item[2].id)
    async with maker() as db, db.begin():
        account = await db.scalar(select(PaperAccount))
        prior = await resources._durable_priors(db, result["scope_key"], account, cutoff=NOW)
        item2 = await bundle(db, name="intent.2", sequence=2, prior=prior)
        await persist(db, item2)
        item2[0].status, item2[0].filled_quantity = "filled", 100
    async with maker() as db:
        account = await db.scalar(select(PaperAccount))
        headers = (await db.execute(select(Receipt).order_by(Receipt.id))).scalars().all()
        models_and_predicates = (
            (Receipt, Receipt.id == headers[0].id),
            (TradeFill, TradeFill.id == first[1]),
            (TradeOrder, TradeOrder.id == first[0]),
            (PaperTradeLog, PaperTradeLog.id == first[2]),
        )
        first_rows = [await resources._book_row(db, model, predicate) for model, predicate in models_and_predicates]
        boundary = sum(row._read_text_bytes for row in first_rows)
        assert sum(len(h.payload_json.encode()) for h in headers) < boundary
        monkeypatch.setattr(resources, "MAX_READ_BYTES", boundary)
        original, transferred = resources._book_row, []
        async def track(*args, **kwargs):
            row = await original(*args, **kwargs)
            if row is not None:
                transferred.append(row._read_text_bytes)
            return row
        monkeypatch.setattr(resources, "_book_row", track)
        with pytest.raises(ValueError, match="resource_book_text_byte_budget"):
            await resources._durable_priors(db, result["scope_key"], account, cutoff=NOW)
        # First complete header+book uses B exactly. SQL gates the second header
        # before transfer, instead of fetching both then failing after B+h2.
        assert transferred == [row._read_text_bytes for row in first_rows]
        assert sum(transferred) == boundary
        assert await db.scalar(select(State.revision)) == 2


@pytest.mark.asyncio
async def test_book_projection_never_hydrates_unrelated_large_reason(database, full_fixture):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        item[0].reason = "ignored" * (resources.MAX_PAYLOAD_BYTES // 7 + 1)
        await db.flush()
        row = await resources._book_row(db, TradeOrder, TradeOrder.id == item[0].id)
        assert row.risk_json == item[0].risk_json
        assert not hasattr(row, "reason")
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("root_only", [False, True])
async def test_nonempty_resource_migration_cannot_destroy_evidence(database, full_fixture, monkeypatch, root_only):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    maker, engine = database
    async with maker() as db, db.begin():
        if root_only:
            account = await db.scalar(select(PaperAccount))
            snapshot = allocator._verify(feed(), now=NOW)
            key, values = resources._scope_values(snapshot, account)
            db.add(State(scope_key=key, **values, revision=0,
                terminal_sequence=snapshot.terminal_sequence,
                lifecycle_prefix_hash=snapshot.lifecycle_prefixes[snapshot.terminal_sequence],
                source_quote_at=snapshot.source_at, source_available_at=snapshot.available_at, checked_at=NOW))
        else:
            item = await bundle(db)
            await persist(db, item)
            item[0].status, item[0].filled_quantity = "filled", 100
    path = Path(__file__).resolve().parents[1] / "alembic/versions/039_after_hours_resource_receipts.py"
    spec = importlib.util.spec_from_file_location("af_nonempty_resource_migration_fixture", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with engine.begin() as connection:
        def attempt(conn):
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
            with pytest.raises(RuntimeError, match="lossy downgrade prohibited"):
                migration.downgrade()
        await connection.run_sync(attempt)
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(State)) == 1
        assert await db.scalar(select(func.count()).select_from(Receipt)) == (0 if root_only else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_account", [None, True, 99])
async def test_original_numeric_account_not_rebound_during_consumption(database, full_fixture, changed_account):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        proof = json.loads(item[0].risk_json)
        proof["paper_after_hours_intent"]["numeric_account_id"] = changed_account
        item[0].risk_json = _json(proof)
        with pytest.raises(HTTPException, match="original_numeric_account_conflict"):
            await persist(db, item)
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("quantity", 100.0), ("quantity", True)])
async def test_consumer_rejects_equal_value_wrong_request_type(database, full_fixture, field, value):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db)
        setattr(item[5], field, value)
        with pytest.raises(HTTPException):
            await persist(db, item)
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_account", [True, 1.0])
async def test_contract_numeric_account_not_a_boolean_or_float(database, full_fixture, changed_account):
    maker, _ = database
    async with maker() as db, db.begin():
        def change(order, fill, trade, contract, timing, req):
            assert contract["account_numeric_id"] == 1
            contract["account_numeric_id"] = changed_account
        item = await bundle(db, changes=change)
        with pytest.raises(HTTPException):
            await persist(db, item)
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0
        await db.rollback()


@pytest.mark.asyncio
async def test_pure_candidate_clock_contract_interoperates_with_dormant_consumer(database, full_fixture):
    maker, _ = database
    async with maker() as db, db.begin():
        item = await bundle(db, use_candidate=True)
        assert item[3]["execution_authorized"] is False
        assert item[4]["execution_authorized"] is False
        result = await persist(db, item)
        assert result["revision"] == 1 and result["execution_authorized"] is False
        item[0].status, item[0].filled_quantity = "filled", 100
    # Only the test has manufactured real book rows/private future scope. This
    # validates schema compatibility, not service/risk/broker authorization.
    async with maker() as db:
        account = await db.scalar(select(PaperAccount))
        priors = await resources._durable_priors(db, result["scope_key"], account, cutoff=NOW)
        assert priors == [item[3]["allocation"]]
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 1


def test_migration_empty_sqlite_upgrade_and_non_sqlite_fail_closed(monkeypatch):
    from sqlalchemy import create_engine, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).resolve().parents[1] / "alembic/versions/039_after_hours_resource_receipts.py"
    spec = importlib.util.spec_from_file_location("af_resource_migration_fixture", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        # Existing book tables only, so migration owns the two new tables.
        Base.metadata.create_all(conn, tables=[PaperAccount.__table__, TradeOrder.__table__,
            PaperTradeLog.__table__, TradeFill.__table__])
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        assert "paper_after_hours_resource_receipt" in inspect(conn).get_table_names()
        migration.downgrade()
        assert "paper_after_hours_resource_scope" not in inspect(conn).get_table_names()
    engine.dispose()
    fake = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.setattr(migration, "op", fake)
    with pytest.raises(RuntimeError):
        migration.upgrade()
