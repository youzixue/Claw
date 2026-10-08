"""A legitimate signal cannot backdate a new reservation into an old quote round."""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.api.v1 import paper
from app.models.trading import TradeOrder, TradeFill
from app.models.stock import QuoteRound
from app.trading import service
from app.trading.paper_authorization import paper_execution_requires_reconciliation
from test_portfolio_dispatch_20260922 import execution_fixture
from test_portfolio_provenance_20260922 import active, captured, AT
from test_portfolio_wallet_20260922 import cash_wallet
from test_paper_deferred_exit_provenance import memory_session
from paper_pending_fixture import accepted_frame


async def accepted_context(db, fixture):
    clock, context = fixture
    clock[0] = AT+timedelta(seconds=40)
    context.update(round_id="actual-current-round", committed_at=AT+timedelta(seconds=30),
                   as_of_at=AT+timedelta(seconds=29), config_version="fixture-config",
                   code_version="fixture-code", records=[{"code": "600001"}])
    await accepted_frame(db, context)
    return clock, context


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["round", "backdate", "both", "asof", "future", "unknown_round"])
async def test_new_reservation_rejects_forged_round_and_submission_clock(
    memory_session, execution_fixture, mutation,
):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    clock, context = await accepted_context(db, execution_fixture)
    cmd.decision_at = clock[0]
    cmd.as_of_at = context["as_of_at"]
    cmd.decision_round_id = context["round_id"]
    if mutation in ("round", "both"):
        cmd.decision_round_id = "pretended-old-round"
    if mutation in ("backdate", "both"):
        cmd.decision_at = AT+timedelta(seconds=3)
    if mutation == "asof":
        cmd.as_of_at = AT
    if mutation == "future":
        cmd.decision_at = clock[0]+timedelta(seconds=1)
    if mutation == "unknown_round":
        cmd.decision_round_id = context["round_id"] = "uncommitted-context"
    with pytest.raises(HTTPException) as exc:
        await service.submit_order(db, cmd)
    assert exc.value.status_code == 409
    assert await db.scalar(select(TradeOrder.id)) is None
    assert await db.scalar(select(TradeFill.id)) is None


@pytest.mark.asyncio
async def test_service_stamps_actual_reservation_time_and_idempotent_retry_keeps_it(
    memory_session, execution_fixture,
):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    clock, context = await accepted_context(db, execution_fixture)
    cmd.decision_at = AT+timedelta(seconds=31)  # source decision precedes service receipt
    cmd.as_of_at = context["as_of_at"]
    cmd.decision_round_id = context["round_id"]
    result = await service.submit_order(db, cmd)
    order = await db.scalar(select(TradeOrder))
    assert result["order"]["status"] == "submitted"
    assert order.decision_at == clock[0]
    assert order.decision_round_id == "actual-current-round"
    import json
    metadata = json.loads(order.risk_json)["paper_deferred_order"]
    assert metadata["portfolio_reservation_clock"]["requested_at"] == (AT+timedelta(seconds=31)).isoformat()
    assert metadata["portfolio_reservation_clock"]["accepted_at"] == clock[0].isoformat()
    first_clock = order.decision_at
    clock[0] += timedelta(seconds=10)
    context["round_id"] = "later-context-not-required-for-idempotent-read"
    retry = await service.submit_order(db, cmd)
    assert retry["order"]["order_id"] == order.order_id
    await db.refresh(order)
    assert order.decision_at == first_clock


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("quality_status", "degraded"), ("source", "unknown"),
    ("config_version", "wrong"), ("code_version", "wrong"),
    ("as_of_at", AT), ("committed_at", AT),
])
async def test_persisted_quote_manifest_must_match_current_context(
    memory_session, execution_fixture, field, value,
):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    clock, context = await accepted_context(db, execution_fixture)
    cmd.decision_at, cmd.as_of_at, cmd.decision_round_id = (
        clock[0], context["as_of_at"], context["round_id"])
    row = await db.scalar(select(QuoteRound).where(QuoteRound.round_id == context["round_id"]))
    setattr(row, field, value)
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await service.submit_order(db, cmd)
    assert exc.value.status_code == 409
    assert await db.scalar(select(TradeOrder.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["backwards", "cross_day", "expired", "round", "quality", "version"])
async def test_clock_and_context_are_rechecked_after_manifest_read(
    memory_session, execution_fixture, monkeypatch, drift,
):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    clock, context = await accepted_context(db, execution_fixture)
    cmd.decision_at, cmd.as_of_at, cmd.decision_round_id = (
        clock[0], context["as_of_at"], context["round_id"])
    real = db.scalar
    did_drift = []
    async def read_then_drift(statement, *args, **kwargs):
        value = await real(statement, *args, **kwargs)
        if any(item.get("entity") is QuoteRound for item in getattr(statement, "column_descriptions", [])):
            did_drift.append(True)
            if drift == "backwards":
                clock[0] -= timedelta(seconds=1)
            elif drift == "cross_day":
                clock[0] += timedelta(days=1)
            elif drift == "expired":
                clock[0] += timedelta(seconds=91)
            elif drift == "round":
                context["round_id"] = "advanced-after-await"
            elif drift == "quality":
                context["quality_status"] = "degraded"
            else:
                context["config_version"] = "changed-after-await"
        return value
    monkeypatch.setattr(db, "scalar", read_then_drift)
    with pytest.raises(HTTPException) as exc:
        await service.submit_order(db, cmd)
    assert exc.value.status_code == 409 and did_drift == [True]
    assert await db.scalar(select(TradeOrder.id)) is None
    assert await db.scalar(select(TradeFill.id)) is None


@pytest.mark.asyncio
async def test_manifest_reread_does_not_trust_a_cached_orm_identity(
    memory_session, execution_fixture,
):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    clock, context = await accepted_context(db, execution_fixture)
    cmd.decision_at, cmd.as_of_at, cmd.decision_round_id = (
        clock[0], context["as_of_at"], context["round_id"])
    cached = await db.scalar(select(QuoteRound).where(QuoteRound.round_id == context["round_id"]))
    # Explicit isolated consistency fault: SQL truth changes, ORM cache does not.
    await db.execute(text("UPDATE quote_round SET quality_status='degraded' WHERE round_id=:round"),
                     {"round": context["round_id"]})
    await db.commit()
    assert cached.quality_status == "ok"
    with pytest.raises(HTTPException) as exc:
        await service.submit_order(db, cmd)
    assert exc.value.status_code == 409
    assert await db.scalar(select(TradeOrder.id)) is None


@pytest.mark.asyncio
async def test_reservation_cleanup_failure_preserves_uncertain_annotation(memory_session, monkeypatch):
    from app.paper.portfolio_reservation import shared_order_reservation, reservation_active
    db = memory_session
    real = db.rollback
    async def fail_cleanup():
        await real()
        raise RuntimeError("fixture cleanup failed")
    monkeypatch.setattr(db, "rollback", fail_cleanup)
    with pytest.raises(RuntimeError, match="cleanup failed") as exc:
        async with shared_order_reservation(db):
            raise RuntimeError("fixture commit acknowledgement lost")
    assert paper_execution_requires_reconciliation(exc.value)
    assert not reservation_active(db)
