"""Per-order consumed-round protection, not cross-order/shared depth certification."""
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
from test_paper_orphan_fill_guard import quote
from paper_pending_fixture import accepted_frame


async def frame(factory, monkeypatch, *, round_id, committed_at, observed_at, source_at=None):
    payload = quote(round_id, committed_at)
    if source_at is not None:
        payload["records"][0]["source_quote_at"] = source_at
    async with factory() as db:
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: observed_at)
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            return await service.reconcile_paper_deferred_orders(
                db, account_id="default", round_id=round_id, now=observed_at)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("mode", ["replay", "old_clock_new_id", "equal_source", "older_source", "forward"])
async def test_completed_partial_round_must_not_be_consumed_again(
    quote_execution_env, monkeypatch, side, mode,
):
    factory = quote_execution_env
    invoke, _, _, _ = await prepare(factory, monkeypatch, "deferred-" + side)
    assert (await invoke())[0]["event"] == "partial"  # B
    second = FILL_AT + timedelta(seconds=20)
    result = await frame(factory, monkeypatch, round_id="C", committed_at=second, observed_at=second)
    assert result[0]["event"] == "partial" and result[0]["order"]["filled_quantity"] == 200
    before = await snapshot(factory)
    actual = second + timedelta(seconds=20)
    round_id = "accepted-next" if mode == "replay" else "D"
    committed = FILL_AT if mode in {"replay", "old_clock_new_id"} else actual
    source = second if mode == "equal_source" else FILL_AT if mode == "older_source" else None
    result = await frame(factory, monkeypatch, round_id=round_id,
        committed_at=committed, observed_at=actual, source_at=source)
    if mode == "forward":
        assert result[0]["event"] == "filled"
        assert result[0]["order"]["filled_quantity"] == 300
        async with factory() as db:
            fills = list((await db.scalars(select(TradeFill).order_by(TradeFill.id))).all())
            assert len(fills) == 3
            assert fills[-1].fill_round_id == "D" and fills[-1].filled_at == actual
    else:
        assert result[0]["event"] == "waiting", result
        assert "轮次" in result[0]["reason"]
        assert not result[0]["fills"]
        assert await snapshot(factory) == before
        async with factory() as db:
            order = (await db.scalars(select(TradeOrder))).one()
            assert order.status == "partial" and order.filled_quantity == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("missing", ["raw", "clock", "receipt_hash", "cumulative_quantity",
                                     "last_round", "fill_deleted", "wrong_code"])
async def test_legacy_or_damaged_partial_evidence_is_not_repaired_by_current_quote(
    quote_execution_env, monkeypatch, side, missing,
):
    factory = quote_execution_env
    invoke, _, _, _ = await prepare(factory, monkeypatch, "deferred-" + side)
    assert (await invoke())[0]["event"] == "partial"
    async with factory() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        raw = json.loads(fill.raw_json)
        if missing == "raw":
            raw.pop("pending_execution_timing")
        elif missing == "clock":
            raw["pending_execution_timing"]["quote_committed_at"] = None
        elif missing == "receipt_hash":
            raw["ledger_execution_timing"]["input_sha256"] = "mismatched-fixture"
        elif missing in {"cumulative_quantity", "last_round"}:
            order = (await db.scalars(select(TradeOrder))).one()
            if missing == "cumulative_quantity":
                order.filled_quantity = 200
            else:
                order.last_fill_round_id = "fabricated-fixture"
        elif missing == "fill_deleted":
            await db.delete(fill)
        else:
            fill.code = "600002"
        fill.raw_json = json.dumps(raw)  # explicit damaged historical FIXTURE, not production
        await db.commit()
    before = await snapshot(factory)
    later = FILL_AT + timedelta(seconds=30)
    result = await frame(factory, monkeypatch, round_id="next",
        committed_at=later, observed_at=later)
    assert result[0]["event"] == "waiting", result
    assert result[0]["fills"] == []
    assert await snapshot(factory) == before
