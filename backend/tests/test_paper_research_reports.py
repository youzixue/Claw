"""Scheduled reporting must never write business tables or acquire trading locks."""
import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.config.settings import settings
from app.models.governance import TradeCalendarModel
from app.paper import research_reports as reports
from test_paper_signal_research import db, DAY

AT = datetime(2026, 9, 8, 16)


def configure(monkeypatch, tmp_path):
    monkeypatch.setattr(reports, "ROOT", tmp_path)
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", DAY.isoformat())


@pytest.mark.asyncio
async def test_publication_is_readonly_snapshot_and_atomic_file(db, monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
    await db.commit()
    database = Path(db.bind.url.database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    result = await reports.publish_daily_paper_research(now=AT, database_url=str(db.bind.url))
    assert result["status"] == "published" and result["read_only"] is True
    content = Path(result["output"]).read_bytes()
    document = json.loads(content)
    assert document["database_snapshot"] == "single_explicit_read_transaction"
    assert document["sections"]["post_exit"]["report"]["summary"]["records"] == 0
    assert len(document["sections"]["signal_portfolio"]["report"]["paired_accounts"]) == 12
    assert hashlib.sha256(content).hexdigest() == result["sha256"]
    assert (Path(result["output"]).stat().st_mode & 0o777) == 0o600
    assert not list(Path(result["output"]).parent.glob("*.tmp"))
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_write_attempt_fails_but_other_section_survives_and_failure_is_not_empty_success(db, monkeypatch, tmp_path):
    from app.paper import signal_research
    configure(monkeypatch, tmp_path)
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
    await db.commit()
    async def bad_builder(connection, **kwargs):
        assert (await connection.execute(text("PRAGMA query_only"))).scalar() == 1
        await connection.execute(text("DELETE FROM paper_account"))
    monkeypatch.setattr(signal_research, "build_parallel_research_report", bad_builder)
    result = await reports.publish_daily_paper_research(now=AT, database_url=str(db.bind.url))
    assert result["status"] == "partial"
    data = json.loads(Path(result["output"]).read_text())
    assert data["sections"]["signal_portfolio"]["report"] is None
    assert data["sections"]["signal_portfolio"]["error_type"] == "OperationalError"
    assert data["sections"]["post_exit"]["status"] == "built"
    assert "DELETE" not in json.dumps(data) and "readonly" not in json.dumps(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("state, expected", [(None, "calendar_unknown"), (False, "non_trading_day")])
async def test_calendar_missing_and_holiday_never_call_builders_or_publish(db, monkeypatch, tmp_path, state, expected):
    from app.paper import signal_research
    configure(monkeypatch, tmp_path)
    if state is not None:
        db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=state))
    await db.commit()
    spy = AsyncMock(side_effect=AssertionError("no calendar fallback"))
    monkeypatch.setattr(signal_research, "build_parallel_research_report", spy)
    result = await reports.publish_daily_paper_research(now=AT, database_url=str(db.bind.url))
    assert result["status"] == expected and not spy.called
    assert not (tmp_path / "outputs").exists()


@pytest.mark.asyncio
async def test_disabled_and_preclose_do_not_open_database(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    assert (await reports.publish_daily_paper_research(database_url="invalid"))["status"] == "disabled"
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    assert (await reports.publish_daily_paper_research(now=AT.replace(hour=10), database_url="invalid"))["status"] == "before_close_review"
    with pytest.raises(ValueError, match="non-future"):
        await reports.publish_daily_paper_research(now=datetime.now()+timedelta(days=1))
    with pytest.raises(ValueError, match="under project outputs"):
        await reports.publish_daily_paper_research(now=AT, output_dir=tmp_path / "elsewhere")


@pytest.mark.parametrize("url", ["postgresql://localhost/db", "sqlite:///:memory:",
                               "sqlite+aiosqlite:///relative.db", "sqlite:///missing.db?mode=rw"])
def test_unsafe_or_unsupported_database_rejected(url):
    with pytest.raises(ValueError):
        reports._database_path(url)


def test_atomic_publication_idempotent_never_overwrites_and_nonfinite_not_published(tmp_path):
    result = reports._publish({"status": "fixture"}, tmp_path, AT)
    before = Path(result["output"]).read_bytes()
    assert reports._publish({"status": "fixture"}, tmp_path, AT) == result
    assert Path(result["output"]).read_bytes() == before
    with pytest.raises(ValueError):
        reports._publish({"bad": float("nan")}, tmp_path, AT)
    assert len(list(tmp_path.iterdir())) == 1


def test_scheduler_registers_both_readonly_jobs_without_replacing_business_review():
    from app.data.scheduler import DataScheduler
    scheduler = DataScheduler()
    scheduler.setup_jobs()
    jobs = {job.id: job for job in scheduler.scheduler.get_jobs()}
    assert {"paper_research_1550", "paper_research_2045", "paper_auto_trade_close"} <= jobs.keys()
    for key, hour, minute in (("paper_research_1550", "15", "50"), ("paper_research_2045", "20", "45")):
        job = jobs[key]
        assert job.max_instances == 1 and job.coalesce and job.misfire_grace_time == 900
        assert str(job.trigger.fields[5]) == hour and str(job.trigger.fields[6]) == minute
        assert job.func.__name__ == "_publish_paper_research"


@pytest.mark.asyncio
async def test_scheduler_report_lock_and_fault_are_isolated_from_trade_execution(monkeypatch):
    from app.data.scheduler import DataScheduler
    scheduler = DataScheduler()
    scheduler._paper_auto_trading = True
    monkeypatch.setattr(reports, "publish_daily_paper_research", AsyncMock(side_effect=RuntimeError("private database path")))
    result = await scheduler._publish_paper_research()
    assert result == {"status": "failed", "error_type": "RuntimeError"}
    assert scheduler._paper_auto_trading is True and scheduler._paper_research_reporting is False
    scheduler._paper_research_reporting = True
    assert (await scheduler._publish_paper_research())["status"] == "already_running"
    scheduler._paper_research_reporting = False
    monkeypatch.setattr(reports, "publish_daily_paper_research", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await scheduler._publish_paper_research()
    assert scheduler._paper_research_reporting is False and scheduler._paper_auto_trading is True
