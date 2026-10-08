"""SELECT-only premarket evidence; never refresh calendars, news or dashboards.

The caller owns an independent, clean, database-enforced read-only AsyncSession.
Naive clocks are explicitly Asia/Shanghai. Mutable calendar/Kline/dashboard
projections are labelled as such, not certified historical availability.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from statistics import median

from sqlalchemy import DateTime, LargeBinary, String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dashboard2.cache import DashboardSnapshotCache
from app.dashboard2.mapping import GLOBAL_TO_CN_SECTOR_MAPPING
from app.models.governance import DashboardSnapshot, TradeCalendarModel
from app.models.news import FinanceNews, NewsAnalysisVersion, NewsContentVersion
from app.models.review import DailyReviewSnapshot
from app.models.stock import StockKline
from app.news.catalyst import NEWS_EVIDENCE_PROTOCOL, load_news_evidence_as_of, news_evidence_statement

CONTEXT_VERSION = "dsh_overnight_evidence_v1"
_MAX_ITEMS = 500
_MAX_READER_ROWS = 1000
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
_FUSION_VERSION = "premarket_dimension_fusion_v1_20261002"
_CALENDAR_LOOKBACK_DAYS = 370
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NEW_YORK = ZoneInfo("America/New_York")
_CONTENT_METADATA_COLUMNS = tuple(getattr(NewsContentVersion, key) for key in (
    "id", "news_id", "content_hash", "origin", "source", "publish_time",
    "first_received_at", "received_at", "recorded_at", "content_available_at",
    "entity_verified_at", "protocol_version",
))
_ANALYSIS_METADATA_COLUMNS = tuple(getattr(NewsAnalysisVersion, key) for key in (
    "id", "content_version_id", "status", "analysis_completed_at", "available_at",
    "result_hash", "protocol_version",
))


def _iso(value):
    return value.isoformat() if value is not None else None


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _bounded_limit(limit: int) -> int:
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    return min(limit, _MAX_ITEMS)


def _require_naive(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("as_of must be an explicit Asia/Shanghai naive datetime")


def _failed(source: str, exc: Exception) -> dict:
    # Do not leak SQL parameters, connection strings or credentials.
    return {"status": "failed", "source": source, "error_type": type(exc).__name__}


def _alignment(expected: date | None, actual: date | None) -> str:
    if actual is None:
        return "missing"
    if expected is None:
        return "calendar_unknown"
    if expected == actual:
        return "matched"
    return "stale" if actual < expected else "unexpected_newer"


async def _read_calendar(db: AsyncSession, target: date) -> dict:
    target_row = await db.scalar(select(TradeCalendarModel).where(
        TradeCalendarModel.trade_date == target,
    ))
    previous = await db.scalar(select(TradeCalendarModel.trade_date).where(
        TradeCalendarModel.trade_date < target,
        TradeCalendarModel.is_trade_day.is_(True),
    ).order_by(TradeCalendarModel.trade_date.desc()).limit(1))
    lower = max(previous or target, target - timedelta(days=_CALENDAR_LOOKBACK_DAYS))
    rows = (await db.execute(select(
        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day,
    ).where(TradeCalendarModel.trade_date.between(lower, target)))).all()
    known = {row.trade_date: row.is_trade_day for row in rows}
    missing = []
    day = lower
    while day <= target:
        if day not in known:
            missing.append(day.isoformat())
        day += timedelta(days=1)
    complete = previous is not None and previous >= lower and not missing
    expected = previous if complete else None
    return {
        "status": "available" if complete else "calendar_unknown",
        "source": "trade_calendar",
        "target_status": ("trade_day" if target_row.is_trade_day else "closed")
        if target_row is not None else "calendar_unknown",
        "target_session_type": target_row.session_type if target_row is not None else None,
        "target_note": target_row.note if target_row is not None else None,
        "expected_previous": _iso(expected),
        "recorded_previous_candidate": _iso(previous),
        "coverage": {"checked_start": _iso(lower), "checked_end": _iso(target),
                     "complete": complete, "missing_dates": missing,
                     "lookback_limit_days": _CALENDAR_LOOKBACK_DAYS},
        "provenance": {"read_mode": "stored_calendar_only", "auto_sync": False,
                       "historical_availability_clock": "unavailable"},
    }


async def _read_kline_dates(db: AsyncSession, target: date) -> dict:
    actual = await db.scalar(select(func.max(StockKline.trade_date)).where(
        StockKline.trade_date < target,
    ))
    trusted = await db.scalar(select(func.max(StockKline.trade_date)).join(
        TradeCalendarModel, TradeCalendarModel.trade_date == StockKline.trade_date,
    ).where(
        StockKline.trade_date < target, TradeCalendarModel.is_trade_day.is_(True),
        StockKline.source.in_(("ths", "tencent_close")), StockKline.close > 0,
        StockKline.close < float("inf"),
    ))
    sources = (await db.execute(select(
        StockKline.source, func.count().label("count"),
    ).where(StockKline.trade_date == actual).group_by(StockKline.source)
      .order_by(StockKline.source).limit(50))).all() if actual is not None else []
    return {
        "status": "available" if actual is not None else "unavailable",
        "source": "stock_kline",
        "actual_kline_date": _iso(actual),
        "previous_actual_trusted_trade_date": _iso(trusted),
        "source_counts_at_actual_date": [
            {"source": row.source, "count": row.count} for row in sources
        ],
        "provenance": {"historical_pit": False, "availability_clock": "unavailable",
                       "anchor_rule": "stored_open_day_with_positive_ths_or_tencent_close",
                       "claim_boundary": "date_anchor_not_full_market_or_price_truth"},
    }


def _frozen_dimensions(payload_json: str) -> dict:
    """Bounded prior snapshot analysis, never rebuilt from today's mutable projections."""
    if not isinstance(payload_json, str):
        return {"status": "unavailable", "reason": "invalid_snapshot_payload"}
    if len(payload_json) > _MAX_SNAPSHOT_BYTES or len(payload_json.encode()) > _MAX_SNAPSHOT_BYTES:
        return {"status": "unavailable", "reason": "snapshot_payload_byte_budget"}
    try:
        payload = json.loads(payload_json)
        dimensions = payload.get("dimensions") if isinstance(payload, dict) else None
    except (ValueError, TypeError, RecursionError):
        dimensions = None
    if not isinstance(dimensions, dict):
        return {"status": "unavailable", "reason": "frozen_dimensions_missing"}
    truncated = []
    def bounded(value, path, depth=0):
        if depth > 5:
            truncated.append(path)
            return None
        if isinstance(value, dict):
            keys = list(value)[:40]
            if len(value) > 40:
                truncated.append(path)
            return {key: bounded(value[key], path + "." + key, depth + 1) for key in keys}
        if isinstance(value, list):
            if len(value) > 30:
                truncated.append(path)
            return [bounded(item, path + "[]", depth + 1) for item in value[:30]]
        if isinstance(value, str):
            if len(value) > 800:
                truncated.append(path)
            return value[:800]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value if value is None or isinstance(value, (int, float, bool)) else None
    items = {key: bounded(dimensions[key], key) for key in ("fundamental", "technical", "capital")
             if isinstance(dimensions.get(key), dict)}
    return {"status": "available" if len(items) == 3 else "partial",
            "items": items, "missing": [key for key in ("fundamental", "technical", "capital") if key not in items],
            "truncated_paths": truncated[:40], "source": "previous_immutable_postmarket_payload",
            "scope": "snapshot_stored_statistics_not_full_market_recomputation",
            "individual_input_availability_certified": False}


async def _read_postmarket(db: AsyncSession, target: date, as_of: datetime) -> dict:
    # Gate payload before hydration. SQLite length(TEXT) counts characters, not
    # UTF-8 bytes; PostgreSQL octet_length preserves exact text byte semantics.
    table = DailyReviewSnapshot
    def sql_bytes(col):
        return (func.octet_length(cast(col, String)) if db.get_bind().dialect.name == "postgresql"
                else func.length(cast(col, LargeBinary)))
    byte_count = sql_bytes(table.payload_json)
    payload = case((byte_count <= _MAX_SNAPSHOT_BYTES, table.payload_json), else_=None)
    columns = [table.id]
    for key in ("review_date", "analysis_trade_date", "as_of_at", "created_at"):
        col = getattr(table, key)
        maximum = 32 if isinstance(col.type, DateTime) else 10
        columns.append(case((sql_bytes(col) <= maximum, col), else_=None).label(key))
    for key in ("data_version", "schema_version", "quality_status"):
        col = getattr(table, key)
        columns.append(case((and_(func.length(col) <= col.type.length,
            sql_bytes(col) <= col.type.length * 4), col), else_=None).label(key))
    columns.extend((payload.label("payload_json"), byte_count.label("payload_byte_count")))
    row = (await db.execute(select(*columns).where(
        DailyReviewSnapshot.phase == "postmarket",
        DailyReviewSnapshot.review_date < target,
        # Select latest visible first; a bad analysis date must not hide it.
        DailyReviewSnapshot.as_of_at <= as_of,
        DailyReviewSnapshot.created_at <= as_of,
    ).order_by(DailyReviewSnapshot.review_date.desc(),
               DailyReviewSnapshot.as_of_at.desc(), DailyReviewSnapshot.id.desc()).limit(1))).one_or_none()
    if row is None:
        return {"status": "unavailable", "source": "daily_review_snapshot",
                "reason": "no_previous_postmarket_snapshot_visible_by_cutoff", "snapshot_id": None}
    payload_valid = (isinstance(row.payload_json, str) and row.payload_byte_count is not None
                     and row.payload_byte_count <= _MAX_SNAPSHOT_BYTES)
    valid = (isinstance(row.as_of_at, datetime) and isinstance(row.created_at, datetime)
             and row.as_of_at.date() == row.review_date
             and row.as_of_at.time() >= time(15) and row.as_of_at <= row.created_at
             and row.analysis_trade_date == row.review_date and payload_valid
             and all(isinstance(getattr(row, k), str) and getattr(row, k).strip()
                     for k in ("data_version", "schema_version", "quality_status")))
    invalid_reason = ("snapshot_payload_byte_budget" if row.payload_byte_count is not None
                      and row.payload_byte_count > _MAX_SNAPSHOT_BYTES else "invalid_postmarket_snapshot")
    return {
        "status": "available" if valid else "invalid",
        "source": "daily_review_snapshot", "snapshot_id": row.id,
        "review_date": _iso(row.review_date), "analysis_trade_date": _iso(row.analysis_trade_date),
        "as_of_at": _iso(row.as_of_at), "created_at": _iso(row.created_at),
        "data_version": row.data_version, "schema_version": row.schema_version,
        "quality_status": row.quality_status,
        "dimensions": _frozen_dimensions(row.payload_json) if valid else {
            "status": "unavailable", "reason": invalid_reason},
        "payload_hash": hashlib.sha256(row.payload_json.encode()).hexdigest() if payload_valid else None,
        "payload_byte_count": row.payload_byte_count, "payload_byte_limit": _MAX_SNAPSHOT_BYTES,
        "provenance": {"immutable_snapshot": True,
                       "availability_rule": "as_of_at_and_created_at_not_after_cutoff",
                       "later_kline_repairs_not_merged": True},
    }


def _fusion_statistics(postmarket, news, after_hours, *, expected, as_of):
    """Research summary of already-read evidence; never query/recompute/authorize."""
    def number_ok(value):
        if type(value) not in (int, float):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False
    fields = {
        "fundamental": ("candidate_count", "requested_candidate_count", "coverage_ratio",
            "valid_pe_count", "valid_pb_count", "valid_growth_count",
            "median_pe_ttm", "median_pb", "median_net_profit_growth"),
        "technical": ("universe_count", "change_coverage", "advance_ratio", "median_return",
            "limit_up_count", "limit_down_count", "broken_limit_count", "seal_rate", "board_height"),
        "capital": ("market_main_net_inflow", "sampled_stock_main_net_inflow",
            "sampled_stock_count", "fund_flow_record_count"),
    }
    dims = postmarket.get("dimensions", {}).get("items", {})
    matched = (expected is not None and postmarket.get("status") == "available"
               and postmarket.get("analysis_trade_date") == expected.isoformat())
    summary = {}
    for name, keys in fields.items():
        value = dims.get(name, {})
        reason = None
        dates = [value.get(key) for key in ("source_trade_date", "analysis_trade_date", "technical_trade_date")
                 if value.get(key) is not None]
        if not matched:
            reason = "previous_snapshot_unavailable_or_date_not_aligned"
        elif not value:
            reason = "frozen_dimension_missing"
        elif value.get("status") in ("unavailable", "failed", "invalid") or value.get("source") == "unavailable":
            reason = "frozen_dimension_source_unavailable"
        elif not dates:
            reason = "frozen_dimension_date_unknown"
        elif any(stamp != expected.isoformat() for stamp in dates):
            reason = "frozen_dimension_date_not_aligned"
        stats = {}
        for key in keys:
            number = value.get(key)
            valid = number_ok(number)
            if key.endswith("_count"):
                valid = valid and type(number) is int and number >= 0
            stats[key] = number if valid and reason is None else None
        missing = [key for key, number in stats.items() if number is None]
        summary[name] = {
            "status": "unavailable" if reason or len(missing) == len(keys) else (
                "partial" if missing else "available"),
            "reason": reason, "statistics": stats, "missing_statistics": missing,
            "source": "previous_immutable_postmarket_payload",
            "source_trade_date": value.get("source_trade_date"),
            "input_status": value.get("status"), "input_source": value.get("source"),
            "snapshot_id": postmarket.get("snapshot_id"), "payload_hash": postmarket.get("payload_hash"),
            "snapshot_available_at": postmarket.get("created_at"),
            "data_version": postmarket.get("data_version"), "quality_status": postmarket.get("quality_status"),
            "scope": value.get("scope") or value.get("sample_basis") or "snapshot_stored_statistics",
            "individual_input_availability_certified": False,
        }
    news_keys = ("count", "selected_count", "positive_count", "negative_count", "neutral_count", "unanalyzed_count")
    summary["news"] = {
        "status": news.get("status", "unavailable"),
        "statistics": {key: news.get(key) for key in news_keys},
        "source": news.get("source"), "window_start": news.get("window_start"),
        "as_of_at": news.get("as_of_at"), "count_scope": news.get("count_scope"),
        "coverage": {key: news.get("coverage", {}).get(key) for key in
            ("count_complete", "reader_truncated_or_unknown", "selection_truncated", "known_zero_news")},
        "claim_boundary": "bounded_visible_model_sentiment_not_certified_benefit_or_all_news",
    }
    items = after_hours.get("items", [])
    ratios = {key: [item[key] for item in items
                   if number_ok(item.get(key)) and 0 <= item[key] <= 1]
              for key in ("after_volume_ratio", "after_amount_ratio")}
    summary["after_hours"] = {
        "status": "partial" if items else "unavailable", "source": after_hours.get("source"),
        "trade_date": after_hours.get("trade_date"), "as_of_at": after_hours.get("as_of_at"),
        "returned_code_count": len(items), "stored_code_count": after_hours.get("stored_code_count"),
        "truncated": after_hours.get("truncated"), "complete_market_coverage": False,
        "statistics": {key: {"valid_count": len(values), "missing_count": len(items) - len(values),
                             "median": median(values) if values else None} for key, values in ratios.items()},
        "count_scope": "bounded_returned_codes_not_market_universe",
        "median_scope": "returned_code_sample_not_collector_universe",
        "source_finality_verified": False, "trading_authority": False,
    }
    return {"protocol": _FUSION_VERSION, "as_of_at": as_of.isoformat(),
            "expected_previous": _iso(expected), "dimensions": summary,
            "missing_or_partial_dimensions": [name for name, value in summary.items()
                                               if value["status"] != "available"],
            "automatic_weight_update": False, "execution_authorized": False}


def _content_metadata(row: NewsContentVersion) -> dict:
    return {
        "news_id": row.news_id, "content_version_id": row.id,
        "content_hash": row.content_hash, "origin": row.origin, "source": row.source,
        "publish_time": _iso(row.publish_time), "first_received_at": _iso(row.first_received_at),
        "received_at": _iso(row.received_at), "recorded_at": _iso(row.recorded_at),
        "content_available_at": _iso(row.content_available_at),
        "entity_verified_at": _iso(row.entity_verified_at),
        "evidence_protocol": row.protocol_version,
    }


def _news_item(row, version: NewsContentVersion, attempt, latest_attempt) -> dict:
    codes, sectors = list(row.related_codes), list(row.related_sectors)
    role = dict(row.role_evidence)
    entities = role.get("entities", [])
    role.update(entities=entities[:50], entity_count=len(entities),
                entities_truncated=len(entities) > 50,
                candidate_codes=role.get("candidate_codes", [])[:50])
    has_codes, has_sectors = bool(codes), bool(sectors)
    item = {
        **_content_metadata(version),
        # Existing daily-review payload leaves remain compatible.
        "id": row.news_id, "title": row.title[:400], "summary": row.summary[:800],
        "importance": row.importance, "category": None, "nlp_status": row.nlp_status,
        "bull_bear": row.bull_bear, "sentiment": row.sentiment,
        "confidence": row.bull_bear_confidence, "related_codes": codes[:50],
        "related_sectors": sectors[:50], "impact_reason": None,
        "impact_scope": ("stock_and_sector" if has_codes and has_sectors else
                         "stock" if has_codes else "sector" if has_sectors else "market_or_unmapped"),
        "has_specific_target": has_codes or has_sectors,
        "related_code_count": len(codes), "related_sector_count": len(sectors),
        "targets_truncated": len(codes) > 50 or len(sectors) > 50,
        "text_truncated": len(row.title) > 400 or len(row.summary) > 800,
        "content_excerpt": row.content[:800], "content_excerpt_truncated": len(row.content) > 800,
        "analysis_version_id": row.analysis_version_id,
        "analysis_completed_at": _iso(row.analysis_completed_at),
        "analysis_available_at": _iso(attempt.available_at) if attempt is not None else None,
        "analysis_result_hash": attempt.result_hash if attempt is not None else None,
        "latest_visible_analysis_attempt": {
            "analysis_version_id": latest_attempt.id, "status": latest_attempt.status,
            "analysis_completed_at": _iso(latest_attempt.analysis_completed_at),
            "analysis_available_at": _iso(latest_attempt.available_at),
            "result_hash": latest_attempt.result_hash,
            "used_for_fields": latest_attempt.id == row.analysis_version_id,
        } if latest_attempt is not None else None,
        "available_at": _iso(row.available_at), "role_evidence": role,
        "research_only": row.research_only, "positive_beneficiary_codes": [],
        "claim_boundary": "mentions_and_model_sentiment_are_not_certified_economic_benefit",
        "analysis_interpretation": "model_or_fallback_hypothesis_not_verified_benefit",
        "nlp_missing_at_cutoff": row.analysis_version_id is None,
        "evidence_kind": "index_observation_not_news" if row.source == "global" else "news_original",
        "collection_lag_interpretation": "publish_to_receipt_delay_not_after_cutoff_receipt",
        "late_collection": version.received_at > version.publish_time,
        "collection_lag_seconds": (version.received_at - version.publish_time).total_seconds(),
    }
    return item


async def read_news_page(db: AsyncSession, *, trade_date: date, as_of: datetime,
                         cursor: str = "", limit: int = 20) -> dict:
    """Small keyset pages of the entire STORED holiday window, not top-N NLP labels."""
    _require_naive(as_of)
    if as_of.date() != trade_date or as_of.time() >= time(9, 30):
        raise ValueError("news pages require a same-day premarket cutoff")
    if as_of > datetime.now(_SHANGHAI).replace(tzinfo=None):
        raise ValueError("news cutoff cannot be in the future")
    if db.new or db.dirty or db.deleted:
        raise ValueError("news pages require a clean independent read-only session")
    size = min(_bounded_limit(limit), 25)
    calendar = await _read_calendar(db, trade_date)
    previous_raw = calendar.get("expected_previous")
    if not previous_raw:
        return {"status": "unavailable", "reason": "stored_previous_trade_date_unknown", "read_only": True}
    previous = date.fromisoformat(previous_raw)
    start = datetime.combine(previous, time(15))
    scope = hashlib.sha256(f"news_page_v1:{trade_date}:{start.isoformat()}:{as_of.isoformat()}".encode()).hexdigest()
    after = 0
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 256:
                raise ValueError("invalid cursor length")
            token = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            if len(cursor) > 256 or token["scope"] != scope or type(token["after"]) is not int or token["after"] < 0:
                raise ValueError("invalid cursor")
            after = token["after"]
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ValueError("news cursor must match the exact date and cutoff") from exc
    stmt = news_evidence_statement(cutoff=as_of, start_time=start)
    metadata = stmt.with_only_columns(NewsContentVersion.id, NewsContentVersion.source).subquery()
    total = await db.scalar(select(func.count()).select_from(metadata))
    sources = (await db.execute(select(metadata.c.source, func.count()).group_by(metadata.c.source))).all()
    candidates = list((await db.scalars(stmt.where(NewsContentVersion.id > after)
                            .order_by(NewsContentVersion.id).limit(size + 1))).all())
    page = candidates[:size]
    rows = await load_news_evidence_as_of(db, as_of_at=as_of, start_time=start, limit=size,
                                         content_version_ids=[v.id for v in page])
    by_id = {v.id: v for v in page}
    items = [_news_item(row, by_id[row.content_version_id], None, None) for row in rows]
    # Analysis provenance is already validated by the shared loader. Metadata is
    # read only for those selected versions, never from current FinanceNews.
    attempt_ids = [row.analysis_version_id for row in rows if row.analysis_version_id is not None]
    attempts = {a.id: a for a in (await db.scalars(select(NewsAnalysisVersion)
                      .where(NewsAnalysisVersion.id.in_(attempt_ids)))).all()} if attempt_ids else {}
    for item in items:
        attempt = attempts.get(item["analysis_version_id"])
        if attempt is not None:
            item["analysis_available_at"] = _iso(attempt.available_at)
            item["analysis_result_hash"] = attempt.result_hash
        item["backend_analysis_kind"] = item["nlp_status"]
        item["research_model_interpretation"] = "separate_from_backend_nlp_not_written_to_business_db"
        # This means publication-to-receipt polling delay, NOT after-cutoff receipt.
        item["received_after_cutoff"] = False
    items.sort(key=lambda item: item["content_version_id"])
    more = len(candidates) > size
    next_cursor = base64.urlsafe_b64encode(json.dumps({"scope": scope, "after": page[-1].id},
                                         separators=(",", ":")).encode()).decode() if more else None
    return {"status": "available", "read_only": True, "trade_date": trade_date.isoformat(),
            "as_of_at": as_of.isoformat(), "window_start": start.isoformat(), "items": items,
            "total_visible_revision_candidates": total, "candidate_counts_by_source": dict(sources),
            "candidate_count_is_not_hash_validated_article_count": True,
            "page_candidate_count": len(page), "returned": len(items),
            "page_rejected_integrity_count": len(page) - len(items), "next_cursor": next_cursor,
            "stored_window_exhausted": not more, "complete_source_collection_certified": False,
            "full_text_read_certified": False, "truncation": "keyset_paged_max_25_items",
            "scope_fingerprint": scope}


async def _read_news_window(
    db: AsyncSession, *, start_time: datetime, as_of: datetime, limit: int = 100,
) -> dict:
    """Use the existing PIT engine; extra SELECTs are metadata diagnostics only.

    Diagnostic counts include CURRENT stored late/legacy revisions. They are not
    article facts available at as_of, and never supply sentiment or beneficiaries.
    Count is exact only if the stored-revision upper bound fits the reader budget.
    """
    _require_naive(as_of)
    _require_naive(start_time)
    item_limit = _bounded_limit(limit)
    cutoff = min(as_of, datetime.now(_SHANGHAI).replace(tzinfo=None))
    budget = min(_MAX_READER_ROWS, item_limit * 5)
    rows = await load_news_evidence_as_of(
        db, as_of_at=cutoff, start_time=start_time, limit=budget,
    )
    window = (NewsContentVersion.publish_time >= start_time,
              NewsContentVersion.publish_time <= cutoff)
    counts = (await db.execute(select(
        func.count().label("stored_revisions"),
        func.count(case((NewsContentVersion.origin == "legacy_unknown", 1))).label("legacy_unknown"),
        func.count(case((NewsContentVersion.recorded_at <= cutoff, 1))).label("recorded_by_cutoff"),
        func.count(case((NewsContentVersion.received_at > cutoff, 1))).label("received_after_cutoff"),
        func.count(case((NewsContentVersion.content_available_at > cutoff, 1))).label("content_after_cutoff"),
    ).where(*window))).one()
    unversioned = int(await db.scalar(select(func.count()).select_from(FinanceNews).where(
        FinanceNews.publish_time >= start_time, FinanceNews.publish_time <= cutoff,
        ~select(NewsContentVersion.id).where(
            NewsContentVersion.news_id == FinanceNews.id,
        ).exists(),
    )) or 0)
    late_analysis_count = int(await db.scalar(select(func.count()).select_from(
        NewsAnalysisVersion,
    ).join(NewsContentVersion, NewsContentVersion.id == NewsAnalysisVersion.content_version_id)
      .where(*window, or_(NewsAnalysisVersion.available_at > cutoff,
                         NewsAnalysisVersion.analysis_completed_at > cutoff))) or 0)
    excluded = list((await db.execute(select(*_CONTENT_METADATA_COLUMNS).where(
        *window, or_(NewsContentVersion.origin == "legacy_unknown",
                     NewsContentVersion.recorded_at > cutoff,
                     NewsContentVersion.received_at > cutoff,
                     NewsContentVersion.content_available_at > cutoff,
                     NewsContentVersion.entity_verified_at > cutoff),
    ).order_by(NewsContentVersion.id.desc()).limit(item_limit + 1))).all())
    complete = counts.recorded_by_cutoff <= budget
    selected = sorted(rows, key=lambda r: (
        r.importance, r.publish_time, r.content_version_id,
    ), reverse=True)[:item_limit]
    version_ids = [r.content_version_id for r in selected]
    versions = {v.id: v for v in (await db.execute(select(*_CONTENT_METADATA_COLUMNS).where(
        NewsContentVersion.id.in_(version_ids),
    ))).all()} if version_ids else {}
    attempt_ids = [r.analysis_version_id for r in selected if r.analysis_version_id is not None]
    attempts = {a.id: a for a in (await db.execute(select(*_ANALYSIS_METADATA_COLUMNS).where(
        NewsAnalysisVersion.id.in_(attempt_ids),
    ))).all()} if attempt_ids else {}
    latest_attempt_ids = select(
        NewsAnalysisVersion.id,
        func.row_number().over(
            partition_by=NewsAnalysisVersion.content_version_id,
            order_by=(NewsAnalysisVersion.available_at.desc(), NewsAnalysisVersion.id.desc()),
        ).label("rank"),
    ).where(
        NewsAnalysisVersion.content_version_id.in_(version_ids),
        NewsAnalysisVersion.available_at <= cutoff,
        NewsAnalysisVersion.analysis_completed_at <= cutoff,
    ).subquery()
    latest_attempts = {a.content_version_id: a for a in (await db.execute(
        select(*_ANALYSIS_METADATA_COLUMNS).join(latest_attempt_ids,
            NewsAnalysisVersion.id == latest_attempt_ids.c.id)
        .where(latest_attempt_ids.c.rank == 1).limit(item_limit),
    )).all()} if version_ids else {}
    items = [_news_item(r, versions[r.content_version_id], attempts.get(r.analysis_version_id),
                        latest_attempts.get(r.content_version_id)) for r in selected]
    analyzed = [r for r in rows if r.nlp_status in {"analyzed", "fallback"}]
    def polarity(row):
        values = {row.bull_bear, row.sentiment}
        return "bull" if values & {"bull", "bullish", "positive"} else (
            "bear" if values & {"bear", "bearish", "negative"} else "neutral")
    polarities = [polarity(r) for r in analyzed]
    diagnostics = {
        "scope": "current_store_metadata_not_asof_article_facts",
        "stored_revision_count": counts.stored_revisions,
        "recorded_by_cutoff_revision_count": counts.recorded_by_cutoff,
        "non_returned_recorded_revision_count": counts.recorded_by_cutoff - len(rows),
        "non_returned_reason": "superseded_invalid_unavailable_or_reader_budget_not_distinguished",
        "legacy_unknown_revision_count": counts.legacy_unknown,
        "unversioned_projection_article_count": unversioned,
        "received_after_cutoff_revision_count": counts.received_after_cutoff,
        "content_available_after_cutoff_revision_count": counts.content_after_cutoff,
        "analysis_after_cutoff_attempt_count": late_analysis_count,
        "excluded_metadata": [
            {**_content_metadata(v),
             "reason": "legacy_unknown" if v.origin == "legacy_unknown" else "not_available_by_cutoff",
             "usable_asof": False} for v in excluded[:item_limit]
        ],
        "excluded_metadata_truncated": len(excluded) > item_limit,
    }
    return {
        "status": "available" if rows else "unavailable",
        "source": "news_content_version+news_analysis_version",
        "evidence_protocol": NEWS_EVIDENCE_PROTOCOL,
        "window_start": _iso(start_time), "as_of_at": _iso(cutoff),
        "requested_as_of_at": _iso(as_of),
        "count": len(rows), "count_scope": "bounded_pit_window",
        "selected_count": len(items), "selection_limit": item_limit,
        "selection_basis": "importance_then_publish_time_within_bounded_pit_page",
        "positive_count": polarities.count("bull"), "negative_count": polarities.count("bear"),
        "neutral_count": polarities.count("neutral"), "unanalyzed_count": len(rows) - len(analyzed),
        "items": items, "diagnostics": diagnostics,
        "coverage": {
            "count_complete": complete, "total_matching_count": len(rows) if complete else None,
            "visible_count_lower_bound": len(rows), "reader_limit": budget,
            "reader_truncated_or_unknown": not complete,
            "selection_truncated": len(rows) > item_limit, "requested_limit": limit,
            "known_zero_news": False, "unobserved_sources": "unknown",
        },
        "freshness": {"cutoff_capped_to_current_clock": cutoff != as_of,
                      "latest_publish_time": _iso(max((r.publish_time for r in rows), default=None)),
                      "latest_available_at": _iso(max((r.available_at for r in rows), default=None))},
        "provenance": {"reader": "app.news.catalyst.load_news_evidence_as_of",
                       "historical_pit": True, "clock_timezone": "Asia/Shanghai",
                       "content_version_ids": version_ids, "analysis_version_ids": attempt_ids,
                       "visible_analysis_attempt_ids": sorted(a.id for a in latest_attempts.values())},
    }


def _us_reference_context(as_of: datetime) -> dict:
    local = as_of.replace(tzinfo=_SHANGHAI).astimezone(_NEW_YORK)
    regular_open = datetime.combine(local.date(), time(9, 30), tzinfo=_NEW_YORK)
    regular_close = datetime.combine(local.date(), time(16), tzinfo=_NEW_YORK)
    late_end = datetime.combine(local.date(), time(20), tzinfo=_NEW_YORK)
    phase = ("non_weekday_reference" if local.weekday() >= 5 else
             "before_regular_reference" if local < regular_open else
             "regular_reference_in_progress" if local < regular_close else
             "late_reference_in_progress" if local < late_end else "after_late_reference_window")
    return {
        "source": "zoneinfo_reference_clock_only", "timezone": "America/New_York",
        "local_as_of": _iso(local), "reference_session_date": _iso(local.date()),
        "utc_offset_seconds": int(local.utcoffset().total_seconds()),
        "dst": bool(local.dst()), "timezone_abbreviation": local.tzname(),
        "calendar_status": "calendar_unknown", "venue_open": None,
        "reference_phase": phase,
        "regular_open_shanghai": _iso(regular_open.astimezone(_SHANGHAI)),
        "regular_close_shanghai": _iso(regular_close.astimezone(_SHANGHAI)),
        "late_end_shanghai": _iso(late_end.astimezone(_SHANGHAI)),
        "late_reference_window_complete": local >= late_end,
        "claim_boundary": "09:30-16:00_and_16:00-20:00_ET_reference_not_NYSE_open_or_quote_evidence",
    }


async def _read_external_observations(db: AsyncSession, as_of: datetime) -> dict:
    """Typed originals archived by existing NewsEngine, not mutable dashboard rows."""
    statement = news_evidence_statement(cutoff=as_of).where(NewsContentVersion.source == "global")
    versions = list((await db.scalars(statement.order_by(NewsContentVersion.recorded_at.desc(),
                               NewsContentVersion.id.desc()).limit(31))).all())
    page = versions[:30]
    rows = await load_news_evidence_as_of(db, as_of_at=as_of, limit=30,
                                          content_version_ids=[version.id for version in page])
    metadata = {v.id: v for v in page}
    items, rejected = [], len(page) - len(rows)
    for row in rows:
        try:
            data = json.loads(row.content)
            if (not isinstance(data, dict) or data.get("kind") != "index_quote_observation"
                    or data.get("protocol") != "global_index_observation_v2_20261008"
                    or data.get("provider") != "akshare:index_global_spot_em"
                    or not isinstance(data.get("code"), str)
                    or data.get("session") != "unknown"
                    or data.get("quote_clock_certified") is not False):
                raise ValueError("not typed evidence")
            from app.news.sources.global_market import validate_index_observation
            validate_index_observation(data)
            fields = {key: data[key] for key in ("code", "name", "price", "previous_close",
                                                "change_pct", "raw_source_time")}
            digest = hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            observed = datetime.fromisoformat(data["collector_observed_at"])
            if (digest != data.get("response_row_hash") or observed.tzinfo is not None
                    or observed > row.received_at or observed > as_of):
                raise ValueError("invalid observation hash/clock")
            version = metadata[row.content_version_id]
            items.append({**data, **_content_metadata(version), "available_at": _iso(row.available_at),
                          "local_observation_usable_asof": True, "usable_asof": False,
                          "session_certified": False, "source_quote_at": None,
                          "observation_age_seconds_at_cutoff": (as_of - observed).total_seconds(),
                          "freshness_scope": "local_receipt_not_verified_latest_exchange_session",
                          "quote_kind": "index_latest_observation",
                          "claim_boundary": "local_received_original_only_not_exchange_session_or_futures"})
        except (ValueError, KeyError, TypeError, AttributeError):
            rejected += 1
    return {"status": "available" if items else "unavailable", "items": items,
            "candidate_count": len(page), "rejected_count": rejected, "truncated": len(versions) > 30,
            "scope": "latest_visible_revision_per_stored_index_not_full_global_market",
            "source": "news_content_version:global", "fetch_performed": False,
            "session_certified": False, "us_index_futures_available": False}


async def _read_external_projection(db: AsyncSession, as_of: datetime) -> dict:
    row = await db.scalar(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_key == DashboardSnapshotCache.SNAPSHOT_KEY,
        DashboardSnapshot.trade_date.is_(None),
    ).order_by(DashboardSnapshot.snapshot_time.desc(), DashboardSnapshot.id.desc()).limit(1))
    expected = sorted(GLOBAL_TO_CN_SECTOR_MAPPING)
    observations = await _read_external_observations(db, as_of)
    base = {
        "forward_observations": observations,
        "source": "dashboard_snapshot:overview-v2", "historical_pit": False,
        "missing_sources": expected, "missing_session_keys": [],
        "unavailable_sources": ["us_index_futures", "complete_us_after_hours"],
        "items": [], "snapshot_id": row.id if row is not None else None,
        "provenance": {"projection": "current_stored_latest_not_historical_PIT",
                       "source_provider": "unknown", "fetch_performed": False},
    }
    if row is None:
        return {**base, "status": "unavailable", "reason": "local_external_cache_missing"}
    try:
        payload = json.loads(row.payload_json)
        if not isinstance(payload, dict) or not isinstance(payload.get("external_factors"), list):
            raise ValueError("invalid external_factors projection")
    except (ValueError, TypeError) as exc:
        return {**base, "status": "failed", "reason": "invalid_external_projection",
                "error_type": type(exc).__name__, "stored_status": row.status,
                "snapshot_time": _iso(row.snapshot_time)}
    raw_items = payload["external_factors"]
    items = []
    for raw in raw_items[:50]:
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            continue
        items.append({
            "key": raw["key"][:100], "label": str(raw.get("label") or "")[:200],
            "price": _finite(raw.get("price")), "change_pct": _finite(raw.get("change_pct")),
            "market": raw.get("market"), "trade_time": raw.get("trade_time"),
            "source": "provider_unknown", "source_timezone": "unknown",
            "session": "unknown", "received_at": None, "available_at": None,
            "usable_asof": False, "historical_pit": False,
        })
    seen = {item["key"] for item in items}
    return {
        **base, "status": "current_projection_only" if row.status == "ok" else "failed",
        "stored_status": row.status, "snapshot_time": _iso(row.snapshot_time),
        "items": items, "missing_sources": sorted(set(expected) - seen),
        "missing_session_keys": sorted(seen), "stored_item_count": len(raw_items),
        "items_truncated": len(raw_items) > 50,
        "freshness": {"snapshot_after_cutoff": row.snapshot_time > as_of,
                      "age_seconds_at_cutoff": (as_of - row.snapshot_time).total_seconds()
                      if row.snapshot_time <= as_of else None,
                      "source_clock_certified": False},
    }


async def read_premarket_context(
    db: AsyncSession, *, trade_date: date, as_of: datetime, limit: int = 100,
) -> dict:
    """Read bounded local context without DDL/DML, commit, sync, fetch or ensure.

    The session must be independent and database-enforced read-only (e.g. SQLite
    mode=ro + query_only). No session lifecycle or business transaction is owned
    here. Failed components remain explicit; cancellation is not swallowed.
    """
    _require_naive(as_of)
    if not isinstance(trade_date, date) or isinstance(trade_date, datetime):
        raise ValueError("trade_date must be an explicit date")
    if as_of.date() != trade_date or as_of.time() >= time(9, 30):
        raise ValueError("as_of must be on trade_date and before 09:30 Shanghai")
    _bounded_limit(limit)
    if db.new or db.dirty or db.deleted:
        raise ValueError("read_premarket_context requires a clean independent read-only session")

    async def component(source, reader):
        try:
            return await reader()
        except Exception as exc:
            return _failed(source, exc)

    with db.no_autoflush:
        calendar = await component("trade_calendar", lambda: _read_calendar(db, trade_date))
        kline = await component("stock_kline", lambda: _read_kline_dates(db, trade_date))
        postmarket = await component("daily_review_snapshot",
                                     lambda: _read_postmarket(db, trade_date, as_of))
        anchor = kline.get("previous_actual_trusted_trade_date")
        news = await component("news_content_version+news_analysis_version",
            lambda: _read_news_window(db, start_time=datetime.combine(date.fromisoformat(anchor), time(15)),
                                      as_of=as_of, limit=limit)) if anchor else {
                "status": "unavailable", "source": "news_content_version+news_analysis_version",
                "reason": "previous_actual_trusted_trade_date_unavailable", "items": [],
                "coverage": {"known_zero_news": False},
            }
        external = await component("dashboard_snapshot:overview-v2",
                                   lambda: _read_external_projection(db, as_of))
        from app.data.after_hours import read_after_hours
        previous = calendar.get("expected_previous")
        after_hours = await component("stock_after_hours_observation",
            lambda: read_after_hours(db, day=date.fromisoformat(previous), as_of=as_of, limit=limit)) if previous else {
                "status": "unavailable", "reason": "expected_previous_trade_day_unknown", "items": []}

    if (after_hours.get("status") != "failed" and after_hours.get("stored_code_count") == 0
            and after_hours.get("returned_codes") == 0 and calendar.get("expected_previous")):
        # Recorded Oct-2 research deployment had no historical backfill; its
        # first stored-calendar natural trading window is Oct-8, not Sep-30.
        prior_day = date.fromisoformat(calendar["expected_previous"])
        after_hours["missing_attribution"] = (
            "pre_deployment_no_historical_capture" if prior_day == date(2026, 9, 30)
            else "stored_observations_missing_collector_outcome_unknown")
        after_hours["deployment_reference"] = "after_hours_research_release_20261002_no_backfill"
        after_hours["deployment_dates_are_reference_not_row_evidence"] = True
        after_hours["first_natural_trade_date_reference"] = "2026-10-08"
        after_hours["missing_is_not_automatic_collection_failure"] = True

    expected = date.fromisoformat(calendar["expected_previous"]) if calendar.get("expected_previous") else None
    actual = date.fromisoformat(kline["actual_kline_date"]) if kline.get("actual_kline_date") else None
    trusted = date.fromisoformat(anchor) if anchor else None
    snapshot_day = date.fromisoformat(postmarket["analysis_trade_date"]) if postmarket.get("analysis_trade_date") else None
    alignments = {"kline": _alignment(expected, actual),
                  "trusted_kline_anchor": _alignment(expected, trusted),
                  "postmarket": _alignment(expected, snapshot_day)}
    components = {"calendar": calendar, "kline": kline, "previous_postmarket": postmarket,
                  "news": news, "external": external, "after_hours": after_hours}
    failures = [name for name, value in components.items() if value["status"] == "failed"]
    context = {
        "context_version": CONTEXT_VERSION, "trade_date": _iso(trade_date),
        "as_of_at": _iso(as_of), "timezone": "Asia/Shanghai", "clock_convention": "naive",
        "status": "partial" if len(failures) < len(components) else "failed",
        **components, "us_reference_session": _us_reference_context(as_of),
        "research_fusion": {
            "statistics": _fusion_statistics(postmarket, news, after_hours, expected=expected, as_of=as_of),
            "prior_frozen_dimensions": ({**postmarket.get("dimensions", {"status": "unavailable"}),
                "snapshot_id": postmarket.get("snapshot_id"), "payload_hash": postmarket.get("payload_hash"),
                "analysis_trade_date": postmarket.get("analysis_trade_date"),
                "expected_previous": _iso(expected), "as_of_at": postmarket.get("as_of_at"),
                "created_at": postmarket.get("created_at"), "data_version": postmarket.get("data_version")}
                if alignments["postmarket"] == "matched" else {
                    "status": "unavailable", "reason": "previous_snapshot_date_not_calendar_aligned",
                    "reference_snapshot_id": postmarket.get("snapshot_id")}),
            "news_cutoff": as_of.isoformat(), "analysis_slot": "next_trade_day_premarket",
            "same_day_capture_retained": True, "recomputed_previous_day_from_current_spot": False,
            "automatic_weight_update": False, "execution_authorized": False,
        },
        "freshness": {"expected_previous": _iso(expected), "actual_kline_date": _iso(actual),
                      "previous_actual_trusted_trade_date": anchor, "alignment": alignments,
                      "silent_date_fallback": False},
        "coverage": {"failed_components": failures,
                     "complete_overnight_coverage": False,
                     "unavailable_sources": external.get("unavailable_sources", [
                         "us_index_futures", "complete_us_after_hours",
                     ])},
        "readiness": {
            "target_confirmed_trade_day": calendar.get("target_status") == "trade_day",
            "baseline_dates_aligned": all(value == "matched" for value in alignments.values())
                                      and postmarket["status"] == "available",
            "complete_overnight_coverage": False,
        },
        "snapshot_ids": {
            "previous_postmarket": postmarket.get("snapshot_id"),
            "dashboard_latest_projection": external.get("snapshot_id"),
            "news_content_versions": news.get("provenance", {}).get("content_version_ids", []),
            "news_analysis_versions": news.get("provenance", {}).get("analysis_version_ids", []),
            "news_visible_analysis_attempts": news.get("provenance", {}).get("visible_analysis_attempt_ids", []),
        },
        "provenance": {"read_only": True, "network_collection": False,
                       "business_db_write": False, "execution_authorized": False,
                       "calendar_and_kline_are_current_stored_projections": True},
    }
    from app.review.after_hours_acceptance import summarize_stored_acceptance
    context["stored_evidence_acceptance"] = summarize_stored_acceptance(context)
    return context
