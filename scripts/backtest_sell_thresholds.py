"""回测验证: 用最近 60 天的 paper_trade_log 样本, 模拟"如果当时用新阈值会怎么样".

策略: 把每笔买入关联的隔天/后续卖出价格时间序列拿出来, 用新旧两套阈值各自决定何时卖,
对比期望 PnL / 胜率 / 盈亏比 / 平均持仓天数.

由于 sell price 时间序列不存在(只有开仓/平仓两个快照), 这里采用保守近似:
- 每个 round-trip 视为一组(buy_price, buy_time, sell_price, sell_time)
- 假设盘中价格是平滑线性变化, 模拟"持仓 N 天的浮盈轨迹"
- 用新旧阈值分别判断是否触发卖出
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.config.settings import settings  # noqa: E402
from app.db.session import Base  # noqa: E402
from app.models.paper import PaperAccount, PaperTradeLog  # noqa: E402
from app.models.stock import StockKline  # noqa: E402


NEW_THRESHOLDS = {
    "stop_loss_pct": 5.0,
    "small_stop_loss_pct": 2.0,
    "take_profit_pct": 5.5,
    "next_day_min_profit_pct": 0.5,
    "pullback_from_high_pct": 2.5,
    "breakeven_protect_high_pct": 3.0,
    "max_hold_days": 5,
}
OLD_THRESHOLDS = {
    "stop_loss_pct": 3.0,
    "small_stop_loss_pct": 1.2,
    "take_profit_pct": 3.5,
    "next_day_min_profit_pct": 1.0,
    "pullback_from_high_pct": 1.5,
    "breakeven_protect_high_pct": 1.5,
    "max_hold_days": 2,
}


@dataclass
class Trip:
    code: str
    buy_dt: datetime
    buy_price: float
    amount: int
    sell_dt: datetime
    sell_price: float
    realized_pnl: float
    actual_profit_pct: float
    actual_hold_days: int


def _simulate_with_thresholds(trip: Trip, thresholds: dict, klines_by_code: dict) -> dict:
    """给定一次买入 + 后续价格时间序列, 用阈值模拟何时卖出."""
    klines = klines_by_code.get(trip.code) or []
    # 找到买入日及之后的价格序列
    future = [k for k in klines if k.trade_date >= trip.buy_dt.date()]
    if not future:
        return {"exit_profit_pct": trip.actual_profit_pct, "exit_day": trip.actual_hold_days, "trigger": "no_data"}
    # 模拟逐日检查
    high_profit = 0.0  # 持仓以来最高浮盈
    commission_rate = 0.0003 + 0.001  # 印花税 0.1% + 手续费 0.03% 单边
    for k in future:
        day_idx = (k.trade_date - trip.buy_dt.date()).days
        if k.close is None or k.close <= 0:
            continue
        profit_pct = (k.close / trip.buy_price - 1) * 100
        # 最高浮盈
        if k.high is not None and k.high > 0:
            high_profit = max(high_profit, (k.high / trip.buy_price - 1) * 100)
        # 硬止损
        if profit_pct <= -thresholds["stop_loss_pct"]:
            return {"exit_profit_pct": profit_pct - commission_rate * 100, "exit_day": day_idx, "trigger": "hard_stop"}
        # 小止损: 假设当天开盘冲高再回落跌破 -2% (worst case 简化处理)
        if profit_pct <= -thresholds["small_stop_loss_pct"] and day_idx >= 1:
            return {"exit_profit_pct": profit_pct - commission_rate * 100, "exit_day": day_idx, "trigger": "small_stop"}
        # 止盈
        if profit_pct >= thresholds["take_profit_pct"]:
            return {"exit_profit_pct": profit_pct - commission_rate * 100, "exit_day": day_idx, "trigger": "take_profit"}
        # 高点回落保护
        if high_profit >= thresholds["breakeven_protect_high_pct"] and profit_pct <= 0.5:
            return {"exit_profit_pct": profit_pct - commission_rate * 100, "exit_day": day_idx, "trigger": "breakeven_protect"}
        # 冲高回落
        if high_profit >= 1.8 and k.high and k.close:
            pullback = (k.close / k.high - 1) * 100
            if pullback <= -thresholds["pullback_from_high_pct"]:
                return {"exit_profit_pct": profit_pct - commission_rate * 100, "exit_day": day_idx, "trigger": "pullback"}
        # 次日不强就走
        if day_idx >= 1 and profit_pct < thresholds["next_day_min_profit_pct"]:
            return {"exit_profit_pct": profit_pct - commission_rate * 100, "exit_day": day_idx, "trigger": "next_day_weak"}
        # 时间止损
        if day_idx >= thresholds["max_hold_days"] and profit_pct <= 0:
            return {"exit_profit_pct": profit_pct - commission_rate * 100, "exit_day": day_idx, "trigger": "time_stop"}
    return {"exit_profit_pct": future[-1].close / trip.buy_price - 1 - commission_rate, "exit_day": (future[-1].trade_date - trip.buy_dt.date()).days, "trigger": "end_of_window"}


def aggregate(sims: list[dict], trips: list[Trip]) -> dict:
    pnls = []
    wins = 0
    hold_days = []
    triggers = {}
    for sim, trip in zip(sims, trips):
        amount_value = trip.buy_price * trip.amount
        pnl = sim["exit_profit_pct"] / 100 * amount_value
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
        hold_days.append(sim["exit_day"])
        triggers[sim["trigger"]] = triggers.get(sim["trigger"], 0) + 1
    total = len(pnls)
    return {
        "n": total,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(sum(pnls) / total, 2) if total else 0,
        "avg_hold_days": round(sum(hold_days) / total, 1) if total else 0,
        "max_loss": round(min(pnls), 2) if pnls else 0,
        "winning_triggers": triggers,
    }


async def main() -> None:
    db_url = settings.DATABASE_URL
    engine = create_async_engine(db_url, future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            # 拿到 active 账户
            account = (
                await session.execute(
                    select(PaperAccount).where(
                        PaperAccount.account_name == "default",
                        PaperAccount.status == "active",
                    )
                )
            ).scalars().first()
            if not account:
                print("no active paper account found")
                return

            # 拿到所有卖单
            sells = (
                await session.execute(
                    select(PaperTradeLog)
                    .where(
                        PaperTradeLog.account_id == account.id,
                        PaperTradeLog.trade_type == "sell",
                        PaperTradeLog.realized_pnl.is_not(None),
                    )
                    .order_by(PaperTradeLog.trade_time)
                )
            ).scalars().all()

            # 配对 buy - sell: 找每个卖出对应的最近买入
            buys = {
                row.code: row
                for row in (
                    await session.execute(
                        select(PaperTradeLog)
                        .where(
                            PaperTradeLog.account_id == account.id,
                            PaperTradeLog.trade_type == "buy",
                        )
                        .order_by(PaperTradeLog.trade_time)
                    )
                ).scalars().all()
            }

            # 按 sell_time 升序, 取最近的 buy < sell_time 配对
            trips = []
            used_buys = set()
            for sell in sells:
                # 找这个 code 在 sell_time 之前未配对的最近一次 buy
                code_buys = [
                    b for b in buys.values()
                    if b.code == sell.code
                    and b.trade_time < sell.trade_time
                    and b.id not in used_buys
                ]
                if not code_buys:
                    continue
                code_buys.sort(key=lambda x: x.trade_time, reverse=True)
                buy = code_buys[0]
                used_buys.add(buy.id)
                if buy.price <= 0 or sell.price <= 0:
                    continue
                actual_profit_pct = round((sell.price / buy.price - 1) * 100, 2)
                actual_hold_days = max(0, (sell.trade_time.date() - buy.trade_time.date()).days)
                trips.append(Trip(
                    code=buy.code,
                    buy_dt=buy.trade_time,
                    buy_price=buy.price,
                    amount=buy.amount,
                    sell_dt=sell.trade_time,
                    sell_price=sell.price,
                    realized_pnl=sell.realized_pnl,
                    actual_profit_pct=actual_profit_pct,
                    actual_hold_days=actual_hold_days,
                ))

            # 加载涉及的所有股票日 K(买入日到卖出日 + 5 天缓冲)
            min_date = min(t.buy_dt.date() for t in trips) - timedelta(days=2)
            max_date = max(t.sell_dt.date() for t in trips) + timedelta(days=10)
            klines = (
                await session.execute(
                    select(StockKline)
                    .where(
                        StockKline.trade_date >= min_date,
                        StockKline.trade_date <= max_date,
                    )
                )
            ).scalars().all()
            klines_by_code: dict[str, list[StockKline]] = {}
            for k in klines:
                klines_by_code.setdefault(k.code, []).append(k)
            for code in klines_by_code:
                klines_by_code[code].sort(key=lambda x: x.trade_date)

            print(f"配对 round-trip 数: {len(trips)}")
            print(f"用 K 线覆盖: {len(klines_by_code)} 个标的")

            old_sims = [_simulate_with_thresholds(t, OLD_THRESHOLDS, klines_by_code) for t in trips]
            new_sims = [_simulate_with_thresholds(t, NEW_THRESHOLDS, klines_by_code) for t in trips]

            actual_total = sum(t.realized_pnl for t in trips)
            actual_hold = sum(t.actual_hold_days for t in trips) / len(trips) if trips else 0
            actual_wins = sum(1 for t in trips if t.realized_pnl > 0)
            print()
            print("=== 实际成交(基线) ===")
            print(f"  笔数: {len(trips)}, 胜率: {round(actual_wins/len(trips)*100, 1) if trips else 0}%")
            print(f"  实际 PnL: {round(actual_total, 2)} 元")
            print(f"  平均持仓: {round(actual_hold, 1)} 天")

            old = aggregate(old_sims, trips)
            new = aggregate(new_sims, trips)

            import json
            print()
            print("=== 旧阈值模拟 ===")
            print(json.dumps(old, ensure_ascii=False, indent=2))
            print()
            print("=== 新阈值模拟 ===")
            print(json.dumps(new, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
