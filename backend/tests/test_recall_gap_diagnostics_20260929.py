"""Isolated SQLite evidence fixtures; never touch the deployment database."""
import json
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.promotion.ledger import _candidate_identity, _hash
from scripts import analyze_recall_gap as cli

DAY = date(2026, 9, 22)
OUTCOME = date(2026, 9, 23)
AS_OF = datetime(2026, 9, 23, 21)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "fixture.db"
    engine = create_engine(f"sqlite:///{path}")
    PromotionPredictionRun.__table__.create(engine)
    PromotionPredictionSnapshot.__table__.create(engine)
    engine.dispose()
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE trade_calendar (trade_date TEXT, is_trade_day INTEGER)")
        c.executemany("INSERT INTO trade_calendar VALUES (?,1)", [(str(DAY),), (str(OUTCOME),)])
        c.execute("CREATE TABLE limit_up_pool (trade_date TEXT, code TEXT, name TEXT,"
                  "consecutive_days INTEGER, quarantined INTEGER)")
    return path


def truth(path, rows):
    with sqlite3.connect(path) as c:
        c.executemany("INSERT INTO limit_up_pool VALUES (?,?,?,?,0)",
                      [(str(OUTCOME), *r) for r in rows])


def batch(path, *, run_id=1, context="promotion_2000", status="completed",
          candidates=None, partial=False, hour=20):
    candidates = candidates if candidates is not None else [("600001", 1, False, True)]
    snapshots, identities = [], []
    clock = datetime(2026, 9, 22, hour, run_id)
    for index, (code, target, selected, eligible) in enumerate(candidates, 1):
        factors = {"prediction_pool_rank": index,
                   "prediction_rank_contract_version": "promotion_rank_contract_v1",
                   "prediction_rank_contract_complete": True,
                   "prediction_rank_eligible": eligible,
                   "prediction_record_scope": "ranked" if selected else "pool_unranked",
                   "prediction_ranked_position": index if selected else 0}
        if selected is not None:
            factors["prediction_ranked_selected"] = selected
        identity = _candidate_identity({"code": code, "target_board": target,
            "candidate_route": "quiet_setup", "raw_probability": .2,
            "probability": .2, "probability_factors": factors}, DAY)
        identities.append(identity)
        snapshots.append(dict(
            run_id=run_id, record_key=_hash(identity), code=code, name="示例", target_board=target,
            prediction_trade_date=DAY, horizon_days=1, candidate_route="quiet_setup",
            rank_scope=identity["rank_scope"], pool_rank=index, rank_position=identity["rank_position"],
            recall_rank_position=0, raw_probability=.2, calibrated_probability=.2,
            features_json=json.dumps(factors), created_at=clock))
    identities.sort(key=lambda i: (i["target_board"], i["prediction_trade_date"], i["code"], i["candidate_route"]))
    payload = _hash(identities)
    keys = [f"fixture-{run_id}-{context}"]
    components = {str(target): {"model_versions": ["m"], "feature_versions": ["f"],
                               "data_versions": ["d"]} for _, target, _, _ in candidates}
    run_key = _hash(dict(model_version="m", feature_version="f", data_version="d",
                         source="schedule", context=context, batch_keys=keys, payload_hash=payload))
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as c:
        c.execute(PromotionPredictionRun.__table__.insert(), dict(
            id=run_id, run_key=run_key, snapshot_batch_key="|".join(keys),
            reference_trade_date=DAY, as_of_at=clock, snapshot_source="schedule",
            snapshot_context=context, model_version="m", feature_version="f", data_version="d",
            status=status, gate_passed=True, candidate_count=len(snapshots) + int(partial),
            payload_hash=payload, metadata_json=json.dumps(dict(batch_keys=keys,
            model_components_by_target=components)), created_at=clock, completed_at=clock))
        if snapshots:
            c.execute(PromotionPredictionSnapshot.__table__.insert(), snapshots)
    engine.dispose()


def analyze(path, **kwargs):
    with cli.readonly_database(path) as c:
        return cli.analyze(c, as_of=AS_OF, start_date=OUTCOME, end_date=OUTCOME, **kwargs)


def test_full_pool_is_not_main_rank(database):
    batch(database, candidates=[("600001", 1, False, True), ("600002", 1, True, True),
                                ("600003", 1, False, False), ("600004", 1, None, True)])
    truth(database, [(f"60000{i}", "示例", 1) for i in range(1, 6)])
    report = analyze(database)
    assert report["daily"][0]["batch_error"] is None
    assert report["counts"] == dict(ranking_miss=1, hit=1, filtered=1, unknown=1, recall_miss=1)
    assert report["original_observed_denominator"] == 5
    assert report["certified"] is False and report["certified_recall"] is None


@pytest.mark.parametrize("failure", ["missing", "failed", "partial", "future", "hash"])
def test_bad_latest_never_falls_back(database, failure):
    truth(database, [("600009", "示例", 1)])
    if failure != "missing":
        batch(database)
        batch(database, run_id=2, status="failed" if failure == "failed" else "completed",
              partial=failure == "partial")
        with sqlite3.connect(database) as c:
            if failure == "future":
                c.execute("UPDATE promotion_prediction_run SET completed_at='2026-09-24 20:00:00' WHERE id=2")
            if failure == "hash":
                c.execute("UPDATE promotion_prediction_snapshot SET features_json='{}' WHERE run_id=2")
    report = analyze(database)
    assert report["counts"] == {"snapshot_incomplete": 1}
    assert report["daily"][0]["run_id"] == (None if failure == "missing" else 2)


def test_context_isolation(database):
    truth(database, [("600001", "示例", 1)])
    batch(database)
    batch(database, run_id=2, context="promotion_1510", hour=15,
          candidates=[("600001", 1, True, True)])
    batch(database, run_id=3, context="promotion_1000", hour=10, status="failed")
    assert analyze(database)["counts"] == {"ranking_miss": 1}
    assert analyze(database, context="promotion_1510")["counts"] == {"hit": 1}


def test_board_st_unknown_boundaries(database):
    batch(database, candidates=[("600001", 2, True, True)])
    truth(database, [("600001", "普通", 2), ("600002", "普通", 3),
                     ("600003", "普通", None), ("600004", "*ST例", 1),
                     ("300001", "创业", 1), ("688001", "科创", 1), ("920001", "北交", 1),
                     ("999999", "未知", 1)])
    report = analyze(database)
    day = report["daily"][0]
    assert day["raw_truth_row_count"] == 8 and day["original_observed_denominator"] == 1
    assert day["counts"] == {"hit": 1}
    assert day["exclusion_reason_counts"] == dict(outside_first_second=1, unknown_board_count=1,
                                                   st_or_delisting=1, non_main_or_unknown_board=4)
    assert next(r for r in day["observations"] if r["code"] == "600003")["consecutive_days"] is None


def test_physical_readonly_transaction_and_outputs(database, tmp_path):
    batch(database)
    truth(database, [("600001", "普通", 1)])
    before = database.read_bytes()
    with cli.readonly_database(database) as c:
        assert c.in_transaction
        c.execute("PRAGMA query_only=OFF")  # URI still enforces physical readonly.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("DELETE FROM limit_up_pool")
    output = tmp_path / "reports"
    assert cli.main(["--database", str(database), "--as-of", AS_OF.isoformat(),
                     "--start-date", str(OUTCOME), "--end-date", str(OUTCOME),
                     "--outdir", str(output)]) == 0
    assert database.read_bytes() == before
    report = json.loads((output / f"recall_gap_{OUTCOME}.json").read_text())
    assert report["original_observed_denominator"] == 1
    assert "未认证" in (output / f"recall_gap_{OUTCOME}.md").read_text()


def test_missing_truth_day_kept_and_calendar_gap_not_skipped(database):
    batch(database)
    report = analyze(database)
    assert len(report["daily"]) == 1
    assert report["daily"][0]["raw_truth_row_count"] == 0
    assert report["certified_recall"] is None
    with sqlite3.connect(database) as c:
        c.execute("DELETE FROM trade_calendar WHERE trade_date=?", (str(DAY),))
    truth(database, [("600001", "普通", 1)])
    assert analyze(database)["counts"] == {"snapshot_incomplete": 1}


@pytest.mark.parametrize("present,absent", [(1, 2), (2, 1)])
def test_missing_entire_lane_is_not_recall_miss(database, present, absent):
    batch(database, candidates=[("600001", present, True, True)])
    truth(database, [("600009", "普通", absent), ("600001", "普通", present)])
    report = analyze(database)
    assert report["daily"][0]["batch_error"] is None
    assert report["counts"] == {"snapshot_incomplete": 1, "hit": 1}
    assert report["daily"][0]["lane_errors"][str(absent)] == "target_lane_missing_or_empty_unproven"
    assert report["historical_label_availability_verified"] is False
    assert report["as_of_semantics"] == "validation_cutoff_not_historical_first_knowledge"


def test_incomplete_rank_metadata_not_selected_by_default():
    snapshot = SimpleNamespace(features_json="{}", rank_scope="pool_unranked", rank_position=None)
    assert cli.classify(snapshot, None) == ("unknown", "rank_contract_missing")
