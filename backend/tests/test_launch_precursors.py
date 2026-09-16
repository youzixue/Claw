from datetime import date, timedelta

import pytest

from app.signal.launch_precursors import (
    apply_logit_delta,
    build_funding_preheat_context,
    build_low_base_sector_ignition_context,
    build_stock_launch_profile,
)


def _repair_bars() -> list[dict]:
    bars: list[dict] = []
    start = date(2026, 1, 1)
    for index in range(121):
        if index < 70:
            close = 10.8 - index * 0.03
        else:
            close = 8.7 + (index - 70) * 0.022
        bars.append(
            {
                "trade_date": start + timedelta(days=index),
                "open": close * 0.997,
                "close": close,
                "high": close * 1.012,
                "low": close * 0.988,
                "volume": 100_000,
                "turnover": 2.4,
                "change_pct": 0.25,
            }
        )
    bars[-1]["volume"] = 125_000
    return bars


def test_stock_launch_profile_requires_repair_and_active_turnover_not_absolute_low():
    profile = build_stock_launch_profile(_repair_bars())

    assert profile["launch_mid_low_repair"] is True
    assert profile["launch_active_volume_turnover"] is True
    assert profile["launch_profile_ready"] is True
    assert 0.12 <= profile["launch_position_120"] <= 0.58
    assert 0.75 <= profile["launch_volume_ratio_20"] <= 2.5

    dead_low = _repair_bars()
    for index, bar in enumerate(dead_low[-25:]):
        bar["close"] = 8.0 - index * 0.02
        bar["open"] = bar["close"] * 1.002
        bar["high"] = bar["close"] * 1.006
        bar["low"] = bar["close"] * 0.994
    rejected = build_stock_launch_profile(dead_low)
    assert rejected["launch_profile_ready"] is False
    assert rejected["launch_deep_low_risk"] is True


def test_funding_preheat_uses_three_day_persistence_instead_of_one_day_spike():
    start = date(2026, 8, 24)
    persistent = build_funding_preheat_context(
        [
            {
                "trade_date": start + timedelta(days=index),
                "main_net_inflow": value * 1_000_000,
                "main_net_inflow_pct": value,
            }
            for index, value in enumerate([-1.0, 1.5, 1.2, 2.0, 2.5])
        ]
    )
    spike = build_funding_preheat_context(
        [
            {
                "trade_date": start + timedelta(days=index),
                "main_net_inflow": value * 1_000_000,
                "main_net_inflow_pct": value,
            }
            for index, value in enumerate([0.2, -4.0, -3.0, -2.0, 12.0])
        ]
    )

    assert persistent["funding_preheat_ready"] is True
    assert persistent["funding_positive_days_3d"] == 3
    assert persistent["funding_main_inflow_pct_3d"] == pytest.approx(5.7)
    assert spike["funding_preheat_ready"] is False
    assert spike["funding_positive_days_3d"] == 1


def test_low_base_sector_ignition_needs_stock_industry_resonance_and_stays_prediction_only():
    stock = build_stock_launch_profile(_repair_bars())
    funding = build_funding_preheat_context(
        [
            {
                "trade_date": date(2026, 8, 24) + timedelta(days=index),
                "main_net_inflow": 10_000_000,
                "main_net_inflow_pct": value,
            }
            for index, value in enumerate([1.5, 2.0, 2.5])
        ]
    )
    industry = {
        "sector_code": "I01",
        "sector_name": "低位设备行业",
        "sector_type": "industry",
        "strength_score": 62.0,
        "limit_up_count": 3,
        "consecutive_days": 2,
        "sector_rotation_score": 68.0,
        "sector_strength_delta": 15.0,
        "sector_limit_up_delta": 2.0,
        "sector_low_position_rotation": True,
        "lifecycle_state": "emerging",
    }

    result = build_low_base_sector_ignition_context(stock, funding, industry)
    no_industry = build_low_base_sector_ignition_context(stock, funding, {})
    outflow = build_funding_preheat_context(
        [
            {
                "trade_date": date(2026, 8, 24) + timedelta(days=index),
                "main_net_inflow": -10_000_000,
                "main_net_inflow_pct": -4.0,
            }
            for index in range(3)
        ]
    )
    outflow_result = build_low_base_sector_ignition_context(stock, outflow, industry)

    assert result["low_base_sector_ignition_ready"] is True
    assert result["low_base_sector_ignition_confirmed"] is True
    assert result["low_base_sector_ignition_prediction_only"] is True
    assert result["launch_direction_logit_delta"] > 0
    assert result["launch_strong_rise_logit_delta"] > result["launch_direction_logit_delta"]
    assert result["launch_probability_targets_split"] is True
    assert outflow_result["launch_direction_logit_delta"] < result["launch_direction_logit_delta"]
    assert outflow_result["launch_strong_rise_logit_delta"] < result["launch_strong_rise_logit_delta"]
    assert no_industry["low_base_sector_ignition_ready"] is False


def test_logit_adjustment_is_small_monotonic_and_bounded():
    assert 0.5 < apply_logit_delta(0.5, 0.2) < 0.56
    assert 0 < apply_logit_delta(0.01, 0.22) < 1
