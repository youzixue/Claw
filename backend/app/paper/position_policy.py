"""只读追溯买入成交的原委托身份与冻结退出参数，不改写旧账。"""
import json
import math
from datetime import datetime

from sqlalchemy import select
from types import SimpleNamespace

from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder


def _object(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def buy_order_evidence(db, *, account_id: int, trades, as_of: datetime) -> dict:
    """Link each visible ledger buy to one original order, never by signal text.

    Missing/ambiguous receipts remain individual ledger buys for quota purposes.
    This is read-only attribution, NOT a replacement for locked execution risk or
    account_execution_integrity_evidence. Canceled partial orders still count.
    """
    evidence = {trade.id: {"status": "missing"} for trade in trades}
    if not trades:
        return evidence
    with db.no_autoflush:
        account_name = await db.scalar(select(PaperAccount.account_name).where(
            PaperAccount.id == account_id))
        if not account_name:
            return evidence
        account_ids = list((await db.scalars(select(PaperAccount.id).where(
            PaperAccount.account_name == account_name, PaperAccount.status == "active"))).all())
        if account_ids != [account_id]:
            return {trade.id: {"status": "invalid"} for trade in trades}
        ids = [str(trade.id) for trade in trades]
        candidates = {}
        # Bounded IN lists; no truncation or loss of ambiguous duplicate receipts.
        for start in range(0, len(ids), 400):
            query = select(TradeFill, TradeOrder).outerjoin(
                TradeOrder, TradeOrder.order_id == TradeFill.order_id,
            ).where(TradeFill.broker_trade_id.in_(ids[start:start + 400]))
            if db.bind.dialect.name == "sqlite":
                from app.trading import paper_after_hours_resources as fixed_resources
                fixed = fixed_resources._fixed_receipt_marker()
                fill_fields = ("id", "fill_id", "order_id", "broker", "broker_trade_id", "code", "side", "price",
                    "quantity", "filled_at", "trade_date", "commission", "tax", "realized_pnl",
                    "decision_round_id", "fill_round_id", "raw_json")
                order_fields = ("id", "order_id", "order_type", "account_id", "broker", "code", "side", "price",
                    "quantity", "filled_quantity", "created_at", "decision_at", "trade_date", "strategy_version",
                    "signal_id", "decision_round_id", "risk_json")
                columns = [fixed_resources._recognition_leaf(getattr(model, key), fixed=fixed).label(prefix + key)
                    for model, fields, prefix in ((TradeFill, fill_fields, "f_"), (TradeOrder, order_fields, "o_"))
                    for key in fields]
                query = select(*columns).select_from(TradeFill).outerjoin(TradeOrder,
                    TradeOrder.order_id == TradeFill.order_id).where(TradeFill.broker_trade_id.in_(ids[start:start + 400]))
                projected = (await db.execute(query)).mappings().all()
                rows = [(SimpleNamespace(**{key: row["f_" + key] for key in fill_fields}),
                         SimpleNamespace(**{key: row["o_" + key] for key in order_fields}) if row["o_id"] is not None else None)
                        for row in projected]
            else:
                rows = (await db.execute(query)).all()
            for fill, order in rows:
                # A future-dated receipt for an already visible ledger row is
                # conflicting evidence, not a reason to fall back to legacy logs.
                candidates.setdefault(fill.broker_trade_id, []).append((fill, order))
    verified_by_order, fixed_roots = {}, {}
    for trade in trades:
        pairs = candidates.get(str(trade.id), [])
        if not pairs:
            continue
        evidence[trade.id] = {"status": "invalid"}
        if len(pairs) != 1 or trade.account_id != account_id:
            continue
        fill, order = pairs[0]
        if order is not None and order.order_type == "after_hours_fixed":
            from app.trading import paper_after_hours_resources as fixed_resources
            root_key = (trade.code, trade.trade_time.date())
            try:
                if root_key not in fixed_roots:
                    fixed_roots[root_key] = await fixed_resources._verified_fixed_receipt_bindings(db,
                        account_numeric_id=account_id, code=trade.code,
                        trade_date=trade.trade_time.date(), cutoff=as_of)
                binding = fixed_roots[root_key].get(fill.fill_id)
                if (trade.trade_type != "buy" or order.account_id != account_name
                        or not fixed_resources._binding_matches_book(binding, trade, fill, order, as_of=as_of)):
                    continue
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
                fixed_roots[root_key] = {}
                continue
        elif not _buy_receipt_matches_order(trade, fill, order, account_name, as_of):
            continue
        # Fixed-price manual entries cannot acquire scale-in authority from a
        # mutable, unrelated deferred marker. Preserve ordinary limit semantics.
        deferred = (_object(order.risk_json).get("paper_deferred_order")
                    if order.order_type != "after_hours_fixed" else None)
        candidate = deferred.get("candidate") if isinstance(deferred, dict) else None
        scale_in = candidate.get("scale_in") if isinstance(candidate, dict) else None
        evidence[trade.id] = {
            "status": "verified", "order_id": order.order_id,
            "scale_in": False if order.order_type == "after_hours_fixed" else (scale_in if isinstance(scale_in, bool) else None),
        }
        verified_by_order.setdefault(order.order_id, []).append((trade, order.filled_quantity))
    for group in verified_by_order.values():
        if sum(trade.amount for trade, _ in group) > min(quantity for _, quantity in group):
            for trade, _ in group:
                evidence[trade.id] = {"status": "invalid"}
    return evidence


def _buy_receipt_matches_order(trade, fill, order, account_name, as_of) -> bool:
    """Validate the existing three-table identity; malformed facts cannot free quota."""
    try:
        if (order is None or not order.order_id
                or order.account_id != account_name
                or order.broker != "paper" or fill.broker != "paper"
                or not (trade.trade_type == fill.side == order.side == "buy")
                or not (trade.code == fill.code == order.code)
                or not (trade.trade_time == fill.filled_at <= as_of)
                or not (order.created_at <= trade.trade_time and order.decision_at <= trade.trade_time)
                or not (trade.trade_time.date() == fill.trade_date == order.trade_date)
                or trade.strategy_version != order.strategy_version
                or not trade.decision_round_id or not trade.fill_round_id
                or not (trade.decision_round_id == fill.decision_round_id == order.decision_round_id)
                or trade.fill_round_id != fill.fill_round_id
                or type(trade.amount) is not int or trade.amount < 100 or trade.amount % 100
                or trade.amount != fill.quantity
                or not (trade.amount <= order.filled_quantity <= order.quantity)
                or not math.isfinite(trade.price) or trade.price <= 0
                or trade.price != fill.price):
            return False
        for key in ("commission", "tax", "realized_pnl"):
            value = getattr(trade, key)
            if (value != getattr(fill, key)
                    or (value is not None and not math.isfinite(value))):
                return False
        raw = _object(fill.raw_json)
        if any(raw.get(key) != getattr(trade, key) for key in (
            "id", "code", "trade_type", "price", "amount", "signal_id",
            "strategy_version", "decision_round_id", "fill_round_id",
            "commission", "tax", "realized_pnl",
        )):
            return False
        if datetime.fromisoformat(str(raw.get("trade_time") or "")) != trade.trade_time:
            return False
        # The synchronous legacy predicate cannot recognize a fixed-price receipt:
        # it requires a fresh durable resource-root read in buy_order_evidence.
        if getattr(order, "order_type", "limit") != "limit" or raw.get("after_hours_fixed_execution") is not None:
            return False
        pending = raw.get("pending_execution_timing")
        immediate = raw.get("immediate_execution_evidence")
        if isinstance(pending, dict) and immediate is None:
            request = pending.get("request_id")
            if (pending.get("contract_version") != "pending_paper_fill_timing_v1_20260914"
                    or pending.get("status") != "validated"
                    or pending.get("order_id") != order.order_id
                    or not isinstance(request, str) or not request
                    or fill.fill_id != "fill-" + request
                    or trade.signal_id not in {request, "chlg-" + request,
                                               "auto-tenbagger_midline-" + request}):
                return False
            timing = pending
        elif isinstance(immediate, dict) and pending is None:
            if (immediate.get("contract_version") != "immediate_paper_fill_v2_20260914"
                    or immediate.get("status") != "fillable"
                    or fill.fill_id != "fill-" + order.order_id
                    or trade.signal_id != (order.signal_id or order.order_id)):
                return False
            timing = immediate
        else:
            return False
        # Bind the broker request as well as the mutable receipt foreign key.
        return (timing.get("mandatory") is True
                and timing.get("account_id") == account_name
                and timing.get("code") == trade.code and timing.get("side") == "buy"
                and timing.get("quote_round_id") == trade.fill_round_id
                and timing.get("filled_quantity") == trade.amount
                and timing.get("fill_price") == trade.price)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return False


async def position_exit_policy(db, *, account_name: str, position, defaults: dict, as_of: datetime):
    """返回参数和来源审计；缺失旧证据不编造、不强平，也不停止正常风险退出。"""
    fallback = dict(defaults)
    trace = {
        "basis": "legacy_or_missing_entry_evidence",
        "position_strategy_version": str(position.strategy_version or ""),
        "missing_keys": sorted(defaults),
    }
    first_buy = await db.scalar(select(PaperTradeLog).where(
        PaperTradeLog.account_id == position.account_id,
        PaperTradeLog.code == position.code,
        PaperTradeLog.trade_type == "buy",
        PaperTradeLog.trade_time >= position.buy_time,
        PaperTradeLog.trade_time <= as_of,
    ).order_by(PaperTradeLog.trade_time, PaperTradeLog.id).limit(1))
    if first_buy is None:
        return fallback, trace
    order = await db.scalar(select(TradeOrder).join(
        TradeFill, TradeFill.order_id == TradeOrder.order_id,
    ).where(
        TradeOrder.broker == "paper", TradeFill.broker == "paper",
        TradeOrder.account_id == account_name, TradeOrder.code == position.code,
        TradeOrder.side == "buy", TradeFill.side == "buy",
        TradeFill.broker_trade_id == str(first_buy.id),
        TradeFill.filled_at <= as_of, TradeOrder.created_at <= as_of,
    ).order_by(TradeFill.filled_at, TradeFill.id).limit(1))
    if order is None:
        return fallback, trace
    evidence = _object(order.risk_json).get("experiment_entry")
    if not isinstance(evidence, dict):
        return fallback, trace
    version = str(position.strategy_version or "")
    if not version or any(str(item or "") != version for item in (
        first_buy.strategy_version, order.strategy_version, evidence.get("strategy_version"),
    )):
        trace["basis"] = "entry_version_mismatch"
        return fallback, trace
    try:
        observed = datetime.fromisoformat(str(evidence.get("observed_at") or ""))
        if observed > first_buy.trade_time or observed > as_of:
            raise ValueError("future entry evidence")
    except (ValueError, TypeError):
        trace["basis"] = "entry_clock_invalid"
        return fallback, trace
    frozen = evidence.get("exit_parameters")
    if not isinstance(frozen, dict):
        return fallback, trace
    origin_trace = {}
    from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
    if account_name == PORTFOLIO_ACCOUNT:
        from app.paper.portfolio_provenance import validated_entry_origin, PortfolioIdentityError
        origin = _object(order.risk_json).get("paper_portfolio_origin")
        # Missing/invalid shared evidence is not a reason to substitute A's
        # mechanics. Fail the shared monitor explicitly; keep old accounts intact.
        checked = await validated_entry_origin(db, order, origin, at=as_of, check_current=False)
        defaults = checked["entry_policy"].get("exit_parameters")
        if (not isinstance(defaults, dict) or not defaults
                or defaults != frozen
                or checked["entry_policy"].get("exit_mode") not in {"short", "midline"}):
            raise PortfolioIdentityError("共享持仓原退出参数/模式缺失或不一致")
        fallback = dict(defaults)
        origin_trace = {
            "origin_account": checked["origin_account"],
            "origin_version": checked["origin_version"],
            "portfolio_signal_key": checked["portfolio_signal_key"],
            "exit_mode": checked["entry_policy"]["exit_mode"],
        }
    owned = {}
    for key in defaults:
        value = frozen.get(key)
        if key == "open_noise_end":
            try:
                datetime.strptime(value, "%H:%M")
            except (ValueError, TypeError):
                continue
        elif isinstance(defaults[key], bool):
            if not isinstance(value, bool):
                continue
        elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            continue
        owned[key] = value
    if not owned:
        return fallback, trace
    missing = sorted(set(defaults) - set(owned))
    return {**fallback, **owned}, {
        "basis": "partial_legacy_snapshot" if missing else "frozen_entry_order",
        "order_id": order.order_id,
        "first_buy_trade_id": first_buy.id,
        "position_strategy_version": version,
        "observed_at": observed.isoformat(),
        "missing_keys": missing,
        **origin_trace,
    }
