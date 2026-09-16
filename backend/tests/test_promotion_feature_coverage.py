import copy
from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.promotion.modeling.feature_coverage import (
    HIST_MATERIALIZATION_VERSION, hist_values_digest, hist_feature_coverage,
)
from app.promotion.modeling.features import (
    FEATURE_VERSION, FeatureRow, FeatureVectorizer, extract_point_in_time_features,
)
from app.promotion.modeling.inference import LoadedPromotionArtifact

CUTOFF = datetime(2026, 9, 8, 20)
NAME = "hist_return_1d"


def proven_row(value=0.0):
    values = {NAME: value, "route_score": 2.0}
    clock = CUTOFF.isoformat()
    proof = {
        "schema_version": HIST_MATERIALIZATION_VERSION, "feature_version": FEATURE_VERSION,
        "code": "600001", "prediction_trade_date": "2026-09-08",
        "as_of_at": clock, "materialized_at": clock, "values_sha256": hist_values_digest(values),
        "fields": {NAME: {"status": "ok", "source": "fixture_only", "source_version": "v1",
                           "source_manifest_sha256": "a" * 64, "source_at": clock,
                           "received_at": clock, "observed_at": clock}},
    }
    return FeatureRow("600001", "2026-09-08", 1, 0, 0.1, "fixture", values,
                      hist_materialization=proof, feature_as_of_at=CUTOFF)


def loaded(names=(NAME,)):
    n = len(names)
    return LoadedPromotionArtifact(
        model_version="fixture", target_board=1, feature_version=FEATURE_VERSION,
        data_version="fixture", artifact_path="", artifact_sha256="a" * 64,
        numeric_names=tuple(names), route_categories=(), regime_categories=(),
        mean=np.ones(n), scale=np.ones(n), coefficients=np.ones(n),
        classifier_intercept=0.0, calibration_slope=1.0, calibration_intercept=0.0,
        fit_end_date="2026-09-01", calibration_end_date="2026-09-02",
        training_config={}, offline_acceptance={},
    )


def test_true_zero_with_bound_materialization_is_usable():
    row = proven_row(0.0)
    assert loaded().transform([row])[0, 0] == -1.0
    raw, calibrated = loaded().predict([row])
    assert np.isfinite(raw).all() and np.isfinite(calibrated).all()
    assert FEATURE_VERSION == "promotion_features_v3_regime_point_in_time"


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -float("inf"), True, "0"])
def test_hist_invalid_not_silently_zero(value):
    row = proven_row()
    values = extract_point_in_time_features({NAME: value, "route_score": None})
    assert values["route_score"] == 0.0
    row = replace(row, values=values)
    with pytest.raises(ValueError, match="missing_or_invalid_hist_values"):
        loaded().predict([row])


def test_missing_key_and_missing_proof_fail_closed():
    for row in [replace(proven_row(), values={}), replace(proven_row(), hist_materialization=None),
                replace(proven_row(), feature_as_of_at=None)]:
        with pytest.raises(ValueError, match="hist feature coverage"):
            loaded().predict([row])


@pytest.mark.parametrize("mutation", [
    "version", "feature_version", "code", "trade_date", "hash", "missing_field",
    "unknown_source", "future", "reversed", "missing_source_clock", "bad_date_only", "different_cutoff",
])
def test_materialization_proof_rejected(mutation):
    row = proven_row()
    proof = copy.deepcopy(row.hist_materialization)
    field = proof["fields"][NAME]
    if mutation == "version":
        proof["schema_version"] = "other"
    elif mutation == "feature_version":
        proof["feature_version"] = "other"
    elif mutation == "code":
        proof["code"] = "600002"
    elif mutation == "trade_date":
        proof["prediction_trade_date"] = "2026-09-07"
    elif mutation == "hash":
        proof["values_sha256"] = "b" * 64
    elif mutation == "missing_field":
        proof["fields"] = {}
    elif mutation == "unknown_source":
        field["status"] = "unknown"
    elif mutation == "future":
        proof["materialized_at"] = (CUTOFF + timedelta(seconds=1)).isoformat()
    elif mutation == "reversed":
        field["received_at"] = (CUTOFF - timedelta(seconds=1)).isoformat()
    elif mutation == "missing_source_clock":
        field["source_at"] = None
    elif mutation == "bad_date_only":
        field["observed_at"] = "2026-09-08"
    elif mutation == "different_cutoff":
        proof["as_of_at"] = (CUTOFF - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        loaded().predict([replace(row, hist_materialization=proof)])


def test_vectorizer_fit_and_transform_guard_both_and_nonhist_unchanged():
    a, b = proven_row(0), proven_row(2)
    v = FeatureVectorizer().fit([a, b])
    assert v.numeric_names == [NAME]
    assert np.array_equal(v.transform([a, b]), np.array([[-1.0], [1.0]]))
    with pytest.raises(ValueError):
        v.transform([replace(a, values={})])
    with pytest.raises(ValueError):
        FeatureVectorizer().fit([a, replace(b, hist_materialization=None)])
    # No hist-required keys: unchanged legacy optional imputation even if extra
    # unavailable hist fields happen to be present in an unused factor bundle.
    legacy = replace(a, values={"route_score": 0, NAME: None}, hist_materialization=None)
    assert loaded(("route_score",)).transform([legacy])[0, 0] == -1.0


def test_explicit_historical_pretraining_not_materialization_or_loaded_inference():
    rows = [replace(proven_row(v), hist_materialization=None) for v in (0, 2)]
    with pytest.raises(ValueError):
        FeatureVectorizer().fit(rows)
    research = FeatureVectorizer(allow_unmaterialized_pretraining=True).fit(rows)
    assert np.isfinite(research.transform(rows)).all()
    with pytest.raises(ValueError):
        research.transform([replace(rows[0], values={})])
    # Even an artifact declaring historical training cannot bypass loaded scoring.
    artifact = loaded()
    artifact.training_config["dataset_source"] = "historical_panel"
    with pytest.raises(ValueError):
        artifact.predict(rows)


@pytest.mark.asyncio
async def test_real_shadow_path_passes_proof_and_rejects_missing(tmp_path, monkeypatch):
    import json
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from app.db.session import Base
    from app.models.promotion import PromotionModelArtifact, PromotionPredictionRun, PromotionPredictionSnapshot
    from app.promotion import shadow

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'shadow_coverage.db'}")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            row = proven_row(0)
            artifact = loaded()
            artifact.offline_acceptance.update({"passed": True, "decision": "shadow_eligible"})
            artifact.training_config.update({"dataset_source": "prediction_snapshots", "snapshot_context": "promotion_2000"})
            monkeypatch.setattr(shadow, "load_promotion_artifact", lambda *a, **kw: artifact)
            db.add(PromotionModelArtifact(id=1, model_version="fixture", feature_version=FEATURE_VERSION,
                   data_version="fixture", status="shadow_eligible", artifact_uri="fixture-only"))
            db.add(PromotionPredictionRun(id=1, run_key="fixture", snapshot_batch_key="fixture",
                   reference_trade_date=CUTOFF.date(), as_of_at=CUTOFF, created_at=CUTOFF,
                   completed_at=CUTOFF + timedelta(seconds=1), snapshot_source="schedule",
                   snapshot_context="promotion_2000", model_version="champion",
                   feature_version=FEATURE_VERSION, data_version="fixture", status="completed",
                   gate_passed=True, candidate_count=1, ranked_count=0, actionable_count=0,
                   payload_hash="a" * 64, metadata_json="{}"))
            factors = {**row.values, "hist_materialization": row.hist_materialization, "market_regime": "recovery"}
            db.add(PromotionPredictionSnapshot(id=1, run_id=1, record_key="fixture", code=row.code,
                   target_board=1, prediction_trade_date=CUTOFF.date(), candidate_route="fixture",
                   calibrated_probability=0.1, rank_scope="pool_unranked", created_at=CUTOFF,
                   features_json=json.dumps(factors), reason_json="{}"))
            await db.commit()
            result = await shadow.run_shadow_inference(db, prediction_run_id=1, artifact_id=1, persist=False)
            assert result["candidate_count"] == 1 and result["persisted"] is False
            factors.pop("hist_materialization")
            await db.execute(text("UPDATE promotion_prediction_snapshot SET features_json=:p WHERE id=1"),
                             {"p": json.dumps(factors)})
            await db.commit()
            db.expire_all()
            with pytest.raises(ValueError, match="hist feature coverage"):
                await shadow.run_shadow_inference(db, prediction_run_id=1, artifact_id=1, persist=False)
    finally:
        await engine.dispose()
