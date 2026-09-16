"""Synthetic forward sell-submission evidence; no real orders or historical repair."""
import asyncio
import copy
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperPosition, PaperShadowEvent, PaperTradeLog
from app.paper import position_observation as m
from test_deferred_execution_observation import outcome
from test_paper_position_observation import AT, context, position, prepare_scan, wall_clock
from test_paper_api import paper_client


def capture(**kwargs):
    args = dict(account_id=4, account_name="default", trade_date=AT.date(),
        quote_context=context(), evaluated_at=AT, outcome=outcome(),
        origin="order_submission", position=position(buy_amount=0, is_closed=True),
        trigger_reason="original-current-trigger")
    args.update(kwargs)
    return m.capture_sell_execution_frame(**args)


def test_response_and_position_are_owned_at_actual_receipt_not_round_cutoff(wall_clock, monkeypatch):
    source = outcome()
    pos = position(buy_amount=0, is_closed=True)
    later = AT + timedelta(seconds=4)
    monkeypatch.setattr(m, "_now", lambda: later)
    frame = capture(outcome=source, position=pos)
    source["fills"][0]["quantity"] = 900
    pos.buy_amount = 999
    payload = json.loads(frame.response_json)
    assert frame.captured_at == later
    assert payload["response_observed_at"] == later.isoformat()
    assert payload["position_state_after_response_observed_at"] == later.isoformat()
    assert payload["strategy_evaluated_at"] == AT.isoformat()
    assert payload["fill_responses"][0]["filled_at"] == (AT-timedelta(seconds=1)).isoformat()
    assert payload["fill_responses"][0]["quantity"] == 100
    assert payload["position_state_after_execution"]["buy_amount"] == 0
    assert payload["position_state_after_execution"]["is_closed"] is True
    assert payload["position_id_from_original_metadata"] is None
    assert payload["submission_position_id"] == 2
    assert payload["original_exit_trigger"] == "original-current-trigger"
    assert payload["original_exit_trigger_basis"] == "current_position_scan"
    assert payload["commit_known_at"] is None and payload["position_fully_closed"] is None
    assert payload["historical_fill_observed_at"] is None and payload["execution_permission"] is None
    assert payload["replay_ready"] is False and payload["can_submit_orders"] is False


@pytest.mark.parametrize("kwargs", [
    {"origin":"buy"}, {"position":None}, {"position":position(id=True)},
    {"position":position(account_id=5)}, {"position":position(account_id=True)},
    {"position":position(code="000002")}, {"account_name":"other"},
    {"trade_date":(AT-timedelta(days=1)).date()}, {"evaluated_at":AT+timedelta(seconds=1)},
    {"quote_context":context(committed_at=AT+timedelta(seconds=1))},
])
def test_unmatched_or_historical_submission_never_fabricates_a_position(wall_clock, kwargs):
    assert capture(**kwargs) is None


@pytest.mark.parametrize("fills", [None, {}, [None], [{}]*65])
def test_missing_fill_list_not_equivalent_to_pending_zero(wall_clock, fills):
    with pytest.raises(ValueError, match="invalid_or_unbounded"):
        capture(outcome=outcome(fills=fills))


def test_same_response_dedups_but_changed_position_and_origin_do_not(wall_clock, monkeypatch):
    first = m._execution_event(capture(), AT)
    later = AT+timedelta(seconds=5)
    monkeypatch.setattr(m, "_now", lambda: later)
    assert m._execution_event(capture(), later)["event_key"] == first["event_key"]
    assert m._execution_event(capture(position=position()), later)["event_key"] != first["event_key"]
    deferred = m.capture_deferred_execution_frame(account_id=4, account_name="default",
        trade_date=AT.date(), quote_context=context(), evaluated_at=AT, outcome=outcome())
    assert m._execution_event(deferred, later)["event_key"] != first["event_key"]
    assert "submission_origin" not in json.loads(deferred.response_json)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,filled,remaining", [
    ("submitted",0,100), ("rejected",0,100), ("partial",50,50), ("filled",100,0),
])
async def test_real_sell_pass_captures_reported_outcome_without_extra_execution(
    paper_client, wall_clock, monkeypatch, status, filled, remaining,
):
    _, maker = paper_client
    account_id, available = await prepare_scan(maker, monkeypatch, "fill")
    monkeypatch.setattr(paper, "_conservative_execution_price", lambda *_:10.1)
    monkeypatch.setattr(paper, "_today_sell_stats", AsyncMock(return_value={"amount":0}))
    monkeypatch.setattr(paper, "_has_active_paper_order", AsyncMock(return_value=False))
    monkeypatch.setattr(paper, "_auto_sell_amount", lambda *_a,**_k:100)
    source = outcome()
    source.pop("deferred")
    source.pop("event")
    source["order"].update(status=status, filled_quantity=filled)
    source["fills"] = [{**source["fills"][0], "quantity":filled}] if filled else []
    frozen = copy.deepcopy(source)
    commands = []
    async def submit(db, command):
        commands.append(command)
        pos = await db.scalar(select(PaperPosition).where(PaperPosition.account_id==account_id))
        pos.buy_amount=remaining
        pos.is_closed=remaining==0
        # Actual returned response comes later than the original QuoteRound clock.
        monkeypatch.setattr(m, "_now", lambda: AT+timedelta(seconds=4))
        return source
    monkeypatch.setattr("app.trading.service.submit_order", submit)
    token = paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        async with maker() as db:
            account = await db.get(PaperAccount, account_id)
            logs = await paper._run_auto_sells(db, account=account, run_id="submission-observation",
                trade_date=AT.date(), trigger="fixture", execute=True, quote_now=AT, log_holds=False)
            await db.commit()
            rows = (await db.scalars(select(PaperShadowEvent).where(
                PaperShadowEvent.route_id==m.EXECUTION_ROUTE_ID))).all()
            assert len(rows)==1
            payload = json.loads(rows[0].snapshot_json)
            assert payload["submission_origin"]=="order_submission"
            assert payload["order_response"]["status"]==status
            assert payload["fill_responses"]==json.loads(m._encode([
                {key:m._leaf(fill.get(key)) for key in m.FILL_RESPONSE_FIELDS} for fill in frozen["fills"]]))
            assert payload["position_state_after_execution"]["buy_amount"]==remaining
            assert payload["position_fully_closed"] is None
            assert payload["response_observed_at"]==(AT+timedelta(seconds=4)).isoformat()
            assert available.await_count==1 and len(commands)==1
            assert commands[0].quantity==100 and commands[0].strategy_version=="old-v1"
            assert len(logs)==1 and logs[0].decision==("executed" if status=="filled"
                else "wait" if status in {"submitted","partial"} else "blocked")
            assert source==frozen
            assert rows[0].event_type=="execution_frame" and rows[0].assumed_fill_price is None
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capture", "append", "cancel"])
async def test_submission_observer_failure_cannot_turn_sell_into_failure(
    paper_client, wall_clock, monkeypatch, failure,
):
    _, maker = paper_client
    account_id, available = await prepare_scan(maker, monkeypatch, "fill")
    monkeypatch.setattr(paper, "_conservative_execution_price", lambda *_:10.1)
    monkeypatch.setattr(paper, "_today_sell_stats", AsyncMock(return_value={"amount":0}))
    monkeypatch.setattr(paper, "_has_active_paper_order", AsyncMock(return_value=False))
    monkeypatch.setattr(paper, "_auto_sell_amount", lambda *_a,**_k:100)
    producer = AsyncMock(return_value=outcome())
    monkeypatch.setattr("app.trading.service.submit_order", producer)
    if failure=="capture":
        def fail(**_kw): raise ValueError("private")
        monkeypatch.setattr(m, "capture_sell_execution_frame", fail)
    else:
        async def fail(*_a):
            if failure=="cancel": raise asyncio.CancelledError()
            raise ValueError("private")
        monkeypatch.setattr(m, "append_deferred_execution_frames", fail)
    token = paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        async with maker() as db:
            account = await db.get(PaperAccount, account_id)
            async def run():
                return await paper._run_auto_sells(db, account=account, run_id="failure",
                    trade_date=AT.date(), trigger="fixture", execute=True, quote_now=AT, log_holds=False)
            if failure=="cancel":
                with pytest.raises(asyncio.CancelledError): await run()
            else:
                logs = await run()
                assert len(logs)==1 and logs[0].decision=="executed"
            assert producer.await_count==1 and available.await_count==1
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_submission_writer_remains_caller_transaction_not_trade_writer(paper_client, wall_clock):
    _, maker = paper_client
    async with maker() as db:
        await db.execute(text("SELECT 1"))
        assert (await m.append_deferred_execution_frames(db, [capture()]))["status"]=="staged_not_committed"
        assert len((await db.scalars(select(PaperShadowEvent))).all())==1
        assert (await db.scalars(select(PaperTradeLog))).all()==[]
        await db.rollback()
    async with maker() as db:
        assert (await db.scalars(select(PaperShadowEvent))).all()==[]
