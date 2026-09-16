"""Execution identity regression: real route semantics, isolated TTLs, old exits.

Only pytest-isolated databases/ASGI are used; no production database or broker.
"""
import hashlib
import json
from datetime import date, datetime, timedelta

import pytest

from app.config.settings import settings, PaperChallengerExecutionPolicy
from app.paper import experiment, strategy_iteration_shadow as shadow
from app.paper.account_policy import ACCOUNT_NAMES, ROUTE_ACCOUNT_NAMES

VERSIONERS = (experiment.execution_version, experiment.standard_execution_version)


def versions(versioner):
    return {name: versioner("fixed-base", name) for name in ACCOUNT_NAMES}


@pytest.mark.parametrize("versioner", VERSIONERS)
@pytest.mark.parametrize("route", (shadow.ROUTE_B, shadow.ROUTE_C, shadow.ROUTE_D, shadow.ROUTE_F2))
def test_real_route_identity_rotation_is_account_local(monkeypatch, versioner, route):
    before = versions(versioner)
    original = shadow.route_version_for
    monkeypatch.setattr(shadow, "route_version_for",
                        lambda key: original(key) + "-new-semantics" if key == route else original(key))
    after = versions(versioner)
    assert {name for name in before if after[name] != before[name]} == {ROUTE_ACCOUNT_NAMES[route]}


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_a2_signal_version_rotation_does_not_rotate_peer_accounts(monkeypatch, versioner):
    before = versions(versioner)
    monkeypatch.setattr(settings, "PAPER_MOMENTUM_RETEST_SHADOW_VERSION", "momentum-test-new")
    after = versions(versioner)
    assert {name for name in before if after[name] != before[name]} == {"challenger_a"}


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_pending_guard_semantics_rotates_all_accounts(monkeypatch, versioner):
    before = versions(versioner)
    monkeypatch.setattr(experiment, "PENDING_BUY_VALIDITY_VERSION", "pending_buy_validity_test_v2")
    assert all(versioner("fixed-base", name) != old for name, old in before.items())


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_global_pending_ttl_only_rotates_accounts_that_consume_it(monkeypatch, versioner):
    before = versions(versioner)
    monkeypatch.setattr(settings, "PAPER_PENDING_BUY_MAX_AGE_SEC",
                        settings.PAPER_PENDING_BUY_MAX_AGE_SEC + 17)
    after = versions(versioner)
    assert {name for name in before if after[name] != before[name]} == (
        set(ACCOUNT_NAMES) - set(ROUTE_ACCOUNT_NAMES.values())
    )


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_one_route_pending_ttl_only_rotates_its_account(monkeypatch, versioner):
    before = versions(versioner)
    policies = dict(settings.PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES)
    policies["challenger_c"] = PaperChallengerExecutionPolicy(max_execution_delay_sec=123)
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_CHALLENGER_EXECUTION_POLICIES", policies)
    after = versions(versioner)
    assert {name for name in before if after[name] != before[name]} == {"challenger_c"}
    assert experiment.execution_signal_identity("challenger_c")["pending_buy_max_age_sec"] == 123


def test_snapshot_contains_real_source_identity_not_copied_static_base():
    for route, name in ROUTE_ACCOUNT_NAMES.items():
        identity = experiment.experiment_parameters(name)["signal_identity"]
        expected = (settings.PAPER_MOMENTUM_RETEST_SHADOW_VERSION if name == "challenger_a"
                    else shadow.route_version_for(route))
        assert identity["route_id"] == route
        assert identity["route_version"] == expected
        assert identity["pending_buy_guard_version"] == "pending_buy_validity_v2"


def test_old_execution_hash_cannot_be_reused_after_upgrade():
    for name in ACCOUNT_NAMES:
        params = experiment.experiment_parameters(name)
        params.pop("signal_identity")
        old_identity = json.dumps({"base": "fixed-base",
                                   "protocol": str(settings.PAPER_EXPERIMENT_VERSION),
                                   "parameters": params}, sort_keys=True)
        digest = hashlib.sha256(old_identity.encode()).hexdigest()[:12]
        old = f"fixed-base:{str(settings.PAPER_EXPERIMENT_VERSION)[:20]}:{digest}"
        assert experiment.execution_version("fixed-base", name) != old


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_deterministic_bounded_owned_identity(versioner):
    before = versions(versioner)
    assert before == versions(versioner)
    assert len(set(before.values())) == len(ACCOUNT_NAMES)
    assert all(len(versioner("x" * 128, name)) <= 64 for name in ACCOUNT_NAMES)
    assert "c3_mainline_first_board" not in str(
        experiment.execution_signal_identity("challenger_c"))


def test_unknown_account_fails_closed():
    with pytest.raises(KeyError):
        experiment.execution_signal_identity("unknown")


# Existing reusable fixture is isolated by tests/conftest.py before app imports.
from test_paper_api import paper_client, qualified_execution_risk, qualified_manual_execution  # noqa: E402, F401


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_version", [None, "prior-execution-v1"])
async def test_new_execution_identity_blocks_scale_in_but_preserves_legacy_exit(
    paper_client, qualified_manual_execution, monkeypatch, legacy_version,
):
    from app.api.v1 import paper
    from app.models.paper import PaperPosition, PaperTradeLog
    from app.models.stock import StockSpot

    from app.core.trade_calendar import trade_calendar

    # Deterministic test-only calendar: never fetch or populate a live calendar.
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(365)]
    monkeypatch.setattr(trade_calendar, "_cache", {day: day.weekday() < 5 for day in days})
    client, maker = paper_client
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    async with maker() as db:
        account = await paper._get_or_create_account(db, "default")
        db.add(StockSpot(code="000001", name="版本隔离样本", price=10.0))
        db.add(PaperPosition(account_id=account.id, code="000001", name="版本隔离样本",
                             buy_price=10, buy_amount=100, buy_time=datetime(2026, 9, 9, 10),
                             current_price=10, strategy_version=legacy_version, is_closed=False))
        # 真实退出正例显式提供买入数量/已付费用，不能从裸持仓猜零买费。
        db.add(PaperTradeLog(account_id=account.id,code="000001",trade_type="buy",
            price=10,amount=100,commission=5,tax=0,trade_time=datetime(2026,9,9,10),
            strategy_version=legacy_version,signal_id="explicit-legacy-fee-basis"))
        await db.commit()

    await qualified_manual_execution("000001", "版本隔离样本")
    response = await client.post("/paper/buy", json={"code": "000001", "price": 10, "amount": 100})
    assert response.status_code == 409
    assert "版本" in response.json()["detail"]
    await qualified_manual_execution("000001", "版本隔离样本", ask=10.01, bid=10)
    response = await client.post("/paper/sell", json={"code": "000001", "price": 10, "amount": 100})
    assert response.status_code == 200
    assert response.json()["trade"]["strategy_version"] == (legacy_version or "legacy_unversioned")
