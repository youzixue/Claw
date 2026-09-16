"""Read-only release audit contracts. No app/settings import, DB or business calls."""
import re

AUDIT_PROTOCOL = "claw_deployment_evidence_v2"
DIGEST_PROTOCOL = "sqlite_pk_ordered_python311_tuple_repr_sha256_v1"
REVISIONS = (
    "027_fund_order_breakdown", "028_factor_evaluation_runs", "029_news_evidence_versions",
    "030_kline_observations", "031_paper_sale_accounting",
    "032_anomaly_candidate_evidence", "033_factor_computation_runs",
)
TABLE_SINCE = {
    "factor_evaluation_run": 28,
    "news_content_version": 29, "news_analysis_version": 29,
    "stock_kline_observation": 30, "paper_sale_accounting": 31,
    "anomaly_candidate_evidence": 32, "factor_computation_run": 33,
}
GUARD_MESSAGES = {
    "news_content_version": "news evidence is append-only",
    "news_analysis_version": "news evidence is append-only",
    "stock_kline_observation": "K-line evidence is append-only",
    "paper_sale_accounting": "sale accounting evidence is append-only",
    "anomaly_candidate_evidence": "anomaly candidate evidence is append-only",
    "factor_computation_run": "factor computation evidence is append-only",
}
REPLACE_CONDITIONS = {
    "paper_sale_accounting": "trade_id=NEW.trade_id OR id=NEW.id",
    "anomaly_candidate_evidence": "id=NEW.id OR (capture_id=NEW.capture_id AND record_id=NEW.record_id)",
    "factor_computation_run": "id=NEW.id OR capture_id=NEW.capture_id",
}


def normalized_sql(sql):
    # Conservative exact contract, not a SQL semantic equivalence engine. It
    # accepts whitespace/case/optional IF NOT EXISTS, not WHEN 0 or UPDATE OF.
    sql = re.sub(r"\s+", " ", sql.strip().rstrip(";")).lower()
    return re.sub(r"^create trigger if not exists ", "create trigger ", sql)


def expected_guards(revision):
    target = int(revision[:3])
    result = {}
    for table, message in GUARD_MESSAGES.items():
        if TABLE_SINCE[table] > target:
            continue
        for action in ("UPDATE", "DELETE"):
            name = f"{table}_no_{action.lower()}"
            result[name] = {
                "table": table,
                "sql": f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
                       f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            }
        if table in REPLACE_CONDITIONS:
            name = f"{table}_no_replace"
            result[name] = {
                "table": table,
                "sql": f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN EXISTS "
                       f"(SELECT 1 FROM {table} WHERE {REPLACE_CONDITIONS[table]}) "
                       f"BEGIN SELECT RAISE(ABORT, '{message}'); END",
            }
    return result


# Exact prospective schemas from 031–033. Unexpected pre-existing structures
# are rejected for operator review, never automatically rebuilt/relabelled.
EVIDENCE_SCHEMAS = {
    "paper_sale_accounting": {
        "columns": {"id": "INTEGER", "trade_id": "INTEGER", "account_id": "INTEGER",
                    "code": "VARCHAR(10)", "version": "VARCHAR(40)", "recorded_at": "DATETIME",
                    "payload_json": "TEXT"},
        "unique": ("trade_id",),
        "indexes": {"ix_paper_sale_accounting_account_code": ("account_id", "code")},
        "foreign_keys": [("paper_trade_log", "trade_id", "id", "NO ACTION", "NO ACTION", "NONE")],
    },
    "anomaly_candidate_evidence": {
        "columns": {"id": "INTEGER", "capture_id": "VARCHAR(40)", "record_id": "VARCHAR(30)",
                    "trade_date": "DATE", "code": "VARCHAR(10)", "captured_at": "DATETIME",
                    "protocol_version": "VARCHAR(40)", "payload_hash": "VARCHAR(64)", "payload_json": "TEXT"},
        "unique": ("capture_id", "record_id"),
        "indexes": {"ix_anomaly_evidence_record_time": ("record_id", "captured_at"),
                    "ix_anomaly_evidence_day_code": ("trade_date", "code")},
        "foreign_keys": [],
    },
    "factor_computation_run": {
        "columns": {"id": "INTEGER", "capture_id": "VARCHAR(40)", "trade_date": "DATE",
                    "read_started_at": "DATETIME", "captured_at": "DATETIME",
                    "protocol_version": "VARCHAR(40)", "payload_hash": "VARCHAR(64)", "payload_json": "TEXT"},
        "unique": ("capture_id",),
        "indexes": {"ix_factor_computation_day_time": ("trade_date", "captured_at")},
        "foreign_keys": [],
    },
}


def schema_issues(table, schema):
    expected = EVIDENCE_SCHEMAS.get(table)
    if expected is None:
        return []
    issues = []
    columns = schema["columns"]
    actual = {row[1]: row for row in columns}
    if (set(actual) != set(expected["columns"])
            or any(str(actual[key][2]).upper() != kind for key, kind in expected["columns"].items())
            or any(actual[key][3] != 1 for key in expected["columns"] if key != "id")
            or [(row[1], row[5]) for row in columns if row[5]] != [("id", 1)]):
        issues.append(f"evidence column contract mismatch: {table}")
    indexes = schema["indexes"]
    unique = [(tuple(row["columns"]), row["partial"], row["origin"])
              for row in indexes.values() if row["unique"]]
    if unique != [(expected["unique"], False, "u")]:
        issues.append(f"evidence unique contract mismatch: {table}")
    for name, fields in expected["indexes"].items():
        row = indexes.get(name)
        if (row is None or tuple(row["columns"]) != fields
                or row["unique"] or row["partial"]):
            issues.append(f"evidence index contract mismatch: {table}.{name}")
    # SQLite pragma FK rows: id,seq,table,from,to,on_update,on_delete,match.
    foreign = [tuple(row[2:]) for row in schema["foreign_keys"]]
    if foreign != expected["foreign_keys"]:
        issues.append(f"evidence foreign key contract mismatch: {table}")
    return issues
