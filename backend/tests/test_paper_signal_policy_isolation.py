"""Active consumers, source evidence versions and settings reject cross-account leakage."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import json

import pytest
from pydantic import ValidationError

from app.api.v1 import paper
from app.config.settings import (
    settings, Settings, PaperConfirmationPolicy, PaperRouteSignalPolicy,
    PaperChallengerExecutionPolicy,
)
from app.paper.account_policy import ACCOUNT_NAMES, route_signal_policy
from app.paper.strategy_iteration_shadow import (
    ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_F2, route_version_for,
    _confirmation_streak_status, _reclaim_confirmed, _rules,
)
from app.paper.strategy_iteration_challenger import _live_route_confirmation_valid, _conservative_entry_price
from app.models.paper import PaperAutoTradeLog
from test_paper_api import paper_client


def versions():
    return {name: paper._strategy_version(name) for name in ACCOUNT_NAMES}


def test_b_confirmation_override_changes_real_filter_and_only_b_version(monkeypatch):
    before = versions()
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_CONFIRMATION_POLICIES", {
        "promotion": PaperConfirmationPolicy(max_pullback_from_high_pct=1),
    })
    assert {name for name, v in before.items() if versions()[name] != v} == {"promotion"}
    spot = SimpleNamespace(price=10.2, avg_price=10, high=10.4, limit_up=11)
    assert not paper._stable_intraday_entry_quote(spot, account_name="promotion")[0]
    assert paper._stable_intraday_entry_quote(spot, account_name="mainline")[0]


def test_c2_signal_override_changes_source_and_recheck_not_other_accounts(monkeypatch):
    before = versions()
    routes = (ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_F2)
    evidence_before = {r: route_version_for(r) for r in routes}
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {
        "challenger_c": PaperRouteSignalPolicy(min_volume_ratio=2, min_samples=4),
    })
    assert {name for name, v in before.items() if versions()[name] != v} == {"challenger_c"}
    assert {r for r, v in evidence_before.items() if route_version_for(r) != v} == {ROUTE_C}
    at = datetime(2026, 9, 8, 10)
    timeline = [at-timedelta(seconds=60), at-timedelta(seconds=30)]
    assert not _confirmation_streak_status(timeline, at, route_id=ROUTE_C)["ready"]
    assert _confirmation_streak_status(timeline, at, route_id=ROUTE_B)["ready"]
    quote = dict(price=10.2, prev_close=10, avg_price=10.1, high=10.3,
                 limit_up=11, ask1_price=10.2, ask1_volume=1000,
                 volume_ratio=1.5, orderbook_imbalance=.5)
    assert not _reclaim_confirmed(quote, min_change_pct=0, max_change_pct=4,
                                 market_change_pct=0, route_id=ROUTE_C)
    assert _reclaim_confirmed(quote, min_change_pct=0, max_change_pct=4,
                             market_change_pct=0, route_id=ROUTE_B)
    event = SimpleNamespace(route_id=ROUTE_C)
    assert not _live_route_confirmation_valid(event=event, snapshot={}, spot=SimpleNamespace(**quote))[0]
    assert _rules(ROUTE_C)["min_volume_ratio"] == 2


def test_d2_execution_override_isolated_from_signal_version(monkeypatch):
    before, evidence_before = versions(), route_version_for(ROUTE_D)
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES", {
        "challenger_d": PaperChallengerExecutionPolicy(max_entry_drift_pct=.05),
    })
    assert {name for name, v in before.items() if versions()[name] != v} == {"challenger_d"}
    assert route_version_for(ROUTE_D) == evidence_before
    event = SimpleNamespace(route_id=ROUTE_D, assumed_fill_price=10, code="600001", name="样本",
                            trade_date=datetime(2026, 9, 8).date())
    spot = SimpleNamespace(price=10, ask1_price=10, ask1_volume=1000, prev_close=9.5, limit_up=10.45)
    assert _conservative_entry_price(event=event, snapshot={}, spot=spot)[0] is None
    event.route_id = ROUTE_B
    assert _conservative_entry_price(event=event, snapshot={}, spot=spot)[0] is not None


def test_main_version_labels_and_legacy_shared_signal_knobs_do_not_change_secondaries(monkeypatch):
    before = versions()
    routes = (ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_F2)
    evidence_before = {r: route_version_for(r) for r in routes}
    monkeypatch.setattr(settings, "PAPER_STRATEGY_B_VERSION", "main-b-new")
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_MIN_VOLUME_RATIO", 99)
    monkeypatch.setattr(settings, "PAPER_CHALLENGER_MAX_ENTRY_DRIFT_PCT", 99)
    assert {name for name, v in before.items() if versions()[name] != v} == {"promotion"}
    assert {r: route_version_for(r) for r in routes} == evidence_before


@pytest.mark.parametrize("field,payload", [
    ("PAPER_ACCOUNT_CONFIRMATION_POLICIES", {"wrong": {}}),
    ("PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {"challenger_c": {"min_samples": 0}}),
    ("PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {"challenger_c": {"min_volume_ratio": float("nan")}}),
    ("PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES", {"challenger_d": {"opening_risk_end": "29:90"}}),
    ("PAPER_ACCOUNT_CONFIRMATION_POLICIES", {"promotion": {"typo": 1}}),
])
def test_policy_configuration_fails_closed_for_unknown_invalid_values(field, payload):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: payload})


@pytest.mark.asyncio
async def test_confirmation_does_not_reuse_peer_old_version_or_future_rows(paper_client):
    _, maker = paper_client
    at = datetime(2026, 9, 8, 10)
    async with maker() as db:
        account = await paper._get_or_create_account(db, "promotion")
        for i, (account_id, version, created, sample) in enumerate([
            (account.id, "old", at-timedelta(seconds=60), at-timedelta(seconds=60)),
            (account.id+100, paper._strategy_version("promotion"), at-timedelta(seconds=60), at-timedelta(seconds=60)),
            (account.id, paper._strategy_version("promotion"), at+timedelta(seconds=1), at-timedelta(seconds=60)),
        ]):
            db.add(PaperAutoTradeLog(account_id=account_id, strategy_version=version,
                trade_date=at.date(), created_at=created, run_id=f"case-{i}", code="600001",
                source="promotion", action="confirm_buy", decision="wait",
                candidate_json=json.dumps({"confirmation_version":"champion_persistent_v1",
                                           "confirmation_sample_at":sample.isoformat()})))
        await db.flush()
        kwargs = dict(account_id=account.id, account_name="promotion", trade_date=at.date(),
                      code="600001", source="promotion", current_at=at)
        assert (await paper._champion_intraday_confirmation_status(db, **kwargs))[0] is False
        db.add(PaperAutoTradeLog(account_id=account.id, strategy_version=paper._strategy_version("promotion"),
            trade_date=at.date(), created_at=at-timedelta(seconds=60), run_id="valid", code="600001",
            source="promotion", action="confirm_buy", decision="wait",
            candidate_json=json.dumps({"confirmation_version":"champion_persistent_v1",
                                       "confirmation_sample_at":(at-timedelta(seconds=60)).isoformat()})))
        await db.flush()
        assert (await paper._champion_intraday_confirmation_status(db, **kwargs))[0] is True
