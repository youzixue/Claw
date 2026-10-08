"""Prospective session research tests; isolated SQLite, fixture HTTP, no production writes."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.data.after_hours import (
    observation, append_observations, read_after_hours, research_features,
    regular_close_material, ths_tail,
)
from app.data.sources.after_hours_source import parse_official, AfterHoursSource, MAX_BYTES
from app.data.sources.ths_kline_source import ThsKlineSource
from app.models.stock import StockAfterHoursObservation, StockKline, StockTag
from app.models.governance import TradeCalendarModel
from app.models.review import DailyReviewSnapshot
from app.review.overnight_evidence import read_premarket_context, _frozen_dimensions

DAY = date(2026, 9, 30)
RECEIVED = datetime(2026, 9, 30, 16, 30)

def sse(**changes):
    value = {"code": "600000", "date": 20260930, "time": 162901,
             "snap": ["浦发银行", 9.48, 147484820, 1386209937, 44500, 421860, "D0      "]}
    value.update(changes)
    return value

def szse(**changes):
    data = {"code": "000001", "marketTime": "2026-09-30 15:30:00",
            "close": "11.35", "now": "11.57", "volume": 1045357, "amount": 1205814857.64,
            "volumeAhT": 887, "amountAhT": 1026259, "tradingPhaseCode2": "00"}
    data.update(changes)
    return {"code": "0", "data": data}

def official_row(code="600000", received=RECEIVED):
    material = parse_official(sse() if code == "600000" else szse(),
                             code=code, day=DAY, received_at=received)
    return observation(code=code, day=DAY, stage="after_hours", accepted_at=received, **material)

@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'research.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as session:
            yield session
    finally:
        await engine.dispose()

def test_official_units_total_not_double_added_and_szse_previous_close():
    for code, payload, shares, amount, total, close in [
        ("600000", sse(), 44500, 421860, 147484820, 9.48),
        ("000001", szse(), 88700, 1026259, 104535700, 11.57),
    ]:
        material = parse_official(payload, code=code, day=DAY, received_at=RECEIVED)
        row = observation(code=code, day=DAY, stage="after_hours", accepted_at=RECEIVED, **material)
        result = research_features(None, json.loads(row.payload_json))
        assert result["after_volume_shares"] == shares
        assert result["after_amount_yuan"] == amount
        assert result["all_day_volume_shares"] == total
        assert result["after_volume_ratio"] == pytest.approx(shares / total)
        assert result["derived_regular_volume_shares"] == total - shares
        assert result["regular_volume_shares"] is None
        assert result["close_price_1500"] == close
        assert result["historical_pit"] is result["trading_authority"] is False
        assert result["source_finality_verified"] is False

@pytest.mark.parametrize("changes", [
    {"code": "600001"}, {"date": 20260929}, {"time": 173001}, {"snap": []},
])
def test_source_identity_date_clock_shape_fail_closed(changes):
    with pytest.raises(ValueError):
        parse_official(sse(**changes), code="600000", day=DAY, received_at=RECEIVED)

@pytest.mark.parametrize("code", ["830001", "510300", "000001x", "6000000", "６０００００"])
def test_uncertified_security_rejected(code):
    with pytest.raises(ValueError):
        parse_official(sse(), code=code, day=DAY, received_at=RECEIVED)

@pytest.mark.parametrize("changes", [
    {"tradingPhaseCode2": "unknown"}, {"marketTime": "2026-09-30 15:29:59"},
    {"volumeAhT": None}, {"amountAhT": None}, {"volumeAhT": -1},
    {"amountAhT": float("nan")}, {"volumeAhT": 1.2},
])
def test_missing_phase_clock_units_values_are_not_zero_or_fill(changes):
    material = parse_official(szse(**changes), code="000001", day=DAY, received_at=RECEIVED)
    row = observation(code="000001", day=DAY, stage="after_hours", accepted_at=RECEIVED, **material)
    assert row.quality_status == "partial"
    assert research_features(None, json.loads(row.payload_json))["after_volume_ratio"] is None

def test_native_zero_is_preserved_but_unknown_not_zero():
    material = parse_official(szse(volumeAhT=0, amountAhT=0), code="000001", day=DAY, received_at=RECEIVED)
    row = observation(code="000001", day=DAY, stage="after_hours", accepted_at=RECEIVED, **material)
    assert row.quality_status == "observed"
    assert research_features(None, json.loads(row.payload_json))["after_volume_ratio"] == 0
    material["values"]["after_amount_yuan"] = 1
    assert observation(code="000001", day=DAY, stage="after_hours", accepted_at=RECEIVED, **material).quality_status == "partial"

def test_total_below_post_and_unclassified_supplier_never_added():
    material = parse_official(szse(volume=1), code="000001", day=DAY, received_at=RECEIVED)
    bad = observation(code="000001", day=DAY, stage="after_hours", accepted_at=RECEIVED, **material)
    assert "daily_total_less_than_after_hours" in json.loads(bad.payload_json)["missing"]
    row = observation(code="600000", day=DAY, stage="after_hours",
                      source="ths", source_version="fixture", received_at=RECEIVED, accepted_at=RECEIVED,
                      values={**ths_tail([""]*9 + ["44500", "421860"]),
                              "reported_daily_volume": 100000, "reported_daily_amount": 999999})
    result = research_features(None, json.loads(row.payload_json))
    assert result["after_volume_shares"] == 44500
    assert result["all_day_volume_shares"] is result["after_volume_ratio"] is None

def test_baseline_window_does_not_upgrade_tencent_estimated_amount():
    at = datetime(2026, 9, 30, 15, 1)
    spot = SimpleNamespace(source_quote_at=datetime(2026, 9, 30, 15),
        received_at=at, price=9.48, volume=1000, amount=999999)
    value = regular_close_material(spot, day=DAY, received_at=at)
    assert value["regular_volume_shares"] == 100000
    assert value["regular_amount_yuan"] is None
    spot.source_quote_at = datetime(2026, 9, 30, 15, 5)
    assert regular_close_material(spot, day=DAY, received_at=datetime(2026, 9, 30, 15, 6))["close_price_1500"] is None

@pytest.mark.asyncio
async def test_idempotent_content_retains_first_availability_and_revision(db):
    first = official_row()
    assert (await append_observations(db, [first]))["inserted"] == 1
    later = official_row(received=RECEIVED + timedelta(hours=1))
    assert (await append_observations(db, [later]))["inserted"] == 0
    await db.commit()
    saved = await db.scalar(select(StockAfterHoursObservation))
    assert saved.available_at == RECEIVED
    result = await read_after_hours(db, day=DAY, as_of=RECEIVED)
    assert result["returned_codes"] == 1
    assert not result["complete_market_coverage"]
    assert (await read_after_hours(db, day=DAY, as_of=RECEIVED - timedelta(seconds=1)))["items"] == []
    changed = sse()
    changed["snap"][4] = 44501
    mat = parse_official(changed, code="600000", day=DAY, received_at=RECEIVED + timedelta(hours=1))
    revision = observation(code="600000", day=DAY, stage="after_hours", **mat,
                           accepted_at=RECEIVED + timedelta(hours=1))
    await append_observations(db, [revision])
    await db.commit()
    assert (await read_after_hours(db, day=DAY, as_of=RECEIVED))["items"][0]["after_volume_shares"] == 44500
    assert (await read_after_hours(db, day=DAY, as_of=RECEIVED + timedelta(hours=1)))["items"][0]["after_volume_shares"] == 44501

@pytest.mark.asyncio
async def test_observed_on_october_2_never_certifies_september_30_cutoff(db):
    now = datetime(2026, 10, 2, 0, 20)
    await append_observations(db, [official_row(received=now)])
    await db.commit()
    assert not (await read_after_hours(db, day=DAY, as_of=RECEIVED))["items"]
    assert (await read_after_hours(db, day=DAY, as_of=now))["items"][0]["source_refs"][0]["available_at"] == now.isoformat()

@pytest.mark.asyncio
async def test_append_only_orm_and_sql_and_bounded_read(db):
    await append_observations(db, [official_row(), official_row("000001")])
    await db.commit()
    result = await read_after_hours(db, day=DAY, as_of=RECEIVED, limit=1)
    assert result["truncated"] and result["stored_code_count"] == 2
    row = await db.scalar(select(StockAfterHoursObservation).limit(1))
    row.quality_status = "fake"
    with pytest.raises(ValueError, match="append-only"):
        await db.flush()
    await db.rollback()
    with pytest.raises(Exception, match="append-only"):
        await db.execute(text("DELETE FROM stock_after_hours_observation"))
    await db.rollback()

@pytest.mark.asyncio
async def test_official_adapter_bounded_http_fixture(monkeypatch):
    monkeypatch.setattr("app.data.sources.after_hours_source.local_now", lambda: RECEIVED)
    async def fixture(request):
        return httpx.Response(200, json=sse())
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture)) as client:
        material = await AfterHoursSource().collect("600000", trade_date=DAY, client=client)
        assert len(material["values"]["response_hash"]) == 64
        assert material["received_at"] == RECEIVED
    async def oversized(request):
        return httpx.Response(200, content=b"x" * (MAX_BYTES + 1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(oversized)) as client:
        with pytest.raises(ValueError, match="byte budget"):
            await AfterHoursSource().collect("600000", trade_date=DAY, client=client)

@pytest.mark.asyncio
async def test_ths_actual_parser_keeps_independent_tail_and_explicit_day(monkeypatch):
    encoded = 'quotebridge({"today":"20261002","data":"20260930,9.4,9.5,9.3,9.48,147484820,1386209937,1,,44500,421860"});'
    real_client = httpx.AsyncClient
    async def fixture(request):
        return httpx.Response(200, text=encoded)
    monkeypatch.setattr("app.data.sources.ths_kline_source.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fixture), **kwargs))
    source = ThsKlineSource()
    source._get_cookie = AsyncMock(return_value="")
    row = await source.collect_after_hours("600000", trade_date=DAY)
    assert row["trade_date"] == DAY.isoformat()
    assert row["after_hours"]["after_volume_shares"] == 44500
    assert row["after_hours"]["after_amount_yuan"] == 421860
    assert row["source_published_at"] is None
    assert await source.collect_after_hours("600000", trade_date=date(2026, 10, 1)) is None

@pytest.mark.asyncio
async def test_premarket_fuses_exact_previous_frozen_dimensions_across_holiday(db):
    target, cutoff = date(2026, 10, 8), datetime(2026, 10, 8, 8)
    day = DAY
    while day <= target:
        db.add(TradeCalendarModel(trade_date=day, is_trade_day=day in {DAY, target}, session_type="full"))
        day += timedelta(days=1)
    db.add(StockKline(code="600000", trade_date=DAY, close=9.48, source="ths", volume=1000))
    frozen = {"dimensions": {"fundamental": {"median_pe": 12, "candidates": [{"code": "600000"}]},
                             "technical": {"source": "frozen_kline", "technical_data_date": DAY.isoformat()},
                             "capital": {"source": "frozen_capital"}}}
    db.add(DailyReviewSnapshot(review_key="frozen-prior", phase="postmarket", review_date=DAY,
        analysis_trade_date=DAY, as_of_at=datetime(2026, 9, 30, 20, 35),
        created_at=datetime(2026, 9, 30, 20, 35), payload_json=json.dumps(frozen),
        data_version="frozen", schema_version="fixture", quality_status="partial"))
    await append_observations(db, [official_row()])
    await db.commit()
    result = await read_premarket_context(db, trade_date=target, as_of=cutoff)
    assert result["after_hours"]["trade_date"] == DAY.isoformat()
    assert result["after_hours"]["items"][0]["after_volume_shares"] == 44500
    dimensions = result["research_fusion"]["prior_frozen_dimensions"]
    assert dimensions["items"]["fundamental"]["median_pe"] == 12
    assert dimensions["snapshot_id"] == result["previous_postmarket"]["snapshot_id"]
    assert dimensions["analysis_trade_date"] == dimensions["expected_previous"] == DAY.isoformat()
    assert not result["research_fusion"]["recomputed_previous_day_from_current_spot"]
    assert result["news"]["window_start"] == "2026-09-30T15:00:00"
    assert not result["readiness"]["complete_overnight_coverage"]

@pytest.mark.asyncio
async def test_stale_snapshot_is_diagnostic_only_not_premarket_fusion(db):
    target, cutoff = date(2026, 10, 8), datetime(2026, 10, 8, 8)
    day = DAY - timedelta(days=1)
    while day <= target:
        db.add(TradeCalendarModel(trade_date=day, is_trade_day=day in {DAY - timedelta(days=1), DAY, target}, session_type="full"))
        day += timedelta(days=1)
    stale = datetime(2026, 9, 29, 20, 35)
    db.add(DailyReviewSnapshot(review_key="stale-prior", phase="postmarket", review_date=stale.date(),
        analysis_trade_date=stale.date(), as_of_at=stale, created_at=stale,
        payload_json=json.dumps({"dimensions": {"fundamental": {"median_pe": 12}}}),
        data_version="stale", schema_version="fixture", quality_status="partial"))
    await db.commit()
    result = await read_premarket_context(db, trade_date=target, as_of=cutoff)
    assert result["previous_postmarket"]["analysis_trade_date"] == stale.date().isoformat()
    assert result["freshness"]["alignment"]["postmarket"] == "stale"
    dimensions = result["research_fusion"]["prior_frozen_dimensions"]
    assert dimensions["status"] == "unavailable" and "items" not in dimensions
    assert dimensions["reference_snapshot_id"] == result["previous_postmarket"]["snapshot_id"]


def test_frozen_dimensions_missing_and_truncation_are_explicit():
    assert _frozen_dimensions('{"other":1}')["status"] == "unavailable"
    result = _frozen_dimensions(json.dumps({"dimensions": {
        "fundamental": {"candidates": list(range(100))}, "technical": {}, "capital": {}}}))
    assert len(result["items"]["fundamental"]["candidates"]) == 30
    assert result["truncated_paths"] == ["fundamental.candidates"]

@pytest.mark.asyncio
async def test_scheduler_holiday_and_closed_window_never_fetch(monkeypatch, db):
    from app.data import scheduler as module
    import app.data.sources.after_hours_source as adapter
    forbidden = AsyncMock(side_effect=AssertionError("network must not run"))
    monkeypatch.setattr(adapter.AfterHoursSource, "collect", forbidden)
    monkeypatch.setattr(adapter, "local_now", lambda: datetime(2026, 10, 2, 16))
    # Point scheduler sessions at THIS temporary DB; calendar says open but official holiday wins.
    db.add(TradeCalendarModel(trade_date=date(2026, 10, 2), is_trade_day=True, session_type="full"))
    await db.commit()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(module, "async_session", maker)
    service = module.DataScheduler()
    result = await service._collect_after_hours_research()
    assert result["status"] == "blocked"
    forbidden.assert_not_awaited()
    monkeypatch.setattr(adapter, "local_now", lambda: datetime(2026, 9, 30, 15, 4, 59))
    assert (await service._collect_after_hours_research())["status"] == "blocked"


@pytest.mark.asyncio
async def test_baseline_missing_quality_and_half_session_calendar(monkeypatch, db):
    from app.data import scheduler as module
    import app.data.sources.after_hours_source as adapter
    monkeypatch.setattr(adapter, "local_now", lambda: datetime(2026, 9, 30, 15, 1))
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    calendar = TradeCalendarModel(trade_date=DAY, is_trade_day=True, session_type="full")
    db.add(calendar)
    db.add(StockTag(code="600000", name="研究样本", board_type="main_sh", board_tag="tradeable", is_suspended=False))
    await db.commit()
    service = module.DataScheduler()
    result = await service._freeze_regular_close_research()
    assert result["status"] == "partial" and result["status_counts"] == {"partial": 1}
    calendar.session_type = "half_day"
    await db.commit()
    assert (await service._freeze_regular_close_research())["status"] == "blocked"
    monkeypatch.setattr(adapter, "local_now", lambda: datetime(2026, 9, 30, 16))
    assert (await service._collect_after_hours_research())["status"] == "blocked"


def test_unverified_legacy_volume_zero_is_unknown_not_measured_zero():
    at = datetime(2026, 9, 30, 15, 1)
    spot = SimpleNamespace(source_quote_at=at.replace(minute=0), received_at=at, price=10, volume=0)
    assert regular_close_material(spot, day=DAY, received_at=at)["regular_volume_shares"] is None


@pytest.mark.asyncio
async def test_sql_replace_does_not_rewrite_first_clock_or_content(db):
    row = official_row()
    await append_observations(db, [row])
    await db.commit()
    stored = await db.scalar(select(StockAfterHoursObservation))
    values = {column.name: getattr(stored, column.name) for column in StockAfterHoursObservation.__table__.columns}
    await db.execute(text("PRAGMA recursive_triggers=OFF"))
    from sqlalchemy.dialects.sqlite import insert
    values["available_at"] = RECEIVED + timedelta(days=1)
    await db.execute(insert(StockAfterHoursObservation).values(values).prefix_with("OR REPLACE"))
    await db.commit()
    await db.refresh(stored)
    assert stored.available_at == RECEIVED
    # A different payload reusing an existing primary key must be rejected.
    values["payload_json"] = "{}"
    values["content_hash"] = "f" * 64
    with pytest.raises(Exception, match="append-only"):
        await db.execute(insert(StockAfterHoursObservation).values(values).prefix_with("OR REPLACE"))
    await db.rollback()
    # Same unique content but different row id/clock also preserves first evidence.
    values.update(id=999, payload_json=row.payload_json, content_hash=row.content_hash)
    await db.execute(insert(StockAfterHoursObservation).values(values).prefix_with("OR REPLACE"))
    await db.commit()
    assert len((await db.scalars(select(StockAfterHoursObservation))).all()) == 1
    assert (await read_after_hours(db, day=DAY, as_of=RECEIVED))["returned_codes"] == 1
    # Duplicate first member must not discard the second new member of a batch.
    assert (await append_observations(db, [official_row(), official_row("000001")]))["inserted"] == 1


@pytest.mark.asyncio
async def test_bad_latest_hash_is_reported_not_replaced_by_old_good(db):
    await append_observations(db, [official_row()])
    bad = official_row(received=RECEIVED + timedelta(minutes=1))
    bad.content_hash = "0" * 64
    await append_observations(db, [bad])
    await db.commit()
    result = await read_after_hours(db, day=DAY, as_of=RECEIVED + timedelta(minutes=1))
    assert result["stored_code_count"] == 1
    assert result["items"][0]["after_volume_shares"] is None
    assert result["items"][0]["observation_missing"]["after_hours"] == ["latest_observation_integrity_unavailable"]


@pytest.fixture
def collector_http_no_network(monkeypatch):
    """Ignore host proxy configuration and forbid any unexpected transport call."""
    real_client = httpx.AsyncClient
    async def forbidden(request):
        raise AssertionError("collector fixture must never make network requests")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, trust_env=False, transport=httpx.MockTransport(forbidden)))


@pytest.mark.asyncio
async def test_scheduler_completed_budget_date_and_missing_records(monkeypatch, db, collector_http_no_network):
    from app.data import scheduler as module
    import app.data.sources.after_hours_source as adapter
    now = datetime(2026, 9, 30, 16, 30)
    monkeypatch.setattr(adapter, "local_now", lambda: now)
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True, session_type="full"))
    for code in ("600000", "000001"):
        db.add(StockTag(code=code, name="研究样本", board_type="main_sh" if code.startswith("6") else "main_sz", board_tag="tradeable", is_suspended=False))
    await db.commit()
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    observed = []
    async def fixture(self, code, **kwargs):
        observed.append(code)
        if code == "000001":
            raise TimeoutError("fixture unavailable")
        return parse_official(sse(), code=code, day=DAY, received_at=now)
    monkeypatch.setattr(adapter.AfterHoursSource, "collect", fixture)
    service = module.DataScheduler()
    result = await service._collect_after_hours_research()
    assert result["selected_codes"] == 2 and result["status_counts"] == {"partial": 1, "observed": 1}
    assert set(observed) == {"600000", "000001"}
    result = await read_after_hours(db, day=DAY, as_of=now)
    assert result["stored_code_count"] == 2
    assert result["items"][0]["after_volume_shares"] is None


@pytest.mark.asyncio
async def test_scheduler_source_request_uses_total_deadline(monkeypatch, db, collector_http_no_network):
    from app.data import scheduler as module
    import app.data.sources.after_hours_source as adapter
    now = datetime(2026, 9, 30, 16, 30)
    monkeypatch.setattr(adapter, "local_now", lambda: now)
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True, session_type="full"))
    db.add(StockTag(code="600000", name="研究样本", board_type="main_sh", board_tag="tradeable", is_suspended=False))
    await db.commit()
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    times = iter([0, 7.99, 7.995, 8.001, 8.001])
    monkeypatch.setattr(module, "_time", SimpleNamespace(monotonic=lambda: next(times, 8.001)))
    monkeypatch.setattr(module.settings, "AFTER_HOURS_RESEARCH_BUDGET_SEC", 8)
    async def slow(self, code, **kwargs):
        await __import__("asyncio").sleep(0.1)
        raise AssertionError("must be canceled before this")
    monkeypatch.setattr(adapter.AfterHoursSource, "collect", slow)
    result = await module.DataScheduler()._collect_after_hours_research()
    assert result["status_counts"] == {"partial": 1}
    item = (await read_after_hours(db, day=DAY, as_of=now))["items"][0]
    assert item["after_volume_shares"] is None


def test_migration_038_isolated_sqlite_guards_and_lossy_downgrade():
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / "alembic/versions/038_after_hours_research.py"
    spec = importlib.util.spec_from_file_location("after_hours_038_fixture", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            migration.op = Operations(MigrationContext.configure(conn))
            migration.upgrade()
            row = official_row()
            values = {column.name: getattr(row, column.name) for column in StockAfterHoursObservation.__table__.columns if column.name != "id"}
            conn.execute(StockAfterHoursObservation.__table__.insert().values(values))
            with pytest.raises(RuntimeError, match="lossy downgrade"):
                migration.downgrade()
            with pytest.raises(Exception, match="append-only"):
                conn.execute(text("UPDATE stock_after_hours_observation SET quality_status='fake'"))
    finally:
        engine.dispose()
