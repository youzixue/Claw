"""Premarket, intraday, and postmarket review workbench API."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.review import (
    DailyReviewNote,
    DailyReviewSnapshot,
    PromotionReviewAttribution,
    ReviewAutomationAlert,
    ReviewAutomationRun,
    ReviewGptReport,
)
from app.review.automation import replay_daily_reviews
from app.review.service import (
    REVIEW_PHASES,
    build_daily_review_snapshot,
    build_review_views,
)


router = APIRouter()


class ReviewBuildRequest(BaseModel):
    review_date: date | None = None
    phase: Literal["premarket", "intraday", "postmarket"] = "postmarket"
    as_of_at: datetime | None = None
    snapshot_context: str = Field(default="", max_length=40)
    persist: bool = True


class ReviewReplayRequest(BaseModel):
    start_date: date
    end_date: date
    phases: list[Literal["premarket", "intraday", "postmarket"]] = Field(
        default_factory=lambda: ["premarket", "intraday", "postmarket"]
    )
    force: bool = False
    persist: bool = False


class ReviewNoteRequest(BaseModel):
    review_date: date
    phase: Literal["premarket", "intraday", "postmarket"]
    category: Literal["observation", "hypothesis", "decision", "follow_up"] = "observation"
    content: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content must not be blank")
        return cleaned

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip()[:40] for value in values if value.strip()))


def _loads(value: str | None, default):
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


_AUTOMATION_META = {
    "daily_review_snapshot": {
        "label": "阶段复盘快照",
        "purpose": "按盘前、盘中、盘后冻结当时可见的数据与结论，供历史核对和模型归因。",
    },
    "market_regime_snapshot": {
        "label": "市场风格快照",
        "purpose": "冻结可解释的市场风格标签，只用于分层观察，不自动修改模型参数。",
    },
}

_ALERT_TYPE_LABELS = {
    "review_quality_degraded": "复盘数据质量降级",
    "review_job_failed": "复盘任务失败",
    "regime_quality_degraded": "市场风格数据降级",
    "regime_job_failed": "市场风格任务失败",
    "stale_run_recovered": "超时任务已释放",
}


def _snapshot_summary(row: DailyReviewSnapshot) -> dict:
    stored = _loads(row.payload_json, {})
    regime = stored.get("market_regime")
    conclusion = stored.get("review_conclusion") or {}
    if isinstance(regime, dict):
        regime_name = regime.get("primary_regime_name")
    else:
        regime_name = conclusion.get("regime_name")
    return {
        "id": row.id,
        "review_key": row.review_key,
        "review_date": row.review_date.isoformat(),
        "analysis_trade_date": row.analysis_trade_date.isoformat(),
        "technical_trade_date": stored.get("technical_trade_date", row.analysis_trade_date.isoformat()),
        "market_trade_date": stored.get("market_trade_date"),  # 旧混日快照不猜测、不改写
        "next_trade_date": stored.get("next_trade_date"),
        "phase": row.phase,
        "as_of_at": row.as_of_at.isoformat(timespec="seconds"),
        "schema_version": row.schema_version,
        "data_version": row.data_version,
        "quality_status": row.quality_status,
        "quality_score": row.quality_score,
        "market_regime": row.market_regime,
        "market_regime_name": regime_name,
        "decision_headline": conclusion.get("headline"),
        "created_at": row.created_at.isoformat(timespec="seconds"),
    }


def _attribution_payload(row: PromotionReviewAttribution) -> dict:
    return {
        "id": row.id,
        "review_snapshot_id": row.review_snapshot_id,
        "prediction_run_id": row.prediction_run_id,
        "prediction_snapshot_id": row.prediction_snapshot_id,
        "code": row.code,
        "name": row.name,
        "target_board": row.target_board,
        "prediction_trade_date": row.prediction_trade_date.isoformat()
        if row.prediction_trade_date
        else None,
        "outcome_trade_date": row.outcome_trade_date.isoformat(),
        "model_version": row.model_version,
        "market_regime": row.market_regime,
        "attribution_type": row.attribution_type,
        "primary_reason": row.primary_reason,
        "causal_status": row.causal_status,
        "predicted_probability": row.predicted_probability,
        "rank_position": row.rank_position,
        "actionable": bool(row.actionable),
        "evidence": _loads(row.evidence_json, {}),
        "created_at": row.created_at.isoformat(timespec="seconds"),
    }


async def _snapshot_detail(
    db: AsyncSession,
    row: DailyReviewSnapshot,
    *,
    include_attributions: bool,
) -> dict:
    payload = _loads(row.payload_json, {})
    summary = _snapshot_summary(row)
    # 列表摘要中的 market_regime 是数据库索引用的代码；详情 payload 中保留
    # 完整风格对象（中文名、置信度、次风格和证据），避免打开历史快照后退化成裸字符串。
    if isinstance(payload.get("market_regime"), dict):
        summary["market_regime_code"] = summary.pop("market_regime")
    payload.update(summary)
    payload["persisted"] = True
    if not payload.get("review_conclusion") or not payload.get("next_session_guide"):
        conclusion, guide = build_review_views(
            payload,
            guidance_trade_date=payload.get("next_trade_date"),
        )
        payload.setdefault("review_conclusion", conclusion)
        payload.setdefault("next_session_guide", guide)
    if include_attributions:
        attributions = list(
            (
                await db.scalars(
                    select(PromotionReviewAttribution)
                    .where(PromotionReviewAttribution.review_snapshot_id == row.id)
                    .order_by(
                        PromotionReviewAttribution.target_board,
                        PromotionReviewAttribution.attribution_type,
                        PromotionReviewAttribution.rank_position.is_(None),
                        PromotionReviewAttribution.rank_position,
                    )
                )
            ).all()
        )
        payload["attributions"] = [
            _attribution_payload(item) for item in attributions
        ]
    return payload


@router.post("/snapshots/build")
async def build_review(
    request: ReviewBuildRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await build_daily_review_snapshot(
            db,
            review_date=request.review_date,
            phase=request.phase,
            as_of_at=request.as_of_at,
            snapshot_context=request.snapshot_context,
            persist=request.persist,
        )
        if request.persist:
            await db.commit()
        return result
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/snapshots")
async def review_snapshots(
    review_date: date | None = None,
    phase: str = "",
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    statement = select(DailyReviewSnapshot)
    if review_date is not None:
        statement = statement.where(DailyReviewSnapshot.review_date == review_date)
    if phase.strip():
        if phase.strip() not in REVIEW_PHASES:
            raise HTTPException(status_code=422, detail="invalid review phase")
        statement = statement.where(DailyReviewSnapshot.phase == phase.strip())
    rows = list(
        (
            await db.scalars(
                statement.order_by(
                    desc(DailyReviewSnapshot.review_date),
                    desc(DailyReviewSnapshot.as_of_at),
                    desc(DailyReviewSnapshot.created_at),
                ).limit(max(1, min(int(limit), 500)))
            )
        ).all()
    )
    return {"count": len(rows), "snapshots": [_snapshot_summary(row) for row in rows]}


@router.get("/latest")
async def latest_review(
    phase: str = "postmarket",
    include_attributions: bool = True,
    db: AsyncSession = Depends(get_db),
):
    if phase not in REVIEW_PHASES:
        raise HTTPException(status_code=422, detail="invalid review phase")
    row = await db.scalar(
        select(DailyReviewSnapshot)
        .where(DailyReviewSnapshot.phase == phase)
        .order_by(
            desc(DailyReviewSnapshot.review_date),
            desc(DailyReviewSnapshot.as_of_at),
            desc(DailyReviewSnapshot.created_at),
        )
        .limit(1)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no persisted review snapshot")
    return await _snapshot_detail(db, row, include_attributions=include_attributions)


@router.get("/snapshots/{snapshot_id}")
async def review_snapshot_detail(
    snapshot_id: int,
    include_attributions: bool = True,
    db: AsyncSession = Depends(get_db),
):
    row = await db.get(DailyReviewSnapshot, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="review snapshot not found")
    return await _snapshot_detail(db, row, include_attributions=include_attributions)


@router.get("/attributions")
async def review_attributions(
    outcome_trade_date: date | None = None,
    target_board: int | None = None,
    attribution_type: str = "",
    market_regime: str = "",
    limit: int = 300,
    db: AsyncSession = Depends(get_db),
):
    statement = select(PromotionReviewAttribution)
    if outcome_trade_date is not None:
        statement = statement.where(
            PromotionReviewAttribution.outcome_trade_date == outcome_trade_date
        )
    if target_board in {1, 2}:
        statement = statement.where(
            PromotionReviewAttribution.target_board == target_board
        )
    if attribution_type.strip():
        statement = statement.where(
            PromotionReviewAttribution.attribution_type == attribution_type.strip()
        )
    if market_regime.strip():
        statement = statement.where(
            PromotionReviewAttribution.market_regime == market_regime.strip()
        )
    rows = list(
        (
            await db.scalars(
                statement.order_by(
                    desc(PromotionReviewAttribution.outcome_trade_date),
                    PromotionReviewAttribution.target_board,
                    PromotionReviewAttribution.rank_position.is_(None),
                    PromotionReviewAttribution.rank_position,
                ).limit(max(1, min(int(limit), 1000)))
            )
        ).all()
    )
    return {"count": len(rows), "attributions": [_attribution_payload(row) for row in rows]}


@router.get("/automation-runs")
async def automation_runs(
    review_date: date | None = None,
    job_name: str = "",
    status: str = "",
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    statement = select(ReviewAutomationRun)
    if review_date is not None:
        statement = statement.where(ReviewAutomationRun.review_date == review_date)
    if job_name.strip():
        statement = statement.where(ReviewAutomationRun.job_name == job_name.strip())
    if status.strip():
        statement = statement.where(ReviewAutomationRun.status == status.strip())
    rows = list(
        (
            await db.scalars(
                statement.order_by(desc(ReviewAutomationRun.started_at)).limit(
                    max(1, min(int(limit), 500))
                )
            )
        ).all()
    )
    return {
        "count": len(rows),
        "runs": [
            {
                "id": row.id,
                "job_name": row.job_name,
                "job_label": (_AUTOMATION_META.get(row.job_name) or {}).get("label", row.job_name),
                "purpose": (_AUTOMATION_META.get(row.job_name) or {}).get("purpose", "记录任务执行状态与可审计结果。"),
                "review_date": row.review_date.isoformat(),
                "phase": row.phase,
                "trigger": row.trigger,
                "attempt": row.attempt,
                "status": row.status,
                "review_snapshot_id": row.review_snapshot_id,
                "regime_snapshot_id": row.regime_snapshot_id,
                "quality_status": row.quality_status,
                "details": _loads(row.details_json, {}),
                "error_message": row.error_message,
                "started_at": row.started_at.isoformat(timespec="seconds"),
                "completed_at": row.completed_at.isoformat(timespec="seconds")
                if row.completed_at
                else None,
            }
            for row in rows
        ],
    }


@router.get("/alerts")
async def automation_alerts(
    review_date: date | None = None,
    severity: str = "",
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    statement = select(ReviewAutomationAlert)
    if review_date is not None:
        statement = statement.where(ReviewAutomationAlert.review_date == review_date)
    if severity.strip():
        statement = statement.where(ReviewAutomationAlert.severity == severity.strip())
    rows = list(
        (
            await db.scalars(
                statement.order_by(desc(ReviewAutomationAlert.created_at)).limit(
                    max(1, min(int(limit), 500))
                )
            )
        ).all()
    )
    return {
        "count": len(rows),
        "alerts": [
            {
                "id": row.id,
                "automation_run_id": row.automation_run_id,
                "review_date": row.review_date.isoformat(),
                "phase": row.phase,
                "severity": row.severity,
                "alert_type": row.alert_type,
                "alert_type_label": _ALERT_TYPE_LABELS.get(row.alert_type, row.alert_type),
                "message": row.message,
                "evidence": _loads(row.evidence_json, {}),
                "created_at": row.created_at.isoformat(timespec="seconds"),
            }
            for row in rows
        ],
    }


@router.post("/replay")
async def replay_reviews(
    request: ReviewReplayRequest,
    db: AsyncSession = Depends(get_db),
):
    if request.start_date > request.end_date:
        raise HTTPException(status_code=422, detail="start_date must not be after end_date")
    if (request.end_date - request.start_date).days > 120:
        raise HTTPException(status_code=422, detail="API replay range is limited to 120 calendar days")
    if not request.phases:
        raise HTTPException(status_code=422, detail="at least one phase is required")
    try:
        return await replay_daily_reviews(
            db,
            start_date=request.start_date,
            end_date=request.end_date,
            phases=request.phases,
            force=request.force,
            persist=request.persist,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/notes")
async def create_review_note(
    request: ReviewNoteRequest,
    db: AsyncSession = Depends(get_db),
):
    note = DailyReviewNote(
        review_date=request.review_date,
        phase=request.phase,
        category=request.category,
        content=request.content,
        tags_json=json.dumps(request.tags, ensure_ascii=False),
        author="user",
        created_at=datetime.now(),
    )
    db.add(note)
    await db.commit()
    return {
        "id": note.id,
        "review_date": note.review_date.isoformat(),
        "phase": note.phase,
        "category": note.category,
        "content": note.content,
        "tags": request.tags,
        "author": note.author,
        "created_at": note.created_at.isoformat(timespec="seconds"),
    }


@router.get("/notes")
async def review_notes(
    review_date: date | None = None,
    phase: str = "",
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
):
    statement = select(DailyReviewNote)
    if review_date is not None:
        statement = statement.where(DailyReviewNote.review_date == review_date)
    if phase.strip():
        statement = statement.where(DailyReviewNote.phase == phase.strip())
    rows = list(
        (
            await db.scalars(
                statement.order_by(desc(DailyReviewNote.created_at)).limit(
                    max(1, min(int(limit), 500))
                )
            )
        ).all()
    )
    return {
        "count": len(rows),
        "notes": [
            {
                "id": row.id,
                "review_date": row.review_date.isoformat(),
                "phase": row.phase,
                "category": row.category,
                "content": row.content,
                "tags": _loads(row.tags_json, []),
                "author": row.author,
                "created_at": row.created_at.isoformat(timespec="seconds"),
            }
            for row in rows
        ],
    }


class ReviewGptGenerateRequest(BaseModel):
    review_date: date
    phase: Literal["premarket", "intraday", "postmarket"] = "postmarket"
    snapshot_id: int = Field(gt=0)


def _gpt_report_payload(row: ReviewGptReport) -> dict:
    return {
        "id": row.id,
        "report_key": row.report_key,
        "review_date": row.review_date.isoformat(),
        "snapshot_id": row.review_snapshot_id,
        "phase": row.phase,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "content": _loads(row.content_json, {}),
        "summary": row.summary,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
    }


@router.get("/gpt-reports/capabilities")
async def review_gpt_capabilities():
    """Expose whether optional AI interpretation is usable before the UI enables it."""

    from app.review.gpt_report import gpt_report_capabilities

    return gpt_report_capabilities()


@router.get("/gpt-reports")
async def review_gpt_reports(
    review_date: date | None = None,
    phase: str = "",
    snapshot_id: int | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    statement = select(ReviewGptReport)
    if review_date is not None:
        statement = statement.where(ReviewGptReport.review_date == review_date)
    if phase.strip():
        statement = statement.where(ReviewGptReport.phase == phase.strip())
    if snapshot_id is not None:
        statement = statement.where(ReviewGptReport.review_snapshot_id == snapshot_id)
    rows = list(
        (
            await db.scalars(
                statement.order_by(desc(ReviewGptReport.created_at)).limit(
                    max(1, min(int(limit), 200))
                )
            )
        ).all()
    )
    return {"count": len(rows), "reports": [_gpt_report_payload(row) for row in rows]}


@router.post("/gpt-reports/generate")
async def review_gpt_generate(
    request: ReviewGptGenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    """调用配置的 GPT 复盘命令生成并幂等保存报告；未配置时 fail closed。

    该接口与确定性复盘快照解耦：失败不阻塞快照，也绝不修改任何模型权重。
    """
    from app.review.gpt_report import GptReportDisabledError, generate_gpt_report

    try:
        return await generate_gpt_report(
            db,
            review_date=request.review_date,
            phase=request.phase,
            snapshot_id=request.snapshot_id,
        )
    except GptReportDisabledError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)[:400]) from exc
