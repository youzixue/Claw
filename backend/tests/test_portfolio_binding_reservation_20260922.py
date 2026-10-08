"""Shared command binding + SQLite reservation ownership, isolated from fills.

Reservation tests use real projection refresh and wallet sizing. They isolate
market/rule evaluation; the full ten-rule/broker ledger chain has its own suite.
"""
import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select

from app.api.v1 import paper
from app.models.trading import TradeOrder
from app.paper import portfolio_provenance as provenance
from app.paper.portfolio_reservation import reservation_active, shared_order_reservation
from app.paper.portfolio_wallet import validate_command_budget
from app.trading import service
from app.trading.paper_authorization import finish_account_write, paper_transaction_active
from test_portfolio_provenance_20260922 import active, captured, AT
from test_portfolio_wallet_20260922 import cash_wallet, pending
from test_paper_deferred_exit_provenance import memory_session
from test_quote_round_execution import quote_execution_env
from paper_pending_fixture import accepted_frame


async def reservation_frame(db, monkeypatch):
    context = {"round_id": "allocation-round", "as_of_at": AT,
               "committed_at": AT, "quality_status": "ok",
               "config_version": "fixture-config", "code_version": "fixture-code",
               "records": [{"code": "600001"}]}
    monkeypatch.setattr(paper, "_public_order_clock", lambda: AT+timedelta(seconds=3))
    monkeypatch.setattr(paper, "_quote_round_context", lambda: context)
    await accepted_frame(db, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["price", "stop", "metadata_stop", "mode", "queue",
                                      "fallback", "candidate", "confirm_clock", "late_new"])
async def test_legal_key_cannot_change_execution_contract(memory_session, active, mutation):
    _, cmd, meta = await captured(memory_session, "promotion")
    if mutation == "price":
        cmd.price = 10.01
    elif mutation == "stop":
        cmd.stop_loss_price = 9.0
    elif mutation == "metadata_stop":
        meta["stop_loss_price"] = 9.0
    elif mutation == "mode":
        cmd.defer_until_next_round = False
    elif mutation == "queue":
        cmd.queue_if_limit_up = True
    elif mutation == "fallback":
        cmd.deferred_metadata = None
        cmd.queue_metadata = meta
    elif mutation == "candidate":
        meta["candidate"]["fake"] = True
    elif mutation == "confirm_clock":
        meta["confirmed_at"] = (AT+timedelta(seconds=2)).isoformat()
    else:
        cmd.decision_at = AT+timedelta(seconds=121)
    with pytest.raises(provenance.PortfolioIdentityError):
        await provenance.command_origin(memory_session, cmd)


@pytest.mark.asyncio
async def test_reservation_is_not_fill_authority_and_survives_projection_refresh(
    memory_session, active, monkeypatch,
):
    db = memory_session
    account = await cash_wallet(db)
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    monkeypatch.setattr(paper, "_nav_reporting_day_open", AsyncMock(return_value=False))
    statements = []
    sync = db.get_bind()
    def committed(conn):
        statements.append("COMMIT")
    event.listen(sync, "commit", committed)
    try:
        async with shared_order_reservation(db):
            statements.clear()  # preflight snapshot release precedes writer ownership
            assert reservation_active(db) and not paper_transaction_active(db)
            await paper._refresh_account(db, account)
            assert statements == [], "projection refresh released the reservation writer"
            assert db.in_transaction()
        assert not reservation_active(db)
    finally:
        event.remove(sync, "commit", committed)


@pytest.mark.asyncio
async def test_reservation_cannot_cross_task_or_session(memory_session):
    db = memory_session
    async with shared_order_reservation(db):
        async def child():
            with pytest.raises(RuntimeError, match="cross"):
                await finish_account_write(db)
        await asyncio.create_task(child())
        with pytest.raises(RuntimeError, match="cross"):
            reservation_active(object())
        with pytest.raises(RuntimeError, match="nest"):
            async with shared_order_reservation(db):
                pass


@pytest.mark.asyncio
async def test_two_sessions_serialize_budget_read_and_order_reservation(
    quote_execution_env, active, monkeypatch,
):
    factory = quote_execution_env
    monkeypatch.setattr(paper, "_paper_now", lambda: AT+timedelta(seconds=3))
    monkeypatch.setattr(paper, "_nav_reporting_day_open", AsyncMock(return_value=False))
    async with factory() as db:
        await reservation_frame(db, monkeypatch)
        await cash_wallet(db)
        _, a, _ = await captured(db, "promotion")
        _, b, _ = await captured(db, "mainline")
        # Only one lot after reserving other future slice fees; two valid keys.
        await pending(db, code="600003", amount=3800, key="reserved-before-race")
    first_refresh = asyncio.Event()
    contender_started = asyncio.Event()
    refresh_order = []
    real_refresh = paper._refresh_account
    async def refresh(db, account):
        result = await real_refresh(db, account)
        refresh_order.append(asyncio.current_task().get_name())
        if len(refresh_order) == 1:
            first_refresh.set()
            await asyncio.wait_for(contender_started.wait(), 2)
            # A second connection is allowed to try, but must not enter refresh.
            await asyncio.sleep(.05)
            assert len(refresh_order) == 1
        return result
    monkeypatch.setattr(paper, "_refresh_account", refresh)
    async def financial_precheck(db, cmd):
        origin = await provenance.command_origin(db, cmd)
        assert reservation_active(db)
        await service._paper_account_context(db, "shared_50k")
        try:
            budget = await validate_command_budget(db, cmd, origin)
        except provenance.PortfolioIdentityError as exc:
            return {"final_level": "block", "block_reasons": [{"message": str(exc)}]}
        return {"final_level": "pass", "warnings": [], "block_reasons": [],
                "paper_portfolio_budget": budget}
    monkeypatch.setattr(service, "_pre_trade_risk_check", financial_precheck)
    monkeypatch.setattr(service, "experiment_active", lambda *_a, **_kw: False)
    async def submit(command, second=False):
        if second:
            await first_refresh.wait()
            contender_started.set()
        async with factory() as db:
            return await service.submit_order(db, command)
    results = await asyncio.wait_for(asyncio.gather(submit(a), submit(b, True)), 8)
    assert sorted(result["order"]["status"] for result in results) == ["risk_blocked", "submitted"]
    async with factory() as db:
        rows = (await db.scalars(select(TradeOrder).where(
            TradeOrder.account_id == "shared_50k", TradeOrder.status == "submitted"))).all()
        assert sum(row.price*(row.quantity-int(row.filled_quantity or 0)) for row in rows) == 39000
    assert len(refresh_order) == 2


@pytest.mark.asyncio
async def test_projection_failure_rolls_back_without_reserving(memory_session, active, monkeypatch):
    db = memory_session
    await reservation_frame(db, monkeypatch)
    account = await cash_wallet(db)
    async def failure(db, cmd):
        account.total_assets = 1
        await finish_account_write(db)
        raise RuntimeError("fixture after projection flush")
    monkeypatch.setattr(service, "_pre_trade_risk_check", failure)
    _, cmd, _ = await captured(db, "promotion")
    with pytest.raises(RuntimeError, match="after projection"):
        await service.submit_order(db, cmd)
    assert not reservation_active(db)
    await db.refresh(account)
    assert account.total_assets == 50000
    assert await db.scalar(select(TradeOrder.id)) is None
