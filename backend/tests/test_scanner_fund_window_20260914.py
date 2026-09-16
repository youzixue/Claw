"""Isolated scanner/window/streak contracts; no provider HTTP or production writes."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.models.stock import FundFlow, StockSpot
from app.models.governance import TradeCalendarModel
from app.signal.anomaly_scanner import AnomalyScanner
from app.signal.capital_anomaly import CapitalAnomalyDetector
from test_main_fund_window_20260914 import db, seed, DAY, PREVIOUS, no_provider_http

NOW = datetime(2026, 9, 14, 10, 1, 10)


async def scanner_data(db, case="ok"):
    await seed(db, amounts={day: 300_000_000 for day in PREVIOUS + [DAY]})
    current = await db.scalar(select(FundFlow).where(FundFlow.trade_date == DAY))
    current.main_net_inflow = 600_000_000
    current.main_net_inflow_pct = 10
    current.source_quote_at = NOW.replace(second=0)
    current.received_at = NOW - timedelta(seconds=2)
    current.observed_at = NOW - timedelta(seconds=1)
    previous = await db.scalar(select(FundFlow).where(FundFlow.trade_date == PREVIOUS[2]))
    if case == "missing":
        await db.delete(previous)
    elif case == "null":
        previous.main_net_inflow = None
    elif case == "unknown_clock":
        previous.source_quote_at = None
    elif case == "preclose":
        previous.source_quote_at = previous.source_quote_at.replace(hour=14, minute=59)
    elif case == "late_latest":
        previous.observed_at = NOW + timedelta(seconds=1)
    elif case == "unsupported":
        previous.source = "stock_spot"
    elif case == "calendar_gap":
        await db.delete(await db.get(TradeCalendarModel, PREVIOUS[2]))
    elif case == "zero":
        previous.main_net_inflow = 0
    db.add(StockSpot(code="000001", name="隔离样本", price=10, open=10, high=10, low=10,
                     prev_close=10, limit_up=11, limit_down=9, change_pct=0,
                     volume=100, amount=1000, volume_ratio=1, turnover=1,
                     updated_at=NOW, source_quote_at=NOW-timedelta(seconds=2),
                     received_at=NOW-timedelta(seconds=1)))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "missing", "null", "unknown_clock", "preclose",
                                  "late_latest", "unsupported", "calendar_gap", "zero"])
@pytest.mark.parametrize("path", ["market", "single"])
async def test_all_scanner_entrypoints_use_complete_window(db, monkeypatch, case, path):
    from app.signal import anomaly_scanner as module
    await scanner_data(db, case)
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda *args: "after_close")
    scanner = AnomalyScanner()
    detector = Mock(wraps=scanner.capital_detector.detect)
    monkeypatch.setattr(scanner.capital_detector, "detect", detector)
    if path == "single":
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW
        monkeypatch.setattr(module, "datetime", Clock)
        await scanner.score_stock("000001", db, target_date=DAY)
    else:
        events = await scanner.scan_market(db, target_date=DAY, as_of_at=NOW)
        current = next(e for e in events if e.detail.get("capital_anomaly_type") == "main_inflow")
        assert current.detail["main_net_inflow"] == 600_000_000
        assert current.detail["fund_5d_window"]["decision_at"] == NOW.isoformat()
        assert current.detail["fund_5d_window"]["basis"] == "dated_latest_not_pit"
        assert current.detail["fund_5d_complete"] is (case in {"ok", "zero"})
        consecutive = [e for e in events if e.detail.get("capital_anomaly_type") == "consecutive"]
        assert bool(consecutive) is (case == "ok")
    assert detector.call_count == 1  # current qualified flow is not disabled by history gaps
    history = detector.call_args.kwargs["recent_funds"]
    if case in {"ok", "zero"}:
        assert len(history) == 5
        assert [item["trade_date"] for item in history] == [day.isoformat() for day in reversed(PREVIOUS)]
        assert all(item["status"] == "dated_known" for item in history)
        assert any(item["main_net_inflow"] == 0 for item in history) is (case == "zero")
    else:
        assert history == []
    # No repaired/rewound source rows were written by either scanner entrypoint.
    await db.rollback()
    current = await db.scalar(select(FundFlow).where(FundFlow.trade_date == DAY))
    assert current.main_net_inflow == 600_000_000
    if case == "null":
        assert (await db.scalar(select(FundFlow).where(FundFlow.trade_date == PREVIOUS[2]))).main_net_inflow is None


@pytest.mark.asyncio
async def test_nonempty_amount_hint_cannot_bypass_source_clock_checks(db, monkeypatch):
    from app.signal import anomaly_scanner as module
    await scanner_data(db, "unknown_clock")
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda *args: "after_close")
    scanner = AnomalyScanner()
    detector = Mock(wraps=scanner.capital_detector.detect)
    monkeypatch.setattr(scanner.capital_detector, "detect", detector)
    await scanner.scan_market(db, target_date=DAY, as_of_at=NOW,
                              recent_funds_map={"000001": [{"main_net_inflow": 9e9}] * 5})
    assert detector.call_args.kwargs["recent_funds"] == []


@pytest.mark.asyncio
async def test_explicit_empty_history_does_not_fall_back(db, monkeypatch):
    from app.signal import anomaly_scanner as module
    await scanner_data(db)
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda *args: "after_close")
    forbidden = AsyncMock(side_effect=AssertionError("explicit unknown must not fallback"))
    monkeypatch.setattr(module, "load_main_fund_window", forbidden)
    scanner = AnomalyScanner()
    detector = Mock(wraps=scanner.capital_detector.detect)
    monkeypatch.setattr(scanner.capital_detector, "detect", detector)
    await scanner.scan_market(db, target_date=DAY, as_of_at=NOW, recent_funds_map={})
    forbidden.assert_not_called()
    assert detector.call_args.kwargs["recent_funds"] == []


@pytest.mark.parametrize("values,days,direction,total", [
    ([3, 3, 3, 3, 3], 5, "in", 15),
    ([3, 3, 3, -9, 9], 3, "in", 9),
    ([-3, -3, -3, -3, -3], 5, "out", 15),
    ([-3, -3, -3, 9, -9], 3, "out", 9),
    ([3, -3, 3, 3, 3], 0, "in", 0),
    ([-3, 3, -3, -3, -3], 0, "out", 0),
    ([0, 3, 3, 3, 3], 0, "in", 0),
    ([3, 3, 0, 3, 3], 0, "in", 0),
    ([3, 3, None, 3, 3], 0, "in", 0),
    ([3, 3, float("inf"), 3, 3], 0, "in", 0),
    ([3, 3, True, 3, 3], 0, "in", 0),
    ([3, 3, 3], 0, "in", 0),
])
def test_consecutive_is_current_uninterrupted_run_not_scattered_count(values, days, direction, total):
    history = [{"main_net_inflow": v * 1e8 if isinstance(v, (int, float)) and not isinstance(v, bool)
                else v} for v in values]
    events = CapitalAnomalyDetector().detect("000001", "隔离", {"main_net_inflow": 0},
                                            recent_funds=history)
    consecutive = [event for event in events if event.anomaly_type == "consecutive"]
    assert bool(consecutive) is bool(days)
    if days:
        assert consecutive[0].detail[f"consecutive_{direction}_days"] == days
        key = "recent_total_inflow" if direction == "in" else "recent_total_outflow"
        assert consecutive[0].detail[key] == total * 1e8
        expected_score = (85 if direction == "in" else 80) if days == 5 else (65 if direction == "in" else 60)
        assert consecutive[0].score == expected_score


def test_finite_days_with_overflowing_streak_do_not_emit_infinite_evidence():
    events = CapitalAnomalyDetector().detect(
        "000001", "隔离", {"main_net_inflow": 0},
        recent_funds=[{"main_net_inflow": 1e308}] * 5,
    )
    assert not any(event.anomaly_type == "consecutive" for event in events)


@pytest.mark.asyncio
async def test_snapshot_freezes_fund_cutoff_before_any_await(db, monkeypatch):
    from app.api.v1 import tenbagger as api
    first = NOW.replace(hour=14, minute=59, second=59)
    class Clock(datetime):
        current = first
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(api, "datetime", Clock)
    async def prewarm(*args, **kwargs):
        Clock.current = NOW.replace(hour=15, minute=1)
        return {"items": {}}
    monkeypatch.setattr(api, "prewarm_eastmoney_main_fund_snapshot", prewarm)
    scanner = AsyncMock(return_value=[])
    monkeypatch.setattr(api.anomaly_scanner, "scan_market", scanner)
    monkeypatch.setattr(api, "_build_b1_state_rows_for_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "_persist_anomaly_snapshot", AsyncMock())
    result = await api._scan_anomaly_snapshot(db, DAY)
    assert scanner.call_args.kwargs["as_of_at"] == first
    assert "recent_funds_map" not in scanner.call_args.kwargs
    assert result["snapshot_time"] == Clock.current.isoformat()  # completion time stays separate
