"""急速拉升检测的精度、量价同窗和诱多拦截边界测试。"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.signal.anomaly_scanner import AnomalyScanner
from app.data.sources.tencent_source import TencentSource


def _spot(
    *,
    code: str = "002580",
    price: float = 100.0,
    amount: float = 30_000_000,
    change_pct: float = 0.0,
    high: float | None = None,
    avg_price: float | None = None,
    volume_ratio: float = 1.1,
    support_strength_score: float = 75.0,
    orderbook_imbalance: float = 0.20,
    bid_depth_5: float = 60_000,
    ask_depth_5: float = 30_000,
    withdrawal_ratio: float = 0.03,
) -> SimpleNamespace:
    return SimpleNamespace(
        code=code,
        name="急拉精度测试股",
        price=price,
        prev_close=100.0,
        open=99.8,
        high=high if high is not None else price,
        low=99.5,
        avg_price=avg_price if avg_price is not None else price * 0.998,
        change_pct=change_pct,
        min5_change=0.0,
        volume_ratio=volume_ratio,
        turnover=2.0,
        amplitude=1.5,
        volume=200_000,
        amount=amount,
        limit_up=110.0,
        limit_down=90.0,
        support_strength_score=support_strength_score,
        orderbook_imbalance=orderbook_imbalance,
        bid_depth_5=bid_depth_5,
        ask_depth_5=ask_depth_5,
        withdrawal_ratio=withdrawal_ratio,
    )


def _positive_fund() -> dict:
    return {
        "main_net_inflow": 80_000_000,
        "main_net_inflow_pct": 3.5,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-14 10:01:00",
    }


def _lagging_negative_fund() -> dict:
    return {
        "main_net_inflow": -300_000_000,
        "main_net_inflow_pct": -6.0,
        "source": "eastmoney_main_fund",
        "is_stale": False,
        "as_of": "2026-08-14 10:01:00",
    }


def _positive_sector() -> dict:
    return {
        "sector_factors": [
            {
                "sector_name": "储能",
                "fund_flow": 8.0,
                "change_pct": 1.4,
                "strength_score": 72,
                "limit_up_count": 2,
            }
        ]
    }


def _track_exact_60s(
    scanner: AnomalyScanner,
    spot: SimpleNamespace,
    *,
    target_change_pct: float,
    target_amount: float = 32_000_000,
) -> dict:
    scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 14, 10, 0, 0),
    )
    spot.price = 100.0 * (1.0 + target_change_pct / 100.0)
    spot.change_pct = target_change_pct
    spot.high = max(spot.high, spot.price)
    spot.avg_price = spot.price * 0.998
    spot.amount = target_amount
    return scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 14, 10, 1, 0),
    )


@pytest.mark.parametrize(
    ("change_pct", "expected_tier"),
    [(0.30, "watch"), (0.60, "medium"), (1.00, "strong")],
)
def test_rolling_60s_exact_thresholds_are_inclusive(
    change_pct: float,
    expected_tier: str,
):
    """报价浮点误差不能让恰好到达0.3/0.6/1.0%的信号漏报。"""
    scanner = AnomalyScanner()
    spot = _spot(code=f"threshold-{change_pct}")

    result = _track_exact_60s(
        scanner,
        spot,
        target_change_pct=change_pct,
    )

    assert result["rolling_60s_change_pct"] == pytest.approx(change_pct)
    assert result["rolling_60s_confirmed"] is True
    assert result["rolling_60s_tier"] == expected_tier


@pytest.mark.parametrize(
    ("change_pct", "expected_tier"),
    [(0.299, ""), (0.599, "watch"), (0.999, "medium")],
)
def test_rolling_60s_values_below_tier_boundaries_do_not_round_up(
    change_pct: float,
    expected_tier: str,
):
    scanner = AnomalyScanner()
    spot = _spot(code=f"below-{change_pct}")

    result = _track_exact_60s(
        scanner,
        spot,
        target_change_pct=change_pct,
    )

    assert result["rolling_60s_tier"] == expected_tier
    assert result["rolling_60s_confirmed"] is (change_pct >= 0.30)


def test_rolling_60s_price_and_amount_keep_the_same_baseline_snapshot():
    scanner = AnomalyScanner()
    spot = _spot(price=100.0, amount=30_000_000)
    scanner._track_rolling_60s_momentum(spot, datetime(2026, 8, 14, 10, 0, 0))

    spot.price = 100.4
    spot.amount = 35_000_000
    scanner._track_rolling_60s_momentum(spot, datetime(2026, 8, 14, 10, 0, 30))

    spot.price = 100.6
    spot.amount = 38_000_000
    result = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 14, 10, 1, 0),
    )

    assert result["rolling_60s_change_pct"] == pytest.approx(0.6)
    assert result["rolling_60s_amount_delta"] == pytest.approx(8_000_000)
    assert result["rolling_60s_interval_sec"] == pytest.approx(60.0)
    assert result["rolling_60s_confirmed"] is True


def test_rolling_60s_rejects_one_percent_price_spike_without_incremental_volume():
    scanner = AnomalyScanner()
    spot = _spot(price=100.0, amount=30_000_000)

    result = _track_exact_60s(
        scanner,
        spot,
        target_change_pct=1.0,
        target_amount=30_900_000,
    )

    assert result["rolling_60s_change_pct"] == pytest.approx(1.0)
    assert result["rolling_60s_amount_delta"] == pytest.approx(900_000)
    assert result["rolling_60s_tier"] == "strong"
    assert result["rolling_60s_confirmed"] is False


def test_rapid_rise_rejects_pullback_after_intrawindow_high():
    """窗口累计仍上涨时，若已从日内高点显著回落，也不能推成急拉买点。"""
    scanner = AnomalyScanner()
    spot = _spot(price=100.0, amount=30_000_000)
    scanner._track_rolling_60s_momentum(spot, datetime(2026, 8, 14, 10, 0, 0))
    spot.price = 101.50
    spot.amount = 31_000_000
    scanner._track_rolling_60s_momentum(spot, datetime(2026, 8, 14, 10, 0, 30))
    spot.price = 100.60
    spot.change_pct = 0.60
    spot.amount = 32_000_000
    rolling = scanner._track_rolling_60s_momentum(
        spot,
        datetime(2026, 8, 14, 10, 1, 0),
    )
    spot.high = 101.50
    spot.avg_price = 100.20
    spot._rolling_60s_momentum = rolling

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=_positive_fund(),
        sector_context=_positive_sector(),
        board_context={},
        reversal_state={"previous_change_pct": 0.2, "scan_change_pct": 0.4},
        today=date(2026, 8, 14),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert spot.price / spot.high < 0.992
    assert rolling["rolling_60s_path_confirmed"] is False
    assert rolling["rolling_60s_confirmed"] is False
    assert result is None


@pytest.mark.parametrize(
    "orderbook_patch",
    [
        {"orderbook_imbalance": -0.16},
        {"withdrawal_ratio": 0.15},
        {"bid_depth_5": 30_000, "ask_depth_5": 41_000},
    ],
)
def test_rapid_rise_rejects_negative_orderbook_or_withdrawal(orderbook_patch: dict):
    scanner = AnomalyScanner()
    spot = _spot()
    rolling = _track_exact_60s(scanner, spot, target_change_pct=0.60)
    spot._rolling_60s_momentum = rolling
    for field, value in orderbook_patch.items():
        setattr(spot, field, value)

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=_positive_fund(),
        sector_context=_positive_sector(),
        board_context={},
        reversal_state={"previous_change_pct": 0.2, "scan_change_pct": 0.4},
        today=date(2026, 8, 14),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is None


def test_first_watch_acceleration_needs_three_core_confirmations_to_push():
    """0.3%首次启动允许入检测池，但仅两项共振时不应进入飞书。"""
    scanner = AnomalyScanner()
    spot = _spot(support_strength_score=75, orderbook_imbalance=0.20)
    rolling = _track_exact_60s(scanner, spot, target_change_pct=0.30)
    spot._rolling_60s_momentum = rolling

    # 盘口+资金两项确认；板块为空，未达到0.3%档的三确认推送要求。
    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=_positive_fund(),
        sector_context={},
        board_context={},
        reversal_state={"previous_change_pct": 0.0, "scan_change_pct": 0.30},
        today=date(2026, 8, 14),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "60秒放量急拉预警"
    assert detail["rolling_60s_tier"] == "watch"
    assert detail["positive_acceleration_core_confirmation_count"] == 2
    assert detail["rolling_60s_alert_pushable"] is False


def test_first_watch_acceleration_pushes_after_three_core_confirmations():
    scanner = AnomalyScanner()
    spot = _spot(support_strength_score=75, orderbook_imbalance=0.20)
    rolling = _track_exact_60s(scanner, spot, target_change_pct=0.30)
    spot._rolling_60s_momentum = rolling

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=_positive_fund(),
        sector_context=_positive_sector(),
        board_context={},
        reversal_state={"previous_change_pct": 0.0, "scan_change_pct": 0.30},
        today=date(2026, 8, 14),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "60秒放量急拉预警"
    assert detail["rolling_60s_tier"] == "watch"
    assert detail["positive_acceleration_core_confirmation_count"] == 3
    assert detail["rolling_60s_alert_pushable"] is True


def test_second_acceleration_at_point_six_pushes_with_two_core_confirmations():
    """持续到0.6%的二次加速，在同窗放量且两项共振时应及时推送。"""
    scanner = AnomalyScanner()
    spot = _spot(support_strength_score=75, orderbook_imbalance=0.20)
    rolling = _track_exact_60s(scanner, spot, target_change_pct=0.60)
    spot._rolling_60s_momentum = rolling

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=_positive_fund(),
        sector_context={},
        board_context={},
        reversal_state={"previous_change_pct": 0.3, "scan_change_pct": 0.30},
        today=date(2026, 8, 14),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "60秒放量急拉预警"
    assert detail["rolling_60s_tier"] == "medium"
    assert detail["positive_acceleration_core_confirmation_count"] == 2
    assert detail["rolling_60s_alert_pushable"] is True


def test_strong_tape_with_extreme_bid_support_can_downgrade_lagging_fund_to_observation():
    """莲花控股类直线启动：资金快照滞后不能盖过同窗量价和极强盘口。"""
    scanner = AnomalyScanner()
    spot = _spot(
        support_strength_score=82,
        orderbook_imbalance=0.40,
        volume_ratio=1.40,
    )
    rolling = _track_exact_60s(scanner, spot, target_change_pct=1.05)
    spot._rolling_60s_momentum = rolling

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=_lagging_negative_fund(),
        sector_context={},
        board_context={},
        reversal_state={"previous_change_pct": 0.0, "scan_change_pct": 1.05},
        today=date(2026, 8, 14),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, label, detail = result
    assert label == "60秒放量强急拉"
    assert detail["fund_flow_divergence_overridden"] is True
    assert detail["positive_acceleration_min_core_confirmation_count"] == 1
    assert detail["detection_pool_member"] is False


def test_strong_tape_with_sector_breadth_can_downgrade_lagging_fund_snapshot():
    """杭电股份类产业链共振：盘口+板块两项可穿透较慢资金快照。"""
    scanner = AnomalyScanner()
    spot = _spot(support_strength_score=75, orderbook_imbalance=0.20)
    rolling = _track_exact_60s(scanner, spot, target_change_pct=1.05)
    spot._rolling_60s_momentum = rolling

    result = scanner._detect_positive_acceleration_setup(
        spot=spot,
        current_fund=_lagging_negative_fund(),
        sector_context=_positive_sector(),
        board_context={},
        reversal_state={"previous_change_pct": 0.0, "scan_change_pct": 1.05},
        today=date(2026, 8, 14),
        fund_snapshot_is_stale=False,
        fund_source="eastmoney_main_fund",
    )

    assert result is not None
    _score, _label, detail = result
    assert detail["positive_acceleration_core_confirmation_count"] == 2
    assert detail["fund_flow_divergence_overridden"] is True


def _tencent_fields(*, price: float, bid_start: float, bid_volume: int) -> list[str]:
    fields = [""] * 88
    fields[1] = "换档测试股"
    fields[2] = "002580"
    fields[3] = str(price)
    fields[4] = "10.00"
    fields[5] = "10.00"
    fields[6] = "100000"
    fields[32] = str(round((price / 10.0 - 1.0) * 100, 2))
    fields[33] = str(price)
    fields[34] = "9.98"
    fields[38] = "2.0"
    fields[47] = "11.00"
    fields[48] = "9.00"
    fields[49] = "1.5"
    fields[51] = str(price - 0.02)
    for index in range(5):
        fields[9 + index * 2] = f"{bid_start - index * 0.01:.2f}"
        fields[10 + index * 2] = str(bid_volume)
        fields[19 + index * 2] = f"{price + 0.01 + index * 0.01:.2f}"
        fields[20 + index * 2] = "3000"
    return fields


def test_orderbook_price_level_shift_reports_withdrawal_as_unavailable():
    source = TencentSource()
    first = source._parse_spot(
        "002580",
        _tencent_fields(price=10.00, bid_start=9.99, bid_volume=10_000),
    )
    second = source._parse_spot(
        "002580",
        _tencent_fields(price=10.10, bid_start=10.09, bid_volume=5_000),
    )

    assert first is not None and second is not None
    assert second["bid_depth_5"] < first["bid_depth_5"]
    # 五档价格完全平移时没有可比价位，不能把“无法计算”伪装成零撤单。
    assert second["withdrawal_ratio"] is None
