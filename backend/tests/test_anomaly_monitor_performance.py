"""Monitor reuse never caches execution evidence or changes technical formulas."""
import asyncio
import copy
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.v1 import tenbagger as api
from app.models.stock import FundFlow, StockKline, StockSpot, StockSectorMapping

DAY = date(2026, 9, 10)


@pytest.fixture(autouse=True)
def isolated_caches(monkeypatch):
    monkeypatch.setattr(api, "_ANOMALY_SNAPSHOT_CACHE", {})
    monkeypatch.setattr(api, "_B1_ANALYSIS_CACHE", api.OrderedDict())
    monkeypatch.setattr(api, "_ANOMALY_SCAN_LOCKS", api.WeakKeyDictionary())
    monkeypatch.setattr(api, "_load_recorded_capital_activity", AsyncMock(return_value={"status": "unavailable"}))


def clock(monkeypatch, value):
    state = [value]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return state[0]
    monkeypatch.setattr(api, "datetime", Clock)
    return state


def payload(at, day=DAY):
    return {"trade_date": str(day), "snapshot_time": at.isoformat(),
            "anomalies": [], "b1_states": [], "summary": {}}


@pytest.mark.parametrize("at,day,ttl", [
    (datetime(2026, 9, 10, 9, 15), DAY, 45),
    (datetime(2026, 9, 10, 10), DAY, 45),
    (datetime(2026, 9, 10, 12), DAY, 45),
    (datetime(2026, 9, 10, 15, 14, 59), DAY, 45),
    (datetime(2026, 9, 10, 15, 15), DAY, 600),
    (datetime(2026, 9, 11, 1), DAY, 600),
    (datetime(2026, 9, 11, 9, 14, 59), DAY, 600),
    (datetime(2026, 9, 11, 9, 15), DAY, 45),
    (datetime(2026, 9, 12, 10), DAY, 600),
    (datetime(2026, 9, 10, 20), date(2026, 9, 11), 45),
])
def test_monitor_reuse_windows(at, day, ttl):
    assert api._monitor_snapshot_ttl(day, at) == ttl


@pytest.mark.parametrize("age,accepted", [(0, True), (45, True), (46, False), (-1, False)])
def test_live_snapshot_absolute_age(monkeypatch, age, accepted):
    now = datetime(2026, 9, 10, 10)
    clock(monkeypatch, now)
    api._set_cached_anomaly_snapshot(DAY, payload(now - timedelta(seconds=age)))
    assert (api._get_cached_anomaly_snapshot(DAY) is not None) == accepted


def test_monitor_does_not_renew_original_clock_or_signal_ttl(monkeypatch):
    now = datetime(2026, 9, 10, 18)
    tick = clock(monkeypatch, now)
    original = payload(now - timedelta(seconds=590))
    api._set_cached_anomaly_snapshot(DAY, original)
    assert api._get_cached_anomaly_snapshot(DAY) is None
    assert api._get_cached_anomaly_snapshot(DAY, ttl_seconds=600)["snapshot_time"] == original["snapshot_time"]
    tick[0] += timedelta(seconds=11)
    assert api._get_cached_anomaly_snapshot(DAY, ttl_seconds=600) is None
    assert original["snapshot_time"] == (now - timedelta(seconds=590)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_age,row_age,accepted", [(590, 590, True), (601, 0, False), (0, -1, False), (-1, 0, False)])
async def test_persisted_age_cannot_be_reset_by_recent_database_write(monkeypatch, payload_age, row_age, accepted):
    now = datetime(2026, 9, 10, 18)
    clock(monkeypatch, now)
    saved = SimpleNamespace(snapshot_time=now - timedelta(seconds=row_age),
        payload_json=json.dumps(payload(now - timedelta(seconds=payload_age))))
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: saved)))
    result = await api._get_persisted_anomaly_snapshot(db, DAY, ttl_seconds=600)
    assert (result is not None) == accepted


def test_wrong_day_or_missing_snapshot_time_is_not_reusable(monkeypatch):
    now = datetime(2026, 9, 10, 18)
    clock(monkeypatch, now)
    api._set_cached_anomaly_snapshot(DAY, payload(now, DAY - timedelta(days=1)))
    assert api._get_cached_anomaly_snapshot(DAY, ttl_seconds=600) is None
    api._set_cached_anomaly_snapshot(DAY, {"trade_date": str(DAY), "anomalies": []})
    assert api._get_cached_anomaly_snapshot(DAY, ttl_seconds=600) is None


def test_cache_date_retention_is_bounded():
    for i in range(5):
        day = DAY + timedelta(days=i)
        api._set_cached_anomaly_snapshot(day, payload(datetime.now(), day))
    assert len(api._ANOMALY_SNAPSHOT_CACHE) == 3


def bars():
    return [{"trade_date": DAY - timedelta(days=80-i), "open": 10 + i * .01,
             "high": 10.4 + i * .01, "low": 9.8 + i * .01, "close": 10.1 + i * .01,
             "volume": 100000+i*100, "turnover": 1.5, "change_pct": .1} for i in range(80)]


def test_identical_technical_input_reuses_analysis_and_returns_owned_values(monkeypatch):
    real = api.analyze_b1_signal
    called = Mock(wraps=real)
    monkeypatch.setattr(api, "analyze_b1_signal", called)
    source = bars()
    expected = real("000001", source)
    first = api._cached_b1_analysis("000001", source)
    first["close"] = -1
    second = api._cached_b1_analysis("000001", copy.deepcopy(source))
    assert second == expected
    assert called.call_count == 1
    assert source == bars()


@pytest.mark.parametrize("field", ["close", "high", "low", "open", "volume", "turnover", "change_pct", "trade_date"])
def test_any_repaired_historical_bar_invalidates_analysis(monkeypatch, field):
    called = Mock(return_value={"close": 10, "nested": {"j": 20}})
    monkeypatch.setattr(api, "analyze_b1_signal", called)
    source = bars()
    api._cached_b1_analysis("000001", source)
    source[0][field] = DAY if field == "trade_date" else source[0][field] + 1
    api._cached_b1_analysis("000001", source)
    assert called.call_count == 2


def test_live_bar_stock_and_evaluator_are_part_of_key(monkeypatch):
    called = Mock(return_value={})
    monkeypatch.setattr(api, "analyze_b1_signal", called)
    source = bars()
    api._cached_b1_analysis("000001", source)
    api._cached_b1_analysis("000002", source)
    source[-1]["close"] += .5
    api._cached_b1_analysis("000001", source)
    assert called.call_count == 3
    newer = Mock(return_value={"close": 99})
    monkeypatch.setattr(api, "analyze_b1_signal", newer)
    assert api._cached_b1_analysis("000001", source)["close"] == 99
    newer.assert_called_once()


def test_lru_is_bounded_and_failed_analysis_is_not_cached(monkeypatch):
    monkeypatch.setattr(api, "_B1_ANALYSIS_CACHE_MAX_ENTRIES", 2)
    called = Mock(return_value={})
    monkeypatch.setattr(api, "analyze_b1_signal", called)
    for code in ["000001", "000002", "000003", "000001"]:
        api._cached_b1_analysis(code, bars())
    assert called.call_count == 4
    assert len(api._B1_ANALYSIS_CACHE) == 2
    called.side_effect = RuntimeError("bad input")
    with pytest.raises(RuntimeError):
        api._cached_b1_analysis("000004", bars())
    assert len(api._B1_ANALYSIS_CACHE) == 2


@pytest.mark.asyncio
async def test_monitor_reuse_not_available_to_live_or_explicit_refresh(monkeypatch):
    now = datetime(2026, 9, 10, 18)
    clock(monkeypatch, now)
    api._set_cached_anomaly_snapshot(DAY, payload(now - timedelta(seconds=120)))
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_persisted_anomaly_snapshot", AsyncMock(return_value=None))
    scan = AsyncMock(return_value=payload(now))
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    await api.prewarm_anomaly_snapshot(None, monitor_read=True)
    scan.assert_not_awaited()
    await api.prewarm_anomaly_snapshot(None)
    assert scan.await_count == 1
    await api.prewarm_anomaly_snapshot(None, monitor_read=True, force_refresh=True)
    assert scan.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_cold_reads_scan_once(monkeypatch):
    now = datetime(2026, 9, 10, 10)
    clock(monkeypatch, now)
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_persisted_anomaly_snapshot", AsyncMock(return_value=None))
    started, release = asyncio.Event(), asyncio.Event()
    async def scan(db, day):
        started.set()
        await release.wait()
        result = payload(now, day)
        api._set_cached_anomaly_snapshot(day, result)
        return result
    wrapped = AsyncMock(side_effect=scan)
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", wrapped)
    tasks = [asyncio.create_task(api.prewarm_anomaly_snapshot(None, monitor_read=True)) for _ in range(6)]
    await started.wait()
    release.set()
    results = await asyncio.gather(*tasks)
    assert wrapped.await_count == 1
    assert all(result == results[0] for result in results)


@pytest.mark.asyncio
async def test_failed_or_cancelled_scan_releases_lock(monkeypatch):
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_persisted_anomaly_snapshot", AsyncMock(return_value=None))
    scan = AsyncMock(side_effect=[RuntimeError("scan failed"), asyncio.CancelledError(), payload(datetime.now())])
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    with pytest.raises(RuntimeError):
        await api.prewarm_anomaly_snapshot(None)
    with pytest.raises(asyncio.CancelledError):
        await api.prewarm_anomaly_snapshot(None)
    result = await asyncio.wait_for(api.prewarm_anomaly_snapshot(None), timeout=1)
    assert result["trade_date"] == str(DAY)
    assert scan.await_count == 3


@pytest.mark.asyncio
async def test_b1_cache_hit_still_revalidates_real_fund_clocks(monkeypatch):
    now = datetime(2026, 9, 10, 10)
    tick = clock(monkeypatch, now)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        for model in (FundFlow, StockKline, StockSpot, StockSectorMapping):
            await conn.run_sync(model.__table__.create)
    called = Mock(wraps=api.analyze_b1_signal)
    monkeypatch.setattr(api, "analyze_b1_signal", called)
    try:
        async with sessions() as db:
            db.add_all([StockKline(code="000001", **bar) for bar in bars()])
            db.add(FundFlow(code="000001", trade_date=DAY, source="tencent",
                source_version="tencent_hsfundtab_v1", main_net_inflow=10000000,
                main_net_inflow_pct=3, source_quote_at=now-timedelta(seconds=2),
                received_at=now-timedelta(seconds=1), observed_at=now))
            await db.commit()
            row = {"code": "000001", "display_score": 50, "detail": {"price": 10}}
            first = (await api._enrich_stock_rows_with_b1([row], db, target_date=DAY))[0]
            assert first["main_net_inflow"] == 10000000
            tick[0] += timedelta(seconds=601)
            second = (await api._enrich_stock_rows_with_b1([row], db, target_date=DAY))[0]
            assert second["main_net_inflow"] is None
            assert second["detail"]["is_stale"] is True
            # B1 is a technical strategy, not a funding gate: this only asserts
            # money expires while the identical technical computation is reused.
            assert called.call_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_snapshot_version_does_not_reuse_legacy_persisted_payload(monkeypatch):
    from app.models.governance import DashboardSnapshot
    now = datetime(2026, 9, 10, 18)
    clock(monkeypatch, now)
    assert api.ANOMALY_SNAPSHOT_VERSION != "v17"
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(DashboardSnapshot.__table__.create)
    try:
        async with async_sessionmaker(engine)() as db:
            db.add(DashboardSnapshot(snapshot_key="tenbagger-anomalies:v17", trade_date=DAY,
                snapshot_time=now, payload_json=json.dumps(payload(now)), status="ok"))
            await db.commit()
            assert await api._get_persisted_anomaly_snapshot(db, DAY, ttl_seconds=600) is None
            assert await api._get_latest_persisted_anomaly_snapshot(db, DAY) is None
            monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
            scan = AsyncMock(return_value={**payload(now), "new_contract": True})
            monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
            result = await api.prewarm_anomaly_snapshot(db, monitor_read=True)
            assert result["new_contract"] is True
            scan.assert_awaited_once()
    finally:
        await engine.dispose()


def limit_event():
    return {
        "code": "000014", "name": "隔离盘口样本", "event_type": "limit_up",
        "score": 92, "level": "critical", "is_one_word_board": False,
        "detail": {"price": 12.8, "consecutive_days": 1, "turnover": 13.5,
            "volume_ratio": 1.9, "support_strength_score": 68, "seal_quality_score": 82,
            "orderbook_imbalance": .16, "bid_depth_5": 36000, "ask_depth_5": 22000,
            "quote_source_at": "2026-09-10T15:00:00",
            "quote_received_at": "2026-09-10T15:00:01",
            "sector_factors": [{"sector_name": "算力", "fund_flow": 8,
                "change_pct": 1.2, "strength_score": 70}]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["stock", "event"])
@pytest.mark.parametrize("age", [0, 299])
async def test_real_get_closed_snapshot_strips_current_book_and_b1_authority(monkeypatch, view, age):
    from app.dashboard2.service import dashboard2_service
    now = datetime(2026, 9, 10, 15, 20)
    clock(monkeypatch, now)
    event = api._enrich_anomaly_display(limit_event(), [limit_event()])
    assert event["buy_point_pushable"]  # prove the original failing positive case
    frozen = {**payload(now-timedelta(seconds=age)), "anomalies": [event],
              "b1_states": [{"code": "000014", "signal_status": "close_confirmed", "pushable": True}]}
    before = copy.deepcopy(frozen)
    api._set_cached_anomaly_snapshot(DAY, frozen)
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_persisted_anomaly_snapshot", AsyncMock(side_effect=AssertionError("must reuse cache")))
    monkeypatch.setattr(dashboard2_service, "_load_a_share_context", AsyncMock(side_effect=RuntimeError("isolated")))
    async def b1(rows, *args, **kwargs):
        # An independently valid B1 must not resurrect current authority either.
        return [{**row, "b1_signal_key": "original_b1", "b1_signal_status": "close_confirmed",
                 "b1_hold_score": 4, "b1_pushable": True} for row in rows]
    monkeypatch.setattr(api, "_enrich_stock_rows_with_b1", b1)
    monkeypatch.setattr(api, "_attach_anomaly_display_context", AsyncMock(side_effect=lambda rows, *a, **kw: rows))
    db = SimpleNamespace(rollback=AsyncMock())
    kwargs = dict(min_score=50, event_type="", setup_track="", sort_by="priority",
                  view=view, page=1, page_size=10, db=db)
    observed = await api.anomaly_monitor(**kwargs, buy_point_only=False)
    items = observed["rows" if view == "stock" else "anomalies"]
    assert len(items) == 1
    assert observed["monitor_observation_only"] is True
    assert observed["snapshot_cache_policy"] == "post_close_read_only"
    assert observed["snapshot_age_seconds"] == age
    assert observed["b1_summary"]["pushable"] == 0
    assert observed["stock_summary"]["buy_point_count"] == 0
    assert items[0]["setup_track"] == ""
    assert items[0]["setup_grade"] == "快照观察"
    for item in [items[0], *items[0].get("events", [])]:
        assert not item.get("buy_point_reached")
        assert not item.get("buy_point_pushable")
        assert not item.get("feishu_pushable")
        assert not item.get("b1_pushable")
    filtered = await api.anomaly_monitor(**kwargs, buy_point_only=True)
    assert filtered["total"] == 0
    assert frozen == before


@pytest.mark.asyncio
async def test_live_get_b1_summary_uses_current_rows_not_old_snapshot(monkeypatch):
    from app.dashboard2.service import dashboard2_service
    now = datetime(2026, 9, 10, 10)
    clock(monkeypatch, now)
    event = api._enrich_anomaly_display(limit_event(), [limit_event()])
    monkeypatch.setattr(api, "prewarm_anomaly_snapshot", AsyncMock(return_value={
        **payload(now), "anomalies": [event],
        "b1_states": [{"code": "000014", "signal_status": "close_confirmed", "pushable": True}]}))
    async def no_current_b1(rows, *args, **kwargs):
        return [{**row, "b1_signal_key": "", "b1_signal_status": "", "b1_pushable": False} for row in rows]
    monkeypatch.setattr(api, "_enrich_stock_rows_with_b1", no_current_b1)
    monkeypatch.setattr(api, "_attach_anomaly_display_context", AsyncMock(side_effect=lambda rows, *a, **kw: rows))
    monkeypatch.setattr(dashboard2_service, "_load_a_share_context", AsyncMock(side_effect=RuntimeError("isolated")))
    result = await api.anomaly_monitor(min_score=50, event_type="", setup_track="",
        sort_by="priority", buy_point_only=False, view="stock", page=1, page_size=10,
        db=SimpleNamespace(rollback=AsyncMock()))
    assert result["monitor_observation_only"] is False
    assert result["b1_summary"]["pushable"] == 0
    assert result["b1_summary"]["total"] == 0
    # A live event is not stripped merely because the independent B1 was lost.
    assert result["rows"][0]["buy_point_pushable"] is True
