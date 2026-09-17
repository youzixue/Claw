"""策略边际验证脚本（2026-09-17 复盘行动项⑤）。

用途
----
把"某个策略是否存在可用边际"从拍脑袋门槛改成**统计功效可辩护**的判定，
并用同一份口径核验 9/17 行动项 ①②③ 的效果。

本脚本固化了两处此前踩过的坑
--------------------------
1. **样本必须按"买入周期"切分**，不能拿 `paper_trade_log` 的卖出流水条数当样本数。
   A 股 T 机制产生部分平仓（T 减仓），一笔持仓会有多条卖出记录；
   若每条都用 `realized_pnl / 买入总成本`，会 ① 同一持仓重复计数（虚增 n）
   ② 收益率被按卖出比例缩小（压缩方差）。
2. **运营性强制退出必须显式剔除**，它们是运维事件、不是策略决策，
   按最新价强平、无视策略的止盈止损。
   识别方式：**枚举全部退出原因后显式分类**，不使用子串猜测
   （此前用子串 '版本隔离' 漏掉了 "旧版或未标版本仓位隔离退出"——
   "版本"与"隔离"被"仓位"隔开）。

判据
----
    n_min = 1.3 × 7.849 × σ² / δ²        （双侧 α=0.05、功效 80%、含 1.3× 日内相关余量）
    t     = mean / (σ/√n)
    通过   = n ≥ n_min 且 t ≥ 2.0 且 mean > 0
淘汰门：n ≥ n_min 且 (t ≤ -2 或 mean 的 95% 上界 < +1%)  → 明确无效
晋级门：n ≥ n_min(δ=2%) 且 t ≥ 2.0 且 mean > 0           → 可给真实资金

用法
----
    cd backend && python ../scripts/validate_strategy_edge.py
    python ../scripts/validate_strategy_edge.py --delta 3.0 --since 2026-09-01
    python ../scripts/validate_strategy_edge.py --account 12 --cut-winners
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

# ── 运营性强制退出：显式枚举，不用子串猜测 ────────────────────────────────
# 新增文案时必须在此登记（脚本会在出现未登记的原因时提示）。
OPERATIONAL_EXIT_PATTERNS = (
    "旧版或未标版本仓位隔离退出",
    "[系统清理]",
)

# ── 弱信号 rung：2026-09-17 行动项②③ 受控的 7 个（用于验收指标） ──────────
WEAK_EXIT_RUNGS = (
    "回落成本线保护", "盘中冲高回落", "盘中收弱",
    "跌破分时均价", "跌破开盘价", "次日不强就走", "跌破5日线",
)
SELF_CORROBORATING_RUNGS = (
    "5分钟急跌", "盘口卖压增强", "放量阴线", "板块退潮",
    # 昨日涨停次日转弱：prev_was_limit_up 专用分支，不由弱信号门槛控制
    "昨日涨停次日转弱",
)

Z_ALPHA, Z_BETA = 1.96, 0.842          # 双侧 α=0.05、功效 80%
CORRELATION_MARGIN = 1.3               # 日内相关保守余量
# 注意：转正线针对"整体平均盈利"，不是"被砍赢家自身"。脚本会分别计算。
# 实测参考（n=49）：转正线 ≈ +7.52%，对应被砍赢家需达 ≈ +5.9%。


def load_positions(db_path: Path, *, since: str | None = None,
                   account_id: int | None = None) -> tuple[list[dict], list[str]]:
    """按买入周期切分持仓，返回 (持仓列表, 未登记的退出原因)。"""
    con = sqlite3.connect(str(db_path))
    sql = """SELECT account_id, code, trade_type, amount, price, trade_time, realized_pnl, reason
             FROM paper_trade_log WHERE trade_type IN ('buy','sell')"""
    params: list = []
    if since:
        sql += " AND trade_time >= ?"
        params.append(since)
    sql += " ORDER BY account_id, code, trade_time"
    rows = con.execute(sql, params).fetchall()
    con.close()

    cycles: dict[tuple, list[dict]] = defaultdict(list)
    for acc, code, tt, amt, px, t, pl, reason in rows:
        if account_id is not None and acc != account_id:
            continue
        key = (acc, code)
        if tt == "buy":
            cycles[key].append({"buys": [(amt or 0, px or 0)], "sells": []})
        elif cycles[key]:
            cycles[key][-1]["sells"].append(
                {"amount": amt or 0, "price": px or 0, "pnl": pl or 0,
                 "reason": str(reason or ""), "at": str(t)}
            )

    positions: list[dict] = []
    unregistered: Counter = Counter()
    for (acc, code), group in cycles.items():
        for cyc in group:
            if not cyc["sells"]:
                continue                       # 未平仓，不计入已完成样本
            cost = sum(a * p for a, p in cyc["buys"])
            if cost <= 0:
                continue
            reasons = [s["reason"] for s in cyc["sells"]]
            operational = any(pat in r for r in reasons for pat in OPERATIONAL_EXIT_PATTERNS)
            if not operational:
                # 未登记的"疑似运营性"原因提示：既非策略信号、也不含止盈止损关键词
                for r in reasons:
                    head = r.split("：")[0][:24]
                    known = (any(k in r for k in WEAK_EXIT_RUNGS)
                             or any(k in r for k in SELF_CORROBORATING_RUNGS)
                             or any(k in r for k in ("止损", "止盈", "到期平仓", "未转强")))
                    if not known:
                        unregistered[head] += 1
            positions.append({
                "account_id": acc,
                "code": code,
                "cost": cost,
                "pnl": sum(s["pnl"] for s in cyc["sells"]),
                "pct": sum(s["pnl"] for s in cyc["sells"]) / cost * 100,
                "operational": operational,
                "tp_hit": any("止盈" in r for r in reasons),
                "weak_exit": any(k in r for r in reasons for k in WEAK_EXIT_RUNGS),
                "t_mechanism": any(("T减仓" in r or "T后保护" in r) for r in reasons),
                "last_reason": reasons[-1],
                "sold_at": cyc["sells"][-1]["at"],
            })
    return positions, [f"{k} (x{v})" for k, v in unregistered.most_common()]


def required_n(sigma: float, delta: float) -> float:
    return CORRELATION_MARGIN * (Z_ALPHA + Z_BETA) ** 2 * sigma ** 2 / delta ** 2


def describe(values: list[float]) -> dict:
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v <= 0]
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "sigma": statistics.stdev(values) if len(values) > 1 else 0.0,
        "win_rate": len(wins) / len(values) if values else 0.0,
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": abs(statistics.mean(losses)) if losses else 0.0,
    }


def verdict(stats: dict, delta: float) -> tuple[str, dict]:
    sigma = stats["sigma"]
    n = stats["n"]
    if n < 2 or sigma <= 0:
        return "样本不足", {}
    mean, se = stats["mean"], sigma / math.sqrt(n)
    t = mean / se
    lo, hi = mean - Z_ALPHA * se, mean + Z_ALPHA * se
    n_min = required_n(sigma, delta)
    payoff = stats["avg_win"] / stats["avg_loss"] if stats["avg_loss"] else float("inf")
    break_even = (stats["avg_loss"] / (stats["avg_win"] + stats["avg_loss"]) * 100
                  if (stats["avg_win"] + stats["avg_loss"]) else 0.0)
    detail = {"t": t, "ci95": (lo, hi), "n_min": n_min, "payoff": payoff,
              "break_even_win_rate": break_even}
    if n < n_min:
        return "样本不足", detail
    if t >= 2.0 and mean > 0:
        return "晋级门通过", detail
    if t <= -2.0 or hi < 1.0:
        return "淘汰门拒绝", detail
    return "不显著（不出结论）", detail


def main() -> int:
    ap = argparse.ArgumentParser(description="策略边际验证（持仓周期口径）")
    ap.add_argument("--delta", type=float, default=2.0, help="想检测的单笔边际 %%（默认 2.0）")
    ap.add_argument("--since", default=None, help="只统计该日期之后的平仓（YYYY-MM-DD）")
    ap.add_argument("--account", type=int, default=None, help="只看某个账户 id")
    ap.add_argument("--cut-winners", action="store_true", help="输出被软信号砍掉的盈利持仓明细")
    args = ap.parse_args()

    positions, unregistered = load_positions(BACKEND / "claw.db", since=args.since,
                                             account_id=args.account)
    if not positions:
        print("无已完成持仓样本")
        return 1
    clean = [p for p in positions if not p["operational"]]
    op_n = len(positions) - len(clean)

    print("=" * 96)
    print("样本构造（按买入周期切分；运营性强制退出显式剔除）")
    print("=" * 96)
    print(f"  持仓周期 {len(positions)} 个；运营性强制退出 {op_n} 个"
          f"（{op_n / len(positions) * 100:.1f}%）→ 干净样本 {len(clean)} 个")
    if unregistered:
        print(f"  ⚠️ 未登记的退出原因（请检查是否应计入运营性事件）：{'; '.join(unregistered)}")

    all_stats = describe([p["pct"] for p in clean])
    label, detail = verdict(all_stats, args.delta)
    print(f"\n  干净样本：σ={all_stats['sigma']:.2f}%  均值={all_stats['mean']:+.2f}%  "
          f"胜率={all_stats['win_rate'] * 100:.1f}%")
    print(f"  平均盈利={all_stats['avg_win']:+.2f}%  平均亏损={all_stats['avg_loss']:.2f}%")
    if detail:
        print(f"  盈亏比={detail['payoff']:.3f}  平衡胜率={detail['break_even_win_rate']:.2f}%")
        print(f"  t={detail['t']:+.2f}  95%CI=[{detail['ci95'][0]:+.2f}%, {detail['ci95'][1]:+.2f}%]"
              f"  n_min(δ={args.delta}%)={detail['n_min']:.0f}")

    print("\n" + "=" * 96)
    print(f"逐策略判定（δ={args.delta}%，含 1.3× 日内相关余量；双侧 α=0.05、功效 80%）")
    print("=" * 96)
    print(f"  {'账户':>4}{'n':>4}{'σ%':>7}{'均值%':>8}{'胜率%':>7}{'盈亏比':>8}"
          f"{'平衡胜率%':>10}{'t':>7}{'n_min':>7}{'判定':>16}")
    by_account: dict[int, list[float]] = defaultdict(list)
    for p in clean:
        by_account[p["account_id"]].append(p["pct"])
    for acc in sorted(by_account):
        values = by_account[acc]
        if len(values) < 2:
            print(f"  {acc:>4}{len(values):>4}{'—':>7}{'—':>8}{'—':>7}{'—':>8}{'—':>10}{'—':>7}{'—':>7}{'样本不足':>16}")
            continue
        st = describe(values)
        lb, dt = verdict(st, args.delta)
        payoff = f"{dt['payoff']:.3f}" if dt else "—"
        print(f"  {acc:>4}{st['n']:>4}{st['sigma']:>7.2f}{st['mean']:>+8.2f}"
              f"{st['win_rate'] * 100:>7.1f}{payoff:>8}"
              f"{(dt['break_even_win_rate'] if dt else 0):>10.2f}"
              f"{(dt['t'] if dt else 0):>+7.2f}{(dt['n_min'] if dt else 0):>7.0f}{lb:>16}")

    # ── 验收指标：行动项 ①②③ 的效果（被软信号砍掉的盈利持仓） ──────────
    winners = [p for p in clean if p["pct"] > 0]
    cut = [p for p in winners if not p["tp_hit"] and p["weak_exit"]]
    tp = [p for p in winners if p["tp_hit"]]
    print("\n" + "=" * 96)
    print("验收指标（行动项 ①②③）：被软信号砍掉的盈利持仓")
    print("=" * 96)
    if winners:
        cut_avg = statistics.mean([p["pct"] for p in cut]) if cut else 0.0
        tp_avg = statistics.mean([p["pct"] for p in tp]) if tp else 0.0
        print(f"  盈利持仓 {len(winners)} 个：触及止盈 {len(tp)} 个（均值 {tp_avg:+.2f}%），"
              f"被软信号砍掉 {len(cut)} 个（均值 {cut_avg:+.2f}%）")
        print(f"  被砍占比 = {len(cut) / len(winners) * 100:.0f}%   （修复前实测基线 12/21 = 57% 口径见文档）")
        print(f"  其中涉及 T 机制 = {sum(1 for p in cut if p['t_mechanism'])}/{len(cut)}")
        print(f"  微利(<+1%)被砍 = {sum(1 for p in cut if p['pct'] < 1)}/{len(cut)}")
        # 转正线针对的是**整体平均盈利**，不是被砍赢家自身：
        #   aw_need = al × (1−w)/w          由实测盈亏比与胜率推导
        #   被砍赢家所需均值 = (aw_need×N_win − tp_avg×N_tp) / N_cut
        n_all, n_tp, n_cut = len(winners), len(tp), len(cut)
        w_rate = len(winners) / len(clean)
        aw_now = statistics.mean([p["pct"] for p in winners])
        aw_need = all_stats["avg_loss"] * (1 - w_rate) / w_rate
        cut_need = ((aw_need * n_all - tp_avg * n_tp) / n_cut) if n_cut else 0.0
        print(f"\n  整体平均盈利 {aw_now:+.2f}%  vs  转正线 {aw_need:+.2f}%"
              f"  →  缺口 {aw_need - aw_now:+.2f}pp")
        print(f"  被砍赢家当前 {cut_avg:+.2f}%  →  需要 {cut_need:+.2f}%"
              f"（止盈组保持 {tp_avg:+.2f}% 不变的前提下）")
        if cut_avg >= cut_need:
            print("  ✅ 已达验收线")
        else:
            print(f"  ⏳ 未达验收线：被砍赢家均值需从 {cut_avg:+.2f}% 提到 {cut_need:+.2f}%"
                  f"（缺口 {cut_need - cut_avg:+.2f}pp）")
        if args.cut_winners and cut:
            print("\n  明细：")
            for p in cut:
                print(f"    账户{p['account_id']:<4}{p['code']}  {p['pct']:>+7.2f}%  "
                      f"T={'Y' if p['t_mechanism'] else 'N'}  {p['last_reason'][:40]}")
    else:
        print("  无盈利持仓样本")

    print(f"\n提示：δ 越小需要样本越多。σ=6.85% 时 δ=3% 约需 "
          f"{required_n(6.85, 3.0):.0f} 笔、δ=2% 约需 {required_n(6.85, 2.0):.0f} 笔。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
