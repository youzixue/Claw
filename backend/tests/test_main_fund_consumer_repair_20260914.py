"""Isolated 9/14 consumer semantics; never fetch providers or touch business DBs."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings
from app.data.main_fund import (
    current_main_fund_evidence, current_main_fund_status, load_current_main_fund_map,
)
from app.models.stock import FundFlow, StockSpot
from test_tencent_fund_consumer_labels import ReadOnlyRows

NOW = datetime(2026, 9, 14, 15, 0, 10)
DAY = NOW.date()


@pytest.fixture(autouse=True)
def no_provider_http(monkeypatch):
    import httpx
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("Consumer test attempted provider HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


def fund(**overrides):
    return {
        "code": "000001", "name": "合成样本", "trade_date": DAY,
        "main_net_inflow": 0.0, "main_net_inflow_pct": 0.0,
        "source": "tencent", "source_version": "tencent_hsfundtab_v1",
        "source_quote_at": NOW - timedelta(seconds=10),
        "received_at": NOW - timedelta(seconds=2),
        "observed_at": NOW - timedelta(seconds=1), **overrides,
    }


def freeze_clock(monkeypatch, api, now=NOW):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(api, "datetime", Clock)


@pytest.mark.parametrize("row,status", [
    (None, "missing"),
    (fund(), "ok"),
    (fund(main_net_inflow=-10), "ok"),
    (fund(main_net_inflow=None), "invalid_values"),
    (fund(main_net_inflow_pct=False), "invalid_values"),
    (fund(main_net_inflow=float("nan")), "invalid_values"),
    (fund(source="stock_spot"), "unsupported_source"),
    (fund(source_quote_at=None), "unknown"),
    (fund(received_at=NOW - timedelta(seconds=11)), "invalid"),
    (fund(observed_at=NOW + timedelta(microseconds=1)), "future"),
    (fund(trade_date=DAY - timedelta(days=1)), "invalid"),
])
def test_diagnostics_and_strict_gate_are_the_same_contract(row, status):
    assert current_main_fund_status(row, trade_date=DAY, decision_at=NOW) == status
    assert (current_main_fund_evidence(row, trade_date=DAY, decision_at=NOW) is not None) == (status == "ok")


def test_post_close_does_not_relax_threshold_or_turn_zero_into_missing():
    source = NOW.replace(second=0)
    row = fund(source_quote_at=source)
    boundary = source + timedelta(seconds=settings.FUND_FLOW_SOURCE_MAX_AGE_SEC)
    assert current_main_fund_evidence(row, trade_date=DAY, decision_at=boundary)["main_net_inflow"] == 0
    assert current_main_fund_status(row, trade_date=DAY, decision_at=boundary + timedelta(microseconds=1)) == "stale"
    assert current_main_fund_evidence(row, trade_date=DAY, decision_at=boundary + timedelta(microseconds=1)) is None


def test_iso_clocks_are_normalized_for_all_consumer_serializers():
    from app.api.v1.spot import _main_fund_payload
    from app.api.v1.paper import _main_fund_evidence
    raw = fund()
    for key in ("source_quote_at", "received_at", "observed_at"):
        raw[key] = raw[key].isoformat()
    item = current_main_fund_evidence(raw, trade_date=DAY, decision_at=NOW)
    assert isinstance(item["source_quote_at"], datetime)
    assert _main_fund_payload(item)["main_net_inflow"] == 0
    assert _main_fund_evidence(item)["main_fund_evidence"]["status"] == "known"
    assert _main_fund_payload(item)["main_fund_source_quote_at"] == raw["source_quote_at"]


@pytest.mark.asyncio
async def test_one_read_diagnostics_rejects_stale_future_without_exposing_amounts():
    stale = NOW - timedelta(seconds=settings.FUND_FLOW_SOURCE_MAX_AGE_SEC + 1)
    db = ReadOnlyRows([
        fund(),
        fund(code="000002", source_quote_at=stale, received_at=stale, observed_at=stale),
        fund(code="000003", observed_at=NOW + timedelta(seconds=1)),
    ])
    diagnostics = {"old": "ok"}
    current = await load_current_main_fund_map(
        db, trade_date=DAY, decision_at=NOW, codes=(str(i).zfill(6) for i in range(1, 5)),
        diagnostics=diagnostics,
    )
    assert diagnostics == {"000001": "ok", "000002": "stale", "000003": "future", "000004": "missing"}
    assert set(current) == {"000001"}
    assert db.reads == 1
    assert await load_current_main_fund_map(db, trade_date=DAY, decision_at=NOW + timedelta(days=1),
                                           codes=["000001"], diagnostics=diagnostics) == {}
    assert diagnostics == {"000001": "invalid_cutoff"}
    assert db.reads == 1


@pytest.mark.asyncio
async def test_spot_list_detail_prewarm_and_paper_share_status_and_zero(tmp_path, monkeypatch):
    from app.api.v1 import spot, tenbagger, paper
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'consumers.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
        await conn.run_sync(StockSpot.__table__.create)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    for api in (spot, tenbagger):
        freeze_clock(monkeypatch, api)
    monkeypatch.setattr(tenbagger, "resolve_latest_trade_date", AsyncMock(return_value=DAY))
    old = NOW - timedelta(seconds=settings.FUND_FLOW_SOURCE_MAX_AGE_SEC + 1)
    try:
        async with Session() as db:
            db.add_all([
                FundFlow(**fund()),
                FundFlow(**fund(code="000002", source_quote_at=old, received_at=old, observed_at=old)),
                FundFlow(**fund(code="000004", main_net_inflow=-1)),
                *[StockSpot(code=f"{i:06}", price=10, main_net_inflow=9e12) for i in range(1, 5)],
            ])
            await db.commit()
            listing = await spot.spot_list(codes="", limit=10, sort_by="main_net_inflow", min_change=-100, db=db)
            assert [r["code"] for r in listing["spots"]] == ["000001", "000004", "000002", "000003"]
            for row in listing["spots"]:
                detail = await spot.spot_detail(row["code"], db)
                for field in ("main_net_inflow", "main_net_inflow_pct", "main_fund_status", "main_fund_reason"):
                    assert row[field] == detail[field]
            rows = {r["code"]: r for r in listing["spots"]}
            assert rows["000001"]["main_net_inflow"] == 0
            assert rows["000001"]["main_fund_available"] is True
            assert rows["000002"]["main_fund_status"] == "stale"
            assert rows["000002"]["main_net_inflow"] is None
            assert rows["000003"]["main_fund_status"] == "unknown"
            assert rows["000003"]["main_fund_reason"] == "missing"
            snapshot = await tenbagger.prewarm_eastmoney_main_fund_snapshot(db, DAY)
            assert snapshot["fund_quality"]["qualified_count"] == 2
            assert snapshot["fund_quality"]["status_counts"] == {"ok": 2, "stale": 1}
            assert snapshot["fund_quality"]["denominator"] == "observed_fund_flow_rows"
            funds = await paper._paper_main_fund_map(db, trade_date=DAY, decision_at=NOW, codes=rows)
            assert set(funds) == set(snapshot["items"]) == {"000001", "000004"}
            assert (await db.scalar(select(StockSpot).where(StockSpot.code == "000003"))).main_net_inflow == 9e12
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_all_stale_prewarm_reports_unavailable_not_measured_zero(monkeypatch):
    from app.api.v1 import tenbagger
    freeze_clock(monkeypatch, tenbagger)
    monkeypatch.setattr(tenbagger, "resolve_latest_trade_date", AsyncMock(return_value=DAY))
    old = NOW - timedelta(seconds=settings.FUND_FLOW_SOURCE_MAX_AGE_SEC + 1)
    snapshot = await tenbagger.prewarm_eastmoney_main_fund_snapshot(
        ReadOnlyRows([fund(source_quote_at=old, received_at=old, observed_at=old)]), DAY,
    )
    assert snapshot["items"] == {}
    assert snapshot["snapshot_time"] is None
    assert snapshot["fund_quality"]["status"] == "unavailable"
    assert snapshot["fund_quality"]["status_counts"] == {"stale": 1}
    assert snapshot["fund_quality"]["qualified_count"] == 0


@pytest.mark.asyncio
async def test_previous_trade_day_prewarm_does_not_claim_zero_inventory(monkeypatch):
    from app.api.v1 import tenbagger
    freeze_clock(monkeypatch, tenbagger, NOW + timedelta(days=1))
    monkeypatch.setattr(tenbagger, "resolve_latest_trade_date", AsyncMock(return_value=DAY))
    db = ReadOnlyRows([fund()])
    snapshot = await tenbagger.prewarm_eastmoney_main_fund_snapshot(db, DAY)
    assert snapshot["items"] == {}
    assert snapshot["fund_quality"]["reason"] == "invalid_cutoff"
    assert snapshot["fund_quality"]["query_performed"] is False
    assert snapshot["fund_quality"]["row_count"] is None
    assert db.reads == 0


@pytest.mark.parametrize("item,expected", [(None, None), (fund(), 0), (fund(main_net_inflow_pct=-2), -2)])
def test_rank_percentage_never_collapses_missing_to_zero(item, expected):
    from app.api.v1.tenbagger import _get_current_main_fund_pct
    assert _get_current_main_fund_pct({"000001": item} if item else {}, "000001") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bull", "tenbagger"])
async def test_rank_generation_and_cache_keep_missing_separate_from_snapshot_zero(tmp_path, monkeypatch, mode):
    from app.api.v1 import tenbagger as api
    from app.db.session import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ranks.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    freeze_clock(monkeypatch, api)
    monkeypatch.setattr(api, "_RANK_CACHE", {})
    monkeypatch.setattr(api, "resolve_latest_trade_date", AsyncMock(return_value=DAY))
    monkeypatch.setattr(api, "_get_persisted_dashboard_snapshot", AsyncMock(return_value=None))
    monkeypatch.setattr(api, "_persist_dashboard_snapshot", AsyncMock())

    async def keep_rows(db, rows):
        return rows
    monkeypatch.setattr(api.stock_tagger, "filter_signals", keep_rows)
    try:
        async with Session() as db:
            db.add(FundFlow(**fund()))
            db.add_all([StockSpot(
                code=f"{i:06}", name="合成排行", price=10, prev_close=10,
                open=10, high=10, low=10, volume=10000, amount=10000000,
                change_pct=0, turnover=1, volume_ratio=1, pe_ttm=15, pb=2,
                circ_market_cap=50, net_profit_growth=20, main_net_inflow=9e12,
            ) for i in (1, 2)])
            await db.commit()
            calculate = api._bull_rank if mode == "bull" else api._tenbagger_rank
            result = await calculate(db)
            rows = {row["code"]: row for row in result["rank"]}
            assert set(rows) == {"000001", "000002"}
            assert rows["000001"]["main_net_inflow_billion"] == 0
            assert rows["000001"]["main_net_inflow_pct"] == 0
            assert rows["000001"]["main_fund_status"] == "snapshot_known"
            assert rows["000001"]["main_fund_source_quote_at"] == fund()["source_quote_at"].isoformat()
            assert rows["000002"]["main_net_inflow_billion"] is None
            assert rows["000002"]["main_net_inflow_pct"] is None
            assert rows["000002"]["main_fund_status"] == "unknown"
            assert rows["000002"]["main_fund_purpose"] == "ranking_snapshot"
            # Cached ranking does not pretend to be live money; original clock stays frozen.
            freeze_clock(monkeypatch, api, NOW + timedelta(minutes=20))
            assert await calculate(db) == result
    finally:
        await engine.dispose()
