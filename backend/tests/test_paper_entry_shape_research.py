"""Shape audit fixtures, not historical samples or a newly invented strategy."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from app.paper.intraday_route_research import (
    ROUTE_A2, ROUTE_C3, build_intraday_route_report, freeze_current_route_context,
)
from test_paper_intraday_route_research import candidate, sample, START, ASOF, POLICY


def hypothesis(family, **kwargs):
    return dict(schema="entry_shape_hypothesis_v1", family=family,
        policy_version="research:shapes-v1", frozen_at=START.isoformat(),
        evidence_ref="frozen:shape", cohort_ref="frozen:all-candidates-before-outcome",
        cohort_frozen_at=START.isoformat(), **kwargs)


def first_board(**kwargs):
    c = candidate(route_id=ROUTE_C3, account_id=None, account_name=None,
        evidence_only=True, previous_trade_date="2026-09-08", **kwargs)
    c["frozen_position"]["position_basis"] = "not_applicable_evidence_only"
    c["entry_shape"] = hypothesis("repair_first_board", prior_context={
        "trade_date":"2026-09-08", "observed_at":"2026-09-09T09:20:00",
        "evidence_ref":"frozen:prior-k", "close":10, "ma20":10.2, "limit_streak":0,
        "price_basis":c["price_basis"]})
    return c


def first_retest(**kwargs):
    c = candidate(route_id=ROUTE_A2, **kwargs)
    c["entry_shape"] = hypothesis("first_retest", armed_anchor={
        "source_quote_at":(START-timedelta(minutes=1)).isoformat(),
        "observed_at":(START-timedelta(minutes=1)).isoformat(),
        "evidence_ref":"shadow:armed", "coverage_ref":"frozen:pre-candidate-path",
        "coverage_end_at":START.isoformat()})
    return c


def quote_for(c, seconds, *, stage=None, price=10.4, **kwargs):
    return sample(seconds, price, candidate_id=c["candidate_id"],
        code=c["code"], account_id=c["account_id"], account_name=c["account_name"],
        production_version=c["production_version"], shape_stage=stage,
        shape_event_ref=f"shadow:{c['candidate_id']}:{seconds}" if stage else None, **kwargs)


def report(cs, rows, as_of=ASOF):
    return build_intraday_route_report(cs, rows, as_of=as_of, policies=[POLICY])


def shape(r, index=0):
    return r["results"][index]["comparisons"][0]["entry_shape"]


def test_first_board_groups_prior_ma20_without_selecting_winners_only():
    below = first_board()
    above = first_board(candidate_id="c2", code="600002")
    above["entry_shape"]["prior_context"]["ma20"] = 9.8
    missing = first_board(candidate_id="c3", code="600003")
    missing["entry_shape"]["prior_context"]["ma20"] = None
    rows = []
    for c, endpoint in ((below, 10.2), (above, 10.6)):
        rows += [quote_for(c, 0, stage="confirmed"),
                 quote_for(c, 30, price=endpoint), quote_for(c, 60, price=endpoint)]
    r = report([below, above, missing], rows)
    assert shape(r)["structure_stratum"] == "prior_below_ma20"
    assert shape(r, 1)["structure_stratum"] == "prior_at_or_above_ma20"
    assert shape(r, 2)["status"] == "unknown"
    groups = [g for g in r["entry_shape_summary"]["strata"] if g["horizon_minutes"] == 1]
    assert sum(g["candidate_count"] for g in groups) == 3
    assert sum(g["negative_count"] for g in groups) == 1
    assert sum(g["positive_count"] for g in groups) == 1
    assert sum(g["unknown_count"] for g in groups) == 1
    assert r["entry_shape_summary"]["full_market_recall"] is None
    assert not r["entry_shape_summary"]["production_promotion_allowed"]


@pytest.mark.parametrize("fault", ["today_k", "future_available", "adjusted", "streak", "bool_ma",
                                  "late_cohort", "no_ref", "fake_account", "fake_fill"])
def test_first_board_future_or_false_evidence_never_classifies_valid(fault):
    c = first_board()
    prior = c["entry_shape"]["prior_context"]
    if fault == "today_k":
        prior["trade_date"] = c["trade_date"]
    elif fault == "future_available":
        prior["observed_at"] = ASOF.isoformat()
    elif fault == "adjusted":
        prior["price_basis"] = "later-qfq"
    elif fault == "streak":
        prior["limit_streak"] = 1
    elif fault == "bool_ma":
        prior["ma20"] = True
    elif fault == "late_cohort":
        c["entry_shape"]["cohort_frozen_at"] = ASOF.isoformat()
    elif fault == "no_ref":
        prior["evidence_ref"] = None
    elif fault == "fake_account":
        c["account_id"] = 99
    else:
        c["actual_fills"] = [{"fill_id":"invented"}]
    r = report([c], [quote_for(c, 0, stage="confirmed")])
    assert r["results"][0]["status"] == "unknown" or shape(r)["status"] == "unknown"


@pytest.mark.parametrize("mode", ["confirmed", "no_pullback", "sealed", "invalidated", "gap", "missing_anchor"])
def test_first_retest_preserves_confirmed_non_retest_failure_and_unknown_controls(mode):
    c = first_retest()
    rows = [quote_for(c, 0, stage="candidate", original_trigger=False),
            quote_for(c, 30, stage="pullback", original_trigger=False),
            quote_for(c, 60, stage="confirmed", original_trigger=True),
            quote_for(c, 90, price=10.2), quote_for(c, 120, price=10.2)]
    if mode == "no_pullback":
        rows[1]["shape_stage"] = None
    elif mode == "sealed":
        rows = rows[:1]+[quote_for(c, 30, stage="sealed", price=11)]
    elif mode == "invalidated":
        rows[1].update(candidate_active=False, shape_stage="invalidated")
    elif mode == "gap":
        rows.pop(1)
    elif mode == "missing_anchor":
        c["entry_shape"].pop("armed_anchor")
    r = shape(report([c], rows))
    assert r["production_permission"] is False
    if mode == "confirmed":
        assert r["status"] == "observed"
        assert r["markouts"]["1"]["endpoint_markout_pct"] < 0
        assert r["markouts"]["1"]["executable_return"] is None
    else:
        assert r["first_event"] is None
        if mode == "sealed":
            assert r["path_outcome"] == "no_retest_sealed_control"
        if mode == "invalidated":
            assert r["reason"] == "candidate_invalidated_no_revival"


def second_setup():
    parent = first_retest()
    at = START+timedelta(minutes=3)
    child = first_retest(candidate_id="new-candidate", frozen_at=at.isoformat())
    child["entry_shape"] = hypothesis("second_ignition", parent_candidate_id="c1",
        parent_event_ref="shadow:c1:30", parent_invalidated_at=(START+timedelta(seconds=30)).isoformat(),
        new_epoch_ref="research:new-epoch")
    child["entry_shape"]["frozen_at"] = at.isoformat()
    rows = [quote_for(parent, 0, stage="candidate", original_trigger=False),
            quote_for(parent, 30, stage="invalidated", candidate_active=False),
            quote_for(parent, 60, stage="confirmed", original_trigger=True),
            quote_for(child, 180, stage="rearmed", original_trigger=False),
            quote_for(child, 210, stage="candidate", original_trigger=False),
            quote_for(child, 240, stage="confirmed", original_trigger=True),
            quote_for(child, 270, price=10.2), quote_for(child, 300, price=10.2)]
    return [parent, child], rows


def test_second_ignition_requires_new_epoch_and_cannot_revive_old_candidate():
    cs, rows = second_setup()
    original = deepcopy((cs, rows))
    r = report(cs, rows)
    assert shape(r)["reason"] == "candidate_invalidated_no_revival"
    assert shape(r, 1)["status"] == "observed"
    assert shape(r, 1)["first_event"]["observed_at"] == (START+timedelta(minutes=4)).isoformat()
    assert shape(r, 1)["markouts"]["1"]["endpoint_markout_pct"] < 0
    assert (cs, rows) == original
    assert report(cs, list(reversed(rows))) == r


@pytest.mark.parametrize("fault", ["same_id", "no_parent", "bad_ref", "parent_future",
                                  "old_version", "no_rearm", "rearm_after_candidate",
                                  "failed_gate", "bad_event_ref"])
def test_second_ignition_never_uses_generic_recross_or_late_parent(fault):
    cs, rows = second_setup()
    c = cs[1]
    if fault == "same_id":
        c["entry_shape"]["parent_candidate_id"] = c["candidate_id"]
    elif fault == "no_parent":
        cs.pop(0)
    elif fault == "bad_ref":
        c["entry_shape"]["parent_event_ref"] = "not-the-terminal-event"
    elif fault == "parent_future":
        c["entry_shape"]["parent_invalidated_at"] = ASOF.isoformat()
    elif fault == "old_version":
        cs[0]["production_version"] = "other-version"
    elif fault == "no_rearm":
        rows[3]["shape_stage"] = None
    elif fault == "rearm_after_candidate":
        rows[3]["shape_stage"], rows[4]["shape_stage"] = "candidate", "rearmed"
    elif fault == "failed_gate":
        rows[5]["setup_valid"] = False
    else:
        rows[5]["shape_event_ref"] = None
    assert shape(report(cs, rows), len(cs)-1)["status"] == "unknown"


def test_unlabeled_and_future_quotes_do_not_manufacture_shape_and_legacy_gate_stays_available():
    c = candidate()
    r = report([c], [sample(0), sample(30), sample(60)])
    assert shape(r)["reason"] == "entry_shape_not_frozen"
    assert r["results"][0]["comparisons"][0]["routes"]["sustained"]["status"] == "true"
    c = first_retest()
    rows = [quote_for(c, 0, stage="candidate", original_trigger=False),
            quote_for(c, 30, stage="pullback"),
            quote_for(c, 60, stage="confirmed", original_trigger=True)]
    first = report([c], rows, as_of=START+timedelta(seconds=30))
    assert shape(first)["first_event"] is None
    rows[-1]["price"] = float("nan")
    later = report([c], rows, as_of=START+timedelta(seconds=30))
    assert first["entry_shape_summary"] == later["entry_shape_summary"]
    json.dumps(later, allow_nan=False)
    assert freeze_current_route_context(ROUTE_C3)["account_policy"] is None


@pytest.mark.parametrize("key", ["account_id", "account_name"])
def test_c3_explicit_null_identity_cannot_be_omitted(key):
    c = first_board()
    c.pop(key)
    r = report([c], [])
    assert r["results"][0]["reason"] == "evidence_only_route_cannot_claim_execution_account"


def test_first_retest_sealed_invalidation_is_not_assumed_fill():
    c = first_retest()
    rows = [quote_for(c, 0, stage="candidate", original_trigger=False),
            quote_for(c, 30, stage="sealed", price=11., candidate_active=False)]
    r = shape(report([c], rows))
    assert r["path_outcome"] == "no_retest_sealed_control" and r["first_event"] is None


def test_empty_report_keeps_no_market_denominator_and_no_promotion():
    r = report([], [])
    assert r["entry_shape_summary"]["strata"] == []
    assert r["entry_shape_summary"]["full_market_recall"] is None
