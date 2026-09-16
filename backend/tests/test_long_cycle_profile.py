from app.signal.long_cycle_profile import build_long_cycle_profile


def _ohlcv(closes: list[float], volumes: list[float] | None = None):
    return {
        "closes": closes,
        "highs": [value * 1.015 for value in closes],
        "lows": [value * 0.985 for value in closes],
        "volumes": volumes or [100.0] * len(closes),
    }


def test_long_cycle_profile_arms_250d_low_base_but_never_direct_buy():
    closes = (
        [20.0 - index * 0.04 for index in range(180)]
        + [12.8 + (index % 6) * 0.05 for index in range(60)]
        + [
            12.7, 13.2, 12.9, 13.4, 13.0,
            13.3, 13.1, 13.5, 13.2, 13.6,
            13.4, 13.7, 13.5, 13.8, 13.6,
            13.9, 13.7, 14.0, 13.9, 14.2,
        ]
    )
    volumes = (
        [200.0] * 200
        + [180.0] * 35
        + [
            260, 170, 165, 280, 160,
            155, 270, 150, 145, 250,
            140, 135, 260, 130, 125,
            280, 120, 115, 300, 320,
        ]
    )

    profile = build_long_cycle_profile(**_ohlcv(closes, volumes))

    assert profile["long_cycle_regime"] == "virgin_low_base"
    assert profile["position_250"] < 0.20
    assert profile["probe_count_20"] >= 1
    assert profile["shape_ready"] is True
    assert profile["direct_buy_ready"] is False
    assert profile["requires_intraday_confirmation"] is True


def test_long_cycle_profile_recognizes_board_memory_after_reset():
    closes = [10.0] * 190
    closes += [10.0, 11.0]
    closes += [11.0 - min(index, 25) * 0.08 for index in range(35)]
    closes += [9.0 + (index % 4) * 0.04 for index in range(33)]
    volumes = [100.0] * 190 + [100.0, 260.0] + [110.0] * 35 + [70.0] * 33

    profile = build_long_cycle_profile(**_ohlcv(closes, volumes))

    assert profile["board_like_count_120"] >= 1
    assert 5 <= profile["days_since_last_board_like"] <= 80
    assert profile["long_cycle_regime"] == "historical_board_reset"
    assert profile["direct_buy_ready"] is False


def test_long_cycle_profile_blocks_high_position_trend_overheat():
    closes = [10.0 * (1.006 ** index) for index in range(260)]
    profile = build_long_cycle_profile(**_ohlcv(closes, [100.0 + index for index in range(260)]))

    assert profile["long_cycle_regime"] == "high_overheat"
    assert profile["position_250"] > 0.95
    assert profile["shape_ready"] is False
    assert profile["setup_phase"] == "overheated"


def test_long_cycle_profile_uses_only_supplied_history():
    history = [10.0 + (index % 5) * 0.02 for index in range(140)]
    baseline = build_long_cycle_profile(**_ohlcv(history))
    future_spike = build_long_cycle_profile(**_ohlcv(history + [20.0]))

    assert baseline["long_cycle_regime"] != "high_overheat"
    assert future_spike["long_cycle_regime"] == "high_overheat"
    assert baseline["sample_days"] == 140
