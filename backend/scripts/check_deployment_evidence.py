"""Compare fresh v2 audit JSON; never open or modify market databases.

This is a locally trusted evidence gate, not a signature, schema migration,
business smoke test or proof of profitability. Old partial reports cannot pass.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re

try:
    from scripts.audit_deployment_evidence import CORE_TABLES
    from scripts.deployment_evidence_contract import (
        AUDIT_PROTOCOL, DIGEST_PROTOCOL, REVISIONS, TABLE_SINCE, EVIDENCE_SCHEMAS,
        expected_guards, normalized_sql, schema_issues,
    )
except ModuleNotFoundError:
    from audit_deployment_evidence import CORE_TABLES
    from deployment_evidence_contract import (
        AUDIT_PROTOCOL, DIGEST_PROTOCOL, REVISIONS, TABLE_SINCE, EVIDENCE_SCHEMAS,
        expected_guards, normalized_sql, schema_issues,
    )

TRIGGERS_BY_REVISION = {revision: set(expected_guards(revision)) for revision in REVISIONS[3:]}
REQUIRED_TRIGGERS = TRIGGERS_BY_REVISION["030_kline_observations"]


def _validate_report(report):
    """Reject malformed/partial owned JSON before the comparison loops."""
    try:
        if (type(report) is not dict or report["audit_protocol"] != AUDIT_PROTOCOL
                or report["digest_protocol"] != DIGEST_PROTOCOL or report["readonly"] is not True
                or not re.fullmatch(r"3\.11\.[0-9]+", report["python_version"])
                or report["core_tables"] != list(CORE_TABLES)):
            raise ValueError("audit protocol, read-only flag or digest scope invalid")
        if (type(report["database"]) is not str or not Path(report["database"]).is_absolute()
                or "\0" in report["database"]):
            raise ValueError("database identity must be an absolute path")
        if datetime.fromisoformat(report["observed_at"]).tzinfo is not None:
            raise ValueError("audit clock must be local naive")
        revision = report["revision"]
        if type(revision) is not list or len(revision) != 1 or revision[0] not in REVISIONS:
            raise ValueError("unsupported baseline/observed schema revision")
        counts, digests, schemas = report["counts"], report["core_digests"], report["table_schemas"]
        if (type(counts) is not dict or not counts
                or any(type(n) is not str or not n or type(v) is not int or v < 0 for n, v in counts.items())
                or type(digests) is not dict or type(schemas) is not dict):
            raise ValueError("invalid table counts or schema/digest mapping")
        required = {name for name in CORE_TABLES if TABLE_SINCE.get(name, 27) <= int(revision[0][:3])}
        if not required <= set(counts):
            raise ValueError("required core table missing: " + ",".join(sorted(required - set(counts))))
        if set(schemas) != set(counts) or set(digests) != set(counts) & set(CORE_TABLES):
            raise ValueError("incomplete schema or core digest denominator")
        for name, schema in schemas.items():
            columns, indexes, foreign = schema["columns"], schema["indexes"], schema["foreign_keys"]
            if (type(schema["sql"]) is not str or not schema["sql"]
                    or type(columns) is not list or not columns
                    or any(type(row) is not list or len(row) != 6
                           or type(row[0]) is not int or type(row[1]) is not str or not row[1]
                           or type(row[2]) is not str or type(row[3]) is not int or row[3] not in (0, 1)
                           or (row[4] is not None and type(row[4]) is not str)
                           or type(row[5]) is not int or row[5] < 0 for row in columns)
                    or len({row[1] for row in columns}) != len(columns)
                    or type(indexes) is not dict or type(foreign) is not list
                    or any(type(row) is not list or len(row) != 8 for row in foreign)):
                raise ValueError("malformed table schema: " + name)
            for index_name, index in indexes.items():
                if (type(index_name) is not str or not index_name
                        or type(index["unique"]) is not bool or type(index["partial"]) is not bool
                        or index["origin"] not in ("c", "u", "pk")
                        or type(index["columns"]) is not list or not index["columns"]
                        or any(value is not None and type(value) is not str for value in index["columns"])
                        or (index["sql"] is not None and type(index["sql"]) is not str)):
                    raise ValueError("malformed index schema: " + name)
            if name in digests:
                digest = digests[name]
                if (type(digest) is not dict or type(digest["count"]) is not int
                        or digest["count"] != counts[name]
                        or digest["columns"] != [row[1] for row in columns]
                        or type(digest["sha256"]) is not str
                        or not re.fullmatch("[0-9a-f]{64}", digest["sha256"])):
                    raise ValueError("invalid core digest: " + name)
        triggers, definitions = report["triggers"], report["trigger_definitions"]
        if (type(triggers) is not list or any(type(name) is not str for name in triggers)
                or triggers != sorted(set(triggers)) or type(definitions) is not dict
                or set(triggers) != set(definitions)):
            raise ValueError("incomplete trigger definition denominator")
        if any(type(row) is not dict or row["table"] not in counts
               or type(row["sql"]) is not str or not row["sql"] for row in definitions.values()):
            raise ValueError("invalid trigger definition")
    except (KeyError, TypeError, IndexError, ValueError, OverflowError) as exc:
        return [str(exc) or "malformed audit"]
    return []


def compare(before, after, *, expected_revision="030_kline_observations",
            expected_before_database=None, expected_after_database=None):
    if expected_revision not in TRIGGERS_BY_REVISION:
        return ["unsupported target schema revision"]
    problems = []
    for label, report in (("before", before), ("after", after)):
        problems.extend(f"invalid {label} audit: {issue}" for issue in _validate_report(report))
    if problems:
        return problems
    if after["revision"] != [expected_revision]:
        problems.append("wrong schema revision")
    if REVISIONS.index(before["revision"][0]) > REVISIONS.index(expected_revision):
        problems.append("baseline revision newer than release target")
    if before["python_version"] != after["python_version"]:
        problems.append("digest runtime version changed")
    if datetime.fromisoformat(before["observed_at"]) > datetime.fromisoformat(after["observed_at"]):
        problems.append("audit clocks reversed")
    pins = (expected_before_database, expected_after_database)
    if pins == (None, None):
        if before["database"] != after["database"]:
            problems.append("database identity changed; pin both baseline and target paths explicitly")
    elif any(type(pin) is not str or not Path(pin).is_absolute() for pin in pins):
        problems.append("both expected database identities must be absolute paths")
    elif before["database"] != pins[0] or after["database"] != pins[1]:
        problems.append("audit database identity does not match explicit pins")
    for name, count in before["counts"].items():
        if after["counts"].get(name) != count:
            problems.append(f"table count changed: {name}")
    for name, digest in before["core_digests"].items():
        if after["core_digests"].get(name) != digest:
            problems.append(f"core content changed: {name}")
    new_tables = set(after["counts"]) - set(before["counts"])
    permitted = {name for name, since in TABLE_SINCE.items()
                 if int(before["revision"][0][:3]) < since <= int(expected_revision[:3])}
    for name in sorted(new_tables):
        if name not in permitted:
            problems.append(f"unexpected new table: {name}")
        if after["counts"][name] != 0:
            problems.append(f"new table not empty: {name}")
    for name in set(before["table_schemas"]) & set(after["table_schemas"]):
        old, new = before["table_schemas"][name], after["table_schemas"][name]
        if any(old[key] != new[key] for key in ("sql", "columns", "foreign_keys")):
            problems.append(f"existing table schema changed: {name}")
        for index, definition in old["indexes"].items():
            if new["indexes"].get(index) != definition:
                problems.append(f"existing index changed: {name}.{index}")
        allowed_indexes = set(EVIDENCE_SCHEMAS.get(name, {}).get("indexes", {}))
        for index in set(new["indexes"]) - set(old["indexes"]) - allowed_indexes:
            problems.append(f"unexpected new index: {name}.{index}")
    for table in set(after["counts"]) & set(EVIDENCE_SCHEMAS):
        problems.extend(schema_issues(table, after["table_schemas"][table]))
    guards = expected_guards(expected_revision)
    if not set(guards) <= set(after["triggers"]):
        problems.append("required append-only triggers missing")
    for name, expected in guards.items():
        actual = after["trigger_definitions"].get(name)
        if actual is not None and (actual["table"] != expected["table"]
                or normalized_sql(actual["sql"]) != normalized_sql(expected["sql"])):
            problems.append(f"required append-only trigger body mismatch: {name}")
    for name, definition in before["trigger_definitions"].items():
        if name not in guards and after["trigger_definitions"].get(name) != definition:
            problems.append(f"existing non-release trigger changed: {name}")
    for name in set(after["triggers"]) - set(before["triggers"]) - set(guards):
        problems.append(f"unexpected new trigger: {name}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--expected-revision", choices=tuple(TRIGGERS_BY_REVISION),
                        default="030_kline_observations")
    parser.add_argument("--expected-before-database", type=str)
    parser.add_argument("--expected-after-database", type=str)
    args = parser.parse_args()
    try:
        before, after = (json.loads(path.read_text(encoding="utf-8")) for path in (args.before, args.after))
        problems = compare(before, after, expected_revision=args.expected_revision,
                           expected_before_database=args.expected_before_database,
                           expected_after_database=args.expected_after_database)
    except (OSError, ValueError) as exc:
        parser.error(f"invalid audit input: {exc}")
    if problems:
        for problem in problems:
            print(problem)
        raise SystemExit(1)
    print(f"PASS (no-business evidence comparison only): {len(before['counts'])} original table counts; "
          f"{len(before['core_digests'])} full core digests unchanged; "
          f"{args.expected_revision}; required trigger bodies verified; "
          f"prospective schemas checked: {len(set(after['counts']) & set(EVIDENCE_SCHEMAS))}; new tables empty")


if __name__ == "__main__":
    main()
