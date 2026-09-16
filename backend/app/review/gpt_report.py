"""GPT automated review report generation (opt-in, idempotent)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.review import DailyReviewSnapshot, ReviewGptReport


class GptReportDisabledError(RuntimeError):
    """Raised when GPT review generation is not configured/enabled."""


def gpt_report_capabilities() -> dict:
    """Return safe UI capability metadata without exposing the configured command."""

    enabled = bool(settings.REVIEW_GPT_ENABLED and str(settings.REVIEW_GPT_CLI or "").strip())
    return {
        "enabled": enabled,
        "provider": str(settings.REVIEW_GPT_PROVIDER or ""),
        "model": str(settings.REVIEW_GPT_MODEL or ""),
        "mode": "frozen_snapshot_only",
        "requires_snapshot_id": True,
        "reason": None
        if enabled
        else "AI 深度解读未配置；确定性复盘、明日预案和预测归因不受影响。",
    }


def _load_json(value: str | None, default):
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, dict) else default


def _report_payload(row: ReviewGptReport) -> dict:
    return {
        "id": row.id,
        "report_key": row.report_key,
        "review_date": row.review_date.isoformat(),
        "snapshot_id": row.review_snapshot_id,
        "phase": row.phase,
        "provider": row.provider,
        "model": row.model,
        "status": row.status,
        "content": _load_json(row.content_json, {}),
        "summary": row.summary,
        "error_message": row.error_message,
        "created_at": (
            row.created_at.isoformat(timespec="seconds") if row.created_at else None
        ),
    }


async def generate_gpt_report(
    db: AsyncSession,
    *,
    review_date: date,
    phase: str,
    snapshot_id: int,
) -> dict:
    """Run the configured review-generator CLI and upsert one idempotent report."""

    cli = str(settings.REVIEW_GPT_CLI or "").strip()
    if not settings.REVIEW_GPT_ENABLED or not cli:
        raise GptReportDisabledError(
            "GPT 自动复盘未启用：请配置 REVIEW_GPT_ENABLED=true 与 REVIEW_GPT_CLI"
        )

    snapshot = await db.get(DailyReviewSnapshot, int(snapshot_id))
    if snapshot is None:
        raise ValueError("指定的冻结复盘快照不存在，请重新选择")
    if snapshot.review_date != review_date or snapshot.phase != phase:
        raise ValueError("review snapshot does not match review_date and phase")

    stored_snapshot = _load_json(snapshot.payload_json, {})
    if stored_snapshot.get("data_version") != snapshot.data_version:
        raise ValueError("review snapshot data_version integrity check failed")
    report_key = hashlib.sha256(
        (
            f"{review_date.isoformat()}:{phase}:{snapshot.id}:"
            f"{snapshot.data_version}"
        ).encode("utf-8")
    ).hexdigest()[:48]

    context = {
        "review_date": review_date.isoformat(),
        "phase": phase,
        "snapshot_identity": {
            "id": snapshot.id,
            "review_key": snapshot.review_key,
            "data_version": snapshot.data_version,
            "as_of_at": snapshot.as_of_at.isoformat(timespec="seconds"),
        },
        "snapshot": stored_snapshot,
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(cli),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(context, ensure_ascii=False).encode("utf-8")),
            timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError((stderr or b"").decode("utf-8", "replace")[:2000])
        parsed = json.loads(stdout.decode("utf-8", "replace"))
    except Exception as exc:
        existing = await db.scalar(
            select(ReviewGptReport).where(
                ReviewGptReport.review_date == review_date,
                ReviewGptReport.phase == phase,
            )
        )
        # A failed regeneration must not destroy the last usable report.
        if existing is None:
            db.add(
                ReviewGptReport(
                    report_key=report_key,
                    review_date=review_date,
                    review_snapshot_id=snapshot.id,
                    phase=phase,
                    provider=str(settings.REVIEW_GPT_PROVIDER),
                    model=str(settings.REVIEW_GPT_MODEL),
                    status="failed",
                    content_json="{}",
                    summary="",
                    error_message=str(exc)[:2000],
                    created_at=datetime.now(),
                )
            )
            await db.commit()
        elif existing.status != "generated":
            existing.review_snapshot_id = snapshot.id
            existing.provider = str(settings.REVIEW_GPT_PROVIDER)
            existing.model = str(settings.REVIEW_GPT_MODEL)
            existing.status = "failed"
            existing.error_message = str(exc)[:2000]
            existing.created_at = datetime.now()
            await db.commit()
        raise

    content = parsed if isinstance(parsed, dict) else {"report": parsed}
    summary = str(content.get("summary") or content.get("title") or "")[:4000]
    existing = await db.scalar(
        select(ReviewGptReport).where(
            ReviewGptReport.review_date == review_date,
            ReviewGptReport.phase == phase,
        )
    )
    if existing is not None:
        existing.report_key = report_key
        existing.review_snapshot_id = snapshot.id
        existing.provider = str(settings.REVIEW_GPT_PROVIDER)
        existing.model = str(settings.REVIEW_GPT_MODEL)
        existing.status = "generated"
        existing.content_json = json.dumps(content, ensure_ascii=False, default=str)
        existing.summary = summary
        existing.error_message = None
        existing.created_at = datetime.now()
        record = existing
    else:
        record = ReviewGptReport(
            report_key=report_key,
            review_date=review_date,
            review_snapshot_id=snapshot.id if snapshot else None,
            phase=phase,
            provider=str(settings.REVIEW_GPT_PROVIDER),
            model=str(settings.REVIEW_GPT_MODEL),
            status="generated",
            content_json=json.dumps(content, ensure_ascii=False, default=str),
            summary=summary,
            created_at=datetime.now(),
        )
        db.add(record)
    await db.commit()
    return _report_payload(record)
