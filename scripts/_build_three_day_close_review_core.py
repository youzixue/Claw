#!/usr/bin/env python3
"""Build a read-only 2026-09-01..03 market and A-F paper-trading review pack.

The script never writes to SQLite.  It combines the frozen daily exports with
Tencent's public five-minute history, persists the raw responses for audit, and
renders deterministic charts plus machine-readable fact tables.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import ssl
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "outputs" / "three_day_review_20260903"
DATES = ("20260901", "20260902", "20260903")
DATE_LABELS = {"20260901": "9月1日", "20260902": "9月2日", "20260903": "9月3日"}
CHAMPION_CODES = {
    "default": "A",
    "promotion": "B",
    "mainline": "C",
    "auction": "D",
    "tenbagger": "E",
    "reversal": "F",
}
FORMAL_CLOSE_SOURCES = {"ths", "tencent_close"}
FROZEN_SEP1_DIR = ROOT / "outputs" / "daily_review_20260901"
FROZEN_SEP1_VERSION = "daily_export_e3d4f43ca1c8"
FROZEN_SEP1_BREADTH = {
    "rising": 2166,
    "falling": 980,
    "flat": 98,
    "total": 3244,
}
FROZEN_SEP1_POOL_AUDIT = {
    "limit_up_count": 83,
    "broken_limit_count": 38,
    "limit_down_count": 5,
    "maximum_board": 7,
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_frozen_sep1() -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    """Load and verify the immutable Sep-1 close export.

    The live database was backfilled later with mixed providers, so it is an
    audit source for Sep-1 rather than the source used for cross-day breadth or
    representative advancer samples.
    """
    metadata_path = FROZEN_SEP1_DIR / "metadata.json"
    report_path = FROZEN_SEP1_DIR / "盘后复盘与模拟盘漏斗.md"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    report = report_path.read_text(encoding="utf-8")
    advancers = read_csv(FROZEN_SEP1_DIR / "上涨个股_20260901.csv")
    pool = read_csv(FROZEN_SEP1_DIR / "涨停池_20260901.csv")

    problems = []
    if metadata.get("data_version") != FROZEN_SEP1_VERSION:
        problems.append(f"version={metadata.get('data_version')!r}")
    if safe_int(metadata.get("rising_count")) != FROZEN_SEP1_BREADTH["rising"]:
        problems.append(f"metadata rising={metadata.get('rising_count')!r}")
    if len(advancers) != FROZEN_SEP1_BREADTH["rising"]:
        problems.append(f"advancer rows={len(advancers)}")
    if len(pool) != FROZEN_SEP1_POOL_AUDIT["limit_up_count"]:
        problems.append(f"limit-up rows={len(pool)}")
    expected_report_facts = (
        "StockKline` 3,244",
        "上涨 2,166",
        "平盘 98",
        "下跌 980",
        "跌停 5",
        "炸板 38",
        "最高 7 板",
    )
    missing = [fact for fact in expected_report_facts if fact not in report]
    if missing:
        problems.append("report missing " + ", ".join(missing))
    if problems:
        raise RuntimeError("invalid immutable Sep-1 export: " + "; ".join(problems))
    return advancers, pool, metadata


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def safe_int(value: Any, default: int = 0) -> int:
    parsed = safe_float(value)
    return int(parsed) if parsed is not None else default


def tencent_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("4", "8")):
        return "bj" + code
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    return "sz" + code


def fetch_symbol(symbol: str, cache_path: Path, refresh: bool) -> dict[str, Any]:
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    url = (
        "https://ifzq.gtimg.cn/appstock/app/kline/mkline?"
        + urllib.parse.urlencode({"param": f"{symbol},m5,,640"})
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://gu.qq.com/",
        },
    )
    context = ssl._create_unverified_context()
    last_error = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20, context=context) as response:
                payload = json.load(response)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            return payload
        except Exception as exc:  # pragma: no cover - network failure is recorded
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))
    return {"code": -1, "msg": last_error, "data": {}}


def parse_m5(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    branch = payload.get("data", {}).get(symbol, {})
    rows = branch.get("m5", []) if isinstance(branch, dict) else []
    parsed: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, list) or len(raw) < 6:
            continue
        stamp = str(raw[0] or "")
        if len(stamp) != 12 or not stamp.isdigit():
            continue
        parsed.append(
            {
                "date": stamp[:8],
                "time": stamp[8:12],
                "open": safe_float(raw[1]),
                "close": safe_float(raw[2]),
                "high": safe_float(raw[3]),
                "low": safe_float(raw[4]),
                "volume": safe_float(raw[5]),
                "amount": safe_float(raw[7]) if len(raw) > 7 else None,
            }
        )
    return parsed


def close_anchor(row: dict[str, str]) -> float | None:
    close = safe_float(row.get("close"))
    change = safe_float(row.get("close_change_pct"))
    if change is None:
        change = safe_float(row.get("change_pct"))
    if close is None or close <= 0 or change is None or change <= -99.0:
        return None
    return close / (1.0 + change / 100.0)


def hhmm_to_minutes(value: str) -> int | None:
    digits = "".join(char for char in str(value or "") if char.isdigit())
    if len(digits) < 4:
        return None
    hour = int(digits[:2])
    minute = int(digits[2:4])
    return hour * 60 + minute


def open_bucket(open_pct: float | None) -> str:
    if open_pct is None:
        return "缺失"
    if open_pct < -1:
        return "<-1%"
    if open_pct <= 1:
        return "-1%~1%"
    if open_pct < 2.6:
        return "1%~2.6%"
    if open_pct < 6:
        return "2.6%~6%"
    return ">=6%"


def seal_bucket(value: str) -> str:
    minute = hhmm_to_minutes(value)
    if minute is None:
        return "缺失"
    if minute <= 9 * 60 + 35:
        return "09:35前"
    if minute <= 10 * 60:
        return "09:35~10:00"
    if minute <= 11 * 60 + 30:
        return "10:00~11:30"
    return "午后"


def path_shape(row: dict[str, str], points: list[dict[str, Any]]) -> str:
    anchor = close_anchor(row)
    if not anchor or not points:
        return "分时缺失"
    first_open = safe_float(points[0].get("open"))
    minimum = min((safe_float(item.get("low"), 10**9) or 10**9) for item in points)
    open_pct = (first_open / anchor - 1) * 100 if first_open else None
    low_pct = (minimum / anchor - 1) * 100 if minimum < 10**9 else None
    breaks = safe_int(row.get("break_count"))
    if open_pct is not None and open_pct >= 9.5 and low_pct is not None and low_pct >= 9.4:
        return "一字/近一字"
    if open_pct is not None and open_pct <= -1:
        return "水下反包"
    if open_pct is not None and open_pct <= 1:
        return "零轴附近启动"
    if open_pct is not None and open_pct < 6:
        return "中低开推进" if breaks == 0 else "中低开换手回封"
    return "高开封板" if breaks == 0 else "高开换手回封"


def return_bucket(value: float) -> str:
    if value < 1:
        return "0~1%"
    if value < 3:
        return "1~3%"
    if value < 5:
        return "3~5%"
    if value < 7:
        return "5~7%"
    if value < 10:
        return "7~10%"
    return ">=10%"


def normalize_reason(reason: str) -> str:
    text = str(reason or "")
    # Order matters: a reason such as “昨日涨停后T+1” must be classified by
    # the operative blocker, not by the incidental word “涨停”.
    rules = [
        ("自动买入暂停", ("暂停", "auto buy", "仅记录", "dry-run", "dry run")),
        ("T+1限制", ("T+1", "T＋1")),
        ("跨版本回补禁止", ("跨策略版本", "跨版本")),
        ("结构过滤", ("已过滤", "形态不符", "封板资金不足", "炸板过多")),
        ("数据质量门禁", ("质量", "覆盖不足", "quality", "spot_fallback", "竞价路径", "竞价覆盖", "watermark")),
        ("无上游候选", ("无上游", "暂无候选", "没有候选", "候选为空", "无满足")),
        ("盘中强度未确认", ("未通过盘中", "未形成盘中", "无强度确认", "没有通过盘中", "形态未确认", "持续强度确认")),
        ("多帧确认等待", ("帧", "持续确认", "确认等待", "等待分时")),
        ("性价比/执行闸门", ("性价比", "观察池", "不追", "追高", "位置赔率")),
        ("持仓/名额已满", ("持仓数量", "日限", "名额", "仓位已满", "持仓已满")),
        ("风控阻断", ("风控", "risk", "回撤锁", "黑名单")),
        ("预算不足一手", ("不足1手", "不足一手", "可买数量为0", "预算不足")),
        ("确认后价格漂移", ("漂移",)),
        ("涨停不可成交", ("卖一", "排队", "一字", "封死")),
        ("止损卖出", ("止损",)),
        ("止盈/回落卖出", ("止盈", "回落", "兑现")),
    ]
    lowered = text.lower()
    for label, needles in rules:
        if any(needle.lower() in lowered for needle in needles):
            return label
    return "其他"


def configure_fonts() -> None:
    plt.rcParams["font.sans-serif"] = [
        "Hiragino Sans GB",
        "Heiti SC",
        "Arial Unicode MS",
        "DejaVu Sans",
    ]
    plt.rcParams["axes.unicode_minus"] = False


def plot_market_overview(
    output: Path,
    advancers: dict[str, list[dict[str, str]]],
    pools: dict[str, list[dict[str, str]]],
    breadth: dict[str, dict[str, int]],
    pool_stats: dict[str, dict[str, Any]],
) -> None:
    configure_fonts()
    fig, axes = plt.subplots(2, 3, figsize=(20, 11), dpi=170)
    x = list(range(len(DATES)))
    labels = [DATE_LABELS[item] for item in DATES]

    for key, color in (("rising", "#ef4444"), ("falling", "#16a34a"), ("flat", "#94a3b8")):
        values = [breadth[item].get(key, 0) for item in DATES]
        axes[0, 0].plot(x, values, marker="o", linewidth=2, color=color, label=key)
        for xi, value in zip(x, values):
            axes[0, 0].text(xi, value, str(value), ha="center", va="bottom", fontsize=8)
    axes[0, 0].set_xticks(x, labels)
    axes[0, 0].set_title("全市场收盘家数（9月1日使用不可变收盘归档）")
    axes[0, 0].legend(["上涨", "下跌", "平盘"], frameon=False)
    axes[0, 0].grid(axis="y", alpha=0.2)

    buckets = ["0~1%", "1~3%", "3~5%", "5~7%", "7~10%", ">=10%"]
    width = 0.25
    for index, trade_date in enumerate(DATES):
        counts = Counter(return_bucket(safe_float(row.get("change_pct"), 0.0) or 0.0) for row in advancers[trade_date])
        values = [counts[item] for item in buckets]
        axes[0, 1].bar(
            [item + (index - 1) * width for item in range(len(buckets))],
            values,
            width=width,
            label=DATE_LABELS[trade_date],
        )
    axes[0, 1].set_xticks(range(len(buckets)), buckets, rotation=25)
    axes[0, 1].set_title("上涨个股收盘涨幅分布")
    axes[0, 1].legend(frameon=False)
    axes[0, 1].grid(axis="y", alpha=0.2)

    first = [pool_stats[item]["first_board_count"] for item in DATES]
    promotion = [pool_stats[item]["promotion_count"] for item in DATES]
    axes[0, 2].bar(x, first, color="#ef4444", label="首板")
    axes[0, 2].bar(x, promotion, bottom=first, color="#7c3aed", label="连板")
    for xi, trade_date in zip(x, DATES):
        axes[0, 2].text(xi, pool_stats[trade_date]["limit_up_count"] + 1, str(pool_stats[trade_date]["limit_up_count"]), ha="center")
    axes[0, 2].set_xticks(x, labels)
    axes[0, 2].set_title("涨停池：首板仍是主要分母")
    axes[0, 2].legend(frameon=False)
    axes[0, 2].grid(axis="y", alpha=0.2)

    open_labels = ["<-1%", "-1%~1%", "1%~2.6%", "2.6%~6%", ">=6%", "缺失"]
    bottoms = [0, 0, 0]
    colors = ["#2563eb", "#06b6d4", "#f59e0b", "#fb7185", "#dc2626", "#cbd5e1"]
    for label, color in zip(open_labels, colors):
        vals = [pool_stats[item]["open_buckets"].get(label, 0) for item in DATES]
        axes[1, 0].bar(x, vals, bottom=bottoms, label=label, color=color)
        bottoms = [a + b for a, b in zip(bottoms, vals)]
    axes[1, 0].set_xticks(x, labels)
    axes[1, 0].set_title("涨停股开盘位置")
    axes[1, 0].legend(frameon=False, fontsize=8, ncol=2)
    axes[1, 0].grid(axis="y", alpha=0.2)

    seal_labels = ["09:35前", "09:35~10:00", "10:00~11:30", "午后", "缺失"]
    bottoms = [0, 0, 0]
    for label, color in zip(seal_labels, ["#dc2626", "#f97316", "#facc15", "#8b5cf6", "#cbd5e1"]):
        vals = [pool_stats[item]["seal_buckets"].get(label, 0) for item in DATES]
        axes[1, 1].bar(x, vals, bottom=bottoms, label=label, color=color)
        bottoms = [a + b for a, b in zip(bottoms, vals)]
    axes[1, 1].set_xticks(x, labels)
    axes[1, 1].set_title("首次封板时段")
    axes[1, 1].legend(frameon=False, fontsize=8, ncol=2)
    axes[1, 1].grid(axis="y", alpha=0.2)

    shape_labels = sorted({
        key
        for trade_date in DATES
        for key in pool_stats[trade_date]["shape_counts"]
    })
    # The union can contain eight or more categories. A fixed seven-color zip
    # silently dropped the final category and made each stacked bar shorter than
    # its limit-up denominator. Cycle a categorical map instead.
    shape_cmap = plt.get_cmap("tab10")
    bottoms = [0, 0, 0]
    for index, label in enumerate(shape_labels):
        color = shape_cmap(index % shape_cmap.N)
        vals = [pool_stats[item]["shape_counts"].get(label, 0) for item in DATES]
        axes[1, 2].bar(x, vals, bottom=bottoms, label=label, color=color)
        bottoms = [a + b for a, b in zip(bottoms, vals)]
    expected_shape_totals = [pool_stats[item]["limit_up_count"] for item in DATES]
    if bottoms != expected_shape_totals:
        raise RuntimeError(
            f"shape chart denominator mismatch: plotted={bottoms}, expected={expected_shape_totals}"
        )
    axes[1, 2].set_xticks(x, labels)
    axes[1, 2].set_title("涨停分时形态（5分钟重建）")
    axes[1, 2].legend(frameon=False, fontsize=8, ncol=2)
    axes[1, 2].grid(axis="y", alpha=0.2)

    fig.suptitle("2026-09-01 至 2026-09-03 收盘复盘：上涨结构与涨停池", fontsize=18, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.965))
    fig.savefig(output / "三日市场宽度与涨停池_20260901_20260903.png", bbox_inches="tight")
    plt.close(fig)


def plot_path_grid(
    path: Path,
    title: str,
    rows: list[dict[str, str]],
    m5_by_code: dict[str, list[dict[str, Any]]],
    trade_date: str,
    max_items: int | None = None,
) -> None:
    configure_fonts()
    selected = rows[:max_items] if max_items else rows
    columns = 5
    row_count = max(math.ceil(len(selected) / columns), 1)
    fig, axes = plt.subplots(row_count, columns, figsize=(22, row_count * 2.55), dpi=150)
    axes_list = list(axes.flat) if hasattr(axes, "flat") else [axes]
    for axis, stock in zip(axes_list, selected):
        code = str(stock.get("code") or "").zfill(6)
        points = [item for item in m5_by_code.get(code, []) if item["date"] == trade_date]
        anchor = close_anchor(stock)
        ys = [
            ((safe_float(item.get("close")) or anchor) / anchor - 1.0) * 100.0
            for item in points
        ] if anchor else []
        xs = list(range(len(ys)))
        consecutive = safe_int(stock.get("consecutive_days"), 0)
        color = "#7c3aed" if consecutive > 1 else "#ef4444"
        axis.plot(xs, ys, color=color, linewidth=1.35)
        axis.fill_between(xs, ys, 0, color=color, alpha=0.08)
        axis.axhline(0, color="#94a3b8", linewidth=0.6, linestyle="--")
        if stock.get("in_limit_up_pool") == "1" or stock.get("consecutive_days"):
            limit_line = 20 if str(stock.get("board_type")) in {"gem", "star"} else 10
            axis.axhline(limit_line, color="#f59e0b", linewidth=0.65, linestyle=":")
        axis.set_xlim(0, 47)
        axis.set_xticks([0, 12, 24, 36, 47], ["9:35", "10:35", "11:30", "14:00", "15:00"], fontsize=6.5)
        axis.tick_params(axis="y", labelsize=6.5)
        axis.grid(axis="y", color="#e2e8f0", linewidth=0.4)
        subtitle = (
            f"{consecutive}板 首封{str(stock.get('limit_up_time') or '--')[:4]} "
            f"开板{safe_int(stock.get('break_count'))}"
            if stock.get("consecutive_days")
            else f"收盘{safe_float(stock.get('change_pct'), 0.0):.2f}%"
        )
        axis.set_title(f"{code} {stock.get('name', '')}\n{subtitle}", fontsize=7.8, loc="left", pad=3)
    for axis in axes_list[len(selected):]:
        axis.axis("off")
    fig.suptitle(title, fontsize=16, fontweight="bold", y=0.998)
    fig.tight_layout(rect=(0, 0, 1, 0.988))
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def plot_trades(
    output: Path,
    trades: list[dict[str, Any]],
    m5_by_code: dict[str, list[dict[str, Any]]],
    names: dict[str, str],
) -> None:
    configure_fonts()
    codes = sorted({str(item["code"]).zfill(6) for item in trades})
    columns = 3
    row_count = max(math.ceil(len(codes) / columns), 1)
    fig, axes = plt.subplots(row_count, columns, figsize=(19, row_count * 3.8), dpi=160)
    axes_list = list(axes.flat) if hasattr(axes, "flat") else [axes]
    strategy_by_code: dict[str, set[str]] = defaultdict(set)
    for item in trades:
        strategy_by_code[str(item["code"]).zfill(6)].add(str(item["strategy_code"]))
    for axis, code in zip(axes_list, codes):
        all_points = [item for item in m5_by_code.get(code, []) if item["date"] in DATES]
        all_points.sort(key=lambda item: (item["date"], item["time"]))
        prices = [safe_float(item.get("close")) for item in all_points]
        axis.plot(range(len(prices)), prices, color="#334155", linewidth=1.25)
        indices_by_date = {
            trade_date: [
                index for index, point in enumerate(all_points)
                if point["date"] == trade_date
            ]
            for trade_date in DATES
        }
        for trade_date in DATES[1:]:
            indices = indices_by_date[trade_date]
            if indices:
                axis.axvline(indices[0] - 0.5, color="#cbd5e1", linestyle="--", linewidth=0.8)
        for trade in [item for item in trades if str(item["code"]).zfill(6) == code]:
            stamp = datetime.fromisoformat(str(trade["trade_time"]))
            date_key = stamp.strftime("%Y%m%d")
            target_minute = stamp.strftime("%H%M")
            candidates = [
                (abs(hhmm_to_minutes(point["time"]) - hhmm_to_minutes(target_minute)), index)
                for index, point in enumerate(all_points)
                if point["date"] == date_key
            ]
            index = min(candidates)[1] if candidates else None
            if index is None:
                continue
            is_buy = trade["trade_type"] == "buy"
            axis.scatter(
                [index],
                [trade["price"]],
                color="#dc2626" if is_buy else "#16a34a",
                marker="^" if is_buy else "v",
                s=38,
                zorder=5,
            )
            axis.annotate(
                f"{trade['strategy_code']}{'买' if is_buy else '卖'}",
                (index, trade["price"]),
                xytext=(2, 6 if is_buy else -12),
                textcoords="offset points",
                fontsize=7,
                color="#dc2626" if is_buy else "#15803d",
            )
        tick_dates = [trade_date for trade_date in DATES if indices_by_date[trade_date]]
        tick_positions = [
            (indices_by_date[trade_date][0] + indices_by_date[trade_date][-1]) / 2
            for trade_date in tick_dates
        ]
        axis.set_xticks(
            tick_positions,
            [DATE_LABELS[trade_date] for trade_date in tick_dates],
            fontsize=7,
        )
        axis.grid(axis="y", alpha=0.2)
        axis.tick_params(axis="y", labelsize=7)
        strategies = "/".join(sorted(strategy_by_code[code]))
        axis.set_title(f"{code} {names.get(code, '')} · 策略{strategies}", fontsize=9, loc="left")
    for axis in axes_list[len(codes):]:
        axis.axis("off")
    fig.suptitle("A–F 模拟盘成交与随后5分钟路径（红三角买、绿三角卖）", fontsize=17, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.975))
    fig.savefig(output / "模拟盘成交与分时路径_20260901_20260903.png", bbox_inches="tight")
    plt.close(fig)


def db_rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params).fetchall()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "backend" / "claw.db")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    advancers: dict[str, list[dict[str, str]]] = {}
    pools: dict[str, list[dict[str, str]]] = {}
    metadata: dict[str, Any] = {}
    names: dict[str, str] = {}
    selected_codes: set[str] = set()
    frozen_advancers, frozen_pool, frozen_metadata = load_frozen_sep1()

    for trade_date in DATES:
        day_dir = output / "data" / trade_date
        day_dir.mkdir(parents=True, exist_ok=True)
        if trade_date == "20260901":
            # Replace the late-mixed copy in this review pack with the verified
            # immutable close export so every downstream chart uses one causal
            # Sep-1 universe.
            advancers[trade_date] = frozen_advancers
            pools[trade_date] = frozen_pool
            metadata[trade_date] = frozen_metadata
            write_csv(day_dir / f"上涨个股_{trade_date}.csv", frozen_advancers)
            write_csv(day_dir / f"涨停池_{trade_date}.csv", frozen_pool)
            (day_dir / "metadata.json").write_text(
                json.dumps(frozen_metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            advancers[trade_date] = read_csv(day_dir / f"上涨个股_{trade_date}.csv")
            pools[trade_date] = read_csv(day_dir / f"涨停池_{trade_date}.csv")
            metadata[trade_date] = json.loads((day_dir / "metadata.json").read_text(encoding="utf-8"))
        for row in advancers[trade_date] + pools[trade_date]:
            code = str(row.get("code") or "").zfill(6)
            names[code] = str(row.get("name") or names.get(code, ""))
        selected_codes.update(str(row.get("code") or "").zfill(6) for row in pools[trade_date])
        top_tradeable = [
            row
            for row in advancers[trade_date]
            if row.get("board_tag") == "tradeable" and row.get("in_limit_up_pool") != "1"
        ][:15]
        selected_codes.update(str(row.get("code") or "").zfill(6) for row in top_tradeable)

    db_uri = f"file:{args.db.resolve()}?mode=ro"
    connection = sqlite3.connect(db_uri, uri=True, timeout=60)
    connection.row_factory = sqlite3.Row

    account_rows = db_rows(connection, "SELECT * FROM paper_account ORDER BY id")
    champion_account_by_name: dict[str, dict[str, Any]] = {}
    for row in account_rows:
        name = str(row.get("account_name") or "")
        if name in CHAMPION_CODES:
            champion_account_by_name[name] = row
    champion_ids = sorted(int(row["id"]) for row in champion_account_by_name.values())
    placeholders = ",".join("?" for _ in champion_ids)

    trades = db_rows(
        connection,
        f"""
        SELECT t.*, a.account_name
        FROM paper_trade_log t
        JOIN paper_account a ON a.id=t.account_id
        WHERE t.account_id IN ({placeholders})
          AND t.trade_time >= '2026-09-01 00:00:00'
          AND t.trade_time < '2026-09-04 00:00:00'
        ORDER BY t.trade_time, t.id
        """,
        tuple(champion_ids),
    )
    for row in trades:
        row["strategy_code"] = CHAMPION_CODES[str(row["account_name"])]
        code = str(row["code"]).zfill(6)
        row["code"] = code
        selected_codes.add(code)

    spot_names = db_rows(
        connection,
        f"SELECT code,name FROM stock_spot WHERE code IN ({','.join('?' for _ in selected_codes)})",
        tuple(sorted(selected_codes)),
    )
    for row in spot_names:
        names[str(row["code"]).zfill(6)] = str(row.get("name") or "")

    raw_dir = output / "raw_m5" / "tencent"
    fetch_manifest: dict[str, Any] = {}
    m5_by_code: dict[str, list[dict[str, Any]]] = {}
    workers = min(max(args.workers, 1), 16)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {}
        for code in sorted(selected_codes):
            symbol = tencent_symbol(code)
            cache = raw_dir / f"{symbol}.json"
            future = executor.submit(fetch_symbol, symbol, cache, args.refresh)
            future_map[future] = (code, symbol, cache)
        for future in as_completed(future_map):
            code, symbol, cache = future_map[future]
            payload = future.result()
            points = parse_m5(payload, symbol)
            m5_by_code[code] = points
            dates_present = Counter(item["date"] for item in points)
            fetch_manifest[code] = {
                "symbol": symbol,
                "cache": str(cache.relative_to(output)),
                "sha256": hashlib.sha256(cache.read_bytes()).hexdigest() if cache.exists() else None,
                "response_code": payload.get("code"),
                "message": payload.get("msg"),
                "point_count": len(points),
                "target_date_points": {date: dates_present.get(date, 0) for date in DATES},
            }

    minute_rows: list[dict[str, Any]] = []
    for code in sorted(m5_by_code):
        for item in m5_by_code[code]:
            if item["date"] in DATES:
                minute_rows.append({"code": code, "name": names.get(code, ""), **item})
    write_csv(
        output / "data" / "三日代表样本5分钟分时_20260901_20260903.csv",
        minute_rows,
        ["code", "name", "date", "time", "open", "close", "high", "low", "volume", "amount"],
    )

    pool_feature_rows: list[dict[str, Any]] = []
    pool_stats: dict[str, dict[str, Any]] = {}
    for trade_date in DATES:
        open_counts: Counter[str] = Counter()
        seal_counts: Counter[str] = Counter()
        shape_counts: Counter[str] = Counter()
        for row in pools[trade_date]:
            code = str(row["code"]).zfill(6)
            points = [item for item in m5_by_code.get(code, []) if item["date"] == trade_date]
            anchor = close_anchor(row)
            first_open = safe_float(points[0].get("open")) if points else None
            open_pct = (
                (first_open / anchor - 1.0) * 100.0
                if first_open is not None and anchor is not None and anchor > 0
                else None
            )
            minimum_pct = (
                (min(safe_float(item.get("low"), anchor) or anchor for item in points) / anchor - 1.0) * 100.0
                if points and anchor
                else None
            )
            shape = path_shape(row, points)
            ob = open_bucket(open_pct)
            sb = seal_bucket(str(row.get("limit_up_time") or ""))
            open_counts[ob] += 1
            seal_counts[sb] += 1
            shape_counts[shape] += 1
            pool_feature_rows.append(
                {
                    "trade_date": trade_date,
                    "code": code,
                    "name": row.get("name"),
                    "consecutive_days": safe_int(row.get("consecutive_days"), 1),
                    "limit_up_time": row.get("limit_up_time"),
                    "break_count": safe_int(row.get("break_count")),
                    "close_change_pct": safe_float(row.get("close_change_pct")),
                    "open_pct_5m": round(open_pct, 4) if open_pct is not None else None,
                    "minimum_pct_5m": round(minimum_pct, 4) if minimum_pct is not None else None,
                    "open_bucket": ob,
                    "seal_bucket": sb,
                    "path_shape": shape,
                    "m5_point_count": len(points),
                }
            )
        pool_stats[trade_date] = {
            "limit_up_count": len(pools[trade_date]),
            "first_board_count": sum(safe_int(row.get("consecutive_days"), 1) == 1 for row in pools[trade_date]),
            "promotion_count": sum(safe_int(row.get("consecutive_days"), 1) >= 2 for row in pools[trade_date]),
            "maximum_board": max((safe_int(row.get("consecutive_days"), 1) for row in pools[trade_date]), default=0),
            "open_buckets": dict(open_counts),
            "seal_buckets": dict(seal_counts),
            "shape_counts": dict(shape_counts),
            "m5_complete_count": sum(
                len([item for item in m5_by_code.get(str(row["code"]).zfill(6), []) if item["date"] == trade_date]) == 48
                for row in pools[trade_date]
            ),
        }
    write_csv(output / "data" / "三日涨停池5分钟形态特征_20260901_20260903.csv", pool_feature_rows)

    breadth: dict[str, dict[str, int]] = {}
    source_breakdown: dict[str, dict[str, dict[str, int]]] = {}
    late_backfill_audit: dict[str, Any] = {}
    pool_count_audit: dict[str, Any] = {}
    for trade_date in DATES:
        date_iso = datetime.strptime(trade_date, "%Y%m%d").date().isoformat()
        rows = db_rows(
            connection,
            """
            SELECT source,
                   SUM(CASE WHEN change_pct > 0 THEN 1 ELSE 0 END) AS rising,
                   SUM(CASE WHEN change_pct < 0 THEN 1 ELSE 0 END) AS falling,
                   SUM(CASE WHEN change_pct = 0 THEN 1 ELSE 0 END) AS flat,
                   COUNT(*) AS total
            FROM stock_kline
            WHERE trade_date=?
            GROUP BY source
            """,
            (date_iso,),
        )
        live_breakdown = {
            str(row["source"]): {
                "rising": safe_int(row["rising"]),
                "falling": safe_int(row["falling"]),
                "flat": safe_int(row["flat"]),
                "total": safe_int(row["total"]),
            }
            for row in rows
        }
        if trade_date == "20260901":
            late_backfill_audit[trade_date] = {
                "decision": "audit_only",
                "reason": "late mixed-provider backfill cannot replace the immutable close export",
                "sources": live_breakdown,
                "total": {
                    key: sum(source.get(key, 0) for source in live_breakdown.values())
                    for key in ("rising", "falling", "flat", "total")
                },
            }
            source_breakdown[trade_date] = {
                "immutable_daily_export": dict(FROZEN_SEP1_BREADTH)
            }
            breadth[trade_date] = dict(FROZEN_SEP1_BREADTH)
        else:
            source_breakdown[trade_date] = live_breakdown
            breadth[trade_date] = {
                key: sum(source.get(key, 0) for source in live_breakdown.values())
                for key in ("rising", "falling", "flat", "total")
            }

        broken = int(
            connection.execute(
                "SELECT COUNT(*) FROM broken_limit_pool WHERE trade_date=?", (date_iso,)
            ).fetchone()[0]
            or 0
        )
        down = int(
            connection.execute(
                "SELECT COUNT(*) FROM limit_down_pool WHERE trade_date=?", (date_iso,)
            ).fetchone()[0]
            or 0
        )
        if trade_date == "20260901":
            pool_count_audit[trade_date] = {
                "live_database": {
                    "limit_up_count": len(pools[trade_date]),
                    "broken_limit_count": broken,
                    "limit_down_count": down,
                    "maximum_board": pool_stats[trade_date]["maximum_board"],
                },
                "immutable_close": dict(FROZEN_SEP1_POOL_AUDIT),
            }
            if pool_stats[trade_date]["maximum_board"] != FROZEN_SEP1_POOL_AUDIT["maximum_board"]:
                raise RuntimeError("Sep-1 pool maximum board disagrees with immutable review")
            pool_stats[trade_date]["broken_limit_count"] = FROZEN_SEP1_POOL_AUDIT["broken_limit_count"]
            pool_stats[trade_date]["limit_down_count"] = FROZEN_SEP1_POOL_AUDIT["limit_down_count"]
        else:
            pool_stats[trade_date]["broken_limit_count"] = broken
            pool_stats[trade_date]["limit_down_count"] = down

    write_csv(
        output / "data" / "三日市场宽度冻结口径_20260901_20260903.csv",
        [
            {
                "date": trade_date,
                **breadth[trade_date],
                "limit_up": pool_stats[trade_date]["limit_up_count"],
                "broken_limit": pool_stats[trade_date]["broken_limit_count"],
                "limit_down": pool_stats[trade_date]["limit_down_count"],
                "max_board": pool_stats[trade_date]["maximum_board"],
                "source": (
                    FROZEN_SEP1_DIR.relative_to(ROOT).as_posix()
                    if trade_date == "20260901"
                    else "+".join(sorted(source_breakdown[trade_date]))
                ),
                "version": metadata[trade_date].get("data_version"),
            }
            for trade_date in DATES
        ],
    )

    auto_logs = db_rows(
        connection,
        f"""
        SELECT l.*, a.account_name
        FROM paper_auto_trade_log l
        JOIN paper_account a ON a.id=l.account_id
        WHERE l.account_id IN ({placeholders})
          AND l.trade_date >= '2026-09-01'
          AND l.trade_date <= '2026-09-03'
        ORDER BY l.trade_date,l.created_at,l.id
        """,
        tuple(champion_ids),
    )
    daily_decisions: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    reason_counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    exact_reasons: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for row in auto_logs:
        strategy = CHAMPION_CODES.get(str(row["account_name"]), "?")
        day = str(row["trade_date"]).replace("-", "")
        daily_decisions[(strategy, day)][f"{row['action']}:{row['decision']}"] += 1
        category = normalize_reason(str(row.get("reason") or ""))
        reason_counts[(strategy, day)][category] += 1
        exact_reasons[(strategy, day)][str(row.get("reason") or "")] += 1

    decision_rows: list[dict[str, Any]] = []
    for strategy in "ABCDEF":
        for trade_date in DATES:
            actions = daily_decisions[(strategy, trade_date)]
            reasons = reason_counts[(strategy, trade_date)]
            exact = exact_reasons[(strategy, trade_date)]
            decision_rows.append(
                {
                    "strategy": strategy,
                    "trade_date": trade_date,
                    "log_count": sum(actions.values()),
                    "buy_executed": actions.get("buy:executed", 0),
                    "sell_executed": actions.get("sell:executed", 0),
                    "blocked_count": sum(value for key, value in actions.items() if key.endswith(":blocked")),
                    "wait_count": sum(value for key, value in actions.items() if key.endswith(":wait")),
                    "top_reason_category": reasons.most_common(1)[0][0] if reasons else "",
                    "top_reason_category_count": reasons.most_common(1)[0][1] if reasons else 0,
                    "top_exact_reason": exact.most_common(1)[0][0] if exact else "",
                    "top_exact_reason_count": exact.most_common(1)[0][1] if exact else 0,
                    "action_decision_counts_json": json.dumps(dict(actions), ensure_ascii=False, sort_keys=True),
                    "reason_category_counts_json": json.dumps(dict(reasons), ensure_ascii=False, sort_keys=True),
                }
            )
    write_csv(output / "data" / "ABCDEF逐日决策漏斗_20260901_20260903.csv", decision_rows)

    # Reconstruct the actual pre-sell weighted entry basis.  A later rebound is
    # not automatically a strategy error: a stop can still be correct when the
    # stock remains below cost, and a five-minute high is not assumed fillable.
    basis_before_trade: dict[int, float | None] = {}
    inventory: dict[tuple[int, str], dict[str, float]] = defaultdict(
        lambda: {"quantity": 0.0, "cost_value": 0.0}
    )
    for row in trades:
        key = (int(row["account_id"]), str(row["code"]))
        state = inventory[key]
        basis_before_trade[int(row["id"])] = (
            state["cost_value"] / state["quantity"] if state["quantity"] > 0 else None
        )
        quantity = max(int(row.get("amount") or 0), 0)
        if row["trade_type"] == "buy":
            state["cost_value"] += float(row["price"]) * quantity
            state["quantity"] += quantity
        elif state["quantity"] > 0:
            sold = min(float(quantity), state["quantity"])
            average = state["cost_value"] / state["quantity"]
            state["quantity"] -= sold
            state["cost_value"] = average * state["quantity"]

    final_limit_codes = {
        trade_date: {str(row["code"]).zfill(6) for row in pools[trade_date]}
        for trade_date in DATES
    }
    trade_review_rows: list[dict[str, Any]] = []
    sold_fly_rows: list[dict[str, Any]] = []
    for row in trades:
        stamp = datetime.fromisoformat(str(row["trade_time"]))
        date_key = stamp.strftime("%Y%m%d")
        minute_key = stamp.strftime("%H%M")
        points = [
            item
            for item in m5_by_code.get(str(row["code"]), [])
            if item["date"] == date_key
            and (hhmm_to_minutes(item["time"]) or 0) >= (hhmm_to_minutes(minute_key) or 0)
        ]
        later_high = max((safe_float(item.get("high"), 0.0) or 0.0 for item in points), default=None)
        day_close = safe_float(points[-1].get("close")) if points else None
        entry_basis = basis_before_trade.get(int(row["id"]))
        missed_high_pct = (
            (later_high / float(row["price"]) - 1.0) * 100.0
            if row["trade_type"] == "sell" and later_high and row["price"]
            else None
        )
        close_after_sell_pct = (
            (day_close / float(row["price"]) - 1.0) * 100.0
            if row["trade_type"] == "sell" and day_close and row["price"]
            else None
        )
        close_vs_entry_pct = (
            (day_close / entry_basis - 1.0) * 100.0
            if row["trade_type"] == "sell" and day_close and entry_basis
            else None
        )
        sold_fly_class = ""
        if row["trade_type"] == "sell":
            closed_limit_up = str(row["code"]) in final_limit_codes.get(date_key, set())
            if closed_limit_up and (close_after_sell_pct or -999) >= 5.0:
                sold_fly_class = "涨停级卖飞"
            elif (
                (close_after_sell_pct or -999) >= 3.0
                and (close_vs_entry_pct or -999) > 0.0
            ):
                sold_fly_class = "收盘确认卖早"
            elif (
                (close_after_sell_pct or -999) >= 1.5
                and (close_vs_entry_pct or -999) > 0.0
            ):
                sold_fly_class = "轻度卖早"
            elif (missed_high_pct or -999) >= 2.0:
                sold_fly_class = "仅盘中反抽"
            elif (close_after_sell_pct or -999) >= 1.0:
                sold_fly_class = "止损后小幅反抽"
            else:
                sold_fly_class = "未卖飞/风控退出"
            sold_fly_rows.append(
                {
                    "strategy": row["strategy_code"],
                    "trade_date": date_key,
                    "trade_time": row["trade_time"],
                    "code": row["code"],
                    "name": names.get(str(row["code"]), ""),
                    "entry_basis": round(entry_basis, 4) if entry_basis else None,
                    "sell_price": row["price"],
                    "amount": row["amount"],
                    "realized_pnl": row.get("realized_pnl"),
                    "later_high": round(later_high, 4) if later_high else None,
                    "day_close": round(day_close, 4) if day_close else None,
                    "missed_high_pct": round(missed_high_pct, 4) if missed_high_pct is not None else None,
                    "close_after_sell_pct": round(close_after_sell_pct, 4) if close_after_sell_pct is not None else None,
                    "close_vs_entry_pct": round(close_vs_entry_pct, 4) if close_vs_entry_pct is not None else None,
                    "closed_limit_up": closed_limit_up,
                    "classification": sold_fly_class,
                    "reason": row.get("reason"),
                }
            )
        trade_review_rows.append(
            {
                "strategy": row["strategy_code"],
                "account_id": row["account_id"],
                "trade_date": date_key,
                "trade_time": row["trade_time"],
                "code": row["code"],
                "name": names.get(str(row["code"]), ""),
                "trade_type": row["trade_type"],
                "price": row["price"],
                "amount": row["amount"],
                "commission": row.get("commission"),
                "realized_pnl": row.get("realized_pnl"),
                "entry_basis_before_trade": round(entry_basis, 4) if entry_basis else None,
                "strategy_version": row.get("strategy_version"),
                "reason": row.get("reason"),
                "later_high_pct_if_sell": round(missed_high_pct, 4) if missed_high_pct is not None else None,
                "close_after_sell_pct": round(close_after_sell_pct, 4) if close_after_sell_pct is not None else None,
                "close_vs_entry_pct": round(close_vs_entry_pct, 4) if close_vs_entry_pct is not None else None,
                "sold_fly_classification": sold_fly_class,
            }
        )
    write_csv(output / "data" / "ABCDEF模拟盘成交复盘_20260901_20260903.csv", trade_review_rows)
    write_csv(output / "data" / "ABCDEF卖飞检查_20260901_20260903.csv", sold_fly_rows)

    plot_market_overview(output, advancers, pools, breadth, pool_stats)
    for trade_date in DATES:
        pool_sorted = sorted(
            pools[trade_date],
            key=lambda row: (
                -safe_int(row.get("consecutive_days"), 1),
                str(row.get("limit_up_time") or "999999"),
                str(row.get("code") or ""),
            ),
        )
        plot_path_grid(
            output / f"涨停池5分钟分时全景_{trade_date}.png",
            f"{DATE_LABELS[trade_date]}涨停池5分钟分时全样本（{len(pool_sorted)}只；红=首板，紫=连板）",
            pool_sorted,
            m5_by_code,
            trade_date,
        )
        top_risers = [
            row
            for row in advancers[trade_date]
            if row.get("board_tag") == "tradeable" and row.get("in_limit_up_pool") != "1"
        ][:15]
        plot_path_grid(
            output / f"上涨个股代表性5分钟分时_{trade_date}.png",
            f"{DATE_LABELS[trade_date]}可交易非涨停股涨幅前15（代表性样本，不是全体上涨股）",
            top_risers,
            m5_by_code,
            trade_date,
            max_items=15,
        )
    plot_trades(output, trades, m5_by_code, names)

    account_facts = {
        CHAMPION_CODES[name]: {
            "account_id": row["id"],
            "account_name": name,
            "initial_capital": row["initial_capital"],
            "total_assets": row["total_assets"],
            "total_return_pct": row["total_return"],
            "max_drawdown_pct": row["max_drawdown"],
            "win_rate": row["win_rate"],
            "trade_count_in_window": sum(item["account_id"] == row["id"] for item in trades),
        }
        for name, row in champion_account_by_name.items()
    }
    manifest = {
        "generated_at": datetime.now().isoformat(),
        "review_window": ["2026-09-01", "2026-09-03"],
        "database": {
            "path": str(args.db.resolve()),
            "mode": "read_only",
            "sha256_not_computed": "8.5GB live database; tables and row-level facts are recorded instead",
        },
        "daily_export_metadata": metadata,
        "breadth": breadth,
        "breadth_scope": {
            "20260901": "immutable close export",
            "20260902": "formal close rows in live database",
            "20260903": "formal close rows in live database",
        },
        "source_breakdown": source_breakdown,
        "late_backfill_audit": late_backfill_audit,
        "limit_pool": pool_stats,
        "pool_count_audit": pool_count_audit,
        "champion_accounts": account_facts,
        "paper_trade_count": len(trades),
        "paper_auto_log_count": len(auto_logs),
        "sold_fly_counts": dict(Counter(row["classification"] for row in sold_fly_rows)),
        "m5": {
            "source": "Tencent public mkline endpoint",
            "frequency": "5m",
            "requested_bars": 640,
            "selected_universe": "all daily limit-up members + top15 tradeable non-limit advancers + all A-F traded codes",
            "symbol_count": len(selected_codes),
            "manifest": fetch_manifest,
        },
        "quality_notes": [
            "2026-09-01 breadth, advancers, and limit-up members use immutable export daily_export_e3d4f43ca1c8; the mixed late-backfilled ths/spot_fallback database snapshot is retained only under late_backfill_audit.",
            "Intraday reconstruction uses 48 five-minute bars per complete session; official limit_up_time from the pool remains the more precise first-seal timestamp.",
            "Top-riser charts are explicitly representative samples. The full advancer denominator remains in each daily CSV.",
            "Sell-flight labels compare only later same-day five-minute highs and closes with the actual sell price; they do not assume that the later high was fillable.",
            "No database row, paper order, Champion strategy threshold, or real broker connection is modified by this script.",
        ],
        "outputs": {
            "market_breadth_scope": "data/三日市场宽度冻结口径_20260901_20260903.csv",
            "pool_features": "data/三日涨停池5分钟形态特征_20260901_20260903.csv",
            "representative_intraday": "data/三日代表样本5分钟分时_20260901_20260903.csv",
            "decision_funnel": "data/ABCDEF逐日决策漏斗_20260901_20260903.csv",
            "trades": "data/ABCDEF模拟盘成交复盘_20260901_20260903.csv",
            "sold_fly": "data/ABCDEF卖飞检查_20260901_20260903.csv",
        },
    }
    correction = {
        "as_of": "2026-09-03 close",
        "authoritative_market_breadth": {
            trade_date: {
                **breadth[trade_date],
                "limit_up": pool_stats[trade_date]["limit_up_count"],
                "broken_limit": pool_stats[trade_date]["broken_limit_count"],
                "limit_down": pool_stats[trade_date]["limit_down_count"],
                "max_board": pool_stats[trade_date]["maximum_board"],
                "version": metadata[trade_date].get("data_version"),
            }
            for trade_date in DATES
        },
        "data_quality_audit": late_backfill_audit,
    }
    (output / "三日复盘口径修正_20260901_20260903.json").write_text(
        json.dumps(correction, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    (output / "三日复盘事实清单_20260901_20260903.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    connection.close()
    print(
        json.dumps(
            {
                "output_dir": str(output),
                "selected_symbols": len(selected_codes),
                "paper_trades": len(trades),
                "paper_auto_logs": len(auto_logs),
                "pool_stats": pool_stats,
                "sold_fly_counts": manifest["sold_fly_counts"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
