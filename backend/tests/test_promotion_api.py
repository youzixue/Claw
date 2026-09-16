from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import promotion, tenbagger
from app.db.session import Base, get_db
from auction_test_evidence import verified_auction_fields
from app.models import stock as stock_models  # noqa: F401
from app.models.news import FinanceNews
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.signal import PromotionPredictionRecord
from app.models.governance import TradeCalendarModel
from app.models.stock import AuctionData, FundFlow, LimitUpPool, SectorInfo, SectorPersistence, StockKline, StockSectorMapping, StockSpot, StockTag


def test_first_board_diagnostic_overview_uses_actual_largest_reason_group():
    diagnostics = {
        "000001": {"primary_reason": "缺少首波记忆", "memory_score": 10},
        "000002": {"primary_reason": "量能不够", "volume_ratio": 0.8},
        "000003": {"primary_reason": "量能不够", "volume_ratio": 0.9},
        "000004": {"primary_reason": "量能不够", "volume_ratio": 1.0},
    }

    payload = promotion._build_first_board_diagnostics_payload(diagnostics)

    assert payload["reason_summary"][0]["reason"] == "量能不够"
    assert payload["reason_summary"][0]["count"] == 3
    assert payload["overview"]["dominant_reason"] == "量能不够"


@pytest_asyncio.fixture
async def promotion_api_env(tmp_path: Path):
    db_path = tmp_path / "promotion_api.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(promotion.router, prefix="/api/v1/promotion")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SessionLocal, client

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.fixture(autouse=True)
def disable_live_dragon_tiger_lookup(monkeypatch):
    async def fake_load_dragon_tiger_context_map(trade_date, codes):
        return {}

    monkeypatch.setattr(promotion, "_load_dragon_tiger_context_map", fake_load_dragon_tiger_context_map)


async def _seed_limit_up_pool(session: AsyncSession):
    session.add_all(
        [
            LimitUpPool(
                code="000001",
                name="测试A",
                trade_date=date(2026, 4, 22),
                consecutive_days=1,
                seal_amount=120_000_000,
                break_count=0,
                turnover=5.2,
                source="test",
            ),
            LimitUpPool(
                code="000002",
                name="测试B",
                trade_date=date(2026, 4, 22),
                consecutive_days=2,
                seal_amount=230_000_000,
                break_count=0,
                turnover=8.1,
                source="test",
            ),
            LimitUpPool(
                code="000003",
                name="测试C",
                trade_date=date(2026, 4, 22),
                consecutive_days=1,
                seal_amount=90_000_000,
                break_count=1,
                turnover=14.5,
                source="test",
            ),
            LimitUpPool(
                code="000001",
                name="测试A",
                trade_date=date(2026, 4, 23),
                consecutive_days=2,
                seal_amount=150_000_000,
                break_count=0,
                turnover=6.3,
                source="test",
            ),
            LimitUpPool(
                code="000002",
                name="测试B",
                trade_date=date(2026, 4, 23),
                consecutive_days=3,
                seal_amount=250_000_000,
                break_count=0,
                turnover=9.0,
                source="test",
            ),
            LimitUpPool(
                code="000004",
                name="测试D",
                trade_date=date(2026, 4, 23),
                consecutive_days=1,
                seal_amount=80_000_000,
                break_count=1,
                turnover=18.0,
                source="test",
            ),
        ]
    )
    await session.commit()


async def _seed_candidate_context(session: AsyncSession):
    # 行情/形态fixture另提供证券身份，不通过缺失tag默认放行。
    session.add(StockTag(code="000010", name="冲板股", board_type="main_sz", board_tag="tradeable"))
    session.add_all(
        [
            SectorInfo(
                sector_code="S1",
                sector_name="AI应用",
                sector_type="concept",
                source="test",
                is_excluded=0,
            ),
            SectorInfo(
                sector_code="S_GENERIC",
                sector_name="沪股通",
                sector_type="concept",
                source="test",
                is_excluded=1,
            ),
            SectorPersistence(
                sector_code="S1",
                sector_name="AI应用",
                trade_date=date(2026, 4, 23),
                consecutive_days=2,
                limit_up_count=4,
                fund_flow=6.5,
                change_pct=3.1,
                strength_score=82,
            ),
            StockSectorMapping(
                code="000001",
                sector_code="S1",
                sector_name="AI应用",
                sector_type="concept",
                source="test",
            ),
            StockSectorMapping(
                code="000010",
                sector_code="S1",
                sector_name="AI应用",
                sector_type="concept",
                source="test",
            ),
            StockSectorMapping(
                code="000010",
                sector_code="S_GENERIC",
                sector_name="沪股通",
                sector_type="concept",
                source="test",
            ),
            StockSpot(
                code="000001",
                name="测试A",
                price=10.5,
                prev_close=10.0,
                open=10.1,
                high=10.5,
                low=9.9,
                limit_up=11.0,
                change_pct=5.0,
                volume=100000,
                amount=10000000,
                turnover=5.2,
                volume_ratio=1.8,
                main_net_inflow=50_000_000,
                support_strength_score=72,
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 14),
                open=9.70,
                close=9.84,
                high=9.95,
                low=9.62,
                volume=180000,
                amount=1_800_000,
                turnover=4.2,
                change_pct=10.0,
                prev_close=8.95,
                source="ths",
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 15),
                open=9.82,
                close=9.90,
                high=10.02,
                low=9.76,
                volume=176000,
                amount=1_936_000,
                turnover=5.1,
                change_pct=10.0,
                prev_close=9.00,
                source="ths",
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 16),
                open=9.88,
                close=9.94,
                high=10.08,
                low=9.82,
                volume=170000,
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 17),
                open=9.95,
                close=9.92,
                high=10.10,
                low=9.86,
                volume=160000,
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 20),
                open=9.91,
                close=9.96,
                high=10.11,
                low=9.88,
                volume=148000,
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 21),
                open=9.95,
                close=9.98,
                high=10.09,
                low=9.90,
                volume=118000,
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 22),
                open=9.99,
                close=10.01,
                high=10.13,
                low=9.92,
                volume=96000,
            ),
            StockKline(
                code="000010",
                trade_date=date(2026, 4, 23),
                open=10.00,
                close=10.02,
                high=10.16,
                low=9.93,
                volume=82000,
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_promotion_ladder_uses_latest_limit_up_pool_data(promotion_api_env):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)

    response = await client.get("/api/v1/promotion/ladder")
    assert response.status_code == 200
    payload = response.json()

    assert payload["trade_date"] == "2026-04-23"
    assert [item["consecutive_days"] for item in payload["ladder"]] == [3, 2, 1]
    assert [item["count"] for item in payload["ladder"]] == [1, 1, 1]
    assert payload["ladder"][0]["stocks"][0]["code"] == "000002"
    assert payload["ladder"][0]["seal_rate"] == 100.0


@pytest.mark.asyncio
async def test_latest_limit_up_trade_date_skips_official_closed_day(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="600910",
                    name="有效交易日",
                    trade_date=date(2026, 6, 18),
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600911",
                    name="休市脏数据",
                    trade_date=date(2026, 6, 19),
                    consecutive_days=1,
                    source="test",
                ),
            ]
        )
        await session.commit()
        latest = await promotion._get_latest_limit_up_trade_date(session)

    assert await promotion.trade_calendar.is_trade_day(date(2026, 6, 19)) is False
    assert latest == date(2026, 6, 18)


@pytest.mark.asyncio
async def test_load_filtered_limit_ups_normalizes_stale_consecutive_days(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="600900",
                    name="跨日连板",
                    trade_date=date(2026, 4, 22),
                    consecutive_days=1,
                    seal_amount=120_000_000,
                    break_count=0,
                    turnover=5.2,
                    source="test",
                ),
                LimitUpPool(
                    code="600900",
                    name="跨日连板",
                    trade_date=date(2026, 4, 23),
                    consecutive_days=1,
                    seal_amount=180_000_000,
                    break_count=0,
                    turnover=6.1,
                    source="test",
                ),
                LimitUpPool(
                    code="600901",
                    name="真实首板",
                    trade_date=date(2026, 4, 23),
                    consecutive_days=1,
                    seal_amount=80_000_000,
                    break_count=0,
                    turnover=4.2,
                    source="test",
                ),
            ]
        )
        await session.commit()
        rows = await promotion._load_filtered_limit_ups(session, date(2026, 4, 23))

    by_code = {item["code"]: item for item in rows}
    assert by_code["600900"]["consecutive_days"] == 2
    assert by_code["600901"]["consecutive_days"] == 1


@pytest.mark.asyncio
async def test_board_height_returns_metrics_from_current_and_previous_trade_dates(promotion_api_env):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)

    response = await client.get("/api/v1/promotion/board-height")
    assert response.status_code == 200
    payload = response.json()

    assert payload["trade_date"] == "2026-04-23"
    assert payload["previous_trade_date"] == "2026-04-22"
    assert payload["height"] == 3
    assert payload["leader"]["code"] == "000002"
    assert payload["limit_up_count"] == 3
    assert payload["seal_rate"] == pytest.approx(66.7, abs=0.05)
    assert payload["promoted_count"] == 2
    assert payload["promotion_rate"] == pytest.approx(2 / 3, abs=1e-4)


@pytest.mark.asyncio
async def test_promotion_probability_target_2_uses_latest_first_board_pool(promotion_api_env):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)

    response = await client.get("/api/v1/promotion/000004/probability")
    assert response.status_code == 200
    payload = response.json()

    assert payload["trade_date"] == "2026-04-23"
    assert payload["current_days"] == 1
    assert payload["next_days"] == 2
    assert payload["probability"] > 0
    assert payload["confidence"] > 0
    assert payload["confidence_label"] in {"高", "中", "低"}
    assert payload["confidence_level"] in {"high", "medium", "low"}
    assert payload["main_probability_name"] == "次日晋级二板概率"
    assert payload["candidate_route"] == "second_board_promotion"

    non_first_board = await client.get("/api/v1/promotion/000002/probability")
    assert non_first_board.status_code == 200
    non_first_payload = non_first_board.json()
    assert non_first_payload["trade_date"] == "2026-04-23"
    assert non_first_payload["current_days"] == 0
    assert non_first_payload["next_days"] == 2
    assert non_first_payload["probability"] == 0
    assert non_first_payload["factors"]["note"] == "当前不在最新交易日首板池，暂无次日晋级二板样本"


@pytest.mark.asyncio
async def test_promotion_probability_target_2_respects_stock_tagger_filter(promotion_api_env):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        session.add(
            StockTag(
                code="000004",
                name="测试D",
                board_type="main_sz",
                board_tag="blocked",
                is_st=True,
                is_suspended=False,
            )
        )
        await session.commit()

    response = await client.get("/api/v1/promotion/000004/probability")
    assert response.status_code == 200
    payload = response.json()

    assert payload["trade_date"] == "2026-04-23"
    assert payload["probability"] == 0
    assert payload["factors"]["note"] == "当前不在最新交易日首板池，暂无次日晋级二板样本"


def test_second_board_relay_gate_requires_tradeable_low_position_first_board():
    item = {
        "turnover": 6.0,
        "break_count": 0,
    }
    factors = {
        "limit_up_minutes": 9 * 60 + 25,
        "is_clean_early_hard_board": True,
        "main_net_inflow": 80_000_000,
        "main_net_inflow_pct": 8.0,
        "reason_cohort_count": 3,
        "reason_strong_cluster": True,
        "sector_morning_strengthening": True,
        "second_board_route": "cluster_second_board",
        "market_risk_level": "neutral",
    }
    position = {
        "preboard_low_position": True,
        "preboard_return_20d": 8.0,
        "preboard_position_60": 0.45,
        "preboard_recent_board_count": 0,
    }

    ready = promotion._build_second_board_relay_gate(
        item=item,
        factors=factors,
        sector={"limit_up_count": 3},
        position=position,
    )
    one_word = promotion._build_second_board_relay_gate(
        item=item,
        factors={**factors, "is_one_word_shape": True},
        sector={"limit_up_count": 3},
        position=position,
    )

    assert ready["relay_pool_ready"] is True
    assert ready["relay_prediction_ready"] is True
    assert ready["relay_quality_score"] >= 68
    assert ready["relay_learning_bucket"] == "T2:cluster_second_board"
    assert one_word["relay_pool_ready"] is False
    assert one_word["relay_prediction_ready"] is True
    assert one_word["relay_prediction_only"] is True
    assert any("一字" in reason for reason in one_word["relay_blockers"])


def test_board_memory_reset_is_recall_seed_not_direct_trade_signal():
    bars = []
    price = 10.0
    for index in range(80):
        change_pct = 9.8 if index == 68 else (-0.35 if index > 68 else 0.05)
        if index == 68:
            price *= 1.098
        elif index > 68:
            price *= 0.9965
        else:
            price *= 1.0005
        bars.append({
            "trade_date": date(2026, 1, 1) + timedelta(days=index),
            "open": price * 0.998,
            "close": price,
            "high": price * (1.005 if index != 68 else 1.0),
            "low": price * 0.993,
            "volume": 220_000 if index == 68 else 85_000,
            "change_pct": change_pct,
        })

    result = promotion._score_board_memory_reset_pattern(
        bars,
        code="600001",
        name="回撤样本",
    )

    assert result["board_memory_reset_ready"] is True
    assert result["board_memory_reset_score"] > 0
    assert 3 <= result["board_memory_days_since_limit_up"] <= 45
    assert result["board_memory_high_20_gap"] <= -2.0


def test_low_base_rotation_is_first_wave_recall_seed_without_recent_limit_up():
    bars = []
    price = 12.0
    for index in range(80):
        if index < 60:
            next_price = 12.0 - index * 0.04
        elif index < 70:
            next_price = price * 1.005
        elif index == 70:
            next_price = price * 1.045
        else:
            next_price = price * 0.988
        change_pct = (next_price / price - 1.0) * 100 if index else 0.0
        price = next_price
        bars.append({
            "trade_date": date(2026, 1, 1) + timedelta(days=index),
            "open": price * 0.998,
            "close": price,
            "high": price * 1.008,
            "low": price * 0.992,
            "volume": 180_000 if index == 70 else 82_000,
            "change_pct": change_pct,
        })

    result = promotion._score_low_base_rotation_pattern(
        bars,
        code="600001",
        name="低位首波样本",
    )

    assert result["low_base_rotation_ready"] is True
    assert result["low_base_rotation_score"] >= promotion.PRE_BOARD_PROBE_MIN_SCORE
    assert result["low_base_position_120"] <= 0.42
    assert result["low_base_high_20_gap"] <= -3.0


@pytest.mark.asyncio
async def test_fund_flow_trend_uses_only_anchor_date_and_three_day_persistence(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    async with SessionLocal() as session:
        session.add_all(
            [
                FundFlow(
                    code="600880",
                    name="资金样本",
                    trade_date=date(2026, 8, 24) + timedelta(days=index),
                    main_net_inflow=value * 1_000_000,
                    main_net_inflow_pct=value,
                )
                for index, value in enumerate([-4.0, 1.5, 2.0, 2.5, 30.0])
            ]
        )
        await session.commit()
        context = await promotion._load_fund_flow_trend_map(
            session,
            ["600880"],
            date(2026, 8, 27),
        )

    item = context["600880"]
    assert item["funding_as_of_trade_date"] == "2026-08-27"
    assert item["funding_main_inflow_pct_3d"] == pytest.approx(6.0)
    assert item["funding_positive_days_3d"] == 3
    assert item["funding_preheat_ready"] is True


@pytest.mark.asyncio
async def test_primary_industry_context_is_independent_from_hot_concept(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    async with SessionLocal() as session:
        session.add_all(
            [
                SectorInfo(
                    sector_code="I_PRIMARY",
                    sector_name="主营设备",
                    sector_type="industry",
                    source="test",
                    stock_count=80,
                ),
                SectorInfo(
                    sector_code="C_HOT",
                    sector_name="泛热门概念",
                    sector_type="concept",
                    source="test",
                    stock_count=500,
                ),
                StockSectorMapping(
                    code="600881",
                    sector_code="I_PRIMARY",
                    sector_name="主营设备",
                    sector_type="industry",
                    source="test",
                ),
                StockSectorMapping(
                    code="600881",
                    sector_code="C_HOT",
                    sector_name="泛热门概念",
                    sector_type="concept",
                    source="test",
                ),
                SectorPersistence(
                    sector_code="I_PRIMARY",
                    sector_name="主营设备",
                    trade_date=date(2026, 8, 27),
                    strength_score=62,
                    limit_up_count=3,
                    fund_flow=4.0,
                    change_pct=2.0,
                    consecutive_days=2,
                ),
                SectorPersistence(
                    sector_code="C_HOT",
                    sector_name="泛热门概念",
                    trade_date=date(2026, 8, 27),
                    strength_score=90,
                    limit_up_count=12,
                    fund_flow=20.0,
                    change_pct=5.0,
                    consecutive_days=5,
                ),
            ]
        )
        await session.commit()
        context = await promotion._load_best_sector_context(
            session,
            date(2026, 8, 27),
            ["600881"],
            allowed_sector_types={"industry"},
            use_live_limit_up_context=False,
        )

    assert context["600881"]["sector_code"] == "I_PRIMARY"
    assert context["600881"]["sector_type"] == "industry"


def test_promotion_model_version_fits_persisted_column():
    assert len(promotion.PROMOTION_MODEL_VERSION) <= 40


def test_launch_precursor_adjusts_direction_probability_not_execution_gate():
    base = {
        "code": "600882",
        "target_board": 1,
        "candidate_route": "mainline_spread_start",
        "probability": 0.10,
        "trade_ready": False,
        "probability_factors": {
            "launch_direction_logit_delta": 0.08,
            "launch_strong_rise_logit_delta": 0.30,
            "launch_probability_targets_split": True,
            "low_base_sector_ignition_ready": True,
            "low_base_sector_ignition_prediction_only": True,
        },
        "sub_probabilities": {},
    }
    adjusted = promotion._apply_promotion_learning_to_candidates([base], {})[0]

    assert adjusted["direction_probability"] > 0.50
    assert adjusted["strong_rise_probability"] > 0.12
    assert adjusted["route_calibrated_probability"] == pytest.approx(0.10)
    assert adjusted["limit_up_probability"] == promotion._clamp_probability(
        adjusted["temporal_event_probability"] * 0.75 + 0.10 * 0.25
    )
    assert adjusted["trade_ready"] is False
    assert adjusted["probability_factors"]["low_base_sector_ignition_prediction_only"] is True
    assert adjusted["probability_factors"]["learning_direction_precursor_delta_applied"] == 0.08
    assert adjusted["probability_factors"]["learning_strong_rise_precursor_delta_applied"] == 0.30


def test_negative_precursor_evidence_can_reduce_direction_without_changing_limit_up_gate():
    base = {
        "code": "600883",
        "target_board": 1,
        "candidate_route": "mainline_spread_start",
        "probability": 0.10,
        "trade_ready": False,
        "probability_factors": {
            "launch_direction_logit_delta": -0.10,
            "launch_strong_rise_logit_delta": -0.15,
            "funding_persistent_outflow_risk": True,
        },
        "sub_probabilities": {},
    }

    adjusted = promotion._apply_promotion_learning_to_candidates([base], {})[0]

    assert adjusted["direction_probability"] < 0.50
    assert adjusted["strong_rise_probability"] < 0.10
    assert adjusted["route_calibrated_probability"] == pytest.approx(0.10)
    assert adjusted["limit_up_probability"] == promotion._clamp_probability(
        adjusted["temporal_event_probability"] * 0.75 + 0.10 * 0.25
    )
    assert adjusted["trade_ready"] is False


def test_launch_precursor_gets_own_context_calibration_bucket():
    bucket = promotion._promotion_context_learning_bucket(
        "T1:mainline_spread_start",
        {
            "low_base_sector_ignition_ready": True,
            "low_base_sector_ignition_confirmed": True,
        },
    )
    assert bucket.endswith("|state:low_base_industry_funding")


def test_direction_research_annotation_preserves_champion_and_blocks_recordability_gap(monkeypatch):
    monkeypatch.setattr(promotion, "_is_recordable_prediction_candidate", lambda item: True)
    candidates = [
        {"code": "600001", "target_board": 1, "direction_probability": .3,
         "prediction_rank_eligible": True, "probability": .9, "probability_factors": {}},
        {"code": "600002", "target_board": 1, "direction_probability": .8,
         "prediction_rank_eligible": True, "probability": .1, "probability_factors": {}},
    ]
    annotated = promotion._annotate_prediction_record_metadata(
        candidates, candidates[:1], ranked_limit=1, rank_eligible_candidates=candidates,
        snapshot_source="schedule", snapshot_context="promotion_2000",
    )
    assert [item["probability"] for item in annotated] == [.9, .1]
    assert annotated[0]["probability_factors"]["prediction_ranked_selected"] is True
    assert annotated[1]["probability_factors"]["prediction_ranked_selected"] is False
    assert annotated[1]["probability_factors"]["direction_research"]["rank_position"] == 1
    assert "direction_research" not in candidates[0]["probability_factors"]
    payload = promotion._direction_research_payload(annotated)
    assert payload["candidates"][0]["code"] == "600002"
    assert payload["research_only"] is True
    assert payload["frozen"] is False

    incomplete = promotion._annotate_prediction_record_metadata(
        candidates[:1], candidates[:1], ranked_limit=1, rank_eligible_candidates=candidates,
        snapshot_source="schedule", snapshot_context="promotion_2000",
    )
    proof = incomplete[0]["probability_factors"]["direction_research"]
    assert proof["rank_contract_complete"] is False
    assert proof["error"] == "eligible_candidates_not_recordable"
    assert promotion._direction_research_payload(incomplete)["status"] == "blocked"
    assert promotion._direction_research_payload(incomplete)["candidates"] == []


def test_direction_research_blocks_noneligible_candidate_dropped_after_annotation(monkeypatch):
    def recordable(item):
        # Initially recordable; enrichment introduces metadata that causes
        # the persistence callback's second check to reject a noneligible row.
        return not (item["code"] == "600002" and
                    "prediction_rank_contract_complete" in item.get("probability_factors", {}))
    monkeypatch.setattr(promotion, "_is_recordable_prediction_candidate", recordable)
    candidates = [
        {"code": "600001", "target_board": 1, "direction_probability": .8,
         "prediction_rank_eligible": True, "probability_factors": {}},
        {"code": "600002", "target_board": 1, "direction_probability": .2,
         "probability_factors": {}},
    ]
    assert all(recordable(item) for item in candidates)
    annotated = promotion._annotate_prediction_record_metadata(
        candidates, candidates[:1], ranked_limit=1,
        rank_eligible_candidates=candidates[:1],
        snapshot_source="schedule", snapshot_context="promotion_2000",
    )
    for item in annotated:
        proof = item["probability_factors"]["direction_research"]
        assert proof["candidate_count"] == 2
        assert proof["rank_contract_complete"] is False
        assert proof["selected"] is False
        assert proof["error"] == "direction_candidates_not_recordable"
        assert proof["missing_recordable_count"] == 1
    payload = promotion._direction_research_payload(annotated)
    assert payload["status"] == "blocked"
    assert payload["candidates"] == []
    assert payload["frozen"] is False


def test_direction_research_does_not_backfill_unannotated_old_records():
    candidates = [{"code": "600001", "target_board": 1, "direction_probability": .8,
                   "probability_factors": {"prediction_rank_eligible": True}}]
    assert promotion._direction_research_payload(candidates)["status"] == "unavailable"
    assert "direction_research" not in candidates[0]["probability_factors"]


def test_launch_precursor_review_metrics_separate_prediction_from_actionability():
    def record(code: str, factors: dict) -> PromotionPredictionRecord:
        return PromotionPredictionRecord(
            code=code,
            target_board=1,
            prediction_trade_date=date(2026, 8, 27),
            candidate_route="mainline_spread_start",
            factors_json=promotion._json_dumps_safe(factors),
        )

    metrics = promotion._build_promotion_launch_precursor_metrics(
        [
            record(
                "600901",
                {
                    "launch_profile_ready": True,
                    "funding_preheat_ready": True,
                    "primary_industry_ignition_ready": True,
                    "low_base_sector_ignition_ready": True,
                    "prediction_actionable": False,
                },
            ),
            record(
                "600902",
                {
                    "launch_profile_ready": True,
                    "prediction_actionable": True,
                },
            ),
            record(
                "600903",
                {
                    "news_direct_high_impact": True,
                    "news_repeated_direct": True,
                    "prediction_actionable": True,
                },
            ),
            record(
                "600904",
                {
                    "funding_persistent_outflow_risk": True,
                    "prediction_actionable": False,
                },
            ),
        ],
        limit_up_codes={"600901"},
        rising_codes={"600901", "600902", "600904"},
        strong_rising_codes={"600901"},
        evaluable_codes={"600901", "600902", "600903", "600904"},
    )

    assert metrics["all_ranked_first_board"]["sample_count"] == 4
    assert metrics["all_ranked_first_board"]["limit_up_precision"] == 0.25
    assert metrics["all_ranked_first_board"]["directional_precision"] == 0.75
    assert metrics["low_base_industry_funding"]["sample_count"] == 1
    assert metrics["low_base_industry_funding"]["limit_up_precision"] == 1.0
    assert metrics["low_base_industry_funding"]["actionable_count"] == 0
    assert metrics["low_base_industry_funding"]["forecast_only_count"] == 1
    assert metrics["launch_profile"]["actionable_limit_up_precision"] == 0.0
    assert metrics["direct_high_impact_news"]["sample_count"] == 1
    assert metrics["persistent_funding_outflow"]["directional_precision"] == 1.0

    aggregate = promotion._aggregate_promotion_launch_precursor_metrics(
        [{"launch_precursor_metrics": metrics}, {"launch_precursor_metrics": metrics}]
    )
    assert aggregate["all_ranked_first_board"]["sample_count"] == 8
    assert aggregate["low_base_industry_funding"]["limit_up_precision"] == 1.0


@pytest.mark.asyncio
async def test_schedule_news_cutoff_is_pinned_to_snapshot_context(monkeypatch):
    trade_date = date(2026, 8, 28)

    async def next_trade_day(_trade_date):
        return date(2026, 8, 31)

    monkeypatch.setattr(promotion.trade_calendar, "next_trade_day", next_trade_day)

    close_1510 = await promotion._resolve_promotion_snapshot_news_end_time(
        trade_date,
        snapshot_source="schedule",
        snapshot_context="promotion_1510",
        now=datetime(2026, 8, 28, 21, 45),
    )
    close_2000 = await promotion._resolve_promotion_snapshot_news_end_time(
        trade_date,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
        now=datetime(2026, 8, 28, 21, 45),
    )
    preopen_0925 = await promotion._resolve_promotion_snapshot_news_end_time(
        trade_date,
        snapshot_source="schedule",
        snapshot_context="promotion_0925",
        now=datetime(2026, 8, 31, 9, 40),
    )
    preopen_0935_before_clock = await promotion._resolve_promotion_snapshot_news_end_time(
        trade_date,
        snapshot_source="schedule",
        snapshot_context="promotion_0935",
        now=datetime(2026, 8, 31, 9, 30),
    )
    mainline_1030 = await promotion._resolve_promotion_snapshot_news_end_time(
        date(2026, 8, 31),
        snapshot_source="schedule",
        snapshot_context="promotion_1030",
        now=datetime(2026, 8, 31, 10, 44),
    )
    page_now = await promotion._resolve_promotion_snapshot_news_end_time(
        trade_date,
        snapshot_source="page",
        snapshot_context="page",
        now=datetime(2026, 8, 28, 21, 45),
    )

    assert close_1510 == datetime(2026, 8, 28, 15, 10)
    assert close_2000 == datetime(2026, 8, 28, 20, 0)
    assert preopen_0925 == datetime(2026, 8, 31, 9, 25)
    assert preopen_0935_before_clock == datetime(2026, 8, 31, 9, 30)
    assert mainline_1030 == datetime(2026, 8, 31, 10, 30)
    assert page_now == datetime(2026, 8, 28, 21, 45)


@pytest.mark.asyncio
async def test_public_candidates_endpoint_cannot_forge_schedule_snapshot(monkeypatch):
    captured = {}

    async def fake_builder(**kwargs):
        captured.update(kwargs)
        return {"prediction_snapshot_source": kwargs["snapshot_source"]}

    monkeypatch.setattr(promotion, "build_promotion_candidates", fake_builder)
    payload = await promotion.promotion_candidates(
        limit=12,
        ranked_limit=30,
        force_refresh=True,
        compact=True,
        db=object(),
    )

    assert payload["prediction_snapshot_source"] == "page"
    assert captured["snapshot_source"] == "page"
    assert captured["snapshot_context"] == "page"
    assert "snapshot_source" not in __import__("inspect").signature(
        promotion.promotion_candidates
    ).parameters


@pytest.mark.asyncio
async def test_official_snapshot_clock_rejects_wrong_declared_session(monkeypatch):
    async def next_trade_day(trade_date):
        return {
            date(2026, 8, 27): date(2026, 8, 28),
            date(2026, 8, 28): date(2026, 8, 31),
        }[trade_date]

    monkeypatch.setattr(promotion.trade_calendar, "next_trade_day", next_trade_day)
    await promotion._validate_official_snapshot_clock(
        "promotion_2000",
        datetime(2026, 8, 28, 20, 0),
        {date(2026, 8, 28)},
    )
    await promotion._validate_official_snapshot_clock(
        "promotion_0925",
        datetime(2026, 8, 31, 9, 25),
        {date(2026, 8, 28)},
    )
    await promotion._validate_official_snapshot_clock(
        "promotion_0935",
        datetime(2026, 8, 31, 9, 35),
        {date(2026, 8, 28), date(2026, 8, 31)},
    )
    await promotion._validate_official_snapshot_clock(
        "promotion_1030",
        datetime(2026, 8, 31, 10, 30),
        {date(2026, 8, 28), date(2026, 8, 31)},
    )
    await promotion._validate_official_snapshot_clock(
        "promotion_1305",
        datetime(2026, 8, 31, 13, 5),
        {date(2026, 8, 28), date(2026, 8, 31)},
    )
    # 重启补跑可能迟到到 09:34，仍按 09:25 竞价语义快照落库。
    await promotion._validate_official_snapshot_clock(
        "promotion_0925",
        datetime(2026, 8, 31, 9, 34),
        {date(2026, 8, 28), date(2026, 8, 31)},
    )
    with pytest.raises(ValueError, match="does not match"):
        await promotion._validate_official_snapshot_clock(
            "promotion_0925",
            datetime(2026, 8, 31, 9, 25),
            {date(2026, 8, 27)},
        )
    with pytest.raises(ValueError, match="must run within"):
        await promotion._validate_official_snapshot_clock(
            "promotion_0925",
            datetime(2026, 8, 31, 20, 0),
            {date(2026, 8, 28)},
        )
    with pytest.raises(ValueError, match="does not match"):
        await promotion._validate_official_snapshot_clock(
            "promotion_2000",
            datetime(2026, 8, 31, 20, 0),
            {date(2026, 8, 28)},
        )


@pytest.mark.asyncio
async def test_morning_second_board_uses_previous_close_even_when_live_pool_is_newer(monkeypatch):
    previous = date(2026, 8, 28)
    current = date(2026, 8, 31)

    async def previous_trade_day(trade_day):
        assert trade_day == current
        return previous

    async def latest_limit_up_trade_date(_db):
        # Simulate current-day limit-up rows already written at 09:35.
        return current

    monkeypatch.setattr(promotion.trade_calendar, "previous_trade_day", previous_trade_day)
    monkeypatch.setattr(promotion, "_get_latest_limit_up_trade_date", latest_limit_up_trade_date)

    morning_anchor = await promotion._resolve_second_board_source_trade_date(
        object(),
        snapshot_source="schedule",
        snapshot_context="promotion_0935",
        recorded_at=datetime(2026, 8, 31, 9, 35),
    )
    close_anchor = await promotion._resolve_second_board_source_trade_date(
        object(),
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
        recorded_at=datetime(2026, 8, 31, 20, 0),
    )

    assert morning_anchor == previous
    assert close_anchor == current


def test_morning_prediction_dates_use_current_signal_session_not_source_anchors():
    previous = date(2026, 8, 28)
    current = date(2026, 8, 31)

    morning_dates = promotion._prediction_trade_dates_by_target(
        first_board_source_trade_date=current,
        second_board_source_trade_date=previous,
        snapshot_source="schedule",
        snapshot_context="promotion_0935",
        recorded_at=datetime(2026, 8, 31, 9, 35),
    )
    close_dates = promotion._prediction_trade_dates_by_target(
        first_board_source_trade_date=current,
        second_board_source_trade_date=previous,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
        recorded_at=datetime(2026, 8, 31, 20, 0),
    )

    assert morning_dates == {1: current, 2: current}
    assert close_dates == {1: current, 2: previous}


@pytest.mark.asyncio
async def test_limit_up_date_resolvers_ignore_quarantined_only_dates(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, _client = promotion_api_env
    valid_date = date(2026, 4, 23)
    quarantined_date = date(2026, 4, 24)

    async def is_trade_day(_trade_day):
        return True

    monkeypatch.setattr(promotion.trade_calendar, "is_trade_day", is_trade_day)
    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="600001",
                    name="有效涨停",
                    trade_date=valid_date,
                    consecutive_days=1,
                ),
                LimitUpPool(
                    code="600002",
                    name="隔离测试行",
                    trade_date=quarantined_date,
                    consecutive_days=1,
                    quarantined=True,
                ),
            ]
        )
        await session.commit()

        latest = await promotion._get_latest_limit_up_trade_date(session)
        previous = await promotion._get_previous_limit_up_trade_date(
            session,
            date(2026, 4, 27),
        )

    assert latest == valid_date
    assert previous == valid_date


def test_second_board_sector_reason_relevance_prevents_unrelated_hot_concept():
    assert promotion._sector_reason_relevance("通信设备", "通信设备") == 4
    assert promotion._sector_reason_relevance("通信设备", "5G通信设备") >= 3
    assert promotion._sector_reason_relevance("通信设备", "智能医疗") == 0
    assert promotion._sector_reason_relevance("算力服务", "算力租赁") >= 2
    assert promotion._sector_reason_relevance("算力服务", "数字水印") == 0


def test_limit_up_reason_theme_mapping_uses_explicit_industry_words_only():
    optical = dict(promotion._limit_up_reason_theme_keys("硅光芯片+CPO+功能性器件"))
    medicine = dict(promotion._limit_up_reason_theme_keys("mRNA疫苗+创新药"))
    unrelated = dict(promotion._limit_up_reason_theme_keys("上海家化+日化用品+品牌消费"))

    assert optical["optical_compute"] == "光通信/算力"
    assert optical["semiconductor"] == "半导体"
    assert medicine["medicine_bio"] == "医药生物"
    assert "medicine_bio" not in unrelated


def test_fresh_direct_hard_news_is_kept_for_next_session_watch_only():
    assert promotion._is_fresh_direct_hard_news_context(
        {
            "news_mapping_mode": "direct_code",
            "news_event_grade": "hard",
            "news_fresh_after_trade_close": True,
            "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 12,
        }
    )
    assert not promotion._is_fresh_direct_hard_news_context(
        {
            "news_mapping_mode": "sector_inferred",
            "news_event_grade": "hard",
            "news_fresh_after_trade_close": True,
            "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 12,
        }
    )


def test_fresh_direct_hard_news_has_watch_display_priority():
    hard_news = {
        "code": "600366",
        "probability": 0.02,
        "probability_factors": {
            "news_mapping_mode": "direct_code",
            "news_event_grade": "hard",
            "news_fresh_after_trade_close": True,
            "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 12,
        },
    }
    ordinary = {"code": "600367", "probability": 0.30}

    assert promotion._first_board_display_priority(hard_news) > promotion._first_board_display_priority(ordinary)


def test_dedupe_keeps_fresh_direct_hard_news_context_for_same_stock():
    hard_news = {
        "code": "600366",
        "target_board": 1,
        "candidate_route": "news_catalyst_start",
        "probability": 0.02,
        "trade_ready": False,
        "keep_in_diagnostics": True,
        "probability_factors": {
            "news_mapping_mode": "direct_code",
            "news_event_grade": "hard",
            "news_fresh_after_trade_close": True,
            "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 12,
        },
    }
    ordinary = {
        "code": "600366",
        "target_board": 1,
        "candidate_route": "quiet_setup",
        "probability": 0.30,
        "trade_ready": False,
        "keep_in_diagnostics": True,
    }

    result = promotion._dedupe_first_board_candidates([ordinary, hard_news])

    assert len(result) == 1
    assert result[0]["candidate_route"] == "news_catalyst_start"


def test_candidate_pool_truncation_preserves_fresh_direct_hard_news():
    hard_news = {
        "code": "600366",
        "candidate_route": "news_catalyst_start",
        "probability": 0.01,
        "trade_ready": False,
        "probability_factors": {
            "news_mapping_mode": "direct_code",
            "news_event_grade": "hard",
            "news_fresh_after_trade_close": True,
            "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 12,
        },
    }
    ordinary = {"code": "600367", "candidate_route": "quiet_setup", "probability": 0.80, "trade_ready": False}

    assert promotion._first_board_candidate_pool_priority(hard_news) > promotion._first_board_candidate_pool_priority(ordinary)


def test_news_catalyst_bull_row_does_not_reuse_weaker_other_route_score():
    spot = StockSpot(
        code="600366",
        name="宁波韵升",
        price=12.61,
        prev_close=12.64,
        change_pct=-0.24,
        volume_ratio=1.06,
        support_strength_score=50.0,
        main_net_inflow=0.0,
        amount=600_000_000,
    )
    result = promotion._build_news_catalyst_bull_row(
        spot,
        {"strength_score": 26.4},
        {
            "news_catalyst_score": 69.0,
            "news_title": "上半年净利润同比增长154.18%",
        },
        existing_row={"code": "600366", "total_score": 43.23, "level": "C", "top_signals": ["板块扩散反推"]},
    )

    assert result["total_score"] > 43.23
    assert "消息催化" in result["top_signals"]


def test_second_board_kline_shape_rewards_hard_board_and_penalizes_divergence():
    market_context = {
        "market_risk_level": "weak",
        "weak_follow_through_market": True,
        "board_height": 4,
        "broken_ratio": 0.48,
        "avg_break_count": 1.5,
        "weak_seal_market": True,
    }
    sector = {
        "strength_score": 72,
        "consecutive_days": 2,
        "limit_up_count": 4,
        "fund_flow": 3.0,
        "change_pct": 1.2,
    }

    hard_shape = promotion._build_second_board_kline_shape(
        {"open": 10.9, "high": 11.0, "low": 10.91, "close": 11.0, "prev_close": 10.0}
    )
    wide_shape = promotion._build_second_board_kline_shape(
        {"open": 9.9, "high": 11.0, "low": 9.75, "close": 11.0, "prev_close": 10.0}
    )

    hard_probability, hard_meta = promotion._adjust_second_board_probability(
        0.35,
        item={
            "seal_amount": 150_000_000,
            "break_count": 0,
            "turnover": 4.0,
            "limit_up_time": "093000",
        },
        sector=sector,
        market_context=market_context,
        kline_shape=hard_shape,
    )
    wide_probability, wide_meta = promotion._adjust_second_board_probability(
        0.35,
        item={
            "seal_amount": 150_000_000,
            "break_count": 0,
            "turnover": 9.0,
            "limit_up_time": "093000",
        },
        sector=sector,
        market_context=market_context,
        kline_shape=wide_shape,
    )

    assert hard_meta["limit_up_shape_label"] == "一字/准一字"
    assert hard_meta["shape_bonus"] > 0
    assert wide_meta["limit_up_shape_label"] == "宽幅分歧板"
    assert wide_meta["shape_penalty"] >= 0.1
    assert wide_probability < hard_probability - 0.08


def test_second_board_probability_rewards_low_rotation_and_penalizes_stale_theme():
    market_context = {
        "market_risk_level": "weak",
        "weak_follow_through_market": True,
        "board_height": 3,
        "broken_ratio": 0.42,
        "avg_break_count": 1.2,
        "weak_seal_market": True,
    }
    item = {
        "seal_amount": 180_000_000,
        "break_count": 0,
        "turnover": 5.2,
        "limit_up_time": "093800",
    }
    reason_context = {
        "reason": "电力",
        "reason_cohort_count": 2,
        "reason_avg_turnover": 5.5,
        "reason_avg_break_count": 0.5,
        "reason_early_seal_ratio": 0.5,
        "reason_avg_support_strength": 60,
        "reason_avg_seal_amount": 160_000_000,
        "reason_strong_cluster": True,
    }

    rotation_prob, rotation_meta = promotion._adjust_second_board_probability(
        0.18,
        item=item,
        sector={
            "strength_score": 52,
            "consecutive_days": 1,
            "limit_up_count": 4,
            "fund_flow": 3.2,
            "change_pct": 2.1,
            "sector_rotation_score": 72,
            "sector_strength_delta": 28,
            "sector_limit_up_delta": 3.5,
            "sector_low_position_rotation": True,
            "sector_rotation_label": "低位轮动抬升",
        },
        market_context=market_context,
        reason_context=reason_context,
        kline_shape={"is_hard_board_shape": True, "limit_up_low_gap_pct": 2.0, "limit_up_open_gap_pct": 2.0},
    )
    stale_prob, stale_meta = promotion._adjust_second_board_probability(
        0.18,
        item={**item, "turnover": 9.2},
        sector={
            "strength_score": 55,
            "consecutive_days": 4,
            "limit_up_count": 1,
            "fund_flow": -4.0,
            "change_pct": -2.4,
            "sector_rotation_score": 34,
            "sector_strength_delta": -22,
            "sector_limit_up_delta": -5,
            "sector_crowded_stale_theme": True,
            "sector_rotation_label": "高位拥挤退潮",
        },
        market_context=market_context,
        reason_context={**reason_context, "reason_strong_cluster": False, "reason_avg_break_count": 2.8},
        kline_shape={"is_wide_divergence_shape": True, "limit_up_low_gap_pct": 8.0, "limit_up_open_gap_pct": 7.0},
    )

    assert rotation_prob > stale_prob
    assert rotation_meta["rotation_bonus"] > 0
    assert rotation_meta["reason_strong_cluster"] is True
    assert stale_meta["rotation_penalty"] > 0
    assert stale_meta["sector_crowded_stale_theme"] is True


def test_second_board_hostile_market_keeps_low_rotation_cluster_route_alive():
    market_context = {
        "market_risk_level": "hostile",
        "weak_follow_through_market": True,
        "board_height": 4,
        "broken_ratio": 0.48,
        "avg_break_count": 1.3,
        "weak_seal_market": True,
        "first_board_count": 120,
    }
    probability, meta = promotion._adjust_second_board_probability(
        0.14,
        item={
            "seal_amount": 130_000_000,
            "break_count": 0,
            "turnover": 5.4,
            "limit_up_time": "094200",
        },
        sector={
            "strength_score": 54,
            "consecutive_days": 1,
            "limit_up_count": 5,
            "fund_flow": 2.6,
            "change_pct": 1.9,
            "sector_rotation_score": 74,
            "sector_strength_delta": 24,
            "sector_limit_up_delta": 4,
            "sector_low_position_rotation": True,
            "sector_rotation_label": "低位轮动抬升",
        },
        market_context=market_context,
        reason_context={
            "reason": "电力设备",
            "reason_cohort_count": 4,
            "reason_avg_turnover": 5.8,
            "reason_avg_break_count": 0.6,
            "reason_early_seal_ratio": 0.5,
            "reason_avg_support_strength": 62,
            "reason_strong_cluster": True,
        },
    )

    assert probability > 0.08
    assert meta["second_board_route"] == "low_rotation_cluster_second_board"
    assert meta["hostile_rotation_relief"] > 0
    assert meta["rotation_cluster_bonus"] > 0


def test_second_board_hostile_broad_diffusion_gets_conservative_floor():
    probability, meta = promotion._adjust_second_board_probability(
        0.01,
        item={
            "seal_amount": 42_000_000,
            "break_count": 6,
            "turnover": 8.4,
            "limit_up_time": "13:18:00",
        },
        sector={
            "strength_score": 44,
            "consecutive_days": 1,
            "limit_up_count": 3,
            "fund_flow": 1.2,
            "change_pct": 0.9,
            "sector_rotation_score": 48,
            "sector_strength_delta": 7.0,
            "sector_limit_up_delta": 2.0,
        },
        market_context={
            "market_risk_level": "hostile",
            "weak_follow_through_market": True,
            "broad_first_board_overflow": True,
            "first_board_count": 90,
            "broken_ratio": 0.43,
            "avg_break_count": 1.4,
            "weak_seal_market": True,
        },
        spot=StockSpot(code="000088", support_strength_score=56, circ_market_cap=120),
        reason_context={
            "reason": "低位轮动",
            "reason_cohort_count": 3,
            "reason_avg_turnover": 9.2,
            "reason_avg_break_count": 2.4,
            "reason_early_seal_ratio": 0.2,
            "reason_avg_support_strength": 55,
            "reason_strong_cluster": False,
        },
    )

    assert probability >= 0.055
    assert meta["second_board_route"] == "diffusion_second_board"
    assert meta["diffusion_second_board"] is True
    assert meta["hostile_rotation_relief"] > 0


def test_contextual_first_board_bull_row_falls_back_when_bull_rank_missing():
    spot = StockSpot(
        code="000777",
        name="路线触发",
        price=10.8,
        prev_close=10.0,
        open=10.1,
        high=10.9,
        low=10.0,
        change_pct=8.0,
        volume_ratio=1.8,
        amount=120_000_000,
        turnover=5.2,
        main_net_inflow=28_000_000,
        support_strength_score=68,
    )
    bull_row = promotion._build_contextual_first_board_bull_row(
        row={"event_types": ["news_catalyst", "breakthrough"]},
        spot=spot,
        sector={"strength_score": 62},
        kline_context={"confirmation_score": 0.48},
        news_context={
            "news_catalyst_score": 88,
            "news_title": "产业链超预期催化",
        },
    )

    assert bull_row["code"] == "000777"
    assert bull_row["total_score"] > 55
    assert "消息催化" in bull_row["top_signals"]


def test_select_spot_for_trade_date_keeps_historical_hot_mainline_spot():
    current_spot = StockSpot(
        code="002409",
        name="雅克科技",
        change_pct=10.0,
        updated_at=datetime(2026, 6, 10, 13, 0),
    )
    fallback_spot = StockSpot(
        code="002409",
        name="雅克科技",
        change_pct=5.48,
        updated_at=datetime(2026, 6, 9, 0, 0),
    )

    selected = promotion._select_spot_for_trade_date(
        current_spot,
        fallback_spot,
        date(2026, 6, 9),
    )

    assert selected is fallback_spot


def test_select_spot_for_trade_date_uses_current_when_same_trade_day():
    current_spot = StockSpot(
        code="002409",
        name="雅克科技",
        change_pct=5.48,
        updated_at=datetime(2026, 6, 9, 14, 30),
    )
    fallback_spot = StockSpot(
        code="002409",
        name="雅克科技",
        change_pct=4.2,
        updated_at=datetime(2026, 6, 9, 0, 0),
    )

    selected = promotion._select_spot_for_trade_date(
        current_spot,
        fallback_spot,
        date(2026, 6, 9),
    )

    assert selected is current_spot


def test_broad_rotation_mainline_learning_uses_empirical_base_rate():
    adjusted = promotion._apply_promotion_learning_to_candidates(
        [
            {
                "code": "000829",
                "target_board": 1,
                "candidate_route": "mainline_spread_start",
                "probability": 0.085,
                "sub_probabilities": {"first_limitup_next_day": 0.085},
                "probability_factors": {
                    "market_broad_first_board_overflow": True,
                    "broad_rotation_member_setup": True,
                },
            }
        ],
        {
            "T1:mainline_spread_start": {
                "sample_count": 21,
                "success_count": 0,
                "success_rate": 0,
                "model_adjustment": -0.088,
            }
        },
    )[0]

    assert 0.024 <= adjusted["probability"] < 0.085
    assert adjusted["probability_factors"]["learning_calibration_method"] == promotion.PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
    assert adjusted["probability_factors"]["learning_probability_adjustment"] < 0
    assert adjusted["probability_factors"]["learning_raw_probability_adjustment"] == pytest.approx(-0.088)
    assert adjusted["probability_factors"]["learning_adjustment_capped"] is False


def test_enrich_first_board_row_keeps_hot_mainline_context_for_existing_snapshot_row():
    row = {
        "code": "000536",
        "name": "华映科技",
        "event_types": ["attention_only"],
        "detail": {"change_pct": 0.3, "volume_ratio": 0.6},
        "primary_reason": "异常快照基础行",
        "secondary_reason": "基础扫描",
    }
    hot_row = {
        "event_types": ["mainline_relay", "mainline_spread", "breakthrough"],
        "detail": {
            "change_pct": 0.9,
            "volume_ratio": 0.72,
            "broad_rotation_member_setup": True,
        },
        "driver_primary": "光学光电",
        "secondary_reason": "光学光电强度62 · 板块涨停6只",
    }

    enriched = promotion._enrich_first_board_row_with_context(
        row,
        hot_mainline_row=hot_row,
    )

    assert "mainline_spread" in enriched["event_types"]
    assert "mainline_relay" in enriched["event_types"]
    assert promotion._should_collect_first_board_diagnostic_from_events(set(enriched["event_types"]))
    assert enriched["detail"]["broad_rotation_member_setup"] is True
    assert enriched["driver_primary"] == "光学光电"


@pytest.mark.asyncio
async def test_best_sector_context_marks_low_rotation_and_stale_theme(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 27)

    async with SessionLocal() as session:
        session.add_all(
            [
                SectorInfo(sector_code="S_ROT", sector_name="电力", sector_type="industry", source="test", is_excluded=0),
                SectorInfo(sector_code="S_STALE", sector_name="半导体", sector_type="industry", source="test", is_excluded=0),
                StockSectorMapping(code="000050", sector_code="S_ROT", sector_name="电力", sector_type="industry", source="test"),
                StockSectorMapping(code="000051", sector_code="S_STALE", sector_name="半导体", sector_type="industry", source="test"),
                SectorPersistence(
                    sector_code="S_ROT",
                    sector_name="电力",
                    trade_date=date(2026, 5, 25),
                    strength_score=18,
                    limit_up_count=0,
                    fund_flow=-0.4,
                    change_pct=-0.6,
                    consecutive_days=0,
                ),
                SectorPersistence(
                    sector_code="S_ROT",
                    sector_name="电力",
                    trade_date=date(2026, 5, 26),
                    strength_score=22,
                    limit_up_count=1,
                    fund_flow=0.2,
                    change_pct=0.3,
                    consecutive_days=0,
                ),
                SectorPersistence(
                    sector_code="S_ROT",
                    sector_name="电力",
                    trade_date=trade_date,
                    strength_score=52,
                    limit_up_count=4,
                    fund_flow=3.2,
                    change_pct=2.1,
                    consecutive_days=1,
                ),
                SectorPersistence(
                    sector_code="S_STALE",
                    sector_name="半导体",
                    trade_date=date(2026, 5, 25),
                    strength_score=86,
                    limit_up_count=8,
                    fund_flow=9.5,
                    change_pct=6.2,
                    consecutive_days=3,
                ),
                SectorPersistence(
                    sector_code="S_STALE",
                    sector_name="半导体",
                    trade_date=trade_date,
                    strength_score=55,
                    limit_up_count=1,
                    fund_flow=-4.0,
                    change_pct=-2.4,
                    consecutive_days=4,
                ),
            ]
        )
        await session.commit()

        context = await promotion._load_best_sector_context(session, trade_date, ["000050", "000051"])

    assert context["000050"]["sector_low_position_rotation"] is True
    assert context["000050"]["sector_rotation_score"] > 65
    assert context["000050"]["sector_rotation_score"] < 100
    assert context["000050"]["sector_fund_flow_delta_percentile"] == 100
    assert context["000050"]["sector_rotation_label"] == "低位轮动抬升"
    assert context["000051"]["sector_crowded_stale_theme"] is True
    assert context["000051"]["sector_rotation_label"] == "高位拥挤退潮"


@pytest.mark.asyncio
async def test_best_sector_context_defaults_to_primary_industry_without_reason_hint(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 27)

    async with SessionLocal() as session:
        session.add_all(
            [
                SectorInfo(sector_code="S_MAIN", sector_name="食品加工", sector_type="industry", source="test", is_excluded=0, stock_count=100),
                SectorInfo(sector_code="S_HOT", sector_name="数据中心", sector_type="concept", source="test", is_excluded=0, stock_count=1),
                StockSectorMapping(code="000060", sector_code="S_MAIN", sector_name="食品加工", sector_type="industry", source="test"),
                StockSectorMapping(code="000060", sector_code="S_HOT", sector_name="数据中心", sector_type="concept", source="test"),
                SectorPersistence(
                    sector_code="S_MAIN", sector_name="食品加工", trade_date=trade_date,
                    strength_score=52, limit_up_count=2, fund_flow=1.0, change_pct=0.8, consecutive_days=1,
                ),
                SectorPersistence(
                    sector_code="S_HOT", sector_name="数据中心", trade_date=trade_date,
                    strength_score=95, limit_up_count=12, fund_flow=20.0, change_pct=6.0, consecutive_days=2,
                ),
            ]
        )
        await session.commit()

        primary = await promotion._load_best_sector_context(session, trade_date, ["000060"])
        hinted = await promotion._load_best_sector_context(
            session,
            trade_date,
            ["000060"],
            reason_by_code={"000060": "数据中心"},
        )

    assert primary["000060"]["sector_name"] == "食品加工"
    assert hinted["000060"]["sector_name"] == "数据中心"


@pytest.mark.asyncio
async def test_best_sector_context_uses_live_main_board_limit_up_breadth_before_derivation(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 8, 20)

    async with SessionLocal() as session:
        session.add_all([
            SectorInfo(
                sector_code="S_LIVE_MED",
                sector_name="创新药",
                sector_type="concept",
                source="test",
                is_excluded=0,
            ),
            StockSectorMapping(
                code="000060", sector_code="S_LIVE_MED", sector_name="创新药",
                sector_type="concept", source="test",
            ),
            *[
                StockSectorMapping(
                    code=code, sector_code="S_LIVE_MED", sector_name="创新药",
                    sector_type="concept", source="test",
                )
                for code in ("600101", "600102", "002103")
            ],
            *[
                    LimitUpPool(
                        code=code, name=f"涨停{code}", trade_date=trade_date,
                        consecutive_days=1, limit_up_reason="创新药", source="test",
                    )
                for code in ("600101", "600102", "002103")
            ],
        ])
        await session.commit()

        context = await promotion._load_best_sector_context(
            session,
            trade_date,
            ["000060"],
            reason_by_code={"000060": "创新药"},
            use_live_limit_up_context=True,
        )
        historical_context = await promotion._load_best_sector_context(
            session,
            trade_date,
            ["000060"],
            reason_by_code={"000060": "创新药"},
            use_live_limit_up_context=False,
        )

    assert historical_context == {}
    assert context["000060"]["sector_live_limit_up_count"] == 3
    assert context["000060"]["sector_live_breadth_override"] is True
    assert context["000060"]["limit_up_count"] == 3
    assert context["000060"]["strength_score"] == 60.0


def test_first_board_probability_rewards_continuity_and_penalizes_high_board_risk():
    row = {
        "setup_grade": "A2 盘口确认后执行",
        "display_score": 108,
        "event_types": ["capital", "breakthrough"],
        "detail": {
            "main_net_inflow_pct": 8.8,
            "volume_ratio": 2.2,
            "support_strength_score": 76,
            "turnover": 8.5,
        },
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 84}
    market_context = {"board_height": 5, "high_board_count": 3, "weak_seal_market": True}
    strong_kline = {
        "confirmation_bonus": 0.13,
        "confirmation_penalty": 0.01,
        "confirmation_score": 0.62,
        "has_doji_confirmation": True,
        "qualified_doji_confirmation": True,
        "has_platform_contraction": True,
        "has_volume_contraction": True,
        "strict_confirmation_count": 3,
        "strict_ready": True,
        "overhead_gap_pct": 1.8,
        "failed_reversal_count": 0,
    }
    weak_kline = {
        "confirmation_bonus": 0.02,
        "confirmation_penalty": 0.12,
        "confirmation_score": 0.29,
        "has_doji_confirmation": False,
        "qualified_doji_confirmation": False,
        "has_platform_contraction": False,
        "has_volume_contraction": False,
        "strict_confirmation_count": 1,
        "strict_ready": False,
        "overhead_gap_pct": 6.6,
        "failed_reversal_count": 2,
    }

    strong_prob, strong_meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 86,
            "consecutive_days": 3,
            "limit_up_count": 5,
            "fund_flow": 8.0,
            "change_pct": 3.2,
        },
        market_context,
        strong_kline,
    )
    weak_prob, weak_meta = promotion._build_first_board_probability(
        {
            **row,
            "risk_flags": [{"code": "high_board_break_risk"}],
        },
        bull_row,
        {
            "strength_score": 32,
            "consecutive_days": 0,
            "limit_up_count": 0,
            "fund_flow": -2.5,
            "change_pct": -0.8,
        },
        market_context,
        weak_kline,
    )

    assert strong_prob > weak_prob
    assert strong_meta["sector_continuity_score"] > weak_meta["sector_continuity_score"]
    assert weak_meta["distribution_penalty"] > strong_meta["distribution_penalty"]
    assert strong_meta["kline_confirmation_score"] > weak_meta["kline_confirmation_score"]
    assert strong_meta["has_doji_confirmation"] is True
    assert strong_meta["qualified_doji_confirmation"] is True
    assert strong_meta["strict_ready"] is True
    assert weak_meta["failed_reversal_count"] > 0


def test_long_cycle_profile_only_adjusts_multi_day_first_board_probability():
    row = {
        "setup_grade": "A2 盘口确认后执行",
        "display_score": 92,
        "event_types": ["pre_board_probe"],
        "detail": {
            "main_net_inflow_pct": 4.0,
            "volume_ratio": 1.6,
            "support_strength_score": 68,
            "change_pct": 2.0,
            "turnover": 5.0,
            "pre_board_probe_score": 76,
        },
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 78}
    sector = {
        "strength_score": 70,
        "consecutive_days": 2,
        "limit_up_count": 3,
        "fund_flow": 3.0,
        "change_pct": 1.8,
    }
    market = {"board_height": 3, "limit_up_count": 42, "sealed_ratio": 0.78}
    base_kline = {
        "confirmation_bonus": 0.08,
        "confirmation_penalty": 0.02,
        "confirmation_score": 0.64,
        "strict_confirmation_count": 3,
        "strict_ready": True,
        "overhead_gap_pct": 2.0,
        "failed_reversal_count": 0,
    }
    ready_kline = {
        **base_kline,
        "long_cycle_profile": {
            "long_cycle_regime": "historical_board_reset",
            "long_cycle_regime_label": "历史涨停记忆重置",
            "quality_setup_score": 78.0,
            "shape_ready": True,
            "setup_phase": "armed",
            "setup_phase_label": "形态入池待盘中确认",
        },
    }

    base_probability, base_meta = promotion._build_first_board_probability(
        row, bull_row, sector, market, base_kline, {}
    )
    ready_probability, ready_meta = promotion._build_first_board_probability(
        row, bull_row, sector, market, ready_kline, {}
    )

    assert ready_probability == base_probability
    assert ready_meta["sub_probabilities"]["first_limitup_next_day"] == base_meta["sub_probabilities"]["first_limitup_next_day"]
    assert ready_meta["sub_probabilities"]["first_limitup_5d"] > base_meta["sub_probabilities"]["first_limitup_5d"]
    assert 0 < ready_meta["long_cycle_five_day_score_delta"] <= 6.0


def test_first_board_probability_rewards_low_position_rotation_and_penalizes_stale_probe():
    row = {
        "setup_grade": "A2 盘口确认后执行",
        "display_score": 98,
        "event_types": ["pre_board_probe"],
        "detail": {
            "main_net_inflow_pct": 2.8,
            "volume_ratio": 1.7,
            "support_strength_score": 62,
            "turnover": 6.0,
            "change_pct": 4.2,
            "intraday_high_pct": 8.8,
            "upper_gap_pct": 2.4,
            "lower_gap_pct": 3.2,
            "close_position": 0.64,
            "limit_probe": True,
            "pre_board_probe_score": 66,
        },
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 66}
    market_context = {
        "market_risk_level": "weak",
        "board_height": 3,
        "high_board_count": 1,
        "weak_seal_market": True,
        "limit_up_count": 46,
        "sealed_ratio": 0.62,
    }
    kline_context = {
        "confirmation_bonus": 0.05,
        "confirmation_penalty": 0.02,
        "confirmation_score": 0.42,
        "strict_confirmation_count": 2,
        "strict_ready": True,
        "overhead_gap_pct": 2.4,
        "failed_reversal_count": 0,
    }

    rotation_prob, rotation_meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 52,
            "consecutive_days": 1,
            "limit_up_count": 4,
            "fund_flow": 3.2,
            "change_pct": 2.1,
            "sector_rotation_score": 72,
            "sector_strength_delta": 28,
            "sector_limit_up_delta": 3.5,
            "sector_low_position_rotation": True,
            "sector_rotation_label": "低位轮动抬升",
        },
        market_context,
        kline_context,
        {"memory_score": 0},
        candidate_route="pre_board_probe_start",
    )
    stale_prob, stale_meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 55,
            "consecutive_days": 4,
            "limit_up_count": 1,
            "fund_flow": -4.0,
            "change_pct": -2.4,
            "sector_rotation_score": 36,
            "sector_strength_delta": -22,
            "sector_limit_up_delta": -5,
            "sector_crowded_stale_theme": True,
            "sector_rotation_label": "高位拥挤退潮",
        },
        market_context,
        kline_context,
        {"memory_score": 0},
        candidate_route="pre_board_probe_start",
    )

    assert rotation_prob > stale_prob
    assert rotation_meta["sector_low_position_rotation"] is True
    assert rotation_meta["rotation_route_points"] > 0
    assert stale_meta["sector_crowded_stale_theme"] is True
    assert stale_meta["stale_theme_penalty_points"] > 0


def test_first_board_probability_splits_probe_wash_from_cashout_failure():
    base_row = {
        "setup_grade": "A2 盘口确认后执行",
        "display_score": 108,
        "event_types": ["pre_board_probe"],
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 82}
    sector = {
        "strength_score": 78,
        "consecutive_days": 2,
        "limit_up_count": 4,
        "fund_flow": 5.0,
        "change_pct": 1.8,
    }
    market_context = {"board_height": 4, "high_board_count": 2, "weak_seal_market": False}
    kline_context = {
        "confirmation_bonus": 0.08,
        "confirmation_penalty": 0.02,
        "confirmation_score": 0.56,
        "strict_confirmation_count": 3,
        "strict_ready": True,
        "overhead_gap_pct": 2.2,
        "failed_reversal_count": 0,
    }

    supportive_prob, supportive_meta = promotion._build_first_board_probability(
        {
            **base_row,
            "detail": {
                "main_net_inflow_pct": 8.0,
                "volume_ratio": 2.2,
                "support_strength_score": 74,
                "turnover": 6.0,
                "change_pct": 6.8,
                "intraday_high_pct": 9.4,
                "upper_gap_pct": 0.8,
                "lower_gap_pct": 5.2,
                "close_position": 0.88,
                "limit_probe": True,
                "preheat_breakout": True,
            },
        },
        bull_row,
        sector,
        market_context,
        kline_context,
        candidate_route="pre_board_probe_start",
    )
    touch_prob, touch_meta = promotion._build_first_board_probability(
        {
            **base_row,
            "detail": {
                "main_net_inflow_pct": 1.0,
                "volume_ratio": 3.2,
                "support_strength_score": 74,
                "turnover": 6.0,
                "change_pct": 1.8,
                "intraday_high_pct": 9.2,
                "upper_gap_pct": 6.2,
                "lower_gap_pct": 1.3,
                "close_position": 0.28,
                "limit_probe": True,
                "preheat_breakout": False,
            },
        },
        bull_row,
        sector,
        market_context,
        kline_context,
        candidate_route="pre_board_probe_start",
    )
    failed_prob, failed_meta = promotion._build_first_board_probability(
        {
            **base_row,
            "detail": {
                "main_net_inflow_pct": -1.5,
                "volume_ratio": 8.4,
                "support_strength_score": 74,
                "turnover": 13.0,
                "change_pct": -5.2,
                "intraday_high_pct": 9.2,
                "upper_gap_pct": 11.6,
                "lower_gap_pct": 0.4,
                "close_position": 0.12,
                "limit_probe": True,
                "preheat_breakout": False,
            },
        },
        bull_row,
        sector,
        market_context,
        {**kline_context, "failed_reversal_count": 1},
        candidate_route="pre_board_probe_start",
    )

    assert supportive_meta["limit_probe_shape_supportive"] is True
    assert supportive_meta["limit_probe_shape_bonus_points"] > 0
    assert touch_meta["limit_probe_shape_supportive"] is True
    assert touch_meta["touch_board_pullback"] is True
    assert touch_meta["pre_board_precursor_type"] == "touch_board_pullback"
    assert touch_meta["limit_probe_shape_bonus_points"] > 0
    assert "摸板回落蓄势" in touch_meta["limit_probe_shape_label"]
    assert failed_meta["limit_probe_shape_failed"] is True
    assert failed_meta["limit_probe_shape_penalty_points"] >= 4.5
    assert touch_prob > failed_prob
    assert touch_meta["route_score"] > failed_meta["route_score"] + 4.0
    assert supportive_prob > failed_prob + 0.03


def test_first_board_probability_splits_fresh_hot_start_into_mainline_and_relay():
    row = {
        "setup_grade": "A2 盘口确认后执行",
        "display_score": 110,
        "event_types": ["capital", "breakthrough"],
        "detail": {
            "main_net_inflow_pct": 9.2,
            "volume_ratio": 1.9,
            "support_strength_score": 66,
            "change_pct": 4.6,
            "turnover": 6.4,
            "open": 10.0,
            "high": 10.12,
            "low": 9.94,
            "price": 10.08,
        },
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 62}
    market_context = {
        "board_height": 3,
        "high_board_count": 1,
        "weak_seal_market": False,
        "limit_up_count": 30,
        "sealed_ratio": 0.7,
    }
    kline_context = {
        "confirmation_bonus": 0.1,
        "confirmation_penalty": 0.01,
        "confirmation_score": 0.56,
        "has_doji_confirmation": False,
        "qualified_doji_confirmation": False,
        "has_platform_contraction": True,
        "has_volume_contraction": False,
        "has_volume_suffocation": False,
        "strict_confirmation_count": 2,
        "strict_ready": True,
        "overhead_gap_pct": 1.6,
        "failed_reversal_count": 0,
    }
    memory_features = {"memory_score": 0.0, "memory_limit_up_hits_50d": 0, "memory_days_since_last_limit_up": 999}

    _mainline_prob, mainline_meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 78,
            "consecutive_days": 3,
            "limit_up_count": 5,
            "fund_flow": 7.8,
            "change_pct": 3.5,
        },
        market_context,
        kline_context,
        memory_features,
    )
    _relay_prob, relay_meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 49,
            "consecutive_days": 1,
            "limit_up_count": 2,
            "fund_flow": 2.4,
            "change_pct": 1.4,
        },
        market_context,
        kline_context,
        memory_features,
    )

    assert mainline_meta["candidate_route"] == "fresh_mainline_start"
    assert mainline_meta["candidate_route_label"] == "主线首波点火"
    assert relay_meta["candidate_route"] == "fresh_relay_start"
    assert relay_meta["candidate_route_label"] == "分支卡位热启动"


def test_resolve_promotion_signal_status_marks_intraday_preview_and_close_confirmed():
    intraday = promotion._resolve_promotion_signal_status(
        target_board=1,
        trade_date=date(2026, 4, 24),
        latest_as_of="2026-04-24T10:15:00",
        kline_context={"provisional_last_bar": True},
        today=date(2026, 4, 24),
        session_name="morning",
    )
    confirmed = promotion._resolve_promotion_signal_status(
        target_board=1,
        trade_date=date(2026, 4, 23),
        latest_as_of="2026-04-23T14:55:00",
        kline_context={"provisional_last_bar": False},
        today=date(2026, 4, 24),
        session_name="morning",
    )
    second_board = promotion._resolve_promotion_signal_status(
        target_board=2,
        trade_date=date(2026, 4, 23),
        latest_as_of="2026-04-23",
        today=date(2026, 4, 24),
        session_name="morning",
    )
    intraday_second_board = promotion._resolve_promotion_signal_status(
        target_board=2,
        trade_date=date(2026, 4, 24),
        latest_as_of="2026-04-24T10:15:00",
        today=date(2026, 4, 24),
        session_name="morning",
    )
    lunch_second_board = promotion._resolve_promotion_signal_status(
        target_board=2,
        trade_date=date(2026, 4, 24),
        latest_as_of="2026-04-24T11:50:00",
        today=date(2026, 4, 24),
        session_name="lunch_break",
    )

    assert intraday["signal_status"] == "intraday_preview"
    assert intraday["signal_status_label"] == "盘中预判"
    assert "未收盘" in intraday["signal_status_reason"]
    assert confirmed["signal_status"] == "close_confirmed"
    assert confirmed["signal_status_label"] == "收盘确认"
    assert second_board["signal_status"] == "close_confirmed"
    assert intraday_second_board["signal_status"] == "intraday_preview"
    assert lunch_second_board["signal_status"] == "intraday_preview"
    assert "尚未最终确认" in intraday_second_board["signal_status_reason"]


def test_resolve_snapshot_clock_source_falls_back_to_row_as_of():
    rows = [
        {
            "code": "000010",
            "detail": {"as_of": "2026-04-24T10:15:00"},
        }
    ]

    assert promotion._resolve_snapshot_clock_source({}, rows) == "2026-04-24T10:15:00"
    assert promotion._resolve_snapshot_clock_source({"snapshot_time": "2026-04-24T14:55:00"}, rows) == "2026-04-24T14:55:00"


@pytest.mark.asyncio
async def test_resolve_first_board_trade_date_accepts_datetime_snapshot_value(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        resolved = await promotion._resolve_first_board_trade_date(
            session,
            {"trade_date": "2026-04-24T10:15:00"},
        )

    assert resolved == date(2026, 4, 24)


@pytest.mark.asyncio
async def test_resolve_first_board_trade_date_uses_current_auction_day(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add(
            AuctionData(
                code="000001",
                trade_date=date(2026, 4, 27),
                auction_time="09:25:00",
                auction_price=10.2,
                prev_close=10.0,
                open_change=2.0,
            )
        )
        await session.commit()
        resolved = await promotion._resolve_first_board_trade_date(
            session,
            {
                "trade_date": "2026-04-24",
                "snapshot_time": "2026-04-27T09:25:10",
            },
        )

    assert resolved == date(2026, 4, 27)


def test_first_board_kline_confirmation_rewards_doji_contraction_and_penalizes_failed_reversal():
    strong_bars = [
        {"open": 9.70, "close": 9.84, "high": 9.95, "low": 9.62, "volume": 180000},
        {"open": 9.82, "close": 9.90, "high": 10.02, "low": 9.76, "volume": 176000},
        {"open": 9.88, "close": 9.94, "high": 10.08, "low": 9.82, "volume": 170000},
        {"open": 9.95, "close": 9.92, "high": 10.10, "low": 9.86, "volume": 160000},
        {"open": 9.91, "close": 9.96, "high": 10.11, "low": 9.88, "volume": 148000},
        {"open": 9.95, "close": 9.98, "high": 10.09, "low": 9.90, "volume": 118000},
        {"open": 9.99, "close": 10.01, "high": 10.13, "low": 9.92, "volume": 96000},
        {"open": 10.00, "close": 10.02, "high": 10.16, "low": 9.93, "volume": 82000},
    ]
    weak_bars = [
        {"open": 9.40, "close": 9.78, "high": 9.92, "low": 9.32, "volume": 150000},
        {"open": 9.76, "close": 9.55, "high": 10.08, "low": 9.50, "volume": 170000},
        {"open": 9.58, "close": 9.44, "high": 9.98, "low": 9.36, "volume": 188000},
        {"open": 9.46, "close": 9.62, "high": 10.20, "low": 9.40, "volume": 210000},
        {"open": 9.66, "close": 9.48, "high": 10.26, "low": 9.40, "volume": 235000},
        {"open": 9.52, "close": 9.36, "high": 9.98, "low": 9.28, "volume": 258000},
        {"open": 9.40, "close": 9.28, "high": 9.88, "low": 9.18, "volume": 272000},
        {"open": 9.32, "close": 9.18, "high": 9.82, "low": 9.05, "volume": 290000},
    ]

    strong = promotion._build_first_board_kline_confirmation(strong_bars)
    weak = promotion._build_first_board_kline_confirmation(weak_bars)

    assert strong["confirmation_score"] > weak["confirmation_score"]
    assert strong["has_doji_confirmation"] is True
    assert strong["qualified_doji_confirmation"] is True
    assert strong["has_volume_contraction"] is True
    assert strong["has_volume_suffocation"] is True
    assert strong["has_platform_contraction"] is True
    assert strong["strict_ready"] is True
    assert weak["failed_reversal_count"] >= 1
    assert weak["confirmation_penalty"] > strong["confirmation_penalty"]
    assert strong["platform_cycle_type"] == "short"


def test_first_board_kline_confirmation_recognizes_trend_breakout_acceleration():
    bars = [
        {"open": 9.80, "close": 9.92, "high": 9.96, "low": 9.72, "volume": 120000},
        {"open": 9.93, "close": 10.02, "high": 10.06, "low": 9.86, "volume": 124000},
        {"open": 10.02, "close": 10.10, "high": 10.14, "low": 9.98, "volume": 128000},
        {"open": 10.08, "close": 10.18, "high": 10.22, "low": 10.04, "volume": 132000},
        {"open": 10.16, "close": 10.24, "high": 10.28, "low": 10.12, "volume": 138000},
        {"open": 10.22, "close": 10.32, "high": 10.36, "low": 10.18, "volume": 144000},
        {"open": 10.30, "close": 10.42, "high": 10.46, "low": 10.26, "volume": 156000},
        {"open": 10.40, "close": 10.58, "high": 10.62, "low": 10.34, "volume": 178000},
    ]

    context = promotion._build_first_board_kline_confirmation(bars)

    assert context["trend_breakout"] is True
    assert context["trend_acceleration_ready"] is True
    assert context["strict_ready"] is True
    assert "趋势新高加速" in context["summary"]


def test_first_board_kline_confirmation_separates_near_high_from_real_breakout():
    bars = [
        {"open": 9.72, "close": 9.82, "high": 9.88, "low": 9.66, "volume": 120000},
        {"open": 9.84, "close": 9.96, "high": 10.02, "low": 9.78, "volume": 124000},
        {"open": 9.98, "close": 10.08, "high": 10.14, "low": 9.92, "volume": 128000},
        {"open": 10.08, "close": 10.16, "high": 10.22, "low": 10.02, "volume": 132000},
        {"open": 10.18, "close": 10.24, "high": 10.30, "low": 10.12, "volume": 136000},
        {"open": 10.24, "close": 10.34, "high": 10.40, "low": 10.20, "volume": 145000},
        {"open": 10.36, "close": 10.42, "high": 10.48, "low": 10.30, "volume": 150000},
        {"open": 10.32, "close": 10.34, "high": 10.39, "low": 10.26, "volume": 168000},
    ]

    context = promotion._build_first_board_kline_confirmation(bars)

    assert context["near_trend_high"] is True
    assert context["trend_breakout"] is False
    assert context["trend_acceleration_ready"] is False
    assert context["trend_confirmation_count"] >= 3
    assert "趋势新高加速" not in context["summary"]


def test_first_board_kline_confirmation_downweights_intraday_spot_fallback_bar():
    confirmed_bars = [
        {"open": 9.70, "close": 9.84, "high": 9.95, "low": 9.62, "volume": 180000},
        {"open": 9.82, "close": 9.90, "high": 10.02, "low": 9.76, "volume": 176000},
        {"open": 9.88, "close": 9.94, "high": 10.08, "low": 9.82, "volume": 170000},
        {"open": 9.95, "close": 9.92, "high": 10.10, "low": 9.86, "volume": 160000},
        {"open": 9.91, "close": 9.96, "high": 10.11, "low": 9.88, "volume": 152000},
        {"open": 9.95, "close": 9.99, "high": 10.12, "low": 9.90, "volume": 149000},
        {"open": 9.99, "close": 10.02, "high": 10.14, "low": 9.94, "volume": 146000},
        {"open": 10.01, "close": 10.03, "high": 10.16, "low": 9.95, "volume": 28000},
    ]
    provisional_bars = confirmed_bars[:-1] + [
        {
            **confirmed_bars[-1],
            "source": "spot_fallback",
            "is_provisional": True,
        }
    ]

    confirmed = promotion._build_first_board_kline_confirmation(confirmed_bars)
    provisional = promotion._build_first_board_kline_confirmation(provisional_bars)

    assert confirmed["has_volume_contraction"] is True
    assert provisional["provisional_last_bar"] is True
    assert provisional["has_volume_contraction"] is False
    assert provisional["confirmation_score"] < confirmed["confirmation_score"]
    assert "盘中K线未收盘降权" in provisional["summary"]


@pytest.mark.asyncio
async def test_load_first_board_kline_context_uses_snapshot_clock_for_provisional_bar(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                StockKline(code="000020", trade_date=date(2026, 4, 14), open=9.70, close=9.84, high=9.95, low=9.62, volume=180000, source="ths"),
                StockKline(code="000020", trade_date=date(2026, 4, 15), open=9.82, close=9.90, high=10.02, low=9.76, volume=176000, source="ths"),
                StockKline(code="000020", trade_date=date(2026, 4, 16), open=9.88, close=9.94, high=10.08, low=9.82, volume=170000, source="ths"),
                StockKline(code="000020", trade_date=date(2026, 4, 17), open=9.95, close=9.92, high=10.10, low=9.86, volume=160000, source="ths"),
                StockKline(code="000020", trade_date=date(2026, 4, 20), open=9.91, close=9.96, high=10.11, low=9.88, volume=152000, source="ths"),
                StockKline(code="000020", trade_date=date(2026, 4, 21), open=9.95, close=9.99, high=10.12, low=9.90, volume=149000, source="ths"),
                StockKline(code="000020", trade_date=date(2026, 4, 22), open=9.99, close=10.02, high=10.14, low=9.94, volume=146000, source="ths"),
                StockKline(code="000020", trade_date=date(2026, 4, 23), open=10.01, close=10.03, high=10.16, low=9.95, volume=28000, source="spot_fallback"),
            ]
        )
        await session.commit()

        context_map = await promotion._load_first_board_kline_context(
            session,
            ["000020"],
            date(2026, 4, 23),
            as_of_date=date(2026, 4, 23),
            session_name="morning",
        )

    assert context_map["000020"]["provisional_last_bar"] is True
    assert "盘中K线未收盘降权" in context_map["000020"]["summary"]


@pytest.mark.asyncio
async def test_trade_day_gap_uses_observed_kline_trade_dates_instead_of_weekdays(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add(
            StockKline(
                code="000020",
                trade_date=date(2026, 2, 23),
                open=10.0,
                close=10.1,
                high=10.2,
                low=9.9,
                volume=100000,
                source="ths",
            )
        )
        await session.commit()

        assert await promotion._trade_day_gap(session, date(2026, 2, 15), date(2026, 2, 23)) == 1


def test_first_board_kline_confirmation_uses_completed_bars_for_prev_low_window_with_provisional_bar():
    bars = [
        {"open": 9.88, "close": 9.93, "high": 10.02, "low": 9.82, "volume": 160000},
        {"open": 9.90, "close": 9.95, "high": 10.04, "low": 9.84, "volume": 150000},
        {"open": 9.96, "close": 10.00, "high": 10.08, "low": 9.90, "volume": 140000},
        {"open": 9.99, "close": 10.01, "high": 10.09, "low": 9.92, "volume": 130000},
        {"open": 10.00, "close": 10.02, "high": 10.10, "low": 9.94, "volume": 120000},
        {"open": 10.02, "close": 10.03, "high": 10.11, "low": 9.80, "volume": 110000},
        {"open": 10.03, "close": 10.04, "high": 10.12, "low": 9.82, "volume": 100000},
        {"open": 10.04, "close": 10.05, "high": 10.13, "low": 9.84, "volume": 90000},
        {"open": 10.05, "close": 10.06, "high": 10.14, "low": 9.96, "volume": 24000, "source": "spot_fallback", "is_provisional": True},
    ]

    context = promotion._build_first_board_kline_confirmation(bars)

    assert context["provisional_last_bar"] is True
    assert context["strict_confirmation_count"] == 3
    assert context["strict_ready"] is True


def test_first_board_kline_confirmation_does_not_treat_doji_alone_as_strict_ready():
    bars = [
        {"open": 9.70, "close": 10.05, "high": 10.20, "low": 9.62, "volume": 160000},
        {"open": 10.08, "close": 9.92, "high": 10.24, "low": 9.80, "volume": 165000},
        {"open": 9.95, "close": 10.10, "high": 10.28, "low": 9.86, "volume": 168000},
        {"open": 10.06, "close": 9.88, "high": 10.30, "low": 9.84, "volume": 171000},
        {"open": 10.12, "close": 9.94, "high": 10.34, "low": 9.90, "volume": 174000},
        {"open": 9.98, "close": 10.00, "high": 10.36, "low": 9.92, "volume": 176000},
        {"open": 10.04, "close": 10.03, "high": 10.38, "low": 9.94, "volume": 178000},
        {"open": 10.02, "close": 10.01, "high": 10.40, "low": 9.95, "volume": 180000},
    ]

    context = promotion._build_first_board_kline_confirmation(bars)

    assert context["has_doji_confirmation"] is True
    assert context["qualified_doji_confirmation"] is False
    assert context["strict_ready"] is False


def test_first_board_kline_confirmation_recognizes_burst_pullback_restart():
    bars = []
    for idx in range(12):
        close_price = 10.00 + idx * 0.02
        bars.append(
            {
                "trade_date": date(2026, 4, 1),
                "open": close_price - 0.03,
                "close": close_price,
                "high": close_price + 0.08,
                "low": close_price - 0.08,
                "volume": 100000 + idx * 1800,
            }
        )
    bars.append(
        {
            "trade_date": date(2026, 4, 17),
            "prev_close": bars[-1]["close"],
            "open": 10.24,
            "close": 10.92,
            "high": 11.22,
            "low": 10.18,
            "volume": 620000,
        }
    )
    bars.extend(
        [
            {"trade_date": date(2026, 4, 20), "open": 10.70, "close": 10.48, "high": 10.82, "low": 10.32, "volume": 230000},
            {"trade_date": date(2026, 4, 21), "open": 10.35, "close": 9.98, "high": 10.42, "low": 9.85, "volume": 185000},
            {"trade_date": date(2026, 4, 22), "open": 9.86, "close": 9.54, "high": 9.98, "low": 9.36, "volume": 150000},
            {"trade_date": date(2026, 4, 23), "open": 9.42, "close": 9.48, "high": 9.62, "low": 9.30, "volume": 132000},
            {"trade_date": date(2026, 4, 24), "open": 9.52, "close": 9.72, "high": 9.84, "low": 9.44, "volume": 142000},
            {"trade_date": date(2026, 4, 27), "open": 9.76, "close": 9.96, "high": 10.08, "low": 9.70, "volume": 158000},
            {"trade_date": date(2026, 4, 28), "open": 10.02, "close": 10.18, "high": 10.30, "low": 9.96, "volume": 174000},
            {"trade_date": date(2026, 4, 29), "open": 10.20, "close": 10.42, "high": 10.52, "low": 10.12, "volume": 196000},
            {"trade_date": date(2026, 4, 30), "open": 10.46, "close": 10.68, "high": 10.80, "low": 10.30, "volume": 225000},
        ]
    )

    context = promotion._build_first_board_kline_confirmation(bars)

    assert context["burst_pullback_restart_ready"] is True
    assert context["burst_pullback_score"] >= 62
    assert context["strict_ready"] is True
    assert context["burst_pullback_depth_pct"] >= 10
    assert context["burst_shrink_ratio"] <= 0.58


def test_first_board_kline_confirmation_uses_adaptive_long_platform_volume_suffocation():
    bars = []
    base_open = 9.82
    for idx in range(33):
        close_price = 9.88 + (idx % 5) * 0.03
        high_price = close_price + 0.16
        low_price = close_price - 0.15
        bars.append(
            {
                "open": base_open + (idx % 3) * 0.01,
                "close": close_price,
                "high": high_price,
                "low": low_price,
                "volume": 220000 - idx * 2500,
            }
        )
    bars.extend(
        [
            {"open": 10.46, "close": 10.50, "high": 10.58, "low": 10.36, "volume": 76000},
            {"open": 10.49, "close": 10.52, "high": 10.60, "low": 10.40, "volume": 68000},
            {"open": 10.51, "close": 10.55, "high": 10.63, "low": 10.43, "volume": 62000},
        ]
    )

    context = promotion._build_first_board_kline_confirmation(bars)

    assert context["platform_cycle_type"] == "long"
    assert context["platform_cycle_days"] >= 31
    assert context["has_volume_suffocation"] is True
    assert context["volume_suffocation_ratio"] < 0.62
    assert "长平台量窒息" in context["summary"]


def test_platform_cycle_handles_zero_high_reference_window():
    bars = [
        {"open": 9.8, "close": 9.9, "high": 0.0, "low": 9.6, "volume": 180000},
        {"open": 9.9, "close": 9.95, "high": 0.0, "low": 9.7, "volume": 170000},
        {"open": 9.95, "close": 10.0, "high": 0.0, "low": 9.8, "volume": 160000},
        {"open": 10.0, "close": 10.02, "high": 0.0, "low": 9.85, "volume": 150000},
        {"open": 10.02, "close": 10.04, "high": 0.0, "low": 9.88, "volume": 140000},
        {"open": 10.04, "close": 10.05, "high": 0.0, "low": 9.9, "volume": 130000},
        {"open": 10.05, "close": 10.06, "high": 0.0, "low": 9.92, "volume": 120000},
        {"open": 10.06, "close": 10.07, "high": 10.20, "low": 9.94, "volume": 70000},
        {"open": 10.07, "close": 10.08, "high": 10.22, "low": 9.96, "volume": 62000},
        {"open": 10.08, "close": 10.10, "high": 10.24, "low": 9.98, "volume": 56000},
    ]

    context = promotion._build_first_board_kline_confirmation(bars)

    assert context["insufficient_history"] is False
    assert context["platform_reference_high"] >= 10.2


def test_tenbagger_kdj_ignores_invalid_unaligned_bars():
    highs = [10, 10.2, None, 10.4, 10.5, 0, 10.6, 10.7, 10.8, 10.9, 11.0]
    lows = [9.8, 9.9, 10.0, None, 10.1, 10.2, 10.2, 10.3, 10.4, 10.5, 10.6]
    closes = [9.9, 10.1, 10.2, 10.3, None, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9]

    assert tenbagger._calc_kdj(highs, lows, closes, 9, 3, 3) is None

    valid_highs = [10 + idx * 0.1 for idx in range(12)]
    valid_lows = [9.8 + idx * 0.1 for idx in range(12)]
    valid_closes = [9.9 + idx * 0.1 for idx in range(12)]
    kdj = tenbagger._calc_kdj(valid_highs, valid_lows, valid_closes, 9, 3, 3)

    assert kdj is not None
    assert len(kdj[0]) == 4
    assert len(kdj[1]) == 4


def test_strict_first_board_gate_requires_primary_trigger_and_structural_confirmation():
    row = {
        "event_types": ["rapid_rise"],
        "detail": {
            "support_strength_score": 72,
            "main_net_inflow_pct": 8.0,
            "volume_ratio": 2.0,
            "change_pct": 4.6,
        },
    }
    sector = {
        "strength_score": 82,
        "consecutive_days": 3,
        "limit_up_count": 4,
        "fund_flow": 6.0,
        "change_pct": 2.8,
    }
    kline_context = {
        "confirmation_score": 0.68,
        "strict_ready": True,
        "strict_confirmation_count": 3,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=82,
        sector=sector,
        kline_context=kline_context,
    )

    assert passed is False
    assert route == "strict_stealth"
    assert "十字星/结构确认不够硬" in blockers


def test_strict_first_board_gate_allows_stealth_setup_when_structure_is_very_strong():
    row = {
        "event_types": ["rapid_rise"],
        "detail": {
            "support_strength_score": 63,
            "main_net_inflow_pct": 0.8,
            "volume_ratio": 1.4,
            "change_pct": 3.8,
        },
    }
    sector = {
        "strength_score": 58,
        "consecutive_days": 2,
        "limit_up_count": 2,
        "fund_flow": 2.6,
        "change_pct": 1.9,
    }
    kline_context = {
        "confirmation_score": 0.69,
        "strict_ready": True,
        "strict_confirmation_count": 4,
        "qualified_doji_confirmation": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 1.6,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=79,
        sector=sector,
        kline_context=kline_context,
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_stealth"


def test_strict_first_board_gate_allows_quiet_setup_when_structure_is_very_strong():
    row = {
        "event_types": ["stealth_setup"],
        "detail": {
            "support_strength_score": 58,
            "main_net_inflow_pct": -0.8,
            "volume_ratio": 1.1,
            "change_pct": 1.6,
            "amplitude": 3.4,
        },
    }
    sector = {
        "strength_score": 56,
        "consecutive_days": 2,
        "limit_up_count": 2,
        "fund_flow": 2.1,
        "change_pct": 1.7,
    }
    kline_context = {
        "confirmation_score": 0.72,
        "strict_ready": True,
        "strict_confirmation_count": 4,
        "qualified_doji_confirmation": True,
        "has_volume_suffocation": True,
        "has_platform_contraction": True,
        "near_breakout": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 1.4,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=81,
        sector=sector,
        kline_context=kline_context,
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_stealth_scan"


def test_strict_first_board_gate_allows_quiet_setup_with_limit_up_nearby_doji_even_without_volume_suffocation():
    row = {
        "event_types": ["stealth_setup"],
        "detail": {
            "support_strength_score": 44,
            "main_net_inflow_pct": -0.4,
            "volume_ratio": 0.82,
            "change_pct": 0.9,
            "amplitude": 4.2,
        },
    }
    sector = {
        "strength_score": 34,
        "consecutive_days": 1,
        "limit_up_count": 1,
        "fund_flow": 0.8,
        "change_pct": 0.9,
    }
    kline_context = {
        "confirmation_score": 0.66,
        "strict_ready": True,
        "strict_confirmation_count": 3,
        "has_doji_confirmation": True,
        "qualified_doji_confirmation": True,
        "has_platform_contraction": True,
        "has_volume_suffocation": False,
        "near_breakout": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 1.9,
        "latest_close": 10.06,
    }
    memory_features = {
        "memory_score": 18.0,
        "memory_limit_up_hits_50d": 1,
        "memory_days_since_last_limit_up": 9,
        "memory_last_limit_up_close": 10.05,
        "memory_last_limit_up_high": 10.08,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=61,
        sector=sector,
        kline_context=kline_context,
        memory_features=memory_features,
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_stealth_scan"


def test_strict_first_board_gate_blocks_quiet_setup_without_memory_or_mainline_edge():
    row = {
        "event_types": ["stealth_setup"],
        "detail": {
            "support_strength_score": 45,
            "main_net_inflow_pct": 0.3,
            "volume_ratio": 0.86,
            "change_pct": 0.4,
            "amplitude": 3.1,
        },
    }
    sector = {
        "strength_score": 27,
        "consecutive_days": 1,
        "limit_up_count": 1,
        "fund_flow": 0.5,
        "change_pct": 0.8,
    }
    kline_context = {
        "confirmation_score": 0.74,
        "strict_ready": True,
        "strict_confirmation_count": 4,
        "qualified_doji_confirmation": True,
        "has_volume_suffocation": True,
        "has_platform_contraction": True,
        "near_breakout": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 1.5,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=68,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
    )

    assert passed is False
    assert route == "strict_stealth_scan"
    assert "缺少首波记忆或主线共振" in blockers


def test_strict_first_board_gate_allows_quiet_setup_with_support_squeeze_even_without_memory():
    row = {
        "event_types": ["stealth_setup"],
        "detail": {
            "support_strength_score": 52,
            "main_net_inflow_pct": -0.2,
            "volume_ratio": 0.94,
            "change_pct": 1.2,
            "amplitude": 3.3,
        },
    }
    sector = {
        "strength_score": 46,
        "consecutive_days": 1,
        "limit_up_count": 1,
        "fund_flow": 1.2,
        "change_pct": 1.0,
    }
    kline_context = {
        "confirmation_score": 0.69,
        "strict_ready": True,
        "strict_confirmation_count": 3,
        "qualified_doji_confirmation": False,
        "has_doji_confirmation": False,
        "has_volume_contraction": True,
        "has_volume_suffocation": True,
        "near_breakout": False,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 4.8,
        "support_zone_price": 10.0,
        "support_zone_gap_pct": 2.4,
        "support_hold_days": 4,
        "support_rebound_count": 3,
        "lower_shadow_absorption": 0.36,
        "has_support_absorption": True,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=69,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_stealth_scan"


def test_strict_first_board_gate_allows_quiet_setup_with_support_volume_release_even_without_memory():
    row = {
        "event_types": ["stealth_setup"],
        "detail": {
            "support_strength_score": 54,
            "main_net_inflow_pct": 1.6,
            "volume_ratio": 1.46,
            "change_pct": 2.4,
            "amplitude": 4.1,
        },
    }
    sector = {
        "strength_score": 42,
        "consecutive_days": 1,
        "limit_up_count": 1,
        "fund_flow": 1.0,
        "change_pct": 0.9,
    }
    kline_context = {
        "confirmation_score": 0.67,
        "strict_ready": True,
        "strict_confirmation_count": 3,
        "qualified_doji_confirmation": False,
        "has_doji_confirmation": False,
        "has_volume_contraction": False,
        "has_volume_suffocation": False,
        "near_breakout": False,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 4.1,
        "support_zone_price": 10.0,
        "support_zone_gap_pct": 2.2,
        "support_hold_days": 4,
        "support_rebound_count": 3,
        "lower_shadow_absorption": 0.34,
        "has_support_absorption": True,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=66,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_stealth_scan"


def test_strict_first_board_gate_allows_news_catalyst_without_memory_edge():
    row = {
        "event_types": ["news_catalyst"],
        "detail": {
            "price": 10.2,
            "high": 10.35,
            "low": 9.86,
            "support_strength_score": 46,
            "main_net_inflow_pct": -0.6,
            "volume_ratio": 1.12,
            "change_pct": 2.2,
            "amplitude": 4.7,
            "news_catalyst_score": 69,
            "news_title": "重大订单落地",
            "news_source": "cninfo",
            "news_importance": 8,
        },
    }
    sector = {
        "strength_score": 26,
        "consecutive_days": 1,
        "limit_up_count": 1,
        "fund_flow": 0.8,
        "change_pct": 0.9,
    }
    kline_context = {
        "confirmation_score": 0.42,
        "strict_ready": False,
        "strict_confirmation_count": 1,
        "near_trend_high": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 4.2,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=54,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
    )
    probability, meta = promotion._build_first_board_probability(
        row,
        {"total_score": 54},
        sector,
        {"limit_up_count": 45, "board_height": 4, "sealed_ratio": 0.78},
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        candidate_route=promotion._candidate_route_from_strict_route(route, sector=sector, bull_score=54, support_strength=46),
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_news_catalyst"
    assert meta["candidate_route"] == "news_catalyst_start"
    assert meta["news_title"] == "重大订单落地"
    assert probability > 0


def test_strict_first_board_gate_allows_auction_surge_without_memory_edge():
    row = {
        "event_types": ["auction_surge"],
        "detail": {
            "price": 12.4,
            "high": 12.55,
            "low": 11.8,
            "support_strength_score": 45,
            "main_net_inflow_pct": 0.4,
            "volume_ratio": 1.42,
            "change_pct": 3.1,
            "amplitude": 5.6,
            "auction_feed_complete": True,
            "auction_evidence_status": "ok",
            "auction_evidence_contract": "auction_provenance_v1",
            "auction_strength_score": 64,
            "auction_open_change": 3.4,
            "auction_volume_ratio": 2.6,
            "auction_amount": 42_000_000,
            "auction_cancelled": False,
        },
    }
    sector = {
        "strength_score": 24,
        "consecutive_days": 1,
        "limit_up_count": 1,
        "fund_flow": 0.6,
        "change_pct": 0.7,
    }
    kline_context = {
        "confirmation_score": 0.41,
        "strict_ready": False,
        "strict_confirmation_count": 1,
        "near_breakout": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 3.6,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=52,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
    )
    probability, meta = promotion._build_first_board_probability(
        row,
        {"total_score": 52},
        sector,
        {"limit_up_count": 45, "board_height": 4, "sealed_ratio": 0.78},
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        candidate_route=promotion._candidate_route_from_strict_route(route, sector=sector, bull_score=52, support_strength=45),
    )
    item = {
        "candidate_route": meta["candidate_route"],
        "probability": probability,
        "route_score": meta["route_score"],
        "support_strength_score": 45,
        "sector_strength_score": 24,
        "change_pct": 3.1,
        "volume_ratio": 1.42,
        "probability_factors": meta,
    }

    assert passed is True
    assert blockers == []
    assert route == "strict_auction_surge"
    assert meta["candidate_route"] == "auction_surge_start"
    assert meta["auction_open_change"] == 3.4
    assert promotion._is_auction_surge_sprint_candidate(item) is True


def test_strict_first_board_gate_allows_mainline_spread_fillup_route():
    row = {
        "event_types": ["mainline_spread"],
        "detail": {
            "price": 8.3,
            "high": 8.38,
            "low": 8.01,
            "support_strength_score": 52,
            "main_net_inflow_pct": 2.6,
            "volume_ratio": 1.28,
            "change_pct": 2.7,
            "amplitude": 4.3,
        },
    }
    sector = {
        "strength_score": 72,
        "consecutive_days": 3,
        "limit_up_count": 5,
        "fund_flow": 6.5,
        "change_pct": 2.6,
    }
    kline_context = {
        "confirmation_score": 0.45,
        "strict_ready": False,
        "strict_confirmation_count": 1,
        "trend_acceleration_ready": True,
        "near_breakout": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 2.8,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=58,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
    )
    _probability, meta = promotion._build_first_board_probability(
        row,
        {"total_score": 58},
        sector,
        {"limit_up_count": 45, "board_height": 4, "sealed_ratio": 0.78},
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        candidate_route=promotion._candidate_route_from_strict_route(route, sector=sector, bull_score=58, support_strength=52),
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_mainline_spread"
    assert meta["candidate_route"] == "mainline_spread_start"


def test_broad_rotation_day_allows_low_position_mainline_spread_without_global_loosen():
    row = {
        "event_types": ["mainline_spread"],
        "detail": {
            "price": 8.3,
            "high": 8.38,
            "low": 8.01,
            "support_strength_score": 44,
            "main_net_inflow_pct": 1.8,
            "volume_ratio": 0.92,
            "change_pct": 1.6,
            "amplitude": 3.1,
        },
    }
    sector = {
        "strength_score": 52,
        "consecutive_days": 1,
        "limit_up_count": 3,
        "fund_flow": 2.5,
        "change_pct": 1.1,
        "sector_low_position_rotation": True,
        "sector_rotation_score": 60,
        "sector_limit_up_delta": 2,
    }
    kline_context = {
        "confirmation_score": 0.35,
        "strict_ready": False,
        "strict_confirmation_count": 1,
        "trend_acceleration_ready": True,
        "near_breakout": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 3.0,
    }

    normal_passed, normal_blockers, _normal_route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=50,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        market_context={"broad_first_board_overflow": False},
    )
    broad_passed, broad_blockers, broad_route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=50,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        market_context={"broad_first_board_overflow": True},
    )
    probability, meta = promotion._build_first_board_probability(
        row,
        {"total_score": 50},
        sector,
        {
            "limit_up_count": 120,
            "first_board_count": 95,
            "board_height": 5,
            "sealed_ratio": 0.62,
            "broad_first_board_overflow": True,
        },
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        candidate_route=promotion._candidate_route_from_strict_route(
            broad_route,
            sector=sector,
            bull_score=50,
            support_strength=44,
        ),
    )

    assert normal_passed is False
    assert "主线扩散强度不足" in normal_blockers
    assert broad_passed is True
    assert broad_blockers == []
    assert meta["candidate_route"] == "mainline_spread_start"
    assert meta["market_broad_first_board_overflow"] is True
    assert meta["broad_rotation_route_points"] > 0
    item = {
        "probability": probability,
        "route_score": meta["route_score"],
        "support_strength_score": 44,
        "sector_strength_score": 52,
        "change_pct": 1.6,
        "volume_ratio": 0.92,
        "main_net_inflow_pct": 1.8,
        "probability_factors": {
            **meta,
            "strict_confirmation_count": 1,
        },
    }
    assert promotion._is_mainline_spread_sprint_candidate(item) is True


def test_broad_rotation_member_setup_allows_flat_or_small_red_mainline_member():
    spot = StockSpot(
        code="000778",
        name="低位轮动",
        price=9.9,
        prev_close=10.0,
        open=9.95,
        high=10.05,
        low=9.75,
        change_pct=-1.0,
        volume_ratio=0.48,
        turnover=3.2,
        amount=80_000_000,
        main_net_inflow=6_000_000,
        support_strength_score=52,
    )
    sector = {
        "sector_name": "芯片概念",
        "strength_score": 76,
        "consecutive_days": 3,
        "limit_up_count": 8,
        "fund_flow": 3.5,
        "change_pct": 1.4,
        "sector_rotation_score": 90,
        "sector_limit_up_delta": 2.5,
    }
    kline_context = {
        "confirmation_score": 0.36,
        "strict_ready": False,
        "strict_confirmation_count": 1,
        "near_breakout": True,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 2.2,
    }
    row = promotion._build_hot_mainline_relay_row(
        spot,
        sector=sector,
        bull_row={"total_score": 62, "top_signals": ["热门主线补涨"]},
        kline_context=kline_context,
        market_context={"broad_first_board_overflow": True},
    )

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=62,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        market_context={"broad_first_board_overflow": True},
    )

    assert "mainline_spread" in row["event_types"]
    assert row["detail"]["broad_rotation_member_setup"] is True
    assert route == "strict_mainline_spread"
    assert passed is True, blockers


def test_sector_catalyst_spread_allows_pullback_member_without_market_overflow():
    row = {
        "event_types": ["mainline_spread"],
        "detail": {
            "price": 9.6,
            "high": 9.88,
            "low": 9.42,
            "support_strength_score": 43,
            "main_net_inflow_pct": -1.2,
            "volume_ratio": 0.58,
            "change_pct": -3.1,
            "amplitude": 4.6,
            "broad_rotation_member_setup": True,
            "broad_rotation_cluster_setup": True,
            "sector_catalyst_spread": True,
        },
    }
    sector = {
        "sector_name": "小金属",
        "strength_score": 58,
        "consecutive_days": 1,
        "limit_up_count": 8,
        "fund_flow": 1.8,
        "change_pct": 1.2,
        "sector_low_position_rotation": True,
        "sector_rotation_score": 64,
        "sector_limit_up_delta": 2.5,
        "sector_strength_delta": 10,
    }
    kline_context = {
        "confirmation_score": 0.22,
        "strict_ready": False,
        "strict_confirmation_count": 0,
        "near_breakout": False,
        "trend_acceleration_ready": False,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 5.0,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=48,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        market_context={"broad_first_board_overflow": False},
    )
    probability, meta = promotion._build_first_board_probability(
        row,
        {"total_score": 48},
        sector,
        {
            "limit_up_count": 56,
            "first_board_count": 36,
            "board_height": 4,
            "sealed_ratio": 0.68,
            "broad_first_board_overflow": False,
        },
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        candidate_route=promotion._candidate_route_from_strict_route(
            route,
            sector=sector,
            bull_score=48,
            support_strength=43,
        ),
    )

    assert passed is True, blockers
    assert route == "strict_mainline_spread"
    assert meta["candidate_route"] == "mainline_spread_start"
    assert meta["sector_catalyst_spread"] is True
    assert meta["broad_rotation_route_points"] > 0
    item = {
        "candidate_route": "mainline_spread_start",
        "probability": max(probability, 0.052),
        "route_score": max(meta["route_score"], 42),
        "support_strength_score": 43,
        "sector_strength_score": 58,
        "main_net_inflow_pct": -1.2,
        "time_horizon": "watch",
        "probability_factors": {
            **meta,
            "market_risk_level": "hostile",
            "broad_rotation_member_setup": True,
            "sector_catalyst_spread": True,
            "strict_confirmation_count": 0,
        },
    }
    assert promotion._is_actionable_first_board_prediction(item) is True


def test_broad_rotation_member_setup_is_recordable_below_hostile_default_probability():
    item = {
        "candidate_route": "mainline_spread_start",
        "probability": 0.052,
        "route_score": 42,
        "support_strength_score": 43,
        "sector_strength_score": 68,
        "main_net_inflow_pct": -2.0,
        "time_horizon": "watch",
        "probability_factors": {
            "market_risk_level": "hostile",
            "market_broad_first_board_overflow": True,
            "broad_rotation_member_setup": True,
            "strict_confirmation_count": 1,
        },
    }

    assert promotion._is_actionable_first_board_prediction(item) is True


def test_first_board_catalyst_routes_do_not_require_strict_confirmation_count():
    kline_context = {
        "confirmation_score": 0.22,
        "strict_ready": False,
        "strict_confirmation_count": 0,
        "near_breakout": False,
        "failed_reversal_count": 0,
        "provisional_last_bar": False,
        "overhead_gap_pct": 4.8,
    }
    base_sector = {
        "strength_score": 34,
        "consecutive_days": 2,
        "limit_up_count": 2,
        "fund_flow": 2.8,
        "change_pct": 1.4,
    }
    cases = [
        (
            {
                "event_types": ["news_catalyst"],
                "detail": {
                    "support_strength_score": 44,
                    "main_net_inflow_pct": -0.8,
                    "volume_ratio": 1.05,
                    "change_pct": 2.4,
                    "amplitude": 4.2,
                    "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 18,
                },
            },
            base_sector,
            {"limit_up_count": 38, "board_height": 4, "sealed_ratio": 0.72},
            "strict_news_catalyst",
        ),
        (
            {
                "event_types": ["auction_surge"],
                "detail": {
                    "support_strength_score": 44,
                    "main_net_inflow_pct": 0.5,
                    "volume_ratio": 1.25,
                    "change_pct": 3.2,
                    "amplitude": 5.0,
                    "auction_feed_complete": True,
                    "auction_evidence_status": "ok",
                    "auction_evidence_contract": "auction_provenance_v1",
                    "auction_strength_score": promotion.AUCTION_SURGE_MIN_SCORE + 12,
                    "auction_open_change": promotion.AUCTION_SURGE_MIN_OPEN_CHANGE + 0.6,
                    "auction_cancelled": False,
                },
            },
            base_sector,
            {"limit_up_count": 38, "board_height": 4, "sealed_ratio": 0.72},
            "strict_auction_surge",
        ),
        (
            {
                "event_types": ["mainline_spread"],
                "detail": {
                    "support_strength_score": 43,
                    "main_net_inflow_pct": 0.2,
                    "volume_ratio": 0.62,
                    "change_pct": 1.3,
                    "amplitude": 3.8,
                },
            },
            {
                "strength_score": 53,
                "consecutive_days": 2,
                "limit_up_count": 4,
                "fund_flow": 4.2,
                "change_pct": 1.9,
                "sector_low_position_rotation": True,
                "sector_rotation_score": 62,
                "sector_limit_up_delta": 3.0,
                "sector_strength_delta": 14.0,
            },
            {
                "limit_up_count": 96,
                "first_board_count": 72,
                "board_height": 4,
                "sealed_ratio": 0.7,
                "broad_first_board_overflow": True,
            },
            "strict_mainline_spread",
        ),
    ]

    for row, sector, market_context, expected_route in cases:
        passed, blockers, route = promotion._passes_strict_first_board_gate(
            row,
            bull_score=54,
            sector=sector,
            kline_context=kline_context,
            memory_features={"memory_score": 0},
            market_context=market_context,
        )
        probability, meta = promotion._build_first_board_probability(
            row,
            {"total_score": 54},
            sector,
            market_context,
            kline_context=kline_context,
            memory_features={"memory_score": 0},
            candidate_route=promotion._candidate_route_from_strict_route(
                route,
                sector=sector,
                bull_score=54,
                support_strength=row["detail"]["support_strength_score"],
            ),
        )

        assert passed is True, blockers
        assert "K线结构共振不足" not in blockers
        assert route == expected_route
        assert probability > 0
        assert meta["strict_confirmation_count"] == 0


def test_pre_board_probe_relieves_failed_reversal_only_when_route_quality_is_strong():
    strong_row = {
        "event_types": ["pre_board_probe", "breakthrough"],
        "detail": {
            "support_strength_score": 78,
            "main_net_inflow_pct": -3.5,
            "volume_ratio": 0.72,
            "change_pct": 3.8,
            "amplitude": 6.2,
            "pre_board_probe_score": promotion.PRE_BOARD_PROBE_MIN_SCORE + 18,
            "intraday_high_pct": 6.8,
            "preheat_breakout": True,
        },
    }
    weak_row = {
        "event_types": ["pre_board_probe", "breakthrough"],
        "detail": {
            "support_strength_score": 50,
            "main_net_inflow_pct": -6.2,
            "volume_ratio": 0.72,
            "change_pct": 2.4,
            "amplitude": 5.0,
            "pre_board_probe_score": promotion.PRE_BOARD_PROBE_MIN_SCORE + 2,
            "intraday_high_pct": 5.0,
            "preheat_breakout": True,
        },
    }
    sector = {
        "strength_score": 62,
        "consecutive_days": 2,
        "limit_up_count": 4,
        "fund_flow": 2.6,
        "change_pct": 1.8,
    }
    kline_context = {
        "confirmation_score": 0.34,
        "strict_ready": False,
        "strict_confirmation_count": 1,
        "near_breakout": True,
        "failed_reversal_count": 1,
        "provisional_last_bar": False,
        "overhead_gap_pct": 2.0,
    }

    strong_passed, strong_blockers, strong_route = promotion._passes_strict_first_board_gate(
        strong_row,
        bull_score=78,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        market_context={"broad_first_board_overflow": True},
    )
    weak_passed, weak_blockers, weak_route = promotion._passes_strict_first_board_gate(
        weak_row,
        bull_score=54,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        market_context={"broad_first_board_overflow": True},
    )

    assert strong_route == "strict_pre_board_probe"
    assert strong_passed is True, strong_blockers
    assert "上冲回落风险" not in strong_blockers
    assert "试盘量能不足" not in strong_blockers
    assert weak_route == "strict_pre_board_probe"
    assert weak_passed is False
    assert "上冲回落风险" in weak_blockers


def test_strict_first_board_gate_allows_oversold_reversal_after_panic_flush():
    row = {
        "event_types": ["oversold_reversal"],
        "detail": {
            "support_strength_score": 48,
            "main_net_inflow_pct": -1.2,
            "volume_ratio": 1.55,
            "change_pct": -5.8,
            "amplitude": 8.4,
            "oversold_reversal_score": 66,
            "upper_gap_pct": 6.5,
            "panic_flush": True,
        },
    }
    sector = {
        "strength_score": 18,
        "consecutive_days": 0,
        "limit_up_count": 0,
        "fund_flow": -0.6,
        "change_pct": -0.8,
    }
    kline_context = {
        "confirmation_score": 0.28,
        "strict_ready": False,
        "strict_confirmation_count": 1,
        "failed_reversal_count": 1,
        "provisional_last_bar": False,
        "overhead_gap_pct": 6.2,
    }

    passed, blockers, route = promotion._passes_strict_first_board_gate(
        row,
        bull_score=52,
        sector=sector,
        kline_context=kline_context,
        memory_features={"memory_score": 0},
    )
    probability, meta = promotion._build_first_board_probability(
        row,
        {"total_score": 52},
        sector,
        {"limit_up_count": 36, "board_height": 7, "sealed_ratio": 0.33, "market_risk_level": "hostile"},
        kline_context=kline_context,
        memory_features={"memory_score": 0},
        candidate_route=promotion._candidate_route_from_strict_route(route, sector=sector, bull_score=52, support_strength=48),
    )

    assert passed is True
    assert blockers == []
    assert route == "strict_oversold_reversal"
    assert meta["candidate_route"] == "oversold_reversal_start"
    assert probability > 0


@pytest.mark.asyncio
async def test_first_board_context_loaders_reject_legacy_news_but_keep_auction(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        session.add_all(
            [
                FinanceNews(
                    source="cninfo",
                    title="000888 签订重大算力订单",
                    content="公司披露重大合同，相关代码 000888。",
                    publish_time=datetime(2026, 5, 20, 21, 0),
                    sentiment="positive",
                    importance=8,
                    related_codes='["000888"]',
                    related_sectors='["算力"]',
                    impact_scope="stock",
                    bull_bear="bull",
                    bull_bear_confidence=0.82,
                    impact_reason="订单金额较大，可能触发独立催化。",
                ),
                FinanceNews(
                    source="em",
                    title="000887 被监管处罚",
                    content="公司收到处罚决定，相关代码 000887。",
                    publish_time=datetime(2026, 5, 20, 20, 30),
                    sentiment="negative",
                    importance=9,
                    related_codes='["000887"]',
                    related_sectors='["算力"]',
                    impact_scope="stock",
                    bull_bear="bear",
                    bull_bear_confidence=0.9,
                    impact_reason="负面高重要度新闻不能进入利好催化。",
                ),
                FinanceNews(
                    source="cls",
                    title="机器人产业链获重大政策支持",
                    content="政策支持机器人关键零部件和智能制造应用。",
                    publish_time=datetime(2026, 5, 20, 22, 0),
                    sentiment="positive",
                    importance=8,
                    related_codes="[]",
                    related_sectors='["机器人"]',
                    impact_scope="sector",
                    bull_bear="bull",
                    bull_bear_confidence=0.86,
                    impact_reason="板块级利好，需要从成分股池反推候选。",
                ),
                SectorInfo(
                    sector_code="S_ROBOT",
                    sector_name="机器人",
                    sector_type="concept",
                    source="test",
                    is_excluded=0,
                ),
                StockSectorMapping(
                    code="000886",
                    sector_code="S_ROBOT",
                    sector_name="机器人",
                    sector_type="concept",
                    source="test",
                ),
                SectorPersistence(
                    sector_code="S_ROBOT",
                    sector_name="机器人",
                    trade_date=trade_date,
                    strength_score=78,
                    limit_up_count=6,
                    fund_flow=8.2,
                    change_pct=3.1,
                    consecutive_days=2,
                ),
                StockSpot(
                    code="000886",
                    name="机器人候选",
                    price=12.2,
                    prev_close=11.8,
                    open=11.9,
                    change_pct=3.39,
                    volume_ratio=2.1,
                    turnover=4.8,
                    support_strength_score=66,
                ),
                AuctionData(
                    code="000889",
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    auction_price=11.3,
                    auction_volume=2_000_000,
                    auction_amount=32_000_000,
                    prev_close=10.9,
                    open_change=3.67,
                    volume_ratio=2.8,
                    is_cancelled=False,
                ),
            ]
        )
        await session.commit()

        news_map = await promotion._load_news_catalyst_context_map(session, trade_date, exclude_codes=set())
        auction_map = await promotion._load_auction_surge_context_map(session, trade_date, exclude_codes=set())

    # FinanceNews只是可变展示表：旧强利好、当前完整板块/行情均不能补出PIT版本。
    assert news_map == {}
    assert auction_map["000889"]["auction_strength_score"] >= promotion.AUCTION_SURGE_MIN_SCORE
    assert auction_map["000889"]["auction_open_change"] == 3.67


@pytest.mark.asyncio
async def test_news_loader_treats_explicit_end_time_as_hard_cutoff(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)

    direct_calls = []

    async def empty_direct_map(*_args, **kwargs):
        direct_calls.append(kwargs)
        return {}

    async def sector_codes(*_args, **_kwargs):
        raise AssertionError("不得用当前板块映射/行情补出历史新闻候选")

    monkeypatch.setattr(promotion, "load_direct_stock_catalyst_map", empty_direct_map)
    monkeypatch.setattr(promotion, "_load_sector_news_candidate_codes", sector_codes)

    async with SessionLocal() as session:
        session.add_all(
            [
                FinanceNews(
                    source="cls",
                    title="机器人产业链获重大政策支持",
                    content="政策支持关键零部件和智能制造应用。",
                    publish_time=datetime(2026, 5, 20, 9, 0),
                    sentiment="positive",
                    importance=9,
                    related_codes="[]",
                    related_sectors='["机器人"]',
                    impact_scope="sector",
                    bull_bear="bull",
                    bull_bear_confidence=0.9,
                ),
                FinanceNews(
                    source="cls",
                    title="算力产业链午间获重大政策支持",
                    content="午间发布的行业政策不能进入09:25预测。",
                    publish_time=datetime(2026, 5, 20, 11, 30),
                    sentiment="positive",
                    importance=9,
                    related_codes="[]",
                    related_sectors='["算力"]',
                    impact_scope="sector",
                    bull_bear="bull",
                    bull_bear_confidence=0.9,
                ),
            ]
        )
        await session.commit()
        news_map = await promotion._load_news_catalyst_context_map(
            session,
            trade_date,
            exclude_codes=set(),
            news_end_time=datetime(2026, 5, 20, 9, 25),
        )

    assert news_map == {}
    assert len(direct_calls) == 1
    assert direct_calls[0]["news_end_time"] == datetime(2026, 5, 20, 9, 25)


@pytest.mark.asyncio
async def test_auction_snapshot_health_does_not_join_same_clock_from_other_dates(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        session.add_all(
            [
                AuctionData(
                    code="000890",
                    trade_date=date(2026, 5, 19),
                    auction_time="09:25:00",
                    auction_price=10.3,
                    prev_close=10.0,
                    open_change=3.0,
                    auction_volume=280_000,
                    auction_amount=28_000_000,
                    volume_ratio=2.2,
                ),
                AuctionData(
                    code="000890",
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    auction_price=10.3,
                    prev_close=10.0,
                    open_change=3.0,
                    auction_volume=0,
                    auction_amount=0,
                    volume_ratio=0,
                ),
                AuctionData(
                    code="000891",
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    **verified_auction_fields(trade_date, "09:25:00"),
                    auction_price=8.24,
                    prev_close=8.0,
                    open_change=3.0,
                    auction_volume=180_000,
                    auction_amount=18_000_000,
                    volume_ratio=1.8,
                ),
            ]
        )
        await session.commit()

        health = await promotion._build_auction_snapshot_health(session, trade_date)
        from app.strategy.auction import auction_collector

        collector_health = await auction_collector.get_snapshot_health(
            session,
            trade_date,
        )

    assert health["latest_code_count"] == 2
    assert health["feed_complete_count"] == 1
    assert health["feed_complete_ratio"] == 0.5
    assert health["status"] == "degraded"
    assert collector_health["snapshot_count"] == 2
    assert collector_health["feed_complete_count"] == 1
    assert collector_health["status"] == "degraded"


@pytest.mark.asyncio
async def test_auction_snapshot_health_marks_pre_final_frame_stale(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        session.add(
            AuctionData(
                code="000892",
                trade_date=trade_date,
                auction_time="09:20:00",
                **verified_auction_fields(trade_date, "09:20:00"),
                auction_price=10.3,
                prev_close=10.0,
                open_change=3.0,
                auction_volume=280_000,
                auction_amount=28_000_000,
                volume_ratio=2.2,
            )
        )
        await session.commit()
        health = await promotion._build_auction_snapshot_health(session, trade_date)

    assert health["feed_complete_ratio"] == 1.0
    assert health["timely_snapshot_count"] == 0
    assert health["stale"] is True
    assert health["status"] == "stale"


@pytest.mark.asyncio
async def test_auction_loader_uses_latest_snapshot_instead_of_intraday_peak(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        session.add_all(
            [
                AuctionData(
                    code="000890",
                    trade_date=trade_date,
                    auction_time="09:15:00",
                    auction_price=11.0,
                    prev_close=10.0,
                    open_change=10.0,
                    volume_ratio=0.0,
                    auction_amount=0.0,
                    is_cancelled=False,
                ),
                AuctionData(
                    code="000890",
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    auction_price=10.12,
                    prev_close=10.0,
                    open_change=1.2,
                    volume_ratio=0.0,
                    auction_amount=0.0,
                    is_cancelled=False,
                ),
            ]
        )
        await session.commit()
        auction_map = await promotion._load_auction_surge_context_map(
            session,
            trade_date,
            exclude_codes=set(),
        )

    assert "000890" not in auction_map


@pytest.mark.asyncio
async def test_auction_loader_keeps_deep_water_reversal_as_watch_seed(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        session.add_all(
            [
                AuctionData(
                    code="000894",
                    trade_date=trade_date,
                    auction_time="09:20:00",
                    auction_price=9.5,
                    prev_close=10.0,
                    open_change=-5.0,
                    volume_ratio=0.0,
                    auction_amount=0.0,
                    is_cancelled=False,
                ),
                AuctionData(
                    code="000894",
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    auction_price=10.29,
                    prev_close=10.0,
                    open_change=2.9,
                    volume_ratio=0.0,
                    auction_amount=0.0,
                    is_cancelled=False,
                ),
                AuctionData(
                    code="000894",
                    trade_date=date(2026, 5, 19),
                    auction_time="09:20:00",
                    auction_price=10.4,
                    prev_close=10.0,
                    open_change=4.0,
                    volume_ratio=2.0,
                    auction_amount=20_000_000,
                    is_cancelled=False,
                ),
            ]
        )
        await session.commit()
        auction_map = await promotion._load_auction_surge_context_map(
            session,
            trade_date,
            exclude_codes=set(),
        )

    item = auction_map["000894"]
    assert item["auction_reversal_confirmed"] is True
    assert item["auction_open_change_delta"] == pytest.approx(7.9)
    assert item["prediction_shape_seed"] is True
    assert item["auction_feed_complete"] is False


@pytest.mark.asyncio
async def test_auction_loader_ignores_zero_price_minus_100_baseline(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        session.add_all([
            AuctionData(
                code="000897",
                trade_date=trade_date,
                auction_time="09:20:00",
                auction_price=0,
                prev_close=10.0,
                open_change=-100.0,
                volume_ratio=0.0,
                auction_amount=0.0,
                is_cancelled=False,
            ),
            AuctionData(
                code="000897",
                trade_date=trade_date,
                auction_time="09:25:00",
                **verified_auction_fields(trade_date, "09:25:00"),
                auction_price=10.29,
                prev_close=10.0,
                open_change=2.9,
                auction_volume=200_000,
                volume_ratio=2.0,
                auction_amount=20_000_000,
                is_cancelled=False,
            ),
        ])
        await session.commit()
        auction_map = await promotion._load_auction_surge_context_map(
            session,
            trade_date,
            exclude_codes=set(),
        )

    item = auction_map["000897"]
    assert item["auction_reversal_confirmed"] is False
    assert item["auction_baseline_open_change"] == pytest.approx(2.9)
    assert item["auction_open_change_delta"] == pytest.approx(0.0)
    assert item["auction_feed_complete"] is True


@pytest.mark.asyncio
async def test_auction_loader_promotes_sector_cluster_to_prediction_seed_without_fake_buy(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    leader_codes = ["000891", "000892", "000893", "000895"]
    follower_code = "000896"
    async with SessionLocal() as session:
        session.add(
            SectorInfo(
                sector_code="S_MED_CLUSTER",
                sector_name="创新药",
                sector_type="concept",
                source="test",
                is_excluded=0,
            )
        )
        for index, code in enumerate([*leader_codes, follower_code]):
            session.add(
                StockSectorMapping(
                    code=code,
                    sector_code="S_MED_CLUSTER",
                    sector_name="创新药",
                    sector_type="concept",
                    source="test",
                )
            )
            session.add(
                AuctionData(
                    code=code,
                    trade_date=trade_date,
                    auction_time="09:25:00",
                    auction_price=10.0 + index * 0.1,
                    prev_close=10.0,
                    open_change=(8.0, 6.5, 4.8, 3.2, 1.5)[index],
                    volume_ratio=None if code == follower_code else 0.0,
                    auction_amount=None if code == follower_code else 0.0,
                    is_cancelled=False,
                )
            )
        await session.commit()
        auction_map = await promotion._load_auction_surge_context_map(
            session,
            trade_date,
            exclude_codes=set(),
        )

    follower = auction_map[follower_code]
    assert follower["auction_cluster_confirmed"] is True
    assert follower["auction_sector_name"] == "创新药"
    assert follower["auction_sector_member_count"] == 4
    assert follower["auction_strength_score"] >= promotion.AUCTION_SURGE_MIN_SCORE
    assert follower["prediction_shape_seed"] is True
    assert follower["auction_feed_complete"] is False


@pytest.mark.asyncio
async def test_sector_rotation_keeps_active_mainline_washout_as_watch_seed(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        for offset, (strength, limit_ups, change_pct) in enumerate(
            [
                (48.0, 4, 1.1),
                (72.0, 6, 2.0),
                (44.0, 4, 0.4),
                (58.0, 5, 1.2),
                (30.0, 3, -0.2),
                (4.0, 3, -2.8),
            ]
        ):
            session.add(
                SectorPersistence(
                    sector_code="S_WASHOUT",
                    sector_name="创新药",
                    trade_date=date(2026, 5, 15 + offset),
                    strength_score=strength,
                    limit_up_count=limit_ups,
                    fund_flow=10.0 if change_pct > 0 else -30.0,
                    change_pct=change_pct,
                    consecutive_days=8 + offset,
                )
            )
        session.add_all(
            [
                SectorPersistence(
                    sector_code="S_DEAD",
                    sector_name="旧题材",
                    trade_date=date(2026, 5, 17),
                    strength_score=68,
                    limit_up_count=5,
                    fund_flow=8,
                    change_pct=1.5,
                    consecutive_days=5,
                ),
                SectorPersistence(
                    sector_code="S_DEAD",
                    sector_name="旧题材",
                    trade_date=trade_date,
                    strength_score=2,
                    limit_up_count=0,
                    fund_flow=-20,
                    change_pct=-3.0,
                    consecutive_days=0,
                ),
            ]
        )
        await session.commit()
        context = await promotion._load_sector_rotation_context(
            session,
            trade_date,
            ["S_WASHOUT", "S_DEAD"],
        )

    assert context["S_WASHOUT"]["sector_washout_reversal_watch"] is True
    assert context["S_WASHOUT"]["sector_crowded_stale_theme"] is False
    assert context["S_WASHOUT"]["sector_recent_active_days"] >= 3
    assert context["S_DEAD"]["sector_washout_reversal_watch"] is False


def test_broad_rotation_row_marks_washout_seed_as_watch_only():
    spot = StockSpot(
        code="000897",
        name="洗盘候选",
        price=9.6,
        prev_close=10.0,
        open=9.8,
        high=10.1,
        low=9.4,
        change_pct=-4.0,
        volume_ratio=0.8,
        turnover=3.2,
        support_strength_score=54,
    )
    sector = {
        "sector_name": "创新药",
        "strength_score": 4,
        "limit_up_count": 3,
        "sector_rotation_score": 42,
        "sector_washout_reversal_watch": True,
        "sector_washout_score": 76,
        "sector_recent_active_days": 5,
        "sector_recent_limit_up_sum": 24,
        "sector_retained_leader_ratio": 0.5,
    }
    setattr(spot, "_promotion_washout_reserved", True)
    row = promotion._build_broad_rotation_first_board_row(
        spot,
        sector=sector,
        bull_row={"total_score": 58, "top_signals": []},
        kline_context={"summary": "回踩支撑"},
        market_context={"broad_first_board_overflow": False},
    )

    assert row["detail"]["sector_washout_reversal_watch"] is True
    assert row["detail"]["prediction_shape_seed"] is True
    assert row["setup_grade"] == "B类观察候选"
    assert row["buy_point_pushable"] is False


def test_prediction_shape_watch_probability_caps_require_auction_volume_confirmation():
    close_only_washout = {
        "prediction_shape_seed": True,
        "sector_washout_reversal_watch": True,
    }
    incomplete_cluster = {
        **close_only_washout,
        "auction_cluster_confirmed": True,
        "auction_feed_complete": False,
    }
    complete_cluster = {
        **close_only_washout,
        "auction_cluster_confirmed": True,
        "auction_feed_complete": True,
        "auction_evidence_status": "ok",
        "auction_evidence_contract": "auction_provenance_v1",
    }

    assert promotion._prediction_shape_watch_probability_cap(close_only_washout) == 0.08
    assert promotion._prediction_shape_watch_probability_cap(incomplete_cluster) == 0.12
    assert promotion._prediction_shape_watch_probability_cap(complete_cluster) == 0.16


@pytest.mark.asyncio
async def test_pre_board_probe_loader_detects_probe_and_oversold_reversal(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    async with SessionLocal() as session:
        session.add_all(
            [
                StockSpot(code="000881", name="试盘股"),
                StockSpot(code="000882", name="反包股"),
                StockSpot(code="300881", name="创业板观察股"),
            ]
        )
        for index, trade_day in enumerate(
            [
                date(2026, 5, 13),
                date(2026, 5, 14),
                date(2026, 5, 15),
                date(2026, 5, 18),
                date(2026, 5, 19),
            ]
        ):
            session.add_all(
                [
                    StockKline(
                        code="000881",
                        trade_date=trade_day,
                        open=10 + index * 0.02,
                        close=10.05 + index * 0.02,
                        high=10.1 + index * 0.02,
                        low=9.95 + index * 0.02,
                        volume=100_000,
                        turnover=2.0,
                        change_pct=0.4,
                        prev_close=10 + index * 0.02,
                        source="ths",
                    ),
                    StockKline(
                        code="000882",
                        trade_date=trade_day,
                        open=12 - index * 0.02,
                        close=11.95 - index * 0.02,
                        high=12.05 - index * 0.02,
                        low=11.9 - index * 0.02,
                        volume=120_000,
                        turnover=2.2,
                        change_pct=-0.2,
                        prev_close=12 - index * 0.02,
                        source="ths",
                    ),
                    StockKline(
                        code="300881",
                        trade_date=trade_day,
                        open=10 + index * 0.02,
                        close=10.05 + index * 0.02,
                        high=10.1 + index * 0.02,
                        low=9.95 + index * 0.02,
                        volume=100_000,
                        turnover=2.0,
                        change_pct=0.4,
                        prev_close=10 + index * 0.02,
                        source="ths",
                    ),
                ]
            )
        session.add_all(
            [
                StockKline(
                    code="000881",
                    trade_date=trade_date,
                    open=10.2,
                    close=10.7,
                    high=10.95,
                    low=10.1,
                    volume=260_000,
                    turnover=6.0,
                    change_pct=5.2,
                    prev_close=10.17,
                    source="ths",
                ),
                StockKline(
                    code="000882",
                    trade_date=trade_date,
                    open=11.8,
                    close=11.3,
                    high=12.1,
                    low=11.25,
                    volume=280_000,
                    turnover=7.0,
                    change_pct=-5.8,
                    prev_close=12.0,
                    source="ths",
                ),
                StockKline(
                    code="300881",
                    trade_date=trade_date,
                    open=10.2,
                    close=10.7,
                    high=10.95,
                    low=10.1,
                    volume=260_000,
                    turnover=6.0,
                    change_pct=5.2,
                    prev_close=10.17,
                    source="ths",
                ),
            ]
        )
        await session.commit()

        context_map = await promotion._load_pre_board_probe_context_map(session, trade_date, exclude_codes=set())

    assert context_map["000881"]["candidate_route"] == "pre_board_probe_start"
    assert context_map["000881"]["pre_board_probe_score"] >= promotion.PRE_BOARD_PROBE_MIN_SCORE
    assert context_map["000882"]["candidate_route"] == "oversold_reversal_start"
    assert context_map["000882"]["oversold_reversal_score"] >= promotion.OVERSOLD_REVERSAL_MIN_SCORE
    assert "300881" not in context_map


@pytest.mark.asyncio
async def test_pre_board_probe_loader_reconciles_deep_wash_close_from_next_spot(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    trade_date = date(2026, 5, 20)
    closes = [10.0 + index * 0.08 for index in range(14)] + [
        12.10, 12.55, 13.05, 13.55, 14.05, 13.55,
    ]
    volumes = [1_000_000.0] * 14 + [
        1_300_000, 1_450_000, 1_600_000, 1_800_000, 2_000_000, 2_600_000,
    ]
    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000883",
                name="深洗测试股",
                price=13.2,
                prev_close=13.15,
                updated_at=datetime(2026, 5, 21, 9, 20),
            )
        )
        session.add(
            StockSpot(
                code="000885",
                name="未来快照测试股",
                price=13.2,
                prev_close=13.15,
                updated_at=datetime(2026, 5, 22, 9, 20),
            )
        )
        for index, close_price in enumerate(closes):
            bar_date = trade_date - timedelta(days=len(closes) - 1 - index)
            previous_close = closes[index - 1] if index else close_price * 0.995
            session.add(
                StockKline(
                    code="000883",
                    trade_date=bar_date,
                    open=close_price * (1.01 if index == len(closes) - 1 else 0.995),
                    close=close_price,
                    high=max(close_price, previous_close) * 1.02,
                    low=min(close_price, previous_close) * 0.98,
                    volume=volumes[index],
                    amount=volumes[index] * close_price,
                    turnover=6.0 if index == len(closes) - 1 else 3.0,
                    change_pct=(close_price / previous_close - 1.0) * 100.0,
                    prev_close=previous_close,
                    source="ths",
                )
            )
            session.add(
                StockKline(
                    code="000885",
                    trade_date=bar_date,
                    open=close_price * (1.01 if index == len(closes) - 1 else 0.995),
                    close=close_price,
                    high=max(close_price, previous_close) * 1.02,
                    low=min(close_price, previous_close) * 0.98,
                    volume=volumes[index],
                    amount=volumes[index] * close_price,
                    turnover=6.0 if index == len(closes) - 1 else 3.0,
                    change_pct=(close_price / previous_close - 1.0) * 100.0,
                    prev_close=previous_close,
                    source="ths",
                )
            )
        # T+1 市场日锚点让 next spot.prev_close 修复可被严格审计。
        session.add(
            StockKline(
                code="000884",
                trade_date=trade_date + timedelta(days=1),
                open=10.0,
                close=10.1,
                high=10.2,
                low=9.9,
                volume=1_000_000,
                amount=10_100_000,
                turnover=2.0,
                change_pct=1.0,
                prev_close=10.0,
                source="ths",
            )
        )
        await session.commit()

        context_map = await promotion._load_pre_board_probe_context_map(
            session,
            trade_date,
            exclude_codes=set(),
        )

    item = context_map["000883"]
    assert item["kline_close_reconciled_from_next_spot"] is True
    assert item["change_pct"] == pytest.approx((13.15 / 14.05 - 1.0) * 100.0, abs=0.01)
    future_item = context_map["000885"]
    assert future_item["kline_close_reconciled_from_next_spot"] is False
    assert future_item["change_pct"] == pytest.approx(
        (13.55 / 14.05 - 1.0) * 100.0,
        abs=0.01,
    )
    assert item["pre_board_precursor_type"] == "strong_trend_deep_wash"
    assert item["prediction_shape_seed"] is True
    assert item["strong_first_bearish_ready"] is True
    assert (
        item["strong_first_bearish_stats"][
            "pullback_technical_persistence_score"
        ]
        >= 66
    )
    row = promotion._build_pre_board_probe_row(
        item,
        sector={"sector_name": "测试板块", "strength_score": 60},
        bull_row={"total_score": 70},
    )
    assert row["detail"]["strong_first_bearish_ready"] is True
    assert row["detail"]["strong_first_bearish_stats"] == item[
        "strong_first_bearish_stats"
    ]


def test_annotate_first_board_timing_marks_support_squeeze_sprint_and_watch_layers():
    sprint_item = {
        "candidate_route": "support_squeeze_start",
        "probability": 0.36,
        "route_score": 62.5,
        "support_strength_score": 46.0,
        "sector_strength_score": 48.0,
        "change_pct": 0.8,
        "volume_ratio": 0.92,
        "signal_status": "close_confirmed",
        "support_squeeze_ready": True,
        "support_zone_gap_pct": 1.8,
        "support_hold_days": 5,
        "sub_probabilities": {"breakout_3d": 0.28},
        "probability_factors": {
            "strict_confirmation_count": 4,
            "qualified_doji_confirmation": True,
        },
    }
    watch_item = {
        "candidate_route": "support_squeeze_start",
        "probability": 0.24,
        "route_score": 53.0,
        "support_strength_score": 37.0,
        "sector_strength_score": 31.0,
        "change_pct": 0.2,
        "volume_ratio": 0.93,
        "signal_status": "close_confirmed",
        "support_squeeze_ready": True,
        "support_zone_gap_pct": 1.6,
        "support_hold_days": 5,
        "sub_probabilities": {"breakout_3d": 0.19},
        "probability_factors": {
            "strict_confirmation_count": 3,
            "qualified_doji_confirmation": False,
        },
    }

    annotated_sprint = promotion._annotate_first_board_timing(sprint_item)
    annotated_watch = promotion._annotate_first_board_timing(watch_item)

    assert annotated_sprint["time_horizon"] == "sprint"
    assert annotated_sprint["time_horizon_label"] == "支撑位首波冲刺"
    assert annotated_sprint["candidate_route_label"] == "支撑位首波冲刺"
    assert annotated_sprint["support_squeeze_stage"] == "support_squeeze_sprint"
    assert annotated_sprint["watch_bucket"] == ""
    assert annotated_sprint["support_squeeze_gap_label"] == "差封板时机"
    assert "日内涨幅先抬到 1.2%+" in annotated_sprint["next_threshold_hint"]
    assert any("承接" in item for item in annotated_sprint["actionable_suggestions"])

    assert annotated_watch["time_horizon"] == "watch"
    assert annotated_watch["time_horizon_label"] == "支撑位观察预备"
    assert annotated_watch["candidate_route_label"] == "支撑位观察预备"
    assert annotated_watch["watch_bucket"] == "support_squeeze_watch"
    assert annotated_watch["watch_bucket_label"] == "支撑位观察预备"
    assert annotated_watch["support_squeeze_stage"] == "support_squeeze_watch"
    assert annotated_watch["support_squeeze_gap_label"] == "差涨幅"
    assert "日内涨幅至少回到 0.6%+" in annotated_watch["next_threshold_hint"]
    assert any("承接" in item for item in annotated_watch["actionable_suggestions"])
    assert any("确认K" in item for item in annotated_watch["actionable_suggestions"])


def test_support_squeeze_sprint_requires_real_confirmation_k_and_min_change_pct():
    item = {
        "candidate_route": "support_squeeze_start",
        "probability": 0.38,
        "route_score": 63.0,
        "support_strength_score": 47.0,
        "sector_strength_score": 46.0,
        "change_pct": 0.4,
        "volume_ratio": 0.92,
        "signal_status": "close_confirmed",
        "support_squeeze_ready": True,
        "support_zone_gap_pct": 1.8,
        "support_hold_days": 5,
        "sub_probabilities": {"breakout_3d": 0.29},
        "probability_factors": {
            "strict_confirmation_count": 4,
            "qualified_doji_confirmation": False,
        },
    }

    assert promotion._is_support_squeeze_sprint_candidate(item) is False


def test_support_volume_release_sprint_requires_change_pct_above_one_percent():
    item = {
        "candidate_route": "support_squeeze_start",
        "probability": 0.38,
        "route_score": 60.0,
        "support_strength_score": 49.0,
        "sector_strength_score": 44.0,
        "change_pct": 0.62,
        "volume_ratio": 1.18,
        "signal_status": "close_confirmed",
        "support_squeeze_ready": True,
        "support_volume_release_ready": True,
        "support_zone_gap_pct": 1.9,
        "support_hold_days": 4,
        "sub_probabilities": {"breakout_3d": 0.26},
        "probability_factors": {
            "strict_confirmation_count": 3,
            "qualified_doji_confirmation": True,
            "support_squeeze_ready": True,
            "support_volume_release_ready": True,
        },
    }

    annotated = promotion._annotate_first_board_timing(item)

    assert promotion._is_support_squeeze_sprint_candidate(item) is False
    assert annotated["time_horizon"] == "watch"
    assert "日内涨幅至少回到 1.0%+" in annotated["next_threshold_hint"]


def test_hostile_market_keeps_oversold_reversal_in_weak_watch_even_when_route_score_is_strong():
    item = {
        "candidate_route": "oversold_reversal_start",
        "probability": 0.035,
        "route_score": 49.0,
        "support_strength_score": 76.0,
        "sector_strength_score": 30.0,
        "change_pct": -4.8,
        "volume_ratio": 1.2,
        "probability_factors": {
            "market_risk_level": "hostile",
            "strict_confirmation_count": 1,
            "oversold_reversal_score": 88.0,
        },
        "sub_probabilities": {"breakout_3d": 0.12},
    }

    annotated = promotion._annotate_first_board_timing(item)

    assert promotion._is_oversold_reversal_sprint_candidate(item) is False
    assert annotated["time_horizon"] == "weak_watch"
    assert promotion._is_actionable_first_board_prediction(annotated) is False


def test_platform_relaunch_needs_own_sprint_gate_instead_of_auto_sprint():
    item = {
        "candidate_route": "platform_relaunch",
        "prediction_mode": "strict_hot",
        "probability": 0.36,
        "route_score": 60.0,
        "support_strength_score": 50.0,
        "sector_strength_score": 46.0,
        "change_pct": 0.3,
        "volume_ratio": 1.1,
        "signal_status": "close_confirmed",
        "limit_up_anchor_gap_pct": 2.2,
        "sub_probabilities": {"breakout_3d": 0.28},
        "probability_factors": {
            "strict_confirmation_count": 3,
            "qualified_doji_confirmation": True,
            "platform_relaunch_event_ready": True,
            "limit_up_nearby_doji_confirmation": True,
            "limit_up_anchor_gap_pct": 2.2,
        },
    }

    annotated = promotion._annotate_first_board_timing(item)

    assert annotated["time_horizon"] == "watch"


def test_quiet_setup_between_point_eight_and_one_point_two_enters_pre_sprint():
    item = {
        "candidate_route": "quiet_setup",
        "prediction_mode": "strict_stealth_scan",
        "probability": 0.5,
        "route_score": 52.0,
        "support_strength_score": 50.0,
        "sector_strength_score": 24.0,
        "change_pct": 0.92,
        "volume_ratio": 0.94,
        "probability_factors": {
            "strict_confirmation_count": 4,
            "qualified_doji_confirmation": True,
            "overhead_gap_pct": 2.0,
        },
        "sub_probabilities": {"breakout_3d": 0.32},
    }

    annotated = promotion._annotate_first_board_timing(item)

    assert annotated["time_horizon"] == "pre_sprint"
    assert annotated["time_horizon_label"] == "准冲刺"


def test_build_first_board_diagnostics_groups_blocked_samples_by_primary_reason():
    diagnostics: dict[str, dict] = {}

    promotion._record_first_board_diagnostic(
        diagnostics,
        code="000001",
        name="主线弱票",
        blockers=["主线强度不足", "题材持续性不足"],
        signal_summary="capital / breakthrough",
        sector_strength_score=28,
        sector_continuity_score=0.12,
        support_strength_score=63,
        memory_score=52,
        bull_score=78,
        diagnostic_route="strict_hot",
    )
    promotion._record_first_board_diagnostic(
        diagnostics,
        code="000002",
        name="没记忆",
        blockers=["缺少首波记忆或主线共振", "量能形态不符合静默蓄势"],
        signal_summary="stealth_setup",
        sector_strength_score=48,
        support_strength_score=51,
        memory_score=0,
        bull_score=70,
        volume_ratio=0.42,
        diagnostic_route="strict_stealth_scan",
    )
    promotion._record_first_board_diagnostic(
        diagnostics,
        code="000003",
        name="量能差",
        blockers=["突破量能不足", "承接分不足"],
        signal_summary="breakthrough",
        sector_strength_score=72,
        support_strength_score=45,
        memory_score=40,
        bull_score=82,
        volume_ratio=0.86,
        diagnostic_route="strict_hot",
    )
    promotion._record_first_board_diagnostic(
        diagnostics,
        code="000004",
        name="结构差",
        blockers=["K线结构共振不足", "距离前高仍偏远"],
        signal_summary="rapid_rise",
        sector_strength_score=75,
        support_strength_score=62,
        memory_score=55,
        bull_score=84,
        strict_confirmation_count=1,
        overhead_gap_pct=5.2,
        diagnostic_route="strict_stealth",
    )

    payload = promotion._build_first_board_diagnostics_payload(diagnostics, limit_per_reason=2)

    assert payload["blocked_total"] == 4
    assert [item["reason"] for item in payload["reason_summary"]] == [
        "缺少首波记忆",
        "主线强度不足",
        "量能不够",
        "K线结构不够",
    ]
    reason_map = {item["reason"]: item for item in payload["blocked_reason_groups"]}
    overview = payload["overview"]
    assert overview["dominant_reason"] == "缺少首波记忆"
    assert "今日首板" in overview["headline"]
    assert overview["top_focuses"][0]["key"] == "memory_score"
    assert reason_map["缺少首波记忆"]["examples"][0]["code"] == "000002"
    assert reason_map["主线强度不足"]["examples"][0]["code"] == "000001"
    assert reason_map["量能不够"]["examples"][0]["code"] == "000003"
    assert reason_map["K线结构不够"]["examples"][0]["code"] == "000004"
    assert reason_map["缺少首波记忆"]["playbook"]
    assert "首波记忆分至少到 28+" in reason_map["缺少首波记忆"]["examples"][0]["next_threshold_hint"]
    assert "主线强度至少回到 55+" in reason_map["主线强度不足"]["examples"][0]["next_threshold_hint"]
    assert "承接分至少到 58+" in reason_map["量能不够"]["examples"][0]["actionable_suggestions"][1]
    assert "结构确认项至少补到 3 项" in reason_map["K线结构不够"]["examples"][0]["next_threshold_hint"]


def test_split_first_board_candidates_separates_sprint_pre_sprint_watch_and_weak_watch():
    sprint, pre_sprint, watch, weak_watch = promotion._split_first_board_candidates(
        [
            {
                "code": "000001",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.76,
                "support_strength_score": 58,
                "sector_strength_score": 26,
                "change_pct": 1.8,
                "volume_ratio": 1.2,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 1.2,
                },
            },
            {
                "code": "000002",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.66,
                "support_strength_score": 44,
                "sector_strength_score": 23,
                "change_pct": -0.3,
                "volume_ratio": 0.9,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 1.4,
                },
            },
            {
                "code": "000003",
                "candidate_route": "quiet_setup",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.5,
                "route_score": 52.0,
                "support_strength_score": 50,
                "sector_strength_score": 22,
                "change_pct": 0.96,
                "volume_ratio": 0.94,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 2.0,
                },
                "sub_probabilities": {"breakout_3d": 0.31},
            },
        ]
    )

    assert sprint[0]["code"] == "000001"
    assert sprint[0]["time_horizon"] == "sprint"
    assert pre_sprint[0]["code"] == "000003"
    assert pre_sprint[0]["time_horizon"] == "pre_sprint"
    assert weak_watch[0]["code"] == "000002"
    assert weak_watch[0]["time_horizon"] == "weak_watch"
    assert watch == []


def test_split_first_board_candidates_deduplicates_same_stock_routes():
    sprint, pre_sprint, watch, weak_watch = promotion._split_first_board_candidates(
        [
            {
                "code": "000636",
                "name": "风华高科",
                "candidate_route": "news_catalyst_start",
                "time_horizon": "sprint",
                "probability": 0.52,
                "route_score": 71.0,
                "support_strength_score": 82,
                "sector_strength_score": 79,
                "change_pct": 3.0,
                "volume_ratio": 1.3,
                "probability_factors": {
                    "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 20,
                },
            },
            {
                "code": "000636",
                "name": "风华高科",
                "candidate_route": "mainline_spread_start",
                "time_horizon": "sprint",
                "probability": 0.71,
                "route_score": 75.0,
                "support_strength_score": 86,
                "sector_strength_score": 80,
                "change_pct": 3.2,
                "volume_ratio": 1.25,
                "probability_factors": {
                    "market_broad_first_board_overflow": True,
                    "broad_rotation_cluster_setup": True,
                    "strict_confirmation_count": 3,
                },
            },
        ]
    )

    all_rows = sprint + pre_sprint + watch + weak_watch
    assert [item["code"] for item in all_rows] == ["000636"]
    assert all_rows[0]["candidate_route"] == "mainline_spread_start"


def test_split_first_board_candidates_backfills_display_when_sprint_is_too_thin():
    sprint, pre_sprint, watch, weak_watch = promotion._split_first_board_candidates(
        [
            {
                "code": "000001",
                "time_horizon": "sprint",
                "time_horizon_label": "1-2日冲刺",
                "probability": 0.58,
                "route_score": 71.0,
            },
            {
                "code": "000002",
                "time_horizon": "pre_sprint",
                "time_horizon_label": "准冲刺",
                "probability": 0.36,
                "route_score": 62.0,
                "main_uptrend_score": 55.0,
            },
            {
                "code": "000003",
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "probability": 0.31,
                "route_score": 58.0,
                "main_uptrend_score": 50.0,
            },
        ],
        min_display_count=3,
    )

    assert [item["code"] for item in sprint] == ["000001", "000002", "000003"]
    assert sprint[1]["display_bucket"] == "pre_sprint_backfill"
    assert pre_sprint == []
    assert watch == []
    assert weak_watch == []


def test_split_first_board_candidates_display_backfills_when_hostile_market_blocks_actionable():
    sprint, pre_sprint, watch, weak_watch = promotion._split_first_board_candidates(
        [
            {
                "code": "000031",
                "candidate_route": "platform_relaunch",
                "time_horizon": "watch",
                "probability": 0.02,
                "route_score": 50.0,
                "support_strength_score": 45,
                "sector_strength_score": 49,
                "main_net_inflow_pct": 0.8,
                "change_pct": 1.4,
                "volume_ratio": 0.9,
                "probability_factors": {
                    "market_risk_level": "hostile",
                    "strict_confirmation_count": 3,
                },
            }
        ]
    )

    assert len(sprint) == 1
    assert sprint[0]["code"] == "000031"
    assert sprint[0]["display_only"] is True
    assert sprint[0]["display_bucket"] == "watch_backfill"
    assert pre_sprint == []
    assert watch == []
    assert weak_watch == []


def test_rank_first_board_candidates_excludes_watch_candidates():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "000001",
                "prediction_mode": "strict",
                "probability": 0.55,
                "route_score": 72,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 82,
                "display_score": 90,
            },
            {
                "code": "000002",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.58,
                "route_score": 74,
                "setup_grade": "B类观察候选",
                "bull_score": 75,
                "display_score": 88,
                "support_strength_score": 44,
                "sector_strength_score": 26,
                "change_pct": -0.2,
                "volume_ratio": 0.9,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 1.4,
                },
            },
        ],
        limit=10,
    )

    assert [item["code"] for item in ranked] == ["000001"]


def test_rank_first_board_candidates_uses_global_evidence_without_route_quotas():
    low_absorb = [
        {
            "code": f"0010{index:02d}",
            "candidate_route": "pre_board_probe_start",
            "time_horizon": "sprint",
            "probability": 0.62 - index * 0.01,
            "route_score": 80 - index,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 80,
            "display_score": 82,
            "support_strength_score": 70,
            "probability_factors": {"pre_board_probe_score": promotion.PRE_BOARD_PROBE_MIN_SCORE + 5},
        }
        for index in range(8)
    ]
    routed = [
        {
            "code": "002001",
            "candidate_route": "news_catalyst_start",
            "time_horizon": "sprint",
            "probability": 0.31,
            "route_score": 62,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 68,
            "display_score": 70,
            "event_types": ["news_catalyst"],
            "probability_factors": {"news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 8},
        },
        {
            "code": "002002",
            "candidate_route": "auction_surge_start",
            "time_horizon": "sprint",
            "probability": 0.29,
            "route_score": 60,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 66,
            "display_score": 68,
            "event_types": ["auction_surge"],
            "probability_factors": {"auction_strength_score": promotion.AUCTION_SURGE_MIN_SCORE + 8},
        },
        {
            "code": "002003",
            "candidate_route": "mainline_spread_start",
            "time_horizon": "sprint",
            "probability": 0.27,
            "route_score": 58,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 65,
            "display_score": 67,
            "event_types": ["mainline_spread"],
            "probability_factors": {"mainline_spread_score": 68},
        },
    ]

    source = low_absorb + routed
    ranked = promotion._rank_first_board_candidates(source, limit=8)
    expected = sorted(
        source,
        key=promotion._first_board_rank_key,
        reverse=True,
    )[:8]

    assert [item["code"] for item in ranked] == [item["code"] for item in expected]
    assert all(item["rank_policy"] == "global_time_visible_evidence" for item in ranked)
    assert all(item["rank_lane_quota"] is None for item in ranked)


def test_rank_first_board_candidates_reserves_strong_trend_reclaim_lane():
    trend_reclaim = {
        "code": "601700",
        "candidate_route": "oversold_reversal_start",
        "time_horizon": "pre_sprint",
        "prediction_rank_eligible": True,
        "prediction_shape_seed": True,
        "probability": 0.16,
        "route_score": 66,
        "setup_grade": "B类观察候选",
        "bull_score": 72,
        "display_score": 74,
        "support_strength_score": 62,
        "probability_factors": {
            "prediction_shape_seed": True,
            "strong_first_bearish_ready": True,
            "strong_first_bearish_persistence_score": 82.0,
            "strong_first_bearish_confirmation": False,
        },
    }
    ordinary = [
        {
            "code": f"0018{index:02d}",
            "candidate_route": "pre_board_probe_start",
            "time_horizon": "sprint",
            "prediction_rank_eligible": True,
            "probability": 0.60 - index * 0.01,
            "route_score": 78 - index,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 78,
            "display_score": 80,
            "support_strength_score": 68,
            "probability_factors": {
                "pre_board_probe_score": promotion.PRE_BOARD_PROBE_MIN_SCORE + 8,
            },
        }
        for index in range(8)
    ]

    ranked = promotion._rank_first_board_candidates(
        ordinary + [trend_reclaim],
        limit=6,
    )
    selected = next(item for item in ranked if item["code"] == "601700")

    assert selected["rank_lane"] == "trend_reclaim"
    assert selected["rank_lane_label"] == "强趋势首阴回收"


def test_rank_first_board_candidates_prioritizes_tradeable_main_board_without_relabeling_route():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "688200",
                "candidate_route": "pre_board_probe_start",
                "time_horizon": "sprint",
                "probability": 0.88,
                "route_score": 92,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 90,
                "display_score": 92,
                "support_strength_score": 90,
                "probability_factors": {
                    "pre_board_probe_score": promotion.PRE_BOARD_PROBE_MIN_SCORE + 20,
                    "strict_confirmation_count": 4,
                },
            },
            {
                "code": "600200",
                "candidate_route": "news_catalyst_start",
                "time_horizon": "sprint",
                "probability": 0.48,
                "route_score": 68,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 76,
                "display_score": 80,
                "support_strength_score": 58,
                "change_pct": 1.2,
                "volume_ratio": 1.4,
                "event_types": ["news_catalyst"],
                "probability_factors": {
                    "market_risk_level": "weak",
                    "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 12,
                    "strict_confirmation_count": 1,
                },
            },
        ],
        limit=2,
    )

    assert ranked[0]["code"] == "600200"
    assert ranked[0]["rank_lane"] == "news_catalyst"


def test_rank_first_board_candidates_keeps_unconfirmed_forecasts_but_not_actionability():
    broad_mainline = [
        {
            "code": f"6005{index:02d}",
            "candidate_route": "mainline_spread_start",
            "time_horizon": "sprint",
            "probability": 0.18 - index * 0.004,
            "route_score": 58 - index * 0.5,
            "setup_grade": "B类观察候选",
            "bull_score": 62,
            "display_score": 66,
            "support_strength_score": 46,
            "sector_strength_score": 62,
            "main_net_inflow_pct": -1.2,
            "change_pct": 1.4,
            "volume_ratio": 0.88,
            "event_types": ["mainline_spread"],
            "probability_factors": {
                "market_risk_level": "hostile",
                "market_broad_first_board_overflow": True,
                "broad_rotation_member_setup": True,
                "sector_low_position_rotation": True,
                "strict_confirmation_count": 1,
            },
        }
        for index in range(8)
    ]
    low_absorb = [
        {
            "code": f"0012{index:02d}",
            "candidate_route": "pre_board_probe_start",
            "time_horizon": "sprint",
            "probability": 0.64 - index * 0.01,
            "route_score": 80 - index,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 82,
            "display_score": 84,
            "support_strength_score": 72,
            "change_pct": 2.6,
            "volume_ratio": 1.6,
            "probability_factors": {
                "pre_board_probe_score": promotion.PRE_BOARD_PROBE_MIN_SCORE + 10,
                "strict_confirmation_count": 2,
            },
        }
        for index in range(8)
    ]
    routed = [
        {
            "code": "600610",
            "candidate_route": "news_catalyst_start",
            "time_horizon": "sprint",
            "probability": 0.28,
            "route_score": 60,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 68,
            "display_score": 70,
            "support_strength_score": 50,
            "event_types": ["news_catalyst"],
            "probability_factors": {"news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 10},
        },
        {
            "code": "600611",
            "candidate_route": "auction_surge_start",
            "time_horizon": "sprint",
            "probability": 0.26,
            "route_score": 59,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 66,
            "display_score": 68,
            "support_strength_score": 50,
            "event_types": ["auction_surge"],
            "probability_factors": {"auction_strength_score": promotion.AUCTION_SURGE_MIN_SCORE + 10},
        },
    ]

    ranked = promotion._rank_first_board_candidates(low_absorb + broad_mainline + routed, limit=10)
    mainline_ranked = [item for item in ranked if item["rank_lane"] == "mainline_spread"]

    assert promotion._first_board_rank_lane(broad_mainline[0]) == "mainline_spread"
    assert len(ranked) == 10
    assert len(mainline_ranked) >= 1
    assert all(item["candidate_route"] == "mainline_spread_start" for item in mainline_ranked)
    assert all(item["rank_active_confirmation"] is False for item in ranked)
    assert all(item["prediction_actionable"] is False for item in ranked)
    assert all(
        item["trade_actionability_status"] in {"conditional", "forecast_only"}
        for item in ranked
    )


def test_rank_first_board_candidates_deduplicates_same_stock_routes():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "600310",
                "candidate_route": "news_catalyst_start",
                "time_horizon": "sprint",
                "probability": 0.68,
                "route_score": 76,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 80,
                "display_score": 80,
                "support_strength_score": 86,
                "sector_strength_score": 72,
                "change_pct": 3.5,
                "volume_ratio": 1.2,
                "event_types": ["news_catalyst"],
                "probability_factors": {
                    "market_risk_level": "weak",
                    "market_broad_first_board_overflow": True,
                    "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 20,
                    "strict_confirmation_count": 2,
                },
            },
            {
                "code": "600310",
                "candidate_route": "mainline_spread_start",
                "time_horizon": "sprint",
                "probability": 0.66,
                "route_score": 74,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 79,
                "display_score": 79,
                "support_strength_score": 82,
                "sector_strength_score": 75,
                "change_pct": 3.2,
                "volume_ratio": 1.1,
                "event_types": ["mainline_spread"],
                "probability_factors": {
                    "market_risk_level": "weak",
                    "market_broad_first_board_overflow": True,
                    "sector_low_position_rotation": True,
                    "strict_confirmation_count": 2,
                },
            },
            {
                "code": "600311",
                "candidate_route": "pre_board_probe_start",
                "time_horizon": "sprint",
                "probability": 0.42,
                "route_score": 64,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 70,
                "display_score": 70,
                "support_strength_score": 72,
                "sector_strength_score": 60,
                "change_pct": 2.6,
                "volume_ratio": 1.0,
                "probability_factors": {
                    "market_risk_level": "weak",
                    "market_broad_first_board_overflow": True,
                    "pre_board_probe_score": promotion.PRE_BOARD_PROBE_MIN_SCORE + 8,
                    "strict_confirmation_count": 2,
                },
            },
        ],
        limit=3,
    )

    assert [item["code"] for item in ranked].count("600310") == 1
    assert {item["code"] for item in ranked} == {"600310", "600311"}


def test_rank_first_board_candidates_falls_back_to_watch_when_sprint_absent():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "000011",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.54,
                "route_score": 64,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 78,
                "display_score": 88,
                "support_strength_score": 44,
                "sector_strength_score": 26,
                "change_pct": 0.6,
                "volume_ratio": 1.0,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 1.4,
                },
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "time_horizon_reason": "仍在首板预测梯队",
            },
            {
                "code": "000012",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.49,
                "route_score": 58,
                "setup_grade": "B类观察候选",
                "bull_score": 71,
                "display_score": 82,
                "support_strength_score": 42,
                "sector_strength_score": 24,
                "change_pct": 0.2,
                "volume_ratio": 0.9,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 1.5,
                },
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "time_horizon_reason": "仍在首板预测梯队",
            },
        ],
        limit=10,
    )

    assert [item["code"] for item in ranked] == ["000011", "000012"]
    assert all(item["time_horizon"] == "watch" for item in ranked)


def test_rank_first_board_candidates_does_not_fill_formal_rank_with_display_only_watch():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "000031",
                "candidate_route": "platform_relaunch",
                "time_horizon": "watch",
                "probability": 0.02,
                "route_score": 50.0,
                "support_strength_score": 45,
                "sector_strength_score": 49,
                "main_net_inflow_pct": 0.8,
                "change_pct": 1.4,
                "volume_ratio": 0.9,
                "probability_factors": {
                    "market_risk_level": "hostile",
                    "strict_confirmation_count": 3,
                },
            }
        ],
        limit=10,
    )

    assert ranked == []


def test_rank_first_board_candidates_prefers_pre_sprint_but_keeps_watch_for_prediction_recall():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "000021",
                "candidate_route": "quiet_setup",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.5,
                "route_score": 52.0,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 79,
                "display_score": 88,
                "support_strength_score": 50,
                "sector_strength_score": 22,
                "change_pct": 0.92,
                "volume_ratio": 0.94,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 2.0,
                },
                "sub_probabilities": {"breakout_3d": 0.31},
            },
            {
                "code": "000022",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.49,
                "route_score": 58,
                "setup_grade": "B类观察候选",
                "bull_score": 71,
                "display_score": 82,
                "support_strength_score": 42,
                "sector_strength_score": 24,
                "change_pct": 0.2,
                "volume_ratio": 0.9,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 1.5,
                },
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "time_horizon_reason": "仍在首板预测梯队",
            },
        ],
        limit=10,
    )

    assert [item["code"] for item in ranked] == ["000021", "000022"]
    assert ranked[0]["time_horizon"] == "pre_sprint"
    assert ranked[1]["time_horizon"] == "watch"


def test_rank_first_board_candidates_keeps_momentum_shape_seed_without_promoting_it_to_buy():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "600123",
                "candidate_route": "pre_board_probe_start",
                "time_horizon": "pre_sprint",
                "probability": 0.025,
                "route_score": 54,
                "trade_ready": False,
                "keep_in_diagnostics": True,
                "prediction_shape_seed": True,
                "change_pct": -0.6,
                "probability_factors": {
                    "prediction_shape_seed": True,
                    "momentum_shakeout_score": 86,
                    "memory_score": 62,
                },
            }
        ],
        limit=10,
    )

    assert [item["code"] for item in ranked] == ["600123"]
    assert ranked[0]["prediction_actionability_label"] == "概率预测·待执行确认"
    assert ranked[0]["prediction_watch_only"] is True
    assert ranked[0]["trade_ready"] is False


def test_rank_first_board_candidates_does_not_fall_back_to_weak_watch():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "000021",
                "prediction_mode": "strict_stealth_scan",
                "probability": 0.52,
                "route_score": 63,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 77,
                "display_score": 86,
                "support_strength_score": 44,
                "sector_strength_score": 26,
                "change_pct": -0.4,
                "volume_ratio": 0.9,
                "probability_factors": {
                    "strict_confirmation_count": 4,
                    "qualified_doji_confirmation": True,
                    "overhead_gap_pct": 1.4,
                },
            },
        ],
        limit=10,
    )

    assert ranked == []


def test_rank_first_board_candidates_includes_actionable_oversold_reversal():
    ranked = promotion._rank_first_board_candidates(
        [
            {
                "code": "000088",
                "candidate_route": "oversold_reversal_start",
                "probability": 0.42,
                "route_score": 88,
                "setup_grade": "A2 盘口确认后执行",
                "bull_score": 86,
                "display_score": 92,
                "support_strength_score": 76,
                "sector_strength_score": 54,
                "sector_limit_up_count": 8,
                "change_pct": -4.8,
                "volume_ratio": 1.4,
                "probability_factors": {
                    "market_risk_level": "hostile",
                    "market_broad_first_board_overflow": True,
                    "broad_rotation_cluster_setup": True,
                    "strict_confirmation_count": 3,
                    "oversold_reversal_score": 88.0,
                },
            },
        ],
        limit=10,
    )

    assert [item["code"] for item in ranked] == ["000088"]
    assert ranked[0]["time_horizon"] == "sprint"
    assert ranked[0]["rank_lane"] == "low_absorb_halfway"


def test_rank_first_board_candidates_does_not_cap_oversold_forecast_cluster():
    def build_oversold(index: int) -> dict:
        return {
            "code": f"00008{index}",
            "candidate_route": "oversold_reversal_start",
            "probability": 0.42 - index * 0.01,
            "route_score": 88 - index,
            "setup_grade": "A2 盘口确认后执行",
            "bull_score": 86,
            "display_score": 92,
            "support_strength_score": 76,
            "sector_strength_score": 54,
            "sector_limit_up_count": 8,
            "change_pct": -4.8,
            "volume_ratio": 1.4,
            "probability_factors": {
                "market_risk_level": "hostile",
                "market_broad_first_board_overflow": True,
                "broad_rotation_cluster_setup": True,
                "strict_confirmation_count": 3,
                "oversold_reversal_score": 88.0,
            },
        }

    ranked = promotion._rank_first_board_candidates([build_oversold(i) for i in range(6)], limit=5)

    assert len(ranked) == 5
    assert all(item["candidate_route"] == "oversold_reversal_start" for item in ranked)
    assert all(item["rank_lane"] == "low_absorb_halfway" for item in ranked)


def test_rank_first_board_candidates_reserves_washout_shapes_after_market_flush():
    candidates = [
        {
            "code": f"00009{index}",
            "candidate_route": "oversold_reversal_start",
            "time_horizon": "pre_sprint",
            "probability": 0.04,
            "route_score": 64 - index,
            "setup_grade": "B类观察候选",
            "bull_score": 62,
            "display_score": 68,
            "support_strength_score": 54,
            "sector_strength_score": 48,
            "change_pct": -3.5,
            "volume_ratio": 1.1,
            "prediction_shape_seed": True,
            "trade_ready": False,
            "probability_factors": {
                "prediction_shape_seed": True,
                "market_washout_reversal_setup": True,
                "oversold_reversal_score": 64.0,
            },
        }
        for index in range(6)
    ]

    ranked = promotion._rank_first_board_candidates(candidates, limit=12)

    assert len(ranked) == 6
    assert all(item["prediction_watch_only"] is True for item in ranked)
    assert all(item["trade_ready"] is False for item in ranked)


def test_prediction_snapshot_metadata_persists_execution_gate_separately():
    candidate = {
        "code": "600880",
        "name": "条件观察",
        "target_board": 1,
        "candidate_route": "pre_board_probe_start",
        "probability": 0.08,
        "trade_ready": True,
        "probability_factors": {"route_score": 62},
    }
    ranked = {
        **candidate,
        "prediction_rank_eligible": True,
        "prediction_actionable": False,
        "prediction_watch_only": False,
        "trade_actionability_status": "conditional",
        "trade_actionability_label": "条件观察·待确认",
        "trade_actionability_score": 58.0,
        "rank_lane": "inverse_early_attack",
        "rank_lane_label": "逆市早盘强攻首板",
        "rank_active_confirmation": False,
    }

    annotated = promotion._annotate_prediction_record_metadata(
        [candidate],
        [ranked],
        ranked_limit=12,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
        news_end_time=datetime(2026, 8, 28, 20, 0),
        candidate_anchor_trade_date=date(2026, 8, 28),
    )[0]
    factors = annotated["probability_factors"]
    reason = promotion._build_prediction_reason_snapshot(annotated)

    assert factors["prediction_ranked_selected"] is True
    assert factors["prediction_rank_contract_version"] == "promotion_rank_contract_v1"
    assert factors["prediction_rank_contract_complete"] is True
    assert factors["prediction_rank_eligible"] is True
    assert factors["prediction_trade_gate_passed"] is True
    assert factors["prediction_actionable"] is False
    assert factors["trade_actionability_status"] == "conditional"
    assert factors["prediction_rank_lane"] == "inverse_early_attack"
    assert factors["prediction_news_end_time"] == "2026-08-28T20:00:00"
    assert factors["prediction_candidate_anchor_trade_date"] == "2026-08-28"
    assert reason["prediction_trade_gate_passed"] is True
    assert reason["prediction_actionable"] is False
    assert reason["trade_actionability_label"] == "条件观察·待确认"


@pytest.mark.asyncio
async def test_page_prediction_record_does_not_overwrite_schedule_snapshot(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 5, 21)
    base_candidate = {
        "code": "000901",
        "name": "调度快照",
        "target_board": 1,
        "candidate_route": "pre_board_probe_start",
        "probability": 0.36,
        "raw_probability": 0.34,
        "model_adjustment": 0.02,
        "confidence_level": "medium",
        "signal_status": "confirmed",
        "probability_factors": {"route_score": 72},
    }
    schedule_candidates = promotion._annotate_prediction_record_metadata(
        [base_candidate],
        [base_candidate],
        ranked_limit=50,
        snapshot_source="schedule",
        snapshot_context="20:00",
    )
    page_candidate = {
        **base_candidate,
        "name": "页面临时",
        "probability": 0.12,
        "raw_probability": 0.11,
        "probability_factors": {"route_score": 40},
    }
    page_candidates = promotion._annotate_prediction_record_metadata(
        [page_candidate],
        [],
        ranked_limit=5,
        snapshot_source="page",
        snapshot_context="ui-small",
    )

    async with SessionLocal() as session:
        saved_details = await promotion._record_promotion_predictions(
            session,
            schedule_candidates,
            {1: prediction_date},
            snapshot_source="schedule",
            quality_gate={"gate_passed": True, "run_id": 77, "status": "passed"},
            return_details=True,
        )
        saved = saved_details.touched
        skipped = await promotion._record_promotion_predictions(
            session,
            page_candidates,
            {1: prediction_date},
            snapshot_source="page",
        )
        record = await session.scalar(
            select(PromotionPredictionRecord).where(
                PromotionPredictionRecord.code == "000901",
                PromotionPredictionRecord.prediction_trade_date == prediction_date,
            )
        )
        ledger_run = await session.scalar(
            select(PromotionPredictionRun).where(
                PromotionPredictionRun.reference_trade_date == prediction_date
            )
        )
        ledger_snapshot = await session.scalar(
            select(PromotionPredictionSnapshot).where(
                PromotionPredictionSnapshot.run_id == ledger_run.id,
                PromotionPredictionSnapshot.code == "000901",
            )
        )

    factors = promotion._json_loads_safe(record.factors_json)
    assert saved == 1
    assert skipped == 0
    assert saved_details.ledger is not None
    assert saved_details.ledger.run_id == ledger_run.id
    assert ledger_run.gate_passed is True
    assert promotion._json_loads_safe(ledger_run.metadata_json)["quality_gate"]["run_id"] == 77
    assert record.name == "调度快照"
    assert record.calibrated_probability == pytest.approx(0.36)
    assert factors["prediction_snapshot_source"] == "schedule"
    assert factors["prediction_ranked_limit"] == 50
    assert factors["prediction_ranked_selected"] is True
    assert factors["learning_eligible"] is True
    assert ledger_run.snapshot_context == "promotion_2000"
    assert ledger_run.candidate_count == 1
    assert ledger_snapshot.legacy_record_id == record.id
    assert ledger_snapshot.calibrated_probability == pytest.approx(0.36)


@pytest.mark.asyncio
async def test_record_promotion_predictions_skips_oversold_observation_pool(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 23)
    candidate = {
        "code": "000902",
        "name": "弱观察",
        "target_board": 1,
        "candidate_route": "oversold_reversal_start",
        "probability": 0.66,
        "raw_probability": 0.66,
        "confidence_level": "medium",
        "signal_status": "confirmed",
        "probability_factors": {
            "candidate_route": "oversold_reversal_start",
            "route_score": 88,
            "oversold_reversal_score": 90,
        },
    }
    schedule_candidates = promotion._annotate_prediction_record_metadata(
        [candidate],
        [candidate],
        ranked_limit=50,
        snapshot_source="schedule",
        snapshot_context="20:00",
    )

    async with SessionLocal() as session:
        saved = await promotion._record_promotion_predictions(
            session,
            schedule_candidates,
            {1: prediction_date},
            snapshot_source="schedule",
        )
        record = await session.scalar(
            select(PromotionPredictionRecord).where(PromotionPredictionRecord.code == "000902")
        )

    assert saved == 0
    assert record is None


@pytest.mark.asyncio
async def test_schedule_prediction_recording_preserves_other_snapshot_contexts(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 23)
    old_pending = PromotionPredictionRecord(
        code="000910",
        name="旧批次",
        target_board=2,
        prediction_trade_date=prediction_date,
        horizon_days=1,
        predicted_probability=0.30,
        calibrated_probability=0.30,
        candidate_route="second_board_promotion",
        learning_bucket="T2:second_board_promotion",
        outcome_status="pending",
        factors_json='{"prediction_snapshot_source":"schedule","prediction_snapshot_context":"promotion_1510","prediction_snapshot_recorded_at":"2026-04-22T15:10:00"}',
    )
    evaluated_record = PromotionPredictionRecord(
        code="000911",
        name="已评估",
        target_board=2,
        prediction_trade_date=prediction_date,
        horizon_days=1,
        predicted_probability=0.31,
        calibrated_probability=0.31,
        candidate_route="second_board_promotion",
        learning_bucket="T2:second_board_promotion",
        outcome_status="success",
        factors_json='{"prediction_snapshot_source":"schedule","prediction_snapshot_context":"promotion_1510","prediction_snapshot_recorded_at":"2026-04-22T15:10:00"}',
    )
    candidate = {
        "code": "000912",
        "name": "新批次",
        "target_board": 2,
        "candidate_route": "second_board_promotion",
        "probability": 0.42,
        "raw_probability": 0.40,
        "confidence_level": "medium",
        "signal_status": "close_confirmed",
        "probability_factors": {"second_board_style_score": 72, "route_score": 42},
    }
    schedule_candidates = promotion._annotate_prediction_record_metadata(
        [candidate],
        [candidate],
        ranked_limit=50,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )

    async with SessionLocal() as session:
        session.add_all([old_pending, evaluated_record])
        await session.commit()

        saved = await promotion._record_promotion_predictions(
            session,
            schedule_candidates,
            {2: prediction_date},
            snapshot_source="schedule",
        )
        rows = (
            await session.execute(
                select(PromotionPredictionRecord).where(
                    PromotionPredictionRecord.target_board == 2,
                    PromotionPredictionRecord.prediction_trade_date == prediction_date,
                )
            )
        ).scalars().all()

    records_by_code = {row.code: row for row in rows}
    assert saved == 1
    assert set(records_by_code) == {"000910", "000911", "000912"}
    assert records_by_code["000910"].outcome_status == "pending"
    assert records_by_code["000911"].outcome_status == "success"
    assert records_by_code["000912"].outcome_status == "pending"
    new_factors = promotion._json_loads_safe(records_by_code["000912"].factors_json)
    assert new_factors["prediction_snapshot_context"] == "promotion_2000"
    assert new_factors["prediction_snapshot_batch_key"].startswith("schedule:promotion_2000:")


@pytest.mark.asyncio
async def test_schedule_prediction_recording_preserves_intraday_and_close_snapshots(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 24)
    intraday_record = PromotionPredictionRecord(
        code="600800",
        name="竞价强攻",
        target_board=1,
        prediction_trade_date=prediction_date,
        horizon_days=1,
        predicted_probability=0.42,
        calibrated_probability=0.42,
        candidate_route="auction_surge_start",
        learning_bucket="T1:auction_surge_start",
        outcome_status="pending",
        factors_json='{"prediction_snapshot_source":"schedule","prediction_snapshot_context":"promotion_0935","prediction_ranked_selected":true,"prediction_ranked_position":4,"prediction_ranked_limit":50,"route_score":68}',
    )
    close_candidate_same_key = {
        "code": "600800",
        "name": "竞价强攻",
        "target_board": 1,
        "candidate_route": "auction_surge_start",
        "probability": 0.18,
        "raw_probability": 0.18,
        "confidence_level": "low",
        "signal_status": "close_confirmed",
        "probability_factors": {"route_score": 18},
    }
    close_candidate_new_key = {
        "code": "600801",
        "name": "晚间新增",
        "target_board": 1,
        "candidate_route": "mainline_spread_start",
        "probability": 0.36,
        "raw_probability": 0.36,
        "confidence_level": "medium",
        "signal_status": "close_confirmed",
        "probability_factors": {"route_score": 50},
    }
    schedule_candidates = promotion._annotate_prediction_record_metadata(
        [close_candidate_same_key, close_candidate_new_key],
        [close_candidate_same_key, close_candidate_new_key],
        ranked_limit=50,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )

    async with SessionLocal() as session:
        session.add(intraday_record)
        await session.commit()

        saved = await promotion._record_promotion_predictions(
            session,
            schedule_candidates,
            {1: prediction_date},
            snapshot_source="schedule",
        )
        rows = (
            await session.execute(
                select(PromotionPredictionRecord).where(
                    PromotionPredictionRecord.target_board == 1,
                    PromotionPredictionRecord.prediction_trade_date == prediction_date,
                )
            )
        ).scalars().all()

    records_by_code: dict[str, list[PromotionPredictionRecord]] = {}
    for row in rows:
        records_by_code.setdefault(row.code, []).append(row)
    assert saved == 2
    assert set(records_by_code) == {"600800", "600801"}
    assert len(records_by_code["600800"]) == 2
    contexts = {
        promotion._prediction_record_snapshot_context(row): row
        for row in records_by_code["600800"]
    }
    assert set(contexts) == {"promotion_0935", "promotion_2000"}
    assert contexts["promotion_0935"].predicted_probability == pytest.approx(0.42)
    assert contexts["promotion_2000"].predicted_probability == pytest.approx(0.18)
    new_factors = promotion._json_loads_safe(records_by_code["600801"][0].factors_json)
    assert new_factors["prediction_snapshot_context"] == "promotion_2000"


@pytest.mark.asyncio
async def test_actual_limit_up_replay_counts_intraday_first_board_confirmation_snapshot(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="000800",
                    name="前日样本",
                    trade_date=date(2026, 4, 23),
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600800",
                    name="当日强攻",
                    trade_date=date(2026, 4, 24),
                    consecutive_days=1,
                    limit_up_time="093100",
                    source="test",
                ),
                PromotionPredictionRecord(
                    code="600800",
                    name="当日强攻",
                    target_board=1,
                    prediction_trade_date=date(2026, 4, 24),
                    horizon_days=1,
                    predicted_probability=0.42,
                    calibrated_probability=0.42,
                    candidate_route="auction_surge_start",
                    learning_bucket="T1:auction_surge_start",
                    outcome_status="pending",
                    reason_snapshot='{"candidate_route_label":"竞价高开强攻"}',
                    factors_json='{"prediction_snapshot_source":"schedule","prediction_snapshot_context":"promotion_0935","prediction_ranked_selected":true,"prediction_ranked_position":4,"prediction_ranked_limit":50,"route_score":68}',
                ),
            ]
        )
        await session.commit()
        actual = await promotion._load_filtered_limit_ups(session, date(2026, 4, 24))
        replay = await promotion._build_actual_limit_up_replay(
            session,
            actual_trade_date=date(2026, 4, 24),
            actual_limit_ups=actual,
        )

    item = next(row for row in replay["items"] if row["code"] == "600800")
    assert item["replay_status"] == "predicted_hit"
    assert item["prediction_found"] is True
    assert item["candidate_route"] == "auction_surge_start"


def test_replay_counts_ranked_low_probability_success_as_hit_and_marks_underestimate():
    record = PromotionPredictionRecord(
        code="600820",
        name="低估命中",
        target_board=1,
        prediction_trade_date=date(2026, 4, 22),
        predicted_probability=0.02,
        calibrated_probability=0.02,
        candidate_route="mainline_spread_start",
        factors_json='{"prediction_ranked_selected":true,"prediction_ranked_position":2}',
    )

    status, reason, detail = promotion._resolve_actual_replay_status(
        code="600820",
        target_board=1,
        record=record,
        tag=None,
    )

    assert status == "predicted_hit"
    assert reason == "预测命中（低概率）"
    assert "低估校准样本" in detail


def test_select_best_prediction_record_accepts_official_oversold_first_board_records():
    oversold_record = PromotionPredictionRecord(
        code="000903",
        name="反包正式路线",
        target_board=1,
        prediction_trade_date=date(2026, 4, 22),
        predicted_probability=0.92,
        calibrated_probability=0.92,
        candidate_route="oversold_reversal_start",
        factors_json='{"prediction_snapshot_source":"schedule","prediction_ranked_selected":true}',
    )
    mainline_record = PromotionPredictionRecord(
        code="000903",
        name="主线",
        target_board=1,
        prediction_trade_date=date(2026, 4, 22),
        predicted_probability=0.18,
        calibrated_probability=0.18,
        candidate_route="mainline_spread_start",
        factors_json='{"prediction_snapshot_source":"schedule","prediction_ranked_selected":true}',
    )

    assert promotion._is_legacy_observation_first_board_record(oversold_record) is False
    assert promotion._select_best_prediction_record([oversold_record]) is oversold_record
    assert promotion._select_best_prediction_record([oversold_record, mainline_record]) is oversold_record


def test_rank_second_board_candidates_prioritizes_tradeable_hard_board_quality():
    gem_high_probability = {
        "code": "300001",
        "name": "观察高分",
        "target_board": 2,
        "probability": 0.80,
        "second_board_style_score": 88,
        "route_score": 80,
        "sector_strength_score": 80,
        "dragon_tiger_score": 60,
        "turnover": 4.0,
        "probability_factors": {
            "second_board_route": "standard_second_board",
            "limit_up_minutes": 9 * 60 + 32,
            "break_count": 0,
            "seal_amount": 150_000_000,
        },
    }
    main_hard_board = {
        "code": "600001",
        "name": "主板硬板",
        "target_board": 2,
        "probability": 0.42,
        "second_board_style_score": 70,
        "route_score": 42,
        "sector_strength_score": 70,
        "dragon_tiger_score": 55,
        "turnover": 5.0,
        "probability_factors": {
            "second_board_route": "low_rotation_cluster_second_board",
            "limit_up_minutes": 9 * 60 + 31,
            "break_count": 0,
            "seal_amount": 220_000_000,
            "main_net_inflow_pct": 7.2,
            "is_clean_early_hard_board": True,
            "is_zero_break_hard_seal": True,
            "has_strong_main_inflow": True,
        },
    }

    ranked = promotion._rank_second_board_candidates([gem_high_probability, main_hard_board], 2)

    assert [item["code"] for item in ranked] == ["600001"]
    assert promotion._second_board_quality_rank_score(main_hard_board) > promotion._second_board_quality_rank_score(gem_high_probability)


def test_rank_second_board_candidates_keeps_prediction_only_cluster_but_blocks_trade():
    cluster_one_word = {
        "code": "002038",
        "name": "题材集群一字",
        "target_board": 2,
        "probability": 0.44,
        "relay_pool_ready": False,
        "relay_prediction_ready": True,
        "relay_quality_score": 64,
        "relay_prediction_quality_score": 96,
        "relay_blockers": ["首板一字/准一字，没有可验证的换手成本"],
        "probability_factors": {
            "is_one_word_shape": True,
            "reason_cohort_count": 8,
            "limit_up_minutes": 9 * 60 + 25,
        },
    }
    normal_trade_watch = {
        "code": "600001",
        "name": "正常换手板",
        "target_board": 2,
        "probability": 0.40,
        "relay_pool_ready": True,
        "relay_prediction_ready": True,
        "relay_quality_score": 82,
        "relay_prediction_quality_score": 78,
        "probability_factors": {},
    }

    ranked = promotion._rank_second_board_candidates(
        [normal_trade_watch, cluster_one_word],
        2,
    )

    assert [item["code"] for item in ranked] == ["002038", "600001"]
    assert ranked[0]["prediction_only"] is True
    assert ranked[0]["trade_ready"] is False
    assert ranked[0]["relay_action_label"] == "只预测·不可追"
    assert ranked[1]["trade_ready"] is True


def test_first_board_rank_prefers_time_visible_empirical_lift_features_over_raw_probability():
    plain_high_probability = {
        "code": "600201",
        "target_board": 1,
        "candidate_route": "pre_board_probe_start",
        "probability": 0.12,
        "route_score": 82,
        "probability_factors": {
            "memory_score": 10,
        },
    }
    confirmed_lift = {
        "code": "600202",
        "target_board": 1,
        "candidate_route": "pre_board_probe_start",
        "probability": 0.04,
        "route_score": 62,
        "probability_factors": {
            "trend_acceleration_ready": True,
            "trend_breakout": True,
            "has_recent_limit_up_event": True,
            "memory_score": 60,
        },
    }

    assert promotion._first_board_empirical_lift_rank_score(confirmed_lift) > promotion._first_board_empirical_lift_rank_score(
        plain_high_probability
    )
    assert promotion._first_board_rank_key(confirmed_lift) > promotion._first_board_rank_key(plain_high_probability)


def test_first_board_rank_prefers_industry_stock_funding_resonance_over_raw_low_base():
    raw_low_base = {
        "candidate_route": "pre_board_probe_start",
        "probability_factors": {
            "prediction_shape_seed": True,
            "low_base_rotation_ready": True,
            "low_base_rotation_score": 72,
        },
    }
    researched_combo = {
        "candidate_route": "pre_board_probe_start",
        "probability_factors": {
            **raw_low_base["probability_factors"],
            "low_base_sector_ignition_ready": True,
            "low_base_sector_ignition_confirmed": True,
            "low_base_sector_ignition_score": 76,
            "funding_preheat_ready": True,
        },
    }
    persistent_outflow = {
        **researched_combo,
        "probability_factors": {
            **researched_combo["probability_factors"],
            "funding_persistent_outflow_risk": True,
        },
    }

    combo_score = promotion._first_board_empirical_lift_rank_score(researched_combo)
    assert combo_score > promotion._first_board_empirical_lift_rank_score(raw_low_base)
    assert combo_score > promotion._first_board_empirical_lift_rank_score(persistent_outflow)


def test_first_board_rank_promotes_auction_cluster_above_close_only_sector_washout():
    close_only_washout = {
        "candidate_route": "mainline_spread_start",
        "probability_factors": {
            "prediction_shape_seed": True,
            "sector_washout_reversal_watch": True,
            "sector_washout_score": 82,
        },
    }
    auction_cluster = {
        "candidate_route": "auction_surge_start",
        "probability_factors": {
            **close_only_washout["probability_factors"],
            "auction_cluster_confirmed": True,
            "auction_feed_complete": False,
            "auction_sector_member_count": 6,
            "auction_sector_avg_open_change": 4.2,
        },
    }

    assert promotion._first_board_empirical_lift_rank_score(auction_cluster) > promotion._first_board_empirical_lift_rank_score(
        close_only_washout
    )


def test_first_board_rank_keeps_sector_cluster_and_exposes_metadata():
    candidates = []
    for index in range(4):
        candidates.append({
            "code": f"60010{index}",
            "target_board": 1,
            "candidate_route": "pre_board_probe_start",
            "probability": 0.20 - index * 0.01,
            "route_score": 75 - index,
            "time_horizon": "sprint",
            "prediction_rank_eligible": True,
            "probability_factors": {
                "sector_code": "pw_concept_阿尔茨海默概念",
                "sector_name": "阿尔茨海默概念",
            },
        })
    for index, sector_name in enumerate(("黄金概念", "CPO概念", "机器人概念", "粮食概念")):
        candidates.append({
            "code": f"60120{index}",
            "target_board": 1,
            "candidate_route": "pre_board_probe_start",
            "probability": 0.10 - index * 0.01,
            "route_score": 60 - index,
            "time_horizon": "sprint",
            "prediction_rank_eligible": True,
            "probability_factors": {
                "sector_code": f"sector_{index}",
                "sector_name": sector_name,
            },
        })

    ranked = promotion._rank_first_board_by_lanes(candidates, 6)
    alzheimer_count = sum(
        1 for item in ranked if item.get("rank_sector_exposure_key") == "pw_concept_阿尔茨海默概念"
    )

    assert len(ranked) == 6
    assert alzheimer_count == 4
    assert all(item["rank_active_confirmation"] is False for item in ranked)
    assert all(item["prediction_actionable"] is False for item in ranked)
    assert all(item["rank_sector_exposure_key"] for item in ranked)


def test_pre_board_washout_expansion_never_shrinks_requested_scan_limit():
    assert promotion._pre_board_probe_effective_limit(2000, False) == 2000
    assert promotion._pre_board_probe_effective_limit(2000, True) >= 2000
    assert promotion._pre_board_probe_effective_limit(900, True) > 900


def test_first_board_temporal_probability_is_bounded_and_monotonic():
    base = {
        "target_board": 1,
        "probability_factors": {
            "memory_score": 20,
            "sector_limit_up_delta": 0,
            "upper_gap_pct": 1,
            "lower_gap_pct": 1,
            "platform_score": 10,
            "support_squeeze_signal_score": 5,
        },
    }
    baseline = promotion._first_board_temporal_probability(base)
    stronger_memory = promotion._first_board_temporal_probability({
        **base,
        "probability_factors": {
            **base["probability_factors"],
            "memory_score": 80,
        },
    })
    limit_probe = promotion._first_board_temporal_probability({
        **base,
        "limit_probe": True,
    })
    static_platform = promotion._first_board_temporal_probability({
        **base,
        "probability_factors": {
            **base["probability_factors"],
            "platform_score": 90,
        },
    })

    assert 0 < baseline < 1
    assert stronger_memory > baseline
    assert limit_probe > baseline
    assert static_platform < baseline
    assert promotion._first_board_temporal_probability({
        "probability_factors": {"memory_score": 10_000},
    }) == promotion._first_board_temporal_probability({
        "probability_factors": {"memory_score": 100},
    })


def test_first_board_recall_rank_keeps_formal_prefix_and_fills_top30():
    candidates = [
        {
            "code": f"600{index:03d}",
            "target_board": 1,
            "candidate_route": "pre_board_probe_start",
            "time_horizon": "sprint",
            "prediction_rank_eligible": True,
            "probability": 0.30 - index * 0.002,
            "temporal_event_probability": 0.01 + index * 0.001,
            "route_score": 70 - index * 0.1,
            "probability_factors": {
                "pre_board_probe_score": 60,
                "temporal_event_probability": 0.01 + index * 0.001,
            },
        }
        for index in range(40)
    ]
    formal = promotion._rank_first_board_candidates(candidates, 12)
    recall = promotion._rank_first_board_recall_candidates(candidates, formal, 30)

    assert len(formal) == 12
    assert len(recall) == 30
    assert [item["code"] for item in recall[:12]] == [
        item["code"] for item in formal
    ]
    assert len({item["code"] for item in recall}) == 30
    assert all(item["recall_rank_tier"] == "formal_top12" for item in recall[:12])
    assert all(item["recall_rank_tier"] == "broad_recall" for item in recall[12:])


def test_prediction_metadata_marks_recall_without_making_it_learning_eligible():
    candidates = [
        {
            "code": f"6000{index:02d}",
            "name": f"候选{index}",
            "target_board": 1,
            "candidate_route": "pre_board_probe_start",
            "probability": 0.05,
            "prediction_rank_eligible": True,
            "probability_factors": {},
        }
        for index in range(15)
    ]
    enriched = promotion._annotate_prediction_record_metadata(
        candidates,
        candidates[:12],
        ranked_limit=12,
        recall_ranked_candidates=candidates,
        recall_ranked_limit=15,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )

    formal_factors = enriched[0]["probability_factors"]
    recall_only_factors = enriched[12]["probability_factors"]
    assert formal_factors["prediction_ranked_selected"] is True
    assert formal_factors["learning_eligible"] is True
    assert recall_only_factors["prediction_ranked_selected"] is False
    assert recall_only_factors["prediction_recall_ranked_selected"] is True
    assert recall_only_factors["prediction_recall_ranked_position"] == 13
    assert recall_only_factors["learning_eligible"] is False


def test_auction_surge_sprint_rejects_overextended_open_gap():
    base = {
        "candidate_route": "auction_surge_start",
        "probability": 0.08,
        "route_score": 62.0,
        "support_strength_score": 58.0,
        "sector_strength_score": 68.0,
        "change_pct": 3.0,
        "volume_ratio": 1.6,
        "probability_factors": {
            "auction_strength_score": 76.0,
            "auction_feed_complete": True,
            "auction_evidence_status": "ok",
            "auction_evidence_contract": "auction_provenance_v1",
        },
    }
    fair_gap = {
        **base,
        "probability_factors": {
            **base["probability_factors"],
            "auction_open_change": promotion.AUCTION_SURGE_MAX_OPEN_CHANGE - 0.1,
        },
    }
    overextended = {
        **base,
        "probability_factors": {
            **base["probability_factors"],
            "auction_open_change": promotion.AUCTION_SURGE_MAX_OPEN_CHANGE,
        },
    }

    assert promotion._is_auction_surge_sprint_candidate(fair_gap) is True
    assert promotion._is_auction_surge_sprint_candidate(overextended) is False


def test_compact_promotion_payload_removes_only_redundant_heavy_sections():
    payload = {
        "ranked_first_board_candidates": [{"code": "600001"}],
        "ranked_second_board_candidates": [{"code": "600002"}],
        "second_board_candidates": [{"code": "600002"}],
        "actual_limit_up_replay": {"items": [{"code": "600003"}]},
        "debug": {
            "mixed_ranked_candidates": [{"code": "600001"}],
            "internal_candidate_limit": 60,
        },
    }

    projected = promotion._project_promotion_candidates_payload(payload, compact=True)

    assert projected["compact"] is True
    assert "second_board_candidates" not in projected
    assert "actual_limit_up_replay" not in projected
    assert "mixed_ranked_candidates" not in projected["debug"]
    assert projected["ranked_second_board_candidates"][0]["code"] == "600002"
    assert payload["second_board_candidates"][0]["code"] == "600002"


def test_cached_promotion_payload_honors_request_limits_and_repairs_counts():
    first_ranked = [
        {"code": "600001", "prediction_actionable": False},
        {"code": "600002", "prediction_actionable": True},
        {"code": "600003", "prediction_actionable": True},
    ]
    second_ranked = [
        {"code": "000001", "trade_ready": True},
        {"code": "000002", "trade_ready": False},
        {"code": "000003", "trade_ready": True},
    ]
    watch_candidates = [
        {
            "code": f"30000{index}",
            "time_horizon": "watch",
            "watch_bucket": "mainline_relay",
            "probability": 0.4 - index * 0.01,
        }
        for index in range(3)
    ]
    payload = {
        "_cache_capacity": {"candidate_limit": 3, "ranked_limit": 3},
        "first_board_candidates": list(first_ranked),
        "first_board_watch_candidates": watch_candidates,
        "second_board_candidates": list(second_ranked),
        "ranked_first_board_candidates": first_ranked,
        "ranked_first_board_recall_candidates": first_ranked,
        "ranked_second_board_candidates": second_ranked,
        "first_board_count": 3,
        "first_board_watch_count": 3,
        "second_board_count": 3,
        "ranked_first_board_count": 3,
        "ranked_first_board_recall_count": 3,
        "ranked_first_board_actionable_count": 2,
        "ranked_first_board_abstained_slots": 0,
        "ranked_second_board_count": 3,
        "ranked_second_board_actionable_count": 2,
    }

    assert promotion._promotion_cached_payload_supports_request(
        payload,
        candidate_limit=2,
        ranked_limit=2,
    ) is True
    assert promotion._promotion_cached_payload_supports_request(
        payload,
        candidate_limit=4,
        ranked_limit=2,
    ) is False

    projected = promotion._project_promotion_candidates_payload(
        payload,
        compact=False,
        candidate_limit=2,
        ranked_limit=2,
    )

    assert [item["code"] for item in projected["first_board_candidates"]] == ["600001", "600002"]
    assert [item["code"] for item in projected["second_board_candidates"]] == ["000001", "000002"]
    assert projected["first_board_count"] == 2
    assert projected["first_board_watch_count"] == 2
    assert projected["second_board_count"] == 2
    assert projected["first_board_watch_overview"]["watch_total"] == 2
    assert len(projected["first_board_watch_groups"][0]["examples"]) == 2
    assert projected["ranked_first_board_count"] == 2
    assert projected["ranked_first_board_recall_count"] == 2
    assert projected["formal_first_board_limit"] == 2
    assert projected["recall_first_board_limit"] == 2
    assert projected["ranked_first_board_actionable_count"] == 1
    assert projected["ranked_first_board_abstained_slots"] == 0
    assert projected["ranked_second_board_count"] == 2
    assert projected["ranked_second_board_actionable_count"] == 1
    assert "_cache_capacity" not in projected
    assert len(payload["first_board_candidates"]) == 3
    assert len(payload["ranked_second_board_candidates"]) == 3
    assert "first_board_watch_overview" not in payload


def test_cached_projection_keeps_formal_top12_separate_from_top30_recall():
    formal = [
        {"code": f"600{index:03d}", "prediction_actionable": False}
        for index in range(15)
    ]
    recall = [
        {"code": f"600{index:03d}", "prediction_actionable": False}
        for index in range(35)
    ]
    projected = promotion._project_promotion_candidates_payload(
        {
            "ranked_first_board_candidates": formal,
            "ranked_first_board_recall_candidates": recall,
        },
        compact=False,
        ranked_limit=30,
    )

    assert len(projected["ranked_first_board_candidates"]) == 12
    assert len(projected["ranked_first_board_recall_candidates"]) == 30
    assert projected["formal_first_board_limit"] == 12
    assert projected["recall_first_board_limit"] == 30
    assert projected["ranked_first_board_abstained_slots"] == 0
    assert [item["code"] for item in projected["ranked_first_board_recall_candidates"][:12]] == [
        item["code"] for item in projected["ranked_first_board_candidates"]
    ]


def test_compact_payload_repairs_lunch_break_cached_signal_status():
    payload = {
        "trade_date": "2026-08-17",
        "first_board_trade_date": "2026-08-17",
        "second_board_trade_date": "2026-08-17",
        "snapshot_time": "2026-08-17T11:50:00",
        "ranked_second_board_candidates": [
            {
                "code": "600001",
                "target_board": 2,
                "latest_as_of": "2026-08-17",
                "signal_status": "close_confirmed",
            }
        ],
    }

    projected = promotion._project_promotion_candidates_payload(payload, compact=True)

    assert projected["ranked_second_board_candidates"][0]["signal_status"] == "intraday_preview"


def test_build_first_board_watch_groups_splits_watch_tiers_and_counts_main_uptrend_ready():
    payload = promotion._build_first_board_watch_groups(
        [
            {
                "code": "000001",
                "time_horizon": "watch",
                "watch_bucket": "platform_relaunch",
                "is_main_uptrend_ready": True,
                "main_uptrend_score": 61.2,
                "probability": 0.41,
                "route_score": 63.0,
            },
            {
                "code": "000002",
                "time_horizon": "watch",
                "watch_bucket": "mainline_relay",
                "is_main_uptrend_ready": False,
                "main_uptrend_score": 49.8,
                "probability": 0.35,
                "route_score": 58.0,
            },
            {
                "code": "000003",
                "time_horizon": "watch",
                "watch_bucket": "quiet_setup",
                "is_main_uptrend_ready": False,
                "main_uptrend_score": 44.1,
                "probability": 0.29,
                "route_score": 52.0,
            },
        ],
        limit_per_group=2,
    )

    assert payload["overview"]["watch_total"] == 3
    assert payload["overview"]["main_uptrend_ready_count"] == 1
    assert "主升浪启动区" in payload["overview"]["headline"]
    assert [group["bucket"] for group in payload["groups"]] == [
        "platform_relaunch",
        "mainline_relay",
        "quiet_setup",
    ]
    assert payload["groups"][0]["main_uptrend_ready_count"] == 1


def test_build_limit_up_platform_pool_highlights_nearby_doji_names():
    payload = promotion._build_limit_up_platform_pool(
        [
            {
                "code": "000001",
                "name": "贴板十字星",
                "probability": 0.48,
                "route_score": 66.0,
                "candidate_route": "platform_relaunch",
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "limit_up_nearby_doji_confirmation": True,
                "limit_up_anchor_gap_pct": 1.4,
                "limit_up_nearby_signal_score": 82.0,
                "limit_up_nearby_pattern_label": "涨停板附近十字星",
                "memory_features": {"memory_score": 62.0},
                "probability_factors": {
                    "has_recent_limit_up_event": True,
                    "platform_relaunch_event_ready": True,
                    "limit_up_nearby_doji_confirmation": True,
                    "limit_up_anchor_gap_pct": 1.4,
                    "limit_up_nearby_signal_score": 82.0,
                    "limit_up_nearby_pattern_label": "涨停板附近十字星",
                    "strict_confirmation_count": 3,
                    "has_platform_contraction": True,
                    "qualified_doji_confirmation": True,
                },
            },
            {
                "code": "000002",
                "name": "贴板横盘",
                "probability": 0.41,
                "route_score": 61.0,
                "candidate_route": "platform_relaunch",
                "time_horizon": "sprint",
                "time_horizon_label": "1-2日冲刺",
                "limit_up_nearby_doji_confirmation": False,
                "limit_up_anchor_gap_pct": 2.6,
                "limit_up_nearby_signal_score": 70.0,
                "limit_up_nearby_pattern_label": "涨停后板附近横盘",
                "memory_features": {"memory_score": 58.0},
                "probability_factors": {
                    "has_recent_limit_up_event": True,
                    "platform_relaunch_event_ready": True,
                    "limit_up_nearby_doji_confirmation": False,
                    "limit_up_anchor_gap_pct": 2.6,
                    "limit_up_nearby_signal_score": 70.0,
                    "limit_up_nearby_pattern_label": "涨停后板附近横盘",
                    "strict_confirmation_count": 2,
                    "has_platform_contraction": True,
                    "qualified_doji_confirmation": False,
                },
            },
            {
                "code": "000003",
                "name": "普通热启动",
                "probability": 0.53,
                "route_score": 68.0,
                "candidate_route": "fresh_mainline_start",
                "probability_factors": {
                    "has_recent_limit_up_event": False,
                    "platform_relaunch_event_ready": False,
                    "limit_up_anchor_gap_pct": 999.0,
                    "strict_confirmation_count": 2,
                },
            },
        ],
        limit=5,
    )

    assert payload["overview"]["pool_total"] == 2
    assert payload["overview"]["doji_count"] == 1
    assert payload["overview"]["sprint_count"] == 1
    assert payload["candidates"][0]["code"] == "000001"
    assert payload["candidates"][0]["limit_up_nearby_pattern_label"] == "涨停板附近十字星"


def test_build_limit_up_platform_diagnostics_focus_on_anchor_doji_and_volume():
    payload = promotion._build_limit_up_platform_diagnostics_payload(
        [
            {
                "code": "000001",
                "name": "已进池",
                "probability": 0.48,
                "route_score": 68.0,
                "candidate_route": "platform_relaunch",
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "limit_up_anchor_gap_pct": 1.2,
                "limit_up_nearby_doji_confirmation": True,
                "limit_up_nearby_signal_score": 82.0,
                "probability_factors": {
                    "has_recent_limit_up_event": True,
                    "limit_up_anchor_gap_pct": 1.2,
                    "limit_up_nearby_doji_confirmation": True,
                    "limit_up_nearby_signal_score": 82.0,
                    "has_doji_confirmation": True,
                    "qualified_doji_confirmation": True,
                    "has_volume_suffocation": True,
                    "has_volume_contraction": True,
                    "volume_suffocation_ratio": 0.58,
                    "platform_cycle_type": "short",
                    "platform_cycle_label": "短平台",
                },
                "memory_features": {"memory_score": 66.0, "memory_last_limit_up_date": "2026-04-18"},
            },
            {
                "code": "000002",
                "name": "离板偏远",
                "probability": 0.36,
                "route_score": 58.0,
                "candidate_route": "platform_relaunch",
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "signal_summary": "平台二次点火",
                "primary_reason": "平台二次点火",
                "secondary_reason": "等待贴板",
                "latest_as_of": "2026-04-23T14:55:00",
                "limit_up_anchor_gap_pct": 6.1,
                "limit_up_nearby_doji_confirmation": False,
                "limit_up_nearby_signal_score": 48.0,
                "probability_factors": {
                    "has_recent_limit_up_event": True,
                    "limit_up_anchor_gap_pct": 6.1,
                    "limit_up_nearby_doji_confirmation": False,
                    "limit_up_nearby_signal_score": 48.0,
                    "has_doji_confirmation": False,
                    "qualified_doji_confirmation": False,
                    "has_volume_suffocation": True,
                    "has_volume_contraction": True,
                    "volume_suffocation_ratio": 0.62,
                    "platform_cycle_type": "mid",
                    "platform_cycle_label": "中平台",
                },
                "memory_features": {"memory_score": 60.0, "memory_last_limit_up_date": "2026-04-16"},
            },
            {
                "code": "000003",
                "name": "差十字星",
                "probability": 0.41,
                "route_score": 61.0,
                "candidate_route": "platform_relaunch",
                "time_horizon": "sprint",
                "time_horizon_label": "1-2日冲刺",
                "signal_summary": "板后贴板横盘",
                "primary_reason": "平台二次点火",
                "secondary_reason": "等待确认K",
                "latest_as_of": "2026-04-23T14:55:00",
                "limit_up_anchor_gap_pct": 2.0,
                "limit_up_nearby_doji_confirmation": False,
                "limit_up_nearby_signal_score": 62.0,
                "probability_factors": {
                    "has_recent_limit_up_event": True,
                    "limit_up_anchor_gap_pct": 2.0,
                    "limit_up_nearby_doji_confirmation": False,
                    "limit_up_nearby_signal_score": 62.0,
                    "has_doji_confirmation": False,
                    "qualified_doji_confirmation": False,
                    "has_volume_suffocation": True,
                    "has_volume_contraction": True,
                    "volume_suffocation_ratio": 0.60,
                    "platform_cycle_type": "short",
                    "platform_cycle_label": "短平台",
                },
                "memory_features": {"memory_score": 58.0, "memory_last_limit_up_date": "2026-04-17"},
            },
            {
                "code": "000004",
                "name": "差量窒息",
                "probability": 0.39,
                "route_score": 59.0,
                "candidate_route": "platform_relaunch",
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "signal_summary": "板后贴板横盘",
                "primary_reason": "平台二次点火",
                "secondary_reason": "等待缩量",
                "latest_as_of": "2026-04-23T14:55:00",
                "limit_up_anchor_gap_pct": 2.6,
                "limit_up_nearby_doji_confirmation": False,
                "limit_up_nearby_signal_score": 57.0,
                "probability_factors": {
                    "has_recent_limit_up_event": True,
                    "limit_up_anchor_gap_pct": 2.6,
                    "limit_up_nearby_doji_confirmation": False,
                    "limit_up_nearby_signal_score": 57.0,
                    "has_doji_confirmation": True,
                    "qualified_doji_confirmation": True,
                    "has_volume_suffocation": False,
                    "has_volume_contraction": True,
                    "volume_suffocation_ratio": 0.91,
                    "platform_cycle_type": "short",
                    "platform_cycle_label": "短平台",
                },
                "memory_features": {"memory_score": 54.0, "memory_last_limit_up_date": "2026-04-17"},
            },
            {
                "code": "000005",
                "name": "再差一个十字星",
                "probability": 0.37,
                "route_score": 56.0,
                "candidate_route": "platform_relaunch",
                "time_horizon": "watch",
                "time_horizon_label": "首板梯队",
                "signal_summary": "板后贴板横盘",
                "primary_reason": "平台二次点火",
                "secondary_reason": "等待确认K",
                "latest_as_of": "2026-04-23T14:55:00",
                "limit_up_anchor_gap_pct": 2.2,
                "limit_up_nearby_doji_confirmation": False,
                "limit_up_nearby_signal_score": 55.0,
                "probability_factors": {
                    "has_recent_limit_up_event": True,
                    "limit_up_anchor_gap_pct": 2.2,
                    "limit_up_nearby_doji_confirmation": False,
                    "limit_up_nearby_signal_score": 55.0,
                    "has_doji_confirmation": False,
                    "qualified_doji_confirmation": False,
                    "has_volume_suffocation": True,
                    "has_volume_contraction": True,
                    "volume_suffocation_ratio": 0.63,
                    "platform_cycle_type": "short",
                    "platform_cycle_label": "短平台",
                },
                "memory_features": {"memory_score": 52.0, "memory_last_limit_up_date": "2026-04-17"},
            },
        ],
        [{"code": "000001", "name": "已进池"}],
        limit_per_group=2,
    )

    assert payload["blocked_total"] == 4
    assert payload["overview"]["dominant_reason"] == "还差十字星确认"
    assert [item["reason"] for item in payload["reason_summary"]] == [
        "还差十字星确认",
        "离板锚点仍偏远",
        "还差量窒息",
    ]
    assert payload["blocked_reason_groups"][0]["examples"][0]["code"] == "000002"
    assert "离板锚点先收敛到" in payload["blocked_reason_groups"][0]["examples"][0]["next_threshold_hint"]
    assert any(
        "贴板缩量十字" in example["next_threshold_hint"] or "更贴板" in example["next_threshold_hint"]
        for group in payload["blocked_reason_groups"]
        if group["reason"] == "还差十字星确认"
        for example in group["examples"]
    )
    assert any(
        "量窒息比先压到" in example["next_threshold_hint"]
        for group in payload["blocked_reason_groups"]
        if group["reason"] == "还差量窒息"
        for example in group["examples"]
    )


def test_second_board_probability_penalizes_weak_tail_boards_in_high_board_market():
    market_context = {"board_height": 5, "high_board_count": 4, "weak_seal_market": True}

    strong_prob, strong_meta = promotion._adjust_second_board_probability(
        0.40,
        item={"turnover": 7.0, "break_count": 0, "limit_up_time": "09:33:01"},
        sector={
            "strength_score": 82,
            "consecutive_days": 2,
            "limit_up_count": 4,
            "fund_flow": 6.2,
            "change_pct": 2.4,
        },
        market_context=market_context,
    )
    weak_prob, weak_meta = promotion._adjust_second_board_probability(
        0.40,
        item={"turnover": 18.5, "break_count": 2, "limit_up_time": "14:42:20"},
        sector={
            "strength_score": 28,
            "consecutive_days": 0,
            "limit_up_count": 1,
            "fund_flow": -1.8,
            "change_pct": -0.5,
        },
        market_context=market_context,
    )

    assert strong_prob > weak_prob
    assert strong_meta["early_seal_bonus"] > 0
    assert weak_meta["distribution_penalty"] > strong_meta["distribution_penalty"]


def test_parse_limit_up_minutes_accepts_compact_eastmoney_time():
    assert promotion._parse_limit_up_minutes("092500") == 9 * 60 + 25
    assert promotion._parse_limit_up_minutes("093856") == 9 * 60 + 38
    assert promotion._parse_limit_up_minutes("09:38:56") == 9 * 60 + 38
    assert promotion._parse_limit_up_minutes("925") == 9 * 60 + 25


def test_second_board_probability_prioritizes_auction_hard_seal_in_fragile_market():
    fragile_context = {
        "board_height": 6,
        "high_board_count": 6,
        "weak_seal_market": True,
        "weak_follow_through_market": True,
        "broad_first_board_overflow": True,
        "avg_break_count": 1.35,
        "broken_ratio": 0.44,
        "first_board_count": 95,
        "market_risk_level": "hostile",
    }
    weak_sector = {
        "strength_score": 22,
        "consecutive_days": 0,
        "limit_up_count": 1,
        "fund_flow": -0.5,
        "change_pct": 0.2,
    }
    strong_sector = {
        "strength_score": 76,
        "consecutive_days": 2,
        "limit_up_count": 5,
        "fund_flow": 5.0,
        "change_pct": 2.2,
    }

    hard_prob, hard_meta = promotion._adjust_second_board_probability(
        0.38,
        item={
            "turnover": 0.58,
            "break_count": 0,
            "limit_up_time": "092500",
            "seal_amount": 537_000_000,
        },
        sector=weak_sector,
        market_context=fragile_context,
    )
    late_prob, late_meta = promotion._adjust_second_board_probability(
        0.38,
        item={
            "turnover": 4.2,
            "break_count": 0,
            "limit_up_time": "145432",
            "seal_amount": 132_000_000,
        },
        sector=strong_sector,
        market_context=fragile_context,
        spot=StockSpot(code="000004", support_strength_score=88, circ_market_cap=120),
    )

    assert hard_prob > late_prob
    assert hard_meta["is_one_word_like"] is True
    assert hard_meta["early_seal_bonus"] >= 0.07
    assert hard_meta["seal_strength_bonus"] > late_meta["seal_strength_bonus"]
    assert late_meta["tail_seal_penalty"] > 0


def test_second_board_hostile_low_rotation_hard_board_has_probability_floor():
    hostile_context = {
        "board_height": 5,
        "high_board_count": 5,
        "weak_seal_market": True,
        "weak_follow_through_market": True,
        "broad_first_board_overflow": True,
        "avg_break_count": 1.25,
        "broken_ratio": 0.42,
        "first_board_count": 88,
        "market_risk_level": "hostile",
    }
    sector = {
        "strength_score": 54,
        "consecutive_days": 1,
        "limit_up_count": 4,
        "fund_flow": 2.6,
        "change_pct": 1.5,
        "sector_strength_delta": 14.0,
        "sector_limit_up_delta": 3.0,
        "sector_rotation_score": 63.0,
        "sector_low_position_rotation": True,
    }
    prob, meta = promotion._adjust_second_board_probability(
        0.01,
        item={
            "turnover": 4.8,
            "break_count": 0,
            "limit_up_time": "09:34:18",
            "seal_amount": 120_000_000,
        },
        sector=sector,
        market_context=hostile_context,
        spot=StockSpot(code="000055", support_strength_score=62, circ_market_cap=110),
        fund_flow=FundFlow(
            code="000055",
            trade_date=date(2026, 6, 1),
            main_net_inflow=68_000_000,
            main_net_inflow_pct=9.2,
        ),
        reason_context={
            "reason_cohort_count": 4,
            "reason_avg_turnover": 6.2,
            "reason_early_seal_ratio": 0.5,
            "reason_avg_support_strength": 62.0,
            "reason_avg_break_count": 0.8,
            "reason_strong_cluster": True,
        },
    )

    assert prob >= 0.12
    assert meta["second_board_route"] == "low_rotation_cluster_second_board"
    assert meta["hard_board_bonus"] > 0
    assert meta["has_strong_main_inflow"] is True
    assert meta["is_clean_early_hard_board"] is True


def test_market_ladder_context_flags_fragile_first_board_overflow():
    limit_ups = [
        {"consecutive_days": 1, "break_count": 0 if index % 3 == 0 else 1}
        for index in range(60)
    ] + [
        {"consecutive_days": 3, "break_count": 2},
        {"consecutive_days": 4, "break_count": 0},
    ]

    context = promotion._build_market_ladder_context(limit_ups)

    assert context["first_board_count"] == 60
    assert context["board_height"] == 4
    assert context["broad_first_board_overflow"] is True
    assert context["weak_follow_through_market"] is True
    assert context["broken_ratio"] > 0.4
    assert context["avg_break_count"] > 0.65


def test_second_board_probability_penalizes_fragile_first_board_overflow_market():
    normal_context = {
        "board_height": 3,
        "high_board_count": 2,
        "weak_seal_market": False,
        "weak_follow_through_market": False,
        "broad_first_board_overflow": False,
        "avg_break_count": 0.4,
        "broken_ratio": 0.22,
        "first_board_count": 28,
    }
    fragile_context = {
        "board_height": 6,
        "high_board_count": 6,
        "weak_seal_market": True,
        "weak_follow_through_market": True,
        "broad_first_board_overflow": True,
        "avg_break_count": 1.35,
        "broken_ratio": 0.44,
        "first_board_count": 95,
    }
    item = {"turnover": 7.5, "break_count": 0, "limit_up_time": "09:36:00"}
    sector = {
        "strength_score": 74,
        "consecutive_days": 2,
        "limit_up_count": 5,
        "fund_flow": 4.0,
        "change_pct": 2.1,
    }

    normal_prob, normal_meta = promotion._adjust_second_board_probability(
        0.40,
        item=item,
        sector=sector,
        market_context=normal_context,
    )
    fragile_prob, fragile_meta = promotion._adjust_second_board_probability(
        0.40,
        item=item,
        sector=sector,
        market_context=fragile_context,
    )

    assert fragile_prob < normal_prob - 0.08
    assert fragile_meta["distribution_penalty"] > normal_meta["distribution_penalty"]
    assert fragile_meta["market_weak_follow_through"] is True
    assert fragile_meta["market_broad_first_board_overflow"] is True


def test_second_board_probability_rewards_morning_sector_strengthening():
    market_context = {
        "board_height": 4,
        "high_board_count": 2,
        "weak_seal_market": False,
        "weak_follow_through_market": False,
    }
    sector = {
        "strength_score": 64,
        "consecutive_days": 1,
        "limit_up_count": 4,
        "fund_flow": 2.0,
        "change_pct": 1.8,
    }
    weak_item = {
        "turnover": 10.8,
        "break_count": 1,
        "limit_up_time": "14:55:37",
        "seal_amount": 12_000_000,
    }
    base_prob, base_meta = promotion._adjust_second_board_probability(
        0.20,
        item=weak_item,
        sector=sector,
        market_context=market_context,
    )
    boosted_prob, boosted_meta = promotion._adjust_second_board_probability(
        0.20,
        item=weak_item,
        sector=sector,
        market_context=market_context,
        morning_sector_context={
            "sector_morning_strengthening": True,
            "sector_morning_strengthening_score": 72,
            "sector_morning_sector_name": "元件",
            "sector_morning_first_board_count": 5,
            "sector_morning_early_count": 3,
            "sector_morning_hard_count": 2,
        },
    )

    assert boosted_prob > base_prob
    assert boosted_meta["morning_strengthening_bonus"] > 0
    assert boosted_meta["morning_penalty_relief"] > 0
    assert boosted_meta["sector_morning_strengthening"] is True
    assert boosted_meta["distribution_penalty"] <= base_meta["distribution_penalty"]


def test_dragon_tiger_context_scores_member_net_buy():
    context = promotion._build_dragon_tiger_context(
        [
            {
                "代码": "000004",
                "解读": "2家机构买入，成功率较高",
                "龙虎榜净买额": 135_000_000,
                "龙虎榜买入额": 220_000_000,
                "龙虎榜卖出额": 85_000_000,
                "龙虎榜成交额": 305_000_000,
                "市场总成交额": 900_000_000,
                "净买额占总成交比": 15.0,
                "成交额占总成交比": 33.9,
                "换手率": 8.2,
                "上榜原因": "日涨幅偏离值达到7%的前5只证券",
            }
        ],
        [
            {
                "交易营业部名称": "机构专用",
                "买入金额": 80_000_000,
                "卖出金额": 5_000_000,
                "净额": 75_000_000,
                "类型": "日涨幅偏离值达到7%的前5只证券",
            },
            {
                "交易营业部名称": "华泰证券股份有限公司总部",
                "买入金额": 55_000_000,
                "卖出金额": 2_000_000,
                "净额": 53_000_000,
                "类型": "日涨幅偏离值达到7%的前5只证券",
            },
        ],
        [
            {
                "交易营业部名称": "深股通专用",
                "买入金额": 10_000_000,
                "卖出金额": 35_000_000,
                "净额": -25_000_000,
                "类型": "日涨幅偏离值达到7%的前5只证券",
            }
        ],
    )

    assert context["is_listed"] is True
    assert context["probability_delta"] > 0
    assert context["institution_net_amount"] == 75_000_000
    assert context["top_buy_members"][0]["name"] == "机构专用"
    assert "龙虎榜净买入" in context["summary"]


def test_second_board_probability_uses_dragon_tiger_member_delta():
    market_context = {"board_height": 3, "high_board_count": 1, "weak_seal_market": False}
    common_item = {"turnover": 6.5, "break_count": 0, "limit_up_time": "09:35:00"}
    common_sector = {
        "strength_score": 70,
        "consecutive_days": 3,
        "limit_up_count": 5,
        "fund_flow": 3.0,
        "change_pct": 2.0,
    }

    base_prob, _base_meta = promotion._adjust_second_board_probability(
        0.40,
        item=common_item,
        sector=common_sector,
        market_context=market_context,
    )
    positive_prob, positive_meta = promotion._adjust_second_board_probability(
        0.40,
        item=common_item,
        sector=common_sector,
        market_context=market_context,
        dragon_tiger={
            "is_listed": True,
            "member_score": 78,
            "probability_delta": 0.05,
            "summary": "龙虎榜净买入1.2亿，机构净买6000万",
        },
    )
    negative_prob, negative_meta = promotion._adjust_second_board_probability(
        0.40,
        item=common_item,
        sector=common_sector,
        market_context=market_context,
        dragon_tiger={
            "is_listed": True,
            "member_score": 22,
            "probability_delta": -0.06,
            "summary": "龙虎榜净卖出1.4亿，机构净卖8000万",
        },
    )

    assert positive_prob > base_prob > negative_prob
    assert positive_meta["dragon_tiger_probability_delta"] == 0.05
    assert negative_meta["dragon_tiger_probability_delta"] == -0.06


def test_second_board_probability_uses_fund_flow_confirmation():
    market_context = {"board_height": 3, "high_board_count": 1, "weak_seal_market": False}
    common_item = {"turnover": 5.8, "break_count": 0, "limit_up_time": "09:34:00"}
    common_sector = {
        "strength_score": 72,
        "consecutive_days": 3,
        "limit_up_count": 5,
        "fund_flow": 3.0,
        "change_pct": 2.0,
    }
    spot = StockSpot(code="000004", support_strength_score=70, circ_market_cap=120, amount=600_000_000)

    base_prob, _base_meta = promotion._adjust_second_board_probability(
        0.38,
        item=common_item,
        sector=common_sector,
        market_context=market_context,
        spot=spot,
    )
    positive_prob, positive_meta = promotion._adjust_second_board_probability(
        0.38,
        item=common_item,
        sector=common_sector,
        market_context=market_context,
        spot=spot,
        fund_flow=FundFlow(main_net_inflow=90_000_000, main_net_inflow_pct=12.0),
    )
    negative_prob, negative_meta = promotion._adjust_second_board_probability(
        0.38,
        item=common_item,
        sector=common_sector,
        market_context=market_context,
        spot=spot,
        fund_flow=FundFlow(main_net_inflow=-40_000_000, main_net_inflow_pct=-6.0),
    )
    zero_flow_prob, zero_flow_meta = promotion._adjust_second_board_probability(
        0.38,
        item=common_item,
        sector=common_sector,
        market_context=market_context,
        spot=StockSpot(
            code="000004",
            support_strength_score=70,
            circ_market_cap=120,
            amount=600_000_000,
            main_net_inflow=90_000_000,
        ),
        fund_flow=FundFlow(main_net_inflow=0, main_net_inflow_pct=0),
    )

    assert positive_prob > base_prob > negative_prob
    assert positive_meta["main_net_inflow_pct"] == 12.0
    assert zero_flow_prob < positive_prob
    assert zero_flow_meta["main_net_inflow"] == 0
    assert zero_flow_meta["main_net_inflow_pct"] == 0
    assert negative_meta["style_penalty"] > positive_meta["style_penalty"]


@pytest.mark.asyncio
async def test_promotion_candidates_returns_first_and_second_board_lists(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "snapshot_time": "2026-04-23T14:55:00",
            "anomalies": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "capital",
                    "score": 88,
                    "description": "资金净额 1.5亿",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 9.82,
                        "change_pct": 6.4,
                        "main_net_inflow": 150_000_000,
                        "main_net_inflow_pct": 9.6,
                        "volume_ratio": 2.1,
                        "support_strength_score": 74,
                        "as_of": "2026-04-23T14:55:00",
                    },
                },
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "breakthrough",
                    "score": 81,
                    "description": "突破60日新高",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 9.82,
                        "change_pct": 6.4,
                        "volume_ratio": 2.1,
                        "support_strength_score": 74,
                        "as_of": "2026-04-23T14:55:10",
                    },
                },
            ],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "total_score": 82,
                    "level": "A",
                }
            ]
        }

    lhb_context = {
        "000004": {
            "is_listed": True,
            "detail_available": True,
            "source": "eastmoney",
            "summary": "龙虎榜净买入1.1亿，机构净买5000万",
            "reasons": ["日涨幅偏离值达到7%的前5只证券"],
            "interpretation": "",
            "net_buy_amount": 110_000_000,
            "buy_amount": 180_000_000,
            "sell_amount": 70_000_000,
            "trade_amount": 250_000_000,
            "market_amount": 800_000_000,
            "net_buy_pct": 13.75,
            "trade_amount_pct": 31.25,
            "turnover": 9.5,
            "institution_net_amount": 50_000_000,
            "northbound_net_amount": 0,
            "broker_branch_net_amount": 60_000_000,
            "positive_member_count": 4,
            "negative_member_count": 1,
            "top_buy_members": [
                {
                    "name": "机构专用",
                    "side": "buy",
                    "category": "institution",
                    "buy_amount": 60_000_000,
                    "sell_amount": 10_000_000,
                    "net_amount": 50_000_000,
                }
            ],
            "top_sell_members": [],
            "member_score": 78,
            "probability_delta": 0.05,
            "risk_flags": [],
        }
    }

    async def fake_lhb_context_map(trade_date, codes):
        return lhb_context

    def fake_cached_lhb_context_map(trade_date, codes):
        return lhb_context

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)
    monkeypatch.setattr(promotion, "_load_dragon_tiger_context_map", fake_lhb_context_map)
    monkeypatch.setattr(promotion, "_load_cached_dragon_tiger_context_map", fake_cached_lhb_context_map)

    response = await client.get("/api/v1/promotion/candidates?limit=8&ranked_limit=12")
    assert response.status_code == 200
    payload = response.json()

    assert payload["trade_date"] == "2026-04-23"
    assert payload["snapshot_time"] == "2026-04-23T14:55:00"
    assert payload["first_board_candidates"][0]["code"] == "000010"
    assert payload["first_board_candidates"][0]["target_board"] == 1
    assert payload["first_board_candidates"][0]["main_probability_name"] == "次日首板概率（低基准率）"
    assert payload["first_board_candidates"][0]["signal_status_label"] == "盘中预判"
    assert payload["first_board_candidates"][0]["kline_confirmation"]["has_doji_confirmation"] is True
    assert payload["first_board_candidates"][0]["probability_factors"]["kline_confirmation_score"] > 0.45
    assert "first_board_pre_sprint_candidates" in payload
    assert "first_board_weak_watch_candidates" in payload
    assert "first_board_diagnostics" in payload
    assert "blocked_reason_groups" in payload["first_board_diagnostics"]
    # 000004 当日炸板且换手偏高，仅保留为 shadow 学习样本，
    # 不再冒充正式二板预测抬高命中率分母。
    assert payload["second_board_candidates"] == []
    assert payload["ranked_second_board_candidates"] == []
    assert payload["ranked_first_board_candidates"][0]["target_board"] == 1
    assert payload["ranked_first_board_candidates"][0]["main_probability_name"] == "次日首板概率（低基准率）"
    assert "ranked_candidates" not in payload
    assert {item["target_board"] for item in payload["debug"]["mixed_ranked_candidates"]} == {1}
    assert "联调排查" in payload["debug"]["notes"][0]


@pytest.mark.asyncio
async def test_promotion_candidates_include_hot_mainline_relay_without_anomaly_trigger(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)
        session.add_all(
            [
                StockSectorMapping(
                    code="000020",
                    sector_code="S1",
                    sector_name="AI应用",
                    sector_type="concept",
                    source="test",
                ),
                StockTag(code="000020", name="补涨股", board_type="main_sz", board_tag="tradeable"),
                StockSpot(
                    code="000020",
                    name="补涨股",
                    price=10.58,
                    prev_close=10.12,
                    open=10.20,
                    high=10.62,
                    low=10.16,
                    change_pct=4.55,
                    amount=420_000_000,
                    turnover=6.2,
                    volume_ratio=1.8,
                    main_net_inflow=72_000_000,
                    support_strength_score=74,
                    updated_at=datetime(2026, 4, 23, 14, 55),
                ),
                FundFlow(
                    code="000020",
                    name="补涨股",
                    trade_date=date(2026, 4, 23),
                    main_net_inflow=72_000_000,
                    main_net_inflow_pct=17.1,
                    big_net_inflow=30_000_000,
                    mid_net_inflow=10_000_000,
                    small_net_inflow=-12_000_000,
                ),
            ]
        )
        trend_bars = [
            (date(2026, 4, 14), 9.80, 9.92, 9.96, 9.72, 120000),
            (date(2026, 4, 15), 9.93, 10.02, 10.06, 9.86, 124000),
            (date(2026, 4, 16), 10.02, 10.10, 10.14, 9.98, 128000),
            (date(2026, 4, 17), 10.08, 10.18, 10.22, 10.04, 132000),
            (date(2026, 4, 20), 10.16, 10.24, 10.28, 10.12, 138000),
            (date(2026, 4, 21), 10.22, 10.32, 10.36, 10.18, 144000),
            (date(2026, 4, 22), 10.30, 10.42, 10.46, 10.26, 156000),
            (date(2026, 4, 23), 10.40, 10.58, 10.62, 10.34, 178000),
        ]
        session.add_all(
            [
                StockKline(
                    code="000020",
                    trade_date=trade_day,
                    open=open_price,
                    close=close_price,
                    high=high_price,
                    low=low_price,
                    volume=volume,
                    source="ths",
                )
                for trade_day, open_price, close_price, high_price, low_price, volume in trend_bars
            ]
        )
        await session.commit()

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {"snapshot_time": "2026-04-23T14:55:00", "anomalies": []}

    async def fake_bull_rank(db):
        return {"rank": []}

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)

    response = await client.get("/api/v1/promotion/candidates?limit=8&ranked_limit=12")
    assert response.status_code == 200
    payload = response.json()

    ranked_codes = [item["code"] for item in payload["ranked_first_board_candidates"]]
    assert "000020" in ranked_codes
    candidate = next(item for item in payload["ranked_first_board_candidates"] if item["code"] == "000020")
    assert candidate["candidate_route"] == "fresh_mainline_start"
    assert "mainline_relay" in candidate["event_types"]
    assert candidate["probability_factors"]["trend_acceleration_ready"] is True


@pytest.mark.asyncio
async def test_hot_mainline_relay_replay_uses_kline_instead_of_future_spot(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_candidate_context(session)
        session.add_all(
            [
                StockSectorMapping(
                    code="000021",
                    sector_code="S1",
                    sector_name="AI应用",
                    sector_type="concept",
                    source="test",
                ),
                StockSpot(
                    code="000021",
                    name="未来异动股",
                    price=11.20,
                    prev_close=10.60,
                    open=10.70,
                    high=11.22,
                    low=10.62,
                    change_pct=5.66,
                    amount=360_000_000,
                    turnover=6.0,
                    volume_ratio=1.9,
                    main_net_inflow=86_000_000,
                    support_strength_score=78,
                    updated_at=datetime(2026, 4, 24, 10, 30),
                ),
                StockKline(
                    code="399999",
                    trade_date=date(2026, 4, 24),
                    open=1.0,
                    close=1.0,
                    high=1.0,
                    low=1.0,
                    volume=1000,
                    amount=1000,
                    turnover=0.1,
                    change_pct=0.0,
                    prev_close=1.0,
                    source="test",
                ),
            ]
        )
        replay_bars = [
            (date(2026, 4, 16), 10.00, 10.04, 10.10, 9.94, 210000, 0.40, 10.00),
            (date(2026, 4, 17), 10.04, 10.05, 10.12, 9.98, 205000, 0.10, 10.04),
            (date(2026, 4, 20), 10.05, 10.07, 10.14, 10.00, 202000, 0.20, 10.05),
            (date(2026, 4, 21), 10.07, 10.08, 10.15, 10.01, 198000, 0.10, 10.07),
            (date(2026, 4, 22), 10.08, 10.09, 10.16, 10.02, 196000, 0.10, 10.08),
            (date(2026, 4, 23), 10.09, 10.11, 10.18, 10.03, 194000, 0.20, 10.09),
        ]
        session.add_all(
            [
                StockKline(
                    code="000021",
                    trade_date=trade_day,
                    open=open_price,
                    close=close_price,
                    high=high_price,
                    low=low_price,
                    volume=volume,
                    amount=2_000_000,
                    turnover=5.0,
                    change_pct=change_pct,
                    prev_close=prev_close,
                    source="ths",
                )
                for trade_day, open_price, close_price, high_price, low_price, volume, change_pct, prev_close in replay_bars
            ]
        )
        await session.commit()

        spots = await promotion._load_hot_mainline_relay_spots(
            session,
            date(2026, 4, 23),
            exclude_codes=set(),
        )

    assert "000021" not in {str(item.code or "") for item in spots}


@pytest.mark.asyncio
async def test_promotion_candidates_diagnostics_skip_limit_down_only_rows(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "snapshot_time": "2026-04-23T14:55:00",
            "anomalies": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "capital",
                    "score": 88,
                    "description": "资金净额 0.4亿",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 9.82,
                        "change_pct": 0.4,
                        "main_net_inflow": 40_000_000,
                        "main_net_inflow_pct": 3.6,
                        "volume_ratio": 1.3,
                        "support_strength_score": 68,
                        "as_of": "2026-04-23T14:55:00",
                    },
                },
                {
                    "code": "000099",
                    "name": "高位跌停票",
                    "event_type": "limit_down",
                    "score": 80,
                    "description": "高位跌停",
                    "setup_grade": "B类观察候选",
                    "setup_grade_display": "B类观察候选",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 18.2,
                        "change_pct": -10.0,
                        "volume_ratio": 0.9,
                        "support_strength_score": 22,
                        "as_of": "2026-04-23T14:55:00",
                    },
                },
            ],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "total_score": 82,
                    "level": "A",
                }
            ]
        }

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)

    response = await client.get("/api/v1/promotion/candidates?limit=8&ranked_limit=12")
    assert response.status_code == 200
    payload = response.json()

    blocked_codes = [
        example["code"]
        for group in payload["first_board_diagnostics"]["blocked_reason_groups"]
        for example in group["examples"]
    ]
    assert "000010" in blocked_codes
    assert "000099" not in blocked_codes
    assert payload["first_board_diagnostics"]["blocked_total"] == 1
    assert payload["first_board_diagnostics"]["notes"][0].startswith("诊断样本已剔除跌停")


@pytest.mark.asyncio
async def test_load_recent_limit_up_memory_filters_invalid_limit_up_pool_rows_by_kline_change_pct(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="002230",
                    name="科大讯飞",
                    trade_date=date(2026, 4, 6),
                    consecutive_days=2,
                    seal_amount=50_000_000,
                    break_count=0,
                    turnover=8.5,
                    source="test",
                ),
                LimitUpPool(
                    code="002230",
                    name="科大讯飞",
                    trade_date=date(2026, 4, 10),
                    consecutive_days=3,
                    seal_amount=445_000_000,
                    break_count=1,
                    turnover=8.9,
                    source="test",
                ),
                LimitUpPool(
                    code="002230",
                    name="科大讯飞",
                    trade_date=date(2026, 4, 8),
                    consecutive_days=2,
                    seal_amount=50_000_000,
                    break_count=0,
                    turnover=8.5,
                    source="test",
                ),
                StockKline(
                    code="002230",
                    trade_date=date(2026, 4, 10),
                    open=47.5,
                    close=47.64,
                    high=48.33,
                    low=47.47,
                    volume=100000,
                    amount=1_000_000,
                    turnover=1.78,
                    change_pct=0.91,
                    prev_close=47.21,
                    source="ths",
                ),
                StockKline(
                    code="002230",
                    trade_date=date(2026, 4, 8),
                    open=46.5,
                    close=47.91,
                    high=47.92,
                    low=46.5,
                    volume=100000,
                    amount=1_000_000,
                    turnover=2.39,
                    change_pct=5.11,
                    prev_close=45.58,
                    source="ths",
                ),
            ]
        )
        await session.commit()

        features = await promotion._load_recent_limit_up_memory(
            session,
            ["002230"],
            date(2026, 4, 25),
        )

    memory = features["002230"]
    assert memory["memory_score"] == 0.0
    assert memory["memory_limit_up_hits_50d"] == 0
    assert memory["memory_raw_limit_up_hits_50d"] == 3
    assert memory["memory_invalid_limit_up_hits_50d"] == 3
    assert memory["memory_missing_kline_limit_up_hits_50d"] == 1
    assert memory["memory_last_limit_up_date"] is None
    assert memory["memory_validation_mode"] == "stock_kline.change_pct+missing_kline_filter"


def test_first_board_probability_uses_limit_up_nearby_doji_as_platform_relaunch_signal():
    row = {
        "setup_grade": "A2 盘口确认后执行",
        "display_score": 98,
        "event_types": ["capital", "breakthrough"],
        "detail": {
            "main_net_inflow_pct": 8.1,
            "volume_ratio": 1.72,
            "support_strength_score": 63,
            "change_pct": 4.2,
            "turnover": 5.4,
            "open": 10.00,
            "high": 10.08,
            "low": 9.96,
            "price": 10.04,
        },
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 71}
    market_context = {
        "board_height": 3,
        "high_board_count": 1,
        "weak_seal_market": False,
        "limit_up_count": 34,
        "sealed_ratio": 0.72,
    }
    kline_context = {
        "confirmation_bonus": 0.08,
        "confirmation_penalty": 0.01,
        "confirmation_score": 0.49,
        "has_doji_confirmation": True,
        "qualified_doji_confirmation": True,
        "has_platform_contraction": True,
        "has_volume_contraction": True,
        "has_volume_suffocation": False,
        "near_breakout": True,
        "strict_confirmation_count": 2,
        "strict_ready": True,
        "overhead_gap_pct": 1.8,
        "failed_reversal_count": 0,
        "latest_close": 10.04,
    }
    memory_features = {
        "memory_score": 34.0,
        "memory_limit_up_hits_50d": 1,
        "memory_days_since_last_limit_up": 8,
        "memory_last_limit_up_close": 10.05,
        "memory_last_limit_up_high": 10.08,
    }

    probability, meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 61,
            "consecutive_days": 2,
            "limit_up_count": 3,
            "fund_flow": 3.2,
            "change_pct": 2.1,
        },
        market_context,
        kline_context,
        memory_features,
    )

    assert probability > 0
    assert meta["candidate_route"] == "platform_relaunch"
    assert meta["limit_up_nearby_doji_confirmation"] is True
    assert meta["limit_up_nearby_pattern_label"] == "涨停板附近十字星"
    assert meta["memory_score"] == 34.0


def test_first_board_probability_uses_support_squeeze_route_for_low_support_squeeze_setup():
    row = {
        "setup_grade": "B类观察候选",
        "display_score": 84,
        "event_types": ["stealth_setup"],
        "detail": {
            "main_net_inflow_pct": 1.8,
            "volume_ratio": 0.82,
            "support_strength_score": 58,
            "change_pct": 2.3,
            "turnover": 3.6,
            "open": 10.12,
            "high": 10.28,
            "low": 10.02,
            "price": 10.24,
        },
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 67}
    market_context = {
        "board_height": 2,
        "high_board_count": 0,
        "weak_seal_market": False,
        "limit_up_count": 28,
        "sealed_ratio": 0.68,
    }
    kline_context = {
        "confirmation_bonus": 0.07,
        "confirmation_penalty": 0.01,
        "confirmation_score": 0.66,
        "has_doji_confirmation": False,
        "qualified_doji_confirmation": False,
        "has_platform_contraction": False,
        "has_volume_contraction": True,
        "has_volume_suffocation": True,
        "near_breakout": False,
        "strict_confirmation_count": 3,
        "strict_ready": True,
        "overhead_gap_pct": 4.6,
        "failed_reversal_count": 0,
        "latest_close": 10.24,
        "support_zone_price": 9.98,
        "support_zone_gap_pct": 2.61,
        "support_hold_days": 4,
        "support_rebound_count": 3,
        "lower_shadow_absorption": 0.37,
        "has_support_absorption": True,
    }
    memory_features = {
        "memory_score": 0.0,
        "memory_limit_up_hits_50d": 0,
        "memory_days_since_last_limit_up": 999,
    }

    probability, meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 48,
            "consecutive_days": 1,
            "limit_up_count": 1,
            "fund_flow": 1.5,
            "change_pct": 1.4,
        },
        market_context,
        kline_context,
        memory_features,
    )

    assert probability > 0
    assert meta["candidate_route"] == "support_squeeze_start"
    assert meta["candidate_route_label"] == "低位支撑首波"
    assert meta["support_squeeze_ready"] is True
    assert meta["support_squeeze_pattern_label"] == "低位支撑量窒息"
    assert meta["support_zone_gap_pct"] == 2.61
    assert meta["memory_score"] == 0.0


def test_first_board_probability_uses_support_squeeze_route_for_low_support_volume_release_setup():
    row = {
        "setup_grade": "B类观察候选",
        "display_score": 82,
        "event_types": ["stealth_setup"],
        "detail": {
            "main_net_inflow_pct": 2.4,
            "volume_ratio": 1.58,
            "support_strength_score": 57,
            "change_pct": 2.8,
            "turnover": 4.1,
            "open": 10.18,
            "high": 10.42,
            "low": 10.08,
            "price": 10.36,
        },
        "risk_flags": [],
        "a1_blockers": [],
    }
    bull_row = {"total_score": 65}
    market_context = {
        "board_height": 2,
        "high_board_count": 0,
        "weak_seal_market": False,
        "limit_up_count": 26,
        "sealed_ratio": 0.66,
    }
    kline_context = {
        "confirmation_bonus": 0.06,
        "confirmation_penalty": 0.01,
        "confirmation_score": 0.64,
        "has_doji_confirmation": False,
        "qualified_doji_confirmation": False,
        "has_platform_contraction": False,
        "has_volume_contraction": False,
        "has_volume_suffocation": False,
        "near_breakout": False,
        "strict_confirmation_count": 3,
        "strict_ready": True,
        "overhead_gap_pct": 4.2,
        "failed_reversal_count": 0,
        "latest_close": 10.36,
        "support_zone_price": 10.06,
        "support_zone_gap_pct": 2.98,
        "support_hold_days": 4,
        "support_rebound_count": 3,
        "lower_shadow_absorption": 0.33,
        "has_support_absorption": True,
    }
    memory_features = {
        "memory_score": 0.0,
        "memory_limit_up_hits_50d": 0,
        "memory_days_since_last_limit_up": 999,
    }

    probability, meta = promotion._build_first_board_probability(
        row,
        bull_row,
        {
            "strength_score": 44,
            "consecutive_days": 1,
            "limit_up_count": 1,
            "fund_flow": 1.3,
            "change_pct": 1.1,
        },
        market_context,
        kline_context,
        memory_features,
    )

    assert probability > 0
    assert meta["candidate_route"] == "support_squeeze_start"
    assert meta["candidate_route_label"] == "低位支撑首波"
    assert meta["support_squeeze_ready"] is True
    assert meta["support_volume_release_ready"] is True
    assert meta["support_squeeze_pattern_label"] == "低位支撑放量启动"
    assert meta["support_zone_gap_pct"] == 2.98
    assert meta["memory_score"] == 0.0


@pytest.mark.asyncio
async def test_promotion_candidates_keeps_watch_tier_when_strict_pool_is_empty(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "snapshot_time": "2026-04-23T14:55:00",
            "anomalies": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "capital",
                    "score": 88,
                    "description": "资金净额 1.5亿",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 9.82,
                        "change_pct": 6.4,
                        "main_net_inflow": 150_000_000,
                        "main_net_inflow_pct": 9.6,
                        "volume_ratio": 2.1,
                        "support_strength_score": 74,
                        "as_of": "2026-04-23T14:55:00",
                    },
                },
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "breakthrough",
                    "score": 81,
                    "description": "突破60日新高",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 9.82,
                        "change_pct": 6.4,
                        "volume_ratio": 2.1,
                        "support_strength_score": 74,
                        "as_of": "2026-04-23T14:55:10",
                    },
                },
            ],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "total_score": 82,
                    "level": "A",
                }
            ]
        }

    def fake_passes_strict_first_board_gate(row, *, bull_score, sector, kline_context, memory_features=None, market_context=None):
        return False, ["缺少首波记忆或主线共振"], "strict_stealth_scan"

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)
    monkeypatch.setattr(promotion, "_passes_strict_first_board_gate", fake_passes_strict_first_board_gate)

    response = await client.get("/api/v1/promotion/candidates?limit=8&ranked_limit=12")
    assert response.status_code == 200
    payload = response.json()

    assert payload["first_board_count"] == 0
    assert payload["first_board_watch_count"] >= 1
    assert payload["first_board_watch_candidates"][0]["code"] == "000010"
    assert payload["first_board_watch_candidates"][0]["time_horizon"] == "watch"
    assert payload["first_board_watch_candidates"][0]["time_horizon_label"] == "首板梯队"
    assert payload["first_board_watch_candidates"][0]["watch_bucket_label"] in {
        "平台二次点火",
        "主线补涨",
        "静默蓄势",
        "主线首波点火",
        "分支卡位热启动",
    }
    assert payload["first_board_watch_candidates"][0]["main_uptrend_label"] in {"主升浪预备", "右侧待确认", "继续观察"}
    assert payload["ranked_first_board_count"] == 1
    assert payload["debug"]["notes"][1] == "首板请使用 ranked_first_board_candidates，二板请使用 ranked_second_board_candidates"
    assert payload["ranked_first_board_candidates"][0]["code"] == "000010"
    assert payload["ranked_first_board_candidates"][0]["prediction_actionable"] is False
    assert payload["ranked_first_board_recall_candidates"][0]["code"] == "000010"
    assert payload["first_board_watch_overview"]["watch_total"] >= 1
    assert payload["first_board_watch_groups"][0]["bucket"] in {
        "fresh_mainline_start",
        "fresh_relay_start",
        "platform_relaunch",
        "mainline_relay",
        "quiet_setup",
        "other",
    }
    assert any(
        example["code"] == "000010"
        for group in payload["first_board_diagnostics"]["blocked_reason_groups"]
        for example in group["examples"]
    )


@pytest.mark.asyncio
async def test_promotion_candidates_ranked_lists_use_ranked_limit_depth(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)
        await session.commit()

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "trade_date": "2026-04-23",
            "snapshot_time": "2026-04-23T14:55:00",
            "anomalies": [],
        }

    def build_first_board_candidate(index: int) -> dict:
        probability = round(0.88 - index * 0.03, 3)
        return {
            "code": f"F{index:03d}",
            "name": f"首板{index}",
            "target_board": 1,
            "target_label": "冲首板",
            "probability": probability,
            "main_probability_name": "首板观察参照概率",
            "sub_probabilities": {
                "first_limitup_next_day": probability,
                "breakout_3d": min(probability + 0.12, 0.99),
                "first_limitup_5d": min(probability + 0.08, 0.99),
                "become_core_10d": min(probability + 0.06, 0.99),
            },
            "confidence": 0.65,
            "confidence_level": "medium",
            "confidence_label": "中",
            "setup_grade": "A2 盘口确认后执行",
            "setup_grade_display": "A2 盘口确认后执行",
            "signal_summary": "平台二次点火",
            "primary_reason": "测试首板候选",
            "secondary_reason": "测试补充说明",
            "current_price": 10.0 + index,
            "change_pct": 4.0 + index * 0.1,
            "turnover": 4.8,
            "volume_ratio": 1.5,
            "main_net_inflow": 12_000_000,
            "main_net_inflow_pct": 6.2,
            "support_strength_score": 72,
            "bull_score": 82,
            "bull_level": "A",
            "display_score": 91 - index,
            "event_types": ["capital", "breakthrough"],
            "sector_name": f"测试主线{index}",
            "sector_strength_score": 80,
            "sector_limit_up_count": 3,
            "sector_consecutive_days": 2,
            "candidate_route": "platform_relaunch",
            "candidate_route_label": "平台二次点火",
            "route_score": 76 - index,
            "memory_features": {"memory_score": 68 - index},
            "has_recent_limit_up_event": True,
            "platform_relaunch_event_ready": True,
            "limit_up_anchor_gap_pct": 1.2 + index * 0.2,
            "limit_up_nearby_doji_confirmation": index % 2 == 0,
            "limit_up_nearby_signal_score": 82 - index,
            "limit_up_nearby_pattern_label": "涨停板附近十字星" if index % 2 == 0 else "涨停后板附近横盘",
            "probability_factors": {
                "sector_code": f"test_sector_{index}",
                "sector_name": f"测试主线{index}",
                "news_mapping_mode": "direct_code",
                "news_event_grade": "hard",
                "news_fresh_after_trade_close": True,
                "news_catalyst_score": promotion.NEWS_CATALYST_MIN_SCORE + 12,
                "has_recent_limit_up_event": True,
                "platform_relaunch_event_ready": True,
                "limit_up_anchor_gap_pct": 1.2 + index * 0.2,
                "limit_up_nearby_doji_confirmation": index % 2 == 0,
                "limit_up_nearby_signal_score": 82 - index,
                "limit_up_nearby_pattern_label": "涨停板附近十字星" if index % 2 == 0 else "涨停后板附近横盘",
                "has_platform_contraction": True,
                "strict_confirmation_count": 4,
                "qualified_doji_confirmation": True,
                "overhead_gap_pct": 1.6,
            },
            "kline_confirmation": {"summary": "测试结构确认"},
            "risk_flags": [],
            "strict_blockers": [],
            "prediction_mode": "strict",
            "latest_as_of": "2026-04-23T14:55:00",
            "signal_status": "close_confirmed",
            "signal_status_label": "收盘确认",
            "signal_status_reason": "测试",
            "is_tradeable": True,
        }

    def build_second_board_candidate(index: int) -> dict:
        probability = round(0.72 - index * 0.03, 3)
        return {
                "code": f"600{500 + index:03d}",
            "name": f"二板{index}",
            "target_board": 2,
            "target_label": "冲二板",
            "probability": probability,
            "main_probability_name": "次日晋级二板概率",
            "sub_probabilities": {"next_second_board": probability},
            "confidence": 0.65,
            "confidence_level": "medium",
            "confidence_label": "中",
            "setup_grade": "首板晋级",
            "setup_grade_display": "首板晋级",
            "signal_summary": "首板→二板",
            "primary_reason": "测试二板候选",
            "secondary_reason": "测试补充说明",
            "current_price": 12.0 + index,
            "change_pct": 10.0,
            "turnover": 6.5,
            "volume_ratio": 1.2,
            "main_net_inflow": 25_000_000,
            "main_net_inflow_pct": 2.8,
            "support_strength_score": 100.0,
            "bull_score": 0.0,
            "bull_level": "",
            "display_score": 50.0,
            "event_types": ["limit_up"],
            "sector_name": "测试主线",
            "sector_strength_score": 78,
            "sector_limit_up_count": 4,
            "sector_consecutive_days": 3,
            "candidate_route": "second_board_promotion",
            "candidate_route_label": "首板晋级",
            "route_score": probability * 100,
            "probability_factors": {"distribution_penalty": 0.02},
            "seal_amount": 100_000_000 + index * 10_000_000,
            "break_count": 0,
            "limit_up_time": "094500",
            "latest_as_of": "2026-04-23",
            "signal_status": "close_confirmed",
            "signal_status_label": "收盘确认",
            "signal_status_reason": "测试",
            "is_tradeable": True,
        }

    async def fake_build_first_board_candidates(
        db,
        trade_date,
        limit_up_codes,
        market_context,
        limit,
        snapshot=None,
        news_end_time=None,
    ):
        assert limit >= 7
        return [
            build_first_board_candidate(index) for index in range(8)
        ], "2026-04-23T14:55:00", {
            "blocked_total": 2,
            "reason_summary": [{"reason": "主线强度不足", "label": "主线强度不足", "count": 2}],
            "blocked_reason_groups": [
                {
                    "reason": "主线强度不足",
                    "label": "主线强度不足",
                    "count": 2,
                    "examples": [{"code": "B001", "name": "被挡1", "blockers": ["主线强度不足"]}],
                }
            ],
            "notes": ["测试诊断"],
        }

    async def fake_build_second_board_candidates(db, trade_date, limit_ups, market_context, limit, **kwargs):
        assert limit >= 7
        return [build_second_board_candidate(index) for index in range(8)]

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_build_first_board_candidates", fake_build_first_board_candidates)
    monkeypatch.setattr(promotion, "_build_second_board_candidates", fake_build_second_board_candidates)

    response = await client.get("/api/v1/promotion/candidates?limit=5&ranked_limit=7")
    assert response.status_code == 200
    payload = response.json()

    assert len(payload["first_board_candidates"]) == 5
    assert len(payload["second_board_candidates"]) == 5
    assert len(payload["ranked_first_board_candidates"]) == 7
    assert len(payload["ranked_first_board_recall_candidates"]) == 7
    assert payload["formal_first_board_limit"] == 7
    assert payload["recall_first_board_limit"] == 7
    assert len(payload["ranked_second_board_candidates"]) == 7
    assert payload["limit_up_platform_count"] == 5
    assert payload["limit_up_platform_overview"]["pool_total"] == 8
    assert payload["limit_up_platform_candidates"][0]["code"] == "F000"
    assert payload["limit_up_platform_candidates"][0]["limit_up_nearby_pattern_label"] == "涨停板附近十字星"
    assert payload["limit_up_platform_diagnostics"]["blocked_total"] == 0
    assert payload["first_board_diagnostics"]["blocked_total"] == 2
    assert payload["prediction_model_version"] == promotion.PROMOTION_MODEL_VERSION
    assert payload["first_board_calibration_version"] == promotion.PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
    assert payload["ranked_first_board_candidates"][0]["code"] == "F000"
    assert payload["ranked_first_board_candidates"][0]["route_calibrated_probability"] > 0
    assert payload["ranked_first_board_candidates"][0]["temporal_event_probability"] > 0
    assert payload["ranked_first_board_candidates"][0]["probability_calibration_version"] == promotion.PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
    assert payload["ranked_first_board_candidates"][-1]["code"] == "F006"
    assert payload["ranked_second_board_candidates"][0]["code"] == "600500"
    assert payload["ranked_second_board_candidates"][-1]["code"] == "600506"
    assert len(payload["debug"]["mixed_ranked_candidates"]) == 7
    assert payload["debug"]["internal_candidate_limit"] >= 7
    assert "_cache_capacity" not in payload

    larger_response = await client.get("/api/v1/promotion/candidates?limit=8&ranked_limit=8")
    assert larger_response.status_code == 200
    larger_payload = larger_response.json()
    assert len(larger_payload["first_board_candidates"]) == 8
    assert len(larger_payload["second_board_candidates"]) == 8
    assert len(larger_payload["ranked_first_board_candidates"]) == 8
    assert len(larger_payload["ranked_first_board_recall_candidates"]) == 8
    assert len(larger_payload["ranked_second_board_candidates"]) == 8
    assert larger_payload["first_board_count"] == 8
    assert larger_payload["second_board_count"] == 8
    assert larger_payload["prediction_health"]["page_cache_source"] == "latest_completed_snapshot"


@pytest.mark.asyncio
async def test_promotion_candidates_anchor_first_board_to_current_anomaly_trade_date(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)
        session.add(
            SectorPersistence(
                sector_code="S1",
                sector_name="AI应用",
                trade_date=date(2026, 4, 24),
                consecutive_days=3,
                limit_up_count=5,
                fund_flow=7.4,
                change_pct=3.6,
                strength_score=88,
            )
        )
        await session.commit()

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "trade_date": "2026-04-24",
            "snapshot_time": "2026-04-24T10:15:00",
            "anomalies": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "capital",
                    "score": 89,
                    "description": "资金净额 1.6亿",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 9.96,
                        "change_pct": 5.8,
                        "main_net_inflow": 160_000_000,
                        "main_net_inflow_pct": 10.2,
                        "volume_ratio": 2.0,
                        "support_strength_score": 77,
                        "as_of": "2026-04-24T10:15:00",
                    },
                }
            ],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "total_score": 83,
                    "level": "A",
                }
            ]
        }

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)

    response = await client.get("/api/v1/promotion/candidates?limit=6&ranked_limit=10")
    assert response.status_code == 200
    payload = response.json()

    assert payload["trade_date"] == "2026-04-24"
    assert payload["first_board_trade_date"] == "2026-04-24"
    assert payload["second_board_trade_date"] == "2026-04-23"
    assert payload["snapshot_time"] == "2026-04-24T10:15:00"
    assert payload["market_context"]["first_board"]["limit_up_count"] == 0
    assert payload["market_context"]["second_board"]["limit_up_count"] == 3
    assert payload["first_board_candidates"][0]["latest_as_of"] == "2026-04-24T10:15:00"


@pytest.mark.asyncio
async def test_promotion_candidates_exclude_generic_relationship_sector_from_mainline(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "trade_date": "2026-04-23",
            "snapshot_time": "2026-04-23T14:20:00",
            "anomalies": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "capital",
                    "score": 86,
                    "description": "资金净额 1.4亿",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 9.88,
                        "change_pct": 5.3,
                        "main_net_inflow": 140_000_000,
                        "main_net_inflow_pct": 8.4,
                        "volume_ratio": 1.9,
                        "support_strength_score": 73,
                        "as_of": "2026-04-23T14:20:00",
                    },
                }
            ],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "total_score": 80,
                    "level": "A",
                }
            ]
        }

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)

    response = await client.get("/api/v1/promotion/candidates?limit=6&ranked_limit=10")
    assert response.status_code == 200
    payload = response.json()

    candidate = payload["first_board_candidates"][0]
    assert candidate["code"] == "000010"
    assert candidate["sector_name"] == "AI应用"
    assert candidate["sector_strength_score"] == 82.0


@pytest.mark.asyncio
async def test_promotion_candidates_include_quiet_setup_candidates_without_anomaly_trigger(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)
        session.add(
            StockSpot(
                code="000010",
                name="静默股",
                price=10.02,
                prev_close=9.98,
                open=9.99,
                high=10.06,
                low=9.94,
                change_pct=0.4,
                amplitude=1.2,
                volume=52000,
                amount=68_000_000,
                turnover=2.8,
                volume_ratio=1.02,
                main_net_inflow=2_500_000,
                support_strength_score=61,
            )
        )
        await session.commit()

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "trade_date": "2026-04-23",
            "snapshot_time": "2026-04-23T15:00:00",
            "anomalies": [],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "静默股",
                    "total_score": 84,
                    "level": "A",
                    "top_signals": ["平台压缩", "缩量蓄势"],
                }
            ]
        }

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)

    response = await client.get("/api/v1/promotion/candidates?limit=8&ranked_limit=12")
    assert response.status_code == 200
    payload = response.json()

    assert len(payload["first_board_watch_candidates"]) >= 1
    candidate = payload["first_board_watch_candidates"][0]
    assert candidate["code"] == "000010"
    assert candidate["prediction_mode"] == "strict_stealth_scan"
    assert candidate["signal_summary"] == "静默蓄势 / 多日形态确认"
    assert candidate["signal_status_label"] == "收盘确认"
    assert candidate["time_horizon_label"] == "首板梯队"


@pytest.mark.asyncio
async def test_promotion_probability_target_1_returns_platform_relaunch_memory_features(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)
        session.add_all(
            [
                LimitUpPool(
                    code="000010",
                    name="冲板股",
                    trade_date=date(2026, 4, 14),
                    consecutive_days=1,
                    seal_amount=180_000_000,
                    break_count=0,
                    turnover=4.2,
                    source="test",
                ),
                LimitUpPool(
                    code="000010",
                    name="冲板股",
                    trade_date=date(2026, 4, 15),
                    consecutive_days=2,
                    seal_amount=260_000_000,
                    break_count=0,
                    turnover=5.1,
                    source="test",
                ),
            ]
        )
        await session.commit()

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "trade_date": "2026-04-23",
            "snapshot_time": "2026-04-23T14:40:00",
            "anomalies": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "capital",
                    "score": 87,
                    "description": "资金净额 1.3亿",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 10.02,
                        "open": 9.99,
                        "high": 10.06,
                        "low": 9.94,
                        "change_pct": 4.8,
                        "main_net_inflow": 130_000_000,
                        "main_net_inflow_pct": 8.6,
                        "volume_ratio": 1.9,
                        "support_strength_score": 75,
                        "turnover": 4.4,
                        "as_of": "2026-04-23T14:40:00",
                    },
                },
                {
                    "code": "000010",
                    "name": "冲板股",
                    "event_type": "breakthrough",
                    "score": 84,
                    "description": "平台后突破",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 10.02,
                        "open": 9.99,
                        "high": 10.06,
                        "low": 9.94,
                        "change_pct": 4.8,
                        "volume_ratio": 1.9,
                        "support_strength_score": 75,
                        "turnover": 4.4,
                        "as_of": "2026-04-23T14:40:30",
                    },
                },
            ],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "冲板股",
                    "total_score": 83,
                    "level": "A",
                }
            ]
        }

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)

    response = await client.get("/api/v1/promotion/000010/probability?target=1")
    assert response.status_code == 200
    payload = response.json()

    assert payload["trade_date"] == "2026-04-23"
    assert payload["main_probability_name"] == "次日首板概率（低基准率）"
    assert payload["candidate_route"] == "platform_relaunch"
    assert payload["candidate_route_label"] == "平台二次点火"
    assert payload["probability"] > 0
    assert payload["sub_probabilities"]["first_limitup_next_day"] == payload["probability"]
    assert payload["sub_probabilities"]["first_limitup_5d"] >= payload["probability"]
    assert payload["memory_features"]["memory_max_board_50d"] == 2
    assert payload["memory_features"]["memory_limit_up_hits_50d"] == 2
    assert payload["memory_features"]["memory_days_since_last_limit_up"] >= 5


@pytest.mark.asyncio
async def test_promotion_probability_target_1_supports_fresh_mainline_start_without_memory(
    promotion_api_env,
    monkeypatch,
):
    SessionLocal, client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        await _seed_candidate_context(session)

    async def fake_prewarm_anomaly_snapshot(db, trade_date=None, force_refresh=False):
        return {
            "trade_date": "2026-04-23",
            "snapshot_time": "2026-04-23T14:35:00",
            "anomalies": [
                {
                    "code": "000010",
                    "name": "新主线热启动",
                    "event_type": "capital",
                    "score": 90,
                    "description": "主力资金点火 1.6 亿",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 10.06,
                        "open": 9.98,
                        "high": 10.12,
                        "low": 9.94,
                        "change_pct": 4.9,
                        "main_net_inflow": 160_000_000,
                        "main_net_inflow_pct": 9.4,
                        "volume_ratio": 1.96,
                        "support_strength_score": 66,
                        "turnover": 5.2,
                        "as_of": "2026-04-23T14:35:00",
                    },
                },
                {
                    "code": "000010",
                    "name": "新主线热启动",
                    "event_type": "breakthrough",
                    "score": 86,
                    "description": "放量突破平台",
                    "setup_grade": "A2 盘口确认后执行",
                    "setup_grade_display": "A2 盘口确认后执行",
                    "setup_track": "",
                    "feishu_pushable": False,
                    "risk_flags": [],
                    "a1_blockers": [],
                    "detail": {
                        "price": 10.06,
                        "open": 9.98,
                        "high": 10.12,
                        "low": 9.94,
                        "change_pct": 4.9,
                        "main_net_inflow_pct": 9.4,
                        "volume_ratio": 1.96,
                        "support_strength_score": 66,
                        "turnover": 5.2,
                        "as_of": "2026-04-23T14:35:20",
                    },
                },
            ],
        }

    async def fake_bull_rank(db):
        return {
            "rank": [
                {
                    "code": "000010",
                    "name": "新主线热启动",
                    "total_score": 61,
                    "level": "B",
                }
            ]
        }

    monkeypatch.setattr(promotion, "prewarm_anomaly_snapshot", fake_prewarm_anomaly_snapshot)
    monkeypatch.setattr(promotion, "_bull_rank", fake_bull_rank)

    response = await client.get("/api/v1/promotion/000010/probability?target=1")
    assert response.status_code == 200
    payload = response.json()

    assert payload["candidate_route"] == "fresh_mainline_start"
    assert payload["candidate_route_label"] == "主线首波点火"
    assert payload["probability"] > 0
    assert payload["memory_features"]["memory_limit_up_hits_50d"] == 0
    assert payload["watch_bucket_label"] == "主线首波点火"


@pytest.mark.asyncio
async def test_second_board_learning_requires_real_first_to_second_board_continuity(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 20)
    outcome_date = date(2026, 4, 21)
    async with SessionLocal() as session:
        session.add_all(
            [
                PromotionPredictionRecord(
                    code="600870",
                    name="普通次日首板",
                    target_board=2,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.30,
                    calibrated_probability=0.20,
                    candidate_route="second_board_promotion",
                    learning_bucket="T2:second_board_promotion",
                    outcome_status="pending",
                ),
                PromotionPredictionRecord(
                    code="600871",
                    name="连续涨停但源字段陈旧",
                    target_board=2,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.30,
                    calibrated_probability=0.20,
                    candidate_route="second_board_promotion",
                    learning_bucket="T2:second_board_promotion",
                    outcome_status="pending",
                ),
                PromotionPredictionRecord(
                    code="600872",
                    name="异常跳板高度",
                    target_board=2,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.30,
                    calibrated_probability=0.20,
                    candidate_route="second_board_promotion",
                    learning_bucket="T2:second_board_promotion",
                    outcome_status="pending",
                ),
                LimitUpPool(
                    code="600871",
                    name="连续涨停但源字段陈旧",
                    trade_date=prediction_date,
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600872",
                    name="异常跳板高度",
                    trade_date=prediction_date,
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600870",
                    name="普通次日首板",
                    trade_date=outcome_date,
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600871",
                    name="连续涨停但源字段陈旧",
                    trade_date=outcome_date,
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600872",
                    name="异常跳板高度",
                    trade_date=outcome_date,
                    consecutive_days=3,
                    source="test",
                ),
                StockKline(
                    code="600870",
                    trade_date=outcome_date,
                    open=10.0,
                    close=11.0,
                    high=11.0,
                    low=10.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=100000,
                ),
                StockKline(
                    code="600871",
                    trade_date=outcome_date,
                    open=10.0,
                    close=11.0,
                    high=11.0,
                    low=10.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=100000,
                ),
                StockKline(
                    code="600872",
                    trade_date=outcome_date,
                    open=10.0,
                    close=11.0,
                    high=11.0,
                    low=10.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=100000,
                ),
            ]
        )
        await session.commit()

        changed = await promotion._refresh_promotion_learning(session)
        await session.commit()
        records = list(
            (
                await session.execute(
                    select(PromotionPredictionRecord).where(
                        PromotionPredictionRecord.code.in_(["600870", "600871", "600872"])
                    )
                )
            ).scalars().all()
        )

    by_code = {record.code: record for record in records}
    assert changed == 3
    assert by_code["600870"].outcome_status == "failed"
    assert by_code["600870"].actual_limit_up_date is None
    assert by_code["600871"].outcome_status == "success"
    assert by_code["600871"].actual_limit_up_date == outcome_date
    assert by_code["600872"].outcome_status == "failed"
    assert by_code["600872"].actual_limit_up_date is None


@pytest.mark.asyncio
async def test_promotion_learning_marks_failed_first_board_and_records_reason(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add(
            PromotionPredictionRecord(
                code="000020",
                name="复盘样本",
                target_board=1,
                prediction_trade_date=date(2026, 4, 20),
                horizon_days=3,
                predicted_probability=0.62,
                calibrated_probability=0.62,
                candidate_route="fresh_mainline_start",
                learning_bucket="T1:fresh_mainline_start",
                outcome_status="pending",
                reason_snapshot='{"candidate_route_label":"主线首波点火"}',
                factors_json='{"kline_confirmation_score":0.42,"strict_ready":false}',
            )
        )
        session.add_all(
            [
                StockKline(
                    code="000020",
                    trade_date=date(2026, 4, 21),
                    open=10.0,
                    close=10.2,
                    high=10.35,
                    low=9.95,
                    change_pct=2.0,
                    prev_close=10.0,
                    volume=100000,
                ),
                StockKline(
                    code="000020",
                    trade_date=date(2026, 4, 22),
                    open=10.2,
                    close=10.1,
                    high=10.4,
                    low=10.0,
                    change_pct=-1.0,
                    prev_close=10.2,
                    volume=110000,
                ),
                StockKline(
                    code="000020",
                    trade_date=date(2026, 4, 23),
                    open=10.1,
                    close=10.25,
                    high=10.45,
                    low=10.05,
                    change_pct=1.5,
                    prev_close=10.1,
                    volume=120000,
                ),
            ]
        )
        await session.commit()

        changed = await promotion._refresh_promotion_learning(session)
        await session.commit()

        record = (
            await session.execute(
                promotion.select(PromotionPredictionRecord).where(PromotionPredictionRecord.code == "000020")
            )
        ).scalar_one()

    assert changed == 1
    assert record.outcome_status == "failed"
    assert "未首板" in record.failure_reason
    assert "K线确认不够" in record.failure_reason
    assert record.actual_max_change_pct == pytest.approx(3.5)


@pytest.mark.asyncio
async def test_promotion_learning_finalizes_ranked_failure_when_stock_kline_is_missing(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add(
            PromotionPredictionRecord(
                code="000021",
                name="缺K线样本",
                target_board=1,
                prediction_trade_date=date(2026, 4, 20),
                horizon_days=1,
                predicted_probability=0.32,
                calibrated_probability=0.18,
                candidate_route="mainline_spread_start",
                learning_bucket="T1:mainline_spread_start",
                outcome_status="pending",
                factors_json=promotion._json_dumps_safe(
                    {
                        "prediction_snapshot_source": "schedule",
                        "prediction_snapshot_context": "promotion_2000",
                        "prediction_snapshot_recorded_at": "2026-04-20T20:00:00",
                        "prediction_ranked_selected": True,
                        "learning_eligible": True,
                    }
                ),
            )
        )
        session.add(
            StockKline(
                code="000099",
                trade_date=date(2026, 4, 21),
                open=8.0,
                close=8.1,
                high=8.2,
                low=7.9,
                change_pct=1.25,
                prev_close=8.0,
                volume=100000,
            )
        )
        await session.commit()

        changed = await promotion._refresh_promotion_learning(session)
        await session.commit()
        record = await session.scalar(
            select(PromotionPredictionRecord).where(PromotionPredictionRecord.code == "000021")
        )

    assert changed == 1
    assert record.outcome_status == "failed"
    assert record.outcome_trade_date == date(2026, 4, 21)


@pytest.mark.asyncio
async def test_promotion_learning_bulk_finalizes_unranked_pool_failures(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 20)
    outcome_date = date(2026, 4, 21)
    base_factors = {
        "prediction_snapshot_source": "schedule",
        "prediction_snapshot_context": "promotion_2000",
        "prediction_snapshot_recorded_at": "2026-04-20T20:00:00",
        "prediction_snapshot_batch_key": "schedule:promotion_2000:2026-04-20T20:00:00",
    }

    async with SessionLocal() as session:
        session.add_all(
            [
                PromotionPredictionRecord(
                    code="000031",
                    name="主榜失败",
                    target_board=1,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.12,
                    calibrated_probability=0.08,
                    candidate_route="mainline_spread_start",
                    learning_bucket="T1:mainline_spread_start",
                    outcome_status="pending",
                    factors_json=promotion._json_dumps_safe(
                        {**base_factors, "prediction_ranked_selected": True, "learning_eligible": True}
                    ),
                ),
                PromotionPredictionRecord(
                    code="000032",
                    name="池内失败",
                    target_board=1,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.06,
                    calibrated_probability=0.03,
                    candidate_route="mainline_spread_start",
                    learning_bucket="T1:mainline_spread_start",
                    outcome_status="pending",
                    factors_json=promotion._json_dumps_safe(
                        {**base_factors, "prediction_ranked_selected": False, "learning_eligible": False}
                    ),
                ),
                StockKline(
                    code="000031",
                    trade_date=outcome_date,
                    open=10,
                    close=10.2,
                    high=10.4,
                    low=9.9,
                    prev_close=10,
                    change_pct=2.0,
                    volume=100000,
                ),
                StockKline(
                    code="000032",
                    trade_date=outcome_date,
                    open=10,
                    close=9.9,
                    high=10.1,
                    low=9.7,
                    prev_close=10,
                    change_pct=-1.0,
                    volume=100000,
                ),
            ]
        )
        await session.commit()

        changed = await promotion._refresh_promotion_learning(session)
        await session.commit()
        records = list(
            (
                await session.execute(
                    select(PromotionPredictionRecord).where(
                        PromotionPredictionRecord.code.in_(["000031", "000032"])
                    )
                )
            ).scalars().all()
        )

    assert changed == 2
    assert {record.outcome_status for record in records} == {"failed"}
    assert {record.outcome_trade_date for record in records} == {outcome_date}


@pytest.mark.asyncio
async def test_promotion_learning_treats_consecutive_vendor_firsts_as_t2_not_t1(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 20)
    outcome_date = date(2026, 4, 21)

    async with SessionLocal() as session:
        t1 = PromotionPredictionRecord(
            code="000033",
            name="连续板样本",
            target_board=1,
            prediction_trade_date=prediction_date,
            horizon_days=1,
            predicted_probability=0.20,
            calibrated_probability=0.12,
            candidate_route="mainline_spread_start",
            learning_bucket="T1:mainline_spread_start",
            outcome_status="pending",
        )
        t2 = PromotionPredictionRecord(
            code="000033",
            name="连续板样本",
            target_board=2,
            prediction_trade_date=prediction_date,
            horizon_days=1,
            predicted_probability=0.30,
            calibrated_probability=0.18,
            candidate_route="second_board_promotion",
            learning_bucket="T2:second_board_promotion",
            outcome_status="pending",
        )
        session.add_all(
            [
                t1,
                t2,
                LimitUpPool(
                    code="000033",
                    name="连续板样本",
                    trade_date=prediction_date,
                    consecutive_days=1,
                    source="vendor_without_board_height",
                ),
                LimitUpPool(
                    code="000033",
                    name="连续板样本",
                    trade_date=outcome_date,
                    consecutive_days=1,
                    source="vendor_without_board_height",
                ),
                StockKline(
                    code="000033",
                    trade_date=outcome_date,
                    open=10.0,
                    close=11.0,
                    high=11.0,
                    low=10.0,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=100000,
                ),
            ]
        )
        await session.commit()

        changed = await promotion._evaluate_promotion_prediction_records_bulk(
            session,
            [t1, t2],
            now=datetime(2026, 4, 21, 16, 0),
        )

    assert changed == 2
    assert t1.outcome_status == "failed"
    assert t1.actual_limit_up_date is None
    assert t2.outcome_status == "success"
    assert t2.actual_limit_up_date == outcome_date


@pytest.mark.asyncio
async def test_promotion_learning_ignores_holiday_limit_pool_as_outcome_clock(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 6, 18)
    official_holiday = date(2026, 6, 19)
    next_market_date = date(2026, 6, 22)

    async with SessionLocal() as session:
        record = PromotionPredictionRecord(
            code="600099",
            name="节假日脏数据样本",
            target_board=1,
            prediction_trade_date=prediction_date,
            horizon_days=1,
            predicted_probability=0.25,
            calibrated_probability=0.15,
            candidate_route="mainline_spread_start",
            learning_bucket="T1:mainline_spread_start",
            outcome_status="pending",
        )
        session.add_all(
            [
                record,
                LimitUpPool(
                    code="600099",
                    name="节假日脏数据样本",
                    trade_date=official_holiday,
                    consecutive_days=1,
                    source="stale_test",
                ),
                StockKline(
                    code="600099",
                    trade_date=next_market_date,
                    open=10.0,
                    close=10.2,
                    high=10.4,
                    low=9.9,
                    prev_close=10.0,
                    change_pct=2.0,
                    volume=100000,
                ),
            ]
        )
        await session.commit()

        changed = await promotion._evaluate_promotion_prediction_record(
            session,
            record,
            now=datetime(2026, 6, 22, 16, 0),
        )

    assert changed is True
    assert record.outcome_status == "failed"
    assert record.outcome_trade_date == next_market_date
    assert record.actual_limit_up_date is None


@pytest.mark.asyncio
async def test_promotion_learning_does_not_finalize_from_intraday_limit_pool(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 8, 14)
    intraday_date = date(2026, 8, 17)

    async with SessionLocal() as session:
        record = PromotionPredictionRecord(
            code="600100",
            name="盘中样本",
            target_board=1,
            prediction_trade_date=prediction_date,
            horizon_days=1,
            predicted_probability=0.20,
            calibrated_probability=0.12,
            candidate_route="mainline_spread_start",
            learning_bucket="T1:mainline_spread_start",
            outcome_status="pending",
        )
        session.add_all(
            [
                record,
                LimitUpPool(
                    code="600100",
                    name="盘中样本",
                    trade_date=intraday_date,
                    consecutive_days=1,
                    source="test",
                ),
            ]
        )
        await session.commit()

        changed = await promotion._evaluate_promotion_prediction_record(
            session,
            record,
            now=datetime(2026, 8, 17, 10, 30),
        )

    assert changed is False
    assert record.outcome_status == "pending"
    assert record.outcome_trade_date is None


@pytest.mark.asyncio
async def test_promotion_learning_resets_premature_same_day_outcome(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        record = PromotionPredictionRecord(
            code="600101",
            name="误结算样本",
            target_board=2,
            prediction_trade_date=date(2026, 8, 14),
            horizon_days=1,
            predicted_probability=0.30,
            calibrated_probability=0.16,
            candidate_route="second_board_promotion",
            learning_bucket="T2:second_board_promotion",
            outcome_status="failed",
            outcome_trade_date=date(2026, 8, 17),
            actual_max_change_pct=-2.0,
            actual_close_change_pct=-1.0,
            failure_reason="盘中旧逻辑误结算",
            failure_tags_json='["旧逻辑"]',
        )
        session.add(record)
        await session.commit()

        reset_count = await promotion._reset_premature_promotion_outcomes(
            session,
            now=datetime(2026, 8, 17, 10, 30),
        )
        await session.commit()

    assert reset_count == 1
    assert record.outcome_status == "pending"
    assert record.outcome_trade_date is None
    assert record.actual_max_change_pct is None
    assert record.failure_reason == ""


@pytest.mark.parametrize("status,precision,recall", [
    ("partial", None, None),
    ("unavailable", None, None),
    ("partial", .01, .01),
    ("complete", None, .01),
    ("complete", .01, None),
])
def test_review_recommendation_does_not_tune_incomplete_window(status, precision, recall):
    recommendation = promotion._promotion_review_recommendation({
        "evaluation_status": status,
        "valid_review_days": 10,
        "review_days": 11,
        "limit_up_precision": precision,
        "limit_up_recall": recall,
        "not_in_pool_count": 359,
        "rank_cutoff_count": 50,
        "calibration_bias": .9,
    })
    assert recommendation["status"] == "data_incomplete"
    assert "未知不是失败" in recommendation["headline"]
    assert all("上修" not in action and "收缩" not in action for action in recommendation["actions"])


def test_promotion_review_recommendation_exposes_auditable_window_and_count():
    recommendation = promotion._promotion_review_recommendation(
        {
            "valid_review_days": 10,
            "limit_up_recall": 0.04,
            "limit_up_precision": 0.12,
            "not_in_pool_count": 359,
            "rank_cutoff_count": 32,
            "score_low_count": 144,
        }
    )

    assert recommendation["status"] == "expand_feature_coverage"
    assert "近10个有效复盘日" in recommendation["headline"]
    assert "359 只实际首/二板" in recommendation["headline"]


@pytest.mark.asyncio
async def test_promotion_learning_review_excludes_current_intraday_date(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all([TradeCalendarModel(trade_date=date(2026, 8, day), is_trade_day=day not in (15, 16)) for day in range(13, 18)])
        session.add_all(
            [
                LimitUpPool(
                    code="600110",
                    name="前前交易日",
                    trade_date=date(2026, 8, 13),
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600111",
                    name="已完成交易日",
                    trade_date=date(2026, 8, 14),
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600112",
                    name="当日盘中触板",
                    trade_date=date(2026, 8, 17),
                    consecutive_days=1,
                    source="test",
                ),
            ]
        )
        await session.commit()

        payload = await promotion._build_promotion_daily_learning_review(
            session,
            lookback_days=10,
            now=datetime(2026, 8, 17, 10, 30),
        )

    assert payload["latest_completed_trade_date"] == "2026-08-14"
    assert payload["latest"]["actual_trade_date"] == "2026-08-14"
    assert all(item["actual_trade_date"] != "2026-08-17" for item in payload["daily"])


@pytest.mark.asyncio
async def test_promotion_daily_learning_review_aligns_ranked_predictions_with_actuals(promotion_api_env):
    SessionLocal, client = promotion_api_env
    prediction_date = date(2026, 4, 22)
    actual_date = date(2026, 4, 23)
    schedule_factors = {
        "prediction_snapshot_source": "schedule",
        "prediction_snapshot_context": "promotion_2000",
        "prediction_snapshot_recorded_at": "2026-04-22T20:00:00",
        "prediction_ranked_selected": False,
        "prediction_ranked_position": 0,
        "prediction_ranked_limit": 10,
        "learning_eligible": False,
        "route_score": 50,
    }

    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="000701",
                    name="前日首板",
                    trade_date=prediction_date,
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="000700",
                    name="次日首板命中",
                    trade_date=actual_date,
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="000701",
                    name="次日二板命中",
                    trade_date=actual_date,
                    consecutive_days=2,
                    source="test",
                ),
                StockKline(
                    code="000700",
                    trade_date=actual_date,
                    open=10.0,
                    close=11.0,
                    high=11.0,
                    low=9.9,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=100000,
                ),
                StockKline(
                    code="000701",
                    trade_date=actual_date,
                    open=10.0,
                    close=11.0,
                    high=11.0,
                    low=9.9,
                    prev_close=10.0,
                    change_pct=10.0,
                    volume=100000,
                ),
                StockKline(
                    code="000799",
                    trade_date=actual_date,
                    open=10.0,
                    close=10.2,
                    high=10.3,
                    low=9.9,
                    prev_close=10.0,
                    change_pct=2.0,
                    volume=100000,
                ),
            ]
        )
        first_board_records = []
        for index in range(promotion.PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS):
            code = "000700" if index == 0 else "000799" if index == 1 else f"6007{index:02d}"
            factors = {
                **schedule_factors,
                "prediction_ranked_selected": index < 2,
                "prediction_ranked_position": index + 1 if index < 2 else 0,
                "prediction_recall_ranked_selected": index < 3,
                "prediction_recall_ranked_position": index + 1 if index < 3 else 0,
                "learning_eligible": index < 2,
            }
            first_board_records.append(
                PromotionPredictionRecord(
                    code=code,
                    name=f"首板样本{index}",
                    target_board=1,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.20,
                    calibrated_probability=0.15,
                    candidate_route="mainline_spread_start",
                    learning_bucket="T1:mainline_spread_start",
                    outcome_status="pending",
                    model_version="promotion_test_v26",
                    factors_json=promotion._json_dumps_safe(factors),
                )
            )
        second_board_records = []
        for index in range(promotion.PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS):
            factors = {
                **schedule_factors,
                "prediction_ranked_selected": index == 0,
                "prediction_ranked_position": 1 if index == 0 else 0,
                # 模拟旧版二板快照：没有宽召回字段，接口必须和“已记录但 0 命中”区分。
                "learning_eligible": index == 0,
            }
            second_board_records.append(
                PromotionPredictionRecord(
                    code="000701" if index == 0 else f"6008{index:02d}",
                    name=f"二板样本{index}",
                    target_board=2,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.30,
                    calibrated_probability=0.25,
                    candidate_route="second_board_promotion",
                    learning_bucket="T2:second_board_promotion",
                    outcome_status="pending",
                    model_version="promotion_test_v26",
                    factors_json=promotion._json_dumps_safe(factors),
                )
            )
        session.add_all(first_board_records + second_board_records)
        session.add_all([TradeCalendarModel(trade_date=day, is_trade_day=True) for day in (prediction_date, actual_date)])
        for code in ("000700", "000701", "000799"):
            session.add(StockKline(code=code, trade_date=prediction_date, close=10, volume=100000, source="ths"))
        for bar in (await session.execute(select(StockKline).where(StockKline.trade_date == actual_date))).scalars():
            bar.source = "ths"
        await session.commit()

    response = await client.get("/api/v1/promotion/learning-review?lookback_days=10")
    assert response.status_code == 200
    payload = response.json()
    latest = payload["latest"]

    assert latest["prediction_trade_date"] == "2026-04-22"
    assert latest["actual_trade_date"] == "2026-04-23"
    assert latest["evaluation_scope"] == "previous_close_schedule_snapshot"
    assert latest["has_unobservable_news_window"] is False
    assert latest["snapshot_complete"] is True
    assert latest["prediction_model_version"] == "promotion_test_v26"
    assert latest["prediction_model_versions"] == ["promotion_test_v26"]
    assert latest["prediction_snapshot_contexts"] == ["promotion_2000"]
    assert latest["actual_target_limit_up_count"] == 2
    assert latest["predicted_count"] == 3
    assert latest["predicted_limit_up_hit_count"] == 2
    assert latest["predicted_rising_hit_count"] == 3
    assert latest["limit_up_precision"] == pytest.approx(2 / 3, abs=0.0001)
    assert latest["limit_up_recall"] == 1.0
    assert latest["lane_metrics"]["target_1"]["hit_count"] == 1
    assert latest["lane_metrics"]["target_1"]["pool_hit_count"] == 1
    assert latest["lane_metrics"]["target_1"]["pool_recall"] == 1.0
    assert latest["lane_metrics"]["target_1"]["recall_ranked_available"] is True
    assert latest["lane_metrics"]["target_1"]["recall_ranked_count"] == 3
    assert latest["lane_metrics"]["target_1"]["recall_hit_count"] == 1
    assert latest["lane_metrics"]["target_1"]["recall_recall"] == 1.0
    assert latest["lane_metrics"]["target_2"]["hit_count"] == 1
    assert latest["lane_metrics"]["target_2"]["pool_hit_count"] == 1
    assert latest["lane_metrics"]["target_2"]["recall_ranked_available"] is False
    launch_metrics = latest["launch_precursor_metrics"]
    assert launch_metrics["all_ranked_first_board"]["sample_count"] == 2
    assert launch_metrics["all_ranked_first_board"]["limit_up_precision"] == 0.5
    assert launch_metrics["all_ranked_first_board"]["directional_precision"] == 1.0
    assert payload["aggregate"]["launch_precursor_metrics"]["all_ranked_first_board"]["sample_count"] == 2


@pytest.mark.asyncio
async def test_promotion_learning_calibrates_probability_by_route_history(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                PromotionPredictionRecord(
                    code=f"0001{i:02d}",
                    name="历史失败",
                    target_board=2,
                    prediction_trade_date=date(2026, 4, 1 + i),
                    horizon_days=1,
                    predicted_probability=0.70,
                    calibrated_probability=0.70,
                    candidate_route="second_board_promotion",
                    learning_bucket="T2:second_board_promotion",
                    outcome_status="failed",
                    model_version=promotion.PROMOTION_MODEL_VERSION,
                    factors_json=promotion._json_dumps_safe({
                        "promotion_event_label_version": promotion.PROMOTION_LABEL_VERSION,
                    }),
                )
                for i in range(8)
            ]
        )
        await session.commit()

        stats = await promotion._load_promotion_learning_stats(session)

    adjusted = promotion._apply_promotion_learning_to_candidates(
        [
            {
                "code": "000200",
                "target_board": 2,
                "candidate_route": "second_board_promotion",
                "probability": 0.70,
                "sub_probabilities": {"next_second_board": 0.70},
                "probability_factors": {},
            }
        ],
        stats,
    )[0]

    assert stats["T2:second_board_promotion"]["sample_count"] == 8
    assert stats["T2:second_board_promotion"]["success_rate"] == 0
    assert adjusted["probability"] < 0.70
    assert adjusted["sub_probabilities"]["next_second_board"] == adjusted["probability"]
    assert adjusted["probability_factors"]["learning_sample_count"] == 8


@pytest.mark.asyncio
async def test_promotion_learning_uses_latest_schedule_batch_only(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 23)
    old_factors = {
        "prediction_snapshot_source": "schedule",
        "prediction_snapshot_context": "promotion_1510",
        "prediction_snapshot_recorded_at": "2026-04-22T15:10:00",
        "prediction_model_version": promotion.PROMOTION_MODEL_VERSION,
        "promotion_event_label_version": promotion.PROMOTION_LABEL_VERSION,
    }
    latest_factors = {
        "prediction_snapshot_source": "schedule",
        "prediction_snapshot_context": "promotion_2000",
        "prediction_snapshot_recorded_at": "2026-04-22T20:00:00",
        "prediction_model_version": promotion.PROMOTION_MODEL_VERSION,
        "promotion_event_label_version": promotion.PROMOTION_LABEL_VERSION,
    }

    async with SessionLocal() as session:
        session.add_all(
            [
                PromotionPredictionRecord(
                    code=f"0003{i:02d}",
                    name="旧批次失败",
                    target_board=1,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.20,
                    calibrated_probability=0.20,
                    candidate_route="pre_board_probe_start",
                    learning_bucket="T1:pre_board_probe_start",
                    outcome_status="failed",
                    factors_json=promotion._json_dumps_safe(old_factors),
                )
                for i in range(4)
            ]
            + [
                PromotionPredictionRecord(
                    code=f"0004{i:02d}",
                    name="新批次成功",
                    target_board=1,
                    prediction_trade_date=prediction_date,
                    horizon_days=1,
                    predicted_probability=0.20,
                    calibrated_probability=0.20,
                    candidate_route="pre_board_probe_start",
                    learning_bucket="T1:pre_board_probe_start",
                    outcome_status="success",
                    factors_json=promotion._json_dumps_safe(latest_factors),
                )
                for i in range(4)
            ]
        )
        await session.commit()
        stats = await promotion._load_promotion_learning_stats(session)

    bucket = stats["T1:pre_board_probe_start"]
    assert bucket["sample_count"] == 4
    assert bucket["success_count"] == 4
    assert bucket["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_promotion_learning_prefers_current_model_over_large_legacy_history(promotion_api_env):
    SessionLocal, _client = promotion_api_env
    route = "pre_board_probe_start"

    async with SessionLocal() as session:
        legacy_records = [
            PromotionPredictionRecord(
                code=f"601{i:03d}",
                name="旧版失败",
                target_board=1,
                prediction_trade_date=date(2026, 5, 1) + timedelta(days=i),
                predicted_probability=0.20,
                calibrated_probability=0.20,
                candidate_route=route,
                learning_bucket=f"T1:{route}",
                outcome_status="failed",
                model_version="promotion_legacy",
                factors_json=promotion._json_dumps_safe({
                    "prediction_snapshot_source": "schedule",
                    "prediction_snapshot_context": "promotion_2000",
                    "prediction_snapshot_recorded_at": f"2026-05-{i + 1:02d}T20:00:00",
                    "prediction_model_version": "promotion_legacy",
                    "prediction_ranked_selected": True,
                    "learning_eligible": True,
                }),
            )
            for i in range(20)
        ]
        current_records = [
            PromotionPredictionRecord(
                code=f"6009{i:02d}",
                name="新版成功",
                target_board=1,
                prediction_trade_date=date(2026, 8, 1) + timedelta(days=i),
                predicted_probability=0.08,
                calibrated_probability=0.08,
                candidate_route=route,
                learning_bucket=f"T1:{route}",
                outcome_status="success",
                model_version=promotion.PROMOTION_MODEL_VERSION,
                factors_json=promotion._json_dumps_safe({
                    "prediction_snapshot_source": "schedule",
                    "prediction_snapshot_context": "promotion_2000",
                    "prediction_snapshot_recorded_at": f"2026-08-{i + 1:02d}T20:00:00",
                    "prediction_model_version": promotion.PROMOTION_MODEL_VERSION,
                    "prediction_ranked_selected": True,
                    "learning_eligible": True,
                }),
            )
            for i in range(8)
        ]
        session.add_all(legacy_records + current_records)
        await session.commit()
        stats = await promotion._load_promotion_learning_stats(session)

    bucket = stats[f"T1:{route}"]
    assert bucket["learning_version_mode"] == "current_model_only"
    assert bucket["sample_count"] == 8
    assert bucket["current_model_sample_count"] == 8
    assert bucket["raw_sample_count"] == 8
    assert bucket["legacy_sample_count_used"] == 0
    assert bucket["success_count"] == 8


def test_first_board_new_route_learning_uses_beta_binomial_probability():
    adjusted = promotion._apply_promotion_learning_to_candidates(
        [
            {
                "code": "000500",
                "target_board": 1,
                "candidate_route": "pre_board_probe_start",
                "probability": 0.20,
                "sub_probabilities": {"first_limitup_next_day": 0.20},
                "probability_factors": {},
            }
        ],
        {
            "T1:pre_board_probe_start": {
                "sample_count": 20,
                "success_rate": 0,
                "model_adjustment": -0.09,
            }
        },
    )[0]

    assert 0.025 <= adjusted["probability"] < 0.20
    assert adjusted["sub_probabilities"]["first_limitup_next_day"] == adjusted["probability"]
    assert adjusted["sub_probabilities"]["first_limitup_5d"] < 0.5
    assert adjusted["probability_factors"]["learning_probability_adjustment"] < 0
    assert adjusted["probability_factors"]["learning_raw_probability_adjustment"] == pytest.approx(-0.09)
    assert adjusted["probability_factors"]["learning_adjustment_capped"] is False
    assert adjusted["probability_factors"]["learning_calibration_method"] == promotion.PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION


def test_first_board_learning_calibrates_direction_separately_from_limit_up():
    adjusted = promotion._apply_promotion_learning_to_candidates(
        [
            {
                "code": "000503",
                "target_board": 1,
                "candidate_route": "oversold_reversal_start",
                "probability": 0.18,
                "sub_probabilities": {"first_limitup_next_day": 0.18},
                "probability_factors": {},
            }
        ],
        {
            "T1:oversold_reversal_start": {
                "sample_count": 80,
                "success_count": 2,
                "success_rate": 0.025,
                "avg_predicted_probability": 0.16,
                "empirical_probability": 0.03,
                "empirical_rising_probability": 0.56,
                "empirical_strong_rising_probability": 0.14,
            }
        },
    )[0]

    assert adjusted["probability"] < 0.08
    assert adjusted["sub_probabilities"]["next_day_rise"] == pytest.approx(0.56, abs=0.01)
    assert adjusted["sub_probabilities"]["next_day_strong_rise"] == pytest.approx(0.14, abs=0.01)
    assert adjusted["sub_probabilities"]["next_day_rise"] > adjusted["probability"]


def test_promotion_learning_falls_back_to_route_bucket_for_new_detailed_bucket():
    adjusted = promotion._apply_promotion_learning_to_candidates(
        [
            {
                "code": "000501",
                "target_board": 2,
                "candidate_route": "second_board_promotion",
                "learning_bucket": "T2:second_board_promotion:standard:quality_a",
                "probability": 0.55,
                "sub_probabilities": {"next_second_board": 0.55},
                "probability_factors": {},
            }
        ],
        {
            "T2:second_board_promotion": {
                "sample_count": 80,
                "success_count": 12,
                "success_rate": 0.15,
                "avg_predicted_probability": 0.40,
                "empirical_probability": 0.16,
            }
        },
    )[0]

    assert adjusted["probability"] < 0.55
    assert adjusted["learning_bucket"] == "T2:second_board_promotion"
    assert adjusted["probability_factors"]["learning_sample_count"] == 80


def test_first_board_learning_uses_capped_missed_hit_boost_for_ranking_only():
    learned = promotion._apply_promotion_learning_to_candidates(
        [
            {
                "code": "000502",
                "target_board": 1,
                "candidate_route": "mainline_spread_start",
                "probability": 0.18,
                "sub_probabilities": {"first_limitup_next_day": 0.18},
                "probability_factors": {},
            }
        ],
        {
            "T1:mainline_spread_start": {
                "sample_count": 80,
                "success_count": 4,
                "success_rate": 0.05,
                "avg_predicted_probability": 0.25,
                "empirical_probability": 0.06,
                "pool_success_count": 20,
                "selection_recall": 0.20,
            }
        },
    )[0]

    unlearned_peer = {
        **learned,
        "code": "000503",
        "probability": learned["probability"] + 0.01,
        "learning_recall_rank_boost": 0.0,
        "probability_factors": {
            **learned["probability_factors"],
            "learning_recall_rank_boost": 0.0,
        },
    }

    assert learned["learning_recall_rank_boost"] == pytest.approx(2.5)
    assert learned["probability_factors"]["learning_recall_rank_boost"] == pytest.approx(2.5)
    assert promotion._first_board_rank_key(learned) > promotion._first_board_rank_key(unlearned_peer)


@pytest.mark.asyncio
async def test_actual_limit_up_replay_classifies_pool_rank_and_misses(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        session.add_all(
            [
                LimitUpPool(
                    code="000005",
                    name="漏识别首板",
                    trade_date=date(2026, 4, 23),
                    consecutive_days=1,
                    seal_amount=70_000_000,
                    break_count=0,
                    turnover=4.5,
                    source="test",
                ),
                LimitUpPool(
                    code="000006",
                    name="ST过滤首板",
                    trade_date=date(2026, 4, 23),
                    consecutive_days=1,
                    seal_amount=50_000_000,
                    break_count=0,
                    turnover=3.2,
                    source="test",
                ),
                StockTag(
                    code="000006",
                    name="ST过滤首板",
                    board_type="main_sz",
                    board_tag="blocked",
                    is_st=True,
                ),
                PromotionPredictionRecord(
                    code="000001",
                    name="测试A",
                    target_board=2,
                    prediction_trade_date=date(2026, 4, 22),
                    horizon_days=1,
                    predicted_probability=0.31,
                    calibrated_probability=0.31,
                    candidate_route="second_board_promotion",
                    learning_bucket="T2:second_board_promotion",
                    outcome_status="success",
                    reason_snapshot='{"candidate_route_label":"首板晋级"}',
                    factors_json='{"prediction_ranked_selected":true,"prediction_ranked_position":3,"prediction_ranked_limit":30,"second_board_style_score":66}',
                ),
                PromotionPredictionRecord(
                    code="000004",
                    name="测试D",
                    target_board=1,
                    prediction_trade_date=date(2026, 4, 22),
                    horizon_days=1,
                    predicted_probability=0.22,
                    calibrated_probability=0.22,
                    candidate_route="mainline_spread_start",
                    learning_bucket="T1:mainline_spread_start",
                    outcome_status="success",
                    reason_snapshot='{"candidate_route_label":"主线扩散补涨","strategy_lane":"mainline_spread_first_board","strategy_lane_label":"主线扩散补涨首板"}',
                    factors_json='{"prediction_ranked_selected":false,"prediction_pool_rank":44,"prediction_ranked_limit":30,"route_score":56}',
                ),
                *[
                    PromotionPredictionRecord(
                        code=f"6008{i:02d}",
                        name=f"首板快照填充{i}",
                        target_board=1,
                        prediction_trade_date=date(2026, 4, 22),
                        horizon_days=1,
                        predicted_probability=0.18,
                        calibrated_probability=0.18,
                        candidate_route="mainline_spread_start",
                        learning_bucket="T1:mainline_spread_start",
                        outcome_status="pending",
                        factors_json='{"prediction_ranked_selected":true,"prediction_ranked_limit":50,"route_score":50}',
                    )
                    for i in range(9)
                ],
            ]
        )
        await session.commit()

        rows = await promotion._load_limit_up_rows(session, date(2026, 4, 23))
        actual_limit_ups = [
            {
                "code": row.code,
                "name": row.name,
                "trade_date": str(row.trade_date),
                "consecutive_days": row.consecutive_days,
                "seal_amount": row.seal_amount,
                "break_count": row.break_count,
                "turnover": row.turnover,
                "limit_up_time": row.limit_up_time or "",
                "limit_up_reason": row.limit_up_reason or "",
            }
            for row in rows
        ]
        replay = await promotion._build_actual_limit_up_replay(
            session,
            actual_trade_date=date(2026, 4, 23),
            actual_limit_ups=actual_limit_ups,
        )

    by_code = {item["code"]: item for item in replay["items"]}
    assert replay["prediction_trade_date"] == "2026-04-22"
    assert by_code["000001"]["replay_status"] == "predicted_hit"
    assert by_code["000004"]["replay_status"] == "rank_cutoff"
    assert by_code["000004"]["strategy_lane_label"] == "主线扩散补涨首板"
    assert by_code["000005"]["replay_status"] == "not_in_pool"
    assert by_code["000006"]["replay_status"] == "filtered"
    assert replay["summary"]["rank_cutoff_count"] == 1
    assert replay["summary"]["not_in_pool_count"] == 1
    assert replay["summary"]["lane_metrics"]["target_1"]["ranked_hit_count"] == 0
    assert replay["summary"]["lane_metrics"]["target_1"]["ranked_precision"] == 0.0
    assert replay["summary"]["lane_metrics"]["target_2"]["ranked_hit_count"] == 1
    assert replay["summary"]["lane_metrics"]["target_2"]["ranked_precision"] == 1.0
    assert replay["summary"]["filtered_count"] == 1


@pytest.mark.asyncio
async def test_actual_limit_up_replay_explains_second_board_snapshot_record_gap(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        session.add(
            LimitUpPool(
                code="000003",
                name="测试C",
                trade_date=date(2026, 4, 23),
                consecutive_days=2,
                seal_amount=95_000_000,
                break_count=0,
                turnover=6.1,
                source="test",
            )
        )
        session.add_all(
            [
                PromotionPredictionRecord(
                    code=f"6009{i:02d}",
                    name=f"二板快照填充{i}",
                    target_board=2,
                    prediction_trade_date=date(2026, 4, 22),
                    horizon_days=1,
                    predicted_probability=0.28,
                    calibrated_probability=0.28,
                    candidate_route="second_board_promotion",
                    learning_bucket="T2:second_board_promotion",
                    outcome_status="pending",
                    factors_json='{"prediction_ranked_selected":true,"prediction_ranked_limit":50,"route_score":28}',
                )
                for i in range(promotion.PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS)
            ]
        )
        await session.commit()

        actual = await promotion._load_filtered_limit_ups(session, date(2026, 4, 23))
        replay = await promotion._build_actual_limit_up_replay(
            session,
            actual_trade_date=date(2026, 4, 23),
            actual_limit_ups=actual,
        )

    by_code = {item["code"]: item for item in replay["items"]}
    assert by_code["000003"]["replay_status"] == "not_in_pool"
    assert by_code["000003"]["blocked_reason"] == "前日首板存在但未写入二板预测记录"
    assert "调度快照没有留下二板预测记录" in by_code["000003"]["blocked_reason_detail"]


@pytest.mark.asyncio
async def test_actual_limit_up_replay_marks_missing_record_as_snapshot_incomplete_when_snapshot_too_small(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                LimitUpPool(
                    code="000800",
                    name="前日样本",
                    trade_date=date(2026, 4, 22),
                    consecutive_days=1,
                    source="test",
                ),
                LimitUpPool(
                    code="600811",
                    name="快照缺失首板",
                    trade_date=date(2026, 4, 23),
                    consecutive_days=1,
                    seal_amount=70_000_000,
                    break_count=0,
                    turnover=4.5,
                    source="test",
                ),
                PromotionPredictionRecord(
                    code="600800",
                    name="少量首板快照",
                    target_board=1,
                    prediction_trade_date=date(2026, 4, 22),
                    horizon_days=1,
                    predicted_probability=0.18,
                    calibrated_probability=0.18,
                    candidate_route="mainline_spread_start",
                    learning_bucket="T1:mainline_spread_start",
                    outcome_status="pending",
                    factors_json='{"prediction_ranked_selected":true,"prediction_ranked_limit":50,"route_score":50}',
                ),
            ]
        )
        await session.commit()
        actual = await promotion._load_filtered_limit_ups(session, date(2026, 4, 23))
        replay = await promotion._build_actual_limit_up_replay(
            session,
            actual_trade_date=date(2026, 4, 23),
            actual_limit_ups=actual,
        )

    item = next(row for row in replay["items"] if row["code"] == "600811")
    assert item["replay_status"] == "snapshot_incomplete"
    assert item["blocked_reason"] == "预测快照不完整"
    assert replay["summary"]["snapshot_incomplete_count"] == 1
    assert replay["summary"]["prediction_record_counts"]["target_1"] == 1


@pytest.mark.asyncio
async def test_prediction_snapshot_health_warns_before_request_backfills_records(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        missing = await promotion._build_prediction_snapshot_health(
            session,
            first_board_trade_date=date(2026, 4, 23),
            second_board_trade_date=date(2026, 4, 23),
            actual_trade_date=date(2026, 4, 23),
        )
        session.add_all(
            [
                *[
                    PromotionPredictionRecord(
                        code=f"00001{i}",
                        name=f"首板快照{i}",
                        target_board=1,
                        prediction_trade_date=date(2026, 4, 23),
                        candidate_route="mainline_spread_start",
                        outcome_status="pending",
                    )
                    for i in range(promotion.PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS)
                ],
                *[
                    PromotionPredictionRecord(
                        code=f"00004{i}",
                        name=f"二板快照{i}",
                        target_board=2,
                        prediction_trade_date=date(2026, 4, 23),
                        candidate_route="second_board_promotion",
                        outcome_status="pending",
                    )
                    for i in range(promotion.PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS)
                ],
                PromotionPredictionRecord(
                    code="000005",
                    name="回放快照",
                    target_board=1,
                    prediction_trade_date=date(2026, 4, 22),
                    candidate_route="mainline_spread_start",
                    outcome_status="pending",
                ),
                AuctionData(
                    code="000010",
                    trade_date=date(2026, 4, 23),
                    auction_time="09:25:00",
                    **verified_auction_fields(date(2026, 4, 23), "09:25:00"),
                    auction_price=10.3,
                    prev_close=10.0,
                    open_change=3.0,
                    auction_volume=280_000,
                    volume_ratio=2.2,
                    auction_amount=28_000_000,
                ),
            ]
        )
        await session.commit()
        ok = await promotion._build_prediction_snapshot_health(
            session,
            first_board_trade_date=date(2026, 4, 23),
            second_board_trade_date=date(2026, 4, 23),
            actual_trade_date=date(2026, 4, 23),
        )

    assert missing["status"] == "missing_snapshot"
    assert missing["should_warn"] is True
    assert missing["message"] == "预测快照缺失"
    assert "不能展示旧预测当今天预测" in missing["detail"]
    assert ok["status"] == "ok"
    assert ok["should_warn"] is False
    assert ok["auction_health"]["snapshot_count"] == 1
    assert ok["record_counts"]["target_1"] == promotion.PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS
    assert ok["record_counts"]["target_2"] == promotion.PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS


@pytest.mark.asyncio
async def test_prediction_snapshot_health_rejects_explicit_old_model_version(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 23)
    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        session.add_all(
            [
                *[
                    PromotionPredictionRecord(
                        code=f"6007{index:02d}",
                        name=f"旧版首板{index}",
                        target_board=1,
                        prediction_trade_date=prediction_date,
                        candidate_route="mainline_spread_start",
                        snapshot_source="schedule",
                        snapshot_context="promotion_2000",
                        model_version="promotion_old_explicit",
                        outcome_status="pending",
                    )
                    for index in range(promotion.PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS)
                ],
                *[
                    PromotionPredictionRecord(
                        code=f"6008{index:02d}",
                        name=f"旧版二板{index}",
                        target_board=2,
                        prediction_trade_date=prediction_date,
                        candidate_route="second_board_promotion",
                        snapshot_source="schedule",
                        snapshot_context="promotion_2000",
                        model_version="promotion_old_explicit",
                        outcome_status="pending",
                    )
                    for index in range(promotion.PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS)
                ],
                PromotionPredictionRecord(
                    code="600899",
                    name="历史回放",
                    target_board=1,
                    prediction_trade_date=date(2026, 4, 22),
                    candidate_route="mainline_spread_start",
                    snapshot_source="schedule",
                    snapshot_context="promotion_2000",
                    model_version="promotion_old_explicit",
                    outcome_status="pending",
                ),
                AuctionData(
                    code="600700",
                    trade_date=prediction_date,
                    auction_time="09:25:00",
                    auction_price=10.3,
                    prev_close=10.0,
                    open_change=3.0,
                    auction_volume=280_000,
                    auction_amount=28_000_000,
                    volume_ratio=2.2,
                ),
            ]
        )
        await session.commit()
        health = await promotion._build_prediction_snapshot_health(
            session,
            first_board_trade_date=prediction_date,
            second_board_trade_date=prediction_date,
            actual_trade_date=prediction_date,
        )

    assert health["status"] == "stale_model_version"
    assert health["message"] == "预测快照版本过期"
    assert health["model_version_mismatch"] is True
    assert health["record_counts"]["target_1"] == 0
    assert health["record_counts"]["raw_target_1"] == promotion.PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS
    assert health["record_counts"]["raw_target_2"] == promotion.PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS
    assert "promotion_old_explicit" in health["detail"]


@pytest.mark.asyncio
async def test_prediction_snapshot_health_counts_unique_codes_not_route_rows(
    promotion_api_env,
):
    SessionLocal, _client = promotion_api_env
    prediction_date = date(2026, 4, 23)
    async with SessionLocal() as session:
        await _seed_limit_up_pool(session)
        session.add_all(
            [
                *[
                    PromotionPredictionRecord(
                        code="600777",
                        name="同股多路线",
                        target_board=1,
                        prediction_trade_date=prediction_date,
                        candidate_route=f"route_{index}",
                        snapshot_source="schedule",
                        snapshot_context="promotion_2000",
                        model_version=promotion.PROMOTION_MODEL_VERSION,
                        outcome_status="pending",
                    )
                    for index in range(promotion.PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS)
                ],
                *[
                    PromotionPredictionRecord(
                        code=f"6008{index:02d}",
                        name=f"二板{index}",
                        target_board=2,
                        prediction_trade_date=prediction_date,
                        candidate_route="second_board_promotion",
                        snapshot_source="schedule",
                        snapshot_context="promotion_2000",
                        model_version=promotion.PROMOTION_MODEL_VERSION,
                        outcome_status="pending",
                    )
                    for index in range(promotion.PROMOTION_MIN_SECOND_BOARD_SNAPSHOT_RECORDS)
                ],
                PromotionPredictionRecord(
                    code="600899",
                    name="历史回放",
                    target_board=1,
                    prediction_trade_date=date(2026, 4, 22),
                    candidate_route="mainline_spread_start",
                    snapshot_source="schedule",
                    snapshot_context="promotion_2000",
                    model_version=promotion.PROMOTION_MODEL_VERSION,
                    outcome_status="pending",
                ),
                AuctionData(
                    code="600777",
                    trade_date=prediction_date,
                    auction_time="09:25:00",
                    auction_price=10.3,
                    prev_close=10.0,
                    open_change=3.0,
                    auction_volume=280_000,
                    auction_amount=28_000_000,
                    volume_ratio=2.2,
                ),
            ]
        )
        await session.commit()
        health = await promotion._build_prediction_snapshot_health(
            session,
            first_board_trade_date=prediction_date,
            second_board_trade_date=prediction_date,
            actual_trade_date=prediction_date,
        )

    assert health["status"] == "missing_snapshot"
    assert health["record_counts"]["target_1"] == 1
    assert health["record_counts"]["raw_target_1"] == 1
    assert (
        health["snapshot_context_counts"]["2026-04-23"]["target_1"]["promotion_2000"]
        == promotion.PROMOTION_MIN_FIRST_BOARD_SNAPSHOT_RECORDS
    )
    assert (
        health["snapshot_context_unique_code_counts"]["2026-04-23"]["target_1"]["promotion_2000"]
        == 1
    )


@pytest.mark.asyncio
async def test_prediction_snapshot_health_warns_when_route_counts_are_too_small(promotion_api_env):
    SessionLocal, _client = promotion_api_env

    async with SessionLocal() as session:
        session.add_all(
            [
                PromotionPredictionRecord(
                    code="000901",
                    name="反包首板快照",
                    target_board=1,
                    prediction_trade_date=date(2026, 4, 23),
                    candidate_route="oversold_reversal_start",
                    outcome_status="pending",
                ),
                PromotionPredictionRecord(
                    code="000902",
                    name="二板快照",
                    target_board=2,
                    prediction_trade_date=date(2026, 4, 23),
                    candidate_route="second_board_promotion",
                    outcome_status="pending",
                ),
                AuctionData(
                    code="000901",
                    trade_date=date(2026, 4, 23),
                    auction_time="09:25:00",
                    auction_price=10.3,
                    prev_close=10.0,
                    open_change=3.0,
                    auction_volume=280_000,
                    volume_ratio=2.2,
                    auction_amount=28_000_000,
                ),
            ]
        )
        await session.commit()
        health = await promotion._build_prediction_snapshot_health(
            session,
            first_board_trade_date=date(2026, 4, 23),
            second_board_trade_date=date(2026, 4, 23),
            actual_trade_date=date(2026, 4, 23),
        )

    assert health["status"] == "missing_snapshot"
    assert health["should_warn"] is True
    assert "预测快照不完整" in health["detail"]
    assert health["record_counts"]["target_1"] == 1
    assert health["record_counts"]["target_2"] == 1
