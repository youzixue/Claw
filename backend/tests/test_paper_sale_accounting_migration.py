"""Real Alembic runs only against tiny isolated SQLite fixtures."""
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys

import pytest
import sqlalchemy as sa

from app.models.paper import PaperTradeLog, PaperSaleAccounting
from app.paper.entry_fee_allocation import VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize("already_exists", [False, True])
async def test_030_to_031_never_backfills_trades_and_guards_evidence(tmp_path, already_exists):
    dbpath = (tmp_path / "before_031.db").resolve()
    assert dbpath.is_relative_to(tmp_path.resolve())
    engine = sa.create_engine(f"sqlite:///{dbpath}")
    at = datetime(2026,9,14,10)
    try:
        with engine.begin() as conn:
            PaperTradeLog.__table__.create(conn)
            conn.execute(PaperTradeLog.__table__.insert().values(id=1,account_id=7,code="000001",
                trade_type="buy",price=10,amount=100,commission=5,tax=0,trade_time=at))
            conn.execute(PaperTradeLog.__table__.insert().values(id=2,account_id=7,code="000001",
                trade_type="sell",price=11,amount=100,commission=5,tax=1.1,trade_time=at,realized_pnl=88.9))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('030_kline_observations')"))
            before=conn.execute(sa.text("SELECT * FROM paper_trade_log ORDER BY id")).all()
            old_evidence=[]
            if already_exists:
                PaperSaleAccounting.__table__.create(conn)
                for action in ("update","delete","replace"):
                    conn.execute(sa.text(f"DROP TRIGGER paper_sale_accounting_no_{action}"))
                conn.execute(PaperSaleAccounting.__table__.insert().values(
                    id=1,trade_id=2,account_id=7,code="000001",version=VERSION,recorded_at=at,
                    payload_json='{"original_fixture":true}'))
                old_evidence=conn.execute(sa.text("SELECT * FROM paper_sale_accounting")).all()
        engine.dispose()
        for _ in range(2):
            child=await asyncio.create_subprocess_exec(sys.executable,"-B","-m","alembic",
                "upgrade","031_paper_sale_accounting",cwd=Path(__file__).resolve().parents[1],
                env={**os.environ,"DATABASE_URL":f"sqlite+aiosqlite:///{dbpath}",
                     "PYTHONDONTWRITEBYTECODE":"1","CLAW_DISABLE_SCHEDULER":"1","SQL_ECHO":"false"},
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await asyncio.wait_for(child.communicate(),timeout=30)
            assert child.returncode==0,(stdout+stderr).decode()
        with engine.begin() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version"))=="031_paper_sale_accounting"
            assert conn.execute(sa.text("SELECT * FROM paper_trade_log ORDER BY id")).all()==before
            assert conn.execute(sa.text("SELECT * FROM paper_sale_accounting")).all()==old_evidence
            triggers=set(conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars())
            assert triggers=={f"paper_sale_accounting_no_{x}" for x in ("update","delete","replace")}
            if not already_exists:
                conn.execute(PaperSaleAccounting.__table__.insert().values(
                    id=1,trade_id=2,account_id=7,code="000001",version=VERSION,recorded_at=at,
                    payload_json='{"new_fixture":true}'))
        for sql in [
            "UPDATE paper_sale_accounting SET payload_json='{}' WHERE id=1",
            "DELETE FROM paper_sale_accounting WHERE id=1",
            "INSERT OR REPLACE INTO paper_sale_accounting SELECT * FROM paper_sale_accounting WHERE id=1",
        ]:
            with engine.connect() as conn:
                with pytest.raises(sa.exc.IntegrityError,match="append-only"):
                    conn.execute(sa.text(sql))
                conn.rollback()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT * FROM paper_trade_log ORDER BY id")).all()==before
    finally:
        engine.dispose()
