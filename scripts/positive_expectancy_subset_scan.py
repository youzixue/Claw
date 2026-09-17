"""正期望子集扫描：把"该不该放宽某个门槛"变成可判定的数字。

!! 口径警告（2026-09-18 实测踩到并更正）!!
------------------------------------------
本脚本用"**次日开盘**买入 → 次日收盘"。这个口径**丢掉隔夜跳空**，
对反包/连板类候选（常高开）会系统性算高收益。
实跑后果：本脚本把 `oversold_reversal_start` 读成"净期望 +0.30%、n=3,394、
CANDIDATE"，而用**引擎自己结算的**真实涨跌（signal 收盘 → outcome 收盘，
n=9,470、两段独立窗口）复核是 **负期望**（合并净 −0.147%，两段不同号）。
因此：
  * 本脚本只用于**同口径下的横向比较**（哪个门槛组合相对更好）；
  * **"能不能开单"必须用 `scripts/route_expectancy_readout.py` 判定**，
    它以引擎结算值为准、且要求两段窗口同号。


背景（为什么需要判据而不是眼睛看均值）
--------------------------------------
本仓库近两周的实测：
  * 连板≥2 无差别次日买入：n=138，均值 −0.67%，胜率 40%
  * `mainline_spread_start` 全候选：n=536，次日 +0.15%、3日 −0.60%
  * `pre_board_probe_start`：n=14,087，次日 −0.04%
  * `oversold_reversal_start`：n=3,394，次日 +0.45%
  * `auction_surge_start`：n=699，次日 −0.50%
在这些量级上，按维度切子集会**必然**切出一些"看着为正"的组合——
这不是发现，是多重比较的产物。所以本脚本对每个子集同时给出
样本量、均值、95%CI（t 分布近似）与**扣摩擦后的净期望**，
并只在 `n >= MIN_SAMPLE` 且净期望 CI 下界 > 0 时才标 `CANDIDATE`。

结论口径
--------
* `CANDIDATE`：样本够、净期望下界为正 → 可以考虑放宽/收紧到该档
* `证据不足`：样本不够 → **不得据此改参**（本仓库大量参数就卡在这一档）
* `负期望`：样本够但净期望为负 → 该档应保持关闭

数据源
------
`stock_kline`（日线，含 amount/volume 可算当日VWAP）、`limit_up_pool`、
`market_sentiment`、`promotion_prediction_snapshot`。全部只读。
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "backend" / "claw.db"
OUT = REPO / "outputs" / "today_review_20260917"

START = "2026-09-01"
END = "2026-09-16"          # 信号日区间（次日入场，故留出下一交易日）
LOOKBACK_FROM = "2026-08-20"

# 往返摩擦：佣金双边 + 印花税 + 滑点，保守取 0.15%
FRICTION_PCT = 0.15
# 最小样本：按 δ=3%、典型 σ≈8% 估，n_min≈53；取 30 作为"可讨论"下限，
# 低于此只报数不下结论。
MIN_SAMPLE = 30
# 判定用的显著性水平（双侧 95%）
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
       8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
       15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086}


def t_crit(n: int) -> float:
    if n <= 1:
        return float("nan")
    if n - 1 in T95:
        return T95[n - 1]
    return 1.96


def load() -> tuple[list[str], dict, list, dict, dict]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    days = [r[0] for r in cur.execute(
        "SELECT DISTINCT trade_date FROM stock_kline WHERE trade_date>=? ORDER BY trade_date",
        (LOOKBACK_FROM,),
    )]
    kline: dict[str, dict] = defaultdict(dict)
    for r in cur.execute(
        "SELECT code, trade_date, open, close, high, low, prev_close, volume, amount "
        "FROM stock_kline WHERE trade_date>=?", (LOOKBACK_FROM,),
    ):
        kline[r["code"]][r["trade_date"]] = dict(r)
    limit_up = cur.execute(
        "SELECT trade_date, code, name, consecutive_days, seal_amount, break_count "
        "FROM limit_up_pool "
        "WHERE trade_date>=? AND trade_date<=? AND quarantined=0", (START, END),
    ).fetchall()
    sentiment = {
        r["trade_date"]: dict(r) for r in cur.execute(
            "SELECT trade_date, advance_decline_ratio, sentiment_cycle, quality_status "
            "FROM market_sentiment WHERE trade_date>=?", (START,),
        )
    }
    tags = {r[0]: r[1] for r in cur.execute("SELECT code, board_tag FROM stock_tags")}
    snapshots = cur.execute(
        "SELECT prediction_trade_date d, code, candidate_route route, raw_probability rp, "
        "       calibrated_probability cp, rank_scope, watch_only, trade_gate_passed, features_json "
        "FROM promotion_prediction_snapshot WHERE prediction_trade_date>=? AND prediction_trade_date<=?",
        (START, END),
    ).fetchall()
    conn.close()
    return days, kline, limit_up, sentiment, tags, snapshots


def forward(kline: dict, days: list[str], code: str, signal: str, horizon: int):
    """次日开盘买入 → 第 horizon 个交易日收盘卖出（百分数）。"""
    seq = [d for d in days if d in kline.get(code, {})]
    if signal not in seq:
        return None
    i = seq.index(signal)
    if i + 1 >= len(seq):
        return None
    entry = kline[code][seq[i + 1]]["open"]
    if not entry or entry <= 0:
        return None
    j = min(i + horizon, len(seq) - 1)
    return (kline[code][seq[j]]["close"] / entry - 1) * 100


def summarise(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = st.mean(values)
    if n == 1:
        return {"n": 1, "mean": mean, "median": mean, "win": 100.0,
                "lo": float("-inf"), "hi": float("inf"), "net": mean - FRICTION_PCT}
    sd = st.stdev(values)
    se = sd / math.sqrt(n)
    half = t_crit(n) * se
    return {
        "n": n, "mean": mean, "median": st.median(values),
        "win": sum(1 for x in values if x > 0) / n * 100,
        "lo": mean - half, "hi": mean + half,
        "net": mean - FRICTION_PCT,
    }


def verdict(summary: dict) -> str:
    if summary.get("n", 0) == 0:
        return "无样本"
    if summary["n"] < MIN_SAMPLE:
        return "证据不足"
    return "CANDIDATE" if summary["net"] and summary["lo"] - FRICTION_PCT > 0 else "负期望"


def style_of(adr) -> str:
    if adr is None:
        return "未知"
    if adr >= 1.5:
        return "一致日"
    if adr >= 0.8:
        return "分歧日"
    return "杀跌日"


def scan_sentence(kline, days, limit_up, sentiment) -> list[dict]:
    """E 族（连板延续）与 F 族（断板反包）的维度扫描。

    入场可行性用**当日最低价**判定：若当日最低涨幅都高于入场上限，
    该候选当天结构上不可能被该门槛接受（necessary condition）。
    """
    results: list[dict] = []
    # ---- E 族：连板 4-8（现行高标）与 3-8（放宽"池子"而非放宽"门槛"）----
    for consec_lo in (4, 3):
      for seal in (1.0, 0.7, 0.5, 0.0):
        for brk in (2, 99):
            for cap in (3.0, 5.0, 99.0):
                vals = []
                for row in limit_up:
                    if not (consec_lo <= int(row["consecutive_days"] or 0) <= 8):
                        continue
                    if (row["seal_amount"] or 0) < seal * 1e8:
                        continue
                    if (row["break_count"] or 0) > brk:
                        continue
                    code, sig = row["code"], row["trade_date"]
                    seq = [d for d in days if d in kline.get(code, {})]
                    if sig not in seq or seq.index(sig) + 1 >= len(seq):
                        continue
                    nb = kline[code][seq[seq.index(sig) + 1]]
                    prev = kline[code][sig]["close"]
                    if not prev:
                        continue
                    if cap < 99 and nb["low"] and (nb["low"] / prev - 1) * 100 > cap:
                        continue          # 当日从未跌到上限以内 → 结构上不可入场
                    v = forward(kline, days, code, sig, 1)
                    if v is not None:
                        vals.append(v)
                results.append({
                    "family": f"E 高标({consec_lo}-8板)",
                    "dims": f"封单≥{seal}亿/炸板≤{brk if brk < 99 else '不限'}/入场涨幅≤{cap if cap < 99 else '不限'}%",
                    "h1": summarise(vals),
                })

    # ---- F 族：断板反包形态 ----
    tag_ok = {c for c in kline}
    def sealed(code: str, day: str) -> bool:
        bar = kline[code].get(day)
        if not bar:
            return False
        prev = bar["prev_close"] or 0
        if not prev:
            return False
        code6 = str(code)
        pct = 0.20 if code6.startswith(("30", "68")) else 0.10
        return (bar["close"] or 0) >= round(prev * (1 + pct), 2) * 0.995

    shapes = []
    for code in tag_ok:
        seq = [d for d in days if d in kline[code]]
        for i in range(1, len(seq)):
            sig = seq[i]
            if sig < START or sig > END:
                continue
            if not sealed(code, sig) or sealed(code, seq[i - 1]):
                continue
            j = i - 2
            last = -1
            while j >= 0:
                if sealed(code, seq[j]):
                    last = j
                    break
                j -= 1
            if last < 0:
                continue
            cons = 1
            cur = last
            while cur > 0 and sealed(code, seq[cur - 1]):
                cons += 1
                cur -= 1
            gap = i - last - 1
            if gap > 3:
                continue
            dip = min((kline[code][seq[k]]["low"] / kline[code][seq[k]]["prev_close"] - 1) * 100
                      for k in range(last + 1, i + 1)
                      if kline[code][seq[k]]["prev_close"])
            vol_ratio = (kline[code][sig]["volume"] or 0) / max(
                1e-9, st.mean([kline[code][seq[k]]["volume"] or 0
                               for k in range(max(0, i - 5), i)]) or 1e-9)
            shapes.append({"code": code, "sig": sig, "cons": cons, "gap": gap,
                           "dip": dip, "vol_ratio": vol_ratio})

    def f_subset(pred) -> list[float]:
        vals = []
        for s in shapes:
            if not pred(s):
                continue
            v = forward(kline, days, s["code"], s["sig"], 1)
            if v is not None:
                vals.append(v)
        return vals

    for cons_min in (3, 2, 1):
        results.append({"family": "F 反包", "dims": f"断板前连板≥{cons_min}",
                        "h1": summarise(f_subset(lambda s, c=cons_min: s["cons"] >= c))})
    results.append({"family": "F 反包", "dims": "断板前连板≥3 且 深跌≤−5%",
                    "h1": summarise(f_subset(lambda s: s["cons"] >= 3 and s["dip"] <= -5.0))})
    results.append({"family": "F 反包", "dims": "断板前连板≥3 且 量比≥1.5",
                    "h1": summarise(f_subset(lambda s: s["cons"] >= 3 and s["vol_ratio"] >= 1.5))})
    results.append({"family": "F 反包", "dims": "断板前连板≥3 且 深跌≤−5% 且 量比≥1.5",
                    "h1": summarise(f_subset(lambda s: s["cons"] >= 3 and s["dip"] <= -5.0
                                             and s["vol_ratio"] >= 1.5))})
    results.append({"family": "F 反包", "dims": "断板前连板≥3 且 断板≤1日",
                    "h1": summarise(f_subset(lambda s: s["cons"] >= 3 and s["gap"] <= 1))})
    return results


def scan_engine(snapshots, kline, days) -> list[dict]:
    """晋级预测引擎首板路线的维度扫描（样本量大，维度来自 features_json）。"""
    results: list[dict] = []
    by_route: dict[str, list] = defaultdict(list)
    seen = set()
    for row in snapshots:
        key = (row["d"], row["code"], row["route"])
        if key in seen:
            continue
        seen.add(key)
        feats = json.loads(row["features_json"] or "{}")
        by_route[row["route"]].append({
            "code": row["code"], "d": row["d"], "cp": row["cp"], "rp": row["rp"],
            "risk": feats.get("market_risk_level"),
            "sector_strength": feats.get("sector_strength_score"),
            "support": feats.get("support_strength_score"),
            "route_score": feats.get("route_score"),
        })

    dims = (
        ("概率分档", lambda x: None if x["cp"] is None else x["cp"], (0.02, 0.04, 0.06)),
        ("市场风险档", lambda x: x["risk"], None),
    )
    for route, items in by_route.items():
        vals_all = [v for it in items if (v := forward(kline, days, it["code"], it["d"], 1)) is not None]
        results.append({"family": f"引擎·{route}", "dims": "全部候选", "h1": summarise(vals_all)})
        # 概率分档
        for cut in (0.02, 0.04, 0.06):
            vals = [v for it in items if (it["cp"] or 0) >= cut
                    and (v := forward(kline, days, it["code"], it["d"], 1)) is not None]
            results.append({"family": f"引擎·{route}", "dims": f"校准概率≥{cut:.2f}", "h1": summarise(vals)})
        # 风险档
        for risk in ("normal", "weak", "hostile"):
            vals = [v for it in items if it["risk"] == risk
                    and (v := forward(kline, days, it["code"], it["d"], 1)) is not None]
            if vals:
                results.append({"family": f"引擎·{route}", "dims": f"风险档={risk}", "h1": summarise(vals)})
    return results


def main() -> int:
    days, kline, limit_up, sentiment, tags, snapshots = load()
    rows = scan_sentence(kline, days, limit_up, sentiment)
    rows += scan_engine(snapshots, kline, days)
    for item in rows:
        item["verdict"] = verdict(item["h1"])

    width = max(len(f"{r['family']} | {r['dims']}") for r in rows)
    print(f"{'子集'.ljust(width)}  {'n':>5} {'均值':>8} {'中位':>8} {'胜率':>6} "
          f"{'净期望':>8} {'95%CI下界(净)':>13} 判定")
    candidates = []
    for r in rows:
        h = r["h1"]
        if not h.get("n"):
            print(f"{r['family'] + ' | ' + r['dims']:<{width}}  {'0':>5} {'-':>8} {'-':>8} {'-':>6} {'-':>8} {'-':>13} {r['verdict']}")
            continue
        net_lo = (h["lo"] - FRICTION_PCT) if math.isfinite(h["lo"]) else float("-inf")
        print(f"{r['family'] + ' | ' + r['dims']:<{width}}  {h['n']:>5} {h['mean']:>+8.2f} "
              f"{h['median']:>+8.2f} {h['win']:>5.0f}% {h['net']:>+8.2f} {net_lo:>+13.2f}  {r['verdict']}")
        if r["verdict"] == "CANDIDATE":
            candidates.append(r)

    print()
    if candidates:
        print("满足判据（n≥%d 且净期望 95%%CI 下界>0）的子集：" % MIN_SAMPLE)
        for r in candidates:
            print(f"  - {r['family']} | {r['dims']}: n={r['h1']['n']} 净期望={r['h1']['net']:+.2f}%")
    else:
        print(f"**没有任何子集同时满足 n≥{MIN_SAMPLE} 且净期望 95%CI 下界>0。**")
        print("按本脚本口径：现有门槛都不应放宽；放宽只会进入'负期望'或'证据不足'。")

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "正期望子集扫描-20260918.md"
    lines = ["# 正期望子集扫描（2026-09-18）", "",
             f"- 信号日区间：{START} → {END}；入场：次日开盘；摩擦：{FRICTION_PCT}%（往返）",
             f"- 判据：`n ≥ {MIN_SAMPLE}` 且**净期望 95%CI 下界 > 0** 才标 CANDIDATE",
             "- 口径提醒：日线近似，未模拟分钟级入场时点与逐笔止损；结果是**筛查**，不是回测。",
             "", "| 子集 | n | 均值 | 中位 | 胜率 | 净期望 | CI下界(净) | 判定 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        h = r["h1"]
        if not h.get("n"):
            lines.append(f"| {r['family']} · {r['dims']} | 0 | - | - | - | - | - | {r['verdict']} |")
            continue
        net_lo = (h["lo"] - FRICTION_PCT) if math.isfinite(h["lo"]) else float("-inf")
        lines.append(f"| {r['family']} · {r['dims']} | {h['n']} | {h['mean']:+.2f}% | "
                     f"{h['median']:+.2f}% | {h['win']:.0f}% | {h['net']:+.2f}% | "
                     f"{net_lo:+.2f}% | {r['verdict']} |")
    if candidates:
        lines += ["", "## 满足判据的子集", ""] + [
            f"- {r['family']} · {r['dims']}：n={r['h1']['n']}，净期望 {r['h1']['net']:+.2f}%"
            for r in candidates
        ]
    else:
        lines += ["", f"**没有任何子集同时满足 n≥{MIN_SAMPLE} 且净期望 95%CI 下界>0。**",
                  "结论：现有门槛不应放宽。"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
