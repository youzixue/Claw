from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1.tenbagger import (
    _load_stock_fund_context,
    _load_stock_plan_sector_context,
    _resolve_stock_plan_dates,
)
from app.db.session import Base
from app.models.sector import SectorLifecycle
from app.models.governance import TradeCalendarModel
from app.models.stock import FundFlow, SectorPersistence, StockKline, StockSectorMapping
from app.signal.anomaly_scanner import AnomalyScanner
from app.signal.next_day_plan import next_day_plan_engine


@pytest_asyncio.fixture
async def plan_session(tmp_path):
    db_path = tmp_path / "stock_next_day_plan.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_plan_dates_exclude_unfinished_intraday_daily_bar(plan_session):
    target_date = date(2026, 9, 1)
    plan_session.add_all([
        StockKline(
            code="000811",
            trade_date=date(2026, 8, 31),
            open=41,
            close=41.77,
            high=42,
            low=40.5,
            volume=1_000_000,
            source="ths",
        ),
        StockKline(
            code="000811",
            trade_date=target_date,
            open=40.5,
            close=38.8,
            high=40.7,
            low=38.2,
            volume=2_000_000,
            source="spot_fallback",
        ),
    ])
    await plan_session.commit()

    intraday = await _resolve_stock_plan_dates(
        plan_session,
        "000811",
        target_date,
        now=datetime(2026, 9, 1, 11, 20),
    )
    after_close = await _resolve_stock_plan_dates(
        plan_session,
        "000811",
        target_date,
        now=datetime(2026, 9, 1, 15, 5),
    )

    assert intraday["is_intraday_provisional"] is True
    assert intraday["technical_trade_date"] == date(2026, 8, 31)
    assert after_close["is_intraday_provisional"] is False
    assert after_close["technical_trade_date"] == target_date


@pytest.mark.asyncio
async def test_fund_context_uses_exact_five_completed_sessions_and_separate_current_flow(plan_session):
    completed_dates = [
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 26),
        date(2026, 8, 27),
        date(2026, 8, 28),
        date(2026, 8, 31),
    ]
    completed_amounts = [100, 200, 300, 400, 500, 600]
    plan_session.add_all([
        TradeCalendarModel(trade_date=day, is_trade_day=True) for day in completed_dates
    ])
    plan_session.add_all([
        FundFlow(
            code="000811",
            name="冰轮环境",
            trade_date=trade_day,
            main_net_inflow=amount * 1_000_000,
            main_net_inflow_pct=float(amount) / 100,
            source="tencent", source_version="tencent_hsfundtab_v1",
            source_quote_at=datetime.combine(trade_day, datetime.min.time()).replace(hour=15),
            received_at=datetime.combine(trade_day, datetime.min.time()).replace(hour=15, second=1),
            observed_at=datetime.combine(trade_day, datetime.min.time()).replace(hour=15, second=2),
        )
        for trade_day, amount in zip(completed_dates, completed_amounts)
    ] + [
        FundFlow(
            code="000811",
            name="冰轮环境",
            trade_date=date(2026, 9, 1),
            main_net_inflow=-700_000_000,
            main_net_inflow_pct=-12.5,
        )
    ])
    await plan_session.commit()

    context = await _load_stock_fund_context(
        plan_session,
        "000811",
        quote_trade_date=date(2026, 9, 1),
        completed_trade_date=date(2026, 8, 31),
        current_snapshot={"source": "eastmoney_main_fund_unavailable", "items": {}},
        as_of_at=datetime(2026, 9, 1, 10),
    )

    assert context["fund_5d_complete"] is True
    assert context["fund_5d_window"]["basis"] == "dated_latest_not_pit"
    assert context["fund_5d_count"] == 5
    assert context["fund_5d_total"] == 2_000_000_000
    assert context["fund_5d_start_date"] == date(2026, 8, 25)
    assert context["fund_5d_end_date"] == date(2026, 8, 31)
    # Undated source/receipt clocks cannot turn a daily research row into live funds.
    assert context["current_source"] == "unavailable"
    assert context["current_trade_date"] is None
    assert context["current_item"] == {}
    row = await plan_session.get(FundFlow, 7)
    assert row.main_net_inflow == -700_000_000  # retained, never rewritten


@pytest.mark.asyncio
async def test_sector_context_prefers_authoritative_primary_industry_and_marks_stock_weaker(plan_session):
    target_date = date(2026, 9, 1)
    plan_session.add_all([
        StockSectorMapping(
            code="000811",
            sector_code="BK3D",
            sector_name="3D打印",
            sector_type="concept",
            source="pywencai",
        ),
        StockSectorMapping(
            code="000811",
            sector_code="SWREF",
            sector_name="制冷设备",
            sector_type="industry",
            source="sw",
            weight=1.0,
        ),
        SectorPersistence(
            sector_code="BK3D",
            sector_name="3D打印",
            trade_date=target_date,
            change_pct=-1.2,
            fund_flow=-64.0,
            strength_score=99,
            consecutive_days=0,
        ),
        SectorPersistence(
            sector_code="SWREF",
            sector_name="制冷设备",
            trade_date=target_date,
            change_pct=-1.0,
            fund_flow=-3.0,
            strength_score=0,
            consecutive_days=0,
        ),
        SectorLifecycle(
            sector_code="BK3D",
            sector_name="3D打印",
            sector_type="concept",
            trade_date=target_date,
            lifecycle_state="dormant",
            state_score=10,
        ),
        SectorLifecycle(
            sector_code="SWREF",
            sector_name="制冷设备",
            sector_type="industry",
            trade_date=target_date,
            lifecycle_state="dormant",
            state_score=5,
        ),
    ])
    await plan_session.commit()

    context = await _load_stock_plan_sector_context(
        plan_session,
        "000811",
        target_date,
        stock_change_pct=-7.0,
    )

    assert context["sector_resonance_name"] == "制冷设备"
    assert context["sector_lifecycle_state"] == "dormant"
    assert context["sector_lifecycle_label"] == "休眠"
    assert context["sector_resonance"] == "个股弱于板块"


@pytest.mark.asyncio
async def test_technical_indicators_include_real_rsi_macd_and_kdj(plan_session):
    target_date = date(2026, 8, 31)
    rows = []
    for index in range(60):
        trade_day = target_date - timedelta(days=59 - index)
        close = 10.0 + index * 0.1
        rows.append(StockKline(
            code="000811",
            trade_date=trade_day,
            open=close - 0.05,
            close=close,
            high=close + 0.2,
            low=close - 0.2,
            volume=1_000_000 + index,
            source="ths",
        ))
    plan_session.add_all(rows)
    await plan_session.commit()

    indicators = await AnomalyScanner()._calc_technical_indicators(
        "000811",
        plan_session,
        target_date=target_date,
    )

    assert indicators["trade_date"] == "2026-08-31"
    assert indicators["rsi14"] == 100.0
    assert "macd_dif" in indicators
    assert "macd_dea" in indicators
    assert "macd_hist" in indicators
    assert 0 <= indicators["kdj_k"] <= 100
    assert 0 <= indicators["kdj_d"] <= 100


def test_missing_rsi_is_not_labeled_healthy_or_used_as_aggressive_confirmation():
    result = next_day_plan_engine.generate(
        code="000811",
        name="冰轮环境",
        bull_level="A",
        bull_score=82,
        price=10.0,
        change_pct=3.0,
        ma5=9.8,
        ma10=9.6,
        ma20=9.4,
        ma60=9.0,
        boll_upper=11.5,
        boll_mid=9.4,
        boll_lower=8.0,
        high_20d=12.0,
        high_60d=13.0,
        rsi14=None,
        macd_signal="",
        kdj_signal="",
        volume_ratio=2.0,
        turnover=5.0,
        fund_5d_billion=1.5,
    )

    assert "RSI健康" not in result.tech_summary
    assert all(strategy.strategy_type != "aggressive" for strategy in result.strategies)


def test_blocked_sector_lifecycle_cannot_generate_direct_buy():
    result = next_day_plan_engine.generate(
        code="000811",
        name="冰轮环境",
        bull_level="A",
        bull_score=88,
        price=10.0,
        change_pct=2.0,
        ma5=9.9,
        ma10=9.7,
        ma20=9.5,
        ma60=9.0,
        boll_upper=11.5,
        boll_mid=9.5,
        boll_lower=8.0,
        high_20d=12.0,
        high_60d=13.0,
        rsi14=62,
        macd_signal="golden_cross",
        volume_ratio=1.8,
        turnover=6.0,
        fund_5d_billion=2.0,
        sector_lifecycle="制冷设备·休眠",
        sector_lifecycle_state="dormant",
    )

    assert "板块生命周期休眠，不支持直接买入" in result.avoid_reasons
    assert all(
        strategy.strategy_type in {"avoid", "watch"}
        for strategy in result.strategies
    )
