"""风控中心 API — 规则链/解禁预警/情绪熔断"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.stock_tagger import stock_tagger
from app.risk.engine import risk_engine, RiskContext
from app.risk.lockup import lockup_manager
from app.risk.circuit_breaker import sentiment_circuit_breaker

router = APIRouter()


# ========== 辅助函数 ==========

async def session_get_stock_tag(db: AsyncSession, code: str):
    """从DB获取股票标记"""
    from app.models.stock import StockTag
    result = await db.execute(select(StockTag).where(StockTag.code == code))
    return result.scalar_one_or_none()


# ========== 请求模型 ==========

class RiskCheckRequest(BaseModel):
    """风控检查请求"""
    code: str
    action: str = "buy"       # buy/sell/hold
    price: float = 0
    amount: int = 0
    total_assets: float = 0
    cash: float = 0
    position_value: float = 0
    max_drawdown: float = 0
    board_tag: str = "tradeable"
    is_st: bool = False
    is_suspended: bool = False
    is_delisting: bool = False
    is_ipo_recent: bool = False
    sentiment_cycle: str = "recovery"
    has_lockup_soon: bool = False
    lockup_ratio: float = 0
    hold_days: int = 0
    profit_pct: float = 0
    current_positions: dict = {}
    sector_exposure: dict = {}


# ========== 风控规则链 ==========

@router.get("/rules")
async def risk_rules():
    """获取风控规则列表"""
    return {
        "rules": risk_engine.get_rules(),
        "total": len(risk_engine.get_rules()),
    }


@router.post("/check")
async def risk_check(req: RiskCheckRequest, db: AsyncSession = Depends(get_db)):
    """执行风控检查 — 自动从DB填充股票状态(ST/停牌/退市)"""
    status = await stock_tagger.load_status(db, req.code)
    # 用户输入只能增加风险，不能替代缺失身份或覆盖数据库的限制。
    board_tag = status["board_tag"]
    if req.board_tag == "suspended":
        board_tag = "suspended"
    elif req.board_tag == "blocked" and board_tag != "suspended":
        board_tag = "blocked"
    elif req.board_tag == "observe_only" and board_tag == "tradeable":
        board_tag = "observe_only"
    is_st = status["is_st"] or req.is_st
    is_suspended = status["is_suspended"] or req.is_suspended
    is_delisting = status["is_delisting"] or req.is_delisting
    is_ipo_recent = status["is_ipo_recent"] or req.is_ipo_recent

    ctx = RiskContext(
        code=req.code,
        action=req.action,
        price=req.price,
        amount=req.amount,
        total_assets=req.total_assets,
        cash=req.cash,
        position_value=req.position_value,
        max_drawdown=req.max_drawdown,
        board_tag=board_tag,
        is_st=is_st,
        is_suspended=is_suspended,
        is_delisting=is_delisting,
        is_ipo_recent=is_ipo_recent,
        sentiment_cycle=req.sentiment_cycle,
        has_lockup_soon=req.has_lockup_soon,
        lockup_ratio=req.lockup_ratio,
        hold_days=req.hold_days,
        profit_pct=req.profit_pct,
        current_positions=req.current_positions,
        sector_exposure=req.sector_exposure,
    )
    result = risk_engine.check(ctx)
    # 附加股票状态信息(方便前端展示)
    result["stock_status"] = {
        **status,
        "is_tradeable": status["is_tradeable"] and board_tag == "tradeable" and not (is_st or is_suspended or is_delisting),
        "board_tag": board_tag,
        "is_st": is_st,
        "is_suspended": is_suspended,
        "is_delisting": is_delisting,
        "is_ipo_recent": is_ipo_recent,
    }
    return result


@router.put("/rules/{rule_name}/toggle")
async def toggle_rule(rule_name: str, enabled: bool = Query(...)):
    """开关风控规则"""
    risk_engine.toggle_rule(rule_name, enabled)
    return {"rule_name": rule_name, "enabled": enabled}


# ========== 解禁预警 ==========

@router.get("/lockup/calendar")
async def lockup_calendar(
    days: int = Query(30, description="查询天数"),
    db: AsyncSession = Depends(get_db),
):
    """解禁日历"""
    from datetime import timedelta
    start_date = date.today()
    end_date = start_date + timedelta(days=days)
    return await lockup_manager.get_lockup_calendar(db, start_date, end_date)


@router.get("/lockup/upcoming")
async def lockup_upcoming(
    days: int = Query(30, description="未来天数"),
    risk_level: Optional[str] = Query(None, description="风险等级 high/medium/low"),
    db: AsyncSession = Depends(get_db),
):
    """未来解禁列表"""
    return {
        "lockups": await lockup_manager.get_upcoming_lockups(db, days, risk_level),
    }


@router.get("/lockup/{code}")
async def lockup_check(
    code: str,
    days: int = Query(30, description="检查天数"),
    db: AsyncSession = Depends(get_db),
):
    """个股解禁检查"""
    result = await lockup_manager.check_stock_lockup(db, code, days)
    return result or {"code": code, "has_lockup_soon": False}


# ========== 情绪熔断 ==========

@router.get("/sentiment/state")
async def sentiment_state(
    trade_date: Optional[str] = Query(None, description="交易日期"),
    db: AsyncSession = Depends(get_db),
):
    """获取情绪状态与仓位建议"""
    d = date.fromisoformat(trade_date) if trade_date else None
    return await sentiment_circuit_breaker.get_position_advice(db, d)


@router.get("/sentiment/history")
async def sentiment_history(
    days: int = Query(30, description="历史天数"),
    db: AsyncSession = Depends(get_db),
):
    """情绪历史"""
    return {
        "history": await sentiment_circuit_breaker.get_history(db, days),
    }
