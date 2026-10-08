"""Watchdog and event-consumer handover; no service startup or real orders."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.data import scheduler as module

AT = datetime(2026, 9, 21, 10, 30)


@pytest.fixture
def env(monkeypatch):
    class Clock(datetime):
        value = AT

        @classmethod
        def now(cls, tz=None):
            return cls.value

    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda: "morning")
    monkeypatch.setattr(module.settings, "PAPER_INTRADAY_AUTO_INTERVAL_SEC", 60)
    scheduler = module.DataScheduler()
    payload = {"round_id": "watchdog-1", "committed_at": AT, "as_of_at": AT,
               "quality_status": "ok", "watchdog_replay": True, "records": []}
    calls = []

    async def risk(frame):
        assert frame is payload
        assert scheduler._quote_dispatch_lock.locked()
        calls.append("risk")

    async def base(**kwargs):
        assert kwargs["quote_payload"] is payload
        assert kwargs["execute"] is True
        assert kwargs["include_position_risk"] is False
        assert scheduler._quote_dispatch_lock.locked()
        calls.append("base_and_E2")
        return {"status": "completed"}

    async def shadow(frame, *, execute_challengers=True):
        assert frame is payload
        assert scheduler._quote_dispatch_lock.locked()
        calls.append("shadow_execute" if execute_challengers else "shadow_evidence_only")
        return {"status": "completed" if execute_challengers else "evidence_only"}

    async def drain(frame):
        assert frame is payload
        calls.append("a2_evidence")
        return {"status": "completed"}

    monkeypatch.setattr(scheduler, "_latest_healthy_quote_payload", AsyncMock(return_value=payload))
    monkeypatch.setattr(scheduler, "_run_quote_round_position_risk", AsyncMock(side_effect=risk))
    monkeypatch.setattr(scheduler, "_run_paper_accounts_isolated", AsyncMock(side_effect=base))
    monkeypatch.setattr(scheduler, "_process_quote_round_shadow", AsyncMock(side_effect=shadow))
    monkeypatch.setattr(scheduler, "_drain_momentum_quote_rounds", AsyncMock(side_effect=drain))
    monkeypatch.setattr(scheduler, "_expire_pending_paper_buys", AsyncMock())
    return SimpleNamespace(scheduler=scheduler, payload=payload, calls=calls, clock=Clock)


async def event_once(env):
    scheduler = env.scheduler
    scheduler.scheduler = SimpleNamespace(running=True)

    class Once:
        async def wait(self):
            return

        def clear(self):
            scheduler.scheduler.running = False

    scheduler._quote_round_event = Once()
    scheduler._quote_round_payload = env.payload
    await scheduler._quote_round_loop()


@pytest.mark.asyncio
async def test_watchdog_executes_challengers_before_marking_round_complete(env):
    s = env.scheduler

    async def shadow(frame, *, execute_challengers=True):
        assert s._last_quote_round_processed_id is None
        assert s._last_quote_round_processed_at is None
        assert execute_challengers and frame is env.payload
        assert s._quote_dispatch_lock.locked()
        env.calls.append("shadow_execute")
        return {"status": "completed"}

    s._process_quote_round_shadow.side_effect = shadow
    await s._paper_intraday_auto_trade()
    assert env.calls == ["risk", "base_and_E2", "shadow_execute"]
    assert s._last_quote_round_processed_id == env.payload["round_id"]
    assert s._last_quote_round_processed_at == AT
    assert s._paper_auto_trading_last_run_at == AT
    assert not s._paper_auto_trading and not s._quote_dispatch_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["watchdog", "event"])
async def test_both_handover_directions_do_not_repeat_entries(env, first):
    s = env.scheduler
    if first == "watchdog":
        await s._paper_intraday_auto_trade()
        await event_once(env)
    else:
        await event_once(env)
        # Beyond watchdog's time debounce: round identity must still deduplicate.
        env.clock.value += timedelta(seconds=70)
        await s._paper_intraday_auto_trade()
    assert env.calls == ["risk", "base_and_E2", "shadow_execute", "a2_evidence"]
    assert s._run_paper_accounts_isolated.await_count == 1
    assert s._process_quote_round_shadow.await_count == 1


@pytest.mark.asyncio
async def test_watchdog_same_round_after_debounce_is_evidence_only(env):
    s = env.scheduler
    await s._paper_intraday_auto_trade()
    env.clock.value += timedelta(seconds=70)
    await s._paper_intraday_auto_trade()
    assert env.calls == ["risk", "base_and_E2", "shadow_execute", "a2_evidence"]
    assert s._last_quote_round_processed_at == AT  # Not a fresh trading success.


@pytest.mark.asyncio
async def test_fresh_watchdog_round_after_handover_still_runs(env):
    s = env.scheduler
    await s._paper_intraday_auto_trade()
    await event_once(env)
    env.clock.value += timedelta(seconds=70)
    env.payload.update(round_id="watchdog-2", committed_at=env.clock.value, as_of_at=env.clock.value)
    await s._paper_intraday_auto_trade()
    assert env.calls == ["risk", "base_and_E2", "shadow_execute", "a2_evidence",
                         "risk", "base_and_E2", "shadow_execute"]
    assert s._last_quote_round_processed_id == "watchdog-2"


@pytest.mark.asyncio
async def test_risk_failure_collects_evidence_without_any_entry(env):
    s = env.scheduler
    s._run_quote_round_position_risk.side_effect = ValueError("risk failed")
    await s._paper_intraday_auto_trade()
    assert env.calls == ["shadow_evidence_only"]
    assert s._run_paper_accounts_isolated.await_count == 0
    assert s._last_quote_round_processed_at is None
    assert s._last_quote_round_processed_id is None
    assert s._paper_auto_trading_last_run_at is None
    assert not s._paper_auto_trading


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["risk", "base", "shadow"])
async def test_cancellation_never_marks_complete_or_leaks_dispatch_lock(env, stage):
    s = env.scheduler
    target = {"risk": s._run_quote_round_position_risk,
              "base": s._run_paper_accounts_isolated,
              "shadow": s._process_quote_round_shadow}[stage]
    target.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await s._paper_intraday_auto_trade()
    assert s._last_quote_round_processed_id is None
    assert s._last_quote_round_processed_at is None
    assert s._paper_auto_trading_last_run_at is None
    assert not s._paper_auto_trading and not s._quote_dispatch_lock.locked()


@pytest.mark.asyncio
async def test_escaping_shadow_failure_remains_retryable_by_event_loop(env):
    s = env.scheduler
    s._process_quote_round_shadow.side_effect = [RuntimeError("dispatch failed"), {"status": "completed"}]
    await s._paper_intraday_auto_trade()
    assert s._last_quote_round_processed_id is None
    await event_once(env)
    assert s._process_quote_round_shadow.await_count == 2
    assert s._last_quote_round_processed_id == env.payload["round_id"]
    # Entry-level idempotency remains the existing executor's responsibility on retries.


@pytest.mark.asyncio
async def test_no_fresh_quotes_only_expires_pending_buys(env):
    s = env.scheduler
    s._latest_healthy_quote_payload.return_value = None
    await s._paper_intraday_auto_trade()
    s._expire_pending_paper_buys.assert_awaited_once_with(AT)
    assert env.calls == []
    assert s._last_quote_round_processed_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["recent_event", "recent_watchdog", "running", "lock", "after_close"])
async def test_existing_skip_guards_never_dispatch_challengers(env, monkeypatch, reason):
    s = env.scheduler
    if reason == "recent_event":
        s._last_quote_round_processed_at = AT - timedelta(seconds=20)
    elif reason == "recent_watchdog":
        s._paper_auto_trading_last_run_at = AT - timedelta(seconds=20)
    elif reason == "running":
        s._paper_auto_trading = True
        s._paper_auto_trading_started_at = AT
    elif reason == "lock":
        await s._quote_dispatch_lock.acquire()
    else:
        monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda: "closed")
    try:
        await s._paper_intraday_auto_trade()
        assert env.calls == []
        assert s._latest_healthy_quote_payload.await_count == 0
    finally:
        if reason == "lock":
            s._quote_dispatch_lock.release()


@pytest.mark.asyncio
async def test_lock_recheck_sees_event_consumer_completion(env):
    s = env.scheduler

    class CompletedBeforeAcquire:
        def locked(self):
            return False

        async def __aenter__(self):
            s._last_quote_round_processed_at = AT
            s._last_quote_round_processed_id = env.payload["round_id"]

        async def __aexit__(self, *args):
            return False

    s._quote_dispatch_lock = CompletedBeforeAcquire()
    await s._paper_intraday_auto_trade()
    assert not env.calls and s._latest_healthy_quote_payload.await_count == 0
    assert not s._paper_auto_trading


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [[], [{"code": "600001", "price": 10.0}]])
async def test_watchdog_uses_real_shadow_dispatch_and_original_quote_context(env, monkeypatch, records):
    env.payload["records"] = records
    from app.api.v1 import paper
    from app.paper import strategy_iteration_shadow, strategy_iteration_challenger
    s = env.scheduler
    monkeypatch.setattr(s, "_process_quote_round_shadow",
                        module.DataScheduler._process_quote_round_shadow.__get__(s))
    shapes = AsyncMock(return_value={})
    monkeypatch.setattr(strategy_iteration_shadow, "scan_strategy_iteration_shadow", shapes)
    seen = []

    async def accounts(db, *, now, account_name):
        assert paper._QUOTE_ROUND_CONTEXT.get() is env.payload
        assert now == env.payload["committed_at"]
        seen.append(account_name)
        return {"status": "completed"}

    monkeypatch.setattr(strategy_iteration_challenger, "run_strategy_iteration_challenger_accounts", accounts)
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id": "outer"})
    try:
        await s._paper_intraday_auto_trade()
        assert paper._QUOTE_ROUND_CONTEXT.get() == {"round_id": "outer"}
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert set(seen) == {"challenger_a", "challenger_b", "challenger_c",
                         "challenger_d", "challenger_f2"}
    assert shapes.await_count == 1
    assert shapes.await_args.args[1] == records
    if records:
        assert shapes.await_args.args[1] is records
    assert shapes.await_args.args[2] == AT
    assert env.calls == ["risk", "base_and_E2", "a2_evidence"]
