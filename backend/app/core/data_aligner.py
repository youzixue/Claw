"""数据对齐与复权"""

from datetime import datetime
from typing import Optional
import pandas as pd
from loguru import logger


class DataAligner:
    """数据对齐与复权处理"""

    def align_timestamp(self, df: pd.DataFrame, time_col: str = "time",
                        target_tz: str = "Asia/Shanghai") -> pd.DataFrame:
        """多源时间戳统一到交易所时区"""
        if time_col in df.columns:
            df[time_col] = pd.to_datetime(df[time_col])
            if df[time_col].dt.tz is not None:
                df[time_col] = df[time_col].dt.tz_convert(target_tz)
            else:
                df[time_col] = df[time_col].dt.tz_localize(target_tz)
        return df

    def forward_adjust(self, quotes: pd.DataFrame,
                       dividend_data: pd.DataFrame) -> pd.DataFrame:
        """前复权处理
        
        前复权: 保持当前价格不变，调整历史价格
        公式: 调整价 = 原价 × 复权因子
        """
        if dividend_data.empty:
            return quotes

        # 计算复权因子 (从最新到最早累乘)
        quotes = quotes.sort_values("trade_date")
        adj_factor = 1.0
        result = quotes.copy()

        for _, div in dividend_data.iterrows():
            ex_date = div.get("ex_date") or div.get("dividend_date")
            if ex_date is None:
                continue
            # 在除权日之前的记录需要调整
            mask = result["trade_date"] < ex_date
            ratio = div.get("adj_ratio", 1.0)
            adj_factor *= ratio

        # 应用复权因子
        for col in ["open", "high", "low", "close"]:
            if col in result.columns:
                result[col] = result[col] * adj_factor

        return result

    def resample(self, df: pd.DataFrame, date_col: str = "trade_date",
                 target_freq: str = "D") -> pd.DataFrame:
        """频率转换 (分钟线→日线等)"""
        df = df.copy()
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.set_index(date_col)

        agg_rules = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "amount": "sum",
        }
        # 只聚合存在的列
        existing_cols = {k: v for k, v in agg_rules.items() if k in df.columns}

        if existing_cols:
            return df.resample(target_freq).agg(existing_cols).dropna(how="all")
        return df


# 全局单例
data_aligner = DataAligner()
