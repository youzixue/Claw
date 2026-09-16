"""Isolated synthetic consumer contracts: no provider HTTP or business DB access."""
from contextlib import nullcontext
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models.stock import FundFlow
from app.models.governance import TradeCalendarModel

from app.api.v1 import tenbagger as api
from app.config.settings import settings

NOW = datetime(2026, 9, 9, 10, 1, 10)
DAY = NOW.date()


def fund(**overrides):
    row = dict(
        code="000001", name="隔离腾讯资金", trade_date=DAY,
        main_net_inflow=0.0, main_net_inflow_pct=0.0,
        source="tencent", source_version="tencent_hsfundtab_v1",
        source_quote_at=NOW.replace(second=0),
        received_at=NOW - timedelta(seconds=2),
        observed_at=NOW - timedelta(seconds=1),
    )
    row.update(overrides)
    return row


class ReadOnlyRows:
    """Only SELECT is available; no real engine/session or filesystem fixture."""
    def __init__(self, rows):
        self.rows = rows
        self.no_autoflush = nullcontext()
        self.reads = 0

    async def execute(self, statement):
        assert statement.is_select
        assert statement.get_execution_options().get("autoflush") is False
        self.reads += 1
        return self

    def mappings(self):
        return iter(self.rows)

    def scalars(self):
        return self

    def all(self):
        return []

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected DB operation: {name}")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    def forbidden(*args, **kwargs):
        raise AssertionError("Consumer attempted provider HTTP")

    monkeypatch.setattr(api, "datetime", Clock)
    monkeypatch.setattr(api, "resolve_latest_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    import requests
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest_asyncio.fixture
async def independent_fund_history_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tencent-history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
        await conn.run_sync(TradeCalendarModel.__table__.create)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            db.add(FundFlow(**fund()))
            # Five explicit confirmed sessions, independent from the current quote.
            for offset in (7, 6, 5, 2, 1):
                day = DAY - timedelta(days=offset)
                close = NOW.replace(hour=15, minute=0, second=0) - timedelta(days=offset)
                db.add(TradeCalendarModel(trade_date=day, is_trade_day=True))
                db.add(FundFlow(**fund(
                    trade_date=day, source_quote_at=close,
                    received_at=close + timedelta(seconds=1), observed_at=close + timedelta(seconds=2),
                )))
            await db.commit()
            yield db
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tencent_prewarm_and_detail_are_read_only_and_preserve_zero_clocks(independent_fund_history_db):
    db = independent_fund_history_db
    # Actual AsyncSession proves calendar no_autoflush context, not only statement flags.
    db.add(FundFlow(**fund(code="pending")))
    snapshot = await api.prewarm_eastmoney_main_fund_snapshot(db, DAY, force_refresh=True)
    assert snapshot["source"] == "fund_flow"
    assert snapshot["snapshot_time"] == (NOW - timedelta(seconds=1)).isoformat()
    item = snapshot["items"]["000001"]
    assert item["provider_source"] == "tencent"
    assert item["source_version"] == "tencent_hsfundtab_v1"
    assert item["source"] == "fund_flow"
    assert item["clock_basis"] == "provider_fund_minute_watermark"
    assert item["source_clock_basis"] == "provider_fund_minute_watermark"
    assert item["main_net_inflow"] == item["main_net_inflow_pct"] == 0
    assert item["super_net_inflow"] is None
    assert item["big_net_inflow"] is None
    assert item["as_of"] == NOW.replace(second=0).isoformat()
    assert item["clock_status"] == "ok"
    assert item["is_stale"] is False
    items, source = api._resolve_anomaly_current_fund_context(snapshot)
    assert items == snapshot["items"]
    assert source == "fund_flow"
    context = await api._load_stock_fund_context(
        db, "000001", quote_trade_date=DAY, completed_trade_date=DAY,
        current_snapshot={"source": "eastmoney_main_fund", "items": {}}, as_of_at=NOW,
    )
    assert context["current_item"] == item
    assert context["current_source"] == "fund_flow"
    assert context["used_fallback"] is False
    assert context["fund_5d_total"] == 0
    assert context["fund_5d_complete"] is True
    assert context["fund_5d_count"] == 5
    assert context["fund_5d_clock_unknown_count"] == 0
    assert context["fund_5d_window"]["session_dates"] == [
        (DAY - timedelta(days=offset)).isoformat() for offset in (7, 6, 5, 2, 1)
    ]
    assert context["fund_5d_window"]["basis"] == "dated_latest_not_pit"
    merged = api._merge_current_main_fund_detail({}, item)
    assert merged["source"] == "fund_flow"
    assert merged["provider_source"] == "tencent"
    assert merged["clock_basis"] == "provider_fund_minute_watermark"
    assert "东方财富" not in api._format_data_source_label(merged["source"])
    assert len(db.new) == 1  # current AND calendar/history reads did not flush pending funds


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"main_net_inflow": None}, {"main_net_inflow_pct": None},
    {"main_net_inflow": float("nan")}, {"main_net_inflow_pct": float("inf")},
    {"source_version": "individual_fund_flow_v3_f124"},
    {"source_quote_at": None}, {"received_at": None}, {"observed_at": None},
    {"observed_at": NOW + timedelta(microseconds=1)},
    {"received_at": NOW - timedelta(minutes=2)},
    {"source_quote_at": NOW - timedelta(seconds=settings.FUND_FLOW_SOURCE_MAX_AGE_SEC + 1)},
    {"trade_date": DAY - timedelta(days=1)},
])
async def test_invalid_funds_and_client_snapshot_cannot_supply_current_evidence(overrides):
    db = ReadOnlyRows([fund(**overrides)])
    snapshot = await api.prewarm_eastmoney_main_fund_snapshot(db, DAY)
    assert snapshot["items"] == {}
    forged = {"source": "fund_flow", "items": {"000001": {
        **fund(main_net_inflow=1e10, main_net_inflow_pct=50),
        "source": "fund_flow", "provider_source": "tencent",
        "clock_status": "ok", "is_stale": False,
    }}}
    context = await api._load_stock_fund_context(
        db, "000001", quote_trade_date=DAY, completed_trade_date=None,
        current_snapshot=forged, as_of_at=NOW,
    )
    assert context["current_item"] == {}
    assert context["current_source"] == "unavailable"
    assert context["used_fallback"] is True
    assert api._resolve_anomaly_current_fund_context(snapshot) == ({}, "unavailable")


@pytest.mark.asyncio
async def test_legacy_and_mixed_provider_snapshots_keep_per_row_labels():
    legacy = fund(code="000002", source="eastmoney", source_version="individual_fund_flow_v3_f124")
    db = ReadOnlyRows([legacy])
    old = await api.prewarm_eastmoney_main_fund_snapshot(db, DAY)
    assert old["source"] == "eastmoney_main_fund"
    assert old["items"]["000002"]["clock_basis"] == "provider_quote_update_f124"
    assert api._resolve_anomaly_current_fund_context(old)[1] == "eastmoney_main_fund"
    db.rows.append(fund())
    mixed = await api.prewarm_eastmoney_main_fund_snapshot(db, DAY)
    assert mixed["source"] == "fund_flow"
    assert mixed["items"]["000002"]["source"] == "eastmoney_main_fund"
    assert mixed["items"]["000001"]["clock_basis"] == "provider_fund_minute_watermark"


def qualified_detail(**overrides):
    return {**fund(main_net_inflow=180_000_000, main_net_inflow_pct=5.2),
            "source": "fund_flow", "provider_source": "tencent", "is_stale": False,
            **overrides}


@pytest.mark.parametrize("overrides", [
    {"provider_source": None}, {"source_version": None}, {"source_quote_at": None},
    {"main_net_inflow_pct": None}, {"is_stale": True},
    {"observed_at": NOW + timedelta(seconds=1)},
])
def test_generic_fund_label_alone_does_not_unlock_old_source_gate(overrides):
    assert api._has_qualified_main_fund_source(qualified_detail())
    assert not api._has_qualified_main_fund_source(qualified_detail(**overrides))
    assert not api._has_qualified_main_fund_source({"source": "fund_flow"})


@pytest.mark.parametrize("support,change,volume", [(72, -0.6, 1.35), (67, -0.6, 1.35), (72, 4.2, 3.1)])
def test_low_absorb_qualified_tencent_has_same_numeric_thresholds_as_legacy(support, change, volume):
    from test_tenbagger_push_logic import _make_low_absorb_anomaly
    legacy = _make_low_absorb_anomaly(
        support_strength_score=support, change_pct=change, volume_ratio=volume,
    )
    # Both providers must describe the SAME stock; foreign funds cannot confirm a buy.
    current = {**legacy, "detail": {**legacy["detail"], **qualified_detail(code=legacy["code"])}}
    old_message = api._build_anomaly_push_message(legacy, [legacy])
    new_message = api._build_anomaly_push_message(current, [current])
    assert (old_message is None) == (new_message is None)
    if old_message is not None:
        assert new_message.title == old_message.title


@pytest.mark.parametrize("support", [69, 70])
def test_capital_direct_execution_support_threshold_is_unchanged(support):
    detail = {
        **qualified_detail(main_net_inflow=600_000_000, main_net_inflow_pct=9.2),
        "change_pct": 3.8, "volume_ratio": 2.4, "amplitude": 4.2, "turnover": 8.5,
        "bid_depth_5": 52000, "ask_depth_5": 26000, "orderbook_imbalance": 0.33,
        "support_strength_score": support,
        "sector_factors": [{"sector_name": "算力", "fund_flow": 12.5, "change_pct": 1.8}],
    }
    current = {"event_type": "capital", "detail": detail}
    legacy = {"event_type": "capital", "detail": {**detail, "source": "eastmoney_main_fund"}}
    peers = [{"event_type": "breakthrough", "score": 82}]
    actual = api._build_capital_confirmation_context(current, peers)
    expected = api._build_capital_confirmation_context(legacy, peers)
    assert actual["entry_grade"] == expected["entry_grade"]
    assert (actual["entry_grade"] == "A1 可直接执行") == (support == 70)
