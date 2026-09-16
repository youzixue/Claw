"""绩效中心 API — 信号统计+因子评估+信号归因"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.signal import SignalPerformance
from app.models.factor import FactorEvaluation
from app.factors import factor_engine

router = APIRouter()


@router.get("/signal-stats")
async def signal_stats(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """信号统计 — 各类型信号数量/胜率/收益"""
    since = date.today() - timedelta(days=days)

    # 按信号类型分组统计
    type_stats_result = await db.execute(
        select(
            SignalPerformance.signal_type,
            func.count(SignalPerformance.signal_id).label("total"),
            func.avg(SignalPerformance.signal_score).label("avg_score"),
            func.count(SignalPerformance.is_correct).label("evaluated"),
            func.sum(case((SignalPerformance.is_correct.is_(True), 1), else_=0)).label("correct"),
            func.avg(SignalPerformance.max_return).label("avg_max_return"),
            func.avg(SignalPerformance.max_drawdown).label("avg_max_drawdown"),
            func.avg(SignalPerformance.net_return_3d).label("avg_net_return_3d"),
            func.avg(SignalPerformance.excess_return_3d).label("avg_excess_return_3d"),
        )
        .where(SignalPerformance.signal_time >= since)
        .group_by(SignalPerformance.signal_type)
    )
    type_rows = type_stats_result.all()

    type_stats = []
    total_signals = 0
    total_correct = 0
    total_evaluated = 0

    for row in type_rows:
        evaluated = row.evaluated or 0
        correct = row.correct or 0
        win_rate = round(correct / evaluated, 4) if evaluated > 0 else None

        type_stats.append({
            "signal_type": row.signal_type,
            "total": row.total,
            "avg_score": round(row.avg_score, 1) if row.avg_score else None,
            "evaluated": evaluated,
            "correct": correct,
            "win_rate": win_rate,
            "avg_max_return": round(row.avg_max_return, 4) if row.avg_max_return else None,
            "avg_max_drawdown": round(row.avg_max_drawdown, 4) if row.avg_max_drawdown else None,
            "avg_net_return_3d": round(row.avg_net_return_3d, 4) if row.avg_net_return_3d is not None else None,
            "avg_excess_return_3d": round(row.avg_excess_return_3d, 4) if row.avg_excess_return_3d is not None else None,
        })
        total_signals += row.total or 0
        total_evaluated += evaluated
        total_correct += correct

    overall_win_rate = round(total_correct / total_evaluated, 4) if total_evaluated > 0 else None

    # 最近10条信号
    recent_result = await db.execute(
        select(SignalPerformance)
        .order_by(desc(SignalPerformance.signal_time))
        .limit(10)
    )
    recent = [
        {
            "signal_id": s.signal_id,
            "stock_code": s.stock_code,
            "signal_type": s.signal_type,
            "signal_variant": s.signal_variant,
            "setup_grade": s.setup_grade,
            "signal_time": s.signal_time.isoformat() if s.signal_time else None,
            "signal_score": s.signal_score,
            "is_correct": s.is_correct,
            "max_return": s.max_return,
            "max_drawdown": s.max_drawdown,
            "return_5d": s.return_5d,
            "net_return_3d": s.net_return_3d,
            "excess_return_3d": s.excess_return_3d,
        }
        for s in recent_result.scalars().all()
    ]

    return {
        "stats": {
            "total_signals": total_signals,
            "total_evaluated": total_evaluated,
            "overall_win_rate": overall_win_rate,
            "by_type": type_stats,
            "recent": recent,
            "period_days": days,
        }
    }


@router.get("/factor-eval")
async def factor_eval(db: AsyncSession = Depends(get_db)):
    """因子评估 — 各因子IC/IR/衰减"""
    from app.factors.evaluator import factor_evaluator
    report = await factor_evaluator.get_factor_report(db)
    latest_by_factor = report["evaluations"]

    # 合并因子注册表信息
    factor_info = factor_engine.get_factor_summary()

    return {
        "factors": list(latest_by_factor.values()),
        "total_evaluated": len(latest_by_factor),
        "total_registered": factor_info.get("total", 0),
        "by_category": factor_info.get("by_category", {}),
    }


@router.get("/attribution/{signal_id}")
async def signal_attribution(signal_id: str, db: AsyncSession = Depends(get_db)):
    """信号归因 — 单条信号的收益归因分析"""
    result = await db.execute(
        select(SignalPerformance).where(SignalPerformance.signal_id == signal_id)
    )
    signal = result.scalar_one_or_none()

    if not signal:
        return {"signal_id": signal_id, "attribution": None, "error": "信号不存在"}

    attribution = {
        "signal_id": signal.signal_id,
        "stock_code": signal.stock_code,
        "signal_type": signal.signal_type,
        "signal_variant": signal.signal_variant,
        "setup_grade": signal.setup_grade,
        "signal_time": signal.signal_time.isoformat() if signal.signal_time else None,
        "signal_price": signal.signal_price,
        "signal_score": signal.signal_score,
        "returns": {
            "1d": signal.return_1d,
            "3d": signal.return_3d,
            "5d": signal.return_5d,
            "10d": signal.return_10d,
            "max": signal.max_return,
        },
        "net_returns": {
            "1d": signal.net_return_1d,
            "3d": signal.net_return_3d,
            "5d": signal.net_return_5d,
            "10d": signal.net_return_10d,
        },
        "benchmark_returns": {
            "1d": signal.benchmark_return_1d,
            "3d": signal.benchmark_return_3d,
            "5d": signal.benchmark_return_5d,
            "10d": signal.benchmark_return_10d,
        },
        "excess_returns": {
            "1d": signal.excess_return_1d,
            "3d": signal.excess_return_3d,
            "5d": signal.excess_return_5d,
            "10d": signal.excess_return_10d,
        },
        "risk": {
            "max_drawdown": signal.max_drawdown,
        },
        "evaluation": {
            "is_correct": signal.is_correct,
            "failure_reason": signal.failure_reason,
            "top_factor": signal.top_factor,
            "board_tag": signal.board_tag,
            "evaluation_version": signal.evaluation_version,
        },
    }

    return {"signal_id": signal_id, "attribution": attribution}
