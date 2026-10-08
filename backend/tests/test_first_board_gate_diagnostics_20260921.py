"""C3 diagnostics must come from the same predicates, not a second strategy."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperShadowEvent
from app.paper import strategy_iteration_shadow as shadow
from test_strategy_iteration_shadow import (
    shadow_env, _first_board_quote, _seed_first_board_denominator, _freeze_shadow_today,
)


def sector():
    return dict(sector_strength=60., sector_change_pct=1., sector_fund_flow=10_000_000.,
                sector_limit_up_count=2)


@pytest.mark.parametrize("fields,issue", [
    ({"price": None}, "price_invalid"),
    ({"prev_close": 0}, "prev_close_invalid"),
    ({"high": None}, "high_invalid"),
    ({"limit_up": None}, "limit_up_invalid"),
    ({"high": 11.}, "already_touched_limit"),
    ({"ask1_volume": 0}, "offer_unavailable"),
    ({"volume_ratio": None}, "volume_ratio_gate"),
    ({"price": 10.8, "high": 10.8}, "change_pct_gate"),
])
def test_eligibility_rejection_names_same_failing_gate(fields, issue):
    ok, metrics = shadow._first_board_eligibility(
        {**_first_board_quote("600201", confirms=True), **fields}, sector())
    assert not ok
    assert issue in metrics["eligibility_gate_issues"]


@pytest.mark.parametrize("field,issue", [
    ("sector_strength", "sector_strength_gate"),
    ("sector_change_pct", "sector_change_gate"),
    ("sector_fund_flow", "sector_flow_gate"),
    ("sector_limit_up_count", "sector_limit_up_count_gate"),
])
def test_sector_failure_is_not_generic(field, issue):
    ok, metrics = shadow._first_board_eligibility(
        _first_board_quote("600201", confirms=True), {**sector(), field: None})
    assert not ok and issue in metrics["eligibility_gate_issues"]


@pytest.mark.parametrize("fields,issue", [
    ({"avg_price": None}, "vwap_gate"), ({"avg_price": 10.4}, "vwap_gate"),
    ({"high": 10.8}, "pullback_gate"), ({"orderbook_imbalance": None}, "orderbook_gate"),
    ({"orderbook_imbalance": -.9}, "orderbook_gate"),
])
def test_confirmation_gate_diagnostics(fields, issue):
    ok, metrics = shadow._first_board_confirmation_frame(
        {**_first_board_quote("600201", confirms=True), **fields}, sector())
    assert not ok
    assert issue in metrics["confirmation_gate_issues"]


def test_valid_frame_has_no_reasons_and_does_not_change_version():
    original = shadow.route_version_for(shadow.ROUTE_C3)
    ok, metrics = shadow._first_board_confirmation_frame(
        _first_board_quote("600201", confirms=True), sector())
    assert ok and metrics["confirmation_gate_issues"] == []
    assert metrics["eligibility_gate_issues"] == []
    assert original == shadow.route_version_for(shadow.ROUTE_C3)


@pytest.mark.asyncio
async def test_universe_member_keeps_exact_gate_without_extra_per_stock_rows(shadow_env, monkeypatch):
    at = datetime(2026, 9, 4, 10)
    _freeze_shadow_today(monkeypatch, at.date())
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE", "2026-09-04")
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
        quotes = [_first_board_quote(code, confirms=True) for code in ("600201", "600202")]
        quotes[0]["volume_ratio"] = None
        quotes[1]["avg_price"] = 10.4
        await shadow.scan_strategy_iteration_shadow(db, quotes, at)
        events = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == shadow.ROUTE_C3))).all())
        audit = next(row for row in events if row.event_type == "universe_audit")
        members = {m["code"]: m for m in json.loads(audit.snapshot_json)["prior_structure"]["current_pool"]["members"]}
        assert members["600201"]["gate_issues"] == ["volume_ratio_gate"]
        assert members["600202"]["gate_issues"] == ["vwap_gate"]
        assert not any(row.event_type in {"confirmed", "gate_rejected"} for row in events)


def test_a2_description_does_not_claim_stale_fixed_budget():
    meta = paper._strategy_display_meta(SimpleNamespace(account_name="challenger_a"))
    assert "10%预算" not in meta["desc"]
    assert "当前账户配置" in meta["desc"]
