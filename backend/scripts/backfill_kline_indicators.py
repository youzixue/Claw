"""批量回填K线涨跌幅/振幅 + 均线/趋势指标

从已有的OHLCV数据中自行计算:
- change_pct: 涨跌幅% = (close - prev_close) / prev_close * 100
- amplitude: 振幅% = (high - low) / prev_close * 100
- ma5/ma10/ma20/ma5_vol: 均线
- trend_state: 趋势状态
- vol_ratio: 量比
- support_price: 支撑位
- resistance_price: 压力位

适用场景: 历史K线数据采集时未计算这些字段, 需要补算。

用法:
  python3 scripts/backfill_kline_indicators.py                    # 全量回填
  python3 scripts/backfill_kline_indicators.py --days 30          # 仅最近30天
  python3 scripts/backfill_kline_indicators.py --sector-type concept  # 仅概念
"""

import asyncio
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from sqlalchemy import select, and_, func

from app.db.session import async_session, init_db
from app.models.sector import SectorKline


def calc_indicators(records: list) -> list:
    """对已排序的K线记录列表, 计算所有指标并返回更新列表

    records: 按trade_date升序排列的SectorKline对象列表
    返回: 需要更新的[(record, field_dict)]列表
    """
    updates = []

    closes = [r.close for r in records]
    volumes = [r.volume for r in records]

    for i, rec in enumerate(records):
        fields = {}

        # 涨跌幅 = (close - prev_close) / prev_close * 100
        if rec.change_pct is None and i > 0:
            prev_close = closes[i - 1]
            cur_close = rec.close
            if prev_close and prev_close > 0 and cur_close is not None:
                fields["change_pct"] = round((cur_close - prev_close) / prev_close * 100, 2)

        # 振幅 = (high - low) / prev_close * 100
        if rec.amplitude is None and i > 0:
            prev_close = closes[i - 1]
            cur_high = rec.high
            cur_low = rec.low
            if prev_close and prev_close > 0 and cur_high is not None and cur_low is not None:
                fields["amplitude"] = round((cur_high - cur_low) / prev_close * 100, 2)

        # MA5
        if rec.ma5 is None and i >= 4 and all(c is not None for c in closes[i-4:i+1]):
            fields["ma5"] = round(sum(closes[i-4:i+1]) / 5, 3)

        # MA10
        if rec.ma10 is None and i >= 9 and all(c is not None for c in closes[i-9:i+1]):
            fields["ma10"] = round(sum(closes[i-9:i+1]) / 10, 3)

        # MA20
        if rec.ma20 is None and i >= 19 and all(c is not None for c in closes[i-19:i+1]):
            fields["ma20"] = round(sum(closes[i-19:i+1]) / 20, 3)

        # MA5成交量
        if rec.ma5_vol is None and i >= 4 and all(v is not None for v in volumes[i-4:i+1]):
            fields["ma5_vol"] = round(sum(volumes[i-4:i+1]) / 5, 0)

        # 量比
        if rec.vol_ratio is None and rec.volume and rec.ma5_vol is None:
            # ma5_vol还没算, 用上面的值
            ma5v = fields.get("ma5_vol")
            if ma5v and ma5v > 0:
                fields["vol_ratio"] = round(rec.volume / ma5v, 2)
        elif rec.vol_ratio is None and rec.volume and rec.ma5_vol and rec.ma5_vol > 0:
            fields["vol_ratio"] = round(rec.volume / rec.ma5_vol, 2)

        # 支撑位/压力位(近20日)
        lookback_start = max(0, i - 19)
        recent_lows = [records[j].low for j in range(lookback_start, i + 1)
                       if records[j].low is not None]
        recent_highs = [records[j].high for j in range(lookback_start, i + 1)
                        if records[j].high is not None]

        if rec.support_price is None and recent_lows:
            fields["support_price"] = round(min(recent_lows), 3)
        if rec.resistance_price is None and recent_highs:
            fields["resistance_price"] = round(max(recent_highs), 3)

        # 趋势状态
        if rec.trend_state is None and rec.close:
            cur_close = rec.close
            ma5 = fields.get("ma5") or rec.ma5
            ma20 = fields.get("ma20") or rec.ma20
            vol_ratio = fields.get("vol_ratio") or rec.vol_ratio

            if ma5 and ma20:
                if cur_close > ma5 > ma20:
                    if vol_ratio and vol_ratio > 1.5:
                        fields["trend_state"] = "breakout_up"
                    else:
                        fields["trend_state"] = "up"
                elif cur_close < ma5 < ma20:
                    if vol_ratio and vol_ratio > 1.5:
                        fields["trend_state"] = "breakout_down"
                    else:
                        fields["trend_state"] = "down"
                else:
                    fields["trend_state"] = "sideways"
            elif ma5:
                if cur_close > ma5:
                    fields["trend_state"] = "up"
                elif cur_close < ma5:
                    fields["trend_state"] = "down"
                else:
                    fields["trend_state"] = "sideways"

        if fields:
            updates.append((rec, fields))

    return updates


async def backfill_kline_indicators(days: int | None = None,
                                     sector_type: str | None = None):
    """批量回填K线指标"""
    await init_db()

    logger.info(f"=== K线指标回填开始: days={days}, sector_type={sector_type} ===")

    # 获取所有板块代码
    async with async_session() as session:
        query = select(SectorKline.sector_code, SectorKline.sector_type).distinct()
        if sector_type:
            query = query.where(SectorKline.sector_type == sector_type)
        result = await session.execute(query)
        sector_list = result.all()

    logger.info(f"待处理板块: {len(sector_list)}个")

    t_total = time.time()
    total_updated = 0

    for idx, (sector_code, s_type) in enumerate(sector_list):
        # 获取该板块的所有K线数据
        async with async_session() as session:
            query = select(SectorKline).where(
                SectorKline.sector_code == sector_code
            ).order_by(SectorKline.trade_date)

            if days:
                start_date = date.today() - timedelta(days=int(days * 1.5))
                query = query.where(SectorKline.trade_date >= start_date)

            result = await session.execute(query)
            records = result.scalars().all()

        if not records:
            continue

        # 计算指标
        updates = calc_indicators(records)

        if not updates:
            continue

        # 批量更新
        async with async_session() as session:
            for rec, fields in updates:
                # 重新查询以绑定到当前session
                result = await session.execute(
                    select(SectorKline).where(
                        and_(
                            SectorKline.sector_code == rec.sector_code,
                            SectorKline.trade_date == rec.trade_date,
                        )
                    )
                )
                db_rec = result.scalar_one_or_none()
                if db_rec:
                    for k, v in fields.items():
                        setattr(db_rec, k, v)

            await session.commit()

        total_updated += len(updates)

        if (idx + 1) % 50 == 0:
            logger.info(f"进度: {idx+1}/{len(sector_list)}板块, "
                       f"累计更新{total_updated}条, 耗时{time.time()-t_total:.0f}s")

    elapsed = time.time() - t_total
    logger.info(f"=== K线指标回填完成: {total_updated}条更新, "
               f"耗时{elapsed:.0f}s ===")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="批量回填K线涨跌幅/振幅/均线/趋势指标")
    parser.add_argument("--days", type=int, default=None,
                        help="仅回填最近N天(默认全量)")
    parser.add_argument("--sector-type", choices=["concept", "industry"], default=None,
                        help="仅回填指定板块类型(默认全部)")
    args = parser.parse_args()

    asyncio.run(backfill_kline_indicators(days=args.days, sector_type=args.sector_type))
