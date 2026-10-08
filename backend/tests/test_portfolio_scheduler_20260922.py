"""Shared allocator must participate in normal/watchdog completion and risk."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.data import scheduler as module
from app.models.paper import PaperAccount
from app.paper import portfolio
from app.paper.account_policy import ACCOUNT_NAMES
from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
from test_paper_watchdog_challengers_20260921 import AT, env, event_once
from test_paper_position_risk_transactions import risk_env


@pytest.fixture
def active(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ACTIVATION_AT", "2026-09-21 09:30:00")


def receipts(env):
    round_id = env.payload["round_id"]
    rows = [{"account_name": name, "status": "completed", "quote_round_id": round_id,
             "source_scan_status": "completed"}
            for name in ACCOUNT_NAMES]
    return {"status": "completed", "accounts": rows[:7]}, {
        "status": "completed", "challenger_accounts": rows[7:]}


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["event", "watchdog"])
async def test_portfolio_finishes_before_round_mark_and_uses_same_context(env, active, monkeypatch, entrypoint):
    s = env.scheduler
    primary, connected = receipts(env)
    s._run_paper_accounts_isolated.side_effect = None
    s._run_paper_accounts_isolated.return_value = primary
    s._process_quote_round_shadow.side_effect = None
    s._process_quote_round_shadow.return_value = connected
    async def run(db, **kwargs):
        assert s._last_quote_round_processed_id is None
        assert s._quote_dispatch_lock.locked()
        assert paper._quote_round_context() is env.payload
        assert all(row["status"] == "completed" and row["quote_round_id"] == env.payload["round_id"]
                   for row in kwargs["source_receipts"].values())
        assert set(kwargs["source_receipts"]) == set(ACCOUNT_NAMES)
        assert kwargs["allow_entries"] is True and kwargs["manage_positions"] is False
        return {"status": "completed", "allocated": 0}
    mock = AsyncMock(side_effect=run)
    monkeypatch.setattr(portfolio, "run_shared_portfolio", mock)
    if entrypoint == "event":
        await event_once(env)
    else:
        await s._paper_intraday_auto_trade()
    assert mock.await_count == 1
    assert s._last_quote_round_processed_id == env.payload["round_id"]
    assert paper._quote_round_context() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["event", "watchdog"])
@pytest.mark.parametrize("failure", [RuntimeError("fixture shared commit failed"), {"status": "degraded"}, None])
async def test_shared_failure_keeps_round_recoverable(env, active, monkeypatch, entrypoint, failure):
    s = env.scheduler
    primary, connected = receipts(env)
    s._run_paper_accounts_isolated.side_effect = None
    s._run_paper_accounts_isolated.return_value = primary
    s._process_quote_round_shadow.side_effect = None
    s._process_quote_round_shadow.return_value = connected
    mock = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    monkeypatch.setattr(portfolio, "run_shared_portfolio", mock)
    if entrypoint == "event":
        await event_once(env)
        assert s._quote_consumer_health["status"] == "failed"
    else:
        assert (await s._paper_intraday_auto_trade())["status"] == "failed"
    assert s._last_quote_round_processed_id is None
    assert s._last_quote_round_processed_at is None
    assert s._paper_dispatch_incomplete
    assert not s._quote_dispatch_lock.locked()


@pytest.mark.asyncio
async def test_missing_wrong_round_or_failed_source_receipts_are_not_guessed(env, active, monkeypatch):
    primary, connected = receipts(env)
    primary["accounts"][0]["quote_round_id"] = "other-round"
    primary["status"] = "failed"
    connected["challenger_accounts"].pop()
    mock = AsyncMock(return_value={"status": "degraded"})
    monkeypatch.setattr(portfolio, "run_shared_portfolio", mock)
    await env.scheduler._run_shared_portfolio_isolated(env.payload, primary, connected)
    args = mock.call_args.kwargs
    assert args["allow_entries"] is True  # healthy origins are not blocked by top-level failure
    assert args["source_receipts"]["default"]["quote_round_id"] == "other-round"
    assert ACCOUNT_NAMES[-1] not in args["source_receipts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "unknown", "not_scanned", "degraded", "completed"])
async def test_business_completion_does_not_forge_source_health(env, active, monkeypatch, state):
    primary, connected = receipts(env)
    primary["accounts"][0]["source_scan_status"] = state
    mock = AsyncMock(return_value={"status": "completed" if state == "completed" else "degraded"})
    monkeypatch.setattr(portfolio, "run_shared_portfolio", mock)
    await env.scheduler._run_shared_portfolio_isolated(env.payload, primary, connected)
    health, failures = portfolio.source_health_for_round(
        mock.call_args.kwargs["source_receipts"], env.payload["round_id"])
    assert health["default"] is (state == "completed")


@pytest.mark.asyncio
async def test_primary_scan_receipt_preserves_explicit_source_issues(env, monkeypatch):
    s = env.scheduler
    monkeypatch.setattr(paper, "PAPER_SCAN_ACCOUNTS", ("default",))
    expected = [{"source": "next_day_plan", "reason_code": "source_read_failed"}]
    monkeypatch.setattr(paper, "run_paper_auto_trade", AsyncMock(return_value={
        "run_id": "fixture-run", "summary": {"source_scan_status": "degraded", "source_scan_issues": expected}}))
    result = await module.DataScheduler._run_paper_accounts_isolated(s,
        execute=True, trigger="fixture", execution_mode="intraday", quote_payload=env.payload)
    assert result["status"] == "completed"  # dispatch acknowledged, not a healthy source claim
    assert result["accounts"][0]["source_scan_status"] == "degraded"
    assert result["accounts"][0]["source_scan_issues"] == expected
    assert result["accounts"][0]["quote_round_id"] == env.payload["round_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["event", "watchdog"])
async def test_partial_sources_allocate_without_falsely_completing_round(env, active, monkeypatch, entrypoint):
    s = env.scheduler
    primary, connected = receipts(env)
    primary["status"] = "failed"
    primary["accounts"][0]["status"] = "failed"
    s._run_paper_accounts_isolated.side_effect = None
    s._run_paper_accounts_isolated.return_value = primary
    s._process_quote_round_shadow.side_effect = None
    s._process_quote_round_shadow.return_value = connected
    async def run(db, **kwargs):
        assert kwargs["allow_entries"] is True
        health, failures = portfolio.source_health_for_round(kwargs["source_receipts"], env.payload["round_id"])
        assert not health["default"] and health["promotion"] and health["challenger_c"]
        return {"status": "degraded", "allocated": 1, "source_failures": failures}
    mock = AsyncMock(side_effect=run)
    monkeypatch.setattr(portfolio, "run_shared_portfolio", mock)
    if entrypoint == "event":
        await event_once(env)
    else:
        assert (await s._paper_intraday_auto_trade())["status"] == "failed"
    assert mock.await_count == 1
    assert s._last_quote_round_processed_id is None and s._paper_dispatch_incomplete


@pytest.mark.asyncio
async def test_duplicate_receipt_never_overwrites_failure_with_success(env, active, monkeypatch):
    primary, connected = receipts(env)
    primary["accounts"].insert(0, {**primary["accounts"][0], "status": "failed"})
    mock = AsyncMock(return_value={"status": "degraded", "allocated": 0})
    monkeypatch.setattr(portfolio, "run_shared_portfolio", mock)
    await env.scheduler._run_shared_portfolio_isolated(env.payload, primary, connected)
    health, _ = portfolio.source_health_for_round(mock.call_args.kwargs["source_receipts"], env.payload["round_id"])
    assert not health["default"] and health["promotion"]


@pytest.mark.asyncio
async def test_summary_failure_not_forged_completed_even_with_good_leaf_receipts(env, active, monkeypatch):
    primary, connected = receipts(env)
    primary["status"] = "failed"
    monkeypatch.setattr(portfolio, "run_shared_portfolio", AsyncMock(return_value={"status":"completed","allocated":1}))
    result = await env.scheduler._run_shared_portfolio_isolated(env.payload, primary, connected)
    assert result["status"] == "degraded" and result["allocated"] == 1
    assert result["dispatch_failures"] == [{"dispatch":"primary","status":"failed"}]


@pytest.mark.asyncio
async def test_disabled_never_invokes_allocator(env, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
    mock = AsyncMock()
    monkeypatch.setattr(portfolio, "run_shared_portfolio", mock)
    result = await env.scheduler._run_shared_portfolio_isolated(env.payload, None, None)
    assert result["status"] == "completed" and result["allocation_status"] == "disabled"
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_created_wallet_remains_in_risk_and_expiry_roster_when_disabled(risk_env, monkeypatch):
    s, maker, payload = risk_env
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ACTIVATION_AT", "")
    assert PORTFOLIO_ACCOUNT not in await s._paper_risk_account_names()
    async with maker() as db:
        db.add(PaperAccount(account_name=PORTFOLIO_ACCOUNT, initial_capital=50000,
                           current_capital=50000, total_assets=50000, status="active"))
        await db.commit()
    assert PORTFOLIO_ACCOUNT in await s._paper_risk_account_names()
    expire = AsyncMock(return_value=0)
    monkeypatch.setattr(paper, "expire_pending_paper_buys", expire)
    await s._expire_pending_paper_buys(payload["committed_at"])
    assert PORTFOLIO_ACCOUNT in [call.kwargs["account_name"] for call in expire.call_args_list]
