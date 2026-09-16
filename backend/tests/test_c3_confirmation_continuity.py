"""C3 continuity only, frozen synthetic quotes and isolated SQLite."""
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select, func

from app.config.settings import settings
from app.models.paper import PaperShadowEvent, PaperShadowEvaluation, PaperTradeLog
from app.models.stock import StockKline
from app.paper import strategy_iteration_shadow as shadow
from test_strategy_iteration_shadow import (
    shadow_env, _first_board_quote, _seed_first_board_denominator, _freeze_shadow_today,
)

START = datetime(2026, 9, 4, 10)


@pytest.fixture(autouse=True)
def config(monkeypatch):
    _freeze_shadow_today(monkeypatch, START.date())
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_ACTIVATION_DATE", START.date().isoformat())


def quotes(at, kind=None, delay=0):
    rows = [
        {**_first_board_quote(code, confirms=True), "source_quote_at": (
            at - timedelta(seconds=delay)).isoformat(), "received_at": at.isoformat()}
        for code in ("600201", "600202")
    ]
    if kind == "negative":
        rows[0]["avg_price"] = 10.31
    elif kind == "missing_clock":
        rows[0].pop("source_quote_at")
    elif kind == "missing":
        rows.pop(0)
    elif kind == "empty":
        rows = []
    elif kind == "book":
        rows[0]["orderbook_imbalance"] = None
    return rows


async def scan(factory, seconds, kind=None, delay=0, start=START):
    at = start + timedelta(seconds=seconds)
    async with factory() as db:
        return await shadow.scan_strategy_iteration_shadow(db, quotes(at, kind, delay), at)


async def events(factory):
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 0
        return list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == shadow.ROUTE_C3, PaperShadowEvent.code == "600201",
        ).order_by(PaperShadowEvent.observed_at, PaperShadowEvent.id))).all())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["negative", "missing_clock", "missing", "empty", "book"])
async def test_c3_negative_unknown_resets_durably(shadow_env, kind):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    await scan(shadow_env, 0)
    await scan(shadow_env, 30, kind)
    for seconds in (60, 90):
        await scan(shadow_env, seconds)
    rows = await events(shadow_env)
    assert not any(e.event_type == "confirmed" for e in rows)
    assert any(e.event_type == "confirmation_reset" for e in rows)
    await scan(shadow_env, 120)
    rows = await events(shadow_env)
    confirmed = [e for e in rows if e.event_type == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].observed_at == START + timedelta(seconds=120)


@pytest.mark.asyncio
async def test_c3_duplicate_source_neither_first_hit_nor_current_pool(shadow_env):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    for seconds in (0, 30, 60):
        await scan(shadow_env, seconds, delay=seconds)
    rows = await events(shadow_env)
    assert not any(e.event_type == "confirmed" for e in rows)
    assert sum(e.event_type == "confirmation_sample" for e in rows) == 1
    async with shadow_env() as db:
        current = await shadow.build_first_board_current_pool(db, now=START + timedelta(seconds=60))
    assert current["confirmed_count"] == 0


@pytest.mark.asyncio
async def test_c3_constant_delay_can_recover(shadow_env):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    await scan(shadow_env, 0, delay=40)
    await scan(shadow_env, 30, "negative", delay=40)
    for seconds in (60, 90, 120):
        await scan(shadow_env, seconds, delay=40)
    assert not any(e.event_type == "confirmed" for e in await events(shadow_env))
    async with shadow_env() as db:
        current = await shadow.build_first_board_current_pool(db, now=START + timedelta(seconds=120))
        assert not next(m for m in current["members"] if m["code"] == "600201")["currently_confirmed"]
    await scan(shadow_env, 150, delay=40)
    rows = await events(shadow_env)
    assert sum(e.event_type == "confirmation_reset" for e in rows) == 1
    confirmed = [e for e in rows if e.event_type == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].observed_at == START + timedelta(seconds=150)


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [datetime(2026, 9, 4, 9, 25), datetime(2026, 9, 4, 11, 31),
                                   datetime(2026, 9, 4, 14, 31)])
async def test_c3_never_first_hits_outside_confirmation_window(shadow_env, start):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    for seconds in (0, 30, 60):
        await scan(shadow_env, seconds, start=start)
    assert not any(e.event_type == "confirmed" for e in await events(shadow_env))


def test_c3_explicit_version_rotates_without_generic_change(monkeypatch):
    generic = {r: shadow.route_version_for(r) for r in
               (shadow.ROUTE_B, shadow.ROUTE_C, shadow.ROUTE_D, shadow.ROUTE_F2)}
    version = shadow.route_version_for(shadow.ROUTE_C3)
    assert version != settings.PAPER_FIRST_BOARD_SHADOW_VERSION
    monkeypatch.setattr(settings, "PAPER_FIRST_BOARD_SHADOW_MIN_RELATIVE_STRENGTH_PCT",
                        settings.PAPER_FIRST_BOARD_SHADOW_MIN_RELATIVE_STRENGTH_PCT + 0.1)
    assert version != shadow.route_version_for(shadow.ROUTE_C3)
    assert generic == {r: shadow.route_version_for(r) for r in generic}


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [datetime(2026, 9, 4, 9, 30), datetime(2026, 9, 4, 13)])
async def test_c3_delayed_pre_session_source_is_not_confirmation(shadow_env, start):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    for seconds in (0, 30, 60):
        await scan(shadow_env, seconds, delay=40, start=start)
    assert not any(e.event_type == "confirmed" for e in await events(shadow_env))
    async with shadow_env() as db:
        current = await shadow.build_first_board_current_pool(db, now=start + timedelta(seconds=60))
        assert current["confirmed_count"] == 0
    for seconds in (90, 120, 150):
        await scan(shadow_env, seconds, delay=40, start=start)
    confirmed = [e for e in await events(shadow_env) if e.event_type == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].observed_at == start + timedelta(seconds=150)


def event(kind, at=START, code="600201", version=None):
    item = shadow._event(route_id=shadow.ROUTE_C3, trade_date=at.date(), observed_at=at,
                         code=code, name=code, event_type=kind, status=kind,
                         quote=quotes(at)[0], prior={})
    if version:
        item["event_key"] = item["event_key"].replace(item["route_version"], version)
        item["route_version"] = version
        payload = json.loads(item["snapshot_json"])
        payload["route_version"] = version
        item["snapshot_json"] = json.dumps(payload)
    return PaperShadowEvent(**item)


@pytest.mark.asyncio
@pytest.mark.parametrize("ready_same_version", [True, False])
async def test_legacy_settlement_requires_own_ready_without_rewriting_history(
        shadow_env, ready_same_version):
    old = settings.PAPER_FIRST_BOARD_SHADOW_VERSION
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
        confirmed = event("confirmed", version=old)
        db.add_all([confirmed, event("structural_pool", version=old),
                    event("eligible", version=old),
                    event("session_ready", code="MARKET",
                          version=old if ready_same_version else None)])
        db.add_all([
            StockKline(code="600201", trade_date=START.date(), open=10, high=10.4, low=10,
                       close=10.3, prev_close=10, change_pct=3, source="ths"),
            StockKline(code="600201", trade_date=date(2026, 9, 7), open=10.3, high=10.5, low=10.2,
                       close=10.4, prev_close=10.3, change_pct=0.97, source="ths"),
        ])
        await db.commit()
        frozen = confirmed.snapshot_json
        finalized = await shadow.finalize_first_board_shadow_sessions(
            db, now=datetime(2026, 9, 7, 21), as_of_date=date(2026, 9, 7))
        assert finalized["sessions"] == 0  # no new-version historical reclassification
        result = await shadow.settle_strategy_iteration_shadow(db, date(2026, 9, 7))
        assert result["signals"] == int(ready_same_version)
        evaluations = (await db.scalars(select(PaperShadowEvaluation))).all()
        assert len(evaluations) == int(ready_same_version)
        if evaluations:
            assert evaluations[0].route_version == old
        await db.refresh(confirmed)
        assert confirmed.snapshot_json == frozen and confirmed.route_version == old


@pytest.mark.asyncio
async def test_old_first_hit_not_current_and_not_new_confirmation(shadow_env):
    old = settings.PAPER_FIRST_BOARD_SHADOW_VERSION
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
        db.add_all([event("confirmed", version=old), event("confirmation_sample", version=old)])
        await db.commit()
    for seconds in (30, 60):
        await scan(shadow_env, seconds)
    async with shadow_env() as db:
        current = await shadow.build_first_board_current_pool(db, now=START + timedelta(seconds=60))
        assert current["confirmed_count"] == current["cumulative_confirmed_today"] == 0
    await scan(shadow_env, 90)
    rows = await events(shadow_env)
    confirmed = [e for e in rows if e.event_type == "confirmed"]
    assert len(confirmed) == 2
    assert {e.route_version for e in confirmed} == {old, shadow.route_version_for(shadow.ROUTE_C3)}


@pytest.mark.asyncio
async def test_lunch_gap_new_afternoon_streak(shadow_env):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    start = datetime(2026, 9, 4, 11, 29, 30)
    for seconds in (0, 30):
        await scan(shadow_env, seconds, start=start)
    afternoon = datetime(2026, 9, 4, 13)
    for seconds in (0, 30):
        await scan(shadow_env, seconds, start=afternoon)
    assert not any(e.event_type == "confirmed" for e in await events(shadow_env))
    await scan(shadow_env, 60, start=afternoon)
    confirmed = [e for e in await events(shadow_env) if e.event_type == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].observed_at == afternoon + timedelta(seconds=60)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_age", [90, 600])
async def test_reset_before_c3_window_cannot_admit_delayed_pre_reset_source(shadow_env, monkeypatch, max_age):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", max_age)
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    await scan(shadow_env, 0)
    await scan(shadow_env, 30, "negative")
    old_reset = next(e for e in await events(shadow_env) if e.event_type == "confirmation_reset")
    frozen_reset = old_reset.snapshot_json
    window = max_age + max(settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC,
                 settings.PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES
                 * settings.PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC
                 ) + settings.PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC + 1
    later = window + 60  # reset at30 is now outside the restoration SQL window
    original = shadow._json_dict
    def bounded(value):
        assert value != frozen_reset, "window-excluded reset must not be JSON-decoded"
        return original(value)
    monkeypatch.setattr(shadow, "_json_dict", bounded)
    # Source=20 precedes the dropped reset=30. Since window > allowed age,
    # forgetting the reset cannot make this old positive source fresh again.
    await scan(shadow_env, later, delay=later - 20)
    rows = await events(shadow_env)
    assert not any(e.event_type == "confirmation_sample"
                   and e.observed_at == START + timedelta(seconds=later) for e in rows)
    assert not any(e.event_type == "confirmed" for e in rows)
    for seconds in (later + 30, later + 60, later + 90):
        await scan(shadow_env, seconds)
    confirmed = [e for e in await events(shadow_env) if e.event_type == "confirmed"]
    assert len(confirmed) == 1 and confirmed[0].observed_at == START + timedelta(seconds=later + 90)


@pytest.mark.asyncio
async def test_bounded_c3_restore_does_not_decode_old_sample_json(shadow_env, monkeypatch):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
        sample = event("confirmation_sample", at=START - timedelta(minutes=20))
        sample.snapshot_json = '{"old_unbounded_json": true}'
        db.add(sample)
        await db.commit()
    original = shadow._json_dict
    def bounded(value):
        assert value != '{"old_unbounded_json": true}', "old C3 JSON must be excluded in SQL"
        return original(value)
    monkeypatch.setattr(shadow, "_json_dict", bounded)
    await scan(shadow_env, 0)
    assert not any(e.event_type == "confirmed" for e in await events(shadow_env))


@pytest.mark.asyncio
async def test_c3_subsecond_negative_is_not_lost_from_current_pool(shadow_env):
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    await scan(shadow_env, 0)
    await scan(shadow_env, 30.1)
    await scan(shadow_env, 30.2, "negative")
    for seconds in (60, 90):
        await scan(shadow_env, seconds)
    assert not any(e.event_type == "confirmed" for e in await events(shadow_env))
    async with shadow_env() as db:
        current = await shadow.build_first_board_current_pool(db, now=START + timedelta(seconds=90))
        assert not next(m for m in current["members"] if m["code"] == "600201")["currently_confirmed"]


@pytest.mark.asyncio
async def test_long_persistence_source_span_survives_restoration_window(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", 90)
    monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC", 300)
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    for seconds in range(0, 391, 30):
        await scan(shadow_env, seconds, delay=seconds * 90 / 390)
        confirmed = [e for e in await events(shadow_env) if e.event_type == "confirmed"]
        assert len(confirmed) == int(seconds == 390)
    assert confirmed[0].observed_at == START + timedelta(seconds=390)


@pytest.mark.asyncio
async def test_cross_gap_source_regression_first_hit_and_current_agree(shadow_env, monkeypatch):
    monkeypatch.setattr(settings, "ANOMALY_QUOTE_MAX_AGE_SEC", 90)
    async with shadow_env() as db:
        await _seed_first_board_denominator(db)
    for seconds, source in [(0, 0), (30, -1), (120, 30), (150, 60), (180, 90), (210, 120)]:
        await scan(shadow_env, seconds, delay=seconds - source)
        confirmed = [e for e in await events(shadow_env) if e.event_type == "confirmed"]
        assert len(confirmed) == int(seconds == 210)
        async with shadow_env() as db:
            current = await shadow.build_first_board_current_pool(
                db, now=START + timedelta(seconds=seconds))
            member = next(m for m in current["members"] if m["code"] == "600201")
            assert member["currently_confirmed"] is (seconds == 210)
    resets = [e for e in await events(shadow_env) if e.event_type == "confirmation_reset"]
    assert len(resets) == 1 and resets[0].observed_at == START + timedelta(seconds=30)
