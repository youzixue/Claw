"""Intraday work budgets, coalescing, cleanup and immutable snapshot boundaries."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.data.scheduler as scheduler_module
from app.config.settings import settings
from app.data.scheduler import DataScheduler, _promotion_news_source_health
from app.news.engine import news_engine


def missing_news_health():
    return {"fresh": False, "refresh_needed": True, "stale_sources": ["cninfo"]}


@pytest.fixture
def frozen_clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 7, 9, 35)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(scheduler_module, "datetime", Clock)
    return Clock


def test_successful_recent_poll_does_not_make_old_content_fresh():
    now = datetime(2026, 9, 7, 9, 35)
    contents = {source: now - timedelta(days=2) for source in ("cninfo", "em", "ths")}
    checked = {
        source: {"status": "ok", "observed_at": now - timedelta(seconds=10)}
        for source in contents
    }
    result = _promotion_news_source_health(contents, now=now, fetch_health=checked)
    assert result["fresh"] is False
    assert result["refresh_needed"] is False
    assert result["checked_without_new_content"] == ["cninfo", "em", "ths"]
    assert result["age_minutes_by_source"]["cninfo"] == 2880


@pytest.mark.parametrize("status", ["timeout", "no_data", "failed"])
def test_failed_poll_cannot_mask_stale_content(status):
    now = datetime(2026, 9, 7, 9, 35)
    result = _promotion_news_source_health(
        {"cninfo": now - timedelta(days=2)},
        now=now,
        fetch_health={"cninfo": {"status": status, "observed_at": now}},
    )
    assert result["fresh"] is False
    assert "cninfo" in result["refresh_needed_sources"]


def test_future_content_and_future_poll_fail_closed():
    now = datetime(2026, 9, 7, 9, 35)
    result = _promotion_news_source_health(
        {"cninfo": now + timedelta(hours=1)},
        now=now,
        fetch_health={"cninfo": {"status": "ok", "observed_at": now + timedelta(seconds=1)}},
    )
    assert "cninfo" in result["stale_sources"]
    assert "cninfo" in result["refresh_needed_sources"]


@pytest.mark.asyncio
async def test_background_news_requests_coalesce_and_then_cool_down(monkeypatch):
    scheduler = DataScheduler()
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_fetch(**kwargs):
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(scheduler, "_read_promotion_news_health", AsyncMock(return_value=missing_news_health()))
    monkeypatch.setattr(news_engine, "fetch_all", slow_fetch)
    try:
        assert (await scheduler._ensure_fresh_promotion_news())["status"] == "refresh_scheduled"
        task = scheduler._promotion_news_refresh_task
        assert (await scheduler._ensure_fresh_promotion_news())["status"] == "refreshing"
        assert scheduler._promotion_news_refresh_task is task
        await asyncio.wait_for(started.wait(), 1)
        release.set()
        await task
        assert scheduler._news_raw_refreshing is False
        assert scheduler._news_ai_refreshing is False
        assert (await scheduler._ensure_fresh_promotion_news())["status"] == "refresh_cooldown"
        assert scheduler._promotion_news_last_result["status"] == "no_data"
    finally:
        release.set()
        task = scheduler._promotion_news_refresh_task
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_news_checks_keep_single_owner_even_without_cooldown(monkeypatch):
    scheduler = DataScheduler()
    read_started, release_read, release_fetch = asyncio.Event(), asyncio.Event(), asyncio.Event()
    checked = 0

    async def health():
        nonlocal checked
        checked += 1
        if checked == 2:
            read_started.set()
        await release_read.wait()
        return missing_news_health()

    async def fetch(**kwargs):
        await release_fetch.wait()
        return []

    monkeypatch.setattr(scheduler, "_read_promotion_news_health", health)
    monkeypatch.setattr(news_engine, "fetch_all", fetch)
    monkeypatch.setattr(settings, "PROMOTION_NEWS_REFRESH_COOLDOWN_SEC", 0)
    requests = [asyncio.create_task(scheduler._ensure_fresh_promotion_news()) for _ in range(2)]
    try:
        await asyncio.wait_for(read_started.wait(), 1)
        release_read.set()
        results = await asyncio.gather(*requests)
        assert sorted(r["status"] for r in results) == ["refresh_scheduled", "refreshing"]
    finally:
        release_read.set()
        release_fetch.set()
        await asyncio.gather(*requests, return_exceptions=True)
        task = scheduler._promotion_news_refresh_task
        if task:
            await task


@pytest.mark.asyncio
async def test_news_refresh_timeout_releases_ownership(monkeypatch):
    scheduler = DataScheduler()
    never = asyncio.Event()
    async def slow_fetch(**kwargs):
        await never.wait()

    monkeypatch.setattr(news_engine, "fetch_all", slow_fetch)
    monkeypatch.setattr(settings, "PROMOTION_NEWS_REFRESH_TIMEOUT_SEC", 0.01)
    result = await scheduler._run_promotion_news_refresh(trigger="promotion_prediction_0935")
    assert result["status"] == "timeout"
    assert not scheduler._news_raw_refreshing
    assert not scheduler._news_ai_refreshing


@pytest.mark.asyncio
async def test_news_health_query_itself_is_bounded(monkeypatch):
    scheduler = DataScheduler()

    async def blocked_read():
        await asyncio.Event().wait()

    monkeypatch.setattr(scheduler, "_read_promotion_news_health", blocked_read)
    monkeypatch.setattr(settings, "PROMOTION_NEWS_CHECK_TIMEOUT_SEC", 0.01)
    result = await scheduler._ensure_fresh_promotion_news()
    assert result == {"status": "check_failed", "fresh": False}
    assert scheduler._promotion_news_refresh_task is None


@pytest.mark.asyncio
async def test_snapshot_timeout_cleans_up_and_has_retry_cooldown(monkeypatch, frozen_clock):
    scheduler = DataScheduler()
    cancelled = asyncio.Event()

    async def blocked_build(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(scheduler, "_build_promotion_snapshot_once", blocked_build)
    monkeypatch.setattr(settings, "PROMOTION_SNAPSHOT_TIMEOUT_SEC", 0.01)
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert result["status"] == "timeout"
    assert cancelled.is_set()
    assert scheduler._promotion_active_phase is None
    assert result["phase_timings_ms"]["calendar"] >= 0
    assert not scheduler._promotion_prediction_refreshing
    assert scheduler._promotion_prediction_task is None
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert result["status"] == "retry_cooldown"


@pytest.mark.asyncio
async def test_unexpected_snapshot_failure_is_audited_and_releases_owner(monkeypatch, frozen_clock):
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_build_promotion_snapshot_once", AsyncMock(side_effect=RuntimeError("fixture")))
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert result["status"] == "failed" and result["error_type"] == "RuntimeError"
    assert scheduler._promotion_prediction_task is None
    assert not scheduler._promotion_prediction_refreshing
    assert scheduler._promotion_active_phase is None


@pytest.mark.asyncio
async def test_pending_context_coalesces_without_dropping_newer_request(monkeypatch, frozen_clock):
    scheduler = DataScheduler()
    scheduler.scheduler = SimpleNamespace(running=True)
    started, release, next_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    contexts = []

    async def build(*args, trigger, **kwargs):
        contexts.append(trigger)
        if trigger.endswith("0925"):
            started.set()
            await release.wait()
        else:
            next_done.set()
        return {"learning": {"recorded_predictions": 1}}

    monkeypatch.setattr(scheduler, "_build_promotion_snapshot_once", build)
    first = asyncio.create_task(scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0925"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        for _ in range(3):
            result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
            assert result["status"] == "coalesced"
        # An older duplicate must not overwrite the pending 09:35 request.
        await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0925")
        assert scheduler._promotion_prediction_pending["context"] == "promotion_0935"
        release.set()
        await first
        await asyncio.wait_for(next_done.wait(), 1)
        task = scheduler._promotion_prediction_task
        if task:
            await task
        assert contexts == ["promotion_prediction_0925", "promotion_prediction_0935"]
        assert not scheduler._promotion_prediction_refreshing
        assert scheduler._promotion_prediction_pending is None
        assert (await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935"))["status"] == "already_completed"
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        task = scheduler._promotion_prediction_task
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_expired_snapshot_never_calls_builder(monkeypatch, frozen_clock):
    frozen_clock.current = datetime(2026, 9, 7, 10, 0)
    scheduler = DataScheduler()
    build = AsyncMock()
    monkeypatch.setattr(scheduler, "_build_promotion_snapshot_once", build)
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert result["status"] == "expired_window"
    build.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_clock", [datetime(2026, 9, 7, 10, 0), datetime(2026, 9, 8, 9, 35)])
async def test_pending_snapshot_is_not_backfilled_after_expiry_or_new_day(monkeypatch, frozen_clock, next_clock):
    scheduler = DataScheduler()
    scheduler.scheduler = SimpleNamespace(running=True)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def build(*args, trigger, **kwargs):
        calls.append(trigger)
        started.set()
        await release.wait()
        return {"learning": {"recorded_predictions": 1}}

    monkeypatch.setattr(scheduler, "_build_promotion_snapshot_once", build)
    first = asyncio.create_task(scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0925"))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
        frozen_clock.current = next_clock
        release.set()
        await first
        assert calls == ["promotion_prediction_0925"]
        assert scheduler._promotion_prediction_task is None
        assert scheduler._promotion_prediction_pending is None
        assert scheduler._promotion_runtime_audit[-1]["status"] == "pending_expired"
    finally:
        release.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


def test_phase_audit_records_elapsed_time_without_changing_signals(monkeypatch):
    scheduler = DataScheduler()
    ticks = iter([10.0, 10.5, 11.25, 11.5])
    # Replace this module reference, not the global event-loop monotonic clock.
    monkeypatch.setattr(scheduler_module, "_time", SimpleNamespace(monotonic=lambda: next(ticks)))
    scheduler._set_promotion_phase("news_cache_check")
    scheduler._set_promotion_phase("candidate_build")
    status = scheduler.get_pipeline_runtime_status()
    assert status["phase_timings_ms"] == {"news_cache_check": 500.0}
    assert status["active_phase"] == "candidate_build"
    assert status["active_phase_elapsed_ms"] == 750.0
    scheduler._set_promotion_phase(None)
    assert scheduler._promotion_phase_timings["candidate_build"] == 1000.0
    assert scheduler._promotion_active_phase is None


@pytest.mark.asyncio
async def test_stop_cancels_detached_work_even_when_apscheduler_is_not_running():
    scheduler = DataScheduler()
    task = asyncio.create_task(asyncio.Event().wait())
    scheduler._promotion_news_refresh_task = task
    scheduler._promotion_prediction_pending = {"context": "promotion_0935"}
    scheduler.stop()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert scheduler._promotion_prediction_pending is None


@pytest.mark.asyncio
async def test_lightweight_health_exposes_pipeline_without_database_io(monkeypatch):
    from app.main import health
    from app.data.scheduler import data_scheduler

    expected = {"contract_version": "intraday_pipeline_v2_nonblocking_news"}
    monkeypatch.setattr(data_scheduler, "get_pipeline_runtime_status", lambda: expected)
    result = await health()
    assert result["status"] == "ok"
    assert result["pipeline"] == expected
    assert "scheduler" in result


def test_runtime_status_is_bounded_and_has_deployment_contract():
    scheduler = DataScheduler()
    for index in range(105):
        scheduler._record_promotion_runtime_audit("snapshot", "test", index=index)
    status = scheduler.get_pipeline_runtime_status()
    assert status["contract_version"] == "intraday_pipeline_v2_nonblocking_news"
    assert len(scheduler._promotion_runtime_audit) == 100
    assert len(status["recent_events"]) == 30
    assert status["confirmation_windows_changed"] is False
