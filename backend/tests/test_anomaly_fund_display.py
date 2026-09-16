"""Radar display contracts only; synthetic fixtures, no orders or production DB."""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.data.main_fund import (
    main_fund_display_evidence, load_current_main_fund_map,
    load_latest_main_fund_display_map,
)
from app.models.stock import FundFlow, StockSpot
from app.api.v1 import tenbagger
from app.signal.anomaly_scanner import AnomalyScanner
from test_scheduler_kline_fill import scheduler_db_env

import pytest

from app.api.v1.tenbagger import _build_stock_rows


def event(detail):
    return {
        "code": "600908", "name": "隔离展示样本", "event_type": "breakthrough",
        "score": 80, "description": "展示合同测试",
        "detail": {"price": 6.06, "change_pct": 2.54, **detail},
    }


@pytest.mark.parametrize("value", [None, 0, 120000000, -120000000])
def test_event_fund_and_book_display_preserve_missing_zero_and_sign(value):
    fields = (
        "main_net_inflow", "main_net_inflow_pct", "support_strength_score",
        "seal_quality_score", "withdrawal_ratio",
    )
    detail = {key: value for key in fields}
    original = event(detail)
    before = deepcopy(original)
    row = _build_stock_rows([original])[0]
    for key in fields:
        assert row["events"][0][key] == value
    assert original == before


@pytest.mark.parametrize("source,provider,version,status,stale", [
    ("fund_flow", "tencent", "tencent_hsfundtab_v1", "ok", False),
    ("fund_flow_stale", "tencent", "tencent_hsfundtab_v1", "stale", True),
    ("eastmoney_main_fund", "eastmoney", "individual_fund_flow_v3_f124", "ok", False),
    ("unavailable", None, None, "unknown", True),
    ("stock_spot", None, None, None, None),
])
def test_event_detail_keeps_provider_and_original_fund_clock(source, provider, version, status, stale):
    metadata = dict(
        source=source, provider_source=provider, source_version=version,
        source_quote_at="2026-09-10T10:00:00",
        received_at="2026-09-10T10:00:05",
        observed_at="2026-09-10T10:00:06",
        clock_status=status, is_stale=stale,
    )
    row = _build_stock_rows([event({
        **metadata, "as_of": "2026-09-10T10:01:00",
        "main_net_inflow": 0, "main_net_inflow_pct": 0,
    })])[0]
    detail = row["events"][0]
    for key, value in metadata.items():
        assert detail[key] == value
    assert detail["source_quote_at"] != detail["latest_as_of"]


def test_legacy_missing_fields_do_not_turn_into_measured_zero_or_fake_source():
    detail = _build_stock_rows([event({})])[0]["events"][0]
    for key in (
        "main_net_inflow", "main_net_inflow_pct", "support_strength_score",
        "seal_quality_score", "withdrawal_ratio", "provider_source", "clock_status",
    ):
        assert detail[key] is None


NOW = datetime(2026, 9, 7, 10, 1, 2)
CLOSE = NOW.replace(hour=18)


def measured(**changes):
    return {
        "code": "000001", "name": "隔离资金", "trade_date": NOW.date(),
        "main_net_inflow": 120000000, "main_net_inflow_pct": 12.5,
        "source": "tencent", "source_version": "tencent_hsfundtab_v1",
        "source_quote_at": NOW - timedelta(seconds=2),
        "received_at": NOW - timedelta(seconds=1), "observed_at": NOW,
        **changes,
    }


@pytest.mark.parametrize("value", [0, 1000000, -1000000])
def test_display_keeps_measured_values_after_expiry_without_certifying_live(value):
    raw = measured(main_net_inflow=value)
    result = main_fund_display_evidence(raw, trade_date=NOW.date(), as_of_at=CLOSE)
    assert result["available"] and result["clock_status"] == "stale"
    assert result["main_net_inflow"] == value
    assert result["purpose"] == "display_only"
    assert result["source_quote_at"] == raw["source_quote_at"].isoformat()
    assert raw["source"] == "tencent"


@pytest.mark.parametrize("changes", [
    {"main_net_inflow": None}, {"main_net_inflow": True},
    {"main_net_inflow_pct": float("nan")}, {"main_net_inflow": float("inf")},
    {"source": "stock_spot"}, {"source": "stock_spot", "provider_source": "tencent"},
    {"source_version": "unknown"}, {"source_quote_at": None},
    {"source_quote_at": "2026-09-07"}, {"trade_date": NOW.date() - timedelta(days=1)},
    {"observed_at": CLOSE + timedelta(seconds=1)},
    {"received_at": NOW - timedelta(seconds=5)},
    {"observed_at": NOW + timedelta(seconds=601)},
])
def test_display_does_not_whiten_unknown_invalid_future_or_mistyped_evidence(changes):
    result = main_fund_display_evidence(measured(**changes), trade_date=NOW.date(), as_of_at=CLOSE)
    assert result["available"] is False
    assert result["main_net_inflow"] is None


def test_event_snapshot_is_not_reconstructed_with_later_daily_row():
    early = main_fund_display_evidence(
        measured(), trade_date=NOW.date(), as_of_at=NOW,
        basis="event_snapshot", event_at=NOW,
    )
    later_view = main_fund_display_evidence(
        early, trade_date=NOW.date(), as_of_at=CLOSE,
        basis="event_snapshot", event_at=early["event_at"],
    )
    assert later_view["available"]
    assert later_view["event_at"] == NOW.isoformat()
    assert later_view["clock_status"] == "stale"
    late_data = measured(source_quote_at=NOW + timedelta(hours=1),
                         received_at=NOW + timedelta(hours=1),
                         observed_at=NOW + timedelta(hours=1))
    result = main_fund_display_evidence(
        late_data, trade_date=NOW.date(), as_of_at=CLOSE,
        basis="event_snapshot", event_at=NOW,
    )
    assert result["available"] is False and result["clock_status"] == "future"
    old = measured(source_quote_at=NOW - timedelta(seconds=601),
                   received_at=NOW - timedelta(seconds=600),
                   observed_at=NOW - timedelta(seconds=599))
    assert not main_fund_display_evidence(
        old, trade_date=NOW.date(), as_of_at=CLOSE,
        basis="event_snapshot", event_at=NOW,
    )["available"]


@pytest.mark.asyncio
async def test_display_query_preserves_real_zero_and_never_flushes_pending_or_restores_live():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
    try:
        async with sessions() as db:
            db.add(FundFlow(**measured(main_net_inflow=0)))
            await db.commit()
            before = (await db.execute(text("SELECT * FROM fund_flow"))).all()
            # Pending data would overwrite the measured zero if the display flushed.
            row = await db.scalar(select(FundFlow))
            row.main_net_inflow = 999
            display = await load_latest_main_fund_display_map(
                db, trade_date=NOW.date(), as_of_at=CLOSE, codes=["000001", "999999"],
            )
            assert display["000001"]["main_net_inflow"] == 0
            assert "999999" not in display
            assert await load_current_main_fund_map(
                db, trade_date=NOW.date(), decision_at=CLOSE,
            ) == {}
            assert (await db.execute(text("SELECT * FROM fund_flow"))).all() == before
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_response_projection_does_not_modify_execution_flags_or_repaint_old_events(monkeypatch):
    from app.data import main_fund
    latest = main_fund_display_evidence(measured(), trade_date=NOW.date(), as_of_at=CLOSE)
    monkeypatch.setattr(main_fund, "load_latest_main_fund_display_map",
                        AsyncMock(return_value={"000001": latest}))
    evidence = main_fund_display_evidence(
        measured(main_net_inflow=-1), trade_date=NOW.date(), as_of_at=NOW,
        basis="event_snapshot", event_at=NOW,
    )
    rows = [
        {"code": "000001", "detail": {"main_net_inflow": None, "source": "unavailable"},
         "fund_evidence": evidence, "main_net_inflow": None, "buy_point_pushable": False,
         "feishu_pushable": False, "events": [{"key": "new", "fund_display": evidence}]},
        {"code": "000001", "detail": {"main_net_inflow": None},
         "events": [{"key": "old", "main_net_inflow": 0}]},
    ]
    before = deepcopy(rows)
    result = await tenbagger._attach_anomaly_display_context(
        rows, object(), trade_date=NOW.date(), as_of_at=CLOSE,
    )
    assert rows == before
    assert result[0]["fund_display"]["main_net_inflow"] == -1
    assert result[0]["fund_display"]["basis"] == "event_snapshot"
    assert result[1]["fund_display"]["main_net_inflow"] == 120000000
    assert result[1]["fund_display"]["basis"] == "latest_snapshot"
    assert result[1]["events"][0]["fund_display"]["available"] is False
    for i in range(2):
        assert result[i]["detail"] == before[i]["detail"]
    for key in ("main_net_inflow", "buy_point_pushable", "feishu_pushable"):
        assert result[0][key] == before[0][key]


def test_primary_event_keeps_whole_fund_frame_not_nonzero_peer_fallback():
    zero = main_fund_display_evidence(
        measured(main_net_inflow=0), trade_date=NOW.date(), as_of_at=NOW,
        basis="event_snapshot", event_at=NOW,
    )
    primary = {**event({"fund_evidence": zero, "main_net_inflow": 0}), "score": 99}
    peer = {**event({"main_net_inflow": 999999}), "score": 60, "event_type": "capital"}
    row = _build_stock_rows([primary, peer])[0]
    assert row["fund_evidence"] == zero
    assert row["fund_evidence"]["main_net_inflow"] == 0


@pytest.mark.parametrize("now,changes,status", [
    (NOW, {}, "ok"),
    (CLOSE, {}, "historical"),
    (NOW + timedelta(seconds=100), {}, "stale"),
    (NOW, {"quote_source_at": None}, "unknown"),
    (NOW, {"quote_source_at": NOW + timedelta(seconds=1)}, "future"),
    (NOW, {"quote_received_at": NOW - timedelta(seconds=2)}, "invalid"),
    (NOW, {"ask_depth_5": 0}, "single_sided"),
])
def test_book_clock_is_independent_from_fund_clock(now, changes, status):
    detail = {"quote_source_at": NOW - timedelta(seconds=1), "quote_received_at": NOW,
              "bid_depth_5": 100, "ask_depth_5": 50,
              "source_quote_at": CLOSE, "received_at": CLOSE, **changes}
    result = tenbagger._anomaly_orderbook_display(detail, as_of_at=now)
    assert result["clock_status"] == status


@pytest.mark.asyncio
async def test_real_scan_freezes_fund_evidence_even_on_limit_up_without_changing_detector_fields(
    scheduler_db_env, monkeypatch,
):
    async with scheduler_db_env() as db:
        db.add(FundFlow(**measured()))
        db.add(StockSpot(code="000001", name="隔离涨停", price=11, prev_close=10,
                         open=10, high=11, low=10, limit_up=11, limit_down=9,
                         volume=100, amount=100000, change_pct=10, volume_ratio=1,
                         turnover=1, source_quote_at=NOW, received_at=NOW, updated_at=NOW))
        await db.commit()
        scanner = AnomalyScanner()
        events = await scanner.scan_market(
            db, target_date=NOW.date(), as_of_at=NOW, recent_funds_map={},
        )
        item = next(e for e in events if e.event_type == "limit_up")
        assert "main_net_inflow" not in item.detail  # original detector contract
        assert item.score == 85
        assert item.detail["fund_evidence"]["available"]
        assert item.detail["fund_evidence"]["main_net_inflow"] == 120000000
        assert item.detail["fund_evidence"]["source_quote_at"] != item.detail["quote_source_at"]
        assert item.detail["fund_evidence"]["event_at"] == NOW.isoformat()
        assert item.detail["quote_source_at"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_display_read_failure_keeps_original_evidence_and_does_not_fall_back_to_execution(monkeypatch):
    from app.data import main_fund
    from sqlalchemy.exc import OperationalError
    monkeypatch.setattr(main_fund, "load_latest_main_fund_display_map",
                        AsyncMock(side_effect=OperationalError("SELECT", {}, Exception("isolated"))))
    db = SimpleNamespace(rollback=AsyncMock())
    evidence = main_fund_display_evidence(
        measured(), trade_date=NOW.date(), as_of_at=NOW,
        basis="event_snapshot", event_at=NOW,
    )
    rows = [
        {"code": "000001", "fund_evidence": evidence, "detail": {"main_net_inflow": None}},
        {"code": "000002", "detail": {"main_net_inflow": 999999}},
    ]
    result = await tenbagger._attach_anomaly_display_context(
        rows, db, trade_date=NOW.date(), as_of_at=CLOSE,
    )
    db.rollback.assert_awaited_once()
    assert result[0]["fund_display"]["main_net_inflow"] == 120000000
    assert result[1]["fund_display"]["main_net_inflow"] is None
    assert result[1]["fund_display"]["clock_status"] == "read_error"
    assert result[1]["detail"]["main_net_inflow"] == 999999


@pytest.mark.asyncio
async def test_foreign_stock_event_frame_cannot_become_this_stocks_display(monkeypatch):
    from app.data import main_fund
    monkeypatch.setattr(main_fund, "load_latest_main_fund_display_map", AsyncMock(return_value={}))
    evidence = main_fund_display_evidence(
        measured(), trade_date=NOW.date(), as_of_at=NOW,
        basis="event_snapshot", event_at=NOW,
    )
    result = await tenbagger._attach_anomaly_display_context(
        [{"code": "000002", "fund_evidence": evidence, "events": [{"fund_display": evidence}]}],
        object(), trade_date=NOW.date(), as_of_at=CLOSE,
    )
    assert result[0]["fund_display"]["available"] is False
    assert result[0]["events"][0]["fund_display"]["available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("buy_only", [False, True])
async def test_endpoint_fund_sort_matches_display_and_does_not_change_buy_filter(monkeypatch, buy_only):
    from app.data import main_fund
    from app.dashboard2.service import dashboard2_service
    rows = [
        {"code": code, "detail": {"main_net_inflow": None}, "main_net_inflow": None,
         "display_score": 80, "buy_point_pushable": ready, "events": []}
        for code, ready in [("000001", False), ("000002", True), ("000003", True), ("000004", True)]
    ]
    original = deepcopy(rows)
    mapping = {
        code: main_fund_display_evidence(
            measured(code=code, main_net_inflow=amount),
            trade_date=NOW.date(), as_of_at=CLOSE,
        )
        for code, amount in [("000001", 1), ("000002", 0), ("000003", -1)]
    }
    load = AsyncMock(return_value=mapping)
    monkeypatch.setattr(main_fund, "load_latest_main_fund_display_map", load)
    class CurrentSnapshotClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    # Test sorting independently on a valid live snapshot; missing-time/closed
    # snapshots are observation-only and have dedicated endpoint regressions.
    monkeypatch.setattr(tenbagger, "datetime", CurrentSnapshotClock)
    monkeypatch.setattr(tenbagger, "prewarm_anomaly_snapshot", AsyncMock(return_value={
        "trade_date": str(NOW.date()), "snapshot_time": NOW.isoformat(),
        "anomalies": [], "b1_states": [],
    }))
    monkeypatch.setattr(tenbagger, "_load_recorded_capital_activity", AsyncMock(return_value={"status": "unavailable"}))
    monkeypatch.setattr(tenbagger, "_build_stock_rows", lambda *a, **kw: deepcopy(rows))
    monkeypatch.setattr(tenbagger, "_enrich_stock_rows_with_b1", AsyncMock(side_effect=lambda rows, *a, **kw: rows))
    monkeypatch.setattr(tenbagger, "_apply_stock_row_buy_point_status", lambda row: row)
    monkeypatch.setattr(dashboard2_service, "_load_a_share_context",
                        AsyncMock(side_effect=RuntimeError("isolated market context")))
    db = SimpleNamespace(rollback=AsyncMock())
    result = await tenbagger.anomaly_monitor(
        min_score=50, event_type="", setup_track="", sort_by="net_inflow",
        buy_point_only=buy_only, view="stock", page=1, page_size=10, db=db,
    )
    expected = ["000002", "000003", "000004"] if buy_only else ["000001", "000002", "000003", "000004"]
    assert [row["code"] for row in result["rows"]] == expected
    assert set(load.call_args.kwargs["codes"]) == set(expected)
    assert all(row["detail"]["main_net_inflow"] is None for row in result["rows"])
    assert rows == original
