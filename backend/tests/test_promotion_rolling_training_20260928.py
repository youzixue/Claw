"""Isolated rolling-window mechanics, not evidence of real trading accuracy."""
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest
from sqlalchemy import event, text

from app.promotion.modeling.features import FeatureRow
from app.promotion.modeling.walk_forward import (
    fit_temporally_calibrated_model, training_window_rows, walk_forward_evaluate,
)
from test_promotion_ledger_dataset import env, seed, truth, build
from test_promotion_direction_cli import cli_module


def rows():
    days = [date(2026, 5, 4) + timedelta(days=i) for i in range(60)]
    days = [d for d in days if d.weekday() < 5]
    return [FeatureRow(code=f"{i:06d}", trade_date=d.isoformat(), target_board=1,
                       label=int(i == 0), baseline_probability=.1, candidate_route="fixture",
                       values={"memory_score": float(90 if i == 0 else 10)})
            for d in days for i in range(8)]


def evaluate(data, **kw):
    return walk_forward_evaluate(data, initial_train_days=20, validation_days=5,
                                 step_days=5, calibration_days=3, **kw)


def test_cli_exposes_rolling_window_without_changing_default(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["train"])
    assert cli_module().parse_args().train_window_days is None
    monkeypatch.setattr(sys, "argv", ["train", "--train-window-days", "60"])
    assert cli_module().parse_args().train_window_days == 60


def test_expanding_default_and_large_window_are_identical():
    data = rows()
    original = evaluate(data)
    explicit = evaluate(data, train_window_days=None)
    large = evaluate(data, train_window_days=100)
    np.testing.assert_array_equal(original["predictions"], explicit["predictions"])
    np.testing.assert_array_equal(original["predictions"], large["predictions"])
    assert original["training_window_policy"] == "expanding"


def test_rolling_preserves_validation_dates_denominator_and_separates_tail():
    data = rows()
    baseline = evaluate(data)
    rolling = evaluate(data, train_window_days=15)
    assert rolling["trade_dates"] == baseline["trade_dates"]
    np.testing.assert_array_equal(rolling["labels"], baseline["labels"])
    assert rolling["training_window_policy"] == "rolling"
    for fold in rolling["folds"]:
        assert fold["train_sample_count"] == 15 * 8
        assert fold["train_end_date"] < fold["validation_start_date"]
        assert fold["fit_contract"]["fit_end_date"] < fold["fit_contract"]["calibration_start_date"]
        assert fold["fit_contract"]["calibration_end_date"] == fold["train_end_date"]
    assert rolling["folds"][-1]["train_start_date"] > rolling["folds"][0]["train_start_date"]


def test_fit_uses_same_tail_window_and_reports_true_bounds_for_unsorted_input():
    data = rows()
    selected = training_window_rows(data, train_window_days=15, calibration_days=3)
    model = fit_temporally_calibrated_model(data, calibration_days=3, train_window_days=15)
    explicit = fit_temporally_calibrated_model(selected, calibration_days=3)
    np.testing.assert_array_equal(model.predict(data), explicit.predict(data))
    reversed_model = fit_temporally_calibrated_model(list(reversed(selected)), calibration_days=3)
    assert reversed_model.fit_start_date == model.fit_start_date
    assert reversed_model.fit_end_date == model.fit_end_date
    assert reversed_model.calibration_start_date == model.calibration_start_date
    assert reversed_model.calibration_end_date == model.calibration_end_date


def test_future_labels_do_not_change_earlier_fold_predictions():
    data = rows()
    baseline = evaluate(data, train_window_days=15)
    cutoff = baseline["folds"][0]["validation_end_date"]
    changed = [replace(r, label=1-r.label, values={"memory_score": 999999.})
               if r.trade_date > cutoff else r for r in data]
    result = evaluate(changed, train_window_days=15)
    n = baseline["folds"][0]["validation_sample_count"]
    np.testing.assert_array_equal(result["predictions"][:n], baseline["predictions"][:n])


@pytest.mark.parametrize("value", [True, 0, -1, 9, 10.5, "15"])
def test_invalid_window_is_rejected_not_silently_skipped(value):
    with pytest.raises(ValueError, match="train_window_days"):
        evaluate(rows(), train_window_days=value)


def test_single_class_window_is_reported_not_hidden():
    data = rows()
    dates = sorted({r.trade_date for r in data})
    data = [replace(r, label=0) if r.trade_date in set(dates[5:20]) else r for r in data]
    result = evaluate(data, train_window_days=15)
    assert result["folds"][0]["status"] == "skipped"
    assert result["skipped_fold_count"] >= 1


@pytest.mark.asyncio
async def test_registered_research_artifact_uses_validated_rolling_window(env, monkeypatch, tmp_path):
    import json
    from sqlalchemy import select
    from app.models.promotion import PromotionModelArtifact
    from app.promotion.modeling.dataset import DatasetBundle
    from app.promotion.modeling import training

    db, _ = env
    data = rows()
    async def fixture_panel(*args, **kwargs):
        return DatasetBundle(rows=data, data_version="fixture_window",
                             diagnostics={"daily": []},
                             start_date=date.fromisoformat(data[0].trade_date),
                             end_date=date.fromisoformat(data[-1].trade_date))
    monkeypatch.setattr(training, "build_historical_panel_dataset", fixture_panel)
    result = await training.train_promotion_challenger(
        db, target_board=1, dataset_source="historical_panel",
        initial_train_days=20, validation_days=5, step_days=5, calibration_days=3,
        train_window_days=15, minimum_samples=1, minimum_trade_days=1,
        minimum_positives=1, persist=True, artifact_dir=tmp_path / "artifacts")
    artifact = (await db.scalars(select(PromotionModelArtifact))).one()
    payload = json.loads((tmp_path / "artifacts" / (result["model_version"] + ".json")).read_text())
    selected = training_window_rows(data, train_window_days=15, calibration_days=3)
    assert artifact.status == "pretrained"
    assert artifact.training_start_date.isoformat() == min(r.trade_date for r in selected)
    assert payload["training_config"]["train_window_days"] == 15
    assert payload["model"]["fit_start_date"] == artifact.training_start_date.isoformat()
    assert payload["model"]["fit_end_date"] < payload["model"]["calibration_start_date"]


@pytest.mark.asyncio
async def test_ledger_research_does_not_require_unused_source_migration(env):
    db, _ = env
    await seed(db)
    await truth(db)
    # Fixture-only legacy schema: no business migration or production write.
    for column in ("source_version", "source_quote_at", "observed_at", "evidence_json"):
        await db.execute(text(f"ALTER TABLE limit_up_pool DROP COLUMN {column}"))
    await db.commit()
    statements = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        result = await build(db)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(result.rows) == 2
    pool_reads = [s for s in statements if "FROM limit_up_pool" in s]
    assert pool_reads and all("source_version" not in s for s in pool_reads)
