"""Stable model identity and runtime-mode helpers for promotion prediction.

The legacy implementation remains the production champion until a challenger has
completed an explicit shadow-validation and promotion workflow.  Keeping this
metadata outside the large API module makes every snapshot self-describing and
provides a single rollback switch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from math import isfinite
from numbers import Real
from typing import Any, Mapping

from app.config.settings import settings


LEGACY_PROMOTION_MODEL_VERSION = "promotion_v20260829_27_governed"
DEFAULT_PROMOTION_FEATURE_VERSION = "legacy_point_in_time_features_v2"
DEFAULT_PROMOTION_DATA_VERSION = "legacy_labels_v2_regime_freeze_20260829"

PROBABILITY_CONTRACT_VERSION = "promotion_probability_v1"
LEGACY_PROBABILITY_CONTRACT_VERSION = "legacy_probability_v0"
_PROBABILITY_FIELDS = ("p_raw", "p_calibrated", "production_probability")


class ProbabilityContractError(ValueError):
    """Missing/invalid evidence must block the whole batch, not become zero."""


def validate_probability(value: Any, *, field: str) -> float:
    """Require a finite numeric probability in [0, 1]; never coerce strings/bools."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ProbabilityContractError(f"{field}: missing_or_non_numeric")
    try:
        probability = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ProbabilityContractError(f"{field}: invalid_numeric") from None
    if not isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ProbabilityContractError(f"{field}: non_finite_or_out_of_range")
    return probability


@dataclass(frozen=True, slots=True)
class PromotionProbability:
    contract_version: str
    p_raw: float
    p_calibrated: float
    production_probability: float

    def as_payload(self) -> dict:
        return {
            "probability_contract_version": self.contract_version,
            "p_raw": self.p_raw,
            "p_calibrated": self.p_calibrated,
            "production_probability": self.production_probability,
        }


def resolve_promotion_probability(
    item: Mapping[str, Any], *, allow_legacy: bool = False
) -> PromotionProbability:
    """Read one protocol without falling back from a broken versioned payload.

    An unversioned legacy producer is accepted only at an explicitly opted-in
    boundary. Its numeric probability is the old production output; a truly
    absent raw field retains the historical raw=probability convention. This
    does not backfill or relabel any historical row as the new protocol.
    """
    version = item.get("probability_contract_version")
    has_new_fields = any(field in item for field in _PROBABILITY_FIELDS)
    if version == PROBABILITY_CONTRACT_VERSION or (
        allow_legacy and version == LEGACY_PROBABILITY_CONTRACT_VERSION and has_new_fields
    ):
        values = [validate_probability(item.get(field), field=field) for field in _PROBABILITY_FIELDS]
        return PromotionProbability(version, *values)
    if (
        not allow_legacy
        or has_new_fields
        or ("probability_contract_version" in item and version != LEGACY_PROBABILITY_CONTRACT_VERSION)
    ):
        raise ProbabilityContractError("probability_contract_version: missing_or_unsupported")
    probability = validate_probability(item.get("probability"), field="probability")
    raw = validate_probability(
        item["raw_probability"] if "raw_probability" in item else probability,
        field="raw_probability",
    )
    return PromotionProbability(LEGACY_PROBABILITY_CONTRACT_VERSION, raw, probability, probability)


def project_promotion_probability(item: Mapping[str, Any]) -> dict:
    """Validate a producer payload and freeze identical evidence at every writer.

    Versioned fields win over obsolete storage aliases. An already-frozen
    contract must agree with the producer; it cannot be silently overwritten.
    """
    probability = resolve_promotion_probability(item, allow_legacy=True)
    source_factors = item.get("probability_factors")
    if source_factors is not None and not isinstance(source_factors, Mapping):
        raise ProbabilityContractError("probability_factors: not_an_object")
    factors = dict(source_factors or {})
    if "probability_contract" in factors:
        frozen = factors["probability_contract"]
        if not isinstance(frozen, Mapping):
            raise ProbabilityContractError("probability_contract: not_an_object")
        if "probability_contract_version" not in frozen or not all(
            field in frozen for field in _PROBABILITY_FIELDS
        ):
            raise ProbabilityContractError("probability_contract: incomplete_frozen_evidence")
        if resolve_promotion_probability(frozen, allow_legacy=True) != probability:
            raise ProbabilityContractError("probability_contract: conflicting_evidence")
    factors["probability_contract"] = probability.as_payload()
    return {
        **item,
        "raw_probability": probability.p_raw,
        "probability": probability.production_probability,
        "probability_factors": factors,
    }


class PromotionRuntimeMode(StrEnum):
    """How the production API treats a challenger model."""

    LEGACY = "legacy"
    SHADOW = "shadow"
    COMPARE = "compare"
    DEPLOYED = "deployed"

    @classmethod
    def parse(cls, value: str | None) -> "PromotionRuntimeMode":
        normalized = str(value or "").strip().lower()
        try:
            return cls(normalized)
        except ValueError:
            return cls.LEGACY


@dataclass(frozen=True, slots=True)
class PromotionModelIdentity:
    runtime_mode: PromotionRuntimeMode
    champion_model_version: str
    challenger_model_version: str | None
    feature_version: str
    data_version: str

    @property
    def active_model_version(self) -> str:
        """The version allowed to drive the production response."""

        return self.champion_model_version

    @property
    def challenger_enabled(self) -> bool:
        return bool(
            self.challenger_model_version
            and self.runtime_mode in {PromotionRuntimeMode.SHADOW, PromotionRuntimeMode.COMPARE}
        )

    def as_payload(self) -> dict:
        payload = asdict(self)
        payload["runtime_mode"] = self.runtime_mode.value
        payload["active_model_version"] = self.active_model_version
        payload["challenger_enabled"] = self.challenger_enabled
        payload["production_source"] = "champion"
        return payload


def _clean(value: str | None, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def get_promotion_model_identity() -> PromotionModelIdentity:
    """Resolve model metadata from settings with safe legacy defaults."""

    challenger = str(settings.PROMOTION_CHALLENGER_MODEL_VERSION or "").strip() or None
    return PromotionModelIdentity(
        runtime_mode=PromotionRuntimeMode.parse(settings.PROMOTION_RUNTIME_MODE),
        champion_model_version=_clean(
            settings.PROMOTION_CHAMPION_MODEL_VERSION,
            LEGACY_PROMOTION_MODEL_VERSION,
        ),
        challenger_model_version=challenger,
        feature_version=_clean(
            settings.PROMOTION_FEATURE_VERSION,
            DEFAULT_PROMOTION_FEATURE_VERSION,
        ),
        data_version=_clean(
            settings.PROMOTION_DATA_VERSION,
            DEFAULT_PROMOTION_DATA_VERSION,
        ),
    )
