"""Scheduler owns per-account rollback/retry, never replaying uncertain commits."""
import asyncio
from datetime import timedelta
import sqlite3
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.api.v1 import paper
from app.data import scheduler as module
from app.models.paper import PaperAccount
from app.paper import strategy_iteration_challenger as challengers
from app.paper import strategy_iteration_shadow as shadows
from app.trading.paper_authorization import mark_paper_execution_uncertain
from test_paper_position_risk_transactions import AT, risk_env


@pytest.fixture
def dispatch(risk_env, monkeypatch):
    s, maker, payload = risk_env
    monkeypatch.setattr(paper, "PAPER_CHALLENGER_ACCOUNT_BY_ROUTE",
                        {"route_a": "challenger_a", "route_b": "challenger_b"})
    monkeypatch.setattr(s, "_drain_momentum_quote_rounds", AsyncMock(return_value={"status": "completed"}))
    monkeypatch.setattr(shadows, "scan_strategy_iteration_shadow", AsyncMock(return_value={}))
    return s, maker, payload


@pytest.mark.asyncio
async def test_busy_snapshot_owner_retries_fresh_session_and_keeps_other_account(dispatch, monkeypatch):
    s, maker, payload = dispatch
    seen, sessions, recovered, codes = [], [], [], []
    async def execute(db, *, now, account_name):
        assert now == AT and paper._QUOTE_ROUND_CONTEXT.get() is payload
        seen.append(account_name)
        sessions.append(db)
        if len(seen) == 1:
            await db.execute(text("BEGIN"))
            row = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == "challenger_a"))
            async with maker() as writer:
                await writer.execute(text("UPDATE paper_account SET current_capital=49000 WHERE account_name='challenger_a'"))
                await writer.commit()
            try:
                row.current_capital = 48000
                await db.flush()
            except OperationalError as exc:
                codes.append(exc.orig.sqlite_errorcode)
                raise
            pytest.fail("WAL fixture did not produce stale snapshot")
        if account_name == "challenger_a":
            row = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
            assert row.current_capital == 49000
        return {"status": "completed", "entries": 0}
    async def recover(db):
        assert not db.in_transaction()
        recovered.append(db)
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    monkeypatch.setattr(s, "_recover_paper_notification_ingress", recover)
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id": "outer"})
    try:
        result = await s._process_quote_round_shadow(payload)
        assert paper._QUOTE_ROUND_CONTEXT.get() == {"round_id": "outer"}
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert codes == [517]
    assert seen == ["challenger_a", "challenger_a", "challenger_b"]
    assert len({id(db) for db in sessions}) == 3
    assert recovered == sessions
    assert result["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,attempts", [("busy", 2), ("ordinary", 1), ("uncertain", 1), ("expired", 1)])
async def test_failure_is_bounded_and_other_accounts_continue(dispatch, monkeypatch, kind, attempts):
    s, _, payload = dispatch
    seen = []
    async def execute(db, *, now, account_name):
        seen.append(account_name)
        assert now == AT  # The decision clock must never become the retry wall clock.
        if account_name == "challenger_a":
            if kind == "ordinary":
                raise ValueError("application error")
            exc = OperationalError("isolated busy", {}, sqlite3.OperationalError("database is locked"))
            if kind == "uncertain":
                mark_paper_execution_uncertain(exc)
            if kind == "expired":
                class Later(module.datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return AT + timedelta(hours=1)
                monkeypatch.setattr(module, "datetime", Later)
            raise exc
        return {"status": "completed"}
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    result = await s._process_quote_round_shadow(payload)
    assert seen == ["challenger_a"] * attempts + ["challenger_b"]
    assert result["status"] == "failed"
    assert "challenger_execution" in result["failed_components"]
    assert payload["committed_at"] == AT


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [None, {}, {"status": "failed"}, {"status": "unknown"}])
async def test_invalid_receipt_is_failure_but_does_not_starve_next_account(dispatch, monkeypatch, receipt):
    s, _, payload = dispatch
    seen = []
    async def execute(db, *, now, account_name):
        seen.append(account_name)
        return receipt if account_name == "challenger_a" else {"status": "completed"}
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    result = await s._process_quote_round_shadow(payload)
    assert seen == ["challenger_a", "challenger_b"]
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_explicit_policy_skip_is_not_a_failed_call(dispatch, monkeypatch):
    s, _, payload = dispatch
    run = AsyncMock(return_value={"status": "skipped", "reason": "order_window_closed"})
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", run)
    result = await s._process_quote_round_shadow(payload)
    assert result["status"] == "completed"
    assert run.await_count == 2
    assert all(row["status"] == "skipped" for row in result["challenger_accounts"])


@pytest.mark.asyncio
async def test_cancellation_rolls_back_closes_and_does_not_continue(dispatch, monkeypatch):
    s, _, payload = dispatch
    seen, recovered = [], []
    async def execute(db, *, now, account_name):
        seen.append((db, account_name))
        await db.execute(text("BEGIN"))
        raise asyncio.CancelledError()
    async def recover(db):
        assert not db.in_transaction()
        recovered.append(db)
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    monkeypatch.setattr(s, "_recover_paper_notification_ingress", recover)
    with pytest.raises(asyncio.CancelledError):
        await s._process_quote_round_shadow(payload)
    assert [name for _, name in seen] == ["challenger_a"]
    assert recovered == [seen[0][0]]


@pytest.mark.asyncio
async def test_cleanup_error_cannot_erase_uncertain_execution_marker(dispatch, monkeypatch):
    s, _, payload = dispatch
    seen = []
    class Session:
        def __init__(self):
            self.info = {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def rollback(self):
            raise OperationalError("cleanup failed", {}, sqlite3.OperationalError("database is locked"))
    async def execute(db, *, now, account_name):
        seen.append(account_name)
        if account_name == "challenger_a":
            raise mark_paper_execution_uncertain(ValueError("commit receipt unknown"))
        return {"status": "completed"}
    monkeypatch.setattr(module, "async_session", Session)
    monkeypatch.setattr(s, "_recover_paper_notification_ingress", AsyncMock())
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    result = await s._process_quote_round_shadow(payload)
    assert seen == ["challenger_a", "challenger_b"]
    assert result["status"] == "failed"
    assert result["challenger_accounts"][0]["execution_requires_reconciliation"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_stage", ["rollback", "close"])
async def test_cleanup_busy_cannot_mask_cancellation_and_retry_orders(dispatch, monkeypatch, cleanup_stage):
    s, _, payload = dispatch
    seen = []
    class Session:
        def __init__(self):
            self.info = {}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            if cleanup_stage == "close":
                raise OperationalError("close failed", {}, sqlite3.OperationalError("database is locked"))
            return False
        async def rollback(self):
            if cleanup_stage == "rollback":
                raise OperationalError("rollback failed", {}, sqlite3.OperationalError("database is locked"))
    async def execute(db, *, now, account_name):
        seen.append(account_name)
        raise asyncio.CancelledError("dispatcher shutdown")
    monkeypatch.setattr(module, "async_session", Session)
    monkeypatch.setattr(s, "_recover_paper_notification_ingress", AsyncMock())
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    with pytest.raises(asyncio.CancelledError, match="dispatcher shutdown"):
        await s._process_quote_round_shadow(payload)
    assert seen == ["challenger_a"]
