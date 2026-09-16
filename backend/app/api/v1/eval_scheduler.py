"""因子评估调度 API — 评估任务/因子存储/权重更新"""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.risk.factor_scheduler import factor_eval_scheduler, factor_storage

router = APIRouter()


@router.post("/evaluate/daily")
async def run_daily_evaluation(
    trade_date: Optional[str] = Query(None, description="评估日期 YYYY-MM-DD"),
    as_of_at: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    """执行每日因子评估(盘后)"""
    try:
        d = date.fromisoformat(trade_date) if trade_date else None
        return await factor_eval_scheduler.run_daily_evaluation(db, d, as_of_at=as_of_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/compute-and-store")
async def compute_and_store_factors(
    trade_date: Optional[str] = Query(None, description="交易日期"),
    codes: Optional[str] = Query(None, description="股票代码(逗号分隔)"),
    db: AsyncSession = Depends(get_db),
):
    """有界逐股研究计算并追加旧格式值；非完整截面排名或历史PIT。"""
    try:
        d = date.fromisoformat(trade_date) if trade_date else None
        code_list = codes.split(",") if codes else None
        return await factor_eval_scheduler.compute_and_store_factors(db, d, code_list)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/report")
async def evaluation_report(db: AsyncSession = Depends(get_db)):
    """获取因子评估报告"""
    return await factor_eval_scheduler.get_evaluation_report(db)


@router.get("/decaying")
async def decaying_factors(db: AsyncSession = Depends(get_db)):
    """获取衰减因子列表"""
    factors = await factor_eval_scheduler.get_decaying_factors(db)
    return {"total": len(factors), "factors": factors}


@router.get("/weights")
async def factor_weights(db: AsyncSession = Depends(get_db)):
    """获取当前因子权重"""
    return await factor_eval_scheduler.get_factor_weights(db)


@router.post("/force-recompute")
async def force_recompute(
    factor_name: Optional[str] = Query(None, description="因子名(空=全部)"),
    trade_date: Optional[str] = Query(None, description="评估日期"),
    db: AsyncSession = Depends(get_db),
):
    """强制重新计算因子评估"""
    d = date.fromisoformat(trade_date) if trade_date else None
    return await factor_eval_scheduler.force_recompute(db, factor_name, d)


@router.get("/values/{factor_name}")
async def get_factor_values(
    factor_name: str,
    trade_date: Optional[str] = Query(None, description="交易日期"),
    codes: Optional[str] = Query(None, description="股票代码(逗号分隔)"),
    db: AsyncSession = Depends(get_db),
):
    """查询因子值"""
    d = date.fromisoformat(trade_date) if trade_date else date.today()
    code_list = codes.split(",") if codes else None
    values = await factor_storage.get_factor_values(db, factor_name, d, code_list)
    return {
        "factor_name": factor_name,
        "trade_date": str(d),
        "total": len(values),
        "values": values[:100],
    }


@router.get("/history/{factor_name}/{code}")
async def get_factor_history(
    factor_name: str,
    code: str,
    days: int = Query(60, description="历史天数"),
    db: AsyncSession = Depends(get_db),
):
    """获取因子历史值"""
    df = await factor_storage.get_factor_history(db, factor_name, code, days)
    if df.empty:
        return {"factor_name": factor_name, "code": code, "history": []}
    return {
        "factor_name": factor_name,
        "code": code,
        "total": len(df),
        "history": df.to_dict(orient="records"),
    }
