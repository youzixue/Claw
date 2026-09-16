import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from apscheduler.events import (
    EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED,
    JobExecutionEvent,
)

from app.data.pipeline_runtime import PipelineRuntimeHealth


DAY = datetime(2026, 9, 14, 10)


def monitor(start=DAY):
    result = PipelineRuntimeHealth(started_at=start)
    result.observe_calendar(start.date(), True)
    return result


def quote(state, at, previous=None, quality="ok", visible=None, round_id="qr-test"):
    return state.observe_quote({
        "committed_at": at, "round_id": round_id,
        "quality_status": quality, "quality_reason": "",
        "code_version": "code-test", "config_version": "config-test",
        "records": [{"secret": "must-not-retain"}],
    }, previous_committed_at=previous, visible_at=visible or at)


def test_gap_remains_visible_after_fresh_rounds():
    state = monitor()
    quote(state, DAY, DAY - timedelta(seconds=306))
    now = DAY + timedelta(seconds=30)
    quote(state, now, DAY)
    result = state.snapshot(now=now + timedelta(seconds=2))
    assert result["status"] == "degraded"
    assert result["quote"]["status"] == "fresh"
    assert result["quote"]["gap_events_today"][0]["active_gap_sec"] == 306
    assert result["quote"]["last_round"]["intercommit_continuity"]["status"] == "continuous"
    assert result["historical_day_completeness"] == "not_certified"
    assert result["execution_gates_changed"] is False
    assert "secret" not in str(result)


@pytest.mark.parametrize("seconds,expected", [(0, "fresh"), (90, "fresh"), (91, "stale"), (306, "stale")])
def test_staleness_does_not_require_another_successful_commit(seconds, expected):
    state = monitor()
    quote(state, DAY)
    assert state.snapshot(now=DAY + timedelta(seconds=seconds))["quote"]["status"] == expected


def test_missing_data_and_restart_history_are_not_falsely_healthy():
    state = monitor()
    assert state.snapshot(now=DAY)["quote"]["status"] == "awaiting_first_round"
    result = state.snapshot(now=DAY + timedelta(seconds=91))
    assert result["quote"]["status"] == "missing"
    assert result["status"] == "degraded"
    assert result["scope"] == "current_process_only"


def test_unknown_calendar_does_not_certify_day_or_trigger_io():
    state = PipelineRuntimeHealth(started_at=DAY)
    result = state.snapshot(now=DAY + timedelta(minutes=5))
    assert result["status"] == "unknown"
    assert result["quote"]["expected_now"] is None
    state.observe_calendar(DAY.date(), False)
    assert state.snapshot(now=DAY)["quote"]["status"] == "not_expected"


@pytest.mark.parametrize("now", [
    DAY.replace(hour=12), DAY.replace(hour=15, minute=1),
    datetime(2026, 9, 19, 10), datetime(2026, 10, 1, 10),
])
def test_scheduled_closed_windows_are_not_new_tail_alerts(now):
    state = monitor()
    quote(state, DAY)
    result = state.snapshot(now=now)
    assert result["quote"]["status"] == "not_expected"
    assert result["quote"]["gap_events_today"] == []


def test_lunch_break_excluded_but_trading_missing_minutes_not_excluded():
    state = monitor()
    before = DAY.replace(hour=11, minute=29, second=40)
    after = DAY.replace(hour=13, minute=0, second=30)
    quote(state, before)
    result = state.snapshot(now=after)
    assert result["quote"]["status"] == "fresh"
    assert result["quote"]["active_tail_sec"] == 50
    assert state.snapshot(now=after + timedelta(seconds=41))["quote"]["status"] == "stale"


def test_start_during_lunch_counts_only_active_seconds_and_preserves_missing_morning():
    state = monitor(DAY.replace(hour=12))
    assert state.snapshot(now=DAY.replace(hour=13, second=30))["quote"]["status"] == "awaiting_first_round"
    assert state.snapshot(now=DAY.replace(hour=13, minute=2))["quote"]["status"] == "missing"
    state = monitor(DAY.replace(hour=9, minute=14))
    assert state.snapshot(now=DAY.replace(hour=13, second=1))["quote"]["status"] == "missing"


def test_new_day_does_not_reuse_yesterday_as_a_fresh_round():
    state = monitor()
    quote(state, DAY, DAY - timedelta(seconds=300))
    tomorrow = DAY + timedelta(days=1)
    state.observe_calendar(tomorrow.date(), True)
    result = state.snapshot(now=tomorrow)
    assert result["quote"]["status"] == "missing"
    assert result["quote"]["gap_events_today"] == []


@pytest.mark.parametrize("at,visible", [
    (None, DAY),
    (DAY, DAY - timedelta(seconds=1)),
    (DAY - timedelta(seconds=1), DAY),
])
def test_bad_commit_clocks_do_not_overwrite_last_good_round(at, visible):
    state = monitor()
    quote(state, DAY, round_id="first")
    result = quote(state, at, DAY, visible=visible, round_id="bad")
    assert result["status"] == "invalid_clock"
    snapshot = state.snapshot(now=DAY + timedelta(seconds=2))
    assert snapshot["quote"]["last_round"]["round_id"] == "first"
    assert snapshot["status"] == "degraded"


def test_timezone_normalization_and_actual_visibility_delay():
    state = monitor()
    utc = DAY.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    quote(state, utc, visible=DAY + timedelta(seconds=8))
    result = state.snapshot(now=DAY + timedelta(seconds=8))
    assert result["quote"]["last_round"]["commit_to_visibility_sec"] == 8
    assert result["quote"]["status"] == "fresh"


def test_degraded_quote_not_a_success_and_snapshot_is_owned():
    state = monitor()
    quote(state, DAY, quality="degraded")
    result = state.snapshot(now=DAY + timedelta(seconds=1))
    assert result["quote"]["status"] == "degraded"
    result["quote"]["last_round"]["intercommit_continuity"]["status"] = "fake"
    assert state.snapshot(now=DAY)["quote"]["last_round"]["intercommit_continuity"]["status"] == "unknown"


@pytest.mark.parametrize("event_code,expected", [
    (EVENT_JOB_ERROR, "error"), (EVENT_JOB_MISSED, "missed"),
    (EVENT_JOB_MAX_INSTANCES, "max_instances"),
])
def test_scheduler_failures_are_sticky_even_after_success(event_code, expected):
    state = monitor()
    quote(state, DAY)
    event = SimpleNamespace(
        code=event_code, job_id="promotion_prediction_1510",
        scheduled_run_times=[DAY], exception=RuntimeError("secret-do-not-expose"),
    )
    failed = state.observe_job(event, observed_at=DAY + timedelta(seconds=300))
    assert failed["status"] == expected
    assert failed["finish_lateness_sec"] == 300
    state.observe_job(JobExecutionEvent(EVENT_JOB_EXECUTED, event.job_id, "default", DAY),
                      observed_at=DAY + timedelta(seconds=310))
    result = state.snapshot(now=DAY + timedelta(seconds=310))
    assert result["job_alerts_today"][0]["status"] == expected
    assert result["jobs"][0]["status"] == "executed"
    assert result["business_success_requires_persisted_batch"] is True
    assert "secret" not in str(result)


def test_returned_failure_is_not_scheduler_business_success():
    state = monitor()
    event = JobExecutionEvent(
        EVENT_JOB_EXECUTED, "tencent_spot", "default", DAY,
        retval={"status": "failed", "reason": "private-result", "data": [1, 2, 3]},
    )
    row = state.observe_job(event, observed_at=DAY)
    assert row["status"] == "business_degraded"
    assert "private-result" not in str(state.snapshot(now=DAY))
    event.retval = {"status": "skipped"}
    assert state.observe_job(event, observed_at=DAY)["status"] == "executed"


def test_runtime_retention_is_bounded_and_past_alerts_not_current_day():
    state = monitor()
    for i in range(150):
        event = JobExecutionEvent(EVENT_JOB_MISSED, f"job-{i}", "default", DAY)
        state.observe_job(event, observed_at=DAY)
    result = state.snapshot(now=DAY)
    assert len(result["jobs"]) == len(result["job_alerts_today"]) == 100
    assert state.snapshot(now=DAY + timedelta(days=1))["job_alerts_today"] == []


def test_scheduler_exposes_and_disposes_listener_without_starting_jobs(monkeypatch):
    from app.data.scheduler import DataScheduler

    scheduler = DataScheduler()
    # Do not start a real scheduler or invoke any business callback.
    scheduler.scheduler.add_listener(scheduler._on_scheduler_job_event, EVENT_JOB_MISSED)
    scheduler._runtime_listener_registered = True
    scheduler._on_scheduler_job_event(
        JobExecutionEvent(EVENT_JOB_MISSED, "promotion_prediction_1510", "default", datetime.now())
    )
    result = scheduler.get_pipeline_runtime_status()["operational_health"]
    assert result["jobs"][0]["status"] == "missed"
    assert len(scheduler.scheduler._listeners) == 1
    scheduler.stop()
    scheduler.stop()
    assert len(scheduler.scheduler._listeners) == 0


@pytest.mark.asyncio
async def test_stop_cancels_observation_task_even_if_scheduler_is_already_stopped():
    from app.data.scheduler import DataScheduler

    scheduler = DataScheduler()
    task = asyncio.create_task(asyncio.Event().wait())
    scheduler._pipeline_health_task = task
    scheduler.stop()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_observation_loop_logs_stale_without_running_business_jobs(monkeypatch):
    from app.data import scheduler as module

    scheduler = module.DataScheduler()
    scheduler.scheduler = SimpleNamespace(running=True)
    scheduler._pipeline_runtime_health = monitor(DAY)
    quote(scheduler._pipeline_runtime_health, DAY)
    real_snapshot = scheduler._pipeline_runtime_health.snapshot
    monkeypatch.setattr(scheduler._pipeline_runtime_health, "snapshot",
                        lambda: real_snapshot(now=DAY + timedelta(seconds=306)))
    warnings = []
    monkeypatch.setattr(module.logger, "warning", lambda *args: warnings.append(args))
    async def finish(_seconds):
        scheduler.scheduler.running = False
    # Replace this module's asyncio reference, not the shared event loop.
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        sleep=finish, CancelledError=asyncio.CancelledError,
    ))
    await scheduler._pipeline_health_loop()
    assert len(warnings) == 1
    assert warnings[0][1]["quote_status"] == "stale"
    assert warnings[0][1]["active_tail_sec"] == 306
