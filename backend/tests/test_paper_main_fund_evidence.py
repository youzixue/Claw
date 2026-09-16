"""隔离资金消费回归：测试库/固定时钟，不请求实源，不调用下单链路。"""
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.models.stock import FundFlow, StockSpot
from test_paper_api import paper_client

AT = datetime(2026, 9, 7, 10)
CODE = "600902"


def fund_row(amount=1_000_000, **changes):
    values = dict(
        code=CODE, name="独立资金样本", trade_date=AT.date(),
        main_net_inflow=amount, main_net_inflow_pct=2,
        source="eastmoney", source_version="individual_fund_flow_v3_f124",
        source_quote_at=AT - timedelta(seconds=5),
        received_at=AT - timedelta(seconds=3), observed_at=AT - timedelta(seconds=1),
    )
    values.update(changes)
    return FundFlow(**values)


def quote():
    return dict(code=CODE, name="水下翻红样本", price=10.2, prev_close=10,
                open=9.8, low=9.7, high=10.25, change_pct=2, avg_price=10,
                volume_ratio=1.5, main_net_inflow=999_999_999,
                orderbook_imbalance=.5, min5_change=.1)


@pytest.fixture
def fixed_paper(monkeypatch):
    monkeypatch.setattr(paper, "_paper_now", lambda: AT)
    monkeypatch.setattr(paper, "_latest_sector_context_for_code", AsyncMock(return_value=(
        {"sector_strength": 85, "sector_change_pct": 2, "sector_fund_flow": 20,
         "sector_limit_up_count": 5}, "固定测试主线")))
    monkeypatch.setattr(paper, "_paper_effective_min5_change", lambda *a, **kw: .1)


@pytest.mark.asyncio
@pytest.mark.parametrize("underwater", [False, True])
@pytest.mark.parametrize("round_mode", [False, True])
@pytest.mark.parametrize("kind", ["positive", "negative", "zero", "missing", "stale", "future"])
async def test_candidate_funds_not_legacy_spot(paper_client, fixed_paper, underwater, round_mode, kind):
    _, maker = paper_client
    record = quote()
    payload = {"round_id": "immutable", "records": [record], "as_of_at": AT, "committed_at": AT}
    original = deepcopy(payload)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload if round_mode else {})
    try:
        async with maker() as db:
            db.add(StockSpot(**record))
            if kind != "missing":
                changes = {}
                if kind == "stale":
                    changes = dict(source_quote_at=AT-timedelta(hours=2),
                                   received_at=AT-timedelta(hours=2), observed_at=AT-timedelta(hours=2))
                if kind == "future":
                    changes = dict(received_at=AT+timedelta(seconds=1), observed_at=AT+timedelta(seconds=1))
                db.add(fund_row({"negative": -1_000_000, "zero": 0}.get(kind, 1_000_000), **changes))
            await db.commit()
            fn = paper._underwater_reversal_candidates if underwater else paper._green_limit_reversal_candidates
            candidates, notes = await fn(db, limit=5, trade_date=AT.date())
        if kind == "positive":
            assert len(candidates) == 1
            assert candidates[0]["main_net_inflow"] == 1_000_000
            evidence = candidates[0]["main_fund_evidence"]
            assert evidence["dataset"] == "fund_flow"
            assert evidence["source"] == "eastmoney"
            assert "quote_round_id" not in evidence
        else:
            assert candidates == []
            if kind in {"missing", "stale", "future"}:
                assert "unknown" in " ".join(notes)
        assert payload == original  # never retrofit acquired funds into quote records
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [1_000_000, -1_000_000, 0, None])
@pytest.mark.parametrize("source", ["green_limit_reversal", "underwater_reversal"])
async def test_execution_rechecks_real_funds_not_candidate_or_spot(paper_client, fixed_paper, amount, source):
    _, maker = paper_client
    candidate = {"code": CODE, "_source": source, "main_net_inflow": 8_000_000,
                 "execution_confirmation": True}
    async with maker() as db:
        if amount is not None:
            db.add(fund_row(amount))
        await db.commit()
        reason = await paper._confirm_candidate_main_fund(
            db, candidate, SimpleNamespace(**quote()), trade_date=AT.date(), decision_at=AT)
    assert bool(reason) == (amount is None or amount <= 0)
    assert candidate["main_net_inflow"] == amount
    assert candidate["main_fund_evidence"]["status"] == ("unknown" if amount is None else "known")


@pytest.mark.asyncio
async def test_late_received_fund_cannot_be_backfilled_into_round(paper_client, fixed_paper):
    _, maker = paper_client
    payload = {"round_id": "earlier", "records": [quote()], "as_of_at": AT,
               "committed_at": AT + timedelta(seconds=30)}
    original = deepcopy(payload)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        async with maker() as db:
            db.add(fund_row(received_at=AT+timedelta(seconds=10), observed_at=AT+timedelta(seconds=11)))
            await db.commit()
            assert await paper._paper_main_fund_map(
                db, trade_date=AT.date(), decision_at=AT+timedelta(seconds=30)) == {}
            assert payload == original
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("as_of", [None, "2026-09-07T10:00:00", AT-timedelta(days=1)])
async def test_invalid_round_cutoff_fails_closed_without_fund_query(monkeypatch, as_of):
    from app.data import main_fund
    loader = AsyncMock(side_effect=AssertionError("invalid cutoff cannot query"))
    monkeypatch.setattr(main_fund, "load_current_main_fund_map", loader)
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id": "bad", "as_of_at": as_of})
    try:
        assert await paper._paper_main_fund_map(None, trade_date=AT.date(), decision_at=AT) == {}
        loader.assert_not_called()
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["ma5_pullback", "icepoint_reversal", "tenbagger_midline", "reversal_pullback", "momentum_first_retest"])
async def test_book_alternatives_and_nonfund_strategies_preserved(paper_client, fixed_paper, source):
    _, maker = paper_client
    candidate = {"code": CODE, "_source": source}
    spot = SimpleNamespace(**dict(quote(), support_strength_score=60, bid_ratio=20))
    async with maker() as db:
        reason = await paper._confirm_candidate_main_fund(db, candidate, spot, trade_date=AT.date(), decision_at=AT)
    assert reason == ""
    if source in {"ma5_pullback", "icepoint_reversal"}:
        assert candidate["main_fund_evidence"]["status"] == "unknown"
    else:
        assert "main_fund_evidence" not in candidate


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [None, 0, -9_999_999])
async def test_real_positive_is_not_prefiltered_by_legacy_spot(paper_client, fixed_paper, legacy):
    _, maker = paper_client
    async with maker() as db:
        db.add(StockSpot(**dict(quote(), main_net_inflow=legacy)))
        db.add(fund_row())
        await db.commit()
        rows, _ = await paper._green_limit_reversal_candidates(db, limit=5, trade_date=AT.date())
    assert [row["code"] for row in rows] == [CODE]


@pytest.mark.asyncio
async def test_original_fund_rank_uses_real_amount_before_cap(monkeypatch):
    funds = {
        f"600{i:03}": {"main_net_inflow": float(i), "main_net_inflow_pct": 1}
        for i in range(1, 151)
    }
    monkeypatch.setattr(paper, "_paper_main_fund_map", AsyncMock(return_value=funds))
    rows = [SimpleNamespace(code=code, change_pct=1, main_net_inflow=-item["main_net_inflow"])
            for code, item in funds.items()]
    selected, _, _ = await paper._funded_reversal_rows(
        None, rows, trade_date=AT.date(), limit=1, underwater=True)
    assert len(selected) == 120
    assert selected[0].code == "600150"
    assert selected[-1].code == "600031"


@pytest.mark.asyncio
async def test_a2_price_path_does_not_query_or_require_main_funds(monkeypatch):
    from app.paper.experiment import PRICE_PATH_ACCOUNTS, sentiment_is_required
    assert "challenger_a" in PRICE_PATH_ACCOUNTS
    assert sentiment_is_required("challenger_a") is False
    loader = AsyncMock(side_effect=AssertionError("A2 must not acquire a new fund gate"))
    monkeypatch.setattr(paper, "_paper_main_fund_map", loader)
    candidate = {"code": CODE, "_source": "momentum_first_retest", "execution_confirmation": True}
    assert await paper._confirm_candidate_main_fund(
        None, candidate, SimpleNamespace(**quote()), trade_date=AT.date(), decision_at=AT) == ""
    loader.assert_not_called()
    assert candidate["execution_confirmation"] is True
    assert "main_fund_evidence" not in candidate


def test_nested_snapshot_funds_are_not_retained_or_mutated():
    original = {"main_net_inflow": 99_999, "main_net_inflow_pct": 99,
                "source": "eastmoney_main_fund", "observed_at": AT+timedelta(seconds=30),
                "sector_factors": [{"strength_score": 85}]}
    frozen = deepcopy(original)
    candidate = {"detail": original}
    paper._bind_main_fund_evidence(candidate, None)
    assert candidate["detail"]["main_net_inflow"] is None
    assert candidate["detail"]["observed_at"] is None
    assert candidate["detail"]["source"] == "unavailable"
    assert candidate["detail"]["main_fund_evidence"]["status"] == "unknown"
    assert original == frozen


@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("main_present", [False, True])
def test_binding_clears_all_unavailable_fund_breakdowns(known, main_present):
    fields = (
        "super_net_inflow", "super_net_inflow_pct", "big_net_inflow", "big_net_inflow_pct",
        "mid_net_inflow", "mid_net_inflow_pct", "small_net_inflow", "small_net_inflow_pct",
        "main_pct", "super_pct", "big_pct", "mid_pct", "small_pct",
    )
    original = {key: 999 for key in fields}
    original.update(price=10.2, orderbook_imbalance=.5, sector_factors=[{"strength_score": 85}])
    if main_present:
        original.update(main_net_inflow=999, main_net_inflow_pct=99)
    frozen = deepcopy(original)
    candidate = {"detail": original, "super_net_inflow": 999}
    fund = ({"main_net_inflow": 0.0, "main_net_inflow_pct": 0.0,
             "big_net_inflow": 0.0, "mid_net_inflow": -12.0, "small_net_inflow": 15.0}
            if known else None)
    paper._bind_main_fund_evidence(candidate, fund)
    detail = candidate["detail"]
    for key in fields:
        assert detail[key] == (fund or {}).get(key)
    assert candidate["super_net_inflow"] is None
    assert detail["is_stale"] is not known
    assert detail["main_net_inflow"] == (0.0 if known else None)
    assert {key: detail[key] for key in ("price", "orderbook_imbalance", "sector_factors")} == {
        key: original[key] for key in ("price", "orderbook_imbalance", "sector_factors")}
    assert original == frozen


def test_binding_does_not_change_nonfund_detail():
    detail = {"price": 10.2, "source": "quote", "observed_at": AT, "is_stale": False}
    candidate = {"detail": detail}
    paper._bind_main_fund_evidence(candidate, None)
    assert candidate["detail"] is detail
    assert detail == {"price": 10.2, "source": "quote", "observed_at": AT, "is_stale": False}


@pytest.mark.asyncio
async def test_earlier_decision_wins_over_round_as_of(monkeypatch):
    from app.data import main_fund
    loader = AsyncMock(return_value={})
    monkeypatch.setattr(main_fund, "load_current_main_fund_map", loader)
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id": "future-as-of", "as_of_at": AT+timedelta(seconds=30)})
    try:
        await paper._paper_main_fund_map(None, trade_date=AT.date(), decision_at=AT)
        assert loader.await_args.kwargs["decision_at"] == AT
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_real_zero_is_known_not_missing(paper_client, fixed_paper):
    _, maker = paper_client
    async with maker() as db:
        db.add(fund_row(0, main_net_inflow_pct=0))
        await db.commit()
        funds = await paper._paper_main_fund_map(db, trade_date=AT.date(), codes=[CODE], decision_at=AT)
    evidence = paper._main_fund_evidence(funds[CODE])
    assert evidence["main_net_inflow"] == 0
    assert evidence["main_net_inflow_pct"] == 0
    assert evidence["main_fund_evidence"]["status"] == "known"
