"""Projection-only optimization must retain every audit row and fail-closed finding."""
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.db.session import Base
from app.core.prediction_data_quality import PredictionDataQualityAuditor
from app.models.signal import PromotionPredictionRecord
from app.models.stock import StockKline


@pytest_asyncio.fixture
async def audited_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    queries = []
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        yield db, queries
    await engine.dispose()


def record(code, **kwargs):
    values = dict(code=code, target_board=1, prediction_trade_date=date(2026, 9, 18),
                  snapshot_context="promotion_1305",
                  snapshot_recorded_at=datetime(2026, 9, 18, 13, 5),
                  factors_json='{"irrelevant":"' + "x" * 65536 + '"}',
                  reason_snapshot="y" * 65536)
    values.update(kwargs)
    return PromotionPredictionRecord(**values)


def assert_narrow(queries, required):
    statements = [q for q in queries if "FROM promotion_prediction_record" in q]
    assert len(statements) == 1  # no per-row lazy loads / missing-field fallback
    selected = statements[0].split("FROM")[0]
    assert "reason_snapshot" not in selected
    assert "factors_json" not in selected
    assert all(name in selected for name in required)


@pytest.mark.asyncio
async def test_outcome_projection_retains_all_invalid_rows_and_twenty_examples(audited_db):
    db, queries = audited_db
    for day in (date(2026, 9, 18), date(2026, 9, 21), date(2026, 9, 22)):
        db.add(StockKline(code="000001", trade_date=day, close=10))
    for i in range(25):
        db.add(record(f"{i:06d}", outcome_status="failed",
                      outcome_trade_date=date(2026, 9, 22), horizon_days=1))
    db.add(record("001000", outcome_status="success", outcome_trade_date=date(2026, 9, 21)))
    db.add(record("001001", outcome_status="failed", horizon_days=2, outcome_trade_date=date(2026, 9, 22)))
    db.add(record("001002", outcome_status="pending", outcome_trade_date=date(2026, 9, 22)))
    db.add(record("001003", outcome_status="failed", horizon_days=20, outcome_trade_date=date(2026, 9, 22)))
    db.add(record("001004", outcome_status="failed", outcome_trade_date=None))
    await db.commit()
    queries.clear()
    found = await PredictionDataQualityAuditor()._outcome_date_findings(db, date(2026,9,21), lookback_days=20)
    assert len(found) == 1 and found[0].blocking
    assert found[0].evidence["invalid_count"] == 25
    assert len(found[0].evidence["examples"]) == 20
    assert all(x["expected_outcome_date"] == date(2026,9,21) for x in found[0].evidence["examples"])
    assert_narrow(queries, ["id", "code", "prediction_trade_date", "horizon_days", "outcome_trade_date"])


@pytest.mark.asyncio
@pytest.mark.parametrize("context,lower,upper", [
    ("promotion_1510", "15:05:00", "16:30:00"),
    ("promotion_2000", "19:50:00", "21:30:00"),
    ("promotion_0925", "09:20:00", "09:40:00"),
    ("promotion_0935", "09:30:00", "09:50:00"),
    ("promotion_1000", "09:55:00", "10:15:00"),
    ("promotion_1030", "10:25:00", "10:45:00"),
    ("promotion_1305", "13:00:00", "13:20:00"),
    ("promotion_1400", "13:55:00", "14:15:00"),
    ("promotion_1430", "14:25:00", "14:45:00"),
])
async def test_cutoff_boundaries_unchanged_without_loading_blobs(audited_db, context, lower, upper):
    db, queries = audited_db
    lo = datetime.fromisoformat("2026-09-18T" + lower)
    hi = datetime.fromisoformat("2026-09-18T" + upper)
    for i, clock in enumerate((lo-timedelta(microseconds=1), lo, hi, hi+timedelta(microseconds=1))):
        db.add(record(f"{i:06d}", snapshot_context=context, snapshot_recorded_at=clock))
    db.add(record("001000", snapshot_context=context, snapshot_recorded_at=None))
    db.add(record("001001", snapshot_context="unknown", snapshot_recorded_at=lo))
    await db.commit()
    queries.clear()
    found = await PredictionDataQualityAuditor()._snapshot_cutoff_findings(db, date(2026,9,21), lookback_days=20)
    assert len(found) == 1 and not found[0].blocking
    assert found[0].evidence["invalid_count"] == 2
    assert {x["recorded_at"] for x in found[0].evidence["examples"]} == {lo-timedelta(microseconds=1), hi+timedelta(microseconds=1)}
    assert_narrow(queries, ["id", "snapshot_context", "snapshot_recorded_at"])


@pytest.mark.asyncio
async def test_empty_audit_queries_remain_empty(audited_db):
    db, _ = audited_db
    auditor = PredictionDataQualityAuditor()
    assert await auditor._outcome_date_findings(db, date(2026,9,21), lookback_days=20) == []
    assert await auditor._snapshot_cutoff_findings(db, date(2026,9,21), lookback_days=20) == []
