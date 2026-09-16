#!/usr/bin/env python3
"""Render a frozen morning limit-up pool as reproducible intraday small multiples."""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def minute_index(value: str) -> float:
    raw = "".join(character for character in str(value or "") if character.isdigit()).zfill(6)
    hour = int(raw[:2])
    minute = int(raw[2:4])
    second = int(raw[4:6])
    return (hour * 60 + minute + second / 60) - (9 * 60 + 30)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", default="2026-09-03")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    compact = args.trade_date.replace("-", "")
    output_dir = args.output_dir or ROOT / "outputs" / ("morning_review_" + compact)
    pool = load_csv(output_dir / ("涨停池_" + compact + ".csv"))
    minutes = load_csv(output_dir / ("涨停全样本分钟分时_" + compact + ".csv"))

    minute_by_code: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in minutes:
        minute_by_code[str(row["code"]).zfill(6)].append(row)
    for rows in minute_by_code.values():
        rows.sort(key=lambda row: row["time"])

    pool.sort(
        key=lambda row: (
            -int(float(row.get("consecutive_days") or 0)),
            str(row.get("limit_up_time") or "999999"),
            str(row.get("code") or ""),
        )
    )

    plt.rcParams["font.sans-serif"] = [
        "Hiragino Sans GB",
        "Heiti SC",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False

    columns = 5
    rows_count = math.ceil(len(pool) / columns)
    figure, axes = plt.subplots(rows_count, columns, figsize=(22, rows_count * 2.85), dpi=160)
    axes_list = list(axes.flat)

    for axis, stock in zip(axes_list, pool):
        code = str(stock["code"]).zfill(6)
        points = minute_by_code.get(code, [])
        xs = [minute_index(row["time"]) for row in points]
        ys = [float(row["change_pct"]) for row in points]
        board_type = str(stock.get("board_type") or "")
        limit_pct = 20.0 if board_type in {"gem", "star"} else 10.0
        color = "#ef4444" if int(float(stock.get("consecutive_days") or 0)) == 1 else "#7c3aed"

        axis.plot(xs, ys, color=color, linewidth=1.45)
        axis.fill_between(xs, ys, 0, color=color, alpha=0.08)
        axis.axhline(0, color="#94a3b8", linewidth=0.6, linestyle="--")
        axis.axhline(limit_pct, color="#f59e0b", linewidth=0.65, linestyle=":")

        seal_raw = str(stock.get("limit_up_time") or "")
        if seal_raw:
            seal_x = minute_index(seal_raw)
            axis.axvline(seal_x, color="#0891b2", linewidth=0.75, alpha=0.8)

        minimum = min(ys) if ys else 0.0
        lower = min(-2.0, math.floor(minimum - 0.5))
        upper = 21.0 if limit_pct == 20.0 else 11.0
        axis.set_ylim(lower, upper)
        axis.set_xlim(0, 120)
        axis.set_xticks([0, 30, 60, 90, 120])
        axis.set_xticklabels(["9:30", "10:00", "10:30", "11:00", "11:30"], fontsize=6.5)
        axis.tick_params(axis="y", labelsize=6.5)
        axis.grid(axis="y", color="#e2e8f0", linewidth=0.45)
        axis.set_title(
            f"{code} {stock.get('name', '')}  {stock.get('consecutive_days', '1')}板\n"
            f"首封 {seal_raw[:2]}:{seal_raw[2:4]}  开板{stock.get('break_count', '0')}次",
            fontsize=8.2,
            loc="left",
            pad=3,
        )

    for axis in axes_list[len(pool) :]:
        axis.axis("off")

    figure.suptitle(
        f"{args.trade_date} 上午涨停池分时全样本（{len(pool)}只）\n"
        "红线=首板，紫线=连板，青色竖线=首次封板；数据截至 11:30",
        fontsize=16,
        fontweight="bold",
        y=0.998,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.975))
    output_path = output_dir / ("涨停池分时全景_" + compact + ".png")
    figure.savefig(output_path, bbox_inches="tight")
    plt.close(figure)

    advancers = load_csv(output_dir / ("上涨个股_" + compact + ".csv"))
    change_buckets = Counter()
    eligibility = Counter()
    for stock in advancers:
        change = float(stock.get("change_pct") or 0)
        if change >= 10:
            bucket = ">=10%"
        elif change >= 7:
            bucket = "7%-10%"
        elif change >= 5:
            bucket = "5%-7%"
        elif change >= 3:
            bucket = "3%-5%"
        elif change >= 1:
            bucket = "1%-3%"
        else:
            bucket = "0%-1%"
        change_buckets[bucket] += 1
        eligibility[str(stock.get("board_tag") or "unknown")] += 1

    tradeable = [stock for stock in advancers if stock.get("board_tag") == "tradeable"][:12]
    overview, overview_axes = plt.subplots(1, 3, figsize=(19, 6.5), dpi=170)
    bucket_labels = ["0%-1%", "1%-3%", "3%-5%", "5%-7%", "7%-10%", ">=10%"]
    bucket_values = [change_buckets[label] for label in bucket_labels]
    overview_axes[0].bar(bucket_labels, bucket_values, color="#ef4444", alpha=0.82)
    overview_axes[0].set_title("上涨个股涨幅分布")
    overview_axes[0].tick_params(axis="x", rotation=30)
    for index, value in enumerate(bucket_values):
        overview_axes[0].text(index, value, str(value), ha="center", va="bottom", fontsize=9)

    eligibility_labels = ["可自动交易", "仅观察", "禁止交易"]
    eligibility_values = [
        eligibility.get("tradeable", 0),
        eligibility.get("observe_only", 0),
        eligibility.get("blocked", 0),
    ]
    overview_axes[1].bar(
        eligibility_labels,
        eligibility_values,
        color=["#16a34a", "#f59e0b", "#64748b"],
        alpha=0.85,
    )
    overview_axes[1].set_title("上涨股交易权限分层")
    for index, value in enumerate(eligibility_values):
        overview_axes[1].text(index, value, str(value), ha="center", va="bottom", fontsize=9)

    names = [f"{row['code']} {row['name']}" for row in reversed(tradeable)]
    values = [float(row["change_pct"]) for row in reversed(tradeable)]
    overview_axes[2].barh(names, values, color="#ef4444", alpha=0.82)
    overview_axes[2].set_title("可交易上涨股涨幅前12")
    overview_axes[2].set_xlabel("涨幅 %")
    overview_axes[2].tick_params(axis="y", labelsize=8)
    for index, value in enumerate(values):
        overview_axes[2].text(value, index, f" {value:.2f}%", va="center", fontsize=8)

    for axis in overview_axes:
        axis.grid(axis="y", color="#e2e8f0", linewidth=0.5)
        axis.set_axisbelow(True)
    overview.suptitle(
        f"{args.trade_date} 上午上涨个股结构（共 {len(advancers)} 只，快照截至 11:30）",
        fontsize=16,
        fontweight="bold",
    )
    overview.tight_layout(rect=(0, 0, 1, 0.94))
    overview_path = output_dir / ("上涨个股结构概览_" + compact + ".png")
    overview.savefig(overview_path, bbox_inches="tight")
    plt.close(overview)
    print(output_path)
    print(overview_path)


if __name__ == "__main__":
    main()
