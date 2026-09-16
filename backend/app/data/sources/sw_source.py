"""申万数据源 — 行业标准"""

from datetime import date
from typing import Any
import pandas as pd
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.core.data_quality import data_quality_guard

import akshare as ak


class ShenwanSource(DataSourceBase):
    """申万数据源 (via AkShare)
    
    核心能力: 三级行业413个 + 成分股权重+计入日期 + 
              行业级PE/PB/股息率 + 个股行业映射(5805只)
    
    API对照:
      一级行业列表: ak.sw_index_first_info() → 31条
      二级行业列表: ak.sw_index_second_info() → 124条
      三级行业列表: ak.sw_index_third_info() → 258条
      三级成分股:   ak.sw_index_third_cons(symbol=行业代码)
      个股行业映射: ak.stock_industry_clf_hist_sw() → 12717条
    """

    source_name = "shenwan"
    rate_limit = 0.2

    # ===== 行业列表 =====

    async def get_sw_index_list(self, level: int = 1) -> pd.DataFrame:
        """获取申万行业列表
        
        level: 1=一级31个, 2=二级124个, 3=三级258个
        
        一级返回列: 行业代码, 行业名称, 成份个数, 静态市盈率, TTM市盈率, 市净率, 静态股息率
        二级返回列: 行业代码, 行业名称, 上级行业, 成份个数, 静态市盈率, TTM市盈率, 市净率, 静态股息率
        三级返回列: 行业代码, 行业名称, 上级行业, 成份个数, 静态市盈率, TTM市盈率, 市净率, 静态股息率
        """
        func_map = {
            1: ak.sw_index_first_info,
            2: ak.sw_index_second_info,
            3: ak.sw_index_third_info,
        }
        df = await self._safe_call(
            f"sw_index_list_l{level}",
            func_map[level],
        )
        return df

    # ===== 成分股 =====

    async def get_sw_cons(self, symbol: str) -> pd.DataFrame:
        """获取行业成分股 (含权重+计入日期)
        
        返回列: 序号, 股票代码, 股票简称, 纳入时间, 申万1级, 申万2级, 申万3级,
                价格, 市盈率, 市盈率ttm, 市净率, 股息率, 市值,
                归母净利润同比增长, 营业收入同比增长
        """
        return await self._safe_call(
            "sw_index_cons",
            ak.sw_index_third_cons,
            symbol=symbol,
        )

    # ===== 行业行情 =====

    async def get_sw_index_daily(self, symbol: str, start_date: str = "",
                                  end_date: str = "") -> pd.DataFrame:
        """行业指数历史行情"""
        return await self._safe_call(
            "sw_index_daily",
            ak.sw_index_daily_em,
            symbol=symbol, start_date=start_date, end_date=end_date,
        )

    # ===== 个股行业映射 =====

    async def get_stock_industry_clf(self) -> pd.DataFrame:
        """个股→行业映射 (5805只)
        
        返回列: symbol(股票代码), start_date, industry_code(申万行业代码), update_time
        注意: 同一只股票可能有多条记录(行业调整)，取最新的一条
        """
        return await self._safe_call(
            "stock_industry_clf",
            ak.stock_industry_clf_hist_sw,
        )

    # ===== 接口实现 =====

    async def collect(self, session: AsyncSession, **kwargs) -> Any:
        api = kwargs.get("api", "index_list")
        if api == "index_list":
            return await self.get_sw_index_list(kwargs.get("level", 1))
        elif api == "cons":
            return await self.get_sw_cons(kwargs["symbol"])
        elif api == "daily":
            return await self.get_sw_index_daily(
                kwargs["symbol"],
                start_date=kwargs.get("start_date", ""),
                end_date=kwargs.get("end_date", ""),
            )
        elif api == "stock_mapping":
            return await self.get_stock_industry_clf()
        else:
            return pd.DataFrame()

    async def health_check(self, session: AsyncSession) -> bool:
        try:
            df = await self.get_sw_index_list(1)
            return len(df) > 0
        except Exception as e:
            await data_quality_guard.record_failure(session, self.source_name, "health_check", str(e))
            return False
