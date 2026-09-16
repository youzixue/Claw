"""Public HTTP and private booking boundary; all DBs/quotes below are fixtures."""
import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1 import paper, trading
from app.models.governance import TradeCalendarModel
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.stock import MarketSentiment, QuoteRound, StockSpot, StockTag
from app.models.trading import TradeOrder, TradeFill
from app.risk.engine import risk_engine
from app.trading import service
from app.trading.broker import BrokerOrderRequest, PaperBrokerAdapter
from app.trading.paper_authorization import (
    _paper_execution_scope, authorize_broker_request, authorize_ledger_request,
)
from app.trading.paper_public_execution import public_fill_evidence
from test_paper_api import paper_client, qualified_execution_risk

AT = datetime(2026, 9, 14, 10)


def quote(**changes):
    row = dict(code="000001", name="平安银行", price=10, limit_down=9, limit_up=11,
               source_quote_at=AT-timedelta(seconds=3),
               received_at=AT-timedelta(seconds=2), updated_at=AT-timedelta(seconds=1),
               quote_round_id="fixture-round", ask1_price=10, ask1_volume=10,
               bid1_price=9.99, bid1_volume=10)
    row.update(changes)
    return SimpleNamespace(**row)


class LocalDB:
    from contextlib import nullcontext
    no_autoflush = nullcontext()
    def __init__(self, calendar=True, session_type="full"):
        self.calendar = (SimpleNamespace(is_trade_day=calendar, session_type=session_type)
                         if calendar is not None else None)
    async def get(self, model, key):
        assert model is TradeCalendarModel
        return self.calendar
    async def scalar(self, statement):
        return SimpleNamespace(quality_status="ok", source="tencent", trade_date=AT.date(),
            committed_at=AT-timedelta(seconds=1), as_of_at=AT-timedelta(seconds=3),
            config_version="fixture-config", code_version="fixture-code")


def round_row(round_id, at):
    return QuoteRound(round_id=round_id, source="tencent", trade_date=at.date(),
        committed_at=at-timedelta(seconds=1), as_of_at=at-timedelta(seconds=3),
        expected_count=1, received_count=1, source_time_count=1, coverage=1,
        source_time_coverage=1, quality_status="ok",
        config_version="fixture-config", code_version="fixture-code")


@pytest.mark.asyncio
@pytest.mark.parametrize("changes, now, calendar, session_type", [
    ({}, AT, None, "full"), ({}, AT, False, "full"), ({}, AT, True, None),
    ({}, AT, True, "half_day"),
    ({}, AT.replace(hour=9, minute=25), True, "full"),
    ({}, AT.replace(hour=11, minute=30), True, "full"),
    ({}, AT.replace(hour=12), True, "full"),
    ({}, AT.replace(hour=14, minute=57), True, "full"),
    ({}, AT.replace(hour=15), True, "full"),
    ({}, datetime(2026, 9, 19, 10), True, "full"),
    ({}, datetime(2026, 10, 1, 10), True, "full"),
    ({"code":"000002"}, AT, True, "full"),
    ({"quote_round_id":None}, AT, True, "full"),
    ({"source_quote_at":None}, AT, True, "full"),
    ({"received_at":None}, AT, True, "full"),
    ({"updated_at":None}, AT, True, "full"),
    ({"source_quote_at":AT+timedelta(seconds=1)}, AT, True, "full"),
    ({"source_quote_at":AT-timedelta(seconds=91)}, AT, True, "full"),
    ({"received_at":AT-timedelta(seconds=4)}, AT, True, "full"),
    ({"updated_at":AT+timedelta(seconds=1)}, AT, True, "full"),
    ({"limit_down":None}, AT, True, "full"),
    ({"limit_up":float("nan")}, AT, True, "full"),
    ({"limit_down":11,"limit_up":9}, AT, True, "full"),
    ({"price":12}, AT, True, "full"),
    ({"ask1_price":12}, AT, True, "full"),
    ({"ask1_volume":float("inf")}, AT, True, "full"),
    ({"ask1_volume":-1}, AT, True, "full"),
    ({"ask1_price":0,"ask1_volume":0,"price":11,"bid1_price":11}, AT, True, "full"),
    ({"ask1_volume":0}, AT, True, "full"),
    ({"ask1_price":10.01}, AT, True, "full"),
    ({"ask2_price":10,"ask2_volume":1}, AT, True, "full"),
    ({"ask2_price":9.99,"ask2_volume":1}, AT, True, "full"),
    ({"bid1_price":10}, AT, True, "full"),
])
async def test_public_quote_rejects_without_full_executable_evidence(
    monkeypatch, changes, now, calendar, session_type,
):
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=quote(**changes)))
    result = await public_fill_evidence(
        LocalDB(calendar, session_type),
        service.SubmitOrderCommand(code="000001", side="buy", price=10, quantity=100), now=now)
    assert result["status"] == "rejected" and result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "degraded", "source", "clock", "future_asof"])
async def test_public_quote_requires_exact_healthy_round_manifest(monkeypatch, fault):
    db = LocalDB()
    row = await db.scalar(None)
    if fault == "missing":
        row = None
    elif fault == "degraded":
        row.quality_status = "degraded"
    elif fault == "source":
        row.source = "unknown"
    elif fault == "clock":
        row.committed_at += timedelta(seconds=1)
    else:
        row.as_of_at = AT+timedelta(seconds=1)
    db.scalar = AsyncMock(return_value=row)
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=quote()))
    result = await public_fill_evidence(db, service.SubmitOrderCommand(
        code="000001",side="buy",price=10,quantity=100),now=AT)
    assert result["status"] == "rejected" and "轮次" in result["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision, validation", [
    (AT, AT+timedelta(seconds=91)),
    (AT-timedelta(seconds=5), AT),
    (AT+timedelta(seconds=1), AT),
    (AT-timedelta(days=1), AT),
    (AT, AT.replace(hour=15)),
])
async def test_delayed_validation_cannot_borrow_future_quote_or_backdate_fill(monkeypatch, decision, validation):
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=quote()))
    result = await public_fill_evidence(LocalDB(), service.SubmitOrderCommand(
        code="000001",side="buy",price=10,quantity=100,decision_at=decision),now=validation)
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_market_order_not_silently_treated_as_synthetic_price(monkeypatch):
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=quote()))
    result = await public_fill_evidence(LocalDB(), service.SubmitOrderCommand(
        code="000001",side="buy",price=10,quantity=100,order_type="market"),now=AT)
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_public_depth_uses_original_limit_actual_vwap_and_unchanged_participation(monkeypatch):
    monkeypatch.setattr(service.settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", .5)
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(
        return_value=quote(ask1_volume=2, ask2_price=10.1, ask2_volume=2)))
    cmd = service.SubmitOrderCommand(code="000001", side="buy", price=10.2, quantity=200)
    result = await public_fill_evidence(LocalDB(), cmd, now=AT)
    assert result["status"] == "fillable"
    assert result["fill_price"] == 10.05 and result["limit_price"] == 10.2
    assert result["filled_quantity"] == 200
    cmd.quantity = 300
    result = await public_fill_evidence(LocalDB(), cmd, now=AT)
    assert result["status"] == "rejected" and result["visible_fillable_quantity"] == 200


@pytest.mark.asyncio
async def test_limit_down_no_bid_cannot_sell(monkeypatch):
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=quote(
        price=9, bid1_price=0, bid1_volume=0, ask1_price=9, ask1_volume=100)))
    result = await public_fill_evidence(LocalDB(),
        service.SubmitOrderCommand(code="000001", side="sell", price=9, quantity=100), now=AT)
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_direct_adapter_or_private_booking_rejects_even_forged_metadata():
    req = BrokerOrderRequest(order_id="fake", code="000001", side="buy", price=10, quantity=100)
    tokens = [paper._PAPER_FILL_CONTEXT.set({"committed_at":AT, "fill_round_id":"fake"}),
              paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.set(True)]
    try:
        for action in (
            PaperBrokerAdapter().place_order(None, req),
            paper._book_paper_buy(paper.SimBuyRequest(code="000001",price=10,amount=100),
                                 account_name="default",db=None),
            paper._book_paper_sell(paper.SimSellRequest(code="000001",price=10,amount=100),
                                  account_name="default",db=None),
        ):
            with pytest.raises(HTTPException) as error:
                await action
            assert error.value.status_code == 403
    finally:
        paper._PAPER_FILL_CONTEXT.reset(tokens[0])
        paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.reset(tokens[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["request", "db", "mutation", "task"])
async def test_scope_is_bound_to_exact_request_db_values_and_task(mismatch):
    db = object()
    req = BrokerOrderRequest(order_id="one", code="000001", side="buy", price=10, quantity=100)
    with _paper_execution_scope(db, req):
        with pytest.raises(HTTPException):
            if mismatch == "request":
                authorize_broker_request(db, replace(req))
            elif mismatch == "db":
                authorize_broker_request(object(), req)
            elif mismatch == "mutation":
                req.price = 12
                authorize_broker_request(db, req)
            else:
                async def child():
                    authorize_broker_request(db, req)
                await asyncio.create_task(child())
    with pytest.raises(HTTPException):
        authorize_broker_request(db, req)


@pytest.mark.asyncio
async def test_authorization_is_one_use_and_ledger_parameters_are_bound():
    db = object()
    req = BrokerOrderRequest(order_id="one",code="000001",side="buy",price=10,quantity=100)
    with _paper_execution_scope(db, req):
        authorize_broker_request(db, req)
        with pytest.raises(HTTPException):
            authorize_broker_request(db, req)
        book = paper.SimBuyRequest(code="000001",price=10,amount=100,signal_id="one")
        with pytest.raises(HTTPException):
            authorize_ledger_request(db, book, side="buy",account_name="promotion")
        authorize_ledger_request(db, book, side="buy",account_name="default")
        with pytest.raises(HTTPException):
            authorize_ledger_request(db, book, side="buy",account_name="default")


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [{}, {"trade":{}}, {"trade":{"id":1,"price":10}},
    {"trade":{"id":1,"price":10,"amount":200}}])
async def test_adapter_does_not_synthesize_missing_or_mismatched_receipt(monkeypatch, receipt):
    monkeypatch.setattr(paper, "_book_paper_buy", AsyncMock(return_value=receipt))
    with pytest.raises(ValueError, match="禁止合成"):
        await service._dispatch_broker_order(PaperBrokerAdapter(), None,
            BrokerOrderRequest(order_id="test",code="000001",side="buy",price=10,quantity=100))


@pytest_asyncio.fixture
async def public_env(paper_client, qualified_execution_risk, monkeypatch):
    client, factory = paper_client
    at = AT
    monkeypatch.setattr(paper, "_public_order_clock", lambda:at)
    monkeypatch.setattr(paper, "_paper_now", lambda:at)
    local_days = {
        at.date()-timedelta(days=i):(at.date()-timedelta(days=i)).weekday()<5 for i in range(32)}
    monkeypatch.setattr(paper.trade_calendar, "_cache", dict(local_days))
    # Holding-day statistics call _ensure_loaded even with a populated cache.
    # Keep this fixture local without bypassing real quote/risk/calendar-table gates.
    async def local_loaded(self, year):
        assert self is paper.trade_calendar and year == at.year, "unsupported public fixture calendar year"
        assert self._cache == local_days
    monkeypatch.setattr(type(paper.trade_calendar), "_ensure_loaded", local_loaded)
    calendar_network = AsyncMock(side_effect=AssertionError("public fixture forbids calendar network"))
    monkeypatch.setattr(type(paper.trade_calendar), "_sync_from_source", calendar_network)
    async with factory() as db:
        sentiment = await db.scalar(select(MarketSentiment))
        sentiment.trade_date = at.date()
        db.add(round_row("fixture-round", at))
        db.add(TradeCalendarModel(trade_date=at.date(),is_trade_day=True,session_type="full"))
        db.add(StockTag(code="000001",name="平安银行",board_type="main_sz",
                       board_tag="tradeable",is_st=False,is_delisting=False,is_suspended=False))
        db.add(StockSpot(**vars(quote(
            source_quote_at=at-timedelta(seconds=3), received_at=at-timedelta(seconds=2),
            updated_at=at-timedelta(seconds=1)))))
        await db.commit()
    yield client, factory, at
    calendar_network.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_buy_has_real_risk_order_receipt_stop_and_idempotency(public_env):
    client, factory, _ = public_env
    payload = {"code":"000001","price":10.1,"amount":100,"signal_id":"manual-proof",
               "stop_loss_price":9.3,"entry_sector_code":"BK_REAL","entry_sector_name":"银行"}
    response = await asyncio.wait_for(client.post("/paper/buy", json=payload), timeout=10)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["order"]["price"] == 10.1
    assert result["trade"]["price"] == 10
    assert result["risk"]["checked_rules"] > 0
    assert result["risk"]["evaluation_status"] == "complete"
    assert result["order"]["decision_round_id"] == "fixture-round"
    assert result["fills"][0]["broker_trade_id"] == str(result["trade"]["id"])
    replay = await client.post("/paper/buy", json=payload)
    assert replay.status_code == 200 and replay.json()["idempotent_replay"]
    conflict = await client.post("/paper/buy", json={**payload,"price":10.2})
    assert conflict.status_code == 409
    async with factory() as db:
        position = await db.scalar(select(PaperPosition))
        assert position.stop_loss_price == 9.3 and position.entry_sector_code == "BK_REAL"
        for model in (TradeOrder, TradeFill, PaperTradeLog, PaperPosition):
            assert await db.scalar(select(func.count()).select_from(model)) == 1


@pytest.mark.asyncio
async def test_public_t1_exit_preserves_position_version_and_evidence(public_env):
    client, factory, at = public_env
    buy = await client.post("/paper/buy",json={"code":"000001","price":10,"amount":100})
    assert buy.status_code == 200, buy.text
    sell = {"code":"000001","price":9.99,"amount":100}
    blocked = await client.post("/paper/sell",json=sell)
    assert blocked.status_code == 400 and "T+1" in blocked.text
    async with factory() as db:
        position = await db.scalar(select(PaperPosition))
        position.buy_time = at-timedelta(days=1)
        position.strategy_version = "legacy-position-version"
        trade = await db.scalar(select(PaperTradeLog))
        trade.trade_time = position.buy_time
        await db.commit()
    sold = await client.post("/paper/sell",json=sell)
    assert sold.status_code == 200, sold.text
    assert sold.json()["trade"]["strategy_version"] == "legacy-position-version"
    async with factory() as db:
        assert await db.scalar(select(func.count(TradeFill.id))) == 2
        assert await db.scalar(select(func.count(PaperTradeLog.id))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing_tag", "observe", "blacklist", "st"])
async def test_public_identity_hard_boundary_survives_disabled_configurable_rules(public_env, bad):
    client, factory, _ = public_env
    from app.models.stock import StockBlacklist
    async with factory() as db:
        tag = await db.get(StockTag, "000001")
        if bad == "missing_tag":
            await db.delete(tag)
        elif bad == "observe":
            tag.board_tag = "observe_only"
        elif bad == "blacklist":
            db.add(StockBlacklist(code="000001",reason="manual_review",source="manual",start_date=date.today()))
        else:
            tag.is_st = True
        await db.commit()
    # Keep another real rule enabled: this is not the empty-chain failure case.
    for rule in risk_engine._rules:
        if rule.rule_name in {"stock_blacklist", "observe_only", "blacklist", "board_restriction"}:
            rule.enabled = False
    response = await client.post("/paper/buy",json={"code":"000001","price":10,"amount":100})
    assert response.status_code == 400
    async with factory() as db:
        order = await db.scalar(select(TradeOrder))
        assert order.status == "risk_blocked"
        assert "stock_identity_boundary" in order.risk_json
        for model in (TradeFill, PaperTradeLog, PaperPosition):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["flag", "board_tag"])
async def test_suspended_sell_is_not_executable_even_with_overnight_inventory(public_env, restriction):
    client, factory, at = public_env
    bought = await client.post("/paper/buy",json={"code":"000001","price":10,"amount":100})
    assert bought.status_code == 200
    async with factory() as db:
        position = await db.scalar(select(PaperPosition))
        position.buy_time = at - timedelta(days=1)
        entry = await db.scalar(select(PaperTradeLog))
        entry.trade_time = position.buy_time
        tag = await db.get(StockTag, "000001")
        if restriction == "flag":
            tag.is_suspended = True
        else:
            tag.board_tag = "suspended"
        await db.commit()
    response = await client.post("/paper/sell",json={"code":"000001","price":9.99,"amount":100})
    assert response.status_code == 400 and "停牌" in response.text
    async with factory() as db:
        order = await db.scalar(select(TradeOrder).where(TradeOrder.side=="sell"))
        assert order.status == "risk_blocked" and "stock_identity_boundary" in order.risk_json
        assert await db.scalar(select(func.count(TradeFill.id))) == 1
        assert await db.scalar(select(func.count(PaperTradeLog.id))) == 1


@pytest.mark.asyncio
async def test_public_rejection_is_persisted_without_trade_or_fill(public_env):
    client, factory, _ = public_env
    response = await client.post("/paper/buy",json={"code":"000001","price":9.9,"amount":100})
    assert response.status_code == 400 and "五档" in response.text
    async with factory() as db:
        order = await db.scalar(select(TradeOrder))
        assert order.status == "rejected" and "paper_public_execution" in order.risk_json
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        assert await db.scalar(select(func.count(PaperTradeLog.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy","sell"])
async def test_public_challenger_cannot_be_authorized_by_forged_metadata(paper_client, side):
    client, factory = paper_client
    token = paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.set(True)
    try:
        result = await client.post(f"/paper/{side}",params={"account_name":"challenger_e"},
                                  json={"code":"000001","price":10,"amount":100})
        assert result.status_code == 403
        async with factory() as db:
            with pytest.raises(HTTPException) as error:
                await trading.create_order(trading.SubmitOrderRequest(
                    code="000001",side=side,price=10,quantity=100,account_id="challenger_e",
                    strategy_id="paper-auto-short",source="tenbagger_midline",
                    signal_id="auto-tenbagger_midline-fake"), db)
            assert error.value.status_code == 403
            assert await db.scalar(select(func.count(PaperAccount.id))) == 0
    finally:
        paper._CHALLENGER_INTERNAL_ORDER_CONTEXT.reset(token)
