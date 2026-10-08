"""Temp DB / fake sources only: do not start APScheduler or production collectors."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
import app.data.scheduler as module
from app.data.scheduler import DataScheduler
from app.config.settings import settings
from app.news.engine import news_engine


@pytest.fixture
def subject(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 10, 8, 7, 43)
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(module, "datetime", Clock)
    scheduler = DataScheduler()
    class DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def scalar(self, statement): return True
    monkeypatch.setattr(module, "async_session", DB)
    return scheduler, Clock


@pytest.mark.asyncio
async def test_recovery_after_missed_raw_ai_stops_at_cutoff(subject, monkeypatch):
    scheduler, clock = subject
    raw = AsyncMock(return_value={"status": "partial"})
    ai = AsyncMock(return_value={"status": "partial"})
    monkeypatch.setattr(scheduler, "_news_raw_refresh", raw)
    monkeypatch.setattr(scheduler, "_news_ai_refresh", ai)
    result = await scheduler._news_premarket_recovery()
    assert set(result["results"]) == {"raw", "ai"}
    await scheduler._news_premarket_recovery()
    assert raw.await_count == ai.await_count == 1  # cooldown, not every tick
    clock.current = datetime(2026, 10, 8, 8)
    assert (await scheduler._news_premarket_recovery())["status"] == "outside_window"
    assert raw.await_count == ai.await_count == 1
    clock.current = datetime(2026, 10, 9, 0, 1)
    assert (await scheduler._news_premarket_recovery())["status"] == "outside_window"


@pytest.mark.asyncio
async def test_new_raw_commit_invalidates_earlier_ai_completion(subject, monkeypatch):
    scheduler, _ = subject
    scheduler._news_job_health = {
        "raw": {"status":"saved","completed_at":"2026-10-08T07:36:00+08:00"},
        "ai": {"status":"no_pending","completed_at":"2026-10-08T07:35:00+08:00"}}
    raw, ai = AsyncMock(), AsyncMock(return_value={"status":"partial"})
    monkeypatch.setattr(scheduler,"_news_raw_refresh",raw)
    monkeypatch.setattr(scheduler,"_news_ai_refresh",ai)
    await scheduler._news_premarket_recovery()
    assert raw.await_count == 0 and ai.await_count == 1


@pytest.mark.asyncio
async def test_raw_cancellation_releases_owner_and_budget_is_before_0800(subject, monkeypatch):
    scheduler, clock = subject
    started = asyncio.Event()
    async def slow(**kwargs):
        started.set()
        await asyncio.Future()
    monkeypatch.setattr(news_engine, "fetch_all", slow)
    task = asyncio.create_task(scheduler._news_raw_refresh())
    await started.wait()
    assert (await scheduler._news_raw_refresh())["status"] == "busy"
    assert (await scheduler._news_weekend_refresh())["raw"]["status"] == "busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scheduler._news_raw_refreshing is False
    clock.current = datetime(2026, 10, 8, 7, 54, 59)
    assert scheduler._news_budget() == 1
    clock.current = datetime(2026, 10, 8, 7, 55)
    assert (await scheduler._news_raw_refresh())["reason"] == "premarket_cutoff"


@pytest.mark.asyncio
async def test_ai_receipt_captures_start_watermark_not_late_finish(subject, monkeypatch):
    scheduler, _ = subject
    scheduler._news_job_health["raw"]={"status":"saved","completed_at":"2026-10-08T07:34:00+08:00"}
    async def analyze(*args,**kwargs):
        scheduler._news_job_health["raw"]={"status":"saved","completed_at":"2026-10-08T07:35:50+08:00"}
        return {"status":"analyzed","selected":1,"processed":1}
    monkeypatch.setattr(news_engine,"analyze_pending",analyze)
    result=await scheduler._news_ai_refresh()
    assert result["consumed_raw_watermark"]=="2026-10-08T07:34:00+08:00"
    assert result["consumed_raw_watermark"] != scheduler._news_job_health["raw"]["completed_at"]


@pytest.mark.asyncio
async def test_ai_does_not_fetch_and_preserves_actual_fallback_counts(subject, monkeypatch):
    scheduler, _ = subject
    fetch = AsyncMock(side_effect=AssertionError("NLP must use persisted originals"))
    monkeypatch.setattr(news_engine, "fetch_all", fetch)
    monkeypatch.setattr(news_engine, "analyze_pending", AsyncMock(return_value={
        "status": "partial", "selected": 32, "processed": 30, "ai_full": 0,
        "ai_partial": 0, "keyword": 30, "failed": 2, "selection_truncated": True}))
    result = await scheduler._news_ai_refresh()
    assert result["processed"] == 30 and result["keyword"] == 30 and result["ai_full"] == 0
    assert scheduler._news_ai_refreshing is False and fetch.await_count == 0


@pytest.mark.asyncio
async def test_recovery_calendar_unknown_or_closed_is_not_weekday_guess(subject, monkeypatch):
    scheduler, _ = subject
    class DB:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def scalar(self, statement): return None
    monkeypatch.setattr(module, "async_session", DB)
    raw = AsyncMock()
    monkeypatch.setattr(scheduler, "_news_raw_refresh", raw)
    assert (await scheduler._news_premarket_recovery())["status"] == "blocked"
    assert raw.await_count == 0


@pytest.mark.asyncio
async def test_recovery_rechecks_deadline_after_raw_before_ai(subject, monkeypatch):
    scheduler, clock = subject
    async def raw():
        clock.current = datetime(2026, 10, 8, 7, settings.NEWS_PREMARKET_RECOVERY_END_MINUTE)
        return {"status": "partial"}
    monkeypatch.setattr(scheduler, "_news_raw_refresh", raw)
    ai = AsyncMock()
    monkeypatch.setattr(scheduler, "_news_ai_refresh", ai)
    result = await scheduler._news_premarket_recovery()
    assert set(result["results"]) == {"raw"} and ai.await_count == 0
