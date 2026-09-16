"""Isolated radar evidence only: no real push, quotes, orders or production DB."""
import copy
import hashlib
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, select, text, update, delete
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.v1 import tenbagger as api
from app.models.signal import AnomalyCandidateEvidence as Evidence, AnomalyCandidateRecord as Record, SignalPerformance
from app.push.channels.base import PushMessage
from app.signal.candidate_evidence import append_evaluations, freeze
from test_tenbagger_anomaly_logic import tenbagger_session

AT = datetime(2026, 9, 15, 10)


@pytest.fixture
def capture_clock(monkeypatch):
    clock = [AT]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(api, "datetime", Clock)
    monkeypatch.setattr(api, "_resolve_anomaly_buy_point_fields", lambda item, peers: {
        "buy_point_grade": "A2", "buy_point_pushable": item.get("pushable", True),
        "buy_point_blockers": list(item.get("blockers", [])),
    })
    return clock


def anomaly():
    return {"code": "600001", "name": "隔离样本", "event_type": "capital",
            "score": 90, "detail": {"capital_anomaly_type": "main_inflow", "value": 1}}


def message(item, observation=False):
    identity = api._anomaly_signal_identity(item) + ("|observation" if observation else "")
    return PushMessage(title="fixture", content="isolated", stock_code=item["code"],
                       extra={"signal_identity": identity})


async def evidence(db):
    return (await db.scalars(select(Evidence).order_by(Evidence.id))).all()


@pytest.mark.asyncio
async def test_later_rejection_and_a_b_a_do_not_rewrite_first_reason(tenbagger_session, capture_clock):
    db, item = tenbagger_session, anomaly()
    for reason in ("first", "second", "first"):
        item["blockers"] = [reason]
        item["pushable"] = False
        result = await api._persist_anomaly_candidate_records(db, AT.date(), [item], evaluated=True)
        assert result["status"] == "recorded"
        capture_clock[0] += timedelta(seconds=20)
    rows = await evidence(db)
    assert len(rows) == 3
    bodies = [json.loads(row.payload_json) for row in rows]
    assert [row["candidate_blockers"] for row in bodies] == [["first"], ["second"], ["first"]]
    assert len({row.capture_id for row in rows}) == 3
    assert all(row["historical_pit_verified"] is False and row["trading_authority"] is False for row in bodies)
    assert all(row["physical_commit_at"] is None for row in bodies)
    projection = (await db.scalars(select(Record))).one()
    assert projection.first_seen_at == AT and projection.last_seen_at == AT + timedelta(seconds=40)
    assert projection.seen_count == 3


@pytest.mark.asyncio
async def test_ever_sent_does_not_mean_sent_in_later_capture(tenbagger_session, capture_clock):
    db, item = tenbagger_session, anomaly()
    await api._persist_anomaly_candidate_records(db, AT.date(), [item], evaluated=True,
        messages=[message(item)], results=[{"sent": True, "channels": {"feishu": True}}])
    capture_clock[0] += timedelta(seconds=30)
    item["pushable"], item["blockers"] = False, ["now_invalid"]
    await api._persist_anomaly_candidate_records(db, AT.date(), [item], evaluated=True)
    bodies = [json.loads(row.payload_json) for row in await evidence(db)]
    assert bodies[0]["delivery"]["status"] == "reported_sent"
    assert bodies[1]["delivery"]["status"] == "not_dispatched"
    assert bodies[1]["pushed_before_capture"] is True
    assert bodies[1]["candidate_blockers"] == ["now_invalid"]
    assert (await db.scalars(select(Record))).one().status == "pushed"  # compatibility only
    assert (await db.scalars(select(SignalPerformance))).all() == []  # no push quota writes


@pytest.mark.asyncio
@pytest.mark.parametrize("results,expected", [
    ([], "unknown"),
    ([{}], "unknown"),
    ([None], "unknown"),
    ([{"sent": False, "throttled": True}], "throttled"),
    ([{"sent": False, "disabled": True}], "failed"),
    ([{"sent": False, "channels": {"feishu": False}}], "failed"),
    ([{"sent": True, "channels": {"feishu": True, "ws": False}}], "reported_sent"),
])
async def test_missing_or_channel_outcomes_are_frozen_not_inferred(
    tenbagger_session, capture_clock, results, expected,
):
    item = anomaly()
    await api._persist_anomaly_candidate_records(tenbagger_session, AT.date(), [item],
        evaluated=True, messages=[message(item, observation=True)], results=results)
    body = json.loads((await evidence(tenbagger_session))[0].payload_json)
    assert body["delivery"]["status"] == expected
    assert body["delivery"]["attempts"][0]["message_kind"] == "observation"
    assert body["delivery"]["attempts"][0]["result_available"] == bool(results and isinstance(results[0], dict))
    assert body["candidate_pushable"] is True  # technical input != execution permission


@pytest.mark.asyncio
async def test_multiple_results_remain_distinct_instead_of_success_overwrite(tenbagger_session, capture_clock):
    item = anomaly()
    await api._persist_anomaly_candidate_records(tenbagger_session, AT.date(), [item],
        evaluated=True, messages=[message(item), message(item, True), message(item)],
        results=[{"sent": True, "channels": {"ws": True}},
                 {"sent": False, "channels": {"feishu": False}}])
    body = json.loads((await evidence(tenbagger_session))[0].payload_json)
    attempts = body["delivery"]["attempts"]
    assert len(attempts) == 3
    assert [attempt["sent"] for attempt in attempts] == [True, False, None]
    assert [attempt["message_kind"] for attempt in attempts] == ["signal", "observation", "signal"]
    assert attempts[2]["result_available"] is False
    assert body["delivery"]["status"] == "reported_sent"


@pytest.mark.asyncio
async def test_snapshot_and_gate_inputs_frozen_before_first_database_await(tenbagger_session, capture_clock, monkeypatch):
    db, item = tenbagger_session, anomaly()
    identity = api._anomaly_signal_identity(item)
    gates = {identity: {"round_budget_passed": False}}
    real, fired = db.execute, []
    async def execute(*args, **kwargs):
        if not fired:
            fired.append(True)
            item["detail"]["value"] = 999
            gates[identity]["round_budget_passed"] = True
        return await real(*args, **kwargs)
    monkeypatch.setattr(db, "execute", execute)
    await api._persist_anomaly_candidate_records(db, AT.date(), [item], evaluated=True, gate_trace=gates)
    body = json.loads((await evidence(db))[0].payload_json)
    assert fired and body["snapshot"]["anomaly"]["detail"]["value"] == 1
    assert body["gate_trace"]["round_budget_passed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("day_delta", [-1, 1])
async def test_historical_refresh_is_not_backdated_or_written(tenbagger_session, capture_clock, day_delta):
    db = tenbagger_session
    result = await api._persist_anomaly_candidate_records(
        db, (AT + timedelta(days=day_delta)).date(), [anomaly()], evaluated=True)
    assert result["status"] == "not_recorded"
    assert await evidence(db) == []
    assert (await db.scalars(select(Record))).all() == []


@pytest.mark.asyncio
async def test_nonfinite_evidence_is_explicitly_unavailable_not_fabricated(tenbagger_session, capture_clock):
    item = anomaly()
    item["detail"]["value"] = float("nan")
    result = await api._persist_anomaly_candidate_records(tenbagger_session, AT.date(), [item], evaluated=True)
    assert result["status"] == "unavailable"
    assert result["reason"] == "non_json_or_nonfinite_candidate_evidence"
    assert await evidence(tenbagger_session) == []


@pytest.mark.asyncio
async def test_missing_migration_keeps_compatibility_but_reports_no_evidence(tenbagger_session, capture_clock):
    db = tenbagger_session
    await db.execute(text("DROP TABLE anomaly_candidate_evidence"))  # isolated empty fixture only
    await db.commit()
    result = await api._persist_anomaly_candidate_records(db, AT.date(), [anomaly()], evaluated=True)
    assert result["status"] == "unavailable"
    assert (await db.scalars(select(Record))).one().status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("trace", [
    {"identity": {"bad": float("nan")}}, [], {"identity": []},
])
async def test_bad_gate_trace_cannot_turn_valid_snapshot_into_false_recorded_status(
    tenbagger_session, capture_clock, trace,
):
    result = await api._persist_anomaly_candidate_records(tenbagger_session, AT.date(),
        [anomaly()], evaluated=True, gate_trace=trace)
    assert result["status"] == "unavailable"
    assert await evidence(tenbagger_session) == []


def observation():
    return {"record_id": "candidate-fixture", "code": "600001", "candidate_blockers": ["first"]}


@pytest.mark.asyncio
async def test_exact_capture_retry_and_collision_never_rewrite(tenbagger_session):
    db = tenbagger_session
    args = dict(capture_id="fixed", trade_date=AT.date(), captured_at=AT, observations=[observation()])
    first = await append_evaluations(db, **args)
    await db.commit()
    before = (await evidence(db))[0].payload_json
    second = await append_evaluations(db, **args)
    await db.commit()
    assert first["inserted"] == 1 and second["inserted"] == 0 and second["matched"] == 1
    args["observations"][0]["candidate_blockers"] = ["changed"]
    with pytest.raises(ValueError, match="collision"):
        await append_evaluations(db, **args)
    await db.rollback()
    assert len(await evidence(db)) == 1 and (await evidence(db))[0].payload_json == before
    assert (await evidence(db))[0].payload_hash == hashlib.sha256(before.encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["orm_update", "orm_delete", "bulk_update", "bulk_delete", "replace_id", "replace_key"])
async def test_immutable_guards_cover_orm_sql_and_sqlite_replace(tenbagger_session, kind):
    db = tenbagger_session
    await append_evaluations(db, capture_id="fixed", trade_date=AT.date(), captured_at=AT,
                             observations=[observation()])
    await db.commit()
    row = (await evidence(db))[0]
    before = row.payload_json
    with pytest.raises((ValueError, IntegrityError), match="append-only"):
        if kind == "orm_update":
            row.payload_json = "{}"
            await db.flush()
        elif kind == "orm_delete":
            await db.delete(row)
            await db.flush()
        elif kind == "bulk_update":
            await db.execute(update(Evidence).values(payload_json="{}"))
        elif kind == "bulk_delete":
            await db.execute(delete(Evidence))
        else:
            columns = ("id," if kind == "replace_id" else "") + "capture_id,record_id,trade_date,code,captured_at,protocol_version,payload_hash,payload_json"
            await db.execute(text("INSERT OR REPLACE INTO anomaly_candidate_evidence ("+columns+
                                  ") SELECT "+columns+" FROM anomaly_candidate_evidence"))
    await db.rollback()
    assert len(await evidence(db)) == 1 and (await evidence(db))[0].payload_json == before


def test_migration_only_appends_empty_schema_and_retains_history():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/032_anomaly_candidate_evidence.py"
    spec = importlib.util.spec_from_file_location("candidate_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE anomaly_candidate_record (record_id TEXT, snapshot_json TEXT)"))
        conn.execute(text("INSERT INTO anomaly_candidate_record VALUES ('legacy','unchanged')"))
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        migration.upgrade()
        assert conn.execute(text("SELECT * FROM anomaly_candidate_record")).all() == [("legacy", "unchanged")]
        assert conn.scalar(text("SELECT COUNT(*) FROM anomaly_candidate_evidence")) == 0
        triggers = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert triggers == {"anomaly_candidate_evidence_no_update", "anomaly_candidate_evidence_no_delete",
                            "anomaly_candidate_evidence_no_replace"}
        with pytest.raises(RuntimeError, match="retained"):
            migration.downgrade()
    engine.dispose()


@pytest.mark.asyncio
async def test_real_refresh_records_exact_stage_membership_without_extra_push(
    tenbagger_session, capture_clock, monkeypatch,
):
    from app.api.v1 import ws
    db = tenbagger_session
    items = [{**anomaly(), "code": f"60000{i}"} for i in range(1, 7)]
    messages = [message(item) for item in items[:5]]
    payload = {"anomalies": items, "b1_states": [], "snapshot_time": AT.isoformat()}
    monkeypatch.setattr(api, "_resolve_anomaly_trade_date", AsyncMock(return_value=AT.date()))
    monkeypatch.setattr(api, "_ensure_dynamic_trend_pool", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "_settle_anomaly_signal_performance", AsyncMock())
    monkeypatch.setattr(api, "_get_cached_anomaly_snapshot", lambda day: {"anomalies": [], "b1_states": []})
    monkeypatch.setattr(api, "prewarm_anomaly_snapshot", AsyncMock(return_value=payload))
    monkeypatch.setattr(api.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(ws.ws_manager, "push_anomaly", AsyncMock())
    monkeypatch.setattr(api, "_select_pushworthy_anomalies", lambda *a, **k: list(items))
    monkeypatch.setattr(api, "_select_unsent_push_recovery_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "_build_push_messages_for_anomalies", lambda *a, **k: messages)
    monkeypatch.setattr(api, "_build_early_observation_push_message", lambda *a, **k: None)
    monkeypatch.setattr(api, "_select_unsent_observation_recovery_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(api, "_filter_push_messages_by_performance", AsyncMock(return_value=messages[:4]))
    monkeypatch.setattr(api, "_cap_automatic_push_messages", lambda *a, **k: messages[:3])
    monkeypatch.setattr(api, "_cap_automatic_push_messages_by_persisted_budget", AsyncMock(return_value=messages[:2]))
    dispatch = AsyncMock(return_value=[{"sent": True}, {"sent": False, "throttled": True}])
    monkeypatch.setattr(api.push_scheduler, "push_batch", dispatch)
    ledger = AsyncMock()
    monkeypatch.setattr(api, "_record_sent_anomaly_signals", ledger)
    result = await api.refresh_and_push_anomaly_snapshot(db, AT.date())
    assert result["candidate_evidence_capture"]["status"] == "recorded"
    assert "candidate_evidence_capture" not in payload
    dispatch.assert_awaited_once_with(messages[:2])
    ledger.assert_awaited_once()
    rows = {row.code: json.loads(row.payload_json) for row in await evidence(db)}
    assert len(rows) == 6
    assert rows["600001"]["delivery"]["status"] == "reported_sent"
    assert rows["600002"]["delivery"]["status"] == "throttled"
    assert rows["600003"]["gate_trace"]["round_budget_passed"] is True
    assert rows["600003"]["gate_trace"]["persisted_budget_passed"] is False
    assert rows["600004"]["gate_trace"]["performance_filter_passed"] is True
    assert rows["600004"]["gate_trace"]["round_budget_passed"] is False
    assert rows["600005"]["gate_trace"]["technical_message_built"] is True
    assert rows["600005"]["gate_trace"]["performance_filter_passed"] is False
    assert rows["600006"]["gate_trace"]["strategy_selected"] is True
    assert rows["600006"]["gate_trace"]["technical_message_built"] is False
    assert all(rows[code]["delivery"]["status"] == "not_dispatched"
               for code in ("600003", "600004", "600005", "600006"))


def test_032_deployment_preflight_requires_both_evidence_tables_and_six_new_guards(tmp_path):
    from scripts.check_deployment_evidence import compare, TRIGGERS_BY_REVISION
    from deployment_fixtures import release_pair
    before, after = release_pair(tmp_path, "032_anomaly_candidate_evidence")
    assert compare(before, after, expected_revision="032_anomaly_candidate_evidence") == []
    assert "wrong schema revision" in compare(before, after)
    for trigger in TRIGGERS_BY_REVISION["032_anomaly_candidate_evidence"]:
        damaged = copy.deepcopy(after)
        damaged["triggers"].remove(trigger)
        damaged["trigger_definitions"].pop(trigger)
        assert "required append-only triggers missing" in compare(
            before, damaged, expected_revision="032_anomaly_candidate_evidence")
    for table in ("paper_sale_accounting", "anomaly_candidate_evidence"):
        damaged = copy.deepcopy(after)
        damaged["counts"][table] = 1
        damaged["core_digests"][table]["count"] = 1
        assert f"new table not empty: {table}" in compare(
            before, damaged, expected_revision="032_anomaly_candidate_evidence")
        del damaged["counts"][table]
        assert any("table missing" in error for error in compare(
            before, damaged, expected_revision="032_anomaly_candidate_evidence"))


@pytest.mark.parametrize("table", ["anomaly_candidate_record", "anomaly_candidate_evidence"])
def test_both_candidate_tables_are_content_audited_readonly(tmp_path, table):
    import sqlite3
    from scripts.audit_deployment_evidence import audit_database
    path = tmp_path / "isolated.db"
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload_json TEXT)")
        db.execute(f"INSERT INTO {table} VALUES (1, 'original')")
    before_bytes = path.read_bytes()
    before = audit_database(path)
    assert before["core_digests"][table]["count"] == 1
    assert path.read_bytes() == before_bytes
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE {table} SET payload_json='changed'")
    after = audit_database(path)
    assert before["counts"] == after["counts"]
    assert before["core_digests"] != after["core_digests"]


@pytest.mark.asyncio
async def test_parent_commit_failure_never_reports_recorded_evidence(tenbagger_session, capture_clock, monkeypatch):
    db = tenbagger_session
    async def fail():
        raise OperationalError("COMMIT fixture", {}, RuntimeError("unavailable"))
    with monkeypatch.context() as patch:
        patch.setattr(db, "commit", fail)
        result = await api._persist_anomaly_candidate_records(db, AT.date(), [anomaly()], evaluated=True)
    assert result["status"] == "unavailable"
    assert await evidence(db) == []
    assert (await db.scalars(select(Record))).all() == []
