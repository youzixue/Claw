"""板块轮动日历 — 日线级别记录板块活跃史

取代原来的桑基图流向，改为日历形式展示:
- 横向: 日期轴(最近10-20个交易日)
- 纵向: 板块列表
- 单元格: 板块当日状态(颜色区分) + 涨停数

优势:
1. 直观看到板块活跃周期
2. 清晰判断一日游(单日活跃后消失)
3. 识别主线(连续多日活跃)
4. 看到板块切换节奏
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Dict, Optional

from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sector import SectorLifecycle, SectorRotationCalendar
from app.models.stock import SectorInfo


@dataclass
class CalendarCell:
    """日历单元格"""
    trade_date: date
    sector_code: str
    sector_name: str
    lifecycle_state: str
    limit_up_count: int = 0
    max_height: int = 0
    fund_flow: float = 0
    is_active: bool = False
    state_change: str = "unchanged"
    leader_stocks: List[Dict] = field(default_factory=list)


@dataclass
class SectorCalendarRow:
    """板块日历行"""
    sector_code: str
    sector_name: str
    sector_type: str
    cells: List[CalendarCell] = field(default_factory=list)
    
    # 统计
    active_days: int = 0
    max_consecutive_days: int = 0
    is_main_line: bool = False


class SectorRotationCalendarEngine:
    """板块轮动日历引擎"""
    
    # 状态颜色映射(前端使用)
    STATE_COLORS = {
        "dormant": "#6b7280",       # 灰色
        "emerging": "#22c55e",      # 绿色-刚启动
        "accelerating": "#3b82f6",  # 蓝色-加速
        "climax": "#ef4444",        # 红色-高潮
        "diverging": "#f59e0b",     # 橙色-分化
        "declining": "#8b5cf6",     # 紫色-退潮
        "one_day": "#94a3b8",       # 浅灰-一日游
    }
    
    async def build_calendar(
        self,
        session: AsyncSession,
        end_date: date = None,
        days: int = 10,
        sector_type: str = None,
        min_active_days: int = 1,
    ) -> Dict:
        """构建板块轮动日历
        
        Args:
            end_date: 结束日期(默认最近交易日)
            days: 展示天数(默认10天)
            sector_type: 板块类型过滤(concept/industry)
            min_active_days: 最少活跃天数(过滤一直休眠的)
        """
        if end_date is None:
            end_date = date.today()
        
        start_date = end_date - timedelta(days=days + 5)  # 多取几天确保覆盖交易日
        
        # 1. 获取所有板块
        query = select(SectorInfo).where(
            SectorInfo.source == "pywencai"
        )
        if sector_type:
            query = query.where(SectorInfo.sector_type == sector_type)
        
        result = await session.execute(query)
        sectors = result.scalars().all()
        
        # 2. 获取日期范围内的生命周期数据
        result = await session.execute(
            select(SectorLifecycle).where(
                and_(
                    SectorLifecycle.trade_date >= start_date,
                    SectorLifecycle.trade_date <= end_date,
                )
            ).order_by(desc(SectorLifecycle.trade_date))
        )
        lifecycle_records = result.scalars().all()
        
        # 3. 构建日期列表(去重排序)
        date_set = set(r.trade_date for r in lifecycle_records)
        date_list = sorted(list(date_set), reverse=True)[:days]  # 取最近days天
        date_list.reverse()  # 正序排列
        
        # 4. 按板块分组数据
        sector_data_map: Dict[str, Dict[date, SectorLifecycle]] = {}
        for record in lifecycle_records:
            if record.sector_code not in sector_data_map:
                sector_data_map[record.sector_code] = {}
            sector_data_map[record.sector_code][record.trade_date] = record
        
        # 5. 构建日历行
        rows = []
        for sector in sectors:
            row = await self._build_sector_row(
                sector, date_list, sector_data_map.get(sector.sector_code, {})
            )
            
            # 过滤: 至少活跃min_active_days天
            if row.active_days >= min_active_days:
                rows.append(row)
        
        # 6. 排序: 主线优先,然后按活跃天数
        rows.sort(key=lambda r: (r.is_main_line, r.active_days), reverse=True)
        
        return {
            "dates": [d.isoformat() for d in date_list],
            "rows": [
                {
                    "sector_code": r.sector_code,
                    "sector_name": r.sector_name,
                    "sector_type": r.sector_type,
                    "active_days": r.active_days,
                    "max_consecutive_days": r.max_consecutive_days,
                    "is_main_line": r.is_main_line,
                    "cells": [
                        {
                            "date": c.trade_date.isoformat(),
                            "state": c.lifecycle_state,
                            "state_color": self.STATE_COLORS.get(c.lifecycle_state, "#6b7280"),
                            "limit_up_count": c.limit_up_count,
                            "max_height": c.max_height,
                            "fund_flow": round(c.fund_flow, 2),
                            "is_active": c.is_active,
                            "state_change": c.state_change,
                            "leader_stocks": c.leader_stocks,
                        }
                        for c in r.cells
                    ],
                }
                for r in rows
            ],
            "state_colors": self.STATE_COLORS,
        }
    
    async def _build_sector_row(
        self,
        sector: SectorInfo,
        date_list: List[date],
        data_map: Dict[date, SectorLifecycle],
    ) -> SectorCalendarRow:
        """构建单个板块的日历行"""
        row = SectorCalendarRow(
            sector_code=sector.sector_code,
            sector_name=sector.sector_name,
            sector_type=sector.sector_type,
        )
        
        prev_state = None
        consecutive_count = 0
        max_consecutive = 0
        
        for d in date_list:
            record = data_map.get(d)
            
            if record:
                cell = CalendarCell(
                    trade_date=d,
                    sector_code=sector.sector_code,
                    sector_name=sector.sector_name,
                    lifecycle_state=record.lifecycle_state,
                    limit_up_count=record.limit_up_count,
                    max_height=record.max_board_height,
                    fund_flow=record.fund_flow,
                    is_active=record.limit_up_count > 0,
                    leader_stocks=self._parse_json(record.leader_stocks),
                )
                
                # 判断状态变化
                if prev_state is None:
                    cell.state_change = "new"
                elif record.lifecycle_state != prev_state:
                    if self._state_score(record.lifecycle_state) > self._state_score(prev_state):
                        cell.state_change = "upgraded"
                    else:
                        cell.state_change = "downgraded"
                else:
                    cell.state_change = "unchanged"
                
                prev_state = record.lifecycle_state
                
                # 统计活跃天数
                if record.limit_up_count > 0:
                    row.active_days += 1
                    consecutive_count += 1
                    max_consecutive = max(max_consecutive, consecutive_count)
                else:
                    consecutive_count = 0
                    
                if record.is_main_line:
                    row.is_main_line = True
            else:
                # 无数据=休眠
                cell = CalendarCell(
                    trade_date=d,
                    sector_code=sector.sector_code,
                    sector_name=sector.sector_name,
                    lifecycle_state="dormant",
                    is_active=False,
                    state_change="unchanged" if prev_state == "dormant" else "downgraded",
                )
                prev_state = "dormant"
                consecutive_count = 0
            
            row.cells.append(cell)
        
        row.max_consecutive_days = max_consecutive
        return row
    
    def _state_score(self, state: str) -> int:
        """状态分数(用于比较)"""
        scores = {
            "dormant": 0,
            "one_day": 10,
            "declining": 20,
            "diverging": 40,
            "emerging": 60,
            "accelerating": 80,
            "climax": 100,
        }
        return scores.get(state, 0)
    
    def _parse_json(self, json_str: Optional[str]) -> List[Dict]:
        """解析JSON字符串"""
        if not json_str:
            return []
        try:
            import json
            return json.loads(json_str)
        except:
            return []
    
    async def get_sector_detail_timeline(
        self,
        session: AsyncSession,
        sector_code: str,
        days: int = 20,
    ) -> List[Dict]:
        """获取单个板块的详细时间线"""
        end_date = date.today()
        start_date = end_date - timedelta(days=days)
        
        result = await session.execute(
            select(SectorLifecycle).where(
                and_(
                    SectorLifecycle.sector_code == sector_code,
                    SectorLifecycle.trade_date >= start_date,
                    SectorLifecycle.trade_date <= end_date,
                )
            ).order_by(desc(SectorLifecycle.trade_date))
        )
        records = result.scalars().all()
        
        return [
            {
                "date": r.trade_date.isoformat(),
                "state": r.lifecycle_state,
                "state_score": r.state_score,
                "limit_up_count": r.limit_up_count,
                "first_board_count": r.first_board_count,
                "consecutive_board_count": r.consecutive_board_count,
                "max_board_height": r.max_board_height,
                "ladders": self._parse_json(r.ladder_stocks),
                "leader_stocks": self._parse_json(r.leader_stocks),
                "fund_flow": r.fund_flow,
                "quality_score": r.quality_score,
                "is_main_line": r.is_main_line,
            }
            for r in records
        ]
