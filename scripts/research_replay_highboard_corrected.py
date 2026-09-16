"""Research-only audit replay for strategy E (high-board relay).

This does not change production strategy code.  It corrects four material
validation gaps in ``replay_five_strategies.py``:

1. only project-tradeable main-board symbols are eligible;
2. position size is the production 15% budget, rounded to 100-share lots;
3. at most one entry per day and three concurrent positions are accepted;
4. T+1 and right-censoring are enforced (no same-day exit or forced exit at
   the end of the available sample).

The execution model is still daily-bar approximate.  An open limit-up that
trades below the limit later that day is treated as buyable at the limit;
a full-day one-price limit-up is skipped.
"""
from __future__ import annotations

import asyncio
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.v1.paper import _midline_sell_reason  # noqa: E402
from app.config.settings import settings  # noqa: E402


INITIAL_CAPITAL = 50_000.0
POSITION_PCT = float(settings.PAPER_TENBAGGER_POSITION_PCT)
MAX_DAILY_BUYS = int(settings.PAPER_TENBAGGER_MAX_DAILY_BUYS)
MAX_POSITIONS = int(settings.PAPER_TENBAGGER_MAX_POSITIONS)
TAKE_PROFIT_PCT = float(settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT)
STOP_LOSS_PCT = float(settings.PAPER_HIGHBOARD_STOP_LOSS_PCT)
MAX_HOLD_DAYS = int(settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS)
COMMISSION_RATE = 0.0003
STAMP_TAX_RATE = 0.001


@dataclass
class Bar:
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    change_pct: float


@dataclass
class Trade:
    code: str
    name: str
    signal_date: date
    buy_date: date
    buy_price: float
    sell_date: date
    sell_price: float
    shares: int
    return_pct: float
    pnl: float
    hold_days: int
    reason: str


class _FakePosition:
    def __init__(self, buy_price: float, code: str):
        self.buy_price = buy_price
        self.stop_loss_price = None
        self.code = code


def _is_mainboard_code(code: str) -> bool:
    return code.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def _limit_pct(code: str) -> float:
    # This audit only admits main-board codes.  ST is excluded separately.
    return 0.10


def _next_idx(bars: list[Bar], signal_date: date) -> int:
    for idx, bar in enumerate(bars):
        if bar.trade_date > signal_date:
            return idx
    return -1


def _ma(bars: list[Bar], end_idx: int, window: int) -> float | None:
    values = [bar.close for bar in bars[: end_idx + 1] if bar.close > 0]
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _simulate(signal: dict, bars: list[Bar]) -> tuple[Trade | None, str]:
    idx = _next_idx(bars, signal["signal_date"])
    if idx < 1:
        return None, "no_next_bar"
    buy_bar = bars[idx]
    prev_close = bars[idx - 1].close
    limit_price = round(prev_close * (1 + _limit_pct(signal["code"])), 2)

    # Full-day one-price board: no executable offer.  If it opens later, the
    # waiting order is approximated as filled at the limit price.
    at_limit_open = buy_bar.open >= limit_price - 0.01
    full_day_one_price = at_limit_open and buy_bar.low >= limit_price - 0.01
    if full_day_one_price:
        return None, "one_price_limit_up"
    buy_price = limit_price if at_limit_open else buy_bar.open
    if buy_price <= 0:
        return None, "bad_buy_price"

    budget = INITIAL_CAPITAL * POSITION_PCT * 0.98
    shares = int(math.floor(budget / buy_price / 100.0) * 100)
    if shares < 100:
        return None, "one_lot_unaffordable"

    stop_price = round(buy_price * (1 - STOP_LOSS_PCT / 100), 2)
    buy_day_breached = (buy_bar.close / buy_price - 1) * 100 <= -STOP_LOSS_PCT

    # A complete three-session observation window is required unless an exit
    # fires earlier.  This prevents the last database day being mislabeled as
    # a normal time exit.
    for offset in range(1, MAX_HOLD_DAYS + 1):
        day_idx = idx + offset
        if day_idx >= len(bars):
            return None, "right_censored"
        bar = bars[day_idx]
        if buy_day_breached and offset == 1:
            sell_price = bar.open
            reason = "T+1 deferred hard stop"
        else:
            return_pct = (bar.close / buy_price - 1) * 100
            ctx = {
                "price": bar.close,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "change_pct": bar.change_pct,
                "volume_ratio": None,
                "avg_price": None,
                "min5_change": None,
                "orderbook_imbalance": None,
                "stop_loss_price": stop_price,
                "prev_was_limit_up": False,
                "ma5": _ma(bars, day_idx, 5),
                "ma10": _ma(bars, day_idx, 10),
                "ma20": _ma(bars, day_idx, 20),
            }
            reason = _midline_sell_reason(
                _FakePosition(buy_price, signal["code"]),
                ctx,
                return_pct,
                offset,
                params={
                    "take_profit_pct": TAKE_PROFIT_PCT,
                    "stop_loss_pct": STOP_LOSS_PCT,
                    "max_hold_days": MAX_HOLD_DAYS,
                },
            )
            if not reason and return_pct <= -STOP_LOSS_PCT:
                reason = f"close hard stop {return_pct:.2f}%"
            if not reason and offset == MAX_HOLD_DAYS:
                reason = f"time exit {MAX_HOLD_DAYS} sessions"
            if not reason:
                continue
            sell_price = bar.close

        gross_buy = buy_price * shares
        gross_sell = sell_price * shares
        fees = round(gross_buy * COMMISSION_RATE, 2) + round(gross_sell * COMMISSION_RATE, 2)
        fees += round(gross_sell * STAMP_TAX_RATE, 2)
        pnl = round(gross_sell - gross_buy - fees, 2)
        return_pct = round((sell_price / buy_price - 1) * 100, 2)
        return Trade(
            code=signal["code"],
            name=signal["name"],
            signal_date=signal["signal_date"],
            buy_date=buy_bar.trade_date,
            buy_price=round(buy_price, 2),
            sell_date=bar.trade_date,
            sell_price=round(sell_price, 2),
            shares=shares,
            return_pct=return_pct,
            pnl=pnl,
            hold_days=offset,
            reason=str(reason),
        ), "executed"

    return None, "right_censored"


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT p.code,p.name,p.trade_date,p.consecutive_days,p.seal_amount,p.break_count,"
                    "COALESCE(t.board_tag,'tradeable') AS board_tag,COALESCE(t.is_st,0) AS is_st,"
                    "COALESCE(t.is_suspended,0) AS is_suspended,COALESCE(t.is_delisting,0) AS is_delisting "
                    "FROM limit_up_pool p LEFT JOIN stock_tags t ON t.code=p.code "
                    "WHERE p.consecutive_days BETWEEN :min_c AND :max_c "
                    "AND p.seal_amount>=:min_seal AND p.break_count<=:max_break "
                    "AND COALESCE(p.quarantined,0)=0 "
                    "ORDER BY p.trade_date,p.consecutive_days DESC,p.seal_amount DESC"
                ),
                {
                    "min_c": int(settings.PAPER_HIGHBOARD_MIN_CONSECUTIVE),
                    "max_c": int(settings.PAPER_HIGHBOARD_MAX_CONSECUTIVE),
                    "min_seal": float(settings.PAPER_HIGHBOARD_MIN_SEAL_AMOUNT) * 1e8,
                    "max_break": int(settings.PAPER_HIGHBOARD_MAX_BREAK_COUNT),
                },
            )
        ).mappings().all()
        signals = []
        rejected = Counter()
        for row in rows:
            code = str(row["code"] or "")
            name = str(row["name"] or code)
            if not _is_mainboard_code(code) or row["board_tag"] != "tradeable":
                rejected["observe_or_non_mainboard"] += 1
                continue
            if bool(row["is_st"]) or bool(row["is_suspended"]) or bool(row["is_delisting"]) or "ST" in name.upper():
                rejected["blocked_tag"] += 1
                continue
            signals.append(
                {
                    "code": code,
                    "name": name,
                    "signal_date": date.fromisoformat(str(row["trade_date"])),
                    "consecutive_days": int(row["consecutive_days"] or 0),
                    "seal_amount": float(row["seal_amount"] or 0),
                }
            )

        codes = sorted({s["code"] for s in signals})
        start = min(s["signal_date"] for s in signals) - timedelta(days=40)
        krows = (
            await session.execute(
                text(
                    "SELECT code,trade_date,open,high,low,close,change_pct FROM stock_kline "
                    "WHERE code IN (%s) AND trade_date>=:start ORDER BY code,trade_date"
                    % ",".join(f":c{i}" for i in range(len(codes)))
                ),
                {"start": start.isoformat(), **{f"c{i}": code for i, code in enumerate(codes)}},
            )
        ).mappings().all()
    await engine.dispose()

    bars_by_code: dict[str, list[Bar]] = defaultdict(list)
    for row in krows:
        bars_by_code[str(row["code"])].append(
            Bar(
                trade_date=date.fromisoformat(str(row["trade_date"])),
                open=float(row["open"] or 0),
                high=float(row["high"] or 0),
                low=float(row["low"] or 0),
                close=float(row["close"] or 0),
                change_pct=float(row["change_pct"] or 0),
            )
        )

    by_signal_date: dict[date, list[dict]] = defaultdict(list)
    for signal in signals:
        by_signal_date[signal["signal_date"]].append(signal)

    candidate_trades: list[Trade] = []
    for signal_date in sorted(by_signal_date):
        day_signals = sorted(
            by_signal_date[signal_date],
            key=lambda item: (-item["consecutive_days"], -item["seal_amount"], item["code"]),
        )
        accepted_today = 0
        for signal in day_signals:
            trade, status = _simulate(signal, bars_by_code.get(signal["code"], []))
            if trade is None:
                rejected[status] += 1
                continue
            if accepted_today >= MAX_DAILY_BUYS:
                rejected["daily_buy_cap"] += 1
                continue
            candidate_trades.append(trade)
            accepted_today += 1

    # Enforce the concurrent-position cap after candidate execution paths are known.
    trades: list[Trade] = []
    for trade in sorted(candidate_trades, key=lambda item: (item.buy_date, item.code)):
        active = [old for old in trades if old.buy_date <= trade.buy_date <= old.sell_date]
        if any(old.code == trade.code for old in active):
            rejected["existing_position"] += 1
            continue
        concurrent = len(active)
        if concurrent >= MAX_POSITIONS:
            rejected["position_cap"] += 1
            continue
        trades.append(trade)

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    avg_return = sum(t.return_pct for t in trades) / len(trades) if trades else 0
    total_pnl = sum(t.pnl for t in trades)
    lines = [
        "# 策略E校正后研究回放（非生产）",
        "",
        "> 日线近似；固定现行参数，不重新寻优。",
        "> 校正：主板可交易过滤、15%预算/100股整手、每日1笔/最多3仓、T+1、右删失。",
        "> 剩余限制：开板成交由日线最低价近似，无法还原排队/可成交量；StockTag 是当前快照而非历史时点标签。",
        "> 因此本报告只能用于发现旧回放偏差和形成 challenger，不能作为提高生产风险暴露的依据。",
        "",
        "## 摘要",
        "",
        f"- 原始质量信号：{len(rows)}",
        f"- 交易域内信号：{len(signals)}",
        f"- 校正后可评估成交：{len(trades)}",
        f"- 胜率：{(len(wins)/len(trades)*100 if trades else 0):.1f}%",
        f"- 平均收益率：{avg_return:.2f}%",
        f"- 总盈亏（50,000元账户、每笔15%预算近似）：{total_pnl:.2f}元",
        f"- 平均单笔盈亏：{(total_pnl/len(trades) if trades else 0):.2f}元",
        f"- 排除/跳过：{dict(rejected)}",
        "",
        "## 逐笔",
        "",
        "|代码|名称|信号日|买入日|买入价|股数|卖出日|卖出价|收益%|盈亏元|原因|",
        "|---|---|---|---|---:|---:|---|---:|---:|---:|---|",
    ]
    for t in trades:
        lines.append(
            f"|{t.code}|{t.name}|{t.signal_date}|{t.buy_date}|{t.buy_price:.2f}|{t.shares}|"
            f"{t.sell_date}|{t.sell_price:.2f}|{t.return_pct:.2f}|{t.pnl:.2f}|{t.reason}|"
        )
    out = ROOT / "outputs" / "daily_review_20260901" / "策略E校正研究回放_20260901.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:18]))
    print(f"written: {out}")


if __name__ == "__main__":
    asyncio.run(main())
