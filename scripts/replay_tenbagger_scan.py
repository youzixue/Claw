"""E(连板高标接力) 止盈/止损/持仓/MA20 参数扫描 (2026-08-31)

目的: 在严格口径下寻找 E 的稳健最优卖出参数组合, 并做防过拟合检验:
  1. 网格: 止盈 8/10/12/15/20/25 × 止损 5/6/8/10 × 持仓 3/5/7/10 × MA20 on/off
  2. 稳健性: 剔除Top3赢家后的平均收益 (检验不依赖少数大赢家)
  3. 稳定性: 前后半段分时段收益 (检验时间稳定性)
  4. 邻域平滑: 最优解周围参数不应剧烈跳变

口径与回放引擎一致: 信号=limit_up_pool连板4-8, ST剔除, 20%板区分,
次日开盘买入, 次日一字跳过, 逐日按收盘价检查卖出规则, 佣金万3+印花千1.
每笔固定投入 2 万元 (等权), 收益率口径更公平.

用法:  python scripts/replay_tenbagger_scan.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.models.stock import StockKline
from scripts.replay_five_strategies import (
    COMMISSION_RATE,
    STAMP_TAX_RATE,
    _load_highboard_signals,
    _load_kline_map,
)

CAPITAL_PER_TRADE = 20_000.0  # 每笔等权 2 万元


@dataclass
class ScanParams:
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_days: int
    use_ma20: bool


def _ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def _simulate_one(
    code: str,
    name: str,
    signal_date: date,
    klines: list[StockKline],
    p: ScanParams,
) -> dict | None:
    """单信号单参数模拟: 返回 {ret_pct, sell_reason, sell_date} 或 None(不可买)."""
    if "ST" in str(name or "").upper():
        return None
    limit_pct = 0.20 if str(code or "").startswith(("688", "300", "301")) else 0.10
    idx = -1
    for i, k in enumerate(klines):
        if k.trade_date > signal_date:
            idx = i
            break
    if idx < 0 or idx >= len(klines):
        return None
    buy_k = klines[idx]
    buy_price = float(buy_k.open or buy_k.close or 0)
    if buy_price <= 0:
        return None
    # 次日一字板(开盘≈涨停)无法买入
    if idx >= 1:
        prev_close = float(getattr(klines[idx - 1], "close", 0) or 0)
        if prev_close > 0 and buy_price >= prev_close * (1 + limit_pct) * 0.998:
            return None

    stop_loss_price = buy_price * (1 - p.stop_loss_pct / 100)
    # MA20 计算: 前置K线不足时 ma20=None, 跳过 MA20 规则 (与实盘 _build_short_sell_context 一致)
    closes_before = [float(k.close or 0) for k in klines[:idx]]

    # 逐日检查 (day_offset=0 仅硬止损, T+1)
    for day_offset in range(0, p.max_hold_days + 1):
        day_idx = idx + day_offset
        if day_idx >= len(klines):
            break
        k = klines[day_idx]
        price = float(k.close or 0)
        if price <= 0:
            continue
        profit_pct = (price / buy_price - 1) * 100
        reason = ""
        if day_offset == 0:
            if profit_pct <= -p.stop_loss_pct:
                reason = f"买入当日收盘破硬止损：{profit_pct:.2f}%"
        else:
            if price <= stop_loss_price:
                reason = f"触发止损价：{price:.2f} <= {stop_loss_price:.2f}"
            elif profit_pct <= -p.stop_loss_pct:
                reason = f"触发硬止损：{profit_pct:.2f}%"
            elif profit_pct >= p.take_profit_pct:
                reason = f"触发止盈：{profit_pct:.2f}%"
            elif p.use_ma20:
                ma20 = _ma(closes_before + [float(x.close or 0) for x in klines[idx:day_idx]], 20)
                if ma20 is not None and price < ma20:
                    reason = f"跌破20日线：{price:.2f} < MA20 {ma20:.2f}"
        if not reason and day_offset >= p.max_hold_days:
            reason = f"持仓{p.max_hold_days}天到期平仓"
        if reason:
            sell_price = price
            ret_pct = (sell_price / buy_price - 1) * 100
            # 成本: 佣金万3双向 + 印花千1卖出 (按固定投入近似)
            cost_bps = COMMISSION_RATE * 2 + STAMP_TAX_RATE
            ret_pct_net = ret_pct - cost_bps * 100
            return {"ret_pct": ret_pct_net, "reason": reason, "sell_date": k.trade_date}

    # 数据截止未卖出: 用最后可用收盘 (保守口径, 与回放引擎一致)
    last_k = klines[min(idx + p.max_hold_days, len(klines) - 1)]
    sell_price = float(last_k.close or 0)
    ret_pct = (sell_price / buy_price - 1) * 100
    cost_bps = COMMISSION_RATE * 2 + STAMP_TAX_RATE
    return {"ret_pct": ret_pct - cost_bps * 100, "reason": "数据截止平仓", "sell_date": last_k.trade_date}


def _evaluate(results: list[dict], all_signal_dates: list[date]) -> dict:
    """等权评估: 平均收益/胜率/盈亏比/剔除Top3/前后半段."""
    if not results:
        return {"n": 0}
    rets = [r["ret_pct"] for r in results]
    wins = [x for x in rets if x > 0]
    losses = [abs(x) for x in rets if x <= 0]
    n = len(rets)
    avg_ret = sum(rets) / n
    win_rate = len(wins) / n * 100
    pl = (sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else 0.0
    # 剔除 Top3 赢家
    sorted_rets = sorted(rets, reverse=True)
    rem_rets = sorted_rets[3:]
    avg_ret_no_top3 = sum(rem_rets) / len(rem_rets) if rem_rets else 0.0
    # 前后半段 (按卖出日期)
    dates = sorted(r["sell_date"] for r in results)
    mid = dates[len(dates) // 2]
    first = [r["ret_pct"] for r in results if r["sell_date"] <= mid]
    second = [r["ret_pct"] for r in results if r["sell_date"] > mid]
    first_avg = sum(first) / len(first) if first else 0.0
    second_avg = sum(second) / len(second) if second else 0.0
    return {
        "n": n,
        "avg_ret": round(avg_ret, 3),
        "win_rate": round(win_rate, 1),
        "pl_ratio": round(pl, 2),
        "avg_ret_no_top3": round(avg_ret_no_top3, 3),
        "first_half": round(first_avg, 3),
        "second_half": round(second_avg, 3),
        "max_loss": round(min(rets), 2),
        "total_ret": round(sum(rets), 1),
    }


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            signals = await _load_highboard_signals(session)
            codes = list(dict.fromkeys(s["code"] for s in signals))
            min_signal = min(s["signal_date"] for s in signals)
            kline_map = await _load_kline_map(session, codes, min_signal - timedelta(days=60))
    finally:
        await engine.dispose()

    print(f"信号: {len(signals)} 个 (limit_up_pool 连板4-8, {min_signal} 起)")
    grid = []
    for tp in (8, 10, 12, 15, 20, 25):
        for sl in (5, 6, 8, 10):
            for hold in (3, 5, 7, 10):
                for ma20 in (False, True):
                    grid.append(ScanParams(tp, sl, hold, ma20))

    rows = []
    for p in grid:
        results = []
        for sig in signals:
            code = sig.get("code") or ""
            kl = kline_map.get(code)
            if not kl:
                continue
            r = _simulate_one(code, sig.get("name") or code, sig["signal_date"], kl, p)
            if r:
                results.append(r)
        ev = _evaluate(results, [])
        ev["tp"], ev["sl"], ev["hold"], ev["ma20"] = (
            p.take_profit_pct, p.stop_loss_pct, p.max_hold_days, p.use_ma20,
        )
        rows.append(ev)

    # 排名: 平均收益降序, 但要求样本量>=60 且 剔除Top3后仍>0
    viable = [r for r in rows if r["n"] >= 60 and r["avg_ret_no_top3"] > 0]
    viable.sort(key=lambda r: r["avg_ret"], reverse=True)
    print(f"\n有效组合(样本>=60 且剔除Top3后仍正): {len(viable)} 个")
    print(f"{'止盈':>4}{'止损':>5}{'持仓':>5}{'MA20':>6}{'样本':>6}{'均收%':>8}{'胜率%':>7}{'盈亏比':>7}{'剔Top3%':>9}{'前半%':>8}{'后半%':>8}{'最大亏%':>8}")
    print("-" * 96)
    for r in viable[:25]:
        print(
            f"{r['tp']:>5}{r['sl']:>5}{r['hold']:>5}{str(r['ma20']):>7}"
            f"{r['n']:>6}{r['avg_ret']:>8.3f}{r['win_rate']:>7.1f}{r['pl_ratio']:>7.2f}"
            f"{r['avg_ret_no_top3']:>9.3f}{r['first_half']:>8.3f}{r['second_half']:>8.3f}{r['max_loss']:>8.2f}"
        )

    # 基线对比: 当前配置 12/8/5 无MA20
    print("\n基线 12/8/5/无MA20:")
    base = [r for r in rows if r["tp"] == 12 and r["sl"] == 8 and r["hold"] == 5 and not r["ma20"]]
    if base:
        b = base[0]
        print(f"  样本{b['n']} 均收{b['avg_ret']}% 胜率{b['win_rate']}% 盈亏比{b['pl_ratio']} 剔Top3 {b['avg_ret_no_top3']}% 前后半 {b['first_half']}/{b['second_half']}%")

    # 全局 top10 (不看约束)
    print("\n全局 Top10 (仅按平均收益):")
    all_sorted = sorted(rows, key=lambda r: r["avg_ret"], reverse=True)
    for r in all_sorted[:10]:
        flag = " ✓稳健" if r["n"] >= 60 and r["avg_ret_no_top3"] > 0 else ""
        print(
            f"  止盈{r['tp']:>3} 止损{r['sl']:>3} 持仓{r['hold']:>3} MA20={str(r['ma20']):>5}"
            f" 样本{r['n']:>4} 均收{r['avg_ret']:>7.3f}% 胜率{r['win_rate']:>5.1f}%"
            f" 盈亏比{r['pl_ratio']:>5.2f} 剔Top3 {r['avg_ret_no_top3']:>7.3f}% 前后半 {r['first_half']:>6.3f}/{r['second_half']:>6.3f}%{flag}"
        )


if __name__ == "__main__":
    asyncio.run(main())
