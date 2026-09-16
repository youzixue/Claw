"""盘中市场当日/完整日线前日分离；当前日表不能回填历史午间。"""
from datetime import date, datetime, timedelta
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func, text

from app.review import service, automation
from app.models.stock import StockKline, StockSpot, MarketSentiment, LimitUpPool, FundFlow, SectorPersistence
from app.models.regime import MarketRegimeSnapshot
from app.models.review import DailyReviewSnapshot, ReviewAutomationRun
from test_daily_review import review_env

DAY, PREVIOUS = date(2026, 9, 7), date(2026, 9, 4)
NOON = datetime(2026, 9, 7, 11, 30)


def clock(monkeypatch, now):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(service, "datetime", FrozenDateTime)


async def seed(db, observed=NOON):
    for code in ("600001", "600002"):
        db.add(StockKline(code=code, trade_date=PREVIOUS, open=10, close=9, high=10, low=9,
                          prev_close=10, change_pct=-10, turnover=1))
        db.add(StockSpot(code=code, name=code, price=10.5, change_pct=5, turnover=2,
                         updated_at=observed, source_quote_at=observed))
    db.add_all([
        LimitUpPool(code="600001", trade_date=PREVIOUS, consecutive_days=1),
        LimitUpPool(code="600002", trade_date=PREVIOUS, consecutive_days=1),
        LimitUpPool(code="600001", trade_date=DAY, consecutive_days=2),
        MarketSentiment(trade_date=PREVIOUS, observed_at=datetime(2026,9,4,15,10),
                        quality_status="ok", main_net_inflow=-99, limit_up_count=99),
        MarketSentiment(trade_date=DAY, observed_at=observed, quality_status="ok",
                        main_net_inflow=12, limit_up_count=1, limit_down_count=0,
                        broken_limit_count=0, board_height=2, seal_rate=100),
        FundFlow(code="600001", trade_date=PREVIOUS, observed_at=datetime(2026,9,4,15,10),
                 main_net_inflow=-99),
        FundFlow(code="600001", trade_date=DAY, observed_at=observed, main_net_inflow=50),
        SectorPersistence(sector_code="old", trade_date=PREVIOUS, strength_score=99),
        SectorPersistence(sector_code="today", trade_date=DAY, strength_score=70),
    ])
    await db.flush()


@pytest.mark.asyncio
async def test_live_noon_uses_current_market_not_previous_close(review_env, monkeypatch):
    maker, _ = review_env
    clock(monkeypatch, NOON)
    async with maker() as db:
        await seed(db)
        # Future and prior-day regimes cannot replace the actual current as-of snapshot.
        for key, observed in [("known", NOON - timedelta(minutes=1)),
                              ("future", NOON + timedelta(minutes=1)),
                              ("prior", datetime(2026, 9, 4, 15))]:
            db.add(MarketRegimeSnapshot(snapshot_key=key, trade_date=observed.date(),
                as_of_at=observed, created_at=observed, primary_regime="recovery",
                quality_status="good", regime_version="test", data_version="test"))
        await db.flush()
        result = await service.build_daily_review_snapshot(db, review_date=DAY,
            phase="intraday", as_of_at=NOON, persist=False)
    assert result["analysis_trade_date"] == result["technical_trade_date"] == "2026-09-04"
    assert result["market_trade_date"] == "2026-09-07"
    d = result["dimensions"]
    assert d["capital"]["source_trade_date"] == "2026-09-07"
    assert d["capital"]["market_main_net_inflow"] == 12
    assert d["capital"]["sampled_stock_main_net_inflow"] == 50
    assert [s["sector_code"] for s in d["capital"]["top_sectors"]] == ["today"]
    assert d["technical"]["median_return"] == 5
    assert d["technical"]["closed_kline_baseline"]["advance_ratio"] == 0
    assert d["limit_up_learning"]["count"] == 1
    assert d["limit_up_learning"]["previous_trade_date"] == "2026-09-04"
    assert d["limit_up_learning"]["previous_board_premium_pct"] == 5
    assert d["limit_up_learning"]["strategy_iteration_sample"]["status"] == "not_applicable_before_close"
    assert result["market_regime"]["snapshot_key"] == "known"
    assert result["quality"]["status"] != "good"  # unversioned daily rows cannot certify historical PIT


@pytest.mark.asyncio
async def test_historical_noon_does_not_read_afternoon_daily_rows_or_yesterday_fallback(review_env, monkeypatch):
    maker, _ = review_env
    afternoon = NOON + timedelta(hours=2)
    clock(monkeypatch, afternoon)
    async with maker() as db:
        await seed(db, observed=afternoon)
        result = await service.build_daily_review_snapshot(db, review_date=DAY,
            phase="intraday", as_of_at=NOON, persist=False)
    d = result["dimensions"]
    assert d["capital"]["market_main_net_inflow"] is None
    assert d["capital"]["fund_flow_record_count"] == 0
    assert d["capital"]["top_sectors"] == []
    assert d["technical"]["source"] == "unavailable"
    assert d["technical"]["median_return"] is None
    assert d["technical"]["advance_ratio"] is None
    assert d["technical"]["limit_up_count"] is None
    assert d["limit_up_learning"]["count"] is None
    assert d["limit_up_learning"]["high_board_promotion_rate"] is None
    assert d["limit_up_learning"]["high_boards"] == []
    assert result["market_regime"]["primary_regime"] == "unknown"
    assert result["quality"]["critical_sources_passed"] is False


@pytest.mark.asyncio
async def test_missing_and_stale_clocks_never_certify_current_data(review_env, monkeypatch):
    maker, _ = review_env
    clock(monkeypatch, NOON)
    async with maker() as db:
        await seed(db, observed=NOON - timedelta(hours=1))
        sentiment = await db.scalar(select(MarketSentiment).where(MarketSentiment.trade_date == DAY))
        sentiment.observed_at = None
        await db.flush()
        d, presence = await service._market_dimensions(db, analysis_trade_date=PREVIOUS,
            review_date=DAY, phase="intraday", as_of_at=NOON, plan_snapshots=[])
    assert d["capital"]["market_main_net_inflow"] is None
    assert d["capital"]["fund_flow_record_count"] == 0
    assert d["technical"]["median_return"] is None
    assert presence["market_sentiment"] is presence["intraday_spot"] is False


@pytest.mark.asyncio
async def test_v5_persists_new_snapshot_without_changing_v4_history(review_env, monkeypatch):
    maker, _ = review_env
    clock(monkeypatch, NOON)
    async with maker() as db:
        await seed(db)
        old = DailyReviewSnapshot(review_key="legacy", review_date=DAY,
            analysis_trade_date=PREVIOUS, phase="intraday", as_of_at=NOON,
            schema_version="daily_review_workbench_v4", data_version="old",
            quality_status="good", quality_score=1, payload_json='{"wrong_day_preserved":true}')
        db.add(old)
        await db.flush()
        result = await service.build_daily_review_snapshot(db, review_date=DAY,
            phase="intraday", as_of_at=NOON, persist=True)
        assert result["schema_version"] == "daily_review_workbench_v5"
        assert await db.scalar(select(func.count(DailyReviewSnapshot.id))) == 2
        assert old.payload_json == '{"wrong_day_preserved":true}'


@pytest.mark.asyncio
async def test_automation_v5_does_not_reuse_v4_success_key(review_env, monkeypatch):
    maker, _ = review_env
    old_key = automation._hash({"job_name": "daily_review_snapshot", "review_date": DAY,
                               "phase": "intraday", "snapshot_context": ""})
    clock(monkeypatch, NOON)
    async with maker() as db:
        await seed(db)
        db.add(ReviewAutomationRun(run_key="old", logical_key=old_key,
            job_name="daily_review_snapshot", review_date=DAY, phase="intraday",
            trigger="test", status="completed", attempt=1, started_at=NOON, completed_at=NOON))
        await db.commit()
        result = await automation.run_review_automation(db, review_date=DAY, phase="intraday",
            retry_attempts=1, retry_delay_seconds=0)
        assert result["status"] != "already_completed"
        assert await db.scalar(select(func.count(ReviewAutomationRun.id))) == 2


@pytest.mark.asyncio
async def test_mixed_legacy_fund_clocks_keep_exact_cutoff_and_do_not_rewrite_history(
    review_env, monkeypatch,
):
    maker, _ = review_env
    cutoff = NOON.replace(minute=35, microsecond=185704)
    clock(monkeypatch, cutoff)
    lower = NOON.replace(minute=20)
    async with maker() as db:
        await seed(db)
        await db.execute(text(
            "UPDATE fund_flow SET observed_at=:clock WHERE code='600001' AND trade_date=:day"
        ), {"clock": "2026-09-07T11:29:30.123456", "day": DAY.isoformat()})
        samples = [
            ("620001", (lower-timedelta(microseconds=1)).isoformat(), 9000),
            ("620002", lower.isoformat(sep=" "), 20),
            ("620003", cutoff.isoformat(), 30),
            ("620004", (cutoff+timedelta(microseconds=1)).isoformat(sep=" "), 8000),
            ("620005", None, 7000),
            ("620006", "2026-09-07T03:29:59+00:00", 40),
        ]
        await db.execute(text(
            "INSERT INTO fund_flow(code,trade_date,observed_at,main_net_inflow) "
            "VALUES(:code,:day,:clock,:flow)"
        ), [{"code": code, "day": DAY.isoformat(), "clock": observed, "flow": value}
            for code, observed, value in samples])
        old = DailyReviewSnapshot(review_key="frozen-fund-zero", review_date=DAY,
            analysis_trade_date=PREVIOUS, phase="intraday", as_of_at=cutoff,
            schema_version="daily_review_workbench_v5", data_version="old",
            quality_status="partial", quality_score=.5,
            payload_json='{"capital":{"fund_flow_record_count":0}}')
        db.add(old)
        await db.flush()
        before = (await db.execute(text(
            "SELECT code,observed_at FROM fund_flow ORDER BY code,trade_date"))).all()
        dimensions, presence = await service._market_dimensions(db,
            analysis_trade_date=PREVIOUS, review_date=DAY, phase="intraday",
            as_of_at=cutoff, plan_snapshots=[])
        capital = dimensions["capital"]
        assert capital["fund_flow_record_count"] == 4
        assert capital["sampled_stock_main_net_inflow"] == 140
        assert {row["code"] for row in capital["top_stock_inflows"]} == {
            "600001", "620002", "620003", "620006"}
        assert presence["intraday_temporal_provenance"] is False
        assert old.payload_json == '{"capital":{"fund_flow_record_count":0}}'
        assert (await db.execute(text(
            "SELECT code,observed_at FROM fund_flow ORDER BY code,trade_date"))).all() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("micros", [0, 123456])
async def test_batch_upsert_datetime_matches_orm_storage_and_preserves_date(
    review_env, micros,
):
    from app.data.scheduler import DataScheduler
    maker, _ = review_env
    observed = NOON.replace(microsecond=micros)
    # Full real table record, not a mocked SQL serializer.
    record = {column.name: None for column in FundFlow.__table__.columns if column.name != "id"}
    record.update(code="600099", trade_date=DAY, observed_at=observed,
                  received_at=observed, source_quote_at=observed,
                  main_net_inflow=0, source="fixture", source_version="test")
    async with maker() as db:
        await DataScheduler._batch_upsert(db, FundFlow, [record], ["code", "trade_date"])
        raw = (await db.execute(text(
            "SELECT trade_date,observed_at FROM fund_flow WHERE code='600099'"))).one()
        assert raw == (DAY.isoformat(), observed.isoformat(sep=" ", timespec="microseconds"))
        row = await db.scalar(select(FundFlow).where(
            FundFlow.observed_at >= observed, FundFlow.observed_at <= observed))
        assert row is not None and row.observed_at == observed
        record["main_net_inflow"] = 10
        await DataScheduler._batch_upsert(db, FundFlow, [record], ["code", "trade_date"])
        assert await db.scalar(select(func.count()).select_from(FundFlow)) == 1
        assert await db.scalar(select(FundFlow.main_net_inflow)) == 10
