"""A2 evidence failures retain ordered retry state without false completion."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.data import scheduler as module
from app.paper import momentum_retest_shadow as momentum
from app.paper import strategy_iteration_shadow as shadows
from app.paper import strategy_iteration_challenger as challengers
from test_paper_watchdog_challengers_20260921 import AT, env, event_once


def real_evidence(env, monkeypatch):
    s = env.scheduler
    monkeypatch.setattr(module.settings, "PAPER_MOMENTUM_RETEST_SHADOW_ENABLED", True)
    monkeypatch.setattr(s, "_drain_momentum_quote_rounds", module.DataScheduler._drain_momentum_quote_rounds.__get__(s))
    monkeypatch.setattr(s, "_process_quote_round_shadow", module.DataScheduler._process_quote_round_shadow.__get__(s))
    scan = AsyncMock(side_effect=RuntimeError("A2 evidence commit failed"))
    execute = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(momentum, "scan_momentum_retest_shadow", scan)
    monkeypatch.setattr(shadows, "scan_strategy_iteration_shadow", AsyncMock(return_value={}))
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    return s, scan, execute


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["event", "watchdog"])
async def test_a2_failure_retains_frame_and_other_accounts_without_false_completion(env, monkeypatch, entrypoint):
    s, scan, execute = real_evidence(env, monkeypatch)
    if entrypoint == "event":
        await event_once(env)
        assert s._quote_consumer_health["status"] == "failed"
    else:
        result = await s._paper_intraday_auto_trade()
        assert result["status"] == "failed"
    assert execute.await_count == len(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE)
    assert s._last_quote_round_processed_id is None
    assert s._last_quote_round_processed_at is None
    assert s._paper_auto_trading_last_run_at is None
    assert s._paper_dispatch_incomplete is True
    assert s._momentum_quote_inflight["payload"] is env.payload
    scan.side_effect = None
    scan.return_value = {}
    # Recover the exact pending evidence frame before any later frame, without
    # rewriting its decision clock; execution idempotency belongs to the service.
    if entrypoint == "event":
        await event_once(env)
        assert s._quote_consumer_health["status"] == "completed"
    else:
        assert (await s._paper_intraday_auto_trade())["status"] == "completed"
    assert s._momentum_quote_inflight is None
    assert s._last_quote_round_processed_id == env.payload["round_id"]
    assert s._paper_dispatch_incomplete is False
    assert scan.await_count == 2
    assert all(call.args[2] == AT for call in scan.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["event_dedup", "event_degraded", "watchdog_dedup"])
async def test_evidence_only_paths_report_failure_and_preserve_old_success(env, monkeypatch, path):
    s, scan, execute = real_evidence(env, monkeypatch)
    old_success = AT - timedelta(seconds=120)
    s._last_quote_round_processed_id = env.payload["round_id"]
    s._last_quote_round_processed_at = old_success
    if path == "event_degraded":
        env.payload["quality_status"] = "degraded"
    if path == "watchdog_dedup":
        result = await s._paper_intraday_auto_trade()
        assert result["status"] == "failed"
    else:
        await event_once(env)
        assert s._quote_consumer_health["status"] == "failed"
    assert s._paper_dispatch_incomplete is True
    assert s._last_quote_round_processed_id == env.payload["round_id"]
    assert s._last_quote_round_processed_at == old_success
    assert s._momentum_quote_inflight["payload"] is env.payload
    execute.assert_not_awaited()
    s._run_paper_accounts_isolated.assert_not_awaited()
    assert scan.await_count == 1


@pytest.mark.asyncio
async def test_invalid_evidence_clock_is_not_a_completion_receipt(env, monkeypatch):
    s, scan, execute = real_evidence(env, monkeypatch)
    result = await s._drain_momentum_quote_rounds({"committed_at": None})
    assert result["status"] == "failed"
    assert result["reason"] == "invalid_evidence_clock"
    scan.assert_not_awaited()
    execute.assert_not_awaited()
