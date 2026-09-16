from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.data.sources.tencent_source import TencentSource
from app.signal.anomaly_scanner import (
    AnomalyScanner,
    _build_large_order_inflow_context,
    _derive_kline_boundary,
    _is_main_wave_core_sector_factor,
    _is_tradeable_linkage_sector,
    _spot_is_fresh_for_live_scan,
    _spot_volume_in_kline_unit,
)
from app.signal.dragon_head import DragonHeadScanner
from app.signal.breakthrough import BreakthroughSignal
from app.api.v1 import tenbagger as tenbagger_api
from app.api.v1.tenbagger import (
    DRAGON_SNAPSHOT_VERSION,
    PLAN_SNAPSHOT_VERSION,
    _DYNAMIC_TREND_POOL_SOURCE_PRIORITY,
    _apply_event_limit_up_protocol,
    _apply_high_board_watch_protocol,
    _apply_leader_linkage_protocol,
    _apply_sector_core_laggard_protocol,
    _apply_ultra_short_entry_protocol,
    _apply_next_day_execution_risk_gate,
    _apply_next_day_prediction_quality,
    _apply_repair_followup_entry_protocol,
    _apply_trend_driver_entry_protocol,
    _build_low_expectation_trend_pattern,
    _build_buypoint_continuity_context,
    _build_next_day_plan_replay,
    _build_risk_flags,
    _build_stock_rows,
    _cap_daily_direct_buy_plans,
    _cap_automatic_push_messages_by_persisted_budget,
    _classify_market_breadth,
    _classify_high_board_candidate,
    _dedupe_dragons_by_code,
    _diversified_actionable_top,
    _downgrade_backtested_low_edge_plan,
    _downgrade_observe_only_plan,
    _is_chase_type_anomaly,
    _is_capacity_trend_candidate,
    _is_second_wave_direct_buy_ready,
    _kline_chase_reasons,
    _pick_plan_kline_pattern,
    _score_high_board_ignition_kline_pattern,
    _score_main_wave_kline_pattern,
    _score_momentum_shakeout_kline_pattern,
    _score_platform_breakout_kline_pattern,
    _score_pre_board_ignition_kline_pattern,
    _score_probe_wash_kline_pattern,
    _score_second_wave_kline_pattern,
    _score_second_wave_reset_watch_pattern,
    _score_trend_main_wave_kline_pattern,
    _score_trend_driver_kline_pattern,
    _select_dynamic_trend_pool_candidates,
    _is_low_base_watchlist_anomaly,
    _load_main_wave_plan_candidates,
    _load_sector_core_laggard_plan_candidates,
    _load_sent_repair_plan_candidates,
    _main_wave_pullback_shape_stats,
    _next_day_plan_action_priority,
    _record_sent_anomaly_signals,
    _settle_anomaly_signal_performance,
    _filter_push_messages_by_performance,
    _filter_main_board_items,
    _filter_plan_main_board_items,
    _board_like_change_threshold,
    _format_pattern_pool_rows,
    _get_latest_compatible_plan_snapshot,
    _enrich_plan_candidates_with_direct_catalysts,
    prewarm_dragon_snapshot,
)
from app.models.governance import DashboardSnapshot
from app.models.signal import AnomalyCandidateRecord, SignalPerformance
from app.models.stock import LimitUpPool, SectorInfo, SectorPersistence, StockKline, StockSectorMapping, StockSpot
from app.models.sector import SectorLifecycle
from app.push.channels.base import PushMessage


def test_filter_main_board_items_excludes_gem_star_bse_and_unknown_codes():
    rows = [
        {"code": "600001"},
        {"code": "000001"},
        {"code": "002001"},
        {"code": "003001"},
        {"code": "600002", "name": "ST测试"},
        {"code": "300001"},
        {"code": "301001"},
        {"code": "688001"},
        {"code": "920001"},
        {"code": ""},
    ]

    assert [row["code"] for row in _filter_main_board_items(rows)] == [
        "600001",
        "000001",
        "002001",
        "003001",
    ]


def test_plan_main_board_filter_keeps_only_zero_position_st_research_rows():
    rows = [
        {"code": "600001", "name": "普通主板", "is_tradeable": True},
        {
            "code": "600002",
            "name": "*ST测试",
            "is_tradeable": False,
            "research_only": True,
        },
        {"code": "600003", "name": "ST误放", "is_tradeable": True},
        {"code": "300001", "name": "创业板", "is_tradeable": True},
        {"code": "600004", "name": "退市样本", "is_tradeable": False, "research_only": True},
    ]

    assert [row["code"] for row in _filter_plan_main_board_items(rows)] == [
        "600001",
        "600002",
    ]


def test_plan_quality_rejects_generic_sector_as_main_driver():
    base = {
        "code": "603408",
        "bull_score": 92,
        "main_wave_score": 88,
        "candidate_source": "leader_linkage_follow",
        "change_pct": 1.2,
        "sector_resonance": "强共振",
        "sector_lifecycle_state": "emerging",
        "main_wave_stats": {"setup_state": "armed_pullback"},
        "is_tradeable": True,
    }
    generic = _apply_next_day_prediction_quality({
        **base,
        "sector_resonance_name": "融资融券",
        "sector_driver_causal": False,
    })
    causal = _apply_next_day_prediction_quality({
        **base,
        "sector_resonance_name": "智能家居",
        "sector_driver_causal": True,
    })

    assert causal["plan_priority_score"] > generic["plan_priority_score"]
    assert "缺少可验证产业主驱动" in generic["plan_priority_reasons"]


def test_plan_quality_keeps_dormant_independent_shape_out_of_a_tier_without_catalyst():
    base = {
        "code": "600990",
        "bull_score": 96,
        "main_wave_score": 96,
        "candidate_source": "pre_board_probe_wash",
        "change_pct": -0.5,
        "sector_resonance": "独立行情",
        "sector_resonance_change": -1.8,
        "sector_lifecycle_state": "dormant",
        "sector_driver_causal": False,
        "main_wave_stats": {
            "setup_state": "armed_pullback",
            "position_120": 0.30,
            "dry_up_ratio": 0.70,
        },
        "strategies": [{"strategy_type": "watch"}],
        "is_tradeable": True,
    }

    shape_only = _apply_next_day_prediction_quality(base)
    hard_event = _apply_next_day_prediction_quality({
        **base,
        "event_catalyst": {
            "grade": "hard",
            "type": "major_contract",
            "title": "重大合同公告",
            "fresh_after_close": True,
        },
    })

    assert shape_only["plan_priority_tier"] != "A"
    assert "独立形态缺少当日资金方向" in shape_only["plan_priority_reasons"]
    assert hard_event["plan_priority_score"] > shape_only["plan_priority_score"]
    assert "直接硬消息可追溯" in hard_event["plan_priority_reasons"]


def test_low_expectation_pattern_uses_catalyst_without_faking_entry():
    trend_driver = {
        "is_trend_driver_pattern": True,
        "trend_driver_score": 96.0,
        "trend_driver_stats": {
            "setup_state": "armed_breakout",
            "ma10": 23.41,
            "ma20": 22.53,
            "ma20_slope_5d": 2.15,
            "pct_20": 3.46,
            "support": 23.41,
            "support_gap_pct": -1.11,
            "resistance": 24.95,
            "distance_to_resistance_pct": -7.82,
            "position_60": 0.296,
            "board_like_count_20": 0,
            "max_drawdown_20": 12.05,
        },
    }
    catalyst = {
        "news_event_grade": "hard",
        "news_event_type": "earnings_surge",
        "news_catalyst_score": 61.0,
        "news_title": "上半年净利润同比增长183.64%",
    }

    pattern = _build_low_expectation_trend_pattern(
        trend_driver,
        change_pct=0.61,
        turnover=2.5,
        volume_ratio=0.86,
        news_catalyst=catalyst,
    )

    assert pattern["is_ready"] is True
    assert pattern["source"] == "low_expectation_trend_watch"
    assert pattern["stats"]["setup_state"] == "armed_pullback"
    assert pattern["stats"]["support"] == pytest.approx(22.53)
    assert pattern["stats"]["requires_rolling_60s_volume"] is True
    assert pattern["stats"]["position_before_trigger"] == 0

    enriched = _enrich_plan_candidates_with_direct_catalysts([{
        "code": "001287",
        "candidate_source": pattern["source"],
        "candidate_source_label": pattern["label"],
        "main_wave_score": pattern["score"],
        "main_wave_stats": pattern["stats"],
        "top_signals": [],
        "risk_warnings": [],
    }], {"001287": catalyst})[0]
    assert enriched["candidate_source_label"] == "盘后硬利好-低位趋势确认"
    assert enriched["candidate_source"] == "low_expectation_trend_watch"
    assert any("公告只提升关注优先级" in item for item in enriched["risk_warnings"])


@pytest.mark.asyncio
async def test_sector_core_laggard_loader_requires_multi_hot_sector_membership(tenbagger_session):
    target_date = date(2026, 8, 18)
    tenbagger_session.add_all([
        SectorPersistence(
            sector_code="GN_CORN", sector_name="玉米", trade_date=date(2026, 8, 17),
            consecutive_days=0, limit_up_count=0, fund_flow=-6.2,
            change_pct=-2.4, strength_score=8,
        ),
        SectorPersistence(
            sector_code="GN_GRAIN", sector_name="粮食概念", trade_date=date(2026, 8, 17),
            consecutive_days=0, limit_up_count=0, fund_flow=-5.1,
            change_pct=-1.9, strength_score=10,
        ),
        SectorPersistence(
            sector_code="GN_CORN", sector_name="玉米", trade_date=target_date,
            # 当天首次形成的高宽度扩散，验证无需等到连续活跃第二天。
            consecutive_days=1, limit_up_count=12, fund_flow=10.2,
            change_pct=7.4, strength_score=90,
        ),
        SectorPersistence(
            sector_code="GN_GRAIN", sector_name="粮食概念", trade_date=target_date,
            consecutive_days=3, limit_up_count=16, fund_flow=7.2,
            change_pct=6.5, strength_score=90,
        ),
        SectorLifecycle(
            sector_code="GN_CORN", sector_name="玉米", sector_type="concept",
            trade_date=target_date, lifecycle_state="dormant", state_score=35,
            active_days=1,
        ),
        SectorLifecycle(
            sector_code="GN_GRAIN", sector_name="粮食概念", sector_type="concept",
            trade_date=target_date, lifecycle_state="emerging", state_score=84,
            active_days=3,
        ),
        StockSpot(
            code="000998", name="隆平高科", price=12.1, prev_close=11.9,
            open=11.9, high=12.2, low=11.8, avg_price=12.0, limit_up=13.09,
            change_pct=1.68, volume=100_000, amount=150_000_000,
            volume_ratio=1.8, turnover=3.2, circ_market_cap=180,
        ),
        StockSpot(
            code="002412", name="弱映射样本", price=8.1, prev_close=8.0,
            open=8.0, high=8.2, low=7.95, avg_price=8.08, limit_up=8.8,
            change_pct=1.25, volume=80_000, amount=90_000_000,
            volume_ratio=1.7, turnover=2.6, circ_market_cap=80,
        ),
        LimitUpPool(
            code="600354", name="敦煌种业", trade_date=target_date,
            consecutive_days=1, limit_up_price=9.9, source="test",
        ),
        StockSectorMapping(
            code="000998", sector_code="GN_CORN", sector_name="玉米",
            sector_type="concept", source="pywencai",
        ),
        StockSectorMapping(
            code="000998", sector_code="GN_GRAIN", sector_name="粮食概念",
            sector_type="concept", source="pywencai",
        ),
        StockSectorMapping(
            code="002412", sector_code="GN_CORN", sector_name="玉米",
            sector_type="concept", source="pywencai",
        ),
        StockSectorMapping(
            code="000998", sector_code="IND_SEED", sector_name="农林牧渔-种植业与林业-种子生产",
            sector_type="industry", source="pywencai",
        ),
        StockSectorMapping(
            code="002412", sector_code="IND_MED", sector_name="医药生物-中药-中药III",
            sector_type="industry", source="pywencai",
        ),
        StockSectorMapping(
            code="600354", sector_code="GN_CORN", sector_name="玉米",
            sector_type="concept", source="pywencai",
        ),
        StockSectorMapping(
            code="600354", sector_code="IND_AGRI", sector_name="农林牧渔-种植业与林业-其他种植业",
            sector_type="industry", source="pywencai",
        ),
    ])
    await tenbagger_session.commit()

    rows = await _load_sector_core_laggard_plan_candidates(
        tenbagger_session, target_date, limit=10,
    )

    assert [item["code"] for item in rows] == ["000998"]
    assert rows[0]["candidate_source"] == "sector_core_laggard"
    assert rows[0]["main_wave_stats"]["hot_sector_membership_count"] == 2
    assert rows[0]["main_wave_stats"]["business_aligned_sector_count"] == 1
    assert rows[0]["main_wave_stats"]["position_before_trigger"] == 0
    assert rows[0]["main_wave_stats"]["breadth_climax_risk"] is True
    assert rows[0]["main_wave_stats"]["one_day_reversal_burst"] is True
    assert rows[0]["main_wave_stats"]["previous_sector_change_pct"] < -1.5


def test_sector_core_laggard_plan_stays_zero_position_until_a2_confirmation():
    plan = {
        "candidate_source": "sector_core_laggard",
        "is_tradeable": True,
        "main_wave_stats": {
            "sector_code": "GN_CORN",
            "sector_name": "玉米",
            "sector_strength_score": 90,
            "sector_change_pct": 7.4,
            "sector_fund_flow": 10.2,
            "sector_limit_up_count": 12,
            "sector_lifecycle_state": "emerging",
            "hot_sector_membership_count": 3,
            "hot_sector_names": ["玉米", "粮食概念", "农业种植"],
            "business_aligned_sector_count": 2,
        },
    }

    result = _apply_sector_core_laggard_protocol(plan)

    assert result["strategies"][0]["strategy_type"] == "sector_core_laggard_watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert "buy_signal" not in result
    assert result["monitor_state"] == "sector_core_laggard_revalidation"
    assert result["monitor_trigger"]["status"] == "waiting_current_sector_and_rolling_60s_confirmation"
    assert result["sector_core_laggard"]["overnight_revalidation_required"] is True


def test_sector_core_laggard_is_penalized_for_overnight_rotation_and_climax():
    base = {
        "bull_score": 96,
        "main_wave_score": 92,
        "change_pct": 1.2,
        "strategies": [{"strategy_type": "sector_core_laggard_watch"}],
        "sector_driver_causal": True,
        "sector_resonance": "强共振",
        "sector_lifecycle_state": "emerging",
        "main_wave_stats": {
            "setup_state": "armed_sector_core_laggard",
            "breadth_climax_risk": True,
            "one_day_reversal_burst": True,
        },
        "is_tradeable": True,
    }
    laggard = _apply_next_day_prediction_quality({
        **base,
        "candidate_source": "sector_core_laggard",
    })
    individual = _apply_next_day_prediction_quality({
        **base,
        "candidate_source": "pre_board_probe_wash",
        "strategies": [{"strategy_type": "watch"}],
    })

    assert laggard["plan_priority_score"] < individual["plan_priority_score"]
    assert laggard["plan_priority_tier"] != "A"
    assert "隔夜板块补涨待当日重验" in laggard["plan_priority_reasons"]


def test_diversified_plan_caps_overnight_sector_laggards_at_two_of_top20():
    rows = [
        {
            "code": f"600{i:03d}",
            "candidate_source": "sector_core_laggard",
            "plan_priority_score": 95 - i,
            "strategies": [{"strategy_type": "sector_core_laggard_watch"}],
            "is_tradeable": True,
        }
        for i in range(12)
    ] + [
        {
            "code": f"001{i:03d}",
            "candidate_source": "pre_board_probe_wash",
            "plan_priority_score": 80 - i,
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": True,
        }
        for i in range(20)
    ]

    selected = _diversified_actionable_top(rows, 20)

    assert sum(item["candidate_source"] == "sector_core_laggard" for item in selected) == 2


def test_diversified_plan_reserves_strict_pullback_lane_without_flooding_top20():
    ordinary = [
        {
            "code": f"600{i:03d}",
            "candidate_source": "pre_board_probe_wash",
            "plan_priority_score": 90 - i * 0.1,
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": True,
        }
        for i in range(24)
    ]
    pullbacks = [
        {
            "code": f"001{i:03d}",
            "candidate_source": "main_wave_pullback_pattern",
            "plan_priority_score": 40 - i,
            "main_wave_score": 85 - i,
            "main_wave_stats": {
                "pullback_quality_tier": "strict" if i < 3 else "reclaim",
            },
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": True,
        }
        for i in range(6)
    ]

    selected = _diversified_actionable_top([*ordinary, *pullbacks], 20)
    selected_pullbacks = [
        item for item in selected
        if item["candidate_source"] == "main_wave_pullback_pattern"
    ]

    assert 3 <= len(selected_pullbacks) <= 4
    assert all(
        item["main_wave_stats"]["pullback_quality_tier"] == "strict"
        for item in selected_pullbacks[:3]
    )


def test_diversified_plan_reserves_dynamic_pullback_memory_after_candidate_merge():
    ordinary = [
        {
            "code": f"600{i:03d}",
            "candidate_source": "pre_board_probe_wash",
            "plan_priority_score": 90 - i * 0.1,
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": True,
        }
        for i in range(24)
    ]
    dynamic_pullback = {
        "code": "001999",
        "candidate_source": "trend_driver_setup",
        "dynamic_pool_source": "main_wave_pullback_pattern",
        "plan_priority_score": 35,
        "main_wave_score": 88,
        "main_wave_stats": {"setup_state": "armed_breakout"},
        "dynamic_pool_stats": {"pullback_quality_tier": "strict"},
        "strategies": [{"strategy_type": "watch"}],
        "is_tradeable": True,
    }

    selected = _diversified_actionable_top([*ordinary, dynamic_pullback], 20)

    assert any(item["code"] == "001999" for item in selected)


def test_diversified_plan_reserves_each_high_board_logic_lane():
    lane_types = [
        "event_lock",
        "theme_turnover",
        "emotion_memory",
        "earnings_surprise",
        "second_wave",
    ]
    high_board_rows = [
        {
            "code": f"60000{index}",
            "candidate_source": f"high_board_{lane}",
            "high_board_types": [lane],
            "is_high_board_candidate": True,
            "plan_priority_score": 56,
            "main_wave_score": 75,
            "strategies": [{"strategy_type": "high_board_watch"}],
            "is_tradeable": True,
        }
        for index, lane in enumerate(lane_types, start=1)
    ]
    ordinary = [
        {
            "code": f"001{i:03d}",
            "candidate_source": "pre_board_probe_wash",
            "plan_priority_score": 95 - i,
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": True,
        }
        for i in range(30)
    ]

    selected = _diversified_actionable_top([*ordinary, *high_board_rows], 20)
    selected_codes = {item["code"] for item in selected}

    assert {item["code"] for item in high_board_rows}.issubset(selected_codes)


def test_diversified_plan_keeps_two_st_rows_as_research_only():
    ordinary = [
        {
            "code": f"600{i:03d}",
            "candidate_source": "high_board_theme_turnover",
            "high_board_types": ["theme_turnover"],
            "is_high_board_candidate": True,
            "plan_priority_score": 80 - i,
            "strategies": [{"strategy_type": "high_board_watch"}],
            "is_tradeable": True,
        }
        for i in range(20)
    ]
    st_rows = [
        {
            "code": f"0029{i:02d}",
            "name": f"ST样本{i}",
            "candidate_source": "high_board_theme_turnover",
            "high_board_types": ["theme_turnover"],
            "is_high_board_candidate": True,
            "plan_priority_score": 0,
            "main_wave_score": 70 - i,
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": False,
            "research_only": True,
        }
        for i in range(3)
    ]

    selected = _diversified_actionable_top([*ordinary, *st_rows], 20)

    assert sum(bool(item.get("research_only")) for item in selected) == 2


@pytest.mark.asyncio
async def test_leader_linkage_does_not_use_st_leader_to_endorse_main_board_follower(
    tenbagger_session,
):
    from app.api.v1.tenbagger import _load_leader_linkage_plan_candidates

    rows = await _load_leader_linkage_plan_candidates(
        tenbagger_session,
        date(2026, 8, 21),
        [{
            "code": "600497",
            "name": "驰宏锌锗",
            "leader_links": [{
                "leader_code": "002667",
                "leader_name": "*ST威领",
                "linkage_score": 92,
            }],
        }],
    )

    assert rows == []


@pytest_asyncio.fixture
async def tenbagger_session(tmp_path):
    db_path = tmp_path / "tenbagger_logic.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with SessionLocal() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_snapshot_lookup_index_exists(tenbagger_session):
    result = await tenbagger_session.execute(text("PRAGMA index_list('dashboard_snapshot')"))

    assert "ix_dashboard_snapshot_lookup" in {row[1] for row in result.all()}


@pytest.mark.asyncio
async def test_latest_compatible_plan_snapshot_reuses_newer_different_limit(tenbagger_session):
    target_date = date(2026, 8, 11)
    older = DashboardSnapshot(
        snapshot_key=f"tenbagger-plan:20:{PLAN_SNAPSHOT_VERSION}",
        trade_date=target_date,
        snapshot_time=datetime.now() - timedelta(minutes=30),
        payload_json='{"plans":[{"code":"old"}],"trend_pool":[]}',
        status="ok",
    )
    newer = DashboardSnapshot(
        snapshot_key=f"tenbagger-plan:100:{PLAN_SNAPSHOT_VERSION}",
        trade_date=target_date,
        snapshot_time=datetime.now(),
        payload_json='{"plans":[{"code":"a"},{"code":"b"}],"trend_pool":[{"code":"600162"}]}',
        status="ok",
    )
    tenbagger_session.add_all([older, newer])
    await tenbagger_session.commit()

    payload = await _get_latest_compatible_plan_snapshot(tenbagger_session, target_date, 1)

    assert payload is not None
    assert [item["code"] for item in payload["plans"]] == ["a"]
    assert payload["source_snapshot_key"] == f"tenbagger-plan:100:{PLAN_SNAPSHOT_VERSION}"
    assert payload["served_from_snapshot"] is True
    assert payload["snapshot_stale"] is False


@pytest.mark.asyncio
async def test_next_day_plan_replay_separates_coverage_and_actionable_hits(
    tenbagger_session,
):
    prediction_date = date(2026, 8, 24)
    actual_date = date(2026, 8, 25)
    plans = [
        {
            "code": "000001",
            "name": "可执行命中",
            "is_tradeable": True,
            "strategies": [{"strategy_type": "trend"}],
            "market_breadth": {"direct_buy_ok": True},
            "recommended_action": "回踩确认",
        },
        {
            "code": "000002",
            "name": "观察上涨",
            "is_tradeable": True,
            "strategies": [{"strategy_type": "watch"}],
            "recommended_action": "观察",
        },
    ]
    tenbagger_session.add(
        DashboardSnapshot(
            snapshot_key=f"tenbagger-plan:20:{PLAN_SNAPSHOT_VERSION}",
            trade_date=prediction_date,
            snapshot_time=datetime(2026, 8, 24, 20, 0),
            payload_json=json.dumps(
                {
                    "trade_date": str(prediction_date),
                    "snapshot_time": "2026-08-24T20:00:00",
                    "plans": plans,
                    "trend_pool": [],
                },
                ensure_ascii=False,
            ),
            status="ok",
        )
    )
    for code, change_pct in (("000001", 10.0), ("000002", 1.2)):
        tenbagger_session.add_all(
            [
                StockKline(
                    code=code,
                    trade_date=prediction_date,
                    open=10,
                    close=10,
                    high=10.1,
                    low=9.9,
                    volume=100000,
                    change_pct=0,
                    prev_close=10,
                    source="test",
                ),
                StockKline(
                    code=code,
                    trade_date=actual_date,
                    open=10,
                    close=10 * (1 + change_pct / 100),
                    high=11 if change_pct >= 9.5 else 10.2,
                    low=9.9,
                    volume=120000,
                    change_pct=change_pct,
                    prev_close=10,
                    source="test",
                ),
            ]
        )
    tenbagger_session.add(
        LimitUpPool(
            code="000001",
            name="可执行命中",
            trade_date=actual_date,
            consecutive_days=1,
            source="test",
        )
    )
    await tenbagger_session.commit()

    replay = await _build_next_day_plan_replay(
        tenbagger_session,
        actual_date,
        limit=20,
    )

    assert replay["status"] == "ok"
    assert replay["predicted_count"] == 2
    assert replay["actionable_predicted_count"] == 1
    assert replay["limit_up_hit_count"] == 1
    assert replay["actionable_limit_up_hit_count"] == 1
    assert replay["rising_hit_count"] == 2
    assert replay["directional_precision"] == 1.0


@pytest.mark.asyncio
async def test_dragon_snapshot_reuses_expired_persisted_payload_outside_market(
    tenbagger_session,
    monkeypatch,
):
    target_date = date(2026, 8, 21)
    snapshot_key = f"tenbagger-dragon:50:{DRAGON_SNAPSHOT_VERSION}"
    tenbagger_session.add(DashboardSnapshot(
        snapshot_key=snapshot_key,
        trade_date=target_date,
        snapshot_time=datetime.now() - timedelta(hours=2),
        payload_json=(
            '{"trade_date":"2026-08-21","snapshot_time":"2026-08-21T15:01:00",'
            '"dragons":[{"code":"600508","name":"上海能源"}]}'
        ),
        status="ok",
    ))
    await tenbagger_session.commit()

    async def resolve_target_date(*_args, **_kwargs):
        return target_date

    monkeypatch.setattr(
        "app.api.v1.tenbagger.resolve_latest_trade_date",
        resolve_target_date,
    )
    monkeypatch.setattr("app.api.v1.tenbagger._DRAGON_CACHE", {})
    monkeypatch.setattr(
        "app.api.v1.tenbagger.trade_calendar.get_trade_session",
        lambda *_args, **_kwargs: "after_hours",
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.anomaly_scanner.scan_dragons",
        lambda *_args, **_kwargs: pytest.fail("盘后已有快照时不应重扫全市场"),
    )

    payload = await prewarm_dragon_snapshot(tenbagger_session, force_refresh=False)

    assert payload["dragons"][0]["code"] == "600508"
    assert payload["served_from_snapshot"] is True
    assert payload["snapshot_stale"] is True


def test_pattern_pool_exposes_second_wave_as_zero_position_watch():
    rows = _format_pattern_pool_rows(
        [{
            "code": "600162",
            "name": "香江控股",
            "score": 96,
            "label": "断板二波主升跟踪",
            "candidate_source": "second_wave_pattern",
            "setup_state": "armed_pullback",
            "support": 3.52,
            "resistance": 3.88,
            "main_wave_stats": {},
        }],
        {
            "600162": {
                "name": "香江控股",
                "price": 4.02,
                "change_pct": 10.14,
                "volume_ratio": 1.83,
                "turnover": 8.6,
                "limit_up": 4.02,
            }
        },
    )

    assert rows[0]["source_group"] == "二波跟踪"
    assert rows[0]["position_ratio"] == "0"
    assert rows[0]["feishu_monitoring"] is True
    assert rows[0]["monitor_status"] == "形态命中·当前不可追"
    assert "放量" in rows[0]["trigger_condition"]


def test_pattern_pool_marks_strict_main_wave_pullback_separately():
    rows = _format_pattern_pool_rows(
        [{
            "code": "600001",
            "name": "严格回踩样本",
            "score": 91,
            "label": "主升浪缩量十字星",
            "candidate_source": "main_wave_pullback_pattern",
            "setup_state": "armed_main_wave_shape_pullback",
            "support": 12.7,
            "resistance": 13.4,
            "main_wave_stats": {
                "pullback_quality_tier": "strict",
                "pullback_quality_label": "主升缩量价稳回踩",
                "pullback_day_change_pct": -0.6,
                "pullback_volume_contraction": 0.62,
            },
        }],
        {"600001": {"price": 12.8, "change_pct": -0.4}},
        {"600001": {
            "core_sector_confirmed": True,
            "core_sector_name": "机器人概念",
            "core_identity_confirmed": True,
            "large_order_inflow_confirmed": True,
            "large_order_net_inflow": 36_000_000,
        }},
    )

    assert rows[0]["strict_setup_ready"] is True
    assert rows[0]["monitor_status"] == "严格候选·待60秒A2"
    assert rows[0]["push_policy"] == "核心板块+大单+60秒确认后推送"


def test_main_wave_core_sector_rejects_generic_or_non_positive_lifecycle():
    ready = {
        "sector_name": "机器人概念",
        "sector_type": "concept",
        "source": "pywencai",
        "lifecycle_state": "emerging",
        "lifecycle_score": 60,
        "active_days": 2,
        "strength_score": 72,
        "change_pct": 1.2,
        "fund_flow": 12,
        "limit_up_count": 3,
    }
    assert _is_main_wave_core_sector_factor(ready) is True
    assert _is_main_wave_core_sector_factor({**ready, "sector_name": "融资融券"}) is False
    assert _is_main_wave_core_sector_factor({**ready, "lifecycle_state": "declining"}) is False


def test_large_order_inflow_requires_amount_and_percentage_confirmation():
    ready = _build_large_order_inflow_context({
        "main_net_inflow": 80_000_000,
        "main_net_inflow_pct": 4.2,
        "super_net_inflow": 18_000_000,
        "big_net_inflow": 22_000_000,
        "is_stale": False,
    }, traded_amount=600_000_000)
    weak_big_order = _build_large_order_inflow_context({
        "main_net_inflow": 80_000_000,
        "main_net_inflow_pct": 4.2,
        "super_net_inflow": -12_000_000,
        "big_net_inflow": 13_000_000,
        "is_stale": False,
    }, traded_amount=600_000_000)

    assert ready["large_order_inflow_confirmed"] is True
    assert weak_big_order["large_order_inflow_confirmed"] is False


def test_actionable_breakthrough_filters_out_trend_ready_signals():
    scanner = AnomalyScanner()

    ma_alignment = BreakthroughSignal(
        code="000001",
        name="测试股",
        signal_type="ma",
        strength="strong",
        score=85,
        detail={"alignment": "bullish"},
        description="均线多头排列",
    )
    ma5_cross = BreakthroughSignal(
        code="000001",
        name="测试股",
        signal_type="ma",
        strength="weak",
        score=45,
        detail={"ma": "MA5", "value": 10.0, "price": 10.5},
        description="站上MA5(10.00)",
    )
    price_high = BreakthroughSignal(
        code="000001",
        name="测试股",
        signal_type="price_high",
        strength="strong",
        score=80,
        detail={"price": 15.2, "high": 15.0, "volume_ratio": 1.4},
        description="突破120日新高15.00，放量确认",
    )

    assert scanner._is_actionable_breakthrough(ma_alignment, 2.5, 1.2, 1.8) is False
    assert scanner._is_actionable_breakthrough(ma5_cross, 2.5, 1.2, 1.8) is False
    assert scanner._is_actionable_breakthrough(price_high, 2.5, 1.2, 1.4) is True


def test_spot_volume_is_converted_from_lots_before_kline_comparison():
    assert _spot_volume_in_kline_unit(12_345) == 1_234_500


def test_scanner_computes_real_five_minute_change_from_quote_history():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="000001", price=10.0, updated_at=datetime(2026, 8, 4, 9, 30))

    assert scanner._track_intraday_min5_change(spot, datetime(2026, 8, 4, 9, 30)) is None
    spot.price = 10.1
    assert scanner._track_intraday_min5_change(spot, datetime(2026, 8, 4, 9, 34)) is None
    spot.price = 10.2
    assert scanner._track_intraday_min5_change(spot, datetime(2026, 8, 4, 9, 35)) == pytest.approx(2.0)


def test_trade_confirmation_distinguishes_missing_five_minute_window_from_flat_price():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="000001", price=10.0)

    assert scanner.track_intraday_min5_change(
        spot,
        datetime(2026, 8, 4, 9, 30),
    ) is None
    assert scanner.track_intraday_min5_change(
        spot,
        datetime(2026, 8, 4, 9, 35),
    ) == 0.0


def test_stale_quote_cannot_clear_current_intraday_trackers():
    scanner = AnomalyScanner()
    active = SimpleNamespace(
        code="002792",
        price=34.0,
        prev_close=34.4,
        low=33.8,
        change_pct=-1.2,
        amount=1_000_000_000,
    )
    stale = SimpleNamespace(
        code="600530",
        price=3.85,
        prev_close=3.87,
        low=3.82,
        change_pct=-0.52,
        amount=10_000_000,
    )

    scanner._track_intraday_min5_change(active, datetime(2026, 8, 12, 10, 0))
    scanner._track_intraday_amount_flow(active, datetime(2026, 8, 12, 10, 0))
    scanner._track_intraday_reversal_state(active, datetime(2026, 8, 12, 10, 0))

    assert scanner._track_intraday_min5_change(stale, datetime(2026, 8, 10, 10, 1)) is None
    assert scanner._track_intraday_amount_flow(
        stale,
        datetime(2026, 8, 10, 10, 1),
    )["intraday_amount_interval_sec"] == 0
    assert scanner._track_intraday_reversal_state(
        stale,
        datetime(2026, 8, 10, 10, 1),
    ) == {}

    active.price = 34.68
    active.change_pct = 0.81
    active.amount = 1_008_000_000
    assert scanner._track_intraday_min5_change(
        active,
        datetime(2026, 8, 12, 10, 5),
    ) == pytest.approx(2.0)
    amount_flow = scanner._track_intraday_amount_flow(
        active,
        datetime(2026, 8, 12, 10, 0, 30),
    )
    reversal = scanner._track_intraday_reversal_state(
        active,
        datetime(2026, 8, 12, 10, 0, 30),
    )

    assert amount_flow["intraday_amount_delta"] == pytest.approx(8_000_000)
    assert amount_flow["intraday_amount_interval_sec"] == pytest.approx(30.0)
    assert reversal["crossed_from_underwater"] is True
    assert reversal["scan_change_pct"] == pytest.approx(2.01)


def test_out_of_order_same_day_quote_cannot_replace_latest_tracker_state():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002173",
        price=21.0,
        prev_close=20.8,
        low=20.7,
        change_pct=0.96,
        amount=500_000_000,
    )
    scanner._track_intraday_min5_change(spot, datetime(2026, 8, 12, 10, 0))
    scanner._track_intraday_amount_flow(spot, datetime(2026, 8, 12, 10, 0))
    scanner._track_intraday_reversal_state(spot, datetime(2026, 8, 12, 10, 0))

    spot.price = 20.9
    spot.amount = 490_000_000
    spot.change_pct = 0.48
    assert scanner._track_intraday_min5_change(
        spot,
        datetime(2026, 8, 12, 9, 59, 30),
    ) is None
    assert scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 12, 9, 59, 30),
    )["intraday_amount_interval_sec"] == 0
    assert scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 12, 9, 59, 30),
    ) == {}

    spot.price = 21.42
    spot.amount = 508_000_000
    spot.change_pct = 2.98
    assert scanner._track_intraday_min5_change(
        spot,
        datetime(2026, 8, 12, 10, 5),
    ) == pytest.approx(2.0)
    assert scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 12, 10, 0, 30),
    )["intraday_amount_delta"] == pytest.approx(8_000_000)
    assert scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 12, 10, 0, 30),
    )["previous_change_pct"] == pytest.approx(0.96)


def test_scanner_rejects_impossible_five_minute_snapshot_jump():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="000001", price=10.0)
    scanner._track_intraday_min5_change(spot, datetime(2026, 8, 4, 9, 30))
    spot.price = 12.0

    assert scanner._track_intraday_min5_change(spot, datetime(2026, 8, 4, 9, 35)) is None


def test_scanner_confirms_recent_amount_pace_instead_of_daily_volume_ratio():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="000001", amount=200_000_000)

    first = scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 10, 13, 30),
    )
    spot.amount = 205_000_000
    accelerated = scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 10, 13, 30, 30),
    )
    spot.amount = 205_500_000
    no_follow_through = scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 10, 13, 31),
    )

    assert first["intraday_amount_confirmed"] is False
    assert accelerated["intraday_amount_confirmed"] is True
    assert accelerated["intraday_amount_delta"] == pytest.approx(5_000_000)
    assert accelerated["intraday_amount_pace_ratio"] > 1.25
    assert no_follow_through["intraday_amount_confirmed"] is False


def test_scanner_tracks_true_rolling_60s_price_and_amount_window():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="002173", price=10.0, amount=200_000_000)

    first = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 0),
    )
    spot.price = 10.05
    spot.amount = 204_000_000
    middle = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 0, 30),
    )
    spot.price = 10.10
    spot.amount = 210_000_000
    result = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 1),
    )

    assert first["rolling_60s_confirmed"] is False
    assert middle["rolling_60s_interval_sec"] == 0
    assert result["rolling_60s_change_pct"] == pytest.approx(1.0)
    assert result["rolling_60s_interval_sec"] == pytest.approx(60.0)
    assert result["rolling_60s_amount_delta"] == pytest.approx(10_000_000)
    assert result["rolling_60s_amount_pace_ratio"] > 1.25
    assert result["rolling_60s_tier"] == "strong"
    assert result["rolling_60s_confirmed"] is True


def test_scanner_rolling_60s_rejects_price_move_without_incremental_amount():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="002173", price=10.0, amount=200_000_000)
    scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 0),
    )
    spot.price = 10.04
    spot.amount = 200_400_000
    result = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 1),
    )

    assert result["rolling_60s_change_pct"] == pytest.approx(0.4)
    assert result["rolling_60s_tier"] == "watch"
    assert result["rolling_60s_confirmed"] is False


def test_scanner_rolling_60s_uses_same_baseline_for_price_and_amount():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="002173", price=10.0, amount=200_000_000)
    scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 0),
    )
    spot.price = 10.03
    spot.amount = 203_000_000
    scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 0, 30),
    )
    spot.price = 10.06
    spot.amount = 207_000_000
    result = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 1),
    )

    assert result["rolling_60s_change_pct"] == pytest.approx(0.6)
    assert result["rolling_60s_amount_delta"] == pytest.approx(7_000_000)
    assert result["rolling_60s_tier"] == "medium"


def test_scanner_rolling_60s_does_not_bridge_lunch_break():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="002173", price=10.0, amount=200_000_000)
    scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 11, 29, 30),
    )
    spot.price = 10.10
    spot.amount = 210_000_000
    result = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 13, 0),
    )

    assert result["rolling_60s_interval_sec"] == 0
    assert result["rolling_60s_confirmed"] is False


def test_scanner_rolling_60s_rejects_out_of_order_quote_without_polluting_state():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(code="002173", price=10.0, amount=200_000_000)
    scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 0),
    )
    spot.price = 9.80
    spot.amount = 190_000_000
    stale = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 9, 59, 30),
    )
    spot.price = 10.10
    spot.amount = 210_000_000
    current = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 1),
    )

    assert stale["rolling_60s_interval_sec"] == 0
    assert current["rolling_60s_change_pct"] == pytest.approx(1.0)
    assert current["rolling_60s_amount_delta"] == pytest.approx(10_000_000)


def test_scanner_tracks_underwater_to_positive_transition_across_snapshots():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002580",
        price=9.88,
        prev_close=10.0,
        low=9.75,
        change_pct=-1.2,
    )

    first = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 30),
    )
    spot.price = 10.04
    spot.change_pct = 0.4
    second = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 31),
    )

    assert first["was_underwater"] is True
    assert second["crossed_from_underwater"] is True
    assert second["scan_change_pct"] == pytest.approx(1.6)
    assert second["min_change_pct"] <= -2.5


def test_scanner_does_not_arm_from_historical_day_low_and_latches_real_crossing():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002581",
        price=10.10,
        prev_close=10.0,
        low=9.40,
        change_pct=1.0,
    )

    late_start = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 30),
    )
    assert late_start["min_change_pct"] == pytest.approx(-6.0)
    assert late_start["observed_min_change_pct"] == pytest.approx(1.0)
    assert late_start["was_underwater"] is False
    assert late_start["crossed_from_underwater"] is False

    spot.price = 9.95
    spot.change_pct = -0.5
    armed = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 31),
    )
    assert armed["was_underwater"] is True
    assert armed["crossed_from_underwater"] is False

    spot.price = 10.04
    spot.change_pct = 0.4
    crossed = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 32),
    )
    assert crossed["crossed_from_underwater"] is True

    spot.price = 10.01
    spot.change_pct = 0.1
    held = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 33),
    )
    assert held["crossed_from_underwater"] is True

    spot.price = 9.96
    spot.change_pct = -0.4
    reset = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 34),
    )
    assert reset["crossed_from_underwater"] is False


def test_scanner_quote_batch_inbox_replays_a_b_c_by_source_time():
    scanner = AnomalyScanner()
    quote_times = [
        datetime(2026, 8, 31, 10, 0),
        datetime(2026, 8, 31, 10, 0, 30),
        datetime(2026, 8, 31, 10, 1),
    ]
    for index, quote_time in enumerate(quote_times):
        scanner.enqueue_quote_batch([{
            "code": "000001",
            "name": "批次保留测试",
            "price": 10.0 + index * 0.05,
            "prev_close": 10.0,
            "low": 9.95,
            "change_pct": index * 0.5,
            "amount": 200_000_000 + index * 5_000_000,
            "source_quote_at": quote_time,
            "received_at": quote_time + timedelta(milliseconds=50),
            "updated_at": quote_time + timedelta(milliseconds=100),
        }])

    drained = scanner._drain_quote_batch_inbox(
        blocked_stock_set=set(),
        today=date(2026, 8, 31),
        now=datetime.now(),
    )

    assert drained == 3
    assert [item[0] for item in scanner._quote_history["000001"]] == quote_times
    assert scanner._intraday_reversal_state["000001"]["observation_count"] == 3
    assert scanner._rolling_60s_history["000001"][-1][0] == quote_times[-1]

    # 数据库最新 C 快照随后再次进入事件检测时，不应重复增加观察帧。
    latest = SimpleNamespace(
        code="000001",
        price=10.10,
        prev_close=10.0,
        low=9.95,
        change_pct=1.0,
        amount=210_000_000,
        source_quote_at=quote_times[-1],
        received_at=quote_times[-1] + timedelta(milliseconds=50),
        updated_at=quote_times[-1] + timedelta(milliseconds=100),
    )
    state = scanner._track_intraday_reversal_state(latest)
    assert state["observation_count"] == 3
    assert state["scan_change_pct"] == pytest.approx(0.0)


def test_scanner_warns_on_underwater_volume_acceleration_before_turning_red():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002842",
        name="水下急拉股",
        price=9.65,
        prev_close=10.0,
        open=9.60,
        high=9.70,
        low=9.55,
        avg_price=9.62,
        change_pct=-3.5,
        min5_change=0.0,
        volume_ratio=1.10,
        turnover=3.0,
        amplitude=1.6,
        volume=200_000,
        amount=190_000_000,
        limit_up=11.0,
        limit_down=9.0,
        support_strength_score=75,
        orderbook_imbalance=0.24,
        bid_depth_5=58_000,
        ask_depth_5=30_000,
        withdrawal_ratio=0.03,
    )
    scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 30),
    )
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 10, 13, 30),
    )
    spot.price = 9.82
    spot.high = 9.84
    spot.avg_price = 9.78
    spot.change_pct = -1.8
    spot.min5_change = 1.1
    spot.volume_ratio = 1.40
    spot.amount = 198_000_000
    spot.amplitude = 3.0
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 10, 13, 31),
    )
    state = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 31),
    )
    sector_context = {
        "sector_factors": [{
            "sector_name": "有色金属",
            "fund_flow": 10.0,
            "change_pct": 1.6,
            "strength_score": 72,
            "limit_up_count": 3,
        }],
        "sector_components": [],
    }
    fund = {
        "main_net_inflow": 120_000_000,
        "main_net_inflow_pct": 4.6,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-10 13:31:00",
    }

    result = scanner._detect_underwater_reversal_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 10),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "水下放量急拉预警"
    assert detail["signal_type"] == "underwater_acceleration"
    assert detail["pre_reversal_alert"] is True

    spot.volume_ratio = 0.95
    assert scanner._detect_underwater_reversal_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 10),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    ) is None

    spot.volume_ratio = 1.40
    spot.withdrawal_ratio = 0.20
    assert scanner._detect_underwater_reversal_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 10),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    ) is None


def test_scanner_detects_positive_acceleration_from_two_to_four_pct():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002580",
        name="红盘加速股",
        price=10.178,
        prev_close=10.0,
        open=10.05,
        high=10.20,
        low=10.0,
        avg_price=10.10,
        change_pct=1.78,
        min5_change=0.0,
        volume_ratio=1.25,
        turnover=3.0,
        amplitude=2.0,
        volume=200_000,
        amount=200_000_000,
        limit_up=11.0,
        limit_down=9.0,
        support_strength_score=74,
        orderbook_imbalance=0.20,
        bid_depth_5=60_000,
        ask_depth_5=30_000,
        withdrawal_ratio=0.03,
    )
    scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 30),
    )
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 10, 13, 30),
    )
    spot.price = 10.40
    spot.high = 10.42
    spot.avg_price = 10.20
    spot.change_pct = 4.0
    spot.volume_ratio = 1.55
    spot.amount = 210_000_000
    spot.amplitude = 4.2
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 10, 13, 31),
    )
    state = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 10, 13, 31),
    )
    sector_context = {
        "sector_factors": [{
            "sector_name": "储能",
            "fund_flow": 12.0,
            "change_pct": 1.8,
            "strength_score": 76,
            "limit_up_count": 3,
        }],
    }
    fund = {
        "main_net_inflow": 150_000_000,
        "main_net_inflow_pct": 5.0,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-10 13:31:00",
    }

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 10),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "红盘放量二次加速预警"
    assert detail["signal_type"] == "positive_acceleration"
    assert detail["previous_change_pct"] == pytest.approx(1.78)
    assert detail["scan_change_pct"] == pytest.approx(2.22)
    assert detail["detection_pool_member"] is False
    assert detail["market_wide_detection"] is True
    assert detail["intraday_amount_confirmed"] is True
    assert detail["intraday_amount_pace_ratio"] > 1.25

    spot.volume_ratio = 1.0
    assert scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 10),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    ) is None

    spot.volume_ratio = 1.55
    spot._intraday_amount_flow = {
        "intraday_amount_confirmed": False,
        "intraday_amount_delta": 100_000,
        "intraday_amount_interval_sec": 30.0,
        "intraday_amount_pace_ratio": 0.2,
    }
    assert scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 10),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    ) is None


def test_scanner_detects_split_rolling_60s_rise_with_same_window_amount():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002173",
        name="滚动急拉股",
        price=10.0,
        prev_close=10.0,
        open=9.98,
        high=10.0,
        low=9.95,
        avg_price=9.99,
        change_pct=0.0,
        min5_change=0.0,
        volume_ratio=1.05,
        turnover=2.0,
        amplitude=0.5,
        volume=200_000,
        amount=200_000_000,
        limit_up=11.0,
        limit_down=9.0,
        support_strength_score=76,
        orderbook_imbalance=0.24,
        bid_depth_5=62_000,
        ask_depth_5=30_000,
        withdrawal_ratio=0.03,
    )
    scanner._track_rolling_60s_momentum(spot, datetime(2026, 8, 12, 10, 0))
    scanner._track_intraday_reversal_state(spot, datetime(2026, 8, 12, 10, 0))
    spot.price = 10.05
    spot.high = 10.05
    spot.avg_price = 10.01
    spot.change_pct = 0.5
    spot.amount = 204_000_000
    scanner._track_rolling_60s_momentum(spot, datetime(2026, 8, 12, 10, 0, 30))
    scanner._track_intraday_reversal_state(spot, datetime(2026, 8, 12, 10, 0, 30))
    spot.price = 10.10
    spot.high = 10.10
    spot.avg_price = 10.04
    spot.change_pct = 1.0
    spot.amount = 210_000_000
    rolling = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 12, 10, 1),
    )
    state = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 12, 10, 1),
    )
    sector_context = {
        "sector_factors": [{
            "sector_name": "储能",
            "fund_flow": 12.0,
            "change_pct": 1.8,
            "strength_score": 76,
            "limit_up_count": 3,
        }],
    }
    fund = {
        "main_net_inflow": 120_000_000,
        "main_net_inflow_pct": 4.5,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-12 10:01:00",
    }

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 12),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert rolling["rolling_60s_change_pct"] == pytest.approx(1.0)
    assert result is not None
    _score, label, detail = result
    assert label == "60秒放量强急拉"
    assert detail["positive_acceleration_rolling60"] is True
    assert detail["rolling_60s_tier"] == "strong"
    assert detail["rolling_60s_alert_pushable"] is True

def test_scanner_catches_volume_backed_jump_across_regular_acceleration_window():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002792",
        name="爆拉追赶股",
        price=10.48,
        prev_close=10.0,
        open=9.90,
        high=10.50,
        low=9.85,
        avg_price=10.15,
        change_pct=4.8,
        min5_change=0.0,
        volume_ratio=1.45,
        turnover=14.0,
        amplitude=6.5,
        volume=200_000,
        amount=600_000_000,
        limit_up=11.0,
        limit_down=9.0,
        support_strength_score=78,
        orderbook_imbalance=0.26,
        bid_depth_5=80_000,
        ask_depth_5=36_000,
        withdrawal_ratio=0.03,
    )
    scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 12, 13, 30),
    )
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 12, 13, 30),
    )
    spot.price = 10.78
    spot.high = 10.80
    spot.avg_price = 10.25
    spot.change_pct = 7.8
    spot.volume_ratio = 1.80
    spot.turnover = 19.0
    spot.amplitude = 10.8
    spot.amount = 620_000_000
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 12, 13, 30, 30),
    )
    state = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 12, 13, 30, 30),
    )
    sector_context = {
        "sector_factors": [{
            "sector_name": "通信设备",
            "fund_flow": 12.0,
            "change_pct": 2.1,
            "strength_score": 78,
            "limit_up_count": 3,
        }],
    }
    fund = {
        "main_net_inflow": 180_000_000,
        "main_net_inflow_pct": 6.0,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-12 13:30:30",
    }

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 12),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "盘中爆拉追赶预警"
    assert detail["positive_acceleration_catchup"] is True
    assert detail["scan_change_pct"] == pytest.approx(3.0)
    assert detail["intraday_amount_confirmed"] is True

    # 没有连续快照证明从常规窗口快速跨越，不能仅凭当前+7.8%补造信号。
    detail_state = dict(state)
    detail_state.update({"previous_change_pct": 7.2, "scan_change_pct": 0.6})
    assert scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=detail_state,
        today=date(2026, 8, 12),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    ) is None

    # 即使涨幅跨窗，最近一轮没有真实增量成交也必须拒绝，防止无量诱多。
    spot._intraday_amount_flow = {
        "intraday_amount_confirmed": False,
        "intraday_amount_delta": 200_000,
        "intraday_amount_interval_sec": 30.0,
        "intraday_amount_pace_ratio": 0.3,
    }
    assert scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 12),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    ) is None


def test_scanner_records_volume_backed_jump_directly_to_limit_up():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002173",
        name="极速封板股",
        price=10.78,
        prev_close=10.0,
        open=9.90,
        high=10.80,
        low=9.85,
        avg_price=10.25,
        change_pct=7.8,
        min5_change=0.0,
        volume_ratio=1.8,
        turnover=18.0,
        amplitude=10.8,
        volume=200_000,
        amount=600_000_000,
        limit_up=11.0,
        limit_down=9.0,
        support_strength_score=82,
        orderbook_imbalance=0.32,
        bid_depth_5=100_000,
        ask_depth_5=30_000,
        withdrawal_ratio=0.02,
    )
    scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 12, 13, 30),
    )
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 12, 13, 30),
    )
    spot.price = 11.0
    spot.high = 11.0
    spot.avg_price = 10.30
    spot.change_pct = 10.0
    spot.turnover = 19.5
    spot.amplitude = 11.7
    spot.amount = 625_000_000
    scanner._track_intraday_amount_flow(
        spot,
        datetime(2026, 8, 12, 13, 30, 30),
    )
    state = scanner._track_intraday_reversal_state(
        spot,
        datetime(2026, 8, 12, 13, 30, 30),
    )
    sector_context = {
        "sector_factors": [{
            "sector_name": "医疗服务",
            "fund_flow": 10.0,
            "change_pct": 2.0,
            "strength_score": 76,
            "limit_up_count": 3,
        }],
    }
    fund = {
        "main_net_inflow": 200_000_000,
        "main_net_inflow_pct": 8.0,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-12 13:30:30",
    }

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={},
        reversal_state=state,
        today=date(2026, 8, 12),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "极速封板确认"
    assert detail["positive_acceleration_limit_up"] is True
    assert detail["scan_change_pct"] == pytest.approx(2.2)
    assert detail["intraday_amount_confirmed"] is True


def test_scanner_warns_during_volume_backed_pre_limit_blind_band():
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="600001", name="涨停前加速股", price=10.78, prev_close=10.0,
        open=9.95, high=10.80, low=9.90, avg_price=10.25, change_pct=7.8,
        min5_change=0.0, volume_ratio=1.7, turnover=18.0, amplitude=10.8,
        volume=200_000, amount=600_000_000, limit_up=11.0, limit_down=9.0,
        support_strength_score=80, orderbook_imbalance=0.30,
        bid_depth_5=90_000, ask_depth_5=35_000, withdrawal_ratio=0.03,
    )
    scanner._track_intraday_reversal_state(spot, datetime(2026, 8, 14, 13, 30))
    scanner._track_intraday_amount_flow(spot, datetime(2026, 8, 14, 13, 30))
    spot.price = 10.85
    spot.high = 10.87
    spot.avg_price = 10.30
    spot.change_pct = 8.5
    spot.amount = 612_000_000
    spot.turnover = 19.0
    spot.amplitude = 11.2
    scanner._track_intraday_amount_flow(spot, datetime(2026, 8, 14, 13, 30, 30))
    state = scanner._track_intraday_reversal_state(
        spot, datetime(2026, 8, 14, 13, 30, 30)
    )
    sector_context = {
        "sector_factors": [{
            "sector_name": "通信设备", "fund_flow": 8.0, "change_pct": 2.0,
            "strength_score": 78, "limit_up_count": 3,
        }],
    }
    fund = {
        "main_net_inflow": 160_000_000, "main_net_inflow_pct": 5.0,
        "source": "eastmoney_main_fund", "is_stale": False,
        "as_of": "2026-08-14 13:30:30",
    }

    result = scanner._detect_positive_acceleration_setup(
        spot=spot, current_fund=fund, sector_context=sector_context,
        board_context={}, reversal_state=state, today=date(2026, 8, 14),
        fund_snapshot_is_stale=False, fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "涨停前放量加速预警"
    assert detail["positive_acceleration_pre_limit_up"] is True
    assert detail["positive_acceleration_limit_up"] is False
    assert detail["scan_change_pct"] == pytest.approx(0.7)

    spot._intraday_amount_flow = {
        "intraday_amount_confirmed": False,
        "intraday_amount_delta": 200_000,
        "intraday_amount_interval_sec": 30.0,
        "intraday_amount_pace_ratio": 0.2,
    }
    assert scanner._detect_positive_acceleration_setup(
        spot=spot, current_fund=fund, sector_context=sector_context,
        board_context={}, reversal_state=state, today=date(2026, 8, 14),
        fund_snapshot_is_stale=False, fund_source="eastmoney_main_fund",
    ) is None


def test_tencent_field_62_is_not_written_as_five_minute_change():
    fields = [""] * 88
    fields[1] = "字段测试股"
    fields[2] = "000001"
    fields[3] = "10.20"
    fields[4] = "10.00"
    fields[5] = "10.05"
    fields[6] = "100000"
    fields[32] = "2.00"
    fields[33] = "10.30"
    fields[34] = "9.95"
    fields[47] = "11.00"
    fields[48] = "9.00"
    fields[49] = "1.30"
    fields[51] = "10.12"
    fields[62] = "462.81"

    parsed = TencentSource()._parse_spot("000001", fields)

    assert parsed is not None
    assert parsed["min5_change"] is None


def test_tencent_spot_preserves_source_and_receive_clocks():
    fields = [""] * 88
    fields[1] = "时钟测试股"
    fields[2] = "000001"
    fields[3] = "10.20"
    fields[4] = "10.00"
    fields[5] = "10.05"
    fields[6] = "100000"
    fields[30] = "20260831102345"
    fields[32] = "2.00"
    fields[33] = "10.30"
    fields[34] = "9.95"
    received_at = datetime(2026, 8, 31, 10, 23, 45, 123456)

    parsed = TencentSource()._parse_spot(
        "000001",
        fields,
        received_at=received_at,
    )

    assert parsed is not None
    assert parsed["source_quote_at"] == datetime(2026, 8, 31, 10, 23, 45)
    assert parsed["received_at"] == received_at

    fields[30] = "无效时间"
    assert TencentSource()._parse_spot(
        "000001",
        fields,
        received_at=received_at,
    )["source_quote_at"] is None


def test_live_scan_freshness_prefers_source_clock_and_rejects_future_clock():
    now = datetime(2026, 8, 31, 10, 25)
    fresh = SimpleNamespace(
        source_quote_at=now - timedelta(seconds=30),
        received_at=now,
        updated_at=now,
    )
    repeated_stale = SimpleNamespace(
        source_quote_at=now - timedelta(minutes=5),
        received_at=now,
        updated_at=now,
    )
    legacy = SimpleNamespace(
        source_quote_at=None,
        received_at=now - timedelta(seconds=20),
        updated_at=now,
    )
    future = SimpleNamespace(
        source_quote_at=now + timedelta(seconds=30),
        received_at=now,
        updated_at=now,
    )

    assert _spot_is_fresh_for_live_scan(
        fresh, now=now, trade_date=now.date(),
    ) is True
    assert _spot_is_fresh_for_live_scan(
        repeated_stale, now=now, trade_date=now.date(),
    ) is False
    assert _spot_is_fresh_for_live_scan(
        legacy, now=now, trade_date=now.date(),
    ) is True
    assert _spot_is_fresh_for_live_scan(
        future, now=now, trade_date=now.date(),
    ) is False


def _sector_repair_fixture(*, change_pct: float = 4.5, sector_strength: float = 88.0):
    scanner = AnomalyScanner()
    closes = [18.0 - index * 0.04 for index in range(100)]
    closes.extend([14.0 - index * (4.0 / 19.0) for index in range(20)])
    highs = [value * 1.025 for value in closes]
    lows = [value * 0.975 for value in closes]
    volumes = [1_000_000.0] * 115 + [800_000.0] * 5
    tech = {
        "low_base_profile": {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes,
        }
    }
    spot = SimpleNamespace(
        code="002920",
        name="修复测试股",
        price=10.45,
        prev_close=10.0,
        open=10.02,
        high=10.50,
        low=9.95,
        avg_price=10.25,
        limit_up=11.0,
        limit_down=9.0,
        change_pct=change_pct,
        min5_change=0.35,
        volume_ratio=1.5,
        turnover=3.2,
        amplitude=5.5,
        amount=520_000_000,
        volume=500_000,
        net_profit_growth=28.0,
        pe_ttm=62.0,
        support_strength_score=72,
        orderbook_imbalance=0.18,
        bid_depth_5=180_000,
        ask_depth_5=100_000,
    )
    sector_context = {
        "sector_factors": [{
            "sector_code": "gn_cpo",
            "sector_name": "CPO",
            "change_pct": 4.8,
            "fund_flow": 42.0,
            "strength_score": sector_strength,
            "limit_up_count": 7,
            "peer_count": 5,
        }],
        "sector_components": [],
    }
    return scanner, spot, tech, sector_context


def test_sector_repair_detector_finds_oversold_chain_reversal_without_faking_ma_trend():
    scanner, spot, tech, sector_context = _sector_repair_fixture()

    result = scanner._detect_sector_repair_setup(
        spot=spot,
        tech=tech,
        sector_context=sector_context,
        board_context={"max_recent_consecutive_days": 0, "recent_limit_up_hits": 0},
        market_repair_context={
            "active": True,
            "up_ratio": 0.56,
            "average_change_pct": 0.9,
            "limit_up_count": 60,
        },
        today=date(2026, 8, 4),
    )

    assert result is not None
    score, label, detail = result
    assert score >= 82
    assert label == "强修复板块低位启动"
    assert detail["signal_type"] == "sector_repair_reversal"
    assert detail["ma_status"] == "repair_reversal"
    assert detail["return_20d"] <= -6
    assert detail["detection_pool_member"] is True


def test_sector_repair_detector_rejects_chasing_and_weak_sector_noise():
    scanner, spot, tech, sector_context = _sector_repair_fixture(change_pct=6.8)
    common = {
        "spot": spot,
        "tech": tech,
        "sector_context": sector_context,
        "board_context": {"max_recent_consecutive_days": 0, "recent_limit_up_hits": 0},
        "market_repair_context": {"active": True, "limit_up_count": 60},
        "today": date(2026, 8, 4),
    }
    assert scanner._detect_sector_repair_setup(**common) is None

    scanner, spot, tech, sector_context = _sector_repair_fixture(sector_strength=40)
    common.update({"spot": spot, "tech": tech, "sector_context": sector_context})
    assert scanner._detect_sector_repair_setup(**common) is None


def _old_hot_repair_fixture(*, change_pct: float = 8.0, with_board_memory: bool = True):
    early = [5.0, 5.5, 6.05] if with_board_memory else [5.0, 5.2, 5.4]
    climb_start = early[-1]
    climb = [climb_start + (12.0 - climb_start) * index / 37 for index in range(1, 38)]
    decline = [12.0 - 6.0 * index / 60 for index in range(1, 61)]
    closes = early + climb + decline
    highs = [value * 1.02 for value in closes]
    lows = [value * 0.98 for value in closes]
    volumes = [1_000_000.0] * len(closes)
    scanner = AnomalyScanner()
    spot = SimpleNamespace(
        code="002354",
        name="天娱数科",
        price=6.48,
        prev_close=6.0,
        open=6.02,
        high=6.50,
        low=5.95,
        avg_price=6.25,
        limit_up=6.60,
        limit_down=5.40,
        change_pct=change_pct,
        min5_change=0.55,
        volume_ratio=1.1,
        turnover=10.6,
        amplitude=9.2,
        amount=420_000_000,
        volume=6_000_000,
        support_strength_score=72,
        orderbook_imbalance=0.16,
        bid_depth_5=180_000,
        ask_depth_5=100_000,
    )
    tech = {
        "low_base_profile": {
            "closes": closes,
            "highs": highs,
            "lows": lows,
            "volumes": volumes,
        }
    }
    sector_context = {
        "sector_factors": [{
            "sector_code": "gn_ai",
            "sector_name": "AI应用",
            "change_pct": 1.8,
            "fund_flow": 8.0,
            "strength_score": 72,
            "limit_up_count": 3,
            "peer_count": 2,
        }],
        "sector_components": [],
    }
    return scanner, spot, tech, sector_context


def test_old_hot_oversold_repair_detects_first_strong_day_after_deep_decline():
    scanner, spot, tech, sector_context = _old_hot_repair_fixture()

    result = scanner._detect_old_hot_oversold_repair_setup(
        spot=spot,
        tech=tech,
        sector_context=sector_context,
        board_context={"recent_limit_up_hits": 0},
        today=date(2026, 7, 31),
    )

    assert result is not None
    score, label, detail = result
    assert score >= 84
    assert label == "旧高标超跌首日强修复"
    assert detail["signal_type"] == "old_hot_oversold_repair"
    assert detail["historical_board_like_count"] >= 2
    assert detail["return_20d"] <= -15
    assert detail["position_120"] <= 0.45
    assert detail["detection_pool_member"] is True


def test_old_hot_oversold_repair_rejects_missing_board_memory_and_chasing():
    scanner, spot, tech, sector_context = _old_hot_repair_fixture(with_board_memory=False)
    common = {
        "spot": spot,
        "tech": tech,
        "sector_context": sector_context,
        "board_context": {"recent_limit_up_hits": 0},
        "today": date(2026, 7, 31),
    }
    assert scanner._detect_old_hot_oversold_repair_setup(**common) is None

    scanner, spot, tech, sector_context = _old_hot_repair_fixture(change_pct=9.2)
    common.update({"spot": spot, "tech": tech, "sector_context": sector_context})
    assert scanner._detect_old_hot_oversold_repair_setup(**common) is None


def test_old_hot_oversold_repair_allows_strong_sector_extension_at_low_position():
    scanner, spot, tech, sector_context = _old_hot_repair_fixture(change_pct=8.9)
    spot.amplitude = 10.4
    sector_context["sector_factors"][0].update({
        "change_pct": 3.2,
        "strength_score": 88,
        "limit_up_count": 6,
        "peer_count": 5,
    })

    result = scanner._detect_old_hot_oversold_repair_setup(
        spot=spot,
        tech=tech,
        sector_context=sector_context,
        board_context={"recent_limit_up_hits": 0},
        today=date(2026, 8, 4),
    )

    assert result is not None
    _, _, detail = result
    assert detail["strong_extension_confirmed"] is True
    assert detail["position_120"] <= 0.30
    assert detail["old_hot_repair_driver"]["strength_score"] >= 80


def _trend_driver_fixture(board_days: int = 0):
    closes = [10 + index * 0.01 for index in range(44)]
    highs = [value * 1.015 for value in closes]
    lows = [value * 0.985 for value in closes]
    volumes = [1_000_000] * len(closes)
    closes.append(closes[-1] * 1.05)
    highs.append(closes[-1] * 1.01)
    lows.append(closes[-1] * 0.98)
    volumes.append(2_000_000)
    for index in range(15):
        closes.append(closes[-1] * (0.997 if index < 5 else 1.001))
        highs.append(closes[-1] * 1.01)
        lows.append(closes[-1] * 0.99)
        volumes.append(700_000)
    for _ in range(board_days):
        closes[-1] = closes[-2] * 1.10
        highs[-1] = closes[-1]
        lows[-1] = closes[-1]
    return closes, highs, lows, volumes


def test_trend_driver_finds_pre_breakout_setup_without_waiting_for_25pct_rally():
    closes, highs, lows, volumes = _trend_driver_fixture()

    result = _score_trend_driver_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=0.1,
        turnover=4.0,
        volume_ratio=0.8,
    )

    assert result["is_trend_driver_pattern"] is True
    assert result["trend_driver_label"] == "趋势驱动-平台蓄势"
    assert result["trend_driver_stats"]["setup_state"] == "armed_breakout"
    assert result["trend_driver_stats"]["resistance"] > result["trend_driver_stats"]["support"]


def test_trend_driver_rejects_multi_board_overheated_structure():
    closes, highs, lows, volumes = _trend_driver_fixture()
    closes[-2] = closes[-3] * 1.10
    highs[-2] = lows[-2] = closes[-2]
    closes[-1] = closes[-2] * 1.10
    highs[-1] = lows[-1] = closes[-1]

    result = _score_trend_driver_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=10.0,
        turnover=4.0,
        volume_ratio=0.8,
    )

    assert result["is_trend_driver_pattern"] is False
    assert any("涨停" in reason for reason in result["trend_driver_blockers"])


def _trend_driver_plan(setup_state: str) -> dict:
    return {
        "code": "000001",
        "name": "趋势测试股",
        "candidate_source": "trend_driver_setup",
        "candidate_source_label": "趋势驱动-首波回踩",
        "is_tradeable": True,
        "bull_score": 92,
        "main_wave_score": 92,
        "price": 10.2,
        "change_pct": 0.5,
        "strategies": [{"strategy_type": "avoid"}],
        "avoid_reasons": [],
        "risk_warnings": ["趋势驱动候选已入池，必须等待盘中确认"],
        "notes": [],
        "market_breadth": {
            "direct_buy_ok": True,
            "market_regime_label": "极强窗口",
        },
        "main_wave_stats": {
            "setup_state": setup_state,
            "support": 10.0,
            "resistance": 10.7,
            "support_gap_pct": 2.0,
            "distance_to_resistance_pct": -2.86,
            "range_10_pct": 9.0,
            "dry_up_ratio": 0.8,
            "position_60": 0.6,
            "board_like_count_20": 0,
            "max_drawdown_20": 8.0,
            "turnover": 3.0,
        },
        "resistance_detail": {"primary": 10.7, "secondary": 11.5},
    }


def test_trend_driver_pullback_plan_has_explicit_trigger_and_risk_levels():
    result = _apply_trend_driver_entry_protocol(_trend_driver_plan("armed_pullback"))

    strategy = result["strategies"][0]
    assert strategy["strategy_type"] == "trend_pullback_buy"
    assert strategy["entry_price_hint"].startswith("触发价")
    assert strategy["stop_loss"] < result["buy_signal"]["trigger_price"] < strategy["target_price"]
    assert strategy["risk_reward_ratio"] >= 1.5
    assert result["buy_signal"]["status"] == "waiting_intraday_confirmation"
    assert _next_day_plan_action_priority(result) == 2
    assert result["avoid_reasons"] == []


def test_trend_driver_breakout_plan_requires_low_position_and_volume_trigger():
    plan = _trend_driver_plan("armed_breakout")
    result = _apply_trend_driver_entry_protocol(plan)

    strategy = result["strategies"][0]
    assert strategy["strategy_type"] == "trend_breakout_buy"
    assert "量比1.3~2.5" in strategy["entry_condition"]
    assert result["buy_signal"]["label"] == "低位放量突破"

    high_position = _trend_driver_plan("armed_breakout")
    high_position["main_wave_stats"]["position_60"] = 0.9
    blocked = _apply_trend_driver_entry_protocol(high_position)
    assert blocked["strategies"][0]["strategy_type"] == "avoid"


def test_trend_driver_weak_market_and_fake_target_are_observe_only():
    weak_market = _trend_driver_plan("armed_pullback")
    weak_market["market_breadth"] = {
        "direct_buy_ok": False,
        "market_regime_label": "中性/震荡",
        "direct_buy_blockers": ["上涨家数占比不足88%"],
    }
    weak_result = _apply_trend_driver_entry_protocol(weak_market)
    assert weak_result["strategies"][0]["strategy_type"] == "watch"
    assert weak_result["strategies"][0]["position_ratio"] == "0"

    no_real_target = _trend_driver_plan("armed_breakout")
    no_real_target["resistance_detail"]["secondary"] = 0
    no_real_target["main_wave_stats"].pop("next_resistance", None)
    target_result = _apply_trend_driver_entry_protocol(no_real_target)
    assert target_result["strategies"][0]["strategy_type"] == "watch"
    assert "真实技术压力位" in target_result["invalidation"]


def test_trend_driver_bull_market_keeps_light_conditional_position():
    plan = _trend_driver_plan("armed_pullback")
    plan["market_breadth"] = {
        "direct_buy_ok": False,
        "market_regime": "bull",
        "market_regime_label": "牛市/强势",
        "direct_buy_blockers": ["上涨家数占比不足88%"],
    }

    result = _apply_trend_driver_entry_protocol(plan)

    assert result["strategies"][0]["strategy_type"] == "trend_pullback_buy"
    assert result["strategies"][0]["position_ratio"] == "1/6仓"
    assert result["strategies"][0]["position_before_trigger"] == 0
    assert result["buy_signal"]["position_after_trigger"] == "1/6仓"


def test_probe_wash_plan_exposes_conditional_low_entry_and_shape_levels():
    plan = _trend_driver_plan("armed_pullback")
    plan["candidate_source"] = "pre_board_probe_wash"
    plan["candidate_source_label"] = "连板前兆-试盘缩量洗盘"
    plan["main_wave_stats"].update({
        "position_120": 0.21,
        "dry_up_ratio": 0.36,
        "max_drawdown_20": 20.0,
        "probe_gain_pct": 5.71,
        "terminal_volume_ratio": 0.26,
        "days_since_probe": 6,
    })

    result = _apply_trend_driver_entry_protocol(plan)

    assert result["strategies"][0]["strategy_type"] == "trend_pullback_buy"
    assert result["strategies"][0]["strategy_label"] == "🟢 试盘洗盘回踩"
    assert "未回踩" in result["strategies"][0]["entry_condition"]
    assert result["buy_signal"]["pattern_source"] == "pre_board_probe_wash"
    assert result["buy_signal"]["terminal_volume_ratio"] == pytest.approx(0.26)
    assert result["support_detail"]["source"] == "试盘洗盘支撑"


def test_repair_followup_plan_requires_intraday_second_confirmation():
    plan = {
        "code": "000657",
        "name": "中钨高新",
        "candidate_source": "sector_repair_reversal",
        "is_tradeable": True,
        "price": 52.21,
        "strategies": [{"strategy_type": "aggressive"}],
        "market_breadth": {"direct_buy_ok": True},
        "risk_warnings": [],
        "notes": [],
        "main_wave_stats": {"support": 50.80, "resistance": 52.50},
        "resistance_detail": {"primary": 52.50, "secondary": 56.0},
    }

    result = _apply_repair_followup_entry_protocol(plan)

    strategy = result["strategies"][0]
    assert strategy["strategy_type"] == "repair_followup_buy"
    assert "放量站稳52.66" in strategy["entry_condition"]
    assert result["buy_signal"]["status"] == "waiting_intraday_confirmation"
    assert result["buy_signal"]["entry_mode"] == "breakout_reclaim"
    assert strategy["position_ratio"] == "1/4仓"
    assert strategy["stop_loss"] < result["buy_signal"]["trigger_price"]
    assert strategy["risk_reward_ratio"] >= 1.5
    assert result["support_detail"] == {
        "primary": 50.8,
        "secondary": 0,
        "source": "强修复形态支撑",
    }
    assert result["resistance_detail"] == {
        "primary": 52.5,
        "secondary": 0,
        "source": "强修复突破压力",
    }
    assert _next_day_plan_action_priority(result) == 2


def test_repair_followup_plan_downgrades_when_live_chasing_risk_is_exceeded():
    plan = {
        "code": "002463",
        "name": "沪电股份",
        "candidate_source": "sector_repair_reversal",
        "is_tradeable": True,
        "price": 119.0,
        "change_pct": 6.8,
        "strategies": [{"strategy_type": "aggressive"}],
        "market_breadth": {"direct_buy_ok": True},
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
        "main_wave_stats": {
            "support": 113.1,
            "resistance": 117.12,
            "current_amplitude": 8.5,
            "current_support_strength": 55,
            "current_orderbook_imbalance": -0.2,
        },
        "resistance_detail": {"primary": 117.12, "secondary": 124.0},
    }

    result = _apply_repair_followup_entry_protocol(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert "超过6.5%" in result["invalidation"]
    assert "振幅8.5%偏大" in result["invalidation"]
    assert "盘口承接55" in result["invalidation"]


def test_final_execution_gate_requires_market_sector_lifecycle_and_real_odds():
    plan = _trend_driver_plan("armed_pullback")
    plan = _apply_trend_driver_entry_protocol(plan)
    plan.update({
        "sector_resonance_name": "PCB",
        "sector_resonance_change": 1.8,
        "sector_resonance_fund": 20.0,
        "sector_resonance_score": 78.0,
        "sector_lifecycle_state": "accelerating",
    })
    assert _apply_next_day_execution_risk_gate(plan)["strategies"][0]["strategy_type"] == "trend_pullback_buy"

    plan["sector_lifecycle_state"] = "declining"
    blocked = _apply_next_day_execution_risk_gate(plan)
    assert blocked["strategies"][0]["strategy_type"] == "watch"
    assert "退潮" in blocked["invalidation"]


def test_final_execution_gate_does_not_use_non_extreme_bull_market_as_veto():
    plan = _trend_driver_plan("armed_pullback")
    plan["market_breadth"] = {
        "direct_buy_ok": False,
        "market_regime": "bull",
        "market_regime_label": "牛市/强势",
        "direct_buy_blockers": ["上涨家数占比不足88%"],
    }
    plan = _apply_trend_driver_entry_protocol(plan)
    plan.update({
        "sector_resonance_name": "PCB",
        "sector_resonance_change": 1.8,
        "sector_resonance_fund": 20.0,
        "sector_resonance_score": 78.0,
        "sector_lifecycle_state": "accelerating",
    })

    result = _apply_next_day_execution_risk_gate(plan)

    assert result["strategies"][0]["strategy_type"] == "trend_pullback_buy"
    assert result["strategies"][0]["position_ratio"] == "1/6仓"


def test_final_execution_gate_rejects_position_without_intraday_buy_signal():
    plan = _trend_driver_plan("armed_pullback")
    plan.update({
        "strategies": [{
            "strategy_type": "trend",
            "position_ratio": "1/2仓",
            "target_price_pct": 8.0,
            "risk_reward_ratio": 2.0,
        }],
        "sector_resonance_name": "PCB",
        "sector_resonance_change": 1.8,
        "sector_resonance_fund": 20.0,
        "sector_resonance_score": 78.0,
        "sector_lifecycle_state": "accelerating",
    })
    plan.pop("buy_signal", None)

    result = _apply_next_day_execution_risk_gate(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert "缺少个股级盘中触发信号" in result["invalidation"]


def test_low_expectation_pool_requires_rolling_60s_incremental_volume():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "002166",
        "name": "莱茵生物",
        "score": 86.4,
        "label": "低位启动-趋势临界确认",
        "candidate_source": "low_expectation_trend_watch",
        "setup_state": "armed_breakout",
        "support": 6.82,
        "resistance": 6.98,
        "main_wave_stats": {
            "setup_state": "armed_breakout",
            "position_60": 0.359,
            "board_like_count_20": 0,
            "max_drawdown_20": 4.15,
        },
    }])
    spot = SimpleNamespace(
        code="002166", name="莱茵生物", price=7.01, prev_close=6.94,
        open=6.93, high=7.02, low=6.86, avg_price=6.96,
        limit_up=7.63, limit_down=6.25, change_pct=1.01,
        min5_change=0.55, volume_ratio=1.45, turnover=2.8,
        amplitude=2.3, amount=220_000_000,
        support_strength_score=75, orderbook_imbalance=0.18,
        bid_depth_5=230_000, ask_depth_5=110_000,
        withdrawal_ratio=0.02,
        _intraday_amount_flow={
            "intraday_amount_confirmed": True,
            "intraday_amount_delta": 12_000_000,
            "intraday_amount_interval_sec": 30.0,
            "intraday_amount_pace_ratio": 1.8,
        },
        _rolling_60s_momentum={
            "rolling_60s_confirmed": True,
            "rolling_60s_path_confirmed": True,
            "rolling_60s_change_pct": 0.65,
            "rolling_60s_amount_delta": 9_000_000,
            "rolling_60s_amount_pace_ratio": 1.8,
        },
    )
    fund = {
        "main_net_inflow": 100_000_000,
        "main_net_inflow_pct": 4.2,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-21",
    }
    sector = {
        "sector_factors": [{
            "sector_name": "合成生物",
            "sector_type": "concept",
            "fund_flow": 8.0,
            "change_pct": 1.6,
            "strength_score": 78,
            "limit_up_count": 3,
            "is_sector_leader": True,
        }],
    }
    kwargs = {
        "spot": spot,
        "current_fund": fund,
        "sector_context": sector,
        "board_context": {"max_recent_consecutive_days": 0},
        "today": date(2026, 8, 21),
    }

    result = scanner._detect_trend_driver_watch_setup(**kwargs)
    assert result is not None
    assert result[0] == "low_absorb"
    assert result[2] == "低位预期支撑回收"
    assert result[3]["rolling_60s_confirmed"] is True
    assert result[3]["watchlist_label"] == "低位预期趋势确认池"

    spot._rolling_60s_momentum = {"rolling_60s_confirmed": False}
    assert scanner._detect_trend_driver_watch_setup(**kwargs) is None


def test_dynamic_trend_pool_only_triggers_after_intraday_breakout_confirmation():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "002827",
        "name": "高争民爆",
        "score": 86,
        "label": "连板前兆-120日低位蓄势",
        "candidate_source": "pre_board_long_base",
        "setup_state": "armed_breakout",
        "support": 24.8,
        "resistance": 25.0,
        "main_wave_stats": {
            "long_cycle_regime": "virgin_low_base",
            "long_cycle_regime_label": "250日低位首波底座",
            "quality_setup_score": 78.0,
            "shape_ready": True,
        },
    }])
    spot = SimpleNamespace(
        code="002827", name="高争民爆", price=25.3, prev_close=24.6,
        open=24.8, high=25.35, low=24.75, avg_price=25.05,
        limit_up=27.06, limit_down=22.14, change_pct=2.85,
        min5_change=0.8, volume_ratio=1.8, turnover=5.0, amplitude=2.4,
        support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )
    spot._rolling_60s_momentum = {
        "rolling_60s_confirmed": True,
        "rolling_60s_change_pct": 0.65,
        "rolling_60s_interval_sec": 60.0,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
        "rolling_60s_tier": "medium",
    }

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 120_000_000,
            "main_net_inflow_pct": 5.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-07-31",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 5.0,
                "change_pct": 1.5,
                "strength_score": 75,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 7, 31),
    )

    assert result is not None
    event_type, _, _, detail = result
    assert event_type == "breakthrough"
    assert detail["signal_type"] == "trend_driver_breakthrough"
    assert detail["watchlist_label"] == "动态低位连板观察池"
    assert detail["pre_board_watchlist"] is True
    assert detail["long_cycle_regime"] == "virgin_low_base"
    assert detail["rolling_60s_confirmed"] is True
    assert _is_low_base_watchlist_anomaly({"detail": detail}) is True

    spot._rolling_60s_momentum = {"rolling_60s_confirmed": False}
    assert scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 120_000_000,
            "main_net_inflow_pct": 5.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-07-31",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 5.0,
                "change_pct": 1.5,
                "strength_score": 75,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 7, 31),
    ) is None


def test_sector_core_laggard_requires_exact_hot_sector_and_rolling_incremental_amount():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "000998",
        "name": "隆平高科",
        "score": 92,
        "label": "主线核心补涨-量价确认",
        "candidate_source": "sector_core_laggard",
        "setup_state": "armed_sector_core_laggard",
        "support": 11.8,
        "resistance": 12.2,
        "main_wave_stats": {
            "sector_code": "GN_CORN",
            "sector_name": "玉米",
            "hot_sector_membership_count": 4,
            "business_aligned_sector_count": 3,
        },
    }])
    spot = SimpleNamespace(
        code="000998", name="隆平高科", price=12.1, prev_close=12.0,
        open=11.9, high=12.15, low=11.85, avg_price=12.05,
        limit_up=13.2, limit_down=10.8, change_pct=0.83,
        min5_change=0.6, volume_ratio=1.8, turnover=3.5, amplitude=2.5,
        amount=180_000_000, support_strength_score=75,
        orderbook_imbalance=0.16, bid_depth_5=200_000, ask_depth_5=110_000,
    )
    spot._rolling_60s_momentum = {
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.65,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
    }
    sector_context = {
        "sector_factors": [{
            "sector_code": "GN_CORN",
            "sector_name": "玉米",
            "sector_type": "concept",
            "source": "pywencai",
            "fund_flow": 10.2,
            "change_pct": 7.4,
            "strength_score": 90,
            "limit_up_count": 12,
            "lifecycle_state": "emerging",
            "lifecycle_score": 82,
            "active_days": 2,
        }],
    }
    fund = {
        "main_net_inflow": 90_000_000,
        "main_net_inflow_pct": 4.5,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-18 14:40:00",
    }

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 18),
    )

    assert result is not None
    event_type, _, _, detail = result
    assert event_type == "breakthrough"
    assert detail["signal_type"] == "sector_core_laggard_ignition"
    assert detail["sector_core_laggard_confirmed"] is True
    assert detail["rolling_60s_confirmed"] is True
    assert _is_low_base_watchlist_anomaly({"detail": detail}) is True

    spot._rolling_60s_momentum = {"rolling_60s_confirmed": False}
    assert scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund=fund,
        sector_context=sector_context,
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 18),
    ) is None


def test_second_board_relay_plan_and_intraday_signal_require_two_stage_confirmation():
    plan = {
        "code": "000001",
        "name": "冲二板测试",
        "candidate_source": "second_board_relay",
        "is_tradeable": True,
        "strategies": [{"strategy_type": "aggressive"}],
        "risk_warnings": [],
        "notes": [],
        "main_wave_stats": {
            "board_count": 1,
            "relay_pool_ready": True,
            "relay_quality_score": 82,
            "limit_up_time": "09:58:00",
            "break_count": 0,
            "turnover": 6.0,
            "is_one_word": False,
            "same_reason_limit_up_count": 3,
        },
    }

    result = _apply_event_limit_up_protocol(plan)

    assert result["strategies"][0]["position_ratio"] == "0"
    assert result["buy_signal"]["type"] == "second_board_relay_confirmation"
    assert result["event_catalyst"]["relay_ready"] is True


def test_diversified_plan_reserves_second_board_relay_lane():
    rows = [
        {
            "code": f"600{i:03d}",
            "candidate_source": "pre_board_probe_wash",
            "plan_priority_score": 90 - i,
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": True,
        }
        for i in range(20)
    ] + [
        {
            "code": "000001",
            "candidate_source": "second_board_relay",
            "plan_priority_score": 50,
            "strategies": [{"strategy_type": "event_relay_watch"}],
            "is_tradeable": True,
        }
    ]

    selected = _diversified_actionable_top(rows, 10)

    assert any(item["candidate_source"] == "second_board_relay" for item in selected)


def test_event_first_board_plan_waits_for_auction_and_turnover_confirmation():
    plan = {
        "code": "000537",
        "name": "绿发电力",
        "candidate_source": "event_first_board",
        "candidate_source_label": "重大利好首板接力观察",
        "is_tradeable": True,
        "strategies": [{"strategy_type": "strong_get_stronger"}],
        "risk_warnings": [],
        "notes": [],
        "sector_lifecycle_state": "emerging",
        "sector_resonance": "强共振",
        "main_wave_stats": {
            "board_count": 1,
            "news_event_grade": "hard",
            "news_event_type": "major_project",
            "news_catalyst_score": 78,
            "news_title": "关于投资天津滨海新区46万千瓦风电项目的公告",
            "news_source": "cninfo",
            "limit_up_time": "09:38:00",
            "break_count": 1,
            "turnover": 6.2,
            "is_one_word": False,
            "same_reason_limit_up_count": 4,
            "return_20d": 18.2,
            "board_like_count_20": 1,
        },
    }

    result = _apply_event_limit_up_protocol(plan)

    assert result["strategies"][0]["strategy_type"] == "event_relay_watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert result["event_catalyst"]["relay_ready"] is True
    assert result["buy_signal"]["status"] == "waiting_auction_and_turnover_confirmation"
    assert result["buy_signal"]["auction_change_max"] == pytest.approx(3.5)


def test_event_high_board_rejects_old_news_and_one_word_chase():
    plan = {
        "code": "600721",
        "name": "百花医药",
        "candidate_source": "event_high_board",
        "is_tradeable": True,
        "strategies": [{"strategy_type": "strong_get_stronger"}],
        "risk_warnings": [],
        "notes": [],
        "main_wave_stats": {
            "board_count": 5,
            "news_event_grade": "hard",
            "news_catalyst_score": 82,
            "news_fresh_after_trade_close": False,
            "limit_up_time": "09:25:00",
            "break_count": 0,
            "turnover": 0.8,
            "is_one_word": True,
        },
    }

    result = _apply_event_limit_up_protocol(plan)

    assert result["event_catalyst"]["relay_ready"] is False
    assert "buy_signal" not in result
    assert result["strategies"][0]["entry_price_hint"] == "不追板"


@pytest.mark.parametrize(
    ("candidate_source", "expected_signal_type"),
    [
        ("event_first_board", "event_relay_confirmation"),
        ("second_board_relay", "second_board_relay_confirmation"),
    ],
)
def test_event_relay_dynamic_pool_only_fires_after_full_second_confirmation(
    candidate_source,
    expected_signal_type,
):
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "000537",
        "name": "绿发电力",
        "score": 82,
        "label": "重大利好首板接力观察",
        "candidate_source": candidate_source,
        "setup_state": "armed_event_relay",
        "support": 10.83,
        "resistance": 11.38,
        "main_wave_stats": {
            "board_count": 1,
            "news_event_grade": "hard",
            "news_catalyst_score": 82,
            "news_title": "关于投资天津滨海新区46万千瓦风电项目的公告",
            "relay_quality_score": 86,
            "relay_pool_ready": True,
            "is_one_word": False,
            "break_count": 1,
            "turnover": 6.0,
        },
    }])
    spot = SimpleNamespace(
        code="000537", name="绿发电力", price=11.32, prev_close=11.00,
        open=11.22, high=11.35, low=11.18, avg_price=11.25,
        limit_up=12.10, limit_down=9.90, change_pct=2.91,
        min5_change=0.55, volume_ratio=1.6, turnover=4.5, amplitude=1.55,
        support_strength_score=75, orderbook_imbalance=0.16,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )
    spot._rolling_60s_momentum = {
        "rolling_60s_confirmed": True,
        "rolling_60s_change_pct": 0.65,
        "rolling_60s_interval_sec": 60.0,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
        "rolling_60s_tier": "medium",
    }

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 100_000_000,
            "main_net_inflow_pct": 4.5,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-08-11",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 8.0,
                "change_pct": 1.6,
                "strength_score": 75,
                "limit_up_count": 3,
            }],
        },
        board_context={"max_recent_consecutive_days": 1},
        today=date(2026, 8, 11),
    )

    assert result is not None
    event_type, _, _, detail = result
    assert event_type == "breakthrough"
    assert detail["signal_type"] == expected_signal_type
    assert detail["event_relay_confirmed"] is True
    if candidate_source == "second_board_relay":
        assert detail["relay_quality_score"] == pytest.approx(86)
    assert _is_low_base_watchlist_anomaly({"detail": detail}) is True

    # 仅有竞价/VWAP/板块/盘口还不够；缺少滚动60秒或盘中增量成交时，
    # 不得把无量拉升当作冲二板买点推送。
    spot._rolling_60s_momentum = {}
    no_incremental_amount_result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 100_000_000,
            "main_net_inflow_pct": 4.5,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-08-11",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 8.0,
                "change_pct": 1.6,
                "strength_score": 75,
                "limit_up_count": 3,
            }],
        },
        board_context={"max_recent_consecutive_days": 1},
        today=date(2026, 8, 11),
    )
    assert no_incremental_amount_result is None


def test_dragon_scanner_separates_independent_leader_recognition_and_tradability():
    results = DragonHeadScanner().scan_sector("pw_ai", "AI教育", [
        {
            "code": "003032", "name": "传智教育", "change_pct": 10.0,
            "main_net_inflow": 100_000_000, "consecutive_days": 1,
            "turnover": 0.8, "seal_amount": 180_000_000,
            "volume_ratio": 0.9, "is_limit_up": True, "limit_up_time": "09:25:00",
            "break_count": 0, "is_one_word": True, "seal_quality_score": 88,
            "event_grade": "hard", "event_score": 82,
            "return_20d": 12.0, "position_120": 0.42,
        },
        {
            "code": "002607", "name": "中公教育", "change_pct": 2.4,
            "main_net_inflow": 30_000_000, "consecutive_days": 1,
            "turnover": 4.0, "volume_ratio": 1.5, "is_limit_up": False,
            "amount": 200_000_000, "price": 4.12, "avg_price": 4.08,
            "return_20d": 5.0, "position_120": 0.30,
        },
        {
            "code": "300359", "name": "全通教育", "change_pct": 1.2,
            "main_net_inflow": 10_000_000, "consecutive_days": 1,
            "turnover": 3.0, "volume_ratio": 1.2, "is_limit_up": False,
            "amount": 160_000_000, "price": 6.20, "avg_price": 6.16,
            "return_20d": 8.0, "position_120": 0.35,
        },
    ])

    leader = next(item for item in results if item.code == "003032")
    assert leader.link_role == "leader_a"
    assert leader.leader_type == "independent_event"
    assert leader.recognition_score >= 72
    assert leader.tradability_score < 50
    assert not [item for item in results if item.link_role == "follower_b"]


def test_dragon_scanner_does_not_label_closed_limit_up_as_high_tradability():
    result = DragonHeadScanner().scan_sector("pw_power", "电力", [{
        "code": "600001", "name": "换手首板", "change_pct": 10.0,
        "main_net_inflow": 80_000_000, "consecutive_days": 1,
        "turnover": 8.0, "volume_ratio": 2.0, "is_limit_up": True,
        "limit_up_time": "10:00:00", "seal_quality_score": 75,
        "return_20d": 15.0, "position_120": 0.50,
    }])[0]

    assert result.tradability_score <= 70
    assert "已封板仅辨识不追价" in result.reasons


def test_dragon_scanner_can_recognize_non_limit_trend_leader_without_marking_it_chaseable():
    results = DragonHeadScanner().scan_sector("pw_grid", "电网设备", [
        {
            "code": "601700", "name": "风范股份", "change_pct": 1.2,
            "main_net_inflow": 95_000_000, "consecutive_days": 1,
            "turnover": 5.0, "volume_ratio": 1.35, "is_limit_up": False,
            "amount": 520_000_000, "price": 12.8,
            "ma5": 12.5, "ma10": 11.9, "ma20": 11.2,
            "ma20_slope_5d": 1.2, "return_5d": 6.8,
            "return_20d": 42.0, "position_120": 0.72,
        },
        {
            "code": "002953", "name": "日丰股份", "change_pct": 10.0,
            "main_net_inflow": 35_000_000, "consecutive_days": 1,
            "turnover": 9.0, "volume_ratio": 2.1, "is_limit_up": True,
            "limit_up_time": "13:52:00", "seal_quality_score": 55,
            "return_5d": 3.0, "return_20d": 8.0, "position_120": 0.55,
        },
        {
            "code": "600312", "name": "平高电气", "change_pct": -0.4,
            "main_net_inflow": -8_000_000, "consecutive_days": 1,
            "turnover": 2.0, "volume_ratio": 0.8, "is_limit_up": False,
            "price": 19.0, "ma5": 19.1, "ma10": 19.2, "ma20": 19.3,
            "ma20_slope_5d": -0.2, "return_5d": -1.0,
            "return_20d": 2.0, "position_120": 0.50,
        },
    ])

    leader = next(item for item in results if item.code == "601700")
    assert leader.trend_leadership_score >= 82
    assert leader.link_role == "leader_a"
    assert leader.leader_type == "trend_leader"
    assert "趋势龙头" in "".join(leader.reasons)


def test_dragon_scanner_only_maps_b_after_real_sector_diffusion():
    results = DragonHeadScanner().scan_sector("pw_food", "食品饮料", [
        {
            "code": "605179", "name": "一鸣食品", "change_pct": 10.0,
            "main_net_inflow": 120_000_000, "consecutive_days": 2,
            "turnover": 5.0, "seal_amount": 160_000_000,
            "volume_ratio": 1.8, "is_limit_up": True, "limit_up_time": "09:30:00",
            "limit_up_reason": "乳品",
            "break_count": 0, "seal_quality_score": 86,
            "return_20d": 18.0, "position_120": 0.50,
            "primary_industry": "食品饮料-饮料制造-乳品",
        },
        {
            "code": "605337", "name": "李子园", "change_pct": 10.0,
            "main_net_inflow": 60_000_000, "consecutive_days": 1,
            "turnover": 6.0, "seal_amount": 80_000_000,
            "volume_ratio": 1.6, "is_limit_up": True, "limit_up_time": "10:10:00",
            "limit_up_reason": "乳品",
            "break_count": 1, "seal_quality_score": 74,
            "return_20d": 12.0, "position_120": 0.45,
            "primary_industry": "食品饮料-饮料制造-乳品",
        },
        {
            "code": "002946", "name": "新乳业", "change_pct": 2.2,
            "main_net_inflow": 30_000_000, "consecutive_days": 1,
            "turnover": 4.0, "volume_ratio": 1.5, "is_limit_up": False,
            "amount": 220_000_000, "price": 15.20, "avg_price": 15.12,
            "return_20d": 5.0, "position_120": 0.30,
            "return_5d": 3.0, "position_20": 0.70,
            "ma5": 15.0, "ma10": 14.8, "ma20": 14.5,
            "ma20_slope_5d": 0.8,
            "primary_industry": "食品饮料-饮料制造-乳品",
        },
    ], allow_linkage=True)

    follower = next(item for item in results if item.code == "002946")
    assert follower.link_role == "follower_b"
    assert follower.leader_code == "605179"
    assert follower.linkage_score >= 72
    assert follower.business_relevance_score >= 76
    assert follower.follower_shape_score >= 68
    assert follower.theme_alignment_score >= 75


def test_dragon_scanner_rejects_cross_industry_concept_and_weak_b_shape():
    scanner = DragonHeadScanner()
    stocks = [
        {
            "code": "600664", "name": "哈药股份", "change_pct": 10.0,
            "main_net_inflow": 120_000_000, "consecutive_days": 2,
            "turnover": 5.0, "volume_ratio": 1.6, "is_limit_up": True,
            "limit_up_time": "09:32:00", "seal_quality_score": 84,
            "limit_up_reason": "医药商业",
            "return_20d": 12.0, "position_120": 0.55,
            "primary_industry": "医药生物-化学制药-化学制剂",
        },
        {
            "code": "600329", "name": "达仁堂", "change_pct": 10.0,
            "main_net_inflow": 60_000_000, "consecutive_days": 1,
            "turnover": 5.0, "volume_ratio": 1.5, "is_limit_up": True,
            "limit_up_time": "10:02:00", "seal_quality_score": 75,
            "limit_up_reason": "中药",
            "return_20d": 8.0, "position_120": 0.48,
            "primary_industry": "医药生物-中药-中药Ⅲ",
        },
        {
            "code": "600315", "name": "上海家化", "change_pct": 4.4,
            "main_net_inflow": 50_000_000, "consecutive_days": 1,
            "turnover": 3.0, "volume_ratio": 1.6, "is_limit_up": False,
            "amount": 280_000_000, "price": 18.02, "avg_price": 17.65,
            "return_20d": 3.0, "return_5d": 4.0, "position_120": 0.45,
            "position_20": 0.75, "ma5": 17.8, "ma10": 17.5, "ma20": 17.2,
            "ma20_slope_5d": 0.6,
            "primary_industry": "美容护理-美容护理-化妆品",
        },
        {
            "code": "002727", "name": "一心堂", "change_pct": 2.0,
            "main_net_inflow": 40_000_000, "consecutive_days": 1,
            "turnover": 3.0, "volume_ratio": 1.4, "is_limit_up": False,
            "amount": 240_000_000, "price": 11.0, "avg_price": 10.9,
            "return_20d": -4.0, "return_5d": -6.0, "position_120": 0.30,
            "position_20": 0.25, "ma5": 11.2, "ma10": 11.4, "ma20": 11.6,
            "ma20_slope_5d": -1.2,
            "primary_industry": "医药生物-医药商业-药店",
        },
    ]
    results = scanner.scan_sector("pw_birth", "三胎概念", stocks, allow_linkage=True)

    assert next(item for item in results if item.code == "600315").link_role == ""
    assert next(item for item in results if item.code == "002727").link_role == ""
    assert scanner._business_relevance_score(stocks[0], stocks[3], "三胎概念") == 0
    assert scanner._follower_shape_score(stocks[3]) < 68


def test_leader_linkage_rejects_generic_or_unconfirmed_sector_tags():
    confirmed = SimpleNamespace(
        strength_score=72,
        change_pct=2.1,
        fund_flow=6.5,
        limit_up_count=3,
    )
    concept_meta = {
        "source": "pywencai",
        "sector_type": "concept",
        "sector_name": "人形机器人",
    }

    assert _is_tradeable_linkage_sector(concept_meta, confirmed) is True
    assert _is_tradeable_linkage_sector(
        {**concept_meta, "sector_name": "2026中报预增"},
        confirmed,
    ) is False
    assert _is_tradeable_linkage_sector(
        concept_meta,
        SimpleNamespace(
            strength_score=72,
            change_pct=2.1,
            fund_flow=-1.0,
            limit_up_count=3,
        ),
    ) is False
    assert _is_tradeable_linkage_sector(
        {**concept_meta, "source": "sw"},
        confirmed,
    ) is False


def test_leader_linkage_plan_is_zero_position_until_intraday_confirmation():
    plan = {
        "code": "002607",
        "name": "中公教育",
        "candidate_source": "leader_linkage_follow",
        "is_tradeable": True,
        "strategies": [{"strategy_type": "aggressive"}],
        "risk_warnings": [],
        "notes": [],
        "main_wave_stats": {
            "leader_code": "003032",
            "leader_name": "传智教育",
            "leader_recognition_score": 86,
            "leader_type": "independent_event",
            "linkage_score": 72,
            "business_relevance_score": 92,
            "follower_shape_score": 82,
            "theme_alignment_score": 90,
            "leader_driver_reason": "AI教育",
            "link_sector_name": "AI教育",
            "return_20d": 8.0,
        },
    }

    result = _apply_leader_linkage_protocol(plan)

    assert result["strategies"][0]["strategy_type"] == "leader_linkage_watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert result["leader_linkage"]["relay_ready"] is True
    assert result["buy_signal"]["status"] == "waiting_leader_and_follower_confirmation"


def test_leader_linkage_dynamic_pool_requires_a_stable_and_b_turning_strong():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "002607",
        "name": "中公教育",
        "score": 72,
        "label": "看传智教育做B",
        "candidate_source": "leader_linkage_follow",
        "setup_state": "armed_leader_linkage",
        "support": 3.90,
        "resistance": 4.30,
        "main_wave_stats": {
            "leader_code": "003032",
            "leader_name": "传智教育",
            "leader_recognition_score": 86,
            "linkage_score": 72,
            "business_relevance_score": 92,
            "follower_shape_score": 82,
            "theme_alignment_score": 90,
            "leader_driver_reason": "AI教育",
            "link_sector_name": "AI教育",
        },
    }])
    follower = SimpleNamespace(
        code="002607", name="中公教育", price=4.12, prev_close=4.02,
        open=4.02, high=4.14, low=3.99, avg_price=4.08,
        limit_up=4.42, limit_down=3.62, change_pct=2.49,
        min5_change=0.55, volume_ratio=1.6, turnover=4.5, amplitude=3.7,
        support_strength_score=74, orderbook_imbalance=0.15,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )
    leader = SimpleNamespace(
        code="003032", name="传智教育", price=11.00, prev_close=10.00,
        high=11.00, avg_price=10.82, limit_up=11.00, change_pct=10.0,
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=follower,
        leader_spot=leader,
        current_fund={
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 4.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-08-11 10:05:00",
        },
        sector_context={
            "sector_factors": [{
                "sector_name": "AI教育",
                "fund_flow": 6.0,
                "change_pct": 1.5,
                "strength_score": 75,
                "limit_up_count": 1,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 11),
    )

    assert result is not None
    event_type, _, _, detail = result
    assert event_type == "breakthrough"
    assert detail["signal_type"] == "leader_linkage_confirmation"
    assert detail["leader_linkage_confirmed"] is True
    assert detail["leader_code"] == "003032"
    assert _is_low_base_watchlist_anomaly({"detail": detail}) is True


def test_probe_breakout_source_is_in_dynamic_pool_and_keeps_specific_label():
    assert _DYNAMIC_TREND_POOL_SOURCE_PRIORITY["pre_board_probe_breakout"] > 0
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "000903",
        "name": "大位科技",
        "score": 90,
        "label": "连板前兆-试盘突破",
        "candidate_source": "pre_board_probe_breakout",
        "setup_state": "armed_breakout",
        "support": 7.60,
        "resistance": 7.99,
        "main_wave_stats": {
            "long_cycle_regime": "virgin_low_base",
            "long_cycle_regime_label": "250日低位首波底座",
            "quality_setup_score": 76.0,
            "shape_ready": True,
        },
    }])
    spot = SimpleNamespace(
        code="000903", name="大位科技", price=8.08, prev_close=7.80,
        open=7.84, high=8.10, low=7.78, avg_price=7.98,
        limit_up=8.58, limit_down=7.02, change_pct=3.59,
        min5_change=0.7, volume_ratio=1.7, turnover=7.0, amplitude=4.1,
        support_strength_score=72, orderbook_imbalance=0.14,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )
    spot._rolling_60s_momentum = {
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.70,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
    }

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 100_000_000,
            "main_net_inflow_pct": 4.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-08-04",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 6.0,
                "change_pct": 1.2,
                "strength_score": 70,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 4),
    )

    assert result is not None
    _, _, _, detail = result
    assert detail["watchlist_label"] == "动态试盘突破观察池"
    assert detail["pre_board_watchlist"] is True


def test_probe_wash_source_triggers_specific_intraday_breakout_event():
    assert _DYNAMIC_TREND_POOL_SOURCE_PRIORITY["pre_board_probe_wash"] > 0
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "003032",
        "name": "传智教育",
        "score": 96,
        "label": "连板前兆-试盘缩量洗盘",
        "candidate_source": "pre_board_probe_wash",
        "setup_state": "armed_pullback",
        "support": 5.33,
        "resistance": 5.63,
        "main_wave_stats": {
            "long_cycle_regime": "historical_board_reset",
            "long_cycle_regime_label": "历史涨停记忆重置",
            "quality_setup_score": 80.0,
            "shape_ready": True,
        },
    }])
    spot = SimpleNamespace(
        code="003032", name="传智教育", price=5.68, prev_close=5.47,
        open=5.48, high=5.70, low=5.42, avg_price=5.58,
        limit_up=6.02, limit_down=4.92, change_pct=3.84,
        min5_change=0.8, volume_ratio=1.7, turnover=6.0, amplitude=5.1,
        support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )
    spot._rolling_60s_momentum = {
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.80,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
    }

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 100_000_000,
            "main_net_inflow_pct": 4.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-07-27",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 6.0,
                "change_pct": 1.2,
                "strength_score": 70,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 7, 27),
    )

    assert result is not None
    event_type, _, description, detail = result
    assert event_type == "breakthrough"
    assert description == "试盘缩量洗盘放量突破"
    assert detail["signal_type"] == "trend_driver_breakthrough"
    assert detail["trend_setup_source"] == "pre_board_probe_wash"
    assert detail["watchlist_label"] == "动态试盘洗盘观察池"
    assert detail["pre_board_watchlist"] is True


def test_momentum_shakeout_source_enters_pre_board_monitor_and_breakout_event():
    assert _DYNAMIC_TREND_POOL_SOURCE_PRIORITY["pre_board_momentum_shakeout"] > 0
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "002842",
        "name": "翔鹭钨业",
        "score": 91,
        "label": "中期动量-缩量洗盘",
        "candidate_source": "pre_board_momentum_shakeout",
        "setup_state": "armed_pullback",
        "support": 38.31,
        "resistance": 42.79,
        "main_wave_stats": {
            "quality_setup_score": 78.0,
            "shape_ready": True,
        },
    }])
    spot = SimpleNamespace(
        code="002842", name="翔鹭钨业", price=42.90, prev_close=41.20,
        open=41.30, high=43.00, low=41.10, avg_price=42.30,
        limit_up=45.32, limit_down=37.08, change_pct=4.13,
        min5_change=0.8, volume_ratio=1.8, turnover=7.0, amplitude=4.6,
        support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )
    spot._rolling_60s_momentum = {
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.80,
        "rolling_60s_amount_delta": 8_000_000,
        "rolling_60s_amount_pace_ratio": 1.8,
    }

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 100_000_000,
            "main_net_inflow_pct": 4.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-08-18",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 6.0,
                "change_pct": 1.2,
                "strength_score": 70,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 18),
    )

    assert result is not None
    event_type, _, _, detail = result
    assert event_type == "breakthrough"
    assert detail["signal_type"] == "trend_driver_breakthrough"
    assert detail["trend_setup_source"] == "pre_board_momentum_shakeout"
    assert detail["watchlist_label"] == "中期动量缩量洗盘池"
    assert detail["pre_board_watchlist"] is True


def test_dynamic_pool_reserves_capacity_for_probe_and_long_base_sources():
    candidates = [
        {
            "code": f"00{index:04d}",
            "candidate_source": "trend_driver_setup",
            "main_wave_score": 96,
            "total_score": 90,
        }
        for index in range(120)
    ]
    candidates.extend([
        {
            "code": f"60{index:04d}",
            "candidate_source": "pre_board_probe_breakout",
            "main_wave_score": 90,
            "total_score": 82,
        }
        for index in range(5)
    ])
    candidates.extend([
        {
            "code": f"30{index:04d}",
            "candidate_source": "pre_board_long_base",
            "main_wave_score": 86,
            "total_score": 78,
        }
        for index in range(5)
    ])

    selected = _select_dynamic_trend_pool_candidates(candidates, limit=100)
    sources = [str(item.get("candidate_source") or "") for item in selected]

    assert len(selected) == 100
    assert sources.count("pre_board_probe_breakout") == 5
    assert sources.count("pre_board_long_base") == 5


def test_dynamic_pool_prefers_time_sensitive_probe_over_broad_primary_pattern():
    selected = _select_dynamic_trend_pool_candidates([{
        "code": "000903",
        "name": "试盘股",
        "candidate_source": "trend_driver_setup",
        "candidate_source_label": "趋势驱动-平台蓄势",
        "main_wave_score": 96,
        "main_wave_stats": {"support": 7.5, "resistance": 8.2},
        "dynamic_pool_source": "pre_board_probe_breakout",
        "dynamic_pool_label": "连板前兆-试盘突破",
        "dynamic_pool_score": 90,
        "dynamic_pool_stats": {"support": 7.6, "resistance": 7.99, "setup_state": "armed_breakout"},
    }], limit=100)

    assert selected[0]["candidate_source"] == "pre_board_probe_breakout"
    assert selected[0]["candidate_source_label"] == "连板前兆-试盘突破"
    assert selected[0]["main_wave_stats"]["resistance"] == 7.99


def test_dynamic_pool_reserves_tenbagger_pullback_watch():
    candidates = [
        {
            "code": f"00{index:04d}",
            "candidate_source": "trend_driver_setup",
            "main_wave_score": 96,
            "total_score": 90,
        }
        for index in range(120)
    ]
    candidates.append({
        "code": "603162",
        "candidate_source": "tenbagger_pullback_watch",
        "candidate_source_label": "牛股潜质-回踩等待",
        "main_wave_score": 74,
        "total_score": 78,
        "main_wave_stats": {
            "setup_state": "armed_pullback",
            "support": 11.28,
            "resistance": 12.90,
            "tenbagger_score": 78.5,
        },
    })

    selected = _select_dynamic_trend_pool_candidates(candidates, limit=100)

    assert any(
        item["code"] == "603162"
        and item["candidate_source"] == "tenbagger_pullback_watch"
        for item in selected
    )


def test_dynamic_pool_reserves_time_sensitive_main_wave_pullback_shapes():
    candidates = [
        {
            "code": f"00{index:04d}",
            "candidate_source": "second_wave_pattern",
            "main_wave_score": 120 - index / 10,
            "total_score": 95,
        }
        for index in range(140)
    ]
    candidates.extend([
        {
            "code": f"60{index:04d}",
            "candidate_source": "trend_main_wave_pattern",
            "main_wave_score": 88 - index,
            "total_score": 86,
            "dynamic_pool_source": "main_wave_pullback_pattern",
            "dynamic_pool_label": "主升浪缩量十字星",
            "dynamic_pool_score": 82 - index,
            "dynamic_pool_stats": {
                "support": 12.0,
                "resistance": 13.0,
                "pullback_shape_ready": True,
                "pullback_anchor_low": 12.0,
                "pullback_prior_gain_pct": 8.0,
            },
        }
        for index in range(4)
    ])

    selected = _select_dynamic_trend_pool_candidates(candidates, limit=100)
    pullback_codes = {
        str(item.get("code") or "")
        for item in selected
        if item.get("candidate_source") == "main_wave_pullback_pattern"
    }

    assert len(selected) == 100
    assert pullback_codes == {"600000", "600001", "600002", "600003"}


def test_dynamic_pool_reserves_platform_and_second_wave_sources():
    candidates = [
        {
            "code": f"00{index:04d}",
            "candidate_source": "trend_driver_setup",
            "main_wave_score": 96,
            "total_score": 90,
        }
        for index in range(120)
    ]
    candidates.extend([
        {
            "code": "600556",
            "candidate_source": "platform_breakout_pattern",
            "candidate_source_label": "平台突破主升跟踪",
            "main_wave_score": 92,
            "total_score": 88,
            "main_wave_stats": {"support": 4.86, "resistance": 5.04},
        },
        {
            "code": "002842",
            "candidate_source": "second_wave_pattern",
            "candidate_source_label": "断板二波主升跟踪",
            "main_wave_score": 86,
            "total_score": 84,
            "main_wave_stats": {"support": 29.80, "resistance": 31.60},
        },
        {
            "code": "000815",
            "candidate_source": "second_wave_reset_watch",
            "candidate_source_label": "高标下杀二波准备",
            "main_wave_score": 82,
            "total_score": 80,
            "main_wave_stats": {
                "support": 13.70,
                "resistance": 17.37,
                "setup_state": "armed_second_wave_reset",
            },
        },
    ])

    selected = _select_dynamic_trend_pool_candidates(candidates, limit=100)
    selected_codes = {item["code"] for item in selected}

    assert {"600556", "002842", "000815"}.issubset(selected_codes)
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool(selected)
    assert scanner.dynamic_trend_pool["600556"]["source"] == "platform_breakout_pattern"
    assert scanner.dynamic_trend_pool["002842"]["source"] == "second_wave_pattern"
    assert scanner.dynamic_trend_pool["000815"]["source"] == "second_wave_reset_watch"


def test_dynamic_pool_reserves_main_wave_sources_for_green_open_reclaim():
    candidates = [
        {
            "code": f"00{index:04d}",
            "candidate_source": "trend_driver_setup",
            "main_wave_score": 96,
            "total_score": 90,
            "main_wave_stats": {
                "setup_state": "armed_pullback",
                "support": 10.0,
                "resistance": 11.0,
            },
        }
        for index in range(120)
    ]
    candidates.extend([
        {
            "code": "000636",
            "name": "风华高科",
            "candidate_source": "main_wave_pattern",
            "candidate_source_label": "多板主升浪跟踪",
            "main_wave_score": 94,
            "total_score": 94,
            "main_wave_stats": {
                "setup_state": "armed_main_wave_pullback",
                "support": 59.92,
                "resistance": 67.87,
                "pct_12": 48.85,
                "near_high_ratio": 0.97,
            },
        },
        {
            "code": "600001",
            "candidate_source": "trend_main_wave_pattern",
            "main_wave_score": 88,
            "total_score": 88,
            "main_wave_stats": {
                "setup_state": "armed_main_wave_pullback",
                "support": 12.0,
                "resistance": 13.5,
                "pct_20": 31.0,
                "near_high_ratio": 0.94,
            },
        },
        *[
            {
                "code": f"60{index:04d}",
                "candidate_source": "main_wave_pattern",
                "main_wave_score": 110 - index,
                "total_score": 95,
                "main_wave_stats": {
                    "setup_state": "armed_main_wave_pullback",
                    "support": 12.0,
                    "resistance": 13.5,
                },
            }
            for index in range(15)
        ],
    ])

    selected = _select_dynamic_trend_pool_candidates(candidates, limit=100)
    selected_codes = {item["code"] for item in selected}

    assert {"000636", "600001"}.issubset(selected_codes)
    assert sum(
        1 for item in selected if item.get("candidate_source") == "main_wave_pattern"
    ) >= 16


@pytest.mark.asyncio
async def test_main_wave_loader_keeps_current_main_wave_as_dynamic_pool_source(
    tenbagger_session,
):
    db_session = tenbagger_session
    closes = [10.0 + index * 0.08 for index in range(48)] + [
        13.8, 15.18, 16.70, 16.10, 17.20, 18.92,
        19.60, 20.30, 21.10, 22.00, 22.70, 23.40,
    ]
    for index, close in enumerate(closes):
        db_session.add(StockKline(
            code="000636",
            trade_date=date(2026, 5, 15) + timedelta(days=index),
            open=close * 0.99,
            close=close,
            high=close * 1.02,
            low=close * 0.98,
            volume=10_000_000 + index * 200_000,
            amount=200_000_000,
            turnover=5.0,
            change_pct=3.0,
            source="test",
        ))
    db_session.add(StockSpot(
        code="000636",
        name="风华高科",
        price=23.40,
        prev_close=22.70,
        open=23.00,
        high=23.80,
        low=22.80,
        # 竞价阶段尚未成交也不能让昨晚日K候选从预案池消失。
        volume=0,
        amount=0,
        change_pct=3.08,
        turnover=8.0,
        volume_ratio=1.2,
        circ_market_cap=200.0,
    ))
    await db_session.commit()

    target_date = date(2026, 5, 15) + timedelta(days=len(closes) - 1)
    candidates = await _load_main_wave_plan_candidates(
        db_session,
        target_date,
        set(),
        limit=80,
    )
    item = next(row for row in candidates if row["code"] == "000636")

    assert item["dynamic_pool_source"] == "main_wave_pattern"
    assert item["dynamic_pool_stats"]["setup_state"] == "armed_main_wave_pullback"
    assert item["dynamic_pool_stats"]["support"] > 0


def test_main_wave_pool_detects_green_open_selloff_reclaim_with_volume():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "000636",
        "name": "风华高科",
        "score": 94,
        "label": "多板主升浪跟踪",
        "candidate_source": "main_wave_pattern",
        "setup_state": "armed_main_wave_pullback",
        "support": 59.92,
        "resistance": 67.87,
        "main_wave_stats": {
            "setup_state": "armed_main_wave_pullback",
            "support": 59.92,
            "resistance": 67.87,
            "pct_12": 48.85,
            "near_high_ratio": 0.97,
        },
    }])
    spot = SimpleNamespace(
        code="000636", name="风华高科", price=64.55, prev_close=65.85,
        open=63.02, high=64.72, low=62.60, avg_price=64.35,
        limit_up=72.44, limit_down=59.27, change_pct=-1.97,
        min5_change=0.72, volume_ratio=1.18, turnover=6.8, amplitude=3.22,
        support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=220_000, ask_depth_5=120_000,
        withdrawal_ratio=0.03,
        _intraday_amount_flow={
            "intraday_amount_confirmed": True,
            "intraday_amount_delta": 8_000_000,
            "intraday_amount_interval_sec": 30.0,
            "intraday_amount_pace_ratio": 1.8,
        },
        _rolling_60s_momentum={
            "rolling_60s_confirmed": True,
            "rolling_60s_change_pct": 0.82,
            "rolling_60s_interval_sec": 60.0,
            "rolling_60s_amount_delta": 15_000_000,
            "rolling_60s_amount_pace_ratio": 1.7,
            "rolling_60s_tier": "medium",
        },
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 120_000_000,
            "main_net_inflow_pct": 3.2,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-08-12 09:42:00",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 18.0,
                "change_pct": 1.5,
                "strength_score": 76,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 2},
        today=date(2026, 8, 12),
        reversal_state={"scan_change_pct": 0.70},
    )

    assert result is not None
    event_type, _, label, detail = result
    assert event_type == "low_absorb"
    assert label == "主升浪绿开下杀回收"
    assert detail["signal_type"] == "main_wave_green_open_reclaim"
    assert detail["main_wave_green_open_reclaim_rolling60"] is True
    assert detail["open_change_pct"] == pytest.approx(-4.30, abs=0.02)


def test_strong_trend_deep_wash_is_watch_memory_not_direct_pullback():
    opens = [
        4.52, 4.60, 4.47, 4.51, 5.00, 4.88, 4.84, 4.82, 4.81, 4.80,
        4.96, 5.82, 6.51, 7.10, 6.87, 6.30, 6.60, 6.91, 7.46, 7.04,
        6.64, 7.55, 7.18, 6.90, 7.53,
    ]
    closes = [
        4.65, 4.51, 4.50, 4.95, 4.86, 4.92, 4.81, 4.86, 4.80, 4.89,
        5.38, 5.92, 6.51, 7.16, 6.44, 7.08, 6.90, 7.59, 7.10, 6.56,
        7.22, 7.31, 6.82, 7.50, 7.01,
    ]
    highs = [
        4.76, 4.62, 4.57, 4.95, 5.00, 4.97, 4.89, 4.92, 4.94, 4.91,
        5.38, 5.92, 6.51, 7.16, 6.94, 7.08, 7.10, 7.59, 7.65, 7.10,
        7.22, 7.94, 7.45, 7.50, 7.57,
    ]
    lows = [
        4.52, 4.34, 4.40, 4.50, 4.78, 4.82, 4.79, 4.75, 4.75, 4.78,
        4.95, 5.50, 5.94, 6.90, 6.44, 6.30, 6.59, 6.75, 7.06, 6.39,
        6.30, 7.26, 6.60, 6.90, 6.76,
    ]
    volumes = [
        247432, 260756, 151794, 499974, 499419, 377210, 237773, 270136,
        235087, 224109, 359551, 609978, 1752006, 1804372, 982140,
        2712904, 2070578, 1636706, 2075273, 2010982, 1858814, 2808999,
        1880326, 825474, 2065417,
    ]

    result = _main_wave_pullback_shape_stats(
        opens=opens,
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
    )

    assert result["pullback_shape_type"] == "deep_wash_first_bearish"
    assert result["pullback_quality_tier"] == "reclaim"
    assert result["pullback_requires_next_session_reclaim"] is True
    assert result["pullback_reclaim_price"] == pytest.approx(7.01)
    assert result["pullback_volume_dry_up"] is False
    assert result["pullback_technical_persistence_score"] >= 66
    assert result["pullback_technical_persistence_tier"] in {"medium", "high"}


def test_deep_wash_first_bearish_pushes_only_after_reclaim_with_rolling_volume():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "601700",
        "name": "风范股份",
        "score": 82,
        "label": "主升首阴深洗观察",
        "candidate_source": "main_wave_pullback_pattern",
        "setup_state": "armed_main_wave_shape_pullback",
        "support": 6.76,
        "resistance": 7.57,
        "main_wave_stats": {
            "setup_state": "armed_main_wave_shape_pullback",
            "support": 6.76,
            "resistance": 7.57,
            "pct_20": 42.48,
            "near_high_ratio": 0.883,
            "pullback_shape_ready": True,
            "pullback_shape_type": "deep_wash_first_bearish",
            "pullback_shape_label": "主升首阴深洗观察",
            "pullback_anchor_low": 6.76,
            "pullback_anchor_close": 7.01,
            "pullback_reclaim_price": 7.01,
            "pullback_requires_next_session_reclaim": True,
            "pullback_quality_tier": "reclaim",
        },
    }])
    spot = SimpleNamespace(
        code="601700", name="风范股份", price=7.03, prev_close=7.01,
        open=7.04, high=7.06, low=6.70, avg_price=6.98,
        limit_up=7.71, limit_down=6.31, change_pct=0.29,
        min5_change=0.62, volume_ratio=1.35, turnover=8.2, amplitude=5.14,
        amount=420_000_000, support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=220_000, ask_depth_5=120_000, withdrawal_ratio=0.03,
        _intraday_amount_flow={"intraday_amount_confirmed": True},
        _rolling_60s_momentum={
            "rolling_60s_confirmed": True,
            "rolling_60s_change_pct": 0.62,
            "rolling_60s_amount_delta": 12_000_000,
            "rolling_60s_amount_pace_ratio": 1.6,
        },
    )
    kwargs = {
        "spot": spot,
        "current_fund": {
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 3.2,
            "super_net_inflow": 12_000_000,
            "big_net_inflow": 16_000_000,
            "source": "eastmoney_main_fund",
            "is_stale": False,
        },
        "sector_context": {"sector_factors": [{
            "sector_name": "特高压",
            "sector_type": "concept",
            "source": "pywencai",
            "fund_flow": 12.0,
            "change_pct": 1.6,
            "strength_score": 78,
            "limit_up_count": 3,
            "lifecycle_state": "emerging",
            "lifecycle_score": 68,
            "active_days": 2,
            "is_sector_leader": True,
        }]},
        "board_context": {"max_recent_consecutive_days": 1},
        "today": date(2026, 8, 24),
        "reversal_state": {"scan_change_pct": 0.60},
    }

    result = scanner._detect_trend_driver_watch_setup(**kwargs)
    assert result is not None
    assert result[0] == "low_absorb"
    assert result[2] == "主升首阴深洗收回"
    assert result[3]["main_wave_deep_wash_reclaim_confirmed"] is True
    assert result[3]["main_wave_strict_pullback_confirmed"] is False

    spot.price = 6.96
    spot.change_pct = -0.71
    assert scanner._detect_trend_driver_watch_setup(**kwargs) is None


def test_deep_wash_hard_event_can_replace_weak_sector_but_not_rolling_or_orderbook():
    scanner = AnomalyScanner()
    setup = {
        "code": "601700",
        "name": "风范股份",
        "score": 88,
        "label": "主升首阴深洗观察",
        "candidate_source": "main_wave_pullback_pattern",
        "setup_state": "armed_main_wave_shape_pullback",
        "support": 6.76,
        "resistance": 7.57,
        "main_wave_stats": {
            "setup_state": "armed_main_wave_shape_pullback",
            "support": 6.76,
            "resistance": 7.57,
            "pct_20": 42.48,
            "near_high_ratio": 0.883,
            "pullback_shape_ready": True,
            "pullback_shape_type": "deep_wash_first_bearish",
            "pullback_shape_label": "主升首阴深洗观察",
            "pullback_anchor_low": 6.76,
            "pullback_anchor_close": 7.01,
            "pullback_reclaim_price": 7.01,
            "pullback_requires_next_session_reclaim": True,
            "pullback_quality_tier": "reclaim",
            "news_event_grade": "hard",
            "news_catalyst_score": 86,
            "news_title": "关于南方电网项目中标的公告",
        },
    }
    scanner.update_dynamic_trend_pool([setup])
    spot = SimpleNamespace(
        code="601700", name="风范股份", price=7.03, prev_close=7.01,
        open=6.88, high=7.06, low=6.70, avg_price=6.98,
        limit_up=7.71, limit_down=6.31, change_pct=0.29,
        min5_change=0.62, volume_ratio=1.35, turnover=8.2, amplitude=5.14,
        amount=420_000_000, support_strength_score=76, orderbook_imbalance=0.16,
        bid_depth_5=220_000, ask_depth_5=120_000, withdrawal_ratio=0.03,
        _intraday_amount_flow={"intraday_amount_confirmed": False},
        _rolling_60s_momentum={
            "rolling_60s_confirmed": True,
            "rolling_60s_change_pct": 0.62,
            "rolling_60s_amount_delta": 12_000_000,
            "rolling_60s_amount_pace_ratio": 1.6,
        },
    )
    kwargs = {
        "spot": spot,
        "current_fund": {"source": "stock_spot", "is_stale": True},
        "sector_context": {"sector_factors": []},
        "board_context": {"max_recent_consecutive_days": 1},
        "today": date(2026, 8, 24),
        "reversal_state": {"scan_change_pct": 0.60},
    }

    result = scanner._detect_trend_driver_watch_setup(**kwargs)
    assert result is not None
    assert result[3]["main_wave_direct_hard_catalyst_confirmed"] is True

    spot.orderbook_imbalance = -0.05
    spot.bid_depth_5 = 100_000
    spot.ask_depth_5 = 150_000
    assert scanner._detect_trend_driver_watch_setup(**kwargs) is None


def test_tenbagger_pullback_requires_rolling_60s_volume_and_stays_below_chase_window():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "603162",
        "name": "海通发展",
        "score": 74,
        "label": "牛股潜质-回踩等待",
        "candidate_source": "tenbagger_pullback_watch",
        "setup_state": "armed_pullback",
        "support": 11.28,
        "resistance": 12.90,
        "main_wave_stats": {
            "setup_state": "armed_pullback",
            "support": 11.28,
            "resistance": 12.90,
            "tenbagger_score": 78.5,
            "potential_quality_score": 72.0,
        },
    }])
    spot = SimpleNamespace(
        code="603162", name="海通发展", price=11.36, prev_close=11.42,
        open=11.24, high=11.39, low=11.24, avg_price=11.34,
        limit_up=12.56, limit_down=10.28, change_pct=-0.53,
        min5_change=0.52, volume_ratio=1.25, turnover=4.6, amplitude=1.31,
        amount=280_000_000, support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=220_000, ask_depth_5=120_000, withdrawal_ratio=0.03,
        _intraday_amount_flow={"intraday_amount_confirmed": True},
        _rolling_60s_momentum={
            "rolling_60s_confirmed": True,
            "rolling_60s_path_confirmed": True,
            "rolling_60s_change_pct": 0.46,
            "rolling_60s_interval_sec": 60.0,
            "rolling_60s_amount_delta": 8_000_000,
            "rolling_60s_amount_pace_ratio": 1.6,
            "rolling_60s_tier": "watch",
        },
    )
    kwargs = {
        "spot": spot,
        "current_fund": {
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 3.2,
            "source": "eastmoney_main_fund",
            "is_stale": False,
        },
        "sector_context": {
            "sector_factors": [{
                "fund_flow": 18.0,
                "change_pct": 1.5,
                "strength_score": 76,
                "limit_up_count": 2,
            }],
        },
        "board_context": {},
        "today": date(2026, 8, 14),
    }

    result = scanner._detect_trend_driver_watch_setup(**kwargs)
    assert result is not None
    assert result[0] == "low_absorb"
    assert result[2] == "牛股潜质支撑回收"
    assert result[3]["tenbagger_pullback_confirmed"] is True

    spot._rolling_60s_momentum = {"rolling_60s_confirmed": False}
    assert scanner._detect_trend_driver_watch_setup(**kwargs) is None

    spot._rolling_60s_momentum = {
        "rolling_60s_confirmed": True,
        "rolling_60s_path_confirmed": True,
        "rolling_60s_change_pct": 0.60,
    }
    spot.change_pct = 2.1
    assert scanner._detect_trend_driver_watch_setup(**kwargs) is None


def test_main_wave_green_open_selloff_does_not_trigger_without_reclaim_volume():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "000636",
        "name": "风华高科",
        "score": 94,
        "label": "多板主升浪跟踪",
        "candidate_source": "main_wave_pattern",
        "setup_state": "armed_main_wave_pullback",
        "support": 59.92,
        "resistance": 67.87,
        "main_wave_stats": {
            "pct_12": 48.85,
            "near_high_ratio": 0.97,
            "support": 59.92,
            "resistance": 67.87,
        },
    }])
    spot = SimpleNamespace(
        code="000636", name="风华高科", price=62.75, prev_close=65.85,
        open=63.02, high=63.10, low=62.60, avg_price=62.90,
        limit_up=72.44, limit_down=59.27, change_pct=-4.71,
        min5_change=-0.65, volume_ratio=1.35, turnover=3.0, amplitude=0.76,
        support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=220_000, ask_depth_5=120_000,
        withdrawal_ratio=0.03,
        _intraday_amount_flow={
            "intraday_amount_confirmed": False,
            "intraday_amount_delta": 500_000,
            "intraday_amount_interval_sec": 30.0,
            "intraday_amount_pace_ratio": 0.8,
        },
        _rolling_60s_momentum={"rolling_60s_confirmed": False},
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 120_000_000,
            "main_net_inflow_pct": 3.2,
            "source": "eastmoney_main_fund",
            "is_stale": False,
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 18.0,
                "change_pct": 1.5,
                "strength_score": 76,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 2},
        today=date(2026, 8, 12),
        reversal_state={"scan_change_pct": -0.5},
    )

    assert result is None


def test_main_wave_first_bearish_low_reclaim_requires_rolling_60s_volume():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "600001",
        "name": "主升首阴样本",
        "score": 90,
        "label": "主升浪首阴",
        "candidate_source": "main_wave_pullback_pattern",
        "setup_state": "armed_main_wave_shape_pullback",
        "support": 12.70,
        "resistance": 13.40,
        "main_wave_stats": {
            "setup_state": "armed_main_wave_shape_pullback",
            "support": 12.70,
            "resistance": 13.40,
            "pct_20": 22.8,
            "near_high_ratio": 0.96,
            "pullback_shape_ready": True,
            "pullback_shape_type": "first_bearish",
            "pullback_shape_label": "主升浪首阴",
            "pullback_anchor_low": 12.70,
            "pullback_quality_tier": "strict",
        },
    }])
    spot = SimpleNamespace(
        code="600001", name="主升首阴样本", price=12.83, prev_close=12.92,
        open=12.88, high=12.91, low=12.68, avg_price=12.80,
        limit_up=14.21, limit_down=11.63, change_pct=-0.70,
        min5_change=0.42, volume_ratio=1.05, turnover=4.2, amplitude=1.78,
        support_strength_score=73, orderbook_imbalance=0.14,
        bid_depth_5=210_000, ask_depth_5=120_000, withdrawal_ratio=0.03,
        _intraday_amount_flow={"intraday_amount_confirmed": True},
        _rolling_60s_momentum={
            "rolling_60s_confirmed": True,
            "rolling_60s_change_pct": 0.46,
            "rolling_60s_amount_delta": 5_000_000,
            "rolling_60s_amount_pace_ratio": 1.5,
            "rolling_60s_interval_sec": 60.0,
        },
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 3.0,
            "super_net_inflow": 20_000_000,
            "big_net_inflow": 25_000_000,
            "source": "eastmoney_main_fund",
            "is_stale": False,
        },
        sector_context={"sector_factors": [{
            "sector_name": "机器人概念",
            "sector_type": "concept",
            "source": "pywencai",
            "fund_flow": 10.0,
            "change_pct": 1.2,
            "strength_score": 74,
            "limit_up_count": 2,
            "lifecycle_state": "emerging",
            "lifecycle_score": 60,
            "active_days": 2,
            "is_sector_leader": True,
        }]},
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 13),
        reversal_state={"scan_change_pct": 0.38},
    )

    assert result is not None
    event_type, _, label, detail = result
    assert event_type == "low_absorb"
    assert label == "主升浪首阴低点回收"
    assert detail["signal_type"] == "main_wave_shape_pullback_reclaim"
    assert detail["main_wave_shape_pullback_rolling60"] is True
    assert detail["main_wave_strict_pullback_confirmed"] is True
    assert detail["large_order_inflow_confirmed"] is True
    assert detail["main_wave_sector_leader_confirmed"] is True

    scanner.dynamic_trend_pool["600001"]["stats"]["pullback_quality_tier"] = "watch"
    assert scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 3.0,
            "super_net_inflow": 20_000_000,
            "big_net_inflow": 25_000_000,
            "source": "eastmoney_main_fund",
            "is_stale": False,
        },
        sector_context={"sector_factors": [{
            "sector_name": "机器人概念",
            "sector_type": "concept",
            "source": "pywencai",
            "fund_flow": 10.0,
            "change_pct": 1.2,
            "strength_score": 74,
            "limit_up_count": 2,
            "lifecycle_state": "emerging",
            "lifecycle_score": 60,
            "active_days": 2,
            "is_sector_leader": True,
        }]},
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 13),
        reversal_state={"scan_change_pct": 0.38},
    ) is None
    scanner.dynamic_trend_pool["600001"]["stats"]["pullback_quality_tier"] = "strict"

    spot._rolling_60s_momentum = {"rolling_60s_confirmed": False}
    assert scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 3.0,
            "super_net_inflow": 20_000_000,
            "big_net_inflow": 25_000_000,
            "source": "eastmoney_main_fund",
            "is_stale": False,
        },
        sector_context={"sector_factors": [{
            "sector_name": "机器人概念",
            "sector_type": "concept",
            "source": "pywencai",
            "fund_flow": 10.0,
            "change_pct": 1.2,
            "strength_score": 74,
            "limit_up_count": 2,
            "lifecycle_state": "emerging",
            "lifecycle_score": 60,
            "active_days": 2,
            "is_sector_leader": True,
        }]},
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 13),
        reversal_state={"scan_change_pct": 0.38},
    ) is None


def test_main_wave_intraday_support_reclaim_requires_rolling_60s_volume():
    """当日正在形成首阴时，只能按盘前趋势支撑回收触发，不能用事后低点。"""
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "600002",
        "name": "主升回踩样本",
        "score": 88,
        "label": "主升浪加速",
        "candidate_source": "main_wave_pattern",
        "setup_state": "armed_main_wave_pullback",
        "support": 12.70,
        "resistance": 13.40,
        "main_wave_stats": {
            "support": 12.70,
            "resistance": 13.40,
            "pct_12": 28.0,
            "near_high_ratio": 0.96,
        },
    }])
    spot = SimpleNamespace(
        code="600002", name="主升回踩样本", price=12.83, prev_close=12.92,
        open=12.90, high=12.91, low=12.68, avg_price=12.80,
        limit_up=14.21, limit_down=11.63, change_pct=-0.70,
        min5_change=0.42, volume_ratio=1.05, turnover=4.2, amplitude=1.78,
        support_strength_score=73, orderbook_imbalance=0.14,
        bid_depth_5=210_000, ask_depth_5=120_000, withdrawal_ratio=0.03,
        _intraday_amount_flow={"intraday_amount_confirmed": True},
        _rolling_60s_momentum={
            "rolling_60s_confirmed": True,
            "rolling_60s_change_pct": 0.46,
            "rolling_60s_amount_delta": 5_000_000,
            "rolling_60s_amount_pace_ratio": 1.5,
            "rolling_60s_interval_sec": 60.0,
        },
    )
    kwargs = {
        "spot": spot,
        "current_fund": {
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 3.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
        },
        "sector_context": {"sector_factors": [{
            "fund_flow": 10.0,
            "change_pct": 1.2,
            "strength_score": 74,
            "limit_up_count": 1,
        }]},
        "board_context": {"max_recent_consecutive_days": 0},
        "today": date(2026, 8, 13),
        "reversal_state": {"scan_change_pct": 0.38},
    }

    result = scanner._detect_trend_driver_watch_setup(**kwargs)

    assert result is not None
    event_type, _, label, detail = result
    assert event_type == "low_absorb"
    assert label == "主升浪支撑回收"
    assert detail["signal_type"] == "main_wave_support_reclaim"
    assert detail["main_wave_support_reclaim_rolling60"] is True
    assert detail["main_wave_shape_pullback_confirmed"] is False

    spot._rolling_60s_momentum = {"rolling_60s_confirmed": False}
    assert scanner._detect_trend_driver_watch_setup(**kwargs) is None


def test_dynamic_trend_pool_detects_support_reclaim_low_absorb():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "003032",
        "name": "传智教育",
        "score": 88,
        "label": "趋势驱动-首波回踩",
        "setup_state": "armed_pullback",
        "support": 10.0,
        "resistance": 11.0,
    }])
    spot = SimpleNamespace(
        code="003032", name="传智教育", price=10.10, prev_close=10.15,
        open=10.02, high=10.20, low=9.95, avg_price=10.05,
        limit_up=11.17, limit_down=9.14, change_pct=-0.49,
        min5_change=0.6, volume_ratio=1.3, turnover=4.0, amplitude=2.5,
        support_strength_score=72, orderbook_imbalance=0.12,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 4.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-07-31",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 6.0,
                "change_pct": 1.2,
                "strength_score": 74,
                "limit_up_count": 1,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 7, 31),
    )

    assert result is not None
    event_type, _, _, detail = result
    assert event_type == "low_absorb"
    assert detail["signal_type"] == "trend_driver_low_absorb"
    assert detail["low_absorb_type"] == "trend_driver_support_reclaim"


def test_dynamic_trend_pool_warns_when_price_first_touches_support():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "003032",
        "name": "传智教育",
        "score": 88,
        "label": "趋势驱动-首波回踩",
        "setup_state": "armed_pullback",
        "support": 10.0,
        "resistance": 11.0,
    }])
    spot = SimpleNamespace(
        code="003032", name="传智教育", price=10.02, prev_close=10.20,
        open=10.12, high=10.18, low=9.99, avg_price=10.08,
        limit_up=11.22, limit_down=9.18, change_pct=-1.76,
        min5_change=-0.20, volume_ratio=1.15, turnover=3.2, amplitude=1.9,
        support_strength_score=72, orderbook_imbalance=0.12,
        bid_depth_5=180_000, ask_depth_5=100_000,
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 80_000_000,
            "main_net_inflow_pct": 4.0,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-08-10 10:05:00",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 6.0,
                "change_pct": 1.2,
                "strength_score": 74,
                "limit_up_count": 1,
            }],
        },
        board_context={"max_recent_consecutive_days": 0},
        today=date(2026, 8, 10),
    )

    assert result is not None
    event_type, _, label, detail = result
    assert event_type == "low_absorb"
    assert label == "趋势支撑到达预警"
    assert detail["signal_type"] == "trend_support_touch"
    assert detail["wait_reclaim_confirmation"] is True
    assert detail["near_intraday_low"] is True


def test_dynamic_second_wave_pool_detects_first_intraday_restart():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "000815",
        "name": "美利云",
        "score": 82,
        "label": "高标下杀二波准备",
        "candidate_source": "second_wave_reset_watch",
        "setup_state": "armed_second_wave_reset",
        "support": 13.70,
        "resistance": 17.37,
        "main_wave_stats": {
            "setup_state": "armed_second_wave_reset",
            "reset_type": "high_board_reset",
            "max_board_streak": 4,
            "days_since_peak": 6,
            "drawdown_pct": -22.77,
            "first_wave_gain_pct": 48.2,
            "support": 13.70,
            "resistance": 17.37,
        },
    }])
    spot = SimpleNamespace(
        code="000815", name="美利云", price=14.20, prev_close=13.80,
        open=13.86, high=14.22, low=13.72, avg_price=14.05,
        limit_up=15.18, limit_down=12.42, change_pct=2.90,
        min5_change=0.90, volume_ratio=1.45, turnover=5.2, amplitude=3.6,
        support_strength_score=74, orderbook_imbalance=0.16,
        bid_depth_5=180_000, ask_depth_5=100_000,
        _intraday_amount_flow={
            "intraday_amount_confirmed": True,
            "intraday_amount_delta": 6_000_000,
            "intraday_amount_interval_sec": 30.0,
            "intraday_amount_pace_ratio": 2.2,
        },
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 100_000_000,
            "main_net_inflow_pct": 4.2,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-07-31 10:05:00",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 8.0,
                "change_pct": 1.5,
                "strength_score": 76,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 4},
        today=date(2026, 7, 31),
        reversal_state={"scan_change_pct": 1.1},
    )

    assert result is not None
    event_type, _, label, detail = result
    assert event_type == "low_absorb"
    assert label == "高标下杀二波启动预警"
    assert detail["signal_type"] == "second_wave_restart"
    assert detail["second_wave_reset_type"] == "high_board_reset"
    assert detail["detection_pool_member"] is True


def test_dynamic_second_wave_pool_detects_limit_down_cascade_reversal():
    scanner = AnomalyScanner()
    scanner.update_dynamic_trend_pool([{
        "code": "603823",
        "name": "百合花",
        "score": 82,
        "label": "高标下杀二波准备",
        "candidate_source": "second_wave_reset_watch",
        "setup_state": "armed_second_wave_reset",
        "support": 44.83,
        "resistance": 80.90,
        "main_wave_stats": {
            "setup_state": "armed_second_wave_reset",
            "reset_type": "trend_cascade_reset",
            "max_board_streak": 2,
            "days_since_peak": 7,
            "drawdown_pct": -47.27,
            "first_wave_gain_pct": 99.91,
            "support": 44.83,
            "resistance": 80.90,
        },
    }])
    spot = SimpleNamespace(
        code="603823", name="百合花", price=48.50, prev_close=49.81,
        open=44.83, high=48.60, low=44.83, avg_price=47.90,
        limit_up=54.79, limit_down=44.83, change_pct=-2.63,
        min5_change=1.20, volume_ratio=1.44, turnover=6.0, amplitude=8.4,
        support_strength_score=76, orderbook_imbalance=0.20,
        bid_depth_5=190_000, ask_depth_5=100_000,
        _intraday_amount_flow={
            "intraday_amount_confirmed": True,
            "intraday_amount_delta": 8_000_000,
            "intraday_amount_interval_sec": 30.0,
            "intraday_amount_pace_ratio": 2.4,
        },
    )

    result = scanner._detect_trend_driver_watch_setup(
        spot=spot,
        current_fund={
            "main_net_inflow": 90_000_000,
            "main_net_inflow_pct": 3.8,
            "source": "eastmoney_main_fund",
            "is_stale": False,
            "as_of": "2026-07-31 10:20:00",
        },
        sector_context={
            "sector_factors": [{
                "fund_flow": 7.0,
                "change_pct": 1.3,
                "strength_score": 74,
                "limit_up_count": 2,
            }],
        },
        board_context={"max_recent_consecutive_days": 2},
        today=date(2026, 7, 31),
        reversal_state={"scan_change_pct": 1.8},
    )

    assert result is not None
    _, _, _, detail = result
    assert detail["signal_type"] == "second_wave_restart"
    assert detail["second_wave_reset_type"] == "trend_cascade_reset"
    assert detail["change_pct"] == pytest.approx(-2.63)


def test_low_base_watchlist_emits_dynamic_platform_confirmation(monkeypatch):
    scanner = AnomalyScanner()
    closes = [5.8 + index * (0.65 / 119) for index in range(120)]
    highs = [value + 0.05 for value in closes]
    highs[25] = 7.4
    lows = [value - 0.08 for value in closes]
    spot = SimpleNamespace(
        code="002768",
        name="国恩股份",
        price=6.72,
        prev_close=6.52,
        open=6.55,
        high=6.74,
        low=6.48,
        avg_price=6.62,
        limit_up=7.17,
        limit_down=5.87,
        change_pct=3.07,
        min5_change=0.8,
        volume=350_000,
        amount=230_000_000,
        turnover=5.1,
        volume_ratio=2.0,
        amplitude=4.0,
        circ_market_cap=46.5,
        pe_ttm=20.0,
        pb=2.0,
        dividend_yield=1.0,
        net_profit_growth=133.8,
        bid1_price=6.71,
        bid1_volume=20_000,
        ask1_price=6.72,
        ask1_volume=10_000,
        bid_depth_5=80_000,
        ask_depth_5=40_000,
        orderbook_imbalance=0.18,
        bid_ask_spread=0.01,
        seal_quality_score=0,
        support_strength_score=72,
        withdrawal_ratio=0.02,
        change_amt=0.2,
    )
    result = scanner._detect_low_base_watch_setup(
        spot=spot,
        tech={"low_base_profile": {"closes": closes, "highs": highs, "lows": lows}},
        current_fund={
            "main_net_inflow": 120_000_000,
            "main_net_inflow_pct": 5.2,
            "source": "eastmoney_main_fund",
            "as_of": "2026-07-16 10:20:00",
            "is_stale": False,
        },
        sector_context={
            "sector_factors": [
                {
                    "sector_name": "改性塑料",
                    "fund_flow": 12.0,
                    "change_pct": 1.5,
                    "strength_score": 60,
                    "limit_up_count": 2,
                }
            ],
            "sector_components": [],
        },
        board_context={"max_recent_consecutive_days": 0, "recent_limit_up_hits": 0},
        today=date(2026, 7, 16),
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    event_type, score, label, detail = result
    assert event_type == "breakthrough"
    assert score >= 80
    assert label == "核心成长平台放量确认"
    assert detail["low_base_watchlist"] is True
    assert detail["watchlist_member"] is True
    assert detail["low_base_setup_confirmed"] is True
    assert detail["core_growth_qualified"] is True
    assert detail["watchlist_label"] == "核心成长观察池"
    assert detail["core_growth_logic"]
    assert detail["core_growth_confirmations"][0] == "净利润增速+133.8%"
    assert detail["high_20d"] > 0

    for field_name, invalid_value in (
        ("net_profit_growth", -5.0),
        ("net_profit_growth", 250.0),
        ("pe_ttm", 0.0),
        ("pe_ttm", 120.0),
        ("circ_market_cap", 350.0),
    ):
        invalid_spot = SimpleNamespace(**vars(spot))
        setattr(invalid_spot, field_name, invalid_value)
        assert scanner._detect_low_base_watch_setup(
            spot=invalid_spot,
            tech={"low_base_profile": {"closes": closes, "highs": highs, "lows": lows}},
            current_fund={
                "main_net_inflow": 120_000_000,
                "main_net_inflow_pct": 5.2,
                "source": "eastmoney_main_fund",
                "as_of": "2026-07-16 10:20:00",
                "is_stale": False,
            },
            sector_context={
                "sector_factors": [
                    {
                        "sector_name": "改性塑料",
                        "fund_flow": 12.0,
                        "change_pct": 1.5,
                        "strength_score": 60,
                        "limit_up_count": 2,
                    }
                ],
                "sector_components": [],
            },
            board_context={"max_recent_consecutive_days": 0, "recent_limit_up_hits": 0},
            today=date(2026, 7, 16),
            fund_source="eastmoney_main_fund",
        ) is None

    monkeypatch.setattr(
        "app.signal.anomaly_scanner.settings.ANOMALY_CORE_GROWTH_MAX_POSITION_120D",
        50.0,
    )
    assert scanner._detect_low_base_watch_setup(
        spot=spot,
        tech={"low_base_profile": {"closes": closes, "highs": highs, "lows": lows}},
        current_fund={
            "main_net_inflow": 120_000_000,
            "main_net_inflow_pct": 5.2,
            "source": "eastmoney_main_fund",
            "as_of": "2026-07-16 10:20:00",
            "is_stale": False,
        },
        sector_context={
            "sector_factors": [
                {
                    "sector_name": "改性塑料",
                    "fund_flow": 12.0,
                    "change_pct": 1.5,
                    "strength_score": 60,
                    "limit_up_count": 2,
                }
            ],
            "sector_components": [],
        },
        board_context={"max_recent_consecutive_days": 0, "recent_limit_up_hits": 0},
        today=date(2026, 7, 16),
        fund_source="eastmoney_main_fund",
    ) is None


@pytest.mark.asyncio
async def test_sent_a2_repair_signal_is_carried_into_plan_and_dynamic_pool(tenbagger_session):
    session = tenbagger_session
    signal_day = date(2026, 8, 4)
    session.add(StockSpot(
        code="000657",
        name="中钨高新",
        price=52.21,
        prev_close=47.99,
        high=52.60,
        low=47.80,
        change_pct=8.79,
        turnover=5.57,
        volume_ratio=1.33,
        main_net_inflow=540_000_000,
        circ_market_cap=250.0,
    ))
    session.add(StockKline(
        code="000657",
        trade_date=signal_day,
        open=48.10,
        close=52.21,
        high=52.60,
        low=47.80,
        volume=10_000_000,
        turnover=5.57,
        change_pct=8.79,
    ))
    session.add(SignalPerformance(
        signal_id="an260804000657repair",
        stock_code="000657",
        signal_time=datetime(2026, 8, 4, 13, 20),
        signal_price=51.80,
        signal_score=94,
        signal_type="watch_breakthrough",
        signal_variant="sector_repair_reversal",
        setup_grade="A2 盘口确认后执行",
        top_factor="产业链强修复",
    ))
    await session.commit()

    candidates = await _load_sent_repair_plan_candidates(session, signal_day)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["candidate_source"] == "sector_repair_reversal"
    assert candidate["dynamic_pool_source"] == "sector_repair_reversal"
    assert candidate["main_wave_stats"]["sent_signal"] is True
    assert candidate["main_wave_stats"]["resistance"] > candidate["main_wave_stats"]["support"]


@pytest.mark.asyncio
async def test_anomaly_candidate_lifecycle_persists_all_gate_outcomes(
    tenbagger_session,
    monkeypatch,
):
    session = tenbagger_session
    signal_day = date(2026, 8, 31)
    class CaptureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 31, 10)
    monkeypatch.setattr(tenbagger_api, "datetime", CaptureClock)
    anomalies = [
        {
            "code": code,
            "name": name,
            "event_type": "capital",
            "score": score,
            "detail": {"capital_anomaly_type": "main_inflow"},
        }
        for code, name, score in (
            ("600101", "成功候选", 91),
            ("600102", "节流候选", 88),
            ("600103", "失败候选", 86),
            ("600104", "门控拒绝候选", 70),
        )
    ]

    def fake_buy_fields(item, _peers):
        rejected = item["code"] == "600104"
        return {
            "buy_point_grade": "B类观察候选" if rejected else "A2 盘口确认后执行",
            "buy_point_pushable": not rejected,
            "buy_point_blockers": ["行情覆盖不足"] if rejected else [],
        }

    monkeypatch.setattr(
        tenbagger_api,
        "_resolve_anomaly_buy_point_fields",
        fake_buy_fields,
    )

    await tenbagger_api._persist_anomaly_candidate_records(
        session,
        signal_day,
        anomalies,
        evaluated=False,
    )
    initial = list((await session.execute(
        select(AnomalyCandidateRecord).order_by(AnomalyCandidateRecord.code)
    )).scalars().all())
    assert [row.status for row in initial] == ["candidate"] * 4

    outcome_anomalies = anomalies[:3]
    messages = [
        PushMessage(
            title=item["name"],
            content="测试",
            stock_code=item["code"],
            extra={
                "signal_identity": tenbagger_api._anomaly_signal_identity(item),
            },
        )
        for item in outcome_anomalies
    ]
    await tenbagger_api._persist_anomaly_candidate_records(
        session,
        signal_day,
        anomalies,
        evaluated=True,
        messages=messages,
        results=[
            {"sent": True, "throttled": False},
            {"sent": False, "throttled": True},
            {"sent": False, "throttled": False, "disabled": False},
        ],
    )

    rows = {
        row.code: row
        for row in (await session.execute(select(AnomalyCandidateRecord))).scalars().all()
    }
    assert rows["600101"].status == "pushed"
    assert rows["600101"].pushed is True
    assert rows["600101"].pushed_at is not None
    assert rows["600102"].status == "throttled"
    assert json.loads(rows["600102"].reject_reasons_json) == ["推送限频"]
    assert rows["600103"].status == "failed"
    assert "推送发送失败" in json.loads(rows["600103"].reject_reasons_json)
    assert rows["600104"].status == "rejected"
    assert json.loads(rows["600104"].reject_reasons_json) == ["行情覆盖不足"]
    assert all(row.seen_count == 2 for row in rows.values())
    assert (await session.execute(select(SignalPerformance))).scalars().all() == []

    # 后续轮次未重新发送，也不能擦除当天已经成功推送的状态。
    await tenbagger_api._persist_anomaly_candidate_records(
        session,
        signal_day,
        [anomalies[0]],
        evaluated=True,
    )
    await session.refresh(rows["600101"])
    assert rows["600101"].status == "pushed"
    assert rows["600101"].seen_count == 3


@pytest.mark.asyncio
async def test_persisted_hourly_push_budget_survives_process_restart(tenbagger_session, monkeypatch):
    session = tenbagger_session
    target_day = date(2026, 8, 4)
    now = datetime.combine(target_day, datetime.min.time()).replace(hour=10)
    for index in range(2):
        session.add(SignalPerformance(
            signal_id=f"budget{index:02d}",
            stock_code=f"60000{index}",
            signal_time=now - timedelta(minutes=10 + index),
            signal_price=10.0,
            signal_score=90,
            signal_type="anomaly_breakthrough",
            signal_variant="budget_test",
            setup_grade="A2 盘口确认后执行",
        ))
    await session.commit()
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_MAX_PER_HOUR",
        2,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_A1_RESERVE_PER_HOUR",
        1,
    )
    messages = [
        PushMessage(
            title=f"预算测试{index}",
            content="测试",
            stock_code=f"00000{index}",
            extra={
                "setup_grade": "A1 可直接执行" if index == 1 else "A2 盘口确认后执行",
                "watchlist_member": index == 1,
            },
        )
        for index in range(3)
    ]

    accepted = await _cap_automatic_push_messages_by_persisted_budget(
        session,
        target_day,
        messages,
        now=now,
    )

    assert len(accepted) == 1
    assert accepted[0].extra["setup_grade"].startswith("A1")


@pytest.mark.asyncio
async def test_persisted_push_budget_smooths_a2_burst_instead_of_refilling_on_the_hour(
    tenbagger_session,
    monkeypatch,
):
    session = tenbagger_session
    target_day = date(2026, 8, 4)
    now = datetime.combine(target_day, datetime.min.time()).replace(hour=10)
    for index in range(3):
        session.add(SignalPerformance(
            signal_id=f"smooth{index:02d}",
            stock_code=f"60100{index}",
            signal_time=now - timedelta(seconds=22 - index),
            signal_price=10.0,
            signal_score=82,
            signal_type="watch_low_absorb",
            signal_variant="smooth_budget_test",
            setup_grade="A2 盘口确认后执行",
        ))
    await session.commit()
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_MAX_PER_HOUR",
        18,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_BURST_CAPACITY",
        3,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_A1_RESERVE_PER_HOUR",
        0,
    )
    messages = [
        PushMessage(
            title=f"平滑预算测试{index}",
            content="测试",
            stock_code=f"00010{index}",
            extra={"setup_grade": "A2 盘口确认后执行"},
        )
        for index in range(2)
    ]

    accepted = await _cap_automatic_push_messages_by_persisted_budget(
        session,
        target_day,
        messages,
        now=now,
    )

    assert accepted == []


@pytest.mark.asyncio
async def test_persisted_push_budget_refills_a2_between_intraday_scans(
    tenbagger_session,
    monkeypatch,
):
    session = tenbagger_session
    target_day = date(2026, 8, 4)
    now = datetime.combine(target_day, datetime.min.time()).replace(hour=10)
    for index in range(3):
        session.add(SignalPerformance(
            signal_id=f"refill{index:02d}",
            stock_code=f"60200{index}",
            signal_time=now - timedelta(minutes=5, seconds=2 - index),
            signal_price=10.0,
            signal_score=82,
            signal_type="watch_low_absorb",
            signal_variant="smooth_refill_test",
            setup_grade="A2 盘口确认后执行",
        ))
    await session.commit()
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_MAX_PER_HOUR",
        18,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_BURST_CAPACITY",
        3,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_A1_RESERVE_PER_HOUR",
        0,
    )
    messages = [
        PushMessage(
            title=f"补充预算测试{index}",
            content="测试",
            stock_code=f"00020{index}",
            extra={"setup_grade": "A2 盘口确认后执行"},
        )
        for index in range(2)
    ]

    accepted = await _cap_automatic_push_messages_by_persisted_budget(
        session,
        target_day,
        messages,
        now=now,
    )

    assert len(accepted) == 1
    assert accepted[0].stock_code == "000200"


@pytest.mark.asyncio
async def test_persisted_push_budget_counts_same_stock_once(
    tenbagger_session,
    monkeypatch,
):
    session = tenbagger_session
    target_day = date(2026, 8, 4)
    now = datetime.combine(target_day, datetime.min.time()).replace(hour=10)
    for index, variant in enumerate(("regular", "catchup")):
        session.add(SignalPerformance(
            signal_id=f"dedup-budget-{index}",
            stock_code="600001",
            signal_time=now - timedelta(minutes=10 - index),
            signal_price=10.0,
            signal_score=88,
            signal_type="watch_breakthrough",
            signal_variant=f"positive_acceleration_{variant}",
            setup_grade="B类观察候选",
        ))
    await session.commit()
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_MAX_PER_HOUR", 2,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_BURST_CAPACITY", 3,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_A1_RESERVE_PER_HOUR", 0,
    )
    message = PushMessage(
        title="新股异动", content="测试", stock_code="600002",
        extra={"setup_grade": "B类观察候选"},
    )

    accepted = await _cap_automatic_push_messages_by_persisted_budget(
        session, target_day, [message], now=now,
    )

    assert accepted == [message]


@pytest.mark.asyncio
async def test_strong_rolling_rise_uses_reserved_budget_when_a2_bucket_is_empty(
    tenbagger_session,
    monkeypatch,
):
    session = tenbagger_session
    target_day = date(2026, 8, 4)
    now = datetime.combine(target_day, datetime.min.time()).replace(hour=10)
    for index in range(3):
        session.add(SignalPerformance(
            signal_id=f"rapid-reserve{index}",
            stock_code=f"60310{index}",
            signal_time=now - timedelta(seconds=20 - index),
            signal_price=10.0,
            signal_score=82,
            signal_type="watch_low_absorb",
            signal_variant="ordinary_a2",
            setup_grade="A2 盘口确认后执行",
        ))
    await session.commit()
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_MAX_PER_HOUR", 18,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_BURST_CAPACITY", 3,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_A1_RESERVE_PER_HOUR", 0,
    )
    monkeypatch.setattr(
        "app.api.v1.tenbagger.settings.ANOMALY_PUSH_RAPID_STRONG_RESERVE_PER_HOUR", 1,
    )
    ordinary = PushMessage(
        title="普通A2", content="测试", stock_code="000301",
        extra={"setup_grade": "A2 盘口确认后执行"},
    )
    strong = PushMessage(
        title="60秒强急拉", content="测试", stock_code="000302",
        extra={
            "setup_grade": "A2 盘口确认后执行",
            "signal_variant": "positive_acceleration_strong",
            "rapid_rise_strong_confirmed": True,
        },
    )

    accepted = await _cap_automatic_push_messages_by_persisted_budget(
        session, target_day, [ordinary, strong], now=now,
    )

    assert accepted == [strong]


@pytest.mark.asyncio
async def test_sent_anomaly_signal_is_recorded_and_settled(tenbagger_session):
    session = tenbagger_session
    signal_day = date(2026, 7, 15)
    message = PushMessage(
        title="重点观察测试",
        content="测试",
        stock_code="600351",
        stock_name="亚宝药业",
        extra={
            "signal_identity": "600351|breakthrough|low_base_breakthrough|6.54",
            "event_type": "breakthrough",
            "watchlist_member": True,
            "low_base_watchlist": True,
            "low_base_setup_confirmed": True,
            "signal_variant": "low_base_breakthrough",
            "setup_grade": "A2 盘口确认后执行",
            "signal_price": 10.0,
            "signal_score": 88,
            "signal_label": "低位平台放量确认",
        },
    )
    await _record_sent_anomaly_signals(
        session,
        signal_day,
        [message],
        [{"sent": True}],
    )
    record = (await session.execute(select(SignalPerformance))).scalar_one()
    assert record.signal_type == "watch_breakthrough"
    assert record.signal_variant == "low_base_breakthrough"
    assert record.setup_grade == "A2 盘口确认后执行"
    assert record.board_tag == "watchlist"

    session.add_all([
        StockKline(code="600351", trade_date=date(2026, 7, 16), open=10.0, high=10.5, low=9.8, close=10.2, volume=1_000_000),
        StockKline(code="600351", trade_date=date(2026, 7, 17), open=10.2, high=10.8, low=10.1, close=10.6, volume=1_100_000),
        StockKline(code="600351", trade_date=date(2026, 7, 20), open=10.6, high=11.0, low=10.4, close=10.8, volume=1_200_000),
    ])
    await session.commit()

    await _settle_anomaly_signal_performance(session, date(2026, 7, 20))
    await session.refresh(record)
    assert record.return_1d == pytest.approx(2.0)
    assert record.return_3d == pytest.approx(8.0)
    assert record.net_return_3d == pytest.approx(7.64)
    assert record.benchmark_return_3d == pytest.approx(0.0)
    assert record.excess_return_3d == pytest.approx(7.64)
    assert record.is_correct is True


@pytest.mark.asyncio
async def test_observation_push_is_persisted_for_budget_but_not_buy_performance(
    tenbagger_session,
):
    session = tenbagger_session
    message = PushMessage(
        title="全市场放量急拉观察",
        content="测试",
        stock_code="600352",
        stock_name="测试股份",
        extra={
            "signal_identity": "600352|positive_acceleration|observation",
            "event_type": "breakthrough",
            "signal_variant": "positive_acceleration_observation",
            "setup_grade": "B类观察候选",
            "signal_price": 10.0,
            "signal_score": 78,
            "signal_label": "60秒放量急拉观察",
            "observation_only": True,
        },
    )

    await _record_sent_anomaly_signals(
        session,
        date(2026, 8, 13),
        [message],
        [{"sent": True}],
    )
    record = (await session.execute(select(SignalPerformance))).scalar_one()

    assert record.signal_type == "observation"
    assert record.evaluation_version == "anomaly_observation_v1"
    assert record.setup_grade == "B类观察候选"

    session.add(StockKline(
        code="600352",
        trade_date=date(2026, 8, 14),
        open=10.0,
        high=10.5,
        low=9.5,
        close=9.8,
        volume=1_000_000,
    ))
    await session.commit()
    await _settle_anomaly_signal_performance(session, date(2026, 8, 14))
    await session.refresh(record)

    assert record.return_1d is None
    assert record.net_return_1d is None


@pytest.mark.asyncio
async def test_mature_poor_a2_performance_blocks_future_push(tenbagger_session, monkeypatch):
    monkeypatch.setattr("app.api.v1.tenbagger.settings.ANOMALY_EVAL_MIN_SAMPLES", 3)
    session = tenbagger_session
    for index in range(3):
        session.add(SignalPerformance(
            signal_id=f"poor-a2-{index}",
            stock_code="600351",
            signal_time=datetime(2026, 7, 10 + index, 10, 0),
            signal_price=10.0,
            signal_score=85,
            signal_type="watch_breakthrough",
            signal_variant="low_base_breakthrough",
            setup_grade="A2 盘口确认后执行",
            net_return_3d=-1.0,
            excess_return_3d=-1.5,
            is_correct=False,
            evaluation_version="anomaly_v2",
        ))
    await session.commit()
    message = PushMessage(
        title="低位突破A2",
        content="测试",
        stock_code="600351",
        category="anomaly",
        extra={
            "event_type": "breakthrough",
            "signal_variant": "low_base_breakthrough",
            "setup_grade": "A2 盘口确认后执行",
            "watchlist_member": True,
        },
    )

    accepted = await _filter_push_messages_by_performance(
        session,
        date(2026, 7, 16),
        [message],
    )

    assert accepted == []
    assert message.extra["performance_evidence"]["blocked"] is True


@pytest.mark.asyncio
async def test_scan_market_detects_green_reversal_ma5_low_absorb(tenbagger_session):
    trade_day = date(2026, 4, 20)
    code = "000112"
    closes = [
        9.10, 9.16, 9.20, 9.25, 9.31,
        9.38, 9.45, 9.52, 9.60, 9.68,
        9.74, 9.80, 9.86, 9.92, 9.98,
        10.02, 10.00, 10.04, 10.01, 10.03,
    ]
    for index, close in enumerate(closes):
        tenbagger_session.add(
            StockKline(
                code=code,
                trade_date=trade_day - timedelta(days=len(closes) - index),
                open=close * 0.99,
                high=close * 1.02,
                low=close * 0.98,
                close=close,
                volume=1_000_000 + index * 20_000,
                amount=(1_000_000 + index * 20_000) * close,
                turnover=4.0,
                change_pct=1.0,
                prev_close=closes[index - 1] if index else close,
            )
        )
    tenbagger_session.add(
        StockSpot(
            code=code,
            name="低吸确认股",
            price=10.08,
            prev_close=10.14,
            open=9.92,
            high=10.18,
            low=9.78,
            change_pct=-0.6,
            amplitude=3.9,
            min5_change=0.82,
            volume=520_000,
            amount=520_000_000,
            turnover=6.4,
            volume_ratio=1.35,
            avg_price=10.02,
            bid_depth_5=48_000,
            ask_depth_5=26_000,
            orderbook_imbalance=0.18,
            support_strength_score=72,
            withdrawal_ratio=0.03,
            limit_up=11.15,
            limit_down=9.13,
        )
    )
    tenbagger_session.add(
        StockSectorMapping(
            code=code,
            sector_code="BK001",
            sector_name="机器人",
            sector_type="concept",
            source="pywencai",
        )
    )
    tenbagger_session.add(
        SectorPersistence(
            sector_code="BK001",
            sector_name="机器人",
            trade_date=trade_day,
            consecutive_days=2,
            limit_up_count=3,
            fund_flow=8.2,
            change_pct=1.1,
            strength_score=72,
        )
    )
    await tenbagger_session.commit()

    scanner = AnomalyScanner()
    events = await scanner.scan_market(
        tenbagger_session,
        min_score=50,
        target_date=trade_day,
        current_fund_map={
            code: {
                "main_net_inflow": 180_000_000,
                "main_net_inflow_pct": 5.2,
                "super_net_inflow": 90_000_000,
                "super_net_inflow_pct": 1.7,
                "big_net_inflow": 80_000_000,
                "big_net_inflow_pct": 2.2,
                "source": "eastmoney_main_fund",
                "as_of": "2026-04-20 10:18:00",
            }
        },
        fund_trade_date=trade_day,
    )

    low_absorb = next((event for event in events if event.event_type == "low_absorb"), None)
    assert low_absorb is not None
    assert "绿盘弱转强低吸" in low_absorb.description
    assert low_absorb.detail["low_absorb_type"] == "green_reversal_ma5_pullback"
    assert low_absorb.detail["distance_to_ma5_pct"] <= 2.4


@pytest.mark.asyncio
async def test_historical_single_snapshot_does_not_fabricate_underwater_volume_confirmation(
    tenbagger_session,
):
    trade_day = date(2026, 8, 10)
    code = "002580"
    tenbagger_session.add_all([
        StockSpot(
            code=code,
            name="水下快速启动股",
            price=10.32,
            prev_close=10.0,
            open=9.90,
            high=10.45,
            low=9.78,
            change_pct=3.2,
            amplitude=6.7,
            min5_change=1.15,
            volume=720_000,
            amount=730_000_000,
            turnover=8.2,
            volume_ratio=1.35,
            avg_price=10.12,
            bid_depth_5=62_000,
            ask_depth_5=31_000,
            orderbook_imbalance=0.26,
            support_strength_score=76,
            withdrawal_ratio=0.03,
            limit_up=11.0,
            limit_down=9.0,
        ),
        StockSectorMapping(
            code=code,
            sector_code="BK_POWER",
            sector_name="储能",
            sector_type="concept",
            source="test",
        ),
        SectorPersistence(
            sector_code="BK_POWER",
            sector_name="储能",
            trade_date=trade_day,
            consecutive_days=2,
            limit_up_count=4,
            fund_flow=12.0,
            change_pct=1.8,
            strength_score=76,
        ),
    ])
    await tenbagger_session.commit()

    scanner = AnomalyScanner()
    events = await scanner.scan_market(
        tenbagger_session,
        min_score=50,
        target_date=trade_day,
        current_fund_map={
            code: {
                "main_net_inflow": 180_000_000,
                "main_net_inflow_pct": 5.2,
                "super_net_inflow": 80_000_000,
                "super_net_inflow_pct": 2.1,
                "big_net_inflow": 70_000_000,
                "big_net_inflow_pct": 2.0,
                "source": "eastmoney_main_fund",
                "as_of": "2026-08-10 13:35:00",
                "is_stale": False,
            }
        },
        fund_trade_date=trade_day,
    )

    reversal = next(
        (
            event
            for event in events
            if event.code == code
            and event.event_type == "low_absorb"
            and event.detail.get("signal_type") == "underwater_reversal"
        ),
        None,
    )
    # 单个历史快照只能看到全天量比，不能证明急拉窗口存在真实增量成交。
    assert reversal is None


def test_dedupe_dragons_by_code_keeps_best_sector_context():
    dragons = [
        {
            "code": "000001",
            "name": "目标股",
            "sector_code": "BK001",
            "sector_name": "",
            "score": 98.5,
            "level": "dragon",
            "change_pct": 10.0,
        },
        {
            "code": "000001",
            "name": "目标股",
            "sector_code": "BK002",
            "sector_name": "目标概念",
            "score": 99.0,
            "level": "dragon",
            "change_pct": 10.0,
        },
        {
            "code": "000002",
            "name": "次强股",
            "sector_code": "BK003",
            "sector_name": "次强概念",
            "score": 80.0,
            "level": "quasi_dragon",
            "change_pct": 6.0,
        },
    ]

    deduped = _dedupe_dragons_by_code(dragons)

    assert [item["code"] for item in deduped] == ["000001", "000002"]
    assert deduped[0]["sector_code"] == "BK002"
    assert deduped[0]["related_sector_count"] == 2


def test_main_wave_pattern_detects_multi_board_stair_step():
    closes = [
        10.0, 10.2, 10.4, 10.6, 10.8,
        11.0, 12.1, 13.3, 14.6, 16.0,
        17.6, 19.3, 21.2, 22.0, 21.4,
        22.5, 24.7, 27.1, 26.5, 27.8,
    ]
    highs = [round(close * 1.01, 2) for close in closes]
    lows = [round(close * 0.97, 2) for close in closes]
    volumes = [
        100, 105, 102, 110, 108,
        120, 160, 180, 210, 230,
        260, 300, 340, 330, 310,
        360, 450, 520, 480, 500,
    ]

    result = _score_main_wave_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=4.9,
        turnover=8.0,
        volume_ratio=1.4,
    )

    assert result["is_main_wave_pattern"] is True
    assert result["main_wave_label"] == "多板主升浪跟踪"
    assert result["main_wave_stats"]["board_like_count_12"] >= 2
    assert result["main_wave_score"] >= 70


def test_main_wave_pattern_rejects_flat_or_broken_trend():
    closes = [
        10.0, 10.1, 10.0, 10.2, 10.1,
        10.0, 10.1, 10.0, 10.2, 10.1,
        10.0, 9.9, 9.8, 9.7, 9.6,
        9.5, 9.4, 9.3, 9.2, 9.1,
    ]
    highs = [round(close * 1.01, 2) for close in closes]
    lows = [round(close * 0.99, 2) for close in closes]
    volumes = [100 for _ in closes]

    result = _score_main_wave_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-2.0,
        turnover=1.0,
        volume_ratio=0.8,
    )

    assert result["is_main_wave_pattern"] is False
    assert "12日涨幅未进入主升浪区" in result["main_wave_blockers"]


def test_main_wave_pattern_marks_first_bearish_pullback_anchor():
    closes = [10.0 + index * 0.08 for index in range(14)] + [
        12.10, 12.55, 13.05, 13.55, 14.05, 13.78,
    ]
    opens = [value * 0.995 for value in closes]
    opens[-1] = 14.02
    highs = [value * 1.02 for value in closes]
    lows = [value * 0.98 for value in closes]
    volumes = [1_000_000.0] * 14 + [1_300_000, 1_450_000, 1_600_000, 1_800_000, 2_000_000, 1_250_000]

    result = _score_main_wave_kline_pattern(
        opens=opens,
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-1.97,
        turnover=6.0,
        volume_ratio=0.72,
    )

    stats = result["main_wave_stats"]
    assert result["is_main_wave_pattern"] is True
    assert stats["pullback_shape_type"] == "first_bearish"
    assert stats["pullback_anchor_low"] == pytest.approx(lows[-1], abs=0.01)
    assert stats["pullback_quality_tier"] == "strict"


def test_main_wave_first_bearish_with_deeper_drop_is_watch_only():
    closes = [10.0 + index * 0.08 for index in range(14)] + [
        12.10, 12.55, 13.05, 13.55, 14.05, 13.45,
    ]
    opens = [value * 0.995 for value in closes]
    opens[-1] = 14.02
    highs = [value * 1.02 for value in closes]
    lows = [value * 0.98 for value in closes]
    volumes = [1_000_000.0] * 14 + [
        1_300_000, 1_450_000, 1_600_000, 1_800_000, 2_000_000, 1_700_000,
    ]

    result = _score_main_wave_kline_pattern(
        opens=opens,
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-4.27,
        turnover=7.0,
        volume_ratio=0.9,
    )

    stats = result["main_wave_stats"]
    assert stats["pullback_shape_type"] == "first_bearish"
    assert stats["pullback_quality_tier"] == "watch"
    assert stats["pullback_price_stable"] is False


def test_main_wave_pattern_marks_low_volume_doji_pullback_anchor():
    closes = [10.0 + index * 0.08 for index in range(14)] + [
        12.10, 12.55, 13.05, 13.55, 14.05, 13.99,
    ]
    opens = [value * 0.995 for value in closes]
    opens[-1] = 13.97
    highs = [value * 1.02 for value in closes]
    lows = [value * 0.98 for value in closes]
    volumes = [1_000_000.0] * 14 + [1_300_000, 1_450_000, 1_600_000, 1_800_000, 2_000_000, 1_000_000]

    result = _score_main_wave_kline_pattern(
        opens=opens,
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-0.46,
        turnover=4.0,
        volume_ratio=0.55,
    )

    stats = result["main_wave_stats"]
    assert result["is_main_wave_pattern"] is True
    assert stats["pullback_shape_type"] == "low_volume_doji"
    assert stats["pullback_quality_tier"] == "strict"


def test_second_wave_pattern_detects_broken_board_reclaim():
    closes = [
        10.0, 11.0, 12.1, 13.3, 14.6,
        16.0, 14.4, 13.4, 13.8, 14.2,
        14.8, 15.4, 16.2, 17.0, 17.8,
        18.4, 19.0, 18.7, 19.4, 20.1,
        20.8, 21.5, 22.1, 22.8,
    ]
    highs = [round(close * 1.02, 2) for close in closes]
    lows = [round(close * 0.98, 2) for close in closes]
    volumes = [
        100, 160, 220, 280, 360,
        450, 520, 410, 360, 340,
        380, 410, 460, 500, 540,
        560, 590, 520, 610, 650,
        680, 720, 760, 800,
    ]

    result = _score_second_wave_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=3.2,
        turnover=9.0,
        volume_ratio=1.5,
    )

    assert result["is_second_wave_pattern"] is True
    assert result["second_wave_label"] == "断板二波主升跟踪"
    assert result["second_wave_stats"]["max_board_streak"] >= 4
    assert result["second_wave_stats"]["rebound_from_low_pct"] >= 10


def test_second_wave_pattern_rejects_unrepaired_broken_board():
    closes = [
        10.0, 11.0, 12.1, 13.3, 14.6,
        16.0, 14.4, 13.0, 12.5, 12.2,
        12.0, 11.8, 11.6, 11.4, 11.3,
        11.2, 11.1, 11.0, 10.9, 10.8,
        10.7, 10.6, 10.5, 10.4,
    ]
    highs = [round(close * 1.02, 2) for close in closes]
    lows = [round(close * 0.98, 2) for close in closes]
    volumes = [100 + i * 10 for i in range(len(closes))]

    result = _score_second_wave_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-1.0,
        turnover=4.0,
        volume_ratio=0.9,
    )

    assert result["is_second_wave_pattern"] is False
    assert "断板低点修复不足" in result["second_wave_blockers"]


def test_second_wave_pattern_anchors_latest_equal_height_board_cluster():
    closes = [
        10.0, 11.0, 12.1, 13.31,
        12.0, 11.5, 11.2, 11.0, 10.9, 10.8,
        10.7, 10.6, 10.5, 10.4, 10.3, 10.2,
        10.1, 10.0, 10.1, 10.0, 10.0,
        11.0, 12.1, 13.31,
        11.8, 10.8, 11.0, 11.4, 11.8, 12.2,
        12.5, 12.8, 13.0, 13.2,
    ]
    highs = [round(close * 1.02, 3) for close in closes]
    lows = [round(close * 0.98, 3) for close in closes]
    volumes = [1_000_000 + index * 10_000 for index in range(len(closes))]

    result = _score_second_wave_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=1.5,
        turnover=6.0,
        volume_ratio=1.3,
    )

    assert result["second_wave_stats"]["max_board_streak"] == 3
    assert result["second_wave_stats"]["days_after_break"] == 10
    assert result["second_wave_stats"]["support"] > 0
    assert result["second_wave_stats"]["resistance"] > result["second_wave_stats"]["support"]


def test_second_wave_reset_watch_arms_after_four_board_pullback_before_restart():
    closes = [9.6 + index * 0.02 for index in range(20)]
    closes.extend([10.0, 11.0, 12.1, 13.31, 14.64])
    closes.extend([13.36, 13.70, 13.05, 12.40, 11.85])
    highs = [round(value * 1.015, 3) for value in closes]
    lows = [round(value * 0.985, 3) for value in closes]
    volumes = [1_000_000.0] * 20
    volumes.extend([1_200_000, 1_500_000, 2_000_000, 2_600_000, 3_200_000])
    volumes.extend([3_600_000, 2_900_000, 2_400_000, 1_900_000, 1_500_000])

    result = _score_second_wave_reset_watch_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-4.4,
    )

    assert result["is_second_wave_reset_watch"] is True
    stats = result["second_wave_reset_stats"]
    assert stats["reset_type"] == "high_board_reset"
    assert stats["max_board_streak"] >= 4
    assert -35 <= stats["drawdown_pct"] <= -12
    assert stats["setup_state"] == "armed_second_wave_reset"


def test_second_wave_reset_watch_arms_after_main_wave_limit_down_cascade():
    closes = [20.0 * (1.035 ** index) for index in range(25)]
    for _ in range(5):
        closes.append(closes[-1] * 0.90)
    highs = [round(value * 1.015, 3) for value in closes]
    lows = [round(value * 0.985, 3) for value in closes]
    volumes = [1_500_000.0] * 25 + [1_000_000, 700_000, 400_000, 120_000, 80_000]

    result = _score_second_wave_reset_watch_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-10.0,
    )

    assert result["is_second_wave_reset_watch"] is True
    stats = result["second_wave_reset_stats"]
    assert stats["reset_type"] == "trend_cascade_reset"
    assert stats["first_wave_gain_pct"] >= 35
    assert stats["limit_down_like_count"] >= 2
    assert stats["dry_up_ratio"] <= 0.9


def test_trend_main_wave_pattern_detects_smooth_acceleration():
    closes = [
        10.0, 10.1, 10.2, 10.4, 10.5,
        10.7, 10.9, 11.0, 11.2, 11.5,
        11.7, 12.0, 12.2, 12.6, 12.9,
        13.1, 13.5, 13.8, 14.2, 14.6,
        15.0, 15.5, 16.0, 16.6, 17.1,
        17.8, 18.4, 19.0, 19.5, 20.2,
    ]
    highs = [round(close * 1.02, 2) for close in closes]
    lows = [round(close * 0.98, 2) for close in closes]
    volumes = [
        100, 102, 104, 105, 108,
        110, 112, 115, 118, 120,
        125, 130, 132, 138, 142,
        148, 155, 160, 168, 175,
        185, 195, 205, 218, 230,
        245, 260, 275, 290, 310,
    ]

    result = _score_trend_main_wave_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=3.6,
        turnover=6.0,
        volume_ratio=1.3,
    )

    assert result["is_trend_main_wave_pattern"] is True
    assert result["trend_main_wave_label"] == "趋势主升加速跟踪"
    assert result["trend_main_wave_stats"]["board_like_count_20"] <= 2
    assert result["trend_main_wave_stats"]["above_ma10_days"] >= 5


def test_trend_main_wave_pattern_rejects_choppy_nontrend():
    closes = [
        10.0, 10.8, 10.1, 10.9, 10.2,
        11.0, 10.3, 11.1, 10.4, 11.2,
        10.5, 11.3, 10.4, 11.1, 10.2,
        10.8, 10.1, 10.6, 9.9, 10.4,
        9.8, 10.3, 9.7, 10.2, 9.6,
        10.1, 9.5, 10.0, 9.4, 9.9,
    ]
    highs = [round(close * 1.03, 2) for close in closes]
    lows = [round(close * 0.96, 2) for close in closes]
    volumes = [150 for _ in closes]

    result = _score_trend_main_wave_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-1.0,
        turnover=2.0,
        volume_ratio=0.9,
    )

    assert result["is_trend_main_wave_pattern"] is False
    assert "中期趋势斜率不足" in result["trend_main_wave_blockers"]


def test_platform_breakout_pattern_detects_compressed_breakout():
    closes = [
        18.8, 19.1, 19.4, 19.2, 19.0,
        19.3, 19.7, 20.0, 19.8, 20.1,
        20.4, 20.2, 20.6, 20.5, 20.8,
        20.6, 20.3, 20.7, 20.9, 20.6,
        20.5, 19.9, 20.3, 20.8, 23.2,
    ]
    highs = [round(close * 1.015, 2) for close in closes]
    lows = [round(close * 0.985, 2) for close in closes]
    volumes = [
        100, 102, 105, 100, 98,
        106, 112, 118, 110, 115,
        120, 116, 124, 122, 130,
        126, 118, 121, 128, 119,
        116, 112, 118, 130, 260,
    ]

    result = _score_platform_breakout_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=9.5,
        turnover=8.0,
        volume_ratio=2.0,
    )

    assert result["is_platform_breakout_pattern"] is True
    assert result["platform_breakout_label"] == "平台突破主升跟踪"
    assert result["platform_breakout_stats"]["breakout_pct"] >= 2
    assert result["platform_breakout_stats"]["platform_range_pct"] <= 25


def test_platform_breakout_pattern_rejects_no_breakout():
    closes = [
        18.8, 19.1, 19.4, 19.2, 19.0,
        19.3, 19.7, 20.0, 19.8, 20.1,
        20.4, 20.2, 20.6, 20.5, 20.8,
        20.6, 20.3, 20.7, 20.9, 20.6,
        20.5, 19.9, 20.3, 20.8, 20.7,
    ]
    highs = [round(close * 1.015, 2) for close in closes]
    lows = [round(close * 0.985, 2) for close in closes]
    volumes = [120 for _ in closes]

    result = _score_platform_breakout_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=1.0,
        turnover=3.0,
        volume_ratio=0.9,
    )

    assert result["is_platform_breakout_pattern"] is False
    assert "尚未有效突破平台" in result["platform_breakout_blockers"]


def _chuanzhi_probe_wash_fixture():
    base_closes = [8.0 - index * (2.85 / 106) for index in range(107)]
    closes = base_closes + [
        5.21, 5.10, 5.20, 4.98, 5.08, 5.37, 5.65,
        5.59, 5.58, 5.42, 5.34, 5.60, 5.47,
    ]
    highs = [value * 1.02 for value in base_closes] + [
        5.34, 5.26, 5.32, 5.26, 5.08, 5.59, 5.76,
        5.91, 5.75, 5.57, 5.49, 5.60, 5.63,
    ]
    lows = [value * 0.98 for value in base_closes] + [
        5.09, 5.06, 5.01, 4.96, 4.89, 5.34, 5.34,
        5.54, 5.48, 5.28, 5.23, 5.26, 5.44,
    ]
    volumes = [10_000_000.0] * len(base_closes) + [
        13_506_274, 9_285_300, 12_782_300, 11_062_100, 8_784_100,
        34_649_518, 30_315_318, 24_203_000, 21_730_900, 16_354_600,
        14_638_900, 13_868_400, 9_010_800,
    ]
    return closes, highs, lows, volumes


def test_probe_wash_pattern_detects_chuanzhi_sequence_before_first_board():
    closes, highs, lows, volumes = _chuanzhi_probe_wash_fixture()

    result = _score_probe_wash_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-2.32,
        turnover=3.2,
    )

    stats = result["probe_wash_stats"]
    assert result["is_probe_wash_pattern"] is True
    assert result["probe_wash_label"] == "连板前兆-试盘缩量洗盘"
    assert stats["days_since_probe"] == 6
    assert stats["terminal_volume_ratio"] <= 0.30
    assert stats["wash_drawdown_pct"] >= 8.0
    assert stats["support"] < stats["resistance"] == pytest.approx(5.63)
    assert stats["setup_state"] == "armed_pullback"


def test_probe_wash_pattern_rejects_washout_without_volume_contraction():
    closes, highs, lows, volumes = _chuanzhi_probe_wash_fixture()
    volumes[-3:] = [28_000_000.0, 27_000_000.0, 26_000_000.0]

    result = _score_probe_wash_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-2.32,
        turnover=3.2,
    )

    assert result["is_probe_wash_pattern"] is False


def test_pre_board_ignition_prefers_probe_wash_over_broad_low_base():
    closes, highs, lows, volumes = _chuanzhi_probe_wash_fixture()

    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-2.32,
        turnover=3.2,
        volume_ratio=0.7,
    )

    assert result["is_pre_board_ignition_pattern"] is True
    assert result["pre_board_ignition_source"] == "pre_board_probe_wash"
    assert result["pre_board_ignition_stats"]["terminal_volume_ratio"] <= 0.30
    assert result["pre_board_ignition_stats"]["pre_board_long_cycle_ready"] is True
    assert result["pre_board_ignition_stats"]["pre_board_long_cycle_ready_reason"] in {
        "long_cycle_regime",
        "ordered_probe_sequence",
    }


def test_pre_board_ignition_detects_medium_term_momentum_shakeout_as_watch_only():
    closes = [10.0 + index * 0.02 for index in range(111)]
    closes.extend([12.4, 12.65, 12.9, 13.75, 14.0, 14.25, 14.55, 14.8, 15.05])
    closes.append(14.96)
    highs = [value * 1.012 for value in closes]
    lows = [value * 0.988 for value in closes]
    volumes = [100.0] * len(closes)
    volumes[-8] = 190.0
    volumes[-1] = 78.0

    shape = _score_momentum_shakeout_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-0.6,
        turnover=8.0,
    )
    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-0.6,
        turnover=8.0,
        volume_ratio=0.78,
    )

    assert shape["is_momentum_shakeout_pattern"] is True
    assert shape["momentum_shakeout_stats"]["prediction_only"] is True
    assert result["is_pre_board_ignition_pattern"] is True
    assert result["pre_board_ignition_source"] == "pre_board_momentum_shakeout"


def test_pre_board_ignition_detects_compressed_seed_before_limit_up():
    closes = [
        10.0, 10.1, 10.05, 10.12, 10.08,
        10.15, 10.18, 10.11, 10.2, 10.24,
        10.18, 10.28, 10.3, 10.26, 10.34,
        10.32, 10.38, 10.42, 10.4, 10.48,
        10.5, 10.56, 10.54, 10.62, 10.7,
    ]
    highs = [round(close * 1.012, 2) for close in closes]
    lows = [round(close * 0.988, 2) for close in closes]
    volumes = [
        200, 210, 205, 198, 202,
        196, 190, 188, 185, 182,
        180, 176, 172, 168, 165,
        160, 158, 155, 150, 148,
        146, 142, 138, 134, 132,
    ]

    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=1.2,
        turnover=2.5,
        volume_ratio=0.7,
    )

    assert result["is_pre_board_ignition_pattern"] is True
    assert result["pre_board_ignition_source"] == "pre_board_compression"
    assert result["pre_board_ignition_label"] == "连板前兆-压缩蓄势"


def test_pre_board_ignition_detects_probe_breakout_before_limit_up():
    closes = [
        20.0, 20.1, 19.9, 20.2, 20.0,
        20.3, 20.4, 20.2, 20.5, 20.6,
        20.4, 20.8, 21.0, 20.9, 21.2,
        21.1, 21.4, 21.8, 22.2, 22.0,
        22.6, 23.2, 23.0, 24.1, 25.2,
    ]
    highs = [round(close * 1.018, 2) for close in closes]
    lows = [round(close * 0.985, 2) for close in closes]
    volumes = [
        120, 118, 122, 121, 119,
        125, 128, 126, 130, 132,
        128, 135, 140, 138, 142,
        145, 150, 165, 170, 155,
        190, 210, 180, 230, 260,
    ]

    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=4.6,
        turnover=8.0,
        volume_ratio=1.8,
    )

    assert result["is_pre_board_ignition_pattern"] is True
    assert result["pre_board_ignition_source"] == "pre_board_probe_breakout"
    assert "温和放量" in result["pre_board_ignition_tags"]


def test_pre_board_ignition_detects_120d_low_base_before_limit_up():
    closes = (
        [round(20 - index * 0.13, 2) for index in range(60)]
        + [round(12.2 - index * 0.045, 2) for index in range(40)]
        + [
            10.2, 10.55, 10.35, 10.75, 10.5,
            10.42, 10.7, 10.55, 10.92, 10.7,
            10.62, 10.95, 10.8, 11.18, 10.95,
            10.88, 11.12, 11.0, 11.35, 11.78,
        ]
    )
    highs = [round(close * 1.015, 2) for close in closes]
    lows = [round(close * 0.985, 2) for close in closes]
    volumes = (
        [220] * 60
        + [150] * 40
        + [
            130, 135, 128, 155, 132,
            125, 150, 130, 160, 135,
            126, 165, 138, 175, 142,
            134, 180, 145, 210, 240,
        ]
    )

    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=3.8,
        turnover=4.0,
        volume_ratio=1.6,
    )

    stats = result["pre_board_ignition_stats"]
    assert result["is_pre_board_ignition_pattern"] is True
    assert result["pre_board_ignition_source"] == "pre_board_long_base"
    assert stats["sample_days"] == 120
    assert stats["pos_120"] <= 0.30
    assert stats["range_tightening_ratio"] <= 0.55
    assert stats["strong_probe_days_60"] >= 3
    assert 0 < stats["support"] < stats["resistance"]


def test_pre_board_120d_low_base_rejects_falling_knife_without_probe():
    closes = [round(20 - index * 0.08, 2) for index in range(120)]
    highs = [round(close * 1.01, 2) for close in closes]
    lows = [round(close * 0.99, 2) for close in closes]
    volumes = [220 - index for index in range(120)]

    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=-0.8,
        turnover=2.0,
        volume_ratio=0.8,
    )

    assert result["pre_board_ignition_source"] != "pre_board_long_base"
    assert result["is_pre_board_ignition_pattern"] is False


def test_pre_board_ignition_rejects_already_limit_up_or_overheated():
    closes = [
        10.0, 10.2, 10.4, 10.6, 10.9,
        11.2, 11.5, 11.9, 12.2, 12.6,
        13.0, 13.4, 13.9, 14.5, 15.2,
        16.0, 16.8, 17.6, 18.5, 19.5,
        20.5, 21.6, 22.8, 24.0, 26.4,
    ]
    highs = [round(close * 1.01, 2) for close in closes]
    lows = [round(close * 0.99, 2) for close in closes]
    volumes = [100 + i * 20 for i in range(len(closes))]

    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=10.0,
        turnover=25.0,
        volume_ratio=4.0,
    )

    assert result["is_pre_board_ignition_pattern"] is False
    assert result["pre_board_ignition_source"] == ""


def test_pre_board_ignition_rejects_recent_board_like_memory():
    closes = [
        20.0, 20.1, 20.0, 20.2, 20.1,
        20.3, 20.4, 20.5, 20.7, 20.8,
        22.7, 22.3, 22.0, 22.2, 22.4,
        22.5, 22.7, 22.9, 23.1, 23.0,
        23.2, 23.5, 23.4, 23.8, 24.4,
    ]
    highs = [round(close * 1.018, 2) for close in closes]
    lows = [round(close * 0.985, 2) for close in closes]
    volumes = [120 + i * 3 for i in range(len(closes))]

    result = _score_pre_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=4.6,
        turnover=8.0,
        volume_ratio=1.8,
    )

    assert result["is_pre_board_ignition_pattern"] is False
    assert result["pre_board_ignition_source"] == ""
    assert "已有涨停/类涨停明牌" in result["pre_board_ignition_blockers"][0]


def test_pick_plan_kline_pattern_prefers_backtested_edge_over_raw_score():
    pattern = _pick_plan_kline_pattern(
        main_wave={
            "is_main_wave_pattern": False,
            "main_wave_score": 0,
        },
        second_wave={
            "is_second_wave_pattern": False,
            "second_wave_score": 0,
        },
        trend_wave={
            "is_trend_main_wave_pattern": True,
            "trend_main_wave_score": 88,
            "trend_main_wave_label": "趋势主升加速跟踪",
            "trend_main_wave_tags": ["均线斜率向上"],
            "trend_main_wave_stats": {"pct_20": 30},
        },
        pre_board_wave={
            "is_pre_board_ignition_pattern": True,
            "pre_board_ignition_score": 76,
            "pre_board_ignition_label": "连板前兆-压缩蓄势",
            "pre_board_ignition_tags": ["压缩蓄势"],
            "pre_board_ignition_stats": {"pct_20": 12},
            "pre_board_ignition_source": "pre_board_compression",
        },
    )

    assert pattern["source"] == "pre_board_compression"
    assert pattern["score"] == 76


def test_second_wave_direct_buy_ready_requires_clean_reclaim_setup():
    assert _is_second_wave_direct_buy_ready({
        "days_after_break": 12,
        "break_down_pct": -18.0,
        "reclaim_peak_ratio": 0.9,
        "near_recent_high_ratio": 0.96,
        "post_up_days": 5,
        "vol_expansion": 1.7,
    }) is True

    assert _is_second_wave_direct_buy_ready({
        "days_after_break": 12,
        "break_down_pct": -18.0,
        "reclaim_peak_ratio": 0.9,
        "near_recent_high_ratio": 0.96,
        "post_up_days": 5,
        "vol_expansion": 0.9,
    }) is False


def test_backtested_low_edge_trend_source_is_downgraded_to_watch():
    plan = {
        "code": "000008",
        "candidate_source": "trend_main_wave_pattern",
        "main_wave_stats": {"pct_20": 30},
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert any("回测胜率偏低" in reason for reason in result["risk_warnings"])


def test_backtested_long_base_only_enters_intraday_confirmation_pool():
    plan = {
        "code": "000016",
        "candidate_source": "pre_board_long_base",
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert any("120日低位底座" in reason for reason in result["risk_warnings"])


def test_backtested_low_edge_second_wave_generic_is_observe_only():
    plan = {
        "code": "000009",
        "candidate_source": "second_wave_pattern",
        "main_wave_stats": {
            "days_after_break": 12,
            "break_down_pct": -18.0,
            "reclaim_peak_ratio": 0.9,
            "near_recent_high_ratio": 0.96,
            "post_up_days": 5,
            "vol_expansion": 1.7,
        },
        "strategies": [{"strategy_type": "main_wave_confirm", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert any("普通二波" in reason for reason in result["risk_warnings"])


def test_second_wave_reset_plan_exposes_detection_state_without_buy_position():
    plan = {
        "code": "000815",
        "candidate_source": "second_wave_reset_watch",
        "main_wave_stats": {
            "setup_state": "armed_second_wave_reset",
            "reset_type": "high_board_reset",
            "support": 13.70,
            "days_since_peak": 5,
            "drawdown_pct": -22.77,
            "max_board_streak": 4,
        },
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    strategy = result["strategies"][0]
    assert strategy["strategy_type"] == "watch"
    assert strategy["strategy_label"] == "⏱️ 二波检测中"
    assert strategy["entry_price_hint"] == "未触发前不买"
    assert strategy["position_ratio"] == "0"
    assert "13.70" in strategy["invalidation"]
    assert result["monitor_state"] == "armed_second_wave_reset"
    assert result["monitor_state_label"] == "二波检测中"
    assert "止跌急拉" in result["intraday_trigger"]


def test_backtested_low_edge_platform_breakout_is_observe_only():
    plan = {
        "code": "000014",
        "change_pct": 4.2,
        "candidate_source": "platform_breakout_pattern",
        "main_wave_stats": {
            "platform_range_pct": 12.0,
            "breakout_pct": 3.0,
            "pct_20": 18.0,
            "near_high_ratio": 0.96,
        },
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert any("平台突破" in reason for reason in result["risk_warnings"])


def test_backtested_low_edge_plain_rank_trend_is_observe_only():
    plan = {
        "code": "000015",
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert any("普通强势排行" in reason for reason in result["risk_warnings"])


def test_backtested_low_edge_pre_board_requires_low_dry_compression():
    plan = {
        "code": "000010",
        "change_pct": 4.8,
        "candidate_source": "pre_board_compression",
        "main_wave_stats": {
            "platform_range_pct": 16.0,
            "range_20_pct": 10.0,
            "pos_60": 0.86,
            "breakout_pct": 0.3,
            "pct_10": 18.0,
            "pct_20": 28.0,
            "dry_up_ratio": 0.95,
            "vol_today_ratio": 1.1,
            "board_like_count_20": 0,
        },
        "market_breadth": {
            "direct_buy_ok": True,
            "up_ratio": 0.58,
            "index_above_ma20": True,
        },
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert any("滚动60秒增量成交" in reason for reason in result["risk_warnings"])


def test_pre_board_compression_remains_zero_position_until_intraday_confirmation():
    plan = {
        "code": "000011",
        "change_pct": 1.2,
        "candidate_source": "pre_board_compression",
        "main_wave_score": 86,
        "main_wave_stats": {
            "platform_range_pct": 10.0,
            "range_20_pct": 14.0,
            "pos_60": 0.58,
            "breakout_pct": -1.2,
            "pct_10": 3.0,
            "pct_20": 8.0,
            "dry_up_ratio": 0.5,
            "vol_today_ratio": 0.72,
            "board_like_count_20": 0,
        },
        "market_breadth": {
            "direct_buy_ok": True,
            "up_ratio": 0.9,
            "avg_change": 1.6,
            "index_ret5": 4.2,
            "index_ret20": 6.0,
            "index_above_ma20": True,
            "index_above_ma60": True,
        },
        "price": 10.0,
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert any("准备态" in reason for reason in result["risk_warnings"])


def test_backtested_low_edge_pre_board_requires_market_breadth():
    plan = {
        "code": "000016",
        "change_pct": 1.2,
        "candidate_source": "pre_board_compression",
        "main_wave_stats": {
            "platform_range_pct": 10.0,
            "range_20_pct": 14.0,
            "pos_60": 0.58,
            "pct_10": 3.0,
            "pct_20": 8.0,
            "dry_up_ratio": 0.5,
            "vol_today_ratio": 0.72,
            "board_like_count_20": 0,
        },
        "market_breadth": {
            "direct_buy_ok": False,
            "up_ratio": 0.42,
            "avg_change": -0.2,
            "index_ret5": -1.1,
            "index_ret20": -3.0,
            "index_above_ma20": False,
        },
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"


def test_classify_market_breadth_marks_hot_bull_and_bear_regimes():
    hot = _classify_market_breadth(
        index_above_ma20=True,
        index_above_ma60=True,
        up_ratio=0.9,
        avg_change=1.8,
        index_ret5=4.5,
        index_ret20=6.0,
    )
    assert hot["market_regime"] == "hot"
    assert hot["direct_buy_ok"] is True
    assert hot["direct_buy_blockers"] == []

    bull = _classify_market_breadth(
        index_above_ma20=True,
        index_above_ma60=True,
        up_ratio=0.6,
        avg_change=0.3,
        index_ret5=1.0,
        index_ret20=2.0,
    )
    assert bull["market_regime"] == "bull"
    assert bull["direct_buy_ok"] is False
    assert any("上涨家数占比不足" in blocker for blocker in bull["direct_buy_blockers"])

    bear = _classify_market_breadth(
        index_above_ma20=False,
        index_above_ma60=False,
        up_ratio=0.3,
        avg_change=-1.4,
        index_ret5=-4.5,
        index_ret20=-8.0,
    )
    assert bear["market_regime"] == "bear"
    assert bear["direct_buy_ok"] is False
    assert "熊市/弱势环境" in bear["direct_buy_blockers"][0]


def test_backtested_low_edge_high_board_lockup_is_observe_only():
    plan = {
        "code": "000012",
        "change_pct": 8.8,
        "candidate_source": "high_board_lockup_acceleration",
        "main_wave_stats": {
            "pct_20": 18.0,
            "near_high_ratio": 0.96,
        },
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert any("缩量锁筹" in reason for reason in result["risk_warnings"])


def test_backtested_low_edge_high_board_turnover_second_wave_is_observe_only():
    plan = {
        "code": "000013",
        "change_pct": 1.2,
        "candidate_source": "high_board_turnover_second_wave",
        "main_wave_stats": {
            "break_down_pct": 16.0,
            "reclaim_peak_ratio": 0.9,
            "vol_expansion": 1.8,
            "max_board_streak": 3,
            "board_like_count_20": 3,
        },
        "strategies": [{"strategy_type": "trend", "position_ratio": "1/3仓"}],
        "avoid_reasons": [],
        "risk_warnings": [],
        "notes": [],
    }

    result = _downgrade_backtested_low_edge_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert any("高标换手二波" in reason for reason in result["risk_warnings"])


def test_next_day_diversified_top_keeps_actionable_rows_ahead_of_shape_buckets():
    rows = [
        {
            "code": "000001",
            "bull_score": 96,
            "candidate_source": "pre_board_compression",
            "strategies": [{"strategy_type": "avoid"}],
        },
        {
            "code": "000002",
            "bull_score": 82,
            "candidate_source": "trend_main_wave_pattern",
            "strategies": [{"strategy_type": "main_wave_confirm"}],
            "market_breadth": {"direct_buy_ok": True},
        },
        {
            "code": "000003",
            "bull_score": 80,
            "candidate_source": "main_wave_pattern",
            "strategies": [{"strategy_type": "trend"}],
            "market_breadth": {"direct_buy_ok": True},
        },
    ]

    result = _diversified_actionable_top(rows, 2)

    assert [row["code"] for row in result] == ["000002", "000003"]


def test_cap_daily_direct_buy_plans_keeps_only_top_two_direct_entries():
    rows = [
        {
            "code": f"00000{i}",
            "bull_score": 100 - i,
            "main_wave_score": 100 - i,
            "candidate_source": "pre_board_compression",
                "strategies": [{"strategy_type": "trend", "position_ratio": "1/4仓"}],
                "market_breadth": {"direct_buy_ok": True},
            "buy_signal": {"type": "trend", "status": "waiting_intraday_confirmation"},
            "avoid_reasons": [],
            "risk_warnings": [],
            "notes": [],
        }
        for i in range(1, 5)
    ]

    result = _cap_daily_direct_buy_plans(rows)

    assert [row["code"] for row in result] == ["000001", "000002", "000003", "000004"]
    assert [row["strategies"][0]["strategy_type"] for row in result[:2]] == ["trend", "trend"]
    assert result[2]["strategies"][0]["strategy_type"] == "watch"
    assert result[3]["strategies"][0]["strategy_type"] == "watch"
    assert "Top2回测上限" in result[2]["invalidation"]
    assert "buy_signal" in result[0]
    assert "buy_signal" not in result[2]


def test_cap_daily_direct_buy_plans_keeps_distinct_signal_families_first():
    rows = [
        {
            "code": "600001",
            "bull_score": 98,
            "candidate_source": "sector_repair_reversal",
            "strategies": [{"strategy_type": "repair_followup_buy"}],
            "market_breadth": {"direct_buy_ok": True},
            "buy_signal": {"type": "repair_followup_buy"},
        },
        {
            "code": "600002",
            "bull_score": 97,
            "candidate_source": "sector_repair_reversal",
            "strategies": [{"strategy_type": "repair_followup_buy"}],
            "market_breadth": {"direct_buy_ok": True},
            "buy_signal": {"type": "repair_followup_buy"},
        },
        {
            "code": "002130",
            "bull_score": 96,
            "candidate_source": "pre_board_probe_wash",
            "strategies": [{"strategy_type": "trend_pullback_buy"}],
            "market_breadth": {"direct_buy_ok": True},
            "buy_signal": {"type": "trend_pullback_buy"},
        },
    ]

    result = _cap_daily_direct_buy_plans(rows)
    strategy_by_code = {
        item["code"]: item["strategies"][0]["strategy_type"]
        for item in result
    }

    assert strategy_by_code["600001"] == "repair_followup_buy"
    assert strategy_by_code["002130"] == "trend_pullback_buy"
    assert strategy_by_code["600002"] == "watch"


def test_observe_only_plan_is_downgraded_to_watch_without_position():
    plan = {
        "code": "300001",
        "is_tradeable": False,
        "tag": "👁️ 仅观察",
        "strategies": [{
            "strategy_type": "main_wave_confirm",
            "strategy_label": "主升确认",
            "position_ratio": "1/2仓",
        }],
        "avoid_reasons": [],
        "notes": [],
    }

    result = _downgrade_observe_only_plan(plan)

    assert result["strategies"][0]["strategy_type"] == "watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert result["strategy"] == "watch"


def test_st_board_threshold_is_date_aware_after_2026_rule_change():
    assert _board_like_change_threshold(date(2026, 7, 3), is_st=True) == 4.5
    assert _board_like_change_threshold(date(2026, 7, 6), is_st=True) == 8.8
    assert _board_like_change_threshold(date(2026, 8, 26), is_st=False) == 8.8


def test_high_board_classifier_allows_loss_stock_and_separates_earnings_lane():
    classified = _classify_high_board_candidate({
        "code": "600001",
        "name": "亏损样本",
        "pe_ttm": -38.0,
        "net_profit_growth": -12.0,
        "turnover": 6.5,
        "volume_ratio": 1.8,
        "candidate_source": "high_board_theme_turnover",
        "top_signals": ["半年报减亏并预计扭亏"],
        "main_wave_stats": {
            "board_count": 2,
            "is_current_limit_up": True,
            "turnover": 6.5,
            "same_reason_limit_up_count": 3,
            "limit_up_reason": "半年报减亏",
        },
    })

    assert classified["is_high_board_candidate"] is True
    assert "theme_turnover" in classified["high_board_types"]
    assert "earnings_surprise" in classified["high_board_types"]
    assert classified["pe_ttm"] < 0


def test_capacity_trend_filter_excludes_large_low_turnover_single_board_but_not_relay():
    capacity = {
        "candidate_source": "high_board_earnings_surprise",
        "high_board_types": ["earnings_surprise"],
        "is_high_board_candidate": True,
        "circ_market_cap_billion": 900,
        "turnover": 3.5,
        "main_wave_stats": {"board_count": 1, "news_title": "半年报增长"},
    }
    relay = {
        **capacity,
        "circ_market_cap_billion": 120,
        "main_wave_stats": {"board_count": 3},
    }

    assert _is_capacity_trend_candidate(capacity) is True
    assert _is_capacity_trend_candidate(relay) is False


def test_high_board_watch_protocol_never_turns_yesterday_board_into_position():
    result = _apply_high_board_watch_protocol({
        "code": "002001",
        "name": "高标样本",
        "high_board_types": ["theme_turnover", "emotion_memory"],
        "high_board_type_labels": ["题材换手龙", "情绪记忆龙"],
        "main_wave_stats": {"board_count": 3, "turnover": 8.0},
        "strategies": [{"strategy_type": "aggressive", "position_ratio": "1/4仓"}],
    })

    assert result["strategies"][0]["strategy_type"] == "high_board_watch"
    assert result["strategies"][0]["position_ratio"] == "0"
    assert "昨日涨停" in result["risk_warnings"][-1]


def test_high_board_bull_market_exposes_only_after_trigger_light_position():
    result = _apply_high_board_watch_protocol({
        "code": "002001",
        "name": "高标样本",
        "is_tradeable": True,
        "high_board_types": ["theme_turnover"],
        "high_board_type_labels": ["题材换手龙"],
        "market_breadth": {
            "direct_buy_ok": False,
            "market_regime": "bull",
        },
        "main_wave_stats": {"board_count": 3, "turnover": 8.0},
        "strategies": [{"strategy_type": "aggressive", "position_ratio": "1/4仓"}],
    })

    assert result["strategies"][0]["position_ratio"] == "1/8仓"
    assert result["strategies"][0]["position_before_trigger"] == 0
    assert result["buy_signal"]["status"] == "waiting_auction_and_turnover_confirmation"
    assert result["buy_signal"]["position_after_trigger"] == "1/8仓"


def test_diversified_plan_caps_high_board_research_at_half_of_top20():
    high_board_rows = [
        {
            "code": f"600{i:03d}",
            "candidate_source": "high_board_theme_turnover",
            "high_board_types": ["theme_turnover"],
            "is_high_board_candidate": True,
            "plan_priority_score": 90 - i,
            "strategies": [{"strategy_type": "high_board_watch"}],
            "is_tradeable": True,
        }
        for i in range(14)
    ]
    ordinary_rows = [
        {
            "code": f"001{i:03d}",
            "candidate_source": "pre_board_probe_wash",
            "plan_priority_score": 80 - i,
            "strategies": [{"strategy_type": "watch"}],
            "is_tradeable": True,
        }
        for i in range(14)
    ]

    selected = _diversified_actionable_top([*high_board_rows, *ordinary_rows], 20)

    assert len(selected) == 20
    assert sum(bool(item.get("is_high_board_candidate")) for item in selected) == 10


def test_high_board_ignition_detects_platform_breakout_seed():
    closes = [
        10.0, 10.1, 10.2, 10.1, 10.3,
        10.4, 10.2, 10.5, 10.6, 10.4,
        10.7, 10.8, 10.6, 10.9, 11.0,
        10.8, 10.9, 11.1, 10.9, 11.0,
        11.2, 11.1, 11.3, 11.4, 12.55,
    ]
    highs = [round(close * 1.015, 2) for close in closes]
    lows = [round(close * 0.985, 2) for close in closes]
    volumes = [
        100, 98, 101, 99, 103,
        105, 100, 108, 110, 106,
        112, 114, 108, 116, 118,
        112, 114, 120, 113, 116,
        122, 118, 125, 130, 280,
    ]

    result = _score_high_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=9.8,
        turnover=8.5,
        volume_ratio=2.1,
    )

    assert result["is_high_board_ignition_pattern"] is True
    assert result["high_board_ignition_source"] == "high_board_platform_breakout"
    assert result["high_board_ignition_label"] == "连板起爆-平台突破"


def test_high_board_ignition_detects_lockup_acceleration():
    closes = [
        18.0, 18.2, 18.4, 18.7, 19.0,
        19.3, 19.6, 20.0, 20.3, 20.8,
        21.2, 21.8, 22.4, 23.0, 23.7,
        24.5, 25.2, 26.0, 26.8, 27.7,
        28.8, 30.0, 33.0, 36.3, 39.9,
    ]
    highs = [round(close * 1.005, 2) for close in closes]
    lows = [round(close * 0.985, 2) for close in closes]
    volumes = [
        100, 104, 106, 108, 110,
        115, 118, 120, 126, 130,
        138, 142, 146, 150, 154,
        160, 165, 168, 170, 172,
        160, 130, 90, 80, 70,
    ]

    result = _score_high_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=10.0,
        turnover=1.8,
        volume_ratio=0.4,
    )

    assert result["is_high_board_ignition_pattern"] is True
    assert result["high_board_ignition_source"] == "high_board_lockup_acceleration"
    assert "缩量锁筹" in result["high_board_ignition_tags"]


def test_high_board_ignition_detects_turnover_second_wave():
    closes = [
        10.0, 11.0, 12.1, 13.3, 14.6,
        16.0, 14.8, 13.9, 14.2, 14.6,
        15.1, 15.6, 16.2, 16.8, 17.4,
        18.0, 18.7, 19.2, 19.8, 20.4,
        21.0, 21.8, 22.5, 23.2, 24.0,
    ]
    highs = [round(close * 1.02, 2) for close in closes]
    lows = [round(close * 0.97, 2) for close in closes]
    volumes = [
        100, 180, 260, 340, 430,
        520, 480, 420, 390, 380,
        410, 450, 500, 540, 580,
        620, 680, 720, 760, 820,
        880, 940, 1010, 1080, 1160,
    ]

    result = _score_high_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=3.5,
        turnover=18.0,
        volume_ratio=1.4,
    )

    assert result["is_high_board_ignition_pattern"] is True
    assert result["high_board_ignition_source"] == "high_board_turnover_second_wave"
    assert "换手接力" in result["high_board_ignition_tags"]


def test_high_board_turnover_second_wave_requires_current_attack_confirmation():
    closes = [
        10.0, 11.0, 12.1, 13.3, 14.6,
        16.0, 14.8, 13.9, 14.2, 14.6,
        15.1, 15.6, 16.2, 16.8, 17.4,
        18.0, 18.7, 19.2, 19.8, 20.4,
        21.0, 21.8, 22.5, 23.2, 24.0,
    ]
    highs = [round(close * 1.02, 2) for close in closes]
    lows = [round(close * 0.97, 2) for close in closes]
    volumes = [100 + index * 45 for index in range(len(closes))]

    result = _score_high_board_ignition_kline_pattern(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        change_pct=0.5,
        turnover=18.0,
        volume_ratio=1.4,
    )

    assert result["high_board_ignition_source"] != "high_board_turnover_second_wave"
    assert "当日没有主动上攻确认" in result["high_board_ignition_blockers"]


@pytest.mark.asyncio
async def test_scan_market_anchors_limitup_and_sector_context_to_target_date(tenbagger_session):
    target_day = date(2026, 4, 23)
    future_day = date(2026, 4, 24)
    session = tenbagger_session
    session.add_all(
        [
            StockSpot(
                code="000001",
                name="目标股",
                price=11.0,
                open=10.5,
                high=11.0,
                low=10.2,
                limit_up=11.0,
                limit_down=9.0,
                change_pct=10.0,
                volume=1000000,
                turnover=8.0,
                volume_ratio=1.5,
            ),
            StockSectorMapping(
                code="000001",
                sector_code="BK001",
                sector_name="目标板块",
                sector_type="concept",
                source="test",
            ),
            LimitUpPool(
                code="000001",
                name="目标股",
                trade_date=target_day,
                consecutive_days=2,
                seal_amount=120000000,
            ),
            LimitUpPool(
                code="000001",
                name="目标股",
                trade_date=future_day,
                consecutive_days=5,
                seal_amount=500000000,
            ),
            SectorPersistence(
                sector_code="BK001",
                sector_name="目标板块",
                trade_date=target_day,
                consecutive_days=3,
                limit_up_count=1,
                fund_flow=2.5,
                change_pct=2.8,
                strength_score=75,
            ),
            SectorPersistence(
                sector_code="BK001",
                sector_name="目标板块",
                trade_date=future_day,
                consecutive_days=8,
                limit_up_count=6,
                fund_flow=12.0,
                change_pct=7.8,
                strength_score=98,
            ),
        ]
    )
    await session.commit()

    scanner = AnomalyScanner()
    events = await scanner.scan_market(session, min_score=50, target_date=target_day)
    limit_up = next(item for item in events if item.code == "000001" and item.event_type == "limit_up")

    assert limit_up.detail["consecutive_days"] == 2
    assert limit_up.detail["sector_factors"][0]["consecutive_days"] == 3
    assert limit_up.detail["sector_factors"][0]["strength_score"] == 75


@pytest.mark.asyncio
async def test_technical_indicators_ignore_kline_after_target_date(tenbagger_session):
    target_day = date(2026, 4, 23)
    session = tenbagger_session
    rows = []
    for index in range(6):
        trade_day = target_day - timedelta(days=5 - index)
        close = 10 + index
        rows.append(
            StockKline(
                code="000001",
                trade_date=trade_day,
                open=close - 0.2,
                close=close,
                high=close + 0.5,
                low=close - 0.5,
                volume=100000 + index,
            )
        )
    rows.append(
        StockKline(
            code="000001",
            trade_date=target_day + timedelta(days=1),
            open=100,
            close=100,
            high=101,
            low=99,
            volume=999999,
        )
    )
    session.add_all(rows)
    await session.commit()

    scanner = AnomalyScanner()
    tech = await scanner._calc_technical_indicators("000001", session, target_date=target_day)

    assert tech["ma5"] == 13.0


@pytest.mark.asyncio
async def test_intraday_repair_profile_excludes_current_trade_day_bar(tenbagger_session):
    target_day = date(2026, 7, 31)
    session = tenbagger_session
    rows = []
    for index in range(61):
        trade_day = target_day - timedelta(days=60 - index)
        close = 100.0 if trade_day == target_day else 10.0 + index * 0.01
        rows.append(StockKline(
            code="002354",
            trade_date=trade_day,
            open=close,
            close=close,
            high=close * 1.01,
            low=close * 0.99,
            volume=1_000_000,
        ))
    session.add_all(rows)
    await session.commit()

    scanner = AnomalyScanner()
    tech = await scanner._calc_technical_indicators("002354", session, target_date=target_day)

    assert len(tech["low_base_profile"]["closes"]) == 60
    assert tech["low_base_profile"]["closes"][-1] < 11.0


@pytest.mark.asyncio
async def test_scan_dragons_falls_back_to_stock_sector_mapping(tenbagger_session):
    target_day = date(2026, 4, 23)
    session = tenbagger_session
    session.add_all(
        [
            StockSpot(
                code="000001",
                name="龙头股",
                price=11.0,
                open=10.5,
                high=11.0,
                low=10.2,
                limit_up=11.0,
                limit_down=9.0,
                change_pct=10.0,
                volume=1000000,
                turnover=8.0,
                volume_ratio=1.5,
            ),
            LimitUpPool(
                code="000001",
                name="龙头股",
                trade_date=target_day,
                consecutive_days=2,
                seal_amount=120000000,
                limit_up_reason="目标板块订单落地",
            ),
            SectorInfo(
                sector_code="BK001",
                sector_name="目标板块",
                sector_type="concept",
                source="test",
                is_excluded=0,
            ),
            SectorInfo(
                sector_code="BK999",
                sector_name="融资融券",
                sector_type="concept",
                source="test",
                is_excluded=1,
            ),
            StockSectorMapping(
                code="000001",
                sector_code="BK001",
                sector_name="目标板块",
                sector_type="concept",
                source="test",
            ),
            StockSectorMapping(
                code="000001",
                sector_code="BK999",
                sector_name="融资融券",
                sector_type="concept",
                source="test",
            ),
        ]
    )
    await session.commit()

    scanner = AnomalyScanner()
    dragons = await scanner.scan_dragons(session, target_date=target_day)

    assert [item.sector_code for item in dragons] == ["BK001"]
    assert dragons[0].name == "龙头股"
    assert dragons[0].level == "dragon"


@pytest.mark.asyncio
async def test_scan_dragons_prefers_named_pywencai_sector_over_empty_shenwan_mapping(tenbagger_session):
    target_day = date(2026, 4, 23)
    session = tenbagger_session
    session.add_all(
        [
            StockSpot(
                code="000889",
                name="中嘉博创",
                price=11.0,
                open=10.5,
                high=11.0,
                low=10.2,
                limit_up=11.0,
                limit_down=9.0,
                change_pct=10.0,
                volume=1000000,
                turnover=8.0,
                volume_ratio=1.5,
            ),
            LimitUpPool(
                code="000889",
                name="中嘉博创",
                trade_date=target_day,
                consecutive_days=2,
                seal_amount=120000000,
                limit_up_reason="算力租赁订单落地",
            ),
            StockSectorMapping(
                code="000889",
                sector_code="pw_concept_算力租赁",
                sector_name="算力租赁",
                sector_type="concept",
                source="pywencai",
            ),
            StockSectorMapping(
                code="000889",
                sector_code="730103",
                sector_name="",
                sector_type="sw_l2",
                source="shenwan",
            ),
        ]
    )
    await session.commit()

    scanner = AnomalyScanner()
    dragons = await scanner.scan_dragons(session, target_date=target_day)

    assert [item.sector_code for item in dragons] == ["pw_concept_算力租赁"]
    assert dragons[0].sector_name == "算力租赁"


@pytest.mark.asyncio
async def test_scan_dragons_rejects_unrelated_concept_mapping(tenbagger_session):
    target_day = date(2026, 4, 23)
    session = tenbagger_session
    session.add_all(
        [
            StockSpot(
                code="000001",
                name="区域零售龙头",
                price=11.0,
                open=10.5,
                high=11.0,
                low=10.2,
                limit_up=11.0,
                limit_down=9.0,
                change_pct=10.0,
                volume=1000000,
                turnover=8.0,
                volume_ratio=1.5,
            ),
            LimitUpPool(
                code="000001",
                name="区域零售龙头",
                trade_date=target_day,
                consecutive_days=2,
                seal_amount=120000000,
                limit_up_reason="回购+新零售",
            ),
            StockSectorMapping(
                code="000001",
                sector_code="pw_concept_西部大开发",
                sector_name="西部大开发",
                sector_type="concept",
                source="pywencai",
            ),
            StockSectorMapping(
                code="000001",
                sector_code="pw_industry_零售",
                sector_name="零售",
                sector_type="industry",
                source="pywencai",
            ),
        ]
    )
    await session.commit()

    scanner = AnomalyScanner()
    dragons = await scanner.scan_dragons(session, target_date=target_day)

    assert dragons
    assert {item.sector_code for item in dragons} == {"pw_industry_零售"}


def test_should_scan_capital_uses_current_flow_pct_and_recent_funds():
    scanner = AnomalyScanner()

    assert scanner._should_scan_capital(
        current_main_inflow=60_000_000,
        current_main_inflow_pct=8.5,
        recent_funds=[],
        volume_ratio=1.9,
        change_pct=2.8,
    ) is True

    assert scanner._should_scan_capital(
        current_main_inflow=0,
        current_main_inflow_pct=0.0,
        recent_funds=[
            {"main_net_inflow": 30_000_000},
            {"main_net_inflow": 25_000_000},
            {"main_net_inflow": 22_000_000},
        ],
        volume_ratio=0.9,
        change_pct=0.2,
    ) is True

    assert scanner._should_scan_capital(
        current_main_inflow=0,
        current_main_inflow_pct=0.0,
        recent_funds=[],
        volume_ratio=1.0,
        change_pct=0.5,
    ) is False


def test_build_stock_rows_groups_events_by_code_and_filters_track():
    anomalies = [
        {
            "code": "000001",
            "name": "测试股A",
            "event_type": "capital",
            "score": 88,
            "description": "资金净额 2.0亿",
            "setup_grade": "A1 可直接执行",
            "setup_grade_display": "A1-趋势/资金型",
            "setup_track": "趋势/资金型",
            "feishu_pushable": True,
            "risk_flags": [],
            "a1_blockers": [],
            "detail": {
                "price": 12.3,
                "change_pct": 2.6,
                "main_net_inflow": 200_000_000,
                "main_net_inflow_pct": 12.2,
                "as_of": "2026-04-15T10:00:00",
            },
        },
        {
            "code": "000001",
            "name": "测试股A",
            "event_type": "breakthrough",
            "score": 80,
            "description": "突破60日新高",
            "setup_grade": "A2 盘口确认后执行",
            "setup_grade_display": "A2 盘口确认后执行",
            "setup_track": "",
            "feishu_pushable": False,
            "risk_flags": [],
            "a1_blockers": ["等待二次确认"],
            "detail": {
                "price": 12.3,
                "change_pct": 2.6,
                "as_of": "2026-04-15T10:01:00",
            },
        },
        {
            "code": "000002",
            "name": "测试股B",
            "event_type": "limit_up",
            "score": 86,
            "description": "2连板涨停",
            "setup_grade": "A1 可直接执行",
            "setup_grade_display": "A1-打板型",
            "setup_track": "打板型",
            "feishu_pushable": True,
            "risk_flags": [],
            "a1_blockers": [],
            "detail": {
                "price": 9.8,
                "change_pct": 9.9,
                "as_of": "2026-04-15T10:02:00",
            },
        },
    ]

    rows = _build_stock_rows(anomalies, setup_track="趋势/资金型", sort_by="priority")

    assert len(rows) == 1
    assert rows[0]["code"] == "000001"
    assert rows[0]["event_count"] == 2
    assert set(rows[0]["event_types"]) == {"capital", "breakthrough"}


def test_build_stock_rows_supports_server_side_sorting():
    anomalies = [
        {
            "code": "000001",
            "name": "测试股A",
            "event_type": "capital",
            "score": 78,
            "description": "资金净额 1.0亿",
            "setup_grade": "A2 盘口确认后执行",
            "setup_grade_display": "A2 盘口确认后执行",
            "setup_track": "",
            "feishu_pushable": False,
            "risk_flags": [],
            "a1_blockers": [],
            "detail": {
                "main_net_inflow": 100_000_000,
                "change_pct": 1.2,
                "as_of": "2026-04-15T10:00:00",
            },
        },
        {
            "code": "000002",
            "name": "测试股B",
            "event_type": "capital",
            "score": 76,
            "description": "资金净额 3.0亿",
            "setup_grade": "B类观察候选",
            "setup_grade_display": "B类观察候选",
            "setup_track": "",
            "feishu_pushable": False,
            "risk_flags": [],
            "a1_blockers": [],
            "detail": {
                "main_net_inflow": 300_000_000,
                "change_pct": 0.8,
                "as_of": "2026-04-15T09:59:00",
            },
        },
    ]

    rows = _build_stock_rows(anomalies, sort_by="net_inflow")

    assert [row["code"] for row in rows] == ["000002", "000001"]


# === 安全买入边际（K线追高过滤） ===


def test_derive_kline_boundary_computes_bias_distance_and_returns():
    tech = {
        "ma20": 10.0,
        "ma60": 9.0,
        "high_20d": 11.0,
        "high_60d": 12.0,
        "high_120d": 13.0,
        "return_20d": 15.0,
        "recent_closes": [8.0, 8.2, 8.5, 8.8, 9.0, 9.5],
    }
    boundary = _derive_kline_boundary(tech, 10.5)
    assert boundary["bias_to_ma20_pct"] == pytest.approx(5.0)
    assert boundary["bias_to_ma60_pct"] == pytest.approx(16.67)
    assert boundary["dist_to_high_20d_pct"] == pytest.approx(-4.55)
    assert boundary["dist_to_high_60d_pct"] == pytest.approx(-12.5)
    assert boundary["dist_to_high_120d_pct"] == pytest.approx(-19.23)
    assert boundary["return_20d"] == pytest.approx(15.0)
    assert boundary["return_5d"] == pytest.approx(31.25)


def test_derive_kline_boundary_returns_empty_on_nonpositive_price():
    assert _derive_kline_boundary({"ma20": 10.0}, 0) == {}


def test_derive_kline_boundary_missing_fields_do_not_crash():
    boundary = _derive_kline_boundary({}, 10.0)
    assert "bias_to_ma20_pct" not in boundary
    assert "bias_to_ma60_pct" not in boundary
    assert boundary.get("return_20d") == 0.0


def test_kline_chase_reasons_flags_every_overextended_boundary():
    detail = {
        "bias_to_ma20_pct": 9.0,
        "bias_to_ma60_pct": 20.0,
        "dist_to_high_60d_pct": 0.0,
        "return_5d": 15.0,
        "return_20d": 30.0,
    }
    reasons = _kline_chase_reasons(detail)
    assert len(reasons) == 5
    assert any("MA20" in r for r in reasons)
    assert any("MA60" in r for r in reasons)
    assert any("60日高点" in r for r in reasons)
    assert any("近5日" in r for r in reasons)
    assert any("近20日" in r for r in reasons)


def test_kline_chase_reasons_passes_safe_boundaries():
    detail = {
        "bias_to_ma20_pct": 3.0,
        "bias_to_ma60_pct": 10.0,
        "dist_to_high_60d_pct": -8.0,
        "return_5d": 6.0,
        "return_20d": 12.0,
    }
    assert _kline_chase_reasons(detail) == []


def test_kline_chase_reasons_ignores_missing_fields():
    assert _kline_chase_reasons({}) == []


def test_is_chase_type_anomaly_only_targets_momentum_types():
    assert _is_chase_type_anomaly({"event_type": "capital", "detail": {}}) is True
    assert _is_chase_type_anomaly({"event_type": "limit_up", "detail": {}}) is True
    assert _is_chase_type_anomaly({"event_type": "pump_dump", "detail": {}}) is True
    assert _is_chase_type_anomaly({"event_type": "breakthrough", "detail": {"signal_type": ""}}) is True
    assert _is_chase_type_anomaly({"event_type": "breakthrough", "detail": {"signal_type": "momentum_burst"}}) is True
    assert _is_chase_type_anomaly(
        {"event_type": "breakthrough", "detail": {"signal_type": "leader_linkage_confirmation"}}
    ) is False
    assert _is_chase_type_anomaly({"event_type": "low_absorb", "detail": {}}) is False


def test_build_risk_flags_adds_high_position_chase_danger_flag():
    flags = _build_risk_flags({"bias_to_ma20_pct": 9.5})
    codes = [flag["code"] for flag in flags]
    assert "high_position_chase_risk" in codes
    chase_flag = next(flag for flag in flags if flag["code"] == "high_position_chase_risk")
    assert chase_flag["level"] == "danger"
    assert chase_flag["label"] == "高位追涨风险"


def test_build_risk_flags_empty_when_no_risk():
    assert _build_risk_flags({}) == []


def test_chase_filter_toggle_gates_kline_reasons(monkeypatch):
    """总开关关闭时回到旧算法，不应用任何日K追高拦截。"""
    from app.api.v1 import tenbagger as tb

    calls: list[dict] = []

    def _spy(detail: dict) -> list[str]:
        calls.append(detail)
        return []

    monkeypatch.setattr("app.api.v1.tenbagger._kline_chase_reasons", _spy)
    entry = {
        "anomaly": {
            "event_type": "capital",
            "detail": {"bias_to_ma20_pct": 9.0},
        },
        "event_type": "capital",
        "setup_grade": "A1 可直接执行",
    }

    monkeypatch.setattr(tb.settings, "ANOMALY_CHASE_FILTER_ENABLED", True)
    _build_buypoint_continuity_context(entry, [])
    assert len(calls) == 1

    calls.clear()
    monkeypatch.setattr(tb.settings, "ANOMALY_CHASE_FILTER_ENABLED", False)
    _build_buypoint_continuity_context(entry, [])
    assert len(calls) == 0
