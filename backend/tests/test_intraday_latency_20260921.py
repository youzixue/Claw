"""Deterministic dispatch contention; isolated DB from conftest, no live I/O."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.data import scheduler as module


@pytest.mark.asyncio
@pytest.mark.parametrize("old_quality,new_quality", [
    ("ok", "ok"), ("ok", "degraded"), ("degraded", "ok"),
    ("degraded", "degraded"),
])
async def test_lock_wait_coalesces_latest_without_losing_evidence(monkeypatch, old_quality, new_quality):
    s = module.DataScheduler()
    s.scheduler = SimpleNamespace(running=True)
    monkeypatch.setattr(module.settings, "PAPER_MOMENTUM_RETEST_SHADOW_ENABLED", True)
    at = datetime(2026, 9, 21, 10, 30)
    old = dict(round_id="old", committed_at=at, as_of_at=at,
               quality_status=old_quality, records=[])
    new = dict(round_id="new", committed_at=at + timedelta(seconds=30),
               as_of_at=at + timedelta(seconds=29), quality_status=new_quality, records=[])
    lock = asyncio.Lock()
    waiting = asyncio.Event()

    class Contended:
        async def __aenter__(self):
            waiting.set()
            await lock.acquire()
        async def __aexit__(self, *args):
            lock.release()

    s._quote_dispatch_lock = Contended()
    seen = []

    async def risk(payload):
        seen.append(("risk", payload["round_id"]))

    async def entries(**kwargs):
        seen.append(("entries", kwargs["quote_payload"]["round_id"]))
        assert kwargs["include_position_risk"] is False
        return {"status": "completed"}

    async def evidence(payload, **kwargs):
        seen.append(("evidence", payload["round_id"]))
        s.scheduler.running = False
        return {"status": "completed"}

    s._run_quote_round_position_risk = AsyncMock(side_effect=risk)
    s._run_paper_accounts_isolated = AsyncMock(side_effect=entries)
    s._process_quote_round_shadow = AsyncMock(side_effect=evidence)
    s._drain_momentum_quote_rounds = AsyncMock(side_effect=evidence)
    s._expire_pending_paper_buys = AsyncMock()
    await lock.acquire()
    s._publish_quote_round(old)
    task = asyncio.create_task(s._quote_round_loop())
    try:
        await asyncio.wait_for(waiting.wait(), timeout=2)
        s._publish_quote_round(new)
        lock.release()
        await asyncio.wait_for(task, timeout=2)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    expected = ([("risk", "new"), ("entries", "new")] if new_quality == "ok" else [])
    assert seen == expected + [("evidence", "new")]
    assert [entry["payload"]["round_id"] for entry in s._momentum_quote_inbox] == ["old", "new"]
    assert s._expire_pending_paper_buys.await_count == int(new_quality != "ok")
    assert not s._quote_round_event.is_set()  # No redundant next wake for the selected frame.
    health = s._quote_consumer_health
    assert health["round_id"] == "new"
    assert health["round_as_of_at"] == str(new["as_of_at"])
    assert health["round_collected_at"] == str(new["committed_at"])
    assert health["lock_wait_ms"] >= 0
    assert health["publish_to_start_ms"] >= 0
    assert health["dispatch_ms"] >= 0
    assert health["status"] == ("completed" if new_quality == "ok" else "degraded")


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_ms", [0.0, 40.0], ids=["disabled-shared", "timed-shared-stage"])
async def test_publication_during_dispatch_is_not_lost_and_stages_use_monotonic(monkeypatch, shared_ms):
    s = module.DataScheduler()
    s.scheduler = SimpleNamespace(running=True)
    monkeypatch.setattr(module.settings, "PAPER_MOMENTUM_RETEST_SHADOW_ENABLED", False)
    monkeypatch.setattr(module.settings, "PAPER_PORTFOLIO_ENABLED", False)
    ticks = [10.0]
    monkeypatch.setattr(module, "_time", SimpleNamespace(monotonic=lambda: ticks[0]))
    at = datetime(2026, 9, 21, 10, 30)
    first = dict(round_id="first", committed_at=at, quality_status="ok", records=[])
    second = dict(round_id="second", committed_at=at + timedelta(seconds=30),
                  quality_status="ok", records=[])
    seen, timings = [], []

    async def risk(payload):
        seen.append(("risk", payload["round_id"]))
        ticks[0] += 0.010
        if payload is first:
            s._publish_quote_round(second)

    async def entries(**kwargs):
        seen.append(("entries", kwargs["quote_payload"]["round_id"]))
        ticks[0] += 0.020
        return {"status": "completed"}

    async def shadow(payload):
        timings.append(s._quote_consumer_health)
        ticks[0] += 0.030
        if payload is second:
            s.scheduler.running = False
        return {"status": "completed"}

    s._run_quote_round_position_risk = risk
    s._run_paper_accounts_isolated = entries
    s._process_quote_round_shadow = shadow
    shared_calls = []
    if shared_ms:
        # Timing-only stage double. The zero case retains the real disabled path;
        # execution and source-receipt gates are covered by portfolio scheduler tests.
        async def shared(payload, primary, connected):
            shared_calls.append(payload["round_id"])
            ticks[0] += shared_ms / 1000
            return {"status": "completed", "allocated": 0}
        s._run_shared_portfolio_isolated = shared
    s._publish_quote_round(first)
    await asyncio.wait_for(s._quote_round_loop(), timeout=2)
    assert seen == [("risk", "first"), ("entries", "first"),
                    ("risk", "second"), ("entries", "second")]
    assert [item["stages_ms"] for item in timings] == [
        dict(position_risk=10.0, entries=20.0, shadow_and_challengers=30.0, shared_portfolio=shared_ms),
        dict(position_risk=10.0, entries=20.0, shadow_and_challengers=30.0, shared_portfolio=shared_ms),
    ]
    assert [item["dispatch_ms"] for item in timings] == [60.0 + shared_ms, 60.0 + shared_ms]
    assert timings[1]["publish_to_start_ms"] == 50.0 + shared_ms
    assert shared_calls == (["first", "second"] if shared_ms else [])
    if not shared_ms:
        assert all(item["shared_portfolio"]["allocation_status"] == "disabled" for item in timings)
    assert not s._quote_round_event.is_set()
    assert "created_at" not in timings[0]  # No invented event/DB clock.


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["duplicate", "failure", "cancel", "empty"])
async def test_terminal_diagnostics_and_lock_release(monkeypatch, outcome):
    s = module.DataScheduler()
    s.scheduler = SimpleNamespace(running=True)
    payload = dict(round_id="round", committed_at=datetime(2026, 9, 21, 10, 30),
                   quality_status="ok", records=[])
    s._quote_round_payload = None if outcome == "empty" else payload
    s._quote_round_event.set()
    s._last_quote_round_processed_id = "round" if outcome == "duplicate" else None

    async def risk(_):
        if outcome == "cancel":
            raise asyncio.CancelledError
        raise ValueError("isolated risk failure")

    async def stop(*args, **kwargs):
        s.scheduler.running = False
        return {"status": "completed"}

    s._run_quote_round_position_risk = AsyncMock(side_effect=risk)
    s._run_paper_accounts_isolated = AsyncMock()
    s._process_quote_round_shadow = AsyncMock(side_effect=stop)
    s._drain_momentum_quote_rounds = AsyncMock(side_effect=stop)
    if outcome == "empty":
        # Stop after this wake without changing real asyncio.Event semantics.
        original_clear = s._quote_round_event.clear
        def clear():
            original_clear()
            s.scheduler.running = False
        monkeypatch.setattr(s._quote_round_event, "clear", clear)
    if outcome == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await s._quote_round_loop()
    else:
        await asyncio.wait_for(s._quote_round_loop(), timeout=2)
    assert not s._quote_dispatch_lock.locked()
    assert not s._quote_round_event.is_set()
    s._run_paper_accounts_isolated.assert_not_awaited()
    expected = {"duplicate": "deduplicated", "failure": "failed",
                "cancel": "canceled", "empty": "not_run"}[outcome]
    assert s._quote_consumer_health["status"] == expected
    if outcome != "empty":
        assert s._quote_consumer_health["publish_to_start_ms"] is None
        assert "actual_finished_at" in s._quote_consumer_health
    if outcome == "failure":
        s._process_quote_round_shadow.assert_awaited_once_with(payload, execute_challengers=False)
        assert s._last_quote_round_processed_id is None
