"""⑨ chronic 数据源：把"开盘前必然没有数据"与"端点真的坏了"分开。

实测（2026-09-18，`data_source_health` 全历史，按 updated_at 的时分切分）
--------------------------------------------------------------------------
| source/api | 开盘前(<09:30) 失败 | 盘中/盘后 失败 | 定性 |
|---|---|---|---|
| `index/daily_snapshot` | 61 | 10 | 以开盘前为主，盘中仍有真实失败 |
| `tencent/individual_fund_flow` | **110** | **0** | 100% 是开盘前产物 |
| `akshare/concept_fund_flow` | 79 | **1965** | 盘中持续抖动，上游问题 |

前两项每交易日早盘各写一条 `status=down` 且 `fail_streak` 递增
（index 到 7~8、tencent 到 17），于是汇总告警每早误报一次 [ALERT]。
第三项是上游页面/响应问题（`'NoneType' object has no attribute 'text'` 180 次、
表头列数变化 46 次），不是本项目解析错误。

修法
----
前两项：只在**开盘前窗口内**给失败原因加 `[EXPECTED-PREOPEN]` 标记，
记录仍如实落库（status/fail_streak 不变，不掩盖历史），只把该行排除出汇总告警；
盘中/盘后的真实故障不带标记，仍然照常告警。
第三项：把上游异常翻译成带端点名、异常类型与成因判断的可行动诊断。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.data import scheduler as scheduler_module
from app.data.scheduler import (
    CONCEPT_FUND_FLOW_ENDPOINT,
    DEGRADED_COVERAGE_MARKER,
    EXPECTED_PREOPEN_MARKER,
    SOURCE_ALERT_FAIL_STREAK,
    DataScheduler,
    _classify_concept_fund_flow_error,
    _expected_preopen_unavailable,
    _preopen_marked,
)


@pytest.mark.parametrize("moment,expected", [
    (datetime(2026, 9, 18, 0, 0), True),
    (datetime(2026, 9, 18, 9, 15), True),
    (datetime(2026, 9, 18, 9, 24), True),
    (datetime(2026, 9, 18, 9, 29, 59), True),
    (datetime(2026, 9, 18, 9, 30), False),
    (datetime(2026, 9, 18, 10, 17), False),
    (datetime(2026, 9, 18, 15, 0), False),
])
def test_preopen_window_boundary(moment, expected):
    assert _expected_preopen_unavailable(moment) is expected


def test_marker_is_only_added_inside_the_preopen_window():
    inside = _preopen_marked("当日有效指数仅1/3", datetime(2026, 9, 18, 9, 20))
    assert inside.startswith(EXPECTED_PREOPEN_MARKER)
    assert "当日有效指数仅1/3" in inside
    # 盘中失败绝不能带标记 —— 带了就等于把真故障静音（index 实测有 10 次）
    outside = _preopen_marked("当日有效指数仅1/3", datetime(2026, 9, 18, 10, 17))
    assert outside == "当日有效指数仅1/3"
    assert EXPECTED_PREOPEN_MARKER not in outside


def test_markers_are_distinct_from_the_degraded_coverage_marker():
    """两个标记的语义不同，混用会让告警排除范围出错。"""
    assert EXPECTED_PREOPEN_MARKER != DEGRADED_COVERAGE_MARKER
    assert not EXPECTED_PREOPEN_MARKER.startswith(DEGRADED_COVERAGE_MARKER)


def test_alert_scan_excludes_both_markers_but_keeps_the_threshold():
    """汇总告警必须排除两个标记，但阈值与时间窗不变。"""
    import inspect

    body = inspect.getsource(DataScheduler._alert_persistently_failing_sources)
    assert "NOT LIKE :degraded_marker" in body
    assert "NOT LIKE :preopen_marker" in body
    assert '"threshold": SOURCE_ALERT_FAIL_STREAK' in body
    assert '"since": datetime.now() - timedelta(seconds=SOURCE_ALERT_MAX_AGE_SEC)' in body


def test_alert_scan_still_reads_only_the_latest_row_per_source():
    """排除标记不得顺手放宽"只取最新一行"的语义。"""
    import inspect

    body = inspect.getsource(DataScheduler._alert_persistently_failing_sources)
    assert "ROW_NUMBER() OVER (PARTITION BY source, api_name" in body
    assert "WHERE rn = 1 AND fail_streak >= :threshold" in body


def test_threshold_is_still_three_consecutive_failures():
    assert SOURCE_ALERT_FAIL_STREAK == 3


@pytest.mark.parametrize("exc,expect_tokens", [
    (AttributeError("'NoneType' object has no attribute 'text'"), ("AttributeError", "空响应")),
    (ValueError("Length mismatch: Expected axis has 12 elements, new values have 13 elements"),
     ("ValueError", "表头列数变化")),
    (ValueError("no text parsed from document (line 0)"), ("非数据文档")),
    (RuntimeError("connection reset"), ("RuntimeError", "上游异常")),
])
def test_concept_error_classification_is_actionable(exc, expect_tokens):
    message = _classify_concept_fund_flow_error(exc)
    assert CONCEPT_FUND_FLOW_ENDPOINT in message
    assert str(exc) in message, "原始异常文本必须保留，不能被改写掉"
    for token in expect_tokens:
        assert token in message, token
    assert "不影响个股资金入库" in message


def test_timeout_reason_is_still_recorded_verbatim():
    """设计内的等待超时文案不能因为本次改动被改写。"""
    import inspect

    body = inspect.getsource(DataScheduler._collect_concept_fund_flow_bounded)
    assert "概念资金元数据等待超时，保留单一在途请求；不阻塞个股资金入库" in body


def test_module_still_imports_time_for_the_window():
    """窗口判定依赖 time 类型；缺了它 `_MARKET_OPEN_TIME` 会失效。"""
    assert scheduler_module._MARKET_OPEN_TIME.hour == 9
    assert scheduler_module._MARKET_OPEN_TIME.minute == 30


@pytest.mark.asyncio
async def test_alert_scan_skips_preopen_rows_but_still_alerts_real_failures(tmp_path):
    """端到端：标记行不告警，未标记的同 streak 行照常告警。

    这是本改动的核心保证 —— 去掉早盘噪音，但**不**碰盘中真故障。
    """
    from datetime import timedelta

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.data.scheduler import _SOURCE_ALERT_LAST
    from app.db.session import Base
    from app.models.risk import DataSourceHealth

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alert.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now()
    try:
        async with maker() as db:
            db.add_all([
                # 开盘前噪声：streak 远高于阈值，但带标记 → 不告警
                DataSourceHealth(
                    source="tencent", api_name="individual_fund_flow", status="down",
                    fail_streak=17, updated_at=now, error_msg=(
                        f"{EXPECTED_PREOPEN_MARKER} 腾讯资金本轮无有效数据"
                    ),
                ),
                # 同 streak、同时间，但没有标记 → 必须告警（对照组）
                DataSourceHealth(
                    source="akshare", api_name="concept_fund_flow", status="down",
                    fail_streak=17, updated_at=now,
                    error_msg="概念资金上游失败(AttributeError)：上游返回空响应",
                ),
                # 已不再探测的旧行（时间超出告警窗口）→ 不告警
                DataSourceHealth(
                    source="eastmoney", api_name="health_check", status="down",
                    fail_streak=3512, updated_at=now - timedelta(days=15),
                    error_msg="stale row",
                ),
            ])
            await db.commit()
            _SOURCE_ALERT_LAST.clear()
            alerted = await DataScheduler()._alert_persistently_failing_sources(db)
            assert alerted == 1
            # 记录本身一行都没被动过（不掩盖历史）
            rows = (await db.execute(select(DataSourceHealth))).scalars().all()
            assert len(rows) == 3
            assert {r.source for r in rows} == {"tencent", "akshare", "eastmoney"}
    finally:
        await engine.dispose()
        _SOURCE_ALERT_LAST.clear()
