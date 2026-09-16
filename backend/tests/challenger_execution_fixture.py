"""Explicit execution fixture for Challenger integration cases, never autouse.

Runs real registered RiskEngine, quote validator, service, broker and books. Only
test clocks/calendar/data are supplied; existing prices/depth/invalid fields stay.
This does not certify production risk configuration or strategy profitability.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import pytest_asyncio
from sqlalchemy import select

from app.api.v1 import paper
from app.models.governance import TradeCalendarModel
from app.models.stock import MarketSentiment, QuoteRound, StockSpot
from app.paper.strategy_iteration_challenger import run_strategy_iteration_challenger_accounts
from app.risk.engine import risk_engine
from app.risk.rules import register_all_rules
from app.trading.service import submit_order


@pytest_asyncio.fixture
async def qualified_challenger_execution(challenger_env, monkeypatch):
    monkeypatch.setattr(risk_engine, "_rules", [])
    register_all_rules()
    checked = []
    real_check = risk_engine.check
    def check(ctx):
        result = real_check(ctx)
        checked.append(result)
        return result
    monkeypatch.setattr(risk_engine, "check", check)
    clock = {"at": datetime(2026, 9, 1, 10)}
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock["at"])
    real_now = paper._paper_now
    def now():
        if paper._PAPER_FILL_CONTEXT.get() or paper._QUOTE_ROUND_CONTEXT.get():
            return real_now()
        return clock["at"]
    monkeypatch.setattr(paper, "_paper_now", now)

    @asynccontextmanager
    async def frame(db, at):
        clock["at"] = at
        assert at.year == 2026 and at.month == 9
        if await db.get(TradeCalendarModel, at.date()) is None:
            db.add(TradeCalendarModel(trade_date=at.date(), is_trade_day=at.weekday()<5,
                                     session_type="full" if at.weekday()<5 else "closed"))
        sentiment = await db.scalar(select(MarketSentiment).where(
            MarketSentiment.trade_date == at.date()))
        if sentiment is None:
            sentiment = MarketSentiment(
                trade_date=at.date(), sentiment_cycle="recovery", sentiment_score=60,
                limit_up_count=40, limit_down_count=5, broken_limit_count=8, seal_rate=70,
                board_height=3, advance_decline_ratio=1.2, turnover_total=1.2,
                main_net_inflow=20, quality_status="ok", calculation_version="isolated_challenger")
            db.add(sentiment)
        sentiment.observed_at = at-timedelta(seconds=2)
        # Exact fixture observation frame. No price/depth/identity field correction.
        records = [dict(row) for row in (await db.execute(
            select(*StockSpot.__table__.columns))).mappings().all()]
        round_id = "isolated-challenger:" + at.isoformat()
        for record in records:
            original_at = record["updated_at"]
            assert isinstance(original_at, datetime), "fixture must provide its observation clock"
            record.update(source_quote_at=original_at-timedelta(seconds=2),
                          received_at=original_at-timedelta(seconds=1),
                          quote_round_id=round_id)
        as_of = max((row["source_quote_at"] for row in records), default=at)
        if not await db.scalar(select(QuoteRound.id).where(QuoteRound.round_id==round_id)):
            db.add(QuoteRound(round_id=round_id, source="tencent", trade_date=at.date(),
                as_of_at=as_of, committed_at=at, expected_count=len(records),
                received_count=len(records), source_time_count=len(records),
                coverage=1, source_time_coverage=1, quality_status="ok",
                config_version="isolated-fixture", code_version="isolated-fixture"))
        await db.commit()
        payload = {"round_id":round_id, "committed_at":at, "as_of_at":as_of,
                   "trade_date":at.date(), "quality_status":"ok",
                   "config_version":"isolated-fixture", "code_version":"isolated-fixture",
                   "records":records, "records_by_code":{r["code"]:r for r in records}}
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            yield payload
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

    async def run(db, *, now, command=None, **kwargs):
        async with frame(db, now):
            if command is not None:
                return await submit_order(db, command)
            return await run_strategy_iteration_challenger_accounts(db, now=now, **kwargs)
    run.frame = frame
    run.risk_results = checked
    yield run
    # If service was reached, registration must have been real and complete.
    assert all(r["checked_rules"] > 0 and r["evaluation_status"] == "complete" for r in checked)
