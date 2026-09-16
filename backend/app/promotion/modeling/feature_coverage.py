"""Fail-closed hist feature evidence without changing legacy non-hist imputation.

This validates a frozen declaration, not raw archives or vendor authenticity.
Only a future materializer may produce this contract; M0 never backfills it.
Historical pretraining can explicitly bypass *evidence*, never finite values.
Loaded-artifact inference has no such bypass.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from numbers import Real
from typing import Any, Iterable, Mapping

HIST_MATERIALIZATION_VERSION = "promotion_hist_materialization_v1"
HIST_COVERAGE_VERSION = "promotion_hist_coverage_v1"


def hist_values_digest(values: Mapping[str, Any]) -> str:
    payload = {k: float(v) for k, v in values.items() if k.startswith("hist_")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _clock(value) -> datetime | None:
    if isinstance(value, str):
        if len(value) < 19 or value[10] not in {"T", " "}:
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def _hash(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def hist_feature_coverage(
    values: Mapping[str, Any], required_names: Iterable[str], *,
    materialization: dict | None, code: str, trade_date: str,
    as_of_at: datetime | None, feature_version: str,
    allow_unmaterialized_pretraining: bool = False,
) -> dict:
    required = sorted({name for name in required_names if name.startswith("hist_")})
    errors = []
    invalid = [name for name in required if name not in values or isinstance(values.get(name), bool)
               or not isinstance(values.get(name), Real) or not math.isfinite(float(values[name]))]
    if invalid:
        errors.append({"reason": "missing_or_invalid_hist_values", "fields": invalid})
    if not required:
        return {"version": HIST_COVERAGE_VERSION, "passed": True, "required": [], "errors": []}
    if allow_unmaterialized_pretraining:
        return {"version": HIST_COVERAGE_VERSION, "passed": not errors, "required": required,
                "errors": errors, "evidence_status": "unverified_pretraining_only"}
    proof = materialization if isinstance(materialization, dict) else {}
    if proof.get("schema_version") != HIST_MATERIALIZATION_VERSION:
        errors.append({"reason": "missing_or_unsupported_hist_materialization"})
    if (proof.get("code") != code or proof.get("prediction_trade_date") != trade_date
            or proof.get("feature_version") != feature_version):
        errors.append({"reason": "hist_materialization_identity_mismatch"})
    cutoff = _clock(as_of_at)
    declared_cutoff = _clock(proof.get("as_of_at"))
    materialized = _clock(proof.get("materialized_at"))
    if cutoff is None or declared_cutoff != cutoff or materialized is None or materialized > cutoff:
        errors.append({"reason": "hist_materialization_clock_invalid"})
    try:
        content_matches = _hash(proof.get("values_sha256")) and proof["values_sha256"] == hist_values_digest(values)
    except (TypeError, ValueError, OverflowError):
        content_matches = False
    if not content_matches:
        errors.append({"reason": "hist_materialization_values_hash_mismatch"})
    fields = proof.get("fields") if isinstance(proof.get("fields"), dict) else {}
    for name in required:
        field = fields.get(name)
        if not isinstance(field, dict):
            errors.append({"reason": "hist_field_provenance_missing", "field": name})
            continue
        clocks = [_clock(field.get(key)) for key in ("source_at", "received_at", "observed_at")]
        if (field.get("status") != "ok" or not field.get("source") or not field.get("source_version")
                or not _hash(field.get("source_manifest_sha256"))):
            errors.append({"reason": "hist_field_provenance_unverified", "field": name})
        if (any(clock is None for clock in clocks) or materialized is None
                or not clocks[0] <= clocks[1] <= clocks[2] <= materialized):
            errors.append({"reason": "hist_field_clock_invalid", "field": name})
    return {"version": HIST_COVERAGE_VERSION, "passed": not errors,
            "required": required, "errors": errors, "evidence_status": "validated_declaration_only"}


def require_hist_feature_coverage(rows, required_names, *, feature_version: str,
                                  allow_unmaterialized_pretraining: bool = False) -> None:
    required_names = tuple(required_names)
    for row in rows:
        result = hist_feature_coverage(
            row.values, required_names, materialization=row.hist_materialization,
            code=row.code, trade_date=row.trade_date, as_of_at=row.feature_as_of_at,
            feature_version=feature_version,
            allow_unmaterialized_pretraining=allow_unmaterialized_pretraining,
        )
        if not result["passed"]:
            raise ValueError(f"hist feature coverage failed for {row.code}: {result['errors']}")
