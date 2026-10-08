"""Reconstruct a multi-year point-in-time candidate panel from daily bars.

This panel is for challenger pretraining and feature discovery. It deliberately
uses only information available at each close and reports broad-prefilter recall;
it does not impersonate frozen Champion predictions.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.price_limit_rules import (
    ST_MAINBOARD_LIMIT_REFORM_DATE,
    limit_up_change_threshold,
)
from app.core.trade_calendar import is_official_closed_day
from app.models.stock import FundFlow, StockKline
from app.promotion.modeling.dataset import DatasetBundle
from app.promotion.modeling.features import FeatureRow
from app.promotion.regime import classify_historical_market_context
from app.promotion.outcome_evidence import formal_outcome_bar_error


_MAIN_CODE_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
_LIMIT_UP_THRESHOLD = limit_up_change_threshold(
    "600000", adjusted_bar=True
)  # forward-adjusted main-board detection threshold
_HISTORICAL_PANEL_VERSION = (
    "historical_panel_v3_outcome_blind_candidates_partial_labels"
)
_REFERENCE_BETA_PRIORS = {
    1: (2.0, 48.0),  # 4% conservative first-board candidate prior
    2: (2.0, 8.0),  # 20% conservative consecutive-board candidate prior
}


@dataclass(frozen=True, slots=True)
class _Bar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    turnover: float
    change_pct: float
    prev_close: float
    source: str = ""


def _number(value, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if np.isfinite(parsed) else default


def _is_main_code(code: str) -> bool:
    return str(code or "").startswith(_MAIN_CODE_PREFIXES)


def _is_limit_up(change_pct: float) -> bool:
    return _number(change_pct) >= _LIMIT_UP_THRESHOLD


def _is_ambiguous_pre_reform_st_move(bar: _Bar) -> bool:
    """Flag a 5%-like move whose historical ST identity cannot be recovered."""

    return (
        bar.trade_date < ST_MAINBOARD_LIMIT_REFORM_DATE
        and 4.5 <= _number(bar.change_pct) <= 6.2
    )


def _safe_mean(values: np.ndarray) -> float:
    return float(np.mean(values)) if len(values) else 0.0


def _expanding_reference_base_rate(
    *,
    target_board: int,
    prior_positive_count: int,
    prior_sample_count: int,
) -> float:
    """Return a smoothed rate based only on outcomes before the scored day."""

    alpha, beta = _REFERENCE_BETA_PRIORS[int(target_board)]
    return float(
        (max(int(prior_positive_count), 0) + alpha)
        / (max(int(prior_sample_count), 0) + alpha + beta)
    )


def _return(close: np.ndarray, index: int, days: int) -> float:
    previous_index = index - days
    if previous_index < 0 or close[previous_index] <= 0:
        return 0.0
    return float((close[index] / close[previous_index] - 1.0) * 100.0)


def _gap_to(reference: float, current: float) -> float:
    if reference <= 0:
        return 0.0
    return float((current / reference - 1.0) * 100.0)


def _historical_values(
    bars: list[_Bar],
    index: int,
    *,
    fund_map: dict[tuple[str, date], tuple[float, float]],
    market_context: dict[date, dict[str, float]],
) -> tuple[dict[str, float], tuple[float, float, float, float]]:
    current = bars[index]
    window = bars[max(0, index - 60) : index + 1]
    local_index = len(window) - 1
    close = np.asarray([bar.close for bar in window], dtype=float)
    volume = np.asarray([bar.volume for bar in window], dtype=float)
    amount = np.asarray([bar.amount for bar in window], dtype=float)
    changes = np.asarray([bar.change_pct for bar in window], dtype=float)
    highs = np.asarray([bar.high for bar in window], dtype=float)
    previous_volumes_5 = volume[max(0, local_index - 5) : local_index]
    previous_volumes_20 = volume[max(0, local_index - 20) : local_index]
    previous_amounts_5 = amount[max(0, local_index - 5) : local_index]
    mean_volume_5 = _safe_mean(previous_volumes_5)
    mean_volume_20 = _safe_mean(previous_volumes_20)
    mean_amount_5 = _safe_mean(previous_amounts_5)

    def moving_average(days: int) -> float:
        return _safe_mean(close[max(0, len(close) - days) :])

    def rolling_high(days: int) -> float:
        values = highs[max(0, len(highs) - days) :]
        return float(np.max(values)) if len(values) else current.high

    limit_indexes = [
        item_index
        for item_index, change in enumerate(changes)
        if _is_limit_up(float(change))
    ]
    previous_limit_indexes = [
        item_index for item_index in limit_indexes if item_index < local_index
    ]
    days_since_limit = (
        local_index - previous_limit_indexes[-1] if previous_limit_indexes else 999
    )
    current_fund = fund_map.get((current.code, current.trade_date), (0.0, 0.0))
    recent_fund_dates = [bar.trade_date for bar in bars[max(0, index - 2) : index + 1]]
    fund_3d = sum(fund_map.get((current.code, day), (0.0, 0.0))[0] for day in recent_fund_dates)
    market = market_context.get(current.trade_date, {})
    prev_close = current.prev_close or (bars[index - 1].close if index > 0 else 0.0)
    amplitude = (
        (current.high - current.low) / prev_close * 100.0 if prev_close > 0 else 0.0
    )
    close_position = (
        (current.close - current.low) / (current.high - current.low)
        if current.high > current.low
        else 0.5
    )
    ma5 = moving_average(5)
    ma10 = moving_average(10)
    ma20 = moving_average(20)
    ma60 = moving_average(60)
    high20 = rolling_high(20)
    high60 = rolling_high(60)
    values = {
        "hist_return_1d": current.change_pct,
        "hist_return_3d": _return(close, local_index, 3),
        "hist_return_5d": _return(close, local_index, 5),
        "hist_return_10d": _return(close, local_index, 10),
        "hist_return_20d": _return(close, local_index, 20),
        "hist_volatility_5d": float(np.std(changes[max(0, local_index - 4) : local_index + 1])),
        "hist_volatility_20d": float(np.std(changes[max(0, local_index - 19) : local_index + 1])),
        "hist_volume_ratio_5d": current.volume / mean_volume_5 if mean_volume_5 > 0 else 0.0,
        "hist_volume_ratio_20d": current.volume / mean_volume_20 if mean_volume_20 > 0 else 0.0,
        "hist_amount_ratio_5d": current.amount / mean_amount_5 if mean_amount_5 > 0 else 0.0,
        "hist_turnover": current.turnover,
        "hist_amplitude": amplitude,
        "hist_close_position": close_position,
        "hist_gap_pct": _gap_to(prev_close, current.open),
        "hist_ma_gap_5d": _gap_to(ma5, current.close),
        "hist_ma_gap_10d": _gap_to(ma10, current.close),
        "hist_ma_gap_20d": _gap_to(ma20, current.close),
        "hist_ma_gap_60d": _gap_to(ma60, current.close),
        "hist_high_gap_20d": _gap_to(high20, current.close),
        "hist_high_gap_60d": _gap_to(high60, current.close),
        "hist_drawdown_20d": min(_gap_to(high20, current.close), 0.0),
        "hist_drawdown_60d": min(_gap_to(high60, current.close), 0.0),
        "hist_limit_hits_5d": float(sum(_is_limit_up(value) for value in changes[max(0, local_index - 4) : local_index + 1])),
        "hist_limit_hits_20d": float(sum(_is_limit_up(value) for value in changes[max(0, local_index - 19) : local_index + 1])),
        "hist_limit_hits_60d": float(sum(_is_limit_up(value) for value in changes[max(0, local_index - 59) : local_index + 1])),
        "hist_days_since_limit_up": float(min(days_since_limit, 999)),
        "hist_fund_main_net_inflow": current_fund[0],
        "hist_fund_main_net_inflow_pct": current_fund[1],
        "hist_fund_3d_sum": fund_3d,
        "hist_market_advance_ratio": _number(market.get("advance_ratio")),
        "hist_market_limit_up_count": _number(market.get("limit_up_count")),
        "hist_market_median_return": _number(market.get("median_return")),
        "hist_market_return_dispersion": _number(market.get("return_dispersion")),
    }
    momentum_score = (
        values["hist_return_5d"] * 0.8
        + values["hist_return_20d"] * 0.25
        + min(values["hist_volume_ratio_5d"], 5.0) * 4.0
        + values["hist_close_position"] * 8.0
    )
    proximity_score = (
        -abs(values["hist_high_gap_20d"]) * 0.7
        - abs(values["hist_ma_gap_20d"]) * 0.2
        + min(values["hist_turnover"], 20.0) * 0.25
        + values["hist_limit_hits_20d"] * 5.0
    )
    reversal_score = (
        -values["hist_return_10d"] * 0.5
        + values["hist_return_1d"] * 0.7
        + values["hist_close_position"] * 10.0
        + min(values["hist_volume_ratio_5d"], 5.0) * 2.0
    )
    liquidity_score = (
        np.log1p(max(current.amount, 0.0))
        + min(values["hist_turnover"], 30.0) * 0.3
        + min(values["hist_volume_ratio_20d"], 5.0) * 2.0
    )
    return values, (
        float(momentum_score),
        float(proximity_score),
        float(reversal_score),
        float(liquidity_score),
    )


def _quick_prefilter_scores(bars: list[_Bar], index: int) -> tuple[float, float, float, float]:
    current = bars[index]
    close5 = bars[index - 5].close if index >= 5 else current.close
    close10 = bars[index - 10].close if index >= 10 else close5
    close20 = bars[index - 20].close if index >= 20 else close10
    ma20 = _safe_mean(
        np.asarray(
            [bar.close for bar in bars[max(0, index - 19) : index + 1]],
            dtype=float,
        )
    )
    return5 = _gap_to(close5, current.close)
    return10 = _gap_to(close10, current.close)
    return20 = _gap_to(close20, current.close)
    prior_volumes = [bar.volume for bar in bars[max(0, index - 5) : index]]
    mean_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0.0
    volume_ratio = current.volume / mean_volume if mean_volume > 0 else 0.0
    recent_high = max(bar.high for bar in bars[max(0, index - 19) : index + 1])
    high_gap = _gap_to(recent_high, current.close)
    prev_close = current.prev_close or (bars[index - 1].close if index else 0.0)
    close_position = (
        (current.close - current.low) / (current.high - current.low)
        if current.high > current.low
        else 0.5
    )
    momentum = return5 * 0.8 + return20 * 0.25 + min(volume_ratio, 5.0) * 4.0 + close_position * 8.0
    proximity = (
        -abs(high_gap) * 0.7
        - abs(_gap_to(ma20, current.close)) * 0.2
        + min(current.turnover, 20.0) * 0.25
    )
    reversal = -return10 * 0.5 + current.change_pct * 0.7 + close_position * 10.0 + min(volume_ratio, 5.0) * 2.0
    liquidity = np.log1p(max(current.amount, 0.0)) + min(current.turnover, 30.0) * 0.3 + min(volume_ratio, 5.0) * 2.0
    return float(momentum), float(proximity), float(reversal), float(liquidity)


def _select_broad_candidates(
    candidates: list[dict],
    *,
    limit: int,
    policy: str = "legacy_union",
) -> list[dict]:
    if policy not in {"legacy_union", "balanced_round_robin"}:
        raise ValueError("unsupported historical candidate policy")
    if limit <= 0:
        return []
    if policy == "balanced_round_robin":
        # Fixed total budget; lane overlaps are filled from that lane's next
        # unique name, rather than summing incomparable raw score units.
        lanes = [sorted(candidates, key=lambda x: (x["scores"][lane], x["code"]), reverse=True)
                 for lane in range(4)]
        cursors = [0] * 4
        selected = {}
        while len(selected) < min(limit, len(candidates)):
            progressed = False
            for lane, ranked in enumerate(lanes):
                while cursors[lane] < len(ranked) and ranked[cursors[lane]]["code"] in selected:
                    cursors[lane] += 1
                if cursors[lane] < len(ranked):
                    item = ranked[cursors[lane]]
                    selected[item["code"]] = item
                    cursors[lane] += 1
                    progressed = True
                if len(selected) >= limit:
                    break
            if not progressed:
                break
        return list(selected.values())
    if len(candidates) <= limit:
        return sorted(candidates, key=lambda item: (item["scores"][0], item["code"]), reverse=True)
    lane_limit = max(limit // 4, 1)
    selected: dict[str, dict] = {}
    for lane in range(4):
        ranked = sorted(
            candidates,
            key=lambda item: (item["scores"][lane], item["code"]),
            reverse=True,
        )
        for item in ranked[:lane_limit]:
            selected[item["code"]] = item
    if len(selected) < limit:
        aggregate = sorted(
            candidates,
            key=lambda item: (sum(item["scores"]), item["code"]),
            reverse=True,
        )
        for item in aggregate:
            selected.setdefault(item["code"], item)
            if len(selected) >= limit:
                break
    return sorted(
        selected.values(),
        key=lambda item: (sum(item["scores"]), item["code"]),
        reverse=True,
    )[:limit]


def _research_outcome_label(current, outcome, *, outcome_day, target_board, label_target):
    """Read-time historical labels only; outcome checks NEVER select candidates."""
    if outcome_day is None:
        return None, "outcome_session_missing"
    cursor = current.trade_date + timedelta(days=1)
    while cursor < outcome_day:
        if cursor.weekday() < 5 and not is_official_closed_day(cursor):
            return None, "outcome_calendar_gap"
        cursor += timedelta(days=1)
    error = formal_outcome_bar_error(current, outcome)
    if error:
        return None, error
    if _is_ambiguous_pre_reform_st_move(outcome):
        return None, "outcome_identity_ambiguous"
    if label_target == "next_day_close_up":
        return int(outcome.close > outcome.prev_close), ""
    current_limit = _is_limit_up(current.change_pct)
    outcome_limit = _is_limit_up(outcome.change_pct)
    return int(outcome_limit and (not current_limit if target_board == 1 else current_limit)), ""


async def build_historical_panel_dataset(
    db: AsyncSession,
    *,
    target_board: int,
    lookback_trade_days: int = 250,
    history_days: int = 60,
    candidate_limit_per_day: int = 450,
    minimum_universe_count: int = 500,
    end_date: date | None = None,
    candidate_policy: str = "legacy_union",
    label_target: str = "promotion",
) -> DatasetBundle:
    """Build a broad, multi-year close→next-session panel from local K-lines."""

    target_board = int(target_board)
    if candidate_policy not in {"legacy_union", "balanced_round_robin"}:
        raise ValueError("unsupported historical candidate policy")
    if label_target not in {"promotion", "next_day_close_up"} or (label_target != "promotion" and target_board != 1):
        raise ValueError("unsupported historical research label target")
    if target_board not in {1, 2}:
        raise ValueError("target_board must be 1 or 2")
    lookback_trade_days = max(int(lookback_trade_days), 80)
    history_days = max(int(history_days), 20)
    candidate_limit_per_day = max(int(candidate_limit_per_day), 50)
    minimum_universe_count = max(int(minimum_universe_count), 1)

    date_statement = select(StockKline.trade_date).distinct()
    if end_date is not None:
        date_statement = date_statement.where(StockKline.trade_date <= end_date)
    requested_date_count = lookback_trade_days + history_days + 2
    descending_dates = [
        row[0]
        for row in (
            await db.execute(
                date_statement.order_by(desc(StockKline.trade_date)).limit(requested_date_count)
            )
        ).all()
        if row[0]
        and row[0].weekday() < 5
        and not is_official_closed_day(row[0])
    ]
    market_dates = sorted(descending_dates)
    if len(market_dates) < history_days + 20:
        raise ValueError("insufficient K-line trade days for historical panel")
    feature_dates = market_dates[history_days:-1]
    if len(feature_dates) > lookback_trade_days:
        feature_dates = feature_dates[-lookback_trade_days:]
    first_query_date = market_dates[0]
    last_query_date = market_dates[-1]
    code_filter = or_(
        *(StockKline.code.like(f"{prefix}%") for prefix in _MAIN_CODE_PREFIXES)
    )
    raw_rows = (
        await db.execute(
            select(
                StockKline.code,
                StockKline.trade_date,
                StockKline.open,
                StockKline.high,
                StockKline.low,
                StockKline.close,
                StockKline.volume,
                StockKline.amount,
                StockKline.turnover,
                StockKline.change_pct,
                StockKline.prev_close,
                StockKline.source,
            ).where(
                StockKline.trade_date >= first_query_date,
                StockKline.trade_date <= last_query_date,
                code_filter,
            )
        )
    ).all()
    by_code: dict[str, list[_Bar]] = defaultdict(list)
    market_changes: dict[date, list[float]] = defaultdict(list)
    market_limit_counts: dict[date, int] = defaultdict(int)
    for row in raw_rows:
        code = str(row[0] or "").strip()
        if not _is_main_code(code):
            continue
        prev_close = _number(row[10])
        close = _number(row[5])
        change = _number(row[9])
        if abs(change) < 1e-9 and prev_close > 0 and close > 0:
            change = (close / prev_close - 1.0) * 100.0
        bar = _Bar(
            code=code,
            trade_date=row[1],
            open=_number(row[2]),
            high=_number(row[3]),
            low=_number(row[4]),
            close=close,
            volume=_number(row[6]),
            amount=_number(row[7]),
            turnover=_number(row[8]),
            change_pct=change,
            prev_close=prev_close,
            source=str(row[11] or ""),
        )
        by_code[code].append(bar)
        market_changes[bar.trade_date].append(change)
        if _is_limit_up(change):
            market_limit_counts[bar.trade_date] += 1
    for bars in by_code.values():
        bars.sort(key=lambda item: item.trade_date)

    market_context = {}
    for trade_day, changes in market_changes.items():
        values = np.asarray(changes, dtype=float)
        market_context[trade_day] = {
            "advance_ratio": float(np.mean(values > 0)) if len(values) else 0.0,
            "limit_up_count": float(market_limit_counts.get(trade_day, 0)),
            "median_return": float(np.median(values)) if len(values) else 0.0,
            "return_dispersion": float(np.std(values)) if len(values) else 0.0,
            "universe_count": float(len(values)),
        }
    ordered_context_dates = sorted(market_context)
    for context_index, trade_day in enumerate(ordered_context_dates):
        previous_context = (
            market_context[ordered_context_dates[context_index - 1]]
            if context_index > 0
            else market_context[trade_day]
        )
        market_context[trade_day]["previous_advance_ratio"] = previous_context[
            "advance_ratio"
        ]
        market_context[trade_day]["previous_limit_up_count"] = previous_context[
            "limit_up_count"
        ]

    incomplete_feature_dates = [
        trade_day
        for trade_day in feature_dates
        if len(market_changes.get(trade_day, [])) < minimum_universe_count
    ]
    incomplete_feature_date_set = set(incomplete_feature_dates)
    feature_dates = [
        trade_day for trade_day in feature_dates if trade_day not in incomplete_feature_date_set
    ]
    if len(feature_dates) < 20:
        raise ValueError(
            "insufficient complete historical-panel dates after universe quality gate"
        )

    fund_rows = (
        await db.execute(
            select(
                FundFlow.code,
                FundFlow.trade_date,
                FundFlow.main_net_inflow,
                FundFlow.main_net_inflow_pct,
            ).where(
                FundFlow.trade_date >= first_query_date,
                FundFlow.trade_date <= last_query_date,
            )
        )
    ).all()
    fund_map = {
        (str(row[0] or "").strip(), row[1]): (_number(row[2]), _number(row[3]))
        for row in fund_rows
        if row[1]
    }

    feature_date_set = set(feature_dates)
    next_market_date = {
        market_dates[index]: market_dates[index + 1]
        for index in range(len(market_dates) - 1)
    }
    candidates_by_date: dict[date, list[dict]] = defaultdict(list)
    universe_positives_by_date: dict[date, int] = defaultdict(int)
    clock_aligned_sample_count = 0
    excluded_ambiguous_st_current_moves = 0
    excluded_ambiguous_st_outcome_moves = 0
    for code, bars in by_code.items():
        index_by_date = {bar.trade_date: index for index, bar in enumerate(bars)}
        for feature_day in feature_dates:
            index = index_by_date.get(feature_day)
            if index is None or index < history_days - 1:
                continue
            current = bars[index]
            if current.close <= 0 or current.volume <= 0:
                continue
            clock_aligned_sample_count += 1
            # Only information at T can decide membership. Missing/bad T+1
            # evidence becomes an unknown label, never a freed candidate slot.
            if _is_ambiguous_pre_reform_st_move(current):
                excluded_ambiguous_st_current_moves += 1
                continue
            current_limit = _is_limit_up(current.change_pct)
            if target_board == 2 and not current_limit:
                continue
            if target_board == 1 and current_limit:
                continue
            outcome_day = next_market_date.get(feature_day)
            outcome_index = index_by_date.get(outcome_day)
            outcome = bars[outcome_index] if outcome_index is not None else None
            label, label_error = _research_outcome_label(
                current, outcome, outcome_day=outcome_day,
                target_board=target_board, label_target=label_target)
            if label_error == "outcome_identity_ambiguous":
                excluded_ambiguous_st_outcome_moves += 1
            if label == 1:
                universe_positives_by_date[feature_day] += 1
            candidates_by_date[feature_day].append(
                {
                    "code": code,
                    "label": label,
                    "label_error": label_error,
                    "bars": bars,
                    "index": index,
                    "scores": _quick_prefilter_scores(bars, index),
                }
            )

    rows: list[FeatureRow] = []
    pool_positive_count = 0
    universe_positive_count = 0
    universe_candidate_count = 0
    reference_positive_count = 0
    reference_sample_count = 0
    day_diagnostics: list[dict] = []
    for feature_day in sorted(feature_date_set):
        candidates = candidates_by_date.get(feature_day, [])
        universe_candidate_count += len(candidates)
        universe_positives = universe_positives_by_date.get(feature_day, 0)
        universe_positive_count += universe_positives
        selected = (
            candidates
            if target_board == 2
            else _select_broad_candidates(
                candidates, limit=candidate_limit_per_day, policy=candidate_policy
            )
        )
        selected_positives = sum(item["label"] == 1 for item in selected)
        selected_known = sum(item["label"] is not None for item in selected)
        pool_positive_count += selected_positives
        # The comparison curve is deliberately prequential: today's labels are
        # not allowed to set today's probability.  The old same-day base rate was
        # an outcome oracle that made Brier/ECE comparisons invalid even though
        # it never entered the challenger feature matrix.
        reference_base_rate = _expanding_reference_base_rate(
            target_board=target_board,
            prior_positive_count=reference_positive_count,
            prior_sample_count=reference_sample_count,
        )
        if label_target == "next_day_close_up":
            reference_base_rate = (reference_positive_count + 10.0) / (reference_sample_count + 20.0)
        day_regime = classify_historical_market_context(
            market_context.get(feature_day, {})
        )
        for rank, item in enumerate(selected, start=1):
            rank_multiplier = (1.4 - 0.8 * ((rank - 1) / max(len(selected) - 1, 1))
                               if label_target == "promotion" else 1.0)
            values, _scores = _historical_values(
                item["bars"],
                item["index"],
                fund_map=fund_map,
                market_context=market_context,
            )
            rows.append(
                FeatureRow(
                    code=item["code"],
                    trade_date=feature_day.isoformat(),
                    target_board=target_board,
                    label=item["label"],
                    baseline_probability=float(
                        min(max(reference_base_rate * rank_multiplier, 1e-4), 0.8)
                    ),
                    candidate_route="historical_broad_prefilter",
                    values=values,
                    market_regime=day_regime,
                )
            )
        day_diagnostics.append(
            {
                "trade_date": feature_day.isoformat(),
                "universe_count": len(candidates),
                "candidate_count": len(selected),
                "universe_positive_count": universe_positives,
                "candidate_positive_count": selected_positives,
                "universe_unknown_count": sum(item["label"] is None for item in candidates),
                "candidate_unknown_count": len(selected) - selected_known,
                "candidate_unknown_reasons": dict(Counter(item["label_error"] for item in selected if item["label"] is None)),
                "unknown_candidates": [{"code": item["code"], "reason": item["label_error"]}
                                       for item in selected if item["label"] is None],
                "reference_base_rate": reference_base_rate,
                "reference_prior_sample_count": reference_sample_count,
            }
        )
        reference_positive_count += selected_positives
        reference_sample_count += selected_known

    digest_header = (
        f"{_HISTORICAL_PANEL_VERSION}|{target_board}|{feature_dates[0]}|"
        f"{feature_dates[-1]}|{len(rows)}|"
        f"{universe_positive_count}|{candidate_limit_per_day}|{candidate_policy}|{label_target}"
    )
    digest_builder = hashlib.sha256(digest_header.encode("utf-8"))
    for row in rows:
        digest_builder.update(
            json.dumps(
                {
                    "code": row.code,
                    "trade_date": row.trade_date,
                    "label": row.label,
                    "baseline_probability": row.baseline_probability,
                    "candidate_route": row.candidate_route,
                    "market_regime": row.market_regime,
                    "values": row.values,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    digest_builder.update(json.dumps(day_diagnostics, sort_keys=True, allow_nan=False).encode())
    digest = digest_builder.hexdigest()[:16]
    universe_unknown_count = sum(day["universe_unknown_count"] for day in day_diagnostics)
    diagnostics = {
        "dataset_source": "historical_kline_panel",
        "label_source": "read_time_formal_close_partial_labels_not_first_knowledge",
        "label_target": label_target,
        "candidate_policy": candidate_policy,
        "candidate_selection_contract": "T_only_selection_then_label_v3",
        "unknown_count": sum(row.label is None for row in rows),
        "universe_unknown_count": universe_unknown_count,
        "historical_first_knowledge_verified": False,
        "manual_review_eligible": False,
        "research_only": True,
        "target_board": target_board,
        "eligible_records": len(rows),
        "positive_count": sum(row.label == 1 for row in rows),
        "negative_count": sum(row.label == 0 for row in rows),
        "trade_day_count": len({row.trade_date for row in rows}),
        "universe_candidate_count": universe_candidate_count,
        "universe_positive_count": universe_positive_count,
        "candidate_positive_count": pool_positive_count,
        "prefilter_recall": (pool_positive_count / universe_positive_count
                             if universe_positive_count else 0.0) if not universe_unknown_count else None,
        "observed_prefilter_recall": (pool_positive_count / universe_positive_count
                                      if universe_positive_count else None),
        "candidate_limit_per_day": candidate_limit_per_day,
        "regime_sample_counts": dict(Counter(row.market_regime for row in rows)),
        "regime_trade_day_counts": {
            regime: len({row.trade_date for row in rows if row.market_regime == regime})
            for regime in sorted({row.market_regime for row in rows})
        },
        "minimum_universe_count": minimum_universe_count,
        "excluded_incomplete_trade_days": len(incomplete_feature_dates),
        "excluded_ambiguous_st_moves": excluded_ambiguous_st_current_moves,
        "excluded_ambiguous_st_current_moves": excluded_ambiguous_st_current_moves,
        "unknown_ambiguous_st_outcome_moves": excluded_ambiguous_st_outcome_moves,
        "clock_aligned_sample_count": clock_aligned_sample_count,
        "ambiguous_st_exclusion_rate": (
            (
                excluded_ambiguous_st_current_moves
            )
            / clock_aligned_sample_count
            if clock_aligned_sample_count
            else 0.0
        ),
        "point_in_time_contract": "features use bars/fund flow at or before feature date only",
        "reference_probability": (
            "expanding_prior_broad_prefilter_rank_heuristic_not_production_champion"
            if label_target == "promotion" else "expanding_close_up_beta_prior_not_production_champion"
        ),
        "reference_probability_contract": (
            "each day uses only earlier selected outcomes plus a fixed beta prior"
        ),
        "daily": day_diagnostics,
    }
    return DatasetBundle(
        rows=rows,
        diagnostics=diagnostics,
        data_version=f"historical_panel_v3_{label_target}_{len(rows)}_{digest}",
        start_date=(date.fromisoformat(rows[0].trade_date) if rows else None),
        end_date=(date.fromisoformat(rows[-1].trade_date) if rows else None),
    )
