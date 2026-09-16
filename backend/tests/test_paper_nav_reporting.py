"""Reporting-only contracts; all writes are confined to in-memory test databases."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.db.session import Base
from app.models.paper import PaperAccount, PaperNav, PaperPosition
from app.models.stock import StockSpot
from app.models.governance import TradeCalendarModel
from app.paper.strategy_iteration_challenger import (
    _account_return_breakdown, _common_period_comparison,
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # Reporting calendar must never sync remotely or write the global database.
    async def forbidden(*args, **kwargs):
        raise AssertionError("reporting must not load/sync global calendar")
    # Restore the class descriptor, never leave a bound mock on the singleton.
    monkeypatch.setattr(type(paper.trade_calendar), "_ensure_loaded", forbidden)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


def account(account_id=1, **kwargs):
    values = dict(id=account_id, account_name="default", initial_capital=50000,
                  current_capital=32863.05, total_assets=50975.05,
                  total_return=1.95, max_drawdown=0, sharpe_ratio=0,
                  win_rate=0, status="active")
    values.update(kwargs)
    item = PaperAccount(**values)
    item.trade_stats = {"total_pnl": 1086.05}
    item.fee_drag = 70
    return item


async def seed_previous(db, account_id=1, day=date(2026, 9, 9), nav=1.014382):
    db.add(PaperNav(account_id=account_id, trade_date=day, nav=nav, daily_return=0))
    await db.commit()


@pytest.mark.asyncio
async def test_lifetime_profit_is_not_open_position_pnl_and_daily_is_unrounded(db):
    item = account()
    db.add(item)
    # Open holdings may lose money while closed trades made a lifetime profit.
    db.add(PaperPosition(account_id=1, code="000811", buy_price=182.03,
                        current_price=181.12, buy_amount=100,
                        buy_time=datetime(2026, 9, 9, 10), profit_loss=-91))
    db.add(StockSpot(code="000811", price=181.12, source_quote_at=datetime(2026, 9, 10, 10)))
    await seed_previous(db)
    statements = []
    event.listen(db.bind.sync_engine, "before_cursor_execute",
                 lambda conn, cursor, statement, *args: statements.append(statement))
    result = await _account_return_breakdown(db, item, now=datetime(2026, 9, 10, 11))
    assert result["total_pnl"] == 975.05
    assert result["realized_pnl"] == 1086.05
    assert result["fees_paid"] == 70
    assert result["daily_pnl"] == 255.95
    assert result["daily_return_pct"] == pytest.approx(0.504642)
    assert result["daily_status"] == "ok"
    assert result["daily_trade_date"] == "2026-09-10"
    assert "精度6位" in result["daily_note"]
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements)


@pytest.mark.asyncio
async def test_previous_nav_must_belong_to_same_account_and_exact_previous_day(db):
    await seed_previous(db, day=date(2026, 9, 8))
    await seed_previous(db, account_id=2)
    result = await _account_return_breakdown(db, account(), now=datetime(2026, 9, 10, 11))
    assert result["daily_status"] == "missing_previous_nav"
    assert result["daily_pnl"] is result["daily_return_pct"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("now,status", [
    (datetime(2026, 9, 6, 11), "non_trading_day"),
    (datetime(2026, 10, 1, 11), "non_trading_day"),
    (datetime(2026, 9, 10, 9, 29), "before_open"),
])
async def test_no_fake_daily_or_initial_or_refresh_nav(db, monkeypatch, now, status):
    monkeypatch.setattr(paper, "_paper_now", lambda: now)
    item = await paper._get_or_create_account(db)
    await paper._refresh_account(db, item)
    result = await _account_return_breakdown(db, item, now=now)
    assert result["daily_status"] == status
    assert result["daily_trade_date"] is None
    assert result["daily_pnl"] is result["daily_return_pct"] is None
    assert list((await db.scalars(select(PaperNav))).all()) == []


@pytest.mark.asyncio
async def test_weekend_account_is_not_backfilled_to_friday(db, monkeypatch):
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 6, 11))
    item = await paper._get_or_create_account(db)
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 7, 11))
    await paper._refresh_account(db, item)
    rows = list((await db.scalars(select(PaperNav))).all())
    assert [row.trade_date for row in rows] == [date(2026, 9, 7)]
    result = await _account_return_breakdown(db, item)
    assert result["daily_status"] == "missing_previous_nav"


@pytest.mark.asyncio
@pytest.mark.parametrize("price,quote_at", [
    (10, datetime(2026, 9, 9, 15)),
    (0, datetime(2026, 9, 10, 10)),
    (-1, datetime(2026, 9, 10, 10)),
    (float("inf"), datetime(2026, 9, 10, 10)),
    (10, None),
    (10, datetime(2026, 9, 11, 10)),
])
async def test_stale_or_invalid_holding_quote_hides_daily(db, price, quote_at):
    db.add(PaperPosition(account_id=1, code="000811", buy_price=10,
                        buy_amount=100, buy_time=datetime(2026, 9, 9, 10)))
    db.add(StockSpot(code="000811", price=price, source_quote_at=quote_at))
    await seed_previous(db)
    result = await _account_return_breakdown(db, account(), now=datetime(2026, 9, 10, 11))
    assert result["daily_status"] == "stale_valuation"
    assert result["daily_pnl"] is result["daily_return_pct"] is None


@pytest.mark.asyncio
async def test_cash_only_accounts_can_report_and_missing_realized_is_not_zero(db):
    item = account(total_assets=50000, current_capital=50000)
    del item.trade_stats
    await seed_previous(db, nav=1)
    result = await _account_return_breakdown(db, item, now=datetime(2026, 9, 10, 11))
    assert result["daily_status"] == "ok"
    assert result["daily_pnl"] == 0
    assert result["realized_pnl"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("capital,assets,nav", [
    (0, 50000, 1), (50000, float("nan"), 1), (50000, 50000, 0),
    (50000, 50000, -1), (50000, 50000, float("inf")),
])
async def test_invalid_daily_inputs(db, capital, assets, nav):
    await seed_previous(db, nav=nav)
    result = await _account_return_breakdown(
        db, account(initial_capital=capital, total_assets=assets),
        now=datetime(2026, 9, 10, 11))
    assert result["daily_status"] == "invalid_data"
    assert result["daily_pnl"] is result["daily_return_pct"] is None


@pytest.mark.asyncio
async def test_filters_pollution_and_common_period_without_deleting_history(db):
    days = [date(2026, 9, 4), date(2026, 9, 6), date(2026, 9, 7),
            date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10),
            date(2026, 9, 25), date(2026, 10, 2)]
    db.add_all([PaperNav(account_id=aid, trade_date=day, nav=nav, daily_return=0)
                for aid in [1, 2] for day, nav in zip(days, [1, 9, 1.01, 0, -1, 1.03, 9, 9])])
    await db.commit()
    before = list((await db.scalars(select(PaperNav).order_by(PaperNav.trade_date))).all())
    filtered = [await paper._filter_reporting_nav(
        db, [r for r in before if r.account_id == aid], now=datetime(2026, 9, 10, 11))
        for aid in [1, 2]]
    common = _common_period_comparison(*filtered)
    assert common["session_count"] == 3
    assert common["start_date"] == "2026-09-04"
    assert common["end_date"] == "2026-09-10"
    assert common["champion_return_pct"] == 3
    assert len(list((await db.scalars(select(PaperNav))).all())) == len(before)
    # A persisted nonstandard exchange closure overrides weekday fallback.
    db.add(TradeCalendarModel(trade_date=date(2026, 9, 7), is_trade_day=False))
    await db.commit()
    assert len(await paper._filter_reporting_nav(db, filtered[0], now=datetime(2026, 9, 10, 11))) == 2


@pytest.mark.asyncio
async def test_plain_nav_uses_filter_without_rewriting_weekend_history(db, monkeypatch):
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 6, 11))
    item = await paper._get_or_create_account(db)
    await seed_previous(db, account_id=item.id, day=date(2026, 9, 4), nav=1.04)
    await seed_previous(db, account_id=item.id, day=date(2026, 9, 6), nav=1.02)
    # Direct route call with the isolated fixture, never an HTTP/live API call.
    payload = await paper.paper_nav(account_name="default", db=db)
    assert [n["date"] for n in payload["nav"]] == ["2026-09-04"]
    rows = list((await db.scalars(select(PaperNav).order_by(PaperNav.trade_date))).all())
    assert [(r.trade_date, r.nav) for r in rows] == [
        (date(2026, 9, 4), 1.04), (date(2026, 9, 6), 1.02)]


@pytest.mark.asyncio
async def test_display_rejects_nonfinite_nav_and_today_before_open(db):
    rows = [SimpleNamespace(trade_date=date(2026, 9, 9), nav=nav)
            for nav in [None, float("nan"), float("inf"), -1, 0]]
    rows.append(SimpleNamespace(trade_date=date(2026, 9, 10), nav=1))
    assert await paper._filter_reporting_nav(db, rows, now=datetime(2026, 9, 10, 9)) == []


@pytest.mark.asyncio
async def test_comparison_accounts_share_contract_and_filter_common_period(db, monkeypatch):
    from app.paper.strategy_iteration_challenger import build_strategy_iteration_challenger_comparison

    monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 10, 11))
    champion = await paper._get_or_create_account(db, "default")
    challenger = await paper._get_or_create_account(db, "challenger_a")
    for aid in [champion.id, challenger.id]:
        await seed_previous(db, account_id=aid)
        await seed_previous(db, account_id=aid, day=date(2026, 9, 6), nav=9)
        await seed_previous(db, account_id=aid, day=date(2026, 9, 11), nav=9)
    result = await build_strategy_iteration_challenger_comparison(db, account_name="default")
    pair = next(p for p in result["pairs"] if p["route_id"] == "momentum_first_retest")
    expected = {"total_pnl", "realized_pnl", "fees_paid", "daily_pnl", "daily_return_pct",
                "daily_trade_date", "daily_status", "daily_note"}
    for side in ["champion", "challenger"]:
        assert set(pair[side]["return_breakdown"]) == expected
        assert pair[side]["return_breakdown"]["daily_status"] == "ok"
        assert [n["date"] for n in pair[side]["nav"]] == ["2026-09-09", "2026-09-10"]
    assert pair["champion"]["id"] != pair["challenger"]["id"]
    assert pair["common_period"]["session_count"] == 2
    for coverage in result["strategy_coverage"]:
        assert set(coverage["champion"]["return_breakdown"]) == expected
        if coverage.get("challenger"):
            assert set(coverage["challenger"]["return_breakdown"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, float("nan"), float("inf"), -float("inf")])
async def test_missing_or_nonfinite_lifetime_fields_stay_null(db, unknown):
    item = account(initial_capital=unknown, total_assets=unknown)
    item.trade_stats = {"total_pnl": unknown}
    item.fee_drag = unknown
    result = await _account_return_breakdown(db, item, now=datetime(2026, 9, 10, 11))
    for key in ["total_pnl", "realized_pnl", "fees_paid", "daily_pnl", "daily_return_pct"]:
        assert result[key] is None


@pytest.mark.asyncio
async def test_absent_fees_do_not_use_legacy_zero_default(db):
    item = account()
    del item.fee_drag
    del item.trade_stats
    result = await _account_return_breakdown(db, item, now=datetime(2026, 9, 6, 11))
    assert result["realized_pnl"] is None
    assert result["fees_paid"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, float("inf"), -float("inf")])
async def test_partial_realized_aggregate_is_unknown_without_rebuilding_trades(db, unknown):
    from app.models.paper import PaperTradeLog

    db.add(PaperTradeLog(account_id=1, code="000811", trade_type="sell", price=10,
                        amount=100, trade_time=datetime(2026, 9, 9, 10),
                        realized_pnl=unknown))
    await db.commit()
    item = account()
    result = await _account_return_breakdown(db, item, now=datetime(2026, 9, 6, 11))
    assert result["realized_pnl"] is None
    assert item.trade_stats["total_pnl"] == 1086.05  # No mutation/recalculation.
    other = await _account_return_breakdown(db, account(account_id=2), now=datetime(2026, 9, 6, 11))
    assert other["realized_pnl"] == 1086.05  # Unknown sell is account-scoped.


@pytest.mark.asyncio
async def test_new_daily_return_uses_friday_not_polluted_sunday(db, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 7, 11))
    monkeypatch.setattr(paper, "_cash_from_trades", AsyncMock(return_value=53000))
    item = await paper._get_or_create_account(db)
    await seed_previous(db, account_id=item.id, day=date(2026, 9, 4), nav=1.04)
    await seed_previous(db, account_id=item.id, day=date(2026, 9, 6), nav=1.02)
    await paper._refresh_account(db, item)
    rows = list((await db.scalars(select(PaperNav).order_by(PaperNav.trade_date))).all())
    assert [(r.trade_date, r.nav, r.daily_return) for r in rows] == [
        (date(2026, 9, 4), 1.04, 0),
        (date(2026, 9, 6), 1.02, 0),
        (date(2026, 9, 7), 1.06, 1.92),
    ]
    breakdown = await _account_return_breakdown(db, item)
    assert round(breakdown["daily_return_pct"], 2) == rows[-1].daily_return


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_nav", [None, 0, -1, float("inf")])
async def test_new_daily_return_never_uses_older_nav_when_previous_missing_or_invalid(
    db, monkeypatch, previous_nav,
):
    monkeypatch.setattr(paper, "_paper_now", lambda: datetime(2026, 9, 10, 11))
    item = await paper._get_or_create_account(db)
    baseline = await db.scalar(select(PaperNav).where(PaperNav.account_id == item.id))
    assert baseline.daily_return is None
    await seed_previous(db, account_id=item.id, day=date(2026, 9, 8), nav=1.01)
    if previous_nav is not None:
        await seed_previous(db, account_id=item.id, nav=previous_nav)
    await paper._refresh_account(db, item)
    current = await db.scalar(select(PaperNav).where(
        PaperNav.account_id == item.id, PaperNav.trade_date == date(2026, 9, 10)))
    assert current.nav == 1
    assert current.daily_return is None
