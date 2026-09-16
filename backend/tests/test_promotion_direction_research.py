"""Direction-only research contracts; fixtures never touch the business DB."""
import copy
import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from app.models.promotion import PromotionPredictionSnapshot
from app.models.stock import StockKline
from app.promotion.direction_research import (
    DIRECTION_LABEL_VERSION, annotate_direction_research, frozen_direction_probability,
)
from app.promotion.modeling.dataset import DatasetBundle
from app.promotion.modeling.direction import evaluate_direction_bundle
from app.promotion.modeling.features import FeatureRow, extract_point_in_time_features
from app.promotion.modeling.ledger_dataset import build_ledger_training_dataset
from test_promotion_ledger_dataset import env, seed, truth, seal_fixture, DAY, OUTCOME, CUTOFF


def candidate(code, p, eligible=True):
    return {"code": code, "target_board": 1, "direction_probability": p,
            "probability": 0.02 if code.endswith("1") else 0.20,
            "trade_ready": False, "prediction_actionable": False,
            "probability_factors": {"prediction_rank_eligible": eligible,
                "memory_score": 100 if code.endswith("1") else 0,
                "learning_direction_probability_method": "fixture_route_posterior"}}


def test_direction_rank_ignores_event_score_and_keeps_production_unchanged():
    rows = [candidate("600001", .3), candidate("600002", .8)]
    before = copy.deepcopy(rows)
    enriched, meta = annotate_direction_research(rows)
    assert rows == before
    assert [item["code"] for item in meta["candidates"]] == ["600002", "600001"]
    assert meta["status"] == "insufficient_candidates"
    assert meta["manual_review_eligible"] is False
    for old, new in zip(rows, enriched):
        assert {k: v for k, v in new.items() if k != "probability_factors"} == {
            k: v for k, v in old.items() if k != "probability_factors"}
        assert {k: v for k, v in new["probability_factors"].items() if k != "direction_research"} == old["probability_factors"]
        assert frozen_direction_probability(new["probability_factors"]) == old["direction_probability"]
        assert extract_point_in_time_features(new["probability_factors"]) == extract_point_in_time_features(old["probability_factors"])


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -.1, 1.1, True, ".8", 10**400])
def test_one_invalid_eligible_probability_blocks_whole_research_ranking(bad):
    _, meta = annotate_direction_research([candidate("600001", .9), candidate("600002", bad)])
    assert meta["status"] == "blocked" and meta["candidates"] == []
    assert meta["missing_probability_count"] == 1


def test_ranking_ties_eligibility_and_future_fields():
    rows = [candidate("600002", .8), candidate("600001", .8), candidate("600003", .99, False)]
    rows[0]["actual_future_return"] = 999
    _, meta = annotate_direction_research(rows)
    assert [r["code"] for r in meta["candidates"]] == ["600001", "600002"]
    assert annotate_direction_research(list(reversed(rows)))[1]["candidates"] == meta["candidates"]
    _, duplicate = annotate_direction_research([rows[0], rows[0]])
    assert duplicate["status"] == "blocked"
    assert annotate_direction_research([])[1]["status"] == "insufficient_candidates"
    with pytest.raises(ValueError):
        annotate_direction_research(rows, limit=True)


async def add_direction_fixture(db, *, ineligible_code=None):
    snapshots = list((await db.scalars(select(PromotionPredictionSnapshot).order_by(PromotionPredictionSnapshot.code))).all())
    rows, _ = annotate_direction_research([candidate(s.code, .6 + i*.1, s.code != ineligible_code) for i, s in enumerate(snapshots)])
    for s, row in zip(snapshots, rows):
        factors = json.loads(s.features_json)
        # Synthetic fixture construction only; retain the ORM append-only guard.
        await db.execute(text("UPDATE promotion_prediction_snapshot SET features_json=:features WHERE id=:id"),
                         {"features": json.dumps({**factors, "prediction_rank_eligible": row["probability_factors"]["prediction_rank_eligible"],
                             "direction_research": row["probability_factors"]["direction_research"]}), "id": s.id})
    await db.commit()
    db.expire_all()
    await seal_fixture(db)


@pytest.mark.asyncio
async def test_direction_dataset_uses_close_up_not_limit_event_and_frozen_baseline(env):
    db, _ = env
    await seed(db)
    await truth(db)
    await add_direction_fixture(db)
    bars = list((await db.scalars(select(StockKline).where(StockKline.trade_date == OUTCOME))).all())
    for bar in bars:
        bar.close = 11.0 if bar.code == "600001" else 10.2
    await db.commit()
    direction = await build_ledger_training_dataset(db, target_board=1,
        label_target=DIRECTION_LABEL_VERSION, as_of_at=CUTOFF)
    event = await build_ledger_training_dataset(db, target_board=1, as_of_at=CUTOFF)
    assert [r.label for r in direction.rows] == [1, 1]
    assert [r.label for r in event.rows] == [1, 0]
    assert [r.baseline_probability for r in direction.rows] == pytest.approx([.6, .7])
    assert [r.baseline_probability for r in event.rows] == [.1, .1]
    assert direction.data_version != event.data_version
    assert direction.diagnostics["label_version"] == DIRECTION_LABEL_VERSION


@pytest.mark.asyncio
async def test_direction_dataset_preserves_prediction_time_eligibility(env):
    db, _ = env
    await seed(db)
    await truth(db)
    await add_direction_fixture(db, ineligible_code="600002")
    result = await build_ledger_training_dataset(db, target_board=1,
        label_target=DIRECTION_LABEL_VERSION, as_of_at=CUTOFF)
    assert [row.code for row in result.rows] == ["600001"]
    assert result.diagnostics["excluded_runs"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["legacy", "missing_one", "truncated_universe", "eligibility", "rank"])
async def test_direction_dataset_never_reconstructs_missing_old_direction_scores(env, mutation):
    db, _ = env
    await seed(db)
    await truth(db)
    if mutation != "legacy":
        await add_direction_fixture(db)
        s = (await db.scalars(select(PromotionPredictionSnapshot))).first()
        factors = json.loads(s.features_json)
        if mutation == "missing_one":
            factors.pop("direction_research")
        elif mutation == "eligibility":
            factors["prediction_rank_eligible"] = False
        elif mutation == "rank":
            factors["direction_research"]["rank_position"] = 99
        else:
            factors["direction_research"]["candidate_count"] += 1
        await db.execute(text("UPDATE promotion_prediction_snapshot SET features_json=:features WHERE id=:id"),
                         {"features": json.dumps(factors), "id": s.id})
        await db.commit()
        db.expire_all()
        await seal_fixture(db)
    result = await build_ledger_training_dataset(db, target_board=1,
        label_target=DIRECTION_LABEL_VERSION, as_of_at=CUTOFF)
    assert result.rows == []
    assert result.diagnostics["excluded_runs"]


def bundle(days=70, stocks=20):
    # Deliberately synthetic separable feature distribution, not market evidence.
    rows = []
    d = date(2026, 1, 5)
    for day in range(days):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        for stock in range(stocks):
            feature = stock / stocks
            rows.append(FeatureRow(code=f"{600000+stock}", trade_date=d.isoformat(),
                target_board=1, label=int(stock >= stocks//3), baseline_probability=.65,
                candidate_route="fixture", values={"route_score": feature},
                market_regime="recovery" if day % 2 else "broad_trend"))
        d += timedelta(days=1)
    return DatasetBundle(rows, {"label_version": DIRECTION_LABEL_VERSION,
        "storage_source": "immutable_prediction_ledger", "excluded_runs": []}, "fixture_direction", None, None)


def test_direction_walk_forward_separate_baseline_no_production_approval():
    result = evaluate_direction_bundle(bundle())
    assert result["walk_forward"]["validation_trade_day_count"] >= 30
    assert "direction_reference_metrics" in result["walk_forward"]
    assert "champion_metrics" not in result["walk_forward"]
    assert result["manual_review_eligible"] is False
    assert result["production_unchanged"] is True and result["persisted"] is False
    assert result["bootstrap"]["unit"] == "trade_day"
    for fold in result["walk_forward"]["folds"]:
        if fold["status"] == "completed":
            assert fold["fit_contract"]["fit_end_date"] < fold["fit_contract"]["calibration_start_date"]
            assert fold["fit_contract"]["calibration_end_date"] < fold["validation_start_date"]


@pytest.mark.parametrize("problem", ["underfilled", "excluded", "few_days", "empty", "skipped_dates"])
def test_direction_eighty_percent_requires_complete_fixed_lists_and_time_evidence(problem):
    b = bundle(days=35 if problem == "few_days" else 70, stocks=8 if problem == "underfilled" else 20)
    if problem == "excluded":
        b.diagnostics["excluded_runs"] = [{"run_id": 99, "reason": "candidate_outcome_unknown"}]
    if problem == "empty":
        b = replace(b, rows=[])
    result = evaluate_direction_bundle(b, step_days=6 if problem == "skipped_dates" else 5)
    assert result["target_met"] is None and result["status"] == "insufficient_evidence"
    assert result["manual_review_eligible"] is False


def test_skipped_training_window_cannot_certify_eighty_percent():
    b = bundle(days=100)
    first_dates = set(sorted({r.trade_date for r in b.rows})[:30])
    b = replace(b, rows=[replace(r, label=1) if r.trade_date in first_dates else r for r in b.rows])
    result = evaluate_direction_bundle(b)
    assert result["walk_forward"]["skipped_fold_count"] > 0
    assert result["walk_forward"]["validation_trade_day_count"] >= 30
    assert result["target_met"] is None
    assert result["status"] == "insufficient_evidence"


def test_direction_refuses_wrong_labels_duplicates_and_overlapping_windows():
    b = bundle()
    with pytest.raises(ValueError, match="immutable direction"):
        evaluate_direction_bundle(replace(b, diagnostics={}))
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_direction_bundle(replace(b, rows=b.rows + b.rows[:1]))
    with pytest.raises(ValueError, match="overlap"):
        evaluate_direction_bundle(b, validation_days=5, step_days=1)
