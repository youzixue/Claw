"""解禁预警模块 — 限售股解禁日历+风险评级

核心能力:
- 解禁数据采集(AkShare巨潮资讯)
- 解禁日历查询(未来N天)
- 风险评级(high/medium/low)
- 个股解禁影响评估
- 批量解禁预警
"""

from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.risk import LockupExpiry


# ========== 风险评级 ==========

def calc_lockup_risk_level(unlock_ratio: float, unlock_type: str = "") -> str:
    """计算解禁风险等级

    Args:
        unlock_ratio: 解禁占流通比%
        unlock_type: 解禁类型(首发原股东/定向增发/股权激励)

    Returns:
        high/medium/low
    """
    # 首发原股东解禁 = 高风险(成本极低，抛压大)
    if unlock_type == "首发原股东":
        if unlock_ratio >= 5:
            return "high"
        elif unlock_ratio >= 2:
            return "medium"

    # 定向增发解禁 = 中风险
    if unlock_type == "定向增发":
        if unlock_ratio >= 10:
            return "high"
        elif unlock_ratio >= 5:
            return "medium"

    # 股权激励 = 低风险(通常不会大量抛售)
    if unlock_type == "股权激励":
        if unlock_ratio >= 8:
            return "medium"
        return "low"

    # 通用评级
    if unlock_ratio >= 10:
        return "high"
    elif unlock_ratio >= 3:
        return "medium"
    return "low"


# ========== 解禁管理器 ==========

class LockupManager:
    """解禁预警管理器"""

    # 解禁数据采集(AkShare)
    async def collect_lockup_data(self, session: AsyncSession,
                                   start_date: str = "",
                                   end_date: str = "") -> pd.DataFrame:
        """采集限售股解禁数据

        AkShare接口:
        - ak.stock_restricted_release_queue_sse(date)  上交所解禁
        - ak.stock_restricted_release_queue_szse(date) 深交所解禁
        """
        import akshare as ak
        import asyncio

        try:
            loop = asyncio.get_event_loop()

            if not start_date:
                start_date = date.today().strftime("%Y%m%d")
            if not end_date:
                end_date = (date.today() + timedelta(days=90)).strftime("%Y%m%d")

            # 上交所解禁
            try:
                df_sse = await loop.run_in_executor(
                    None,
                    lambda: ak.stock_restricted_release_queue_sse(date=start_date)
                )
            except Exception:
                df_sse = pd.DataFrame()

            # 深交所解禁
            try:
                df_szse = await loop.run_in_executor(
                    None,
                    lambda: ak.stock_restricted_release_queue_szse(date=start_date)
                )
            except Exception:
                df_szse = pd.DataFrame()

            dfs = []
            if df_sse is not None and not df_sse.empty:
                dfs.append(df_sse)
            if df_szse is not None and not df_szse.empty:
                dfs.append(df_szse)

            if not dfs:
                return pd.DataFrame()

            df = pd.concat(dfs, ignore_index=True)
            logger.info(f"解禁数据采集: {len(df)}条")
            return df

        except Exception as e:
            logger.error(f"解禁数据采集失败: {e}")
            return pd.DataFrame()

    async def save_lockup_data(self, session: AsyncSession,
                                df: pd.DataFrame) -> int:
        """保存解禁数据"""
        count = 0
        for _, row in df.iterrows():
            code = str(row.get("股票代码", row.get("代码", ""))).zfill(6)
            if not code:
                continue

            # 解析日期
            unlock_date_val = row.get("解禁日期", row.get("上市日期", ""))
            try:
                if isinstance(unlock_date_val, str):
                    unlock_date = date.fromisoformat(unlock_date_val.replace("/", "-"))
                elif isinstance(unlock_date_val, date):
                    unlock_date = unlock_date_val
                else:
                    continue
            except (ValueError, TypeError):
                continue

            unlock_volume = float(row.get("解禁数量", row.get("限售解禁数量", 0)) or 0)
            unlock_ratio = float(row.get("解禁比例", row.get("占流通比", 0)) or 0)
            unlock_type = str(row.get("解禁类型", row.get("限售类型", "")) or "")

            risk_level = calc_lockup_risk_level(unlock_ratio, unlock_type)

            lockup = LockupExpiry(
                code=code,
                name=str(row.get("股票简称", row.get("名称", "")) or ""),
                unlock_date=unlock_date,
                unlock_volume=unlock_volume,
                unlock_ratio=unlock_ratio,
                unlock_type=unlock_type,
                risk_level=risk_level,
            )
            session.add(lockup)
            count += 1

        await session.commit()
        logger.info(f"解禁数据保存: {count}条")
        return count

    async def get_upcoming_lockups(self, session: AsyncSession,
                                    days: int = 30,
                                    risk_level: str = None) -> list[dict]:
        """获取未来N天的解禁日历

        Args:
            days: 未来天数
            risk_level: 过滤风险等级(high/medium/low)
        """
        today = date.today()
        end_date = today + timedelta(days=days)

        query = select(LockupExpiry).where(
            and_(
                LockupExpiry.unlock_date >= today,
                LockupExpiry.unlock_date <= end_date,
            )
        ).order_by(LockupExpiry.unlock_date, LockupExpiry.unlock_ratio.desc())

        result = await session.execute(query)
        records = result.scalars().all()

        items = []
        for r in records:
            if risk_level and r.risk_level != risk_level:
                continue
            items.append({
                "code": r.code,
                "name": r.name,
                "unlock_date": str(r.unlock_date),
                "unlock_volume": r.unlock_volume,
                "unlock_ratio": r.unlock_ratio,
                "unlock_type": r.unlock_type,
                "risk_level": r.risk_level,
                "days_until": (r.unlock_date - today).days,
            })

        return items

    async def check_stock_lockup(self, session: AsyncSession,
                                  code: str,
                                  days: int = 30) -> Optional[dict]:
        """检查个股近期是否有解禁"""
        today = date.today()
        end_date = today + timedelta(days=days)

        result = await session.execute(
            select(LockupExpiry).where(
                and_(
                    LockupExpiry.code == code,
                    LockupExpiry.unlock_date >= today,
                    LockupExpiry.unlock_date <= end_date,
                )
            ).order_by(LockupExpiry.unlock_date)
        )
        records = result.scalars().all()

        if not records:
            return None

        # 取最近的一条
        nearest = records[0]
        max_ratio = max(r.unlock_ratio or 0 for r in records)
        has_high = any(r.risk_level == "high" for r in records)

        return {
            "code": code,
            "has_lockup_soon": True,
            "lockup_count": len(records),
            "nearest_date": str(nearest.unlock_date),
            "nearest_ratio": nearest.unlock_ratio,
            "max_ratio": max_ratio,
            "days_until": (nearest.unlock_date - today).days,
            "has_high_risk": has_high,
            "lockup_ratio": max_ratio,  # 供风控规则使用
            "items": [
                {
                    "unlock_date": str(r.unlock_date),
                    "unlock_ratio": r.unlock_ratio,
                    "unlock_type": r.unlock_type,
                    "risk_level": r.risk_level,
                }
                for r in records
            ],
        }

    async def get_lockup_risk_stocks(self, session: AsyncSession,
                                      days: int = 7) -> list[dict]:
        """获取近期高风险解禁股票列表"""
        lockups = await self.get_upcoming_lockups(session, days, risk_level="high")
        return lockups

    async def get_lockup_calendar(self, session: AsyncSession,
                                   start_date: date = None,
                                   end_date: date = None) -> dict:
        """获取解禁日历(按日期分组)"""
        today = date.today()
        if not start_date:
            start_date = today
        if not end_date:
            end_date = today + timedelta(days=30)

        result = await session.execute(
            select(LockupExpiry).where(
                and_(
                    LockupExpiry.unlock_date >= start_date,
                    LockupExpiry.unlock_date <= end_date,
                )
            ).order_by(LockupExpiry.unlock_date)
        )
        records = result.scalars().all()

        # 按日期分组
        calendar = {}
        for r in records:
            d = str(r.unlock_date)
            if d not in calendar:
                calendar[d] = {
                    "date": d,
                    "count": 0,
                    "high_risk_count": 0,
                    "total_volume": 0,
                    "items": [],
                }
            calendar[d]["count"] += 1
            calendar[d]["total_volume"] += r.unlock_volume or 0
            if r.risk_level == "high":
                calendar[d]["high_risk_count"] += 1
            calendar[d]["items"].append({
                "code": r.code,
                "name": r.name,
                "unlock_ratio": r.unlock_ratio,
                "risk_level": r.risk_level,
            })

        return {
            "start_date": str(start_date),
            "end_date": str(end_date),
            "total_days": len(calendar),
            "calendar": list(calendar.values()),
        }


# 全局
lockup_manager = LockupManager()
