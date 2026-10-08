"""Existing daily projection and sampled-minute readers; never download or backfill."""
from __future__ import annotations
from datetime import datetime, time, timedelta
import hashlib
from io import BytesIO
from pathlib import Path
import re

from sqlalchemy import select

from app.config.settings import settings
from app.models.stock import QuoteRound, StockKline, StockKlineObservation
from app.review.evidence_store import owned, record
from app.review.evidence_readers import envelope

MAX_MINUTE_FILES = 300
MAX_MINUTE_TOTAL_BYTES = 128 * 1024 * 1024


def _regular_minutes(day):
    return {datetime.combine(day, start)+timedelta(minutes=i)
            for start in (time(9,30), time(13)) for i in range(120)}


def _aggregate_five(rows):
    groups = {}
    for row in rows:
        minute = datetime.fromisoformat(row["minute"])
        key = minute.replace(minute=minute.minute//5*5, second=0, microsecond=0).isoformat()
        groups.setdefault(key, []).append(row)
    result = []
    for minute, samples in sorted(groups.items()):
        samples.sort(key=lambda r: r["minute"])
        valid = all(type(r.get(k)) in (int, float) for r in samples for k in ("open","high","low","close"))
        result.append({"minute": minute, "open": samples[0].get("open") if valid else None,
            "high": max(r["high"] for r in samples) if valid else None,
            "low": min(r["low"] for r in samples) if valid else None,
            "close": samples[-1].get("close") if valid else None,
            "sampled_minutes": len(samples), "complete_five_sampled_minutes": len(samples)==5,
            "cumulative_session_volume_at_end": samples[-1].get("volume"),
            "cumulative_session_amount_at_end": samples[-1].get("amount"),
            "minute_volume": None, "minute_amount": None,
            "price_basis": "aggregate_of_sampled_minute_quotes_not_exchange_5m"})
    return result


async def price_evidence(db, *, day, at, code, period="daily", cursor=0, limit=100, archive_root=None):
    result = {**envelope(day, at), "code": code, "period": period,
              "eligible_for_historical_training_or_strategy_tuning": False}
    if period == "daily":
        if day == at.date() and at.time() < time(15,15):
            return {**result, "status": "unavailable", "reason": "mutable_daily_close_before_close"}
        rows = list((await db.scalars(select(StockKline).where(
            StockKline.code == code, StockKline.trade_date <= day,
            StockKline.trade_date >= day-timedelta(days=90))
            .order_by(StockKline.trade_date.desc()).limit(60))).all())
        observations = list((await db.scalars(select(StockKlineObservation).where(
            StockKlineObservation.code == code, StockKlineObservation.trade_date == day)
            .order_by(StockKlineObservation.id.desc()).limit(20))).all())
        return {**result, "status": "ok" if rows else "unavailable",
                "items": [record(r) for r in rows[cursor:cursor+limit]],
                "total_bounded": len(rows), "history_day_budget": 90, "row_budget": 60,
                "next_cursor": cursor+limit if cursor+limit<len(rows) else None,
                "price_basis": "mixed_THS_forward_adjusted_Tencent_raw_compatibility",
                "source_basis_set": sorted({r.source or "unknown" for r in rows}),
                "historical_PIT": False, "availability_at": None,
                "content_observations": [record(r) for r in observations],
                "observation_note": "content_catalog_available_at_NULL_not_historical_PIT"}
    if period not in {"sampled_1m", "sampled_5m"}:
        raise ValueError("only daily, sampled_1m and sampled_5m are supported")
    import pyarrow.parquet as pq
    root = Path(archive_root or settings.QUOTE_ROUND_ARCHIVE_DIR).resolve()
    directory = root / "minute" / ("trade_date="+day.isoformat())
    paths = sorted(directory.glob("minute=????.parquet"))
    rounds = list((await db.scalars(select(QuoteRound).where(
        QuoteRound.trade_date == day, QuoteRound.committed_at <= at,
        QuoteRound.as_of_at <= at))).all())
    round_map = {r.round_id: r for r in rounds}
    samples, errors, hashes = [], [], []
    total_bytes = 0
    for path in paths[:MAX_MINUTE_FILES]:
        minute_id = path.stem.removeprefix("minute=")
        if not re.fullmatch(r"\d{4}", minute_id):
            continue
        observed = datetime.combine(day, time(int(minute_id[:2]), int(minute_id[2:])))
        # The in-progress minute file can later contain samples from after cutoff.
        if observed+timedelta(minutes=1) > at:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            errors.append({"minute": minute_id, "reason": "unsafe_archive_path"})
            continue
        size = path.stat().st_size
        if size > 4*1024*1024 or total_bytes+size > MAX_MINUTE_TOTAL_BYTES:
            errors.append({"minute": minute_id, "reason": "archive_byte_budget"})
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(4*1024*1024+1)
            total_bytes += len(raw)
            if len(raw) > 4*1024*1024:
                raise ValueError("minute_file_byte_budget")
            table = pq.read_table(BytesIO(raw), filters=[("code", "=", code)])
            leaves = table.to_pylist()
            if len(leaves) > 1:
                raise ValueError("ambiguous_code_minute")
            if not leaves:
                errors.append({"minute": minute_id, "reason": "code_not_sampled"})
                continue
            row = leaves[0]
            round_row = round_map.get(row.get("quote_round_id"))
            if not round_row or row.get("minute") != observed.isoformat():
                raise ValueError("round_or_minute_identity_missing_at_cutoff")
            for key in ("source_quote_at", "received_at", "updated_at"):
                clock = datetime.fromisoformat(str(row.get(key)))
                if clock.tzinfo is not None or clock > at:
                    raise ValueError("invalid_or_future_source_clock")
            row["quote_round_quality"] = round_row.quality_status
            row["source"] = round_row.source
            row["config_version"] = round_row.config_version
            row["code_version"] = round_row.code_version
            samples.append(owned(row))
            hashes.append({"minute": minute_id, "sha256": hashlib.sha256(raw).hexdigest()})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({"minute": minute_id, "reason": type(exc).__name__})
    regular = _regular_minutes(day)
    required = {m for m in regular if m+timedelta(minutes=1)<=at}
    present = {datetime.fromisoformat(r["minute"]) for r in samples}
    missing = sorted(m.isoformat() for m in required-present)
    rows = samples if period == "sampled_1m" else _aggregate_five(samples)
    result.update(status="partial" if missing or errors or len(paths)>MAX_MINUTE_FILES else "sampled_window_available",
        items=rows[cursor:cursor+limit], total_available=len(rows),
        next_cursor=cursor+limit if cursor+limit<len(rows) else None,
        observed_now_archive_not_historical_ready_at=True, full_exchange_bars=False,
        price_basis="sampled_quote_prices", volume_basis="cumulative_session_not_minute_increment",
        source_files=hashes, source_file_count=len(paths), examined_file_budget=MAX_MINUTE_FILES,
        bytes_read=total_bytes, errors=errors, missing_regular_minutes=missing,
        expected_regular_minutes_at_cutoff=len(required), observed_regular_minutes=len(required & present),
        intraminute_quote_coverage="unknown_not_proven_by_minute_presence")
    return result
