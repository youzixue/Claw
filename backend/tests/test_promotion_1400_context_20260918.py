"""② A3：晋级预测下午 2 小时空档 —— 新增 promotion_1400 / promotion_1430。

缺陷
----
原上下文为 0925/0935/1000/1030/1305/1510/2000。
下午只有 1305（13:00-13:20）与 1510（15:05-15:30），
而 PAPER_INTRADAY_BUY_END=14:50、PAPER_MOMENTUM_RETEST_CANDIDATE_END=14:30
—— 13:20~14:50 期间没有任何新鲜候选，账户在用陈旧快照交易。
实测 9/17：1305 最后一次运行 13:02:29，1510 首次运行 15:10:00，
空档 2 小时 7.5 分。

修法
----
新增 `promotion_1400`（窗口 13:55-14:15，任务 14:00）与
`promotion_1430`（窗口 14:25-14:45，任务 14:30），
在**全部注册点**登记（漏一处会导致跨模块行为不一致）。

注意 `_promotion_prediction_snapshot_context` 有两条路径：
1) 任务名后缀表（`trigger` 形如 promotion_prediction_HHMM）
2) 时钟兜底（`trigger` 为 schedule 时按时分推断）
两条都必须认识新上下文，否则计划任务写出的快照上下文名会退化成
`promotion_prediction_1400`，既不在 QUALITY 白名单也不在官方窗口表，
等于新增任务但写不出可消费的正式批次（本文件第 3 个用例即为此回归护栏）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.api.v1.paper import PAPER_MAINLINE_INTRADAY_CONTEXTS
from app.api.v1.promotion import (
    PROMOTION_INTRADAY_CONTEXTS,
    PROMOTION_MAINLINE_REFRESH_CONTEXTS,
    _PROMOTION_OFFICIAL_CONTEXT_WINDOWS,
)
from app.core.prediction_data_quality import PREDICTION_INTRADAY_CONTEXTS
from app.data.scheduler import (
    _promotion_prediction_snapshot_context,
    _promotion_startup_catchup_trigger,
)

NEW_CONTEXTS = ("promotion_1400", "promotion_1430")


def _minutes(hhmm: tuple[int, int]) -> int:
    return hhmm[0] * 60 + hhmm[1]


def test_context_registered_in_paper_mainline_intraday():
    """mainline 必须能消费新上下文，否则新增的时段对它无效。"""
    for context in NEW_CONTEXTS:
        assert context in PAPER_MAINLINE_INTRADAY_CONTEXTS


def test_paper_mainline_contexts_stay_in_chronological_order():
    order = [_minutes((int(c.split("_")[1][:2]), int(c.split("_")[1][2:])))
             for c in PAPER_MAINLINE_INTRADAY_CONTEXTS]
    assert order == sorted(order)


def test_promotion_refresh_contexts_stay_in_chronological_order():
    order = [_minutes((int(c.split("_")[1][:2]), int(c.split("_")[1][2:])))
             for c in PROMOTION_MAINLINE_REFRESH_CONTEXTS]
    assert order == sorted(order), "刷新上下文顺序错乱会让审计口径难以核对"


def test_context_registered_in_promotion_api():
    for context in NEW_CONTEXTS:
        assert context in PROMOTION_INTRADAY_CONTEXTS
        assert context in _PROMOTION_OFFICIAL_CONTEXT_WINDOWS
    assert _PROMOTION_OFFICIAL_CONTEXT_WINDOWS["promotion_1400"] == ((13, 55), (14, 15))
    assert _PROMOTION_OFFICIAL_CONTEXT_WINDOWS["promotion_1430"] == ((14, 25), (14, 45))


def test_context_registered_in_quality_gate():
    """质量门禁不认识上下文会按非盘中口径校验，导致校验结论失真。"""
    for context in NEW_CONTEXTS:
        assert context in PREDICTION_INTRADAY_CONTEXTS


def test_afternoon_coverage_reaches_the_intraday_buy_end():
    """13:00 之后到 14:50 收盘买入口径，不得出现 >35 分钟的数据空档。

    35 分钟 = 早盘既有时点 10:00->10:30 的既有节奏上限，
    不要求比现有设计更密，只要求下午不再出现 2 小时级别的空洞。
    """
    # 只考察下午连续竞价；跨过 11:30-13:00 午休不算空档。
    windows = sorted(
        (ctx, _PROMOTION_OFFICIAL_CONTEXT_WINDOWS[ctx])
        for ctx in PROMOTION_MAINLINE_REFRESH_CONTEXTS
        if _minutes(_PROMOTION_OFFICIAL_CONTEXT_WINDOWS[ctx][0]) >= 13 * 60
    )
    assert len(windows) >= 3, f"下午上下文不足: {windows}"
    last = None
    for ctx, (start, end) in windows:
        assert _minutes(end) >= _minutes(start)
        if last is not None:
            gap = _minutes(start) - _minutes(last)
            assert gap <= 35, f"{ctx} 与上一个窗口间隔 {gap} 分钟过大"
        last = end
    # 最后一个窗口必须覆盖到收盘前买入口径
    assert _minutes(windows[-1][1][1]) >= 14 * 60 + 45


@pytest.mark.parametrize("trigger,expected", [
    ("promotion_prediction_0925", "promotion_0925"),
    ("promotion_prediction_0935", "promotion_0935"),
    ("promotion_prediction_1000", "promotion_1000"),
    ("promotion_prediction_1030", "promotion_1030"),
    ("promotion_prediction_1305", "promotion_1305"),
    ("promotion_prediction_1400", "promotion_1400"),
    ("promotion_prediction_1430", "promotion_1430"),
    ("promotion_prediction_1510", "promotion_1510"),
    ("promotion_prediction_2000", "promotion_2000"),
])
def test_task_name_maps_to_official_context(trigger, expected):
    """回归护栏：每个计划任务名都必须映射到与官方窗口表一致的上下文名。"""
    context = _promotion_prediction_snapshot_context(trigger)
    assert context == expected
    assert context in _PROMOTION_OFFICIAL_CONTEXT_WINDOWS
    assert context in PREDICTION_INTRADAY_CONTEXTS or context in {
        "promotion_1510", "promotion_2000",
    }


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 9, 18, 13, 54), "promotion_1354"),  # 窗口外：按时分兜底命名（既有行为）
    (datetime(2026, 9, 18, 13, 55), "promotion_1400"),
    (datetime(2026, 9, 18, 14, 15), "promotion_1400"),
    (datetime(2026, 9, 18, 14, 16), "promotion_1430"),
    (datetime(2026, 9, 18, 14, 30), "promotion_1430"),
    (datetime(2026, 9, 18, 14, 45), "promotion_1430"),
    (datetime(2026, 9, 18, 15, 0), "promotion_1510"),
])
def test_scheduler_clock_resolves_the_new_context(moment, expected):
    assert _promotion_prediction_snapshot_context("schedule", moment) == expected


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 9, 18, 13, 54), ""),
    (datetime(2026, 9, 18, 13, 56), "promotion_prediction_1400"),
    (datetime(2026, 9, 18, 14, 16), ""),
    (datetime(2026, 9, 18, 14, 26), "promotion_prediction_1430"),
    (datetime(2026, 9, 18, 14, 40), "promotion_prediction_1430"),
    (datetime(2026, 9, 18, 14, 46), ""),
])
def test_startup_catchup_windows(moment, expected):
    """重启补跑窗口必须与正式窗口一致，不得凭空制造或加宽批次。"""
    assert _promotion_startup_catchup_trigger(moment) == expected


def test_new_contexts_are_known_to_the_ledger_and_research_scope():
    import inspect

    from app.promotion import ledger, route_rank_research

    for context in NEW_CONTEXTS:
        assert context in inspect.getsource(ledger.ScheduleBatch)
        assert context in route_rank_research.CONTEXTS
