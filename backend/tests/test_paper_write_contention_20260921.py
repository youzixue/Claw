"""Real isolated SQLite contention; no low-level rollback/retry of business state."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog, PaperTradeLog
from app.models.trading import TradeOrder
from test_paper_api import paper_client

NOW = datetime(2026, 9, 21, 10, 5)


@pytest.mark.asyncio
async def test_scan_heartbeat_releases_writer_before_slow_candidates(paper_client, monkeypatch):
    _, maker = paper_client
    monkeypatch.setattr(paper.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_TRADE_ENABLED", True)
    monkeypatch.setattr(paper, "_should_run_intraday_auto_trade", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_paper_order_window_status", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_strategy_auto_order_enabled", lambda *a, **kw: True)
    monkeypatch.setattr(paper, "experiment_active", lambda *a, **kw: True)
    monkeypatch.setattr(paper, "_is_intraday_buy_window", lambda *a, **kw: True)
    async def refresh(db, account):
        return account
    monkeypatch.setattr(paper, "_refresh_account", refresh)
    for name, value in {
        "_run_auto_t_buybacks": [], "_open_positions": [], "_today_auto_new_buy_logs": [],
        "_active_paper_order_codes": set(), "_market_sentiment_for_date": None,
    }.items():
        monkeypatch.setattr(paper, name, AsyncMock(return_value=value))
    entered = asyncio.Event()
    async def slow_candidates(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(paper, "_promotion_route_buy_candidates", slow_candidates)
    async with maker() as scanner:
        task = asyncio.create_task(paper.run_paper_auto_trade(
            scanner, account_name="promotion", include_position_risk=False, now=NOW,
            trigger="isolated-concurrency", execution_mode="intraday"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with maker() as writer:
                await writer.execute(text("PRAGMA busy_timeout=100"))
                writer.add(PaperAutoTradeLog(
                    run_id="independent-writer", trade_date=NOW.date(),
                    trigger="test", source="system", action="scan", decision="observed",
                    reason="independent audit writer"))
                await writer.commit()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await scanner.rollback()
    async with maker() as db:
        logs = list((await db.scalars(select(PaperAutoTradeLog))).all())
        assert len(logs) == 2
        assert sum(row.reason_code == "account_scan_entered" for row in logs) == 1
        assert await db.scalar(select(func.count()).select_from(TradeOrder)) == 0
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 0


@pytest.mark.asyncio
async def test_unresolved_writer_busy_is_not_swallowed_or_retried_as_success(paper_client):
    _, maker = paper_client
    async with maker() as holder, maker() as scanner:
        await holder.execute(text("BEGIN IMMEDIATE"))
        await scanner.execute(text("PRAGMA busy_timeout=50"))
        try:
            with pytest.raises(OperationalError, match="locked"):
                await paper._add_auto_log(
                    scanner, run_id="must-not-pretend-persisted", trade_date=NOW.date(),
                    trigger="test", source="system", action="scan", decision="observed",
                    reason="caller owns rollback")
            assert not scanner.is_active
        finally:
            await holder.rollback()
            await scanner.rollback()
        # Only the caller clears its failed transaction. No missing audit is
        # silently accepted and no trade is resubmitted from this low-level helper.
        await scanner.execute(text("SELECT 1"))
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(PaperAutoTradeLog)) == 0
