"""推送中心 API — 飞书+WebSocket+4时段复盘"""

from fastapi import APIRouter, Query
from loguru import logger

from app.push.scheduler import push_scheduler
from app.push.channels.base import PushMessage
from app.push.templates.anomaly import (
    limit_up_alert, capital_anomaly_alert,
    breakthrough_alert, bull_stock_alert,
)
from app.push.templates.review import (
    morning_review, midday_review, closing_review, evening_review,
)
from app.push.templates.risk import (
    risk_block_alert, drawdown_alert, lockup_warning,
    sentiment_circuit_alert,
)

router = APIRouter()


@router.get("/test")
async def push_test():
    """测试飞书推送"""
    result = await push_scheduler.test_feishu()
    return {"result": result}


@router.post("/send")
async def push_send(message: PushMessage):
    """手动推送消息"""
    result = await push_scheduler.push(message)
    return {"result": result}


@router.get("/stats")
async def push_stats():
    """推送统计"""
    return await push_scheduler.get_stats()


@router.get("/paper-buy-points")
async def paper_buy_point_status():
    """十二账户买点通知的持久送达审计；不触发交易或测试推送。"""
    from app.push.paper_buy_points import notification_status
    from app.data.scheduler import data_scheduler
    result = await notification_status()
    task = data_scheduler._paper_buy_point_push_task
    result["worker_running"] = bool(task and not task.done() and data_scheduler.scheduler.running)
    return result


@router.get("/history")
async def push_history(limit: int = Query(50)):
    """推送历史"""
    return {"history": push_scheduler.get_history(limit)}


@router.get("/throttle")
async def throttle_stats():
    """限频统计"""
    from app.push.throttle import push_throttle
    return push_throttle.get_stats()


# ========== 4时段复盘 ==========

@router.post("/review/morning")
async def push_morning_review():
    """推送盘前复盘"""
    msg = morning_review()
    result = await push_scheduler.push(msg)
    return {"result": result}


@router.post("/review/midday")
async def push_midday_review():
    """推送午盘复盘"""
    msg = midday_review()
    result = await push_scheduler.push(msg)
    return {"result": result}


@router.post("/review/closing")
async def push_closing_review():
    """推送盘后复盘"""
    msg = closing_review()
    result = await push_scheduler.push(msg)
    return {"result": result}


@router.post("/review/evening")
async def push_evening_review():
    """推送晚间复盘"""
    msg = evening_review()
    result = await push_scheduler.push(msg)
    return {"result": result}


# ========== 异动推送 ==========

@router.post("/alert/limit-up")
async def push_limit_up(code: str, name: str, price: float,
                        consecutive_days: int = 1):
    """推送涨停异动"""
    msg = limit_up_alert(code, name, price, consecutive_days)
    result = await push_scheduler.push(msg)
    return {"result": result}


@router.post("/alert/capital")
async def push_capital_anomaly(code: str, name: str, description: str,
                               level: str = "major"):
    """推送资金异动"""
    msg = capital_anomaly_alert(code, name, description, level)
    result = await push_scheduler.push(msg)
    return {"result": result}


@router.post("/alert/breakthrough")
async def push_breakthrough(code: str, name: str, description: str,
                            strength: str = "moderate"):
    """推送突破信号"""
    msg = breakthrough_alert(code, name, description, strength)
    result = await push_scheduler.push(msg)
    return {"result": result}
