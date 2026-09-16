from datetime import date

from app.data.scheduler import _next_sector_active_days
from app.sector.lifecycle import LifecycleState, SectorLifecycleData, SectorLifecycleEngine


def make_data(**kwargs) -> SectorLifecycleData:
    base = dict(
        sector_code="pw_concept_ai",
        sector_name="人工智能",
        sector_type="concept",
        trade_date=date(2026, 4, 14),
    )
    base.update(kwargs)
    return SectorLifecycleData(**base)


def test_active_days_only_continue_when_today_still_active():
    assert _next_sector_active_days(3, 2, 1.5) == 4
    assert _next_sector_active_days(3, 0, 2.0) == 4
    assert _next_sector_active_days(3, 0, -1.0) == 0


def test_dormant_not_overridden_by_one_day():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=0,
        consecutive_board_count=0,
        max_board_height=0,
        fund_flow=0,
        active_days=0,
    )
    assert engine._determine_state(data) == LifecycleState.DORMANT


def test_dormant_must_not_capture_active_structure():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=7,
        consecutive_board_count=1,
        max_board_height=6,
        fund_flow=1.2,
        active_days=0,
    )
    assert engine._determine_state(data) == LifecycleState.EMERGING


def test_attributed_limit_up_cluster_is_emerging_when_sector_fund_is_missing():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=3,
        first_board_count=3,
        consecutive_board_count=0,
        max_board_height=1,
        fund_flow=0,
        active_days=0,
    )
    assert engine._determine_state(data) == LifecycleState.EMERGING


def test_negative_fund_with_remaining_structure_falls_to_diverging():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=6,
        consecutive_board_count=2,
        max_board_height=4,
        limit_down_count=1,
        fund_flow=-24,
        active_days=0,
    )
    assert engine._determine_state(data) == LifecycleState.DIVERGING


def test_broad_first_board_cluster_with_sector_outflow_is_diverging_not_dormant():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=4,
        first_board_count=4,
        consecutive_board_count=0,
        max_board_height=1,
        fund_flow=-8,
        active_days=0,
    )
    assert engine._determine_state(data) == LifecycleState.DIVERGING


def test_high_leader_with_weak_acceptance_prefers_diverging_over_emerging():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=7,
        first_board_count=5,
        consecutive_board_count=2,
        max_board_height=6,
        limit_down_count=1,
        fund_flow=-2,
        active_days=0,
        persistence_consecutive_days=1,
        kline_trend="sideways",
    )
    assert engine._determine_state(data) == LifecycleState.DIVERGING


def test_solo_high_leader_with_too_many_first_boards_prefers_diverging():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=7,
        first_board_count=6,
        consecutive_board_count=1,
        max_board_height=6,
        limit_down_count=0,
        fund_flow=1.2,
        active_days=0,
        persistence_consecutive_days=1,
        kline_trend=None,
    )
    assert engine._determine_state(data) == LifecycleState.DIVERGING


def test_diverging_to_declining_when_leader_breaks_and_capital_worsens():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.DIVERGING,
        limit_up_count=1,
        first_board_count=1,
        consecutive_board_count=0,
        max_board_height=1,
        limit_down_count=2,
        fund_flow=-20,
        fund_flow_3d=-30,
        kline_trend="down",
    )
    assert engine._determine_state(data) == LifecycleState.DECLINING


def test_three_limit_downs_without_prior_hot_state_need_other_weakness_to_decline():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=2,
        first_board_count=2,
        consecutive_board_count=0,
        max_board_height=1,
        limit_down_count=3,
        fund_flow=4.16,
        fund_flow_3d=-18.85,
        persistence_consecutive_days=1,
        kline_trend=None,
    )
    assert engine._determine_state(data) == LifecycleState.EMERGING


def test_reason_matching_uses_sector_keywords_for_primary_attribution():
    engine = SectorLifecycleEngine()
    assert engine._reason_matches_sector("房地产开发+新型城镇化", "新型城镇化") is True
    assert engine._reason_matches_sector("专业工程", "新型城镇化") is False
    assert engine._reason_matches_sector("创新药+中药+中报预增", "医药生物-中药-中药Ⅲ") is True
    assert engine._reason_matches_sector("种植业", "农业种植") is True
    assert engine._reason_matches_sector("种子生产", "玉米") is True
    assert engine._reason_matches_sector("种植业", "粮食概念") is True
    assert engine._reason_matches_sector("百货零售", "农业种植") is False


def test_theme_mapping_can_attribute_industry_like_reason_to_concept():
    engine = SectorLifecycleEngine()
    assert engine._theme_matches_sector("其他电子", "数据中心") is True
    assert engine._theme_matches_sector("电池", "钠离子电池") is True
    assert engine._theme_matches_sector("养殖业", "猪肉") is True
    assert engine._theme_matches_sector("包装印刷", "芯片概念") is False


def test_blank_limit_down_mapping_without_prior_hot_state_should_not_be_full_counted():
    engine = SectorLifecycleEngine()

    class Dummy:
        def __init__(self, code, reason=""):
            self.code = code
            self.reason = reason

    entries = [Dummy("000001", ""), Dummy("000002", ""), Dummy("000003", "")]
    filtered = engine._filter_attributed_entries(
        entries,
        "新型城镇化",
        lambda item: item.reason,
        allow_blank_mapped=False,
    )
    assert len(filtered) == 0


def test_leader_resonance_can_keep_top_samples_for_strong_sector():
    engine = SectorLifecycleEngine()

    class Dummy:
        def __init__(self, code, reason="", consecutive_days=1, seal_amount=0):
            self.code = code
            self.reason = reason
            self.consecutive_days = consecutive_days
            self.seal_amount = seal_amount

    entries = [
        Dummy("000001", "其他电子", consecutive_days=4, seal_amount=80_000_000),
        Dummy("000002", "包装印刷", consecutive_days=1, seal_amount=2_000_000),
    ]
    filtered = engine._filter_attributed_entries(
        entries,
        "数据中心",
        lambda item: item.reason,
        allow_blank_mapped=True,
        sector_strength=78,
        sector_fund_flow=30,
    )
    assert [item.code for item in filtered] == ["000001"]


def test_st_stock_name_is_excluded_from_lifecycle_pool():
    engine = SectorLifecycleEngine()
    assert engine._is_excluded_stock("600381", "*ST春天", set()) is True
    assert engine._is_excluded_stock("002051", "中工国际", set()) is False


def test_one_day_requires_previous_emerging_and_no_follow_through():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.EMERGING,
        limit_up_count=0,
        consecutive_board_count=0,
        max_board_height=0,
        fund_flow=-2,
        active_days=1,
    )
    assert engine._determine_state(data) == LifecycleState.ONE_DAY


def test_accelerating_requires_structure_and_continuity():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.EMERGING,
        limit_up_count=6,
        consecutive_board_count=3,
        max_board_height=3,
        fund_flow=6,
        active_days=1,
        kline_trend="up",
    )
    assert engine._determine_state(data) == LifecycleState.ACCELERATING


def test_accelerating_allows_explosive_structure_without_kline_data():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.EMERGING,
        limit_up_count=4,
        first_board_count=3,
        consecutive_board_count=2,
        max_board_height=4,
        fund_flow=30.45,
        active_days=1,
        total_active_5d=2,
        kline_trend=None,
        kline_close=None,
        kline_ma5=None,
        kline_ma20=None,
    )
    assert engine._determine_state(data) == LifecycleState.ACCELERATING


def test_accelerating_allows_first_day_explosive_breakout():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=12,
        consecutive_board_count=4,
        max_board_height=4,
        fund_flow=18,
        active_days=0,
        persistence_consecutive_days=1,
        kline_trend="up",
    )
    assert engine._determine_state(data) == LifecycleState.ACCELERATING


def test_emerging_not_upgraded_to_accelerating_when_width_is_strong_but_height_not_enough():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.EMERGING,
        limit_up_count=6,
        first_board_count=5,
        consecutive_board_count=1,
        max_board_height=2,
        fund_flow=18,
        active_days=1,
        total_active_5d=2,
        kline_trend=None,
    )
    assert engine._determine_state(data) == LifecycleState.EMERGING


def test_accelerating_requires_kline_confirmation():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.EMERGING,
        limit_up_count=7,
        consecutive_board_count=3,
        max_board_height=4,
        fund_flow=9,
        active_days=1,
        persistence_consecutive_days=2,
        kline_trend="sideways",
        kline_close=99,
        kline_ma5=100,
        kline_ma20=101,
        limit_down_count=1,
    )
    assert engine._determine_state(data) == LifecycleState.DIVERGING


def test_emerging_allows_broad_first_day_but_incomplete_ladder():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=7,
        consecutive_board_count=2,
        max_board_height=2,
        fund_flow=12,
        active_days=0,
        persistence_consecutive_days=1,
        kline_trend="up",
    )
    assert engine._determine_state(data) == LifecycleState.EMERGING


def test_broad_start_with_kline_breakdown_is_diverging_not_emerging():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=6,
        consecutive_board_count=1,
        max_board_height=2,
        fund_flow=8,
        active_days=0,
        persistence_consecutive_days=1,
        kline_trend="down",
        kline_close=98,
        kline_ma5=100,
        kline_ma20=102,
    )
    assert engine._determine_state(data) == LifecycleState.DIVERGING


def test_emerging_single_low_board_requires_positive_fund_and_basic_momentum():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=None,
        limit_up_count=1,
        consecutive_board_count=0,
        max_board_height=1,
        fund_flow=0.3,
        strength_score=18,
        change_pct=0.2,
        active_days=0,
        persistence_consecutive_days=1,
        kline_trend=None,
    )
    assert engine._determine_state(data) == LifecycleState.DORMANT


def test_climax_requires_batch_limit_up_and_high_leader():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.ACCELERATING,
        limit_up_count=12,
        consecutive_board_count=6,
        max_board_height=5,
        fund_flow=8,
        active_days=2,
        kline_trend="breakout_up",
        kline_vol_ratio=1.3,
    )
    assert engine._determine_state(data) == LifecycleState.CLIMAX


def test_climax_rejects_when_kline_lacks_overheat_confirmation():
    engine = SectorLifecycleEngine()
    data = make_data(
        prev_state=LifecycleState.ACCELERATING,
        limit_up_count=12,
        consecutive_board_count=6,
        max_board_height=5,
        fund_flow=8,
        active_days=2,
        kline_trend="sideways",
        kline_vol_ratio=0.8,
        kline_close=100,
        kline_ma5=100,
    )
    assert engine._determine_state(data) == LifecycleState.ACCELERATING


def test_main_line_rejects_solo_high_leader_without_followers():
    engine = SectorLifecycleEngine()
    data = make_data(
        lifecycle_state=LifecycleState.ACCELERATING,
        limit_up_count=7,
        first_board_count=6,
        consecutive_board_count=1,
        max_board_height=6,
        fund_flow=8,
        total_active_5d=4,
        persistence_consecutive_days=4,
        quality_score=75,
        ladders=[{"height": 6, "stocks": [{"code": "000001"}]}, {"height": 1, "stocks": [{"code": "000002"}]}],
    )
    assert engine._is_main_line(data) is False


def test_main_line_status_strengthening_for_accelerating_core_line():
    engine = SectorLifecycleEngine()
    data = make_data(
        lifecycle_state=LifecycleState.ACCELERATING,
        limit_up_count=12,
        first_board_count=7,
        consecutive_board_count=4,
        max_board_height=5,
        fund_flow=18,
        total_active_5d=4,
        persistence_consecutive_days=3,
        quality_score=85,
        ladders=[
            {"height": 5, "stocks": [{"code": "000001"}]},
            {"height": 3, "stocks": [{"code": "000002"}]},
            {"height": 1, "stocks": [{"code": "000003"}]},
        ],
    )
    assert engine._main_line_status(data) == "strengthening"


def test_main_line_status_diverging_for_still_active_but_weakening_line():
    engine = SectorLifecycleEngine()
    data = make_data(
        lifecycle_state=LifecycleState.DIVERGING,
        limit_up_count=6,
        first_board_count=3,
        consecutive_board_count=2,
        max_board_height=4,
        fund_flow=2,
        total_active_5d=4,
        persistence_consecutive_days=3,
        quality_score=72,
        ladders=[
            {"height": 4, "stocks": [{"code": "000001"}]},
            {"height": 2, "stocks": [{"code": "000002"}]},
        ],
    )
    assert engine._main_line_status(data) == "diverging"
