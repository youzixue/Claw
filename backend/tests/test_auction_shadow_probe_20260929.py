"""Offline safety checks for the natural-window diagnostic; never production/network."""
import asyncio
import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.governance import TradeCalendarModel
from app.models.stock import AuctionData, StockKline, StockTag

PATH = Path(__file__).resolve().parents[2] / "outputs/auction_source_capability_20260929/verify_sina_evidence_live.py"
spec = importlib.util.spec_from_file_location("auction_shadow_probe", PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
DAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 9, 16, 30)


@pytest_asyncio.fixture
async def source_db(tmp_path):
    path = tmp_path / "source.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine)() as db:
            db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
            db.add_all([StockTag(code=c, board_type="main_sh", board_tag="tradeable")
                        for c in ("600001", "600002")])
            db.add_all([StockKline(code="600001", trade_date=DAY-timedelta(days=n), volume=v)
                        for n, v in enumerate((101, 102, 103, 105, 106), 1)])
            await db.commit()
    finally:
        await engine.dispose()
    return path


def set_clock(monkeypatch, value=NOW):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return value
    monkeypatch.setattr(probe, "datetime", Clock)


def evidence(code="600001", source="sina", **changes):
    fields = dict(
        code=code, trade_date=DAY, auction_time=NOW.strftime("%H:%M:%S"),
        auction_price=10.5, prev_close=10, auction_volume=10000, auction_amount=105000,
        volume_ratio=2.0, source=source, source_version="isolated_fixture",
        source_quote_at=NOW-timedelta(seconds=3), received_at=NOW, observed_at=NOW,
        price_basis="indicative_match", volume_basis="indicative_matched",
        volume_unit="share", amount_unit="CNY",
    )
    fields.update(changes)
    return AuctionData(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (None, RuntimeError, asyncio.CancelledError))
async def test_shadow_never_seeds_fake_history_freezes_now_or_leaks_temp_db(
    source_db, tmp_path, monkeypatch, failure,
):
    set_clock(monkeypatch)
    paths = []

    async def collect(self, db, day, *, codes):
        # Signature deliberately has no now argument; live probe must not freeze it.
        paths.append(Path(db.bind.url.database))
        assert paths[-1] != source_db
        assert await db.scalar(select(func.count()).select_from(StockKline)) == 0
        means = await self._load_average_daily_volume(db, codes, day)
        assert means == {"600001": 103.4}, "no integer mean truncation or fabricated daily rows"
        db.add(evidence())
        await db.commit()
        if failure:
            raise failure("isolated failure after partial commit")
        return {"status": "ok", "written": 1}

    monkeypatch.setattr(probe.AuctionCollector, "collect_sina_auction_evidence", collect)
    dest = tmp_path / "result"
    if failure:
        with pytest.raises(failure):
            await probe.verify(source_db, dest, source="sina")
        payload = json.loads((dest / "live_verification.json").read_text())
        assert failure.__name__ in payload["error"]
        assert failure.__name__ in payload["rounds"][0]["error"]
        assert payload["rows_written_to_temp_db"] == 1
        assert payload["missing_verified_codes"] == ["600002"]
    else:
        payload = await probe.verify(source_db, dest, source="sina")
        assert payload["full_universe_count"] == 2
        assert payload["evidence_status_counts"] == {"ok": 1}
        assert payload["missing_verified_codes"] == ["600002"]
        assert payload["per_code_distinct_clocks"] == {"600001": 1, "600002": 0}
    assert paths and all(not p.exists() for p in paths)
    assert payload["production_db_written"] is False
    assert payload["natural_forward_accepted"] is False
    assert payload["source_unchanged"] is True
    ro = probe.readonly_engine(source_db)
    try:
        async with ro.connect() as con:
            assert await con.scalar(select(func.count()).select_from(AuctionData)) == 0
            assert await con.scalar(select(func.count()).select_from(StockKline)) == 5
    finally:
        await ro.dispose()


@pytest.mark.asyncio
async def test_report_is_exclusive_and_outside_real_window_never_opens_db(tmp_path, monkeypatch):
    dest = tmp_path / "result"
    dest.mkdir()
    report = dest / "live_verification.json"
    report.write_text("frozen evidence")
    set_clock(monkeypatch)
    with pytest.raises(FileExistsError):
        await probe.verify(tmp_path / "missing.db", dest)
    assert report.read_text() == "frozen evidence"
    set_clock(monkeypatch, datetime(2026, 9, 29, 12))
    with pytest.raises(ValueError, match="outside real source window"):
        await probe.verify(tmp_path / "missing.db", tmp_path / "unused")
    assert not (tmp_path / "missing.db").exists()
    assert not (tmp_path / "unused").exists()


@pytest.mark.asyncio
async def test_readonly_engine_refuses_writes(source_db):
    engine = probe.readonly_engine(source_db)
    try:
        async with engine.begin() as connection:
            assert await connection.scalar(text("PRAGMA query_only")) == 1
            with pytest.raises(OperationalError, match="readonly"):
                await connection.execute(text("DELETE FROM stock_kline"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_calendar_stops_before_source_request(source_db, tmp_path, monkeypatch):
    # Natural next-day clock, but fixture intentionally lacks that calendar date.
    set_clock(monkeypatch, NOW+timedelta(days=1))
    collect = AsyncMock(side_effect=AssertionError("must not request source"))
    monkeypatch.setattr(probe.AuctionCollector, "collect_sina_auction_evidence", collect)
    with pytest.raises(ValueError, match="calendar unknown/closed"):
        await probe.verify(source_db, tmp_path / "unknown")
    collect.assert_not_awaited()
    payload = json.loads((tmp_path / "unknown/live_verification.json").read_text())
    assert "error" in payload and payload["natural_forward_accepted"] is False


def test_summary_checks_every_row_and_deduplicates_cross_source_clock():
    rows = [evidence(code=f"60{n:04d}") for n in range(201)]
    rows += [evidence(code="600000", source="tencent"),
             evidence(code="600201", received_at=None)]
    codes = [f"60{n:04d}" for n in range(203)]
    result = probe.summarize(rows, decision=NOW, full_universe=2995, sampled_codes=codes)
    assert result["evidence_status_counts"] == {"ok": 202, "unknown": 1}
    assert result["full_universe_count"] == 2995
    assert result["sampled_count"] == 203
    assert result["per_code_distinct_clocks"]["600000"] == 1
    assert result["codes_with_two_distinct_clocks"] == 0
    assert result["missing_verified_codes"] == ["600201", "600202"]
    assert len(result["evidence_rows"]) == 203


def test_combined_uses_only_source_registrations_and_preserves_terminal_seconds():
    plan = probe.shadow_schedule(DAY)
    assert set(plan) == set(probe.SHADOW_JOBS)
    assert len(plan["auction_evidence_early"]["planned_ticks"]) == 20
    assert plan["auction_evidence_early"]["sources"] == ("tencent_early", "sina")
    for key, seconds in (("auction_evidence_0925", [6, 20, 28]),
                         ("auction_evidence_eastmoney_0925", [4, 20])):
        item = plan[key]
        assert [datetime.fromisoformat(t).second for t in item["planned_ticks"]] == seconds
        assert item["options"] == dict(id=key, coalesce=True, max_instances=1, misfire_grace_time=5)


@pytest.mark.parametrize("change", ("callback", "trigger", "missing", "option"))
def test_unknown_registration_fails_closed(tmp_path, monkeypatch, change):
    path = tmp_path / "app/data/scheduler.py"
    path.parent.mkdir(parents=True)
    original = (probe.BACKEND / "app/data/scheduler.py").read_text()
    if change == "callback":
        original = original.replace("self._auction_collect_tencent_evidence,", "self._intraday_fast,", 1)
    elif change == "trigger":
        original = original.replace('second="6,20,28"', 'second=dynamic_seconds', 1)
    elif change == "missing":
        original = original.replace('id="auction_evidence_0925"', 'id="renamed_job"', 1)
    else:
        original = original.replace('id="auction_evidence_0925",', 'id="auction_evidence_0925", executor="different",', 1)
    path.write_text(original)
    monkeypatch.setattr(probe, "BACKEND", tmp_path)
    with pytest.raises(ValueError):
        probe.shadow_schedule(DAY)


def test_terminal_dedup_and_no_ratio_path_counts_are_not_interchangeable():
    final_at = NOW.replace(minute=25, second=10)
    source_clock = final_at - timedelta(seconds=5)
    def final(source, **changes):
        return evidence(source=source, auction_time=final_at.strftime("%H:%M:%S"),
                        source_quote_at=source_clock, received_at=final_at, observed_at=final_at,
                        price_basis="auction_opening", volume_basis="auction_matched", **changes)
    rows = [evidence(), final("tencent"), final("eastmoney"),
            final("sina", volume_ratio=None)]
    result = probe.summarize(rows, decision=final_at, full_universe=2995,
                             sampled_codes=["600001", "600002"])
    assert result["per_code_distinct_clocks"] == {"600001": 2, "600002": 0}
    assert result["terminal_distinct_clocks"] == {"600001": 1, "600002": 0}
    assert result["terminal_same_source_clocks"]["600001"] == {"tencent": 1, "eastmoney": 1}
    assert result["path_bucket_distinct_clocks_without_ratio"]["600001"] == dict(early=1, middle=0, final=1)
    assert result["evidence_status_counts"] == {"ok": 3, "incomplete_values": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", (None, RuntimeError, asyncio.CancelledError))
async def test_combined_real_scheduler_shared_db_failure_and_cancel_cleanup(
    source_db, tmp_path, monkeypatch, failure,
):
    from apscheduler.triggers.date import DateTrigger
    # Real APScheduler/executor, only fire times compressed. All clocks/rows are
    # synthetic offline fixtures, never accepted as natural-window evidence.
    clock = [NOW]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(probe, "datetime", Clock)
    original_plan = probe.shadow_schedule(DAY)
    def compressed_plan(day):
        # Construct after temp schema/tag setup; fixture setup is not scheduling lag.
        real_now = datetime.now(probe.SHANGHAI)
        for job_id, item in original_plan.items():
            delay = 0.02 if job_id == "auction_evidence_early" else 0.75
            at = real_now + timedelta(seconds=delay)
            item["trigger"] = DateTrigger(run_date=at)
            item["planned_ticks"] = [at.isoformat()]
        return original_plan
    monkeypatch.setattr(probe, "shadow_schedule", compressed_plan)
    early_done, tencent_started, eastmoney_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    paths, sessions, order = [], [], []

    async def save(db, source, final=False):
        paths.append(Path(db.bind.url.database))
        sessions.append(db)
        assert paths[-1] != source_db
        assert await db.scalar(select(func.count()).select_from(StockKline)) == 0
        assert await db.scalar(select(func.count()).select_from(StockTag)) == 2
        if final:
            at = NOW.replace(minute=25, second=10)
            row = evidence(source=source, auction_time=at.strftime("%H:%M:%S"),
                           source_quote_at=at-timedelta(seconds=5), received_at=at, observed_at=at,
                           price_basis="auction_opening", volume_basis="auction_matched")
        else:
            row = evidence(source=source)
        db.add(row)
        await db.commit()
        order.append(source)
        return {"status": "ok", "written": 1}

    async def early_t(self, db, day, *, codes):
        return await save(db, "tencent")
    async def early_s(self, db, day, *, codes):
        result = await save(db, "sina")
        clock[0] = NOW.replace(minute=25, second=10)
        early_done.set()
        return result
    async def final_t(self, db, day, *, codes):
        assert early_done.is_set()
        tencent_started.set()
        await eastmoney_done.wait()
        if failure is asyncio.CancelledError:
            await asyncio.Event().wait()
        if failure:
            raise failure("isolated tencent failure")
        return await save(db, "tencent", final=True)
    async def final_e(self, db, day, *, codes):
        await tencent_started.wait()
        result = await save(db, "eastmoney", final=True)
        eastmoney_done.set()
        return result
    async def close(day):
        await asyncio.wait_for(eastmoney_done.wait(), 3)
        if failure is asyncio.CancelledError:
            raise failure()
    monkeypatch.setattr(probe, "wait_for_shadow_close", close)
    for name, method in (("tencent_early", early_t), ("sina", early_s),
                         ("tencent_final", final_t), ("eastmoney_final", final_e)):
        monkeypatch.setattr(probe.AuctionCollector, probe.METHODS[name], method)
    dest = tmp_path / "combined"
    if failure is asyncio.CancelledError:
        with pytest.raises(failure):
            await probe.verify(source_db, dest, source="combined", limit=1)
        payload = json.loads((dest / "live_verification.json").read_text())
    else:
        payload = await probe.verify(source_db, dest, source="combined", limit=1)
    assert order[:2] == ["tencent", "sina"]
    assert "eastmoney" in order
    assert len({id(db) for db in sessions}) == len(sessions)
    assert len(set(paths)) == 1 and not paths[0].exists()
    assert payload["full_universe_count"] == 2 and payload["sampled_count"] == 1
    assert payload["rows_written_to_temp_db"] == (4 if failure is None else 3)
    assert payload["terminal_distinct_clocks"] == {"600001": 1}
    health = payload["collector_snapshot_health"]
    assert health["tradeable_universe_count"] == 2
    assert health["multi_frame_complete_count"] == 1  # whole window, NOT two terminal frames
    assert payload["natural_forward_accepted"] is False
    assert payload["production_db_written"] is False
    if failure:
        assert any(failure.__name__ in entry.get("error", "") for entry in payload["rounds"])
    ro = probe.readonly_engine(source_db)
    try:
        async with ro.connect() as con:
            assert await con.scalar(select(func.count()).select_from(AuctionData)) == 0
    finally:
        await ro.dispose()


@pytest.mark.asyncio
async def test_combined_rejects_synthetic_controls_and_postwindow_without_db(tmp_path, monkeypatch):
    set_clock(monkeypatch)
    with pytest.raises(ValueError, match="single-source only"):
        await probe.verify(tmp_path / "never.db", tmp_path / "never", source="combined", rounds=2)
    set_clock(monkeypatch, NOW.replace(hour=12))
    with pytest.raises(ValueError, match="outside real source window"):
        await probe.verify(tmp_path / "never.db", tmp_path / "never", source="combined")
    assert not (tmp_path / "never.db").exists()
    assert not (tmp_path / "never").exists()


@pytest.mark.asyncio
async def test_combined_records_max_instances_without_blocking_other_source(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_EXECUTED
    from apscheduler.triggers.interval import IntervalTrigger
    set_clock(monkeypatch, NOW.replace(minute=25, second=10))
    plan = probe.shadow_schedule(DAY)
    plan.pop("auction_evidence_early")
    start = datetime.now(probe.SHANGHAI) + timedelta(seconds=0.05)
    for item in plan.values():
        item["trigger"] = IntervalTrigger(seconds=0.04, start_date=start,
                                         end_date=start+timedelta(seconds=0.24))
        item["planned_ticks"] = [(start+timedelta(seconds=0.04*n)).isoformat() for n in range(7)]
    monkeypatch.setattr(probe, "shadow_schedule", lambda day: plan)
    started, release = asyncio.Event(), asyncio.Event()
    entered = []
    @asynccontextmanager
    async def factory():
        yield object()
    async def tencent(db, day, *, codes):
        entered.append("tencent")
        started.set()
        await release.wait()
        return {"written": 0}
    async def eastmoney(db, day, *, codes):
        entered.append("eastmoney")
        return {"written": 0}
    async def close(day):
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.sleep(0.15)
        release.set()
    monkeypatch.setattr(probe, "wait_for_shadow_close", close)
    collector = SimpleNamespace(collect_tencent_auction_evidence=tencent,
                                collect_eastmoney_auction_evidence=eastmoney)
    payload = {"rounds": []}
    await probe.run_combined(factory, collector, ["600001"], DAY, payload)
    assert entered.count("tencent") == 1
    assert entered.count("eastmoney") >= 2
    assert any(e["event_code"] == EVENT_JOB_MAX_INSTANCES
               and e["job_id"] == "auction_evidence_0925" for e in payload["schedule_events"])
    executions = [e for e in payload["schedule_events"] if e["event_code"] == EVENT_JOB_EXECUTED]
    assert all(e["round_indices"] for e in executions)
    assert all(0 <= i < len(payload["rounds"]) for e in executions for i in e["round_indices"])


@pytest.mark.asyncio
async def test_combined_late_wakeup_never_calls_sources(monkeypatch):
    from apscheduler.triggers.date import DateTrigger
    from types import SimpleNamespace
    set_clock(monkeypatch, NOW.replace(minute=25, second=31))
    plan = probe.shadow_schedule(DAY)
    at = datetime.now(probe.SHANGHAI) + timedelta(seconds=0.01)
    for item in plan.values():
        item["trigger"] = DateTrigger(run_date=at)
        item["planned_ticks"] = [at.isoformat()]
    monkeypatch.setattr(probe, "shadow_schedule", lambda day: plan)
    async def close(day):
        await asyncio.sleep(0.1)
    monkeypatch.setattr(probe, "wait_for_shadow_close", close)
    methods = {name: AsyncMock(side_effect=AssertionError("late source request")) for name in probe.METHODS.values()}
    payload = {"rounds": []}
    def forbidden_factory():
        raise AssertionError("late session opened")
    await probe.run_combined(forbidden_factory, SimpleNamespace(**methods), ["600001"], DAY, payload)
    assert len(payload["rounds"]) == 4
    assert all(r["status"] == "window_closed" for r in payload["rounds"])
    for method in methods.values():
        method.assert_not_awaited()

