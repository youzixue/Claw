"""Prevent relabeling context A as explicit fill B; real isolated SQLite books.

Risk/calendar fixture isolates this identity test, not a complete quote acceptance.
"""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1 import paper
from app.models.trading import TradeOrder
from app.trading import service
from paper_pending_fixture import accepted_frame
from test_quote_round_execution import quote_execution_env
from test_paper_orphan_fill_guard import AT, place_deferred, quote
from test_paper_atomic_execution_20260914 import isolated_transaction_policy_and_calendar, snapshot


async def orders(factory):
    async with factory() as db:
        return [tuple(row) for row in (await db.execute(
            select(*TradeOrder.__table__.columns).order_by(TradeOrder.id))).all()]


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("actual", ["decision", "actual-next"])
async def test_explicit_round_cannot_replace_actual_context_round(
    quote_execution_env, side, actual,
):
    factory = quote_execution_env
    await place_deferred(factory, paper.PAPER_ACCOUNT_DEFAULT, side)
    before, before_orders = await snapshot(factory), await orders(factory)
    at = AT+timedelta(seconds=30)
    async with factory() as db:
        token = paper._QUOTE_ROUND_CONTEXT.set(quote(actual, at))
        try:
            with pytest.raises(HTTPException) as error:
                await service.reconcile_paper_deferred_orders(
                    db, account_id=paper.PAPER_ACCOUNT_DEFAULT, round_id="forged-next", now=at)
            assert error.value.status_code == 409
            assert "轮次" in error.value.detail and "不一致" in error.value.detail
            assert not db.in_transaction()  # fail before any order/ledger query or write
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert await snapshot(factory) == before
    assert await orders(factory) == before_orders


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("provided", ["actual-next", ""])
async def test_matching_or_omitted_explicit_round_preserves_actual_fill_identity(
    quote_execution_env, monkeypatch, side, provided,
):
    factory = quote_execution_env
    await place_deferred(factory, paper.PAPER_ACCOUNT_DEFAULT, side)
    at = AT+timedelta(seconds=30)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    async with factory() as db:
        payload = quote("actual-next", at)
        await accepted_frame(db, payload)
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            result = await service.reconcile_paper_deferred_orders(
                db, account_id=paper.PAPER_ACCOUNT_DEFAULT, round_id=provided, now=at)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert result[0]["event"] == "partial"
        assert result[0]["order"]["decision_round_id"] == "decision"
        assert result[0]["fills"][0]["fill_round_id"] == "actual-next"
        assert result[0]["fills"][0]["decision_round_id"] == "decision"


@pytest.mark.asyncio
async def test_expiration_only_does_not_require_fill_round_binding(quote_execution_env):
    factory = quote_execution_env
    await place_deferred(factory, paper.PAPER_ACCOUNT_DEFAULT, "buy")
    before = await snapshot(factory)
    async with factory() as db:
        token = paper._QUOTE_ROUND_CONTEXT.set(quote("old-context", AT))
        try:
            result = await service.reconcile_paper_deferred_orders(
                db, account_id=paper.PAPER_ACCOUNT_DEFAULT, round_id="maintenance",
                now=AT+timedelta(seconds=30), expire_only=True)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert result == []  # this fixture has no automatic pending-buy validity
    assert await snapshot(factory) == before
