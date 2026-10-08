"""Isolated integration checks for prospective pool projections; no network."""
import asyncio
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.stock import LimitUpPool, LimitDownPool, BrokenLimitPool, QuoteRound
from app.data.limit_pool import persist_tencent_limit_state, supplement_wencai_limit_details, limit_pool_health
from app.data.quote_round import build_quote_round_record

DAY = date(2026, 9, 28)
NOW = datetime(2026, 9, 28, 10, 0)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pools.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def spot(code="000001", *, price=11.0, high=11.0, source_at=NOW, **overrides):
    return dict(code=code, name="测试", price=price, high=high, low=10.0 if price >= 10 else price,
                prev_close=10.0, limit_up=11.0, limit_down=9.0, volume=100,
                turnover=3.0, source_quote_at=source_at, received_at=source_at, **overrides)


async def save(db, records, at=NOW, expected=None):
    record = build_quote_round_record(records, expected_count=expected or len(records), committed_at=at)
    state = await persist_tencent_limit_state(db, records, observed_at=at, expected_count=expected or len(records))
    evidence = json.loads(record["component_watermarks_json"])
    evidence["limit_pool"] = state
    record["component_watermarks_json"] = json.dumps(evidence)
    db.add(QuoteRound(**record))
    await db.commit()
    db.expire_all()
    return state


@pytest.mark.asyncio
async def test_states_are_mutually_exclusive_and_unknown_is_null(db):
    await save(db, [spot(), spot("000002", price=10.8), spot("000003", price=9, high=10)])
    up = await db.scalar(select(LimitUpPool))
    assert up.code == "000001"
    assert (up.break_count, up.consecutive_days, up.limit_up_time, up.seal_amount) == (None,) * 4
    assert up.source == "tencent"
    assert await db.scalar(select(BrokenLimitPool.code)) == "000002"
    assert await db.scalar(select(LimitDownPool.code)) == "000003"
    health = await limit_pool_health(db, trade_date=DAY, decision_at=NOW)
    assert health["detail_unknown_count"] == 1
    assert health["status"] == "blocked"


@pytest.mark.asyncio
async def test_partial_empty_and_regressed_frames_do_not_clear_missing_stocks(db):
    await save(db, [spot(), spot("000002")])
    await save(db, [spot(price=10.8, source_at=NOW + timedelta(seconds=30))],
               at=NOW + timedelta(seconds=30), expected=2)
    assert list(await db.scalars(select(LimitUpPool.code))) == ["000002"]
    assert await db.scalar(select(BrokenLimitPool.code)) == "000001"
    await save(db, [spot(source_at=NOW)], at=NOW + timedelta(seconds=40), expected=2)
    assert await db.scalar(select(BrokenLimitPool.code)) == "000001"
    await save(db, [], at=NOW + timedelta(seconds=50), expected=2)
    assert await db.scalar(select(LimitUpPool.code)) == "000002"
    health = await limit_pool_health(db, trade_date=DAY, decision_at=NOW + timedelta(seconds=50))
    assert health["status"] == "blocked" and health["coverage"] == 0


DETAIL = dict(consecutive_days=1, break_count=0, limit_up_time="09:35:00",
              seal_amount=80_000_000, limit_up_reason="电网")


@pytest.mark.asyncio
async def test_wencai_enriches_only_verified_tencent_states(db):
    await save(db, [spot()])
    assert await supplement_wencai_limit_details(
        db, {"000001": DETAIL, "000002": DETAIL},
        requested_at=NOW, observed_at=NOW + timedelta(seconds=10),
    ) == 1
    await db.commit()
    health = await limit_pool_health(db, trade_date=DAY, decision_at=NOW + timedelta(seconds=10))
    assert health["ready"] is True
    assert list(await db.scalars(select(LimitUpPool.code))) == ["000001"]
    row = await db.scalar(select(LimitUpPool))
    assert row.break_count == 0 and row.consecutive_days == 1
    assert json.loads(row.evidence_json)["wencai"]["provider_timestamp"] is None
    # A subsequent quote preserves the bounded, independently clocked details.
    await save(db, [spot(source_at=NOW + timedelta(seconds=30))], at=NOW + timedelta(seconds=30))
    row = await db.scalar(select(LimitUpPool))
    assert row.break_count == 0
    # Once stale they are NULL again, never the ORM 0/1 defaults.
    late = NOW + timedelta(minutes=6)
    await save(db, [spot(source_at=late)], at=late)
    row = await db.scalar(select(LimitUpPool))
    assert row.break_count is None and row.consecutive_days is None


@pytest.mark.asyncio
async def test_inflight_metadata_cannot_overwrite_newer_state_or_historical_date(db):
    await save(db, [spot()])
    assert await supplement_wencai_limit_details(
        db, {"000001": DETAIL}, requested_at=NOW - timedelta(seconds=1), observed_at=NOW,
    ) == 0
    with pytest.raises(ValueError):
        await supplement_wencai_limit_details(
            db, {"000001": DETAIL}, requested_at=NOW, observed_at=NOW + timedelta(days=1),
        )


@pytest.mark.asyncio
async def test_close_needs_real_close_clock_and_close_metadata(db):
    last = NOW.replace(hour=14, minute=59)
    close = NOW.replace(hour=15, minute=0)
    await save(db, [spot(source_at=last)], at=last)
    health = await limit_pool_health(db, trade_date=DAY, decision_at=close, require_close=True)
    assert not health["ready"]
    await save(db, [spot(source_at=close)], at=close)
    await supplement_wencai_limit_details(db, {"000001": DETAIL},
                                         requested_at=close, observed_at=close + timedelta(seconds=10))
    await db.commit()
    health = await limit_pool_health(db, trade_date=DAY, decision_at=close.replace(hour=20), require_close=True)
    assert health["ready"]


@pytest.mark.asyncio
async def test_complete_zero_market_is_distinct_from_empty_response(db):
    await save(db, [spot(price=10.5, high=10.6)])
    health = await limit_pool_health(db, trade_date=DAY, decision_at=NOW)
    assert health["ready"] and health["up_count"] == 0
    health = await limit_pool_health(db, trade_date=DAY, decision_at=NOW + timedelta(minutes=10))
    assert not health["ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("incoming_clock", [NOW - timedelta(seconds=1), None])
async def test_quote_watermark_survives_normal_state_and_rejects_resurrection(db, monkeypatch, incoming_clock):
    from unittest.mock import AsyncMock
    from app.data import scheduler as module
    from app.models.stock import StockSpot
    db.add(StockSpot(code="000001", price=10.5, source_quote_at=NOW, updated_at=NOW))
    await db.commit()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 28, 10, 0, 30)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda *args: "morning")
    scheduler = module.DataScheduler()
    scheduler._tradeable_codes = ["000001"]
    scheduler._sources["tencent"] = type("FakeTencent", (), {
        "collect_spot_batch": AsyncMock(return_value=[spot(source_at=incoming_clock)])
    })()
    monkeypatch.setattr(scheduler, "_schedule_quote_round_archive", lambda payload: None)
    monkeypatch.setattr(scheduler, "_publish_quote_round", lambda payload: None)
    monkeypatch.setattr(scheduler, "_request_anomaly_scan", lambda reason: None)
    monkeypatch.setattr("app.signal.anomaly_scanner.anomaly_scanner.enqueue_quote_batch", lambda records: None)
    result = await scheduler._tencent_spot_collect()
    assert result["status"] == "degraded" and result["collected"] == 0
    db.expire_all()
    persisted = await db.get(StockSpot, "000001")
    assert persisted.price == 10.5 and persisted.source_quote_at == NOW
    assert await db.scalar(select(LimitUpPool.id)) is None


@pytest.mark.asyncio
async def test_board_height_reports_unknown_not_zero_when_source_incomplete(db, monkeypatch):
    from unittest.mock import AsyncMock
    from app.api.v1 import promotion
    from app.core.trade_calendar import trade_calendar
    monkeypatch.setattr(trade_calendar, "is_trade_day", AsyncMock(side_effect=lambda day: day == DAY))
    await save(db, [spot()])
    result = await promotion.board_height(db)
    assert result["height"] is None and result["limit_up_count"] is None
    assert result["source_health"]["ready"] is False


@pytest.mark.asyncio
async def test_new_unknown_state_not_first_board_or_memory_bonus(db):
    from app.api.v1 import promotion
    from app.models.stock import StockTag
    db.add(StockTag(code="000001", name="测试", board_type="main", board_tag="tradeable",
                    is_st=False, is_suspended=False, is_delisting=False))
    await db.commit()
    await save(db, [spot()])
    assert await promotion._load_filtered_limit_ups(db, DAY) == []
    memory = await promotion._load_recent_limit_up_memory(db, ["000001"], DAY)
    assert memory["000001"]["memory_score"] == 0


@pytest.mark.asyncio
async def test_scheduler_wencai_singleflight_timeout_does_not_spawn_more_requests(monkeypatch):
    from app.data import scheduler as module
    from unittest.mock import AsyncMock, MagicMock
    scheduler = module.DataScheduler()
    event = asyncio.Event()
    started = []
    async def query(*args, **kwargs):
        started.append(True)
        await event.wait()
        return pd.DataFrame()
    monkeypatch.setattr(module.WencaiStreamSource, "query_async", query)
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(module.settings, "LIMIT_POOL_WENCAI_WAIT_SEC", .01)
    session = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "async_session", lambda: context)
    monkeypatch.setattr(module.data_quality_guard, "record_failure", AsyncMock())
    try:
        assert (await scheduler._intraday_fast(force=True))["status"] == "failed"
        assert (await scheduler._intraday_fast(force=True))["status"] == "failed"
        assert len(started) == 1
    finally:
        event.set()
        await scheduler._limit_detail_task


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_unknown_board_identity_stays_pending_not_first_board_label(db, bulk):
    from app.api.v1 import promotion
    from app.models.signal import PromotionPredictionRecord
    from app.models.stock import StockKline
    await save(db, [spot()])
    db.add(StockKline(code="000001", trade_date=DAY, open=10, high=11, low=10,
                      close=11, prev_close=10, change_pct=10, source="ths"))
    prediction = PromotionPredictionRecord(code="000001", target_board=1,
        prediction_trade_date=date(2026, 9, 25), horizon_days=1, outcome_status="pending")
    db.add(prediction)
    await db.commit()
    if bulk:
        changed = await promotion._evaluate_promotion_prediction_records_bulk(
            db, [prediction], now=NOW.replace(hour=20))
    else:
        changed = await promotion._evaluate_promotion_prediction_record(
            db, prediction, now=NOW.replace(hour=20))
    assert not changed
    assert prediction.outcome_status == "pending"


@pytest.mark.asyncio
async def test_future_first_touch_is_not_certified(db):
    await save(db, [spot()])
    await supplement_wencai_limit_details(
        db, {"000001": {**DETAIL, "limit_up_time": "14:00:00"}},
        requested_at=NOW, observed_at=NOW + timedelta(seconds=10),
    )
    await db.commit()
    assert (await db.scalar(select(LimitUpPool))).limit_up_time is None


def test_migration_adds_empty_evidence_and_preserves_legacy_rows():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, text
    path = Path(__file__).parents[1] / "alembic/versions/037_limit_pool_source_evidence.py"
    spec = importlib.util.spec_from_file_location("pool_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        for table in migration.TABLES:
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, code TEXT, break_count INTEGER)"))
            connection.execute(text(f"INSERT INTO {table} VALUES (1, '000001', 0)"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()  # SQLite compatibility columns may already exist.
        for table in migration.TABLES:
            row = connection.execute(text(f"SELECT * FROM {table}")).mappings().one()
            assert row["code"] == "000001" and row["break_count"] == 0
            assert row["source_version"] is None and row["source_quote_at"] is None
            assert row["observed_at"] is None and row["evidence_json"] is None
        migration.downgrade()
    engine.dispose()


@pytest.mark.asyncio
async def test_no_eastmoney_live_lhb_or_scheduler_source():
    from app.api.v1.promotion import _load_dragon_tiger_context_map
    from app.data.scheduler import DataScheduler
    assert await _load_dragon_tiger_context_map(DAY, ["000001"]) == {}
    assert "eastmoney" not in DataScheduler()._sources
