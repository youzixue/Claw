"""Read-only OFFLINE SQLite schema probe; never a deployment/execution certificate.

Explicit existing snapshot only. No settings/engine/service imports, migrations,
account reads, fetches, repairs, backup creation or runtime toggles. Refuse WAL/
journal sidecars; immutable SQLite prevents auxiliary file creation. A caller
must obtain a consistent offline snapshot separately. Schema != loaded code,
valid historical receipts, supplier capability or natural trading-day acceptance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
# These two modules contain pure DDL only, with empty parent-package initializers.
from app.data.after_hours_schema import SQLITE_INSERT_GUARD
from app.trading.paper_after_hours_resource_schema import sqlite_guards

VERSION = "after_hours_offline_schema_probe_v1_20261002"
EXPECTED_HEAD = "040_after_hours_partial_receipts"
MAX_FIELD_BYTES = 64 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_COLUMNS, MAX_INDEXES, MAX_STEPS, MAX_SECONDS = 128, 64, 200000, 3

OBSERVATION = "stock_after_hours_observation"
SCOPE = "paper_after_hours_resource_scope"
RECEIPT = "paper_after_hours_resource_receipt"
# Declared schema, not a claim about row values or historical PIT availability.
NEW_COLUMNS = {
    OBSERVATION: {
        "id":"INTEGER", "code":"VARCHAR(10)", "trade_date":"DATE", "stage":"VARCHAR(24)",
        "source":"VARCHAR(32)", "source_version":"VARCHAR(64)", "content_hash":"VARCHAR(64)",
        "payload_json":"TEXT", "source_quote_at":"DATETIME", "received_at":"DATETIME",
        "recorded_at":"DATETIME", "available_at":"DATETIME", "quality_status":"VARCHAR(24)",
        "protocol_version":"VARCHAR(48)"},
    SCOPE: {
        "scope_key":"VARCHAR(64)", "account_numeric_id":"INTEGER", "account_name":"VARCHAR(40)",
        "code":"VARCHAR(10)", "trade_date":"DATE", "source":"VARCHAR(64)", "source_version":"VARCHAR(64)",
        "session_id":"VARCHAR(160)", "revision":"INTEGER", "terminal_sequence":"INTEGER",
        "lifecycle_prefix_hash":"VARCHAR(64)", "source_quote_at":"DATETIME",
        "source_available_at":"DATETIME", "checked_at":"DATETIME"},
    RECEIPT: {
        "id":"INTEGER", "allocation_id":"VARCHAR(64)", "scope_key":"VARCHAR(64)",
        "trade_fill_id":"INTEGER", "paper_trade_id":"INTEGER", "order_id":"VARCHAR(40)",
        "protocol_version":"VARCHAR(64)", "payload_json":"TEXT", "content_hash":"VARCHAR(64)",
        "recorded_at":"DATETIME"},
}
TABLE_CHECKS = {
    SCOPE: {"revision >= 0 AND terminal_sequence >= 0"},
    RECEIPT: {"length(payload_json) <= 2097152"},
}
BOOK_COLUMNS = {
    "trade_order": set("id order_id idempotency_key broker account_id code side order_type price quantity "
        "filled_quantity strategy_version signal_id decision_round_id decision_at trade_date created_at "
        "risk_json status avg_fill_price last_fill_round_id updated_at external_order_id".split()),
    "trade_fill": set("id fill_id order_id broker external_order_id code side price quantity commission tax "
        "realized_pnl broker_trade_id raw_json decision_round_id fill_round_id trade_date filled_at".split()),
    "paper_trade_log": set("id account_id code trade_type price amount trade_time commission tax signal_id "
        "realized_pnl strategy_version decision_round_id fill_round_id".split()),
    "paper_account": {"id", "account_name", "status"},
}
UNIQUE_KEYS = {
    OBSERVATION: {("id",), ("code","trade_date","stage","source","source_version","content_hash")},
    SCOPE: {("scope_key",), ("account_numeric_id","code","trade_date")},
    RECEIPT: {("id",), ("allocation_id",), ("trade_fill_id",), ("paper_trade_id",)},
    "trade_order": {("id",), ("order_id",), ("idempotency_key",)},
    "trade_fill": {("id",), ("fill_id",)}, "paper_trade_log": {("id",)}, "paper_account": {("id",)},
}


class ProbeBlocked(ValueError):
    pass


def canonical_sql(sql):
    # Preserve quoted literals/identifiers exactly. Lowercasing the entire DDL
    # could conceal a guard whose case-sensitive protocol literal was changed.
    pieces = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
    for i in range(0, len(pieces), 2):
        pieces[i] = re.sub(r"\s+", " ",
            re.sub(r"\bIF\s+NOT\s+EXISTS\s+", "", pieces[i], flags=re.I)).lower()
    return "".join(pieces).strip().rstrip(";")


def expected_guards():
    result = {s.split("IF NOT EXISTS ", 1)[1].split()[0]: s for s in sqlite_guards()}
    for action in ("UPDATE", "DELETE"):
        result["after_hours_no_"+action.lower()] = (
            f"CREATE TRIGGER after_hours_no_{action.lower()} BEFORE {action} ON {OBSERVATION} "
            "BEGIN SELECT RAISE(ABORT, 'after-hours evidence is append-only'); END")
    result["after_hours_no_replace"] = SQLITE_INSERT_GUARD
    return result


def _stamp(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def _sidecars(path):
    return any(Path(str(path)+suffix).exists() for suffix in ("-wal", "-shm", "-journal"))


class _Budget:
    def __init__(self):
        self.used = 0
        self.deadline = time.monotonic()+MAX_SECONDS

    def rows(self, db, source, params, fields, maximum, error):
        """Trusted internal SQL only. Gate WHOLE batch UTF8 before hydration.

        fields=(SQL expression, text byte cap or None for numeric metadata).
        Window cost includes every row, including rows beyond LIMIT. A failing
        batch returns NULL text, never an uncharged prefetched suffix. Numeric
        counters, SQL scan pages and query strings are outside the byte budget.
        """
        if time.monotonic() > self.deadline:
            raise ProbeBlocked("metadata_time_budget")
        selected = ", ".join(f"{expr} AS c{i}" for i,(expr,_) in enumerate(fields))
        sizes = [f"coalesce(length(CAST(c{i} AS BLOB)),0)"
                 for i,(_,cap) in enumerate(fields) if cap is not None]
        cost = "+".join(sizes) or "0"
        projections = []
        for i,(_,cap) in enumerate(fields):
            if cap is None:
                projections.append(f"c{i}")
            else:
                projections.append(f"CASE WHEN _rows<=? AND _bytes<=? "
                    f"AND length(CAST(c{i} AS BLOB))<=? THEN c{i} END")
        gates = [v for _,cap in fields if cap is not None
                 for v in (maximum, MAX_METADATA_BYTES-self.used, cap)]
        query = f"""WITH _af_probe_metadata AS (SELECT {selected} FROM {source}),
            _af_probe_measured AS (SELECT *,count(*) OVER() AS _rows,
                         sum({cost}) OVER() AS _bytes FROM _af_probe_metadata)
            SELECT {", ".join(projections)},_rows,_bytes
            FROM _af_probe_measured LIMIT ?"""
        fetched = db.execute(query, (*params,*gates,maximum+1)).fetchall()
        # Charge ALL text actually returned, before semantic validation.
        self.used += sum(len(value.encode()) for row in fetched
            for (value,(_,cap)) in zip(row,fields)
            if cap is not None and isinstance(value,str))
        if self.used > MAX_METADATA_BYTES:
            raise ProbeBlocked("metadata_byte_budget")
        if (len(fetched)>maximum or any(row[-2]>maximum for row in fetched)
                or any(cap is not None and (not isinstance(row[i],str)
                    or "\x00" in row[i] or len(row[i].encode())>cap)
                    for row in fetched for i,(_,cap) in enumerate(fields))):
            raise ProbeBlocked(error)
        if time.monotonic() > self.deadline:
            raise ProbeBlocked("metadata_time_budget")
        return [row[:-2] for row in fetched]

    def schema(self, db, kind, name):
        row = self.rows(db, "sqlite_schema WHERE type=? AND name=?", (kind,name),
            [("sql",MAX_FIELD_BYTES)], 1, "schema_metadata_missing_or_byte_budget")
        return row[0][0] if row else None


def _table(db, budget, table):
    # Never hydrate default expressions or arbitrary unrelated TEXT.
    rows = budget.rows(db, "pragma_table_xinfo(?)", (table,),
        [("name",160),("type",64),('"notnull"',None),("pk",None),("hidden",None)],
        MAX_COLUMNS, "column_metadata_row_budget_or_missing")
    if not rows:
        raise ProbeBlocked("column_metadata_row_budget_or_missing")
    columns, primary = {}, []
    for name, declared, required, pk, hidden in rows:
        if hidden or name in columns:
            raise ProbeBlocked("unsupported_hidden_or_duplicate_column")
        columns[name] = (declared.upper().replace(" ", ""), required)
        if pk:
            primary.append((pk, name))
    primary = tuple(name for _,name in sorted(primary))
    expected_primary = ("scope_key",) if table==SCOPE else ("id",)
    if primary!=expected_primary:
        raise ProbeBlocked("unexpected_primary_key")
    unique = {primary}
    indexes = budget.rows(db, "pragma_index_list(?)", (table,),
        [("name",160),('"unique"',None),("partial",None)],
        MAX_INDEXES, "index_metadata_row_budget")
    for name, is_unique, partial in indexes:
        if not is_unique:
            continue
        leaves = budget.rows(db, "pragma_index_xinfo(?) WHERE key=1 ORDER BY seqno", (name,),
            [("seqno",None),("name",160),("coll",64)], MAX_COLUMNS, "index_leaf_budget")
        if not leaves:
            raise ProbeBlocked("index_leaf_budget")
        if partial:
            # An extra partial unique can prohibit the second fragment while
            # leaving the expected full-key set unchanged. Do not certify it.
            raise ProbeBlocked("unexpected_partial_unique_index")
        # Same column names do not imply the same uniqueness relation: an
        # extra NOCASE/RTRIM index cannot be folded into the BINARY model key.
        if any(collation!="BINARY" for _,_,collation in leaves):
            raise ProbeBlocked("unexpected_unique_key_collation")
        unique.add(tuple(leaf for _,leaf,_ in leaves))
    return columns, unique


def _check_expressions(ddl):
    """Extract unquoted CHECK expressions, not comments/string bait.

    Conservative small lexer, not a general SQL equivalence prover. Unsupported
    syntax is blocked rather than inferred safe. Quoted identifiers retain tags,
    so a constraint merely named "check" cannot stand for a CHECK keyword.
    """
    pattern = re.compile(r"""\s+|--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|"(?:\"\"|[^"])*"|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|\d+|<=|>=|<>|!=|[(),.=<>+*/;\-]""")
    tokens, end = [], 0
    for match in pattern.finditer(ddl):
        if match.start()!=end:
            raise ProbeBlocked("unsupported_table_ddl_syntax")
        end = match.end()
        token = match.group()
        if token.isspace() or token.startswith(("--","/*")):
            continue
        tokens.append(token if token[0] in "'\"[" else token.lower())
    if end!=len(ddl):
        raise ProbeBlocked("unsupported_table_ddl_syntax")
    checks = set()
    for i,token in enumerate(tokens):
        if token!="check" or i+1==len(tokens) or tokens[i+1]!="(":
            continue
        start, depth, j = i+2, 1, i+2
        while j<len(tokens) and depth:
            depth += (tokens[j]=="(")-(tokens[j]==")")
            j += 1
        if depth:
            raise ProbeBlocked("invalid_table_check")
        checks.add(tuple(tokens[start:j-1]))
    return checks


def inspect_snapshot(database):
    answer = {"protocol_version":VERSION, "status":"blocked", "read_only":True,
        "target_kind":"explicit_offline_sqlite_snapshot", "expected_head":EXPECTED_HEAD,
        "database_schema_ready":False, "execution_authorized":False,
        "runtime_loaded_version":None, "runtime_auto_policy_verified":False,
        "order_level_source_certified":False, "natural_acceptance_verified":False,
        "historical_receipts_certified":False, "checks":{}, "missing":[], "mismatched":[],
        "metadata_bytes_read":0}
    db = None
    budget = _Budget()
    try:
        path = Path(database).expanduser().resolve(strict=True)
        if not path.is_file() or _sidecars(path):
            raise ProbeBlocked("existing_offline_regular_file_without_sidecars_required")
        stamp = _stamp(path)
        # Safe ONLY for the explicitly offline/no-sidecar contract. Never point
        # at a live WAL database or use this command as a backup mechanism.
        db = sqlite3.connect(path.as_uri()+"?mode=ro&immutable=1", uri=True, timeout=0)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        calls = [0]
        def progress():
            calls[0] += 1000
            return int(calls[0]>MAX_STEPS or time.monotonic()>budget.deadline)
        db.set_progress_handler(progress, 1000)
        db.execute("BEGIN")
        # CAST(TEXT AS BLOB) uses the DATABASE encoding. SQL byte gates are
        # UTF8 gates only for UTF8 snapshots; generated encoding metadata itself
        # is a bounded ASCII enum. Refuse UTF16 before reading arbitrary DDL.
        encodings = budget.rows(db, "pragma_encoding", (),
            [("encoding",16)], 1, "snapshot_encoding_metadata_budget_or_invalid")
        if encodings != [("UTF-8",)]:
            raise ProbeBlocked("utf8_offline_snapshot_required")
        answer["checks"]["sqlite_encoding"] = "UTF-8"
        version_ddl = budget.schema(db, "table", "alembic_version")
        if version_ddl is None:
            answer["checks"]["migration_head"] = "unavailable"
            answer["missing"].append("alembic_version")
        else:
            versions = budget.rows(db, "alembic_version", (),
                [("version_num",80)], 2, "migration_head_metadata_budget_or_invalid")
            versions = [value for (value,) in versions]
            answer["observed_heads"] = versions
            answer["checks"]["migration_head"] = "matched" if versions==[EXPECTED_HEAD] else "mismatched"
            if versions!=[EXPECTED_HEAD]:
                answer["mismatched"].append("migration_head")
        for table in UNIQUE_KEYS:
            ddl = budget.schema(db, "table", table)
            if ddl is None:
                answer["missing"].append(table)
                continue
            columns, unique = _table(db, budget, table)
            expected = NEW_COLUMNS.get(table)
            if expected is not None:
                if (set(columns)!=set(expected)
                        or any(columns[name][0]!=kind for name,kind in expected.items() if name in columns)
                        or any(value[1]!=int(not (table==OBSERVATION and name=="source_quote_at"))
                               for name,value in columns.items())):
                    answer["mismatched"].append(table+".columns")
                checks = _check_expressions(ddl)
                wanted = {next(iter(_check_expressions("CHECK("+expression+")")))
                          for expression in TABLE_CHECKS.get(table,set())}
                if checks!=wanted:
                    answer["mismatched"].append(table+".check_constraints")
            elif (not BOOK_COLUMNS[table].issubset(columns) or columns["id"][0]!="INTEGER"):
                answer["mismatched"].append(table+".required_book_columns")
            if unique!=UNIQUE_KEYS[table]:
                answer["mismatched"].append(table+".unique_keys")
        guards = expected_guards()
        for name, expected in guards.items():
            installed = budget.schema(db, "trigger", name)
            if installed is None:
                answer["missing"].append(name)
            elif canonical_sql(installed)!=canonical_sql(expected):
                answer["mismatched"].append(name)
        answer["checks"]["required_guards"] = len(guards)
        answer["checks"]["book_row_values_and_foreign_key_runtime"] = "not_certified"
        answer["expected_guard_sha256"] = hashlib.sha256(json.dumps(
            {k:canonical_sql(v) for k,v in sorted(guards.items())}, sort_keys=True).encode()).hexdigest()
        if _stamp(path)!=stamp or _sidecars(path):
            raise ProbeBlocked("offline_snapshot_changed_during_read")
        answer["database_schema_ready"] = not answer["missing"] and not answer["mismatched"]
        answer["status"] = "schema_verified_not_deployment_certified" if answer["database_schema_ready"] else "blocked"
    except (ProbeBlocked, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        answer["error_kind"] = str(exc) if isinstance(exc, ProbeBlocked) else type(exc).__name__
        answer["database_schema_ready"] = False
    finally:
        if db is not None:
            db.close()
        answer["metadata_bytes_read"] = budget.used
    return answer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Explicit existing OFFLINE SQLite snapshot; never default DATABASE_URL")
    args = parser.parse_args(argv)
    report = inspect_snapshot(args.snapshot)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["database_schema_ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
