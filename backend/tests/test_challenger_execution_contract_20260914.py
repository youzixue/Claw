"""Per-route real risk/service/broker/SQLite integration in local fixtures only."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, delete

from app.api.v1 import paper
from app.models.governance import TradeCalendarModel
from app.models.paper import PaperShadowEvent, PaperPosition, PaperTradeLog
from app.models.stock import QuoteRound
from app.models.trading import TradeOrder, TradeFill
from app.paper.strategy_iteration_challenger import run_strategy_iteration_challenger_accounts
from app.paper.strategy_iteration_shadow import ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_F2
from app.trading.service import SubmitOrderCommand, submit_order
from challenger_execution_fixture import qualified_challenger_execution
from test_strategy_iteration_challenger import challenger_env, _seed_confirmed

AT = datetime(2026, 9, 8, 10)
ROUTES = ["momentum_first_retest", ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_F2]


async def event_rows(db):
    return [tuple(row) for row in (await db.execute(
        select(*PaperShadowEvent.__table__.columns).order_by(PaperShadowEvent.id))).all()]


async def seed(db, route):
    price, ask = (10.4, 10.41) if route == "momentum_first_retest" else (10.05, 10.06)
    await _seed_confirmed(db, code="600301", route_id=route, now=AT, price=price, ask=ask)
    return ask


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_each_route_passes_actual_risk_and_books_only_its_own_receipt(
    challenger_env, qualified_challenger_execution, route,
):
    # No _risk_check_for_buy/pre_trade_risk_check/broker/quote-validator mocks.
    async with challenger_env() as db:
        ask = await seed(db, route)
        before = await event_rows(db)
        result = await qualified_challenger_execution(db, now=AT)
        assert result["entries"] == 1
        order = (await db.scalars(select(TradeOrder))).one()
        fill = (await db.scalars(select(TradeFill))).one()
        trade = (await db.scalars(select(PaperTradeLog))).one()
        position = (await db.scalars(select(PaperPosition))).one()
        assert order.account_id == paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE[route]
        assert order.source == route and order.strategy_id == "paper-challenger-forward"
        assert order.status == "filled"
        assert order.price >= fill.price == trade.price == position.buy_price == ask
        assert fill.quantity == trade.amount == position.buy_amount == order.quantity
        assert fill.quantity > 0 and fill.quantity % 100 == 0
        assert fill.order_id == order.order_id and fill.broker_trade_id == str(trade.id)
        assert fill.filled_at == trade.trade_time == AT
        assert position.account_id == trade.account_id
        assert trade.strategy_version == position.strategy_version == order.strategy_version
        risk = json.loads(order.risk_json)
        assert risk["checked_rules"] == 10 and risk["evaluation_status"] == "complete"
        assert risk["final_level"] == "pass"
        assert risk["paper_immediate_execution"]["mandatory"] is True
        assert risk["paper_immediate_execution"]["status"] == "fillable"
        assert risk["paper_ledger_timing"]["physical_commit_at"] is None
        replay = await submit_order(db, SubmitOrderCommand(
            code=order.code, side=order.side, price=order.price, quantity=order.quantity,
            broker="paper", account_id=order.account_id, idempotency_key=order.idempotency_key))
        assert replay["idempotent_replay"]
        assert [row["fill_id"] for row in replay["fills"]] == [fill.fill_id]
        assert len((await db.scalars(select(PaperTradeLog))).all()) == 1
        assert await event_rows(db) == before
    assert qualified_challenger_execution.risk_results


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("kind", ["missing_calendar", "unhealthy_round", "future_receive"])
async def test_confirmed_route_cannot_override_independent_execution_evidence(
    challenger_env, qualified_challenger_execution, route, kind,
):
    async with challenger_env() as db:
        await seed(db, route)
        before = await event_rows(db)
        async with qualified_challenger_execution.frame(db, AT) as payload:
            if kind == "missing_calendar":
                await db.execute(delete(TradeCalendarModel).where(
                    TradeCalendarModel.trade_date == AT.date()))
                await db.commit()
            elif kind == "unhealthy_round":
                row = await db.scalar(select(QuoteRound).where(
                    QuoteRound.round_id == payload["round_id"]))
                row.quality_status = "degraded"
                await db.commit()
            else:
                payload["records_by_code"]["600301"]["received_at"] = AT+timedelta(seconds=1)
            result = await run_strategy_iteration_challenger_accounts(db, now=AT)
        assert result["entries"] == 0
        assert not (await db.scalars(select(TradeFill))).all()
        assert not (await db.scalars(select(PaperTradeLog))).all()
        assert not (await db.scalars(select(PaperPosition))).all()
        orders = list((await db.scalars(select(TradeOrder))).all())
        assert all(o.status == "rejected" and o.filled_quantity == 0 for o in orders)
        assert await event_rows(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ROUTES)
async def test_valid_quote_does_not_authorize_cross_route_account(
    challenger_env, qualified_challenger_execution, route,
):
    async with challenger_env() as db:
        ask = await seed(db, route)
        owner = paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE[route]
        foreign_route = next(r for r in ROUTES if r != route)
        before = await event_rows(db)
        result = await qualified_challenger_execution(db, now=AT, command=SubmitOrderCommand(
            code="600301", side="buy", price=ask, quantity=100, broker="paper",
            account_id=owner, strategy_id="paper-challenger-forward",
            strategy_version=paper._strategy_version(owner), source=foreign_route,
            signal_id="chlg-wrong-route-fixture", decision_at=AT,
            idempotency_key="isolated-cross-route"))
        assert result["order"]["status"] == "rejected"
        assert "只接受内部前向确认事件委托" in result["order"]["error_message"]
        assert result["risk"]["checked_rules"] == 10
        assert result["fills"] == []
        assert not (await db.scalars(select(TradeFill))).all()
        assert not (await db.scalars(select(PaperTradeLog))).all()
        assert await event_rows(db) == before
