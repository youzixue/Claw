"""Timeout-path optimization: identical learning gates, bounded audit CPU, no partial success."""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from app.api.v1 import promotion as p
from app.core.prediction_data_quality import PredictionDataQualityAuditor
from app.db.session import Base
from app.models.signal import PromotionPredictionRecord as Record

DAY = date(2026, 9, 18)


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'promotion-timeout.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


async def baseline_loader(db, cutoff):
    # Exact pre-optimization selection; no network/real DB or fixture omission.
    return list((await db.scalars(select(Record).where(
        Record.outcome_status.in_(["success", "failed"]),
        Record.prediction_trade_date >= cutoff,
    ))).all())


@pytest.mark.asyncio
async def test_intraday_blob_pruning_preserves_legacy_unknown_and_learning(db, monkeypatch):
    contexts = sorted(p.PROMOTION_INTRADAY_CONTEXTS) + ["promotion_1510", "promotion_2000", "", "legacy", "unknown"]
    sources = ["schedule", "page", "legacy", "", " schedule ", "SCHEDULE"]
    for index, (source, context) in enumerate((s, c) for s in sources for c in contexts):
        for lane in (1, 2):
            for n in range(4):
                db.add(Record(
                    code=f"{index * 10+n:06d}", target_board=lane, prediction_trade_date=DAY,
                    snapshot_source=source, snapshot_context=context,
                    snapshot_recorded_at=datetime(2026, 9, 18, 20),
                    snapshot_batch_key="fixed", model_version=p.PROMOTION_MODEL_VERSION,
                    outcome_status="success" if n == 0 else "failed",
                    candidate_route="mainline_spread_start", predicted_probability=.21+n*.03,
                    actual_max_change_pct=n+1,
                    factors_json=p._json_dumps_safe({
                        "prediction_snapshot_source": "schedule",
                        "prediction_snapshot_context": "promotion_2000",
                        "promotion_event_label_version": p.PROMOTION_LABEL_VERSION,
                        "learning_eligible": n != 3,
                        "unused_payload": "x"*4096,
                    })))
    await db.commit()
    db.expunge_all()
    raw = await baseline_loader(db, DAY)
    expected_ids = sorted(r.id for r in p._latest_learning_batch_records(raw))
    excluded = sum(r.snapshot_source == "schedule" and r.snapshot_context in p.PROMOTION_INTRADAY_CONTEXTS for r in raw)
    raw_size = len(raw)
    db.expunge_all()
    loaded = []
    def on_load(target, context):
        loaded.append(target.id)
    event.listen(Record, "load", on_load)
    try:
        optimized = await p._load_promotion_learning_records(db, DAY)
    finally:
        event.remove(Record, "load", on_load)
    assert excluded > 0
    assert len(loaded) == raw_size - excluded
    assert sorted(r.id for r in p._latest_learning_batch_records(optimized)) == expected_ids
    current_stats = await p._load_promotion_learning_stats(db)
    with monkeypatch.context() as patch:
        patch.setattr(p, "_load_promotion_learning_records", baseline_loader)
        baseline_stats = await p._load_promotion_learning_stats(db)
    assert current_stats == baseline_stats
    assert current_stats, "empty statistics are not equivalence evidence"


@pytest.mark.asyncio
async def test_learning_date_status_and_canonical_fallback_are_unchanged(db):
    for index, (day, status, context) in enumerate([
        (DAY-timedelta(days=1), "success", "promotion_2000"),
        (DAY, "pending", "promotion_2000"),
        (DAY, "failed", "promotion_1510"),
        (DAY, "success", "promotion_0935"),
        (DAY+timedelta(days=1), "success", "promotion_0935"),
    ]):
        db.add(Record(code=f"{index:06d}", target_board=1, prediction_trade_date=day,
            snapshot_source="schedule", snapshot_context=context,
            snapshot_recorded_at=datetime(2026,9,18,20), snapshot_batch_key="same",
            outcome_status=status))
    await db.commit()
    rows = await p._load_promotion_learning_records(db, DAY)
    assert [r.code for r in rows] == ["000002"]
    assert [r.code for r in p._latest_learning_batch_records(rows)] == ["000002"]


class Stream:
    def __init__(self, rows, error=None, close_error=None):
        self.rows, self.error, self.closed = rows, error, False
        self.close_error = close_error
    def partitions(self, size):
        assert size == 256
        async def chunks():
            for i in range(0, len(self.rows), size):
                yield self.rows[i:i+size]
                if self.error:
                    raise self.error
        return chunks()
    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class AuditDB:
    def __init__(self, stream):
        self.rows = stream
    async def stream(self, stmt):
        assert stmt.get_execution_options()["yield_per"] == 256
        sql = str(stmt)
        assert "factors_json" not in sql and "reason_snapshot" not in sql
        return self.rows
    async def execute(self, stmt):
        return SimpleNamespace(all=lambda: [(DAY,), (date(2026,9,21),), (date(2026,9,22),)])


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_outcome_date_findings", "_snapshot_cutoff_findings"])
async def test_complete_counts_first_twenty_examples_and_cpu_cooperation(method):
    rows = [SimpleNamespace(id=i, code=f"{i:06d}", prediction_trade_date=DAY, horizon_days=1,
        outcome_trade_date=date(2026,9,22), snapshot_context="promotion_0935",
        snapshot_recorded_at=datetime(2026,9,18,9,50,0,1)) for i in range(1025)]
    stream = Stream(rows)
    done = False
    ticks = 0
    async def pulse():
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(pulse())
    try:
        found = await getattr(PredictionDataQualityAuditor(), method)(AuditDB(stream), DAY, lookback_days=120)
    finally:
        done = True
        await task
    assert ticks >= 4, "unbounded audit CPU starved the loop"
    assert stream.closed
    assert found[0].evidence["invalid_count"] == 1025
    assert [x["id"] for x in found[0].evidence["examples"]] == list(range(20))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_outcome_date_findings", "_snapshot_cutoff_findings"])
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, RuntimeError])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_failed_or_cancelled_cursor_closes_without_partial_audit(method, error_type, cleanup_fails, monkeypatch):
    row = SimpleNamespace(id=1, code="000001", prediction_trade_date=DAY, horizon_days=1,
        outcome_trade_date=date(2026,9,22), snapshot_context="promotion_0935",
        snapshot_recorded_at=datetime(2026,9,18,10))
    primary = error_type("primary")
    stream = Stream([row]*300, primary, ValueError("cleanup") if cleanup_fails else None)
    auditor = PredictionDataQualityAuditor()
    monkeypatch.setattr(auditor, "_build_watermarks", AsyncMock(return_value=[]))
    monkeypatch.setattr(auditor, "_calendar_findings", AsyncMock(return_value=[]))
    if method == "_snapshot_cutoff_findings":
        monkeypatch.setattr(auditor, "_outcome_date_findings", AsyncMock(return_value=[]))
    persist = AsyncMock()
    monkeypatch.setattr(auditor, "_persist", persist)
    with pytest.raises(error_type) as caught:
        await auditor.audit(AuditDB(stream), trade_date_value=DAY, snapshot_context="promotion_0935")
    assert caught.value is primary
    assert stream.closed
    persist.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_outcome_date_findings", "_snapshot_cutoff_findings"])
async def test_real_task_cancel_at_cpu_checkpoint_closes_cursor(method):
    row = SimpleNamespace(id=1, code="000001", prediction_trade_date=DAY, horizon_days=1,
        outcome_trade_date=date(2026,9,22), snapshot_context="promotion_0935",
        snapshot_recorded_at=datetime(2026,9,18,10))
    stream = Stream([row]*1025)
    task = asyncio.create_task(getattr(PredictionDataQualityAuditor(), method)(
        AuditDB(stream), DAY, lookback_days=120))
    asyncio.get_running_loop().call_soon(task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncio.CancelledError, RuntimeError])
async def test_learning_cleanup_cannot_replace_primary_failure(error_type):
    primary = error_type("primary")
    stream = Stream(list(range(300)), primary, ValueError("cleanup"))
    class DB:
        async def stream_scalars(self, stmt):
            return stream
    with pytest.raises(error_type) as caught:
        await p._load_promotion_learning_records(DB(), DAY)
    assert caught.value is primary
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_outcome_date_findings", "_snapshot_cutoff_findings"])
async def test_cleanup_failure_after_success_is_not_suppressed(method):
    stream = Stream([], close_error=ValueError("cleanup"))
    with pytest.raises(ValueError, match="cleanup"):
        await getattr(PredictionDataQualityAuditor(), method)(AuditDB(stream), DAY, lookback_days=120)
    assert stream.closed


# Explicit frozen v1 function is a mandatory research fixture, never a skipped
# test or a synthetic rewrite of the algorithm being checked.
def preboard_v1():
    import hashlib
    from pathlib import Path
    fixture = Path(__file__).resolve().parents[2] / "outputs/intraday_repair_20260924/preboard_v2/baseline/preboard_function.py"
    raw = fixture.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "7998d9e1a1bbc105012bf46d87cc93b5e7093877e6c16d44aa889e624d9d5fb1"
    scope = dict(vars(p))
    exec(compile(raw, str(fixture), "exec"), scope)
    return scope["_load_pre_board_probe_context_map"]


class PreboardStream(Stream):
    def __init__(self, rows, error=None, close_error=None):
        super().__init__(rows, error, close_error)
        self.partition_sizes = []
    def partitions(self, size):
        assert size == 2048
        async def chunks():
            for i in range(0, len(self.rows), size):
                self.partition_sizes.append(len(self.rows[i:i+size]))
                yield self.rows[i:i+size]
                if self.error:
                    raise self.error
        return chunks()


def latest_bar(code, *, skip=False):
    # Same scores for each stock expose the original stable tie-order.
    return (code, 10.0, 10.5, 10.7 if not skip else 9.8, 9.8,
            1000.0, 10000.0, 3.0, 5.0, 10.0)


def historical_bar(code, day=DAY-timedelta(days=1)):
    return (code, day, 10.0, 10.0, 10.2, 9.8, 800.0, 8000.0, 3.0, 0.0, 10.0)


class PreboardDB:
    def __init__(self, latest, stream, *, buffered=False):
        self.latest, self.rows, self.buffered = latest, stream, buffered
        self.queries = []
        self.name_queries = 0
    async def execute(self, stmt):
        sql = str(stmt)
        self.queries.append(sql)
        if "FROM stock_spot" in sql:
            self.name_queries += 1
            return SimpleNamespace(all=lambda: [
                (row[0], "测试", 10.0, datetime(2026,9,18,15)) for row in self.latest])
        if "stock_kline.trade_date < " in sql:
            assert self.buffered, "historical rows must stream, not execute/all"
            return SimpleNamespace(all=lambda: self.rows.rows)
        return SimpleNamespace(all=lambda: self.latest)
    async def stream(self, stmt):
        assert stmt.get_execution_options()["yield_per"] == 2048
        self.queries.append(str(stmt))
        return self.rows


def prepare_preboard(monkeypatch):
    monkeypatch.setattr(p.stock_tagger, "is_tradeable", lambda code: True)
    monkeypatch.setattr(p, "_get_exact_next_observed_market_date", AsyncMock(return_value=None))


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 2047, 2048, 2049, 4097])
async def test_preboard_stream_full_history_and_original_tie_order(monkeypatch, count):
    prepare_preboard(monkeypatch)
    codes = ["600003", "600001", "600002"]
    latest = [latest_bar(code) for code in codes]
    # A single stock crosses partition boundaries. No dropped/reordered rows.
    history = [historical_bar("600003", DAY-timedelta(days=2))] * count
    history += [historical_bar("600001"), historical_bar("600002")]
    before = PreboardDB(latest, PreboardStream(history), buffered=True)
    after = PreboardDB(latest, PreboardStream(history))
    old = await preboard_v1()(before, DAY, exclude_codes=set(), limit=2)
    new = await p._load_pre_board_probe_context_map(after, DAY, exclude_codes=set(), limit=2)
    assert old and new
    assert new == old and list(new) == list(old)
    assert before.queries == after.queries
    assert after.rows.closed
    assert all(size <= 2048 for size in after.rows.partition_sizes)
    if count == 0:
        # The equal-history stocks are tied; original input insertion wins.
        assert list(new).index("600001") < list(new).index("600002")


@pytest.mark.asyncio
async def test_preboard_all_continue_cpu_loop_yields(monkeypatch):
    prepare_preboard(monkeypatch)
    rows = PreboardStream([])
    fixture = PreboardDB([latest_bar(f"60{i:04d}", skip=True) for i in range(80)], rows, buffered=True)
    ticks = 0
    done = False
    async def pulse():
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(0)
    task = asyncio.create_task(pulse())
    try:
        assert await p._load_pre_board_probe_context_map(fixture, DAY, exclude_codes=set()) == {}
    finally:
        done = True
        await task
    assert ticks >= 5, "continue paths bypassed bounded CPU cooperation"
    assert rows.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["read", "cancel", "conversion"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_preboard_history_failure_closes_preserves_primary_and_stops(monkeypatch, failure, cleanup_fails):
    prepare_preboard(monkeypatch)
    primary = asyncio.CancelledError("primary") if failure == "cancel" else RuntimeError("primary")
    data = [historical_bar("600001")]
    if failure == "conversion":
        class BadRow:
            def __getitem__(self, index):
                raise primary
        data = [BadRow()]
    rows = PreboardStream(data, None if failure == "conversion" else primary,
                          ValueError("cleanup") if cleanup_fails else None)
    fixture = PreboardDB([latest_bar("600001")], rows)
    with pytest.raises(type(primary)) as caught:
        await p._load_pre_board_probe_context_map(fixture, DAY, exclude_codes=set())
    assert caught.value is primary
    assert rows.closed
    assert fixture.name_queries == 0


@pytest.mark.asyncio
async def test_preboard_real_cancel_on_history_checkpoint(monkeypatch):
    prepare_preboard(monkeypatch)
    rows = PreboardStream([historical_bar("600001")] * 4097)
    fixture = PreboardDB([latest_bar("600001")], rows)
    task = asyncio.create_task(p._load_pre_board_probe_context_map(fixture, DAY, exclude_codes=set()))
    asyncio.get_running_loop().call_soon(task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rows.closed and fixture.name_queries == 0


@pytest.mark.asyncio
async def test_preboard_real_cancel_at_cpu_loop_before_continue(monkeypatch):
    prepare_preboard(monkeypatch)
    rows = PreboardStream([])
    fixture = PreboardDB([latest_bar(f"60{i:04d}", skip=True) for i in range(80)], rows, buffered=True)
    task = asyncio.create_task(p._load_pre_board_probe_context_map(fixture, DAY, exclude_codes=set()))
    asyncio.get_running_loop().call_soon(task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rows.closed and fixture.name_queries == 1


@pytest.mark.asyncio
async def test_preboard_successful_read_close_failure_is_visible(monkeypatch):
    prepare_preboard(monkeypatch)
    rows = PreboardStream([], close_error=ValueError("cleanup"))
    fixture = PreboardDB([latest_bar("600001")], rows)
    with pytest.raises(ValueError, match="cleanup"):
        await p._load_pre_board_probe_context_map(fixture, DAY, exclude_codes=set())
    assert rows.closed and fixture.name_queries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [False, True])
async def test_preboard_sql_date_boundaries_missing_history_full_map_parity(db, monkeypatch, history):
    from app.models.stock import StockKline, StockSpot
    prepare_preboard(monkeypatch)
    codes = ["600003", "600001", "600002"]
    for code in codes:
        db.add(StockSpot(code=code, name="测试", price=10.5, prev_close=10))
        db.add(StockKline(code=code, trade_date=DAY, open=10, close=10.5, high=10.7,
                         low=9.8, volume=1000, amount=10000, turnover=3, change_pct=5, prev_close=10))
        if history:
            for day in (DAY-timedelta(days=211), DAY-timedelta(days=210),
                        DAY-timedelta(days=1), DAY+timedelta(days=1)):
                db.add(StockKline(code=code, trade_date=day, open=10, close=10,
                                 high=10.2, low=9.8, volume=800, amount=8000,
                                 turnover=3, change_pct=0, prev_close=10))
    await db.commit()
    statements = []
    def observe(conn, cursor, sql, params, context, many):
        if sql.lstrip().startswith("SELECT"):
            statements.append((sql, params))
    event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
    try:
        before = await preboard_v1()(db, DAY, exclude_codes=set(), limit=2)
        old_sql = list(statements); statements.clear()
        after = await p._load_pre_board_probe_context_map(db, DAY, exclude_codes=set(), limit=2)
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
    assert before and after
    assert before == after and list(before) == list(after)
    assert old_sql == statements, "SQL predicates/order/date bounds must not change"
    history_query = next((sql, params) for sql, params in statements if "stock_kline.trade_date < " in sql)
    assert str(DAY-timedelta(days=210)) in history_query[1]
    assert str(DAY) in history_query[1]
