"""CLI uses only explicit read-only DB; never falls back to a default connection."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from scripts import paper_profit_protection_inputs as cli


@pytest.fixture
def args(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "archive").mkdir()
    dbpath = tmp_path / "fixture.db"
    with sqlite3.connect(dbpath) as db:
        db.execute("create table protected(id integer)")
        db.execute("insert into protected values (1)")
    return SimpleNamespace(database=dbpath,archive_root=tmp_path/"archive",
        start=date(2026,9,8),end=date(2026,9,8),as_of=datetime(2026,9,8,16),
        output=tmp_path/"outputs"/"new.json",policy_version="research:diagnostic",
        activation_profit_pct=5.,pullback_from_peak_pct=2.,
        max_source_age_sec=90,max_sample_gap_sec=90)


@pytest.mark.asyncio
async def test_cli_connection_actually_denies_writes_and_publishes_once(args, monkeypatch):
    from app.paper import profit_protection_inputs
    async def builder(db, **kwargs):
        assert kwargs["as_of"] == args.as_of
        assert kwargs["policies"][0].version == args.policy_version
        assert (await db.execute(text("PRAGMA query_only"))).scalar() == 1
        assert (await db.execute(text("select count(*) from protected"))).scalar() == 1
        with pytest.raises(DBAPIError):
            await db.execute(text("delete from protected"))
        return {"status":"blocked","position_audits":[],"positions":[]}
    monkeypatch.setattr(profit_protection_inputs,"build_profit_protection_inputs",builder)
    result = await cli.run(args)
    raw = args.output.read_bytes()
    assert json.loads(raw) == result
    assert stat.S_IMODE(args.output.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        await cli.run(args)
    assert args.output.read_bytes() == raw
    with sqlite3.connect(args.database) as db:
        assert db.execute("select count(*) from protected").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["future","aware","reverse","end_future","outside","missing_db","missing_archive","invalid_policy"])
async def test_invalid_boundaries_fail_before_connect(args, monkeypatch, fault):
    from sqlalchemy.ext import asyncio as sql_async
    monkeypatch.setattr(sql_async,"create_async_engine",lambda *a,**k: pytest.fail("unexpected DB connection"))
    if fault == "future": args.as_of = datetime.now()+timedelta(days=1)
    elif fault == "aware": args.as_of = args.as_of.replace(tzinfo=timezone.utc)
    elif fault == "reverse": args.start = args.end+timedelta(days=1)
    elif fault == "end_future": args.end = args.as_of.date()+timedelta(days=1)
    elif fault == "outside": args.output = args.output.parent.parent/"outside.json"
    elif fault == "missing_db": args.database = args.database.with_name("missing.db")
    elif fault == "missing_archive": args.archive_root = args.archive_root/"missing"
    elif fault == "invalid_policy": args.policy_version = "production"
    with pytest.raises(ValueError):
        await cli.run(args)
    assert not args.output.exists()
