"""E（连板高标接力）/ F（断板反包）可执行收益实验。

为什么需要
----------
`promotion_prediction_record` 那条路走不通：E/F 是**直读 `limit_up_pool`** 的
独立候选机制，不经过晋升引擎，也就没有 `actual_close_change_pct`。
而且 `_tenbagger_midline_candidates` / `_reversal_pullback_candidates` 都用
`_spot_by_code`（**当前**实时行情）做盘中确认，**无法用它们回放历史**。

所以本脚本：
  * 信号侧（历史可复现）：K 线 + `limit_up_pool` 精确重建候选集，口径照抄
    `paper.py` 的闸门与 `settings` 参数；
  * 入场侧：用逐轮真实报价归档 `runtime/quote_rounds/compact/` 重放盘中确认
    （归档有 `price / change_pct / avg_price(VWAP) / high`，正好够）；
  * 出场侧：固定窗口 + 按引擎止盈/止损的 K 线近似（两种并列）。

入场口径（与 paper.py 对齐）
---------------------------
E 账户6(`tenbagger`)：封单≥1.0亿、炸板≤2、连板4–8、
    涨幅≤3.0%、现价≥VWAP、距日内高点回撤≤2.0%、买入起点 09:30
F 账户7(`reversal`)：断板前连板≥3、断板间隔≤3、窗口深跌≤−5%、
    信号日量比≥1.5、信号日开盘非一字、涨幅≤3.0%、现价≥VWAP、回撤≤2.0%

**已知缺口**
  * E 的 `max_peak_change_pct=6.0`（盘中峰值涨幅）在归档里无法可靠还原 → 未复刻。
  * 引擎还有报价路径确认（≥2 帧/60 秒）、涨停排板队列、分批建仓、风控，
    本脚本只测**信号可执行性**。
  * 出场用 K 线近似：同一根 K 线同时触及止盈与止损时，**保守按先止损**。
  * 归档只从 2026-09-07 起 → 可检验的买入日很少。
"""
from __future__ import annotations

import glob
import sqlite3
import statistics as st
from datetime import date, datetime, time
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "backend" / "claw.db"
ROUNDS = Path(__file__).resolve().parents[1] / "runtime" / "quote_rounds" / "compact"
FRICTION_PCT = 0.15

# E（账户6 tenbagger，PAPER_HIGHBOARD_*）
E_MIN_CONSEC, E_MAX_CONSEC = 4, 8
E_MIN_SEAL = 1.0e8
E_MAX_BREAK = 2
E_MAX_ENTRY_CHANGE = 3.0          # PAPER_TENBAGGER_MAX_INTRADAY_CONFIRM_CHANGE_PCT
E_MAX_PULLBACK = 2.0
E_BUY_START = time(9, 30)
E_HOLD, E_TP, E_SL = 3, 18.0, 6.0

# F（账户7 reversal，PAPER_REVERSAL_*）
F_MIN_CONSEC = 3
F_MAX_GAP = 3
F_MIN_DIP = -5.0
F_MIN_VOL_RATIO = 1.5
F_MAX_ENTRY_CHANGE = 3.0
F_MAX_PULLBACK = 2.0
F_BUY_START = time(9, 30)
F_HOLD, F_TP, F_SL = 3, 12.0, 8.0
BUY_END = time(14, 50)


def conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def trade_days(c: sqlite3.Connection) -> list[str]:
    return [str(r[0]) for r in c.execute(
        "SELECT trade_date FROM trade_calendar WHERE is_trade_day=1 ORDER BY trade_date")]


def load_kline(c: sqlite3.Connection) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for code, d, o, cl, hi, lo, pc, vol in c.execute(
        "SELECT code, trade_date, open, close, high, low, prev_close, volume FROM stock_kline"
    ):
        out.setdefault(str(code), {})[str(d)] = {
            "open": float(o) if o else 0.0, "close": float(cl) if cl else 0.0,
            "high": float(hi) if hi else 0.0, "low": float(lo) if lo else 0.0,
            "prev_close": float(pc) if pc else 0.0, "volume": float(vol) if vol else 0.0,
        }
    return out


def load_limit_up(c: sqlite3.Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for d, code, name, cons, seal, brk, quar in c.execute(
        "SELECT trade_date, code, name, consecutive_days, seal_amount, break_count, quarantined "
        "FROM limit_up_pool"
    ):
        out.setdefault(str(d), []).append({
            "code": str(code), "name": str(name or ""), "cons": int(cons or 0),
            "seal": float(seal or 0), "break": int(brk or 0), "quarantined": bool(quar),
        })
    return out


def round_files(day: str) -> list[str]:
    return sorted(glob.glob(str(ROUNDS / f"trade_date={day}" / "qr-*.parquet")))


def load_rounds(day: str, codes: set[str]) -> dict[str, list[tuple]]:
    import pandas as pd

    frames = [pd.read_parquet(
        p, columns=["code", "price", "change_pct", "avg_price", "high", "committed_at"],
    ) for p in round_files(day)]
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True)
    df = df[df["code"].isin(codes)].sort_values("committed_at")
    out: dict[str, list[tuple]] = {}
    for code, price, chg, avg, high, committed in zip(
        df["code"], df["price"], df["change_pct"], df["avg_price"],
        df["high"], df["committed_at"],
    ):
        stamp = str(committed)
        ts = stamp[11:19] if len(stamp) >= 19 else ""
        if len(ts) != 8 or ts[2] != ":":
            continue
        out.setdefault(str(code), []).append(
            (ts, float(price or 0), float(chg) if chg is not None else None,
             float(avg or 0), float(high or 0))
        )
    return out


def sealed(bar: dict, code: str) -> bool:
    pc = bar["prev_close"]
    if not pc:
        return False
    pct = 0.20 if str(code).startswith(("30", "68")) else 0.10
    return bar["close"] >= round(pc * (1 + pct), 2) * 0.995


def build_candidates(kline, limit_up, days) -> dict[str, list[dict]]:
    """返回 {买入日D: [候选]}。信号在 D 的前一交易日收盘后确认。"""
    by_day: dict[str, list[dict]] = {}
    for i, day in enumerate(days[1:], start=1):
        sig = days[i - 1]
        bucket = by_day.setdefault(day, [])
        # ---- E：连板 4-8 高标接力 ----
        for row in limit_up.get(sig, []):
            if row["quarantined"] or not (E_MIN_CONSEC <= row["cons"] <= E_MAX_CONSEC):
                continue
            if row["seal"] < E_MIN_SEAL or row["break"] > E_MAX_BREAK:
                continue
            bucket.append({"code": row["code"], "name": row["name"], "family": "E"})
        # ---- F：断板反包 ----
        for code, bars in kline.items():
            seq = [d for d in days if d in bars]
            if sig not in seq:
                continue
            i_sig = seq.index(sig)
            bar = bars[sig]
            if not sealed(bar, code) or (i_sig >= 1 and sealed(bars[seq[i_sig - 1]], code)):
                continue
            j, last = i_sig - 2, -1
            while j >= 0:
                if sealed(bars[seq[j]], code):
                    last = j
                    break
                j -= 1
            if last < 0:
                continue
            cons, cur = 1, last
            while cur > 0 and sealed(bars[seq[cur - 1]], code):
                cons += 1
                cur -= 1
            gap = i_sig - last - 1
            if cons < F_MIN_CONSEC or gap > F_MAX_GAP:
                continue
            window = [bars[seq[k]] for k in range(last + 1, i_sig + 1)]
            dips = [(b["low"] / b["prev_close"] - 1) * 100 for b in window if b["prev_close"]]
            if not dips or min(dips) > F_MIN_DIP:
                continue
            prev5 = [bars[seq[k]]["volume"] for k in range(max(0, i_sig - 5), i_sig)]
            base = st.mean(prev5) if prev5 else 0.0
            if base <= 0 or bar["volume"] / base < F_MIN_VOL_RATIO:
                continue
            if bar["prev_close"] and bar["open"] >= round(bar["prev_close"] * 1.10, 2) * 0.995:
                continue                       # 信号日一字开盘
            bucket.append({"code": code, "name": "", "family": "F"})
    return by_day


def entry_with_confirm(history, *, buy_start, max_change, max_pullback):
    for ts, price, chg, avg, high in history:
        if ts < buy_start.strftime("%H:%M:%S") or ts > BUY_END.strftime("%H:%M:%S"):
            continue
        if not price or price <= 0 or chg is None:
            continue
        if chg > max_change:
            continue
        if not (avg > 0) or price < avg:            # require_above_vwap
            continue
        if high > 0 and (high - price) / high * 100 > max_pullback:
            continue
        return price
    return None


def entry_no_confirm(history, *, buy_start):
    for ts, price, _c, _a, _h in history:
        if ts < buy_start.strftime("%H:%M:%S") or ts > BUY_END.strftime("%H:%M:%S"):
            continue
        if price and price > 0:
            return price
    return None


def exit_bar_approx(bars, entry_price, hold, tp, sl):
    """K 线近似的规则化出场：同时触及时保守按先止损。"""
    for bar in bars[:hold]:
        last = (bar["low"] / entry_price - 1) * 100
        first = (bar["high"] / entry_price - 1) * 100
        if last <= -sl:
            return -sl
        if first >= tp:
            return tp
    if not bars:
        return None
    return (bars[-1]["close"] / entry_price - 1) * 100


def net(vals):
    return st.mean(vals) - FRICTION_PCT if len(vals) > 1 else float("nan")


def ci(vals):
    n = len(vals)
    if n < 2:
        return float("nan")
    tc = 1.96 if n > 21 else 2.086
    return st.mean(vals) - tc * st.stdev(vals) / n ** 0.5 - FRICTION_PCT


def show(label, vals):
    n = len(vals)
    if n < 2:
        print(f"    {label:<32} n={n:<4} 样本不足")
        return
    print(f"    {label:<32} n={n:<4} 净={net(vals):+7.3f}%  净CI下界={ci(vals):+7.3f}%  "
          f"胜率={sum(1 for v in vals if v > 0) / n * 100:4.0f}%")


def main() -> int:
    c = conn()
    days = trade_days(c)
    kline = load_kline(c)
    limit_up = load_limit_up(c)
    cands = build_candidates(kline, limit_up, days)
    buy_days = sorted(d for d in cands if round_files(d))
    print("E/F 可执行收益实验（信号=历史K线+涨停池；入场=逐轮真实报价；摩擦 0.15%）")
    print("⚠️ E 的 max_peak_change_pct 未复刻；出场用K线近似；归档仅 2026-09-07 起\n")

    for family, cfg in (
        ("E", dict(buy_start=E_BUY_START, max_change=E_MAX_ENTRY_CHANGE,
                   max_pullback=E_MAX_PULLBACK, hold=E_HOLD, tp=E_TP, sl=E_SL)),
        ("F", dict(buy_start=F_BUY_START, max_change=F_MAX_ENTRY_CHANGE,
                   max_pullback=F_MAX_PULLBACK, hold=F_HOLD, tp=F_TP, sl=F_SL)),
    ):
        res = {k: [] for k in ("nc_close", "wc_close", "nc_rule", "wc_rule")}
        pool = wc = 0
        per_day = []
        for day in buy_days:
            rows = [r for r in cands[day] if r["family"] == family]
            if not rows:
                continue
            rounds = load_rounds(day, {r["code"] for r in rows})
            nxt = days[days.index(day) + 1:] if day in days else []
            bought = 0
            for row in rows:
                hist = rounds.get(row["code"]) or []
                if not hist:
                    continue
                pool += 1
                p_nc = entry_no_confirm(hist, buy_start=cfg["buy_start"])
                p_wc = entry_with_confirm(hist, buy_start=cfg["buy_start"],
                                          max_change=cfg["max_change"],
                                          max_pullback=cfg["max_pullback"])
                hold_bars_common = [
                    kline.get(row["code"], {}).get(d) for d in nxt[:cfg["hold"]]
                ]
                hold_bars_common = [b for b in hold_bars_common if b]
                if p_nc:
                    bar0 = kline.get(row["code"], {}).get(day)
                    if bar0 and bar0["close"]:
                        res["nc_close"].append((bar0["close"] / p_nc - 1) * 100)
                    v = exit_bar_approx(hold_bars_common, p_nc, cfg["hold"], cfg["tp"], cfg["sl"])
                    if v is not None:
                        res["nc_rule"].append(v)
                if p_wc:
                    bought += 1
                    bar0 = kline.get(row["code"], {}).get(day)
                    if bar0 and bar0["close"]:
                        res["wc_close"].append((bar0["close"] / p_wc - 1) * 100)
                    v = exit_bar_approx(hold_bars_common, p_wc, cfg["hold"], cfg["tp"], cfg["sl"])
                    if v is not None:
                        res["wc_rule"].append(v)
            wc += bought
            per_day.append(f"    {day}  信号={len(rows):>3}  无确认入场={len(rows):>3}  有确认入场={bought:>3}")

        print("=" * 78)
        print(f"{family} 族   池={pool}  确认链通过={wc}")
        print("\n".join(per_day) if per_day else "    （无可检验买入日）")
        print("  --- 出场=当日收盘（日内）---")
        show("不用确认链", res["nc_close"])
        show("用确认链（≈现行）", res["wc_close"])
        print(f"  --- 出场=引擎止盈{cfg['tp']:.0f}%/止损{cfg['sl']:.0f}%，最长{cfg['hold']}日（K线近似）---")
        show("不用确认链", res["nc_rule"])
        show("用确认链（≈现行）", res["wc_rule"])
        print()

    print("⚠️ 口径限制（不得省略）：")
    print("   1. 入场价是「该轮报价」而非成交价；引擎还要过报价路径确认/排板队列/分批/风控。")
    print("   2. E 的 max_peak_change_pct=6.0 未复刻 →「确认链通过」偏多（上界）。")
    print("   3. 出场 K 线近似：同根 K 线同时触及止盈止损时保守按先止损。")
    print("   4. 归档仅 2026-09-07 起，可检验买入日很少，样本量可能不足以下结论。")
    print()
    print("结论：E/F 的样本量**不足以判定** —— 是「证据不足」，不是「负期望」，")
    print("      也不是「该开的没开」。长窗口扫描里 F 的宽口径(n=80)已确认负期望，")
    print("      但 F 现行配置只有 6 个样本、E 现行配置只有 8 个。")
    print("      两条路线都**不应**基于现有证据调整参数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
