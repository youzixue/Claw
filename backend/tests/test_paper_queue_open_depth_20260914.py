"""Queue opening must prove quantity, not use a price-only fallback.

Real temporary SQLite books/receipts/quote timing; inherited risk mocks isolate
matching mechanics only. No production DB, route relaxation or live orders.
"""
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.trading import TradeFill, TradeOrder
from app.trading import service
from test_quote_round_execution import quote_execution_env
from test_paper_atomic_execution_20260914 import isolated_transaction_policy_and_calendar, snapshot
from test_paper_pending_clock_20260914 import prepare, FILL_AT
from test_quote_round_execution import _round_payload
from paper_pending_fixture import accepted_frame

CASES = [
    ("exact", {}, 1.0, True),
    ("surplus", {"ask1_volume": 30}, 1.0, True),
    ("multiple", {"ask1_volume": 1, "ask2_price": 10.1, "ask2_volume": 2}, 1.0, True),
    ("fraction_exact", {"ask1_volume": 6}, 0.5, True),
    ("absent", {"ask1_price": None, "ask1_volume": None}, 1.0, False),
    ("empty", {"ask1_price": 0, "ask1_volume": 0}, 1.0, False),
    ("no_hands", {"ask1_volume": None}, 1.0, False),
    ("zero_hands", {"ask1_volume": 0}, 1.0, False),
    ("negative_hands", {"ask1_volume": -1}, 1.0, False),
    ("one_hand", {"ask1_volume": 1}, 1.0, False),
    ("below_lot", {"ask1_volume": 0.99}, 1.0, False),
    ("below_total", {"ask1_volume": 2.99}, 1.0, False),
    ("fraction_insufficient", {"ask1_volume": 5.99}, 0.5, False),
    ("fraction_per_level", {"ask1_volume": 3, "ask2_price": 10.1, "ask2_volume": 3}, 0.5, False),
    ("crossed", {"bid1_price": 10.01}, 1.0, False),
    ("locked", {"bid1_price": 10}, 1.0, False),
    ("duplicate", {"ask2_price": 10, "ask2_volume": 100}, 1.0, False),
    ("unordered", {"ask2_price": 9.9, "ask2_volume": 100}, 1.0, False),
    ("over_limit", {"ask1_price": 11.01}, 1.0, False),
    ("missing_down", {"limit_down": None}, 1.0, False),
    ("missing_up", {"limit_up": None}, 1.0, False),
    ("nan_ask", {"ask1_volume": float("nan")}, 1.0, False),
    ("inf_ask", {"ask1_volume": float("inf")}, 1.0, False),
    ("inf_price", {"ask2_price": float("inf"), "ask2_volume": 1}, 1.0, False),
    ("bad_bid", {"bid2_price": 9.98, "bid2_volume": -1}, 1.0, False),
    ("zero_ratio", {}, 0.0, False),
    ("negative_ratio", {}, -1.0, False),
    ("nan_ratio", {}, float("nan"), False),
    ("inf_ratio", {}, float("inf"), False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,changes,ratio,fillable", CASES, ids=[case[0] for case in CASES])
async def test_queue_open_requires_full_valid_depth(
    quote_execution_env, monkeypatch, label, changes, ratio, fillable,
):
    factory = quote_execution_env
    # Participation is part of the ORIGINAL strategy version, not changed mid-order.
    monkeypatch.setattr(paper.settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", ratio)
    invoke, _, payload, order_id = await prepare(factory, monkeypatch, "queue-open")
    record = payload["records"][0]
    record.update(ask1_price=10.0, ask1_volume=3, bid1_price=9.99, bid1_volume=3)
    record.update(changes)
    before = await snapshot(factory)
    result = (await invoke())[0]
    if not fillable:
        assert result["event"] == "waiting", result
        assert result["fills"] == []
        assert await snapshot(factory) == before
        async with factory() as db:
            order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == order_id))
            assert order.status == "submitted" and order.filled_quantity == 0
        return
    assert result["event"] == "filled", result
    # Keep original pessimistic limit, not lower quote VWAP / last price.
    assert result["order"]["filled_quantity"] == 300
    assert result["order"]["avg_fill_price"] == 11.0
    async with factory() as db:
        row = (await db.scalars(select(TradeFill))).one()
        raw = json.loads(row.raw_json)
        timing = raw["pending_execution_timing"]
        proof = timing["queue_open_depth"]
        assert proof["contract_version"] == "queue_open_depth_v1_20260914"
        assert proof["scope"] == "one_order_visible_depth_not_shared_capacity"
        assert proof["status"] == "fillable"
        assert proof["visible_fillable_quantity"] == proof["requested_quantity"] == row.quantity == 300
        assert sum(item["quantity"] for item in proof["depth_levels"]) == 300
        assert proof["depth_vwap"] <= proof["fill_price"] == row.price == 11.0
        assert proof["partial_fill_allowed"] is False
        assert proof["participation_ratio"] == ratio
        assert timing["quote_round_id"] == row.fill_round_id == payload["round_id"]
        assert raw["ledger_execution_timing"]["input_sha256"] == hashlib.sha256(
            service._json_dumps(timing).encode()).hexdigest()


@pytest.mark.asyncio
async def test_insufficient_open_waits_for_new_round_then_fills_once(quote_execution_env, monkeypatch):
    factory = quote_execution_env
    invoke, clock, payload, order_id = await prepare(factory, monkeypatch, "queue-open")
    payload["records"][0]["ask1_volume"] = 1
    before = await snapshot(factory)
    first = (await invoke())[0]
    assert first["event"] == "waiting"
    assert await snapshot(factory) == before
    async with factory() as db:
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == order_id))
        rejected = json.loads(order.risk_json)["paper_limit_up_queue"]["open_depth_evaluation"]
        assert rejected["status"] == "waiting" and rejected["visible_fillable_quantity"] == 100
        assert rejected["quote_round_id"] == payload["round_id"]
    later = FILL_AT + timedelta(seconds=30)
    next_frame = _round_payload("next-full-opening", later, [
        dict(code="600001", price=10, ask1_price=10, ask1_volume=3,
             bid1_price=9.99, bid1_volume=3, volume=1000, limit_up=11)])
    clock[0] = later
    async with factory() as db:
        await accepted_frame(db, next_frame)
        token = paper._QUOTE_ROUND_CONTEXT.set(next_frame)
        try:
            result = (await service.reconcile_paper_limit_up_orders(db, account_id="default", now=later))[0]
            assert result["event"] == "filled"
            assert result["order"]["filled_quantity"] == 300
            assert result["order"]["avg_fill_price"] == 11.0
            after = await snapshot(factory)
            assert await service.reconcile_paper_limit_up_orders(db, account_id="default", now=later) == []
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert await snapshot(factory) == after
    assert len(after["trade_fill"]) == 1


@pytest.mark.asyncio
async def test_book_await_cannot_mutate_the_frozen_open_depth_proof(quote_execution_env, monkeypatch):
    from app.trading import paper_authorization as authorization

    invoke, _, payload, _ = await prepare(quote_execution_env, monkeypatch, "queue-open")
    real = paper._refresh_account
    hit = []
    async def mutable_context(*args, **kwargs):
        result = await real(*args, **kwargs)
        scope = authorization._SCOPE.get()
        if scope is not None and scope.stage == "ledger":
            hit.append(True)
            payload["records"][0]["ask1_volume"] = 0
        return result
    monkeypatch.setattr(paper, "_refresh_account", mutable_context)
    assert (await invoke())[0]["event"] == "filled"
    assert hit and payload["records"][0]["ask1_volume"] == 0
    async with quote_execution_env() as db:
        row = (await db.scalars(select(TradeFill))).one()
        raw = json.loads(row.raw_json)
        frozen = raw["pending_execution_timing"]
        assert frozen["queue_open_depth"]["depth_levels"] == [{"level": 1, "price": 10.0, "quantity": 300}]
        assert frozen["queue_open_depth"]["visible_fillable_quantity"] == 300
        assert raw["ledger_execution_timing"]["input_sha256"] == hashlib.sha256(
            service._json_dumps(frozen).encode()).hexdigest()
