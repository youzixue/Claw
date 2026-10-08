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
def test_live_route_confirmation_semantics_only_rotate_connected_accounts(monkeypatch, versioner):
    before = versions(versioner)
    monkeypatch.setattr(experiment, "LIVE_ROUTE_CONFIRMATION_CONTRACT_VERSION", "route-confirm-test")
    after = versions(versioner)
    assert {name for name in before if after[name] != before[name]} == set(ROUTE_ACCOUNT_NAMES.values())
    for name in ACCOUNT_NAMES:
        identity = experiment.execution_signal_identity(name)
        assert ("live_route_confirmation_contract" in identity) is (name in ROUTE_ACCOUNT_NAMES.values())


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_primary_confirmation_semantics_only_rotate_seven_primary_accounts(monkeypatch, versioner):
    primary = set(ACCOUNT_NAMES) - set(ROUTE_ACCOUNT_NAMES.values())
    assert primary == {"default", "promotion", "mainline", "auction", "tenbagger",
                       "reversal", "challenger_e"}
    before = versions(versioner)
    monkeypatch.setattr(experiment, "PRIMARY_BUY_CONFIRMATION_CONTRACT_VERSION",
                        "primary-quote-confirm-test")
    after = versions(versioner)
    assert {name for name in before if after[name] != before[name]} == primary
    for name in ACCOUNT_NAMES:
        identity = experiment.execution_signal_identity(name)
        assert ("primary_buy_confirmation_contract" in identity) is (name in primary)
        if name in primary:
            assert "live_route_confirmation_contract" not in identity


@pytest.mark.parametrize("versioner", VERSIONERS)
@pytest.mark.parametrize("account_name", ("default", "promotion", "mainline", "auction",
                                          "tenbagger", "reversal", "challenger_e"))
def test_old_primary_confirmation_identity_cannot_be_reused(versioner, account_name):
    signal = experiment.execution_signal_identity(account_name)
    assert signal.pop("primary_buy_confirmation_contract") == "primary_source_quote_v4"
    if versioner is experiment.standard_execution_version:
        body = {"base": "fixed-base", "account_name": account_name,
                "parameters": experiment.account_parameter_snapshot(account_name),
                "signal_identity": signal}
        prefix = "fixed-base:execution_v2"
    else:
        parameters = experiment.experiment_parameters(account_name)
        parameters["signal_identity"] = signal
        protocol = str(settings.PAPER_EXPERIMENT_VERSION)
        body = {"base": "fixed-base", "protocol": protocol, "parameters": parameters}
        prefix = f"fixed-base:{protocol[:20]}"
    old = f"{prefix}:{hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]}"
    assert versioner("fixed-base", account_name) != old


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


@pytest.mark.parametrize("versioner", VERSIONERS)
@pytest.mark.parametrize("constant,affected", [
    ("PROMOTION_CANDIDATE_CONTRACT_VERSION", {"promotion", "mainline", "auction"}),
    ("REVERSAL_PENDING_EVIDENCE_CONTRACT_VERSION", {"reversal"}),
])
def test_boundary_semantics_rotate_only_consuming_accounts(monkeypatch, versioner, constant, affected):
    before = versions(versioner)
    monkeypatch.setattr(experiment, constant, "isolated-new-contract")
    after = versions(versioner)
    assert {name for name in before if before[name] != after[name]} == affected


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_decision_hold_clock_rotates_all_twelve_not_parameters(monkeypatch, versioner):
    before = versions(versioner)
    parameters = {name: experiment.account_parameter_snapshot(name) for name in ACCOUNT_NAMES}
    monkeypatch.setattr(experiment, "EXIT_HOLD_CLOCK_CONTRACT_VERSION", "exit-clock-test")
    after = versions(versioner)
    assert len(before) == 12 and all(before[name] != after[name] for name in before)
    assert {name: experiment.account_parameter_snapshot(name) for name in ACCOUNT_NAMES} == parameters


@pytest.mark.parametrize("versioner", VERSIONERS)
def test_opening_weak_gate_scope_rotates_only_seven_short_accounts(monkeypatch, versioner):
    before = versions(versioner)
    parameters = {name: experiment.account_parameter_snapshot(name) for name in ACCOUNT_NAMES}
    affected = {"default", "promotion", "mainline", "auction",
                "challenger_b", "challenger_c", "challenger_d"}
    monkeypatch.setattr(experiment, "SHORT_EXIT_WEAK_GATE_CONTRACT_VERSION", "short-gate-test")
    after = versions(versioner)
    assert {name for name in before if before[name] != after[name]} == affected
    assert {name: experiment.account_parameter_snapshot(name) for name in ACCOUNT_NAMES} == parameters
    for name in ACCOUNT_NAMES:
        assert ("short_exit_weak_gate_contract" in experiment.execution_signal_identity(name)) == (name in affected)


def test_c2_path_contract_rotates_only_c2_signal_version(monkeypatch):
    before = {route: shadow.route_version_for(route) for route in shadow.ROUTE_IDS}
    real_rules = shadow._rules
    def old_rules(route):
        rules = real_rules(route)
        rules.pop("confirmed_path_contract", None)
        return rules
    monkeypatch.setattr(shadow, "_rules", old_rules)
    after = {route: shadow.route_version_for(route) for route in shadow.ROUTE_IDS}
    assert {route for route in before if before[route] != after[route]} == {shadow.ROUTE_C}


def test_unknown_account_fails_closed():
    with pytest.raises(KeyError):
        experiment.execution_signal_identity("unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("continuous", (False, True))
@pytest.mark.parametrize("account_name,contract", [
    *((name, "primary_buy_confirmation_contract") for name in (
        "default", "promotion", "mainline", "auction", "tenbagger", "reversal", "challenger_e")),
    *((name, "promotion_candidate_contract") for name in ("promotion", "mainline", "auction")),
    ("reversal", "reversal_pending_evidence_contract"),
])
async def test_primary_contract_upgrade_rejects_old_buy_in_real_version_gate(
    monkeypatch, continuous, account_name, contract,
):
    from types import SimpleNamespace
    from app.api.v1 import paper
    from app.trading import service

    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", continuous)
    current_identity = experiment.execution_signal_identity
    def prior_identity(name):
        identity = current_identity(name)
        identity.pop(contract, None)
        return identity
    with monkeypatch.context() as previous:
        previous.setattr(experiment, "execution_signal_identity", prior_identity)
        old_version = paper._strategy_version(account_name)
    order = SimpleNamespace(account_id=account_name, broker="paper", side="buy",
                            strategy_version=old_version)
    reason = await service._pending_order_version_reason(
        None, order, {}, now=datetime(2026, 9, 21, 10))
    assert "策略版本已变化" in reason and "非保护性减仓" in reason
    order.strategy_version = paper._strategy_version(account_name)
    assert await service._pending_order_version_reason(
        None, order, {}, now=datetime(2026, 9, 21, 10)) == ""


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
