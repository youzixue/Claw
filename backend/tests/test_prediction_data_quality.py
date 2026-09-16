from datetime import date, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.prediction_data_quality import PredictionDataQualityAuditor
from app.db.session import Base
from app.models.governance import DataQualityIssue, DataQualityRun, DataWatermark
from app.models.signal import PromotionPredictionRecord
from app.models.stock import AuctionData, FundFlow, LimitUpPool, StockKline, StockSpot, StockTag


@pytest_asyncio.fixture
async def quality_session(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'prediction_quality.db'}", future=True
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


async def _seed_complete_day(session: AsyncSession, trade_date_value: date) -> None:
    for index, code in enumerate(("000001", "600000"), start=1):
        session.add(StockSpot(code=code, name=f"测试{index}", price=10.0 + index))
        session.add(
            StockKline(
                code=code,
                trade_date=trade_date_value,
                open=10.0,
                high=10.5,
                low=9.8,
                close=10.2,
                volume=100_000,
            )
        )
        session.add(
            FundFlow(
                code=code,
                name=f"测试{index}",
                trade_date=trade_date_value,
                main_net_inflow=1_000_000,
                main_net_inflow_pct=1.0,
                source="eastmoney", source_version="individual_fund_flow_v3_f124",
                source_quote_at=datetime.combine(trade_date_value, datetime.min.time()).replace(hour=9, minute=34),
                received_at=datetime.combine(trade_date_value, datetime.min.time()).replace(hour=9, minute=34, second=1),
                observed_at=datetime.combine(trade_date_value, datetime.min.time()).replace(hour=9, minute=34, second=2),
            )
        )
    session.add(
        LimitUpPool(
            code="000001",
            name="测试1",
            trade_date=trade_date_value,
            consecutive_days=1,
        )
    )
    await session.commit()


from auction_test_evidence import verified_auction_fields


def _add_complete_auction_path(
    session: AsyncSession,
    trade_date_value: date,
    codes: tuple[str, ...] = ("000001", "600000"),
) -> None:
    """竞价可执行性要求至少两个正值帧，避免单点价格冒充完整增量路径。"""
    for code in codes:
        for index, auction_time in enumerate(("09:24:00", "09:25:00"), start=1):
            session.add(
                AuctionData(
                    code=code,
                    trade_date=trade_date_value,
                    auction_time=auction_time,
                    **verified_auction_fields(trade_date_value, auction_time),
                    auction_price=10.0 + index * 0.1,
                    prev_close=10.0,
                    open_change=float(index),
                    auction_volume=80_000 + index * 10_000,
                    auction_amount=800_000 + index * 100_000,
                    volume_ratio=1.0 + index * 0.1,
                )
            )


@pytest.mark.asyncio
async def test_complete_evening_snapshot_passes_quality_gate(quality_session):
    target = date(2026, 6, 18)
    await _seed_complete_day(quality_session, target)

    result = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_2000",
        persist=False,
    )

    assert result["gate_passed"] is True
    assert result["blocking_count"] == 0
    statuses = {item["dataset"]: item["status"] for item in result["watermarks"]}
    assert statuses["stock_kline"] == "ok"
    assert statuses["fund_flow"] == "ok"
    assert statuses["limit_up_pool"] == "ok"
    assert statuses["auction_data"] == "missing"


@pytest.mark.asyncio
async def test_0925_quality_window_accepts_restart_catchup_until_0940(quality_session):
    target = date(2026, 6, 18)
    quality_session.add_all(
        [
            PromotionPredictionRecord(
                code="000001",
                target_board=1,
                prediction_trade_date=target,
                snapshot_context="promotion_0925",
                snapshot_recorded_at=datetime(2026, 6, 18, 9, 34),
            ),
            PromotionPredictionRecord(
                code="000002",
                target_board=1,
                prediction_trade_date=target,
                snapshot_context="promotion_0925",
                snapshot_recorded_at=datetime(2026, 6, 18, 9, 40),
            ),
            PromotionPredictionRecord(
                code="000003",
                target_board=1,
                prediction_trade_date=target,
                snapshot_context="promotion_0925",
                snapshot_recorded_at=datetime(2026, 6, 18, 9, 40, 1),
            ),
        ]
    )
    await quality_session.commit()

    findings = await PredictionDataQualityAuditor()._snapshot_cutoff_findings(
        quality_session,
        target,
        lookback_days=20,
    )

    assert len(findings) == 1
    assert findings[0].issue_type == "snapshot_outside_context_window"
    assert findings[0].evidence["invalid_count"] == 1
    assert findings[0].evidence["examples"][0]["recorded_at"] == datetime(
        2026, 6, 18, 9, 40, 1
    )


@pytest.mark.asyncio
async def test_morning_auction_rows_without_incremental_fields_fail_closed(quality_session):
    target = date(2026, 6, 18)
    await _seed_complete_day(quality_session, target)
    for code in ("000001", "600000"):
        quality_session.add(
            AuctionData(
                code=code,
                trade_date=target,
                auction_time="09:25:00",
                auction_price=10.2,
                prev_close=10.0,
                open_change=2.0,
                auction_volume=0,
                auction_amount=0,
                volume_ratio=0,
            )
        )
    await quality_session.commit()

    result = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_0935",
        as_of_at=datetime(2026, 6, 18, 9, 35),
        persist=False,
    )

    auction = next(
        item for item in result["watermarks"] if item["dataset"] == "auction_data"
    )
    assert result["gate_passed"] is False
    assert auction["status"] == "blocked"
    assert auction["record_count"] == 0
    assert auction["expected_count"] == 2
    assert auction["completeness"] == 0.0
    assert auction["details"]["raw_record_count"] == 2
    assert auction["details"]["auction_health"]["field_degraded"] is True
    assert auction["max_available_at"] is None  # legacy clock labels stay unknown
    assert result["route_gates"]["second_board_promotion"]["gate_passed"] is True
    assert result["route_gates"]["mainline_spread_start"]["gate_passed"] is True
    assert result["route_gates"]["auction_surge_start"]["gate_passed"] is False
    assert result["route_gates"]["auction_surge_start"]["blocking_datasets"] == ["auction_data"]

    rows = list((await quality_session.scalars(select(AuctionData))).all())
    for row in rows:
        row.auction_volume = 100_000
        row.auction_amount = 1_000_000
        row.volume_ratio = 1.2
        quality_session.add(
            AuctionData(
                code=row.code,
                trade_date=target,
                auction_time="09:24:00",
                auction_price=10.1,
                prev_close=10.0,
                open_change=1.0,
                auction_volume=80_000,
                auction_amount=800_000,
                volume_ratio=1.1,
            )
        )
    await quality_session.commit()
    numeric_only = await PredictionDataQualityAuditor().audit(
        quality_session, trade_date_value=target, snapshot_context="promotion_0935",
        as_of_at=datetime(2026, 6, 18, 9, 35), persist=False,
    )
    assert numeric_only["route_gates"]["auction_surge_start"]["gate_passed"] is False
    # Only this isolated fixture simulates new, verified collection; production
    # migrations and consumers never backfill old auction records.
    for row in (await quality_session.scalars(select(AuctionData))).all():
        for key, value in verified_auction_fields(target, row.auction_time).items():
            setattr(row, key, value)
    await quality_session.commit()
    recovered = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_0935",
        as_of_at=datetime(2026, 6, 18, 9, 35),
        persist=False,
    )
    recovered_auction = next(
        item for item in recovered["watermarks"] if item["dataset"] == "auction_data"
    )
    assert recovered["gate_passed"] is True
    assert recovered_auction["status"] == "ok"
    assert recovered_auction["record_count"] == 2
    assert recovered_auction["completeness"] == 1.0
    assert all(
        gate["gate_passed"]
        for gate in recovered["route_gates"].values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_context", ["promotion_0935", "promotion_1030", "promotion_1305"])
async def test_intraday_current_limit_pool_does_not_require_unfinished_daily_kline(
    quality_session,
    snapshot_context,
):
    previous = date(2026, 6, 17)
    target = date(2026, 6, 18)
    await _seed_complete_day(quality_session, previous)
    quality_session.add(
        LimitUpPool(
            code="600000",
            name="盘中涨停",
            trade_date=target,
            consecutive_days=1,
        )
    )
    _add_complete_auction_path(quality_session, target)
    await quality_session.commit()

    morning = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context=snapshot_context,
        persist=False,
    )

    assert morning["gate_passed"] is True
    assert all(gate["gate_passed"] for gate in morning["route_gates"].values())
    assert not any(
        issue["issue_type"] == "non_trading_or_unobserved_date"
        and issue["trade_date"] == target.isoformat()
        for issue in morning["issues"]
    )
    dataset_dates = {
        item["dataset"]: item["trade_date"] for item in morning["watermarks"]
    }
    assert dataset_dates["stock_kline"] == previous.isoformat()
    assert dataset_dates["fund_flow"] == previous.isoformat()
    assert dataset_dates["limit_up_pool"] == target.isoformat()

    postmarket = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_2000",
        persist=False,
    )
    assert postmarket["gate_passed"] is False
    assert any(
        issue["issue_type"] == "non_trading_or_unobserved_date"
        and issue["trade_date"] == target.isoformat()
        for issue in postmarket["issues"]
    )


@pytest.mark.asyncio
async def test_intraday_spot_fallback_kline_uses_previous_completed_session(
    quality_session,
):
    """今日行情占位日K不能把完整的昨日正式日K挤出质量闸门。"""
    previous = date(2026, 6, 17)
    target = date(2026, 6, 18)
    await _seed_complete_day(quality_session, previous)
    for code in ("000001", "600000"):
        quality_session.add(
            StockTag(
                code=code,
                name=f"测试{code}",
                board_type="main_sz" if code.startswith("0") else "main_sh",
                board_tag="tradeable",
                is_st=False,
                is_suspended=False,
                is_delisting=False,
            )
        )
        quality_session.add(
            StockKline(
                code=code,
                trade_date=target,
                open=10.2,
                high=10.6,
                low=10.0,
                close=10.4,
                volume=50_000,
                source="spot_fallback",
            )
        )
    quality_session.add(
        LimitUpPool(
            code="600000",
            name="盘中涨停",
            trade_date=target,
            consecutive_days=1,
        )
    )
    _add_complete_auction_path(quality_session, target)
    await quality_session.commit()

    result = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_1030",
        persist=False,
    )

    kline = next(item for item in result["watermarks"] if item["dataset"] == "stock_kline")
    assert result["gate_passed"] is True
    assert kline["trade_date"] == previous.isoformat()
    assert kline["record_count"] == 2
    assert kline["expected_count"] == 2
    assert sum(kline["details"]["source_counts"].values()) == 2
    assert "spot_fallback" not in kline["details"]["source_counts"]
    assert kline["details"]["spot_fallback_count"] == 0
    assert kline["details"]["intraday_daily_bar_policy"] == "previous_completed_session"
    assert result["route_gates"]["second_board_promotion"]["gate_passed"] is True
    assert result["route_gates"]["mainline_spread_start"]["gate_passed"] is True


@pytest.mark.asyncio
async def test_morning_default_target_uses_today_not_latest_daily_kline(
    monkeypatch,
    quality_session,
):
    previous = date(2026, 6, 17)
    target = date(2026, 6, 18)
    await _seed_complete_day(quality_session, previous)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 6, 18)

    monkeypatch.setattr("app.core.prediction_data_quality.date", FixedDate)

    morning = await PredictionDataQualityAuditor().audit(
        quality_session,
        snapshot_context="promotion_0925",
        persist=False,
    )
    postmarket = await PredictionDataQualityAuditor().audit(
        quality_session,
        snapshot_context="promotion_2000",
        persist=False,
    )

    assert morning["trade_date"] == target.isoformat()
    assert postmarket["trade_date"] == previous.isoformat()


@pytest.mark.asyncio
async def test_kline_completeness_uses_tradeable_universe_not_all_market_peak(quality_session):
    """观察板历史K线缺口不能误伤只交易主板的生产质量闸门。"""
    target = date(2026, 6, 18)
    previous = date(2026, 6, 17)
    tags = (
        ("000001", "主板一", "main_sz", "tradeable"),
        ("600000", "主板二", "main_sh", "tradeable"),
        ("300001", "创业板", "gem", "observe_only"),
        ("688001", "科创板", "star", "observe_only"),
    )
    for code, name, board_type, board_tag in tags:
        quality_session.add(
            StockTag(
                code=code,
                name=name,
                board_type=board_type,
                board_tag=board_tag,
                is_st=False,
                is_suspended=False,
                is_delisting=False,
            )
        )
        quality_session.add(
            StockKline(
                code=code,
                trade_date=previous,
                open=10,
                high=10.5,
                low=9.8,
                close=10.2,
                volume=100_000,
            )
        )
        for auction_time, auction_price in (
            ("09:24:00", 10.1),
            ("09:25:00", 10.2),
        ):
            quality_session.add(
                AuctionData(
                    code=code,
                    trade_date=target,
                    auction_time=auction_time,
                    **verified_auction_fields(target, auction_time),
                    auction_price=auction_price,
                    prev_close=10.0,
                    open_change=(auction_price / 10.0 - 1) * 100,
                    auction_volume=100_000,
                    auction_amount=1_000_000,
                    volume_ratio=1.2,
                )
            )
        if board_tag == "tradeable":
            quality_session.add(
                StockKline(
                    code=code,
                    trade_date=target,
                    open=10,
                    high=10.5,
                    low=9.8,
                    close=10.2,
                    volume=100_000,
                )
            )
    quality_session.add(
        LimitUpPool(
            code="000001",
            name="主板一",
            trade_date=target,
            consecutive_days=1,
        )
    )
    await quality_session.commit()

    result = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_1510",
        persist=False,
    )

    kline = next(item for item in result["watermarks"] if item["dataset"] == "stock_kline")
    auction = next(item for item in result["watermarks"] if item["dataset"] == "auction_data")
    assert result["gate_passed"] is True
    assert kline["record_count"] == 2
    assert kline["expected_count"] == 2
    assert kline["completeness"] == 1.0
    assert kline["details"]["coverage_scope"] == "tradeable_stock_tags"
    assert kline["details"]["recent_peak_count"] == 4
    assert auction["record_count"] == 2
    assert auction["expected_count"] == 2
    assert auction["completeness"] == 1.0
    assert auction["details"]["coverage_scope"] == (
        "latest_auction_frame_tradeable_required_fields"
    )
    assert auction["details"]["auction_health"]["raw_snapshot_count"] == 4


@pytest.mark.asyncio
async def test_holiday_limit_pool_is_blocking_and_audit_is_persisted(quality_session):
    target = date(2026, 6, 22)
    await _seed_complete_day(quality_session, target)
    quality_session.add(
        LimitUpPool(
            code="000002",
            name="错误日期",
            trade_date=date(2026, 6, 19),
            consecutive_days=1,
        )
    )
    await quality_session.commit()

    result = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_2000",
        persist=True,
    )

    assert result["gate_passed"] is False
    assert result["run_id"] > 0
    assert any(
        issue["issue_type"] == "non_trading_or_unobserved_date"
        and issue["trade_date"] == "2026-06-19"
        for issue in result["issues"]
    )
    assert await quality_session.scalar(select(func.count()).select_from(DataQualityRun)) == 1
    assert await quality_session.scalar(select(func.count()).select_from(DataQualityIssue)) >= 1
    assert await quality_session.scalar(select(func.count()).select_from(DataWatermark)) == 4
    import json
    from app.promotion.route_contract import route_contract_payload, KNOWN_CANDIDATE_ROUTES
    persisted = await quality_session.scalar(select(DataQualityRun))
    frozen = json.loads(persisted.summary_json)
    assert frozen["route_contract"] == result["route_contract"] == route_contract_payload()
    assert set(frozen["route_gates"]) == KNOWN_CANDIDATE_ROUTES


@pytest.mark.asyncio
async def test_quarantined_dirty_limit_pool_is_not_blocking(quality_session):
    target = date(2026, 6, 22)
    await _seed_complete_day(quality_session, target)
    quality_session.add(
        LimitUpPool(
            code="000002",
            name="已隔离脏日期",
            trade_date=date(2026, 6, 19),
            consecutive_days=1,
            quarantined=True,
        )
    )
    await quality_session.commit()

    result = await PredictionDataQualityAuditor().audit(
        quality_session,
        trade_date_value=target,
        snapshot_context="promotion_2000",
        persist=False,
    )

    assert result["gate_passed"] is True
    assert result["blocking_count"] == 0
    assert not any(
        issue["issue_type"] == "non_trading_or_unobserved_date"
        for issue in result["issues"]
    )
