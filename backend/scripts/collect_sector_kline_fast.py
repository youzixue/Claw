"""板块K线快速采集 — 绕过AkShare V8瓶颈，直接请求同花顺API

核心优化:
1. 预计算v_code(0.1s) + 预缓存inner_code(30s, 并发8) → 后续请求无需再调V8
2. ThreadPoolExecutor并发5请求同花顺K线API → 线程安全(V8已不在路径上)
3. 行业板块: 直接用symbol_code请求(行业不需要inner_code)

性能对比(376个概念):
- AkShare原版串行: ≈3.5分钟
- fast并发5: ≈15秒 (含预缓存30秒 = 总计45秒)

用法:
  # 每日盘后增量(推荐)
  python3 scripts/collect_sector_kline_fast.py --mode daily

  # 首次初始化(250天全量)
  python3 scripts/collect_sector_kline_fast.py --mode init

  # 补漏修正
  python3 scripts/collect_sector_kline_fast.py --mode repair
"""

import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import akshare as ak
import pandas as pd
import py_mini_racer
import requests
from bs4 import BeautifulSoup
from loguru import logger
from sqlalchemy import select, and_, func, text

from app.db.session import async_session, init_db
from app.models.stock import SectorInfo
from app.models.sector import SectorKline

# 从原版脚本复用解析和计算逻辑
from scripts.collect_sector_kline import (
    parse_kline_df,
    compute_indicators,
    _merge_and_compute,
    _batch_upsert,
    _load_existing_kline,
    _extract_level2_name,
    _UPSERT_SQL,
    DAILY_LOOKBACK,
    INIT_LOOKBACK,
    MA_NEED_DAYS,
)


# ===== 常量 =====
CONCURRENCY = 5          # 并发线程数(实测5最优, 8会触发限流)
CACHE_CONCURRENCY = 8    # 预缓存inner_code的并发数


# ===== 同花顺直接请求层 =====

class ThsKlineFetcher:
    """同花顺K线直接请求器(绕过AkShare V8瓶颈)
    
    初始化流程:
    1. 加载ths.js → 计算v_code (0.1s)
    2. 加载概念/行业code_map (5s)
    3. 并发预缓存概念板块inner_code (30s)
    
    之后每次请求只需直接HTTP调K线API，线程安全
    """
    
    def __init__(self):
        self.v_code = None
        self.concept_code_map = {}   # {概念名: symbol_code}
        self.industry_code_map = {}  # {行业名: symbol_code}
        self.inner_code_map = {}     # {概念名: inner_code}
        self._initialized = False
    
    def init(self, sector_type: str = "concept"):
        """初始化: 预计算v_code + 预缓存inner_code
        
        Args:
            sector_type: "concept"或"industry"或"all", 只初始化需要的类型
        """
        if self._initialized:
            return
        
        t0 = time.time()
        
        # 1. 预计算v_code
        js_path = self._find_ths_js()
        with open(js_path, "r") as f:
            js_content = f.read()
        js_engine = py_mini_racer.MiniRacer()
        js_engine.eval(js_content)
        self.v_code = js_engine.call("v")
        logger.info(f"v_code获取: {time.time()-t0:.2f}s")
        
        # 2. 加载code_map(按需)
        if sector_type in ("concept", "all"):
            t1 = time.time()
            from akshare.stock_feature.stock_board_concept_ths import _get_stock_board_concept_name_ths
            self.concept_code_map = _get_stock_board_concept_name_ths()
            logger.info(f"概念code_map: {len(self.concept_code_map)}个, 耗时{time.time()-t1:.2f}s")
        
        if sector_type in ("industry", "all"):
            t2 = time.time()
            from akshare.stock_feature.stock_board_industry_ths import _get_stock_board_industry_name_ths
            self.industry_code_map = _get_stock_board_industry_name_ths()
            logger.info(f"行业code_map: {len(self.industry_code_map)}个, 耗时{time.time()-t2:.2f}s")
        
        # 3. 预缓存inner_code(仅概念板块需要)
        if sector_type in ("concept", "all"):
            self._cache_inner_codes()
        
        self._initialized = True
        logger.info(f"ThsKlineFetcher初始化完成, 总耗时{time.time()-t0:.1f}s")
    
    def _find_ths_js(self) -> str:
        """查找ths.js路径"""
        import inspect
        con_src = inspect.getfile(ak.stock_board_concept_index_ths)
        pkg_dir = os.path.dirname(con_src)
        js_path = os.path.join(pkg_dir, "ths.js")
        if os.path.exists(js_path):
            return js_path
        # 备用搜索
        for root, dirs, files in os.walk(pkg_dir):
            if "ths.js" in files:
                return os.path.join(root, "ths.js")
        raise FileNotFoundError("ths.js not found")
    
    def _cache_inner_codes(self):
        """并发预缓存概念板块inner_code(带本地缓存)
        
        缓存策略:
        - 本地文件: claw.db同目录下inner_code_cache.json
        - 有效期: 7天(板块代码不会频繁变化)
        - 缓存命中: 直接加载(0.1s)
        - 缓存过期/缺失: 并发获取+写入(约30s)
        """
        import sqlite3
        db_path = Path(__file__).resolve().parent.parent / "claw.db"
        cache_path = db_path.parent / "inner_code_cache.json"
        
        # 尝试加载缓存
        if cache_path.exists():
            try:
                with open(cache_path, "r") as f:
                    cache = json.load(f)
                cache_time = cache.get("timestamp", 0)
                cache_age = time.time() - cache_time
                if cache_age < 7 * 86400:  # 7天有效期
                    self.inner_code_map = cache.get("data", {})
                    logger.info(f"inner_code缓存命中: {len(self.inner_code_map)}个, "
                                f"缓存龄{cache_age/3600:.1f}小时")
                    return
                else:
                    logger.info(f"inner_code缓存过期(龄{cache_age/86400:.1f}天), 重新获取")
            except Exception as e:
                logger.warning(f"inner_code缓存加载失败: {e}, 重新获取")
        
        # 并发获取
        t0 = time.time()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cookie": f"v={self.v_code}",
        }
        
        def _get_one(item):
            name, symbol_code = item
            url = f"https://q.10jqka.com.cn/gn/detail/code/{symbol_code}/"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                soup = BeautifulSoup(r.text, features="lxml")
                inner = soup.find(name="input", attrs={"id": "clid"})
                if inner:
                    return (name, inner["value"])
            except:
                pass
            return (name, None)
        
        items = list(self.concept_code_map.items())
        total = len(items)
        
        with ThreadPoolExecutor(max_workers=CACHE_CONCURRENCY) as pool:
            futures = {pool.submit(_get_one, item): item for item in items}
            done_count = 0
            for future in as_completed(futures):
                name, inner_code = future.result()
                if inner_code:
                    self.inner_code_map[name] = inner_code
                done_count += 1
                if done_count % 50 == 0:
                    logger.info(f"inner_code缓存进度: {done_count}/{total}")
        
        elapsed = time.time() - t0
        logger.info(f"inner_code缓存完成: {len(self.inner_code_map)}/{total}个, 耗时{elapsed:.1f}s")
        
        # 写入缓存
        try:
            with open(cache_path, "w") as f:
                json.dump({"timestamp": time.time(), "data": self.inner_code_map}, f)
            logger.info(f"inner_code缓存已写入: {cache_path}")
        except Exception as e:
            logger.warning(f"inner_code缓存写入失败: {e}")
    
    def fetch_concept_kline(self, name: str, start_date: str, end_date: str) -> pd.DataFrame:
        """采集概念板块K线(直接请求同花顺API)"""
        inner_code = self.inner_code_map.get(name)
        if not inner_code:
            return pd.DataFrame()
        return self._fetch_kline(inner_code, start_date, end_date)
    
    def fetch_industry_kline(self, name: str, start_date: str, end_date: str) -> pd.DataFrame:
        """采集行业板块K线(直接请求同花顺API)
        
        行业不需要inner_code, 直接用symbol_code请求
        """
        symbol_code = self.industry_code_map.get(name)
        if not symbol_code:
            return pd.DataFrame()
        return self._fetch_kline(symbol_code, start_date, end_date)
    
    def _fetch_kline(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """直接请求同花顺K线API(线程安全)"""
        current_year = datetime.now().year
        begin_year = int(start_date[:4])
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "http://q.10jqka.com.cn",
            "Host": "d.10jqka.com.cn",
            "Cookie": f"v={self.v_code}",
        }
        big_df = pd.DataFrame()
        for year in range(begin_year, current_year + 1):
            url = f"https://d.10jqka.com.cn/v4/line/bk_{code}/01/{year}.js"
            try:
                r = requests.get(url, headers=headers, timeout=10)
                data_text = r.text
                json_str = data_text[data_text.find("{"):-1]
                try:
                    decoded = json.loads(json_str)
                except json.JSONDecodeError:
                    decoded = eval(json_str)
                temp_df = pd.DataFrame(decoded["data"].split(";"))
                temp_df = temp_df.iloc[:, 0].str.split(",", expand=True)
                big_df = pd.concat(objs=[big_df, temp_df], ignore_index=True)
            except:
                continue
        
        if big_df.empty:
            return big_df
        
        big_df = big_df.iloc[:, :7]
        big_df.columns = ["日期", "开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"]
        big_df["日期"] = pd.to_datetime(big_df["日期"], errors="coerce").dt.date
        big_df.index = pd.to_datetime(big_df["日期"], errors="coerce")
        big_df = big_df[start_date:end_date]
        big_df.reset_index(drop=True, inplace=True)
        for col in ["开盘价", "最高价", "最低价", "收盘价", "成交量", "成交额"]:
            big_df[col] = pd.to_numeric(big_df[col], errors="coerce")
        return big_df


# ===== 名称映射 =====

def _get_kline_name(sector: "SectorInfo") -> str | None:
    """获取板块用于调AkShare接口的名称(与原版一致)"""
    if sector.sector_type == "industry":
        return _extract_level2_name(sector.sector_name)
    
    if sector.kline_name is not None:
        if sector.kline_name == "":
            return None
        return sector.kline_name
    
    return sector.sector_name


# ===== 主采集流程 =====

async def collect_kline_fast(sector_type: str = "concept", days: int = 250,
                              sector_name: str | None = None,
                              mode: str = "daily"):
    """快速采集板块K线数据(并发5, 绕过V8)"""
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
    start_date = end_date - timedelta(days=int(days * 1.5))
    start_str = start_date.strftime("%Y%m%d")
    end_str = end_date.strftime("%Y%m%d")
    
    logger.info(f"=== 板块K线快速采集开始: mode={mode}, type={sector_type}, days={days}, "
                f"range={start_date}~{end_date} ===")
    
    # 初始化请求器(按需)
    fetcher = ThsKlineFetcher()
    if sector_type == "all":
        fetcher.init("all")
    else:
        fetcher.init(sector_type)
    
    if sector_type == "industry":
        await _collect_industry_kline_fast(fetcher, days, start_str, end_str, mode, sector_name)
    else:
        await _collect_concept_kline_fast(fetcher, days, start_str, end_str, mode, sector_name)


async def _collect_concept_kline_fast(fetcher: ThsKlineFetcher, days: int,
                                       start_str: str, end_str: str,
                                       mode: str, sector_name: str | None):
    """并发采集概念板块K线"""
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
    
    # 过滤: 只采集有inner_code的板块
    fetchable = []
    skip_no_name = 0
    for s in sectors:
        kname = _get_kline_name(s)
        if kname is None:
            skip_no_name += 1
        elif kname in fetcher.inner_code_map:
            fetchable.append(s)
        else:
            skip_no_name += 1
    
    logger.info(f"待采集概念板块: {len(fetchable)}个 (无K线源={skip_no_name})")
    
    # 并发采集(AkShare V8已不在路径上, requests线程安全)
    t_total = time.time()
    total_records = 0
    total_upserted = 0
    error_count = 0
    
    def _fetch_one(sector):
        """单个板块采集(线程中执行)"""
        name = _get_kline_name(sector)
        df = fetcher.fetch_concept_kline(name, start_str, end_str)
        if df is None or df.empty:
            return (sector, None, "empty")
        records = parse_kline_df(df, sector.sector_code, sector.sector_name, "concept")
        return (sector, records, None)
    
    # 分批处理: 每批CONCURRENCY个并发请求 → 解析+upsert → 下一批
    batch_size = CONCURRENCY * 4  # 每批20个
    async with async_session() as session:
        for batch_start in range(0, len(fetchable), batch_size):
            batch = fetchable[batch_start:batch_start + batch_size]
            
            # 并发请求
            batch_results = []
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                futures = {pool.submit(_fetch_one, s): s for s in batch}
                for future in as_completed(futures):
                    try:
                        result = future.result(timeout=30)
                        batch_results.append(result)
                    except Exception as e:
                        logger.warning(f"采集异常: {e}")
                        error_count += 1
            
            # 串行解析+upsert(需要async session)
            for sector, records, err in batch_results:
                if err or not records:
                    if err and err != "empty":
                        error_count += 1
                    continue
                
                total_records += len(records)
                
                if mode == "daily":
                    existing = await _load_existing_kline(session, sector.sector_code)
                    existing_dates = {r["trade_date"] for r in existing}
                    merged = _merge_and_compute(existing, records)
                    to_upsert = [r for r in merged if r["trade_date"] not in existing_dates]
                else:
                    to_upsert = compute_indicators(records)
                
                if to_upsert:
                    total_upserted += len(to_upsert)
                    await _batch_upsert(session, to_upsert)
            
            # 每批提交
            await session.commit()
            
            done = min(batch_start + batch_size, len(fetchable))
            if done % 50 == 0 or done == len(fetchable):
                logger.info(f"概念进度: {done}/{len(fetchable)}, "
                            f"采集{total_records}条→upsert{total_upserted}条, "
                            f"失败{error_count}个, 耗时{time.time()-t_total:.0f}s")
    
    elapsed = time.time() - t_total
    logger.info(f"=== 概念K线快速采集完成: 采集{total_records}条→upsert{total_upserted}条, "
                f"失败{error_count}个, 耗时{elapsed:.0f}s ===")


async def _collect_industry_kline_fast(fetcher: ThsKlineFetcher, days: int,
                                         start_str: str, end_str: str,
                                         mode: str, sector_name: str | None):
    """并发采集行业板块K线(行业不需要inner_code, 直接用symbol_code)"""
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
    
    logger.info(f"待采集行业: {len(level2_groups)}个二级, "
                f"覆盖{len(all_industries)-skipped}个三级"
                f"{f', 跳过{skipped}个' if skipped else ''}")
    
    # 并发采集(行业直接用symbol_code)
    t_total = time.time()
    total_records = 0
    total_upserted = 0
    error_count = 0
    
    ak_names = list(level2_groups.keys())
    
    def _fetch_one_industry(ak_name):
        df = fetcher.fetch_industry_kline(ak_name, start_str, end_str)
        if df is None or df.empty:
            return (ak_name, None, "empty")
        return (ak_name, df, None)
    
    batch_size = CONCURRENCY * 4
    async with async_session() as session:
        for batch_start in range(0, len(ak_names), batch_size):
            batch = ak_names[batch_start:batch_start + batch_size]
            
            # 并发请求
            batch_results = []
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                futures = {pool.submit(_fetch_one_industry, name): name for name in batch}
                for future in as_completed(futures):
                    try:
                        result = future.result(timeout=30)
                        batch_results.append(result)
                    except Exception as e:
                        logger.warning(f"行业采集异常: {e}")
                        error_count += 1
            
            # 串行解析+upsert
            for ak_name, df, err in batch_results:
                if err or df is None or df.empty:
                    if err and err != "empty":
                        error_count += 1
                    continue
                
                sectors_in_group = level2_groups[ak_name]
                for s in sectors_in_group:
                    new_records = parse_kline_df(df, s.sector_code, s.sector_name, "industry")
                    if not new_records:
                        continue
                    total_records += len(new_records)
                    
                    if mode == "daily":
                        existing = await _load_existing_kline(session, s.sector_code)
                        existing_dates = {r["trade_date"] for r in existing}
                        merged = _merge_and_compute(existing, new_records)
                        to_upsert = [r for r in merged if r["trade_date"] not in existing_dates]
                    else:
                        to_upsert = compute_indicators(new_records)
                    
                    if to_upsert:
                        total_upserted += len(to_upsert)
                        await _batch_upsert(session, to_upsert)
            
            await session.commit()
            
            done = min(batch_start + batch_size, len(ak_names))
            if done % 20 == 0 or done == len(ak_names):
                logger.info(f"行业进度: {done}/{len(ak_names)}, "
                            f"采集{total_records}条→upsert{total_upserted}条, "
                            f"失败{error_count}个, 耗时{time.time()-t_total:.0f}s")
    
    elapsed = time.time() - t_total
    logger.info(f"=== 行业K线快速采集完成: 采集{total_records}条→upsert{total_upserted}条, "
                f"失败{error_count}个, 耗时{elapsed:.0f}s ===")


# ===== 入口 =====

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="板块K线快速采集(并发)")
    parser.add_argument("--mode", choices=["init", "daily", "repair"],
                        default="daily", help="采集模式")
    parser.add_argument("--type", choices=["concept", "industry", "all"],
                        default="all", help="板块类型(默认all)")
    parser.add_argument("--days", type=int, default=0, help="覆盖天数")
    parser.add_argument("--sector", type=str, default=None, help="指定板块名称")
    args = parser.parse_args()

    async def main():
        if args.days:
            mode = "custom"
        else:
            mode = args.mode

        if args.type == "all":
            await collect_kline_fast("concept", days=args.days or 0, sector_name=args.sector, mode=mode)
            await collect_kline_fast("industry", days=args.days or 0, sector_name=args.sector, mode=mode)
        else:
            await collect_kline_fast(args.type, days=args.days or 0, sector_name=args.sector, mode=mode)

    asyncio.run(main())
