"""一次性清理脚本：把 paper_position 里 2026-04-30 买入的 3 只僵尸持仓按最新价平仓.

只跑一次, 不会触碰其它正常仓位.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.api.v1 import paper as paper_api  # noqa: E402
from app.config.settings import settings  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog  # noqa: E402
from app.models.stock import StockSpot  # noqa: E402


ZOMBIE_BUY_DATE = "2026-04-30"


async def _resolve_price(session: AsyncSession, code: str, fallback: float) -> tuple[float, str]:
    """优先用 stock_spot 最新价, 否则退回 paper_position.current_price."""
    spot = (
        await session.execute(
            select(StockSpot).where(StockSpot.code == code)
        )
    ).scalar_one_or_none()
    if spot and spot.price and float(spot.price) > 0:
        return float(spot.price), "stock_spot"
    return float(fallback or 0), "position.current_price"


async def cleanup(session_factory: async_sessionmaker[AsyncSession]) -> dict:
    summary = {"closed": [], "skipped": []}

    async with session_factory() as session:
        account = (
            await session.execute(
                select(PaperAccount).where(PaperAccount.account_name == "default").order_by(PaperAccount.id)
            )
        ).scalars().first()
        if not account:
            return {"error": "default paper account not found"}

        zombie_positions = (
            await session.execute(
                select(PaperPosition).where(
                    PaperPosition.account_id == account.id,
                    PaperPosition.is_closed.is_(False),
                    PaperPosition.buy_time.like(f"{ZOMBIE_BUY_DATE}%"),
                )
            )
        ).scalars().all()

        for position in zombie_positions:
            sell_price, price_source = await _resolve_price(session, position.code, position.current_price or position.buy_price)
            if sell_price <= 0:
                summary["skipped"].append({"code": position.code, "reason": "no valid price"})
                continue

            amount_value = sell_price * position.buy_amount
            commission = paper_api._commission(amount_value)
            stamp_tax = paper_api._stamp_tax(amount_value)
            realized_pnl = round(
                (sell_price - position.buy_price) * position.buy_amount - commission - stamp_tax,
                2,
            )

            trade = PaperTradeLog(
                account_id=account.id,
                code=position.code,
                trade_type="sell",
                price=round(sell_price, 2),
                amount=position.buy_amount,
                trade_time=datetime.now(),
                commission=round(commission + stamp_tax, 2),
                signal_id="manual-cleanup-zombie-position",
                reason="[系统清理] 持仓超过4个月从未刷新, 强制按最新价平仓释放资金",
                realized_pnl=realized_pnl,
            )
            session.add(trade)

            position.buy_amount = 0
            position.is_closed = True
            position.current_price = sell_price
            position.profit_loss = round((sell_price - position.buy_price) * 0, 2)
            position.profit_pct = round((sell_price / position.buy_price - 1) * 100, 2) if position.buy_price else 0
            position.hold_days = max(0, (datetime.now().date() - position.buy_time.date()).days)

            summary["closed"].append({
                "code": position.code,
                "name": position.name,
                "buy_price": position.buy_price,
                "sell_price": sell_price,
                "amount": position.buy_amount if position.buy_amount else 0,
                "realized_pnl": realized_pnl,
                "price_source": price_source,
            })

        await session.flush()
        # 触发账户刷新, 让 drawdown / nav / win_rate 跟着重算
        account = await paper_api._refresh_account(session, account)
        summary["account_after"] = {
            "total_assets": account.total_assets,
            "current_capital": account.current_capital,
            "total_return": account.total_return,
            "max_drawdown": account.max_drawdown,
            "win_rate": account.win_rate,
        }
        await session.commit()

    return summary


async def main() -> None:
    db_url = settings.DATABASE_URL
    engine = create_async_engine(db_url, future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        result = await cleanup(SessionLocal)
    finally:
        await engine.dispose()

    import json
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
