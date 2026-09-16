"""复盘确定性修复：不可交易股票不能挤占A召回；空可行域不是账户暂停。"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.models.stock import StockSpot, StockTag, StockBlacklist, StockSectorMapping, SectorPersistence
from test_paper_api import paper_client


@pytest.mark.parametrize("underwater", [False, True])
@pytest.mark.asyncio
async def test_ineligible_reversal_rows_do_not_consume_candidate_cap(paper_client, underwater, monkeypatch):
    _, maker = paper_client
    day = date(2026, 9, 7)
    at = datetime(2026, 9, 7, 10)
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    from app.models.stock import FundFlow
    valid = dict(code="600902", name="主板合格样本", price=10.2, prev_close=10,
                 open=9.8, low=9.7, high=10.25, change_pct=2, avg_price=10,
                 volume_ratio=1.5, main_net_inflow=1_000_000,
                 orderbook_imbalance=.5, min5_change=.1)
    # 每一组均超过召回上限；它们通过价格预筛但没有交易资格。
    other_board = [dict(valid, code=f"300{i:03d}", main_net_inflow=2_000_000+i) for i in range(150)]
    tagged = [dict(valid, code=f"601{i:03d}", main_net_inflow=3_000_000+i) for i in range(150)]
    named = [dict(valid, code=f"603{i:03d}", name="ST样本", main_net_inflow=4_000_000+i) for i in range(150)]
    blocked = dict(valid, code="605001", main_net_inflow=8_000_000)
    records = [*other_board, *tagged, *named, blocked, valid]
    fn = paper._underwater_reversal_candidates if underwater else paper._green_limit_reversal_candidates
    async with maker() as db:
        db.add_all([StockSpot(**row) for row in records])
        # 独立合格资金fixture；资格过滤应先于资金排序和截断。
        db.add_all([FundFlow(code=row["code"], trade_date=day, main_net_inflow=1_000_000,
            main_net_inflow_pct=2, source="eastmoney", source_version="individual_fund_flow_v3_f124",
            source_quote_at=at, received_at=at, observed_at=at) for row in records])
        db.add_all([StockTag(code=row["code"], board_type="main_sh", board_tag="tradeable", is_suspended=True) for row in tagged])
        db.add(StockBlacklist(code="605001", reason="suspended", start_date=day))
        db.add_all([
            StockSectorMapping(code="600902", sector_code="valid", sector_name="主线",
                               sector_type="concept", source="test"),
            SectorPersistence(sector_code="valid", sector_name="主线", trade_date=day,
                              strength_score=85, change_pct=2, fund_flow=20, limit_up_count=5),
        ])
        await db.commit()
        sql, _ = await fn(db, limit=5, trade_date=day)
        at = datetime(2026, 9, 7, 10)
        token = paper._QUOTE_ROUND_CONTEXT.set(
            {"round_id": "universe", "records": records, "committed_at": at, "as_of_at": at})
        try:
            frozen, _ = await fn(db, limit=5, trade_date=day)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert [x["code"] for x in sql] == ["600902"]
    assert [x["code"] for x in frozen] == ["600902"]


@pytest.mark.parametrize("code,name", [
    ("300001", "创业板"), ("688001", "科创板"), ("920821", "北交所"),
    ("600001", "*ST样本"), ("600001", "样本退"), ("600abc", "无效"),
])
def test_round_prefilter_rejects_ineligible_domain(code, name):
    assert not paper._round_reversal_prefilter(SimpleNamespace(code=code, name=name), underwater=True)


def test_a_price_band_reports_empty_without_changing_thresholds(monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_INTRADAY_CONFIRM_MAX_PULLBACK_FROM_HIGH_PCT", 2.0)
    monkeypatch.setattr(paper.settings, "PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT", 3.0)
    band = paper._a_entry_price_band(SimpleNamespace(high=10.92, low=10.2, change_pct=2))
    assert band["status"] == "empty"
    assert band["lower_price"] == pytest.approx(10.7016)
    assert band["upper_price"] == pytest.approx(10.506)
    assert band["sufficient_for_entry"] is False
    # 恰好相等仍有必要交集；并未保证盘口价格和所有其余门槛通过。
    boundary = paper._a_entry_price_band(SimpleNamespace(high=10.3/.98, low=10, change_pct=1))
    assert boundary["status"] == "nonempty"


@pytest.mark.parametrize("fields", [
    {}, {"high":10,"low":None,"change_pct":1},
    {"high":10,"low":0,"change_pct":1},
    {"high":10,"low":9,"change_pct":-1},
])
def test_missing_or_nonpositive_change_does_not_fabricate_price_band(fields):
    assert paper._a_entry_price_band(SimpleNamespace(**fields))["status"] == "not_applicable"
