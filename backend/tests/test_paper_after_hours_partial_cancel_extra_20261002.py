"""Adversarial expiry/cancellation observations; temporary DB / research proposals only."""
import json
from copy import deepcopy
from datetime import timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.api.v1 import paper
from app.models.trading import TradeOrder
from app.trading import service
from app.trading import paper_after_hours_allocation as allocation
from app.trading.paper_after_hours_execution import CONTRACT, _partial_request_identifier
from test_trading_api import trading_client
from test_paper_after_hours_20261002 import environment, AT, submit
from test_paper_after_hours_allocation_20261002 import audited_fixture, local
from test_paper_after_hours_atomic_20261002 import pending, frame, snapshot
from test_paper_after_hours_partial_atomic_20261002 import invoke
from test_paper_after_hours_partial_cancel_20261002 import staged, cancel
from test_paper_after_hours_partial_allocation_20261002 import (
    capacity, simulated_state, partial_plan, NOW)


@pytest.mark.asyncio
async def test_prior_service_zero_cancel_is_read_only_compatible_with_partial_fifo(environment, audited_fixture):
    first = await pending(environment, quantity=300)
    environment.clock[0] = AT
    later = await pending(environment)
    await cancel(environment, first)
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == first))
        risk = json.loads(row.risk_json)
        # Exactly the previous service's stored ZERO-fill observation, no migration/relabel writes.
        risk["paper_after_hours_cancel"] = {
            "contract_version":CONTRACT, "reason":"user_cancel",
            "canceled_at":environment.clock[0].isoformat(),
            "original_session_end_at":AT.replace(minute=30).isoformat(),
            "unfilled_quantity":300, "filled_quantity_preserved":0,
            "simulation_only":True, "exchange_cancel_receipt":None}
        row.risk_json = json.dumps(risk)
        await db.commit()
        frozen = row.risk_json
    assert (await invoke(environment, later, frame(quantity=150)))["order"]["status"] == "filled"
    async with environment.maker() as db:
        assert await db.scalar(select(TradeOrder.risk_json).where(TradeOrder.order_id == first)) == frozen


def canceled_observation(order, *, filled=0, legacy=False, reason="user_cancel", at=NOW):
    result = simulated_state(order, filled, canceled=True)
    risk = json.loads(result.risk_json)
    proof = risk["paper_after_hours_cancel"]
    proof.update(canceled_at=at.isoformat(), reason=reason,
        original_session_end_at=AT.replace(minute=30).isoformat())
    if legacy:
        proof["contract_version"] = CONTRACT
        proof.pop("order_id")
        proof.pop("original_quantity")
    result.risk_json = json.dumps(risk)
    return result


@pytest.mark.parametrize("damage", ["partial", "quantity", "version", "future", "exchange", "reason"])
def test_legacy_zero_cancel_cannot_alias_partial_or_bad_observation(audited_fixture, damage):
    order = local("head", quantity=300)
    result = canceled_observation(order, legacy=True)
    risk = json.loads(result.risk_json)
    proof = risk["paper_after_hours_cancel"]
    if damage == "partial":
        result.filled_quantity = 100
        proof.update(filled_quantity_preserved=100, unfilled_quantity=200)
    elif damage == "quantity":
        proof["unfilled_quantity"] = 100
    elif damage == "version":
        proof["contract_version"] = "unknown"
    elif damage == "future":
        proof["canceled_at"] = (NOW+timedelta(seconds=10)).isoformat()
    elif damage == "exchange":
        proof["exchange_cancel_receipt"] = "sent-is-not-cancel"
    else:
        proof["reason"] = "user"
    result.risk_json = json.dumps(risk)
    plan = partial_plan(capacity(150), [result], now=NOW+timedelta(seconds=1))
    assert plan["status"] == "evidence_blocked" and plan["fills"] == []


@pytest.mark.parametrize("reason,at", [
    ("session_end", AT.replace(minute=30)), ("session_end", AT+timedelta(days=1)),
    ("user_cancel", AT.replace(minute=30)), ("session_end", AT),
])
def test_cancel_reason_never_widens_matching_clock(audited_fixture, reason, at):
    order = local("head", quantity=300)
    result = canceled_observation(order, reason=reason, at=at)
    risk, intent = json.loads(result.risk_json), json.loads(result.risk_json)["paper_after_hours_intent"]
    valid_expiry = reason == "session_end" and at >= AT.replace(minute=30)
    if valid_expiry:
        assert allocation._partial_cancel_clock(result, risk, intent, at) == at
    else:
        with pytest.raises(ValueError):
            allocation._partial_cancel_clock(result, risk, intent, at)
    # A post-close cancellation observation is NOT a valid fill/source session.
    if at >= AT.replace(minute=30):
        plan = partial_plan(capacity(150), [result], now=at)
        assert plan["status"] == "evidence_blocked" and plan["proposals"] == []


def test_equal_clock_history_requires_exact_pre_cancel_fill_reference(audited_fixture):
    order = local("head", quantity=300)
    payload = capacity(150)
    prior = partial_plan(payload, [order])["proposals"][0]
    result = canceled_observation(order, filled=100, at=NOW)
    blocked = partial_plan(payload, [result], now=NOW, prior_proposals=[prior])
    assert blocked["status"] == "evidence_blocked"
    risk = json.loads(result.risk_json)
    intent = risk["paper_after_hours_intent"]
    intent["numeric_account_id"] = 7
    # Changing the intent changes the prior's local identity; use the legitimate
    # pure planner with numeric identity present before its original proposal.
    order.risk_json = json.dumps({"paper_after_hours_intent":intent})
    prior = partial_plan(payload, [order])["proposals"][0]
    risk["paper_after_hours_cancel"]["verified_fill_ids"] = ["fill-"+_partial_request_identifier(7,prior)]
    result.risk_json = json.dumps(risk)
    good = partial_plan(payload, [result], now=NOW, prior_proposals=[prior])
    assert good["status"] == "waiting" and good["proposals"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, AT.replace(tzinfo=timezone.utc)])
async def test_invalid_post_cas_clock_does_not_abort_next_expiry(environment, audited_fixture, monkeypatch, invalid):
    bad, _ = await staged(environment)
    environment.clock[0] = AT+timedelta(seconds=2)
    good = await pending(environment)
    end = AT.replace(minute=31)
    calls = []
    def clock():
        calls.append(True)
        # reconcile start, bad start, bad after-history, invalid bad post-CAS.
        return invalid if len(calls) == 4 else end
    monkeypatch.setattr(paper, "_public_order_clock", clock)
    before = await snapshot(environment)
    async with environment.maker() as db:
        state = await service.reconcile_paper_after_hours_intents(db)
        assert await db.scalar(select(TradeOrder.status).where(TradeOrder.order_id == bad)) == "partial"
    assert state["canceled"] == [good]
    assert state["errors"] == [{"order_id":bad,"http_status":409}]
    assert await snapshot(environment) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["null_intent", "null_filled", "bad_partial"])
async def test_bad_active_diagnostic_is_isolated_before_end(environment, damage):
    bad = (await submit(environment))["order"]["order_id"]
    good = (await submit(environment))["order"]["order_id"]
    async with environment.maker() as db:
        if damage == "null_intent":
            await db.execute(text("UPDATE trade_order SET risk_json=:p WHERE order_id=:id"),
                {"p":json.dumps({"paper_after_hours_intent":None}),"id":bad})
        elif damage == "null_filled":
            await db.execute(text("UPDATE trade_order SET filled_quantity=NULL WHERE order_id=:id"),{"id":bad})
        else:
            await db.execute(text("UPDATE trade_order SET status='partial' WHERE order_id=:id"),{"id":bad})
        await db.commit()
    async with environment.maker() as db:
        state = await service.reconcile_paper_after_hours_intents(db)
    assert state["errors"] == [{"order_id":bad,"http_status":409}]
    assert [p["order_id"] for p in state["waiting"]] == [good]
    assert state["fills"] == []


@pytest.mark.asyncio
async def test_expiry_truncation_keeps_unprocessed_partial_remainder_for_next_batch(environment, audited_fixture):
    bad, _ = await staged(environment)
    environment.clock[0] = AT+timedelta(seconds=2)
    good = await pending(environment)
    environment.clock[0] = AT.replace(minute=31)
    async with environment.maker() as db:
        first = await service.reconcile_paper_after_hours_intents(db, limit=1)
        assert first["canceled"] == [bad] and first["truncated"] is True
    async with environment.maker() as db:
        second = await service.reconcile_paper_after_hours_intents(db, limit=1)
        assert second["canceled"] == [good] and second["truncated"] is False


@pytest.mark.asyncio
async def test_zero_projection_missing_fill_cannot_hide_consumption_receipt(environment, audited_fixture):
    identifier, _ = await staged(environment)
    from app.trading.paper_after_hours_resource_schema import sqlite_guards
    async with environment.maker() as db:
        # Damaged storage fixture; do NOT perform this repair/deletion in business code.
        await db.execute(text("DROP TRIGGER af_bound_trade_fill_no_delete"))
        await db.execute(text("DELETE FROM trade_fill"))
        statement = next(s for s in sqlite_guards() if "af_bound_trade_fill_no_delete" in s)
        await db.execute(text(statement))
        await db.execute(text("UPDATE trade_order SET filled_quantity=0,status='submitted'"))
        await db.commit()
    before = await snapshot(environment)
    with pytest.raises(HTTPException) as error:
        await cancel(environment, identifier)
    assert error.value.status_code == 409 and await snapshot(environment) == before
    async with environment.maker() as db:
        row = await db.scalar(select(TradeOrder))
        assert row.status == "submitted" and "paper_after_hours_cancel" not in json.loads(row.risk_json)


@pytest.mark.asyncio
async def test_historical_fee_policy_change_does_not_prevent_remainder_cancel(environment, audited_fixture, monkeypatch):
    from app.config.settings import settings
    identifier, _ = await staged(environment)
    before = await snapshot(environment)
    monkeypatch.setattr(settings, "PAPER_MIN_COMMISSION", 99.0)
    monkeypatch.setattr(settings, "PAPER_COMMISSION_RATE", 0.1)
    assert (await cancel(environment, identifier))["status"] == "canceled"
    assert await snapshot(environment) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cas", "after_cas"])
async def test_cancel_cas_failure_never_releases_old_consumption(environment, audited_fixture, monkeypatch, failure):
    from sqlalchemy.ext.asyncio import AsyncSession
    identifier, _ = await staged(environment)
    before = await snapshot(environment)
    original = AsyncSession.execute
    fired = []
    async def fail(self, statement, *args, **kwargs):
        result = await original(self, statement, *args, **kwargs)
        if (getattr(statement, "is_update", False) and statement.table.name == "trade_order"
                and auth.paper_transaction_active(self)):
            fired.append(True)
            if failure == "cas":
                from types import SimpleNamespace
                return SimpleNamespace(rowcount=0)
            raise RuntimeError("cancel CAS acknowledgement failure")
        return result
    from app.trading import paper_authorization as auth
    monkeypatch.setattr(AsyncSession, "execute", fail)
    with pytest.raises(HTTPException if failure == "cas" else RuntimeError):
        await cancel(environment, identifier)
    assert fired and await snapshot(environment) == before
    async with environment.maker() as db:
        assert await db.scalar(select(TradeOrder.status)) == "partial"


@pytest.mark.parametrize("references", ["fill-afp-"+"a"*31, {"fill-afp-"+"a"*31:True},
    [True], ["fill-afp-"+"A"*31], ["fill-afp-"+"a"*31]*2])
def test_verified_cancel_fill_references_are_strict_not_string_membership(audited_fixture, references):
    original = local("head", quantity=300)
    payload = capacity(150)
    first = partial_plan(payload, [original])["proposals"][0]
    result = canceled_observation(original, filled=100, at=NOW+timedelta(milliseconds=200))
    risk = json.loads(result.risk_json)
    risk["paper_after_hours_cancel"]["verified_fill_ids"] = references
    result.risk_json = json.dumps(risk)
    answer = partial_plan(payload, [result], now=NOW+timedelta(seconds=1), prior_proposals=[first])
    assert answer["status"] == "evidence_blocked" and answer["proposals"] == []
