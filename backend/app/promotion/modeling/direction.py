"""Offline close-up head on verified immutable runs; no deployment/DB writes."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from app.promotion.direction_research import DIRECTION_LABEL_VERSION, DIRECTION_TARGET_PRECISION
from app.promotion.modeling.acceptance import evaluate_challenger_acceptance
from app.promotion.modeling.dataset import DatasetBundle
from app.promotion.modeling.ledger_dataset import build_ledger_training_dataset
from app.promotion.modeling.walk_forward import walk_forward_evaluate

DIRECTION_EVALUATION_VERSION = "direction_walk_forward_research_v1"
DIRECTION_MIN_VALIDATION_DAYS = 30


def evaluate_direction_bundle(bundle: DatasetBundle, *, initial_train_days: int = 25,
                              validation_days: int = 5, step_days: int = 5,
                              calibration_days: int = 5) -> dict:
    """Use the same features, whole-run guards and time splits, a different label.

    Passing a retrospective study is NOT shadow eligibility. The existing manual
    deployment protocol only supports promotion events, never this new target.
    """
    if (bundle.diagnostics.get("label_version") != DIRECTION_LABEL_VERSION
            or bundle.diagnostics.get("storage_source") != "immutable_prediction_ledger"):
        raise ValueError("direction evaluation requires verified immutable direction labels")
    result = {
        "version": DIRECTION_EVALUATION_VERSION, "label_version": DIRECTION_LABEL_VERSION,
        "data_version": bundle.data_version, "diagnostics": bundle.diagnostics,
        "scope": "offline_research_only", "production_unchanged": True,
        "manual_review_eligible": False, "persisted": False,
        "target_precision": DIRECTION_TARGET_PRECISION,
        "minimum_validation_trade_days": DIRECTION_MIN_VALIDATION_DAYS,
        "historical_first_knowledge_verified": False,
        "notes": ["上涨标签使用封存T+1收盘与前收盘，不用涨停标签或日内最高涨幅",
                  "时间外回测不是前向影子验收；不替换Champion、不自动晋级、不代表可成交收益"],
    }
    if not bundle.rows:
        return {**result, "status": "insufficient_evidence", "reason": "no_verified_direction_rows",
                "target_met": None, "walk_forward": None}
    identities = [(r.trade_date, r.code) for r in bundle.rows]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate frozen direction stock/day")
    rows = sorted(bundle.rows, key=lambda row: (row.trade_date, row.code))
    if any(type(r.label) is not int or r.label not in (0, 1) for r in rows):
        raise ValueError("invalid direction outcome label")
    if step_days < validation_days:
        raise ValueError("direction validation windows must not overlap")
    try:
        evaluation = walk_forward_evaluate(rows, initial_train_days=initial_train_days,
            validation_days=validation_days, step_days=step_days, calibration_days=calibration_days)
    except ValueError as exc:
        return {**result, "status": "insufficient_evidence", "reason": str(exc),
                "target_met": None, "walk_forward": None}
    rank = evaluation["challenger_metrics"]["daily_rank"]["12"]
    daily = rank["daily"]
    day_count = len(daily)
    selected = np.asarray([d["selected_count"] for d in daily], dtype=float)
    hits = np.asarray([d["hit_count"] for d in daily], dtype=float)
    # Resample entire trading sessions, not correlated stocks independently.
    draws = np.random.default_rng(0).integers(0, day_count, size=(500, day_count))
    denominator = selected[draws].sum(axis=1)
    boot = hits[draws].sum(axis=1) / denominator
    lower = float(np.quantile(boot, 0.05))
    evidence_complete = (not bundle.diagnostics.get("excluded_runs")
                         and evaluation["skipped_fold_count"] == 0
                         and step_days == validation_days)
    full_lists = bool(daily) and all(d["selected_count"] == 12 for d in daily)
    enough_days = day_count >= DIRECTION_MIN_VALIDATION_DAYS
    # Reuse all existing offline discrimination, calibration and regime gates.
    acceptance = evaluate_challenger_acceptance(evaluation, policy={
        "minimum_validation_trade_days": DIRECTION_MIN_VALIDATION_DAYS,
    })
    checks_passed = all(c["passed"] for c in acceptance["checks"])
    has_evidence = evidence_complete and full_lists and enough_days
    target_met = bool(rank["precision"] >= DIRECTION_TARGET_PRECISION
                      and lower >= DIRECTION_TARGET_PRECISION and checks_passed) if has_evidence else None
    public = {key: value for key, value in evaluation.items()
              if key not in {"predictions", "baseline_probabilities", "labels", "trade_dates"}}
    # This baseline is the frozen route-direction estimate, NOT a limit-up model
    # evaluated against a mismatched label and NOT a deployed direction Champion.
    public["direction_reference_metrics"] = public.pop("champion_metrics")
    for regime in public.get("regime_metrics", {}).values():
        regime["direction_reference_metrics"] = regime.pop("champion_metrics")
    return {**result, "status": "research_target_met" if target_met else
            "target_not_met" if has_evidence else "insufficient_evidence",
            "target_met": target_met, "walk_forward": public,
            "top12_precision_lower_95": round(lower, 6),
            "bootstrap": {"unit": "trade_day", "resamples": 500, "seed": 0},
            "evidence_complete": evidence_complete, "full_top12_lists": full_lists,
            "offline_quality_checks": acceptance["checks"],
            "daily_target_met_count": sum(d["selected_count"] == 12
                and d["hit_count"] / 12 >= DIRECTION_TARGET_PRECISION for d in daily)}


def validate_direction_as_of(as_of_at: datetime) -> None:
    if not isinstance(as_of_at, datetime) or as_of_at.tzinfo is not None:
        raise ValueError("direction research requires a naive Asia/Shanghai as_of_at")
    if as_of_at > datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None):
        raise ValueError("direction research as_of_at cannot be in the future")


async def research_direction_challenger(db, *, as_of_at: datetime,
                                       start_date=None, end_date=None,
                                       snapshot_context="promotion_2000", model_version=None,
                                       initial_train_days=25, validation_days=5,
                                       step_days=5, calibration_days=5) -> dict:
    validate_direction_as_of(as_of_at)
    bundle = await build_ledger_training_dataset(db, target_board=1,
        label_target=DIRECTION_LABEL_VERSION, as_of_at=as_of_at,
        start_date=start_date, end_date=end_date, snapshot_context=snapshot_context,
        model_version=model_version)
    return evaluate_direction_bundle(bundle, initial_train_days=initial_train_days,
        validation_days=validation_days, step_days=step_days, calibration_days=calibration_days)
