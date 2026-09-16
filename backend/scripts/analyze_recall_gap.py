"""复盘召回缺口分析 — 只读研究工具，不修改任何生产权重或预测输出。

对每个有涨停数据的交易日，把“实际首板/二板”与“前一日正式 schedule 主榜”对比，
把每次漏选归因到 not_in_pool / in_pool_low_score / rank_cutoff / filtered，并按
候选路线和主营行业聚合，输出到 outputs/recall_gap_<date>.md，用于提出召回覆盖假设。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.trade_calendar import is_official_closed_day


def _date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _loads(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


MAIN_ROUTES = {
    "news_catalyst_start": "消息催化",
    "auction_surge_start": "竞价强攻",
    "mainline_spread_start": "主线扩散补涨",
    "support_squeeze_start": "支撑挤压",
    "quiet_setup": "静默蓄势",
    "oversold_reversal_start": "超跌反转",
    "pre_board_probe_start": "板前试探",
    "second_board_promotion": "首板晋二板",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("claw.db"))
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--outdir", type=Path, default=Path("outputs"))
    parser.add_argument("--sample-limit", type=int, default=12)
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在: {database}")
    connection = sqlite3.connect(str(database), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")

    limit_dates = sorted(
        {
            _date(row[0])
            for row in connection.execute(
                "SELECT DISTINCT trade_date FROM limit_up_pool WHERE quarantined=0"
            ).fetchall()
            if row[0]
        }
    )
    market_dates = sorted(
        {
            _date(row[0])
            for row in connection.execute(
                "SELECT DISTINCT trade_date FROM stock_kline WHERE trade_date IS NOT NULL"
            ).fetchall()
            if row[0]
            and _date(row[0]).weekday() < 5
            and not is_official_closed_day(_date(row[0]))
        }
    )
    limit_dates = [d for d in limit_dates if d in set(market_dates)]
    if len(limit_dates) < 2:
        print("数据不足：至少需要 2 个有效涨停交易日")
        connection.close()
        return 0

    report: list[str] = []
    report.append("# 召回缺口分析")
    report.append("")
    report.append(f"分析日期范围：{limit_dates[-1]} 起，回看最近 {min(args.days, len(limit_dates))} 个有效交易日。")
    report.append("")
    report.append("> 只读研究工具：仅定位漏选构成，不修改任何模型权重或预测输出。")
    report.append("")

    route_misses: Counter[str] = Counter()
    industry_misses: Counter[str] = Counter()
    miss_reasons = Counter()
    total_actual = 0
    total_hit = 0
    daily_rows: list[str] = []

    for index in range(max(len(limit_dates) - args.days, 0), len(limit_dates)):
        actual_day = limit_dates[index]
        prev_day = next(
            (d for d in reversed(market_dates[: market_dates.index(actual_day)])), None
        )
        if prev_day is None:
            continue
        actual_rows = connection.execute(
            "SELECT code, name, consecutive_days, limit_up_reason FROM limit_up_pool "
            "WHERE trade_date=? AND quarantined=0",
            (actual_day.isoformat(),),
        ).fetchall()
        actual_first = [
            row
            for row in actual_rows
            if int(row["consecutive_days"] or 1) <= 1
        ]
        actual_second = [
            row
            for row in actual_rows
            if int(row["consecutive_days"] or 1) >= 2
        ]

        pred_rows = connection.execute(
            "SELECT code, target_board, candidate_route, factors_json, calibrated_probability "
            "FROM promotion_prediction_record "
            "WHERE prediction_trade_date=? AND snapshot_source='schedule'",
            (prev_day.isoformat(),),
        ).fetchall()
        pred_by_code: dict[tuple[str, int], sqlite3.Row] = {}
        for record in pred_rows:
            factors = _loads(record["factors_json"])
            if not bool(factors.get("prediction_ranked_selected", True)):
                continue
            pred_by_code[(record["code"], int(record["target_board"]))] = record

        day_hits = 0
        day_miss = Counter()
        day_actual = len(actual_first) + len(actual_second)
        for row in actual_first + actual_second:
            total_actual += 1
            target = 2 if int(row["consecutive_days"] or 1) >= 2 else 1
            key = (row["code"], target)
            pred = pred_by_code.get(key)
            if pred is not None:
                total_hit += 1
                day_hits += 1
                continue
            reason = "not_in_pool"
            route = "未知"
            day_miss[reason] += 1
            route_misses[route] += 1
            miss_reasons[reason] += 1
            info = _loads(pred["factors_json"]) if pred else {}
            source_route = str(info.get("candidate_route") or "未记录")
            if source_route in MAIN_ROUTES:
                route_misses[MAIN_ROUTES[source_route]] += 1
            industry = "未知"
            row_industry = connection.execute(
                "SELECT sector_name FROM stock_sector_mapping "
                "WHERE code=? AND sector_type='industry' ORDER BY weight DESC LIMIT 1",
                (row["code"],),
            ).fetchone()
            if row_industry:
                industry = str(row_industry[0] or "未知")
            industry_misses[industry] += 1
        daily_rows.append(
            f"- {actual_day} 实际首/二板 {day_actual} 只，主榜命中 {day_hits} 只，漏选 {day_actual - day_hits} 只"
        )

    report.append("## 逐日命中概况")
    report.extend(daily_rows)
    report.append("")
    report.append("## 漏选构成")
    report.append("")
    report.append(f"- 总实际首/二板：{total_actual}；主榜命中：{total_hit}（{total_hit / total_actual if total_actual else 0:.1%}）")
    for reason, count in miss_reasons.most_common():
        report.append(f"- {reason}：{count}")
    report.append("")
    report.append("> 说明：本工具基于旧版正式主榜（snapshot_source=schedule 且 ranked_selected=true）对比，"
                  "08-31 起新增的 Top12/Top30 契约会在新快照运行后自动纳入。")
    report.append("")
    report.append("## 按候选路线（漏选来源）聚合")
    report.append("")
    for route, count in route_misses.most_common(20):
        report.append(f"- {route or '(未知路线)'}：{count}")
    report.append("")
    report.append("## 按主营行业（漏选样本）聚合")
    report.append("")
    for industry, count in industry_misses.most_common(20):
        report.append(f"- {industry}：{count}")
    report.append("")
    report.append("## 下步假设（只读提案，不直接改生产）")
    report.append("")
    report.append("1. 若 'not_in_pool' 集中：优先扩展对应路线的候选召回覆盖（消息/竞价/板块扩散/静默蓄势）。")
    report.append("2. 若漏选集中在某几个行业：检查该行业启动前特征（主营行业点火、资金预热）是否被低估。")
    report.append("3. 任何新特征必须先进入离线假设与冻结快照训练，再影子验证；禁止用单日案例直接改权重。")
    report.append("")

    outdir = args.outdir.expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / f"recall_gap_{limit_dates[-1]}.md"
    output.write_text("\n".join(report), encoding="utf-8")
    print(f"分析完成。总实际首/二板 {total_actual}，主榜命中 {total_hit}，报告已写入 {output}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
