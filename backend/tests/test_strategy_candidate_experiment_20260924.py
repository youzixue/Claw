"""Isolated frozen fixtures: no runtime DB, settings changes, orders or network."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from app.paper.intraday_route_research import (
    STRATEGY_CANDIDATE_ROUTES, STRATEGY_CANDIDATE_ACCOUNTS, RouteResearchPolicy,
    build_strategy_candidate_experiment_report,
    evaluate_strategy_candidate_experiment,
    strategy_candidate_experiment_contract, evaluate_strategy_notification_overlay,
)

START = datetime(2026, 9, 23, 10)
ASOF = START + timedelta(minutes=10)


def candidate(route="B2", **changes):
    return dict(candidate_id="c1", route=route, code="600001", trade_date="2026-09-23",
                account_id=None if route == "C3" else 1, account_name=STRATEGY_CANDIDATE_ACCOUNTS[route],
                frozen_at=START.isoformat(), production_version="frozen-v1", evidence_ref="frozen:c1",
                original_candidate=True, original_confirmed=route in ("A", "C", "C2", "C3", "E", "E2", "F", "F2"),
                original_confirmed_at=START.isoformat() if route in ("A", "C", "C2", "C3", "E", "E2", "F", "F2") else None,
                gate_inputs={"rule_snapshot": {"minimum_change_pct": 0, "maximum_change_pct": 4, "min_vwap_slope_pct": 0}},
                gate_ref="snapshot:gate", **changes)


def row(seconds=0, price=10.3, **changes):
    at = START + timedelta(seconds=seconds)
    result = dict(candidate_id="c1", sample_id=f"s{seconds}", evidence_ref=f"snapshot:{seconds}",
                  account_id=1, account_name="challenger_b",
                  source_quote_at=at.isoformat(), received_at=at.isoformat(), observed_at=at.isoformat(),
                  price=price, prev_close=10.0, avg_price=10.1, ask1_price=price,
                  ask1_volume=100, original_gate=True, original_gate_ref=f"gate:{seconds}",
                  relative_strength_pct=1.0, sector_relative_strength_pct=0.5)
    result.update(changes)
    return result


def evaluate(route="B2", rows=None, c=None, **kwargs):
    c = c or candidate(route)
    rows = rows if rows is not None else [row(), row(30)]
    # Quote fixtures are shared across route tests; bind their owned test accounts.
    rows = [dict(r, account_id=c["account_id"], account_name=c["account_name"]) for r in rows]
    return evaluate_strategy_candidate_experiment(c, rows, as_of=ASOF, **kwargs)


@pytest.mark.parametrize("route", STRATEGY_CANDIDATE_ROUTES)
def test_all_routes_have_executable_pure_entry_and_no_side_effects(route):
    c, rows = candidate(route), [row(), row(30)]
    before = deepcopy((c, rows))
    out = evaluate(c=c, rows=rows)
    assert (c, rows) == before
    assert out["production_permission"] is False
    assert out["orders_created"] == out["pushes_created"] == 0
    assert out["calibrated_probability"] is None
    assert out["early_observation"]["is_buy_point"] is False
    assert out["candidate"]["status"] in {"observed", "control", "unknown"}


def test_b2_short_streak_retains_original_static_window():
    assert evaluate()["candidate"]["value"] is True
    out = evaluate(rows=[row(), row(30, price=10.5, original_gate=False)])
    assert out["candidate"]["value"] is False
    assert out["baseline"]["value"] is False


def test_missing_gate_is_not_negative_or_confirmed():
    out = evaluate(rows=[row(original_gate=None), row(30, original_gate=None)])
    assert out["candidate"]["value"] is None


@pytest.mark.parametrize("field,value", [("ask1_price", 0), ("ask1_volume", 0),
                                         ("ask1_price", None), ("ask1_volume", None)])
def test_offer_required(field, value):
    out = evaluate(rows=[row(**{field: value}), row(30, **{field: value})])
    assert out["candidate"]["value"] is not True


def test_future_row_cannot_complete_streak():
    out = evaluate(rows=[row(), row(601)])
    assert out["candidate"]["value"] is False
    assert out["rejections"]["future_observation_excluded"] == 1


def test_duplicate_source_and_id_cannot_complete_streak():
    rows = [row(), row(sample_id="copy", observed_at=(START+timedelta(seconds=30)).isoformat(),
                       received_at=(START+timedelta(seconds=30)).isoformat())]
    out = evaluate(rows=rows)
    assert out["candidate"]["value"] is False
    assert out["sample_count"] == 1


def test_conflicting_duplicate_blocks_path():
    out = evaluate(rows=[row(), row(price=10.31), row(30)])
    assert out["candidate"]["reason"] == "path_invalid"
    assert out["rejections"]["conflicting_duplicate"] >= 1


@pytest.mark.parametrize("delta", [timedelta(days=1), timedelta(hours=2)])
def test_cross_day_and_lunch_samples_unknown(delta):
    invalid = row()
    clock = START + delta
    invalid.update(source_quote_at=clock.isoformat(), received_at=clock.isoformat(), observed_at=clock.isoformat())
    out = evaluate_strategy_candidate_experiment(candidate(), [invalid], as_of=clock+timedelta(minutes=1))
    assert out["candidate"]["value"] is None


def test_lunch_cannot_bridge_confirmation():
    rows = []
    for index, at in enumerate((datetime(2026, 9, 23, 11, 30), datetime(2026, 9, 23, 13))):
        rows.append(row(index, source_quote_at=at.isoformat(), received_at=at.isoformat(), observed_at=at.isoformat()))
    out = evaluate_strategy_candidate_experiment(candidate(), rows, as_of=datetime(2026, 9, 23, 13, 1))
    assert out["candidate"]["value"] is False


def test_a_recent_dual_cross_not_cumulative_day_low():
    yes = evaluate("A", [row(price=9.9, original_gate=False), row(30)])
    no = evaluate("A", [row(low=9.0), row(30, low=9.0)])
    assert yes["candidate"]["value"] is True
    assert no["candidate"]["value"] is False


def test_a_dual_lines_can_be_crossed_on_successive_real_frames():
    out = evaluate("A", [row(price=9.9), row(30, price=10.05), row(60, price=10.2)])
    assert out["candidate"]["value"] is True


def test_a_gap_cannot_reconstruct_reclaim():
    out = evaluate("A", [row(price=9.9), row(100)])
    assert out["candidate"]["value"] is None


def test_a2_early_watch_is_not_buy_and_preserves_baseline():
    early = datetime(2026, 9, 23, 9, 31)
    c = candidate("A2")
    c["frozen_at"] = early.isoformat()
    r = row(source_quote_at=early.isoformat(), received_at=early.isoformat(), observed_at=early.isoformat())
    out = evaluate(c=c, rows=[r])
    assert out["early_observation"]["value"] is True
    assert out["early_observation"]["is_buy_point"] is False
    assert out["candidate"]["value"] is False


def test_a2_missing_offer_cannot_reconfirm_frozen_original():
    c = candidate("A2")
    c.update(original_confirmed=True, original_confirmed_at=START.isoformat())
    out = evaluate(c=c, rows=[row(ask1_volume=0)])
    assert out["baseline"]["value"] is True
    assert out["candidate"]["value"] is None


def test_b_probability_is_ranking_not_fake_calibration_or_signal():
    c = candidate("B", probability=0.12, pool_identity=True)
    out = evaluate(c=c)
    assert out["early_observation"]["ranking_score"] == 0.12
    assert out["candidate"]["value"] is None
    assert out["calibrated_probability"] is None


@pytest.mark.parametrize("identity,expected", [(True, True), (False, False), (None, None)])
def test_c_original_pool_mainline_preserved(identity, expected):
    c = candidate("C", pool_identity=True, mainline_identity=identity)
    assert evaluate(c=c)["candidate"]["value"] is expected


@pytest.mark.parametrize("route", ["C2", "C3"])
def test_repair_quality_requires_positive_vwap_and_relative_strength(route):
    assert evaluate(route, [row(avg_price=10), row(30, avg_price=10.1)])["candidate"]["value"] is True
    assert evaluate(route, [row(avg_price=10.1), row(30, avg_price=10.1)])["candidate"]["value"] is False
    key = "relative_strength_pct" if route == "C2" else "sector_relative_strength_pct"
    assert evaluate(route, [row(avg_price=10), row(30, **{key: None})])["candidate"]["value"] is None
    assert evaluate(route, [row(avg_price=10), row(30, **{key: -0.1})])["candidate"]["value"] is False


@pytest.mark.parametrize("route,keys", [
    ("D", ["two_distinct_verified_frames", "original_source_quality_passed"]),
    ("D2", ["verified_early", "two_distinct_verified_middle", "verified_final"]),
])
def test_auction_source_contract_never_creates_signal(route, keys):
    c = candidate(route, source_contract={**{k: True for k in keys}, "evidence_ref": "source:verified",
        "observed_at": START.isoformat(), "evidence_at": START.isoformat()})
    out = evaluate(c=c)
    assert out["candidate"]["value"] is None
    assert out["early_observation"]["value"] is True
    c["source_contract"][keys[0]] = None
    assert evaluate(c=c)["early_observation"]["value"] is None


@pytest.mark.parametrize("route", ["E", "E2"])
def test_reseal_or_genuinely_new_cancel_round(route):
    c = candidate(route, highboard_identity=True)
    assert evaluate(c=c, rows=[row(price=10.7, limit_up=11), row(30, price=10.99, limit_up=11)])["candidate"]["value"] is True
    c.update(round_id="r2", previous_round_id="r1", cancelled_at=(START-timedelta(seconds=1)).isoformat())
    assert evaluate(c=c)["candidate"]["value"] is True
    c["round_id"] = "r1"
    assert evaluate(c=c)["candidate"]["value"] is False


@pytest.mark.parametrize("route", ["F", "F2"])
def test_broken_board_identity_and_extension_branches(route):
    c = candidate(route, broken_board_identity=True)
    nonextended = [row(i, price=10.1+i/10000) for i in range(0, 301, 60)]
    assert evaluate(c=c, rows=nonextended)["candidate"]["value"] is True
    extended = [row(i, price=10.1+i/500) for i in range(0, 301, 60)]
    assert evaluate(c=c, rows=extended)["candidate"]["value"] is False
    retest = [row(0, 10.5), row(30, 10.4), row(60, 10.45)]
    assert evaluate(c=c, rows=retest)["candidate"]["value"] is True
    c["broken_board_identity"] = None
    assert evaluate(c=c, rows=retest)["candidate"]["value"] is None


def test_f_missing_five_minute_path_is_not_nonextended():
    assert evaluate(c=candidate("F2", broken_board_identity=True))["candidate"]["value"] is None


def test_future_original_confirmation_not_baseline():
    c = candidate()
    c.update(original_confirmed=True, original_confirmed_at=(ASOF+timedelta(seconds=1)).isoformat())
    assert evaluate(c=c)["baseline"]["value"] is None


def test_hash_and_unknowns_and_input_order_deterministic():
    c = candidate()
    one = build_strategy_candidate_experiment_report([c], [row(), row(30)], as_of=ASOF)
    two = build_strategy_candidate_experiment_report([c], [row(30), row()], as_of=ASOF)
    assert one == two
    duplicate = build_strategy_candidate_experiment_report([c, c], [row(), row(30)], as_of=ASOF)
    assert duplicate["summary"] == {"unknown": 2}
    assert strategy_candidate_experiment_contract()["production_permission"] is False


def test_source_and_ranking_do_not_require_fabricated_intraday_samples():
    c = candidate("D", source_contract={"two_distinct_verified_frames": True,
        "original_source_quality_passed": True, "evidence_ref": "auction-source",
        "observed_at": START.isoformat(), "evidence_at": START.isoformat()})
    assert evaluate(c=c, rows=[])["early_observation"]["value"] is True
    b = candidate("B", pool_identity=True, probability=0.2)
    assert evaluate(c=b, rows=[])["early_observation"]["ranking_score"] == 0.2
    b["original_candidate"] = False
    assert evaluate(c=b, rows=[])["early_observation"]["ranking_score"] is None


def test_malformed_source_contract_and_batch_identity_fail_closed():
    assert evaluate(c=candidate("D", source_contract=["invalid"]), rows=[])["candidate"]["value"] is None
    c = candidate()
    c["candidate_id"] = []
    result = build_strategy_candidate_experiment_report([c], [], as_of=ASOF)
    assert result["results"][0]["candidate"]["value"] is None


def test_above_limit_or_unfillable_reseal_never_confirms():
    c = candidate("E2", highboard_identity=True)
    assert evaluate(c=c, rows=[row(price=10.7, limit_up=11),
        row(30, price=11.1, limit_up=11)])["candidate"]["value"] is False
    assert evaluate(c=c, rows=[row(price=10.7, limit_up=11),
        row(30, price=11, limit_up=11, ask1_volume=0)])["candidate"]["value"] is False


def test_b_ranking_is_within_frozen_date_version_not_calibration():
    c1 = candidate("B", pool_identity=True, probability=0.2)
    c2 = candidate("B", pool_identity=True, probability=0.1)
    c2["candidate_id"] = "c2"
    c3 = candidate("B", pool_identity=True, probability=0.3)
    c3.update(candidate_id="c3", production_version="other-v")
    report = build_strategy_candidate_experiment_report([c1, c2, c3], [], as_of=ASOF)
    assert [r["early_observation"]["supplied_cohort_rank"] for r in report["results"]] == [1, 2, 1]
    assert all(r["candidate"]["value"] is None for r in report["results"])


def test_pre_candidate_price_context_does_not_move_original_candidate_clock():
    c = candidate("A")
    c.update(frozen_at=(START+timedelta(seconds=60)).isoformat(), context_start_at=START.isoformat())
    c["original_confirmed_at"] = c["frozen_at"]
    rows = [row(price=9.9), row(30, price=10.05), row(60, price=10.2)]
    out = evaluate(c=c, rows=rows)
    assert out["candidate"]["value"] is True
    assert out["input_refs"]["frozen_at"] == c["frozen_at"]
    assert out["event_persistence_availability"] == "unknown"
    assert out["historically_executable"] is None
    assert evaluate(c=c, rows=rows[:-1])["candidate"]["reason"] == "context_only_no_candidate_frame"


@pytest.mark.parametrize("route", ["A", "C2", "C3", "F2"])
def test_explicit_feature_history_is_allowed_without_backdating_candidate(route):
    c = candidate(route, broken_board_identity=True)
    c["frozen_at"] = (START+timedelta(seconds=300)).isoformat()
    c["original_confirmed_at"] = c["frozen_at"]
    rows = [row(i, price=9.99+i/1500, avg_price=10.0+i/10000,
                sample_role="feature_history", original_gate=None) for i in range(0, 300, 60)]
    rows.append(row(300, price=10.2, avg_price=10.04))
    out = evaluate(c=c, rows=rows)
    assert out["candidate"]["value"] is True
    assert out["feature_history_count"] == 5
    assert out["input_refs"]["frozen_at"] == c["frozen_at"]
    assert out["candidate"]["first_event"]["source_quote_at"] == c["frozen_at"]


@pytest.mark.parametrize("route", ["A", "F2"])
def test_history_does_not_override_missing_vwap_cross_or_excess_extension(route):
    c = candidate(route, broken_board_identity=True)
    c["frozen_at"] = (START+timedelta(seconds=300)).isoformat()
    c["original_confirmed_at"] = c["frozen_at"]
    rows = [row(i, price=9.9+i/1000, avg_price=9.8+i/10000,
                sample_role="feature_history", original_gate=None) for i in range(0, 300, 60)]
    rows.append(row(300, price=10.2, avg_price=9.9))
    assert evaluate(c=c, rows=rows)["candidate"]["value"] is False


@pytest.mark.parametrize("route", ["B2", "A2"])
def test_feature_history_never_qualifies_original_confirmation_routes(route):
    c = candidate(route)
    c["frozen_at"] = (START+timedelta(seconds=60)).isoformat()
    out = evaluate(c=c, rows=[row(sample_role="feature_history"), row(30, sample_role="feature_history"), row(60)])
    assert out["candidate"]["value"] is None
    assert out["rejections"]["feature_history_route_not_supported"] == 2


def test_history_role_cannot_be_current_event_even_when_after_freeze():
    out = evaluate("A", [row(price=9.9, sample_role="feature_history"),
                         row(30, sample_role="feature_history")])
    assert out["candidate"]["reason"] == "context_only_no_candidate_frame"


def test_b2_pre_candidate_context_never_counts_toward_two_by_thirty():
    c = candidate()
    c.update(frozen_at=(START+timedelta(seconds=60)).isoformat(), context_start_at=START.isoformat())
    policy = RouteResearchPolicy("research:b2_2x30_v1", 2, 30, 75)
    assert evaluate(c=c, rows=[row(), row(30), row(60)], policy=policy)["candidate"]["value"] is False
    assert evaluate(c=c, rows=[row(), row(30), row(60), row(90)], policy=policy)["candidate"]["value"] is True


@pytest.mark.parametrize("context", ["invalid", "2026-09-22T10:00:00", "2026-09-23T10:00:01"])
def test_context_clock_must_be_explicit_same_day_past(context):
    out = evaluate(c=candidate(context_start_at=context))
    assert out["candidate"]["reason"] == "context_clock_unproven"


def test_missing_commit_is_not_blanket_unknown_or_historical_execution_claim():
    rows = [row(), row(30)]
    assert evaluate(rows=rows)["candidate"]["value"] is True
    rows[0]["committed_at"] = "2026-09-24T10:00:00"
    out = evaluate(rows=rows)
    assert out["candidate"]["value"] is None
    assert out["rejections"]["quote_storage_clock_not_available"] == 1
    assert out["event_persistence_availability"] == "unknown"


def notification(route="A"):
    return {"sample_id": "signal1", "route": route, "code": "600001",
        "trade_date": "2026-09-23", "production_version": "frozen-v1", "evidence_ref": "frozen:signal1",
        "baseline_confirmed": True, "notification_observed_at": "2026-09-23T10:00:00+08:00",
        "original_confirmed_at": "2026-09-23T10:00:00+08:00",
        "original_features": {"anchor_known": True, "current_quote": {
            "source_quote_at": "2026-09-23T10:00:00", "received_at": "2026-09-23T10:00:00",
            "updated_at": "2026-09-23T10:00:00"}, "segment_start": "2026-09-23T09:50:00+08:00",
            "recent_reclaim_le300sec": True, "last_cross_age_sec": 20, "current_dual_reclaimed": True,
            "segment_frames": 20, "vwap_segment_change_pct": 0.1},
        "frozen_confirmation": {"first_sample_at": "2026-09-23T09:59:00",
            "last_sample_at": "2026-09-23T10:00:00", "vwap_slope_pct": 0.1}}


@pytest.mark.parametrize("route", ["A", "A2", "C2", "C3"])
def test_notification_direct_overlay_runs_without_inventing_offer_or_commit(route):
    out = evaluate_strategy_notification_overlay(notification(route), as_of=ASOF)
    assert out["feature_overlay"]["value"] is True
    assert out["candidate"]["value"] is None
    assert out["baseline"]["value"] is True
    assert out["is_buy_point"] is False
    assert out["event_persistence_availability"] == "unknown"


def test_notification_overlay_never_borrows_notification_anchor_or_outcome():
    r = notification()
    before = evaluate_strategy_notification_overlay(r, as_of=ASOF)
    r["notification_features"] = {"recent_reclaim_le300sec": False}
    r["outcome_only"] = {"close_markout_pct": -99}
    assert evaluate_strategy_notification_overlay(r, as_of=ASOF) == before
    r["original_confirmed_at"] = None
    assert evaluate_strategy_notification_overlay(r, as_of=ASOF)["feature_overlay"]["value"] is None


def test_notification_overlay_future_and_left_censored_unknown():
    r = notification()
    r["original_features"]["current_quote"]["updated_at"] = "2026-09-23T10:00:01"
    assert evaluate_strategy_notification_overlay(r, as_of=ASOF)["feature_overlay"]["value"] is None
    r = notification()
    r["original_features"].update(recent_reclaim_le300sec=False, last_cross_age_sec=None,
        segment_start="2026-09-23T09:59:00", segment_left_censored=True)
    assert evaluate_strategy_notification_overlay(r, as_of=ASOF)["feature_overlay"]["value"] is None


def test_notification_negative_slope_retained_as_component_not_full_gate():
    r = notification("C3")
    r["original_features"]["vwap_segment_change_pct"] = -0.1
    out = evaluate_strategy_notification_overlay(r, as_of=ASOF)
    assert out["feature_overlay"]["value"] is False
    assert out["candidate"]["value"] is None


@pytest.mark.parametrize("samples,seconds,label", [
    (3, 60, "baseline_3x60"), (2, 60, "count_only_2x60"),
    (3, 30, "duration_only_3x30"), (2, 30, "joint_2x30")])
def test_b2_factorial_labels_and_default(samples, seconds, label):
    result = evaluate(policy=RouteResearchPolicy("research:factorial", samples, seconds, 75))
    assert result["comparison_label"] == label
    assert evaluate()["policy"]["min_persistence_sec"] == 30
    assert evaluate()["comparison_label"] == "joint_2x30"
    assert evaluate(rows=[row(), row(20)])["candidate"]["value"] is False
    assert evaluate("C2")["comparison_label"] == "route_specific_overlay_not_short_persistence"


@pytest.mark.parametrize("route", ["D", "D2"])
@pytest.mark.parametrize("field,value", [
    ("observed_at", None), ("evidence_at", None),
    ("observed_at", "2026-09-23T10:00:01"), ("evidence_at", "2026-09-23T10:00:01"),
    ("evidence_at", "2026-09-22T10:00:00"),
    ("observed_at", "2026-09-23T09:59:59")])
def test_source_contract_requires_real_same_day_nonfuture_ordered_clocks(route, field, value):
    source = dict(two_distinct_verified_frames=True, original_source_quality_passed=True,
        verified_early=True, two_distinct_verified_middle=True, verified_final=True,
        evidence_ref="auction:source", evidence_at=START.isoformat(), observed_at=START.isoformat())
    source[field] = value
    out = evaluate(c=candidate(route, source_contract=source), rows=[])
    assert out["early_observation"]["value"] is None
    assert out["early_observation"]["source_contract_clock_valid"] is False
    assert out["candidate"]["value"] is None


def test_a2_ineligible_cannot_pass_even_with_historical_baseline_true():
    c = candidate("A2")
    c.update(original_candidate=False, original_confirmed=True, original_confirmed_at=START.isoformat())
    out = evaluate(c=c)
    assert out["baseline"]["value"] is True
    assert out["candidate"]["value"] is False


def test_candidate_observation_allows_earlier_fresh_source_in_same_frame():
    c = candidate()
    c["frozen_at"] = (START+timedelta(seconds=4)).isoformat()
    rows = [row(i, received_at=(START+timedelta(seconds=i+3)).isoformat(),
                    observed_at=(START+timedelta(seconds=i+4)).isoformat()) for i in (0, 30)]
    out = evaluate(c=c, rows=rows)
    assert out["candidate"]["value"] is True
    assert out["sample_count"] == 2
    assert out["candidate"]["first_event"]["observed_at"] >= c["frozen_at"]
    assert rows[0]["source_quote_at"] < c["frozen_at"]


def test_source_before_freeze_is_not_permission_to_use_stale_quotes():
    c = candidate()
    c["frozen_at"] = (START+timedelta(seconds=181)).isoformat()
    out = evaluate(c=c, rows=[row(received_at=c["frozen_at"], observed_at=c["frozen_at"])])
    assert out["candidate"]["value"] is None
    assert out["rejections"]["sample_clock_invalid"] == 1


def test_feature_history_observation_before_freeze_cannot_be_event():
    c = candidate("A")
    c["frozen_at"] = (START+timedelta(seconds=31)).isoformat()
    out = evaluate(c=c, rows=[row(price=9.9, sample_role="feature_history"),
                             row(30, sample_role="feature_history")])
    assert out["candidate"]["reason"] == "context_only_no_candidate_frame"


def test_b2_requires_both_source_and_observation_persistence():
    rows = [row(observed_at=(START+timedelta(seconds=30)).isoformat()),
            row(30, observed_at=(START+timedelta(seconds=30)).isoformat())]
    assert evaluate(rows=rows)["candidate"]["value"] is False
    rows = [row(), row(30, observed_at=(START+timedelta(seconds=100)).isoformat())]
    assert evaluate(rows=rows)["candidate"]["value"] is False


def test_b2_preserves_frozen_vwap_slope_and_missing_rule_unknown():
    assert evaluate(rows=[row(avg_price=10.2), row(30, avg_price=10.1)])["candidate"]["value"] is False
    c = candidate()
    del c["gate_inputs"]["rule_snapshot"]["min_vwap_slope_pct"]
    assert evaluate(c=c)["candidate"]["value"] is None


def test_duplicate_earliest_lawful_observation_is_order_independent():
    early = row()
    late = row(sample_id="repeat", received_at=(START+timedelta(seconds=59)).isoformat(),
               observed_at=(START+timedelta(seconds=60)).isoformat())
    nxt = row(30)
    assert evaluate(rows=[early, late, nxt]) == evaluate(rows=[late, nxt, early])
    assert evaluate(rows=[late, nxt, early])["candidate"]["value"] is True


@pytest.mark.parametrize("field,value", [("account_id", 9), ("account_name", "default"),
                                        ("account_id", True), ("account_name", None)])
def test_explicit_cross_account_frame_rejected(field, value):
    c = candidate()
    rows = [row(), row(30)]
    rows[1][field] = value
    out = evaluate_strategy_candidate_experiment(c, rows, as_of=ASOF)
    assert out["candidate"]["value"] is None
    assert out["rejections"]["sample_account_identity_conflict"] == 1


def test_candidate_account_route_and_c3_no_account_contract():
    c = candidate()
    c["account_name"] = "default"
    assert evaluate_strategy_candidate_experiment(c, [row()], as_of=ASOF)["candidate"]["value"] is None
    c = candidate("C3")
    c.update(account_id=1, account_name="challenger_c")
    assert evaluate_strategy_candidate_experiment(c, [], as_of=ASOF)["candidate"]["reason"] == "candidate_account_route_unproven"
    c = candidate()
    del c["account_id"]
    assert evaluate_strategy_candidate_experiment(c, [], as_of=ASOF)["candidate"]["reason"] == "candidate_account_route_unproven"


def test_a2_original_confirmation_must_be_visible_by_evaluated_frame():
    c = candidate("A2")
    c.update(original_confirmed=True, original_confirmed_at=(START+timedelta(minutes=5)).isoformat())
    out = evaluate(c=c, rows=[row()])
    assert out["baseline"]["value"] is True
    assert out["candidate"]["value"] is None
    assert out["candidate"]["reason"] == "original_confirmation_after_evaluated_at"


def test_overlay_explicit_future_commit_is_not_unknown_commit():
    r = notification()
    r["original_features"]["current_quote"]["committed_at"] = "2026-09-23T10:00:01"
    out = evaluate_strategy_notification_overlay(r, as_of=ASOF)
    assert out["feature_overlay"]["value"] is None
    assert out["feature_overlay"]["reason"] == "original_quote_commit_not_available_at_anchor"


@pytest.mark.parametrize("field", ["source_quote_at", "received_at", "updated_at", "committed_at"])
def test_overlay_pre5_reference_four_clocks_must_be_available_by_cutoff(field):
    r = notification("F2")
    reference = {k: "2026-09-23T09:55:00" for k in
                 ("source_quote_at", "received_at", "updated_at", "committed_at")}
    r["original_features"].update(pre5_pct=1.0, pre5_reference_quote=reference)
    assert evaluate_strategy_notification_overlay(r, as_of=ASOF)["feature_overlay"]["value"] is True
    reference[field] = "2026-09-23T09:55:01"
    assert evaluate_strategy_notification_overlay(r, as_of=ASOF)["feature_overlay"]["value"] is None
    reference[field] = None
    assert evaluate_strategy_notification_overlay(r, as_of=ASOF)["feature_overlay"]["value"] is None


@pytest.mark.parametrize("route", ["A", "C", "C2", "C3", "E", "E2", "F", "F2"])
@pytest.mark.parametrize("confirmed", [False, None, True])
def test_quality_overlay_cannot_remove_original_formal_confirmation(route, confirmed):
    c = candidate(route, pool_identity=True, mainline_identity=True,
                  highboard_identity=True, broken_board_identity=True)
    c["original_confirmed"] = confirmed
    # True is a future confirmation relative to evaluated_at, despite visible as_of.
    c["original_confirmed_at"] = (START+timedelta(minutes=5)).isoformat() if confirmed else None
    out = evaluate(c=c)
    assert out["candidate"]["value"] is (False if confirmed is False else None)
    assert out["candidate"]["reason"] == (
        "original_confirmation_after_evaluated_at" if confirmed
        else "original_formal_confirmation_required_for_quality_overlay")


@pytest.mark.parametrize("field,value", [
    ("predicate_observed_at", "2026-09-23T10:10:01"),
    ("predicate_observed_at", "2026-09-23T09:59:59"),
    ("predicate_observed_at", "bad"),
    ("predicate_evidence_ref", None),
    ("predicate_evidence_ref", "unrelated"),
    ("sample_role", "feature_history"),
])
def test_explicit_predicate_clock_cannot_borrow_unavailable_evidence(field, value):
    c = candidate("C2")
    r = row(30, avg_price=10.2, predicate_observed_at="2026-09-23T10:00:35",
            predicate_evidence_ref="gate:30")
    r[field] = value
    out = evaluate(c=c, rows=[row(avg_price=10), r])
    assert out["candidate"]["value"] is None
    assert out["rejections"]["predicate_clock_or_reference_unproven"] == 1


def test_predicate_evaluation_does_not_restamp_quote_or_backdate_confirmation():
    c = candidate("C2")
    c.update(frozen_at="2026-09-23T10:00:35", original_confirmed_at="2026-09-23T10:00:35",
             context_start_at=START.isoformat())
    rows = [row(avg_price=10, sample_role="feature_history", original_gate=None),
            row(30, avg_price=10.2, predicate_observed_at="2026-09-23T10:00:35",
                predicate_evidence_ref="gate:30")]
    out = evaluate(c=c, rows=rows)
    assert out["candidate"]["value"] is True
    assert out["evaluated_at"] == c["frozen_at"]
    assert out["candidate"]["first_event"]["observed_at"] == rows[-1]["observed_at"]
    assert out["candidate"]["first_event"]["predicate_observed_at"] == c["frozen_at"]


def test_predicate_clock_cannot_add_b2_persistence():
    rows = [row(), row(20, predicate_observed_at="2026-09-23T10:00:35", predicate_evidence_ref="gate:20")]
    assert evaluate(rows=rows)["candidate"]["value"] is False


@pytest.mark.parametrize("field,value", [
    ("updated_at", "2026-09-23T10:00:31"), ("committed_at", "2026-09-23T10:00:31"),
    ("updated_at", "bad"), ("committed_at", None),
])
def test_explicit_quote_storage_clock_must_be_available(field, value):
    out = evaluate(rows=[row(), row(30, **{field: value})])
    assert out["candidate"]["value"] is None
    assert out["rejections"]["quote_storage_clock_not_available"] == 1


def test_explicit_context_lower_bound_is_not_bypassed_by_history_role():
    c = candidate("A")
    c.update(frozen_at="2026-09-23T10:00:30", original_confirmed_at="2026-09-23T10:00:30",
             context_start_at="2026-09-23T10:00:10")
    out = evaluate(c=c, rows=[row(price=9.9, sample_role="feature_history"), row(30)])
    assert out["candidate"]["value"] is None
    assert out["rejections"]["sample_clock_invalid"] == 1


def test_custom_frozen_policy_and_settings_independence(monkeypatch):
    from app.config.settings import settings
    before = evaluate()
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES", 99)
    assert evaluate() == before
    out = evaluate(policy=RouteResearchPolicy("research:control", 3, 60, 75))
    assert out["candidate"]["value"] is False
