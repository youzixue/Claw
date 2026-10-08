"""Retired portfolio cannot notify or buy; original accounts and old exits remain."""
import json
from datetime import timedelta
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.config.settings import settings
from app.models.paper import PaperTradeLog, PaperPosition, PaperAccount
from app.models.trading import TradeFill
from app.push import paper_buy_points as points
from test_portfolio_push_reliability_20260922 import shared, setup, publish, signal, logs, record
from test_portfolio_execution_20260922 import (
    quote_execution_env, real_execution, active, allocate, reconcile,
)


async def queued(shared):
    maker, now, _ = shared
    await publish(maker, signal(now))
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["recovered"] == 1
    return maker, now


@pytest.mark.asyncio
async def test_disabled_suppresses_already_queued_and_never_replays_on_reenable(shared, monkeypatch):
    maker, now = await queued(shared)
    send = shared[2]
    before = [(r.id, r.candidate_json) for r in await logs(maker)]
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
    result = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert result["count"] == 0 and not send.called
    rows = await logs(maker)
    terminal = [r for r in rows if r.action == points.DELIVERY]
    assert len(terminal) == 1 and terminal[0].decision == "disabled"
    assert json.loads(terminal[0].candidate_json)["cause"] == "portfolio_disabled"
    assert before == [(r.id, r.candidate_json) for r in rows if r.action == points.SIGNAL]
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", True)
    await points.dispatch_buy_points(now=now+timedelta(seconds=16), session_factory=maker)
    assert not send.called
    assert len(await logs(maker)) == len(rows)


@pytest.mark.asyncio
async def test_original_notification_survives_portfolio_retirement(shared, monkeypatch):
    maker, now = await queued(shared)
    await record(maker, now)
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
    execution = AsyncMock(side_effect=AssertionError("retired portfolio projection must not run"))
    monkeypatch.setattr(points, "_portfolio_execution", execution)
    result = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert result["count"] == 1 and shared[2].call_count == 1
    audits = shared[2].call_args.args[0].extra["signal_audits"]
    assert len(audits) == 1 and audits[0]["account"] != "shared_50k"
    execution.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_switch_rechecked_after_attempt_lease_commit(shared, monkeypatch, mixed):
    maker, now = await queued(shared)
    if mixed:
        await record(maker, now)
    class DisableAtCommit(AsyncSession):
        async def commit(self):
            await super().commit()
            monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
    late_maker = async_sessionmaker(maker.kw["bind"], class_=DisableAtCommit, expire_on_commit=False)
    result = await points.dispatch_buy_points(now=now, session_factory=late_maker)
    assert result["count"] == int(mixed)
    assert shared[2].call_count == int(mixed)
    if mixed:
        assert all(x["account"] != "shared_50k" for x in shared[2].call_args.args[0].extra["signal_audits"])
    states = [r for r in await logs(maker) if r.account_id == 50 and r.action == points.DELIVERY]
    assert states[-1].decision == "disabled"
    assert json.loads(states[-1].candidate_json)["cause"] == "portfolio_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_existing_pending_buy_cannot_increase_inventory_after_disabled(
    quote_execution_env, real_execution, monkeypatch, partial,
):
    clock = real_execution
    async with quote_execution_env() as db:
        _, order = await allocate(db, clock)
        if partial:
            outcome = await reconcile(db, clock, "retire-first-slice", hands=1)
            assert outcome[0]["event"] == "partial"
        wallet = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == "shared_50k"))
        before = (wallet.current_capital, order.filled_quantity,
                  await db.scalar(select(func.count(TradeFill.id))),
                  await db.scalar(select(func.count(PaperTradeLog.id))))
        monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
        outcome = await reconcile(db, clock, "retire-no-further-buy")
        assert outcome and outcome[0]["event"] in ("risk_blocked", "canceled"), outcome
        await db.refresh(wallet); await db.refresh(order)
        after = (wallet.current_capital, order.filled_quantity,
                 await db.scalar(select(func.count(TradeFill.id))),
                 await db.scalar(select(func.count(PaperTradeLog.id))))
        assert after == before
        positions = (await db.scalars(select(PaperPosition))).all()
        assert sum(p.buy_amount for p in positions) == (100 if partial else 0)
