"""断板反包信号全市场回测 (2026-08-31)

背景: 金牛化工(7/17-8/28 +97%)等"断板反包+趋势"股是当前5策略的盲区。
本脚本用 2018-01 起全市场 K 线重建涨停/连板历史 (约880万根K线/5200+只),
定义并回测"断板反包"信号, 评估期望是否为正、能否穿越牛熊。

信号定义 (无未来函数):
  当日涨停 (change_pct >= 涨停幅-0.2, 按板块 10%/20%)
  + 前一日未涨停 (断板, 今日是"反包"而非连板)
  + 过去 past_days 个交易日内出现过连板 >= min_cons (强势基因, 有辨识度)
  + 非一字板 (次日开盘可买入)
买入: 信号日次日开盘价 (与实盘 E 口径一致)
卖出: 逐日检查 止盈/硬止损/到期 (收盘价口径, T+1)
成本: 佣金万3双向 + 印花千1卖出, 每笔等权 2 万元
过滤: ST 股 (当前快照) / 北交所(4,8开头) 排除; 停牌自然跳过

用法: python scripts/replay_break_reversal.py
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import settings
from app.core.price_limit_rules import price_limit_rule
from app.core.stock_tagger import stock_tagger

COMMISSION_RATE = 0.0003
STAMP_TAX_RATE = 0.001
CAPITAL_PER_TRADE = 20_000.0


@dataclass
class SignalParams:
    min_consecutive: int   # 断板前需连板≥此数 (最后一棒连板数)
    max_gap_days: int      # 从最后一棒涨停到反包日的断板交易日数上限 (1=次日立即反包)
    min_vol_ratio: float   # 反包日成交量 / 前5日均量 下限 (1.0=不限)
    take_profit_pct: float
    stop_loss_pct: float
    max_hold_days: int


def _limit_pct(code: str, trade_date: date) -> float:
    """复用全项目唯一的按板块、日期生效的涨跌停规则。"""
    return price_limit_rule(code, trade_date=trade_date).nominal_limit_pct / 100.0


def _detect_signals(df: pd.DataFrame, p: SignalParams) -> list[dict]:
    """对单只股票K线检测断板反包信号. df 需按 trade_date 升序.

    信号定义 (封死口径, 炸板不算连板):
      - limit_hit: 盘中触及涨停价 (high >= 涨停价*0.995)
      - sealed:    收盘封死涨停 (close >= 涨停价*0.995)
      - 连板: 连续 sealed 的天数
      - 断板反包: 今日 sealed + 昨日未 sealed + 最近一棒连板(sealed 连续)>= min_consecutive
                  + 距最近一棒断板交易日数 <= max_gap_days + 放量达标 + 非一字(开盘<涨停价)
    """
    if df.empty:
        return []
    codes = df["code"].iloc[0]
    df = df.reset_index(drop=True)
    # trade_date 统一为 date
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    n = len(df)
    limits = [_limit_pct(codes, d) for d in df["trade_date"]]
    # 涨停价: A股四舍五入到分
    limit_prices = []
    for i in range(n):
        prev_close = df["prev_close"].iloc[i] if pd.notna(df["prev_close"].iloc[i]) else df["close"].iloc[i - 1] if i > 0 else 0
        limit_prices.append(round(prev_close * (1 + limits[i]), 2) if prev_close and prev_close > 0 else 0.0)
    highs = [float(x or 0) for x in df["high"]]
    closes = [float(x or 0) for x in df["close"]]
    limit_hit = [lp > 0 and highs[i] >= lp * 0.995 for i, lp in enumerate(limit_prices)]
    sealed = [lp > 0 and closes[i] >= lp * 0.995 for i, lp in enumerate(limit_prices)]
    # 连板天数 (收盘封死连续计数)
    consec = [0] * n
    for i in range(n):
        if sealed[i]:
            consec[i] = (consec[i - 1] + 1) if i > 0 and sealed[i - 1] else 1
    # 反包日放量: 成交量 > 前5日均量 * min_vol_ratio
    vol_ratio_ok = [True] * n
    if p.min_vol_ratio > 1.0:
        for i in range(n):
            if i < 5:
                vol_ratio_ok[i] = False
            else:
                avg5 = df["volume"].iloc[i - 5 : i].mean()
                vol_ratio_ok[i] = avg5 > 0 and df["volume"].iloc[i] >= avg5 * p.min_vol_ratio
    # 信号: 今日封死 + 昨日未封死 + 最近一棒连板>=min 距今日断板<=max_gap + 放量 + 非一字
    signals = []
    last_sealed_idx = -1   # 今日之前最近一次封死的位置
    last_consec = 0        # 那一棒的连板数
    for i in range(n):
        if i >= 1 and sealed[i] and not sealed[i - 1]:
            # 断板交易日数不包含最后涨停日和今日反包日；旧口径直接相减
            # 会整体多算 1 天，使 max_gap_days 标签与实际样本错位。
            gap = i - last_sealed_idx - 1 if last_sealed_idx >= 0 else 10**9
            if (
                1 <= gap <= p.max_gap_days
                and last_consec >= p.min_consecutive
                and vol_ratio_ok[i]
            ):
                open_px = float(df["open"].iloc[i] or 0)
                if not (limit_prices[i] > 0 and open_px >= limit_prices[i] * 0.998):
                    # 附加质量标记: 量比 / 断板日表现
                    vol_ratio = 0.0
                    if i >= 5:
                        avg5 = df["volume"].iloc[i - 5 : i].mean()
                        vol_ratio = float(df["volume"].iloc[i]) / avg5 if avg5 > 0 else 0.0
                    prev_day_chg = float(df["change_pct"].iloc[i - 1] or 0)
                    anchor_close = float(df["close"].iloc[last_sealed_idx] or 0)
                    break_lows = [
                        float(value)
                        for value in df["low"].iloc[last_sealed_idx + 1:i]
                        if pd.notna(value) and float(value) > 0
                    ]
                    if anchor_close > 0 and break_lows:
                        window_dip_pct = (min(break_lows) / anchor_close - 1) * 100
                        signals.append({
                            "code": codes,
                            "signal_date": df["trade_date"].iloc[i],
                            "signal_idx": i,
                            "consec_before": last_consec,
                            "gap_days": gap,
                            "limit_pct": limits[i],
                            "vol_ratio": round(vol_ratio, 2),
                            "prev_day_chg": round(prev_day_chg, 2),
                            "window_dip_pct": round(window_dip_pct, 2),
                        })
        if sealed[i]:
            last_sealed_idx = i
            last_consec = consec[i]
    return signals


def _simulate_one(df: pd.DataFrame, sig: dict, p: SignalParams) -> dict | None:
    """信号日次日开盘买入, 逐日检查止盈/止损/到期. 返回收益%或None.

    收益口径: 复合收益 (买入开盘价 → 逐日 close 复合), 不使用绝对价格比,
    避免除权缺口污染 (2018-12 等除权日 close 跳变导致 -1050% 假亏损).
    """
    idx = sig["signal_idx"]
    if idx + 1 >= len(df):
        return None
    buy_k = df.iloc[idx + 1]
    buy_price = float(buy_k["open"] or buy_k["close"] or 0)
    if buy_price <= 0:
        return None
    # 买入日一字板 (开盘≈涨停) 无法买入
    limits = _limit_pct(sig["code"], buy_k["trade_date"])
    prev_close = float(buy_k["prev_close"] if pd.notna(buy_k["prev_close"]) else df["close"].iloc[idx])
    if prev_close > 0 and buy_price >= prev_close * (1 + limits) * 0.998:
        return None

    # 买入当日: 收盘相对开盘的收益 (T+1, 当日仅硬止损)
    day0_close = float(buy_k["close"] or 0)
    if day0_close <= 0:
        return None
    # 数据质量护栏: 异常涨跌幅(除权未复权污染)视为数据损坏, 放弃该笔
    if abs(float(buy_k["change_pct"] or 0)) > 30:
        return None
    day0_ret = (day0_close / buy_price - 1) * 100
    deferred_t1_stop = day0_ret <= -p.stop_loss_pct

    # 之后逐日: 用 change_pct 复合 (除权安全), 检查止损/止盈/到期
    profit_pct = day0_ret
    for offset in range(1, p.max_hold_days + 1):
        di = idx + 1 + offset
        if di >= len(df):
            # 未走满持有窗口属于右删失，不能把样本末端伪装成到期卖出。
            return None
        k = df.iloc[di]
        if deferred_t1_stop:
            next_open = float(k["open"] or k["close"] or 0)
            if next_open <= 0:
                return None
            t1_ret = (next_open / buy_price - 1) * 100
            return _mk_result(
                df,
                di,
                t1_ret,
                "买入日止损穿越，T+1次日开盘退出（日K无法证明跌停队列成交）",
            )
        chg = float(k["change_pct"] or 0)
        if abs(chg) > 30:
            return None
        profit_pct = (1 + profit_pct / 100) * (1 + chg / 100) * 100 - 100
        if profit_pct <= -p.stop_loss_pct:
            return _mk_result(df, di, profit_pct, "触发止损")
        if profit_pct >= p.take_profit_pct:
            return _mk_result(df, di, profit_pct, "触发止盈")
    # 只有完整走到持有上限才记为到期平仓。
    return _mk_result(df, idx + 1 + p.max_hold_days, profit_pct, "到期平仓")


def _mk_result(df, di, profit_pct, reason):
    cost_bps = COMMISSION_RATE * 2 + STAMP_TAX_RATE
    return {
        "ret_pct": round(profit_pct - cost_bps * 100, 4),
        "reason": reason,
        "sell_date": df["trade_date"].iloc[di],
    }


def _evaluate(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t["ret_pct"] for t in trades]
    n = len(rets)
    wins = [x for x in rets if x > 0]
    losses = [abs(x) for x in rets if x <= 0]
    avg = sum(rets) / n
    wr = len(wins) / n * 100
    pl = (sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else 0.0
    s3 = sorted(rets, reverse=True)[3:]
    s5 = sorted(rets, reverse=True)[5:]
    s10 = sorted(rets, reverse=True)[10:]
    lo, hi = 999.0, -999.0
    for i in range(n):
        rest = rets[:i] + rets[i + 1:]
        a = sum(rest) / len(rest)
        lo, hi = min(lo, a), max(hi, a)
    return {
        "n": n,
        "avg_ret": round(avg, 3),
        "win_rate": round(wr, 1),
        "pl_ratio": round(pl, 2),
        "no_top3": round(sum(s3) / len(s3), 3) if s3 else 0,
        "no_top5": round(sum(s5) / len(s5), 3) if s5 else 0,
        "no_top10": round(sum(s10) / len(s10), 3) if s10 else 0,
        "loo_min": round(lo, 3),
        "loo_max": round(hi, 3),
        "max_loss": round(min(rets), 2),
        "total_ret": round(sum(rets), 1),
    }


def _group_by_code(signals: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for s in signals:
        groups.setdefault(s["code"], []).append(s)
    return groups


async def load_all_klines(session: AsyncSession, exclude_st: bool = True) -> list[pd.DataFrame]:
    """按股票加载K线 (排除ST/北交所). 返回按 code 分组的 DataFrame 列表."""
    st_codes = set()
    if exclude_st:
        rows = (await session.execute(text("SELECT code FROM stock_tags WHERE is_st = 1"))).all()
        st_codes = {str(r[0]) for r in rows}
    df_all = await session.execute(text(
        "SELECT code, trade_date, open, close, high, low, volume, change_pct, prev_close "
        "FROM stock_kline ORDER BY code, trade_date"
    ))
    dfs: list[pd.DataFrame] = []
    cur_code = None
    rows_buf = []
    count = 0
    for row in df_all.fetchall():
        code = str(row.code)
        if not stock_tagger.is_tradeable(code) or code in st_codes:
            continue
        if cur_code is None:
            cur_code = code
        if code != cur_code:
            dfs.append(pd.DataFrame(rows_buf, columns=[
                "code", "trade_date", "open", "close", "high", "low", "volume", "change_pct", "prev_close"]))
            rows_buf = []
            cur_code = code
        rows_buf.append((code, row.trade_date, row.open, row.close, row.high, row.low, row.volume, row.change_pct, row.prev_close))
        count += 1
    if rows_buf:
        dfs.append(pd.DataFrame(rows_buf, columns=[
            "code", "trade_date", "open", "close", "high", "low", "volume", "change_pct", "prev_close"]))
    # 统一 trade_date 为 date 类型 (下游 _limit_pct/_simulate_one 需要)
    for df in dfs:
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    print(f"已加载 {len(dfs)} 只股票 / {count} 行K线")
    return dfs


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, future=True)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with SessionLocal() as session:
            dfs = await load_all_klines(session)
    finally:
        await engine.dispose()

    # 宽松信号检测: 连板≥2 / 断板≤5日, 附加质量标记, 之后按组合过滤
    base = SignalParams(min_consecutive=2, max_gap_days=5, min_vol_ratio=1.0,
                        take_profit_pct=15.0, stop_loss_pct=8.0, max_hold_days=5)
    print(f"检测断板反包信号 (宽松: 连板≥2 / 断板≤5日)...")
    all_signals: list[dict] = []
    for df in dfs:
        all_signals.extend(_detect_signals(df, base))
    print(f"总信号: {len(all_signals)}")
    years = Counter(str(s["signal_date"])[:4] for s in all_signals)
    print("按年份:", dict(sorted(years.items())))

    df_by_code = {df["code"].iloc[0]: df for df in dfs}

    def run_filter(sigs: list[dict], p: SignalParams) -> dict:
        trades = []
        for code, sgroup in _group_by_code(sigs).items():
            df = df_by_code.get(code)
            if df is None:
                continue
            for sig in sgroup:
                r = _simulate_one(df, sig, p)
                if r:
                    r["code"] = code
                    r["signal_date"] = sig["signal_date"]
                    trades.append(r)
        return _evaluate(trades)

    # ============ 信号质量扫描: 连板数 × 断板天数 × 窗口最深回撤 × 放量 ============
    print(f"\n{'连板≥':>5}{'断板≤':>5}{'窗口回撤≤':>9}{'放量≥':>6}{'样本':>6}{'均收%':>8}{'胜率%':>7}{'盈亏比':>7}{'剔Top3%':>9}{'剔Top5%':>9}{'留一最差%':>10}")
    print('-' * 95)
    scan_rows = []
    for min_cons in (2, 3, 4):
        for max_gap in (1, 2, 3, 5):
            for window_dip_max in (99, -2, -5):
                for min_vr in (1.0, 1.5):
                    sigs = [
                        s for s in all_signals
                        if s["consec_before"] >= min_cons
                        and s["gap_days"] <= max_gap
                        and s["window_dip_pct"] <= window_dip_max
                        and s["vol_ratio"] >= min_vr
                    ]
                    if not sigs:
                        continue
                    p = SignalParams(min_consecutive=min_cons, max_gap_days=max_gap,
                                     min_vol_ratio=min_vr, take_profit_pct=15.0,
                                     stop_loss_pct=8.0, max_hold_days=5)
                    ev = run_filter(sigs, p)
                    scan_rows.append((ev, min_cons, max_gap, window_dip_max, min_vr))
                    dip_label = "不限" if window_dip_max == 99 else str(window_dip_max)
                    print(f"{min_cons:>5}{max_gap:>5}{dip_label:>10}{min_vr:>6}{ev['n']:>6}"
                          f"{ev['avg_ret']:>8.3f}{ev['win_rate']:>7.1f}{ev['pl_ratio']:>7.2f}"
                          f"{ev['no_top3']:>9.3f}{ev['no_top5']:>9.3f}{ev['loo_min']:>10.3f}")

    # ============ 对最优信号子集做卖出参数扫描 ============
    print("\n\n=== 卖出参数扫描 (信号: 连板≥3 / 断板≤3 / 窗口最深≤-5% / 放量≥1.5x) ===")
    best_sigs = [
        s for s in all_signals
        if s["consec_before"] >= 3 and s["gap_days"] <= 3 and s["window_dip_pct"] <= -5
        and s["vol_ratio"] >= 1.5
    ]
    print(f"信号数: {len(best_sigs)}")
    print(f"{'止盈':>5}{'止损':>5}{'持仓':>5}{'样本':>6}{'均收%':>8}{'胜率%':>7}{'盈亏比':>7}{'剔Top3%':>9}{'剔Top5%':>9}{'留一最差%':>10}{'最大亏%':>8}")
    print('-' * 90)
    for tp in (8, 10, 12, 15, 20):
        for sl in (5, 6, 8, 10):
            for hold in (3, 5, 7, 10):
                p = SignalParams(min_consecutive=3, max_gap_days=3, min_vol_ratio=1.5,
                                 take_profit_pct=tp, stop_loss_pct=sl, max_hold_days=hold)
                ev = run_filter(best_sigs, p)
                if ev["n"] < 30:
                    continue
                print(f"{tp:>5}{sl:>5}{hold:>5}{ev['n']:>6}{ev['avg_ret']:>8.3f}{ev['win_rate']:>7.1f}"
                      f"{ev['pl_ratio']:>7.2f}{ev['no_top3']:>9.3f}{ev['no_top5']:>9.3f}{ev['loo_min']:>10.3f}{ev['max_loss']:>8.2f}")


if __name__ == "__main__":
    asyncio.run(main())
