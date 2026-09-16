"""板块营地 API v2 — 生命周期+轮动日历+主线识别

重构后的板块营地API:
- /lifecycle: 板块生命周期状态(刚启动/加速/高潮/分化/退潮)
- /calendar: 板块轮动日历(日线级别活跃史)
- /main-lines: 主线板块追踪
- /detail/{code}: 单个板块详情(含连板梯队)
- /emerging: 刚启动板块列表
- /climax: 高潮板块列表
- /declining: 退潮板块列表
"""

from datetime import date
from typing import Optional, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_date import resolve_latest_trade_date
from app.db.session import get_db
from app.models.sector import SectorLifecycle, SectorMainLine
from app.models.stock import SectorInfo, SectorPersistence
from app.sector.lifecycle import SectorLifecycleEngine, LifecycleState
from app.sector.rotation_calendar import SectorRotationCalendarEngine
from app.sector.main_line import MainLineDetector

router = APIRouter()

# 引擎实例
lifecycle_engine = SectorLifecycleEngine()
calendar_engine = SectorRotationCalendarEngine()
main_line_detector = MainLineDetector()


async def _latest_lifecycle_trade_date(db: AsyncSession) -> date:
    return await resolve_latest_trade_date(db, SectorLifecycle.trade_date)


@router.get("/lifecycle")
async def get_sector_lifecycle(
    trade_date: Optional[str] = Query(None, description="交易日期 YYYY-MM-DD"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    state: Optional[str] = Query(None, description="状态过滤: emerging/accelerating/climax/diverging/declining"),
    min_quality: int = Query(0, ge=0, le=100, description="最低质量分"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """获取板块生命周期状态(实时计算 + DB缓存双模式)

    数据策略:
    - 优先读 SectorLifecycle 预计算表(快)
    - 若表中无数据或记录数<10 → 调用引擎实时计算(从Persistence+LimitUpPool+K线)
    - 实时计算结果不回写DB(避免脏数据), 直接返回
    
    字段说明:
    - lifecycle_state: 生命周期状态(dormant/emerging/accelerating/climax/diverging/declining/one_day)
    - strength_score: 板块综合强度(连续0-100, 基于资金流+涨跌幅+涨停数)
    - change_pct: 涨跌幅(%)
    - limit_up_count/first_board_count/consecutive_board_count: 涨停/首板/连板数
    - max_board_height: 最高连板高度
    - leader_stocks: 龙头股列表 [{code,name,height}]
    - fund_flow: 当日资金净流入(亿)
    - quality_score: 质量分(梯队完整度+龙头+资金, 0-100)
    """
    if trade_date:
        d = date.fromisoformat(trade_date)
    else:
        result = await db.execute(
            select(SectorLifecycle.trade_date).order_by(desc(SectorLifecycle.trade_date)).limit(1)
        )
        latest = result.scalar()
        if latest:
            d = latest
        else:
            persist_result = await db.execute(
                select(SectorPersistence.trade_date).order_by(desc(SectorPersistence.trade_date)).limit(1)
            )
            d = persist_result.scalar() or date.today()
    
    # ===== 尝试读取预计算表 =====
    query = select(SectorLifecycle).where(SectorLifecycle.trade_date == d)

    if sector_type:
        query = query.where(SectorLifecycle.sector_type == sector_type)
    if state:
        query = query.where(SectorLifecycle.lifecycle_state == state)
    if min_quality > 0:
        query = query.where(SectorLifecycle.quality_score >= min_quality)

    query = query.order_by(desc(SectorLifecycle.quality_score))
    
    db_result = await db.execute(query)
    db_records = db_result.scalars().all()

    items = []
    
    # ===== DB有数据(>=10条) → 用缓存 =====
    if len(db_records) >= 10:
        import json

        persist_res = await db.execute(
            select(SectorPersistence.sector_code, SectorPersistence.strength_score, SectorPersistence.change_pct)
            .where(SectorPersistence.trade_date == d)
        )
        persist_map = {}
        change_map = {}
        for r in persist_res.all():
            persist_map[r[0]] = r[1]
            change_map[r[0]] = r[2]

        for r in db_records:
            items.append({
                "sector_code": r.sector_code,
                "sector_name": r.sector_name,
                "sector_type": r.sector_type,
                "trade_date": r.trade_date.isoformat(),
                "lifecycle_state": r.lifecycle_state,
                "state_score": r.state_score,
                "strength_score": persist_map.get(r.sector_code, 0),
                "change_pct": round(change_map.get(r.sector_code, 0) or 0, 2),
                "limit_up_count": r.limit_up_count or 0,
                "first_board_count": r.first_board_count or 0,
                "consecutive_board_count": r.consecutive_board_count or 0,
                "max_board_height": r.max_board_height or 0,
                "ladders": json.loads(r.ladder_stocks) if r.ladder_stocks else [],
                "leader_stocks": json.loads(r.leader_stocks) if r.leader_stocks else [],
                "fund_flow": round(r.fund_flow, 2) if r.fund_flow is not None else None,
                "fund_flow_3d": round(r.fund_flow_3d, 2) if r.fund_flow_3d is not None else None,
                "active_days": r.active_days or 0,
                "total_active_5d": r.total_active_5d or 0,
                "quality_score": round(r.quality_score, 1) if r.quality_score is not None else None,
                "is_main_line": r.is_main_line == 1,
            })

    # ===== DB无数据或太少 → 引擎实时计算 =====
    else:
        # 从SectorPersistence取有数据的板块列表(按strength排序, 最多150个)
        persist_q = select(
            SectorPersistence.sector_code,
            SectorPersistence.sector_name,
            SectorPersistence.strength_score,
            SectorPersistence.change_pct,
        ).where(
            SectorPersistence.trade_date == d,
        ).order_by(desc(SectorPersistence.strength_score)).limit(150)
        
        persist_res = await db.execute(persist_q)
        sector_rows = persist_res.all()
        
        if not sector_rows:
            return {"trade_date": d.isoformat(), "total": 0, "items": [], "state_stats": {}}
        
        # 获取板块类型映射
        si_res = await db.execute(select(SectorInfo.sector_code, SectorInfo.sector_type))
        si_map = {r[0]: r[1] for r in si_res.all()}
        
        # 并行调用引擎分析(限制并发数避免过慢)
        analyzed = []
        for row in sector_rows:
            code = row[0]
            name = row[1]
            p_strength = row[2] or 0
            p_change = row[3] or 0
            stype = si_map.get(code, "concept")
            
            if sector_type and stype != sector_type:
                continue
            
            data = await lifecycle_engine.analyze_sector(db, code, name, stype, d)
            
            # 状态过滤
            if state and data.lifecycle_state.value != state:
                continue
            
            # 质量分过滤
            if min_quality > 0 and data.quality_score < min_quality:
                continue
            
            analyzed.append((data.quality_score or 0, {
                "sector_code": code,
                "sector_name": name,
                "sector_type": stype,
                "trade_date": d.isoformat(),
                "lifecycle_state": data.lifecycle_state.value,
                "state_score": lifecycle_engine.state_scores.get(data.lifecycle_state, 0),
                "strength_score": round(p_strength, 1) if p_strength else 0,
                "change_pct": round(p_change, 2) if p_change else 0,
                "limit_up_count": data.limit_up_count,
                "first_board_count": data.first_board_count,
                "consecutive_board_count": data.consecutive_board_count,
                "max_board_height": data.max_board_height,
                "ladders": data.ladders,
                "leader_stocks": data.leader_stocks,
                "fund_flow": round(data.fund_flow, 2) if data.fund_flow else None,
                "fund_flow_3d": round(data.fund_flow_3d, 2) if data.fund_flow_3d else None,
                "active_days": data.active_days,
                "quality_score": round(data.quality_score, 1) if data.quality_score else None,
                "is_main_line": data.is_main_line,
            }))
        
        # 按质量分排序
        analyzed.sort(key=lambda x: x[0], reverse=True)
        items = [x[1] for x in analyzed]
    
    # 分页
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]
    
    # 按状态分组统计
    state_stats = {}
    for item in items:
        s = item["lifecycle_state"]
        if s not in state_stats:
            state_stats[s] = 0
        state_stats[s] += 1
    
    return {
        "trade_date": d.isoformat(),
        "total": total,
        "page": page,
        "page_size": page_size,
        "state_stats": state_stats,
        "items": page_items,
    }


@router.get("/calendar")
async def get_rotation_calendar(
    days: int = Query(10, ge=5, le=30, description="展示天数"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    min_active_days: int = Query(1, ge=0, description="最少活跃天数"),
    db: AsyncSession = Depends(get_db),
):
    """获取板块轮动日历
    
    以日历形式展示最近N天各板块的状态变化:
    - 横向: 日期轴
    - 纵向: 板块列表
    - 单元格: 当日状态(颜色)+涨停数
    
    用于识别:
    - 一日游板块(单日活跃后消失)
    - 主线板块(连续多日活跃)
    - 板块切换节奏
    """
    calendar = await calendar_engine.build_calendar(
        session=db,
        days=days,
        sector_type=sector_type,
        min_active_days=min_active_days,
    )
    return calendar


@router.get("/main-lines")
async def get_main_lines(
    include_ended: bool = Query(False, description="包含近期结束的主线"),
    db: AsyncSession = Depends(get_db),
):
    """获取主线板块列表
    
    主线判定标准:
    - 连续>=3天处于活跃状态
    - 有完整连板梯队
    - 龙头高度>=3板
    - 板块质量分>=60
    """
    trade_date = await _latest_lifecycle_trade_date(db)
    main_lines = await main_line_detector.detect_main_lines(db, trade_date)
    
    result = {
        "trade_date": trade_date.isoformat(),
        "active_count": len([ml for ml in main_lines if ml.status == "active"]),
        "main_lines": [
            {
                "sector_code": ml.sector_code,
                "sector_name": ml.sector_name,
                "sector_type": ml.sector_type,
                "status": ml.status,
                "start_date": ml.start_date.isoformat() if ml.start_date else None,
                "end_date": ml.end_date.isoformat() if ml.end_date else None,
                "duration_days": ml.duration_days,
                "current_state": ml.current_state,
                "max_height": ml.max_height,
                "total_limit_up": ml.total_limit_up,
                "avg_fund_flow": ml.avg_fund_flow,
                "leader": {
                    "code": ml.leader_stock,
                    "name": ml.leader_name,
                    "max_height": ml.leader_max_height,
                },
                "current_ladders": ml.current_ladders,
            }
            for ml in main_lines
        ],
    }
    
    if include_ended:
        summary = await main_line_detector.get_main_line_summary(db)
        result["recent_ended"] = summary.get("recent_ended", [])
    
    return result


@router.get("/detail/{sector_code}")
async def get_sector_detail(
    sector_code: str,
    days: int = Query(20, ge=5, le=60, description="历史天数"),
    db: AsyncSession = Depends(get_db),
):
    """获取单个板块详情
    
    包含:
    - 生命周期时间线
    - 连板梯队历史
    - 龙头股变化
    - 资金流向
    """
    # 获取板块信息
    result = await db.execute(
        select(SectorInfo).where(SectorInfo.sector_code == sector_code)
    )
    sector = result.scalar_one_or_none()
    
    if not sector:
        return {"error": "板块不存在"}
    
    # 获取时间线
    timeline = await calendar_engine.get_sector_detail_timeline(
        db, sector_code, days
    )
    
    # 获取当前状态
    result = await db.execute(
        select(SectorLifecycle).where(
            and_(
                SectorLifecycle.sector_code == sector_code,
            )
        ).order_by(desc(SectorLifecycle.trade_date)).limit(1)
    )
    current = result.scalar_one_or_none()
    
    import json
    return {
        "sector_code": sector_code,
        "sector_name": sector.sector_name if sector else "",
        "sector_type": sector.sector_type if sector else "",
        "current_state": current.lifecycle_state if current else "unknown",
        "current_quality_score": current.quality_score if current else 0,
        "current_ladders": json.loads(current.ladder_stocks) if current and current.ladder_stocks else [],
        "current_leaders": json.loads(current.leader_stocks) if current and current.leader_stocks else [],
        "timeline": timeline,
    }


@router.get("/emerging")
async def get_emerging_sectors(
    min_fund_flow: float = Query(1.0, description="最小资金流入(亿)"),
    db: AsyncSession = Depends(get_db),
):
    """获取刚启动板块(机会挖掘)
    
    筛选条件:
    - 状态为emerging
    - 资金流入>=阈值
    - 有连板股出现
    """
    trade_date = await _latest_lifecycle_trade_date(db)
    result = await db.execute(
        select(SectorLifecycle).where(
            and_(
                SectorLifecycle.trade_date == trade_date,
                SectorLifecycle.lifecycle_state == "emerging",
                SectorLifecycle.fund_flow >= min_fund_flow,
                SectorLifecycle.consecutive_board_count >= 1,
            )
        ).order_by(desc(SectorLifecycle.quality_score))
    )
    records = result.scalars().all()
    
    import json
    return {
        "trade_date": trade_date.isoformat(),
        "count": len(records),
        "sectors": [
            {
                "code": r.sector_code,
                "name": r.sector_name,
                "limit_up_count": r.limit_up_count,
                "consecutive_count": r.consecutive_board_count,
                "max_height": r.max_board_height,
                "fund_flow": r.fund_flow,
                "quality_score": r.quality_score,
                "leaders": json.loads(r.leader_stocks) if r.leader_stocks else [],
            }
            for r in records
        ],
    }


@router.get("/climax")
async def get_climax_sectors(
    db: AsyncSession = Depends(get_db),
):
    """获取高潮板块(风险提示)
    
    高潮板块特征:
    - 批量涨停>=10只
    - 龙头高度>=5板
    - 注意分化风险
    """
    trade_date = await _latest_lifecycle_trade_date(db)
    result = await db.execute(
        select(SectorLifecycle).where(
            and_(
                SectorLifecycle.trade_date == trade_date,
                SectorLifecycle.lifecycle_state == "climax",
            )
        ).order_by(desc(SectorLifecycle.max_board_height))
    )
    records = result.scalars().all()
    
    import json
    return {
        "trade_date": trade_date.isoformat(),
        "count": len(records),
        "warning": "高潮板块注意分化风险，谨慎追高",
        "sectors": [
            {
                "code": r.sector_code,
                "name": r.sector_name,
                "limit_up_count": r.limit_up_count,
                "max_height": r.max_board_height,
                "consecutive_count": r.consecutive_board_count,
                "leaders": json.loads(r.leader_stocks) if r.leader_stocks else [],
                "fund_flow": r.fund_flow,
            }
            for r in records
        ],
    }


@router.get("/declining")
async def get_declining_sectors(
    db: AsyncSession = Depends(get_db),
):
    """获取退潮板块(回避提示)
    
    退潮板块特征:
    - 龙头断板
    - 批量跌停
    - 资金大幅流出
    """
    trade_date = await _latest_lifecycle_trade_date(db)
    result = await db.execute(
        select(SectorLifecycle).where(
            and_(
                SectorLifecycle.trade_date == trade_date,
                SectorLifecycle.lifecycle_state.in_(["declining", "diverging"]),
            )
        ).order_by(SectorLifecycle.fund_flow)  # 资金流出多的排前面
    )
    records = result.scalars().all()
    
    return {
        "trade_date": trade_date.isoformat(),
        "count": len(records),
        "warning": "退潮板块建议回避，勿接飞刀",
        "sectors": [
            {
                "code": r.sector_code,
                "name": r.sector_name,
                "state": r.lifecycle_state,
                "limit_up_count": r.limit_up_count,
                "fund_flow": r.fund_flow,
                "active_days": r.active_days,
            }
            for r in records
        ],
    }


@router.get("/one-day")
async def get_one_day_sectors(
    db: AsyncSession = Depends(get_db),
):
    """获取一日游板块(避坑指南)
    
    一日游特征:
    - 前日首板潮，今日无持续
    - 无连板股
    """
    trade_date = await _latest_lifecycle_trade_date(db)
    result = await db.execute(
        select(SectorLifecycle).where(
            and_(
                SectorLifecycle.trade_date == trade_date,
                SectorLifecycle.lifecycle_state == "one_day",
            )
        )
    )
    records = result.scalars().all()
    
    return {
        "trade_date": trade_date.isoformat(),
        "count": len(records),
        "warning": "一日游板块次日无溢价，避免追高",
        "sectors": [
            {
                "code": r.sector_code,
                "name": r.sector_name,
                "limit_up_count": r.limit_up_count,
                "prev_first_board": r.first_board_count,
            }
            for r in records
        ],
    }
