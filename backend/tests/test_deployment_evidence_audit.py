from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from scripts.audit_deployment_evidence import audit_database
from deployment_fixtures import release_pair


def test_readonly_audit_reports_content_without_changing_bytes(tmp_path):
    path = tmp_path / "sample.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE stock_daily(id INTEGER PRIMARY KEY, close REAL)")
        db.executemany("INSERT INTO stock_daily VALUES(?, ?)", [(2, None), (1, 9.0)])
    before = path.read_bytes()
    first = audit_database(path)
    second = audit_database(path)
    assert first["readonly"]
    assert first["counts"] == {"stock_daily": 2}
    assert first["core_digests"] == second["core_digests"]
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        db.execute("UPDATE stock_daily SET close=10 WHERE id=1")
    assert audit_database(path)["core_digests"] != first["core_digests"]


def test_comparison_rejects_count_content_and_revision_changes(tmp_path):
    from scripts.check_deployment_evidence import compare
    before, after = release_pair(tmp_path)
    assert compare(before, after) == []
    after["core_digests"]["stock_daily"]["sha256"] = "a" * 64
    assert compare(before, after) == ["core content changed: stock_daily"]
    after["counts"]["stock_daily"] = 2
    after["core_digests"]["stock_daily"]["count"] = 2
    after["revision"] = ["027_fund_order_breakdown"]
    assert "table count changed: stock_daily" in compare(before, after)
    assert "wrong schema revision" in compare(before, after)


def test_031_is_explicit_and_requires_sale_table_and_all_guards(tmp_path):
    from copy import deepcopy
    from scripts.check_deployment_evidence import compare
    before, after = release_pair(tmp_path, "031_paper_sale_accounting")
    assert compare(before,after,expected_revision="031_paper_sale_accounting")==[]
    assert "wrong schema revision" in compare(before,after)  # default030 never silently advances
    for missing in ("paper_sale_accounting_no_update","paper_sale_accounting_no_delete","paper_sale_accounting_no_replace"):
        bad=deepcopy(after);bad["triggers"].remove(missing)
        bad["trigger_definitions"].pop(missing)
        assert "required append-only triggers missing" in compare(before,bad,expected_revision="031_paper_sale_accounting")
    after["counts"]["paper_sale_accounting"]=1
    after["core_digests"]["paper_sale_accounting"]["count"]=1
    assert "new table not empty: paper_sale_accounting" in compare(before,after,expected_revision="031_paper_sale_accounting")
    del after["counts"]["paper_sale_accounting"]
    assert any("required core table missing: paper_sale_accounting" in error for error in
               compare(before,after,expected_revision="031_paper_sale_accounting"))
    assert compare(before,after,expected_revision="future")==["unsupported target schema revision"]


def test_existing_sale_fee_evidence_is_part_of_full_content_digest(tmp_path):
    path=tmp_path/"sale-proof.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE paper_sale_accounting(id INTEGER PRIMARY KEY, payload_json TEXT)")
        db.execute("INSERT INTO paper_sale_accounting VALUES (1, ?)", ('{"original":1}',))
    before=audit_database(path)
    assert before["core_digests"]["paper_sale_accounting"]["count"]==1
    with sqlite3.connect(path) as db:
        db.execute("UPDATE paper_sale_accounting SET payload_json='{}'")
    after=audit_database(path)
    assert after["counts"]==before["counts"]
    assert after["core_digests"]!=before["core_digests"]


def test_missing_database_is_never_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        audit_database(path)
    assert not path.exists()


def test_cli_rejects_overwriting_database(tmp_path):
    path = tmp_path / "sample.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE stock_daily(id INTEGER PRIMARY KEY)")
    before = path.read_bytes()
    script = Path(__file__).resolve().parents[1] / "scripts" / "audit_deployment_evidence.py"
    result = subprocess.run([sys.executable, "-B", str(script), "--database", str(path),
                             "--output", str(path)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "output cannot replace" in result.stderr
    assert path.read_bytes() == before
