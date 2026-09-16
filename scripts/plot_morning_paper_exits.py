#!/usr/bin/env python3
"""Plot morning paper-account exits against their later intraday paths."""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from research_limit_up_intraday_patterns import (
    get_tencent_payload,
    parse_tencent,
    write_csv,
)


ROOT = Path(__file__).resolve().parents[1]


def minute_index(value: str) -> float:
    raw = "".join(character for character in str(value or "") if character.isdigit()).zfill(6)
    hour = int(raw[:2])
    minute = int(raw[2:4])
    second = int(raw[4:6])
    return (hour * 60 + minute + second / 60) - (9 * 60 + 30)


def fetch_sell_rows(db_path: Path, trade_date: str) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT
          account.account_name,
          trade.id,
          trade.code,
          COALESCE(position.name, trade.code) AS name,
          trade.price,
          trade.amount,
          trade.trade_time,
          trade.realized_pnl,
          trade.reason,
          trade.strategy_version
        FROM paper_trade_log AS trade
        JOIN paper_account AS account ON account.id = trade.account_id
        LEFT JOIN paper_position AS position
          ON position.account_id = trade.account_id AND position.code = trade.code
        WHERE date(trade.trade_time) = ?
          AND trade.trade_type = 'sell'
          AND time(trade.trade_time) <= '11:30:59'
        GROUP BY trade.id
        ORDER BY trade.trade_time, trade.id
        """,
        (trade_date,),
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default="2026-09-03")
    parser.add_argument("--db", type=Path, default=ROOT / "backend" / "claw.db")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    compact = args.trade_date.replace("-", "")
    output_dir = args.output_dir or ROOT / "outputs" / ("morning_review_" + compact)
    cache_dir = output_dir / ("raw_exit_intraday_" + compact) / "tencent"
    sells = fetch_sell_rows(args.db, args.trade_date)
    sells_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sells:
        row["code"] = str(row["code"]).zfill(6)
        sells_by_code[row["code"]].append(row)

    minute_by_code: dict[str, list[dict[str, Any]]] = {}
    summary: list[dict[str, Any]] = []
    for code, code_sells in sells_by_code.items():
        payload, status = get_tencent_payload(code, cache_dir, args.refresh)
        points, quote = parse_tencent(code, args.trade_date, payload) if payload else ([], {})
        minute_by_code[code] = points
        total_amount = sum(int(row["amount"] or 0) for row in code_sells)
        weighted_sell = (
            sum(float(row["price"] or 0) * int(row["amount"] or 0) for row in code_sells)
            / total_amount
            if total_amount
            else 0.0
        )
        morning_price = float(points[-1]["price"]) if points else None
        post_sell_pct = (
            round((morning_price / weighted_sell - 1) * 100, 2)
            if morning_price is not None and weighted_sell
            else None
        )
        summary.append(
            {
                "trade_date": args.trade_date,
                "account_name": code_sells[0]["account_name"],
                "code": code,
                "name": code_sells[0]["name"],
                "sell_count": len(code_sells),
                "total_amount": total_amount,
                "weighted_sell_price": round(weighted_sell, 4),
                "first_sell_time": min(str(row["trade_time"]) for row in code_sells),
                "last_sell_time": max(str(row["trade_time"]) for row in code_sells),
                "realized_pnl": round(sum(float(row["realized_pnl"] or 0) for row in code_sells), 2),
                "morning_last_price": morning_price,
                "post_sell_change_pct": post_sell_pct,
                "exit_reason": " | ".join(dict.fromkeys(str(row["reason"] or "") for row in code_sells)),
                "minute_source_status": status,
                "minute_point_count": len(points),
            }
        )

    summary.sort(key=lambda row: float(row["post_sell_change_pct"] or 0), reverse=True)
    write_csv(output_dir / ("模拟盘卖出后分时复盘_" + compact + ".csv"), summary)

    plt.rcParams["font.sans-serif"] = [
        "Hiragino Sans GB",
        "Heiti SC",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False
    columns = 3
    row_count = (len(summary) + columns - 1) // columns
    figure, axes = plt.subplots(row_count, columns, figsize=(19, row_count * 3.65), dpi=170)
    axes_list = list(axes.flat) if hasattr(axes, "flat") else [axes]

    summary_by_code = {row["code"]: row for row in summary}
    for axis, code in zip(axes_list, [row["code"] for row in summary]):
        points = minute_by_code.get(code, [])
        xs = [minute_index(str(row["time"])) for row in points]
        ys = [float(row["change_pct"]) for row in points]
        info = summary_by_code[code]
        follow = float(info["post_sell_change_pct"] or 0)
        color = "#ef4444" if follow >= 2 else ("#f59e0b" if follow > 0 else "#16a34a")
        axis.plot(xs, ys, color=color, linewidth=1.7)
        axis.fill_between(xs, ys, 0, color=color, alpha=0.08)
        axis.axhline(0, color="#94a3b8", linewidth=0.65, linestyle="--")

        for sale in sells_by_code[code]:
            sell_x = minute_index(str(sale["trade_time"])[11:19].replace(":", ""))
            axis.axvline(sell_x, color="#2563eb", linewidth=0.9, alpha=0.9)
            if points:
                nearest = min(points, key=lambda row: abs(minute_index(str(row["time"])) - sell_x))
                axis.scatter([sell_x], [float(nearest["change_pct"])], color="#2563eb", s=16, zorder=4)

        low = min(ys) if ys else -1.0
        high = max(ys) if ys else 1.0
        padding = max(1.0, (high - low) * 0.15)
        axis.set_ylim(min(-1.0, low - padding), max(1.0, high + padding))
        axis.set_xlim(0, 120)
        axis.set_xticks([0, 30, 60, 90, 120])
        axis.set_xticklabels(["9:30", "10:00", "10:30", "11:00", "11:30"], fontsize=7)
        axis.tick_params(axis="y", labelsize=7)
        axis.grid(axis="y", color="#e2e8f0", linewidth=0.45)
        axis.set_title(
            f"{code} {info['name']} · {info['account_name']}\n"
            f"卖出均价 {info['weighted_sell_price']:.2f} → 11:30 {info['morning_last_price']:.2f}  "
            f"卖后 {follow:+.2f}%  已实现 {info['realized_pnl']:+.2f}",
            fontsize=9,
            loc="left",
            pad=4,
        )

    for axis in axes_list[len(summary) :]:
        axis.axis("off")

    figure.suptitle(
        f"{args.trade_date} 上午模拟盘卖出后分时（蓝线=卖出时点）\n"
        "红色=卖后上涨≥2%，橙色=卖后小涨，绿色=卖后下跌；仅用于执行复盘，不用午后数据反改早盘规则",
        fontsize=15,
        fontweight="bold",
        y=0.998,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.96))
    output_path = output_dir / ("模拟盘卖出后分时复盘_" + compact + ".png")
    figure.savefig(output_path, bbox_inches="tight")
    plt.close(figure)
    print(output_path)


if __name__ == "__main__":
    main()
