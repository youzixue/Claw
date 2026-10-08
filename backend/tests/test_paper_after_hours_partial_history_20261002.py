"""Current DB fragment STRUCTURE, not actual execution/ledger/source certification."""
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text, update, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.session import Base
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import (TradeOrder, TradeFill, PaperAfterHoursResourceScope as State,
                               PaperAfterHoursResourceReceipt as Receipt)
from app.data.after_hours import _json
from app.trading import paper_after_hours_resources as resources
from app.trading.paper_after_hours_resource_schema import sqlite_guards, FULL_PROTOCOL
from test_paper_after_hours_partial_schema_20261002 import TABLES, seed, header, KEY, ORDER, AT, NOW


@pytest_asyncio.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'partial-history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=TABLES))
        await conn.execute(text("PRAGMA foreign_keys=OFF"))
        await conn.execute(text("PRAGMA recursive_triggers=OFF"))
        await conn.run_sync(seed)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), engine
    finally:
        await engine.dispose()


async def fragments(engine, count=3):
    async with engine.begin() as conn:
        for number in range(1, count+1):
            await conn.run_sync(lambda c, number=number: header(c, number))


async def read(db, **changes):
    values = dict(account_numeric_id=7, code="600000", trade_date=AT.date(), cutoff=NOW+timedelta(seconds=10))
    values.update(changes)
    return await resources._partial_receipt_history_structure(db, **values)


@asynccontextmanager
async def damaged(engine):
    """Explicit corrupt-store fixture, then reinstall every EXACT production guard."""
    async with engine.begin() as conn:
        for statement in sqlite_guards():
            name = statement.split("IF NOT EXISTS ", 1)[1].split()[0]
            await conn.execute(text("DROP TRIGGER "+name))
        yield conn
        for statement in sqlite_guards():
            await conn.execute(text(statement))


async def payload_change(conn, number, key, value, *, remove=False):
    row = (await conn.execute(select(Receipt.id, Receipt.payload_json).order_by(Receipt.id))).all()[number-1]
    payload = json.loads(row.payload_json)
    if remove:
        del payload[key]
    else:
        payload[key] = value
    await conn.execute(update(Receipt).where(Receipt.id==row.id).values(
        payload_json=_json(payload), content_hash=hashlib.sha256(_json(payload).encode()).hexdigest()))


@pytest.mark.asyncio
async def test_three_fragments_restart_read_and_no_authority(database):
    maker, engine = database
    await fragments(engine)
    for _ in range(2):
        async with maker() as db:
            pending = TradeOrder(order_id="unflushed", broker="paper", account_id="default",
                code="600000", side="buy", order_type="limit", price=10, quantity=100, status="pending")
            db.add(pending)
            result = await read(db)
            assert pending in db.new
            with db.no_autoflush:
                assert await db.scalar(select(func.count()).select_from(TradeOrder).where(
                    TradeOrder.order_id=="unflushed")) == 0
            db.expunge(pending)  # The test\'s later queries must not flush its own seed.
            assert result["receipt_count"] == 3
            assert result["orders"][ORDER] == dict(original_quantity=300, fragment_count=3,
                filled_quantity=300, remaining_quantity=0, fixed_price=10, current_status="filled",
                last_stored_fill_at=(NOW+timedelta(seconds=2)).isoformat())
            assert result["structure_only"] is True
            for key in ("execution_authorized","ledger_contract_supported","durable_resource_slices_certified",
                        "raw_execution_contract_certified","quota_binding_certified","fees_certified",
                        "realized_pnl_certified","historical_asof_projection_certified"):
                assert result[key] is False
            assert await db.scalar(select(State.revision)) == 3
            assert await db.scalar(select(PaperAccount.current_capital)) == 50000
            with pytest.raises(ValueError):
                await resources._verified_receipt_bindings(db, account_numeric_id=7,
                    code="600000", trade_date=AT.date(), cutoff=NOW+timedelta(seconds=10))


@pytest.mark.asyncio
async def test_no_root_or_empty_history_is_not_zero_consumption(database):
    maker, engine = database
    async with maker() as db:
        with pytest.raises(ValueError, match="missing_consumption"):
            await read(db)
        with pytest.raises(ValueError, match="root_missing"):
            await read(db, code="000001")


@pytest.mark.asyncio
async def test_partial_canceled_projection_keeps_economic_history(database):
    maker, engine = database
    await fragments(engine, 1)
    async with engine.begin() as conn:
        await conn.execute(update(TradeOrder).values(status="canceled"))
    async with maker() as db:
        result = await read(db)
        assert result["orders"][ORDER]["filled_quantity"] == 100
        assert result["orders"][ORDER]["remaining_quantity"] == 200
        assert result["orders"][ORDER]["current_status"] == "canceled"
        assert result["execution_authorized"] is False  # Not cancel-contract certification.


@pytest.mark.asyncio
async def test_interleaved_originals_share_root_but_not_cumulative_projection(database):
    maker, engine = database
    other = "other.intent"
    async with engine.begin() as conn:
        await conn.execute(insert(TradeOrder).values(id=2, order_id=other, broker="paper",
            account_id="default", order_type="after_hours_fixed", code="600000", side="buy",
            price=10.5, quantity=200, filled_quantity=0, status="submitted", trade_date=AT.date(),
            created_at=AT-timedelta(seconds=1), decision_at=AT-timedelta(seconds=1)))
        await conn.run_sync(lambda c: header(c, 1))
        await conn.run_sync(lambda c: header(c, 2, order_id=other, original_quantity=200, fragment_index=1))
        await conn.run_sync(lambda c: header(c, 3, fragment_index=2))
        await conn.run_sync(lambda c: header(c, 4, fragment_index=3))
        await conn.run_sync(lambda c: header(c, 5, order_id=other, original_quantity=200, fragment_index=2))
    async with maker() as db:
        result = await read(db)
        assert result["receipt_count"] == 5
        assert result["orders"][ORDER]["filled_quantity"] == 300
        assert result["orders"][other]["filled_quantity"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("fragment_index",3), ("fragment_index",True), ("fragment_index",2.0),
    ("original_quantity",200), ("cumulative_before",0), ("cumulative_before",False),
    ("cumulative_after",201), ("remaining_quantity_after",0),
    ("account_numeric_id",7.0), ("trade_fill_id",1), ("paper_trade_id",True),
    ("request_id","afp-"+"G"*31), ("request_id","afp"+"2"*31),
    ("fixed_price","10"), ("fixed_price",False), ("fixed_price",11),
    ("protocol_version",FULL_PROTOCOL), ("scope_key","b"*64), ("order_id","other"),
    ("allocation_id","b"*64),
])
async def test_rehashed_bad_coordinates_never_publish_partial_summary(database, key, value):
    maker, engine = database
    await fragments(engine)
    async with damaged(engine) as conn:
        await payload_change(conn, 2, key, value)
    async with maker() as db:
        with pytest.raises(ValueError):
            await read(db)
        assert await db.scalar(select(State.revision)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", [
    "hash", "missing_receipt", "missing_fill", "missing_trade", "missing_order",
    "root_revision", "root_clock", "projection_zero", "projection_status",
    "wrong_account", "wrong_code", "wrong_day", "wrong_broker_link",
    "fee_mismatch", "negative_fees", "fixed_price", "historical_clock",
    "full_protocol", "unknown_protocol", "duplicate_json", "extra_fill",
    "duplicate_trade_claim", "huge_status",
])
async def test_corrupt_history_fails_whole_scope_without_repair(database, damage):
    maker, engine = database
    await fragments(engine)
    async with damaged(engine) as conn:
        if damage == "hash":
            await conn.execute(update(Receipt).where(Receipt.trade_fill_id==2).values(content_hash="0"*64))
        elif damage in {"missing_receipt","missing_fill","missing_trade","missing_order"}:
            table = {"missing_receipt":"paper_after_hours_resource_receipt","missing_fill":"trade_fill",
                     "missing_trade":"paper_trade_log","missing_order":"trade_order"}[damage]
            await conn.execute(text(f"DELETE FROM {table} WHERE id="+("1" if damage=="missing_order" else "2")))
        elif damage == "root_revision":
            await conn.execute(update(State).values(revision=0))
        elif damage == "root_clock":
            await conn.execute(update(State).values(checked_at=NOW))
        elif damage == "projection_zero":
            await conn.execute(update(TradeOrder).values(filled_quantity=0))
        elif damage == "projection_status":
            await conn.execute(update(TradeOrder).values(status="submitted"))
        elif damage == "wrong_account":
            await conn.execute(update(PaperTradeLog).where(PaperTradeLog.id==2).values(account_id=8))
        elif damage == "wrong_code":
            await conn.execute(update(TradeFill).where(TradeFill.id==2).values(code="000001"))
        elif damage == "wrong_day":
            await conn.execute(update(TradeFill).where(TradeFill.id==2).values(trade_date=AT.date()-timedelta(days=1)))
        elif damage == "wrong_broker_link":
            await conn.execute(update(TradeFill).where(TradeFill.id==2).values(broker_trade_id="1"))
        elif damage in {"fee_mismatch","negative_fees"}:
            await conn.execute(update(TradeFill).where(TradeFill.id==2).values(commission=-1 if damage=="negative_fees" else 6))
        elif damage == "fixed_price":
            await conn.execute(update(TradeFill).where(TradeFill.id==2).values(price=9))
            await conn.execute(update(PaperTradeLog).where(PaperTradeLog.id==2).values(price=9))
            await payload_change(conn,2,"fixed_price",9)
        elif damage == "historical_clock":
            at = NOW-timedelta(microseconds=1)
            await conn.execute(update(TradeFill).where(TradeFill.id==2).values(filled_at=at))
            await conn.execute(update(PaperTradeLog).where(PaperTradeLog.id==2).values(trade_time=at))
            await conn.execute(update(Receipt).where(Receipt.trade_fill_id==2).values(recorded_at=at))
        elif damage in {"full_protocol","unknown_protocol"}:
            value = FULL_PROTOCOL if damage=="full_protocol" else "unknown"
            await conn.execute(update(Receipt).where(Receipt.trade_fill_id==2).values(protocol_version=value))
            await payload_change(conn,2,"protocol_version",value)
        elif damage == "duplicate_json":
            raw = await conn.scalar(select(Receipt.payload_json).where(Receipt.trade_fill_id==2))
            await conn.execute(update(Receipt).where(Receipt.trade_fill_id==2).values(
                payload_json=raw[:-1]+',"cumulative_before":100}'))
        elif damage in {"extra_fill","duplicate_trade_claim"}:
            await conn.execute(insert(TradeFill).values(fill_id="unreceipted",
                order_id=ORDER if damage=="extra_fill" else "other", broker="paper", code="600000",
                side="buy", price=10, quantity=100, broker_trade_id="2", raw_json="{}"))
        elif damage == "huge_status":
            await conn.execute(update(TradeOrder).values(status="x"*(resources.MAX_READ_BYTES+1)))
    async with maker() as db:
        with pytest.raises(ValueError):
            await read(db)


@pytest.mark.asyncio
async def test_declared_leaf_and_utf8_read_budget_before_return(database, monkeypatch):
    maker, engine = database
    await fragments(engine)
    async with maker() as db:
        actual = (await read(db))["read_text_bytes"]
    monkeypatch.setattr(resources,"MAX_READ_BYTES",actual-1)
    async with maker() as db:
        with pytest.raises(ValueError, match="byte_budget"):
            await read(db)
    monkeypatch.setattr(resources,"MAX_READ_BYTES",actual)
    async with maker() as db:
        assert (await read(db))["read_text_bytes"] == actual


@pytest.mark.asyncio
async def test_unrelated_proof_and_annotations_are_not_hydrated_or_certified(database, monkeypatch):
    maker, engine = database
    await fragments(engine,1)
    async with damaged(engine) as conn:
        await conn.execute(update(TradeOrder).values(reason="原"*10000, risk_json="原"*10000))
        await conn.execute(update(TradeFill).values(raw_json="原"*10000))
    monkeypatch.setattr(resources,"MAX_PAYLOAD_BYTES",4096)
    async with maker() as db:
        result = await read(db)
        assert result["receipt_count"] == 1
        assert result["raw_execution_contract_certified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["rows","payload","missing_guard","changed_root"])
async def test_budgets_and_root_recheck_do_not_publish_incomplete_history(database, monkeypatch, kind):
    maker, engine = database
    await fragments(engine)
    if kind == "rows":
        monkeypatch.setattr(resources,"MAX_RECEIPTS",2)
    elif kind == "payload":
        monkeypatch.setattr(resources,"MAX_PAYLOAD_BYTES",256)
    elif kind == "missing_guard":
        async with engine.begin() as conn:
            await conn.execute(text("DROP TRIGGER af_resource_receipt_no_update"))
    else:
        original, root_reads = resources._book_row, 0
        async def changed(db, model, predicate, **kwargs):
            nonlocal root_reads
            row = await original(db, model, predicate, **kwargs)
            if model is State and row is not None:
                root_reads += 1
                if root_reads == 2:
                    row.revision += 1
            return row
        monkeypatch.setattr(resources,"_book_row",changed)
    async with maker() as db:
        with pytest.raises(ValueError, match="root_changed_during_read" if kind=="changed_root" else None):
            await read(db)
    if kind == "changed_root":
        assert root_reads == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changes",[
    dict(account_numeric_id=8), dict(account_numeric_id=True), dict(code="000001"),
    dict(trade_date=AT.date()+timedelta(days=1)),
    dict(cutoff=NOW-timedelta(microseconds=1)),
])
async def test_identity_and_cutoff_fail_closed(database, changes):
    maker, engine = database
    await fragments(engine)
    async with maker() as db:
        with pytest.raises(ValueError):
            await read(db, **changes)


@pytest.mark.asyncio
async def test_multi_header_transfer_is_charged_before_next_sql_row(database, monkeypatch):
    maker, engine = database
    await fragments(engine, 2)
    original, transferred = resources._book_row, []
    async def track(*args, **kwargs):
        row = await original(*args, **kwargs)
        if row is not None:
            transferred.append(row._read_text_bytes)
        return row
    monkeypatch.setattr(resources,"_book_row",track)
    async with maker() as db:
        await read(db)
    first = transferred[:5]  # State + first header/fill/order/trade.
    boundary = sum(first)
    async with maker() as db:
        payload_bytes = sum((await db.scalars(select(func.length(
            func.cast(Receipt.payload_json, resources.LargeBinary))))).all())
    assert payload_bytes < boundary
    transferred.clear()
    monkeypatch.setattr(resources,"MAX_READ_BYTES",boundary)
    async with maker() as db:
        with pytest.raises(ValueError, match="byte_budget"):
            await read(db)
    assert transferred == first
    assert sum(transferred) == boundary


@pytest.mark.asyncio
async def test_tiny_total_budget_gates_root_before_hydration(database, monkeypatch):
    maker, engine = database
    await fragments(engine)
    original, transferred = resources._book_row, []
    async def track(*args, **kwargs):
        row = await original(*args, **kwargs)
        if row is not None:
            transferred.append(row._read_text_bytes)
        return row
    monkeypatch.setattr(resources,"_book_row",track)
    monkeypatch.setattr(resources,"MAX_READ_BYTES",64)
    async with maker() as db:
        with pytest.raises(ValueError, match="byte_budget"):
            await read(db)
    assert transferred == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("status","closed"),("account_name","other")])
async def test_existing_account_identity_is_not_recreated(database, field, value):
    maker, engine = database
    await fragments(engine)
    async with damaged(engine) as conn:
        await conn.execute(update(PaperAccount).values(**{field:value}))
    async with maker() as db:
        with pytest.raises(ValueError):
            await read(db)
        assert await db.scalar(select(func.count()).select_from(PaperAccount)) == 1
        assert await db.scalar(select(State.revision)) == 3
