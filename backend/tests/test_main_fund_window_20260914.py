"""Synthetic session/calendar/window contracts; no HTTP or business database."""
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.data.main_fund_window import (
    load_main_fund_window, completed_fund_through, closed_fund_status,
    fund_window_payload, MAIN_FUND_WINDOW_VERSION,
)
from app.models.stock import FundFlow, StockSpot
from app.models.governance import TradeCalendarModel
from app.db.session import Base

DAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 15, 1)
PREVIOUS = [date(2026, 9, d) for d in (7, 8, 9, 10, 11)]
CLOSED = PREVIOUS[1:] + [DAY]


def fund(day, amount=10, code="000001", **overrides):
    source = datetime.combine(day, datetime.min.time()).replace(hour=15)
    return {
        "code": code, "trade_date": day, "main_net_inflow": amount, "main_net_inflow_pct": 0,
        "source": "tencent", "source_version": "tencent_hsfundtab_v1",
        "source_quote_at": source, "received_at": source + timedelta(seconds=5),
        "observed_at": source + timedelta(seconds=6), **overrides,
    }


@pytest.fixture(autouse=True)
def no_provider_http(monkeypatch):
    import httpx
    import requests
    def forbidden(*args, **kwargs):
        raise AssertionError("No network in fund-window tests")
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fund-window.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as session:
            yield session
    finally:
        await engine.dispose()


async def seed(db, *, sessions=PREVIOUS + [DAY], code="000001", amounts=None):
    db.add_all([TradeCalendarModel(trade_date=day, is_trade_day=True) for day in sessions])
    db.add_all([FundFlow(**fund(day, (amounts or {}).get(day, 10), code)) for day in sessions])
    await db.commit()


def frozen_clock(monkeypatch, api, now=NOW):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(api, "datetime", Clock)


@pytest.mark.parametrize("now,through,expected", [
    (NOW.replace(hour=14, minute=59), DAY, DAY - timedelta(days=1)),
    (NOW.replace(hour=15, minute=0), DAY, DAY),
    (NOW, DAY, DAY),
    (NOW, DAY + timedelta(days=1), None),
    (NOW, None, None),
])
def test_completed_boundary_is_explicit(now, through, expected):
    assert completed_fund_through(through, now) == expected


@pytest.mark.asyncio
async def test_exact_five_session_window_not_seven_or_ten_natural_days(db):
    await seed(db, amounts={PREVIOUS[0]: 9e12})
    window = await load_main_fund_window(db, through_date=DAY, decision_at=NOW, codes=["000001"])
    assert window["session_dates"] == [d.isoformat() for d in CLOSED]
    assert window["items"]["000001"]["total"] == 50  # old seventh-day extra would dominate
    assert window["basis"] == "dated_latest_not_pit"
    assert window["version"] == MAIN_FUND_WINDOW_VERSION
    assert fund_window_payload(window, "000001")["fund_5d_complete"] is True


@pytest.mark.asyncio
async def test_intraday_excludes_today_and_weekend_keeps_friday(db):
    await seed(db, amounts={DAY: 9e12})
    window = await load_main_fund_window(db, through_date=DAY, decision_at=NOW.replace(hour=14), codes=["000001"])
    assert window["session_dates"] == [d.isoformat() for d in PREVIOUS]
    assert window["items"]["000001"]["total"] == 50
    weekend = await load_main_fund_window(db, through_date=date(2026, 9, 13),
                                         decision_at=datetime(2026, 9, 13, 18), codes=["000001"])
    assert weekend["items"]["000001"]["total"] == 50


@pytest.mark.asyncio
async def test_long_official_holiday_uses_confirmed_sessions_not_calendar_span(db):
    sessions = [date(2026, 9, d) for d in (24, 28, 29, 30)] + [date(2026, 10, 12)]
    await seed(db, sessions=sessions)
    result = await load_main_fund_window(db, through_date=sessions[-1],
                                        decision_at=datetime(2026, 10, 12, 16), codes=["000001"])
    assert result["session_dates"] == [d.isoformat() for d in sessions]
    assert result["items"]["000001"]["total"] == 50


@pytest.mark.asyncio
async def test_calendar_gap_cannot_be_filled_by_earlier_fund_rows(db):
    await seed(db)
    row = await db.get(TradeCalendarModel, date(2026, 9, 10))
    await db.delete(row)
    await db.commit()
    result = await load_main_fund_window(db, through_date=DAY, decision_at=NOW, codes=["000001"])
    assert result["status"] == "calendar_incomplete"
    assert result["items"] == {}
    assert fund_window_payload(result, "000001")["fund_5d_billion"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,expected", [(0, 0), (-10, -50), (10, 50)])
async def test_real_zero_and_signed_totals_are_preserved(db, amount, expected):
    await seed(db, amounts={day: amount for day in PREVIOUS + [DAY]})
    result = await load_main_fund_window(db, through_date=DAY, decision_at=NOW, codes=["000001", "000002"])
    assert result["items"]["000001"]["total"] == expected
    assert result["items"]["000002"]["total"] is None
    assert result["items"]["000002"]["status_counts"] == {"missing": 5}


@pytest.mark.parametrize("overrides,status", [
    ({"main_net_inflow": None}, "invalid_values"),
    ({"main_net_inflow": float("inf")}, "invalid_values"),
    ({"main_net_inflow": True}, "invalid_values"),
    ({"main_net_inflow_pct": None}, "invalid_values"),
    ({"source": "stock_spot"}, "unsupported_source"),
    ({"source_quote_at": None}, "unknown"),
    ({"source_quote_at": NOW.replace(hour=14, minute=59), "received_at": NOW, "observed_at": NOW}, "incomplete_session"),
    ({"observed_at": NOW + timedelta(seconds=1)}, "future"),
    ({"received_at": NOW.replace(hour=14)}, "invalid"),
])
def test_invalid_history_never_becomes_complete_session(overrides, status):
    assert closed_fund_status(fund(DAY, **overrides), decision_at=NOW) == status


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing", "null", "future", "unknown_clock", "preclose", "overflow"])
async def test_one_bad_session_means_unknown_not_partial_sum_or_backfill(db, case):
    await seed(db)
    row = await db.scalar(select(FundFlow).where(FundFlow.trade_date == DAY))
    if case == "missing":
        await db.delete(row)
    elif case == "null":
        row.main_net_inflow = None
    elif case == "future":
        row.observed_at = NOW + timedelta(seconds=1)
    elif case == "unknown_clock":
        row.source_quote_at = None
    elif case == "preclose":
        row.source_quote_at = NOW.replace(hour=14, minute=59)
    else:
        for item in (await db.scalars(select(FundFlow))).all():
            item.main_net_inflow = 1e308
    await db.commit()
    result = await load_main_fund_window(db, through_date=DAY, decision_at=NOW, codes=["000001"])
    item = result["items"]["000001"]
    assert item["total"] is None
    assert item["complete"] is False
    assert result["session_dates"] == [d.isoformat() for d in CLOSED]


@pytest.mark.asyncio
async def test_reader_does_not_flush_or_rewind_a_later_latest_row(db):
    await seed(db)
    row = await db.scalar(select(FundFlow).where(FundFlow.trade_date == DAY))
    row.main_net_inflow = 999999
    db.add(StockSpot(code="pending", price=10))
    result = await load_main_fund_window(db, through_date=DAY, decision_at=NOW, codes=["000001"])
    assert result["items"]["000001"]["total"] == 50  # no autoflush
    assert row in db.dirty and len(db.new) == 1
    await db.rollback()
    row = await db.scalar(select(FundFlow).where(FundFlow.trade_date == DAY))
    row.observed_at = NOW + timedelta(minutes=1)
    await db.commit()
    result = await load_main_fund_window(db, through_date=DAY, decision_at=NOW, codes=["000001"])
    assert result["items"]["000001"]["total"] is None  # not yesterday's value relabeled today


@pytest.mark.asyncio
async def test_detail_and_anomaly_history_share_complete_window_contract(db):
    from app.api.v1 import tenbagger as api
    await seed(db)
    context = await api._load_stock_fund_context(db, "000001", quote_trade_date=DAY,
                                                completed_trade_date=DAY, as_of_at=NOW)
    recent = await api._load_recent_main_fund_map(db, DAY, as_of_at=NOW)
    assert context["fund_5d_total"] == sum(row["main_net_inflow"] for row in recent["000001"]) == 50
    row = await db.scalar(select(FundFlow).where(FundFlow.trade_date == DAY))
    row.main_net_inflow = None
    await db.commit()
    recent = await api._load_recent_main_fund_map(db, DAY, as_of_at=NOW)
    assert recent["000001"] == []  # detector's legacy None->0 coercion cannot run
    context = await api._load_stock_fund_context(db, "000001", quote_trade_date=DAY,
                                                completed_trade_date=DAY, as_of_at=NOW)
    assert context["fund_5d_total"] is None
    assert context["fund_5d_count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bull", "tenbagger"])
async def test_rank_models_receive_exact_window_and_cache_keeps_original_boundary(db, monkeypatch, mode):
    from app.api.v1 import tenbagger as api
    await seed(db, amounts={PREVIOUS[0]: 9e12})
    db.add_all([StockSpot(code=f"{i:06}", name="窗口隔离", price=10, prev_close=10,
                         open=10, high=10, low=10, volume=10000, amount=1e7,
                         change_pct=0, turnover=1, volume_ratio=1, pe_ttm=15, pb=2,
                         circ_market_cap=50, net_profit_growth=20) for i in (1, 2)])
    await db.commit()
    frozen_clock(monkeypatch, api)
    monkeypatch.setattr(api, "_RANK_CACHE", {})
    monkeypatch.setattr(api, "resolve_latest_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_persisted_dashboard_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_persist_dashboard_snapshot", AsyncMock())
    async def keep(db, rows):
        return rows
    monkeypatch.setattr(api.stock_tagger, "filter_signals", keep)
    model = api._bull_model if mode == "bull" else api.tenbagger_model
    method = "score_from_spot" if mode == "bull" else "score"
    spy = Mock(wraps=getattr(model, method))
    monkeypatch.setattr(model, method, spy)
    calculate = api._bull_rank if mode == "bull" else api._tenbagger_rank
    result = await calculate(db)
    rows = {row["code"]: row for row in result["rank"]}
    assert set(rows) == {"000001", "000002"}
    assert rows["000001"]["fund_5d_billion"] == 50 / 1e8
    assert rows["000001"]["fund_5d_complete"] is True
    assert rows["000002"]["fund_5d_billion"] is None
    assert rows["000002"]["fund_5d_complete"] is False
    for call in spy.call_args_list:
        assert call.kwargs["main_net_inflow_5d"] == (50 if call.kwargs["code"] == "000001" else None)
    assert rows["000001"]["fund_5d_window"]["decision_at"] == NOW.isoformat()
    frozen_clock(monkeypatch, api, NOW + timedelta(days=1))
    assert await calculate(db) == result  # frozen ranking, not a historical PIT recomputation


@pytest.mark.asyncio
async def test_complete_zero_does_not_trigger_existing_missing_fund_plan_gate(db):
    from app.signal.next_day_plan import next_day_plan_engine
    await seed(db, amounts={day: 0 for day in PREVIOUS + [DAY]})
    window = await load_main_fund_window(db, through_date=DAY, decision_at=NOW, codes=["000001", "000002"])
    args = dict(code="000001", name="隔离门禁", bull_level="A", bull_score=90, price=10,
                change_pct=0, ma5=10, ma10=9.9, ma20=9.8, volume_ratio=1.5)
    for code in ("000001", "000002"):
        evidence = fund_window_payload(window, code)
        plan = next_day_plan_engine.generate(
            **args, fund_5d_billion=evidence["fund_5d_billion"],
            fund_5d_complete=evidence["fund_5d_complete"],
        )
        assert any("近5日资金数据不足" in reason for reason in plan.avoid_reasons) == (code == "000002")


def test_score_formulas_remain_original_for_known_inputs():
    from app.signal.bull_score import BullScoreModel
    from app.signal.tenbagger_model import TenbaggerModel
    assert BullScoreModel.WEIGHTS["capital"] == 0.25
    assert TenbaggerModel.WEIGHTS["capital"] == 0.15
    bull = BullScoreModel()
    baseline = dict(code="000001", name="基线", turnover=1, volume_ratio=1)
    neutral = bull.score(**baseline, main_net_inflow_5d=0).dimensions["capital"]
    assert bull.score(**baseline, main_net_inflow_5d=300000000).dimensions["capital"] == neutral
    assert bull.score(**baseline, main_net_inflow_5d=300000001).dimensions["capital"] == neutral + 5
    assert bull.score(**baseline, main_net_inflow_5d=-200000001).dimensions["capital"] == neutral - 8
