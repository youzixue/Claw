"""Canonical identity helpers for point-in-time promotion snapshots."""

from __future__ import annotations

import re
from datetime import datetime


def normalize_snapshot_source(source: str | None) -> str:
    normalized = str(source or "").strip().lower()
    return "schedule" if normalized in {"schedule", "scheduler", "cron"} else "page"


def normalize_snapshot_context(context: str | None, *, source: str = "") -> str:
    normalized = str(context or "").strip().lower()
    compact = re.sub(r"[^0-9]", "", normalized)
    if normalized.startswith("promotion_"):
        return normalized
    if compact in {"1510", "2000", "0925", "0935", "925", "935"}:
        return f"promotion_{compact.zfill(4)}"
    return normalized or source


def parse_snapshot_recorded_at(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def snapshot_batch_key_from_factors(factors: dict) -> str:
    explicit_key = str(factors.get("prediction_snapshot_batch_key") or "").strip()
    if explicit_key:
        return explicit_key
    source = normalize_snapshot_source(factors.get("prediction_snapshot_source"))
    context = str(factors.get("prediction_snapshot_context") or source).strip()
    recorded_at = str(factors.get("prediction_snapshot_recorded_at") or "").strip()
    return f"{source}:{context}:{recorded_at}"
