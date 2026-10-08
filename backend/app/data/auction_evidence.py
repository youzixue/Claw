"""Auction provenance: receipt labels and positive spot fields are not proof.

Legacy rows remain unknown. This contract does not assert that any currently
configured public adapter supplies indicative matched price/quantity.
"""
from datetime import datetime, time
import hashlib
import json
import math

from numpy import bool_

from app.config.settings import settings
from app.data.fund_flow_clock import local_clock


def auction_source_frame_key(row) -> str:
    """Storage identity, not authorization; re-fetching a quote is not a new frame.

    Missing provider clocks remain missing. Only in that unknown case does the
    actual observation identify storage; it never becomes source_quote_at.
    """
    source = str(getattr(row, "source", None) or "")
    version = str(getattr(row, "source_version", None) or "")
    source_clock = local_clock(getattr(row, "source_quote_at", None))
    observed = local_clock(getattr(row, "observed_at", None))
    has_source_identity = source_clock is not None and bool(source and version)
    clock = source_clock if has_source_identity else observed
    parts = [
        "auction_source_frame_v1", str(row.code), str(row.trade_date),
        source, version, "source" if has_source_identity else "observation_unknown_source",
        clock.isoformat() if clock is not None else "",
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def auction_latest_order():
    """Deterministic latest row; never fall back to older higher-quality evidence."""
    from app.models.stock import AuctionData
    return (
        AuctionData.auction_time.desc(),
        AuctionData.observed_at.desc().nulls_last(),
        AuctionData.received_at.desc().nulls_last(),
        AuctionData.source_quote_at.desc().nulls_last(),
        AuctionData.id.desc(),
    )


def latest_auction_ids(trade_date, *, end_time="09:25:30", conditions=()):
    """Latest-per-code SQL identity shared by health, ranking and factor readers."""
    from sqlalchemy import func, select
    from app.models.stock import AuctionData
    ranked = select(
        AuctionData.id.label("id"),
        func.row_number().over(partition_by=AuctionData.code,
                               order_by=auction_latest_order()).label("position"),
    ).where(
        AuctionData.trade_date == trade_date,
        AuctionData.auction_time.between("09:15:00", end_time),
        *conditions,
    ).subquery()
    return select(ranked.c.id).where(ranked.c.position == 1)


def auction_context_complete(context) -> bool:
    """Only accept a derived context stamped by the current provenance gate.

    Older persisted 'complete=true' or missing flags cannot regain executable
    status during ranking; this never substitutes for validating source rows.
    """
    return bool(
        isinstance(context, dict)
        and context.get("auction_feed_complete") is True
        and context.get("auction_evidence_status") == "ok"
        and context.get("auction_evidence_contract") == "auction_provenance_v1"
    )


def positive_number(value) -> bool:
    if isinstance(value, (bool, bool_)):
        return False
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def volume_in_shares(value, unit) -> float | None:
    """Convert declared units only; never infer hands/shares from magnitude."""
    if not positive_number(value) or not isinstance(unit, str):
        return None
    multiplier = {"share": 1, "lot100": 100}.get(unit)
    if multiplier is None:
        return None
    shares = float(value) * multiplier
    return shares if math.isfinite(shares) else None


def auction_evidence_status(row, *, decision_at=None, require_ratio=True) -> str:
    """Validate original same-day provenance, also for historical read-only use.

    Freshness is measured at original observation, not against today's clock.
    A decision cutoff still rejects any evidence unavailable at that decision.
    """
    if row is None:
        return "unknown"
    source, receipt, observed = (
        local_clock(getattr(row, key, None))
        for key in ("source_quote_at", "received_at", "observed_at")
    )
    decision = local_clock(decision_at if decision_at is not None else datetime.now())
    if decision is None:
        return "unknown"
    if any(value is not None and value > decision for value in (source, receipt, observed)):
        return "future"
    if any(value is None for value in (source, receipt, observed)):
        return "unknown"
    if not getattr(row, "source", None) or not getattr(row, "source_version", None):
        return "unknown"
    trade_date = getattr(row, "trade_date", None)
    if (
        any(value.date() != trade_date for value in (source, receipt, observed))
        or not source <= receipt <= observed
        or any(not time(9, 15) <= value.time() <= time(9, 25, 30)
               for value in (source, receipt, observed))
        or getattr(row, "auction_time", None) != observed.strftime("%H:%M:%S")
    ):
        return "invalid_clock"
    max_age = settings.AUCTION_SOURCE_MAX_AGE_SEC
    if not positive_number(max_age) or (observed - source).total_seconds() > max_age:
        return "stale_source"
    price_basis = getattr(row, "price_basis", None)
    volume_basis = getattr(row, "volume_basis", None)
    basis_ok = (
        price_basis == "indicative_match"
        and volume_basis == "indicative_matched"
        and source.time() < time(9, 25)
    ) or (
        price_basis == "auction_opening"
        and volume_basis == "auction_matched"
        and source.time() >= time(9, 25)
    )
    if not basis_ok:
        return "unverified_basis"
    volume_unit = getattr(row, "volume_unit", None)
    if (
        not isinstance(volume_unit, str)
        or volume_unit not in {"share", "lot100"}
        or getattr(row, "amount_unit", None) != "CNY"
    ):
        return "unknown_unit"
    fields = ["auction_price", "prev_close", "auction_volume", "auction_amount"]
    if require_ratio:
        fields.append("volume_ratio")
    if not all(positive_number(getattr(row, key, None)) for key in fields):
        return "incomplete_values"
    # The quantity consumed by a path must remain finite after unit conversion.
    # A positive raw lot count alone does not establish a usable share count.
    if volume_in_shares(getattr(row, "auction_volume", None), volume_unit) is None:
        return "incomplete_values"
    return "ok"


# Diagnostics only: never substitute missing fields or alter the gate above.
_DIAGNOSTIC_FIELDS = ("auction_price", "prev_close", "auction_volume", "auction_amount", "volume_ratio")
_DIAGNOSTIC_STATUSES = ("incomplete_values", "invalid_clock", "unverified_basis", "unknown_unit")


def diagnose_missing_evidence_fields(row, *, decision_at=None) -> dict:
    """Name missing inputs, without logging raw vendor payloads or numeric values."""
    status = auction_evidence_status(row, decision_at=decision_at)
    code = str(getattr(row, "code", "") or "")
    safe_code = len(code) == 6 and code.isascii() and code.isdigit()
    return {
        "status": status,
        "missing_positive_fields": {
            key: 1 for key in _DIAGNOSTIC_FIELDS
            if row is not None and not positive_number(getattr(row, key, None))
        },
        "missing_volume_unit": row is not None and (
            getattr(row, "volume_unit", None) not in ("share", "lot100")
            or getattr(row, "amount_unit", None) != "CNY"
        ),
        **{f"sample_codes_for_{key}": [code] if safe_code and status == key else []
           for key in _DIAGNOSTIC_STATUSES},
        "sample_size_cap": 5,
    }


def merge_diagnoses(diagnoses: list[dict]) -> dict:
    """Bounded samples and aggregate counts, not a second evidence contract."""
    merged = {"missing_positive_fields": {}, "missing_volume_unit_rows": 0,
              "sample_size_cap": 5,
              **{f"sample_codes_for_{key}": [] for key in _DIAGNOSTIC_STATUSES}}
    for item in diagnoses:
        for field, count in item["missing_positive_fields"].items():
            counts = merged["missing_positive_fields"]
            counts[field] = counts.get(field, 0) + count
        merged["missing_volume_unit_rows"] += int(item["missing_volume_unit"])
        for status in _DIAGNOSTIC_STATUSES:
            key = f"sample_codes_for_{status}"
            for code in item[key]:
                if len(merged[key]) < 5 and code not in merged[key]:
                    merged[key].append(code)
    return merged
