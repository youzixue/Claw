"""Isolated consumer-local reuse: no business clock, policy or global cache changes."""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sql_event

from app.api.v1 import paper
from app.paper import strategy_iteration_challenger as c
from test_challenger_consumer_latency_20260921 import store, ev, log, order, ROUTE


def closed_lots(count=40):
    rows = []
    for i in range(count):
        code = f"{600000+i:06}"
        rows.extend([
            SimpleNamespace(id=i*2+1, code=code, trade_type="buy", amount=100,
                price=10, commission=1, tax=0, realized_pnl=None,
                trade_time=datetime(2026, 9, 21, 10)),
            SimpleNamespace(id=i*2+2, code=code, trade_type="sell", amount=100,
                price=11, commission=1, tax=0.55, realized_pnl=97.45,
                trade_time=datetime(2026, 9, 24, 10)),
        ])
    return rows


@pytest.mark.asyncio
async def test_repeated_closed_lots_reuse_one_calendar_result_and_keep_every_sample(monkeypatch):
    calendar = AsyncMock(return_value=3)
    monkeypatch.setattr(paper, "_trade_day_hold_days", calendar)
    rows = closed_lots()
    actual = await paper._round_trip_samples(list(reversed(rows)))
    assert calendar.await_count == 1
    assert len(actual) == 40
    for i, sample in enumerate(actual):
        assert sample == dict(code=f"{600000+i:06}", buy_time="2026-09-21 10:00:00",
            sell_time="2026-09-24 10:00:00", buy_count=1, sell_count=1,
            pnl=97.45, return_pct=9.74, hold_days=3)
    # A later refresh must see corrections; there is no process/session cache.
    calendar.return_value = 2
    assert {s["hold_days"] for s in await paper._round_trip_samples(rows)} == {2}
    assert calendar.await_count == 2


@pytest.mark.asyncio
async def test_empty_and_unclosed_lots_do_not_load_calendar(monkeypatch):
    calendar = AsyncMock(side_effect=AssertionError("calendar not needed"))
    monkeypatch.setattr(paper, "_trade_day_hold_days", calendar)
    assert await paper._round_trip_samples([]) == []
    assert await paper._round_trip_samples(closed_lots(1)[:1]) == []
    calendar.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_date_pairs_and_same_day_zero_are_not_conflated(monkeypatch):
    rows = closed_lots(3)
    rows[3].trade_time = datetime(2026, 9, 23, 10)
    rows[5].trade_time = rows[4].trade_time
    calendar = AsyncMock(side_effect=[0, 2, 3])
    monkeypatch.setattr(paper, "_trade_day_hold_days", calendar)
    actual = await paper._round_trip_samples(rows)
    assert [(s["code"], s["hold_days"]) for s in actual] == [
        ("600002", 0), ("600001", 2), ("600000", 3)]
    assert calendar.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_calendar_failure_or_cancel_propagates_and_does_not_cache(monkeypatch, error):
    calendar = AsyncMock(side_effect=error("injected"))
    monkeypatch.setattr(paper, "_trade_day_hold_days", calendar)
    with pytest.raises(error, match="injected"):
        await paper._round_trip_samples(closed_lots())
    calendar.side_effect = None
    calendar.return_value = 3
    assert len(await paper._round_trip_samples(closed_lots())) == 40
    assert calendar.await_count == 2


@pytest.mark.asyncio
async def test_real_task_cancel_during_calendar_load_leaves_no_cached_result(monkeypatch):
    entered = asyncio.Event()
    async def wait(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(paper, "_trade_day_hold_days", wait)
    task = asyncio.create_task(paper._round_trip_samples(closed_lots()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    calendar = AsyncMock(return_value=4)
    monkeypatch.setattr(paper, "_trade_day_hold_days", calendar)
    assert {s["hold_days"] for s in await paper._round_trip_samples(closed_lots())} == {4}
    assert calendar.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected_queries", [("order", 3), ("trade", 6), ("log", 9)])
async def test_positive_only_short_circuit_query_counts(store, kind, expected_queries):
    from app.models.paper import PaperTradeLog
    _, maker = store
    engine = store[0]
    keys = [f"positive-{i}" for i in range(401)]
    if kind == "order":
        rows = [order(k) for k in keys]
    elif kind == "trade":
        rows = [PaperTradeLog(account_id=11, code="600001", trade_type="buy",
            price=10, amount=100, trade_time=datetime(2026, 9, 21, 10),
            signal_id=c._signal_token(k, ROUTE)) for k in keys]
    else:
        rows = [log(k, action="skip_terminal") for k in keys]
    async with maker() as db:
        db.add_all(rows)
        await db.commit()
        statements = []
        def count(conn, cursor, stmt, params, context, many):
            if stmt.lstrip().upper().startswith("SELECT"):
                statements.append(stmt)
        sql_event.listen(engine.sync_engine, "before_cursor_execute", count)
        try:
            actual = await c._processed_event_keys(db, [ev(k) for k in keys],
                {ROUTE: (11, "challenger_b")}, quote_round_id="r")
        finally:
            sql_event.remove(engine.sync_engine, "before_cursor_execute", count)
        assert actual == set(keys)
        assert len(statements) == expected_queries


@pytest.mark.asyncio
async def test_negative_result_never_survives_new_order_or_new_round(store):
    _, maker = store
    async with maker() as db:
        kwargs = dict(quote_round_id="new")
        accounts = {ROUTE: (11, "challenger_b")}
        db.add(log("wait", quote_round_id="old"))
        await db.commit()
        assert await c._processed_event_keys(db, [ev("wait"), ev("later")], accounts, **kwargs) == set()
        db.add(order("later"))
        await db.commit()
        assert await c._processed_event_keys(db, [ev("wait"), ev("later")], accounts, **kwargs) == {"later"}
        assert await c._already_processed(db, account_id=11, account_name="challenger_b",
            run_id=c._run_id("later"), signal_token=c._signal_token("later", ROUTE),
            quote_round_id="new")
        assert await c._processed_event_keys(db, [ev("wait")], accounts, quote_round_id="old") == {"wait"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", [1, 2, 3])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_batch_read_failure_or_cancel_is_not_success(failure_at, error):
    empty = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []), all=lambda: [])
    reads = [empty] * (failure_at-1) + [error("injected")]
    db = SimpleNamespace(execute=AsyncMock(side_effect=reads))
    with pytest.raises(error, match="injected"):
        await c._processed_event_keys(db, [ev("x")],
            {ROUTE: (11, "challenger_b")}, quote_round_id="r")
    assert db.execute.await_count == failure_at


@pytest.mark.asyncio
async def test_real_calendar_repeated_read_parity(store, monkeypatch, record_property):
    """Actual calendar loader reads a temp DB, never the running DB/network."""
    from app.core import trade_calendar as calendar_module
    from app.models.governance import TradeCalendarModel
    engine, maker = store
    first = date(2026, 1, 1)
    async with maker() as db:
        db.add_all([TradeCalendarModel(trade_date=first+timedelta(days=i),
            is_trade_day=(first+timedelta(days=i)).weekday() < 5, session_type="full")
            for i in range(365)])
        await db.commit()
    calendar = calendar_module.TradeCalendar()
    monkeypatch.setattr(calendar_module, "async_session", maker)
    monkeypatch.setattr(paper, "trade_calendar", calendar)
    sync = AsyncMock(side_effect=AssertionError("network calendar forbidden"))
    monkeypatch.setattr(calendar, "_sync_from_source", sync)
    selects = []
    def count(conn, cursor, stmt, params, context, many):
        if stmt.lstrip().upper().startswith("SELECT"):
            selects.append(stmt)
    sql_event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        # Pre-change loop invoked the same real hold-day function once per lot.
        before = [await paper._trade_day_hold_days(date(2026, 9, 21), date(2026, 9, 24))
                  for _ in range(40)]
        before_queries = len(selects)
        selects.clear()
        samples = await paper._round_trip_samples(closed_lots())
        assert before == [s["hold_days"] for s in samples] == [3] * 40
        assert before_queries == 40 and len(selects) == 1
        record_property("calendar_selects_before", before_queries)
        record_property("calendar_selects_after", len(selects))
    finally:
        sql_event.remove(engine.sync_engine, "before_cursor_execute", count)
    sync.assert_not_awaited()
    # Even a previously cached range is reloaded by the next call.
    async with maker() as db:
        day = await db.get(TradeCalendarModel, date(2026, 9, 23))
        day.is_trade_day = False
        await db.commit()
    assert {s["hold_days"] for s in await paper._round_trip_samples(closed_lots())} == {2}


@pytest.mark.asyncio
async def test_unmapped_and_empty_batch_are_query_free():
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("query forbidden")))
    assert await c._processed_event_keys(db, [], {}, quote_round_id="r") == set()
    assert await c._processed_event_keys(db, [ev("x")], {}, quote_round_id="r") == set()
