"""真实指数历史窗口的不可变可知时点证据，独立于可覆盖的StockDaily行情表。"""
from datetime import date, datetime, time
import hashlib
import json

import pandas as pd
from sqlalchemy import select

from app.data.index_history import (
    BENCHMARK_INDEX_CODES, HISTORY_API, HISTORY_SCHEMA, HISTORY_CONTRACTS, INDEX_SYMBOLS,
    expected_index_trade_days, normalize_index_daily_frame, reconcile_index_history,
    _finite_positive,
)
from app.models.risk import DataSourceHealth


def _clock(value):
    try:
        at = datetime.fromisoformat(str(value))
        return at if at.tzinfo is None else None
    except (ValueError, TypeError):
        return None


def inputs_hash(dates, closes):
    return hashlib.sha256(json.dumps({"dates": dates, "closes": closes},
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def build_history_evidence(frames, *, expected_days, through_date, observed_at):
    evidence = {"schema": HISTORY_SCHEMA, "through_date": through_date.isoformat(),
                "observed_at": observed_at.isoformat(), "expected_count": len(expected_days),
                "complete": False, "codes": {}, "scope": "future_entries_only_no_relabel"}
    if len(expected_days) != 65 or expected_days[-1] != through_date:
        evidence["reason"] = "连续交易日历不足65日或末日不匹配"
        return evidence
    cutoff = datetime.combine(through_date, time(15, 5))
    if observed_at < cutoff:
        evidence["reason"] = "目标交易日尚未完成收盘"
        return evidence
    for code in BENCHMARK_INDEX_CODES:
        frame = frames.get(code)
        frame = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()
        normalized = normalize_index_daily_frame(frame, code=code)
        contract = frame.attrs.get("source_contract")
        source_at = _clock(frame.attrs.get("source_observed_at"))
        item = {"source_contract": contract, "index_symbol": frame.attrs.get("index_symbol"),
                "source_observed_at": source_at.isoformat() if source_at else None,
                "source_failures": frame.attrs.get("source_failures", []),
                "primary_failures": frame.attrs.get("primary_failures", []),
                "missing_dates": [day.isoformat() for day in expected_days if day not in normalized]}
        valid = (contract in HISTORY_CONTRACTS and item["index_symbol"] == INDEX_SYMBOLS[code]
                 and source_at is not None and cutoff <= source_at <= observed_at
                 and not item["missing_dates"])
        if valid:
            item["input_dates"] = [day.isoformat() for day in expected_days]
            item["input_closes"] = [normalized[day]["close"] for day in expected_days]
            item["input_sha256"] = inputs_hash(item["input_dates"], item["input_closes"])
        item["complete"] = bool(valid)
        evidence["codes"][code] = item
    evidence["complete"] = all(item["complete"] for item in evidence["codes"].values())
    if not evidence["complete"]:
        evidence["reason"] = "真实指数历史响应/代码/收盘日期或窗口不完整"
    return evidence


async def latest_verified_history(db, *, through_date: date, at: datetime, expected_days=None):
    """只读首次可知时间<=at的冻结输入；不从后来回补的StockDaily重新解释过去。"""
    if at.tzinfo is not None:
        return None, None
    expected_days = expected_days if expected_days is not None else await expected_index_trade_days(
        db, through_date=through_date)
    if len(expected_days) != 65 or expected_days[-1] != through_date:
        return None, None
    expected = [day.isoformat() for day in expected_days]
    cutoff = datetime.combine(through_date, time(15, 5))
    rows = list((await db.scalars(select(DataSourceHealth).where(
        DataSourceHealth.source == "index", DataSourceHealth.api_name == HISTORY_API,
        DataSourceHealth.status == "up", DataSourceHealth.completeness >= 1,
        DataSourceHealth.last_success >= cutoff, DataSourceHealth.last_success <= at,
        DataSourceHealth.updated_at <= at,
    ).order_by(DataSourceHealth.id.desc()).limit(50))).all())
    for row in rows:
        try:
            payload = json.loads(row.error_msg or "{}")
            payload_at = _clock(payload.get("observed_at"))
            if (payload.get("schema") != HISTORY_SCHEMA or payload.get("complete") is not True
                    or payload.get("through_date") != through_date.isoformat()
                    or payload_at is None or not cutoff <= payload_at <= row.last_success <= at):
                continue
            for code in BENCHMARK_INDEX_CODES:
                item = payload["codes"][code]
                clocks = _clock(item.get("source_observed_at"))
                closes = item.get("input_closes")
                if (item.get("source_contract") not in HISTORY_CONTRACTS
                        or item.get("index_symbol") != INDEX_SYMBOLS[code]
                        or clocks is None or not cutoff <= clocks <= payload_at
                        or item.get("input_dates") != expected
                        or not isinstance(closes, list) or len(closes) != 65
                        or any(_finite_positive(close) is None for close in closes)
                        or inputs_hash(expected, closes) != item.get("input_sha256")):
                    raise ValueError("指数冻结窗口合同不匹配")
            return row, payload
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return None, None


async def refresh_verified_history(*, source, through_date, session_factory):
    """读日历→关闭DB→网络→短写事务：网络不得占用数据库写锁。"""
    async with session_factory() as db:
        expected = await expected_index_trade_days(db, through_date=through_date)
        health, payload = await latest_verified_history(db, through_date=through_date,
                                                       at=datetime.now(), expected_days=expected)
        if health is not None:
            return {"status": "already_verified", "health_id": health.id,
                    "through_date": through_date.isoformat(), "complete": True}
    frames = {}
    if len(expected) == 65 and expected[-1] == through_date:
        for code in BENCHMARK_INDEX_CODES:
            try:
                frames[code] = await source.get_index_history_window(
                    code, start_date=expected[0], end_date=through_date, expected_trade_dates=expected)
            except Exception as exc:
                frame = pd.DataFrame()
                frame.attrs["source_failures"] = [{"error_type": type(exc).__name__}]
                frames[code] = frame
    observed_at = datetime.now()
    payload = build_history_evidence(frames, expected_days=expected, through_date=through_date,
                                     observed_at=observed_at)
    # Append a newly observed input window only. A complete current observation
    # must not backfill StockDaily or certify visibility at an older decision.
    async with session_factory() as db:
        if payload["complete"]:
            class FrozenSource:
                async def get_index_daily(self, code):
                    return frames[code]
            audit = await reconcile_index_history(db, FrozenSource(), through_date=through_date)
            payload["projection_audit"] = audit
        payload["projection_policy"] = "audit_only_no_historical_projection"
        health = DataSourceHealth(
            source="index", api_name=HISTORY_API,
            status="up" if payload["complete"] else "degraded",
            completeness=1.0 if payload["complete"] else 0.0,
            last_success=observed_at if payload["complete"] else None,
            last_failure=None if payload["complete"] else observed_at,
            fail_streak=0 if payload["complete"] else 1,
            error_msg=json.dumps(payload, ensure_ascii=False, allow_nan=False),
            updated_at=observed_at,
        )
        db.add(health)
        await db.commit()
        return {"status": health.status, "health_id": health.id,
                "through_date": through_date.isoformat(), "complete": payload["complete"],
                "reason": payload.get("reason"),
                "inserted_count": 0, "repaired_invalid_count": 0,
                "projection_policy": payload["projection_policy"],
                "candidate_count": payload.get("projection_audit", {}).get("candidate_count", 0)}
