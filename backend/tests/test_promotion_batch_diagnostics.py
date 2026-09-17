"""Expected-window diagnostics use isolated SQLite and SELECT-only paths."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import promotion
from app.core.prediction_data_quality import PREDICTION_ROUTE_REQUIRED_DATASETS
from app.db.session import Base, get_db
from app.models.governance import TradeCalendarModel
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.promotion.batch_diagnostics import build_batch_diagnostics, load_batch_diagnostics
from app.promotion.versioning import get_promotion_model_identity

DAY = date(2026, 9, 14)
NOW = datetime(2026, 9, 14, 18, 44)
WINDOWS = {"promotion_1510": promotion._PROMOTION_OFFICIAL_CONTEXT_WINDOWS["promotion_1510"]}
ROUTES = tuple(PREDICTION_ROUTE_REQUIRED_DATASETS)


def identity():
    value = get_promotion_model_identity()
    return dict(model_version=value.active_model_version, feature_version=value.feature_version,
                data_version=value.data_version, runtime_mode=value.runtime_mode.value)


def attempt(id=1, **changes):
    row = dict(
        id=id, run_key=f"run-{id}", snapshot_batch_key=f"schedule:promotion_1510:{id}",
        reference_trade_date=DAY, as_of_at=datetime(2026, 9, 14, 15, 10, id),
        created_at=datetime(2026, 9, 14, 15, 10, id),
        completed_at=datetime(2026, 9, 14, 15, 11, id),
        snapshot_source="schedule", snapshot_context="promotion_1510",
        **identity(), status="completed", gate_passed=True,
        candidate_count=1, ranked_count=1, actionable_count=0,
        metadata_json=json.dumps({
            "trade_date_by_target": {"1": str(DAY)},
            "quality_gate": {"route_gates": {r: {"gate_passed": True} for r in ROUTES}},
        }),
    )
    return {**row, **changes}


def aggregate(id=1, **changes):
    return dict(run_id=id, target_board=1, prediction_trade_date=DAY,
                candidate_route="mainline_spread_start", snapshot_count=1,
                ranked_count=1, actionable_count=0, **changes)


def report(attempts=(), aggregates=(), **changes):
    args = dict(trade_date=DAY, checked_at=NOW, calendar_is_trade_day=True,
                expected_identity=identity(), context_windows=WINDOWS,
                required_routes=ROUTES, attempts=attempts, snapshot_aggregates=aggregates,
                canonical_close_contexts=promotion.PROMOTION_CANONICAL_CLOSE_CONTEXTS)
    return build_batch_diagnostics(**{**args, **changes})


def window(result):
    return next(x for x in result["contexts"] if x["snapshot_context"] == "promotion_1510")


@pytest.mark.parametrize(("clock", "state", "phase"), [
    (datetime(2026, 9, 14, 15, 4, 59), "not_due", "not_due"),
    (datetime(2026, 9, 14, 15, 5), "awaiting_persisted_attempt", "open"),
    (datetime(2026, 9, 14, 16, 30, 59, 999999), "awaiting_persisted_attempt", "open"),
    (datetime(2026, 9, 14, 16, 31), "missing_persisted_attempt", "expired"),
    (NOW, "missing_persisted_attempt", "expired"),
])
def test_missing_attempt_window_boundaries(clock, state, phase):
    result = report(checked_at=clock)
    w = window(result)
    assert (w["status"], w["window_phase"]) == (state, phase)
    assert w["attempt_count"] == 0 and w["latest_attempt"] is None
    assert w["missing_attempt_cause"] == "unknown"
    assert w["automatic_catchup_allowed"] is False
    assert result["process_absence_proven"] is False


@pytest.mark.parametrize(("calendar", "state"), [
    (None, "calendar_unknown"), (False, "not_expected_closed_session"),
])
def test_calendar_unknown_or_closed_is_not_a_missing_trading_batch(calendar, state):
    assert window(report(calendar_is_trade_day=calendar))["status"] == state


def test_later_success_does_not_satisfy_other_expected_context():
    row = attempt(snapshot_context="promotion_2000")
    assert window(report([row], [aggregate()]))["status"] == "missing_persisted_attempt"


def test_created_today_or_candidate_date_yesterday_does_not_replace_attempt_session():
    old = attempt(reference_trade_date=DAY - timedelta(days=3),
                  as_of_at=datetime(2026, 9, 11, 15, 10))
    assert window(report([old]))["attempt_count"] == 0
    intraday = attempt(snapshot_context="promotion_0935", reference_trade_date=DAY-timedelta(days=3),
        as_of_at=datetime(2026, 9, 14, 9, 35), created_at=datetime(2026, 9, 14, 9, 35),
        completed_at=datetime(2026, 9, 14, 9, 36))
    result = report([intraday], context_windows={"promotion_0935": ((9, 30), (9, 50))})
    assert result["contexts"][0]["attempt_count"] == 1
    assert result["contexts"][0]["latest_attempt"]["reference_trade_date"] == "2026-09-11"


def test_complete_record_and_routes_do_not_authorize_execution():
    result = report([attempt()], [aggregate()])
    w = window(result)
    assert w["status"] == "completed"
    assert w["latest_attempt"]["targets"][1]["status"] == "absent_unproven"
    assert w["latest_attempt"]["routes"][0]["execution_eligibility"] == "not_evaluated"
    assert result["candidate_or_execution_authorization"] is False
    assert result["historical_point_in_time_certified"] is False


@pytest.mark.parametrize("reason", ["generation_pending", "empty_unproven", "fully_filtered", "invalid_candidate_identity"])
def test_latest_blocker_does_not_fall_back_to_good_run(reason):
    blocked = attempt(2, status="blocked", candidate_count=0, ranked_count=0,
        gate_passed=False, metadata_json=json.dumps({"persistence": {"status": reason, "input_count": None}}))
    w = window(report([attempt(), blocked], [aggregate()]))
    assert w["latest_attempt"]["id"] == 2
    assert w["latest_attempt"]["input_count"] is None
    assert w["status"] == ("generation_pending_unresolved" if reason == "generation_pending" else "persisted_blocked")
    assert w["fallback_to_older_run"] is False and w["attempt_count"] == 2


def test_later_completed_does_not_erase_prior_generation_pending():
    pending = attempt(status="blocked", candidate_count=0, ranked_count=0, gate_passed=False,
        metadata_json='{"persistence":{"status":"generation_pending","input_count":null}}')
    w = window(report([pending, attempt(2)], [aggregate(2)]))
    assert w["status"] == "completed"
    assert w["attempts"][0]["diagnostic_status"] == "generation_pending_unresolved"


@pytest.mark.parametrize("field", ["model_version", "feature_version", "data_version", "runtime_mode"])
def test_latest_foreign_model_identity_is_not_filtered_to_older_good(field):
    newer = attempt(2, **{field: "other"})
    w = window(report([attempt(), newer], [aggregate(), aggregate(2)]))
    assert w["latest_attempt"]["id"] == 2
    assert w["status"] == "invalid_persisted_attempt"
    assert "model_identity_mismatch" in w["latest_attempt"]["issues"]


@pytest.mark.parametrize("changes,issue", [
    ({"as_of_at": datetime(2026, 9, 14, 17)}, "attempt_outside_declared_session_window"),
    ({"created_at": datetime(2026, 9, 14, 15, 9)}, "invalid_attempt_clock_order"),
    ({"completed_at": datetime(2026, 9, 14, 19)}, "completion_not_visible_at_cutoff"),
    ({"completed_at": None}, "invalid_attempt_clock_order"),
    ({"as_of_at": datetime(2026, 9, 14, 15, 10, tzinfo=timezone.utc)}, "attempt_outside_declared_session_window"),
    ({"candidate_count": 2}, "snapshot_count_mismatch"),
    ({"ranked_count": 0}, "ranked_count_mismatch"),
    ({"actionable_count": 1}, "actionable_count_mismatch"),
])
def test_bad_clock_or_counts_remain_invalid(changes, issue):
    w = window(report([attempt(**changes)], [aggregate()]))
    assert w["status"] == "invalid_persisted_attempt"
    assert issue in w["latest_attempt"]["issues"]


def test_success_but_route_blocked_is_not_missing_or_global_success():
    row = attempt()
    meta = json.loads(row["metadata_json"])
    meta["quality_gate"]["route_gates"]["auction_surge_start"] = {
        "gate_passed": False, "blocking_datasets": ["auction_data"]}
    row["metadata_json"] = json.dumps(meta)
    w = window(report([row], [aggregate()]))
    assert w["status"] == "completed_route_blocked"
    assert w["latest_attempt"]["batch_gate_passed"] is True
    route = next(r for r in w["latest_attempt"]["routes"] if r["route"] == "auction_surge_start")
    assert route["blocking_datasets"] == ["auction_data"]


@pytest.mark.parametrize("metadata", ['{}', 'not-json', '[]', '{"quality_gate":{"route_gates":null}}'])
def test_absent_or_bad_route_evidence_does_not_infer_from_global_ok(metadata):
    w = window(report([attempt(metadata_json=metadata, candidate_count=0, ranked_count=0)]))
    assert w["status"] == "completed_empty_unproven"
    assert all(r["gate_status"] == "unknown" for r in w["latest_attempt"]["routes"])


def test_absent_route_map_with_valid_nonempty_snapshot_reports_unknown():
    meta = json.dumps({"trade_date_by_target": {"1": str(DAY)}})
    w = window(report([attempt(metadata_json=meta)], [aggregate()]))
    assert w["status"] == "completed_route_gate_unknown"



@pytest.mark.parametrize("route", [None, "", "   ", 123, ["bad"], "legacy_unknown"])
def test_missing_or_unknown_route_never_authorized_by_empty_or_foreign_gate(route):
    row = attempt(candidate_count=2, ranked_count=2)
    meta = json.loads(row["metadata_json"])
    meta["quality_gate"]["route_gates"].update({"": {"gate_passed": True},
                                               "legacy_unknown": {"gate_passed": True}})
    row["metadata_json"] = json.dumps(meta)
    group = aggregate()
    group["candidate_route"] = route
    result = report([row], [aggregate(), group])
    latest = window(result)["latest_attempt"]
    assert latest["diagnostic_status"] == "invalid_persisted_attempt"
    unknown = [r for r in latest["routes"] if not r["declared_in_quality_contract"]]
    assert len(unknown) == 1
    assert unknown[0]["gate_status"] == "unknown"
    assert unknown[0]["gate_passed"] is None
    assert unknown[0]["snapshot_count"] == 1
    assert latest["persisted_snapshot_count"] == 2
    assert result["candidate_or_execution_authorization"] is False


@pytest.mark.parametrize("bad_date", [None, "", 123, [], {}, "garbage",
                                      "2026-02-30", datetime(2026, 9, 14)])
def test_mixed_unknown_persisted_target_dates_are_invalid_not_sort_errors(bad_date):
    group = aggregate()
    group["prediction_trade_date"] = bad_date
    latest = window(report([attempt(candidate_count=2, ranked_count=2)],
                           [aggregate(), group]))["latest_attempt"]
    assert latest["diagnostic_status"] == "invalid_persisted_attempt"
    assert "invalid_persisted_target_date" in latest["issues"]
    assert latest["targets"][0]["snapshot_count"] == 2
    assert latest["targets"][0]["persisted_prediction_trade_dates"] == [str(DAY)]


@pytest.mark.parametrize("bad_date", [None, "", 123, [], {}, "garbage"])
def test_unknown_declared_target_date_is_invalid(bad_date):
    row = attempt()
    meta = json.loads(row["metadata_json"])
    meta["trade_date_by_target"]["1"] = bad_date
    row["metadata_json"] = json.dumps(meta)
    latest = window(report([row], [aggregate()]))["latest_attempt"]
    assert "invalid_declared_target_date" in latest["issues"]
    assert latest["diagnostic_status"] == "invalid_persisted_attempt"


def test_target_date_not_conflated_with_run_reference():
    group = aggregate()
    group["prediction_trade_date"] = DAY + timedelta(days=1)
    w = window(report([attempt()], [group]))
    assert "target_date_contract_mismatch" in w["latest_attempt"]["issues"]
    assert "future_prediction_target_date" in w["latest_attempt"]["issues"]


def test_inputs_immutable_and_page_rows_cannot_fill_window():
    a = [attempt(snapshot_source="page")]
    before = deepcopy(a)
    assert window(report(a, [aggregate()]))["status"] == "missing_persisted_attempt"
    assert a == before


@pytest.mark.parametrize("clock", ["2026-09-14T18:44:00", NOW.replace(tzinfo=timezone.utc), None])
def test_bad_request_clock_is_rejected(clock):
    with pytest.raises(ValueError):
        report(checked_at=clock)


def test_close_attempt_cannot_satisfy_today_with_self_consistent_old_target_dates():
    yesterday = DAY - timedelta(days=3)
    row = attempt(reference_trade_date=yesterday)
    meta = json.loads(row["metadata_json"])
    meta["trade_date_by_target"] = {"1": str(yesterday)}
    row["metadata_json"] = json.dumps(meta)
    group = aggregate()
    group["prediction_trade_date"] = yesterday
    w = window(report([row], [group]))
    assert w["status"] == "invalid_persisted_attempt"
    assert "close_reference_date_mismatch" in w["latest_attempt"]["issues"]
    assert "close_target_date_mismatch" in w["latest_attempt"]["issues"]


def test_unsupported_context_is_reported_without_filling_expected_window():
    row = attempt(snapshot_context="promotion_unknown")
    result = report([row], [aggregate()])
    assert result["unsupported_context_attempt_ids"] == [1]
    assert window(result)["status"] == "missing_persisted_attempt"


def test_invalid_target_board_remains_in_snapshot_denominator_and_blocks():
    group = aggregate()
    group["target_board"] = 3
    latest = window(report([attempt()], [group]))["latest_attempt"]
    assert latest["persisted_snapshot_count"] == 1
    assert "invalid_target_board" in latest["issues"]


def test_batch_false_and_route_true_remain_separate_facts():
    latest = window(report([attempt(gate_passed=False)], [aggregate()]))["latest_attempt"]
    assert latest["diagnostic_status"] == "completed_batch_gate_not_passed"
    assert all(r["gate_status"] == "passed" for r in latest["routes"])
    assert latest["batch_gate_passed"] is False


def test_unmapped_observed_route_has_no_invented_gate():
    group = aggregate()
    group["candidate_route"] = "news_catalyst_start"
    row = attempt()
    meta = json.loads(row["metadata_json"])
    # Freeze an actually missing historical gate; the default helper now has
    # all current routes and must not fabricate a historical absence.
    meta["quality_gate"]["route_gates"].pop("news_catalyst_start")
    row["metadata_json"] = json.dumps(meta)
    latest = window(report([row], [group]))["latest_attempt"]
    route = next(r for r in latest["routes"] if r["route"] == "news_catalyst_start")
    assert route["gate_passed"] is None
    assert route["declared_in_quality_contract"] is False


@pytest_asyncio.fixture
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'batch-health.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield engine, maker
    await engine.dispose()


async def seed(maker):
    row = attempt()
    async with maker() as db:
        db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
        db.add(PromotionPredictionRun(**row, payload_hash="test"))
        db.add(PromotionPredictionSnapshot(
            id=1, run_id=1, record_key="one", code="600001", target_board=1,
            prediction_trade_date=DAY, horizon_days=1, candidate_route="mainline_spread_start",
            rank_scope="ranked", rank_position=1, actionable=False, trade_gate_passed=True,
            watch_only=False, raw_probability=.1, calibrated_probability=.1,
            reason_json="{}", features_json="{}", created_at=NOW))
        await db.commit()


@pytest.mark.asyncio
async def test_loader_runs_query_only_and_does_not_autoflush(database):
    engine, maker = database
    await seed(maker)
    statements = []
    def trace(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())
    event.listen(engine.sync_engine, "before_cursor_execute", trace)
    async with maker() as db:
        await db.execute(text("PRAGMA query_only=ON"))
        db.add(TradeCalendarModel(trade_date=DAY + timedelta(days=1), is_trade_day=True))
        result = await load_batch_diagnostics(db, trade_date=DAY, checked_at=NOW,
            expected_identity=identity(), context_windows=WINDOWS, required_routes=ROUTES)
        assert window(result)["status"] == "completed"
        assert len(db.new) == 1
    assert set(statements) <= {"PRAGMA", "SELECT"}


@pytest.mark.asyncio
@pytest.mark.parametrize("column,value,state", [
    ("candidate_route", "", "invalid_persisted_attempt"),
    ("candidate_route", "unknown_legacy", "invalid_persisted_attempt"),
    ("prediction_trade_date", "not-a-date", "evidence_unavailable"),
    ("prediction_trade_date", 123, "evidence_unavailable"),
])
async def test_legacy_invalid_values_are_read_only_unknown(database, column, value, state):
    _, maker = database
    await seed(maker)
    # Corrupt only the disposable fixture, then enable query_only before diagnosis.
    async with maker() as db:
        await db.execute(text(f"UPDATE promotion_prediction_snapshot SET {column}=:value"),
                         {"value": value})
        await db.commit()
        await db.execute(text("PRAGMA query_only=ON"))
        result = await load_batch_diagnostics(db, trade_date=DAY, checked_at=NOW,
            expected_identity=identity(), context_windows=WINDOWS, required_routes=ROUTES)
        assert window(result)["status"] == state
        assert result["candidate_or_execution_authorization"] is False


@pytest.mark.asyncio
async def test_unavailable_schema_is_unknown_without_create_tables(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'absent.db'}")
    try:
        async with async_sessionmaker(engine)() as db:
            await db.execute(text("PRAGMA query_only=ON"))
            result = await load_batch_diagnostics(db, trade_date=DAY, checked_at=NOW,
                expected_identity=identity(), context_windows=WINDOWS, required_routes=ROUTES)
            assert result["evidence_available"] is False
            assert window(result)["status"] == "evidence_unavailable"
            assert not (await db.execute(text("select name from sqlite_master where type='table'"))).all()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_attempt_cap_is_not_a_partial_success(database, monkeypatch):
    from app.promotion import batch_diagnostics
    _, maker = database
    await seed(maker)
    monkeypatch.setattr(batch_diagnostics, "MAX_ATTEMPTS", 0)
    async with maker() as db:
        result = await load_batch_diagnostics(db, trade_date=DAY, checked_at=NOW,
            expected_identity=identity(), context_windows=WINDOWS, required_routes=ROUTES)
    assert result["evidence_error"] == "attempt_limit_exceeded_not_a_complete_denominator"


@pytest.mark.asyncio
async def test_read_only_endpoint_avoids_generation_storage_calendar_network(database, monkeypatch):
    _, maker = database
    await seed(maker)
    def forbidden(*args, **kwargs):
        raise AssertionError("batch-health must not generate or fetch data")
    monkeypatch.setattr(promotion, "promotion_candidates", forbidden)
    monkeypatch.setattr(promotion.trade_calendar, "is_trade_day", forbidden)
    from app.promotion import ledger
    monkeypatch.setattr(ledger, "ensure_prediction_ledger_storage", forbidden)
    app = FastAPI()
    app.include_router(promotion.router, prefix="/api/v1/promotion")
    async def readonly_db():
        async with maker() as db:
            await db.execute(text("PRAGMA query_only=ON"))
            yield db
    app.dependency_overrides[get_db] = readonly_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/promotion/batch-health?trade_date=2026-09-14")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "persisted_formal_attempts_read_only"
    # 上下文清单必须与官方窗口表同源，新增/删除时点不应再改这个数字。
    assert {c["snapshot_context"] for c in payload["contexts"]} == set(
        promotion._PROMOTION_OFFICIAL_CONTEXT_WINDOWS
    )
    assert window(payload)["attempt_count"] == 1


@pytest.mark.asyncio
async def test_read_only_loader_overrides_known_holiday_without_fetch(database):
    _, maker = database
    await seed(maker)
    async with maker() as db:
        result = await load_batch_diagnostics(db, trade_date=DAY, checked_at=NOW,
            expected_identity=identity(), context_windows=WINDOWS,
            required_routes=ROUTES, officially_closed=True)
    assert window(result)["status"] == "not_expected_closed_session"
