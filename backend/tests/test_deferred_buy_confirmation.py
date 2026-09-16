"""Real submit -> deferred producer -> log; fixture-only risk/broker, no business DB."""
import asyncio
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperShadowEvent, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.paper import confirmation_evidence as m
from app.trading import service
from test_paper_deferred_exit_provenance import memory_session
from paper_pending_fixture import accepted_frame

AT = datetime(2026, 9, 9, 10)
MODES = ("canceled", "version_changed", "version_missing", "quote_wait", "depth_wait",
         "risk_blocked", "risk_warn", "broker_rejected", "partial", "filled",
         "partial_quote_wait", "partial_depth_wait")
RESULT = {"canceled": "canceled", "version_changed": "risk_blocked",
          "version_missing": "risk_blocked", "quote_wait": "submitted",
          "depth_wait": "submitted", "risk_blocked": "risk_blocked",
          "risk_warn": "risk_blocked", "broker_rejected": "rejected",
          "partial": "partial", "filled": "filled",
          "partial_quote_wait": "partial", "partial_depth_wait": "partial"}


def candidate(at=AT):
    return {"code": "600001", "score": 90, "execution_confirmation": True,
        "confirmation_evidence": {
            "historical_quote_path_confirmed": "true", "current_setup_valid": "true",
            "execution_permitted": "unknown", "order_result": "not_submitted",
            "historical_confirmed_at": (at-timedelta(seconds=2)).isoformat(),
            "historical_log_id": 7, "observed_at": at.isoformat(),
        }}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_submit_reconcile_log_keeps_history_and_current_response_separate(
    memory_session, monkeypatch, mode, legacy,
):
    db = memory_session
    monkeypatch.setattr(m, "_observation_now", lambda: AT+timedelta(seconds=20))
    monkeypatch.setattr(service, "experiment_active", lambda *_a, **_kw: False)
    # This matrix tests observation/provenance, with entry policy isolated like risk/broker.
    # Real pending-buy contracts (including legacy cancellation) have dedicated regressions.
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda _order: False)
    risk_calls, quote_calls, broker_calls, outcomes = [], [], [], []
    phase = {"name": "submit"}
    first_partial = mode.startswith("partial")

    async def risk(_db, command):
        risk_calls.append((phase["name"], command.quantity, command.price))
        level = ("block" if mode == "risk_blocked" else "warn" if mode == "risk_warn" else "pass")
        return {"final_level": "pass" if phase["name"] == "submit" else level,
                "warnings": [], "block_reasons": []}

    def quote_check(*_a, **_kw):
        quote_calls.append(phase["name"])
        return (not (mode == "quote_wait" or
                mode == "partial_quote_wait" and phase["name"] == "later"), "fixture_quote")

    def depth(*_a, **_kw):
        if mode == "depth_wait" or mode == "partial_depth_wait" and phase["name"] == "later":
            return 0, None, []
        return 100, 10.0, [{"level": 1, "price": 10.0, "quantity": 100}]

    class FixtureBroker:
        async def place_order(self, _db, request):
            broker_calls.append(request)
            if mode == "broker_rejected":
                raise RuntimeError("fixture rejected")
            fill = SimpleNamespace(fill_id="fixture-fill", price=10.0, quantity=100,
                commission=5.0, tax=0.0, realized_pnl=0.0, broker_trade_id="999",
                filled_at=AT, raw={})
            return SimpleNamespace(accepted=True, external_order_id="fixture-external", fills=[fill])

    original = service.reconcile_paper_deferred_orders
    async def capture_producer(*args, **kwargs):
        rows = await original(*args, **kwargs)
        outcomes.extend(copy.deepcopy(rows))
        return rows

    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda *_: FixtureBroker())
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=SimpleNamespace(price=10)))
    monkeypatch.setattr(service, "_depth_fill_plan", depth)
    monkeypatch.setattr(service, "reconcile_paper_deferred_orders", capture_producer)
    monkeypatch.setattr(paper, "_execution_quote_status", quote_check)
    account = PaperAccount(account_name="default", initial_capital=100000,
        current_capital=100000, total_assets=100000)
    db.add(account)
    await db.commit()
    decision_at = AT-timedelta(days=1) if mode == "canceled" else AT-timedelta(seconds=30)
    source = {} if legacy else candidate(decision_at)
    initial = copy.deepcopy(source)
    version = "frozen-old" if mode == "version_changed" else paper._strategy_version("default")
    command = service.SubmitOrderCommand(
        code="600001", side="buy", price=10.1, quantity=300 if first_partial else 100,
        broker="paper", account_id="default", strategy_id="paper-auto-short",
        source="next_day_plan", strategy_version=version, decision_at=decision_at,
        as_of_at=decision_at, decision_round_id="decision", execute=True,
        defer_until_next_round=True, deferred_metadata={"candidate": source, "block_warn": True},
        config_version="fixture-config", code_version="fixture-code",
    )
    submission = await service.submit_order(db, command)
    assert submission["order"]["status"] == "submitted" and submission["fills"] == []
    assert broker_calls == []
    order = await db.scalar(select(TradeOrder))
    assert json.loads(order.risk_json)["paper_deferred_order"]["candidate"] == initial
    # Match the real API's post-submit update; serialization already froze the
    # pre-submit candidate. An observation fix must not rewrite that old evidence.
    if not legacy:
        source["confirmation_evidence"] = m.order_confirmation_evidence(source, "submitted")
    if mode == "version_missing":
        order.strategy_version = None  # isolated pre-existing legacy-order fixture
        await db.commit()
    version = order.strategy_version
    phase["name"] = "first"
    first_frame = {
        "round_id": "first", "quality_status": "ok", "committed_at": AT, "as_of_at": AT,
        "config_version": "fixture-config", "code_version": "fixture-code",
        "records": [],
    }
    if first_partial or mode in {"filled", "broker_rejected"}:
        # Only cases which reach dispatch opt into the real timing validator.
        spot = dict(code="600001", name="fixture", price=10, source_quote_at=AT,
                    ask1_price=10, ask1_volume=1,
                    received_at=AT, updated_at=AT, quote_round_id="first")
        first_frame["records"] = [spot]
        await accepted_frame(db, first_frame)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
        monkeypatch.setattr(service, "_paper_execution_spot",
                            AsyncMock(return_value=SimpleNamespace(**spot)))
    token = paper._QUOTE_ROUND_CONTEXT.set(first_frame)
    try:
        args = dict(account=account, trade_date=AT.date(), trigger="fixture", observed_at=AT)
        logs = await paper._reconcile_deferred_order_logs(db, run_id="first", **args)
        assert await paper._reconcile_deferred_order_logs(db, run_id="repeat", **args) == []
        await db.commit()
        assert len(logs) == 1
        first_frozen = logs[0].candidate_json
        payload = json.loads(first_frozen)
        evidence = payload["confirmation_evidence"]
        observation = payload["deferred_order_observation"]
        assert evidence["historical_quote_path_confirmed"] == ("unknown" if legacy else "true")
        assert evidence["historical_log_id"] == (None if legacy else 7)
        assert evidence["current_setup_valid"] == "unknown"
        assert evidence["order_result"] == order.status == RESULT[mode]
        assert evidence["order_result"] != "not_submitted"
        expected_permission = ("true" if first_partial or mode == "filled" else
                               "unknown" if mode in {"quote_wait", "depth_wait"} else "false")
        assert evidence["execution_permitted"] == expected_permission
        assert evidence["observed_at"] == (AT+timedelta(seconds=20)).isoformat()
        assert observation["strategy_evaluated_at"] == AT.isoformat()
        assert observation["order_response"]["strategy_version"] == version
        assert logs[0].strategy_version == (version or "legacy_unversioned")
        assert observation["original_decision_confirmation"] == m.confirmation_evidence(initial)
        assert observation["replay_ready"] is False and observation["commit_known_at"] is None
        # Real submitted order -> producer -> ordinary log -> pure research adapter.
        from app.paper.dependency_research import adapt_paper_record, LOG_FIELDS
        record = {key:getattr(logs[0], key, None) for key in LOG_FIELDS if key != "account_name"}
        record["candidate_json"] = logs[0].candidate_json
        research_batch = {"account_id":account.id, "account_name":"default",
            "strategy_version":logs[0].strategy_version, "decision_run_id":logs[0].run_id,
            "quote_round_id":logs[0].quote_round_id, "frozen_at":(AT+timedelta(seconds=21)).isoformat()}
        adapted = adapt_paper_record(record,research_batch)
        assert adapted["status"] == "adapted"
        assert adapted["production_facts"]["order_result"] == order.status
        assert adapted["decision_at"] == AT
        assert adapted["response_available_at"] == AT+timedelta(seconds=20)
        assert outcomes[0]["deferred"]["candidate"] == initial
        assert json.loads(order.risk_json)["paper_deferred_order"]["candidate"] == initial
        assert len(broker_calls) == int(first_partial or mode in {"filled", "broker_rejected"})
        assert len(risk_calls) == (1 + int(first_partial or mode in {"filled", "broker_rejected", "risk_blocked", "risk_warn"})
                                  + int(first_partial or mode in {"filled", "broker_rejected"}))
        # Dispatch timing rechecks quote freshness after local calendar/round queries.
        assert len(quote_calls) == (int(mode not in {"canceled", "version_changed", "version_missing"})
                                    + int(first_partial or mode in {"filled", "broker_rejected"}))

        if mode in {"partial_quote_wait", "partial_depth_wait"}:
            phase["name"] = "later"
            later = AT+timedelta(seconds=5)
            second_token = paper._QUOTE_ROUND_CONTEXT.set({
                "round_id": "later", "quality_status": "ok", "committed_at": later, "as_of_at": later,
            })
            try:
                second = await paper._reconcile_deferred_order_logs(
                    db, account=account, trade_date=AT.date(), trigger="fixture", observed_at=later, run_id="later")
                assert await paper._reconcile_deferred_order_logs(
                    db, account=account, trade_date=AT.date(), trigger="fixture", observed_at=later, run_id="later-repeat") == []
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(second_token)
            await db.commit()
            assert logs[0].candidate_json == first_frozen
            evidence = json.loads(second[0].candidate_json)["confirmation_evidence"]
            assert evidence["order_result"] == "partial"  # cumulative order state
            assert evidence["execution_permitted"] == "unknown"  # no fill / risk pass this round
            assert order.filled_quantity == 100 and len(broker_calls) == 1
            assert len(risk_calls) == 3  # submission + first preflight + locked recheck; waiting adds no risk call
        fills = list((await db.scalars(select(TradeFill))).all())
        assert len(fills) == int(first_partial or mode == "filled")
        if fills:
            assert (fills[0].quantity, fills[0].price, fills[0].commission, fills[0].tax) == (100,10,5,0)
        assert order.price == 10.1 and order.quantity == (300 if first_partial else 100)
        assert list((await db.scalars(select(PaperTradeLog))).all()) == []
        assert list((await db.scalars(select(PaperShadowEvent))).all()) == []
        assert json.loads(order.risk_json)["paper_deferred_order"]["candidate"] == initial
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


def response(status="filled", event="filled", fills=None):
    return {"event": event, "order": {"order_id": "one", "broker": "paper",
        "code": "600001", "side": "buy", "status": status},
        "fills": ([{"order_id":"one","broker":"paper","code":"600001","side":"buy","quantity":100}]
                  if fills is None else fills)}


@pytest.mark.parametrize("status,event,fills,permission", [
    ("filled","filled",[], "unknown"),
    ("partial","waiting",[], "unknown"),
    ("submitted","waiting",[], "unknown"),
    ("risk_blocked","risk_blocked",[], "false"),
    ("rejected","rejected",[], "false"),
    ("canceled","canceled",[], "false"),
    ("filled","risk_blocked",[], "unknown"),
    ("filled","filled",[{"quantity":100}], "unknown"),
    ("filled","filled",[{"order_id":"wrong","broker":"paper","code":"600001","side":"buy","quantity":100}], "unknown"),
    ("filled","filled",[{"order_id":"one","broker":"paper","code":"600001","side":"buy","quantity":True}], "unknown"),
])
def test_current_permission_requires_this_response_not_old_order_state(monkeypatch,status,event,fills,permission):
    monkeypatch.setattr(m, "_observation_now", lambda: AT)
    source = candidate()
    source["confirmation_evidence"]["execution_permitted"] = "true"
    before = copy.deepcopy(source)
    result = m.deferred_buy_log_evidence(source, outcome=response(status,event,fills), code="600001", evaluated_at=AT)
    assert result["confirmation_evidence"]["execution_permitted"] == permission
    assert result["confirmation_evidence"]["current_setup_valid"] == "unknown"
    assert source == before


@pytest.mark.parametrize("mutation", [
    "future", "past_day", "missing_order", "sell", "wrong_code", "missing_id",
    "missing_fills", "too_many_fills",
])
def test_invalid_response_or_clock_cannot_acquire_execution_evidence(monkeypatch, mutation):
    monkeypatch.setattr(m, "_observation_now", lambda: AT)
    out = response()
    evaluated = AT
    if mutation == "future":
        evaluated += timedelta(seconds=1)
    elif mutation == "past_day":
        evaluated -= timedelta(days=1)
    elif mutation == "missing_order":
        out["order"] = None
    elif mutation == "sell":
        out["order"]["side"] = "sell"
    elif mutation == "wrong_code":
        out["order"]["code"] = "600002"
    elif mutation == "missing_id":
        out["order"]["order_id"] = ""
    elif mutation == "missing_fills":
        out["fills"] = None
    elif mutation == "too_many_fills":
        out["fills"] *= 65
    with pytest.raises(ValueError):
        m.deferred_buy_log_evidence(candidate(), outcome=out, code="600001", evaluated_at=evaluated)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ordinary", "cancel"])
async def test_optional_audit_failure_never_changes_original_execution_log(memory_session, monkeypatch, caplog, failure):
    db = memory_session
    def fail(*_a, **_kw):
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise ValueError("sensitive detail")
    monkeypatch.setattr(paper, "deferred_buy_log_evidence", fail)
    call = paper._add_auto_log(
        db, run_id="fixture", trade_date=AT.date(), trigger="fixture", source="next_day_plan",
        code="600001", action="buy", decision="executed", reason="filled", amount=100,
        strategy_version="frozen", candidate=candidate(), created_at=AT,
        deferred_buy_outcome=response(),
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError): await call
        assert list((await db.scalars(select(PaperAutoTradeLog))).all()) == []
    else:
        log = await call
        await db.commit()
        assert log.decision == "executed" and log.amount == 100 and log.strategy_version == "frozen"
        assert json.loads(log.candidate_json)["confirmation_evidence"]["execution_permitted"] == "unknown"
        assert "sensitive detail" not in caplog.text


@pytest.mark.asyncio
async def test_candidate_marker_cannot_skip_original_preorder_freezer(memory_session):
    source = candidate()
    source["deferred_order_observation"] = {"schema_version":"confirmation:deferred_buy_v1"}
    source["confirmation_evidence"]["order_result"] = "filled"
    log = await paper._add_auto_log(memory_session, run_id="fixture", trade_date=AT.date(),
        trigger="fixture", source="next_day_plan", code="600001",
        action="skip_buy", decision="blocked", reason="pre-order risk", candidate=source, created_at=AT)
    assert json.loads(log.candidate_json)["confirmation_evidence"]["order_result"] == "not_submitted"
