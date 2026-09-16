"""Synthetic contract boundaries, explicitly not retained-market replay results."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.paper.intraday_route_research import (
    EARLY_LAUNCH_VERSION, ROUTE_A2, ROUTE_ACCOUNTS,
    build_early_launch_report, early_launch_variant_contract,
)

START = datetime(2026, 9, 10, 9, 30)


def fixture():
    identity = dict(account_id=4, account_name=ROUTE_ACCOUNTS[ROUTE_A2],
                    production_version="frozen-a2-baseline")
    candidate = dict(candidate_id="design-1", code="002912", route_id=ROUTE_A2,
        evidence_ref="synthetic:candidate", frozen_at=START, trade_date="2026-09-10",
        is_trade_day=True, calendar_ref="synthetic:calendar", eligible=True,
        price_basis="raw:20260910", prev_close=10, trigger_price=10.3,
        frozen_account_policy=dict(initial_capital=50000, **identity),
        frozen_position=dict(cash=50000, shares=0, **identity), **identity)
    candidate["launch_variant"] = dict(version=EARLY_LAUNCH_VERSION, frozen_at=START,
        cohort_frozen_at=START, eligibility_observed_at=START,
        code=candidate["code"], trade_date=candidate["trade_date"],
        price_basis=candidate["price_basis"], historical_eligibility_verified=True,
        sample_role="design", evidence_ref="synthetic:variant", cohort_ref="synthetic:cohort",
        eligibility_ref="synthetic:tag", predicate_policy_ref="synthetic:launch-policy")
    rows = []
    for seconds in range(0, 181, 30):
        at = START + timedelta(seconds=seconds)
        rows.append(dict(candidate_id=candidate["candidate_id"], sample_id=str(seconds),
            code=candidate["code"], evidence_ref="synthetic:quote:"+str(seconds),
            source_quote_at=at, received_at=at, observed_at=at, price=10.4,
            prev_close=10, price_basis=candidate["price_basis"],
            setup_valid=False, original_trigger=False, predicate_ref="synthetic:a2-reject",
            candidate_active=True, launch_setup_valid=True,
            launch_predicate_ref="synthetic:launch:"+str(seconds),
            launch_policy_ref="synthetic:launch-policy", launch_version=EARLY_LAUNCH_VERSION,
            book_source_at=at, book_evidence_ref="synthetic:book:"+str(seconds),
            ask1_price=10.41, ask1_volume=100, bid1_price=10.39, bid1_volume=100,
            limit_up=11, limit_evidence_ref="synthetic:limit", **identity))
    return candidate, rows


def run(c, rows, as_of=START+timedelta(minutes=4)):
    return build_early_launch_report([c], rows, as_of=as_of)


def launch(report):
    return report["results"][0]["early_launch"]


def test_opt_in_independent_of_production_gate_and_no_fill_or_mutation():
    c, rows = fixture()
    before = deepcopy((c, rows))
    result = run(c, rows)
    event = launch(result)
    assert event["status"] == "observed"
    assert event["first_event"]["sample_id"] == "60"
    assert event["first_event"]["reference_is_fill"] is False
    assert event["net_profit"] is None
    assert result["results"][0]["costs"]["actual_quantity"] is None
    assert result["results"][0]["comparisons"][0]["routes"]["original"]["first_event"] is None
    assert result["launch_summary"]["same_budget_performance"] is None
    assert result["launch_summary"]["evaluation_split"] == "unverified_not_out_of_time"
    assert (c, rows) == before
    assert run(c, rows) == result


@pytest.mark.parametrize("field,value", [
    ("ask1_volume", None), ("ask1_volume", 0), ("ask1_volume", True),
    ("ask1_price", float("nan")), ("bid1_volume", None),
    ("book_evidence_ref", ""), ("book_source_at", START-timedelta(seconds=1)),
    ("limit_evidence_ref", ""), ("launch_setup_valid", None),
    ("launch_version", "research:unknown"), ("launch_policy_ref", "other"),
    ("launch_predicate_ref", ""), ("candidate_active", None),
    ("bid1_price", 10.5), ("account_id", 99), ("price_basis", "future-adjusted"),
])
def test_unknown_book_predicate_identity_fails_closed(field, value):
    c, rows = fixture()
    rows[1][field] = value
    assert launch(run(c, rows))["status"] == "unknown"


@pytest.mark.parametrize("field,value", [
    ("version", "research:unregistered"), ("historical_eligibility_verified", False),
    ("eligibility_observed_at", START+timedelta(seconds=1)),
    ("cohort_frozen_at", START+timedelta(seconds=1)), ("cohort_ref", ""),
    ("code", "000980"), ("trade_date", "2026-09-11"),
])
def test_no_current_tag_or_posthoc_cohort_backfill(field, value):
    c, rows = fixture()
    c["launch_variant"][field] = value
    assert launch(run(c, rows))["status"] == "unknown"


def test_limit_queue_and_negative_gate_do_not_confirm_and_reset_streak():
    c, rows = fixture()
    for r in rows:
        r.update(price=11, ask1_price=11)
    assert launch(run(c, rows))["first_event"] is None
    c, rows = fixture()
    rows[1]["launch_setup_valid"] = False
    assert launch(run(c, rows))["first_event"]["sample_id"] == "120"


def test_gap_initial_missing_invalidation_duplicate_and_retrograde():
    c, rows = fixture()
    assert launch(run(c, rows[3:]))["reason"] == "launch_coverage_gap_no_reconstruction"
    assert launch(run(c, [rows[0], *rows[3:]]))["status"] == "unknown"
    rows[1]["candidate_active"] = False
    assert launch(run(c, rows))["status"] == "control"
    c, rows = fixture()
    duplicate = dict(rows[1], ask1_volume=200)
    assert launch(run(c, [*rows, duplicate]))["status"] == "unknown"
    c, rows = fixture()
    rows[1]["observed_at"] = START+timedelta(seconds=65)
    rows[1]["received_at"] = rows[1]["observed_at"]
    assert launch(run(c, rows))["status"] == "unknown"


def test_exact_window_end_and_future_observation_never_create_confirmation():
    c, rows = fixture()
    for r in rows:
        for key in ("source_quote_at", "received_at", "observed_at", "book_source_at"):
            r[key] += timedelta(minutes=4)
    c["frozen_at"] += timedelta(minutes=4)
    assert launch(run(c, rows, START+timedelta(minutes=6)))["first_event"] is None
    c, rows = fixture()
    assert launch(run(c, rows, START+timedelta(seconds=59)))["first_event"] is None
    future = dict(rows[-1], account_id=999, observed_at=START+timedelta(minutes=10))
    assert launch(run(c, [*rows, future])) == launch(run(c, rows))


def test_missing_markout_is_unknown_not_zero_and_all_candidates_in_denominator():
    c, rows = fixture()
    result = run(c, rows[:3])
    assert launch(result)["markouts"]["1"]["endpoint_markout_pct"] is None
    assert launch(result)["markouts"]["1"]["status"] == "unknown"
    failure = deepcopy(c)
    failure.update(candidate_id="failure", code="000816")
    failure["launch_variant"].update(code="000816", sample_role="failure_control")
    result = build_early_launch_report([c, failure], rows[:3], as_of=START+timedelta(minutes=4))
    assert result["launch_summary"]["counts"] == dict(observed=1, control=0, unknown=1)
    assert result["launch_summary"]["candidate_count"] == 2


@pytest.mark.parametrize("price,expected", [(10.299, False), (10.3, True)])
def test_three_percent_boundary_is_not_sample_specific(price, expected):
    c, rows = fixture()
    for row in rows:
        row["price"] = price
    assert (launch(run(c, rows))["first_event"] is not None) is expected


def test_received_at_window_end_and_insufficient_persistence():
    c, rows = fixture()
    # Candidate at 09:33, sources ending 09:34, received exactly 09:35.
    c["frozen_at"] += timedelta(minutes=3)
    rows = rows[:3]
    for row in rows:
        for key in ("source_quote_at", "received_at", "observed_at", "book_source_at"):
            row[key] += timedelta(minutes=3)
    rows[-1]["received_at"] = rows[-1]["observed_at"] = START+timedelta(minutes=5)
    assert launch(run(c, rows, START+timedelta(minutes=5)))["first_event"] is None
    c, rows = fixture()
    rows = rows[:3]
    for index, row in enumerate(rows):
        for key in ("source_quote_at", "received_at", "observed_at", "book_source_at"):
            row[key] = START+timedelta(seconds=index*29)
    assert launch(run(c, rows))["first_event"] is None


def test_production_a2_and_c2_snapshots_are_unchanged():
    from app.paper.momentum_retest_shadow import MomentumRetestPolicy
    from app.paper.strategy_iteration_shadow import ROUTE_C, _rules
    a2 = MomentumRetestPolicy.from_settings().snapshot()
    c2 = _rules(ROUTE_C)
    c, rows = fixture()
    run(c, rows)
    assert MomentumRetestPolicy.from_settings().snapshot() == a2
    assert _rules(ROUTE_C) == c2


def test_registry_is_owned_and_empty_or_unregistered_contract_remains_unknown():
    registry = early_launch_variant_contract()
    registry["policy"]["min_samples"] = 1
    assert early_launch_variant_contract()["policy"]["min_samples"] == 3
    c, rows = fixture()
    del c["launch_variant"]
    assert launch(run(c, rows))["status"] == "unknown"
    assert build_early_launch_report([], [], as_of=START)["launch_summary"]["candidate_count"] == 0
