"""Pure limit-state projection; no fetching, database writes or universe claims.

Naive clocks are Asia/Shanghai wall clocks. Aware clocks are compared in that
zone; original source clocks are retained in output. The caller owns trading-day
calendar validation and may clear only explicitly returned valid_codes.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import json
import math
import re
from zoneinfo import ZoneInfo

import pandas as pd

TENCENT_LIMIT_VERSION = "tencent_limit_state_v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HALF_CENT = Decimal("0.005")
_CODE = re.compile(r"(?:00[0-3]|30[01]|60[0135]|68[89]|4\d{2}|8\d{2}|920)\d{3}")


def _code(value, *, suffix=False):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if suffix:
        value = re.sub(r"\.(?:SH|SZ|BJ)$", "", value, flags=re.IGNORECASE)
    return value if _CODE.fullmatch(value) and value != "000000" else None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() and math.isfinite(float(result)) else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def _clock(value):
    if not isinstance(value, datetime) or pd.isna(value):
        return None
    return (value.replace(tzinfo=_SHANGHAI) if value.tzinfo is None
            else value.astimezone(_SHANGHAI))


def _equal(left, right):
    return abs(left - right) < _HALF_CENT


def _above(left, right):
    return left > right and not _equal(left, right)


def _text(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and value.lower() not in {"nan", "none", "null", "<na>"} else None


def project_tencent_limits(
    records: list[dict], *, observed_at: datetime, max_age_sec: float
) -> dict:
    """Project current state, never infer 10% limits or metadata from OHLC.

    rejected_count counts input rows (including every duplicate occurrence).
    After close a same-day 15:00+ source quote remains usable that evening.
    Earlier source quotes still obey max_age_sec, including during lunch.
    Down and broken can overlap: today's high touched up, now trading at down.
    """
    observed = _clock(observed_at)
    age_limit = _number(max_age_sec)
    if observed is None or age_limit is None or age_limit < 0:
        raise ValueError("valid observed_at and finite nonnegative max_age_sec required")
    result = {"valid_codes": set(), "up": [], "down": [], "broken": [],
              "rejected_count": 0}
    counts = Counter(_code(row.get("code")) for row in records if isinstance(row, dict))
    for row in records:
        code = _code(row.get("code")) if isinstance(row, dict) else None
        if code is None or counts[code] != 1:
            result["rejected_count"] += 1
            continue
        source = _clock(row.get("source_quote_at"))
        received = _clock(row.get("received_at"))
        values = {key: _number(row.get(key)) for key in
                  ("price", "limit_up", "limit_down", "prev_close", "high", "low", "volume")}
        valid_clock = (
            source is not None and received is not None
            and source.date() == received.date() == observed.date()
            and source <= received <= observed and source.time() >= time(9, 30)
        )
        if valid_clock:
            closed = observed.time() >= time(15) and source.time() >= time(15)
            valid_clock = closed or (
                source.time() <= time(15)
                and (observed - source).total_seconds() <= age_limit
            )
        if not valid_clock or any(v is None or v <= 0 for v in values.values()):
            result["rejected_count"] += 1
            continue
        p, up, down, prev, high, low = (values[k] for k in
                                       ("price", "limit_up", "limit_down", "prev_close", "high", "low"))
        if not (down < prev < up) or any((
            _above(low, p), _above(p, high), _above(high, up), _above(down, low),
            _above(low, high),
        )):
            result["rejected_count"] += 1
            continue
        result["valid_codes"].add(code)
        at_up, at_down = _equal(p, up), _equal(p, down)
        broken = _equal(high, up) and _above(up, p)
        turnover = _number(row.get("turnover"))
        turnover = float(turnover) if turnover is not None and turnover >= 0 else None
        round_id = row.get("quote_round_id", row.get("round_id"))
        if not isinstance(round_id, (str, int)) or isinstance(round_id, bool):
            round_id = None
        common = {
            "code": code, "name": _text(row.get("name")), "trade_date": observed.date(),
            "source": "tencent", "source_version": TENCENT_LIMIT_VERSION,
            "source_quote_at": row["source_quote_at"], "observed_at": observed_at,
        }

        def evidence(state):
            return json.dumps({
                "state": state,
                "state_basis": "current_quote_at_explicit_limit" if state != "broken"
                               else "intraday_high_touched_up_current_below_up",
                "source_quote_at": row["source_quote_at"].isoformat(),
                "received_at": row["received_at"].isoformat(),
                "observed_at": observed_at.isoformat(), "quote_round_id": round_id,
                "scope": "verified_codes_only_not_full_market",
                "first_touch_time_known": False, "break_count_known": False,
                "price": float(p), "high": float(high), "low": float(low),
                "limit_up": float(up), "limit_down": float(down),
            }, ensure_ascii=False, allow_nan=False)

        if at_up:
            result["up"].append({
                **common, "evidence_json": evidence("up"), "quarantined": False,
                "limit_up_price": float(p), "turnover": turnover,
                "limit_up_time": None, "seal_amount": None, "break_count": None,
                "consecutive_days": None, "limit_up_reason": None,
            })
        if at_down:
            result["down"].append({
                **common, "evidence_json": evidence("down"),
                "limit_down_time": None, "break_count": None,
                "consecutive_days": None, "reason": None,
            })
        if broken:
            result["broken"].append({
                **common, "evidence_json": evidence("broken"), "limit_up_price": float(up),
                "limit_up_time": None, "break_time": None, "seal_duration": None,
                "seal_amount": None, "close_price": float(p),
                "final_state": "broken", "close_at_limit": False,
            })
    return result


_METRICS = {
    "consecutive_days": ("连续涨停天数",),
    "break_count": ("涨停开板次数",),
    "limit_up_time": ("首次涨停时间",),
    "limit_up_reason": ("涨停原因类别", "涨停原因"),
    "seal_amount": ("涨停封单额",),
}


def _metric_column(column, base, day):
    """Only exact dated fields, optionally carrying a simple explicit unit."""
    if not isinstance(column, str):
        return None
    for unit, scale in (("", 1), ("元", 1), ("万", 10000), ("万元", 10000),
                        ("亿", 100000000), ("亿元", 100000000)):
        marker = f"[{day}]"
        candidates = {f"{base}{marker}"} if not unit else {
            f"{base}({unit}){marker}", f"{base}{marker}({unit})",
            f"{base}（{unit}）{marker}", f"{base}{marker}（{unit}）",
        }
        if column in candidates:
            return scale
    return None


def _detail_time(value, trade_date):
    if isinstance(value, datetime):
        clock = _clock(value)
        return clock.strftime("%H:%M:%S") if clock and clock.date() == trade_date else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        try:
            return time.fromisoformat(value).strftime("%H:%M:%S")
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", value):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.strftime("%H:%M:%S") if parsed.date() == trade_date else None
        except ValueError:
            return None
    return None


def parse_wencai_limit_details(df: pd.DataFrame, trade_date: date) -> dict:
    """Return dated metadata only; empty/wrong-day/truncated frames raise.

    Missing/invalid values stay None, including unknown board/break counts.
    Numeric seal amounts use the existing parser's CNY contract. Only explicit
    simple unit column labels scale values; embedded '1.2亿' strings are not guessed.
    Duplicate normalized codes reject the frame, rather than picking a winner.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise ValueError("empty Wencai frame is not complete-market evidence")
    if not df.columns.is_unique:
        raise ValueError("duplicate Wencai columns")
    day = trade_date.strftime("%Y%m%d")
    selected = {}
    for metric, aliases in _METRICS.items():
        for alias in aliases:
            matches = [(col, _metric_column(col, alias, day)) for col in df.columns
                       if metric == "seal_amount" or col == f"{alias}[{day}]"]
            matches = [(col, scale) for col, scale in matches if scale is not None]
            if matches:
                if len(matches) != 1:
                    raise ValueError(f"ambiguous dated field: {metric}")
                selected[metric] = matches[0]
                break
    if not selected:
        raise ValueError("no exact trade-date Wencai metadata fields")
    code_column = "股票代码" if "股票代码" in df.columns else "代码"
    if code_column not in df.columns:
        raise ValueError("missing Wencai code column")
    codes = [_code(value, suffix=True) for value in df[code_column]]
    valid = [code for code in codes if code is not None]
    if len(valid) != len(set(valid)):
        raise ValueError("duplicate normalized Wencai codes")
    output = {}
    for code, (_, row) in zip(codes, df.iterrows()):
        if code is None:
            continue
        details = dict.fromkeys(_METRICS)
        for metric, (column, scale) in selected.items():
            value = row[column]
            if metric == "limit_up_reason":
                details[metric] = _text(value)
            elif metric == "limit_up_time":
                details[metric] = _detail_time(value, trade_date)
            else:
                number = _number(value)
                minimum = 1 if metric == "consecutive_days" else 0
                if number is None or number < minimum:
                    continue
                if metric == "seal_amount":
                    amount = number * scale
                    # Decimal can be finite while overflowing the ORM float.
                    if abs(amount) <= Decimal(str(float.fromhex("0x1.fffffffffffffp+1023"))):
                        details[metric] = float(amount)
                elif number == number.to_integral_value():
                    details[metric] = int(number)
        output[code] = details
    for key in ("code_count", "row_count"):
        if key in df.attrs:
            count = _number(df.attrs[key])
            if count is None or count < 0 or count != count.to_integral_value():
                raise ValueError(f"invalid Wencai {key}")
            if count > len(output):
                raise ValueError(f"truncated Wencai frame: {key} exceeds returned codes")
    return output
