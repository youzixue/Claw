"""锁定涨停池取数源迁移（2026-09-17）后的字段契约与「同类涨停」口径。

背景
----
`_after_market` 的涨停原因补齐此前调用失效的旧 pywencai
（`pw.get_limit_up_pool()`，上游自 2026-08 下旬改 SSE 流后完全不可用），
每天抛错，导致 `limit_up_reason` 退回东财「所属行业」口径 —— 即
`scheduler._after_market` 注释自己警告过的「'所属行业'会被误当涨停逻辑」。
迁移到 `WencaiStreamSource`（问句不变 `涨停 连板数`）后有两个契约点必须锁死：

1. **列名别名**：新源把原因列从 `涨停原因类别[YYYYMMDD]` 改为
   `涨停原因[YYYYMMDD]`。若不识别，`limit_up_reason` 会**静默写成 None**
   —— 因为 else 分支用的是精确键 `row.get("涨停原因")`，匹配不到带日期后缀
   的真实列名。内容同构已由库内 903 条问财历史记录验证
   （`预重整+BIPV+建筑装饰` 这类 `A+B+C` 题材组合）。

2. **同类涨停口径**：`same_reason_limit_up_count` 原按 reason **字符串相等**
   统计。问财 `A+B+C` 组合近乎唯一 —— 实测 08-17/08-21/08-24 三天该口径
   「同类数」**恒为 1**，`>=3` 命中率 0%（东财格式为 19%~60%）。
   若不同步修正，追板预案的 `has_board_cohort` 门槛会永久失效。
   改为「共享至少一个题材词」，且在东财格式下与旧口径**逐股等价**。
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from app.api.v1.tenbagger import _reason_cohort_count_map
from app.data.scheduler import DataScheduler

# 新流式源对问句 `涨停 连板数` 的真实列集合（2026-09-17 抓取）
STREAM_COLUMNS = [
    "封流比[20260916]",
    "首次涨停时间[20260916]",
    "涨停开板次数[20260916]",
    "code",
    "涨停封单量[20260916]",
    "最新价",
    "最终涨停时间[20260916]",
    "涨停原因[20260916]",
    "最新涨跌幅",
    "market_code",
    "连续涨停天数[20260916]",
    "股票代码",
    "涨停封单额[20260916]",
    "几天几板[20260916]",
    "封耗比[20260916]",
    "股票简称",
    "封成比[20260916]",
    "涨停[20260916]",
]

STREAM_ROW = {
    "封流比[20260916]": 0.42,
    "首次涨停时间[20260916]": "09:35:12",
    "涨停开板次数[20260916]": 0,
    "code": "301236",
    "涨停封单量[20260916]": 1234.0,
    "最新价": 12.34,
    "最终涨停时间[20260916]": "09:35:12",
    "涨停原因[20260916]": "海上风电+清洁能源+福建国资",
    "最新涨跌幅": 10.02,
    "market_code": "33",
    "连续涨停天数[20260916]": 2,
    "股票代码": "301236.SZ",
    "涨停封单额[20260916]": 88_000_000.0,
    "几天几板[20260916]": "2天2板",
    "封耗比[20260916]": 1.1,
    "股票简称": "测试股",
    "封成比[20260916]": 0.9,
    "涨停[20260916]": "涨停",
}


def _stream_frame(**overrides) -> pd.DataFrame:
    row = dict(STREAM_ROW)
    row.update(overrides)
    return pd.DataFrame([row], columns=None)


# ---------- 1. 列名别名：`涨停原因` 必须被识别 ----------


def test_new_source_reason_column_is_recognized():
    """迁移的关键防线：不认识 `涨停原因[日期]` 就会静默写 None。"""
    records = DataScheduler._parse_limit_up_df(
        _stream_frame(), date(2026, 9, 16), source="pywencai"
    )
    assert len(records) == 1
    assert records[0]["limit_up_reason"] == "海上风电+清洁能源+福建国资"
    assert records[0]["limit_up_reason"] is not None


def test_new_source_all_pywencai_fields_are_mapped():
    """5 个旧列名在新源下完全一致，必须逐字段映射（不得退回东财列）。"""
    records = DataScheduler._parse_limit_up_df(
        _stream_frame(), date(2026, 9, 16), source="pywencai"
    )
    rec = records[0]
    assert rec["code"] == "301236"
    assert rec["name"] == "测试股"
    assert rec["consecutive_days"] == 2          # 连续涨停天数
    assert rec["seal_amount"] == 88_000_000.0    # 涨停封单额
    assert rec["break_count"] == 0               # 涨停开板次数
    assert rec["limit_up_time"] == "09:35:12"    # 首次涨停时间
    # 东财侧字段不得被误用：新源没有「封板资金」/「连板数」/「封板时间」
    assert rec["limit_up_price"] == 12.34        # 回退到「最新价」
    assert rec["source"] == "pywencai"
    assert rec["quarantined"] is False


def test_legacy_reason_column_still_works():
    """旧列名 `涨停原因类别` 必须继续可用。"""
    frame = pd.DataFrame(
        [{"股票代码": "600000", "股票简称": "浦发银行", "涨停原因类别[20260824]": "预重整+BIPV"}]
    )
    records = DataScheduler._parse_limit_up_df(frame, date(2026, 8, 24), source="pywencai")
    assert records[0]["limit_up_reason"] == "预重整+BIPV"


def test_legacy_reason_column_wins_when_both_present():
    """两者并存时 `涨停原因类别`（旧口径）优先，且不受列顺序影响。"""
    for reason_columns in (
        ["涨停原因[20260916]", "涨停原因类别[20260916]"],
        ["涨停原因类别[20260916]", "涨停原因[20260916]"],
    ):
        columns = ["股票代码", "股票简称", *reason_columns]
        row = {"股票代码": "600000", "股票简称": "浦发银行"}
        row["涨停原因[20260916]"] = "新口径"
        row["涨停原因类别[20260916]"] = "旧口径"
        frame = pd.DataFrame([[row[c] for c in columns]], columns=columns)
        records = DataScheduler._parse_limit_up_df(
            frame, date(2026, 9, 16), source="pywencai"
        )
        assert len(records) == 1
        assert records[0]["limit_up_reason"] == "旧口径"


def test_reason_falls_back_to_board_stats_when_absent():
    """完全没有原因列时，仍按既有逻辑回退到 `涨停统计`，而不是崩溃。"""
    frame = pd.DataFrame([{"代码": "000001", "名称": "平安银行", "涨停统计": "3/2"}])
    records = DataScheduler._parse_limit_up_df(frame, date(2026, 9, 16), source="eastmoney")
    assert records[0]["limit_up_reason"] == "3/2"


# ---------- 2. 同类涨停口径：题材词 vs 字符串相等 ----------


def _rows(*pairs):
    return list(pairs)


def test_cohort_matches_on_shared_theme_token():
    """问财 `A+B+C`：共享任一题材词即同类，这是迁移后门槛仍有效的关键。"""
    rows = _rows(
        ("301236", "黄金珠宝+黄金涨价+年报增长"),
        ("600111", "黄金珠宝+锂电池"),
        ("300999", "半导体+国产替代"),
    )
    counts = _reason_cohort_count_map(rows)
    assert counts["黄金珠宝+黄金涨价+年报增长"] == 2
    assert counts["黄金珠宝+锂电池"] == 2
    assert counts["半导体+国产替代"] == 1  # 孤立股


def test_cohort_equals_string_equality_for_single_industry():
    """东财单一行业名：切分后只有一个 token，token 口径必须与旧口径等价。"""
    rows = _rows(
        ("000001", "通信设备"),
        ("000002", "通信设备"),
        ("000003", "通信设备"),
        ("000004", "半导体"),
    )
    counts = _reason_cohort_count_map(rows)
    assert counts["通信设备"] == 3   # 与 group_by 字符串计数一致
    assert counts["半导体"] == 1


def test_cohort_count_includes_self_and_is_per_reason():
    """同一 reason 的多只股票计数相同，且含自身（与旧口径一致）。"""
    rows = _rows(("a", "同一题材"), ("b", "同一题材"), ("c", "别的"))
    counts = _reason_cohort_count_map(rows)
    assert counts["同一题材"] == 2
    assert counts["别的"] == 1


def test_cohort_excludes_blank_and_nan_reasons():
    rows = _rows(("a", ""), ("b", "nan"), ("c", None), ("d", "通信设备"))
    counts = _reason_cohort_count_map(rows)
    assert "" not in counts
    assert "nan" not in counts
    assert counts == {"通信设备": 1}


def test_cohort_falls_back_to_string_equality_without_tokens():
    """切不出题材词（如纯单字）时退化为字符串相等，不把不相关股票并成同类。"""
    rows = _rows(("a", "A"), ("b", "A"), ("c", "B"))
    counts = _reason_cohort_count_map(rows)
    assert counts["A"] == 2
    assert counts["B"] == 1


@pytest.mark.parametrize("separator", ["+", "/", "、", "，", ",", "；", ";", "-"])
def test_cohort_splits_on_all_supported_separators(separator):
    rows = _rows(("a", f"共享题材{separator}其他"), ("b", "共享题材"))
    counts = _reason_cohort_count_map(rows)
    assert counts[f"共享题材{separator}其他"] == 2


# ---------- 3. 封板时间格式：必须归一到库内既有的 HH:MM:SS ----------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-16 09:37:00", "09:37:00"),   # 新流式源（带日期）
        ("2026-09-16  09:37:00", "09:37:00"),  # 双空格
        (" 10:23:42", "10:23:42"),             # 旧问财（前导空格）
        ("09:30:00", "09:30:00"),              # 东财
        ("093000", "093000"),                  # 东财紧凑格式，原样保留
        ("", ""),
        (None, ""),
        (float("nan"), ""),
        ("nan", ""),
    ],
)
def test_limit_up_time_normalized_to_time_only(raw, expected):
    from app.data.scheduler import _normalize_limit_up_time

    assert _normalize_limit_up_time(raw) == expected


def test_new_source_limit_up_time_does_not_break_downstream_consumers():
    """带日期的 19 字符时间会同时击穿字符串比较与 split(':') 解析。

    真实事故面（2026-09-17 迁移时实测）：
      * `tenbagger` 的 `limit_up_time > "10:30:00"` —— `'2' > '1'`，
        词序比较恒 True，每只票都被判「封板时间偏晚」并阻断追板预案；
      * `factors/breakout.py` 的 `map(int, v.split(":"))`
        —— `int("2026-09-16 09")` 抛 ValueError。
    """
    records = DataScheduler._parse_limit_up_df(
        _stream_frame(), date(2026, 9, 16), source="pywencai"
    )
    value = records[0]["limit_up_time"]
    assert value == "09:35:12"
    assert not (value > "10:30:00")             # 09:35 不得被判成偏晚
    assert [int(p) for p in value.split(":")] == [9, 35, 12]   # breakout 因子可解析


def test_late_limit_up_time_still_flagged():
    """回归：真正偏晚的时间仍要能被判出来，不能一律放行。"""
    records = DataScheduler._parse_limit_up_df(
        _stream_frame(**{"首次涨停时间[20260916]": "2026-09-16 14:25:48"}),
        date(2026, 9, 16),
        source="pywencai",
    )
    assert records[0]["limit_up_time"] == "14:25:48"
    assert records[0]["limit_up_time"] > "10:30:00"


def test_cohort_handles_sqlalchemy_row_like_tuples():
    """调用方传的是 SQLAlchemy Row 或 (code, reason) 元组，两者都要能用。"""
    assert _reason_cohort_count_map([("000001", "通信设备"), ("000002", "通信设备")]) == {
        "通信设备": 2
    }


def test_cohort_empty_input():
    assert _reason_cohort_count_map([]) == {}


# ---------- 4. 汇总告警必须区分「取到了但覆盖未核验」与「端点不可用」 ----------


@pytest.mark.asyncio
async def test_source_alert_skips_degraded_coverage_rows(tmp_path):
    """`_update_stock_status` 在成功路径上也记 failure（覆盖未核验），
    汇总告警若不排除会**每小时误报** `pywencai/stock_status 端点不可用`。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.data.scheduler import (
        DEGRADED_COVERAGE_MARKER,
        SOURCE_ALERT_FAIL_STREAK,
        DataScheduler,
    )
    from app.db.session import Base
    from app.models.risk import DataSourceHealth

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alert.db'}", future=True)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as session:
        now = datetime.now()
        session.add_all([
            # 覆盖未核验 —— 必须跳过
            DataSourceHealth(
                source="pywencai", api_name="stock_status", status="down",
                last_failure=now, fail_streak=SOURCE_ALERT_FAIL_STREAK + 47,
                error_msg=f"{DEGRADED_COVERAGE_MARKER}已正向合并214只风险标的；全量覆盖和风险解除未核验",
                updated_at=now,
            ),
            # 真实不可用 —— 必须照常告警
            DataSourceHealth(
                source="pywencai", api_name="stock_mapping", status="down",
                last_failure=now, fail_streak=SOURCE_ALERT_FAIL_STREAK + 12,
                error_msg="'NoneType' object has no attribute 'get'",
                updated_at=now,
            ),
        ])
        await session.commit()
        alerted = await DataScheduler()._alert_persistently_failing_sources(session)
        await session.rollback()
    await engine.dispose()
    assert alerted == 1, "只应告警真实不可用的那条"


@pytest.mark.asyncio
async def test_partial_empty_queries_still_alert(tmp_path):
    """有查询为空是真信号，不得被降级标记吞掉。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.data.scheduler import SOURCE_ALERT_FAIL_STREAK, DataScheduler
    from app.db.session import Base
    from app.models.risk import DataSourceHealth

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'alert2.db'}", future=True)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with maker() as session:
        now = datetime.now()
        session.add(DataSourceHealth(
            source="pywencai", api_name="stock_status", status="down",
            last_failure=now, fail_streak=SOURCE_ALERT_FAIL_STREAK + 5,
            error_msg="部分查询为空；已正向合并214只风险标的；全量覆盖和风险解除未核验",
            updated_at=now,
        ))
        await session.commit()
        alerted = await DataScheduler()._alert_persistently_failing_sources(session)
        await session.rollback()
    await engine.dispose()
    assert alerted == 1
