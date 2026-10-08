"""Real append/rollback/API receipts; no live DB, broker, network or push."""
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog, PaperTradeLog
from app.paper.candidate_audit import append_candidate_audits, promotion_trace, ACTION
from test_paper_api import paper_client
from test_promotion_candidate_trace_20260923 import seed, candidate_clock, DAY, AT


@pytest.mark.asyncio
async def test_audit_appends_transitions_and_references_unchanged_with_one_flush(
    paper_client, candidate_clock, monkeypatch,
):
    _, maker = paper_client
    async with maker() as db:
        run, rows = await seed(db)
        account = await paper._get_or_create_account(db, "promotion")
        await db.commit()
        kwargs = dict(append_log=paper._add_auto_log, account_id=account.id,
                      strategy_version=paper._strategy_version("promotion"), run_id="scan-1",
                      trade_date=DAY, trigger="isolated")
        def item(reason="candidate_selected", **extra):
            return promotion_trace(rows[0], run, cutoff=AT, decision_at=AT,
                reason_code=reason, reason=reason, probability=.8, **extra)
        flush = AsyncMock(wraps=db.flush)
        monkeypatch.setattr(db, "flush", flush)
        first, summary = await append_candidate_audits(db, [item(), item()], **kwargs)
        assert len(first) == 1 and flush.await_count == 1
        assert summary == {"evaluated": 2, "appended": 1, "unchanged": 1,
                           "reused_log_ids": [first[0].id]}
        assert first[0].executed_trade_id is None
        await db.commit()
        original_id = first[0].id
        again, summary = await append_candidate_audits(db, [item(metrics={"price": 10.16})], **kwargs)
        assert not again and summary["reused_log_ids"] == [original_id]
        different, _ = await append_candidate_audits(db, [item("candidate_quote_missing")], **kwargs)
        assert len(different) == 1
        await db.rollback()
        # Rollback never leaves a fictitious completion or a process-local dedup mark.
        row = await db.get(PaperAutoTradeLog, original_id)
        diagnostic = {key: value for key, value in item_template(row).items()}
        diagnostic["candidate"]["candidate_trace"]["state_key"] = "new-state-after-rollback"
        replay, _ = await append_candidate_audits(db, [diagnostic], **kwargs)
        assert len(replay) == 1
        await db.commit()
        new_version, _ = await append_candidate_audits(db, [diagnostic], **{**kwargs, "strategy_version": "different-version"})
        assert len(new_version) == 1
        diagnostic["candidate"]["candidate_trace"]["decision_at"] = (AT+timedelta(days=1)).isoformat()
        other_day, _ = await append_candidate_audits(db, [diagnostic], **{**kwargs, "trade_date": DAY+timedelta(days=1)})
        assert len(other_day) == 1
        await db.rollback()
        assert not (await db.scalars(select(PaperTradeLog))).all()


@pytest.mark.asyncio
async def test_future_audit_row_cannot_suppress_current_observation(paper_client, candidate_clock):
    _, maker = paper_client
    async with maker() as db:
        run, rows = await seed(db)
        account = await paper._get_or_create_account(db, "promotion")
        version = paper._strategy_version("promotion")
        diagnostic = promotion_trace(rows[0], run, cutoff=AT, decision_at=AT,
            reason_code="candidate_selected", reason="selected", probability=.8)
        future = await paper._add_auto_log(db, account_id=account.id, strategy_version=version,
            run_id="future-scan", trade_date=DAY, trigger="isolated", source="candidate",
            action=ACTION, decision="candidate", created_at=AT+timedelta(seconds=5), **diagnostic)
        await db.commit()
        logs, summary = await append_candidate_audits(db, [diagnostic], append_log=paper._add_auto_log,
            account_id=account.id, strategy_version=version, run_id="current-scan",
            trade_date=DAY, trigger="isolated")
        assert len(logs) == 1 and logs[0].id != future.id
        assert summary["unchanged"] == 0


@pytest.mark.asyncio
async def test_mainline_live_value_changes_do_not_duplicate_same_predicate(paper_client,candidate_clock,monkeypatch):
    _,maker=paper_client
    monkeypatch.setattr(paper.settings,"PAPER_MAINLINE_LIVE_CONFIRM_ENABLED",True)
    monkeypatch.setattr(paper.settings,"PAPER_MAINLINE_LIVE_CONFIRM_MIN_SECTOR_STRENGTH",50)
    async with maker() as db:
        run,rows=await seed(db,"mainline",specs=[{"rank_scope":"recall_ranked",
            "features_json":json.dumps({"broad_rotation_member_setup":True,"sector_catalyst_spread":True,
            "strict_confirmation_count":99,"sector_strength_score":99})}])
        row=rows[0]
        keys=[];codes=[]
        for strength in (10,11,99):
            audit={}
            reason=paper._promotion_mainline_live_confirm_reject_reason(row,run,trade_date=DAY,
                sector_context={"sector_trade_date":DAY.isoformat(),"sector_strength":strength,
                                "sector_observed_at":(AT-timedelta(seconds=1)).isoformat(),
                                "sector_change_pct":1,"sector_fund_flow":-1,"sector_limit_up_count":3},
                visible_at=AT,diagnostic=audit)
            codes.append(audit["predicate_code"])
            diagnostic=promotion_trace(row,run,cutoff=AT,decision_at=AT,
                reason_code="candidate_mainline_unconfirmed",reason=reason,
                probability=.8,metrics={"full_reason":reason,**audit})
            keys.append(diagnostic["candidate"]["candidate_trace"]["state_key"])
        assert codes==["sector_strength","sector_strength","sector_flow"]
        assert keys[0]==keys[1] and keys[1]!=keys[2]


def item_template(log):
    return dict(code=log.code, name=log.name, reason=log.reason, stage_code=log.stage_code,
                reason_code=log.reason_code, candidate=json.loads(log.candidate_json))


@pytest.mark.asyncio
@pytest.mark.parametrize("account_name", ["promotion", "mainline", "auction"])
async def test_real_main_loop_commits_stock_traces_and_reuse_receipt_visible_via_api(
    paper_client, candidate_clock, monkeypatch, account_name,
):
    client, maker = paper_client
    monkeypatch.setattr(paper, "_should_run_intraday_auto_trade", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_paper_order_window_status", AsyncMock(return_value=(True, "")))
    async with maker() as db:
        await seed(db, account_name, specs=[{"watch_only": True}, {"trade_gate_passed": False}])
        results = [await paper.run_paper_auto_trade(db, account_name=account_name, now=AT,
            execute=False, include_position_risk=False) for _ in range(2)]
        for result in results:
            receipts = [r for r in result["logs"] if r["action"] == "candidate_scan"]
            assert len(receipts) == 1
            assert receipts[0]["candidate_audit_summary"]["evaluated"] == 2
        assert results[1]["logs"][0]["executed_trade_id"] is None
        audits = list((await db.scalars(select(PaperAutoTradeLog).where(PaperAutoTradeLog.action == ACTION))).all())
        assert len(audits) == 2
        second_receipt = next(r for r in results[1]["logs"] if r["action"] == "candidate_scan")
        assert second_receipt["candidate_audit_summary"]["appended"] == 0
        assert set(second_receipt["candidate_audit_summary"]["reused_log_ids"]) == {r.id for r in audits}
        assert not (await db.scalars(select(PaperTradeLog))).all()
    response = await client.get("/paper/auto/logs", params={"account_name": account_name, "limit": 100})
    assert response.status_code == 200
    # The existing API includes snapshots and exact reasons, not a new parallel endpoint.
    payload = response.json()
    body = json.dumps(payload)
    assert "candidate_watch_only" in body and "candidate_trade_gate_failed" in body
    assert "prediction_snapshot_id" in body and "candidate_trace_v1" in body
