"""资金流新鲜度必须按「交易时间」度量，而不是墙钟时间。

背景（2026-09-16 实测）
----------------------
`promotion_1305` 在 13:02:11 判定时，`fund_flow` 水印为
`completeness=0.0 / record_count=0 / clock_status_counts={'stale': 2993}`，
导致 **15/15 晋级路线全部 `route_required_watermark_not_ok`**；
而同一个上午的四次 run 全部正常：

| 时段 | 时刻 | route 通过 | clock_status |
|---|---|---|---|
| 0935 | 09:35 | 14/15 | `ok` |
| 1000 | 09:55 | 14/15 | `ok` |
| 1030 | 10:25 | 14/15 | `ok` |
| **1305** | **13:02** | **0/15** | **`stale`** |
| 1510 | 15:10 | 14/15 | `historical_known` |

原因是新鲜度用的墙钟差：腾讯资金水印在午休（11:30–13:00）停止推进，
13:02 判定时 `13:02 − 11:29 ≈ 93 分钟 > 600 秒` → `stale`。

**午休休市是正常市场行为，把它判成数据故障才是缺陷。**

这些用例同时锁定「修复生效」与「安全性未被放宽」两侧。
"""
from datetime import date, datetime

import pytest

from app.core.trade_calendar import trading_elapsed_seconds
from app.data.fund_flow_clock import fund_clock_status

TRADE_DATE = date(2026, 9, 16)
LIMIT = 600.0          # settings.FUND_FLOW_SOURCE_MAX_AGE_SEC 默认值


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 16, hour, minute, second)


def _status(sq: datetime, obs: datetime, dec: datetime, *, live: bool = True) -> str:
    return fund_clock_status(sq, obs, obs, TRADE_DATE, dec, LIMIT, require_live=live)


# ── 交易时间映射本身 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hour,minute,expected_minutes",
    [
        # 原点为当日 00:00（不是开盘），因为**只有午休**冻结交易时钟：
        # 开盘前与盘后都按墙钟推进，否则那两个区间的缺口会被压成 0。
        (0, 30, 30.0),      # 开盘前推进
        (9, 0, 540.0),      # 开盘前推进
        (9, 30, 570.0),     # 09:30
        (10, 30, 630.0),    # 上午已走 60 分钟
        (11, 30, 690.0),    # 上午收市
        (12, 0, 690.0),     # 午休：冻结
        (12, 59, 690.0),    # 午休：冻结
        (13, 0, 690.0),     # 下午开盘瞬间仍是 690（连续）
        (13, 2, 692.0),     # 下午已走 2 分钟
        (15, 0, 810.0),     # 收市
        (18, 0, 990.0),     # 盘后**继续推进**（不封顶，否则会压缩真实缺口）
    ],
)
def test_trading_elapsed_freezes_over_lunch_and_after_close(hour, minute, expected_minutes):
    """午休不推进交易时钟；上午/下午/盘后各自线性推进。"""
    assert trading_elapsed_seconds(_at(hour, minute)) == pytest.approx(
        expected_minutes * 60.0, abs=1.0
    )


def test_after_close_keeps_advancing_so_gaps_are_not_compressed():
    """盘后必须继续推进：封顶会把收盘前后的真实缺口压缩掉。

    这是初版实现的缺陷，被既有断言
    `test_main_fund_consumer_repair_20260914`（NOW=15:00:10，源钟 14:50:09）
    与 `test_tenbagger_push_logic` 抓出：601 秒的缺口曾被算成 591 秒。
    """
    assert (
        trading_elapsed_seconds(_at(15, 0, 10)) - trading_elapsed_seconds(_at(14, 50, 9))
    ) == pytest.approx(601.0, abs=1.0)


def test_lunch_break_contributes_zero_trading_time():
    """11:30 → 13:00 这 90 分钟墙钟，交易时间为 0。"""
    assert trading_elapsed_seconds(_at(13, 0)) - trading_elapsed_seconds(_at(11, 30)) == 0.0


# ── 修复生效：午休跨越不再误判 stale ─────────────────────────────────


def test_lunch_crossing_is_not_stale():
    """9/16 的真实失败场景：午休前水印 → 13:02 判定，必须为 ok。"""
    # 腾讯资金水印停在午休前，13:02 的首个午盘 run 判定
    assert _status(_at(11, 29), _at(11, 30), _at(13, 2, 11)) == "ok"


@pytest.mark.parametrize("minute", [0, 1, 2, 5, 9])
def test_all_afternoon_boundary_minutes_are_ok(minute: int):
    """午盘开盘后、交易时间差仍在阈值内的判定都应通过。

    注意上界：11:29 → 13:09 的交易时间差恰为 10 分钟（600 秒，边界内）；
    到 13:10 就是 11 分钟，**应当**判 stale —— 因为下午确实已过 10 个
    交易分钟仍未更新。这一点由 test_afternoon_still_stale_after_threshold 锁定。
    """
    assert _status(_at(11, 29), _at(11, 30), _at(13, minute)) == "ok"


def test_afternoon_still_stale_after_threshold():
    """午休虽不推进交易时钟，但下午开盘后超过阈值的陈旧仍必须拦截。"""
    assert _status(_at(11, 29), _at(11, 30), _at(13, 10)) == "stale"


def test_morning_close_watermark_still_ok_late_afternoon():
    """即使下午很晚判定，午休前的水印在交易时间口径下也只差下午的运行时长。"""
    # 11:29 → 14:05 = 下午 65 分钟 = 3900 秒 > 600 → 应 stale（下午确实没更新）
    assert _status(_at(11, 29), _at(11, 30), _at(14, 5)) == "stale"


# ── 安全性未被放宽：交易时段真实缺口仍拦截 ───────────────────────────


@pytest.mark.parametrize(
    "sq_h,sq_m,dec_h,dec_m",
    [
        (10, 0, 10, 11),    # 上午 11 分钟
        (10, 0, 11, 0),     # 上午 60 分钟
        (13, 30, 13, 41),   # 下午 11 分钟
        (14, 0, 15, 0),     # 下午 60 分钟
    ],
)
def test_real_intraday_gap_is_still_stale(sq_h, sq_m, dec_h, dec_m):
    """交易时段内超过阈值的真实缺口必须照常判 stale —— 本次修复不放宽任何阈值。"""
    assert _status(_at(sq_h, sq_m), _at(sq_h, sq_m), _at(dec_h, dec_m)) == "stale"


@pytest.mark.parametrize("minutes", [1, 5, 9, 10])
def test_intraday_gap_within_limit_is_ok(minutes: int):
    """阈值内（≤10 分钟）的正常采集间隔必须为 ok。"""
    from datetime import timedelta

    start = _at(10, 0)
    assert _status(start, start, start + timedelta(minutes=minutes)) == "ok"


def test_limit_is_not_widened():
    """显式确认阈值未被放宽：600 秒原样，601 秒即失效。"""
    from datetime import timedelta

    start = _at(10, 0)
    assert _status(start, start, start + timedelta(seconds=600)) == "ok"
    assert _status(start, start, start + timedelta(seconds=601)) == "stale"


# ── 盘后路径不受影响 ─────────────────────────────────────────────────


@pytest.mark.parametrize("dec_h,dec_m", [(15, 10), (20, 0)])
def test_postmarket_uses_historical_known(dec_h, dec_m):
    """require_live=False（收盘后）仍走 historical_known，与本次修复无关。"""
    assert _status(_at(15, 0), _at(15, 0), _at(dec_h, dec_m), live=False) == "historical_known"


def test_order_violation_still_rejected():
    """source > observed 的时序矛盾仍被判 invalid，不受交易时间口径影响。"""
    st = fund_clock_status(_at(10, 30), _at(10, 0), _at(10, 0), TRADE_DATE,
                           _at(10, 35), LIMIT, require_live=True)
    assert st == "invalid"


def test_other_trade_date_still_rejected():
    """跨日水印仍被拒（交易时间口径只在同日内有意义）。"""
    st = fund_clock_status(_at(10, 0), _at(10, 0), _at(10, 0), TRADE_DATE,
                           _at(10, 5), LIMIT, require_live=True)
    assert st == "ok"
    from datetime import timedelta
    st2 = fund_clock_status(
        _at(10, 0), _at(10, 0), _at(10, 0), TRADE_DATE + timedelta(days=1),
        _at(10, 5), LIMIT, require_live=True,
    )
    assert st2 == "invalid"
