"""安全隔离涨停池中交易日不在权威K线日历的脏记录。

默认只读预览。脚本把 ``stock_kline`` 的有效交易日视为唯一结算时钟：
- 报告周末、官方休市日或无对应K线日期的涨停池行；
- 仅显式传入 ``--apply`` 时把这些行标记 ``quarantined=true``（软隔离，
  不物理删除；质量审计与预测加载会跳过隔离行）。

执行前应先备份数据库；该脚本不会猜测或删除源数据。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.trade_calendar import is_official_closed_day


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("claw.db"))
    parser.add_argument("--apply", action="store_true", help="标记隔离；缺省仅预览")
    parser.add_argument("--sample-limit", type=int, default=20)
    return parser.parse_args()


def _date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在: {database}")

    connection = sqlite3.connect(str(database), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    required_tables = {"stock_kline", "limit_up_pool"}
    missing = sorted(table for table in required_tables if table not in {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    })
    if missing:
        connection.close()
        raise SystemExit(f"缺少数据表: {', '.join(missing)}")

    if not _has_column(connection, "limit_up_pool", "quarantined"):
        connection.close()
        raise SystemExit("limit_up_pool 缺少 quarantined 列，请先执行 alembic upgrade head")

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

    invalid_rows = []
    for row in connection.execute(
        """
        SELECT id, code, trade_date, source
        FROM limit_up_pool
        WHERE quarantined = 0
        ORDER BY trade_date, id
        """
    ).fetchall():
        trade_day = _date(row["trade_date"])
        if (
            trade_day.weekday() >= 5
            or is_official_closed_day(trade_day)
            or trade_day not in market_date_set
        ):
            invalid_rows.append(row)

    print(f"database={database}")
    print(f"authoritative_market_dates={len(market_dates)}")
    print(f"quarantine_candidates={len(invalid_rows)}")
    for row in invalid_rows[: max(args.sample_limit, 0)]:
        print(f"  row id={row['id']} code={row['code']} trade_date={row['trade_date']} source={row['source']}")

    if not args.apply or not invalid_rows:
        print("mode=dry-run" if not args.apply else "mode=apply no-op")
        connection.close()
        return 0

    ids = [int(row["id"]) for row in invalid_rows]
    with connection:
        connection.executemany(
            "UPDATE limit_up_pool SET quarantined=1 WHERE id=?",
            [(record_id,) for record_id in ids],
        )
    print(f"mode=apply quarantined={len(ids)}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
