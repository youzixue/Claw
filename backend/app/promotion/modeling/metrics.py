"""Low-base-rate classification and daily ranking metrics."""

from __future__ import annotations

from collections import defaultdict

import numpy as np


_EPSILON = 1e-7


def average_precision(labels: np.ndarray, probabilities: np.ndarray) -> float:
    y = np.asarray(labels, dtype=int)
    p = np.asarray(probabilities, dtype=float)
    positives = int(np.sum(y))
    if positives == 0:
        return 0.0
    order = np.argsort(-p, kind="stable")
    sorted_probabilities = p[order]
    sorted_labels = y[order]
    cumulative_true = 0
    cumulative_count = 0
    score = 0.0
    start = 0
    while start < len(y):
        end = start + 1
        while end < len(y) and sorted_probabilities[end] == sorted_probabilities[start]:
            end += 1
        group_true = int(np.sum(sorted_labels[start:end]))
        cumulative_true += group_true
        cumulative_count += end - start
        if group_true:
            score += (group_true / positives) * (cumulative_true / cumulative_count)
        start = end
    return float(score)


def roc_auc(labels: np.ndarray, probabilities: np.ndarray) -> float:
    y = np.asarray(labels, dtype=int)
    p = np.asarray(probabilities, dtype=float)
    positives = int(np.sum(y))
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return 0.5
    order = np.argsort(p, kind="stable")
    sorted_probabilities = p[order]
    ranks = np.empty(len(p), dtype=float)
    start = 0
    while start < len(p):
        end = start + 1
        while end < len(p) and sorted_probabilities[end] == sorted_probabilities[start]:
            end += 1
        average_rank = ((start + 1) + end) / 2.0
        ranks[order[start:end]] = average_rank
        start = end
    positive_rank_sum = float(np.sum(ranks[y == 1]))
    return (
        positive_rank_sum - positives * (positives + 1) / 2.0
    ) / (positives * negatives)


def calibration_bins(
    labels: np.ndarray,
    probabilities: np.ndarray,
    *,
    bins: int = 10,
) -> tuple[list[dict], float]:
    y = np.asarray(labels, dtype=float)
    p = np.asarray(probabilities, dtype=float)
    result: list[dict] = []
    expected_error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        mask = (p >= lower) & (p < upper if index < bins - 1 else p <= upper)
        count = int(np.sum(mask))
        if not count:
            continue
        mean_probability = float(np.mean(p[mask]))
        observed_rate = float(np.mean(y[mask]))
        expected_error += count / len(y) * abs(mean_probability - observed_rate)
        result.append(
            {
                "lower": round(lower, 4),
                "upper": round(upper, 4),
                "count": count,
                "mean_probability": round(mean_probability, 6),
                "observed_rate": round(observed_rate, 6),
            }
        )
    return result, float(expected_error)


def daily_top_k_metrics(
    labels: np.ndarray,
    probabilities: np.ndarray,
    trade_dates: list[str],
    *,
    k: int,
) -> dict:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, trade_date in enumerate(trade_dates):
        groups[trade_date].append(index)
    selected_count = 0
    hit_count = 0
    positive_count = 0
    daily: list[dict] = []
    for trade_date in sorted(groups):
        indexes = np.asarray(groups[trade_date], dtype=int)
        order = indexes[np.argsort(-probabilities[indexes], kind="stable")]
        selected = order[: min(k, len(order))]
        hits = int(np.sum(labels[selected]))
        positives = int(np.sum(labels[indexes]))
        selected_count += len(selected)
        hit_count += hits
        positive_count += positives
        daily.append(
            {
                "trade_date": trade_date,
                "candidate_count": len(indexes),
                "selected_count": len(selected),
                "hit_count": hits,
                "positive_count": positives,
            }
        )
    precision = hit_count / selected_count if selected_count else 0.0
    recall = hit_count / positive_count if positive_count else 0.0
    base_rate = positive_count / len(labels) if len(labels) else 0.0
    return {
        "k": k,
        "trade_day_count": len(groups),
        "selected_count": selected_count,
        "hit_count": hit_count,
        "positive_count": positive_count,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "lift": round(precision / base_rate, 6) if base_rate else 0.0,
        "daily": daily,
    }


def classification_metrics(
    labels: np.ndarray,
    probabilities: np.ndarray,
    trade_dates: list[str],
    *,
    top_ks: tuple[int, ...] = (5, 12, 30),
) -> dict:
    y = np.asarray(labels, dtype=float)
    p = np.clip(np.asarray(probabilities, dtype=float), _EPSILON, 1.0 - _EPSILON)
    if len(y) == 0:
        return {"sample_count": 0}
    bins, ece = calibration_bins(y, p)
    base_rate = float(np.mean(y))
    return {
        "sample_count": len(y),
        "positive_count": int(np.sum(y)),
        "negative_count": int(len(y) - np.sum(y)),
        "base_rate": round(base_rate, 6),
        "average_probability": round(float(np.mean(p)), 6),
        "average_precision": round(average_precision(y, p), 6),
        "roc_auc": round(roc_auc(y, p), 6),
        "brier_score": round(float(np.mean((p - y) ** 2)), 6),
        "log_loss": round(
            float(np.mean(-(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))),
            6,
        ),
        "expected_calibration_error": round(ece, 6),
        "calibration_bins": bins,
        "daily_rank": {
            str(k): daily_top_k_metrics(y, p, trade_dates, k=k) for k in top_ks
        },
    }
