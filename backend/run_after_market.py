"""
补拉04-15/16/17 K线数据 (服务器04-15起挂)

策略: THS last.js 拉近140条→过滤3天→upsert补缺失
东财涨停/跌停/炸板: 指定日期参数
"""
import asyncio, sys, os, time as _time
from datetime import date

sys.path.insert(0, os.path.dirname(__file__))

from loguru import logger
from sqlalchemy import select, text
from app.db.session import async_session
from app.data.sources.ths_kline_source import ThsKlineSource
from app.data.sources.eastmoney_source import EastMoneySource
from app.data.scheduler import DataScheduler, _normalize_code
from app.models.stock import StockTag, StockKline, LimitUpPool, LimitDownPool, BrokenLimitPool


MISSING_DATES = ["2026-04-15", "2026-04-16", "2026-04-17"]


async def repair_kline():
    """用THS last.js并发补拉缺失日期K线"""
    import httpx
    
    async with async_session() as s:
        result = await s.execute(
            select(StockTag.code).where(StockTag.board_tag == "tradeable")
        )
        codes = [r[0] for r in result.all()]
    
    print(f"需采集: {len(codes)}只, 目标日期: {MISSING_DATES}")
    
    ths = ThsKlineSource()
    concurrency = 10
    sem = asyncio.Semaphore(concurrency)
    total_written = {d: 0 for d in MISSING_DATES}
    batch_klines = []
    done = 0
    t0 = _time.monotonic()
    
    async def fetch_one(code: str):
        nonlocal done
        async with sem:
            try:
                raw = await ths._fetch_kline(code, "last.js")
                if raw:
                    target = [k for k in raw if k["trade_date"] in MISSING_DATES]
                    if target:
                        derived = ths._calc_derived(target)
                        return derived
            except Exception:
                pass
            finally:
                done += 1
                if done % 500 == 0:
                    elapsed = _time.monotonic() - t0
                    speed = done / elapsed if elapsed > 0 else 0
                    print(f"  进度: {done}/{len(codes)} | {speed:.1f}只/s")
        return []
    
    async with httpx.AsyncClient(timeout=20, limits=httpx.Limits(max_connections=concurrency*2)):
        # 分批并发
        batch_size = concurrency * 4
        for i in range(0, len(codes), batch_size):
            batch = codes[i:i+batch_size]
            tasks = [fetch_one(c) for c in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for r in results:
                if isinstance(r, Exception) or not r:
                    continue
                for k in r:
                    total_written[k["trade_date"]] = total_written.get(k["trade_date"], 0) + 1
                batch_klines.extend(r)
            
            # 每2000条写一次DB
            if len(batch_klines) >= 2000:
                async with async_session() as s:
                    await DataScheduler._batch_upsert(s, StockKline, batch_klines, ["code", "trade_date"])
                    await s.commit()
                print(f"  💾 DB写入: {len(batch_klines)}条 | 累计: {total_written}")
                batch_klines = []
    
    # 末批
    if batch_klines:
        async with async_session() as s:
            await DataScheduler._batch_upsert(s, StockKline, batch_klines, ["code", "trade_date"])
            await s.commit()
        print(f"  💾 DB写入(末批): {len(batch_klines)}条")
    
    elapsed = _time.monotonic() - t0
    print(f"\n✅ K线补全完成({elapsed:.0f}s): {total_written}")


async def repair_em_pools():
    """东财涨停/跌停/炸板"""
    em = EastMoneySource()
    scheduler = DataScheduler()
    
    for d in ["20260415", "20260416", "20260417"]:
        dt = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        print(f"\n东财池子 {d}...")
        
        # 涨停
        try:
            df = await em.get_limit_up_pool(trade_date=d)
            if df is not None and len(df) > 0:
                records = scheduler._parse_limit_up_df(df, dt, source="eastmoney")
                async with async_session() as s:
                    await scheduler._upsert_limit_up(s, records)
                    await s.commit()
                print(f"  涨停: {len(records)}条")
        except Exception as e:
            print(f"  涨停失败: {e}")
        
        # 跌停
        try:
            df = await em.get_limit_down_pool(trade_date=d)
            if df is not None and len(df) > 0:
                records = scheduler._parse_limit_down_df(df, dt, source="eastmoney")
                async with async_session() as s:
                    await scheduler._upsert_limit_down(s, records)
                    await s.commit()
                print(f"  跌停: {len(records)}条")
        except Exception as e:
            print(f"  跌停失败: {e}")
        
        # 炸板
        try:
            df = await em.get_broken_limit_pool(trade_date=d)
            if df is not None and len(df) > 0:
                records = scheduler._parse_broken_limit_df(df, dt, source="eastmoney")
                async with async_session() as s:
                    await scheduler._upsert_broken_limit(s, records)
                    await s.commit()
                print(f"  炸板: {len(records)}条")
        except Exception as e:
            print(f"  炸板失败: {e}")


async def verify():
    print(f"\n{'='*60}")
    print("📊 验证结果")
    print(f"{'='*60}")
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), 'claw.db'))
    cur = conn.cursor()
    for d in MISSING_DATES:
        cur.execute(f"SELECT COUNT(*) FROM stock_kline WHERE trade_date='{d}'")
        kline = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM limit_up_pool WHERE trade_date='{d}'")
        lu = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM fund_flow WHERE trade_date='{d}'")
        ff = cur.fetchone()[0]
        print(f"  {d}: K线={kline} 涨停={lu} 资金流={ff}")
    conn.close()


async def main():
    print("=" * 60)
    print("🦅 补拉 04-15/16/17 缺失数据")
    print("=" * 60)
    await repair_kline()
    await repair_em_pools()
    await verify()


if __name__ == "__main__":
    asyncio.run(main())
