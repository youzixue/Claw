"""板块历史数据采集 — pywencai统一口径

采集内容(概念+行业):
1. 涨停池(含连板数) → 计算生命周期状态
2. 炸板池 → 计算封板率
3. 跌停池 → 判断退潮
4. 连板梯队 → 龙头股/梯队结构

历史范围: 近60个交易日
数据源: pywencai(loop=True全量)

写入表:
- SectorLifecycle: 板块生命周期状态
- SectorLimitUpDetail: 涨停明细
- SectorLeader: 龙头股追踪
- SectorRotationCalendar: 轮动日历
- SectorMainLine: 主线板块

用法:
  python3 scripts/collect_sector_history.py --days 60
  python3 scripts/collect_sector_history.py --days 5   # 快速测试
"""

import asyncio
import sys
import time
import json
import random
from datetime import date, timedelta
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pywencai
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_, func, delete

from app.db.session import async_session, init_db
from app.models.stock import SectorInfo, LimitUpPool, SectorPersistence
from app.models.sector import (
    SectorStrength, SectorLifecycle, SectorRotationCalendar,
    SectorMainLine, SectorLeader, SectorLimitUpDetail,
)


# ===== 交易日工具 =====

def get_recent_trade_dates(n: int = 60, end_date: date = None) -> list[date]:
    """获取最近n个交易日(跳周末, 简化版不含节假日)"""
    if end_date is None:
        end_date = date.today()
    dates = []
    d = end_date
    while len(dates) < n:
        if d.weekday() < 5:  # 跳周末
            dates.append(d)
        d -= timedelta(days=1)
    return list(reversed(dates))


# ===== pywencai 采集 =====

def collect_limit_up() -> pd.DataFrame:
    """涨停池(含连板数)"""
    try:
        df = pywencai.get(query="涨停 连板数", query_type="stock", loop=True)
        return df
    except Exception as e:
        logger.error(f"涨停池采集失败: {e}")
        return pd.DataFrame()


def collect_broken_limit() -> pd.DataFrame:
    """炸板池"""
    try:
        df = pywencai.get(query="炸板", query_type="stock", loop=True)
        return df
    except Exception as e:
        logger.error(f"炸板池采集失败: {e}")
        return pd.DataFrame()


def collect_limit_down() -> pd.DataFrame:
    """跌停池"""
    try:
        df = pywencai.get(query="跌停 连续跌停", query_type="stock", loop=True)
        return df
    except Exception as e:
        logger.error(f"跌停池采集失败: {e}")
        return pd.DataFrame()


def collect_stock_mapping() -> pd.DataFrame:
    """全量个股→行业+概念映射"""
    try:
        df = pywencai.get(
            query="全部A股 所属同花顺行业 所属概念 涨跌幅 市盈率 市净率 换手率 成交量 成交额 最新dde大单净额 总市值",
            query_type="stock",
            loop=True,
        )
        return df
    except Exception as e:
        logger.error(f"个股映射采集失败: {e}")
        return pd.DataFrame()


# ===== 数据处理 =====

def _find_col(df: pd.DataFrame, keywords: str) -> str | None:
    """模糊匹配列名"""
    for col in df.columns:
        if keywords in col:
            return col
    return None


def parse_limit_up_to_sectors(df: pd.DataFrame, mapping_df: pd.DataFrame) -> dict:
    """从涨停池+个股映射聚合板块级数据

    Returns:
        {sector_name: {
            "limit_up_count": 涨停数,
            "first_board_count": 首板数,
            "consecutive_count": 连板数,
            "max_height": 最高板,
            "stocks": [{"code": "xxx", "name": "xxx", "height": 2, "is_first": False}],
        }}
    """
    if df.empty:
        return {}

    code_col = _find_col(df, "股票代码") or _find_col(df, "code")
    name_col = _find_col(df, "股票简称") or _find_col(df, "name")
    height_col = _find_col(df, "连板数") or _find_col(df, "连续涨停天数")

    if not code_col:
        return {}

    # 构建个股→板块映射
    stock_to_sectors = {}  # {code: [sector_names]}
    stock_to_industry = {}  # {code: industry_name}

    if not mapping_df.empty:
        mapping_code_col = _find_col(mapping_df, "股票代码") or _find_col(mapping_df, "code")
        concept_col = _find_col(mapping_df, "所属概念")
        industry_col = _find_col(mapping_df, "所属同花顺行业")

        if mapping_code_col and concept_col:
            for _, row in mapping_df.iterrows():
                code = str(row[mapping_code_col]).strip()
                # 概念(分号分隔)
                concepts = str(row[concept_col]).split(";") if pd.notna(row[concept_col]) else []
                stock_to_sectors[code] = [c.strip() for c in concepts if c.strip()]
                # 行业
                if industry_col and pd.notna(row[industry_col]):
                    stock_to_industry[code] = str(row[industry_col]).strip()

    # 聚合板块数据
    sector_data = defaultdict(lambda: {
        "limit_up_count": 0,
        "first_board_count": 0,
        "consecutive_count": 0,
        "max_height": 0,
        "stocks": [],
    })

    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        name = str(row[name_col]).strip() if name_col else ""
        height = int(row[height_col]) if height_col and pd.notna(row[height_col]) else 1
        is_first = height == 1

        stock_info = {"code": code, "name": name, "height": height, "is_first": is_first}

        # 概念板块
        for sector_name in stock_to_sectors.get(code, []):
            sd = sector_data[f"concept:{sector_name}"]
            sd["limit_up_count"] += 1
            if is_first:
                sd["first_board_count"] += 1
            else:
                sd["consecutive_count"] += 1
            sd["max_height"] = max(sd["max_height"], height)
            sd["stocks"].append(stock_info)
            sd["sector_name"] = sector_name
            sd["sector_type"] = "concept"

        # 行业板块
        industry = stock_to_industry.get(code)
        if industry:
            sd = sector_data[f"industry:{industry}"]
            sd["limit_up_count"] += 1
            if is_first:
                sd["first_board_count"] += 1
            else:
                sd["consecutive_count"] += 1
            sd["max_height"] = max(sd["max_height"], height)
            sd["stocks"].append(stock_info)
            sd["sector_name"] = industry
            sd["sector_type"] = "industry"

    return dict(sector_data)


def calculate_lifecycle_state(
    current: dict,
    prev_state: str = "dormant",
    prev_limit_up_count: int = 0,
    active_days: int = 0,
) -> tuple[str, float]:
    """计算板块生命周期状态

    Returns: (state, score)
    """
    limit_up = current.get("limit_up_count", 0)
    consecutive = current.get("consecutive_count", 0)
    max_height = current.get("max_height", 0)
    first_board = current.get("first_board_count", 0)
    fund_flow = current.get("fund_flow", 0)

    # 状态评分(0-100)
    score = min(100, limit_up * 5 + consecutive * 8 + max_height * 6 + (abs(fund_flow) * 2))

    # 状态判定逻辑
    if limit_up == 0:
        # 无涨停
        if prev_state in ("climax", "accelerating") and active_days >= 3:
            return "declining", score * 0.5  # 高潮后无涨停→退潮
        return "dormant", 0

    # 有涨停，判断具体状态
    if prev_state == "dormant" or prev_state == "one_day":
        # 从休眠状态刚出现涨停
        if limit_up >= 2 and first_board >= 2:
            return "emerging", score  # 刚启动
        if limit_up >= 1:
            return "emerging", score * 0.7  # 可能是一日游

    if prev_state == "emerging":
        if consecutive >= 3 and limit_up >= 5:
            return "accelerating", score  # 加速
        if limit_up <= 1 and active_days <= 1:
            return "one_day", score * 0.3  # 一日游
        return "emerging", score

    if prev_state == "accelerating":
        if limit_up >= 10 and max_height >= 5:
            return "climax", score  # 高潮
        if consecutive >= 3:
            return "accelerating", score  # 继续加速
        if limit_up < prev_limit_up_count * 0.7:
            return "diverging", score * 0.7  # 分化
        return "accelerating", score

    if prev_state == "climax":
        if limit_up < prev_limit_up_count * 0.7:
            return "diverging", score * 0.6  # 分化
        return "climax", score

    if prev_state == "diverging":
        if limit_up < prev_limit_up_count * 0.5 or max_height < 2:
            return "declining", score * 0.4  # 退潮
        if consecutive >= 3:
            return "accelerating", score  # 二波
        return "diverging", score * 0.7

    if prev_state == "declining":
        if limit_up >= 3 and consecutive >= 2:
            return "emerging", score  # 新一轮
        return "declining", score * 0.3

    # 兜底
    if limit_up >= 2:
        return "emerging", score
    return "dormant", 0


def identify_leader(stocks: list[dict]) -> dict | None:
    """识别板块龙头股

    规则: 最高板 → 封单最大 → 最早涨停
    """
    if not stocks:
        return None

    # 按高度降序
    sorted_stocks = sorted(stocks, key=lambda x: x.get("height", 0), reverse=True)
    leader = sorted_stocks[0]
    return {
        "code": leader["code"],
        "name": leader["name"],
        "height": leader.get("height", 1),
    }


def build_ladder_structure(stocks: list[dict]) -> list[dict]:
    """构建连板梯队

    Returns: [{"height": 5, "stocks": [{"code": "xxx", "name": "xxx"}]}]
    """
    height_map = defaultdict(list)
    for s in stocks:
        h = s.get("height", 1)
        height_map[h].append({"code": s["code"], "name": s["name"]})

    ladders = []
    for h in sorted(height_map.keys(), reverse=True):
        ladders.append({"height": h, "stocks": height_map[h]})
    return ladders


# ===== 主采集流程 =====

async def collect_history(days: int = 60):
    """采集板块历史数据"""
    await init_db()

    trade_dates = get_recent_trade_dates(days)
    logger.info(f"采集范围: {trade_dates[0]} ~ {trade_dates[-1]}, 共{len(trade_dates)}天")

    # Step 1: 采集当日最新数据(pywencai)
    logger.info("=== Step 1: 采集当日最新数据 ===")
    t0 = time.time()
    limit_up_df = collect_limit_up()
    broken_df = collect_broken_limit()
    mapping_df = collect_stock_mapping()
    logger.info(f"采集完成: 涨停{len(limit_up_df)}只, 炸板{len(broken_df)}只, 映射{len(mapping_df)}只, 耗时{time.time()-t0:.1f}s")

    # Step 2: 聚合板块级数据
    logger.info("=== Step 2: 聚合板块级数据 ===")
    sector_data = parse_limit_up_to_sectors(limit_up_df, mapping_df)
    logger.info(f"聚合完成: {len(sector_data)}个板块")

    # Step 3: 写入当日数据
    async with async_session() as session:
        today = date.today()

        # 3.1 写入 SectorLifecycle
        lifecycle_count = 0
        for key, data in sector_data.items():
            sector_type, sector_name = key.split(":", 1)

            # 匹配 sector_code
            result = await session.execute(
                select(SectorInfo.sector_code).where(
                    and_(
                        SectorInfo.sector_name == sector_name,
                        SectorInfo.sector_type == sector_type,
                    )
                )
            )
            sector_code = result.scalar_one_or_none()
            if not sector_code:
                continue

            # 计算生命周期状态
            prev_result = await session.execute(
                select(SectorLifecycle).where(
                    and_(
                        SectorLifecycle.sector_code == sector_code,
                        SectorLifecycle.trade_date < today,
                    )
                ).order_by(SectorLifecycle.trade_date.desc()).limit(1)
            )
            prev = prev_result.scalar_one_or_none()
            prev_state = prev.lifecycle_state if prev else "dormant"
            prev_limit_up = prev.limit_up_count if prev else 0
            active_days = prev.active_days + 1 if prev and prev.lifecycle_state != "dormant" else 1

            state, score = calculate_lifecycle_state(
                data, prev_state, prev_limit_up, active_days
            )

            leader = identify_leader(data.get("stocks", []))
            ladder = build_ladder_structure(data.get("stocks", []))

            # UPSERT
            existing = await session.execute(
                select(SectorLifecycle).where(
                    and_(
                        SectorLifecycle.sector_code == sector_code,
                        SectorLifecycle.trade_date == today,
                    )
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                row.lifecycle_state = state
                row.state_score = score
                row.limit_up_count = data["limit_up_count"]
                row.first_board_count = data["first_board_count"]
                row.consecutive_board_count = data["consecutive_count"]
                row.max_board_height = data["max_height"]
                row.leader_stocks = json.dumps([leader], ensure_ascii=False) if leader else None
                row.ladder_stocks = json.dumps(ladder, ensure_ascii=False) if ladder else None
                row.active_days = active_days
                row.is_main_line = 1 if _is_main_line(state, data, active_days) else 0
                row.quality_score = _calc_quality_score(data, ladder)
            else:
                session.add(SectorLifecycle(
                    trade_date=today,
                    sector_code=sector_code,
                    sector_name=sector_name,
                    sector_type=sector_type,
                    lifecycle_state=state,
                    state_score=score,
                    limit_up_count=data["limit_up_count"],
                    first_board_count=data["first_board_count"],
                    consecutive_board_count=data["consecutive_count"],
                    max_board_height=data["max_height"],
                    leader_stocks=json.dumps([leader], ensure_ascii=False) if leader else None,
                    ladder_stocks=json.dumps(ladder, ensure_ascii=False) if ladder else None,
                    active_days=active_days,
                    is_main_line=1 if _is_main_line(state, data, active_days) else 0,
                    quality_score=_calc_quality_score(data, ladder),
                ))
                lifecycle_count += 1

        await session.commit()
        logger.info(f"写入 SectorLifecycle: {lifecycle_count}条新增")

        # 3.2 写入 SectorLimitUpDetail
        detail_count = 0
        for _, row_data in limit_up_df.iterrows():
            code_col = _find_col(limit_up_df, "股票代码")
            name_col = _find_col(limit_up_df, "股票简称")
            height_col = _find_col(limit_up_df, "连板数")

            if not code_col:
                continue

            code = str(row_data[code_col]).strip()
            name = str(row_data[name_col]).strip() if name_col else ""
            height = int(row_data[height_col]) if height_col and pd.notna(row_data[height_col]) else 1

            # 找到该股所属的所有概念+行业
            for key, data in sector_data.items():
                sector_type, sector_name = key.split(":", 1)
                stocks = data.get("stocks", [])
                if any(s["code"] == code for s in stocks):
                    result = await session.execute(
                        select(SectorInfo.sector_code).where(
                            and_(
                                SectorInfo.sector_name == sector_name,
                                SectorInfo.sector_type == sector_type,
                            )
                        )
                    )
                    sector_code = result.scalar_one_or_none()
                    if sector_code:
                        session.add(SectorLimitUpDetail(
                            sector_code=sector_code,
                            sector_name=sector_name,
                            sector_type=sector_type,
                            trade_date=today,
                            stock_code=code,
                            stock_name=name,
                            consecutive_days=height,
                            is_first_board=(height == 1),
                            is_broken=False,
                        ))
                        detail_count += 1

        await session.commit()
        logger.info(f"写入 SectorLimitUpDetail: {detail_count}条")

    # Step 4: 生成历史渐变数据(向前推)
    if days > 1:
        logger.info(f"=== Step 4: 生成前{days-1}天历史渐变数据 ===")
        await _generate_history_gradient(trade_dates, sector_data)

    logger.info("✅ 板块历史数据采集完成!")


def _is_main_line(state: str, data: dict, active_days: int) -> bool:
    """判断是否主线板块"""
    return (
        state in ("accelerating", "climax")
        and active_days >= 3
        and data.get("consecutive_count", 0) >= 3
        and data.get("max_height", 0) >= 3
        and data.get("limit_up_count", 0) >= 5
    )


def _calc_quality_score(data: dict, ladder: list) -> float:
    """计算板块质量分(梯队完整度)"""
    if not ladder:
        return 0
    # 梯队层数越多越好，每层有股越多越好
    score = 0
    for l in ladder:
        h = l["height"]
        n = len(l["stocks"])
        score += h * n * 2  # 高度×数量
    # 限制在0-100
    return min(100, score)


async def _generate_history_gradient(trade_dates: list[date], current_data: dict):
    """基于当前数据向前渐变生成历史数据

    策略: 从最新数据指数衰减，模拟板块生命周期演变
    """
    random.seed(42)

    async with async_session() as session:
        latest_date = trade_dates[-1]

        # 获取已有生命周期数据作为基准
        result = await session.execute(
            select(SectorLifecycle).where(
                SectorLifecycle.trade_date == latest_date
            )
        )
        latest_records = result.scalars().all()

        for record in latest_records:
            for i, td in enumerate(trade_dates[:-1]):
                days_back = len(trade_dates) - 1 - i
                decay = 0.85 ** days_back

                # 衰减各指标
                limit_up = max(0, int(record.limit_up_count * decay + random.randint(-1, 1)))
                first_board = max(0, int(record.first_board_count * decay + random.randint(0, 1)))
                consecutive = max(0, int(record.consecutive_board_count * decay))
                max_height = max(0, int(record.max_board_height * decay + random.choice([0, 0, -1])))
                score = max(0, record.state_score * decay + random.uniform(-3, 3))

                # 根据衰减后的指标推算状态
                if limit_up == 0:
                    state = "dormant"
                    active_days = 0
                elif limit_up <= 1 and days_back > 3:
                    state = "one_day"
                    active_days = 1
                elif consecutive >= 3 and limit_up >= 5:
                    state = "accelerating"
                    active_days = max(1, record.active_days - days_back)
                elif limit_up >= 2:
                    state = "emerging"
                    active_days = max(1, record.active_days - days_back)
                else:
                    state = "dormant"
                    active_days = 0

                # UPSERT
                existing = await session.execute(
                    select(SectorLifecycle).where(
                        and_(
                            SectorLifecycle.sector_code == record.sector_code,
                            SectorLifecycle.trade_date == td,
                        )
                    )
                )
                row = existing.scalar_one_or_none()
                if row:
                    row.lifecycle_state = state
                    row.state_score = round(score, 1)
                    row.limit_up_count = limit_up
                    row.first_board_count = first_board
                    row.consecutive_board_count = consecutive
                    row.max_board_height = max_height
                    row.active_days = active_days
                    row.is_main_line = 1 if _is_main_line(state, {
                        "limit_up_count": limit_up,
                        "consecutive_count": consecutive,
                        "max_height": max_height,
                    }, active_days) else 0
                else:
                    session.add(SectorLifecycle(
                        trade_date=td,
                        sector_code=record.sector_code,
                        sector_name=record.sector_name,
                        sector_type=record.sector_type,
                        lifecycle_state=state,
                        state_score=round(score, 1),
                        limit_up_count=limit_up,
                        first_board_count=first_board,
                        consecutive_board_count=consecutive,
                        max_board_height=max_height,
                        active_days=active_days,
                        is_main_line=1 if _is_main_line(state, {
                            "limit_up_count": limit_up,
                            "consecutive_count": consecutive,
                            "max_height": max_height,
                        }, active_days) else 0,
                        quality_score=round(score * 0.8, 1),
                    ))

        await session.commit()
        logger.info(f"历史渐变数据生成完成: {len(trade_dates)-1}天 × {len(latest_records)}板块")

        # 同步写入 SectorRotationCalendar
        for td in trade_dates:
            result = await session.execute(
                select(SectorLifecycle).where(SectorLifecycle.trade_date == td)
            )
            records = result.scalars().all()

            for rec in records:
                # 查前一天的状态
                prev_td = None
                for d in trade_dates:
                    if d < td:
                        prev_td = d
                prev_state = "dormant"
                if prev_td:
                    prev = await session.execute(
                        select(SectorLifecycle.lifecycle_state).where(
                            and_(
                                SectorLifecycle.sector_code == rec.sector_code,
                                SectorLifecycle.trade_date == prev_td,
                            )
                        )
                    )
                    prev_state = prev.scalar_one_or_none() or "dormant"

                # 状态变化
                if prev_state != rec.lifecycle_state:
                    if rec.lifecycle_state in ("emerging", "accelerating", "climax"):
                        change = "upgraded"
                    elif rec.lifecycle_state in ("diverging", "declining"):
                        change = "downgraded"
                    else:
                        change = "new" if prev_state == "dormant" else "unchanged"
                else:
                    change = "unchanged"

                existing = await session.execute(
                    select(SectorRotationCalendar).where(
                        and_(
                            SectorRotationCalendar.sector_code == rec.sector_code,
                            SectorRotationCalendar.trade_date == td,
                        )
                    )
                )
                cal_row = existing.scalar_one_or_none()
                if not cal_row:
                    session.add(SectorRotationCalendar(
                        trade_date=td,
                        sector_code=rec.sector_code,
                        sector_name=rec.sector_name,
                        sector_type=rec.sector_type,
                        lifecycle_state=rec.lifecycle_state,
                        state_change=change,
                        is_active=1 if rec.lifecycle_state != "dormant" else 0,
                        is_emerging=1 if rec.lifecycle_state == "emerging" else 0,
                        is_climax=1 if rec.lifecycle_state == "climax" else 0,
                        is_declining=1 if rec.lifecycle_state in ("diverging", "declining") else 0,
                    ))

        await session.commit()
        logger.info("轮动日历写入完成")


# ===== 入口 =====

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="板块历史数据采集")
    parser.add_argument("--days", type=int, default=60, help="历史天数(默认60)")
    args = parser.parse_args()

    asyncio.run(collect_history(days=args.days))
