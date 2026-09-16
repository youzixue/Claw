"""个股日K线 stock_kline 表 init 回填测试脚本

目标:
1. 对10只样本股票执行 collect_init() 全量采集(2018→今)
2. 通过 scheduler 的 _batch_upsert 写入 DB
3. 验证写入数量、字段值准确性、派生字段计算正确性
4. 验证唯一约束(code+trade_date)去重效果

样本股(10只, 覆盖主板/创业板/科创板):
  000001 平安银行, 000333 美的集团, 600519 茅台, 
  300750 宁德时代, 688001 华兴源创

用法:
  python3 scripts/test_kline_init.py              # 默认10只init回填+验证
  python3 scripts/test_kline_init.py --dry-run    # 只采集不写DB(快速验证采集)
  python3 scripts/test_kline_init.py --codes 000001,600519  # 指定股票
  python3 scripts/test_kline_init.py --verify     # 仅验证DB中已有数据
"""

import asyncio
import json
import sys
import time as _time
from datetime import date
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from loguru import logger

# 移除默认handler，用我们自己的格式
logger.remove()
logger.add(sys.stderr, format="{time:HH:mm:ss} | {level:<7} | {message}", level="DEBUG")


# ===== 样本配置 =====

SAMPLE_CODES = [
    "000001",   # 平安银行 - 深市主板
    "000333",   # 美的集团 - 深市主板
    "600036",   # 招商银行 - 沪市主板
    "600519",   # 贵州茅台 - 沪市主板(高价股)
    "601888",   # 中国中免 - 沪市主板
    "300750",   # 宁德时代 - 创业板
    "300059",   # 东方财富 - 创业板
    "688001",   # 华兴源创 - 科创板(首批)
    "688981",   # 中芯国际 - 科创板(高价)
    "002594",   # 比亚迪 - 深市中小板
]


async def run_init_backfill(codes=None, dry_run=False, verify_only=False):
    """执行K线 init 回填全流程"""
    
    if codes is None:
        codes = SAMPLE_CODES
    
    logger.info(f"{'='*72}")
    logger.info(f"  个股日K线 stock_kline Init 回填测试")
    logger.info(f"  时间: {_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"  样本: {len(codes)}只 {', '.join(codes[:5])}...")
    logger.info(f"{'='*72}")
    
    # ---- Step 0: 验证模式 ----
    if verify_only:
        await verify_db_data(codes)
        return
    
    # ---- Step 1: 导入模块 ----
    from app.db.session import async_session, init_db
    from app.data.sources.ths_kline_source import ThsKlineSource
    from app.models.stock import StockKline
    from sqlalchemy import select, func, text
    
    await init_db()
    
    ths = ThsKlineSource()
    
    # ---- Step 2: 健康检查 ----
    logger.info("\n--- Step 1: 同花顺数据源健康检查 ---")
    async with async_session() as session:
        healthy = await ths.health_check(session)
        if not healthy:
            logger.error("❌ 同花顺数据源不可用，终止测试")
            return
        logger.info("✅ 同花顺数据源健康检查通过")
    
    # ---- Step 3: 全量采集 collect_init ----
    all_results = {}  # {code: [kline_dict]}
    total_raw = 0
    total_unique = 0
    t_collect_start = _time.monotonic()
    
    for i, code in enumerate(codes):
        logger.info(f"\n--- Step 2: 采集 [{i+1}/{len(codes)}] {code} ---")
        
        t0 = _time.monotonic()
        klines = await ths.collect_init(code)
        elapsed = _time.monotonic() - t0
        
        if not klines:
            logger.warning(f"⚠️ {code} 采集结果为空，跳过")
            all_results[code] = []
            continue
        
        total_raw += len(klines)
        
        # 统计日期范围
        dates = [k["trade_date"] for k in klines]
        min_date, max_date = min(dates), max(dates)
        unique_dates = len(set(dates))
        total_unique += unique_dates
        
        logger.info(f"✅ {code}: {unique_dates}条(去重前{len(klines)}) | "
                     f"范围 {min_date} ~ {max_date} | 耗时{elapsed:.2f}s")
        
        # 字段抽样展示(前2条 + 最后1条)
        for k in [klines[0], klines[1], klines[-1]]:
            fields_str = ", ".join(
                f"{v}" if isinstance(v, (int, float)) else f"'{v}'"
                for v in [k.get("code"), k.get("trade_date"), k.get("open"),
                          k.get("high"), k.get("low"), k.get("close"),
                          k.get("volume"), k.get("turnover")]
            )
            logger.debug(f"   样例: ({fields_str})")
        
        all_results[code] = klines
        
        # 限流
        await asyncio.sleep(ths.rate_limit)
    
    collect_elapsed = _time.monotonic() - t_collect_start
    logger.info(f"\n📊 采集汇总: {len(codes)}只, "
                f"原始{total_raw}条 → 去重后{total_unique}条, "
                f"总耗时{collect_elapsed:.1f}s")
    
    # ---- Step 4: 字段校验(派生字段手动计算对比) ----
    logger.info(f"\n--- Step 3: 派生字段(change_pct/prev_close) 校验 ---")
    await verify_derived_fields(all_results)
    
    # ---- Step 5: 写入DB ----
    if dry_run:
        logger.info(f"\n--- Dry-run模式, 跳过DB写入 ---")
        return
    
    logger.info(f"\n--- Step 4: 写入 DB (batch upsert) ---")
    from app.data.scheduler import DataScheduler
    from app.models.stock import StockKline
    
    scheduler = DataScheduler()
    write_count = 0
    
    async with async_session() as session:
        for code in codes:
            klines = all_results.get(code, [])
            if not klines:
                continue
            
            records = []
            for k in klines:
                records.append({
                    "code": k["code"],
                    "trade_date": k["trade_date"],
                    "open": k.get("open"),
                    "close": k.get("close"),
                    "high": k.get("high"),
                    "low": k.get("low"),
                    "volume": k.get("volume"),
                    "amount": k.get("amount"),
                    "turnover": k.get("turnover"),
                    "change_pct": k.get("change_pct"),
                    "prev_close": k.get("prev_close"),
                    "source": k.get("source", "ths"),
                })
            
            await scheduler._batch_upsert(
                session, StockKline, records,
                unique_cols=["code", "trade_date"],
            )
            write_count += len(records)
        
        await session.commit()
    
    logger.info(f"✅ 写入完成: 共 {write_count} 条记录")
    
    # ---- Step 6: DB验证 ----
    logger.info(f"\n--- Step 5: DB 数据完整性验证 ---")
    await verify_db_data(codes)


async def verify_derived_fields(all_results: dict[str, list[dict]]):
    """逐条校验派生字段 change_pct 和 prev_close 的计算正确性
    
    规则(来自 ths_kline_source._calc_derived):
    - 第1条(i=0): prev_close=open, change_pct=0
    - 第i条(i>0): prev_close=前一条close, change_pct=(close-prev_close)/prev_close*100
    """
    errors = []
    checked = 0
    tolerance = 0.01  # 浮点误差容忍度
    
    for code, klines in all_results.items():
        if not klines:
            continue
        
        for i, k in enumerate(klines):
            checked += 1
            
            # 校验 prev_close
            if i == 0:
                expected_prev = k["open"]
            else:
                expected_prev = klines[i-1]["close"]
            
            actual_prev = k.get("prev_close", 0)
            if abs(actual_prev - expected_prev) > tolerance:
                errors.append(
                    f"{code}@{k['trade_date']}: prev_close={actual_prev}, "
                    f"期望={expected_prev}"
                )
            
            # 校验 change_pct
            if i == 0:
                expected_chg = 0
            else:
                pc = klines[i-1]["close"]
                if pc and pc > 0:
                    expected_chg = round((k["close"] - pc) / pc * 100, 2)
                else:
                    expected_chg = 0
            
            actual_chg = k.get("change_pct", 0)
            if abs(actual_chg - expected_chg) > tolerance:
                errors.append(
                    f"{code}@{k['trade_date']}: change_pct={actual_chg}, "
                    f"期望={expected_chg}(close={k['close']},prev_close={actual_prev})"
                )
    
    if errors:
        logger.warning(f"⚠️ 派生字段错误: {len(errors)}/{checked}")
        # 只展示前10条
        for e in errors[:10]:
            logger.warning(f"   ❌ {e}")
        if len(errors) > 10:
            logger.warning(f"   ... 还有 {len(errors)-10} 条错误")
    else:
        logger.info(f"✅ 派生字段校验通过: {checked} 条全部正确")


async def verify_db_data(codes):
    """验证DB中的数据完整性和准确性
    
    使用原生SQL避免SQLAlchemy Date类型解析问题(SQLite存TEXT)
    """
    from app.db.session import async_session, init_db
    from sqlalchemy import text as sa_text
    
    await init_db()
    
    async with async_session() as session:
        for code in codes:
            # 用原生SQL避免Date类型解析问题
            result = await session.execute(sa_text(
                "SELECT code, trade_date, open, close, high, low, volume, amount,"
                "turnover, change_pct, prev_close, source "
                f"FROM stock_kline WHERE code = :c ORDER BY trade_date"
            ), {"c": code})
            rows = result.all()
            
            if not rows:
                logger.warning(f"⚠️ {code}: DB中无数据")
                continue
            
            # 数量统计
            count = len(rows)
            dates = [str(r[1]) for r in rows]
            min_d, max_d = min(dates), max(dates)
            
            # 字段非空率 (tuple索引: 0=code 1=trade_date 2=open ... 11=source)
            field_idx = {"open":2, "close":3, "high":4, "low":5, "volume":6,
                        "amount":7, "turnover":8, "change_pct":9, "prev_close":10}
            field_stats = {}
            for fname, idx in field_idx.items():
                non_null = sum(1 for r in rows if r[idx] is not None)
                field_stats[fname] = f"{non_null}/{count}({non_null/count*100:.0f}%)"
            
            logger.info(f"📋 {code}: {count}条 | {min_d} ~ {max_d}")
            logger.info(f"   OHLC: open={field_stats['open']} "
                       f"close={field_stats['close']} "
                       f"high={field_stats['high']} low={field_stats['low']}")
            logger.info(f"   量额: volume={field_stats['volume']} "
                       f"amount={field_stats['amount']} "
                       f"turnover={field_stats['turnover']}")
            logger.info(f"   派生: change_pct={field_stats['change_pct']} "
                       f"prev_close={field_stats['prev_close']}")
            
            # 抽样展示最近5天
            for r in rows[-5:]:
                o, h, l, c = r[2], r[4], r[5], r[3]
                v, t, chg, pc = r[6], r[8], r[9], r[10]
                fmt = lambda x: f"{x:.2f}" if isinstance(x, (int, float)) and x else "NULL"
                logger.info(
                    f"   {r[1]}: O={fmt(o)} H={fmt(h)} L={fmt(l)} C={fmt(c)} "
                    f"V={v if v else 'NULL'} T={fmt(t)}% Δ={fmt(chg)}% PC={fmt(pc)}"
                )
            
            # 验证唯一约束: 检查是否有重复日期
            dup_result = await session.execute(sa_text(
                "SELECT code, trade_date, COUNT(*) as cnt "
                f"FROM stock_kline WHERE code = '{code}' "
                "GROUP BY code, trade_date HAVING cnt > 1"
            ))
            dups = dup_result.all()
            if dups:
                logger.error(f"❌ {code}: 发现重复记录! {[d for d in dups]}")
            else:
                logger.debug(f"   ✅ 无重复(code+date唯一)")
            
            # 验证排序: 日期应递增
            sorted_ok = all(
                str(rows[j][1]) >= str(rows[j-1][1])
                for j in range(1, len(rows))
            )
            if not sorted_ok:
                logger.error(f"❌ {code}: 日期未按顺序排列!")
            else:
                logger.debug(f"   ✅ 日期排序正确")


# ===== 入口 =====

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="个股日K线 init 回填测试")
    parser.add_argument("--codes", type=str, default=None,
                        help="指定股票代码(逗号分隔)，如 000001,600519")
    parser.add_argument("--dry-run", action="store_true",
                        help="只采集不写DB")
    parser.add_argument("--verify", action="store_true",
                        help="仅验证DB已有数据")
    args = parser.parse_args()
    
    target_codes = args.codes.split(",") if args.codes else SAMPLE_CODES
    
    asyncio.run(run_init_backfill(
        codes=target_codes,
        dry_run=args.dry_run,
        verify_only=args.verify,
    ))
