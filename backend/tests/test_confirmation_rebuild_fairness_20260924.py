"""Close-first confirmation replay: exact old contract, real cursors and WAL."""
import ast
import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, insert, select, text, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.paper import strategy_iteration_shadow as s
from app.models.paper import PaperShadowEvent as E

AT = datetime(2026, 9, 24, 14, 50)
DAY = AT.date()
VERSIONS = {route: "test-rebuild-v1" for route in s.ROUTE_IDS}


@pytest.fixture
def old():
    path = Path(__file__).parent / "fixtures/confirmation_state_v3.txt"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "df06e0290495ff0e8e62bb2c71ecaff4815c0b6735f512b0a75c5fc753a4974b"
    scope = dict(vars(s))
    exec(compile(ast.parse(raw.decode()), str(path), "exec"), scope)
    return scope["rebuild"]


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'confirmation.sqlite'}",
        connect_args={"timeout": .05}, pool_size=2, max_overflow=0,
    )
    async with engine.begin() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode=WAL"))).scalar_one() == "wal"
        await conn.run_sync(E.__table__.create)
    yield engine
    await engine.dispose()


def row(key, *, code="600001", kind="confirmation_sample", at=None,
        route=s.ROUTE_C, payload=None, version=None, day=DAY):
    return dict(event_key=key, route_id=route, route_version=version or VERSIONS[route],
                trade_date=day, observed_at=at or AT - timedelta(seconds=30),
                code=code, event_type=kind, status="synthetic",
                snapshot_json=json.dumps(payload) if isinstance(payload, (dict, list))
                else payload if payload is not None
                else '{"quote":{"source_quote_at":"2026-09-24T14:49:00","avg_price":10}}')


def encode(state):
    def pack(v):
        if isinstance(v, datetime): return ["datetime", v.isoformat()]
        if isinstance(v, dict): return ["dict", [[pack(k), pack(x)] for k, x in v.items()]]
        if isinstance(v, tuple): return ["tuple", [pack(x) for x in v]]
        if isinstance(v, list): return ["list", [pack(x) for x in v]]
        if isinstance(v, set): return ["set", sorted([pack(x) for x in v], key=str)]
        return v
    return json.dumps(pack(state), separators=(",", ":"), allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 127, 128, 129, 1025])
async def test_full_state_sql_params_and_partition_boundaries(engine, old, count):
    async with AsyncSession(engine) as db:
        if count:
            await db.execute(insert(E), [row(f"row-{i}") for i in range(count)])
            await db.commit()
        traces = []
        def capture(conn, cursor, statement, params, ctx, many):
            traces.append((statement, params))
        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            states, queries = [], []
            for fn in (old, s._load_confirmation_state):
                traces.clear()
                states.append(await fn(db, DAY, AT, VERSIONS, 180))
                queries.append(list(traces))
            assert encode(states[0]) == encode(states[1])
            assert queries[0] == queries[1] and len(queries[0]) == 2
            if count:
                assert len(states[1][0][(s.ROUTE_C, "600001")]) == count
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_reset_max_source_duplicate_id_bad_json_and_C3_identity(engine, old):
    t = AT - timedelta(seconds=40)
    data = [
        row("sample", at=t),
        row("confirmed", kind="confirmed", at=t),
        row("reset1", kind="confirmation_reset", at=t + timedelta(seconds=1),
            payload={"prior_structure": {"source_boundary_at": "2026-09-24T14:48:00"}}),
        row("reset2", kind="confirmation_reset", at=t + timedelta(seconds=2), payload="{bad"),
        row("dup1", code="dup", at=t),
        row("dup2", code="dup", at=t, payload="{bad"),
        row("dup3", code="dup", at=t, payload={"quote": {
            "source_quote_at": "2026-09-24T14:48:00", "avg_price": 11}}),
        row("dup4", code="dup", at=t, payload={"quote": {
            "source_quote_at": "invalid", "avg_price": "NaN"}}),
        row("old-C3", code="old-hit", route=s.ROUTE_C3, kind="confirmed",
            at=AT - timedelta(hours=4)),
        row("old-C3-sample", code="old-hit", route=s.ROUTE_C3, at=AT - timedelta(hours=4)),
        row("edge-before", code="edge", route=s.ROUTE_C3, at=AT - timedelta(seconds=180, microseconds=1)),
        row("edge", code="edge", route=s.ROUTE_C3, at=AT - timedelta(seconds=180)),
        row("edge-now", code="edge", route=s.ROUTE_C3, at=AT),
        row("bad-day", code="excluded", kind="confirmed", day=DAY - timedelta(days=1)),
        row("bad-version", code="excluded", kind="confirmed", version="old"),
        row("future", code="excluded", kind="confirmed", at=AT + timedelta(microseconds=1)),
    ]
    # Reverse insertion while explicit ids retain semantic observed_at/id order.
    data = [dict(item, id=i + 1) for i, item in enumerate(data)]
    async with AsyncSession(engine) as db:
        await db.execute(insert(E), list(reversed(data))); await db.commit()
        before = await old(db, DAY, AT, VERSIONS, 180)
        after = await s._load_confirmation_state(db, DAY, AT, VERSIONS, 180)
    assert encode(before) == encode(after)
    samples, prices, sources, latest, confirmed = after
    key = (s.ROUTE_C, "600001")
    assert samples[key] == [] and prices[key] == {} and sources[key] == {}
    assert latest[key] == datetime(2026, 9, 24, 14, 49) and key in confirmed
    dup = (s.ROUTE_C, "dup")
    assert samples[dup] == [t] * 4 and prices[dup][t] == 11
    assert sources[dup][t] == datetime(2026, 9, 24, 14, 48)
    assert latest[dup] == datetime(2026, 9, 24, 14, 49)
    assert (s.ROUTE_C3, "old-hit") in confirmed
    assert (s.ROUTE_C3, "old-hit") not in samples
    assert samples[(s.ROUTE_C3, "edge")] == [AT - timedelta(seconds=180), AT]
    assert all(code != "excluded" for route, code in confirmed)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["open", "read", "processing", "second", "close"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("double", [False, True])
async def test_failure_is_primary_and_never_partial(engine, monkeypatch, where, cancel, double):
    primary = asyncio.CancelledError("primary") if cancel else RuntimeError("primary")
    cleanup = RuntimeError("cleanup")
    calls = []; cursor = None
    async with AsyncSession(engine) as db:
        await db.execute(insert(E), [row(f"row-{i}") for i in range(129)])
        await db.commit()
        original_json = s._json_dict
        if where == "processing":
            counter = [0]
            def fail(value):
                counter[0] += 1
                if counter[0] == 128: raise primary
                return original_json(value)
            monkeypatch.setattr(s, "_json_dict", fail)
        class Fault:
            async def stream(self, stmt):
                nonlocal cursor
                calls.append("open")
                if where == "open": raise primary
                cursor = await db.stream(stmt)
                class Wrapped:
                    async def partitions(self, n):
                        async for part in cursor.partitions(n):
                            yield part
                            if where == "read": raise primary
                    async def close(self):
                        await cursor.close(); calls.append("close")
                        if where == "close": raise primary
                        if double and where in {"read", "processing"}: raise cleanup
                return Wrapped()
            async def scalars(self, stmt):
                calls.append("second")
                assert cursor.closed
                if where == "second": raise primary
                return await db.scalars(stmt)
        with pytest.raises(type(primary)) as exc:
            await s._load_confirmation_state(Fault(), DAY, AT, VERSIONS, 180)
        assert exc.value is primary
        assert cursor is None or cursor.closed
        assert ("second" in calls) is (where == "second")


@pytest.mark.asyncio
async def test_real_task_cancel_after_128_rows_with_close_failure(engine):
    started = asyncio.Event(); cursor = None
    async with AsyncSession(engine) as db:
        await db.execute(insert(E), [row(f"row-{i}") for i in range(129)])
        await db.commit()
        class Blocking:
            async def stream(self, stmt):
                nonlocal cursor
                cursor = await db.stream(stmt)
                class Wrapped:
                    async def partitions(self, n):
                        async for part in cursor.partitions(n):
                            yield part
                            started.set()
                            await asyncio.Event().wait()
                    async def close(self):
                        await cursor.close()
                        raise RuntimeError("secondary close")
                return Wrapped()
            async def scalars(self, stmt):
                pytest.fail("cancelled replay must not issue second SELECT")
        task = asyncio.create_task(s._load_confirmation_state(Blocking(), DAY, AT, VERSIONS, 180))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert cursor.closed
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("mode", ["old", "new"])
async def test_WAL_barrier_visibility_preserves_caller_transaction(engine, old, explicit, mode):
    first = asyncio.Event(); committed = asyncio.Event(); cursor = None
    async with AsyncSession(engine) as reader, AsyncSession(engine) as writer:
        await writer.execute(insert(E), [row("base")]); await writer.commit()
        if explicit: await reader.execute(text("BEGIN"))
        async def barrier():
            first.set(); await asyncio.wait_for(committed.wait(), 2)
        class Reader:
            async def execute(self, stmt):
                result = await reader.execute(stmt); await barrier(); return result
            async def stream(self, stmt):
                nonlocal cursor
                cursor = await reader.stream(stmt)
                class Wrapped:
                    async def partitions(self, n):
                        async for part in cursor.partitions(n): yield part
                    async def close(self):
                        await cursor.close(); await barrier()
                return Wrapped()
            async def scalars(self, stmt):
                assert committed.is_set()
                if cursor is not None: assert cursor.closed
                return await reader.scalars(stmt)
        async def append():
            await asyncio.wait_for(first.wait(), 2)
            await writer.execute(insert(E), [row("between", route=s.ROUTE_C3,
                code="new-hit", kind="confirmed", at=AT - timedelta(seconds=1))])
            await writer.commit(); committed.set()
        task = asyncio.create_task(append())
        try:
            fn = old if mode == "old" else s._load_confirmation_state
            state = await asyncio.wait_for(fn(Reader(), DAY, AT, VERSIONS, 180), 4)
            await task
            assert ((s.ROUTE_C3, "new-hit") in state[-1]) is (not explicit)
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_WAL_reserved_writer_lock_does_not_block_rebuild(engine):
    async with AsyncSession(engine) as reader, AsyncSession(engine) as writer:
        await writer.execute(text("BEGIN IMMEDIATE"))
        await asyncio.wait_for(s._load_confirmation_state(reader, DAY, AT, VERSIONS, 180), 2)
        with pytest.raises(OperationalError, match="locked"):
            await reader.execute(insert(E), [row("test-only-contending-write")])
        await reader.rollback(); await writer.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_actual_scan_calls_loader_once_and_persists_only_complete_state(engine, monkeypatch, fail):
    monkeypatch.setattr(s.settings, "PAPER_STRATEGY_ITERATION_SHADOW_ENABLED", True)
    monkeypatch.setattr(s.settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", False)
    monkeypatch.setattr(s, "route_version_for", lambda route: VERSIONS[route])
    monkeypatch.setattr(s, "_capture_candidate_projection", lambda *a, **k: None)
    async def allowed(*a): return {"600001"}
    async def dates(*a): return []
    monkeypatch.setattr(s, "_allowed_codes", allowed)
    monkeypatch.setattr(s, "_previous_trade_dates", dates)
    original = s._load_confirmation_state; calls = []
    async def load(*args):
        calls.append(args)
        state = await original(*args)
        if fail: raise RuntimeError("rebuild failed")
        return state
    monkeypatch.setattr(s, "_load_confirmation_state", load)
    async with AsyncSession(engine) as db:
        await db.execute(insert(E), [row("historical-sample")]); await db.commit()
        if fail:
            with pytest.raises(RuntimeError, match="rebuild failed"):
                await s.scan_strategy_iteration_shadow(db, [], AT)
        else:
            result = await s.scan_strategy_iteration_shadow(db, [], AT)
            assert result["events"] == 1
        assert len(calls) == 1 and calls[0][1:3] == (DAY, AT)
        rows = list((await db.scalars(select(E).order_by(E.id))).all())
        assert len(rows) == (1 if fail else 2)
        if not fail:
            assert rows[-1].event_type == "confirmation_reset"
            assert rows[-1].route_version == VERSIONS[s.ROUTE_C]
