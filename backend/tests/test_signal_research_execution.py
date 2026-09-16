"""Read-only report attribution: causal clocks and later execution are distinct."""
import json
from datetime import timedelta

import pytest

from app.models.paper import PaperAccount
from app.models.trading import TradeOrder, TradeFill
from app.paper import signal_research as research
from test_paper_signal_research import AT, DAY, db, log_row


def order(**changes):
    values = dict(order_id="execution-audit", account_id="default", broker="paper",
        code="600001", side="buy", source="test", strategy_version="v1",
        decision_round_id="round-1", decision_at=AT, as_of_at=AT,
        created_at=AT, updated_at=AT+timedelta(seconds=30), trade_date=DAY,
        order_type="limit", quantity=200, price=10, status="canceled")
    values.update(changes)
    return TradeOrder(**values)


async def report(db):
    await db.flush()
    return await research.build_signal_research_report(
        db, start_date=DAY, end_date=DAY, as_of=AT+timedelta(minutes=1))


@pytest.mark.asyncio
async def test_decision_cannot_precede_signal_even_with_later_id(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(),
        log_row(id=2, action="wait_buy", decision="wait", run_id="decision",
                created_at=AT-timedelta(seconds=1), reason="older cannot explain signal"),
        log_row(id=3, action="deferred_buy", decision="wait", run_id="decision",
                created_at=AT+timedelta(seconds=20), reason="recorded later"),
        log_row(id=4, action="wait_buy", decision="wait", run_id="decision",
                created_at=AT+timedelta(seconds=10), reason="causal first")])
    result = await report(db)
    assert result["signals"][0]["first_decision_id"] == 4
    assert result["signals"][0]["first_decision_reason"] == "causal first"
    assert not db.new and not db.dirty


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_signal_wall_clock_is_not_the_decision_business_clock(db, legacy):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    signal = log_row(created_at=AT+timedelta(seconds=40))
    if legacy:
        payload = json.loads(signal.candidate_json)
        payload.pop("signal_observed_at")
        payload["notification_schema"] = "paper_buy_point_v1"
        signal.candidate_json = json.dumps(payload)
    db.add_all([signal, log_row(id=2, action="deferred_buy", decision="wait",
                                run_id="decision", created_at=AT)])
    result = await report(db)
    s = result["signals"][0]
    assert s["first_decision_id"] == 2
    assert s["first_decision_clock_basis"] == (
        "legacy_log_sequence_not_wall_clock" if legacy else "signal_observed_business_clock")
    assert s["first_decision_log_clock"] == AT.isoformat()


@pytest.mark.asyncio
async def test_legacy_decision_uses_log_sequence_not_business_clock_sort(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    signal = log_row()
    payload = json.loads(signal.candidate_json)
    payload.pop("signal_observed_at")
    payload["notification_schema"] = "paper_buy_point_v1"
    signal.candidate_json = json.dumps(payload)
    db.add_all([signal,
        log_row(id=2, action="wait_buy", decision="wait", run_id="decision",
                created_at=AT+timedelta(seconds=20), reason="first log"),
        log_row(id=3, action="deferred_buy", decision="wait", run_id="decision",
                created_at=AT+timedelta(seconds=10), reason="later log earlier clock")])
    s = (await report(db))["signals"][0]
    assert s["evidence_status"] == "legacy_record_reference"
    assert s["first_decision_id"] == 2
    assert s["first_decision_clock_basis"] == "legacy_log_sequence_not_wall_clock"


@pytest.mark.asyncio
@pytest.mark.parametrize("observed", [None, "invalid", "2026-09-08T10:00:00+08:00"])
async def test_unknown_signal_cannot_claim_legacy_decision_link(db, observed):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    signal = log_row()
    payload = json.loads(signal.candidate_json)
    payload["notification_schema"] = "paper_buy_point_v2"
    payload["signal_observed_at"] = observed
    signal.candidate_json = json.dumps(payload)
    db.add_all([signal, log_row(id=2, action="deferred_buy", decision="wait",
                               run_id="decision", created_at=AT)])
    s = (await report(db))["signals"][0]
    assert s["evidence_status"] == "unknown"
    assert s["first_decision_id"] is None
    assert s["first_decision_state"] == "unknown"
    assert s["first_decision_clock_basis"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["submitted", "partial", "filled", "canceled", "risk_blocked"])
async def test_order_observation_is_separate_from_first_wait_and_linked_fills(db, status):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), log_row(id=2, action="deferred_buy", decision="wait", run_id="decision",
                                 created_at=AT+timedelta(seconds=1)), order(status=status)])
    if status in {"partial", "filled", "canceled"}:
        db.add(TradeFill(fill_id="partial-real", order_id="execution-audit", broker="paper",
            code="600001", side="buy", quantity=100, price=10, commission=5, tax=0,
            filled_at=AT+timedelta(seconds=20)))
    result = await report(db)
    s = result["signals"][0]
    assert s["first_decision_state"] == "deferred_buy:wait"
    assert s["execution_observation"]["order_status"] == status
    assert s["execution_observation"]["status"] == "current_row_visible_at_as_of"
    assert s["actual_fill_quantity"] == (100 if status in {"partial", "filled", "canceled"} else 0)
    assert result["summary"]["linked_order_status_observations"] == {status: 1}
    assert not db.new and not db.dirty
    json.dumps(result, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"updated_at": AT+timedelta(days=1)},
    {"updated_at": AT-timedelta(seconds=1)},
    {"status": "unexpected"},
])
async def test_future_or_invalid_order_projection_cannot_backfill_old_state(db, change):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), order(**change)])
    result = await report(db)
    obs = result["signals"][0]["execution_observation"]
    assert obs["order_status"] is None
    assert obs["status"] == "unknown_not_reconstructed"


@pytest.mark.parametrize("stamp", [None, "", "bad-clock", True, "2026-09-08T10:00:30+08:00"])
def test_missing_invalid_or_aware_updated_clock_remains_unknown(stamp):
    value = research._execution_observation(order(updated_at=stamp), as_of=AT+timedelta(minutes=1))
    assert value["status"] == "unknown_not_reconstructed"
    assert value["order_status"] is None


@pytest.mark.asyncio
async def test_empty_unlinked_and_duplicate_signals_keep_distinct_denominators(db):
    empty = await report(db)
    assert empty["summary"]["linked_order_status_observations"] == {}
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), log_row(id=2), log_row(id=3, code="600002"), order()])
    result = await report(db)
    a, b, c = result["signals"]
    assert a["execution_observation"]["order_id"] == "execution-audit"
    assert b["execution_observation"]["status"] == "shared_order_already_linked"
    assert c["execution_observation"]["status"] == "no_unique_linked_order"
    assert result["summary"]["linked_order_status_observations"] == {"canceled": 1}


@pytest.mark.asyncio
async def test_structured_validity_reason_needs_own_visible_clock(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    data = {"paper_deferred_order": {"buy_validity_evaluation": {
        "schema": "pending_buy_evaluation_v1", "status": "canceled",
        "evaluated_at": (AT+timedelta(seconds=20)).isoformat(),
        "quote_round_id": "next-round", "reason": "buy_signal_expired: original TTL"}}}
    o = order(risk_json=json.dumps(data), error_message="private arbitrary broker error")
    db.add_all([log_row(), o])
    result = await report(db)
    obs = result["signals"][0]["execution_observation"]
    assert obs["validity_evaluation"]["reason"] == "buy_signal_expired: original TTL"
    assert "private arbitrary" not in json.dumps(obs)
    data["paper_deferred_order"]["buy_validity_evaluation"]["evaluated_at"] = (AT+timedelta(days=1)).isoformat()
    o.risk_json = json.dumps(data)
    o.updated_at = AT+timedelta(seconds=30)
    result = await report(db)
    assert result["signals"][0]["execution_observation"]["validity_evaluation"] is None
