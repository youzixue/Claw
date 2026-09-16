"""Actual 032->033 CLI against temporary SQLite, plus no-business release checks."""
import asyncio
from datetime import date, datetime
import os
from pathlib import Path
import sys

import pytest
import sqlalchemy as sa

from app.models.factor import FactorValue, FactorComputationRun
from scripts.check_deployment_evidence import compare, TRIGGERS_BY_REVISION


async def upgrade(dbpath):
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-B", "-m", "alembic", "upgrade", "033_factor_computation_runs",
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{dbpath}",
             "PYTHONDONTWRITEBYTECODE": "1", "CLAW_DISABLE_SCHEDULER": "1", "SQL_ECHO": "false"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=30)
    except BaseException:
        if child.returncode is None:
            child.kill()
            await child.wait()
        raise
    return child.returncode, (stdout + stderr).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_exists", [False, True])
async def test_032_to_033_preserves_every_old_row_and_restores_guards(tmp_path, already_exists):
    path = (tmp_path / "migration.db").resolve()
    assert path.is_relative_to(tmp_path.resolve())
    engine = sa.create_engine(f"sqlite:///{path}")
    at = datetime(2026, 9, 14, 16)
    try:
        with engine.begin() as c:
            FactorValue.__table__.create(c)
            c.execute(FactorValue.__table__.insert().values(
                stock_code="600001", trade_date=date(2026, 9, 11), factor_name="ma5_bias",
                factor_value=3, factor_rank=1, factor_pct=1))
            old = c.execute(sa.text("SELECT * FROM factor_values")).all()
            c.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)"))
            c.execute(sa.text("INSERT INTO alembic_version VALUES ('032_anomaly_candidate_evidence')"))
            prior = []
            if already_exists:
                FactorComputationRun.__table__.create(c)
                for action in ("update", "delete", "replace"):
                    c.execute(sa.text(f"DROP TRIGGER factor_computation_run_no_{action}"))
                c.execute(FactorComputationRun.__table__.insert().values(
                    capture_id="prior", trade_date=date(2026, 9, 11), read_started_at=at, captured_at=at,
                    protocol_version="fixture", payload_hash="fixture", payload_json='{"fixture":true}'))
                prior = c.execute(sa.text("SELECT * FROM factor_computation_run")).all()
        engine.dispose()
        for _ in range(2):
            code, output = await upgrade(path)
            assert code == 0, output
        with engine.begin() as c:
            assert c.scalar(sa.text("SELECT version_num FROM alembic_version")) == "033_factor_computation_runs"
            assert c.execute(sa.text("SELECT * FROM factor_values")).all() == old
            assert c.execute(sa.text("SELECT * FROM factor_computation_run")).all() == prior
            triggers = set(c.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars())
            assert triggers == {f"factor_computation_run_no_{x}" for x in ("update", "delete", "replace")}
            if not already_exists:
                c.execute(FactorComputationRun.__table__.insert().values(
                    capture_id="new", trade_date=date(2026, 9, 11), read_started_at=at, captured_at=at,
                    protocol_version="fixture", payload_hash="fixture", payload_json="{}"))
        for statement in (
            "UPDATE factor_computation_run SET payload_json='changed'",
            "DELETE FROM factor_computation_run",
            "INSERT OR REPLACE INTO factor_computation_run SELECT * FROM factor_computation_run",
        ):
            with engine.connect() as c:
                with pytest.raises(sa.exc.IntegrityError, match="append-only"):
                    c.execute(sa.text(statement))
                c.rollback()
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_incompatible_existing_schema_does_not_get_silently_approved(tmp_path):
    path = (tmp_path / "incompatible.db").resolve()
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as c:
            c.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)"))
            c.execute(sa.text("INSERT INTO alembic_version VALUES ('032_anomaly_candidate_evidence')"))
            c.execute(sa.text("CREATE TABLE factor_computation_run (id INTEGER PRIMARY KEY, payload_json TEXT)"))
            c.execute(sa.text("INSERT INTO factor_computation_run VALUES (1, 'preserve')"))
        code, output = await upgrade(path)
        assert code != 0 and "schema incompatible" in output
        with engine.connect() as c:
            assert c.scalar(sa.text("SELECT version_num FROM alembic_version")) == "032_anomaly_candidate_evidence"
            assert c.execute(sa.text("SELECT * FROM factor_computation_run")).all() == [(1, "preserve")]
    finally:
        engine.dispose()


def test_release_comparison_requires_all_three_new_tables_and_guards(tmp_path):
    from deployment_fixtures import release_pair
    before, after = release_pair(tmp_path, "033_factor_computation_runs")
    assert compare(before, after, expected_revision="033_factor_computation_runs") == []
    for table in ("paper_sale_accounting", "anomaly_candidate_evidence", "factor_computation_run"):
        copy = {**after, "counts": {k: v for k, v in after["counts"].items() if k != table}}
        assert compare(before, copy, expected_revision="033_factor_computation_runs")
    after["triggers"].remove("factor_computation_run_no_replace")
    after["trigger_definitions"].pop("factor_computation_run_no_replace")
    assert "required append-only triggers missing" in compare(
        before, after, expected_revision="033_factor_computation_runs")
    after["counts"]["factor_computation_run"] = 1
    after["core_digests"]["factor_computation_run"]["count"] = 1
    assert "new table not empty: factor_computation_run" in compare(
        before, after, expected_revision="033_factor_computation_runs")


@pytest.mark.parametrize("table", ["factor_values", "factor_computation_run"])
def test_release_audit_tracks_factor_content_not_just_counts(tmp_path, table):
    import sqlite3
    from scripts.audit_deployment_evidence import audit_database
    path = tmp_path / "audit.db"
    with sqlite3.connect(path) as db:
        db.execute(f"CREATE TABLE {table}(id INTEGER PRIMARY KEY, payload TEXT)")
        db.execute(f"INSERT INTO {table} VALUES(1, 'original')")
    before = audit_database(path)
    with sqlite3.connect(path) as db:
        db.execute(f"UPDATE {table} SET payload='changed' WHERE id=1")
    after = audit_database(path)
    assert before["counts"] == after["counts"]
    assert before["core_digests"][table] != after["core_digests"][table]
