"""回测引擎 — 基于事件驱动的轻量级回测框架

支持:
- 信号回测(给研究样本算收益)
- 策略回测(因子组合→交易信号→收益曲线)
- 绩效分析(年化/夏普/最大回撤/胜率/盈亏比)
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Optional

import numpy as np
import pandas as pd
from loguru import logger


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Return a finite float for optional market fields."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if np.isfinite(result) else default


def _normalize_trade_date(value: Any) -> date:
    """Normalize pandas/datetime/date values to date for reliable matching."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        return value.date()
    return date.fromisoformat(str(value)[:10])


# ========== 数据结构 ==========

class SignalType(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class TradeRecord:
    """交易记录"""
    code: str
    signal_type: SignalType
    trade_date: date
    price: float
    shares: int = 0
    amount: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    net_amount: float = 0.0


@dataclass
class BacktestPosition:
    """回测持仓"""
    code: str
    shares: int = 0
    avg_cost: float = 0.0
    current_price: float = 0.0
    market_value: float = 0.0
    profit_loss: float = 0.0
    profit_pct: float = 0.0

    def update_price(self, price: float):
        self.current_price = price
        self.market_value = self.shares * price
        if self.avg_cost > 0:
            self.profit_loss = self.market_value - self.shares * self.avg_cost
            self.profit_pct = (price / self.avg_cost - 1) * 100


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_capital: float = 1_000_000       # 初始资金
    commission_rate: float = 0.0003           # 佣金万三
    stamp_tax_rate: float = 0.001             # 印花税千一(卖出)
    slippage_pct: float = 0.1                 # 滑点0.1%
    volume_limit_pct: float = 10.0            # 单笔成交量上限%
    avoid_limit_up_down: bool = True          # 涨跌停不可成交
    max_position_pct: float = 30.0            # 单股最大仓位%
    max_positions: int = 5                    # 最大持仓数
    stop_loss_pct: float = 7.0                # 止损%
    take_profit_pct: float = 30.0             # 止盈%
    min_score_to_buy: int = 70                # 买入最低评分


# ========== 绩效分析 ==========

class PerformanceAnalyzer:
    """绩效分析器"""

    @staticmethod
    def calc_nav_curve(trades: list[TradeRecord], daily_values: list[dict],
                       initial_capital: float) -> pd.DataFrame:
        """计算净值曲线"""
        if not daily_values:
            return pd.DataFrame()

        df = pd.DataFrame(daily_values)
        df["nav"] = df["total_value"] / initial_capital
        return df

    @staticmethod
    def calc_metrics(daily_values: list[dict], initial_capital: float,
                     trades: list[TradeRecord]) -> dict:
        """计算回测绩效指标"""
        if not daily_values:
            return {}

        df = pd.DataFrame(daily_values)
        total_values = df["total_value"].values

        # 总收益率
        total_return = (total_values[-1] / initial_capital - 1) * 100

        # 年化收益率
        trading_days = len(total_values)
        annual_return = ((total_values[-1] / initial_capital) ** (252 / max(trading_days, 1)) - 1) * 100

        # 最大回撤
        peak = np.maximum.accumulate(total_values)
        drawdown = (total_values - peak) / peak * 100
        max_drawdown = float(drawdown.min())

        # 最大回撤持续天数
        in_drawdown = drawdown < 0
        max_dd_duration = 0
        current_dd = 0
        for dd in in_drawdown:
            if dd:
                current_dd += 1
                max_dd_duration = max(max_dd_duration, current_dd)
            else:
                current_dd = 0

        # 日收益率
        daily_returns = df["total_value"].pct_change().dropna().values

        # 夏普比率(无风险利率3%)
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() - 0.03 / 252) / daily_returns.std() * np.sqrt(252)
        else:
            sharpe = 0

        # 胜率
        buy_trades = [t for t in trades if t.signal_type == SignalType.BUY]
        sell_trades = [t for t in trades if t.signal_type == SignalType.SELL]
        winning = sum(1 for t in sell_trades if t.net_amount > 0)
        win_rate = (winning / len(sell_trades) * 100) if sell_trades else 0

        # 盈亏比
        profits = [t.net_amount for t in sell_trades if t.net_amount > 0]
        losses = [abs(t.net_amount) for t in sell_trades if t.net_amount < 0]
        avg_profit = np.mean(profits) if profits else 0
        avg_loss = np.mean(losses) if losses else 1
        profit_loss_ratio = avg_profit / avg_loss if avg_loss > 0 else 0

        # 交易次数
        total_trades = len(buy_trades) + len(sell_trades)
        turnover = 0.0
        if initial_capital:
            turnover = sum(abs(t.amount) for t in trades) / initial_capital * 100

        monthly_returns = PerformanceAnalyzer.calc_monthly_returns(daily_values)
        drawdown_curve = PerformanceAnalyzer.calc_drawdown_curve(daily_values)
        trade_distribution = PerformanceAnalyzer.calc_trade_distribution(trades)

        return {
            "total_return_pct": round(total_return, 2),
            "annual_return_pct": round(annual_return, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "max_dd_duration_days": max_dd_duration,
            "sharpe_ratio": round(sharpe, 2),
            "win_rate_pct": round(win_rate, 2),
            "profit_loss_ratio": round(profit_loss_ratio, 2),
            "total_trades": total_trades,
            "buy_count": len(buy_trades),
            "sell_count": len(sell_trades),
            "turnover_pct": round(turnover, 2),
            "monthly_returns": monthly_returns,
            "drawdown_curve": drawdown_curve,
            "trade_distribution": trade_distribution,
            "initial_capital": initial_capital,
            "final_capital": round(total_values[-1], 2),
        }

    @staticmethod
    def calc_monthly_returns(daily_values: list[dict]) -> list[dict]:
        if not daily_values:
            return []
        df = pd.DataFrame(daily_values)
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        df = df.sort_values("trade_date")
        rows = []
        for month, group in df.groupby(df["trade_date"].dt.strftime("%Y-%m")):
            first = float(group.iloc[0]["total_value"])
            last = float(group.iloc[-1]["total_value"])
            rows.append({
                "month": month,
                "return_pct": round((last / first - 1) * 100, 2) if first else 0,
            })
        return rows

    @staticmethod
    def calc_drawdown_curve(daily_values: list[dict]) -> list[dict]:
        if not daily_values:
            return []
        df = pd.DataFrame(daily_values).sort_values("trade_date")
        values = df["total_value"].astype(float).values
        peak = np.maximum.accumulate(values)
        drawdown = np.divide(values - peak, peak, out=np.zeros_like(values), where=peak > 0) * 100
        return [
            {"date": str(row["trade_date"]), "drawdown_pct": round(float(dd), 2)}
            for (_, row), dd in zip(df.iterrows(), drawdown)
        ]

    @staticmethod
    def calc_trade_distribution(trades: list[TradeRecord]) -> dict:
        sell_pnls = [t.net_amount for t in trades if t.signal_type == SignalType.SELL]
        buckets = [
            ("large_loss", lambda v: v <= -5000),
            ("loss", lambda v: -5000 < v < 0),
            ("small_profit", lambda v: 0 <= v < 5000),
            ("profit", lambda v: 5000 <= v < 20000),
            ("large_profit", lambda v: v >= 20000),
        ]
        bucket_counts = {name: 0 for name, _ in buckets}
        for pnl in sell_pnls:
            for name, predicate in buckets:
                if predicate(pnl):
                    bucket_counts[name] += 1
                    break
        return {
            "sell_count": len(sell_pnls),
            "avg_pnl": round(float(np.mean(sell_pnls)), 2) if sell_pnls else 0,
            "median_pnl": round(float(np.median(sell_pnls)), 2) if sell_pnls else 0,
            "best_pnl": round(max(sell_pnls), 2) if sell_pnls else 0,
            "worst_pnl": round(min(sell_pnls), 2) if sell_pnls else 0,
            "buckets": bucket_counts,
        }


# ========== 信号回测 ==========

class SignalBacktester:
    """信号回测器 — 给研究样本计算实际收益"""

    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.analyzer = PerformanceAnalyzer()

    async def backtest_signals(
        self,
        signals: list[dict],
        price_data: dict[str, pd.DataFrame],
        holding_days: list[int] = None,
    ) -> dict:
        """回测信号列表

        Args:
            signals: [{code, signal_date, signal_price, score, ...}]
            price_data: {code: DataFrame(open/high/low/close)}
            holding_days: 持有天数列表 [1, 3, 5, 10]

        Returns:
            回测结果
        """
        if holding_days is None:
            holding_days = [1, 3, 5, 10]

        results = []
        for sig in signals:
            code = sig["code"]
            signal_date = sig["signal_date"]
            signal_price = sig.get("signal_price", 0)

            if code not in price_data:
                continue

            df = price_data[code]
            if "trade_date" in df.columns:
                df = df.set_index("trade_date")

            # 找到信号日
            try:
                if isinstance(signal_date, str):
                    signal_date = date.fromisoformat(signal_date)
                idx = df.index.get_indexer([signal_date], method="nearest")[0]
            except (ValueError, KeyError):
                continue

            if idx < 0 or idx >= len(df):
                continue

            buy_price = signal_price or df.iloc[idx]["close"]

            # 计算持有N天收益
            returns = {}
            for days in holding_days:
                target_idx = idx + days
                if target_idx < len(df):
                    sell_price = df.iloc[target_idx]["close"]
                    ret = (sell_price / buy_price - 1) * 100
                    # 扣除交易成本
                    cost = buy_price * (self.config.commission_rate * 2 + self.config.stamp_tax_rate)
                    ret -= cost / buy_price * 100
                    returns[f"return_{days}d"] = round(ret, 2)

                    # 最大收益/最大回撤(持有期内)
                    period_high = df.iloc[idx:target_idx + 1]["high"].max()
                    period_low = df.iloc[idx:target_idx + 1]["low"].min()
                    returns[f"max_return_{days}d"] = round((period_high / buy_price - 1) * 100, 2)
                    returns[f"max_drawdown_{days}d"] = round((period_low / buy_price - 1) * 100, 2)
                else:
                    returns[f"return_{days}d"] = None
                    returns[f"max_return_{days}d"] = None
                    returns[f"max_drawdown_{days}d"] = None

            results.append({
                "code": code,
                "signal_date": str(signal_date),
                "buy_price": buy_price,
                **returns,
                **{k: v for k, v in sig.items() if k not in ("code", "signal_date", "signal_price")},
            })

        # 统计
        stats = self._calc_signal_stats(results, holding_days)
        return {"signals": results, "stats": stats, "total": len(results)}

    def _calc_signal_stats(self, results: list[dict], holding_days: list[int]) -> dict:
        """统计信号胜率"""
        stats = {}
        for days in holding_days:
            key = f"return_{days}d"
            values = [r[key] for r in results if r.get(key) is not None]
            if not values:
                stats[key] = {"count": 0, "win_rate": 0, "avg_return": 0}
                continue

            wins = sum(1 for v in values if v > 0)
            stats[key] = {
                "count": len(values),
                "win_rate": round(wins / len(values) * 100, 2),
                "avg_return": round(np.mean(values), 2),
                "median_return": round(np.median(values), 2),
                "max_return": round(max(values), 2),
                "min_return": round(min(values), 2),
            }
        return stats


# ========== 策略回测 ==========

class StrategyBacktester:
    """策略回测器 — 完整的资金曲线回测"""

    def __init__(self, config: BacktestConfig = None):
        self.config = config or BacktestConfig()
        self.analyzer = PerformanceAnalyzer()

    async def backtest_strategy(
        self,
        daily_scores: pd.DataFrame,
        price_data: dict[str, pd.DataFrame],
        start_date: date = None,
        end_date: date = None,
    ) -> dict:
        """策略回测

        Args:
            daily_scores: 每日因子评分 DataFrame(code, trade_date, score, ...)
            price_data: {code: DataFrame(trade_date, open, high, low, close)}
            start_date/end_date: 回测区间

        Returns:
            回测结果(净值曲线+绩效指标+交易记录)
        """
        capital = self.config.initial_capital
        cash = capital
        positions: dict[str, BacktestPosition] = {}
        trades: list[TradeRecord] = []
        daily_values: list[dict] = []
        pending_buys = pd.DataFrame()

        daily_scores = daily_scores.copy()
        daily_scores["trade_date"] = daily_scores["trade_date"].apply(_normalize_trade_date)
        normalized_price_data = {}
        price_dates = set()
        for code, df in price_data.items():
            if df is None or len(df) == 0:
                continue
            normalized_df = df.copy()
            if "trade_date" in normalized_df.columns:
                normalized_df["trade_date"] = normalized_df["trade_date"].apply(_normalize_trade_date)
                price_dates.update(normalized_df["trade_date"].dropna().unique())
            normalized_price_data[code] = normalized_df
        price_data = normalized_price_data

        # 回测日期范围必须来自行情交易日，不能只遍历有信号的日期。
        signal_dates = set(daily_scores["trade_date"].dropna().unique())
        dates = sorted(price_dates or signal_dates)
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]

        for current_date in dates:
            day_scores = daily_scores[daily_scores["trade_date"] == current_date]
            day_scores = day_scores.sort_values("score", ascending=False)

            # 更新持仓价格
            for code, pos in positions.items():
                if code in price_data:
                    df = price_data[code]
                    day_data = df[df["trade_date"] == current_date] if "trade_date" in df.columns else df
                    if len(day_data) > 0:
                        pos.update_price(day_data.iloc[-1]["close"])

            # 止损止盈检查
            positions_to_sell = []
            for code, pos in positions.items():
                if pos.profit_pct <= -self.config.stop_loss_pct:
                    positions_to_sell.append((code, "stop_loss"))
                elif pos.profit_pct >= self.config.take_profit_pct:
                    positions_to_sell.append((code, "take_profit"))

            # 卖出
            for code, reason in positions_to_sell:
                pos = positions[code]
                if code in price_data:
                    df = price_data[code]
                    day_data = df[df["trade_date"] == current_date] if "trade_date" in df.columns else df
                    change_pct = _safe_float(day_data.iloc[-1].get("change_pct", 0)) if len(day_data) else 0
                    if self.config.avoid_limit_up_down and change_pct <= -9.5:
                        continue
                    sell_price = pos.current_price * (1 - self.config.slippage_pct / 100)
                    amount = pos.shares * sell_price
                    stamp_tax = amount * self.config.stamp_tax_rate
                    commission = amount * self.config.commission_rate
                    net = amount - stamp_tax - commission

                    trades.append(TradeRecord(
                        code=code, signal_type=SignalType.SELL,
                        trade_date=current_date, price=sell_price,
                        shares=pos.shares, amount=amount,
                        commission=commission, stamp_tax=stamp_tax,
                        net_amount=net - pos.shares * pos.avg_cost,
                    ))

                    cash += net
                    del positions[code]
                    logger.debug(f"[{current_date}] 卖出 {code} {reason}: P&L={pos.profit_pct:.1f}%")

            # 买入使用上一交易日收盘后产生的信号，避免同日收盘信号同日成交的前视偏差。
            for _, row in pending_buys.iterrows():
                code = row["code"]
                score = row.get("score", 0)

                if code in positions:
                    continue
                if len(positions) >= self.config.max_positions:
                    break
                if score < self.config.min_score_to_buy:
                    continue

                if code not in price_data:
                    continue

                df = price_data[code]
                day_data = df[df["trade_date"] == current_date] if "trade_date" in df.columns else df
                if len(day_data) == 0:
                    continue
                row_data = day_data.iloc[-1]
                change_pct = _safe_float(row_data.get("change_pct", 0))
                if self.config.avoid_limit_up_down and change_pct >= 9.5:
                    continue

                execution_price = row_data.get("open") or row_data["close"]
                buy_price = execution_price * (1 + self.config.slippage_pct / 100)
                position_amount = cash * (self.config.max_position_pct / 100)
                shares = int(position_amount / buy_price / 100) * 100  # 整手
                volume = _safe_float(row_data.get("volume", 0))
                if volume > 0:
                    max_volume_shares = int(volume * self.config.volume_limit_pct / 100 / 100) * 100
                    shares = min(shares, max_volume_shares)

                if shares < 100:
                    continue

                amount = shares * buy_price
                commission = amount * self.config.commission_rate

                if cash < amount + commission:
                    continue

                trades.append(TradeRecord(
                    code=code, signal_type=SignalType.BUY,
                    trade_date=current_date, price=buy_price,
                    shares=shares, amount=amount,
                    commission=commission, net_amount=-(amount + commission),
                ))

                cash -= (amount + commission)
                positions[code] = BacktestPosition(
                    code=code, shares=shares, avg_cost=buy_price,
                    current_price=buy_price,
                    market_value=shares * buy_price,
                )

            pending_buys = day_scores

            # 日末净值
            total_position_value = sum(p.market_value for p in positions.values())
            daily_values.append({
                "trade_date": current_date,
                "cash": round(cash, 2),
                "position_value": round(total_position_value, 2),
                "total_value": round(cash + total_position_value, 2),
                "position_count": len(positions),
            })

        # 绩效分析
        metrics = self.analyzer.calc_metrics(daily_values, capital, trades)
        nav_df = self.analyzer.calc_nav_curve(trades, daily_values, capital)

        return {
            "metrics": metrics,
            "daily_values": daily_values,
            "trades": [
                {
                    "code": t.code,
                    "type": t.signal_type.value,
                    "date": str(t.trade_date),
                    "price": t.price,
                    "shares": t.shares,
                    "amount": round(t.amount, 2),
                    "commission": round(t.commission, 2),
                    "stamp_tax": round(t.stamp_tax, 2),
                    "pnl": round(t.net_amount, 2),
                }
                for t in trades
            ],
        }


# 全局
signal_backtester = SignalBacktester()
strategy_backtester = StrategyBacktester()
