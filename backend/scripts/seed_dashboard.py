"""种子数据 — 为 Dashboard 页面填充演示数据

用法: python3 scripts/seed_dashboard.py
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

# 确保可以导入 app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.db.session import async_session, init_db
from app.models.stock import (
    StockDaily, MarketSentiment, SectorInfo, SectorPersistence,
    LimitUpPool, FundFlow, StockTag,
)


# ===== 大盘指数 =====
INDEX_DATA = {
    "000001": {"name": "上证指数", "close": 3342.56, "change_pct": 0.85, "volume": 38567234567, "amount": 456789123456.0},
    "399001": {"name": "深证成指", "close": 10876.34, "change_pct": 1.12, "volume": 45678901234, "amount": 567890123456.0},
    "399006": {"name": "创业板指", "close": 2156.78, "change_pct": 1.56, "volume": 12345678901, "amount": 234567890123.0},
}

# ===== 热门板块 =====
HOT_SECTORS = [
    {"sector_code": "BK0477", "sector_name": "人工智能", "consecutive_days": 5, "limit_up_count": 8, "fund_flow": 35.6, "strength_score": 92.3},
    {"sector_code": "BK0480", "sector_name": "芯片半导体", "consecutive_days": 4, "limit_up_count": 6, "fund_flow": 28.9, "strength_score": 88.1},
    {"sector_code": "BK0428", "sector_name": "光伏储能", "consecutive_days": 3, "limit_up_count": 5, "fund_flow": 22.4, "strength_score": 82.5},
    {"sector_code": "BK0473", "sector_name": "机器人", "consecutive_days": 3, "limit_up_count": 4, "fund_flow": 18.7, "strength_score": 79.8},
    {"sector_code": "BK0438", "sector_name": "数据要素", "consecutive_days": 2, "limit_up_count": 3, "fund_flow": 15.2, "strength_score": 75.6},
    {"sector_code": "BK0447", "sector_name": "低空经济", "consecutive_days": 2, "limit_up_count": 3, "fund_flow": 12.8, "strength_score": 72.4},
    {"sector_code": "BK0451", "sector_name": "军工信息化", "consecutive_days": 2, "limit_up_count": 2, "fund_flow": 10.5, "strength_score": 68.9},
    {"sector_code": "BK0465", "sector_name": "算力基建", "consecutive_days": 1, "limit_up_count": 2, "fund_flow": 9.3, "strength_score": 65.2},
]

# ===== 涨停股 =====
TOP_LIMIT_UP = [
    {"code": "688256", "name": "寒武纪", "limit_up_price": 286.50, "consecutive_days": 5, "seal_amount": 856000000, "break_count": 0, "turnover": 3.2},
    {"code": "300033", "name": "同花顺", "limit_up_price": 178.90, "consecutive_days": 3, "seal_amount": 623000000, "break_count": 0, "turnover": 5.6},
    {"code": "002230", "name": "科大讯飞", "limit_up_price": 65.80, "consecutive_days": 3, "seal_amount": 445000000, "break_count": 1, "turnover": 8.9},
    {"code": "688041", "name": "海光信息", "limit_up_price": 132.60, "consecutive_days": 2, "seal_amount": 389000000, "break_count": 0, "turnover": 4.3},
    {"code": "300418", "name": "昆仑万维", "limit_up_price": 52.30, "consecutive_days": 2, "seal_amount": 312000000, "break_count": 0, "turnover": 6.7},
    {"code": "603019", "name": "中科曙光", "limit_up_price": 68.90, "consecutive_days": 2, "seal_amount": 278000000, "break_count": 1, "turnover": 7.2},
    {"code": "002415", "name": "海康威视", "limit_up_price": 42.50, "consecutive_days": 1, "seal_amount": 234000000, "break_count": 0, "turnover": 3.8},
    {"code": "300750", "name": "宁德时代", "limit_up_price": 245.80, "consecutive_days": 1, "seal_amount": 198000000, "break_count": 0, "turnover": 2.1},
    {"code": "601012", "name": "隆基绿能", "limit_up_price": 28.60, "consecutive_days": 1, "seal_amount": 156000000, "break_count": 2, "turnover": 9.5},
    {"code": "000977", "name": "浪潮信息", "limit_up_price": 45.20, "consecutive_days": 1, "seal_amount": 123000000, "break_count": 0, "turnover": 5.4},
]

# ===== 资金流向 TOP =====
TOP_FUND_FLOW = [
    {"code": "601318", "name": "中国平安", "main_net_inflow": 1256000000, "main_net_inflow_pct": 3.25},
    {"code": "600519", "name": "贵州茅台", "main_net_inflow": 987000000, "main_net_inflow_pct": 2.18},
    {"code": "300750", "name": "宁德时代", "main_net_inflow": 856000000, "main_net_inflow_pct": 1.95},
    {"code": "601012", "name": "隆基绿能", "main_net_inflow": 723000000, "main_net_inflow_pct": 4.12},
    {"code": "002230", "name": "科大讯飞", "main_net_inflow": 689000000, "main_net_inflow_pct": 2.87},
]


async def seed():
    """填充演示数据"""
    await init_db()

    # 取最近的交易日(工作日)
    today = date.today()
    # 如果今天是周末，用上周五
    if today.weekday() >= 5:
        today = today - timedelta(days=today.weekday() - 4)
    trade_date = today

    async with async_session() as session:
        # 检查是否已有数据
        existing = await session.execute(
            select(StockDaily).where(StockDaily.code == "000001").limit(1)
        )
        if existing.scalar_one_or_none():
            print("⚠️  数据库已有数据，跳过种子数据填充")
            print("   如需重填，请先删除 claw.db 文件再运行")
            return

        print(f"🦅 开始填充种子数据 (交易日: {trade_date})...")

        # 1. 大盘指数日线
        for code, info in INDEX_DATA.items():
            # 填充最近5个交易日
            for i in range(5):
                d = trade_date - timedelta(days=i)
                # 跳过周末
                if d.weekday() >= 5:
                    d = d - timedelta(days=d.weekday() - 4)
                pct = info["change_pct"] + (i * 0.3 - 0.6)  # 稍微变化
                session.add(StockDaily(
                    code=code,
                    trade_date=d,
                    open=info["close"] * (1 - pct/100) - 5,
                    high=info["close"] + 15,
                    low=info["close"] * (1 - pct/100) - 20,
                    close=info["close"] * (1 - i * 0.003),
                    volume=info["volume"],
                    amount=info["amount"],
                    turnover=2.5 + i * 0.1,
                    amplitude=1.2 + i * 0.1,
                    change_pct=round(pct, 2),
                    prev_close=info["close"] * (1 - pct/100),
                ))
        print("  ✅ 大盘指数(3只×5天)")

        # 2. 市场情绪
        session.add(MarketSentiment(
            trade_date=trade_date,
            sentiment_cycle="recovery",
            limit_up_count=42,
            limit_down_count=8,
            broken_limit_count=12,
            seal_rate=68.5,
            board_height=5,
            advance_decline_ratio=2.3,
            turnover_total=3.8,
            main_net_inflow=156.8,
        ))
        print("  ✅ 市场情绪")

        # 3. 板块信息
        for s in HOT_SECTORS:
            session.add(SectorInfo(
                sector_code=s["sector_code"],
                sector_name=s["sector_name"],
                sector_type="concept",
                source="akshare",
                stock_count=50 + hash(s["sector_code"]) % 100,
                avg_pe=35.6,
                avg_pb=3.2,
                avg_dividend=1.2,
            ))
        print("  ✅ 板块信息(8个)")

        # 4. 板块持续性
        for s in HOT_SECTORS:
            session.add(SectorPersistence(
                sector_code=s["sector_code"],
                sector_name=s["sector_name"],
                trade_date=trade_date,
                consecutive_days=s["consecutive_days"],
                limit_up_count=s["limit_up_count"],
                fund_flow=s["fund_flow"],
                strength_score=s["strength_score"],
            ))
        print("  ✅ 板块持续性(8个)")

        # 5. 涨停池
        for lu in TOP_LIMIT_UP:
            session.add(LimitUpPool(
                code=lu["code"],
                name=lu["name"],
                trade_date=trade_date,
                limit_up_time="09:30:00",
                limit_up_price=lu["limit_up_price"],
                seal_amount=lu["seal_amount"],
                break_count=lu["break_count"],
                consecutive_days=lu["consecutive_days"],
                turnover=lu["turnover"],
                source="eastmoney",
            ))
        print("  ✅ 涨停池(10只)")

        # 6. 资金流向
        for ff in TOP_FUND_FLOW:
            session.add(FundFlow(
                code=ff["code"],
                name=ff["name"],
                trade_date=trade_date,
                main_net_inflow=ff["main_net_inflow"],
                main_net_inflow_pct=ff["main_net_inflow_pct"],
                big_net_inflow=ff["main_net_inflow"] * 0.6,
                mid_net_inflow=ff["main_net_inflow"] * 0.2,
                small_net_inflow=ff["main_net_inflow"] * 0.2,
            ))
        print("  ✅ 资金流向(5只)")

        await session.commit()

    print(f"\n🦅 种子数据填充完成! 共填充 {trade_date} 的演示数据")


if __name__ == "__main__":
    asyncio.run(seed())
