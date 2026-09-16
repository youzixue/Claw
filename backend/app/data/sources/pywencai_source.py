"""pywencai数据源 — 问财(核心映射+选股)

关键: pywencai.get() 必须传 loop=True 才能自动分页获取全量数据!
不传 loop 默认只返回第一页100条, 这就是之前行业/概念数量不全的根因。
全量采集5197条约需45秒(每页100条, 52页自动循环)
"""

import asyncio
import time as _time
from datetime import date
from typing import Any
import pandas as pd
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.core.data_quality import data_quality_guard
from app.config.settings import settings

import pywencai


class PyWencaiSource(DataSourceBase):
    """pywencai数据源 (via 问财)
    
    核心能力: 个股→行业+概念映射(5197只/257行业/389概念) + 
              涨停/跌停/炸板/连板 + 概念成分股 + 自然语言选股
    
    行业分类体系(同花顺三级):
    - 一级31个: 交通运输/传媒/公用事业/医药生物/...
    - 二级约90个(与akshare ths行业一致)
    - 三级257个: 如"医药生物-中药-中药Ⅲ"
    
    概念板块389个, 比akshare(365)多约15个(AIGC/CRO/ChatGPT等)
    """

    source_name = "pywencai"
    rate_limit = 2.0
    _query_lock = asyncio.Lock()
    query_version = "pywencai_loop_retry_v2"

    async def _query(self, api_name: str, query: str, *, loop: bool = True) -> pd.DataFrame:
        """串行调用问财并重试其常见 ``None.get`` 暂态故障。"""
        last_error: Exception | None = None
        # 问财要求登录会话才返回数据；cookie 从 settings（即 `.env`）读取。
        # 仅在配置了非空值时才传 —— 库的 headers() 会把 None 原样当字符串
        # 塞进 `cookie` 头（发出去是字面量 "None"），不传反而更干净。
        extra: dict = {}
        cookie = str(getattr(settings, "PYWENCAI_COOKIE", "") or "").strip()
        if cookie:
            extra["cookie"] = cookie
        for attempt in range(1, max(int(self.max_retries), 1) + 1):
            try:
                async with self._query_lock:
                    result = await self._safe_call(
                        api_name,
                        pywencai.get,
                        query=query,
                        loop=loop,
                        **extra,
                    )
                if result is None:
                    raise AttributeError("pywencai returned None instead of DataFrame")
                if not isinstance(result, pd.DataFrame):
                    raise TypeError(f"pywencai returned {type(result).__name__}, expected DataFrame")
                return result
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[pywencai] {api_name} 第{attempt}/{self.max_retries}次失败: {exc}"
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(min(float(self.rate_limit) * attempt, 5.0))
        raise RuntimeError(
            f"pywencai/{api_name} 连续{self.max_retries}次失败({self.query_version}): {last_error}"
        ) from last_error

    # ===== 个股映射(核心) =====

    async def get_stock_industry_mapping(self) -> pd.DataFrame:
        """获取所有股票的行业+概念映射(全量, loop=True)
        
        返回字段(27个):
        - 股票代码/股票简称/code/最新价/最新涨跌幅/market_code
        - 股票市场类型(全部A股;沪深主板A股;创业板;...)
        - 所属同花顺行业(三级: "医药生物-中药-中药Ⅲ")
        - 所属概念(分号分隔: "机器人概念;无人机;军工;低空经济")
        - 所属概念数量
        - 市盈率(pe)/市净率(pb)/市销率(ps)
        - 股息率(股票获利率)/股息率(近12个月)
        - 基本每股收益/销售毛利率/每股净资产bps
        - 开盘价/最高价/最低价/收盘价/振幅/成交量
        - 最新dde大单净额
        - a股市值(不含限售股)/总股本/总市值
        """
        query = "全部A股 所属同花顺行业 所属概念 涨跌幅 市盈率 市净率 换手率 成交量 成交额 最新dde大单净额 总市值"
        return await self._query("stock_industry_mapping", query, loop=True)

    # ===== 涨停相关 =====

    async def get_limit_up_pool(self) -> pd.DataFrame:
        """涨停池 (含连板数/封板资金/涨停原因)"""
        return await self._query("limit_up_pool", "涨停 连板数", loop=True)

    async def get_limit_down_pool(self) -> pd.DataFrame:
        """跌停池"""
        return await self._query("limit_down_pool", "跌停 连续跌停", loop=True)

    async def get_broken_limit_pool(self) -> pd.DataFrame:
        """炸板池"""
        return await self._query("broken_limit_pool", "炸板", loop=True)

    async def get_consecutive_limit_up(self, min_days: int = 2) -> pd.DataFrame:
        """连板梯队"""
        return await self._query("consecutive_limit_up", f"连续涨停{min_days}天以上", loop=True)

    # ===== 板块成分股 =====

    async def get_concept_cons(self, concept: str) -> pd.DataFrame:
        """概念成分股"""
        return await self._query("concept_cons", f"{concept}概念股", loop=True)

    async def get_industry_cons(self, industry: str) -> pd.DataFrame:
        """行业成分股"""
        return await self._query("industry_cons", f"{industry}成分股", loop=True)

    # ===== 股票状态查询(风控核心) =====

    async def get_st_stocks(self) -> pd.DataFrame:
        """获取ST/*ST股列表"""
        return await self._query("st_stocks", "ST股", loop=True)

    async def get_suspended_stocks(self) -> pd.DataFrame:
        """获取停牌股列表"""
        return await self._query("suspended_stocks", "停牌", loop=True)

    async def get_delisting_risk_stocks(self) -> pd.DataFrame:
        """获取退市风险股列表(*ST股)"""
        return await self._query("delisting_risk_stocks", "*ST股", loop=True)

    async def get_bse_st_stocks(self) -> pd.DataFrame:
        """获取北交所ST股列表"""
        return await self._query("bse_st_stocks", "北交所ST股", loop=True)

    # ===== 自然语言选股 =====

    async def custom_query(self, query: str) -> pd.DataFrame:
        """自定义自然语言查询(全量)"""
        return await self._query("custom_query", query, loop=True)

    # ===== 板块级行情聚合(从个股聚合计算) =====

    async def get_sector_aggregate(self, sector_type: str = "concept") -> pd.DataFrame:
        """从个股数据聚合板块级行情指标
        
        Args:
            sector_type: concept(概念) / industry(行业)
            
        Returns:
            DataFrame含: sector_name, stock_count, avg_change_pct, median_change_pct,
                        total_amount, total_dde, median_pe, median_pb, avg_turnover
        """
        df = await self.get_stock_industry_mapping()
        if df is None or len(df) == 0:
            return pd.DataFrame()
        
        group_col = "所属同花顺行业" if sector_type == "industry" else "所属概念"
        if group_col not in df.columns:
            logger.warning(f"pywencai返回无{group_col}列")
            return pd.DataFrame()
        
        # 涨跌幅列名含日期后缀, 需模糊匹配
        change_col = None
        amount_col = None
        dde_col = None
        pe_col = None
        pb_col = None
        turnover_col = None
        
        for col in df.columns:
            if "最新涨跌幅" in col:
                change_col = col
            elif "成交额" in col and "成交额[20" in col:
                amount_col = col
            elif "最新dde大单净额" in col:
                dde_col = col
            elif "市盈率(pe)" in col or col == "市盈率(pe)":
                pe_col = col
            elif "市净率(pb)" in col or col == "市净率(pb)":
                pb_col = col
            elif "换手率" in col and "换手率[20" not in col:
                turnover_col = col
        
        # 概念板块: 每只股票可能属多个概念(分号分隔), 需展开
        if sector_type == "concept" and "所属概念" in df.columns:
            # 展开概念: "机器人;无人机;军工" → 3行
            df_expanded = df.dropna(subset=["所属概念"]).assign(
                所属概念=df["所属概念"].str.split(";")
            ).explode("所属概念")
            df_expanded["所属概念"] = df_expanded["所属概念"].str.strip()
            df_group = df_expanded
        else:
            df_group = df.dropna(subset=[group_col])
        
        # 数值列转换
        if change_col:
            df_group[change_col] = pd.to_numeric(df_group[change_col], errors="coerce")
        if amount_col:
            df_group[amount_col] = pd.to_numeric(df_group[amount_col], errors="coerce")
        if dde_col:
            df_group[dde_col] = pd.to_numeric(df_group[dde_col], errors="coerce")
        if pe_col:
            df_group[pe_col] = pd.to_numeric(df_group[pe_col], errors="coerce")
        if pb_col:
            df_group[pb_col] = pd.to_numeric(df_group[pb_col], errors="coerce")
        if turnover_col:
            df_group[turnover_col] = pd.to_numeric(df_group[turnover_col], errors="coerce")
        
        # 聚合
        agg_dict = {group_col: "count"}
        if change_col:
            agg_dict[change_col] = ["mean", "median", "max", "min"]
        if amount_col:
            agg_dict[amount_col] = "sum"
        if dde_col:
            agg_dict[dde_col] = "sum"
        if pe_col:
            agg_dict[pe_col] = "median"
        if pb_col:
            agg_dict[pb_col] = "median"
        if turnover_col:
            agg_dict[turnover_col] = "mean"
        
        result = df_group.groupby(group_col).agg(agg_dict).reset_index()
        
        # 展平多级列名
        result.columns = [
            "_".join(col).strip("_") if isinstance(col, tuple) else col
            for col in result.columns
        ]
        
        # 重命名
        rename_map = {group_col: "sector_name"}
        for col in result.columns:
            if "count" in col:
                rename_map[col] = "stock_count"
            elif "mean" in col and "涨跌幅" in col:
                rename_map[col] = "avg_change_pct"
            elif "median" in col and "涨跌幅" in col:
                rename_map[col] = "median_change_pct"
            elif "max" in col and "涨跌幅" in col:
                rename_map[col] = "max_change_pct"
            elif "min" in col and "涨跌幅" in col:
                rename_map[col] = "min_change_pct"
            elif "sum" in col and "成交额" in col:
                rename_map[col] = "total_amount"
            elif "sum" in col and "dde" in col:
                rename_map[col] = "total_dde"
            elif "median" in col and "市盈率" in col:
                rename_map[col] = "median_pe"
            elif "median" in col and "市净率" in col:
                rename_map[col] = "median_pb"
            elif "mean" in col and "换手率" in col:
                rename_map[col] = "avg_turnover"
        
        result = result.rename(columns=rename_map)
        return result

    # ===== 接口实现 =====

    async def collect(self, session: AsyncSession, **kwargs) -> Any:
        api = kwargs.get("api", "stock_mapping")
        if api == "stock_mapping":
            return await self.get_stock_industry_mapping()
        elif api == "limit_up":
            return await self.get_limit_up_pool()
        elif api == "limit_down":
            return await self.get_limit_down_pool()
        elif api == "broken_limit":
            return await self.get_broken_limit_pool()
        elif api == "consecutive_limit_up":
            return await self.get_consecutive_limit_up(kwargs.get("min_days", 2))
        elif api == "concept_cons":
            return await self.get_concept_cons(kwargs["concept"])
        elif api == "st_stocks":
            return await self.get_st_stocks()
        elif api == "suspended_stocks":
            return await self.get_suspended_stocks()
        elif api == "delisting_risk":
            return await self.get_delisting_risk_stocks()
        elif api == "custom":
            return await self.custom_query(kwargs["query"])
        elif api == "sector_aggregate":
            return await self.get_sector_aggregate(kwargs.get("sector_type", "concept"))
        else:
            return pd.DataFrame()

    async def health_check(self, session: AsyncSession) -> bool:
        started = _time.monotonic()
        try:
            df = await self.custom_query("今天涨停")
            await data_quality_guard.record_success(
                session,
                self.source_name,
                "query_limit_up",
                latency_ms=int((_time.monotonic() - started) * 1000),
                record_count=len(df),
            )
            return len(df) >= 0  # 即使0条也说明接口正常
        except Exception as e:
            await data_quality_guard.record_failure(
                session,
                self.source_name,
                "query_limit_up",
                str(e),
                latency_ms=int((_time.monotonic() - started) * 1000),
            )
            return False
