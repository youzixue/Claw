"""指数历史日历、来源规范化与只读缺口对账；历史价格不原位回补。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
import math

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.trade_calendar import is_official_closed_day
from app.models.governance import TradeCalendarModel
from app.models.stock import StockDaily
from app.models.risk import DataSourceHealth


BENCHMARK_INDEX_CODES = ("000001", "399001", "399006")
HISTORY_API = "history_window_v1"
HISTORY_SCHEMA = "verified_index_history_window_v1"
HISTORY_CONTRACTS = {"tencent_index_raw_day_v1", "eastmoney_index_raw_day_v1"}
INDEX_SYMBOLS = {"000001": "sh000001", "399001": "sz399001", "399006": "sz399006"}


def _finite_positive(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _finite_optional(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


async def expected_index_trade_days(
    db: AsyncSession,
    *,
    through_date: date,
    window: int = 65,
) -> list[date]:
    """只使用已落库且经官方休市规则复核的交易日。"""
    if window <= 0:
        return []
    lower_bound = through_date - timedelta(days=max(window * 4, 400))
    rows = dict((await db.execute(select(
        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day,
    ).where(
        TradeCalendarModel.trade_date <= through_date,
        TradeCalendarModel.trade_date >= lower_bound,
    ))).all())
    days: list[date] = []
    cursor = through_date
    while cursor >= lower_bound and len(days) < window:
        if cursor.weekday() < 5 and not is_official_closed_day(cursor):
            # 应开市工作日缺少日历记录就失败关闭，不能跳到更早日期凑满65根。
            if cursor not in rows:
                return []
            if rows[cursor]:
                days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def normalize_index_daily_frame(frame: pd.DataFrame, *, code: str) -> dict[date, dict]:
    """把真实指数日线响应规范化；无效日期/收盘价不进入回补候选。"""
    if not isinstance(frame, pd.DataFrame) or frame.empty or "date" not in frame.columns:
        return {}
    normalized: dict[date, dict] = {}
    conflicts: set[date] = set()
    for _, row in frame.iterrows():
        raw_date = pd.to_datetime(row.get("date"), errors="coerce")
        if pd.isna(raw_date):
            continue
        trade_date = raw_date.date()
        close = _finite_positive(row.get("close"))
        if close is None or trade_date in conflicts:
            continue
        if trade_date in normalized and normalized[trade_date]["close"] != close:
            conflicts.add(trade_date)
            normalized.pop(trade_date)
            continue
        normalized[trade_date] = {
            "code": code,
            "trade_date": trade_date,
            "open": _finite_optional(row.get("open")),
            "high": _finite_optional(row.get("high")),
            "low": _finite_optional(row.get("low")),
            "close": close,
            "volume": int(row["volume"]) if _finite_positive(row.get("volume")) else None,
            "amount": _finite_optional(row.get("amount")),
        }
    ordered = sorted(normalized)
    for index, trade_date in enumerate(ordered):
        record = normalized[trade_date]
        previous_close = normalized[ordered[index - 1]]["close"] if index else None
        record["prev_close"] = previous_close
        record["change_pct"] = (
            round((record["close"] - previous_close) / previous_close * 100, 4)
            if previous_close else None
        )
        high, low = record["high"], record["low"]
        record["amplitude"] = (
            round((high - low) / previous_close * 100, 4)
            if previous_close and high is not None and low is not None else None
        )
        record["turnover"] = None
    return normalized


async def reconcile_index_history(
    db: AsyncSession,
    source,
    *,
    through_date: date,
    window: int = 65,
    codes: tuple[str, ...] = BENCHMARK_INDEX_CODES,
) -> dict:
    """Audit true-source gap candidates only; never insert/repair StockDaily.

    A newly fetched historical value was observed now, not at the old market
    timestamp. The scheduler archives its verified input window separately.
    This legacy function name remains compatible, but no write mode is offered.
    """
    with db.no_autoflush:
        expected_days = await expected_index_trade_days(
            db, through_date=through_date, window=window,
        )
    result = {
        "through_date": through_date.isoformat(),
        "expected_count": len(expected_days),
        "expected_complete": len(expected_days) == window,
        "codes": {},
        "inserted_count": 0,
        "repaired_invalid_count": 0,
        "candidate_count": 0,
        "mode": "audit_only_no_historical_projection",
        "historical_projection_changed": False,
        "complete": False,
    }
    if len(expected_days) != window:
        result["reason"] = f"完整交易日历不足{window}日"
        return result

    expected_set = set(expected_days)
    for code in codes:
        existing_rows = (await db.execute(select(
            StockDaily.trade_date, StockDaily.close,
        ).where(
            StockDaily.code == code,
            StockDaily.trade_date.in_(expected_days),
        ).execution_options(autoflush=False))).all()
        existing_by_day = {row.trade_date: row for row in existing_rows}
        existing_closes = {
            day: close
            for day, row in existing_by_day.items()
            if (close := _finite_positive(row.close)) is not None
        }
        missing_before = sorted(expected_set - set(existing_closes))
        source_error = None
        source_rows: dict[date, dict] = {}
        source_observed_at = None
        if missing_before:
            try:
                source_frame = await source.get_index_daily(code)
                source_observed_at = datetime.now().isoformat(timespec="microseconds")
                source_rows = normalize_index_daily_frame(source_frame, code=code)
            except Exception as exc:  # 数据源失败必须作为结果暴露，不能伪造完整
                source_error = type(exc).__name__
        insertable_days = [day for day in missing_before if day in source_rows]
        invalid_candidates = [day for day in insertable_days if day in existing_by_day]
        missing_candidates = [day for day in insertable_days if day not in existing_by_day]
        # Keep both absent and invalid original rows unchanged. Audit candidates
        # do not fill the projection or establish historical first availability.
        missing_after = missing_before
        frozen_source_rows = [
            source_rows[day] for day in expected_days if day in source_rows
        ]
        source_payload_sha256 = (
            hashlib.sha256(json.dumps(
                frozen_source_rows, sort_keys=True, default=str, separators=(",", ":"),
            ).encode()).hexdigest()
            if source_observed_at is not None else None
        )
        result["codes"][code] = {
            "missing_before": [day.isoformat() for day in missing_before],
            "inserted_dates": [],
            "repaired_invalid_dates": [],
            "missing_candidate_dates": [day.isoformat() for day in missing_candidates],
            "invalid_candidate_dates": [day.isoformat() for day in invalid_candidates],
            "source_missing_dates": [day.isoformat() for day in missing_before if day not in source_rows],
            "missing_after": [day.isoformat() for day in missing_after],
            "existing_close_conflicts_preserved": [day.isoformat() for day, close in existing_closes.items()
                if day in source_rows and abs(close - source_rows[day]["close"]) > 0.000001],
            "source_contract": (source_frame.attrs.get("source_contract") if source_rows else None)
                               or "IndexSource.get_index_daily/stock_zh_index_daily",
            "source_observed_at": source_observed_at,
            "source_payload_sha256": source_payload_sha256,
            "source_expected_row_count": len(frozen_source_rows),
            "source_error": source_error,
        }
        result["candidate_count"] += len(insertable_days)
    result["complete"] = all(not item["missing_after"] for item in result["codes"].values())
    if not result["complete"]:
        result["reason"] = (
            "历史投影缺口仅对账，未原位回补" if result["candidate_count"]
            else "真实指数历史源仍缺少期望交易日"
        )
    return result
