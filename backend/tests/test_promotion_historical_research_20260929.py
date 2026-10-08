"""Explicit historical direction views: isolated fixtures, never training or fallback."""
import copy
import json
import socket
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import model_lab
from app.models.promotion import PromotionPredictionRun as Run, PromotionPredictionSnapshot as Snapshot
from app.models.stock import StockSpot
from app.promotion.direction_research import annotate_direction_research, project_frozen_direction_research
from test_model_lab_api import model_lab_env

DAY = date(2026, 8, 28)
CLOCK = datetime(2026, 8, 28, 20)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("historical research must not access network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def frozen_rows(count=12):
    candidates = [
        {"code": f"60{i:04d}", "name": f"fixture-{i}", "target_board": 1,
         "direction_probability": .5 + i*.01,
         "probability_factors": {"prediction_rank_eligible": True,
                                "learning_direction_probability_method": "fixture_frozen"}}
        for i in range(count)
    ]
    annotated, _ = annotate_direction_research(candidates)
    return [{"code": item["code"], "name": item["name"],
             "factors": item["probability_factors"], "limit_up_probability": .01}
            for item in annotated]


async def seed_run(db, *, key="old", rows=None, run_changes=None, snapshot_changes=None):
    rows = frozen_rows() if rows is None else rows
    values = dict(run_key=key, snapshot_batch_key=f"schedule:promotion_2000:{CLOCK.isoformat()}",
        reference_trade_date=DAY, as_of_at=CLOCK, snapshot_source="schedule",
        snapshot_context="promotion_2000", model_version="test", feature_version="test",
        data_version="test", runtime_mode="legacy", status="completed", gate_passed=True,
        candidate_count=len(rows), ranked_count=len(rows), actionable_count=0,
        payload_hash="f"*64, created_at=CLOCK+timedelta(seconds=1),
        completed_at=CLOCK+timedelta(seconds=3))
    values.update(run_changes or {})
    run = Run(**values)
    db.add(run)
    await db.flush()
    for index, row in enumerate(rows):
        values = dict(run_id=run.id, record_key=f"{key}-{index}", code=row["code"], name=row.get("name"),
            target_board=1, prediction_trade_date=DAY, horizon_days=1,
            candidate_route="fixture", calibrated_probability=.01,
            features_json=json.dumps(row["factors"]), created_at=CLOCK+timedelta(seconds=2))
        if index == 0:
            values.update(snapshot_changes or {})
        db.add(Snapshot(**values))
    await db.commit()
    return run.id


def test_projection_uses_original_ranks_and_leaves_input_unchanged():
    rows = frozen_rows()
    before = copy.deepcopy(rows)
    result = project_frozen_direction_research(list(reversed(rows)), batch_valid=True)
    assert rows == before
    assert result["status"] == "available"
    assert result["manual_review_eligible"] is False
    assert result["production_unchanged"] is True
    assert result["frozen"] is True
    assert result["missing_recordable_count"] is None
    assert [item["code"] for item in result["candidates"]] == [row["code"] for row in reversed(rows)]
    assert all(item["direction_probability"] != item["limit_up_probability"] for item in result["candidates"])
    assert "target_met" not in result and "certified" not in result


@pytest.mark.parametrize("rows", [None, {}, "", [None], [1], ["600001"], [{"code": []}], []])
def test_bad_row_shapes_fail_closed(rows):
    result = project_frozen_direction_research(rows, batch_valid=True)
    assert result["status"] == "blocked"
    assert result["candidates"] == []


@pytest.mark.parametrize("valid", [False, None, 1, "true", [], {}])
def test_batch_valid_requires_explicit_boolean(valid):
    assert project_frozen_direction_research(frozen_rows(), batch_valid=valid)["status"] == "blocked"


@pytest.mark.parametrize("code", ["", "60001", " 600001", "６００００１", "ABCDEF", 600001, True, None])
def test_bad_codes_block_whole_batch(code):
    rows = frozen_rows()
    rows[0]["code"] = code
    assert project_frozen_direction_research(rows, batch_valid=True)["status"] == "blocked"


@pytest.mark.parametrize("field,value", [
    ("candidate_count", 99), ("candidate_count", True), ("eligible_count", 0),
    ("selected_count", 1), ("rank_limit", 30), ("rank_position", True),
    ("rank_position", 99), ("eligible", 1), ("selected", 1), ("probability", True),
    ("probability", float("nan")), ("probability", float("inf")), ("probability", 10**400),
    ("probability", ".9"), ("rank_contract_complete", 1),
    ("error", "known_failure"), ("error", {"malicious": True}),
    ("probability_method", []), ("missing_recordable_count", 1),
    ("missing_recordable_count", True), ("missing_recordable_count", -1),
    ("missing_recordable_count", []),
])
def test_malicious_or_conflicting_proofs_never_display_candidates(field, value):
    rows = frozen_rows()
    rows[0]["factors"]["direction_research"][field] = value
    result = project_frozen_direction_research(rows, batch_valid=True)
    assert result["status"] == "blocked"
    assert result["candidates"] == []


@pytest.mark.parametrize("mutation", ["duplicate", "truncated", "missing_error", "missing_eligible",
                                     "rank_reversal", "eligibility_conflict"])
def test_incomplete_universe_never_rebuilt_or_reranked(mutation):
    rows = frozen_rows()
    if mutation == "duplicate":
        rows[1] = copy.deepcopy(rows[0])
    elif mutation == "truncated":
        rows.pop()
    elif mutation == "rank_reversal":
        for row in rows:
            row["factors"]["direction_research"]["rank_position"] = 13-row["factors"]["direction_research"]["rank_position"]
    elif mutation == "eligibility_conflict":
        rows[0]["factors"]["prediction_rank_eligible"] = False
    else:
        rows[0]["factors"]["direction_research"].pop(mutation.removeprefix("missing_"))
    result = project_frozen_direction_research(rows, batch_valid=True)
    assert result["status"] == "blocked"
    assert result["candidates"] == []


@pytest.mark.parametrize("field", ["version", "label_version", "scope"])
def test_unsupported_proof_is_unavailable(field):
    rows = frozen_rows()
    rows[0]["factors"]["direction_research"][field] = "unsupported"
    assert project_frozen_direction_research(rows, batch_valid=True)["status"] == "unavailable"


def test_optional_zero_recordability_and_missing_contract_are_distinct():
    rows = frozen_rows(2)
    for row in rows:
        row["factors"]["direction_research"]["missing_recordable_count"] = 0
    result = project_frozen_direction_research(rows, batch_valid=True)
    assert result["status"] == "insufficient_candidates"
    assert result["missing_recordable_count"] == 0
    rows[0]["factors"].pop("direction_research")
    assert project_frozen_direction_research(rows, batch_valid=True)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_query_only_mode_ro_no_autoflush_no_initializer_and_no_identity_map_taint(model_lab_env, monkeypatch):
    Session, _ = model_lab_env
    async with Session() as db:
        run_id = await seed_run(db)
        path = db.bind.url.database
    ro_engine = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")
    @event.listens_for(ro_engine.sync_engine, "connect")
    def readonly(connection, record):
        connection.execute("PRAGMA query_only=ON")
    statements = []
    @event.listens_for(ro_engine.sync_engine, "before_cursor_execute")
    def observe(conn, cursor, statement, params, context, many):
        statements.append(statement)
    async def forbidden(*args, **kwargs):
        raise AssertionError("read-only branch called storage initializer")
    monkeypatch.setattr(model_lab, "ensure_prediction_ledger_storage", forbidden)
    try:
        async with AsyncSession(ro_engine, expire_on_commit=False) as db:
            stored = await db.get(Run, run_id)
            stored.status = "blocked"  # uncommitted identity map must not replace frozen DB facts
            db.add(StockSpot(code="600999", name="must never flush"))
            listed = await model_lab.prediction_runs(db=db, compact=True)
            detail = await model_lab.prediction_run_detail(run_id, db=db, direction_only=True, include_features=True)
            assert listed["count"] == 1
            assert detail["run"]["status"] == "completed"
            assert detail["direction_research"]["status"] == "available"
            assert db.new and db.dirty
            assert not any(s.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}
                           for s in statements)
            assert await db.scalar(text("PRAGMA query_only")) == 1
            await db.rollback()
    finally:
        await ro_engine.dispose()


@pytest.mark.asyncio
async def test_explicit_failed_or_missing_batch_does_not_fallback_to_available(model_lab_env):
    Session, client = model_lab_env
    async with Session() as db:
        old = await seed_run(db)
        newer = await seed_run(db, key="new", run_changes={
            "status": "blocked", "as_of_at": CLOCK+timedelta(minutes=1),
            "created_at": CLOCK+timedelta(minutes=1, seconds=1),
            "completed_at": CLOCK+timedelta(minutes=1, seconds=3)})
    historical = await client.get(f"/api/v1/model-lab/runs/{old}", params={"direction_only": True})
    assert historical.status_code == 200 and historical.json()["direction_research"]["status"] == "available"
    blocked = await client.get(f"/api/v1/model-lab/runs/{newer}", params={"direction_only": True})
    assert blocked.json()["run"]["id"] == newer
    assert blocked.json()["direction_research"]["status"] == "blocked"
    assert blocked.json()["direction_research"]["candidates"] == []
    missing = await client.get("/api/v1/model-lab/runs/99999", params={"direction_only": True})
    assert missing.status_code == 404
    listing = (await client.get("/api/v1/model-lab/runs", params={"compact": True})).json()
    assert [row["id"] for row in listing["runs"]] == [newer, old]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"snapshot_source": "page"}, {"snapshot_source": "mixed"}, {"status": "pending"},
    {"gate_passed": False}, {"snapshot_context": "unknown"}, {"snapshot_context": "mixed"},
    {"runtime_mode": "unknown"}, {"candidate_count": 13}, {"candidate_count": -1},
    {"reference_trade_date": DAY-timedelta(days=1)},
    {"as_of_at": CLOCK-timedelta(days=1)}, {"as_of_at": CLOCK.replace(hour=9)},
    {"created_at": CLOCK-timedelta(seconds=1)},
    {"created_at": CLOCK+timedelta(seconds=4)}, {"completed_at": None},
    {"completed_at": CLOCK-timedelta(seconds=1)},
    {"completed_at": datetime(2099, 1, 1)},
])
async def test_batch_type_context_date_and_clocks_fail_closed(model_lab_env, change):
    Session, _ = model_lab_env
    async with Session() as db:
        run_id = await seed_run(db, run_changes=change)
        result = await model_lab.prediction_run_detail(run_id, db=db, direction_only=True)
    assert result["direction_research"]["status"] == "blocked"
    assert result["direction_research"]["candidates"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"prediction_trade_date": DAY-timedelta(days=1)},
    {"created_at": CLOCK-timedelta(seconds=1)},
    {"created_at": CLOCK+timedelta(seconds=4)},
    {"created_at": datetime(2099, 1, 1)}, {"target_board": 9}, {"horizon_days": 2},
])
async def test_snapshot_date_target_and_clock_integrity(model_lab_env, change):
    Session, _ = model_lab_env
    async with Session() as db:
        run_id = await seed_run(db, snapshot_changes=change)
        result = await model_lab.prediction_run_detail(run_id, db=db, direction_only=True)
    assert result["direction_research"]["status"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_json", ["{", "[]", "null", "1", '"text"', '{"direction_research":[]}',
    '{"direction_research":{},"direction_research":{}}', '{"extra":NaN}', "["*2000+ "]"*2000])
async def test_bad_frozen_json_is_unavailable_not_500(model_lab_env, bad_json):
    Session, _ = model_lab_env
    async with Session() as db:
        run_id = await seed_run(db, snapshot_changes={"features_json": bad_json})
        result = await model_lab.prediction_run_detail(run_id, db=db, direction_only=True)
    assert result["direction_research"]["status"] in {"blocked", "unavailable"}
    assert result["direction_research"]["candidates"] == []


@pytest.mark.asyncio
async def test_missing_schema_is_not_initialized_by_read_branches(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'empty.db'}")
    async def forbidden(*args, **kwargs):
        raise AssertionError("initializer called")
    monkeypatch.setattr(model_lab, "ensure_prediction_ledger_storage", forbidden)
    from sqlalchemy.exc import OperationalError
    try:
        async with AsyncSession(engine) as db:
            await db.execute(text("PRAGMA query_only=ON"))
            for operation in (lambda: model_lab.prediction_runs(db=db, compact=True),
                              lambda: model_lab.prediction_run_detail(1, db=db, direction_only=True)):
                with pytest.raises(OperationalError):
                    await operation()
            assert (await db.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).all() == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("context,hour,minute", [
    ("promotion_1510", 15, 10), ("promotion_2000", 20, 0),
    ("promotion_0925", 9, 25), ("promotion_0935", 9, 35),
    ("promotion_1000", 10, 0), ("promotion_1030", 10, 30),
    ("promotion_1305", 13, 5), ("promotion_1400", 14, 0), ("promotion_1430", 14, 30),
])
async def test_supported_official_contexts_are_explicit_views_not_close_results(model_lab_env, context, hour, minute):
    Session, _ = model_lab_env
    clock = CLOCK.replace(hour=hour, minute=minute)
    async with Session() as db:
        run_id = await seed_run(db, rows=frozen_rows(1), run_changes={
            "snapshot_context": context, "as_of_at": clock,
            "created_at": clock+timedelta(seconds=1),
            "completed_at": clock+timedelta(seconds=3),
        }, snapshot_changes={"created_at": clock+timedelta(seconds=2)})
        result = await model_lab.prediction_run_detail(run_id, db=db, direction_only=True)
    assert result["run"]["snapshot_context"] == context
    assert result["direction_research"]["status"] == "insufficient_candidates"
    assert "directional_precision" not in result["direction_research"]
    assert "target_met" not in result["direction_research"]


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [date(2026, 8, 29), date(2026, 2, 17)])
async def test_closed_day_is_not_a_certified_prediction_session(model_lab_env, day):
    Session, _ = model_lab_env
    clock = datetime.combine(day, CLOCK.time())
    async with Session() as db:
        run_id = await seed_run(db, rows=frozen_rows(1), run_changes={
            "reference_trade_date": day, "as_of_at": clock,
            "created_at": clock+timedelta(seconds=1), "completed_at": clock+timedelta(seconds=3),
        }, snapshot_changes={"prediction_trade_date": day, "created_at": clock+timedelta(seconds=2)})
        result = await model_lab.prediction_run_detail(run_id, db=db, direction_only=True)
    assert result["direction_research"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_compact_projection_filters_bounds_and_tied_ids(model_lab_env, monkeypatch):
    Session, client = model_lab_env
    async with Session() as db:
        first = await seed_run(db)
        second = await seed_run(db, key="second")
        await seed_run(db, key="other", run_changes={"model_version": "other"})
        statements = []
        def observe(conn, cursor, statement, params, ctx, many):
            statements.append(statement)
        event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
        try:
            result = await model_lab.prediction_runs(db=db, compact=True, trade_date=DAY,
                snapshot_context=" promotion_2000 ", model_version=" test ", limit=1)
            none = await model_lab.prediction_runs(db=db, compact=True, model_version="absent")
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
    assert [row["id"] for row in result["runs"]] == [second]
    assert none == {"count": 0, "runs": []}
    assert all("metadata_json" not in statement and "payload_hash" not in statement for statement in statements)
    assert all(statement.lstrip().startswith("SELECT") for statement in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["duplicate_valid_key", "extra_nan", "deep_object"])
async def test_valid_proof_cannot_hide_malicious_json_container(model_lab_env, mutation):
    Session, _ = model_lab_env
    factors = frozen_rows()[0]["factors"]
    raw = json.dumps(factors)
    if mutation == "duplicate_valid_key":
        raw = raw[:-1] + ',"direction_research":' + json.dumps(factors["direction_research"]) + '}'
    elif mutation == "extra_nan":
        raw = raw[:-1] + ',"unused":NaN}'
    else:
        raw = raw[:-1] + ',"unused":' + '{"x":'*2000 + '0' + '}'*2000 + '}'
    async with Session() as db:
        run_id = await seed_run(db, snapshot_changes={"features_json": raw})
        result = await model_lab.prediction_run_detail(run_id, db=db, direction_only=True)
    assert result["direction_research"]["status"] in {"blocked", "unavailable"}
    assert result["direction_research"]["candidates"] == []


def test_projection_never_echoes_unrelated_nested_proof_material():
    rows = frozen_rows()
    rows[0]["factors"]["direction_research"]["unrelated"] = {"x": float("nan")}
    result = project_frozen_direction_research(rows, batch_valid=True)
    assert result["status"] == "available"
    assert all("unrelated" not in item["probability_factors"]["direction_research"]
               for item in result["candidates"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("value", [None, {}, "bad"])
def test_invalid_factor_objects_are_not_reconstructed(value):
    rows = frozen_rows()
    rows[0]["factors"] = value
    assert project_frozen_direction_research(rows, batch_valid=True)["status"] in {"blocked", "unavailable"}


def test_declared_missing_recordability_keeps_evidence_but_never_candidates():
    rows = frozen_rows()
    for row in rows:
        row["factors"]["direction_research"]["missing_recordable_count"] = 2
    result = project_frozen_direction_research(rows, batch_valid=True)
    assert result["status"] == "blocked"
    assert result["missing_recordable_count"] == 2
    assert result["candidates"] == []


@pytest.mark.parametrize("mutation", ["eligibility_missing", "eligibility_zero", "rank_missing"])
def test_unselected_rows_still_require_explicit_frozen_fields(mutation):
    source = [
        {"code": f"60{i:04d}", "target_board": 1, "direction_probability": .5,
         "probability_factors": {"prediction_rank_eligible": i != 0,
                                "learning_direction_probability_method": "fixture"}}
        for i in range(13)
    ]
    annotated, _ = annotate_direction_research(source)
    rows = [{"code": item["code"], "factors": item["probability_factors"]} for item in annotated]
    if mutation == "eligibility_missing":
        rows[0]["factors"].pop("prediction_rank_eligible")
    elif mutation == "eligibility_zero":
        rows[0]["factors"]["prediction_rank_eligible"] = 0
    else:
        rows[0]["factors"]["direction_research"].pop("rank_position")
    result = project_frozen_direction_research(rows, batch_valid=True)
    assert result["status"] == "blocked" and result["candidates"] == []
