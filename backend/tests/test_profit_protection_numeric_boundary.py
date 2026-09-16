"""Numeric regressions for the offline evaluator only; no ledger or orders."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import math

import pytest

from app.paper.profit_protection_research import _threshold_reached
from test_paper_profit_protection_research import POLICY, build, position, result, sample

AT = datetime(2026, 9, 9, 9, 30)


@pytest.mark.parametrize("threshold", [5e-324, 1e-12, 1e-10, 1e-9, 0.01, 4.0])
def test_positive_threshold_never_accepts_flat_price(threshold):
    policy = replace(POLICY, activation_profit_pct=threshold, pullback_from_peak_pct=threshold)
    report = build([sample(AT, 10)], policy=policy)
    for basis in result(report)["observations"][0]["research"].values():
        assert basis["peak_profit_pct"] == 0
        assert basis["pullback_from_observed_high_pct"] == 0
        assert basis["trigger"] == "unknown"
        assert basis["legal_research_alert"] == "unknown"


def test_positive_pullback_required_even_after_real_profit():
    policy = replace(POLICY, pullback_from_peak_pct=1e-12)
    rows = [sample(AT, 11), sample(AT+timedelta(seconds=30), 11)]
    assert result(build(rows, policy=policy))["observations"][-1]["research"]["post_entry"]["trigger"] == "unknown"


def test_positive_profit_required_even_after_real_pullback():
    policy = replace(POLICY, activation_profit_pct=1e-12)
    rows = [sample(AT, 10), sample(AT+timedelta(seconds=30), 9.7)]
    assert result(build(rows, policy=policy))["observations"][-1]["research"]["post_entry"]["trigger"] == "unknown"


@pytest.mark.parametrize("cost,price", [(1e-308, 1e308), (10, 1e308), (1, 10**400)])
def test_overflow_input_never_becomes_extrema_or_alert(cost, price):
    p = position(buy_price=cost, stop_loss_price=cost/2)
    rows = [sample(AT, price, buy_price=cost), sample(AT+timedelta(seconds=30), price, buy_price=cost)]
    original = deepcopy((p, rows))
    report = build(rows, p)
    value = result(report)
    assert value["observations"] == []
    assert value["extrema"] == {"post_entry": None, "post_sellable": None}
    assert len(value["rejected_evidence"]) == 2
    assert value["historical_gap_or_invalid"] is True
    assert report["schema_version"] == "profit_protection_research_v2"
    assert report["read_only"] is True
    assert (p, rows) == original
    json.dumps(report, allow_nan=False)


def test_overflow_frame_preserves_prior_high_but_blocks_following_legal_alert():
    rows = [sample(AT, 11), sample(AT+timedelta(seconds=30), 1e308),
            sample(AT+timedelta(seconds=60), 10.5)]
    value = result(build(rows))
    assert value["rejection_counts"] == {"derived_profit_nonfinite": 1}
    assert len(value["observations"]) == 2
    final = value["observations"][-1]["research"]["post_entry"]
    assert final["observed_high"] == 11
    assert final["trigger"] == "true"
    assert final["legal_research_alert"] == "unknown"
    assert math.isfinite(value["extrema"]["post_entry"]["observed_max_profit_pct"])


def test_representable_large_prices_are_not_rejected_by_arbitrary_absolute_cap():
    p = position(buy_price=1e306, stop_loss_price=9e305)
    rows = [sample(AT, 1.1e306, buy_price=1e306),
            sample(AT+timedelta(seconds=30), 1.05e306, buy_price=1e306)]
    value = result(build(rows, p))
    final = value["observations"][-1]["research"]["post_entry"]
    assert not value["rejected_evidence"]
    assert final["trigger"] == "true"
    assert final["peak_profit_pct"] == pytest.approx(10)
    assert value["executable_net_return"] is None


def test_threshold_rounding_accepts_decimal_equality_but_not_materially_below():
    rows = [sample(AT, 10.4), sample(AT+timedelta(seconds=30), 10.192)]
    final = result(build(rows))["observations"][-1]["research"]["post_entry"]
    assert final["trigger"] == "true"
    assert final["legal_research_alert"] == "true"
    assert _threshold_reached(math.nextafter(2, 0), 2)
    assert not _threshold_reached(2-1e-8, 2)
    assert not _threshold_reached(5e-13, 1e-12)


@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), -float("inf"), -1])
def test_threshold_comparison_rejects_invalid_derived_numbers(bad):
    assert not _threshold_reached(bad, 2)
