"""Fund row identity boundaries; synthetic inputs, never provider recovery evidence."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data import scheduler as scheduler_module
from app.data.main_fund import load_current_main_fund_map
from app.data.scheduler import DataScheduler
from app.models.stock import FundFlow, StockSpot


def frame(codes):
    now = datetime.now()
    return pd.DataFrame([{
        "代码": code, "名称": "身份边界合成测试",
        "今日主力净流入-净额": 0, "今日主力净流入-净占比": 0,
        "source_quote_at": now - timedelta(seconds=2),
        "received_at": now - timedelta(seconds=1),
    } for code in codes], dtype=object)


@pytest.mark.parametrize("code", [
    None, pd.NA, float("nan"), float("inf"), True, False, np.bool_(True),
    "", " ", "nan", "None", "<NA>", "True", "000001.0", 600519.0, 600519.5,
    -1, "-00001", "1e5", "0000012", "9" * 5000, 10 ** 5000, "０００００１",
    "٠٠٠٠٠١", "²", "600 519", "sh", "000001\x00",
], ids=[
    "none", "pd-na", "nan-float", "inf-float", "bool-true", "bool-false", "numpy-bool",
    "empty", "blank", "nan-text", "none-text", "na-text", "bool-text",
    "decimal-text", "integral-float", "fractional-float", "negative-int", "negative-text",
    "scientific-text", "overlength", "long-digits", "huge-integer", "fullwidth", "arabic-indic",
    "superscript", "embedded-space", "prefix-only", "nul",
])
def test_invalid_identity_is_not_a_fund_coverage_row(code):
    df = frame(["000002", code])
    records = DataScheduler._parse_individual_fund_flow_df(df, datetime.now().date())
    assert [r["code"] for r in records] == ["000002"]
    assert records[0]["main_net_inflow"] == records[0]["main_net_inflow_pct"] == 0
    assert len(df) == 2  # source denominator and original frame are not rewritten


@pytest.mark.parametrize("code,expected", [
    ("000001", "000001"), ("600519", "600519"), ("1", "000001"), (1, "000001"),
    (np.int64(1), "000001"), ("sh600519", "600519"), ("SH600519", "600519"),
    ("sz000001", "000001"), ("SZ000001", "000001"), ("600519.SH", "600519"),
    ("600519.sh", "600519"), ("000001.SZ", "000001"), ("bj920001", "920001"),
    ("920001.BJ", "920001"), (" 600519 ", "600519"),
])
def test_existing_unambiguous_code_forms_are_preserved(code, expected):
    records = DataScheduler._parse_individual_fund_flow_df(frame([code]), datetime.now().date())
    assert len(records) == 1 and records[0]["code"] == expected
    # Parsing data is not a board, ST, auction or strategy entry authorization.
    assert records[0]["source_version"] == "individual_fund_flow_v3_f124"


@pytest.mark.asyncio
async def test_mixed_frame_keeps_raw_denominator_and_does_not_rewrite_legacy_rows(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scheduler = DataScheduler()
    df = frame(["000001", "nan", "0000012", "０００００１"])
    df.attrs.update(fund_flow_expected_count=20, fund_flow_source="tencent",
                    fund_flow_source_version="tencent_hsfundtab_v1")
    scheduler._tradeable_codes = ["000001"]
    primary = AsyncMock(return_value=df)
    fallback = AsyncMock(side_effect=AssertionError("a good primary row must not be discarded"))
    scheduler._sources = {
        "tencent": SimpleNamespace(get_individual_fund_flow=primary),
        "eastmoney": SimpleNamespace(get_individual_fund_flow=fallback),
        "akshare": SimpleNamespace(get_individual_fund_flow=fallback),
    }
    monkeypatch.setattr(scheduler_module, "async_session", sessions)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "_collect_concept_fund_flow_bounded", AsyncMock(return_value=pd.DataFrame()))
    success, failure = AsyncMock(), AsyncMock()
    monkeypatch.setattr(scheduler_module.data_quality_guard, "record_success", success)
    monkeypatch.setattr(scheduler_module.data_quality_guard, "record_failure", failure)
    try:
        async with engine.begin() as db:
            await db.run_sync(FundFlow.__table__.create)
            await db.run_sync(StockSpot.__table__.create)
        async with sessions() as db:
            # Untimed legacy data is neither rewritten nor made usable by a new collection.
            db.add(FundFlow(code="legacy-unknown", trade_date=datetime.now().date(),
                           main_net_inflow=123, main_net_inflow_pct=1))
            await db.commit()
        await scheduler._intraday_slow()
        async with sessions() as db:
            rows = list((await db.scalars(select(FundFlow).order_by(FundFlow.code))).all())
            assert [r.code for r in rows] == ["000001", "legacy-unknown"]
            assert rows[1].main_net_inflow == 123 and rows[1].source_quote_at is None
            current = await load_current_main_fund_map(db, trade_date=datetime.now().date(),
                                                       decision_at=datetime.now())
            assert set(current) == {"000001"}
            assert current["000001"]["main_net_inflow"] == 0
        primary.assert_awaited_once_with(["000001"])
        fallback.assert_not_awaited()
        call = next(c for c in success.await_args_list if c.args[1:3] == ("tencent", "individual_fund_flow_round"))
        assert call.kwargs["record_count"] == 1
        assert call.kwargs["expected_count"] == 20
        universe = next(c for c in success.await_args_list
                        if c.args[1:3] == ("tencent", "individual_fund_flow"))
        assert universe.kwargs["record_count"] == universe.kwargs["expected_count"] == 1
        assert len(df) == 4 and df.attrs["fund_flow_expected_count"] == 20
    finally:
        await engine.dispose()
