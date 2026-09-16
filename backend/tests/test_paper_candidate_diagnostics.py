"""按账户保留策略交集、真实数据等待与具体阈值，不回写历史诊断。"""
import json
from datetime import date, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog, PaperDailyOutcome
from app.models.stock import LimitUpPool, StockSpot
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot


@pytest.mark.asyncio
async def test_b_empty_is_intersection_not_probability_alone(paper_client, monkeypatch):
    _, maker = paper_client
    day = date(2026,9,7)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026,9,4)))
    monkeypatch.setattr(paper.settings, "PAPER_PROMOTION_MIN_PROBABILITY", .25)
    async with maker() as db:
        run = _governed_promotion_run(run_key="intersection", reference_trade_date=day,
            snapshot_context="promotion_0935", as_of_at=datetime(2026,9,7,9,35))
        db.add(run)
        await db.flush()
        db.add_all([
            _governed_promotion_snapshot(run_id=run.id, record_key="low-p", code="600001",
                prediction_trade_date=day, probability=.123),
            _governed_promotion_snapshot(run_id=run.id, record_key="watch", code="600002",
                prediction_trade_date=day, probability=.256, trade_gate_passed=False, actionable=False),
        ])
        await db.flush()
        diagnostics=[]
        candidates, _ = await paper._promotion_route_buy_candidates(
            db, limit=10, trade_date=day, account_name="promotion", diagnostics=diagnostics)
        assert not candidates
        assert diagnostics[0]["reason_code"] == "candidate_contract_empty"
        assert diagnostics[0]["candidate"]["funnel"] == {
            "route_pool":2, "trade_gate_nonwatch":1, "actionable":1,
            "ranked_or_recall":0, "probability_pass":0, "executable_intersection":0}


@pytest.mark.asyncio
async def test_d_quality_failure_is_explicit_data_gate(paper_client, monkeypatch):
    _, maker = paper_client
    day=date(2026,9,7)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026,9,4)))
    async with maker() as db:
        db.add(_governed_promotion_run(run_key="auction-missing", reference_trade_date=day,
            snapshot_context="promotion_0935", as_of_at=datetime(2026,9,7,9,35),
            route_gates={"auction_surge_start":{"gate_passed":False,"blocking_datasets":["auction_data"]}}))
        await db.flush()
        diagnostics=[]
        candidates, _=await paper._promotion_route_buy_candidates(
            db,limit=10,trade_date=day,account_name="auction",diagnostics=diagnostics)
        assert not candidates
        assert diagnostics[0]["stage_code"] == "data_gate"
        assert diagnostics[0]["candidate"]["blocking_datasets"] == ["auction_data"]


@pytest.mark.asyncio
async def test_e2_uses_own_seal_threshold_and_audits_units(paper_client, monkeypatch):
    _, maker=paper_client
    day=date(2026,9,8)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026,9,7)))
    monkeypatch.setattr(paper.settings,"PAPER_HIGHBOARD_MIN_SEAL_AMOUNT",.1)
    monkeypatch.setattr(paper.settings,"PAPER_CHALLENGER_E_MIN_SEAL_AMOUNT",1.0)
    async with maker() as db:
        db.add(LimitUpPool(code="605577",name="阈值样本",trade_date=date(2026,9,7),
                          consecutive_days=5,seal_amount=79_146_374,break_count=2,quarantined=False))
        db.add(StockSpot(code="605577",name="阈值样本",price=10.1,prev_close=10,
                        high=10.2,low=10,avg_price=10.05,change_pct=1,limit_up=11))
        await db.flush()
        main,_=await paper._tenbagger_midline_candidates(db,limit=10,trade_date=day)
        diagnostics=[]
        secondary,_=await paper._tenbagger_midline_candidates(db,limit=10,trade_date=day,
                                      account_name="challenger_e",diagnostics=diagnostics)
        assert len(main)==1 and not secondary
        reason=diagnostics[0]
        assert reason["code"]=="605577"
        assert reason["reason_code"]=="seal_amount_below_min"
        assert reason["metric_value"]==79_146_374
        assert reason["threshold_value"]==100_000_000
        assert reason["stage_code"]=="strategy_filter"


@pytest.mark.asyncio
async def test_daily_outcome_preserves_data_wait_and_counts_heartbeats(paper_client, monkeypatch):
    _, maker=paper_client
    at=datetime(2026,9,8,15,45)
    monkeypatch.setattr(paper.settings,"PAPER_CONTINUOUS_EXPERIMENT_ENABLED",True)
    # 此专项只验证汇总，不模拟控制样本、成交或修改生产库。
    monkeypatch.setattr(paper,"_record_control_sample",AsyncMock(return_value=None))
    async with maker() as db:
        account=await paper._get_or_create_account(db,"auction")
        version=paper._strategy_version("auction")
        def log(run, action, stage, reason, version_value=version):
            return PaperAutoTradeLog(account_id=account.id,trade_date=at.date(),
                created_at=at.replace(hour=10),run_id=run,action=action,decision="wait",
                source="candidate",reason="必要数据等待",stage_code=stage,reason_code=reason,
                strategy_version=version_value)
        db.add_all([
            log("scan-1","scan","runtime_gate","scan_started"),
            log("decision-1","candidate_reject","data_gate","candidate_data_missing"),
            log("old-scan","scan","runtime_gate","scan_started","old-version"),
        ])
        await db.flush()
        await paper.finalize_paper_daily_outcomes(db,trade_date=at.date(),observed_at=at)
        outcome=await db.scalar(select(PaperDailyOutcome).where(PaperDailyOutcome.account_id==account.id))
        assert outcome.terminal_status=="data_limited"
        assert outcome.scan_count==1
        assert outcome.decision_count==1
        details=json.loads(outcome.details_json)
        assert details["scan_count_basis"]=="scan_heartbeats"
        assert details["data_wait_count"]==1
        assert details["scope"]=="account_current_execution_version"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_name", "route", "target_board"),
    [
        ("promotion", "second_board_promotion", 2),
        ("mainline", "mainline_spread_start", 1),
        ("auction", "auction_surge_start", 1),
    ],
)
@pytest.mark.parametrize("future_field", ["as_of_at", "created_at", "completed_at"])
async def test_prediction_run_cannot_cross_decision_clock(
    paper_client, monkeypatch, account_name, route, target_board, future_field,
):
    """真实决策时点之后生成/完成的批次不能因写着当日上下文就进入候选。"""
    _, maker = paper_client
    decision_at = datetime(2026, 9, 8, 10, 0)
    known_at = datetime(2026, 9, 8, 9, 35)
    monkeypatch.setattr(paper, "_paper_now", lambda: decision_at)
    monkeypatch.setattr(
        paper.trade_calendar, "previous_trade_day",
        AsyncMock(return_value=date(2026, 9, 7)),
    )
    async with maker() as db:
        run = _governed_promotion_run(
            run_key="future-" + future_field,
            reference_trade_date=decision_at.date(),
            snapshot_context="promotion_0935",
            as_of_at=known_at,
        )
        run.created_at = known_at
        run.completed_at = known_at
        setattr(run, future_field, datetime(2026, 9, 8, 10, 0, 1))
        db.add(run)
        await db.flush()
        db.add_all([
            _governed_promotion_snapshot(
                run_id=run.id, record_key="future-candidate", code="600123",
                prediction_trade_date=decision_at.date(), route=route,
                target_board=target_board, probability=.95,
            ),
            StockSpot(
                code="600123", name="时点隔离样本", price=10.2, prev_close=10,
                change_pct=2, volume_ratio=1.5, limit_up=11,
            ),
        ])
        await db.flush()
        diagnostics = []
        candidates, notes = await paper._promotion_route_buy_candidates(
            db, limit=5, trade_date=decision_at.date(),
            account_name=account_name, diagnostics=diagnostics,
        )
        assert candidates == []
        assert any("时点" in note or "可见" in note for note in notes)
        assert diagnostics[0]["reason_code"] == "prediction_not_visible"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clock_case", "expected_code"),
    [
        ("known", "600124"),
        ("at_cutoff", "600124"),
        ("after_quote", None),
        ("completion_missing", None),
        ("quote_clock_missing", None),
        ("quote_clock_wrong_day", None),
        ("latest_failed", None),
    ],
)
async def test_prediction_clock_uses_quote_cutoff_without_old_batch_fallback(
    paper_client, monkeypatch, clock_case, expected_code,
):
    _, maker = paper_client
    day = date(2026, 9, 8)
    cutoff = datetime(2026, 9, 8, 9, 40)
    monkeypatch.setattr(
        paper.trade_calendar, "previous_trade_day",
        AsyncMock(return_value=date(2026, 9, 7)),
    )
    async with maker() as db:
        older = _governed_promotion_run(
            run_key="old-visible", reference_trade_date=day,
            snapshot_context="promotion_0925", as_of_at=datetime(2026, 9, 8, 9, 25),
        )
        latest = _governed_promotion_run(
            run_key="latest-clock", reference_trade_date=day,
            snapshot_context="promotion_0935", as_of_at=datetime(2026, 9, 8, 9, 35),
            status="failed" if clock_case == "latest_failed" else "completed",
        )
        if clock_case == "at_cutoff":
            latest.completed_at = cutoff
        elif clock_case == "after_quote":
            latest.completed_at = cutoff.replace(second=1)
        elif clock_case == "completion_missing":
            latest.completed_at = None
        db.add_all([older, latest])
        await db.flush()
        for run, code in [(older, "600125"), (latest, "600124")]:
            db.add(_governed_promotion_snapshot(
                run_id=run.id, record_key=code, code=code,
                prediction_trade_date=day, probability=.95,
            ))
        await db.flush()
        quote_as_of = cutoff
        if clock_case == "quote_clock_missing":
            quote_as_of = None
        elif clock_case == "quote_clock_wrong_day":
            quote_as_of = datetime(2026, 9, 7, 9, 40)
        token = paper._QUOTE_ROUND_CONTEXT.set({
            "round_id": "test-fixed-clock", "as_of_at": quote_as_of,
            "committed_at": cutoff.replace(second=5),
            "records_by_code": {
                code: {
                    "code": code, "name": code, "price": 10.2, "prev_close": 10,
                    "change_pct": 2, "volume_ratio": 1.5, "limit_up": 11,
                } for code in ["600124", "600125"]
            },
        })
        try:
            candidates, notes = await paper._promotion_route_buy_candidates(
                db, limit=5, trade_date=day, account_name="promotion",
                now=datetime(2026, 9, 8, 9, 50),
            )
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert [item["code"] for item in candidates] == (
            [expected_code] if expected_code else []
        )
        if expected_code is None:
            assert any("禁止回退旧批次" in note for note in notes)


@pytest.mark.asyncio
async def test_mainline_rank_eligibility_does_not_promote_unranked_candidates(
    paper_client, monkeypatch,
):
    _, maker = paper_client
    at = datetime(2026, 9, 8, 10, 5)
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    monkeypatch.setattr(
        paper.trade_calendar, "previous_trade_day",
        AsyncMock(return_value=date(2026, 9, 7)),
    )
    async with maker() as db:
        run = _governed_promotion_run(
            run_key="mainline-unranked", reference_trade_date=at.date(),
            snapshot_context="promotion_1000", as_of_at=at.replace(minute=0),
        )
        db.add(run)
        await db.flush()
        for i, trade_gate in enumerate([True, True, False]):
            snapshot = _governed_promotion_snapshot(
                run_id=run.id, record_key=f"unranked-{i}", code=f"60012{i}",
                prediction_trade_date=at.date(), target_board=1,
                route="mainline_spread_start", probability=.95,
                trade_gate_passed=trade_gate, actionable=False,
            )
            snapshot.rank_scope = "pool_unranked"
            snapshot.features_json = json.dumps({"prediction_rank_eligible": True})
            db.add(snapshot)
        await db.flush()
        diagnostics = []
        candidates, _ = await paper._promotion_route_buy_candidates(
            db, limit=5, trade_date=at.date(), account_name="mainline",
            diagnostics=diagnostics,
        )
        assert candidates == []
        funnel = diagnostics[0]["candidate"]["funnel"]
        assert funnel["route_pool"] == 3
        assert funnel["prediction_rank_eligible"] == 3
        assert funnel["rank_eligible_unranked"] == 3
        assert funnel["trade_eligible_unranked"] == 2
        assert funnel["ranked_or_recall"] == 0
        assert funnel["executable_intersection"] == 0
        assert "排名资格不等于入榜" in diagnostics[0]["reason"]
