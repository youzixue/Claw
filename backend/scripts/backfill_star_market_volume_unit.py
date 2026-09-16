#!/usr/bin/env python3
"""科创板成交量/成交额 100 倍口径错误的**只读审计**工具。

⚠️ 写入已被禁用（2026-09-16）
-----------------------------
本脚本**不提供** `UPDATE stock_kline` 的能力。原因：该做法违反项目现行 K 线数据治理
原则，与仓库既有决策直接冲突：

1. `stock_kline` 是 **`stock_kline_observation`（append-only 证据表）的当日投影**，
   只有 `disposition == 'current_projection'` 的行才会被投影
   （`app/data/kline_observations.py:174-176`）。
2. 历史日期（`trade_date < today`）被显式判为 **`isolated_historical` → 只存证、不投影**
   （`app/data/kline_observations.py:148-149`）。
3. `app/data/scheduler.py::_ths_kline_recent_repair` 文档字符串明确：
   **「历史差异只入隔离版本、不覆盖」**。
4. `backend/scripts/repair_stock_kline_derived.py` 的既有先例：
   **「旧 --apply 已禁用，必须通过隔离版本核查，不能按邻价覆盖历史」**
   —— 其 `--apply` 分支直接 `raise SystemExit`。

因此本脚本与 `repair_stock_kline_derived.py` 保持一致：**只读审计 + 明确拒绝写入**。

背景（2026-09-16 复盘定位）
---------------------------
腾讯 `qt.gtimg.cn` 字段[6]「成交量」的单位按板块不同：
    主板(60/00) / 创业板(30) → 「手」
    科创板(688/689)         → 「股」
`app/data/sources/tencent_source.py` 修复前统一按「手」处理，于是科创板：
    stock_spot.volume   = 真实股数            （被当作「手」）
    stock_spot.amount   = 均价 × 真实股数 × 100（虚高 100 倍）
随后 spot→kline 补全再放大一次：
    stock_kline.volume  = 真实股数 × 100      （虚高 100 倍）
    stock_kline.amount  = 真实金额 × 100      （虚高 100 倍）

**数据源侧已在 `tencent_source.py` 修复**，因此不会再产生新的污染行。

本工具的用途
------------
1. 精确列出**已落库**的受影响行（区间、标的、行数、可释放的量级修正系数），
   供消费端（复盘统计、因子计算、回测）自行决定「排除」或「应用修正系数」；
2. 输出机器可读清单，便于在读取侧做口径修正，而**不触碰历史投影字段**。

如需真正修正历史，正确路径是**追加新的观察证据并扩展投影机制**
（新增 disposition 允许修正投影参与 `stock_kline`），属架构改动，需单独评审，
不在本脚本范围内。

用法
----
    python3 scripts/backfill_star_market_volume_unit.py --from 2026-09-02
    python3 scripts/backfill_star_market_volume_unit.py --from 2026-09-02 --json out.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "claw.db"
STAR_PREFIX = "68"
# 已知污染起始日：2026-09-02（此前 source='ths' 写入的数据正确）。
# 该日期只作默认下界，真正判定仍以换手率交叉校验为准。
DEFAULT_FROM = "2026-09-02"

# 交叉校验容差：相对误差 ≤ 该值视为吻合
MATCH_TOLERANCE = 0.05
# 判定「虚高 100 倍」时，按 100 倍解释的相对误差上限
INFLATED_TOLERANCE = 0.05


@dataclass
class Verdict:
    row_id: int
    code: str
    trade_date: str
    volume: int
    amount: float
    ratio_as_is: float | None      # 按现状解释 / 基准
    ratio_div100: float | None     # 按÷100解释 / 基准
    action: str                    # fix | keep | review
    basis: str = ""                # 判定所用基准


def _relative_error(a: float, b: float) -> float:
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b)


def classify(
    *, volume: float, turnover: float, basis_float_shares: float
) -> tuple[float | None, float | None, str]:
    """按「推算流通股本」判定 volume 是否需要 ÷100。

    判据：正确口径下，volume 与 turnover 推算出的流通股本应当稳定：
        implied_float = volume / (turnover / 100)
    把它与基准流通股本相比：
        as_is  ≈ 1   → volume 已正确
        as_is  ≈ 100 → volume 虚高 100 倍，需 ÷100
    """
    if not volume or not turnover or turnover <= 0 or not basis_float_shares:
        return None, None, "review"
    implied_float = volume / (turnover / 100.0)
    as_is = implied_float / basis_float_shares
    div100 = (implied_float / 100.0) / basis_float_shares
    if _relative_error(div100, 1.0) <= INFLATED_TOLERANCE and as_is > 10:
        return as_is, div100, "fix"
    if _relative_error(as_is, 1.0) <= MATCH_TOLERANCE:
        return as_is, div100, "keep"
    return as_is, div100, "review"


def load_float_shares(conn: sqlite3.Connection) -> dict[str, float]:
    """基准①：今日快照流通股本（股）= 流通市值(亿元) × 1e8 / 价格。

    ⚠️ 该基准对**次新股**不可靠：新股解禁/增发会在数日内改变流通股本，
    用「今日」股本校验「历史」换手率会误判。因此它只作首选基准，
    review 行会回落到基准②（见 load_ths_implied_float）。
    """
    shares: dict[str, float] = {}
    rows = conn.execute(
        "SELECT code, price, circ_market_cap FROM stock_spot "
        "WHERE price > 0 AND circ_market_cap > 0"
    ).fetchall()
    for code, price, cap in rows:
        shares[str(code)] = float(cap) * 1e8 / float(price)
    return shares


def load_ths_implied_float(
    conn: sqlite3.Connection, *, before: str
) -> dict[str, float]:
    """基准②：每个标的在 `before` 之前最近一条 **ths 源**记录推算的流通股本（股）。

    ths 源（同花顺日K）的 volume 单位是「股」，tap 换手率口径一致，
    因此 `volume / (turnover/100)` 就是该日点的真实流通股本。

    该基准与待校验行**同为历史时点**，不受新股解禁影响，是次新股场景的正确参照。
    """
    out: dict[str, float] = {}
    rows = conn.execute(
        "SELECT code, volume, turnover, trade_date FROM stock_kline "
        "WHERE source = 'ths' AND trade_date < ? AND turnover > 0 AND volume > 0 "
        "ORDER BY code, trade_date",
        (before,),
    ).fetchall()
    for code, volume, turnover, _date in rows:
        out[str(code)] = float(volume) / (float(turnover) / 100.0)
    return out


def audit(conn: sqlite3.Connection, *, date_from: str, date_to: str | None) -> list[Verdict]:
    where = ["k.code LIKE ?", "k.trade_date >= ?"]
    params: list[object] = [f"{STAR_PREFIX}%", date_from]
    if date_to:
        where.append("k.trade_date <= ?")
        params.append(date_to)
    # 只处理腾讯写入的行；ths 源数据单位正确，不得改动。
    where.append("k.source IN ('tencent_close', 'spot_fallback')")
    sql = (
        "SELECT k.id, k.code, k.trade_date, k.volume, k.amount, k.turnover "
        f"FROM stock_kline k WHERE {' AND '.join(where)} ORDER BY k.trade_date, k.code"
    )
    float_shares = load_float_shares(conn)
    ths_float = load_ths_implied_float(conn, before=date_from)
    out: list[Verdict] = []
    for row_id, code, trade_date, volume, amount, turnover in conn.execute(sql, params):
        code = str(code)
        volume = float(volume or 0)
        amount = float(amount or 0.0)
        turnover = float(turnover or 0.0)
        # 基准①（今日快照）→ 基准②（同源历史时点，对次新股可靠）
        for basis_name, basis in (
            ("stock_spot", float_shares.get(code)),
            ("ths_implied", ths_float.get(code)),
        ):
            if not basis:
                continue
            as_is, div100, action = classify(
                volume=volume, turnover=turnover, basis_float_shares=basis
            )
            if action in ("fix", "keep"):
                out.append(Verdict(row_id, code, str(trade_date), int(volume),
                                   amount, as_is, div100, action, basis_name))
                break
        else:
            as_is, div100, _ = classify(
                volume=volume, turnover=turnover,
                basis_float_shares=float_shares.get(code) or 0.0,
            )
            out.append(Verdict(row_id, code, str(trade_date), int(volume),
                               amount, as_is, div100, "review", "none"))
    return out


def report(verdicts: list[Verdict]) -> dict[str, int]:
    counts = {"fix": 0, "keep": 0, "review": 0}
    for item in verdicts:
        counts[item.action] += 1
    total = sum(counts.values())
    print(f"扫描 {total} 行（科创板 + 腾讯源）")
    print(f"  需修正 (fix)    : {counts['fix']}")
    print(f"  已正确 (keep)   : {counts['keep']}")
    print(f"  需人工复核      : {counts['review']}")
    print()
    fixes = [v for v in verdicts if v.action == "fix"]
    if fixes:
        from collections import Counter
        by_basis = Counter(v.basis for v in fixes)
        print(f"判定基准分布: {dict(by_basis)}")
        print("需修正样本（前 8 行）：")
        print(f"  {'code':10}{'date':12}{'volume':>16}{'按现状比':>10}{'按÷100比':>10}  基准")
        for v in fixes[:8]:
            print(f"  {v.code:10}{v.trade_date:12}{v.volume:>16}"
                  f"{(v.ratio_as_is or 0):>10.1f}{(v.ratio_div100 or 0):>10.3f}  {v.basis}")
    keeps = [v for v in verdicts if v.action == "keep"]
    if keeps:
        print()
        print("已正确样本（前 3 行，确认未被误改）：")
        for v in keeps[:3]:
            print(f"  {v.code:10}{v.trade_date:12}{v.volume:>16}"
                  f"{(v.ratio_as_is or 0):>10.3f}")
    reviews = [v for v in verdicts if v.action == "review"]
    if reviews:
        print()
        print(f"需复核样本（前 5 行，这些**不会**被自动修改）：")
        for v in reviews[:5]:
            print(f"  {v.code:10}{v.trade_date:12}{v.volume:>16}")
    return counts


def apply_fix(*_args, **_kwargs) -> int:
    """写入已被禁用 —— 与 repair_stock_kline_derived.py 保持同一决策。"""
    raise SystemExit(
        "写入已禁用：历史 K 线投影字段不得直接覆盖。\n"
        "  依据：stock_kline_observation 为 append-only 证据表，stock_kline 仅投影\n"
        "        disposition='current_projection' 的行；历史日期被 isolated_historical 隔离；\n"
        "        scheduler._ths_kline_recent_repair 明确『历史差异只入隔离版本、不覆盖』；\n"
        "        repair_stock_kline_derived.py 的 --apply 同样已禁用。\n"
        "  正确路径：追加新的观察证据并扩展投影机制（需单独评审）。\n"
        "  当前请使用本工具的只读审计输出，在**消费端**决定排除或应用修正系数。"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="科创板 volume/amount 100 倍口径 —— 只读审计（写入已禁用）"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="claw.db 路径")
    parser.add_argument("--from", dest="date_from", default=DEFAULT_FROM, help="起始交易日")
    parser.add_argument("--to", dest="date_to", default=None, help="结束交易日（含）")
    parser.add_argument("--json", dest="json_out", type=Path, default=None,
                        help="输出机器可读的受影响行清单，供消费端做口径修正")
    parser.add_argument("--apply", action="store_true",
                        help="（已禁用）历史投影字段覆盖不被允许")
    parser.add_argument("--i-confirm-backup", action="store_true",
                        help="（保留参数，已无作用）")
    parser.add_argument("--batch", type=int, default=500, help="（保留参数，已无作用）")
    args = parser.parse_args(argv)

    if args.apply:
        apply_fix()          # 直接 SystemExit，不可能走到写入

    if not args.db.exists():
        print(f"数据库不存在: {args.db}", file=sys.stderr)
        return 1

    with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as ro:
        verdicts = audit(ro, date_from=args.date_from, date_to=args.date_to)
    counts = report(verdicts)

    if args.json_out:
        payload = {
            "generated_at": __import__("datetime").datetime.now().isoformat(),
            "date_from": args.date_from,
            "date_to": args.date_to,
            "note": (
                "只读审计输出。未修改任何数据。受影响行需由消费端自行决定"
                "「排除」或「乘以 1/100」；历史投影字段不得覆盖。"
            ),
            "counts": counts,
            "rows": [
                {
                    "id": v.row_id, "code": v.code, "trade_date": v.trade_date,
                    "volume_as_stored": v.volume, "amount_as_stored": v.amount,
                    "action": v.action, "basis": v.basis,
                    "suggested_volume": int(round(v.volume / 100.0)),
                    "suggested_amount": round(v.amount / 100.0, 2),
                }
                for v in verdicts if v.action == "fix"
            ],
        }
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print()
        print(f"已写出机器可读清单: {args.json_out}（{counts['fix']} 行）")

    print()
    print("（只读审计模式，未写入任何数据；本工具不提供写入能力）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
