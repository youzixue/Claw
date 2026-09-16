"""Read-only five-completed-session funds, never a historical PIT reconstruction."""
from datetime import date, datetime, time, timedelta
import math

from sqlalchemy import select

from app.config.settings import settings
from app.data.fund_flow_clock import evidence_clock, fund_clock_status, main_fund_values_valid
from app.data.index_history import expected_index_trade_days
from app.data.main_fund import main_fund_source_supported
from app.models.stock import FundFlow

MAIN_FUND_WINDOW_VERSION = "five_confirmed_closed_sessions_v1"
_WINDOW_FIELDS = (
    "code", "trade_date", "main_net_inflow", "main_net_inflow_pct",
    "source", "source_version", "source_quote_at", "received_at", "observed_at",
)


def completed_fund_through(through_date: date | None, decision_at: datetime) -> date | None:
    """Today's unfinished session never belongs to the five full-session baseline."""
    cutoff = evidence_clock(decision_at)
    if cutoff is None or through_date is None or through_date > cutoff.date():
        return None
    if through_date == cutoff.date() and cutoff.time() < time(15):
        return through_date - timedelta(days=1)
    return through_date


def closed_fund_status(row, *, decision_at: datetime) -> str:
    """Require a real closing watermark visible by the original ranking cutoff."""
    if row is None:
        return "missing"
    if not main_fund_source_supported(row.get("source"), row.get("source_version")):
        return "unsupported_source"
    if not main_fund_values_valid(row.get("main_net_inflow"), row.get("main_net_inflow_pct")):
        return "invalid_values"
    status = fund_clock_status(
        row.get("source_quote_at"), row.get("received_at"), row.get("observed_at"),
        row.get("trade_date"), decision_at, settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
        require_live=False,
    )
    if status != "historical_known":
        return status
    if evidence_clock(row.get("source_quote_at")).time() < time(15):
        return "incomplete_session"
    return "dated_known"


async def load_main_fund_window(db, *, through_date: date | None, decision_at: datetime, codes=None) -> dict:
    """Exactly five confirmed sessions; any calendar/day/value gap keeps sum unknown.

    FundFlow retains ONE latest row per stock/day. We reject later observations,
    never rewind them or copy latest into older days, and label accepted values
    dated_latest_not_pit rather than an immutable historical decision ledger.
    """
    cutoff = evidence_clock(decision_at)
    end = completed_fund_through(through_date, decision_at)
    requested = tuple(sorted({str(code) for code in codes if code})) if codes is not None else None
    result = {
        "version": MAIN_FUND_WINDOW_VERSION, "basis": "dated_latest_not_pit",
        "status": "invalid_cutoff" if end is None else "calendar_incomplete",
        "decision_at": cutoff.isoformat() if cutoff else None,
        "requested_through_date": through_date.isoformat() if through_date else None,
        "through_date": end.isoformat() if end else None,
        "expected_count": 5, "session_dates": [], "items": {},
    }
    if end is None:
        return result
    # Reuse the existing local-only confirmed-calendar contract. Its query must
    # not flush caller-owned ORM state; it never fetches or writes calendars.
    with db.no_autoflush:
        sessions = await expected_index_trade_days(db, through_date=end, window=5)
    if len(sessions) != 5:
        return result
    result.update(status="calendar_known", session_dates=[day.isoformat() for day in sessions])
    if requested == ():
        return result
    statement = select(*(getattr(FundFlow, key) for key in _WINDOW_FIELDS)).where(
        FundFlow.trade_date.in_(sessions),
    )
    if requested is not None:
        statement = statement.where(FundFlow.code.in_(requested))
    rows = (await db.execute(statement.execution_options(autoflush=False))).mappings()
    by_code = {}
    for row in rows:
        by_code.setdefault(str(row["code"]), {})[row["trade_date"]] = row
    for code in requested if requested is not None else sorted(by_code):
        history = []
        counts = {}
        for day in sessions:
            row = by_code.get(code, {}).get(day)
            status = closed_fund_status(row, decision_at=cutoff)
            counts[status] = counts.get(status, 0) + 1
            history.append({
                "trade_date": day.isoformat(), "status": status,
                "main_net_inflow": float(row["main_net_inflow"]) if status == "dated_known" else None,
            })
        complete = counts.get("dated_known", 0) == 5
        total = sum(item["main_net_inflow"] for item in history) if complete else None
        if total is not None and not math.isfinite(total):
            complete, total = False, None
            counts["invalid_total"] = 1
        result["items"][code] = {
            "total": total, "complete": complete,
            "status": "dated_known" if complete else "incomplete",
            "valid_count": counts.get("dated_known", 0), "status_counts": counts,
            "history": history,
        }
    return result


def fund_window_payload(window: dict, code: str) -> dict:
    """Small JSON projection for ranking/detail UI; missing is never measured zero."""
    item = window.get("items", {}).get(code, {})
    total = item.get("total")
    return {
        "fund_5d_billion": total / 1e8 if total is not None else None,
        "fund_5d_complete": bool(item.get("complete")),
        "fund_5d_count": item.get("valid_count", 0),
        "fund_5d_status": item.get("status", window["status"]),
        "fund_5d_window": {
            key: window[key] for key in (
                "version", "basis", "decision_at", "through_date", "session_dates", "expected_count",
            )
        },
    }
