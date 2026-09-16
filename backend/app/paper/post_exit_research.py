"""Read-only D0 research after the actual final liquidation; never executable PnL."""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path
import asyncio
import hashlib
import json
import math
import re

import pandas as pd
import pyarrow.parquet as pq
from sqlalchemy import select

from app.config.settings import settings
from app.core.price_limit_rules import price_limit_rule
from app.models.governance import TradeCalendarModel
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.stock import QuoteRound
from app.models.trading import TradeFill, TradeOrder
from app.paper.experiment_report import complete_cycles


@dataclass(frozen=True)
class QuoteArchiveRef:
    """Owned immutable leaves only; safe to send to the file-reading worker."""
    round_id: str
    trade_date: date
    committed_at: datetime
    source: str
    quality_status: str
    archive_status: str
    archive_path: str | None
    focus_path: str | None
    config_version: str
    code_version: str


@dataclass(frozen=True)
class PostExitPolicy:
    max_source_age_sec: int = 180
    max_sample_gap_sec: int = 90

    def __post_init__(self):
        for value in (self.max_source_age_sec, self.max_sample_gap_sec):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("research clock limits must be positive integers")

    def contract(self):
        return {"version": "post_exit_d0_v1",
                "clock_policy_hash": hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16], **asdict(self),
                "thresholds_pct": {"intraday_rebound": 2, "close_early": 3, "limit_level": 5},
                "reference": "last_trade_that_reduces_inventory_to_zero",
                "entry_basis": "remaining_gross_weighted_cost_excluding_fees",
                "close_basis": "formal_close_health_unavailable_p0",
                "terminal_quote_scope": "observed_reference_only_not_formal_close",
                "price_basis": "raw_sampled_quote_prices",
                "executable_return": None, "hypothetical_net_cash": None}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _clock(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def _object(value):
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _owned_json(value):
    """Convert owned audit metadata only, never serialize ORM entities."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _owned_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_owned_json(v) for v in value]
    return value


def _session_seconds(at):
    """Elapsed continuous-session seconds; lunch is not a missing quote gap."""
    sec = at.hour * 3600 + at.minute * 60 + at.second + at.microsecond / 1e6
    return max(0, min(sec - 34200, 7200)) + max(0, min(sec - 46800, 7200))


def _in_quote_session(at):
    return time(9, 30) <= at.time() <= time(11, 30) or time(13) <= at.time() <= time(15, 5)


def _safe_archive_bytes(root, raw_path, family, day, round_id):
    """No arbitrary DB paths, URL readers, traversal, escaped symlinks or TOCTOU re-read."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", round_id):
        raise ValueError("invalid archive identity")
    expected = root / family / ("trade_date=" + day.isoformat()) / (round_id + ".parquet")
    path = Path(str(raw_path or ""))
    if not path.is_absolute() or path.resolve() != expected.resolve():
        raise ValueError("archive identity mismatch")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("archive outside root or unavailable")
    if resolved.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("archive too large")
    with resolved.open("rb") as stream:
        content = stream.read(128 * 1024 * 1024 + 1)
    if len(content) > 128 * 1024 * 1024:
        raise ValueError("archive too large")
    return content


def load_quote_archive(rounds, codes_by_day, *, root, policy):
    """Read each compact/focus file once per batch; retain invalid-round denominators."""
    root = Path(root).resolve()
    series, manifest = defaultdict(list), []
    required = {"code", "price", "prev_close", "source_quote_at", "received_at",
                "committed_at", "quote_round_id"}
    for row in rounds:
        codes = codes_by_day.get(row.trade_date, set())
        if not codes:
            continue
        base = {"round_id": row.round_id, "commit_at": row.committed_at.isoformat()}
        round_manifest = {"round_id": row.round_id, "family": "round_metadata",
            "trade_date": row.trade_date.isoformat(), "commit_at": row.committed_at.isoformat(),
            "source": row.source, "quality_status": row.quality_status,
            "archive_status": row.archive_status, "config_version": row.config_version,
            "code_version": row.code_version, "requested_codes": sorted(codes)}
        manifest.append(round_manifest)
        reason = ""
        compact, focus = {}, {}
        if row.source != "tencent" or row.quality_status != "ok":
            reason = "round_source_or_quality_unknown"
        elif row.archive_status != "ready":
            reason = "archive_not_ready"
        else:
            for family, raw in (("compact", row.archive_path), ("focus", row.focus_path)):
                if family == "focus" and not raw:
                    continue
                try:
                    blob = _safe_archive_bytes(root, raw, family, row.trade_date, row.round_id)
                    schema = set(pq.ParquetFile(BytesIO(blob)).schema.names)
                    needed = required if family == "compact" else {"code", "price", "source_quote_at", "quote_round_id", "limit_up"}
                    if not needed <= schema:
                        raise ValueError("missing columns")
                    columns = sorted(needed | ({"name"} & schema))
                    frame = pd.read_parquet(BytesIO(blob), columns=columns,
                                            filters=[("code", "in", sorted(codes))])
                    duplicates = set(frame.loc[frame.code.duplicated(keep=False), "code"].astype(str))
                    data = {str(item["code"]): item for item in frame.to_dict("records")
                            if str(item["code"]) not in duplicates}
                    if family == "compact":
                        compact = data
                    else:
                        focus = data
                    manifest.append({"round_id": row.round_id, "family": family,
                        "sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob),
                        "config_version": row.config_version, "code_version": row.code_version})
                except Exception:
                    # Deliberately no exception text / local DB paths in reports.
                    if family == "compact":
                        reason = "archive_unavailable_or_invalid"
                    else:
                        manifest.append({"round_id": row.round_id, "family": family,
                                         "status": "limit_evidence_unavailable"})
        round_manifest["reason_code"] = reason or "round_available"
        if reason:
            manifest.append({"round_id": row.round_id, "family": "compact", "status": reason})
        for code in codes:
            item = compact.get(code)
            error = reason or ("code_missing_or_duplicate" if item is None else "")
            sample = dict(base)
            if not error:
                source, received = _clock(item.get("source_quote_at")), _clock(item.get("received_at"))
                commit, price, prev = _clock(item.get("committed_at")), _number(item.get("price")), _number(item.get("prev_close"))
                if (commit != row.committed_at or str(item.get("quote_round_id")) != row.round_id
                        or source is None or received is None or source.date() != row.trade_date
                        or received.date() != row.trade_date or not _in_quote_session(source)
                        or not source <= received <= row.committed_at
                        or (row.committed_at-source).total_seconds() > policy.max_source_age_sec
                        or price is None or price <= 0 or prev is None or prev <= 0):
                    error = "invalid_quote_identity_clock_or_price"
                else:
                    limit = None
                    focused = focus.get(code, {})
                    if (str(focused.get("quote_round_id")) == row.round_id
                            and _clock(focused.get("source_quote_at")) == source
                            and _number(focused.get("price")) == price):
                        raw_limit = _number(focused.get("limit_up"))
                        name = focused.get("name") or item.get("name")
                        if isinstance(name, str) and name and raw_limit and raw_limit > prev:
                            nominal = price_limit_rule(code, name=name, trade_date=row.trade_date).nominal_limit_pct
                            expected = round(prev * (1 + nominal/100), 2)
                            if abs(raw_limit-expected) <= .011 and price <= raw_limit + .005:
                                limit = raw_limit
                    sample.update(source_at=source.isoformat(), price=price, prev_close=prev, limit_up=limit)
            sample["reason"] = error
            series[(row.trade_date, code)].append(sample)
    return series, manifest


def evaluate_post_exit_labels(cycle, records, *, as_of, policy=None):
    """Tri-state observed facts; no daily high, interpolation or fillable-profit claims."""
    policy = policy or PostExitPolicy()
    result = {"intraday_rebound": "unknown", "close_early": "unknown", "limit_level": "unknown",
              "status": "unknown", "coverage": "unknown", "reasons": [],
              "sampled_peak_after_exit_pct": None, "sampled_low_after_exit_pct": None,
              "close_after_exit_pct": None, "close_vs_entry_basis_pct": None,
              "sample_count": 0, "executable_return": None, "hypothetical_net_cash": None}
    if not cycle.get("eligible") or cycle.get("identity_status") != "valid":
        return {**result, "status": "excluded" if not cycle.get("eligible") and "invalid_quantity_history" not in cycle.get("exclusion_reasons", []) else "unknown",
                "reasons": cycle.get("exclusion_reasons") or ["identity_unproven"]}
    exit_at, price = _clock(cycle.get("exit_at")), _number(cycle.get("final_exit_price"))
    if exit_at is None or price is None or price <= 0 or as_of < exit_at:
        return {**result, "reasons": ["invalid_exit_reference"]}
    close_at = datetime.combine(exit_at.date(), time(15))
    if not _in_quote_session(exit_at) or exit_at >= close_at:
        return {**result, "reasons": ["exit_outside_remaining_session"]}
    horizon = min(as_of, close_at)
    relevant = [r for r in records if _clock(r.get("commit_at"))
                and exit_at < _clock(r["commit_at"]) <= as_of
                and _clock(r["commit_at"]).date() == exit_at.date()]
    invalid = sum(bool(r.get("reason")) for r in relevant)
    points, conflicts = {}, set()
    for row in relevant:
        source = _clock(row.get("source_at"))
        if (row.get("reason") or source is None or source <= exit_at or source > as_of
                or source > _clock(row["commit_at"]) or not _in_quote_session(source)
                or source.date() != exit_at.date() or row["round_id"] == cycle.get("fill_round_id")):
            continue
        if (_number(row.get("price")) is None or row["price"] <= 0
                or _number(row.get("prev_close")) is None or row["prev_close"] <= 0):
            invalid += 1
            continue
        # The same source clock is one observation only if its price basis also
        # agrees. Discarding a contradictory prev_close would make labels depend
        # on archive iteration order and could falsely certify a sampled window.
        if source in points and any(
                points[source][key] != row[key] for key in ("price", "prev_close")):
            conflicts.add(source)
        else:
            points.setdefault(source, row)
    samples = [row for stamp, row in sorted(points.items()) if stamp not in conflicts]
    if not samples:
        return {**result, "reasons": ["post_exit_samples_missing"], "missing_round_count": invalid}
    result["sample_count"] = len(samples)
    result["missing_round_count"] = invalid
    if len({r["prev_close"] for r in samples}) != 1 or conflicts:
        return {**result, "reasons": ["price_basis_or_source_clock_conflict"]}
    clocks = [_clock(r["source_at"]) for r in samples if _clock(r["source_at"]) <= horizon]
    bounds = [exit_at, *clocks, horizon]
    gaps = [max(0, _session_seconds(b)-_session_seconds(a)) for a, b in zip(bounds, bounds[1:])]
    gap = max(gaps, default=0)
    complete = not invalid and gap <= policy.max_sample_gap_sec
    result.update(coverage="complete_sampled_window" if complete else "partial",
                  max_session_gap_sec=round(gap, 3), first_sample_at=samples[0]["source_at"],
                  last_sample_at=samples[-1]["source_at"])
    hi, lo = max(samples, key=lambda r: r["price"]), min(samples, key=lambda r: r["price"])
    peak, low = (hi["price"]/price-1)*100, (lo["price"]/price-1)*100
    result.update(sampled_peak_after_exit_pct=round(peak, 6),
                  sampled_low_after_exit_pct=round(low, 6), peak_at=hi["source_at"],
                  intraday_rebound="true" if peak >= 2-1e-9 else "false" if complete and as_of >= close_at else "unknown")
    terminals = [r for r in samples if time(15) <= _clock(r["source_at"]).time() <= time(15, 5)]
    if as_of < datetime.combine(exit_at.date(), time(15, 15)) or not terminals:
        return {**result, "status": "pending_close" if as_of < close_at else "unknown",
                "reasons": ["terminal_close_not_confirmed"]}
    if len({r["price"] for r in terminals}) != 1:
        return {**result, "reasons": ["terminal_close_conflict"]}
    terminal = terminals[-1]
    close_return = (terminal["price"]/price-1)*100
    basis = _number(cycle.get("entry_basis_before_final_exit"))
    # Terminal quotes are observations, not immutable formal close-health proof.
    result["terminal_quote_after_exit_pct"] = round(close_return, 6)
    if basis and basis > 0:
        above_cost = (terminal["price"]/basis-1)*100
        result["terminal_quote_vs_entry_basis_pct"] = round(above_cost, 6)
    result["terminal_quote_at"] = terminal["source_at"]
    result["reasons"].append("formal_close_health_unavailable")
    if not terminal.get("limit_up"):
        result["reasons"].append("frozen_limit_price_missing")
    if not complete:
        result["reasons"].append("sampling_coverage_partial")
    result["status"] = "partial"  # L2/L3 remain unknown without formal close health.
    return result


def _entry_evidence(order):
    raw = _object(order.risk_json).get("experiment_entry")
    if not isinstance(raw, dict):
        return {}
    return {"active": raw.get("active"), "strategy_version": raw.get("strategy_version"),
            **{key: raw.get(key) if isinstance(raw.get(key), dict) else {}
               for key in ("entry_sentiment", "entry_regime", "entry_bull_bear")}}


def _fill_identity(trade, pairs):
    if len(pairs) != 1:
        return "fill_missing_or_ambiguous"
    fill, order = pairs[0]
    quantities = [_number(v) for v in (trade.amount, fill.quantity, order.quantity)]
    if any(v is None or v <= 0 or not v.is_integer() for v in quantities):
        return "fill_identity_conflict"
    if (fill.side != trade.trade_type or fill.code != trade.code or order.code != trade.code
            or order.side != trade.trade_type or fill.quantity != trade.amount
            or fill.filled_at != trade.trade_time or order.strategy_version != trade.strategy_version
            or order.created_at is None or order.created_at > fill.filled_at
            or quantities[2] < quantities[1]):
        return "fill_identity_conflict"
    for a, b in ((fill.price, trade.price), (fill.commission, trade.commission), (fill.tax, trade.tax)):
        x, y = _number(a), _number(b)
        if x is None or y is None or abs(x-y) > 1e-6:
            return "fill_price_or_fee_conflict"
    if not fill.fill_round_id or fill.fill_round_id != trade.fill_round_id:
        return "fill_round_unproven"
    return ""


async def build_post_exit_report(db, *, start_date, end_date, as_of, archive_root=None, policy=None, versions=None):
    """Never autoflush caller business state; CLI additionally enforces read-only SQLite."""
    with db.no_autoflush:
        return await _build_post_exit_report(db, start_date=start_date, end_date=end_date,
            as_of=as_of, archive_root=archive_root if archive_root is not None else settings.QUOTE_ROUND_ARCHIVE_DIR,
            policy=policy, versions=versions)


async def _build_post_exit_report(db, *, start_date, end_date, as_of, archive_root, policy=None, versions=None):
    """Caller owns a read-only snapshot. Full history precedes window filtering."""
    if _clock(as_of) is None or start_date > end_date or end_date > as_of.date():
        raise ValueError("invalid research date/as_of boundary")
    policy = policy or PostExitPolicy()
    accounts = list((await db.scalars(select(PaperAccount).order_by(PaperAccount.id))).all())
    if versions is None:
        from app.api.v1.paper import _strategy_version
        versions = {a.account_name: _strategy_version(a.account_name) for a in accounts}
    trades = list((await db.scalars(select(PaperTradeLog).where(
        PaperTradeLog.trade_time <= as_of).order_by(PaperTradeLog.trade_time, PaperTradeLog.id))).all())
    pairs = (await db.execute(select(TradeFill, TradeOrder).join(
        TradeOrder, TradeOrder.order_id == TradeFill.order_id).where(
        TradeFill.broker == "paper", TradeOrder.broker == "paper",
        TradeFill.filled_at <= as_of, TradeOrder.created_at <= as_of))).all()
    by_account, matched = defaultdict(list), defaultdict(list)
    for trade in trades:
        by_account[trade.account_id].append(trade)
    for fill, order in pairs:
        if fill.broker_trade_id:
            matched[(order.account_id, str(fill.broker_trade_id))].append((fill, order))
    calendar = dict((await db.execute(select(TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day).where(
        TradeCalendarModel.trade_date >= start_date, TradeCalendarModel.trade_date <= end_date))).all())
    cycles = []
    for account in accounts:
        account_trades = by_account[account.id]
        evidence = {str(t.id): _entry_evidence(matched[(account.account_name, str(t.id))][0][1])
                    for t in account_trades if len(matched[(account.account_name, str(t.id))]) == 1}
        audit = []
        complete_cycles(account_trades, evidence, version=versions.get(account.account_name, ""),
                        start_date=settings.PAPER_EXPERIMENT_START_DATE, audit_cycles=audit)
        by_id = {t.id: t for t in account_trades}
        for cycle in audit:
            exit_at = _clock(cycle["exit_at"])
            if not start_date <= exit_at.date() <= end_date:
                continue
            cycle.update(account_id=account.id, account_name=account.account_name,
                         cycle_key=f"{account.id}:{cycle.get('first_buy_trade_id', 'unknown')}:{cycle['exit_trade_id']}",
                         reference_basis="last_liquidating_trade", identity_status="valid")
            errors = cycle["exclusion_reasons"]
            if not cycle.get("complete"):
                errors.append("not_complete_cycle")
            for trade_id in cycle.get("trade_ids", []):
                t = by_id[trade_id]
                reason = _fill_identity(t, matched[(account.account_name, str(t.id))])
                if reason and reason not in errors:
                    errors.append(reason)
                if _number(t.price) is None or t.price <= 0 or any(_number(v) is None or v < 0 for v in (t.commission, t.tax)):
                    errors.append("invalid_trade_price_or_fee")
                if t.trade_type == "buy" and t.trade_time.date() == exit_at.date():
                    errors.append("same_day_buy_full_exit_t1_conflict")
            if calendar.get(exit_at.date()) is not True:
                errors.append("exit_calendar_unknown")
            final_trade = by_id[cycle["exit_trade_id"]]
            cycle["fill_round_id"] = final_trade.fill_round_id
            cycle["exit_reason"] = final_trade.reason
            cycle["fill_ids"] = [p[0].fill_id for tid in cycle.get("trade_ids", [])
                                for p in matched[(account.account_name, str(tid))]]
            if errors:
                cycle["identity_status"] = "excluded_or_unproven"
            cycle["actual_pnl_evidence"] = "invalid" if any(
                e.startswith("fill_") or e in {"invalid_trade_price_or_fee", "oversold", "not_complete_cycle"}
                for e in errors) else "trade_log_cashflows"
            if cycle["actual_pnl_evidence"] == "invalid":
                for key in ("net_pnl", "actual_fees", "final_exit_fees", "final_exit_net_cash"):
                    cycle[key] = None
            cycles.append(cycle)
    codes_by_day = defaultdict(set)
    for cycle in cycles:
        if cycle["identity_status"] == "valid" and cycle.get("eligible"):
            codes_by_day[_clock(cycle["exit_at"]).date()].add(cycle["code"])
    rounds = list((await db.scalars(select(QuoteRound).where(
        QuoteRound.trade_date >= start_date, QuoteRound.trade_date <= end_date,
        QuoteRound.committed_at <= as_of).order_by(QuoteRound.committed_at, QuoteRound.id))).all())
    earliest_exit = {}
    for cycle in cycles:
        if cycle["identity_status"] == "valid" and cycle.get("eligible"):
            stamp = _clock(cycle["exit_at"])
            earliest_exit[stamp.date()] = min(earliest_exit.get(stamp.date(), stamp), stamp)
    rounds = [r for r in rounds if r.trade_date in earliest_exit and r.committed_at > earliest_exit[r.trade_date]]
    archive_refs = tuple(QuoteArchiveRef(
        round_id=r.round_id, trade_date=r.trade_date, committed_at=r.committed_at,
        source=r.source, quality_status=r.quality_status, archive_status=r.archive_status,
        archive_path=r.archive_path, focus_path=r.focus_path,
        config_version=r.config_version, code_version=r.code_version,
    ) for r in rounds)
    # Never share Session/ORM instances with the worker. File/hash/Parquet work
    # must not occupy the scheduler event loop; all business queries remain here.
    series, manifest = await asyncio.to_thread(
        load_quote_archive, archive_refs,
        {day: frozenset(codes) for day, codes in codes_by_day.items()},
        root=archive_root, policy=policy,
    )
    for cycle in cycles:
        cycle["evaluation"] = evaluate_post_exit_labels(
            cycle, series.get((_clock(cycle["exit_at"]).date(), cycle["code"]), []), as_of=as_of, policy=policy)
    report = _owned_json({
        "report_version": "post_exit_d0_v1", "as_of_at": as_of.isoformat(),
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "policy": policy.contract(), "read_only": True,
        "protocol_start_date": settings.PAPER_EXPERIMENT_START_DATE,
        "requested_current_versions": versions,
        "evidence_hash": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        "archive_availability": "retrospective_read_no_historical_ready_at",
        "warning": "Archive ready status is observed now; source/commit cutoffs do not prove historical file availability. Sampled price markout is not executable profit.",
        "manifest": manifest, "cycles": cycles,
        "summary": {"full_exits": sum(bool(c.get("complete")) for c in cycles),
                    "records": len(cycles),
                    "statuses": dict(Counter(c["evaluation"]["status"] for c in cycles)),
                    "exclusion_reasons": dict(Counter(reason for c in cycles for reason in set(c["exclusion_reasons"])))},
    })
    report["report_data_hash"] = hashlib.sha256(json.dumps(
        report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return report
