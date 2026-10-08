"""C all-rank independent confirmation uses only sector evidence visible to the quote round."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import paper
from app.models.stock import StockSpot
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot

AT = datetime(2026, 9, 28, 10, 10)
DAY = AT.date()

@pytest.mark.asyncio
@pytest.mark.parametrize("rank_scope", ["ranked", "recall_ranked", "pool_unranked"])
@pytest.mark.parametrize("clock,allowed", [
    ("before", True), ("equal", True), ("after_round", False), ("future", False),
    ("missing", False), ("malformed", False), ("previous_day", False), ("aware", False),
])
async def test_real_c_candidate_chain_checks_sector_clock_for_every_rank(
    paper_client, monkeypatch, rank_scope, clock, allowed,
):
    _, maker = paper_client
    cutoff = AT - timedelta(seconds=20)
    clocks = {
        "before": cutoff-timedelta(seconds=1), "equal": cutoff,
        "after_round": cutoff+timedelta(seconds=1), "future": AT+timedelta(seconds=1),
        "missing": None, "malformed": "not-a-clock", "previous_day": cutoff-timedelta(days=1),
        "aware": cutoff.replace(tzinfo=timezone(timedelta(hours=8))),
    }
    observed = clocks[clock]
    sector = dict(sector_trade_date=DAY.isoformat(), sector_strength=75,
                  sector_change_pct=2, sector_fund_flow=10, sector_limit_up_count=7,
                  sector_observed_at=observed.isoformat() if isinstance(observed,datetime) else observed)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=DAY-timedelta(days=4)))
    monkeypatch.setattr(paper, "_promotion_mainline_live_sector_context", AsyncMock(return_value=sector))
    monkeypatch.setattr(paper.settings, "PAPER_MAINLINE_LIVE_CONFIRM_ENABLED", True)
    monkeypatch.setattr(paper.settings, "PAPER_MAINLINE_ROUTE_POOL_ENABLED", True)
    factors = dict(prediction_rank_contract_version="promotion_rank_contract_v1",
        prediction_rank_contract_complete=True, prediction_rank_eligible=True,
        broad_rotation_member_setup=True, sector_catalyst_spread=True,
        strict_confirmation_count=3, sector_strength_score=70)
    quote=dict(code="600002", name="isolated", price=10.1, prev_close=10,
               change_pct=1, limit_up=11, volume_ratio=1.5)
    token=paper._QUOTE_ROUND_CONTEXT.set({"round_id":"sector-clock", "as_of_at":cutoff,
        "committed_at":cutoff, "quality_status":"ok", "records":[quote]})
    try:
        async with maker() as db:
            run=_governed_promotion_run(run_key="c-clock", reference_trade_date=DAY,
                snapshot_context="promotion_1000", as_of_at=AT.replace(minute=0))
            db.add(run);await db.flush()
            row=_governed_promotion_snapshot(run_id=run.id, record_key="c-clock-row", code="600002",
                prediction_trade_date=DAY, probability=.09, target_board=1,
                route="mainline_spread_start", actionable=False, watch_only=False)
            row.rank_scope=rank_scope;row.features_json=json.dumps(factors)
            db.add_all([row,StockSpot(**quote)]);await db.commit()
            diagnostics=[]
            candidates,notes=await paper._promotion_route_buy_candidates(
                db,limit=5,trade_date=DAY,account_name="mainline",now=AT,diagnostics=diagnostics)
            assert bool(candidates) is allowed, (notes, diagnostics)
            assert row.actionable is False and row.features_json==json.dumps(factors)
            if not allowed:
                assert any("观测时点" in text or "可见截止" in text for text in notes),notes
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


def test_sector_clock_contract_rotates_only_c(monkeypatch):
    from app.paper import experiment
    names=experiment.EXPERIMENT_ACCOUNTS
    before={name:paper._strategy_version(name) for name in names}
    monkeypatch.setattr(experiment,"MAINLINE_SECTOR_CLOCK_CONTRACT_VERSION","different-clock")
    assert {name for name in names if paper._strategy_version(name)!=before[name]}=={"mainline"}
