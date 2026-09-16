"""Opt-in frozen audit regression. No business API, no writable connection.

Run with PAPER_ACCOUNTING_EVIDENCE=/absolute/path/to/evidence.sqlite.
Never substitutes this fixture for the currently deployed database.
"""
import hashlib
import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from app.paper.accounting import accounting_snapshot


@pytest.mark.skipif(not os.environ.get("PAPER_ACCOUNTING_EVIDENCE"), reason="explicit frozen fixture required")
def test_20260914_frozen_13_accounts_cash_fees_and_inventory():
    path = Path(os.environ["PAPER_ACCOUNTING_EVIDENCE"]).resolve()
    def sha():
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    before = sha()
    assert before == "68a7d1fb336da3bb46a92b6672dda3e3bff8c1de28a0588a0aa400fa00f7ae05"
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.row_factory = sqlite3.Row
    try:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        accounts = [dict(x) for x in db.execute("SELECT * FROM paper_account ORDER BY id")]
        assert len(accounts) == 13
        assert db.execute("SELECT count(*) FROM paper_trade_log").fetchone()[0] == 110
        assert db.execute("SELECT count(*) FROM paper_trade_log WHERE substr(trade_time,1,10)='2026-09-14'").fetchone()[0] == 25
        results = []
        for a in accounts:
            rows = [dict(x) for x in db.execute("SELECT * FROM paper_trade_log WHERE account_id=?", (a["id"],))]
            positions = [dict(x) for x in db.execute("SELECT * FROM paper_position WHERE account_id=? AND is_closed=0", (a["id"],))]
            out = accounting_snapshot(a, rows, positions, as_of=date(2026, 9, 14))
            assert out["status"] == "ok", (a["id"], out["issues"])
            assert out["cash_reconciliation_residual"] == 0
            assert out["asset_reconciliation_residual"] == 0
            assert out["unexplained_realized_adjustment"] == 0
            assert out["total_economic_pnl"] == pytest.approx(a["total_assets"] - a["initial_capital"], abs=.011)
            results.append(out)
        assert sum(len(x["positions"]) for x in results) == 9
        assert sum(x["gross_unrealized_pnl"] for x in results) == pytest.approx(1343.99)
        assert sum(x["net_unrealized_pnl"] for x in results) == pytest.approx(1298.99)
        assert sum(x["remaining_entry_fees"] for x in results) == 45
        assert sum(x["today_realized_ledger_pnl"] for x in results) == pytest.approx(-3817.16)
        assert sum(x["today_realized_net_pnl"] for x in results) == pytest.approx(-3817.16)
        assert sum(x["legacy_entry_fee_adjustment"] for x in results) == pytest.approx(-141.97)
        assert [x["account_scope"] for x in results].count("isolated_challenger") == 6
        assert [x["account_scope"] for x in results].count("primary") == 6
        assert results[0]["account_scope"] == "closed_legacy"
    finally:
        db.close()
    assert sha() == before
