"""Synthetic end-to-end signal contracts; no production DB/orders/push/HTTP."""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import tenbagger as api
from app.config.settings import settings
from app.data.main_fund import anomaly_main_fund_evidence, freeze_anomaly_main_fund
from app.db.session import Base
from app.models.stock import FundFlow, StockSpot
from app.signal.anomaly_scanner import AnomalyScanner

NOW = datetime(2026, 9, 9, 10, 1, 10)
DAY = NOW.date()
PROVIDERS = [("tencent", "tencent_hsfundtab_v1"),
             ("eastmoney", "individual_fund_flow_v3_f124")]


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(api, "datetime", Clock)
    def forbidden(*args, **kwargs):
        raise AssertionError("isolated contract attempted provider HTTP")
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def fund(source="tencent", version="tencent_hsfundtab_v1", **overrides):
    row = dict(code="000001", trade_date=DAY, source=source, source_version=version,
                main_net_inflow=600_000_000, main_net_inflow_pct=10.0,
                super_net_inflow=200_000_000, super_net_inflow_pct=3.3,
                big_net_inflow=400_000_000, big_net_inflow_pct=6.7,
                source_quote_at=NOW.replace(second=0), received_at=NOW-timedelta(seconds=5),
                observed_at=NOW-timedelta(seconds=4))
    row.update(overrides)
    return row


def event(frame=None):
    detail = dict(price=10.38, avg_price=10.30, change_pct=3.8, volume_ratio=2.4,
                  amplitude=4.0, turnover=8.5, support_strength_score=72,
                  bid_depth_5=52000, ask_depth_5=26000, orderbook_imbalance=.33,
                  capital_anomaly_type="main_inflow", source="fund_flow", is_stale=False,
                  source_quote_at=(NOW-timedelta(seconds=2)).isoformat(),
                  received_at=(NOW-timedelta(seconds=1)).isoformat(),
                  quote_source_at=(NOW-timedelta(seconds=2)).isoformat(),
                  sector_factors=[dict(sector_name="算力", fund_flow=12.5, change_pct=1.8, strength_score=72)])
    detail["fund_signal_evidence"] = freeze_anomaly_main_fund(
        frame if frame is not None else fund(), code="000001", trade_date=DAY, decision_at=NOW)
    return dict(code="000001", name="隔离合成", event_type="capital", level="critical",
                score=95, detail=detail, description="资金净流入")


def test_legacy_source_label_without_provenance_cannot_qualify_for_execution():
    assert not api._has_qualified_main_fund_source({
        "source": "eastmoney_main_fund", "is_stale": False,
        "main_net_inflow": 600_000_000, "main_net_inflow_pct": 10.0,
    })


@pytest.mark.parametrize("source,version", PROVIDERS)
@pytest.mark.parametrize("injected", [True, False])
@pytest.mark.asyncio
async def test_real_scanner_to_grade_track_and_push_selection(source, version, injected, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            raw = fund(source, version)
            db.add(FundFlow(**raw))
            db.add(StockSpot(code="000001", name="隔离合成", price=10.38, open=10.1,
                high=10.4, low=10.0, prev_close=10, limit_up=11, limit_down=9,
                change_pct=3.8, amplitude=4.0, volume=1000000, amount=6000000000,
                volume_ratio=2.4, turnover=8.5, updated_at=NOW,
                source_quote_at=NOW-timedelta(seconds=2), received_at=NOW-timedelta(seconds=1),
                avg_price=10.30, bid_depth_5=52000, ask_depth_5=26000,
                orderbook_imbalance=.33, support_strength_score=72))
            await db.commit()
            kwargs = {}
            if injected:
                monkeypatch.setattr(api, "resolve_latest_trade_date", AsyncMock(return_value=DAY))
                snapshot = await api.prewarm_eastmoney_main_fund_snapshot(db, DAY)
                items, source_label = api._resolve_anomaly_current_fund_context(snapshot)
                kwargs = dict(current_fund_map=items, fund_source=source_label)
            scanner = AnomalyScanner()
            # Freeze only the sector premise. Capital detection and grading are real.
            monkeypatch.setattr(scanner, "_build_sector_context_detail", lambda *a: {
                "sector_factors": event()["detail"]["sector_factors"]})
            events = await scanner.scan_market(db, target_date=DAY, as_of_at=NOW,
                fund_trade_date=DAY, recent_funds_map={}, **kwargs)
            capital = next(e for e in events if e.event_type == "capital")
            anomaly = api._serialize_event(capital)
            before = deepcopy(anomaly)
            frame = anomaly["detail"]["fund_signal_evidence"]
            assert frame["source"] == source
            assert frame["source_version"] == version
            assert frame["source_quote_at"] == raw["source_quote_at"].isoformat()
            assert frame["observed_at"] == raw["observed_at"].isoformat()
            assert anomaly["detail"]["source_quote_at"] != frame["source_quote_at"]
            enriched = api._enrich_anomaly_display(anomaly, [anomaly])
            assert enriched["setup_grade"] == "A1 可直接执行"
            assert enriched["setup_track"] == "趋势/资金型"
            assert enriched["buy_point_pushable"] is True
            assert api._summarize_anomalies([enriched])["capital_count"] == 1
            assert len(api._select_pushworthy_anomalies([enriched], [])) == 1
            messages = api._build_push_messages_for_anomalies([enriched], peer_anomalies=[enriched])
            assert len(messages) == 1
            assert messages[0].extra["fund_signal_evidence"] == frame
            assert anomaly == before
            # Query-time B1/latest funds cannot change the original event evidence.
            row = api._build_aggregated_anomaly_row([enriched])
            assert row["setup_track"] == "趋势/资金型"
            assert row["buy_point_pushable"]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("support,expected", [(69, "A2 盘口确认后执行"), (70, "A1 可直接执行")])
@pytest.mark.parametrize("source,version", PROVIDERS)
def test_original_support_thresholds_unchanged(source, version, support, expected):
    anomaly = event(fund(source, version))
    anomaly["detail"]["support_strength_score"] = support
    assert api._build_capital_confirmation_context(anomaly)["entry_grade"] == expected


@pytest.mark.parametrize("changes", [
    {"source_version": None}, {"source": "stock_spot"},
    {"source_quote_at": None}, {"received_at": None}, {"observed_at": None},
    {"observed_at": (NOW+timedelta(seconds=1)).isoformat()},
    {"event_at": (NOW-timedelta(seconds=6)).isoformat()},
    {"event_at": (NOW+timedelta(seconds=1)).isoformat()},
    {"source_quote_at": (NOW-timedelta(seconds=601)).isoformat()},
    {"source_quote_at": (NOW-timedelta(days=1)).isoformat()},
    {"received_at": (NOW-timedelta(minutes=1)).isoformat()},
    {"trade_date": (DAY-timedelta(days=1)).isoformat()},
    {"code": "000002"}, {"purpose": "display_only"}, {"available": False},
    {"main_net_inflow": None}, {"main_net_inflow_pct": float("nan")},
    {"main_net_inflow": True},
])
def test_bad_evidence_cannot_be_replaced_by_flat_or_display_values(changes):
    anomaly = event()
    anomaly["detail"].update(fund())  # enticing valid flat fallback must be ignored
    anomaly["detail"]["fund_signal_evidence"].update(changes)
    anomaly["detail"]["fund_display"] = dict(fund(), purpose="display_only")
    before = deepcopy(anomaly)
    assert api._build_capital_confirmation_context(anomaly) is None
    enriched = api._enrich_anomaly_display(anomaly, [anomaly])
    assert not enriched["buy_point_pushable"]
    assert not enriched["setup_track"]
    assert api._select_pushworthy_anomalies([anomaly], []) == []
    assert anomaly == before


def test_zero_negative_and_expired_frames_do_not_become_buy_signals(monkeypatch):
    for amount in (0, -600_000_000):
        raw = fund()
        raw["main_net_inflow"] = amount
        raw["main_net_inflow_pct"] = 0 if not amount else -10
        anomaly = event(raw)
        qualified = anomaly_main_fund_evidence(anomaly["detail"], code="000001", decision_at=NOW)
        assert qualified is not None
        assert qualified["main_net_inflow"] == amount
        assert api._build_capital_confirmation_context(anomaly) is None
    old = event()
    class AfterClose(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(hour=18)
    monkeypatch.setattr(api, "datetime", AfterClose)
    assert api._build_capital_confirmation_context(old) is None
    assert api._select_pushworthy_anomalies([old], []) == []


def test_signal_frame_is_independent_of_later_provider_row_and_quote_refresh():
    raw = fund()
    anomaly = event(raw)
    raw["main_net_inflow"] = -1e9
    anomaly["detail"].update(main_net_inflow=-1e9, main_net_inflow_pct=-99)
    before = deepcopy(anomaly)
    assert api._build_capital_confirmation_context(anomaly)["direction"] == "inflow"
    assert anomaly == before
    assert anomaly["detail"]["fund_signal_evidence"]["main_net_inflow"] == 600_000_000


def test_fund_based_low_absorb_and_message_use_same_frozen_frame():
    from test_tenbagger_push_logic import _make_low_absorb_anomaly
    anomaly = _make_low_absorb_anomaly(code="000001")
    raw = fund(main_net_inflow=180_000_000, main_net_inflow_pct=5.2)
    anomaly["detail"]["fund_signal_evidence"] = freeze_anomaly_main_fund(
        raw, code="000001", trade_date=DAY, decision_at=NOW)
    anomaly["detail"].update(main_net_inflow=None, source="unavailable", is_stale=True)
    before = deepcopy(anomaly)
    result = api._enrich_anomaly_display(anomaly, [anomaly])
    assert result["setup_grade"] == "A1 可直接执行"
    entry = api._build_anomaly_push_message_data(anomaly, [anomaly])
    assert entry["setup_grade"] == result["setup_grade"]
    assert entry["message"].extra["fund_signal_evidence"] == anomaly["detail"]["fund_signal_evidence"]
    assert anomaly == before
    anomaly["detail"]["fund_signal_evidence"]["available"] = False
    assert api._build_low_absorb_entry_context(anomaly) is None


def test_paper_current_binding_cannot_repair_an_earlier_unknown_signal():
    from app.api.v1.paper import _bind_main_fund_evidence
    anomaly = event({})
    anomaly["detail"].update(main_net_inflow=None, main_net_inflow_pct=None)
    original_frame = deepcopy(anomaly["detail"]["fund_signal_evidence"])
    _bind_main_fund_evidence(anomaly, fund())
    assert anomaly["detail"]["main_net_inflow"] == 600_000_000
    assert anomaly["detail"]["fund_signal_evidence"] == original_frame
    assert api._build_capital_confirmation_context(anomaly) is None


@pytest.mark.parametrize("changes", [
    {"code": "000002"}, {"trade_date": DAY-timedelta(days=1)},
    {"source": "stock_spot", "provider_source": "tencent"},
    {"purpose": "display_only"}, {"is_stale": True},
])
def test_freezing_cannot_relabel_foreign_stale_or_display_frames(changes):
    frame = freeze_anomaly_main_fund(fund(**changes), code="000001", trade_date=DAY, decision_at=NOW)
    assert frame["available"] is False
    assert anomaly_main_fund_evidence({"fund_signal_evidence": frame}, code="000001", decision_at=NOW) is None


def test_large_order_boolean_cannot_outlive_its_fund_frame():
    anomaly = event()
    anomaly["detail"].update(large_order_inflow_confirmed=True, large_order_net_inflow=600_000_000)
    assert api._signal_fund_detail(anomaly)["large_order_inflow_confirmed"]
    anomaly["detail"]["fund_signal_evidence"]["available"] = False
    result = api._signal_fund_detail(anomaly)
    assert result["large_order_inflow_confirmed"] is False
    assert result["large_order_net_inflow"] is None
    assert anomaly["detail"]["large_order_inflow_confirmed"] is True  # history unchanged
