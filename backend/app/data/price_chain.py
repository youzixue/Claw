"""Shared price-basis checks. Never infer a corporate action or rewrite old bars."""
import math
from numbers import Real

# Preserve the existing paper/research tolerance; this is NOT a trading threshold.
PRICE_CHAIN_MAX_ABS_GAP = 0.011
FORMAL_CLOSE_SOURCES = frozenset({"ths", "tencent_close"})


def price_chain_evidence(previous_close, current_prev_close) -> dict:
    def positive(value):
        try:
            return (isinstance(value, Real) and not isinstance(value, bool)
                    and math.isfinite(value) and value > 0)
        except (ValueError, TypeError, OverflowError):
            return False
    valid = positive(previous_close) and positive(current_prev_close)
    gap = abs(float(previous_close) - float(current_prev_close)) if valid else None
    return {
        "policy": "preserve_observed_price_chain_v1",
        "status": "unknown" if gap is None else "conflict" if gap > PRICE_CHAIN_MAX_ABS_GAP else "consistent",
        "previous_close": float(previous_close) if positive(previous_close) else None,
        "current_prev_close": float(current_prev_close) if positive(current_prev_close) else None,
        "abs_gap": gap, "max_abs_gap": PRICE_CHAIN_MAX_ABS_GAP,
        "cause": "not_inferred", "historical_prices_changed": False,
    }
