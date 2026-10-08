"""SELECT-only shared-wallet projection; never refresh marks or authorize trades."""
import json
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models.paper import (
    PaperAccount, PaperPosition, PaperNav, PaperTradeLog,
    PaperPortfolioSignal, PaperPortfolioDecision,
)
from app.models.trading import TradeOrder, TradeFill
from app.paper.portfolio_contract import (
    PORTFOLIO_ACCOUNT, portfolio_policy, portfolio_version, portfolio_active, entry_version, worst_case_buy_fee,
)


def _leaf(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _project(row, fields):
    return {key: _leaf(row[key]) for key in fields}


def _number(value, *, nonnegative=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or (nonnegative and value < 0):
        return None
    return float(value)


async def _rows(db, model, fields, *filters, order=(), limit=None):
    query = select(*(getattr(model, key) for key in fields)).where(*filters)
    if order:
        query = query.order_by(*order)
    if limit is not None:
        query = query.limit(limit)
    return (await db.execute(query)).mappings().all()


async def _position_origin(db, position, at):
    """Reporting-only first actual fill binding, not new-entry authorization.

    No imports from API/trading services. Missing/malformed evidence is unknown;
    never infer a route from names, scores or currently enabled strategy versions.
    """
    unknown = {"status": "unknown", "origin_account": None,
               "origin_version": None, "portfolio_signal_key": None}
    buys = await _rows(db, PaperTradeLog, ("id", "strategy_version"), 
        PaperTradeLog.account_id == position["account_id"],
        PaperTradeLog.code == position["code"], PaperTradeLog.trade_type == "buy",
        PaperTradeLog.trade_time >= position["buy_time"], PaperTradeLog.trade_time <= at,
        order=(PaperTradeLog.trade_time, PaperTradeLog.id), limit=1)
    if not buys:
        return unknown
    buy = buys[0]
    order = (await db.execute(select(TradeOrder.strategy_version, TradeOrder.source,
        TradeOrder.signal_id, TradeOrder.risk_json).join(
        TradeFill, TradeFill.order_id == TradeOrder.order_id).where(
        TradeOrder.broker == "paper", TradeOrder.account_id == PORTFOLIO_ACCOUNT,
        TradeOrder.code == position["code"], TradeOrder.side == "buy",
        TradeFill.broker == "paper", TradeFill.side == "buy", TradeFill.code == position["code"],
        TradeFill.broker_trade_id == str(buy["id"]), TradeFill.quantity > 0,
        TradeFill.filled_at <= at, TradeOrder.created_at <= at,
    ).order_by(TradeFill.filled_at, TradeFill.id).limit(1))).mappings().first()
    if not order:
        return unknown
    try:
        frozen = json.loads(order["risk_json"] or "{}").get("paper_portfolio_origin")
        if not isinstance(frozen, dict) or not frozen.get("portfolio_signal_key"):
            return unknown
        signals = await _rows(db, PaperPortfolioSignal,
            ("signal_key", "origin_account", "origin_account_id", "origin_version",
             "portfolio_version", "code", "source", "source_signal_id"),
            PaperPortfolioSignal.signal_key == frozen["portfolio_signal_key"], limit=1)
        if not signals:
            return unknown
        signal = signals[0]
        if any(frozen.get(k) != signal[k] for k in (
                "origin_account", "origin_account_id", "origin_version", "portfolio_version")):
            return unknown
        expected = entry_version(signal["origin_version"], policy_version=signal["portfolio_version"])
        if not (expected == frozen.get("entry_version") == order["strategy_version"] == buy["strategy_version"] == position["strategy_version"]
                and signal["code"] == position["code"] and signal["source"] == order["source"]
                and signal["source_signal_id"] == order["signal_id"]):
            return unknown
        return {"status": "known", "origin_account": signal["origin_account"],
                "origin_version": signal["origin_version"], "portfolio_signal_key": signal["signal_key"]}
    except (ValueError, TypeError, AttributeError, KeyError):
        return unknown


async def portfolio_report(db, limit=100):
    """Owned leaf JSON from stored ledger; no commit/flush/network/mark-to-market.

    All queries disable autoflush, even if caller supplies a dirty session.
    No account is manufactured for the empty state; 50000 is policy budget only.
    """
    limit = max(1, min(500, limit)) if type(limit) is int else 100
    at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    warnings = []
    policy = portfolio_policy()
    try:
        version = portfolio_version()
    except (ValueError, TypeError, OverflowError):
        version = None
        warnings.append("invalid_portfolio_policy")
    result = {
        "account_name": PORTFOLIO_ACCOUNT, "generated_at": at.isoformat(),
        "policy": {k: _leaf(v) for k, v in policy.items()}, "version": version,
        "enabled": policy.get("enabled") is True, "active": portfolio_active(at),
        "status": "not_started", "initial_budget": 50000, "wallet": None,
        "positions": [], "pending_orders": [], "nav": [], "signals": [], "decisions": [],
        "warnings": warnings,
        "basis": "stored ledger only; no revaluation or execution; reservations include worst per-100-share slice fees, not paid fees or fills",
        "limit": limit,
    }
    with db.no_autoflush:
        accounts = await _rows(db, PaperAccount,
            ("id", "account_name", "status", "initial_capital", "current_capital",
             "total_assets", "total_return", "max_drawdown"),
            PaperAccount.account_name == PORTFOLIO_ACCOUNT, limit=2)
        if not accounts:
            return result
        if len(accounts) != 1:
            warnings.append("ambiguous_shared_wallet")
            result["status"] = "degraded"
            return result
        account = accounts[0]
        cash = _number(account["current_capital"])
        assets = _number(account["total_assets"])
        if cash is None or assets is None or account["initial_capital"] != 50000:
            warnings.append("invalid_stored_wallet_values")
        wallet = {
            **_project(account, ("id", "account_name", "status", "initial_capital")),
            "cash": cash, "stored_total_assets": assets,
            "stored_total_return": _number(account["total_return"], nonnegative=False),
            "stored_max_drawdown": _number(account["max_drawdown"], nonnegative=False),
            "reserved_cash": None, "pending_buy_notional": None, "available_cash": None,
        }
        result["wallet"] = wallet
        position_fields = ("id", "account_id", "code", "name", "buy_price", "buy_amount",
                           "buy_time", "current_price", "profit_loss", "profit_pct", "strategy_version")
        positions = await _rows(db, PaperPosition, position_fields,
            PaperPosition.account_id == account["id"], PaperPosition.is_closed.is_(False),
            order=(PaperPosition.buy_time.desc(), PaperPosition.id.desc()))
        for position in positions[:limit]:
            if any(_number(position[k]) is None for k in ("buy_price", "buy_amount", "current_price")):
                warnings.append("invalid_stored_position_values")
            item = _project(position, tuple(k for k in position_fields if k != "account_id"))
            item["origin"] = await _position_origin(db, position, at)
            result["positions"].append(item)
        order_fields = ("order_id", "code", "name", "side", "status", "price", "quantity",
                        "filled_quantity", "source", "signal_id", "strategy_version", "created_at")
        orders = await _rows(db, TradeOrder, order_fields,
            TradeOrder.broker == "paper", TradeOrder.account_id == PORTFOLIO_ACCOUNT,
            TradeOrder.status.in_(("pending", "submitted", "partial")),
            order=(TradeOrder.created_at.desc(), TradeOrder.id.desc()))
        reserved, pending_notional, valid = 0., 0., True
        fee_rate, min_fee = _number(policy.get("commission_rate")), _number(policy.get("min_commission"))
        for order in orders:
            item = _project(order, order_fields)
            quantity, filled = order["quantity"], order["filled_quantity"]
            remaining = quantity-filled if type(quantity) is int and type(filled) is int and 0 <= filled < quantity else None
            item["remaining_quantity"] = remaining
            item["reserved_cash"] = 0 if order["side"] == "sell" else None
            if order["side"] == "buy":
                price = _number(order["price"])
                if remaining is None or price is None or price <= 0 or fee_rate is None or min_fee is None:
                    valid = False
                else:
                    notional = price*remaining
                    try:
                        fee = worst_case_buy_fee(quantity=remaining, price=price,
                            commission_rate=fee_rate, min_commission=min_fee)
                    except ValueError:
                        fee = None
                    if not math.isfinite(notional) or fee is None:
                        valid = False
                        if len(result["pending_orders"]) < limit:
                            result["pending_orders"].append(item)
                        continue
                    item["reserved_cash"] = _leaf(notional+fee)
                    reserved += notional+fee
                    pending_notional += notional
            elif order["side"] != "sell":
                valid = False
            if len(result["pending_orders"]) < limit:
                result["pending_orders"].append(item)
        if valid and math.isfinite(reserved) and math.isfinite(pending_notional):
            wallet["reserved_cash"], wallet["pending_buy_notional"] = reserved, pending_notional
            wallet["available_cash"] = cash-reserved if cash is not None else None
            if cash is not None and reserved > cash:
                warnings.append("reservations_exceed_stored_cash")
        else:
            warnings.append("invalid_pending_reservation")
        nav_fields = ("trade_date", "nav", "daily_return")
        result["nav"] = [_project(row, nav_fields) for row in await _rows(
            db, PaperNav, nav_fields, PaperNav.account_id == account["id"],
            order=(PaperNav.trade_date.desc(), PaperNav.id.desc()), limit=limit)]
        signal_fields = ("id", "signal_key", "portfolio_version", "origin_account", "origin_account_id",
            "origin_version", "source", "code", "name", "source_signal_id", "shadow_event_key",
            "confirmed_at", "as_of_at", "observed_at", "decision_round_id")
        result["signals"] = [_project(row, signal_fields) for row in await _rows(
            db, PaperPortfolioSignal, signal_fields,
            order=(PaperPortfolioSignal.observed_at.desc(), PaperPortfolioSignal.id.desc()), limit=limit)]
        d = PaperPortfolioDecision
        s = PaperPortfolioSignal
        o = TradeOrder
        decisions = (await db.execute(select(
            d.id, d.decision_key, d.signal_id, s.signal_key, s.origin_account, s.origin_version,
            s.code, s.name, d.decision, d.reason_code, d.reason, d.order_id,
            o.status.label("order_status"), d.portfolio_version, d.decision_round_id,
            d.as_of_at, d.observed_at,
        ).outerjoin(s, d.signal_id == s.id).outerjoin(o, (d.order_id == o.order_id)
            & (o.broker == "paper") & (o.account_id == PORTFOLIO_ACCOUNT)
        ).where(d.account_id == account["id"]).order_by(
            d.observed_at.desc(), d.id.desc()).limit(limit))).mappings().all()
        result["decisions"] = [{k: _leaf(v) for k, v in row.items()} for row in decisions]
        result["counts"] = {"open_positions": len(positions), "pending_orders": len(orders)}
        if any(p["origin"]["status"] == "unknown" for p in result["positions"]):
            warnings.append("position_origin_unknown")
        result["warnings"] = list(dict.fromkeys(warnings))
        result["status"] = "degraded" if warnings else ("ready" if result["active"] else "disabled")
    return result
