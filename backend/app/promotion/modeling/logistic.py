"""Small deterministic NumPy logistic model with out-of-fold calibration."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


_EPSILON = 1e-7


def sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def logit(probabilities: np.ndarray) -> np.ndarray:
    clipped = np.clip(probabilities, _EPSILON, 1.0 - _EPSILON)
    return np.log(clipped / (1.0 - clipped))


@dataclass(slots=True)
class LogisticBinaryClassifier:
    l2: float = 0.02
    learning_rate: float = 0.03
    max_iter: int = 250
    tolerance: float = 1e-7
    positive_weight: float | None = None
    coefficients: np.ndarray | None = None
    intercept: float = 0.0
    iterations: int = 0
    final_loss: float | None = None

    def fit(self, matrix: np.ndarray, labels: np.ndarray) -> "LogisticBinaryClassifier":
        x = np.asarray(matrix, dtype=float)
        y = np.asarray(labels, dtype=float)
        if x.ndim != 2 or y.ndim != 1 or len(x) != len(y):
            raise ValueError("invalid feature/label shape")
        if len(y) < 2 or len(np.unique(y)) < 2:
            raise ValueError("binary classifier requires both outcome classes")

        rows, columns = x.shape
        weights = np.zeros(columns, dtype=float)
        base_rate = float(np.clip(np.mean(y), _EPSILON, 1.0 - _EPSILON))
        intercept = float(np.log(base_rate / (1.0 - base_rate)))
        positive_weight = self.positive_weight
        if positive_weight is None:
            positives = max(float(np.sum(y)), 1.0)
            negatives = max(float(rows - np.sum(y)), 1.0)
            positive_weight = min(max(float(np.sqrt(negatives / positives)), 1.0), 6.0)
        sample_weights = np.where(y > 0.5, positive_weight, 1.0)
        sample_weights = sample_weights / np.mean(sample_weights)

        first_moment = np.zeros_like(weights)
        second_moment = np.zeros_like(weights)
        intercept_first = 0.0
        intercept_second = 0.0
        previous_loss = float("inf")
        for iteration in range(1, self.max_iter + 1):
            probabilities = sigmoid(x @ weights + intercept)
            error = (probabilities - y) * sample_weights
            gradient = (x.T @ error) / rows + self.l2 * weights
            intercept_gradient = float(np.mean(error))

            # Adam keeps the pure-NumPy optimizer stable across differently sized
            # walk-forward folds while remaining deterministic.
            first_moment = 0.9 * first_moment + 0.1 * gradient
            second_moment = 0.999 * second_moment + 0.001 * (gradient * gradient)
            intercept_first = 0.9 * intercept_first + 0.1 * intercept_gradient
            intercept_second = 0.999 * intercept_second + 0.001 * (
                intercept_gradient * intercept_gradient
            )
            first_hat = first_moment / (1.0 - 0.9**iteration)
            second_hat = second_moment / (1.0 - 0.999**iteration)
            intercept_first_hat = intercept_first / (1.0 - 0.9**iteration)
            intercept_second_hat = intercept_second / (1.0 - 0.999**iteration)
            weights -= self.learning_rate * first_hat / (np.sqrt(second_hat) + 1e-8)
            intercept -= self.learning_rate * intercept_first_hat / (
                np.sqrt(intercept_second_hat) + 1e-8
            )

            updated_probabilities = sigmoid(x @ weights + intercept)
            clipped = np.clip(
                updated_probabilities, _EPSILON, 1.0 - _EPSILON
            )
            loss = float(
                np.mean(
                    sample_weights
                    * (-(y * np.log(clipped) + (1.0 - y) * np.log(1.0 - clipped)))
                )
                + 0.5 * self.l2 * float(weights @ weights)
            )
            if abs(previous_loss - loss) <= self.tolerance:
                self.iterations = iteration
                previous_loss = loss
                break
            previous_loss = loss
            self.iterations = iteration

        self.coefficients = weights
        self.intercept = intercept
        self.positive_weight = float(positive_weight)
        self.final_loss = previous_loss
        return self

    def predict_proba(self, matrix: np.ndarray) -> np.ndarray:
        if self.coefficients is None:
            raise RuntimeError("classifier is not fitted")
        return sigmoid(np.asarray(matrix, dtype=float) @ self.coefficients + self.intercept)

    def payload(self) -> dict:
        if self.coefficients is None:
            raise RuntimeError("classifier is not fitted")
        return {
            "algorithm": "numpy_logistic_regression",
            "coefficients": self.coefficients.tolist(),
            "intercept": self.intercept,
            "l2": self.l2,
            "learning_rate": self.learning_rate,
            "positive_weight": self.positive_weight,
            "iterations": self.iterations,
            "final_loss": self.final_loss,
        }


@dataclass(slots=True)
class PlattCalibrator:
    slope: float = 1.0
    intercept: float = 0.0
    max_iter: int = 100
    l2: float = 0.001
    tolerance: float = 1e-9

    def fit(self, probabilities: np.ndarray, labels: np.ndarray) -> "PlattCalibrator":
        raw = np.asarray(probabilities, dtype=float)
        y = np.asarray(labels, dtype=float)
        if raw.ndim != 1 or y.ndim != 1 or len(raw) != len(y):
            raise ValueError("invalid calibration probability/label shape")
        if len(y) < 2:
            raise ValueError("calibrator requires at least two samples")
        if not np.all(np.isfinite(raw)) or not np.all(np.isfinite(y)):
            raise ValueError("calibration data must be finite")
        if np.any((y < 0.0) | (y > 1.0)):
            raise ValueError("calibration labels must be in [0, 1]")
        if len(y) < 2 or len(np.unique(y)) < 2:
            self.slope = 1.0
            self.intercept = 0.0
            return self

        scores = logit(raw)
        base_rate = float(np.clip(np.mean(y), _EPSILON, 1.0 - _EPSILON))
        raw_rate = float(np.clip(np.mean(raw), _EPSILON, 1.0 - _EPSILON))
        slope = 1.0
        # Class weighting in the ranking model deliberately changes the fitted
        # prior.  Starting Platt's intercept at the observed prior correction
        # avoids hundreds of tiny gradient steps before calibration even begins.
        intercept = float(
            np.log(base_rate / (1.0 - base_rate))
            - np.log(raw_rate / (1.0 - raw_rate))
        )

        def objective(candidate_slope: float, candidate_intercept: float) -> float:
            logits = candidate_slope * scores + candidate_intercept
            cross_entropy = np.mean(np.logaddexp(0.0, logits) - y * logits)
            regularization = 0.5 * self.l2 * (candidate_slope - 1.0) ** 2
            return float(cross_entropy + regularization)

        for _iteration in range(max(int(self.max_iter), 1)):
            predicted = sigmoid(slope * scores + intercept)
            error = predicted - y
            variance = np.maximum(predicted * (1.0 - predicted), 1e-9)
            gradient = np.asarray(
                [
                    np.mean(error * scores) + self.l2 * (slope - 1.0),
                    np.mean(error),
                ],
                dtype=float,
            )
            hessian = np.asarray(
                [
                    [
                        np.mean(variance * scores * scores) + self.l2,
                        np.mean(variance * scores),
                    ],
                    [np.mean(variance * scores), np.mean(variance) + 1e-9],
                ],
                dtype=float,
            )
            try:
                newton_step = np.linalg.solve(hessian, gradient)
            except np.linalg.LinAlgError:
                newton_step = np.linalg.lstsq(hessian, gradient, rcond=None)[0]
            if not np.all(np.isfinite(newton_step)):
                break

            current_loss = objective(slope, intercept)
            directional_improvement = float(gradient @ newton_step)
            step_scale = 1.0
            accepted = False
            next_slope = slope
            next_intercept = intercept
            for _line_search in range(24):
                candidate_slope = float(
                    np.clip(slope - step_scale * newton_step[0], 0.05, 8.0)
                )
                candidate_intercept = float(
                    np.clip(intercept - step_scale * newton_step[1], -12.0, 12.0)
                )
                candidate_loss = objective(candidate_slope, candidate_intercept)
                if candidate_loss <= current_loss - (
                    1e-4 * step_scale * max(directional_improvement, 0.0)
                ):
                    next_slope = candidate_slope
                    next_intercept = candidate_intercept
                    accepted = True
                    break
                step_scale *= 0.5
            if not accepted:
                break
            parameter_change = max(
                abs(next_slope - slope), abs(next_intercept - intercept)
            )
            slope = next_slope
            intercept = next_intercept
            if parameter_change <= self.tolerance * (
                1.0 + max(abs(slope), abs(intercept))
            ):
                break
        self.slope = float(np.clip(slope, 0.05, 8.0))
        self.intercept = float(np.clip(intercept, -12.0, 12.0))
        return self

    def transform(self, probabilities: np.ndarray) -> np.ndarray:
        return sigmoid(self.slope * logit(np.asarray(probabilities, dtype=float)) + self.intercept)

    def payload(self) -> dict:
        return {
            "method": "platt_out_of_time_tail",
            "slope": self.slope,
            "intercept": self.intercept,
            "l2": self.l2,
        }
