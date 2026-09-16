"""Point-in-time, explainable A-share market-regime classification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any

import numpy as np
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.regime import MarketRegimeSnapshot
from app.models.sector import SectorLifecycle, SectorRotation
from app.models.stock import LimitUpPool, MarketSentiment, SectorPersistence, StockKline


REGIME_VERSION = "ashare_regime_rules_v1"
REGIME_LABELS = (
    "risk_off",
    "recovery",
    "sector_rotation",
    "sector_maintrend",
    "individual_maintrend",
    "high_board_speculation",
    "broad_trend",
    "balanced",
)
REGIME_NAMES_ZH = {
    "risk_off": "退潮/风险规避",
    "recovery": "冰点修复",
    "sector_rotation": "板块轮动",
    "sector_maintrend": "板块主升",
    "individual_maintrend": "个股独立主升",
    "high_board_speculation": "高标投机",
    "broad_trend": "普涨趋势",
    "balanced": "均衡震荡",
}


@dataclass(frozen=True, slots=True)
class RegimeInputs:
    advance_ratio: float = 0.5
    previous_advance_ratio: float = 0.5
    median_return: float = 0.0
    return_dispersion: float = 0.0
    limit_up_count: int = 0
    previous_limit_up_count: int = 0
    limit_down_count: int = 0
    broken_limit_count: int = 0
    seal_rate: float = 0.0
    board_height: int = 0
    first_board_count: int = 0
    consecutive_board_count: int = 0
    main_net_inflow: float = 0.0
    active_sector_count: int = 0
    strong_sector_count: int = 0
    mainline_sector_count: int = 0
    persistent_sector_count: int = 0
    rotation_signal_count: int = 0
    top_sector_limit_share: float = 0.0
    top_sector_strength: float = 0.0
    universe_count: int = 0
    sentiment_cycle: str = ""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if np.isfinite(parsed) else default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return float(min(max(value, low), high))


def _positive(value: float, scale: float) -> float:
    return _clip(value / scale, 0.0, 1.0) if scale > 0 else 0.0


def classify_market_regime(inputs: RegimeInputs) -> dict:
    """Score styles without future data; all evidence is available at the close."""

    breadth = _clip(inputs.advance_ratio, 0.0, 1.0)
    previous_breadth = _clip(inputs.previous_advance_ratio, 0.0, 1.0)
    breadth_delta = breadth - previous_breadth
    limit_delta = inputs.limit_up_count - inputs.previous_limit_up_count
    seal_ratio = _clip(inputs.seal_rate / 100.0, 0.0, 1.0)
    consecutive_share = (
        inputs.consecutive_board_count / inputs.limit_up_count
        if inputs.limit_up_count > 0
        else 0.0
    )
    first_board_share = (
        inputs.first_board_count / inputs.limit_up_count
        if inputs.limit_up_count > 0
        else 0.0
    )
    concentration = _clip(inputs.top_sector_limit_share, 0.0, 1.0)
    cycle = (inputs.sentiment_cycle or "").lower()

    scores = {
        "risk_off": _clip(
            (0.45 - breadth) * 125
            + _positive(inputs.limit_down_count, 20) * 24
            + _positive(inputs.broken_limit_count, 35) * 16
            + (1.0 - seal_ratio) * 15
            + _positive(-inputs.main_net_inflow, 80) * 12
            + (15 if cycle in {"freezing", "declining", "divergence"} else 0)
        ),
        "recovery": _clip(
            _positive(breadth_delta, 0.22) * 28
            + _positive(limit_delta, 25) * 24
            + _positive(breadth - 0.42, 0.25) * 14
            + seal_ratio * 12
            + _positive(inputs.main_net_inflow, 80) * 10
            + (20 if cycle == "recovery" else 0)
        ),
        "sector_rotation": _clip(
            _positive(inputs.active_sector_count, 12) * 18
            + _positive(inputs.strong_sector_count, 8) * 20
            + _positive(inputs.rotation_signal_count, 8) * 20
            + _positive(inputs.persistent_sector_count, 6) * 12
            + (1.0 - concentration) * 12
            + _positive(breadth - 0.42, 0.25) * 10
            - _positive(inputs.mainline_sector_count, 3) * 8
        ),
        "sector_maintrend": _clip(
            concentration * 28
            + _positive(inputs.mainline_sector_count, 3) * 24
            + _positive(inputs.persistent_sector_count, 5) * 16
            + _positive(inputs.top_sector_strength, 80) * 14
            + _positive(inputs.board_height - 1, 5) * 10
            + seal_ratio * 8
        ),
        "individual_maintrend": _clip(
            _positive(inputs.board_height - 1, 6) * 30
            + (1.0 - concentration) * 24
            + _positive(4 - inputs.strong_sector_count, 4) * 14
            + _positive(inputs.limit_up_count, 40) * 8
            + seal_ratio * 10
            + _positive(breadth - 0.35, 0.3) * 8
        ),
        "high_board_speculation": _clip(
            _positive(inputs.board_height - 2, 6) * 32
            + _positive(consecutive_share, 0.35) * 22
            + _positive(0.48 - breadth, 0.3) * 14
            + _positive(0.55 - first_board_share, 0.4) * 12
            + _positive(inputs.broken_limit_count, 25) * 10
            + (10 if cycle in {"climax", "divergence"} else 0)
        ),
        "broad_trend": _clip(
            _positive(breadth - 0.5, 0.28) * 30
            + _positive(inputs.median_return, 2.0) * 20
            + _positive(inputs.limit_up_count, 80) * 16
            + _positive(inputs.main_net_inflow, 100) * 14
            + seal_ratio * 10
            + (1.0 - _positive(inputs.return_dispersion, 4.0)) * 10
        ),
        "balanced": _clip(
            48
            - abs(breadth - 0.5) * 55
            - _positive(abs(inputs.median_return), 2.0) * 12
            - _positive(inputs.board_height - 3, 6) * 8
        ),
    }
    ranking = sorted(scores.items(), key=lambda item: (item[1], item[0]), reverse=True)
    primary, primary_score = ranking[0]
    secondary, secondary_score = ranking[1]
    margin = primary_score - secondary_score
    confidence = _clip(0.42 + margin / 85.0, 0.0, 0.95)

    evidence = [
        f"上涨占比 {breadth:.1%}，较前一交易日 {breadth_delta:+.1%}",
        f"涨停 {inputs.limit_up_count} / 跌停 {inputs.limit_down_count} / 炸板 {inputs.broken_limit_count}",
        f"最高 {inputs.board_height} 板，连板占涨停 {consecutive_share:.1%}",
        f"强势板块 {inputs.strong_sector_count}，主线板块 {inputs.mainline_sector_count}，头部涨停集中度 {concentration:.1%}",
    ]
    return {
        "primary_regime": primary,
        "primary_regime_name": REGIME_NAMES_ZH[primary],
        "secondary_regime": secondary,
        "secondary_regime_name": REGIME_NAMES_ZH[secondary],
        "confidence": confidence,
        "scores": {key: round(value, 4) for key, value in ranking},
        "evidence": evidence,
    }


def classify_historical_market_context(context: dict[str, float]) -> str:
    """Reduced classifier for reconstructed historical K-line panels."""

    result = classify_market_regime(
        RegimeInputs(
            advance_ratio=_number(context.get("advance_ratio"), 0.5),
            previous_advance_ratio=_number(
                context.get("previous_advance_ratio"),
                _number(context.get("advance_ratio"), 0.5),
            ),
            median_return=_number(context.get("median_return")),
            return_dispersion=_number(context.get("return_dispersion")),
            limit_up_count=int(_number(context.get("limit_up_count"))),
            previous_limit_up_count=int(
                _number(context.get("previous_limit_up_count"))
            ),
            universe_count=int(_number(context.get("universe_count"))),
        )
    )
    return str(result["primary_regime"])


def _transition(previous: str | None, current: str) -> str:
    if not previous:
        return "initial"
    if previous == current:
        return "unchanged"
    if current == "risk_off":
        return "risk_off"
    if previous == "risk_off" and current in {"recovery", "broad_trend"}:
        return "risk_on"
    return "style_shift"


async def _resolve_trade_date(db: AsyncSession, requested: date | None) -> date:
    statement = select(StockKline.trade_date)
    if requested is not None:
        statement = statement.where(StockKline.trade_date <= requested)
    resolved = await db.scalar(statement.order_by(desc(StockKline.trade_date)).limit(1))
    if resolved is None:
        raise ValueError("no StockKline trade date is available for regime classification")
    return resolved


async def build_market_regime_snapshot(
    db: AsyncSession,
    *,
    trade_date: date | None = None,
    snapshot_context: str = "postmarket",
    as_of_at: datetime | None = None,
    persist: bool = True,
    minimum_universe_count: int = 500,
) -> dict:
    """Collect one close-time feature set, classify it, and append idempotently."""

    resolved_date = await _resolve_trade_date(db, trade_date)
    snapshot_context = (snapshot_context or "postmarket").strip().lower()
    if snapshot_context not in {"postmarket", "premarket"}:
        raise ValueError("regime snapshot_context must be postmarket or premarket")
    as_of_at = as_of_at or datetime.combine(resolved_date, time(hour=20))

    previous_date = await db.scalar(
        select(StockKline.trade_date)
        .where(StockKline.trade_date < resolved_date)
        .order_by(desc(StockKline.trade_date))
        .limit(1)
    )
    current_changes = [
        _number(value)
        for value in (
            await db.scalars(
                select(StockKline.change_pct).where(
                    StockKline.trade_date == resolved_date
                )
            )
        ).all()
        if value is not None
    ]
    previous_changes = (
        [
            _number(value)
            for value in (
                await db.scalars(
                    select(StockKline.change_pct).where(
                        StockKline.trade_date == previous_date
                    )
                )
            ).all()
            if value is not None
        ]
        if previous_date
        else []
    )
    universe_count = len(current_changes)
    advance_ratio = (
        float(np.mean(np.asarray(current_changes) > 0)) if current_changes else 0.5
    )
    previous_advance_ratio = (
        float(np.mean(np.asarray(previous_changes) > 0))
        if previous_changes
        else advance_ratio
    )
    median_return = float(np.median(current_changes)) if current_changes else 0.0
    dispersion = float(np.std(current_changes)) if current_changes else 0.0

    sentiment = await db.scalar(
        select(MarketSentiment).where(MarketSentiment.trade_date == resolved_date)
    )
    previous_sentiment = (
        await db.scalar(
            select(MarketSentiment).where(MarketSentiment.trade_date == previous_date)
        )
        if previous_date
        else None
    )
    limit_rows = list(
        (
            await db.scalars(
                select(LimitUpPool).where(LimitUpPool.trade_date == resolved_date)
            )
        ).all()
    )
    sector_rows = list(
        (
            await db.scalars(
                select(SectorPersistence).where(
                    SectorPersistence.trade_date == resolved_date
                )
            )
        ).all()
    )
    lifecycle_rows = list(
        (
            await db.scalars(
                select(SectorLifecycle).where(
                    SectorLifecycle.trade_date == resolved_date
                )
            )
        ).all()
    )
    rotation_rows = list(
        (
            await db.scalars(
                select(SectorRotation).where(SectorRotation.trade_date == resolved_date)
            )
        ).all()
    )

    limit_up_count = len(limit_rows) or int(_number(getattr(sentiment, "limit_up_count", 0)))
    first_board_count = sum(int(row.consecutive_days or 1) <= 1 for row in limit_rows)
    consecutive_board_count = sum(int(row.consecutive_days or 1) >= 2 for row in limit_rows)
    board_height = max(
        [int(row.consecutive_days or 1) for row in limit_rows]
        + [int(_number(getattr(sentiment, "board_height", 0)))]
    )
    sector_limit_counts = [max(int(row.limit_up_count or 0), 0) for row in sector_rows]
    sector_limit_total = sum(sector_limit_counts)
    top_sector_limit_share = (
        max(sector_limit_counts) / sector_limit_total if sector_limit_total else 0.0
    )
    strong_sector_count = sum(
        _number(row.strength_score) >= 60 or _number(row.change_pct) >= 1.0
        for row in sector_rows
    )
    active_sector_count = sum(
        int(row.limit_up_count or 0) > 0
        or _number(row.strength_score) >= 45
        or _number(row.change_pct) >= 0.5
        for row in sector_rows
    )
    persistent_sector_count = sum(int(row.consecutive_days or 0) >= 2 for row in sector_rows)
    mainline_sector_count = sum(bool(row.is_main_line) for row in lifecycle_rows)
    top_sector_strength = max(
        [_number(row.strength_score) for row in sector_rows]
        + [_number(row.state_score) for row in lifecycle_rows]
        + [0.0]
    )

    inputs = RegimeInputs(
        advance_ratio=advance_ratio,
        previous_advance_ratio=previous_advance_ratio,
        median_return=median_return,
        return_dispersion=dispersion,
        limit_up_count=limit_up_count,
        previous_limit_up_count=int(
            _number(getattr(previous_sentiment, "limit_up_count", 0))
        ),
        limit_down_count=int(_number(getattr(sentiment, "limit_down_count", 0))),
        broken_limit_count=int(_number(getattr(sentiment, "broken_limit_count", 0))),
        seal_rate=_number(getattr(sentiment, "seal_rate", 0)),
        board_height=board_height,
        first_board_count=first_board_count,
        consecutive_board_count=consecutive_board_count,
        main_net_inflow=_number(getattr(sentiment, "main_net_inflow", 0)),
        active_sector_count=active_sector_count,
        strong_sector_count=strong_sector_count,
        mainline_sector_count=mainline_sector_count,
        persistent_sector_count=persistent_sector_count,
        rotation_signal_count=len(rotation_rows),
        top_sector_limit_share=top_sector_limit_share,
        top_sector_strength=top_sector_strength,
        universe_count=universe_count,
        sentiment_cycle=str(getattr(sentiment, "sentiment_cycle", "") or ""),
    )
    result = classify_market_regime(inputs)

    source_presence = {
        "stock_kline": bool(current_changes),
        "market_sentiment": sentiment is not None,
        "limit_up_pool": bool(limit_rows) or limit_up_count == 0,
        "sector_persistence": bool(sector_rows),
        "sector_lifecycle": bool(lifecycle_rows),
    }
    input_coverage = sum(source_presence.values()) / len(source_presence)
    quality_status = (
        "good"
        if universe_count >= max(int(minimum_universe_count), 1)
        and sentiment is not None
        and (sector_rows or lifecycle_rows)
        else "partial"
    )
    confidence = float(result["confidence"])
    if quality_status != "good":
        confidence *= max(input_coverage, 0.35)
    result["confidence"] = round(confidence, 6)

    features = asdict(inputs)
    data_payload = {
        "trade_date": resolved_date.isoformat(),
        "previous_date": previous_date.isoformat() if previous_date else None,
        "features": features,
        "source_presence": source_presence,
    }
    data_hash = hashlib.sha256(
        json.dumps(data_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    data_version = f"regime_data_{data_hash[:16]}"
    snapshot_key = hashlib.sha256(
        f"{resolved_date}|{snapshot_context}|{REGIME_VERSION}|{data_version}".encode(
            "utf-8"
        )
    ).hexdigest()

    previous_snapshot = await db.scalar(
        select(MarketRegimeSnapshot)
        .where(
            MarketRegimeSnapshot.trade_date < resolved_date,
            MarketRegimeSnapshot.snapshot_context == snapshot_context,
            MarketRegimeSnapshot.regime_version == REGIME_VERSION,
        )
        .order_by(desc(MarketRegimeSnapshot.trade_date), desc(MarketRegimeSnapshot.created_at))
        .limit(1)
    )
    previous_regime = previous_snapshot.primary_regime if previous_snapshot else None
    transition_type = _transition(previous_regime, str(result["primary_regime"]))
    result.update(
        {
            "snapshot_key": snapshot_key,
            "trade_date": resolved_date.isoformat(),
            "as_of_at": as_of_at.isoformat(timespec="seconds"),
            "snapshot_context": snapshot_context,
            "regime_version": REGIME_VERSION,
            "data_version": data_version,
            "previous_regime": previous_regime,
            "transition_type": transition_type,
            "quality_status": quality_status,
            "input_coverage": round(input_coverage, 4),
            "universe_count": universe_count,
            "features": features,
            "source_presence": source_presence,
            "persisted": False,
        }
    )
    if not persist:
        return result

    existing = await db.scalar(
        select(MarketRegimeSnapshot).where(
            MarketRegimeSnapshot.snapshot_key == snapshot_key
        )
    )
    if existing is None:
        existing = MarketRegimeSnapshot(
            snapshot_key=snapshot_key,
            trade_date=resolved_date,
            as_of_at=as_of_at,
            snapshot_context=snapshot_context,
            regime_version=REGIME_VERSION,
            data_version=data_version,
            primary_regime=str(result["primary_regime"]),
            secondary_regime=str(result["secondary_regime"]),
            previous_regime=previous_regime,
            transition_type=transition_type,
            confidence=confidence,
            quality_status=quality_status,
            input_coverage=input_coverage,
            universe_count=universe_count,
            scores_json=json.dumps(result["scores"], ensure_ascii=False, sort_keys=True),
            features_json=json.dumps(features, ensure_ascii=False, sort_keys=True),
            evidence_json=json.dumps(
                {"evidence": result["evidence"], "source_presence": source_presence},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        db.add(existing)
        await db.flush()
    result["id"] = existing.id
    result["persisted"] = True
    return result
