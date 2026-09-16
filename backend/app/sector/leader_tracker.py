"""板块龙头股追踪

龙头股判定标准:
1. 同板块内连板高度最高
2. 封板时间最早(日内强度)
3. 封单金额最大(资金认可度)
4. 流通市值适中(50-200亿为佳)

龙头股生命周期:
- 首板: 启动信号，尚不确定
- 2-3板: 确立龙头地位
- 4-5板: 主升浪，板块跟风
- 6板+: 妖股阶段，独立走势
- 断板: 龙头结束，板块退潮信号
"""

from dataclasses import dataclass
from datetime import date
from typing import List, Dict, Optional

from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock import LimitUpPool, StockDaily, StockTag


@dataclass
class SectorLeader:
    """板块龙头股"""
    code: str
    name: str
    sector_code: str
    sector_name: str
    
    # 连板数据
    board_height: int = 0           # 当前连板数
    first_board_date: date = None   # 首板日期
    limit_up_times: List[str] = None  # 每日涨停时间
    
    # 强度指标
    avg_seal_amount: float = 0      # 平均封单金额
    total_seal_amount: float = 0    # 累计封单
    first_seal_rank: int = 0        # 日内封板时间排名
    
    # 市值
    market_cap: float = 0           # 流通市值(亿)
    
    # 状态
    is_leader: bool = False         # 是否确立龙头
    is_broken: bool = False         # 是否已断板


class SectorLeaderTracker:
    """板块龙头股追踪器"""
    
    async def track_sector_leaders(
        self,
        session: AsyncSession,
        sector_code: str,
        sector_name: str,
        trade_date: date,
        days: int = 10,
    ) -> List[SectorLeader]:
        """追踪板块龙头股"""
        
        # 获取该板块最近N天的涨停股
        start_date = trade_date - __import__('datetime').timedelta(days=days)
        
        result = await session.execute(
            select(LimitUpPool).where(
                and_(
                    LimitUpPool.trade_date >= start_date,
                    LimitUpPool.trade_date <= trade_date,
                    LimitUpPool.limit_up_reason.like(f"%{sector_name}%"),
                )
            ).order_by(desc(LimitUpPool.consecutive_days), LimitUpPool.limit_up_time)
        )
        records = result.scalars().all()
        
        # 按股票分组
        stock_map: Dict[str, List[LimitUpPool]] = {}
        for r in records:
            if r.code not in stock_map:
                stock_map[r.code] = []
            stock_map[r.code].append(r)
        
        # 构建龙头股数据
        leaders = []
        for code, lu_list in stock_map.items():
            lu_list.sort(key=lambda x: x.trade_date)
            latest = lu_list[-1]
            
            leader = SectorLeader(
                code=code,
                name=latest.name,
                sector_code=sector_code,
                sector_name=sector_name,
                board_height=latest.consecutive_days or 1,
                first_board_date=lu_list[0].trade_date,
                limit_up_times=[lu.limit_up_time for lu in lu_list if lu.limit_up_time],
                total_seal_amount=sum(lu.seal_amount or 0 for lu in lu_list),
            )
            
            # 计算平均封单
            seal_amounts = [lu.seal_amount for lu in lu_list if lu.seal_amount]
            if seal_amounts:
                leader.avg_seal_amount = sum(seal_amounts) / len(seal_amounts)
            
            # 获取市值
            await self._fetch_market_cap(session, leader, trade_date)
            
            # 判定是否龙头(高度>=2且封单充足)
            leader.is_leader = leader.board_height >= 2 and leader.avg_seal_amount > 10000000
            
            leaders.append(leader)
        
        # 按高度排序
        leaders.sort(key=lambda x: (x.board_height, x.avg_seal_amount), reverse=True)
        
        return leaders
    
    async def _fetch_market_cap(
        self,
        session: AsyncSession,
        leader: SectorLeader,
        trade_date: date,
    ):
        """获取股票市值 — 优先从StockSpot取(实时), 回退到BoardCons"""
        # 优先: StockSpot(实时, 有流通市值)
        try:
            from app.models.stock import StockSpot
            result = await session.execute(
                select(StockSpot.circ_market_cap).where(StockSpot.code == leader.code)
            )
            cap = result.scalar_one_or_none()
            if cap and cap > 0:
                leader.market_cap = cap  # DB已存亿, 直接用
                return
        except Exception:
            pass

        # 回退: BoardCons(盘后数据)
        try:
            from app.models.stock import BoardCons
            result = await session.execute(
                select(BoardCons.market_cap).where(BoardCons.code == leader.code).limit(1)
            )
            cap = result.scalar_one_or_none()
            if cap and cap > 0:
                leader.market_cap = cap  # BoardCons已存亿
                return
        except Exception:
            pass

        leader.market_cap = 0
    
    async def get_leader_ladder(
        self,
        session: AsyncSession,
        trade_date: date,
    ) -> Dict:
        """获取全市场连板梯队"""
        
        result = await session.execute(
            select(LimitUpPool).where(
                LimitUpPool.trade_date == trade_date
            ).order_by(desc(LimitUpPool.consecutive_days))
        )
        records = result.scalars().all()
        
        # 按高度分组
        ladder_map: Dict[int, List[Dict]] = {}
        for r in records:
            height = r.consecutive_days or 1
            if height not in ladder_map:
                ladder_map[height] = []
            
            ladder_map[height].append({
                "code": r.code,
                "name": r.name,
                "height": height,
                "limit_up_time": r.limit_up_time,
                "seal_amount": r.seal_amount,
                "reason": r.limit_up_reason,
            })
        
        # 构建梯队
        ladders = []
        for height in sorted(ladder_map.keys(), reverse=True):
            stocks = ladder_map[height]
            # 按封单金额排序
            stocks.sort(key=lambda x: x["seal_amount"] or 0, reverse=True)
            
            ladders.append({
                "height": height,
                "count": len(stocks),
                "stocks": stocks[:10],  # 每高度取前10
            })
        
        return {
            "trade_date": trade_date.isoformat(),
            "max_height": max(ladder_map.keys()) if ladder_map else 0,
            "total_limit_up": len(records),
            "ladders": ladders,
        }
