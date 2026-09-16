"""Isolated real-SQL factor input tests; no live DB/API/network or PIT claims."""
from datetime import date, datetime, timedelta, timezone
import json

import pandas as pd
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.session import Base
from app.models.stock import StockDaily, StockKline, FundFlow
from app.models.factor import FactorValue
from app.models.governance import TradeCalendarModel
from app.factors import FactorEngine
from app.factors.market_inputs import factor_sessions, load_factor_market_inputs, PROTOCOL
from app.api.v1 import factors as api, eval_scheduler as scheduler_api
from app.risk import factor_scheduler as scheduler

DAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 16)
CODE = "600001"


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inputs.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(api, "datetime", Clock)
    monkeypatch.setattr(scheduler, "datetime", Clock)
    async with factory() as session:
        yield session
    await engine.dispose()


async def seed(db):
    first = date(2026, 7, 1)
    for offset in range((date(2026, 9, 20) - first).days + 1):
        day = first + timedelta(days=offset)
        db.add(TradeCalendarModel(trade_date=day, is_trade_day=day.weekday() < 5))
    await db.commit()
    days = await factor_sessions(db, trade_date=DAY, as_of_at=NOW)
    for day in days:
        db.add(StockKline(code=CODE, trade_date=day, open=10, close=10, high=10.5,
                         low=9.5, prev_close=10, volume=10000, amount=100000,
                         turnover=1, change_pct=0, source="ths"))
        db.add(StockDaily(code=CODE, trade_date=day, open=990, close=990, high=990,
                         low=990, prev_close=990, volume=99, amount=99,
                         turnover=99, amplitude=99, change_pct=99))
        clock = datetime.combine(day, datetime.min.time()).replace(hour=15)
        db.add(FundFlow(code=CODE, trade_date=day, main_net_inflow=1000,
                       main_net_inflow_pct=1, big_net_inflow=0, mid_net_inflow=None,
                       small_net_inflow=-100, source="tencent", source_version="tencent_hsfundtab_v1",
                       source_quote_at=clock, received_at=clock, observed_at=clock))
    await db.commit()
    return days


async def row(db, model, day=DAY):
    return await db.scalar(select(model).where(model.code == CODE, model.trade_date == day))


async def load(db, **kwargs):
    return await load_factor_market_inputs(
        db, code=kwargs.pop("code", CODE), trade_date=kwargs.pop("trade_date", DAY),
        as_of_at=kwargs.pop("as_of_at", NOW), **kwargs)


@pytest.mark.asyncio
async def test_single_and_batch_consume_identical_real_market_inputs(db):
    await seed(db)
    reply = await api.compute_factors(CODE, DAY.isoformat(), db)
    batch = await scheduler.FactorEvaluationScheduler().compute_and_store_factors(db, DAY, [CODE])
    assert reply["input_evidence"] == batch["input_evidence"][CODE]
    assert reply["input_evidence"]["protocol"] == PROTOCOL
    assert reply["input_evidence"]["usable_bar_count"] == 30
    assert reply["input_evidence"]["qualified_fund_count"] == 30
    assert reply["factors"]["ma5_bias"]["value"] == 0
    assert reply["factors"]["main_inflow_strength"]["value"] == 1
    assert reply["factors"]["big_order_pct"]["value"] == 0
    assert reply["factors"]["fund_flow_trend_3d"]["value"] == 3000
    assert reply["factors"]["retail_outflow_pct"]["value"] == .1
    stored = await db.scalar(select(FactorValue).where(FactorValue.factor_name == "main_inflow_strength"))
    assert stored.factor_value == 1 and stored.trade_date == DAY
    assert batch["scope"] == "bounded_per_stock_research_not_ranked_cross_section"
    assert batch["point_in_time_verified"] is batch["automatic_weight_update"] is False
    assert batch["promotion_eligible"] is False
    assert reply["factor_count"] == 48
    assert 0 < reply["available_factor_count"] < 48
    assert reply["available_factor_count"] == batch["available_factor_values"]
    assert reply["input_evidence"]["column_available_counts"]["amplitude"] == 0
    json.dumps(reply, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 9, 14, 9, 30), date(2026, 9, 11)),
    (datetime(2026, 9, 14, 15, 9, 59), date(2026, 9, 11)),
    (datetime(2026, 9, 14, 15, 10), DAY),
    (datetime(2026, 9, 13, 16), date(2026, 9, 11)),
    (datetime(2026, 9, 15, 0, 1), DAY),
])
async def test_default_resolves_completed_calendar_not_latest_stock_row(db, now, expected):
    await seed(db)
    frame, evidence = await load(db, trade_date=None, as_of_at=now)
    assert frame.iloc[-1]["trade_date"] == expected
    assert evidence["trade_date"] == expected.isoformat()
    assert evidence["point_in_time_verified"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("day,now,reason", [
    (date(2026, 9, 13), NOW, "not_confirmed_session"),
    (DAY, datetime(2026, 9, 14, 15, 9), "not_completed"),
    (date(2026, 9, 15), NOW, "not_completed"),
    ("2026-09-14", NOW, "requires_date"),
    (DAY, NOW.replace(tzinfo=timezone.utc), "local_naive_clock"),
])
async def test_invalid_explicit_day_never_silently_changes_label(db, day, now, reason):
    await seed(db)
    with pytest.raises(ValueError, match=reason):
        await load(db, trade_date=day, as_of_at=now)


@pytest.mark.asyncio
async def test_absent_and_gapped_calendar_do_not_infer_from_market_rows(db):
    with pytest.raises(ValueError, match="calendar_incomplete"):
        await load(db)
    days = await seed(db)
    calendar = await db.scalar(select(TradeCalendarModel).where(
        TradeCalendarModel.trade_date == days[-3]))
    await db.delete(calendar)
    await db.commit()
    with pytest.raises(ValueError, match="calendar_incomplete"):
        await load(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, -3, -20])
async def test_missing_bar_retains_date_hole_not_stockdaily_fallback(db, offset):
    days = await seed(db)
    await db.delete(await row(db, StockKline, days[offset]))
    await db.commit()
    frame, evidence = await load(db)
    assert len(frame) == 30
    assert pd.isna(frame.iloc[offset]["close"])
    assert "bar_missing" in evidence["days"][offset]["bar_issues"]
    results = await FactorEngine().compute_single(CODE, DAY, frame)
    if offset == -20:
        assert results["ma5_bias"].value == 0
    else:
        assert results["ma5_bias"].value is None
    assert results["macd_signal"].value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,reason", [
    ("source", "spot_fallback", "nonformal_source"),
    ("source", "unknown", "nonformal_source"),
    ("open", -1, "invalid_ohlc"),
    ("high", 9, "invalid_envelope"),
    ("volume", 0, "volume_missing_or_suspended"),
    ("volume", None, "volume_missing_or_suspended"),
    ("amount", -1, "invalid_amount"),
    ("turnover", -1, "invalid_turnover"),
    ("prev_close", None, "price_chain_unknown"),
    ("prev_close", 10.02, "price_chain_conflict"),
])
async def test_structural_source_and_chain_errors_never_make_scores(db, field, value, reason):
    await seed(db)
    setattr(await row(db, StockKline), field, value)
    await db.commit()
    frame, evidence = await load(db)
    assert reason in evidence["days"][-1]["bar_issues"]
    assert pd.isna(frame.iloc[-1]["close"])
    assert (await FactorEngine().compute_single(CODE, DAY, frame))["ma5_bias"].value is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,status", [
    ("source", "akshare", "unsupported_source"),
    ("source_version", "unknown", "unsupported_source"),
    ("main_net_inflow", None, "invalid_values"),
    ("main_net_inflow_pct", None, "invalid_values"),
    ("source_quote_at", None, "unknown"),
    ("source_quote_at", datetime(2026, 9, 14, 14, 59), "incomplete_session"),
    ("observed_at", datetime(2026, 9, 14, 17), "future"),
])
async def test_fund_source_and_real_clock_gate_not_raw_latest(db, field, value, status):
    await seed(db)
    setattr(await row(db, FundFlow), field, value)
    await db.commit()
    frame, evidence = await load(db)
    assert evidence["days"][-1]["fund_status"] == status
    assert pd.isna(frame.iloc[-1]["main_net_inflow"])
    assert pd.isna(frame.iloc[-1]["big_net_inflow"])
    results = await FactorEngine().compute_single(CODE, DAY, frame)
    assert results["main_inflow_strength"].value is None
    assert results["fund_flow_trend_3d"].value is None
    assert results["ma5_bias"].value == 0


@pytest.mark.asyncio
async def test_true_zero_and_missing_breakdown_remain_distinct(db):
    await seed(db)
    fund = await row(db, FundFlow)
    fund.main_net_inflow = 0
    fund.big_net_inflow = None
    await db.commit()
    frame, evidence = await load(db)
    results = await FactorEngine().compute_single(CODE, DAY, frame)
    assert results["main_inflow_strength"].value == 0
    assert results["big_order_pct"].value is None
    assert frame.iloc[-1]["small_net_inflow"] == -100
    assert pd.isna(frame.iloc[-1]["amplitude"])
    assert evidence["qualified_fund_count"] == 30


@pytest.mark.asyncio
async def test_internal_fund_hole_does_not_compress_three_days(db):
    days = await seed(db)
    await db.delete(await row(db, FundFlow, days[-2]))
    await db.commit()
    frame, evidence = await load(db)
    results = await FactorEngine().compute_single(CODE, DAY, frame)
    assert results["fund_flow_trend_3d"].value is None
    assert results["main_inflow_strength"].value == 1
    assert evidence["days"][-2]["fund_status"] == "missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["１２３４５６", "60001", "600001x", "", " 600001", None])
async def test_code_validation_is_ascii_and_exact(db, code):
    with pytest.raises(ValueError, match="six_ascii_digits"):
        await load(db, code=code)


@pytest.mark.asyncio
async def test_old_factor_rows_remain_unchanged_and_all_query_inputs_are_readonly(db):
    await seed(db)
    service = scheduler.FactorEvaluationScheduler()
    first = await service.compute_and_store_factors(db, DAY, [CODE])
    old = await db.scalar(select(FactorValue).where(FactorValue.factor_name == "main_inflow_strength"))
    fund = await row(db, FundFlow)
    fund.main_net_inflow = 2000
    await db.commit()
    second = await service.compute_and_store_factors(db, DAY, [CODE])
    assert first["saved_values"] == 48 and second["saved_values"] == 0
    await db.refresh(old)
    assert old.factor_value == 1
    assert second["input_evidence"][CODE]["input_values_sha256"] != first["input_evidence"][CODE]["input_values_sha256"]
    # Pending unrelated source changes must not flush or contaminate SQL projections.
    fund.main_net_inflow = 9999
    frame, _ = await load(db)
    assert frame.iloc[-1]["main_net_inflow"] == 2000
    assert fund in db.dirty
    await db.rollback()


@pytest.mark.asyncio
async def test_bounded_batch_exposes_deterministic_deferred_denominator(db, monkeypatch):
    await seed(db)
    service = scheduler.FactorEvaluationScheduler()
    observed = []
    original_compute = scheduler.FactorEngine.compute_single
    async def observed_compute(self, code, day, frame):
        observed.append(code)
        return await original_compute(self, code, day, frame)
    monkeypatch.setattr(scheduler.FactorEngine, "compute_single", observed_compute)
    codes = [f"600{i:03d}" for i in range(101)]
    report = await service.compute_and_store_factors(db, DAY, list(reversed(codes)) + [codes[0]])
    assert report["status"] == "partial"
    assert report["requested_count"] == 101 and report["attempted_count"] == 100
    assert report["computed_stocks"] == 100
    assert observed == codes[:100] and report["deferred_codes"] == codes[100:]
    # Real calculators now provide the captured 48-factor denominator. Unknown
    # stocks remain null, rather than a fake empty result pretending to be complete.
    assert await db.scalar(select(func.count()).select_from(FactorValue)) == 4800
    assert report["computation_capture"]["status"] == "committed"


@pytest.mark.asyncio
@pytest.mark.parametrize("function,args", [
    (api.compute_factors, ("600001", "bad")),
    (api.compute_factors, ("600001", "2026-09-15")),
    (scheduler_api.compute_and_store_factors, ("bad", "600001")),
    (scheduler_api.compute_and_store_factors, ("2026-09-15", "600001")),
])
async def test_api_invalid_inputs_are_422_not_server_error(db, function, args):
    with pytest.raises(HTTPException) as caught:
        await function(*args, db)
    assert caught.value.status_code == 422


@pytest.mark.asyncio
async def test_real_http_contract_preserves_null_zero_and_422_without_lifespan(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    await seed(db)
    app = FastAPI()
    app.include_router(api.router, prefix="/factors")
    async def isolated_db():
        yield db
    app.dependency_overrides[api.get_db] = isolated_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://isolated") as client:
        response = await client.post("/factors/compute/600001?trade_date=2026-09-14")
        assert response.status_code == 200
        body = response.json()
        assert body["factors"]["big_order_pct"]["value"] == 0
        assert body["factors"]["sector_fund_flow"]["value"] is None
        assert body["factors"]["sector_fund_flow"]["confidence"] == 0
        assert body["input_evidence"]["point_in_time_verified"] is False
        bad = await client.post("/factors/compute/１２３４５６")
        assert bad.status_code == 422


@pytest.mark.asyncio
async def test_empty_stock_has_unknown_factors_not_48_healthy_observations(db):
    await seed(db)
    result = await api.compute_factors("600002", DAY.isoformat(), db)
    assert result["available_factor_count"] == 0 and result["factor_count"] == 48
    assert result["input_evidence"]["usable_bar_count"] == 0
    assert all(r["value"] is None and r["confidence"] == 0 for r in result["factors"].values())
    assert await db.scalar(select(func.count()).select_from(FactorValue)) == 0


@pytest.mark.asyncio
async def test_missing_price_anchor_is_not_invented_from_first_row(db):
    days = await seed(db)
    await db.delete(await row(db, StockKline, days[0]))
    await db.commit()
    frame, evidence = await load(db)
    assert pd.isna(frame.iloc[0]["close"])
    assert evidence["days"][0]["price_chain"]["status"] == "unknown"
    assert evidence["days"][1]["bar_issues"] == []
    factors = await FactorEngine().compute_single(CODE, DAY, frame)
    assert factors["macd_signal"].value is None and factors["ma5_bias"].value == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("gap,expected", [(0.01, "consistent"), (0.02, "conflict")])
async def test_existing_shared_price_tolerance_unchanged(db, gap, expected):
    await seed(db)
    bar = await row(db, StockKline)
    bar.prev_close = 10 + gap
    await db.commit()
    _, evidence = await load(db)
    assert evidence["days"][-1]["price_chain"]["max_abs_gap"] == .011
    assert evidence["days"][-1]["price_chain"]["status"] == expected


@pytest.mark.asyncio
async def test_excluded_future_rows_do_not_change_evaluated_frame_hash(db):
    await seed(db)
    frame, before = await load(db)
    db.add(StockKline(code=CODE, trade_date=date(2026, 9, 15), open=999, close=999,
                     high=999, low=999, source="ths", volume=10000))
    await db.commit()
    after_frame, after = await load(db)
    pd.testing.assert_frame_equal(frame, after_frame)
    assert before == after


@pytest.mark.asyncio
async def test_calendar_rechecked_after_batch_initial_lookup(db, monkeypatch):
    await seed(db)
    original = scheduler.FactorEngine.compute_single
    calls = []
    async def change_calendar_after_first(self, code, day, frame):
        calls.append(code)
        result = await original(self, code, day, frame)
        calendar = await db.scalar(select(TradeCalendarModel).where(
            TradeCalendarModel.trade_date == DAY))
        await db.delete(calendar)
        await db.flush()
        return result
    monkeypatch.setattr(scheduler.FactorEngine, "compute_single", change_calendar_after_first)
    report = await scheduler.FactorEvaluationScheduler().compute_and_store_factors(
        db, DAY, [CODE, "600002"])
    assert calls == [CODE]
    assert report["status"] == "partial" and report["errors"] == 1
    assert report["failures"] == {"600002": "ValueError"}
    assert report["computed_stocks"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("codes", [[], None])
async def test_no_market_codes_is_not_successful_full_market_batch(db, codes):
    await seed(db)
    if codes is None:
        from sqlalchemy import delete
        await db.execute(delete(StockKline))
        await db.commit()
    report = await scheduler.FactorEvaluationScheduler().compute_and_store_factors(db, DAY, codes)
    assert report["status"] == "no_data"
    assert report["computed_stocks"] == report["saved_values"] == report["requested_count"] == 0
    assert report["available_factor_values"] == 0
