"""Isolated SELECT-only contracts; never start the application or call HTTP."""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import model_lab
from app.config.settings import settings
from app.db import session as application_session
from app.models.promotion import (
    PromotionDeploymentEvent as Deployment,
    PromotionModelArtifact as Artifact,
    PromotionPredictionRun as Prediction,
    PromotionShadowEvaluation as Evaluation,
    PromotionShadowRun as Shadow,
    PromotionTrainingRun as Training,
)
from app.promotion import deployment, ledger, shadow
from app.promotion.labels import PROMOTION_LABEL_VERSION
from app.promotion.modeling import training
from app.promotion.modeling.acceptance import DEFAULT_ACCEPTANCE_POLICY, SHADOW_ACCEPTANCE_POLICY
from app.promotion.modeling.features import FEATURE_VERSION
from app.promotion.versioning import get_promotion_model_identity
from app.review import model_evidence as evidence
from scripts import ashare_review_mcp as mcp


CLOCK = datetime(2026, 9, 29, 20, 0, 0, 123456)
TABLES = (Artifact.__table__, Prediction.__table__, Training.__table__,
          Shadow.__table__, Evaluation.__table__, Deployment.__table__)
TOOLS = (
    ("ashare_prediction_runs", Prediction, "runs", 50, 200),
    ("ashare_training_runs", Training, "training_runs", 30, 100),
    ("ashare_shadow_runs", Shadow, "shadow_runs", 100, 300),
    ("ashare_shadow_evaluations", Evaluation, "evaluations", 100, 300),
    ("ashare_deployments", Deployment, None, None, None),
)
WRITE_ACTIONS = {
    getattr(sqlite3, f"SQLITE_{name}") for name in (
        "INSERT", "UPDATE", "DELETE", "CREATE_INDEX", "CREATE_TABLE",
        "CREATE_TEMP_INDEX", "CREATE_TEMP_TABLE", "CREATE_TEMP_TRIGGER",
        "CREATE_TEMP_VIEW", "CREATE_TRIGGER", "CREATE_VIEW", "DROP_INDEX",
        "DROP_TABLE", "DROP_TEMP_INDEX", "DROP_TEMP_TABLE", "DROP_TEMP_TRIGGER",
        "DROP_TEMP_VIEW", "DROP_TRIGGER", "DROP_VIEW", "ALTER_TABLE", "REINDEX",
        "ANALYZE", "ATTACH", "DETACH",
    )
}


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_guard():
    # Override the global conftest initializer only for this module: these tests
    # own tiny disposable databases and must never call application init_db.
    yield


@pytest.fixture(autouse=True)
def forbid_business_writes(monkeypatch):
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append("business/session write")
        raise AssertionError("model evidence must not call a business writer or HTTP handler")

    for module in (model_lab, deployment, ledger, shadow, training):
        monkeypatch.setattr(module, "ensure_prediction_ledger_storage", forbidden)
    for name in ("prediction_runs", "training_runs", "shadow_runs", "shadow_evaluations",
                 "deployments", "train_challenger", "run_shadow", "evaluate_shadow",
                 "approve_deployment", "rollback_deployment", "get_db"):
        monkeypatch.setattr(model_lab, name, forbidden)
    for name in ("current_deployment", "list_deployment_state", "approve_challenger",
                 "rollback_challenger", "apply_active_promotion_overlay"):
        monkeypatch.setattr(deployment, name, forbidden)
    monkeypatch.setattr(training, "train_promotion_challenger", forbidden)
    monkeypatch.setattr(shadow, "run_shadow_inference", forbidden)
    monkeypatch.setattr(shadow, "evaluate_shadow_artifact", forbidden)
    monkeypatch.setattr(application_session, "init_db", forbidden)
    monkeypatch.setattr(application_session, "async_session", forbidden)
    monkeypatch.setattr(mcp, "_http_get", forbidden)
    for name in ("add", "add_all", "delete", "flush", "commit", "merge", "rollback", "get"):
        monkeypatch.setattr(AsyncSession, name, forbidden)
    yield attempted
    assert attempted == []


def _policy():
    return {
        **DEFAULT_ACCEPTANCE_POLICY, **SHADOW_ACCEPTANCE_POLICY,
        "minimum_outcome_kline_rows": max(int(settings.PROMOTION_SHADOW_MIN_KLINE_ROWS), 1),
        "minimum_outcome_kline_completeness": min(
            max(float(settings.PROMOTION_SHADOW_MIN_KLINE_COMPLETENESS), 0.0), 1.0),
    }


def _artifact(tmp_path, target):
    model_version = f"challenger-fixture-{target}"
    payload = {
        "schema_version": "promotion_model_artifact_v1", "target_board": target,
        "model_version": model_version, "feature_version": FEATURE_VERSION,
        "data_version": f"sealed-fixture-data-{target}",
        "model": {
            "fit_end_date": "2026-08-01", "calibration_end_date": "2026-08-10",
            "vectorizer": {"feature_version": FEATURE_VERSION,
                           "numeric_names": ["route_score"], "route_categories": [],
                           "regime_categories": [], "mean": [0.0], "scale": [1.0]},
            "classifier": {"algorithm": "numpy_logistic_regression",
                           "coefficients": [0.1], "intercept": -1.0},
            "calibrator": {"method": "platt_out_of_time_tail", "slope": 1.0, "intercept": 0.0},
        },
        "training_config": {"fixture": True}, "acceptance": {"passed": True},
    }
    path = tmp_path / f"artifact-{target}.json"
    raw = json.dumps(payload).encode()
    path.write_bytes(raw)
    return {
        "id": target, "model_version": model_version, "feature_version": FEATURE_VERSION,
        "data_version": payload["data_version"], "algorithm": "numpy_logistic_regression",
        "status": "approved", "artifact_uri": str(path), "created_at": CLOCK,
    }, hashlib.sha256(raw).hexdigest()


async def _database(tmp_path, *, count=2, omit=None):
    path = (tmp_path / "evidence.sqlite").resolve()
    assert path.is_relative_to(tmp_path.resolve())
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    selected_tables = [table for table in TABLES if table.name != omit]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Artifact.metadata.create_all(sync, tables=selected_tables))
            if not count:
                return path
            records = {model: [] for model in (Prediction, Training, Shadow, Evaluation)}
            artifacts, hashes = {}, {}
            for target in (1, 2):
                artifacts[target], hashes[target] = _artifact(tmp_path, target)
            await connection.execute(Artifact.__table__.insert(), list(artifacts.values()))
            for identity in range(1, count + 1):
                target = 1 if identity % 2 else 2
                clock = CLOCK + timedelta(days=identity - 1)
                context = "promotion_2000" if target == 1 else "promotion_1510"
                model_version = f"champion-recorded-{identity}"
                metadata = {"fixture": identity, "quality": None,
                            "model_version": model_version, "data_version": f"pit-data-{identity}"}
                metrics = {"sample_count": 80 + identity, "positive_count": 0,
                           "unknown_count": 3, "average_precision": None,
                           "regime_slices": {"unknown": {"sample_count": 3}},
                           "brier_score": 0.125, "win_rate": 0.0}
                acceptance = {"passed": target == 1, "policy": _policy(),
                              "decision": "manual_review_eligible" if target == 1 else "rejected",
                              "checks": [{"name": "fixture", "passed": target == 1}]}
                records[Prediction].append({
                    "id": identity, "run_key": f"run-{identity}",
                    "snapshot_batch_key": f"batch-{identity}", "reference_trade_date": clock.date(),
                    "as_of_at": clock, "snapshot_source": "schedule", "snapshot_context": context,
                    "model_version": model_version, "feature_version": f"features-{identity}",
                    "data_version": f"pit-data-{identity}", "runtime_mode": "legacy",
                    "status": "completed" if target == 1 else "blocked",
                    "gate_passed": True if target == 1 else None, "candidate_count": 80 + identity,
                    "ranked_count": 12, "actionable_count": 0, "payload_hash": f"hash-{identity}",
                    "metadata_json": json.dumps(metadata), "created_at": clock,
                    "completed_at": clock + timedelta(seconds=1) if target == 1 else None,
                })
                records[Training].append({
                    "id": identity, "training_key": f"train-{identity}", "target_board": target,
                    "status": "completed" if target == 1 else "failed",
                    "model_version": artifacts[target]["model_version"] if target == 1 else None,
                    "feature_version": FEATURE_VERSION, "data_version": f"pit-data-{identity}",
                    "config_json": json.dumps({"snapshot_context": context, "persist": False}),
                    "dataset_json": json.dumps({"data_version": f"pit-data-{identity}",
                                                "as_of": clock.isoformat(), "unknown_count": 3}),
                    "metrics_json": json.dumps(metrics), "acceptance_json": json.dumps(acceptance),
                    "artifact_id": target if target == 1 else None,
                    "error_message": None if target == 1 else "fixture failure",
                    "started_at": clock, "completed_at": clock + timedelta(seconds=1),
                })
                records[Shadow].append({
                    "id": identity, "shadow_key": f"shadow-{identity}", "prediction_run_id": identity,
                    "artifact_id": target, "target_board": target, "snapshot_context": context,
                    "reference_trade_date": clock.date(), "as_of_at": clock,
                    "champion_model_version": model_version,
                    "challenger_model_version": artifacts[target]["model_version"],
                    "feature_version": FEATURE_VERSION, "data_version": f"pit-data-{identity}",
                    "status": "completed", "candidate_count": 80 + identity,
                    "payload_hash": f"shadow-hash-{identity}", "metadata_json": json.dumps(metadata),
                    "created_at": clock, "completed_at": clock + timedelta(seconds=1),
                })
                records[Evaluation].append({
                    "id": identity, "evaluation_key": f"evaluation-{identity}", "artifact_id": target,
                    "trigger_shadow_run_id": identity, "target_board": target,
                    "snapshot_context": context, "challenger_model_version": artifacts[target]["model_version"],
                    "label_version": PROMOTION_LABEL_VERSION, "outcome_end_date": clock.date(),
                    "evaluated_shadow_run_count": identity, "trade_day_count": 30,
                    "sample_count": 80 + identity, "positive_count": 0,
                    "decision": acceptance["decision"], "metrics_json": json.dumps(metrics),
                    "acceptance_json": json.dumps(acceptance),
                    "metadata_json": json.dumps({"evaluation_version": shadow.SHADOW_EVALUATION_VERSION,
                                                 "data_version": f"pit-data-{identity}", "unknown_count": 3}),
                    "created_at": clock + timedelta(seconds=2),
                })
            for model, rows in records.items():
                await connection.execute(model.__table__.insert(), rows)
            events = []
            for target in (1, 2):
                events.append({
                    "id": target, "event_key": f"event-{target}", "target_board": target,
                    "action": "approve" if target == 1 else "rollback",
                    "deployment_mode": "probability_overlay" if target == 1 else "legacy",
                    "artifact_id": 1 if target == 1 else None,
                    "evidence_evaluation_id": 1 if target == 1 else None,
                    "from_model_version": "previous-recorded-version",
                    "to_model_version": artifacts[1]["model_version"] if target == 1 else "legacy-recorded",
                    "operator": "fixture-operator", "reason": "isolated test fixture only",
                    "confirmation_phrase": "fixture",
                    "metadata_json": json.dumps({"artifact_sha256": hashes[1]} if target == 1 else {}),
                    "created_at": CLOCK + timedelta(seconds=3 + target),
                })
            await connection.execute(Deployment.__table__.insert(), events)
    finally:
        await engine.dispose()
    return path


@asynccontextmanager
async def _read_session(path: Path, *, autoflush=False):
    before = path.read_bytes()
    guard = SimpleNamespace(statements=[], write_attempts=[], flush_attempts=[])
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")

    def authorize(action, first, second, database, source):
        if action in WRITE_ACTIONS:
            guard.write_attempts.append((action, first, second))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @event.listens_for(engine.sync_engine, "connect")
    def readonly(connection, record):
        connection.execute("PRAGMA query_only=ON")
        connection.run_async(lambda raw: raw.set_authorizer(authorize))

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def observe(connection, cursor, statement, params, context, many):
        guard.statements.append(statement)

    try:
        async with AsyncSession(engine, autoflush=autoflush, expire_on_commit=False) as db:
            assert await db.scalar(text("PRAGMA query_only")) == 1
            await db.execute(text("BEGIN"))
            assert db.in_transaction()

            @event.listens_for(db.sync_session, "before_flush")
            def no_flush(*args):
                guard.flush_attempts.append("flush")
                raise AssertionError("even a flush attempt is forbidden")

            guard.statements.clear()
            guard.db = db
            yield guard
            assert db.in_transaction(), "the adapter must not close the caller-owned transaction"
    finally:
        await engine.dispose()
        assert path.read_bytes() == before, "the read adapter changed SQLite bytes"


def _assert_select_only(guard):
    assert guard.statements
    assert all(sql.lstrip().upper().startswith("SELECT ") for sql in guard.statements)
    assert guard.write_attempts == []
    assert guard.flush_attempts == []


def test_names_argument_sets_and_limit_defaults_match_existing_mcp_schema():
    assert evidence.MODEL_EVIDENCE_TOOLS == {tool[0] for tool in TOOLS}
    for name, model, output, default, maximum in TOOLS:
        schema = mcp.TOOLS[name]["inputSchema"]
        assert evidence._ARGUMENTS[name] == set(schema["properties"])
        if default is not None:
            assert evidence._LIMITS[name] == (schema["properties"]["limit"]["default"],
                                               schema["properties"]["limit"]["maximum"])
            assert schema["properties"]["limit"]["minimum"] == 1


@pytest.mark.asyncio
async def test_five_paths_preserve_original_fields_clocks_identity_metrics_and_eligibility(tmp_path):
    path = await _database(tmp_path)
    async with _read_session(path) as guard:
        results = {name: await evidence.read_model_evidence(guard.db, tool_name=name, arguments={})
                   for name, *_ in TOOLS}
        run = results["ashare_prediction_runs"]["runs"][1]
        assert run == {
            "id": 1, "run_key": "run-1", "snapshot_batch_key": "batch-1",
            "reference_trade_date": "2026-09-29", "as_of_at": "2026-09-29T20:00:00",
            "snapshot_source": "schedule", "snapshot_context": "promotion_2000",
            "model_version": "champion-recorded-1", "feature_version": "features-1",
            "data_version": "pit-data-1", "runtime_mode": "legacy", "status": "completed",
            "gate_passed": True, "candidate_count": 81, "ranked_count": 12, "actionable_count": 0,
            "payload_hash": "hash-1",
            "metadata": {"fixture": 1, "quality": None, "model_version": "champion-recorded-1",
                         "data_version": "pit-data-1"},
            "created_at": "2026-09-29T20:00:00", "completed_at": "2026-09-29T20:00:01",
        }
        blocked = results["ashare_prediction_runs"]["runs"][0]
        assert blocked["status"] == "blocked" and blocked["gate_passed"] is None
        assert blocked["completed_at"] is None
        trained = results["ashare_training_runs"]["training_runs"][1]
        assert set(trained) == {
            "id", "training_key", "target_board", "status", "model_version", "feature_version",
            "data_version", "config", "dataset", "metrics", "acceptance", "artifact_id",
            "error_message", "started_at", "completed_at",
        }
        assert trained["model_version"] == "challenger-fixture-1"
        assert trained["feature_version"] == FEATURE_VERSION and trained["data_version"] == "pit-data-1"
        assert trained["started_at"] == CLOCK.isoformat()
        assert trained["dataset"]["as_of"] == CLOCK.isoformat()
        assert trained["metrics"]["positive_count"] == 0 and trained["metrics"]["average_precision"] is None
        failed = results["ashare_training_runs"]["training_runs"][0]
        assert failed["model_version"] is None and failed["error_message"] == "fixture failure"
        paired = results["ashare_shadow_runs"]["shadow_runs"][1]
        assert set(paired) == {
            "id", "shadow_key", "prediction_run_id", "artifact_id", "target_board",
            "snapshot_context", "reference_trade_date", "as_of_at", "champion_model_version",
            "challenger_model_version", "status", "candidate_count", "metadata", "created_at",
        }
        assert paired["champion_model_version"] == "champion-recorded-1"
        assert paired["challenger_model_version"] == "challenger-fixture-1"
        assert paired["reference_trade_date"] == "2026-09-29" and paired["as_of_at"] == CLOCK.isoformat()
        evaluated = results["ashare_shadow_evaluations"]["evaluations"][1]
        assert set(evaluated) == {
            "id", "evaluation_key", "artifact_id", "target_board", "snapshot_context",
            "challenger_model_version", "label_version", "outcome_end_date",
            "evaluated_shadow_run_count", "trade_day_count", "sample_count", "positive_count",
            "decision", "metrics", "acceptance", "metadata", "created_at",
        }
        assert evaluated["label_version"] == PROMOTION_LABEL_VERSION
        assert evaluated["decision"] == "manual_review_eligible" and evaluated["acceptance"]["passed"] is True
        assert evaluated["metrics"]["unknown_count"] == 3 and evaluated["positive_count"] == 0
        assert evaluated["outcome_end_date"] == "2026-09-29"
        deployed = results["ashare_deployments"]
        assert set(deployed) == {"lanes", "events", "automatic_promotion"}
        assert deployed["automatic_promotion"] is False
        active, rolled_back = deployed["lanes"]
        assert active["active"] is True and active["integrity_ok"] is True
        assert active["effective_active"] is True
        assert active["effective_model_version"] == "challenger-fixture-1"
        assert active["execution_enabled"] == bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED)
        assert rolled_back["active"] is False and rolled_back["effective_active"] is False
        assert rolled_back["latest_event"]["action"] == "rollback"
        assert [row["id"] for row in deployed["events"]] == [2, 1]
        assert set(deployed["events"][0]) == {
            "id", "event_key", "target_board", "action", "deployment_mode", "artifact_id",
            "evidence_evaluation_id", "from_model_version", "to_model_version",
            "operator", "reason", "metadata", "created_at",
        }
        json.dumps(results, allow_nan=False)  # No custom encoder or erased dates needed.
        _assert_select_only(guard)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,model,output,default,maximum", TOOLS)
async def test_missing_primary_table_raises_operational_error_without_repair(
        tmp_path, name, model, output, default, maximum):
    path = await _database(tmp_path, count=0, omit=model.__tablename__)
    async with _read_session(path) as guard:
        with pytest.raises(evidence.OperationalError, match="no such table"):
            await evidence.read_model_evidence(guard.db, tool_name=name, arguments={})
        _assert_select_only(guard)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [Artifact.__tablename__, Evaluation.__tablename__])
async def test_even_empty_deployment_requires_registry_and_evidence_schema(tmp_path, missing):
    path = await _database(tmp_path, count=0, omit=missing)
    async with _read_session(path) as guard:
        with pytest.raises(evidence.OperationalError, match="no such table"):
            await evidence.read_model_evidence(guard.db, tool_name="ashare_deployments", arguments={})
        _assert_select_only(guard)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,model,output,default,maximum", TOOLS)
async def test_missing_column_is_unavailable_not_empty_success(
        tmp_path, name, model, output, default, maximum):
    path = await _database(tmp_path, count=0, omit=model.__tablename__)
    with sqlite3.connect(path) as writer:
        writer.execute(f"CREATE TABLE {model.__tablename__} (id INTEGER PRIMARY KEY)")
    async with _read_session(path) as guard:
        with pytest.raises(evidence.OperationalError, match="no such column"):
            await evidence.read_model_evidence(guard.db, tool_name=name, arguments={})
        _assert_select_only(guard)


@pytest.mark.asyncio
async def test_existing_empty_tables_are_legitimate_empty_evidence(tmp_path):
    path = await _database(tmp_path, count=0)
    async with _read_session(path) as guard:
        for name, model, output, default, maximum in TOOLS:
            result = await evidence.read_model_evidence(guard.db, tool_name=name, arguments={})
            if output:
                assert result == {"count": 0, output: []}
            else:
                assert result["events"] == [] and result["automatic_promotion"] is False
                assert [lane["target_board"] for lane in result["lanes"]] == [1, 2]
                assert all(lane["integrity_ok"] and not lane["effective_active"] for lane in result["lanes"])
        _assert_select_only(guard)


@pytest.mark.asyncio
async def test_original_filters_do_not_resolve_dates_or_fallback(tmp_path):
    path = await _database(tmp_path)
    async with _read_session(path) as guard:
        read = evidence.read_model_evidence
        result = await read(guard.db, tool_name="ashare_prediction_runs", arguments={
            "trade_date": "2026-09-29", "snapshot_context": " promotion_2000 ",
            "model_version": " champion-recorded-1 ", "limit": 1})
        assert [row["id"] for row in result["runs"]] == [1]
        assert (await read(guard.db, tool_name="ashare_prediction_runs",
                           arguments={"trade_date": "2026-10-01"})) == {"count": 0, "runs": []}
        for name, model, output, default, maximum in TOOLS[1:4]:
            args = {"target_board": 2}
            if name != "ashare_training_runs":
                args["artifact_id"] = 2
            result = await read(guard.db, tool_name=name, arguments=args)
            assert [row["id"] for row in result[output]] == [2]
            if "artifact_id" in args:
                result = await read(guard.db, tool_name=name, arguments={"artifact_id": 999})
                assert result == {"count": 0, output: []}
        _assert_select_only(guard)


@pytest.mark.asyncio
async def test_limits_use_original_defaults_maxima_and_order(tmp_path):
    path = await _database(tmp_path, count=305)
    async with _read_session(path) as guard:
        for name, model, output, default, maximum in TOOLS[:4]:
            for args, expected in (({}, default), ({"limit": 1}, 1), ({"limit": maximum}, maximum)):
                result = await evidence.read_model_evidence(guard.db, tool_name=name, arguments=args)
                assert result["count"] == expected
                assert [row["id"] for row in result[output]] == list(range(305, 305 - expected, -1))
        _assert_select_only(guard)


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("unknown", {}), ("ashare_deployments", {"target_board": 1}),
    ("ashare_prediction_runs", {"compact": True}), ("ashare_prediction_runs", {"trade_date": None}),
    ("ashare_prediction_runs", {"trade_date": "2026-09-31"}),
    ("ashare_prediction_runs", {"trade_date": "20260929"}),
    ("ashare_prediction_runs", {"model_version": 1}), ("ashare_prediction_runs", {"limit": True}),
    ("ashare_prediction_runs", {"limit": 201}), ("ashare_training_runs", {"limit": 101}),
    ("ashare_training_runs", {"target_board": 0}), ("ashare_training_runs", {"target_board": True}),
    ("ashare_shadow_runs", {"artifact_id": 0}), ("ashare_shadow_runs", {"artifact_id": "1"}),
    ("ashare_shadow_runs", {"limit": 301}), ("ashare_shadow_evaluations", {"limit": 301}),
    ("ashare_shadow_evaluations", {"limit": 0}), ("ashare_deployments", None),
    ("ashare_deployments", []),
])
async def test_invalid_arguments_are_rejected_before_any_sql(tmp_path, name, arguments):
    path = await _database(tmp_path, count=0)
    async with _read_session(path) as guard:
        with pytest.raises(ValueError):
            await evidence.read_model_evidence(guard.db, tool_name=name, arguments=arguments)
        assert guard.statements == [] and guard.write_attempts == []


@pytest.mark.asyncio
async def test_autoflush_and_dirty_identity_map_cannot_taint_any_read_path(tmp_path):
    path = await _database(tmp_path)
    async with _read_session(path, autoflush=True) as guard:
        with guard.db.no_autoflush:  # Arrange dirty objects without a setup-side flush.
            for model in (Prediction, Training, Shadow, Evaluation, Artifact, Deployment):
                row = (await guard.db.execute(select(model).where(model.id == 1))).scalar_one()
                if model is Artifact:
                    row.model_version = "uncommitted-artifact-version"
                elif model is Evaluation:
                    row.decision = "rejected"
                elif model is Deployment:
                    row.artifact_id = None
                else:
                    row.status = "uncommitted-status"
        guard.db.sync_session.add(Training(
            training_key="must-never-flush", target_board=1, feature_version=FEATURE_VERSION))
        dirty, pending = set(guard.db.dirty), set(guard.db.new)
        guard.statements.clear()
        results = {name: await evidence.read_model_evidence(guard.db, tool_name=name, arguments={})
                   for name, *_ in TOOLS}
        assert results["ashare_prediction_runs"]["runs"][1]["status"] == "completed"
        assert results["ashare_training_runs"]["training_runs"][1]["status"] == "completed"
        assert results["ashare_shadow_runs"]["shadow_runs"][1]["status"] == "completed"
        assert results["ashare_shadow_evaluations"]["evaluations"][1]["decision"] == "manual_review_eligible"
        assert results["ashare_deployments"]["lanes"][0]["effective_model_version"] == "challenger-fixture-1"
        assert results["ashare_deployments"]["lanes"][0]["integrity_ok"] is True
        assert set(guard.db.dirty) == dirty and set(guard.db.new) == pending
        assert guard.db.autoflush is True
        _assert_select_only(guard)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_artifact_row", "missing_local_file", "hash", "policy", "label"])
async def test_deployment_integrity_preserves_existing_fail_closed_semantics(tmp_path, fault):
    path = await _database(tmp_path)
    with sqlite3.connect(path) as writer:
        if fault == "missing_artifact_row":
            writer.execute("UPDATE promotion_deployment_event SET artifact_id=999 WHERE id=1")
        elif fault == "missing_local_file":
            writer.execute("UPDATE promotion_model_artifact SET artifact_uri=? WHERE id=1",
                           (str(tmp_path / "absent.json"),))
        elif fault == "hash":
            writer.execute("UPDATE promotion_deployment_event SET metadata_json=? WHERE id=1",
                           (json.dumps({"artifact_sha256": "wrong"}),))
        elif fault == "policy":
            writer.execute("UPDATE promotion_shadow_evaluation SET acceptance_json=? WHERE id=1",
                           (json.dumps({"passed": True, "policy": {"obsolete": True}}),))
        else:
            writer.execute("UPDATE promotion_shadow_evaluation SET label_version=? WHERE id=1", ("obsolete",))
    async with _read_session(path) as guard:
        result = await evidence.read_model_evidence(guard.db, tool_name="ashare_deployments", arguments={})
        lane = result["lanes"][0]
        assert lane["integrity_ok"] is False and lane["effective_active"] is False
        assert lane["effective_model_version"] == get_promotion_model_identity().active_model_version
        assert lane["integrity_error"]
        _assert_select_only(guard)


@pytest.mark.asyncio
async def test_original_object_json_fallbacks_do_not_recompute_metrics(tmp_path):
    path = await _database(tmp_path)
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE promotion_training_run SET metrics_json=?, acceptance_json=? WHERE id=1",
                       ("invalid JSON", "[1, 2]"))
        writer.execute("UPDATE promotion_shadow_evaluation SET metrics_json=?, acceptance_json=? WHERE id=1",
                       ("null", "[]"))
    async with _read_session(path) as guard:
        trained = await evidence.read_model_evidence(guard.db, tool_name="ashare_training_runs", arguments={})
        evaluated = await evidence.read_model_evidence(guard.db, tool_name="ashare_shadow_evaluations", arguments={})
        assert trained["training_runs"][1]["metrics"] == trained["training_runs"][1]["acceptance"] == {}
        assert evaluated["evaluations"][1]["metrics"] == evaluated["evaluations"][1]["acceptance"] == {}
        assert evaluated["evaluations"][1]["decision"] == "manual_review_eligible"  # Stored claim only.
        _assert_select_only(guard)
