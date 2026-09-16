from app.config.settings import settings
from app.data.scheduler import DataScheduler, _review_schedule_time


def test_review_schedule_time_fails_safe():
    assert _review_schedule_time("08:45", (1, 2)) == (8, 45)
    assert _review_schedule_time("25:99", (20, 35)) == (20, 35)
    assert _review_schedule_time("bad", (11, 35)) == (11, 35)


def test_scheduler_registers_three_stage_review_and_regime_jobs():
    scheduler = DataScheduler()
    scheduler.setup_jobs()
    job_ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert {
        "review_snapshot_premarket",
        "review_snapshot_intraday",
        "review_snapshot_postmarket",
        "market_regime_snapshot",
    } <= job_ids
    assert settings.PROMOTION_LEGACY_DAILY_WEIGHT_ADJUSTMENT_ENABLED is False
