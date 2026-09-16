"""Read-only deployment evidence digests. Does not initialize app/settings or databases."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import platform
import sys

try:  # Both direct CLI and import via the existing scripts namespace.
    from scripts.deployment_evidence_contract import AUDIT_PROTOCOL, DIGEST_PROTOCOL
except ModuleNotFoundError:
    from deployment_evidence_contract import AUDIT_PROTOCOL, DIGEST_PROTOCOL

CORE_TABLES = (
    "stock_kline", "stock_kline_observation", "stock_daily", "stock_spot", "fund_flow", "finance_news",
    "news_content_version", "news_analysis_version", "promotion_prediction_record",
    "promotion_prediction_run", "promotion_prediction_snapshot", "promotion_shadow_prediction",
    "paper_account", "paper_position", "paper_trade_log", "paper_nav", "paper_auto_trade_log",
    "paper_control_sample", "paper_daily_outcome", "paper_shadow_evaluation", "paper_shadow_event",
    "trade_order", "trade_fill", "limit_up_pool", "broken_limit_pool", "market_sentiment",
    "factor_values", "factor_evaluation_run", "factor_computation_run",
    "signal_performance", "paper_sale_accounting",
    "anomaly_candidate_record", "anomaly_candidate_evidence",
)


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def audit_database(database: Path) -> dict:
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("deployment digest protocol requires Python 3.11")
    path = database.resolve(strict=True)
    if not path.is_file():
        raise ValueError("database must be an existing file")
    # URI ro prevents creation, PRAGMA query_only also forbids accidental writes.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        connection.execute("BEGIN")  # one consistent read snapshot
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        result = {
            "database": str(path), "observed_at": datetime.now().isoformat(),
            "audit_protocol": AUDIT_PROTOCOL, "digest_protocol": DIGEST_PROTOCOL,
            "python_version": platform.python_version(), "core_tables": list(CORE_TABLES),
            "readonly": True, "counts": {}, "core_digests": {},
            "revision": [], "triggers": [], "trigger_definitions": {}, "table_schemas": {},
        }
        for table in tables:
            result["counts"][table] = connection.execute(
                f"SELECT COUNT(*) FROM {identifier(table)}").fetchone()[0]
            indexes = {}
            for row in connection.execute(f"PRAGMA index_list({identifier(table)})"):
                indexes[row[1]] = {
                    "unique": bool(row[2]), "origin": row[3], "partial": bool(row[4]),
                    "columns": [item[2] for item in connection.execute(
                        f"PRAGMA index_info({identifier(row[1])})")],
                    "sql": connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (row[1],)).fetchone()[0],
                }
            result["table_schemas"][table] = {
                "sql": connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0],
                "columns": [list(row) for row in connection.execute(f"PRAGMA table_info({identifier(table)})")],
                "foreign_keys": [list(row) for row in connection.execute(f"PRAGMA foreign_key_list({identifier(table)})")],
                "indexes": indexes,
            }
        for table in CORE_TABLES:
            if table not in tables:
                continue
            columns = connection.execute(f"PRAGMA table_info({identifier(table)})").fetchall()
            pk = [row[1] for row in sorted(columns, key=lambda item: item[5]) if row[5]]
            order = ", ".join(identifier(name) for name in pk) if pk else "rowid"
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute(f"SELECT * FROM {identifier(table)} ORDER BY {order}"):
                digest.update(repr(tuple(row)).encode("utf-8"))
                digest.update(b"\n")
                count += 1
            result["core_digests"][table] = {
                "count": count, "columns": [row[1] for row in columns], "sha256": digest.hexdigest(),
            }
        if "alembic_version" in tables:
            result["revision"] = [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]
        result["trigger_definitions"] = {row[0]: {"table": row[1], "sql": row[2]}
            for row in connection.execute("SELECT name,tbl_name,sql FROM sqlite_master "
                                          "WHERE type='trigger' ORDER BY name")}
        result["triggers"] = list(result["trigger_definitions"])
        connection.rollback()
        return result
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    database, output = args.database.resolve(strict=True), args.output.resolve()
    if output in {database, Path(str(database)+"-wal"), Path(str(database)+"-shm")}:
        parser.error("output cannot replace database evidence")
    result = audit_database(database)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"read-only audit: {len(result['counts'])} tables; {len(result['core_digests'])} content digests; output={output}")


if __name__ == "__main__":
    main()
