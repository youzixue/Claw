"""Structured missing-data waits stay failclosed and do not bypass route gates."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.config.settings import settings
from app.models.stock import LimitUpPool
from test_pending_buy_validity import AT, quote
from test_paper_deferred_exit_provenance import memory_session


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [
    ("data", "waiting"), ("clock", "waiting"), ("hard", "canceled"),
    ("mixed", "canceled"), ("wrong_code", "canceled"), ("text_only", "canceled"),
])
async def test_primary_uses_structured_diagnostics_not_reason_text(memory_session, monkeypatch, kind, expected):
    async def candidates(*args, diagnostics, **kwargs):
        data = {"stage_code":"data_gate", "reason_code":"candidate_data_missing",
                "candidate":{"recoverable":True}}
        if kind == "data":
            diagnostics.append(data)
        elif kind == "clock":
            diagnostics.append({"stage_code":"data_gate", "reason_code":"prediction_not_visible"})
        elif kind == "hard":
            diagnostics.append({"stage_code":"strategy_filter", "candidate":{"recoverable":True}})
        elif kind == "mixed":
            diagnostics.extend([data, {"stage_code":"strategy_filter"}])
        elif kind == "wrong_code":
            diagnostics.append({**data, "code":"600999"})
        return [], ["缺数据请稍后再试"]  # text must never authorize recoverability
    monkeypatch.setattr(paper, "_promotion_route_buy_candidates", candidates)
    c = {"code":"002988", "_source":"promotion_promotion", "prediction_run_key":"original"}
    status, _ = await paper._pending_primary_buy_confirmation(
        memory_session, account_name="promotion", source=c["_source"], candidate=c,
        spot=quote(AT), limit_price=30.88, now=AT)
    assert status == expected


@pytest.mark.asyncio
async def test_replaced_prediction_identity_cannot_be_consumed(memory_session, monkeypatch):
    monkeypatch.setattr(paper, "_promotion_route_buy_candidates", AsyncMock(
        return_value=([{"prediction_run_key":"new"}], [])))
    c = {"code":"002988", "_source":"promotion_promotion", "prediction_run_key":"old"}
    status, reason = await paper._pending_primary_buy_confirmation(
        memory_session, account_name="promotion", source=c["_source"], candidate=c,
        spot=quote(AT), limit_price=30.88, now=AT)
    assert status == "canceled" and "批次" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("seal,expected", [(None,"waiting"), (1.,"canceled"), (200000000.,"valid")])
async def test_real_e_generator_missing_seal_differs_from_insufficient_seal(memory_session, monkeypatch, seal, expected):
    db = memory_session
    day = AT.date()-timedelta(days=1)
    monkeypatch.setattr(settings, "PAPER_TENBAGGER_ENABLED", True)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=day))
    spot = quote(AT, code="600001", price=11., prev_close=10., high=11.,
        open=10.5, low=10.4, avg_price=10.7, change_pct=10., limit_up=11.,
        bid1_price=11., bid1_volume=100, ask1_price=0., ask1_volume=0., volume=50000)
    monkeypatch.setattr(paper, "_spot_by_code", AsyncMock(return_value=spot))
    db.add(LimitUpPool(code="600001", trade_date=day, consecutive_days=4,
        seal_amount=seal, break_count=0, quarantined=False))
    await db.commit()
    c = {"code":"600001", "_source":"tenbagger_midline", "signal_date":day.isoformat()}
    status, _ = await paper._pending_primary_buy_confirmation(
        db, account_name="challenger_e", source=c["_source"], candidate=c,
        spot=spot, limit_price=11., now=AT)
    assert status == expected


@pytest.mark.asyncio
async def test_missing_change_waits_without_consuming_generator(memory_session, monkeypatch):
    generator = AsyncMock(side_effect=AssertionError("missing current quote must not pass"))
    monkeypatch.setattr(paper, "_promotion_route_buy_candidates", generator)
    c = {"code":"002988", "_source":"promotion_promotion"}
    status, _ = await paper._pending_primary_buy_confirmation(
        memory_session, account_name="promotion", source=c["_source"], candidate=c,
        spot=quote(AT, change_pct=None), limit_price=30.88, now=AT)
    assert status == "waiting" and generator.await_count == 0
