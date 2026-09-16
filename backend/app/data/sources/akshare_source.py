"""同花顺数据源 (AkShare) — 主力行情源"""

from datetime import date, datetime
from typing import Any, Optional
import pandas as pd
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.core.data_quality import data_quality_guard
from app.config.settings import settings

import akshare as ak


class AkShareSource(DataSourceBase):
    """同花顺数据源 (via AkShare)
    
    核心能力: 板块列表/概况/指数历史 + 个股资金流(5190只) + 
              行业/概念资金流 + 大单追踪 + 技术选股(11种) + 财务数据
    """

    source_name = "akshare"
    rate_limit = 0.5  # settings.AKSHARE_RATE_LIMIT

    # ===== 板块 =====

    async def get_sector_list(self, sector_type: str = "concept") -> pd.DataFrame:
        """获取板块列表 (行业/概念)"""
        if sector_type == "concept":
            return await self._safe_call("stock_board_concept_name_ths", ak.stock_board_concept_name_ths)
        else:
            return await self._safe_call("stock_board_industry_name_ths", ak.stock_board_industry_name_ths)

    async def get_sector_index(self, symbol: str, start_date: str = "",
                                end_date: str = "", sector_type: str = "concept") -> pd.DataFrame:
        """获取板块指数历史(OHLCV)

        Args:
            symbol: 板块名称(如"光伏概念"/"医药生物")
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            sector_type: concept(概念)/industry(行业)

        Returns:
            DataFrame含: 日期/开盘/收盘/最高/最低/成交量/成交额/涨跌幅 等
        """
        if sector_type == "concept":
            return await self._safe_call(
                "stock_board_concept_index_ths",
                ak.stock_board_concept_index_ths,
                symbol=symbol, start_date=start_date, end_date=end_date,
            )
        else:
            return await self._safe_call(
                "stock_board_industry_index_ths",
                ak.stock_board_industry_index_ths,
                symbol=symbol, start_date=start_date, end_date=end_date,
            )

    # ===== 资金流 =====

    async def get_individual_fund_flow(self, symbol: str = "即时") -> pd.DataFrame:
        """获取具有明确字段口径的即时个股资金流。

        AkShare 的 ``stock_individual_fund_flow_rank`` 来自东方财富，今日榜提供
        主力及大/中/小单净额；``stock_fund_flow_individual`` 来自同花顺，只提供
        全部流入、流出和总净额，不能冒充主力净流入。返回帧通过 ``attrs`` 携带
        精确来源和口径，调用方据此决定是否可写入 FundFlow 主力指标。
        """
        candidates = []
        if hasattr(ak, "stock_individual_fund_flow_rank"):
            candidates.append((
                "stock_individual_fund_flow_rank",
                ak.stock_individual_fund_flow_rank,
                {"indicator": "今日"},
                "eastmoney_via_akshare",
                "individual_fund_flow_eastmoney_akshare_v1",
                "main_order_net_flow",
            ))
        if hasattr(ak, "stock_individual_fund_flow_rank_ths"):
            candidates.append((
                "stock_individual_fund_flow_rank_ths",
                ak.stock_individual_fund_flow_rank_ths,
                {"indicator": "即时"},
                "ths_via_akshare",
                "individual_fund_flow_ths_rank_v1",
                "unknown",
            ))
        if hasattr(ak, "stock_fund_flow_individual"):
            candidates.append((
                "stock_fund_flow_individual",
                ak.stock_fund_flow_individual,
                {"symbol": symbol},
                "ths_via_akshare",
                "individual_total_fund_flow_ths_v1",
                "total_in_minus_out",
            ))

        last_error: Exception | None = None
        for api_name, fn, kwargs, source, source_version, semantics in candidates:
            try:
                df = await self._safe_call(api_name, fn, **kwargs)
                if isinstance(df, pd.DataFrame) and len(df) > 0:
                    df.attrs.update({
                        "fund_flow_source": source,
                        "fund_flow_source_version": source_version,
                        "fund_flow_semantics": semantics,
                        # Receipt proves transport only. The installed AkShare
                        # adapter drops f124; do not invent a source clock here.
                        "fund_flow_received_at": datetime.now(),
                        "fund_flow_clock_basis": "client_received_only",
                    })
                    return df
            except Exception as exc:
                last_error = exc
                logger.warning(f"[akshare] 个股资金流接口 {api_name} 调用失败: {exc}")
                continue

        if last_error:
            logger.error(f"[akshare] 个股资金流全部接口失败: {last_error}")
        return pd.DataFrame()

    async def get_sector_fund_flow(self, sector_type: str = "概念") -> pd.DataFrame:
        """行业/概念资金流"""
        if sector_type == "概念":
            return await self._safe_call("stock_fund_flow_concept", ak.stock_fund_flow_concept)
        else:
            return await self._safe_call("stock_fund_flow_industry", ak.stock_fund_flow_industry)

    # ===== 技术选股 =====

    async def get_stock_rank(self, indicator: str) -> pd.DataFrame:
        """技术选股 (11种策略)
        
        indicator: 连涨/连跌/放量/缩量/创新高/创新低/...
        """
        return await self._safe_call(
            "stock_rank_ths",
            ak.stock_rank_ths,
            indicator=indicator,
        )

    # ===== 财务数据 =====

    async def get_stock_financial(self, symbol: str, indicator: str = "按报告期") -> pd.DataFrame:
        """财务数据"""
        return await self._safe_call(
            "stock_financial_analysis_indicator",
            ak.stock_financial_analysis_indicator,
            symbol=symbol, indicator=indicator,
        )

    # ===== 行情 =====

    async def get_stock_history(self, symbol: str, period: str = "daily",
                                start_date: str = "", end_date: str = "",
                                adjust: str = "qfq") -> pd.DataFrame:
        """个股历史行情 (前复权)"""
        return await self._safe_call(
            "stock_zh_a_hist",
            ak.stock_zh_a_hist,
            symbol=symbol, period=period,
            start_date=start_date, end_date=end_date, adjust=adjust,
        )

    # ===== 接口实现 =====

    async def collect(self, session: AsyncSession, **kwargs) -> Any:
        """通用采集入口"""
        api = kwargs.get("api", "sector_list")
        if api == "sector_list":
            return await self.get_sector_list(kwargs.get("sector_type", "concept"))
        elif api == "fund_flow":
            return await self.get_individual_fund_flow()
        elif api == "sector_fund_flow":
            return await self.get_sector_fund_flow(kwargs.get("sector_type", "概念"))
        elif api == "stock_history":
            return await self.get_stock_history(
                kwargs["symbol"],
                start_date=kwargs.get("start_date", ""),
                end_date=kwargs.get("end_date", ""),
            )
        elif api == "sector_index":
            return await self.get_sector_index(
                kwargs["symbol"],
                start_date=kwargs.get("start_date", ""),
                end_date=kwargs.get("end_date", ""),
                sector_type=kwargs.get("sector_type", "concept"),
            )
        else:
            logger.warning(f"未知采集类型: {api}")
            return pd.DataFrame()

    async def health_check(self, session: AsyncSession) -> bool:
        """健康检查 — 尝试获取板块列表"""
        try:
            df = await self.get_sector_list("concept")
            return len(df) > 0
        except Exception as e:
            await data_quality_guard.record_failure(session, self.source_name, "health_check", str(e))
            return False
