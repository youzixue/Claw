"""Isolated positive-only batch idempotency parity and actual observation clocks."""
import json
import time as wall_time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import pytest_asyncio
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.db.session import Base
from app.models.paper import PaperAutoTradeLog, PaperTradeLog
from app.models.trading import TradeOrder
from app.paper import strategy_iteration_challenger as c
from app.paper import confirmation_evidence as evidence
from app.api.v1 import paper

DAY = date(2026, 9, 21)
AT = datetime(2026, 9, 21, 10)
ROUTE = "b_weak_open_second_board"

@pytest_asyncio.fixture
async def store(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path/"consumer.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, maker
    await engine.dispose()

def ev(key, route=ROUTE):
    return SimpleNamespace(event_key=key, route_id=route)

def log(key, **kw):
    return PaperAutoTradeLog(account_id=11, run_id=c._run_id(key), trade_date=DAY,
        action=kw.pop("action", "wait_buy"), decision=kw.pop("decision", "wait"),
        created_at=AT, **kw)

def order(key, **kw):
    return TradeOrder(order_id="order-"+key, broker=kw.pop("broker", "paper"),
        account_id=kw.pop("account_id", "challenger_b"), code="600001",
        side=kw.pop("side", "buy"), order_type="limit", price=10, quantity=100,
        status=kw.pop("status", "submitted"), signal_id=c._signal_token(key, ROUTE),
        trade_date=DAY, **kw)

@pytest.mark.asyncio
async def test_batch_exactly_matches_existing_per_event_truth_and_scoping(store):
    _, maker = store
    rows = [
        log("terminal", action="skip_terminal", decision="skipped"),
        log("same_wait", quote_round_id="round-new"),
        log("old_wait", quote_round_id="round-old"),
        log("legacy_wait", candidate_json=json.dumps({"decision_round_id":"round-new"})),
        log("bad_json", candidate_json="[not-json"),
        log("explicit_other", quote_round_id="round-old", candidate_json=json.dumps({"decision_round_id":"round-new"})),
        log("filled_log", executed_trade_id=4),
        log("dry", decision="dry_run"),
        order("order"), order("cancel", status="canceled"), order("risk", status="risk_blocked"),
        order("rejected", status="rejected"), order("broker", broker="other"),
        order("wrong_account", account_id="challenger_c"), order("sell", side="sell"),
        PaperTradeLog(account_id=11, code="600001", trade_type="buy", price=10,
                      amount=100, trade_time=AT, signal_id=c._signal_token("trade", ROUTE)),
    ]
    keys = ["terminal","same_wait","old_wait","legacy_wait","bad_json","explicit_other","filled_log",
            "dry","order","cancel","risk","rejected","broker","wrong_account","sell","trade","unseen"]
    async with maker() as db:
        db.add_all(rows)
        await db.commit()
        expected = set()
        for key in keys:
            if await c._already_processed(db, account_id=11, account_name="challenger_b",
                  run_id=c._run_id(key), signal_token=c._signal_token(key, ROUTE), quote_round_id="round-new"):
                expected.add(key)
        actual = await c._processed_event_keys(db, [ev(k) for k in keys],
                         {ROUTE:(11,"challenger_b")}, quote_round_id="round-new")
        assert actual == expected == {"terminal","same_wait","legacy_wait","filled_log","dry","order","cancel","risk","trade"}
        assert await c._processed_event_keys(db, [ev("terminal")], {ROUTE:(12,"challenger_b")}, quote_round_id="round-new") == set()

@pytest.mark.asyncio
async def test_batch_queries_are_bounded_and_no_negative_cache_is_reused(store):
    engine, maker = store
    keys = ["e"+str(i) for i in range(401)]
    count = []
    async with maker() as db:
        db.add_all([log(k, action="skip_terminal") for k in keys])
        await db.commit()
        def before(conn, cursor, stmt, params, context, many):
            if stmt.lstrip().upper().startswith("SELECT"):
                count.append(stmt)
        sql_event.listen(engine.sync_engine, "before_cursor_execute", before)
        try:
            start = wall_time.perf_counter()
            prior = set()
            for key in keys:
                if await c._already_processed(db, account_id=11, account_name="challenger_b",
                        run_id=c._run_id(key), signal_token=c._signal_token(key, ROUTE), quote_round_id="r"):
                    prior.add(key)
            scalar_seconds, scalar_queries = wall_time.perf_counter()-start, len(count)
            count.clear()
            start = wall_time.perf_counter()
            actual = await c._processed_event_keys(db, [ev(k) for k in keys],
                             {ROUTE:(11,"challenger_b")}, quote_round_id="r")
            batch_seconds = wall_time.perf_counter()-start
        finally:
            sql_event.remove(engine.sync_engine, "before_cursor_execute", before)
        assert actual == prior == set(keys)
        assert scalar_queries == 1203 and len(count) == 9
        print(json.dumps({"isolated_terminal_events":401, "scalar_queries":scalar_queries,
              "batch_queries":len(count), "scalar_seconds":scalar_seconds,
              "batch_seconds":batch_seconds, "parity":True, "live_intraday_benchmark":False}))
        db.add(order("new_after_snapshot"))
        await db.commit()
        # A signal absent in the positive snapshot still uses the original live check.
        assert await c._already_processed(db, account_id=11, account_name="challenger_b",
            run_id=c._run_id("new_after_snapshot"), signal_token=c._signal_token("new_after_snapshot", ROUTE), quote_round_id="r")

@pytest.mark.asyncio
async def test_empty_or_unmapped_events_do_not_read_business_tables():
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("unexpected query")))
    assert await c._processed_event_keys(db, [], {}, quote_round_id="r") == set()
    assert await c._processed_event_keys(db, [ev("x")], {}, quote_round_id="r") == set()

@pytest.mark.parametrize("field", ["confirmed_at", "event_created_at", "decision_at"])
def test_wall_clock_observation_does_not_manufacture_negative_latency(monkeypatch, field):
    monkeypatch.setattr(evidence, "_observation_now", lambda: AT)
    args = dict(decision_at=AT, confirmed_at=AT-timedelta(seconds=10), event_created_at=AT-timedelta(seconds=5))
    args[field] = AT+timedelta(seconds=1)
    observed = evidence.entry_consumer_timing(**args)
    assert observed["clock_status"] == "invalid"
    assert observed["confirmed_to_consumer_seconds"] is None
    assert observed["commit_known_at"] is None

def test_native_wall_time_is_not_injected_business_time(monkeypatch):
    monkeypatch.setattr(evidence, "_observation_now", lambda: AT+timedelta(seconds=30))
    result = evidence.entry_consumer_timing(decision_at=AT, confirmed_at=AT-timedelta(seconds=5),
                                            event_created_at=AT-timedelta(seconds=2))
    assert result["consumer_started_at"] == (AT+timedelta(seconds=30)).isoformat()
    assert result["confirmed_to_consumer_seconds"] == 35
    assert result["event_created_to_consumer_seconds"] == 32
    assert result["clock_status"] == "ok"
    assert result["commit_known_at"] is None
    assert result["basis"] == "per_candidate_entry_not_transaction_commit"
    # Primary routes lack an equivalent immutable shadow confirmed timestamp.
    primary = evidence.entry_consumer_timing(decision_at=AT)
    assert primary["confirmed_to_consumer_seconds"] is None

@pytest.mark.asyncio
async def test_log_timing_preserves_business_clock_and_does_not_mutate_candidate(store, monkeypatch):
    _, maker = store
    monkeypatch.setattr(evidence, "_observation_now", lambda: AT+timedelta(seconds=2))
    candidate = {"execution_timing": evidence.entry_consumer_timing(decision_at=AT)}
    frozen = json.loads(json.dumps(candidate))
    monkeypatch.setattr(evidence, "_observation_now", lambda: AT+timedelta(seconds=5))
    async with maker() as db:
        row = await paper._add_auto_log(db, run_id="clock", trade_date=DAY,
            trigger="test", source="test", code="600001", action="skip_buy", decision="skipped",
            reason="test", candidate=candidate, created_at=AT, strategy_version="test")
        payload = json.loads(row.candidate_json)
        assert row.created_at == AT
        assert payload["execution_timing"]["log_observed_at"] == (AT+timedelta(seconds=5)).isoformat()
        assert payload["execution_timing"]["consumer_to_log_seconds"] == 3
        assert payload["execution_timing"]["commit_known_at"] is None
        assert candidate == frozen
