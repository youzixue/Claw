"""Parent integration: frozen scanner cutoff and nullable dragon API evidence."""
from datetime import datetime, timedelta
import importlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1.tenbagger import _serialize_dragon_result, _dragon_snapshot_key
from app.signal.dragon_head import DragonHeadScanner
from app.signal.anomaly_scanner import AnomalyScanner
from app.models.stock import FundFlow
from test_main_fund_window_20260914 import db, no_provider_http
from test_scanner_fund_consumers_20260914 import seed_consumer, NOW, DAY


@pytest.mark.parametrize("amount,status", [(0, "ok"), (-100000000, "ok"), (100000000, "ok"),
                                           (None, "missing"), (1e9, "stale")])
def test_dragon_api_preserves_current_null_zero_negative_and_separate_dated_evidence(amount, status):
    display = {"available": True, "main_net_inflow": 2e8, "purpose": "display_only",
               "source_quote_at": "2026-09-14T15:00:00"}
    result = DragonHeadScanner().scan_sector("S1", "隔离", [{
        "code": "600001", "main_net_inflow": amount, "main_fund_status": status,
        "main_fund_decision_at": NOW.isoformat(), "main_fund_display": display,
    }])[0]
    payload = _serialize_dragon_result(result)
    assert payload["main_net_inflow"] == (amount if status == "ok" else None)
    assert payload["main_fund_status"] == status
    assert payload["main_fund_decision_at"] == NOW.isoformat()
    assert payload["main_fund_display"] == display
    payload["main_fund_display"]["main_net_inflow"] = 0
    assert result.main_fund_display["main_net_inflow"] == 2e8
    assert "v6_strict_current_fund_v1" in _dragon_snapshot_key(50)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["single", "bulk", "resonance"])
async def test_default_fund_decision_is_before_first_await_not_retimed_after_queries(db, monkeypatch, method):
    await seed_consumer(db)
    row = await db.scalar(select(FundFlow))
    row.observed_at = NOW + timedelta(microseconds=500000)
    await db.commit()
    module = importlib.import_module("app.signal.anomaly_scanner")
    class Clock(datetime):
        current = NOW
        @classmethod
        def now(cls):
            return cls.fromisoformat(cls.current.isoformat())
    original_execute = db.execute
    async def delayed_execute(*args, **kwargs):
        Clock.current += timedelta(seconds=1)
        return await original_execute(*args, **kwargs)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(db, "execute", delayed_execute)
    scanner = AnomalyScanner()
    monkeypatch.setattr(scanner, "_load_leader_history_features", AsyncMock(return_value={}))
    monkeypatch.setattr(scanner, "_load_primary_industry_map", AsyncMock(return_value={}))
    if method == "single":
        result = (await scanner._assemble_sector_stocks(db, "S1", DAY))[0]
    elif method == "bulk":
        result = (await scanner._assemble_many_sector_stocks(db, ["S1"], DAY))["S1"][0]
    else:
        result = await scanner.analyze_resonance("000001", db, DAY)
    assert Clock.current > NOW
    assert result["main_fund_decision_at"] == NOW.isoformat()
    assert result["main_net_inflow"] is None and result["main_fund_status"] != "ok"
    display = result["main_fund_display"]
    assert display["available"] is False and display["clock_status"] == "future"
    assert display["main_net_inflow"] is None
