"""主线板块识别与追踪

主线判定标准:
1. 连续>=3天处于emerging/accelerating/climax状态
2. 有完整连板梯队(>=3只连板,高度>=3)
3. 有明确龙头股(最高板且封板质量高)
4. 近5天涨停总数>=20只
5. 板块质量分>=60

主线生命周期:
- 启动期: 首次识别为主线(1-2天)
- 主升期: 连续活跃>=3天,龙头高度>=5
- 震荡期: 状态在climax/diverging之间切换
- 结束: 状态变为declining或连续2天无涨停
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Dict, Optional

from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sector import SectorMainLine, SectorLifecycle


@dataclass
class MainLineSector:
    """主线板块数据"""
    sector_code: str
    sector_name: str
    sector_type: str
    
    # 主线周期
    start_date: date
    end_date: Optional[date] = None
    duration_days: int = 0
    
    # 强度指标
    max_height: int = 0
    total_limit_up: int = 0
    avg_fund_flow: float = 0
    current_state: str = ""
    
    # 龙头股
    leader_stock: str = ""
    leader_name: str = ""
    leader_max_height: int = 0
    
    # 当前梯队
    current_ladders: List[Dict] = field(default_factory=list)
    
    # 状态
    status: str = "active"  # active/ended


class MainLineDetector:
    """主线板块检测器"""
    
    async def detect_main_lines(
        self,
        session: AsyncSession,
        trade_date: date = None,
    ) -> List[MainLineSector]:
        """检测当前主线板块"""
        if trade_date is None:
            trade_date = date.today()
        
        # 1. 获取当日处于活跃状态且有is_main_line标记的板块
        result = await session.execute(
            select(SectorLifecycle).where(
                and_(
                    SectorLifecycle.trade_date == trade_date,
                    SectorLifecycle.is_main_line == 1,
                )
            ).order_by(desc(SectorLifecycle.quality_score))
        )
        records = result.scalars().all()
        
        main_lines = []
        for record in records:
            ml = await self._build_main_line(session, record, trade_date)
            main_lines.append(ml)
        
        return main_lines
    
    async def _build_main_line(
        self,
        session: AsyncSession,
        record: SectorLifecycle,
        trade_date: date,
    ) -> MainLineSector:
        """构建主线板块数据"""
        ml = MainLineSector(
            sector_code=record.sector_code,
            sector_name=record.sector_name,
            sector_type=record.sector_type,
            start_date=trade_date,  # 需要查历史确定真实启动日
            current_state=record.lifecycle_state,
            max_height=record.max_board_height,
        )
        
        # 解析龙头股
        import json
        try:
            leaders = json.loads(record.leader_stocks) if record.leader_stocks else []
            if leaders:
                ml.leader_stock = leaders[0].get("code", "")
                ml.leader_name = leaders[0].get("name", "")
                ml.leader_max_height = leaders[0].get("height", 0)
        except:
            pass
        
        # 解析梯队
        try:
            ml.current_ladders = json.loads(record.ladder_stocks) if record.ladder_stocks else []
        except:
            ml.current_ladders = []
        
        # 查历史确定启动日期和统计
        start_date, stats = await self._get_main_line_history(
            session, record.sector_code, trade_date
        )
        ml.start_date = start_date
        ml.duration_days = (trade_date - start_date).days + 1
        ml.total_limit_up = stats.get("total_limit_up", 0)
        ml.avg_fund_flow = stats.get("avg_fund_flow", 0)
        ml.max_height = stats.get("max_height", record.max_board_height)
        
        # 检查是否已结束
        if record.lifecycle_state == "declining":
            ml.status = "ended"
            ml.end_date = trade_date
        
        return ml
    
    async def _get_main_line_history(
        self,
        session: AsyncSession,
        sector_code: str,
        end_date: date,
    ) -> tuple:
        """获取主线板块历史"""
        # 取最近20天数据
        start_query = end_date - timedelta(days=20)
        
        result = await session.execute(
            select(SectorLifecycle).where(
                and_(
                    SectorLifecycle.sector_code == sector_code,
                    SectorLifecycle.trade_date >= start_query,
                    SectorLifecycle.trade_date <= end_date,
                )
            ).order_by(SectorLifecycle.trade_date)
        )
        records = result.scalars().all()
        
        if not records:
            return end_date, {"total_limit_up": 0, "avg_fund_flow": 0, "max_height": 0}
        
        # 找启动日期(第一个emerging状态)
        start_date = records[0].trade_date
        for r in records:
            if r.lifecycle_state in ["emerging", "accelerating", "climax"]:
                start_date = r.trade_date
                break
        
        # 统计
        total_limit_up = sum(r.limit_up_count for r in records)
        avg_fund_flow = sum(r.fund_flow for r in records) / len(records) if records else 0
        max_height = max(r.max_board_height for r in records) if records else 0
        
        return start_date, {
            "total_limit_up": total_limit_up,
            "avg_fund_flow": round(avg_fund_flow, 2),
            "max_height": max_height,
        }
    
    async def save_main_line(
        self,
        session: AsyncSession,
        main_line: MainLineSector,
    ):
        """保存主线板块记录"""
        # 检查是否已存在
        result = await session.execute(
            select(SectorMainLine).where(
                and_(
                    SectorMainLine.sector_code == main_line.sector_code,
                    SectorMainLine.status == "active",
                )
            )
        )
        existing = result.scalar_one_or_none()
        
        if existing:
            # 更新
            existing.end_date = main_line.end_date
            existing.duration_days = main_line.duration_days
            existing.max_height = main_line.max_height
            existing.total_limit_up = main_line.total_limit_up
            existing.avg_fund_flow = main_line.avg_fund_flow
            existing.leader_stock = main_line.leader_stock
            existing.leader_name = main_line.leader_name
            existing.leader_max_height = main_line.leader_max_height
            existing.status = main_line.status
        else:
            # 新建
            session.add(SectorMainLine(
                sector_code=main_line.sector_code,
                sector_name=main_line.sector_name,
                sector_type=main_line.sector_type,
                start_date=main_line.start_date,
                end_date=main_line.end_date,
                duration_days=main_line.duration_days,
                max_height=main_line.max_height,
                total_limit_up=main_line.total_limit_up,
                avg_fund_flow=main_line.avg_fund_flow,
                leader_stock=main_line.leader_stock,
                leader_name=main_line.leader_name,
                leader_max_height=main_line.leader_max_height,
                status=main_line.status,
            ))
        
        await session.commit()
    
    async def get_main_line_summary(
        self,
        session: AsyncSession,
    ) -> Dict:
        """获取主线板块汇总"""
        # 当前主线
        result = await session.execute(
            select(SectorMainLine).where(SectorMainLine.status == "active")
        )
        active_lines = result.scalars().all()
        
        # 近期结束的主线(7天内)
        from datetime import datetime
        week_ago = datetime.now() - timedelta(days=7)
        result = await session.execute(
            select(SectorMainLine).where(
                and_(
                    SectorMainLine.status == "ended",
                    SectorMainLine.updated_at >= week_ago,
                )
            )
        )
        recent_ended = result.scalars().all()
        
        return {
            "active_main_lines": [
                {
                    "code": ml.sector_code,
                    "name": ml.sector_name,
                    "type": ml.sector_type,
                    "start_date": ml.start_date.isoformat() if ml.start_date else None,
                    "duration_days": ml.duration_days,
                    "max_height": ml.max_height,
                    "leader": f"{ml.leader_name}({ml.leader_stock})",
                    "leader_height": ml.leader_max_height,
                }
                for ml in active_lines
            ],
            "recent_ended": [
                {
                    "code": ml.sector_code,
                    "name": ml.sector_name,
                    "start_date": ml.start_date.isoformat() if ml.start_date else None,
                    "end_date": ml.end_date.isoformat() if ml.end_date else None,
                    "duration_days": ml.duration_days,
                    "end_reason": ml.end_reason,
                }
                for ml in recent_ended
            ],
            "total_active": len(active_lines),
        }
