"""数据治理 API"""

import json
from datetime import date, datetime
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.db.session import get_db
from app.core.trade_calendar import trade_calendar
from app.core.data_quality import data_quality_guard, SourceHealth
from app.core.data_backfill import backfill_manager
from app.core.stock_tagger import stock_tagger, TAG_LABEL_MAP
from app.models.governance import DataQualityRun
from app.models.stock import StockKline, StockSpot

router = APIRouter()


def _resolve_spot_trade_date(latest_updated_at: datetime | date | None) -> date | None:
    if latest_updated_at is None:
        return None
    if isinstance(latest_updated_at, datetime):
        return latest_updated_at.date()
    if isinstance(latest_updated_at, date):
        return latest_updated_at
    return None


async def _build_spot_kline_alignment_check(db: AsyncSession) -> dict:
    spot_meta = await db.execute(
        select(func.max(StockSpot.updated_at), func.count()).select_from(StockSpot)
    )
    latest_spot_updated_at, spot_count = spot_meta.one()
    latest_spot_trade_date = _resolve_spot_trade_date(latest_spot_updated_at)

    latest_kline_trade_date = await db.scalar(
        select(func.max(StockKline.trade_date)).select_from(StockKline)
    )

    message = ""
    status = "up"
    completeness = 1.0
    date_gap_days = None

    if not spot_count:
        status = "down"
        completeness = 0.0
        message = "stock_spot 无数据"
    elif latest_kline_trade_date is None:
        status = "down"
        completeness = 0.0
        message = (
            f"spot 最新交易日 {latest_spot_trade_date or '--'}，但 stock_kline 无数据"
        )
    elif latest_spot_trade_date is None:
        status = "down"
        completeness = 0.0
        message = (
            f"spot 最新更新时间非法: {latest_spot_updated_at or '--'}"
        )
    else:
        date_gap_days = (latest_spot_trade_date - latest_kline_trade_date).days
        if date_gap_days != 0:
            status = "down"
            completeness = 0.0
            if date_gap_days > 0:
                message = (
                    f"spot 最新交易日 {latest_spot_trade_date} 晚于 kline {latest_kline_trade_date}，"
                    f"相差 {date_gap_days} 天"
                )
            else:
                message = (
                    f"spot 最新交易日 {latest_spot_trade_date} 早于 kline {latest_kline_trade_date}，"
                    f"相差 {abs(date_gap_days)} 天"
                )
        else:
            message = (
                f"spot 与 kline 已对齐: {latest_spot_trade_date}"
            )

    return {
        "source": "spot_kline_alignment",
        "api_name": "Spot/KLine 日期对齐检查",
        "status": status,
        "latency_ms": None,
        "completeness": completeness,
        "message": message,
        "latest_spot_updated_at": latest_spot_updated_at.isoformat() if isinstance(latest_spot_updated_at, datetime) else None,
        "latest_spot_trade_date": latest_spot_trade_date.isoformat() if latest_spot_trade_date else None,
        "latest_kline_trade_date": latest_kline_trade_date.isoformat() if latest_kline_trade_date else None,
        "date_gap_days": date_gap_days,
    }


# ===== 交易日历 =====

class TradeDayResponse(BaseModel):
    date: date
    is_trade_day: bool
    session: str

@router.get("/calendar/today")
async def calendar_today():
    """今天是否交易日 + 当前时段"""
    return {
        "date": date.today().isoformat(),
        "is_trade_day": await trade_calendar.is_trade_day(),
        "session": trade_calendar.get_trade_session(),
        "is_trading_hours": await trade_calendar.is_trading_hours(),
    }

@router.get("/calendar/next-trade-day")
async def next_trade_day():
    """下一个交易日"""
    nd = await trade_calendar.next_trade_day()
    return {"next_trade_day": nd.isoformat()}


# ===== 数据质量 =====

@router.get("/health")
async def data_source_health(db: AsyncSession = Depends(get_db)):
    """数据源健康状态"""
    health_list = await data_quality_guard.get_all_health(db)
    alignment_check = await _build_spot_kline_alignment_check(db)
    from app.data.scheduler import data_scheduler

    return {
        "pipeline": data_scheduler.get_pipeline_runtime_status(),
        "sources": [alignment_check] + [
            {
                "source": h.source,
                "api_name": h.api_name,
                "status": h.status,
                "latency_ms": h.latency_ms,
                "completeness": h.completeness,
                "message": h.error_msg,
            }
            for h in health_list
        ]
    }


@router.get("/prediction-quality")
async def prediction_quality(
    refresh: bool = False,
    snapshot_context: str = "",
    lookback_days: int = 120,
    db: AsyncSession = Depends(get_db),
):
    """Return the latest prediction-data audit, optionally refreshing it."""

    if refresh:
        return await data_quality_guard.audit_prediction_data(
            db,
            snapshot_context=snapshot_context,
            persist=True,
            lookback_days=max(20, min(int(lookback_days), 365)),
        )
    latest = await db.scalar(
        select(DataQualityRun).order_by(DataQualityRun.started_at.desc()).limit(1)
    )
    if latest:
        try:
            payload = json.loads(latest.summary_json or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        payload.update(
            {
                "run_id": latest.id,
                "status": latest.status,
                "gate_passed": bool(latest.gate_passed),
                "issue_count": int(latest.issue_count or 0),
                "blocking_count": int(latest.blocking_count or 0),
            }
        )
        return payload
    return await data_quality_guard.audit_prediction_data(
        db,
        snapshot_context=snapshot_context,
        persist=False,
        lookback_days=max(20, min(int(lookback_days), 365)),
    )


@router.post("/prediction-quality/run")
async def run_prediction_quality(
    snapshot_context: str = "manual",
    lookback_days: int = 120,
    db: AsyncSession = Depends(get_db),
):
    """Persist a manual prediction-data audit run."""

    return await data_quality_guard.audit_prediction_data(
        db,
        snapshot_context=snapshot_context,
        persist=True,
        lookback_days=max(20, min(int(lookback_days), 365)),
    )


# ===== 回填管理 =====

class BackfillRequest(BaseModel):
    data_type: str       # quote/fund_flow/sector/news
    start_date: date
    end_date: date

@router.post("/backfill")
async def create_backfill(req: BackfillRequest, db: AsyncSession = Depends(get_db)):
    """创建回填任务"""
    task = await backfill_manager.create_task(db, req.data_type, req.start_date, req.end_date)
    return {"task_id": task.id, "status": task.status}

@router.get("/backfill/tasks")
async def backfill_tasks(db: AsyncSession = Depends(get_db)):
    """获取回填任务列表"""
    pending = await backfill_manager.get_pending_tasks(db)
    running = await backfill_manager.get_running_tasks(db)
    return {
        "pending": len(pending),
        "running": len(running),
        "tasks": [
            {
                "id": t.id,
                "data_type": t.data_type,
                "start_date": t.start_date.isoformat(),
                "end_date": t.end_date.isoformat(),
                "status": t.status,
                "progress": t.progress,
            }
            for t in pending + running
        ]
    }


# ===== 股票标记 =====

@router.get("/stock-tags/{code}")
async def get_stock_tag(code: str):
    """获取股票标记"""
    board_type = stock_tagger.get_board_type(code)
    board_tag = stock_tagger.get_board_tag(board_type)
    return {
        "code": code,
        "board_type": board_type,
        "board_tag": board_tag,
        "board_label": stock_tagger.get_tag_label(code),
        "is_tradeable": stock_tagger.is_tradeable(code),
    }

@router.get("/stock-tags")
async def stock_tag_stats(db: AsyncSession = Depends(get_db)):
    """股票标记统计"""
    filter_info = await stock_tagger.get_signal_filter(db)
    return {
        "blocked_count": len(filter_info["blocked_codes"]),
        "suspended_count": len(filter_info["suspended_codes"]),
        "observe_count": len(filter_info["observe_codes"]),
        "limit_up_count": len(filter_info["limit_up_codes"]),
    }
