"""东财数据源 — 短线专用"""

from datetime import date, datetime
from typing import Any, Optional
import asyncio
import time as _time
import math
import pandas as pd
import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.data.sources.base import DataSourceBase
from app.data.source_capture import observe_response
from app.data.fund_flow_clock import eastmoney_quote_clock, verified_fund_clocks
from app.core.data_quality import data_quality_guard

import akshare as ak


class EastMoneySource(DataSourceBase):
    """东财数据源 (via AkShare)
    
    核心能力: 涨停/跌停/炸板/强势股池 + 龙虎榜 + 大盘资金流 (稳定接口!)
    
    API名变更记录 (AkShare版本更新):
      旧: stock_em_zt_pool → 新: stock_zt_pool_em
      旧: stock_em_dt_pool → 新: stock_zt_pool_dtgc_em
      旧: stock_em_zb_pool → 新: stock_zt_pool_zbgc_em
      旧: stock_em_qgqp   → 新: stock_zt_pool_strong_em (强势股)
    """

    source_name = "eastmoney"
    rate_limit = 0.1  # 东财接口稳定，缩短限流到0.1s
    INDIVIDUAL_FUND_FLOW_URL = "https://push2.eastmoney.com/api/qt/clist/get"

    def __init__(self, *, response_capture=None):
        super().__init__()
        self._response_capture = response_capture  # Explicit research only; no default disk writes.

    # ===== 涨停/跌停/炸板 =====

    async def get_limit_up_pool(self, trade_date: str = "") -> pd.DataFrame:
        """涨停池
        
        返回列: 代码, 名称, 涨停价, 最新价, 涨跌幅, 成交额, 流通市值,
                封板资金, 首次封板时间, 最后封板时间, 炸板次数, 连板数, 涨停统计
        """
        if not trade_date:
            trade_date = date.today().strftime("%Y%m%d")
        return await self._safe_call(
            "stock_zt_pool_em",
            ak.stock_zt_pool_em,
            date=trade_date,
        )

    async def get_limit_down_pool(self, trade_date: str = "") -> pd.DataFrame:
        """跌停池
        
        返回列: 代码, 名称, 跌停价, ...
        注意: 只能获取最近30个交易日数据
        """
        if not trade_date:
            trade_date = date.today().strftime("%Y%m%d")
        return await self._safe_call(
            "stock_zt_pool_dtgc_em",
            ak.stock_zt_pool_dtgc_em,
            date=trade_date,
        )

    async def get_broken_limit_pool(self, trade_date: str = "") -> pd.DataFrame:
        """炸板池
        
        返回列: 代码, 名称, 涨停价, 首次封板时间, 最后封板时间, ...
        注意: 只能获取最近30个交易日数据
        """
        if not trade_date:
            trade_date = date.today().strftime("%Y%m%d")
        return await self._safe_call(
            "stock_zt_pool_zbgc_em",
            ak.stock_zt_pool_zbgc_em,
            date=trade_date,
        )

    async def get_strong_stock_pool(self, indicator: str = "强势股") -> pd.DataFrame:
        """强势股池"""
        return await self._safe_call(
            "stock_zt_pool_strong_em",
            ak.stock_zt_pool_strong_em,
        )

    # ===== 龙虎榜 =====

    async def get_dragon_tiger_list(self, start_date: str = "",
                                     end_date: str = "") -> pd.DataFrame:
        """龙虎榜"""
        return await self._safe_call(
            "stock_lhb_detail_em",
            ak.stock_lhb_detail_em,
            start_date=start_date, end_date=end_date,
        )

    async def get_dragon_tiger_stock_detail(
        self,
        symbol: str,
        trade_date: str,
        flag: str = "买入",
    ) -> pd.DataFrame:
        """个股龙虎榜席位明细"""
        return await self._safe_call(
            "stock_lhb_stock_detail_em",
            ak.stock_lhb_stock_detail_em,
            symbol=symbol,
            date=trade_date,
            flag=flag,
        )

    # ===== 大盘资金流 =====

    async def get_market_fund_flow(self) -> pd.DataFrame:
        """大盘资金流
        
        返回列: 日期, 上证-收盘价, 上证-涨跌幅, 深证-收盘价, 深证-涨跌幅,
                主力净流入-净额, 主力净流入-净占比,
                超大单净流入-净额, 超大单净流入-净占比,
                大单净流入-净额, 大单净流入-净占比,
                中单净流入-净额, 中单净流入-净占比,
                小单净流入-净额, 小单净流入-净占比
        """
        return await self._safe_call(
            "stock_market_fund_flow",
            ak.stock_market_fund_flow,
        )

    async def get_individual_fund_flow(self, indicator: str = "今日") -> pd.DataFrame:
        """东方财富个股主力资金流排行

        与东财 App「主力资金」口径保持一致，字段包含:
        代码, 名称, 最新价, 今日涨跌幅,
        今日主力净流入-净额, 今日主力净流入-净占比,
        今日超大单净流入-净额, 今日超大单净流入-净占比,
        今日大单净流入-净额, 今日大单净流入-净占比,
        今日中单净流入-净额, 今日中单净流入-净占比,
        今日小单净流入-净额, 今日小单净流入-净占比
        """
        indicator_map = {
            "今日": [
                "f62",
                "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
            ],
        }
        if indicator not in indicator_map:
            raise ValueError(f"不支持的资金流周期: {indicator}")

        base_params = {
            # A changing money-flow rank shifts page boundaries during collection.
            # Page by stable stock code, then restore the public money-flow ordering.
            "fid": "f12",
            "po": "1",
            "pz": "100",
            "pn": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "fs": "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2",
            "fields": indicator_map[indicator][1],
        }

        async def fetch_page(client, page, expected_total=None):
            params = {**base_params, "pn": str(page)}
            for attempt in range(self.max_retries):
                await self._rate_limit()
                try:
                    resp = await client.get(self.INDIVIDUAL_FUND_FLOW_URL, params=params)
                    resp.raise_for_status()
                    received_at = datetime.now()
                    observe_response(self._response_capture, source="eastmoney_fund",
                        request_key=(page, attempt + 1), response=resp, received_at=received_at)
                    payload = resp.json()
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if not isinstance(data, dict):
                        raise ValueError("individual_fund_flow: missing data")
                    total = data.get("total")
                    diff = data.get("diff")
                    if (not isinstance(total, int) or isinstance(total, bool)
                            or total <= 0 or not isinstance(diff, list)):
                        raise ValueError("individual_fund_flow: empty or invalid page")
                    if expected_total is not None and total != expected_total:
                        raise ValueError("individual_fund_flow: total changed during pagination")
                    expected_rows = min(100, total - (page - 1) * 100)
                    if len(diff) != expected_rows or not all(isinstance(row, dict) for row in diff):
                        raise ValueError("individual_fund_flow: incomplete page")
                    # Keep each page's receipt clock; later pages/retries cannot
                    # refresh source clocks or earlier pages' receipt timestamps.
                    return total, [{**row, "received_at": received_at} for row in diff]
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    # Retry only transient errors; do not disclose proxy credentials/URLs.
                    retryable = (
                        not isinstance(exc, httpx.HTTPStatusError)
                        or exc.response.status_code in (408, 429, 500, 502, 503, 504)
                    )
                    if not retryable or attempt + 1 >= self.max_retries:
                        raise RuntimeError(
                            f"individual_fund_flow: page {page} failed ({type(exc).__name__})"
                        ) from None
                    await asyncio.sleep(2 ** attempt)

        # Bound the whole round, not each of dozens of pages. Preserve the existing
        # direct route; never silently switch proxy policy when a request fails.
        round_timeout = max(0.01, float(settings.EASTMONEY_FUND_FLOW_ROUND_TIMEOUT_SEC))
        page_timeout = max(0.01, float(settings.EASTMONEY_FUND_FLOW_PAGE_TIMEOUT_SEC))
        try:
            async with asyncio.timeout(round_timeout):
                async with httpx.AsyncClient(
                    timeout=page_timeout, trust_env=False, headers={"User-Agent": "Mozilla/5.0"},
                ) as client:
                    total, rows = await fetch_page(client, 1)
                    # Reuse page 1; sequential pacing avoids an eight-request burst.
                    for page in range(2, (total + 99) // 100 + 1):
                        _, diff = await fetch_page(client, page, total)
                        rows.extend(diff)
        except TimeoutError:
            raise TimeoutError(f"individual_fund_flow: collection exceeded {round_timeout:g} seconds") from None

        # Key-based mapping: JSON key order and extra fields are not a schema.
        field_map = {
            "f12": "代码", "f14": "名称", "f2": "最新价", "f3": "今日涨跌幅",
            "f62": "今日主力净流入-净额", "f184": "今日主力净流入-净占比",
            "f66": "今日超大单净流入-净额", "f69": "今日超大单净流入-净占比",
            "f72": "今日大单净流入-净额", "f75": "今日大单净流入-净占比",
            "f78": "今日中单净流入-净额", "f81": "今日中单净流入-净占比",
            "f84": "今日小单净流入-净额", "f87": "今日小单净流入-净占比",
        }
        temp_df = pd.DataFrame(rows)
        if not set(field_map).issubset(temp_df.columns):
            raise ValueError("individual_fund_flow: missing required fields")
        codes = temp_df["f12"]
        if (not codes.map(lambda code: isinstance(code, str) and len(code) == 6
                          and code.isascii() and code.isdigit()).all()
                or codes.nunique() != total):
            raise ValueError("individual_fund_flow: duplicate or invalid stock codes")
        # Require real finite provider values; never publish missing amounts
        # as fabricated neutral flows. Genuine numeric zero remains valid.
        for field in field_map:
            if field in ("f12", "f14", "f2", "f3"):
                continue
            # pandas/float coerce JSON booleans to 1/0; neither is a measured flow.
            if temp_df[field].map(pd.api.types.is_bool).any():
                raise ValueError(f"individual_fund_flow: invalid numeric field {field}")
            values = pd.to_numeric(temp_df[field], errors="coerce")
            if not values.map(lambda value: pd.notna(value) and math.isfinite(value)).all():
                raise ValueError(f"individual_fund_flow: invalid numeric field {field}")
            temp_df[field] = values
        observed_at = datetime.now()
        source_clocks = [eastmoney_quote_clock(row.get("f124")) for row in rows]
        valid_clocks = [
            verified_fund_clocks(source_at, row["received_at"], observed_at,
                                 observed_at.date(), settings.FUND_FLOW_SOURCE_MAX_AGE_SEC)
            for row, source_at in zip(rows, source_clocks)
        ]
        temp_df["source_quote_at"] = source_clocks
        temp_df = temp_df[list(field_map) + ["source_quote_at", "received_at"]].rename(columns=field_map)
        temp_df = temp_df.loc[[clock is not None for clock in valid_clocks]]
        if temp_df.empty:
            raise ValueError("individual_fund_flow: no same-day fresh source quote clocks (f124)")
        temp_df = temp_df.sort_values(
            "今日主力净流入-净额", ascending=False, kind="stable",
        ).reset_index(drop=True)
        temp_df.insert(0, "序号", range(1, len(temp_df) + 1))
        temp_df.attrs.update({
            "fund_flow_source": "eastmoney",
            "fund_flow_source_version": "individual_fund_flow_v3_f124",
            "fund_flow_clock_basis": "provider_quote_update_f124",
            "fund_flow_observed_at": observed_at,
            "fund_flow_expected_count": total,
            "fund_flow_clock_rejected_count": total - len(temp_df),
        })
        return temp_df

    # ===== 接口实现 =====

    async def collect(self, session: AsyncSession, **kwargs) -> Any:
        """通用采集入口"""
        api = kwargs.get("api", "limit_up")
        today = kwargs.get("date", date.today().strftime("%Y%m%d"))

        if api == "limit_up":
            return await self.get_limit_up_pool(today)
        elif api == "limit_down":
            return await self.get_limit_down_pool(today)
        elif api == "broken_limit":
            return await self.get_broken_limit_pool(today)
        elif api == "strong_pool":
            return await self.get_strong_stock_pool()
        elif api == "dragon_tiger":
            return await self.get_dragon_tiger_list(start_date=today, end_date=today)
        elif api == "dragon_tiger_stock_detail":
            return await self.get_dragon_tiger_stock_detail(
                symbol=kwargs.get("symbol", ""),
                trade_date=today,
                flag=kwargs.get("flag", "买入"),
            )
        elif api == "market_fund_flow":
            return await self.get_market_fund_flow()
        elif api == "individual_fund_flow":
            return await self.get_individual_fund_flow(kwargs.get("indicator", "今日"))
        else:
            return pd.DataFrame()

    async def health_check_endpoints(self, session: AsyncSession) -> dict[str, bool]:
        """按端点记录健康，避免资金流单点故障把涨停池也误报为不可用。"""
        checks = {
            "limit_up_pool": self.get_limit_up_pool,
            "market_fund_flow": self.get_market_fund_flow,
        }
        results: dict[str, bool] = {}
        for api_name, call in checks.items():
            started = _time.monotonic()
            try:
                df = await call()
                ok = isinstance(df, pd.DataFrame) and (
                    api_name == "limit_up_pool" or len(df) > 0
                )
                if not ok:
                    raise RuntimeError("endpoint returned empty or invalid payload")
                await data_quality_guard.record_success(
                    session,
                    self.source_name,
                    api_name,
                    latency_ms=int((_time.monotonic() - started) * 1000),
                    record_count=len(df),
                )
                results[api_name] = True
            except Exception as exc:
                await data_quality_guard.record_failure(
                    session,
                    self.source_name,
                    api_name,
                    str(exc),
                    latency_ms=int((_time.monotonic() - started) * 1000),
                )
                results[api_name] = False
        return results

    async def health_check(self, session: AsyncSession) -> bool:
        endpoint_results = await self.health_check_endpoints(session)
        return any(endpoint_results.values())
