"""Accounting is display-only; fixture writes are strictly in-memory."""
from copy import deepcopy
from datetime import date, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.paper.accounting import accounting_snapshot, load_accounting

DAY = date(2026, 9, 14)


def trade(i, kind="buy", qty=100, price=10, fee=5, pnl=None, day="2026-09-14", **kw):
    return dict(id=i, account_id=1, code="000001", trade_type=kind, amount=qty,
                price=price, commission=fee, tax=0, realized_pnl=pnl,
                trade_time=f"{day} 10:00:{i:02d}", strategy_version="v1",
                forced_probe=False, excluded_from_performance=False, **kw)


def position(**kw):
    return dict(id=1, account_id=1, code="000001", buy_amount=100,
                buy_price=10, current_price=11, **kw)


def snapshot(trades, positions=(), cash=8995, assets=10095):
    account = dict(id=1, account_name="default", initial_capital=10000,
                   current_capital=cash, total_assets=assets)
    return accounting_snapshot(account, trades, positions, as_of=DAY)


def test_open_net_does_not_double_deduct_paid_entry_or_future_exit_fees():
    trades, positions = [trade(1)], [position()]
    original = deepcopy((trades, positions))
    out = snapshot(trades, positions)
    assert out["status"] == "ok"
    assert out["gross_unrealized_pnl"] == 100
    assert out["net_unrealized_pnl"] == 95
    assert out["total_economic_pnl"] == 95
    assert out["remaining_entry_fees"] == 5
    assert out["positions"][1]["net_unrealized_pct"] == pytest.approx(9.4527)
    assert (trades, positions) == original


def test_legacy_fee_bridge_never_overwrites_ledger():
    rows = [trade(1, day="2026-09-04"), trade(2, "sell", price=11, pnl=95, day="2026-09-04")]
    out = snapshot(rows, cash=10090, assets=10090)
    assert out["realized_ledger_pnl"] == 95
    assert out["realized_net_pnl"] == 90
    assert out["legacy_entry_fee_adjustment"] == -5
    assert out["unexplained_realized_adjustment"] == 0
    assert rows[1]["realized_pnl"] == 95
    assert out["today_realized_net_pnl"] == 0


def test_unknown_delta_is_not_called_legacy_fee():
    out = snapshot([trade(1, day="2026-09-04"), trade(2, "sell", price=11, pnl=85, day="2026-09-04")],
                   cash=10090, assets=10090)
    assert out["legacy_entry_fee_adjustment"] == 0
    assert out["unexplained_realized_adjustment"] == 5
    assert out["issues"]


def test_partial_exit_and_scale_in_costs_conserve_cash():
    rows = [trade(1, qty=200), trade(2, "sell", price=11, pnl=92.5),
            trade(3, price=12), trade(4, "sell", price=13, pnl=191.25)]
    p = position()
    p.update(buy_price=11, current_price=12)
    out = snapshot(rows, [p], cash=9180, assets=10380)
    assert out["status"] == "ok"
    assert out["realized_net_pnl"] == 283.75
    assert out["remaining_entry_fees"] == 3.75
    assert out["net_unrealized_pnl"] == 96.25
    assert out["positions"][1]["cycle_total_net_pnl"] == 380
    assert out["total_economic_pnl"] == 380
    assert out["eligible_closed_cycles"] == []


def test_completed_cycle_full_cashflows_and_excluded_samples():
    rows = [trade(1), trade(2, "sell", price=11, pnl=90),
            trade(3), trade(4, "sell", price=12, pnl=190)]
    rows[2]["forced_probe"] = True
    out = snapshot(rows, cash=10280, assets=10280)
    assert out["total_economic_pnl"] == 280  # actual cash cannot disappear
    assert out["excluded_cycle_count"] == 1
    assert out["closed_cycle_performance"] == dict(scope="account_lifetime_all_versions_not_current_protocol", sample_count=1, net_pnl=90, win_rate=100, excluded_cycle_count=1)
    assert len(out["eligible_closed_cycles"]) == 1
    assert out["eligible_closed_cycles"][0]["realized_net_pnl"] == 90


@pytest.mark.parametrize("field,value", [("commission", None), ("price", float("nan")),
                                         ("amount", 0), ("commission", -5)])
def test_missing_invalid_does_not_become_zero(field, value):
    t = trade(1)
    t[field] = value
    out = snapshot([t], [position()])
    assert out["status"] == "incomplete"
    assert out["net_unrealized_pnl"] is None
    assert out["positions"] == {}


def test_account_isolation_inventory_and_future_fail_closed():
    for modify in ("cross", "future", "orphan"):
        t = trade(1)
        if modify == "cross":
            t["account_id"] = 2
        if modify == "future":
            t["trade_time"] = "2026-09-15 10:00:00"
        out = snapshot([t], [] if modify == "orphan" else [position()])
        assert out["status"] == "incomplete"


def test_vwap_rounding_bridge_is_not_mislabeled_fee():
    p = position()
    p.update(buy_price=10.0001)
    out = snapshot([trade(1)], [p])
    assert out["gross_unrealized_pnl"] == 99.99
    assert out["net_unrealized_pnl"] == 94.99
    assert out["stored_cost_rounding_bridge"] == .01
    assert out["total_economic_pnl"] == 95
    assert out["legacy_entry_fee_adjustment"] == 0


def test_cash_mismatch_not_silently_adjusted():
    out = snapshot([trade(1)], [position()], cash=8996, assets=10096)
    assert out["status"] == "unreconciled"
    assert out["cash_reconciliation_residual"] == 1
    assert out["legacy_entry_fee_adjustment"] == 0


@pytest.mark.asyncio
async def test_loader_only_selects_and_never_flushes_dirty_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        account = PaperAccount(id=1, account_name="default", initial_capital=10000,
                               current_capital=8995, total_assets=10095)
        t = trade(1)
        t["trade_time"] = datetime(2026, 9, 14, 10)
        db.add_all([account, PaperTradeLog(**t), PaperPosition(**position(), buy_time=datetime(2026, 9, 14))])
        await db.commit()
        # Pending foreign account proves no_autoflush, and account_id isolation.
        db.add(PaperTradeLog(**{**t, "id": 2, "account_id": 2}))
        statements = []
        event.listen(engine.sync_engine, "before_cursor_execute",
                     lambda c, cu, statement, *args: statements.append(statement))
        out = await load_accounting(db, account, as_of=DAY)
        assert out["status"] == "ok"
        assert out["net_unrealized_pnl"] == 95
        assert len(db.new) == 1
        assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cash_shift,asset_shift", [(0, 0), (2, 0), (0, -3), (2, -3)])
async def test_presentation_routes_add_fields_without_mutating_original_amounts(monkeypatch, cash_shift, asset_shift):
    from app.api.v1 import paper
    from app.models.paper import PaperNav
    from app.models.stock import StockSpot
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        account = PaperAccount(id=1, account_name="default", initial_capital=10000,
                               current_capital=8995, total_assets=10095)
        account.current_capital += cash_shift
        account.total_assets += cash_shift + asset_shift
        t = trade(1)
        t["trade_time"] = datetime(2026, 9, 14, 10)
        p = PaperPosition(**position(), buy_time=datetime(2026, 9, 14, 10),
                          profit_loss=100, profit_pct=10)
        db.add_all([account, PaperTradeLog(**t), p,
                    PaperNav(account_id=1, trade_date=date(2026, 9, 11), nav=1),
                    StockSpot(code="000001", price=11, source_quote_at=datetime(2026, 9, 14, 15))])
        await db.commit()

        async def get_account(*args, **kwargs):
            return account
        async def refresh(*args, **kwargs):
            return account
        monkeypatch.setattr(paper, "_get_or_create_account", get_account)
        monkeypatch.setattr(paper, "_refresh_account", refresh)
        monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 14, 16))
        statements = []
        event.listen(engine.sync_engine, "before_cursor_execute",
                     lambda c, cu, statement, *args: statements.append(statement))
        result = await paper.paper_account(account_name="default", db=db)
        audit = result["account"]["accounting"]
        assert audit["cash_reconciliation_residual"] == cash_shift
        assert audit["asset_reconciliation_residual"] == asset_shift
        assert audit["total_economic_pnl"] == 95
        assert audit["status"] == ("unreconciled" if cash_shift or asset_shift else "ok")
        assert any("现金对账不平衡" in issue for issue in audit["issues"]) == bool(cash_shift)
        assert any("资产对账不平衡" in issue for issue in audit["issues"]) == bool(asset_shift)
        assert audit["today_mtm"]["daily_pnl"] == 95 + cash_shift + asset_shift
        assert result["account"]["accounting"]["today_realized_net_pnl"] == 0
        positions = await paper.paper_positions(account_name="default", db=db)
        assert positions["positions"][0]["profit_loss"] == 100
        assert positions["positions"][0]["accounting"]["net_unrealized_pnl"] == 95
        assert account.current_capital == 8995 + cash_shift
        assert p.profit_loss == 100
        assert not db.dirty and not db.new
        assert all(s.lstrip().upper().startswith("SELECT") for s in statements)
    await engine.dispose()


def test_three_losing_open_positions_frozen_audit_prices():
    # 9/14 observed entry/current marks, not artificial early-high profits.
    cases = [(16.0367, 300, 15.99, -14.01, -19.01),
             (17.015, 200, 16.89, -25, -30),
             (67.96, 100, 67.83, -13, -18)]
    for buy, qty, close, gross, net in cases:
        p = position()
        p.update(buy_price=buy, buy_amount=qty, current_price=close)
        out = snapshot([trade(1, qty=qty, price=buy)], [p],
                       cash=10000-buy*qty-5, assets=10000+(close-buy)*qty-5)
        assert out["gross_unrealized_pnl"] == gross
        assert out["net_unrealized_pnl"] == net
        assert out["today_realized_net_pnl"] == 0
        assert out["positions"][1]["cycle_total_net_pnl"] == net
