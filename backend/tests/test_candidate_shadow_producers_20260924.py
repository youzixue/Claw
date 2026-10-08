"""Producer-only regression: temporary SQLite, no runtime DB/API/network."""
import copy
import sys
import socket
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

import app.paper.momentum_retest_shadow as momentum
import app.paper.strategy_iteration_shadow as shadow
from app.config.settings import settings
from app.models.paper import PaperShadowEvent, PaperTradeLog
from test_momentum_retest_shadow import _policy, _quote, _run_confirmed_path
from test_strategy_iteration_shadow import (
    shadow_env, _seed_first_board_denominator, _seed_structures,
    _first_board_quote, _freeze_shadow_today, _quote as generic_quote,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("producer tests must not access the network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def install_sink(monkeypatch, mode, packets):
    def capture(packet):
        if mode == "throw":
            raise RuntimeError("research sink failed")
        if mode == "on":
            packets.append(copy.deepcopy(packet))
    monkeypatch.setitem(sys.modules, "app.paper.candidate_shadow",
                        SimpleNamespace(capture_frame=capture))


def normalized(events):
    return [{k: v for k, v in event.items() if k != "created_at"} for event in events]


def test_a2_events_and_state_identical_for_off_on_and_failing_hook(monkeypatch):
    results, states = [], []
    for mode in ("off", "on", "throw"):
        packets = []
        install_sink(monkeypatch, mode, packets)
        engine = _run_confirmed_path()
        results.append(normalized(engine.pending_events()))
        states.append(engine._states["600001"].snapshot())
        if mode == "on":
            gates = [p for p in packets if p["stage"] == "static_gate"]
            assert gates and gates[-1]["original_gate"] is True
            assert any(p["stage"] == "confirmed" and p["original_confirmed"] is True for p in packets)
            assert all(p["account_id"] is None and p["account_name"] == "challenger_a" for p in packets)
    assert results[0] == results[1] == results[2]
    assert states[0] == states[1] == states[2]


def test_a2_early_watch_has_real_predicate_clock_and_no_candidate(monkeypatch):
    packets = []
    install_sink(monkeypatch, "on", packets)
    engine = momentum.MomentumRetestShadowEngine(_policy())
    at = datetime(2026, 9, 24, 9, 31)
    quote = _quote(10.4, 4, 25_000_000)
    quote.update(source_quote_at=at, received_at=at + timedelta(seconds=1),
                 updated_at=at + timedelta(seconds=2), quote_round_id="qr-early")
    before = copy.deepcopy(quote)
    engine.observe_batch([quote], at + timedelta(seconds=2), {"600001"})
    observation = next(p for p in packets if p["stage"] == "observation")
    assert observation["original_candidate"] is False
    assert observation["original_confirmed"] is False
    assert observation["original_gate"] is None
    assert observation["observed_at"] != at
    assert observation["producer_reported_at"] == at + timedelta(seconds=2)
    assert observation["quote"]["source_quote_at"] == at
    assert quote == before
    assert not any(e["event_type"] == "confirmed" for e in engine.pending_events())


@pytest.mark.parametrize("source", [None, "invalid", datetime(2026, 9, 25, 9, 31)])
def test_a2_invalid_source_is_unknown_not_formal_confirmation(monkeypatch, source):
    packets = []
    install_sink(monkeypatch, "on", packets)
    engine = momentum.MomentumRetestShadowEngine(_policy())
    quote = _quote(10.4, 4, 25_000_000)
    quote["source_quote_at"] = source
    engine.observe_batch([quote], datetime(2026, 9, 24, 9, 31), {"600001"})
    packet = packets[-1]
    assert packet["stage"] == "scan_complete"
    assert packet["gate_inputs"]["invalid_source_clock_count"] == 1
    assert packet["gate_inputs"]["noncandidate_aggregated_count"] == 1
    assert packet["original_gate"] is packet["original_confirmed"] is None
    assert packet["identities"] == {}
    assert engine.pending_events() == []


def test_a2_empty_and_outside_universe_are_distinct(monkeypatch):
    packets = []
    install_sink(monkeypatch, "on", packets)
    engine = momentum.MomentumRetestShadowEngine(_policy())
    at = datetime(2026, 9, 24, 9, 31)
    engine.observe_batch([], at, set())
    assert packets[-1]["gate_inputs"]["allowed_quote_count"] == 0
    assert packets[-1]["stage"] == "scan_complete"
    engine.observe_batch([_quote(10.4, 4, 25_000_000)], at, set())
    assert packets[-1]["gate_inputs"]["outside_allowed_count"] == 1
    assert packets[-1]["gate_inputs"]["not_full_universe_tick_capture"] is True


@pytest.mark.asyncio
async def test_disabled_producers_do_not_touch_db(monkeypatch):
    packets = []
    install_sink(monkeypatch, "on", packets)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "PAPER_MOMENTUM_RETEST_SHADOW_ENABLED", False)
    at = datetime(2026, 9, 24, 10)
    assert (await shadow.scan_strategy_iteration_shadow(None, [], at))["events"] == 0
    assert (await momentum.scan_momentum_retest_shadow(None, [], at))["events"] == 0
    assert {p["route"] for p in packets} == {"A2", "B2", "C2", "D2", "F2", "C3"}
    assert all(p["stage"] == "not_scanned" and p["original_confirmed"] is None for p in packets)


@pytest.mark.asyncio
async def test_generic_frames_failures_and_resets_preserve_events(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", False)
    _freeze_shadow_today(monkeypatch, date(2026, 9, 1))
    async with shadow_env() as db:
        await _seed_structures(db)
        outputs = []
        for mode in ("off", "on", "throw"):
            await db.execute(delete(PaperShadowEvent))
            await db.commit()
            packets = []
            install_sink(monkeypatch, mode, packets)
            for seconds in (0, 30):
                at = datetime(2026, 9, 1, 9, 35) + timedelta(seconds=seconds)
                quotes = [generic_quote(f"60000{i}") for i in range(1, 5)]
                for q in quotes:
                    q.update(source_quote_at=at, received_at=at, updated_at=at,
                             quote_round_id=f"qr-{seconds}")
                # A legal zero offer must remain zero and fail the original gate.
                quotes[0]["ask1_volume"] = 0
                await shadow.scan_strategy_iteration_shadow(db, quotes, at)
            rows = (await db.scalars(select(PaperShadowEvent).order_by(PaperShadowEvent.event_key))).all()
            outputs.append([(r.event_key, r.snapshot_json, r.status) for r in rows])
            if mode == "on":
                b = [p for p in packets if p["route"] == "B2" and p["stage"] == "static_gate"]
                assert len(b) == 2 and all(p["original_gate"] is False for p in b)
                assert all(p["quote"]["ask1_volume"] == 0 for p in b)
                assert [p["producer_reported_at"] for p in b] == [
                    datetime(2026, 9, 1, 9, 35), datetime(2026, 9, 1, 9, 35, 30)]
                assert all(p["observed_at"] != p["producer_reported_at"] for p in b)
                assert any(p["route"] == "B2" and p["stage"] == "reset" for p in packets)
                assert any(p["route"] == "F2" and p["identities"].get("broken_board_identity") is True
                           for p in packets)
                assert all(p["original_confirmed"] is not True for p in b)
                d = [p for p in packets if p["route"] == "D2" and p["stage"] == "source_contract"]
                assert d and d[0]["source_contract"]["verified_early"] is True
                assert d[0]["source_contract"]["two_distinct_verified_middle"] is True
                assert d[0]["source_contract"]["verified_final"] is True
                assert d[0]["source_contract"]["evidence_at"] <= d[0]["source_contract"]["observed_at"]
        assert outputs[0] == outputs[1] == outputs[2]
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_gap, bulk", [(False, False), (True, False), (False, True)])
async def test_cf_missing_structure_unknown_is_bounded_and_nonintrusive(
        shadow_env, monkeypatch, missing_gap, bulk):
    from app.models.stock import LimitUpPool
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", False)
    _freeze_shadow_today(monkeypatch, date(2026, 9, 1))
    async with shadow_env() as db:
        await _seed_structures(db)
        original_scalars = db.scalars

        async def source_fixture(statement, *args, **kwargs):
            result = await original_scalars(statement, *args, **kwargs)
            descriptions = getattr(statement, "column_descriptions", [])
            if not descriptions or descriptions[0].get("entity") is not LimitUpPool:
                return result
            rows = result.all()
            if missing_gap or bulk:
                replacements = []
                for row in rows:
                    if row.code not in ("600002", "600003"):
                        replacements.append(row)
                        continue
                    for index in range(40 if bulk else 1):
                        replacements.append(SimpleNamespace(
                            code=f"{row.code}-{index}" if bulk else row.code,
                            trade_date=date(2026, 8, 1) if missing_gap else row.trade_date,
                            consecutive_days=row.consecutive_days, name=row.name,
                            limit_up_time=row.limit_up_time))
                rows = replacements
            return SimpleNamespace(all=lambda: rows)

        monkeypatch.setattr(db, "scalars", source_fixture)
        outputs = []
        for mode in ("off", "on", "throw"):
            await db.execute(delete(PaperShadowEvent))
            await db.commit()
            packets = []
            install_sink(monkeypatch, mode, packets)
            quotes = [generic_quote("600001"), generic_quote("600004")]
            if missing_gap:
                quotes += [generic_quote("600002"), generic_quote("600003")]
            await shadow.scan_strategy_iteration_shadow(
                db, quotes, datetime(2026, 9, 1, 9, 35))
            rows = (await original_scalars(
                select(PaperShadowEvent).order_by(PaperShadowEvent.event_key))).all()
            outputs.append([(r.event_key, r.snapshot_json, r.status) for r in rows])
            if mode == "on":
                for route in ("C2", "F2"):
                    receipt = next(p for p in packets
                        if p["route"] == route and p["stage"] == "scan_complete")
                    counts = receipt["gate_inputs"]
                    assert counts["unknown_structure_count"] == (40 if bulk else 1)
                    assert counts["missing_quote_count"] == (0 if missing_gap else 40 if bulk else 1)
                    assert counts["missing_gap_count"] == int(missing_gap)
                    assert counts["unknown_detail_count"] == (16 if bulk else 1)
                    assert counts["unknown_aggregated_count"] == (24 if bulk else 0)
                    unknown = [p for p in packets
                               if p["route"] == route and p["stage"] == "unknown"]
                    assert len(unknown) == counts["unknown_detail_count"]
                    assert all(p["original_candidate"] is False
                               and p["original_gate"] is None
                               and p["original_confirmed"] is None
                               and p["identities"] == {} for p in unknown)
        assert outputs[0] == outputs[1] == outputs[2]
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 0


@pytest.mark.asyncio
async def test_c3_every_member_frame_and_confirmation_preserve_events(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE", "2026-09-04")
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
        outputs = []
        for mode in ("off", "on", "throw"):
            await db.execute(delete(PaperShadowEvent))
            await db.commit()
            packets = []
            install_sink(monkeypatch, mode, packets)
            for seconds in (0, 30, 60):
                at = datetime(2026, 9, 4, 10) + timedelta(seconds=seconds)
                quotes = [_first_board_quote("600201", confirms=True),
                          _first_board_quote("600202", confirms=False)]
                for q in quotes:
                    q.update(source_quote_at=at, received_at=at, updated_at=at,
                             quote_round_id=f"c3-{seconds}")
                await shadow.scan_strategy_iteration_shadow(db, quotes, at)
            rows = (await db.scalars(select(PaperShadowEvent).order_by(PaperShadowEvent.event_key))).all()
            outputs.append([(r.event_key, r.snapshot_json, r.status) for r in rows])
            if mode == "on":
                frames = [p for p in packets if p["route"] == "C3" and p["stage"] == "static_gate"]
                assert len(frames) == 6
                assert {p["original_gate"] for p in frames} == {True, False}
                assert all(p["account_id"] is None and p["account_name"] is None for p in frames)
                assert all(p["sector_relative_strength_pct"] is not None for p in frames)
                assert any(p["route"] == "C3" and p["stage"] == "confirmed"
                           and p["original_confirmed"] is True for p in packets)
        assert outputs[0] == outputs[1] == outputs[2]
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 0


def test_generic_metrics_are_original_values_not_another_predicate(monkeypatch):
    evidence = {}
    quote = generic_quote("600001")
    original = copy.deepcopy(quote)
    result = shadow._reclaim_confirmed(
        quote, min_change_pct=0, max_change_pct=4, market_change_pct=0,
        evidence=evidence)
    assert result == shadow._reclaim_confirmed(
        quote, min_change_pct=0, max_change_pct=4, market_change_pct=0)
    assert evidence["relative_strength_pct"] == pytest.approx(0.5)
    assert quote == original


def test_a2_5000_outside_pattern_quotes_are_aggregated_without_changing_events(monkeypatch):
    at = datetime(2026, 9, 24, 9, 31)
    quotes = [_quote(10.2, 2, 25_000_000, code=str(600000 + i), avg_price=10.1)
              for i in range(5000)]
    allowed = {q["code"] for q in quotes}
    outputs = []
    momentum._research_policy_snapshot.cache_clear()
    for mode in ("off", "on"):
        packets = []
        install_sink(monkeypatch, mode, packets)
        engine = momentum.MomentumRetestShadowEngine(_policy())
        for seconds in (0, 30):
            engine.observe_batch(quotes, at + timedelta(seconds=seconds), allowed)
        outputs.append(normalized(engine.pending_events()))
        if mode == "on":
            assert len(packets) == 2  # one full coverage receipt per scan, no 10k frames
            assert all(p["stage"] == "scan_complete" for p in packets)
            for p in packets:
                assert p["gate_inputs"]["visited_quote_count"] == 5000
                assert p["gate_inputs"]["allowed_quote_count"] == 5000
                assert p["gate_inputs"]["noncandidate_aggregated_count"] == 5000
                assert p["gate_inputs"]["detailed_observation_count"] == 0
    assert outputs[0] == outputs[1]
    assert momentum._research_policy_snapshot.cache_info().misses == 1


def test_a2_existing_candidate_unknown_and_early_watch_are_not_thinned(monkeypatch):
    packets = []
    install_sink(monkeypatch, "on", packets)
    engine = momentum.MomentumRetestShadowEngine(_policy())
    at = datetime(2026, 9, 24, 9, 31)
    quotes = [_quote(10.4, 4, 25_000_000, code=str(600000 + i)) for i in range(100)]
    engine.observe_batch(quotes, at, {q["code"] for q in quotes})
    assert sum(p["stage"] == "observation" for p in packets) == 100
    assert packets[-1]["gate_inputs"]["early_watch_count"] == 100
    state = engine._states["600001"]
    state.candidate_at = at
    state.stage = "candidate"
    packet_count = len(packets)
    quote = _quote(10.4, 4, 25_000_000)
    quote["source_quote_at"] = "invalid"
    engine.observe_batch([quote], at + timedelta(seconds=30), {"600001"})
    extra = packets[packet_count:]
    assert any(p["stage"] == "unknown" and p["original_candidate"] is True for p in extra)
    assert any(p["stage"] == "coverage_blocked" for p in extra)
    assert engine._maybe_release_coverage_block(
        state, _quote(10.2, 2, 26_000_000), at + timedelta(seconds=60), 2.0, 30.0)
    assert any(p["stage"] == "reset"
               and p["reason"] == "original_candidate_coverage_rearm" for p in packets)


def test_episode_stable_across_scans_and_only_new_confirmation_gets_clock(monkeypatch):
    packets = []
    install_sink(monkeypatch, "on", packets)
    at = datetime(2026, 9, 24, 10)
    for stage, seconds in (("static_gate", 0), ("confirmed", 30), ("static_gate", 60)):
        shadow._capture_candidate_projection(
            shadow.ROUTE_B, at + timedelta(seconds=seconds), code="600001",
            quote={"code": "600001", "quote_round_id": f"qr-{seconds}"},
            stage=stage, reason="test", original_candidate=True,
            original_confirmed=True, original_gate=True if stage == "static_gate" else None)
    assert len({p["episode_id"] for p in packets}) == 1
    assert len({p["scan_id"] for p in packets}) == 3
    assert "original_confirmed_at" not in packets[0]["identities"]
    assert packets[1]["identities"]["original_confirmed_at"] == packets[1]["observed_at"]
    assert "original_confirmed_at" not in packets[2]["identities"]


def _actual_runtime(monkeypatch, tmp_path, clock):
    import app.paper.candidate_shadow as runtime_module
    from app.paper import intraday_route_research as core

    class ClockMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class PredicateClock(datetime, metaclass=ClockMeta):
        @classmethod
        def now(cls, tz=None):
            return clock[0]  # real datetime leaves, not subclass instances

        @classmethod
        def fromisoformat(cls, value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(shadow, "datetime", PredicateClock)
    monkeypatch.setattr(momentum, "datetime", PredicateClock)
    monkeypatch.setattr(runtime_module, "_now", lambda: clock[0])
    runtime = runtime_module._Runtime(tmp_path, {
        "challenger_b": {"account_id": 101, "strategy_version": "test-execution-b"},
    })
    runtime.core = core
    records = []
    runtime.emit = records.append  # inspect worker outputs, no filesystem publication
    monkeypatch.setattr(runtime_module, "_runtime", runtime)
    return runtime, records


def test_a2_5000_quotes_do_not_overflow_real_runtime_frame_queue(monkeypatch, tmp_path):
    clock = [datetime(2026, 9, 24, 9, 31)]
    runtime, records = _actual_runtime(monkeypatch, tmp_path, clock)
    engine = momentum.MomentumRetestShadowEngine(_policy())
    quotes = [_quote(10.2, 2, 25_000_000, code=str(600000 + i), avg_price=10.1)
              for i in range(5000)]
    engine.observe_batch(quotes, clock[0], {q["code"] for q in quotes})
    assert runtime.queue.qsize() == 1
    assert runtime.generation == 0
    assert runtime.counts["captured_frame"] == 1
    _drain_runtime(runtime)
    receipt = next(r for r in records if r["kind"] == "scan_receipt")
    assert receipt["capture_input"]["gate_inputs"]["visited_quote_count"] == 5000
    assert runtime.counts["queue_full"] == 0


def _drain_runtime(runtime):
    while not runtime.queue.empty():
        kind, packet, generation = runtime.queue.get_nowait()
        runtime.boundary()
        if kind == "frame":
            runtime.frame(packet)
        else:
            runtime.quotes(packet)


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate_delay", [0, 2])
async def test_real_capture_worker_b2_two_frames_not_original_three(shadow_env, monkeypatch, tmp_path, predicate_delay):
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", False)
    _freeze_shadow_today(monkeypatch, date(2026, 9, 1))
    clock = [datetime(2026, 9, 1, 9, 40)]
    runtime, records = _actual_runtime(monkeypatch, tmp_path, clock)
    async with shadow_env() as db:
        await _seed_structures(db)
        originals = []
        for seconds in (0, 30, 60):
            at = datetime(2026, 9, 1, 9, 40) + timedelta(seconds=seconds)
            clock[0] = at  # deterministic natural same-frame capture
            quotes = [generic_quote(f"60000{i}") for i in range(1, 5)]
            for q in quotes:
                q.update(source_quote_at=at.isoformat(), received_at=at.isoformat(),
                         updated_at=at.isoformat(), quote_round_id=f"natural-{seconds}")
                if q["code"] != "600001":
                    q.update(price=9.9, avg_price=9.9)
            quotes[0].update(price=10.15, high=10.2, avg_price=10.02 + seconds / 3000)
            from app.paper.candidate_shadow import capture_quotes
            capture_quotes(quotes, observed_at=at, round_id=f"natural-{seconds}")
            _drain_runtime(runtime)
            clock[0] = at + timedelta(seconds=predicate_delay)
            await shadow.scan_strategy_iteration_shadow(db, quotes, at)
            _drain_runtime(runtime)
            originals.append(await db.scalar(select(func.count()).select_from(PaperShadowEvent).where(
                PaperShadowEvent.route_id == shadow.ROUTE_B, PaperShadowEvent.event_type == "confirmed")))
            if seconds == 30:
                candidates = [r for r in records if r.get("kind") == "frame" and r.get("route") == "B2"]
                assert any(r["result"]["candidate"]["value"] is True for r in candidates), (
                    runtime.status(), [(r.get("capture_input", {}).get("stage"),
                    r["result"]["candidate"]) for r in candidates])
                assert originals == [0, 0]
        assert originals == [0, 0, 1]
        b = [r for r in records if r.get("kind") == "frame" and r.get("route") == "B2"]
        assert len({r["candidate_id"] for r in b if r["capture_input"].get("code") == "600001"}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("predicate_delay", [0, 2])
async def test_real_capture_worker_c3_confirmation_clock_is_evaluable(shadow_env, monkeypatch, tmp_path, predicate_delay):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE", "2026-09-04")
    _freeze_shadow_today(monkeypatch, date(2026, 9, 4))
    clock = [datetime(2026, 9, 4, 10)]
    runtime, records = _actual_runtime(monkeypatch, tmp_path, clock)
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
        for seconds in (0, 30, 60):
            at = datetime(2026, 9, 4, 10) + timedelta(seconds=seconds)
            clock[0] = at
            quotes = [_first_board_quote("600201", confirms=True),
                      _first_board_quote("600202", confirms=False)]
            for q in quotes:
                q.update(source_quote_at=at.isoformat(), received_at=at.isoformat(),
                         updated_at=at.isoformat(), quote_round_id=f"natural-c3-{seconds}")
            quotes[0]["avg_price"] += seconds / 3000
            from app.paper.candidate_shadow import capture_quotes
            capture_quotes(quotes, observed_at=at, round_id=f"natural-c3-{seconds}")
            _drain_runtime(runtime)
            clock[0] = at + timedelta(seconds=predicate_delay)
            await shadow.scan_strategy_iteration_shadow(db, quotes, at)
            _drain_runtime(runtime)
        c = [r for r in records if r.get("kind") == "frame" and r.get("route") == "C3"
             and r.get("capture_input", {}).get("code") == "600201"]
        assert any(r["result"]["baseline"]["value"] is True
                   and r["result"]["candidate"]["value"] is True for r in c), (
                   runtime.status(), [(r["capture_input"]["stage"], r["result"]) for r in c])
        confirmations = [r for r in c if r["capture_input"]["stage"] == "confirmed"]
        assert confirmations
        for row in confirmations:
            assert row["candidate_input"]["original_confirmed_at"] == row["capture_input"]["observed_at"]
