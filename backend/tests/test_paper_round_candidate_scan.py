"""生产行情轮次候选链路：属性/字典契约与截断前预筛回归。"""
from datetime import date, datetime
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import paper
from app.models.stock import StockSpot, StockSectorMapping, SectorPersistence
from test_paper_api import paper_client

@pytest.mark.asyncio
@pytest.mark.parametrize("underwater", [False, True])
async def test_round_candidates_match_sql_and_prefilter_before_cap(paper_client, underwater, monkeypatch):
    _, maker = paper_client
    at = datetime(2026, 9, 7, 10)
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    from app.models.stock import FundFlow
    valid = dict(code="600902", name="水下翻红样本", price=10.1, prev_close=10,
        open=9.8, low=9.7, high=10.2, change_pct=1, avg_price=10, volume_ratio=1.5,
        main_net_inflow=1_000_000, orderbook_imbalance=.5, min5_change=.1)
    if not underwater:
        valid.update(price=10.2, change_pct=2, high=10.25)
    decoys = [dict(valid, code=f"6{i:05d}", name="未曾水下", price=10.5, open=10.1,
        low=9.99, change_pct=5, main_net_inflow=2_000_000+i) for i in range(150)]
    records = [*decoys, valid]
    fn = paper._underwater_reversal_candidates if underwater else paper._green_limit_reversal_candidates
    async with maker() as db:
        db.add_all([StockSpot(**row) for row in records])
        db.add(FundFlow(code="600902", trade_date=at.date(), main_net_inflow=1_000_000,
            main_net_inflow_pct=2, source="eastmoney", source_version="individual_fund_flow_v3_f124",
            source_quote_at=at, received_at=at, observed_at=at))
        db.add_all([
            StockSectorMapping(code="600902", sector_code="valid", sector_name="主线",
                               sector_type="concept", source="test"),
            SectorPersistence(sector_code="valid", sector_name="主线", trade_date=at.date(),
                              strength_score=85, change_pct=2, fund_flow=20, limit_up_count=5),
        ])
        await db.commit()
        sql, _ = await fn(db, limit=5, trade_date=at.date())
        # DB rows then change; the immutable quote route must continue to use captured data.
        spot = await db.get(StockSpot, "600902")
        spot.price = 9
        await db.commit()
        token = paper._QUOTE_ROUND_CONTEXT.set({"round_id":"actual-shape", "records":records,
            "committed_at":at, "as_of_at":at, "quality_status":"ok"})
        try:
            frozen, _ = await fn(db, limit=5, trade_date=at.date())
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert [x["code"] for x in sql] == ["600902"]
    assert [x["code"] for x in frozen] == ["600902"]
    assert frozen[0]["price"] == sql[0]["price"] == valid["price"]


@pytest.mark.asyncio
async def test_round_candidate_scan_handles_missing_fields_without_database_fallback(paper_client, monkeypatch):
    _, maker = paper_client
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id":"missing-fields", "records":[{"code":"600001"}]})
    try:
        async with maker() as db:
            for fn in (paper._underwater_reversal_candidates, paper._green_limit_reversal_candidates):
                result, _ = await fn(db, limit=5, trade_date=date(2026,9,7))
                assert result == []
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
