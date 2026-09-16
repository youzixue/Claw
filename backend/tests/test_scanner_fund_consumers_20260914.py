"""Round5: isolated current-vs-dated fund contracts, no providers/production DB."""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.stock import FundFlow, StockSpot, BoardCons, StockSectorMapping, SectorPersistence
from app.signal.anomaly_scanner import AnomalyScanner
from app.signal.dragon_head import DragonHeadScanner
from test_main_fund_window_20260914 import db, fund, DAY, no_provider_http

NOW = datetime(2026, 9, 14, 10, 1, 10)


async def seed_consumer(db, case="ok", amount=123456789, sector_amount=2):
    row = fund(DAY, amount, source_quote_at=NOW-timedelta(seconds=10),
               received_at=NOW-timedelta(seconds=2), observed_at=NOW-timedelta(seconds=1))
    if case == "source":
        row["source"] = "stock_spot"
    elif case == "no_source":
        row["source"] = None
    elif case == "version":
        row["source_version"] = None
    elif case == "percentage":
        row["main_net_inflow_pct"] = None
    elif case == "clock":
        row["source_quote_at"] = None
    elif case == "received":
        row["received_at"] = None
    elif case == "observed":
        row["observed_at"] = None
    elif case == "stale":
        for key in ("source_quote_at", "received_at", "observed_at"):
            row[key] -= timedelta(minutes=30)
    elif case == "future":
        row["observed_at"] = NOW + timedelta(seconds=1)
    elif case == "old":
        row = fund(DAY-timedelta(days=3), amount)
    elif case == "wrong_clock_date":
        row["source_quote_at"] -= timedelta(days=1)
    if case != "missing":
        db.add(FundFlow(**row))
    db.add(StockSpot(code="000001", name="隔离", price=10, change_pct=3))
    db.add(BoardCons(code="000001", name="隔离", sector_code="S1"))
    db.add(StockSectorMapping(code="000001", sector_code="S1", sector_name="隔离板块"))
    db.add(SectorPersistence(sector_code="S1", trade_date=DAY, change_pct=3,
                             consecutive_days=3, fund_flow=sector_amount))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ok", "source", "clock", "received", "observed",
                                   "stale", "future", "old", "wrong_clock_date", "missing",
                                   "no_source", "version", "percentage"])
@pytest.mark.parametrize("amount", [123456789, 0, -123456789])
async def test_three_consumers_share_current_qualification(db, monkeypatch, case, amount):
    await seed_consumer(db, case, amount)
    scanner = AnomalyScanner()
    monkeypatch.setattr(scanner, "_load_leader_history_features", AsyncMock(return_value={}))
    monkeypatch.setattr(scanner, "_load_primary_industry_map", AsyncMock(return_value={}))
    single = (await scanner._assemble_sector_stocks(db, "S1", DAY, as_of_at=NOW))[0]
    bulk = (await scanner._assemble_many_sector_stocks(db, ["S1"], DAY, as_of_at=NOW))["S1"][0]
    resonance = await scanner.analyze_resonance("000001", db, DAY, as_of_at=NOW)
    for result in (single, bulk, resonance):
        assert result["main_net_inflow"] == (amount if case == "ok" else None)
        assert (result["main_fund_status"] == "ok") is (case == "ok")
        assert result["main_fund_status"] == single["main_fund_status"]
        assert result["main_fund_decision_at"] == NOW.isoformat()
        assert result["main_fund_display"] == single["main_fund_display"]
    expected = (30 if amount > 0 else 5) if case == "ok" else 0
    assert resonance["sectors"][0]["fund_score"] == expected
    assert resonance["best_resonance_score"] == 40 + 20 + expected
    assert resonance["purpose"] == "research_only"
    if case == "stale":
        display = single["main_fund_display"]
        assert display["available"] is True
        assert display["main_net_inflow"] == amount
        assert display["purpose"] == "display_only"
    if case in {"old", "missing"}:
        assert single["main_fund_display"] is None
    # Owned result, no DB writes or synthetic recovery by consumer.
    await db.rollback()
    rows = (await db.execute(select(FundFlow))).scalars().all()
    assert len(rows) == (0 if case == "missing" else 1)
    if rows:
        assert rows[0].main_net_inflow == amount


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,sector_amount,score", [(10, -2, 10), (-10, -2, 15),
                                                       (0, 0, 5), (10, None, 0)])
async def test_resonance_valid_numeric_branches_unchanged(db, amount, sector_amount, score):
    await seed_consumer(db, amount=amount, sector_amount=sector_amount)
    result = await AnomalyScanner().analyze_resonance("000001", db, DAY, as_of_at=NOW)
    assert result["sectors"][0]["fund_score"] == score
    assert result["sectors"][0]["sector_fund"] == sector_amount


@pytest.mark.asyncio
async def test_prior_sector_date_not_current_fund_and_postmarket_stays_dated(db):
    await seed_consumer(db)
    sector = await db.scalar(select(SectorPersistence))
    sector.trade_date -= timedelta(days=3)
    await db.commit()
    scanner = AnomalyScanner()
    result = await scanner.analyze_resonance("000001", db, DAY, as_of_at=NOW)
    assert result["sectors"][0]["sector_fund"] is None
    assert result["sectors"][0]["fund_score"] == 0
    result = await scanner.analyze_resonance("000001", db, DAY, as_of_at=NOW.replace(hour=17))
    assert result["main_net_inflow"] is None
    assert result["main_fund_display"]["main_net_inflow"] == 123456789
    assert result["main_fund_display"]["purpose"] == "display_only"
    assert result["main_fund_display"]["source_quote_at"].startswith("2026-09-14T10:")
    assert result["sectors"][0]["fund_score"] == 0


@pytest.mark.parametrize("value,status", [(None, "missing"), (1e9, "stale"),
                                          (float("inf"), "ok"), (float("nan"), "ok"),
                                          (True, "ok"), ("not-a-number", "ok")])
def test_dragon_unknown_never_earns_fund_bonus_or_becomes_zero(value, status):
    scanner = DragonHeadScanner()
    baseline = {"code": "000001", "name": "隔离", "change_pct": 3}
    unknown = {**baseline, "main_net_inflow": value, "main_fund_status": status,
               "main_fund_display": {"main_net_inflow": 1e9, "purpose": "display_only"}}
    actual = scanner.scan_sector("S1", "隔离", [unknown])[0]
    expected = scanner.scan_sector("S1", "隔离", [baseline])[0]
    assert actual.main_net_inflow is None
    assert actual.score == expected.score
    assert actual.trend_leadership_score == expected.trend_leadership_score
    assert "资金大幅流入" not in actual.reasons
    unknown["main_fund_display"]["main_net_inflow"] = 0
    assert actual.main_fund_display["main_net_inflow"] == 1e9


@pytest.mark.parametrize("value", [0, -100, 100])
def test_dragon_preserves_finite_signed_amounts(value):
    result = DragonHeadScanner().scan_sector("S1", "隔离", [
        {"code": "000001", "main_net_inflow": value, "main_fund_status": "ok"}])[0]
    assert result.main_net_inflow == value
    assert ("资金大幅流入" in result.reasons) is (value > 0)
