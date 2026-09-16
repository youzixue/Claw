"""长周期涨停前形态画像。

该模块只使用传入时点及之前的完整日 K，不读取未来数据，也不直接生成买入信号。
它负责回答“属于哪一种准备结构”；盘中是否可执行仍由异动扫描器的量价、
VWAP、盘口、资金和产业主驱动门控决定。
"""

from __future__ import annotations

from typing import Iterable


REGIME_LABELS = {
    "virgin_low_base": "250日低位首波底座",
    "historical_board_reset": "历史涨停记忆重置",
    "trend_first_pullback": "中期趋势首次回踩",
    "oversold_reset": "超跌止跌修复",
    "high_overheat": "高位过热",
    "neutral": "长周期结构中性",
}


def _mean(values: Iterable[float]) -> float:
    clean = [float(value) for value in values if value is not None and float(value) >= 0]
    return sum(clean) / len(clean) if clean else 0.0


def _return_pct(values: list[float], period: int) -> float:
    if len(values) < period or values[-period] <= 0:
        return 0.0
    return (values[-1] / values[-period] - 1.0) * 100.0


def _position(close: float, highs: list[float], lows: list[float], period: int) -> float:
    size = min(len(highs), len(lows), period)
    if size <= 0:
        return 0.5
    high = max(highs[-size:])
    low = min(lows[-size:])
    if high <= low:
        return 0.5
    return max(0.0, min(1.0, (close - low) / (high - low)))


def _max_drawdown_pct(closes: list[float], period: int) -> float:
    values = closes[-min(len(closes), period):]
    peak = 0.0
    drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak * 100.0)
    return drawdown


def build_long_cycle_profile(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    board_threshold_pct: float = 8.8,
) -> dict:
    """构建最多260个交易日的可解释形态画像。

    输出的 ``quality_setup_score`` 是候选排序分，不是收益率或胜率；
    ``shape_ready`` 只允许进入观察池，永远不代表可直接买入。
    """
    bars: list[tuple[float, float, float, float]] = []
    for close, high, low, volume in zip(closes, highs, lows, volumes):
        try:
            values = (float(close), float(high), float(low), float(volume or 0.0))
        except (TypeError, ValueError):
            continue
        if min(values[:3]) <= 0 or values[1] < values[2] or values[3] < 0:
            continue
        bars.append(values)
    bars = bars[-260:]
    if len(bars) < 20:
        return {
            "sample_days": len(bars),
            "long_cycle_regime": "neutral",
            "long_cycle_regime_label": REGIME_LABELS["neutral"],
            "quality_setup_score": 0.0,
            "shape_ready": False,
            "direct_buy_ready": False,
            "requires_intraday_confirmation": True,
            "setup_phase": "insufficient_history",
            "setup_phase_label": "历史不足",
            "confirmation_requirements": ["至少补齐120个完整交易日日K"],
        }

    clean_closes = [item[0] for item in bars]
    clean_highs = [item[1] for item in bars]
    clean_lows = [item[2] for item in bars]
    clean_volumes = [item[3] for item in bars]
    last_close = clean_closes[-1]
    sample_days = len(bars)

    returns = {
        period: _return_pct(clean_closes, period)
        for period in (5, 20, 60, 120, 250)
    }
    positions = {
        period: _position(last_close, clean_highs, clean_lows, period)
        for period in (60, 120, 250)
    }
    ma20 = _mean(clean_closes[-20:])
    ma20_previous = _mean(clean_closes[-25:-5]) if sample_days >= 25 else ma20
    ma20_slope_5d = (
        (ma20 / ma20_previous - 1.0) * 100.0 if ma20_previous > 0 else 0.0
    )
    high20 = max(clean_highs[-20:])
    high60 = max(clean_highs[-min(sample_days, 60):])
    low20 = min(clean_lows[-20:])
    range20 = (high20 / low20 - 1.0) * 100.0 if low20 > 0 else 0.0
    distance_high20 = (last_close / high20 - 1.0) * 100.0 if high20 > 0 else 0.0
    distance_high60 = (last_close / high60 - 1.0) * 100.0 if high60 > 0 else 0.0

    daily_changes = [
        (clean_closes[index] / clean_closes[index - 1] - 1.0) * 100.0
        for index in range(1, sample_days)
        if clean_closes[index - 1] > 0
    ]

    def count_boards(period: int) -> int:
        return sum(
            1 for value in daily_changes[-min(len(daily_changes), period):]
            if value >= board_threshold_pct
        )

    board_indices = [
        index
        for index, value in enumerate(daily_changes, start=1)
        if value >= board_threshold_pct
    ]
    last_board_age = sample_days - 1 - board_indices[-1] if board_indices else 999

    def count_probes(period: int) -> int:
        start = max(1, sample_days - period)
        hits = 0
        for index in range(start, sample_days):
            previous_close = clean_closes[index - 1]
            if previous_close <= 0:
                continue
            change = (clean_closes[index] / previous_close - 1.0) * 100.0
            volume_base = _mean(clean_volumes[max(0, index - 20):index])
            volume_ratio = clean_volumes[index] / volume_base if volume_base > 0 else 0.0
            if 3.0 <= change < board_threshold_pct and volume_ratio >= 1.10:
                hits += 1
        return hits

    probe_count_20 = count_probes(20)
    probe_count_60 = count_probes(60)
    prior_volume = _mean(clean_volumes[-25:-5]) if sample_days >= 25 else _mean(clean_volumes[:-5])
    recent_volume = _mean(clean_volumes[-5:])
    dry_up_ratio = recent_volume / prior_volume if prior_volume > 0 else 1.0
    board_count_20 = count_boards(20)
    board_count_60 = count_boards(60)
    board_count_120 = count_boards(120)
    board_count_250 = count_boards(250)
    max_drawdown_20 = _max_drawdown_pct(clean_closes, 20)

    high_overheat = bool(
        (positions[250] >= 0.86 and returns[60] >= 30.0)
        or returns[20] >= 32.0
        or (positions[120] >= 0.92 and returns[20] >= 20.0)
    )
    memory_reset = bool(
        board_count_120 >= 1
        and 5 <= last_board_age <= 80
        and positions[120] <= 0.68
        and returns[20] <= 15.0
        and dry_up_ratio <= 1.18
    )
    oversold_reset = bool(
        returns[60] <= -15.0
        and positions[120] <= 0.32
        and ma20_slope_5d >= -1.2
        and (probe_count_20 >= 1 or returns[5] >= -1.5)
    )
    trend_first_pullback = bool(
        returns[120] >= 18.0
        and 0.38 <= positions[120] <= 0.86
        and -12.0 <= returns[20] <= 15.0
        and board_count_20 == 0
        and last_close >= ma20 * 0.94
        and dry_up_ratio <= 1.15
    )
    virgin_low_base = bool(
        sample_days >= 120
        and board_count_120 == 0
        and positions[250] <= 0.38
        and returns[60] <= 22.0
        and ma20_slope_5d >= -1.0
    )

    if high_overheat:
        regime = "high_overheat"
    elif memory_reset:
        regime = "historical_board_reset"
    elif trend_first_pullback:
        regime = "trend_first_pullback"
    elif oversold_reset:
        regime = "oversold_reset"
    elif virgin_low_base:
        regime = "virgin_low_base"
    else:
        regime = "neutral"

    score = 28.0
    if regime in {"virgin_low_base", "historical_board_reset", "oversold_reset"}:
        score += 20.0
    elif regime == "trend_first_pullback":
        score += 17.0
    elif regime == "high_overheat":
        score -= 24.0
    if sample_days >= 250:
        score += 5.0
    elif sample_days >= 120:
        score += 3.0
    if probe_count_20 >= 1:
        score += 10.0
    elif probe_count_60 >= 2:
        score += 6.0
    if 0.55 <= dry_up_ratio <= 1.0:
        score += 10.0
    elif dry_up_ratio <= 1.15:
        score += 5.0
    elif dry_up_ratio >= 1.55:
        score -= 8.0
    if ma20_slope_5d >= 0.0:
        score += 9.0
    elif ma20_slope_5d >= -0.45:
        score += 4.0
    else:
        score -= 6.0
    if -18.0 <= distance_high20 <= -2.0:
        score += 7.0
    elif -2.0 < distance_high20 <= 1.0:
        score += 5.0
    if 12.0 <= range20 <= 45.0:
        score += 4.0
    elif range20 > 65.0:
        score -= 6.0
    if board_count_20 > 0:
        score -= 12.0
    quality_setup_score = max(0.0, min(100.0, score))

    short_preparation_count = sum(
        (
            probe_count_20 >= 1,
            dry_up_ratio <= 1.15,
            ma20_slope_5d >= -0.45,
            -18.0 <= distance_high20 <= 1.5,
        )
    )
    shape_ready = bool(
        sample_days >= 120
        and regime in {
            "virgin_low_base",
            "historical_board_reset",
            "trend_first_pullback",
            "oversold_reset",
        }
        and quality_setup_score >= 58.0
        and short_preparation_count >= 2
        and board_count_20 == 0
    )
    setup_phase = (
        "overheated"
        if high_overheat
        else "armed"
        if shape_ready
        else "preparing"
    )
    setup_phase_label = {
        "overheated": "高位过热禁追",
        "armed": "形态入池待盘中确认",
        "preparing": "结构准备中",
    }[setup_phase]

    return {
        "sample_days": sample_days,
        "long_cycle_regime": regime,
        "long_cycle_regime_label": REGIME_LABELS[regime],
        "quality_setup_score": round(quality_setup_score, 1),
        "shape_ready": shape_ready,
        "direct_buy_ready": False,
        "requires_intraday_confirmation": True,
        "setup_phase": setup_phase,
        "setup_phase_label": setup_phase_label,
        "return_5d": round(returns[5], 2),
        "return_20d": round(returns[20], 2),
        "return_60d": round(returns[60], 2),
        "return_120d": round(returns[120], 2),
        "return_250d": round(returns[250], 2),
        "position_60": round(positions[60], 3),
        "position_120": round(positions[120], 3),
        "position_250": round(positions[250], 3),
        "distance_high_20d_pct": round(distance_high20, 2),
        "distance_high_60d_pct": round(distance_high60, 2),
        "range_20d_pct": round(range20, 2),
        "max_drawdown_20d_pct": round(max_drawdown_20, 2),
        "dry_up_ratio": round(dry_up_ratio, 3),
        "ma20_slope_5d": round(ma20_slope_5d, 3),
        "probe_count_20": probe_count_20,
        "probe_count_60": probe_count_60,
        "board_like_count_20": board_count_20,
        "board_like_count_60": board_count_60,
        "board_like_count_120": board_count_120,
        "board_like_count_250": board_count_250,
        "days_since_last_board_like": last_board_age,
        "short_preparation_count": short_preparation_count,
        "confirmation_requirements": [
            "滚动60秒价格抬升且增量成交速率达标",
            "回收VWAP或预设支撑/压力位",
            "有效产业主驱动、盘口承接或大单资金至少双确认",
            "涨幅进入赔率窗口且未接近涨停价",
        ],
    }
