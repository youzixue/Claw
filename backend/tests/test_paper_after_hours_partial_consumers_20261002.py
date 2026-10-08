"""Read-only ACTUAL partial receipt recognition in isolated synthetic future roots.

No partial broker/book/risk authorization, live provider, deployment or execution
is asserted. The future scope is borrowed only from the temporary kernel fixture.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update, func, text, event

from app.api.v1 import paper
from app.data.after_hours import _json
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.paper.position_policy import buy_order_evidence
from app.paper.strategy_iteration_challenger import _today_buy_count
from app.trading import paper_after_hours_resources as resources, paper_authorization as auth, paper_after_hours_allocation as allocation
from app.trading.paper_execution_integrity import account_execution_integrity_evidence
from test_paper_after_hours_partial_resources_20261002 import database, persist, stage, ORDER_FIELDS, State, Receipt
from test_paper_after_hours_partial_book_20261002 import second_and_third, book
from test_paper_after_hours_partial_contract_20261002 import original, freeze
from test_paper_after_hours_partial_allocation_20261002 import capacity, append_offer, simulated_state
from test_paper_after_hours_partial_fees_20261002 import fee_model, sell, selling_feed
from test_paper_after_hours_allocation_20261002 import AT, NOW, audited_fixture
from test_paper_after_hours_consumers_20261002 import damage_fixture


LATE = AT + timedelta(days=1)


async def seed(maker, monkeypatch, count=3):
    items = second_and_third()[:count]
    for item in items:
        monkeypatch.setattr(paper, "_public_order_clock", lambda: item[4].trade_time)
        async with maker() as db, db.begin():
            await persist(db, item)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: LATE)
    return items


async def recognize(db, cutoff=LATE):
    return await resources._verified_partial_receipt_bindings(db, account_numeric_id=7,
        code="600000", trade_date=AT.date(), cutoff=cutoff)


async def consumers(db, cutoff=LATE, side="buy"):
    trades = list((await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all())
    token = auth._PAPER_TRANSACTION.set((db, asyncio.current_task()))
    try:
        following = SimpleNamespace(order_id="following", account_id="default", code="600000",
            side=side, trade_date=AT.date())
        integrity = await account_execution_integrity_evidence(db, following, {"quote_round_id":"following.round"})
    finally:
        auth._PAPER_TRANSACTION.reset(token)
    return integrity, await buy_order_evidence(db, account_id=7, trades=trades, as_of=cutoff)


@pytest.mark.asyncio
async def test_three_actual_fragments_recognized_by_fill_but_one_original_quota(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    items = await seed(maker, monkeypatch)
    async with maker() as db:
        before = await db.scalar(select(State.revision)), await db.scalar(select(PaperAccount.current_capital))
        bindings = await recognize(db)
        assert set(bindings) == {item[3].fill_id for item in items}
        assert {binding["order_id"] for binding in bindings.values()} == {"head"}
        assert sum(binding["quantity"] for binding in bindings.values()) == 300
        with pytest.raises(ValueError):  # The old explicit full contract is NOT relaxed.
            await resources._verified_receipt_bindings(db, account_numeric_id=7,
                code="600000", trade_date=AT.date(), cutoff=LATE)
        integrity, quota = await consumers(db)
        assert integrity["status"] == "validated"
        assert integrity["checked_trade_count"] == integrity["checked_receipt_count"] == 3
        assert list(quota.values()) == [{"status":"verified", "order_id":"head", "scale_in":False}] * 3
        assert await _today_buy_count(db, 7, AT.date(), as_of=LATE) == 1
        assert before == (await db.scalar(select(State.revision)), await db.scalar(select(PaperAccount.current_capital)))
        assert not db.new and not db.dirty and not db.deleted


@pytest.mark.asyncio
async def test_historical_fees_do_not_access_current_settings_or_live_provider(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    await seed(maker, monkeypatch)
    from app.trading import paper_after_hours_execution as execution, paper_after_hours_allocation as allocation
    def no_current_policy():
        raise AssertionError("historical recognition read current fee policy")
    monkeypatch.setattr(execution, "_partial_fee_parameters", no_current_policy)
    monkeypatch.setattr(allocation, "_PROVIDERS", {})  # Historical receipt is not a new quote.
    async with maker() as db:
        assert len(await recognize(db)) == 3
        integrity, quota = await consumers(db)
        assert integrity["status"] == "validated" and all(p["status"] == "verified" for p in quota.values())


@pytest.mark.asyncio
async def test_canceled_remainder_preserves_actual_fragments_and_cannot_acquire_scale_in(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    await seed(maker, monkeypatch, count=2)
    async with maker() as db, db.begin():
        order = await db.scalar(select(TradeOrder))
        risk = json.loads(order.risk_json)
        risk["paper_deferred_order"] = {"candidate":{"scale_in":True}}
        # Cancellation projection is deliberately not a matching/fee authority.
        await db.execute(update(TradeOrder).values(status="canceled", risk_json=_json(risk)))
    async with maker() as db:
        assert len(await recognize(db)) == 2
        integrity, quota = await consumers(db)
        assert integrity["status"] == "validated"
        assert list(quota.values()) == [{"status":"verified", "order_id":"head", "scale_in":False}] * 2
        assert await _today_buy_count(db, 7, AT.date(), as_of=LATE) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["delete_receipt", "delete_fill", "root_revision", "root_prefix", "root_clock_backwards",
    "unknown_protocol", "mixed_full_protocol", "candidate_protocol", "structure_only", "fee", "intent_hash", "future_intent",
    "projection_zero", "raw_duplicate_key", "extra_fill", "missing_peer", "moved_account"])
async def test_whole_root_damage_never_returns_partial_verified_history(database, audited_fixture, fee_model, monkeypatch, damage):
    maker, _ = database
    await seed(maker, monkeypatch)
    async with maker() as db, db.begin():
        async def mutate(session):
            receipt = await session.get(Receipt, 1)
            fill = await session.get(TradeFill, 1)
            order = await session.scalar(select(TradeOrder))
            if damage == "delete_receipt":
                await session.execute(text("DELETE FROM paper_after_hours_resource_receipt WHERE id=1"))
            elif damage == "delete_fill":
                await session.delete(fill)
            elif damage == "root_revision":
                await session.execute(update(State).values(revision=4))
            elif damage == "root_prefix":
                await session.execute(update(State).values(lifecycle_prefix_hash="0"*64))
            elif damage == "root_clock_backwards":
                await session.execute(update(State).values(checked_at=AT+timedelta(milliseconds=200)))
            elif damage in {"unknown_protocol", "mixed_full_protocol"}:
                await session.execute(update(Receipt).where(Receipt.id == 1).values(protocol_version=
                    "unknown" if damage == "unknown_protocol" else resources.CONSUMPTION_VERSION))
            elif damage in {"candidate_protocol", "structure_only", "fee"}:
                raw = json.loads(fill.raw_json)
                if damage == "candidate_protocol":
                    from app.trading import paper_after_hours_execution as execution
                    raw["after_hours_fixed_execution"]["contract_version"] = execution.PARTIAL_CANDIDATE_VERSION
                elif damage == "structure_only":
                    raw = {}
                else:
                    raw["after_hours_fixed_execution"]["fee_preview"]["model"]["minimum_commission"] = 6.0
                fill.raw_json = _json(raw)
            elif damage in {"intent_hash", "future_intent"}:
                risk = json.loads(order.risk_json)
                risk["paper_after_hours_intent"]["requested_at" if damage == "intent_hash" else "accepted_at"] = LATE.isoformat()
                order.risk_json = _json(risk)
            elif damage == "projection_zero":
                order.filled_quantity = 0
            elif damage == "raw_duplicate_key":
                fill.raw_json = fill.raw_json[:-1] + ',"id":101}'
            elif damage == "extra_fill":
                fill.id = 99
                # Old receipt still references its original id; also retain orphan count.
            elif damage == "missing_peer":
                session.add(TradeOrder(order_id="missing.peer", broker="paper", account_id="default",
                    order_type="after_hours_fixed", code="600000", side="buy", quantity=100, price=10.5,
                    status="filled", filled_quantity=100, trade_date=AT.date()))
            else:
                trade = await session.get(PaperTradeLog, 101)
                trade.account_id = 8
            await session.flush()
        await damage_fixture(db, mutate)
    async with maker() as db:
        with pytest.raises((ValueError, KeyError)):
            await recognize(db)
        integrity, quota = await consumers(db)
        assert integrity["status"] == "blocked"
        assert all(p["status"] != "verified" for p in quota.values())
        assert not db.new and not db.dirty and not db.deleted


@pytest.mark.asyncio
async def test_json_only_without_consumption_cannot_recognize_or_release_quota(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    item = second_and_third()[0]
    async with maker() as db, db.begin():
        order, *_ = await stage(db, item)
        order.status, order.filled_quantity = "partial", 100
    monkeypatch.setattr(paper, "_public_order_clock", lambda: LATE)
    async with maker() as db:
        with pytest.raises(ValueError):
            await recognize(db)
        integrity, quota = await consumers(db)
        assert integrity["status"] == "blocked" and list(quota.values()) == [{"status":"invalid"}]


@pytest.mark.asyncio
async def test_pending_unrelated_rows_not_autoflushed_by_recognition(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    await seed(maker, monkeypatch)
    async with maker() as db:
        pending = TradeOrder(order_id="pending.unrelated", code="000001", side="buy", quantity=100, price=10)
        db.add(pending)
        assert len(await recognize(db)) == 3 and pending in db.new
        with db.no_autoflush:
            assert await db.scalar(select(func.count()).select_from(TradeOrder).where(TradeOrder.order_id == pending.order_id)) == 0
        db.expunge(pending)


@pytest.mark.asyncio
async def test_final_root_recheck_not_first_read_is_required(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    await seed(maker, monkeypatch)
    real, count = resources._book_row, 0
    async def changed(db, model, *args, **kwargs):
        nonlocal count
        row = await real(db, model, *args, **kwargs)
        if model is State:
            count += 1
            if count == 2:
                row.revision += 1
        return row
    monkeypatch.setattr(resources, "_book_row", changed)
    async with maker() as db:
        with pytest.raises(ValueError, match="root_changed_during_read"):
            await recognize(db)
        assert count == 2


@pytest.mark.asyncio
async def test_all_history_and_final_root_share_exact_utf8_budget(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    await seed(maker, monkeypatch)
    real, read_bytes = resources._book_row, 0
    async def measure(*args, **kwargs):
        nonlocal read_bytes
        row = await real(*args, **kwargs)
        read_bytes += row._read_text_bytes if row else 0
        return row
    monkeypatch.setattr(resources, "_book_row", measure)
    async with maker() as db:
        assert len(await recognize(db)) == 3
    async with maker() as db:
        # Include guard names/DDL and the initial account-name query, not just books.
        overhead = await resources._verify_sqlite_guards(db)
        overhead += len((await db.scalar(select(PaperAccount.account_name).where(PaperAccount.id == 7))).encode())
    budget = read_bytes + overhead
    monkeypatch.setattr(resources, "_book_row", real)
    monkeypatch.setattr(resources, "MAX_READ_BYTES", budget)
    async with maker() as db:
        assert len(await recognize(db)) == 3
    monkeypatch.setattr(resources, "MAX_READ_BYTES", budget-1)
    async with maker() as db:
        with pytest.raises(ValueError, match="budget"):
            await recognize(db)


async def rebind_original(session, *, quantity=300, limit=10.5):
    """Self-consistent corrupted TEMP rows, not an authorization/signing API."""
    from app.trading import paper_after_hours_execution as execution
    order = await session.scalar(select(TradeOrder))
    fill = await session.scalar(select(TradeFill))
    receipt = await session.scalar(select(Receipt))
    order.quantity, order.price = quantity, limit
    risk = json.loads(order.risk_json)
    risk["paper_after_hours_intent"].update(original_quantity=quantity, original_limit_price=limit)
    order.risk_json = _json(risk)
    payload = json.loads(receipt.payload_json)
    contract, timing, proposal = payload["execution"], payload["ledger_timing"], payload["allocation"]
    proposal.update(allocation._partial_identity(order, risk["paper_after_hours_intent"]),
        remaining_quantity_after=quantity-100)
    proposal["proposal_id"] = allocation._hash({k:v for k,v in proposal.items() if k != "proposal_id"})
    req = execution._partial_request_identifier(7, proposal)
    contract.update(allocation=proposal, original_quantity=quantity, original_limit_price=limit,
        remaining_quantity_after=quantity-100, local_intent_hash=proposal["local_intent_hash"],
        request_id=req, expected_fill_id="fill-"+req)
    timing.update(request_id=req, remaining_quantity_after=quantity-100, input_sha256=resources._digest(contract))
    fill.fill_id = "fill-"+req
    raw = json.loads(fill.raw_json)
    raw.update(after_hours_fixed_execution=contract, ledger_execution_timing=timing)
    fill.raw_json = _json(raw)
    payload.update(execution=contract, execution_json=_json(contract), allocation=proposal,
        ledger_timing=timing, request_id=req, allocation_id=proposal["proposal_id"],
        original_quantity=quantity, remaining_quantity_after=quantity-100)
    await session.execute(update(Receipt).where(Receipt.id == receipt.id).values(
        allocation_id=proposal["proposal_id"], payload_json=_json(payload), content_hash=resources._digest(payload)))
    await session.flush()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["buy_limit", "original_odd_lot"])
async def test_self_consistent_economic_rows_still_require_valid_limit_and_original_lot(database, audited_fixture, fee_model, monkeypatch, damage):
    maker, _ = database
    await seed(maker, monkeypatch, count=1)
    async with maker() as db, db.begin():
        await damage_fixture(db, lambda session: rebind_original(session,
            limit=9.0 if damage == "buy_limit" else 10.5,
            quantity=150 if damage == "original_odd_lot" else 300))
    async with maker() as db:
        # Original odd quantities now fail at the shared FIFO declaration gate,
        # before the actual-book limit check. Keep both rejection paths precise.
        expected = ("unsupported original fixed-price declaration quantity"
                    if damage == "original_odd_lot" else "limit_or_original_quantity")
        with pytest.raises(ValueError, match=expected):
            await recognize(db)
        integrity, quota = await consumers(db)
        assert integrity["status"] == "blocked" and list(quota.values()) == [{"status":"invalid"}]


@pytest.mark.asyncio
async def test_unrelated_giant_trigger_is_not_loaded_by_schema_verifier(database, audited_fixture, fee_model, monkeypatch):
    maker, engine = database
    await seed(maker, monkeypatch, count=1)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TRIGGER unrelated_big_trigger AFTER INSERT ON stock_spot "
            "BEGIN /*" + "x" * (resources.MAX_PAYLOAD_BYTES+1) + "*/ SELECT 1; END"))
    selects = []
    def observe(conn, cursor, statement, parameters, context, many):
        if "sqlite_master" in statement and statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with maker() as db:
            assert len(await recognize(db)) == 1
        schema_queries = [q for q in selects if "type='trigger'" in q]
        assert schema_queries and all("name IN (" in q for q in schema_queries)
        assert any("CASE WHEN" in q for q in schema_queries)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)


@pytest.mark.asyncio
async def test_oversized_required_guard_rejected_before_ddl_text_hydration(database, audited_fixture, fee_model, monkeypatch):
    maker, engine = database
    await seed(maker, monkeypatch, count=1)
    async with engine.begin() as conn:
        name, ddl = (await conn.execute(text("SELECT name, sql FROM sqlite_master "
            "WHERE name='af_resource_receipt_no_delete'"))).one()
        await conn.execute(text(f'DROP TRIGGER "{name}"'))
        await conn.execute(text(ddl.replace("BEGIN", "BEGIN /*"+"x"*(resources.MAX_PAYLOAD_BYTES+1)+"*/", 1)))
    async with maker() as db:
        with pytest.raises(ValueError, match="schema_byte_budget"):
            await recognize(db)


@pytest.mark.asyncio
async def test_two_complete_originals_share_one_root_but_not_one_order_binding(database, audited_fixture, fee_model, monkeypatch):
    maker, _ = database
    head, later = original(quantity=100), original("later", quantity=100, sequence=2)
    payload = capacity(200)
    first = book(freeze(payload, head, context=[head,later]))
    second = book(freeze(payload, later, context=[simulated_state(head,100),later],
        priors=[json.loads(first[0].contract_json)["allocation"]]), sequence=2)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: NOW)
    async with maker() as db, db.begin():
        db.add(TradeOrder(**{key:getattr(later,key) for key in ORDER_FIELDS}))
        await persist(db,first)
    async with maker() as db, db.begin():
        await persist(db,second)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: LATE)
    async with maker() as db:
        bindings = await recognize(db)
        assert {b["order_id"] for b in bindings.values()} == {"head","later"}
        assert len(bindings) == 2 and await db.scalar(select(func.count()).select_from(State)) == 1
        integrity,quota = await consumers(db)
        assert integrity["status"] == "validated"
        assert {p["order_id"] for p in quota.values()} == {"head","later"}
        assert all(p["status"] == "verified" and p["scale_in"] is False for p in quota.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("incompatible_limit", [False, True])
async def test_sell_partial_receipt_uses_own_tax_and_limit_direction(database, audited_fixture, fee_model, monkeypatch, incompatible_limit):
    maker, _ = database
    item = book(freeze(selling_feed(), sell(original())))
    monkeypatch.setattr(paper, "_public_order_clock", lambda: item[4].trade_time)
    async with maker() as db, db.begin():
        await persist(db,item)
    if incompatible_limit:
        async with maker() as db, db.begin():
            await damage_fixture(db, lambda session: rebind_original(session,limit=11.0))
    monkeypatch.setattr(paper, "_public_order_clock", lambda: LATE)
    async with maker() as db:
        if incompatible_limit:
            with pytest.raises(ValueError,match="limit_or_original_quantity"):
                await recognize(db)
        else:
            binding = next(iter((await recognize(db)).values()))
            assert binding["side"] == "sell" and item[4].tax > 0
        integrity,quota = await consumers(db,side="sell")
        assert integrity["status"] == ("blocked" if incompatible_limit else "validated")
        assert list(quota.values()) == [{"status":"invalid"}]  # Selling cannot create buy quota evidence.


