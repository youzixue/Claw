"""因子输入契约：没有观测值不是零分，有限窗口不能跳过NaN后继续排名。"""
import json
from dataclasses import asdict
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.factors.base import (
    FACTOR_INPUT_CONTRACT, FactorBase, FactorEngine, FactorRegistry, FactorResult,
)

FACTORS = list(FactorRegistry.all_factors().values())


@pytest.fixture
def complete_inputs():
    close = np.array([10 + i * .03 + (i % 3) * .15 for i in range(30)])
    df = pd.DataFrame({
        "close": close, "high": close + .2, "low": close - .2,
        "volume": np.arange(30) * 1000.0 + 100_000,
        "amount": np.full(30, 50_000_000.0), "turnover": np.full(30, 2.0),
        "amplitude": np.full(30, 3.0), "main_net_inflow": np.full(30, 200_000.0),
        "big_net_inflow": np.full(30, 100_000.0),
        "small_net_inflow": np.full(30, -100_000.0),
        "margin_change": np.full(30, 1.0),
    })
    context = {}
    for factor in FACTORS:
        for key in factor.required_context:
            context[key] = factor.context_enums[key][0] if key in factor.context_enums else 1.0
    context.update(news_avg_importance=5.0, limit_up_time="10:00:00")
    return df, context


@pytest.mark.parametrize("factor", FACTORS, ids=lambda f: f.factor_name)
def test_all_registered_factors_empty_inputs_are_unavailable(factor):
    result = factor.calculate(pd.DataFrame())
    assert result.value is None
    assert result.confidence == 0.0
    assert result.rank is result.pct is None
    assert result.direction == factor.direction
    assert result.meta["input_contract"] == FACTOR_INPUT_CONTRACT
    json.dumps(asdict(result), allow_nan=False)


@pytest.mark.parametrize("factor", FACTORS, ids=lambda f: f.factor_name)
def test_complete_inputs_keep_existing_formula_and_direction(factor, complete_inputs):
    df, context = complete_inputs
    result = factor.calculate(df, **context)
    original = factor.calculate.__wrapped__(factor, df, **context)
    assert result.value == original.value
    assert result.value is not None, (factor.factor_name, result.meta)
    assert result.direction == factor.direction
    assert result.confidence == 1
    json.dumps(asdict(result), allow_nan=False)


CONTEXT_FIELDS = [(factor, key) for factor in FACTORS for key in factor.required_context]


@pytest.mark.parametrize("bad", [None, np.nan, np.inf, -np.inf, "", "bad", True, [1]])
@pytest.mark.parametrize("factor,key", CONTEXT_FIELDS,
                         ids=[f"{f.factor_name}:{key}" for f, key in CONTEXT_FIELDS])
def test_each_required_context_rejects_missing_or_invalid(factor, key, bad, complete_inputs):
    df, context = complete_inputs
    context[key] = bad
    result = factor.calculate(df, **context)
    assert result.value is None and result.confidence == 0.0
    assert any(issue["field"] == key for issue in result.meta["input_issues"])
    json.dumps(asdict(result), allow_nan=False)


DEPENDENCIES = [(factor, key) for factor in FACTORS for key in factor.dependencies]


@pytest.mark.parametrize("bad", [None, np.nan, np.inf, -np.inf, "bad"])
@pytest.mark.parametrize("factor,key", DEPENDENCIES,
                         ids=[f"{f.factor_name}:{key}" for f, key in DEPENDENCIES])
def test_each_required_market_window_rejects_bad_leaf(factor, key, bad, complete_inputs):
    df, context = complete_inputs
    df[key] = df[key].astype(object)
    df.loc[df.index[-1], key] = bad
    result = factor.calculate(df, **context)
    assert result.value is None and result.confidence == 0.0
    assert result.direction == factor.direction


@pytest.mark.parametrize("bad", [None, np.nan, np.inf, -np.inf, True, "0", [1], {"a": 1}])
def test_result_invalid_values_clear_rank_and_confidence(bad):
    result = FactorResult("x", value=bad, rank=1, pct=1,
                          meta={"nested": [np.nan, np.inf, np.float64(0)]})
    assert result.value is None and result.rank is result.pct is None
    assert result.confidence == 0
    assert result.meta["nested"] == [None, None, 0.0]
    json.dumps(asdict(result), allow_nan=False)


@pytest.mark.parametrize("confidence", [None, np.nan, np.inf, -1, 1.1, "1", True])
def test_invalid_confidence_cannot_retain_rank(confidence):
    result = FactorResult("x", value=0, confidence=confidence, rank=1, pct=1)
    assert result.value == 0 and result.confidence == 0
    assert result.rank is result.pct is None


def test_numeric_overflow_is_unavailable():
    result = FactorResult("x", value=10 ** 400)
    assert result.value is None and result.confidence == 0


def test_zero_is_a_real_observation():
    result = FactorResult("x", value=np.float64(0))
    assert result.value == 0 and result.confidence == 1


@pytest.mark.parametrize("name,kwargs", [
    ("limit_up_count", {"limit_up_count": 0}),
    ("board_height", {"board_height": 0}),
    ("sector_resonance", {"stock_main_net_inflow": 0, "sector_main_net_inflow": 0}),
    ("sector_support", {"sector_limit_up_count": 0, "sector_stock_count": 20}),
    ("auction_strength", {"auction_volume_ratio": 0, "auction_open_change": 0}),
    ("news_heat", {"news_count_1h": 0, "news_count_24h": 0}),
    ("bull_resonance", {"stock_bull_ratio": 0, "sector_bull_ratio": 0}),
    ("policy_sensitivity", {"policy_frequency_30d": 0, "policy_impact_coefficient": 1}),
    ("margin_buy_strength", {"margin_buy": 0, "amount": 50_000_000}),
])
def test_explicit_zero_context_is_not_missing(name, kwargs):
    result = FactorRegistry.get(name).calculate(pd.DataFrame(), **kwargs)
    assert result.value == 0 and result.confidence == 1


@pytest.mark.parametrize("kwargs", [
    {"news_count_1h": 0}, {"news_count_24h": 0},
    {"news_count_1h": 1, "news_count_24h": 0, "news_avg_importance": 5},
    {"news_count_1h": 1, "news_count_24h": 2},
    {"news_count_1h": 1, "news_count_24h": 2, "news_avg_importance": np.nan},
    {"news_count_1h": -1, "news_count_24h": 2, "news_avg_importance": 5},
])
def test_news_absent_partial_and_inconsistent_windows_are_not_empty(kwargs):
    assert FactorRegistry.get("news_heat").calculate(pd.DataFrame(), **kwargs).value is None


@pytest.mark.parametrize("name", ["sentiment_cycle", "lifecycle_stage"])
def test_unknown_category_cannot_become_recovery_or_launch(name):
    assert FactorRegistry.get(name).calculate(pd.DataFrame(), **{name: "unknown"}).value is None


def test_heuristic_promotion_score_is_not_a_calibrated_probability():
    result = FactorRegistry.get("promotion_rate").calculate(
        pd.DataFrame(), consecutive_days=1, sentiment_cycle="recovery")
    assert result.value == 40
    assert result.meta["calibrated_probability"] is False
    assert result.meta["semantics"] == "heuristic_score"


@pytest.mark.parametrize("name,column,position", [
    ("fund_flow_trend_3d", "main_net_inflow", -2),
    ("volume_ratio", "volume", -4),
    ("volume_spike", "volume", -15),
    ("chip_structure", "close", -10),
    ("rsi_14", "close", -4),
    ("macd_signal", "close", 0),
    ("kdj_golden", "high", 0),
])
def test_internal_missing_bars_are_not_skipped_by_sum_mean_or_ema(
    name, column, position, complete_inputs,
):
    df, context = complete_inputs
    df.loc[df.index[position], column] = np.nan
    assert FactorRegistry.get(name).calculate(df, **context).value is None


def test_fixed_window_does_not_depend_on_unused_older_bars(complete_inputs):
    df, context = complete_inputs
    expected = FactorRegistry.get("ma5_bias").calculate(df, **context).value
    df.loc[0, "close"] = np.nan
    assert FactorRegistry.get("ma5_bias").calculate(df, **context).value == expected


@pytest.mark.parametrize("name", ["volume_spike", "breakout_energy"])
def test_twenty_prior_bars_require_twenty_one_observations(name, complete_inputs):
    df, context = complete_inputs
    factor = FactorRegistry.get(name)
    assert factor.calculate(df.iloc[-20:], **context).value is None
    assert factor.calculate(df.iloc[-21:], **context).value is not None


def test_margin_history_cannot_skip_missing_day_or_borrow_fallback(complete_inputs):
    df, context = complete_inputs
    df.loc[df.index[-3], "margin_change"] = np.nan
    factor = FactorRegistry.get("margin_balance_trend")
    assert factor.calculate(df, margin_balance_change_avg5=1).value is None
    assert factor.calculate(pd.DataFrame(), margin_balance_change_avg5=0).value == 0


@pytest.mark.parametrize("clock", [None, "", "bad", "25:00:00", "10:70:00", "16:00:00", "12:00:00"])
def test_first_board_missing_or_invalid_time_is_not_late_board(clock):
    result = FactorRegistry.get("first_board_quality").calculate(
        pd.DataFrame(), limit_up_time=clock, seal_amount=100_000_000, break_count=0)
    assert result.value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", [1, -1])
async def test_cross_section_excludes_missing_and_zero_confidence_but_keeps_zero(monkeypatch, direction):
    class TestFactor(FactorBase):
        factor_name = "test_value"
        def calculate(self, df, **kwargs):
            return FactorResult(self.factor_name, value=df["value"].iloc[-1],
                                confidence=df["confidence"].iloc[-1], direction=self.direction)
    factor = TestFactor()
    factor.direction = direction
    monkeypatch.setattr(FactorRegistry, "_factors", {"test_value": factor})
    stock_data = {code: pd.DataFrame({"value": [value], "confidence": [confidence]})
                  for code, value, confidence in [
                      ("600001", 0, 1), ("600002", 2, 1), ("600003", None, 1),
                      ("600004", np.nan, 1), ("600005", np.inf, 1), ("600006", 9, 0)]}
    results = await FactorEngine().compute_cross_section(date(2026, 9, 11), stock_data)
    assert results["600001"]["test_value"].rank == (2 if direction == 1 else 1)
    assert results["600002"]["test_value"].rank == (1 if direction == 1 else 2)
    for code in ("600003", "600004", "600005", "600006"):
        assert results[code]["test_value"].rank is None


@pytest.mark.asyncio
async def test_exception_keeps_reverse_direction(monkeypatch):
    class BrokenFactor(FactorBase):
        factor_name = "broken"
        direction = -1
        def calculate(self, df, **kwargs):
            raise ValueError("test invalid input")
    monkeypatch.setattr(FactorRegistry, "_factors", {"broken": BrokenFactor()})
    results = await FactorEngine().compute_single("600001", date(2026, 9, 11), pd.DataFrame())
    result = results["broken"]
    assert result.value is None and result.confidence == 0
    assert result.direction == -1 and result.meta["error_type"] == "ValueError"
