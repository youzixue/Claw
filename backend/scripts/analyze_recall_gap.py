"""只读召回诊断：不可变完整池与主榜分开；原始观察不等于认证命中率。

日期范围是结局交易日，as-of 为 Asia/Shanghai 无时区时间。默认 20:00
盘后 context；最新尝试失败不回退。不会调用生产 API、补表、回填或重评分。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.stock_tagger import StockTagger
from app.promotion.modeling.ledger_dataset import _batch_error
from app.promotion.outcome_evidence import next_recorded_trade_day, _is_completed_outcome_date


def _loads(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


@contextmanager
def readonly_database(path):
    """A consistent read transaction, including WAL; never immutable=1 on live DB."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"数据库不存在: {path}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _tables(connection):
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _object(row):
    result = dict(row)
    for key in ("as_of_at", "created_at", "completed_at"):
        if key in result:
            try:
                result[key] = datetime.fromisoformat(result[key]) if result[key] else None
            except (ValueError, TypeError):
                result[key] = None
    for key in ("reference_trade_date", "prediction_trade_date"):
        if key in result:
            result[key] = date.fromisoformat(result[key])
    if "gate_passed" in result:
        result["gate_passed"] = {0: False, 1: True}.get(result["gate_passed"])
    return SimpleNamespace(**result)


def select_batch(connection, prediction_day, context, cutoff):
    tables = _tables(connection)
    if not {"promotion_prediction_run", "promotion_prediction_snapshot"} <= tables:
        return None, [], "immutable_ledger_missing"
    runs = [_object(r) for r in connection.execute(
        "SELECT * FROM promotion_prediction_run WHERE reference_trade_date=? "
        "AND snapshot_source='schedule' AND snapshot_context=?",
        (prediction_day.isoformat(), context))]
    if not runs:
        return None, [], "latest_run_missing"
    # Same selection as ledger_dataset: do NOT prefilter failed/future/partial attempts.
    run = max(runs, key=lambda r: (r.as_of_at if isinstance(r.as_of_at, datetime)
                                  and r.as_of_at.tzinfo is None else datetime.max, r.id))
    snapshots = [_object(r) for r in connection.execute(
        "SELECT * FROM promotion_prediction_snapshot WHERE run_id=? ORDER BY id", (run.id,))]
    error = _batch_error(run, snapshots, cutoff)
    if not error and any(s.prediction_trade_date != prediction_day or s.horizon_days != 1
                         for s in snapshots):
        error = "inconsistent_target_lane"
    if not error and len({(s.code, s.target_board) for s in snapshots}) != len(snapshots):
        error = "duplicate_stock_in_target_lane"
    return run, snapshots, error


def classify(snapshot, batch_error):
    if batch_error:
        return "snapshot_incomplete", batch_error
    if snapshot is None:
        return "recall_miss", "absent_from_verified_whole_batch"
    f = _loads(snapshot.features_json)
    if (f.get("prediction_rank_contract_version") != "promotion_rank_contract_v1"
            or f.get("prediction_rank_contract_complete") is not True
            or type(f.get("prediction_ranked_selected")) is not bool
            or type(f.get("prediction_rank_eligible")) is not bool):
        return "unknown", "rank_contract_missing"
    selected, eligible = f["prediction_ranked_selected"], f["prediction_rank_eligible"]
    if selected:
        if not eligible or snapshot.rank_scope != "ranked" or not snapshot.rank_position:
            return "unknown", "rank_contract_conflict"
        return "hit", "frozen_main_rank_selected"
    if snapshot.rank_scope == "ranked":
        return "unknown", "rank_contract_conflict"
    if not eligible:
        # This is ranking eligibility, NOT an inferred execution/trade-gate reason.
        return "filtered", "frozen_prediction_rank_eligible_false"
    return "ranking_miss", "in_full_pool_not_main_rank"


def analyze(connection, *, as_of, start_date=None, end_date=None, days=10,
            context="promotion_2000"):
    if as_of.tzinfo is not None:
        raise ValueError("as-of 必须为 Asia/Shanghai 无时区时间")
    if context not in {"promotion_1510", "promotion_2000"}:
        raise ValueError("次日召回诊断只支持独立盘后 context")
    if days <= 0 or (start_date and end_date and start_date > end_date):
        raise ValueError("无效日期范围或 days")
    tables = _tables(connection)
    calendar = {}
    if "trade_calendar" in tables:
        calendar = {date.fromisoformat(r[0]): bool(r[1]) if r[1] in (0, 1) else None
                    for r in connection.execute("SELECT trade_date,is_trade_day FROM trade_calendar")}
    pool_days = set()
    if "limit_up_pool" in tables:
        pool_days = {date.fromisoformat(r[0]) for r in connection.execute(
            "SELECT DISTINCT trade_date FROM limit_up_pool WHERE trade_date<=?",
            (as_of.date().isoformat(),)) if r[0]}
    dates = sorted({d for d, opened in calendar.items() if opened} | pool_days)
    dates = [d for d in dates if d <= as_of.date() and (not start_date or d >= start_date)
             and (not end_date or d <= end_date)]
    if start_date is None:
        dates = dates[-days:]
    tagger = StockTagger()
    daily = []
    for actual_day in dates:
        previous = max((d for d, opened in calendar.items() if opened and d < actual_day), default=None)
        calendar_error = "prediction_calendar_unknown"
        if previous:
            next_day, calendar_error = next_recorded_trade_day(previous, calendar, through=actual_day)
            if next_day != actual_day:
                calendar_error = calendar_error or "not_immediate_next_session"
        run, snapshots, error = None, [], calendar_error
        if not error:
            cutoff = min(as_of, datetime.combine(actual_day, time(9, 15)))
            run, snapshots, error = select_batch(connection, previous, context, cutoff)
        if not _is_completed_outcome_date(actual_day, now=as_of):
            error = "outcome_not_closed"
        by_key = {(s.code, s.target_board): s for s in snapshots}
        # Existing ledger has no sealed per-lane successful-empty receipt. A total
        # candidate count (or trade_date_by_target) cannot prove an empty lane.
        lane_errors = {target: error or (
            None if any(s.target_board == target for s in snapshots)
            else "target_lane_missing_or_empty_unproven"
        ) for target in (1, 2)}
        raw = []
        if "limit_up_pool" in tables:
            raw = [dict(r) for r in connection.execute(
                "SELECT * FROM limit_up_pool WHERE trade_date=?", (actual_day.isoformat(),))]
        counts, exclusions, observations = Counter(), Counter(), []
        for row in raw:
            code, name, board = row["code"], row.get("name"), row.get("consecutive_days")
            reasons = []
            if row.get("quarantined") != 0:
                reasons.append("quarantined_or_unknown")
            if not tagger.is_tradeable(code):
                reasons.append("non_main_or_unknown_board")
            if any(tagger.name_risks(name)):
                reasons.append("st_or_delisting")
            if board not in (1, 2):
                reasons.append("unknown_board_count" if board is None else "outside_first_second")
            if reasons:
                exclusions.update(reasons)
                observations.append({"code": code, "name": name, "consecutive_days": board,
                                     "classification": "excluded", "evidence": reasons})
                continue
            snapshot = by_key.get((code, board))
            classification, evidence = classify(snapshot, lane_errors[board])
            counts[classification] += 1
            observations.append({
                "code": code, "name": name, "consecutive_days": board,
                "classification": classification, "evidence": evidence,
                "snapshot_id": snapshot.id if snapshot else None,
                "candidate_route": snapshot.candidate_route if snapshot else None,
                "rank_scope": snapshot.rank_scope if snapshot else None,
                "identity_status": "historical_identity_unverified",
            })
        daily.append({
            "outcome_date": actual_day.isoformat(),
            "prediction_date": previous.isoformat() if previous else None,
            "context": context, "run_id": run.id if run else None,
            "run_key": run.run_key if run else None,
            "run_as_of_at": str(run.as_of_at) if run else None,
            "batch_error": error,
            "lane_errors": {str(k): v for k, v in lane_errors.items()},
            "expected_pool_count": run.candidate_count if run else None,
            "observed_pool_count": len(snapshots),
            "observed_main_rank_count": sum(
                _loads(s.features_json).get("prediction_ranked_selected") is True for s in snapshots),
            "raw_truth_row_count": len(raw),
            "original_observed_denominator": sum(counts.values()),
            "excluded_row_count": sum(o["classification"] == "excluded" for o in observations),
            "exclusion_reason_counts": dict(exclusions), "counts": dict(counts),
            "lane_counts": {str(target): dict(Counter(
                o["classification"] for o in observations
                if o["consecutive_days"] == target and o["classification"] != "excluded"
            )) for target in (1, 2)},
            "certified_recall": None,
            "truth_status": "read_time_observation_completeness_unverified",
            "observations": observations,
        })
    totals = Counter()
    for day in daily:
        totals.update(day["counts"])
    return {
        "contract": "recall_gap_observation_v2", "as_of_at": as_of.isoformat(),
        "snapshot_context": context, "start_date": str(start_date) if start_date else None,
        "end_date": str(end_date) if end_date else None,
        "certified": False, "certified_recall": None,
        "historical_label_availability_verified": False,
        "as_of_semantics": "validation_cutoff_not_historical_first_knowledge",
        "limitations": [
            "只读原始观察，不是完整命中率、训练认证或生产晋级依据。",
            "as-of 仅为批次时钟校验截止，不证明读取时真值在历史当时已知。",
            "整条赛道无快照且无正式空池收据时标记不完整，不推断召回漏失。",
            "涨停池为读取时状态；未验证封存真值完整性或历史首次可见时点。",
            "历史证券身份未验证；名称屏蔽仅为保守观察过滤，不读取当前行业冒充历史行业。",
            "recall_miss/ranking_miss 是完整快照相对于观察涨停行的归因，不证明市场因果。",
            "不计算独立上涨方向命中率，不修改 Champion、生产排序或交易闸门。",
        ],
        "original_observed_denominator": sum(d["original_observed_denominator"] for d in daily),
        "counts": dict(totals), "daily": daily,
    }


def markdown(report, sample_limit=12):
    lines = ["# 召回缺口诊断（未认证原始观察）", "",
             f"as-of: {report['as_of_at']} / context: {report['snapshot_context']}", ""]
    lines += [f"> {s}" for s in report["limitations"]]
    lines += ["", f"原观察分母：{report['original_observed_denominator']}；完整命中率：未知", ""]
    for day in report["daily"]:
        lines += [f"## {day['outcome_date']} ← {day['prediction_date']}",
                  f"- run={day['run_id']}；batch_error={day['batch_error']}；"
                  f"完整池实存/声明={day['observed_pool_count']}/{day['expected_pool_count']}；"
                  f"主榜实存={day['observed_main_rank_count']}",
                  f"- 原涨停行={day['raw_truth_row_count']}；观察分母={day['original_observed_denominator']}；"
                  f"排除行={day['excluded_row_count']}；排除原因={day['exclusion_reason_counts']}",
                  f"- 分类={day['counts']}；赛道缺口={day['lane_errors']}；真值={day['truth_status']}"]
        for row in day["observations"][:max(0, sample_limit)]:
            lines.append(f"- {row['code']} 板数={row['consecutive_days']} "
                         f"{row['classification']}：{row['evidence']}")
        lines.append("- 完整逐股证据见同名 JSON（Markdown 样本限制不影响分母）。")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("claw.db"))
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--outdir", type=Path, default=Path("outputs"))
    parser.add_argument("--sample-limit", type=int, default=12)
    parser.add_argument("--as-of", "--as-of-at", dest="as_of", required=True, type=datetime.fromisoformat)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--context", "--snapshot-context", dest="context", default="promotion_2000",
                        choices=["promotion_1510", "promotion_2000"])
    args = parser.parse_args(argv)
    with readonly_database(args.database) as connection:
        report = analyze(connection, as_of=args.as_of, start_date=args.start_date,
                         end_date=args.end_date, days=args.days, context=args.context)
    outdir = args.outdir.expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    suffix = report["daily"][-1]["outcome_date"] if report["daily"] else args.as_of.date().isoformat()
    output = outdir / f"recall_gap_{suffix}"
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(markdown(report, args.sample_limit), encoding="utf-8")
    print(f"未认证只读观察：原分母 {report['original_observed_denominator']}；JSON/Markdown: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
