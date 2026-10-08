"""Offline-only schema probe. All DB setup is temporary; no runtime database."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from app.db.session import Base
from app.models.stock import StockAfterHoursObservation
from app.models.trading import (
    TradeOrder, TradeFill, PaperAfterHoursResourceScope, PaperAfterHoursResourceReceipt)
from app.models.paper import PaperAccount, PaperTradeLog
from scripts import check_after_hours_readiness as probe

TABLES = [StockAfterHoursObservation.__table__, PaperAccount.__table__,
    TradeOrder.__table__, TradeFill.__table__, PaperTradeLog.__table__,
    PaperAfterHoursResourceScope.__table__, PaperAfterHoursResourceReceipt.__table__]


@pytest.fixture
def snapshot(tmp_path):
    path = tmp_path/"offline.sqlite"
    engine = create_engine("sqlite:///"+str(path))
    with engine.begin() as conn:
        Base.metadata.create_all(conn, tables=TABLES)
        conn.exec_driver_sql("CREATE TABLE alembic_version(version_num VARCHAR(80) NOT NULL PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO alembic_version VALUES (?)", (probe.EXPECTED_HEAD,))
    engine.dispose()
    return path


def edit(path, statement, values=()):
    with sqlite3.connect(path) as db:
        db.execute(statement, values)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_schema_equivalent_is_not_runtime_source_or_natural_acceptance(snapshot):
    before = fingerprint(snapshot)
    answer = probe.inspect_snapshot(snapshot)
    assert answer["database_schema_ready"], answer
    assert answer["status"] == "schema_verified_not_deployment_certified"
    assert answer["read_only"] is True and answer["execution_authorized"] is False
    assert answer["runtime_loaded_version"] is None
    assert answer["order_level_source_certified"] is False
    assert answer["natural_acceptance_verified"] is False
    assert answer["historical_receipts_certified"] is False
    assert fingerprint(snapshot) == before
    assert not any(Path(str(snapshot)+suffix).exists() for suffix in ("-wal","-shm","-journal"))


@pytest.mark.parametrize("head", [None, "039_after_hours_resource_receipts", "041_future",
                                 "", "040_after_hours_partial_receipts\x00suffix"])
def test_head_missing_old_unknown_and_nul_never_infer_upgrade(snapshot, head):
    edit(snapshot, "DELETE FROM alembic_version")
    if head is not None:
        edit(snapshot, "INSERT INTO alembic_version VALUES (?)", (head,))
    before = fingerprint(snapshot)
    assert probe.inspect_snapshot(snapshot)["database_schema_ready"] is False
    assert fingerprint(snapshot) == before


def test_two_migration_heads_do_not_collapse_to_latest(snapshot):
    edit(snapshot,"INSERT INTO alembic_version VALUES ('041_other_branch')")
    assert probe.inspect_snapshot(snapshot)["checks"]["migration_head"] == "mismatched"


@pytest.mark.parametrize("trigger", ["after_hours_no_replace", "after_hours_no_update",
    "af_resource_scope_monotonic", "af_resource_receipt_book_binding", "af_bound_trade_fill_no_update"])
def test_missing_guards_are_not_installed_or_assumed(snapshot, trigger):
    with sqlite3.connect(snapshot) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
        assert trigger in names
        db.execute("DROP TRIGGER "+trigger)
    before = fingerprint(snapshot)
    answer = probe.inspect_snapshot(snapshot)
    assert answer["database_schema_ready"] is False and trigger in answer["missing"]
    assert fingerprint(snapshot) == before


def test_case_sensitive_literal_rewrite_is_not_hidden_by_sql_normalization(snapshot):
    name = "af_resource_receipt_book_binding"
    with sqlite3.connect(snapshot) as db:
        statement = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (name,)).fetchone()[0]
        assert probe.canonical_sql(statement) == probe.canonical_sql(probe.expected_guards()[name])
        db.execute("DROP TRIGGER "+name)
        changed = statement.replace("'paper'", "'PAPER'")
        assert changed != statement
        db.execute(changed)
    answer = probe.inspect_snapshot(snapshot)
    assert name in answer["mismatched"] and answer["database_schema_ready"] is False


@pytest.mark.parametrize("sidecar", ["-wal","-shm","-journal"])
def test_sidecars_block_before_connection_even_if_empty(snapshot, monkeypatch, sidecar):
    Path(str(snapshot)+sidecar).write_bytes(b"")
    monkeypatch.setattr(probe.sqlite3,"connect",lambda *a,**k:pytest.fail("must refuse live/journal snapshot"))
    answer = probe.inspect_snapshot(snapshot)
    assert answer["error_kind"] == "existing_offline_regular_file_without_sidecars_required"


def test_absent_path_never_creates_database_or_defaults_to_environment(tmp_path, monkeypatch):
    path = tmp_path/"missing.sqlite"
    monkeypatch.setenv("DATABASE_URL", "sqlite:////must-not-use.db")
    answer = probe.inspect_snapshot(path)
    assert not path.exists() and answer["database_schema_ready"] is False
    assert answer["error_kind"] == "FileNotFoundError"


def test_directory_is_not_database_and_no_output_files(tmp_path):
    before = set(tmp_path.iterdir())
    answer = probe.inspect_snapshot(tmp_path)
    assert answer["database_schema_ready"] is False and set(tmp_path.iterdir()) == before


def test_unrelated_massive_trigger_and_business_payload_are_not_read(snapshot, monkeypatch):
    with sqlite3.connect(snapshot) as db:
        db.execute("CREATE TABLE unrelated(note TEXT)")
        db.execute("INSERT INTO unrelated VALUES (?)", ("secret"*1000000,))
        db.execute("CREATE TRIGGER unrelated_big BEFORE INSERT ON unrelated BEGIN SELECT '"+("x"*100000)+"'; END")
    before = fingerprint(snapshot)
    answer = probe.inspect_snapshot(snapshot)
    assert answer["database_schema_ready"], answer
    assert answer["metadata_bytes_read"] < probe.MAX_METADATA_BYTES
    assert "secret" not in json.dumps(answer)
    assert fingerprint(snapshot) == before


def test_required_massive_ddl_is_sql_gated(snapshot, monkeypatch):
    name = "after_hours_no_update"
    with sqlite3.connect(snapshot) as db:
        db.execute("DROP TRIGGER "+name)
        db.execute("CREATE TRIGGER "+name+" BEFORE UPDATE ON stock_after_hours_observation "
            "BEGIN SELECT '"+("x"*probe.MAX_FIELD_BYTES)+"'; END")
    answer = probe.inspect_snapshot(snapshot)
    assert answer["database_schema_ready"] is False
    assert answer["error_kind"] == "schema_metadata_missing_or_byte_budget"


def test_probe_cannot_issue_updates_even_if_internal_helper_is_misused(snapshot, monkeypatch):
    original = probe._table
    def attempted_write(db, *args):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM alembic_version")
        return original(db,*args)
    monkeypatch.setattr(probe,"_table",attempted_write)
    before = fingerprint(snapshot)
    assert probe.inspect_snapshot(snapshot)["database_schema_ready"]
    assert fingerprint(snapshot) == before


def test_existing_snapshot_change_is_not_certified(snapshot, monkeypatch):
    original = probe._stamp
    calls = []
    def changed(path):
        calls.append(True)
        value = original(path)
        return value if len(calls) == 1 else (*value[:-1],value[-1]+1)
    monkeypatch.setattr(probe,"_stamp",changed)
    answer = probe.inspect_snapshot(snapshot)
    assert answer["error_kind"] == "offline_snapshot_changed_during_read"
    assert answer["database_schema_ready"] is False


def test_no_app_settings_engine_import_when_cli_runs_and_explicit_flag_required(snapshot):
    result = subprocess.run([sys.executable,str(Path(probe.__file__)), "--snapshot",str(snapshot)],
        capture_output=True,text=True,check=False)
    assert result.returncode == 0, result.stderr
    answer = json.loads(result.stdout)
    assert answer["database_schema_ready"] and answer["execution_authorized"] is False
    missing = subprocess.run([sys.executable,str(Path(probe.__file__))],
        capture_output=True,text=True,check=False)
    assert missing.returncode == 2 and "--snapshot" in missing.stderr
    isolated = subprocess.run([sys.executable, "-c",
        "import runpy,sys,json; runpy.run_path(sys.argv[1],run_name='offline_probe'); "
        "print(json.dumps([m for m in sys.modules if m.startswith("
        "('app.db','app.config','app.models','app.main','app.data.scheduler'))]))",
        str(Path(probe.__file__))], capture_output=True,text=True,check=False)
    assert isolated.returncode == 0, isolated.stderr
    assert json.loads(isolated.stdout) == []


def test_canonical_sql_preserves_quoted_json_paths_protocols_and_spaces():
    one = "CREATE TRIGGER IF NOT EXISTS t BEFORE INSERT ON x WHEN NEW.v='ABC  Def' BEGIN SELECT 1; END;"
    two = "create trigger t before insert on x when NEW.v='ABC  Def' begin select 1; end"
    assert probe.canonical_sql(one) == probe.canonical_sql(two)
    assert probe.canonical_sql(one) != probe.canonical_sql(one.replace("ABC","abc"))
    assert probe.canonical_sql(one) != probe.canonical_sql(one.replace("  Def"," Def"))


def damage_ddl(path, table, change):
    """Only temporary corruption fixtures; the probe never enables writable_schema."""
    with sqlite3.connect(path) as db:
        sql = db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]
        changed = change(sql)
        assert changed != sql
        db.execute("PRAGMA writable_schema=ON")
        db.execute("UPDATE sqlite_schema SET sql=? WHERE name=?", (changed,table))
        version = db.execute("PRAGMA schema_version").fetchone()[0]
        db.execute("PRAGMA schema_version="+str(version+1))
        db.execute("PRAGMA writable_schema=OFF")


@pytest.mark.parametrize("table,original,replacement,diagnostic", [
    (probe.SCOPE, "source_quote_at DATETIME NOT NULL", "source_quote_at DATETIME",
        ".columns"),
    (probe.OBSERVATION, "source_quote_at DATETIME", "source_quote_at DATETIME NOT NULL",
        ".columns"),
    (probe.SCOPE, "CHECK (revision >= 0 AND terminal_sequence >= 0)", "CHECK (1)",
        ".check_constraints"),
    (probe.RECEIPT, "CHECK (length(payload_json) <= 2097152)", "CHECK (1)",
        ".check_constraints"),
])
def test_new_column_and_real_check_constraints_are_required(
        snapshot, table, original, replacement, diagnostic):
    damage_ddl(snapshot,table,lambda sql:sql.replace(original,replacement))
    answer = probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"], answer
    assert table+diagnostic in answer["mismatched"], answer


def test_comment_or_quoted_string_check_bait_is_not_a_constraint(snapshot):
    damage_ddl(snapshot,probe.SCOPE,lambda sql:sql.replace(
        "CHECK (revision >= 0 AND terminal_sequence >= 0)",
        "CHECK (1) /* CHECK (revision >= 0 AND terminal_sequence >= 0) */"))
    answer = probe.inspect_snapshot(snapshot)
    assert probe.SCOPE+".check_constraints" in answer["mismatched"], answer
    assert probe._check_expressions("CREATE TABLE t(x TEXT DEFAULT 'CHECK (x>0)')")==set()


def test_added_unique_original_order_constraint_breaks_multi_fragment_schema(snapshot):
    edit(snapshot,"CREATE UNIQUE INDEX bad_original_single_fill ON "
        "paper_after_hours_resource_receipt(order_id)")
    answer = probe.inspect_snapshot(snapshot)
    assert probe.RECEIPT+".unique_keys" in answer["mismatched"], answer


def test_missing_observation_content_unique_does_not_pass(snapshot):
    damage_ddl(snapshot,probe.OBSERVATION,lambda sql:sql.replace(
        "CONSTRAINT uq_after_hours_content UNIQUE (code, trade_date, stage, source, source_version, content_hash)",
        "CONSTRAINT uq_after_hours_content UNIQUE (code)"))
    answer = probe.inspect_snapshot(snapshot)
    assert probe.OBSERVATION+".unique_keys" in answer["mismatched"], answer


def test_global_utf8_budget_is_exact_and_no_batch_can_overfetch(snapshot, monkeypatch):
    ordinary = probe.inspect_snapshot(snapshot)
    exact = ordinary["metadata_bytes_read"]
    assert ordinary["database_schema_ready"] and exact>0
    monkeypatch.setattr(probe,"MAX_METADATA_BYTES",exact)
    assert probe.inspect_snapshot(snapshot)["database_schema_ready"]
    monkeypatch.setattr(probe,"MAX_METADATA_BYTES",exact-1)
    denied = probe.inspect_snapshot(snapshot)
    assert not denied["database_schema_ready"]
    assert denied["metadata_bytes_read"] <= exact-1


@pytest.mark.parametrize("allowance", [14,15])
def test_whole_prefetched_batch_is_sql_gated_before_returned_text(monkeypatch, allowance):
    monkeypatch.setattr(probe,"MAX_METADATA_BYTES",allowance+6)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE metadata(v TEXT)")
        db.executemany("INSERT INTO metadata VALUES (?)",[("汉汉",),("汉汉汉",)])
        fetched = []
        class Cursor:
            def __init__(self,cursor): self.cursor=cursor
            def fetchall(self):
                rows=self.cursor.fetchall()
                fetched.extend(rows)
                return rows
        class Connection:
            def execute(self,*args): return Cursor(db.execute(*args))
        budget = probe._Budget()
        budget.used=6
        if allowance==15:
            assert budget.rows(Connection(),"metadata",(),[("v",16)],2,"too_big")==[
                ("汉汉",),("汉汉汉",)]
            assert budget.used==21
        else:
            with pytest.raises(probe.ProbeBlocked,match="too_big"):
                budget.rows(Connection(),"metadata",(),[("v",16)],2,"too_big")
            assert budget.used==6
            assert all(row[0] is None for row in fetched)
        assert len(fetched)==2


@pytest.mark.parametrize("maximum", [1,2])
def test_row_gate_returns_no_text_for_overfull_batch(maximum):
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE metadata(v TEXT)")
        db.executemany("INSERT INTO metadata VALUES (?)",[("a",),("b",),("c",)])
        budget=probe._Budget()
        with pytest.raises(probe.ProbeBlocked,match="too_many"):
            budget.rows(db,"metadata",(),[("v",16)],maximum,"too_many")
        assert budget.used==0


@pytest.mark.parametrize("limit,setting,error", [
    (1,"MAX_COLUMNS","column_metadata_row_budget_or_missing"),
    (1,"MAX_INDEXES","index_metadata_row_budget"),
    (0,"MAX_SECONDS","metadata_time_budget"),
    (0,"MAX_STEPS","OperationalError"),
])
def test_sql_metadata_work_limits_fail_closed_without_file_changes(
        snapshot,monkeypatch,limit,setting,error):
    monkeypatch.setattr(probe,setting,limit)
    before=fingerprint(snapshot)
    answer=probe.inspect_snapshot(snapshot)
    assert answer["database_schema_ready"] is False
    assert answer["error_kind"]==error, answer
    assert fingerprint(snapshot)==before


@pytest.mark.parametrize("head", ["x"*81,"汉"*30])
def test_head_per_field_limit_uses_bytes_not_characters(snapshot,head):
    edit(snapshot,"UPDATE alembic_version SET version_num=?", (head,))
    answer=probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"]
    assert answer["error_kind"]=="migration_head_metadata_budget_or_invalid"


def test_actual_038_039_040_migration_ddl_matches_offline_probe(tmp_path):
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path=tmp_path/"migrated-offline.sqlite"
    engine=create_engine("sqlite:///"+str(path))
    modules=[]
    root=Path(probe.__file__).resolve().parents[1]/"alembic"/"versions"
    for filename in ("038_after_hours_research.py","039_after_hours_resource_receipts.py",
                     "040_after_hours_partial_receipt_guards.py"):
        spec=importlib.util.spec_from_file_location("offline_"+filename[:-3],root/filename)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn,tables=[PaperAccount.__table__,TradeOrder.__table__,
                TradeFill.__table__,PaperTradeLog.__table__])
            conn.exec_driver_sql("CREATE TABLE alembic_version(version_num TEXT NOT NULL PRIMARY KEY)")
            with Operations.context(MigrationContext.configure(conn)):
                for module in modules:
                    module.upgrade()
                    conn.exec_driver_sql("DELETE FROM alembic_version")
                    conn.exec_driver_sql("INSERT INTO alembic_version VALUES (?)", (module.revision,))
    finally:
        engine.dispose()
    before=fingerprint(path)
    answer=probe.inspect_snapshot(path)
    assert answer["database_schema_ready"],answer
    assert fingerprint(path)==before


def test_declared_book_status_required_by_canonical_guard(snapshot):
    # SQLite allows CREATE TRIGGER bodies with a column absent from the book.
    damage_ddl(snapshot,"paper_account",lambda sql:sql.replace(
        "\n\tstatus VARCHAR(10),",""))
    answer=probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"],answer
    assert "paper_account.required_book_columns" in answer["mismatched"],answer


def test_extra_partial_unique_cannot_silently_disable_second_fragment(snapshot):
    edit(snapshot,"CREATE UNIQUE INDEX bad_partial_order ON "
        "paper_after_hours_resource_receipt(order_id) WHERE "
        "protocol_version='after_hours_partial_receipt_resources_v1_20261002'")
    answer=probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"],answer
    assert answer["error_kind"]=="unexpected_partial_unique_index",answer


@pytest.mark.parametrize("encoding",["UTF-16le","UTF-16be"])
def test_utf16_snapshot_rejected_before_arbitrary_schema_text(tmp_path,monkeypatch,encoding):
    path=tmp_path/"utf16.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA encoding='"+encoding+"'")
        db.execute("CREATE TABLE unrelated(note TEXT)")
        db.execute("CREATE TRIGGER big BEFORE INSERT ON unrelated BEGIN SELECT '"+
            ("汉"*23000)+"'; END")
        assert db.execute("PRAGMA encoding").fetchone()[0]==encoding
    queries=[]
    real_connect=probe.sqlite3.connect
    def traced(*args,**kwargs):
        db=real_connect(*args,**kwargs)
        db.set_trace_callback(queries.append)
        return db
    monkeypatch.setattr(probe.sqlite3,"connect",traced)
    before=fingerprint(path)
    answer=probe.inspect_snapshot(path)
    assert not answer["database_schema_ready"]
    assert answer["error_kind"]=="utf8_offline_snapshot_required",answer
    assert answer["metadata_bytes_read"]==len(encoding.encode())
    assert not any("FROM sqlite_schema" in query for query in queries)
    assert fingerprint(path)==before


def test_extra_check_cannot_silently_restrict_resource_revision(snapshot):
    damage_ddl(snapshot,probe.SCOPE,lambda sql:sql.replace(
        "CHECK (revision >= 0 AND terminal_sequence >= 0)",
        "CHECK (revision >= 0 AND terminal_sequence >= 0), CHECK (revision < 3)"))
    answer=probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"]
    assert probe.SCOPE+".check_constraints" in answer["mismatched"],answer



