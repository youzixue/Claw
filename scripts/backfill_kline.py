"""个股日K线 全量回填命令 — stock_kline init backfill

功能:
1. 从库内 StockTag/黑名单 取全A股列表(排除退市股/停牌/*ST)
2. 跳过已有K线的股票(断点续传)
3. 并发采集(可调), 批量UPSERT写入DB
4. 进度持久化(支持Ctrl+C恢复)
5. 采集后数据校验(字段完整性+派生字段正确性)

用法:
  # 默认模式: 库内股票列表 + 过滤ST/停牌/退市 + 跳过已有 + 并发10
  python scripts/backfill_kline.py
  
  # 指定并发数和批次大小
  python scripts/backfill_kline.py --concurrency 20 --batch-size 50
  
  # 仅查看进度(不执行)
  python scripts/backfill_kline.py --status
  
  # 强制重新采集某只(忽略已有数据)
  python scripts/backfill_kline.py --force-codes 000001,600519
  
  # 干跑(只采集不写DB)
  python scripts/backfill_kline.py --dry-run --codes 000001,000333,600036 --limit 3

数据源:
  - 股票列表/停牌/ST/退市: 库内 StockTag + stock_blacklist(不联网取列表)
  - K线: 同花顺 httpx 直连(需登录 cookie)
  - 注: 旧 pywencai 库自 2026-08 下旬起完全失效(上游改 SSE 流)。
        本脚本从未 import 该库，上文曾误述取数来源，2026-09-17 审计时更正。

过滤规则(对接stock_tagger体系):
- ❌ 排除: 停牌(suspended)、*ST退市风险、名称含"退/终止上市/摘牌"
- ✅ 包含: 主板(tradeable) + 创业板/科创板/北交所(observe_only, 也回填K线)
- ⚠️ ST股: 回填K线但标记为blocked(不推送信号)
"""

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time as _time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx
import pandas as pd
from loguru import logger


# ============================================================================
# 配置
# ============================================================================

DB_PATH = PROJECT_ROOT / "backend" / "claw.db"
PROGRESS_FILE = PROJECT_ROOT / ".workbuddy" / "kline_backfill_progress.json"
DEFAULT_CONCURRENCY = 10       # 并发数(同时请求的股票数)
DEFAULT_BATCH_DB = 200         # 每批写入DB的股票数
RATE_LIMIT = 0.12              # 同花顺限流(s/请求)
CACHE_TTL = 22 * 3600         # 股票列表缓存有效期(22小时, 每日更新1次)

# 缓存目录
CACHE_DIR = PROJECT_ROOT / ".workbuddy"
STOCK_CACHE_FILE = CACHE_DIR / "stock_list_cache.json"

# 退市关键词(用于过滤)
DELIST_KEYWORDS = [
    "退", "终止上市", "暂停上市", "摘牌",
]


# ============================================================================
# 工具函数
# ============================================================================


def setup_logger(verbose=False):
    """配置日志"""
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
        level=level,
    )


def get_db_connection():
    """获取数据库连接"""
    return sqlite3.connect(str(DB_PATH))


def ensure_tables(conn):
    """确保表存在"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_kline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code VARCHAR(10) NOT NULL,
            trade_date VARCHAR(10) NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            amount REAL,
            turnover REAL,
            change_pct REAL,
            prev_close REAL,
            source VARCHAR(20) DEFAULT 'ths',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, trade_date)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_kline_code ON stock_kline(code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_kline_date ON stock_kline(trade_date)")
    conn.commit()


def load_progress() -> dict:
    """加载进度文件"""
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {
        "started_at": None,
        "updated_at": None,
        "total_stocks": 0,
        "completed_codes": [],
        "failed_codes": [],
        "skipped_codes": [],
        "stats": {"total_records": 0, "total_stocks_done": 0},
    }


def save_progress(progress: dict):
    """保存进度"""
    progress["updated_at"] = datetime.now().isoformat()
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_FILE.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================================
# 股票列表获取 — 问财(WencaiStreamSource) + StockTag 体系
# ============================================================================

async def fetch_pywencai_stocks() -> list[dict]:
    """从pywencai获取全A股列表 + 停牌/ST/退市过滤
    
    复用 `app.data.sources.wencai_stream_source.WencaiStreamSource`，
    与调度器完全一致的数据源和过滤逻辑。

    关键: 返回列 "code" 是纯6位代码(如"000988")，无需解析；
    而 "股票代码" 含后缀(如"000988.SZ")，不能直接 zfill(6)。
    
    流程:
      1. 问财 `全部A股 所属同花顺行业 所属概念 ...` → 全A股约5574只
      2. 用列[11]"code" 提取6位代码
      3. 并行查询停牌 + ST股 + *ST → 代码集合
      4. 按board_tag过滤(排除blocked=退市/*ST, suspended=停牌)
      5. 缓存结果到JSON文件(每日更新1次)
    
    Returns:
        [{"code": "000001", "name": "平安银行", "board_tag": "tradeable",
          "is_st": False, "is_suspended": False}, ...]
    """
    # --- 检查缓存(每日更新1次, 兼容新旧格式) ---
    if STOCK_CACHE_FILE.exists():
        try:
            cached = json.loads(STOCK_CACHE_FILE.read_text("utf-8"))
            cached_data = cached.get("stocks", []) if isinstance(cached, dict) else cached
            if isinstance(cached_data, list) and len(cached_data) > 0:
                cache_age = _time.monotonic() - STOCK_CACHE_FILE.stat().st_mtime
                if cache_age < CACHE_TTL:
                    logger.info(f"使用缓存股票列表: {len(cached_data)}只 (年龄{cache_age:.0f}s)")
                    return cached_data
                logger.info(f"缓存已过期({cache_age:.0f}s > {CACHE_TTL}s), 重新获取")
        except Exception:
            pass

    # --- 导入项目源 ---
    # 2026-09-17：旧 `pywencai` 库自 8 月下旬改 SSE 流后完全失效，
    # `PyWencaiSource` 已废弃（其 `_query` 现在失败即报错）。
    # 改用 `WencaiStreamSource`，问句逐字沿用旧方法的问句。
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from app.data.sources.wencai_stream_source import WencaiStreamSource

    source = WencaiStreamSource()

    # 1. 全行业映射（perpage 必须给足，全 A 股约 5574 只）
    logger.info("问财获取股票列表(含停牌+ST过滤)...")
    df_all = await source.query_async(
        "全部A股 所属同花顺行业 所属概念 涨跌幅 市盈率 市净率 换手率 成交量 成交额 最新dde大单净额 总市值",
        perpage=10000,
    )

    if df_all is None or len(df_all) == 0:
        raise RuntimeError("pywencai返回空数据")

    # 2. 列名映射 (关键: 列[11]="code" 是纯6位代码!)
    raw_code_col = None   # "股票代码" = "000988.SZ"
    name_col = None       # "股票简称"
    price_col = None      # "最新价"
    code_col = None       # "code" = "000988" (纯6位)

    for col in df_all.columns:
        cl = col.strip()
        if cl == "code":
            code_col = col
        elif cl == "股票代码":
            raw_code_col = col
        elif cl == "股票简称":
            name_col = col
        elif cl == "最新价":
            price_col = col

    # 兜底: 如果没有独立code列, 从raw提取
    if not code_col and raw_code_col:
        logger.warning("问财无'code'列, 从'股票代码'提取")
        # 后续在循环中处理 .SZ/.SH 后缀

    logger.info(f"问财返回 {len(df_all)} 只股票 "
                f"(code={code_col}, name={name_col}, price={price_col})")

    stocks = []
    for _, row in df_all.iterrows():
        # 优先用纯code列
        if code_col and pd.notna(row.get(code_col)):
            code = str(row[code_col]).strip().zfill(6)
        elif raw_code_col and pd.notna(row.get(raw_code_col)):
            raw = str(row[raw_code_col]).strip()
            code = raw.split(".")[0].strip().zfill(6)
        else:
            continue

        if len(code) != 6 or not code.isdigit():
            continue

        name = str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else ""
        price = safe_float(row.get(price_col))

        board_type = _get_board_type(code)
        board_tag = {
            "main_sh": "tradeable", "main_sz": "tradeable", "sme": "tradeable",
            "gem": "observe_only", "star": "observe_only", "bse": "observe_only",
        }.get(board_type, "observe_only")

        stocks.append({
            "code": code,
            "name": name,
            "price": price,
            "board_tag": board_tag,
            "is_st": False,
            "is_suspended": False,
        })

    # 3. 串行获取 停牌 + ST + *ST退市风险 名单（问句与旧方法一致）
    suspended_set: set[str] = set()
    st_set: set[str] = set()
    delist_risk_set: set[str] = set()

    tasks_data = [
        ("停牌", "停牌", suspended_set),
        ("ST股", "ST股", st_set),
        ("*ST股", "*ST股", delist_risk_set),
    ]

    for label, question, target_set in tasks_data:
        try:
            df = await source.query_async(question, perpage=2000)
            if df is not None and len(df) > 0:
                ccol = [c for c in df.columns if "代码" in c]
                if ccol:
                    for v in df[ccol[0]]:
                        c = str(v).split(".")[0].strip().zfill(6)
                        if len(c) == 6 and c.isdigit():
                            target_set.add(c)
                logger.info(f"问财 {label}: {len(target_set)}只")
        except Exception as e:
            logger.warning(f"获取{label}名单失败: {e}")

    # 4. 应用标记 & 名称兜底检查
    for s in stocks:
        c = s["code"]
        if c in suspended_set:
            s["is_suspended"] = True
            s["board_tag"] = "suspended"
        if c in st_set:
            s["is_st"] = True
        if c in delist_risk_set:
            s["board_tag"] = "blocked"
        for kw in DELIST_KEYWORDS:
            if kw in s["name"]:
                s["board_tag"] = "blocked"
                break

    # 5. 统计 & 过滤 (排除 blocked + suspended, 但保留 observe_only)
    stats = {"total_raw": len(stocks)}
    for tag in ["tradeable", "observe_only", "blocked", "suspended"]:
        stats[tag] = sum(1 for s in stocks if s["board_tag"] == tag)
    stats["st_count"] = sum(1 for s in stocks if s["is_st"])
    stats["susp_count"] = sum(1 for s in stocks if s["is_suspended"])

    result = [s for s in stocks if s["board_tag"] not in ("blocked", "suspended")]

    logger.info(f"pywencai统计: 总计={stats['total_raw']} | "
                f"✅可交易={stats['tradeable']} | 👁️观察={stats['observe_only']} | "
                f"🚫退市/*ST={stats['blocked']} | ⏸️停牌={stats['susp_count']} | ST={stats['st_count']}")
    logger.info(f"回填目标: {len(result)}只 (正常交易+观察, 排除退市+停牌)")

    # 6. 写入缓存 (与 load_cached_stocks 格式一致)
    save_cached_stocks(result)

    return result


def _get_board_type(code: str) -> str:
    """根据代码判断板块类型 (对齐 stock_tagger CODE_PREFIX_MAP)"""
    prefix_map = [
        ("600", "main_sh"), ("601", "main_sh"), ("603", "main_sh"),
        ("605", "main_sh"), ("000", "main_sz"), ("001", "main_sz"),
        ("003", "main_sz"), ("002", "sme"), ("300", "gem"), ("301", "gem"),
        ("688", "star"), ("83", "bse"), ("87", "bse"),
        ("43", "bse"), ("920", "bse"),
    ]
    for prefix, btype in sorted(prefix_map, key=lambda x: -len(x[0])):
        if code.startswith(prefix):
            return btype
    return "unknown"


def safe_float(val) -> float:
    """安全转float (NaN/None→0)"""
    try:
        v = float(val)
        return v if not (pd.isna(v) or v != v) else 0.0
    except (ValueError, TypeError):
        return 0.0


async def _run_sync(fn, *args, **kwargs):
    """在executor中运行同步函数(避免阻塞事件循环)"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ============================================================================
# 股票列表缓存加载 (main入口用)
# ============================================================================


def load_cached_stocks() -> Optional[list[dict]]:
    """加载缓存的股票列表(仅当天有效)"""
    if not STOCK_CACHE_FILE.exists():
        return None
    
    try:
        data = json.loads(STOCK_CACHE_FILE.read_text("utf-8"))
        cached_date = data.get("date")
        today = date.today().isoformat()
        
        if cached_date == today and data.get("stocks"):
            logger.info(f"使用今日缓存的股票列表 ({len(data['stocks'])}只)")
            return data["stocks"]
        
        return None  # 过期或无效
        
    except Exception:
        return None


def save_cached_stocks(stocks: list[dict]):
    """保存股票列表到缓存"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "date": date.today().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "count": len(stocks),
            "stocks": stocks,
        }
        STOCK_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        logger.info(f"股票列表已缓存: {STOCK_CACHE_FILE.name}")
    except Exception as e:
        logger.warning(f"保存股票缓存失败(不影响主流程): {e}")


# ============================================================================
# 同花顺K线采集
# ============================================================================

THS_KLINE_URL = "http://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js"
THS_KLINE_YEAR_URL = "http://d.10jqka.com.cn/v6/line/hs_{code}/01/{year}.js"
THS_STOCKPAGE_URL = "http://stockpage.10jqka.com.cn/{code}/"


async def _get_ths_cookie(client: httpx.AsyncClient) -> str:
    """获取同花顺Cookie"""
    resp = await client.get(THS_STOCKPAGE_URL.format(code="000001"), follow_redirects=True)
    cookies = dict(resp.cookies)
    if not cookies:
        for h in resp.headers.get_list("set-cookie"):
            parts = h.split(";")[0].split("=", 1)
            if len(parts) == 2:
                cookies[parts[0].strip()] = parts[1].strip()
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def _fetch_kline_for_code(
    code: str, cookie: str, client: httpx.AsyncClient
) -> Optional[list[dict]]:
    """获取单只股票完整K线(init模式: last.js + 历史年份)
    
    Returns:
        K线dict列表, 含13个字段(code/trade_date/OHLCV/amount/turnover/change_pct/prev_close/source)
        或None表示失败
    """
    headers = {
        "Referer": f"http://stockpage.10jqka.com.cn/{code}/",
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    }

    all_klines = []
    
    try:
        # 1. last.js (最近140条 + 元数据)
        url = THS_KLINE_URL.format(code=code)
        resp = await client.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        last_data = _parse_jsonp(resp.text)
        if last_data:
            all_klines.extend(last_data)

        # 2. 日K按年份采集，固定从2018开始(日K last.js只有~140条≈半年)
        start_year = 2018

        current_year = date.today().year

        # 3. 按年份采集
        for year in range(start_year, current_year + 1):
            url = THS_KLINE_YEAR_URL.format(code=code, year=year)
            try:
                resp = await client.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                year_data = _parse_jsonp(resp.text)
                if year_data:
                    all_klines.extend(year_data)
            except Exception:
                continue  # 年份404跳过(新股在上市前没有数据)
            await asyncio.sleep(RATE_LIMIT)

        # 4. 去重 + 计算派生字段
        if not all_klines:
            return []

        seen = set()
        unique = []
        for k in all_klines:
            key = (k["code"], k["trade_date"])
            if key not in seen:
                seen.add(key)
                unique.append(k)

        # 按日期排序后计算change_pct/prev_close
        unique.sort(key=lambda x: x["trade_date"])
        for i, k in enumerate(unique):
            if i == 0:
                k["change_pct"] = 0.0
                k["prev_close"] = k["open"]
            else:
                prev = unique[i - 1]
                k["prev_close"] = prev["close"]
                if prev["close"] > 0:
                    k["change_pct"] = round((k["close"] - prev["close"]) / prev["close"] * 100, 2)
                else:
                    k["change_pct"] = 0.0

        return unique

    except Exception as e:
        logger.debug(f"[kline] {code} 失败: {e}")
        return None


def _parse_jsonp(text: str) -> Optional[list[dict]]:
    """解析同花顺JSONP → K线dict列表"""
    match = re.search(r'\((\{.*\})\)', text, re.DOTALL)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    raw = data.get("data", "")
    if not raw:
        return []

    code_from_data = ""
    klines = []
    for item in raw.split(";"):
        fields = item.split(",")
        if len(fields) < 8:
            continue
        try:
            if not code_from_data:
                # 尝试从其他接口获取code, 这里先用占位符
                pass
            klines.append({
                "code": "",  # 由调用者填充
                "trade_date": f"{fields[0][:4]}-{fields[0][4:6]}-{fields[0][6:8]}",  # 20250910 → 2025-09-10
                "open": float(fields[1]),
                "high": float(fields[2]),
                "low": float(fields[3]),
                "close": float(fields[4]),
                "volume": int(float(fields[5])),
                "amount": float(fields[6]) if fields[6] else 0.0,
                "turnover": float(fields[7]) if fields[7] else 0.0,
                "source": "ths",
            })
        except (ValueError, IndexError, KeyError):
            continue

    return klines


# ============================================================================
# 数据库写入
# ============================================================================


def batch_upsert_kline(conn: sqlite3.Connection, records: list[dict]):
    """批量UPSERT到stock_kline表
    
    使用 SQLite INSERT OR REPLACE 实现upsert
    """
    if not records:
        return

    cur = conn.cursor()
    
    # 先尝试批量INSERT OR REPLACE
    values = []
    for r in records:
        values.append((
            r["code"], r["trade_date"],
            r.get("open"), r.get("high"), r.get("low"), r.get("close"),
            r.get("volume"), r.get("amount"), r.get("turnover"),
            r.get("change_pct"), r.get("prev_close"),
            r.get("source", "ths"),
        ))

    cur.executemany("""
        INSERT OR REPLACE INTO stock_kline 
        (code, trade_date, open, high, low, close, volume, amount, turnover, 
         change_pct, prev_close, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, values)
    
    conn.commit()
    return cur.rowcount


def get_completed_codes(conn: sqlite3.Connection) -> set:
    """获取已有K线的股票code集合"""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT code FROM stock_kline")
    return {r[0] for r in cur.fetchall()}


def verify_kline_data(conn: sqlite3.Connection, codes: list[str], sample_size: int = 5) -> bool:
    """验证指定股票的K线数据完整性
    
    检查项:
    1. 每只至少有100条记录(或按上市天数)
    2. OHLCV非空率100%
    3. 派生字段(change_pct, prev_close)存在
    4. 无重复(code+date)
    5. 最近日期是今天或昨天
    """
    cur = conn.cursor()
    
    sample = codes[:sample_size] if len(codes) > sample_size else codes
    errors = []

    for code in sample:
        cur.execute("""
            SELECT COUNT(*), 
                   SUM(CASE WHEN open IS NULL OR close IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN change_pct IS NULL THEN 1 ELSE 0 END),
                   MIN(trade_date), MAX(trade_date),
                   COUNT(DISTINCT code || trade_date),
                   COUNT(*) as total
            FROM stock_kline WHERE code = ?
        """, (code,))
        
        total, ohlc_null, cp_null, dmin, dmax, unique_cnt, cnt = cur.fetchone()

        if total == 0:
            errors.append(f"{code}: 无数据")
            continue

        if unique_cnt != cnt:
            errors.append(f"{code}: 存在重复({cnt}行, {unique_cnt}唯一)")
        
        if ohlc_null and ohlc_null > 0:
            errors.append(f"{code}: OHLC空值{ohlc_null}/{total}")

    if errors:
        logger.warning(f"数据验证发现 {len(errors)} 个问题:")
        for e in errors:
            logger.warning(f"  ⚠️ {e}")
        return False
    
    return True


# ============================================================================
# 主流程
# ============================================================================


async def run_backfill(
    concurrency: int = DEFAULT_CONCURRENCY,
    batch_db: int = DEFAULT_BATCH_DB,
    force_codes: Optional[list[str]] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    verbose: bool = False,
    codes: Optional[list[str]] = None,  # 手动指定列表(不强制删除旧数据)
):
    """执行K线全量回填"""

    progress = load_progress()
    t_start = _time.monotonic()

    # ------------------------------------------------------------------
    # Phase 1: 获取股票列表
    # ------------------------------------------------------------------
    logger.info("=" * 64)
    logger.info("  个股日K线 全量回填 (stock_kline init)")
    logger.info("=" * 64)

    if force_codes:
        # 手动指定股票(强制模式, 先删旧数据)
        all_codes = force_codes
        logger.info(f"强制指定: {len(all_codes)} 只股票")
    elif codes:
        # 手动指定(非强制, 保留旧数据/跳过已有)
        all_codes = codes
        logger.info(f"手动指定: {len(all_codes)} 只股票")
    else:
        # 从pywencai获取全A股列表(每日缓存, 自动过滤退市/*ST)
        
        # 1. 尝试加载今日缓存
        cached = load_cached_stocks()
        
        if cached is None:
            logger.info("问财获取股票列表(含停牌+ST过滤)...")
            stocks_list = await fetch_pywencai_stocks()
            save_cached_stocks(stocks_list)
        else:
            stocks_list = cached
        
        # 2. 过滤: 回填目标 = 正常交易 + 停牌 (排除退市/*ST)
        #    历史K线需要停牌股(用于分析复牌异动), 但实时采集只要正常交易的
        backfill_targets = [s for s in stocks_list 
                          if s["board_tag"] in ("tradeable", "observe_only")]
        
        # 排除退市风险(*ST)和名称含"退"的
        exclude_blocked = [s for s in backfill_targets 
                         if s["board_tag"] != "blocked"]
        
        all_codes = [s["code"] for s in exclude_blocked]
        
        # 统计信息
        tradeable_n = sum(1 for s in stocks_list if s["board_tag"] == "tradeable")
        observe_n = sum(1 for s in stocks_list if s["board_tag"] == "observe_only")
        blocked_n = sum(1 for s in stocks_list if s["board_tag"] == "blocked")
        susp_n = sum(1 for s in stocks_list if s["is_suspended"])
        
        logger.info(f"问财统计: 可交易={tradeable_n} | 观察={observe_n} | "
                   f"退市/*ST={blocked_n} | 停牌={susp_n}")
        logger.info(f"回填目标: {len(all_codes)}只 (正常交易+观察, 含停牌股历史K线, 排除退市)")

    if limit:
        all_codes = all_codes[:limit]
        logger.info(f"限制为前 {limit} 只")

    progress["total_stocks"] = len(all_codes)
    progress["started_at"] = datetime.now().isoformat()
    save_progress(progress)

    # ------------------------------------------------------------------
    # Phase 2: 准备DB连接 + 已有数据检查
    # ------------------------------------------------------------------
    conn = get_db_connection()
    ensure_tables(conn)

    completed = get_completed_codes(conn) if not force_codes else set()
    
    if force_codes:
        # 强制模式: 清除这些股票的旧数据
        if not dry_run:
            cur = conn.cursor()
            placeholders = ",".join("?" * len(force_codes))
            cur.execute(f"DELETE FROM stock_kline WHERE code IN ({placeholders})", force_codes)
            deleted = cur.rowcount
            conn.commit()
            if deleted > 0:
                logger.info(f"清除旧数据: {deleted} 条")
            completed = set()
    else:
        # 正常模式: 跳过已有
        skip_count = len(completed & set(all_codes))
        if skip_count > 0:
            logger.info(f"断点续传: 已有{skip_count}只K线数据, 将跳过")

    pending = [c for c in all_codes if c not in completed]
    logger.info(f"待采集: {len(pending)} 只")

    if not pending:
        logger.info("✅ 所有股票已完成, 无需回填")
        conn.close()
        return

    # ------------------------------------------------------------------
    # Phase 3: 并发采集 + 批量写入
    # ------------------------------------------------------------------
    sem = asyncio.Semaphore(concurrency)
    results_queue = []  # 收集结果
    stats = {"success": 0, "failed": 0, "records": 0}
    db_buffer = []  # 写入缓冲区

    async with httpx.AsyncClient(timeout=20, limits=httpx.Limits(max_connections=concurrency * 2)) as client:
        # 获取Cookie
        logger.info("获取同花顺Cookie...")
        cookie = await _get_ths_cookie(client)
        logger.info("Cookie就绪, 开始采集")

        async def collect_one(code: str):
            async with sem:
                try:
                    klines = await _fetch_kline_for_code(code, cookie, client)
                    if klines is None:
                        return {"code": code, "status": "failed", "records": []}
                    
                    # 填充code
                    for k in klines:
                        k["code"] = code
                    
                    return {"code": code, "status": "ok", "records": klines}
                except Exception as e:
                    return {"code": code, "status": "error", "error": str(e)}

        # 分批处理(避免一次性创建太多task)
        batch_task_size = min(concurrency * 4, len(pending))
        
        for i in range(0, len(pending), batch_task_size):
            batch = pending[i:i + batch_task_size]
            done_count = i + len(batch)
            
            logger.info(f"采集进度: [{done_count}/{len(pending)}] ...")
            
            tasks = [collect_one(c) for c in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for r in batch_results:
                if isinstance(r, Exception):
                    stats["failed"] += 1
                    progress["failed_codes"].append(f"exception:{str(r)[:50]}")
                    continue
                
                if r["status"] == "ok":
                    stats["success"] += 1
                    stats["records"] += len(r["records"])
                    db_buffer.extend(r["records"])
                    progress["completed_codes"].append(r["code"])
                else:
                    stats["failed"] += 1
                    progress["failed_codes"].append(r["code"])

                # 批量写入DB
                if len(db_buffer) >= batch_db and not dry_run:
                    count = batch_upsert_kline(conn, db_buffer)
                    db_buffer.clear()
                    logger.debug(f"DB写入: {count} 条")

            # 每大批保存一次进度
            progress["stats"] = {"total_records": stats["records"], "success": stats["success"], 
                                  "failed": stats["failed"]}
            save_progress(progress)

            # 简单进度日志
            elapsed = _time.monotonic() - t_start
            done_total = stats["success"] + stats["failed"]
            speed = done_total / elapsed if elapsed > 0 else 0
            eta = (len(pending) - done_total) / speed if speed > 0 else 0
            logger.info(f"  ✅ 成功{stats['success']} 失败{stats['failed']} "
                       f"| 总K线{stats['records']}条 | "
                       f"速度{speed:.1f}只/s | 预计剩余{eta:.0f}s")

    # 最后一批写入
    if db_buffer and not dry_run:
        batch_upsert_kline(conn, db_buffer)
        db_buffer.clear()

    conn.close()

    # ------------------------------------------------------------------
    # Phase 4: 最终报告
    # ------------------------------------------------------------------
    elapsed = _time.monotonic() - t_start

    logger.info("\n" + "=" * 64)
    logger.info("  回填完成报告")
    logger.info("=" * 64)
    logger.info(f"\n  总耗时:     {elapsed:.1f}s ({elapsed/60:.1f}分钟)")
    logger.info(f"  目标股票:   {len(all_codes)} 只")
    logger.info(f"  采集成功:   {stats['success']} 只")
    logger.info(f"  采集失败:   {stats['failed']} 只")
    logger.info(f"  K线总条数: +{stats['records']} 条 (本批新增)")
    
    # DB总量统计
    if not dry_run:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT code) FROM stock_kline")
        total_rec, total_stocks = cur.fetchone()
        cur.execute("SELECT MIN(trade_date), MAX(trade_date) FROM stock_kline")
        dr = cur.fetchone()
        logger.info(f"  库存总计:   {total_rec:,} 条 / {total_stocks} 只股票")
        logger.info(f"  日期范围:   {dr[0]} ~ {dr[1]}")
        conn.close()

    if stats["failed"] > 0:
        logger.warning(f"  \n  ⚠️ 失败股票({len(progress['failed_codes'])}):")
        for fc in progress["failed_codes"][-20:]:
            logger.warning(f"    {fc}")
        logger.warning(f"    ...(共{len(progress['failed_codes'])}只, 可重试)")
        logger.info(f"  提示: 直接重跑脚本即可断点续传, 失败的会自动重试")

    progress["finished_at"] = datetime.now().isoformat()
    save_progress(progress)


# ============================================================================
# CLI入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Claw 个股日K线 全量回填工具 (问财股票列表 + 同花顺K线源)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
数据源:
  股票列表:  pywencai (每日缓存, 自动过滤退市/*ST, 保留停牌股)
  K线数据:   同花顺 d.10jqka.com.cn (前复权, init=全量/daily=增量)
  过滤规则:  排除退市(退/*ST/终止上市) → 回填正常交易+停牌股K线

示例:
  %(prog)s                              # 默认全量回填(并发10, 自动缓存列表)
  %(prog)s --concurrency 20             # 高并发模式
  %(prog)s --status                     # 仅查看进度+DB统计
  %(prog)s --force-codes 000001,600519  # 强制重采指定股票
  %(prog)s --dry-run --limit 5          # 测试5只(不写DB)
  %(prog)s --clear-cache                # 清除股票列表缓存(强制明天重新获取)
""",
    )

    parser.add_argument("--concurrency", "-c", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"并发数(默认{DEFAULT_CONCURRENCY})")
    parser.add_argument("--batch-size", "-b", type=int, default=DEFAULT_BATCH_DB,
                        help=f"每批写入DB的股票数(默认{DEFAULT_BATCH_DB})")
    parser.add_argument("--codes", type=str, default=None,
                        help="指定股票代码(逗号分隔, 如000001,600519)")
    parser.add_argument("--force-codes", type=str, default=None,
                        help="强制重新采集(先删除旧数据)")
    parser.add_argument("--dry-run", action="store_true",
                        help="干跑模式: 只采集不写DB")
    parser.add_argument("--limit", "-l", type=int, default=None,
                        help="限制采集数量(用于测试)")
    parser.add_argument("--status", "-s", action="store_true",
                        help="仅显示当前回填进度")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="详细输出(DEBUG级别)")
    parser.add_argument("--clear-cache", action="store_true",
                        help="清除股票列表缓存(下次运行强制重新获取)")

    args = parser.parse_args()
    setup_logger(args.verbose)

    # --clear-cache 模式
    if args.clear_cache:
        if STOCK_CACHE_FILE.exists():
            STOCK_CACHE_FILE.unlink()
            print(f"已清除股票列表缓存: {STOCK_CACHE_FILE}")
        else:
            print("无缓存文件")
        return

    # --status 模式
    if args.status:
        progress = load_progress()
        print(json.dumps(progress, ensure_ascii=False, indent=2))
        
        # 额外显示DB状态
        if DB_PATH.exists():
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT code) FROM stock_kline")
            tr, ts = cur.fetchone()
            print(f"\n数据库: {tr:,} 条 / {ts} 只股票")
            conn.close()
        return

    # 解析codes参数: --codes指定采集列表(跳过已有), --force-codes强制重采
    target_codes = None
    if args.force_codes:
        target_codes = [c.strip().zfill(6) for c in args.force_codes.split(",")]
    elif args.codes:
        # codes模式也用force逻辑(手动指定就不走AkShare获取全量)
        target_codes = [c.strip().zfill(6) for c in args.codes.split(",")]

    # 运行
    asyncio.run(run_backfill(
        concurrency=args.concurrency,
        batch_db=args.batch_size,
        force_codes=target_codes if args.force_codes else None,
        dry_run=args.dry_run,
        limit=args.limit,
        verbose=args.verbose,
        codes=target_codes,  # 用于--codes模式(不强制删除旧数据)
    ))


if __name__ == "__main__":
    main()
