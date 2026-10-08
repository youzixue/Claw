"""板块轮动引擎 — 轮动检测 + 强弱排名 + 持续性分析 + 资金流向矩阵

核心能力:
- 板块强弱排名: 综合涨幅/资金/涨停家数/持续性
- 板块轮动检测: 资金从冷→热切换信号
- 板块持续性分析: 连续活跃天数+衰退预警
- 资金流向矩阵: 板块间资金流向桑基图数据
- 板块共振检测: 多板块同向联动
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_, func, desc, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sector import SectorRotation, SectorStrength
from app.models.stock import (
    SectorInfo, StockSectorMapping, SectorPersistence,
    FundFlow, LimitUpPool, StockDaily,
)


# ========== 数据结构 ==========

@dataclass
class SectorRankItem:
    """板块排名项"""
    sector_code: str
    sector_name: str
    sector_type: str = ""
    rank: int = 0
    rank_change: int = 0
    strength_score: float = 0
    change_pct: float = 0           # 板块涨幅%
    fund_flow: float = 0            # 资金净流入(亿)
    limit_up_count: int = 0         # 涨停家数
    consecutive_days: int = 0       # 连续活跃天数
    stock_count: int = 0            # 成分股数
    is_hot: bool = False            # 是否热门


@dataclass
class RotationSignal:
    """轮动信号"""
    from_sector: str
    to_sector: str
    from_name: str = ""
    to_name: str = ""
    flow_amount: float = 0          # 流向金额(亿)
    rotation_type: str = "gradual"  # gradual/sudden
    confidence: float = 0           # 信号置信度


# ========== 辅助函数 ==========

async def _get_latest_trade_date(session: AsyncSession, table_class, date_col) -> date:
    """获取指定表中最近的交易日期"""
    result = await session.execute(
        select(func.max(date_col))
    )
    latest = result.scalar()
    return latest if latest else date.today()


async def _get_latest_sector_persistence_date(session: AsyncSession) -> date:
    """获取SectorPersistence表最近有数据的日期"""
    return await _get_latest_trade_date(session, SectorPersistence, SectorPersistence.trade_date)


async def _get_latest_sector_strength_date(session: AsyncSession) -> date:
    """获取SectorStrength表最近有数据的日期"""
    return await _get_latest_trade_date(session, SectorStrength, SectorStrength.trade_date)


async def _get_latest_limit_up_date(session: AsyncSession) -> date:
    """获取LimitUpPool表最近有数据的日期"""
    return await _get_latest_trade_date(session, LimitUpPool, LimitUpPool.trade_date)


async def _get_latest_rotation_date(session: AsyncSession) -> date:
    """获取SectorRotation表最近有数据的日期"""
    return await _get_latest_trade_date(session, SectorRotation, SectorRotation.trade_date)


# ========== 板块轮动引擎 ==========

class SectorRotationEngine:
    """板块轮动引擎"""

    # 强弱评分权重
    SCORE_WEIGHTS = {
        "change_pct": 0.25,         # 涨幅权重
        "fund_flow": 0.30,          # 资金流权重
        "limit_up_count": 0.20,     # 涨停家数权重
        "consecutive_days": 0.15,   # 持续性权重
        "rank_change": 0.10,        # 排名变化权重
    }

    # 持续性阈值
    HOT_CONSECUTIVE_DAYS = 3        # 连续3天活跃=热门
    DECLINE_THRESHOLD = -5.0        # 评分下降>5=衰退

    async def calc_sector_strength(self, session: AsyncSession,
                                    trade_date: date = None) -> list[SectorRankItem]:
        """计算板块强弱排名

        评分维度:
        1. 板块涨幅(25%)
        2. 资金净流入(30%)
        3. 涨停家数(20%)
        4. 持续性(15%)
        5. 排名变化(10%)

        注意: 只有有SectorPersistence数据的板块才参与排名，
        空板块不参与(避免几百个0分板块淹没有效数据)
        """
        # 回退到最近有数据的交易日
        if trade_date is None:
            trade_date = await _get_latest_sector_persistence_date(session)

        # 获取板块资金流(含涨幅)
        sector_funds = await self._get_sector_fund_flow(session, trade_date)

        # 如果没有资金流数据，直接返回空
        if not sector_funds:
            return []

        # 获取涨停分布
        limit_up_dist = await self._get_limit_up_distribution(session, trade_date)

        # 获取昨日排名 — 回退到最近有数据的日期
        prev_ranks = await self._get_prev_ranks(session, trade_date)

        # 获取持续性
        persistences = await self._get_persistences(session, trade_date)

        # 只对有SectorPersistence数据的板块排名
        # 查询这些板块的SectorInfo
        sector_codes = list(sector_funds.keys())
        sectors_result = await session.execute(
            select(SectorInfo).where(SectorInfo.sector_code.in_(sector_codes))
        )
        sectors_map = {s.sector_code: s for s in sectors_result.scalars().all()}

        # 计算评分
        items = []
        for code in sector_codes:
            sector = sectors_map.get(code)
            name = sector.sector_name if sector else code
            sector_type = sector.sector_type if sector else ""

            # 从SectorPersistence获取已有数据
            fund_data = sector_funds.get(code, {})
            change_pct = fund_data.get("change_pct", 0)
            fund_flow = fund_data.get("fund_flow", 0)
            saved_strength = fund_data.get("strength_score", 0)
            consecutive_days = fund_data.get("consecutive_days", 0)
            limit_up_count = fund_data.get("limit_up_count", 0)
            stock_count = sector.stock_count if sector else 0

            # 如果SectorPersistence已有强度评分(采集脚本计算), 直接使用
            # 否则用公式计算
            if saved_strength > 0:
                strength_score = saved_strength
            else:
                # 各维度评分(0-100)
                change_score = min(max(change_pct * 10, 0), 100) if change_pct > 0 else max(change_pct * 10, -50)
                fund_score = min(max(fund_flow * 5, 0), 100) if fund_flow > 0 else max(fund_flow * 5, -50)
                limit_score = min(limit_up_count * 20, 100)
                persist_score = min(consecutive_days * 20, 100)
                rank_score = 50

                strength_score = (
                    change_score * self.SCORE_WEIGHTS["change_pct"]
                    + fund_score * self.SCORE_WEIGHTS["fund_flow"]
                    + limit_score * self.SCORE_WEIGHTS["limit_up_count"]
                    + persist_score * self.SCORE_WEIGHTS["consecutive_days"]
                    + rank_score * self.SCORE_WEIGHTS["rank_change"]
                )

            strength_score = max(0, min(100, strength_score))

            items.append(SectorRankItem(
                sector_code=code,
                sector_name=name,
                sector_type=sector_type,
                strength_score=round(strength_score, 1),
                change_pct=round(change_pct, 2),
                fund_flow=round(fund_flow, 2),
                limit_up_count=limit_up_count,
                consecutive_days=consecutive_days,
                stock_count=stock_count or 0,
                is_hot=consecutive_days >= self.HOT_CONSECUTIVE_DAYS or fund_flow > 5,
            ))

        # 按评分排序
        items.sort(key=lambda x: x.strength_score, reverse=True)

        # 设置排名和排名变化
        for i, item in enumerate(items, 1):
            item.rank = i
            prev = prev_ranks.get(item.sector_code, 0)
            item.rank_change = prev - i if prev > 0 else 0  # 正=上升

        return items

    async def save_strength_ranking(self, session: AsyncSession,
                                     items: list[SectorRankItem],
                                     trade_date: date) -> int:
        """保存板块强弱排名；批量读取既有身份，保留输入顺序及原提交边界。"""
        codes = list(dict.fromkeys(item.sector_code for item in items))
        existing_by_code = {}
        # Bound bind variables without dropping identities or changing ranking.
        for offset in range(0, len(codes), 500):
            existing = await session.execute(
                select(SectorStrength).where(
                    SectorStrength.trade_date == trade_date,
                    SectorStrength.sector_code.in_(codes[offset:offset + 500]),
                )
            )
            existing_by_code.update((row.sector_code, row) for row in existing.scalars().all())

        count = 0
        for item in items:
            row = existing_by_code.get(item.sector_code)
            if row:
                row.rank = item.rank
                row.rank_change = item.rank_change
                row.strength_score = item.strength_score
                row.consecutive_days = item.consecutive_days
                row.fund_flow = item.fund_flow
                row.sector_type = item.sector_type
                row.change_pct = getattr(item, "change_pct", None)
                row.limit_up_count = getattr(item, "limit_up_count", None)
            else:
                row = SectorStrength(
                    trade_date=trade_date,
                    sector_code=item.sector_code,
                    sector_name=item.sector_name,
                    sector_type=item.sector_type,
                    rank=item.rank,
                    rank_change=item.rank_change,
                    strength_score=item.strength_score,
                    change_pct=getattr(item, "change_pct", None),
                    fund_flow=item.fund_flow,
                    limit_up_count=getattr(item, "limit_up_count", None),
                    consecutive_days=item.consecutive_days,
                    is_hot=1 if (
                        (item.fund_flow or 0) > 10
                        or (item.consecutive_days or 0) >= 3
                    ) else 0,
                )
                session.add(row)
                # A repeated new identity must update this first row, even when
                # the caller disables autoflush. Preserve its first name/is_hot.
                existing_by_code[item.sector_code] = row
            count += 1

        await session.commit()
        logger.info(f"板块强弱排名保存: {count}条, {trade_date}")
        return count

    async def detect_rotation(self, session: AsyncSession,
                               trade_date: date = None) -> list[RotationSignal]:
        """检测板块轮动信号

        轮动判定:
        1. 资金从冷→热: 前日冷门板块今日资金大量流入
        2. 资金从热→冷: 前日热门板块今日资金大幅流出
        3. 突然切换: 单日资金方向剧变
        """
        if trade_date is None:
            trade_date = await _get_latest_sector_persistence_date(session)

        # 获取两日排名
        today_items = await self.calc_sector_strength(session, trade_date)

        # 获取昨日排名 — 回退到最近有数据的日期
        latest_prev_date = await _get_latest_sector_strength_date(session)
        if latest_prev_date and latest_prev_date >= trade_date:
            # 昨日数据等于或晚于今日，不比较
            prev_date = trade_date - timedelta(days=1)
        else:
            prev_date = latest_prev_date or (trade_date - timedelta(days=1))

        prev_result = await session.execute(
            select(SectorStrength).where(SectorStrength.trade_date == prev_date)
        )
        prev_records = prev_result.scalars().all()
        prev_map = {r.sector_code: r for r in prev_records}

        signals = []

        for item in today_items:
            prev = prev_map.get(item.sector_code)
            if not prev:
                continue

            # 排名大幅上升 = 资金流入
            rank_rise = prev.rank - item.rank
            fund_change = item.fund_flow - (prev.fund_flow or 0)

            # 资金大幅流入且排名上升 → 从其他板块流入
            if rank_rise >= 5 and fund_change > 0:
                signals.append(RotationSignal(
                    from_sector="其他",
                    to_sector=item.sector_code,
                    to_name=item.sector_name,
                    flow_amount=fund_change,
                    rotation_type="gradual" if rank_rise < 10 else "sudden",
                    confidence=min(rank_rise / 10, 1.0),
                ))

            # 资金大幅流出且排名下降 → 流出到其他板块
            if rank_rise <= -5 and fund_change < 0:
                signals.append(RotationSignal(
                    from_sector=item.sector_code,
                    from_name=item.sector_name,
                    to_sector="其他",
                    flow_amount=abs(fund_change),
                    rotation_type="gradual" if rank_rise > -10 else "sudden",
                    confidence=min(abs(rank_rise) / 10, 1.0),
                ))

        # 保存轮动信号(用 merge 避免重复)
        for signal in signals:
            existing = await session.execute(
                select(SectorRotation).where(
                    and_(
                        SectorRotation.trade_date == trade_date,
                        SectorRotation.from_sector == signal.from_sector,
                        SectorRotation.to_sector == signal.to_sector,
                    )
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.flow_amount = signal.flow_amount
                row.rotation_type = signal.rotation_type
            else:
                session.add(SectorRotation(
                    trade_date=trade_date,
                    from_sector=signal.from_sector,
                    to_sector=signal.to_sector,
                    flow_amount=signal.flow_amount,
                    rotation_type=signal.rotation_type,
                ))

        await session.commit()
        logger.info(f"板块轮动信号: {len(signals)}个, {trade_date}")

        return signals

    async def get_persistence_analysis(self, session: AsyncSession,
                                        trade_date: date = None,
                                        min_days: int = 2) -> list[dict]:
        """板块持续性分析

        返回连续活跃N天以上的板块
        """
        if trade_date is None:
            trade_date = await _get_latest_sector_persistence_date(session)

        result = await session.execute(
            select(SectorPersistence).where(
                and_(
                    SectorPersistence.trade_date == trade_date,
                    SectorPersistence.consecutive_days >= min_days,
                )
            ).order_by(SectorPersistence.consecutive_days.desc())
        )
        records = result.scalars().all()

        return [
            {
                "sector_code": r.sector_code,
                "sector_name": r.sector_name,
                "consecutive_days": r.consecutive_days or 0,
                "limit_up_count": r.limit_up_count or 0,
                "fund_flow": round(r.fund_flow or 0, 2),
                "strength_score": round(r.strength_score or 0, 1),
                "is_declining": (r.strength_score or 0) < 50,
            }
            for r in records
        ]

    async def get_fund_flow_matrix(self, session: AsyncSession,
                                    trade_date: date = None) -> dict:
        """资金流向矩阵(桑基图数据)

        Returns:
            {
                "nodes": [{name: "半导体"}, {name: "新能源"}, ...],
                "links": [{source: "半导体", target: "新能源", value: 5.2}, ...],
            }
        """
        if trade_date is None:
            trade_date = await _get_latest_rotation_date(session)

        result = await session.execute(
            select(SectorRotation).where(SectorRotation.trade_date == trade_date)
        )
        rotations = result.scalars().all()

        # 收集所有板块 — 使用名称而非代码
        sector_names = set()
        links = []
        for r in rotations:
            from_name = r.from_sector if r.from_sector != "其他" else "其他"
            to_name = r.to_sector if r.to_sector != "其他" else "其他"

            # 尝试从SectorInfo获取中文名称
            if r.from_sector != "其他":
                si_result = await session.execute(
                    select(SectorInfo.sector_name).where(
                        SectorInfo.sector_code == r.from_sector
                    ).limit(1)
                )
                si_name = si_result.scalar_one_or_none()
                if si_name:
                    from_name = si_name

            if r.to_sector != "其他":
                si_result = await session.execute(
                    select(SectorInfo.sector_name).where(
                        SectorInfo.sector_code == r.to_sector
                    ).limit(1)
                )
                si_name = si_result.scalar_one_or_none()
                if si_name:
                    to_name = si_name

            if from_name != "其他":
                sector_names.add(from_name)
            if to_name != "其他":
                sector_names.add(to_name)

            links.append({
                "source": from_name,
                "target": to_name,
                "value": round(r.flow_amount or 0, 2),
                "type": r.rotation_type or "gradual",
            })

        nodes = [{"name": name} for name in sorted(sector_names)]

        return {
            "trade_date": str(trade_date),
            "nodes": nodes,
            "links": links,
            "total_flow": round(sum(r.flow_amount or 0 for r in rotations), 2),
        }

    async def get_sector_detail(self, session: AsyncSession,
                                 sector_code: str,
                                 trade_date: date = None) -> dict:
        """获取板块详情(成分股+资金流+涨停)"""
        if trade_date is None:
            trade_date = await _get_latest_sector_persistence_date(session)

        # 板块信息 — sector_code不是主键，需要用where查询
        sector_result = await session.execute(
            select(SectorInfo).where(SectorInfo.sector_code == sector_code).limit(1)
        )
        sector_info = sector_result.scalar_one_or_none()

        # 成分股
        result = await session.execute(
            select(func.count()).select_from(StockSectorMapping).where(
                StockSectorMapping.sector_code == sector_code
            )
        )
        member_count = result.scalar() or 0

        # 持续性
        result = await session.execute(
            select(SectorPersistence).where(
                and_(
                    SectorPersistence.sector_code == sector_code,
                    SectorPersistence.trade_date == trade_date,
                )
            )
        )
        persistence = result.scalar_one_or_none()

        return {
            "sector_code": sector_code,
            "sector_name": sector_info.sector_name if sector_info else "",
            "sector_type": sector_info.sector_type if sector_info else "",
            "member_count": member_count,
            "consecutive_days": persistence.consecutive_days if persistence else 0,
            "strength_score": round(persistence.strength_score or 0, 1) if persistence else 0,
            "fund_flow": round(persistence.fund_flow or 0, 2) if persistence else 0,
            "limit_up_count": persistence.limit_up_count if persistence else 0,
        }

    # ========== 内部方法 ==========

    async def _get_sectors(self, session: AsyncSession) -> list:
        """获取板块列表 — 优先取有持续性数据的板块，再补其余板块"""
        # 先获取有持续性数据的板块(有资金流+强度评分)
        result = await session.execute(
            select(SectorInfo).limit(500)
        )
        return result.scalars().all()

    async def _get_sector_fund_flow(self, session: AsyncSession,
                                     trade_date: date) -> dict:
        """获取板块资金流(含涨幅)

        直接从SectorPersistence获取已采集的资金流和强度评分
        涨幅从强度评分反推或从采集脚本写入
        """
        # 从SectorPersistence获取资金流
        result = await session.execute(
            select(SectorPersistence).where(
                SectorPersistence.trade_date == trade_date
            )
        )
        records = result.scalars().all()

        data = {}
        for r in records:
            data[r.sector_code] = {
                "fund_flow": r.fund_flow or 0,
                "change_pct": r.change_pct or 0,  # 从SectorPersistence获取涨幅
                "strength_score": r.strength_score or 0,
                "consecutive_days": r.consecutive_days or 0,
                "limit_up_count": r.limit_up_count or 0,
            }

        return data

    async def _get_limit_up_distribution(self, session: AsyncSession,
                                          trade_date: date) -> dict:
        """获取涨停股板块分布

        每只涨停股可能属于多个板块，统计每个板块的涨停数
        """
        # 回退到最近有涨停数据的日期
        limit_up_result = await session.execute(
            select(LimitUpPool).where(LimitUpPool.trade_date == trade_date)
        )
        limit_ups = limit_up_result.scalars().all()

        if not limit_ups:
            # 回退到最近有数据的日期
            latest_date = await _get_latest_limit_up_date(session)
            if latest_date != trade_date:
                limit_up_result = await session.execute(
                    select(LimitUpPool).where(LimitUpPool.trade_date == latest_date)
                )
                limit_ups = limit_up_result.scalars().all()

        # 统计每个板块的涨停数
        # 为了效率，一次性查出所有涨停股的板块映射
        if not limit_ups:
            return {}

        codes = [lu.code for lu in limit_ups]
        mapping_result = await session.execute(
            select(StockSectorMapping.sector_code, StockSectorMapping.code).where(
                StockSectorMapping.code.in_(codes)
            )
        )
        mappings = mapping_result.all()

        # 建立code→sector_codes映射
        code_sectors = {}
        for m in mappings:
            if m.code not in code_sectors:
                code_sectors[m.code] = []
            code_sectors[m.code].append(m.sector_code)

        # 统计每个板块的涨停数
        dist = {}
        for lu in limit_ups:
            sector_codes = code_sectors.get(lu.code, [])
            for sc in sector_codes:
                dist[sc] = dist.get(sc, 0) + 1

        return dist

    async def _get_prev_ranks(self, session: AsyncSession,
                               trade_date: date) -> dict:
        """获取前一日排名 — 回退到最近有数据的日期"""
        # 尝试获取trade_date之前最近的一天
        result = await session.execute(
            select(SectorStrength).where(
                SectorStrength.trade_date < trade_date
            ).order_by(desc(SectorStrength.trade_date)).limit(500)
        )
        records = result.scalars().all()

        if not records:
            return {}

        # 取最近日期的所有记录
        latest_date = records[0].trade_date
        return {r.sector_code: r.rank for r in records if r.trade_date == latest_date}

    async def _get_persistences(self, session: AsyncSession,
                                 trade_date: date) -> dict:
        """获取板块持续性"""
        result = await session.execute(
            select(SectorPersistence).where(SectorPersistence.trade_date == trade_date)
        )
        records = result.scalars().all()

        # 如果当天没数据，回退到最近有数据的日期
        if not records:
            latest_date = await _get_latest_sector_persistence_date(session)
            if latest_date != trade_date:
                result = await session.execute(
                    select(SectorPersistence).where(SectorPersistence.trade_date == latest_date)
                )
                records = result.scalars().all()

        return {r.sector_code: r.consecutive_days or 0 for r in records}


# 全局
sector_rotation_engine = SectorRotationEngine()
