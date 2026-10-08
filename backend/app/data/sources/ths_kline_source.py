"""同花顺日K线数据源 — d.10jqka.com.cn

能力:
- 个股日K线(前复权), 11字段: 日期/OHLCV/成交额/换手率/涨跌幅/昨收
- last.js约140条/只, 按年份增量采集(full history)
- 盘后15:10一次, 5000只并发5≈5分钟

采集模式:
- init: 全量采集(按年份分批)
- daily: 盘后增量(仅当天)
- repair: 每月修正漂移(重新采集近30天)

注意:
- URL路径参数: 01=日K 11=周K 21=月K (必须用01!)
- [9]/[10]盘后股数/元，2026-10-02四板样本经官方与新浪交叉核验；[8]未知
- 尾字段保留为研究材料，不认证全市场/ETF覆盖、历史可用钟或成交资格
- 前复权[5]成交量单位=股(非手)
- 必须先访问stockpage获取Cookie
"""

import asyncio
import json
import re
import time as _time
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.data.source_capture import observe_response
from app.data.after_hours import ths_tail
from app.data.sources.after_hours_source import local_now


class ThsKlineSource(DataSourceBase):
    """同花顺日K线数据源"""

    source_name = "ths_kline"
    rate_limit = 0.2  # 限流

    def __init__(self, *, response_capture=None):
        super().__init__()
        self._response_capture = response_capture  # Explicit research only; no default disk writes.

    KLINE_URL = "http://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js"
    KLINE_YEAR_URL = "http://d.10jqka.com.cn/v6/line/hs_{code}/01/{year}.js"
    STOCKPAGE_URL = "http://stockpage.10jqka.com.cn/{code}/"

    # Cookie缓存
    _cookie_cache: dict[str, str] = {}
    _cookie_expire: float = 0

    def _bust_cache_url(self, url: str) -> str:
        """添加缓存破坏参数，绕过同花顺CDN旧缓存"""
        import time as _t
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}_={int(_t.time())}"

    async def _get_cookie(self) -> str:
        """获取同花顺Cookie(缓存7天)"""
        now = _time.monotonic()
        if self._cookie_cache and now < self._cookie_expire:
            return "; ".join(f"{k}={v}" for k, v in self._cookie_cache.items())

        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            # 访问stockpage获取Cookie
            resp = await client.get(self.STOCKPAGE_URL.format(code="000001"))
            cookies = dict(resp.cookies)
            if not cookies:
                # 从headers提取Set-Cookie
                for header_val in resp.headers.get_list("set-cookie"):
                    parts = header_val.split(";")[0].split("=", 1)
                    if len(parts) == 2:
                        cookies[parts[0].strip()] = parts[1].strip()

            if cookies:
                self._cookie_cache = cookies
                self._cookie_expire = now + 7 * 86400  # 7天过期
                logger.debug(f"[ths_kline] Cookie更新: {list(cookies.keys())}")

            return "; ".join(f"{k}={v}" for k, v in cookies.items())

    async def collect_after_hours(self, code: str, *, trade_date: date) -> Optional[dict]:
        """Explicit-date auxiliary observation; never select metadata.today or ffill."""
        if type(trade_date) is not date or trade_date > local_now().date():
            raise ValueError("explicit non-future trade date required")
        rows = await self._fetch_kline(code, url_suffix="last.js", max_attempts=1)
        target = trade_date.isoformat()
        return next((row for row in rows or [] if row["trade_date"] == target), None)

    async def _fetch_kline(
        self,
        code: str,
        url_suffix: str = "last.js",
        max_attempts: int = 2,
    ) -> Optional[list[dict]]:
        """获取K线数据(带CDN缓存重试)

        同花顺CDN节点间缓存不一致，需要多次请求取最新结果

        Args:
            code: 6位股票代码
            url_suffix: "last.js" or "{year}.js"

        Returns:
            解析后的K线数据列表
        """
        best_result = None
        best_count = 0

        attempts = max(int(max_attempts), 1)
        for attempt in range(attempts):
            cookie = await self._get_cookie()

            if url_suffix == "last.js":
                url = self.KLINE_URL.format(code=code)
            else:
                year = url_suffix.rstrip(".js")
                url = self.KLINE_YEAR_URL.format(code=code, year=year)

            headers = {
                "Referer": f"http://stockpage.10jqka.com.cn/{code}/",
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }

            async with httpx.AsyncClient(timeout=15) as client:
                try:
                    bust_url = self._bust_cache_url(url)
                    resp = await client.get(bust_url, headers=headers)
                    resp.raise_for_status()
                    received_at = local_now()
                    if self._response_capture is not None:
                        observe_response(self._response_capture, source="ths_kline",
                            request_key=(code, url_suffix, attempt + 1), response=resp,
                            received_at=received_at)
                    text = resp.text
                except Exception as e:
                    logger.warning(f"[ths_kline] {code} 请求失败(第{attempt+1}次): {e}")
                    continue

            # 解析JSONP: quotebridge_xxx({...});
            json_match = re.search(r'\((\{.*\})\)', text, re.DOTALL)
            if not json_match:
                logger.warning(f"[ths_kline] {code} JSONP解析失败")
                continue

            try:
                data = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                logger.warning(f"[ths_kline] {code} JSON解析失败")
                continue

            # 提取K线数据
            raw_data = data.get("data", "")
            if not raw_data:
                if best_result is None:
                    best_result = []
                continue

            # 数据格式: "日期,开,高,低,收,量,额,换手率;日期,开,高,低,收,量,额,换手率;..."
            klines = []
            for item in raw_data.split(";"):
                fields = item.split(",")
                if len(fields) < 8:
                    continue
                try:
                    klines.append({
                        "code": code,
                        "trade_date": f"{fields[0][:4]}-{fields[0][4:6]}-{fields[0][6:8]}",
                        "open": float(fields[1]),
                        "high": float(fields[2]),
                        "low": float(fields[3]),
                        "close": float(fields[4]),
                        "volume": int(float(fields[5])),
                        "amount": float(fields[6]),
                        "turnover": float(fields[7]) if fields[7] else None,
                        "source": "ths",
                        "after_hours": ths_tail(fields),
                        "received_at": received_at,
                        "source_published_at": None,
                    })
                except (ValueError, OverflowError, IndexError):
                    continue

            # 年度文件在收盘后可能命中不同 CDN 版本。条数相同时保留后一次
            # 带随机 cache-buster 的快照，避免首个旧节点回包直接污染当日K线。
            if len(klines) >= best_count:
                best_result = klines
                best_count = len(klines)

            if attempt + 1 < attempts:
                await asyncio.sleep(0.3)

        return best_result

    def _calc_derived(self, klines: list[dict]) -> list[dict]:
        """计算派生字段: change_pct, prev_close"""
        # 按日期排序
        klines.sort(key=lambda x: x["trade_date"])

        for i, k in enumerate(klines):
            if i == 0:
                # Unknown is not a flat day; opening price is not previous close.
                k["change_pct"] = None
                k["prev_close"] = None
            else:
                prev = klines[i - 1]
                k["prev_close"] = prev["close"]
                if prev["close"] > 0:
                    k["change_pct"] = round((k["close"] - prev["close"]) / prev["close"] * 100, 2)
                else:
                    k["change_pct"] = None

        return klines

    async def collect_init(self, code: str, *, coverage: dict | None = None) -> list[dict]:
        """全量采集: 先last.js获取元数据, 再按年份采集

        Args:
            code: 6位股票代码

        Returns:
            K线数据列表
        """
        all_klines = []
        if coverage is not None:
            coverage.update(last_rows=0, year_rows={}, unavailable_years=[], certified=False)

        # 1. 先取last.js(最近140条+元数据)
        last_data = await self._fetch_kline(code, "last.js")
        if coverage is not None:
            coverage["last_rows"] = len(last_data or [])
        if not last_data:
            return []

        all_klines.extend(last_data)

        # 2. 日K按年份采集，固定从2018开始(日K last.js只有~140条≈半年)
        start_year = 2018

        current_year = local_now().year

        # 3. 按年份采集
        for year in range(start_year, current_year + 1):
            year_data = await self._fetch_kline(code, f"{year}.js")
            if coverage is not None:
                coverage["year_rows"][str(year)] = len(year_data or [])
                if not year_data:
                    coverage["unavailable_years"].append(year)
            if year_data:
                all_klines.extend(year_data)
            await asyncio.sleep(self.rate_limit)

        # 4. 去重(同一天可能last.js和year.js都有)
        seen = set()
        unique = []
        for k in all_klines:
            key = (k["code"], k["trade_date"])
            if key not in seen:
                seen.add(key)
                unique.append(k)

        return self._calc_derived(unique)

    async def collect_daily(self, code: str) -> list[dict]:
        """盘后增量: 优先取当年年份文件, fallback到last.js, 只保留最新一天

        Args:
            code: 6位股票代码

        Returns:
            最新的K线数据(0-1条) - 只返回今天的数据，如果没有则返回空
        """
        today_str = date.today().isoformat()
        current_year = date.today().year

        def select_today(rows: list[dict] | None) -> list[dict]:
            """先在完整序列上计算昨收/涨跌幅，再截取当天。

            旧实现先截成单根K线再调用 ``_calc_derived``，会把当天昨收误设为
            开盘价并令 change_pct 恒为 0，进而污染回测和市场基准。
            """
            if not rows:
                return []
            derived = self._calc_derived(list(rows))
            return [kline for kline in derived if kline["trade_date"] == today_str]

        # 1. 优先用当年年份文件 — 数据更新更快(last.js可能滞后)
        # 盘后首轮以吞吐为主；CDN多节点校验由21:20定向回补任务承担。
        year_data = await self._fetch_kline(code, f"{current_year}.js", max_attempts=1)
        latest = select_today(year_data)
        if latest:
            return latest

        # 2. fallback到last.js
        all_data = await self._fetch_kline(code, "last.js", max_attempts=1)
        latest = select_today(all_data)
        if latest:
            return latest

        # 3. 今天数据在两个接口都不存在(非交易日或数据源未更新)
        logger.debug(f"[ths_kline] {code} 今日({today_str})数据未生成，跳过")
        return []

    async def collect_repair(
        self,
        code: str,
        lookback_days: int = 30,
        as_of: date | None = None,
        fetch_attempts: int = 2,
    ) -> list[dict]:
        """回补最近一段时间的K线，覆盖停机和数据源延迟造成的历史缺口。"""
        repair_end = as_of or date.today()
        repair_start = repair_end - timedelta(days=max(int(lookback_days), 1))
        all_klines = []

        for year in range(repair_start.year, repair_end.year + 1):
            year_data = await self._fetch_kline(
                code,
                f"{year}.js",
                max_attempts=fetch_attempts,
            )
            if year_data:
                all_klines.extend(year_data)
            await asyncio.sleep(self.rate_limit)

        seen = set()
        unique = []
        for kline in all_klines:
            key = (kline["code"], kline["trade_date"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(kline)

        derived = self._calc_derived(unique)
        start_str = repair_start.isoformat()
        end_str = repair_end.isoformat()
        return [
            kline
            for kline in derived
            if start_str <= kline["trade_date"] <= end_str
        ]

    async def collect(self, session: AsyncSession, **kwargs) -> list[dict]:
        """通用采集入口

        kwargs:
            codes: list[str] 股票代码列表
            mode: str "init"/"daily"/"repair"
            repair_days: int repair模式回补自然日数，默认30
        """
        codes = kwargs.get("codes", [])
        mode = kwargs.get("mode", "daily")
        repair_days = max(int(kwargs.get("repair_days", 30)), 1)

        all_klines = []
        for code in codes:
            if mode == "init":
                klines = await self.collect_init(code)
            elif mode == "daily":
                klines = await self.collect_daily(code)
            else:  # repair
                klines = await self.collect_repair(code, lookback_days=repair_days)

            if klines:
                all_klines.extend(klines)

            # 并发控制
            await asyncio.sleep(self.rate_limit)

        return all_klines

    async def health_check(self, session: AsyncSession) -> bool:
        """健康检查 — 获取平安银行K线"""
        try:
            klines = await self._fetch_kline("000001", "last.js")
            return klines is not None and len(klines) > 0
        except Exception:
            return False
