"""Route-only cache/concurrency tests: no DB, HTTP, producer or market truth writes."""
import asyncio
from types import SimpleNamespace

import pytest

from app.api.v1 import promotion as p


class DB:
    def __init__(self, bind):
        self.bind = bind

    def get_bind(self):
        return self.bind


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(p, "_PROMOTION_LEARNING_REVIEW_CACHE", {})
    monkeypatch.setattr(p, "_PROMOTION_LEARNING_REVIEW_FLIGHTS", {})
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(p, "time", SimpleNamespace(monotonic=lambda: clock.now))
    bind = SimpleNamespace(url="sqlite+aiosqlite:///isolated-not-opened.db")
    return clock, bind


async def wait_users(count):
    for _ in range(100):
        if sum(users for _, users in p._PROMOTION_LEARNING_REVIEW_FLIGHTS.values()) == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {count} registered requests")


def key(db, days=10):
    return (*p._promotion_cache_scope(db), days)


@pytest.mark.asyncio
async def test_same_key_cold_burst_builds_once_preserving_unknown_payload(env, monkeypatch):
    _, bind = env
    sessions = [DB(bind) for _ in range(8)]
    release = asyncio.Event()
    calls = []
    original = {"latest": {"directional_precision": None, "predicted_count": 0},
                "daily": [], "aggregate": {"evaluation_status": "unavailable"}}

    async def build(db, *, lookback_days):
        calls.append((db, lookback_days))
        await release.wait()
        return original

    monkeypatch.setattr(p, "_build_promotion_daily_learning_review", build)
    tasks = [asyncio.create_task(p.promotion_learning_review(db=db)) for db in sessions]
    try:
        await wait_users(8)
        assert calls == [(sessions[0], 10)]
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert len(calls) == 1
    assert results[0] is original
    assert all(result["cache_hit"] is True for result in results[1:])
    assert all(result["latest"]["directional_precision"] is None for result in results)
    assert "cache_hit" not in original
    assert "cache_age_seconds" not in original
    assert not p._PROMOTION_LEARNING_REVIEW_FLIGHTS


@pytest.mark.asyncio
async def test_ttl_is_unchanged_and_starts_at_build_completion(env, monkeypatch):
    clock, bind = env
    db = DB(bind)
    calls = []

    async def build(db, *, lookback_days):
        calls.append(lookback_days)
        clock.now += 9.0
        return {"generation": len(calls)}

    monkeypatch.setattr(p, "_build_promotion_daily_learning_review", build)
    assert (await p.promotion_learning_review(db=db))["generation"] == 1
    assert p._PROMOTION_LEARNING_REVIEW_CACHE[key(db)][0] == 1009.0
    clock.now += p.PROMOTION_PAGE_CACHE_TTL_SECONDS
    at_boundary = await p.promotion_learning_review(db=db)
    assert at_boundary["cache_hit"] is True
    assert at_boundary["cache_age_seconds"] == p.PROMOTION_PAGE_CACHE_TTL_SECONDS
    clock.now += 0.001
    assert (await p.promotion_learning_review(db=db))["generation"] == 2
    assert calls == [10, 10]
    assert not p._PROMOTION_LEARNING_REVIEW_FLIGHTS


@pytest.mark.asyncio
async def test_normalized_days_share_gate_and_distinct_scopes_run_independently(env, monkeypatch):
    _, bind = env
    # Same URL but a distinct engine identity must not share a result/session.
    other_bind = SimpleNamespace(url=bind.url)
    sessions = [DB(bind), DB(bind), DB(bind), DB(other_bind)]
    release = asyncio.Event()
    calls = []

    async def build(db, *, lookback_days):
        calls.append((db, lookback_days))
        await release.wait()
        return {"days": lookback_days}

    monkeypatch.setattr(p, "_build_promotion_daily_learning_review", build)
    tasks = [asyncio.create_task(p.promotion_learning_review(lookback_days=days, db=db))
             for days, db in zip([1, 3, 999, 3], sessions)]
    try:
        await wait_users(4)
        assert len(calls) == 3
        assert {days for _, days in calls} == {3, p.PROMOTION_REVIEW_MAX_DAYS}
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert not p._PROMOTION_LEARNING_REVIEW_FLIGHTS


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_failed_or_cancelled_owner_waiter_retries_using_own_session(env, monkeypatch, cancel_owner):
    _, bind = env
    owner_db, waiter_db = DB(bind), DB(bind)
    release = asyncio.Event()
    calls = []

    async def build(db, *, lookback_days):
        calls.append(db)
        if db is owner_db:
            await release.wait()
            raise ValueError("isolated read failure")
        return {"status": "ok"}

    monkeypatch.setattr(p, "_build_promotion_daily_learning_review", build)
    owner = asyncio.create_task(p.promotion_learning_review(db=owner_db))
    waiter = asyncio.create_task(p.promotion_learning_review(db=waiter_db))
    try:
        await wait_users(2)
        assert key(owner_db) not in p._PROMOTION_LEARNING_REVIEW_CACHE
        if cancel_owner:
            owner.cancel()
        else:
            release.set()
        with pytest.raises(asyncio.CancelledError if cancel_owner else ValueError):
            await owner
        assert await waiter == {"status": "ok"}
    finally:
        release.set()
        await asyncio.gather(owner, waiter, return_exceptions=True)
    assert calls == [owner_db, waiter_db]
    assert p._PROMOTION_LEARNING_REVIEW_CACHE[key(owner_db)][1] == {"status": "ok"}
    assert not p._PROMOTION_LEARNING_REVIEW_FLIGHTS


@pytest.mark.asyncio
async def test_cancelled_waiter_keeps_gate_for_owner_and_new_waiter(env, monkeypatch):
    _, bind = env
    db = DB(bind)
    release = asyncio.Event()
    calls = 0

    async def build(db, *, lookback_days):
        nonlocal calls
        calls += 1
        await release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(p, "_build_promotion_daily_learning_review", build)
    owner = asyncio.create_task(p.promotion_learning_review(db=db))
    waiter = asyncio.create_task(p.promotion_learning_review(db=DB(bind)))
    tasks = [owner, waiter]
    try:
        await wait_users(2)
        original_lock = p._PROMOTION_LEARNING_REVIEW_FLIGHTS[key(db)][0]
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await wait_users(1)
        newcomer = asyncio.create_task(p.promotion_learning_review(db=DB(bind)))
        tasks.append(newcomer)
        await wait_users(2)
        assert p._PROMOTION_LEARNING_REVIEW_FLIGHTS[key(db)][0] is original_lock
        assert calls == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert newcomer.result()["cache_hit"] is True
    assert calls == 1
    assert not p._PROMOTION_LEARNING_REVIEW_FLIGHTS


@pytest.mark.asyncio
async def test_cache_invalidation_between_requests_still_rebuilds(env, monkeypatch):
    _, bind = env
    db = DB(bind)
    calls = 0

    async def build(db, *, lookback_days):
        nonlocal calls
        calls += 1
        return {"generation": calls}

    monkeypatch.setattr(p, "_build_promotion_daily_learning_review", build)
    await p.promotion_learning_review(db=db)
    p._PROMOTION_LEARNING_REVIEW_CACHE.pop(key(db))
    assert (await p.promotion_learning_review(db=db))["generation"] == 2
    assert not p._PROMOTION_LEARNING_REVIEW_FLIGHTS


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_lone_failure_never_cached_or_leaked(env, monkeypatch, cancel):
    _, bind = env
    db = DB(bind)

    async def build(db, *, lookback_days):
        raise asyncio.CancelledError() if cancel else RuntimeError("read failed")

    monkeypatch.setattr(p, "_build_promotion_daily_learning_review", build)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await p.promotion_learning_review(db=db)
    assert key(db) not in p._PROMOTION_LEARNING_REVIEW_CACHE
    assert not p._PROMOTION_LEARNING_REVIEW_FLIGHTS
