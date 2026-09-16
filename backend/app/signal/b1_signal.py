"""B1 buy-point signal evaluation for anomaly prioritization and export."""

from __future__ import annotations

from typing import Any

import numpy as np


SIGNAL_LABELS = {
    "volume_reversal": "缩量拐头B1",
    "volume_b1": "缩量B1",
    "super_volume_b1": "超级缩量B1",
    "white_line_retest_b1": "回踩白线B1",
    "super_bull_retest_b1": "超牛股回踩白线B1",
    "yellow_line_retest_b1": "回踩黄线B1",
    "original_b1": "原始B1",
}


def _as_array(bars: list[dict[str, Any]], key: str, default: float = 0.0) -> np.ndarray:
    values = []
    for bar in bars:
        raw = bar.get(key)
        try:
            values.append(float(raw) if raw is not None else default)
        except Exception:
            values.append(default)
    return np.asarray(values, dtype=float)


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if len(values) < period:
        return result
    alpha = 2.0 / (period + 1)
    result[period - 1] = float(np.mean(values[:period]))
    for idx in range(period, len(values)):
        result[idx] = alpha * values[idx] + (1 - alpha) * result[idx - 1]
    return result


def _ma(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if len(values) < period:
        return result
    for idx in range(period - 1, len(values)):
        result[idx] = float(np.mean(values[idx - period + 1:idx + 1]))
    return result


def _rsi(values: np.ndarray, period: int = 3) -> np.ndarray:
    result = np.full(len(values), np.nan)
    if len(values) < period + 1:
        return result
    delta = np.diff(values)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.zeros(len(values))
    avg_loss = np.zeros(len(values))
    avg_gain[period] = float(np.mean(gain[:period]))
    avg_loss[period] = float(np.mean(loss[:period]))
    for idx in range(period + 1, len(values)):
        avg_gain[idx] = (avg_gain[idx - 1] * (period - 1) + gain[idx - 1]) / period
        avg_loss[idx] = (avg_loss[idx - 1] * (period - 1) + loss[idx - 1]) / period
    result[period] = 100.0 if avg_loss[period] == 0 else 100.0 - 100.0 / (1.0 + avg_gain[period] / avg_loss[period])
    for idx in range(period + 1, len(values)):
        if avg_loss[idx] == 0:
            result[idx] = 100.0
        else:
            rs = avg_gain[idx] / avg_loss[idx]
            result[idx] = 100.0 - 100.0 / (1.0 + rs)
    return result


def _kdj(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, n: int = 9, m1: int = 3, m2: int = 3) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    length = len(closes)
    k_arr = np.full(length, 50.0)
    d_arr = np.full(length, 50.0)
    j_arr = np.full(length, 50.0)
    if length < n:
        return k_arr, d_arr, j_arr
    prev_k = 50.0
    prev_d = 50.0
    for idx in range(n - 1, length):
        window_high = highs[idx - n + 1:idx + 1]
        window_low = lows[idx - n + 1:idx + 1]
        high_n = float(np.max(window_high))
        low_n = float(np.min(window_low))
        rsv = 50.0 if high_n == low_n else (closes[idx] - low_n) / (high_n - low_n) * 100.0
        prev_k = ((m1 - 1) * prev_k + rsv) / m1
        prev_d = ((m2 - 1) * prev_d + prev_k) / m2
        k_arr[idx] = prev_k
        d_arr[idx] = prev_d
        j_arr[idx] = 3.0 * prev_k - 2.0 * prev_d
    return k_arr, d_arr, j_arr


def _tail_count(flags: np.ndarray, periods: int) -> int:
    if len(flags) == 0:
        return 0
    return int(np.count_nonzero(flags[-periods:]))


def _tail_every(flags: np.ndarray, periods: int) -> bool:
    if len(flags) < periods:
        return False
    return bool(np.all(flags[-periods:]))


def _last_max(arr: np.ndarray, periods: int) -> float:
    window = arr[-periods:] if len(arr) >= periods else arr
    return float(np.max(window)) if len(window) else 0.0


def _last_min(arr: np.ndarray, periods: int) -> float:
    window = arr[-periods:] if len(arr) >= periods else arr
    return float(np.min(window)) if len(window) else 0.0


def _bars_since_last_true(flags: np.ndarray) -> int:
    indices = np.flatnonzero(flags)
    if len(indices) == 0:
        return 10_000
    return int(len(flags) - 1 - indices[-1])


def _eq(lhs: float, rhs: float, tolerance: float = 1e-6) -> bool:
    return abs(float(lhs) - float(rhs)) <= tolerance


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if abs(float(denominator)) < 1e-9:
        return default
    return float(numerator) / float(denominator)


def _pick_signal(flags: dict[str, bool]) -> str | None:
    priority = [
        "volume_reversal",
        "super_volume_b1",
        "volume_b1",
        "super_bull_retest_b1",
        "white_line_retest_b1",
        "yellow_line_retest_b1",
        "original_b1",
    ]
    for key in priority:
        if flags.get(key):
            return key
    return None


def _evaluate_snapshot(code: str, bars: list[dict[str, Any]]) -> dict[str, Any]:
    if len(bars) < 60:
        return {
            "signal_key": None,
            "signal_label": "",
            "j": None,
            "rsi": None,
            "short_score": None,
            "long_score": None,
            "hold_score": None,
            "kdj_signal": "",
            "close": float(bars[-1].get("close") or 0) if bars else 0.0,
            "trend_white_price": None,
            "is_break_trend": False,
        }

    opens = _as_array(bars, "open")
    highs = _as_array(bars, "high")
    lows = _as_array(bars, "low")
    closes = _as_array(bars, "close")
    volumes = _as_array(bars, "volume")

    trend_white = _ema(_ema(closes, 9), 11)
    yellow_components = [
        _ema(_ema(closes, 7), 7),
        _ema(_ema(closes, 14), 14),
        _ema(_ema(closes, 28), 28),
        _ema(_ema(closes, 56), 56),
    ]
    yellow_stack = np.vstack(yellow_components)
    valid_counts = np.sum(~np.isnan(yellow_stack), axis=0)
    yellow_sum = np.nansum(yellow_stack, axis=0)
    yellow_line = np.divide(yellow_sum, valid_counts, out=np.full(len(closes), np.nan), where=valid_counts > 0)
    bbi = (_ma(closes, 3) + _ma(closes, 6) + _ma(closes, 12) + _ma(closes, 24)) / 4.0

    k_arr, _, j_arr = _kdj(highs, lows, closes, 9, 3, 3)
    rsi_arr = _rsi(closes, 3)

    idx = len(closes) - 1
    prev_idx = idx - 1
    if prev_idx < 0:
        return {
            "signal_key": None,
            "signal_label": "",
            "j": None,
            "rsi": None,
            "short_score": None,
            "long_score": None,
            "hold_score": None,
            "kdj_signal": "",
            "close": float(closes[-1]),
            "trend_white_price": None,
            "is_break_trend": False,
        }

    short_low = _last_min(lows, 3)
    short_high = _last_max(closes, 3)
    short_score = 50.0 if abs(short_high - short_low) < 1e-9 else (closes[idx] - short_low) / (short_high - short_low) * 100.0

    long_low = _last_min(lows, 21)
    long_high = _last_max(highs, 21)
    long_score = 50.0 if abs(long_high - long_low) < 1e-9 else (closes[idx] - long_low) / (long_high - long_low) * 100.0

    board_like = str(code or "").startswith(("30", "68", "4", "8", "9"))
    recent_big_move = False
    for pos in range(max(1, len(closes) - 200), len(closes)):
        if closes[pos - 1] > 0 and closes[pos] / closes[pos - 1] > 1.15:
            recent_big_move = True
            break
    amplitude_limit = 8.0 if board_like or recent_big_move else 5.0
    relax_factor = 0.9 if board_like or recent_big_move else 1.0

    day_amplitude = _safe_div(highs[idx] - lows[idx], lows[idx], 0.0) * 100.0
    day_change_abs = _safe_div(abs(closes[idx] - closes[prev_idx]), closes[prev_idx], 0.0) * 100.0 * relax_factor
    rising_doji = closes[idx] > closes[prev_idx] and _safe_div(abs(closes[idx] - opens[idx]), opens[idx], 0.0) * 100.0 * relax_factor < 1.8

    short_series = []
    long_series = []
    for pos in range(len(closes)):
        llv3 = _last_min(lows[:pos + 1], 3)
        hhv3 = _last_max(closes[:pos + 1], 3)
        llv21 = _last_min(lows[:pos + 1], 21)
        hhv21 = _last_max(highs[:pos + 1], 21)
        short_val = 50.0 if abs(hhv3 - llv3) < 1e-9 else (closes[pos] - llv3) / (hhv3 - llv3) * 100.0
        long_val = 50.0 if abs(hhv21 - llv21) < 1e-9 else (closes[pos] - llv21) / (hhv21 - llv21) * 100.0
        short_series.append(short_val)
        long_series.append(long_val)
    short_series_arr = np.asarray(short_series, dtype=float)
    long_series_arr = np.asarray(long_series, dtype=float)

    needle_under_20 = (short_series_arr <= 20) & (long_series_arr >= 75) | ((long_series_arr - short_series_arr) >= 70)
    treasure_basin = _tail_count(long_series_arr >= 75, 8) >= 6 and _tail_count(short_series_arr <= 70, 7) >= 4 and _tail_count(short_series_arr <= 50, 8) >= 1
    double_halberd = _tail_every(long_series_arr >= 75, 8) and _tail_count(short_series_arr <= 50, 6) >= 2 and _tail_count(short_series_arr <= 20, 7) >= 1
    red_more_than_green = _tail_count(closes >= opens, 15) > 7 or _tail_count(closes[1:] > closes[:-1], 11) > 5

    recent_volume_window = volumes[-40:] if len(volumes) >= 40 else volumes
    if len(recent_volume_window):
        max_volume = float(np.max(recent_volume_window))
        max_positions = np.flatnonzero(recent_volume_window == max_volume)
        rel_idx = int(max_positions[-1])
        abs_idx = len(volumes) - len(recent_volume_window) + rel_idx
        vday = len(volumes) - 1 - abs_idx
    else:
        abs_idx = len(volumes) - 1
        vday = 0

    prev_close_on_volume_peak = closes[abs_idx - 1] if abs_idx - 1 >= 0 else closes[abs_idx]
    not_big_green_bar = closes[abs_idx] >= prev_close_on_volume_peak or closes[abs_idx] >= opens[abs_idx]
    big_green_far = vday >= 15 and not not_big_green_bar

    shrink = volumes[idx] < _last_max(volumes, 20) * 0.416 or volumes[idx] < _last_max(volumes, 50) / 3.0
    retest_shrink = volumes[idx] < _last_max(volumes, 20) * 0.45 or volumes[idx] < _last_max(volumes, 50) / 3.0
    moderate_shrink = volumes[idx] < _last_max(volumes, 20) * 0.618 or volumes[idx] < _last_max(volumes, 50) / 3.0
    super_shrink = volumes[idx] < _last_max(volumes, 30) / 4.0 or volumes[idx] < _last_max(volumes, 50) / 6.0

    recent_amplitude = _safe_div(_last_max(highs, 20) - _last_min(lows, 20), _last_min(lows, 20), 0.0) * 100.0
    recent_amplitude_alt = _safe_div(_last_max(highs, 12) - _last_min(lows, 14), _last_min(lows, 14), 0.0) * 100.0
    long_amplitude = _safe_div(_last_max(highs, 50) - _last_min(lows, 50), _last_min(lows, 50), 0.0) * 100.0
    recent_anomaly = recent_amplitude >= 15 or recent_amplitude_alt >= 11
    long_anomaly = long_amplitude >= 30
    super_anomaly = recent_amplitude >= 60
    washout_anomaly = _tail_count(needle_under_20, 10) >= 2 or treasure_basin or double_halberd

    uptrend = trend_white[idx] >= yellow_line[idx] * 0.999 and (closes[idx] >= yellow_line[idx] or (closes[idx] > yellow_line[idx] * 0.975 and closes[idx] > opens[idx]))
    strong_trend = (
        _tail_every(yellow_line[1:] >= yellow_line[:-1] * 0.999, 13)
        and trend_white[idx] >= trend_white[prev_idx]
        and _tail_every(trend_white > yellow_line, 20)
        and _tail_every(trend_white[1:] >= trend_white[:-1], 11)
        and red_more_than_green
    )

    bbi_up = _tail_every(bbi[1:] >= bbi[:-1] * 0.999, 20) or _tail_count(bbi[1:] >= bbi[:-1], 25) >= 23
    cross_flags = (closes[1:] > yellow_line[1:]) & (closes[:-1] <= yellow_line[:-1])
    super_bull = bbi_up and (recent_amplitude >= 30 or long_amplitude > 80) and _bars_since_last_true(cross_flags) > 12

    distance_white = _safe_div(abs(closes[idx] - trend_white[idx]), closes[idx], 0.0) * 100.0
    low_distance_white = _safe_div(abs(lows[idx] - trend_white[idx]), trend_white[idx], 0.0) * 100.0
    distance_bbi = _safe_div(abs(closes[idx] - bbi[idx]), closes[idx], 0.0) * 100.0
    low_distance_bbi = _safe_div(abs(lows[idx] - bbi[idx]), bbi[idx], 0.0) * 100.0
    retest_white = (
        (closes[idx] >= trend_white[idx] and distance_white <= 2.0)
        or (closes[idx] < trend_white[idx] and distance_white < 0.8)
        or (
            closes[idx] >= bbi[idx]
            and distance_bbi < 2.5
            and low_distance_bbi < 1.0
            and distance_white <= 3.0
            and day_change_abs < 1.0
            and closes[idx] > closes[prev_idx]
        )
    )
    white_support = closes[idx] >= trend_white[idx] and distance_white < 1.5
    strong_retest_no_break = (low_distance_white < 1.0 or low_distance_bbi < 0.5) and closes[idx] > trend_white[idx] and distance_white <= 3.5

    distance_yellow = _safe_div(abs(closes[idx] - yellow_line[idx]), yellow_line[idx], 0.0) * 100.0
    retest_yellow = (
        (closes[idx] >= yellow_line[idx] and (distance_yellow <= 1.5 or (distance_yellow <= 2.0 and day_change_abs < 1.0)))
        or (closes[idx] < yellow_line[idx] and distance_yellow <= 0.8)
    )

    has_death_cross = j_arr[idx] <= k_arr[idx] and j_arr[prev_idx] > k_arr[prev_idx]
    has_golden_cross = j_arr[idx] > k_arr[idx] and j_arr[prev_idx] <= k_arr[prev_idx]
    falling = closes[idx] < closes[prev_idx]
    volume_expansion = volumes[idx] > volumes[prev_idx] and falling
    broken_white = closes[idx] < trend_white[idx]
    trend_turning_down = trend_white[idx] < trend_white[prev_idx]
    hold_score = int((not falling) + (not volume_expansion) + (not broken_white) + (j_arr[idx] > k_arr[idx]) + (not trend_turning_down))

    rsi_value = float(rsi_arr[idx]) if not np.isnan(rsi_arr[idx]) else None
    prev_rsi = float(rsi_arr[prev_idx]) if not np.isnan(rsi_arr[prev_idx]) else None
    j_value = float(j_arr[idx]) if not np.isnan(j_arr[idx]) else None
    prev_j = float(j_arr[prev_idx]) if not np.isnan(j_arr[prev_idx]) else None
    k_value = float(k_arr[idx]) if not np.isnan(k_arr[idx]) else None

    signal_flags = {
        "volume_reversal": (
            uptrend
            and rsi_value is not None
            and prev_rsi is not None
            and (rsi_value - 15.0) >= prev_rsi
            and ((prev_rsi < 20.0) or (prev_j is not None and prev_j < 14.0))
            and day_amplitude < (amplitude_limit + 0.5)
            and (day_change_abs < 2.3 or (rising_doji and day_change_abs < 4.0))
            and (not_big_green_bar or big_green_far)
            and (recent_anomaly or long_anomaly or washout_anomaly)
            and closes[idx] >= yellow_line[idx]
        ),
        "volume_b1": (
            uptrend
            and j_value is not None
            and rsi_value is not None
            and (j_value < 14.0 or rsi_value < 23.0)
            and ((rsi_value + j_value < 55.0) or j_value <= _last_min(j_arr, 20) + 1e-6)
            and day_amplitude < amplitude_limit
            and (day_change_abs < 2.5 or rising_doji)
            and (not_big_green_bar or big_green_far)
            and (shrink or (moderate_shrink and day_change_abs < 1.0))
            and (recent_anomaly or long_anomaly or washout_anomaly)
        ),
        "original_b1": (
            trend_white[idx] > yellow_line[idx]
            and closes[idx] >= yellow_line[idx] * 0.99
            and yellow_line[idx] >= yellow_line[prev_idx]
            and j_value is not None
            and rsi_value is not None
            and (j_value < 13.0 or rsi_value < 21.0)
            and (rsi_value + j_value) < _last_min(rsi_arr + j_arr, 15) * 1.5
            and moderate_shrink
            and (not_big_green_bar or big_green_far)
            and (
                _safe_div(abs(closes[idx] - opens[idx]) * 100.0, opens[idx], 0.0) < 1.5
                or super_shrink
                or (moderate_shrink and volumes[idx] < _last_min(volumes, 20) * 1.1 and j_value <= _last_min(j_arr, 20) + 1e-6)
                or (moderate_shrink and (distance_white < 1.8 or distance_bbi < 1.5 or distance_yellow < 2.8))
            )
            and (recent_anomaly or long_anomaly or washout_anomaly)
        ),
        "super_volume_b1": (
            uptrend
            and j_value is not None
            and rsi_value is not None
            and (j_value < 14.0 or rsi_value < 23.0)
            and (rsi_value + j_value) < 60.0
            and long_amplitude >= 45.0
            and (
                day_amplitude < amplitude_limit
                or (super_anomaly and day_amplitude < amplitude_limit + 3.2 and closes[idx] > opens[idx] and closes[idx] > trend_white[idx])
            )
            and (((closes[idx] < opens[idx]) and volumes[idx] < volumes[prev_idx] and closes[idx] >= yellow_line[idx]) or (closes[idx] >= opens[idx]))
            and (day_change_abs < 2.0 or rising_doji)
            and (not_big_green_bar or big_green_far)
            and super_shrink
            and (recent_anomaly or long_anomaly or washout_anomaly)
        ),
        "white_line_retest_b1": (
            strong_trend
            and j_value is not None
            and rsi_value is not None
            and (j_value < 30.0 or rsi_value < 40.0 or washout_anomaly)
            and (rsi_value + j_value) < 70.0
            and (day_amplitude < amplitude_limit + 0.5 or distance_white < 1.0 or distance_bbi < 1.0)
            and retest_white
            and (day_change_abs < 2.0 or (day_change_abs < 5.0 and white_support))
            and (not_big_green_bar or big_green_far)
            and retest_shrink
            and (recent_anomaly or long_anomaly or washout_anomaly)
            and lows[idx] <= closes[prev_idx]
        ),
        "super_bull_retest_b1": (
            super_bull
            and j_value is not None
            and rsi_value is not None
            and (j_value < 35.0 or rsi_value < 45.0 or washout_anomaly)
            and (rsi_value + j_value) < 80.0
            and _eq(rsi_value + j_value, _last_min(rsi_arr + j_arr, 25), 1e-4)
            and day_amplitude < amplitude_limit + 1.0
            and (day_change_abs < 2.5 or distance_white < 2.0)
            and strong_retest_no_break
            and (not_big_green_bar or big_green_far)
            and (recent_anomaly or long_anomaly or washout_anomaly)
            and moderate_shrink
        ),
        "yellow_line_retest_b1": (
            trend_white[idx] >= yellow_line[idx]
            and closes[idx] >= yellow_line[idx] * 0.975
            and j_value is not None
            and rsi_value is not None
            and (j_value < 13.0 or rsi_value < 18.0)
            and retest_yellow
            and (not_big_green_bar or big_green_far)
            and (
                shrink
                or (
                    moderate_shrink
                    and (j_value <= _last_min(j_arr, 20) + 1e-6 or rsi_value <= _last_min(rsi_arr, 14) + 1e-6)
                )
            )
            and yellow_line[idx] >= yellow_line[prev_idx] * 0.997
            and _ma(closes, 60)[idx] >= _ma(closes, 60)[prev_idx]
            and recent_amplitude >= 11.9
            and long_amplitude >= 19.5
        ),
    }

    signal_key = _pick_signal(signal_flags)
    kdj_signal = "golden_cross" if has_golden_cross else ("death_cross" if has_death_cross else "")
    return {
        "signal_key": signal_key,
        "signal_label": SIGNAL_LABELS.get(signal_key, ""),
        "j": round(j_value, 2) if j_value is not None else None,
        "rsi": round(rsi_value, 2) if rsi_value is not None else None,
        "short_score": round(float(short_score), 2),
        "long_score": round(float(long_score), 2),
        "hold_score": hold_score,
        "kdj_signal": kdj_signal,
        "close": round(float(closes[idx]), 2),
        "trend_white_price": round(float(trend_white[idx]), 2) if not np.isnan(trend_white[idx]) else None,
        "is_break_trend": bool(broken_white),
    }


def _empty_history_metrics() -> dict[str, Any]:
    return {
        "success_rate": None,
        "success_samples": 0,
        "success_scope": "",
        "history_high_confidence": False,
    }


def _history_stats(code: str, bars: list[dict[str, Any]], current_signal_key: str | None, future_days: int = 3) -> dict[str, Any]:
    if len(bars) < 80:
        return _empty_history_metrics()

    close_arr = _as_array(bars, "close")
    start_idx = max(60, len(bars) - 160)
    end_idx = len(bars) - future_days - 1
    all_stats: list[tuple[str, bool]] = []
    same_stats: list[tuple[str, bool]] = []

    for idx in range(start_idx, end_idx + 1):
        snapshot = _evaluate_snapshot(code, bars[:idx + 1])
        signal_key = snapshot.get("signal_key")
        if not signal_key:
            continue
        future_max_close = float(np.max(close_arr[idx + 1:idx + future_days + 1]))
        success = future_max_close > close_arr[idx]
        all_stats.append((signal_key, success))
        if current_signal_key and signal_key == current_signal_key:
            same_stats.append((signal_key, success))

    selected = same_stats if len(same_stats) >= 3 else all_stats
    scope = "same_signal" if len(same_stats) >= 3 else ("all_b1" if selected else "")
    sample_count = len(selected)
    if sample_count == 0:
        metrics = _empty_history_metrics()
        metrics["success_scope"] = scope
        return metrics

    success_count = sum(1 for _, success in selected if success)
    success_rate = success_count / sample_count
    return {
        "success_rate": round(success_rate, 4),
        "success_samples": sample_count,
        "success_scope": scope,
        "history_high_confidence": sample_count >= 3 and success_rate >= 0.55,
    }


def _priority_bonus(signal_key: str | None, hold_score: int | None, success_rate: float | None, success_samples: int) -> int:
    if not signal_key:
        return 0
    bonus = 8
    if success_rate is not None and success_samples >= 3:
        if success_rate >= 0.65 and success_samples >= 5:
            bonus += 12
        elif success_rate >= 0.55:
            bonus += 8
        elif success_rate >= 0.45:
            bonus += 4
    if (hold_score or 0) >= 4:
        bonus += 4
    return bonus


def analyze_b1_signal(code: str, bars: list[dict[str, Any]], future_days: int = 3) -> dict[str, Any]:
    snapshot = _evaluate_snapshot(code, bars)
    history_3d = _history_stats(code, bars, snapshot.get("signal_key"), future_days=3) if snapshot.get("signal_key") else _empty_history_metrics()
    history_5d = _history_stats(code, bars, snapshot.get("signal_key"), future_days=5) if snapshot.get("signal_key") else _empty_history_metrics()
    priority_bonus = _priority_bonus(
        snapshot.get("signal_key"),
        snapshot.get("hold_score"),
        history_3d.get("success_rate"),
        int(history_3d.get("success_samples") or 0),
    )
    return {
        **snapshot,
        "success_rate_3d": history_3d.get("success_rate"),
        "success_samples": history_3d.get("success_samples"),
        "success_scope": history_3d.get("success_scope"),
        "history_high_confidence": history_3d.get("history_high_confidence"),
        "success_rate_5d": history_5d.get("success_rate"),
        "success_samples_5d": history_5d.get("success_samples"),
        "success_scope_5d": history_5d.get("success_scope"),
        "history_high_confidence_5d": history_5d.get("history_high_confidence"),
        "priority_bonus": priority_bonus,
    }
