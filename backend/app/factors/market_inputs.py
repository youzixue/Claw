"""Shared read-time daily factor inputs; NOT historical PIT or execution evidence.

Never reads StockDaily/StockSpot as a fallback. Calendar holes remain holes.
No persistence, networking, settings changes, training or orders occur here.
"""
from datetime import date, datetime, time, timedelta
import hashlib
import json
import re

import pandas as pd
from sqlalchemy import select

from app.data.index_history import expected_index_trade_days
from app.data.kline_observations import BAR_FIELDS, kline_quality_issues
from app.data.main_fund_window import closed_fund_status
from app.data.price_chain import FORMAL_CLOSE_SOURCES, price_chain_evidence
from app.factors.base import _finite_number
from app.models.stock import FundFlow, StockKline

PROTOCOL = "factor_formal_daily_inputs_v1"
WINDOW = 30  # Preserve the existing factor horizon, plus one price-chain anchor.
MARKET_FIELDS = ("open", "high", "low", "close", "volume", "amount",
                 "turnover", "amplitude", "change_pct")
FUND_FIELDS = ("main_net_inflow", "big_net_inflow", "mid_net_inflow", "small_net_inflow")
FUND_COLUMNS = ("code", "trade_date", *FUND_FIELDS, "main_net_inflow_pct",
                "source", "source_version", "source_quote_at", "received_at", "observed_at")


def validate_code(code):
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        raise ValueError("factor_code_requires_six_ascii_digits")


async def factor_sessions(db, *, trade_date: date | None, as_of_at: datetime) -> list[date]:
    """Use confirmed LOCAL sessions, never the latest available stock's row date."""
    if not isinstance(as_of_at, datetime) or as_of_at.tzinfo is not None:
        raise ValueError("factor_input_requires_local_naive_clock")
    if trade_date is not None and type(trade_date) is not date:
        raise ValueError("factor_input_requires_date")
    through = as_of_at.date() - timedelta(days=as_of_at.time() < time(15, 10))
    if trade_date is not None and trade_date > through:
        raise ValueError("factor_session_not_completed")
    with db.no_autoflush:
        sessions = await expected_index_trade_days(
            db, through_date=trade_date or through, window=WINDOW + 1)
    if len(sessions) != WINDOW + 1:
        raise ValueError("factor_calendar_incomplete")
    if trade_date is not None and sessions[-1] != trade_date:
        raise ValueError("factor_requested_date_not_confirmed_session")
    return sessions


def _bar_issues(row):
    if row is None:
        return ["bar_missing"]
    issues = kline_quality_issues(row)
    if row["source"] not in FORMAL_CLOSE_SOURCES:
        issues.append("nonformal_source")
    # A zero-volume suspended row is not a normal trading observation.
    volume = _finite_number(row["volume"])
    if volume is None or volume <= 0:
        issues.append("volume_missing_or_suspended")
    return list(dict.fromkeys(issues))


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def load_factor_market_inputs(db, *, code: str, trade_date: date | None,
                                   as_of_at: datetime):
    """Return a fixed-length frame and owned diagnostics from explicit SQL leaves.

    Always recheck the local calendar. Caller-supplied session lists must not
    bypass missing/closed dates or be mistaken for verified calendar evidence.
    """
    validate_code(code)
    sessions = await factor_sessions(db, trade_date=trade_date, as_of_at=as_of_at)
    bars = (await db.execute(select(*(getattr(StockKline, k) for k in BAR_FIELDS))
        .where(StockKline.code == code, StockKline.trade_date.in_(sessions))
        .execution_options(autoflush=False))).mappings().all()
    funds = (await db.execute(select(*(getattr(FundFlow, k) for k in FUND_COLUMNS))
        .where(FundFlow.code == code, FundFlow.trade_date.in_(sessions[1:]))
        .execution_options(autoflush=False))).mappings().all()
    by_day = {row["trade_date"]: row for row in bars}
    by_fund_day = {row["trade_date"]: row for row in funds}
    rows, diagnostics = [], []
    for before_day, day in zip(sessions, sessions[1:]):
        bar, previous = by_day.get(day), by_day.get(before_day)
        issues = _bar_issues(bar)
        prior_issues = _bar_issues(previous)
        chain = price_chain_evidence(
            previous["close"] if previous is not None and not prior_issues else None,
            bar["prev_close"] if bar is not None else None)
        if chain["status"] != "consistent":
            issues.append("price_chain_" + chain["status"])
        row = {"trade_date": day}
        # Do not filter bad/missing sessions out and concatenate disconnected rows.
        for key in MARKET_FIELDS:
            row[key] = _finite_number(bar[key]) if not issues and key != "amplitude" else None
        # StockKline has no amplitude column: do not borrow a mutable StockDaily
        # field. Preserve unknown pending an explicitly validated derivation.
        fund = by_fund_day.get(day)
        fund_status = closed_fund_status(fund, decision_at=as_of_at)
        for key in FUND_FIELDS:
            row[key] = _finite_number(fund[key]) if fund_status == "dated_known" else None
        rows.append(row)
        diagnostics.append({
            "trade_date": day.isoformat(), "bar_issues": issues,
            "source": bar["source"] if bar is not None else None,
            "price_chain": chain, "fund_status": fund_status,
            "fund_source": fund["source"] if fund is not None else None,
            "fund_source_version": fund["source_version"] if fund is not None else None,
            **{key: fund[key].isoformat() if fund is not None and fund[key] is not None else None
               for key in ("source_quote_at", "received_at", "observed_at")},
        })
    # NaN is an internal pandas missing marker only; external evidence is strict JSON.
    material = [{**row, "trade_date": row["trade_date"].isoformat()} for row in rows]
    evidence = {
        "protocol": PROTOCOL, "basis": "read_time_formal_daily_projection_not_pit",
        "code": code, "trade_date": sessions[-1].isoformat(),
        "as_of_at": as_of_at.isoformat(), "window_sessions": WINDOW,
        "session_dates": [day.isoformat() for day in sessions[1:]],
        "anchor_date": sessions[0].isoformat(),
        "usable_bar_count": sum(not item["bar_issues"] for item in diagnostics),
        "qualified_fund_count": sum(item["fund_status"] == "dated_known" for item in diagnostics),
        "days": diagnostics, "input_values_sha256": _digest(material),
        "column_available_counts": {
            key: sum(row[key] is not None for row in rows) for key in (*MARKET_FIELDS, *FUND_FIELDS)
        },
        "point_in_time_verified": False, "trading_authority": False,
        "automatic_weight_update": False, "promotion_eligible": False,
        "limitations": [
            "StockKline and FundFlow are mutable dated projections, not original availability ledgers",
            "as_of_at is a requested read cutoff, not proof of historical first availability",
            "amplitude and unsupplied per-stock context remain unknown; no StockDaily/Spot fallback",
            "this returned frame/hash is not an append-only persisted factor capture or realized return",
        ],
    }
    return pd.DataFrame(rows, columns=("trade_date", *MARKET_FIELDS, *FUND_FIELDS)), evidence
