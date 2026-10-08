"""Forward scheduler-event diagnostics: temporary DB, no real schedule/network."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from apscheduler.events import (JobExecutionEvent, JobSubmissionEvent, EVENT_JOB_EXECUTED,
    EVENT_JOB_ERROR, EVENT_JOB_MISSED, EVENT_JOB_MAX_INSTANCES)
from sqlalchemy import func, select, event as sql_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data import scheduler as module
from app.data.after_hours import scheduler_event_receipt, append_scheduler_event_receipt
from app.data.sources import after_hours_source as adapter
from app.models.review import ReviewAutomationRun
from app.models.stock import StockAfterHoursObservation, StockTag
from app.models.governance import TradeCalendarModel
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from test_after_hours_research_20261002 import db, DAY, sse, collector_http_no_network

SLOT = datetime(2026, 9, 30, 15, 35)
OBSERVED = SLOT + timedelta(minutes=1)


def terminal(job="after_hours_research_1535", *, code=EVENT_JOB_EXECUTED,
             scheduled=SLOT, result=None, exception=None):
    # Real APScheduler event shape, not a claim that the scheduler naturally ran.
    return JobExecutionEvent(code, job, "default",
        scheduled.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc),
        retval=result, exception=exception)


def summary(at=SLOT, **changes):
    value = {"status": "observed", "selected_codes": 1, "inserted": 1,
        "status_counts": {"observed": 1}, "committed": True,
        "started_at": at.isoformat(), "completed_at": (at + timedelta(seconds=1)).isoformat()}
    value.update(changes)
    return value


def details(record):
    return json.loads(record["details_json"])


@pytest.mark.parametrize("job,at", [
    ("after_hours_regular_baseline", SLOT.replace(minute=1)),
    ("after_hours_research_1535", SLOT),
    ("after_hours_research_2010", SLOT.replace(hour=20, minute=10)),
])
def test_actual_event_shape_has_own_clock_and_never_certifies_source_or_nature(job, at):
    record = scheduler_event_receipt(terminal(job, scheduled=at, result=summary(at)),
        observed_at=at+timedelta(minutes=1))
    material = details(record)
    assert record["review_date"] == DAY
    assert record["started_at"] == record["completed_at"] == at+timedelta(minutes=1)
    assert material["handler_started_at"] == at.isoformat()
    assert material["capture_summary_recorded"] is True
    assert material["natural_acceptance_verified"] is False
    assert material["source_first_availability_certified"] is False
    assert material["historical_pit"] is material["execution_authorized"] is False


@pytest.mark.parametrize("damage", [
    {"committed": "true"}, {"selected_codes": True}, {"inserted": 1.0},
    {"status_counts": {"observed": True}}, {"status_counts": {"unexpected": 1}},
    {"status_counts": None}, {"inserted": 2}, {"selected_codes": 10001},
    {"started_at": "2026-09-30"}, {"completed_at": "2026-09-30T15:37:00"},
    {"started_at": "2026-09-30T15:34:59"}, {"status": "failed"},
    {"status": "disabled"}, {"status": "blocked"}, {"status": "busy"},
])
def test_python_return_is_not_accepted_capture_on_bad_or_blocked_summary(damage):
    record = scheduler_event_receipt(terminal(result=summary(**damage)), observed_at=OBSERVED)
    assert details(record)["capture_summary_recorded"] is False
    assert details(record)["natural_acceptance_verified"] is False


@pytest.mark.parametrize("code,status", [(EVENT_JOB_ERROR,"error"), (EVENT_JOB_MISSED,"missed")])
def test_failure_and_miss_are_not_zero_observation_success(code,status):
    record = scheduler_event_receipt(terminal(code=code, result=summary(),
        exception=RuntimeError("secret " * 100000)), observed_at=OBSERVED)
    assert record["status"] == status
    assert details(record)["capture_summary_recorded"] is False
    assert "secret" not in record["details_json"] and len(record["details_json"].encode()) <= 8192


def test_max_instances_is_aggregated_bounded_diagnostic_not_handler_run():
    e = JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, "after_hours_research_1535", "default", [SLOT])
    record = scheduler_event_receipt(e, observed_at=OBSERVED)
    assert record["status"] == "max_instances" and details(record)["scheduled_time_count"] == 1
    assert details(record)["handler_started_at"] is None
    assert details(record)["capture_summary_recorded"] is False
    e.scheduled_run_times *= 1001
    with pytest.raises(ValueError, match="times budget"):
        scheduler_event_receipt(e, observed_at=OBSERVED)


def test_frozen_scalars_ignore_large_retval_and_authority_booleans():
    result = summary(natural_acceptance_verified=True, execution_authorized=True,
        text="credentials " * 300000)
    e = terminal(result=result)
    record = scheduler_event_receipt(e, observed_at=OBSERVED)
    result["status_counts"]["observed"] = 9999
    result["started_at"] = "future"
    assert details(record)["status_counts"] == {"observed": 1}
    assert details(record)["execution_authorized"] is False
    assert "credentials" not in record["details_json"]


@pytest.mark.parametrize("clock", [SLOT-timedelta(seconds=1), SLOT.replace(tzinfo=timezone.utc), None])
def test_receipt_observation_clock_must_be_naive_and_not_before_scheduled(clock):
    with pytest.raises(ValueError):
        scheduler_event_receipt(terminal(result=summary()), observed_at=clock)


@pytest.mark.asyncio
async def test_dedup_preserves_first_event_clock_and_never_creates_storage(db):
    e = terminal(result=summary())
    first = scheduler_event_receipt(e, observed_at=OBSERVED)
    statements = []
    def inspect(conn, cursor, text, *args):
        statements.append(text)
    sql_event.listen(db.bind.sync_engine, "before_cursor_execute", inspect)
    try:
        assert (await append_scheduler_event_receipt(db, first))["status"] == "recorded"
        await db.commit()
        repeat = scheduler_event_receipt(e, observed_at=OBSERVED+timedelta(hours=1))
        assert (await append_scheduler_event_receipt(db, repeat))["status"] == "duplicate"
        await db.commit()
    finally:
        sql_event.remove(db.bind.sync_engine, "before_cursor_execute", inspect)
    rows = (await db.scalars(select(ReviewAutomationRun))).all()
    assert len(rows) == 1 and rows[0].started_at == OBSERVED
    assert rows[0].details_json == first["details_json"]
    assert all(not text.lstrip().upper().startswith(("CREATE", "DROP", "UPDATE", "DELETE")) for text in statements)


async def setup_collector(db, monkeypatch, clock):
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True, session_type="full"))
    db.add(StockTag(code="600000", name="fixture", board_type="main_sh", board_tag="tradeable"))
    await db.commit()
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr(adapter, "local_now", lambda: clock[0])
    calls = []
    async def collect(self, code, **kwargs):
        calls.append(code)
        return adapter.parse_official(sse(time=153000), code=code, day=DAY, received_at=clock[0])
    monkeypatch.setattr(adapter.AfterHoursSource, "collect", collect)
    return module.DataScheduler(), calls


async def drain(service):
    tasks = tuple(service._after_hours_receipt_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    assert not service._after_hours_receipt_tasks


@pytest.mark.asyncio
async def test_recheck_with_zero_new_content_has_separate_actual_event_record(
        db, monkeypatch, collector_http_no_network):
    clock = [SLOT]
    service, calls = await setup_collector(db, monkeypatch, clock)
    first = await service._collect_after_hours_research()
    assert first["inserted"] == 1 and first["committed"] is True
    service._on_scheduler_job_event(terminal(result=first))
    await drain(service)
    observation = await db.scalar(select(StockAfterHoursObservation))
    old_values = (observation.content_hash, observation.available_at, observation.received_at)
    clock[0] = SLOT.replace(hour=20,minute=10)
    second = await service._collect_after_hours_research()
    assert second["inserted"] == 0
    service._on_scheduler_job_event(terminal("after_hours_research_2010", scheduled=clock[0], result=second))
    await drain(service)
    rows = list((await db.scalars(select(ReviewAutomationRun).order_by(ReviewAutomationRun.id))).all())
    assert len(rows) == 2 and len({r.run_key for r in rows}) == 2
    assert [json.loads(r.details_json)["capture_summary_recorded"] for r in rows] == [True,True]
    assert [json.loads(r.details_json)["inserted"] for r in rows] == [1,0]
    await db.refresh(observation)
    assert (observation.content_hash, observation.available_at, observation.received_at) == old_values
    assert calls == ["600000","600000"]
    for model in (PaperAccount,PaperTradeLog,TradeOrder,TradeFill):
        assert await db.scalar(select(func.count()).select_from(model)) == 0
    assert service._after_hours_receipt_health["commit_ack_returned"] is True


@pytest.mark.asyncio
async def test_commit_ack_loss_is_unknown_without_recollection_or_row_release(
        db, monkeypatch, collector_http_no_network):
    clock = [SLOT]
    service, calls = await setup_collector(db, monkeypatch, clock)
    result = await service._collect_after_hours_research()
    real_commit = AsyncSession.commit
    async def lose_ack(session):
        await real_commit(session)
        raise ConnectionError("fixture lost ACK; sensitive URI is not retained")
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", lose_ack)
        service._on_scheduler_job_event(terminal(result=result))
        await drain(service)
    assert service._after_hours_receipt_health["status"] == "unknown"
    assert service._after_hours_receipt_health["error_type"] == "ConnectionError"
    assert calls == ["600000"]
    assert await db.scalar(select(func.count()).select_from(ReviewAutomationRun)) == 1
    assert await db.scalar(select(func.count()).select_from(StockAfterHoursObservation)) == 1
    service._on_scheduler_job_event(terminal(result=result))
    await drain(service)
    assert service._after_hours_receipt_health["status"] == "duplicate"
    assert calls == ["600000"]


@pytest.mark.asyncio
async def test_listener_task_budget_and_other_jobs_are_isolated(db, monkeypatch):
    monkeypatch.setattr(adapter,"local_now",lambda:OBSERVED)
    service = module.DataScheduler()
    gate = asyncio.Event()
    async def delayed(record):
        await gate.wait()
    monkeypatch.setattr(service, "_persist_after_hours_job_event", delayed)
    service._on_scheduler_job_event(terminal("tencent_spot",result=summary()))
    assert not service._after_hours_receipt_tasks
    for _ in range(17):
        service._on_scheduler_job_event(terminal(result=summary()))
    assert len(service._after_hours_receipt_tasks) == 16
    assert service._after_hours_receipt_health["status"] == "dropped"
    gate.set()
    await drain(service)
    assert await db.scalar(select(func.count()).select_from(ReviewAutomationRun)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"inserted": 0}, {"status": "failed"}])
async def test_changed_redelivery_keeps_first_event_and_marks_conflict(db, change):
    first = scheduler_event_receipt(terminal(result=summary()), observed_at=OBSERVED)
    await append_scheduler_event_receipt(db, first)
    await db.commit()
    changed = scheduler_event_receipt(terminal(result=summary(**change)),
        observed_at=OBSERVED+timedelta(seconds=1))
    assert changed["run_key"] == first["run_key"]
    assert (await append_scheduler_event_receipt(db, changed))["status"] == "conflict"
    await db.commit()
    rows = (await db.scalars(select(ReviewAutomationRun))).all()
    assert len(rows) == 1
    assert rows[0].details_json == first["details_json"] and rows[0].started_at == OBSERVED


@pytest.mark.asyncio
async def test_stop_cancels_not_yet_started_receipt_without_leaving_pending(db, monkeypatch):
    from app.news import engine as news_module
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr(adapter, "local_now", lambda:OBSERVED)
    monkeypatch.setattr(news_module, "news_engine", SimpleNamespace(cancel_pending_fetches=lambda:None))
    service = module.DataScheduler()
    service._process_awake_guard = SimpleNamespace(stop=lambda:None)
    service._on_scheduler_job_event(terminal(result=summary()))
    tasks = tuple(service._after_hours_receipt_tasks)
    assert len(tasks) == 1 and service._after_hours_receipt_health["status"] == "pending"
    service.stop()  # No await before cancellation: writer has not started.
    await asyncio.gather(*tasks, return_exceptions=True)
    assert not service._after_hours_receipt_tasks
    assert service._after_hours_receipt_health["status"] == "unknown"
    assert service._after_hours_receipt_health["commit_ack_returned"] is False
    assert await db.scalar(select(func.count()).select_from(ReviewAutomationRun)) == 0


@pytest.mark.asyncio
async def test_empty_universe_records_event_without_claiming_handler_commit_ack(
        db, monkeypatch, collector_http_no_network):
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True, session_type="full"))
    await db.commit()
    monkeypatch.setattr(module, "async_session", async_sessionmaker(db.bind, expire_on_commit=False))
    monkeypatch.setattr(adapter, "local_now", lambda:SLOT)
    service = module.DataScheduler()
    async def forbidden_commit(session):
        raise AssertionError("empty handler must not manufacture a commit ACK")
    async def forbidden_collect(*args, **kwargs):
        raise AssertionError("empty universe must not collect sources")
    monkeypatch.setattr(adapter.AfterHoursSource, "collect", forbidden_collect)
    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", forbidden_commit)
        result = await service._collect_after_hours_research()
    assert result["status"] == "unavailable" and result["selected_codes"] == result["inserted"] == 0
    assert result["committed"] is False
    service._on_scheduler_job_event(terminal(result=result))
    await drain(service)
    row = await db.scalar(select(ReviewAutomationRun))
    material = json.loads(row.details_json)
    assert material["handler_commit_ack_reported"] is False
    assert material["capture_summary_recorded"] is False
    assert service._after_hours_receipt_health["commit_ack_returned"] is True
    assert await db.scalar(select(func.count()).select_from(StockAfterHoursObservation)) == 0
