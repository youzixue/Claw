"""板块生命周期状态机 — 判断板块所处阶段

生命周期状态定义:
1. DORMANT(休眠): 无涨停,资金流出或无流向
2. EMERGING(刚启动): 首板出现(1-2只),资金初进
3. ACCELERATING(加速): 连板梯队形成,资金持续
4. CLIMAX(高潮): 批量涨停,龙头高度>5板
5. DIVERGING(分化): 掉队股增多,后排开始跌
6. DECLINING(退潮): 板块达到高点后持续回调(龙头断板/梯队瓦解/K线破位/资金出逃)
7. ONE_DAY(一日游): 当天涨停,次日无持续
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Optional, List, Dict
import json

from sqlalchemy import select, and_, or_, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sector import SectorLifecycle, SectorKline
from app.models.stock import (
    LimitUpPool,
    LimitDownPool,
    SectorPersistence,
    StockSectorMapping,
    StockTag,
    StockBlacklist,
)


class LifecycleState(str, Enum):
    """板块生命周期状态"""
    DORMANT = "dormant"
    EMERGING = "emerging"
    ACCELERATING = "accelerating"
    CLIMAX = "climax"
    DIVERGING = "diverging"
    DECLINING = "declining"
    ONE_DAY = "one_day"


@dataclass
class SectorLifecycleData:
    """板块生命周期数据"""
    sector_code: str
    sector_name: str
    sector_type: str
    trade_date: date
    
    lifecycle_state: LifecycleState = LifecycleState.DORMANT
    prev_state: Optional[LifecycleState] = None
    state_change: str = "unchanged"
    
    limit_up_count: int = 0
    first_board_count: int = 0
    consecutive_board_count: int = 0
    max_board_height: int = 0
    limit_down_count: int = 0
    raw_limit_up_count: int = 0
    raw_first_board_count: int = 0
    raw_consecutive_board_count: int = 0
    raw_max_board_height: int = 0
    
    ladders: List[Dict] = field(default_factory=list)
    leader_stocks: List[Dict] = field(default_factory=list)
    raw_ladders: List[Dict] = field(default_factory=list)
    raw_leader_stocks: List[Dict] = field(default_factory=list)
    attributed_reason_samples: List[str] = field(default_factory=list)
    raw_reason_samples: List[str] = field(default_factory=list)
    attribution_confidence: str = "none"
    attribution_confidence_score: int = 0
    
    fund_flow: float = 0
    fund_flow_3d: float = 0
    strength_score: float = 0
    change_pct: float = 0
    active_days: int = 0
    total_active_5d: int = 0
    persistence_consecutive_days: int = 0
    quality_score: float = 0
    main_line_status: str = "none"
    is_main_line: bool = False

    # K线技术因子(从SectorKline读取)
    kline_trend: Optional[str] = None      # up/down/sideways/breakout_up/breakout_down
    kline_vol_ratio: Optional[float] = None  # 量比
    kline_support: Optional[float] = None   # 支撑位
    kline_resistance: Optional[float] = None  # 压力位
    kline_ma5: Optional[float] = None       # 5日均线
    kline_ma20: Optional[float] = None      # 20日均线
    kline_close: Optional[float] = None     # 当日收盘指数


class SectorLifecycleEngine:
    """板块生命周期引擎"""
    
    def __init__(self):
        self.state_scores = {
            LifecycleState.DORMANT: 0,
            LifecycleState.ONE_DAY: 10,
            LifecycleState.DECLINING: 20,
            LifecycleState.DIVERGING: 40,
            LifecycleState.EMERGING: 60,
            LifecycleState.ACCELERATING: 80,
            LifecycleState.CLIMAX: 100,
        }
    
    async def analyze_sector(
        self,
        session: AsyncSession,
        sector_code: str,
        sector_name: str,
        sector_type: str,
        trade_date: date,
        prev_snapshot: Optional["SectorLifecycleData"] = None,
        recent_history: Optional[List["SectorLifecycleData"]] = None,
    ) -> SectorLifecycleData:
        """分析单个板块的生命周期状态"""
        
        data = SectorLifecycleData(
            sector_code=sector_code,
            sector_name=sector_name,
            sector_type=sector_type,
            trade_date=trade_date,
        )
        
        # 获取K线技术因子
        await self._fetch_kline_factors(session, data)
        
        # 获取资金数据
        await self._fetch_fund_flow(session, data)

        # 获取涨停/跌停数据
        await self._fetch_limit_up_data(session, data)
        await self._fetch_limit_down_data(session, data)
        
        # 获取历史状态
        prev_data = None if prev_snapshot else await self._fetch_prev_state(session, sector_code, trade_date)
        if prev_snapshot:
            data.prev_state = prev_snapshot.lifecycle_state
            if prev_snapshot.lifecycle_state in {
                LifecycleState.EMERGING,
                LifecycleState.ACCELERATING,
                LifecycleState.CLIMAX,
            }:
                data.active_days = prev_snapshot.active_days or 0
        elif prev_data:
            data.prev_state = LifecycleState(prev_data.lifecycle_state)
            # 仅继承正向活跃周期，避免分化/退潮/一日游污染 active_days。
            if prev_data.lifecycle_state in {
                LifecycleState.EMERGING,
                LifecycleState.ACCELERATING,
                LifecycleState.CLIMAX,
            }:
                data.active_days = prev_data.active_days or 0

        if data.persistence_consecutive_days > 0:
            data.active_days = max(data.active_days, max(data.persistence_consecutive_days - 1, 0))
        
        # 计算近5天活跃度
        if recent_history is not None:
            data.total_active_5d = sum(1 for item in recent_history[-5:] if item.limit_up_count > 0)
        else:
            data.total_active_5d = await self._calc_active_5d(session, sector_code, trade_date)
        
        # 判断当前状态
        data.lifecycle_state = self._determine_state(data)
        
        # 判断状态变化
        if data.prev_state is None:
            data.state_change = "new"
        elif self.state_scores[data.lifecycle_state] > self.state_scores[data.prev_state]:
            data.state_change = "upgraded"
        elif self.state_scores[data.lifecycle_state] < self.state_scores[data.prev_state]:
            data.state_change = "downgraded"
        else:
            data.state_change = "unchanged"
        
        # 计算质量分
        data.quality_score = self._calc_quality_score(data)
        
        # 更新活跃天数
        if data.lifecycle_state in [LifecycleState.EMERGING, LifecycleState.ACCELERATING, LifecycleState.CLIMAX]:
            data.active_days += 1
        else:
            data.active_days = 0

        # 判断主线档位
        data.main_line_status = self._main_line_status(data)
        data.is_main_line = data.main_line_status != "none"
        
        return data

    async def analyze_sectors_batch(
        self,
        session: AsyncSession,
        sectors: List[Dict],
        trade_date: date,
        prev_snapshot_map: Optional[Dict[str, "SectorLifecycleData"]] = None,
        recent_history_map: Optional[Dict[str, List["SectorLifecycleData"]]] = None,
    ) -> Dict[str, SectorLifecycleData]:
        """批量分析板块生命周期，避免实时接口走逐板块 N+1 查询。"""
        if not sectors:
            return {}

        sector_codes = [item["sector_code"] for item in sectors]
        sector_meta = {item["sector_code"]: item for item in sectors}

        kline_rows = (
            await session.execute(
                select(SectorKline).where(
                    and_(
                        SectorKline.trade_date == trade_date,
                        SectorKline.sector_code.in_(sector_codes),
                    )
                )
            )
        ).scalars().all()
        kline_map = {row.sector_code: row for row in kline_rows}

        persistence_rows = (
            await session.execute(
                select(SectorPersistence).where(
                    and_(
                        SectorPersistence.sector_code.in_(sector_codes),
                        SectorPersistence.trade_date >= trade_date - timedelta(days=5),
                        SectorPersistence.trade_date <= trade_date,
                    )
                )
            )
        ).scalars().all()
        persistence_by_code: Dict[str, List[SectorPersistence]] = {}
        for row in persistence_rows:
            persistence_by_code.setdefault(row.sector_code, []).append(row)
        for rows in persistence_by_code.values():
            rows.sort(key=lambda item: item.trade_date)

        prev_rows = (
            await session.execute(
                select(SectorLifecycle).where(
                    and_(
                        SectorLifecycle.sector_code.in_(sector_codes),
                        # 状态机只需要最近状态和近5个活跃样本。旧实现把这些
                        # 板块的全部历史生命周期一次性读入，运行越久越慢。
                        SectorLifecycle.trade_date >= trade_date - timedelta(days=14),
                        SectorLifecycle.trade_date < trade_date,
                    )
                ).order_by(SectorLifecycle.sector_code, desc(SectorLifecycle.trade_date))
            )
        ).scalars().all()
        prev_map: Dict[str, SectorLifecycle] = {}
        recent_lifecycle_by_code: Dict[str, List[SectorLifecycle]] = {}
        for row in prev_rows:
            if row.sector_code not in prev_map:
                prev_map[row.sector_code] = row
            recent_lifecycle_by_code.setdefault(row.sector_code, [])
            if len(recent_lifecycle_by_code[row.sector_code]) < 5:
                recent_lifecycle_by_code[row.sector_code].append(row)

        mapping_rows = (
            await session.execute(
                select(StockSectorMapping.sector_code, StockSectorMapping.code).where(
                    StockSectorMapping.sector_code.in_(sector_codes)
                )
            )
        ).all()
        stock_to_sectors: Dict[str, List[str]] = {}
        for sector_code, code in mapping_rows:
            stock_to_sectors.setdefault(code, []).append(sector_code)

        stock_codes = list(stock_to_sectors.keys())
        blocked_codes = await self._load_blocked_stock_codes(session, stock_codes, trade_date)
        limit_up_rows = []
        limit_down_rows = []
        if stock_codes:
            limit_up_rows = (
                await session.execute(
                    select(LimitUpPool).where(
                        and_(
                            LimitUpPool.trade_date == trade_date,
                            LimitUpPool.code.in_(stock_codes),
                        )
                    )
                )
            ).scalars().all()
            limit_up_rows = [
                row for row in limit_up_rows
                if not self._is_excluded_stock(row.code, row.name, blocked_codes)
            ]
            limit_down_rows = (
                await session.execute(
                    select(LimitDownPool).where(
                        and_(
                            LimitDownPool.trade_date == trade_date,
                            LimitDownPool.code.in_(stock_codes),
                        )
                    )
                )
            ).scalars().all()
            limit_down_rows = [
                row for row in limit_down_rows
                if not self._is_excluded_stock(row.code, row.name, blocked_codes)
            ]

        limit_ups_by_sector: Dict[str, List[LimitUpPool]] = {code: [] for code in sector_codes}
        limit_downs_by_sector: Dict[str, List[LimitDownPool]] = {code: [] for code in sector_codes}
        for row in limit_up_rows:
            for sector_code in stock_to_sectors.get(row.code, []):
                limit_ups_by_sector.setdefault(sector_code, []).append(row)
        for row in limit_down_rows:
            for sector_code in stock_to_sectors.get(row.code, []):
                limit_downs_by_sector.setdefault(sector_code, []).append(row)

        analyzed: Dict[str, SectorLifecycleData] = {}
        for sector_code in sector_codes:
            meta = sector_meta[sector_code]
            data = SectorLifecycleData(
                sector_code=sector_code,
                sector_name=meta["sector_name"],
                sector_type=meta["sector_type"],
                trade_date=trade_date,
            )

            kline = kline_map.get(sector_code)
            if kline:
                data.kline_trend = kline.trend_state
                data.kline_vol_ratio = kline.vol_ratio
                data.kline_support = kline.support_price
                data.kline_resistance = kline.resistance_price
                data.kline_ma5 = kline.ma5
                data.kline_ma20 = kline.ma20
                data.kline_close = kline.close

            persisted_rows = persistence_by_code.get(sector_code, [])
            today_persist = persisted_rows[-1] if persisted_rows and persisted_rows[-1].trade_date == trade_date else None
            if today_persist:
                data.fund_flow = today_persist.fund_flow or 0
                data.persistence_consecutive_days = today_persist.consecutive_days or 0
                data.strength_score = today_persist.strength_score or 0
                data.change_pct = today_persist.change_pct or 0
            data.fund_flow_3d = sum((row.fund_flow or 0) for row in persisted_rows[-3:])

            prev_snapshot = (prev_snapshot_map or {}).get(sector_code)
            if prev_snapshot:
                data.prev_state = prev_snapshot.lifecycle_state
                if prev_snapshot.lifecycle_state in {
                    LifecycleState.EMERGING,
                    LifecycleState.ACCELERATING,
                    LifecycleState.CLIMAX,
                }:
                    data.active_days = prev_snapshot.active_days or 0
            else:
                prev_data = prev_map.get(sector_code)
                if prev_data:
                    data.prev_state = LifecycleState(prev_data.lifecycle_state)
                    if prev_data.lifecycle_state in {
                        LifecycleState.EMERGING,
                        LifecycleState.ACCELERATING,
                        LifecycleState.CLIMAX,
                    }:
                        data.active_days = prev_data.active_days or 0

            if recent_history_map and sector_code in recent_history_map:
                data.total_active_5d = sum(
                    1 for row in recent_history_map.get(sector_code, [])[-5:]
                    if (row.limit_up_count or 0) > 0
                )
            else:
                lifecycle_history_total = sum(
                    1 for row in recent_lifecycle_by_code.get(sector_code, [])[:5]
                    if (row.limit_up_count or 0) > 0
                )
                persistence_history_total = sum(
                    1 for row in persisted_rows[-5:]
                    if (row.limit_up_count or 0) > 0 or (row.fund_flow or 0) > 0
                )
                data.total_active_5d = max(lifecycle_history_total, persistence_history_total)

            raw_limit_ups = limit_ups_by_sector.get(sector_code, [])
            raw_height_map: Dict[int, List[Dict]] = {}
            for lu in raw_limit_ups:
                height = lu.consecutive_days or 1
                raw_height_map.setdefault(height, []).append({
                    "code": lu.code,
                    "name": lu.name,
                    "seal_amount": lu.seal_amount or 0,
                    "height": height,
                })
            data.raw_limit_up_count = len(raw_limit_ups)
            data.raw_reason_samples = sorted({
                (lu.limit_up_reason or "").strip()
                for lu in raw_limit_ups
                if (lu.limit_up_reason or "").strip()
            })[:5]
            data.raw_consecutive_board_count = sum(len(stocks) for h, stocks in raw_height_map.items() if h >= 2)
            data.raw_first_board_count = len(raw_height_map.get(1, []))
            data.raw_max_board_height = max(raw_height_map.keys()) if raw_height_map else 0
            data.raw_ladders = [
                {"height": height, "stocks": raw_height_map[height]}
                for height in sorted(raw_height_map.keys(), reverse=True)
            ]
            if data.raw_max_board_height >= 1 and raw_height_map:
                top_stocks = sorted(raw_height_map.get(data.raw_max_board_height, []), key=lambda x: x["seal_amount"], reverse=True)
                data.raw_leader_stocks = top_stocks[:1]

            if data.sector_type == "industry":
                exact_limit_ups = raw_limit_ups
                attribution_score = 4 if raw_limit_ups else 0
            else:
                exact_limit_ups, attribution_score = self._filter_attributed_entries_with_meta(
                    raw_limit_ups,
                    data.sector_name,
                    lambda lu: lu.limit_up_reason,
                    allow_blank_mapped=True,
                    sector_strength=data.strength_score,
                    sector_fund_flow=data.fund_flow,
                )
            exact_limit_ups = list({lu.code: lu for lu in exact_limit_ups}.values())
            data.attribution_confidence_score = attribution_score
            data.attribution_confidence = self._attribution_confidence_from_score(attribution_score)
            data.attributed_reason_samples = sorted({
                (lu.limit_up_reason or "").strip()
                for lu in exact_limit_ups
                if (lu.limit_up_reason or "").strip()
            })[:5]

            raw_limit_downs = limit_downs_by_sector.get(sector_code, [])
            attributed_limit_downs = self._filter_attributed_entries(
                raw_limit_downs,
                data.sector_name,
                lambda ld: ld.reason,
                allow_blank_mapped=data.prev_state in [
                    LifecycleState.CLIMAX,
                    LifecycleState.ACCELERATING,
                    LifecycleState.DIVERGING,
                    LifecycleState.DECLINING,
                ],
                sector_strength=data.strength_score,
                sector_fund_flow=data.fund_flow,
            )

            if raw_limit_ups:
                data.limit_up_count = len(exact_limit_ups)
            else:
                data.limit_up_count = int((today_persist.limit_up_count or 0) if today_persist else 0)
            data.limit_down_count = len({ld.code for ld in attributed_limit_downs})

            height_map: Dict[int, List[Dict]] = {}
            for lu in exact_limit_ups:
                height = lu.consecutive_days or 1
                height_map.setdefault(height, []).append({
                    "code": lu.code,
                    "name": lu.name,
                    "seal_amount": lu.seal_amount or 0,
                    "height": height,
                })

            data.consecutive_board_count = sum(len(stocks) for h, stocks in height_map.items() if h >= 2)
            data.first_board_count = len(height_map.get(1, []))
            data.max_board_height = max(height_map.keys()) if height_map else 0
            for height in sorted(height_map.keys(), reverse=True):
                data.ladders.append({"height": height, "stocks": height_map[height]})
            if data.max_board_height >= 1 and height_map:
                top_stocks = sorted(height_map.get(data.max_board_height, []), key=lambda x: x["seal_amount"], reverse=True)
                data.leader_stocks = top_stocks[:1]

            data.lifecycle_state = self._determine_state(data)
            if data.prev_state is None:
                data.state_change = "new"
            elif self.state_scores[data.lifecycle_state] > self.state_scores[data.prev_state]:
                data.state_change = "upgraded"
            elif self.state_scores[data.lifecycle_state] < self.state_scores[data.prev_state]:
                data.state_change = "downgraded"
            else:
                data.state_change = "unchanged"

            data.quality_score = self._calc_quality_score(data)
            if data.lifecycle_state in [LifecycleState.EMERGING, LifecycleState.ACCELERATING, LifecycleState.CLIMAX]:
                data.active_days += 1
            else:
                data.active_days = 0
            data.main_line_status = self._main_line_status(data)
            data.is_main_line = data.main_line_status != "none"

            analyzed[sector_code] = data

        return analyzed
    
    async def _fetch_kline_factors(self, session: AsyncSession, data: SectorLifecycleData):
        """从SectorKline读取当日K线技术因子"""
        result = await session.execute(
            select(SectorKline).where(
                and_(
                    SectorKline.sector_code == data.sector_code,
                    SectorKline.trade_date == data.trade_date,
                )
            )
        )
        kline = result.scalar_one_or_none()
        if kline:
            data.kline_trend = kline.trend_state
            data.kline_vol_ratio = kline.vol_ratio
            data.kline_support = kline.support_price
            data.kline_resistance = kline.resistance_price
            data.kline_ma5 = kline.ma5
            data.kline_ma20 = kline.ma20
            data.kline_close = kline.close

    def _build_search_patterns(self, sector_name: str) -> List[str]:
        """从板块名生成多组LIKE匹配模式(全称+简称+括号拆分)

        例如: "CPO概念(共封装光学)" → ["%CPO概念%", "%CPO%", "%共封装光学%", "%CPO概念(共封装光学)%"]
              "PCB概念" → ["%PCB概念%", "%PCB%"]
        """
        patterns = [f"%{sector_name}%"]  # 全称

        # 简称: 去掉常见后缀
        short = sector_name
        for suffix in ("概念", "板块", "产业", "主题", "(指数)",):
            if short.endswith(suffix):
                short = short[: -len(suffix)]
                break
        if short and len(short) >= 2:
            patterns.append(f"%{short}%")

        # 括号内容拆分: "名称(别名)" → 分别匹配
        import re
        parens = re.findall(r"[（(]([^）)]+)[）)]", sector_name)
        for p in parens:
            if p and len(p) >= 2:
                patterns.append(f"%{p}%")

        return patterns

    def _extract_sector_keywords(self, sector_name: str) -> List[str]:
        """提取板块关键词，用于主归因/共振归因匹配。"""
        import re

        keywords = set()
        if sector_name:
            keywords.add(sector_name.strip().lower())

        base = sector_name or ""
        # 同花顺行业名按“一级-二级-三级”存储；拆分后才能让
        # “医药生物-中药-中药Ⅲ”匹配涨停归因中的“中药”。
        for raw_part in re.split(r"[-/—]", base):
            part = re.sub(
                r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫⅰⅱⅲⅳⅴⅵⅶⅷⅸⅹ]+$",
                "",
                raw_part.strip(),
            )
            if len(part) >= 2:
                keywords.add(part.lower())
        for suffix in ("概念", "板块", "产业", "主题", "(指数)",):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        if base and len(base) >= 2:
            keywords.add(base.strip().lower())

        for part in re.findall(r"[（(]([^）)]+)[）)]", sector_name or ""):
            if part and len(part.strip()) >= 2:
                keywords.add(part.strip().lower())

        normalized = (sector_name or "").lower()
        alias_groups = {
            "芯片": ["芯片", "半导体", "存储芯片", "先进封装"],
            "半导体": ["半导体", "芯片", "存储芯片", "先进封装"],
            "数据中心": ["数据中心", "算力中心", "算力", "东数西算", "智算中心", "aic"],
            "东数西算": ["东数西算", "算力", "算力中心", "数据中心", "智算中心"],
            "算力": ["算力", "算力中心", "数据中心", "东数西算", "智算中心"],
            "云计算": ["云计算", "算力", "数据中心", "东数西算", "服务器", "it服务"],
            "信创": ["信创", "it服务", "软件开发", "计算机设备", "操作系统"],
            "人工智能": ["人工智能", "ai", "it服务", "软件开发", "计算机设备", "算力"],
            "ai智能体": ["ai智能体", "人工智能", "it服务", "软件开发", "计算机设备"],
            "aigc": ["aigc", "人工智能", "it服务", "软件开发", "计算机设备"],
            "ai应用": ["ai应用", "人工智能", "it服务", "软件开发", "计算机设备"],
            "deepseek": ["deepseek", "人工智能", "it服务", "软件开发", "计算机设备"],
            "商业航天": ["商业航天", "航天", "航空装备", "军工装备", "卫星"],
            "军工": ["军工", "军工电子", "军工装备", "航空装备", "航天"],
            "华为": ["华为", "鸿蒙", "鲲鹏", "昇腾", "消费电子", "通信设备", "软件开发"],
            "鸿蒙": ["鸿蒙", "华为", "软件开发", "it服务", "消费电子", "计算机设备"],
            "鲲鹏": ["鲲鹏", "华为", "软件开发", "it服务", "计算机设备"],
            "消费电子": ["消费电子", "电子", "智能终端"],
            "智能穿戴": ["智能穿戴", "消费电子", "智能终端", "可穿戴设备", "无线耳机"],
            "无线耳机": ["无线耳机", "消费电子", "智能终端", "智能穿戴"],
            "医疗器械": ["医疗器械", "医疗设备", "医药器械", "体外诊断"],
            "锂电池": ["锂电池", "电池", "固态电池", "储能电池"],
            "固态电池": ["固态电池", "电池", "锂电池"],
            "储能": ["储能", "电池", "储能电池"],
            "动力电池回收": ["动力电池回收", "电池回收", "电池", "能源金属", "循环利用"],
            "燃料电池": ["燃料电池", "氢能源", "电池", "氢燃料"],
            "机器人": ["机器人", "人形机器人", "自动化设备"],
            "机器人概念": ["机器人", "人形机器人", "自动化设备", "通用设备", "专用设备"],
            "人形机器人": ["人形机器人", "机器人", "自动化设备", "通用设备", "专用设备"],
            "特斯拉": ["特斯拉", "汽车零部", "汽车电子", "电池", "智能驾驶"],
            "宁德时代": ["宁德时代", "电池", "其他电源", "能源金属"],
            "虚拟电厂": ["虚拟电厂", "电力", "其他电源", "电网设备", "智能电网"],
            "低空经济": ["低空经济", "无人机", "航空装备", "军工电子", "飞行汽车"],
            "飞行汽车": ["飞行汽车", "低空经济", "航空装备", "军工电子", "evtol"],
            "多模态ai": ["多模态ai", "人工智能", "多模态", "it服务", "软件开发", "计算机设备"],
            "先进封装": ["先进封装", "半导体", "芯片", "封装", "电子化学"],
            "毫米波雷达": ["毫米波雷达", "雷达", "元件", "汽车电子", "军工电子"],
            "智能音箱": ["智能音箱", "消费电子", "智能终端", "无线耳机"],
            "无线充电": ["无线充电", "消费电子", "其他电子", "电池", "充电"],
            "富士康": ["富士康", "消费电子", "其他电子", "智能终端"],
            "氟化工": ["氟化工", "化工", "电池材料", "新材料"],
            "金属铜": ["金属铜", "铜", "工业金属", "有色金属"],
            "金属钴": ["金属钴", "钴", "能源金属", "有色金属"],
            "国产航母": ["国产航母", "军工", "军工电子", "航空装备", "航运装备"],
            "两轮车": ["两轮车", "电池", "其他电源", "汽车零部", "消费电子"],
            "京津冀": ["京津冀", "京津冀一体化", "房地产", "基建"],
            "租售同权": ["租售同权", "房地产", "地产服务", "一般零售"],
            "物业管理": ["物业管理", "地产服务", "房地产服务"],
            "新型城镇化": ["新型城镇化", "城镇化", "基建"],
            "猪肉": ["猪肉", "养殖", "养殖业", "生猪", "饲料"],
            "养殖": ["养殖", "养殖业", "猪肉", "生猪", "饲料"],
            # 概念成分已经由 StockSectorMapping 精确约束；这里仅补齐同花顺
            # “农业种植/玉米/粮食概念”与涨停归因“种植业/种子生产”的语义差。
            "农业种植": ["农业种植", "种植业", "种子生产", "粮食种植", "其他种植业"],
            "玉米": ["玉米", "种植业", "种子生产", "粮食种植"],
            "粮食": ["粮食", "种植业", "种子生产", "粮食种植"],
            "种业": ["种业", "种子生产", "种植业"],
        }
        for trigger, aliases in alias_groups.items():
            if trigger in normalized:
                keywords.update(alias.lower() for alias in aliases)

        return sorted(keywords, key=len, reverse=True)

    def _extract_sector_theme_tags(self, sector_name: str) -> List[str]:
        normalized = (sector_name or "").lower()
        theme_groups = {
            "数据中心": ["其他电子", "其他电源", "互联网电", "元件", "通信设备", "服务器"],
            "液冷服务器": ["其他电源", "其他电子", "元件", "通用设备", "服务器", "液冷"],
            "英伟达": ["半导体", "其他电子", "电子化学", "消费电子", "元件", "ai芯片"],
            "小米": ["消费电子", "其他电子", "元件", "汽车零部", "智能终端"],
            "新能源汽车": ["汽车零部", "汽车电子", "电池", "其他电源", "工业金属", "智能驾驶"],
            "汽车电子": ["汽车零部", "汽车电子", "其他电子", "元件", "消费电子"],
            "钠离子电池": ["电池", "其他电源", "储能电池", "新能源电池"],
            "锂电池": ["电池", "其他电源", "储能电池", "新能源电池"],
            "无人机": ["军工电子", "航空装备", "通用设备", "无人机"],
            "军民融合": ["军工电子", "航空装备", "军工装备", "通用设备"],
            "卫星导航": ["航空装备", "军工电子", "小金属", "卫星"],
            "华为": ["其他电子", "元件", "消费电子", "通信设备", "软件开发", "计算机设", "it服务Ⅱ"],
            "鸿蒙": ["消费电子", "软件开发", "计算机设", "it服务Ⅱ"],
            "鲲鹏": ["软件开发", "计算机设", "it服务Ⅱ", "通信服务"],
            "人工智能": ["it服务Ⅱ", "软件开发", "计算机设", "消费电子", "通信服务"],
            "ai智能体": ["it服务Ⅱ", "软件开发", "计算机设", "消费电子"],
            "aigc": ["it服务Ⅱ", "软件开发", "计算机设", "消费电子"],
            "ai应用": ["it服务Ⅱ", "软件开发", "计算机设", "消费电子"],
            "deepseek": ["it服务Ⅱ", "软件开发", "计算机设", "消费电子"],
            "多模态ai": ["it服务Ⅱ", "软件开发", "计算机设", "消费电子"],
            "云计算": ["it服务Ⅱ", "软件开发", "计算机设", "通信服务", "其他电子"],
            "信创": ["it服务Ⅱ", "软件开发", "计算机设", "通信服务"],
            "医疗器械": ["医疗器械", "中药Ⅱ", "化学制药", "电网设备", "医药商业"],
            "智能穿戴": ["消费电子", "其他电子", "元件", "智能终端"],
            "无线耳机": ["消费电子", "其他电子", "元件", "智能终端"],
            "机器人概念": ["通用设备", "专用设备", "消费电子", "计算机设", "自动化设备"],
            "人形机器人": ["通用设备", "专用设备", "消费电子", "计算机设", "自动化设备"],
            "特斯拉": ["汽车零部", "汽车电子", "电池", "其他电源"],
            "宁德时代": ["电池", "其他电源", "能源金属"],
            "动力电池回收": ["电池", "能源金属", "其他电源", "循环利用"],
            "燃料电池": ["电池", "小金属", "其他电源", "氢能源"],
            "虚拟电厂": ["其他电源", "电力", "电网设备", "智能电网"],
            "先进封装": ["半导体", "电子化学", "元件", "其他电子"],
            "毫米波雷达": ["元件", "汽车电子", "军工电子", "航空装备"],
            "智能音箱": ["消费电子", "其他电子", "元件", "智能终端"],
            "无线充电": ["其他电子", "消费电子", "电池", "充电设备"],
            "富士康": ["其他电子", "消费电子", "元件", "智能终端"],
            "氟化工": ["电池", "化工原料", "新材料", "电子化学"],
            "金属铜": ["工业金属", "能源金属", "有色金属"],
            "金属钴": ["能源金属", "有色金属", "电池"],
            "国产航母": ["军工电子", "航空装备", "航运装备", "军工装备"],
            "两轮车": ["其他电源", "电池", "汽车零部", "消费电子"],
            "京津冀": ["房地产开", "一般零售", "专业工程", "基建"],
            "租售同权": ["一般零售", "房地产开", "地产服务"],
            "低空经济": ["军工电子", "航空装备", "通用设备"],
            "飞行汽车": ["航空装备", "军工电子", "汽车零部"],
            "海峡两岸": ["互联网电", "元件", "玻璃玻纤", "包装印刷"],
            "人民币贬值受益": ["纺织制造", "消费电子", "专业工程", "元件"],
            "粤港澳大湾区": ["消费电子", "其他电子", "半导体", "房地产开"],
            "猪肉": ["养殖业", "养殖", "生猪", "饲料"],
        }
        tags = set()
        for trigger, values in theme_groups.items():
            if trigger in normalized:
                tags.update(v.lower() for v in values)
        return sorted(tags)

    def _is_excluded_stock(self, code: str, name: Optional[str], blocked_codes: set[str]) -> bool:
        normalized_name = (name or "").upper().replace(" ", "")
        if code in blocked_codes:
            return True
        if normalized_name.startswith("*ST") or normalized_name.startswith("ST"):
            return True
        if "退" in normalized_name or "退市" in normalized_name:
            return True
        return False

    async def _load_blocked_stock_codes(self, session: AsyncSession, stock_codes: List[str], trade_date: date) -> set[str]:
        if not stock_codes:
            return set()

        tag_rows = (
            await session.execute(
                select(StockTag.code).where(
                    and_(
                        StockTag.code.in_(stock_codes),
                        or_(
                            StockTag.is_st.is_(True),
                            StockTag.is_suspended.is_(True),
                            StockTag.is_delisting.is_(True),
                            StockTag.board_tag == "suspended",
                        ),
                    )
                )
            )
        ).all()
        blocked_codes = {row[0] for row in tag_rows}

        blacklist_rows = (
            await session.execute(
                select(StockBlacklist.code).where(
                    and_(
                        StockBlacklist.code.in_(stock_codes),
                        StockBlacklist.start_date <= trade_date,
                        or_(StockBlacklist.end_date.is_(None), StockBlacklist.end_date >= trade_date),
                        StockBlacklist.reason.in_(["st", "delisting", "suspended"]),
                    )
                )
            )
        ).all()
        blocked_codes.update(row[0] for row in blacklist_rows)
        return blocked_codes

    def _reason_matches_sector(self, reason: Optional[str], sector_name: str) -> bool:
        if not reason:
            return False
        normalized_reason = reason.lower().replace(" ", "")
        return any(
            kw.replace(" ", "") in normalized_reason
            for kw in self._extract_sector_keywords(sector_name)
            if kw
        )

    def _theme_matches_sector(self, reason: Optional[str], sector_name: str) -> bool:
        if not reason:
            return False
        normalized_reason = reason.lower().replace(" ", "")
        return any(tag.replace(" ", "") in normalized_reason for tag in self._extract_sector_theme_tags(sector_name))

    def _entry_attribution_score(
        self,
        entry,
        sector_name: str,
        reason_getter,
        *,
        allow_blank_mapped: bool = False,
        sector_strength: float = 0,
        sector_fund_flow: float = 0,
        is_leader_candidate: bool = False,
    ) -> int:
        reason = (reason_getter(entry) or "").strip()
        score = 0
        if reason:
            if self._reason_matches_sector(reason, sector_name):
                score += 4
            elif self._theme_matches_sector(reason, sector_name):
                score += 2
        elif allow_blank_mapped:
            score += 1

        if getattr(entry, "consecutive_days", 0) and getattr(entry, "consecutive_days", 0) >= 2:
            score += 1
        if getattr(entry, "seal_amount", 0) and float(getattr(entry, "seal_amount", 0) or 0) >= 50_000_000:
            score += 1
        if is_leader_candidate and sector_strength >= 60 and sector_fund_flow > 5:
            score += 1
        return score

    def _attribution_confidence_from_score(self, score: int) -> str:
        if score >= 4:
            return "primary"
        if score >= 2:
            return "theme"
        if score >= 1:
            return "resonance"
        return "none"

    def _filter_attributed_entries_with_meta(
        self,
        entries: List,
        sector_name: str,
        reason_getter,
        *,
        allow_blank_mapped: bool = False,
        sector_strength: float = 0,
        sector_fund_flow: float = 0,
    ) -> tuple[List, int]:
        filtered = []
        best_score = 0
        for idx, entry in enumerate(entries):
            score = self._entry_attribution_score(
                entry,
                sector_name,
                reason_getter,
                allow_blank_mapped=allow_blank_mapped,
                sector_strength=sector_strength,
                sector_fund_flow=sector_fund_flow,
                is_leader_candidate=(idx == 0),
            )
            best_score = max(best_score, score)
            if score >= 3:
                filtered.append(entry)

        if filtered:
            return filtered, best_score

        if sector_strength >= 60 and sector_fund_flow > 5 and entries:
            ranked = sorted(
                entries,
                key=lambda item: (
                    getattr(item, "consecutive_days", 0) or 0,
                    float(getattr(item, "seal_amount", 0) or 0),
                ),
                reverse=True,
            )
            resonance = []
            for entry in ranked[:2]:
                score = self._entry_attribution_score(
                    entry,
                    sector_name,
                    reason_getter,
                    allow_blank_mapped=allow_blank_mapped,
                    sector_strength=sector_strength,
                    sector_fund_flow=sector_fund_flow,
                    is_leader_candidate=True,
                )
                best_score = max(best_score, score)
                if score >= 2:
                    resonance.append(entry)
            if resonance:
                return resonance, best_score

        return [], best_score

    def _filter_attributed_entries(
        self,
        entries: List,
        sector_name: str,
        reason_getter,
        *,
        allow_blank_mapped: bool = False,
        sector_strength: float = 0,
        sector_fund_flow: float = 0,
    ) -> List:
        filtered, _ = self._filter_attributed_entries_with_meta(
            entries,
            sector_name,
            reason_getter,
            allow_blank_mapped=allow_blank_mapped,
            sector_strength=sector_strength,
            sector_fund_flow=sector_fund_flow,
        )
        return filtered

    async def _fetch_limit_up_data(self, session: AsyncSession, data: SectorLifecycleData):
        """获取板块涨停数据 — 三级匹配策略

        Level 1: StockSectorMapping精确关联(code列表IN查询)
        Level 2: 增强LIKE fallback(全称+简称+括号拆解多模式OR)
        Level 3: 直接解析limit_up_reason归因(不依赖映射表)
        """
        limit_ups = []

        # === Level 1: 映射表精确匹配 ===
        mapping_result = await session.execute(
            select(StockSectorMapping.code).where(
                StockSectorMapping.sector_code == data.sector_code
            )
        )
        sector_codes_list = [r[0] for r in mapping_result.all()]
        blocked_codes = await self._load_blocked_stock_codes(session, sector_codes_list, data.trade_date)

        mapped_limit_ups = []
        if sector_codes_list:
            result = await session.execute(
                select(LimitUpPool).where(
                    and_(
                        LimitUpPool.trade_date == data.trade_date,
                        LimitUpPool.code.in_(sector_codes_list),
                    )
                )
            )
            mapped_limit_ups = [
                lu for lu in result.scalars().all()
                if not self._is_excluded_stock(lu.code, lu.name, blocked_codes)
            ]
            raw_height_map = {}
            for lu in mapped_limit_ups:
                height = lu.consecutive_days or 1
                raw_height_map.setdefault(height, []).append({
                    "code": lu.code,
                    "name": lu.name,
                    "seal_amount": lu.seal_amount or 0,
                    "height": height,
                })
            data.raw_limit_up_count = len(mapped_limit_ups)
            data.raw_reason_samples = sorted({
                (lu.limit_up_reason or "").strip()
                for lu in mapped_limit_ups
                if (lu.limit_up_reason or "").strip()
            })[:5]
            data.raw_consecutive_board_count = sum(len(stocks) for h, stocks in raw_height_map.items() if h >= 2)
            data.raw_first_board_count = len(raw_height_map.get(1, []))
            data.raw_max_board_height = max(raw_height_map.keys()) if raw_height_map else 0
            data.raw_ladders = [
                {"height": height, "stocks": raw_height_map[height]}
                for height in sorted(raw_height_map.keys(), reverse=True)
            ]
            if data.raw_max_board_height >= 1 and raw_height_map:
                top_stocks = sorted(raw_height_map.get(data.raw_max_board_height, []), key=lambda x: x["seal_amount"], reverse=True)
                data.raw_leader_stocks = top_stocks[:1]
            if data.sector_type == "industry":
                # 主营行业映射是一对一治理结果，本身就是归因证据；只有概念
                # 映射还需要涨停原因二次确认，防止泛概念重复累计。
                limit_ups = mapped_limit_ups
                attribution_score = 4 if mapped_limit_ups else 0
            else:
                limit_ups, attribution_score = self._filter_attributed_entries_with_meta(
                    mapped_limit_ups,
                    data.sector_name,
                    lambda lu: lu.limit_up_reason,
                    allow_blank_mapped=True,
                    sector_strength=data.strength_score,
                    sector_fund_flow=data.fund_flow,
                )
            data.attribution_confidence_score = attribution_score
            data.attribution_confidence = self._attribution_confidence_from_score(attribution_score)

        # === Level 2: 增强LIKE fallback ===
        if not limit_ups:
            patterns = self._build_search_patterns(data.sector_name)
            conditions = [
                LimitUpPool.limit_up_reason.like(p) for p in patterns
            ]
            result = await session.execute(
                select(LimitUpPool).where(
                    and_(
                        LimitUpPool.trade_date == data.trade_date,
                        or_(*conditions),
                    )
                )
            )
            limit_ups = [
                lu for lu in result.scalars().all()
                if not self._is_excluded_stock(lu.code, lu.name, blocked_codes)
            ]

        # === Level 3: 直接解析limit_up_reason归因 ===
        if not limit_ups:
            # 取当日所有涨停股的reason, 用关键词匹配
            all_result = await session.execute(
                select(LimitUpPool).where(
                    LimitUpPool.trade_date == data.trade_date,
                )
            )
            all_lus = all_result.scalars().all()

            core_keywords = self._extract_sector_keywords(data.sector_name)
            if core_keywords:
                for lu in all_lus:
                    if self._is_excluded_stock(lu.code, lu.name, blocked_codes):
                        continue
                    reason = lu.limit_up_reason or ""
                    if any(kw in reason.lower() for kw in core_keywords):
                        limit_ups.append(lu)

        # 去重，避免多级归因重复计数
        deduped = {}
        for lu in limit_ups:
            deduped[lu.code] = lu
        limit_ups = list(deduped.values())
        data.attributed_reason_samples = sorted({
            (lu.limit_up_reason or "").strip()
            for lu in limit_ups
            if (lu.limit_up_reason or "").strip()
        })[:5]
        if limit_ups and data.attribution_confidence == "none":
            data.attribution_confidence = "primary"
            data.attribution_confidence_score = 4
        
        data.limit_up_count = len(limit_ups)
        
        height_map = {}
        for lu in limit_ups:
            height = lu.consecutive_days or 1
            if height not in height_map:
                height_map[height] = []
            height_map[height].append({
                "code": lu.code,
                "name": lu.name,
                "seal_amount": lu.seal_amount or 0,
                "height": height,
            })
        
        data.consecutive_board_count = sum(
            len(stocks) for h, stocks in height_map.items() if h >= 2
        )
        data.first_board_count = len(height_map.get(1, []))
        data.max_board_height = max(height_map.keys()) if height_map else 0
        
        # 构建梯队
        for height in sorted(height_map.keys(), reverse=True):
            data.ladders.append({
                "height": height,
                "stocks": height_map[height],
            })
        
        # 龙头股: 每个板块唯一一个——最高板中封单金额最大的个股
        # 首板板块: 封单最大的首板股; 连板板块: 最高板梯队中封单最大的
        if data.max_board_height >= 1 and height_map:
            top_stocks = height_map.get(data.max_board_height, [])
            top_stocks.sort(key=lambda x: x["seal_amount"], reverse=True)
            data.leader_stocks = top_stocks[:1]  # 龙头只有一个

    async def _fetch_limit_down_data(self, session: AsyncSession, data: SectorLifecycleData):
        """获取板块跌停数，退潮/分化判断需要该维度。"""
        mapping_result = await session.execute(
            select(StockSectorMapping.code).where(
                StockSectorMapping.sector_code == data.sector_code
            )
        )
        sector_codes_list = [r[0] for r in mapping_result.all()]
        if not sector_codes_list:
            data.limit_down_count = 0
            return
        blocked_codes = await self._load_blocked_stock_codes(session, sector_codes_list, data.trade_date)

        result = await session.execute(
            select(LimitDownPool).where(
                and_(
                    LimitDownPool.trade_date == data.trade_date,
                    LimitDownPool.code.in_(sector_codes_list),
                )
            )
        )
        mapped_limit_downs = [
            ld for ld in result.scalars().all()
            if not self._is_excluded_stock(ld.code, ld.name, blocked_codes)
        ]
        allow_blank_mapped = data.prev_state in [
            LifecycleState.CLIMAX,
            LifecycleState.ACCELERATING,
            LifecycleState.DIVERGING,
            LifecycleState.DECLINING,
        ]
        attributed_limit_downs = self._filter_attributed_entries(
            mapped_limit_downs,
            data.sector_name,
            lambda ld: ld.reason,
            allow_blank_mapped=allow_blank_mapped,
            sector_strength=data.strength_score,
            sector_fund_flow=data.fund_flow,
        )
        data.limit_down_count = len({ld.code for ld in attributed_limit_downs})
    
    async def _fetch_fund_flow(self, session: AsyncSession, data: SectorLifecycleData):
        """获取板块资金数据"""
        result = await session.execute(
            select(SectorPersistence).where(
                and_(
                    SectorPersistence.trade_date == data.trade_date,
                    SectorPersistence.sector_code == data.sector_code,
                )
            )
        )
        sp = result.scalar_one_or_none()
        if sp:
            data.fund_flow = sp.fund_flow or 0
            data.persistence_consecutive_days = sp.consecutive_days or 0
            data.strength_score = sp.strength_score or 0
            data.change_pct = sp.change_pct or 0
        
        three_days_ago = data.trade_date - timedelta(days=5)
        result = await session.execute(
            select(func.sum(SectorPersistence.fund_flow)).where(
                and_(
                    SectorPersistence.sector_code == data.sector_code,
                    SectorPersistence.trade_date >= three_days_ago,
                    SectorPersistence.trade_date <= data.trade_date,
                )
            )
        )
        data.fund_flow_3d = result.scalar() or 0
    
    async def _fetch_prev_state(self, session: AsyncSession, sector_code: str, trade_date: date):
        """获取前一日状态"""
        result = await session.execute(
            select(SectorLifecycle).where(
                and_(
                    SectorLifecycle.sector_code == sector_code,
                    SectorLifecycle.trade_date < trade_date,
                )
            ).order_by(desc(SectorLifecycle.trade_date)).limit(1)
        )
        return result.scalar_one_or_none()
    
    async def _calc_active_5d(self, session: AsyncSession, sector_code: str, trade_date: date) -> int:
        """计算近5天活跃天数"""
        five_days_ago = trade_date - timedelta(days=7)
        result = await session.execute(
            select(func.count()).where(
                and_(
                    SectorLifecycle.sector_code == sector_code,
                    SectorLifecycle.trade_date >= five_days_ago,
                    SectorLifecycle.trade_date <= trade_date,
                    SectorLifecycle.limit_up_count > 0,
                )
            )
        )
        lifecycle_total = result.scalar() or 0
        if lifecycle_total:
            return lifecycle_total

        fallback = await session.execute(
            select(func.count()).where(
                and_(
                    SectorPersistence.sector_code == sector_code,
                    SectorPersistence.trade_date >= five_days_ago,
                    SectorPersistence.trade_date <= trade_date,
                    or_(
                        SectorPersistence.limit_up_count > 0,
                        SectorPersistence.fund_flow > 0,
                    ),
                )
            )
        )
        return fallback.scalar() or 0
    
    def _determine_state(self, data: SectorLifecycleData) -> LifecycleState:
        """判断当前生命周期状态

        判定优先级(从高到低):
        1. climax: 最强正向状态
        2. declining: 最强负向状态
        3. diverging: 强转弱过渡
        4. accelerating: 主升阶段
        5. emerging: 启动阶段
        6. one_day: 启动失败
        7. dormant: 最终兜底
        """
        if self._check_climax(data):
            return LifecycleState.CLIMAX
        if self._check_declining(data):
            return LifecycleState.DECLINING
        if self._check_diverging(data):
            return LifecycleState.DIVERGING
        if self._check_accelerating(data):
            return LifecycleState.ACCELERATING
        if self._check_emerging(data):
            return LifecycleState.EMERGING
        if self._check_one_day(data):
            return LifecycleState.ONE_DAY

        # 兜底: 只要当日仍有涨停结构，就不应判为休眠。
        if data.limit_up_count > 0 or data.max_board_height > 0:
            if (
                (data.fund_flow < 0 or self._is_breakdown_trend(data))
                and (
                    data.limit_up_count >= 2
                    or
                    data.consecutive_board_count >= 1
                    or data.max_board_height >= 3
                    or data.limit_down_count > 0
                )
            ):
                return LifecycleState.DIVERGING

            has_min_trigger = (
                data.fund_flow > 0
                and not self._is_breakdown_trend(data)
                and (
                    data.limit_up_count >= 5
                    or
                    data.limit_up_count >= 2
                    or data.consecutive_board_count >= 1
                    or data.max_board_height >= 2
                )
                and (
                    data.limit_up_count >= 5
                    or data.strength_score >= 35
                    or data.change_pct >= 0.8
                    or self._is_uptrend(data)
                )
            )
            if has_min_trigger:
                return LifecycleState.EMERGING

        return LifecycleState.DORMANT

    def _is_uptrend(self, data: SectorLifecycleData) -> bool:
        return bool(
            data.kline_trend in ("up", "breakout_up")
            or (
                data.kline_close
                and data.kline_ma5
                and data.kline_close >= data.kline_ma5
            )
        )

    def _is_breakdown_trend(self, data: SectorLifecycleData) -> bool:
        return bool(
            data.kline_trend in ("down", "breakdown", "breakout_down")
            or (
                data.kline_close
                and data.kline_ma20
                and data.kline_close < data.kline_ma20 * 0.98
            )
        )

    def _is_weak_trend(self, data: SectorLifecycleData) -> bool:
        return bool(
            data.kline_trend in ("sideways", "down", "breakdown", "breakout_down")
            or (
                data.kline_close
                and data.kline_ma5
                and data.kline_close < data.kline_ma5
            )
        )

    def _has_kline_observation(self, data: SectorLifecycleData) -> bool:
        return bool(
            data.kline_trend
            or data.kline_close
            or data.kline_ma5
            or data.kline_ma20
            or data.kline_vol_ratio
        )

    def _has_kline_start_confirmation(self, data: SectorLifecycleData) -> bool:
        if data.kline_trend in ("breakout_up", "up"):
            return True
        if (
            data.kline_close
            and data.kline_ma5
            and data.kline_ma20
            and data.kline_close >= data.kline_ma5 >= data.kline_ma20
        ):
            return True
        return False

    def _has_kline_acceleration_confirmation(self, data: SectorLifecycleData) -> bool:
        if not self._has_kline_observation(data):
            return True
        return bool(
            data.kline_trend in ("breakout_up", "up")
            or (
                data.kline_close
                and data.kline_ma5
                and data.kline_close >= data.kline_ma5
            )
        )

    def _has_kline_climax_confirmation(self, data: SectorLifecycleData) -> bool:
        if not self._has_kline_observation(data):
            return True
        return bool(
            data.kline_trend == "breakout_up"
            or (
                data.kline_close
                and data.kline_ma5
                and data.kline_close >= data.kline_ma5 * 1.02
            )
            or (data.kline_vol_ratio and data.kline_vol_ratio >= 1.2)
            or (
                data.kline_trend == "up"
                and data.kline_close
                and data.kline_ma5
                and data.kline_close >= data.kline_ma5
            )
        )
    
    def _check_emerging(self, data: SectorLifecycleData) -> bool:
        """判断是否刚启动"""
        if data.prev_state and data.prev_state in [LifecycleState.ACCELERATING, LifecycleState.CLIMAX]:
            return False

        has_basic_momentum = (
            data.strength_score >= 35
            or data.change_pct >= 0.8
            or self._has_kline_start_confirmation(data)
        )
        weak_kline = self._is_breakdown_trend(data)

        # 标准启动：2-4只已完成归因的涨停本身就是有效宽度。板块资金字段在
        # 部分数据源/交易日会缺失并落成0，不能因此把真实涨停簇误判成休眠；
        # 但明确净流出或K线破位仍不放行，且缺资金时不会升级到加速/高潮。
        if (
            2 <= data.limit_up_count <= 4
            and data.fund_flow >= 0
            and not weak_kline
        ):
            return True

        # 弱启动：仅1只涨停或低位连板，必须满足资金转正，且强度/涨幅过下限。
        if (
            data.limit_up_count == 1
            and data.max_board_height <= 2
            and data.fund_flow > 0
            and has_basic_momentum
            and not weak_kline
        ):
            return True

        # 宽度型启动: 当天涨停面已明显铺开，但梯队尚未形成完整加速结构。
        if (
            data.limit_up_count >= 5
            and data.fund_flow >= 0
            and (
                data.max_board_height <= 2
                or data.consecutive_board_count < 3
            )
            and not weak_kline
        ):
            return True

        # 无涨停但有早期趋势/资金共振，仅允许从休眠/退潮后重新启动。
        if (
            data.limit_up_count == 0
            and data.fund_flow > 3.0
            and data.kline_close
            and data.kline_ma5
            and data.kline_ma20
            and data.kline_close >= data.kline_ma5 >= data.kline_ma20
            and data.prev_state not in [LifecycleState.ACCELERATING, LifecycleState.CLIMAX]
            and not weak_kline
        ):
            return True

        return False
    
    def _check_accelerating(self, data: SectorLifecycleData) -> bool:
        """判断是否加速"""
        if data.fund_flow < 3.0:
            return False
        if data.max_board_height < 3:
            return False
        if self._is_breakdown_trend(data):
            return False

        has_momentum = (
            data.active_days >= 1
            or data.persistence_consecutive_days >= 2
            or data.total_active_5d >= 2
            or data.limit_up_count >= 8
            or (data.limit_up_count >= 6 and data.max_board_height >= 4)
        )
        if not has_momentum:
            return False

        # 标准加速: 梯队足够完整，板块宽度也已经铺开。
        standard_accelerating = (
            data.consecutive_board_count >= 3
            and data.limit_up_count >= 5
        )

        # 爆发式加速: 连板虽然未达到3只，但高标+宽度+资金已明显强于普通启动。
        explosive_accelerating = (
            data.consecutive_board_count >= 2
            and data.limit_up_count >= 4
            and data.max_board_height >= 4
            and data.fund_flow >= 8.0
            and (
                data.active_days >= 1
                or data.persistence_consecutive_days >= 2
                or data.total_active_5d >= 2
            )
        )

        # 龙头驱动型加速: 头部高度足够，板块宽度虽偏首板，但资金和涨停数量已经显著增强。
        leader_driven_accelerating = (
            data.limit_up_count >= 5
            and data.max_board_height >= 4
            and data.fund_flow >= 12.0
            and data.consecutive_board_count >= 1
            and data.first_board_count <= max(data.limit_up_count - 1, 1)
            and data.active_days >= 1
        )

        if not (standard_accelerating or explosive_accelerating or leader_driven_accelerating):
            return False

        # K线有数据时仍作为确认项；没有K线时不再一票否决。
        if self._has_kline_observation(data) and not self._has_kline_acceleration_confirmation(data):
            return False
        return True
    
    def _check_climax(self, data: SectorLifecycleData) -> bool:
        """判断是否高潮"""
        if data.limit_up_count < 10:
            return False
        if data.max_board_height < 5:
            return False
        if data.consecutive_board_count < 5:
            return False
        if data.fund_flow < 5.0:
            return False
        if not self._has_kline_climax_confirmation(data):
            return False
        return True
    
    def _check_diverging(self, data: SectorLifecycleData) -> bool:
        """判断是否分化"""
        if data.prev_state not in [LifecycleState.CLIMAX, LifecycleState.ACCELERATING]:
            # 无前序状态时，也要识别“高标仍在但承接转弱”的盘中分化。
            has_high_leader = data.max_board_height >= 4
            still_active = data.limit_up_count >= 3
            weak_acceptance = (
                data.fund_flow <= 0
                or data.limit_down_count >= 1
                or self._is_weak_trend(data)
            )
            ladder_weakening = (
                data.consecutive_board_count <= 2
                or data.first_board_count >= max(data.consecutive_board_count * 2, 4)
            )
            solo_leader_diverging = (
                data.max_board_height >= 5
                and data.limit_up_count >= 5
                and data.consecutive_board_count <= 1
                and data.first_board_count >= max(data.limit_up_count - 1, 4)
                and data.fund_flow <= 2
            )
            early_fade = (
                data.prev_state == LifecycleState.EMERGING
                and has_high_leader
                and still_active
                and weak_acceptance
            )
            return solo_leader_diverging or (has_high_leader and still_active and weak_acceptance and (ladder_weakening or early_fade))

        if (
            data.limit_up_count < 5
            or data.max_board_height < 4
            or data.consecutive_board_count < 3
            or data.fund_flow <= 0
        ):
            return True
        if self._is_weak_trend(data) and (
            data.fund_flow <= 2
            or data.limit_down_count >= 1
            or data.limit_up_count < 7
        ):
            return True
        return False
    
    def _check_declining(self, data: SectorLifecycleData) -> bool:
        """判断是否退潮 — 板块达到高点后持续回调

        退潮的核心定义: 曾经活跃(高潮/加速)的板块, 现在热度明显下降

        判定维度(满足任一即触发):
        1. 前日高潮/加速 → 今日无涨停梯队(龙头断板+连板归零)
        2. 连续2天以上退潮状态(惯性延续)
        3. K线趋势向下 + 资金大幅流出(技术面确认)
        4. 从高潮直接跳水: 涨停数骤降70%+
        """
        # 修复态保护: 分化/退潮后的当日首板修复，且资金重新转正时，不应继续压成退潮。
        has_repair_signal = (
            data.prev_state in [LifecycleState.DIVERGING, LifecycleState.DECLINING]
            and data.first_board_count >= 1
            and data.limit_up_count >= 1
            and data.fund_flow > 3
            and not self._is_breakdown_trend(data)
        )
        if has_repair_signal:
            return False

        # 维度1: 高潮后梯队瓦解(最经典的退潮形态)
        if data.prev_state in [LifecycleState.CLIMAX, LifecycleState.ACCELERATING]:
            if data.max_board_height <= 1 and data.consecutive_board_count == 0:
                return True
        if data.prev_state in [LifecycleState.CLIMAX, LifecycleState.ACCELERATING, LifecycleState.DIVERGING]:
            if data.limit_up_count > 0 and data.limit_up_count < (data.total_active_5d or 1):
                # 有少量涨停但远低于近5天活跃水平, 说明热度在衰减
                pass  # 不单独触发, 配合其他条件

        # 维度2: 惯性延续 — 已经是退潮状态且继续走弱
        if data.prev_state == LifecycleState.DECLINING:
            # 继续退潮: 无涨停 或 资金继续流出 或 K线下跌
            is_weaker = (
                data.limit_up_count == 0
                or data.fund_flow < -5
                or self._is_breakdown_trend(data)
            )
            if is_weaker:
                return True
            # 即使不更弱但也没恢复 → 维持退潮(给emerging机会)
            if data.fund_flow <= 0 and data.limit_up_count <= 1 and not self._check_emerging(data):
                return True

        # 维度3: 分化后进一步恶化 → 退潮
        if data.prev_state == LifecycleState.DIVERGING:
            if data.limit_down_count >= 2 or data.fund_flow < -10:
                return True
            # 分化后涨停数继续减少到接近零
            if data.limit_up_count <= 1 and data.consecutive_board_count == 0 and (
                data.fund_flow <= 0 or self._is_weak_trend(data)
            ):
                return True

        # 维度3.5: 高标断板 + 资金继续恶化 + 跌停扩散/梯队坍塌
        high_leader_failed = (
            data.prev_state in [LifecycleState.CLIMAX, LifecycleState.ACCELERATING, LifecycleState.DIVERGING]
            and data.max_board_height <= 2
            and data.consecutive_board_count == 0
        )
        capital_continues_to_worsen = (
            data.fund_flow < -15
            or (data.fund_flow_3d is not None and data.fund_flow_3d < -25)
        )
        downside_spreading = (
            data.limit_down_count >= 2
            or data.limit_up_count <= 2
            or self._is_breakdown_trend(data)
        )
        if high_leader_failed and capital_continues_to_worsen and downside_spreading:
            return True

        # 维度4: K线破位 + 资金出逃 = 技术面确认退潮
        has_kline_signal = (
            self._is_breakdown_trend(data) or
            (data.kline_close and data.kline_ma20 and data.kline_close < data.kline_ma20 * 0.97)
        )
        has_fund_exit = data.fund_flow < -10 or (data.fund_flow_3d and data.fund_flow_3d < -20)
        if has_kline_signal and has_fund_exit:
            return True

        # 维度5: 批量跌停(最强烈的退潮信号)
        # 但如果没有明确前序强状态，且当日仍有首板修复、资金未转负，
        # 仅凭静态映射打到的跌停股不应直接判退潮。
        if data.limit_down_count >= 3:
            if data.prev_state in [
                LifecycleState.CLIMAX,
                LifecycleState.ACCELERATING,
                LifecycleState.DIVERGING,
                LifecycleState.DECLINING,
            ]:
                return True

            has_confirming_weakness = (
                data.limit_up_count == 0
                or data.fund_flow < 0
                or (data.fund_flow_3d is not None and data.fund_flow_3d < -25)
                or self._is_breakdown_trend(data)
            )
            if has_confirming_weakness:
                return True

        return False
    
    def _check_one_day(self, data: SectorLifecycleData) -> bool:
        """判断是否一日游"""
        if data.prev_state == LifecycleState.EMERGING and data.limit_up_count <= 1:
            # 前一日刚启动，次日既没有扩散，也没有资金承接。
            if (
                data.active_days <= 1
                and data.fund_flow <= 0
                and (self._is_weak_trend(data) or data.limit_up_count == 0)
                and not self._check_emerging(data)
            ):
                return True
        return False
    
    def _calc_quality_score(self, data: SectorLifecycleData) -> float:
        """计算板块质量分"""
        score = 0
        
        heights = {l["height"] for l in data.ladders}
        if len(heights) >= 3:
            score += 30
        elif len(heights) >= 2:
            score += 20
        elif len(heights) >= 1:
            score += 10
        
        if data.max_board_height >= 7:
            score += 30
        elif data.max_board_height >= 5:
            score += 25
        elif data.max_board_height >= 3:
            score += 15
        
        if data.limit_up_count >= 15:
            score += 20
        elif data.limit_up_count >= 10:
            score += 15
        elif data.limit_up_count >= 5:
            score += 10
        
        if data.fund_flow >= 10:
            score += 20
        elif data.fund_flow >= 5:
            score += 15
        elif data.fund_flow >= 2:
            score += 10
        
        return min(score, 100)
    
    def _main_line_status(self, data: SectorLifecycleData) -> str:
        """主线四档: strengthening / continuing / diverging / none"""
        active_enough = data.total_active_5d >= 3 or data.persistence_consecutive_days >= 3
        has_core_height = data.max_board_height >= 3
        has_quality = data.quality_score >= 60
        fake_solo_leader = (
            data.first_board_count >= max(data.limit_up_count - 1, 4)
            and data.consecutive_board_count <= 1
        )

        if data.lifecycle_state in [LifecycleState.DORMANT, LifecycleState.ONE_DAY, LifecycleState.DECLINING]:
            return "none"
        if not active_enough or not has_core_height or not has_quality:
            return "none"
        if fake_solo_leader:
            return "none"

        if data.lifecycle_state == LifecycleState.DIVERGING:
            if data.limit_up_count >= 3 and data.max_board_height >= 3:
                return "diverging"
            return "none"

        if data.lifecycle_state == LifecycleState.CLIMAX:
            return "strengthening"

        if data.lifecycle_state == LifecycleState.ACCELERATING:
            if data.fund_flow >= 5 and data.consecutive_board_count >= 3 and data.max_board_height >= 4:
                return "strengthening"
            return "continuing"

        if data.lifecycle_state == LifecycleState.EMERGING:
            if data.fund_flow > 0 and (data.consecutive_board_count >= 2 or data.limit_up_count >= 6):
                return "continuing"
            return "none"

        return "none"

    def _is_main_line(self, data: SectorLifecycleData) -> bool:
        return self._main_line_status(data) != "none"
    
    async def save_lifecycle(self, session: AsyncSession, data: SectorLifecycleData):
        """保存生命周期数据"""
        result = await session.execute(
            select(SectorLifecycle).where(
                and_(
                    SectorLifecycle.trade_date == data.trade_date,
                    SectorLifecycle.sector_code == data.sector_code,
                )
            )
        )
        existing = result.scalar_one_or_none()
        
        leader_stocks_json = json.dumps(data.leader_stocks, ensure_ascii=False) if data.leader_stocks else "[]"
        ladders_json = json.dumps(data.ladders, ensure_ascii=False) if data.ladders else "[]"
        
        if existing:
            existing.lifecycle_state = data.lifecycle_state.value
            existing.state_score = self.state_scores[data.lifecycle_state]
            existing.limit_up_count = data.limit_up_count
            existing.first_board_count = data.first_board_count
            existing.consecutive_board_count = data.consecutive_board_count
            existing.max_board_height = data.max_board_height
            existing.leader_stocks = leader_stocks_json
            existing.ladder_stocks = ladders_json
            existing.fund_flow = data.fund_flow
            existing.fund_flow_3d = data.fund_flow_3d
            existing.active_days = data.active_days
            existing.total_active_5d = data.total_active_5d
            existing.quality_score = data.quality_score
            existing.is_main_line = 1 if data.is_main_line else 0
            existing.kline_trend = data.kline_trend
            existing.kline_vol_ratio = data.kline_vol_ratio
            existing.kline_support = data.kline_support
            existing.kline_resistance = data.kline_resistance
            existing.kline_ma5 = data.kline_ma5
            existing.kline_ma20 = data.kline_ma20
            existing.kline_close = data.kline_close
        else:
            session.add(SectorLifecycle(
                trade_date=data.trade_date,
                sector_code=data.sector_code,
                sector_name=data.sector_name,
                sector_type=data.sector_type,
                lifecycle_state=data.lifecycle_state.value,
                state_score=self.state_scores[data.lifecycle_state],
                limit_up_count=data.limit_up_count,
                first_board_count=data.first_board_count,
                consecutive_board_count=data.consecutive_board_count,
                max_board_height=data.max_board_height,
                leader_stocks=leader_stocks_json,
                ladder_stocks=ladders_json,
                fund_flow=data.fund_flow,
                fund_flow_3d=data.fund_flow_3d,
                active_days=data.active_days,
                total_active_5d=data.total_active_5d,
                quality_score=data.quality_score,
                is_main_line=1 if data.is_main_line else 0,
                kline_trend=data.kline_trend,
                kline_vol_ratio=data.kline_vol_ratio,
                kline_support=data.kline_support,
                kline_resistance=data.kline_resistance,
                kline_ma5=data.kline_ma5,
                kline_ma20=data.kline_ma20,
                kline_close=data.kline_close,
            ))
        
        await session.commit()
