"""Source clocks must survive every DB/cache fallback, without rewriting history."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.api.v1.tenbagger import _load_stock_fund_context
from app.core.prediction_data_quality import PredictionDataQualityAuditor
from app.data.fund_flow_clock import evidence_clock, fund_clock_status
from app.models.stock import FundFlow, StockSpot, StockTag
from app.models.governance import TradeCalendarModel
from app.signal.anomaly_scanner import AnomalyScanner
from test_scheduler_kline_fill import scheduler_db_env

NOW = datetime(2026, 9, 7, 10, 1, 2)


def clocks(case):
    if case == "unknown":
        return None, None, None
    if case == "stale":
        return NOW - timedelta(seconds=601), NOW - timedelta(seconds=600), NOW - timedelta(seconds=599)
    if case == "future":
        return NOW - timedelta(seconds=2), NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)
    if case == "invalid":
        return NOW - timedelta(seconds=1), NOW - timedelta(seconds=2), NOW
    return NOW - timedelta(seconds=2), NOW - timedelta(seconds=1), NOW


def fund(code="000001", case="ok"):
    source, received, observed = clocks(case)
    return FundFlow(code=code, trade_date=NOW.date(), main_net_inflow=200_000_000,
                    main_net_inflow_pct=8, big_net_inflow=100_000_000,
                    source="eastmoney", source_version="individual_fund_flow_v3_f124",
                    source_quote_at=source, received_at=received, observed_at=observed)


@pytest.mark.parametrize("case", ["ok", "unknown", "stale", "future", "invalid"])
def test_current_fund_cannot_be_whitened_by_false_stale_flag(case):
    source, received, observed = clocks(case)
    raw = {"source_quote_at": source.isoformat() if source else None,
           "received_at": received.isoformat() if received else None,
           "observed_at": observed.isoformat() if observed else None,
           "main_net_inflow": 1, "main_net_inflow_pct": 1, "is_stale": False,
           "source": "eastmoney", "source_version": "individual_fund_flow_v3_f124"}
    item = AnomalyScanner._current_fund_evidence(raw, NOW.date(), NOW)
    assert item["clock_status"] == case
    assert item["is_stale"] == (case != "ok")
    assert raw["is_stale"] is False


def test_date_only_string_is_not_inferred_as_source_midnight():
    assert evidence_clock("2026-09-07") is None
    assert fund_clock_status("2026-09-07", NOW, NOW, NOW.date(), NOW, 600) == "unknown"


def test_historical_known_does_not_mean_live_and_future_is_never_known():
    source, received, observed = clocks("stale")
    assert fund_clock_status(source, received, observed, NOW.date(), NOW, 600) == "stale"
    assert fund_clock_status(source, received, observed, NOW.date(), NOW, 600, require_live=False) == "historical_known"
    assert fund_clock_status(*clocks("future"), NOW.date(), NOW, 600, require_live=False) == "future"
    assert fund_clock_status(None, None, None, NOW.date(), NOW, 600, require_live=False) == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "unknown", "stale", "future", "invalid"])
async def test_scan_db_fallback_does_not_feed_unverified_capital_signals(scheduler_db_env, case):
    async with scheduler_db_env() as session:
        session.add(fund(case=case))
        session.add(StockSpot(code="000001", name="测试", price=10, open=10, high=10, low=10,
                              prev_close=10, limit_up=11, limit_down=9, change_pct=0,
                              volume=100, amount=1000, volume_ratio=1, turnover=1,
                              updated_at=NOW))
        await session.commit()
        scanner = AnomalyScanner()
        scanner._should_scan_capital = Mock(return_value=True)
        scanner.capital_detector.detect = Mock(return_value=[])
        await scanner.scan_market(session, target_date=NOW.date(), as_of_at=NOW)
        assert scanner.capital_detector.detect.call_count == (1 if case == "ok" else 0)
        row = await session.scalar(select(FundFlow))
        assert row.main_net_inflow == 200_000_000
        assert (row.source_quote_at, row.received_at, row.observed_at) == clocks(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "unknown", "stale", "future", "invalid"])
async def test_detail_fallback_reports_staleness_without_erasing_research_values(scheduler_db_env, case):
    async with scheduler_db_env() as session:
        session.add(fund(case=case))
        await session.commit()
        context = await _load_stock_fund_context(
            session, "000001", quote_trade_date=NOW.date(), completed_trade_date=NOW.date(),
            current_snapshot={"source": "eastmoney_main_fund_unavailable", "items": {}}, as_of_at=NOW,
        )
        if case == "ok":
            assert context["current_item"]["clock_status"] == "ok"
            assert context["current_item"]["is_stale"] is False
            assert context["current_item"]["main_net_inflow"] == 200_000_000
            assert context["current_as_of"] == clocks(case)[0].isoformat()
        else:
            # Current projection no longer republishes unqualified historical
            # values. Original dated research and raw rows remain intact.
            assert context["current_item"] == {}
            assert context["current_source"] == "unavailable"
            assert context["current_as_of"] is None
        # A single current-day row cannot certify five completed sessions.
        # No calendar was supplied; do not erase the raw row or infer a window.
        assert context["fund_5d_total"] is None
        assert context["fund_5d_complete"] is False
        assert context["fund_5d_status"] == "calendar_incomplete"
        assert context["fund_5d_window"]["session_dates"] == []
        assert context["fund_5d_clock_unknown_count"] == 0
        row = await session.scalar(select(FundFlow))
        assert row.main_net_inflow == 200_000_000
        assert (row.source_quote_at, row.received_at, row.observed_at) == clocks(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_history", [False, True])
async def test_completed_history_is_independent_of_current_fund_freshness(scheduler_db_env, bad_history):
    days = [(NOW - timedelta(days=offset)).date() for offset in (7, 6, 5, 4, 3)]
    async with scheduler_db_env() as session:
        session.add(fund(case="stale"))
        for index, day in enumerate(days):
            source = datetime.combine(day, datetime.min.time()).replace(hour=15)
            session.add(TradeCalendarModel(trade_date=day, is_trade_day=True))
            session.add(FundFlow(
                code="000001", trade_date=day, main_net_inflow=0 if index == 0 else -50_000_000,
                main_net_inflow_pct=0 if index == 0 else -2,
                source="tencent", source_version="tencent_hsfundtab_v1",
                source_quote_at=None if bad_history and index == 2 else source,
                received_at=source + timedelta(seconds=1), observed_at=source + timedelta(seconds=2),
            ))
        await session.commit()
        context = await _load_stock_fund_context(
            session, "000001", quote_trade_date=NOW.date(), completed_trade_date=days[-1], as_of_at=NOW,
        )
        assert context["current_item"] == {}
        assert context["fund_5d_complete"] is (not bad_history)
        assert context["fund_5d_total"] == (None if bad_history else -200_000_000)
        assert context["fund_5d_count"] == (4 if bad_history else 5)
        assert context["fund_5d_clock_unknown_count"] == int(bad_history)
        assert context["fund_5d_window"]["session_dates"] == [day.isoformat() for day in days]
        raw = await session.scalar(select(FundFlow).where(FundFlow.trade_date == days[2]))
        assert raw.main_net_inflow == -50_000_000
        assert (raw.source_quote_at is None) == bad_history


@pytest.mark.asyncio
async def test_watermark_counts_each_rows_clock_not_only_trade_date(scheduler_db_env):
    cases = ("ok", "unknown", "stale", "future", "invalid")
    async with scheduler_db_env() as session:
        for index, case in enumerate(cases, start=1):
            code = f"{index:06}"
            session.add(fund(code=code, case=case))
            session.add(StockTag(code=code, board_type="main_sz", board_tag="tradeable", is_st=False,
                                 is_suspended=False, is_delisting=False))
        await session.commit()
        result = await PredictionDataQualityAuditor().audit(
            session, trade_date_value=NOW.date(), snapshot_context="promotion_1030",
            as_of_at=NOW, persist=False,
        )
        watermark = next(item for item in result["watermarks"] if item["dataset"] == "fund_flow")
        assert watermark["record_count"] == 1
        assert watermark["expected_count"] == 5
        assert watermark["completeness"] == 0.2
        assert watermark["max_available_at"] == NOW.isoformat()
        assert watermark["details"]["raw_record_count"] == 5
        assert watermark["details"]["clock_status_counts"] == {case: 1 for case in cases}
        assert "fund_flow" in result["route_gates"]["mainline_spread_start"]["blocking_datasets"]
        persisted = list((await session.scalars(select(FundFlow).order_by(FundFlow.code))).all())
        assert len(persisted) == 5
        assert persisted[1].source_quote_at is None


@pytest.mark.asyncio
async def test_valid_clock_but_missing_main_percentage_is_not_coverage(scheduler_db_env):
    async with scheduler_db_env() as session:
        row = fund()
        row.main_net_inflow_pct = None
        session.add(row)
        await session.commit()
        watermarks = await PredictionDataQualityAuditor()._build_watermarks(
            session, NOW.date(), "promotion_1030", as_of_at=NOW,
        )
        result = next(item for item in watermarks if item["dataset"] == "fund_flow")
        assert result["record_count"] == 0
        assert result["details"]["clock_status_counts"] == {"invalid_values": 1}


@pytest.mark.asyncio
async def test_detail_route_passes_unavailable_flag_for_stale_funds(scheduler_db_env, monkeypatch):
    from app.api.v1 import tenbagger as api
    from app.models.stock import StockKline
    async with scheduler_db_env() as session:
        session.add(fund(case="unknown"))
        session.add(StockSpot(code="000001", name="测试", price=10, open=10, high=10.2, low=9.9,
                              prev_close=9.9, limit_up=10.89, limit_down=8.91, change_pct=1,
                              volume=1000, amount=10000, volume_ratio=1.2, turnover=2))
        session.add(StockKline(code="000001", trade_date=NOW.date(), open=10, close=10,
                              high=10.2, low=9.9, volume=1000))
        await session.commit()
        monkeypatch.setattr(api, "resolve_latest_trade_date", AsyncMock(return_value=NOW.date()))
        monkeypatch.setattr(api, "prewarm_eastmoney_main_fund_snapshot", AsyncMock(return_value={
            "source": "eastmoney_main_fund_unavailable", "items": {},
        }))
        monkeypatch.setattr(api, "_load_stock_plan_sector_context", AsyncMock(return_value={"available": True}))
        monkeypatch.setattr(api, "_load_market_regime_context", AsyncMock(return_value={"sentiment_trade_date": NOW.date()}))
        spy = Mock(wraps=api.next_day_plan_engine.generate)
        monkeypatch.setattr(api.next_day_plan_engine, "generate", spy)
        result = await api.stock_next_day_plan("000001", db=session)
        assert spy.call_args.kwargs["current_fund_available"] is False
        assert spy.call_args.kwargs["fund_5d_complete"] is False
        assert result["current_fund_clock_status"] == "unknown"
        assert result["current_fund_is_stale"] is True
        assert result["fund_5d_clock_unknown_count"] == 0
        assert result["fund_5d_status"] == "calendar_incomplete"
        assert result["fund_5d_billion"] is None
        assert result["fund_5d_complete"] is False
        assert result["quality_status"] == "degraded"
        assert any("源时钟" in warning for warning in result["quality_warnings"])
