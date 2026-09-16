"""Isolated sampled-evidence aggregation; no real archive, DB or market claims."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.paper.intraday_route_research import build_intraday_route_report
from test_paper_intraday_route_research import START, ASOF, POLICY, candidate, sample


def build(candidates, samples, **kwargs):
    return build_intraday_route_report(
        candidates, samples, as_of=kwargs.get("as_of", ASOF),
        policies=kwargs.get("policies", [POLICY]))


def strata(report, comparison="original", horizon=1):
    return [s for s in report["evidence_summary"]["strata"]
            if s["comparison"] == comparison and s["horizon_minutes"] == horizon]


def test_success_failure_flat_unknown_and_rejected_denominators():
    candidates, samples = [], []
    for key, price in (("win", 10.6), ("loss", 10.2), ("flat", 10.4),
                       ("missing", None), ("ineligible", None)):
        candidates.append(candidate(candidate_id=key, eligible=key != "ineligible"))
        samples.append(sample(0, candidate_id=key))
        if price is not None:
            samples.extend(sample(i, price, candidate_id=key) for i in (30, 60))
    before = deepcopy((candidates, samples))
    result = build(candidates, samples)
    rows = strata(result)
    assert sum(s["candidate_count"] for s in rows) == 5
    assert sum(s["event_count"] for s in rows) == 4
    for label, count in (("positive", 1), ("negative", 1), ("flat", 1), ("unknown", 2)):
        assert sum(s[label + "_count"] for s in rows) == count
    assert sum(s["rejected_candidate_count"] for s in rows) == 1
    assert result["evidence_summary"]["full_market_recall"] is None
    assert (candidates, samples) == before
    for row in result["evidence_summary"]["strata"]:
        assert row["candidate_count"] == sum(row[k + "_count"] for k in (
            "positive", "negative", "flat", "unknown"))


def test_versions_policies_sessions_never_pool_and_order_is_deterministic():
    afternoon = datetime(2026, 9, 9, 13, 15)
    candidates = [candidate(), candidate(candidate_id="c2", production_version="v2",
        frozen_at=afternoon.isoformat())]
    rows = [sample(i) for i in (0, 30, 60)]
    for i in (0, 30, 60):
        at = (afternoon + timedelta(seconds=i)).isoformat()
        rows.append(sample(i, candidate_id="c2", production_version="v2",
            source_quote_at=at, received_at=at, observed_at=at))
    policies = [POLICY, replace(POLICY, version="research:v2")]
    kwargs = {"policies": policies, "as_of": afternoon + timedelta(minutes=15)}
    result = build(candidates, rows, **kwargs)
    summary = result["evidence_summary"]
    assert summary == build(candidates[::-1], rows[::-1], **kwargs)["evidence_summary"]
    groups = strata(result)
    assert len(groups) == 4
    assert {s["event_session"] for s in groups} == {"morning", "afternoon"}
    assert {s["production_version"] for s in groups} == {"frozen-old-v1", "v2"}
    assert all(s["candidate_count"] == 1 for s in groups)


@pytest.mark.parametrize("rows,reason", [
    ([sample(0), sample(60)], "coverage_gap"),
    ([sample(0)], "samples_missing"),
    ([sample(0), sample(30, price=float("nan"))],
     "comparison_path_unproven_original_evidence_retained"),
])
def test_missing_corrupt_coverage_never_enters_observed_denominator(rows, reason):
    groups = strata(build([candidate()], rows))
    assert len(groups) == 1
    assert groups[0]["coverage"] == "unknown"
    assert groups[0]["coverage_reason"] == reason
    assert groups[0]["unknown_count"] == 1
    assert groups[0]["positive_count"] == 0


def test_right_censoring_no_event_and_non_applicable_are_retained():
    result = build([candidate()], [sample(0)], as_of=START)
    assert strata(result)[0]["coverage_reason"] == "right_censored"
    for comparison in ("sustained", "recross", "reseal"):
        row = strata(result, comparison)[0]
        assert row["event_session"] == "no_observed_event"
        assert row["unknown_count"] == row["candidate_count"] == 1
        assert row["event_count"] == 0


def test_invalidated_old_signal_cannot_use_afternoon_restart_as_recross():
    rows = [sample(0), sample(30), sample(60), sample(90, candidate_active=False)]
    for i, price in ((10800, 10.4), (10830, 10.2), (10860, 10.4)):
        rows.append(sample(i, price))
    result = build([candidate()], rows, as_of=datetime(2026, 9, 9, 13, 2))
    route = result["results"][0]["comparisons"][0]["routes"]
    assert route["sustained"]["current_reason"] == "candidate_invalidated_no_revival"
    assert len(route["sustained"]["events"]) == 1
    assert route["recross"]["events"] == []
    assert strata(result, "sustained")[0]["invalidated_count"] == 1


def test_empty_malformed_duplicate_and_orphan_inputs_preserve_scope():
    assert build([], [])["evidence_summary"]["strata"] == []
    result = build([None, candidate(), candidate()], [sample(0, candidate_id="orphan")])
    assert sum(s["candidate_count"] for s in strata(result)) == 3
    assert sum(s["unknown_count"] for s in strata(result)) == 3
    assert result["orphan_sample_count"] == 1


def test_future_sample_does_not_change_evaluated_strata():
    rows = [sample(i) for i in (0, 30, 60)]
    before = build([candidate()], rows)
    future = sample(90, price=float("nan"),
                    observed_at=(ASOF + timedelta(seconds=1)).isoformat())
    after = build([candidate()], rows + [future])
    assert before["evidence_summary"] == after["evidence_summary"]
