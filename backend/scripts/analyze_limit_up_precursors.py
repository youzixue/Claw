#!/usr/bin/env python3
"""无未来函数地研究主板首板启动前的消息、资金、K线与主营行业形态。

示例：
    python3 scripts/analyze_limit_up_precursors.py \
      --db claw.db --start-date 2026-04-06 --end-date 2026-08-28 \
      --control-ratio 5 --output-prefix outputs/limit_up_precursors_20260828

脚本以 T-1 已完成日K/资金/板块数据预测 T 日结果；上一收盘消息截止 T-1
20:00，隔夜消息单独截止 T 09:25。SQLite 以只读模式打开，不修改业务库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from math import log
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.news.catalyst import classify_news_event
from app.signal.launch_precursors import (
    FEATURE_VERSION,
    build_funding_preheat_context,
    build_primary_industry_evidence,
    build_stock_launch_profile,
)


MAIN_BOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
POSITIVE_SENTIMENTS = {"positive", "bullish"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _main_board(code: str) -> bool:
    normalized = str(code or "").strip()
    return len(normalized) == 6 and normalized.startswith(MAIN_BOARD_PREFIXES)


def _chunks(values: list[str], size: int = 700) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" for _ in values)


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _iso_datetime(value: str | datetime | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _trade_dates(connection: sqlite3.Connection, start: date, end: date) -> list[date]:
    return [
        _parse_date(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT trade_date
            FROM stock_kline
            WHERE trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            (str(start), str(end)),
        )
    ]


def _known_st_codes(connection: sqlite3.Connection) -> set[str]:
    codes: set[str] = set()
    try:
        rows = connection.execute(
            "SELECT code FROM stock_tags WHERE COALESCE(is_st, 0) = 1"
        )
        codes.update(str(row[0] or "").strip() for row in rows)
    except sqlite3.OperationalError:
        pass
    rows = connection.execute(
        "SELECT DISTINCT code FROM limit_up_pool WHERE UPPER(COALESCE(name, '')) LIKE '%ST%'"
    )
    codes.update(str(row[0] or "").strip() for row in rows)
    return {code for code in codes if code}


def _event_day_outcomes(connection: sqlite3.Connection, event_date: date) -> dict[str, dict]:
    outcomes: dict[str, dict] = {}
    for row in connection.execute(
        """
        SELECT code, change_pct, high, prev_close, close, turnover
        FROM stock_kline
        WHERE trade_date = ?
        """,
        (str(event_date),),
    ):
        code = str(row[0] or "").strip()
        if not _main_board(code):
            continue
        change_pct = _float(row[1])
        high_change_pct = (
            (_float(row[2]) / _float(row[3]) - 1.0) * 100.0
            if _float(row[2]) > 0 and _float(row[3]) > 0
            else change_pct
        )
        outcomes[code] = {
            "event_change_pct": round(change_pct, 3),
            "event_high_change_pct": round(high_change_pct, 3),
            "event_rise": change_pct > 0.0,
            "event_strong_rise": max(change_pct, high_change_pct) >= 5.0,
        }
    return outcomes


def _strict_first_board_codes(
    connection: sqlite3.Connection,
    event_date: date,
    previous_date: date,
    st_codes: set[str],
) -> tuple[set[str], set[str]]:
    # previous_date 保留在公开函数签名中，便于旧调用方兼容；首板真值必须
    # 使用涨停池权威连板字段，不能再用“前一日未涨停”近似（断板反包会被
    # 错标为首板）。
    _ = previous_date
    current_rows = list(
        connection.execute(
            "SELECT code, name, consecutive_days "
            "FROM limit_up_pool WHERE trade_date = ?",
            (str(event_date),),
        )
    )
    all_limit_up: set[str] = set()
    first_board: set[str] = set()
    for code_value, name_value, consecutive_days_value in current_rows:
        code = str(code_value or "").strip()
        name = str(name_value or "").upper().replace(" ", "")
        if not _main_board(code) or code in st_codes or "ST" in name:
            continue
        all_limit_up.add(code)
        try:
            consecutive_days = int(consecutive_days_value or 1)
        except (TypeError, ValueError):
            consecutive_days = 1
        if consecutive_days == 1:
            first_board.add(code)
    return first_board, all_limit_up


def _deterministic_controls(
    universe: Iterable[str],
    *,
    event_date: date,
    excluded: set[str],
    count: int,
) -> list[str]:
    candidates = [code for code in universe if code not in excluded and _main_board(code)]
    candidates.sort(
        key=lambda code: hashlib.sha1(f"{event_date}:{code}".encode("utf-8")).hexdigest()
    )
    return candidates[:count]


def _load_kline_history(
    connection: sqlite3.Connection,
    codes: list[str],
    as_of_date: date,
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    start = as_of_date - timedelta(days=220)
    for chunk in _chunks(codes):
        sql = f"""
            SELECT code, trade_date, open, close, high, low, volume, turnover, change_pct
            FROM stock_kline
            WHERE code IN ({_placeholders(chunk)})
              AND trade_date BETWEEN ? AND ?
            ORDER BY code, trade_date
        """
        params = [*chunk, str(start), str(as_of_date)]
        for row in connection.execute(sql, params):
            grouped[str(row[0] or "")].append(
                {
                    "trade_date": _parse_date(row[1]),
                    "open": _float(row[2]),
                    "close": _float(row[3]),
                    "high": _float(row[4]),
                    "low": _float(row[5]),
                    "volume": _float(row[6]),
                    "turnover": _float(row[7]),
                    "change_pct": _float(row[8]),
                }
            )
    return {code: rows[-120:] for code, rows in grouped.items()}


def _load_funding(
    connection: sqlite3.Connection,
    codes: list[str],
    as_of_date: date,
) -> dict[str, dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    start = as_of_date - timedelta(days=14)
    for chunk in _chunks(codes):
        sql = f"""
            SELECT code, trade_date, main_net_inflow, main_net_inflow_pct
            FROM fund_flow
            WHERE code IN ({_placeholders(chunk)})
              AND trade_date BETWEEN ? AND ?
            ORDER BY code, trade_date
        """
        for row in connection.execute(sql, [*chunk, str(start), str(as_of_date)]):
            grouped[str(row[0] or "")].append(
                {
                    "trade_date": _parse_date(row[1]),
                    "main_net_inflow": _float(row[2]),
                    "main_net_inflow_pct": _float(row[3]),
                }
            )
    return {
        code: build_funding_preheat_context(rows)
        for code, rows in grouped.items()
    }


def _sector_rotation_context(rows: list[sqlite3.Row], as_of_date: date) -> dict:
    current_rows = [row for row in rows if _parse_date(row[1]) == as_of_date]
    if not current_rows:
        return {}
    current = max(current_rows, key=lambda row: (_float(row[2]), _int(row[3]), _float(row[4])))
    previous = sorted(
        [row for row in rows if _parse_date(row[1]) < as_of_date],
        key=lambda row: _parse_date(row[1]),
    )
    strengths = [_float(row[2]) for row in previous]
    breadths = [_int(row[3]) for row in previous]
    flows = [_float(row[4]) for row in previous]
    current_strength = _float(current[2])
    current_breadth = _int(current[3])
    current_flow = _float(current[4])
    current_days = _int(current[6])
    avg_strength = sum(strengths) / len(strengths) if strengths else 0.0
    avg_breadth = sum(breadths) / len(breadths) if breadths else 0.0
    avg_flow = sum(flows) / len(flows) if flows else 0.0
    strength_delta = current_strength - avg_strength if previous else current_strength
    breadth_delta = current_breadth - avg_breadth if previous else float(current_breadth)
    flow_delta = current_flow - avg_flow if previous else current_flow
    has_group_confirmation = current_breadth >= 2 or (
        current_breadth == 1
        and current_strength >= 48.0
        and strength_delta >= 18.0
        and flow_delta >= 2.0
    )
    low_rotation = bool(
        len(previous) >= 2
        and current_strength >= 36.0
        and current_breadth >= 1
        and has_group_confirmation
        and (
            strength_delta >= 10.0
            or breadth_delta >= 2.0
            or (current_breadth >= 3 and avg_breadth <= 1.2)
        )
        and max(strengths or [0.0]) <= max(72.0, current_strength + 18.0)
        and current_days <= 3
    )
    rotation_score = max(
        0.0,
        min(
            100.0,
            current_strength * 0.45
            + max(strength_delta, 0.0) * 0.8
            + max(breadth_delta, 0.0) * 5.0
            + (8.0 if current_flow > 0 else 0.0)
            + (8.0 if low_rotation else 0.0),
        ),
    )
    return {
        "strength_score": current_strength,
        "limit_up_count": current_breadth,
        "fund_flow": current_flow,
        "change_pct": _float(current[5]),
        "consecutive_days": current_days,
        "sector_strength_delta": round(strength_delta, 2),
        "sector_limit_up_delta": round(breadth_delta, 2),
        "sector_fund_flow_delta": round(flow_delta, 2),
        "sector_rotation_score": round(rotation_score, 2),
        "sector_low_position_rotation": low_rotation,
    }


def _load_primary_industries(
    connection: sqlite3.Connection,
    codes: list[str],
    as_of_date: date,
) -> dict[str, dict]:
    mappings: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for chunk in _chunks(codes):
        sql = f"""
            SELECT code, sector_code, sector_name
            FROM stock_sector_mapping
            WHERE code IN ({_placeholders(chunk)})
              AND sector_type = 'industry'
        """
        for row in connection.execute(sql, chunk):
            mappings[str(row[0] or "")].append((str(row[1] or ""), str(row[2] or "")))
    sector_codes = sorted({item[0] for values in mappings.values() for item in values if item[0]})
    if not sector_codes:
        return {}
    history: dict[str, list[sqlite3.Row]] = defaultdict(list)
    start = as_of_date - timedelta(days=14)
    for chunk in _chunks(sector_codes):
        sql = f"""
            SELECT sector_code, trade_date, strength_score, limit_up_count,
                   fund_flow, change_pct, consecutive_days
            FROM sector_persistence
            WHERE sector_code IN ({_placeholders(chunk)})
              AND trade_date BETWEEN ? AND ?
            ORDER BY sector_code, trade_date
        """
        for row in connection.execute(sql, [*chunk, str(start), str(as_of_date)]):
            history[str(row[0] or "")].append(row)
    lifecycle: dict[str, str] = {}
    try:
        for chunk in _chunks(sector_codes):
            sql = f"""
                SELECT sector_code, lifecycle_state
                FROM sector_lifecycle
                WHERE sector_code IN ({_placeholders(chunk)}) AND trade_date = ?
            """
            for row in connection.execute(sql, [*chunk, str(as_of_date)]):
                lifecycle[str(row[0] or "")] = str(row[1] or "")
    except sqlite3.OperationalError:
        pass

    result: dict[str, dict] = {}
    for code, code_mappings in mappings.items():
        candidates: list[dict] = []
        for sector_code, sector_name in code_mappings:
            context = _sector_rotation_context(history.get(sector_code, []), as_of_date)
            if not context:
                continue
            context.update(
                {
                    "sector_code": sector_code,
                    "sector_name": sector_name,
                    "sector_type": "industry",
                    "lifecycle_state": lifecycle.get(sector_code, ""),
                }
            )
            candidates.append(context)
        if candidates:
            result[code] = max(
                candidates,
                key=lambda item: (
                    bool(item.get("sector_low_position_rotation")),
                    _float(item.get("strength_score")),
                    _int(item.get("limit_up_count")),
                    _float(item.get("sector_rotation_score")),
                ),
            )
    return result


def _news_context(
    connection: sqlite3.Connection,
    codes: list[str],
    previous_date: date,
    event_date: date,
) -> dict[str, dict]:
    code_set = set(codes)
    close_cutoff = datetime.combine(previous_date, time(hour=20))
    overnight_cutoff = datetime.combine(event_date, time(hour=9, minute=25))
    start = close_cutoff - timedelta(hours=72)
    result: dict[str, dict] = defaultdict(
        lambda: {
            "direct_news_72h": False,
            "direct_bullish_news_72h": False,
            "direct_high_impact_news_72h": False,
            "direct_hard_news_72h": False,
            "direct_news_count_72h": 0,
            "overnight_direct_news": False,
            "overnight_direct_bullish_news": False,
        }
    )
    rows = connection.execute(
        """
        SELECT title, publish_time, importance, bull_bear, sentiment,
               impact_scope, related_codes
        FROM finance_news
        WHERE publish_time >= ? AND publish_time < ?
        ORDER BY publish_time
        """,
        (start.isoformat(sep=" "), overnight_cutoff.isoformat(sep=" ")),
    )
    for row in rows:
        publish_time = _iso_datetime(row[1])
        if publish_time is None:
            continue
        related = {
            str(item or "").strip().zfill(6)
            for item in _json_list(row[6])
            if str(item or "").strip()
        }
        matched = related & code_set
        if not matched:
            continue
        grade, _event_type, _adjustment = classify_news_event(str(row[0] or ""))
        bullish = (
            str(row[3] or "").lower() == "bull"
            or str(row[4] or "").lower() in POSITIVE_SENTIMENTS
        ) and grade not in {"risk", "routine"}
        high_impact = bool(
            str(row[5] or "") == "stock"
            and (_int(row[2]) >= 6 or grade == "hard")
            and bullish
        )
        for code in matched:
            item = result[code]
            if publish_time < close_cutoff:
                item["direct_news_72h"] = True
                item["direct_news_count_72h"] += 1
                item["direct_bullish_news_72h"] |= bullish
                item["direct_high_impact_news_72h"] |= high_impact
                item["direct_hard_news_72h"] |= grade == "hard" and bullish
            else:
                item["overnight_direct_news"] = True
                item["overnight_direct_bullish_news"] |= bullish
    return dict(result)


def _feature_row(
    *,
    code: str,
    event_date: date,
    as_of_date: date,
    first_board: bool,
    outcome: dict,
    bars: list[dict],
    funding: dict,
    industry: dict,
    news: dict,
) -> dict:
    stock = build_stock_launch_profile(bars)
    industry_evidence = build_primary_industry_evidence(industry)
    row = {
        "code": code,
        "event_date": str(event_date),
        "as_of_date": str(as_of_date),
        "first_board": first_board,
        **outcome,
        **stock,
        **funding,
        **industry_evidence,
        **news,
    }
    row["low_position_42"] = _float(stock.get("launch_position_120"), 1.0) <= 0.42
    row["low_position_55"] = _float(stock.get("launch_position_120"), 1.0) <= 0.55
    row["industry_strength_55"] = _float(
        industry_evidence.get("primary_industry_strength")
    ) >= 55.0
    row["industry_breadth_2"] = _int(
        industry_evidence.get("primary_industry_limit_up_count")
    ) >= 2
    row["low_industry_active_combo"] = bool(
        stock.get("launch_mid_low_repair")
        and stock.get("launch_active_volume_turnover")
        and industry_evidence.get("primary_industry_ignition_ready")
    )
    row["low_industry_active_funding_combo"] = bool(
        row["low_industry_active_combo"]
        and funding.get("funding_preheat_ready")
    )
    row["direct_repeated_news_72h"] = _int(news.get("direct_news_count_72h")) >= 3
    return row


FEATURES = [
    "low_position_42",
    "low_position_55",
    "launch_mid_low_repair",
    "launch_active_volume_turnover",
    "launch_profile_ready",
    "funding_preheat_ready",
    "funding_preheat_supportive",
    "funding_persistent_outflow_risk",
    "industry_strength_55",
    "industry_breadth_2",
    "primary_industry_low_position_rotation",
    "primary_industry_ignition_ready",
    "low_industry_active_combo",
    "low_industry_active_funding_combo",
    "direct_news_72h",
    "direct_bullish_news_72h",
    "direct_high_impact_news_72h",
    "direct_hard_news_72h",
    "direct_repeated_news_72h",
    "overnight_direct_news",
    "overnight_direct_bullish_news",
]


def _rate(numerator: int | float, denominator: int | float) -> float:
    return numerator / denominator if denominator else 0.0


def _summarize(samples: list[dict]) -> dict:
    positives = [row for row in samples if row.get("first_board")]
    controls = [row for row in samples if not row.get("first_board")]
    baseline_rise = _rate(sum(bool(row.get("event_rise")) for row in samples), len(samples))
    baseline_strong = _rate(
        sum(bool(row.get("event_strong_rise")) for row in samples), len(samples)
    )
    summaries: list[dict] = []
    for feature in FEATURES:
        present = [row for row in samples if bool(row.get(feature))]
        positive_prevalence = _rate(
            sum(bool(row.get(feature)) for row in positives), len(positives)
        )
        control_prevalence = _rate(
            sum(bool(row.get(feature)) for row in controls), len(controls)
        )
        rise_rate = _rate(sum(bool(row.get("event_rise")) for row in present), len(present))
        strong_rate = _rate(
            sum(bool(row.get("event_strong_rise")) for row in present), len(present)
        )
        summaries.append(
            {
                "feature": feature,
                "sample_count": len(present),
                "coverage": round(_rate(len(present), len(samples)), 4),
                "positive_prevalence": round(positive_prevalence, 4),
                "control_prevalence": round(control_prevalence, 4),
                "first_board_likelihood_ratio": round(
                    positive_prevalence / control_prevalence, 3
                )
                if control_prevalence > 0
                else None,
                "rise_rate": round(rise_rate, 4),
                "rise_lift": round(rise_rate / baseline_rise, 3)
                if baseline_rise > 0
                else None,
                "strong_rise_rate": round(strong_rate, 4),
                "strong_rise_lift": round(strong_rate / baseline_strong, 3)
                if baseline_strong > 0
                else None,
            }
        )
    return {
        "sample_count": len(samples),
        "positive_count": len(positives),
        "control_count": len(controls),
        "sample_rise_rate": round(baseline_rise, 4),
        "sample_strong_rise_rate": round(baseline_strong, 4),
        "features": summaries,
    }


CLOSE_BACKTEST_FEATURES = (
    "low_position_42",
    "launch_mid_low_repair",
    "launch_active_volume_turnover",
    "launch_profile_ready",
    "funding_preheat_ready",
    "funding_persistent_outflow_risk",
    "primary_industry_ignition_ready",
    "low_industry_active_combo",
    "low_industry_active_funding_combo",
    "direct_news_72h",
    "direct_high_impact_news_72h",
    "direct_repeated_news_72h",
)
PREOPEN_BACKTEST_FEATURES = (
    *CLOSE_BACKTEST_FEATURES,
    "overnight_direct_news",
    "overnight_direct_bullish_news",
)


def _learn_feature_log_lifts(
    samples: list[dict],
    features: Iterable[str],
    *,
    outcome_key: str = "first_board",
) -> dict[str, float]:
    positives = [row for row in samples if bool(row.get(outcome_key))]
    controls = [row for row in samples if not bool(row.get(outcome_key))]
    if not positives or not controls:
        return {}
    weights: dict[str, float] = {}
    for feature in features:
        positive_rate = (
            sum(bool(row.get(feature)) for row in positives) + 1.0
        ) / (len(positives) + 2.0)
        control_rate = (
            sum(bool(row.get(feature)) for row in controls) + 1.0
        ) / (len(controls) + 2.0)
        raw_weight = log(max(positive_rate, 1e-9) / max(control_rate, 1e-9))
        weights[feature] = round(max(-1.25, min(raw_weight, 1.50)), 4)
    return weights


def _weighted_feature_score(row: dict, weights: dict[str, float]) -> float:
    return sum(weight for feature, weight in weights.items() if bool(row.get(feature)))


def _binary_auc(scored_rows: list[tuple[float, bool]]) -> float | None:
    positive_count = sum(label for _score, label in scored_rows)
    negative_count = len(scored_rows) - positive_count
    if not positive_count or not negative_count:
        return None
    ordered = sorted(scored_rows, key=lambda item: item[0])
    positive_rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        positive_rank_sum += average_rank * sum(
            label for _score, label in ordered[index:end]
        )
        index = end
    auc = (
        positive_rank_sum - positive_count * (positive_count + 1) / 2.0
    ) / (positive_count * negative_count)
    return round(auc, 4)


def _evaluate_rank_score(
    samples: list[dict],
    *,
    score_name: str,
    score_fn,
    score_target: str = "first_board",
) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    scored_rows: list[tuple[float, bool]] = []
    nonzero_count = 0
    for row in samples:
        score = float(score_fn(row))
        scored_rows.append((score, bool(row.get(score_target))))
        nonzero_count += score != 0.0
        grouped[str(row.get("event_date") or "")].append({**row, "_score": score})

    top_quintile: list[dict] = []
    matched_size_top: list[dict] = []
    for rows in grouped.values():
        ranked = sorted(
            rows,
            key=lambda row: (-float(row.get("_score") or 0.0), str(row.get("code") or "")),
        )
        top_count = max(1, (len(ranked) + 4) // 5)
        top_quintile.extend(ranked[:top_count])
        positive_count = sum(bool(row.get("first_board")) for row in ranked)
        matched_size_top.extend(ranked[:positive_count])

    def outcome_rate(rows: list[dict], key: str) -> float:
        return _rate(sum(bool(row.get(key)) for row in rows), len(rows))

    baseline_first_board = outcome_rate(samples, "first_board")
    baseline_rise = outcome_rate(samples, "event_rise")
    baseline_strong = outcome_rate(samples, "event_strong_rise")
    top_first_board = outcome_rate(top_quintile, "first_board")
    top_rise = outcome_rate(top_quintile, "event_rise")
    top_strong = outcome_rate(top_quintile, "event_strong_rise")
    target_baseline = outcome_rate(samples, score_target)
    target_top_rate = outcome_rate(top_quintile, score_target)
    matched_precision = outcome_rate(matched_size_top, "first_board")
    return {
        "score": score_name,
        "score_target": score_target,
        "sample_count": len(samples),
        "date_count": len(grouped),
        "nonzero_score_coverage": round(_rate(nonzero_count, len(samples)), 4),
        "roc_auc": _binary_auc(scored_rows),
        "target_baseline_rate": round(target_baseline, 4),
        "target_top_quintile_rate": round(target_top_rate, 4),
        "target_top_quintile_lift": round(_rate(target_top_rate, target_baseline), 3),
        "baseline_first_board_rate": round(baseline_first_board, 4),
        "top_quintile_count": len(top_quintile),
        "top_quintile_first_board_rate": round(top_first_board, 4),
        "top_quintile_first_board_lift": round(
            _rate(top_first_board, baseline_first_board), 3
        ),
        "matched_size_precision": round(matched_precision, 4),
        "matched_size_recall": round(
            _rate(
                sum(bool(row.get("first_board")) for row in matched_size_top),
                sum(bool(row.get("first_board")) for row in samples),
            ),
            4,
        ),
        "baseline_rise_rate": round(baseline_rise, 4),
        "top_quintile_rise_rate": round(top_rise, 4),
        "top_quintile_rise_lift": round(_rate(top_rise, baseline_rise), 3),
        "baseline_strong_rise_rate": round(baseline_strong, 4),
        "top_quintile_strong_rise_rate": round(top_strong, 4),
        "top_quintile_strong_rise_lift": round(_rate(top_strong, baseline_strong), 3),
    }


def _temporal_holdout_backtest(samples: list[dict]) -> dict:
    event_dates = sorted({str(row.get("event_date") or "") for row in samples if row.get("event_date")})
    if len(event_dates) < 4:
        return {"status": "insufficient_dates", "date_count": len(event_dates)}
    split_index = max(2, min(int(len(event_dates) * 0.70), len(event_dates) - 1))
    train_dates = set(event_dates[:split_index])
    holdout_dates = set(event_dates[split_index:])
    training = [row for row in samples if str(row.get("event_date") or "") in train_dates]
    holdout = [row for row in samples if str(row.get("event_date") or "") in holdout_dates]
    close_weights = _learn_feature_log_lifts(training, CLOSE_BACKTEST_FEATURES)
    preopen_weights = _learn_feature_log_lifts(training, PREOPEN_BACKTEST_FEATURES)
    direction_weights = _learn_feature_log_lifts(
        training,
        CLOSE_BACKTEST_FEATURES,
        outcome_key="event_rise",
    )
    preopen_direction_weights = _learn_feature_log_lifts(
        training,
        PREOPEN_BACKTEST_FEATURES,
        outcome_key="event_rise",
    )
    strong_rise_weights = _learn_feature_log_lifts(
        training,
        CLOSE_BACKTEST_FEATURES,
        outcome_key="event_strong_rise",
    )
    methods = [
        _evaluate_rank_score(
            holdout,
            score_name="raw_low_position",
            score_fn=lambda row: 1.0 if bool(row.get("low_position_42")) else 0.0,
        ),
        _evaluate_rank_score(
            holdout,
            score_name="previous_close_launch_precursors",
            score_fn=lambda row: _weighted_feature_score(row, close_weights),
        ),
        _evaluate_rank_score(
            holdout,
            score_name="preopen_with_overnight_news",
            score_fn=lambda row: _weighted_feature_score(row, preopen_weights),
        ),
        _evaluate_rank_score(
            holdout,
            score_name="previous_close_direction_precursors",
            score_fn=lambda row: _weighted_feature_score(row, direction_weights),
            score_target="event_rise",
        ),
        _evaluate_rank_score(
            holdout,
            score_name="preopen_direction_with_overnight_news",
            score_fn=lambda row: _weighted_feature_score(row, preopen_direction_weights),
            score_target="event_rise",
        ),
        _evaluate_rank_score(
            holdout,
            score_name="previous_close_strong_rise_precursors",
            score_fn=lambda row: _weighted_feature_score(row, strong_rise_weights),
            score_target="event_strong_rise",
        ),
    ]
    return {
        "status": "ok",
        "split_policy": "first 70% event dates train feature log-lifts; last 30% untouched holdout",
        "universe_note": "date-matched strict first boards plus deterministic non-limit-up controls",
        "training_start_date": event_dates[0],
        "training_end_date": event_dates[split_index - 1],
        "training_date_count": len(train_dates),
        "training_sample_count": len(training),
        "holdout_start_date": event_dates[split_index],
        "holdout_end_date": event_dates[-1],
        "holdout_date_count": len(holdout_dates),
        "holdout_sample_count": len(holdout),
        "previous_close_feature_weights": close_weights,
        "preopen_feature_weights": preopen_weights,
        "previous_close_direction_feature_weights": direction_weights,
        "preopen_direction_feature_weights": preopen_direction_weights,
        "previous_close_strong_rise_feature_weights": strong_rise_weights,
        "methods": methods,
    }


def analyze(
    connection: sqlite3.Connection,
    *,
    start_date: date,
    end_date: date,
    control_ratio: int,
    max_dates: int = 0,
) -> tuple[dict, list[dict]]:
    dates = _trade_dates(connection, start_date, end_date)
    if max_dates > 0:
        dates = dates[-max_dates:]
    st_codes = _known_st_codes(connection)
    samples: list[dict] = []
    date_stats: list[dict] = []
    for index, event_date in enumerate(dates):
        if index == 0:
            previous_value = connection.execute(
                "SELECT MAX(trade_date) FROM stock_kline WHERE trade_date < ?",
                (str(event_date),),
            ).fetchone()[0]
            if not previous_value:
                continue
            previous_date = _parse_date(previous_value)
        else:
            previous_date = dates[index - 1]
        outcomes = _event_day_outcomes(connection, event_date)
        first_board, all_limit_up = _strict_first_board_codes(
            connection, event_date, previous_date, st_codes
        )
        first_board &= outcomes.keys()
        if not first_board:
            continue
        controls = _deterministic_controls(
            outcomes.keys(),
            event_date=event_date,
            excluded=all_limit_up | st_codes,
            count=max(len(first_board) * control_ratio * 2, len(first_board) * control_ratio),
        )
        candidate_codes = sorted(first_board) + controls
        history = _load_kline_history(connection, candidate_codes, previous_date)
        eligible_positives = [code for code in sorted(first_board) if len(history.get(code, [])) >= 61]
        eligible_controls = [code for code in controls if len(history.get(code, [])) >= 61][
            : len(eligible_positives) * control_ratio
        ]
        selected = eligible_positives + eligible_controls
        if not selected:
            continue
        funding_map = _load_funding(connection, selected, previous_date)
        industry_map = _load_primary_industries(connection, selected, previous_date)
        news_map = _news_context(connection, selected, previous_date, event_date)
        empty_funding = build_funding_preheat_context([])
        for code in selected:
            samples.append(
                _feature_row(
                    code=code,
                    event_date=event_date,
                    as_of_date=previous_date,
                    first_board=code in first_board,
                    outcome=outcomes[code],
                    bars=history.get(code, []),
                    funding=funding_map.get(code, empty_funding),
                    industry=industry_map.get(code, {}),
                    news=news_map.get(code, {}),
                )
            )
        date_stats.append(
            {
                "event_date": str(event_date),
                "as_of_date": str(previous_date),
                "first_board_count": len(eligible_positives),
                "control_count": len(eligible_controls),
            }
        )

    summary = _summarize(samples)
    report = {
        "feature_version": FEATURE_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "study_start_date": str(start_date),
        "study_end_date": str(end_date),
        "trade_date_count": len(date_stats),
        "control_ratio": control_ratio,
        "asof_policy": {
            "kline": "T-1 completed daily bars only",
            "fund_flow": "trade_date <= T-1; last 3/5 available rows",
            "sector": "primary industry persistence/lifecycle <= T-1",
            "previous_close_news": "publish_time < T-1 20:00; 72-hour lookback",
            "overnight_news": "T-1 20:00 <= publish_time < T 09:25; separate feature",
            "outcome": "T daily close/high; never used in features",
        },
        "sample_definition": {
            "positive": "main-board non-ST limit-up at T with limit_up_pool.consecutive_days == 1",
            "control": "deterministic date-matched main-board non-limit-up stocks",
            "minimum_history_bars": 61,
        },
        "coverage": {
            "funding_3d_complete": sum(
                bool(row.get("funding_coverage_complete_3d")) for row in samples
            ),
            "primary_industry_present": sum(
                bool(row.get("primary_industry_code")) for row in samples
            ),
            "direct_news_present": sum(bool(row.get("direct_news_72h")) for row in samples),
        },
        "date_stats": date_stats,
        "summary": summary,
        "temporal_holdout_backtest": _temporal_holdout_backtest(samples),
    }
    return report, samples


def _markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# 涨停启动前特征研究",
        "",
        f"- 特征版本：`{report['feature_version']}`",
        f"- 区间：{report['study_start_date']} → {report['study_end_date']}",
        f"- 交易日：{report['trade_date_count']}",
        f"- 样本：{summary['sample_count']}（首板 {summary['positive_count']} / 对照 {summary['control_count']}）",
        f"- 匹配样本上涨率：{summary['sample_rise_rate']:.1%}",
        f"- 匹配样本强上涨率：{summary['sample_strong_rise_rate']:.1%}",
        "",
        "## 无未来函数口径",
        "",
        *[f"- {key}: {value}" for key, value in report["asof_policy"].items()],
        "",
        "## 特征结果",
        "",
        "| 特征 | 样本 | 覆盖 | 首板正/负覆盖 | LR | 上涨率/lift | 强涨率/lift |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["features"]:
        lr = "--" if item["first_board_likelihood_ratio"] is None else f"{item['first_board_likelihood_ratio']:.3f}"
        rise_lift = "--" if item["rise_lift"] is None else f"{item['rise_lift']:.3f}"
        strong_lift = "--" if item["strong_rise_lift"] is None else f"{item['strong_rise_lift']:.3f}"
        lines.append(
            f"| `{item['feature']}` | {item['sample_count']} | {item['coverage']:.1%} | "
            f"{item['positive_prevalence']:.1%}/{item['control_prevalence']:.1%} | {lr} | "
            f"{item['rise_rate']:.1%}/{rise_lift} | {item['strong_rise_rate']:.1%}/{strong_lift} |"
        )

    holdout = report.get("temporal_holdout_backtest") or {}
    if holdout.get("status") == "ok":
        lines.extend(
            [
                "",
                "## 时间外留出回测",
                "",
                f"- 训练：{holdout['training_start_date']} → {holdout['training_end_date']}，"
                f"{holdout['training_date_count']} 日 / {holdout['training_sample_count']} 样本",
                f"- 留出：{holdout['holdout_start_date']} → {holdout['holdout_end_date']}，"
                f"{holdout['holdout_date_count']} 日 / {holdout['holdout_sample_count']} 样本",
                "- 特征权重只在前70%日期学习，后30%日期不参与训练；留出宇宙仍是日期匹配首板+对照，不等同真实全市场基准率。",
                "",
                "| 排序方法 | 训练目标/AUC | Top20%目标率/lift | Top20%首板率/lift | Top20%上涨率/lift | Top20%强涨率/lift |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        target_labels = {
            "first_board": "首板",
            "event_rise": "收涨",
            "event_strong_rise": "强涨",
        }
        for item in holdout.get("methods") or []:
            auc_text = "--" if item.get("roc_auc") is None else f"{item['roc_auc']:.3f}"
            target_label = target_labels.get(item.get("score_target"), str(item.get("score_target") or "--"))
            lines.append(
                f"| `{item['score']}` | {target_label}/{auc_text} | "
                f"{item['target_top_quintile_rate']:.1%}/{item['target_top_quintile_lift']:.3f} | "
                f"{item['top_quintile_first_board_rate']:.1%}/{item['top_quintile_first_board_lift']:.3f} | "
                f"{item['top_quintile_rise_rate']:.1%}/{item['top_quintile_rise_lift']:.3f} | "
                f"{item['top_quintile_strong_rise_rate']:.1%}/{item['top_quintile_strong_rise_lift']:.3f} |"
            )
        lines.extend(
            [
                "",
                "### 上一收盘首板训练权重（log lift，已封顶）",
                "",
                "| 特征 | 权重 |",
                "|---|---:|",
            ]
        )
        for feature, weight in holdout.get("previous_close_feature_weights", {}).items():
            lines.append(f"| `{feature}` | {weight:+.4f} |")
        for title, weight_key in (
            ("上一收盘普通收涨训练权重", "previous_close_direction_feature_weights"),
            ("上一收盘强涨训练权重", "previous_close_strong_rise_feature_weights"),
        ):
            lines.extend(
                [
                    "",
                    f"### {title}（log lift，已封顶）",
                    "",
                    "| 特征 | 权重 |",
                    "|---|---:|",
                ]
            )
            for feature, weight in holdout.get(weight_key, {}).items():
                lines.append(f"| `{feature}` | {weight:+.4f} |")

    lines.extend(
        [
            "",
            "## 使用约束",
            "",
            "- 绝对低位不是正向信号；只使用“中低位已修复 + 活跃量价 + 主营行业点火”的组合。",
            "- 首板、普通收涨和强涨分别训练/校准；三日资金只做对应目标的有界微调，单日净流入不得直接生成买点。",
            "- 直接个股消息与板块扩散消息分开；隔夜消息不得进入上一收盘回放。",
            "- 所有组合均为预测证据，交易执行继续受竞价完整性、盘口承接和风控闸门约束。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="claw.db", help="SQLite 数据库路径")
    parser.add_argument("--start-date", default="2026-04-06")
    parser.add_argument("--end-date", default=str(date.today()))
    parser.add_argument("--control-ratio", type=int, default=5)
    parser.add_argument("--max-dates", type=int, default=0, help="仅研究最近N个交易日，0为全量")
    parser.add_argument(
        "--output-prefix",
        default="outputs/limit_up_precursors",
        help="输出前缀（生成 .json/.md）",
    )
    parser.add_argument("--include-samples", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"database not found: {db_path}")
    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    if start_date > end_date:
        parser.error("start-date must be <= end-date")
    if args.control_ratio < 1:
        parser.error("control-ratio must be >= 1")

    connection = _read_only_connection(db_path)
    try:
        report, samples = analyze(
            connection,
            start_date=start_date,
            end_date=end_date,
            control_ratio=args.control_ratio,
            max_dates=max(args.max_dates, 0),
        )
    finally:
        connection.close()

    output_prefix = Path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_payload = dict(report)
    if args.include_samples:
        json_payload["samples"] = samples
    output_prefix.with_suffix(".json").write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    output_prefix.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "json": str(output_prefix.with_suffix('.json')),
                "markdown": str(output_prefix.with_suffix('.md')),
                "sample_count": report["summary"]["sample_count"],
                "positive_count": report["summary"]["positive_count"],
                "control_count": report["summary"]["control_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
