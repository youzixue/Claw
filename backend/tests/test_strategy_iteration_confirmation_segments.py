"""Synthetic forward frames only; all persistence is isolated SQLite."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings, PaperRouteSignalPolicy
from app.models.paper import PaperShadowEvent, PaperTradeLog
from app.paper.strategy_iteration_shadow import (
    ROUTE_B, ROUTE_C, ROUTE_C3, ROUTE_D, ROUTE_F2, route_version_for,
    scan_strategy_iteration_shadow,
)
from test_strategy_iteration_shadow import shadow_env, _seed_structures, _quote, _freeze_shadow_today

START = datetime(2026, 9, 1, 9, 40)
ROUTES = [(ROUTE_B, "600001"), (ROUTE_C, "600002"),
          (ROUTE_F2, "600003"), (ROUTE_D, "600004")]


@pytest.fixture(autouse=True)
def route_policy(monkeypatch):
    # Isolate continuity, not alpha: keep original count/duration/gap and risk.
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {
        name: PaperRouteSignalPolicy(min_relative_strength_pct=0.0)
        for name in ("challenger_b", "challenger_c", "challenger_d", "challenger_f2")
    })


def frame(seconds, *, bad_code=None, kind=None, source_seconds=None):
    now = START + timedelta(seconds=seconds)
    rows = []
    for code in ("600001", "600002", "600003", "600004"):
        q = {**_quote(code), "source_quote_at": (
            START + timedelta(seconds=seconds if source_seconds is None else source_seconds)
        ).isoformat(), "received_at": now.isoformat()}
        if code == bad_code:
            if kind == "negative":
                q["price"] = 9.99
            elif kind == "missing_vwap":
                q["avg_price"] = None
            elif kind == "missing_clock":
                q.pop("source_quote_at")
            elif kind == "missing_quote":
                continue
        rows.append(q)
    return [] if kind == "empty" else rows


async def scan_restarted(factory, seconds, **kwargs):
    # A new session per frame forces reconstruction from committed events.
    async with factory() as db:
        return await scan_strategy_iteration_shadow(
            db, frame(seconds, **kwargs), START + timedelta(seconds=seconds)
        )


async def events_for(factory, route, code):
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 0
        return list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == route, PaperShadowEvent.code == code,
        ).order_by(PaperShadowEvent.observed_at, PaperShadowEvent.id))).all())


@pytest.mark.asyncio
@pytest.mark.parametrize("route,code", ROUTES)
@pytest.mark.parametrize("kind", ["negative", "missing_vwap", "missing_clock", "missing_quote", "empty"])
async def test_intervening_failure_is_durable_segment_boundary(shadow_env, route, code, kind):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0)
    await scan_restarted(shadow_env, 30, bad_code=code, kind=kind)
    await scan_restarted(shadow_env, 60)
    await scan_restarted(shadow_env, 90)
    rows = await events_for(shadow_env, route, code)
    assert not any(e.event_type == "confirmed" for e in rows)
    resets = [e for e in rows if e.event_type == "confirmation_reset"]
    assert resets and resets[0].observed_at == START + timedelta(seconds=30)
    audit = json.loads(resets[0].snapshot_json)
    assert audit["prior_structure"]["reason"]
    if kind == "negative":
        assert audit["quote"]["price"] == 9.99
    await scan_restarted(shadow_env, 120)
    rows = await events_for(shadow_env, route, code)
    confirmed = [e for e in rows if e.event_type == "confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0].observed_at == START + timedelta(seconds=120)
    status = json.loads(confirmed[0].snapshot_json)["prior_structure"]["confirmation"]
    assert status["sample_count"] == 3
    assert status["persistence_sec"] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("route,code", ROUTES)
async def test_duplicate_source_does_not_age_or_count(shadow_env, route, code):
    async with shadow_env() as db:
        await _seed_structures(db)
    for seconds in (0, 30, 60):
        await scan_restarted(shadow_env, seconds, source_seconds=0)
    rows = await events_for(shadow_env, route, code)
    assert not any(e.event_type == "confirmed" for e in rows)
    assert sum(e.event_type == "confirmation_sample" for e in rows) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("route,code", ROUTES)
async def test_long_gap_starts_new_streak(shadow_env, route, code):
    async with shadow_env() as db:
        await _seed_structures(db)
    for seconds in (0, 30, 120, 150):
        await scan_restarted(shadow_env, seconds)
    assert not any(e.event_type == "confirmed" for e in await events_for(shadow_env, route, code))
    await scan_restarted(shadow_env, 180)
    rows = await events_for(shadow_env, route, code)
    confirmed = [e for e in rows if e.event_type == "confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0].observed_at == START + timedelta(seconds=180)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_seconds", [None, 31, -500])
async def test_invalid_source_clock_cannot_extend_existing_streak(shadow_env, source_seconds):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0)
    rows = frame(30, source_seconds=source_seconds)
    if source_seconds is None:
        for row in rows:
            row["source_quote_at"] = "not-a-clock"
    async with shadow_env() as db:
        await scan_strategy_iteration_shadow(db, rows, START + timedelta(seconds=30))
    for seconds in (60, 90):
        await scan_restarted(shadow_env, seconds)
    rows = await events_for(shadow_env, ROUTE_B, "600001")
    assert not any(e.event_type == "confirmed" for e in rows)
    assert any(e.event_type == "confirmation_reset" for e in rows)


@pytest.mark.asyncio
async def test_source_duration_cannot_be_replaced_by_receipt_duration(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
    for seconds, source in ((0, 0), (30, 10), (60, 20)):
        await scan_restarted(shadow_env, seconds, source_seconds=source)
    rows = await events_for(shadow_env, ROUTE_B, "600001")
    assert not any(e.event_type == "confirmed" for e in rows)
    latest = [e for e in rows if e.event_type == "confirmation_sample"][-1]
    status = json.loads(latest.snapshot_json)["prior_structure"]["confirmation"]
    assert status["sample_count"] == 3 and status["persistence_sec"] == 60
    assert status["source_persistence_sec"] == 20 and not status["ready"]


@pytest.mark.asyncio
async def test_delayed_pre_reset_source_does_not_reopen_segment(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0)
    await scan_restarted(shadow_env, 30, bad_code="600001", kind="negative")
    await scan_restarted(shadow_env, 60, source_seconds=20)
    for seconds in (90, 120):
        await scan_restarted(shadow_env, seconds)
    rows = await events_for(shadow_env, ROUTE_B, "600001")
    assert not any(e.event_type == "confirmed" for e in rows)
    assert not any(e.event_type == "confirmation_sample"
                   and e.observed_at == START + timedelta(seconds=60) for e in rows)
    await scan_restarted(shadow_env, 150)
    assert any(e.event_type == "confirmed" for e in
               await events_for(shadow_env, ROUTE_B, "600001"))


@pytest.mark.asyncio
async def test_subsecond_negative_reset_and_exact_replay_are_durable(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0)
    for seconds in (30.1, 30.2):
        await scan_restarted(shadow_env, seconds, bad_code="600001", kind="negative")
    replay = await scan_restarted(shadow_env, 30.2, bad_code="600001", kind="negative")
    assert replay["events"] == 0
    rows = await events_for(shadow_env, ROUTE_B, "600001")
    resets = [e for e in rows if e.event_type == "confirmation_reset"]
    assert len(resets) == 2 and len({e.event_key for e in resets}) == 2
    for seconds in (60, 90):
        await scan_restarted(shadow_env, seconds)
    assert not any(e.event_type == "confirmed" for e in
                   await events_for(shadow_env, ROUTE_B, "600001"))


@pytest.mark.asyncio
async def test_old_evidence_version_is_not_consumed(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0)
    async with shadow_env() as db:
        samples = (await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.event_type == "confirmation_sample"))).all()
        for event in samples:
            event.route_version = "legacy-positive-only"
        await db.commit()
    for seconds in (30, 60):
        await scan_restarted(shadow_env, seconds)
    assert not any(e.event_type == "confirmed" for e in
                   await events_for(shadow_env, ROUTE_B, "600001"))
    await scan_restarted(shadow_env, 90)
    assert any(e.event_type == "confirmed" for e in
               await events_for(shadow_env, ROUTE_B, "600001"))
    assert route_version_for(ROUTE_C3) != str(settings.PAPER_FIRST_BOARD_SHADOW_VERSION)


@pytest.mark.asyncio
async def test_source_gap_alone_resets_even_with_regular_receipts(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
    for seconds, source in ((0, -60), (30, -30), (60, 60), (90, 90)):
        await scan_restarted(shadow_env, seconds, source_seconds=source)
    rows = await events_for(shadow_env, ROUTE_B, "600001")
    assert not any(e.event_type == "confirmed" for e in rows)
    assert any(e.event_type == "confirmation_reset" and
               json.loads(e.snapshot_json)["prior_structure"]["reason"] == "observation_or_source_gap"
               for e in rows)
    await scan_restarted(shadow_env, 120)
    assert any(e.event_type == "confirmed" for e in
               await events_for(shadow_env, ROUTE_B, "600001"))


@pytest.mark.asyncio
async def test_stronger_policy_is_not_lowered_by_segment_repair(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_ACCOUNT_ROUTE_SIGNAL_POLICIES", {
        name: PaperRouteSignalPolicy(min_samples=4, min_persistence_sec=90,
                                    min_relative_strength_pct=0.0)
        for name in ("challenger_b", "challenger_c", "challenger_d", "challenger_f2")
    })
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0)
    await scan_restarted(shadow_env, 30, bad_code="600001", kind="negative")
    for seconds in (60, 90, 120):
        await scan_restarted(shadow_env, seconds)
    assert not any(e.event_type == "confirmed" for e in
                   await events_for(shadow_env, ROUTE_B, "600001"))
    await scan_restarted(shadow_env, 150)
    confirmed = [e for e in await events_for(shadow_env, ROUTE_B, "600001")
                 if e.event_type == "confirmed"]
    assert len(confirmed) == 1
    status = json.loads(confirmed[0].snapshot_json)["prior_structure"]["confirmation"]
    assert status["sample_count"] == 4 and status["persistence_sec"] == 90


@pytest.mark.asyncio
async def test_recreated_engine_restores_reset_from_sqlite(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0)
    await scan_restarted(shadow_env, 30, bad_code="600001", kind="negative")
    old_engine = shadow_env.kw["bind"]
    url = old_engine.url
    await old_engine.dispose()
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for seconds in (60, 90):
            await scan_restarted(factory, seconds)
        assert not any(e.event_type == "confirmed" for e in
                       await events_for(factory, ROUTE_B, "600001"))
        await scan_restarted(factory, 120)
        assert any(e.event_type == "confirmed" for e in
                   await events_for(factory, ROUTE_B, "600001"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_c3_empty_frame_audit_survives_without_history(shadow_env, monkeypatch):
    _freeze_shadow_today(monkeypatch, START.date())
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE", START.date().isoformat())
    async with shadow_env() as db:
        result = await scan_strategy_iteration_shadow(db, [], START)
    rows = await events_for(shadow_env, ROUTE_C3, "MARKET")
    assert result["confirmed"] == 0
    assert len(rows) == 1 and rows[0].event_type == "coverage_blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("route,code", ROUTES)
@pytest.mark.parametrize("kind", ["negative", "missing_clock"])
async def test_fixed_source_delay_recovers_without_advancing_empty_boundary(
        shadow_env, route, code, kind):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0, source_seconds=-40)
    await scan_restarted(shadow_env, 30, source_seconds=-10, bad_code=code, kind=kind)
    # Every new frame is fresh under the original 90s age limit, but source
    # time lags receipt by 40s: do not implicitly demand latency < poll cadence.
    for seconds in (60, 90, 120):
        await scan_restarted(shadow_env, seconds, source_seconds=seconds - 40)
    rows = await events_for(shadow_env, route, code)
    resets = [e for e in rows if e.event_type == "confirmation_reset"]
    assert len(resets) == 1
    assert json.loads(resets[0].snapshot_json)["prior_structure"]["source_boundary_at"] == (
        START + timedelta(seconds=30)).isoformat()
    samples = [e.observed_at for e in rows if e.event_type == "confirmation_sample"]
    assert samples == [START, START + timedelta(seconds=90), START + timedelta(seconds=120)]
    assert not any(e.event_type == "confirmed" for e in rows)
    await scan_restarted(shadow_env, 150, source_seconds=110)
    rows = await events_for(shadow_env, route, code)
    confirmed = [e for e in rows if e.event_type == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].observed_at == START + timedelta(seconds=150)
    status = json.loads(confirmed[0].snapshot_json)["prior_structure"]["confirmation"]
    assert status["sample_count"] == status["source_sample_count"] == 3
    assert status["persistence_sec"] == status["source_persistence_sec"] == 60
    assert status["max_sample_gap_sec"] == 75


@pytest.mark.asyncio
@pytest.mark.parametrize("route,code", ROUTES)
async def test_active_source_regression_still_resets_then_recovers_with_delay(shadow_env, route, code):
    async with shadow_env() as db:
        await _seed_structures(db)
    await scan_restarted(shadow_env, 0, source_seconds=-40)
    await scan_restarted(shadow_env, 30, source_seconds=-10)
    await scan_restarted(shadow_env, 60, source_seconds=-20)
    # A true regression broke the active segment at observed=60. The delayed
    # source=50 frame must be ignored without shifting that boundary to 90.
    await scan_restarted(shadow_env, 90, source_seconds=50)
    rows = await events_for(shadow_env, route, code)
    resets = [e for e in rows if e.event_type == "confirmation_reset"]
    assert len(resets) == 1
    audit = json.loads(resets[0].snapshot_json)["prior_structure"]
    assert audit["reason"] == "source_clock_regressed"
    assert audit["source_boundary_at"] == (START + timedelta(seconds=60)).isoformat()
    for seconds in (120, 150):
        await scan_restarted(shadow_env, seconds, source_seconds=seconds - 40)
    assert not any(e.event_type == "confirmed" for e in await events_for(shadow_env, route, code))
    await scan_restarted(shadow_env, 180, source_seconds=140)
    rows = await events_for(shadow_env, route, code)
    confirmed = [e for e in rows if e.event_type == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].observed_at == START + timedelta(seconds=180)
