"""Read-only recorded capital counts and lunch monitoring, not signal TTL changes."""
import copy
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api.v1 import tenbagger as api
from app.models.signal import AnomalyCandidateRecord
from app.models.stock import StockTag
from app.models.governance import DashboardSnapshot
import json

DAY = date(2026, 9, 11)
NOON = datetime(2026, 9, 11, 12, 18)


@pytest.fixture(autouse=True)
def isolate_snapshot_cache(monkeypatch):
    monkeypatch.setattr(api, "_ANOMALY_SNAPSHOT_CACHE", {})


def freeze_clock(monkeypatch, at):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at
    monkeypatch.setattr(api, "datetime", Clock)


def candidate(identity, code="000001", **kw):
    fields = dict(record_id=identity, identity=identity, code=code, name="测试股",
        trade_date=DAY, event_type="capital", first_seen_at=datetime(2026, 9, 11, 9, 31),
        last_seen_at=datetime(2026, 9, 11, 11, 29), status="rejected",
        last_score=90, seen_count=300, snapshot_json='{"unsafe_old_buy_point":true}')
    fields.update(kw)
    return AnomalyCandidateRecord(**fields)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(AnomalyCandidateRecord.__table__.create)
        await connection.run_sync(StockTag.__table__.create)
        await connection.run_sync(DashboardSnapshot.__table__.create)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recorded_counts_distinguish_stock_identity_and_repeated_observations(db):
    db.add_all([candidate("a"), candidate("b"), candidate("c", "600001",
        last_seen_at=datetime(2026, 9, 11, 11, 20), last_score=20)])
    await db.commit()
    statements = []
    def record_sql(conn, cursor, statement, params, context, many):
        statements.append(statement)
    event.listen(db.bind.sync_engine, "before_cursor_execute", record_sql)
    try:
        result = await api._load_recorded_capital_activity(db, trade_date=DAY, as_of_at=NOON)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", record_sql)
    assert result["status"] == "ok"
    assert result["stock_count"] == 2 and result["record_count"] == 3
    assert result["stocks"][0]["code"] == "000001"
    assert result["stocks"][0]["record_count"] == 2  # NOT seen_count=600
    assert result["last_seen_at"] == "2026-09-11T11:29:00"
    assert len(statements) == 1 and statements[0].lstrip().startswith("SELECT")
    assert "snapshot_json" not in statements[0]
    assert set(result["stocks"][0]) == {"code", "name", "first_seen_at", "last_seen_at", "record_count"}
    assert len(db.new) == 0 and len(db.dirty) == 0


@pytest.mark.asyncio
async def test_recorded_scope_rejects_wrong_dates_future_bad_order_and_blocked_boards(db):
    db.add_all([
        candidate("ok"), candidate("gem", "300001"), candidate("star", "688001"),
        candidate("st", "000002", name="*ST测试"), candidate("delistname", "000003", name="退市测试"),
        candidate("suspended", "000004"), candidate("delisting", "000005"),
        candidate("blocked", "000006"), candidate("sttag", "000007"),
        candidate("prior", "000008", trade_date=DAY-timedelta(days=1)),
        candidate("future", "000009", last_seen_at=NOON+timedelta(seconds=1)),
        candidate("reversed", "000010", first_seen_at=NOON),
        candidate("wrong_clock_day", "000011", first_seen_at=datetime(2026, 9, 10, 9, 30)),
        candidate("other_event", "000012", event_type="breakthrough"),
    ])
    db.add_all([
        StockTag(code="000004", name="停牌测试", board_type="main_sz", board_tag="tradeable", is_suspended=True),
        StockTag(code="000005", name="退市标记", board_type="main_sz", board_tag="tradeable", is_delisting=True),
        StockTag(code="000006", name="屏蔽测试", board_type="main_sz", board_tag="blocked"),
        StockTag(code="000007", name="特别处理", board_type="main_sz", board_tag="tradeable", is_st=True),
    ])
    await db.commit()
    result = await api._load_recorded_capital_activity(db, trade_date=DAY, as_of_at=NOON)
    assert result["stock_count"] == result["record_count"] == 1
    assert result["stocks"][0]["code"] == "000001"
    assert (await db.execute(select(AnomalyCandidateRecord))).scalars().all()  # read did not delete history


@pytest.mark.asyncio
async def test_empty_is_zero_but_unavailable_is_not_zero(db):
    empty = await api._load_recorded_capital_activity(db, trade_date=DAY, as_of_at=NOON)
    assert empty["status"] == "empty" and empty["stock_count"] == 0
    failing = SimpleNamespace(execute=AsyncMock(side_effect=OperationalError("read", {}, Exception("locked"))), rollback=AsyncMock())
    missing = await api._load_recorded_capital_activity(failing, trade_date=DAY, as_of_at=NOON)
    assert missing["status"] == "unavailable"
    assert missing["stock_count"] is None and missing["record_count"] is None
    assert missing["stocks"] == []
    failing.rollback.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [None, DAY+timedelta(days=1)])
async def test_unknown_or_future_trade_date_never_queries_history(day):
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("must not read")), rollback=AsyncMock())
    result = await api._load_recorded_capital_activity(db, trade_date=day, as_of_at=NOON)
    assert result["status"] == "unavailable" and result["stock_count"] is None
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("at,session", [
    (datetime(2026, 9, 11, 11, 29, 59), "morning"),
    (datetime(2026, 9, 11, 11, 30), "lunch_break"),
    (datetime(2026, 9, 11, 12, 59, 59), "lunch_break"),
    (datetime(2026, 9, 11, 13), "afternoon"),
    (datetime(2026, 9, 11, 15), "after_hours"),
    (datetime(2026, 9, 12, 10), "weekend"),
    (datetime(2026, 10, 1, 10), "holiday"),
])
def test_exchange_session_boundaries(at, session):
    assert api._monitor_market_session(at) == session


def snapshot(at=None, **kw):
    result = {"trade_date": str(DAY), "snapshot_time": (at or datetime(2026, 9, 11, 11, 29)).isoformat(),
              "anomalies": [], "summary": {}, "b1_states": []}
    result.update(kw)
    return result


@pytest.mark.asyncio
async def test_lunch_monitor_preserves_stored_clock_and_never_scans(monkeypatch):
    freeze_clock(monkeypatch, NOON)
    original = snapshot()
    before = copy.deepcopy(original)
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    saved = AsyncMock(return_value=original)
    scan = AsyncMock(side_effect=AssertionError("lunch cannot rescan"))
    monkeypatch.setattr(api, "_get_latest_persisted_anomaly_snapshot", saved)
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    result = await api.prewarm_anomaly_snapshot(object(), monitor_read=True)
    assert result["snapshot_time"] == "2026-09-11T11:29:00"
    assert original == before
    scan.assert_not_awaited()
    saved.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, snapshot(trade_date="2026-09-10"),
    snapshot(datetime(2026, 9, 11, 13)), snapshot(snapshot_time=None)])
async def test_lunch_missing_wrong_day_future_or_unknown_snapshot_is_unavailable(monkeypatch, saved):
    freeze_clock(monkeypatch, NOON)
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_latest_persisted_anomaly_snapshot", AsyncMock(return_value=saved))
    scan = AsyncMock(side_effect=AssertionError("no replacement snapshot"))
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    result = await api.prewarm_anomaly_snapshot(object(), monitor_read=True)
    assert result["degraded"] and result["snapshot_time"] is None
    assert result["anomalies"] == []
    scan.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("at,monitor,force", [(NOON, False, False), (NOON, True, True),
                                           (datetime(2026, 9, 11, 13), True, False)])
async def test_default_force_and_afternoon_use_original_scan_path(monkeypatch, at, monitor, force):
    freeze_clock(monkeypatch, at)
    monkeypatch.setattr(api, "_ANOMALY_SCAN_LOCKS", api.WeakKeyDictionary())
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_cached_anomaly_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(api, "_get_persisted_anomaly_snapshot", AsyncMock(return_value=None))
    old = AsyncMock(side_effect=AssertionError("must not reuse lunch history"))
    monkeypatch.setattr(api, "_get_latest_persisted_anomaly_snapshot", old)
    scan = AsyncMock(return_value=snapshot(at))
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    await api.prewarm_anomaly_snapshot(object(), monitor_read=monitor, force_refresh=force)
    scan.assert_awaited_once()
    old.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["stock", "event"])
@pytest.mark.parametrize("at,paused", [(NOON, True), (datetime(2026, 9, 11, 13), False)])
async def test_real_get_history_never_merges_into_current_signals(db, monkeypatch, view, at, paused):
    from app.dashboard2.service import dashboard2_service
    from tests.test_anomaly_monitor_performance import limit_event
    freeze_clock(monkeypatch, at)
    db.add(candidate("recorded", code="600001"))
    await db.commit()
    current = api._enrich_anomaly_display(limit_event(), [limit_event()])
    assert current["buy_point_pushable"]
    original = snapshot(at, anomalies=[current])
    before = copy.deepcopy(original)
    monkeypatch.setattr(api, "prewarm_anomaly_snapshot", AsyncMock(return_value=original))
    monkeypatch.setattr(api, "_enrich_stock_rows_with_b1", AsyncMock(side_effect=lambda rows, *a, **kw: rows))
    monkeypatch.setattr(api, "_attach_anomaly_display_context", AsyncMock(side_effect=lambda rows, *a, **kw: rows))
    monkeypatch.setattr(dashboard2_service, "_load_a_share_context", AsyncMock(side_effect=RuntimeError("isolated")))
    kwargs = dict(min_score=50, event_type="", setup_track="", sort_by="priority",
                  view=view, page=1, page_size=10, db=db, buy_point_only=False)
    result = await api.anomaly_monitor(**kwargs)
    assert result["capital_activity"]["stock_count"] == 1
    assert result["capital_activity"]["stocks"][0]["code"] == "600001"
    assert result["stock_summary"]["capital_count"] == 0
    assert result["detection_paused"] is paused
    assert result["monitor_observation_only"] is paused
    rows = result["rows" if view == "stock" else "anomalies"]
    assert all(row["code"] != "600001" for row in rows)
    assert bool(rows[0]["buy_point_pushable"]) is not paused
    if paused:
        assert result["snapshot_cache_policy"] == "lunch_read_only"
        assert result["stock_summary"]["buy_point_count"] == 0
    capital = await api.anomaly_monitor(**{**kwargs, "event_type": "capital"})
    assert capital["total"] == 0 and capital["capital_activity"]["stock_count"] == 1
    buy = await api.anomaly_monitor(**{**kwargs, "buy_point_only": True})
    assert buy["total"] == (0 if paused else 1)
    assert original == before


@pytest.mark.asyncio
async def test_lunch_persisted_row_must_itself_be_visible_at_cutoff(db, monkeypatch):
    freeze_clock(monkeypatch, NOON)
    db.add_all([
        DashboardSnapshot(snapshot_key=api._anomaly_snapshot_key(), trade_date=DAY,
            snapshot_time=datetime(2026, 9, 11, 11, 30),
            payload_json=json.dumps(snapshot(marker="visible")), status="ok"),
        DashboardSnapshot(snapshot_key=api._anomaly_snapshot_key(), trade_date=DAY,
            snapshot_time=datetime(2026, 9, 11, 13),
            payload_json=json.dumps(snapshot(marker="future_row")), status="ok"),
    ])
    await db.commit()
    result = await api._get_latest_persisted_anomaly_snapshot(db, DAY, as_of_at=NOON)
    assert result["marker"] == "visible"
    # Optional cutoff does not change legacy consumers' default contract.
    latest = await api._get_latest_persisted_anomaly_snapshot(db, DAY)
    assert latest["marker"] == "future_row"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [False, True])
async def test_lunch_memory_fallback_preserves_original_data(monkeypatch, error):
    freeze_clock(monkeypatch, NOON)
    original = snapshot()
    before = copy.deepcopy(original)
    api._set_cached_anomaly_snapshot(DAY, original)
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_latest_persisted_anomaly_snapshot",
        AsyncMock(side_effect=OperationalError("read", {}, Exception("busy"))) if error else AsyncMock(return_value=None))
    scan = AsyncMock(side_effect=AssertionError("must not scan"))
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    db = SimpleNamespace(rollback=AsyncMock())
    result = await api.prewarm_anomaly_snapshot(db, monitor_read=True)
    assert result["snapshot_time"] == before["snapshot_time"]
    assert result is not original and original == before
    assert bool(result.get("degraded")) is error
    scan.assert_not_awaited()
    assert db.rollback.await_count == int(error)


@pytest.mark.asyncio
async def test_afternoon_rejects_even_freshly_minted_lunch_snapshot(monkeypatch):
    at = datetime(2026, 9, 11, 13)
    freeze_clock(monkeypatch, at)
    old = snapshot(at - timedelta(seconds=20))
    monkeypatch.setattr(api, "_ANOMALY_SCAN_LOCKS", api.WeakKeyDictionary())
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_cached_anomaly_snapshot", lambda *a, **kw: old)
    monkeypatch.setattr(api, "_get_persisted_anomaly_snapshot", AsyncMock(return_value=old))
    scan = AsyncMock(return_value=snapshot(at))
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    result = await api.prewarm_anomaly_snapshot(object(), monitor_read=True)
    scan.assert_awaited_once()
    assert result["snapshot_time"] == at.isoformat()
    assert old["snapshot_time"] == "2026-09-11T12:59:40"


@pytest.mark.asyncio
async def test_lunch_missing_today_quotes_never_rescans_fallback_trade_date(monkeypatch):
    freeze_clock(monkeypatch, NOON)
    prior = DAY - timedelta(days=1)
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=prior))
    monkeypatch.setattr(api, "_get_latest_persisted_anomaly_snapshot", AsyncMock(return_value=None))
    scan = AsyncMock(side_effect=AssertionError("must not rescan stale fallback day"))
    monkeypatch.setattr(api, "_scan_anomaly_snapshot", scan)
    result = await api.prewarm_anomaly_snapshot(object(), monitor_read=True)
    assert result["degraded"] and result["trade_date"] == str(prior)
    assert result["snapshot_time"] is None
    scan.assert_not_awaited()


@pytest.mark.asyncio
async def test_corrupt_sqlite_timestamp_does_not_break_auxiliary_history(db):
    db.add(candidate("bad_clock"))
    await db.commit()
    # SQL literal is deliberately corrupt ONLY inside this isolated memory DB.
    await db.execute(text("UPDATE anomaly_candidate_record SET last_seen_at='2026-09-11 10:invalid' WHERE record_id='bad_clock'"))
    await db.commit()
    result = await api._load_recorded_capital_activity(db, trade_date=DAY, as_of_at=NOON)
    assert result["status"] == "unavailable" and result["stock_count"] is None
    assert result["stocks"] == []


@pytest.mark.asyncio
async def test_get_failed_snapshot_and_no_memory_never_fabricates_snapshot_clock(db, monkeypatch):
    from app.dashboard2.service import dashboard2_service
    freeze_clock(monkeypatch, NOON)
    monkeypatch.setattr(api, "prewarm_anomaly_snapshot", AsyncMock(side_effect=OperationalError("read", {}, Exception("busy"))))
    monkeypatch.setattr(api, "_enrich_stock_rows_with_b1", AsyncMock(side_effect=lambda rows, *a, **kw: rows))
    monkeypatch.setattr(dashboard2_service, "_load_a_share_context", AsyncMock(side_effect=RuntimeError("isolated")))
    result = await api.anomaly_monitor(min_score=50, event_type="", setup_track="", sort_by="priority",
        buy_point_only=False, view="stock", page=1, page_size=10, db=db)
    assert result["snapshot_time"] is None and result["degraded"]
    assert result["monitor_observation_only"] is True and result["detection_paused"] is True
    assert result["capital_activity"]["stock_count"] is None
    assert result["rows"] == []
