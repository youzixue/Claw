#!/usr/bin/env python3
"""每日盘后复盘报告 — 一键生成

用法:
    python run_review.py                    # 复盘最近交易日
    python run_review.py --date 2026-08-18  # 指定日期
    python run_review.py --full             # 完整报告(含消息面)
    python run_review.py --quick            # 仅板块+资金概览

输出内容:
    1. 市场情绪概览: 涨跌比/涨停数/封板率/情绪周期
    2. 板块涨跌排行: 各板块涨跌幅+资金流向+持续性
    3. 涨停池分析: 涨停个股量价关系+连板+主线判断
    4. 大阳线分析: 涨幅>5%个股的持续性判断
    5. 消息面整理: 盘前/盘中/盘后重要新闻+事件催化
    6. 明日展望: 主线方向+风险提示
"""

import asyncio
import sys
import os
from datetime import date, datetime, timedelta

# 确保项目根目录和 backend 在 sys.path
_project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _project_root)
sys.path.insert(0, os.path.join(_project_root, "backend"))

from loguru import logger
from sqlalchemy import select, desc, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db.session import async_session
from backend.app.core.trade_calendar import trade_calendar
from backend.app.core.data_date import resolve_latest_trade_date
from backend.app.models.stock import (
    SectorPersistence, LimitUpPool, LimitDownPool,
    FundFlow, MarketSentiment, StockSpot, StockKline,
)
from backend.app.models.news import FinanceNews

# =============================================================================
# 工具函数
# =============================================================================

def _safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _fmt_pct(val: float) -> str:
    """格式化百分比，红涨绿跌"""
    if val > 0:
        return f"+{val:.2f}%"
    elif val < 0:
        return f"{val:.2f}%"
    return "0.00%"


def _fmt_yi(val: float) -> str:
    """格式化金额(亿)"""
    yi = val / 1e8
    if yi > 0:
        return f"+{yi:.2f}亿"
    elif yi < 0:
        return f"{yi:.2f}亿"
    return "0亿"


# =============================================================================
# 核心复盘逻辑
# =============================================================================

async def run_review(db: AsyncSession, target_date: date, full: bool = False):
    """执行完整复盘"""

    print("=" * 80)
    print(f"  📊 A股盘后复盘报告 — {target_date}")
    print("=" * 80)

    # ===== 1. 市场情绪概览 =====
    print("\n" + "─" * 60)
    print("  【一、市场情绪概览】")
    print("─" * 60)

    sentiment = await db.execute(
        select(MarketSentiment).where(MarketSentiment.trade_date == target_date)
    )
    sent = sentiment.scalar_one_or_none()

    if sent:
        cycle_map = {
            "climax": "🔥 高潮期",
            "divergence": "⚡ 分歧期",
            "freezing": "❄️ 冰点期",
            "recovery": "🌱 修复期",
        }
        print(f"  情绪周期: {cycle_map.get(sent.sentiment_cycle, sent.sentiment_cycle)}")
        print(f"  涨停: {sent.limit_up_count}家  |  跌停: {sent.limit_down_count}家")
        print(f"  炸板: {sent.broken_limit_count}家  |  封板率: {sent.seal_rate:.0f}%")
        print(f"  涨跌比: {sent.advance_decline_ratio:.2f}")
        print(f"  最高连板: {sent.board_height}板")
        print(f"  主力净流入: {_fmt_yi((sent.main_net_inflow or 0) * 1e8)}")
    else:
        print("  ⚠️ 无市场情绪数据")

    # ===== 2. 板块涨跌排行 =====
    print("\n" + "─" * 60)
    print("  【二、板块涨跌排行 Top15】")
    print("─" * 60)

    sectors = await db.execute(
        select(SectorPersistence)
        .where(SectorPersistence.trade_date == target_date)
        .order_by(desc(SectorPersistence.change_pct))
    )
    sector_rows = sectors.scalars().all()

    if sector_rows:
        print(f"  {'板块名称':<16s} {'涨跌幅':>8s} {'资金净流入':>10s} {'涨停数':>6s} {'持续天数':>8s} {'强度':>6s}")
        print("  " + "-" * 58)
        for s in sector_rows[:15]:
            print(
                f"  {s.sector_name[:14]:<16s} "
                f"{_fmt_pct(_safe_float(s.change_pct)):>8s} "
                f"{_fmt_yi(_safe_float(s.fund_flow) * 1e8):>10s} "
                f"{s.limit_up_count or 0:>6d} "
                f"{s.consecutive_days or 0:>8d} "
                f"{_safe_float(s.strength_score):>6.0f}"
            )

        # 跌幅Top5
        print(f"\n  📉 跌幅最大板块:")
        sorted_desc = sorted(sector_rows, key=lambda x: _safe_float(x.change_pct))
        for s in sorted_desc[:5]:
            print(
                f"    {s.sector_name[:14]:<16s} "
                f"{_fmt_pct(_safe_float(s.change_pct)):>8s} "
                f"{_fmt_yi(_safe_float(s.fund_flow) * 1e8):>10s}"
            )
    else:
        print("  ⚠️ 无板块数据")

    # ===== 3. 涨停池分析 =====
    print("\n" + "─" * 60)
    print("  【三、涨停池分析】")
    print("─" * 60)

    limit_ups = await db.execute(
        select(LimitUpPool)
        .where(LimitUpPool.trade_date == target_date)
        .order_by(desc(LimitUpPool.consecutive_days))
    )
    lu_rows = limit_ups.scalars().all()

    if lu_rows:
        # 按连板数分组统计
        board_stats = {}
        for lu in lu_rows:
            days = lu.consecutive_days or 1
            board_stats[days] = board_stats.get(days, 0) + 1

        print(f"  涨停总数: {len(lu_rows)}家")
        print(f"  连板分布: ", end="")
        for days in sorted(board_stats.keys(), reverse=True):
            print(f"{days}板×{board_stats[days]} ", end="")
        print()

        # 涨停原因分类
        reason_map = {}
        for lu in lu_rows:
            reason = (lu.limit_up_reason or "未分类").strip()
            # 取第一个关键词
            key = reason.split("+")[0].split("、")[0][:8]
            reason_map[key] = reason_map.get(key, 0) + 1

        print(f"  涨停主线: ", end="")
        for r, c in sorted(reason_map.items(), key=lambda x: -x[1])[:5]:
            print(f"{r}({c}家) ", end="")
        print()

        # 连板股详情
        print(f"\n  🔥 连板股(≥2板):")
        multi_board = [lu for lu in lu_rows if (lu.consecutive_days or 0) >= 2]
        if multi_board:
            for lu in sorted(multi_board, key=lambda x: -(x.consecutive_days or 0)):
                print(
                    f"    {lu.code} {lu.name:<8s} "
                    f"{lu.consecutive_days}连板 "
                    f"封单{_fmt_yi(_safe_float(lu.seal_amount))} "
                    f"换手{_safe_float(lu.turnover):.1f}% "
                    f"| {lu.limit_up_reason or '未知'}"
                )
        else:
            print("    无连板股")

        # 炸板分析
        broken = [lu for lu in lu_rows if (lu.break_count or 0) > 0]
        if broken:
            print(f"\n  ⚠️ 炸板股({len(broken)}家):")
            for lu in broken:
                print(f"    {lu.code} {lu.name} 炸板{lu.break_count}次")
    else:
        print("  ⚠️ 无涨停数据")

    # ===== 4. 跌停池 =====
    print("\n" + "─" * 60)
    print("  【四、跌停池】")
    print("─" * 60)

    limit_downs = await db.execute(
        select(LimitDownPool)
        .where(LimitDownPool.trade_date == target_date)
    )
    ld_rows = limit_downs.scalars().all()

    if ld_rows:
        print(f"  跌停总数: {len(ld_rows)}家")
        for ld in ld_rows[:10]:
            reason = ld.reason or "未知"
            print(f"    {ld.code} {ld.name:<8s} {ld.consecutive_days}连跌 | {reason[:30]}")
    else:
        print("  无跌停")

    # ===== 5. 大阳线分析(涨幅>5%非涨停) =====
    print("\n" + "─" * 60)
    print("  【五、大阳线分析(涨幅>5%非涨停)】")
    print("─" * 60)

    big_up = await db.execute(
        select(StockSpot)
        .where(
            StockSpot.price > 0,
            StockSpot.volume > 0,
            StockSpot.change_pct > 5,
        )
        .order_by(desc(StockSpot.change_pct))
        .limit(20)
    )
    big_rows = big_up.scalars().all()

    # 过滤涨停股
    lu_codes = {lu.code for lu in lu_rows} if lu_rows else set()
    big_non_lu = [s for s in big_rows if s.code not in lu_codes]

    if big_non_lu:
        print(f"  大阳线(非涨停): {len(big_non_lu)}家")
        print(f"  {'代码':<10s} {'名称':<10s} {'涨幅':>8s} {'换手':>6s} {'量比':>6s} {'主力净流入':>10s} {'持续判断':>12s}")
        print("  " + "-" * 66)
        for s in big_non_lu[:10]:
            # 量价持续判断
            change = _safe_float(s.change_pct)
            turnover = _safe_float(s.turnover)
            vr = _safe_float(s.volume_ratio, 1.0)
            if vr > 2 and turnover > 10:
                persistence = "放量换手充分"
            elif vr > 2 and turnover < 5:
                persistence = "放量但换手低"
            elif vr < 0.8 and change > 7:
                persistence = "缩量大阳⚠️"
            else:
                persistence = "正常"
            print(
                f"  {s.code:<10s} {s.name or '':<10s} "
                f"{_fmt_pct(change):>8s} "
                f"{turnover:>6.1f}% "
                f"{vr:>6.1f} "
                f"{_fmt_yi(_safe_float(s.main_net_inflow)):>10s} "
                f"{persistence:>12s}"
            )
    else:
        print("  无大阳线非涨停股")

    # ===== 6. 消息面整理 =====
    if full:
        print("\n" + "─" * 60)
        print("  【六、消息面整理】")
        print("─" * 60)

        # 盘前消息 (当日06:00-09:30)
        day_start = datetime.combine(target_date, datetime.min.time())
        pre_market_start = day_start.replace(hour=6)
        pre_market_end = day_start.replace(hour=9, minute=30)

        # 盘中消息 (09:30-15:00)
        intra_start = day_start.replace(hour=9, minute=30)
        intra_end = day_start.replace(hour=15)

        # 盘后消息 (15:00-次日06:00)
        after_start = day_start.replace(hour=15)
        after_end = day_start + timedelta(days=1, hours=6)

        for period_name, p_start, p_end in [
            ("盘前", pre_market_start, pre_market_end),
            ("盘中", intra_start, intra_end),
            ("盘后", after_start, after_end),
        ]:
            news_query = await db.execute(
                select(FinanceNews)
                .where(
                    FinanceNews.publish_time >= p_start,
                    FinanceNews.publish_time < p_end,
                    FinanceNews.importance >= 7,
                )
                .order_by(desc(FinanceNews.importance))
                .limit(10)
            )
            news_rows = news_query.scalars().all()

            if news_rows:
                print(f"\n  📰 {period_name}重要消息 ({len(news_rows)}条):")
                for n in news_rows:
                    bull_icon = "🟢" if n.bull_bear == "bull" else "🔴" if n.bull_bear == "bear" else "⚪"
                    title = (n.title or "")[:60]
                    sectors = n.related_sectors or ""
                    print(f"    {bull_icon} [{n.importance}] {title}")
                    if sectors:
                        print(f"       影响板块: {sectors}")
            else:
                print(f"\n  📰 {period_name}: 无重要消息")

    # ===== 7. 明日展望 =====
    print("\n" + "=" * 80)
    print("  【明日展望】")
    print("=" * 80)

    # 判断主线
    if sector_rows:
        active_sectors = [s for s in sector_rows if (s.consecutive_days or 0) >= 3]
        if active_sectors:
            print(f"\n  🔥 持续活跃板块({len(active_sectors)}个):")
            for s in sorted(active_sectors, key=lambda x: -(x.consecutive_days or 0)):
                print(f"    {s.sector_name}: {s.consecutive_days}天活跃, 涨停{s.limit_up_count or 0}家")

    # 风险提示
    print(f"\n  ⚠️ 风险提示:")
    if sent:
        if (sent.limit_up_count or 0) > 100:
            print(f"    - 涨停家数{sent.limit_up_count}家，市场过热，注意分化")
        if (sent.limit_up_count or 0) < 20:
            print(f"    - 涨停家数仅{sent.limit_up_count}家，市场情绪低迷")
        if (sent.board_height or 0) < 3:
            print(f"    - 最高连板仅{sent.board_height}板，短线情绪弱")
        if (sent.advance_decline_ratio or 0) < 0.8:
            print(f"    - 涨跌比{sent.advance_decline_ratio:.2f}，普跌行情")
    if ld_rows and len(ld_rows) > 10:
        print(f"    - 跌停{len(ld_rows)}家，注意风险传导")

    print(f"\n  📌 声明: 以上分析仅供参考，不构成投资建议。")
    print(f"     数据来源: 东财/同花顺/Tencent/财联社/巨潮资讯")
    print(f"     生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)


# =============================================================================
# 调度器集成: 供 scheduler._daily_review 调用
# =============================================================================

async def daily_review(db: AsyncSession, target_date: date | None = None, full: bool = True):
    """调度器集成的复盘入口"""
    if target_date is None:
        target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
    await run_review(db, target_date, full=full)


# =============================================================================
# CLI 入口
# =============================================================================

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="A股每日盘后复盘报告")
    parser.add_argument("--date", type=str, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--full", action="store_true", help="完整报告(含消息面)")
    parser.add_argument("--quick", action="store_true", help="快速报告(仅板块+资金)")
    args = parser.parse_args()

    async with async_session() as db:
        if args.date:
            target_date = date.fromisoformat(args.date)
        else:
            target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)

        if not await trade_calendar.is_trade_day(target_date):
            logger.warning(f"{target_date} 非交易日，使用最近交易日")
            target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)

        logger.info(f"复盘日期: {target_date}")
        full = args.full or not args.quick
        await run_review(db, target_date, full=full)


if __name__ == "__main__":
    asyncio.run(main())