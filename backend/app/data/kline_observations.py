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
import os
from pathlib import Path
import re
import tempfile

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


# Offline download files are an explicit chart view, not a database projection.
DOWNLOAD_PROTOCOL = "kline_chart_download_v1"
DOWNLOAD_MAX_BYTES = 8 * 1024 * 1024


def write_download_json(path: Path, payload: dict) -> None:
    """Atomically publish a local manifest; never expose half-written JSON."""
    path = Path(path).resolve()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists() and temporary.resolve().parent == path.parent:
            temporary.unlink()


def _download_code(code: str) -> str:
    if not re.fullmatch(r"[0-9]{6}", str(code)):
        raise ValueError("历史下载需要6位股票代码")
    return code


def _validate_download(snapshot: dict, code: str) -> None:
    if (snapshot.get("protocol_version") != DOWNLOAD_PROTOCOL
            or snapshot.get("code") != code
            or snapshot.get("scope") != "chart_only"
            or snapshot.get("historical_pit_eligible") is not False
            or snapshot.get("coverage", {}).get("certified") is not False):
        raise ValueError("历史下载元数据无效")
    downloaded_at = datetime.fromisoformat(snapshot["downloaded_at"])
    if downloaded_at.tzinfo is None:
        raise ValueError("历史下载时钟缺少时区")
    start, end = date.fromisoformat(snapshot["start_date"]), date.fromisoformat(snapshot["end_date"])
    if not date(2018, 1, 1) <= start <= end <= downloaded_at.date():
        raise ValueError("历史下载日期范围无效")
    rows = snapshot.get("klines")
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError("历史下载为空或超出上限")
    previous = None
    for row in rows:
        day = date.fromisoformat(row["trade_date"])
        if (row.get("code") != code or row.get("source") != "ths"
                or not start <= day <= end or (previous is not None and day <= previous)
                or kline_quality_issues(row)):
            raise ValueError("历史下载行情身份/日期/质量无效")
        previous = day


def save_kline_download(root: Path, code: str, records: list[dict], *,
                        start: date, end: date, downloaded_at: datetime,
                        coverage: dict) -> dict:
    """Keep a full single-download THS series; do not splice adjustment vintages."""
    code = _download_code(code)
    rows = [{key: _owned_leaf(row.get(key)) for key in BAR_FIELDS} for row in records]
    rows.sort(key=lambda row: row["trade_date"])
    snapshot = {
        "protocol_version": DOWNLOAD_PROTOCOL, "code": code, "scope": "chart_only",
        "source_version": SOURCE_VERSIONS["ths"], "price_basis": "forward_adjusted_as_observed",
        "historical_pit_eligible": False, "downloaded_at": downloaded_at.isoformat(),
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "coverage": {**coverage, "certified": False}, "klines": rows,
    }
    _validate_download(snapshot, code)
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > DOWNLOAD_MAX_BYTES:
        raise ValueError("历史下载文件超出上限")
    digest = hashlib.sha256(encoded).hexdigest()
    directory = Path(root).resolve() / code
    # Version files remain intact if a later download fails.
    version = directory / f"{digest}.json"
    if not version.exists():
        write_download_json(version, snapshot)
    write_download_json(directory / "latest.json",
                        {"protocol_version": DOWNLOAD_PROTOCOL, "sha256": digest})
    return {"rows": len(rows), "sha256": digest, "downloaded_at": snapshot["downloaded_at"],
            "min_date": rows[0]["trade_date"], "max_date": rows[-1]["trade_date"],
            "coverage": snapshot["coverage"]}


def load_kline_download(root: Path, code: str) -> dict | None:
    """Read one validated snapshot without connecting to or updating a database."""
    code = _download_code(code)
    directory = Path(root).resolve() / code
    pointer_path = directory / "latest.json"
    if not pointer_path.is_file():
        return None
    try:
        if pointer_path.stat().st_size > 4096:
            raise ValueError("历史下载索引超出上限")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        digest = pointer.get("sha256", "")
        if (pointer.get("protocol_version") != DOWNLOAD_PROTOCOL
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError("历史下载索引无效")
        path = directory / f"{digest}.json"
        if path.resolve().parent != directory.resolve() or path.stat().st_size > DOWNLOAD_MAX_BYTES:
            raise ValueError("历史下载文件路径或大小无效")
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("历史下载校验失败")
        snapshot = json.loads(encoded)
        _validate_download(snapshot, code)
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("历史下载不可用，请重新下载；旧交易数据未改动") from exc
