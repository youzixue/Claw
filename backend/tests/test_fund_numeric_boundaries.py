"""Fund numeric boundaries: synthetic inputs only, not provider recovery evidence."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data.fund_flow_clock import eastmoney_quote_clock, fund_clock_status, main_fund_values_valid
from app.data.main_fund import current_main_fund_evidence, fund_order_breakdown, load_current_main_fund_map
from app.data.scheduler import DataScheduler
from app.data.sources.eastmoney_source import EastMoneySource
from app.models.stock import FundFlow

DAY_TIME = datetime(2026, 9, 9, 10)
TZ = ZoneInfo("Asia/Shanghai")
SECONDS = int(DAY_TIME.replace(tzinfo=TZ).timestamp())
BREAKDOWNS = tuple(f"{size}_net_inflow{suffix}" for size in ("super", "big", "mid", "small") for suffix in ("", "_pct"))
LABELS = {"super": "超大单", "big": "大单", "mid": "中单", "small": "小单"}


def evidence(**overrides):
    return {"code": "000001", "name": "合成边界测试", "trade_date": DAY_TIME.date(),
            "main_net_inflow": 0, "main_net_inflow_pct": 0,
            "source": "eastmoney", "source_version": "individual_fund_flow_v3_f124",
            "source_quote_at": DAY_TIME - timedelta(seconds=2),
            "received_at": DAY_TIME - timedelta(seconds=1), "observed_at": DAY_TIME,
            **overrides}


def frame_row(now, **overrides):
    return {"代码": "000001", "名称": "合成边界测试", "今日主力净流入-净额": 0,
            "今日主力净流入-净占比": 0, "source_quote_at": now - timedelta(seconds=2),
            "received_at": now - timedelta(seconds=1), **overrides}


@pytest.mark.parametrize("value", ["9" * 5000, "0" * 5000, "9" * 4000, 10 ** 1000,
                                   True, False, np.bool_(True), np.bool_(False), None,
                                   float("inf"), float("nan"), "９" * 10, SECONDS * 1000],
                         ids=["long-digits", "long-zero", "huge-seconds-text", "huge-int",
                              "true", "false", "np-true", "np-false", "missing",
                              "inf", "nan", "unicode-digits", "milliseconds"])
def test_bad_provider_seconds_return_unknown_without_raising(value):
    assert eastmoney_quote_clock(value) is None


@pytest.mark.parametrize("value", [SECONDS, str(SECONDS), float(SECONDS), np.int64(SECONDS), "0" + str(SECONDS)])
def test_real_provider_seconds_remain_unchanged(value):
    assert eastmoney_quote_clock(value) == DAY_TIME


@pytest.mark.parametrize("value", [True, False, np.bool_(True), np.bool_(False), None,
                                   float("nan"), float("inf"), 10 ** 1000],
                         ids=["true", "false", "np-true", "np-false", "missing", "nan", "inf", "huge-int"])
@pytest.mark.parametrize("field", ["main_net_inflow", "main_net_inflow_pct"])
def test_invalid_main_values_fail_closed_in_real_projection(field, value):
    assert current_main_fund_evidence(evidence(**{field: value}),
                                     trade_date=DAY_TIME.date(), decision_at=DAY_TIME) is None
    assert not main_fund_values_valid(value, 0)
    assert not main_fund_values_valid(0, value)


@pytest.mark.parametrize("value", [0, -12, 12, np.int64(0), np.float64(-1.25), Decimal("1.25"), "0", "-1.25", "1e7"])
def test_measured_numbers_keep_zero_sign_and_existing_string_compatibility(value):
    projected = current_main_fund_evidence(evidence(main_net_inflow=value, main_net_inflow_pct=value),
                                         trade_date=DAY_TIME.date(), decision_at=DAY_TIME)
    assert projected["main_net_inflow"] == float(value)
    assert projected["main_net_inflow_pct"] == float(value)
    assert fund_order_breakdown({"super_net_inflow": value})["super_net_inflow"] == float(value)


@pytest.mark.parametrize("field", BREAKDOWNS)
@pytest.mark.parametrize("value", [np.bool_(True), np.bool_(False)])
def test_numpy_boolean_optional_breakdown_is_not_a_measured_one_or_zero(field, value):
    assert fund_order_breakdown({field: value})[field] is None
    projected = current_main_fund_evidence(evidence(**{field: value}),
                                         trade_date=DAY_TIME.date(), decision_at=DAY_TIME)
    assert projected[field] is None
    assert projected["main_net_inflow"] == 0


@pytest.mark.parametrize("age,expected", [(0, "ok"), (599.999999, "ok"), (600, "ok"),
                                          (600.000001, "stale"), (601, "stale")])
def test_no_freshness_gate_relaxation(age, expected):
    source = DAY_TIME - timedelta(seconds=age)
    assert fund_clock_status(source, source, source, DAY_TIME.date(), DAY_TIME, 600) == expected


@pytest.mark.parametrize("field", ["main_net_inflow", "main_net_inflow_pct", *BREAKDOWNS])
def test_one_overflow_cell_cannot_discard_another_good_stock(field):
    now = datetime.now()
    if field.startswith("main"):
        label = "主力"
    else:
        label = LABELS[field.split("_")[0]]
    column = f"今日{label}净流入-" + ("净占比" if field.endswith("_pct") else "净额")
    good = frame_row(now)
    bad = frame_row(now, **{"代码": "000002", column: 10 ** 1000})
    # Preserve supplier Python integers before parsing instead of coercing them in the fixture.
    parsed = DataScheduler._parse_individual_fund_flow_df(pd.DataFrame([good, bad], dtype=object),
                                                         now.date(), observed_at=now)
    by_code = {row["code"]: row for row in parsed}
    assert by_code["000001"]["main_net_inflow"] == 0
    if field.startswith("main"):
        assert set(by_code) == {"000001"}
    else:
        assert set(by_code) == {"000001", "000002"}
        assert by_code["000002"][field] is None


@pytest.mark.parametrize("value,expected", [("12万", 120000), ("-3亿", -300000000), ("0%", 0), ("1,200", 1200)])
def test_parser_retains_original_explicit_units(value, expected):
    now = datetime.now()
    parsed = DataScheduler._parse_individual_fund_flow_df(
        pd.DataFrame([frame_row(now, **{"今日主力净流入-净额": value})]), now.date(), observed_at=now)
    assert parsed[0]["main_net_inflow"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_clock", ["9" * 5000, "0" * 5000], ids=["long-digits", "long-zero"])
async def test_one_bad_http_clock_keeps_good_row_without_inflating_coverage(monkeypatch, bad_clock):
    now = datetime.now()
    raw = {"f124": int(now.replace(tzinfo=TZ).timestamp()), "f12": "000001", "f14": "合成",
           "f2": 10, "f3": 0, "f62": 0, "f184": 0, "f66": 0, "f69": 0,
           "f72": 0, "f75": 0, "f78": 0, "f81": 0, "f84": 0, "f87": 0}
    real_client = httpx.AsyncClient
    calls = []

    def dispatch(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {"total": 2,
            "diff": [raw, {**raw, "f12": "000002", "f124": bad_clock}]}})

    def factory(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(dispatch))

    monkeypatch.setattr("app.data.sources.eastmoney_source.httpx.AsyncClient", factory)
    df = await EastMoneySource().get_individual_fund_flow()
    assert list(df["代码"]) == ["000001"]
    assert df.attrs["fund_flow_expected_count"] == 2  # rejected row never shrinks the denominator
    assert df.attrs["fund_flow_clock_rejected_count"] == 1
    assert len(calls) == 1
    observed = df.attrs["fund_flow_observed_at"]
    parsed = DataScheduler._parse_individual_fund_flow_df(df, observed.date(), observed_at=observed)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as db:
            await db.run_sync(FundFlow.__table__.create)
        async with sessions() as db:
            await DataScheduler._batch_upsert(db, FundFlow, parsed, ["code", "trade_date"])
            await db.commit()
            projected = await load_current_main_fund_map(db, trade_date=observed.date(), decision_at=observed)
            assert set(projected) == {"000001"}
            assert projected["000001"]["main_net_inflow"] == 0
            assert projected["000001"]["super_net_inflow"] == 0
    finally:
        await engine.dispose()
