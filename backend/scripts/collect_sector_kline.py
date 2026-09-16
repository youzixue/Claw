"""板块K线数据采集 — AkShare板块指数历史

数据源:
- 概念板块K线: ak.stock_board_concept_index_ths(symbol="光伏概念")
- 行业板块K线: ak.stock_board_industry_index_ths(symbol="中药")

关键说明:
- 概念板块: pywencai概念名(如"光伏概念")与AkShare概念名一致，可直接用
- 行业板块: pywencai用三级名(如"医药生物-中药-中药Ⅲ"), AkShare用二级名(如"中药")
  → 采集时提取pywencai二级名调AkShare, 同一AkShare行业名下的多个三级行业共享K线

返回字段(实际):
- AkShare返回: 日期, 开盘价, 收盘价, 最高价, 最低价, 成交量, 成交额 (带"价"后缀)
- 不返回涨跌幅/振幅 → 自行计算: change_pct = (close - prev_close) / prev_close * 100
- 均线/趋势指标: 采集时计算(MA5/MA10/MA20/MA5量/trend_state/vol_ratio/support/resistance)

采集策略(3种模式):
1. 初始化(init): 全量250天历史, 首次部署用
2. 每日增量(daily): 请求30天+DB中已有历史→合并计算MA/趋势→只upsert新/变数据
3. 补漏(repair): 请求250天全量+全量upsert, 修正历史数据

每日增量核心逻辑:
- AkShare每次请求30天(API耗时≈250天一样快)
- 从DB加载每个板块已有最新30天K线 → 作为前序数据
- 合并(前序+新采集) → 重新计算MA5/MA10/MA20/trend/vol_ratio等
- 只upsert新增/变更的记录(批量ON CONFLICT DO UPDATE)

写入: SectorKline 表

性能(实测):
- AkShare API耗时与请求天数无关(1天≈250天≈0.6s)
- 概念板块: 0.6s/板块, 376个≈4分钟
- 行业板块: 0.3s/板块, 90个≈0.5分钟
- 每日增量(30天): 约5分钟(含DB读取+计算)

用法:
  # 首次初始化(250天全量)
  python3 scripts/collect_sector_kline.py --mode init

  # 每日盘后增量
  python3 scripts/collect_sector_kline.py --mode daily

  # 补漏修正(250天全量upsert)
  python3 scripts/collect_sector_kline.py --mode repair

  # 指定类型
  python3 scripts/collect_sector_kline.py --mode daily --type concept
  python3 scripts/collect_sector_kline.py --mode init --type industry

  # 指定单个板块
  python3 scripts/collect_sector_kline.py --mode daily --sector "光伏概念"
"""

import asyncio
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_, func, text

from app.db.session import async_session, init_db
from app.models.stock import SectorInfo
from app.models.sector import SectorKline


# ===== 常量 =====

DAILY_LOOKBACK = 30       # 每日增量请求天数
INIT_LOOKBACK = 250       # 初始化请求天数
MA_NEED_DAYS = 25         # MA20+趋势计算最少前序天数


# ===== AkShare 采集 =====

def fetch_sector_kline(symbol: str, sector_type: str = "concept",
                        start_date: str = "", end_date: str = "") -> pd.DataFrame:
    """采集单个板块K线数据(同步调用)
    
    注意: AkShare接口 start_date/end_date 为空字符串时会报错,
    需要不传参数(让接口返回全量)或传有效日期字符串(YYYYMMDD格式)
    """
    try:
        kwargs = {"symbol": symbol}
        # 只有非空字符串才传日期参数(AkShare要求YYYYMMDD格式)
        if start_date and start_date.strip():
            kwargs["start_date"] = start_date
        if end_date and end_date.strip():
            kwargs["end_date"] = end_date
        
        if sector_type == "concept":
            df = ak.stock_board_concept_index_ths(**kwargs)
        else:
            df = ak.stock_board_industry_index_ths(**kwargs)
        return df
    except Exception as e:
        logger.warning(f"采集失败 [{sector_type}:{symbol}]: {e}")
        return pd.DataFrame()


def _find_col(df: pd.DataFrame, keywords: str) -> str | None:
    """模糊匹配列名"""
    for col in df.columns:
        if keywords in col:
            return col
    return None


def parse_kline_df(df: pd.DataFrame, sector_code: str, sector_name: str,
                    sector_type: str) -> list[dict]:
    """解析K线DataFrame → SectorKline 记录列表(仅OHLCV, 不含指标)"""
    if df is None or df.empty:
        return []

    date_col = _find_col(df, "日期") or _find_col(df, "date")
    open_col = _find_col(df, "开盘") or _find_col(df, "open")
    high_col = _find_col(df, "最高") or _find_col(df, "high")
    low_col = _find_col(df, "最低") or _find_col(df, "low")
    close_col = _find_col(df, "收盘") or _find_col(df, "close")
    vol_col = _find_col(df, "成交量") or _find_col(df, "volume")
    amt_col = _find_col(df, "成交额") or _find_col(df, "amount")
    change_col = _find_col(df, "涨跌幅") or _find_col(df, "change_pct")
    amp_col = _find_col(df, "振幅") or _find_col(df, "amplitude")

    if not date_col:
        logger.warning(f"找不到日期列, 跳过 [{sector_code}]")
        return []

    records = []
    for _, row in df.iterrows():
        try:
            d = row[date_col]
            if isinstance(d, pd.Timestamp):
                trade_date = d.date()
            elif isinstance(d, date):
                trade_date = d
            elif isinstance(d, str):
                trade_date = date.fromisoformat(d[:10])
            else:
                continue

            def _safe_float(val):
                try:
                    v = float(val)
                    return v if pd.notna(v) else None
                except (ValueError, TypeError):
                    return None

            rec = {
                "sector_code": sector_code,
                "sector_name": sector_name,
                "sector_type": sector_type,
                "trade_date": trade_date,
                "open": _safe_float(row.get(open_col)) if open_col else None,
                "high": _safe_float(row.get(high_col)) if high_col else None,
                "low": _safe_float(row.get(low_col)) if low_col else None,
                "close": _safe_float(row.get(close_col)) if close_col else None,
                "volume": _safe_float(row.get(vol_col)) if vol_col else None,
                "amount": _safe_float(row.get(amt_col)) if amt_col else None,
                # 优先使用原始列值，后续 compute_indicators 可在需要时重新计算
                "change_pct": _safe_float(row.get(change_col)) if change_col else None,
                "amplitude": _safe_float(row.get(amp_col)) if amp_col else None,
                "ma5": None, "ma10": None, "ma20": None, "ma5_vol": None,
                "trend_state": None, "vol_ratio": None,
                "support_price": None, "resistance_price": None,
                "source": "akshare",
            }
            records.append(rec)
        except Exception:
            continue

    # 按日期排序
    records.sort(key=lambda r: r["trade_date"])
    return records


def compute_indicators(records: list[dict]) -> list[dict]:
    """对已排序的K线记录计算全部技术指标(涨跌幅/均线/趋势)
    
    可用于:
    - 全量模式: parse_kline_df → compute_indicators (与旧逻辑兼容)
    - 增量模式: DB前序+新采集 → 合并排序 → compute_indicators → 只取新增部分
    """
    if not records:
        return records

    # 第一步: 涨跌幅/振幅
    for i, rec in enumerate(records):
        if i > 0:
            prev_close = records[i - 1]["close"]
            if prev_close and prev_close > 0:
                if rec["close"] is not None:
                    rec["change_pct"] = round((rec["close"] - prev_close) / prev_close * 100, 2)
                if rec["high"] is not None and rec["low"] is not None:
                    rec["amplitude"] = round((rec["high"] - rec["low"]) / prev_close * 100, 2)

    # 第二步: 均线
    closes = [r["close"] for r in records]
    volumes = [r["volume"] for r in records]

    for i, rec in enumerate(records):
        if i >= 4 and all(c is not None for c in closes[i-4:i+1]):
            rec["ma5"] = round(sum(closes[i-4:i+1]) / 5, 3)
        if i >= 9 and all(c is not None for c in closes[i-9:i+1]):
            rec["ma10"] = round(sum(closes[i-9:i+1]) / 10, 3)
        if i >= 19 and all(c is not None for c in closes[i-19:i+1]):
            rec["ma20"] = round(sum(closes[i-19:i+1]) / 20, 3)
        if i >= 4 and all(v is not None for v in volumes[i-4:i+1]):
            rec["ma5_vol"] = round(sum(volumes[i-4:i+1]) / 5, 0)

    # 第三步: 趋势指标
    for i, rec in enumerate(records):
        if rec["volume"] and rec["ma5_vol"] and rec["ma5_vol"] > 0:
            rec["vol_ratio"] = round(rec["volume"] / rec["ma5_vol"], 2)

        lookback_start = max(0, i - 19)
        recent_lows = [records[j]["low"] for j in range(lookback_start, i + 1)
                       if records[j]["low"] is not None]
        if recent_lows:
            rec["support_price"] = round(min(recent_lows), 3)

        recent_highs = [records[j]["high"] for j in range(lookback_start, i + 1)
                        if records[j]["high"] is not None]
        if recent_highs:
            rec["resistance_price"] = round(max(recent_highs), 3)

        if rec["close"] and rec["ma5"] and rec["ma20"]:
            if rec["close"] > rec["ma5"] > rec["ma20"]:
                rec["trend_state"] = "breakout_up" if (rec["vol_ratio"] and rec["vol_ratio"] > 1.5) else "up"
            elif rec["close"] < rec["ma5"] < rec["ma20"]:
                rec["trend_state"] = "breakout_down" if (rec["vol_ratio"] and rec["vol_ratio"] > 1.5) else "down"
            else:
                rec["trend_state"] = "sideways"
        elif rec["close"] and rec["ma5"]:
            if rec["close"] > rec["ma5"]:
                rec["trend_state"] = "up"
            elif rec["close"] < rec["ma5"]:
                rec["trend_state"] = "down"
            else:
                rec["trend_state"] = "sideways"

    return records


# ===== DB 批量 upsert =====

_UPSERT_SQL = text("""
    INSERT INTO sector_kline (
        sector_code, sector_name, sector_type, trade_date,
        open, high, low, close, volume, amount,
        change_pct, amplitude,
        ma5, ma10, ma20, ma5_vol,
        trend_state, vol_ratio, support_price, resistance_price,
        source
    ) VALUES (
        :sector_code, :sector_name, :sector_type, :trade_date,
        :open, :high, :low, :close, :volume, :amount,
        :change_pct, :amplitude,
        :ma5, :ma10, :ma20, :ma5_vol,
        :trend_state, :vol_ratio, :support_price, :resistance_price,
        :source
    )
    ON CONFLICT(sector_code, trade_date) DO UPDATE SET
        sector_name=excluded.sector_name,
        open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
        volume=excluded.volume, amount=excluded.amount,
        change_pct=excluded.change_pct, amplitude=excluded.amplitude,
        ma5=excluded.ma5, ma10=excluded.ma10, ma20=excluded.ma20, ma5_vol=excluded.ma5_vol,
        trend_state=excluded.trend_state, vol_ratio=excluded.vol_ratio,
        support_price=excluded.support_price, resistance_price=excluded.resistance_price,
        source=excluded.source
""")


async def _batch_upsert(session, records: list[dict]):
    """批量upsert K线记录(ON CONFLICT DO UPDATE)"""
    if not records:
        return
    # trade_date需要转成字符串(SQLite兼容)
    for rec in records:
        if isinstance(rec.get("trade_date"), date):
            rec["trade_date"] = rec["trade_date"].isoformat()
    await session.execute(_UPSERT_SQL, records)


# ===== 名称映射 =====

def _extract_level2_name(pywencai_name: str) -> str | None:
    """从pywencai三级行业名中提取二级名

    例: "医药生物-中药-中药Ⅲ" → "中药"
        "电子-半导体-半导体Ⅲ" → "半导体"
    """
    parts = pywencai_name.split("-")
    if len(parts) >= 2:
        return parts[1]
    return None


def _get_kline_name(sector: "SectorInfo") -> str | None:
    """获取板块用于调AkShare接口的名称
    
    优先级:
    1. sector.kline_name (手动映射, 名称不同时填充)
    2. sector.sector_name (名称一致, 直接用)
    3. None (无法调AkShare, 如kline_name='')
    
    行业板块: 提取二级名
    """
    if sector.sector_type == "industry":
        return _extract_level2_name(sector.sector_name)
    
    # 概念板块
    if sector.kline_name is not None:
        if sector.kline_name == "":  # 空字符串=无K线源
            return None
        return sector.kline_name
    
    # kline_name为None=名称一致, 直接用sector_name
    return sector.sector_name


# ===== 主采集流程 =====

async def collect_kline(sector_type: str = "concept", days: int = 250,
                         sector_name: str | None = None,
                         mode: str = "daily"):
    """采集板块K线数据

    模式:
    - init: 首次初始化, 请求250天全量, 全部upsert
    - daily: 每日增量, 请求30天+DB前序数据→合并计算指标→只upsert新增
    - repair: 补漏修正, 请求250天全量, 全部upsert
    """
    await init_db()

    if mode == "init":
        days = INIT_LOOKBACK
    elif mode == "daily":
        days = DAILY_LOOKBACK
    elif mode == "repair":
        days = INIT_LOOKBACK
    else:
        days = days or INIT_LOOKBACK

    end_date = date.today()
    start_date = end_date - timedelta(days=int(days * 1.5))  # 预留非交易日
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")

    logger.info(f"=== 板块K线采集开始: mode={mode}, type={sector_type}, days={days}, "
                f"range={start_date}~{end_date} ===")

    if sector_type == "industry":
        await _collect_industry_kline(days, start_str, end_str, mode, sector_name)
    else:
        await _collect_concept_kline(days, start_str, end_str, mode, sector_name)


async def _load_existing_kline(session, sector_code: str, lookback_days: int = MA_NEED_DAYS) -> list[dict]:
    """从DB加载板块最近N天K线作为前序数据(用于计算MA/趋势)"""
    cutoff = date.today() - timedelta(days=lookback_days * 2)  # 多留余量
    result = await session.execute(
        select(SectorKline).where(
            and_(
                SectorKline.sector_code == sector_code,
                SectorKline.trade_date >= cutoff,
            )
        ).order_by(SectorKline.trade_date)
    )
    rows = result.scalars().all()
    return [
        {
            "sector_code": r.sector_code,
            "sector_name": r.sector_name,
            "sector_type": r.sector_type,
            "trade_date": r.trade_date,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
            "amount": r.amount,
            "change_pct": None,  # 会重算
            "amplitude": None,
            "ma5": None, "ma10": None, "ma20": None, "ma5_vol": None,
            "trend_state": None, "vol_ratio": None,
            "support_price": None, "resistance_price": None,
            "source": r.source,
        }
        for r in rows
    ]


def _merge_and_compute(existing: list[dict], new: list[dict]) -> list[dict]:
    """合并DB前序数据+新采集数据, 去重后计算指标
    
    返回: 合并后完整记录列表(已计算指标)
    """
    # 按日期去重: 新数据优先
    by_date: dict[date, dict] = {}
    for rec in existing:
        by_date[rec["trade_date"]] = rec
    for rec in new:
        by_date[rec["trade_date"]] = rec

    # 按日期排序
    merged = sorted(by_date.values(), key=lambda r: r["trade_date"])

    # 全量计算指标
    return compute_indicators(merged)


async def _collect_concept_kline(days: int, start_str: str, end_str: str,
                                  mode: str, sector_name: str | None):
    """采集概念板块K线"""
    # 获取板块列表
    async with async_session() as session:
        query = select(SectorInfo).where(
            and_(
                SectorInfo.source.in_(["pywencai", "akshare"]),
                SectorInfo.sector_type == "concept",
            )
        )
        if sector_name:
            query = query.where(SectorInfo.sector_name == sector_name)
        result = await session.execute(query)
        sectors = result.scalars().all()

    if not sectors:
        logger.warning("未找到 concept 板块")
        return

    # 统计映射情况
    mapped = sum(1 for s in sectors if s.kline_name and s.kline_name != "")
    skip_count = sum(1 for s in sectors if _get_kline_name(s) is None)
    logger.info(f"待采集概念板块: {len(sectors)}个 (名称映射={mapped}, 无K线源={skip_count})")

    # 串行采集(AkShare底层V8引擎线程不安全)
    t_total = time.time()
    total_records = 0
    total_upserted = 0
    error_count = 0
    skip_no_name = 0
    commit_batch = 20

    async with async_session() as session:
        for idx, s in enumerate(sectors):
            kname = _get_kline_name(s)
            if kname is None:
                skip_no_name += 1
                continue

            try:
                df = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_sector_kline, kname, "concept", start_str, end_str
                )
            except Exception as e:
                logger.warning(f"采集异常 [{s.sector_name}]: {e}")
                error_count += 1
                continue

            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                continue

            new_records = parse_kline_df(df, s.sector_code, s.sector_name, "concept")
            if not new_records:
                continue

            total_records += len(new_records)

            if mode == "daily":
                # 增量模式: 加载DB前序+合并计算→只upsert DB中没有的日期
                existing = await _load_existing_kline(session, s.sector_code)
                existing_dates = {r["trade_date"] for r in existing}
                merged = _merge_and_compute(existing, new_records)

                # 只upsertDB中不存在的日期(新增天)
                to_upsert = [r for r in merged
                             if r["trade_date"] not in existing_dates]
            else:
                # init/repair: 全量计算+全量upsert
                to_upsert = compute_indicators(new_records)

            if to_upsert:
                total_upserted += len(to_upsert)
                await _batch_upsert(session, to_upsert)

            # 批量提交
            if (idx + 1) % commit_batch == 0 or (idx + 1) == len(sectors):
                await session.commit()

            if (idx + 1) % 50 == 0:
                logger.info(f"概念进度: {idx+1}/{len(sectors)}, "
                            f"采集{total_records}条→upsert{total_upserted}条, "
                            f"失败{error_count}个, 耗时{time.time()-t_total:.0f}s")

    elapsed = time.time() - t_total
    logger.info(f"=== 概念K线采集完成: 采集{total_records}条→upsert{total_upserted}条, "
                f"失败{error_count}个, 耗时{elapsed:.0f}s ===")


async def _collect_industry_kline(days: int, start_str: str, end_str: str,
                                   mode: str, sector_name: str | None):
    """采集行业板块K线(N对1: 多个pywencai三级行业共享一个AkShare二级行业K线)

    策略:
    1. 获取所有pywencai行业板块(257个)
    2. 提取二级名, 按(AkShare)二级名分组 → 90个组
    3. 只调90次AkShare接口(而非257次)
    4. 每组K线数据复制到组内所有三级行业
    """
    # 获取所有pywencai行业板块
    async with async_session() as session:
        query = select(SectorInfo).where(
            and_(
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type == "industry",
            )
        )
        if sector_name:
            query = query.where(SectorInfo.sector_name == sector_name)
        result = await session.execute(query)
        all_industries = result.scalars().all()

    if not all_industries:
        logger.warning("未找到 industry 板块")
        return

    # 按二级名分组: {akshare_name: [SectorInfo, ...]}
    level2_groups: dict[str, list] = {}
    skipped = 0
    for s in all_industries:
        kname = _get_kline_name(s)
        if kname:
            if kname not in level2_groups:
                level2_groups[kname] = []
            level2_groups[kname].append(s)
        else:
            skipped += 1

    logger.info(f"待采集行业: {len(level2_groups)}个AkShare二级行业, "
                f"覆盖{len(all_industries)-skipped}个pywencai三级行业"
                f"{f', 跳过{skipped}个' if skipped else ''}")

    # 串行采集
    t_total = time.time()
    total_records = 0
    total_upserted = 0
    error_count = 0
    commit_batch = 10

    ak_names = list(level2_groups.keys())

    async with async_session() as session:
        for idx, ak_name in enumerate(ak_names):
            try:
                df = await asyncio.get_event_loop().run_in_executor(
                    None, fetch_sector_kline, ak_name, "industry", start_str, end_str
                )
            except Exception as e:
                logger.warning(f"采集异常 [{ak_name}]: {e}")
                error_count += 1
                continue

            if df is None or (isinstance(df, pd.DataFrame) and df.empty):
                continue

            # 为组内每个pywencai三级行业生成K线记录
            sectors_in_group = level2_groups[ak_name]
            for s in sectors_in_group:
                new_records = parse_kline_df(df, s.sector_code, s.sector_name, "industry")
                if not new_records:
                    continue
                total_records += len(new_records)

                if mode == "daily":
                    # 增量模式: 只upsert DB中不存在的日期
                    existing = await _load_existing_kline(session, s.sector_code)
                    existing_dates = {r["trade_date"] for r in existing}
                    merged = _merge_and_compute(existing, new_records)
                    to_upsert = [r for r in merged
                                 if r["trade_date"] not in existing_dates]
                else:
                    to_upsert = compute_indicators(new_records)

                if to_upsert:
                    total_upserted += len(to_upsert)
                    await _batch_upsert(session, to_upsert)

            # 批量提交
            if (idx + 1) % commit_batch == 0 or (idx + 1) == len(ak_names):
                await session.commit()

            if (idx + 1) % 10 == 0:
                logger.info(f"行业进度: {idx+1}/{len(ak_names)}, "
                            f"采集{total_records}条→upsert{total_upserted}条, "
                            f"失败{error_count}个, 耗时{time.time()-t_total:.0f}s")

    elapsed = time.time() - t_total
    logger.info(f"=== 行业K线采集完成: 采集{total_records}条→upsert{total_upserted}条, "
                f"失败{error_count}个, 耗时{elapsed:.0f}s ===")


# ===== 入口 =====

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="板块K线数据采集")
    parser.add_argument("--mode", choices=["init", "daily", "repair"],
                        default="daily", help="采集模式: init=首次初始化(250天), daily=每日增量(30天), repair=补漏修正(250天)")
    parser.add_argument("--type", choices=["concept", "industry", "all"],
                        default="all", help="板块类型(默认all)")
    parser.add_argument("--days", type=int, default=0,
                        help="覆盖天数(默认: init/repair=250, daily=30)")
    parser.add_argument("--sector", type=str, default=None,
                        help="指定板块名称(如'光伏概念')")
    args = parser.parse_args()

    async def main():
        if args.days:
            # 用户手动指定天数
            mode = "custom"
        else:
            mode = args.mode

        if args.type == "all":
            await collect_kline("concept", days=args.days or 0, sector_name=args.sector, mode=mode)
            await collect_kline("industry", days=args.days or 0, sector_name=args.sector, mode=mode)
        else:
            await collect_kline(args.type, days=args.days or 0, sector_name=args.sector, mode=mode)

    asyncio.run(main())
