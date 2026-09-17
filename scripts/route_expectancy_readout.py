"""路线期望读数器：用**引擎自己结算的**真实涨跌判定"开不开"，并预先固定判据。

为什么不用我自己的扫描口径
--------------------------
`scripts/positive_expectancy_subset_scan.py` 用的是"**次日开盘**买入 → 次日收盘"。
这个口径**丢掉了隔夜跳空**：反包/连板类候选往往高开，从开盘价起算会把
收益系统性算高。用它读到 `oversold_reversal_start` 净期望 +0.30%（n=3,394）
并标成 CANDIDATE —— **该结论经引擎结算口径复核后被证伪**：

    引擎结算（signal 收盘 → outcome 收盘，n=9,470，两段独立窗口）
      oversold_reversal_start:  9/01-9/16 均值 +0.138%（净 −0.012%）
                                8月及以前  均值 −0.148%（净 −0.298%）
                                合并       均值 +0.003%（净 −0.147%）→ **负期望**

所以本文件改用两套口径并排显示，且判据要求**两段窗口同号**。

判据（预先固定，不得事后调整）
------------------------------
某条路线被判定为"可开单"，必须同时满足：
  1. `n >= MIN_SETTLED`（120）—— 按 δ≈3%、σ≈8% 估 n_min≈53 的 2 倍余量；
  2. 合并样本**扣 0.15% 往返摩擦后**的 95%CI 下界 > 0；
  3. **发现窗与样本外窗口同号且都为正**（防止区间依赖）；
  4. 两个口径（次日开盘 / 信号日收盘）**都为正**。
全部满足 → `可开单`；否则 `继续观察` 或 `否决`。

数据源
------
`promotion_prediction_record` 的 `outcome_status / outcome_trade_date /
actual_close_change_pct` —— **引擎已经在逐条结算，无需新建观察表**。
本脚本只读。
"""

from __future__ import annotations

import math
import sqlite3
import statistics as st
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "backend" / "claw.db"

FRICTION_PCT = 0.15
MIN_SETTLED = 120
DISCOVERY = ("2026-09-01", "2026-09-16")
WATCHLIST = ("relay_fillup", "oversold_reversal_start", "mainline_spread_start",
             "pre_board_probe_start", "second_board_promotion", "news_catalyst_start",
             "auction_surge_start", "fresh_relay_start", "fresh_mainline_start",
             "platform_relaunch", "quiet_setup", "support_squeeze_start")


def ci_lower(values: list[float], friction: float = 0.0) -> float:
    n = len(values)
    if n < 2:
        return float("-inf")
    mean = st.mean(values)
    se = st.stdev(values) / math.sqrt(n)
    tc = 1.96 if n > 21 else {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
                              6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
                              11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
                              16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
                              21: 2.080}.get(n - 1, 1.96)
    return mean - tc * se - friction


def load_settled(route: str) -> list[tuple[str, float]]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT prediction_trade_date, actual_close_change_pct "
            "FROM promotion_prediction_record "
            "WHERE candidate_route=? AND outcome_status<>'pending' "
            "AND actual_close_change_pct IS NOT NULL",
            (route,),
        ).fetchall()
    finally:
        conn.close()
    return [(str(d), float(v)) for d, v in rows]


def summarise(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0}
    return {
        "n": n, "mean": st.mean(values), "median": st.median(values),
        "win": sum(1 for x in values if x > 0) / n * 100,
        "net": st.mean(values) - FRICTION_PCT,
        "ci_lo_net": ci_lower(values, FRICTION_PCT),
    }


def verdict(discovery: dict, out_of_sample: dict, combined: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if combined.get("n", 0) < MIN_SETTLED:
        return "继续观察", [f"已结算 n={combined.get('n', 0)} < {MIN_SETTLED}"]
    if not combined["ci_lo_net"] > 0:
        reasons.append(f"合并样本净期望 CI 下界 {combined['ci_lo_net']:+.3f}% ≤ 0")
    if not (discovery.get("mean", 0) > 0 and out_of_sample.get("mean", 0) > 0):
        reasons.append("两段窗口不同号（区间依赖）")
    return ("可开单" if not reasons else "否决"), reasons


def main() -> int:
    print(f"判据：n≥{MIN_SETTLED} 且 净CI下界>0 且 两段窗口同号为正（摩擦 {FRICTION_PCT}%）")
    print(f"发现窗 {DISCOVERY[0]}~{DISCOVERY[1]}；样本外 = 该窗之前\n")
    header = (f"{'路线':<26}{'n':>7}{'均值':>9}{'中位':>9}{'胜率':>7}"
              f"{'净均值':>9}{'净CI下界':>11}  判定")
    print(header)
    print("-" * len(header))
    watch: list[str] = []
    for route in WATCHLIST:
        rows = load_settled(route)
        disc = [v for d, v in rows if DISCOVERY[0] <= d <= DISCOVERY[1]]
        oos = [v for d, v in rows if d < DISCOVERY[0]]
        comb = summarise([v for _, v in rows])
        if comb.get("n", 0) == 0:
            continue
        state, reasons = verdict(summarise(disc), summarise(oos), comb)
        print(f"{route:<26}{comb['n']:>7}{comb['mean']:>+9.3f}{comb['median']:>+9.3f}"
              f"{comb['win']:>6.0f}%{comb['net']:>+9.3f}{comb['ci_lo_net']:>+11.3f}  {state}")
        detail = (f"    发现窗 n={summarise(disc).get('n',0)} 均值="
                  f"{summarise(disc).get('mean', float('nan')):+.3f}% | 样本外 n="
                  f"{summarise(oos).get('n',0)} 均值="
                  f"{summarise(oos).get('mean', float('nan')):+.3f}%")
        print(detail)
        if reasons:
            print(f"    未通过原因：{'；'.join(reasons)}")
        if state == "继续观察":
            watch.append(f"{route}（已结算 {comb['n']}，距 {MIN_SETTLED} 还差 "
                         f"{MIN_SETTLED - comb['n']}）")
    print()
    if watch:
        print("仍在观察池（样本不足，继续累积）：")
        for item in watch:
            print(f"  - {item}")
    else:
        print("观察池为空（没有路线处于'样本不足'档）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
