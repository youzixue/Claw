"""Read-only future-receipt recognition fixtures, NOT real book/broker integration."""
import asyncio
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, func

from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.paper.position_policy import buy_order_evidence, _buy_receipt_matches_order
from app.trading import paper_after_hours_resources as resources, paper_authorization as auth
from app.trading.paper_execution_integrity import account_execution_integrity_evidence
from app.trading.paper_after_hours_resource_schema import sqlite_guards
from app.trading.service import _json_dumps
from app.data.after_hours import _json
from test_paper_after_hours_resources_20261002 import database, full_fixture, bundle, persist, State, Receipt
from test_paper_after_hours_allocation_20261002 import NOW, AT


async def seed(maker, count=1, *, with_receipts=True):
    ids, prior = [], []
    async with maker() as db, db.begin():
        for sequence in range(1, count + 1):
            item = await bundle(db, name=f"intent.{sequence}", sequence=sequence, prior=prior, use_candidate=True)
            if with_receipts:
                await persist(db, item)
            item[0].status, item[0].filled_quantity = "filled", item[0].quantity
            ids.append((item[0].order_id, item[1].id, item[2].id))
            prior.append(item[3]["allocation"])
    return ids


async def read_evidence(db, *, cutoff=NOW, side="buy"):
    trades = list((await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all())
    token = auth._PAPER_TRANSACTION.set((db, asyncio.current_task()))
    try:
        next_order = SimpleNamespace(order_id="next.order", account_id="default", code="600000", side=side,
                                     trade_date=AT.date())
        integrity = await account_execution_integrity_evidence(db, next_order, {"quote_round_id": "next.round"})
    finally:
        auth._PAPER_TRANSACTION.reset(token)
    quota = await buy_order_evidence(db, account_id=1, trades=trades, as_of=cutoff)
    return integrity, quota


@pytest.mark.asyncio
async def test_typed_receipts_recognized_read_only_after_restart_and_source_ttl(database, full_fixture):
    maker, _ = database
    ids = await seed(maker, count=2)
    async with maker() as db:
        before = await db.scalar(select(State.revision)), await db.scalar(select(PaperAccount.current_capital))
        # Historical receipt recognition does not pretend it is a NEW live quote.
        late = NOW + timedelta(days=1)
        roots = await resources._verified_receipt_bindings(db, account_numeric_id=1,
            code="600000", trade_date=AT.date(), cutoff=late)
        assert set(roots) == {item[0] for item in ids}
        integrity, quota = await read_evidence(db, cutoff=late)
        assert integrity["status"] == "validated"
        assert integrity["checked_trade_count"] == integrity["checked_receipt_count"] == 2
        assert [q["status"] for q in quota.values()] == ["verified", "verified"]
        assert before == (await db.scalar(select(State.revision)), await db.scalar(select(PaperAccount.current_capital)))
        assert not db.new and not db.dirty and not db.deleted
        # The synchronous old predicate MUST NOT verify the new mode by JSON alone.
        fill = await db.get(TradeFill, ids[0][1])
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == ids[0][0]))
        trade = await db.get(PaperTradeLog, ids[0][2])
        assert not _buy_receipt_matches_order(trade, fill, order, "default", late)


@pytest.mark.asyncio
async def test_json_contract_without_durable_consumption_cannot_free_quota(database, full_fixture):
    maker, _ = database
    await seed(maker, with_receipts=False)
    async with maker() as db:
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "blocked"
        assert list(quota.values()) == [{"status": "invalid"}]
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 0


async def damage_fixture(db, operation):
    # Only this temporary database: emulate corrupted legacy storage, then restore
    # every real guard so the reader must validate history, not just trigger names.
    installed = list((await db.execute(text("SELECT name FROM sqlite_master WHERE type='trigger' AND "
        "name GLOB 'af_*'"))).scalars())
    for name in installed:
        assert name.startswith("af_") and name.replace("_", "").isalnum()
        await db.execute(text(f'DROP TRIGGER "{name}"'))
    await operation(db)
    for statement in sqlite_guards():
        await db.execute(text(statement))


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["delete_header", "delete_fill", "bad_hash", "root_revision",
    "root_clock", "root_clock_backwards", "root_prefix", "original_numeric_account", "future_intent",
    "intent_identity", "moved_book_account", "mixed_ordinary_contract"])
async def test_damaged_consumption_or_book_blocks_both_consumers(database, full_fixture, damage):
    maker, _ = database
    ids = await seed(maker, count=2)
    async with maker() as db, db.begin():
        async def mutate(session):
            if damage == "delete_header":
                await session.execute(text("DELETE FROM paper_after_hours_resource_receipt WHERE id=1"))
            elif damage == "delete_fill":
                await session.execute(text("DELETE FROM trade_fill WHERE id=:id"), {"id": ids[0][1]})
            elif damage == "bad_hash":
                await session.execute(text("UPDATE paper_after_hours_resource_receipt SET content_hash=:hash WHERE id=1"),
                                      {"hash": "0" * 64})
            elif damage == "root_revision":
                await session.execute(text("UPDATE paper_after_hours_resource_scope SET revision=3"))
            elif damage == "root_clock":
                await session.execute(text("UPDATE paper_after_hours_resource_scope SET checked_at=:at"),
                                      {"at": (NOW + timedelta(seconds=1)).isoformat(" ")})
            elif damage == "root_clock_backwards":
                await session.execute(text("UPDATE paper_after_hours_resource_scope SET checked_at=:at"),
                                      {"at": (AT + timedelta(milliseconds=200)).isoformat(" ")})
            elif damage == "root_prefix":
                await session.execute(text("UPDATE paper_after_hours_resource_scope SET lifecycle_prefix_hash=:hash"),
                                      {"hash": "0" * 64})
            elif damage == "moved_book_account":
                session.add(PaperAccount(account_name="promotion", status="active", initial_capital=50000,
                                         current_capital=50000, total_assets=50000))
                await session.flush()
                other = await session.scalar(select(PaperAccount.id).where(PaperAccount.account_name == "promotion"))
                await session.execute(text("UPDATE paper_trade_log SET account_id=:other WHERE id=:id"),
                                      {"other": other, "id": ids[0][2]})
                await session.execute(text("UPDATE trade_order SET account_id='promotion' WHERE order_id=:id"),
                                      {"id": ids[0][0]})
            elif damage in {"original_numeric_account", "future_intent", "intent_identity"}:
                order = await session.scalar(select(TradeOrder).where(TradeOrder.order_id == ids[0][0]))
                raw = json.loads(order.risk_json)
                if damage == "original_numeric_account":
                    raw["paper_after_hours_intent"]["numeric_account_id"] = 9
                elif damage == "future_intent":
                    raw["paper_after_hours_intent"]["accepted_at"] = (NOW + timedelta(days=1)).isoformat()
                else:
                    raw["paper_after_hours_intent"]["side"] = "sell"
                await session.execute(text("UPDATE trade_order SET risk_json=:raw WHERE order_id=:id"),
                                      {"raw": _json(raw), "id": ids[0][0]})
            else:
                fill = await session.get(TradeFill, ids[0][1])
                raw = json.loads(fill.raw_json)
                raw["immediate_execution_evidence"] = raw["after_hours_fixed_execution"]
                await session.execute(text("UPDATE trade_fill SET raw_json=:raw WHERE id=:id"),
                                      {"raw": _json(raw), "id": ids[0][1]})
        await damage_fixture(db, mutate)
    async with maker() as db:
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "blocked"
        assert all(q["status"] in {"invalid", "missing"} for q in quota.values())
        # Corruption is not repaired and resource revision is never reset/released.
        assert await db.scalar(select(State.revision)) == (3 if damage == "root_revision" else 2)


@pytest.mark.asyncio
async def test_future_root_blocks_pit_recognition_without_repair(database, full_fixture):
    maker, _ = database
    await seed(maker)
    async with maker() as db:
        with pytest.raises(ValueError):
            await resources._verified_receipt_bindings(db, account_numeric_id=1, code="600000",
                trade_date=AT.date(), cutoff=NOW - timedelta(microseconds=1))
        _, quota = await read_evidence(db, cutoff=NOW - timedelta(microseconds=1))
        assert list(quota.values()) == [{"status": "invalid"}]


@pytest.mark.asyncio
async def test_original_account_not_inferred_from_name_or_caller_views(database, full_fixture):
    maker, _ = database
    ids = await seed(maker)
    async with maker() as db:
        with pytest.raises(ValueError):
            await resources._verified_receipt_bindings(db, account_numeric_id=99, code="600000",
                                                      trade_date=AT.date(), cutoff=NOW)
        roots = await resources._verified_receipt_bindings(db, account_numeric_id=1, code="600000",
                                                          trade_date=AT.date(), cutoff=NOW)
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == ids[0][0]))
        fill, trade = await db.get(TradeFill, ids[0][1]), await db.get(PaperTradeLog, ids[0][2])
        view = SimpleNamespace(**{key: getattr(order, key) for key in ("order_id", "order_type", "broker",
            "code", "side", "account_id", "quantity", "filled_quantity", "price", "created_at", "decision_at",
            "trade_date", "strategy_version", "decision_round_id")})
        view.strategy_version = "another-version"
        assert not resources._binding_matches_book(roots[order.order_id], trade, fill, view, as_of=NOW)


@pytest.mark.asyncio
async def test_missing_ddl_guard_blocks_recognition(database, full_fixture):
    maker, _ = database
    await seed(maker)
    async with maker() as db, db.begin():
        await db.execute(text("DROP TRIGGER af_resource_scope_no_delete"))
    async with maker() as db:
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "blocked"
        assert list(quota.values()) == [{"status": "invalid"}]


@pytest.mark.asyncio
async def test_mutable_deferred_marker_or_exact_log_never_releases_fixed_buy_quota(database, full_fixture):
    from app.models.paper import PaperAutoTradeLog
    from app.paper.strategy_iteration_challenger import _today_buy_count
    maker, _ = database
    ids = await seed(maker)
    async with maker() as db, db.begin():
        order = await db.scalar(select(TradeOrder))
        proof = json.loads(order.risk_json)
        proof["paper_deferred_order"] = {"candidate": {"scale_in": True}}
        order.risk_json = _json(proof)
        db.add(PaperAutoTradeLog(account_id=1, run_id="unrelated.manual.marker", trade_date=AT.date(),
            created_at=NOW, code="600000", action="buy", decision="executed", executed_trade_id=ids[0][2],
            strategy_version=order.strategy_version, candidate_json=_json({"scale_in": True})))
    async with maker() as db:
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "validated"
        assert list(quota.values())[0]["scale_in"] is False
        assert await _today_buy_count(db, 1, AT.date(), as_of=NOW) == 1


@pytest.mark.asyncio
async def test_unused_large_reason_is_not_hydrated_before_recognition(database, full_fixture, monkeypatch):
    maker, _ = database
    await seed(maker)
    async with maker() as db, db.begin():
        await db.execute(text("UPDATE trade_order SET reason=:value"), {"value": "unused" * (resources.MAX_READ_BYTES // 6 + 1)})
    original = resources._binding_matches_book
    def inspect_view(binding, trade, fill, order, **kwargs):
        assert not hasattr(order, "reason") and not hasattr(order, "error_message")
        assert order.risk_json is None and fill.raw_json is None  # Identity scan, not whole proofs.
        return original(binding, trade, fill, order, **kwargs)
    monkeypatch.setattr(resources, "_binding_matches_book", inspect_view)
    async with maker() as db:
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "validated" and all(q["status"] == "verified" for q in quota.values())


@pytest.mark.asyncio
async def test_large_fixed_raw_never_parsed_before_sql_byte_gate(database, full_fixture, monkeypatch):
    from app.trading import service
    maker, _ = database
    ids = await seed(maker)
    async with maker() as db, db.begin():
        async def mutate(session):
            fill = await session.get(TradeFill, ids[0][1])
            raw = json.loads(fill.raw_json)
            raw["oversized_comment"] = "x" * (resources.MAX_READ_BYTES + 1)
            await session.execute(text("UPDATE trade_fill SET raw_json=:raw WHERE id=:id"),
                                  {"raw": _json(raw), "id": ids[0][1]})
        await damage_fixture(db, mutate)
    original = service._json_loads_dict
    def inspect_raw(raw):
        assert raw is None or len(raw.encode()) <= resources.MAX_PAYLOAD_BYTES
        return original(raw)
    monkeypatch.setattr(service, "_json_loads_dict", inspect_raw)
    async with maker() as db:
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "blocked"
        assert list(quota.values()) == [{"status": "invalid"}]


@pytest.mark.asyncio
async def test_ordinary_and_fixed_receipts_mix_without_shared_resource_allocation(database, full_fixture):
    maker, _ = database
    await seed(maker)
    async with maker() as db, db.begin():
        ordinary = await bundle(db, name="ordinary.1", sequence=2)
        order, fill, trade = ordinary[:3]
        order.order_type, order.status, order.filled_quantity = "limit", "filled", 100
        prior = {"contract_version": "immediate_paper_fill_v2_20260914", "status": "fillable",
            "mandatory": True, "account_id": "default", "code": order.code, "side": "buy",
            "fill_price": trade.price, "filled_quantity": trade.amount, "quote_round_id": trade.fill_round_id}
        timing = {"guard_version": "paper_ledger_timing_v1_20260914", "status": "validated",
            "input_contract_version": prior["contract_version"],
            "input_sha256": hashlib.sha256(_json_dumps(prior).encode()).hexdigest(),
            "quote_round_id": trade.fill_round_id, "before_mutation_checked_at": NOW.isoformat()}
        raw = json.loads(fill.raw_json)
        del raw["after_hours_fixed_execution"]
        raw.update(immediate_execution_evidence=prior, ledger_execution_timing=timing)
        fill.raw_json = _json_dumps(raw)  # Preserve old-contract JSON insertion order/hash.
    async with maker() as db:
        integrity, quota = await read_evidence(db)
        assert integrity["status"] == "validated" and integrity["checked_trade_count"] == 2
        assert all(q["status"] == "verified" for q in quota.values())
        assert await db.scalar(select(State.revision)) == 1
        assert await db.scalar(select(func.count()).select_from(Receipt)) == 1
