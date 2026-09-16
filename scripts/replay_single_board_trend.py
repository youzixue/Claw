"""单板趋势形态信号全市场回测 (2026-08-31)

背景: 盛达资源(+52%)/金牛化工(+97%)/赤天化(+80%)/招金黄金(+87%) 是"间歇涨停+趋势上行"
形态 —— 与断板反包(连板≥3后深跌反包)不同, 它们涨停都是单板(连板≤2), 靠反复放量脉冲推升。

本脚本定义并扫描"单板趋势"信号, 全市场 2018-2026 回测看期望是否为正。

信号候选 (无未来函数, 全部基于信号日收盘后可见数据):
  v1 放量脉冲: 过去20日内有封死涨停(基因) + 当日涨幅≥6%且量比≥1.5 + 收盘在MA20上方
  v2 回踩再启动: 过去20日内有封死涨停 + 近3日回调整理(不破MA20) + 当日放量涨≥4%
  v3 单板涨停: 当日封死涨停 + 前一日未涨停(单板) + 过去20日内有涨停基因 + 连板≤2
买入: 信号日次日开盘 (一字跳过), 卖出参数扫描 (止盈/止损/持仓)
成本: 佣金万3+印花千1, 每笔等权2万, 复合收益, 数据护栏 |chg|>30
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from scripts.replay_break_reversal import (
    COMMISSION_RATE, STAMP_TAX_RATE, _limit_pct, _evaluate,
)

CAPITAL_PER_TRADE = 20_000.0


@dataclass
class TrendParams:
    variant: str          # v1/v2/v3
    min_gene_win: int     # 过去N日内有涨停基因
    min_pulse_pct: float  # 当日脉冲涨幅下限
    min_vol_ratio: float  # 当日量比下限
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_days: int


def _detect_trend_signals(df: pd.DataFrame, p: TrendParams) -> list[dict]:
    """单板趋势信号检测. df 需按 trade_date 升序."""
    if df.empty:
        return []
    codes = df["code"].iloc[0]
    df = df.reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    n = len(df)
    limits = [_limit_pct(codes, d) for d in df["trade_date"]]
    # 涨停价/封死
    lps, sealed = [], []
    for i in range(n):
        pc = df["prev_close"].iloc[i] if pd.notna(df["prev_close"].iloc[i]) else (df["close"].iloc[i-1] if i > 0 else 0)
        lps.append(round(pc * (1 + limits[i]), 2) if pc and pc > 0 else 0.0)
        sealed.append(lps[i] > 0 and float(df["close"].iloc[i] or 0) >= lps[i] * 0.995)
    # 连板数
    consec = [0] * n
    for i in range(n):
        if sealed[i]:
            consec[i] = (consec[i-1] + 1) if i > 0 and sealed[i-1] else 1
    closes = [float(x or 0) for x in df["close"]]
    vols = [float(x or 0) for x in df["volume"]]
    chgs = [float(x or 0) for x in df["change_pct"]]
    signals = []
    for i in range(2, n):
        if abs(chgs[i]) > 30:
            continue
        # 基因: 过去 min_gene_win 日内有过封死涨停
        gene_win = p.min_gene_win
        lo = max(0, i - gene_win)
        has_gene = any(sealed[lo:i])
        if not has_gene:
            continue
        # 均线
        ma5 = sum(closes[i-4:i+1]) / 5
        ma10 = sum(closes[i-9:i+1]) / 10 if i >= 9 else ma5
        ma20 = sum(closes[i-19:i+1]) / 20 if i >= 19 else ma10
        avg5v = sum(vols[i-5:i]) / 5 if i >= 5 else (sum(vols[:i]) / i if i > 0 else 0)
        vol_ratio = vols[i] / avg5v if avg5v > 0 else 0
        # 连板≤2 (单板趋势排除高标连板)
        if consec[i] > 2:
            continue
        # 当日一字跳过
        prev_close = df["prev_close"].iloc[i] if pd.notna(df["prev_close"].iloc[i]) else closes[i-1]
        open_px = float(df["open"].iloc[i] or 0)
        if prev_close > 0 and lps[i] > 0 and open_px >= lps[i] * 0.998:
            continue
        # 收盘在 MA20 上方 (趋势未破坏)
        if closes[i] <= ma20:
            continue
        ok = False
        if p.variant == "v1":
            # 放量脉冲: 当日涨≥min_pulse_pct 且量比达标
            ok = chgs[i] >= p.min_pulse_pct and vol_ratio >= p.min_vol_ratio
        elif p.variant == "v2":
            # 回踩再启动: 近3日有回调(至少1日<0且不破MA20) + 当日放量涨
            recent_dip = any(chgs[j] < 0 for j in range(max(lo, i-3), i))
            ok = recent_dip and chgs[i] >= p.min_pulse_pct and vol_ratio >= p.min_vol_ratio
        elif p.variant == "v3":
            # 单板涨停: 当日封死 + 昨日未封死 + 有基因
            ok = sealed[i] and (i < 1 or not sealed[i-1])
        if not ok:
            continue
        signals.append({
            "code": codes,
            "signal_date": df["trade_date"].iloc[i],
            "signal_idx": i,
            "vol_ratio": round(vol_ratio, 2),
            "chg": round(chgs[i], 2),
            "ma20": round(ma20, 2),
        })
    return signals


def _simulate_one(df: pd.DataFrame, sig: dict, p: TrendParams) -> dict | None:
    """次日开盘买入, 逐日止盈/止损/到期 (复合收益)."""
    idx = sig["signal_idx"]
    if idx + 1 >= len(df):
        return None
    buy_k = df.iloc[idx + 1]
    buy_price = float(buy_k["open"] or buy_k["close"] or 0)
    if buy_price <= 0:
        return None
    limits = _limit_pct(sig["code"], buy_k["trade_date"])
    prev_close = float(buy_k["prev_close"] if pd.notna(buy_k["prev_close"]) else df["close"].iloc[idx])
    if prev_close > 0 and buy_price >= prev_close * (1 + limits) * 0.998:
        return None
    if abs(float(buy_k["change_pct"] or 0)) > 30:
        return None
    day0_close = float(buy_k["close"] or 0)
    if day0_close <= 0:
        return None
    day0_ret = (day0_close / buy_price - 1) * 100
    if day0_ret <= -p.stop_loss_pct:
        return _mk_result(df, idx + 1, day0_ret, "买入当日收盘破硬止损")
    profit_pct = day0_ret
    for offset in range(1, p.max_hold_days + 1):
        di = idx + 1 + offset
        if di >= len(df):
            break
        k = df.iloc[di]
        chg = float(k["change_pct"] or 0)
        if abs(chg) > 30:
            return _mk_result(df, di, profit_pct, "数据异常终止")
        profit_pct = (1 + profit_pct / 100) * (1 + chg / 100) * 100 - 100
        if profit_pct <= -p.stop_loss_pct:
            return _mk_result(df, di, profit_pct, "触发止损")
        if profit_pct >= p.take_profit_pct:
            return _mk_result(df, di, profit_pct, "触发止盈")
    last_di = min(idx + 1 + p.max_hold_days, len(df) - 1)
    profit_pct = day0_ret
    for di in range(idx + 2, last_di + 1):
        chg = float(df.iloc[di]["change_pct"] or 0)
        if abs(chg) > 30:
            return _mk_result(df, di, profit_pct, "数据异常终止")
        profit_pct = (1 + profit_pct / 100) * (1 + chg / 100) * 100 - 100
    return _mk_result(df, last_di, profit_pct, "到期平仓")


def _mk_result(df, di, profit_pct, reason):
    cost_bps = COMMISSION_RATE * 2 + STAMP_TAX_RATE
    return {
        "ret_pct": round(profit_pct - cost_bps * 100, 4),
        "reason": reason,
        "sell_date": df["trade_date"].iloc[di],
    }


async def load_all_klines(session: AsyncSession) -> list[pd.DataFrame]:
    st_codes = set()
    rows = (await session.execute(text("SELECT code FROM stock_tags WHERE is_st = 1"))).all()
    st_codes = {str(r[0]) for r in rows}
    df_all = await session.execute(text(
        "SELECT code, trade_date, open, close, high, low, volume, change_pct, prev_close "
        "FROM stock_kline ORDER BY code, trade_date"
    ))
    dfs, cur_code, buf, count = [], None, [], 0
    for row in df_all.fetchall():
        code = str(row.code)
        if code.startswith(("4", "8")) or code in st_codes:
            continue
        if cur_code is None:
            cur_code = code
        if code != cur_code:
            dfs.append(pd.DataFrame(buf, columns=[
                "code", "trade_date", "open", "close", "high", "low", "volume", "change_pct", "prev_close"]))
            buf, cur_code = [], code
        buf.append((code, row.trade_date, row.open, row.close, row.high, row.low, row.volume, row.change_pct, row.prev_close))
        count += 1
    if buf:
        dfs.append(pd.DataFrame(buf, columns=[
            "code", "trade_date", "open", "close", "high", "low", "volume", "change_pct", "prev_close"]))
    for df in dfs:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    print(f"已加载 {len(dfs)} 只 / {count} 行")
    return dfs


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            dfs = await load_all_klines(session)
    finally:
        await engine.dispose()

    # 每种变体检测信号并扫描卖出参数
    for variant, desc in (("v1", "放量脉冲(基因+涨≥6%+量比≥1.5+MA20上)"),
                          ("v2", "回踩再启动(基因+近3日回调+放量涨≥4%)"),
                          ("v3", "单板涨停(基因+当日封死+非连板)")):
        p_detect = TrendParams(variant, min_gene_win=20, min_pulse_pct=6.0,
                               min_vol_ratio=1.5, take_profit_pct=15.0,
                               stop_loss_pct=8.0, max_hold_days=5)
        print(f"\n\n########## {variant} {desc} ##########")
        all_signals = []
        for df in dfs:
            all_signals.extend(_detect_trend_signals(df, p_detect))
        print(f"信号: {len(all_signals)} | 按年: {dict(sorted(Counter(str(s['signal_date'])[:4] for s in all_signals).items()))}")
        if not all_signals:
            continue
        by_code: dict[str, list[dict]] = {}
        for s in all_signals:
            by_code.setdefault(s["code"], []).append(s)
        df_by_code = {df["code"].iloc[0]: df for df in dfs}

        # 卖出参数网格 (固定 20日基因/脉冲阈值)
        print(f"{'止盈':>5}{'止损':>5}{'持仓':>5}{'样本':>6}{'均收%':>8}{'胜率%':>7}{'盈亏比':>7}{'剔Top3%':>9}{'剔Top5%':>9}{'留一最差%':>10}")
        print('-' * 82)
        best = None
        for tp in (10, 12, 15, 20):
            for sl in (5, 6, 8):
                for hold in (3, 5, 7):
                    trades = []
                    for code, sigs in by_code.items():
                        df = df_by_code.get(code)
                        if df is None:
                            continue
                        for sig in sigs:
                            r = _simulate_one(df, sig, TrendParams(
                                variant, 20, p_detect.min_pulse_pct, p_detect.min_vol_ratio,
                                tp, sl, hold))
                            if r:
                                r["code"] = code
                                r["signal_date"] = sig["signal_date"]
                                trades.append(r)
                    ev = _evaluate(trades)
                    if ev["n"] < 100:
                        continue
                    if best is None or ev["avg_ret"] > best[1]["avg_ret"]:
                        best = (f"{tp}/{sl}/{hold}", ev)
                    print(f"{tp:>5}{sl:>5}{hold:>5}{ev['n']:>6}{ev['avg_ret']:>8.3f}{ev['win_rate']:>7.1f}"
                          f"{ev['pl_ratio']:>7.2f}{ev['no_top3']:>9.3f}{ev['no_top5']:>9.3f}{ev['loo_min']:>10.3f}")
        if best:
            print(f"==> {variant} 最优: {best[0]} 均收{best[1]['avg_ret']}% 胜率{best[1]['win_rate']}%")

        # 对最优参数做分年度
        if best:
            tp, sl, hold = (int(x) for x in best[0].split("/"))
            trades = []
            for code, sigs in by_code.items():
                df = df_by_code.get(code)
                if df is None:
                    continue
                for sig in sigs:
                    r = _simulate_one(df, sig, TrendParams(variant, 20, p_detect.min_pulse_pct,
                                                            p_detect.min_vol_ratio, tp, sl, hold))
                    if r:
                        r["code"] = code
                        r["signal_date"] = sig["signal_date"]
                        trades.append(r)
            print(f"\n{variant} 最优参数 {best[0]} 分年度:")
            by_year = {}
            for t in trades:
                y = str(t["sell_date"])[:4]
                by_year.setdefault(y, []).append(t["ret_pct"])
            for y in sorted(by_year):
                r = by_year[y]
                avg = sum(r) / len(r)
                wr = sum(1 for x in r if x > 0) / len(r) * 100
                print(f"  {y}: {len(r)}笔 均收{avg:+.3f}% 胜率{wr:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
