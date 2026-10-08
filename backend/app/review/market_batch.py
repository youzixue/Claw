"""SELECT-only, observed-now full stored-universe descriptive review.

The caller owns an isolated mode=ro/query_only/no-autoflush/BEGIN session.
This module neither opens a business session nor writes files. A batch streams
each eligible minute parquet once, not once per stock. There is deliberately no
cache: every page reobserves the manifest and the mutable daily projection.

Minute files follow QuoteRoundArchive._write_minute, NOT exchange-bar semantics:
one row/code/commit-minute, sampled OHLC, last-round clocks, first_observed_at,
and last-frame cumulative session volume/amount. Finished minutes only are used.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import stat

from sqlalchemy import and_, case, false, func, or_, select, union

from app.models.stock import BrokenLimitPool, LimitUpPool, QuoteRound, StockKline
from app.review.evidence_readers import envelope
from app.review.evidence_store import bounded, cutoff, owned, trade_day
from app.review.price_evidence import _regular_minutes

SCHEMA_VERSION = "claw_market_descriptive_batch_v1"
MAX_CODES = 20000
MAX_RECENT_BARS = 6
MAX_FEATURE_ROWS = 200
MAX_MINUTE_FILES = 300
MAX_MANIFEST_REFS = 12  # Complete digest/counts, bounded evidence-reference samples for the model.
MAX_MINUTE_FILE_BYTES = 4 * 1024 * 1024
MAX_MINUTE_TOTAL_BYTES = 128 * 1024 * 1024
# Defend against tiny compressed files expanding into unbounded Python objects.
MAX_MINUTE_ROWS = 20000
MAX_MINUTE_DECODED_BYTES = 64 * 1024 * 1024
MINUTE_BATCH_ROWS = 2048
DAILY_FIELDS = ("id", "code", "trade_date", "open", "high", "low", "close",
                "prev_close", "change_pct", "volume", "amount", "turnover", "source")
LIMIT_FIELDS = ("id", "code", "trade_date", "source", "source_version",
                "source_quote_at", "observed_at", "limit_up_time", "limit_up_price",
                "consecutive_days", "break_count", "quarantined")
BROKEN_FIELDS = ("id", "code", "trade_date", "source", "source_version",
                 "source_quote_at", "observed_at", "limit_up_time", "break_time",
                 "close_price", "close_at_limit", "final_state")
ROUND_FIELDS = ("id", "round_id", "trade_date", "source", "committed_at", "as_of_at",
                "expected_count", "received_count", "source_time_count", "quality_status",
                "source_min_at", "source_max_at", "received_min_at", "received_max_at",
                "config_version", "code_version")
MINUTE_REQUIRED = frozenset(("code", "minute", "quote_round_id", "source_quote_at",
    "received_at", "updated_at", "first_observed_at", "open", "high", "low", "close",
    "price_samples", "price_basis", "volume_basis"))
MINUTE_COLUMNS = tuple(sorted(MINUTE_REQUIRED | {"volume", "amount"}))


def _hashable(value):
    # API prices sanitize nonfinite values to missing. The INPUT identity must
    # still distinguish a stored invalid float from a genuinely NULL field.
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_float": repr(value)}
    if isinstance(value, dict):
        return {str(key): _hashable(leaf) for key, leaf in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hashable(leaf) for leaf in value]
    return owned(value)


def _hash_into(digest, value):
    digest.update(json.dumps(_hashable(value), sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode())
    digest.update(b"\n")


def _digest(value):
    digest = hashlib.sha256()
    _hash_into(digest, value)
    return digest.hexdigest()


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _text(value):
    """Keep feature text compact; long/untrusted source labels remain references."""
    value = str(value or "unknown")
    return value if len(value.encode()) <= 128 else "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _direction(value, *, unavailable=False):
    value = _number(value)
    if unavailable:
        return "unassessed_before_close"
    return "missing" if value is None else "up" if value > 0 else "down" if value < 0 else "flat"


def _clock(value):
    try:
        clock = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return clock if clock.tzinfo is None else None


def _budget():
    return {"codes": MAX_CODES, "recent_bars_per_code": MAX_RECENT_BARS,
            "feature_rows": MAX_FEATURE_ROWS, "minute_files": MAX_MINUTE_FILES,
            "manifest_returned_refs": min(MAX_MANIFEST_REFS, MAX_MINUTE_FILES),
            "minute_file_bytes": MAX_MINUTE_FILE_BYTES,
            "minute_total_bytes": MAX_MINUTE_TOTAL_BYTES,
            "minute_rows_per_file": MAX_MINUTE_ROWS,
            "minute_decoded_bytes_per_file": MAX_MINUTE_DECODED_BYTES}


def _population(day):
    universe = union(select(StockKline.code).where(StockKline.trade_date == day),
        select(LimitUpPool.code).where(LimitUpPool.trade_date == day,
                                      LimitUpPool.quarantined.is_(False)),
        select(BrokenLimitPool.code).where(BrokenLimitPool.trade_date == day)).subquery()
    return select(universe.c.code, StockKline.id.label("kline_id"),
        StockKline.change_pct, LimitUpPool.id.label("limit_id"),
        BrokenLimitPool.id.label("broken_id")).select_from(universe).outerjoin(
        StockKline, and_(StockKline.code == universe.c.code, StockKline.trade_date == day)
    ).outerjoin(LimitUpPool, and_(LimitUpPool.code == universe.c.code,
        LimitUpPool.trade_date == day, LimitUpPool.quarantined.is_(False))
    ).outerjoin(BrokenLimitPool, and_(BrokenLimitPool.code == universe.c.code,
        BrokenLimitPool.trade_date == day)).subquery()


def _winner(population, daily_allowed):
    finite = population.c.change_pct.between(-1.7976931348623157e308, 1.7976931348623157e308)
    # SQL NULL is not a third cohort: the complement MUST retain pool-only and
    # missing-return failures rather than losing them through NOT(NULL).
    return func.coalesce(or_(population.c.limit_id.is_not(None),
               and_(finite, population.c.change_pct > 0)), false()) if daily_allowed else false()


async def _counts(db, population, daily_allowed, condition=None):
    finite = population.c.change_pct.between(-1.7976931348623157e308, 1.7976931348623157e308)
    known = and_(population.c.kline_id.is_not(None), finite)
    conditions = {
        "day_kline": population.c.kline_id.is_not(None),
        "missing_day_kline": population.c.kline_id.is_(None),
        "valid_limit_up": population.c.limit_id.is_not(None),
        "broken_limit": population.c.broken_id.is_not(None),
        "limit_and_broken": and_(population.c.limit_id.is_not(None), population.c.broken_id.is_not(None)),
        "daily_up": and_(known, population.c.change_pct > 0) if daily_allowed else false(),
        "daily_flat": and_(known, population.c.change_pct == 0) if daily_allowed else false(),
        "daily_down": and_(known, population.c.change_pct < 0) if daily_allowed else false(),
        "daily_missing_return": or_(population.c.kline_id.is_(None), ~finite,
                                    population.c.change_pct.is_(None)) if daily_allowed else false(),
        "daily_unassessed_before_close": false() if daily_allowed else population.c.code.is_not(None),
    }
    statement = select(func.count().label("all"), *(
        func.coalesce(func.sum(case((test, 1), else_=0)), 0).label(label)
        for label, test in conditions.items())).select_from(population)
    if condition is not None:
        statement = statement.where(condition)
    return dict((await db.execute(statement)).mappings().one())


async def _selected_rows(db, model, fields, selected, day):
    statement = select(*(getattr(model, key) for key in fields)).join(
        selected, model.code == selected.c.code).where(model.trade_date == day).order_by(model.code, model.id)
    if model is LimitUpPool:
        statement = statement.where(LimitUpPool.quarantined.is_(False))
    return [dict(row) for row in (await db.execute(statement)).mappings()]


async def _history(db, selected, day, daily_allowed):
    history_day = day if daily_allowed else day - timedelta(days=1)
    # Seek the last six rows via the existing unique (code, trade_date) index.
    # A window rank over every historical row took ~31s in the actual archive
    # acceptance, even though only six rows/code were returned. This correlated
    # LIMIT has identical date/ID ordering without scanning each code\'s full history.
    candidate = StockKline.__table__.alias("recent_kline")
    recent_ids = select(candidate.c.id).where(
        candidate.c.code == selected.c.code, candidate.c.trade_date <= history_day
    ).order_by(candidate.c.trade_date.desc(), candidate.c.id.desc()).limit(
        MAX_RECENT_BARS).correlate(selected)
    statement = select(*(getattr(StockKline, key) for key in DAILY_FIELDS)).select_from(
        selected).join(StockKline, StockKline.id.in_(recent_ids)).order_by(
        StockKline.code, StockKline.trade_date, StockKline.id)
    digest, history = hashlib.sha256(), {}
    result = await db.stream(statement)
    try:
        async for row in result.mappings():
            leaf = dict(row)
            _hash_into(digest, leaf)
            # No cross-day return or synthetic adjusted/raw price chain is built.
            history.setdefault(leaf["code"], []).append(owned(leaf))
    finally:
        await result.close()
    return history, digest.hexdigest()


@dataclass
class _Samples:
    present: int = 0
    first_minute: datetime | None = None
    last_minute: datetime | None = None
    first_observed: datetime | None = None
    last_observed: datetime | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    amount: float | None = None
    basis: tuple | None = None
    mixed_basis: bool = False
    source_examples: set = field(default_factory=set)
    source_examples_truncated: bool = False
    price_samples: int = 0
    rejected_rows: Counter = field(default_factory=Counter)
    round_quality: Counter = field(default_factory=Counter)
    non_regular_rows: int = 0

    def add(self, row, minute, index, round_row):
        self.present |= 1 << index
        basis = (row["price_basis"], _text(round_row["source"]))
        if self.basis is None:
            self.basis = basis
        self.mixed_basis |= self.basis != basis or basis[1] == "unknown"
        if len(self.source_examples) < 4 or basis[1] in self.source_examples:
            self.source_examples.add(basis[1])
        else:
            self.source_examples_truncated = True
        quality = round_row["quality_status"]
        self.round_quality[quality if quality in {"ok", "degraded"} else "unknown"] += 1
        self.price_samples += row["price_samples"]
        high, low = _number(row["high"]), _number(row["low"])
        self.high = max(self.high, high) if self.high is not None else high
        self.low = min(self.low, low) if self.low is not None else low
        if self.first_minute is None or minute < self.first_minute:
            self.first_minute, self.first_observed = minute, _clock(row["first_observed_at"])
            self.open = _number(row["open"])
        if self.last_minute is None or minute > self.last_minute:
            self.last_minute, self.last_observed = minute, _clock(row["updated_at"])
            self.close = _number(row["close"])
            # These are endpoints, never sums or inferred minute increments.
            self.volume, self.amount = _number(row.get("volume")), _number(row.get("amount"))


def _sample_error(row, minute, day, at, rounds):
    if row.get("minute") != minute.isoformat():
        return "minute_identity_mismatch"
    round_id = row.get("quote_round_id")
    if not isinstance(round_id, str) or not round_id:
        return "invalid_round_identity"
    round_row = rounds.get(round_id)
    if round_row is None:
        return "round_missing_at_cutoff"
    clocks = {key: _clock(row.get(key)) for key in (
        "source_quote_at", "received_at", "updated_at", "first_observed_at")}
    if any(value is None for value in clocks.values()):
        return "invalid_or_missing_code_clock"
    if any(value > at for value in clocks.values()):
        return "future_code_clock"
    if any(value.date() != day for value in clocks.values()):
        return "code_clock_trade_date_mismatch"
    updated = clocks["updated_at"]
    if updated != round_row["committed_at"] or updated.replace(second=0, microsecond=0) != minute:
        return "round_commit_identity_mismatch"
    if (round_row["trade_date"] != day or round_row["as_of_at"].date() != day
            or round_row["as_of_at"] > at or round_row["committed_at"] > at):
        return "round_clock_mismatch"
    if not minute <= clocks["first_observed_at"] <= updated or clocks["received_at"] > updated:
        return "first_or_received_clock_order"
    for clock_key, prefix in (("source_quote_at", "source"), ("received_at", "received")):
        lower, upper = _clock(round_row.get(prefix + "_min_at")), _clock(round_row.get(prefix + "_max_at"))
        if ((lower is not None and clocks[clock_key] < lower)
                or (upper is not None and clocks[clock_key] > upper)):
            return "code_clock_outside_round_bounds"
    if row.get("price_basis") != "sampled_quote_prices":
        return "unsupported_sampled_price_basis"
    if row.get("volume_basis") != "cumulative_session":
        return "unsupported_volume_basis"
    prices = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
    if any(value is None or value <= 0 for value in prices):
        return "missing_or_invalid_sampled_price"
    opening, high, low, close = prices
    if high < max(opening, low, close) or low > min(opening, high, close):
        return "invalid_sampled_ohlc"
    if type(row.get("price_samples")) is not int or row["price_samples"] < 1:
        return "missing_or_invalid_price_samples"
    return None


class _ArchiveReadError(ValueError):
    def __init__(self, reason, bytes_read=0):
        super().__init__(reason)
        self.bytes_read = bytes_read


def _read_file(path, observed_stat):
    """One bounded read, also rejecting atomic replacement/growth during observation."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, key) != getattr(observed_stat, key) for key in keys):
            raise _ArchiveReadError("archive_changed_during_read")
        # Read only the reserved, observed size; never spend an extra sentinel
        # byte beyond the per-file/total budget on a concurrently growing file.
        raw = stream.read(observed_stat.st_size)
        after = os.fstat(stream.fileno())
    try:
        current = path.stat()
    except OSError as exc:
        raise _ArchiveReadError("archive_changed_during_read", len(raw)) from exc
    if (len(raw) > MAX_MINUTE_FILE_BYTES or len(raw) != observed_stat.st_size
            or any(getattr(after, key) != getattr(before, key) for key in keys)
            or any(getattr(current, key) != getattr(before, key) for key in keys)):
        raise _ArchiveReadError("archive_changed_during_read", len(raw))
    return raw


async def _minutes(root, day, at, rounds, codes, regular, required):
    import pyarrow.parquet as pq

    samples = {code: _Samples() for code in codes}
    index = {minute: i for i, minute in enumerate(regular)}
    required_set = set(required)
    manifest, manifest_digest = [], hashlib.sha256()
    statuses, row_errors, file_errors = Counter(), Counter(), Counter()
    result = {"source_file_count": 0, "examined_files": 0, "files_read": 0,
              "bytes_read": 0, "bytes_reserved": 0, "rows_read": 0,
              "validated_rows": 0, "rejected_rows": 0, "outside_selected_universe_rows": 0,
              "non_regular_rows": 0, "read_attempts": 0,
              "unassessed_known_file_rows": 0, "unassessed_unknown_row_files": 0}
    directory = root / "minute" / ("trade_date=" + day.isoformat())
    if not directory.resolve().is_relative_to(root):
        paths = []
        file_errors["unsafe_archive_directory"] += 1
    else:
        paths = sorted(directory.glob("minute=*.parquet"), key=lambda path: path.name)
    result["source_file_count"] = len(paths)
    discovered, rejected_files = set(), set()
    eligible_ordinal = 0
    for path in paths:
        leaf = {"path": "minute/trade_date=" + day.isoformat() + "/" + path.name,
                "sha256": None, "size": None, "mtime_ns": None, "status": None}
        minute = None
        try:
            match = re.fullmatch(r"minute=(\d{4})\.parquet", path.name)
            if not match:
                raise ValueError("invalid_minute_filename")
            minute = datetime.combine(day, time(int(match[1][:2]), int(match[1][2:])))
            leaf["minute"] = minute.isoformat()
            if minute in required_set:
                discovered.add(minute)
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("unsafe_archive_path")
            observed_stat = path.stat()
            leaf.update(size=observed_stat.st_size, mtime_ns=observed_stat.st_mtime_ns)
            if not stat.S_ISREG(observed_stat.st_mode):
                raise ValueError("archive_not_regular_file")
            # A minute file can be replaced with later samples. Never use an
            # unfinished minute's aggregate even if its last row looks early.
            if minute + timedelta(minutes=1) > at:
                leaf["status"] = "future_or_unfinished_minute"
                continue
            eligible_ordinal += 1
            if eligible_ordinal > MAX_MINUTE_FILES:
                raise ValueError("archive_file_budget")
            result["examined_files"] += 1
            size = observed_stat.st_size
            if size > MAX_MINUTE_FILE_BYTES:
                raise ValueError("archive_file_byte_budget")
            if result["bytes_reserved"] + size > MAX_MINUTE_TOTAL_BYTES:
                raise ValueError("archive_total_byte_budget")
            result["bytes_reserved"] += size
            result["read_attempts"] += 1
            raw = _read_file(path, observed_stat)
            result["files_read"] += 1
            result["bytes_read"] += len(raw)
            leaf["sha256"] = hashlib.sha256(raw).hexdigest()
            parquet = pq.ParquetFile(BytesIO(raw))
            leaf["row_count"] = parquet.metadata.num_rows
            names = set(parquet.schema_arrow.names)
            if not MINUTE_REQUIRED <= names:
                raise ValueError("missing_minute_contract_columns")
            if parquet.metadata.num_rows > MAX_MINUTE_ROWS:
                raise ValueError("archive_row_budget")
            columns = [key for key in MINUTE_COLUMNS if key in names]
            decoded_bytes = sum(group.column(i).total_uncompressed_size
                for group in (parquet.metadata.row_group(j) for j in range(parquet.metadata.num_row_groups))
                for i in range(group.num_columns) if group.column(i).path_in_schema in columns)
            if decoded_bytes > MAX_MINUTE_DECODED_BYTES:
                raise ValueError("archive_decoded_byte_budget")
            # One file's selected columns only; never hold all-minute raw bars.
            rows, duplicates, bad_identifiers = {}, Counter(), 0
            for batch in parquet.iter_batches(batch_size=MINUTE_BATCH_ROWS, columns=columns, use_threads=False):
                for row in batch.to_pylist():
                    result["rows_read"] += 1
                    code = row.get("code")
                    if not isinstance(code, str) or not code:
                        bad_identifiers += 1
                    elif code in rows:
                        duplicates[code] += 1
                    else:
                        rows[code] = row
            local_errors = Counter()
            if bad_identifiers:
                local_errors["invalid_code_identity"] += bad_identifiers
            for code, row in rows.items():
                state = samples.get(code)
                if state is None:
                    result["outside_selected_universe_rows"] += 1 + duplicates[code]
                error = ("ambiguous_code_minute" if code in duplicates else
                         _sample_error(row, minute, day, at, rounds))
                if error:
                    count = 1 + duplicates[code]
                    local_errors[error] += count
                    if state is not None:
                        state.rejected_rows[error] += count
                    continue
                result["validated_rows"] += 1
                if minute not in required_set:
                    result["non_regular_rows"] += 1
                    if state is not None:
                        state.non_regular_rows += 1
                elif state is not None:
                    state.add(row, minute, index[minute], rounds[row["quote_round_id"]])
            row_errors.update(local_errors)
            result["rejected_rows"] += sum(local_errors.values())
            leaf.update(row_count=parquet.metadata.num_rows, row_errors=dict(sorted(local_errors.items())),
                        status="read_with_rejected_rows" if local_errors else "read")
        except Exception as exc:
            # Local evidence failure remains unassessed, never zero-price/flat.
            known_reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                "invalid_minute_filename", "unsafe_archive_path", "archive_not_regular_file",
                "archive_file_budget", "archive_file_byte_budget", "archive_total_byte_budget",
                "archive_changed_during_read", "missing_minute_contract_columns",
                "archive_row_budget", "archive_decoded_byte_budget"} else (
                "invalid_minute_filename" if minute is None and isinstance(exc, ValueError)
                else "archive_decode_or_io_error")
            leaf["status"] = known_reason
            leaf["error_type"] = type(exc).__name__
            if isinstance(exc, _ArchiveReadError):
                result["bytes_read"] += exc.bytes_read
                result["files_read"] += bool(exc.bytes_read)
            if "row_count" in leaf:
                result["unassessed_known_file_rows"] += leaf["row_count"]
            else:
                result["unassessed_unknown_row_files"] += 1
            file_errors[known_reason] += 1
            if minute in required_set:
                rejected_files.add(minute)
        finally:
            statuses[leaf["status"]] += 1
            _hash_into(manifest_digest, leaf)
            if len(manifest) < min(MAX_MANIFEST_REFS, MAX_MINUTE_FILES):
                manifest.append(leaf)
        await asyncio.sleep(0)
    result.update(status_counts=dict(sorted(statuses.items())),
        file_errors=dict(sorted(file_errors.items())), row_errors=dict(sorted(row_errors.items())),
        expected_regular_minutes_at_cutoff=len(required),
        missing_required_file_minutes=len(required_set - discovered),
        rejected_required_file_minutes=len(rejected_files),
        file_manifest_sha256=manifest_digest.hexdigest(), file_manifest=manifest,
        manifest_omitted_files=max(0, len(paths) - len(manifest)),
        manifest_scope="bounded_reference_sample; digest_and_status_counts_cover_all_discovered_paths",
        observed_now_archive_not_historical_ready_at=True,
        full_exchange_bars=False, intraminute_quote_coverage="unknown",
        clock_validation_scope="latest_code_round_clocks_plus_first_commit_only; earlier_frame_clocks_unavailable",
        unassessed_decoded_rows=result["rows_read"] - result["validated_rows"] - result["rejected_rows"],
        row_denominator_status="partial_with_unassessed_files" if file_errors else "all_read_file_rows_accounted",
        byte_budget_accounting="bytes_reserved_is_hard_cap; bytes_read_counts_returned_bytes_only",
        volume_basis="last_frame_cumulative_session_never_minute_increment",
        price_basis="same_source_sampled_quote_prices_only")
    return samples, result


def _minute_feature(state, required, regular, price_return):
    present = [minute for i, minute in enumerate(regular) if state.present & (1 << i)]
    present_set = set(present)
    missing = [minute for minute in required if minute not in present_set]
    missing_set = set(missing)
    longest, run, previous = 0, 0, None
    for minute in required:
        if previous is None or minute - previous != timedelta(minutes=1):
            run = 0
        run = run + 1 if minute in missing_set else 0
        longest, previous = max(longest, run), minute
    available = bool(present) and not state.mixed_basis
    first_last = _number(price_return(state.close, state.open)) if available else None
    span = _number(state.high - state.low) if available else None
    range_pct = _number(span / state.open * 100) if span is not None else None
    flags = []
    if not present:
        flags.append("no_valid_regular_sample_missing_not_flat")
    if missing:
        flags.append("regular_sample_gaps")
    if state.mixed_basis:
        flags.append("mixed_or_unknown_sampled_source_basis_prices_unassessed")
    if state.rejected_rows:
        flags.append("rejected_code_rows")
    if state.round_quality.get("degraded") or state.round_quality.get("unknown"):
        flags.append("degraded_or_unknown_quote_round")
    return {"status": "missing" if not present else "unassessed_mixed_basis" if not available
            else "partial_sampled" if missing else "all_expected_sampled_minutes_observed",
        "expected_regular_minutes": len(required), "observed_regular_minutes": len(present),
        "missing_regular_minutes": len(missing), "longest_missing_regular_minute_run": longest,
        "first_missing_minute": missing[0].isoformat() if missing else None,
        "last_missing_minute": missing[-1].isoformat() if missing else None,
        "first_minute": state.first_minute.isoformat() if state.first_minute else None,
        "last_minute": state.last_minute.isoformat() if state.last_minute else None,
        "first_observed_at": state.first_observed.isoformat() if state.first_observed else None,
        "last_observed_at": state.last_observed.isoformat() if state.last_observed else None,
        "sampled_open": state.open if available else None, "sampled_high": state.high if available else None,
        "sampled_low": state.low if available else None, "sampled_close": state.close if available else None,
        "sampled_range": span, "sampled_range_pct_of_first": range_pct,
        "first_last_return_pct": first_last, "price_samples": state.price_samples,
        "cumulative_session_volume_at_end": state.volume if available else None,
        "cumulative_session_amount_at_end": state.amount if available else None,
        "minute_volume": None, "minute_amount": None,
        "source_basis_examples": sorted(state.source_examples),
        "source_basis_examples_truncated": state.source_examples_truncated,
        "rejected_rows": dict(sorted(state.rejected_rows.items())),
        "round_quality_minute_counts": dict(sorted(state.round_quality.items())),
        "non_regular_rows_excluded": state.non_regular_rows, "quality_flags": flags}


def _feature(code, membership, history, limit_row, broken_row, state, day, daily_allowed,
             required, regular, helpers):
    price_return, opening_bucket, recovery_shape = helpers
    day_bar = next((row for row in history if row["trade_date"] == day.isoformat()), None)
    bases = sorted({_text(row.get("source")) for row in history})
    unknown_basis = not bases or "unknown" in bases
    mixed = len(bases) > 1
    open_pct = _number(price_return(day_bar["open"], day_bar["prev_close"])) if day_bar else None
    low_pct = _number(price_return(day_bar["low"], day_bar["prev_close"])) if day_bar else None
    direction = _direction(day_bar["change_pct"] if day_bar else None, unavailable=not daily_allowed)
    winner = daily_allowed and (limit_row is not None or direction == "up")
    pool = "limit_and_broken" if limit_row and broken_row else (
        "limit_up" if limit_row else "broken" if broken_row else "neither")
    # Source labels identify compatibility projections, NOT proved adjustment factors.
    recent = [{key: (_text(value) if key == "source" else value) for key, value in row.items()
               if key != "code"} for row in history]
    def pool_leaf(row):
        if row is None:
            return None
        if not daily_allowed:
            return {"id": row["id"], "code": code, "trade_date": day.isoformat(),
                    "status": "mutable_day_pool_fields_withheld_before_close"}
        return {key: (_text(value) if key in {"source", "source_version", "final_state"} else owned(value))
                for key, value in row.items()}
    return {"code": code, "cohort": "rising_or_limit" if winner else "non_rising",
        "feature_scope": "observed_now_descriptive_not_frozen_decision",
        "eligible_for_threshold_tuning": False,
        "day_kline_id": membership["kline_id"], "day_kline": next(
            (row for row in recent if row["trade_date"] == day.isoformat()), None),
        "daily_status": "unassessed_before_close" if not daily_allowed else "available" if day_bar else "missing",
        "daily_direction": direction, "open_return_pct": open_pct, "low_return_pct": low_pct,
        "opening_bucket": opening_bucket(open_pct),
        "recovery_shape": recovery_shape(open_pct=open_pct, low_pct=low_pct),
        "pool_membership": pool, "limit_up": pool_leaf(limit_row), "broken_limit": pool_leaf(broken_row),
        "recent_daily_bars": recent, "recent_bar_count": len(recent),
        "source_basis_set": bases, "mixed_or_unknown_basis": mixed or unknown_basis,
        "daily_basis_quality": "mixed_sources_unverified" if mixed else "unknown" if unknown_basis else "single_source_unverified",
        "minute": _minute_feature(state, required, regular, price_return)}


def _histogram_leaf(feature):
    minute = feature["minute"]
    return {"daily_direction": feature["daily_direction"], "pool_membership": feature["pool_membership"],
        "opening_bucket": feature["opening_bucket"], "recovery_shape": feature["recovery_shape"],
        "daily_basis_quality": feature["daily_basis_quality"],
        "recent_bar_count": str(feature["recent_bar_count"]),
        "sampled_minute_coverage": minute["status"],
        "sampled_first_last_direction": _direction(minute["first_last_return_pct"])}


async def read_market_batch(db, *, day: date, at: datetime, section="summary",
                            cohort="all", cursor="", limit=50, archive_root=None) -> dict:
    """Return a deterministic observed-now descriptive batch or bounded code page.

    Filters/pages do not alter the batch input fingerprint or its full-population
    statistics. non_rising is the complement of proved *stored* rising/valid-limit
    membership, INCLUDING missing/unassessed evidence; it does not mean flat/down.
    Before 15:15 target-day daily OHLC/returns and winner classification are
    withheld. Earlier stored daily history and finished minute sampling can still
    be described. Historical availability and stock identity are never inferred.
    """
    at = cutoff(at)
    day = trade_day(day, as_of=at)
    if section not in {"summary", "features"} or cohort not in {"all", "rising_or_limit", "non_rising"}:
        raise ValueError("market_batch requires summary/features and all/rising_or_limit/non_rising")
    if type(limit) is not int or not 1 <= limit <= MAX_FEATURE_ROWS or not isinstance(cursor, str):
        raise ValueError("market_batch requires a string code cursor and limit 1..200")
    if db.autoflush or not db.in_transaction():
        raise ValueError("market_batch requires caller-owned no-autoflush explicit read transaction")
    from app.review.service import _price_return, _opening_bucket, _intraday_recovery_shape
    helpers = (_price_return, _opening_bucket, _intraday_recovery_shape)
    daily_allowed = datetime.combine(day, time(15, 15)) <= at
    population = _population(day)
    winner = _winner(population, daily_allowed)
    counts = {"all": await _counts(db, population, daily_allowed),
              "rising_or_limit": await _counts(db, population, daily_allowed, winner),
              "non_rising": await _counts(db, population, daily_allowed, ~winner)}
    quarantined = await db.scalar(select(func.count()).select_from(LimitUpPool).where(
        LimitUpPool.trade_date == day, LimitUpPool.quarantined.is_(True)))
    selected = select(population.c.code).order_by(population.c.code).limit(MAX_CODES).subquery()
    members = [dict(row) for row in (await db.execute(select(population).join(
        selected, population.c.code == selected.c.code).order_by(population.c.code))).mappings()]
    codes = [row["code"] for row in members]
    history, daily_hash = await _history(db, selected, day, daily_allowed)
    limits = await _selected_rows(db, LimitUpPool, LIMIT_FIELDS, selected, day)
    broken = await _selected_rows(db, BrokenLimitPool, BROKEN_FIELDS, selected, day)
    limit_map, broken_map = {row["code"]: row for row in limits}, {row["code"]: row for row in broken}
    round_rows = [dict(row) for row in (await db.execute(select(*(getattr(QuoteRound, key) for key in ROUND_FIELDS)
        ).where(QuoteRound.trade_date == day, QuoteRound.committed_at <= at, QuoteRound.as_of_at <= at)
        .order_by(QuoteRound.committed_at, QuoteRound.round_id))).mappings()]
    rounds = {row["round_id"]: row for row in round_rows}
    if archive_root is None:
        from app.config.settings import settings
        archive_root = settings.QUOTE_ROUND_ARCHIVE_DIR
    root = Path(archive_root).resolve()
    regular = sorted(_regular_minutes(day))
    required = [minute for minute in regular if minute + timedelta(minutes=1) <= at]
    states, archive = await _minutes(root, day, at, rounds, codes, regular, required)
    inputs = {"version": SCHEMA_VERSION, "trade_date": day.isoformat(), "as_of": at.isoformat(),
        "budgets": _budget(), "daily_rows_sha256": daily_hash,
        "selected_membership_sha256": _digest([{key: row[key] for key in (
            "code", "kline_id", "limit_id", "broken_id")} for row in members]),
        "selected_pool_rows_sha256": _digest({"limit": limits, "broken": broken}),
        "quote_round_metadata_sha256": _digest(round_rows),
        "minute_source_manifest_sha256": archive["file_manifest_sha256"],
        "archive_state_sha256": _digest({key: value for key, value in archive.items() if key != "file_manifest"}),
        "global_counts_sha256": _digest(counts),
        "quarantined_limit_rows_excluded": quarantined}
    fingerprint = _digest(inputs)
    histograms = {name: {} for name in counts}
    coverage = {name: Counter() for name in counts}
    page = []
    for member in members:
        code = member["code"]
        feature = _feature(code, member, history.get(code, []), limit_map.get(code),
            broken_map.get(code), states[code], day, daily_allowed, required, regular, helpers)
        for name in ("all", feature["cohort"]):
            for key, value in _histogram_leaf(feature).items():
                histograms[name].setdefault(key, Counter())[value] += 1
            leaf = coverage[name]
            leaf["assessed_codes"] += 1
            leaf["daily_available"] += feature["daily_status"] == "available"
            leaf["daily_missing"] += feature["daily_status"] == "missing"
            leaf["daily_unassessed_before_close"] += not daily_allowed
            leaf["recent_daily_rows"] += feature["recent_bar_count"]
            leaf["mixed_or_unknown_daily_basis"] += feature["mixed_or_unknown_basis"]
            leaf["codes_with_regular_samples"] += bool(feature["minute"]["observed_regular_minutes"])
            leaf["observed_code_minutes"] += feature["minute"]["observed_regular_minutes"]
            leaf["rejected_code_rows"] += sum(states[code].rejected_rows.values())
            leaf["sampled_prices_unassessed_mixed_basis"] += states[code].mixed_basis
            leaf["codes_with_degraded_or_unknown_rounds"] += bool(states[code].round_quality.get("degraded")
                or states[code].round_quality.get("unknown"))
        if section == "features" and code > cursor and (cohort == "all" or feature["cohort"] == cohort) and len(page) <= limit:
            page.append(feature)
        if coverage["all"]["assessed_codes"] % 128 == 0:
            await asyncio.sleep(0)
    for name in counts:
        unassessed = counts[name]["all"] - coverage[name]["assessed_codes"]
        for key in ("daily_direction", "pool_membership", "opening_bucket", "recovery_shape",
                    "daily_basis_quality", "recent_bar_count", "sampled_minute_coverage", "sampled_first_last_direction"):
            histogram = histograms[name].setdefault(key, Counter())
            if unassessed:
                histogram["unassessed_code_budget"] += unassessed
        coverage[name].update({"total_codes": counts[name]["all"], "unassessed_code_budget": unassessed,
            "expected_code_minutes": len(required) * counts[name]["all"],
            "missing_or_unassessed_code_minutes": len(required) * counts[name]["all"] - coverage[name]["observed_code_minutes"]})
    total, assessed = counts["all"]["all"], len(codes)
    partial = (not daily_allowed or total > assessed or counts["all"]["missing_day_kline"]
        or counts["all"]["daily_missing_return"] or coverage["all"]["mixed_or_unknown_daily_basis"]
        or coverage["all"]["missing_or_unassessed_code_minutes"] or archive["file_errors"]
        or archive["row_errors"] or coverage["all"]["sampled_prices_unassessed_mixed_basis"]
        or coverage["all"]["codes_with_degraded_or_unknown_rounds"])
    result = {**envelope(day, at), "schema_version": SCHEMA_VERSION,
        "status": "unavailable" if not total else "partial" if partial else "descriptive_batch_available",
        "section": section, "cohort": cohort, "input_fingerprint": fingerprint,
        "input_components": inputs, "counts_by_cohort": counts,
        "total_stored_universe": total, "assessed_universe": assessed,
        "unassessed_code_budget": total - assessed, "truncated": total > assessed,
        "quarantined_limit_rows_excluded": quarantined, "coverage_by_cohort": {
            key: dict(sorted(value.items())) for key, value in coverage.items()},
        "budgets": _budget(), "daily_close_withheld": not daily_allowed,
        "daily_close_not_before": datetime.combine(day, time(15, 15)).isoformat(),
        "universe_scope": "all_stored_day_kline_plus_valid_limit_and_broken_pools_not_exchange_listing",
        "cohort_definition": "stored_daily_up_or_valid_limit; complement_retains_missing_and_unassessed",
        "cohort_classification_withheld_before_close": not daily_allowed,
        "daily_price_basis": "mutable_mixed_THS_adjusted_Tencent_raw_compatibility",
        "historical_PIT": False, "availability_at": None,
        "future_or_late_daily_revisions": "unknown_observed_now",
        "historical_stock_identity": "unknown_not_current_StockTag_backfill",
        "pool_membership_basis": "current_mutable_projection_not_historical_pool_membership",
        "full_market_strategy_shape_control": "unavailable_no_frozen_decision_denominator",
        "daily_grouping_semantics": "existing_helper_morphology_labels_not_confirmed_recovery_or_signal",
        "strategy_shape_evaluated_as_of": None,
        "eligible_for_historical_training_or_strategy_tuning": False,
        "eligible_for_threshold_tuning": False, "full_exchange_bars": False,
        "intraday_daily_prev_close_comparison": "not_performed_incompatible_price_bases",
        "unassessed_codes_reference": {"source_tables": ["stock_kline", "limit_up_pool", "broken_limit_pool"],
            "trade_date": day.isoformat(), "after_code": codes[-1] if codes else None,
            "count": total - assessed, "ordering": "code_ascending"},
        "cache": {"enabled": False, "scope": "observed_now_non_PIT",
                  "reason": "no_cross_page_mutable_manifest_cache; each_call_restreams_bounded_files"}}
    if not total:
        result["reason"] = "empty_stored_universe_not_confirmed_empty_exchange"
    if section == "summary":
        result.update(grouped_histograms={name: {key: dict(sorted(value.items())) for key, value in groups.items()}
            for name, groups in histograms.items()}, archive=archive,
            quote_rounds={"available_at_cutoff": len(round_rows),
                "expected_quote_universe_count": max((row["expected_count"] for row in round_rows), default=None),
                "quality_counts": dict(sorted(Counter(_text(row["quality_status"]) for row in round_rows).items())),
                "metadata_sha256": inputs["quote_round_metadata_sha256"]})
    else:
        result.update(items=page[:limit], returned=min(limit, len(page)), cursor=cursor,
            next_cursor=page[limit - 1]["code"] if len(page) > limit else None,
            total_matching=counts[cohort]["all"], assessed_matching=coverage[cohort]["assessed_codes"],
            unreturned_due_code_budget=coverage[cohort]["unassessed_code_budget"],
            truncation="code_keyset_page_with_explicit_unassessed_budget",
            archive={key: value for key, value in archive.items() if key != "file_manifest"})
    return bounded(result)
