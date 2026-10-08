"""Recent limit-up memory: exact pre-change outputs, SQL and bounded cursor lifecycle."""
import asyncio
import json
import hashlib
from datetime import date, timedelta
from pathlib import Path
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.api.v1 import promotion as p

DAY = date(2026, 9, 21)

@pytest.fixture
def baseline():
    path = Path(__file__).parent / "fixtures" / "recent_limitup_memory_v2.txt"
    source = path.read_bytes()
    assert hashlib.sha256(source).hexdigest() == "4cd89cf21c305778691859e9e4deb6543f3eb86616f712683a1c1317faaa008e"
    scope = dict(vars(p))
    exec(compile(source, str(path), "exec"), scope)
    return scope["_load_recent_limit_up_memory"]

@pytest.fixture
def candidate():
    return p._load_recent_limit_up_memory

@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory.sqlite'}")
    # Projection-only adversarial fixture deliberately omits production uniqueness:
    # duplicate keys must preserve cursor last-write-wins even in legacy/bad data.
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE stock_kline (code TEXT, trade_date DATE, change_pct FLOAT, prev_close FLOAT, close FLOAT, high FLOAT)"))
        await conn.execute(text("CREATE TABLE limit_up_pool (code TEXT, name TEXT, trade_date DATE, consecutive_days INTEGER, seal_amount FLOAT, break_count INTEGER)"))
    try:
        async with AsyncSession(engine) as session:
            yield session
    finally:
        await engine.dispose()

def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)

@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 511, 512, 513, 2049])
async def test_real_session_all_features_sql_params_order(db, baseline, candidate, count):
    codes = [f"600{i:03}" for i in range(max(0, count - 1))]  # plus one duplicate = exact row boundary
    if codes:
        await db.execute(text("INSERT INTO stock_kline VALUES (:c,:d,10,10,11,11)"),
                         [{"c": c, "d": DAY.isoformat()} for c in codes])
        await db.execute(text("INSERT INTO limit_up_pool VALUES (:c,'original',:d,1,100,0)"),
                         [{"c": c, "d": DAY.isoformat()} for c in codes])
        # Cross-partition duplicate Kline overwrites original value; same-day
        # duplicate pool rows retain their emitted order and full hit denominator.
        await db.execute(text("INSERT INTO stock_kline VALUES (:c,:d,20,10,12,13)"), {"c": codes[0], "d": DAY.isoformat()})
        await db.execute(text("INSERT INTO limit_up_pool VALUES (:c,'second',:d,4,200,2)"), {"c": codes[0], "d": DAY.isoformat()})
    await db.commit()
    statements = []
    def observe(conn, cursor, sql, params, ctx, many):
        statements.append((sql, params))
    event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
    try:
        outputs, traces = [], []
        for fn in (baseline, candidate):
            statements.clear()
            outputs.append(await fn(db, codes + codes[:1], DAY))
            traces.append(list(statements))
        assert encode(outputs[0]) == encode(outputs[1])
        assert traces[0] == traces[1]
        assert list(outputs[1]) == codes
        if codes:
            assert outputs[1][codes[0]]["memory_last_limit_up_high"] == 13
            assert outputs[1][codes[0]]["memory_raw_limit_up_hits_50d"] == 2
            assert outputs[1][codes[0]]["memory_last_seal_amount"] == 100
        else:
            assert outputs == [{}, {}] and traces == [[], []]
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", observe)

@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["days", "error", "cancel"])
async def test_real_gap_helper_calendar_fallback(db, baseline, candidate, monkeypatch, fallback):
    earlier = DAY - timedelta(days=4)
    await db.execute(text("INSERT INTO limit_up_pool VALUES ('600001','no kline',:d,1,0,0)"), {"d": earlier.isoformat()})
    await db.commit()
    calls = []
    cancellation = asyncio.CancelledError("calendar cancellation")
    async def isolated_calendar(start, end):
        calls.append((start, end))
        if fallback == "error":
            raise RuntimeError("calendar unavailable")
        if fallback == "cancel":
            raise cancellation
        return [earlier + timedelta(days=1), DAY]
    monkeypatch.setattr(p.trade_calendar, "trade_days_between", isolated_calendar)
    outputs = []
    for fn in (baseline, candidate):
        if fallback == "cancel":
            with pytest.raises(asyncio.CancelledError) as exc:
                await fn(db, ["600001"], DAY)
            assert exc.value is cancellation
        else:
            outputs.append(await fn(db, ["600001"], DAY))
    assert calls == [(earlier + timedelta(days=1), DAY)] * 2
    if outputs:
        assert encode(outputs[0]) == encode(outputs[1])
        expected = 2 if fallback == "days" else p._weekday_gap(earlier, DAY)
        assert outputs[1]["600001"]["memory_days_since_last_limit_up"] == expected

@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize("kind", ["open", "open_cancel", "read_close", "cancel_close", "close_only"])
async def test_real_cursor_fault_contract(db, candidate, monkeypatch, stage, kind):
    original = db.stream
    calls, opened, closed = [], [], []
    primary = asyncio.CancelledError("primary") if "cancel" in kind else RuntimeError("primary")
    secondary = RuntimeError("cleanup")
    class FaultCursor:
        def __init__(self, cursor, fail):
            self.cursor, self.fail = cursor, fail
        async def partitions(self, size):
            async for part in self.cursor.partitions(size):
                yield part
            if self.fail and kind in ("read_close", "cancel_close"):
                raise primary
        async def close(self):
            await self.cursor.close()
            closed.append(self)
            if self.fail:
                raise secondary
    async def stream(*args, **kwargs):
        calls.append(1)
        fail = len(calls) == stage
        if fail and kind in ("open", "open_cancel"):
            raise primary
        cursor = await original(*args, **kwargs)
        wrapped = FaultCursor(cursor, fail)
        opened.append(wrapped)
        return wrapped
    monkeypatch.setattr(db, "stream", stream)
    expected = secondary if kind == "close_only" else primary
    with pytest.raises(type(expected)) as exc:
        await candidate(db, ["600001"], DAY)
    assert exc.value is expected
    assert len(calls) == stage
    assert all(item.cursor.closed for item in opened)
    assert len(closed) == len(opened)

@pytest.mark.asyncio
async def test_nonempty_codes_without_rows_remain_known_empty(db, baseline, candidate):
    codes = ["600001", "300001", "unknown"]
    old = await baseline(db, codes, DAY)
    new = await candidate(db, codes, DAY)
    assert encode(new) == encode(old)
    assert list(new) == codes
    assert all(row["memory_limit_up_hits_50d"] == 0 for row in new.values())

@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_real_nonempty_cursor_task_cancel_closes_without_partial_result(db, candidate, monkeypatch, cleanup_failure):
    await db.execute(text("INSERT INTO stock_kline VALUES ('600001',:d,10,10,11,11)"),
                     [{"d": DAY.isoformat()}] * 1025)
    await db.commit()
    original_stream = db.stream
    entered = asyncio.Event()
    hold = asyncio.Event()
    cursors = []
    rows_delivered = []
    class HeldCursor:
        def __init__(self, cursor):
            self.cursor = cursor
        async def partitions(self, size):
            async for part in self.cursor.partitions(size):
                rows_delivered.append(len(part))
                yield part
                entered.set()
                await hold.wait()
        async def close(self):
            await self.cursor.close()
            if cleanup_failure:
                raise RuntimeError("wrapper cleanup failure after real close")
    async def stream(*args, **kwargs):
        cursor = await original_stream(*args, **kwargs)
        cursors.append(cursor)
        return HeldCursor(cursor)
    monkeypatch.setattr(db, "stream", stream)
    task = asyncio.create_task(candidate(db, ["600001"], DAY))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    assert rows_delivered == [512]
    assert len(cursors) == 1 and cursors[0].closed
