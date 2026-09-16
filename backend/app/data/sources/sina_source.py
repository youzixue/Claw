"""新浪数据源 — 概念成分股+个股行情"""

from datetime import date
from typing import Any
import pandas as pd
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.core.data_quality import data_quality_guard

import akshare as ak


class SinaSource(DataSourceBase):
    """新浪数据源 (via AkShare)
    
    核心能力: 175个概念+84个行业 板块实时行情+成分股明细(含个股级PE/PB/市值/换手率)
    
    API对照:
      板块实时行情: ak.stock_sector_spot(indicator='概念'/'行业')
        返回: label, 板块, 公司家数, 平均价格, 涨跌额, 涨跌幅, 总成交量, 总成交额,
              股票代码, 个股-涨跌幅, 个股-当前价, 个股-涨跌额, 股票名称
      行业分类: ak.stock_classify_sina()
        返回: symbol, code, name, trade, pricechange, changepercent, buy, sell,
              settlement, open, high, low, volume, amount, ticktime,
              per, pb, mktcap, nmc, turnoverratio, class
    """

    source_name = "sina"
    rate_limit = 1.0

    # ===== 板块实时行情 =====

    async def get_sector_list(self) -> pd.DataFrame:
        """获取新浪概念板块列表+实时行情
        
        indicator='概念' → 175个概念板块
        返回列: label, 板块, 公司家数, 平均价格, 涨跌额, 涨跌幅, 总成交量, 总成交额,
                股票代码, 个股-涨跌幅, 个股-当前价, 个股-涨跌额, 股票名称
        """
        return await self._safe_call(
            "stock_sector_spot",
            ak.stock_sector_spot,
            indicator="概念",
        )

    async def get_industry_list(self) -> pd.DataFrame:
        """获取新浪行业板块列表+实时行情
        
        indicator='行业' → 84个行业板块
        """
        return await self._safe_call(
            "stock_sector_spot_industry",
            ak.stock_sector_spot,
            indicator="行业",
        )

    # ===== 行业分类+个股行情 =====

    async def get_stock_classify(self) -> pd.DataFrame:
        """获取新浪行业分类 — 含全A股实时行情
        
        返回列: symbol, code, name, trade, pricechange, changepercent,
                buy, sell, settlement, open, high, low, volume, amount,
                ticktime, per, pb, mktcap, nmc, turnoverratio, class
        注意: 此接口耗时较长(698个板块, ~6分钟), 仅深度复盘时调用
        """
        return await self._safe_call(
            "stock_classify_sina",
            ak.stock_classify_sina,
        )

    # ===== 板块成分股 =====

    async def get_sector_detail(self, sector_label: str) -> pd.DataFrame:
        """获取板块成分股明细 (含PE/PB/市值/换手率/成交量)
        
        sector_label: 板块label, 如 'gn_hwqc'(华为汽车), 'hangye_ZA01'(农业)
        """
        return await self._safe_call(
            "stock_sector_detail",
            ak.stock_sector_detail,
            sector=sector_label,
        )

    # ===== 接口实现 =====

    async def collect(self, session: AsyncSession, **kwargs) -> Any:
        api = kwargs.get("api", "sector_list")
        if api == "sector_list":
            return await self.get_sector_list()
        elif api == "industry_list":
            return await self.get_industry_list()
        elif api == "sector_detail":
            return await self.get_sector_detail(kwargs["sector_label"])
        elif api == "stock_classify":
            return await self.get_stock_classify()
        else:
            return pd.DataFrame()

    async def health_check(self, session: AsyncSession) -> bool:
        try:
            df = await self.get_sector_list()
            return len(df) > 0
        except Exception as e:
            await data_quality_guard.record_failure(session, self.source_name, "health_check", str(e))
            return False
