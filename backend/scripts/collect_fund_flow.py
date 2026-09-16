"""板块资金流实时采集 — AkShare板块级接口

数据源:
- 概念资金流: ak.stock_fund_flow_concept() → 387个概念
- 行业资金流: ak.stock_fund_flow_industry() → 90个同花顺一级行业

写入:
- SectorPersistence: 更新fund_flow/change_pct
- SectorStrength: 更新fund_flow

性能: 概念1.7s + 行业0.4s ≈ 2s内完成
"""

import asyncio
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_, func, text

from app.db.session import async_session, init_db
from app.models.stock import SectorInfo, SectorPersistence
from app.models.sector import SectorStrength


def _collect_concept_fund_flow() -> pd.DataFrame:
    """采集概念板块资金流 (1-2s)"""
    df = ak.stock_fund_flow_concept()
    # 列: 序号, 行业, 行业指数, 行业-涨跌幅, 流入资金, 流出资金, 净额, 公司家数, 领涨股, 领涨股-涨跌幅, 当前价
    df = df.rename(columns={
        "行业": "sector_name",
        "行业-涨跌幅": "change_pct",
        "净额": "fund_flow",     # 亿
        "流入资金": "fund_in",
        "流出资金": "fund_out",
        "公司家数": "stock_count",
        "领涨股": "lead_stock",
        "领涨股-涨跌幅": "lead_change_pct",
    })
    df["sector_type"] = "concept"
    return df[["sector_name", "change_pct", "fund_flow", "fund_in", "fund_out",
               "stock_count", "lead_stock", "lead_change_pct", "sector_type"]]


def _collect_industry_fund_flow() -> pd.DataFrame:
    """采集行业板块资金流 (0.3-0.5s)"""
    df = ak.stock_fund_flow_industry()
    df = df.rename(columns={
        "行业": "sector_name",
        "行业-涨跌幅": "change_pct",
        "净额": "fund_flow",
        "流入资金": "fund_in",
        "流出资金": "fund_out",
        "公司家数": "stock_count",
        "领涨股": "lead_stock",
        "领涨股-涨跌幅": "lead_change_pct",
    })
    df["sector_type"] = "industry"
    return df[["sector_name", "change_pct", "fund_flow", "fund_in", "fund_out",
               "stock_count", "lead_stock", "lead_change_pct", "sector_type"]]


async def _match_sector_code(session, sector_name: str, sector_type: str) -> str | None:
    """根据板块名称匹配SectorInfo中的sector_code

    匹配策略:
    1. 精确匹配sector_name
    2. 概念: 模糊匹配(去空格/特殊字符)
    3. 行业: 一级行业名匹配三级行业前缀
    """
    # 1. 精确匹配
    result = await session.execute(
        select(SectorInfo.sector_code).where(
            and_(
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type == sector_type,
                SectorInfo.sector_name == sector_name,
            )
        ).limit(1)
    )
    code = result.scalar_one_or_none()
    if code:
        return code

    # 2. 概念模糊匹配(去除空格和特殊字符)
    clean_name = sector_name.replace(" ", "").replace("　", "")
    result = await session.execute(
        select(SectorInfo.sector_code, SectorInfo.sector_name).where(
            and_(
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type == sector_type,
            )
        )
    )
    all_sectors = result.all()
    for row in all_sectors:
        db_clean = row.sector_name.replace(" ", "").replace("　", "")
        if db_clean == clean_name:
            return row.sector_code

    # 3. 行业: 一级行业名匹配三级行业前缀
    # 如"电池" 匹配 "电力设备-电池-*"
    if sector_type == "industry":
        for row in all_sectors:
            parts = row.sector_name.split("-")
            if len(parts) >= 2 and parts[1] == sector_name:
                return row.sector_code
            if len(parts) >= 1 and parts[0] == sector_name:
                return row.sector_code

    return None


async def collect_and_save():
    """采集板块资金流并写入DB"""
    await init_db()

    t0 = time.time()
    logger.info("开始采集板块资金流...")

    # 1. 确定最近交易日(AkShare返回的是最近交易日数据)
    async with async_session() as session:
        # 优先用已有的SectorPersistence最近日期(历史交易日)
        result = await session.execute(
            select(func.max(SectorPersistence.trade_date))
        )
        latest_db_date = result.scalar()
    
    # 如果DB里有历史数据，取最后一个交易日作为trade_date
    # AkShare接口返回的总是最新交易日的数据
    trade_date = latest_db_date or date.today()
    
    # 但如果今天是交易日且盘中，应使用今天；非交易日使用上一个交易日
    today = date.today()
    if today.weekday() < 5:  # 工作日
        # 工作日：假设AkShare返回的是今天或昨天的数据
        # 简单处理：如果已有今天的持续性数据，用今天；否则用最近的历史日期
        pass
    else:
        # 周末/节假日：AkShare返回的是周五的数据
        # 找到最近的周五
        days_since_friday = (today.weekday() - 4) % 7
        last_friday = today - __import__('datetime').timedelta(days=days_since_friday)
        if trade_date < last_friday:
            trade_date = last_friday

    # 1. 采集
    concept_df = _collect_concept_fund_flow()
    industry_df = _collect_industry_fund_flow()
    t1 = time.time()
    logger.info(f"采集完成: 概念{len(concept_df)}个, 行业{len(industry_df)}个, 耗时{t1-t0:.1f}s")

    # 2. 合并
    all_df = pd.concat([concept_df, industry_df], ignore_index=True)

    # 3. 匹配sector_code并写入
    today = date.today()
    matched = 0
    unmatched_names = []

    async with async_session() as session:
        # 预加载所有pywencai板块映射(name → code)
        result = await session.execute(
            select(SectorInfo.sector_code, SectorInfo.sector_name, SectorInfo.sector_type)
            .where(SectorInfo.source == "pywencai")
        )
        all_sectors = result.all()

        # 建立名称→code映射
        name_map = {}  # (sector_name_clean, sector_type) → sector_code
        for row in all_sectors:
            key = (row.sector_name, row.sector_type)
            name_map[key] = row.sector_code
            # 概念去空格
            clean_key = (row.sector_name.replace(" ", "").replace("　", ""), row.sector_type)
            if clean_key not in name_map:
                name_map[clean_key] = row.sector_code

        # 行业二级/一级名称→sector_codes映射
        # AkShare的行业名可能是pywencai的二级名(如"电池"→"电力设备-电池")
        industry_sub_map = {}  # sub_name → [sector_code, ...]
        for row in all_sectors:
            if row.sector_type == "industry":
                parts = row.sector_name.split("-")
                # 一级名(如"电力设备")
                l1 = parts[0] if parts else row.sector_name
                if l1 not in industry_sub_map:
                    industry_sub_map[l1] = []
                industry_sub_map[l1].append(row.sector_code)
                # 二级名(如"电池"→"电力设备-电池")
                if len(parts) >= 2:
                    l2 = parts[1]
                    if l2 not in industry_sub_map:
                        industry_sub_map[l2] = []
                    industry_sub_map[l2].append(row.sector_code)

        for _, row in all_df.iterrows():
            sector_name = str(row["sector_name"])
            sector_type = row["sector_type"]
            fund_flow = float(row["fund_flow"]) if pd.notna(row["fund_flow"]) else 0.0
            change_pct = float(row["change_pct"]) if pd.notna(row["change_pct"]) else 0.0

            # 尝试精确匹配
            code = name_map.get((sector_name, sector_type))
            if not code:
                clean = sector_name.replace(" ", "").replace("　", "")
                code = name_map.get((clean, sector_type))

            if code:
                # 精确匹配: 直接更新
                await _update_sector_fund(session, code, sector_name, trade_date, fund_flow, change_pct)
                matched += 1
            elif sector_type == "industry":
                # 行业名匹配一级/二级: 将资金流均匀分配到子行业
                sub_codes = industry_sub_map.get(sector_name, [])
                if sub_codes:
                    per_code_flow = round(fund_flow / len(sub_codes), 2)
                    per_code_pct = round(change_pct, 2)  # 涨跌幅相同
                    for c in sub_codes:
                        await _update_sector_fund(session, c, sector_name, trade_date, per_code_flow, per_code_pct)
                    matched += len(sub_codes)
                else:
                    unmatched_names.append(f"{sector_type}:{sector_name}")
            else:
                unmatched_names.append(f"{sector_type}:{sector_name}")

        await session.commit()

    t2 = time.time()
    logger.info(f"写入完成: 匹配{matched}条, 未匹配{len(unmatched_names)}个, 耗时{t2-t1:.1f}s")

    if unmatched_names:
        logger.warning(f"未匹配板块(前20): {unmatched_names[:20]}")

    # 4. 验证
    async with async_session() as session:
        result = await session.execute(
            select(func.sum(SectorPersistence.fund_flow)).where(
                SectorPersistence.trade_date == trade_date
            )
        )
        total_flow = result.scalar() or 0
        logger.info(f"交易日{trade_date}板块资金净流入总额: {total_flow:.2f}亿")

        result = await session.execute(
            select(func.count()).select_from(SectorPersistence).where(
                SectorPersistence.trade_date == trade_date
            )
        )
        total_records = result.scalar()
        logger.info(f"交易日{trade_date}持续性记录数: {total_records}")

    logger.info(f"总耗时: {time.time()-t0:.1f}s")


async def _update_sector_fund(session, sector_code, fallback_name, trade_date, fund_flow, change_pct):
    """更新单个板块的资金流数据
    
    fallback_name: AkShare传入的板块名，仅在新增记录且无DB名称时使用
    """
    # 先从DB获取正确的sector_name
    si_result = await session.execute(
        select(SectorInfo.sector_name).where(SectorInfo.sector_code == sector_code).limit(1)
    )
    db_name = si_result.scalar_one_or_none()
    sector_name = db_name or fallback_name

    # 更新SectorPersistence
    result = await session.execute(
        select(SectorPersistence).where(
            and_(
                SectorPersistence.sector_code == sector_code,
                SectorPersistence.trade_date == trade_date,
            )
        )
    )
    sp = result.scalar_one_or_none()
    if sp:
        sp.fund_flow = round(fund_flow, 2)
        sp.change_pct = round(change_pct, 2)  # 更新涨幅
        sp.sector_name = sector_name  # 修正为DB名称
        # 根据资金流重新计算连续天数和强度
        if fund_flow > 0:
            sp.consecutive_days = max(sp.consecutive_days or 0, 1)
        else:
            sp.consecutive_days = max((sp.consecutive_days or 0) - 1, 0)
        # 更新强度评分: 基于资金流和涨跌幅
        sp.strength_score = round(max(0, min(100, 50 + change_pct * 5 + min(fund_flow, 50) * 0.5)), 1)
    else:
        # 新增记录
        consecutive = 1 if fund_flow > 0 else 0
        strength = round(max(0, min(100, 50 + change_pct * 5 + min(fund_flow, 50) * 0.5)), 1)
        session.add(SectorPersistence(
            sector_code=sector_code,
            sector_name=sector_name,
            trade_date=trade_date,
            consecutive_days=consecutive,
            limit_up_count=0,
            fund_flow=round(fund_flow, 2),
            change_pct=round(change_pct, 2),
            strength_score=strength,
        ))

    # 同步更新SectorStrength
    result2 = await session.execute(
        select(SectorStrength).where(
            and_(
                SectorStrength.sector_code == sector_code,
                SectorStrength.trade_date == trade_date,
            )
        )
    )
    ss = result2.scalar_one_or_none()
    if ss:
        ss.fund_flow = round(fund_flow, 2)


if __name__ == "__main__":
    asyncio.run(collect_and_save())
