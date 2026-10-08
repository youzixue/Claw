"""Audited local evidence projections. No account/service initializers or network."""
from __future__ import annotations
from datetime import date, datetime, time, timedelta
import hashlib
import json

from sqlalchemy import and_, func, or_, select, union

from app.models.governance import TradeCalendarModel
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperDailyOutcome, PaperPosition, PaperTradeLog
from app.models.review import DailyReviewSnapshot
from app.models.stock import BrokenLimitPool, LimitUpPool, QuoteRound, StockKline
from app.models.trading import TradeFill, TradeOrder
from app.paper.experiment import EXPERIMENT_ACCOUNTS
from app.review.evidence_store import local_now, owned, record

MAX_ACCOUNT_TRADES = 20000


def envelope(day, at):
    return {"trade_date": day.isoformat(), "as_of": at.isoformat(), "timezone": "Asia/Shanghai",
            "read_at": local_now().isoformat(), "read_only": True,
            "database_snapshot": "single_explicit_read_transaction",
            "historical_projection_reconstructed": False}


async def calendar_context(db, day):
    row = await db.get(TradeCalendarModel, day)
    previous = await db.scalar(select(func.max(TradeCalendarModel.trade_date)).where(
        TradeCalendarModel.trade_date < day, TradeCalendarModel.is_trade_day.is_(True)))
    next_day = await db.scalar(select(func.min(TradeCalendarModel.trade_date)).where(
        TradeCalendarModel.trade_date > day, TradeCalendarModel.is_trade_day.is_(True)))
    # A missing intervening date can hide a trading day.
    gaps = None
    if previous:
        count = await db.scalar(select(func.count()).select_from(TradeCalendarModel).where(
            TradeCalendarModel.trade_date >= previous, TradeCalendarModel.trade_date <= day))
        gaps = (day-previous).days + 1 - count
    return {"status": "calendar_unknown" if row is None else (
                "trading_day" if row.is_trade_day else "non_trading_day"),
            "is_trade_day": row.is_trade_day if row else None,
            "expected_previous_trade_date": previous.isoformat() if previous and gaps == 0 else None,
            "previous_stored_open_date": previous.isoformat() if previous else None,
            "intervening_calendar_missing_days": gaps,
            "next_stored_trade_date": next_day.isoformat() if next_day else None,
            "source": "stored_trade_calendar_no_sync", "historical_calendar_available_at": None}


def _fingerprint(result):
    result["input_fingerprint"] = hashlib.sha256(json.dumps(
        {k: v for k, v in result.items() if k not in {"read_at", "input_fingerprint"}},
        ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return result


async def readiness(db, *, day, at, phase):
    from app.review.research_artifacts import artifact_catalog
    context = await calendar_context(db, day)
    result = {**envelope(day, at), "phase": phase, "calendar": context, "components": {}}
    if context["status"] != "trading_day":
        result["status"] = "skipped_non_trading_day" if context["is_trade_day"] is False else "blocked_calendar_unknown"
        return _fingerprint(result)
    analysis_day = day if phase == "postmarket" else (
        date.fromisoformat(context["expected_previous_trade_date"])
        if context["expected_previous_trade_date"] else None)
    result["analysis_trade_date"] = analysis_day.isoformat() if analysis_day else None
    latest_kline = await db.scalar(select(func.max(StockKline.trade_date)).where(
        StockKline.trade_date <= (analysis_day or day-timedelta(days=1))))
    snapshot = await db.scalar(select(DailyReviewSnapshot).where(
        DailyReviewSnapshot.review_date == day, DailyReviewSnapshot.phase == phase,
        DailyReviewSnapshot.as_of_at <= at, DailyReviewSnapshot.created_at <= at,
    ).order_by(DailyReviewSnapshot.as_of_at.desc(), DailyReviewSnapshot.id.desc()).limit(1))
    result["components"]["snapshot"] = {"status": "available" if snapshot else "missing",
        "snapshot": {k: record(snapshot)[k] for k in (
            "id", "review_date", "analysis_trade_date", "as_of_at", "created_at",
            "schema_version", "data_version", "quality_status", "quality_score")} if snapshot else None}
    result["components"]["daily_kline"] = {"status": "available" if latest_kline == analysis_day else "stale_or_missing",
        "expected_date": result["analysis_trade_date"],
        "actual_date": latest_kline.isoformat() if latest_kline else None,
        "historical_pit": False, "basis": "mutable_mixed_basis_compatibility_projection"}
    if analysis_day:
        outcomes = list((await db.scalars(select(PaperDailyOutcome).where(
            PaperDailyOutcome.trade_date == analysis_day, PaperDailyOutcome.is_terminal.is_(True),
            PaperDailyOutcome.finalized_at <= at))).all())
        accounts = list((await db.scalars(select(PaperAccount).where(
            PaperAccount.account_name.in_(EXPERIMENT_ACCOUNTS)))).all())
        id_names = {r.id: r.account_name for r in accounts}
        by_name = {id_names.get(r.account_id): r for r in outcomes}
        result["components"]["daily_outcomes"] = {
            "status": "available" if all(n in by_name for n in EXPERIMENT_ACCOUNTS) else "partial",
            "expected_accounts": list(EXPERIMENT_ACCOUNTS),
            "available_accounts": [n for n in EXPERIMENT_ACCOUNTS if n in by_name],
            "missing_accounts": [n for n in EXPERIMENT_ACCOUNTS if n not in by_name],
            "note": "stored finalized rows only; not a request to finalize"}
        catalog = artifact_catalog(analysis_day, as_of=at)
        result["components"]["research_artifacts"] = {"status": "available" if catalog["items"] else "missing", **catalog}
    after_deadline = phase == "premarket" and (day != at.date() or at.time() >= time(9, 15))
    # 15:30 is the human-selected research window, not the producer finalization time.
    # Later terminal outcomes/artifacts/snapshots remain unavailable at this cutoff;
    # missing components still yield partial and never trigger a producer.
    before_window = phase == "postmarket" and (day == at.date() and at.time() < time(15, 30))
    result["premarket_deadline"] = "09:15:00 Asia/Shanghai"
    result["late_delivery"] = after_deadline
    statuses = [c["status"] for c in result["components"].values()]
    result["status"] = ("blocked_after_premarket_deadline" if after_deadline else
                        "blocked_before_close" if before_window else
                        "ready" if analysis_day and all(s == "available" for s in statuses)
                        and snapshot and snapshot.quality_status == "good" else "partial")
    return _fingerprint(result)


async def _account_rows(db, name=None):
    query = select(PaperAccount).where(PaperAccount.account_name.in_(
        [name] if name else EXPERIMENT_ACCOUNTS))
    return list((await db.scalars(query.order_by(PaperAccount.id))).all())


def _account_identity(accounts, name):
    """Current active identity is distinct from all-version historical facts."""
    instances = [a for a in accounts if a.account_name == name]
    active = [a for a in instances if a.status == "active"]
    identity = {"account_name": name, "instance_count": len(instances),
                "active_instance_count": len(active),
                "active_account_ids": [a.id for a in active],
                "historical_accounts": [{"account_id": a.id, "status": a.status}
                                        for a in instances if a.status != "active"],
                "identity_scope": "current_stored_active_not_historical_as_of"}
    return (active[0] if len(active) == 1 else None), identity


def _json_evidence(raw):
    if not raw:
        return None
    if len(raw.encode()) > 24 * 1024:
        return owned(raw)
    try:
        return owned(json.loads(raw))
    except (TypeError, ValueError):
        return {"status": "invalid_json", "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def _row(row):
    data = record(row)
    # Broker transport blobs/error strings are not research evidence contracts.
    for key in ("raw_json", "error_message", "external_order_id", "broker_trade_id"):
        data.pop(key, None)
    for key in ("risk_json", "candidate_json", "payload_json"):
        if isinstance(data.get(key), str):
            data[key] = _json_evidence(data[key])
    return data


async def _page(db, model, conditions, *, cursor, limit):
    total = await db.scalar(select(func.count()).select_from(model).where(*conditions))
    rows = list((await db.scalars(select(model).where(*conditions, model.id > cursor)
                                 .order_by(model.id).limit(limit+1))).all())
    return {"total_matching": total, "items": [_row(r) for r in rows[:limit]],
            "next_cursor": rows[limit-1].id if len(rows) > limit else None,
            "cursor": cursor, "returned": min(len(rows), limit), "truncation": "keyset_paged"}


async def execution_evidence(db, *, day, at, account_name=None, code=None,
                             section="summary", cursor=0, limit=50):
    result = envelope(day, at)
    accounts = await _account_rows(db, account_name)
    names = [account_name] if account_name else list(EXPERIMENT_ACCOUNTS)
    ids = [a.id for a in accounts]
    if section == "summary":
        from app.paper.accounting import load_accounting
        entries = []
        for name in names:
            account, identity = _account_identity(accounts, name)
            if account is None:
                entries.append({**identity, "status": "account_missing_or_ambiguous",
                                "reason": "no_unique_active_account"})
                continue
            counts = await db.scalar(select(func.count()).select_from(PaperTradeLog).where(
                PaperTradeLog.account_id == account.id))
            current = _row(account)
            entry = {**identity, "account_id": account.id, "status": "stored_projection",
                     "all_version_trade_count": counts, "stored_account": current,
                     "valuation_at": None, "valuation_freshness": "unknown",
                     "stored_projection_not_historical_as_of": True}
            latest = await db.scalar(select(func.max(PaperTradeLog.trade_time)).where(
                PaperTradeLog.account_id == account.id))
            # Current cash/positions must never be spliced into a past inventory.
            if counts > MAX_ACCOUNT_TRADES:
                entry.update(accounting_status="unavailable_budget", accounting=None)
            elif latest and latest > at:
                entry.update(accounting_status="unavailable_current_projection_has_future_trades", accounting=None)
            else:
                accounting = await load_accounting(db, account, as_of=day)
                entry["accounting_status"] = accounting["status"]
                entry["accounting"] = {k: v for k, v in accounting.items()
                    if k not in {"trades", "positions", "eligible_closed_cycles"}}
                entry["positions_accounting"] = accounting["positions"]
                entry["complete_cycles"] = {"count": len(accounting["eligible_closed_cycles"]),
                    "detail_reader": "paper_execution_evidence section=cycles account_name"}
                entry["historical_valuation_pit"] = False
            entries.append(entry)
        result.update(status="ok", accounts=entries, expected_accounts=names,
                      C3_execution_connected=False,
                      performance_scope="all_versions_actual_facts_separate_from_current_protocol")
        return result
    if section == "cycles":
        from app.paper.accounting import load_accounting
        account, identity = _account_identity(accounts, account_name)
        if not account_name or account is None:
            return {**result, **identity, "status": "account_missing_or_ambiguous",
                    "reason": "cycles_require_unique_explicit_active_account"}
        result.update(account_id=account.id, account_identity=identity,
                      cycle_scope="current_active_physical_account_all_strategy_versions")
        count = await db.scalar(select(func.count()).select_from(PaperTradeLog).where(PaperTradeLog.account_id == account.id))
        latest = await db.scalar(select(func.max(PaperTradeLog.trade_time)).where(PaperTradeLog.account_id == account.id))
        if count > MAX_ACCOUNT_TRADES or (latest and latest > at):
            return {**result, "status": "unavailable", "reason": "accounting_budget_or_future_projection",
                    "trade_count": count}
        accounting = await load_accounting(db, account, as_of=day)
        rows = [c for c in accounting["eligible_closed_cycles"]
                if c["close_time"][:10] == day.isoformat() and (not code or c["code"] == code)]
        rows.sort(key=lambda c: c["sell_trade_id"])
        selected = [c for c in rows if c["sell_trade_id"] > cursor]
        result.update(status=accounting["status"], issues=accounting["issues"],
                      items=selected[:limit], total_matching=len(rows),
                      next_cursor=selected[limit-1]["sell_trade_id"] if len(selected)>limit else None,
                      excluded_cycle_count=accounting["excluded_cycle_count"])
        return result
    if section == "orders":
        model, conditions = TradeOrder, [
            TradeOrder.broker == "paper", TradeOrder.account_id.in_(names),
            TradeOrder.trade_date == day, TradeOrder.created_at <= at]
    elif section == "fills":
        order_ids = select(TradeOrder.order_id).where(
            TradeOrder.broker == "paper", TradeOrder.account_id.in_(names))
        model, conditions = TradeFill, [
            TradeFill.broker == "paper", TradeFill.order_id.in_(order_ids),
            TradeFill.trade_date == day, TradeFill.filled_at <= at]
    elif section == "trades":
        model, conditions = PaperTradeLog, [
            PaperTradeLog.account_id.in_(ids), PaperTradeLog.trade_time >= datetime.combine(day, time.min),
            PaperTradeLog.trade_time < datetime.combine(day+timedelta(days=1), time.min),
            PaperTradeLog.trade_time <= at]
    elif section == "positions":
        identities = [_account_identity(accounts, name) for name in names]
        active_ids = [a.id for a, _ in identities if a is not None]
        result["account_identities"] = [identity for _, identity in identities]
        result["position_scope"] = "unique_current_active_accounts_only"
        result["unresolved_accounts"] = [identity["account_name"] for a, identity in identities if a is None]
        result["excluded_non_active_open_position_count"] = await db.scalar(
            select(func.count()).select_from(PaperPosition).where(
                PaperPosition.account_id.in_([a.id for a in accounts if a.status != "active"]),
                PaperPosition.is_closed.is_(False)))
        model, conditions = PaperPosition, [PaperPosition.account_id.in_(active_ids), PaperPosition.is_closed.is_(False)]
    else:
        raise ValueError("unknown execution section")
    if code:
        conditions.append(model.code == code)
    result.update(status="ok", section=section, **await _page(db, model, conditions, cursor=cursor, limit=limit))
    result["current_mutable_state_not_historical_terminal"] = section in {"orders", "positions"}
    result["account_map"] = {str(a.id): a.account_name for a in accounts}
    result["absence_means"] = "no_matching_stored_row_not_proof_of_no_business_event"
    return result


async def decision_trace(db, *, day, at, account_name=None, code=None,
                         run_id=None, strategy_version=None, log_id=None,
                         notification_only=False, cursor=0, limit=50):
    accounts = await _account_rows(db, account_name)
    ids = [a.id for a in accounts]
    conditions = [PaperAutoTradeLog.account_id.in_(ids),
                  PaperAutoTradeLog.trade_date == day, PaperAutoTradeLog.created_at <= at]
    if code:
        conditions.append(PaperAutoTradeLog.code == code)
    if run_id:
        conditions.append(PaperAutoTradeLog.run_id == run_id)
    if strategy_version:
        conditions.append(PaperAutoTradeLog.strategy_version == strategy_version)
    if log_id:
        # Explicit id dereference still obeys all requested scopes.
        conditions.append(PaperAutoTradeLog.id == log_id)
    if notification_only:
        conditions.append(PaperAutoTradeLog.action.in_(("buy_signal", "signal_ingress", "signal_push")))
    page = await _page(db, PaperAutoTradeLog, conditions, cursor=cursor, limit=limit)
    if notification_only:
        from app.paper.signal_research import signal_record, _delivery_evidence
        names = {a.id: a.account_name for a in accounts}
        for item in page["items"]:
            if item["action"] != "buy_signal":
                continue
            row = await db.get(PaperAutoTradeLog, item["id"])
            # Match only an explicit signal_log_id, never merely same code/day.
            matches = list((await db.scalars(select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.account_id == row.account_id,
                PaperAutoTradeLog.trade_date == day, PaperAutoTradeLog.action == "signal_push",
                PaperAutoTradeLog.created_at <= at,
                func.json_valid(PaperAutoTradeLog.candidate_json) == 1,
                func.json_extract(PaperAutoTradeLog.candidate_json, "$.signal_log_id") == row.id,
            ).order_by(PaperAutoTradeLog.id).limit(1001))).all())
            if len(matches) <= 1000:
                item["delivery_evidence"] = _delivery_evidence(
                    signal_record(row, names[row.account_id]), matches, as_of=at)
            else:
                item["delivery_evidence"] = {"status": "unavailable_receipt_budget", "count_at_least": 1001}
            item["user_received_at"] = None
            item["sent_not_user_receipt_or_fill"] = True
    return {**envelope(day, at), "status": "ok", **page,
            "account_map": {str(a.id): a.account_name for a in accounts},
            "missing_trace": "unknown_not_unscreened_or_gate_failure",
            "candidate_trace_semantics": "first_state_transition_with_reused_log_ids_not_every_scan",
            "C3": "separate_paper_c3_events_no_execution_account"}


async def market_universe(db, *, day, at, cursor="", limit=100, include_history=True):
    if day == at.date() and at.time() < time(15, 15):
        return {**envelope(day, at), "status": "unavailable", "reason": "no_intraday_close_backfill"}
    universe = union(select(StockKline.code).where(StockKline.trade_date == day),
                     select(LimitUpPool.code).where(LimitUpPool.trade_date == day, LimitUpPool.quarantined.is_(False)),
                     select(BrokenLimitPool.code).where(BrokenLimitPool.trade_date == day)).subquery()
    total = await db.scalar(select(func.count()).select_from(universe))
    codes = list((await db.scalars(select(universe.c.code).where(universe.c.code > cursor)
                                  .order_by(universe.c.code).limit(limit+1))).all())
    selected = codes[:limit]
    bars = list((await db.scalars(select(StockKline).where(
        StockKline.trade_date == day, StockKline.code.in_(selected)))).all())
    bar_map = {b.code: b for b in bars}
    limits = list((await db.scalars(select(LimitUpPool).where(
        LimitUpPool.trade_date == day, LimitUpPool.code.in_(selected),
        LimitUpPool.quarantined.is_(False)))).all())
    broken = list((await db.scalars(select(BrokenLimitPool).where(
        BrokenLimitPool.trade_date == day, BrokenLimitPool.code.in_(selected)))).all())
    limit_map, broken_map = {r.code: r for r in limits}, {r.code: r for r in broken}
    counts = {}
    for label, predicate in (
        ("up", StockKline.change_pct > 0), ("flat", StockKline.change_pct == 0),
        ("down", StockKline.change_pct < 0), ("missing_return", StockKline.change_pct.is_(None))):
        counts[label] = await db.scalar(select(func.count()).select_from(StockKline).where(
            StockKline.trade_date == day, predicate))
    expected = await db.scalar(select(func.max(QuoteRound.expected_count)).where(QuoteRound.trade_date == day))
    history = {}
    if include_history and selected:
        rank = func.row_number().over(partition_by=StockKline.code,
                                     order_by=StockKline.trade_date.desc()).label("rn")
        ranked = select(StockKline.id, rank).where(
            StockKline.code.in_(selected), StockKline.trade_date <= day,
            StockKline.trade_date >= day-timedelta(days=60)).subquery()
        recent = (await db.scalars(select(StockKline).join(
            ranked, StockKline.id == ranked.c.id).where(ranked.c.rn <= 6)
            .order_by(StockKline.code, StockKline.trade_date))).all()
        for bar in recent:
            history.setdefault(bar.code, []).append(_row(bar))
    from app.review.service import _price_return, _opening_bucket, _intraday_recovery_shape
    items = []
    for code in selected:
        bar, seal, failure = bar_map.get(code), limit_map.get(code), broken_map.get(code)
        open_pct = _price_return(bar.open, bar.prev_close) if bar else None
        low_pct = _price_return(bar.low, bar.prev_close) if bar else None
        recent = history.get(code, [])
        bases = sorted({r["source"] or "unknown" for r in recent})
        items.append({"code": code, "day_kline": _row(bar) if bar else None,
            "limit_up": _row(seal) if seal else None, "broken_limit": _row(failure) if failure else None,
            "recent_daily_bars": recent, "recent_bar_count": len(recent),
            "source_basis_set": bases, "mixed_or_unknown_basis": len(bases) != 1 or "unknown" in bases,
            "outcome_shape": {"opening_bucket": _opening_bucket(open_pct),
                "recovery_shape": _intraday_recovery_shape(open_pct=open_pct, low_pct=low_pct)},
            "price_basis": "mutable_compatibility_daily_projection_not_historical_PIT",
            "strategy_shape_evaluated_as_of": None, "eligible_for_threshold_tuning": False})
    return {**envelope(day, at), "status": "ok", "items": items, "total_stored_universe": total,
        "returned": len(items), "next_cursor": selected[-1] if len(codes)>limit else None,
        "counts": counts, "expected_quote_universe_count": expected,
        "universe_scope": "stored_day_kline_plus_pools_not_verified_all_exchange_listing",
        "full_market_strategy_shape_control": "unavailable_no_frozen_decision_denominator",
        "historical_stock_identity": "unknown_not_current_StockTag_backfill",
        "future_or_late_daily_revisions": "unknown_local_projection_observed_now",
        "intraday_source": "ashare_price_evidence sampled_1m; missing is unknown"}
