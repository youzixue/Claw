"""Outcome-blind recall and independent direction labels; isolated fixtures only."""
from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import pytest
from sqlalchemy import delete, select, update

from app.models.stock import StockKline
from app.promotion.modeling.acceptance import evaluate_challenger_acceptance
from app.promotion.modeling.historical_dataset import (
    _Bar, _research_outcome_label, _select_broad_candidates, build_historical_panel_dataset,
)
from app.promotion.modeling.metrics import partial_classification_metrics
from app.promotion.modeling.walk_forward import walk_forward_evaluate
from test_promotion_modeling import modeling_session, _seed_historical_panel
from test_promotion_rolling_training_20260928 import rows


def bar(day, **kw):
    return _Bar(code="600001", trade_date=day, open=10, high=11, low=9,
                close=kw.get("close", 10.), prev_close=10., volume=kw.get("volume", 1000.),
                amount=10000., turnover=1., change_pct=kw.get("change_pct", 0.),
                source=kw.get("source", "ths"))


def test_direction_target_is_close_up_not_limit_or_high():
    before = bar(date(2026, 9, 7))
    after = bar(date(2026, 9, 8), close=10.01, change_pct=.1)
    args = dict(outcome_day=after.trade_date, target_board=1)
    assert _research_outcome_label(before, after, label_target="promotion", **args) == (0, "")
    assert _research_outcome_label(before, after, label_target="next_day_close_up", **args) == (1, "")
    assert _research_outcome_label(before, replace(after, close=10), label_target="next_day_close_up", **args) == (0, "")


@pytest.mark.parametrize("change,reason", [
    ({"source": "spot"}, "candidate_outcome_source_unverified"),
    ({"volume": 0}, "candidate_outcome_suspended_or_invalid"),
    ({"prev_close": 9.}, "candidate_outcome_price_chain_discontinuity"),
])
def test_unknown_direction_truth_never_becomes_zero(change, reason):
    before, after = bar(date(2026, 9, 7)), bar(date(2026, 9, 8))
    assert _research_outcome_label(before, replace(after, **change),
        outcome_day=after.trade_date, target_board=1, label_target="next_day_close_up") == (None, reason)


def test_missing_session_is_not_skipped_and_holiday_is_allowed():
    before = bar(date(2026, 9, 7))
    label, reason = _research_outcome_label(before, bar(date(2026, 9, 9)),
        outcome_day=date(2026, 9, 9), target_board=1, label_target="next_day_close_up")
    assert label is None and reason == "outcome_calendar_gap"
    before = bar(date(2026, 9, 24))
    assert _research_outcome_label(before, bar(date(2026, 9, 28)),
        outcome_day=date(2026, 9, 28), target_board=1, label_target="next_day_close_up") == (0, "")


def test_balanced_recall_is_fixed_budget_deterministic_and_outcome_blind():
    candidates = [dict(code=f"{i:06d}", scores=(100-i, i, -abs(50-i), i % 11), label=i % 2)
                  for i in range(100)]
    original = _select_broad_candidates(candidates, limit=12, policy="balanced_round_robin")
    changed = [{**r, "label": None, "outcome": "whatever"} for r in reversed(candidates)]
    result = _select_broad_candidates(changed, limit=12, policy="balanced_round_robin")
    assert [r["code"] for r in original] == [r["code"] for r in result]
    assert len({r["code"] for r in result}) == 12
    assert {"000000", "000099", "000050"} <= {r["code"] for r in result}
    assert _select_broad_candidates(candidates, limit=0, policy="balanced_round_robin") == []


def test_unknown_top_rank_keeps_slot_instead_of_promoting_known_winner():
    result = partial_classification_metrics([None, 1, 0], [.9, .8, .1], ["d"]*3, top_ks=(1, 2))
    assert result["sample_count"] == 3 and result["unknown_count"] == 1
    top = result["daily_rank"]["1"]
    assert top["selected_count"] == 1 and top["hit_count"] == 0
    assert top["precision"] is None and (top["precision_lower"], top["precision_upper"]) == (0., 1.)
    assert result["daily_rank"]["2"]["precision_lower"] == .5
    assert result["observed_metrics"]["sample_count"] == 2
    assert not evaluate_challenger_acceptance({"research_only": True})["passed"]


@pytest.mark.parametrize("probabilities", [[float("nan")], [float("inf")], [-.1], [1.1], [[.5]]])
def test_partial_metrics_reject_invalid_probability_arrays(probabilities):
    with pytest.raises(ValueError, match="invalid partial"):
        partial_classification_metrics([None], probabilities, ["d"])


def test_partial_empty_list_is_not_zero_hit_rate():
    metrics = partial_classification_metrics([], [], [], top_ks=(12,))
    assert metrics["sample_count"] == 0
    assert metrics["daily_rank"]["12"]["precision"] is None


def test_all_unknown_metrics_are_not_zero_accuracy():
    result = partial_classification_metrics([None, None], [.5, .4], ["d", "d"], top_ks=(2,))
    assert result["observed_metrics"] == {"sample_count": 0}
    assert result["daily_rank"]["2"]["precision"] is None
    assert result["daily_rank"]["2"]["precision_upper"] == 1


def test_partial_walk_forward_preserves_unknown_candidates_and_cannot_pass_gate():
    data = rows()
    data = [replace(r, label=None) if r.code.endswith("7") else r for r in data]
    kwargs = dict(initial_train_days=20, validation_days=5, step_days=5, calibration_days=3)
    with pytest.raises(ValueError, match="unknown labels"):
        walk_forward_evaluate(data, **kwargs)
    result = walk_forward_evaluate(data, allow_unknown_labels=True, **kwargs)
    assert result["research_only"] is True
    assert result["challenger_metrics"]["sample_count"] == len(result["predictions"])
    assert result["challenger_metrics"]["unknown_count"] > 0
    assert len(result["labels"]) == len(result["trade_dates"])
    assert not evaluate_challenger_acceptance(result)["passed"]


@pytest.mark.asyncio
async def test_future_bar_deletion_does_not_change_yesterday_candidates(modeling_session):
    db, _ = modeling_session
    await _seed_historical_panel(db)
    args = dict(target_board=1, lookback_trade_days=80, history_days=20,
                candidate_limit_per_day=50, minimum_universe_count=1)
    before = await build_historical_panel_dataset(db, **args)
    target_day = sorted({r.trade_date for r in before.rows})[-2]
    selected = [r for r in before.rows if r.trade_date == target_day]
    victim = selected[0]
    days = list((await db.scalars(select(StockKline.trade_date).distinct().order_by(StockKline.trade_date))).all())
    outcome_day = next(d for d in days if d > date.fromisoformat(target_day))
    await db.execute(delete(StockKline).where(StockKline.code == victim.code,
                                             StockKline.trade_date == outcome_day))
    await db.commit()
    after = await build_historical_panel_dataset(db, **args)
    changed = [r for r in after.rows if r.trade_date == target_day]
    assert [(r.code, r.values) for r in selected] == [(r.code, r.values) for r in changed]
    assert next(r for r in changed if r.code == victim.code).label is None
    assert after.diagnostics["unknown_count"] >= 1
    assert before.data_version != after.data_version


@pytest.mark.asyncio
async def test_two_heads_have_identical_feature_universe(modeling_session):
    db, _ = modeling_session
    await _seed_historical_panel(db)
    args = dict(target_board=1, lookback_trade_days=80, history_days=20,
                candidate_limit_per_day=50, minimum_universe_count=1)
    event = await build_historical_panel_dataset(db, **args)
    direction = await build_historical_panel_dataset(db, label_target="next_day_close_up", **args)
    assert [(r.code, r.trade_date, r.values) for r in event.rows] == [
        (r.code, r.trade_date, r.values) for r in direction.rows]
    assert any(a.label != b.label for a, b in zip(event.rows, direction.rows))
    assert direction.diagnostics["manual_review_eligible"] is False
    assert event.data_version != direction.data_version
    assert all((a.label is None) == (b.label is None) for a, b in zip(event.rows, direction.rows))


@pytest.mark.asyncio
async def test_partial_panel_training_report_preserves_unknown_and_pretraining_gate(modeling_session, monkeypatch):
    import json
    from app.promotion.modeling import training
    from app.promotion.modeling.dataset import DatasetBundle

    db, _ = modeling_session
    data = [replace(r, label=None) if r.code.endswith("7") else r for r in rows()]
    dates = sorted({r.trade_date for r in data})
    diagnostics = {"daily": [
        {"trade_date": day, "universe_positive_count": 10, "candidate_positive_count": 5,
         "universe_unknown_count": 2, "candidate_unknown_count": 1} for day in dates
    ]}

    async def fixture_panel(*args, **kwargs):
        return DatasetBundle(rows=data, data_version="fixture_partial_v3",
            diagnostics=diagnostics, start_date=date.fromisoformat(dates[0]),
            end_date=date.fromisoformat(dates[-1]))

    monkeypatch.setattr(training, "build_historical_panel_dataset", fixture_panel)
    result = await training.train_promotion_challenger(
        db, target_board=1, dataset_source="historical_panel",
        initial_train_days=20, validation_days=5, step_days=5, calibration_days=3,
        minimum_samples=1, minimum_trade_days=1, minimum_positives=1, persist=False)
    assert result["status"] == "completed" and result["persisted"] is False
    assert result["artifact_uri"] is None
    assert result["acceptance"]["passed"] is False
    assert result["acceptance"]["decision"] == "pretraining_only"
    metrics = result["walk_forward"]["challenger_metrics"]
    assert metrics["unknown_count"] > 0
    assert metrics["sample_count"] > metrics["observed_metrics"]["sample_count"]
    universe = result["walk_forward"]["historical_universe_rank_metrics"]
    assert universe["candidate_prefilter_recall"] is None
    assert universe["challenger"]["12"]["full_universe_recall"] is None
    assert result["dataset"]["guards"]["minimum_samples"]["actual"] == sum(
        r.label is not None for r in data)
    json.dumps(result, default=str, allow_nan=False)
