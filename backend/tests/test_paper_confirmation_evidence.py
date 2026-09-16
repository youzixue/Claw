"""Frozen evidence is observability, not candidate revival or an execution gate."""
import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
from sqlalchemy import event, select
from app.models.stock import StockSpot

import pytest
from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog
from app.paper.confirmation_evidence import (
    confirmation_evidence, freeze_log_evidence, historical_confirmation_evidence,
    order_confirmation_evidence,
)
from test_paper_api import paper_client

DAY = date(2026, 9, 8)
AT = datetime(2026, 9, 8, 10)


def test_legacy_bool_and_missing_or_malformed_evidence_are_unknown():
    for candidate in ({}, {"execution_confirmation": True}, {"execution_confirmation": False},
                      {"confirmation_evidence": []}, {"confirmation_evidence": {"current_setup_valid": False}}):
        evidence = confirmation_evidence(candidate)
        assert evidence["historical_quote_path_confirmed"] == "unknown"
        assert evidence["current_setup_valid"] == "unknown"
        assert evidence["execution_permitted"] == "unknown"


@pytest.mark.asyncio
async def test_history_is_scoped_visible_and_never_infers_false(paper_client):
    _, maker = paper_client
    base = dict(account_id=1, trade_date=DAY, code="600001", source="next_day_plan",
                strategy_version="a:v1", action="confirm_buy", decision="quote_confirmed",
                run_id="test", trigger="test", reason="quote only", created_at=AT)
    payload = {"confirmation_version": "champion_persistent_v1", "confirmation_sample_at": AT.isoformat()}
    async with maker() as db:
        for overrides, override_payload in [
            ({"account_id": 2}, {}),
            ({"trade_date": DAY-timedelta(days=1)}, {}),
            ({"code": "600002"}, {}),
            ({"source": "other"}, {}),
            ({"strategy_version": "a:old"}, {}),
            ({"created_at": AT+timedelta(seconds=1)}, {}),
            ({}, {"confirmation_sample_at": (AT+timedelta(seconds=1)).isoformat()}),
            ({}, {"confirmation_sample_at": (AT-timedelta(days=1)).isoformat()}),
            ({"created_at": AT-timedelta(days=1)}, {}),
            ({}, {"confirmation_sample_at": "broken"}),
            ({}, {"confirmation_sample_at": "2026-09-08T10:00:00+08:00"}),
            ({}, {"confirmation_version": "old"}),
            ({"decision": "wait"}, {}),
        ]:
            db.add(PaperAutoTradeLog(**(base | overrides), candidate_json=json.dumps(payload | override_payload)))
        await db.flush()
        args = dict(account_id=1, trade_date=DAY, code="600001", source="next_day_plan",
                    strategy_version="a:v1", observed_at=AT)
        unknown = await historical_confirmation_evidence(db, **args)
        assert unknown["historical_quote_path_confirmed"] == "unknown"
        valid = PaperAutoTradeLog(**base, candidate_json=json.dumps(payload))
        db.add(valid)
        await db.flush()
        evidence = await historical_confirmation_evidence(db, **args)
        assert evidence["historical_quote_path_confirmed"] == "true"
        assert evidence["historical_log_id"] == valid.id
        assert evidence["current_setup_valid"] == evidence["execution_permitted"] == "unknown"
        assert unknown["historical_quote_path_confirmed"] == "unknown"


@pytest.mark.asyncio
async def test_log_freezes_historical_true_setup_false_and_no_order(paper_client):
    _, maker = paper_client
    candidate = {"execution_confirmation": False, "confirmation_evidence": {
        "historical_quote_path_confirmed": "true", "current_setup_valid": "false",
        "observed_at": AT.isoformat(),
    }}
    async with maker() as db:
        log = await paper._add_auto_log(
            db, run_id="empty-band", trade_date=DAY, trigger="test", source="next_day_plan",
            action="skip_buy", decision="skipped", reason="empty price band", candidate=candidate,
            created_at=AT,
        )
        frozen = paper._auto_log_payload(log)["confirmation_evidence"]
        assert frozen["historical_quote_path_confirmed"] == "true"
        assert frozen["current_setup_valid"] == frozen["execution_permitted"] == "false"
        assert frozen["order_result"] == "not_submitted"
        candidate["confirmation_evidence"]["current_setup_valid"] = "true"
        assert paper._auto_log_payload(log)["confirmation_evidence"] == frozen
        assert candidate["execution_confirmation"] is False
        assert "execution_permitted" not in candidate["confirmation_evidence"]


@pytest.mark.parametrize("action,decision,result", [
    ("confirm_buy", "quote_confirmed", "not_submitted"), ("buy", "dry_run", "dry_run"),
])
def test_quote_confirmation_and_dry_run_do_not_prove_order_permission(action, decision, result):
    evidence = freeze_log_evidence({}, action=action, decision=decision)
    assert evidence["execution_permitted"] == "unknown"
    assert evidence["order_result"] == result


@pytest.mark.parametrize("status,permission", [
    ("filled", "true"), ("submitted", "true"), ("partial", "true"),
    ("rejected", "false"), ("risk_blocked", "false"), ("", "unknown"),
    ("unexpected", "unknown"), ("canceled", "unknown"),
])
def test_order_result_does_not_conflate_permission_and_fill(status, permission):
    candidate = {"confirmation_evidence": {"historical_quote_path_confirmed": "true"}}
    evidence = order_confirmation_evidence(candidate, status)
    assert evidence["historical_quote_path_confirmed"] == "true"
    assert evidence["execution_permitted"] == permission
    assert evidence["order_result"] == (status or "unknown")
    assert "execution_permitted" not in candidate["confirmation_evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_failure", [False, True])
async def test_early_empty_band_keeps_history_without_restoring_candidate_or_order(paper_client, monkeypatch, audit_failure):
    _, maker = paper_client
    candidate = {"code": "600001", "name": "test", "_source": "next_day_plan",
                 "score": 95, "execution_confirmation": False}
    monkeypatch.setattr(paper, "_paper_auto_buy_candidates", AsyncMock(return_value=([candidate], [])))
    monkeypatch.setattr(paper, "_should_run_intraday_auto_trade", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_paper_order_window_status", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_execution_quote_status", lambda *args: (True, ""))
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    confirmation_gate = AsyncMock(side_effect=AssertionError("early filter must not restore readiness"))
    monkeypatch.setattr(paper, "_champion_intraday_confirmation_status", confirmation_gate)
    async with maker() as db:
        account = await paper._get_or_create_account(db, "default")
        db.add(StockSpot(code="600001", name="test", price=10, high=12, low=9,
                         change_pct=1, updated_at=AT))
        db.add(PaperAutoTradeLog(
            account_id=account.id, trade_date=DAY, code="600001", source="next_day_plan",
            strategy_version=paper._strategy_version("default"), created_at=AT-timedelta(minutes=2),
            action="confirm_buy", decision="quote_confirmed", run_id="prior", trigger="test", reason="quote",
            candidate_json=json.dumps({"confirmation_version": "champion_persistent_v1",
                "confirmation_sample_at": (AT-timedelta(minutes=2)).isoformat()}),
        ))
        await db.commit()
        failures = []
        def fail_audit(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT paper_auto_trade_log.id, paper_auto_trade_log.created_at,"):
                failures.append(statement)
                # Real SQL error inside the SAVEPOINT, not a mocked strategy gate.
                return "SELECT missing_column FROM paper_auto_trade_log", ()
            return statement, parameters
        if audit_failure:
            # run may commit/reacquire a connection; scope to this fixture-only engine.
            event.listen(db.bind.sync_engine, "before_cursor_execute", fail_audit, retval=True)
        try:
            result = await paper.run_paper_auto_trade(
                db, now=AT, execute=False, include_position_risk=False, account_name="default",
            )
        finally:
            if audit_failure:
                event.remove(db.bind.sync_engine, "before_cursor_execute", fail_audit)
        assert len(failures) == int(audit_failure)
    row = next(row for row in result["logs"] if row.get("reason_code") == "entry_constraint_empty")
    assert row["confirmation_evidence"]["historical_quote_path_confirmed"] == ("unknown" if audit_failure else "true")
    assert row["confirmation_evidence"]["current_setup_valid"] == "false"
    assert row["confirmation_evidence"]["execution_permitted"] == "false"
    assert row["confirmation_evidence"]["order_result"] == "not_submitted"
    assert not any(row["action"] in {"buy", "queue_buy", "deferred_buy"} for row in result["logs"])
    assert candidate["execution_confirmation"] is False
    confirmation_gate.assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_sql_failure_rolls_back_only_savepoint_without_flushing_business_state(
    paper_client, monkeypatch, caplog,
):
    _, maker = paper_client
    async with maker() as db:
        existing = PaperAutoTradeLog(
            run_id="outer-before", trade_date=DAY, created_at=AT, action="hold", decision="wait",
        )
        db.add(existing)
        await db.flush()
        pending = PaperAutoTradeLog(
            run_id="outer-pending", trade_date=DAY, created_at=AT, action="hold", decision="wait",
        )
        db.add(pending)
        connection = await db.connection()
        rollbacks = []
        def on_rollback(conn, name, context):
            rollbacks.append(name)
        event.listen(connection.sync_connection, "rollback_savepoint", on_rollback)
        def fail_audit(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT "):
                # Real database error: SAVEPOINT must be exited with rollback.
                return "SELECT private_audit_failure FROM paper_auto_trade_log", ()
            return statement, parameters
        event.listen(connection.sync_connection, "before_cursor_execute", fail_audit, retval=True)
        try:
            evidence = await historical_confirmation_evidence(
                db, account_id=1, trade_date=DAY, code="600001", source="next_day_plan",
                strategy_version="a:v1", observed_at=AT,
            )
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", fail_audit)
            event.remove(connection.sync_connection, "rollback_savepoint", on_rollback)
        assert len(rollbacks) == 1
        assert evidence["historical_quote_path_confirmed"] == "unknown"
        assert evidence["historical_log_id"] is None
        assert evidence["execution_permitted"] == "unknown"
        assert pending.id is None  # observational query must not trigger ORM flush
        assert db.is_active
        await db.commit()
        ids = list((await db.scalars(select(PaperAutoTradeLog.run_id))).all())
        assert set(ids) == {"outer-before", "outer-pending"}
    messages = [record for record in caplog.records if record.name.endswith("confirmation_evidence")]
    assert len(messages) == 1
    assert messages[0].getMessage() == "Confirmation history audit unavailable; evidence is unknown"
    assert messages[0].exc_info is None
    assert "private_audit_failure" not in caplog.text


@pytest.mark.asyncio
async def test_observation_date_mismatch_returns_unknown_without_query(paper_client, monkeypatch):
    _, maker = paper_client
    async with maker() as db:
        connect = AsyncMock(side_effect=AssertionError("wrong-day observation must not query"))
        monkeypatch.setattr(db, "connection", connect)
        evidence = await historical_confirmation_evidence(
            db, account_id=1, trade_date=DAY, code="600001", source="next_day_plan",
            strategy_version="a:v1", observed_at=AT+timedelta(days=1),
        )
        assert evidence["historical_quote_path_confirmed"] == "unknown"
        connect.assert_not_awaited()


def test_old_log_payload_is_not_enriched_from_later_history():
    log = PaperAutoTradeLog(candidate_json=json.dumps({"execution_confirmation": True}),
                            action="skip_buy", decision="blocked", reason="risk")
    assert paper._auto_log_payload(log)["confirmation_evidence"]["historical_quote_path_confirmed"] == "unknown"
