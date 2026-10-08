"""真实历史窗口时点/来源/调度合同；隔离SQLite与假HTTP，不发飞书。"""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.data import index_history_window as window
from app.data.index_history import HISTORY_API, BENCHMARK_INDEX_CODES
from app.data.sources.index_source import IndexSource
from app.models.governance import TradeCalendarModel
from app.models.risk import DataSourceHealth
from app.models.stock import StockDaily
from app.paper.experiment_regime import freeze_benchmark_regime
from test_index_history import db, trade_days, history_frame

THROUGH = date(2026, 9, 8)
AT = datetime(2026, 9, 8, 17)


def frames(days, *, observed_at=AT):
    result = {}
    for code, symbol in window.INDEX_SYMBOLS.items():
        frame = history_frame(days, base=3000)
        frame.attrs.update(source_contract="tencent_index_raw_day_v1",
                           index_symbol=symbol, source_observed_at=observed_at.isoformat())
        result[code] = frame
    return result


async def seed(db, *, through=THROUGH, observed_at=AT):
    days = trade_days(through, 65)
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    payload = window.build_history_evidence(frames(days, observed_at=observed_at),
        expected_days=days, through_date=through, observed_at=observed_at)
    row = DataSourceHealth(source="index", api_name=HISTORY_API, status="up",
        completeness=1, last_success=observed_at, updated_at=observed_at,
        error_msg=json.dumps(payload))
    db.add(row)
    await db.commit()
    return days, row, payload


@pytest.mark.asyncio
async def test_frozen_window_is_visible_only_after_real_observation_and_not_mutable_daily(db):
    days, row, payload = await seed(db)
    _, before = await window.latest_verified_history(db, through_date=THROUGH,
                                                     at=AT - timedelta(seconds=1))
    assert before is None
    result = await freeze_benchmark_regime(db, at=datetime(2026, 9, 9, 10),
                                          previous_trade_date=THROUGH)
    assert result["label"] == "bull" and result["source_health_id"] == row.id
    db.add(StockDaily(code="000001", trade_date=THROUGH, close=1))  # 不得覆盖冻结输入
    await db.commit()
    again = await freeze_benchmark_regime(db, at=datetime(2026, 9, 9, 10),
                                         previous_trade_date=THROUGH)
    assert result == again
    assert result["benchmarks"][0]["close"] == 3064
    assert "input_sha256" in result["benchmarks"][0]
    assert (await freeze_benchmark_regime(db, at=AT, previous_trade_date=THROUGH))["label"] == "unknown"


@pytest.mark.asyncio
async def test_newer_intraday_health_does_not_replace_completed_history_window(db):
    _, original, _ = await seed(db)
    db.add(DataSourceHealth(source="index", api_name="daily_snapshot", status="up",
        completeness=1, last_success=datetime(2026, 9, 9, 9, 30)))
    await db.commit()
    row, _ = await window.latest_verified_history(db, through_date=THROUGH,
                                                   at=datetime(2026, 9, 9, 10))
    assert row.id == original.id


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["hash", "future", "symbol", "source", "wrong_end", "short", "negative", "bool", "calendar_gap"])
async def test_tampered_or_missing_history_fails_to_unknown(db, problem):
    days, row, payload = await seed(db)
    item = payload["codes"]["000001"]
    if problem == "hash": item["input_closes"][0] += 1
    if problem == "future": item["source_observed_at"] = "2026-09-10T10:00:00"
    if problem == "symbol": item["index_symbol"] = "sz000001"  # 平安银行不是上证指数
    if problem == "source": item["source_contract"] = "unverified"
    if problem == "wrong_end": payload["through_date"] = "2026-09-07"
    if problem == "short": item["input_closes"].pop()
    if problem == "negative": item["input_closes"][0] = -1
    if problem == "bool": item["input_closes"][0] = True
    if problem == "calendar_gap":
        await db.delete(await db.get(TradeCalendarModel, days[-10]))
    row.error_msg = json.dumps(payload)
    # ORM updated_at默认now；显式保持原证据时点，以测试内容校验而不是偶然时钟拦截。
    row.updated_at = AT
    await db.commit()
    result = await freeze_benchmark_regime(db, at=datetime(2026, 9, 9, 10),
                                          previous_trade_date=THROUGH)
    assert result["label"] == "unknown"


@pytest.mark.parametrize("problem", ["missing_day", "future_observation", "pre_close", "duplicate_conflict", "symbol", "empty", "nan"])
def test_evidence_requires_real_complete_index_response(problem):
    days = trade_days(THROUGH, 65)
    quotes = frames(days)
    at = AT
    if problem == "missing_day": quotes["000001"] = quotes["000001"].drop(index=10)
    if problem == "future_observation": quotes["000001"].attrs["source_observed_at"] = "2026-09-09T17:00:00"
    if problem == "pre_close": at = datetime(2026, 9, 8, 14)
    if problem == "symbol": quotes["000001"].attrs["index_symbol"] = "sz000001"
    if problem == "empty": quotes["000001"] = pd.DataFrame()
    if problem == "nan": quotes["000001"].loc[10, "close"] = float("nan")
    if problem == "duplicate_conflict":
        raw = quotes["000001"]
        extra = raw.iloc[[0]].copy()
        extra["close"] = 1
        quotes["000001"] = pd.concat([raw, extra], ignore_index=True)
        quotes["000001"].attrs = dict(raw.attrs)
    result = window.build_history_evidence(quotes, expected_days=days, through_date=THROUGH, observed_at=at)
    assert not result["complete"]


@pytest.mark.asyncio
async def test_refresh_network_outside_transaction_and_cached_restart_does_not_refetch(db, monkeypatch):
    days = trade_days(THROUGH, 65)
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    await db.commit()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return AT
    monkeypatch.setattr(window, "datetime", Clock)
    quotes = frames(days)
    async def fetch(code, **kwargs):
        async with maker() as other:
            other.add(DataSourceHealth(source="test", api_name="network-lock-check"))
            await other.commit()
        return quotes[code]
    source = SimpleNamespace(get_index_history_window=AsyncMock(side_effect=fetch))
    first = await window.refresh_verified_history(source=source, through_date=THROUGH, session_factory=maker)
    assert first["complete"] and first["inserted_count"] == first["repaired_invalid_count"] == 0
    assert first["candidate_count"] == 195
    assert first["projection_policy"] == "audit_only_no_historical_projection"
    assert await db.scalar(select(StockDaily.id)) is None
    health, evidence = await window.latest_verified_history(db, through_date=THROUGH, at=AT)
    assert health is not None and evidence["complete"]
    assert evidence["projection_audit"]["complete"] is False  # source != historical projection
    assert evidence["codes"]["000001"]["input_closes"][-1] == 3064
    second = await window.refresh_verified_history(source=source, through_date=THROUGH, session_factory=maker)
    assert second["status"] == "already_verified"
    assert source.get_index_history_window.await_count == 3


@pytest.mark.asyncio
async def test_refresh_preserves_good_invalid_and_missing_daily_rows_byte_for_byte(db, monkeypatch):
    days = trade_days(THROUGH, 65)
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    db.add_all([
        StockDaily(code="000001", trade_date=days[0], close=9000, prev_close=8999, amount=123),
        StockDaily(code="399001", trade_date=days[2], close=None, open=0, change_pct=-99),
    ])
    await db.commit()
    before = list((await db.execute(select(StockDaily.__table__).order_by(StockDaily.id))).all())
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return AT
    monkeypatch.setattr(window, "datetime", Clock)
    quotes = frames(days)
    source = SimpleNamespace(get_index_history_window=AsyncMock(side_effect=lambda code, **kw: quotes[code]))
    result = await window.refresh_verified_history(source=source, through_date=THROUGH, session_factory=maker)
    assert result["complete"] and result["inserted_count"] == result["repaired_invalid_count"] == 0
    after = list((await db.execute(select(StockDaily.__table__).order_by(StockDaily.id))).all())
    assert after == before
    # New forward-available source evidence is retained, not republished as the
    # old projection or made visible before the actual collection time.
    assert (await window.latest_verified_history(db, through_date=THROUGH, at=AT))[1]["complete"]
    assert (await window.latest_verified_history(db, through_date=THROUGH, at=AT-timedelta(microseconds=1)))[1] is None


@pytest.mark.asyncio
async def test_partial_source_does_not_backfill_or_publish_healthy_window(db, monkeypatch):
    days = trade_days(THROUGH, 65)
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    await db.commit()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    quotes = frames(days)
    quotes["399006"] = pd.DataFrame()
    source = SimpleNamespace(get_index_history_window=AsyncMock(side_effect=lambda code, **kw: quotes[code]))
    result = await window.refresh_verified_history(source=source, through_date=THROUGH, session_factory=maker)
    assert not result["complete"] and result["status"] == "degraded"
    assert await db.scalar(select(StockDaily.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("first_ok", [True, False])
async def test_index_http_contract_raw_day_and_explicit_market(monkeypatch, first_ok):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "proxy.finance.qq.com":
            return httpx.Response(200, json={"code": 0, "data": {"sh000001": {
                ("day" if first_ok else "qfqday"): [["2026-09-08", "3935", "3940", "3951", "3925", "999"]]}}})
        assert request.url.params["secid"] == "1.000001"
        assert request.url.params["fqt"] == "0"
        return httpx.Response(200, json={"rc": 0, "data": {"code": "000001",
            "klines": ["2026-09-08,3935,3940,3951,3925,999,999,0"]}})
    real_client = httpx.AsyncClient
    monkeypatch.setattr("app.data.sources.index_source.httpx.AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    frame = await IndexSource().get_index_history_window("000001", start_date=date(2026, 6, 1), end_date=THROUGH)
    if first_ok:
        assert frame.iloc[0]["close"] == "3940"
        assert frame.attrs["index_symbol"] == "sh000001"
        assert frame.attrs["source_contract"] == "tencent_index_raw_day_v1"
    else:
        assert frame.empty
        assert frame.attrs["source_failures"] == [{"provider": "tencent", "error_type": "ValueError"}]
    assert "volume" not in frame.columns and "amount" not in frame.columns
    assert len(calls) == 1  # 禁止回退东财


@pytest.mark.asyncio
async def test_stale_primary_window_stays_missing_without_eastmoney_fallback(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "proxy.finance.qq.com":
            return httpx.Response(200, json={"code": 0, "data": {"sh000001": {
                "day": [["2026-09-07", "3935", "3940", "3951", "3925"]]}}})
        return httpx.Response(200, json={"rc": 0, "data": {"code": "000001",
            "klines": ["2026-09-08,3935,3940,3951,3925,999,999,0"]}})
    client = httpx.AsyncClient
    monkeypatch.setattr("app.data.sources.index_source.httpx.AsyncClient",
                        lambda **kw: client(transport=httpx.MockTransport(handler), **kw))
    frame = await IndexSource().get_index_history_window("000001", start_date=THROUGH,
        end_date=THROUGH, expected_trade_dates=[THROUGH])
    assert len(calls) == 1
    assert frame.empty
    assert frame.attrs["source_failures"][0]["provider"] == "tencent"


@pytest.mark.asyncio
async def test_scheduler_never_fetches_history_in_trading_window(monkeypatch):
    import app.data.scheduler as module
    scheduler = module.DataScheduler()
    fetch = AsyncMock()
    monkeypatch.setattr(window, "refresh_verified_history", fetch)
    for hour, minute in ((9, 0), (9, 25), (10, 0), (12, 0), (14, 59), (15, 20)):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None): return datetime(2026, 9, 8, hour, minute)
        monkeypatch.setattr(module, "datetime", Clock)
        assert (await scheduler._refresh_index_history())["status"] == "outside_history_window"
    assert not fetch.called
    scheduler.setup_jobs()
    assert scheduler.scheduler.get_job("index_history_window") is not None
