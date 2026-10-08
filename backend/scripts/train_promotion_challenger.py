"""训练并走步验证晋级预测挑战者；默认只读，不会替换生产冠军。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import async_session
from app.promotion.modeling.training import train_promotion_challenger


def _date(value: str) -> date:
    return date.fromisoformat(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-board", type=int, choices=(1, 2), default=1)
    parser.add_argument("--objective", choices=("promotion", "next_day_close_up"), default="promotion")
    parser.add_argument("--as-of", type=datetime.fromisoformat,
                        help="方向研究必填：Asia/Shanghai本地无时区时间；拒绝未来数据")
    parser.add_argument(
        "--dataset-source",
        choices=("historical_panel", "prediction_snapshots"),
        default="historical_panel",
    )
    parser.add_argument("--start-date", type=_date)
    parser.add_argument("--end-date", type=_date)
    parser.add_argument("--snapshot-context", default="promotion_2000")
    parser.add_argument("--historical-lookback-days", type=int, default=120)
    parser.add_argument("--historical-candidate-limit", type=int, default=450)
    parser.add_argument("--initial-train-days", type=int, default=60)
    parser.add_argument("--validation-days", type=int, default=10)
    parser.add_argument("--step-days", type=int, default=10)
    parser.add_argument("--calibration-days", type=int, default=10)
    parser.add_argument("--train-window-days", type=int, default=None,
                        help="滚动训练交易日数（包含校准尾窗）；不传则保持扩展训练，不改变生产模型")
    parser.add_argument("--persist", action="store_true", help="保存训练记录和模型产物；仍不激活生产")
    parser.add_argument("--output", type=Path, help="额外写出完整 JSON 报告")
    args = parser.parse_args()
    if args.objective == "next_day_close_up":
        if args.dataset_source != "prediction_snapshots" or args.target_board != 1 or args.persist:
            parser.error("方向研究仅支持主板首板冻结候选，要求 --dataset-source prediction_snapshots，禁止 --persist")
        from app.promotion.modeling.direction import validate_direction_as_of
        try:
            validate_direction_as_of(args.as_of)
        except ValueError as exc:
            parser.error(str(exc))
    return args


async def _run(args: argparse.Namespace) -> dict:
    if args.objective == "next_day_close_up":
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from app.db.session import engine as configured_engine
        from app.promotion.modeling.direction import research_direction_challenger
        if configured_engine.url.get_backend_name() != "sqlite":
            raise ValueError("direction CLI currently requires a physically read-only SQLite database")
        database = Path(configured_engine.url.database or "").expanduser().resolve()
        if not database.is_file():
            raise ValueError("direction research database does not exist")
        engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_uri()}?mode=ro&uri=true",
                                     connect_args={"timeout": 30})
        try:
            async with engine.connect() as connection:
                await connection.execute(text("PRAGMA query_only=ON"))
                await connection.execute(text("BEGIN"))
                async with AsyncSession(bind=connection, autoflush=False) as session:
                    return await research_direction_challenger(session, as_of_at=args.as_of,
                        start_date=args.start_date, end_date=args.end_date,
                        snapshot_context=args.snapshot_context,
                        initial_train_days=args.initial_train_days, validation_days=args.validation_days,
                        step_days=args.step_days, calibration_days=args.calibration_days,
                        train_window_days=getattr(args, "train_window_days", None))
        finally:
            await engine.dispose()
    async with async_session() as session:
        return await train_promotion_challenger(
            session,
            target_board=args.target_board,
            dataset_source=args.dataset_source,
            start_date=args.start_date,
            end_date=args.end_date,
            snapshot_context=args.snapshot_context,
            historical_lookback_days=args.historical_lookback_days,
            historical_candidate_limit=args.historical_candidate_limit,
            initial_train_days=args.initial_train_days,
            validation_days=args.validation_days,
            step_days=args.step_days,
            calibration_days=args.calibration_days,
            train_window_days=getattr(args, "train_window_days", None),
            persist=args.persist,
        )


def main() -> int:
    args = parse_args()
    result = asyncio.run(_run(args))
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
        print(f"report={output}")
    evaluation = result.get("walk_forward") or {}
    challenger = evaluation.get("challenger_metrics", {})
    champion = evaluation.get("champion_metrics") or evaluation.get("direction_reference_metrics", {})
    if evaluation.get("research_only"):
        print("metrics_scope=observed_outcomes_only_not_complete_cohort")
        print(f"unknown_outcome_count={challenger.get('unknown_count')}")
        challenger = challenger.get("observed_metrics", {})
        champion = champion.get("observed_metrics", {})
    if args.objective == "next_day_close_up":
        print(f"direction_research_status={result.get('status')}")
        print(f"target_met={result.get('target_met')}")
    print(f"model_version={result.get('model_version')}")
    print(f"data_version={result.get('data_version')}")
    print(f"persisted={result.get('persisted')}")
    print(f"acceptance={result.get('acceptance', {}).get('decision')}")
    print(
        "average_precision="
        f"challenger:{challenger.get('average_precision')} "
        f"champion:{champion.get('average_precision')}"
    )
    print(
        "brier_score="
        f"challenger:{challenger.get('brier_score')} "
        f"champion:{champion.get('brier_score')}"
    )
    print("production_unchanged=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
