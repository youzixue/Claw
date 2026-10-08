"""Verify origin strategy identity separately from the shared paper cash ledger.

Public account labels or client-supplied scores are not authorization. Every entry
must bind a committed immutable outbox row; protective exits bind actual fills.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.data.fund_flow_clock import evidence_clock
from app.models.paper import PaperAccount, PaperPortfolioSignal, PaperPosition, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.paper.account_policy import ACCOUNT_NAMES, ROUTE_ACCOUNT_NAMES
from app.paper.portfolio_contract import (
    PORTFOLIO_ACCOUNT, portfolio_active, portfolio_version, entry_version,
    policy_from_settings, validate_signal_times,
)


class PortfolioIdentityError(ValueError):
    pass


def _object(value):
    try:
        obj = json.loads(value or "{}")
    except (ValueError, TypeError):
        raise PortfolioIdentityError("portfolio evidence is not valid JSON") from None
    if not isinstance(obj, dict):
        raise PortfolioIdentityError("portfolio evidence must be an object")
    return obj


def _require(condition, reason):
    if not condition:
        raise PortfolioIdentityError(reason)


async def validated_entry_origin(db, order, metadata, *, at, check_current=True):
    """Return owned original-policy data; never substitute the origin's cash.

    check_current=False is exclusively for tracing a filled position's protective
    exit. It does not authorize a new buy, add, or pending remainder.
    """
    from app.api.v1 import paper
    _require(order.account_id == PORTFOLIO_ACCOUNT and order.broker == "paper",
             "shared portfolio is paper-only")
    _require(isinstance(metadata, dict), "portfolio metadata missing")
    key = str(metadata.get("portfolio_signal_key") or "")
    _require(bool(key), "immutable portfolio signal identity missing")
    row = await db.scalar(select(PaperPortfolioSignal).where(
        PaperPortfolioSignal.signal_key == key).execution_options(populate_existing=True))
    _require(row is not None, "immutable portfolio signal not found")
    _require(row.origin_account in ACCOUNT_NAMES, "original strategy is not registered")
    expected_version = entry_version(row.origin_version, policy_version=row.portfolio_version)
    _require(str(order.strategy_version or "") == expected_version,
             "portfolio order entry version does not bind original policy")
    _require(order.code == row.code and order.source == row.source
             and order.signal_id == row.source_signal_id,
             "portfolio order changed original code/source/signal")
    route = next((route for route, account in ROUTE_ACCOUNT_NAMES.items()
                  if account == row.origin_account), None)
    _require(order.strategy_id == ("paper-challenger-forward" if route else "paper-auto-short"),
             "portfolio order strategy path does not match origin")
    observed, confirmed, created, asof, now = [
        evidence_clock(v) for v in (row.observed_at, row.confirmed_at, row.created_at, row.as_of_at, at)]
    _require(all(v is not None for v in (observed, confirmed, created, asof, now)),
             "portfolio evidence clocks missing")
    _require(confirmed <= observed <= created <= now and asof <= observed,
             "portfolio evidence not available at decision")
    _require(confirmed.date() == observed.date() == asof.date(),
             "portfolio evidence crosses sessions")
    policy = _object(row.entry_policy_json)
    candidate = _object(row.candidate_json)
    exit_policy = _object(row.exit_policy_json)
    _require(exit_policy == {"exit_parameters": policy.get("exit_parameters"),
                             "exit_mode": policy.get("exit_mode")},
             "frozen exit policy disagrees with original entry contract")
    _require(policy.get("schema") == "portfolio_origin_entry_v1"
             and candidate.get("code") == row.code and candidate.get("_source") == row.source,
             "portfolio candidate/policy identity invalid")
    try:
        import math
        price, stop = float(order.price), float(policy["stop_loss_price"])
        _require(math.isfinite(price) and math.isfinite(stop) and price > stop > 0,
                 "portfolio BUY price is at or below its protective stop")
    except (TypeError, ValueError, KeyError, OverflowError):
        raise PortfolioIdentityError("portfolio BUY price/stop cannot be verified") from None
    _require(bool(row.decision_round_id), "original quote round missing")
    _require((route is None and not row.shadow_event_key)
             or (route == row.source and row.shadow_event_key
                 and candidate.get("event_key") == row.shadow_event_key
                 and candidate.get("route_id") == route),
             "immutable route-event binding invalid")
    if check_current:
        _require(portfolio_active(now) and portfolio_active(confirmed),
                 "portfolio inactive or confirmation predates activation")
        _require(row.portfolio_version == portfolio_version(),
                 "portfolio allocation policy changed")
        _require(row.origin_version == paper._strategy_version(row.origin_account),
                 "original strategy policy changed")
        ids = (await db.scalars(select(PaperAccount.id).where(
            PaperAccount.account_name == row.origin_account, PaperAccount.status == "active"))).all()
        _require(list(ids) == [row.origin_account_id],
                 "original account replaced, missing or ambiguous")
        ttl = policy.get("max_execution_delay_sec")
        _require(type(ttl) in (int, float) and 0 < ttl < 86400,
                 "original confirmation TTL invalid")
        _require(now.date() == confirmed.date() and now <= confirmed + timedelta(seconds=ttl),
                 "portfolio signal expired; cannot refresh with current quote")
    return {
        "schema": "paper_portfolio_origin_v1", "portfolio_signal_key": row.signal_key,
        "origin_account": row.origin_account, "origin_account_id": row.origin_account_id,
        "origin_version": row.origin_version, "portfolio_version": row.portfolio_version,
        "entry_version": expected_version, "confirmed_at": confirmed.isoformat(),
        "observed_at": observed.isoformat(), "as_of_at": asof.isoformat(),
        "decision_round_id": row.decision_round_id, "source": row.source,
        "source_signal_id": row.source_signal_id, "shadow_event_key": row.shadow_event_key,
        "candidate": candidate, "entry_policy": policy,
    }


async def position_origin(db, *, position, as_of):
    """Trace the original first filled BUY; never infer origin from a stock name."""
    buy = await db.scalar(select(PaperTradeLog).where(
        PaperTradeLog.account_id == position.account_id, PaperTradeLog.code == position.code,
        PaperTradeLog.trade_type == "buy", PaperTradeLog.trade_time >= position.buy_time,
        PaperTradeLog.trade_time <= as_of,
    ).order_by(PaperTradeLog.trade_time, PaperTradeLog.id).limit(1))
    _require(buy is not None, "shared position has no original BUY ledger")
    order = await db.scalar(select(TradeOrder).join(TradeFill, TradeFill.order_id == TradeOrder.order_id).where(
        TradeOrder.broker == "paper", TradeOrder.account_id == PORTFOLIO_ACCOUNT,
        TradeOrder.code == position.code, TradeOrder.side == "buy", TradeFill.broker == "paper",
        TradeFill.side == "buy", TradeFill.broker_trade_id == str(buy.id),
        TradeFill.filled_at <= as_of, TradeOrder.created_at <= as_of,
    ).order_by(TradeFill.filled_at, TradeFill.id).limit(1))
    _require(order is not None, "shared position original order/fill binding missing")
    _require(order.strategy_version == buy.strategy_version == position.strategy_version,
             "shared position entry versions disagree")
    payload = _object(order.risk_json)
    origin = payload.get("paper_portfolio_origin")
    _require(isinstance(origin, dict), "shared position frozen origin missing")
    owned = await validated_entry_origin(db, order, origin, at=as_of, check_current=False)
    _require(all(origin.get(key) == owned[key] for key in (
        "origin_account", "origin_account_id", "origin_version", "portfolio_version", "entry_version")),
        "shared position frozen origin changed")
    return owned


async def _validate_buy_command_binding(db, cmd, metadata, origin, *, at):
    """Bind a new reservation or a real existing order's slice, not just labels."""
    import math
    from app.config.settings import settings
    from app.paper.portfolio_wallet import order_key
    _require(cmd.idempotency_key == order_key(origin["portfolio_signal_key"]),
             "shared BUY requires immutable signal-derived idempotency")
    _require(metadata.get("candidate") == origin["candidate"]
             and evidence_clock(metadata.get("confirmed_at")) == evidence_clock(origin["confirmed_at"]),
             "shared BUY changed original confirmation metadata")
    try:
        price, stop, meta_stop = float(cmd.price), float(cmd.stop_loss_price), float(metadata["stop_loss_price"])
        frozen_price = float(origin["entry_policy"]["price"])
        frozen_stop = float(origin["entry_policy"]["stop_loss_price"])
        _require(all(math.isfinite(v) for v in (price, stop, meta_stop, frozen_price, frozen_stop)),
                 "shared BUY non-finite command evidence")
        _require(price > stop >= frozen_stop and abs(stop-meta_stop) < .000001,
                 "shared BUY protective stop is missing, relaxed or inconsistent")
    except (TypeError, ValueError, KeyError, OverflowError):
        raise PortfolioIdentityError("shared BUY command price/stop binding invalid") from None
    existing = await db.scalar(select(TradeOrder).where(
        TradeOrder.account_id == PORTFOLIO_ACCOUNT,
        TradeOrder.idempotency_key == cmd.idempotency_key,
    ).execution_options(populate_existing=True))
    if existing is not None:
        _require(existing.broker == "paper" and existing.side == "buy"
                 and existing.code == cmd.code and existing.source == cmd.source
                 and existing.signal_id == cmd.signal_id
                 and existing.strategy_version == cmd.strategy_version
                 and existing.status in ("pending", "submitted", "partial"),
                 "shared fill does not bind an active original order")
        payload = _object(existing.risk_json)
        frozen = payload.get("paper_deferred_order") or payload.get("paper_limit_up_queue")
        _require(isinstance(frozen, dict) and frozen.get("portfolio_signal_key") == origin["portfolio_signal_key"],
                 "shared fill original execution contract missing")
        _require(abs(stop-float(frozen.get("stop_loss_price") or 0)) < .000001
                 and price <= float(existing.price)+.000001
                 and 0 < cmd.quantity <= existing.quantity-int(existing.filled_quantity or 0),
                 "shared fill changed reserved limit, protection or remaining quantity")
        return
    queued = (origin["entry_policy"].get("entry_details") or {}).get("queue_if_limit_up") is True
    _require(abs(price-frozen_price) < .000001, "shared new BUY changed original decision limit")
    _require(cmd.execute is True and cmd.defer_until_next_round is True
             and cmd.queue_if_limit_up is queued,
             "shared new BUY must retain its original queue mode and next-round fallback")
    for bound in (cmd.deferred_metadata, cmd.queue_metadata if queued else cmd.deferred_metadata):
        _require(isinstance(bound, dict) and bound.get("portfolio_signal_key") == origin["portfolio_signal_key"]
                 and bound.get("stop_loss_price") == metadata.get("stop_loss_price")
                 and bound.get("candidate") == origin["candidate"],
                 "shared new BUY fallback metadata is inconsistent")
    clock_reason = validate_signal_times(policy=policy_from_settings(settings),
        confirmed_at=evidence_clock(origin["confirmed_at"]), observed_at=evidence_clock(origin["observed_at"]),
        as_of_at=evidence_clock(origin["as_of_at"]), now=at)
    _require(not clock_reason, "shared new allocation clock invalid: " + str(clock_reason or ""))


async def command_origin(db, cmd):
    """Resolve a validated strategy policy, retaining account_id as the wallet."""
    if str(cmd.account_id or "") != PORTFOLIO_ACCOUNT:
        return None
    _require(cmd.broker == "paper", "shared portfolio is paper-only")
    metadata = cmd.deferred_metadata or cmd.queue_metadata or {}
    at = evidence_clock(cmd.decision_at)
    _require(at is not None, "portfolio command decision clock missing")
    if cmd.side == "buy":
        origin = await validated_entry_origin(db, cmd, metadata, at=at)
        await _validate_buy_command_binding(db, cmd, metadata, origin, at=at)
        return origin
    _require(cmd.side == "sell", "unknown portfolio side")
    _require(cmd.strategy_id == "paper-auto-short" and cmd.source == "position",
             "shared portfolio only accepts position-bound protective sells")
    position_id = metadata.get("position_id")
    _require(type(position_id) is int and position_id > 0, "shared exit position binding missing")
    position = await db.scalar(select(PaperPosition).join(
        PaperAccount, PaperAccount.id == PaperPosition.account_id).where(
        PaperPosition.id == position_id, PaperPosition.code == cmd.code,
        PaperPosition.is_closed.is_(False), PaperAccount.account_name == PORTFOLIO_ACCOUNT,
        PaperAccount.status == "active",
    ).execution_options(populate_existing=True))
    _require(position is not None, "shared exit position unavailable")
    _require(cmd.strategy_version == position.strategy_version, "shared exit changed entry attribution")
    _require(0 < cmd.quantity <= position.buy_amount, "shared exit exceeds held quantity")
    return await position_origin(db, position=position, as_of=at)
