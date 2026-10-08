"""Single-wallet sizing and locked budget checks; no alternative order/fill path."""
import json
import math
from dataclasses import asdict
from datetime import datetime, time
from types import SimpleNamespace

from sqlalchemy import select

from app.config.settings import settings
from app.models.paper import PaperAccount, PaperPosition, PaperPortfolioSignal
from app.models.trading import TradeOrder, TradeFill
from app.paper.account_policy import ROUTE_ACCOUNT_NAMES
from app.paper.portfolio_contract import (
    PORTFOLIO_ACCOUNT, policy_from_settings, allocate_budget, entry_version, worst_case_buy_fee,
)
from app.paper.portfolio_provenance import PortfolioIdentityError, _require


def order_key(signal_key):
    return "portfolio:" + signal_key


async def get_portfolio_account(db):
    """One new 50000 wallet; never reset/replace/merge a historic account."""
    from app.api.v1 import paper
    from app.models.paper import PaperNav
    from app.paper.portfolio_contract import portfolio_active
    from app.trading.paper_authorization import finish_account_write

    async def existing():
        rows = (await db.scalars(select(PaperAccount).where(
            PaperAccount.account_name == PORTFOLIO_ACCOUNT))).all()
        if not rows:
            return None
        _require(len(rows) == 1 and rows[0].status == "active"
                 and rows[0].initial_capital == 50000,
                 "shared wallet cannot be replaced, reset or silently recapitalized")
        return rows[0]

    row = await existing()
    if row is not None:
        return row
    _require(portfolio_active(paper._public_order_clock()),
             "new shared wallet requires explicit valid activation")
    async with paper._ACCOUNT_INIT_LOCK:
        row = await existing()
        if row is not None:
            return row
        row = PaperAccount(account_name=PORTFOLIO_ACCOUNT, strategy="shared_portfolio",
            initial_capital=50000., current_capital=50000., total_assets=50000.,
            total_return=0, max_drawdown=0, sharpe_ratio=0, win_rate=0, status="active")
        db.add(row)
        await db.flush()
        at = paper._public_order_clock()
        if await paper._nav_reporting_day_open(db, at):
            db.add(PaperNav(account_id=row.id, trade_date=at.date(), nav=1, daily_return=None))
        await finish_account_write(db)
        await db.refresh(row)
        return row


def _positive(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise PortfolioIdentityError("wallet numeric evidence missing")
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise PortfolioIdentityError("wallet numeric evidence invalid")
    return float(value)


def origin_symbol_cap(origin):
    """Original route ceiling, not an allocation score or revised threshold."""
    name = origin["origin_account"]
    values = origin["entry_policy"]["parameters"]["account"]
    key = {
        "default": "PAPER_AUTO_STAGED_ENTRY_MAX_POSITION_PCT",
        "promotion": "PAPER_PROMOTION_POSITION_PCT",
        "mainline": "PAPER_MAINLINE_POSITION_PCT",
        "auction": "PAPER_AUCTION_POSITION_PCT",
        "tenbagger": "PAPER_TENBAGGER_POSITION_PCT",
        "reversal": "PAPER_REVERSAL_POSITION_PCT",
        "challenger_a": "PAPER_CHALLENGER_A_POSITION_PCT",
        "challenger_b": "PAPER_CHALLENGER_B_POSITION_PCT",
        "challenger_c": "PAPER_CHALLENGER_C_POSITION_PCT",
        "challenger_d": "PAPER_CHALLENGER_D_POSITION_PCT",
        "challenger_e": "PAPER_CHALLENGER_E_POSITION_PCT",
        "challenger_f2": "PAPER_CHALLENGER_F2_POSITION_PCT",
    }[name]
    cap = _positive(values.get(key))
    _require(cap <= 1, "original symbol cap has invalid units")
    return cap


async def wallet_state(db, *, code, now, excluding_order_key=""):
    """Fresh ledger, unresolved reservations and healthy marked holdings.

    Pending sells never release funds. Only the exact current BUY may be excluded
    when checking its remaining fill; already-filled inventory stays in exposure.
    """
    from app.api.v1 import paper
    accounts = (await db.scalars(select(PaperAccount).where(
        PaperAccount.account_name == PORTFOLIO_ACCOUNT
    ).execution_options(populate_existing=True))).all()
    _require(len(accounts) == 1 and accounts[0].status == "active",
             "shared wallet is missing, replaced, closed or ambiguous")
    account = accounts[0]
    _require(account.initial_capital == 50000, "shared wallet initial cash is not 50000")
    cash = _positive(await paper._cash_from_trades(db, account), zero=True)
    positions = (await db.scalars(select(PaperPosition).where(
        PaperPosition.account_id == account.id, PaperPosition.is_closed.is_(False)
    ).execution_options(populate_existing=True))).all()
    held, position_by_code = {}, {}
    for position in positions:
        _require(position.code not in held and type(position.buy_amount) is int and position.buy_amount > 0,
                 "shared holdings are duplicated or invalid")
        spot = await paper._spot_by_code(db, position.code)
        good, reason = paper._execution_quote_status(spot, now.date(), now=now)
        _require(good, "shared holding cannot be valued from a healthy quote: " + str(reason))
        held[position.code] = _positive(getattr(spot, "price", None)) * position.buy_amount
        position_by_code[position.code] = position
    pending_rows = (await db.scalars(select(TradeOrder).where(
        TradeOrder.broker == "paper", TradeOrder.account_id == PORTFOLIO_ACCOUNT,
        TradeOrder.side == "buy", TradeOrder.status.in_(("pending", "submitted", "partial")),
    ).execution_options(populate_existing=True))).all()
    pending, reserve = {}, 0.
    for order in pending_rows:
        if excluding_order_key and order.idempotency_key == excluding_order_key:
            _require(order.code == code, "current reservation identity does not match symbol")
            continue
        remaining = int(order.quantity or 0) - int(order.filled_quantity or 0)
        _require(remaining > 0 and remaining % 100 == 0, "unresolved BUY quantity invalid")
        notional = _positive(order.price) * remaining
        pending[order.code] = pending.get(order.code, 0.) + notional
        reserve += notional + worst_case_buy_fee(quantity=remaining, price=order.price,
            commission_rate=settings.PAPER_COMMISSION_RATE, min_commission=settings.PAPER_MIN_COMMISSION)
    opened = set((await db.scalars(select(PaperPosition.code).where(
        PaperPosition.account_id == account.id,
        PaperPosition.buy_time >= datetime.combine(now.date(), time.min),
        PaperPosition.buy_time <= now,
    ))).all())
    held_value, pending_value = sum(held.values()), sum(pending.values())
    return {
        "account": account, "positions": position_by_code, "held": held, "pending": pending,
        "total_assets": cash + held_value, "cash": cash, "reserved_cash": reserve,
        "held_exposure": held_value, "pending_exposure": pending_value,
        "symbol_held_exposure": held.get(code, 0.), "symbol_pending_exposure": pending.get(code, 0.),
        "occupied_symbols": len(set(held) | set(pending)),
        # Re-entering an already counted name does not become a sixth unique name.
        "daily_new_symbols": len((opened | (set(pending)-set(held))) - {code}),
        "is_new_symbol": code not in held and code not in pending,
    }


def _effective_stop(state, origin, existing_stop=None):
    stops = [_positive(origin["entry_policy"].get("stop_loss_price"))]
    held = state["positions"].get(origin["candidate"]["code"])
    if held is not None:
        stops.append(_positive(held.stop_loss_price))
    if existing_stop is not None:
        stops.append(_positive(existing_stop))
    return max(stops)


def _frozen_order_stop(order):
    """Read existing service metadata, never replace it with caller's lower stop."""
    try:
        payload = json.loads(order.risk_json or "{}")
    except (ValueError, TypeError):
        raise PortfolioIdentityError("shared pending protection JSON invalid") from None
    _require(isinstance(payload, dict), "shared pending protection JSON is not an object")
    stops = []
    for key in ("paper_deferred_order", "paper_limit_up_queue"):
        if key in payload:
            metadata = payload[key]
            _require(isinstance(metadata, dict), "shared pending protection metadata invalid")
            stops.append(_positive(metadata.get("stop_loss_price")))
    # Metadata-less legacy/test orders still use origin+held protection. Actual
    # command mode/provenance validation is the service owner's separate gate.
    return max(stops) if stops else None


async def shared_buy_layers(db, *, account_id, code, now, origin):
    """Count original filled confirmations, not pf-slice ledger request IDs.

    Current-day actual fill times define the window; all slices of one immutable
    signal are one layer. Latest actual fill controls cooldown conservatively.
    Unknown/mismatched provenance fails closed rather than reducing the count.
    """
    account = await db.scalar(select(PaperAccount.id).where(
        PaperAccount.id == account_id, PaperAccount.account_name == PORTFOLIO_ACCOUNT))
    _require(account is not None, "shared layer wallet identity missing")
    rows = (await db.execute(select(
        TradeFill.filled_at, TradeFill.quantity, TradeFill.code.label("fill_code"),
        TradeOrder.order_id, TradeOrder.code, TradeOrder.source, TradeOrder.signal_id,
        TradeOrder.strategy_version, TradeOrder.idempotency_key, TradeOrder.risk_json,
    ).join(TradeOrder, TradeFill.order_id == TradeOrder.order_id).where(
        TradeOrder.broker == "paper", TradeOrder.account_id == PORTFOLIO_ACCOUNT,
        TradeOrder.side == "buy", TradeOrder.code == code,
        TradeFill.broker == "paper", TradeFill.side == "buy",
        TradeFill.filled_at >= datetime.combine(now.date(), time.min),
        TradeFill.filled_at <= now, TradeOrder.created_at <= now,
    ))).mappings().all()
    layers, latest, evidence = set(), None, {}
    for row in rows:
        _require(type(row["quantity"]) is int and row["quantity"] > 0
                 and row["fill_code"] == code, "shared layer fill quantity/code invalid")
        try:
            frozen = json.loads(row["risk_json"] or "{}").get("paper_portfolio_origin")
        except (ValueError, TypeError, AttributeError):
            raise PortfolioIdentityError("shared layer origin JSON invalid") from None
        _require(isinstance(frozen, dict), "shared layer original signal missing")
        key = frozen.get("portfolio_signal_key")
        _require(isinstance(key, str) and bool(key), "shared layer original signal missing")
        if key not in evidence:
            evidence[key] = await db.scalar(select(PaperPortfolioSignal).where(
                PaperPortfolioSignal.signal_key == key))
        signal = evidence[key]
        _require(signal is not None, "shared layer immutable signal missing")
        _require(all(frozen.get(field) == getattr(signal, field) == origin[field]
                     for field in ("origin_account", "origin_account_id", "origin_version", "portfolio_version")),
                 "shared layer belongs to another origin/version")
        expected = entry_version(signal.origin_version, policy_version=signal.portfolio_version)
        _require(row["strategy_version"] == expected == frozen.get("entry_version")
                 and signal.code == code and signal.source == row["source"]
                 and signal.source_signal_id == row["signal_id"]
                 and row["idempotency_key"] == order_key(key),
                 "shared layer order does not bind immutable confirmation")
        layers.add(key)
        latest = max(latest, row["filled_at"]) if latest else row["filled_at"]
    return len(layers), latest


async def origin_size_limit(db, *, state, origin, price, now, effective_stop_loss_price=None):
    """Reuse original sizing/top-up checks with shared cash, not baseline cash."""
    from app.api.v1 import paper
    name = origin["origin_account"]
    policy = origin["entry_policy"]
    details = policy.get("entry_details") or {}
    candidate = origin["candidate"]
    code = candidate["code"]
    held = state["positions"].get(code)
    if code in state["pending"]:
        return 0, "symbol_has_pending_buy"
    route = next((r for r, a in ROUTE_ACCOUNT_NAMES.items() if a == name), None)
    top_up = held is not None
    stop_loss = _effective_stop(state, origin, effective_stop_loss_price)
    if _positive(price) <= stop_loss:
        return 0, "effective_protection_stop_reached"
    if top_up:
        if held.strategy_version != origin["entry_version"]:
            return 0, "existing_position_has_other_origin_or_version"
        layers, last_buy = await shared_buy_layers(
            db, account_id=state["account"].id, code=code, now=now, origin=origin)
        if route:
            execution = policy["parameters"]["route_execution"]
            prior = last_buy or held.buy_time
            confirmed = datetime.fromisoformat(origin["confirmed_at"])
            cost = _positive(held.buy_price)
            cost_return = (price / cost - 1) * 100
            if not (execution.get("target_top_up_enabled") is True
                    and (confirmed - prior).total_seconds() >= execution["top_up_cooldown_sec"] + 60
                    and layers < execution["top_up_max_daily_layers"]
                    and 0 <= cost_return <= execution["top_up_max_cost_return_pct"]
                    and price > float(held.stop_loss_price or 0)):
                return 0, "original_challenger_top_up_not_confirmed"
        elif name == "default":
            reason = paper._scale_in_reject_reason(
                candidate, position=held, price=price, score=float(details.get("score") or 0),
                total_assets=state["total_assets"], bought_code_today=layers > 0,
                now=now, last_buy_at=last_buy, daily_layers=layers)
            if reason:
                return 0, reason
        else:
            return 0, "origin_does_not_allow_top_up"
    free_cash = max(0., state["cash"]-state["reserved_cash"])
    cap = origin_symbol_cap(origin)
    if route:
        execution = policy["parameters"]["route_execution"]
        opening_end = datetime.strptime(execution["opening_risk_end"], "%H:%M").time()
        original_clock = datetime.fromisoformat(origin["as_of_at"])
        factor = float(execution["opening_position_factor"]) if min(now.time(), original_clock.time()) < opening_end else 1.
        target = state["total_assets"] * cap * min(max(factor, 0.), 1.)
        if top_up:
            target = max(0., target - held.buy_amount * price)
        budget = min(target, free_cash * (1. - float(execution["cash_buffer_pct"])))
        amount = paper._round_lot(budget / price)
    elif name == "default":
        account = state["account"]
        # Policy identity is original A, every monetary leaf is the shared ledger.
        sizing_view = SimpleNamespace(account_name=name, total_assets=state["total_assets"],
            initial_capital=account.initial_capital, current_capital=free_cash,
            current_drawdown=getattr(account, "current_drawdown", account.max_drawdown),
            max_drawdown=account.max_drawdown)
        amount = paper._layered_auto_buy_amount(sizing_view, price=price,
            open_count=len(state["held"]), score=float(details.get("score") or 0),
            position=held)
        caps = []
        if candidate.get("leader_first_move"):
            caps.append(settings.PAPER_AUTO_GREEN_REVERSAL_LEADER_MAX_BUY_AMOUNT)
        if candidate.get("daily_participation"):
            caps.append(settings.PAPER_AUTO_DAILY_PARTICIPATION_MAX_BUY_AMOUNT)
        if details.get("continuous_participation_probe"):
            caps.append(settings.PAPER_AUTO_DRAWDOWN_CONTINUOUS_PARTICIPATION_MAX_AMOUNT)
        if candidate.get("icepoint_reversal"):
            caps.append(settings.PAPER_AUTO_ICEPOINT_REVERSAL_MAX_BUY_AMOUNT)
        if candidate.get("ma5_pullback"):
            caps.append(settings.PAPER_AUTO_MA5_PULLBACK_MAX_BUY_AMOUNT)
        for limit in caps:
            amount = min(amount, paper._round_lot(limit))
    else:
        amount = paper._round_lot(min(state["total_assets"] * cap * .98, free_cash) / price)
    if top_up:
        amount = paper._scale_in_risk_amount(amount, position=held, price=price,
                                            stop_loss=stop_loss, total_assets=state["total_assets"])
    return amount, "" if amount >= 100 else "original_layer_budget_below_one_lot"


async def budget_for_origin(db, *, origin, price, now, excluding_order_key="",
                            enforce_original_size=True, existing_order_stop_loss_price=None):
    state = await wallet_state(db, code=origin["candidate"]["code"], now=now,
                               excluding_order_key=excluding_order_key)
    effective_stop = _effective_stop(state, origin, existing_order_stop_loss_price)
    if excluding_order_key:
        existing = await db.scalar(select(TradeOrder).where(
            TradeOrder.broker == "paper", TradeOrder.account_id == PORTFOLIO_ACCOUNT,
            TradeOrder.idempotency_key == excluding_order_key,
        ).execution_options(populate_existing=True))
        _require(existing is not None and existing.code == origin["candidate"]["code"]
                 and existing.side == "buy", "shared remaining protection order missing")
        frozen_stop = _frozen_order_stop(existing)
        if frozen_stop is not None:
            effective_stop = max(effective_stop, frozen_stop)
    fields = ("total_assets", "cash", "reserved_cash", "held_exposure", "pending_exposure",
              "symbol_held_exposure", "symbol_pending_exposure", "occupied_symbols",
              "daily_new_symbols", "is_new_symbol")
    decision = allocate_budget(policy=policy_from_settings(settings), price=price,
                               route_cap_ratio=origin_symbol_cap(origin),
                               **{key: state[key] for key in fields})
    owned = {**asdict(decision), "wallet": {key: state[key] for key in fields},
             "origin_account": origin["origin_account"],
             "effective_stop_loss_price": effective_stop}
    if _positive(price) <= effective_stop:
        owned.update(allowed=False, amount=0, notional=0., fee=0., reserve=0.,
                     reason_code="effective_protection_stop_reached")
        return owned
    if decision.allowed and enforce_original_size:
        amount, reason = await origin_size_limit(db, state=state, origin=origin, price=price, now=now,
                                               effective_stop_loss_price=effective_stop)
        if amount < decision.amount:
            notional = amount * price
            fee = (worst_case_buy_fee(quantity=amount, price=price,
                commission_rate=settings.PAPER_COMMISSION_RATE,
                min_commission=settings.PAPER_MIN_COMMISSION) if amount else 0.)
            owned.update(amount=amount, allowed=amount >= 100, reason_code=reason or "original_layer_cap",
                         notional=notional, fee=fee, reserve=notional+fee)
    return owned


async def validate_command_budget(db, cmd, origin):
    """Called both before submission and under the existing fill transaction lock."""
    _require(cmd.idempotency_key == order_key(origin["portfolio_signal_key"]),
             "shared BUY idempotency must be derived from its immutable signal")
    # At fill time the original per-order layer was already frozen and accepted;
    # re-applying its cooldown to a partial fill would incorrectly reject it.
    existing = await db.scalar(select(TradeOrder).where(
        TradeOrder.idempotency_key == cmd.idempotency_key,
        TradeOrder.account_id == PORTFOLIO_ACCOUNT,
    ).execution_options(populate_existing=True))
    if existing is not None:
        _require(existing.code == cmd.code and existing.source == cmd.source
                 and existing.signal_id == cmd.signal_id
                 and existing.strategy_version == cmd.strategy_version
                 and existing.side == "buy"
                 and existing.status in ("pending", "submitted", "partial"),
                 "shared pending order identity/status changed")
        _require(cmd.quantity <= int(existing.quantity) - int(existing.filled_quantity or 0),
                 "shared fill exceeds original remaining quantity")
    required_quantity = cmd.quantity
    reservation_price = cmd.price
    if existing is not None:
        # Check the ENTIRE unresolved reservation, not just this depth-limited
        # slice. A partial fill cannot leave the remaining promise over budget.
        required_quantity = int(existing.quantity) - int(existing.filled_quantity or 0)
        reservation_price = _positive(existing.price)
        _require(cmd.price <= reservation_price + .000001, "shared fill exceeds original BUY limit")
    result = await budget_for_origin(
        db, origin=origin, price=reservation_price, now=cmd.decision_at,
        excluding_order_key=cmd.idempotency_key if existing is not None else "",
        enforce_original_size=existing is None)
    _require(result["allowed"], "shared wallet budget blocked: " + str(result["reason_code"]))
    _require(result["amount"] >= required_quantity,
             "shared wallet budget blocked: required_quantity_exceeds_post_fee_capacity "
             f"(required={required_quantity}, allowed={result['amount']}, "
             f"reserved_slice_fee={result.get('fee')})")
    effective_stop = _positive(result["effective_stop_loss_price"])
    _require(_positive(cmd.price) > effective_stop,
             "shared actual fill price reached effective protection stop")
    _require(_positive(getattr(cmd, "stop_loss_price", None)) == effective_stop,
             "shared command protection differs from locked effective stop")
    metadata = [getattr(cmd, field, None) for field in ("deferred_metadata", "queue_metadata")]
    metadata = [item for item in metadata if item is not None]
    _require(bool(metadata), "shared command protection metadata missing")
    for item in metadata:
        _require(isinstance(item, dict), "shared command protection metadata invalid")
        _require(_positive(item.get("stop_loss_price")) == effective_stop,
                 "shared command metadata protection differs from locked effective stop")
    if existing is not None:
        _require(_frozen_order_stop(existing) == effective_stop,
                 "shared pending protection is stale; do not rewrite the reserved stop")
        payload = json.loads(existing.risk_json)
        for key in ("paper_deferred_order", "paper_limit_up_queue"):
            if key in payload:
                _require(_positive(payload[key].get("stop_loss_price")) == effective_stop,
                         "shared pending metadata protection contracts disagree")
    return result
