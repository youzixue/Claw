"""Scheduling only; collectors, clocks and sessions are isolated, never live feeds."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.data import scheduler as module
from app.data.scheduler import DataScheduler
from app.strategy.auction import auction_collector


@pytest.fixture
def env(monkeypatch):
    sessions = []
    class Session:
        async def __aenter__(self):
            sessions.append(self)
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(module, "async_session", Session)
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    return DataScheduler(), sessions


@pytest.mark.asyncio
async def test_tencent_job_uses_one_session(env, monkeypatch):
    """腾讯终场任务是独立任务：一次触发只跑腾讯，用一次 session。

    2026-09-29 实测教训：把腾讯与东财放在同一任务里顺序跑，腾讯一轮（约 35s）
    会把整个 09:25:00–09:25:30 窗口吃掉 —— 当天东财**一次都没被触发**
    （日志 `skipped: maximum number of running instances`），于是每只仍然只有
    1 个不同源时刻。现在两者各自独立任务、并行运行。
    """
    scheduler, sessions = env
    starts = []
    tencent = AsyncMock(return_value={"status": "ok", "written": 1})
    eastmoney = AsyncMock(return_value={"status": "ok", "written": 1})
    monkeypatch.setattr(auction_collector, "collect_tencent_auction_evidence", tencent)
    monkeypatch.setattr(auction_collector, "collect_eastmoney_auction_evidence", eastmoney)
    await asyncio.wait_for(scheduler._auction_collect_tencent_evidence(), 1)
    tencent.assert_awaited_once()
    eastmoney.assert_not_awaited(), "东财必须由它自己的任务触发，不再被腾讯挤掉"
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_eastmoney_job_is_independent_and_uses_own_session(env, monkeypatch):
    scheduler, sessions = env
    tencent = AsyncMock(return_value={"status": "ok", "written": 1})
    eastmoney = AsyncMock(return_value={"status": "ok", "written": 1})
    monkeypatch.setattr(auction_collector, "collect_tencent_auction_evidence", tencent)
    monkeypatch.setattr(auction_collector, "collect_eastmoney_auction_evidence", eastmoney)
    await asyncio.wait_for(scheduler._auction_collect_eastmoney_evidence(), 1)
    eastmoney.assert_awaited_once()
    tencent.assert_not_awaited()
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_one_source_failure_does_not_disable_the_other(env, monkeypatch):
    """任一来源失败都不得串行抑制另一任务；来源独立不等于不同源时刻。"""
    scheduler, _ = env
    tencent = AsyncMock(side_effect=RuntimeError("isolated source failure"))
    eastmoney = AsyncMock(return_value={"status": "ok", "written": 1})
    monkeypatch.setattr(auction_collector, "collect_tencent_auction_evidence", tencent)
    monkeypatch.setattr(auction_collector, "collect_eastmoney_auction_evidence", eastmoney)
    await scheduler._auction_collect_tencent_evidence()
    await scheduler._auction_collect_eastmoney_evidence()
    tencent.assert_awaited_once()
    eastmoney.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_day_does_not_sample(env, monkeypatch):
    scheduler, sessions = env
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=False))
    await scheduler._auction_collect_tencent_evidence()
    assert sessions == []


def test_final_sample_accepts_short_scheduler_lag_without_reentrancy():
    scheduler = DataScheduler()
    scheduler.setup_jobs()
    for job_id in ("auction_collect_0925", "auction_evidence_0925", "auction_evidence_eastmoney_0925"):
        job = scheduler.scheduler.get_job(job_id)
        assert job.misfire_grace_time == 5
        assert job.coalesce is True
        assert job.max_instances == 1


@pytest.mark.asyncio
async def test_slow_tencent_does_not_prevent_eastmoney_start(env, monkeypatch):
    scheduler, sessions = env
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return {"status": "ok", "written": 1}

    eastmoney = AsyncMock(return_value={"status": "ok", "written": 1})
    monkeypatch.setattr(auction_collector, "collect_tencent_auction_evidence", slow)
    monkeypatch.setattr(auction_collector, "collect_eastmoney_auction_evidence", eastmoney)
    task = asyncio.create_task(scheduler._auction_collect_tencent_evidence())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(scheduler._auction_collect_eastmoney_evidence(), 1)
        eastmoney.assert_awaited_once()
        assert not task.done()
        assert len(sessions) == 2 and sessions[0] is not sessions[1]
    finally:
        release.set()
        await task

