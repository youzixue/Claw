"""Verified calendar persistence boundaries, temporary SQLite and no provider IO."""
from datetime import date, datetime, timedelta
import importlib
import json
import socket
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import Session

from app.db.session import Base
from app.models.governance import TradeCalendarModel, DataWatermarkRevision
from app.promotion.outcome_evidence import next_recorded_trade_day

module = importlib.import_module("app.core.trade_calendar")


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("calendar regression must not use the network")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest_asyncio.fixture
async def factory(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'calendar.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
            TradeCalendarModel.__table__, DataWatermarkRevision.__table__,
        ]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(module, "async_session", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


async def states(factory):
    async with factory() as db:
        return dict((await db.execute(select(
            TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day
        ))).all())


async def receipts(factory):
    async with factory() as db:
        return (await db.execute(select(DataWatermarkRevision))).scalars().all()


async def seed(factory, values):
    async with factory() as db:
        db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=state,
                                     session_type="full" if state else "closed",
                                     note="preserve existing")
                   for day, state in values.items())
        await db.commit()


@pytest.mark.asyncio
async def test_open_only_loaded_year_is_completed_without_provider_fetch(factory, monkeypatch):
    days = {}
    day = date(2026, 1, 1)
    while day.year == 2026:
        if day.weekday() < 5 and not module.is_official_closed_day(day):
            days[day] = True
        day += timedelta(days=1)
    assert len(days) > 200
    await seed(factory, days)
    def forbidden(*args, **kwargs):
        raise AssertionError("existing open-session calendar must not refetch source")
    monkeypatch.setattr(module.ak, "tool_trade_date_hist_sina", forbidden)
    calendar = module.TradeCalendar()
    before = datetime.now()
    await calendar._ensure_loaded(2026)
    saved = await states(factory)
    assert len(saved) == 365 and sum(saved.values()) == len(days)
    assert saved[date(2026, 9, 25)] is False
    assert saved[date(2026, 2, 24)] is True
    assert saved[date(2026, 10, 8)] is True
    assert saved[date(2026, 10, 9)] is True
    assert next_recorded_trade_day(date(2026, 9, 24), saved, through=date(2026, 9, 29)) == (date(2026, 9, 28), "")
    assert next_recorded_trade_day(date(2026, 9, 18), saved, through=date(2026, 9, 21)) == (date(2026, 9, 21), "")
    audit = await receipts(factory)
    assert len(audit) == 1 and audit[0].observed_at >= before
    proof = json.loads(audit[0].details_json)
    assert proof["historical_arrival_certified"] is False
    assert "2026-09-25" in proof["added_dates"]
    assert audit[0].record_count == 365 - len(days)
    await calendar._ensure_loaded(2026)
    assert len(await receipts(factory)) == 1
    async with factory() as db:
        original = await db.get(TradeCalendarModel, date(2026, 9, 28))
        assert original.note == "preserve existing"


@pytest.mark.asyncio
async def test_known_closures_do_not_fill_ordinary_missing_weekdays(factory):
    await seed(factory, {date(2026, 9, 22): True, date(2026, 9, 24): True})
    await module.TradeCalendar().sync_known_closed_days(2026)
    saved = await states(factory)
    assert date(2026, 9, 23) not in saved
    assert next_recorded_trade_day(date(2026, 9, 22), saved, through=date(2026, 9, 24)) == (None, "outcome_calendar_gap")
    assert not any(state for day, state in saved.items() if day not in (date(2026, 9, 22), date(2026, 9, 24)))


@pytest.mark.asyncio
async def test_conflicting_open_holiday_is_not_overwritten(factory):
    await seed(factory, {date(2026, 9, 25): True})
    calendar = module.TradeCalendar()
    with pytest.raises(ValueError, match="conflicts with recorded"):
        await calendar.sync_known_closed_days(2026)
    assert await states(factory) == {date(2026, 9, 25): True}
    assert await receipts(factory) == [] and calendar._cache == {}


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_inserted_closures(factory):
    def reject_receipt(session, *args):
        if any(isinstance(item, DataWatermarkRevision) for item in session.new):
            raise RuntimeError("audit unavailable")
    event.listen(Session, "before_flush", reject_receipt)
    calendar = module.TradeCalendar()
    try:
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await calendar.sync_known_closed_days(2026)
    finally:
        event.remove(Session, "before_flush", reject_receipt)
    assert await states(factory) == {}
    assert await receipts(factory) == [] and calendar._cache == {}


@pytest.mark.asyncio
async def test_unverified_other_year_is_not_seeded(factory):
    assert await module.TradeCalendar().sync_known_closed_days(2027) == []
    assert await states(factory) == {} and await receipts(factory) == []


@pytest.mark.asyncio
async def test_partial_source_only_adds_provided_opens_and_known_closures(factory, monkeypatch):
    await seed(factory, {date(2026, 9, 23): False})
    frame = SimpleNamespace(iterrows=lambda: iter(enumerate([
        {"trade_date": "2026-09-22"}, {"trade_date": date(2026, 9, 23)},
        {"trade_date": datetime(2026, 9, 24)},
    ])))
    monkeypatch.setattr(module.ak, "tool_trade_date_hist_sina", lambda: frame)
    calendar = module.TradeCalendar()
    await calendar._sync_from_source(2026)
    saved = await states(factory)
    assert saved[date(2026, 9, 22)] is True
    assert saved[date(2026, 9, 24)] is True
    assert saved[date(2026, 9, 23)] is False
    assert date(2026, 9, 21) not in saved
    assert calendar._cache[date(2026, 9, 23)] is False
    assert saved[date(2026, 9, 25)] is False
    assert len(await receipts(factory)) == 1


@pytest.mark.asyncio
async def test_inconsistent_provider_date_does_not_create_evidence(factory, monkeypatch):
    frame = SimpleNamespace(iterrows=lambda: iter([(0, {"trade_date": "2026-09-25"})]))
    monkeypatch.setattr(module.ak, "tool_trade_date_hist_sina", lambda: frame)
    calendar = module.TradeCalendar()
    await calendar._sync_from_source(2026)
    assert await states(factory) == {} and await receipts(factory) == []
    assert await calendar.is_trade_day(date(2026, 9, 25)) is False


@pytest.mark.asyncio
async def test_concurrent_closure_sync_has_one_receipt_without_duplicate_evidence(factory):
    import asyncio
    result = await asyncio.gather(
        module.TradeCalendar().sync_known_closed_days(2026),
        module.TradeCalendar().sync_known_closed_days(2026),
    )
    assert sum(map(len, result)) == len(await states(factory))
    assert len(await receipts(factory)) == 1


@pytest.mark.asyncio
async def test_spring_reopens_on_official_feb24_not_feb25(factory):
    calendar = module.TradeCalendar()
    calendar._cache = {date(2026, 2, day): day < 14 or day >= 24 for day in range(10, 28)}
    assert await calendar.next_trade_day(date(2026, 2, 13)) == date(2026, 2, 24)
