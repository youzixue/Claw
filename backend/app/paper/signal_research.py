"""只读全量确认研究；信号、假想收益和真实模拟成交分账，不产生委托。"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math

from sqlalchemy import select

from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperShadowEvent
from app.models.trading import TradeOrder, TradeFill
from app.models.stock import StockKline
from app.models.governance import TradeCalendarModel
from app.paper.experiment import EXPERIMENT_ACCOUNTS, freeze_entry_evidence
from app.data.price_chain import (
    FORMAL_CLOSE_SOURCES as _FORMAL_CLOSE_SOURCES,
    PRICE_CHAIN_MAX_ABS_GAP as _PRICE_CHAIN_MAX_ABS_GAP,
)

SCHEMA = "paper_signal_research_v1"
ACTIONS = ("buy", "deferred_buy", "queue_buy", "skip_buy", "wait_buy", "skip_terminal")


def _object(value):
    try:
        obj = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _clock(value):
    try:
        at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return at if at.tzinfo is None else None
    except (ValueError, TypeError):
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _finite_sum(values):
    """Derived cash/statistics must also remain JSON-safe; never drop bad terms."""
    if any(_number(value) is None for value in values):
        return None
    try:
        return _number(math.fsum(values))
    except (ValueError, OverflowError):
        return None


def _finite_mean(values):
    if not values:
        return None
    # Divide before summing: two finite 1e308 values have a finite mean.
    mean = _finite_sum([value / len(values) for value in values])
    return round(mean, 6) if mean is not None else None


async def freeze_signal_labels(db, account_name, strategy_version, *, at):
    """只用于新信号；缺少证据不读取当前状态反填旧信号。失败不阻断交易。"""
    missing = {"schema": "signal_labels_v1", "observed_at": at.isoformat(),
               "strategy_version": strategy_version, "regime": "unknown",
               "bull_bear": "unknown", "sentiment": "unknown", "status": "unknown"}
    try:
        async with db.begin_nested():
            value = await freeze_entry_evidence(db, account_name, at=at, sentiment={})
        if value.get("strategy_version") != strategy_version:
            return {**missing, "reason": "strategy_version_mismatch"}
        return {**missing, "status": "recorded",
                "regime": value["entry_regime"].get("label") or "unknown",
                "bull_bear": value["entry_bull_bear"].get("label") or "unknown",
                "regime_snapshot_key": value["entry_regime"].get("snapshot_key"),
                "benchmark_source_health_id": value["entry_bull_bear"].get("source_health_id"),
                "sentiment_reason": "no_immutable_sentiment_in_signal_contract"}
    except Exception as exc:
        return {**missing, "reason": "audit_unavailable", "error_type": type(exc).__name__}


@dataclass(frozen=True)
class MarkoutPolicy:
    """显式研究假设，不声称这是历史账户真实成本或可成交资金组合。"""
    quantity: int = 100
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    stamp_tax_rate: float = 0.001
    slippage_pct: float = 0.10

    def __post_init__(self):
        if (isinstance(self.quantity, bool) or not isinstance(self.quantity, int)
                or self.quantity <= 0 or self.quantity % 100 or _number(self.quantity) is None):
            raise ValueError("quantity must be positive whole A-share lots")
        for key in ("commission_rate", "minimum_commission", "stamp_tax_rate", "slippage_pct"):
            raw = getattr(self, key)
            n = _number(raw)
            if not isinstance(raw, (int, float)) or n is None or n < 0:
                raise ValueError("invalid cost assumption: " + key)
        if self.commission_rate >= 1 or self.stamp_tax_rate >= 1 or self.slippage_pct >= 100:
            raise ValueError("invalid percentage assumption")

    def contract(self):
        values = asdict(self)
        version = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]
        return {"version": "fixed_lot_markout_v1-" + version, **values,
                "entry": "signal_reference_plus_slippage", "exit": "source_allowlisted_horizon_close_minus_slippage",
                "scope": "hypothetical_markout_not_executable_portfolio",
                "minimum_fee_applied_per_side": True,
                "outcome_evidence_contract": {
                    "schema": "current_kline_observation_v1",
                    "origin": "StockKline_current_read_snapshot",
                    "validation": "source_allowlist_positive_close_and_price_chain_only",
                    "supplier_finality_verified": False,
                    "price_basis_verified": False,
                    "historical_first_availability_verified": False,
                    "promotion_evidence_eligible": False,
                    "legacy_formal_reason_codes_are_not_certificates": True,
                }}


def horizon_markout(signal, *, horizon, bars, calendar, as_of, policy):
    """严格按已登记交易日取T+1/T+3，不跳过缺K线、停牌或价格断点。"""
    result = {"horizon": horizon, "status": "unknown", "gross_return_pct": None,
              "net_return_pct": None, "exit_date": None, "executable_return": None,
              "outcome_evidence_grade": "current_projection_unverified",
              "promotion_evidence_eligible": False, "close_observations": []}
    if horizon not in (1, 3):
        raise ValueError("supported horizons are 1 and 3")
    if signal.get("evidence_status") not in {"valid", "legacy_record_reference"}:
        return {**result, "reason": "invalid_signal_evidence"}
    if signal.get("queue_order") is True:
        return {**result, "reason": "limit_up_queue_fill_unproven"}
    reference = _number(signal.get("reference_price"))
    if reference is None or reference <= 0:
        return {**result, "reason": "invalid_reference_price"}
    day = date.fromisoformat(signal["trade_date"])
    if calendar.get(day) is not True:
        return {**result, "reason": "signal_day_calendar_unknown"}
    cursor, days = day, []
    while len(days) < horizon:
        cursor += timedelta(days=1)
        if cursor > as_of.date():
            return {**result, "status": "right_censored", "reason": "horizon_not_elapsed"}
        if cursor not in calendar:
            return {**result, "reason": "calendar_gap"}
        if calendar[cursor] is True:
            days.append(cursor)
    result["exit_date"] = days[-1].isoformat()
    if days[-1] == as_of.date() and as_of.time() < time(15, 15):
        return {**result, "status": "right_censored", "reason": "horizon_close_not_final"}
    prior, endpoints = None, []
    for current in [day, *days]:
        bar = bars.get((signal["code"], current))
        if bar is None or bar.source not in _FORMAL_CLOSE_SOURCES:
            return {**result, "reason": "formal_bar_missing"}
        close = _number(bar.close)
        # Own just the leaves consumed by this read, not a reference to mutable ORM.
        # This makes differing source/price projections visible in dataset_sha256;
        # neither a source name nor 15:15 certifies finality or a price basis.
        result["close_observations"].append({
            "code": signal["code"], "trade_date": current.isoformat(),
            "source": bar.source, "close": close, "prev_close": _number(getattr(bar, "prev_close", None)),
        })
        if close is None or close <= 0:
            return {**result, "reason": "invalid_close"}
        if prior is not None:
            prev = _number(bar.prev_close)
            if prev is None or prev <= 0 or abs(prev-prior) > _PRICE_CHAIN_MAX_ABS_GAP:
                return {**result, "status": "right_censored", "reason": "price_chain_discontinuity"}
            endpoints.append(close)
        prior = close
    entry = reference * (1 + policy.slippage_pct/100)
    exit_price = endpoints[-1] * (1 - policy.slippage_pct/100)
    buy = entry * policy.quantity
    sell = exit_price * policy.quantity
    buy_cash = buy + max(policy.minimum_commission, buy*policy.commission_rate)
    sell_cash = sell - max(policy.minimum_commission, sell*policy.commission_rate) - sell*policy.stamp_tax_rate
    if any(_number(value) is None for value in (entry, exit_price, buy, sell, buy_cash, sell_cash)):
        return {**result, "reason": "non_finite_markout_arithmetic"}
    if min(entry, exit_price, buy, sell, buy_cash) <= 0:
        return {**result, "reason": "non_positive_markout_arithmetic"}
    gross_pct = (endpoints[-1]/reference-1)*100
    net_pct = (sell_cash/buy_cash-1)*100
    if _number(gross_pct) is None or _number(net_pct) is None:
        return {**result, "reason": "non_finite_markout_arithmetic"}
    return {**result, "status": "evaluated", "reason": "hypothetical_close_markout",
            "gross_return_pct": round(gross_pct, 6),
            "net_return_pct": round(net_pct, 6),
            "reference_exit_close": endpoints[-1], "buy_cash": round(buy_cash, 6),
            "sell_cash": round(sell_cash, 6), "same_day_high_used": False}


def signal_record(row, account_name):
    payload = _object(row.candidate_json)
    observed = _clock(payload.get("signal_observed_at"))
    created = _clock(row.created_at)
    asof = _clock(row.as_of_at)
    price = _number(row.price)
    identity_ok = bool(created and asof and asof <= created
                       and asof.date() == row.trade_date == created.date()
                       and row.quote_round_id and row.strategy_version and row.source
                       and payload.get("decision_run_id") and price is not None and price > 0
                       and payload.get("account_name") == account_name)
    valid = bool(identity_ok and observed and asof <= observed <= created
                 and observed.date() == row.trade_date)
    # v1只保存真实记录时钟与报价轮水位。保留确认时刻unknown，不冒充v2，
    # 但可作为当时已记录报价的研究基准，不能据此否认已存在的实际成交。
    legacy = bool(identity_ok and payload.get("notification_schema") == "paper_buy_point_v1"
                  and payload.get("signal_observed_at") is None)
    reference_at = observed if valid else created if legacy else None
    context = payload.get("market_context")
    context = context if isinstance(context, dict) else {}
    price_at = _clock(context.get("source_quote_at"))
    price_clock_ok = bool(reference_at and price_at and price_at <= reference_at
                          and price_at.date() == row.trade_date
                          and context.get("schema") == "signal_market_context_v1"
                          and context.get("quote_round_id") == row.quote_round_id
                          and _number(context.get("price")) == price)
    raw_labels = payload.get("signal_labels")
    labels = raw_labels if isinstance(raw_labels, dict) else {}
    labels_at = _clock(labels.get("observed_at"))
    labels_ok = bool(valid and labels.get("schema") == "signal_labels_v1"
                     and labels.get("strategy_version") == row.strategy_version
                     and labels.get("status") == "recorded" and labels_at == observed)
    return {"signal_id": row.id, "signal_key": payload.get("signal_key"),
            "account_id": row.account_id, "account_name": account_name,
            "account_role": "challenger" if account_name.startswith("challenger_") else "champion",
            "route_id": row.source or "unknown", "route_version": "unknown",
            "route_version_evidence": "not_recorded",
            "strategy_version": row.strategy_version or "unknown", "code": row.code,
            "trade_date": row.trade_date.isoformat(), "source": row.source,
            "observed_at": observed.isoformat() if observed else None,
            "recorded_at": created.isoformat() if created else None,
            "quote_round_id": row.quote_round_id, "decision_run_id": payload.get("decision_run_id"),
            "session": ("AM" if reference_at.time() < time(12) else "PM") if reference_at else "unknown",
            "reference_at": reference_at.isoformat() if reference_at else None,
            "reference_clock_basis": "signal_observed_at" if valid else "legacy_recorded_at_not_confirmation_time" if legacy else "unknown",
            "reference_price": price, "queue_order": payload.get("queue_order") is True,
            "reference_price_at": price_at.isoformat() if price_clock_ok else None,
            "reference_price_clock_status": "frozen_stock_quote" if price_clock_ok else "unknown_not_backfilled",
            "evidence_status": "valid" if valid else "legacy_record_reference" if legacy else "unknown",
            "capture_contract": payload.get("research_capture_schema") or "legacy_notification_coupled",
            "labels_status": "frozen" if labels_ok else "unknown_not_backfilled",
            **{key: labels.get(key) or "unknown" if labels_ok else "unknown"
               for key in ("regime", "bull_bear", "sentiment")}}


def _stock_days(rows, *, last=False):
    # Explicit research reference clock, then durable ID; never merge routes/versions.
    ordered = sorted(rows, key=lambda r: (
        r.get("reference_at") or r.get("recorded_at") or "", r.get("signal_id", 0)))
    chosen = {}
    for row in ordered:
        key = tuple(row.get(k, "unknown") for k in (
            "account_id", "strategy_version", "route_id", "route_version", "trade_date", "code"))
        if last or key not in chosen:
            chosen[key] = row
    return list(chosen.values())


def _horizon_stats(rows):
    horizons = {}
    for h in (1, 3):
        values = [r["markouts"][str(h)] for r in rows]
        evaluated = [v["net_return_pct"] for v in values if v["status"] == "evaluated"]
        horizons[str(h)] = {"statuses": dict(Counter(v["status"] for v in values)),
                           "evaluated_count": len(evaluated),
                           "mean_net_markout_pct": _finite_mean(evaluated),
                           "positive_fraction": sum(v > 0 for v in evaluated)/len(evaluated) if evaluated else None,
                           "outcome_evidence_grades": dict(Counter(v["outcome_evidence_grade"] for v in values)),
                           # This reader has no certificate verifier, regardless of ready flags.
                           "certified_evaluated_count": 0}
    return horizons


def _stats(rows):
    first, last = _stock_days(rows), _stock_days(rows, last=True)
    sent = [r for r in rows if r.get("delivery", {}).get("sent_audit_count", 0)]
    return {"confirmation_events": len(rows), "first_account_version_day_codes": len(first),
            "deduplication_key": ["account_id", "strategy_version", "route_id", "route_version", "trade_date", "code"],
            "last_stock_day_horizons": _horizon_stats(last),
            "sent_audit_events": sum(r.get("delivery", {}).get("sent_audit_count", 0) for r in rows),
            "sent_signal_events": len(sent),
            "sent_stock_day_count": len(_stock_days(sent)),
            "sent_first_stock_day_horizons": _horizon_stats(_stock_days(sent)),
            "sent_last_stock_day_horizons": _horizon_stats(_stock_days(sent, last=True)),
            "fill_linked_events": sum(r["actual_fill_quantity"] > 0 for r in rows),
            "first_decision_states": dict(Counter(r["first_decision_state"] for r in rows)),
            # One unique linked order per signal; not the account-wide order funnel.
            "linked_order_status_observations": dict(Counter(
                r["execution_observation"]["order_status"] for r in rows
                if r.get("execution_observation", {}).get("order_status") is not None)),
            "capture_contracts": dict(Counter(r["capture_contract"] for r in rows)), "horizons": _horizon_stats(first)}


def _delivery_evidence(signal, rows, *, as_of):
    evidence = []
    recorded_at = _clock(signal["recorded_at"])
    for row in rows:
        payload = _object(row.candidate_json)
        # signal_log_id alone is insufficient: do not borrow another account/version.
        if (type(payload.get("signal_log_id")) is not int
                or payload["signal_log_id"] != signal["signal_id"]
                or row.account_id != signal["account_id"]
                or row.strategy_version != signal["strategy_version"]
                or row.code != signal["code"] or row.trade_date.isoformat() != signal["trade_date"]
                or row.quote_round_id != signal["quote_round_id"] or row.source != "feishu"
                or recorded_at is None or not recorded_at <= row.created_at <= as_of):
            continue
        clocks = [_clock(payload.get(k)) for k in (
            "dispatch_started_at", "send_started_at", "send_completed_at")]
        valid = bool(payload.get("delivery_clock_schema") == "paper_push_transport_v1"
                     and payload.get("transport_clock_status") == "ok"
                     and all(clocks) and recorded_at <= clocks[0]
                     <= clocks[1] <= clocks[2] <= row.created_at <= as_of)
        evidence.append({
            "audit_id": row.id, "status": row.decision,
            "audit_recorded_at": row.created_at.isoformat(),
            "transport_clock_status": "ok" if valid else "unknown_or_invalid_not_backfilled",
            **{key: value.isoformat() if valid else None for key, value in zip((
                "dispatch_started_at", "send_started_at", "send_completed_at"), clocks)},
            "user_received_at": None,
        })
    return {"audits": evidence, "sent_audit_count": sum(e["status"] == "sent" for e in evidence),
            "channel_success_is_user_receipt": False,
            "post_send_price": None, "post_send_price_status": "not_recorded",
            "signal_price_is_send_price": False}


def _execution_observation(order, *, as_of, already_linked=False):
    """A current mutable order projection, never a reconstructed historical state.

    Fill quantities remain sourced solely from the separately time-scoped fill
    ledger. A filled/canceled order status is not evidence of a fill or no fill.
    """
    result = {"schema": "signal_execution_observation_v1", "order_id": None,
              "status": "no_unique_linked_order", "order_status": None,
              "row_updated_at": None, "validity_evaluation": None,
              "basis": "current_order_row_not_immutable_history"}
    if order is None:
        return result
    result["order_id"] = order.order_id
    if already_linked:
        return {**result, "status": "shared_order_already_linked"}
    created, updated = _clock(order.created_at), _clock(order.updated_at)
    allowed = {"pending", "submitted", "partial", "filled", "canceled",
               "rejected", "risk_blocked", "dry_run"}
    if not (created and updated and created <= updated <= as_of and order.status in allowed):
        return {**result, "status": "unknown_not_reconstructed"}
    result.update(status="current_row_visible_at_as_of", order_status=order.status,
                  row_updated_at=updated.isoformat())
    payload = _object(order.risk_json)
    containers = [payload[k] for k in ("paper_deferred_order", "paper_limit_up_queue")
                  if isinstance(payload.get(k), dict)]
    evaluation = containers[0].get("buy_validity_evaluation") if len(containers) == 1 else None
    if isinstance(evaluation, dict):
        at = _clock(evaluation.get("evaluated_at"))
        if (evaluation.get("schema") == "pending_buy_evaluation_v1"
                and at and created <= at <= updated
                and evaluation.get("status") in {"waiting", "canceled"}
                and isinstance(evaluation.get("quote_round_id"), str)
                and evaluation["quote_round_id"].strip()
                and isinstance(evaluation.get("reason"), str)):
            result["validity_evaluation"] = {
                "status": evaluation["status"], "evaluated_at": at.isoformat(),
                "quote_round_id": evaluation["quote_round_id"],
                "reason": evaluation["reason"][:800],
            }
    # Do not dump arbitrary broker/SQL errors or the rest of risk_json.
    return result


async def build_signal_research_report(db, *, start_date, end_date, as_of, policy=None):
    """调用方用只读事务；完整分母无SQL截断。所有版本分组，不改写历史。"""
    if not _clock(as_of) or start_date > end_date or end_date > as_of.date():
        raise ValueError("invalid report interval")
    policy = policy or MarkoutPolicy()
    accounts = list((await db.scalars(select(PaperAccount).where(
        PaperAccount.account_name.in_(EXPERIMENT_ACCOUNTS)))).all())
    names = {a.id: a.account_name for a in accounts}
    logs = list((await db.scalars(select(PaperAutoTradeLog).where(
        PaperAutoTradeLog.account_id.in_(names), PaperAutoTradeLog.trade_date >= start_date,
        PaperAutoTradeLog.trade_date <= end_date, PaperAutoTradeLog.created_at <= as_of,
        PaperAutoTradeLog.action.in_(("buy_signal", "signal_push", *ACTIONS)),
    ).order_by(PaperAutoTradeLog.id))).all())
    signals = [signal_record(row, names[row.account_id]) for row in logs
               if row.action == "buy_signal" and row.decision == "confirmed"]
    deliveries = defaultdict(list)
    for row in logs:
        if row.action == "signal_push":
            signal_id = _object(row.candidate_json).get("signal_log_id")
            if type(signal_id) is int:
                deliveries[signal_id].append(row)
    events = list((await db.scalars(select(PaperShadowEvent).where(
        PaperShadowEvent.event_key.in_({s["signal_key"] for s in signals if isinstance(s["signal_key"], str)}),
        PaperShadowEvent.trade_date >= start_date, PaperShadowEvent.trade_date <= end_date,
        PaperShadowEvent.observed_at <= as_of, PaperShadowEvent.created_at <= as_of,
        PaperShadowEvent.event_type == "confirmed",
    ))).all())
    event_map = {event.event_key: event for event in events}
    for signal in signals:
        event = event_map.get(signal["signal_key"]) if isinstance(signal["signal_key"], str) else None
        reference_at = _clock(signal["reference_at"])
        if (event and reference_at and event.route_id == signal["route_id"]
                and event.code == signal["code"] and event.trade_date.isoformat() == signal["trade_date"]
                and event.observed_at <= event.created_at <= reference_at and event.route_version):
            signal["route_version"] = event.route_version
            signal["route_version_evidence"] = "exact_frozen_confirmed_event"
        signal["delivery"] = _delivery_evidence(signal, deliveries[signal["signal_id"]], as_of=as_of)
    decisions = defaultdict(list)
    for row in logs:
        if row.action in ACTIONS:
            decisions[(row.account_id, row.strategy_version, row.trade_date.isoformat(),
                       row.code, row.source, row.run_id)].append(row)
    orders = list((await db.scalars(select(TradeOrder).where(
        TradeOrder.broker == "paper", TradeOrder.account_id.in_(EXPERIMENT_ACCOUNTS),
        TradeOrder.side == "buy", TradeOrder.trade_date >= start_date,
        TradeOrder.trade_date <= end_date, TradeOrder.created_at <= as_of,
        TradeOrder.decision_at <= as_of, TradeOrder.as_of_at <= TradeOrder.decision_at,
    ))).all())
    order_groups = defaultdict(list)
    for order in orders:
        order_groups[(order.account_id, order.strategy_version, order.code,
                      order.source, order.decision_round_id)].append(order)
    fills = list((await db.scalars(select(TradeFill).where(
        TradeFill.broker == "paper", TradeFill.side == "buy",
        TradeFill.order_id.in_([o.order_id for o in orders]), TradeFill.filled_at <= as_of,
    ))).all())
    fill_groups = defaultdict(list)
    for fill in fills:
        fill_groups[fill.order_id].append(fill)
    calendar = dict((await db.execute(select(TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day).where(
        TradeCalendarModel.trade_date >= start_date, TradeCalendarModel.trade_date <= as_of.date(),
    ))).all())
    bars = list((await db.scalars(select(StockKline).where(
        StockKline.code.in_({r["code"] for r in signals}), StockKline.trade_date >= start_date,
        StockKline.trade_date <= as_of.date(),
    ))).all())
    bar_map = {(b.code, b.trade_date): b for b in bars}
    used_orders = set()
    for signal in signals:
        key = tuple(signal[k] for k in ("account_id", "strategy_version", "trade_date", "code", "source", "decision_run_id"))
        # _add_auto_log.created_at is the injected quote/fill business clock,
        # whereas buy_signal.created_at is wall_now. Compare only compatible
        # clocks; a delayed signal INSERT must not erase its same-round decision.
        reference = _clock(signal["observed_at"]) if signal["evidence_status"] == "valid" else None
        legacy = signal["evidence_status"] == "legacy_record_reference"
        linked = sorted((r for r in decisions[key]
                         if (reference or legacy) and r.id > signal["signal_id"]
                         and _clock(r.created_at) and r.created_at <= as_of
                         and (reference is None or reference <= r.created_at)),
                        key=(lambda r: (r.created_at, r.id)) if reference else (lambda r: r.id))
        first = linked[0] if linked else None
        signal["first_decision_clock_basis"] = (
            "signal_observed_business_clock" if reference else "legacy_log_sequence_not_wall_clock") if first else None
        signal["first_decision_log_clock"] = first.created_at.isoformat() if first else None
        signal["first_decision_state"] = (first.action + ":" + first.decision) if first else "unknown"
        signal["first_decision_id"] = first.id if first else None
        signal["first_decision_reason"] = str(first.reason or "") if first else None
        # 唯一同账户/版本/来源/个股/报价轮订单；不能靠后来同股成交贴回旧信号。
        candidates = order_groups[tuple(signal[k] for k in ("account_name", "strategy_version", "code", "source", "quote_round_id"))]
        matched = candidates[0] if len(candidates) == 1 and signal["evidence_status"] in {"valid", "legacy_record_reference"} else None
        matched_fills = []
        already_linked = bool(matched and matched.order_id in used_orders)
        if matched and not already_linked:
            matched_fills = [f for f in fill_groups[matched.order_id]
                             if f.code == signal["code"] and f.filled_at >= matched.decision_at
                             and _number(f.quantity) is not None and f.quantity > 0
                             and _number(f.price) is not None and f.price > 0]
            used_orders.add(matched.order_id)
        signal["actual_fill_quantity"] = sum(f.quantity for f in matched_fills)
        fees_known = all(_number(f.commission) is not None and f.commission >= 0
                         and _number(f.tax) is not None and f.tax >= 0 for f in matched_fills)
        fill_cash = _finite_sum([f.price*f.quantity + f.commission + f.tax
                                 for f in matched_fills]) if fees_known else None
        cost_known = fees_known and fill_cash is not None
        signal["actual_fill_cash_including_fees"] = round(fill_cash, 6) if matched_fills and cost_known else None
        signal["actual_fill_cost_status"] = "known" if matched_fills and cost_known else "unknown" if matched_fills else "no_linked_fill"
        signal["actual_fill_cost_reason"] = (
            "no_linked_fill" if not matched_fills else
            "invalid_or_missing_fees" if not fees_known else
            "non_finite_fill_cost_arithmetic" if fill_cash is None else "finite_fee_inclusive_cash"
        )
        signal["actual_fill_ids"] = [f.fill_id for f in matched_fills]
        signal["order_link_status"] = ("already_linked_to_prior_signal" if already_linked else "unique" if matched
                                       else "ambiguous" if len(candidates) > 1
                                       else "invalid_signal_evidence" if candidates else "not_found")
        signal["execution_observation"] = _execution_observation(
            matched, as_of=as_of, already_linked=already_linked)
        signal["markouts"] = {str(h): horizon_markout(signal, horizon=h, bars=bar_map,
                              calendar=calendar, as_of=as_of, policy=policy) for h in (1, 3)}
    strata = defaultdict(list)
    for signal in signals:
        strata[tuple(signal[k] for k in ("account_id", "account_name", "account_role", "route_id", "route_version", "strategy_version", "trade_date", "session", "regime", "bull_bear"))].append(signal)
    return {"schema": SCHEMA, "read_only": True, "as_of": as_of.isoformat(),
            "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
            "outcome_data_observed_at": datetime.now().isoformat(),
            "policy": policy.contract(),
            "dataset_sha256": hashlib.sha256(json.dumps(signals, sort_keys=True, ensure_ascii=False,
                                                        allow_nan=False).encode()).hexdigest(),
            "summary": _stats(signals),
            "accounts": [{"account_name": name, **_stats([r for r in signals if r["account_name"] == name])}
                         for name in EXPERIMENT_ACCOUNTS],
            "strata": [{**dict(zip(("account_id", "account_name", "account_role", "route_id", "route_version", "strategy_version", "trade_date", "session", "regime", "bull_bear"), key)), **_stats(rows)} for key, rows in sorted(strata.items())],
            "signals": signals,
            "cautions": ["全量仅指已记录确认；旧通知开关或采集失败造成的缺证据不回填。",
                         "sent按精确关联飞书审计计数，仅渠道成功，不证明用户接收/阅读/成交；旧sending及缺transport clocks不补齐。",
                         "sent首末次仍按信号参考时钟取样，不是发送后可成交价格；发送后价格未记录。",
                         "fill_linked_events仅严格同轮信号关联成交，不是账户全部成交分母；完整轮次须读取并列portfolio。",
                         "v2首次决策比较信号观测与决策业务时钟，不与信号落库墙钟混用；旧缺观测时钟仅保留日志序列关联。后续委托状态是当前行可见性投影，不是历史终态重建；状态不能替代成交表，撤余量也不等于从未成交。",
                         "固定整手等成本假设的来源白名单日K参考markout不是盘口可成交收益、真实组合胜率或生产改参依据。",
                         "结果日线仅为本次读取的可变投影；来源白名单及15:15门不证明供应商最终性、价基或历史首次可用时间，不能用于模型晋级。",
                         "重复同股跨账户/同交易日样本相关；未到期、缺日历、价格断点和排队未证实单列。",
                         "全市场同形态对照与实际完整平仓轮次仍须并列，不能用信号收益替代。"]}


async def build_parallel_research_report(db, *, start_date, end_date, as_of, policy=None):
    """同一只读快照并列两种真实分母；不把markout命中率与组合胜率作差。"""
    from app.paper.experiment_report import build_experiment_report

    signals = await build_signal_research_report(
        db, start_date=start_date, end_date=end_date, as_of=as_of, policy=policy,
    )
    portfolio = await build_experiment_report(db, now=as_of)
    actual = {row["account_name"]: row for row in portfolio["accounts"]}
    paired = []
    for row in signals["accounts"]:
        current = actual[row["account_name"]]
        matching = [item for item in signals["signals"]
                    if item["account_id"] == current.get("account_id")
                    and item["strategy_version"] == current["strategy_version"]]
        paired.append({
            "account_name": row["account_name"],
            "actual_account_id": current.get("account_id"),
            "actual_strategy_version": current["strategy_version"],
            "all_recorded_signals": row,
            "current_instance_version_signals": _stats(matching),
            "actual_closed_round_trips": current["closed_round_trips"],
            "actual_open_round_trips": current["open_round_trips"],
            "actual_excluded_round_trips": current["excluded_round_trips"],
            "actual_realized_net_pnl": current["net_pnl"],
            "actual_win_rate": current["win_rate"],
            "performance_difference": None,
            "winner": None,
        })
    evidence = {"signal_dataset_sha256": signals["dataset_sha256"],
                "actual_portfolio": portfolio, "paired_accounts": paired}
    return {**signals, "parallel_schema": "signal_portfolio_parallel_v1",
            "actual_portfolio": portfolio, "paired_accounts": paired,
            "parallel_dataset_sha256": hashlib.sha256(json.dumps(
                evidence, sort_keys=True, ensure_ascii=False, allow_nan=False,
            ).encode()).hexdigest(),
            "comparison_contract": {
                "signal_scope": "all_recorded_instances_versions_in_requested_date_range",
                "portfolio_scope": "latest_active_instance_current_protocol_entry_since_configured_start",
                "portfolio_scope_start": portfolio["start_date"],
                "portfolio_scope_as_of": as_of.isoformat(),
                "configuration_observed_at": signals["outcome_data_observed_at"],
                "historical_configuration_reconstructed": False,
                "same_sample_denominator": False,
                "signal_return": "hypothetical_fixed_lot_T1_T3_close_markout",
                "portfolio_return": "actual_complete_cycle_realized_cashflows_net_of_fees",
                "capacity_policy_evaluated": False,
                "full_market_same_shape": {"status": "unavailable", "sample_count": None,
                    "reason": "no_frozen_full_market_strategy_shape_control_ledger"},
                "note": "并列不代表同样本对照；窗口、账户实例、版本和出场周期均分开。当前实例信号子集仍不是已完成轮次的配对收益，不给差值或胜负。",
            }}

