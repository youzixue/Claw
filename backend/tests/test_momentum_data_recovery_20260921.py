"""E1/E2 pure state-machine regressions; no market services or runtime database."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import json

import pytest

from app.paper.momentum_retest_shadow import MomentumRetestShadowEngine
from test_momentum_retest_shadow import _policy, _quote

START = datetime(2026, 9, 21, 10)
CODE = "600001"


def send(engine, sec, base_price=10.4, **overrides):
    at = START + timedelta(seconds=sec)
    quote = _quote(base_price, (base_price / 10 - 1) * 100, 30_000_000 + sec * 100_000)
    quote.update(source_quote_at=at, quote_round_id=f"isolated-{sec}")
    quote.update(overrides)
    engine.observe_batch([quote], at, {CODE})
    return engine.states.get(CODE)


def candidate():
    engine = MomentumRetestShadowEngine(_policy())
    send(engine, 0, 10.2, avg_price=10.1)
    send(engine, 10)
    assert engine.states[CODE].stage == "candidate"
    return engine


@pytest.mark.parametrize("value", [None, float("nan"), "bad", 0])
def test_change_is_recomputed_from_prices_not_missing_or_inconsistent_percentage(value):
    engine = candidate()
    state = send(engine, 20, change_pct=value)
    assert state.stage == "candidate"
    assert state.last_price == 10.4


@pytest.mark.parametrize("field", ["price", "prev_close", "amount", "avg_price", "ask1_price", "ask1_volume", "limit_up"])
@pytest.mark.parametrize("value", [None, float("nan"), "bad"])
def test_unknown_path_frame_blocks_without_fake_terminal_or_path_evidence(field, value):
    engine = candidate()
    state = engine.states[CODE]
    before = (state.last_at, state.observation_count)
    send(engine, 20, **{field: value})
    assert state.stage == "coverage_blocked"
    assert (state.last_at, state.observation_count) == before
    assert state.quote_history == []
    assert not any(e["event_type"] == "invalidated" for e in engine.pending_events())
    if field == "price":
        blocked_event = next(e for e in engine.pending_events() if e["event_type"] == "coverage_blocked")
        assert blocked_event["price"] is None
    # Recovery at strong prices must never continue the pre-missing candidate.
    send(engine, 30)
    assert state.stage == "coverage_blocked"
    send(engine, 40, 10.2, avg_price=10.1)
    assert state.stage == "armed"
    assert len(state.quote_history) == 1
    assert state.candidate_at is None
    assert state.peak_price == 0


def test_true_zero_offer_and_true_limit_up_remain_terminal():
    for overrides in ({"ask1_volume": 0}, {"ask1_price": 0}, {"price": 11.0, "limit_up": 11.0}):
        engine = candidate()
        send(engine, 20, **overrides)
        state = engine.states[CODE]
        assert state.stage == "invalidated"
        before = (state.last_at, state.observation_count, list(state.quote_history))
        send(engine, 30, 10.2, avg_price=10.1)
        assert state.stage == "invalidated"
        assert (state.last_at, state.observation_count, state.quote_history) == before


def blocked():
    engine = candidate()
    send(engine, 110)
    assert engine.states[CODE].stage == "coverage_blocked"
    return engine


def test_blocked_continuous_strong_frames_can_later_rearm_without_counting_path():
    engine = blocked()
    state = engine.states[CODE]
    n = state.observation_count
    for sec in range(120, 241, 10):
        send(engine, sec)
        assert state.stage == "coverage_blocked"
        assert state.observation_count == n
    send(engine, 250, 10.2, avg_price=10.1)
    assert state.stage == "armed"
    assert len(state.quote_history) == 1


@pytest.mark.parametrize("source_sec", [110, 100])
def test_duplicate_backwards_source_does_not_refresh_blocked_continuity(source_sec):
    engine = blocked()
    for received in (150, 180, 200):
        send(engine, received, source_quote_at=START + timedelta(seconds=source_sec))
    send(engine, 210, 10.2, avg_price=10.1)
    assert engine.states[CODE].stage == "coverage_blocked"
    send(engine, 220, 10.2, avg_price=10.1)
    assert engine.states[CODE].stage == "armed"


@pytest.mark.parametrize("bad_source", [None, "bad", START + timedelta(days=1), START + timedelta(seconds=21)])
def test_explicit_invalid_source_clock_blocks_without_fallback(bad_source):
    engine = candidate()
    state = engine.states[CODE]
    previous = state.last_at
    send(engine, 20, source_quote_at=bad_source)
    assert state.stage == "coverage_blocked"
    assert state.last_at == previous
    assert not state.quote_history
    # First trustworthy frame re-establishes clock only, not rearm evidence.
    send(engine, 30, 10.2, avg_price=10.1)
    assert state.stage == "coverage_blocked"
    send(engine, 40, 10.2, avg_price=10.1)
    assert state.stage == "armed"


def test_invalid_path_frame_cannot_be_replaced_by_older_source():
    engine = candidate()
    send(engine, 30, ask1_price=None)
    send(engine, 40, 10.2, avg_price=10.1, source_quote_at=START + timedelta(seconds=20))
    assert engine.states[CODE].stage == "coverage_blocked"
    send(engine, 50, 10.2, avg_price=10.1)
    assert engine.states[CODE].stage == "coverage_blocked"
    send(engine, 60, 10.2, avg_price=10.1)
    assert engine.states[CODE].stage == "armed"


def test_recovery_snapshot_restores_ordering_but_not_missing_path():
    engine = candidate()
    send(engine, 20, amount=None)
    rows = [SimpleNamespace(trade_date=e["trade_date"], event_type=e["event_type"],
                            snapshot_json=e["snapshot_json"]) for e in engine.pending_events()]
    restored = MomentumRetestShadowEngine(_policy())
    restored.restore(rows, consumer_watermark=START + timedelta(seconds=100))
    send(restored, 110, 10.2, avg_price=10.1)
    assert restored.states[CODE].stage == "coverage_blocked"
    send(restored, 120, 10.2, avg_price=10.1)
    assert restored.states[CODE].stage == "armed"


@pytest.mark.parametrize("overrides", [
    {"amount": None, "ask1_volume": 0},
    {"avg_price": None, "price": 11.0, "limit_up": 11.0},
])
def test_known_unfillable_evidence_wins_over_unrelated_unknown_fields(overrides):
    engine = candidate()
    send(engine, 20, **overrides)
    assert engine.states[CODE].stage == "invalidated"


@pytest.mark.parametrize("terminal", ["confirmed", "invalidated", "expired"])
def test_bad_source_or_missing_fields_never_modify_terminal(terminal):
    engine = candidate()
    state = engine.states[CODE]
    state.stage = terminal
    before = state.snapshot()
    send(engine, 20, source_quote_at=None)
    send(engine, 30, amount=None)
    send(engine, 40, 10.2, avg_price=10.1)
    assert state.snapshot() == before


def test_missing_frame_cannot_refresh_clock_even_after_several_rounds():
    engine = candidate()
    for sec in range(20, 241, 10):
        send(engine, sec, ask1_price=None)
    send(engine, 250, 10.2, avg_price=10.1)
    assert engine.states[CODE].stage == "coverage_blocked"
    send(engine, 260, 10.2, avg_price=10.1)
    assert engine.states[CODE].stage == "armed"


def test_unknown_source_blocks_replaying_pre_gap_source_frames():
    engine = candidate()
    send(engine, 20, source_quote_at=None)
    for received, source in ((30, 15), (40, 16)):
        send(engine, received, 10.2, avg_price=10.1,
             source_quote_at=START + timedelta(seconds=source))
    assert engine.states[CODE].stage == "coverage_blocked"
    assert engine.states[CODE].last_valid_frame_at is None


def test_recovery_requires_and_can_complete_a_wholly_new_path():
    engine = candidate()
    send(engine, 20, amount=None)
    send(engine, 30)
    send(engine, 40, 10.2, avg_price=10.1)
    assert engine.states[CODE].stage == "armed"
    for sec, price in ((50, 10.4), (80, 10.5), (110, 10.42), (140, 10.41), (170, 10.46)):
        send(engine, sec, price)
    assert engine.states[CODE].stage == "confirmed"
    confirmed = next(e for e in engine.pending_events() if e["event_type"] == "confirmed")
    payload = json.loads(confirmed["snapshot_json"])
    assert payload["state"]["candidate_at"] == (START + timedelta(seconds=50)).isoformat()
    assert all(datetime.fromisoformat(row[0]) >= START + timedelta(seconds=40)
               for row in payload["state"]["quote_history"])


@pytest.mark.parametrize("stage", ["unseen", "armed", "candidate", "pullback"])
@pytest.mark.parametrize("field", ["amount", "ask1_price", "ask1_volume", "avg_price", "limit_up"])
def test_required_path_fields_are_not_evidence_at_any_nonterminal_stage(stage, field):
    engine = candidate()
    state = engine.states[CODE]
    state.stage = stage
    before = state.observation_count
    send(engine, 20, **{field: None})
    assert state.stage == "coverage_blocked"
    assert state.observation_count == before
    assert state.quote_history == []
    assert state.last_valid_frame_at is None
    send(engine, 30, 10.2, avg_price=10.1)
    assert state.stage == "coverage_blocked"
    send(engine, 40, 10.2, avg_price=10.1)
    assert state.stage == "armed"
    assert len(state.quote_history) == 1


def test_zero_cumulative_amount_is_known_but_not_candidate_liquidity():
    engine = MomentumRetestShadowEngine(_policy())
    send(engine, 0, 10.2, avg_price=10.1, amount=0)
    assert engine.states[CODE].stage == "armed"
    send(engine, 10, amount=0)
    assert engine.states[CODE].stage == "armed"
    assert engine.states[CODE].quote_history[-1][2] == 0
    assert not any(e["event_type"] == "coverage_blocked" for e in engine.pending_events())
    assert any(e["event_type"] == "screened" for e in engine.pending_events())


def test_zero_liquidity_values_are_not_missing():
    engine = candidate()
    state = send(engine, 20, withdrawal_ratio=0, orderbook_imbalance=0)
    assert state.stage == "candidate"
