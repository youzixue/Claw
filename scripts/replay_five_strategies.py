"""五策略历史回放引擎 (2026-08-31)

严谨性设计:
1. 信号层: 只用系统真实产生的信号快照 (promotion_prediction_record / dashboard_snapshot)
2. 交易层: 直接调用 paper.py 的 _short_sell_reason / _midline_sell_reason,
   与实盘卖出规则零偏差
3. 触发模拟: 信号日 → 次日开盘价买入 → 逐日按实盘卖出规则检查止盈/止损/时间止损
4. 绩效层: 复用 backtest.PerformanceAnalyzer (年化/回撤/夏普/胜率/盈亏比/交易分布)
5. 成本: 佣金万3(买卖) + 印花税千1(卖), 与实盘 _commission/_stamp_tax 一致

用法:
    python scripts/replay_five_strategies.py            # 全策略回放
    python scripts/replay_five_strategies.py --strategy B   # 单策略
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import paper as paper_api
from app.api.v1.paper import (
    _midline_sell_reason,
    _short_sell_reason,
    _strategy_sell_params_by_name,
)
from app.backtest.engine import PerformanceAnalyzer, TradeRecord, SignalType
from app.config.settings import settings
from app.core.data_date import resolve_latest_trade_date
from app.core.price_limit_rules import price_limit_rule
from app.core.stock_tagger import stock_tagger
from app.models.stock import StockKline


COMMISSION_RATE = 0.0003   # 万3 买卖双向
STAMP_TAX_RATE = 0.001     # 千1 仅卖出


@dataclass
class ReplayConfig:
    strategy: str            # A/B/C/D/E
    account_name: str
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_days: int
    min_probability: float   # B/C/D 用; A/E 忽略
    min_score: float = 0     # E 用
    buy_price_mode: str = "next_open"  # 次日开盘价买入
    initial_capital: float = 50_000.0
    position_pct: float = 0.15
    max_daily_buys: int = 2
    max_positions: int = 4


@dataclass
class ReplayTrade:
    code: str
    name: str
    signal_date: date
    buy_date: date
    buy_price: float
    sell_date: date
    sell_price: float
    shares: int
    profit_pct: float
    pnl: float
    hold_days: int
    sell_reason: str
    signal_meta: dict = field(default_factory=dict)


def _strategy_configs() -> dict[str, ReplayConfig]:
    # 回放默认值必须直接复用当前模拟盘配置；旧硬编码曾让 B/C 的买入日
    # 延后止损与生产参数不一致，即使后续卖出函数读取了 settings 也不是真同源。
    return {
        "A": ReplayConfig(
            "A", "default",
            settings.PAPER_AUTO_TAKE_PROFIT_PCT,
            settings.PAPER_AUTO_STOP_LOSS_PCT,
            settings.PAPER_AUTO_MAX_HOLD_DAYS,
            0,
            position_pct=settings.PAPER_AUTO_POSITION_PCT,
            max_daily_buys=settings.PAPER_AUTO_MAX_DAILY_NEW_BUYS,
            max_positions=settings.PAPER_AUTO_MAX_POSITIONS,
        ),
        "B": ReplayConfig(
            "B", "promotion",
            settings.PAPER_PROMOTION_TAKE_PROFIT_PCT,
            settings.PAPER_PROMOTION_STOP_LOSS_PCT,
            settings.PAPER_PROMOTION_MAX_HOLD_DAYS,
            settings.PAPER_PROMOTION_MIN_PROBABILITY,
            position_pct=settings.PAPER_PROMOTION_POSITION_PCT,
            max_daily_buys=settings.PAPER_PROMOTION_MAX_DAILY_BUYS,
            max_positions=settings.PAPER_PROMOTION_MAX_POSITIONS,
        ),
        "C": ReplayConfig(
            "C", "mainline",
            settings.PAPER_MAINLINE_TAKE_PROFIT_PCT,
            settings.PAPER_MAINLINE_STOP_LOSS_PCT,
            settings.PAPER_MAINLINE_MAX_HOLD_DAYS,
            settings.PAPER_MAINLINE_MIN_PROBABILITY,
            position_pct=settings.PAPER_MAINLINE_POSITION_PCT,
            max_daily_buys=settings.PAPER_MAINLINE_MAX_DAILY_BUYS,
            max_positions=settings.PAPER_MAINLINE_MAX_POSITIONS,
        ),
        "D": ReplayConfig(
            "D", "auction",
            settings.PAPER_AUCTION_TAKE_PROFIT_PCT,
            settings.PAPER_AUCTION_STOP_LOSS_PCT,
            settings.PAPER_AUCTION_MAX_HOLD_DAYS,
            settings.PAPER_AUCTION_MIN_PROBABILITY,
            position_pct=settings.PAPER_AUCTION_POSITION_PCT,
            max_daily_buys=settings.PAPER_AUCTION_MAX_DAILY_BUYS,
            max_positions=settings.PAPER_AUCTION_MAX_POSITIONS,
        ),
        "E": ReplayConfig(
            "E", "tenbagger",
            settings.PAPER_HIGHBOARD_TAKE_PROFIT_PCT,
            settings.PAPER_HIGHBOARD_STOP_LOSS_PCT,
            settings.PAPER_HIGHBOARD_MAX_HOLD_DAYS,
            0,
            min_score=0,
            position_pct=settings.PAPER_TENBAGGER_POSITION_PCT,
            max_daily_buys=settings.PAPER_TENBAGGER_MAX_DAILY_BUYS,
            max_positions=settings.PAPER_TENBAGGER_MAX_POSITIONS,
        ),
    }


# =========================================================================
# 信号加载
# =========================================================================

async def _load_promotion_signals(
    session: AsyncSession,
    route: str,
    target_board: int,
    min_probability: float,
) -> list[dict]:
    """从 promotion_prediction_record 加载真实信号 (schedule 快照, close_confirmed)."""
    rows = (
        await session.execute(
            select(text("code"), text("name"), text("prediction_trade_date"), text("calibrated_probability"))
            .select_from(text("promotion_prediction_record"))
            .where(
                text("candidate_route = :route"),
                text("target_board = :tb"),
                text("snapshot_source = 'schedule'"),
                text("signal_status = 'close_confirmed'"),
                text("calibrated_probability >= :p"),
            )
            .params(route=route, tb=target_board, p=min_probability)
            .order_by(text("prediction_trade_date"))
        )
    ).all()
    return [
        {
            "code": str(r.code),
            "name": str(r.name or r.code),
            "signal_date": date.fromisoformat(str(r.prediction_trade_date)),
            "probability": float(r.calibrated_probability or 0),
        }
        for r in rows
    ]


async def _load_plan_signals(session: AsyncSession) -> list[dict]:
    """从 dashboard_snapshot 加载明日预案的可执行买点 (策略A)."""
    from app.api.v1.paper import _plan_is_direct_buy_candidate

    rows = (
        await session.execute(
            text("SELECT payload_json, trade_date FROM dashboard_snapshot WHERE snapshot_key LIKE 'tenbagger-plan:%'")
        )
    ).all()
    signals: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        trade_date = row[1]
        if not trade_date:
            continue
        for plan in payload.get("plans") or []:
            try:
                if _plan_is_direct_buy_candidate(plan):
                    signals.append({
                        "code": str(plan.get("code") or ""),
                        "name": str(plan.get("name") or plan.get("code") or ""),
                        "signal_date": date.fromisoformat(str(trade_date)),
                        "probability": float(plan.get("plan_priority_score") or plan.get("bull_score") or 0),
                        "strategy_label": str(plan.get("strategy_label") or plan.get("action") or ""),
                    })
            except Exception:
                continue
    return signals


async def _load_tenbagger_signals(session: AsyncSession, min_score: float) -> list[dict]:
    """从 tenbagger-rank 快照加载十倍潜力候选 (策略E)."""
    rows = (
        await session.execute(
            text("SELECT payload_json, trade_date FROM dashboard_snapshot WHERE snapshot_key LIKE 'tenbagger-rank:tenbagger%'")
        )
    ).all()
    signals: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row[0])
        except Exception:
            continue
        trade_date = row[1]
        if not trade_date:
            continue
        for item in payload.get("rank") or []:
            score = float(item.get("total_score") or 0)
            if score >= min_score:
                signals.append({
                    "code": str(item.get("code") or ""),
                    "name": str(item.get("name") or item.get("code") or ""),
                    "signal_date": date.fromisoformat(str(trade_date)),
                    "probability": score,
                })
    return signals


async def _load_highboard_signals(session: AsyncSession) -> list[dict]:
    """从 limit_up_pool 加载连板≥4 高标候选 (策略E 2026-08-31 重建).

    与实盘 _tenbagger_midline_candidates 口径一致: 连板4-8 + 封板资金≥1亿
    + 炸板≤2次 + 未隔离 (2026-08-31 回放引擎对齐修复).
    """
    from app.api.v1.paper import settings as paper_settings

    min_cons = int(paper_settings.PAPER_HIGHBOARD_MIN_CONSECUTIVE)
    max_cons = int(paper_settings.PAPER_HIGHBOARD_MAX_CONSECUTIVE)
    min_seal = float(paper_settings.PAPER_HIGHBOARD_MIN_SEAL_AMOUNT) * 1e8
    max_break = int(paper_settings.PAPER_HIGHBOARD_MAX_BREAK_COUNT)
    rows = (
        await session.execute(
            text(
                "SELECT code, name, trade_date, consecutive_days, seal_amount, break_count, limit_up_reason "
                "FROM limit_up_pool "
                "WHERE consecutive_days >= :min_c AND consecutive_days <= :max_c "
                "AND seal_amount >= :min_seal AND break_count <= :max_break AND quarantined = 0 "
                "ORDER BY trade_date, consecutive_days DESC, seal_amount DESC, code"
            ).bindparams(min_c=min_cons, max_c=max_cons, min_seal=min_seal, max_break=max_break)
        )
    ).all()
    signals: list[dict] = []
    for r in rows:
        signals.append({
            "code": str(r.code),
            "name": str(r.name or r.code),
            "signal_date": date.fromisoformat(str(r.trade_date)),
            "consecutive_days": int(r.consecutive_days or 0),
            "seal_amount": float(r.seal_amount or 0),
            "break_count": int(r.break_count or 0),
            "limit_up_reason": str(r.limit_up_reason or ""),
        })
    return signals


# =========================================================================
# 交易模拟
# =========================================================================

async def _load_kline_map(session: AsyncSession, codes: list[str], start: date) -> dict[str, list[StockKline]]:
    """批量加载回放区间 K线 (按 code 分组升序)."""
    result = (
        await session.execute(
            select(StockKline)
            .where(StockKline.code.in_(codes), StockKline.trade_date >= start)
            .order_by(StockKline.code, StockKline.trade_date)
        )
    ).scalars().all()
    kline_map: dict[str, list[StockKline]] = {}
    for row in result:
        kline_map.setdefault(row.code, []).append(row)
    return kline_map


def _next_trade_day_idx(klines: list[StockKline], signal_date: date) -> int:
    """信号日之后第一个交易日 (严格 > signal_date, 防未来函数)."""
    for i, k in enumerate(klines):
        if k.trade_date > signal_date:
            return i
    return -1


def _simulate_hold(
    cfg: ReplayConfig,
    code: str,
    name: str,
    signal_date: date,
    klines: list[StockKline],
    sell_params: dict,
    signal_meta: dict,
) -> ReplayTrade | None:
    """信号日 → 次日开盘买入 → 逐日按实盘卖出规则持有.

    返回一笔完整的 ReplayTrade; 无次日K线或价格异常返回 None.
    """
    # 2026-08-31 修复: ST股不参与回放 (实盘 risk 层会拦截)
    if (
        not stock_tagger.is_tradeable(code)
        or "ST" in str(name or "").upper()
        or "退" in str(name or "")
    ):
        return None
    idx = _next_trade_day_idx(klines, signal_date)
    if idx < 0 or idx >= len(klines):
        return None
    buy_k = klines[idx]
    buy_price = float(buy_k.open or buy_k.close or 0)
    if buy_price <= 0:
        return None
    # 任一策略次日以涨停价开盘都不能按该开盘价虚构成交；即使稍后开板，
    # next_open 回放也没有可证明的后续委托时点。昨收优先使用交易所口径
    # prev_close，缺失时才回退前一根收盘。
    if idx >= 1:
        prev_close = float(
            getattr(buy_k, "prev_close", 0)
            or getattr(klines[idx - 1], "close", 0)
            or 0
        )
        limit_rule = price_limit_rule(code, trade_date=buy_k.trade_date)
        limit_price = round(
            prev_close * (1 + limit_rule.nominal_limit_pct / 100.0),
            2,
        ) if prev_close > 0 else 0.0
        if limit_price > 0 and buy_price >= limit_price * 0.998:
            return None

    position_cash = max(cfg.initial_capital * cfg.position_pct * 0.98, 0.0)
    shares = int(position_cash // (buy_price * 100)) * 100
    if shares < 100:
        return None
    buy_cost = buy_price * shares
    buy_commission = round(buy_cost * COMMISSION_RATE, 2)
    stop_loss_price = round(buy_price * (1 - cfg.stop_loss_pct / 100), 2)

    # 模拟持仓: 从买入日逐日检查
    max_profit_high = 0.0
    deferred_t1_stop = False
    for day_offset in range(0, cfg.max_hold_days + 1):
        day_idx = idx + day_offset
        if day_idx >= len(klines):
            break
        k = klines[day_idx]
        hold_days = day_offset
        price = float(k.close or 0)
        if price <= 0:
            continue
        profit_pct = round((price / buy_price - 1) * 100, 2)
        # 当日最高浮盈 (用 high 评估盘中触发)
        if k.high:
            day_high_profit = round((float(k.high) / buy_price - 1) * 100, 2)
            max_profit_high = max(max_profit_high, day_high_profit)

        # T+1: 买入日只能记录止损穿越，绝不能回填为当日成交；下一交易日
        # 才按开盘价尝试退出。日K无法证明跌停排队成交，报告中保留该限制。
        if day_offset == 0:
            if profit_pct <= -cfg.stop_loss_pct:
                deferred_t1_stop = True
            continue

        if deferred_t1_stop:
            sell_price = float(k.open or k.close or 0)
            if sell_price <= 0:
                continue
            exit_profit_pct = round((sell_price / buy_price - 1) * 100, 2)
            amount_value = sell_price * shares
            sell_commission = round(amount_value * COMMISSION_RATE, 2)
            stamp_tax = round(amount_value * STAMP_TAX_RATE, 2)
            pnl = round(
                (sell_price - buy_price) * shares
                - buy_commission
                - sell_commission
                - stamp_tax,
                2,
            )
            return ReplayTrade(
                code=code,
                name=name,
                signal_date=signal_date,
                buy_date=buy_k.trade_date,
                buy_price=round(buy_price, 2),
                sell_date=k.trade_date,
                sell_price=round(sell_price, 2),
                shares=shares,
                profit_pct=exit_profit_pct,
                pnl=pnl,
                hold_days=1,
                sell_reason="买入日止损穿越，T+1次日开盘退出（日K无法证明跌停队列成交）",
                signal_meta=signal_meta,
            )

        # 构建实盘卖出上下文 (日线口径近似盘中)
        # 2026-08-31 晚修复: 补 ma5/ma10/ma20 (实盘 _build_short_sell_context 提供,
        # 此前回放 ctx 缺均线 → 跌破MA20规则在回放中从未生效, 与实盘零偏差对齐)
        closes_full = [float(x.close or 0) for x in klines[: day_idx + 1]]
        ma5 = round(sum(closes_full[-5:]) / 5, 4) if len(closes_full) >= 5 else None
        ma10 = round(sum(closes_full[-10:]) / 10, 4) if len(closes_full) >= 10 else None
        ma20 = round(sum(closes_full[-20:]) / 20, 4) if len(closes_full) >= 20 else None
        ctx = {
            "price": price,
            "open": float(k.open or 0),
            "high": float(k.high or 0),
            "low": float(k.low or 0),
            "change_pct": float(k.change_pct or 0),
            "volume_ratio": None,          # 日线无法复现, 传 None 让规则跳过
            "avg_price": None,             # 同上
            "min5_change": None,
            "orderbook_imbalance": None,
            "stop_loss_price": stop_loss_price,
            "prev_was_limit_up": False,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
        }

        if cfg.strategy == "E":
            reason = _midline_sell_reason(
                _FakePosition(buy_price),
                ctx,
                profit_pct,
                hold_days,
                params={
                    "take_profit_pct": cfg.take_profit_pct,
                    "stop_loss_pct": cfg.stop_loss_pct,
                    "max_hold_days": cfg.max_hold_days,
                },
            )
        else:
            reason = _short_sell_reason(
                _FakePosition(buy_price),
                ctx,
                profit_pct,
                hold_days,
                k.trade_date,
                now=datetime.combine(k.trade_date, time(15, 0)),
                params=sell_params,
            )
        if reason:
            sell_price = price
            # 止盈/冲高回落按实盘可部分卖出, 但日线回放取整笔 (保守口径)
            amount_value = sell_price * shares
            sell_commission = round(amount_value * COMMISSION_RATE, 2)
            stamp_tax = round(amount_value * STAMP_TAX_RATE, 2)
            pnl = round((sell_price - buy_price) * shares - buy_commission - sell_commission - stamp_tax, 2)
            return ReplayTrade(
                code=code, name=name,
                signal_date=signal_date, buy_date=buy_k.trade_date, buy_price=round(buy_price, 2),
                sell_date=k.trade_date, sell_price=round(sell_price, 2),
                shares=shares, profit_pct=profit_pct, pnl=pnl,
                hold_days=hold_days, sell_reason=reason,
                signal_meta=signal_meta,
            )
        # 收盘跌破止损价 (硬止损日线兜底)
        if profit_pct <= -cfg.stop_loss_pct:
            sell_price = price
            amount_value = sell_price * shares
            sell_commission = round(amount_value * COMMISSION_RATE, 2)
            stamp_tax = round(amount_value * STAMP_TAX_RATE, 2)
            pnl = round((sell_price - buy_price) * shares - buy_commission - sell_commission - stamp_tax, 2)
            return ReplayTrade(
                code=code, name=name,
                signal_date=signal_date, buy_date=buy_k.trade_date, buy_price=round(buy_price, 2),
                sell_date=k.trade_date, sell_price=round(sell_price, 2),
                shares=shares, profit_pct=profit_pct, pnl=pnl,
                hold_days=hold_days, sell_reason=f"收盘跌破硬止损：{profit_pct:.2f}%",
                signal_meta=signal_meta,
            )

        if day_offset >= cfg.max_hold_days:
            sell_price = price
            amount_value = sell_price * shares
            sell_commission = round(amount_value * COMMISSION_RATE, 2)
            stamp_tax = round(amount_value * STAMP_TAX_RATE, 2)
            pnl = round(
                (sell_price - buy_price) * shares
                - buy_commission
                - sell_commission
                - stamp_tax,
                2,
            )
            return ReplayTrade(
                code=code,
                name=name,
                signal_date=signal_date,
                buy_date=buy_k.trade_date,
                buy_price=round(buy_price, 2),
                sell_date=k.trade_date,
                sell_price=round(sell_price, 2),
                shares=shares,
                profit_pct=profit_pct,
                pnl=pnl,
                hold_days=hold_days,
                sell_reason=f"回放持仓上限{cfg.max_hold_days}个交易日到期平仓",
                signal_meta=signal_meta,
            )

    # 数据截止早于完整持有窗口时右删失，不把最后一根K线伪装成策略平仓。
    return None


class _FakePosition:
    """极简 PaperPosition 替身: 只暴露 _short_sell_reason/_midline_sell_reason 需要的字段."""
    def __init__(self, buy_price: float):
        self.buy_price = buy_price
        self.stop_loss_price = None
        self.code = ""


# =========================================================================
# 回放主流程
# =========================================================================

def _aggregate(trades: list[ReplayTrade]) -> dict:
    if not trades:
        return {"count": 0}
    pnls = [t.pnl for t in trades]
    pcts = [t.profit_pct for t in trades]
    hold_days = [t.hold_days for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    profits = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p <= 0]
    avg_profit = sum(profits) / len(profits) if profits else 0
    avg_loss = sum(losses) / len(losses) if losses else 1
    reasons = Counter(t.sell_reason for t in trades)
    hold_dist = Counter(t.hold_days for t in trades)
    return {
        "count": len(trades),
        "win_rate": round(wins / len(trades) * 100, 1),
        "avg_pnl": round(sum(pnls) / len(pnls), 2),
        "avg_profit_pct": round(sum(pcts) / len(pcts), 2),
        "total_pnl": round(sum(pnls), 2),
        "profit_loss_ratio": round(avg_profit / avg_loss, 2) if avg_loss > 0 else 0,
        "avg_hold_days": round(sum(hold_days) / len(hold_days), 1),
        "median_hold_days": sorted(hold_days)[len(hold_days) // 2],
        "max_single_loss": round(min(pnls), 2),
        "sell_reasons": dict(reasons.most_common(10)),
        "hold_days_dist": dict(sorted(hold_dist.items())),
    }


def _apply_portfolio_constraints(
    trades: list[ReplayTrade],
    cfg: ReplayConfig,
) -> tuple[list[ReplayTrade], int]:
    """Apply the same account-level scarcity that independent trades otherwise hide."""

    ranked = sorted(
        trades,
        key=lambda item: (
            item.buy_date,
            -float(
                item.signal_meta.get("probability")
                or item.signal_meta.get("consecutive_days")
                or 0
            ),
            item.code,
        ),
    )
    selected: list[ReplayTrade] = []
    daily_buys: Counter[date] = Counter()
    blocked = 0
    for trade in ranked:
        active = [
            item
            for item in selected
            if item.buy_date <= trade.buy_date <= item.sell_date
        ]
        if daily_buys[trade.buy_date] >= max(int(cfg.max_daily_buys), 1):
            blocked += 1
            continue
        if len(active) >= max(int(cfg.max_positions), 1):
            blocked += 1
            continue
        if any(item.code == trade.code for item in active):
            blocked += 1
            continue
        selected.append(trade)
        daily_buys[trade.buy_date] += 1
    return selected, blocked


async def replay_strategy(session: AsyncSession, key: str, config_override: dict | None = None) -> dict:
    cfg = _strategy_configs()[key]
    if config_override:
        for k, v in config_override.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    sell_params = _strategy_sell_params_by_name(cfg.account_name)
    # 参数覆盖: cfg 的止盈/止损/持仓覆盖必须同步注入卖出规则参数, 否则 _short_sell_reason 仍用 settings 默认
    if config_override:
        if "take_profit_pct" in config_override:
            sell_params["take_profit_pct"] = cfg.take_profit_pct
        if "stop_loss_pct" in config_override:
            sell_params["stop_loss_pct"] = cfg.stop_loss_pct
            # 策略B/C/D 的小止损=硬止损, 一并覆盖避免噪音提前离场
            if sell_params.get("small_stop_loss_pct") is not None:
                sell_params["small_stop_loss_pct"] = cfg.stop_loss_pct
        if "max_hold_days" in config_override:
            sell_params["max_hold_days"] = cfg.max_hold_days

    # 1. 加载信号
    if key in {"B", "C", "D"}:
        route_map = {
            "B": ("second_board_promotion", 2),
            "C": ("mainline_spread_start", 1),
            "D": ("auction_surge_start", 1),
        }
        route, tb = route_map[key]
        signals = await _load_promotion_signals(session, route, tb, cfg.min_probability)
    elif key == "A":
        signals = await _load_plan_signals(session)
    else:  # E: 连板高标接力 (2026-08-31 重建, 从涨停池连板≥4)
        signals = await _load_highboard_signals(session)

    if not signals:
        return {"key": key, "config": cfg, "signals": 0, "trades": [], "stats": _aggregate([]), "error": "无有效信号"}

    # 2. 加载 K线 (信号日往前推 30 天保证 MA20 可用)
    codes = list(dict.fromkeys(s["code"] for s in signals if s.get("code")))
    min_signal = min(s["signal_date"] for s in signals)
    kline_start = min_signal - timedelta(days=40)
    kline_map = await _load_kline_map(session, codes, kline_start)

    # 3. 逐信号模拟
    trades: list[ReplayTrade] = []
    skipped = 0
    for sig in signals:
        code = sig.get("code") or ""
        if code not in kline_map:
            skipped += 1
            continue
        trade = _simulate_hold(
            cfg, code, sig.get("name") or code, sig["signal_date"],
            kline_map[code], sell_params,
            {k: v for k, v in sig.items() if k not in ("code", "name", "signal_date")},
        )
        if trade:
            trades.append(trade)
        else:
            skipped += 1

    simulated_count = len(trades)
    trades, portfolio_blocked = _apply_portfolio_constraints(trades, cfg)
    skipped += portfolio_blocked
    stats = _aggregate(trades)
    return {
        "key": key,
        "config": cfg,
        "signals": len(signals),
        "executed": len(trades),
        "independent_trade_candidates": simulated_count,
        "portfolio_blocked": portfolio_blocked,
        "skipped": skipped,
        "stats": stats,
        "trades": trades,
    }


def _trades_to_records(trades: list[ReplayTrade]) -> list[TradeRecord]:
    records = []
    for t in trades:
        records.append(TradeRecord(
            code=t.code, signal_type=SignalType.BUY, trade_date=t.buy_date,
            price=t.buy_price, shares=t.shares, amount=t.buy_price * t.shares,
        ))
        records.append(TradeRecord(
            code=t.code, signal_type=SignalType.SELL, trade_date=t.sell_date,
            price=t.sell_price, shares=t.shares, amount=t.sell_price * t.shares,
            net_amount=t.pnl,
        ))
    return records


def _build_nav_curve(trades: list[ReplayTrade], initial_capital: float = 50_000.0) -> pd.DataFrame:
    """按交易日构建简单净值曲线 (现金+持仓市值近似)."""
    if not trades:
        return pd.DataFrame()
    days = sorted({t.buy_date for t in trades} | {t.sell_date for t in trades})
    cash = initial_capital
    holdings: dict[str, tuple[int, float]] = {}
    rows = []
    for day in days:
        for t in trades:
            if t.buy_date == day:
                cash -= t.buy_price * t.shares
                holdings[t.code] = (t.shares, t.buy_price)
            if t.sell_date == day:
                cash += t.sell_price * t.shares
                holdings.pop(t.code, None)
        mkt_value = sum(shares * price for _, (shares, price) in holdings.items())
        rows.append({"date": day, "total_value": cash + mkt_value})
    return pd.DataFrame(rows)


async def run_all(session: AsyncSession) -> dict:
    results = {}
    for key in ["A", "B", "C", "D", "E"]:
        print(f"回放策略 {key} ...", flush=True)
        results[key] = await replay_strategy(session, key)
    return results


def _print_summary(results: dict) -> None:
    print("\n" + "=" * 90)
    print("五策略历史回放摘要 (日线级近似, 信号=真实快照, 卖出=实盘同源规则)")
    print("=" * 90)
    header = f"{'策略':<4}{'信号':>6}{'成交':>6}{'胜率%':>8}{'平均%':>8}{'盈亏比':>8}{'均持仓':>8}{'总盈亏':>10}{'最大单亏':>10}"
    print(header)
    print("-" * 90)
    for key in results:
        r = results[key]
        st = r["stats"]
        if r.get("error"):
            print(f"{key:<4} 错误: {r['error']}")
            continue
        print(
            f"{key:<4}{r['signals']:>6}{r['executed']:>6}"
            f"{st['win_rate']:>8}{st['avg_profit_pct']:>8}{st['profit_loss_ratio']:>8}"
            f"{st['avg_hold_days']:>8}{st['total_pnl']:>10.2f}{st['max_single_loss']:>10.2f}"
        )
    print("=" * 90)


def _write_report(results: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    for key, r in results.items():
        st = r["stats"]
        lines = [
            f"# 策略{key} 历史回放报告",
            f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"> 口径: 日线级近似 | 信号=真实快照 | 卖出=实盘同源规则",
            f"> 配置: 止盈{r['config'].take_profit_pct}% / 止损{r['config'].stop_loss_pct}% / 持仓≤{r['config'].max_hold_days}天",
            "",
            "## 一、总体绩效",
            "",
            "| 指标 | 值 |",
            "|---|---|",
            f"| 信号数 | {r['signals']} |",
            f"| 实际成交 | {r['executed']} |",
            f"| 胜率 | {st['win_rate']}% |",
            f"| 平均收益率 | {st['avg_profit_pct']}% |",
            f"| 盈亏比 | {st['profit_loss_ratio']} |",
            f"| 平均持仓天数 | {st['avg_hold_days']} |",
            f"| 总盈亏 | {st['total_pnl']} 元 |",
            f"| 最大单笔亏损 | {st['max_single_loss']} 元 |",
            "",
            "## 二、卖出原因分布",
            "",
            "| 卖出原因 | 次数 |",
            "|---|---|",
        ]
        for reason, cnt in st["sell_reasons"].items():
            lines.append(f"| {reason} | {cnt} |")
        lines += [
            "",
            "## 三、持仓天数分布",
            "",
            "| 持仓天数 | 次数 |",
            "|---|---|",
        ]
        for d, cnt in st["hold_days_dist"].items():
            lines.append(f"| {d} 天 | {cnt} |")
        lines += [
            "",
            "## 四、逐笔交易明细",
            "",
            "| 代码 | 名称 | 信号日 | 买入日 | 买入价 | 卖出日 | 卖出价 | 盈亏% | 盈亏(元) | 持仓 | 卖出原因 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for t in r["trades"]:
            lines.append(
                f"| {t.code} | {t.name} | {t.signal_date} | {t.buy_date} | {t.buy_price} "
                f"| {t.sell_date} | {t.sell_price} | {t.profit_pct:.2f}% | {t.pnl:.2f} "
                f"| {t.hold_days}天 | {t.sell_reason} |"
            )
        out_path = out_dir / f"replay_strategy_{key}_{ts}.md"
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"已写入 {out_path}")


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="五策略历史回放")
    parser.add_argument("--strategy", choices=["A", "B", "C", "D", "E", "ALL"], default="ALL")
    parser.add_argument("--out", default=str(ROOT / "outputs"))
    args = parser.parse_args()

    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            if args.strategy == "ALL":
                results = await run_all(session)
            else:
                results = {args.strategy: await replay_strategy(session, args.strategy)}
    finally:
        await engine.dispose()

    _print_summary(results)
    _write_report(results, Path(args.out))


if __name__ == "__main__":
    asyncio.run(main())
