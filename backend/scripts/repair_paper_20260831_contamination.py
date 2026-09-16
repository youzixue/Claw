"""定点修复 2026-08-31 模拟盘测试夹具污染，并初始化策略B账户。

这是一次性、可审计的数据修复脚本：

* 默认只读预览，不改库；
* ``--apply`` 还必须同时提供确认令牌；
* 应用前把全部受影响行写入 JSON 备份；
* 只接受已核验的 600001/600002 测试夹具，发现真实持仓或其他成交即中止；
* 晋级预测不可变 run/snapshot 台账保留，仅清除兼容表并将脏看板快照失效。

执行应用前必须先停止后端，避免调度器与修复事务并发写入。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "backend" / "claw.db"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs"
REPAIR_DATE = date(2026, 8, 31)
CONFIRM_TOKEN = "RESET-promotion-20260831"
DIRTY_CODES = ("600001", "600002")
DIRTY_NAMES = ("断板反包票", "普通涨停")
DIRTY_KLINE_IDS = tuple(range(8_843_483, 8_843_492))
DIRTY_LIMIT_IDS = (18_072, 18_073)
DIRTY_DASHBOARD_IDS = (96_063, 96_389)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true", help="应用修复；缺省仅预览")
    parser.add_argument(
        "--confirm",
        default="",
        help=f"应用时必须精确输入 {CONFIRM_TOKEN}",
    )
    return parser.parse_args()


def _rows(connection: sqlite3.Connection, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict]:
    return [dict(row) for row in connection.execute(sql, parameters).fetchall()]


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _placeholders(values: tuple[Any, ...]) -> str:
    return ",".join("?" for _ in values)


def _collect(connection: sqlite3.Connection) -> dict[str, Any]:
    account_rows = _rows(
        connection,
        "SELECT * FROM paper_account WHERE account_name=? ORDER BY id",
        ("promotion",),
    )
    if len(account_rows) != 1:
        raise RuntimeError(f"策略B账户数量异常，期望1个，实际{len(account_rows)}个")
    account = account_rows[0]
    account_id = int(account["id"])

    position_rows = _rows(
        connection,
        "SELECT * FROM paper_position WHERE account_id=? ORDER BY id",
        (account_id,),
    )
    trade_rows = _rows(
        connection,
        "SELECT * FROM paper_trade_log WHERE account_id=? ORDER BY id",
        (account_id,),
    )
    nav_rows = _rows(
        connection,
        "SELECT * FROM paper_nav WHERE account_id=? ORDER BY id",
        (account_id,),
    )
    auto_rows = _rows(
        connection,
        "SELECT * FROM paper_auto_trade_log WHERE account_id=? ORDER BY id",
        (account_id,),
    )
    order_rows = _rows(
        connection,
        "SELECT * FROM trade_order WHERE account_id=? ORDER BY id",
        ("promotion",),
    )
    order_ids = tuple(str(row["order_id"]) for row in order_rows)
    fill_rows = (
        _rows(
            connection,
            f"SELECT * FROM trade_fill WHERE order_id IN ({_placeholders(order_ids)}) ORDER BY id",
            order_ids,
        )
        if order_ids
        else []
    )

    dirty_spots = _rows(
        connection,
        f"SELECT * FROM stock_spot WHERE code IN ({_placeholders(DIRTY_CODES)}) ORDER BY code",
        DIRTY_CODES,
    )
    dirty_limits = _rows(
        connection,
        f"SELECT * FROM limit_up_pool WHERE id IN ({_placeholders(DIRTY_LIMIT_IDS)}) ORDER BY id",
        DIRTY_LIMIT_IDS,
    )
    dirty_klines = _rows(
        connection,
        f"SELECT * FROM stock_kline WHERE id IN ({_placeholders(DIRTY_KLINE_IDS)}) ORDER BY id",
        DIRTY_KLINE_IDS,
    )
    dirty_predictions = _rows(
        connection,
        """
        SELECT *
        FROM promotion_prediction_record
        WHERE prediction_trade_date=?
          AND code IN (?, ?)
          AND name IN (?, ?)
          AND snapshot_context IN ('promotion_0925', 'promotion_0935')
        ORDER BY id
        """,
        (REPAIR_DATE.isoformat(), *DIRTY_CODES, *DIRTY_NAMES),
    )
    dirty_dashboards = _rows(
        connection,
        f"SELECT * FROM dashboard_snapshot WHERE id IN ({_placeholders(DIRTY_DASHBOARD_IDS)}) ORDER BY id",
        DIRTY_DASHBOARD_IDS,
    )
    immutable_runs = _rows(
        connection,
        """
        SELECT * FROM promotion_prediction_run
        WHERE reference_trade_date=?
          AND snapshot_context IN ('promotion_0925', 'promotion_0935')
        ORDER BY id
        """,
        (REPAIR_DATE.isoformat(),),
    )
    immutable_snapshots = _rows(
        connection,
        """
        SELECT s.*
        FROM promotion_prediction_snapshot AS s
        JOIN promotion_prediction_run AS r ON r.id=s.run_id
        WHERE r.reference_trade_date=?
          AND s.code IN (?, ?)
          AND s.name IN (?, ?)
        ORDER BY s.id
        """,
        (REPAIR_DATE.isoformat(), *DIRTY_CODES, *DIRTY_NAMES),
    )

    return {
        "repair_date": REPAIR_DATE.isoformat(),
        "account": account_rows,
        "paper_positions": position_rows,
        "paper_trades": trade_rows,
        "paper_nav": nav_rows,
        "paper_auto_logs": auto_rows,
        "trade_orders": order_rows,
        "trade_fills": fill_rows,
        "dirty_stock_spot": dirty_spots,
        "dirty_limit_up_pool": dirty_limits,
        "dirty_stock_kline": dirty_klines,
        "dirty_prediction_compatibility": dirty_predictions,
        "dirty_dashboard_snapshots": dirty_dashboards,
        "preserved_immutable_runs": immutable_runs,
        "preserved_immutable_snapshots": immutable_snapshots,
    }


def _validate(snapshot: dict[str, Any]) -> None:
    account = snapshot["account"][0]
    if account["status"] != "active" or account["strategy"] != "promotion":
        raise RuntimeError(f"策略B账户状态/策略异常: {account}")

    open_positions = [row for row in snapshot["paper_positions"] if not bool(row["is_closed"])]
    if open_positions:
        raise RuntimeError("策略B存在未平仓持仓，拒绝初始化；请先人工核对")

    for key in ("paper_positions", "paper_trades", "trade_orders"):
        unexpected = sorted(
            {
                str(row.get("code") or "")
                for row in snapshot[key]
                if str(row.get("code") or "") not in DIRTY_CODES
            }
        )
        if unexpected:
            raise RuntimeError(f"{key} 含非测试代码 {unexpected}，拒绝删除")

    spots = snapshot["dirty_stock_spot"]
    if len(spots) != 2 or {row["name"] for row in spots} != set(DIRTY_NAMES):
        raise RuntimeError("600001/600002 行情不再匹配已核验测试夹具，拒绝删除")
    if any(not str(row.get("updated_at") or "").startswith(REPAIR_DATE.isoformat()) for row in spots):
        raise RuntimeError("测试行情更新时间不匹配修复日，拒绝删除")

    limits = snapshot["dirty_limit_up_pool"]
    if len(limits) != 2:
        raise RuntimeError(f"测试涨停池行数量异常: {len(limits)}")
    if any(
        row["code"] not in DIRTY_CODES
        or row["name"] not in DIRTY_NAMES
        or row["trade_date"] != REPAIR_DATE.isoformat()
        or row["limit_up_reason"] != "测试"
        or row["source"] is not None
        for row in limits
    ):
        raise RuntimeError("涨停池行不再匹配已核验测试夹具，拒绝隔离")

    klines = snapshot["dirty_stock_kline"]
    if len(klines) != len(DIRTY_KLINE_IDS):
        raise RuntimeError(f"测试K线行数量异常: {len(klines)}")
    if any(
        row["code"] not in DIRTY_CODES
        or float(row["open"]) != float(row["high"])
        or float(row["open"]) != float(row["low"])
        or float(row["open"]) != float(row["close"])
        or int(row["volume"] or 0) != 100_000
        for row in klines
    ):
        raise RuntimeError("K线行不再匹配已核验测试夹具，拒绝删除")

    dashboards = snapshot["dirty_dashboard_snapshots"]
    if len(dashboards) != len(DIRTY_DASHBOARD_IDS):
        raise RuntimeError(f"脏看板快照数量异常: {len(dashboards)}")
    for row in dashboards:
        payload = str(row.get("payload_json") or "")
        if not any(name in payload for name in DIRTY_NAMES):
            raise RuntimeError(f"看板快照 {row['id']} 未包含测试名称，拒绝失效")


def _summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    account = snapshot["account"][0]
    return {
        "account_id": account["id"],
        "capital_before": account["current_capital"],
        "rows": {
            key: len(value)
            for key, value in snapshot.items()
            if isinstance(value, list)
        },
        "immutable_ledger_policy": "preserve",
    }


def _write_backup(snapshot: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = output_dir / f"paper_repair_20260831_backup_{timestamp}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "repair": "paper_20260831_fixture_contamination",
        "data": snapshot,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return output


def _apply(connection: sqlite3.Connection, snapshot: dict[str, Any]) -> dict[str, int]:
    account_id = int(snapshot["account"][0]["id"])
    order_ids = tuple(str(row["order_id"]) for row in snapshot["trade_orders"])
    counts: dict[str, int] = {}

    connection.execute("BEGIN IMMEDIATE")
    try:
        if order_ids:
            cursor = connection.execute(
                f"DELETE FROM trade_fill WHERE order_id IN ({_placeholders(order_ids)})",
                order_ids,
            )
            counts["trade_fill_deleted"] = cursor.rowcount
        else:
            counts["trade_fill_deleted"] = 0
        counts["trade_order_deleted"] = connection.execute(
            "DELETE FROM trade_order WHERE account_id=?",
            ("promotion",),
        ).rowcount
        counts["paper_auto_log_deleted"] = connection.execute(
            "DELETE FROM paper_auto_trade_log WHERE account_id=?",
            (account_id,),
        ).rowcount
        counts["paper_trade_deleted"] = connection.execute(
            "DELETE FROM paper_trade_log WHERE account_id=?",
            (account_id,),
        ).rowcount
        counts["paper_position_deleted"] = connection.execute(
            "DELETE FROM paper_position WHERE account_id=?",
            (account_id,),
        ).rowcount
        counts["paper_nav_deleted"] = connection.execute(
            "DELETE FROM paper_nav WHERE account_id=?",
            (account_id,),
        ).rowcount
        connection.execute(
            """
            UPDATE paper_account
            SET strategy='promotion', initial_capital=50000, current_capital=50000,
                total_assets=50000, total_return=0, max_drawdown=0,
                sharpe_ratio=0, win_rate=0, status='active'
            WHERE id=? AND account_name='promotion'
            """,
            (account_id,),
        )
        connection.execute(
            "INSERT INTO paper_nav(account_id, trade_date, nav, daily_return) VALUES (?, ?, 1.0, 0.0)",
            (account_id, REPAIR_DATE.isoformat()),
        )

        counts["stock_spot_deleted"] = connection.execute(
            f"DELETE FROM stock_spot WHERE code IN ({_placeholders(DIRTY_CODES)}) AND name IN (?, ?)",
            (*DIRTY_CODES, *DIRTY_NAMES),
        ).rowcount
        counts["stock_kline_deleted"] = connection.execute(
            f"DELETE FROM stock_kline WHERE id IN ({_placeholders(DIRTY_KLINE_IDS)})",
            DIRTY_KLINE_IDS,
        ).rowcount
        counts["limit_up_quarantined"] = connection.execute(
            f"UPDATE limit_up_pool SET quarantined=1 WHERE id IN ({_placeholders(DIRTY_LIMIT_IDS)})",
            DIRTY_LIMIT_IDS,
        ).rowcount
        counts["prediction_compatibility_deleted"] = connection.execute(
            """
            DELETE FROM promotion_prediction_record
            WHERE prediction_trade_date=?
              AND code IN (?, ?)
              AND name IN (?, ?)
              AND snapshot_context IN ('promotion_0925', 'promotion_0935')
            """,
            (REPAIR_DATE.isoformat(), *DIRTY_CODES, *DIRTY_NAMES),
        ).rowcount
        counts["dashboard_snapshot_invalidated"] = connection.execute(
            f"UPDATE dashboard_snapshot SET status='invalid_dirty' WHERE id IN ({_placeholders(DIRTY_DASHBOARD_IDS)})",
            DIRTY_DASHBOARD_IDS,
        ).rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return counts


def _verify(connection: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    account = dict(
        connection.execute("SELECT * FROM paper_account WHERE id=?", (account_id,)).fetchone()
    )
    result = {
        "account": account,
        "paper_positions": connection.execute(
            "SELECT count(*) FROM paper_position WHERE account_id=?", (account_id,)
        ).fetchone()[0],
        "paper_trades": connection.execute(
            "SELECT count(*) FROM paper_trade_log WHERE account_id=?", (account_id,)
        ).fetchone()[0],
        "paper_auto_logs": connection.execute(
            "SELECT count(*) FROM paper_auto_trade_log WHERE account_id=?", (account_id,)
        ).fetchone()[0],
        "paper_nav": _rows(
            connection,
            "SELECT * FROM paper_nav WHERE account_id=? ORDER BY trade_date",
            (account_id,),
        ),
        "dirty_spots_remaining": connection.execute(
            f"SELECT count(*) FROM stock_spot WHERE code IN ({_placeholders(DIRTY_CODES)}) AND name IN (?, ?)",
            (*DIRTY_CODES, *DIRTY_NAMES),
        ).fetchone()[0],
        "dirty_klines_remaining": connection.execute(
            f"SELECT count(*) FROM stock_kline WHERE id IN ({_placeholders(DIRTY_KLINE_IDS)})",
            DIRTY_KLINE_IDS,
        ).fetchone()[0],
        "dirty_predictions_remaining": connection.execute(
            """
            SELECT count(*) FROM promotion_prediction_record
            WHERE prediction_trade_date=? AND code IN (?, ?) AND name IN (?, ?)
              AND snapshot_context IN ('promotion_0925', 'promotion_0935')
            """,
            (REPAIR_DATE.isoformat(), *DIRTY_CODES, *DIRTY_NAMES),
        ).fetchone()[0],
        "quarantined_limit_rows": connection.execute(
            f"SELECT count(*) FROM limit_up_pool WHERE id IN ({_placeholders(DIRTY_LIMIT_IDS)}) AND quarantined=1",
            DIRTY_LIMIT_IDS,
        ).fetchone()[0],
        "invalid_dashboard_rows": connection.execute(
            f"SELECT count(*) FROM dashboard_snapshot WHERE id IN ({_placeholders(DIRTY_DASHBOARD_IDS)}) AND status='invalid_dirty'",
            DIRTY_DASHBOARD_IDS,
        ).fetchone()[0],
        "immutable_runs_preserved": connection.execute(
            "SELECT count(*) FROM promotion_prediction_run WHERE reference_trade_date=?",
            (REPAIR_DATE.isoformat(),),
        ).fetchone()[0],
    }
    clean_account = (
        float(account["initial_capital"] or 0) == 50_000
        and float(account["current_capital"] or 0) == 50_000
        and float(account["total_assets"] or 0) == 50_000
        and float(account["total_return"] or 0) == 0
        and result["paper_positions"] == 0
        and result["paper_trades"] == 0
        and result["paper_auto_logs"] == 0
        and len(result["paper_nav"]) == 1
        and float(result["paper_nav"][0]["nav"]) == 1.0
    )
    result["verified"] = bool(
        clean_account
        and result["dirty_spots_remaining"] == 0
        and result["dirty_klines_remaining"] == 0
        and result["dirty_predictions_remaining"] == 0
        and result["quarantined_limit_rows"] == len(DIRTY_LIMIT_IDS)
        and result["invalid_dashboard_rows"] == len(DIRTY_DASHBOARD_IDS)
    )
    return result


def main() -> int:
    args = parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file():
        raise SystemExit(f"数据库不存在: {database}")
    if args.apply and args.confirm != CONFIRM_TOKEN:
        raise SystemExit(f"确认令牌不匹配；需要 --confirm {CONFIRM_TOKEN}")

    connection = sqlite3.connect(str(database), timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=60000")
    required_tables = {
        "paper_account",
        "paper_position",
        "paper_trade_log",
        "paper_auto_trade_log",
        "paper_nav",
        "trade_order",
        "trade_fill",
        "stock_spot",
        "stock_kline",
        "limit_up_pool",
        "promotion_prediction_record",
        "promotion_prediction_run",
        "promotion_prediction_snapshot",
        "dashboard_snapshot",
    }
    missing = sorted(required_tables - _tables(connection))
    if missing:
        connection.close()
        raise SystemExit(f"缺少数据表: {', '.join(missing)}")

    try:
        snapshot = _collect(connection)
        _validate(snapshot)
        print(json.dumps({"database": str(database), "mode": "apply" if args.apply else "dry-run", **_summary(snapshot)}, ensure_ascii=False, indent=2))
        if not args.apply:
            print(f"dry-run通过；应用前停止后端，然后追加 --apply --confirm {CONFIRM_TOKEN}")
            return 0

        backup = _write_backup(snapshot, args.output_dir.expanduser().resolve())
        counts = _apply(connection, snapshot)
        verification = _verify(connection, int(snapshot["account"][0]["id"]))
        print(json.dumps({"backup": str(backup), "changes": counts, "verification": verification}, ensure_ascii=False, indent=2, default=str))
        if not verification["verified"]:
            raise RuntimeError("事务已提交，但修复后校验未通过；请根据备份人工复核")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
