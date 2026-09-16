"""交易日历服务 — P0核心"""

from datetime import date, datetime, time, timedelta
from typing import Optional
from loguru import logger

import akshare as ak
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session
from app.models.governance import TradeCalendarModel


# A股交易时段定义
TRADE_SESSIONS = {
    "pre_auction":   (time(9, 15),  time(9, 25)),    # 集合竞价
    "morning":       (time(9, 30),  time(11, 30)),   # 早盘
    "lunch_break":   (time(11, 30), time(13, 0)),    # 午休
    "afternoon":     (time(13, 0),  time(15, 0)),    # 午盘
    "after_hours":   (time(15, 0),  time(20, 0)),    # 盘后处理
    "night_session": (time(21, 0),  time(23, 59)),   # 期货夜盘(开始)
}

# 休市状态
CLOSED_SESSIONS = {"closed", "weekend", "holiday"}

OFFICIAL_CLOSED_RANGES = (
    # 2026 年沪深北交易所节假日休市安排
    (date(2026, 1, 1), date(2026, 1, 4)),
    (date(2026, 2, 14), date(2026, 2, 24)),
    (date(2026, 2, 28), date(2026, 2, 28)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 5, 9), date(2026, 5, 9)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 20), date(2026, 9, 20)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 10)),
)


def is_official_closed_day(dt: date) -> bool:
    """Return a deterministic exchange-holiday override.

    This synchronous helper is safe for label builders and quality gates that
    must reject known closed dates before any network/calendar fallback.
    """

    return any(start <= dt <= end for start, end in OFFICIAL_CLOSED_RANGES)


def _is_official_closed_day(dt: date) -> bool:
    """Backward-compatible private alias."""

    return is_official_closed_day(dt)


class TradeCalendar:
    """交易日历服务"""

    def __init__(self):
        self._cache: dict[date, bool] = {}

    async def _ensure_loaded(self, year: int):
        """确保指定年份的交易日历已加载"""
        async with async_session() as session:
            result = await session.execute(
                select(TradeCalendarModel).where(
                    TradeCalendarModel.trade_date >= date(year, 1, 1),
                    TradeCalendarModel.trade_date <= date(year, 12, 31),
                )
            )
            rows = result.scalars().all()
            if len(rows) > 200:  # 一年约250个交易日
                for row in rows:
                    self._cache[row.trade_date] = row.is_trade_day
                return

        # 没有数据，从AkShare获取
        await self._sync_from_source(year)

    async def _sync_from_source(self, year: int):
        """从AkShare同步交易日历"""
        try:
            df = ak.tool_trade_date_hist_sina()
            trade_dates = set()
            for _, row in df.iterrows():
                d = row["trade_date"]
                if isinstance(d, str):
                    d = datetime.strptime(d, "%Y-%m-%d").date()
                if d.year == year:
                    trade_dates.add(d)

            # 写入数据库
            async with async_session() as session:
                for d in trade_dates:
                    existing = await session.get(TradeCalendarModel, d)
                    if not existing:
                        session.add(TradeCalendarModel(
                            trade_date=d,
                            is_trade_day=True,
                            session_type="full",
                        ))
                # 补充非交易日（简化：只标记交易日）
                await session.commit()

            # 更新缓存
            for d in trade_dates:
                self._cache[d] = True

            logger.info(f"交易日历同步完成: {year}年, {len(trade_dates)}个交易日")

        except Exception as e:
            logger.error(f"交易日历同步失败: {e}")
            # 降级：用简单规则判断
            self._fallback_calendar(year)

    def _fallback_calendar(self, year: int):
        """降级方案：用周末规则判断"""
        from datetime import timedelta
        d = date(year, 1, 1)
        end = date(year, 12, 31)
        while d <= end:
            self._cache[d] = d.weekday() < 5  # 周一到周五
            d += timedelta(days=1)

    async def is_trade_day(self, dt: Optional[date] = None) -> bool:
        """是否交易日"""
        dt = dt or date.today()
        if _is_official_closed_day(dt):
            self._cache[dt] = False
            return False
        if dt not in self._cache:
            await self._ensure_loaded(dt.year)
        return self._cache.get(dt, dt.weekday() < 5)

    def get_trade_session(self, dt: Optional[datetime] = None) -> str:
        """获取当前时段"""
        dt = dt or datetime.now()
        current_time = dt.time()

        for session_name, (start, end) in TRADE_SESSIONS.items():
            if start <= current_time < end:
                return session_name

        # 期货夜盘跨日判断 (0:00-2:30)
        if time(0, 0) <= current_time < time(2, 30):
            return "night_session"

        return "closed"

    async def next_trade_day(self, dt: Optional[date] = None) -> date:
        """下一个交易日；覆盖春节、国庆等长休市窗口。"""
        dt = dt or date.today()
        d = dt + timedelta(days=1)
        for _ in range(40):
            if await self.is_trade_day(d):
                return d
            d += timedelta(days=1)
        raise RuntimeError(f"无法在40日内解析 {dt} 的下一交易日")

    async def previous_trade_day(self, dt: Optional[date] = None) -> date:
        """上一个交易日。"""

        dt = dt or date.today()
        d = dt - timedelta(days=1)
        for _ in range(40):
            if await self.is_trade_day(d):
                return d
            d -= timedelta(days=1)
        raise RuntimeError(f"无法在40日内解析 {dt} 的上一交易日")

    async def shift_trade_day(self, dt: date, offset: int) -> date:
        """按官方交易日移动，offset=0 返回输入日（若为交易日）。"""

        if offset == 0:
            if not await self.is_trade_day(dt):
                raise ValueError(f"{dt} 不是交易日")
            return dt
        current = dt
        step = 1 if offset > 0 else -1
        for _ in range(abs(offset)):
            current = (
                await self.next_trade_day(current)
                if step > 0
                else await self.previous_trade_day(current)
            )
        return current

    async def trade_days_between(self, start: date, end: date) -> list[date]:
        """两个日期间的交易日列表"""
        await self._ensure_loaded(start.year)
        if end.year != start.year:
            await self._ensure_loaded(end.year)

        days: list[date] = []
        d = start
        while d <= end:
            if await self.is_trade_day(d):
                days.append(d)
            d += timedelta(days=1)
        return days

    async def is_trading_hours(self) -> bool:
        """当前是否在交易时段"""
        if not await self.is_trade_day():
            return False
        session = self.get_trade_session()
        return session in ("pre_auction", "morning", "afternoon")


# 全局单例
trade_calendar = TradeCalendar()
