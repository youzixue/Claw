"""融资融券模块 — 杠杆资金监控与情绪分析

核心能力:
- 融资融券数据采集(AkShare接口)
- 融资买入强度/余额趋势因子计算
- 杠杆情绪指数(市场整体杠杆水平)
- 个股融资融券异动识别
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_date import resolve_latest_trade_date
from app.models.stock import MarginData
from app.core.stock_tagger import stock_tagger


# ========== 数据结构 ==========

@dataclass
class MarginAnomaly:
    """融资融券异动"""
    code: str
    name: str = ""
    trade_date: date = None
    anomaly_type: str = ""           # margin_surge/short_surge/balance_high/margin_exit
    margin_buy: float = 0
    margin_balance: float = 0
    margin_change_pct: float = 0
    short_balance: float = 0
    detail: str = ""


@dataclass
class MarketMarginIndex:
    """市场杠杆情绪指数"""
    trade_date: date
    total_margin_balance: float = 0   # 市场融资余额总计(亿)
    total_short_balance: float = 0    # 市场融券余额总计(亿)
    margin_change_pct: float = 0      # 余额环比变化%
    leverage_sentiment: float = 0     # 杠杆情绪指数(0-100)
    margin_top_codes: list = field(default_factory=list)  # 融资买入额TOP
    short_top_codes: list = field(default_factory=list)    # 融券余额TOP


# ========== 数据采集 ==========

class MarginCollector:
    """融资融券数据采集器"""

    async def collect_margin_data(self, session: AsyncSession,
                                   code: str = None,
                                   start_date: str = "",
                                   end_date: str = "") -> pd.DataFrame:
        """采集融资融券数据

        AkShare接口:
        - ak.stock_margin_detail_sse(date)   上交所融资融券明细
        - ak.stock_margin_detail_szse(date)  深交所融资融券明细
        - ak.stock_margin_underlying_info_szse(date) 深交所标的
        """
        import akshare as ak

        try:
            loop = __import__("asyncio").get_event_loop()

            if code:
                # 个股融资融券
                symbol = code[:6]
                df = await loop.run_in_executor(
                    None,
                    lambda: ak.stock_margin_detail_szse(
                        date=end_date or datetime.now().strftime("%Y%m%d")
                    ),
                )
                if df is not None and not df.empty:
                    df = df[df["证券代码"] == symbol] if "证券代码" in df.columns else df
            else:
                # 市场整体
                trade_date_str = end_date or datetime.now().strftime("%Y%m%d")
                df = await loop.run_in_executor(
                    None,
                    lambda: ak.stock_margin_detail_szse(date=trade_date_str),
                )

            if df is None or df.empty:
                return pd.DataFrame()

            logger.info(f"融资融券数据采集: {len(df)}条")
            return df

        except Exception as e:
            logger.error(f"融资融券数据采集失败: {e}")
            return pd.DataFrame()

    async def save_margin_data(self, session: AsyncSession,
                                df: pd.DataFrame, trade_date: date) -> int:
        """保存融资融券数据"""
        count = 0
        for _, row in df.iterrows():
            code = str(row.get("证券代码", row.get("代码", ""))).zfill(6)
            if not code:
                continue

            margin = MarginData(
                code=code,
                trade_date=trade_date,
                margin_buy=float(row.get("融资买入额", 0) or 0),
                margin_balance=float(row.get("融资余额", 0) or 0),
                margin_change=0,  # 计算环比
                short_sell=float(row.get("融券卖出量", 0) or 0),
                short_balance=float(row.get("融券余额", 0) or 0),
                total_balance=float(row.get("融资融券余额", 0) or 0),
            )
            session.add(margin)
            count += 1

        await session.commit()
        logger.info(f"融资融券数据保存: {count}条, {trade_date}")
        return count


# ========== 融资融券分析 ==========

class MarginAnalyzer:
    """融资融券分析器"""

    # 异动阈值
    MARGIN_BUY_SURGE_THRESHOLD = 200.0    # 融资买入额>200%
    MARGIN_CHANGE_SURGE = 5.0             # 余额环比增长>5%
    MARGIN_CHANGE_EXIT = -5.0             # 余额环比下降>5%
    SHORT_SURGE_THRESHOLD = 50.0          # 融券余额环比增长>50%

    async def analyze_anomalies(self, session: AsyncSession,
                                 trade_date: date = None) -> list[MarginAnomaly]:
        """识别融资融券异动"""
        trade_date = await resolve_latest_trade_date(
            session,
            MarginData.trade_date,
            requested=trade_date,
        )

        result = await session.execute(
            select(MarginData).where(MarginData.trade_date == trade_date)
        )
        records = result.scalars().all()

        anomalies = []
        for record in records:
            anomaly = self._check_anomaly(record)
            if anomaly:
                anomalies.append(anomaly)

        logger.info(f"融资融券异动: {len(anomalies)}个, {trade_date}")
        return anomalies

    def _check_anomaly(self, record: MarginData) -> Optional[MarginAnomaly]:
        """检查单只股票融资融券异动"""
        change = record.margin_change or 0

        # 融资余额环比大增
        if change >= self.MARGIN_CHANGE_SURGE:
            return MarginAnomaly(
                code=record.code,
                trade_date=record.trade_date,
                anomaly_type="margin_surge",
                margin_buy=record.margin_buy,
                margin_balance=record.margin_balance,
                margin_change_pct=round(change, 2),
                short_balance=record.short_balance,
                detail=f"融资余额环比+{change:.1f}%，杠杆资金大举进入",
            )

        # 融资余额环比大降
        if change <= self.MARGIN_CHANGE_EXIT:
            return MarginAnomaly(
                code=record.code,
                trade_date=record.trade_date,
                anomaly_type="margin_exit",
                margin_buy=record.margin_buy,
                margin_balance=record.margin_balance,
                margin_change_pct=round(change, 2),
                short_balance=record.short_balance,
                detail=f"融资余额环比{change:.1f}%，杠杆资金撤离",
            )

        # 融券余额大增
        if record.short_balance and record.short_balance > 0:
            # 简化: 融券余额绝对值较大
            if record.short_balance > record.margin_balance * 0.3:
                return MarginAnomaly(
                    code=record.code,
                    trade_date=record.trade_date,
                    anomaly_type="short_surge",
                    margin_buy=record.margin_buy,
                    margin_balance=record.margin_balance,
                    margin_change_pct=round(change, 2),
                    short_balance=record.short_balance,
                    detail=f"融券余额={record.short_balance:.0f}，做空力量增强",
                )

        return None

    async def calc_margin_index(self, session: AsyncSession,
                                 trade_date: date = None) -> MarketMarginIndex:
        """计算市场杠杆情绪指数"""
        trade_date = await resolve_latest_trade_date(
            session,
            MarginData.trade_date,
            requested=trade_date,
        )

        result = await session.execute(
            select(MarginData).where(MarginData.trade_date == trade_date)
        )
        records = result.scalars().all()

        if not records:
            return MarketMarginIndex(trade_date=trade_date)

        # 汇总
        total_margin = sum(r.margin_balance or 0 for r in records)
        total_short = sum(r.short_balance or 0 for r in records)
        avg_change = np.mean([r.margin_change or 0 for r in records]) if records else 0

        # 杠杆情绪指数(0-100)
        # 基于: 余额变化方向 + 变化幅度 + 融资占比
        sentiment = 50  # 中性
        if avg_change > 0:
            sentiment += min(avg_change * 5, 25)  # 正向加
        else:
            sentiment += max(avg_change * 5, -25)  # 负向减

        # 融资/融券比
        if total_short > 0:
            ms_ratio = total_margin / total_short
            if ms_ratio > 100:  # 融资远大于融券=看多
                sentiment += 10
            elif ms_ratio < 50:
                sentiment -= 10

        sentiment = max(0, min(100, sentiment))

        # TOP股票
        margin_top = sorted(records, key=lambda r: r.margin_buy or 0, reverse=True)[:10]
        short_top = sorted(records, key=lambda r: r.short_balance or 0, reverse=True)[:10]

        return MarketMarginIndex(
            trade_date=trade_date,
            total_margin_balance=round(total_margin / 1e8, 2),  # 转亿
            total_short_balance=round(total_short / 1e8, 2),
            margin_change_pct=round(avg_change, 2),
            leverage_sentiment=round(sentiment, 2),
            margin_top_codes=[{"code": r.code, "margin_buy": r.margin_buy} for r in margin_top],
            short_top_codes=[{"code": r.code, "short_balance": r.short_balance} for r in short_top],
        )

    async def get_margin_factors(self, code: str,
                                  session: AsyncSession,
                                  trade_date: date = None) -> dict:
        """获取个股融资融券因子(供因子引擎使用)"""
        trade_date = await resolve_latest_trade_date(
            session,
            MarginData.trade_date,
            requested=trade_date,
        )

        result = await session.execute(
            select(MarginData).where(
                and_(
                    MarginData.code == code,
                    MarginData.trade_date == trade_date,
                )
            )
        )
        record = result.scalar_one_or_none()

        if not record:
            return {
                "margin_buy": 0,
                "margin_balance_change_avg5": np.nan,
            }

        # 查最近5天计算趋势
        result5 = await session.execute(
            select(MarginData.margin_change).where(
                and_(
                    MarginData.code == code,
                    MarginData.trade_date <= trade_date,
                )
            ).order_by(MarginData.trade_date.desc()).limit(5)
        )
        changes = result5.scalars().all()
        avg_change = float(np.mean(changes)) if changes else np.nan

        return {
            "margin_buy": record.margin_buy or 0,
            "margin_balance_change_avg5": avg_change,
        }


# 全局
margin_collector = MarginCollector()
margin_analyzer = MarginAnalyzer()
