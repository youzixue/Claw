"""回放首板 v26 Top12/Top30，并比较 900 与 2000 槽位的严格 as-of 召回。"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import promotion


def parse_date(value) -> date:
    return date.fromisoformat(str(value)[:10])


def read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def parse_json(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_datetime(value) -> datetime:
    raw = str(value or "").strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.min


def canonical_batches(connection, start_date: date, end_date: date) -> dict:
    rows = connection.execute(
        """
        SELECT id, code, name, prediction_trade_date, candidate_route,
               predicted_probability, calibrated_probability, factors_json,
               snapshot_batch_key, snapshot_recorded_at, model_version
        FROM promotion_prediction_record
        WHERE target_board = 1
          AND snapshot_source = 'schedule'
          AND snapshot_context = 'promotion_2000'
          AND prediction_trade_date BETWEEN ? AND ?
        ORDER BY prediction_trade_date, snapshot_recorded_at, id
        """,
        (str(start_date), str(end_date)),
    ).fetchall()
    grouped = defaultdict(lambda: defaultdict(list))
    times = {}
    for row in rows:
        prediction_date = parse_date(row["prediction_trade_date"])
        batch_key = str(row["snapshot_batch_key"] or "")
        grouped[prediction_date][batch_key].append(row)
        times[(prediction_date, batch_key)] = max(
            times.get((prediction_date, batch_key), datetime.min),
            parse_datetime(row["snapshot_recorded_at"]),
        )
    selected = {}
    for prediction_date, batches in grouped.items():
        latest_key = max(
            batches,
            key=lambda key: times[(prediction_date, key)],
        )
        selected[prediction_date] = batches[latest_key]
    return selected


def next_market_date(connection, prediction_date: date) -> date | None:
    row = connection.execute(
        "SELECT MIN(trade_date) FROM limit_up_pool WHERE trade_date > ?",
        (str(prediction_date),),
    ).fetchone()
    return parse_date(row[0]) if row and row[0] else None


def actual_first_board_codes(connection, actual_date: date) -> set[str]:
    rows = connection.execute(
        """
        SELECT code, name FROM limit_up_pool
        WHERE trade_date = ? AND COALESCE(consecutive_days, 1) = 1
        """,
        (str(actual_date),),
    ).fetchall()
    result = set()
    for row in rows:
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "").upper().replace(" ", "")
        if code and "ST" not in name and promotion.stock_tagger.is_tradeable(code):
            result.add(code)
    return result


def record_item(row) -> dict:
    factors = parse_json(row["factors_json"])
    route_probability = promotion._safe_float(
        row["calibrated_probability"],
        promotion._safe_float(row["predicted_probability"]),
    )
    item = {
        **factors,
        "code": str(row["code"] or "").strip(),
        "name": str(row["name"] or ""),
        "target_board": 1,
        "candidate_route": str(row["candidate_route"] or ""),
        "probability": route_probability,
        "route_calibrated_probability": route_probability,
        "probability_factors": factors,
        "sub_probabilities": factors.get("sub_probabilities") or {},
        "is_tradeable": True,
    }
    temporal_probability = promotion._first_board_temporal_probability(item)
    probability = promotion._clamp_probability(
        temporal_probability * 0.75 + route_probability * 0.25
    )
    item["temporal_event_probability"] = temporal_probability
    item["probability"] = probability
    item["limit_up_probability"] = probability
    item["probability_factors"] = {
        **factors,
        "route_calibrated_probability": route_probability,
        "temporal_event_probability": temporal_probability,
    }
    return item


def ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def auc(rows: list[tuple[float, bool]]) -> float | None:
    ordered = sorted(rows, key=lambda pair: pair[0])
    positives = sum(bool(label) for _score, label in ordered)
    negatives = len(ordered) - positives
    if not positives or not negatives:
        return None
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = (index + 1 + end) / 2.0
        rank_sum += average_rank * sum(
            bool(label) for _score, label in ordered[index:end]
        )
        index = end
    return round(
        (rank_sum - positives * (positives + 1) / 2.0)
        / (positives * negatives),
        4,
    )


def probability_metrics(rows: list[tuple[float, bool]]) -> dict:
    if not rows:
        return {
            "sample_count": 0,
            "average_probability": 0.0,
            "observed_rate": 0.0,
            "brier_score": 0.0,
            "roc_auc": None,
        }
    return {
        "sample_count": len(rows),
        "average_probability": round(
            sum(score for score, _label in rows) / len(rows),
            4,
        ),
        "observed_rate": round(
            sum(bool(label) for _score, label in rows) / len(rows),
            4,
        ),
        "brier_score": round(
            sum((score - int(label)) ** 2 for score, label in rows)
            / len(rows),
            5,
        ),
        "roc_auc": auc(rows),
    }


def replay_rank(connection, batches: dict) -> dict:
    daily = []
    route_probability_rows = []
    temporal_probability_rows = []
    blend_probability_rows = []
    asof_violations = []
    for prediction_date in sorted(batches):
        rows = batches[prediction_date]
        actual_date = next_market_date(connection, prediction_date)
        if actual_date is None:
            continue
        items = [record_item(row) for row in rows]

        # 先生成全部名单，随后才查询 actual_codes，保证结果不参与特征和排序。
        formal = promotion._rank_first_board_candidates(
            items, promotion.PROMOTION_MAX_FORMAL_RANKED
        )
        recall = promotion._rank_first_board_recall_candidates(
            items, formal, promotion.PROMOTION_MAX_RECALL_RANKED
        )
        current = [
            item for item in items
            if (item.get("probability_factors") or {}).get(
                "prediction_ranked_selected"
            ) is True
        ]

        for row in rows:
            recorded_at = parse_datetime(row["snapshot_recorded_at"])
            if recorded_at.date() != prediction_date:
                asof_violations.append({
                    "prediction_date": str(prediction_date),
                    "code": row["code"],
                    "snapshot_recorded_at": str(row["snapshot_recorded_at"]),
                })

        actual_codes = actual_first_board_codes(connection, actual_date)
        pool_codes = {item["code"] for item in items}
        current_codes = {item["code"] for item in current}
        formal_codes = {item["code"] for item in formal}
        recall_codes = {item["code"] for item in recall}
        for item in items:
            label = item["code"] in actual_codes
            route_probability_rows.append((
                promotion._safe_float(item["route_calibrated_probability"]),
                label,
            ))
            temporal_probability_rows.append((
                promotion._safe_float(item["temporal_event_probability"]),
                label,
            ))
            blend_probability_rows.append((
                promotion._safe_float(item["probability"]),
                label,
            ))
        daily.append({
            "prediction_date": str(prediction_date),
            "actual_date": str(actual_date),
            "model_version": str(rows[0]["model_version"] or "legacy"),
            "actual_first_board_count": len(actual_codes),
            "pool_count": len(pool_codes),
            "pool_hit_count": len(pool_codes & actual_codes),
            "current_count": len(current_codes),
            "current_hit_count": len(current_codes & actual_codes),
            "formal_count": len(formal_codes),
            "formal_hit_count": len(formal_codes & actual_codes),
            "recall_count": len(recall_codes),
            "recall_hit_count": len(recall_codes & actual_codes),
        })

    actual_total = sum(row["actual_first_board_count"] for row in daily)

    def aggregate(prefix: str) -> dict:
        count = sum(row[f"{prefix}_count"] for row in daily)
        hits = sum(row[f"{prefix}_hit_count"] for row in daily)
        return {
            "samples": count,
            "hits": hits,
            "precision": ratio(hits, count),
            "recall": ratio(hits, actual_total),
            "hit_days": sum(row[f"{prefix}_hit_count"] > 0 for row in daily),
        }

    pool_hits = sum(row["pool_hit_count"] for row in daily)
    return {
        "date_count": len(daily),
        "date_range": (
            f"{daily[0]['prediction_date']}..{daily[-1]['prediction_date']}"
            if daily else ""
        ),
        "actual_first_board_count": actual_total,
        "pool": {"hits": pool_hits, "recall": ratio(pool_hits, actual_total)},
        "current_formal": aggregate("current"),
        "v26_formal_top12": aggregate("formal"),
        "v26_recall_top30": aggregate("recall"),
        "probability_calibration": {
            "route_posterior": probability_metrics(route_probability_rows),
            "temporal_logit": probability_metrics(temporal_probability_rows),
            "v26_blend": probability_metrics(blend_probability_rows),
        },
        "temporal_probability_auc": auc(temporal_probability_rows),
        "asof_violation_count": len(asof_violations),
        "asof_violations": asof_violations[:20],
        "daily": daily,
    }


async def replay_scan(db_path: Path, prediction_dates: list[date]) -> dict:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.resolve()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    connection = read_only(db_path)
    daily = []
    try:
        async with Session() as db:
            for prediction_date in prediction_dates:
                actual_date = next_market_date(connection, prediction_date)
                if actual_date is None:
                    continue
                exclude_codes = {
                    str(row[0] or "").strip()
                    for row in connection.execute(
                        "SELECT code FROM limit_up_pool WHERE trade_date = ?",
                        (str(prediction_date),),
                    )
                }
                context_map = await promotion._load_pre_board_probe_context_map(
                    db,
                    prediction_date,
                    exclude_codes=exclude_codes,
                    market_context={},
                    limit=2000,
                )
                ordered_codes = list(context_map)
                actual_codes = actual_first_board_codes(connection, actual_date)
                hits_900 = len(set(ordered_codes[:900]) & actual_codes)
                hits_2000 = len(set(ordered_codes[:2000]) & actual_codes)
                daily.append({
                    "prediction_date": str(prediction_date),
                    "actual_date": str(actual_date),
                    "actual_first_board_count": len(actual_codes),
                    "scan_900_count": min(len(ordered_codes), 900),
                    "scan_900_hit_count": hits_900,
                    "scan_2000_count": min(len(ordered_codes), 2000),
                    "scan_2000_hit_count": hits_2000,
                })
    finally:
        connection.close()
        await engine.dispose()

    actual_total = sum(row["actual_first_board_count"] for row in daily)
    hits_900 = sum(row["scan_900_hit_count"] for row in daily)
    hits_2000 = sum(row["scan_2000_hit_count"] for row in daily)
    return {
        "date_count": len(daily),
        "actual_first_board_count": actual_total,
        "scan_900_hit_count": hits_900,
        "scan_900_recall": ratio(hits_900, actual_total),
        "scan_2000_hit_count": hits_2000,
        "scan_2000_recall": ratio(hits_2000, actual_total),
        "incremental_hits": hits_2000 - hits_900,
        "daily": daily,
    }


def markdown(report: dict) -> str:
    rank = report["rank_replay"]
    scan = report["strict_scan_replay"]
    lines = [
        "# 首板预测 v26 无未来函数回测",
        "",
        f"- 训练截止：{report['training_cutoff']}",
        f"- 时间外回放：{rank['date_range']}（{rank['date_count']} 日）",
        f"- as-of 违规：{rank['asof_violation_count']}",
        "",
        "| 口径 | 样本 | 命中 | 精度 | 召回 | 命中日 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, key in (
        ("旧正式榜", "current_formal"),
        ("v26 正式 Top12", "v26_formal_top12"),
        ("v26 Top30 宽召回", "v26_recall_top30"),
    ):
        item = rank[key]
        lines.append(
            f"| {label} | {item['samples']} | {item['hits']} | "
            f"{item['precision']:.2%} | {item['recall']:.2%} | "
            f"{item['hit_days']} |"
        )
    lines += [
        "",
        f"- 候选池召回：{rank['pool']['hits']}/"
        f"{rank['actual_first_board_count']} = {rank['pool']['recall']:.2%}",
        f"- 时序概率全池 AUC：{rank['temporal_probability_auc']}",
        f"- 路由后验 Brier：{rank['probability_calibration']['route_posterior']['brier_score']}",
        f"- v26 混合概率 Brier：{rank['probability_calibration']['v26_blend']['brier_score']}",
        "",
        "## 严格扫描覆盖",
        "",
        f"- 900 槽位：{scan['scan_900_hit_count']}/"
        f"{scan['actual_first_board_count']} = {scan['scan_900_recall']:.2%}",
        f"- 2000 槽位：{scan['scan_2000_hit_count']}/"
        f"{scan['actual_first_board_count']} = {scan['scan_2000_recall']:.2%}",
        f"- 2000 相对 900 增量命中：{scan['incremental_hits']}",
        "",
        "## 口径",
        "",
        "- 正样本只认下一交易日 limit_up_pool.consecutive_days == 1。",
        "- 排名仅使用 T 日 promotion_2000 持久化快照，真实结果在名单生成后查询。",
        "- 扫描走生产 as-of 路径；T+2/T+3 StockSpot 快照会被拒绝。",
        "- Top12 是精排短名单；Top30 是宽召回观察层，不是买入清单。",
        "- prediction_actionable 独立表达交易执行性，不删除预测样本。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="claw.db")
    parser.add_argument("--start-date", default="2026-08-12")
    parser.add_argument("--end-date", default="2026-08-27")
    parser.add_argument("--training-cutoff", default="2026-08-10")
    parser.add_argument("--scan-dates", type=int, default=7)
    parser.add_argument(
        "--output-prefix",
        default="outputs/first_board_rank_v26_holdout",
    )
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"database not found: {db_path}")
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if start_date > end_date:
        parser.error("start-date must be <= end-date")

    connection = read_only(db_path)
    try:
        batches = canonical_batches(connection, start_date, end_date)
        rank_replay = replay_rank(connection, batches)
    finally:
        connection.close()
    scan_count = max(args.scan_dates, 0)
    scan_dates = sorted(batches)[-scan_count:] if scan_count else []
    strict_scan_replay = asyncio.run(replay_scan(db_path, scan_dates))

    report = {
        "model_version": promotion.PROMOTION_MODEL_VERSION,
        "calibration_version": (
            promotion.PROMOTION_FIRST_BOARD_TEMPORAL_CALIBRATION_VERSION
        ),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "training_cutoff": args.training_cutoff,
        "holdout_start": str(start_date),
        "holdout_end": str(end_date),
        "asof_policy": {
            "ranking": "T-day persisted schedule:promotion_2000 batch",
            "truth": "first limit_up_pool date after T; consecutive_days == 1",
            "spot_reconciliation": "only exact next observed market date",
            "operation_order": "rank lists built before actual truth query",
        },
        "rank_replay": rank_replay,
        "strict_scan_replay": strict_scan_replay,
    }
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = prefix.with_suffix(".json")
    md_path = prefix.with_suffix(".md")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "json": str(json_path),
        "markdown": str(md_path),
        "rank_replay": {
            key: rank_replay[key]
            for key in (
                "date_count", "current_formal", "v26_formal_top12",
                "v26_recall_top30", "probability_calibration",
                "temporal_probability_auc", "asof_violation_count",
            )
        },
        "strict_scan_replay": {
            key: strict_scan_replay[key]
            for key in (
                "date_count", "scan_900_recall",
                "scan_2000_recall", "incremental_hits",
            )
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
