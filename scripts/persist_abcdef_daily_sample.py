"""Persist one immutable ABCDEF daily outcome sample and its artifact manifest.

This command is deliberately post-event and descriptive. It builds the normal
DailyReviewSnapshot (whose payload contains strategy_iteration_sample) and adds
one idempotent analyst note pointing to the detailed research artifacts. It
never updates strategy weights, thresholds or orders.

Usage:
    python3 scripts/persist_abcdef_daily_sample.py --date 2026-09-01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.session import async_session
from app.models.review import DailyReviewNote
from app.review.service import build_daily_review_snapshot


NOTE_PREFIX = "[ABCDEF_DAILY_SAMPLE_V4]"


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def persist_sample(
    *,
    trade_date: date,
    manifest_path: Path,
    report_path: Path,
) -> dict:
    manifest = _load_manifest(manifest_path)
    async with async_session() as session:
        snapshot = await build_daily_review_snapshot(
            session,
            review_date=trade_date,
            phase="postmarket",
            persist=True,
        )
        sample = (
            snapshot.get("dimensions", {})
            .get("limit_up_learning", {})
            .get("strategy_iteration_sample", {})
        )
        note_marker = (
            f"{NOTE_PREFIX} {trade_date.isoformat()} "
            f"review_key={snapshot.get('review_key')}"
        )
        existing_note = await session.scalar(
            select(DailyReviewNote)
            .where(
                DailyReviewNote.review_date == trade_date,
                DailyReviewNote.phase == "postmarket",
                DailyReviewNote.content.like(f"{note_marker}%"),
            )
            .order_by(DailyReviewNote.id)
            .limit(1)
        )
        content = (
            f"{note_marker} | snapshot_id={snapshot.get('id')} "
            f"| review_key={snapshot.get('review_key')} "
            f"| limit_up={manifest.get('pool_count', sample.get('coverage', {}).get('limit_up_count'))} "
            f"| first_board={manifest.get('firstboard_count')} "
            f"| promotion={manifest.get('promotion_count')} "
            f"| minute_rows={manifest.get('minute_row_count')} "
            f"| kline_codes={manifest.get('kline_success_count')} "
            f"| indicative_auction_full={manifest.get('eastmoney_full_auction_count')} "
            f"| manifest={manifest_path.as_posix()} "
            f"| report={report_path.as_posix()} "
            "| causal_status=descriptive_not_causal "
            "| eligible_for_threshold_tuning=false "
            "| decision=A状态恢复修复；E保留严格成交；B/C/D Champion暂停自动买入并保留候选审计；F1停用；B/C/D/F2新增形态进入前向影子与隔离paper子账户，永不连接真实券商"
        )
        if existing_note is None:
            session.add(
                DailyReviewNote(
                    review_date=trade_date,
                    phase="postmarket",
                    category="observation",
                    content=content,
                    tags_json=json.dumps(
                        [
                            "ABCDEF",
                            "每日迭代样本",
                            "涨停全样本",
                            "前向影子",
                            "阈值冻结",
                        ],
                        ensure_ascii=False,
                    ),
                    author="strategy_iteration",
                )
            )
        await session.commit()
        return {
            "trade_date": trade_date.isoformat(),
            "snapshot_id": snapshot.get("id"),
            "review_key": snapshot.get("review_key"),
            "sample_version": sample.get("sample_version"),
            "sample_limit_up_count": sample.get("coverage", {}).get("limit_up_count"),
            "manifest_pool_count": manifest.get("pool_count"),
            "note_created": existing_note is None,
            "manifest_path": manifest_path.as_posix(),
            "report_path": report_path.as_posix(),
        }


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--report", default="")
    return parser.parse_args()


async def main() -> None:
    args = _args()
    trade_date = date.fromisoformat(args.date)
    output_dir = ROOT / "outputs" / f"daily_review_{trade_date:%Y%m%d}"
    manifest = (
        Path(args.manifest)
        if args.manifest
        else output_dir / f"涨停全样本数据清单_{trade_date:%Y%m%d}.json"
    )
    report = (
        Path(args.report)
        if args.report
        else output_dir / f"ABCDEF策略深度评审_{trade_date:%Y%m%d}.md"
    )
    result = await persist_sample(
        trade_date=trade_date,
        manifest_path=manifest,
        report_path=report,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
