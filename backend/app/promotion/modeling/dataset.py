"""Legacy compatibility-record research builder, NOT immutable training evidence.

Production-source challenger training uses ledger_dataset.build_ledger_training_dataset.
This legacy adapter is retained for old research callers and tests only.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.trade_calendar import is_official_closed_day
from app.models.regime import MarketRegimeSnapshot
from app.models.signal import PromotionPredictionRecord
from app.models.stock import LimitUpPool, StockKline
from app.promotion.labels import PROMOTION_LABEL_VERSION, promotion_event_label
from app.promotion.modeling.features import FeatureRow, extract_point_in_time_features
from app.promotion.regime import REGIME_LABELS
from app.promotion.snapshot_identity import (
    normalize_snapshot_context,
    normalize_snapshot_source,
    parse_snapshot_recorded_at,
)


@dataclass(frozen=True, slots=True)
class DatasetBundle:
    rows: list[FeatureRow]
    diagnostics: dict
    data_version: str
    start_date: date | None
    end_date: date | None


def _loads(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _record_source(record: PromotionPredictionRecord, factors: dict) -> str:
    explicit = str(record.snapshot_source or "").strip()
    return normalize_snapshot_source(
        explicit
        if explicit and explicit != "legacy"
        else factors.get("prediction_snapshot_source")
    )


def _record_context(record: PromotionPredictionRecord, factors: dict) -> str:
    explicit = str(record.snapshot_context or "").strip()
    if explicit and explicit != "legacy":
        return normalize_snapshot_context(explicit)
    source = _record_source(record, factors)
    return normalize_snapshot_context(
        factors.get("prediction_snapshot_context"), source=source
    )


def _recorded_at(record: PromotionPredictionRecord, factors: dict) -> datetime:
    if isinstance(record.snapshot_recorded_at, datetime):
        return record.snapshot_recorded_at
    return (
        parse_snapshot_recorded_at(factors.get("prediction_snapshot_recorded_at"))
        or record.created_at
        or datetime.min
    )


def _data_version(
    rows: list[tuple[PromotionPredictionRecord, dict, date, int]],
) -> str:
    payload = PROMOTION_LABEL_VERSION + "|" + "|".join(
        f"{record.id}:{record.code}:{record.prediction_trade_date}:{outcome_date}:{label}:"
        f"{record.model_version or factors.get('prediction_model_version') or 'legacy'}"
        for record, factors, outcome_date, label in rows
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"promotion_labels_{len(rows)}_{digest}"


async def build_training_dataset(
    db: AsyncSession,
    *,
    target_board: int,
    start_date: date | None = None,
    end_date: date | None = None,
    snapshot_context: str = "promotion_2000",
) -> DatasetBundle:
    """Return deduplicated, authoritative-horizon samples.

    Features come only from the factors frozen at prediction time. Labels are
    rebuilt from valid-date LimitUpPool events using StockKline dates as the
    session clock; stale legacy outcome fields are diagnostics, not truth.
    """

    statement = select(PromotionPredictionRecord).where(
        PromotionPredictionRecord.target_board == int(target_board)
    )
    if start_date is not None:
        statement = statement.where(
            PromotionPredictionRecord.prediction_trade_date >= start_date
        )
    if end_date is not None:
        statement = statement.where(
            PromotionPredictionRecord.prediction_trade_date <= end_date
        )
    records = list((await db.execute(statement)).scalars())
    diagnostics = {
        "dataset_source": "legacy_compatibility_records",
        "immutable_ledger_evidence": False,
        "loaded_records": len(records),
        "excluded_non_schedule": 0,
        "excluded_context": 0,
        "excluded_bad_cutoff": 0,
        "excluded_incomplete_horizon": 0,
        "repaired_outcome_clock": 0,
        "outcome_label_disagreement": 0,
        "deduplicated_records": 0,
    }

    eligible: list[tuple[PromotionPredictionRecord, dict]] = []
    for record in records:
        factors = _loads(record.factors_json)
        if _record_source(record, factors) != "schedule":
            diagnostics["excluded_non_schedule"] += 1
            continue
        if _record_context(record, factors) != snapshot_context:
            diagnostics["excluded_context"] += 1
            continue
        recorded_at = _recorded_at(record, factors)
        if recorded_at.date() > record.prediction_trade_date:
            diagnostics["excluded_bad_cutoff"] += 1
            continue
        eligible.append((record, factors))

    if not eligible:
        return DatasetBundle(
            rows=[],
            diagnostics={**diagnostics, "eligible_records": 0},
            data_version="promotion_labels_empty",
            start_date=None,
            end_date=None,
        )

    minimum_prediction_date = min(
        record.prediction_trade_date for record, _ in eligible
    )
    latest_kline_date = await db.scalar(select(func.max(StockKline.trade_date)))
    if latest_kline_date is None:
        return DatasetBundle(
            rows=[],
            diagnostics={
                **diagnostics,
                "eligible_records": 0,
                "excluded_incomplete_horizon": len(eligible),
            },
            data_version="promotion_labels_no_market_clock",
            start_date=None,
            end_date=None,
        )
    market_dates = sorted(
        row[0]
        for row in (
            await db.execute(
                select(StockKline.trade_date)
                .where(
                    StockKline.trade_date
                    >= minimum_prediction_date - timedelta(days=3),
                    StockKline.trade_date <= latest_kline_date,
                )
                .distinct()
                .order_by(StockKline.trade_date)
            )
        ).all()
        if row[0]
        and row[0].weekday() < 5
        and not is_official_closed_day(row[0])
    )

    clock_valid: list[tuple[PromotionPredictionRecord, dict, date]] = []
    for record, factors in eligible:
        first_future_index = bisect_right(market_dates, record.prediction_trade_date)
        horizon = max(int(record.horizon_days or 1), 1)
        expected_index = first_future_index + horizon - 1
        expected = (
            market_dates[expected_index] if expected_index < len(market_dates) else None
        )
        if expected is None:
            diagnostics["excluded_incomplete_horizon"] += 1
            continue
        if record.outcome_trade_date is not None and record.outcome_trade_date != expected:
            diagnostics["repaired_outcome_clock"] += 1
        clock_valid.append((record, factors, expected))

    if not clock_valid:
        return DatasetBundle(
            rows=[],
            diagnostics={**diagnostics, "eligible_records": 0},
            data_version="promotion_labels_no_complete_horizon",
            start_date=None,
            end_date=None,
        )

    maximum_outcome_date = max(expected for _record, _factors, expected in clock_valid)
    market_date_set = set(market_dates)
    pool_events = {
        (str(code or "").strip(), trade_day)
        for code, trade_day in (
            await db.execute(
                select(LimitUpPool.code, LimitUpPool.trade_date).where(
                    LimitUpPool.trade_date >= minimum_prediction_date,
                    LimitUpPool.trade_date <= maximum_outcome_date,
                    LimitUpPool.quarantined.is_(False),
                )
            )
        ).all()
        if trade_day
        and trade_day.weekday() < 5
        and not is_official_closed_day(trade_day)
        and trade_day in market_date_set
    }

    labelled: list[tuple[PromotionPredictionRecord, dict, date, int]] = []
    for record, factors, expected in clock_valid:
        code = str(record.code or "").strip()
        outcome_limit_up = (code, expected) in pool_events
        prediction_day_limit_up = (
            code,
            record.prediction_trade_date,
        ) in pool_events
        label = promotion_event_label(
            target_board=target_board,
            outcome_limit_up=outcome_limit_up,
            prediction_day_limit_up=prediction_day_limit_up,
        )
        legacy_label = (
            1
            if record.outcome_status == "success"
            else 0
            if record.outcome_status == "failed"
            else None
        )
        if legacy_label is not None and legacy_label != label:
            diagnostics["outcome_label_disagreement"] += 1
        labelled.append((record, factors, expected, label))

    # One stock/date/target observation contributes at most once. Prefer the
    # latest same-context snapshot, without allowing a later calendar cutoff.
    latest_by_key: dict[
        tuple[str, date, int], tuple[PromotionPredictionRecord, dict, date, int]
    ] = {}
    for record, factors, expected, label in labelled:
        key = (
            str(record.code or "").strip(),
            record.prediction_trade_date,
            int(record.target_board),
        )
        previous = latest_by_key.get(key)
        if previous is None or (_recorded_at(record, factors), int(record.id or 0)) > (
            _recorded_at(previous[0], previous[1]),
            int(previous[0].id or 0),
        ):
            latest_by_key[key] = (record, factors, expected, label)
    diagnostics["deduplicated_records"] = len(labelled) - len(latest_by_key)

    selected = sorted(
        latest_by_key.values(),
        key=lambda item: (
            item[0].prediction_trade_date,
            str(item[0].code),
            int(item[0].id or 0),
        ),
    )
    selected_dates = sorted({item[0].prediction_trade_date for item in selected})
    regime_snapshots_by_date: dict[date, list[MarketRegimeSnapshot]] = {}
    if selected_dates:
        regime_snapshots = list(
            (
                await db.scalars(
                    select(MarketRegimeSnapshot)
                    .where(
                        MarketRegimeSnapshot.trade_date.in_(selected_dates),
                        MarketRegimeSnapshot.snapshot_context == "postmarket",
                    )
                    .order_by(
                        MarketRegimeSnapshot.trade_date,
                        MarketRegimeSnapshot.created_at,
                        MarketRegimeSnapshot.id,
                    )
                )
            ).all()
        )
        for regime_snapshot in regime_snapshots:
            regime_snapshots_by_date.setdefault(
                regime_snapshot.trade_date, []
            ).append(regime_snapshot)

    def regime_for(record: PromotionPredictionRecord, factors: dict) -> str:
        explicit = str(factors.get("market_regime") or "").strip()
        if explicit in REGIME_LABELS:
            return explicit
        cutoff = _recorded_at(record, factors)
        eligible_snapshots = [
            snapshot
            for snapshot in regime_snapshots_by_date.get(
                record.prediction_trade_date, []
            )
            if snapshot.created_at <= cutoff and snapshot.as_of_at <= cutoff
        ]
        if not eligible_snapshots:
            return "unknown"
        value = eligible_snapshots[-1].primary_regime
        return value if value in REGIME_LABELS else "unknown"

    rows = [
        FeatureRow(
            code=str(record.code or "").strip(),
            trade_date=record.prediction_trade_date.isoformat(),
            target_board=int(record.target_board),
            label=label,
            baseline_probability=min(
                max(
                    _float(
                        record.calibrated_probability,
                        _float(record.predicted_probability, 0.0),
                    ),
                    1e-6,
                ),
                1.0 - 1e-6,
            ),
            candidate_route=str(record.candidate_route or ""),
            values=extract_point_in_time_features(factors),
            market_regime=regime_for(record, factors),
        )
        for record, factors, _expected, label in selected
    ]
    diagnostics.update(
        {
            "eligible_records": len(rows),
            "positive_count": sum(row.label for row in rows),
            "negative_count": sum(1 - row.label for row in rows),
            "trade_day_count": len({row.trade_date for row in rows}),
            "regime_sample_counts": {
                regime: sum(row.market_regime == regime for row in rows)
                for regime in sorted({row.market_regime for row in rows})
            },
            "regime_coverage": (
                sum(row.market_regime != "unknown" for row in rows) / len(rows)
                if rows
                else 0.0
            ),
            "snapshot_context": snapshot_context,
            "label_source": "limit_up_pool_on_stock_kline_session_clock",
            "label_version": PROMOTION_LABEL_VERSION,
        }
    )
    return DatasetBundle(
        rows=rows,
        diagnostics=diagnostics,
        data_version=_data_version(selected),
        start_date=(date.fromisoformat(rows[0].trade_date) if rows else None),
        end_date=(date.fromisoformat(rows[-1].trade_date) if rows else None),
    )
