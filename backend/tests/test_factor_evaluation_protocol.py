"""真实收益IC、标签成熟度、旧口径隔离及GET无写副作用。"""
import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.session import Base
from app.factors.evaluator import (
    FactorEvaluationService, ICAnalyzer, FactorDecayDetector, IC_PROTOCOL, evaluate_ic_material,
)
from app.models.factor import FactorValue, FactorEvaluation, FactorEvaluationRun
from app.models.stock import StockKline
from app.models.governance import TradeCalendarModel

DAY = date(2026, 9, 7)
CUTOFF = datetime(2026, 9, 9, 16)


@pytest_asyncio.fixture
async def factor_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'factor.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        yield db
    await engine.dispose()


async def seed(db, name="ma5_bias"):
    for offset in range(3):
        day = DAY + timedelta(days=offset)
        db.add(TradeCalendarModel(trade_date=day, is_trade_day=True))
        for i in range(12):
            code = f"600{i:03d}"
            daily_ratio = 1 - (i + 1) * .005
            close = 10 * daily_ratio ** offset
            db.add(FactorValue(stock_code=code, trade_date=day, factor_name=name,
                               factor_value=i, factor_rank=i + 1, factor_pct=i / 11))
            db.add(StockKline(code=code, trade_date=day, close=close,
                             prev_close=10 if offset == 0 else 10 * daily_ratio ** (offset - 1),
                             volume=1000, source="ths"))
    db.add(FactorEvaluation(factor_name=name, eval_date=CUTOFF.date(),
                           ic_mean=.42, ic_std=.2, ir=2.1, win_rate=.9,
                           is_decaying=True, decay_days=8))
    await db.commit()


@pytest.mark.asyncio
async def test_actual_returns_have_opposite_sign_to_rank_autocorrelation(factor_db):
    await seed(factor_db)
    result = await FactorEvaluationService().evaluate_factor(
        factor_db, "ma5_bias", as_of_at=CUTOFF)
    assert result["ic_summary"]["ic_mean"] == -1
    assert result["ic_summary"]["sample_count"] == 2
    assert result["ic_summary"]["ir"] is None
    assert result["paired_count"] == 24 and result["excluded_count"] == 12
    assert result["protocol_version"] == IC_PROTOCOL
    assert result["promotion_eligible"] is result["automatic_weight_update"] is False
    assert result["point_in_time_verified"] is False
    assert result["decay"]["is_decaying"] is None
    legacy = await factor_db.scalar(sa.select(FactorEvaluation))
    assert legacy.ic_mean == .42 and legacy.ir == 2.1
    json.dumps(result, allow_nan=False)


@pytest.mark.asyncio
async def test_reverse_direction_does_not_reverse_raw_ic(factor_db):
    await seed(factor_db, "rsi_14")
    result = await FactorEvaluationService().evaluate_factor(
        factor_db, "rsi_14", as_of_at=CUTOFF, persist=False)
    assert result["ic_summary"]["ic_mean"] == -1
    assert result["direction_adjusted_ic_mean"] == 1


@pytest.mark.asyncio
async def test_intraday_query_excludes_todays_final_bars_and_factor_rows(factor_db):
    await seed(factor_db)
    result = await FactorEvaluationService().evaluate_factor(
        factor_db, "ma5_bias", as_of_at=datetime(2026, 9, 9, 11, 30))
    assert result["through_date"] == "2026-09-08"
    assert result["paired_count"] == 12 and result["input_count"] == 24
    run = await factor_db.get(FactorEvaluationRun, result["run_id"])
    material = json.loads(run.input_json)
    assert all(row["trade_date"] <= "2026-09-08" for row in material["bars"] + material["factors"])
    assert result["ic_summary"]["ic_std"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,reason", [
    ("source", "spot_to_kline", "candidate_outcome_source_unverified"),
    ("volume", 0, "candidate_outcome_suspended_or_invalid"),
    ("close", None, "candidate_outcome_suspended_or_invalid"),
    ("prev_close", 9, "candidate_outcome_price_chain_discontinuity"),
])
async def test_invalid_formal_bar_excluded_not_filled(field, value, reason, factor_db):
    await seed(factor_db)
    bars = (await factor_db.scalars(sa.select(StockKline).where(
        StockKline.trade_date == DAY + timedelta(days=1)))).all()
    for bar in bars:
        setattr(bar, field, value)
    await factor_db.commit()
    result = await FactorEvaluationService().evaluate_factor(factor_db, "ma5_bias", as_of_at=CUTOFF)
    assert result["exclusions"][reason] >= 12
    assert result["paired_count"] <= 12


@pytest.mark.asyncio
async def test_missing_calendar_cannot_jump_to_next_available_bar(factor_db):
    await seed(factor_db)
    await factor_db.execute(sa.delete(TradeCalendarModel).where(
        TradeCalendarModel.trade_date == DAY + timedelta(days=1)))
    await factor_db.commit()
    result = await FactorEvaluationService().evaluate_factor(factor_db, "ma5_bias", as_of_at=CUTOFF)
    assert result["paired_count"] == 0
    assert result["exclusions"]["outcome_calendar_gap"] == 12
    assert result["ic_summary"] == {}


@pytest.mark.asyncio
async def test_append_only_idempotence_and_frozen_material_replay(factor_db):
    await seed(factor_db)
    service = FactorEvaluationService()
    first = await service.evaluate_factor(factor_db, "ma5_bias", as_of_at=CUTOFF)
    same = await service.evaluate_factor(factor_db, "ma5_bias", as_of_at=CUTOFF)
    assert same["run_id"] == first["run_id"]
    old = await factor_db.get(FactorEvaluationRun, first["run_id"])
    original_json = old.input_json
    # 测试隔离库模拟日K修订；旧运行仍只引用原冻结材料。
    bar = await factor_db.scalar(sa.select(StockKline).where(StockKline.trade_date == DAY))
    bar.volume = 0
    await factor_db.commit()
    newer = await service.evaluate_factor(factor_db, "ma5_bias", as_of_at=CUTOFF)
    assert newer["input_hash"] != first["input_hash"]
    assert newer["run_id"] != first["run_id"]
    assert old.input_json == original_json
    replayed = evaluate_ic_material(json.loads(original_json), ICAnalyzer(), FactorDecayDetector())
    assert replayed["ic_summary"] == first["ic_summary"]
    assert await factor_db.scalar(sa.select(sa.func.count()).select_from(FactorEvaluation)) == 1


@pytest.mark.asyncio
async def test_get_endpoints_never_evaluate_commit_or_relabel_legacy(factor_db, monkeypatch):
    from app.api.v1.factors import evaluate_factors, evaluate_single_factor
    from app.api.v1.performance import factor_eval
    from app.factors.evaluator import factor_evaluator
    await seed(factor_db)
    monkeypatch.setattr(factor_db, "commit", AsyncMock(side_effect=AssertionError("GET writes")))
    monkeypatch.setattr(factor_evaluator, "evaluate_factor", AsyncMock(side_effect=AssertionError("GET computes")))
    monkeypatch.setattr(factor_evaluator, "evaluate_all_factors", AsyncMock(side_effect=AssertionError("GET computes")))
    all_result = await evaluate_factors(eval_date="2026-09-09", db=factor_db)
    single = await evaluate_single_factor("ma5_bias", eval_date="2026-09-09", db=factor_db)
    assert all_result["results"]["ma5_bias"]["status"] == single["status"] == "legacy_not_ic"
    assert single["ic_summary"] == {}
    perf = await factor_eval(db=factor_db)
    assert perf["factors"][0]["ic_mean"] is None
    assert perf["factors"][0]["is_decaying"] is None
    assert await factor_db.scalar(sa.select(sa.func.count()).select_from(FactorEvaluationRun)) == 0


@pytest.mark.asyncio
async def test_legacy_factor_values_are_not_overwritten_by_recompute(factor_db):
    from app.risk.factor_scheduler import FactorStorageService
    await seed(factor_db)
    storage = FactorStorageService()
    result = await storage.save_factor_values(factor_db, DAY, {
        "600000": {"ma5_bias": {"value": 999, "rank": 12, "pct": 1}},
    })
    original = await factor_db.scalar(sa.select(FactorValue).where(
        FactorValue.stock_code == "600000", FactorValue.trade_date == DAY))
    assert result == 0
    assert original.factor_value == 0 and original.factor_rank == 1


@pytest.mark.asyncio
async def test_get_invalid_dates_return_422_without_writes(factor_db):
    from fastapi import HTTPException
    from app.api.v1.factors import evaluate_factors
    for requested in ("bad-date", "2099-01-01"):
        with pytest.raises(HTTPException) as error:
            await evaluate_factors(eval_date=requested, db=factor_db)
        assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_explicit_post_keeps_cutoff_and_never_calls_optimizer(factor_db, monkeypatch):
    from app.factors.base import FactorRegistry
    from app.factors.evaluator import factor_evaluator
    from app.api.v1.eval_scheduler import run_daily_evaluation
    await seed(factor_db)
    monkeypatch.setattr(FactorRegistry, "_factors", {"ma5_bias": FactorRegistry.get("ma5_bias")})
    def forbidden(*args, **kwargs):
        raise AssertionError("research must not optimize production weights")
    monkeypatch.setattr(factor_evaluator.weight_optimizer, "optimize_weights", forbidden)
    result = await run_daily_evaluation(
        trade_date="2026-09-09", as_of_at=datetime(2026, 9, 9, 11, 30), db=factor_db)
    assert result["as_of_at"] == "2026-09-09T11:30:00"
    assert result["evaluated_factors"] == 1
    assert result["automatic_weight_update"] is result["promotion_eligible"] is False
    saved = await factor_db.scalar(sa.select(FactorEvaluationRun))
    assert json.loads(saved.result_json)["through_date"] == "2026-09-08"


@pytest.mark.asyncio
async def test_empty_factor_results_do_not_count_as_successful_evaluations(factor_db, monkeypatch):
    from app.factors.base import FactorRegistry
    from app.risk.factor_scheduler import FactorEvaluationScheduler
    monkeypatch.setattr(FactorRegistry, "_factors", {"ma5_bias": FactorRegistry.get("ma5_bias")})
    result = await FactorEvaluationScheduler().run_daily_evaluation(factor_db, as_of_at=CUTOFF)
    assert result["total_factors"] == 1 and result["evaluated_factors"] == 0


@pytest.mark.asyncio
async def test_preview_is_read_only_and_empty_data_is_explicit(factor_db):
    result = await FactorEvaluationService().evaluate_factor(
        factor_db, "ma5_bias", as_of_at=CUTOFF, persist=False)
    assert result["status"] == "insufficient_data" and result["persisted"] is False
    assert result["ic_summary"] == {} and result["paired_count"] == 0
    assert await factor_db.scalar(sa.select(sa.func.count()).select_from(FactorEvaluationRun)) == 0


@pytest.mark.asyncio
async def test_future_cutoff_is_rejected(factor_db):
    with pytest.raises(ValueError, match="未来"):
        await FactorEvaluationService().evaluate_factor(
            factor_db, "ma5_bias", as_of_at=datetime.now() + timedelta(days=2))


@pytest.mark.parametrize("case", ["constant_x", "constant_y", "too_few", "different_assets", "duplicate_assets"])
def test_degenerate_cross_sections_do_not_create_fake_zero_ic(case):
    x = pd.Series(np.arange(12, dtype=float))
    y = x * 2
    if case == "constant_x": x[:] = 1
    if case == "constant_y": y[:] = 1
    if case == "too_few": x.iloc[8:] = np.inf
    if case == "different_assets": y.index = np.arange(12) + 1
    if case == "duplicate_assets": x.index = [0] * 12
    assert ICAnalyzer().calc_ic_series(x, y).empty


def test_real_ic_accepts_ties_and_keeps_zero_return():
    x = pd.Series([0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, np.inf])
    y = pd.Series([0, 0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 3])
    assert ICAnalyzer().calc_ic_series(x, y).iloc[0] == pytest.approx(1)


def test_unmatured_tail_does_not_permanently_hide_genuine_decay():
    days = [d.date() for d in pd.bdate_range("2026-07-01", "2026-07-31")]
    material = {
        "factor_name": "ma5_bias", "direction": 1,
        "as_of_at": "2026-07-31T16:00:00", "through_date": "2026-07-31",
        "calendar": [[d.date().isoformat(), d.weekday() < 5]
                     for d in pd.date_range("2026-07-01", "2026-07-31")],
        "factors": [], "bars": [],
    }
    for t, day in enumerate(days):
        for i in range(10):
            code = f"600{i:03d}"
            ratio = 1 - (i + 1) * .001
            material["factors"].append({"stock_code": code, "trade_date": day.isoformat(), "value": i})
            material["bars"].append({
                "stock_code": code, "trade_date": day.isoformat(), "close": 10 * ratio ** t,
                "prev_close": 10 * ratio ** max(t - 1, 0), "volume": 1000, "source": "ths",
            })
    result = evaluate_ic_material(material, ICAnalyzer(), FactorDecayDetector())
    assert result["decay"]["is_decaying"] is True
    assert result["decay"]["decay_days"] == 20
    assert result["daily_ic"][-1]["status"] == "outcome_session_not_available"


def test_summary_and_decay_do_not_turn_missing_days_into_normal():
    analyzer = ICAnalyzer()
    assert analyzer.summarize_ic(pd.Series([np.nan, np.inf])) == {}
    one = analyzer.summarize_ic(pd.Series([0.0, np.nan]))
    assert one["ic_mean"] == 0 and one["ic_std"] is one["ir"] is None
    assert FactorDecayDetector().detect_decay(pd.Series([-.1] * 19 + [np.nan]))["is_decaying"] is None


@pytest.mark.asyncio
async def test_migration_preserves_legacy_values_upgrade_idempotent_and_downgrade(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    path = Path(__file__).resolve().parents[1] / "alembic/versions/028_factor_evaluation_runs.py"
    spec = importlib.util.spec_from_file_location("factor_run_028", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    def exercise(conn):
        legacy = sa.Table("factor_evaluation", sa.MetaData(),
                          sa.Column("id", sa.Integer, primary_key=True), sa.Column("ic_mean", sa.Float))
        legacy.create(conn)
        conn.execute(legacy.insert().values(id=1, ic_mean=.42))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        migration.upgrade()
        assert sa.inspect(conn).has_table("factor_evaluation_run")
        assert conn.scalar(sa.select(legacy.c.ic_mean)) == .42
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM factor_evaluation_run")) == 0
        migration.downgrade()
        assert not sa.inspect(conn).has_table("factor_evaluation_run")
        assert conn.scalar(sa.select(legacy.c.ic_mean)) == .42
    try:
        async with engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await engine.dispose()
