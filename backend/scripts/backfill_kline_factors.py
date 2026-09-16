"""批量回填K线技术因子到SectorLifecycle表

从SectorKline表读取当日K线记录中的技术因子,
更新到同sector_code+trade_date的SectorLifecycle记录中。

回填字段:
- kline_trend: 趋势状态(up/down/sideways/breakout_up/breakout_down)
- kline_vol_ratio: 量比
- kline_support: 支撑位
- kline_resistance: 压力位
- kline_ma5: 5日均线
- kline_ma20: 20日均线
- kline_close: 当日收盘指数

用法:
  python3 scripts/backfill_kline_factors.py                    # 全量回填
  python3 scripts/backfill_kline_factors.py --days 30          # 仅回填最近30天
  python3 scripts/backfill_kline_factors.py --sector-type concept  # 仅概念板块
"""

import asyncio
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import select, and_, update, func

from app.db.session import async_session, init_db
from app.models.sector import SectorLifecycle, SectorKline


async def backfill_kline_factors(days: int | None = None,
                                  sector_type: str | None = None):
    """批量回填K线技术因子到SectorLifecycle

    Args:
        days: 仅回填最近N天的记录, None=全量
        sector_type: 仅回填指定类型(concept/industry), None=全部
    """
    await init_db()

    logger.info(f"=== K线因子回填开始: days={days}, sector_type={sector_type} ===")

    # 查询需要更新的Lifecycle记录(只取kline_close为None的)
    async with async_session() as session:
        query = select(SectorLifecycle).where(
            SectorLifecycle.kline_close.is_(None)
        )
        if days:
            start_date = date.today() - timedelta(days=int(days * 1.5))
            query = query.where(SectorLifecycle.trade_date >= start_date)
        if sector_type:
            query = query.where(SectorLifecycle.sector_type == sector_type)

        query = query.order_by(SectorLifecycle.trade_date.desc())
        result = await session.execute(query)
        lifecycle_records = result.scalars().all()

    total = len(lifecycle_records)
    if total == 0:
        logger.info("无需回填, 所有Lifecycle记录已有K线因子")
        return

    logger.info(f"待回填Lifecycle记录: {total}条")

    # 批量查询对应的K线数据
    # 先收集所有(sector_code, trade_date)对
    code_date_pairs = [(r.sector_code, r.trade_date) for r in lifecycle_records]

    # 按trade_date范围批量查K线
    trade_dates = set(r.trade_date for r in lifecycle_records)
    sector_codes = set(r.sector_code for r in lifecycle_records)

    async with async_session() as session:
        kline_result = await session.execute(
            select(SectorKline).where(
                and_(
                    SectorKline.sector_code.in_(sector_codes),
                    SectorKline.trade_date.in_(trade_dates),
                )
            )
        )
        kline_records = kline_result.scalars().all()

    # 构建(sector_code, trade_date) → SectorKline 映射
    kline_map = {}
    for k in kline_records:
        key = (k.sector_code, k.trade_date)
        kline_map[key] = k

    logger.info(f"K线数据命中: {len(kline_map)}条 / 需匹配{total}条")

    # 批量更新Lifecycle记录
    t_start = time.time()
    updated = 0
    no_kline = 0
    batch_size = 200

    async with async_session() as session:
        for i, lc in enumerate(lifecycle_records):
            kline = kline_map.get((lc.sector_code, lc.trade_date))
            if kline is None:
                no_kline += 1
                continue

            lc.kline_trend = kline.trend_state
            lc.kline_vol_ratio = kline.vol_ratio
            lc.kline_support = kline.support_price
            lc.kline_resistance = kline.resistance_price
            lc.kline_ma5 = kline.ma5
            lc.kline_ma20 = kline.ma20
            lc.kline_close = kline.close
            updated += 1

            # 批量提交
            if (i + 1) % batch_size == 0:
                await session.commit()
                logger.info(f"回填进度: {i+1}/{total}, 已更新{updated}条, "
                           f"无K线{no_kline}条, 耗时{time.time()-t_start:.0f}s")

        # 提交剩余
        await session.commit()

    elapsed = time.time() - t_start
    logger.info(f"=== K线因子回填完成: 更新{updated}条, 无K线{no_kline}条, "
               f"耗时{elapsed:.0f}s ===")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="批量回填K线因子到SectorLifecycle")
    parser.add_argument("--days", type=int, default=None,
                        help="仅回填最近N天(默认全量)")
    parser.add_argument("--sector-type", choices=["concept", "industry"], default=None,
                        help="仅回填指定板块类型(默认全部)")
    args = parser.parse_args()

    asyncio.run(backfill_kline_factors(days=args.days, sector_type=args.sector_type))
