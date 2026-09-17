"""Isolated release gate regressions; no live DB, server or business endpoint."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from scripts.audit_deployment_evidence import audit_database
from scripts.check_deployment_evidence import compare, TRIGGERS_BY_REVISION
from deployment_fixtures import create_release_database, release_pair


def test_empty_evidence_cannot_authorize_release():
    before = {"counts": {}, "core_digests": {}}
    after = {**deepcopy(before), "revision": ["030_kline_observations"],
             "triggers": sorted(TRIGGERS_BY_REVISION["030_kline_observations"])}
    assert compare(before, after), "empty evidence must not print a passing release gate"


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    return release_pair(tmp_path_factory.mktemp("release-gate"), "034_data_watermark_revisions")


@pytest.fixture
def reports(pair):
    return deepcopy(pair)


def gate(reports, **kwargs):
    return compare(*reports, expected_revision="034_data_watermark_revisions", **kwargs)


def test_complete_real_audits_pass_and_default_does_not_silently_advance(reports):
    assert gate(reports) == []
    assert "wrong schema revision" in compare(*reports)


@pytest.mark.parametrize("field", [
    "audit_protocol", "digest_protocol", "readonly", "core_tables",
    "python_version", "database", "observed_at", "revision", "table_schemas",
    "trigger_definitions", "counts", "core_digests",
])
@pytest.mark.parametrize("side", [0, 1])
def test_partial_or_legacy_reports_are_rejected(reports, side, field):
    reports[side].pop(field)
    assert gate(reports)


@pytest.mark.parametrize("field,bad", [
    ("readonly", False), ("readonly", 1), ("core_tables", []),
    ("python_version", "3.12.0"), ("database", "relative.sqlite"),
    ("observed_at", "bad"), ("revision", []), ("counts", {}),
    ("table_schemas", []), ("triggers", "names"), ("trigger_definitions", {}),
])
def test_bad_types_and_protocols_never_become_pass(reports, field, bad):
    reports[1][field] = bad
    assert gate(reports)


@pytest.mark.parametrize("bad", [True, -1, "1", None, 1.5])
def test_counts_require_nonnegative_integers(reports, bad):
    reports[1]["counts"]["stock_daily"] = bad
    assert gate(reports)


@pytest.mark.parametrize("table", ["stock_daily", "stock_kline_observation", "trade_order"])
def test_omitted_core_content_digest_is_not_optional(reports, table):
    reports[1]["core_digests"].pop(table)
    assert gate(reports)


def test_wrong_database_rejected_unless_both_identities_explicitly_pinned(reports):
    reports[1]["database"] = "/isolated/other.sqlite"
    assert gate(reports)
    assert gate(reports, expected_before_database=reports[0]["database"],
                expected_after_database="/isolated/other.sqlite") == []
    assert gate(reports, expected_after_database="/isolated/other.sqlite")
    assert gate(reports, expected_before_database="/wrong/baseline.sqlite",
                expected_after_database="/isolated/other.sqlite")


def test_reversed_clock_and_newer_baseline_rejected(reports):
    reports[0]["observed_at"] = "2099-01-01T00:00:00"
    assert "audit clocks reversed" in gate(reports)
    # Existing complete033 snapshot cannot be treated as a030 upgrade baseline.
    after = reports[1]
    assert "baseline revision newer than release target" in compare(after, deepcopy(after))


@pytest.mark.parametrize("name", sorted(TRIGGERS_BY_REVISION["034_data_watermark_revisions"]))
def test_trigger_name_without_effective_body_does_not_pass(reports, name):
    reports[1]["trigger_definitions"][name]["sql"] = f"CREATE TRIGGER {name} BEFORE UPDATE ON stock_daily BEGIN SELECT 1; END"
    assert any("trigger body mismatch" in problem for problem in gate(reports))


@pytest.mark.parametrize("suffix", [
    "WHEN 0", "WHEN NEW.id < 0", "WHEN NEW.id = 999999999",
])
def test_conditional_noop_guards_rejected(reports, suffix):
    row = reports[1]["trigger_definitions"]["factor_computation_run_no_update"]
    row["sql"] = row["sql"].replace(" BEGIN ", f" {suffix} BEGIN ")
    assert any("trigger body mismatch" in problem for problem in gate(reports))


def test_existing_schema_change_with_same_counts_and_values_rejected(reports):
    reports[1]["table_schemas"]["stock_daily"]["sql"] += " /* unexpected rebuild */"
    assert "existing table schema changed: stock_daily" in gate(reports)


@pytest.mark.parametrize("mutation", ["wrong_type", "nullable", "wrong_pk", "missing_unique", "wrong_index", "partial_index", "wrong_fk"])
def test_new_evidence_schema_must_match_real_contract(reports, mutation):
    schema = reports[1]["table_schemas"]["paper_sale_accounting"]
    if mutation == "wrong_type":
        schema["columns"][1][2] = "TEXT"
    elif mutation == "nullable":
        schema["columns"][1][3] = 0
    elif mutation == "wrong_pk":
        schema["columns"][0][5] = 0
    elif mutation == "missing_unique":
        for item in schema["indexes"].values():
            item["unique"] = False
    elif mutation == "wrong_index":
        schema["indexes"]["ix_paper_sale_accounting_account_code"]["columns"].reverse()
    elif mutation == "partial_index":
        schema["indexes"]["ix_paper_sale_accounting_account_code"]["partial"] = True
    else:
        schema["foreign_keys"] = []
    assert any("contract mismatch" in problem for problem in gate(reports))


def test_count_preserving_kline_observation_change_is_now_content_audited(tmp_path):
    path = tmp_path / "observations.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE stock_kline_observation(id INTEGER PRIMARY KEY, payload TEXT)")
        db.execute("INSERT INTO stock_kline_observation VALUES(1, 'original')")
    first = audit_database(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE stock_kline_observation SET payload='changed' WHERE id=1")
    second = audit_database(path)
    assert first["counts"] == second["counts"]
    assert first["core_digests"] != second["core_digests"]


def test_extra_partial_unique_constraint_not_silently_ignored(reports):
    schema = reports[1]["table_schemas"]["factor_computation_run"]
    schema["indexes"]["unexpected_partial_unique"] = {
        "unique": True, "origin": "c", "partial": True, "columns": ["protocol_version"],
        "sql": "CREATE UNIQUE INDEX unexpected_partial_unique ON factor_computation_run(protocol_version) WHERE 1",
    }
    assert any("unique contract mismatch" in message for message in gate(reports))


def test_expected_query_index_wrong_identity_or_predicate_is_rejected(reports):
    schema = reports[1]["table_schemas"]["factor_computation_run"]
    schema["indexes"]["ix_factor_computation_day_time"]["partial"] = True
    assert any("index contract mismatch" in message for message in gate(reports))


def test_unexpected_new_trigger_is_not_an_automatic_release_change(reports):
    after = reports[1]
    after["trigger_definitions"]["unreviewed_writer"] = {
        "table": "stock_daily", "sql": "CREATE TRIGGER unreviewed_writer AFTER INSERT ON stock_daily BEGIN SELECT 1; END",
    }
    after["triggers"] = sorted(after["trigger_definitions"])
    assert "unexpected new trigger: unreviewed_writer" in gate(reports)


def test_actual_wrong_same_named_trigger_is_visible_without_testing_writes_on_production(tmp_path):
    before, after = release_pair(tmp_path, "034_data_watermark_revisions")
    path = Path(after["database"])
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER factor_computation_run_no_update")
        db.execute("CREATE TRIGGER factor_computation_run_no_update BEFORE UPDATE OF missing_column "
                   "ON factor_computation_run BEGIN SELECT RAISE(ABORT, 'factor computation evidence is append-only'); END")
    bad = audit_database(path)
    assert bad["counts"] == after["counts"] and bad["core_digests"] == after["core_digests"]
    assert any("trigger body mismatch" in message for message in compare(
        before, bad, expected_revision="034_data_watermark_revisions"))


@pytest.mark.parametrize("malformed", [False, True])
def test_cli_reports_failure_instead_of_crashing_or_printing_pass(tmp_path, reports, malformed):
    first, last = (tmp_path / "before.json", tmp_path / "after.json")
    first.write_text(json.dumps(reports[0]))
    last.write_text("{" if malformed else json.dumps({}))
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_deployment_evidence.py"
    result = subprocess.run([sys.executable, "-B", str(script), str(first), str(last)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode in (1, 2)
    assert "PASS" not in result.stdout and "Traceback" not in result.stderr


def test_successful_030_cli_does_not_claim_031_to_033_are_present(tmp_path):
    before, after = release_pair(tmp_path)
    first, last = tmp_path / "before.json", tmp_path / "after.json"
    first.write_text(json.dumps(before))
    last.write_text(json.dumps(after))
    script = Path(__file__).resolve().parents[1] / "scripts" / "check_deployment_evidence.py"
    result = subprocess.run([sys.executable, "-B", str(script), str(first), str(last)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "prospective schemas checked: 0" in result.stdout
    assert "031–033 schemas verified" not in result.stdout


def test_real_030_to_034_and_disabled_full_lifespan_have_no_business_changes(tmp_path):
    path = create_release_database(tmp_path)
    before = audit_database(path)
    backend = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{path}",
           "CLAW_DISABLE_SCHEDULER": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "QUOTE_ROUND_ARCHIVE_ENABLED": "false", "SQL_ECHO": "false",
           "SCHEDULER_PREVENT_IDLE_SLEEP": "false"}
    child = subprocess.run([sys.executable, "-B", "-m", "alembic", "upgrade", "034_data_watermark_revisions"],
                           cwd=backend, env=env, capture_output=True, text=True, timeout=40)
    assert child.returncode == 0, child.stderr
    program = """
import asyncio, json, socket
def forbidden(*args, **kwargs):
    raise AssertionError('isolated no-business smoke forbids network/scheduler')
socket.socket.connect = forbidden
from app.main import app
from app.data.scheduler import data_scheduler
from httpx import ASGITransport, AsyncClient
data_scheduler.start = forbidden
async def smoke():
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://isolated') as client:
            assert (await client.get('/')).status_code == 200
            result = await client.get('/health')
            assert result.status_code == 200
            assert result.json()['scheduler']['running'] is False
            assert result.json()['scheduler']['job_count'] == 0
asyncio.run(smoke())
"""
    child = subprocess.run([sys.executable, "-B", "-c", program],
                           cwd=backend, env=env, capture_output=True, text=True, timeout=40)
    assert child.returncode == 0, child.stderr
    after = audit_database(path)
    assert compare(before, after, expected_revision="034_data_watermark_revisions") == []
    assert after["counts"]["trade_order"] == after["counts"]["paper_trade_log"] == 0
    assert all(after["counts"][table] == 0 for table in (
        "paper_sale_accounting", "anomaly_candidate_evidence", "factor_computation_run"))

