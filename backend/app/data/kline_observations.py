"""Append-only, unreviewed K-line versions and a current-day-only projection.

This is a distinct-content catalog, NOT a complete sequence of HTTP observations.
recorded_at is assigned before commit, never vendor time or historical availability.
No record here is eligible for historical PIT features or automatic promotion.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import hashlib
import json
import math
import re

from sqlalchemy import select, tuple_, or_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock import StockKline, StockKlineObservation

PROTOCOL_VERSION = "kline_observation_v1"
BAR_FIELDS = ("code", "trade_date", "open", "close", "high", "low", "volume",
              "amount", "turnover", "change_pct", "prev_close", "source")
SOURCE_VERSIONS = {
    "ths": "ths_v6_line_01_nullable_prev_v2",
    "tencent_close": "tencent_close_projection_v2",
    "spot_fallback": "tencent_spot_projection_v2",
}


def _owned_leaf(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # Preserve an invalid observation as a labelled value, never zero/JSON NaN.
        return value if math.isfinite(value) else {"nonfinite": str(value)}
    raise ValueError("unsupported K-line scalar")


def _payload(row: dict) -> tuple[str, str]:
    encoded = json.dumps({key: _owned_leaf(row.get(key)) for key in BAR_FIELDS},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False)
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def kline_quality_issues(row: dict) -> list[str]:
    """Structural gates only; passing does NOT certify adjustment/calendar/PIT."""
    issues = []
    prices = {key: _finite(row.get(key)) for key in ("open", "close", "high", "low")}
    if any(value is None or value <= 0 for value in prices.values()):
        issues.append("invalid_ohlc")
    elif (prices["high"] < max(prices["open"], prices["close"])
          or prices["low"] > min(prices["open"], prices["close"])
          or prices["high"] < prices["low"]):
        issues.append("invalid_envelope")
    for key in ("volume", "amount", "turnover", "prev_close", "change_pct"):
        raw = row.get(key)
        if raw is None:
            continue  # Unknown remains NULL, not a measured zero.
        number = _finite(raw)
        if (number is None or (key != "change_pct" and number < 0)
                or (key == "prev_close" and number == 0)
                or (key == "volume" and (number != int(number) or number > 2**63 - 1))):
            issues.append("invalid_" + key)
    if row.get("source") not in SOURCE_VERSIONS:
        issues.append("unsupported_source")
    return issues


async def persist_kline_observations(
    session: AsyncSession, records: list[dict], *, now: datetime | None = None,
    projection_day: date | None = None,
) -> dict:
    """Append candidates + old projected bytes; only today's valid rows may project.

    Caller owns the transaction. An evidence write failure must also prevent the
    projection write. Past missing bars are archived, NOT backfilled into a table
    whose consumers do not have an availability-time contract. projection_day
    must be confirmed by the caller through the authoritative trade calendar;
    by default even current-day observations remain isolated.
    """
    now = now or datetime.now()
    if now.tzinfo is not None:
        raise ValueError("K-line recording requires local naive clock")
    today = now.date()
    normalized = []
    for original in records:
        row = {key: original.get(key) for key in BAR_FIELDS}
        if not re.fullmatch(r"[0-9]{6}", str(row["code"] or "")):
            raise ValueError("invalid K-line identity")
        row["code"] = str(row["code"])
        day = row["trade_date"]
        if isinstance(day, str):
            day = date.fromisoformat(day)
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError("invalid K-line trade date")
        row["trade_date"] = day
        normalized.append(row)

    keys = list(dict.fromkeys((row["code"], row["trade_date"]) for row in normalized))
    existing = {}
    for offset in range(0, len(keys), 200):
        rows = (await session.execute(
            select(*(getattr(StockKline, field) for field in BAR_FIELDS))
            .where(tuple_(StockKline.code, StockKline.trade_date).in_(keys[offset:offset + 200]))
        )).all()
        existing.update({(row.code, row.trade_date): dict(row._mapping) for row in rows})

    evidence = []
    projected = []
    dispositions = Counter()
    issue_counts = Counter()
    examples = []

    def capture(row, *, origin, disposition, issues=()):
        encoded, digest = _payload(row)
        source = row.get("source")
        evidence.append({
            "code": row["code"], "trade_date": row["trade_date"],
            "origin": origin, "disposition": disposition,
            "payload_json": encoded, "payload_hash": digest,
            "recorded_at": now, "available_at": None,
            "source_version": SOURCE_VERSIONS.get(source, "unknown") if origin == "observed_candidate" else "legacy_unknown",
            "price_basis": ("forward_adjusted_as_observed" if source == "ths" else "unadjusted_quote")
                if origin == "observed_candidate" and source in SOURCE_VERSIONS else "legacy_unknown",
            "quality_issues_json": json.dumps(list(issues), separators=(",", ":")),
            "protocol_version": PROTOCOL_VERSION,
        })

    for row in normalized:
        key = row["code"], row["trade_date"]
        previous = existing.get(key)
        issues = kline_quality_issues(row)
        if row["trade_date"] < today:
            disposition = "isolated_historical"
        elif row["trade_date"] > today:
            disposition = "isolated_future"
        elif projection_day != today:
            disposition = "isolated_unconfirmed_day"
        elif issues:
            disposition = "isolated_invalid"
        elif previous and (
            (row["source"] == "ths" and previous["source"] == "tencent_close")
            or (row["source"] == "spot_fallback" and previous["source"] != "spot_fallback")
        ):
            disposition = "protected_source"
        else:
            disposition = "current_projection"

        # Preserve the prior projection before ANY replacement. This observation
        # of legacy bytes does not create a historical receipt/availability time.
        if previous and _payload(previous)[1] != _payload(row)[1]:
            capture(previous, origin="legacy_projection", disposition="preserved_original")
        capture(row, origin="observed_candidate", disposition=disposition, issues=issues)
        dispositions[disposition] += 1
        issue_counts.update(issues)
        if disposition != "current_projection" and len(examples) < 20:
            examples.append({"code": row["code"], "trade_date": row["trade_date"].isoformat(),
                             "disposition": disposition, "issues": issues})
        if disposition == "current_projection":
            projected.append(row)
            existing[key] = row

    # Distinct-content versions are idempotent on retry. They are not a poll log.
    inserted = 0
    for offset in range(0, len(evidence), 100):
        result = await session.execute(insert(StockKlineObservation).values(evidence[offset:offset + 100])
            .on_conflict_do_nothing(index_elements=[
                "code", "trade_date", "origin", "disposition", "payload_hash"]))
        inserted += max(int(result.rowcount or 0), 0)
    written = 0
    for source in SOURCE_VERSIONS:
        source_rows = [row for row in projected if row["source"] == source]
        for offset in range(0, len(source_rows), 100):
            stmt = insert(StockKline).values(source_rows[offset:offset + 100])
            # Repeat precedence in SQL to protect against a concurrent collector.
            condition = None
            if source == "ths":
                condition = or_(StockKline.source.is_(None), StockKline.source != "tencent_close")
            elif source == "spot_fallback":
                condition = StockKline.source == "spot_fallback"
            stmt = stmt.on_conflict_do_update(index_elements=["code", "trade_date"],
                set_={field: getattr(stmt.excluded, field) for field in BAR_FIELDS
                      if field not in {"code", "trade_date"}}, where=condition)
            result = await session.execute(stmt)
            written += max(int(result.rowcount or 0), 0)
    return {
        "status": "degraded" if sum(count for key, count in dispositions.items()
                                  if key.startswith("isolated_")) else "ok",
        "protocol_version": PROTOCOL_VERSION, "input_count": len(normalized),
        "written": written, "observations_appended": inserted,
        "dispositions": dict(dispositions), "quality_issues": dict(issue_counts),
        "examples": examples, "example_limit": 20,
        "historical_projection_writes": 0, "historical_pit_eligible": False,
        "evidence_scope": "unreviewed_distinct_content_not_poll_history",
        "recorded_at": now.isoformat(), "recorded_at_is_commit_time": False,
    }
