"""Forward deferred responses are observations, not closed-cycle or replay proof."""
import asyncio
import builtins
import copy
import json
from datetime import timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, text

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperPosition, PaperShadowEvent
from app.paper import position_observation as m
from app.trading import service
from test_paper_deferred_exit_provenance import memory_session
from test_paper_position_observation import AT, context, wall_clock


def outcome(**kw):
    return {
        "event": "filled",
        "order": {"id": 7, "order_id": "order-1", "broker": "paper", "account_id": "default",
            "code": "000001", "side": "sell", "price": 10, "quantity": 100,
            "filled_quantity": 100, "status": "filled", "strategy_version": "frozen-v1",
            "decision_round_id": "decision", "last_fill_round_id": "qr-1",
            "decision_at": AT-timedelta(seconds=30), "trade_date": AT.date().isoformat()},
        "fills": [{"id": 11, "fill_id": "fill-1", "order_id": "order-1", "broker": "paper",
            "code": "000001", "side": "sell", "price": 10, "quantity": 100,
            "commission": 5.0, "tax": 0.5, "broker_trade_id": "88",
            "filled_at": AT-timedelta(seconds=1), "fill_round_id": "qr-1"}],
        "deferred": {"position_id": 2, "candidate": {"exit_trigger_reason": "original-stop"}},
        **kw,
    }


def capture(**kw):
    args = dict(account_id=4, account_name="default", trade_date=AT.date(),
        quote_context=context(), evaluated_at=AT, outcome=outcome())
    args.update(kw)
    return m.capture_deferred_execution_frame(**args)


def test_response_is_owned_and_never_claims_position_closure(wall_clock):
    source = outcome()
    frame = capture(outcome=source)
    source["order"]["quantity"] = 900
    source["fills"][0]["commission"] = 999
    source["deferred"]["candidate"]["exit_trigger_reason"] = "mutated"
    row = m._execution_event(frame, AT+timedelta(seconds=3))
    payload = json.loads(row["snapshot_json"])
    assert payload["order_response"]["quantity"] == 100
    assert payload["fill_responses"][0]["commission"] == 5
    assert payload["original_exit_trigger"] == "original-stop"
    assert payload["fill_responses"][0]["filled_at"] == (AT-timedelta(seconds=1)).isoformat()
    assert row["observed_at"] == AT and row["created_at"] == AT+timedelta(seconds=3)
    for key in ("position_state_before_execution", "position_state_after_execution",
                "position_fully_closed", "commit_known_at", "historical_fill_observed_at",
                "execution_permission"):
        assert payload[key] is None
    assert payload["replay_ready"] is False and payload["can_submit_orders"] is False
    assert row["price"] is None and row["assumed_fill_price"] is None
    assert row["event_type"] == "execution_frame" and row["status"] == "observed_unverified"


@pytest.mark.parametrize("kwargs", [
    {"account_id": True}, {"account_id": -1}, {"account_name": "another"},
    {"trade_date": (AT-timedelta(days=1)).date()},
    {"evaluated_at": AT+timedelta(seconds=1)}, {"evaluated_at": AT.replace(tzinfo=timezone.utc)},
    {"quote_context": {}}, {"quote_context": context(committed_at=AT+timedelta(seconds=1))},
    {"quote_context": context(round_id="")}, {"outcome": None},
])
def test_manual_historical_future_or_wrong_account_is_not_captured(wall_clock, kwargs):
    assert capture(**kwargs) is None


@pytest.mark.parametrize("key,value", [
    ("broker", "live"), ("side", "buy"), ("code", "bad"), ("code", "０００００１"),
    ("order_id", ""), ("order_id", None),
])
def test_non_paper_sell_identity_is_not_captured(wall_clock, key, value):
    source = outcome()
    source["order"][key] = value
    assert capture(outcome=source) is None


@pytest.mark.parametrize("fills", [None, {}, [None], [{}]*65])
def test_missing_or_oversized_fill_list_is_not_reported_as_zero(wall_clock, fills):
    with pytest.raises(ValueError, match="invalid_or_unbounded"):
        capture(outcome=outcome(fills=fills))


def test_empty_wait_and_legacy_missing_stay_distinct(wall_clock):
    payload = json.loads(capture(outcome=outcome(event="waiting", fills=[], deferred={})).response_json)
    assert payload["fill_responses"] == []
    assert payload["position_id_from_original_metadata"] is None
    assert payload["original_exit_trigger"] is None
    assert payload["original_exit_trigger_basis"] == "legacy_missing"
    assert payload["historical_before_first_observation"] == "unknown_not_backfilled"


def test_capture_clocks_not_revisions_but_changed_facts_are(wall_clock, monkeypatch):
    original = m._execution_event(capture(), AT)
    later = AT+timedelta(seconds=3)
    monkeypatch.setattr(m, "_now", lambda: later)
    assert m._execution_event(capture(), later)["event_key"] == original["event_key"]
    for field, value in (("event", "partial"), ("fills", [])):
        assert m._execution_event(capture(outcome=outcome(**{field: value})), later)["event_key"] != original["event_key"]
    assert m._execution_event(capture(account_id=5), later)["event_key"] != original["event_key"]
    with pytest.raises(ValueError, match="clock"):
        m._execution_event(capture(), AT)


@pytest.mark.asyncio
async def test_writer_caller_rollback_dedup_and_no_dirty_orm_flush(memory_session, wall_clock):
    db = memory_session
    await db.execute(text("SELECT 1"))  # No real SQLite write transaction yet.
    assert (await m.append_deferred_execution_frames(db, [capture()]))["status"] == "staged_not_committed"
    await db.rollback()
    assert list((await db.scalars(select(PaperShadowEvent))).all()) == []
    assert (await m.append_deferred_execution_frames(db, [capture(), capture()]))["attempted"] == 2
    await db.commit()
    assert len(list((await db.scalars(select(PaperShadowEvent))).all())) == 1
    invalid = PaperPosition(account_id=None, code="bad")
    db.add(invalid)
    flushed = []
    event.listen(db.sync_session, "before_flush", lambda *_: flushed.append(True))
    assert (await m.append_deferred_execution_frames(db, [capture()]))["status"] == "staged_not_committed"
    assert flushed == [] and invalid in db.new
    await db.rollback()


@pytest.mark.asyncio
async def test_failure_in_second_chunk_rolls_back_whole_audit_not_outer_work(memory_session, wall_clock):
    db = memory_session
    conn = await db.connection()
    await conn.execute(text("CREATE TEMP TABLE retained_marker(value INTEGER)"))
    await conn.execute(text("INSERT INTO retained_marker VALUES (7)"))
    await conn.execute(text("""CREATE TEMP TRIGGER reject_execution BEFORE INSERT ON paper_shadow_event
        WHEN NEW.code = '000002' BEGIN SELECT RAISE(ABORT, 'private audit detail'); END"""))
    frames = [capture(account_id=i+1) for i in range(32)]
    last = outcome()
    last["order"]["code"] = "000002"
    frames.append(capture(outcome=last))
    assert (await m.append_deferred_execution_frames(db, frames))["status"] == "unavailable"
    assert (await conn.execute(text("SELECT value FROM retained_marker"))).scalar_one() == 7
    assert (await conn.execute(text("SELECT count(*) FROM paper_shadow_event"))).scalar_one() == 0
    await db.commit()


@pytest.mark.asyncio
async def test_bounds_no_io_and_cancellation_propagates(memory_session, wall_clock, monkeypatch):
    db = memory_session
    connection = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(db, "connection", connection)
    assert (await m.append_deferred_execution_frames(db, []))["status"] == "not_observed"
    assert (await m.append_deferred_execution_frames(db, [capture()]*257))["status"] == "unavailable"
    connection.assert_not_called()
    with pytest.raises(asyncio.CancelledError):
        await m.append_deferred_execution_frames(db, [capture()])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "import", "capture", "append", "cancel", "business_log"])
async def test_optional_hook_isolation_and_original_failure_semantics(
    memory_session, wall_clock, monkeypatch, caplog, failure,
):
    db = memory_session
    account = PaperAccount(account_name="default", initial_capital=100000)
    db.add(account)
    await db.commit()
    source = outcome()
    before = copy.deepcopy(source)
    producer = AsyncMock(return_value=[source])
    stock = AsyncMock(return_value=("isolated", None))
    monkeypatch.setattr(service, "reconcile_paper_deferred_orders", producer)
    monkeypatch.setattr(paper, "_stock_info", stock)
    if failure == "import":
        original_import = builtins.__import__
        def fail_import(name, *a, **kw):
            if name == "app.paper.position_observation":
                raise ImportError("sensitive payload")
            return original_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", fail_import)
    elif failure == "capture":
        def fail_capture(**_kw):
            raise ValueError("sensitive payload")
        monkeypatch.setattr(m, "capture_deferred_execution_frame", fail_capture)
    elif failure in {"append", "cancel"}:
        async def fail_append(*_a):
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("sensitive payload")
        monkeypatch.setattr(m, "append_deferred_execution_frames", fail_append)
    elif failure == "business_log":
        monkeypatch.setattr(paper, "_add_auto_log", AsyncMock(side_effect=RuntimeError("business failure")))
    token = paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        call = paper._reconcile_deferred_order_logs(
            db, account=account, run_id="fixture", trade_date=AT.date(), trigger="fixture", observed_at=AT,
        )
        if failure in {"cancel", "business_log"}:
            with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
                await call
            await db.rollback()
        else:
            logs = await call
            await db.commit()
            assert len(logs) == 1 and logs[0].decision == "executed" and logs[0].amount == 100
            assert logs[0].strategy_version == "frozen-v1"
            assert json.loads(logs[0].candidate_json)["exit_trigger_reason"] == "original-stop"
        assert source == before
        assert producer.await_count == stock.await_count == 1
        events = list((await db.scalars(select(PaperShadowEvent))).all())
        assert len(events) == int(failure == "none")
        assert "sensitive payload" not in caplog.text
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_batch_overflow_never_truncates_or_changes_original_processing(memory_session, wall_clock, monkeypatch):
    db = memory_session
    account = PaperAccount(account_name="default", initial_capital=100000)
    db.add(account)
    await db.commit()
    producer = AsyncMock(return_value=[outcome() for _ in range(257)])
    capture_spy = AsyncMock(side_effect=AssertionError("must not capture oversized batch"))
    logger = AsyncMock(return_value="ordinary-log")
    monkeypatch.setattr(service, "reconcile_paper_deferred_orders", producer)
    monkeypatch.setattr(m, "capture_deferred_execution_frame", capture_spy)
    monkeypatch.setattr(paper, "_stock_info", AsyncMock(return_value=("isolated", None)))
    monkeypatch.setattr(paper, "_add_auto_log", logger)
    token = paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        logs = await paper._reconcile_deferred_order_logs(
            db, account=account, run_id="fixture", trade_date=AT.date(), trigger="fixture", observed_at=AT)
        assert len(logs) == logger.await_count == 257
        capture_spy.assert_not_called()
        assert list((await db.scalars(select(PaperShadowEvent))).all()) == []
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
