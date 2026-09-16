"""Read-only candidate selection against isolated fixtures; never submit orders."""
import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.models.stock import StockSpot
from app.promotion.versioning import (
    PROBABILITY_CONTRACT_VERSION, LEGACY_PROBABILITY_CONTRACT_VERSION,
    ProbabilityContractError,
)
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot


def evidence(production=0.0):
    return {
        "probability_contract_version": PROBABILITY_CONTRACT_VERSION,
        "p_raw": 0.7, "p_calibrated": 0.6, "production_probability": production,
    }


@pytest.mark.parametrize("field", ["p_raw", "p_calibrated", "production_probability"])
@pytest.mark.parametrize("value", [None, True, False, "0.9", "", float("nan"), float("inf"), -float("inf"), -0.1, 1.1])
def test_snapshot_bad_frozen_evidence_never_uses_valid_old_column(field, value):
    frozen = evidence()
    frozen[field] = value
    row = SimpleNamespace(features_json=json.dumps({"probability_contract": frozen}), calibrated_probability=.99)
    with pytest.raises(ProbabilityContractError):
        paper._promotion_snapshot_probability(row)


@pytest.mark.parametrize("field", ["p_raw", "p_calibrated", "production_probability", "probability_contract_version"])
def test_snapshot_missing_frozen_field_never_falls_back(field):
    frozen = evidence()
    del frozen[field]
    row = SimpleNamespace(features_json=json.dumps({"probability_contract": frozen}), calibrated_probability=.99)
    with pytest.raises(ProbabilityContractError):
        paper._promotion_snapshot_probability(row)


@pytest.mark.parametrize("features", [
    '{', '[]', '{"probability_contract":null}', '{"production_probability":0.9}',
    '{"probability_contract":{"probability":0.99}}',
])
def test_invalid_features_fail_closed(features):
    with pytest.raises(ProbabilityContractError):
        paper._promotion_snapshot_probability(SimpleNamespace(features_json=features, calibrated_probability=.99))


def test_new_zero_and_explicit_frozen_legacy_and_old_zero():
    row = SimpleNamespace(features_json=json.dumps({"probability_contract": evidence(0)}), calibrated_probability=.99)
    assert paper._promotion_snapshot_probability(row) == 0
    old = evidence(0)
    old["probability_contract_version"] = LEGACY_PROBABILITY_CONTRACT_VERSION
    row.features_json = json.dumps({"probability_contract": old})
    assert paper._promotion_snapshot_probability(row) == 0
    row.features_json = "{}"
    row.calibrated_probability = 0
    assert paper._promotion_snapshot_probability(row) == 0


@pytest.mark.parametrize("value", [None, True, False, "0.8", float("nan"), float("inf"), -1, 2])
def test_invalid_legacy_column_does_not_become_zero(value):
    with pytest.raises(ProbabilityContractError):
        paper._promotion_snapshot_probability(SimpleNamespace(features_json="{}", calibrated_probability=value))


@pytest.mark.asyncio
@pytest.mark.parametrize("account,target,route", [
    ("promotion", 2, "second_board_promotion"),
    ("mainline", 1, "mainline_spread_start"),
    ("auction", 1, "auction_surge_start"),
])
async def test_bad_watch_row_blocks_selected_route_before_filters(paper_client, monkeypatch, account, target, route):
    _, maker = paper_client
    day = date(2026, 9, 7)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 4)))
    async with maker() as db:
        run = _governed_promotion_run(run_key="bad-probability", reference_trade_date=day,
            snapshot_context="promotion_0935", as_of_at=datetime(2026, 9, 7, 9, 35))
        db.add(run)
        await db.flush()
        good = _governed_promotion_snapshot(run_id=run.id, record_key="good", code="600001",
            prediction_trade_date=day, target_board=target, route=route)
        bad = _governed_promotion_snapshot(run_id=run.id, record_key="bad-watch", code="600002",
            prediction_trade_date=day, target_board=target, route=route,
            probability=.99, actionable=False, watch_only=True)
        bad.features_json = json.dumps({"probability_contract": evidence(None)})
        db.add_all([good, bad])
        await db.flush()
        diagnostics = []
        candidates, notes = await paper._promotion_route_buy_candidates(
            db, limit=10, trade_date=day, account_name=account, diagnostics=diagnostics,
            now=datetime(2026, 9, 7, 10))
        assert candidates == []
        assert "禁止回退" in notes[0]
        assert diagnostics[0]["reason_code"] == "probability_contract_invalid"
        assert diagnostics[0]["candidate"]["route_pool"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("account,target,route,expected", [
    ("promotion", 2, "second_board_promotion", ["600003", "600002"]),
    ("mainline", 1, "mainline_spread_start", ["600003", "600002", "600001"]),
    ("auction", 1, "auction_surge_start", ["600003", "600002", "600001"]),
])
@pytest.mark.parametrize("invalid_news", [None, "claimed-ready", [], True, 1])
async def test_threshold_sort_and_output_use_production_not_old_columns(
    paper_client, monkeypatch, account, target, route, expected, invalid_news,
):
    _, maker = paper_client
    day = date(2026, 9, 7)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 4)))
    monkeypatch.setattr(paper.settings, "PAPER_PROMOTION_MIN_PROBABILITY", .25)
    async with maker() as db:
        run = _governed_promotion_run(run_key="production-sort", reference_trade_date=day,
            snapshot_context="promotion_0935", as_of_at=datetime(2026, 9, 7, 9, 35))
        db.add(run)
        await db.flush()
        snapshots = {}
        frozen_news = {"status": "unknown", "evidence_id": "frozen-only-test", "nested": {"ids": [1]}}
        for code, old, production in [("600001", .99, 0), ("600002", .1, .4), ("600003", .01, .5)]:
            row = _governed_promotion_snapshot(run_id=run.id, record_key=code, code=code,
                prediction_trade_date=day, target_board=target, route=route, probability=old)
            factors = {"probability_contract": evidence(production)}
            if code == "600003":
                factors["news_evidence"] = frozen_news
            elif code == "600002":
                factors["news_evidence"] = invalid_news
            row.features_json = json.dumps(factors)
            snapshots[code] = row
            db.add_all([row, StockSpot(code=code, name=code, price=10.15, prev_close=10,
                                      change_pct=1.5, volume_ratio=1.8)])
        await db.flush()
        candidates, _ = await paper._promotion_route_buy_candidates(
            db, limit=10, trade_date=day, account_name=account, now=datetime(2026, 9, 7, 10))
        assert [item["code"] for item in candidates] == expected
        for item in candidates:
            assert item["prediction_snapshot_id"] == snapshots[item["code"]].id
            assert item["news_evidence"] == (frozen_news if item["code"] == "600003" else {})
        candidates[0]["news_evidence"]["nested"]["ids"].append(2)
        assert json.loads(snapshots["600003"].features_json)["news_evidence"]["nested"]["ids"] == [1]
        assert [item["probability"] for item in candidates] == ([.5, .4] if account == "promotion" else [.5, .4, 0])


@pytest.mark.parametrize("production", [None, float("nan"), 0.0])
def test_mainline_independent_confirmation_does_not_fallback(production, monkeypatch):
    day = date(2026, 9, 7)
    monkeypatch.setattr(paper.settings, "PAPER_MAINLINE_LIVE_CONFIRM_ENABLED", True)
    monkeypatch.setattr(paper.settings, "PAPER_MAINLINE_LIVE_CONFIRM_MIN_PROBABILITY", .02)
    row = SimpleNamespace(
        rank_scope="ranked", calibrated_probability=.99,
        features_json=json.dumps({"probability_contract": evidence(production),
            "broad_rotation_member_setup": True, "sector_catalyst_spread": True}),
    )
    reason = paper._promotion_mainline_live_confirm_reject_reason(
        row, SimpleNamespace(snapshot_context="promotion_0935", reference_trade_date=day),
        trade_date=day, sector_context={})
    assert ("合同无效" in reason) if production is None or production != production else ("0.000低于" in reason)
