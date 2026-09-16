"""补充全部pywencai板块的持续性+轮动种子数据

以4/10真实采集数据为基准，向前推4天生成合理的渐变数据。
策略:
- 4/10(最新): 已有真实数据(从collect_fund_flow.py采集)
- 4/09~4/06: 基于真实数据渐变衰减(资金流×衰减因子, 强度评分渐变)
- 连续天数: 根据资金流方向推算(连续流入=天数递增)
- 轮动信号: 从排名变化推算, 优先板块间轮动而非"其他→板块"
"""

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, and_, func, delete
from app.db.session import async_session, init_db
from app.models.stock import SectorInfo, SectorPersistence
from app.models.sector import SectorStrength, SectorRotation


def _get_trade_dates(n=5):
    """获取最近n个交易日(跳周末), 以2026-04-10为最新"""
    latest = date(2026, 4, 10)  # 周五, 有真实数据
    dates = []
    d = latest
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    return list(reversed(dates))


async def seed():
    await init_db()
    async with async_session() as session:
        trade_dates = _get_trade_dates(5)
        latest_date = trade_dates[-1]  # 2026-04-10
        print(f"交易日: {[str(d) for d in trade_dates]}")
        print(f"最新(真实数据日): {latest_date}")

        # ===== 1. 清理旧的种子轮动数据(保留4/10真实采集的) =====
        await session.execute(
            delete(SectorRotation).where(SectorRotation.trade_date != latest_date)
        )
        print("清理旧轮动数据完成")

        # ===== 2. 获取4/10真实数据作为基准 =====
        result = await session.execute(
            select(SectorPersistence).where(SectorPersistence.trade_date == latest_date)
        )
        latest_records = result.scalars().all()
        print(f"4/10真实持续性数据: {len(latest_records)}条")

        # 构建 sector_code → 最新数据 映射
        latest_map = {}
        for r in latest_records:
            latest_map[r.sector_code] = {
                "sector_name": r.sector_name,
                "fund_flow": r.fund_flow or 0,
                "change_pct": r.change_pct or 0,
                "strength_score": r.strength_score or 0,
                "consecutive_days": r.consecutive_days or 0,
                "limit_up_count": r.limit_up_count or 0,
            }

        # ===== 3. 为前4天生成渐变数据 =====
        # 衰减策略:
        # - 资金流: 每天衰减0.7~0.9倍(向0回归)
        # - 强度: 每天衰减0.85~0.95倍
        # - 连续天数: 资金流为正时+1, 为负时重置为0
        # - 涨跌幅: 与资金流方向一致, 幅度渐减
        import random
        random.seed(42)

        persistence_count = 0
        strength_count = 0

        for sector_code, base in latest_map.items():
            sector_name = base["sector_name"]
            fund = base["fund_flow"]
            strength = base["strength_score"]
            change_pct = base["change_pct"]
            consecutive = base["consecutive_days"]
            limit_up = base["limit_up_count"]

            for i, td in enumerate(trade_dates[:-1]):  # 跳过最新日
                days_back = len(trade_dates) - 1 - i  # 距最新日天数
                decay = 0.82 ** days_back  # 指数衰减

                # 资金流衰减(向0回归, 加随机波动)
                prev_fund = round(fund * decay + random.uniform(-1, 1), 2)
                # 强度衰减
                prev_strength = round(max(0, min(100, strength * decay + random.uniform(-2, 2))), 1)
                # 涨跌幅衰减
                prev_change = round(change_pct * decay + random.uniform(-0.3, 0.3), 2)
                # 连续天数: 资金流>0时连续, 否则重置
                if prev_fund > 0:
                    prev_consecutive = max(0, consecutive - days_back)
                else:
                    prev_consecutive = 0
                # 涨停数衰减
                prev_limit_up = max(0, int(limit_up * decay))

                # 检查是否已有数据
                existing = await session.execute(
                    select(SectorPersistence).where(
                        and_(
                            SectorPersistence.sector_code == sector_code,
                            SectorPersistence.trade_date == td,
                        )
                    )
                )
                row = existing.scalar_one_or_none()
                if row:
                    # 更新已有记录
                    row.fund_flow = prev_fund
                    row.strength_score = prev_strength
                    row.change_pct = prev_change
                    row.consecutive_days = prev_consecutive
                    row.limit_up_count = prev_limit_up
                else:
                    session.add(SectorPersistence(
                        sector_code=sector_code,
                        sector_name=sector_name,
                        trade_date=td,
                        consecutive_days=prev_consecutive,
                        limit_up_count=prev_limit_up,
                        fund_flow=prev_fund,
                        change_pct=prev_change,
                        strength_score=prev_strength,
                    ))
                    persistence_count += 1

                # 同步写SectorStrength
                existing2 = await session.execute(
                    select(SectorStrength).where(
                        and_(
                            SectorStrength.sector_code == sector_code,
                            SectorStrength.trade_date == td,
                        )
                    )
                )
                row2 = existing2.scalar_one_or_none()
                if row2:
                    row2.strength_score = prev_strength
                    row2.consecutive_days = prev_consecutive
                    row2.fund_flow = prev_fund
                else:
                    session.add(SectorStrength(
                        trade_date=td,
                        sector_code=sector_code,
                        sector_name=sector_name,
                        rank=0,
                        rank_change=0,
                        strength_score=prev_strength,
                        consecutive_days=prev_consecutive,
                        fund_flow=prev_fund,
                    ))
                    strength_count += 1

        await session.commit()
        print(f"前4天渐变数据: 持续性新增{persistence_count}条, 强度新增{strength_count}条")

        # ===== 4. 更新所有天的SectorStrength排名 =====
        for td in trade_dates:
            # 同类型内排名(概念/行业分开)
            for sector_type in ("concept", "industry"):
                result = await session.execute(
                    select(SectorStrength, SectorInfo.sector_type)
                    .outerjoin(SectorInfo, SectorInfo.sector_code == SectorStrength.sector_code)
                    .where(
                        and_(
                            SectorStrength.trade_date == td,
                            SectorInfo.sector_type == sector_type,
                        )
                    )
                    .order_by(SectorStrength.strength_score.desc())
                )
                records = result.all()
                for i, (rec, _) in enumerate(records, 1):
                    rec.rank = i

            # 全量排名(所有类型一起)
            result = await session.execute(
                select(SectorStrength).where(SectorStrength.trade_date == td)
                .order_by(SectorStrength.strength_score.desc())
            )
            all_records = result.scalars().all()
            for i, rec in enumerate(all_records, 1):
                # 保存全量排名到rank字段
                pass  # 已在上面按类型排了

            # 计算排名变化(与前一个交易日比)
            prev_td = None
            for d in trade_dates:
                if d < td:
                    prev_td = d
            if prev_td:
                prev_result = await session.execute(
                    select(SectorStrength).where(SectorStrength.trade_date == prev_td)
                )
                prev_map = {r.sector_code: r.rank for r in prev_result.scalars().all()}
                result = await session.execute(
                    select(SectorStrength).where(SectorStrength.trade_date == td)
                )
                for rec in result.scalars().all():
                    prev_rank = prev_map.get(rec.sector_code, 0)
                    rec.rank_change = prev_rank - rec.rank if prev_rank > 0 else 0

        await session.commit()
        print("强度排名+排名变化更新完成")

        # ===== 5. 生成板块间轮动信号 =====
        # 策略: 找排名上升且资金流入的板块(流入) + 排名下降且资金流出的板块(流出)
        # 配对: 流出板块 → 流入板块
        rotation_count = 0
        for td in trade_dates:
            # 找流入板块: rank_change > 0 且 fund_flow > 0
            rising_result = await session.execute(
                select(SectorStrength).where(
                    and_(
                        SectorStrength.trade_date == td,
                        SectorStrength.rank_change > 3,
                        SectorStrength.fund_flow > 0,
                    )
                ).order_by(SectorStrength.rank_change.desc()).limit(10)
            )
            rising = rising_result.scalars().all()

            # 找流出板块: rank_change < 0 且 fund_flow < 0
            falling_result = await session.execute(
                select(SectorStrength).where(
                    and_(
                        SectorStrength.trade_date == td,
                        SectorStrength.rank_change < -3,
                        SectorStrength.fund_flow < 0,
                    )
                ).order_by(SectorStrength.rank_change.asc()).limit(10)
            )
            falling = falling_result.scalars().all()

            # 配对: 流出→流入
            for i, fall in enumerate(falling):
                if i < len(rising):
                    rise = rising[i]
                    # 流向金额取流出和流入中较小的一个的30%~80%
                    flow = round(min(abs(fall.fund_flow), rise.fund_flow) * random.uniform(0.3, 0.7), 2)
                    rot_type = "sudden" if abs(fall.rank_change) >= 10 else "gradual"

                    # 检查是否已存在
                    existing = await session.execute(
                        select(SectorRotation).where(
                            and_(
                                SectorRotation.trade_date == td,
                                SectorRotation.from_sector == fall.sector_code,
                                SectorRotation.to_sector == rise.sector_code,
                            )
                        )
                    )
                    if not existing.scalar_one_or_none():
                        session.add(SectorRotation(
                            trade_date=td,
                            from_sector=fall.sector_code,
                            to_sector=rise.sector_code,
                            flow_amount=flow,
                            rotation_type=rot_type,
                        ))
                        rotation_count += 1

        await session.commit()
        print(f"板块间轮动信号: 新增{rotation_count}条")

        # ===== 6. 验证 =====
        for td in trade_dates:
            r1 = await session.execute(
                select(func.count()).select_from(SectorPersistence)
                .where(SectorPersistence.trade_date == td)
            )
            r2 = await session.execute(
                select(func.count()).select_from(SectorStrength)
                .where(SectorStrength.trade_date == td)
            )
            r3 = await session.execute(
                select(func.count()).select_from(SectorRotation)
                .where(SectorRotation.trade_date == td)
            )
            print(f"  {td}: 持续性={r1.scalar()}, 强度={r2.scalar()}, 轮动={r3.scalar()}")

    print("\n✅ 全量种子数据补充完成!")


if __name__ == "__main__":
    asyncio.run(seed())
