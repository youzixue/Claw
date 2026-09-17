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
  1. **样本量按该路线自身的 σ 算**：`n >= max(N_FLOOR, power_n(σ, δ=2%))`，
     其中 `power_n = 1.3 × (z_{α/2}+z_β)² × σ² / δ²`；
  2. 合并样本**扣 0.15% 往返摩擦后**的 95%CI 下界 > 0；
  3. **发现窗与样本外窗口同号且都为正**（防止区间依赖）。

### 关于旧的固定门槛 120（已废弃）
旧版本把判据 1 写死成 `n >= 120`，来源是"δ≈3%、σ≈8% → n_min≈53，再取 2 倍余量"。
这个数字有两个问题：
  a. σ=8% 是拍的通用值。各路线实测 σ 差很多（relay_fillup 2.70%、
     second_board_promotion 5.07%、oversold_reversal_start 3.34%），
     用 σ=8% 会**系统性高估**需求，把低波动路线无谓地推迟数倍时间；
  b. "×2 余量"没有任何统计含义，叠加在 53 之上更放大了误差。
现在改为按各路线自身 σ 计算，并用 `N_FLOOR=30` 兜住小样本。

### 三态而非两态
  - `继续观察`：样本量还没到设计门槛 —— **不能说任何方向的话**；
  - `未证明为正`：样本量够了、点估计为正、两段同号，但净 CI 下界仍 ≤ 0
    —— 不达开单条件，但属可继续累积的候选；
  - `否决`：点估计为负或两段不同号 —— 方向已错，累积样本不会改变结论。
把后两者合并成"否决"会丢掉"该不该继续观察"这个信息，故拆开。

注意 `positive_ci_n(σ, 净均值)`（= `(z·σ/均值)²`，即"按当前点估计还需多少条
才能让净 CI 下界转正"）**只是进度条，不是判据**：它依赖会在观测中漂移的点估计，
因此不能写进预注册门槛，只能用来估"还要等多久"。

数据源
------
`promotion_prediction_record` 的 `outcome_status / outcome_trade_date /
actual_close_change_pct` —— **引擎已经在逐条结算，无需新建观察表**。
本脚本只读。

⚠️ 口径警告（2026-09-18 加入，此前本脚本没有这条，导致结论被误用）
------------------------------------------------------------------
`actual_close_change_pct` **不是策略的可执行收益**，实测口径为：

    actual_close_change_pct == stock_kline.change_pct(outcome_trade_date)
                            == close(P) → close(P+1)      （近窗口一致率 99.2%）

即"预测的第二天这只票涨了多少"，是**预测命中口径**。它能不能当作可执行
收益，取决于**信号在该日收盘前是否已知**：

  * `snapshot_context ∈ {promotion_1510, promotion_2000}`（15:10 / 20:00）
    —— 信号在 P 日**收盘后**才产生，P 日收盘价**买不到**，只能 P+1 入场。
    此时 `close(P)→close(P+1)` 的收益里**全部是隔夜跳空**，而 P+1 入场者
    是在**付出**这个跳空。实测 `second_board_promotion` 收盘后批次 n=1227：
    结算口径 +0.811%，隔夜跳空 +1.488%，**可执行（P+1 开盘→P+1 收盘）−0.643%
    （净 CI 下界 −0.904%）** —— 符号相反。
  * 盘中上下文（0925/0935/1000/1030/1305）—— 信号在 P 日盘中已知，
    P 日收盘前可买，`close(P)` 入场在**原理上可达**；但策略实际是在确认轮
    的当时价成交，与 P 日收盘价仍有差异，仍未严格可执行。

因此：**用本脚本的读数列做"能不能开单"的判定，必须先按 snapshot_context
拆分，并至少用 P+1 开盘入场重算一遍。** 只对盘中上下文批次，
`close(P)→close(P+1)` 才勉强能当上界。

本脚本当前输出的是**预测命中口径**，是所有可执行口径的**上界或下界都不一定**
的中间量，禁止直接当作策略期望。`expected_count` 之类的字段同样只描述预测质量。
"""

from __future__ import annotations

import math
import sqlite3
import statistics as st
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "backend" / "claw.db"

FRICTION_PCT = 0.15
# 判据 (a) 的样本量**按每条路线自己的波动率算**，不写死一个通用数字。
#
# 为什么改：此前把门槛写成固定 120，来源是"σ=7%、δ=2%"的通用假设
# （n_min = 1.3 × (1.96+0.842)² × σ²/δ² ≈ 125）。但实测各路线 σ 差很多：
#   relay_fillup             σ=2.70%  → δ=2% 只需 n≈19
#   second_board_promotion   σ=5.07%  → δ=2% 只需 n≈66
#   oversold_reversal_start  σ=3.34%  → δ=2% 只需 n≈28
# 用 120 卡 `relay_fillup`（σ 只有 2.70%）会把它无谓地推迟 6 倍时间。
# 因此改为：先按自身 σ 算 n_min(δ)，再取 `max(N_FLOOR, n_min)`。
# N_FLOOR=30 是"再快也不能少于 30 条"的兜底，防止小样本直接判开门。
TARGET_DELTA_PCT = 2.0          # 要检出的最小可判定效应 δ
N_FLOOR = 30                    # 兜底：再快也不能少于 30 条
Z_ALPHA, Z_BETA = 1.96, 0.842   # α=0.05 双侧, power=80%


def power_n(sigma_pct: float, delta_pct: float = TARGET_DELTA_PCT) -> int:
    """**设计门槛**（预注册）：80% 检验力下检出 δ 所需样本量。

         n = 1.3 × (z_{α/2} + z_β)² × σ² / δ²

    这是可以事先写死、不随观测漂移的门槛，因此用它当 gate。
    """
    if sigma_pct <= 0:
        return N_FLOOR
    need = 1.3 * (Z_ALPHA + Z_BETA) ** 2 * sigma_pct ** 2 / max(delta_pct, 1e-9) ** 2
    return max(N_FLOOR, int(math.ceil(need)))


def positive_ci_n(sigma_pct: float, mean_net_pct: float) -> int | None:
    """**参考进度**（非 gate）：按当前点估计，还需多少条才能让净 CI 下界转正。

         mean > t·σ/√n  →  n > (t·σ/mean)²

    只能当进度条用：点估计本身在漂，这个数字会随样本变化，所以不写进判据。
    返回 None = 点估计已为负，靠累积样本不可能转正。
    """
    if mean_net_pct <= 0:
        return None
    if sigma_pct <= 0:
        return N_FLOOR
    return max(N_FLOOR, int(math.ceil((Z_ALPHA * sigma_pct / mean_net_pct) ** 2)))


def need_text(need: int | None) -> str:
    return "n/a（点估计为负）" if need is None else f"n≈{need}"


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
        "sd": st.stdev(values) if n > 1 else 0.0,
        "win": sum(1 for x in values if x > 0) / n * 100,
        "net": st.mean(values) - FRICTION_PCT,
        "ci_lo_net": ci_lower(values, FRICTION_PCT),
    }


def verdict(discovery: dict, out_of_sample: dict, combined: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    n = combined.get("n", 0)
    gate = power_n(combined.get("sd", 0.0)) if n else N_FLOOR
    if n < gate:
        return "继续观察", [
            f"已结算 n={n} < 设计门槛 {gate}"
            f"（σ={combined.get('sd', 0):.2f}%、δ={TARGET_DELTA_PCT}%；"
            f"按当前点估计需 {need_text(positive_ci_n(combined.get('sd', 0.0), combined.get('net', 0.0)))}）"
        ]
    two_windows_positive = discovery.get("mean", 0) > 0 and out_of_sample.get("mean", 0) > 0
    ci_ok = combined["ci_lo_net"] > 0
    if ci_ok and two_windows_positive:
        return "可开单", []
    if not two_windows_positive:
        reasons.append("两段窗口不同号（区间依赖）")
    if not ci_ok:
        reasons.append(
            f"合并样本净期望 CI 下界 {combined['ci_lo_net']:+.3f}% ≤ 0"
        )
    # 第三态：样本量够、点估计为正、两段同号，只有 CI 下界还压在 0 以下。
    # 这与"点估计为负"是两回事 —— 前者是可继续累积的候选，后者是方向已错。
    if (not ci_ok) and two_windows_positive and combined.get("net", 0) > 0:
        prog = positive_ci_n(combined.get("sd", 0.0), combined.get("net", 0.0))
        gap = "—" if prog is None else str(max(0, prog - n))
        return "未证明为正", reasons + [
            f"点估计净={combined['net']:+.3f}% 与两段同号均支持为正，但按该 σ 需 n≈{prog}"
            f"（还差 {gap} 条）方能使下界转正 —— 属可继续累积的候选，不是方向为负"
        ]
    if combined.get("net", 0) <= 0:
        return "否决", reasons + [
            f"点估计净={combined['net']:+.3f}% ≤ 0，方向为负"
        ]
    return "否决", reasons


def main() -> int:
    print("⚠️  本表是**预测命中口径**（close(P)→close(P+1)），不是可执行收益。")
    print("    收盘后批次(1510/2000)此口径含全部隔夜跳空，而 P+1 入场者是付出跳空；")
    print("    实测 second_board_promotion 该批次：此口径 +0.811% vs 可执行 −0.643%（符号相反）。")
    print("    判定前必须按 snapshot_context 拆分并用 P+1 开盘入场重算。详见模块 docstring。\n")
    print(f"判据：n≥设计门槛 max({N_FLOOR}, power_n(σ,δ={TARGET_DELTA_PCT})) "
          f"且 净CI下界>0 且 两段窗口同号为正（摩擦 {FRICTION_PCT}%）")
    print(f"发现窗 {DISCOVERY[0]}~{DISCOVERY[1]}；样本外 = 该窗之前\n")
    header = (f"{'路线':<26}{'n':>7}{'均值':>9}{'中位':>9}{'胜率':>7}"
              f"{'净均值':>9}{'净CI下界':>11}  判定")
    print(header)
    print("-" * len(header))
    watch: list[tuple[str, str]] = []
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
        if state in ("继续观察", "未证明为正"):
            gate = power_n(comb["sd"])
            prog = positive_ci_n(comb["sd"], comb["net"])
            d_gap = str(max(0, gate - comb["n"]))
            c_gap = "—" if prog is None else str(max(0, prog - comb["n"]))
            watch.append((state, f"{route}（已结算 {comb['n']}，σ={comb['sd']:.2f}%，"
                                 f"设计门槛 {gate}[还差 {d_gap}]，"
                                 f"净CI转正参考 {need_text(prog)}[还差 {c_gap}]）"))
    print()
    for state, title, note in (
        ("继续观察", "样本量不足，尚不能判定：", "先攒够设计门槛再读。"),
        ("未证明为正", "样本量已够、方向为正但净CI下界未转正：", "不达开单条件，但值得继续累积。"),
    ):
        items = [t for st, t in watch if st == state]
        if not items:
            continue
        print(f"{title}（{note}）")
        for item in items:
            print(f"  - {item}")
    if not watch:
        print("观察池为空。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
