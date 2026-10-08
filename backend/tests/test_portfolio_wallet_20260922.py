"""Shared actual cash + reservations, never 12 independent initial capitals."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.trading import TradeOrder
from app.paper import portfolio_wallet as wallet, portfolio_provenance as provenance
from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
from app.paper.account_policy import ACCOUNT_NAMES
from test_paper_deferred_exit_provenance import memory_session
from test_portfolio_provenance_20260922 import active, captured, AT


async def cash_wallet(db):
    row = PaperAccount(account_name=PORTFOLIO_ACCOUNT, initial_capital=50000.,
                       current_capital=50000., total_assets=50000., max_drawdown=0, status="active")
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNT_NAMES)
async def test_each_origin_has_real_sizing_and_stricter_symbol_limit(memory_session, active, monkeypatch, account):
    db = memory_session
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    await cash_wallet(db)
    _, cmd, _ = await captured(db, account)
    origin = await provenance.command_origin(db, cmd)
    result = await wallet.budget_for_origin(db, origin=origin, price=10., now=cmd.decision_at)
    assert result["allowed"], result
    assert result["amount"] % 100 == 0 and 100 <= result["amount"] <= 1000
    assert result["wallet"]["total_assets"] == 50000.
    assert result["wallet"]["cash"] == 50000.


async def pending(db, *, code="600002", amount=4000, price=10., key="other"):
    order = TradeOrder(order_id="pending-"+key, broker="paper", account_id=PORTFOLIO_ACCOUNT,
        code=code, side="buy", order_type="limit", price=price, quantity=amount, filled_quantity=0,
        status="submitted", trade_date=AT.date(), idempotency_key=key,
        created_at=AT, decision_at=AT, source="next_day_plan",
        signal_id="other", strategy_id="paper-auto-short")
    db.add(order)
    await db.commit()
    return order


@pytest.mark.asyncio
async def test_pending_buys_exhaust_exposure_without_spending_cash(memory_session, active):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    origin = await provenance.command_origin(db, cmd)
    order = await pending(db)
    result = await wallet.budget_for_origin(db, origin=origin, price=10., now=cmd.decision_at)
    assert not result["allowed"] and result["reason_code"] == "portfolio_exposure_cap"
    assert result["wallet"]["cash"] == 50000.
    assert result["wallet"]["reserved_cash"] == 40200.  # 40 minimum-size slices * 5 fee
    order.status = "canceled"
    await db.commit()
    assert (await wallet.budget_for_origin(db, origin=origin, price=10., now=cmd.decision_at))["allowed"]


@pytest.mark.asyncio
async def test_same_symbol_pending_never_uses_another_layer(memory_session, active):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    origin = await provenance.command_origin(db, cmd)
    await pending(db, code=cmd.code, amount=100)
    result = await wallet.budget_for_origin(db, origin=origin, price=10., now=cmd.decision_at)
    assert not result["allowed"] and result["reason_code"] == "symbol_has_pending_buy"


@pytest.mark.asyncio
async def test_locked_budget_excludes_only_own_remainder(memory_session, active):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db, "promotion")
    origin = await provenance.command_origin(db, cmd)
    order = await pending(db, code=cmd.code, amount=500, key=cmd.idempotency_key)
    order.source, order.signal_id, order.strategy_version = cmd.source, cmd.signal_id, cmd.strategy_version
    order.risk_json = json.dumps({"paper_deferred_order": {
        **cmd.deferred_metadata, "stop_loss_price": cmd.stop_loss_price}})
    await db.commit()
    result = await wallet.validate_command_budget(db, cmd, origin)
    assert result["wallet"]["reserved_cash"] == 0
    competitor = await pending(db, code="600003", amount=3900, key="competitor")
    # Raw room is 1000, but prospective per-slice fees lower the 80% denominator.
    # Even 100 shares cannot fit until another real reservation is released.
    with pytest.raises(provenance.PortfolioIdentityError):
        await wallet.validate_command_budget(db, cmd, origin)
    order.quantity = 100
    await db.commit()
    with pytest.raises(provenance.PortfolioIdentityError):
        await wallet.validate_command_budget(db, cmd, origin)
    # Release 100 shares of a competing reservation, not inject cash/inflate NAV.
    competitor.quantity = 3800
    await db.commit()
    result = await wallet.validate_command_budget(db, cmd, origin)
    assert result["amount"] == 100
    cmd.quantity = 200
    with pytest.raises(provenance.PortfolioIdentityError):
        await wallet.validate_command_budget(db, cmd, origin)


@pytest.mark.asyncio
async def test_invalid_or_duplicated_wallet_never_allocates(memory_session, active):
    db = memory_session
    await cash_wallet(db)
    _, cmd, _ = await captured(db)
    origin = await provenance.command_origin(db, cmd)
    await cash_wallet(db)
    with pytest.raises(provenance.PortfolioIdentityError):
        await wallet.budget_for_origin(db, origin=origin, price=10., now=cmd.decision_at)


@pytest.mark.asyncio
async def test_unhealthy_mark_does_not_invent_portfolio_room(memory_session, active, monkeypatch):
    db = memory_session
    account = await cash_wallet(db)
    _, cmd, _ = await captured(db)
    position = PaperPosition(account_id=account.id, code="600002", buy_price=10,
        buy_amount=100, buy_time=AT-timedelta(days=1), is_closed=False)
    db.add(position)
    await db.commit()
    monkeypatch.setattr(paper, "_spot_by_code", AsyncMock(return_value=None))
    origin = await provenance.command_origin(db, cmd)
    with pytest.raises(provenance.PortfolioIdentityError):
        await wallet.budget_for_origin(db, origin=origin, price=10., now=cmd.decision_at)
