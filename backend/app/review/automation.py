"""Audited review/regime automation, retry, replay, and alert persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.review import (
    DailyReviewNote,
    DailyReviewSnapshot,
    PromotionReviewAttribution,
    ReviewAutomationAlert,
    ReviewAutomationRun,
)
from app.models.stock import StockKline
from app.models.paper import PaperAutoTradeLog
from app.models.regime import MarketRegimeSnapshot
from app.promotion.regime import build_market_regime_snapshot
from app.review.service import REVIEW_PHASES, REVIEW_SCHEMA_VERSION, build_daily_review_snapshot


_TERMINAL_SUCCESS = {"completed", "completed_with_warnings"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


async def ensure_review_automation_storage(db: AsyncSession) -> None:
    """Create workbench tables for deployments whose migration has not run yet."""

    for table in (
        DailyReviewSnapshot.__table__,
        PromotionReviewAttribution.__table__,
        DailyReviewNote.__table__,
        ReviewAutomationRun.__table__,
        ReviewAutomationAlert.__table__,
    ):
        await db.run_sync(
            lambda sync_session, target=table: target.create(
                bind=sync_session.get_bind(), checkfirst=True
            )
        )


async def _append_alert(
    db: AsyncSession,
    *,
    run: ReviewAutomationRun,
    severity: str,
    alert_type: str,
    message: str,
    evidence: dict | None = None,
) -> ReviewAutomationAlert:
    alert_key = _hash({"run_id": run.id, "alert_type": alert_type, "message": message})
    existing = await db.scalar(
        select(ReviewAutomationAlert).where(
            ReviewAutomationAlert.alert_key == alert_key
        )
    )
    if existing is not None:
        return existing
    alert = ReviewAutomationAlert(
        alert_key=alert_key,
        automation_run_id=run.id,
        review_date=run.review_date,
        phase=run.phase,
        severity=severity,
        alert_type=alert_type,
        message=message,
        evidence_json=_json(evidence or {}),
        created_at=datetime.now(),
    )
    db.add(alert)
    await db.flush()
    return alert


# 「静默失效」判据：账户整日 0 成交、且其阻断原因属**数据质量类**。
# 用 stage_code / reason_code 判别，**不做原因文案匹配** —— 文案会改，码不会。
# 取值依据 app/api/v1/paper.py 数据闸门写入路径与 2026-09-16 实测分布：
#   stage_code=data_gate            -> candidate_data_missing / prediction_batch_blocked / prediction_not_visible
#   reason_code=data_quality        -> 候选扫描侧质量闸门
#   reason_code=auction_quality     -> 竞价数据质量
_DATA_QUALITY_STAGES = ("data_gate",)
_DATA_QUALITY_REASONS = ("data_quality", "auction_quality", "candidate_data_missing")


async def alert_silent_data_quality_blockage(
    db: AsyncSession,
    *,
    run: ReviewAutomationRun,
    review_date: date,
) -> list[ReviewAutomationAlert]:
    """账户当日 0 成交、且被数据质量闸门挡住时告警。

    背景（2026-09-16 复盘）：13 个模拟账户中 9 个当日 0 成交，原因全部是数据质量类
    阻断，但系统**没有任何告警** —— 只留下一行行决策日志，事后靠人工翻 43,956 行
    日志才发现。本函数把这个模式变成一条可检索的告警。

    只在 postmarket 阶段调用：盘中 0 成交是正常状态（买入窗口未开或尚未成交），
    盘中拿它告警会持续误报。
    """
    executed = func.sum(
        case(
            (
                and_(
                    PaperAutoTradeLog.action == "buy",
                    PaperAutoTradeLog.decision == "executed",
                ),
                1,
            ),
            else_=0,
        )
    ).label("executed")
    blocked = func.sum(
        case(
            (
                or_(
                    PaperAutoTradeLog.stage_code.in_(_DATA_QUALITY_STAGES),
                    PaperAutoTradeLog.reason_code.in_(_DATA_QUALITY_REASONS),
                ),
                1,
            ),
            else_=0,
        )
    ).label("blocked")

    rows = (
        await db.execute(
            select(
                PaperAutoTradeLog.account_id,
                executed,
                blocked,
                func.count().label("total"),
            )
            .where(PaperAutoTradeLog.trade_date == review_date)
            .group_by(PaperAutoTradeLog.account_id)
        )
    ).all()

    alerts: list[ReviewAutomationAlert] = []
    for account_id, executed_count, blocked_count, total in rows:
        if not total or executed_count or not blocked_count:
            continue
        top = (
            await db.execute(
                select(
                    PaperAutoTradeLog.stage_code,
                    PaperAutoTradeLog.reason_code,
                    func.count().label("n"),
                )
                .where(
                    PaperAutoTradeLog.trade_date == review_date,
                    PaperAutoTradeLog.account_id == account_id,
                    or_(
                        PaperAutoTradeLog.stage_code.in_(_DATA_QUALITY_STAGES),
                        PaperAutoTradeLog.reason_code.in_(_DATA_QUALITY_REASONS),
                    ),
                )
                .group_by(PaperAutoTradeLog.stage_code, PaperAutoTradeLog.reason_code)
                .order_by(desc("n"))
                .limit(3)
            )
        ).all()
        alerts.append(
            await _append_alert(
                db,
                run=run,
                severity="warning",
                alert_type="account_silent_data_quality_blockage",
                message=(
                    f"{review_date} 账户 {account_id} 全天 0 成交，"
                    f"其中 {int(blocked_count)} 条为数据质量类阻断"
                ),
                evidence={
                    "account_id": account_id,
                    "trade_date": str(review_date),
                    "executed_buys": 0,
                    "data_quality_blocks": int(blocked_count),
                    "total_logs": int(total),
                    "top_blocking_reasons": [
                        {"stage_code": s, "reason_code": r, "count": int(n)}
                        for s, r, n in top
                    ],
                },
            )
        )
    return alerts


async def recover_stale_automation_runs(
    db: AsyncSession,
    *,
    stale_after_minutes: int = 30,
) -> int:
    cutoff = datetime.now() - timedelta(minutes=max(int(stale_after_minutes), 1))
    stale_rows = list(
        (
            await db.scalars(
                select(ReviewAutomationRun).where(
                    ReviewAutomationRun.status == "running",
                    ReviewAutomationRun.started_at < cutoff,
                )
            )
        ).all()
    )
    for row in stale_rows:
        row.status = "failed_stale"
        row.error_message = "automation process ended without a terminal status"
        row.completed_at = datetime.now()
        await _append_alert(
            db,
            run=row,
            severity="error",
            alert_type="stale_run_recovered",
            message=f"{row.job_name} 检测到超时运行并已释放，可安全重试",
            evidence={"started_at": row.started_at, "cutoff": cutoff},
        )
    if stale_rows:
        await db.commit()
    return len(stale_rows)


async def _existing_success(
    db: AsyncSession,
    logical_key: str,
) -> ReviewAutomationRun | None:
    return await db.scalar(
        select(ReviewAutomationRun)
        .where(
            ReviewAutomationRun.logical_key == logical_key,
            ReviewAutomationRun.status.in_(_TERMINAL_SUCCESS),
        )
        .order_by(desc(ReviewAutomationRun.attempt))
        .limit(1)
    )


async def _next_attempt(db: AsyncSession, logical_key: str) -> int:
    maximum = await db.scalar(
        select(func.max(ReviewAutomationRun.attempt)).where(
            ReviewAutomationRun.logical_key == logical_key
        )
    )
    return int(maximum or 0) + 1


async def run_review_automation(
    db: AsyncSession,
    *,
    review_date: date,
    phase: str,
    trigger: str = "schedule",
    snapshot_context: str = "",
    retry_attempts: int | None = None,
    retry_delay_seconds: int | None = None,
    force: bool = False,
) -> dict:
    phase = str(phase).strip().lower()
    if phase not in REVIEW_PHASES:
        raise ValueError(f"phase must be one of {REVIEW_PHASES}")
    await ensure_review_automation_storage(db)
    await recover_stale_automation_runs(db)
    logical_key = _hash(
        {
            "job_name": "daily_review_snapshot",
            "schema_version": REVIEW_SCHEMA_VERSION,
            "review_date": review_date,
            "phase": phase,
            "snapshot_context": snapshot_context,
        }
    )
    if not force and (existing := await _existing_success(db, logical_key)) is not None:
        return {
            "status": "already_completed",
            "automation_run_id": existing.id,
            "review_snapshot_id": existing.review_snapshot_id,
            "quality_status": existing.quality_status,
        }

    retries = max(
        int(retry_attempts or settings.REVIEW_AUTOMATION_RETRY_ATTEMPTS), 1
    )
    delay = max(
        int(
            retry_delay_seconds
            if retry_delay_seconds is not None
            else settings.REVIEW_AUTOMATION_RETRY_DELAY_SECONDS
        ),
        0,
    )
    last_error = ""
    for retry_index in range(retries):
        attempt = await _next_attempt(db, logical_key)
        run = ReviewAutomationRun(
            run_key=_hash({"logical_key": logical_key, "attempt": attempt}),
            logical_key=logical_key,
            job_name="daily_review_snapshot",
            review_date=review_date,
            phase=phase,
            trigger=trigger,
            attempt=attempt,
            status="running",
            started_at=datetime.now(),
        )
        db.add(run)
        await db.commit()
        run_id = int(run.id)
        try:
            payload = await build_daily_review_snapshot(
                db,
                review_date=review_date,
                phase=phase,
                snapshot_context=snapshot_context,
                persist=True,
            )
            await db.commit()
            persisted_run = await db.get(ReviewAutomationRun, run_id)
            assert persisted_run is not None
            quality = payload.get("quality") or {}
            quality_status = str(quality.get("status") or "insufficient")
            persisted_run.status = (
                "completed" if quality_status == "good" else "completed_with_warnings"
            )
            persisted_run.review_snapshot_id = payload.get("id")
            persisted_run.quality_status = quality_status
            persisted_run.details_json = _json(
                {
                    "review_key": payload.get("review_key"),
                    "data_version": payload.get("data_version"),
                    "analysis_trade_date": payload.get("analysis_trade_date"),
                    "quality": quality,
                    "prediction_review_status": (
                        payload.get("prediction_review") or {}
                    ).get("status"),
                }
            )
            persisted_run.completed_at = datetime.now()
            if quality_status != "good":
                await _append_alert(
                    db,
                    run=persisted_run,
                    severity="warning",
                    alert_type="review_quality_degraded",
                    message=f"{review_date} {phase} 复盘快照质量为 {quality_status}",
                    evidence={
                        "warnings": quality.get("warnings") or [],
                        "critical_sources": quality.get("critical_sources") or [],
                    },
                )
            await db.commit()
            # 静默失效告警：仅在 postmarket（全天已结束）评估，
            # 避免盘中"尚未到买入窗口"被误报成数据质量阻断。
            if phase == "postmarket":
                await alert_silent_data_quality_blockage(
                    db, run=persisted_run, review_date=review_date
                )
                await db.commit()
            return {
                "status": persisted_run.status,
                "automation_run_id": run_id,
                "review_snapshot_id": payload.get("id"),
                "quality_status": quality_status,
                "attempt": attempt,
            }
        except Exception as exc:
            last_error = str(exc)
            await db.rollback()
            persisted_run = await db.get(ReviewAutomationRun, run_id)
            if persisted_run is not None:
                persisted_run.status = "failed"
                persisted_run.error_message = last_error[:4000]
                persisted_run.completed_at = datetime.now()
                await _append_alert(
                    db,
                    run=persisted_run,
                    severity="error",
                    alert_type="review_job_failed",
                    message=f"{review_date} {phase} 复盘自动化失败（第 {attempt} 次）",
                    evidence={"error": last_error[:1000]},
                )
                await db.commit()
            if retry_index + 1 < retries and delay:
                await asyncio.sleep(delay)
    return {"status": "failed", "error": last_error, "attempts": retries}


async def run_regime_automation(
    db: AsyncSession,
    *,
    trigger: str = "schedule",
    retry_attempts: int | None = None,
    retry_delay_seconds: int | None = None,
    force: bool = False,
) -> dict:
    await ensure_review_automation_storage(db)
    await recover_stale_automation_runs(db)
    trade_date = await db.scalar(
        select(StockKline.trade_date).order_by(desc(StockKline.trade_date)).limit(1)
    )
    if trade_date is None:
        raise ValueError("no StockKline date is available for regime automation")
    logical_key = _hash({"job_name": "market_regime_snapshot", "trade_date": trade_date})
    if not force and (existing := await _existing_success(db, logical_key)) is not None:
        return {
            "status": "already_completed",
            "automation_run_id": existing.id,
            "regime_snapshot_id": existing.regime_snapshot_id,
            "quality_status": existing.quality_status,
        }

    retries = max(
        int(retry_attempts or settings.REVIEW_AUTOMATION_RETRY_ATTEMPTS), 1
    )
    delay = max(
        int(
            retry_delay_seconds
            if retry_delay_seconds is not None
            else settings.REVIEW_AUTOMATION_RETRY_DELAY_SECONDS
        ),
        0,
    )
    last_error = ""
    for retry_index in range(retries):
        attempt = await _next_attempt(db, logical_key)
        run = ReviewAutomationRun(
            run_key=_hash({"logical_key": logical_key, "attempt": attempt}),
            logical_key=logical_key,
            job_name="market_regime_snapshot",
            review_date=trade_date,
            phase="postmarket",
            trigger=trigger,
            attempt=attempt,
            status="running",
            started_at=datetime.now(),
        )
        db.add(run)
        await db.commit()
        run_id = int(run.id)
        try:
            payload = await build_market_regime_snapshot(
                db, trade_date=trade_date, persist=True
            )
            await db.commit()
            persisted_run = await db.get(ReviewAutomationRun, run_id)
            assert persisted_run is not None
            quality_status = str(payload.get("quality_status") or "partial")
            persisted_run.status = (
                "completed" if quality_status == "good" else "completed_with_warnings"
            )
            persisted_run.regime_snapshot_id = payload.get("id")
            persisted_run.quality_status = quality_status
            persisted_run.details_json = _json(
                {
                    "snapshot_key": payload.get("snapshot_key"),
                    "data_version": payload.get("data_version"),
                    "primary_regime": payload.get("primary_regime"),
                    "confidence": payload.get("confidence"),
                    "source_presence": payload.get("source_presence"),
                }
            )
            persisted_run.completed_at = datetime.now()
            if quality_status != "good":
                await _append_alert(
                    db,
                    run=persisted_run,
                    severity="warning",
                    alert_type="regime_quality_degraded",
                    message=f"{trade_date} 市场风格快照数据质量为 {quality_status}",
                    evidence={"source_presence": payload.get("source_presence")},
                )
            await db.commit()
            return {
                "status": persisted_run.status,
                "automation_run_id": run_id,
                "regime_snapshot_id": payload.get("id"),
                "quality_status": quality_status,
                "attempt": attempt,
            }
        except Exception as exc:
            last_error = str(exc)
            await db.rollback()
            persisted_run = await db.get(ReviewAutomationRun, run_id)
            if persisted_run is not None:
                persisted_run.status = "failed"
                persisted_run.error_message = last_error[:4000]
                persisted_run.completed_at = datetime.now()
                await _append_alert(
                    db,
                    run=persisted_run,
                    severity="error",
                    alert_type="regime_job_failed",
                    message=f"{trade_date} 市场风格自动化失败（第 {attempt} 次）",
                    evidence={"error": last_error[:1000]},
                )
                await db.commit()
            if retry_index + 1 < retries and delay:
                await asyncio.sleep(delay)
    return {"status": "failed", "error": last_error, "attempts": retries}


async def replay_daily_reviews(
    db: AsyncSession,
    *,
    start_date: date,
    end_date: date,
    phases: Iterable[str] = REVIEW_PHASES,
    force: bool = False,
    persist: bool = True,
) -> dict:
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    normalized_phases = tuple(dict.fromkeys(str(item).strip().lower() for item in phases))
    if not normalized_phases or any(item not in REVIEW_PHASES for item in normalized_phases):
        raise ValueError(f"phases must be a non-empty subset of {REVIEW_PHASES}")
    trade_dates = [
        item
        for item in (
            await db.scalars(
                select(StockKline.trade_date)
                .where(
                    StockKline.trade_date >= start_date,
                    StockKline.trade_date <= end_date,
                )
                .distinct()
                .order_by(StockKline.trade_date)
            )
        ).all()
        if item is not None
    ]
    results: list[dict] = []
    for trade_day in trade_dates:
        for phase in normalized_phases:
            if persist:
                result = await run_review_automation(
                    db,
                    review_date=trade_day,
                    phase=phase,
                    trigger="replay",
                    retry_attempts=1,
                    retry_delay_seconds=0,
                    force=force,
                )
            else:
                try:
                    payload = await build_daily_review_snapshot(
                        db,
                        review_date=trade_day,
                        phase=phase,
                        persist=False,
                    )
                    result = {
                        "status": "dry_run",
                        "quality_status": (payload.get("quality") or {}).get("status"),
                        "data_version": payload.get("data_version"),
                    }
                except ValueError as exc:
                    result = {"status": "skipped", "error": str(exc)}
            results.append(
                {"review_date": trade_day.isoformat(), "phase": phase, **result}
            )
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "phases": list(normalized_phases),
        "trade_day_count": len(trade_dates),
        "execution_count": len(results),
        "persisted": persist,
        "results": results,
    }
