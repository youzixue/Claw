"""因子引擎 API — 因子查询/计算/评估"""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.factors import factor_engine, FactorRegistry, FactorCategory

router = APIRouter()


@router.get("/summary")
async def factor_summary():
    """因子概览 — 10类48因子列表"""
    return factor_engine.get_factor_summary()


@router.get("/categories")
async def factor_categories():
    """因子分类统计"""
    return {
        "total": FactorRegistry.count(),
        "by_category": FactorRegistry.summary(),
        "categories": [
            {
                "key": cat.value,
                "count": len(FactorRegistry.get_by_category(cat)),
                "factors": [
                    {
                        "name": f.factor_name,
                        "direction": f.direction,
                        "description": f.description,
                    }
                    for f in FactorRegistry.get_by_category(cat)
                ],
            }
            for cat in FactorCategory
        ],
    }


@router.get("/{factor_name}/info")
async def factor_info(factor_name: str):
    """单个因子详情"""
    factor = FactorRegistry.get(factor_name)
    if not factor:
        return {"error": f"因子不存在: {factor_name}"}

    return {
        "name": factor.factor_name,
        "category": factor.category.value,
        "direction": factor.direction,
        "description": factor.description,
        "dependencies": factor.dependencies,
    }


@router.get("/evaluate")
async def evaluate_factors(
    eval_date: Optional[str] = Query(None, description="评估日期 YYYY-MM-DD"),
    as_of_at: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    """只读获取已保存评估；打开页面不得计算/写库。"""
    from app.factors.evaluator import factor_evaluator

    try:
        d = date.fromisoformat(eval_date) if eval_date else None
        return await factor_evaluator.get_evaluations(db, d, as_of_at=as_of_at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/evaluate/{factor_name}")
async def evaluate_single_factor(
    factor_name: str,
    eval_date: Optional[str] = Query(None, description="评估日期"),
    as_of_at: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
):
    """只读获取单因子的最新评估。"""
    from app.factors.evaluator import factor_evaluator

    try:
        d = date.fromisoformat(eval_date) if eval_date else None
        result = await factor_evaluator.get_evaluations(db, d, factor_name, as_of_at=as_of_at)
        return result["results"].get(factor_name, {"factor_name": factor_name, "status": "not_evaluated"})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/report")
async def factor_report(db: AsyncSession = Depends(get_db)):
    """因子报告概览"""
    from app.factors.evaluator import factor_evaluator

    return await factor_evaluator.get_factor_report(db)


@router.post("/compute/{code}")
async def compute_factors(
    code: str,
    trade_date: Optional[str] = Query(None, description="计算日期"),
    db: AsyncSession = Depends(get_db),
):
    """Read-time formal daily research factors; no historical PIT claim or writes."""
    from app.factors.market_inputs import load_factor_market_inputs

    # Freeze the real request-start cutoff; never stamp the last old row as today.
    at = datetime.now()
    try:
        requested = date.fromisoformat(trade_date) if trade_date else None
        df, evidence = await load_factor_market_inputs(
            db, code=code, trade_date=requested, as_of_at=at)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    d = date.fromisoformat(evidence["trade_date"])
    results = await factor_engine.compute_single(code, d, df)
    return {
        "code": code,
        "trade_date": d.isoformat(),
        "requested_trade_date": trade_date,
        "input_evidence": evidence,
        "factors": {
            name: {
                "value": r.value, "rank": r.rank, "pct": r.pct,
                "confidence": r.confidence, "meta": r.meta,
            }
            for name, r in results.items()
        },
        "factor_count": len(results),
        "available_factor_count": sum(r.value is not None and r.confidence > 0 for r in results.values()),
    }
