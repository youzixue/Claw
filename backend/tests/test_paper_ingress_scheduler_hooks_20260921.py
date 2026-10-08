"""Real scheduler owners with isolated/fake sessions; no lifecycle/network."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
import pytest
from app.data import scheduler as module
from app.api.v1 import paper
from app.paper import strategy_iteration_challenger as challengers
from app.paper import strategy_iteration_shadow as shadows
from app.push import paper_buy_points as points
from test_paper_buy_points import setup, logs
from test_paper_buy_point_reliability_20260921 import failed_entry

@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False,True])
async def test_all_seven_primary_owners_retry_only_after_close_even_on_rollback(monkeypatch, failed):
    scheduler=module.DataScheduler()
    states=[]
    class Session:
        def __init__(self): self.closed=False; self.rolled=False; self.info={}
        async def __aenter__(self): return self
        async def __aexit__(self,*args): self.closed=True
        async def rollback(self): self.rolled=True
    def factory():
        session=Session()
        states.append(session)
        return session
    async def run(session,**kwargs):
        session.info["account"]=kwargs["account_name"]
        if failed: raise RuntimeError("isolated business error")
        return {"summary":{}}
    async def retry(session):
        assert session.closed
        assert session.rolled is failed
        session.info["retried"]=True
    monkeypatch.setattr(module,"async_session",factory)
    monkeypatch.setattr(paper,"run_paper_auto_trade",AsyncMock(side_effect=run))
    monkeypatch.setattr(scheduler,"_recover_paper_notification_ingress",AsyncMock(side_effect=retry))
    await scheduler._run_paper_accounts_isolated(execute=True,trigger="test",execution_mode="intraday")
    assert {s.info["account"] for s in states} == set(paper.PAPER_SCAN_ACCOUNTS)
    assert len(states)==7 and all(s.info["retried"] for s in states)

@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False,True])
async def test_connected_five_owner_retries_after_close(monkeypatch,failed):
    scheduler=module.DataScheduler()
    sessions=[]
    class Session:
        def __init__(self): self.closed=False; self.rolled=False; self.info={}
        async def __aenter__(self): sessions.append(self); return self
        async def __aexit__(self,*args): self.closed=True
        async def rollback(self): self.rolled=True
    async def run(session,**kwargs):
        session.info["account"]=kwargs["account_name"]
        if failed: raise RuntimeError("isolated challenger error")
        return {"status": "completed"}
    recovered=[]
    async def retry(session):
        assert session.closed
        assert session.rolled is failed
        assert session.info["account"] in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.values()
        recovered.append(session)
    monkeypatch.setattr(module,"async_session",Session)
    monkeypatch.setattr(scheduler,"_drain_momentum_quote_rounds",AsyncMock(return_value={"status": "completed"}))
    monkeypatch.setattr(shadows,"scan_strategy_iteration_shadow",AsyncMock(return_value={}))
    monkeypatch.setattr(challengers,"run_strategy_iteration_challenger_accounts",AsyncMock(side_effect=run))
    monkeypatch.setattr(scheduler,"_recover_paper_notification_ingress",AsyncMock(side_effect=retry))
    await scheduler._process_quote_round_shadow({"committed_at":datetime(2026,9,21,10),"records":[]})
    assert len(recovered)==5
    assert {s.info["account"] for s in recovered} == set(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.values())

@pytest.mark.asyncio
async def test_scheduler_hook_really_commits_original_failed_entry_after_close(setup,monkeypatch):
    maker,now,send=setup
    db=await failed_entry(maker,now)
    await db.close()
    monkeypatch.setattr(module,"async_session",maker)
    await module.DataScheduler()._recover_paper_notification_ingress(db)
    assert len([r for r in await logs(maker) if r.action==points.SIGNAL])==1
    assert not send.called

@pytest.mark.asyncio
async def test_notification_timeout_does_not_change_trade_state_or_wait_forever(monkeypatch):
    from types import SimpleNamespace
    scheduler=module.DataScheduler()
    db=SimpleNamespace(info={points._FAILED_INGRESS:{"event":{}}})
    canceled=asyncio.Event()
    async def stalled(*args,**kwargs):
        try: await asyncio.Event().wait()
        finally: canceled.set()
    monkeypatch.setattr(points,"retry_failed_buy_points",stalled)
    monkeypatch.setattr(module.settings,"PAPER_BUY_POINT_PUSH_INTERVAL_SEC",0.1)
    await asyncio.wait_for(scheduler._recover_paper_notification_ingress(db),timeout=1)
    assert canceled.is_set() and db.info[points._FAILED_INGRESS]

@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False,True])
async def test_manual_auto_endpoint_also_releases_session_before_recovery(monkeypatch, failed):
    from types import SimpleNamespace
    db=SimpleNamespace(info={points._FAILED_INGRESS:{"event":{}}},closed=False)
    async def close(): db.closed=True
    db.close=close
    async def run(*args,**kwargs):
        if failed: raise RuntimeError("original failure")
        return {"summary":{}}
    async def retry(session):
        assert session.closed
        return {"recovered":1}
    monkeypatch.setattr(paper,"run_paper_auto_trade",run)
    check=AsyncMock(side_effect=retry)
    monkeypatch.setattr(points,"retry_failed_buy_points",check)
    req=paper.AutoRunRequest(execute=False,execution_mode="manual")
    if failed:
        with pytest.raises(RuntimeError,match="original failure"):
            await paper.paper_auto_run(req,account_name="default",db=db)
    else:
        assert await paper.paper_auto_run(req,account_name="default",db=db)=={"summary":{}}
    check.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success","business_error","cancel"])
@pytest.mark.parametrize("close_fails", [False,True])
async def test_manual_optional_cleanup_cannot_replace_business_error_or_cancel(monkeypatch,outcome,close_fails):
    from types import SimpleNamespace
    class BusinessError(Exception): pass
    class CloseError(Exception): pass
    db=SimpleNamespace(info={points._FAILED_INGRESS:{"event":{}}})
    async def close():
        if close_fails: raise CloseError("cleanup")
    db.close=AsyncMock(side_effect=close)
    async def run(*args,**kwargs):
        if outcome=="business_error":raise BusinessError("original")
        if outcome=="cancel":raise asyncio.CancelledError()
        return {"summary":{}}
    monkeypatch.setattr(paper,"run_paper_auto_trade",run)
    retry=AsyncMock()
    monkeypatch.setattr(points,"retry_failed_buy_points",retry)
    req=paper.AutoRunRequest(execute=False,execution_mode="manual")
    if outcome=="success":
        assert await paper.paper_auto_run(req,account_name="default",db=db)=={"summary":{}}
    else:
        with pytest.raises(BusinessError if outcome=="business_error" else asyncio.CancelledError):
            await paper.paper_auto_run(req,account_name="default",db=db)
    if close_fails or outcome=="cancel":retry.assert_not_awaited()
    else:retry.assert_awaited_once()
    if outcome=="cancel":db.close.assert_not_awaited()

@pytest.mark.asyncio
async def test_real_task_cancel_skips_optional_ingress_cleanup(monkeypatch):
    from types import SimpleNamespace
    db=SimpleNamespace(info={points._FAILED_INGRESS:{"event":{}}},close=AsyncMock())
    entered=asyncio.Event()
    async def run(*args,**kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(paper,"run_paper_auto_trade",run)
    retry=AsyncMock()
    monkeypatch.setattr(points,"retry_failed_buy_points",retry)
    req=paper.AutoRunRequest(execute=False,execution_mode="manual")
    task=asyncio.create_task(paper.paper_auto_run(req,account_name="default",db=db))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    retry.assert_not_awaited()
    db.close.assert_not_awaited()


def test_health_exposes_owned_latest_clock_evidence_without_db():
    scheduler=module.DataScheduler()
    scheduler._quote_consumer_health={"status":"completed","stages_ms":{"entries":12}}
    view=scheduler.get_pipeline_runtime_status()["quote_consumer"]
    assert view["scope"]=="latest_event_consumer_attempt_current_process_only"
    assert view["commit_clock_available"] is False
    view["stages_ms"]["entries"]=0
    assert scheduler._quote_consumer_health["stages_ms"]["entries"]==12
