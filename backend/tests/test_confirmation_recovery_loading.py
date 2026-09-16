"""Read-shape optimization must preserve full-day generic state semantics."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import event
from app.models.paper import PaperShadowEvent
from app.paper import strategy_iteration_shadow as shadow
from test_strategy_iteration_shadow import shadow_env, _seed_structures
from test_strategy_iteration_confirmation_segments import START, frame, route_policy, events_for, ROUTES


@pytest.mark.asyncio
async def test_restore_pairs_route_version_in_sql_and_projects_only_leaves(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
        for route, code in ROUTES:
            q = next(q for q in frame(0) if q["code"] == code)
            item = shadow._event(route_id=route, code=code, name=code, trade_date=START.date(),
                observed_at=START, event_type="confirmation_sample", status="waiting", quote=q, prior={})
            # Both an obsolete version and a current version belonging to the
            # wrong route must be excluded by SQL, not after ORM materialization.
            wrong = shadow.route_version_for(shadow.ROUTE_C if route != shadow.ROUTE_C else shadow.ROUTE_B)
            for version in ("obsolete-sql-sentinel", wrong):
                db.add(PaperShadowEvent(**{**item, "route_version": version,
                    "event_key": item["event_key"] + ":" + version,
                    "snapshot_json": '{"unrelated_old_version_sentinel":true}'}))
        await db.commit()
    captured = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if ("FROM paper_shadow_event" in statement and
                "ORDER BY paper_shadow_event.observed_at, paper_shadow_event.id" in statement):
            captured.append((statement, parameters))
    engine = shadow_env.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        async with shadow_env() as db:
            await shadow.scan_strategy_iteration_shadow(db, frame(30), START + timedelta(seconds=30))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(captured) == 1
    sql, params = captured[0]
    for route, _ in ROUTES:
        version = shadow.route_version_for(route)
        assert any(params[i:i+2] == (route, version) for i in range(len(params)-1))
    projection = sql.split("FROM paper_shadow_event", 1)[0]
    for unused in ("event_key", "name", "status", "assumed_fill_price", "created_at", "trade_date"):
        assert "paper_shadow_event." + unused not in projection
    assert "paper_shadow_event.snapshot_json" in projection
    for route, code in ROUTES:
        samples = [r for r in await events_for(shadow_env, route, code)
                   if r.route_version == shadow.route_version_for(route)
                   and r.event_type == "confirmation_sample"]
        assert len(samples) == 1
        assert json.loads(samples[0].snapshot_json)["prior_structure"]["confirmation"]["sample_count"] == 1


@pytest.mark.asyncio
async def test_long_vwap_segment_keeps_first_anchor_not_last_three_or_time_window(shadow_env):
    async with shadow_env() as db:
        await _seed_structures(db)
    for seconds in range(0, 421, 30):
        rows = frame(seconds)
        rows[0]["avg_price"] = 10.02 if seconds in (0, 420) else (10.01 if seconds == 390 else 10.00)
        async with shadow_env() as db:
            await shadow.scan_strategy_iteration_shadow(db, rows, START + timedelta(seconds=seconds))
        confirmed = [e for e in await events_for(shadow_env, shadow.ROUTE_B, "600001")
                     if e.event_type == "confirmed"]
        assert len(confirmed) == int(seconds == 420)
    status = json.loads(confirmed[0].snapshot_json)["prior_structure"]["confirmation"]
    assert status["sample_count"] == 15
    assert status["persistence_sec"] == 420
    assert status["first_sample_at"] == START.isoformat()
    assert status["vwap_slope_pct"] == 0
