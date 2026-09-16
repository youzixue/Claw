"""Current C3 read model is separate from frozen first-hit/evaluation evidence."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.config.settings import settings
from app.models.paper import PaperShadowEvent
from app.models.stock import QuoteRound, SectorPersistence
from app.paper import strategy_iteration_shadow as shadow
from test_strategy_iteration_shadow import (
    shadow_env, _first_board_quote, _seed_first_board_denominator, _freeze_shadow_today,
)


@pytest.fixture(autouse=True)
def current_pool_config(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE", "2026-09-04")
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", 75)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES", 3)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC", 60)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC", 75)


def frame(at, *, codes=("600201",), eligible=True, passing=True, blocked=False, legacy=False):
    return PaperShadowEvent(
        id=int(at.timestamp()), route_id=shadow.ROUTE_C3,
        route_version=shadow.route_version_for(shadow.ROUTE_C3), trade_date=at.date(),
        observed_at=at, event_type="coverage_blocked" if blocked else "universe_audit",
        snapshot_json=json.dumps({"prior_structure": {
            "reason": "行情缺失" if blocked else None, "quote_coverage": 0 if blocked else 1,
            "current_pool": {} if legacy else {
                "read_model_version": "c3_current_pool_v1",
                "members": [{"code": c, "eligible": eligible, "confirmation_frame": passing,
                             "source_quote_at": at.isoformat(), "reason": "等待确认"} for c in codes],
            },
        }}),
    )


def test_normal_current_and_empty_pool():
    at = datetime(2026, 9, 4, 10)
    frames = [frame(at + timedelta(seconds=i)) for i in (0, 30, 60)]
    result = shadow.project_first_board_current_pool(frames, now=at + timedelta(seconds=60))
    assert result["confirmed_count"] == 1
    assert result["cumulative_confirmed_today"] == 0  # independent first-hit ledger
    empty = shadow.project_first_board_current_pool(
        frames + [frame(at + timedelta(seconds=90), codes=())],
        now=at + timedelta(seconds=90), cumulative_confirmed_codes=["600201"],
    )
    assert empty["valid"] and empty["structural_count"] == 0
    assert empty["cumulative_confirmed_today"] == 1
    assert empty["invalidated"][0]["code"] == "600201"


@pytest.mark.parametrize("bad", ["coverage", "missing", "failed", "legacy", "round"])
def test_bad_intervening_frame_resets_confirmation(bad):
    at = datetime(2026, 9, 4, 10)
    middle = frame(at + timedelta(seconds=30),
                   blocked=bad == "coverage", codes=() if bad == "missing" else ("600201",),
                   passing=bad != "failed", legacy=bad == "legacy")
    result = shadow.project_first_board_current_pool(
        [frame(at), middle, frame(at + timedelta(seconds=60))],
        now=at + timedelta(seconds=60), cumulative_confirmed_codes=["600201"],
        invalid_quote_times=[at + timedelta(seconds=40)] if bad == "round" else [],
    )
    assert result["confirmed_count"] == 0
    assert result["cumulative_confirmed_today"] == 1


@pytest.mark.parametrize("clock,valid", [
    ("10:01:00", True), ("10:02:15", True), ("10:02:16", False),
    ("11:31:00", False), ("14:31:00", False),
])
def test_expiry_and_session_boundary(clock, valid):
    at = datetime(2026, 9, 4, 10)
    now = datetime.fromisoformat("2026-09-04T" + clock)
    result = shadow.project_first_board_current_pool(
        [frame(at + timedelta(seconds=i)) for i in (0, 30, 60)], now=now,
    )
    assert result["valid"] is valid
    if not valid:
        assert result["confirmed_count"] == 0 and result["reason"]


def test_old_day_future_and_legacy_not_current():
    at = datetime(2026, 9, 4, 10)
    for frames in ([frame(at - timedelta(days=1))], [frame(at + timedelta(seconds=1))],
                   [frame(at, legacy=True)], []):
        result = shadow.project_first_board_current_pool(frames, now=at)
        assert not result["valid"] and result["structural_count"] == 0


def test_repeated_quote_round_is_not_continuous_new_evidence():
    at = datetime(2026, 9, 4, 10)
    frames = [frame(at + timedelta(seconds=i)) for i in (0, 30, 60)]
    for row in frames:
        payload = json.loads(row.snapshot_json)
        payload["prior_structure"]["current_pool"]["quote_round_ids"] = ["same-round"]
        row.snapshot_json = json.dumps(payload)
    assert shadow.project_first_board_current_pool(
        frames, now=at + timedelta(seconds=60),
    )["confirmed_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", ["ok", "degraded"])
async def test_unscanned_quote_round_invalidates_current(shadow_env, quality):
    at = datetime(2026, 9, 4, 10)
    async with shadow_env() as db:
        row = frame(at)
        row.event_key = "read-model-test"
        row.code = "MARKET"
        row.name = "test"
        row.status = "complete"
        db.add(row)
        db.add(QuoteRound(
            round_id="not-scanned", trade_date=at.date(),
            committed_at=at + timedelta(seconds=1), as_of_at=at,
            expected_count=2, received_count=2, quality_status=quality,
            quality_reason="test", config_version="test", code_version="test",
        ))
        await db.commit()
        result = await shadow.build_first_board_current_pool(db, now=at + timedelta(seconds=2))
        assert not result["valid"]
        assert result["confirmed_count"] == 0
        assert result["latest_quote_round"]["round_id"] == "not-scanned"


@pytest.mark.asyncio
async def test_live_all_market_entry_exit_and_immutable_evidence(shadow_env, monkeypatch):
    at = datetime(2026, 9, 4, 10)
    _freeze_shadow_today(monkeypatch, at.date())
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
        sector = await db.scalar(select(SectorPersistence))
        sector.strength_score = 20
        await db.commit()
        quotes = [_first_board_quote(c, confirms=True) for c in ("600201", "600202")]
        await shadow.scan_strategy_iteration_shadow(db, quotes, at)
        empty = await shadow.build_first_board_current_pool(db, now=at)
        assert empty["valid"] and empty["structural_count"] == 0
        # Neither code needs yesterday's limit-up leaderboard to enter C3.
        sector.strength_score = 60
        await db.commit()
        for seconds in (30, 60, 90):
            for quote in quotes:
                quote["source_quote_at"] = (at + timedelta(seconds=seconds)).isoformat()
            await shadow.scan_strategy_iteration_shadow(db, quotes, at + timedelta(seconds=seconds))
        active = await shadow.build_first_board_current_pool(db, now=at + timedelta(seconds=90))
        assert active["confirmed_count"] == active["cumulative_confirmed_today"] == 2
        assert all(set(m) == {"code", "name", "eligible", "currently_confirmed", "reason"}
                   for m in active["members"])
        audits = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == shadow.ROUTE_C3,
            PaperShadowEvent.event_type == "universe_audit",
        ))).all())
        for audit in audits:
            members = json.loads(audit.snapshot_json)["prior_structure"]["current_pool"]["members"]
            assert all(set(m) == {"code", "name", "eligible", "confirmation_frame", "source_quote_at", "reason"}
                       for m in members)
        evidence = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.event_type.in_(("confirmed", "eligible", "structural_pool"))
        ))).all())
        frozen = {r.event_key: r.snapshot_json for r in evidence}
        for r in evidence:
            prior = json.loads(r.snapshot_json)["prior_structure"]
            if r.event_type in {"eligible", "confirmed"}:
                assert "eligibility" in prior
            if r.event_type == "confirmed":
                assert "confirmation_metrics" in prior
        # Missing orderbook remains unknown in this stricter read model, even
        # though the frozen evidence version historically allowed that leaf.
        for q in quotes:
            q["orderbook_imbalance"] = None
        await shadow.scan_strategy_iteration_shadow(db, quotes, at + timedelta(seconds=105))
        unknown = await shadow.build_first_board_current_pool(db, now=at + timedelta(seconds=105))
        assert unknown["confirmed_count"] == 0 and unknown["cumulative_confirmed_today"] == 2
        # Valid quote but no offer: downgrade to structure, not "confirmed today".
        for q in quotes:
            q["ask1_volume"] = 0
        await shadow.scan_strategy_iteration_shadow(db, quotes, at + timedelta(seconds=120))
        down = await shadow.build_first_board_current_pool(db, now=at + timedelta(seconds=120))
        assert down["structural_count"] == 2 and down["eligible_count"] == 0
        assert down["confirmed_count"] == 0 and down["cumulative_confirmed_today"] == 2
        sector.strength_score = 20
        await db.commit()
        await shadow.scan_strategy_iteration_shadow(db, quotes, at + timedelta(seconds=150))
        out = await shadow.build_first_board_current_pool(db, now=at + timedelta(seconds=150))
        assert out["structural_count"] == 0 and len(out["invalidated"]) == 2
        await shadow.scan_strategy_iteration_shadow(db, [], at + timedelta(seconds=180))
        bad = await shadow.build_first_board_current_pool(db, now=at + timedelta(seconds=180))
        assert not bad["valid"] and bad["coverage"]["quote_coverage"] == 0
        for r in evidence:
            await db.refresh(r)
            assert r.snapshot_json == frozen[r.event_key]


@pytest.mark.asyncio
@pytest.mark.parametrize("age,delay,cadence,reset_before", [
    (90, 65, 1, 70),      # reset is excluded by the original 66-row cap
    (600, 295, 30, 300),  # reset is excluded by the original 226-second window
])
async def test_loader_must_not_forget_reset_with_delayed_source(
        shadow_env, monkeypatch, age, delay, cadence, reset_before):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", age)
    at = datetime(2026, 9, 4, 10, 20)
    async with shadow_env() as db:
        for i, seconds in enumerate(range(-reset_before, 1, cadence)):
            when = at + timedelta(seconds=seconds)
            row = frame(when, passing=seconds != -reset_before)
            row.id = None
            row.event_key = f"delayed-history:{i}"
            row.code = "MARKET"
            row.name = "test"
            row.status = "complete"
            payload = json.loads(row.snapshot_json)
            payload["prior_structure"]["current_pool"]["members"][0]["source_quote_at"] = (
                when - timedelta(seconds=delay)).isoformat()
            row.snapshot_json = json.dumps(payload)
            db.add(row)
        await db.commit()
        result = await shadow.build_first_board_current_pool(db, now=at)
    assert result["confirmed_count"] == 0
    if cadence == 1:
        assert not result["valid"]
        assert result["history_complete"] is False


@pytest.mark.asyncio
async def test_complete_large_source_delay_window_can_confirm(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", 600)
    at = datetime(2026, 9, 4, 10, 20)
    async with shadow_env() as db:
        for i, seconds in enumerate(range(-600, 1, 30)):
            when = at + timedelta(seconds=seconds)
            row = frame(when)
            row.id = None
            row.event_key = f"complete-delayed-history:{i}"
            row.code, row.name, row.status = "MARKET", "test", "complete"
            payload = json.loads(row.snapshot_json)
            payload["prior_structure"]["current_pool"]["members"][0]["source_quote_at"] = (
                when - timedelta(seconds=500)).isoformat()
            row.snapshot_json = json.dumps(payload)
            db.add(row)
        await db.commit()
        result = await shadow.build_first_board_current_pool(db, now=at)
    assert result["valid"] and result["history_complete"]
    assert result["confirmed_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [66, 67])
async def test_history_cap_detects_actual_overflow_not_exact_full_page(shadow_env, count):
    at = datetime(2026, 9, 4, 10)
    async with shadow_env() as db:
        for i in range(count):
            row = frame(at + timedelta(seconds=i))
            row.id = None
            row.event_key = f"cap-boundary:{i}"
            row.code, row.name, row.status = "MARKET", "test", "complete"
            db.add(row)
        await db.commit()
        result = await shadow.build_first_board_current_pool(db, now=at + timedelta(seconds=count - 1))
    assert result["history_complete"] is (count == 66)
    assert result["valid"] is (count == 66)
    assert result["confirmed_count"] == int(count == 66)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["negative", "coverage", "invalid_round"])
async def test_loader_gap_cannot_hide_known_reset(shadow_env, monkeypatch, bad):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", 90)
    at = datetime(2026, 9, 4, 10)
    async with shadow_env() as db:
        if bad == "invalid_round":
            db.add(QuoteRound(
                round_id="gap-invalid", trade_date=at.date(),
                committed_at=at + timedelta(seconds=45), as_of_at=at,
                expected_count=2, received_count=1, quality_status="degraded",
                quality_reason="test", config_version="test", code_version="test",
            ))
        for seconds, source in [(0, 0), (90, 0), (120, 30), (150, 60), (180, 90), (210, 120)]:
            row = frame(at + timedelta(seconds=seconds),
                        passing=not (seconds == 0 and bad == "negative"),
                        blocked=seconds == 0 and bad == "coverage")
            row.id = None
            row.event_key = f"gap-reset:{seconds}"
            row.code, row.name, row.status = "MARKET", "test", "complete"
            payload = json.loads(row.snapshot_json)
            payload["prior_structure"]["current_pool"]["members"][0]["source_quote_at"] = (
                at + timedelta(seconds=source)).isoformat()
            row.snapshot_json = json.dumps(payload)
            db.add(row)
            await db.commit()
            if seconds >= 150:
                result = await shadow.build_first_board_current_pool(
                    db, now=at + timedelta(seconds=seconds))
                recovery = 210 if bad == "invalid_round" else 180
                assert result["history_complete"]
                assert result["confirmed_count"] == int(seconds >= recovery)


@pytest.mark.asyncio
async def test_long_persistence_loader_keeps_delayed_negative_boundary(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", 90)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC", 300)
    at = datetime(2026, 9, 4, 10)
    async with shadow_env() as db:
        for seconds in [0, *range(60, 421, 30)]:
            row = frame(at + timedelta(seconds=seconds), passing=seconds != 0)
            row.id = None
            row.event_key = f"long-reset:{seconds}"
            row.code, row.name, row.status = "MARKET", "test", "complete"
            payload = json.loads(row.snapshot_json)
            payload["prior_structure"]["current_pool"]["members"][0]["source_quote_at"] = (
                at + timedelta(seconds=seconds - 90 if seconds else 0)).isoformat()
            row.snapshot_json = json.dumps(payload)
            db.add(row)
            await db.commit()
            if seconds >= 390:
                result = await shadow.build_first_board_current_pool(
                    db, now=at + timedelta(seconds=seconds))
                assert result["history_complete"]
                assert result["confirmed_count"] == int(seconds == 420)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeline,recovery", [
    ([(0, 0, True), (30, -1, True), (120, 30, True),
      (150, 60, True), (180, 90, True), (210, 120, True)], 210),
    ([(0, -40, True), (30, -10, False), (60, 20, True),
      (90, 50, True), (120, 80, True), (150, 110, True)], 150),
    ([(0, -65, True), (30, -35, False), (60, -5, True),
      (90, 25, True), (120, 55, True), (150, 85, True), (180, 115, True)], 180),
    ([(0, 0, True), (30, 0, True), (60, 0, True),
      (90, 30, True), (120, 60, True)], 120),
    ([(0, 0, True), (30, 30, False), (60, 20, True), (90, 10, True),
      (120, 40, True), (150, 70, True), (180, 100, True)], 180),
])
async def test_loader_cross_gap_regression_and_delayed_recovery(
        shadow_env, monkeypatch, timeline, recovery):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", 90)
    at = datetime(2026, 9, 4, 10)
    for seconds, source, passing in timeline:
        async with shadow_env() as db:
            row = frame(at + timedelta(seconds=seconds), passing=passing)
            row.id = None
            row.event_key = f"source-regression:{seconds}"
            row.code, row.name, row.status = "MARKET", "test", "complete"
            payload = json.loads(row.snapshot_json)
            payload["prior_structure"]["current_pool"]["members"][0]["source_quote_at"] = (
                at + timedelta(seconds=source)).isoformat()
            row.snapshot_json = json.dumps(payload)
            db.add(row)
            await db.commit()
        async with shadow_env() as db:
            result = await shadow.build_first_board_current_pool(
                db, now=at + timedelta(seconds=seconds))
            assert result["history_complete"]
            assert result["confirmed_count"] == int(seconds >= recovery)
