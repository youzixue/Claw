"""Explicit point-in-time feature contract for promotion challengers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.promotion.modeling.feature_coverage import require_hist_feature_coverage

import numpy as np


FEATURE_VERSION = "promotion_features_v3_regime_point_in_time"

# Only fields derived from information observable at the declared snapshot are
# eligible. Model outputs, historical-learning aggregates, ranks and labels are
# intentionally excluded to prevent teacher/target leakage.
COMMON_NUMERIC_FEATURES = (
    "market_main_net_inflow",
    "market_first_board_count",
    "market_broad_first_board_overflow",
    "sector_continuity_score",
    "sector_rotation_score",
    "sector_strength_delta",
    "sector_limit_up_delta",
    "distribution_penalty",
    "route_score",
    "memory_score",
    "platform_score",
    "theme_score",
    "trigger_score",
    "market_score",
    "risk_penalty",
    "kline_confirmation_score",
    "support_squeeze_signal_score",
    "support_volume_release_signal_score",
    "support_zone_gap_pct",
    "support_hold_days",
    "support_rebound_count",
    "strict_confirmation_count",
    "volume_suffocation_ratio",
    "overhead_gap_pct",
    "failed_reversal_count",
    "close_strength_pct",
    "news_catalyst_score",
    "news_count",
    "auction_strength_score",
    "auction_open_change",
    "auction_volume_ratio",
    "auction_amount",
    "trend_breakout_gap_pct",
    "platform_confirmation_count",
    "trend_confirmation_count",
    "pre_board_probe_score",
    "oversold_reversal_score",
    "intraday_high_pct",
    "upper_gap_pct",
    "lower_gap_pct",
    "close_position",
    "limit_probe_shape_score",
    "burst_pullback_score",
    "burst_volume_ratio",
    "burst_pullback_depth_pct",
    "burst_shrink_ratio",
    "burst_rebound_pct",
    "burst_high_gap_pct",
    "burst_restart_days",
    "burst_restart_volume_ratio",
    "main_net_inflow",
    "early_seal_bonus",
    "turnover_bonus",
    "market_regime_penalty",
    "market_avg_break_count",
    "market_broken_ratio",
    "dragon_tiger_score",
    "dragon_tiger_probability_delta",
    "second_board_style_score",
    "support_strength_score",
    "circ_market_cap",
    # Reconstructable daily-bar features used by the multi-year historical panel.
    "hist_return_1d",
    "hist_return_3d",
    "hist_return_5d",
    "hist_return_10d",
    "hist_return_20d",
    "hist_volatility_5d",
    "hist_volatility_20d",
    "hist_volume_ratio_5d",
    "hist_volume_ratio_20d",
    "hist_amount_ratio_5d",
    "hist_turnover",
    "hist_amplitude",
    "hist_close_position",
    "hist_gap_pct",
    "hist_ma_gap_5d",
    "hist_ma_gap_10d",
    "hist_ma_gap_20d",
    "hist_ma_gap_60d",
    "hist_high_gap_20d",
    "hist_high_gap_60d",
    "hist_drawdown_20d",
    "hist_drawdown_60d",
    "hist_limit_hits_5d",
    "hist_limit_hits_20d",
    "hist_limit_hits_60d",
    "hist_days_since_limit_up",
    "hist_fund_main_net_inflow",
    "hist_fund_main_net_inflow_pct",
    "hist_fund_3d_sum",
    "hist_market_advance_ratio",
    "hist_market_limit_up_count",
    "hist_market_median_return",
    "hist_market_return_dispersion",
)

COMMON_BOOLEAN_FEATURES = (
    "sector_low_position_rotation",
    "sector_crowded_stale_theme",
    "has_high_board_risk",
    "has_recent_limit_up_event",
    "platform_relaunch_event_ready",
    "limit_up_nearby_doji_confirmation",
    "support_squeeze_ready",
    "support_squeeze_only_ready",
    "support_volume_release_ready",
    "lower_shadow_absorption",
    "has_support_absorption",
    "has_doji_confirmation",
    "qualified_doji_confirmation",
    "has_platform_contraction",
    "has_volume_contraction",
    "has_volume_suffocation",
    "strict_ready",
    "auction_cancelled",
    "near_trend_high",
    "trend_breakout",
    "trend_acceleration_ready",
    "ma_alignment",
    "trend_volume_release",
    "limit_probe",
    "preheat_breakout",
    "washout_reversal",
    "panic_flush",
    "limit_probe_shape_supportive",
    "limit_probe_shape_failed",
    "burst_pullback_restart_ready",
    "touch_board_pullback",
    "trend_preheat",
    "panic_repair",
    "dragon_tiger_listed",
)

PROHIBITED_KEY_TOKENS = (
    "actual_",
    "outcome",
    "future",
    "label",
    "success",
    "failure",
    "learning_",
    "prediction_",
    "next_day",
    "sub_probabilities",
)


@dataclass(frozen=True, slots=True)
class FeatureRow:
    code: str
    trade_date: str
    target_board: int
    label: int
    baseline_probability: float
    candidate_route: str
    values: dict[str, Any]
    market_regime: str = "unknown"
    hist_materialization: dict | None = None
    feature_as_of_at: datetime | None = None


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if np.isfinite(parsed) else 0.0


def validate_feature_contract() -> None:
    invalid = [
        key
        for key in (*COMMON_NUMERIC_FEATURES, *COMMON_BOOLEAN_FEATURES)
        if any(token in key.lower() for token in PROHIBITED_KEY_TOKENS)
    ]
    if invalid:
        raise ValueError(f"point-in-time feature contract contains prohibited keys: {invalid}")


def extract_point_in_time_features(factors: dict) -> dict[str, Any]:
    validate_feature_contract()
    values = {
        # Preserve invalid hist values for the required-feature guard; never turn
        # unknown into zero. Non-hist legacy behavior intentionally stays frozen.
        key: factors[key] if key.startswith("hist_") else _number(factors.get(key))
        for key in COMMON_NUMERIC_FEATURES
        if key in factors
    }
    values.update(
        {
            key: 1.0 if bool(factors.get(key)) else 0.0
            for key in COMMON_BOOLEAN_FEATURES
            if key in factors
        }
    )
    return values


class FeatureVectorizer:
    """Fit-only-on-training standardizer and candidate-route encoder."""

    def __init__(self, *, allow_unmaterialized_pretraining: bool = False) -> None:
        self.allow_unmaterialized_pretraining = allow_unmaterialized_pretraining
        self.numeric_names: list[str] = []
        self.route_categories: list[str] = []
        self.regime_categories: list[str] = []
        self.mean: np.ndarray | None = None
        self.scale: np.ndarray | None = None

    @property
    def feature_names(self) -> list[str]:
        return (
            self.numeric_names
            + [f"route={route}" for route in self.route_categories]
            + [f"regime={regime}" for regime in self.regime_categories]
        )

    def fit(self, rows: list[FeatureRow]) -> "FeatureVectorizer":
        if not rows:
            raise ValueError("cannot fit feature vectorizer without rows")
        route_values = sorted(
            {row.candidate_route for row in rows if row.candidate_route}
        )
        self.route_categories = route_values if len(route_values) > 1 else []
        regime_values = sorted(
            {
                row.market_regime
                for row in rows
                if row.market_regime and row.market_regime != "unknown"
            }
        )
        self.regime_categories = regime_values if len(regime_values) > 1 else []
        present_names = set().union(*(row.values.keys() for row in rows))
        contract_names = list(COMMON_NUMERIC_FEATURES) + list(COMMON_BOOLEAN_FEATURES)
        candidate_names = [name for name in contract_names if name in present_names]
        require_hist_feature_coverage(rows, candidate_names, feature_version=FEATURE_VERSION,
                                     allow_unmaterialized_pretraining=self.allow_unmaterialized_pretraining)
        if candidate_names:
            full_matrix = np.asarray(
                [[row.values.get(name, 0.0) for name in candidate_names] for row in rows],
                dtype=float,
            )
            full_scale = np.nanstd(full_matrix, axis=0)
            active_indexes = [
                index for index, value in enumerate(full_scale) if value >= 1e-9
            ]
            self.numeric_names = [candidate_names[index] for index in active_indexes]
            matrix = full_matrix[:, active_indexes]
            self.mean = np.nanmean(matrix, axis=0)
            self.scale = np.nanstd(matrix, axis=0)
        else:
            self.numeric_names = []
            self.mean = np.asarray([], dtype=float)
            self.scale = np.asarray([], dtype=float)
        return self

    def transform(self, rows: list[FeatureRow]) -> np.ndarray:
        if self.mean is None or self.scale is None:
            raise RuntimeError("feature vectorizer is not fitted")
        require_hist_feature_coverage(rows, self.numeric_names, feature_version=FEATURE_VERSION,
                                     allow_unmaterialized_pretraining=self.allow_unmaterialized_pretraining)
        numeric = np.asarray(
            [[row.values.get(name, 0.0) for name in self.numeric_names] for row in rows],
            dtype=float,
        )
        numeric = np.nan_to_num((numeric - self.mean) / self.scale, nan=0.0, posinf=0.0, neginf=0.0)
        matrices = [numeric]
        if self.route_categories:
            route_index = {
                route: index for index, route in enumerate(self.route_categories)
            }
            routes = np.zeros((len(rows), len(self.route_categories)), dtype=float)
            for row_index, row in enumerate(rows):
                column = route_index.get(row.candidate_route)
                if column is not None:
                    routes[row_index, column] = 1.0
            matrices.append(routes)
        if self.regime_categories:
            regime_index = {
                regime: index for index, regime in enumerate(self.regime_categories)
            }
            regimes = np.zeros((len(rows), len(self.regime_categories)), dtype=float)
            for row_index, row in enumerate(rows):
                column = regime_index.get(row.market_regime)
                if column is not None:
                    regimes[row_index, column] = 1.0
            matrices.append(regimes)
        return matrices[0] if len(matrices) == 1 else np.concatenate(matrices, axis=1)

    def fit_transform(self, rows: list[FeatureRow]) -> np.ndarray:
        return self.fit(rows).transform(rows)

    def payload(self) -> dict:
        if self.mean is None or self.scale is None:
            raise RuntimeError("feature vectorizer is not fitted")
        return {
            "feature_version": FEATURE_VERSION,
            "numeric_names": self.numeric_names,
            "route_categories": self.route_categories,
            "regime_categories": self.regime_categories,
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "feature_names": self.feature_names,
        }
