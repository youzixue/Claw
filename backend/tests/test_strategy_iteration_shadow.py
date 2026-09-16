import json
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.paper.strategy_iteration_shadow as shadow_module
from auction_test_evidence import verified_auction_fields
from app.config.settings import settings, PaperRouteSignalPolicy
from app.db.session import Base
from app.models import paper as paper_models  # noqa: F401
from app.models import stock as stock_models  # noqa: F401
from app.models.paper import PaperShadowEvaluation, PaperShadowEvent, PaperTradeLog
from app.models.stock import (
    AuctionData,
    LimitUpPool,
    SectorPersistence,
    StockKline,
    StockSectorMapping,
    StockSpot,
    StockTag,
)
from app.paper.strategy_iteration_shadow import (
    ROUTE_B,
    ROUTE_C,
    ROUTE_C3,
    ROUTE_D,
    ROUTE_F2,
    _confirmation_streak_status,
    _first_board_session_observation_health,
    build_strategy_iteration_evidence_summary,
    finalize_first_board_shadow_sessions,
    route_version_for,
    scan_strategy_iteration_shadow,
    settle_strategy_iteration_shadow,
)


@pytest_asyncio.fixture
async def shadow_env(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'strategy-iteration.db'}",
        future=True,
    )
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield SessionLocal
    await engine.dispose()


def _quote(code: str, *, open_price: float = 9.8) -> dict:
    return {
        "code": code,
        "name": f"测试{code}",
        "price": 10.05,
        "prev_close": 10.0,
        "open": open_price,
        "high": 10.10,
        "low": 9.70,
        "limit_up": 11.0,
        "limit_down": 9.0,
        "change_pct": 0.5,
        "amount": 30_000_000,
        "volume_ratio": 1.2,
        "avg_price": 10.02,
        "main_net_inflow": 2_000_000,
        "ask1_price": 10.06,
        "ask1_volume": 500,
        "bid1_price": 10.05,
        "bid1_volume": 800,
        "orderbook_imbalance": 0.15,
    }


def test_shadow_confirmation_accepts_subsecond_scheduler_jitter():
    first = datetime(2026, 9, 1, 9, 30, 0)
    status = _confirmation_streak_status(
        [first, first + timedelta(seconds=30)],
        first + timedelta(seconds=59, milliseconds=800),
    )

    assert status["ready"] is True
    assert status["sample_count"] == 3
    assert status["persistence_sec"] == pytest.approx(59.8)


def test_first_board_session_health_requires_real_afternoon_coverage(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MIN_SESSION_FRAMES", 4)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MIN_MORNING_FRAMES", 3)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MIN_AFTERNOON_FRAMES", 2)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MAX_FRAME_GAP_SEC", 7200)
    trade_day = date(2026, 9, 4)
    result = _first_board_session_observation_health(
        [
            datetime(2026, 9, 4, 9, 30),
            datetime(2026, 9, 4, 9, 31),
            datetime(2026, 9, 4, 11, 25),
            # A lone late recovery frame cannot stand in for the afternoon.
            datetime(2026, 9, 4, 14, 30),
        ],
        trade_day,
    )

    assert result["frame_count"] == 4
    assert result["checks"]["enough_frames"] is True
    assert result["checks"]["enough_morning_frames"] is True
    assert result["checks"]["enough_afternoon_frames"] is False
    assert result["checks"]["afternoon_started_on_time"] is False
    assert result["complete"] is False


async def _seed_structures(session: AsyncSession) -> None:
    previous_2 = date(2026, 8, 28)
    previous_1 = date(2026, 8, 31)
    codes = ("600001", "600002", "600003", "600004")
    for code in codes:
        session.add(
            StockTag(
                code=code,
                name=f"测试{code}",
                board_type="main_sh",
                board_tag="tradeable",
                is_st=False,
                is_suspended=False,
                is_delisting=False,
                is_ipo_recent=False,
            )
        )
        for trade_day in (previous_2, previous_1):
            session.add(
                StockKline(
                    code=code,
                    trade_date=trade_day,
                    open=10.0,
                    high=10.2,
                    low=9.8,
                    close=10.0,
                    prev_close=10.0,
                    change_pct=0.0,
                    volume=1_000_000,
                )
            )
    session.add_all(
        [
            LimitUpPool(
                code="600001",
                name="昨日首板",
                trade_date=previous_1,
                consecutive_days=1,
                limit_up_time="09:35:00",
                quarantined=False,
            ),
            LimitUpPool(
                code="600002",
                name="首板记忆",
                trade_date=previous_2,
                consecutive_days=1,
                limit_up_time="10:00:00",
                quarantined=False,
            ),
            LimitUpPool(
                code="600003",
                name="高标断板",
                trade_date=previous_2,
                consecutive_days=3,
                limit_up_time="10:30:00",
                quarantined=False,
            ),
            AuctionData(
                code="600004",
                trade_date=date(2026, 9, 1),
                auction_time="09:18:00",
                **verified_auction_fields(date(2026, 9, 1), "09:18:00"),
                auction_amount=100_000,
                auction_price=9.50,
                auction_volume=1_000,
                prev_close=10.0,
                open_change=-5.0,
            ),
            AuctionData(
                code="600004",
                trade_date=date(2026, 9, 1),
                auction_time="09:21:00",
                **verified_auction_fields(date(2026, 9, 1), "09:21:00"),
                auction_amount=100_000,
                auction_price=9.70,
                auction_volume=1_200,
                prev_close=10.0,
                open_change=-3.0,
            ),
            AuctionData(
                code="600004",
                trade_date=date(2026, 9, 1),
                auction_time="09:23:00",
                **verified_auction_fields(date(2026, 9, 1), "09:23:00"),
                auction_amount=100_000,
                auction_price=9.90,
                auction_volume=1_500,
                prev_close=10.0,
                open_change=-1.0,
            ),
            AuctionData(
                code="600004",
                trade_date=date(2026, 9, 1),
                auction_time="09:25:00",
                **verified_auction_fields(date(2026, 9, 1), "09:25:00"),
                auction_amount=100_000,
                auction_price=10.05,
                auction_volume=2_000,
                prev_close=10.0,
                open_change=0.5,
            ),
        ]
    )
    await session.commit()


async def _scan_three_confirmation_frames(
    session: AsyncSession,
    quotes: list[dict],
    first_at: datetime,
) -> list[dict]:
    results = []
    for seconds in (0, 30, 60):
        results.append(
            await scan_strategy_iteration_shadow(
                session,
                [{**quote, "source_quote_at": (first_at + timedelta(seconds=seconds)).isoformat()}
                 for quote in quotes],
                first_at + timedelta(seconds=seconds),
            )
        )
    return results


def _freeze_shadow_today(monkeypatch, frozen: date) -> None:
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return cls(frozen.year, frozen.month, frozen.day)

    monkeypatch.setattr(shadow_module, "date", FrozenDate)


def _relax_c3_session_health_for_unit_test(monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MIN_SESSION_FRAMES", 3)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MIN_MORNING_FRAMES", 3)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MIN_AFTERNOON_FRAMES", 0)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_FIRST_FRAME_DEADLINE",
        "10:00",
    )
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_MORNING_LAST_NOT_BEFORE",
        "10:01",
    )
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_LAST_FRAME_NOT_BEFORE",
        "10:01",
    )
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MAX_FRAME_GAP_SEC", 60)


@pytest.mark.asyncio
async def test_all_shape_routes_record_denominator_and_confirmations_without_orders(
    shadow_env,
    monkeypatch,
):
    # This test isolates route/streak persistence; relative-strength rejection is
    # covered separately below.
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {
        name: PaperRouteSignalPolicy(min_relative_strength_pct=0.0)
        for name in ("challenger_b", "challenger_c", "challenger_d", "challenger_f2")
    })
    SessionLocal = shadow_env
    observed_at = datetime(2026, 9, 1, 9, 40)
    quotes = [_quote(code) for code in ("600001", "600002", "600003", "600004")]

    async with SessionLocal() as session:
        await _seed_structures(session)
        first, second, result = await _scan_three_confirmation_frames(
            session,
            quotes,
            observed_at,
        )
        duplicate = await scan_strategy_iteration_shadow(
            session,
            quotes,
            observed_at + timedelta(seconds=60),
        )
        events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).order_by(
                        PaperShadowEvent.route_id,
                        PaperShadowEvent.event_type,
                    )
                )
            ).all()
        )
        trade_count = await session.scalar(
            select(func.count()).select_from(PaperTradeLog)
        )

    assert first["events"] == 12
    assert first["confirmed"] == 0
    assert second["events"] == 4
    assert second["confirmed"] == 0
    assert result["events"] == 8
    assert result["confirmed"] == 4
    assert duplicate["events"] == 0
    assert duplicate["confirmed"] == 0
    assert trade_count == 0
    assert {event.route_id for event in events} == {
        ROUTE_B,
        ROUTE_C,
        ROUTE_D,
        ROUTE_F2,
    }
    assert sum(event.event_type == "structural_pool" for event in events) == 4
    assert sum(event.event_type == "eligible" for event in events) == 4
    assert sum(event.event_type == "confirmation_sample" for event in events) == 12
    assert sum(event.event_type == "confirmed" for event in events) == 4
    assert all(event.assumed_fill_price for event in events if event.event_type == "confirmed")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_field", ["source_quote_at", "volume_unit", "price_basis"])
async def test_d2_positive_legacy_path_stays_observed_not_confirmed(shadow_env, missing_field):
    async with shadow_env() as session:
        await _seed_structures(session)
        rows = list((await session.scalars(select(AuctionData))).all())
        for row in rows:
            setattr(row, missing_field, None)
        await session.commit()
        await _scan_three_confirmation_frames(
            session, [_quote("600004")], datetime(2026, 9, 1, 9, 40)
        )
        events = list((await session.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == ROUTE_D
        ))).all())
        assert any(event.event_type == "structural_pool" for event in events)
        assert any(event.event_type == "evidence_blocked" for event in events)
        assert not any(event.event_type in {"eligible", "confirmed"} for event in events)
        assert await session.scalar(select(func.count()).select_from(PaperTradeLog)) == 0
        for row in rows:
            await session.refresh(row)
            assert getattr(row, missing_field) is None


def test_d2_evidence_version_rotates_without_changing_other_routes(monkeypatch):
    before = {route: route_version_for(route) for route in (ROUTE_B, ROUTE_C, ROUTE_D, ROUTE_F2)}
    monkeypatch.setattr(settings, "AUCTION_SOURCE_MAX_AGE_SEC", settings.AUCTION_SOURCE_MAX_AGE_SEC + 1)
    after = {route: route_version_for(route) for route in before}
    assert after[ROUTE_D] != before[ROUTE_D]
    assert all(after[route] == before[route] for route in before if route != ROUTE_D)


@pytest.mark.asyncio
async def test_generic_routes_require_market_relative_strength(shadow_env):
    SessionLocal = shadow_env
    first_at = datetime(2026, 9, 1, 9, 40)
    # All route candidates merely match the same-frame market median. Absolute
    # +0.5% alone must not satisfy the production +0.5pp relative-strength rule.
    quotes = [_quote(code) for code in ("600001", "600002", "600003", "600004")]
    async with SessionLocal() as session:
        await _seed_structures(session)
        await _scan_three_confirmation_frames(session, quotes, first_at)
        b_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).where(
                        PaperShadowEvent.route_id == ROUTE_B,
                        PaperShadowEvent.code == "600001",
                    )
                )
            ).all()
        )

    assert {row.event_type for row in b_events} == {"structural_pool", "eligible", "confirmation_reset"}
    assert all(row.event_type != "confirmed" for row in b_events)


@pytest.mark.asyncio
async def test_generic_confirmation_requires_rising_vwap_and_fillable_offer(
    shadow_env,
):
    SessionLocal = shadow_env
    first_at = datetime(2026, 9, 1, 9, 40)

    def frame(avg_price: float, *, ask_volume: float = 500, seconds: int = 0) -> list[dict]:
        rows = [_quote(code) for code in ("600001", "600002", "600003", "600004")]
        for row in rows:
            row.update({"price": 10.0, "high": 10.02, "avg_price": 10.0,
                        "source_quote_at": (first_at + timedelta(seconds=seconds)).isoformat()})
        rows[0].update({
            "price": 10.10,
            "high": 10.12,
            "avg_price": avg_price,
            "ask1_price": 10.11,
            "ask1_volume": ask_volume,
        })
        return rows

    async with SessionLocal() as session:
        await _seed_structures(session)
        # A positive ask price with zero displayed volume is not a fillable frame.
        await scan_strategy_iteration_shadow(
            session,
            frame(10.02, ask_volume=0),
            first_at,
        )
        no_offer_samples = await session.scalar(
            select(func.count()).select_from(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_B,
                PaperShadowEvent.code == "600001",
                PaperShadowEvent.event_type == "confirmation_sample",
            )
        )
        # Three later valid frames satisfy time persistence but VWAP slopes down,
        # so v4 remains waiting instead of manufacturing a confirmation.
        for offset, average in ((30, 10.02), (60, 10.01), (90, 10.00)):
            await scan_strategy_iteration_shadow(
                session,
                frame(average, seconds=offset),
                first_at + timedelta(seconds=offset),
            )
        b_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).where(
                        PaperShadowEvent.route_id == ROUTE_B,
                        PaperShadowEvent.code == "600001",
                    )
                )
            ).all()
        )

    assert no_offer_samples == 0
    assert sum(row.event_type == "confirmation_sample" for row in b_events) == 3
    assert all(row.event_type != "confirmed" for row in b_events)
    latest_sample = max(
        (row for row in b_events if row.event_type == "confirmation_sample"),
        key=lambda row: row.observed_at,
    )
    confirmation = json.loads(latest_sample.snapshot_json)["prior_structure"][
        "confirmation"
    ]
    assert confirmation["time_streak_ready"] is True
    assert confirmation["vwap_slope_passed"] is False
    assert confirmation["ready"] is False


@pytest.mark.asyncio
async def test_missing_indicative_auction_path_is_a_coverage_block_not_a_signal(
    shadow_env,
):
    SessionLocal = shadow_env
    async with SessionLocal() as session:
        session.add(
            StockTag(
                code="600010",
                name="无竞价路径",
                board_type="main_sh",
                board_tag="tradeable",
                is_st=False,
                is_suspended=False,
                is_delisting=False,
                is_ipo_recent=False,
            )
        )
        session.add(
            StockKline(
                code="600010",
                trade_date=date(2026, 8, 31),
                open=10,
                high=10,
                low=10,
                close=10,
                prev_close=10,
                change_pct=0,
                volume=1000,
            )
        )
        # 价格 0 是缺失，不得因为上游同时给出 -10% 就伪造成竞价跌停路径。
        session.add_all(
            [
                AuctionData(
                    code="600010",
                    trade_date=date(2026, 9, 1),
                    auction_time="09:18:00",
                    auction_price=0,
                    prev_close=10.0,
                    open_change=-10.0,
                ),
                AuctionData(
                    code="600010",
                    trade_date=date(2026, 9, 1),
                    auction_time="09:25:00",
                    auction_price=10.05,
                    prev_close=10.0,
                    open_change=0.5,
                ),
            ]
        )
        await session.commit()

        result = await scan_strategy_iteration_shadow(
            session,
            [_quote("600010")],
            datetime(2026, 9, 1, 9, 40),
        )
        coverage = await session.scalar(
            select(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_D,
                PaperShadowEvent.event_type == "coverage_blocked",
            )
        )

    assert result["confirmed"] == 0
    assert coverage is not None
    assert coverage.code == "MARKET"
    assert coverage.assumed_fill_price is None


@pytest.mark.asyncio
async def test_complete_auction_path_stays_in_denominator_when_recovery_rule_fails(
    shadow_env,
):
    SessionLocal = shadow_env
    trade_date = date(2026, 9, 1)
    async with SessionLocal() as session:
        session.add_all(
            [
                StockTag(
                    code="600011",
                    name="竞价完整未触发",
                    board_type="main_sh",
                    board_tag="tradeable",
                    is_st=False,
                    is_suspended=False,
                    is_delisting=False,
                    is_ipo_recent=False,
                ),
                StockKline(
                    code="600011",
                    trade_date=date(2026, 8, 31),
                    open=10,
                    high=10,
                    low=10,
                    close=10,
                    prev_close=10,
                    change_pct=0,
                    volume=1000,
                ),
                AuctionData(
                    code="600011",
                    trade_date=trade_date,
                    auction_time="09:18:00",
                    auction_price=9.95,
                    prev_close=10.0,
                    open_change=-0.5,
                ),
                AuctionData(
                    code="600011",
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    auction_price=10.0,
                    prev_close=10.0,
                    open_change=0.0,
                ),
            ]
        )
        await session.commit()

        result = await scan_strategy_iteration_shadow(
            session,
            [_quote("600011", open_price=10.0)],
            datetime(2026, 9, 1, 9, 40),
        )
        route_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).where(PaperShadowEvent.route_id == ROUTE_D)
                )
            ).all()
        )

    assert result["confirmed"] == 0
    assert [item.event_type for item in route_events] == ["structural_pool"]


@pytest.mark.asyncio
async def test_auction_price_recovery_without_positive_volume_path_is_blocked(
    shadow_env,
):
    SessionLocal = shadow_env
    trade_date = date(2026, 9, 1)
    async with SessionLocal() as session:
        session.add_all(
            [
                StockTag(
                    code="600012",
                    name="竞价零量占位",
                    board_type="main_sh",
                    board_tag="tradeable",
                    is_st=False,
                    is_suspended=False,
                    is_delisting=False,
                    is_ipo_recent=False,
                ),
                StockKline(
                    code="600012",
                    trade_date=date(2026, 8, 31),
                    open=10,
                    high=10,
                    low=10,
                    close=10,
                    prev_close=10,
                    change_pct=0,
                    volume=1000,
                ),
                AuctionData(
                    code="600012",
                    trade_date=trade_date,
                    auction_time="09:18:00",
                    auction_price=9.5,
                    auction_volume=0,
                    prev_close=10.0,
                ),
                AuctionData(
                    code="600012",
                    trade_date=trade_date,
                    auction_time="09:21:00",
                    auction_price=9.7,
                    auction_volume=0,
                    prev_close=10.0,
                ),
                AuctionData(
                    code="600012",
                    trade_date=trade_date,
                    auction_time="09:23:00",
                    auction_price=9.9,
                    auction_volume=0,
                    prev_close=10.0,
                ),
                AuctionData(
                    code="600012",
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    auction_price=10.05,
                    auction_volume=6500,
                    prev_close=10.0,
                ),
            ]
        )
        await session.commit()

        await _scan_three_confirmation_frames(
            session,
            [_quote("600012")],
            datetime(2026, 9, 1, 9, 40),
        )
        route_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).where(
                        PaperShadowEvent.route_id == ROUTE_D,
                        PaperShadowEvent.code == "600012",
                    )
                )
            ).all()
        )

    assert {item.event_type for item in route_events} == {
        "structural_pool",
        "evidence_blocked",
    }
    assert all(item.assumed_fill_price is None for item in route_events)


@pytest.mark.asyncio
async def test_shadow_outcome_right_censors_ex_right_price_chain_break(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE", 0.20)
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {
        name: PaperRouteSignalPolicy(min_relative_strength_pct=0.0)
        for name in ("challenger_b", "challenger_c", "challenger_d", "challenger_f2")
    })
    SessionLocal = shadow_env
    trade_date = date(2026, 9, 1)
    async with SessionLocal() as session:
        await _seed_structures(session)
        await _scan_three_confirmation_frames(
            session,
            [_quote("600001")],
            datetime(2026, 9, 1, 9, 40),
        )
        session.add_all(
            [
                StockKline(
                    code="600001",
                    trade_date=trade_date,
                    open=9.8,
                    high=10.1,
                    low=9.7,
                    close=10.05,
                    prev_close=10.0,
                    change_pct=0.5,
                    volume=1_000_000,
                ),
                StockKline(
                    code="600001",
                    trade_date=date(2026, 9, 2),
                    open=9.5,
                    high=9.7,
                    low=9.4,
                    close=9.6,
                    prev_close=9.5,
                    change_pct=1.05,
                    volume=1_000_000,
                ),
            ]
        )
        await session.commit()

        result = await settle_strategy_iteration_shadow(
            session,
            as_of_date=date(2026, 9, 2),
        )
        evaluation_count = await session.scalar(
            select(func.count()).select_from(PaperShadowEvaluation)
        )

    assert result["signals"] == 1
    assert result["evaluations_added"] == 0
    assert evaluation_count == 0


@pytest.mark.asyncio
async def test_later_settlement_accepts_persisted_historical_tencent_close(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE", 0.20)
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {
        name: PaperRouteSignalPolicy(min_relative_strength_pct=0.0)
        for name in ("challenger_b", "challenger_c", "challenger_d", "challenger_f2")
    })
    SessionLocal = shadow_env
    async with SessionLocal() as session:
        await _seed_structures(session)
        await _scan_three_confirmation_frames(
            session,
            [_quote("600001")],
            datetime(2026, 9, 1, 9, 40),
        )
        session.add_all([
            StockKline(
                code="600001",
                trade_date=date(2026, 9, 1),
                open=9.8,
                high=10.1,
                low=9.7,
                close=10.05,
                prev_close=10.0,
                change_pct=0.5,
                volume=1_000_000,
                source="tencent_close",
            ),
            StockKline(
                code="600001",
                trade_date=date(2026, 9, 2),
                open=10.05,
                high=10.3,
                low=10.0,
                close=10.2,
                prev_close=10.05,
                change_pct=1.4925,
                volume=1_000_000,
                source="tencent_close",
            ),
            # The mutable live spot has already advanced to the next session.
            # It must not invalidate immutable Sep-1/Sep-2 close rows.
            StockSpot(
                code="600001",
                name="测试600001",
                price=10.2,
                prev_close=10.05,
                updated_at=datetime(2026, 9, 3, 10, 0),
            ),
        ])
        await session.commit()
        result = await settle_strategy_iteration_shadow(
            session,
            as_of_date=date(2026, 9, 2),
        )
        evaluation = await session.scalar(select(PaperShadowEvaluation))

    assert result == {"signals": 1, "evaluations_added": 1}
    assert evaluation is not None
    assert evaluation.horizon_days == 1
    assert evaluation.exit_trade_date == date(2026, 9, 2)


@pytest.mark.asyncio
async def test_shadow_outcomes_are_right_censored_by_available_trade_sessions(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {
        name: PaperRouteSignalPolicy(min_relative_strength_pct=0.0)
        for name in ("challenger_b", "challenger_c", "challenger_d", "challenger_f2")
    })
    SessionLocal = shadow_env
    observed_at = datetime(2026, 9, 1, 9, 40)
    codes = ("600001", "600002", "600003", "600004")
    async with SessionLocal() as session:
        await _seed_structures(session)
        await _scan_three_confirmation_frames(
            session,
            [_quote(code) for code in codes],
            observed_at,
        )
        for code in codes:
            session.add(
                StockKline(
                    code=code,
                    trade_date=date(2026, 9, 1),
                    open=9.8,
                    high=10.10,
                    low=9.70,
                    close=10.05,
                    prev_close=10.0,
                    change_pct=0.5,
                    volume=1_000_000,
                )
            )
        for offset, trade_day in enumerate(
            (date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)),
            start=1,
        ):
            for code in codes:
                session.add(
                    StockKline(
                        code=code,
                        trade_date=trade_day,
                        open=10.05 + offset * 0.02,
                        high=10.20 + offset * 0.02,
                        low=9.95,
                        close=10.10 + offset * 0.03,
                        prev_close=(
                            10.05
                            if offset == 1
                            else 10.10 + (offset - 1) * 0.03
                        ),
                        change_pct=0.5,
                        volume=1_000_000,
                    )
                )
        await session.commit()

        result = await settle_strategy_iteration_shadow(
            session,
            as_of_date=date(2026, 9, 4),
        )
        evaluations = list((await session.scalars(select(PaperShadowEvaluation))).all())

    assert result["evaluations_added"] == 8
    assert {item.horizon_days for item in evaluations} == {1, 3}
    assert all(item.horizon_days != 5 for item in evaluations)

    summary = build_strategy_iteration_evidence_summary(
        evaluations,
        route_id=ROUTE_B,
        horizon_days=3,
    )
    assert summary["sample_count"] == 1
    assert summary["independent_sessions"] == 1
    assert summary["max_session_share"] == 1.0
    assert summary["max_code_share"] == 1.0
    assert summary["gates"]["enough_independent_sessions"] is False
    assert summary["gates"]["enough_confirmed_samples"] is False
    assert summary["benchmark_label"] == "同期全市场个股等权平均涨跌幅"
    assert "佣金" in summary["return_basis"]
    assert summary["evidence_gate_passed"] is False
    assert summary["execution_enabled"] is False
    assert summary["promotion_requires_manual_review"] is True


def _first_board_quote(code: str, *, confirms: bool) -> dict:
    price = 10.30 if confirms else 10.20
    return {
        "code": code,
        "name": f"首板候选{code}",
        "price": price,
        "prev_close": 10.0,
        "open": 10.05,
        "high": 10.35 if confirms else 10.25,
        "low": 9.95,
        "change_pct": round((price / 10.0 - 1) * 100, 2),
        "amount": 50_000_000,
        "volume_ratio": 1.5,
        "avg_price": 10.15 if confirms else 10.21,
        "main_net_inflow": 3_000_000,
        "ask1_price": price + 0.01,
        "ask1_volume": 500,
        "bid1_price": price,
        "bid1_volume": 900,
        "orderbook_imbalance": 0.20,
        "limit_up": 11.0,
    }


async def _seed_first_board_denominator(session: AsyncSession) -> None:
    for code in ("600201", "600202"):
        session.add_all(
            [
                StockTag(
                    code=code,
                    name=f"首板候选{code}",
                    board_type="main_sh",
                    board_tag="tradeable",
                    is_st=False,
                    is_suspended=False,
                    is_delisting=False,
                    is_ipo_recent=False,
                ),
                StockKline(
                    code=code,
                    trade_date=date(2026, 9, 3),
                    open=10.0,
                    high=10.1,
                    low=9.9,
                    close=10.0,
                    prev_close=10.0,
                    change_pct=0.0,
                    volume=1_000_000,
                    source="ths",
                ),
                StockSectorMapping(
                    code=code,
                    sector_code="pw_concept_first_board_test",
                    sector_name="首板测试主线",
                    sector_type="concept",
                    source="pywencai",
                    source_version="test_v1",
                    observed_at=datetime(2026, 9, 3, 15, 10),
                ),
            ]
        )
    session.add(
        SectorPersistence(
            sector_code="pw_concept_first_board_test",
            sector_name="首板测试主线",
            trade_date=date(2026, 9, 4),
            consecutive_days=2,
            limit_up_count=2,
            fund_flow=5.0,
            change_pct=1.0,
            strength_score=60.0,
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_first_board_shadow_keeps_complete_denominator_and_never_orders(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "2026-09-04",
    )
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    _relax_c3_session_health_for_unit_test(monkeypatch)
    SessionLocal = shadow_env
    first_at = datetime(2026, 9, 4, 10, 0)
    quotes = [
        _first_board_quote("600201", confirms=True),
        _first_board_quote("600202", confirms=False),
    ]

    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        results = await _scan_three_confirmation_frames(session, quotes, first_at)
        c3_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent)
                    .where(PaperShadowEvent.route_id == ROUTE_C3)
                    .order_by(PaperShadowEvent.observed_at, PaperShadowEvent.id)
                )
            ).all()
        )
        trade_count = await session.scalar(
            select(func.count()).select_from(PaperTradeLog)
        )

    assert results[-1]["confirmed"] == 1
    assert sum(row.event_type == "structural_pool" for row in c3_events) == 2
    assert sum(row.event_type == "eligible" for row in c3_events) == 2
    assert sum(row.event_type == "universe_audit" for row in c3_events) == 3
    assert sum(row.event_type == "confirmation_sample" for row in c3_events) == 3
    assert sum(row.event_type == "confirmed" for row in c3_events) == 1
    assert {row.code for row in c3_events if row.event_type == "confirmed"} == {
        "600201"
    }
    assert all(row.route_version == route_version_for(ROUTE_C3) for row in c3_events)
    assert route_version_for(ROUTE_C3) != settings.PAPER_STRATEGY_ITERATION_SHADOW_VERSION
    assert trade_count == 0
    structural_snapshot = json.loads(
        next(row.snapshot_json for row in c3_events if row.event_type == "structural_pool")
    )
    assert structural_snapshot["prior_structure"]["denominator_role"] == (
        "fresh_first_board_active_sector"
    )
    assert structural_snapshot["eligible_for_production"] is False
    universe_snapshot = json.loads(
        next(row.snapshot_json for row in c3_events if row.event_type == "universe_audit")
    )
    context_audit = universe_snapshot["prior_structure"]["context_audit"]
    assert context_audit["mapped_code_count"] == 2
    assert context_audit["persistence_context_code_count"] == 2
    assert context_audit["structural_context_code_count"] == 2
    assert context_audit["reason_counts"]["missing_mapping"] == 0


@pytest.mark.asyncio
async def test_first_board_shadow_controls_are_post_cutoff_and_outcome_independent(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "2026-09-04",
    )
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    _relax_c3_session_health_for_unit_test(monkeypatch)
    SessionLocal = shadow_env
    trade_day = date(2026, 9, 4)
    quotes = [
        _first_board_quote("600201", confirms=True),
        _first_board_quote("600202", confirms=False),
    ]

    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        await _scan_three_confirmation_frames(
            session,
            quotes,
            datetime(2026, 9, 4, 10, 0),
        )
        before_close = await finalize_first_board_shadow_sessions(
            session,
            as_of_date=trade_day,
            now=datetime(2026, 9, 4, 14, 59),
        )
        assert before_close["status"] == "before_formal_close"

        for code in ("600201", "600202"):
            session.add(
                StockKline(
                    code=code,
                    trade_date=trade_day,
                    open=10.05,
                    high=11.0,
                    low=9.95,
                    close=11.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=2_000_000,
                    source="ths",
                )
            )
        await session.commit()
        finalized = await finalize_first_board_shadow_sessions(
            session,
            as_of_date=trade_day,
            now=datetime(2026, 9, 4, 20, 35),
        )
        route_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).where(
                        PaperShadowEvent.route_id == ROUTE_C3
                    )
                )
            ).all()
        )

        session.add_all(
            [
                StockKline(
                    code="600201",
                    trade_date=date(2026, 9, 7),
                    open=11.10,
                    high=11.60,
                    low=10.90,
                    close=11.50,
                    prev_close=11.0,
                    change_pct=4.5455,
                    volume=1_800_000,
                    source="ths",
                ),
                StockKline(
                    code="600202",
                    trade_date=date(2026, 9, 7),
                    open=10.95,
                    high=11.10,
                    low=10.70,
                    close=10.80,
                    prev_close=11.0,
                    change_pct=-1.8182,
                    volume=1_600_000,
                    source="ths",
                ),
            ]
        )
        await session.commit()
        settled = await settle_strategy_iteration_shadow(
            session,
            as_of_date=date(2026, 9, 7),
        )
        evaluations = list(
            (
                await session.scalars(
                    select(PaperShadowEvaluation).where(
                        PaperShadowEvaluation.route_id == ROUTE_C3,
                        PaperShadowEvaluation.horizon_days == 1,
                    )
                )
            ).all()
        )

    assert finalized["controls_added"] == 1
    assert finalized["outcomes_added"] == 2
    assert finalized["sessions_ready_added"] == 1
    assert {row.code for row in route_events if row.event_type == "control"} == {
        "600202"
    }
    assert {
        row.status for row in route_events if row.event_type == "session_outcome"
    } == {"confirmed_first_board", "unconfirmed_first_board"}
    control = next(row for row in route_events if row.event_type == "control")
    control_snapshot = json.loads(control.snapshot_json)
    assert control_snapshot["prior_structure"]["selected_without_market_outcome"] is True
    assert settled["evaluations_added"] == 2
    assert {
        json.loads(row.details_json)["cohort"] for row in evaluations
    } == {"confirmed", "eligible_unconfirmed_control"}

    summary = build_strategy_iteration_evidence_summary(
        evaluations,
        route_id=ROUTE_C3,
        route_version=route_version_for(ROUTE_C3),
        horizon_days=1,
    )
    assert summary["sample_count"] == 1
    assert summary["control_sample_count"] == 1
    assert summary["confirmed_minus_control_pct"] > 0
    assert summary["gates"]["beats_unconfirmed_control"] is True
    assert summary["execution_enabled"] is False


@pytest.mark.asyncio
async def test_first_board_shadow_blocks_incomplete_quote_denominator(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "2026-09-04",
    )
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    SessionLocal = shadow_env

    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        await scan_strategy_iteration_shadow(
            session,
            [_first_board_quote("600201", confirms=True)],
            datetime(2026, 9, 4, 10, 0),
        )
        c3_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).where(
                        PaperShadowEvent.route_id == ROUTE_C3
                    )
                )
            ).all()
        )

    assert [(row.event_type, row.code) for row in c3_events] == [
        ("coverage_blocked", "MARKET")
    ]
    snapshot = json.loads(c3_events[0].snapshot_json)
    assert snapshot["prior_structure"]["quote_coverage"] == 0.5


@pytest.mark.asyncio
async def test_first_board_shadow_refuses_historical_event_insertion(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "2026-09-04",
    )
    # Even after activation, replaying yesterday's clock must not create C3 rows.
    _freeze_shadow_today(monkeypatch, date(2026, 9, 5))
    SessionLocal = shadow_env
    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        await scan_strategy_iteration_shadow(
            session,
            [
                _first_board_quote("600201", confirms=True),
                _first_board_quote("600202", confirms=False),
            ],
            datetime(2026, 9, 4, 10, 0),
        )
        count = await session.scalar(
            select(func.count()).select_from(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_C3
            )
        )

    assert count == 0


@pytest.mark.asyncio
async def test_first_board_shadow_invalid_activation_date_is_fail_closed(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "not-a-date",
    )
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    SessionLocal = shadow_env
    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        await scan_strategy_iteration_shadow(
            session,
            [
                _first_board_quote("600201", confirms=True),
                _first_board_quote("600202", confirms=False),
            ],
            datetime(2026, 9, 4, 10, 0),
        )
        finalized = await finalize_first_board_shadow_sessions(
            session,
            as_of_date=date(2026, 9, 4),
            now=datetime(2026, 9, 4, 20, 35),
        )
        count = await session.scalar(
            select(func.count()).select_from(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_C3
            )
        )

    assert count == 0
    assert finalized["status"] == "activation_config_invalid"


@pytest.mark.asyncio
async def test_first_board_shadow_blocks_missing_current_sector_context(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "2026-09-04",
    )
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    SessionLocal = shadow_env
    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        missing = await session.scalar(
            select(StockSectorMapping).where(StockSectorMapping.code == "600202")
        )
        missing.sector_code = "pw_concept_missing_snapshot"
        await session.commit()
        await scan_strategy_iteration_shadow(
            session,
            [
                _first_board_quote("600201", confirms=True),
                _first_board_quote("600202", confirms=False),
            ],
            datetime(2026, 9, 4, 10, 0),
        )
        c3_events = list(
            (
                await session.scalars(
                    select(PaperShadowEvent).where(
                        PaperShadowEvent.route_id == ROUTE_C3
                    )
                )
            ).all()
        )

    assert [(row.event_type, row.code) for row in c3_events] == [
        ("coverage_blocked", "MARKET")
    ]
    audit = json.loads(c3_events[0].snapshot_json)["prior_structure"]
    assert audit["persistence_context_coverage"] == 0.5
    assert audit["context_audit"]["reason_counts"][
        "missing_current_sector_persistence"
    ] == 1


@pytest.mark.asyncio
async def test_first_board_outcomes_reject_stale_tencent_close_rows(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "2026-09-04",
    )
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    _relax_c3_session_health_for_unit_test(monkeypatch)
    SessionLocal = shadow_env
    trade_day = date(2026, 9, 4)
    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        await _scan_three_confirmation_frames(
            session,
            [
                _first_board_quote("600201", confirms=True),
                _first_board_quote("600202", confirms=False),
            ],
            datetime(2026, 9, 4, 10, 0),
        )
        for code in ("600201", "600202"):
            session.add(
                StockKline(
                    code=code,
                    trade_date=trade_day,
                    open=10.05,
                    high=11.0,
                    low=9.95,
                    close=11.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=2_000_000,
                    source="tencent_close",
                )
            )
        await session.commit()
        finalized = await finalize_first_board_shadow_sessions(
            session,
            as_of_date=trade_day,
            now=datetime(2026, 9, 4, 20, 35),
        )
        outcomes = await session.scalar(
            select(func.count()).select_from(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_C3,
                PaperShadowEvent.event_type == "session_outcome",
            )
        )
        quality = await session.scalar(
            select(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_C3,
                PaperShadowEvent.event_type == "outcome_blocked",
            )
        )

    assert finalized["outcomes_added"] == 0
    assert finalized["quality_blocks_added"] == 1
    assert outcomes == 0
    assert quality is not None
    quality_snapshot = json.loads(quality.snapshot_json)["prior_structure"]
    assert quality_snapshot["stored_tencent_close_count"] == 2
    assert quality_snapshot["fresh_tencent_close_count"] == 0


@pytest.mark.asyncio
async def test_first_board_unconfirmed_controls_require_full_session_observation(
    shadow_env,
    monkeypatch,
):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE",
        "2026-09-04",
    )
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    SessionLocal = shadow_env
    async with SessionLocal() as session:
        await _seed_first_board_denominator(session)
        await _scan_three_confirmation_frames(
            session,
            [
                _first_board_quote("600201", confirms=True),
                _first_board_quote("600202", confirms=False),
            ],
            datetime(2026, 9, 4, 10, 0),
        )
        result = await finalize_first_board_shadow_sessions(
            session,
            as_of_date=date(2026, 9, 4),
            now=datetime(2026, 9, 4, 20, 35),
        )
        controls = await session.scalar(
            select(func.count()).select_from(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_C3,
                PaperShadowEvent.event_type == "control",
            )
        )
        blocked = await session.scalar(
            select(PaperShadowEvent).where(
                PaperShadowEvent.route_id == ROUTE_C3,
                PaperShadowEvent.event_type == "session_blocked",
            )
        )
        session.add_all(
            [
                StockKline(
                    code="600201",
                    trade_date=date(2026, 9, 4),
                    open=10.1,
                    high=11.0,
                    low=10.0,
                    close=11.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=2_000_000,
                    source="ths",
                ),
                StockKline(
                    code="600201",
                    trade_date=date(2026, 9, 7),
                    open=11.1,
                    high=11.3,
                    low=10.9,
                    close=11.2,
                    prev_close=11.0,
                    change_pct=1.8182,
                    volume=1_500_000,
                    source="ths",
                ),
            ]
        )
        await session.commit()
        settled = await settle_strategy_iteration_shadow(
            session,
            as_of_date=date(2026, 9, 7),
        )

    assert result["controls_added"] == 0
    assert result["quality_blocks_added"] == 1
    assert controls == 0
    assert blocked is not None
    assert settled == {"signals": 0, "evaluations_added": 0}
    health = json.loads(blocked.snapshot_json)["prior_structure"][
        "session_observation_health"
    ]
    assert health["complete"] is False
    assert health["checks"]["started_on_time"] is False
    assert health["checks"]["covered_route_end"] is False
