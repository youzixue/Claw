"""Bounded, SELECT-only C3 event ledger; never calls scanners or account services."""

import json
import math
from datetime import date

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import PaperShadowEvent

SNAPSHOT_MAX_CHARS = 65536
EVENT_TYPES = frozenset({
    "all", "confirmed", "eligible", "reset", "block", "confirmation_reset",
    "coverage_blocked", "evidence_blocked", "session_blocked", "outcome_blocked",
    "structural_pool", "confirmation_sample", "universe_audit", "control",
    "session_ready", "session_outcome",
})


def _leaf(value):
    if isinstance(value, str):
        return value[:1000]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return None


def _snapshot_summary(raw):
    if raw is None:
        return {"snapshot_status": "oversized", "reason": None, "confirmation": {}}
    try:
        snapshot = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return {"snapshot_status": "invalid", "reason": None, "confirmation": {}}
    if not isinstance(snapshot, dict):
        return {"snapshot_status": "invalid", "reason": None, "confirmation": {}}
    prior = snapshot.get("prior_structure")
    prior = prior if isinstance(prior, dict) else {}
    confirmation = prior.get("confirmation")
    confirmation = confirmation if isinstance(confirmation, dict) else {}
    quote = snapshot.get("quote")
    quote = quote if isinstance(quote, dict) else {}
    return {
        "snapshot_status": "available",
        "reason": _leaf(prior.get("reason")),
        "structure": _leaf(prior.get("structure")),
        "causal_status": _leaf(snapshot.get("causal_status")),
        "source_quote_at": _leaf(quote.get("source_quote_at")),
        "confirmation": {
            key: _leaf(confirmation[key]) for key in (
                "ready", "sample_count", "persistence_sec", "first_sample_at",
                "source_sample_count", "source_persistence_sec", "confirmation_version",
            ) if key in confirmation
        },
    }


async def list_c3_records(
    db: AsyncSession, *, trade_date: date | None = None, keyword: str = "",
    event_type: str = "confirmed", version: str = "all", page: int = 1,
    page_size: int = 20,
) -> dict:
    # Only pure version calculation is reused; no comparison/scanning/account path.
    from app.paper.strategy_iteration_shadow import ROUTE_C3, route_version_for

    if event_type not in EVENT_TYPES or version not in {"all", "current"}:
        raise ValueError("Unsupported C3 event/version filter")
    if not 1 <= page <= 1000000 or not 1 <= page_size <= 100 or len(keyword) > 80:
        raise ValueError("Invalid C3 pagination or keyword")
    current_version = route_version_for(ROUTE_C3)
    e = PaperShadowEvent
    conditions = [e.route_id == ROUTE_C3]
    if trade_date is not None:
        conditions.append(e.trade_date == trade_date)
    if version == "current":
        conditions.append(e.route_version == current_version)
    if event_type == "reset":
        conditions.append(e.event_type == "confirmation_reset")
    elif event_type == "block":
        conditions.append(e.event_type.endswith("_blocked", autoescape=True))
    elif event_type != "all":
        conditions.append(e.event_type == event_type)
    keyword = keyword.strip()
    if keyword:
        conditions.append(or_(
            e.code.contains(keyword, autoescape=True),
            e.name.contains(keyword, autoescape=True),
        ))
    # Do not hydrate Text snapshots/ORM entities. Oversized snapshots (notably
    # universe frames) remain in the denominator but never cross into Python.
    bounded_snapshot = case(
        (func.length(e.snapshot_json) <= SNAPSHOT_MAX_CHARS, e.snapshot_json),
        else_=None,
    ).label("bounded_snapshot")
    statement = select(
        e.id, e.event_key, e.route_id, e.route_version, e.trade_date, e.observed_at,
        e.code, e.name, e.event_type, e.status, e.price, e.assumed_fill_price,
        e.change_pct,
    ).where(*conditions).order_by(
        e.trade_date.desc(), e.observed_at.desc(), e.id.desc(),
    ).offset((page - 1) * page_size).limit(page_size)
    with db.no_autoflush:
        total = int(await db.scalar(select(func.count(e.id)).where(*conditions)) or 0)
        rows = (await db.execute(statement)).mappings().all()
        # Page metadata first: a sort must never evaluate snapshot expressions
        # for the full filtered history (universe frames can total gigabytes).
        snapshots = {}
        if rows:
            snapshots = dict((await db.execute(
                select(e.id, bounded_snapshot).where(e.id.in_([row["id"] for row in rows]))
            )).all())
    from app.push.paper_buy_points import c3_research_notification_receipts
    with db.no_autoflush:
        receipts = await c3_research_notification_receipts(
            db, event_keys=[row["event_key"] for row in rows if row["event_type"] == "confirmed"],
        )
    items = []
    for row in rows:
        item = dict(row)
        for key in ("price", "assumed_fill_price", "change_pct"):
            item[key] = _leaf(item[key])
        item["confirmed_price"] = item["price"] if item["event_type"] == "confirmed" else None
        item.update(_snapshot_summary(snapshots.get(row["id"])))
        item["execution_signal"] = False
        receipt = receipts.get(row["event_key"])
        if row["event_type"] != "confirmed":
            notification = {"status": "not_applicable", "cause": "not_confirmed_event"}
        elif receipt is None:
            notification = {"status": "not_queued", "cause": "no_notification_receipt"}
        else:
            notification = {
                key: _leaf(receipt.get(key)) for key in (
                    "status", "cause", "previous_status", "previous_cause",
                    "shadow_event_id", "route_version", "signal_observed_at", "checked_at",
                    "send_started_at", "send_completed_at", "signal_log_id", "delivery_log_id",
                )
            }
        notification.update(user_received_at=None, research_only=True, execution_connected=False)
        item["notification"] = notification
        items.append(item)
    return {
        "items": items, "total": total, "page": page, "page_size": page_size,
        "route_id": ROUTE_C3, "current_version": current_version,
        "filters": {"trade_date": trade_date, "keyword": keyword,
                    "event_type": event_type, "version": version},
        "read_only": True,
        "notice": "C3前向研究检测记录，非执行信号，本记录未下单；确认不代表涨停或策略盈利。"
                  "默认跨全部历史版本；未涨停、重置和拦截事件不删除。历史未入队不等于发送失败；"
                  "通道发送成功不代表用户已收到。",
    }
