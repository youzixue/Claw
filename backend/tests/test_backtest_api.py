from datetime import date, datetime, timedelta

import httpx
import pandas as pd
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.backtest import BacktestConfig, StrategyBacktester
from app.api.v1 import backtest
from app.db.session import Base, get_db
from app.models import backtest as backtest_models  # noqa: F401
from app.models import factor as factor_models  # noqa: F401
from app.models.signal import SignalPerformance
from app.models.stock import StockDaily, StockKline, StockTag


@pytest_asyncio.fixture
async def backtest_client(tmp_path):
    db_path = tmp_path / "backtest.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(backtest.router, prefix="/backtest")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, SessionLocal

    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_backtest_data(SessionLocal):
    async with SessionLocal() as session:
        session.add_all([
            SignalPerformance(
                signal_id="sig-1",
                stock_code="000001",
                signal_time=datetime(2026, 1, 2, 10, 0),
                signal_price=10.0,
                signal_score=85,
                signal_type="strong",
                return_1d=0.8,
                return_3d=1.5,
                return_5d=2.5,
                max_drawdown=-1.2,
            ),
            SignalPerformance(
                signal_id="sig-2",
                stock_code="000002",
                signal_time=datetime(2026, 1, 2, 11, 0),
                signal_price=20.0,
                signal_score=30,
                signal_type="weak",
                return_1d=-0.5,
                return_3d=-0.8,
                return_5d=-1.0,
                max_drawdown=-2.0,
            ),
            SignalPerformance(
                signal_id="sig-3",
                stock_code="000001",
                signal_time=datetime(2026, 1, 3, 10, 0),
                signal_price=10.8,
                signal_score=82,
                signal_type="strong",
                return_1d=-0.2,
                return_3d=1.0,
                return_5d=1.5,
                max_drawdown=-1.5,
            ),
            SignalPerformance(
                signal_id="sig-4",
                stock_code="000001",
                signal_time=datetime(2026, 1, 4, 10, 0),
                signal_price=10.9,
                signal_score=81,
                signal_type="strong",
                return_1d=0.1,
                return_3d=-0.5,
                return_5d=-0.5,
                max_drawdown=-2.5,
            ),
            SignalPerformance(
                signal_id="sig-5",
                stock_code="000001",
                signal_time=datetime(2026, 1, 5, 10, 0),
                signal_price=11.0,
                signal_score=80,
                signal_type="strong",
            ),
            StockDaily(code="000001", trade_date=date(2026, 1, 2), open=10, high=10.5, low=9.9, close=10, volume=1_000_000, change_pct=0),
            StockDaily(code="000001", trade_date=date(2026, 1, 3), open=10.2, high=11, low=10, close=10.8, volume=1_100_000, change_pct=8),
            StockDaily(code="000001", trade_date=date(2026, 1, 4), open=10.8, high=11.1, low=10.6, close=10.9, volume=1_000_000, change_pct=0.93),
            StockDaily(code="000001", trade_date=date(2026, 1, 5), open=10.8, high=11.2, low=10.5, close=11, volume=900_000, change_pct=0.92),
            StockDaily(code="000002", trade_date=date(2026, 1, 2), open=20, high=20.5, low=19.8, close=20, volume=800_000, change_pct=0),
            StockTag(code="000001", name="平安银行", board_type="main_sz", board_tag="tradeable"),
            StockTag(code="000002", name="万科A", board_type="main_sz", board_tag="tradeable"),
        ])
        await session.commit()


async def _seed_kline_data(SessionLocal):
    async with SessionLocal() as session:
        rows = []
        start = date(2026, 1, 1)
        for idx in range(45):
            trade_date = start + timedelta(days=idx)
            close = 10 + idx * 0.12
            if idx >= 30:
                close += idx * 0.05
            prev_close = 10 + (idx - 1) * 0.12 if idx else close
            rows.append(StockKline(
                code="000003",
                trade_date=trade_date,
                open=close * 0.98,
                high=close * 1.02,
                low=close * 0.97,
                close=close,
                volume=1_000_000 + idx * 50_000,
                amount=close * (1_000_000 + idx * 50_000),
                turnover=3.0,
                change_pct=((close - prev_close) / prev_close * 100) if idx else 0,
                prev_close=prev_close,
            ))
        await session.merge(rows[-1])
        session.add_all(rows[:-1])
        await session.commit()


@pytest.mark.asyncio
async def test_strategy_engine_iterates_all_price_dates_without_signals_and_delays_execution():
    backtester = StrategyBacktester(BacktestConfig(
        initial_capital=100_000,
        commission_rate=0,
        stamp_tax_rate=0,
        slippage_pct=0,
        avoid_limit_up_down=False,
        stop_loss_pct=7,
        take_profit_pct=30,
        max_positions=1,
        min_score_to_buy=70,
    ))
    daily_scores = pd.DataFrame([
        {"code": "000001", "trade_date": date(2026, 1, 2), "score": 90},
    ])
    price_data = {
        "000001": pd.DataFrame([
            {"trade_date": date(2026, 1, 2), "open": 10.0, "close": 10.0, "high": 10.2, "low": 9.9, "volume": 1_000_000, "change_pct": 0},
            {"trade_date": date(2026, 1, 3), "open": 10.0, "close": 10.0, "high": 10.1, "low": 9.9, "volume": 1_000_000, "change_pct": 0},
            {"trade_date": date(2026, 1, 4), "open": 9.0, "close": 8.8, "high": 9.0, "low": 8.7, "volume": 1_000_000, "change_pct": -12},
        ])
    }

    result = await backtester.backtest_strategy(
        daily_scores, price_data, date(2026, 1, 2), date(2026, 1, 4)
    )

    assert [item["trade_date"] for item in result["daily_values"]] == [
        date(2026, 1, 2),
        date(2026, 1, 3),
        date(2026, 1, 4),
    ]
    buy_trades = [trade for trade in result["trades"] if trade["type"] == "buy"]
    assert buy_trades[0]["date"] == "2026-01-03"
    sell_trades = [trade for trade in result["trades"] if trade["type"] == "sell"]
    assert sell_trades[0]["date"] == "2026-01-04"


@pytest.mark.asyncio
async def test_signal_backtest_filters_and_returns_stats(backtest_client):
    client, SessionLocal = backtest_client
    await _seed_backtest_data(SessionLocal)

    response = await client.post("/backtest/signal", json={
        "signal_ids": ["sig-1"],
        "start_date": "2026-01-02",
        "end_date": "2026-01-02",
        "holding_days": [1],
    })

    assert response.status_code == 200
    data = response.json()
    assert data["run_id"]
    assert data["total"] == 1
    assert data["stats"]["return_1d"]["count"] == 1
    assert data["stats"]["return_1d"]["win_rate"] == 100.0

    performance = await client.get(f"/backtest/performance/{data['run_id']}")
    signals = await client.get(f"/backtest/signals/{data['run_id']}")

    assert performance.status_code == 200
    perf_data = performance.json()
    assert perf_data["run_type"] == "signal"
    assert perf_data["total_count"] == 1

    assert signals.status_code == 200
    signal_rows = signals.json()["signals"]
    assert signal_rows[0]["code"] == "000001"
    assert signal_rows[0]["return_1d"] > 0


@pytest.mark.asyncio
async def test_strategy_backtest_executes_with_signal_scores(backtest_client):
    client, SessionLocal = backtest_client
    await _seed_backtest_data(SessionLocal)

    response = await client.post("/backtest/strategy", json={
        "start_date": "2026-01-02",
        "end_date": "2026-01-05",
        "initial_capital": 1_000_000,
        "min_score_to_buy": 70,
    })

    assert response.status_code == 200
    data = response.json()
    assert data["run_id"]
    assert data["status"] == "completed"
    assert data["metrics"]["initial_capital"] == 1_000_000
    assert data["trades"]
    assert data["trades"][0]["type"] == "buy"


@pytest.mark.asyncio
async def test_backtest_status_strategies_and_recent_runs(backtest_client):
    client, SessionLocal = backtest_client
    await _seed_backtest_data(SessionLocal)

    status = await client.get("/backtest/status")
    strategies = await client.get("/backtest/strategies")

    assert status.status_code == 200
    status_data = status.json()
    assert status_data["signal_count"] == 5
    assert status_data["stock_daily_trade_days"] == 4
    assert status_data["price_source"] == "stock_daily"
    assert status_data["price_trade_days"] == 4
    assert status_data["factor_value_count"] == 0
    assert status_data["can_signal_backtest"] is True
    assert status_data["can_strategy_backtest"] is True

    assert strategies.status_code == 200
    strategy_items = strategies.json()["strategies"]
    assert strategy_items[0]["id"] == "signal_score_rank"
    assert strategy_items[0]["enabled"] is True
    assert any(item["id"] == "factor_top_rank" and item["enabled"] is False for item in strategy_items)

    unsupported = await client.post("/backtest/strategy", json={
        "strategy_id": "factor_top_rank",
        "start_date": "2026-01-02",
        "end_date": "2026-01-05",
    })
    assert unsupported.status_code == 400

    strategy = await client.post("/backtest/strategy", json={
        "strategy_id": "signal_score_rank",
        "start_date": "2026-01-02",
        "end_date": "2026-01-05",
        "initial_capital": 1_000_000,
        "min_score_to_buy": 70,
    })
    run_id = strategy.json()["run_id"]

    recent = await client.get("/backtest/runs")
    assert recent.status_code == 200
    runs = recent.json()["runs"]
    assert runs[0]["run_id"] == run_id
    assert runs[0]["strategy_id"] == "signal_score_rank"


@pytest.mark.asyncio
async def test_candidate_preview_shows_latest_selection(backtest_client):
    client, SessionLocal = backtest_client
    await _seed_backtest_data(SessionLocal)

    response = await client.get("/backtest/candidates", params={
        "start_date": "2026-01-02",
        "end_date": "2026-01-05",
        "min_score": 80,
        "limit": 10,
    })

    assert response.status_code == 200
    data = response.json()
    assert data["signal_date"] == "2026-01-05"
    assert data["rules"]["entry"] == "评分 >= 80，形态达标且净收益期望为正"
    assert data["rules"]["ranking"] == "短线形态 + 个股/板块证据 + 成本后收益回撤排序"
    assert data["candidates"][0]["code"] == "000001"
    assert data["candidates"][0]["name"] == "平安银行"
    assert data["candidates"][0]["score"] == 80
    assert data["candidates"][0]["decision"] == "观察"
    assert data["candidates"][0]["reason"].startswith("形态不达标")
    assert data["candidates"][0]["evidence"]["sample_count"] == 3
    assert data["candidates"][0]["evidence"]["source_samples"]["stock"] == 3
    assert data["candidates"][0]["evidence"]["win_rate_1d"] == pytest.approx(33.33)
    assert data["candidates"][0]["evidence"]["avg_return_3d"] == pytest.approx(0.67)
    assert data["candidates"][0]["evidence"]["avg_net_return_3d"] == pytest.approx(0.31)
    assert data["candidates"][0]["evidence"]["expected_net_return"] == pytest.approx(0.07)
    assert data["candidates"][0]["evidence"]["shape"]["passed"] is False
    assert data["candidates"][0]["evidence"]["short_score"] > 0


@pytest.mark.asyncio
async def test_backfill_signals_from_kline_unlocks_signal_backtest(backtest_client):
    client, SessionLocal = backtest_client
    await _seed_kline_data(SessionLocal)

    status_before = (await client.get("/backtest/status")).json()
    assert status_before["signal_count"] == 0
    assert status_before["price_source"] == "stock_kline"
    assert status_before["can_signal_backtest"] is False

    backfill = await client.post("/backtest/backfill-signals", json={
        "start_date": "2026-01-25",
        "end_date": "2026-02-10",
        "max_codes": 10,
        "min_score": 60,
        "max_signals": 20,
    })

    assert backfill.status_code == 200
    data = backfill.json()
    assert data["created"] > 0
    assert data["data_status"]["can_signal_backtest"] is True

    signal_bt = await client.post("/backtest/signal", json={
        "start_date": "2026-01-25",
        "end_date": "2026-02-10",
        "min_score": 60,
        "holding_days": [1],
    })
    assert signal_bt.status_code == 200
    result = signal_bt.json()
    assert result["total"] > 0
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_strategy_result_can_be_queried_by_run_id(backtest_client):
    client, SessionLocal = backtest_client
    await _seed_backtest_data(SessionLocal)

    strategy = await client.post("/backtest/strategy", json={
        "start_date": "2026-01-02",
        "end_date": "2026-01-05",
        "initial_capital": 1_000_000,
        "min_score_to_buy": 70,
        "benchmark_code": "000001",
        "validation_mode": "split",
        "slippage_pct": 0.1,
        "volume_limit_pct": 10,
        "avoid_limit_up_down": True,
    })
    run_id = strategy.json()["run_id"]
    strategy_data = strategy.json()

    performance = await client.get(f"/backtest/performance/{run_id}")
    trades = await client.get(f"/backtest/trades/{run_id}")

    assert performance.status_code == 200
    perf_data = performance.json()
    assert perf_data["run_id"] == run_id
    assert perf_data["run_type"] == "strategy"
    assert perf_data["initial_capital"] == 1_000_000
    assert perf_data["nav_curve"]
    assert perf_data["benchmark"]["code"] == "000001"
    assert perf_data["benchmark_curve"]
    assert perf_data["monthly_returns"]
    assert perf_data["drawdown_curve"]
    assert perf_data["trade_distribution"]["sell_count"] >= 0
    assert perf_data["validation"]["mode"] == "split"
    assert len(perf_data["validation"]["windows"]) == 2
    assert strategy_data["benchmark_curve"]
    assert strategy_data["validation"]["windows"]

    assert trades.status_code == 200
    trade_data = trades.json()
    assert trade_data["run_id"] == run_id
    assert trade_data["trades"][0]["code"] == "000001"
    assert trade_data["trades"][0]["shares"] >= 100
    assert trade_data["trades"][0]["commission"] > 0

    strategy_2 = await client.post("/backtest/strategy", json={
        "start_date": "2026-01-02",
        "end_date": "2026-01-05",
        "initial_capital": 1_000_000,
        "min_score_to_buy": 80,
        "benchmark_code": "000001",
        "validation_mode": "walk_forward",
    })
    run_id_2 = strategy_2.json()["run_id"]
    compare = await client.get("/backtest/compare", params={"run_ids": f"{run_id},{run_id_2}"})

    assert compare.status_code == 200
    compare_runs = compare.json()["runs"]
    assert [row["run_id"] for row in compare_runs] == [run_id, run_id_2]
    assert "excess_return_pct" in compare_runs[0]
