from math import inf, nan

import numpy as np
import pytest

from app.data.price_chain import price_chain_evidence, PRICE_CHAIN_MAX_ABS_GAP, FORMAL_CLOSE_SOURCES


@pytest.mark.parametrize("previous,current,status", [
    (10.0, 10.0, "consistent"), (10.0, 10.01, "consistent"),
    (10.0, 10.02, "conflict"), (10.3, 10.0, "conflict"),
    (10.0, 5.0, "conflict"), (5.0, 10.0, "conflict"),
])
def test_price_chain_retains_values_without_inventing_cause(previous, current, status):
    evidence = price_chain_evidence(previous, current)
    assert evidence["status"] == status
    assert evidence["previous_close"] == previous
    assert evidence["current_prev_close"] == current
    assert evidence["historical_prices_changed"] is False
    assert evidence["cause"] == "not_inferred"
    assert evidence["max_abs_gap"] == 0.011


@pytest.mark.parametrize("bad", [None, 0, -1, nan, inf, -inf, True, np.bool_(True), "10.0"])
@pytest.mark.parametrize("side", [0, 1])
def test_bad_prices_are_unknown_not_neutral_or_repaired(bad, side):
    pair = [10.0, 10.0]
    pair[side] = bad
    result = price_chain_evidence(*pair)
    assert result["status"] == "unknown"
    assert result["abs_gap"] is None
    assert result["historical_prices_changed"] is False


def test_existing_consumers_keep_the_exact_original_threshold_and_sources():
    from app.paper import strategy_iteration_shadow, signal_research
    from app.promotion import outcome_evidence
    for module in (strategy_iteration_shadow, signal_research, outcome_evidence):
        assert module._PRICE_CHAIN_MAX_ABS_GAP == PRICE_CHAIN_MAX_ABS_GAP == 0.011
        assert module._FORMAL_CLOSE_SOURCES == FORMAL_CLOSE_SOURCES == {"ths", "tencent_close"}
