"""Learning reads only used outcome evidence, never changes probabilities or gates."""
from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.api.v1 import promotion as p
from app.db.session import Base
from app.models.governance import TradeCalendarModel
from app.models.signal import PromotionPredictionRecord
from app.models.stock import StockKline

DAY = date(2026, 8, 13)
AFTER = date(2026, 8, 14)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'learning.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def seed(db, *, source="schedule", bad=None):
    db.add_all([TradeCalendarModel(trade_date=day, is_trade_day=True) for day in (DAY, AFTER)])
    for i in range(4):
        code = f"60000{i+1}"
        db.add(PromotionPredictionRecord(
            code=code, target_board=1, prediction_trade_date=DAY,
            predicted_probability=.5, calibrated_probability=.5,
            outcome_status="success" if i == 0 else "failed",
            candidate_route="mainline_spread_start", learning_bucket="T1:mainline_spread_start",
            model_version=p.PROMOTION_MODEL_VERSION,
            snapshot_source=source, snapshot_context="promotion_2000",
            snapshot_recorded_at=datetime(2026, 8, 13, 20),
            factors_json=p._json_dumps_safe({
                "promotion_event_label_version": p.PROMOTION_LABEL_VERSION,
                "prediction_snapshot_source": source,
                "prediction_snapshot_context": "promotion_2000",
                "prediction_ranked_selected": True,
            })))
        db.add(StockKline(code=code, trade_date=DAY, close=10, volume=100, source="ths"))
        if bad != "missing":
            db.add(StockKline(code=code, trade_date=AFTER, close=11,
                prev_close=9 if bad == "chain" else 10,
                volume=0 if bad == "volume" else 100,
                change_pct=None if bad == "change" else 10,
                source="spot_fallback" if bad == "source" else "ths"))
    await db.commit()


@pytest.mark.asyncio
async def test_outcome_query_projects_contract_columns_without_orm_hydration(db):
    await seed(db)
    statements = []
    def observe(conn, cursor, statement, params, context, many):
        if "FROM stock_kline" in statement:
            statements.append(statement)
    event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
    try:
        stats = await p._load_promotion_learning_stats(db)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
    assert stats["T1:mainline_spread_start"]["directional_sample_count"] == 4
    assert statements
    for statement in statements:
        projection = statement.split("FROM")[0]
        assert "stock_kline.id" not in projection, "unused ORM columns inflate each formal build"
        assert "stock_kline.amount" not in projection
        for column in ("code", "trade_date", "close", "prev_close", "volume", "source", "change_pct"):
            assert f"stock_kline.{column}" in projection


@pytest.mark.asyncio
async def test_page_only_history_does_not_load_any_direction_bars(db):
    await seed(db, source="page")
    statements = []
    def observe(conn, cursor, statement, params, context, many):
        if "FROM stock_kline" in statement:
            statements.append(statement)
    event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
    try:
        stats = await p._load_promotion_learning_stats(db)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
    assert stats["T1:mainline_spread_start"]["directional_sample_count"] == 0
    assert statements == [], "no eligible outcome pairs must cause zero Kline reads"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "missing", "source", "volume", "chain", "change"])
async def test_direction_evidence_gate_and_denominator_unchanged(db, bad):
    await seed(db, bad=bad)
    stats = await p._load_promotion_learning_stats(db)
    row = stats["T1:mainline_spread_start"]
    valid = 4 if bad is None else 0
    assert row["sample_count"] == 4
    assert row["success_count"] == 1
    assert row["directional_sample_count"] == valid
    assert row["directional_unknown_count"] == 4-valid
    assert row["pool_directional_sample_count"] == valid
