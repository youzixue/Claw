"""腾讯实时行情成交量单位契约测试；无真实 HTTP / DB。

背景（2026-09-16 复盘定位）：
腾讯 `qt.gtimg.cn` 字段[6]「成交量」的单位**按板块不同**：
    主板(60/00) / 创业板(30) → 「手」
    科创板(688)             → 「股」
而 `stock_spot.volume` 的既有契约是「手」（见 `app/data/scheduler.py` 的
spot→kline 补全 `spot.volume * 100`，以及
`app/signal/anomaly_scanner._spot_volume_in_kline_unit`）。

修复前统一按「手」处理，导致科创板 volume / amount 双双放大 100 倍：
    688256 寒武纪 2026-09-16 volume=1,330,494,200、amount=1.50e12，
    按换手率 2.12% 反推真实值应为 volume≈13,304,942 股、amount≈150 亿元。

本测试锁定三个不变量：
1. 科创板      → 归一化为整数「手」（÷100 四舍五入）
2. 其余板块    → 原样保留「手」
3. 归一化后 `_spot_volume_in_kline_unit`(即 ×100) 能还原回原始「股」，误差 <0.001%
"""
from app.data.sources.tencent_source import TencentSource
from app.signal.anomaly_scanner import _spot_volume_in_kline_unit


def _fields(code: str, *, volume: str, price: str = "10.00", avg_price: str = "10.00") -> list[str]:
    """构造一个满足 `_parse_spot` 索引需求的最小 88 字段列表。"""
    fields = [""] * 88
    fields[0] = f"v_{code}"
    fields[1] = "测试标的"
    fields[3] = price          # 最新价
    fields[4] = "9.90"         # 昨收
    fields[5] = "9.95"         # 开
    fields[6] = volume         # 成交量（单位随板块不同）
    for index in range(9, 29):  # 五档买卖价量
        fields[index] = "0"
    fields[30] = "20260916100000"
    fields[32] = "1.01"        # 涨幅%
    fields[33] = "10.20"       # 高
    fields[34] = "9.90"        # 低
    fields[38] = "1.00"        # 换手%
    fields[39] = "20.0"        # PE
    fields[44] = "100.0"       # 流通市值(亿)
    fields[46] = "2.0"         # PB
    fields[47] = "10.89"       # 涨停
    fields[48] = "8.91"        # 跌停
    fields[49] = "1.5"         # 量比
    fields[51] = avg_price     # VWAP
    fields[64] = "1.0"         # 股息率
    fields[74] = "0.1"         # 委比
    fields[79] = "10.0"        # 净利润增速
    return fields


# ---------------------------------------------------------------- 单位归一化

def test_star_market_volume_is_shares_and_gets_normalised_to_hands():
    """科创板字段[6]是「股」，必须 ÷100 归一化为「手」。"""
    # 13,304,942 股 → 133,049.42 手 → 四舍五入 133,049 手
    assert TencentSource._volume_in_hands("688256", 13_304_942) == 133_049


def test_mainboard_and_chinext_volume_stay_in_hands():
    """主板/创业板字段[6]本来就是「手」，不得改动。"""
    assert TencentSource._volume_in_hands("000001", 949_626) == 949_626
    assert TencentSource._volume_in_hands("600000", 723_404) == 723_404
    assert TencentSource._volume_in_hands("300750", 787_163) == 787_163


def test_star_market_prefix_boundary_is_exact():
    """只有 68 开头才是科创板；688/689 属于科创板，600/300/000 不属于。"""
    assert TencentSource._volume_in_hands("688001", 10_000) == 100
    assert TencentSource._volume_in_hands("689009", 10_000) == 100
    # 60 开头是沪主板，不能被 68 规则误伤
    assert TencentSource._volume_in_hands("600001", 10_000) == 10_000
    assert TencentSource._volume_in_hands("688"[:2] + "0", 10_000) == 100


def test_star_market_non_multiple_of_100_shares_rounds_to_nearest_hand():
    """科创板允许 200 股以上按 1 股递增，股数不必是 100 的整数倍。

    四舍五入到整数手的相对误差必须远小于修复前的 100 倍口径错误。
    """
    raw_shares = 13_304_942          # 非 100 整数倍
    hands = TencentSource._volume_in_hands("688256", raw_shares)
    assert isinstance(hands, int)
    restored_shares = _spot_volume_in_kline_unit(hands)
    relative_error = abs(restored_shares - raw_shares) / raw_shares
    assert relative_error < 1e-5, f"还原误差过大: {relative_error}"


def test_zero_and_small_star_market_volumes_do_not_crash():
    """停牌/无成交时字段可为 0 或空，归一化不得抛异常。"""
    assert TencentSource._volume_in_hands("688256", 0) == 0
    assert TencentSource._volume_in_hands("688256", 49) == 0    # 不足半手 → 0
    # 恰好半手（50 股）走 Python 的 round-half-to-even（round(0.5)==0）。
    # 不锁定这个平局方向，只锁定「整数手 + 误差 ≤ 半手」，避免测试绑定实现细节。
    for raw in (50, 51, 149, 150, 151):
        hands = TencentSource._volume_in_hands("688256", raw)
        assert isinstance(hands, int)
        assert abs(hands * 100 - raw) <= 50, f"raw={raw} hands={hands} 误差超过半手"


def test_star_market_rounding_error_is_bounded_by_half_a_lot():
    """归一化误差上界：任意科创板股数，还原后误差 ≤ 50 股（半手）。

    对比修复前的 100 倍口径错误，该误差可忽略；这里显式锁定上界，
    防止将来有人改成 floor/ceil 造成单边系统性偏差。
    """
    for raw in (200, 250, 999, 10_001, 13_304_942, 1_330_494_200):
        hands = TencentSource._volume_in_hands("688256", raw)
        assert abs(hands * 100 - raw) <= 50, f"raw={raw} 误差超过半手"


# ------------------------------------------------- 经 _parse_spot 的端到端口径

def test_spot_amount_is_not_inflated_100x_for_star_market():
    """端到端：科创板 amount 必须 ≈ 均价 × 真实股数，而不是 ×100。"""
    source = TencentSource()
    # 13,304,942 股，VWAP 1124.89 → 真实成交额 ≈ 1.4966e10（约 150 亿）
    spot = source._parse_spot("688256", _fields("688256", volume="13304942", avg_price="1124.89"))
    assert spot is not None
    expected_amount = 1124.89 * 13_304_942          # 保留原始股数精度
    assert spot["amount"] > 0
    assert abs(spot["amount"] - expected_amount) / expected_amount < 1e-4, (
        f"科创板 amount 口径错误: got={spot['amount']:.0f} expected≈{expected_amount:.0f}"
    )
    # 明确拒绝旧的 100 倍结果
    assert spot["amount"] < expected_amount * 2


def test_spot_amount_for_mainboard_unchanged():
    """主板口径不得回归：手数 × 100 = 股数。"""
    source = TencentSource()
    spot = source._parse_spot("000001", _fields("000001", volume="949626", avg_price="11.65"))
    assert spot is not None
    assert spot["volume"] == 949_626
    expected_amount = 11.65 * 949_626 * 100         # 11.06 亿
    assert abs(spot["amount"] - expected_amount) / expected_amount < 1e-6


def test_spot_volume_times_100_reproduces_share_count_for_all_boards():
    """契约不变量：stock_spot.volume(手) × 100 必须能还原「股」。

    spot→kline 的补全路径（scheduler.py `spot.volume * 100`）与
    anomaly_scanner `_spot_volume_in_kline_unit` 都依赖这条不变量。
    """
    source = TencentSource()
    cases = [
        # (code, 腾讯字段[6]原值, 该板块下原值的单位, 真实股数)
        ("000001", "949626", "hands", 949_626 * 100),
        ("300750", "787163", "hands", 787_163 * 100),
        ("688256", "13304942", "shares", 13_304_942),
    ]
    for code, raw, _unit, true_shares in cases:
        spot = source._parse_spot(code, _fields(code, volume=raw))
        assert spot is not None
        restored = _spot_volume_in_kline_unit(spot["volume"])
        relative_error = abs(restored - true_shares) / true_shares
        assert relative_error < 1e-5, f"{code} 还原股数误差过大: {relative_error}"
