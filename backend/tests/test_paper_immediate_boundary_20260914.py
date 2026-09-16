"""All immediate service orders must use real quote evidence; fixture DBs only."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.api.v1 import paper
from app.models.governance import TradeCalendarModel
from app.models.paper import PaperPosition, PaperTradeLog
from app.models.stock import QuoteRound, StockSpot
from app.models.trading import TradeOrder, TradeFill
from app.trading import service, paper_public_execution
from test_paper_public_boundary_20260914 import (
    AT, LocalDB, quote, public_env, paper_client, qualified_execution_risk,
)


async def seed_exit(db, *, same_day=False):
    account = await paper._get_or_create_account(db)
    at = AT if same_day else AT - timedelta(days=3)
    db.add(PaperTradeLog(account_id=account.id, code="000001", trade_type="buy",
        price=10, amount=100, commission=5, tax=0, trade_time=at,
        strategy_version="legacy-fixture"))
    db.add(PaperPosition(account_id=account.id, code="000001", buy_price=10,
        buy_amount=100, buy_time=at, current_price=10, is_closed=False,
        strategy_version="legacy-fixture"))
    await db.commit()


def command(side, flag="omitted", **changes):
    values = dict(code="000001", side=side, price=10.2 if side=="buy" else 9.9,
        quantity=100, account_id="default", decision_at=AT,
        decision_round_id="fixture-round", as_of_at=AT-timedelta(seconds=3),
        source="position" if side=="sell" else "internal-fixture",
        strategy_id="paper-auto-short", signal_id=f"internal-{side}")
    if flag != "omitted":
        values["require_immediate_quote"] = flag
    values.update(changes)
    return service.SubmitOrderCommand(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["omitted", False, True, None, 0, "false"])
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_every_internal_immediate_fill_uses_depth_and_preserves_provenance(public_env, monkeypatch, flag, side):
    _, factory, _ = public_env
    clocks = iter([AT, AT+timedelta(seconds=1)])
    # Subsequent ledger checks observe the same current clock, not an exhausted stub.
    monkeypatch.setattr(paper, "_public_order_clock", lambda:next(clocks, AT+timedelta(seconds=1)))
    async with factory() as db:
        if side == "sell":
            await seed_exit(db)
        cmd = command(side, flag)
        result = await service.submit_order(db, cmd)
        assert result["order"]["status"] == "filled", result
        proof = result["risk"]["paper_immediate_execution"]
        assert proof == result["risk"]["paper_public_execution"]
        assert proof["scope"] == "all_immediate_paper_orders" and proof["mandatory"] is True
        assert proof["contract_version"] == "immediate_paper_fill_v2_20260914"
        expected_price = 10 if side=="buy" else 9.99
        assert result["fills"][0]["price"] == expected_price != cmd.price
        assert cmd.decision_at == AT and cmd.as_of_at == AT-timedelta(seconds=3)
        fill = await db.scalar(select(TradeFill))
        assert fill.filled_at == AT+timedelta(seconds=1)
        assert fill.fill_round_id == "fixture-round"
        order = await db.scalar(select(TradeOrder))
        assert order.decision_at == AT and order.as_of_at == cmd.as_of_at
        if side=="sell":
            sale = await db.scalar(select(PaperTradeLog).where(PaperTradeLog.trade_type=="sell"))
            assert sale.strategy_version=="legacy-fixture"
            assert json.loads(fill.raw_json)["entry_fee_allocation"]["allocated_entry_fee"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["omitted", False])
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("bad", ["calendar", "quality", "wrong_round", "wrong_asof",
                                  "no_depth", "after_hours", "expired", "missing_source", "prior_day_asof"])
async def test_internal_flag_cannot_bypass_mandatory_evidence(public_env, monkeypatch, flag, side, bad):
    _, factory, _ = public_env
    async with factory() as db:
        if side=="sell":
            await seed_exit(db)
        cmd = command(side, flag)
        spot = await db.get(StockSpot, "000001")
        if bad=="calendar":
            await db.delete(await db.get(TradeCalendarModel, AT.date()))
        elif bad=="quality":
            row = await db.scalar(select(QuoteRound))
            row.quality_status="degraded"
        elif bad=="prior_day_asof":
            row = await db.scalar(select(QuoteRound))
            row.as_of_at=AT-timedelta(days=1)
            cmd.as_of_at=row.as_of_at
        elif bad=="wrong_round":
            cmd.decision_round_id="another-original-round"
        elif bad=="wrong_asof":
            cmd.as_of_at=AT-timedelta(seconds=4)
        elif bad=="no_depth":
            setattr(spot, "ask1_volume" if side=="buy" else "bid1_volume", 0)
        elif bad=="after_hours":
            monkeypatch.setattr(paper, "_public_order_clock", lambda:AT.replace(hour=16))
        elif bad=="expired":
            monkeypatch.setattr(paper, "_public_order_clock", lambda:AT+timedelta(seconds=91))
        elif bad=="missing_source":
            spot.source_quote_at=None
        await db.commit()
        original_decision_round=cmd.decision_round_id
        original_asof=cmd.as_of_at
        count_before = await db.scalar(select(func.count(PaperTradeLog.id)))
        result = await service.submit_order(db, cmd)
        assert result["order"]["status"]=="rejected", result
        assert result["risk"]["paper_immediate_execution"]["status"]=="rejected"
        assert result["fills"]==[]
        assert cmd.decision_round_id==original_decision_round and cmd.as_of_at==original_asof
        assert await db.scalar(select(func.count(PaperTradeLog.id)))==count_before
        assert await db.scalar(select(func.count(TradeFill.id)))==0
        if side=="sell":
            position = await db.scalar(select(PaperPosition))
            assert position.buy_amount==100 and not position.is_closed


@pytest.mark.asyncio
async def test_internal_quote_boundary_does_not_remove_t1(public_env):
    _, factory, _ = public_env
    async with factory() as db:
        await seed_exit(db, same_day=True)
        result=await service.submit_order(db, command("sell", False))
        assert result["order"]["status"]=="rejected" and "T+1" in result["order"]["error_message"]
        assert result["risk"]["paper_immediate_execution"]["status"]=="fillable"
        assert await db.scalar(select(func.count(TradeFill.id)))==0


@pytest.mark.asyncio
async def test_dry_run_remains_no_broker_and_no_immediate_fill(public_env, monkeypatch):
    _, factory, _ = public_env
    guard=AsyncMock(side_effect=AssertionError("dry-run must not match"))
    monkeypatch.setattr(paper_public_execution, "public_fill_evidence", guard)
    async with factory() as db:
        result=await service.submit_order(db, command("buy", False, execute=False))
        assert result["order"]["status"]=="accepted" and result["fills"]==[]
        guard.assert_not_awaited()
        assert await db.scalar(select(func.count(PaperTradeLog.id)))==0


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [AT+timedelta(seconds=91), AT.replace(hour=11,minute=30),
    AT.replace(hour=14,minute=57), AT+timedelta(days=1), AT-timedelta(microseconds=1),
    None, AT.replace(tzinfo=timezone.utc)])
async def test_query_delay_cannot_reuse_pre_query_clock(monkeypatch, later):
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=quote()))
    result=await paper_public_execution.public_fill_evidence(
        LocalDB(), command("buy", False), now=AT, validation_clock=lambda:later)
    assert result["status"]=="rejected"


@pytest.mark.asyncio
async def test_input_asof_is_round_asof_not_individual_source_time(public_env):
    _, factory, _=public_env
    async with factory() as db:
        row=await db.scalar(select(QuoteRound))
        row.as_of_at=AT-timedelta(seconds=5)  # round aggregate may precede this stock
        await db.commit()
        cmd=command("buy", False, as_of_at=row.as_of_at)
        result=await service.submit_order(db, cmd)
        assert result["order"]["status"]=="filled",result
        assert cmd.as_of_at==AT-timedelta(seconds=5)
        evidence=result["risk"]["paper_immediate_execution"]
        assert evidence["quote_as_of_at"]!=evidence["source_quote_at"]
