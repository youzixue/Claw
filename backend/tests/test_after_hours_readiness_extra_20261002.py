"""Additional offline metadata counterexamples; every database is temporary."""
import sqlite3
import pytest
from test_after_hours_readiness_20261002 import snapshot, edit, fingerprint
from scripts import check_after_hours_readiness as probe


@pytest.mark.parametrize("table,column,collation", [
    (probe.RECEIPT,"allocation_id","NOCASE"),
    (probe.RECEIPT,"allocation_id","RTRIM"),
    (probe.SCOPE,"scope_key","NOCASE"),
    ("trade_order","order_id","NOCASE"),
])
def test_duplicate_signature_cannot_hide_different_unique_collation(
        snapshot,table,column,collation):
    edit(snapshot,f"CREATE UNIQUE INDEX bad_collation ON {table}({column} COLLATE {collation})")
    before=fingerprint(snapshot)
    answer=probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"],answer
    assert answer["error_kind"]=="unexpected_unique_key_collation",answer
    assert fingerprint(snapshot)==before


def test_redundant_binary_unique_does_not_invent_a_new_identity(snapshot):
    edit(snapshot,"CREATE UNIQUE INDEX redundant_binary ON "
        "paper_after_hours_resource_receipt(allocation_id COLLATE BINARY DESC)")
    answer=probe.inspect_snapshot(snapshot)
    assert answer["database_schema_ready"],answer


def test_snapshot_filename_uri_characters_are_not_query_parameters(snapshot):
    path=snapshot.with_name("离线 #? % snapshot.sqlite")
    snapshot.rename(path)
    before=fingerprint(path)
    answer=probe.inspect_snapshot(path)
    assert answer["database_schema_ready"],answer
    assert fingerprint(path)==before


@pytest.mark.parametrize("table,old_primary,new_primary", [
    (probe.RECEIPT,"id","allocation_id"),
    (probe.SCOPE,"scope_key","account_numeric_id, code, trade_date"),
])
def test_primary_cannot_be_swapped_with_same_signature_unique(
        snapshot,table,old_primary,new_primary):
    with sqlite3.connect(snapshot) as db:
        ddl=db.execute("SELECT sql FROM sqlite_schema WHERE name=?", (table,)).fetchone()[0]
        changed=ddl.replace("PRIMARY KEY ("+old_primary+")","UNIQUE ("+old_primary+")")
        changed=changed.replace("UNIQUE ("+new_primary+")","PRIMARY KEY ("+new_primary+")")
        assert changed!=ddl
        # Recreate valid empty temporary schema, rather than orphaning autoindexes.
        for name in probe.expected_guards():
            db.execute("DROP TRIGGER IF EXISTS "+name)
        db.execute("DROP TABLE "+table)
        db.execute(changed)
        for statement in probe.expected_guards().values():
            db.execute(statement)
    answer=probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"],answer
    assert answer["error_kind"]=="unexpected_primary_key",answer


def test_sidecar_appearing_after_initial_gate_blocks_certificate(snapshot,monkeypatch):
    real=probe._table
    calls=[]
    def appeared(db,*args):
        result=real(db,*args)
        if not calls:
            calls.append(True)
            # This fixture creates a zero-byte sidecar; probe itself never does.
            snapshot.with_name(snapshot.name+"-wal").write_bytes(b"")
        return result
    monkeypatch.setattr(probe,"_table",appeared)
    answer=probe.inspect_snapshot(snapshot)
    assert not answer["database_schema_ready"]
    assert answer["error_kind"]=="offline_snapshot_changed_during_read"


def test_non_sqlite_regular_file_is_never_repaired(tmp_path):
    path=tmp_path/"not-sqlite.bin"
    path.write_bytes(b"not an SQLite database\n")
    before=fingerprint(path)
    answer=probe.inspect_snapshot(path)
    assert not answer["database_schema_ready"]
    assert answer["error_kind"]=="DatabaseError"
    assert fingerprint(path)==before
