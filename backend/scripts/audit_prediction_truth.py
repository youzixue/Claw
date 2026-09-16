"""审计并安全修复晋级预测标签时钟。

默认只读预览。脚本把 ``stock_kline`` 的有效交易日视为唯一结算时钟：
- 报告周末、官方休市日或无对应 K 线日期的涨停池脏行；源数据不删除，运行时会逻辑隔离。
- 报告已结算记录中不符合 ``horizon_days`` 的 outcome_trade_date。
- 仅显式传入 ``--apply`` 时，把错误结算重置为 pending，等待现有学习任务按权威 K 线重新结算。

执行前应先备份数据库；该脚本不会猜测或直接改写涨停池源数据。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from bisect import bisect_right
from datetime import date
from pathlib import Path

# Make direct execution from ``backend/scripts`` resolve the application package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.trade_calendar import is_official_closed_day


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("claw.db"))
    parser.add_argument("--apply", action="store_true", help="重置错误结算；缺省仅审计")
    parser.add_argument("--sample-limit", type=int, default=20)
    return parser.parse_args()


def _date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在: {database}")

    connection = sqlite3.connect(str(database), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    required_tables = {"stock_kline", "limit_up_pool", "promotion_prediction_record"}
    missing = sorted(table for table in required_tables if not _table_exists(connection, table))
    if missing:
        connection.close()
        raise SystemExit(f"缺少数据表: {', '.join(missing)}")

    market_dates = sorted(
        _date(row[0])
        for row in connection.execute(
            "SELECT DISTINCT trade_date FROM stock_kline WHERE trade_date IS NOT NULL"
        ).fetchall()
        if row[0]
        and _date(row[0]).weekday() < 5
        and not is_official_closed_day(_date(row[0]))
    )
    market_date_set = set(market_dates)

    invalid_limit_rows = []
    for row in connection.execute(
        """
        SELECT trade_date, COUNT(*) AS row_count
        FROM limit_up_pool
        WHERE trade_date IS NOT NULL
        GROUP BY trade_date
        ORDER BY trade_date
        """
    ).fetchall():
        trade_day = _date(row["trade_date"])
        if (
            trade_day.weekday() >= 5
            or is_official_closed_day(trade_day)
            or trade_day not in market_date_set
        ):
            invalid_limit_rows.append((trade_day, int(row["row_count"] or 0)))

    invalid_outcomes: list[tuple[int, str, date, date, date]] = []
    settled_rows = connection.execute(
        """
        SELECT id, code, prediction_trade_date, horizon_days, outcome_trade_date
        FROM promotion_prediction_record
        WHERE outcome_status IN ('success', 'failed')
          AND outcome_trade_date IS NOT NULL
        ORDER BY id
        """
    ).fetchall()
    for row in settled_rows:
        prediction_day = _date(row["prediction_trade_date"])
        actual_day = _date(row["outcome_trade_date"])
        horizon = max(int(row["horizon_days"] or 1), 1)
        first_future_index = bisect_right(market_dates, prediction_day)
        expected_index = first_future_index + horizon - 1
        if expected_index >= len(market_dates):
            continue
        expected_day = market_dates[expected_index]
        if actual_day != expected_day:
            invalid_outcomes.append(
                (int(row["id"]), str(row["code"]), prediction_day, actual_day, expected_day)
            )

    sample_limit = max(int(args.sample_limit), 0)
    print(f"database={database}")
    print(f"authoritative_market_dates={len(market_dates)}")
    print(
        "logical_quarantine_limit_pool_dates="
        f"{len(invalid_limit_rows)} rows={sum(item[1] for item in invalid_limit_rows)}"
    )
    for trade_day, count in invalid_limit_rows[:sample_limit]:
        print(f"  limit_up_pool {trade_day}: {count} rows")
    print(f"invalid_settled_outcomes={len(invalid_outcomes)}")
    for record_id, code, prediction_day, actual_day, expected_day in invalid_outcomes[:sample_limit]:
        print(
            f"  prediction #{record_id} {code}: prediction={prediction_day} "
            f"actual={actual_day} expected={expected_day}"
        )

    if not args.apply or not invalid_outcomes:
        print("mode=dry-run" if not args.apply else "mode=apply no-op")
        connection.close()
        return 0

    ids = [item[0] for item in invalid_outcomes]
    with connection:
        connection.executemany(
            """
            UPDATE promotion_prediction_record
            SET outcome_status='pending',
                outcome_trade_date=NULL,
                actual_limit_up_date=NULL,
                actual_max_change_pct=NULL,
                actual_close_change_pct=NULL,
                failure_reason='',
                failure_tags_json='[]',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            [(record_id,) for record_id in ids],
        )
    print(f"mode=apply reset_to_pending={len(ids)}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
