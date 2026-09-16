"""交易执行 API — 委托/撤单/成交回报/同步"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.trading.service import (
    SubmitOrderCommand,
    cancel_order,
    list_fills,
    list_orders,
    submit_order,
    validated_order_price,
    validate_order_notional,
    sync_broker,
)

router = APIRouter()


class SubmitOrderRequest(BaseModel):
    code: str
    side: str = Field(pattern="^(buy|sell)$")
    price: Annotated[float, BeforeValidator(validated_order_price)] = Field(gt=0)
    quantity: int = Field(ge=100)
    broker: str = "paper"
    account_id: str = "default"
    order_type: str = Field(default="limit", pattern="^(limit|market)$")
    strategy_id: str = ""
    signal_id: str = ""
    source: str = ""
    reason: str = ""
    execute: bool = True

    @model_validator(mode="after")
    def finite_order_amount(self):
        validate_order_notional(self.price, self.quantity)
        return self


@router.post("/orders")
async def create_order(req: SubmitOrderRequest, db: AsyncSession = Depends(get_db)):
    """公开委托不可冒用内部Challenger身份；paper即时成交另验真实深度。"""
    from app.api.v1 import paper
    if req.account_id in paper.PAPER_CHALLENGER_ACCOUNTS:
        raise HTTPException(403, "隔离候选账户只接受内部前向确认事件委托")
    return await submit_order(
        db,
        SubmitOrderCommand(
            code=req.code.strip(),
            side=req.side,
            price=req.price,
            quantity=req.quantity,
            broker=req.broker,
            account_id=req.account_id,
            order_type=req.order_type,
            strategy_id=req.strategy_id,
            signal_id=req.signal_id,
            source=req.source,
            reason=req.reason,
            execute=req.execute,
            require_immediate_quote=req.broker == "paper",
            decision_at=paper._public_order_clock(),
        ),
    )


@router.get("/orders")
async def orders(
    limit: int = Query(100, ge=1, le=500),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """委托列表。"""
    return await list_orders(db, limit=limit, status=status)


@router.post("/orders/{order_id}/cancel")
async def cancel(order_id: str, db: AsyncSession = Depends(get_db)):
    """撤单。已成交、已拒绝、风控拦截的委托不可撤。"""
    return await cancel_order(db, order_id)


@router.get("/fills")
async def fills(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """成交回报列表。"""
    return await list_fills(db, limit=limit)


@router.post("/sync")
async def sync(
    broker: str = Query("paper"),
    db: AsyncSession = Depends(get_db),
):
    """同步账户资金和持仓。"""
    return await sync_broker(db, broker)
