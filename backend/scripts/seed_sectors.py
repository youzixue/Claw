"""种子数据 — 为板块营地页面填充演示数据(pywencai口径)

板块口径:
- 行业板块: pywencai一级行业31个
- 概念板块: pywencai概念389个(种子数据只填充热门30个)
- akshare/申万板块保留, 但不在板块营地展示

用法: python3 scripts/seed_sectors.py
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

# 确保可以导入 app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, and_
from app.db.session import async_session, init_db
from app.models.stock import (
    SectorInfo, SectorPersistence, StockSectorMapping,
    LimitUpPool, FundFlow, StockTag,
)
from app.models.sector import SectorStrength, SectorRotation


# ===== pywencai行业板块(一级行业31个) =====
PW_INDUSTRY_DATA = [
    {"sector_name": "交通运输", "stock_count": 120},
    {"sector_name": "传媒", "stock_count": 95},
    {"sector_name": "公用事业", "stock_count": 135},
    {"sector_name": "农林牧渔", "stock_count": 88},
    {"sector_name": "化工", "stock_count": 210},
    {"sector_name": "医药生物", "stock_count": 310},
    {"sector_name": "商贸零售", "stock_count": 78},
    {"sector_name": "国防军工", "stock_count": 95},
    {"sector_name": "建筑材料", "stock_count": 72},
    {"sector_name": "建筑装饰", "stock_count": 85},
    {"sector_name": "房地产", "stock_count": 68},
    {"sector_name": "有色金属", "stock_count": 115},
    {"sector_name": "机械设备", "stock_count": 280},
    {"sector_name": "汽车", "stock_count": 198},
    {"sector_name": "电子", "stock_count": 312},
    {"sector_name": "电力设备", "stock_count": 286},
    {"sector_name": "社会服务", "stock_count": 68},
    {"sector_name": "纺织服饰", "stock_count": 62},
    {"sector_name": "综合", "stock_count": 35},
    {"sector_name": "计算机", "stock_count": 245},
    {"sector_name": "轻工制造", "stock_count": 92},
    {"sector_name": "通信", "stock_count": 108},
    {"sector_name": "钢铁", "stock_count": 45},
    {"sector_name": "银行", "stock_count": 42},
    {"sector_name": "非银金融", "stock_count": 85},
    {"sector_name": "食品饮料", "stock_count": 78},
    {"sector_name": "环保", "stock_count": 52},
    {"sector_name": "美容护理", "stock_count": 28},
    {"sector_name": "石油石化", "stock_count": 48},
    {"sector_name": "煤炭", "stock_count": 32},
    {"sector_name": "基础化工", "stock_count": 180},
]

# ===== pywencai概念板块(热门30个) =====
PW_CONCEPT_DATA = [
    {"sector_name": "人工智能", "stock_count": 128},
    {"sector_name": "芯片概念", "stock_count": 96},
    {"sector_name": "光伏概念", "stock_count": 85},
    {"sector_name": "新能源汽车", "stock_count": 112},
    {"sector_name": "华为概念", "stock_count": 156},
    {"sector_name": "机器人概念", "stock_count": 78},
    {"sector_name": "数据要素", "stock_count": 65},
    {"sector_name": "低空经济", "stock_count": 42},
    {"sector_name": "算力概念", "stock_count": 55},
    {"sector_name": "CPO概念", "stock_count": 28},
    {"sector_name": "军工", "stock_count": 88},
    {"sector_name": "AIGC概念", "stock_count": 62},
    {"sector_name": "量子科技", "stock_count": 22},
    {"sector_name": "固态电池", "stock_count": 38},
    {"sector_name": "CRO概念", "stock_count": 25},
    {"sector_name": "ChatGPT概念", "stock_count": 48},
    {"sector_name": "web3.0", "stock_count": 32},
    {"sector_name": "DRG/DIP", "stock_count": 18},
    {"sector_name": "跨境支付(CIPS)", "stock_count": 28},
    {"sector_name": "光伏建筑一体化", "stock_count": 22},
    {"sector_name": "阿尔茨海默概念", "stock_count": 35},
    {"sector_name": "人民币贬值受益", "stock_count": 55},
    {"sector_name": "MicroLED概念", "stock_count": 15},
    {"sector_name": "NMN概念", "stock_count": 20},
    {"sector_name": "HJT电池", "stock_count": 26},
    {"sector_name": "MLOps概念", "stock_count": 12},
    {"sector_name": "MR(混合现实)", "stock_count": 18},
    {"sector_name": "PVDF概念", "stock_count": 14},
    {"sector_name": "C2M概念", "stock_count": 16},
    {"sector_name": "AIGC概念", "stock_count": 62},
]

# ===== 持续性数据(5天渐变) =====
PERSISTENCE_SEED = [
    # 概念(热门)
    {"name": "人工智能", "days_start": 5, "fund_start": 35.2, "strength_start": 88},
    {"name": "低空经济", "days_start": 4, "fund_start": 28.5, "strength_start": 82},
    {"name": "芯片概念", "days_start": 3, "fund_start": 22.1, "strength_start": 75},
    {"name": "算力概念", "days_start": 3, "fund_start": 18.6, "strength_start": 72},
    {"name": "AIGC概念", "days_start": 2, "fund_start": 15.3, "strength_start": 68},
    {"name": "机器人概念", "days_start": 4, "fund_start": 20.5, "strength_start": 78},
    {"name": "华为概念", "days_start": 3, "fund_start": 25.8, "strength_start": 76},
    {"name": "CPO概念", "days_start": 2, "fund_start": 12.1, "strength_start": 65},
    {"name": "光伏概念", "days_start": 2, "fund_start": -5.2, "strength_start": 48},
    {"name": "固态电池", "days_start": 2, "fund_start": 8.5, "strength_start": 62},
    # 行业
    {"name": "计算机", "days_start": 3, "fund_start": 22.5, "strength_start": 76},
    {"name": "电子", "days_start": 3, "fund_start": 18.8, "strength_start": 72},
    {"name": "电力设备", "days_start": 2, "fund_start": -3.5, "strength_start": 52},
    {"name": "银行", "days_start": 2, "fund_start": 8.2, "strength_start": 58},
    {"name": "医药生物", "days_start": 2, "fund_start": -8.5, "strength_start": 42},
]

# ===== 涨停股 =====
LIMIT_UP_SEED = [
    {"code": "688256", "name": "寒武纪", "limit_price": 285.6, "seal_amount": 156000000, "consecutive": 2, "reason": "AI芯片龙头"},
    {"code": "002230", "name": "科大讯飞", "limit_price": 58.2, "seal_amount": 89000000, "consecutive": 1, "reason": "AIGC概念"},
    {"code": "300033", "name": "同花顺", "limit_price": 156.8, "seal_amount": 62000000, "consecutive": 1, "reason": "金融科技"},
    {"code": "300418", "name": "昆仑万维", "limit_price": 42.5, "seal_amount": 48000000, "consecutive": 3, "reason": "AIGC+游戏"},
    {"code": "688041", "name": "海光信息", "limit_price": 98.6, "seal_amount": 125000000, "consecutive": 2, "reason": "国产CPU"},
    {"code": "300496", "name": "中科创达", "limit_price": 78.2, "seal_amount": 35000000, "consecutive": 1, "reason": "智能汽车"},
    {"code": "002415", "name": "海康威视", "limit_price": 38.5, "seal_amount": 95000000, "consecutive": 1, "reason": "AI视觉"},
    {"code": "300024", "name": "机器人", "limit_price": 18.6, "seal_amount": 42000000, "consecutive": 2, "reason": "人形机器人"},
]

# ===== 轮动信号 =====
ROTATION_SEED = [
    {"from": "医药生物", "to": "人工智能", "amount": 15.2, "type": "gradual"},
    {"from": "房地产", "to": "低空经济", "amount": 8.5, "type": "sudden"},
    {"from": "光伏概念", "to": "算力概念", "amount": 12.8, "type": "gradual"},
    {"from": "银行", "to": "芯片概念", "amount": 6.2, "type": "gradual"},
]


def _get_trade_dates(n=5):
    """获取最近n个交易日(跳周末)"""
    today = date.today()
    dates = []
    d = today
    while len(dates) < n:
        if d.weekday() < 5:  # 周一到周五
            dates.append(d)
        d -= timedelta(days=1)
    return list(reversed(dates))


async def seed():
    await init_db()
    async with async_session() as session:
        trade_dates = _get_trade_dates(5)
        print(f"交易日: {[str(d) for d in trade_dates]}")

        # ===== 1. pywencai行业板块 → SectorInfo =====
        industry_count = 0
        for ind in PW_INDUSTRY_DATA:
            sector_code = f"pw_industry_{ind['sector_name']}"
            existing = await session.execute(
                select(SectorInfo).where(SectorInfo.sector_code == sector_code)
            )
            row = existing.scalar_one_or_none()
            if not row:
                session.add(SectorInfo(
                    sector_code=sector_code,
                    sector_name=ind["sector_name"],
                    sector_type="industry",
                    source="pywencai",
                    stock_count=ind["stock_count"],
                ))
                industry_count += 1
        print(f"pywencai行业板块: 新增{industry_count}个(总{len(PW_INDUSTRY_DATA)}个)")

        # ===== 2. pywencai概念板块 → SectorInfo =====
        concept_count = 0
        for con in PW_CONCEPT_DATA:
            sector_code = f"pw_concept_{con['sector_name']}"
            existing = await session.execute(
                select(SectorInfo).where(SectorInfo.sector_code == sector_code)
            )
            row = existing.scalar_one_or_none()
            if not row:
                session.add(SectorInfo(
                    sector_code=sector_code,
                    sector_name=con["sector_name"],
                    sector_type="concept",
                    source="pywencai",
                    stock_count=con["stock_count"],
                ))
                concept_count += 1
        print(f"pywencai概念板块: 新增{concept_count}个(总{len(PW_CONCEPT_DATA)}个)")

        # ===== 3. 持续性数据(5天) =====
        persistence_count = 0
        for seed_item in PERSISTENCE_SEED:
            # 检查是行业还是概念
            is_industry = any(ind["sector_name"] == seed_item["name"] for ind in PW_INDUSTRY_DATA)
            prefix = "pw_industry_" if is_industry else "pw_concept_"
            sector_code = f"{prefix}{seed_item['name']}"

            for i, td in enumerate(trade_dates):
                # 渐变衰减
                decay = 1 - i * 0.15
                days = max(1, int(seed_item["days_start"] * decay))
                fund = round(seed_item["fund_start"] * decay, 2)
                strength = round(seed_item["strength_start"] * decay, 1)

                existing = await session.execute(
                    select(SectorPersistence).where(
                        and_(
                            SectorPersistence.sector_code == sector_code,
                            SectorPersistence.trade_date == td,
                        )
                    )
                )
                row = existing.scalar_one_or_none()
                if not row:
                    session.add(SectorPersistence(
                        sector_code=sector_code,
                        sector_name=seed_item["name"],
                        trade_date=td,
                        consecutive_days=days,
                        limit_up_count=max(0, int(3 * decay)),
                        fund_flow=fund,
                        strength_score=strength,
                    ))
                    persistence_count += 1
        print(f"持续性数据: 新增{persistence_count}条")

        # ===== 4. 涨停股 =====
        lu_count = 0
        for td in trade_dates:
            for i, lu in enumerate(LIMIT_UP_SEED):
                existing = await session.execute(
                    select(LimitUpPool).where(
                        and_(
                            LimitUpPool.code == lu["code"],
                            LimitUpPool.trade_date == td,
                        )
                    )
                )
                row = existing.scalar_one_or_none()
                if not row:
                    session.add(LimitUpPool(
                        code=lu["code"],
                        name=lu["name"],
                        trade_date=td,
                        limit_up_price=lu["limit_price"],
                        seal_amount=lu["seal_amount"],
                        consecutive_days=lu["consecutive"],
                        limit_up_reason=lu.get("reason", ""),
                        source="seed",
                    ))
                    lu_count += 1
        print(f"涨停股: 新增{lu_count}条")

        # ===== 5. 轮动信号 =====
        rot_count = 0
        for td in trade_dates:
            for rot in ROTATION_SEED:
                # 判断来源/目标的sector_code
                is_from_industry = any(ind["sector_name"] == rot["from"] for ind in PW_INDUSTRY_DATA)
                is_to_industry = any(ind["sector_name"] == rot["to"] for ind in PW_INDUSTRY_DATA)
                from_code = f"pw_industry_{rot['from']}" if is_from_industry else f"pw_concept_{rot['from']}"
                to_code = f"pw_industry_{rot['to']}" if is_to_industry else f"pw_concept_{rot['to']}"

                existing = await session.execute(
                    select(SectorRotation).where(
                        and_(
                            SectorRotation.trade_date == td,
                            SectorRotation.from_sector == from_code,
                            SectorRotation.to_sector == to_code,
                        )
                    )
                )
                row = existing.scalar_one_or_none()
                if not row:
                    session.add(SectorRotation(
                        trade_date=td,
                        from_sector=from_code,
                        to_sector=to_code,
                        flow_amount=rot["amount"],
                        rotation_type=rot["type"],
                    ))
                    rot_count += 1
        print(f"轮动信号: 新增{rot_count}条")

        await session.commit()
        print("\n✅ 种子数据填充完成!")


if __name__ == "__main__":
    asyncio.run(seed())
