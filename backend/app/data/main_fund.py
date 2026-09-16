"""Read-only projection of current, qualified main-order funds.

StockSpot's legacy main_net_inflow is not a fund source: Tencent field 50 is
five-level order-book difference in hands. Never consult it here. FundFlow is
a daily latest-row table, not an immutable historical replay ledger.
"""
from datetime import date, datetime
import math

from numpy import bool_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.data.fund_flow_clock import (
    evidence_clock, fund_clock_status, local_clock, main_fund_values_valid,
)
from app.models.stock import FundFlow


MAIN_FUND_POLICY_VERSION = "qualified_main_order_fund_v1_no_spot"
# A recognized mapping still needs real per-row clocks and values. The installed
# AkShare adapter drops f124, so its receipt-only frames do NOT pass this gate.
MAIN_FUND_SOURCES = frozenset({
    ("eastmoney", "individual_fund_flow_v3_f124"),
    ("eastmoney_via_akshare", "individual_fund_flow_eastmoney_akshare_v1"),
    ("tencent", "tencent_hsfundtab_v1"),
})
_BREAKDOWN_FIELDS = (
    "super_net_inflow", "super_net_inflow_pct",
    "big_net_inflow", "big_net_inflow_pct",
    "mid_net_inflow", "mid_net_inflow_pct",
    "small_net_inflow", "small_net_inflow_pct",
)
_FIELDS = (
    "code", "name", "trade_date", "main_net_inflow", "main_net_inflow_pct",
    *_BREAKDOWN_FIELDS,
    "source", "source_version", "source_quote_at", "received_at", "observed_at",
)


def main_fund_source_supported(source, source_version) -> bool:
    return (isinstance(source, str) and isinstance(source_version, str)
            and (source, source_version) in MAIN_FUND_SOURCES)


def fund_order_breakdown(row) -> dict:
    """Owned nullable numeric leaves; not a source/freshness certification."""
    get = row.get if hasattr(row, "get") else lambda key: getattr(row, key, None)
    result = {}
    for field in _BREAKDOWN_FIELDS:
        value = get(field)
        try:
            result[field] = (float(value) if not isinstance(value, (bool, bool_))
                             and math.isfinite(float(value)) else None)
        except (ValueError, TypeError, OverflowError):
            result[field] = None
    return result


def current_main_fund_status(row, *, trade_date: date, decision_at: datetime) -> str:
    """Explain the current-evidence gate without exposing rejected numeric values."""
    if row is None:
        return "missing"
    get = row.get if hasattr(row, "get") else lambda key: getattr(row, key, None)
    if get("trade_date") != trade_date:
        return "invalid"
    if not main_fund_source_supported(get("source"), get("source_version")):
        return "unsupported_source"
    if not main_fund_values_valid(get("main_net_inflow"), get("main_net_inflow_pct")):
        return "invalid_values"
    return fund_clock_status(
        get("source_quote_at"), get("received_at"), get("observed_at"),
        trade_date, decision_at, settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
    )


def current_main_fund_evidence(row, *, trade_date: date, decision_at: datetime) -> dict | None:
    """Accept only measured same-day evidence already visible at the cutoff."""
    if current_main_fund_status(row, trade_date=trade_date, decision_at=decision_at) != "ok":
        return None
    get = row.get if hasattr(row, "get") else lambda key: getattr(row, key, None)
    result = {field: get(field) for field in _FIELDS}
    # The validator accepts ISO clocks as well as datetimes. Return one stable
    # type so API serializers and frozen signal consumers see identical leaves.
    for field in ("source_quote_at", "received_at", "observed_at"):
        result[field] = evidence_clock(result[field])
    result["main_net_inflow"] = float(result["main_net_inflow"])
    result["main_net_inflow_pct"] = float(result["main_net_inflow_pct"])
    result.update(fund_order_breakdown(row))
    result.update(
        date=trade_date, clock_status="ok", is_stale=False,
        main_fund_policy=MAIN_FUND_POLICY_VERSION,
        source_clock_basis=("provider_fund_minute_watermark" if get("source") == "tencent"
                            else "provider_quote_watermark_not_fund_calculation_time"),
    )
    return result


ANOMALY_FUND_SCHEMA = "anomaly_main_fund_v1"


def freeze_anomaly_main_fund(row: dict | None, *, code: str, trade_date: date,
                             decision_at: datetime) -> dict:
    """Freeze one qualified signal frame; never promote a display projection."""
    raw = dict(row or {})
    raw_date = raw.get("trade_date")
    if isinstance(raw_date, str):
        try:
            raw_date = date.fromisoformat(raw_date)
        except ValueError:
            raw_date = None
    candidate = {**raw, "trade_date": raw_date,
                 "source": raw.get("provider_source") or raw.get("source")}
    qualified = None
    if (str(raw.get("code") or "") == code and not raw.get("is_stale")
            and raw.get("source") not in {"stock_spot", "unavailable"}
            and raw.get("purpose") != "display_only"):
        qualified = current_main_fund_evidence(
            candidate, trade_date=trade_date, decision_at=decision_at,
        )
    frame = dict(qualified or {})
    for key, value in frame.items():
        if isinstance(value, (date, datetime)):
            frame[key] = value.isoformat()
    return {**frame, "schema": ANOMALY_FUND_SCHEMA, "purpose": "signal",
            "code": code, "trade_date": trade_date.isoformat(),
            "event_at": decision_at.isoformat(), "available": qualified is not None}


def anomaly_main_fund_evidence(detail: dict, *, code: str | None = None,
                               decision_at: datetime) -> dict | None:
    """Recheck the frozen frame at detection AND use time, without DB fallback.

    Legacy flat details need full provenance too. An explicit empty/invalid new
    frame must not fall back to flat amounts, display evidence or a later row.
    """
    if detail.get("purpose") == "display_only":
        return None
    nested = "fund_signal_evidence" in detail
    raw = detail.get("fund_signal_evidence") if nested else detail
    if not isinstance(raw, dict) or raw.get("purpose") == "display_only":
        return None
    if nested and (raw.get("schema") != ANOMALY_FUND_SCHEMA
                   or raw.get("purpose") != "signal" or raw.get("available") is not True):
        return None
    if raw.get("is_stale"):
        return None
    frame_code = str(raw.get("code") or "")
    if nested and not frame_code:
        return None
    if code is not None and frame_code != str(code):
        return None
    trade_date = raw.get("trade_date")
    if isinstance(trade_date, str):
        try:
            trade_date = date.fromisoformat(trade_date)
        except ValueError:
            return None
    cutoff = evidence_clock(decision_at)
    if cutoff is None or trade_date != cutoff.date():
        return None
    source = raw.get("source")
    if source in {"eastmoney_main_fund", "fund_flow"}:
        source = raw.get("provider_source")
    candidate = {**raw, "trade_date": trade_date, "source": source}
    if nested:
        event_at = evidence_clock(raw.get("event_at"))
        if event_at is None or event_at > cutoff or event_at.date() != trade_date:
            return None
        if current_main_fund_evidence(candidate, trade_date=trade_date, decision_at=event_at) is None:
            return None
    return current_main_fund_evidence(candidate, trade_date=trade_date, decision_at=cutoff)


def main_fund_display_evidence(
    row, *, trade_date: date, as_of_at: datetime,
    basis: str = "latest_snapshot", event_at: datetime | None = None,
) -> dict:
    """Display-only projection; NEVER supply it to a signal or execution gate.

    Event evidence is frozen at detection, not reconstructed from FundFlow later.
    A last-known daily row may be shown with its source clock, but it is neither
    a closing final nor evidence that an earlier event had confirmed funds.
    """
    get = row.get if hasattr(row, "get") else lambda key: getattr(row, key, None)
    provider = get("provider_source") or get("source")
    source = get("source")
    source_at, received_at, observed_at = (
        evidence_clock(get(key)) for key in ("source_quote_at", "received_at", "observed_at")
    )
    cutoff = evidence_clock(as_of_at)
    event_clock = evidence_clock(event_at)
    result = {
        "schema": "anomaly_fund_display_v1", "purpose": "display_only",
        "code": str(get("code") or ""),
        "basis": basis, "available": False, "main_net_inflow": None,
        "main_net_inflow_pct": None, "trade_date": str(trade_date),
        "provider_source": provider, "source_version": get("source_version"),
        "source": "fund_flow" if provider == "tencent" else source,
        "source_quote_at": source_at.isoformat() if source_at else None,
        "received_at": received_at.isoformat() if received_at else None,
        "observed_at": observed_at.isoformat() if observed_at else None,
        "event_at": event_clock.isoformat() if event_clock else None,
        "displayed_at": cutoff.isoformat() if cutoff else None,
        "clock_status": "unknown", "is_stale": True,
    }
    if basis not in {"latest_snapshot", "event_snapshot"}:
        return result
    if source in {"stock_spot", "unavailable"}:
        return result
    if not main_fund_source_supported(provider, get("source_version")):
        result["clock_status"] = "unsupported_source"
        return result
    raw_date = get("trade_date")
    if isinstance(raw_date, str):
        try:
            raw_date = date.fromisoformat(raw_date)
        except ValueError:
            raw_date = None
    if raw_date != trade_date:
        result["clock_status"] = "invalid"
        return result
    if not main_fund_values_valid(get("main_net_inflow"), get("main_net_inflow_pct")):
        return result
    if basis == "event_snapshot":
        if event_clock is None or event_clock.date() != trade_date or cutoff is None:
            return result
        if event_clock > cutoff:
            result["clock_status"] = "future"
            return result
        # Only the original event's visibility boundary can certify its evidence.
        at_event = fund_clock_status(
            source_at, received_at, observed_at, trade_date, event_clock,
            settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
        )
        if at_event != "ok":
            result["clock_status"] = at_event
            return result
    historical = fund_clock_status(
        source_at, received_at, observed_at, trade_date, cutoff,
        settings.FUND_FLOW_SOURCE_MAX_AGE_SEC, require_live=False,
    )
    if historical != "historical_known":
        result["clock_status"] = historical
        return result
    current_status = fund_clock_status(
        source_at, received_at, observed_at, trade_date, cutoff,
        settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
    )
    result.update(
        available=True, main_net_inflow=float(get("main_net_inflow")),
        main_net_inflow_pct=float(get("main_net_inflow_pct")),
        clock_status=current_status, is_stale=current_status != "ok",
        **fund_order_breakdown(row),
    )
    return result


async def load_latest_main_fund_display_map(
    db: AsyncSession, *, trade_date: date, as_of_at: datetime, codes,
) -> dict[str, dict]:
    """Read only requested stock-day leaves for UI; no rewind or cross-day fallback."""
    codes = tuple(sorted({str(code) for code in codes if code}))
    if not codes:
        return {}
    query = select(*(getattr(FundFlow, field) for field in _FIELDS)).where(
        FundFlow.trade_date == trade_date, FundFlow.code.in_(codes),
    )
    rows = (await db.execute(query.execution_options(autoflush=False))).mappings()
    return {
        str(row["code"]): main_fund_display_evidence(
            row, trade_date=trade_date, as_of_at=as_of_at,
        )
        for row in rows
    }


async def load_current_main_fund_map(
    db: AsyncSession, *, trade_date: date, decision_at: datetime, codes=None,
    diagnostics: dict[str, str] | None = None,
) -> dict[str, dict]:
    """One authoritative read; no cache, latest-date fallback, flush or writes.

    A newer daily row than the requested cutoff is rejected, NOT rewound to an
    invented previous value. Callers must freeze selected evidence themselves
    when producing immutable decision records. Optional diagnostics contain only
    per-code rejection statuses from this SAME read, never stale/future amounts.
    Without codes the diagnostic denominator is observed rows, not the universe.
    """
    if codes is not None:
        codes = tuple(sorted({str(code) for code in codes if code}))
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({code: "missing" for code in codes or ()})
    cutoff = local_clock(decision_at)
    if cutoff is None or cutoff.date() != trade_date:
        if diagnostics is not None:
            diagnostics.update({code: "invalid_cutoff" for code in codes or ()})
        return {}
    # The existing raw upsert stores ISO datetime with "T", whereas ORM binds
    # use a space. SQLite text comparison can falsely discard every fresh row.
    # Filter date/codes in SQL; compare all three parsed clocks below, including
    # first availability, future rejection and freshness. Never rewrite history.
    query = select(*(getattr(FundFlow, field) for field in _FIELDS)).where(
        FundFlow.trade_date == trade_date,
    )
    if codes is not None:
        if not codes:
            return {}
        query = query.where(FundFlow.code.in_(codes))
    # Selecting scalar columns + autoflush=False keeps unrelated pending ORM
    # state out of this read, including when a caller owns a write transaction.
    rows = (await db.execute(query.execution_options(autoflush=False))).mappings()
    result = {}
    for row in rows:
        item = current_main_fund_evidence(row, trade_date=trade_date, decision_at=cutoff)
        if diagnostics is not None:
            diagnostics[str(row["code"])] = (
                "ok" if item is not None else current_main_fund_status(
                    row, trade_date=trade_date, decision_at=cutoff,
                )
            )
        if item is not None:
            result[str(item["code"])] = item
    return result
