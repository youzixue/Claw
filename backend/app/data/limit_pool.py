"""Prospective limit-pool projections over the existing Tencent quote round.

No HTTP here. Per-code verified quotes retire stale states; missing codes never
mean an empty market. Wencai enriches existing Tencent states, never supplies
price state or overwrites historical dates.
"""
from __future__ import annotations

import json
from datetime import datetime, time

from sqlalchemy import delete, desc, select
from sqlalchemy.dialects.sqlite import insert

from app.config.settings import settings
from app.data.fund_flow_clock import local_clock
from app.data.limit_pool_source import TENCENT_LIMIT_VERSION, project_tencent_limits
from app.models.stock import BrokenLimitPool, LimitDownPool, LimitUpPool, QuoteRound

DETAIL_FIELDS = ("consecutive_days", "break_count", "limit_up_time", "seal_amount", "limit_up_reason")
REQUIRED_DETAILS = ("consecutive_days", "break_count", "limit_up_time", "seal_amount")


def evidence_dict(value):
    try:
        decoded = json.loads(value or "{}")
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError):
        return {}


def details_current(evidence: dict, observed_at: datetime) -> bool:
    detail = evidence.get("wencai") or {}
    try:
        clock = local_clock(datetime.fromisoformat(detail["observed_at"]))
    except (KeyError, ValueError, TypeError):
        return False
    if clock is None or clock.date() != observed_at.date() or clock > observed_at:
        return False
    # Only an actually observed post-close response may persist to the evening.
    return (clock.time() >= time(15, 0)
            or (observed_at - clock).total_seconds() <= settings.LIMIT_POOL_WENCAI_INTERVAL_SEC)


def limit_details_complete(row) -> bool:
    return all(getattr(row, field, None) is not None for field in REQUIRED_DETAILS)


def limit_row_usable(row, *, decision_at):
    """New states require visible clocks plus bounded metadata at consumption."""
    source, observed = local_clock(row.source_quote_at), local_clock(row.observed_at)
    if (not limit_details_complete(row) or source is None or observed is None
            or source.date() != row.trade_date or source > observed or observed > decision_at):
        return False
    if source.time() < time(15, 0) and (
        decision_at - source
    ).total_seconds() > settings.LIMIT_POOL_SOURCE_MAX_AGE_SEC:
        return False
    metadata_at = decision_at if decision_at.date() == row.trade_date else datetime.combine(row.trade_date, time(23, 59))
    return details_current(evidence_dict(row.evidence_json), metadata_at)



async def limit_pool_health(session, *, trade_date, decision_at, require_close=False):
    """Use actual projected universe evidence, never 'one pool row means complete'."""
    quote = await session.scalar(select(QuoteRound).where(
        QuoteRound.trade_date == trade_date, QuoteRound.committed_at <= decision_at,
    ).order_by(desc(QuoteRound.committed_at)).limit(1))
    if quote is None:
        return None  # Historical legacy data keeps its old audit contract.
    state = evidence_dict(quote.component_watermarks_json).get("limit_pool")
    if not isinstance(state, dict):
        return None
    rows = list((await session.scalars(select(LimitUpPool).where(
        LimitUpPool.trade_date == trade_date, LimitUpPool.quarantined.is_(False),
    ))).all())
    unknown = stale = complete = 0
    for row in rows:
        source_at, observed = local_clock(row.source_quote_at), local_clock(row.observed_at)
        evidence = evidence_dict(row.evidence_json)
        if (row.source_version != TENCENT_LIMIT_VERSION or source_at is None or observed is None
                or observed > decision_at or source_at.date() != trade_date):
            stale += 1
            continue
        close_known = source_at.time() >= time(15, 0)
        if ((require_close and not close_known)
                or (not close_known and (decision_at - source_at).total_seconds()
                    > settings.LIMIT_POOL_SOURCE_MAX_AGE_SEC)):
            stale += 1
            continue
        metadata_at = decision_at if decision_at.date() == trade_date else datetime.combine(trade_date, time(23, 59))
        if not limit_details_complete(row) or not details_current(evidence, metadata_at):
            unknown += 1
            continue
        complete += 1
    coverage = float(state.get("coverage") or 0)
    # No limit-up rows can be a genuine zero only with a fully observed current
    # universe. A stale round is not refreshed by an empty result.
    quote_close = float(state.get("close_coverage") or 0) >= settings.DATA_COMPLETENESS_MIN
    round_current = (quote_close if require_close else (
        quote_close or 0 <= (decision_at - quote.committed_at).total_seconds()
        <= settings.LIMIT_POOL_SOURCE_MAX_AGE_SEC))
    ready = (coverage >= settings.DATA_COMPLETENESS_MIN and round_current
             and unknown == 0 and stale == 0)
    return {**state, "ready": ready, "status": "ok" if ready else "blocked",
            "detail_complete_count": complete, "detail_unknown_count": unknown,
            "stale_state_count": stale, "round_current": round_current,
            "required_close": require_close, "round_id": quote.round_id}


async def persist_tencent_limit_state(session, records, *, observed_at, expected_count):
    """Same transaction as StockSpot/QuoteRound; no new polling or per-stock SELECT."""
    frame = project_tencent_limits(records, observed_at=observed_at,
                                   max_age_sec=settings.LIMIT_POOL_SOURCE_MAX_AGE_SEC)
    clocks = {str(row["code"]): local_clock(row.get("source_quote_at")) for row in records
              if str(row.get("code")) in frame["valid_codes"]}
    existing = {}
    regressed = set()
    for model in (LimitUpPool, LimitDownPool, BrokenLimitPool):
        rows = list((await session.scalars(select(model).where(model.trade_date == observed_at.date()))).all())
        existing[model] = {row.code: row for row in rows}
        for row in rows:
            previous = local_clock(row.source_quote_at)
            if row.code in clocks and previous is not None and clocks[row.code] < previous:
                regressed.add(row.code)
    valid_codes = frame["valid_codes"] - regressed
    for model, key in ((LimitUpPool, "up"), (LimitDownPool, "down"), (BrokenLimitPool, "broken")):
        projected = [row for row in frame[key] if row["code"] in valid_codes]
        alive = {row["code"] for row in projected}
        retired_ids = [row.id for code, row in existing[model].items()
                       if code in valid_codes and code not in alive]
        if retired_ids:
            await session.execute(delete(model).where(model.id.in_(retired_ids)))
        for record in projected:
            # ORM DateTime is naive local-market time; retain original offset in
            # evidence_json rather than silently storing UTC as Shanghai time.
            record["source_quote_at"] = local_clock(record["source_quote_at"])
            record["observed_at"] = local_clock(record["observed_at"])
            if model is LimitUpPool:
                old = existing[model].get(record["code"])
                evidence = evidence_dict(record.get("evidence_json"))
                old_evidence = evidence_dict(old.evidence_json) if old else {}
                if (old and old.source_version == TENCENT_LIMIT_VERSION
                        and details_current(old_evidence, observed_at)):
                    for field in DETAIL_FIELDS:
                        record[field] = getattr(old, field)
                    evidence["wencai"] = old_evidence["wencai"]
                record["evidence_json"] = json.dumps(evidence, ensure_ascii=False)
        if projected:
            # Explicit NULLs avoid ORM defaults turning unknown counters into 0/1.
            columns = [column.name for column in model.__table__.columns if column.name != "id"]
            values = [{column: row.get(column) for column in columns} for row in projected]
            statement = insert(model.__table__)
            statement = statement.on_conflict_do_update(
                index_elements=["code", "trade_date"],
                set_={column: getattr(statement.excluded, column) for column in columns
                      if column not in {"code", "trade_date"}},
            )
            await session.execute(statement, values)
    expected = max(int(expected_count), 0)
    return {
        "source_version": TENCENT_LIMIT_VERSION,
        "observed_at": observed_at.isoformat(),
        "expected_count": expected,
        "valid_count": len(valid_codes),
        "coverage": min(len(valid_codes) / expected, 1.0) if expected else 0.0,
        "close_coverage": (sum(clocks[code].time() >= time(15, 0) for code in valid_codes)
                           / expected if expected else 0.0),
        "rejected_count": frame["rejected_count"],
        "regressed_count": len(regressed),
        "up_count": sum(row["code"] in valid_codes for row in frame["up"]),
        "down_count": sum(row["code"] in valid_codes for row in frame["down"]),
        "broken_count": sum(row["code"] in valid_codes for row in frame["broken"]),
        "scope": "verified_quotes_only_not_absence_as_negative",
    }


async def supplement_wencai_limit_details(session, details, *, requested_at, observed_at):
    """Apply only dated details known after the Tencent state, never historical state."""
    if requested_at.date() != observed_at.date():
        raise ValueError("wencai request crossed trade date")
    rows = list((await session.scalars(select(LimitUpPool).where(
        LimitUpPool.trade_date == observed_at.date(),
        LimitUpPool.source_version == TENCENT_LIMIT_VERSION,
    ))).all())
    updated = 0
    for row in rows:
        fields = details.get(row.code)
        source_at = local_clock(row.source_quote_at)
        # A quote arriving while HTTP was in flight must not receive pre-request
        # metadata. The next low-frequency request can enrich it safely.
        if not fields or source_at is None or source_at > requested_at:
            continue
        # Do not enrich a stale intraday state from an unrelated newer answer.
        if source_at.time() < time(15, 0) and (
            requested_at - source_at
        ).total_seconds() > settings.LIMIT_POOL_SOURCE_MAX_AGE_SEC:
            continue
        evidence = evidence_dict(row.evidence_json)
        prior = evidence.get("wencai") or {}
        if str(prior.get("observed_at") or "") > observed_at.isoformat():
            continue
        fields = dict(fields)
        first_time = fields.get("limit_up_time")
        if first_time:
            try:
                first_clock = datetime.combine(row.trade_date, time.fromisoformat(first_time))
                if first_clock > requested_at or not time(9, 25) <= first_clock.time() <= time(15, 0):
                    fields["limit_up_time"] = None
            except (ValueError, TypeError):
                fields["limit_up_time"] = None
        for field in DETAIL_FIELDS:
            setattr(row, field, fields.get(field))
        evidence["wencai"] = {
            "source": "wencai_stream", "source_version": "wencai_dated_limit_details_v1",
            "requested_at": requested_at.isoformat(), "observed_at": observed_at.isoformat(),
            "date_precision": "trade_date", "provider_timestamp": None,
            "known_fields": [field for field in DETAIL_FIELDS if fields.get(field) is not None],
        }
        row.evidence_json = json.dumps(evidence, ensure_ascii=False)
        updated += 1
    await session.flush()
    return updated
