"""Single-flight must survive calendar awaits, setup failure and cancellation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.events import EVENT_JOB_EXECUTED
from app.data import scheduler as module
from app.data.scheduler import DataScheduler
from app.strategy.auction import auction_scheduler


@pytest.fixture
def env(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(module, "async_session", Session)
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda: "pre_auction")
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    return DataScheduler()


@pytest.mark.asyncio
async def test_session_open_failure_does_not_wedge_future_collection(env, monkeypatch):
    class Broken:
        async def __aenter__(self):
            raise RuntimeError("isolated open failure")
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(module, "async_session", Broken)
    try:
        result = await env._auction_collect(force=True)
    except RuntimeError:
        result = None
    assert env._auction_collect_refreshing is False
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_calendar_await_cannot_admit_two_collectors(env, monkeypatch):
    both_checked = asyncio.Event()
    release = asyncio.Event()
    entered = 0
    calls = []
    async def calendar():
        nonlocal entered
        entered += 1
        if entered == 2:
            both_checked.set()
        await both_checked.wait()
        return True
    async def collect(*args, **kwargs):
        calls.append(1)
        await release.wait()
        return {"status": "ok", "saved": 1}
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", calendar)
    monkeypatch.setattr(auction_scheduler, "run_auction_phase", collect)
    tasks = [asyncio.create_task(env._auction_collect(force=True)) for _ in range(2)]
    try:
        await asyncio.wait_for(both_checked.wait(), 1)
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(calls) == 1
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert sorted(r["status"] for r in results) == ["degraded", "ok"]
    assert env._auction_collect_refreshing is False


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_auction_collect_force", "_auction_collect_force_0920",
                                    "_auction_collect_force_0924", "_auction_collect_force_0925"])
async def test_forced_callbacks_preserve_business_failure(env, monkeypatch, method):
    monkeypatch.setattr(auction_scheduler, "run_auction_phase", AsyncMock(return_value={"status": "no_data"}))
    result = await getattr(env, method)()
    assert result["status"] == "degraded"
    assert result["reason"] == "no_data"
    event = SimpleNamespace(code=EVENT_JOB_EXECUTED, job_id="auction_collect_0925",
                            retval=result, exception=None)
    assert env._pipeline_runtime_health.observe_job(event)["status"] == "business_degraded"


@pytest.mark.asyncio
async def test_busy_is_not_reported_as_success(env, monkeypatch):
    collect = AsyncMock()
    monkeypatch.setattr(auction_scheduler, "run_auction_phase", collect)
    env._auction_collect_refreshing = True
    result = await env._auction_collect(force=True)
    assert result == {"status": "degraded", "reason": "previous_running"}
    collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_collection_releases_guard_and_propagates(env, monkeypatch):
    monkeypatch.setattr(auction_scheduler, "run_auction_phase", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await env._auction_collect(force=True)
    assert env._auction_collect_refreshing is False


@pytest.mark.asyncio
async def test_cleanup_failure_must_not_convert_cancel_to_business_result(env, monkeypatch):
    cancellation = asyncio.CancelledError("isolated owner cancellation")
    class BrokenClose:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            raise RuntimeError("isolated cleanup error")
    monkeypatch.setattr(module, "async_session", BrokenClose)
    monkeypatch.setattr(auction_scheduler, "run_auction_phase", AsyncMock(side_effect=cancellation))
    with pytest.raises(asyncio.CancelledError) as caught:
        await env._auction_collect(force=True)
    assert caught.value is cancellation
    assert env._auction_collect_refreshing is False


@pytest.mark.asyncio
async def test_cleanup_failure_after_success_is_failed_not_success(env, monkeypatch):
    class BrokenClose:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            raise RuntimeError("isolated cleanup error")
    monkeypatch.setattr(module, "async_session", BrokenClose)
    monkeypatch.setattr(auction_scheduler, "run_auction_phase", AsyncMock(return_value={"status": "ok"}))
    result = await env._auction_collect(force=True)
    assert result == {"status": "failed", "error_type": "RuntimeError"}
    assert env._auction_collect_refreshing is False


@pytest.mark.asyncio
async def test_closed_day_does_not_open_session(env, monkeypatch):
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=False))
    collect = AsyncMock()
    monkeypatch.setattr(auction_scheduler, "run_auction_phase", collect)
    await env._auction_collect(force=True)
    collect.assert_not_awaited()
    assert env._auction_collect_refreshing is False
