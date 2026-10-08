"""A股指数数据源 — 三大指数真实盘中快照 + 日线回退"""

from typing import Any
from datetime import date, datetime
import httpx

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

    async def get_index_history_window(self, code: str, *, start_date: date,
                                       end_date: date,
                                       expected_trade_dates: list[date] | None = None) -> pd.DataFrame:
        """盘外历史专用：有界异步HTTP，明确指数市场，不调用无超时V8全历史接口。

        腾讯只接纳原始day，绝不拿股票qfqday替代指数；失败保留缺失，不回退东财。
        只消费可核定的日OHLC，不猜测腾讯第六列的量/额单位。
        """
        symbol = self.INDEX_SYMBOLS[code]
        failures = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            try:
                response = await client.get(
                    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
                    params={"param": f"{symbol},day,{start_date},{end_date},120,"},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != 0:
                    raise ValueError("腾讯指数历史状态无效")
                raw = ((payload.get("data") or {}).get(symbol) or {}).get("day")
                if not isinstance(raw, list) or not raw:
                    raise ValueError("腾讯原始day缺失，不接受qfqday")
                rows = []
                for row in raw:
                    if not isinstance(row, list) or len(row) < 5:
                        raise ValueError("腾讯指数历史行不完整")
                    rows.append(dict(zip(("date", "open", "close", "high", "low"), row[:5])))
                contract = "tencent_index_raw_day_v1"
                frame = pd.DataFrame(rows)
                from app.data.index_history import normalize_index_daily_frame
                valid_days = normalize_index_daily_frame(frame, code=code)
                required_days = set(expected_trade_dates or [end_date])
                if not required_days.issubset(valid_days):
                    raise ValueError("腾讯指数历史缺日/坏值；不启用东财回退")
                frame.attrs.update(source_contract=contract, index_symbol=symbol,
                                   source_observed_at=datetime.now().isoformat(),
                                   requested_start=start_date.isoformat(),
                                   requested_end=end_date.isoformat(),
                                   primary_failures=failures)
                return frame
            except Exception as exc:
                # 不把可能含URL/凭据的异常全文写入通知或质量台账。
                failures.append({"provider": "tencent", "error_type": type(exc).__name__})
        frame = pd.DataFrame()
        frame.attrs["source_failures"] = failures
        return frame

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
