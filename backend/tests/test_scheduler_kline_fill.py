from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.data.scheduler as scheduler_module
from app.data.sources.tencent_source import TencentSource
from app.data.scheduler import (
    DataScheduler,
    _calculate_market_sentiment_state,
    _calculate_spot_advance_decline_ratio,
    _promotion_quality_gate_blocks_all_routes,
    _promotion_news_source_health,
    _promotion_startup_catchup_trigger,
    _resolve_spot_snapshot_trade_date,
    _should_run_startup_kline_compensation,
)
from app.db.session import Base
from app.models import stock as stock_models  # noqa: F401
from app.models.stock import BrokenLimitPool, LimitUpPool, StockKline, StockKlineObservation, StockSpot, StockTag


@pytest_asyncio.fixture
async def scheduler_db_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "scheduler_kline.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(scheduler_module, "async_session", SessionLocal)

    try:
        yield SessionLocal
    finally:
        await engine.dispose()


def test_startup_kline_compensation_skips_overnight_stale_spot():
    latest_spot_trade_date = _resolve_spot_snapshot_trade_date(
        datetime(2026, 4, 23, 23, 59, 55),
        today=date(2026, 4, 24),
    )

    assert latest_spot_trade_date == date(2026, 4, 23)
    assert _should_run_startup_kline_compensation(
        session_name="night_session",
        latest_spot_trade_date=latest_spot_trade_date,
        today=date(2026, 4, 24),
    ) is False
    assert _should_run_startup_kline_compensation(
        session_name="morning",
        latest_spot_trade_date=latest_spot_trade_date,
        today=date(2026, 4, 24),
    ) is False


def test_promotion_startup_catchup_only_runs_inside_morning_snapshot_windows():
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 9, 24)) == ""
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 9, 25)) == "promotion_prediction_0925"
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 9, 34)) == "promotion_prediction_0925"
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 9, 35)) == "promotion_prediction_0935"
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 9, 44)) == "promotion_prediction_0935"
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 9, 45)) == ""
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 10, 0)) == "promotion_prediction_1000"
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 10, 30)) == "promotion_prediction_1030"
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 13, 5)) == "promotion_prediction_1305"
    assert _promotion_startup_catchup_trigger(datetime(2026, 8, 20, 13, 20)) == ""


def test_prediction_quality_enforcement_allows_healthy_routes_in_partial_failure():
    partial_gate = {
        "gate_passed": False,
        "route_gates": {
            "second_board_promotion": {"gate_passed": True},
            "mainline_spread_start": {"gate_passed": True},
            "auction_surge_start": {
                "gate_passed": False,
                "blocking_datasets": ["auction_data"],
            },
        },
    }
    all_blocked_gate = {
        "gate_passed": False,
        "route_gates": {
            route: {"gate_passed": False}
            for route in (
                "second_board_promotion",
                "mainline_spread_start",
                "auction_surge_start",
            )
        },
    }

    assert _promotion_quality_gate_blocks_all_routes(partial_gate) is False
    assert _promotion_quality_gate_blocks_all_routes(all_blocked_gate) is True
    assert _promotion_quality_gate_blocks_all_routes({"gate_passed": False}) is True
    assert _promotion_quality_gate_blocks_all_routes({"gate_passed": True}) is False


def test_promotion_news_health_checks_every_critical_source_independently():
    now = datetime(2026, 8, 21, 9, 0)
    stale_health = _promotion_news_source_health(
        {
            "cninfo": now - timedelta(minutes=10),
            "em": now - timedelta(hours=5),
            "ths": now - timedelta(minutes=2),
        },
        now=now,
    )

    assert stale_health["fresh"] is False
    assert stale_health["stale_sources"] == ["em"]
    assert stale_health["age_minutes_by_source"]["em"] == pytest.approx(300.0)

    fresh_health = _promotion_news_source_health(
        {
            "cninfo": now - timedelta(minutes=10),
            "em": now - timedelta(minutes=15),
            "ths": now - timedelta(minutes=2),
        },
        now=now,
    )

    assert fresh_health["fresh"] is True
    assert fresh_health["stale_sources"] == []


@pytest.mark.asyncio
async def test_promotion_news_fallback_does_not_block_prediction(scheduler_db_env, monkeypatch):
    """Slow news I/O must not hold the intraday prediction path open."""
    import asyncio
    from app.news.engine import news_engine

    release_fetch = asyncio.Event()

    async def slow_fetch_all(**kwargs):
        await release_fetch.wait()
        return []

    monkeypatch.setattr(news_engine, "fetch_all", slow_fetch_all)
    scheduler = DataScheduler()
    try:
        result = await asyncio.wait_for(
            scheduler._ensure_fresh_promotion_news(trigger="promotion_prediction_0935"),
            timeout=1.0,
        )
        assert result["status"] == "refresh_scheduled"
        assert result["fresh"] is False
    finally:
        release_fetch.set()
        task = getattr(scheduler, "_promotion_news_refresh_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_market_breadth_uses_same_day_advancers_and_decliners_not_limit_ratio():
    target_date = date(2026, 8, 18)
    ratio, advances, declines, flats, sample_count = _calculate_spot_advance_decline_ratio(
        [
            (10.2, 1000, 2.0, datetime(2026, 8, 18, 15, 1)),
            (9.8, 1000, -2.0, datetime(2026, 8, 18, 15, 1)),
            (9.9, 1000, -1.0, datetime(2026, 8, 18, 15, 1)),
            (10.0, 1000, 0.0, datetime(2026, 8, 18, 15, 1)),
            (11.0, 1000, 5.0, datetime(2026, 8, 17, 15, 1)),
            (0.0, 1000, 3.0, datetime(2026, 8, 18, 15, 1)),
        ],
        target_date=target_date,
    )

    assert ratio == 0.5
    assert (advances, declines, flats, sample_count) == (1, 2, 1, 4)


def test_market_sentiment_score_is_normalized_and_monotonic():
    weak = _calculate_market_sentiment_state(
        limit_up_count=10,
        limit_down_count=35,
        broken_limit_count=25,
        seal_rate=30,
        board_height=1,
        main_net_inflow=-120,
        advance_decline_ratio=0.3,
        breadth_sample_count=5000,
        breadth_coverage=1.0,
        index_avg_change_pct=-2.0,
        index_sample_count=3,
        turnover_total=1.5,
        fund_flow_coverage=1.0,
    )
    strong = _calculate_market_sentiment_state(
        limit_up_count=100,
        limit_down_count=2,
        broken_limit_count=5,
        seal_rate=90,
        board_height=8,
        main_net_inflow=120,
        advance_decline_ratio=2.5,
        breadth_sample_count=5000,
        breadth_coverage=1.0,
        index_avg_change_pct=1.5,
        index_sample_count=3,
        turnover_total=1.5,
        fund_flow_coverage=1.0,
    )

    assert 0 <= weak["score"] <= 100
    assert 0 <= strong["score"] <= 100
    assert strong["score"] > weak["score"]
    assert weak["cycle"] == "freezing"
    assert strong["cycle"] == "climax"


def test_market_sentiment_degraded_coverage_cannot_report_recovery_or_climax():
    state = _calculate_market_sentiment_state(
        limit_up_count=100,
        limit_down_count=1,
        broken_limit_count=1,
        seal_rate=95,
        board_height=9,
        main_net_inflow=150,
        advance_decline_ratio=3.0,
        breadth_sample_count=100,
        breadth_coverage=0.02,
        index_avg_change_pct=1.8,
        index_sample_count=3,
        turnover_total=1.5,
        fund_flow_coverage=0.02,
    )

    assert state["quality_status"] == "degraded"
    assert state["cycle"] == "divergence"
    assert 0 <= state["quality_completeness"] < 1


def test_scheduler_registers_promotion_prediction_and_weekend_news_jobs():
    scheduler = DataScheduler()
    scheduler.setup_jobs()
    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}

    assert {
        "promotion_prediction_1510",
        "promotion_prediction_2000",
        "promotion_prediction_0925",
        "promotion_prediction_0935",
        "promotion_prediction_1000",
        "promotion_prediction_1030",
        "promotion_prediction_1305",
        "promotion_prediction_1400",
        "promotion_prediction_1430",
        "auction_collect_0925",
        "news_weekend_refresh",
        "news_monday_weekend_backfill",
        "ths_kline_recent_repair",
    }.issubset(job_ids)
    tencent_job = scheduler.scheduler.get_job("tencent_spot")
    assert tencent_job is not None
    assert tencent_job.coalesce is True
    assert tencent_job.max_instances == 1
    assert tencent_job.misfire_grace_time == 10
    for job_id in (
        "promotion_prediction_1510",
        "promotion_prediction_2000",
        "promotion_prediction_0925",
        "promotion_prediction_0935",
        "promotion_prediction_1000",
        "promotion_prediction_1030",
        "promotion_prediction_1305",
        "promotion_prediction_1400",
        "promotion_prediction_1430",
    ):
        prediction_job = scheduler.scheduler.get_job(job_id)
        assert prediction_job is not None
        assert prediction_job.coalesce is True
        assert prediction_job.max_instances == 1
        assert prediction_job.misfire_grace_time == 180


@pytest.mark.asyncio
async def test_limit_up_parser_supplies_every_required_batch_upsert_value(scheduler_db_env):
    """新增非空列后，实时涨停池批量写入不得因缺少绑定值整批回滚。"""
    SessionLocal = scheduler_db_env
    target_date = date(2026, 8, 31)
    records = DataScheduler._parse_limit_up_df(
        pd.DataFrame(
            [
                {
                    "代码": "000001",
                    "名称": "平安银行",
                    # 东财真实涨停池只给“最新价”，没有“涨停价”列。
                    "最新价": 12.34,
                    "封板资金": 123_000_000,
                    "炸板次数": 0,
                    "连板数": 1,
                    "换手率": 4.2,
                    "首次封板时间": "09:35:00",
                    "所属行业": "银行",
                }
            ]
        ),
        target_date,
        source="eastmoney",
    )

    assert records[0]["quarantined"] is False
    async with SessionLocal() as session:
        await DataScheduler._batch_upsert(
            session,
            LimitUpPool,
            records,
            unique_cols=["code", "trade_date"],
        )
        await session.commit()

    async with SessionLocal() as session:
        row = await session.scalar(
            select(LimitUpPool).where(
                LimitUpPool.code == "000001",
                LimitUpPool.trade_date == target_date,
            )
        )

    assert row is not None
    assert row.name == "平安银行"
    assert row.limit_up_price == pytest.approx(12.34)
    assert row.quarantined is False


def test_fund_flow_parser_records_source_mapping_version_and_observation_time():
    records = DataScheduler._parse_individual_fund_flow_df(
        pd.DataFrame(
            [
                {
                    "代码": "000001",
                    "名称": "平安银行",
                    "主力净流入-净额": 100_000_000,
                    "主力净流入-净占比": 5.2,
                    "source_quote_at": datetime(2026, 9, 2, 10),
                    "received_at": datetime(2026, 9, 2, 10, 0, 1),
                }
            ]
        ),
        date(2026, 9, 2),
        observed_at=datetime(2026, 9, 2, 10, 0, 2),
    )

    assert len(records) == 1
    assert records[0]["source"] == "eastmoney"
    assert records[0]["source_version"] == "individual_fund_flow_v3_f124"
    assert isinstance(records[0]["observed_at"], datetime)


@pytest.mark.asyncio
async def test_intraday_limit_and_broken_pools_are_final_state_disjoint(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env
    target_date = date.today()

    from app.data.limit_pool import persist_tencent_limit_state
    observed = datetime.combine(target_date, datetime.min.time()).replace(hour=10)
    def quote(code, price, high):
        return dict(code=code, name="腾讯状态", price=price, high=high, low=10,
                    prev_close=10, limit_up=11, limit_down=9, volume=100,
                    source_quote_at=observed, received_at=observed)

    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="000003",
                    name="旧封板",
                    trade_date=target_date,
                    limit_up_price=10.0,
                    source="eastmoney",
                    quarantined=False,
                ),
                BrokenLimitPool(
                    code="000001",
                    name="历史交集",
                    trade_date=target_date,
                    source="eastmoney",
                ),
                BrokenLimitPool(
                    code="000003",
                    name="陈旧炸板",
                    trade_date=target_date,
                    source="eastmoney",
                ),
            ]
        )
        await session.commit()

    async with SessionLocal() as session:
        await persist_tencent_limit_state(
            session, [quote("000001", 11, 11), quote("000002", 10.8, 11),
                      quote("000003", 10.5, 10.8)],
            observed_at=observed, expected_count=3,
        )
        await session.commit()

    async with SessionLocal() as session:
        limit_rows = list(
            (
                await session.scalars(
                    select(LimitUpPool).where(
                        LimitUpPool.trade_date == target_date
                    )
                )
            ).all()
        )
        broken_rows = list(
            (
                await session.scalars(
                    select(BrokenLimitPool).where(
                        BrokenLimitPool.trade_date == target_date
                    )
                )
            ).all()
        )

    limit_codes = {row.code for row in limit_rows}
    broken_codes = {row.code for row in broken_rows}
    assert limit_codes == {"000001"}
    assert broken_codes == {"000002"}
    assert limit_codes.isdisjoint(broken_codes)
    assert limit_rows[0].limit_up_price == pytest.approx(11.0)


@pytest.mark.asyncio
async def test_close_reconcile_moves_stale_limit_row_to_broken_pool(
    scheduler_db_env,
):
    SessionLocal = scheduler_db_env
    target_date = date.today()
    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="002548",
                    name="尾盘开板",
                    trade_date=target_date,
                    limit_up_time="13:15:09",
                    limit_up_price=4.63,
                    seal_amount=3_000_000,
                    break_count=1,
                    consecutive_days=1,
                    source="eastmoney",
                    quarantined=False,
                ),
                BrokenLimitPool(
                    code="002548",
                    name="盘中炸板旧记录",
                    trade_date=target_date,
                    source="eastmoney",
                    final_state="unknown",
                ),
                StockSpot(
                    code="002548",
                    name="尾盘开板",
                    price=4.58,
                    prev_close=4.21,
                    open=4.24,
                    high=4.63,
                    low=4.18,
                    limit_up=4.63,
                    change_pct=8.79,
                    updated_at=datetime.combine(target_date, datetime.min.time()).replace(
                        hour=15,
                        minute=1,
                    ),
                ),
            ]
        )
        await session.commit()
        result = await DataScheduler._reconcile_broken_limit_close_state(
            session,
            target_date,
        )
        limit_row = await session.scalar(
            select(LimitUpPool).where(
                LimitUpPool.code == "002548",
                LimitUpPool.trade_date == target_date,
            )
        )
        broken_row = await session.scalar(
            select(BrokenLimitPool).where(
                BrokenLimitPool.code == "002548",
                BrokenLimitPool.trade_date == target_date,
            )
        )

    assert result["moved_stale_limit"] == 1
    assert limit_row is None
    assert broken_row is not None
    assert broken_row.final_state == "broken"
    assert broken_row.close_at_limit is False
    assert broken_row.close_price == pytest.approx(4.58)
    assert broken_row.limit_up_price == pytest.approx(4.63)


@pytest.mark.asyncio
async def test_spot_to_kline_fill_never_overwrites_authoritative_kline(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env

    async def fake_is_trade_day(dt=None):
        return True

    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", fake_is_trade_day)
    target_date = date(2026, 8, 13)
    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000001",
                name="测试股",
                price=10.5,
                prev_close=10.0,
                open=10.1,
                high=10.6,
                low=9.9,
                change_pct=5.0,
                volume=12345,
                amount=12_000_000,
                turnover=4.8,
                updated_at=datetime(2026, 8, 13, 21, 0, 0),
            )
        )
        session.add(
            StockKline(
                code="000001",
                trade_date=target_date,
                open=9.8,
                close=10.0,
                high=10.1,
                low=9.7,
                volume=9_999_999,
                amount=88_000_000,
                turnover=3.0,
                change_pct=2.0,
                prev_close=9.8,
                source="ths",
            )
        )
        await session.commit()

    scheduler = DataScheduler()
    await scheduler._spot_to_kline_fill()

    async with SessionLocal() as session:
        row = await session.scalar(
            select(StockKline).where(
                StockKline.code == "000001",
                StockKline.trade_date == target_date,
            )
        )

    assert row is not None
    assert row.source == "ths"
    assert row.close == pytest.approx(10.0)
    assert row.volume == 9_999_999


@pytest.mark.asyncio
async def test_close_finalize_replaces_conflicting_same_day_ths_bar(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env
    target_date = date.today()

    async def fake_is_trade_day(dt=None):
        return True

    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", fake_is_trade_day)
    async with SessionLocal() as session:
        session.add_all(
            [
                StockTag(
                    code="000001",
                    name="终场覆盖测试",
                    board_type="main_sz",
                    board_tag="tradeable",
                    is_st=False,
                    is_suspended=False,
                    is_delisting=False,
                    is_ipo_recent=False,
                ),
                StockSpot(
                    code="000001",
                    name="终场覆盖测试",
                    price=10.5,
                    prev_close=10.0,
                    open=10.1,
                    high=10.6,
                    low=9.9,
                    change_pct=5.0,
                    volume=12_345,
                    amount=12_000_000,
                    turnover=4.8,
                    updated_at=datetime.combine(target_date, datetime.min.time()).replace(
                        hour=15,
                        minute=1,
                    ),
                ),
                StockKline(
                    code="000001",
                    trade_date=target_date,
                    open=10.1,
                    close=10.2,
                    high=10.3,
                    low=9.9,
                    volume=1_000_000,
                    amount=10_000_000,
                    turnover=4.0,
                    change_pct=2.0,
                    prev_close=10.0,
                    source="ths",
                ),
            ]
        )
        await session.commit()
        health_before = await DataScheduler._close_snapshot_health(
            session,
            target_date,
        )

    scheduler = DataScheduler()
    result = await scheduler._spot_to_kline_fill(finalize_close=True)

    async with SessionLocal() as session:
        row = await session.scalar(
            select(StockKline).where(
                StockKline.code == "000001",
                StockKline.trade_date == target_date,
            )
        )
        health_after = await scheduler._close_snapshot_health(session, target_date)

    assert health_before["ready"] is False
    assert health_before["conflicting_close_count"] == 1
    assert result["written"] == 1
    assert row.source == "tencent_close"
    assert row.close == pytest.approx(10.5)
    assert row.volume == 1_234_500
    assert health_after["ready"] is True
    assert health_after["conflicting_close_count"] == 0


@pytest.mark.asyncio
async def test_ths_daily_preserves_fresh_same_day_tencent_close(scheduler_db_env, monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    SessionLocal = scheduler_db_env
    target_date = date.today()

    class DelayedThs:
        rate_limit = 0

        async def collect_daily(self, code):
            return [
                {
                    "code": code,
                    "trade_date": target_date.isoformat(),
                    "open": 10.0,
                    "close": 10.2,
                    "high": 10.3,
                    "low": 9.9,
                    "volume": 1_000_000,
                    "amount": 10_000_000,
                    "turnover": 4.0,
                    "change_pct": 2.0,
                    "prev_close": 10.0,
                    "source": "ths",
                }
            ]

    async with SessionLocal() as session:
        session.add_all(
            [
                StockTag(
                    code="000001",
                    name="保护终场测试",
                    board_type="main_sz",
                    board_tag="tradeable",
                    is_st=False,
                    is_suspended=False,
                    is_delisting=False,
                    is_ipo_recent=False,
                ),
                StockSpot(
                    code="000001",
                    name="保护终场测试",
                    price=10.5,
                    prev_close=10.0,
                    open=10.1,
                    high=10.6,
                    low=9.9,
                    change_pct=5.0,
                    updated_at=datetime.combine(target_date, datetime.min.time()).replace(
                        hour=15,
                        minute=1,
                    ),
                ),
                StockKline(
                    code="000001",
                    trade_date=target_date,
                    open=10.1,
                    close=10.5,
                    high=10.6,
                    low=9.9,
                    volume=1_234_500,
                    amount=12_000_000,
                    turnover=4.8,
                    change_pct=5.0,
                    prev_close=10.0,
                    source="tencent_close",
                ),
            ]
        )
        await session.commit()

    scheduler = DataScheduler()
    scheduler._sources["ths_kline"] = DelayedThs()
    await scheduler._ths_kline_daily()

    async with SessionLocal() as session:
        row = await session.scalar(
            select(StockKline).where(
                StockKline.code == "000001",
                StockKline.trade_date == target_date,
            )
        )

    assert row.source == "tencent_close"
    assert row.close == pytest.approx(10.5)
    assert row.volume == 1_234_500


@pytest.mark.asyncio
async def test_close_finalize_uses_only_each_rows_post_close_quote(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env
    target_date = date.today()

    async def fake_is_trade_day(dt=None):
        return True

    monkeypatch.setattr(
        scheduler_module.trade_calendar,
        "is_trade_day",
        fake_is_trade_day,
    )
    async with SessionLocal() as session:
        session.add_all(
            [
                StockSpot(
                    code="000001",
                    name="终场新鲜",
                    price=10.5,
                    prev_close=10.0,
                    open=10.1,
                    high=10.6,
                    low=9.9,
                    change_pct=5.0,
                    volume=12_345,
                    amount=12_000_000,
                    turnover=4.8,
                    updated_at=datetime.combine(
                        target_date,
                        datetime.min.time(),
                    ).replace(hour=15, minute=1),
                ),
                StockSpot(
                    code="000002",
                    name="盘中陈旧",
                    price=9.8,
                    prev_close=10.0,
                    open=10.0,
                    high=10.1,
                    low=9.7,
                    change_pct=-2.0,
                    volume=8_000,
                    amount=8_000_000,
                    turnover=2.0,
                    updated_at=datetime.combine(
                        target_date,
                        datetime.min.time(),
                    ).replace(hour=14, minute=59),
                ),
                StockKline(
                    code="000002",
                    trade_date=target_date,
                    open=10.0,
                    close=9.8,
                    high=10.1,
                    low=9.7,
                    volume=800_000,
                    amount=8_000_000,
                    turnover=2.0,
                    change_pct=-2.0,
                    prev_close=10.0,
                    source="tencent_close",
                ),
            ]
        )
        await session.commit()

    scheduler = DataScheduler()
    result = await scheduler._spot_to_kline_fill(finalize_close=True)

    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(StockKline).where(
                        StockKline.trade_date == target_date
                    )
                )
            ).all()
        )
        health = await scheduler._close_snapshot_health(session, target_date)

    assert result["written"] == 1
    assert result["same_day_spot_count"] == 2
    assert result["stale_spot_count"] == 1
    # Retain stale evidence; it must not contribute to canonical coverage.
    assert {(row.code, row.source) for row in rows} == {
        ("000001", "tencent_close"), ("000002", "tencent_close")
    }
    assert health["stale_tencent_close_count"] == 1
    assert result["cleared_previous_close_count"] == 0
    assert health["ready"] is False
    assert health["canonical_count"] == 1
    assert health["expected_count"] == 2
    assert health["completeness"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_spot_to_kline_fill_preserves_previous_close_conflict_as_evidence(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env

    async def fake_is_trade_day(dt=None):
        return True

    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", fake_is_trade_day)
    previous_date = date.today() - timedelta(days=1)
    current_date = date.today()
    async with SessionLocal() as session:
        session.add(
            StockKline(
                code="600127",
                trade_date=previous_date,
                open=9.7,
                close=10.3,
                high=10.3,
                low=9.6,
                volume=1_000_000,
                amount=10_000_000,
                turnover=2.0,
                change_pct=6.19,
                prev_close=9.7,
                source="ths",
            )
        )
        session.add(
            StockSpot(
                code="600127",
                name="金健米业",
                price=10.5,
                prev_close=10.0,
                open=10.1,
                high=10.6,
                low=9.9,
                change_pct=5.0,
                volume=12_345,
                amount=12_000_000,
                turnover=4.8,
                updated_at=datetime.combine(current_date, datetime.min.time()).replace(hour=15, minute=5),
            )
        )
        await session.commit()

    scheduler = DataScheduler()
    await scheduler._spot_to_kline_fill()

    async with SessionLocal() as session:
        previous_row = await session.scalar(
            select(StockKline).where(
                StockKline.code == "600127",
                StockKline.trade_date == previous_date,
            )
        )
        current_row = await session.scalar(
            select(StockKline).where(
                StockKline.code == "600127",
                StockKline.trade_date == current_date,
            )
        )

    assert previous_row is not None
    assert previous_row.source == "ths"
    assert previous_row.close == pytest.approx(10.3)
    assert previous_row.high == pytest.approx(10.3)
    assert previous_row.low == pytest.approx(9.6)
    assert previous_row.change_pct == pytest.approx(6.19)
    quality = scheduler.get_pipeline_runtime_status()["kline_price_chain"]
    assert quality["conflict_count"] == 1
    assert quality["historical_prices_changed"] is False
    assert quality["examples"][0]["current_prev_close"] == 10.0
    assert quality["examples"][0]["previous_close"] == 10.3
    assert current_row is not None
    assert current_row.prev_close == pytest.approx(10.0)


def test_anomaly_scan_requests_coalesce_and_preserve_earliest_queue_time(monkeypatch):
    timestamps = iter([100.0])
    monkeypatch.setattr(scheduler_module._time, "monotonic", lambda: next(timestamps))
    scheduler = DataScheduler()

    scheduler._request_anomaly_scan("tencent_spot_commit")
    scheduler._request_anomaly_scan("eastmoney_limit_structure")
    trigger, requested_at = scheduler._take_anomaly_scan_request()

    assert trigger == "eastmoney_limit_structure"
    assert requested_at == pytest.approx(100.0)
    assert scheduler._anomaly_scan_event.is_set() is False
    assert scheduler._anomaly_scan_requested_at is None


def test_anomaly_scan_throttle_preserves_event_but_limits_full_scan_frequency(monkeypatch):
    monkeypatch.setattr(scheduler_module.settings, "ANOMALY_PUSH_MIN_FULL_SCAN_GAP_SEC", 15)
    scheduler = DataScheduler()

    assert scheduler._anomaly_scan_throttle_delay(now=100.0) == 0.0
    scheduler._last_anomaly_scan_started_at = 100.0
    assert scheduler._anomaly_scan_throttle_delay(now=106.5) == pytest.approx(8.5)
    assert scheduler._anomaly_scan_throttle_delay(now=115.0) == 0.0

    scheduler._request_anomaly_scan("tencent_spot_commit")
    assert scheduler._anomaly_scan_event.is_set() is True


@pytest.mark.asyncio
async def test_tencent_spot_commit_bulk_upserts_and_requests_anomaly_scan(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 11, 10, 0, 0)

    class FakeTencentSource:
        async def collect_spot_batch(self, codes):
            assert codes == ["000001"]
            return [{"code": "000001", "name": "测试股", "price": 10.8}]

    async def fake_is_trade_day(dt=None):
        return True

    async def fake_is_trading_hours():
        # 日历通知可阻塞或失败，但不得再延迟已提交轮次的发布。
        assert scheduler._quote_round_payload is not None
        assert len(scheduler._momentum_quote_inbox) == 1
        return True

    monkeypatch.setattr(scheduler_module, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", fake_is_trade_day)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", fake_is_trading_hours)
    monkeypatch.setattr(
        scheduler_module.trade_calendar,
        "get_trade_session",
        lambda dt=None: "morning",
    )

    async with SessionLocal() as session:
        session.add(StockSpot(code="000001", name="旧名", price=9.5))
        await session.commit()

    scheduler = DataScheduler()
    # 单测只检查隔离DB提交与发布，不启动写盘归档后台任务。
    monkeypatch.setattr(scheduler, "_schedule_quote_round_archive", lambda payload: None)
    scheduler._sources["tencent"] = FakeTencentSource()
    scheduler._tradeable_codes = ["000001"]
    triggers = []
    queued_batches = []
    monkeypatch.setattr(scheduler, "_request_anomaly_scan", triggers.append)
    monkeypatch.setattr(
        "app.signal.anomaly_scanner.anomaly_scanner.enqueue_quote_batch",
        lambda records: queued_batches.append([dict(item) for item in records]) or len(records),
    )

    await scheduler._tencent_spot_collect()

    async with SessionLocal() as session:
        spot = await session.get(StockSpot, "000001")

    assert spot is not None
    assert spot.name == "测试股"
    assert spot.price == pytest.approx(10.8)
    assert spot.source_quote_at is None
    assert spot.received_at == datetime(2026, 8, 11, 10, 0, 0)
    assert spot.updated_at == datetime(2026, 8, 11, 10, 0, 0)
    assert len(queued_batches) == 1
    assert queued_batches[0][0]["received_at"] == datetime(2026, 8, 11, 10, 0, 0)
    assert queued_batches[0][0]["updated_at"] == datetime(2026, 8, 11, 10, 0, 0)
    assert triggers == ["tencent_spot_commit"]


@pytest.mark.asyncio
async def test_tencent_collection_reuses_one_http_client_per_market_round(monkeypatch):
    source = TencentSource()
    source.rate_limit = 0
    clients = []

    async def fake_fetch_batch(codes, *, client=None):
        clients.append(client)
        return {}

    monkeypatch.setattr(source, "_fetch_batch", fake_fetch_batch)

    await source.collect_spot_batch([f"{index:06d}" for index in range(205)])

    assert len(clients) == 3
    assert clients[0] is not None
    assert all(client is clients[0] for client in clients)
    assert clients[0].is_closed is True


@pytest.mark.asyncio
async def test_tencent_collection_retries_one_instant_connection_failure(monkeypatch):
    source = TencentSource()
    source.rate_limit = 0
    attempts = 0

    async def fake_fetch_batch(codes, *, client=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary dns failure")
        return {}

    monkeypatch.setattr(source, "_fetch_batch", fake_fetch_batch)

    await source.collect_spot_batch(["600186"])

    assert attempts == 2


@pytest.mark.asyncio
async def test_tencent_collection_uses_thread_rescue_when_full_market_coverage_collapses(monkeypatch):
    source = TencentSource()
    source.rate_limit = 0
    rescue_calls = []

    async def fake_fetch_batch(codes, *, client=None):
        return {}

    fields = [""] * 88
    fields[1] = "补抓测试股"
    fields[2] = "600000"
    fields[3] = "10.20"
    fields[4] = "10.00"
    fields[5] = "10.05"
    fields[6] = "1000"
    fields[32] = "2.00"
    fields[33] = "10.30"
    fields[34] = "9.95"
    fields[47] = "11.00"
    fields[48] = "9.00"
    fields[49] = "1.30"
    fields[51] = "10.12"

    def fake_rescue(codes):
        rescue_calls.append(list(codes))
        return {"600000": fields}

    monkeypatch.setattr(source, "_fetch_batch", fake_fetch_batch)
    monkeypatch.setattr(source, "_fetch_missing_batches_sync", fake_rescue)

    rows = await source.collect_spot_batch(
        ["600000", *[f"00{index:04d}" for index in range(599)]]
    )

    assert len(rescue_calls) == 1
    assert len(rescue_calls[0]) == 600
    assert [(row["code"], row["price"]) for row in rows] == [("600000", 10.20)]


@pytest.mark.asyncio
async def test_anomaly_refresh_timeout_releases_reentry_lock(scheduler_db_env, monkeypatch):
    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(scheduler_module.asyncio, "wait_for", fake_wait_for)
    scheduler = DataScheduler()

    await scheduler._refresh_anomaly_snapshot_background()

    assert scheduler._anomaly_snapshot_refreshing is False


def test_anomaly_request_during_refresh_is_replayed_after_current_scan():
    scheduler = DataScheduler()
    scheduler._anomaly_snapshot_refreshing = True

    scheduler._request_anomaly_scan("tencent_spot_commit")

    assert scheduler._anomaly_snapshot_pending is True
    assert scheduler._anomaly_scan_event.is_set() is True


@pytest.mark.asyncio
async def test_spot_to_kline_fill_isolates_old_snapshot_and_preserves_other_dates(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env
    async def fake_is_trade_day(dt=None):
        return dt == date(2026, 4, 23)

    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", fake_is_trade_day)

    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000001",
                name="测试股",
                price=10.5,
                prev_close=10.0,
                open=10.1,
                high=10.6,
                low=9.9,
                change_pct=5.0,
                volume=12345,
                amount=12_000_000,
                turnover=4.8,
                updated_at=datetime(2026, 4, 23, 23, 59, 55),
            )
        )
        session.add(
            StockKline(
                code="000001",
                trade_date=date(2026, 4, 24),
                open=10.1,
                close=10.5,
                high=10.6,
                low=9.9,
                volume=1_000_000,
                amount=12_000_000,
                turnover=4.8,
                change_pct=5.0,
                prev_close=10.0,
                source="spot_fallback",
            )
        )
        session.add(
            StockSpot(
                code="000004",
                name="无效行情",
                price=0.51,
                prev_close=0.51,
                open=0,
                high=0,
                low=0,
                change_pct=0,
                volume=0,
                amount=0,
                turnover=0,
                updated_at=datetime(2026, 4, 23, 23, 59, 55),
            )
        )
        await session.commit()

    scheduler = DataScheduler()
    await scheduler._spot_to_kline_fill()

    async with SessionLocal() as session:
        latest_trade_date = await session.scalar(select(func.max(StockKline.trade_date)))
        future_count = await session.scalar(
            select(func.count()).where(StockKline.trade_date == date(2026, 4, 24))
        )
        kline_23 = await session.scalar(
            select(StockKline).where(
                StockKline.code == "000001",
                StockKline.trade_date == date(2026, 4, 23),
            )
        )
        invalid_count = await session.scalar(
            select(func.count()).where(
                StockKline.code == "000004",
                StockKline.trade_date == date(2026, 4, 23),
            )
        )

    assert latest_trade_date == date(2026, 4, 24)
    assert future_count == 1  # no deletion of a different date's original row
    assert kline_23 is None  # late arrival cannot masquerade as known historical input
    assert invalid_count == 0
    async with SessionLocal() as session:
        candidates = (await session.scalars(select(StockKlineObservation))).all()
    assert len(candidates) == 2
    assert all(row.disposition == "isolated_historical" and row.available_at is None
               for row in candidates)
    import json
    valid = next(row for row in candidates if row.code == "000001")
    assert json.loads(valid.payload_json)["volume"] == 1234500


@pytest.mark.asyncio
async def test_spot_to_kline_fill_skips_non_trade_day_without_deleting_evidence(
    scheduler_db_env,
    monkeypatch,
):
    SessionLocal = scheduler_db_env

    async def fake_is_trade_day(dt=None):
        return False

    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", fake_is_trade_day)

    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000001",
                name="测试股",
                price=10.5,
                prev_close=10.0,
                open=10.1,
                high=10.6,
                low=9.9,
                change_pct=5.0,
                volume=12345,
                amount=12_000_000,
                turnover=4.8,
                updated_at=datetime(2026, 6, 19, 19, 0, 0),
            )
        )
        session.add(
            StockKline(
                code="000001",
                trade_date=date(2026, 6, 19),
                open=10.1,
                close=10.5,
                high=10.6,
                low=9.9,
                volume=1_234_500,
                amount=12_000_000,
                turnover=4.8,
                change_pct=5.0,
                prev_close=10.0,
                source="spot_fallback",
            )
        )
        await session.commit()

    scheduler = DataScheduler()
    await scheduler._spot_to_kline_fill()

    async with SessionLocal() as session:
        holiday_count = await session.scalar(
            select(func.count()).where(StockKline.trade_date == date(2026, 6, 19))
        )

    assert holiday_count == 1  # retained raw evidence, not eligible formal close


@pytest.mark.asyncio
async def test_nightly_ths_repair_archives_history_and_protects_current_close(
    scheduler_db_env, monkeypatch,
):
    from unittest.mock import AsyncMock
    from sqlalchemy import text
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    today = date.today()
    old_day = today - timedelta(days=3)
    old = dict(code="000001", trade_date=old_day, open=10.0, high=10.6, low=9.9,
               close=10.2, volume=1_000_000, amount=10_000_000.0, turnover=1.0,
               change_pct=2.0, prev_close=10.0, source="spot_fallback")
    final = {**old, "trade_date": today, "source": "tencent_close", "close": 10.5}
    candidates = [
        {**old, "source": "ths", "close": 10.4},
        {**final, "source": "ths", "close": 10.3},
        {**old, "source": "ths", "trade_date": today - timedelta(days=1)},
    ]
    async with scheduler_db_env() as db:
        db.add_all([StockKline(**old), StockKline(**final)])
        await db.commit()
        before = (await db.execute(text("SELECT * FROM stock_kline ORDER BY id"))).all()
    scheduler = DataScheduler()
    source = AsyncMock()
    source.collect_repair.return_value = candidates
    scheduler._sources["ths_kline"] = source
    result = await scheduler._ths_kline_recent_repair()
    async with scheduler_db_env() as db:
        assert (await db.execute(text("SELECT * FROM stock_kline ORDER BY id"))).all() == before
        observed = (await db.scalars(select(StockKlineObservation))).all()
    assert len(observed) == 5  # two originals + three newly observed candidates
    assert result["written"] == 0
    assert result["dispositions"] == {"isolated_historical": 2, "protected_source": 1}
    assert all(row.available_at is None for row in observed)
    assert scheduler.get_pipeline_runtime_status()["kline_observations"]["committed"] is True


@pytest.mark.asyncio
async def test_invalid_spot_close_preserves_original_instead_of_deleting_it(
    scheduler_db_env, monkeypatch,
):
    from unittest.mock import AsyncMock
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    today = date.today()
    async with scheduler_db_env() as db:
        db.add(StockKline(code="000001", trade_date=today, open=10.0, high=10.6,
                         low=9.9, close=10.5, source="tencent_close"))
        db.add(StockSpot(code="000001", price=float("inf"), open=10.0, high=10.6,
                         low=9.9, prev_close=10.0, volume=100,
                         updated_at=datetime.combine(today, datetime.min.time()).replace(hour=15, minute=1)))
        await db.commit()
    result = await DataScheduler()._spot_to_kline_fill(finalize_close=True)
    assert result["written"] == 0 and result["status"] == "degraded"
    async with scheduler_db_env() as db:
        assert (await db.scalar(select(StockKline))).close == 10.5
        candidate = await db.scalar(select(StockKlineObservation).where(
            StockKlineObservation.origin == "observed_candidate"))
        assert candidate.disposition == "isolated_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"source": "legacy_unknown"}, {"close": None}, {"close": float("inf")},
    {"open": float("nan")}, {"high": 10.1}, {"low": 10.2},
    {"source": "tencent_close", "high": float("inf")},
])
async def test_close_health_does_not_certify_bad_old_rows_by_source_label(scheduler_db_env, change):
    today = date.today()
    row = dict(code="000001", trade_date=today, open=10.0, close=10.5,
               high=10.6, low=9.9, source="ths")
    row.update(change)
    async with scheduler_db_env() as db:
        db.add(StockKline(**row))
        db.add(StockSpot(code="000001", price=10.5,
                         updated_at=datetime.combine(today, datetime.min.time()).replace(hour=15, minute=1)))
        await db.commit()
        health = await DataScheduler._close_snapshot_health(db, today)
        assert health["ready"] is False
        assert health["canonical_count"] == 0
        assert health["unsupported_source_count"] + health["invalid_formal_ohlc_count"] == 1
        if row["source"] == "tencent_close":
            assert health["fresh_tencent_close_count"] == 1
            assert health["stale_tencent_close_count"] == 0  # invalid != stale
            assert health["qualified_tencent_close_count"] == 0



def test_unavailable_and_invalid_sentiment_fields_are_distinguishable():
    """2026-09-18：`main_net_inflow=None` 是"拿不到"，不是"数值非法"。

    9/15–9/17 连续三日 `market_sentiment/snapshot` 报
    `情绪数值无效: main_net_inflow`，把"当日资金流一条都没通过新鲜度校验"
    误报成数值非法，告警无法指向真因。两者都仍关闭情绪闸门（degraded、
    completeness 0），行为不变，只是原因可区分。
    """
    base = dict(
        limit_up_count=60, limit_down_count=3, broken_limit_count=10,
        seal_rate=85.7, board_height=6, advance_decline_ratio=2.0,
        breadth_sample_count=5000, breadth_coverage=1.0,
        index_avg_change_pct=0.8, index_sample_count=3,
        turnover_total=1.2, fund_flow_coverage=1.0,
    )

    unavailable = _calculate_market_sentiment_state(main_net_inflow=None, **base)
    assert unavailable["quality_status"] == "degraded"
    assert unavailable["quality_completeness"] == 0.0
    assert unavailable["unavailable_fields"] == ["main_net_inflow"]
    assert unavailable["invalid_fields"] == []
    assert "不可用" in unavailable["quality_reason"]
    assert "无效" not in unavailable["quality_reason"]

    invalid = _calculate_market_sentiment_state(main_net_inflow=float("nan"), **base)
    assert invalid["quality_status"] == "degraded"
    assert invalid["invalid_fields"] == ["main_net_inflow"]
    assert invalid["unavailable_fields"] == []
    assert "无效" in invalid["quality_reason"]

    overflow = _calculate_market_sentiment_state(main_net_inflow=0.0, **{**base, "turnover_total": float("inf")})
    assert overflow["invalid_fields"] == ["turnover_total"]

    healthy = _calculate_market_sentiment_state(main_net_inflow=12.5, **base)
    assert healthy["quality_status"] == "ok"
    assert healthy["unavailable_fields"] == [] and healthy["invalid_fields"] == []


def test_unavailable_index_keeps_its_established_downstream_path():
    """`index_avg_change_pct=None` 有既有降级路径，不得被当成"字段不可用"提前返回。"""
    state = _calculate_market_sentiment_state(
        limit_up_count=60, limit_down_count=3, broken_limit_count=10,
        seal_rate=85.7, board_height=6, main_net_inflow=12.5,
        advance_decline_ratio=2.0, breadth_sample_count=5000, breadth_coverage=1.0,
        index_avg_change_pct=None, index_sample_count=0,
        turnover_total=1.2, fund_flow_coverage=1.0,
    )
    assert state["quality_reason"] != "情绪字段不可用: index_avg_change_pct"
    assert "index_avg_change_pct" not in state.get("unavailable_fields", [])
    assert "index_avg_change_pct" not in state.get("invalid_fields", [])
