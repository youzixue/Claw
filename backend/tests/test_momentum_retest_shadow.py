from datetime import date, datetime, time, timedelta
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models import paper as paper_models  # noqa: F401
from app.models import stock as stock_models  # noqa: F401
from app.models.paper import PaperShadowEvaluation, PaperShadowEvent
from app.models.stock import StockKline
from app.paper.momentum_retest_shadow import (
    MomentumRetestPolicy,
    MomentumRetestShadowEngine,
    _persist_shadow_events,
    build_evidence_summary,
    settle_momentum_retest_shadow,
)


def _policy() -> MomentumRetestPolicy:
    return MomentumRetestPolicy(
        version="test_v1",
        start=time(9, 35),
        candidate_end=time(14, 30),
        confirm_end=time(14, 50),
        rearm_max_change_pct=2.5,
        candidate_min_change_pct=3.0,
        candidate_max_change_pct=6.0,
        min_volume_ratio=0.8,
        max_volume_ratio=5.0,
        min_amount=20_000_000,
        min_pullback_pct=0.5,
        max_pullback_pct=1.8,
        min_hold_change_pct=2.5,
        min_recovery_pct=0.3,
        max_peak_gap_pct=0.8,
        min_60s_change_pct=0.15,
        min_amount_pace_ratio=0.8,
        min_orderbook_imbalance=-0.2,
        max_withdrawal_ratio=0.5,
        max_vwap_break_pct=0.2,
        max_quote_gap_sec=90,
        min_track_sec=60,
        max_track_sec=1200,
        max_confirm_wait_sec=480,
    )


def _quote(
    price: float,
    change_pct: float,
    amount: float,
    *,
    code: str = "600001",
    avg_price: float = 10.30,
    ask_price: float | None = None,
) -> dict:
    return {
        "code": code,
        "name": "测试股份",
        "price": price,
        "prev_close": 10.0,
        "change_pct": change_pct,
        "high": max(price, 10.5),
        "low": 10.0,
        "limit_up": 11.0,
        "amount": amount,
        "volume_ratio": 1.5,
        "avg_price": avg_price,
        "ask1_price": ask_price if ask_price is not None else price + 0.01,
        "ask1_volume": 500,
        "bid1_price": price,
        "bid1_volume": 600,
        "orderbook_imbalance": 0.10,
        "support_strength_score": 65.0,
        "withdrawal_ratio": 0.10,
    }


def _run_confirmed_path(*, final_overrides=None) -> MomentumRetestShadowEngine:
    engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 34)
    path = [
        (start, _quote(10.20, 2.0, 18_000_000)),
        (start + timedelta(minutes=1), _quote(10.40, 4.0, 22_000_000)),
        (start + timedelta(minutes=1, seconds=30), _quote(10.50, 5.0, 24_000_000)),
        (start + timedelta(minutes=2), _quote(10.42, 4.2, 26_000_000)),
        (start + timedelta(minutes=2, seconds=30), _quote(10.41, 4.1, 27_500_000)),
        (start + timedelta(minutes=3), _quote(10.46, 4.6, 31_000_000)),
    ]
    if final_overrides:
        path[-1][1].update(final_overrides)
    for observed_at, quote in path:
        engine.observe_batch([quote], observed_at, {"600001"})
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_limit,confirmed", [(6, True), (2, False)])
async def test_scheduler_buffer_preserves_real_state_machine_path_and_persists_events(
    tmp_path, monkeypatch, queue_limit, confirmed,
):
    from unittest.mock import AsyncMock
    from app.data import scheduler as scheduler_module
    from app.paper import momentum_retest_shadow as module

    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a2-buffer.db'}")
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(database, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "async_session", factory)
    monkeypatch.setattr(module, "shadow_engine", MomentumRetestShadowEngine(_policy()))
    monkeypatch.setattr(module, "_hydrated_date", None)
    monkeypatch.setattr(module, "_load_allowed_codes", AsyncMock(return_value={"600001"}))
    monkeypatch.setattr(module.settings, "PAPER_MOMENTUM_RETEST_QUOTE_INBOX_MAX_BATCHES", queue_limit)
    scheduler = scheduler_module.DataScheduler()
    start = datetime(2026, 8, 31, 9, 34)
    points = [
        (0, 10.20, 2.0, 18_000_000), (60, 10.40, 4.0, 22_000_000),
        (90, 10.50, 5.0, 24_000_000), (120, 10.42, 4.2, 26_000_000),
        (150, 10.41, 4.1, 27_500_000), (180, 10.46, 4.6, 31_000_000),
    ]
    try:
        for index, (seconds, price, change, amount) in enumerate(points):
            at = start + timedelta(seconds=seconds)
            quote = _quote(price, change, amount)
            quote["source_quote_at"] = at
            frame = {
                "round_id": f"isolated-a2-{index}", "committed_at": at,
                "quality_status": "ok", "records": [quote],
            }
            scheduler._publish_quote_round(frame)
        # 交易消费者尚未运行，latest-only原实现只剩最终一帧，将无法证明首次路径。
        await scheduler._drain_momentum_quote_rounds(frame)
        async with factory() as db:
            events = (await db.scalars(select(PaperShadowEvent).order_by(PaperShadowEvent.id))).all()
        types = [event.event_type for event in events]
        if confirmed:
            assert types == ["armed", "candidate", "pullback", "confirmed"]
            assert events[-1].observed_at == start + timedelta(seconds=180)
            assert events[-1].price == 10.46
            assert json.loads(events[-1].snapshot_json)["coverage_policy"] == "explicit_consumer_loss_v1"
        else:
            assert types == ["coverage_blocked"]
            extra = json.loads(events[0].snapshot_json)["extra"]
            assert extra["consumer_coverage_loss"]["dropped_rounds"] == 4
        assert not scheduler._momentum_quote_inbox
    finally:
        await database.dispose()


@pytest.mark.parametrize("field,value,passed", [
    ("volume_ratio", .8, True), ("volume_ratio", 5, True),
    ("volume_ratio", .79, False), ("volume_ratio", 5.16, False),
    ("amount", 31_000_000, True), ("amount", 19_000_000, False),
    ("orderbook_imbalance", -.2, True), ("orderbook_imbalance", -.21, False),
    ("withdrawal_ratio", 0, True), ("withdrawal_ratio", .5, True),
    ("withdrawal_ratio", .51, False), ("withdrawal_ratio", -.1, False),
    *[(field, value, False)
      for field in ("volume_ratio", "amount", "orderbook_imbalance", "withdrawal_ratio")
      for value in (None, float("nan"), float("inf"), "", "invalid")],
])
def test_candidate_confirmation_and_execution_share_liquidity_contract(field, value, passed):
    from types import SimpleNamespace
    from app.paper.strategy_iteration_challenger import _live_route_confirmation_valid

    quote = _quote(10.46, 4.6, 31_000_000)
    quote[field] = value
    engine = _run_confirmed_path(final_overrides={field: value})
    events = engine.pending_events()
    assert any(event["event_type"] == "confirmed" for event in events) == passed
    assert (not engine._candidate_gate_reasons(quote)) == passed
    snapshot = {"rule_snapshot": engine.policy.snapshot(), "state": {"peak_price": 10.5}}
    live_ok, reason = _live_route_confirmation_valid(
        SimpleNamespace(route_id="momentum_first_retest"), snapshot, SimpleNamespace(**quote),
    )
    assert live_ok == passed
    if not passed:
        assert engine.states["600001"].stage == "pullback"
        blocked = [event for event in events if event["event_type"] == "confirmation_screened"]
        assert len(blocked) == 1
        # 浏览器必须能严格JSON解析，未知数值保留null，不输出NaN/Infinity。
        def reject_constant(raw):
            raise AssertionError(raw)
        evidence = json.loads(blocked[0]["snapshot_json"], parse_constant=reject_constant)
        assert evidence["extra"]["liquidity_issues"]
        assert reason
        # 尚未确认的首次回踩可在原窗口内继续观察，不消耗confirmed事件身份。
        engine.observe_batch(
            [_quote(10.47, 4.7, 36_000_000)], datetime(2026, 8, 31, 9, 37, 30), {"600001"},
        )
        assert engine.states["600001"].stage == "confirmed"


def test_liquidity_contract_honors_zero_limits_and_rejects_invalid_rules():
    from types import SimpleNamespace
    from app.paper.momentum_retest_shadow import momentum_liquidity_gate_issues
    from app.paper.strategy_iteration_challenger import _live_route_confirmation_valid

    quote = _quote(10.46, 4.6, 31_000_000)
    rules = _policy().snapshot()
    rules["max_withdrawal_ratio"] = 0
    assert momentum_liquidity_gate_issues(quote, rules)[0]["code"] == "out_of_range_withdrawal_ratio"
    valid, _ = _live_route_confirmation_valid(
        SimpleNamespace(route_id="momentum_first_retest"),
        {"rule_snapshot": rules, "state": {"peak_price": 10.5}}, SimpleNamespace(**quote),
    )
    assert not valid  # 不得通过“or 0.5”把显式0上限放宽。
    quote["withdrawal_ratio"] = 0
    assert momentum_liquidity_gate_issues(quote, rules) == []
    for overrides in ({"max_volume_ratio": None}, {"min_volume_ratio": 6},
                      {"min_amount": float("nan")}, {"max_withdrawal_ratio": -1}):
        result = momentum_liquidity_gate_issues(quote, {**rules, **overrides})
        assert result[0]["code"] == "invalid_rule_snapshot"
        assert result[0]["recoverable"] is False


def test_known_consumer_loss_blocks_even_short_quote_gap_and_late_arrivals():
    engine = MomentumRetestShadowEngine(_policy())
    at = datetime(2026, 8, 31, 9, 34)
    engine.observe_batch([_quote(10.20, 2, 18_000_000)], at, {"600001", "600002"})
    loss = {"reason": "consumer_inbox_overflow", "dropped_rounds": 1}
    events = engine.observe_batch(
        [_quote(10.40, 4, 22_000_000)], at + timedelta(seconds=30),
        {"600001", "600002"}, coverage_loss=loss,
    )
    assert [event["event_type"] for event in events] == ["armed", "coverage_blocked"]
    assert json.loads(events[-1]["snapshot_json"])["extra"]["consumer_coverage_loss"] == loss
    engine.observe_batch(
        [_quote(10.20, 2, 24_000_000, code="600002")], at + timedelta(minutes=1),
        {"600001", "600002"},
    )
    assert engine.states["600002"].stage == "coverage_blocked"
    assert not any(event["event_type"] == "confirmed" for event in engine.pending_events())


def test_consumer_loss_survives_restore_but_not_new_trading_date():
    engine = MomentumRetestShadowEngine(_policy())
    at = datetime(2026, 8, 31, 9, 34)
    events = engine.observe_batch(
        [_quote(10.20, 2, 18_000_000)], at, {"600001"},
        coverage_loss={"reason": "consumer_inbox_overflow", "dropped_rounds": 1},
    )
    restored = MomentumRetestShadowEngine(_policy())
    restored.restore([PaperShadowEvent(**event) for event in events])
    restored.observe_batch(
        [_quote(10.20, 2, 18_000_000, code="600002")], at + timedelta(seconds=30),
        {"600002"},
    )
    assert restored.states["600002"].stage == "coverage_blocked"
    restored.observe_batch(
        [_quote(10.20, 2, 18_000_000, code="600002")], at + timedelta(days=1),
        {"600002"},
    )
    assert restored.states["600002"].stage == "armed"


def test_later_consumer_loss_does_not_rewrite_prior_confirmed_event():
    engine = _run_confirmed_path()
    confirmed_before = [event for event in engine.pending_events() if event["event_type"] == "confirmed"]
    engine.observe_batch(
        [_quote(10.50, 5, 35_000_000)], datetime(2026, 8, 31, 9, 38), {"600001"},
        coverage_loss={"reason": "consumer_inbox_overflow", "dropped_rounds": 1},
    )
    assert engine.states["600001"].stage == "confirmed"
    assert [event for event in engine.pending_events() if event["event_type"] == "confirmed"] == confirmed_before


def test_empty_degraded_frame_cannot_silently_rearm_on_next_quote():
    engine = MomentumRetestShadowEngine(_policy())
    at = datetime(2026, 8, 31, 9, 34)
    assert engine.observe_batch(
        [], at, {"600001"}, coverage_loss={"reason": "degraded_quote_round"},
    ) == []
    engine.observe_batch([_quote(10.20, 2, 18_000_000)], at + timedelta(seconds=30), {"600001"})
    assert engine.states["600001"].stage == "coverage_blocked"


def test_requires_observed_below_band_before_claiming_first_pullback():
    engine = MomentumRetestShadowEngine(_policy())

    events = engine.observe_batch(
        [_quote(10.40, 4.0, 30_000_000)],
        datetime(2026, 8, 31, 10, 0),
        {"600001"},
    )

    assert [item["event_type"] for item in events] == ["coverage_blocked"]
    payload = json.loads(events[0]["snapshot_json"])
    assert payload["point_in_time_only"] is True
    assert payload["extra"]["point_in_time_coverage"] == "insufficient"


def test_prestart_entry_into_strong_band_cannot_be_relabelled_as_first_candidate():
    engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 30)
    path = [
        (start, _quote(10.20, 2.0, 18_000_000)),
        (start + timedelta(minutes=1), _quote(10.40, 4.0, 22_000_000)),
        (start + timedelta(minutes=2), _quote(10.41, 4.1, 23_000_000)),
        (start + timedelta(minutes=3), _quote(10.40, 4.0, 24_000_000)),
        (start + timedelta(minutes=4), _quote(10.39, 3.9, 26_000_000)),
        (start + timedelta(minutes=5), _quote(10.38, 3.8, 28_000_000)),
    ]
    for observed_at, quote in path:
        engine.observe_batch([quote], observed_at, {"600001"})

    events = engine.pending_events()
    assert [item["event_type"] for item in events] == ["armed", "coverage_blocked"]
    payload = json.loads(events[-1]["snapshot_json"])
    assert payload["extra"]["point_in_time_coverage"] == "entered_band_before_route_start"


def test_new_stock_after_candidate_window_is_ignored_not_mislabeled_as_coverage_gap():
    engine = MomentumRetestShadowEngine(_policy())

    events = engine.observe_batch(
        [_quote(10.40, 4.0, 30_000_000)],
        datetime(2026, 8, 31, 14, 31),
        {"600001"},
    )

    assert events == []
    assert engine.states["600001"].stage == "unseen"


def test_missing_realtime_rounds_block_path_but_lunch_break_does_not():
    interrupted = MomentumRetestShadowEngine(_policy())
    interrupted.observe_batch(
        [_quote(10.20, 2.0, 18_000_000)],
        datetime(2026, 8, 31, 9, 34),
        {"600001"},
    )
    interrupted.observe_batch(
        [_quote(10.40, 4.0, 24_000_000)],
        datetime(2026, 8, 31, 9, 36),
        {"600001"},
    )
    gap_events = interrupted.pending_events()
    assert [item["event_type"] for item in gap_events] == ["armed", "coverage_blocked"]
    gap_payload = json.loads(gap_events[-1]["snapshot_json"])
    assert gap_payload["extra"]["point_in_time_coverage"] == "quote_gap"

    lunch = MomentumRetestShadowEngine(_policy())
    lunch.observe_batch(
        [_quote(10.20, 2.0, 80_000_000)],
        datetime(2026, 8, 31, 11, 29, 30),
        {"600001"},
    )
    lunch.observe_batch(
        [_quote(10.40, 4.0, 82_000_000)],
        datetime(2026, 8, 31, 13, 0),
        {"600001"},
    )
    assert [item["event_type"] for item in lunch.pending_events()] == ["armed", "candidate"]


@pytest.mark.parametrize("gap_seconds", [93, 94, 306])
def test_quote_gap_does_not_silently_rearm_first_pullback(gap_seconds):
    """恢复新鲜报价不能补造断档期间的首次回踩路径。"""
    engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 9, 14, 9, 30)
    engine.observe_batch([_quote(10.20, 2.0, 18_000_000)], start, {"600001"})
    resumed_at = start + timedelta(seconds=gap_seconds)
    engine.observe_batch(
        [_quote(10.21, 2.1, 20_000_000)], resumed_at, {"600001"}
    )
    engine.observe_batch(
        [_quote(10.40, 4.0, 24_000_000)],
        resumed_at + timedelta(seconds=30),
        {"600001"},
    )

    events = engine.pending_events()
    assert [item["event_type"] for item in events] == ["armed", "coverage_blocked"]
    payload = json.loads(events[-1]["snapshot_json"])
    assert payload["extra"]["quote_gap_sec"] == gap_seconds
    assert payload["extra"]["max_quote_gap_sec"] == 90
    assert engine.states["600001"].stage == "coverage_blocked"


def test_first_pullback_requires_recovery_price_volume_and_book_confirmation():
    engine = _run_confirmed_path()
    events = engine.pending_events()

    assert [item["event_type"] for item in events] == [
        "armed",
        "candidate",
        "pullback",
        "confirmed",
    ]
    confirmed = events[-1]
    payload = json.loads(confirmed["snapshot_json"])
    assert confirmed["assumed_fill_price"] > payload["quote"]["ask1_price"]
    assert payload["extra"]["rolling_60s"]["change_pct"] >= 0.15
    assert payload["extra"]["automatic_order_connected"] is False
    assert payload["extra"]["execution_mode"] == "shadow_only"


def test_screened_reason_is_recorded_and_quality_can_improve_into_candidate():
    engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 34)
    engine.observe_batch(
        [_quote(10.20, 2.0, 10_000_000)],
        start,
        {"600001"},
    )
    engine.observe_batch(
        [_quote(10.35, 3.5, 15_000_000)],
        start + timedelta(minutes=1),
        {"600001"},
    )
    engine.observe_batch(
        [_quote(10.38, 3.8, 22_000_000)],
        start + timedelta(minutes=1, seconds=30),
        {"600001"},
    )

    events = engine.pending_events()
    assert [item["event_type"] for item in events] == ["armed", "screened", "candidate"]
    screened = json.loads(events[1]["snapshot_json"])
    assert "累计成交额不足" in screened["extra"]["candidate_gate_reasons"]


def test_straight_line_rise_never_becomes_confirmation():
    engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 34)
    path = [
        (start, _quote(10.20, 2.0, 18_000_000)),
        (start + timedelta(minutes=1), _quote(10.35, 3.5, 22_000_000)),
        (start + timedelta(minutes=2), _quote(10.42, 4.2, 26_000_000)),
        (start + timedelta(minutes=3), _quote(10.50, 5.0, 31_000_000)),
    ]
    for observed_at, quote in path:
        engine.observe_batch([quote], observed_at, {"600001"})

    assert [item["event_type"] for item in engine.pending_events()] == ["armed", "candidate"]


def test_deep_pullback_is_invalidated_instead_of_confirmed():
    engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 34)
    path = [
        (start, _quote(10.20, 2.0, 18_000_000)),
        (start + timedelta(minutes=1), _quote(10.40, 4.0, 22_000_000)),
        (start + timedelta(minutes=1, seconds=30), _quote(10.50, 5.0, 24_000_000)),
        (start + timedelta(minutes=2), _quote(10.28, 2.8, 27_000_000)),
    ]
    for observed_at, quote in path:
        engine.observe_batch([quote], observed_at, {"600001"})

    assert [item["event_type"] for item in engine.pending_events()] == [
        "armed",
        "candidate",
        "invalidated",
    ]
    assert engine.states["600001"].stage == "invalidated"


def test_out_of_order_or_duplicate_snapshot_cannot_advance_state():
    engine = MomentumRetestShadowEngine(_policy())
    observed_at = datetime(2026, 8, 31, 9, 34)
    quote = _quote(10.20, 2.0, 18_000_000)
    engine.observe_batch([quote], observed_at, {"600001"})
    engine.observe_batch([quote], observed_at, {"600001"})
    engine.observe_batch([quote], observed_at - timedelta(seconds=30), {"600001"})

    assert engine.states["600001"].observation_count == 1
    assert [item["event_type"] for item in engine.pending_events()] == ["armed"]


def test_explicit_cross_day_or_future_source_clock_is_rejected():
    engine = MomentumRetestShadowEngine(_policy())
    committed_at = datetime(2026, 8, 31, 9, 34)
    cross_day = _quote(10.20, 2.0, 18_000_000)
    cross_day["source_quote_at"] = datetime(2026, 8, 30, 15, 0)
    future = _quote(10.20, 2.0, 18_000_000)
    future["source_quote_at"] = committed_at + timedelta(seconds=30)

    assert engine.observe_batch([cross_day], committed_at, {"600001"}) == []
    assert engine.observe_batch([future], committed_at, {"600001"}) == []
    assert "600001" not in engine.states

    valid = _quote(10.20, 2.0, 18_000_000)
    valid["source_quote_at"] = committed_at - timedelta(seconds=5)
    events = engine.observe_batch([valid], committed_at, {"600001"})

    assert [item["event_type"] for item in events] == ["armed"]
    assert engine.states["600001"].last_at == valid["source_quote_at"]


def test_armed_pool_survives_short_restart_gap_and_can_form_candidate():
    first_engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 34)
    first_engine.observe_batch(
        [_quote(10.20, 2.0, 18_000_000)],
        start,
        {"600001"},
    )
    armed = first_engine.pending_events()[0]
    assert armed["event_type"] == "armed"

    restored_engine = MomentumRetestShadowEngine(_policy())
    restored_engine.restore([PaperShadowEvent(**armed)])
    restored_engine.observe_batch(
        [_quote(10.40, 4.0, 24_000_000)],
        start + timedelta(seconds=60),
        {"600001"},
    )

    assert restored_engine.states["600001"].stage == "candidate"
    assert [item["event_type"] for item in restored_engine.pending_events()] == ["candidate"]


def test_armed_pool_restart_with_long_quote_gap_fails_closed():
    first_engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 34)
    first_engine.observe_batch(
        [_quote(10.20, 2.0, 18_000_000)],
        start,
        {"600001"},
    )
    armed = first_engine.pending_events()[0]

    restored_engine = MomentumRetestShadowEngine(_policy())
    restored_engine.restore([PaperShadowEvent(**armed)])
    restored_engine.observe_batch(
        [_quote(10.40, 4.0, 24_000_000)],
        start + timedelta(seconds=120),
        {"600001"},
    )

    events = restored_engine.pending_events()
    assert [item["event_type"] for item in events] == ["coverage_blocked"]
    payload = json.loads(events[0]["snapshot_json"])
    assert payload["extra"]["point_in_time_coverage"] == "quote_gap"


def test_restart_interrupts_nonterminal_path_instead_of_claiming_first_retest():
    first_engine = MomentumRetestShadowEngine(_policy())
    start = datetime(2026, 8, 31, 9, 34)
    first_engine.observe_batch(
        [_quote(10.20, 2.0, 18_000_000)],
        start,
        {"600001"},
    )
    first_engine.observe_batch(
        [_quote(10.40, 4.0, 22_000_000)],
        start + timedelta(minutes=1),
        {"600001"},
    )
    candidate = first_engine.pending_events()[-1]

    restored_engine = MomentumRetestShadowEngine(_policy())
    restored_engine.restore([PaperShadowEvent(**candidate)])
    restored_engine.observe_batch(
        [_quote(10.42, 4.2, 26_000_000)],
        start + timedelta(minutes=2),
        {"600001"},
    )

    events = restored_engine.pending_events()
    assert [item["event_type"] for item in events] == ["coverage_blocked"]
    payload = json.loads(events[0]["snapshot_json"])
    assert payload["extra"]["point_in_time_coverage"] == "interrupted_by_restart"


def _evaluation(index: int, net: float, excess: float) -> PaperShadowEvaluation:
    signal_day = date(2026, 1, 1) + timedelta(days=index // 5)
    return PaperShadowEvaluation(
        signal_event_key=f"signal-{index}",
        route_id="momentum_first_retest",
        route_version="test_v1",
        code=f"60{index:04d}",
        signal_trade_date=signal_day,
        signal_time=datetime.combine(signal_day, time(10, 0)),
        horizon_days=3,
        exit_trade_date=signal_day + timedelta(days=3),
        signal_price=10.0,
        exit_price=10.1,
        net_return_pct=net,
        excess_return_pct=excess,
        max_adverse_pct=-1.5,
    )


def test_evidence_gate_can_pass_but_never_enables_execution():
    rows = [_evaluation(index, 1.0, 0.6) for index in range(100)]

    evidence = build_evidence_summary(rows)

    assert evidence["evidence_gate_passed"] is True
    assert evidence["execution_enabled"] is False
    assert evidence["promotion_requires_manual_review"] is True


def test_top_winner_dependency_fails_robustness_gate():
    rows = [_evaluation(index, -0.2, -0.3) for index in range(99)]
    rows.append(_evaluation(99, 100.0, 99.0))

    evidence = build_evidence_summary(rows)

    assert evidence["avg_net_return_pct"] > 0
    assert evidence["gates"]["top5_removed_still_positive"] is False
    assert evidence["evidence_gate_passed"] is False


def test_single_day_and_single_code_concentration_cannot_pass_evidence_gate():
    rows = [_evaluation(index, 1.0, 0.5) for index in range(100)]
    for item in rows:
        item.signal_trade_date = date(2026, 1, 5)
        item.signal_time = datetime(2026, 1, 5, 10, 0)
        item.code = "600001"

    evidence = build_evidence_summary(rows)

    assert evidence["max_session_share"] == 1.0
    assert evidence["max_code_share"] == 1.0
    assert evidence["gates"]["session_concentration_acceptable"] is False
    assert evidence["gates"]["code_concentration_acceptable"] is False
    assert evidence["evidence_gate_passed"] is False


@pytest.mark.asyncio
async def test_shadow_event_persistence_chunks_full_market_batch(tmp_path):
    db_path = tmp_path / "shadow-batch.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    observed_at = datetime(2026, 9, 1, 9, 30)
    pending = [
        {
            "event_key": f"momentum_first_retest:test_v1:2026-09-01:{index:06d}:armed",
            "route_id": "momentum_first_retest",
            "route_version": "test_v1",
            "trade_date": observed_at.date(),
            "observed_at": observed_at,
            "code": f"{index:06d}",
            "name": f"测试{index}",
            "event_type": "armed",
            "status": "armed",
            "price": 10.0,
            "assumed_fill_price": None,
            "change_pct": 0.0,
            "snapshot_json": "{}",
            "created_at": observed_at,
        }
        for index in range(2600)
    ]
    async with SessionLocal() as session:
        await _persist_shadow_events(session, pending)
        count = await session.scalar(select(func.count()).select_from(PaperShadowEvent))

    assert count == 2600
    await engine.dispose()


@pytest.mark.asyncio
async def test_settlement_is_append_only_cost_aware_and_idempotent(tmp_path):
    db_path = tmp_path / "shadow.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    SessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    event_key = "momentum_first_retest:test_v1:2026-08-31:600001:confirmed"
    original_snapshot = json.dumps({"immutable": "original"}, ensure_ascii=False)
    async with SessionLocal() as session:
        session.add(PaperShadowEvent(
            event_key=event_key,
            route_id="momentum_first_retest",
            route_version="test_v1",
            trade_date=date(2026, 8, 31),
            observed_at=datetime(2026, 8, 31, 10, 0),
            code="600001",
            name="测试股份",
            event_type="confirmed",
            status="confirmed",
            price=10.0,
            assumed_fill_price=10.01,
            change_pct=4.0,
            snapshot_json=original_snapshot,
        ))
        session.add_all([
            StockKline(
                code="600001",
                trade_date=date(2026, 9, 1),
                open=10.0,
                high=10.5,
                low=9.8,
                close=10.2,
                change_pct=2.0,
                prev_close=10.0,
            ),
            StockKline(
                code="600001",
                trade_date=date(2026, 9, 2),
                open=10.2,
                high=10.7,
                low=10.0,
                close=10.4,
                change_pct=1.96,
                prev_close=10.2,
            ),
            StockKline(
                code="600001",
                trade_date=date(2026, 9, 3),
                open=10.4,
                high=10.8,
                low=10.1,
                close=10.6,
                change_pct=1.92,
                prev_close=10.4,
            ),
        ])
        await session.commit()

    async with SessionLocal() as session:
        first = await settle_momentum_retest_shadow(
            session,
            as_of_date=date(2026, 9, 3),
        )
    async with SessionLocal() as session:
        second = await settle_momentum_retest_shadow(
            session,
            as_of_date=date(2026, 9, 3),
        )
        evaluations = (
            await session.execute(
                select(PaperShadowEvaluation)
                .where(PaperShadowEvaluation.signal_event_key == event_key)
                .order_by(PaperShadowEvaluation.horizon_days)
            )
        ).scalars().all()
        original = await session.scalar(
            select(PaperShadowEvent).where(PaperShadowEvent.event_key == event_key)
        )

    assert first == {"signals": 1, "evaluations_added": 2}
    assert second == {"signals": 1, "evaluations_added": 0}
    assert [item.horizon_days for item in evaluations] == [1, 3]
    assert all(item.net_return_pct < item.gross_return_pct for item in evaluations)
    assert original is not None
    assert original.snapshot_json == original_snapshot
    await engine.dispose()
