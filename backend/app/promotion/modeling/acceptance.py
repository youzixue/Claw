"""Explicit challenger acceptance gates; no metric cherry-picking."""

from __future__ import annotations


DEFAULT_ACCEPTANCE_POLICY = {
    "minimum_completed_folds": 3,
    "minimum_validation_trade_days": 15,
    "minimum_validation_positives": 20,
    "minimum_average_precision_ratio": 0.98,
    "maximum_brier_ratio": 1.02,
    "minimum_top12_precision_delta": 0.0,
    "minimum_top30_recall_delta": -0.02,
    "maximum_ece_ratio": 1.15,
    "maximum_absolute_ece": 0.06,
    "minimum_known_regime_coverage": 0.80,
    "minimum_evaluable_regimes": 2,
    "minimum_regime_trade_days": 5,
    "minimum_regime_positives": 3,
    "minimum_regime_ap_ratio": 0.85,
    "maximum_regime_brier_ratio": 1.15,
    "maximum_underperforming_regime_share": 0.50,
    "minimum_paired_bootstrap_days": 0,
    "minimum_bootstrap_ap_delta_lower": None,
    "minimum_bootstrap_brier_improvement_lower": None,
    "minimum_bootstrap_top12_delta_lower": None,
}

# Offline walk-forward may enter shadow on non-inferiority. Production review is
# intentionally stricter: enough distinct sessions plus paired lower bounds must
# show an improvement, preventing daily noise from becoming a new Champion.
SHADOW_ACCEPTANCE_POLICY = {
    "minimum_completed_folds": 30,
    "minimum_validation_trade_days": 30,
    "minimum_validation_positives": 50,
    "minimum_average_precision_ratio": 1.02,
    "maximum_brier_ratio": 1.0,
    "minimum_top12_precision_delta": 0.005,
    "minimum_top30_recall_delta": -0.01,
    "maximum_ece_ratio": 1.0,
    "maximum_absolute_ece": 0.05,
    "minimum_known_regime_coverage": 0.90,
    "minimum_evaluable_regimes": 2,
    "minimum_regime_trade_days": 5,
    "minimum_regime_positives": 3,
    "minimum_regime_ap_ratio": 0.95,
    "maximum_regime_brier_ratio": 1.05,
    "maximum_underperforming_regime_share": 0.34,
    "minimum_paired_bootstrap_days": 30,
    "minimum_bootstrap_ap_delta_lower": 0.0,
    "minimum_bootstrap_brier_improvement_lower": 0.0,
    "minimum_bootstrap_top12_delta_lower": 0.0,
}


def evaluate_challenger_acceptance(
    walk_forward: dict,
    *,
    policy: dict | None = None,
) -> dict:
    rules = {**DEFAULT_ACCEPTANCE_POLICY, **(policy or {})}
    if (walk_forward.get("research_only") or walk_forward.get("unknown_labels_allowed")
            or (walk_forward.get("challenger_metrics") or {}).get("research_only")
            or (walk_forward.get("champion_metrics") or {}).get("research_only")):
        return {"passed": False, "decision": "rejected", "policy": rules,
                "checks": [{"name": "complete_verified_labels", "passed": False,
                            "actual": "partial_historical_research", "required": "verified_complete"}],
                "note": "含未知标签的历史研究不能取得影子或生产资格。"}
    challenger = walk_forward["challenger_metrics"]
    champion = walk_forward["champion_metrics"]
    checks: list[dict] = []

    def add(name: str, passed: bool, actual, required, detail: str) -> None:
        checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "actual": actual,
                "required": required,
                "detail": detail,
            }
        )

    add(
        "completed_folds",
        walk_forward["completed_fold_count"] >= rules["minimum_completed_folds"],
        walk_forward["completed_fold_count"],
        rules["minimum_completed_folds"],
        "必须覆盖多个独立向前验证窗口",
    )
    add(
        "validation_trade_days",
        walk_forward["validation_trade_day_count"]
        >= rules["minimum_validation_trade_days"],
        walk_forward["validation_trade_day_count"],
        rules["minimum_validation_trade_days"],
        "验证期交易日不足时不得晋级",
    )
    add(
        "validation_positives",
        challenger["positive_count"] >= rules["minimum_validation_positives"],
        challenger["positive_count"],
        rules["minimum_validation_positives"],
        "低基准率事件需要足够正样本",
    )

    champion_ap = float(champion.get("average_precision") or 0.0)
    challenger_ap = float(challenger.get("average_precision") or 0.0)
    required_ap = champion_ap * rules["minimum_average_precision_ratio"]
    add(
        "average_precision",
        challenger_ap >= required_ap,
        challenger_ap,
        round(required_ap, 6),
        "主指标使用 PR-AUC/average precision，而非准确率",
    )

    champion_brier = max(float(champion.get("brier_score") or 0.0), 1e-9)
    challenger_brier = float(challenger.get("brier_score") or 0.0)
    maximum_brier = champion_brier * rules["maximum_brier_ratio"]
    add(
        "brier_score",
        challenger_brier <= maximum_brier,
        challenger_brier,
        round(maximum_brier, 6),
        "概率误差不可因追求排序而明显恶化",
    )

    champion_top12 = champion.get("daily_rank", {}).get("12", {})
    challenger_top12 = challenger.get("daily_rank", {}).get("12", {})
    top12_delta = float(challenger_top12.get("precision") or 0.0) - float(
        champion_top12.get("precision") or 0.0
    )
    add(
        "top12_precision",
        top12_delta >= rules["minimum_top12_precision_delta"],
        round(top12_delta, 6),
        rules["minimum_top12_precision_delta"],
        "正式榜精度不得下降",
    )

    champion_top30 = champion.get("daily_rank", {}).get("30", {})
    challenger_top30 = challenger.get("daily_rank", {}).get("30", {})
    top30_delta = float(challenger_top30.get("recall") or 0.0) - float(
        champion_top30.get("recall") or 0.0
    )
    add(
        "top30_recall",
        top30_delta >= rules["minimum_top30_recall_delta"],
        round(top30_delta, 6),
        rules["minimum_top30_recall_delta"],
        "宽召回层不能通过大幅漏掉真实涨停来换精度",
    )

    champion_ece = max(float(champion.get("expected_calibration_error") or 0.0), 1e-9)
    challenger_ece = float(challenger.get("expected_calibration_error") or 0.0)
    maximum_ece = min(
        champion_ece * rules["maximum_ece_ratio"],
        rules["maximum_absolute_ece"],
    )
    add(
        "calibration_error",
        challenger_ece <= maximum_ece,
        challenger_ece,
        round(maximum_ece, 6),
        "概率校准必须可用于仓位/阈值决策",
    )

    regime_metrics = walk_forward.get("regime_metrics") or {}
    total_regime_samples = sum(
        int(item.get("sample_count") or 0) for item in regime_metrics.values()
    )
    known_regime_samples = sum(
        int(item.get("sample_count") or 0)
        for regime, item in regime_metrics.items()
        if regime != "unknown"
    )
    known_regime_coverage = (
        known_regime_samples / total_regime_samples if total_regime_samples else 0.0
    )
    add(
        "known_regime_coverage",
        known_regime_coverage >= rules["minimum_known_regime_coverage"],
        round(known_regime_coverage, 6),
        rules["minimum_known_regime_coverage"],
        "必须先有足够的时点化市场风格标签，不能只看全样本均值",
    )

    evaluable_regimes: list[tuple[str, dict]] = []
    underperforming_regimes: list[str] = []
    for regime, item in regime_metrics.items():
        if regime == "unknown":
            continue
        challenger_slice = item.get("challenger_metrics") or {}
        champion_slice = item.get("champion_metrics") or {}
        if (
            int(item.get("trade_day_count") or 0) < rules["minimum_regime_trade_days"]
            or int(challenger_slice.get("positive_count") or 0)
            < rules["minimum_regime_positives"]
        ):
            continue
        evaluable_regimes.append((regime, item))
        slice_challenger_ap = float(challenger_slice.get("average_precision") or 0.0)
        slice_champion_ap = float(champion_slice.get("average_precision") or 0.0)
        slice_challenger_brier = float(challenger_slice.get("brier_score") or 0.0)
        slice_champion_brier = max(
            float(champion_slice.get("brier_score") or 0.0), 1e-9
        )
        if (
            slice_challenger_ap
            < slice_champion_ap * rules["minimum_regime_ap_ratio"]
            or slice_challenger_brier
            > slice_champion_brier * rules["maximum_regime_brier_ratio"]
        ):
            underperforming_regimes.append(regime)
    add(
        "evaluable_regimes",
        len(evaluable_regimes) >= rules["minimum_evaluable_regimes"],
        len(evaluable_regimes),
        rules["minimum_evaluable_regimes"],
        "至少覆盖多个具有足够交易日和正样本的市场风格",
    )
    underperforming_share = (
        len(underperforming_regimes) / len(evaluable_regimes)
        if evaluable_regimes
        else 1.0
    )
    add(
        "regime_stability",
        bool(evaluable_regimes)
        and underperforming_share <= rules["maximum_underperforming_regime_share"],
        {
            "underperforming_share": round(underperforming_share, 6),
            "underperforming_regimes": underperforming_regimes,
        },
        rules["maximum_underperforming_regime_share"],
        "挑战者不能只靠单一行情风格抬高总分",
    )

    bootstrap = walk_forward.get("paired_bootstrap") or {}
    minimum_bootstrap_days = int(rules.get("minimum_paired_bootstrap_days") or 0)
    if bootstrap or minimum_bootstrap_days > 0:
        bootstrap_days = int(bootstrap.get("trade_day_count") or 0)
        add(
            "paired_bootstrap_days",
            bootstrap_days >= minimum_bootstrap_days,
            bootstrap_days,
            minimum_bootstrap_days,
            "按交易日成组的配对重采样必须覆盖足够独立交易日",
        )
        bootstrap_specs = (
            (
                "bootstrap_average_precision",
                "average_precision_delta",
                rules.get("minimum_bootstrap_ap_delta_lower"),
                "PR-AUC 增益的配对置信下界不得为负",
            ),
            (
                "bootstrap_brier",
                "brier_improvement",
                rules.get("minimum_bootstrap_brier_improvement_lower"),
                "Brier 改善的配对置信下界不得为负",
            ),
            (
                "bootstrap_top12_precision",
                "top12_precision_delta",
                rules.get("minimum_bootstrap_top12_delta_lower"),
                "正式榜精度增益的配对置信下界不得为负",
            ),
        )
        for check_name, metric_name, required_lower, detail in bootstrap_specs:
            if required_lower is None:
                continue
            interval = bootstrap.get(metric_name) or {}
            raw_lower = interval.get("lower")
            lower = float(raw_lower) if raw_lower is not None else None
            add(
                check_name,
                lower is not None and lower >= float(required_lower),
                round(lower, 6) if lower is not None else None,
                float(required_lower),
                detail,
            )

    passed = all(check["passed"] for check in checks)
    return {
        "passed": passed,
        "decision": "shadow_eligible" if passed else "rejected",
        "policy": rules,
        "checks": checks,
        "note": "通过仅代表可进入影子运行，不能自动替换生产冠军。",
    }
