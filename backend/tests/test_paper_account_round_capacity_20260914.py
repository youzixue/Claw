"""Independent simulated accounts, shared depth only WITHIN an account/round.

Entry-risk mocks isolate liquidity/accounting. Real local quote validation, book,
receipts, lock and SQLite commit/rollback remain enabled; no production access.
"""
import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.trading import TradeFill, TradeOrder
from app.trading import service
from app.trading.broker import PaperBrokerAdapter
from test_quote_round_execution import quote_execution_env, _round_payload
from test_paper_atomic_execution_20260914 import isolated_transaction_policy_and_calendar, snapshot
from test_paper_orphan_fill_guard import AT, seed, identity, quote
from paper_pending_fixture import accepted_frame

PATHS = ["immediate-buy", "immediate-sell", "deferred-buy", "deferred-sell", "queue-buy"]
FILL_AT = AT + timedelta(seconds=30)


@pytest.fixture(autouse=True)
def per_test_lock(monkeypatch):
    monkeypatch.setattr(paper, "_TRADE_LOCK", asyncio.Lock())


async def prepare(factory, monkeypatch, paths, *, hands=1, accounts=None, quantities=None):
    accounts = accounts or ["default"] * len(paths)
    quantities = quantities or [100] * len(paths)
    payload = _round_payload("shared-depth-frame", FILL_AT, [
        dict(code="600001", price=10, ask1_price=10.01, ask1_volume=hands,
             bid1_price=10, bid1_volume=hands, volume=1000, limit_up=11)])
    commands = []
    seeded = set()
    for index, (path, account, quantity) in enumerate(zip(paths, accounts, quantities)):
        mode, side = path.split("-")
        async with factory() as db:
            if (account, side) not in seeded:
                await seed(db, account, side)
                seeded.add((account, side))
            strategy, source, signal = identity(account, side)
            command = service.SubmitOrderCommand(code="600001", side=side,
                price=11 if side == "buy" else 10, quantity=quantity, account_id=account,
                decision_at=FILL_AT if mode == "immediate" else AT,
                signal_id=signal+str(index), idempotency_key="capacity-"+str(index),
                strategy_id="" if mode == "immediate" else strategy, source=source,
                queue_if_limit_up=mode=="queue", defer_until_next_round=mode=="deferred",
                queue_metadata={"cancel_time":"14:50","block_warn":True},
                deferred_metadata={"block_warn":side=="buy"})
            if mode != "immediate":
                token = paper._QUOTE_ROUND_CONTEXT.set(quote("decision", AT, queued=mode=="queue"))
                try:
                    submitted = await service.submit_order(db, command)
                    assert submitted["order"]["status"] == "submitted", submitted
                finally:
                    paper._QUOTE_ROUND_CONTEXT.reset(token)
            commands.append(command)
    async with factory() as db:
        await accepted_frame(db, payload)
    monkeypatch.setattr(paper, "_public_order_clock", lambda:FILL_AT)
    async def invoke(index):
        command = commands[index]
        mode = paths[index].split("-")[0]
        async with factory() as db:
            token = paper._QUOTE_ROUND_CONTEXT.set(payload)
            try:
                if mode == "immediate":
                    return [await service.submit_order(db, command)]
                if mode == "queue":
                    return await service.reconcile_paper_limit_up_orders(db, account_id=command.account_id, now=FILL_AT)
                return await service.reconcile_paper_deferred_orders(
                    db, account_id=command.account_id, round_id=payload["round_id"], now=FILL_AT)
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)
    return invoke, payload, commands


def fill_count(results):
    return sum(len(result["fills"]) for result in results)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_same_account_round_cannot_fill_two_orders_from_one_lot(
    quote_execution_env, monkeypatch, path,
):
    factory = quote_execution_env
    invoke, _, _ = await prepare(factory, monkeypatch, [path, path])
    first = await invoke(0)
    second = await invoke(1)
    assert fill_count(first) + fill_count(second) == 1
    async with factory() as db:
        fills = (await db.scalars(select(TradeFill))).all()
        assert len(fills) == 1 and fills[0].quantity == 100
        orders = (await db.scalars(select(TradeOrder))).all()
        assert sorted(order.filled_quantity for order in orders) == [0,100]
        assert any("容量" in (order.error_message or "") for order in orders)


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [
    ("immediate-buy","deferred-buy"),("deferred-buy","immediate-buy"),
    ("immediate-buy","queue-buy"),("queue-buy","immediate-buy"),
    ("deferred-buy","queue-buy"),("queue-buy","deferred-buy"),
])
async def test_immediate_deferred_and_open_queue_share_account_capacity(
    quote_execution_env, monkeypatch, first, second,
):
    invoke, _, _ = await prepare(quote_execution_env, monkeypatch, [first, second])
    assert fill_count(await invoke(0)) == 1
    before = await snapshot(quote_execution_env)
    assert fill_count(await invoke(1)) == 0
    assert await snapshot(quote_execution_env) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_spare_same_level_capacity_is_available_but_not_unlimited(
    quote_execution_env, monkeypatch, path,
):
    invoke, _, _ = await prepare(quote_execution_env, monkeypatch, [path]*3, hands=2)
    results = []
    for i in range(3):
        results.extend(await invoke(i))
    assert fill_count(results) == 2
    async with quote_execution_env() as db:
        rows = (await db.scalars(select(TradeFill))).all()
        assert sum(row.quantity for row in rows) == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_other_strategy_account_has_its_own_counterfactual_capacity(
    quote_execution_env, monkeypatch, path,
):
    invoke, _, _ = await prepare(quote_execution_env, monkeypatch, [path,path],
        accounts=["default","promotion"])
    assert fill_count(await invoke(0)) == 1
    assert fill_count(await invoke(1)) == 1
    async with quote_execution_env() as db:
        rows = (await db.scalars(select(TradeFill))).all()
        assert len(rows) == 2
        for row in rows:
            raw = json.loads(row.raw_json)
            p = raw.get("immediate_execution_evidence") or raw["pending_execution_timing"]
            capacity = p["account_round_depth"]
            assert capacity["scope"] == "independent_account_round_code_side"
            assert capacity["status"] == "validated"
            assert capacity["prior_fill_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy","sell"])
async def test_concurrent_immediate_orders_do_not_both_consume_the_same_lot(
    quote_execution_env, monkeypatch, side,
):
    invoke, _, _ = await prepare(quote_execution_env, monkeypatch, ["immediate-"+side]*2)
    results = await asyncio.gather(invoke(0), invoke(1))
    assert sum(fill_count(result) for result in results) == 1
    async with quote_execution_env() as db:
        assert len((await db.scalars(select(TradeFill))).all()) == 1


@pytest.mark.asyncio
async def test_rolled_back_fill_does_not_reserve_capacity(quote_execution_env, monkeypatch):
    invoke, _, _ = await prepare(quote_execution_env, monkeypatch, ["immediate-buy"]*2)
    real = PaperBrokerAdapter.place_order
    hits = []
    async def failed(self, db, req):
        result = await real(self, db, req)
        assert result.fills
        hits.append(True)
        raise ValueError("failure after real book before atomic rollback")
    with monkeypatch.context() as patch:
        patch.setattr(PaperBrokerAdapter, "place_order", failed)
        assert fill_count(await invoke(0)) == 0
    assert hits
    assert fill_count(await invoke(1)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    "raw_missing", "capacity_missing", "bad_sha", "receipt_quantity",
    "same_round_book_changed", "order_account_changed", "receipt_identity",
])
async def test_inconsistent_prior_receipt_cannot_release_capacity(
    quote_execution_env, monkeypatch, invalid,
):
    factory = quote_execution_env
    invoke, payload, _ = await prepare(factory, monkeypatch, ["immediate-buy"]*2, hands=2)
    assert fill_count(await invoke(0)) == 1
    async with factory() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        raw = json.loads(fill.raw_json)
        if invalid == "raw_missing":
            fill.raw_json = "{}"
        elif invalid == "capacity_missing":
            raw["immediate_execution_evidence"].pop("account_round_depth")
            fill.raw_json = service._json_dumps(raw)
        elif invalid == "bad_sha":
            raw["ledger_execution_timing"]["input_sha256"] = "damaged"
            fill.raw_json = service._json_dumps(raw)
        elif invalid == "receipt_quantity":
            fill.quantity = 200
        elif invalid == "receipt_identity":
            fill.fill_id = "wrong-original-identity"
        elif invalid == "order_account_changed":
            order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id==fill.order_id))
            order.account_id = "promotion"
        else:
            payload["records"][0]["ask1_volume"] = 3
        await db.commit()  # Deliberate corruption in temporary fixture ONLY.
    before = await snapshot(factory)
    result = await invoke(1)
    assert fill_count(result) == 0 and "容量" in result[0]["order"]["error_message"]
    assert await snapshot(factory) == before


@pytest.mark.asyncio
async def test_buy_and_sell_use_distinct_sides_of_one_frame(quote_execution_env, monkeypatch):
    invoke, _, _ = await prepare(quote_execution_env, monkeypatch, ["immediate-sell","immediate-buy"])
    assert fill_count(await invoke(0)) == fill_count(await invoke(1)) == 1


@pytest.mark.asyncio
async def test_new_real_round_can_supply_new_capacity(quote_execution_env, monkeypatch):
    factory = quote_execution_env
    invoke, _, _ = await prepare(factory, monkeypatch, ["immediate-buy"])
    assert fill_count(await invoke(0)) == 1
    later = FILL_AT + timedelta(seconds=30)
    next_frame = _round_payload("next-real-capacity", later, [
        dict(code="600001",price=10,ask1_price=10.01,ask1_volume=1,
             bid1_price=10,bid1_volume=1,volume=1005,limit_up=11)])
    monkeypatch.setattr(paper, "_public_order_clock", lambda:later)
    async with factory() as db:
        await accepted_frame(db, next_frame)
        token = paper._QUOTE_ROUND_CONTEXT.set(next_frame)
        try:
            result = await service.submit_order(db, service.SubmitOrderCommand(
                code="600001",side="buy",price=11,quantity=100,decision_at=later,
                account_id="default",idempotency_key="new-real-round"))
            assert len(result["fills"]) == 1
            assert result["risk"]["paper_account_round_depth"]["prior_fill_count"] == 0
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_guard_cannot_run_outside_transaction(quote_execution_env):
    from fastapi import HTTPException
    from app.trading.paper_depth_capacity import account_round_depth_evidence
    async with quote_execution_env() as db:
        with pytest.raises(HTTPException) as error:
            await account_round_depth_evidence(db, None, {})
        assert error.value.status_code == 403
