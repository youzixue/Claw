"""Real dispatch owners must not hide account failures as successful rounds."""
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import paper
from app.data import scheduler as module
from app.paper import strategy_iteration_challenger as challengers
from app.paper import strategy_iteration_shadow as shadows
from test_paper_watchdog_challengers_20260921 import AT, env, event_once


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['event', 'watchdog'])
async def test_real_connected_failure_does_not_mark_round_complete(env, monkeypatch, entrypoint):
    s=env.scheduler
    monkeypatch.setattr(s, '_process_quote_round_shadow', module.DataScheduler._process_quote_round_shadow.__get__(s))
    monkeypatch.setattr(shadows,'scan_strategy_iteration_shadow',AsyncMock(return_value={}))
    monkeypatch.setattr(challengers,'run_strategy_iteration_challenger_accounts',AsyncMock(side_effect=RuntimeError('business flush failed')))
    if entrypoint=='event': await event_once(env)
    else:
        result = await s._paper_intraday_auto_trade()
        assert result['status'] == 'failed'
    assert s._last_quote_round_processed_id is None
    assert s._last_quote_round_processed_at is None
    assert s._paper_auto_trading_last_run_at is None
    assert not s._quote_dispatch_lock.locked()
    if entrypoint=='event': assert s._quote_consumer_health['status']=='failed'


@pytest.mark.asyncio
async def test_primary_failure_is_reported_after_other_accounts_continue(env, monkeypatch):
    s=env.scheduler;seen=[]
    monkeypatch.setattr(s,'_run_paper_accounts_isolated',module.DataScheduler._run_paper_accounts_isolated.__get__(s))
    monkeypatch.setattr(paper,'PAPER_SCAN_ACCOUNTS',('broken','next','last'))
    async def execute(db,**kw):
        seen.append(kw['account_name'])
        if kw['account_name']=='broken':raise RuntimeError('business failed')
        return {'summary':{}}
    monkeypatch.setattr(paper,'run_paper_auto_trade',execute)
    await event_once(env)
    assert seen==['broken','next','last']
    assert s._quote_consumer_health['status']=='failed'
    assert s._last_quote_round_processed_id is None
    # Connected accounts still receive the valid round; one primary must not
    # silently turn seven isolated owners into a global abort-before-others.
    s._process_quote_round_shadow.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_new_round_bypasses_old_success_debounce_without_replaying_success(env):
    s=env.scheduler
    await s._paper_intraday_auto_trade()
    assert s._last_quote_round_processed_id=='watchdog-1'
    env.clock.value += timedelta(seconds=10)
    env.payload.update(round_id='new-failed-round',committed_at=env.clock.value,as_of_at=env.clock.value)
    s._process_quote_round_shadow.side_effect=RuntimeError('new attempt failed')
    await event_once(env)
    assert s._last_quote_round_processed_id=='watchdog-1'
    before=s._process_quote_round_shadow.await_count
    s._process_quote_round_shadow.side_effect=None
    s._process_quote_round_shadow.return_value={'status':'completed'}
    await s._paper_intraday_auto_trade()
    assert s._process_quote_round_shadow.await_count==before+1
    assert s._last_quote_round_processed_id=='new-failed-round'
    await s._paper_intraday_auto_trade()
    assert s._process_quote_round_shadow.await_count==before+1


@pytest.mark.asyncio
async def test_partial_result_from_primary_or_connected_never_means_completed(env):
    s=env.scheduler
    s._run_paper_accounts_isolated.side_effect=None
    s._run_paper_accounts_isolated.return_value={'status':'failed','failed_accounts':['default']}
    await event_once(env)
    assert s._last_quote_round_processed_id is None
    assert s._quote_consumer_health['status']=='failed'
    s._process_quote_round_shadow.assert_awaited_once()


@pytest.mark.asyncio
async def test_connected_cancellation_is_not_wrapped_or_marked_complete(env,monkeypatch):
    s=env.scheduler
    monkeypatch.setattr(s,'_process_quote_round_shadow',module.DataScheduler._process_quote_round_shadow.__get__(s))
    monkeypatch.setattr(shadows,'scan_strategy_iteration_shadow',AsyncMock(return_value={}))
    monkeypatch.setattr(challengers,'run_strategy_iteration_challenger_accounts',AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):await s._paper_intraday_auto_trade()
    assert s._last_quote_round_processed_id is None
    assert not s._quote_dispatch_lock.locked()


@pytest.mark.parametrize("result", [None, {}, {"status": "evidence_only"}, {"status": "failed"}, False])
def test_missing_or_noncompletion_dispatch_receipt_is_not_success(result):
    with pytest.raises(RuntimeError):
        module.DataScheduler._require_paper_dispatch_success({"status": "completed"}, result)


@pytest.mark.asyncio
async def test_shadow_scan_failure_remains_visible_even_if_connected_accounts_succeed(env, monkeypatch):
    s = env.scheduler
    monkeypatch.setattr(s, "_process_quote_round_shadow", module.DataScheduler._process_quote_round_shadow.__get__(s))
    monkeypatch.setattr(shadows, "scan_strategy_iteration_shadow", AsyncMock(side_effect=RuntimeError("evidence commit failed")))
    execute = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(challengers, "run_strategy_iteration_challenger_accounts", execute)
    await event_once(env)
    assert execute.await_count == len(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE)
    assert {call.kwargs["account_name"] for call in execute.await_args_list} == set(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.values())
    assert s._quote_consumer_health["status"] == "failed"
    assert s._last_quote_round_processed_id is None
