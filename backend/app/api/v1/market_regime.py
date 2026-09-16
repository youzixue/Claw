"""Versioned market-regime classification and history API."""

from __future__ import annotations

import json
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.regime import MarketRegimeSnapshot
from app.promotion.regime import (
    REGIME_NAMES_ZH,
    REGIME_VERSION,
    build_market_regime_snapshot,
)


router = APIRouter()


class MarketRegimeBuildRequest(BaseModel):
    trade_date: date | None = None
    snapshot_context: str = "postmarket"
    as_of_at: datetime | None = None
    persist: bool = True
    minimum_universe_count: int = Field(default=500, ge=1, le=10000)


def _loads(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def regime_payload(row: MarketRegimeSnapshot, *, include_features: bool = True) -> dict:
    evidence_payload = _loads(row.evidence_json)
    payload = {
        "id": row.id,
        "snapshot_key": row.snapshot_key,
        "trade_date": row.trade_date.isoformat(),
        "as_of_at": row.as_of_at.isoformat(timespec="seconds"),
        "snapshot_context": row.snapshot_context,
        "regime_version": row.regime_version,
        "data_version": row.data_version,
        "primary_regime": row.primary_regime,
        "primary_regime_name": REGIME_NAMES_ZH.get(row.primary_regime, row.primary_regime),
        "secondary_regime": row.secondary_regime,
        "secondary_regime_name": REGIME_NAMES_ZH.get(
            row.secondary_regime or "", row.secondary_regime
        ),
        "previous_regime": row.previous_regime,
        "transition_type": row.transition_type,
        "confidence": row.confidence,
        "quality_status": row.quality_status,
        "input_coverage": row.input_coverage,
        "universe_count": row.universe_count,
        "scores": _loads(row.scores_json),
        "evidence": evidence_payload.get("evidence", []),
        "source_presence": evidence_payload.get("source_presence", {}),
        "created_at": row.created_at.isoformat(timespec="seconds"),
    }
    if include_features:
        payload["features"] = _loads(row.features_json)
    return payload


@router.get("/identity")
async def regime_identity():
    return {
        "regime_version": REGIME_VERSION,
        "labels": [
            {"code": code, "name": name} for code, name in REGIME_NAMES_ZH.items()
        ],
        "contract": "close-time point-in-time classification; no automatic model parameter mutation",
    }


@router.post("/classify")
async def classify_regime(
    request: MarketRegimeBuildRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await build_market_regime_snapshot(
            db,
            trade_date=request.trade_date,
            snapshot_context=request.snapshot_context,
            as_of_at=request.as_of_at,
            persist=request.persist,
            minimum_universe_count=request.minimum_universe_count,
        )
        if request.persist:
            await db.commit()
        return result
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/current")
async def current_regime(
    snapshot_context: str = "postmarket",
    include_features: bool = True,
    db: AsyncSession = Depends(get_db),
):
    row = await db.scalar(
        select(MarketRegimeSnapshot)
        .where(MarketRegimeSnapshot.snapshot_context == snapshot_context)
        .order_by(desc(MarketRegimeSnapshot.trade_date), desc(MarketRegimeSnapshot.created_at))
        .limit(1)
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="no persisted regime snapshot; run POST /classify after close",
        )
    return regime_payload(row, include_features=include_features)


@router.get("/history")
async def regime_history(
    start_date: date | None = None,
    end_date: date | None = None,
    primary_regime: str = "",
    snapshot_context: str = "postmarket",
    include_features: bool = False,
    limit: int = 120,
    db: AsyncSession = Depends(get_db),
):
    statement = select(MarketRegimeSnapshot).where(
        MarketRegimeSnapshot.snapshot_context == snapshot_context
    )
    if start_date is not None:
        statement = statement.where(MarketRegimeSnapshot.trade_date >= start_date)
    if end_date is not None:
        statement = statement.where(MarketRegimeSnapshot.trade_date <= end_date)
    if primary_regime.strip():
        statement = statement.where(
            MarketRegimeSnapshot.primary_regime == primary_regime.strip()
        )
    rows = list(
        (
            await db.scalars(
                statement.order_by(
                    desc(MarketRegimeSnapshot.trade_date),
                    desc(MarketRegimeSnapshot.created_at),
                ).limit(max(1, min(int(limit), 500)))
            )
        ).all()
    )
    return {
        "count": len(rows),
        "regime_version": REGIME_VERSION,
        "snapshots": [
            regime_payload(row, include_features=include_features) for row in rows
        ],
    }
