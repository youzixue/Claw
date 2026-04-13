"""A股指数数据源 — 三大指数真实盘中快照 + 日线回退"""

from typing import Any

import pandas as pd
import akshare as ak
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.core.data_quality import data_quality_guard


class IndexSource(DataSourceBase):
    """指数数据源

    优先使用新浪实时指数快照，失败时回退到日线接口。
    """

    source_name = "index"
    rate_limit = 0.2

    INDEX_SYMBOLS = {
        "000001": "sh000001",
        "399001": "sz399001",
        "399006": "sz399006",
    }

    async def get_index_spot(self) -> pd.DataFrame:
        return await self._safe_call(
            "stock_zh_index_spot_sina",
            ak.stock_zh_index_spot_sina,
        )

    async def get_index_daily(self, code: str) -> pd.DataFrame:
        symbol = self.INDEX_SYMBOLS[code]
        return await self._safe_call(
            "stock_zh_index_daily",
            ak.stock_zh_index_daily,
            symbol=symbol,
        )

    async def collect(self, session: AsyncSession, **kwargs) -> Any:
        if kwargs.get("mode") == "spot":
            return await self.get_index_spot()

        code = kwargs.get("code")
        if code:
            return await self.get_index_daily(code)

        result = {"spot": await self.get_index_spot()}
        for idx_code in self.INDEX_SYMBOLS:
            result[idx_code] = await self.get_index_daily(idx_code)
        return result

    async def health_check(self, session: AsyncSession) -> bool:
        try:
            df = await self.get_index_spot()
            return len(df) > 0
        except Exception as e:
            await data_quality_guard.record_failure(session, self.source_name, "health_check", str(e))
            return False
