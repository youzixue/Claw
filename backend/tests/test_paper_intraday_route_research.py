"""Pure fixtures; global conftest uses isolated DB, no production queries or IO."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from app.config.settings import settings
from app.paper.account_policy import account_parameter_snapshot
from app.paper.strategy_iteration_shadow import ROUTE_B, ROUTE_F2, route_version_for, _confirmation_streak_status
from app.paper.intraday_route_research import (
    ROUTE_E2, ROUTE_ACCOUNTS, RouteResearchPolicy, build_intraday_route_report, freeze_current_route_context,
)

START = datetime(2026, 9, 9, 10)
ASOF = START + timedelta(minutes=15)
POLICY = RouteResearchPolicy("research:streak-v1", 3, 60, 45)


def candidate(**kwargs):
    identity = {"account_id": kwargs.get("account_id", 1),
                "account_name": kwargs.get("account_name", ROUTE_ACCOUNTS[kwargs.get("route_id", ROUTE_B)]),
                "production_version": kwargs.get("production_version", "frozen-old-v1")}
    return {"candidate_id": "c1", "code": "600001", "route_id": ROUTE_B,
        "production_version": "frozen-old-v1", "evidence_ref": "candidate-ledger:1",
        "frozen_at": START.isoformat(), "trade_date": "2026-09-09",
        "is_trade_day": True, "calendar_ref": "calendar:20260909",
        "eligible": True, "price_basis": "raw:20260909", "prev_close": 10,
        "trigger_price": 10.3, "frozen_account_policy": {"min_samples": 3, **identity},
        "frozen_position": {"shares": 100, "nav": 10000, "version": "owned-old-position", **identity},
        **identity,
        **kwargs}


def sample(seconds=0, price=10.4, **kwargs):
    at = START+timedelta(seconds=seconds)
    return {"candidate_id": "c1", "sample_id": f"s{seconds}", "code": "600001",
        "account_id": 1, "account_name": "challenger_b",
        "production_version": "frozen-old-v1", "evidence_ref": f"archive:{seconds}",
        "source_quote_at": at.isoformat(), "received_at": at.isoformat(),
        "observed_at": at.isoformat(), "price": price, "prev_close": 10,
        "price_basis": "raw:20260909", "setup_valid": True, "candidate_active": True,
        "predicate_ref": f"frozen-gate:{seconds}", "original_trigger": seconds == 0,
        "limit_up": 11, "limit_evidence_ref": "limit:20260909", **kwargs}


def run(rows, c=None, policy=POLICY, as_of=ASOF):
    return build_intraday_route_report([c or candidate()], rows, as_of=as_of, policies=[policy])


def routes(result):
    return result["results"][0]["comparisons"][0]["routes"]


def test_persistent_setup_not_recross_and_original_trigger_kept():
    value = routes(run([sample(i) for i in (0, 30, 60, 90)]))
    assert value["original"]["first_event"]["sample_id"] == "s0"
    assert value["sustained"]["first_event"]["sample_id"] == "s60"
    assert value["recross"]["status"] == "unknown"
    assert len(value["sustained"]["events"]) == 1


def test_recross_requires_real_above_below_above_not_first_pass():
    first = routes(run([sample(0, 10.2), sample(30, 10.4)]))
    assert first["recross"]["status"] == "unknown"
    second = routes(run([sample(0), sample(30, 10.2, setup_valid=False), sample(60)]))
    assert second["recross"]["first_event"]["sample_id"] == "s60"
    assert second["sustained"]["status"] == "unknown"
    assert second["original"]["first_event"]["sample_id"] == "s0"


@pytest.mark.parametrize("route", [ROUTE_B, ROUTE_F2])
def test_frozen_rules_and_positions_and_old_version_never_mutated(route, monkeypatch):
    c = candidate(route_id=route)
    rows = [sample(i, account_name=ROUTE_ACCOUNTS[route]) for i in (0, 30, 60)]
    original = deepcopy((c, rows))
    snapshot = account_parameter_snapshot("challenger_b")
    version = route_version_for(ROUTE_B)
    before = run(rows, c)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES", 99)
    after = run(rows, c)
    assert before == after
    assert (c, rows) == original
    # restore before verifying the original version remains intact
    monkeypatch.undo()
    assert account_parameter_snapshot("challenger_b") == snapshot
    assert route_version_for(ROUTE_B) == version


def test_withdrawal_resets_confirmation_but_does_not_erase_original():
    value = routes(run([sample(0), sample(30, setup_valid=False),
                        sample(60), sample(90), sample(120)]))
    assert value["sustained"]["first_event"]["sample_id"] == "s120"
    assert value["original"]["status"] == "true"


def test_historical_confirmation_and_current_withdrawal_are_separate():
    rows = [sample(0), sample(30), sample(60), sample(90, setup_valid=False)]
    value = routes(run(rows, as_of=START+timedelta(seconds=90)))
    assert value["sustained"]["status"] == "true"
    assert value["sustained"]["current_status"] == "false"
    assert value["sustained"]["first_event"]["sample_id"] == "s60"
    assert routes(run(rows))["sustained"]["current_status"] == "unknown"  # stale by as_of
    rows[-1]["candidate_active"] = False
    rows.append(sample(120))
    value = routes(run(rows, as_of=START+timedelta(seconds=120)))
    assert value["sustained"]["current_status"] == "false"
    assert value["sustained"]["current_reason"] == "candidate_invalidated_no_revival"


def test_invalidation_is_terminal_no_candidate_resurrection():
    value = routes(run([sample(0), sample(30, candidate_active=False),
                        sample(60), sample(90), sample(120)]))
    assert value["sustained"]["status"] == "unknown"
    assert value["recross"]["status"] == "unknown"


def test_unknown_frame_and_gap_break_confirmation():
    for rows in ([sample(0), sample(30, setup_valid=None), sample(60)],
                 [sample(0), sample(30), sample(100)]):
        assert routes(run(rows))["sustained"]["status"] == "unknown"


def test_input_order_and_exact_duplicates_do_not_add_confirmation_frames():
    rows = [sample(0), sample(30), sample(60)]
    a, b = run(rows), run(list(reversed(rows)))
    assert a == b
    value = run([sample(0), sample(0), sample(30)])
    assert routes(value)["sustained"]["status"] == "unknown"
    assert value["results"][0]["comparisons"][0]["sample_count"] == 2


@pytest.mark.parametrize("fault", ["source", "receive", "missing_clock", "wrong_day", "wrong_code",
    "version", "basis", "prev_close", "bool_price", "string_price", "nan_price", "conflict", "arrival"])
def test_bad_evidence_unknown_not_false(fault):
    row = sample(30)
    if fault == "source":
        row["source_quote_at"] = (ASOF+timedelta(seconds=1)).isoformat()
    elif fault == "receive":
        row["received_at"] = (START-timedelta(seconds=1)).isoformat()
    elif fault == "missing_clock":
        row.pop("source_quote_at")
    elif fault == "wrong_day":
        row["source_quote_at"] = "2026-09-08T10:00:00"
    elif fault == "wrong_code":
        row["code"] = "600002"
    elif fault == "version":
        row["production_version"] = "new-version"
    elif fault == "basis":
        row["price_basis"] = "adjusted"
    elif fault == "prev_close":
        row["prev_close"] = 9.99
    elif fault in {"bool_price", "string_price", "nan_price"}:
        row["price"] = {"bool_price": True, "string_price": "10.4", "nan_price": float("nan")}[fault]
    elif fault == "conflict":
        row["source_quote_at"] = START.isoformat()
        row["price"] = 10.5
    elif fault == "arrival":
        row["observed_at"] = (START+timedelta(seconds=100)).isoformat()
    value = run([sample(0), row, sample(60)])
    assert routes(value)["sustained"]["status"] == "unknown"
    assert routes(value)["recross"]["status"] == "unknown"
    assert routes(value)["original"]["status"] == "true"
    json.dumps(value, allow_nan=False)


def test_future_observation_not_available_and_right_censored_markout():
    value = routes(run([sample(0), sample(30), sample(60)], as_of=START+timedelta(seconds=30)))
    assert value["sustained"]["status"] == "unknown"
    assert value["original"]["markouts"]["1"]["status"] == "unknown"
    assert value["original"]["markouts"]["1"]["reason"] == "right_censored"


def test_markout_horizons_use_sample_prices_not_daily_high_and_no_fill():
    rows = [sample(i, 10 + i/10000, original_trigger=i == 0, high=999, low=.1)
            for i in range(0, 601, 30)]
    c = candidate(trigger_price=10)
    markouts = routes(run(rows, c))["original"]["markouts"]
    for minute in (1, 3, 5, 10):
        r = markouts[str(minute)]
        assert r["status"] == "observed"
        assert r["endpoint_markout_pct"] == pytest.approx(minute*.06)
        assert r["sampled_mfe_pct"] == pytest.approx(minute*.06)
        assert r["sampled_mae_pct"] == 0
        assert r["executable_return"] is r["net_profit"] is None


def test_markout_negative_mae_and_missing_endpoint_not_interpolated():
    value = routes(run([sample(0, 10), sample(30, 9.9)],
                       candidate(trigger_price=10), policy=replace(POLICY, endpoint_tolerance_sec=0)))
    r = value["original"]["markouts"]["1"]
    assert r["sampled_mae_pct"] == -1
    assert r["endpoint_markout_pct"] is None
    assert r["reason"] == "endpoint_missing"


def test_quote_coverage_gap_and_no_after_target_cherry_pick():
    value = routes(run([sample(0), sample(61, 99)]))
    assert value["original"]["markouts"]["1"]["sample_count"] == 0
    assert value["original"]["markouts"]["1"]["endpoint_markout_pct"] is None
    value = routes(run([sample(0), sample(60)]))
    assert value["original"]["markouts"]["1"]["reason"] == "coverage_gap"


def test_lunch_is_trading_minute_pause_but_breaks_confirmation():
    start = datetime(2026, 9, 9, 11, 29, 30)
    c = candidate(frozen_at=start.isoformat())
    times = [start, start+timedelta(seconds=30), datetime(2026, 9, 9, 13),
             datetime(2026, 9, 9, 13, 0, 30)]
    rows = []
    for i, at in enumerate(times):
        rows.append(sample(i, source_quote_at=at.isoformat(), received_at=at.isoformat(),
                           observed_at=at.isoformat(), original_trigger=i == 0))
    value = routes(run(rows, c, as_of=datetime(2026, 9, 9, 13, 1)))
    assert value["sustained"]["status"] == "unknown"
    r = value["original"]["markouts"]["1"]
    assert r["target_at"] == "2026-09-09T13:00:30"
    assert r["status"] == "observed"


def test_e2_reseal_path_fees_and_queue_not_automatic_fill():
    c = candidate(route_id=ROUTE_E2, trigger_price=10.8, limit_up=11, limit_evidence_ref="limit:20260909",
        hypothetical_order={"policy_version": "research:cost-v1", "frozen_at": START.isoformat(),
            "quantity": 100, "limit_price": 11, "commission_rate": .0003,
            "min_commission": 5, "buy_tax_rate": 0})
    value = run([sample(i, p, account_name="challenger_e") for i, p in ((0, 11), (30, 10.8), (60, 11))], c)
    assert routes(value)["reseal"]["first_event"]["sample_id"] == "s60"
    costs = value["results"][0]["costs"]
    assert costs["actual_fees"] is costs["queue_fill_probability"] is costs["executable_return"] is None
    assert costs["hypothetical_order_cost"]["fee_if_filled"] == 5
    assert costs["hypothetical_order_cost"]["charged_fee"] is None
    assert not costs["hypothetical_order_cost"]["fill_assumed"]


@pytest.mark.parametrize("fault", ["no_open", "gap", "limit_changed", "limit_missing", "unknown_setup"])
def test_e2_no_daily_low_or_unproven_reseal(fault):
    c = candidate(route_id=ROUTE_E2, limit_up=11, limit_evidence_ref="limit:20260909")
    rows = [sample(0, 10.8), sample(30, 11, low=9)]
    if fault == "no_open":
        rows[0]["price"] = 11
    elif fault == "gap":
        rows[1] = sample(100, 11)
    elif fault == "limit_changed":
        rows[0]["limit_up"] = 10.9
    elif fault == "limit_missing":
        rows[0]["limit_evidence_ref"] = None
    else:
        rows[1]["setup_valid"] = None
    for row in rows:
        row["account_name"] = "challenger_e"
    assert routes(run(rows, c))["reseal"]["status"] == "unknown"


def test_actual_supplied_fill_costs_not_duplicated_or_replaced_by_hypothetical():
    fill = {"fill_id": "f1", "order_id": "o1", "candidate_id": "c1", "code": "600001",
        "account_id": 1, "account_name": "challenger_b",
        "production_version": "frozen-old-v1", "side": "buy", "quantity": 100, "price": 10.5,
        "commission": 5, "tax": 0, "filled_at": START.isoformat(),
        "observed_at": START.isoformat(), "evidence_ref": "fill-ledger:f1"}
    c = candidate(actual_fills=[fill])
    value = run([sample(0)], c)
    costs = value["results"][0]["costs"]
    assert costs["actual_fees"] == 5
    assert costs["actual_quantity"] == 100
    assert costs["actual_status"] == "supplied_fill_evidence_not_independently_reconciled"
    assert costs["net_profit"] is None
    duplicate = run([sample(0)], candidate(actual_fills=[fill, fill]))
    assert duplicate["results"][0]["costs"]["actual_fees"] is None
    future = dict(fill, observed_at=(ASOF+timedelta(seconds=1)).isoformat())
    assert run([sample(0)], candidate(actual_fills=[future]))["results"][0]["costs"]["actual_fees"] is None


def test_empty_invalid_candidate_and_duplicate_identity_are_reported():
    assert build_intraday_route_report([], [], as_of=ASOF, policies=[POLICY])["results"] == []
    for c in [None, {}, candidate(eligible=False), candidate(calendar_ref=None)]:
        result = build_intraday_route_report([c], [], as_of=ASOF, policies=[POLICY])
        assert result["results"][0]["status"] == "unknown"
    value = build_intraday_route_report([candidate(), candidate()], [], as_of=ASOF, policies=[POLICY])
    assert all(r["reason"] == "duplicate_candidate_identity" for r in value["results"])
    assert routes(run([]))["original"]["status"] == "unknown"


@pytest.mark.parametrize("args", [
    {"version": "production:v1"}, {"min_samples": True}, {"min_samples": 0},
    {"min_persistence_sec": -1}, {"clock_jitter_sec": float("nan")},
    {"endpoint_tolerance_sec": True}])
def test_invalid_policy_rejected(args):
    with pytest.raises(ValueError):
        replace(POLICY, **args)


def test_policy_changes_stay_independent_and_hash_changes():
    fast = replace(POLICY, version="research:fast", min_samples=2, min_persistence_sec=30)
    result = build_intraday_route_report([candidate()], [sample(0), sample(30)],
        as_of=ASOF, policies=[POLICY, fast])
    a, b = result["results"][0]["comparisons"]
    assert a["routes"]["sustained"]["status"] == "unknown"
    assert b["routes"]["sustained"]["status"] == "true"
    assert a["routes"]["original"] == b["routes"]["original"]
    assert a["policy"]["policy_hash"] != b["policy"]["policy_hash"]
    assert result["data_hash"] != run([sample(0), sample(30)])["data_hash"]


def test_original_false_unknown_observations_retained_and_bad_identity_no_exception():
    value = run([sample(0, original_trigger=False), sample(30, original_trigger=None)])
    assert [r["status"] for r in routes(value)["original"]["observations"]] == ["false", "unknown"]
    bad = build_intraday_route_report([candidate(candidate_id=[])], [], as_of=ASOF, policies=[POLICY])
    assert bad["results"][0]["status"] == "unknown"
    bad_row = run([sample(0), sample(30, sample_id=[])])
    assert routes(bad_row)["sustained"]["status"] == "unknown"
    assert routes(run([sample(0, predicate_ref={})]))["original"]["status"] == "unknown"


def test_multiple_candidates_do_not_mix_paths_and_orphans_counted():
    candidates = [candidate(), candidate(candidate_id="c2", code="600002")]
    rows = [sample(0), sample(30), sample(60),
            sample(0, candidate_id="c2", code="600002"),
            sample(30, candidate_id="unmatched")]
    result = build_intraday_route_report(candidates, rows+[None], as_of=ASOF, policies=[POLICY])
    assert result["orphan_sample_count"] == result["unassigned_sample_count"] == 1
    assert result["results"][0]["comparisons"][0]["routes"]["sustained"]["status"] == "true"
    assert result["results"][1]["comparisons"][0]["routes"]["sustained"]["status"] == "unknown"


def test_source_before_freeze_and_stale_source_and_wrong_calendar_unknown():
    for row in [sample(-1), sample(0, observed_at=(START+timedelta(seconds=181)).isoformat())]:
        result = run([row])
        assert result["results"][0]["comparisons"][0]["status"] == "unknown"
    assert run([sample(0)], candidate(is_trade_day=None))["results"][0]["status"] == "unknown"


def test_observation_latency_not_counted_as_available_markout_path():
    rows = [sample(0, observed_at=(START+timedelta(seconds=30)).isoformat()),
            sample(30, price=99), sample(60), sample(90)]
    value = routes(run(rows))
    r = value["original"]["markouts"]["1"]
    assert r["target_at"] == (START+timedelta(seconds=90)).isoformat()
    assert r["sample_count"] == 2  # source at observation instant is not after availability
    assert r["sampled_mfe_pct"] == 0


def test_close_horizon_right_censored_no_next_day_quotes():
    at = datetime(2026, 9, 9, 14, 59, 30)
    c = candidate(frozen_at=at.isoformat())
    row = sample(0, source_quote_at=at.isoformat(), received_at=at.isoformat(), observed_at=at.isoformat())
    value = routes(run([row], c, as_of=datetime(2026, 9, 9, 15, 15)))
    assert all(m["reason"] == "right_censored" for m in value["original"]["markouts"].values())


@pytest.mark.parametrize("fault", ["account_missing", "account_bool", "wrong_route_account",
    "policy_instance", "policy_version", "position_instance", "sample_instance", "sample_account"])
def test_account_instance_route_and_version_bindings(fault):
    c, rows = candidate(), [sample(0), sample(30), sample(60)]
    if fault == "account_missing":
        c.pop("account_id")
    elif fault == "account_bool":
        c["account_id"] = True
    elif fault == "wrong_route_account":
        c["account_name"] = "challenger_f2"
    elif fault == "policy_instance":
        c["frozen_account_policy"]["account_id"] = 2
    elif fault == "policy_version":
        c["frozen_account_policy"]["production_version"] = "other"
    elif fault == "position_instance":
        c["frozen_position"]["account_id"] = 2
    elif fault == "sample_instance":
        rows[1]["account_id"] = 2  # same name, different historical instance
    else:
        rows[1]["account_name"] = "challenger_f2"
    result = run(rows, c)
    assert len(result["results"]) == 1
    if fault.startswith("sample"):
        assert routes(result)["sustained"]["status"] == "unknown"
        assert result["results"][0]["comparisons"][0]["rejections"]["sample_identity_unproven"] == 1
    else:
        assert result["results"][0]["status"] == "unknown"


def test_visible_fill_prefix_causality_and_bad_visible_unknown():
    base = {"fill_id": "f1", "order_id": "o1", "candidate_id": "c1", "code": "600001",
        "account_id": 1, "account_name": "challenger_b", "production_version": "frozen-old-v1",
        "side": "buy", "quantity": 100, "price": 10.5, "commission": 5, "tax": 0,
        "filled_at": START.isoformat(), "observed_at": START.isoformat(), "evidence_ref": "fill:f1"}
    first = run([sample(0)], candidate(actual_fills=[base]))["results"][0]["costs"]
    future = dict(base, account_id=999, commission=float("nan"),
                  observed_at=(ASOF+timedelta(seconds=1)).isoformat())
    for fills in ([base, future], [future, base]):
        result = run([sample(0)], candidate(actual_fills=fills))["results"][0]["costs"]
        assert result["future_fill_count"] == 1
        assert result["visible_fill_count"] == result["valid_visible_fill_count"] == 1
        assert result["actual_scope"] == "visible_supplied_prefix_not_complete_order_or_cycle"
        assert {k: v for k, v in result.items() if k != "future_fill_count"} == {
            k: v for k, v in first.items() if k != "future_fill_count"}
    for changed in (dict(base, fill_id="f2", account_id=2),
                    dict(base, fill_id="f2", evidence_ref=[]),
                    dict(base, fill_id="f2", commission=float("nan"))):
        result = run([sample(0)], candidate(actual_fills=[base, changed]))["results"][0]["costs"]
        assert result["actual_fees"] is None
        assert result["visible_fill_count"] == 2
        assert result["invalid_visible_fill_count"] == 1


def test_rejected_evidence_hash_binds_bad_reference_clock_and_nonfinite_kind():
    variants = [sample(30, source_quote_at="bad", evidence_ref="raw:a"),
                sample(30, source_quote_at="bad", evidence_ref="raw:b"),
                sample(30, source_quote_at="different", evidence_ref="raw:a")]
    values = [run([sample(0), r]) for r in variants]
    assert len({v["data_hash"] for v in values}) == 3
    assert all(routes(v)["sustained"]["status"] == "unknown" for v in values)
    hashes = []
    for price in (None, float("nan"), float("inf"), float("-inf")):
        result = run([sample(0), sample(30, price)])
        hashes.append(result["data_hash"])
        json.dumps(result, allow_nan=False)
    assert len(set(hashes)) == 4
    class NoInspect:
        def __str__(self):
            raise AssertionError("must not stringify live objects")
    bad = sample(30, evidence_ref=NoInspect())
    result = run([bad])
    summary = result["results"][0]["comparisons"][0]["rejected_evidence"][0]
    assert summary["raw_leaf_hash"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("code", ["６００００１", "60001", "6000010", "60000A", " 600001"])
def test_code_ascii_six_digits(code):
    assert run([], candidate(code=code))["results"][0]["reason"] == "code_not_ascii_six_digits"


@pytest.mark.parametrize("ref", ["", "   ", [], {}, True])
def test_calendar_reference_must_be_nonempty_string(ref):
    assert run([], candidate(calendar_ref=ref))["results"][0]["reason"] == "calendar_evidence_missing"


@pytest.mark.parametrize("source,observed", [
    ("11:29:59", "11:30:01"), ("11:30:00", "13:00:00"),
    ("14:59:59", "15:00:01"), ("12:59:59", "13:00:00")])
def test_source_receive_observation_share_declared_trading_session(source, observed):
    s, o = "2026-09-09T"+source, "2026-09-09T"+observed
    row = sample(0, source_quote_at=s, received_at=o, observed_at=o)
    result = run([row], as_of=datetime(2026, 9, 9, 15, 15))
    assert result["results"][0]["comparisons"][0]["status"] == "unknown"


def test_future_bad_sample_does_not_drive_trigger_but_has_separate_hash_evidence():
    visible = [sample(0), sample(30), sample(60)]
    future = sample(90, price=float("nan"), evidence_ref="later",
                    observed_at=(ASOF+timedelta(seconds=1)).isoformat())
    a, b = run(visible), run(visible+[future])
    assert routes(a) == routes(b)
    assert a["data_hash"] != b["data_hash"]
    assert b["results"][0]["comparisons"][0]["rejected_evidence"][0]["reason"] == "future_observation_excluded"


def test_global_and_rejected_candidate_sample_denominators_are_hashed():
    orphan_a = sample(0, candidate_id="orphan", evidence_ref="a")
    orphan_b = dict(orphan_a, evidence_ref="b")
    assert run([orphan_a])["data_hash"] != run([orphan_b])["data_hash"]
    unknown_a, unknown_b = dict(orphan_a, candidate_id=None), dict(orphan_b, candidate_id=None)
    assert run([unknown_a])["data_hash"] != run([unknown_b])["data_hash"]
    c = candidate(eligible=False)
    a, b = run([sample(0, evidence_ref="a")], c), run([sample(0, evidence_ref="b")], c)
    assert a["data_hash"] != b["data_hash"]
    assert a["results"][0]["rejected_sample_evidence"]
    assert a["results"][0]["comparisons"] == []


def test_same_name_distinct_instances_evaluate_only_their_own_samples():
    first, second = candidate(), candidate(candidate_id="c2", account_id=2)
    rows = [sample(i) for i in (0, 30, 60)]
    rows.extend(sample(i, candidate_id="c2", account_id=2) for i in (0, 30, 60))
    result = build_intraday_route_report([first, second], rows, as_of=ASOF, policies=[POLICY])
    assert all(r["comparisons"][0]["routes"]["sustained"]["status"] == "true" for r in result["results"])
    rows[-1]["account_id"] = 1
    changed = build_intraday_route_report([first, second], rows, as_of=ASOF, policies=[POLICY])
    assert changed["results"][0] == result["results"][0]
    assert changed["results"][1]["comparisons"][0]["routes"]["sustained"]["status"] == "unknown"


def test_existing_streak_semantics_reused_without_live_setting_dependency():
    context = freeze_current_route_context(ROUTE_B)
    config = context["account_policy"]["route_signal"]
    policy = RouteResearchPolicy("research:production-frozen",
        config["min_samples"], config["min_persistence_sec"], config["max_sample_gap_sec"],
        clock_jitter_sec=config["clock_jitter_sec"])
    times = [START+timedelta(seconds=i*30) for i in range(20)]
    expected = next(t for t in times if _confirmation_streak_status(
        [s for s in times if s <= t], t, route_id=ROUTE_B)["ready"])
    rows = [sample(int((at-START).total_seconds())) for at in times]
    first = routes(run(rows, policy=policy))["sustained"]["first_event"]
    assert first["source_quote_at"] == expected.isoformat()
    assert context["momentum_path_reference"]["max_quote_gap_sec"] > 0
