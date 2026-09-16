"""交易数据日期口径工具."""

from datetime import date
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.trade_calendar import trade_calendar


async def resolve_latest_trade_date(
    session: AsyncSession,
    date_col,
    requested: date | None = None,
) -> date:
    """统一交易日期策略.

    - 指定 requested 则直接使用
    - 否则优先取 <= today 的最大交易日
    - 若无，再兜底取全表最大交易日
    - 仍无则返回 today
    """
    if requested is not None:
        return requested

    today = date.today()
    candidates = (
        await session.execute(
            select(date_col)
            .where(date_col <= today)
            .distinct()
            .order_by(date_col.desc())
            .limit(20)
        )
    ).scalars().all()
    for candidate in candidates:
        if candidate and await trade_calendar.is_trade_day(candidate):
            return candidate

    latest_any = (await session.execute(select(func.max(date_col)))).scalar_one_or_none()
    return latest_any or today


async def resolve_latest_trade_date_any(
    session: AsyncSession,
    date_cols: Iterable,
    requested: date | None = None,
) -> date:
    """多数据源统一口径：从多个日期列中选最近可用交易日。"""
    if requested is not None:
        return requested

    today = date.today()
    candidates: list[date] = []
    for col in date_cols:
        rows = (
            await session.execute(
                select(col)
                .where(col <= today)
                .distinct()
                .order_by(col.desc())
                .limit(20)
            )
        ).scalars().all()
        for d in rows:
            if d and await trade_calendar.is_trade_day(d):
                candidates.append(d)
                break

    if candidates:
        return max(candidates)

    any_candidates: list[date] = []
    for col in date_cols:
        d = (await session.execute(select(func.max(col)))).scalar_one_or_none()
        if d:
            any_candidates.append(d)

    return max(any_candidates) if any_candidates else today
