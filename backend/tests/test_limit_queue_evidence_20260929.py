"""Real in-memory submit/reconcile; no live DB/network, risk/broker are fixtures."""
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.stock import LimitUpPool
from app.models.trading import TradeFill, TradeOrder
from app.paper.experiment import HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION
from app.trading import service
from paper_pending_fixture import accepted_frame
from test_paper_deferred_exit_provenance import memory_session

AT = datetime(2026, 9, 29, 10)
CONTRACT = "limit_queue_evidence_v1"
INVALID = [None, True, False, -1, 1.5, float("nan"), float("inf"), float("-inf")]


async def setup(db, monkeypatch, **fields):
    monkeypatch.setattr(service, "experiment_active", lambda *_a, **_kw: False)
    monkeypatch.setattr(settings, "PAPER_TENBAGGER_ENABLED", True)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day",
                        AsyncMock(return_value=AT.date()-timedelta(days=1)))
    risk = AsyncMock(return_value={"final_level": "pass", "block_reasons": [], "warnings": []})
    broker = AsyncMock()
    broker.place_order.return_value = SimpleNamespace(
        accepted=True, external_order_id="queue-evidence-fill", status="filled", error_message="",
        fills=[SimpleNamespace(fill_id="queue-evidence-fill", price=11., quantity=100,
            commission=5., tax=0., realized_pnl=0., broker_trade_id="1",
            filled_at=AT+timedelta(seconds=30), raw={})])
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda _: broker)
    spot = SimpleNamespace(**{**dict(code="600001", name="isolated", price=11., prev_close=10.,
        open=10.5, high=11., low=10.4, avg_price=10.7, change_pct=10., limit_up=11., limit_down=9.,
        ask1_price=0., ask1_volume=0., bid1_price=11., bid1_volume=100, volume=50000,
        updated_at=AT, received_at=AT, source_quote_at=AT, quote_round_id="queue-evidence-original"), **fields})
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(side_effect=lambda *_: spot))
    monkeypatch.setattr(paper, "_spot_by_code", AsyncMock(side_effect=lambda *_: spot))
    db.add(LimitUpPool(code="600001", trade_date=AT.date()-timedelta(days=1),
        consecutive_days=4, seal_amount=200_000_000., break_count=0, quarantined=False))
    await db.commit()
    candidate = dict(code="600001", _source="tenbagger_midline",
        signal_date=(AT.date()-timedelta(days=1)).isoformat(),
        entry_mode_contract=HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION, entry_variant="e2_limit_touch")
    command = service.SubmitOrderCommand(code="600001", side="buy", quantity=100, price=11.,
        account_id="challenger_e", strategy_id="paper-auto-short", source="tenbagger_midline",
        strategy_version=paper._strategy_version("challenger_e"), signal_id="queue-evidence-origin",
        decision_at=AT, as_of_at=AT, decision_round_id=spot.quote_round_id, queue_if_limit_up=True,
        queue_metadata={"candidate": candidate, "confirmed_at": AT.isoformat(), "cancel_time": "14:50"})
    result = await service.submit_order(db, command)
    return result, await db.scalar(select(TradeOrder)), spot, risk, broker


async def reconcile(db, monkeypatch, spot, *, seconds=30, volume=50101):
    now = AT+timedelta(seconds=seconds)
    spot.volume = volume
    spot.updated_at = spot.received_at = spot.source_quote_at = now
    spot.quote_round_id = "queue-evidence-fill-" + str(seconds)
    payload = dict(round_id=spot.quote_round_id, quality_status="ok", as_of_at=now,
        committed_at=now, config_version="queue-evidence-fixture", code_version="queue-evidence-fixture",
        records=[vars(spot)])
    await accepted_frame(db, payload)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: now)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        return await service.reconcile_paper_limit_up_orders(db, account_id="challenger_e", now=now)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


def queue(order):
    return json.loads(order.risk_json)["paper_limit_up_queue"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["volume", "bid1_volume"])
@pytest.mark.parametrize("value", INVALID)
async def test_original_unknown_or_invalid_evidence_rejects_without_broker(memory_session, monkeypatch, field, value):
    result, order, _, risk, broker = await setup(memory_session, monkeypatch, **{field: value})
    assert result["order"]["status"] == "rejected"
    assert "queue_evidence_invalid" in order.error_message
    assert result["fills"] == []
    assert broker.place_order.await_count == 0 and risk.await_count == 1
    assert list((await memory_session.scalars(select(TradeFill))).all()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("baseline,ahead", [(0, 0), (50000, 0), (50000, 100)])
async def test_valid_zero_and_exact_coverage_keep_real_fill_contract(memory_session, monkeypatch, baseline, ahead):
    result, order, spot, risk, broker = await setup(memory_session, monkeypatch, volume=baseline, bid1_volume=ahead)
    assert result["order"]["status"] == "submitted"
    original = copy.deepcopy(queue(order))
    assert original["queue_evidence_contract"] == CONTRACT
    assert service.LIMIT_QUEUE_EVIDENCE_CONTRACT_VERSION == CONTRACT
    assert original["baseline_volume_hands"] == baseline and original["queue_ahead_hands"] == ahead
    response = await reconcile(memory_session, monkeypatch, spot, volume=baseline+ahead)
    assert response[0]["event"] == "waiting"
    response = await reconcile(memory_session, monkeypatch, spot, seconds=60, volume=baseline+ahead+1)
    assert response[0]["event"] == "filled", response
    assert broker.place_order.await_count == 1 and risk.await_count == 3
    assert len(list((await memory_session.scalars(select(TradeFill))).all())) == 1
    after = queue(order)
    for key in ("candidate", "buy_validity", "baseline_volume_hands", "queue_ahead_hands", "queue_evidence_contract"):
        assert after[key] == original[key]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["legacy", "wrong_contract", "missing_baseline", "missing_ahead", "bad_original"])
async def test_legacy_or_damaged_original_never_backfills_evidence(memory_session, monkeypatch, mutation):
    _, order, spot, _, broker = await setup(memory_session, monkeypatch)
    data = json.loads(order.risk_json)
    frozen = data["paper_limit_up_queue"]
    if mutation == "legacy":
        frozen.pop("queue_evidence_contract", None)
    elif mutation == "wrong_contract":
        frozen["queue_evidence_contract"] = "old"
    elif mutation == "missing_baseline":
        frozen.pop("baseline_volume_hands")
    elif mutation == "missing_ahead":
        frozen.pop("queue_ahead_hands")
    else:
        frozen["baseline_volume_hands"] = True
    order.risk_json = json.dumps(data)
    await memory_session.commit()
    before = copy.deepcopy(frozen)
    response = await reconcile(memory_session, monkeypatch, spot, volume=999999)
    assert response[0]["event"] == "canceled", response
    assert "queue_evidence" in response[0]["reason"]
    assert broker.place_order.await_count == 0
    after = queue(order)
    for key in ("baseline_volume_hands", "queue_ahead_hands", "candidate", "buy_validity"):
        assert after.get(key) == before.get(key)
    assert list((await memory_session.scalars(select(TradeFill))).all()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", INVALID)
async def test_current_unknown_waits_without_clock_or_baseline_refresh(memory_session, monkeypatch, value):
    _, order, spot, _, broker = await setup(memory_session, monkeypatch)
    before = copy.deepcopy(queue(order))
    response = await reconcile(memory_session, monkeypatch, spot, volume=value)
    assert response[0]["event"] == "waiting", response
    assert order.status == "submitted"
    after = queue(order)
    assert after["buy_validity"] == before["buy_validity"]
    assert after["baseline_volume_hands"] == before["baseline_volume_hands"]
    response = await reconcile(memory_session, monkeypatch, spot, seconds=721, volume=value)
    assert response[0]["event"] == "canceled" and "buy_signal_expired" in response[0]["reason"]
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("after_wait", [False, True])
async def test_cumulative_regression_fails_closed_without_reset(memory_session, monkeypatch, after_wait):
    _, order, spot, _, broker = await setup(memory_session, monkeypatch)
    before = copy.deepcopy(queue(order))
    if after_wait:
        response = await reconcile(memory_session, monkeypatch, spot, volume=50050)
        assert response[0]["event"] == "waiting"
    response = await reconcile(memory_session, monkeypatch, spot, seconds=60, volume=50040 if after_wait else 49999)
    assert response[0]["event"] == "canceled" and "queue_volume_regressed" in response[0]["reason"]
    assert queue(order)["baseline_volume_hands"] == before["baseline_volume_hands"]
    assert queue(order)["buy_validity"] == before["buy_validity"]
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("regressed_now", [False, True])
async def test_route_unknown_cannot_hide_known_cumulative_evidence(memory_session, monkeypatch, regressed_now):
    _, order, spot, _, broker = await setup(memory_session, monkeypatch)
    spot.avg_price = None
    response = await reconcile(memory_session, monkeypatch, spot, volume=49999 if regressed_now else 50050)
    if regressed_now:
        assert response[0]["event"] == "canceled"
        assert "queue_volume_regressed" in response[0]["reason"]
    else:
        assert response[0]["event"] == "waiting"
        assert queue(order)["last_volume_hands"] == 50050
        spot.avg_price = 10.7
        response = await reconcile(memory_session, monkeypatch, spot, seconds=60, volume=50040)
        assert response[0]["event"] == "canceled"
        assert "queue_volume_regressed" in response[0]["reason"]
    assert broker.place_order.await_count == 0
