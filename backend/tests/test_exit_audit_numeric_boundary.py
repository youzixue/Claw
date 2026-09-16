"""Derived numeric guards do not alter exit rules, history or valid extrema."""
from copy import deepcopy
import json
import math

import pytest

from app.paper import exit_audit
from test_paper_exit_audit import position, spot, step


@pytest.mark.parametrize("bad", [10**400, -(10**400), True, float("nan"), float("inf"), 0, -1])
def test_bad_numeric_conversion_returns_unknown_without_exception(bad):
    assert exit_audit._number(bad) is None


@pytest.mark.parametrize("cost,price", [(1e-308, 1e308), (10, 1e308), (10**400, 10), (10, 10**400)])
def test_overflow_never_poison_extrema_or_strict_json(cost, price):
    state, changed = step(pos=position(buy_price=cost), quote=spot(price=price))
    assert not changed
    assert state["post_entry_high"] is None
    assert state["observed_max_profit_pct"] is None
    json.dumps(state, allow_nan=False)


def test_invalid_ratio_preserves_existing_extrema_without_mutating_input():
    old, _ = step()
    original = deepcopy(old)
    after, changed = step(old, pos=position(buy_price=1e-308), quote=spot(price=1e308))
    assert not changed
    assert after == original == old
    json.dumps(after, allow_nan=False)


@pytest.mark.parametrize("cost,high,expected", [(1e-308, 1e308, None), (10, 1e308, None),
    (10**400, 11, None), (10, 10**400, None), (10, 9, -10), (10, 10, 0), (10, 11, 10)])
def test_session_high_ratio_unknown_not_nonfinite_and_legacy_basis_preserved(cost, high, expected):
    ctx = {"high": high}
    exit_audit.attach_exit_audit(ctx, position=position(buy_price=cost), extrema={}, quote_ok=True)
    assert ctx["session_high_vs_cost_pct"] == expected
    assert ctx["high"] == ctx["session_high"] == high
    assert ctx["exit_high_basis"] == "session_high_frozen_rule_unchanged"
    assert ctx["exit_trigger_reason"] == ""
    assert ctx["exit_evaluation_quote_valid"] is True


def test_representable_large_ratio_preserves_valid_calculation():
    state, changed = step(pos=position(buy_price=1e306), quote=spot(price=1.1e306))
    assert changed and state["post_entry_high"] == 1.1e306
    assert state["observed_max_profit_pct"] == pytest.approx(10)
    assert math.isfinite(state["observed_max_profit_pct"])
    assert exit_audit.finite_profit_pct(10, 10) == 0
    assert exit_audit.finite_profit_pct(9, 10) == pytest.approx(-10)
