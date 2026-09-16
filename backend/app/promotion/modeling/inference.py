"""Strict, deterministic inference for persisted promotion-model artifacts.

Only local JSON artifacts produced by ``training.py`` are accepted.  The loader
validates schema, versions, dimensions and finite numeric values before any score
is emitted, so a malformed artifact fails closed instead of silently changing a
production or shadow ranking.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.promotion.modeling.features import (
    COMMON_BOOLEAN_FEATURES,
    COMMON_NUMERIC_FEATURES,
    FEATURE_VERSION,
    FeatureRow,
)
from app.promotion.modeling.logistic import logit, sigmoid
from app.promotion.modeling.feature_coverage import require_hist_feature_coverage


_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
_ALLOWED_NUMERIC_NAMES = set(COMMON_NUMERIC_FEATURES) | set(COMMON_BOOLEAN_FEATURES)


def _finite_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"artifact field {field} is not numeric") from exc
    if not np.isfinite(parsed):
        raise ValueError(f"artifact field {field} must be finite")
    return parsed


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"artifact field {field} must be a string list")
    if len(value) != len(set(value)):
        raise ValueError(f"artifact field {field} contains duplicate values")
    return list(value)


def _float_array(value: Any, field: str) -> np.ndarray:
    if not isinstance(value, list):
        raise ValueError(f"artifact field {field} must be a numeric list")
    array = np.asarray([_finite_float(item, field) for item in value], dtype=float)
    if array.ndim != 1:
        raise ValueError(f"artifact field {field} must be one-dimensional")
    return array


def _local_artifact_path(uri: str) -> Path:
    raw = str(uri or "").strip()
    if raw.startswith("file://"):
        raw = raw[7:]
    if not raw or "://" in raw:
        raise ValueError("only a local promotion artifact path is allowed")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"promotion artifact does not exist: {path}")
    if path.stat().st_size > _MAX_ARTIFACT_BYTES:
        raise ValueError("promotion artifact exceeds the 10 MiB safety limit")
    return path


@dataclass(frozen=True, slots=True)
class LoadedPromotionArtifact:
    model_version: str
    target_board: int
    feature_version: str
    data_version: str
    artifact_path: str
    artifact_sha256: str
    numeric_names: tuple[str, ...]
    route_categories: tuple[str, ...]
    regime_categories: tuple[str, ...]
    mean: np.ndarray
    scale: np.ndarray
    coefficients: np.ndarray
    classifier_intercept: float
    calibration_slope: float
    calibration_intercept: float
    fit_end_date: str
    calibration_end_date: str
    training_config: dict
    offline_acceptance: dict

    @property
    def feature_count(self) -> int:
        return len(self.numeric_names) + len(self.route_categories) + len(self.regime_categories)

    def transform(self, rows: list[FeatureRow]) -> np.ndarray:
        if not rows:
            return np.empty((0, self.feature_count), dtype=float)
        require_hist_feature_coverage(rows, self.numeric_names, feature_version=self.feature_version)
        numeric = np.asarray(
            [[float(row.values.get(name, 0.0)) for name in self.numeric_names] for row in rows],
            dtype=float,
        )
        numeric = np.nan_to_num(
            (numeric - self.mean) / self.scale,
            nan=0.0,
            posinf=0.0,
            neginf=0.0,
        )
        matrices = [numeric]
        if self.route_categories:
            route_index = {name: index for index, name in enumerate(self.route_categories)}
            encoded = np.zeros((len(rows), len(self.route_categories)), dtype=float)
            for row_index, row in enumerate(rows):
                column = route_index.get(row.candidate_route)
                if column is not None:
                    encoded[row_index, column] = 1.0
            matrices.append(encoded)
        if self.regime_categories:
            regime_index = {name: index for index, name in enumerate(self.regime_categories)}
            encoded = np.zeros((len(rows), len(self.regime_categories)), dtype=float)
            for row_index, row in enumerate(rows):
                column = regime_index.get(row.market_regime)
                if column is not None:
                    encoded[row_index, column] = 1.0
            matrices.append(encoded)
        matrix = matrices[0] if len(matrices) == 1 else np.concatenate(matrices, axis=1)
        if matrix.shape[1] != self.feature_count:
            raise RuntimeError("artifact transform produced an unexpected feature dimension")
        return matrix

    def predict(self, rows: list[FeatureRow]) -> tuple[np.ndarray, np.ndarray]:
        if any(int(row.target_board) != self.target_board for row in rows):
            raise ValueError("candidate target_board does not match artifact lane")
        matrix = self.transform(rows)
        raw = sigmoid(matrix @ self.coefficients + self.classifier_intercept)
        calibrated = sigmoid(
            self.calibration_slope * logit(raw) + self.calibration_intercept
        )
        return (
            np.clip(raw, 1e-7, 1.0 - 1e-7),
            np.clip(calibrated, 1e-7, 1.0 - 1e-7),
        )


def load_promotion_artifact(
    artifact_uri: str,
    *,
    expected_model_version: str | None = None,
    expected_feature_version: str | None = FEATURE_VERSION,
    expected_target_board: int | None = None,
) -> LoadedPromotionArtifact:
    """Load and validate one immutable local JSON artifact."""

    path = _local_artifact_path(artifact_uri)
    raw_bytes = path.read_bytes()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("promotion artifact is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("promotion artifact root must be an object")
    if payload.get("schema_version") != "promotion_model_artifact_v1":
        raise ValueError("unsupported promotion artifact schema_version")

    model_version = str(payload.get("model_version") or "").strip()
    feature_version = str(payload.get("feature_version") or "").strip()
    data_version = str(payload.get("data_version") or "").strip()
    try:
        target_board = int(payload.get("target_board"))
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact target_board is invalid") from exc
    if target_board not in {1, 2}:
        raise ValueError("artifact target_board must be 1 or 2")
    if expected_model_version and model_version != expected_model_version:
        raise ValueError("artifact model_version does not match registry")
    if expected_feature_version and feature_version != expected_feature_version:
        raise ValueError("artifact feature_version is not supported by this runtime")
    if expected_target_board is not None and target_board != int(expected_target_board):
        raise ValueError("artifact target_board does not match requested lane")
    if not model_version or not data_version:
        raise ValueError("artifact model_version/data_version is missing")

    model = payload.get("model")
    if not isinstance(model, dict):
        raise ValueError("artifact model payload is missing")
    vectorizer = model.get("vectorizer")
    classifier = model.get("classifier")
    calibrator = model.get("calibrator")
    if not all(isinstance(item, dict) for item in (vectorizer, classifier, calibrator)):
        raise ValueError("artifact model components are incomplete")
    if vectorizer.get("feature_version") != feature_version:
        raise ValueError("artifact vectorizer feature_version mismatch")
    if classifier.get("algorithm") != "numpy_logistic_regression":
        raise ValueError("unsupported promotion classifier")
    if calibrator.get("method") != "platt_out_of_time_tail":
        raise ValueError("unsupported promotion probability calibrator")

    numeric_names = _string_list(vectorizer.get("numeric_names"), "numeric_names")
    unknown_names = sorted(set(numeric_names) - _ALLOWED_NUMERIC_NAMES)
    if unknown_names:
        raise ValueError(f"artifact contains features outside the point-in-time contract: {unknown_names}")
    route_categories = _string_list(vectorizer.get("route_categories"), "route_categories")
    regime_categories = _string_list(vectorizer.get("regime_categories"), "regime_categories")
    mean = _float_array(vectorizer.get("mean"), "mean")
    scale = _float_array(vectorizer.get("scale"), "scale")
    if len(mean) != len(numeric_names) or len(scale) != len(numeric_names):
        raise ValueError("artifact vectorizer numeric dimensions do not match")
    if np.any(scale <= 1e-12):
        raise ValueError("artifact vectorizer scale must be positive")

    coefficients = _float_array(classifier.get("coefficients"), "coefficients")
    expected_dimension = len(numeric_names) + len(route_categories) + len(regime_categories)
    if len(coefficients) != expected_dimension:
        raise ValueError("artifact classifier/vectorizer dimensions do not match")
    if expected_dimension <= 0 or expected_dimension > 1000:
        raise ValueError("artifact feature dimension is outside the supported range")

    training_config = payload.get("training_config") or {}
    if not isinstance(training_config, dict):
        raise ValueError("artifact training_config must be an object")
    offline_acceptance = payload.get("acceptance") or {}
    if not isinstance(offline_acceptance, dict):
        raise ValueError("artifact acceptance must be an object")
    return LoadedPromotionArtifact(
        model_version=model_version,
        target_board=target_board,
        feature_version=feature_version,
        data_version=data_version,
        artifact_path=str(path),
        artifact_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        numeric_names=tuple(numeric_names),
        route_categories=tuple(route_categories),
        regime_categories=tuple(regime_categories),
        mean=mean,
        scale=scale,
        coefficients=coefficients,
        classifier_intercept=_finite_float(classifier.get("intercept"), "classifier.intercept"),
        calibration_slope=_finite_float(calibrator.get("slope"), "calibrator.slope"),
        calibration_intercept=_finite_float(calibrator.get("intercept"), "calibrator.intercept"),
        fit_end_date=str(model.get("fit_end_date") or ""),
        calibration_end_date=str(model.get("calibration_end_date") or ""),
        training_config=dict(training_config),
        offline_acceptance=dict(offline_acceptance),
    )
