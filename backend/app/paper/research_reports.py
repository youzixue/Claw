"""Independent read-only daily research publication; never uses the trading session."""
import asyncio
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import uuid

from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.models.governance import TradeCalendarModel

ROOT = Path(__file__).resolve().parents[3]


def _database_path(database_url):
    url = make_url(database_url)
    if url.drivername not in {"sqlite", "sqlite+aiosqlite"} or url.query or not url.database:
        raise ValueError("research publication requires an existing local SQLite file")
    path = Path(url.database)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("research database must be an existing absolute file")
    return path.resolve()


def _publish(result, directory, at):
    """Prepare fully before atomically publishing; never overwrite a past report."""
    content = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2,
                          allow_nan=False) + "\n").encode()
    digest = hashlib.sha256(content).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    name = f"paper-research-{at:%Y%m%dT%H%M%S%f}-{digest[:16]}.json"
    destination = directory / name
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if destination.read_bytes() != content:
                raise ValueError("immutable research report collision")
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(destination), "sha256": digest}


async def publish_daily_paper_research(*, now=None, database_url=None, output_dir=None):
    """Scheduled 15:50/20:45; calendar is read, never fetched or repaired here."""
    from app.paper.signal_research import build_parallel_research_report
    from app.paper.post_exit_research import build_post_exit_report

    if not settings.PAPER_CONTINUOUS_EXPERIMENT_ENABLED:
        return {"status": "disabled", "read_only": True}
    at = now or datetime.now()
    if not isinstance(at, datetime) or at.tzinfo is not None or at > datetime.now():
        raise ValueError("research as_of must be a non-future Shanghai local clock")
    if at.time() < time(15, 45):
        return {"status": "before_close_review", "read_only": True}
    start = date.fromisoformat(settings.PAPER_EXPERIMENT_START_DATE)
    if start > at.date():
        return {"status": "before_experiment", "read_only": True}
    directory = Path(output_dir or ROOT / "outputs/paper_research").resolve()
    if not directory.is_relative_to((ROOT / "outputs").resolve()):
        raise ValueError("research output must stay under project outputs")
    database = _database_path(database_url or settings.DATABASE_URL)
    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true")

    @event.listens_for(engine.sync_engine, "connect")
    def readonly(connection, _):
        connection.execute("PRAGMA query_only=ON")

    try:
        async with async_sessionmaker(engine, autoflush=False)() as db:
            await db.execute(text("BEGIN"))
            is_day = await db.scalar(select(TradeCalendarModel.is_trade_day).where(
                TradeCalendarModel.trade_date == at.date()))
            if is_day is not True:
                return {"status": "calendar_unknown" if is_day is None else "non_trading_day",
                        "read_only": True, "as_of": at.isoformat()}
            sections = {}
            # SAVEPOINT protects independent sections from a read query error.
            # This is a separate query_only connection, never the trading session.
            for key, builder, kwargs in (
                ("signal_portfolio", build_parallel_research_report,
                 {"start_date": start, "end_date": at.date(), "as_of": at}),
                ("post_exit", build_post_exit_report,
                 {"start_date": at.date(), "end_date": at.date(), "as_of": at,
                  "archive_root": settings.QUOTE_ROUND_ARCHIVE_DIR}),
            ):
                try:
                    async with db.begin_nested():
                        report = await builder(db, **kwargs)
                    sections[key] = {"status": "built", "report": report}
                except Exception as exc:
                    # Do not expose SQL/paths/credentials or count failure as zero samples.
                    sections[key] = {"status": "unavailable", "error_type": type(exc).__name__,
                                     "report": None}
        result = {"schema": "paper_daily_research_v1", "as_of": at.isoformat(),
                  "read_only": True, "database_snapshot": "single_explicit_read_transaction",
                  "report_availability": "generated_now_not_historically_reconstructed",
                  "sections": sections}
        published = await asyncio.to_thread(_publish, result, directory, at)
        return {"status": "published" if all(s["status"] == "built" for s in sections.values()) else "partial",
                "read_only": True, "as_of": at.isoformat(), **published,
                "sections": {key: value["status"] for key, value in sections.items()}}
    finally:
        await engine.dispose()


def build_frozen_cycle_expectancy(evidence, *, account_ids, start_date, end_date, as_of):
    """Offline historical ledger readout; no DB, current policy, or execution calls.

    Reuses accounting_snapshot's inventory cycles, including weighted scale-ins,
    proportional entry fees and whole-cycle probe exclusions. Exact historical
    version sets are never mapped to today's protocol. A cycle is one observation,
    not each partial sale. Closed-window selection uses closing date; entry cohorts
    are reported separately and are descriptive, not a fitted walk-forward test.
    """
    from collections import defaultdict
    from decimal import Decimal
    import math
    import statistics
    from app.paper.accounting import accounting_snapshot

    if (not isinstance(start_date, date) or not isinstance(end_date, date)
            or not isinstance(as_of, datetime) or as_of.tzinfo is not None
            or start_date > end_date or end_date > as_of.date()):
        raise ValueError("explicit valid dates and naive Shanghai as_of required")
    if (not isinstance(evidence, dict) or not account_ids
            or any(type(i) is not int or i <= 0 for i in account_ids)
            or len(set(account_ids)) != len(account_ids)):
        raise ValueError("explicit unique account ids and frozen evidence required")
    for key in ("accounts", "trades", "positions"):
        if not isinstance(evidence.get(key), list):
            raise ValueError("frozen evidence list missing: " + key)
        if any(not isinstance(row, dict) for row in evidence[key]):
            raise ValueError("invalid frozen evidence row: " + key)
        # A row without identity cannot be silently assigned or dropped.
        if any(type(row.get("id")) is not int for row in evidence[key]):
            raise ValueError("frozen row id missing: " + key)
        if len({row["id"] for row in evidence[key]}) != len(evidence[key]):
            raise ValueError("duplicate frozen row id: " + key)
        if key != "accounts" and any(type(row.get("account_id")) is not int
                                      for row in evidence[key]):
            raise ValueError("frozen account identity missing: " + key)

    def stats(cycles):
        values = [Decimal(str(c["realized_net_pnl"])) for c in cycles]
        wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
        n = len(values)
        money = lambda x: round(float(x), 2)
        ordered = sorted(values)
        tail = ordered[:max(1, math.ceil(n * .2))] if n else []
        return {
            "closed_cycles": n, "wins": len(wins), "losses": len(losses),
            "flat": sum(v == 0 for v in values),
            "net_pnl": money(sum(values)) if n else None,
            "expectancy_yuan": money(sum(values) / n) if n else None,
            "win_rate_pct": round(100 * len(wins) / n, 6) if n else None,
            "average_win_yuan": money(sum(wins) / len(wins)) if wins else None,
            "average_loss_yuan": money(sum(losses) / len(losses)) if losses else None,
            "profit_factor": round(float(sum(wins) / -sum(losses)), 6) if losses else None,
            "median_yuan": money(statistics.median(values)) if n else None,
            "worst_cycle_yuan": money(ordered[0]) if n else None,
            "bottom_20pct_count": len(tail),
            "bottom_20pct_mean_yuan": money(sum(tail) / len(tail)) if tail else None,
            "net_without_worst_cycle_yuan": money(sum(values) - ordered[0]) if n > 1 else None,
            "distinct_entry_dates": len({c["buy_time"][:10] for c in cycles}),
            "distinct_codes": len({c["code"] for c in cycles}),
            "sample_status": "no_completed_cycles" if not n else (
                "small_descriptive_sample" if n < 30 else "descriptive_only_dependence_unresolved"),
            "sample_status_note": "30 is a display warning, not power/significance/promotion criterion",
            "positive_expectancy_proven": False,
            "tail_scope": "descriptive_realized_cycle_tail_not_intrahold_MAE_or_CVaR_estimate",
        }

    account_results, all_cycles, all_open_cycles = [], [], []
    for aid in sorted(account_ids):
        matches = [a for a in evidence["accounts"] if a["id"] == aid]
        trades = [t for t in evidence["trades"] if t["account_id"] == aid]
        positions = [p for p in evidence["positions"] if p["account_id"] == aid]
        row = {"account_id": aid, "account_name": matches[0].get("account_name") if matches else None,
               "input_trade_count": len(trades), "input_position_count": len(positions),
               "status": "unavailable", "issues": [], "cycles": [], "statistics": None}
        account_results.append(row)
        if len(matches) != 1:
            row["issues"].append("account_missing_or_ambiguous")
            continue
        for t in trades:
            try:
                at = datetime.fromisoformat(str(t.get("trade_time")))
                if at.tzinfo is not None or at > as_of:
                    raise ValueError()
                if any(type(t.get(k)) not in (bool, int) or t.get(k) not in (0, 1)
                       for k in ("forced_probe", "excluded_from_performance")):
                    raise ValueError()
                if t.get("strategy_version") is not None and not isinstance(t["strategy_version"], str):
                    raise ValueError()
            except (ValueError, TypeError):
                row["issues"].append("invalid_trade_clock_flags_or_version:" + str(t["id"]))
        if any(type(p.get("is_closed")) not in (bool, int) or p.get("is_closed") not in (0, 1)
               for p in positions):
            row["issues"].append("position_closed_state_unknown")
        if row["issues"]:
            continue
        open_positions = [p for p in positions if not p["is_closed"]]
        snapshot = accounting_snapshot(matches[0], trades, open_positions, as_of=as_of.date())
        row["accounting_status"] = snapshot["status"]
        row["issues"].extend(snapshot["issues"])
        if snapshot["status"] != "ok":
            # Preserve input denominator, do not publish partial successful cycles.
            continue
        row.update(status="evaluated", open_positions=len(open_positions),
                   excluded_complete_cycles=snapshot["excluded_cycle_count"],
                   all_eligible_complete_cycles=len(snapshot["eligible_closed_cycles"]),
                   closed_before_window=0, closed_after_window=0,
                   realized_net_pnl_all_history=snapshot["realized_net_pnl"],
                   ledger_to_economic_bridge=snapshot["legacy_entry_fee_adjustment"],
                   cash_reconciliation_residual=snapshot["cash_reconciliation_residual"],
                   asset_reconciliation_residual=snapshot["asset_reconciliation_residual"])
        ordered = sorted(trades, key=lambda t: (t["trade_time"], t["id"]))
        by_id = {t["id"]: t for t in ordered}
        for cycle in snapshot["eligible_closed_cycles"]:
            close_day = date.fromisoformat(cycle["close_time"][:10])
            if close_day < start_date:
                row["closed_before_window"] += 1
                continue
            if close_day > end_date:
                row["closed_after_window"] += 1
                continue
            first = by_id[cycle["buy_trade_ids"][0]]
            last = by_id[cycle["sell_trade_id"]]
            legs = [t for t in ordered if t["code"] == cycle["code"]
                    and (first["trade_time"], first["id"]) <= (t["trade_time"], t["id"])
                    <= (last["trade_time"], last["id"])]
            versions = sorted({t.get("strategy_version") or "UNKNOWN" for t in legs})
            item = {**cycle, "account_id": aid, "account_name": row["account_name"],
                    "strategy_versions": versions,
                    "version_scope": "single_exact_version" if len(versions) == 1 and versions[0]
                        not in ("UNKNOWN", "legacy_unversioned") else "mixed_or_legacy_unknown",
                    "sell_trade_ids": [t["id"] for t in legs if t["trade_type"] == "sell"],
                    "mode": "unknown", "mode_reason": "no_validated_frozen_entry_mode_join",
                    "entry_before_requested_window": first["trade_time"][:10] < start_date.isoformat(),
                    "hold_calendar_days": round((datetime.fromisoformat(last["trade_time"])
                         - datetime.fromisoformat(first["trade_time"])).total_seconds() / 86400, 6)}
            row["cycles"].append(item)
            all_cycles.append(item)
        row["open_cycles"] = []
        for position in open_positions:
            basis = snapshot["positions"][position["id"]]
            first = by_id[basis["buy_trade_ids"][0]]
            legs = [t for t in ordered if t["code"] == position["code"]
                    and (t["trade_time"], t["id"]) >= (first["trade_time"], first["id"])]
            pending = {"account_id": aid, "position_id": position["id"],
                       "code": position["code"], "buy_time": first["trade_time"],
                       "strategy_versions": sorted({t.get("strategy_version") or "UNKNOWN" for t in legs}),
                       "excluded_from_performance": basis["excluded_from_performance"],
                       "status": "right_censored", "realized_complete_cycle_pnl": None,
                       "mode": "unknown"}
            row["open_cycles"].append(pending)
            all_open_cycles.append(pending)
        row["statistics"] = stats(row["cycles"])
        row["unavailable_mode_cycles"] = len(row["cycles"])

    grouped, cohorts = defaultdict(list), defaultdict(list)
    for c in all_cycles:
        key = (c["account_id"], tuple(c["strategy_versions"]))
        grouped[key].append(c)
        day = int(c["buy_time"][8:10])
        period = "01_10" if day <= 10 else "11_20" if day <= 20 else "21_end"
        cohorts[(*key, c["buy_time"][:7] + ":" + period)].append(c)
    open_groups, open_cohorts = defaultdict(list), defaultdict(list)
    for c in all_open_cycles:
        key = (c["account_id"], tuple(c["strategy_versions"]))
        grouped.setdefault(key, [])
        open_groups[key].append(c)
        day = int(c["buy_time"][8:10])
        period = "01_10" if day <= 10 else "11_20" if day <= 20 else "21_end"
        cohort_key = (*key, c["buy_time"][:7] + ":" + period)
        cohorts.setdefault(cohort_key, [])
        open_cohorts[cohort_key].append(c)
    strata = [{"account_id": aid, "strategy_versions": list(versions),
               "right_censored_inventory_cycles": len(open_groups[(aid, versions)]),
               "excluded_open_cycles": sum(c["excluded_from_performance"]
                                          for c in open_groups[(aid, versions)]), **stats(cs)}
              for (aid, versions), cs in sorted(grouped.items())]
    entry_cohorts = [{"account_id": aid, "strategy_versions": list(versions),
                     "entry_period": period,
                     "right_censored_inventory_cycles": len(open_cohorts[(aid, versions, period)]),
                     **stats(cs)}
                    for (aid, versions, period), cs in sorted(cohorts.items())]
    report = {
        "schema": "frozen_cycle_expectancy_v1", "read_only": True,
        "production_permission": False, "production_rules_changed": False,
        "as_of": as_of.isoformat(), "source_capture_as_of": evidence.get("as_of_at"),
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "account_ids": sorted(account_ids), "accounts": account_results,
        "summary": {
            "requested_accounts": len(account_ids),
            "evaluated_accounts": sum(r["status"] == "evaluated" for r in account_results),
            "unavailable_accounts": sum(r["status"] != "evaluated" for r in account_results),
            "eligible_window_cycles": len(all_cycles),
            "input_trades_in_scope": sum(r["input_trade_count"] for r in account_results),
            "input_trades_outside_selected_accounts": sum(t["account_id"] not in account_ids
                                                         for t in evidence["trades"]),
            "mode_unknown_cycles": len(all_cycles),
            "right_censored_inventory_cycles": len(all_open_cycles),
        },
        "strata": strata, "entry_cohorts": entry_cohorts,
        "mode_expectancy": {"status": "unavailable",
             "reason": "ledger_has_no_validated_frozen_mode_and_source_path_join",
             "unknown_cycle_count": len(all_cycles), "not_silently_filtered": True},
        "sustained_shape_hypothesis": {"status": "not_tested",
             "reason": "need_same_candidate_baseline_variant_paired_source_frames_and_T1_execution_outcomes",
             "confirmed_shape_is_positive_expectancy": False},
        "contract": {
            "scope": "surviving_frozen_ledger_historical_versions_not_current_protocol",
            "sample_unit": "account_code_zero_to_zero_inventory_cycle_not_sell_fill",
            "selection": "cycle_close_date_in_requested_window_entry_cohorts_separate",
            "return_basis": "weighted_actual_buy_sell_cashflows_net_of_paid_fees_yuan",
            "fixed_T1_return": None, "intraday_5_15_30min_is_T1_net_return": False,
            "current_configuration_used": False, "historical_configuration_reconstructed": False,
            "independent_samples_proven": False, "walk_forward_validation": "not_performed",
            "open_inventory": "right_censored_not_losses_or_wins",
            "missing_account_or_basis": "unavailable_with_input_denominator_not_zero",
            "promotion_evidence_eligible": False,
        },
    }
    # Input fingerprint includes unavailable/unused evidence, not only winning rows.
    report["input_sha256"] = hashlib.sha256(json.dumps(evidence, ensure_ascii=False,
        sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    report["data_sha256"] = hashlib.sha256(json.dumps(report, ensure_ascii=False,
        sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    return report
