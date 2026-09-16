"""Runtime evidence is not inferred from a switch or a configured authorization date."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog
from app.paper.experiment_report import runtime_scan_evidence
from test_paper_api import paper_client


def test_only_explicit_past_scan_proves_allowed_attempt():
    at = datetime(2026, 9, 8, 10)
    def row(run, when, flags, action="scan"):
        return SimpleNamespace(run_id=run, created_at=when, action=action,
                               candidate_json=json.dumps(flags))
    explicit = {"runtime_evidence_version":"account_scan_v1", "buy_attempt_allowed":True}
    logs = [
        row("old", at-timedelta(minutes=5), {"scan_heartbeat":True}),
        row("future", at+timedelta(seconds=1), explicit),
        row("candidate", at-timedelta(minutes=2), explicit, action="confirm_buy"),
        row("disabled", at-timedelta(minutes=1), {**explicit, "buy_attempt_allowed":False}),
        row("valid", at, explicit),
        row("valid", at, explicit),
    ]
    result = runtime_scan_evidence(logs, as_of=at)
    assert result["scan_started_count"] == 2
    assert result["buy_attempt_allowed_scan_count"] == 1
    assert result["first_buy_attempt_allowed_scan_at"] == at.isoformat()
    assert result["strategy_data_and_risk_passed"] is None
    unknown = runtime_scan_evidence(logs[:3], as_of=at)
    assert unknown["basis"] == "not_recorded_for_this_version"
    assert unknown["first_buy_attempt_allowed_scan_at"] is None


@pytest.mark.asyncio
async def test_disabled_main_account_still_records_actual_scan_not_enabled_buy(paper_client, monkeypatch):
    _, maker = paper_client
    at = datetime(2026, 9, 8, 10)
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_AUTO_TRADE_ENABLED", False)
    monkeypatch.setattr(paper, "_should_run_intraday_auto_trade", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_paper_order_window_status", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    async with maker() as db:
        result = await paper.run_paper_auto_trade(db, now=at, account_name="promotion")
        rows = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action=="scan"))).all())
    assert len(rows) == 1
    assert rows[0].strategy_version == paper._strategy_version("promotion")
    assert json.loads(rows[0].candidate_json)["buy_attempt_allowed"] is False
    assert runtime_scan_evidence(rows, as_of=at)["scan_started_count"] == 1
    assert all(item["action"] != "buy" for item in result["logs"])
