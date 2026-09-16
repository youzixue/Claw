"""Expanding-window walk-forward evaluation with temporal calibration tails."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.promotion.modeling.features import FeatureRow, FeatureVectorizer
from app.promotion.modeling.logistic import LogisticBinaryClassifier, PlattCalibrator
from app.promotion.modeling.metrics import classification_metrics


@dataclass(slots=True)
class FittedPromotionModel:
    vectorizer: FeatureVectorizer
    classifier: LogisticBinaryClassifier
    calibrator: PlattCalibrator
    fit_start_date: str
    fit_end_date: str
    calibration_start_date: str
    calibration_end_date: str

    def predict(self, rows: list[FeatureRow]) -> np.ndarray:
        raw = self.classifier.predict_proba(self.vectorizer.transform(rows))
        return self.calibrator.transform(raw)

    def payload(self) -> dict:
        return {
            "vectorizer": self.vectorizer.payload(),
            "classifier": self.classifier.payload(),
            "calibrator": self.calibrator.payload(),
            "fit_start_date": self.fit_start_date,
            "fit_end_date": self.fit_end_date,
            "calibration_start_date": self.calibration_start_date,
            "calibration_end_date": self.calibration_end_date,
        }


def _date_groups(rows: list[FeatureRow]) -> list[str]:
    return sorted({row.trade_date for row in rows})


def regime_sliced_metrics(
    labels: np.ndarray,
    challenger: np.ndarray,
    baseline: np.ndarray,
    trade_dates: list[str],
    regimes: list[str],
) -> dict[str, dict]:
    """Report stability by market style instead of hiding aggregate regressions."""

    result: dict[str, dict] = {}
    regime_array = np.asarray(regimes, dtype=object)
    for regime in sorted(set(regimes)):
        indexes = np.flatnonzero(regime_array == regime)
        result[regime] = {
            "sample_count": int(len(indexes)),
            "trade_day_count": len({trade_dates[index] for index in indexes}),
            "challenger_metrics": classification_metrics(
                labels[indexes],
                challenger[indexes],
                [trade_dates[index] for index in indexes],
            ),
            "champion_metrics": classification_metrics(
                labels[indexes],
                baseline[indexes],
                [trade_dates[index] for index in indexes],
            ),
        }
    return result


def fit_temporally_calibrated_model(
    rows: list[FeatureRow],
    *,
    calibration_days: int = 5,
    allow_unmaterialized_pretraining: bool = False,
) -> FittedPromotionModel:
    dates = _date_groups(rows)
    if len(dates) < max(calibration_days + 5, 10):
        raise ValueError("insufficient trade days for temporal fit/calibration split")
    calibration_days = min(max(int(calibration_days), 3), max(len(dates) // 3, 3))
    calibration_dates = set(dates[-calibration_days:])
    fit_rows = [row for row in rows if row.trade_date not in calibration_dates]
    calibration_rows = [row for row in rows if row.trade_date in calibration_dates]
    fit_labels = np.asarray([row.label for row in fit_rows], dtype=float)
    calibration_labels = np.asarray([row.label for row in calibration_rows], dtype=float)
    if len(np.unique(fit_labels)) < 2:
        raise ValueError("fit window does not contain both outcome classes")
    if len(np.unique(calibration_labels)) < 2:
        raise ValueError("calibration tail does not contain both outcome classes")

    vectorizer = FeatureVectorizer(allow_unmaterialized_pretraining=allow_unmaterialized_pretraining)
    fit_matrix = vectorizer.fit_transform(fit_rows)
    classifier = LogisticBinaryClassifier().fit(fit_matrix, fit_labels)
    raw_calibration = classifier.predict_proba(vectorizer.transform(calibration_rows))
    calibrator = PlattCalibrator().fit(raw_calibration, calibration_labels)
    return FittedPromotionModel(
        vectorizer=vectorizer,
        classifier=classifier,
        calibrator=calibrator,
        fit_start_date=fit_rows[0].trade_date,
        fit_end_date=fit_rows[-1].trade_date,
        calibration_start_date=calibration_rows[0].trade_date,
        calibration_end_date=calibration_rows[-1].trade_date,
    )


def walk_forward_evaluate(
    rows: list[FeatureRow],
    *,
    initial_train_days: int = 25,
    validation_days: int = 5,
    step_days: int = 5,
    calibration_days: int = 5,
    allow_unmaterialized_pretraining: bool = False,
) -> dict:
    dates = _date_groups(rows)
    initial_train_days = max(int(initial_train_days), calibration_days + 8)
    validation_days = max(int(validation_days), 1)
    step_days = max(int(step_days), 1)
    if step_days < validation_days:
        raise ValueError(
            "step_days must be greater than or equal to validation_days "
            "so out-of-time observations are not counted more than once"
        )
    if len(dates) < initial_train_days + validation_days:
        raise ValueError(
            f"insufficient trade days for walk-forward: {len(dates)} < "
            f"{initial_train_days + validation_days}"
        )

    predictions: list[float] = []
    baseline_probabilities: list[float] = []
    labels: list[int] = []
    trade_dates: list[str] = []
    regimes: list[str] = []
    folds: list[dict] = []
    train_end_index = initial_train_days
    while train_end_index < len(dates):
        validation_end_index = min(train_end_index + validation_days, len(dates))
        train_dates = set(dates[:train_end_index])
        validation_date_values = set(dates[train_end_index:validation_end_index])
        train_rows = [row for row in rows if row.trade_date in train_dates]
        validation_rows = [row for row in rows if row.trade_date in validation_date_values]
        if not validation_rows:
            break
        try:
            model = fit_temporally_calibrated_model(
                train_rows, calibration_days=calibration_days,
                allow_unmaterialized_pretraining=allow_unmaterialized_pretraining,
            )
        except ValueError as exc:
            folds.append(
                {
                    "status": "skipped",
                    "reason": str(exc),
                    "train_start_date": dates[0],
                    "train_end_date": dates[train_end_index - 1],
                    "validation_start_date": dates[train_end_index],
                    "validation_end_date": dates[validation_end_index - 1],
                }
            )
            train_end_index += step_days
            continue
        fold_predictions = model.predict(validation_rows)
        fold_labels = np.asarray([row.label for row in validation_rows], dtype=float)
        fold_dates = [row.trade_date for row in validation_rows]
        fold_metrics = classification_metrics(fold_labels, fold_predictions, fold_dates)
        predictions.extend(fold_predictions.tolist())
        baseline_probabilities.extend(row.baseline_probability for row in validation_rows)
        labels.extend(row.label for row in validation_rows)
        trade_dates.extend(fold_dates)
        regimes.extend(row.market_regime or "unknown" for row in validation_rows)
        folds.append(
            {
                "status": "completed",
                "train_start_date": dates[0],
                "train_end_date": dates[train_end_index - 1],
                "validation_start_date": dates[train_end_index],
                "validation_end_date": dates[validation_end_index - 1],
                "train_sample_count": len(train_rows),
                "validation_sample_count": len(validation_rows),
                "validation_positive_count": int(np.sum(fold_labels)),
                "metrics": fold_metrics,
                "fit_contract": {
                    "fit_end_date": model.fit_end_date,
                    "calibration_start_date": model.calibration_start_date,
                    "calibration_end_date": model.calibration_end_date,
                    "calibrator": model.calibrator.payload(),
                    "classifier_positive_weight": model.classifier.positive_weight,
                },
            }
        )
        train_end_index += step_days

    if not predictions:
        raise ValueError("no valid walk-forward folds were produced")
    label_array = np.asarray(labels, dtype=float)
    challenger_array = np.asarray(predictions, dtype=float)
    baseline_array = np.asarray(baseline_probabilities, dtype=float)
    return {
        "folds": folds,
        "completed_fold_count": sum(fold["status"] == "completed" for fold in folds),
        "skipped_fold_count": sum(fold["status"] == "skipped" for fold in folds),
        "validation_trade_day_count": len(set(trade_dates)),
        "challenger_metrics": classification_metrics(
            label_array, challenger_array, trade_dates
        ),
        "champion_metrics": classification_metrics(label_array, baseline_array, trade_dates),
        "regime_metrics": regime_sliced_metrics(
            label_array,
            challenger_array,
            baseline_array,
            trade_dates,
            regimes,
        ),
        "predictions": challenger_array,
        "baseline_probabilities": baseline_array,
        "labels": label_array,
        "trade_dates": trade_dates,
    }
