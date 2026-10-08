"""Pure observation v2: inherited frozen 9/23 fixtures, no DB/network/runtime."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import pytest

from app.paper.intraday_route_research import (
    RouteResearchPolicy, evaluate_strategy_candidate_experiment,
    evaluate_strategy_candidate_experiment_v2,
)
from test_strategy_candidate_experiment_20260924 import START, candidate, row


def fixture(route="C2"):
    c = candidate(route)
    rows = [dict(row(0, price=10.2), account_id=c["account_id"], account_name=c["account_name"]),
            dict(row(30, price=10.3), account_id=c["account_id"], account_name=c["account_name"])]
    c["execution_strategy_version"] = "execution-v1"
    contract = {k: c[k] for k in (
        "route", "account_id", "account_name", "execution_strategy_version")}
    contract.update(schema="candidate_shadow_execution_freshness_v1",
                    policy_observed_at=START.isoformat(), max_execution_delay_sec=90)
    return c, rows, contract


def evaluate(c=None, rows=None, contract=None, seconds=30, **kwargs):
    default_c, default_rows, default_contract = fixture()
    return evaluate_strategy_candidate_experiment_v2(
        default_c if c is None else c, default_rows if rows is None else rows,
        as_of=START + timedelta(seconds=seconds),
        execution_contract=default_contract if contract is None else contract, **kwargs)


def test_original_module_bytes_preserved_exactly():
    data = Path(__file__).resolve().parents[1].joinpath(
        "app/paper/intraday_route_research.py").read_bytes()
    prefix = data.split(b"\n\ndef _experiment_v2_freshness", 1)[0]
    assert hashlib.sha256(prefix).hexdigest() == (
        "c77d0dff024f108a58737a5db8793a21c2a2dc0eb54a874ac01d7ac5e7beaf47")


def test_flat_vwap_with_actual_price_progress_is_only_new_hypothesis():
    out = evaluate()
    assert out["baseline"]["value"] is True
    assert out["candidate"]["value"] is False  # v1 strict slope counterexample kept
    assert out["research_v2"]["value"] is True
    assert out["confirmation_freshness"]["value"] is True
    assert out["research_v2"]["is_buy_point"] is False
    assert out["historically_executable"] is None
    assert out["orders_created"] == out["pushes_created"] == 0
    assert out["production_permission"] is False


def test_v1_output_fields_unchanged_and_cached_result_verified():
    c, rows, contract = fixture()
    v1 = evaluate_strategy_candidate_experiment(c, rows, as_of=START+timedelta(seconds=30))
    result = evaluate(c=c, rows=rows, contract=contract, original_result=v1)
    assert all(result[key] == value for key, value in v1.items())
    assert result["research_v2"]["value"] is True
    bad = deepcopy(v1)
    bad["baseline"]["confirmed_at"] = (START+timedelta(seconds=30)).isoformat()
    assert evaluate(original_result=bad)["research_v2"]["reason"] == "original_result_mismatch"


def test_inputs_are_owned_no_mutation():
    c, rows, contract = fixture()
    before = deepcopy((c, rows, contract))
    out = evaluate(c=c, rows=rows, contract=contract)
    out["research_v2"]["evidence_refs"].append("not-input")
    out["policy"]["min_samples"] = 100
    assert (c, rows, contract) == before


@pytest.mark.parametrize("seconds,expected", [(90, True), (91, False), (300, False)])
def test_original_ttl_inclusive_boundary_and_never_refreshes(seconds, expected):
    c, rows, contract = fixture()
    rows[-1].update(source_quote_at=(START+timedelta(seconds=seconds)).isoformat(),
                    received_at=(START+timedelta(seconds=seconds)).isoformat(),
                    observed_at=(START+timedelta(seconds=seconds)).isoformat())
    out = evaluate(c=c, rows=rows, contract=contract, seconds=seconds)
    assert out["confirmation_freshness"]["value"] is expected
    assert out["confirmation_freshness"]["original_confirmed_at"] == START.isoformat()
    assert out["baseline"]["value"] is True
    if not expected:
        assert out["research_v2"]["value"] is False


def test_missing_contract_is_unknown_not_assumed_production_ttl():
    c, rows, _ = fixture()
    out = evaluate_strategy_candidate_experiment_v2(c, rows, as_of=START+timedelta(seconds=30))
    assert out["confirmation_freshness"]["value"] is None
    assert out["research_v2"]["value"] is None


@pytest.mark.parametrize("ttl", [None, 0, -1, True, "90", float("inf"), float("nan")])
def test_invalid_explicit_ttl_is_unknown(ttl):
    c, rows, contract = fixture()
    contract["max_execution_delay_sec"] = ttl
    assert evaluate(c=c, rows=rows, contract=contract)["confirmation_freshness"]["value"] is None


@pytest.mark.parametrize("key,value", [
    ("execution_strategy_version", "different"), ("account_id", 2), ("account_id", True),
    ("account_name", "default"), ("route", "C3"), ("schema", "unknown"),
    ("execution_strategy_version", ""),
])
def test_contract_identity_mismatch_or_missing_evidence(key, value):
    c, rows, contract = fixture()
    contract[key] = value
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


def test_future_policy_clock_cannot_be_attached_retroactively():
    c, rows, contract = fixture()
    contract["policy_observed_at"] = (START+timedelta(seconds=31)).isoformat()
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


def test_policy_clock_is_not_confirmation_anchor():
    c, rows, contract = fixture()
    contract["policy_observed_at"] = rows[-1]["observed_at"]
    out = evaluate(c=c, rows=rows, contract=contract)
    assert out["confirmation_freshness"]["age_sec"] == 30
    assert out["confirmation_freshness"]["original_confirmed_at"] == START.isoformat()


def test_previous_day_startup_policy_is_valid_without_backdating():
    c, rows, contract = fixture()
    contract["policy_observed_at"] = (START-timedelta(days=1)).isoformat()
    out = evaluate(c=c, rows=rows, contract=contract)
    assert out["candidate_v2"]["value"] is True
    assert out["candidate_v2"] == out["research_v2"]
    assert out["confirmation_freshness"]["policy_observed_at"] == contract["policy_observed_at"]


@pytest.mark.parametrize("anchor", [None, "bad", "2026-09-23T10:01:00", "2026-09-23T10:00:00+08:00"])
def test_missing_future_or_unsupported_anchor_clock(anchor):
    c, rows, contract = fixture()
    c["original_confirmed_at"] = contract["original_confirmed_at"] = anchor
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


@pytest.mark.parametrize("seconds,reason", [
    (2*3600, "original_confirmation_cross_session"),
    (3*3600, "original_confirmation_cross_session"),
    (24*3600, "original_confirmation_cross_day"),
])
def test_lunch_afternoon_and_next_day_cannot_reuse_original(seconds, reason):
    c, rows, contract = fixture()
    contract["max_execution_delay_sec"] = 200000
    out = evaluate(c=c, rows=rows, contract=contract, seconds=seconds)
    assert out["confirmation_freshness"]["reason"] == reason
    assert out["research_v2"]["value"] is False


def test_stale_quote_does_not_become_current_with_fresh_contract():
    c, rows, contract = fixture()
    contract["max_execution_delay_sec"] = 600
    out = evaluate(c=c, rows=rows, contract=contract, seconds=211)
    assert out["confirmation_freshness"]["value"] is True
    assert out["research_v2"]["reason"] == "current_quote_or_confirmation_clock_unproven"


@pytest.mark.parametrize("price,expected", [(10.2, False), (10.19, False), (10.3, True)])
def test_flat_down_and_genuine_up_price(price, expected):
    c, rows, contract = fixture()
    rows[-1]["price"] = rows[-1]["ask1_price"] = price
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is expected


def test_intermediate_vwap_dip_cannot_hide_behind_rising_endpoints():
    c, rows, contract = fixture()
    rows.insert(1, dict(rows[0], sample_id="middle", evidence_ref="middle",
        source_quote_at=(START+timedelta(seconds=15)).isoformat(),
        received_at=(START+timedelta(seconds=15)).isoformat(),
        observed_at=(START+timedelta(seconds=15)).isoformat(), avg_price=10.0, price=10.25))
    rows[-1]["avg_price"] = 10.2
    out = evaluate(c=c, rows=rows, contract=contract)
    assert out["candidate"]["value"] is True
    assert out["research_v2"]["value"] is False
    assert out["research_v2"]["vwap_nondecreasing"] is False


def test_rising_vwap_does_not_replace_actual_price_progress():
    c, rows, contract = fixture()
    rows[0]["avg_price"] = 10
    rows[-1]["price"] = rows[0]["price"]
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is False


def test_one_weak_frame_and_gap_cannot_satisfy_path():
    c, rows, contract = fixture()
    assert evaluate(c=c, rows=rows[-1:], contract=contract)["research_v2"]["value"] is None
    rows[0]["source_quote_at"] = rows[0]["received_at"] = rows[0]["observed_at"] = (
        START-timedelta(seconds=60)).isoformat()
    rows[0]["sample_role"] = "feature_history"
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


def test_frozen_policy_count_and_duration_not_ignored():
    p = RouteResearchPolicy("research:test-v2", 3, 60, 75)
    assert evaluate(policy=p)["research_v2"]["value"] is None
    p = RouteResearchPolicy("research:test-v2", 1, 0, 75)
    _, rows, _ = fixture()
    assert evaluate(rows=rows[-1:], policy=p)["research_v2"]["value"] is None


@pytest.mark.parametrize("key,value,expected", [
    ("relative_strength_pct", -0.1, False), ("relative_strength_pct", 0, True),
    ("relative_strength_pct", None, None), ("avg_price", None, None),
    ("avg_price", 10.4, False), ("original_gate", False, False),
    ("original_gate", None, None), ("original_gate_ref", "", None),
    ("ask1_price", 0, False), ("ask1_volume", 0, False),
    ("ask1_price", None, None), ("ask1_volume", float("inf"), None),
    ("limit_up", 10.25, False),
])
def test_current_quality_offer_and_gate_boundaries(key, value, expected):
    c, rows, contract = fixture()
    rows[-1][key] = value
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is expected


@pytest.mark.parametrize("key,value", [
    ("account_id", 2), ("account_name", "default"), ("code", "600002"),
    ("production_version", "next"), ("candidate_id", "wrong"), ("price", float("nan")),
    ("prev_close", 9), ("evidence_ref", ""), ("round_id", "other"),
])
def test_illegal_sample_identity_version_or_price_fails_closed(key, value):
    c, rows, contract = fixture()
    rows[-1][key] = value
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


def test_false_pool_identity_does_not_get_rescued():
    c, rows, contract = fixture()
    c["original_candidate"] = False
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is False


def test_future_samples_and_future_predicates_cannot_change_past_result():
    c, rows, contract = fixture()
    baseline = evaluate(c=c, rows=rows, contract=contract)
    future = dict(rows[-1], observed_at=(START+timedelta(seconds=31)).isoformat(),
                  account_id=999, price=-1)
    assert evaluate(c=c, rows=rows+[future], contract=contract) == baseline
    future["observed_at"] = rows[-1]["observed_at"]
    future["predicate_observed_at"] = (START+timedelta(seconds=31)).isoformat()
    assert evaluate(c=c, rows=rows+[future], contract=contract) == baseline


def test_duplicate_same_source_never_counts_as_second_frame():
    c, rows, contract = fixture()
    duplicate = dict(rows[0], sample_id="copy", evidence_ref="copy",
                     observed_at=rows[-1]["observed_at"], received_at=rows[-1]["received_at"])
    out = evaluate(c=c, rows=[rows[0], duplicate], contract=contract)
    assert out["sample_count"] == 1
    assert out["research_v2"]["value"] is None


def test_conflicting_duplicate_and_reverse_arrival_fail_closed():
    c, rows, contract = fixture()
    assert evaluate(c=c, rows=rows+[dict(rows[-1], price=10.4)], contract=contract)[
        "research_v2"]["value"] is None
    rows[0]["observed_at"] = rows[-1]["observed_at"]
    rows[-1]["sample_id"] = "a"
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


def test_input_order_is_not_arrival_order():
    c, rows, contract = fixture()
    assert evaluate(c=c, rows=rows, contract=contract) == evaluate(c=c, rows=rows[::-1], contract=contract)


@pytest.mark.parametrize("change", [
    {"stage": "reset"}, {"stage": "cancelled"}, {"producer_reset": True},
    {"candidate_active": False},
])
def test_old_confirmation_cannot_revive_after_reset(change):
    c, rows, contract = fixture()
    rows[0].update(change)
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is False


@pytest.mark.parametrize("key", ["episode_id", "stream_id", "generation"])
def test_generation_change_cannot_bridge_nearby_quotes(key):
    c, rows, contract = fixture()
    rows[0][key], rows[-1][key] = "before", "after"
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


@pytest.mark.parametrize("route", ["A", "A2", "B", "B2", "C", "D", "D2", "E", "E2", "F", "F2", "C3"])
def test_other_routes_are_only_v1_plus_freshness_diagnostics(route):
    c, rows, contract = fixture(route)
    if route == "F2":
        c["broken_board_identity"] = True
    v1 = evaluate_strategy_candidate_experiment(c, rows, as_of=START+timedelta(seconds=30))
    out = evaluate(c=c, rows=rows, contract=contract)
    assert out["candidate"] == out["v1_diagnostic"] == v1["candidate"]
    assert out["research_v2"]["value"] is None
    assert out["research_v2"]["reason"] == "v1_diagnostic_only_no_new_algorithm"
    if route == "F2":
        assert out["candidate"]["value"] is None
        assert out["candidate"]["reason"] == "five_minute_extension_coverage_missing"
    if route == "C3":
        assert out["account_id"] is None and out["account_name"] is None


@pytest.mark.parametrize("candidate_value", [None, {}, {"route": "C2"}, {"route": []}])
def test_malformed_candidate_is_unknown(candidate_value):
    assert evaluate_strategy_candidate_experiment_v2(
        candidate_value, [], as_of=START)["research_v2"]["value"] is None


def test_invalid_asof_or_policy_rejected_explicitly():
    with pytest.raises(ValueError):
        evaluate_strategy_candidate_experiment_v2({}, [], as_of="bad")
    with pytest.raises(ValueError):
        evaluate(policy={})


def test_real_jixin_frozen_case_keeps_v1_counterexample_without_inventing_ttl():
    path = Path(__file__).resolve().parents[2] / (
        "outputs/shadow_repair_20260924/core/frozen_jixin_case.json")
    case = json.loads(path.read_text())
    out = evaluate_strategy_candidate_experiment_v2(
        case["candidate_input"], case["sample_inputs"], as_of=case["predicate_asof"],
        original_result=case["result"], execution_contract=case["execution_contract"])
    assert out["baseline"] == case["result"]["baseline"]
    assert out["candidate"] == case["result"]["candidate"]
    assert out["baseline"]["value"] is True
    assert out["candidate"]["value"] is False
    assert out["candidate"]["vwap_slope_pct"] == 0
    assert out["confirmation_freshness"]["value"] is None
    assert out["research_v2"]["reason"] == "execution_contract_missing"
    # Actual contemporaneous prices were falling, not the rising-platform hypothesis.
    assert [r["price"] for r in case["sample_inputs"]] == [5.12, 5.11, 5.08, 5.08]


@pytest.mark.parametrize("key,value", [
    ("source_quote_at", "2026-09-23T10:00:31"),
    ("received_at", "2026-09-23T10:00:31"),
    ("committed_at", "2026-09-23T10:00:31"),
    ("updated_at", "2026-09-23T10:00:31"),
    ("source_quote_at", "2026-09-22T10:00:30"),
    ("source_quote_at", "2026-09-23T10:00:30+08:00"),
])
def test_incoherent_sample_clock_cannot_certify_past(key, value):
    c, rows, contract = fixture()
    rows[-1][key] = value
    assert evaluate(c=c, rows=rows, contract=contract)["research_v2"]["value"] is None


def test_intermediate_price_drop_is_not_endpoint_progress():
    c, rows, contract = fixture()
    middle = dict(rows[0], sample_id="middle", evidence_ref="middle",
        source_quote_at=(START+timedelta(seconds=15)).isoformat(),
        received_at=(START+timedelta(seconds=15)).isoformat(),
        observed_at=(START+timedelta(seconds=15)).isoformat(), price=10.4)
    out = evaluate(c=c, rows=[rows[0], middle, rows[-1]], contract=contract)
    assert out["research_v2"]["value"] is False
    assert out["research_v2"]["price_progress"] is False


def test_current_quote_future_anchor_is_not_historical_buy():
    c, rows, contract = fixture()
    c["original_confirmed_at"] = contract["original_confirmed_at"] = (
        START+timedelta(seconds=45)).isoformat()
    out = evaluate(c=c, rows=rows, contract=contract, seconds=45)
    assert out["confirmation_freshness"]["value"] is True
    assert out["research_v2"]["value"] is None


def test_pure_function_calls_no_file_network_or_database(monkeypatch):
    import builtins
    import socket
    from sqlalchemy.engine import Engine
    from sqlalchemy.ext.asyncio import AsyncSession

    def forbidden(*args, **kwargs):
        raise AssertionError("pure research evaluator attempted IO")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Engine, "connect", forbidden)
    monkeypatch.setattr(AsyncSession, "execute", forbidden)
    assert evaluate()["research_v2"]["value"] is True
