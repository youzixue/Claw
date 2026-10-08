"""Point-in-time premarket, intraday, and postmarket review snapshots."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.stock_tagger import stock_tagger
from app.core.trade_calendar import is_official_closed_day, trade_calendar
from app.models.governance import TradeCalendarModel
from app.models.news import FinanceNews
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.regime import MarketRegimeSnapshot
from app.models.review import DailyReviewSnapshot, PromotionReviewAttribution
from app.models.sector import SectorLifecycle
from app.models.stock import (
    FundFlow,
    LimitUpPool,
    MarketSentiment,
    SectorPersistence,
    StockFundamentalDaily,
    StockKline,
    StockSpot,
)
from app.promotion.regime import REGIME_NAMES_ZH, build_market_regime_snapshot
from app.review.overnight_evidence import _read_news_window


REVIEW_SCHEMA_VERSION = "daily_review_workbench_v5"
REVIEW_VIEW_SCHEMA_VERSION = "daily_review_decision_view_v3"
REVIEW_PHASES = ("premarket", "intraday", "postmarket")
_MIN_CANDIDATE_COUNT = {1: 10, 2: 3}
_NEWS_SELECTION_LIMIT = 100

_REGIME_STANCE = {
    "risk_off": ("defensive", "防守等待"),
    "recovery": ("balanced", "修复确认"),
    "sector_rotation": ("selective", "轮动择强"),
    "sector_maintrend": ("selective", "主线跟踪"),
    "individual_maintrend": ("selective", "个股择强"),
    "high_board_speculation": ("defensive", "高标控险"),
    "broad_trend": ("offensive", "顺势观察"),
    "balanced": ("balanced", "均衡等待"),
    "unknown": ("defensive", "数据不足"),
}


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads(value: str | None, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if np.isfinite(parsed) else default


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / float(denominator), 6) if denominator else 0.0


def _price_return(value: Any, prev_close: Any) -> float | None:
    price = _optional_number(value)
    anchor = _optional_number(prev_close)
    if price is None or anchor is None or anchor <= 0:
        return None
    return round((price / anchor - 1.0) * 100.0, 4)


def _opening_bucket(open_pct: float | None) -> str:
    if open_pct is None:
        return "unknown"
    if open_pct <= -7.0:
        return "deep_negative_le_-7"
    if open_pct < -1.0:
        return "negative_-7_to_-1"
    if open_pct <= 1.0:
        return "zero_axis_-1_to_1"
    if open_pct < 5.0:
        return "moderate_high_1_to_5"
    return "high_ge_5"


def _seal_time_bucket(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    normalized = raw.replace(":", "")
    if len(normalized) < 4 or not normalized[:4].isdigit():
        return "unknown"
    hhmm = int(normalized[:4])
    if hhmm <= 935:
        return "open_to_0935"
    if hhmm <= 1030:
        return "0935_to_1030"
    if hhmm <= 1130:
        return "1030_to_1130"
    if hhmm <= 1400:
        return "afternoon_to_1400"
    return "after_1400"


def _news_polarity(bull_bear: Any, sentiment: Any) -> str:
    """Normalize the two historical news sentiment vocabularies."""

    values = {
        str(bull_bear or "").strip().lower(),
        str(sentiment or "").strip().lower(),
    }
    if values & {"bull", "bullish", "positive"}:
        return "bull"
    if values & {"bear", "bearish", "negative"}:
        return "bear"
    return "neutral"


def _news_snapshot_item(row: FinanceNews) -> dict[str, Any]:
    """Keep only owned, filterable news leaves inside the immutable snapshot."""

    related_codes = _loads(row.related_codes, [])
    related_sectors = _loads(row.related_sectors, [])
    related_codes = related_codes if isinstance(related_codes, list) else []
    related_sectors = related_sectors if isinstance(related_sectors, list) else []
    has_codes = bool(related_codes)
    has_sectors = bool(related_sectors)
    impact_scope = (
        "stock_and_sector"
        if has_codes and has_sectors
        else "stock"
        if has_codes
        else "sector"
        if has_sectors
        else "market_or_unmapped"
    )
    return {
        "id": row.id,
        "publish_time": row.publish_time.isoformat(timespec="seconds"),
        "source": row.source,
        "title": row.title,
        "summary": row.summary,
        "importance": row.importance,
        "category": row.category,
        "nlp_status": row.nlp_status,
        "bull_bear": _news_polarity(row.bull_bear, row.sentiment),
        "confidence": row.bull_bear_confidence,
        "related_codes": related_codes,
        "related_sectors": related_sectors,
        "impact_reason": row.impact_reason,
        "impact_scope": impact_scope,
        "has_specific_target": has_codes or has_sectors,
    }


def _intraday_recovery_shape(
    *,
    open_pct: float | None,
    low_pct: float | None,
) -> str:
    if open_pct is None:
        return "unknown"
    if open_pct <= -7.0:
        return "deep_negative_open_recovery"
    if open_pct < -1.0:
        return "negative_open_recovery"
    if open_pct <= 1.0:
        if low_pct is not None and low_pct < -1.0:
            return "zero_axis_then_underwater_recovery"
        return "zero_axis_recovery"
    if low_pct is not None and low_pct < 0:
        return "positive_open_then_underwater_recovery"
    return "positive_open_recovery"


def _build_strategy_iteration_sample(
    *,
    analysis_trade_date: date,
    bar_rows: list[Any],
    limit_rows: list[LimitUpPool],
    limit_pool_known: bool,
) -> dict[str, Any]:
    """Freeze post-event morphology without turning one session into a trading rule."""

    bars_by_code = {
        str(row.code or "").strip(): row
        for row in bar_rows
        if str(row.code or "").strip()
    }
    outcomes: list[dict[str, Any]] = []
    opening_counts: Counter[str] = Counter()
    first_board_opening_counts: Counter[str] = Counter()
    shape_counts: Counter[str] = Counter()
    seal_time_counts: Counter[str] = Counter()
    complete_kline_count = 0

    for limit_row in sorted(
        limit_rows,
        key=lambda row: (
            -int(row.consecutive_days or 1),
            str(row.limit_up_time or "99:99:99"),
            str(row.code or ""),
        ),
    ):
        code = str(limit_row.code or "").strip()
        bar = bars_by_code.get(code)
        prev_close = getattr(bar, "prev_close", None) if bar is not None else None
        open_pct = _price_return(getattr(bar, "open", None), prev_close)
        low_pct = _price_return(getattr(bar, "low", None), prev_close)
        high_pct = _price_return(getattr(bar, "high", None), prev_close)
        close_pct = (
            _optional_number(getattr(bar, "change_pct", None))
            if bar is not None
            else None
        )
        if open_pct is not None and low_pct is not None and close_pct is not None:
            complete_kline_count += 1
        opening_bucket = _opening_bucket(open_pct)
        recovery_shape = _intraday_recovery_shape(
            open_pct=open_pct,
            low_pct=low_pct,
        )
        seal_bucket = _seal_time_bucket(limit_row.limit_up_time)
        consecutive_days = int(limit_row.consecutive_days or 1)
        opening_counts[opening_bucket] += 1
        if consecutive_days <= 1:
            first_board_opening_counts[opening_bucket] += 1
        shape_counts[recovery_shape] += 1
        seal_time_counts[seal_bucket] += 1
        outcomes.append(
            {
                "code": code,
                "name": limit_row.name,
                "consecutive_days": consecutive_days,
                "is_first_board": consecutive_days <= 1,
                "open_pct": open_pct,
                "low_pct": low_pct,
                "high_pct": high_pct,
                "close_pct": close_pct,
                "opening_bucket": opening_bucket,
                "recovery_shape": recovery_shape,
                "limit_up_time": limit_row.limit_up_time,
                "seal_time_bucket": seal_bucket,
                "break_count": int(limit_row.break_count or 0),
                "turnover": _optional_number(limit_row.turnover),
                "reason": limit_row.limit_up_reason,
            }
        )

    return {
        "sample_version": "abcdef_daily_outcome_v1",
        "trade_date": analysis_trade_date.isoformat(),
        "sample_type": "post_event_outcome_morphology",
        "causal_status": "descriptive_not_causal",
        "eligible_for_threshold_tuning": False,
        "promotion_gate": "仅作为每日前向样本；至少积累20个独立交易日并用候选集分母评估后，才可提议修改生产阈值。",
        "coverage": {
            "limit_pool_known": bool(limit_pool_known),
            "limit_up_count": len(limit_rows) if limit_pool_known else None,
            "kline_complete_count": complete_kline_count,
            "kline_complete_ratio": _ratio(complete_kline_count, len(limit_rows)),
            "indicative_auction_path_available": False,
            "indicative_auction_note": "本样本只有正式开盘价；不得据此声称已验证09:15-09:25竞价抢筹路径。",
        },
        "opening_bucket_counts": dict(sorted(opening_counts.items())),
        "first_board_opening_bucket_counts": dict(
            sorted(first_board_opening_counts.items())
        ),
        "recovery_shape_counts": dict(sorted(shape_counts.items())),
        "seal_time_bucket_counts": dict(sorted(seal_time_counts.items())),
        "outcomes": outcomes,
    }


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _default_as_of(review_date: date, phase: str) -> datetime:
    """Return a truthful stage cutoff instead of labelling a manual snapshot with future time."""

    stage_cutoff = {
        "premarket": datetime.combine(review_date, time(hour=8, minute=45)),
        "intraday": datetime.combine(review_date, time(hour=11, minute=30)),
        "postmarket": datetime.combine(review_date, time(hour=20, minute=30)),
    }[phase]
    now = datetime.now()
    if now.date() != review_date:
        return stage_cutoff
    if phase == "intraday":
        return now
    return min(now, stage_cutoff)


def _validate_phase_as_of(
    *,
    review_date: date,
    phase: str,
    as_of_at: datetime,
) -> None:
    if as_of_at.date() != review_date:
        raise ValueError("as_of_at date must match review_date")
    current = datetime.now()
    if review_date == current.date() and as_of_at > current:
        raise ValueError("as_of_at 不能晚于当前时间")
    cutoff = as_of_at.time()
    if phase == "premarket" and cutoff >= time(hour=9, minute=30):
        raise ValueError("盘前快照 as_of_at 必须早于 09:30，禁止纳入开盘后信息")
    if phase == "intraday" and not (
        time(hour=9, minute=30) <= cutoff <= time(hour=15)
    ):
        raise ValueError("盘中快照 as_of_at 必须位于 09:30-15:00")
    if phase == "postmarket" and cutoff < time(hour=15):
        raise ValueError("盘后快照 as_of_at 必须不早于 15:00")


async def _resolve_analysis_date(
    db: AsyncSession,
    review_date: date,
    phase: str,
) -> date:
    statement = select(StockKline.trade_date)
    if phase in {"premarket", "intraday"}:
        statement = statement.where(StockKline.trade_date < review_date)
    else:
        statement = statement.where(StockKline.trade_date <= review_date)
    result = await db.scalar(statement.order_by(desc(StockKline.trade_date)).limit(1))
    if result is None:
        raise ValueError("no leakage-safe StockKline date is available for this review phase")
    return result


async def _previous_market_date(db: AsyncSession, trade_date: date) -> date | None:
    return await db.scalar(
        select(StockKline.trade_date)
        .where(StockKline.trade_date < trade_date)
        .order_by(desc(StockKline.trade_date))
        .limit(1)
    )


async def _is_review_trade_day(db: AsyncSession, target_date: date) -> bool:
    calendar_row = await db.get(TradeCalendarModel, target_date)
    if calendar_row is not None:
        return bool(calendar_row.is_trade_day)
    return target_date.weekday() < 5 and not is_official_closed_day(target_date)


async def _next_guidance_date(db: AsyncSession, trade_date: date) -> date:
    """Resolve the next session from the local calendar, with the audited holiday fallback."""

    local_date = await db.scalar(
        select(TradeCalendarModel.trade_date)
        .where(
            TradeCalendarModel.trade_date > trade_date,
            TradeCalendarModel.is_trade_day.is_(True),
        )
        .order_by(TradeCalendarModel.trade_date)
        .limit(1)
    )
    if local_date is not None and local_date <= trade_date + timedelta(days=40):
        return local_date
    candidate = trade_date + timedelta(days=1)
    for _ in range(40):
        if candidate.weekday() < 5 and not is_official_closed_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise ValueError(f"无法解析 {trade_date.isoformat()} 后的下一交易日")


async def _latest_prediction_run(
    db: AsyncSession,
    trade_date: date,
    as_of_at: datetime,
    snapshot_context: str,
) -> PromotionPredictionRun | None:
    return await db.scalar(
        select(PromotionPredictionRun)
        .where(
            PromotionPredictionRun.reference_trade_date == trade_date,
            PromotionPredictionRun.snapshot_context == snapshot_context,
            PromotionPredictionRun.snapshot_source == "schedule",
            PromotionPredictionRun.as_of_at <= as_of_at,
            PromotionPredictionRun.status == "completed",
        )
        .order_by(desc(PromotionPredictionRun.as_of_at), desc(PromotionPredictionRun.id))
        .limit(1)
    )


async def _run_snapshots(
    db: AsyncSession,
    run: PromotionPredictionRun | None,
) -> list[PromotionPredictionSnapshot]:
    if run is None:
        return []
    return list(
        (
            await db.scalars(
                select(PromotionPredictionSnapshot)
                .where(PromotionPredictionSnapshot.run_id == run.id)
                .order_by(
                    PromotionPredictionSnapshot.target_board,
                    PromotionPredictionSnapshot.rank_position.is_(None),
                    PromotionPredictionSnapshot.rank_position,
                    PromotionPredictionSnapshot.pool_rank,
                )
            )
        ).all()
    )


def _snapshot_brief(row: PromotionPredictionSnapshot) -> dict:
    reason = _loads(row.reason_json, {})
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "target_board": row.target_board,
        "candidate_route": row.candidate_route,
        "probability": row.calibrated_probability,
        "rank_position": row.rank_position,
        "pool_rank": row.pool_rank,
        "actionable": bool(row.actionable),
        "watch_only": bool(row.watch_only),
        "reason": reason.get("reason"),
        "risk_flags": reason.get("risk_flags") or [],
    }


async def _prediction_plan(
    db: AsyncSession,
    *,
    review_date: date,
    analysis_trade_date: date,
    phase: str,
    as_of_at: datetime,
    snapshot_context: str,
) -> tuple[dict, list[PromotionPredictionSnapshot]]:
    run_day = review_date if phase == "intraday" else analysis_trade_date
    run = await _latest_prediction_run(db, run_day, as_of_at, snapshot_context)
    if run is None and run_day != analysis_trade_date:
        run_day = analysis_trade_date
        run = await _latest_prediction_run(db, run_day, as_of_at, snapshot_context)
    snapshots = await _run_snapshots(db, run)
    by_target = {}
    for target in (1, 2):
        target_rows = [row for row in snapshots if int(row.target_board) == target]
        ranked = [row for row in target_rows if row.rank_position is not None]
        by_target[str(target)] = {
            "candidate_count": len(target_rows),
            "ranked_count": len(ranked),
            "actionable_count": sum(bool(row.actionable) for row in target_rows),
            "top_candidates": [_snapshot_brief(row) for row in ranked[:20]],
        }
    return (
        {
            "status": ("stale_for_session" if phase == "intraday" and run_day != review_date else "ready") if run else "missing",
            "plan_trade_date": run_day.isoformat(),
            "run_id": run.id if run else None,
            "model_version": run.model_version if run else None,
            "as_of_at": run.as_of_at.isoformat(timespec="seconds") if run else None,
            "quality_gate_passed": run.gate_passed if run else None,
            "targets": by_target,
        },
        snapshots,
    )


async def _prediction_outcome_review(
    db: AsyncSession,
    *,
    outcome_trade_date: date,
    as_of_at: datetime,
    snapshot_context: str,
    market_regime: str | None,
) -> tuple[dict, list[dict]]:
    prediction_trade_date = await _previous_market_date(db, outcome_trade_date)
    if prediction_trade_date is None:
        return {"status": "no_previous_session", "targets": {}}, []
    run = await _latest_prediction_run(
        db, prediction_trade_date, as_of_at, snapshot_context
    )
    snapshots = await _run_snapshots(db, run)
    raw_actual_rows = list(
        (
            await db.scalars(
                select(LimitUpPool).where(
                    LimitUpPool.trade_date == outcome_trade_date,
                    LimitUpPool.quarantined.is_(False),
                )
            )
        ).all()
    )
    # Prediction quality must use the same main-board + StockTag eligibility
    # boundary as the canonical promotion learning review.  The market heat
    # panel still keeps the full non-quarantined limit-up pool; only model
    # scoring is scoped.
    eligible_actual_items = await stock_tagger.filter_signals(
        db,
        [
            {"code": row.code, "name": row.name or ""}
            for row in raw_actual_rows
            if str(row.code or "").strip()
        ],
        mark_observe=False,
    )
    eligible_actual_codes = {
        str(item.get("code") or "").strip() for item in eligible_actual_items
    }
    actual_rows = [
        row
        for row in raw_actual_rows
        if str(row.code or "").strip() in eligible_actual_codes
        and stock_tagger.is_tradeable(str(row.code or "").strip())
    ]
    outcome_changes = {
        str(code or "").strip(): parsed
        for code, change in (
            await db.execute(
                select(StockKline.code, StockKline.change_pct).where(
                    StockKline.trade_date == outcome_trade_date
                )
            )
        ).all()
        if (parsed := _optional_number(change)) is not None
    }
    targets: dict[str, dict] = {}
    attribution_rows: list[dict] = []
    for target in (1, 2):
        target_snapshots = [row for row in snapshots if int(row.target_board) == target]
        actual_target = {
            row.code: row
            for row in actual_rows
            if (
                int(row.consecutive_days or 1) == 1
                if target == 1
                else int(row.consecutive_days or 1) == 2
            )
        }
        pool_map = {row.code: row for row in target_snapshots}
        ranked_map = {
            row.code: row for row in target_snapshots if row.rank_position is not None
        }
        actionable_codes = {
            row.code for row in target_snapshots if bool(row.actionable)
        }
        complete = bool(run) and len(target_snapshots) >= _MIN_CANDIDATE_COUNT[target]
        actual_codes = set(actual_target)
        pool_codes = set(pool_map)
        ranked_codes = set(ranked_map)
        hit_codes = ranked_codes & actual_codes
        scorecard = {
            "snapshot_complete": complete,
            "actual_universe_scope": "tradeable_main_board_non_quarantined",
            "minimum_candidate_count": _MIN_CANDIDATE_COUNT[target],
            "candidate_count": len(target_snapshots),
            "ranked_count": len(ranked_codes),
            "actual_count": len(actual_codes),
            "pool_hit_count": len(pool_codes & actual_codes),
            "ranked_hit_count": len(hit_codes),
            "actionable_hit_count": len(actionable_codes & actual_codes),
            "pool_recall": _ratio(len(pool_codes & actual_codes), len(actual_codes)),
            "ranked_recall": _ratio(len(hit_codes), len(actual_codes)),
            "ranked_precision": _ratio(len(hit_codes), len(ranked_codes)),
            "actionable_recall": _ratio(
                len(actionable_codes & actual_codes), len(actual_codes)
            ),
            "not_in_pool_count": len(actual_codes - pool_codes) if complete else None,
            "ranking_miss_count": len((actual_codes & pool_codes) - ranked_codes)
            if complete
            else None,
        }
        targets[str(target)] = scorecard
        if not complete:
            continue

        for code, actual in actual_target.items():
            snapshot = pool_map.get(code)
            if snapshot is None:
                attribution_type = "recall_miss"
                primary_reason = "not_in_candidate_pool"
            elif code not in ranked_map:
                attribution_type = "ranking_miss"
                primary_reason = "in_pool_but_not_ranked"
            elif code not in actionable_codes:
                attribution_type = "hit_not_actionable"
                primary_reason = "ranked_hit_blocked_by_trade_gate"
            else:
                attribution_type = "hit"
                primary_reason = "ranked_actionable_hit"
            reason_payload = _loads(snapshot.reason_json, {}) if snapshot else {}
            attribution_rows.append(
                {
                    "prediction_run_id": run.id if run else None,
                    "prediction_snapshot_id": snapshot.id if snapshot else None,
                    "code": code,
                    "name": actual.name or (snapshot.name if snapshot else ""),
                    "target_board": target,
                    "prediction_trade_date": prediction_trade_date,
                    "outcome_trade_date": outcome_trade_date,
                    "model_version": run.model_version if run else None,
                    "market_regime": market_regime,
                    "attribution_type": attribution_type,
                    "primary_reason": primary_reason,
                    "causal_status": "observed_structural_location",
                    "predicted_probability": snapshot.calibrated_probability
                    if snapshot
                    else None,
                    "rank_position": snapshot.rank_position if snapshot else None,
                    "actionable": int(bool(snapshot and snapshot.actionable)),
                    "evidence": {
                        "actual_consecutive_days": int(actual.consecutive_days or 1),
                        "candidate_route": snapshot.candidate_route if snapshot else None,
                        "reason": reason_payload.get("reason"),
                        "risk_flags": reason_payload.get("risk_flags") or [],
                        "claim_boundary": "observed pipeline location, not proven market causality",
                    },
                }
            )

        for code in sorted(ranked_codes - actual_codes):
            snapshot = ranked_map[code]
            reason_payload = _loads(snapshot.reason_json, {})
            attribution_rows.append(
                {
                    "prediction_run_id": run.id if run else None,
                    "prediction_snapshot_id": snapshot.id,
                    "code": code,
                    "name": snapshot.name,
                    "target_board": target,
                    "prediction_trade_date": prediction_trade_date,
                    "outcome_trade_date": outcome_trade_date,
                    "model_version": run.model_version if run else None,
                    "market_regime": market_regime,
                    "attribution_type": "false_positive",
                    "primary_reason": "ranked_prediction_not_realized",
                    "causal_status": "hypothesis_not_proven",
                    "predicted_probability": snapshot.calibrated_probability,
                    "rank_position": snapshot.rank_position,
                    "actionable": int(bool(snapshot.actionable)),
                    "evidence": {
                        "observed_change_pct": outcome_changes.get(code),
                        "candidate_route": snapshot.candidate_route,
                        "reason": reason_payload.get("reason"),
                        "risk_flags": reason_payload.get("risk_flags") or [],
                        "claim_boundary": "prediction error is observed; causal reason needs analyst verification",
                    },
                }
            )

    return (
        {
            "status": "complete" if run and all(
                item["snapshot_complete"] for item in targets.values()
            ) else "snapshot_incomplete",
            "prediction_trade_date": prediction_trade_date.isoformat(),
            "outcome_trade_date": outcome_trade_date.isoformat(),
            "run_id": run.id if run else None,
            "model_version": run.model_version if run else None,
            "targets": targets,
            "attribution_count": len(attribution_rows),
            "guardrail": "incomplete snapshots never turn missing rows into not-in-pool failures",
        },
        attribution_rows,
    )


async def _market_dimensions(
    db: AsyncSession,
    *,
    analysis_trade_date: date,
    review_date: date,
    phase: str,
    as_of_at: datetime,
    plan_snapshots: list[PromotionPredictionSnapshot],
) -> tuple[dict, dict[str, bool]]:
    market_trade_date = review_date if phase == "intraday" else analysis_trade_date
    # 没有逐次观测版本的日表只能现场捕获，不能拿当前行重建历史午间状态。
    captured_at = datetime.now()
    live_capture = (phase == "intraday" and captured_at.date() == review_date
                    and 0 <= (captured_at - as_of_at).total_seconds() <= 60)
    unversioned_allowed = phase != "intraday" or live_capture
    market_open = datetime.combine(market_trade_date, time(9, 30))
    freshness_cutoff = (datetime.combine(review_date, time(11, 30))
                        if phase == "intraday" and time(11, 30) < as_of_at.time() < time(13)
                        else as_of_at)
    bar_rows = (
        await db.execute(
            select(
                StockKline.code,
                StockKline.change_pct,
                StockKline.amount,
                StockKline.turnover,
                StockKline.close,
                StockKline.high,
                StockKline.low,
                StockKline.open,
                StockKline.prev_close,
            ).where(StockKline.trade_date == analysis_trade_date)
        )
    ).all()
    change_values = [
        parsed
        for row in bar_rows
        if (parsed := _optional_number(row[1])) is not None
    ]
    turnover_values = [
        parsed
        for row in bar_rows
        if (parsed := _optional_number(row[3])) is not None
    ]
    changes = np.asarray(change_values, dtype=float)
    turnovers = np.asarray(turnover_values, dtype=float)
    kline_change_coverage = _ratio(len(change_values), len(bar_rows))
    sentiment = await db.scalar(
        select(MarketSentiment).where(
            MarketSentiment.trade_date == market_trade_date
        )
    )
    if sentiment is not None:
        observed = sentiment.observed_at
        if ((observed is not None and observed > as_of_at)
            or (phase == "intraday" and (
                observed is None or observed < market_open
                or observed < freshness_cutoff - timedelta(minutes=10)
            ))):
            sentiment = None
    limit_rows = list(
        (
            await db.scalars(
                select(LimitUpPool).where(
                    LimitUpPool.trade_date == market_trade_date,
                    LimitUpPool.quarantined.is_(False),
                )
            )
        ).all()
    )
    if not unversioned_allowed:
        limit_rows = []
    # 当前涨停状态与上一完整交易日对比，盘中不能偷用日终K线结算。
    previous_trade_date = await _previous_market_date(db, market_trade_date)
    previous_limit_rows = (
        list(
            (
                await db.scalars(
                    select(LimitUpPool).where(
                        LimitUpPool.trade_date == previous_trade_date,
                        LimitUpPool.quarantined.is_(False),
                    )
                )
            ).all()
        )
        if previous_trade_date is not None
        else []
    )
    previous_board_codes = {
        str(row.code or "").strip()
        for row in previous_limit_rows
        if str(row.code or "").strip()
    }
    previous_high_board_codes = {
        str(row.code or "").strip()
        for row in previous_limit_rows
        if str(row.code or "").strip() and int(row.consecutive_days or 1) >= 2
    }
    bar_change_by_code = {
        str(row[0] or "").strip(): parsed
        for row in bar_rows
        if row[0] is not None
        and (parsed := _optional_number(row[1])) is not None
    } if bar_rows else {}
    today_continue_codes = {
        str(row.code or "").strip()
        for row in limit_rows
        if str(row.code or "").strip() in previous_board_codes
    }
    today_continues = [
        str(row.code or "").strip()
        for row in limit_rows
        if str(row.code or "").strip() in previous_high_board_codes
    ]
    def _mean_premium(codes: set[str]) -> float | None:
        premiums = [
            bar_change_by_code[code]
            for code in codes
            if code in bar_change_by_code
        ]
        return round(float(np.mean(premiums)), 4) if premiums else None
    high_board_promotion_rate = (
        len(today_continues) / len(previous_high_board_codes)
        if previous_high_board_codes
        else None
    )
    if phase == "intraday":
        from app.data.fund_flow_clock import evidence_clock

        # Legacy SQLite rows contain both ISO "T" and ORM " " separators.
        # Parse the bounded single-day universe instead of lexically comparing
        # them or rounding SQL datetime() to seconds (which admits future micros).
        fund_lower_bound = max(market_open, freshness_cutoff - timedelta(minutes=10))
        day_fund_rows = list((await db.scalars(
            select(FundFlow).where(
                FundFlow.trade_date == market_trade_date,
                FundFlow.observed_at.is_not(None),
            ).order_by(desc(FundFlow.main_net_inflow))
        )).all())
        visible_fund_rows = [
            row for row in day_fund_rows
            if (observed := evidence_clock(row.observed_at)) is not None
            and fund_lower_bound <= observed <= as_of_at
        ]
        fund_flow_record_count = len(visible_fund_rows)
        fund_rows = visible_fund_rows[:30]
    else:
        fund_flow_record_count = int(
            await db.scalar(
                select(func.count()).select_from(FundFlow)
                .where(FundFlow.trade_date == market_trade_date)
            ) or 0
        )
        fund_rows = list((await db.scalars(
            select(FundFlow).where(FundFlow.trade_date == market_trade_date)
            .order_by(desc(FundFlow.main_net_inflow)).limit(30)
        )).all())
    sector_rows = list(
        (
            await db.scalars(
                select(SectorPersistence)
                .where(SectorPersistence.trade_date == market_trade_date)
                .order_by(desc(SectorPersistence.strength_score))
                .limit(30)
            )
        ).all()
    )
    lifecycle_rows = list(
        (
            await db.scalars(
                select(SectorLifecycle)
                .where(SectorLifecycle.trade_date == market_trade_date)
                .order_by(desc(SectorLifecycle.state_score))
                .limit(20)
            )
        ).all()
    )

    if not unversioned_allowed:
        sector_rows, lifecycle_rows = [], []

    # Premarket spans the last actual stored session's close, including holidays.
    # The existing PIT reader owns revision/availability and role attribution.
    news_start = (
        datetime.combine(analysis_trade_date, time(15))
        if phase == "premarket" else as_of_at - timedelta(hours=14)
    )
    news_evidence = await _read_news_window(
        db, start_time=news_start, as_of=as_of_at, limit=_NEWS_SELECTION_LIMIT,
    )
    news_rows = news_evidence["items"]

    use_spot = phase == "intraday"
    spot_rows = (
        list(
            (
                await db.scalars(
                    select(StockSpot).where(
                        StockSpot.updated_at <= as_of_at,
                        StockSpot.updated_at >= max(market_open, freshness_cutoff - timedelta(seconds=120)),
                        func.coalesce(StockSpot.source_quote_at, StockSpot.updated_at) <= as_of_at,
                        func.coalesce(StockSpot.source_quote_at, StockSpot.updated_at) >= max(
                            market_open, freshness_cutoff - timedelta(seconds=120)),
                        StockSpot.price > 0,
                    )
                )
            ).all()
        )
        if use_spot
        else []
    )
    spot_change_values = [
        parsed
        for row in spot_rows
        if (parsed := _optional_number(row.change_pct)) is not None
    ]
    spot_changes = np.asarray(spot_change_values, dtype=float)
    if phase == "intraday":
        # 不用昨日宽度/换手补成当日行情；日线基准另列，分时缺失就是未知。
        changes = np.asarray([], dtype=float)
        turnovers = np.asarray([value for row in spot_rows
                                if (value := _optional_number(row.turnover)) is not None], dtype=float)
        bar_change_by_code = {row.code: value for row in spot_rows
                              if (value := _optional_number(row.change_pct)) is not None}
    plan_codes = {row.code for row in plan_snapshots}
    candidate_spots = [row for row in spot_rows if row.code in plan_codes]

    # 历史/盘后复盘优先使用当日截面基本面快照；缺失则诚实标记 unavailable。
    fundamental_history = (
        list(
            (
                await db.scalars(
                    select(StockFundamentalDaily).where(
                        StockFundamentalDaily.trade_date == analysis_trade_date,
                        StockFundamentalDaily.code.in_(plan_codes),
                    )
                )
            ).all()
        )
        if plan_codes
        else []
    )
    use_history = bool(fundamental_history)
    fundamental_candidates = (
        fundamental_history
        if use_history
        else candidate_spots
    )
    if use_history:
        fundamental_source = "daily_snapshot"
        fundamental_availability_reason = None
        fundamental_scope = (
            "冻结观察池的当日基本面截面，用于识别估值和盈利质量风险；"
            "不用于判断市场涨跌，也不是独立买入信号。"
        )
    elif candidate_spots:
        fundamental_source = "intraday_spot"
        fundamental_availability_reason = None
        fundamental_scope = (
            "冻结观察池的盘中基本面截面，用于识别估值和盈利质量风险；"
            "不代表全市场，也不是独立买入信号。"
        )
    elif not plan_codes:
        fundamental_source = "unavailable"
        fundamental_availability_reason = "prediction_plan_missing"
        fundamental_scope = (
            "当前阶段尚未冻结下一交易日观察池，因此没有基本面统计对象；"
            "页面中的空值不代表 PE、PB 或利润增速为 0。"
        )
    elif phase == "intraday" and not spot_rows:
        fundamental_source = "unavailable"
        fundamental_availability_reason = "intraday_spot_missing"
        fundamental_scope = "观察池已存在，但当前 as-of 时点缺少盘中基本面截面。"
    else:
        fundamental_source = "unavailable"
        fundamental_availability_reason = "daily_snapshot_missing"
        fundamental_scope = (
            "观察池已存在，但当日冻结基本面截面缺失；历史复盘不会用当前数据回填。"
        )

    reason_counts = Counter(
        str(row.limit_up_reason or "未归因").strip() or "未归因" for row in limit_rows
    )
    high_boards = sorted(
        limit_rows,
        key=lambda row: (int(row.consecutive_days or 1), _number(row.seal_amount)),
        reverse=True,
    )[:20]
    positive_news = news_evidence["positive_count"]
    negative_news = news_evidence["negative_count"]
    neutral_news = news_evidence["neutral_count"]
    sampled_flow_values = [
        parsed
        for row in fund_rows
        if (parsed := _optional_number(row.main_net_inflow)) is not None
    ]
    valid_pe = [
        parsed
        for row in fundamental_candidates
        if (parsed := _optional_number(row.pe_ttm)) is not None and parsed > 0
    ]
    valid_pb = [
        parsed
        for row in fundamental_candidates
        if (parsed := _optional_number(row.pb)) is not None and parsed > 0
    ]
    valid_growth = [
        parsed
        for row in fundamental_candidates
        if (parsed := _optional_number(row.net_profit_growth)) is not None
    ]
    requested_fundamental_count = len(plan_codes)
    fundamental_coverage = (
        _ratio(len(fundamental_candidates), requested_fundamental_count)
        if requested_fundamental_count
        else None
    )
    turnover_total_trillion = _optional_number(
        sentiment.turnover_total if sentiment is not None else None
    )
    if turnover_total_trillion is not None and turnover_total_trillion <= 0:
        turnover_total_trillion = None
    limit_sources = {
        str(row.source or "").strip().lower()
        for row in limit_rows
        if str(row.source or "").strip()
    }
    if limit_sources == {"eastmoney"}:
        reason_basis = "industry_classification"
        reason_label = "所属行业归类"
    elif limit_sources == {"pywencai"}:
        reason_basis = "cause_category"
        reason_label = "涨停原因类别"
    elif limit_sources:
        reason_basis = "mixed_source_classification"
        reason_label = "行业 / 原因归类"
    else:
        reason_basis = "unknown"
        reason_label = "归类信息"
    sentiment_complete = (
        sentiment is not None
        and str(sentiment.quality_status or "").strip().lower() == "ok"
        and all(
            getattr(sentiment, field, None) is not None
            for field in (
                "limit_up_count",
                "limit_down_count",
                "broken_limit_count",
                "seal_rate",
                "board_height",
            )
        )
    )
    limit_pool_known = bool(limit_rows) or (
        sentiment is not None
        and sentiment.limit_up_count is not None
        and int(_number(sentiment.limit_up_count)) == 0
    )

    dimensions = {
        "capital": {
            "analysis_trade_date": market_trade_date.isoformat(),
            "source_trade_date": market_trade_date.isoformat(),
            "market_main_net_inflow": _optional_number(
                sentiment.main_net_inflow if sentiment is not None else None
            ),
            "market_main_net_inflow_scope": "tradeable_non_st",
            "market_main_net_inflow_source": "market_sentiment",
            "sampled_stock_main_net_inflow": (
                round(sum(sampled_flow_values), 4)
                if sampled_flow_values
                else None
            ),
            "sampled_stock_count": len(sampled_flow_values),
            "fund_flow_record_count": fund_flow_record_count,
            "sample_basis": "top_main_net_inflow_30",
            "top_stock_inflows": [
                {
                    "code": row.code,
                    "name": row.name,
                    "main_net_inflow": row.main_net_inflow,
                    "main_net_inflow_pct": row.main_net_inflow_pct,
                }
                for row in fund_rows[:12]
            ],
            "top_sectors": [
                {
                    "sector_code": row.sector_code,
                    "sector_name": row.sector_name,
                    "strength_score": row.strength_score,
                    "fund_flow": row.fund_flow,
                    "change_pct": row.change_pct,
                    "consecutive_days": row.consecutive_days,
                    "limit_up_count": row.limit_up_count,
                }
                for row in sector_rows[:12]
            ],
        },
        "news": {
            **news_evidence,
            "window_start": news_start.isoformat(timespec="seconds"),
            "as_of_at": news_evidence["as_of_at"],
            "count_scope": "as_of_window",  # Existing view field; see count_basis/coverage.
            "count_basis": "bounded_pit_window_not_legacy_projection",
            "positive_count": positive_news,
            "negative_count": negative_news,
            "neutral_count": neutral_news,
        },
        "fundamental": {
            "status": fundamental_source,
            "source_trade_date": (analysis_trade_date if use_history else market_trade_date).isoformat(),
            "purpose": "检查冻结观察池的估值与盈利质量风险，不参与市场择时，也不单独产生买点。",
            "scope": fundamental_scope,
            "availability_reason": fundamental_availability_reason,
            "candidate_count": len(fundamental_candidates),
            "requested_candidate_count": requested_fundamental_count,
            "coverage_ratio": fundamental_coverage,
            "valid_pe_count": len(valid_pe),
            "valid_pb_count": len(valid_pb),
            "valid_growth_count": len(valid_growth),
            "median_pe_ttm": round(float(np.median(valid_pe)), 4) if valid_pe else None,
            "median_pb": round(float(np.median(valid_pb)), 4) if valid_pb else None,
            "median_net_profit_growth": round(float(np.median(valid_growth)), 4)
            if valid_growth
            else None,
            "candidates": [
                {
                    "code": row.code,
                    "name": row.name,
                    "pe_ttm": row.pe_ttm,
                    "pb": row.pb,
                    "net_profit_growth": row.net_profit_growth,
                    "circ_market_cap": row.circ_market_cap,
                }
                for row in fundamental_candidates[:30]
            ],
        },
        "technical": {
            "source": ("intraday_spot" if spot_rows else "unavailable") if phase == "intraday" else "stock_kline_close",
            "source_trade_date": market_trade_date.isoformat(),
            "technical_trade_date": analysis_trade_date.isoformat(),
            "closed_kline_baseline": {
                "source_trade_date": analysis_trade_date.isoformat(),
                "universe_count": len(bar_rows), "change_coverage": kline_change_coverage,
                "advance_ratio": round(float(np.mean(np.asarray(change_values) > 0)), 6) if change_values else None,
            },
            "intraday_quote_count": len(spot_change_values),
            "intraday_coverage_vs_prior_kline": _ratio(len(spot_change_values), len(bar_rows)) if bar_rows else None,
            "sentiment_quality_status": (
                sentiment.quality_status if sentiment is not None else None
            ),
            "sentiment_quality_reason": (
                sentiment.quality_reason if sentiment is not None else None
            ),
            "sentiment_observed_at": (
                sentiment.observed_at.isoformat(timespec="seconds")
                if sentiment is not None and sentiment.observed_at is not None
                else None
            ),
            "change_coverage": kline_change_coverage,
            "universe_count": (
                int(len(spot_changes))
                if len(spot_changes)
                else int(len(changes))
                if len(changes)
                else None
            ),
            "advance_ratio": (
                round(float(np.mean(spot_changes > 0)), 6)
                if len(spot_changes)
                else round(float(np.mean(changes > 0)), 6)
                if len(changes)
                else None
            ),
            "median_return": (
                round(float(np.median(spot_changes)), 6)
                if len(spot_changes)
                else round(float(np.median(changes)), 6)
                if len(changes)
                else None
            ),
            "return_dispersion": (
                round(float(np.std(spot_changes)), 6)
                if len(spot_changes)
                else round(float(np.std(changes)), 6)
                if len(changes)
                else None
            ),
            # 沪深成交额使用情绪快照中上证+深证指数聚合的万亿元口径。
            # 不能再把混合来源个股 K 线 amount 相加：历史 tencent_close
            # 存在不同板块成交量单位不一致，曾把页面放大到十余倍。
            "turnover_total_trillion": turnover_total_trillion,
            "total_amount": (
                round(turnover_total_trillion * 1_000_000_000_000, 2)
                if turnover_total_trillion is not None
                else None
            ),
            "total_amount_source": (
                "market_sentiment_index_aggregate"
                if turnover_total_trillion is not None
                else "unavailable"
            ),
            "median_turnover": (
                round(float(np.median(turnovers)), 4) if len(turnovers) else None
            ),
            "limit_up_count": (
                len(limit_rows)
                if limit_rows
                else int(sentiment.limit_up_count)
                if sentiment is not None and sentiment.limit_up_count is not None
                else None
            ),
            "limit_down_count": (
                int(sentiment.limit_down_count)
                if sentiment is not None and sentiment.limit_down_count is not None
                else None
            ),
            "broken_limit_count": (
                int(sentiment.broken_limit_count)
                if sentiment is not None and sentiment.broken_limit_count is not None
                else None
            ),
            "seal_rate": _optional_number(
                sentiment.seal_rate if sentiment is not None else None
            ),
            "board_height": (
                max(
                    [int(row.consecutive_days or 1) for row in limit_rows]
                    + (
                        [int(sentiment.board_height)]
                        if sentiment is not None and sentiment.board_height is not None
                        else []
                    )
                )
                if limit_rows
                or (sentiment is not None and sentiment.board_height is not None)
                else None
            ),
        },
        "limit_up_learning": {
            "source_trade_date": market_trade_date.isoformat(),
            "outcome_scope": "live_unsettled" if phase == "intraday" else "completed_session",
            "temporal_status": ("live_mutable_capture" if live_capture else "unavailable_historical_asof") if phase == "intraday" else "daily_snapshot",
            "count": len(limit_rows) if limit_pool_known else None,
            "first_board_count": (
                sum(int(row.consecutive_days or 1) <= 1 for row in limit_rows)
                if limit_pool_known
                else None
            ),
            "consecutive_board_count": (
                sum(int(row.consecutive_days or 1) >= 2 for row in limit_rows)
                if limit_pool_known
                else None
            ),
            "previous_trade_date": (
                previous_trade_date.isoformat() if previous_trade_date else None
            ),
            "high_board_promotion_rate": high_board_promotion_rate if limit_pool_known else None,
            "high_board_break_rate": (
                round(1.0 - high_board_promotion_rate, 4)
                if high_board_promotion_rate is not None and limit_pool_known
                else None
            ),
            "high_board_continue_count": len(today_continues) if limit_pool_known else None,
            "high_board_previous_count": len(previous_high_board_codes),
            "previous_board_premium_pct": _mean_premium(previous_board_codes),
            "high_board_premium_pct": _mean_premium(previous_high_board_codes),
            "reason_distribution": [
                {"reason": reason, "count": count}
                for reason, count in reason_counts.most_common(20)
            ],
            "reason_basis": reason_basis,
            "reason_label": reason_label,
            "high_boards": [
                {
                    "code": row.code,
                    "name": row.name,
                    "consecutive_days": int(row.consecutive_days or 1),
                    "limit_up_time": row.limit_up_time,
                    "seal_amount": row.seal_amount,
                    "break_count": row.break_count,
                    "turnover": row.turnover,
                    "reason": row.limit_up_reason,
                    "source": row.source,
                }
                for row in high_boards
            ],
            "mainline_sectors": [
                {
                    "sector_code": row.sector_code,
                    "sector_name": row.sector_name,
                    "state": row.lifecycle_state,
                    "state_score": row.state_score,
                    "quality_score": row.quality_score,
                    "max_board_height": row.max_board_height,
                }
                for row in lifecycle_rows
                if bool(row.is_main_line)
            ][:10],
            "strategy_iteration_sample": _build_strategy_iteration_sample(
                analysis_trade_date=analysis_trade_date,
                bar_rows=bar_rows,
                limit_rows=limit_rows,
                limit_pool_known=limit_pool_known,
            ) if phase != "intraday" else {
                "status": "not_applicable_before_close",
                "note": "盘中涨停、回封与溢价未结算；不得拼接昨日K线生成当日学习标签。",
            },
        },
        "temporal_provenance": {
            "market_trade_date": market_trade_date.isoformat(),
            "technical_trade_date": analysis_trade_date.isoformat(),
            "requested_as_of_at": as_of_at.isoformat(),
            "captured_at": captured_at.isoformat() if live_capture else None,
            "mutable_daily_tables": ["limit_up_pool", "sector_persistence", "sector_lifecycle"],
            "mutable_daily_status": ("live_capture_not_historical_replay" if live_capture else "unavailable_at_historical_asof") if phase == "intraday" else "completed_daily_view",
            "note": "无逐次观测版本的日表不能证明历史盘中状态；仅保留现场读取，历史盘中缺失不回填。",
        },
    }
    presence = {
        "stock_kline": bool(bar_rows) and kline_change_coverage >= 0.80,
        "market_sentiment": sentiment_complete,
        "limit_up_pool": limit_pool_known,
        "fund_flow": bool(sampled_flow_values),
        "sector": bool(sector_rows) or bool(lifecycle_rows),
        "news": bool(news_rows),
        "intraday_spot": (bool(spot_change_values) and _ratio(len(spot_change_values), len(bar_rows)) >= 0.80) if phase == "intraday" else True,
        "intraday_temporal_provenance": False if phase == "intraday" else True,
    }
    return dimensions, presence


def _optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if np.isfinite(parsed) else None


def _count_text(value: Any, *, suffix: str = "") -> str:
    parsed = _optional_number(value)
    return "数据不足" if parsed is None else f"{int(parsed)}{suffix}"


def _pct_ratio(value: Any) -> str:
    parsed = _optional_number(value)
    return "数据不足" if parsed is None else f"{parsed * 100:.1f}%"


def _pct_points(value: Any, *, signed: bool = False) -> str:
    parsed = _optional_number(value)
    if parsed is None:
        return "数据不足"
    prefix = "+" if signed and parsed > 0 else ""
    return f"{prefix}{parsed:.2f}%"


def _flow_yi(value: Any, *, yuan: bool = False) -> str:
    parsed = _optional_number(value)
    if parsed is None:
        return "数据不足"
    amount = parsed / 100_000_000 if yuan else parsed
    return f"{amount:+.2f}亿"


def _regime_view(value: Any) -> dict:
    if isinstance(value, dict):
        code = str(value.get("primary_regime") or "unknown")
        return {
            **value,
            "primary_regime": code,
            "primary_regime_name": value.get("primary_regime_name")
            or REGIME_NAMES_ZH.get(code, "未知"),
        }
    code = str(value or "unknown")
    return {
        "primary_regime": code,
        "primary_regime_name": REGIME_NAMES_ZH.get(code, "未知"),
        "confidence": None,
        "evidence": [],
    }


def build_review_views(
    payload: dict,
    *,
    guidance_trade_date: date | str | None = None,
) -> tuple[dict, dict]:
    """Build a deterministic, human-readable decision view from frozen snapshot leaves."""

    dimensions = payload.get("dimensions") or {}
    technical = dimensions.get("technical") or {}
    capital = dimensions.get("capital") or {}
    learning = dimensions.get("limit_up_learning") or {}
    quality = payload.get("quality") or {}
    prediction_plan = payload.get("prediction_plan") or {}
    prediction_review = payload.get("prediction_review") or {}
    regime = _regime_view(payload.get("market_regime"))
    phase = str(payload.get("phase") or "postmarket")

    source_presence = payload.get("source_presence") or {}
    advance_ratio = _optional_number(technical.get("advance_ratio"))
    median_return = _optional_number(technical.get("median_return"))
    limit_up_count = _optional_number(technical.get("limit_up_count"))
    limit_down_count = _optional_number(technical.get("limit_down_count"))
    broken_limit_count = _optional_number(technical.get("broken_limit_count"))
    seal_rate = _optional_number(technical.get("seal_rate"))
    board_height = _optional_number(technical.get("board_height"))
    market_flow = _optional_number(capital.get("market_main_net_inflow"))
    promotion_rate = _optional_number(learning.get("high_board_promotion_rate"))
    high_board_premium = _optional_number(learning.get("high_board_premium_pct"))
    if not source_presence.get("stock_kline") and not source_presence.get(
        "intraday_spot"
    ):
        advance_ratio = None
        median_return = None
    if not source_presence.get("market_sentiment"):
        limit_down_count = None
        broken_limit_count = None
        seal_rate = None
        market_flow = None
    if not source_presence.get("limit_up_pool"):
        board_height = None
        promotion_rate = None
        high_board_premium = None

    if advance_ratio is None:
        breadth_label = "市场宽度不足"
        breadth_read = "缺少上涨家数口径，不能判断普涨或分化"
        breadth_tone = "muted"
    elif advance_ratio >= 0.60:
        breadth_label = "普涨占优"
        breadth_read = "多数股票上涨，赚钱效应具备广度"
        breadth_tone = "positive"
    elif advance_ratio >= 0.53:
        breadth_label = "涨多跌少"
        breadth_read = "市场宽度偏强，但仍需确认持续性"
        breadth_tone = "positive"
    elif advance_ratio >= 0.47:
        breadth_label = "涨跌分化"
        breadth_read = "指数或热点不能代表全市场，宜择强不追高"
        breadth_tone = "neutral"
    elif advance_ratio >= 0.40:
        breadth_label = "跌多涨少"
        breadth_read = "赚钱效应收缩，仓位与追涨条件应更严格"
        breadth_tone = "warning"
    else:
        breadth_label = "普跌压力"
        breadth_read = "市场宽度明显偏弱，优先控制回撤"
        breadth_tone = "danger"

    if market_flow is None:
        flow_label = "资金方向未知"
        flow_read = "可交易池主力资金口径缺失"
        flow_tone = "muted"
    elif market_flow > 30:
        flow_label = "主力净流入"
        flow_read = "资金面与上涨方向形成正向确认"
        flow_tone = "positive"
    elif market_flow < -30:
        flow_label = "主力净流出"
        flow_read = "资金承接不足，强势题材需防冲高回落"
        flow_tone = "warning"
    else:
        flow_label = "资金近均衡"
        flow_read = "可交易池资金方向不强，更多依赖结构性机会"
        flow_tone = "neutral"

    if seal_rate is None:
        emotion_label = "封板质量未知"
        emotion_read = "涨停与炸板口径不完整"
        emotion_tone = "muted"
    elif (
        seal_rate >= 70
        and broken_limit_count is not None
        and limit_up_count is not None
        and broken_limit_count <= max(limit_up_count * 0.4, 8)
    ):
        emotion_label = "封板质量较好"
        emotion_read = "短线情绪承接尚可，但高标仍需逐只确认"
        emotion_tone = "positive"
    elif seal_rate >= 58:
        emotion_label = "情绪有分歧"
        emotion_read = "涨停扩散同时伴随炸板，次日更适合等分歧确认"
        emotion_tone = "neutral"
    else:
        emotion_label = "炸板压力偏高"
        emotion_read = "追涨容错率下降，需防高位负反馈扩散"
        emotion_tone = "danger"

    regime_code = regime["primary_regime"]
    regime_name = regime["primary_regime_name"]
    stance, stance_label = _REGIME_STANCE.get(
        regime_code, _REGIME_STANCE["unknown"]
    )
    quality_status = str(quality.get("status") or payload.get("quality_status") or "insufficient")
    market_evidence_ready = all(
        item is not None
        for item in (
            advance_ratio,
            limit_up_count,
            limit_down_count,
            broken_limit_count,
            seal_rate,
        )
    )
    if not market_evidence_ready:
        stance, stance_label = "defensive", "市场证据不足"
    elif quality_status != "good" and stance == "offensive":
        stance, stance_label = "selective", "数据降级择强"
    if (
        (advance_ratio is not None and advance_ratio < 0.40)
        or (seal_rate is not None and seal_rate < 50)
        or regime_code in {"risk_off", "high_board_speculation"}
    ):
        stance, stance_label = "defensive", "防守等待"

    regime_confidence = _optional_number(regime.get("confidence"))
    quality_score = _optional_number(quality.get("score"))
    confidence_values = [item for item in (regime_confidence, quality_score) if item is not None]
    confidence = min(confidence_values) if confidence_values else 0.0
    if quality_status != "good":
        confidence *= 0.85
    if not market_evidence_ready:
        confidence = 0.0
    confidence = round(max(0.0, min(confidence, 1.0)), 4)

    high_board_value = (
        f"最高 {_count_text(board_height, suffix='板')}；高标晋级 {_pct_ratio(promotion_rate)}；"
        f"上一批高标当日溢价 {_pct_points(high_board_premium, signed=True)}"
    )
    evidence = [
        {
            "label": "市场宽度",
            "value": f"上涨 {_pct_ratio(advance_ratio)} / 中位涨幅 {_pct_points(median_return, signed=True)}",
            "interpretation": breadth_read,
            "tone": breadth_tone,
        },
        {
            "label": "短线情绪",
            "value": (
                f"涨停 {_count_text(limit_up_count)} / 跌停 {_count_text(limit_down_count)} / "
                f"炸板 {_count_text(broken_limit_count)} / 封板率 {_pct_points(seal_rate)}"
            ),
            "interpretation": emotion_read,
            "tone": emotion_tone,
        },
        {
            "label": "资金方向",
            "value": f"可交易池主力 {_flow_yi(market_flow)}",
            "interpretation": flow_read,
            "tone": flow_tone,
        },
        {
            "label": "高标反馈",
            "value": high_board_value,
            "interpretation": (
                "晋级与溢价共同用于判断接力容错，不把单只高标表现外推为全市场。"
            ),
            "tone": "positive"
            if promotion_rate is not None and promotion_rate >= 0.5
            else "warning",
        },
    ]

    risks: list[str] = []
    for warning in quality.get("warnings") or []:
        risks.append(str(warning))
    if market_flow is not None and market_flow < -30:
        risks.append(f"可交易池主力净流出 {abs(market_flow):.2f}亿，热点承接需二次确认")
    if seal_rate is not None and seal_rate < 60:
        risks.append(f"封板率仅 {seal_rate:.1f}%，追涨失败率可能上升")
    if (
        limit_up_count is not None
        and limit_up_count > 0
        and broken_limit_count is not None
        and broken_limit_count > limit_up_count * 0.5
    ):
        risks.append(f"炸板 {int(broken_limit_count)} 家，已超过涨停家数的一半")
    if promotion_rate is not None and promotion_rate < 0.5:
        risks.append(f"高标晋级率 {_pct_ratio(promotion_rate)}，接力容错偏低")
    if prediction_review.get("status") == "snapshot_incomplete":
        risks.append("上一交易日预测快照不完整，本日不能计算候选池漏失")
    risks = list(dict.fromkeys(item for item in risks if item))

    phase_prefix = {
        "premarket": "盘前",
        "intraday": "盘中",
        "postmarket": "盘后",
    }.get(phase, "复盘")
    headline = f"{regime_name} · {breadth_label} · {flow_label} · {emotion_label}"
    summary = (
        f"{phase_prefix}证据显示当前风格为“{regime_name}”。"
        f"市场宽度呈现{breadth_label}，资金面为{flow_label}，短线端{emotion_label}；"
        f"执行基调定为“{stance_label}”，只在触发条件成立后观察，不把复盘结论当作收益承诺。"
    )
    conclusion = {
        "view_schema_version": REVIEW_VIEW_SCHEMA_VERSION,
        "headline": headline,
        "summary": summary,
        "stance": stance,
        "stance_label": stance_label,
        "confidence": confidence,
        "quality_status": quality_status,
        "regime_code": regime_code,
        "regime_name": regime_name,
        "evidence": evidence,
        "risks": risks,
        "data_cutoff": payload.get("as_of_at"),
        "analysis_trade_date": payload.get("analysis_trade_date"),
    }

    mainline_rows = learning.get("mainline_sectors") or []
    top_sectors = capital.get("top_sectors") or []
    focus_sectors = mainline_rows[:3] or top_sectors[:3]
    sector_names = [
        str(item.get("sector_name") or "").strip()
        for item in focus_sectors
        if str(item.get("sector_name") or "").strip()
    ]
    high_boards = learning.get("high_boards") or []
    board_names = [
        f"{item.get('name') or item.get('code')}（{int(_number(item.get('consecutive_days'), 1))}板）"
        for item in high_boards[:3]
    ]

    targets = prediction_plan.get("targets") or {}
    watchlist: list[dict] = []
    for target in (2, 1):
        target_rows = (targets.get(str(target)) or {}).get("top_candidates") or []
        actionable_rows = [item for item in target_rows if bool(item.get("actionable"))]
        selected_rows = actionable_rows[:4] if actionable_rows else target_rows[:2]
        for item in selected_rows:
            if len(watchlist) >= 6:
                break
            watchlist.append(
                {
                    "code": item.get("code"),
                    "name": item.get("name"),
                    "target_board": target,
                    "rank_position": item.get("rank_position"),
                    "probability": item.get("probability"),
                    "actionable": bool(item.get("actionable")),
                    "candidate_route": item.get("candidate_route"),
                    "role": "晋级观察" if target == 2 else "首板观察",
                    "condition": "仅在非一字、未触及涨停且盘中量价确认时观察",
                }
            )

    focus: list[dict] = []
    if sector_names:
        focus.append(
            {
                "title": "主线与强势板块",
                "items": sector_names,
                "trigger": "板块强度、涨停梯队和资金方向至少两项继续确认",
                "invalidation": "板块高开低走、核心股连续炸板或资金快速转负",
            }
        )
    if board_names:
        focus.append(
            {
                "title": "高标情绪锚",
                "items": board_names,
                "trigger": "竞价非一字，开盘后换手承接且回封质量改善",
                "invalidation": "龙头快速跌停、同梯队批量断板或炸板率继续上升",
            }
        )
    if watchlist:
        focus.append(
            {
                "title": "冻结模型观察池",
                "items": [f"{item['name']}（{item['code']}）" for item in watchlist[:4]],
                "trigger": "只按冻结排名与交易门禁复核，不使用次日结果回填概率",
                "invalidation": "风险标记触发、超出计划价格区间或数据质量降级",
            }
        )

    target_date = (
        guidance_trade_date.isoformat()
        if isinstance(guidance_trade_date, date)
        else str(guidance_trade_date or payload.get("next_trade_date") or "")
        or None
    )
    guide = {
        "view_schema_version": REVIEW_VIEW_SCHEMA_VERSION,
        "trade_date": target_date,
        "stance": stance,
        "stance_label": stance_label,
        "summary": (
            f"目标交易日以“{stance_label}”为基调：先看宽度和封板质量，再看主线承接，"
            "最后才核对个股；任何观察池都不是自动买入指令。"
        ),
        "focus": focus,
        "watchlist": watchlist,
        "playbook": [
            {
                "scenario": "强势延续",
                "tone": "positive",
                "trigger": "上涨占比不低于55%、封板率升至65%以上，且主线资金未明显转负",
                "response": "只跟踪主线核心与已冻结观察池，等待换手确认，避免追一字或加速板",
                "invalidation": "宽度跌破45%或主线核心批量炸板",
            },
            {
                "scenario": "分歧修复",
                "tone": "warning",
                "trigger": "指数/宽度回落，但主线核心未破关键支撑且炸板后出现有效回封",
                "response": "缩小观察范围，优先看有板块共振和明确失效位的标的",
                "invalidation": "大盘净流出扩大、跌停扩散或高标负反馈增强",
            },
            {
                "scenario": "风险收缩",
                "tone": "danger",
                "trigger": "上涨占比低于40%、封板率低于50%，或跌停/炸板同步扩散",
                "response": "停止新增追涨观察，优先处理风险与等待下一次完整快照",
                "invalidation": "宽度和封板率连续恢复，且关键数据源重新通过质量门禁",
            },
        ],
        "do_not": [
            "不追一字板、连续加速且无换手的高标",
            "不把单日命中或失误直接用于修改 Champion 参数",
            "数据源或预测快照不完整时，不把缺失记录归因为模型漏选",
        ],
        "quality_status": quality_status,
        "source_as_of": payload.get("as_of_at"),
        "disclaimer": "条件化观察指引，不构成收益承诺，也不会触发自动下单。",
    }
    return conclusion, guide


def _action_plan(phase: str, payload: dict) -> list[dict]:
    regime = _regime_view(payload.get("market_regime"))
    plan = payload.get("prediction_plan") or {}
    outcome = payload.get("prediction_review") or {}
    quality = payload.get("quality") or {}
    common = [
        {
            "priority": "P0",
            "action": "保持 Champion 参数冻结",
            "reason": "单日结果只进入归因样本，不直接触发权重和阈值改写",
        },
        {
            "priority": "P1",
            "action": "按风格切片累计样本",
            "reason": f"当前风格为 {regime.get('primary_regime_name') or '未知'}，只作上下文和分层统计",
        },
    ]
    if quality.get("status") != "good":
        common.insert(
            0,
            {
                "priority": "P0",
                "action": "先修复缺失数据源，再解释模型表现",
                "reason": "；".join(quality.get("warnings") or []) or "当前快照质量未通过完整门禁",
            },
        )
    if phase == "premarket":
        return [
            {
                "priority": "P0",
                "action": "核对盘前消息、竞价与昨晚冻结候选",
                "reason": f"预测计划状态 {plan.get('status', 'missing')}；不允许用开盘后信息回填盘前快照",
            },
            *common,
        ]
    if phase == "intraday":
        return [
            {
                "priority": "P0",
                "action": "只记录确认/失效信号，不覆盖原始预测",
                "reason": "盘中快照用于执行确认与漂移监控，不作为收盘训练标签",
            },
            *common,
        ]
    return [
        {
            "priority": "P0",
            "action": "按召回、排序、校准和交易门禁分别复盘",
            "reason": f"本次预测复盘状态 {outcome.get('status', 'unknown')}；快照不完整时禁止归因为未入池",
        },
        *common,
    ]


async def build_daily_review_snapshot(
    db: AsyncSession,
    *,
    review_date: date | None = None,
    phase: str = "postmarket",
    as_of_at: datetime | None = None,
    snapshot_context: str = "",
    persist: bool = True,
) -> dict:
    """Build an auditable stage snapshot; persistence never mutates model weights."""

    phase = str(phase or "postmarket").strip().lower()
    if phase not in REVIEW_PHASES:
        raise ValueError(f"phase must be one of {REVIEW_PHASES}")
    snapshot_context = str(snapshot_context or "").strip() or {
        "premarket": "promotion_2000",
        "intraday": "promotion_0935",
        "postmarket": "promotion_2000",
    }[phase]
    if review_date is None:
        latest_local_trade_date = await db.scalar(
            select(StockKline.trade_date)
            .order_by(desc(StockKline.trade_date))
            .limit(1)
        )
        if latest_local_trade_date is None:
            raise ValueError("no StockKline trade date is available for daily review")
        review_date = (
            latest_local_trade_date
            if phase == "postmarket"
            else await trade_calendar.next_trade_day(latest_local_trade_date)
        )
    now = datetime.now()
    if review_date > now.date():
        raise ValueError("review_date 不能晚于当前日期，禁止预建未来复盘快照")
    as_of_at = as_of_at or _default_as_of(review_date, phase)
    if (
        phase == "postmarket"
        and review_date == now.date()
        and now.time() < time(hour=15, minute=5)
    ):
        raise ValueError("当日盘后复盘需等待 15:05 收盘数据补全后再生成")
    _validate_phase_as_of(
        review_date=review_date,
        phase=phase,
        as_of_at=as_of_at,
    )
    if phase in {"premarket", "intraday"} and not await _is_review_trade_day(
        db, review_date
    ):
        raise ValueError(f"{review_date.isoformat()} 不是交易日，不能生成{phase}快照")
    analysis_trade_date = await _resolve_analysis_date(db, review_date, phase)
    if phase == "postmarket" and analysis_trade_date != review_date:
        raise ValueError(
            f"{review_date.isoformat()} 尚无完整收盘数据；最近可用交易日为 "
            f"{analysis_trade_date.isoformat()}，请改选该日期或等待盘后数据完成"
        )
    guidance_trade_date = (
        review_date
        if phase in {"premarket", "intraday"} and review_date > analysis_trade_date
        else await _next_guidance_date(db, analysis_trade_date)
    )

    prediction_plan, plan_snapshots = await _prediction_plan(
        db,
        review_date=review_date,
        analysis_trade_date=analysis_trade_date,
        phase=phase,
        as_of_at=as_of_at,
        snapshot_context=snapshot_context,
    )
    dimensions, source_presence = await _market_dimensions(
        db,
        analysis_trade_date=analysis_trade_date,
        review_date=review_date,
        phase=phase,
        as_of_at=as_of_at,
        plan_snapshots=plan_snapshots,
    )
    market_trade_date = review_date if phase == "intraday" else analysis_trade_date
    market_regime = {
        "primary_regime": "unknown", "primary_regime_name": "未知",
        "quality_status": "insufficient", "evidence": [],
        "trade_date": market_trade_date.isoformat(),
    }
    if phase == "intraday":
        # 只读取此前已冻结的本日风格；不能用昨日盘后风格或事后重算冒充当前。
        regime_row = await db.scalar(select(MarketRegimeSnapshot).where(
            MarketRegimeSnapshot.trade_date == market_trade_date,
            MarketRegimeSnapshot.as_of_at <= as_of_at,
            MarketRegimeSnapshot.created_at <= as_of_at,
            MarketRegimeSnapshot.as_of_at >= max(
                datetime.combine(market_trade_date, time(9, 30)),
                as_of_at - timedelta(minutes=30)),
        ).order_by(desc(MarketRegimeSnapshot.as_of_at), desc(MarketRegimeSnapshot.id)).limit(1))
        if regime_row is not None:
            market_regime.update({
                "primary_regime": regime_row.primary_regime,
                "primary_regime_name": REGIME_NAMES_ZH.get(regime_row.primary_regime, "未知"),
                "quality_status": regime_row.quality_status,
                "snapshot_key": regime_row.snapshot_key,
                "as_of_at": regime_row.as_of_at.isoformat(),
                "confidence": regime_row.confidence,
                "evidence": _loads(regime_row.evidence_json, []),
                "source": "immutable_market_regime_snapshot",
            })
        else:
            market_regime["reason"] = "缺少本交易日as-of之前有效的盘中风格快照；不借用昨日收盘标签"
    else:
        try:
            market_regime = await build_market_regime_snapshot(
                db, trade_date=analysis_trade_date, snapshot_context="postmarket",
                as_of_at=datetime.combine(analysis_trade_date, time(hour=20)), persist=False,
            )
        except ValueError:
            pass
    source_presence["market_regime"] = (market_regime.get("primary_regime") != "unknown"
        and (phase != "intraday" or market_regime.get("quality_status") == "good"))
    source_presence["promotion_prediction"] = prediction_plan.get("status") == "ready"

    prediction_review = {
        "status": "not_applicable_before_close",
        "targets": {},
        "guardrail": "outcomes are scored only in postmarket snapshots",
    }
    attributions: list[dict] = []
    if phase == "postmarket":
        prediction_regime_row = await db.scalar(
            select(MarketRegimeSnapshot)
            .where(MarketRegimeSnapshot.trade_date < analysis_trade_date)
            .order_by(desc(MarketRegimeSnapshot.trade_date), desc(MarketRegimeSnapshot.created_at))
            .limit(1)
        )
        prediction_review, attributions = await _prediction_outcome_review(
            db,
            outcome_trade_date=analysis_trade_date,
            as_of_at=as_of_at,
            snapshot_context=snapshot_context,
            market_regime=(
                prediction_regime_row.primary_regime
                if prediction_regime_row
                else market_regime.get("primary_regime")
            ),
        )
        source_presence["promotion_outcome_review"] = prediction_review.get("status") == "complete"
    else:
        source_presence["promotion_outcome_review"] = True

    quality_score = _ratio(sum(source_presence.values()), len(source_presence))
    critical_sources = {
        "premarket": ("stock_kline", "market_regime", "promotion_prediction"),
        "intraday": ("stock_kline", "intraday_spot", "promotion_prediction", "intraday_temporal_provenance"),
        "postmarket": (
            "stock_kline",
            "market_sentiment",
            "limit_up_pool",
            "market_regime",
            "promotion_outcome_review",
        ),
    }[phase]
    critical_sources_passed = all(
        source_presence.get(name, False) for name in critical_sources
    )
    quality_status = (
        "good"
        if quality_score >= 0.80 and critical_sources_passed
        else "partial"
        if quality_score >= 0.50
        else "insufficient"
    )
    warnings = [
        f"数据源 {name} 缺失或不满足当前阶段口径"
        for name, present in source_presence.items()
        if not present
    ]
    core_payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "view_schema_version": REVIEW_VIEW_SCHEMA_VERSION,
        "review_date": review_date.isoformat(),
        "analysis_trade_date": analysis_trade_date.isoformat(),  # 兼容旧API：仍为完整日线基准
        "technical_trade_date": analysis_trade_date.isoformat(),
        "market_trade_date": market_trade_date.isoformat(),
        "next_trade_date": guidance_trade_date.isoformat(),
        "phase": phase,
        "snapshot_context": snapshot_context,
        "market_regime": market_regime,
        "dimensions": dimensions,
        "prediction_plan": prediction_plan,
        "prediction_review": prediction_review,
        "source_presence": source_presence,
    }
    data_hash = _digest(core_payload)
    data_version = f"review_data_{data_hash[:16]}"
    review_key = _digest(
        {
            "review_date": review_date,
            "phase": phase,
            "schema_version": REVIEW_SCHEMA_VERSION,
            "data_version": data_version,
        }
    )
    payload = {
        **core_payload,
        "review_key": review_key,
        "data_version": data_version,
        "as_of_at": as_of_at.isoformat(timespec="seconds"),
        "quality": {
            "status": quality_status,
            "score": quality_score,
            "critical_sources": list(critical_sources),
            "critical_sources_passed": critical_sources_passed,
            "source_presence": source_presence,
            "warnings": warnings,
        },
        "action_plan": [],
        "guardrails": [
            "消息、资金、技术和基本面均保留各自来源与缺失状态",
            "自动归因只描述样本在漏斗中的位置，不宣称市场因果",
            "单日复盘不会自动修改 Champion、阈值或特征权重",
        ],
        "attributions": attributions,
        "persisted": False,
    }
    conclusion, next_session_guide = build_review_views(
        payload,
        guidance_trade_date=guidance_trade_date,
    )
    payload["review_conclusion"] = conclusion
    payload["next_session_guide"] = next_session_guide
    payload["action_plan"] = _action_plan(phase, payload)
    if not persist:
        return payload

    existing = await db.scalar(
        select(DailyReviewSnapshot).where(DailyReviewSnapshot.review_key == review_key)
    )
    if existing is None:
        stored_payload = {key: value for key, value in payload.items() if key != "attributions"}
        existing = DailyReviewSnapshot(
            review_key=review_key,
            review_date=review_date,
            analysis_trade_date=analysis_trade_date,
            phase=phase,
            as_of_at=as_of_at,
            schema_version=REVIEW_SCHEMA_VERSION,
            data_version=data_version,
            quality_status=quality_status,
            quality_score=quality_score,
            market_regime=market_regime.get("primary_regime"),
            payload_json=_json(stored_payload),
            created_at=datetime.now(),
        )
        db.add(existing)
        await db.flush()
        for attribution in attributions:
            attribution_key = _digest(
                {
                    "review_key": review_key,
                    "code": attribution["code"],
                    "target_board": attribution["target_board"],
                    "attribution_type": attribution["attribution_type"],
                }
            )
            db.add(
                PromotionReviewAttribution(
                    attribution_key=attribution_key,
                    review_snapshot_id=existing.id,
                    prediction_run_id=attribution.get("prediction_run_id"),
                    prediction_snapshot_id=attribution.get("prediction_snapshot_id"),
                    code=attribution["code"],
                    name=attribution.get("name"),
                    target_board=attribution["target_board"],
                    prediction_trade_date=attribution.get("prediction_trade_date"),
                    outcome_trade_date=attribution["outcome_trade_date"],
                    model_version=attribution.get("model_version"),
                    market_regime=attribution.get("market_regime"),
                    attribution_type=attribution["attribution_type"],
                    primary_reason=attribution["primary_reason"],
                    causal_status=attribution["causal_status"],
                    predicted_probability=attribution.get("predicted_probability"),
                    rank_position=attribution.get("rank_position"),
                    actionable=attribution.get("actionable", 0),
                    evidence_json=_json(attribution.get("evidence") or {}),
                    created_at=datetime.now(),
                )
            )
        await db.flush()
    payload["id"] = existing.id
    payload["persisted"] = True
    return payload
