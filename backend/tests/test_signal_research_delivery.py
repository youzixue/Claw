"""P1 research only: isolated SQLite, no dispatch, no historical writes."""
import json
from datetime import timedelta

import pytest

from app.models.paper import PaperAccount, PaperShadowEvent
from app.paper import signal_research as research
from test_paper_signal_research import AT, DAY, db, log_row


def delivery(id, signal_id=1, status="sent", at=None, **changes):
    return log_row(id=id, action="signal_push", decision=status, source="feishu",
                   created_at=at or AT + timedelta(seconds=5),
                   candidate_json=json.dumps({"signal_log_id": signal_id}), **changes)


async def report(db, at=None):
    await db.flush()
    return await research.build_signal_research_report(
        db, start_date=DAY, end_date=DAY, as_of=at or AT + timedelta(minutes=1))


@pytest.mark.asyncio
async def test_sent_is_distinct_from_confirmed_and_fill_with_legacy_clocks(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), log_row(id=2, at=AT+timedelta(seconds=1)),
                delivery(3), delivery(4), delivery(5, signal_id=2, status="sending"),
                delivery(6, version="other"), delivery(7, signal_id=2, at=AT+timedelta(days=1))])
    result = await report(db)
    summary = result["summary"]
    assert summary["confirmation_events"] == 2
    assert summary["sent_audit_events"] == 2
    assert summary["sent_signal_events"] == summary["sent_stock_day_count"] == 1
    assert summary["fill_linked_events"] == 0
    first, second = result["signals"]
    assert first["account_role"] == "champion"
    assert first["actual_fill_cash_including_fees"] is None
    assert first["delivery"]["audits"][0]["send_started_at"] is None
    assert first["delivery"]["post_send_price"] is None
    assert second["delivery"]["sent_audit_count"] == 0
    assert not db.new and not db.dirty
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("price,quote_round,stamp,known", [
    (10, "round-1", -1, True), (11, "round-1", -1, False),
    (10, "other", -1, False), (10, "round-1", 1, False),
])
def test_pre_push_price_uses_only_own_frozen_stock_clock(price, quote_round, stamp, known):
    row = log_row()
    payload = json.loads(row.candidate_json)
    payload["market_context"] = {
        "schema": "signal_market_context_v1", "price": price, "quote_round_id": quote_round,
        "source_quote_at": (AT+timedelta(seconds=stamp)).isoformat(),
    }
    row.candidate_json = json.dumps(payload)
    value = research.signal_record(row, "default")
    assert (value["reference_price_at"] is not None) is known
    assert value["reference_price"] == 10
    assert value["observed_at"] == AT.isoformat()


@pytest.mark.parametrize("invalid", [False, True])
def test_transport_clocks_validate_order_without_fabrication(invalid):
    signal = research.signal_record(log_row(), "default")
    row = delivery(2)
    row.candidate_json = json.dumps({
        "signal_log_id": 1, "delivery_clock_schema": "paper_push_transport_v1",
        "transport_clock_status": "ok", "dispatch_started_at": AT.isoformat(),
        "send_started_at": (AT+timedelta(seconds=2)).isoformat(),
        "send_completed_at": (AT+timedelta(seconds=1 if invalid else 3)).isoformat(),
    })
    value = research._delivery_evidence(signal, [row], as_of=AT+timedelta(seconds=6))
    assert value["sent_audit_count"] == 1
    audit = value["audits"][0]
    assert (audit["send_started_at"] is None) is invalid
    assert audit["user_received_at"] is None


def test_stock_day_first_last_use_clock_and_isolate_route_and_versions():
    base = dict(account_id=1, strategy_version="v1", route_id="r1", route_version="rv1",
                code="600001", trade_date=DAY.isoformat())
    rows = [
        dict(base, signal_id=1, reference_at=(AT+timedelta(seconds=10)).isoformat()),
        dict(base, signal_id=8, reference_at=AT.isoformat()),
        dict(base, signal_id=3, route_id="r2", reference_at=AT.isoformat()),
        dict(base, signal_id=4, route_version="rv2", reference_at=AT.isoformat()),
        dict(base, signal_id=5, strategy_version="v2", reference_at=AT.isoformat()),
        dict(base, signal_id=6, account_id=2, reference_at=AT.isoformat()),
    ]
    assert {r["signal_id"] for r in research._stock_days(rows)} == {8, 3, 4, 5, 6}
    assert {r["signal_id"] for r in research._stock_days(rows, last=True)} == {1, 3, 4, 5, 6}


@pytest.mark.asyncio
async def test_frozen_event_version_is_exact_and_not_current_configuration(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), log_row(id=2, code="600002")])
    db.add(PaperShadowEvent(event_key="key-1", route_id="test", route_version="historical",
        code="600001", trade_date=DAY, event_type="confirmed", status="confirmed",
        observed_at=AT-timedelta(seconds=2), created_at=AT-timedelta(seconds=1)))
    db.add(PaperShadowEvent(event_key="key-2", route_id="other", route_version="wrong",
        code="600002", trade_date=DAY, event_type="confirmed", status="confirmed",
        observed_at=AT-timedelta(seconds=2), created_at=AT-timedelta(seconds=1)))
    value = await report(db)
    assert value["signals"][0]["route_version"] == "historical"
    assert value["signals"][1]["route_version"] == "unknown"
    assert len(value["strata"]) == 2


@pytest.mark.asyncio
async def test_sent_first_last_returns_have_their_own_denominator(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), log_row(id=2, at=AT+timedelta(seconds=1), price=11),
                delivery(3), delivery(4, signal_id=2)])
    value = await report(db)
    summary = value["summary"]
    assert summary["sent_signal_events"] == 2 and summary["sent_stock_day_count"] == 1
    for name in ("horizons", "last_stock_day_horizons",
                 "sent_first_stock_day_horizons", "sent_last_stock_day_horizons"):
        assert summary[name]["1"]["evaluated_count"] == 0
        assert summary[name]["1"]["positive_fraction"] is None
