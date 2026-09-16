"""Unified scheduler -> FundFlow -> read projection; no business DB or live HTTP."""
from datetime import datetime, timedelta
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import tenbagger
from app.data import scheduler as scheduler_module
from app.data.scheduler import DataScheduler
from app.models.stock import FundFlow, StockSpot


def frame(amount=0):
    now = datetime.now()
    return pd.DataFrame([{
        "代码": "000001", "名称": "隔离测试",
        "今日主力净流入-净额": amount, "今日主力净流入-净占比": 0,
        "source_quote_at": now - timedelta(seconds=2),
        "received_at": now - timedelta(seconds=1),
    }])


@pytest.mark.parametrize("source,version", [
    ("tencent", "field50"), ("ths", "total_fund_v1"),
    ("eastmoney", "unknown"), ("unknown", "individual_fund_flow_v3_f124"),
])
def test_unknown_source_contract_rejected(source, version):
    assert DataScheduler._parse_individual_fund_flow_df(
        frame(), datetime.now().date(), source=source, source_version=version,
    ) == []


@pytest.mark.parametrize("amount", [None, float("nan"), float("inf"), True])
def test_bad_amount_not_synthetic_zero(amount):
    assert DataScheduler._parse_individual_fund_flow_df(
        frame(amount), datetime.now().date(),
    ) == []


@pytest.mark.parametrize("source,version", [
    ("tencent", "field50"), ("ths", "total_fund_v1"),
    ("eastmoney", "unknown"), ("eastmoney", "individual_fund_flow_v3_f124"),
])
def test_collection_and_sentiment_share_source_gate(source, version):
    from app.data.main_fund import current_main_fund_evidence
    now = datetime.now()
    parsed = DataScheduler._parse_individual_fund_flow_df(
        frame(), now.date(), source=source, source_version=version,
    )
    row = {"code": "000001", "trade_date": now.date(),
           "source": source, "source_version": version,
           "main_net_inflow": 0, "main_net_inflow_pct": 0,
           "source_quote_at": now, "received_at": now, "observed_at": now}
    evidence = current_main_fund_evidence(row, trade_date=now.date(), decision_at=now)
    assert bool(parsed) == (evidence is not None)
    body = inspect.getsource(DataScheduler._intraday_indices_and_sentiment)
    assert "current_main_fund_evidence(" in body
    assert 'main_flows = [row["main_net_inflow"] for row in qualified_funds]' in body


def test_parser_and_compatibility_projection_share_zero_and_missing_contract():
    df = frame()
    records = DataScheduler._parse_individual_fund_flow_df(df, datetime.now().date())
    item = tenbagger._build_eastmoney_main_fund_items(df)["000001"]
    assert records[0]["main_net_inflow"] == item["main_net_inflow"] == 0
    assert records[0]["big_net_inflow"] is item["big_net_inflow"] is None
    assert item["source_quote_at"] == records[0]["source_quote_at"].isoformat()
    assert item["provider_source"] == "eastmoney"
    assert item["clock_basis"] == "provider_quote_update_f124"
    assert tenbagger._format_data_source_label("fund_flow") != "同花顺资金净额"


@pytest.mark.asyncio
async def test_scheduler_single_fetch_commits_then_prewarm_only_reads(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
        await conn.run_sync(StockSpot.__table__.create)
    response = frame()
    response.attrs.update(fund_flow_source="tencent",
                          fund_flow_source_version="tencent_hsfundtab_v1")
    source = AsyncMock(return_value=response)
    scheduler = DataScheduler()
    scheduler._tradeable_codes = ["000001"]
    old_source = AsyncMock(side_effect=AssertionError("old individual sources are forbidden"))
    scheduler._sources = {
        "tencent": SimpleNamespace(get_individual_fund_flow=source),
        "eastmoney": SimpleNamespace(get_individual_fund_flow=old_source),
        "akshare": SimpleNamespace(get_individual_fund_flow=old_source),
    }
    monkeypatch.setattr(scheduler_module, "async_session", sessions)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "_collect_concept_fund_flow_bounded", AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(scheduler_module.data_quality_guard, "record_success", AsyncMock())
    monkeypatch.setattr(scheduler_module.data_quality_guard, "record_failure", AsyncMock())
    monkeypatch.setattr(tenbagger, "resolve_latest_trade_date", AsyncMock(return_value=datetime.now().date()))
    forbidden = AsyncMock(side_effect=AssertionError("projection must not fetch/persist/cache"))
    monkeypatch.setattr(tenbagger.EastMoneySource, "get_individual_fund_flow", forbidden)
    monkeypatch.setattr("app.data.sources.tencent_source.TencentSource.get_individual_fund_flow", forbidden)
    monkeypatch.setattr("app.data.sources.akshare_source.AkShareSource.get_individual_fund_flow", forbidden)
    monkeypatch.setattr(tenbagger, "_get_cached_payload", forbidden)
    monkeypatch.setattr(tenbagger, "_persist_dashboard_snapshot", forbidden)
    monkeypatch.setattr(tenbagger, "_get_persisted_dashboard_snapshot", forbidden)
    monkeypatch.setattr(tenbagger, "_get_latest_persisted_dashboard_snapshot", forbidden)
    try:
        async with sessions() as db:
            assert (await tenbagger.prewarm_eastmoney_main_fund_snapshot(db))["items"] == {}
        await scheduler._intraday_slow()
        async with sessions() as db:
            row = (await db.execute(select(FundFlow))).scalar_one()
            from app.data.main_fund import current_main_fund_evidence
            assert current_main_fund_evidence(row, trade_date=datetime.now().date(), decision_at=datetime.now()), row.__dict__
            from app.data.main_fund import load_current_main_fund_map
            current = await load_current_main_fund_map(db, trade_date=datetime.now().date(), decision_at=datetime.now())
            assert current, row.__dict__
            commit = AsyncMock(side_effect=AssertionError("read-only"))
            monkeypatch.setattr(db, "commit", commit)
            monkeypatch.setattr(db, "flush", commit)
            for force in (False, True):
                payload = await tenbagger.prewarm_eastmoney_main_fund_snapshot(db, force_refresh=force)
                item = payload["items"]["000001"]
                assert item["main_net_inflow"] == 0
                assert item["big_net_inflow"] is None
                assert item["provider_source"] == "tencent"
                assert item["source_version"] == "tencent_hsfundtab_v1"
                assert item["observed_at"] == row.observed_at.isoformat()
                assert payload["snapshot_time"] == row.observed_at.isoformat()
                json.dumps(payload)
            commit.assert_not_awaited()
        source.assert_awaited_once_with(["000001"])
        old_source.assert_not_awaited()
        forbidden.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_projection_rejects_stale_or_unknown_table_rows(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
    records = DataScheduler._parse_individual_fund_flow_df(frame(), datetime.now().date())
    records[0]["source_quote_at"] -= timedelta(days=1)
    unknown = {**records[0], "code": "000002", "source": "ths"}
    monkeypatch.setattr(tenbagger, "resolve_latest_trade_date", AsyncMock(return_value=datetime.now().date()))
    try:
        async with sessions() as db:
            db.add_all([FundFlow(**records[0]), FundFlow(**unknown)])
            await db.commit()
            snapshot = await tenbagger.prewarm_eastmoney_main_fund_snapshot(db, force_refresh=True)
            assert snapshot["items"] == {}
            assert snapshot["snapshot_time"] is None
            assert tenbagger._resolve_anomaly_current_fund_context(snapshot) == ({}, "unavailable")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, None])
async def test_b1_no_kline_preserves_zero_and_missing_not_input_fallback(monkeypatch, amount):
    from app.data import main_fund
    current = {"000001": {"main_net_inflow": amount}} if amount is not None else {}
    monkeypatch.setattr(main_fund, "load_current_main_fund_map", AsyncMock(return_value=current))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])))
    rows = [{"code": "000001", "main_net_inflow": 999999, "detail": {"main_net_inflow": 999999}}]
    result = await tenbagger._enrich_stock_rows_with_b1(rows, db, target_date=datetime.now().date())
    assert result[0]["main_net_inflow"] is amount
    assert result[0]["detail"]["main_net_inflow"] is amount
    assert rows[0]["main_net_inflow"] == 999999
    body = inspect.getsource(tenbagger._enrich_stock_rows_with_b1)
    assert "spot.main_net_inflow" not in body
    assert "latest_fund_trade_date" not in body


@pytest.mark.asyncio
async def test_stock_current_context_cannot_be_established_by_old_snapshot(monkeypatch):
    from app.data import main_fund
    monkeypatch.setattr(main_fund, "load_current_main_fund_map", AsyncMock(return_value={}))
    now = datetime.now()
    snapshot = {"source": "eastmoney_main_fund", "items": {
        "000001": {"main_net_inflow": 999999, "main_net_inflow_pct": 10,
                   "source_quote_at": now, "received_at": now, "observed_at": now}
    }}
    result = await tenbagger._load_stock_fund_context(
        object(), "000001", quote_trade_date=now.date(),
        completed_trade_date=None, current_snapshot=snapshot, as_of_at=now,
    )
    assert result["current_item"] == {}
    assert result["current_source"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, None])
async def test_b1_full_enrichment_never_uses_detail_or_spot_fund(monkeypatch, amount):
    from app.data import main_fund
    current = {"000001": {"main_net_inflow": amount}} if amount is not None else {}
    monkeypatch.setattr(main_fund, "load_current_main_fund_map", AsyncMock(return_value=current))
    now = datetime.now()
    response = lambda rows: SimpleNamespace(all=lambda: rows)
    spot = SimpleNamespace(
        code="000001", price=10, change_pct=1, turnover=1,
        main_net_inflow=777777, updated_at=now,
    )
    spot_result = SimpleNamespace(scalars=lambda: response([spot]))
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        response([(now.date(),)]), response([]), spot_result, response([]),
    ]))
    monkeypatch.setattr(tenbagger, "_build_live_b1_bar", lambda *args, **kwargs: None)
    monkeypatch.setattr(tenbagger, "_apply_stock_row_buy_point_status", lambda row: row)
    rows = [{"code": "000001", "detail": {"main_net_inflow": 999999}}]
    result = await tenbagger._enrich_stock_rows_with_b1(rows, db, target_date=now.date())
    assert result[0]["main_net_inflow"] is amount
    assert result[0]["detail"]["main_net_inflow"] is amount


@pytest.mark.asyncio
async def test_stock_current_context_public_clocks_are_iso_strings(monkeypatch):
    from app.data import main_fund
    now = datetime.now()
    row = {"code": "000001", "date": now.date(), "trade_date": now.date(),
           "source": "eastmoney", "source_version": "individual_fund_flow_v3_f124",
           "main_net_inflow": 0, "main_net_inflow_pct": 0,
           "source_quote_at": now - timedelta(seconds=2),
           "received_at": now - timedelta(seconds=1), "observed_at": now}
    monkeypatch.setattr(main_fund, "load_current_main_fund_map", AsyncMock(return_value={"000001": row}))
    result = await tenbagger._load_stock_fund_context(
        object(), "000001", quote_trade_date=now.date(),
        completed_trade_date=None, as_of_at=now,
    )
    item = result["current_item"]
    for key in ("source_quote_at", "received_at", "observed_at", "date", "trade_date"):
        assert item[key] == row[key].isoformat()
    assert result["current_as_of"] == item["as_of"] == row["source_quote_at"].isoformat()
    assert item["main_net_inflow"] == 0
    json.dumps(item)


@pytest.mark.asyncio
@pytest.mark.parametrize("has_kline_dates", [False, True])
async def test_b1_new_main_fund_cannot_whiten_old_order_breakdown(monkeypatch, has_kline_dates):
    from app.data import main_fund
    now = datetime.now()
    current = {"main_net_inflow": 10000000, "main_net_inflow_pct": 3,
               "source": "eastmoney", "source_quote_at": now}
    monkeypatch.setattr(main_fund, "load_current_main_fund_map",
                        AsyncMock(return_value={"000001": current}))
    response = lambda rows: SimpleNamespace(all=lambda: rows)
    results = [response([])]
    if has_kline_dates:
        results = [response([(now.date(),)]), response([]),
                   SimpleNamespace(scalars=lambda: response([])), response([])]
    db = SimpleNamespace(execute=AsyncMock(side_effect=results))
    monkeypatch.setattr(tenbagger, "_build_live_b1_bar", lambda *args, **kwargs: None)
    monkeypatch.setattr(tenbagger, "_apply_stock_row_buy_point_status", lambda row: row)
    fields = ("super_net_inflow", "super_net_inflow_pct", "big_net_inflow",
              "big_net_inflow_pct", "mid_net_inflow", "mid_net_inflow_pct",
              "small_net_inflow", "small_net_inflow_pct")
    old_detail = {key: 999999 for key in fields}
    result = await tenbagger._enrich_stock_rows_with_b1(
        [{"code": "000001", "detail": old_detail}], db, target_date=now.date(),
    )
    detail = result[0]["detail"]
    assert detail["main_net_inflow"] == 10000000
    assert detail["is_stale"] is False
    for key in fields:
        assert detail[key] is None
        assert old_detail[key] == 999999


@pytest.mark.asyncio
@pytest.mark.parametrize("separator", ["T", " "])
@pytest.mark.parametrize("cutoff_delta_us,expected", [(-1, False), (0, True), (1, True)])
async def test_raw_timestamp_separator_has_identical_exact_cutoff_semantics(separator, cutoff_delta_us, expected):
    from sqlalchemy import text
    from app.data.main_fund import load_current_main_fund_map
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    observed = datetime(2026, 9, 9, 5, 0, 0, 123456)
    raw = observed.isoformat(sep=separator)
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
    try:
        async with sessions() as db:
            # Simulate both actual scheduler raw-T and SQLAlchemy space storage;
            # no mocked helper and no global/business database.
            await db.execute(text(
                "INSERT INTO fund_flow (code, trade_date, main_net_inflow, "
                "main_net_inflow_pct, source, source_version, "
                "source_quote_at, received_at, observed_at) "
                "VALUES (:code, :day, 0, 0, :source, :version, :clock, :clock, :clock)"
            ), {"code": "000001", "day": observed.date().isoformat(),
                "source": "eastmoney", "version": "individual_fund_flow_v3_f124",
                "clock": raw})
            await db.commit()
            stored = await db.scalar(text("SELECT observed_at FROM fund_flow"))
            assert stored == raw
            cutoff = observed + timedelta(microseconds=cutoff_delta_us)
            result = await load_current_main_fund_map(
                db, trade_date=observed.date(), decision_at=cutoff, codes=["000001"],
            )
            assert bool(result) is expected
            if expected:
                assert result["000001"]["main_net_inflow"] == 0
                assert result["000001"]["observed_at"] == observed
    finally:
        await engine.dispose()

