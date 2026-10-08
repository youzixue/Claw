"""交易日历服务 — P0核心"""

from datetime import date, datetime, time, timedelta
from typing import Optional
import json
from loguru import logger

import akshare as ak
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session
from app.models.governance import TradeCalendarModel, DataWatermarkRevision


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


def _seconds_of(session_time: time) -> int:
    """当日 00:00 起算的秒数。"""
    return session_time.hour * 3600 + session_time.minute * 60 + session_time.second


def trading_elapsed_seconds(moment: datetime) -> float:
    """把墙钟时刻映射为「当日已流逝的交易时间（秒）」，**不封顶**。

    午休（11:30–13:00）与开盘前不推进；**收盘后继续按墙钟推进**。

    用途：把「数据新鲜度」从**墙钟口径**改为**交易时间口径**。
    否则正常的午休休市会被误判成数据陈旧 —— 2026-09-16 实测：午休后
    13:02 的首个 run 因钟差计算被判 stale，**15 条晋级路线全部失败关闭**，
    而同一个上午的四次 run 全部正常。午休期间墙钟走 93 分钟、交易时间
    只走 0 分钟，规则原意显然是「交易期间数据不能断」。

    ⚠️ **只有午休冻结，开盘前与盘后都按墙钟推进**。
    若开盘前也返回 0（初版如此），则 00:00–09:30 之间任意两个时刻的交易时间
    差都是 0，陈旧判定会整段失效 —— 该缺陷被
    `test_tenbagger_push_logic` 的过期时钟断言抓出；若盘后冻结则会把收盘
    前后的真实缺口压缩掉（`test_main_fund_consumer_repair_20260914` 抓出）。
    实现取「午夜起墙钟秒数 − 已跨过的午休时长」，该式在午休处连续且全程单调。

    注意：交易时段内真实的超阈值缺口，在此口径下**仍然照常拦截**，
    本函数不放宽任何新鲜度阈值。
    """
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    base = (moment - midnight).total_seconds()
    lunch_start = _seconds_of(TRADE_SESSIONS["lunch_break"][0])
    lunch_end = _seconds_of(TRADE_SESSIONS["lunch_break"][1])
    if base <= lunch_start:
        return base                      # 开盘前与上午：墙钟推进
    if base < lunch_end:
        return float(lunch_start)        # 午休：交易时间冻结
    return base - (lunch_end - lunch_start)   # 下午与盘后：扣掉午休时长

OFFICIAL_CLOSED_RANGES = (
    # SSE 2025-12-22 official schedule (not government make-up workdays):
    # https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml
    (date(2026, 1, 1), date(2026, 1, 4)),
    (date(2026, 2, 14), date(2026, 2, 23)),
    (date(2026, 2, 28), date(2026, 2, 28)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 5, 9), date(2026, 5, 9)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 20), date(2026, 9, 20)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 7)),
    (date(2026, 10, 10), date(2026, 10, 10)),
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


async def _persist_known_closed_days(session: AsyncSession, year: int) -> list[date]:
    """Record only independently known closures, never infer weekday state from absence.

    Existing rows are not rewritten. The append-only receipt is observed NOW,
    not evidence that this application had recorded the dates in the past.
    Caller owns commit/rollback; a receipt failure rolls back the inserted days.
    """
    # Only the currently verified annual contract may seed missing evidence.
    # Other years need their own official source review, not this year's template.
    if year != 2026:
        return []
    start, end = date(year, 1, 1), date(year, 12, 31)
    closed = {}
    day = start
    while day <= end:
        if day.weekday() >= 5 or is_official_closed_day(day):
            closed[day] = "verified_weekend_v1" if day.weekday() >= 5 else "sse_2026_closure_v1"
        day += timedelta(days=1)
    existing = dict((await session.execute(select(
        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day
    ).where(TradeCalendarModel.trade_date.between(start, end)))).all())
    conflicts = [day.isoformat() for day in closed if day in existing and existing[day] is not False]
    if conflicts:
        raise ValueError("calendar closure conflicts with recorded state: " + ", ".join(conflicts))
    missing = sorted(set(closed) - set(existing))
    if not missing:
        return []
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        raise ValueError("unsupported calendar persistence dialect")
    # Concurrent syncs cannot overwrite evidence or claim each other's inserts.
    result = await session.execute(insert(TradeCalendarModel).values([
        {"trade_date": day, "is_trade_day": False, "session_type": "closed", "note": closed[day]}
        for day in missing
    ]).on_conflict_do_nothing(index_elements=["trade_date"]).returning(TradeCalendarModel.trade_date))
    added = sorted(result.scalars().all())
    observed = datetime.now()
    if added:
        session.add(DataWatermarkRevision(
            dataset="trade_calendar_closed_days", trade_date=start,
            observed_at=observed, max_available_at=None, replaced_at=observed,
            replacement_kind="calendar_closed_seed", record_count=len(added),
            expected_count=len(closed), completeness=len(added) / len(closed),
            status="partial",
            details_json=json.dumps({
                "contract": "verified_closed_calendar_seed_v1",
                "scope": "missing_known_closures_only", "historical_arrival_certified": False,
                "sources": [
                    "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml",
                    "https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf",
                ],
                "added_dates": [day.isoformat() for day in added],
                "notes": {day.isoformat(): closed[day] for day in added},
                "ordinary_missing_weekdays": "unknown_not_inferred",
            }, ensure_ascii=False),
        ))
        await session.flush()
    # A conflicting writer must not turn a skipped insert into a false receipt.
    states = dict((await session.execute(select(
        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day
    ).where(TradeCalendarModel.trade_date.in_(missing)))).all())
    if any(states.get(day) is not False for day in missing):
        raise ValueError("calendar closure changed concurrently")
    return added


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
            if sum(row.is_trade_day is True for row in rows) > 200:  # Closures are not open-session coverage.
                added = await _persist_known_closed_days(session, year)
                await session.commit()
                for row in rows:
                    self._cache[row.trade_date] = row.is_trade_day
                self._cache.update({day: False for day in added})
                return

        # 没有数据，从AkShare获取
        await self._sync_from_source(year)

    async def sync_known_closed_days(self, year: int) -> list[date]:
        """Idempotent, source-backed calendar repair; no market or prediction writes."""
        async with async_session() as session:
            added = await _persist_known_closed_days(session, year)
            await session.commit()
        self._cache.update({day: False for day in added})
        return added

    async def _sync_from_source(self, year: int):
        """从AkShare同步交易日历"""
        try:
            df = ak.tool_trade_date_hist_sina()
            trade_dates = set()
            for _, row in df.iterrows():
                d = row["trade_date"]
                if isinstance(d, str):
                    d = datetime.strptime(d, "%Y-%m-%d").date()
                if isinstance(d, datetime):
                    d = d.date()
                if d.year == year:
                    if d.weekday() >= 5 or is_official_closed_day(d):
                        raise ValueError(f"calendar source marks known closure open: {d}")
                    trade_dates.add(d)
            if not trade_dates:
                raise ValueError(f"calendar source has no open sessions for {year}")

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
                await session.flush()
                await _persist_known_closed_days(session, year)
                await session.commit()
                saved_states = dict((await session.execute(select(
                    TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day
                ).where(TradeCalendarModel.trade_date.between(date(year, 1, 1), date(year, 12, 31))))).all())

            # Only committed states enter the cache; retain explicit existing closures.
            self._cache.update(saved_states)

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
