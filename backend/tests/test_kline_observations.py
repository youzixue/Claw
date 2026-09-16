from datetime import date, datetime, timedelta
import json

import pytest
import pytest_asyncio
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.stock import StockKline, StockKlineObservation
from app.data.kline_observations import persist_kline_observations, kline_quality_issues

NOW = datetime(2026, 9, 14, 18, 0)


def bar(**changes):
    return dict(code="000001", trade_date=NOW.date(), open=10.0, high=11.0,
                low=9.5, close=10.5, volume=10000, amount=100000.0,
                turnover=1.0, change_pct=5.0, prev_close=10.0, source="ths", **changes)


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'observations.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_historical_correction_and_missing_gap_never_rewrite_projection(sessions):
    old = bar()
    old.update(trade_date=NOW.date() - timedelta(days=3), close=10.2)
    candidate = {**old, "close": 10.5}
    missing = {**candidate, "code": "000002"}
    async with sessions() as db:
        db.add(StockKline(**old))
        await db.commit()
        before = (await db.execute(text("SELECT * FROM stock_kline"))).all()
        result = await persist_kline_observations(db, [candidate, missing], projection_day=NOW.date(), now=NOW)
        await db.commit()
        assert (await db.execute(text("SELECT * FROM stock_kline"))).all() == before
        assert result["written"] == 0
        assert result["dispositions"] == {"isolated_historical": 2}
        rows = (await db.scalars(select(StockKlineObservation))).all()
        assert len(rows) == 3
        assert all(row.available_at is None and row.recorded_at == NOW for row in rows)
        original = next(row for row in rows if row.origin == "legacy_projection")
        assert json.loads(original.payload_json)["close"] == 10.2
        assert original.source_version == "legacy_unknown"


@pytest.mark.asyncio
async def test_current_close_archives_prior_and_retry_is_content_idempotent(sessions):
    old = bar()
    candidate = {**old, "source": "tencent_close", "close": 10.8}
    async with sessions() as db:
        db.add(StockKline(**old))
        await db.commit()
        result = await persist_kline_observations(db, [candidate], projection_day=NOW.date(), now=NOW)
        await db.commit()
        assert result["written"] == 1
        projected = await db.scalar(select(StockKline))
        assert projected.close == 10.8 and projected.source == "tencent_close"
        assert await db.scalar(select(func.count()).select_from(StockKlineObservation)) == 2
        result = await persist_kline_observations(db, [candidate], projection_day=NOW.date(), now=NOW + timedelta(minutes=5))
        await db.commit()
        assert result["observations_appended"] == 0
        assert await db.scalar(select(func.count()).select_from(StockKlineObservation)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("incoming", ["ths", "spot_fallback"])
async def test_current_tencent_close_cannot_be_replaced_by_lower_source(sessions, incoming):
    old = bar()
    old["source"] = "tencent_close"
    async with sessions() as db:
        db.add(StockKline(**old))
        await db.commit()
        result = await persist_kline_observations(db, [{**old, "source": incoming, "close": 10.6}], projection_day=NOW.date(), now=NOW)
        await db.commit()
        assert result["written"] == 0
        assert result["dispositions"] == {"protected_source": 1}
        assert (await db.scalar(select(StockKline))).close == 10.5


@pytest.mark.parametrize(("field", "value"), [
    ("open", float("nan")), ("close", float("inf")), ("high", -1), ("low", 12),
    ("volume", float("inf")), ("volume", -1), ("volume", 1.5), ("volume", 2**64),
    ("amount", float("-inf")), ("turnover", -1), ("prev_close", 0), ("change_pct", True),
])
@pytest.mark.asyncio
async def test_invalid_values_are_archived_not_projected(sessions, field, value):
    row = {**bar(), field: value}
    async with sessions() as db:
        result = await persist_kline_observations(db, [row], projection_day=NOW.date(), now=NOW)
        await db.commit()
        assert result["status"] == "degraded" and result["written"] == 0
        assert await db.scalar(select(func.count()).select_from(StockKline)) == 0
        captured = await db.scalar(select(StockKlineObservation))
        assert json.loads(captured.quality_issues_json)
        json.loads(captured.payload_json, parse_constant=lambda x: pytest.fail(x))


def test_missing_previous_close_and_real_zero_volume_remain_distinct():
    row = {**bar(), "volume": 0, "prev_close": None, "change_pct": None}
    assert kline_quality_issues(row) == []


@pytest.mark.asyncio
async def test_future_observation_does_not_become_market_history(sessions):
    row = {**bar(), "trade_date": NOW.date() + timedelta(days=1)}
    async with sessions() as db:
        result = await persist_kline_observations(db, [row], projection_day=NOW.date(), now=NOW)
        await db.commit()
        assert result["dispositions"] == {"isolated_future": 1}
        assert result["written"] == 0 and result["historical_pit_eligible"] is False


@pytest.mark.asyncio
async def test_transaction_rollback_keeps_old_projection_and_no_partial_evidence(sessions):
    old = bar()
    async with sessions() as db:
        db.add(StockKline(**old))
        await db.commit()
        await persist_kline_observations(db, [{**old, "source": "tencent_close"}], projection_day=NOW.date(), now=NOW)
        await db.rollback()
        assert (await db.scalar(select(StockKline))).source == "ths"
        assert await db.scalar(select(func.count()).select_from(StockKlineObservation)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "UPDATE stock_kline_observation SET source_version='fake'",
    "DELETE FROM stock_kline_observation",
])
async def test_sql_mutation_of_evidence_is_rejected(sessions, mutation):
    async with sessions() as db:
        await persist_kline_observations(db, [bar()], projection_day=NOW.date(), now=NOW)
        await db.commit()
        with pytest.raises(Exception, match="append-only"):
            await db.execute(text(mutation))
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(StockKlineObservation)) == 1


@pytest.mark.asyncio
async def test_generic_scheduler_upsert_cannot_bypass_historical_contract(sessions):
    from app.data.scheduler import DataScheduler
    async with sessions() as db:
        with pytest.raises(ValueError, match="append-only"):
            await DataScheduler._batch_upsert(db, StockKline, [bar()], ["code", "trade_date"])


@pytest.mark.asyncio
async def test_projection_requires_caller_calendar_confirmation(sessions):
    async with sessions() as db:
        result = await persist_kline_observations(db, [bar()], now=NOW)
        await db.commit()
        assert result["dispositions"] == {"isolated_unconfirmed_day": 1}
        assert result["written"] == 0


@pytest.mark.asyncio
async def test_current_projection_is_not_granted_by_yesterdays_calendar(sessions):
    async with sessions() as db:
        result = await persist_kline_observations(
            db, [bar()], now=NOW, projection_day=NOW.date() - timedelta(days=1))
        await db.commit()
        assert result["written"] == 0


@pytest.mark.asyncio
async def test_evidence_insert_failure_cannot_change_projection(sessions):
    old = bar()
    async with sessions() as db:
        db.add(StockKline(**old))
        await db.execute(text("""CREATE TRIGGER no_capture BEFORE INSERT ON stock_kline_observation
            BEGIN SELECT RAISE(ABORT, 'capture unavailable'); END"""))
        await db.commit()
        with pytest.raises(Exception, match="capture unavailable"):
            await persist_kline_observations(db, [{**old, "source": "tencent_close", "close": 10.8}],
                                            now=NOW, projection_day=NOW.date())
        await db.rollback()
        assert (await db.scalar(select(StockKline))).close == 10.5


def test_legacy_derived_repair_apply_is_rejected_before_database_access(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    # No tables needed: apply must be rejected before opening/querying this file.
    path = tmp_path / "fixture.db"
    path.write_bytes(b"not a sqlite database")
    process = subprocess.run([sys.executable, "-B",
        str(Path(__file__).resolve().parents[1] / "scripts/repair_stock_kline_derived.py"),
        "--database", str(path), "--apply"], capture_output=True, text=True)
    assert process.returncode != 0 and "历史派生字段覆盖已禁用" in process.stderr
    assert path.read_bytes() == b"not a sqlite database"
