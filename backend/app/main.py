"""Claw 鹰爪量化交易系统 — FastAPI 入口"""

import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.config.settings import settings
from app.core.tls_trust import install_extra_ca_bundle
from app.db.session import engine, init_db

# 必须早于任何数据源网络调用：把随仓库携带的缺失 CA 中间证书并入进程信任源。
# 背景：申万个股行业分类文件的服务端不下发中间证书，导致 shenwan/stock_mapping
# 长期 down（已验证仅用 certifi 仍失败）。此处补齐证书链，而非关闭校验。
install_extra_ca_bundle()
from app.api.v1 import (
    dashboard, sectors, stocks, tenbagger, promotion,
    sentiment, news, risk, governance, performance, paper, ws,
    factors, backtest, auction, margin, eval_scheduler,
    push, ai, spot, commodity_linkage, trading, sectors_v2, model_lab, market_regime,
    daily_review,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期"""
    logger.info(f"🦅 Claw v{settings.APP_VERSION} 启动中...")
    await init_db()
    logger.info("✅ 数据库初始化完成")

    # 注册风控规则
    from app.risk.rules import register_all_rules
    register_all_rules()
    logger.info("✅ 风控规则链初始化完成")

    # 初始化 AI 模块
    from app.ai.provider import ai_provider
    if ai_provider.enabled:
        logger.info(f"✅ AI模块已启用: {ai_provider.model} @ {ai_provider.base_url}")
    else:
        logger.info("ℹ️ AI模块未启用(关键词降级模式)")

    scheduler_disabled = os.getenv("CLAW_DISABLE_SCHEDULER") == "1"
    if scheduler_disabled:
        logger.info("ℹ️ 数据采集调度器已按环境变量 CLAW_DISABLE_SCHEDULER=1 跳过")
    else:
        # 启动数据采集调度器
        from app.data.scheduler import data_scheduler
        try:
            data_scheduler.start()
            if data_scheduler.scheduler.running:
                logger.info("✅ 数据采集调度器已启动(快频10s/慢频30s/竞价30s/新浪5min/日K三轮)")
            else:
                logger.error("❌ 数据采集调度器启动失败: scheduler.running=False")
        except Exception as e:
            logger.error(f"❌ 数据采集调度器启动异常: {e}")
            import traceback
            logger.error(traceback.format_exc())

    # 启动时检查今日K线数据是否完整，不完整则异步补偿
    import asyncio as _asyncio
    from datetime import date as _date
    async def _startup_kline_check():
        try:
            from sqlalchemy import select, func
            from app.models.stock import StockKline, StockSpot
            from app.db.session import async_session
            from app.core.trade_calendar import trade_calendar
            from app.data.scheduler import _resolve_spot_snapshot_trade_date, _should_run_startup_kline_compensation

            today = _date.today()

            # 只在工作日检查
            if today.weekday() >= 5:
                return

            session_name = trade_calendar.get_trade_session()
            async with async_session() as session:
                latest_spot_result = await session.execute(
                    select(func.max(StockSpot.updated_at)).select_from(StockSpot)
                )
                latest_spot_updated_at = latest_spot_result.scalar_one_or_none()

                latest_spot_trade_date = _resolve_spot_snapshot_trade_date(
                    latest_spot_updated_at,
                    today=today,
                )
                if not _should_run_startup_kline_compensation(
                    session_name=session_name,
                    latest_spot_trade_date=latest_spot_trade_date,
                    today=today,
                ):
                    logger.info(
                        "ℹ️ 启动时K线补偿跳过: "
                        f"session={session_name}, latest_spot_trade_date={latest_spot_trade_date}"
                    )
                    return

                result = await session.execute(
                    select(func.count()).where(StockKline.trade_date == today)
                )
                count = result.scalar()

            if not count or count < 2500:
                logger.warning(f"⚠️ 启动时发现今日K线不足({count or 0}条)，5秒后启动补偿采集...")
                await _asyncio.sleep(5)
                await data_scheduler._spot_to_kline_fill()
                logger.info("✅ 启动时K线补偿采集完成")
            else:
                logger.info(f"✅ 今日K线数据完整({count}条)")
        except Exception as e:
            logger.error(f"启动时K线补偿异常: {e}")
    
    if not scheduler_disabled:
        _asyncio.create_task(_startup_kline_check())

    yield

    # 关闭调度器
    if not scheduler_disabled:
        from app.data.scheduler import data_scheduler
        data_scheduler.stop()
    from app.push.channels import feishu_channel
    await feishu_channel.close()
    await ai_provider.close()
    await engine.dispose()
    logger.info("🦅 Claw 关闭")


app = FastAPI(
    title="Claw 鹰爪量化交易系统",
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.DEBUG else ["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(dashboard.router, prefix="/api/v1/dashboard", tags=["行情总览"])
app.include_router(sectors.router, prefix="/api/v1/sectors", tags=["板块营地"])
app.include_router(sectors_v2.router, prefix="/api/v2/sectors", tags=["板块营地v2"])
app.include_router(stocks.router, prefix="/api/v1/stocks", tags=["个股详情"])
app.include_router(tenbagger.router, prefix="/api/v1/tenbagger", tags=["牛股雷达"])
app.include_router(promotion.router, prefix="/api/v1/promotion", tags=["晋级预测"])
app.include_router(model_lab.router, prefix="/api/v1/model-lab", tags=["模型实验室"])
app.include_router(market_regime.router, prefix="/api/v1/market-regime", tags=["市场风格"])
app.include_router(daily_review.router, prefix="/api/v1/daily-review", tags=["每日复盘"])
app.include_router(commodity_linkage.router, prefix="/api/v1/commodity-linkage", tags=["商品联动"])
app.include_router(sentiment.router, prefix="/api/v1/sentiment", tags=["情绪面"])
app.include_router(news.router, prefix="/api/v1/news", tags=["新闻面"])
app.include_router(risk.router, prefix="/api/v1/risk", tags=["风控中心"])
app.include_router(governance.router, prefix="/api/v1/governance", tags=["数据治理"])
app.include_router(performance.router, prefix="/api/v1/performance", tags=["绩效中心"])
app.include_router(paper.router, prefix="/api/v1/paper", tags=["模拟盘"])
app.include_router(trading.router, prefix="/api/v1/trading", tags=["交易执行"])
app.include_router(ws.router, prefix="/api/v1/ws", tags=["WebSocket"])
app.include_router(factors.router, prefix="/api/v1/factors", tags=["因子引擎"])
app.include_router(backtest.router, prefix="/api/v1/backtest", tags=["回测引擎"])
app.include_router(auction.router, prefix="/api/v1/auction", tags=["竞价分析"])
app.include_router(margin.router, prefix="/api/v1/margin", tags=["融资融券"])
app.include_router(eval_scheduler.router, prefix="/api/v1/eval", tags=["因子评估调度"])
app.include_router(push.router, prefix="/api/v1/push", tags=["推送中心"])
app.include_router(ai.router, prefix="/api/v1/ai", tags=["AI模块"])
app.include_router(spot.router, prefix="/api/v1/stocks", tags=["实时行情+K线"])


@app.get("/")
async def root():
    return {
        "name": "Claw 鹰爪量化交易系统",
        "version": settings.APP_VERSION,
        "status": "running",
    }


@app.get("/health")
async def health():
    """健康检查端点 - 包含调度器状态"""
    from app.data.scheduler import data_scheduler
    
    scheduler_status = {
        "running": data_scheduler.scheduler.running,
        "job_count": len(data_scheduler.scheduler.get_jobs()),
        "paper_auto_trading": data_scheduler._paper_auto_trading,
        "paper_auto_trading_started_at": (
            data_scheduler._paper_auto_trading_started_at.isoformat()
            if data_scheduler._paper_auto_trading_started_at else None
        ),
    }
    
    if data_scheduler.scheduler.running:
        jobs = []
        for job in data_scheduler.scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            })
        scheduler_status["jobs"] = jobs
    
    return {
        "status": "ok",
        "scheduler": scheduler_status,
        # Runtime latency/version inspection must not depend on slow DB health queries.
        "pipeline": data_scheduler.get_pipeline_runtime_status(),
    }
