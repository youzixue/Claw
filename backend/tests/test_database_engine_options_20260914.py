"""Database bootstrap must work with declared and installed SQLAlchemy releases."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("kind", ["file", "file_uri", "memory", "empty_memory", "named_memory"])
def test_real_application_engine_pool_and_transaction_contract(tmp_path, kind):
    backend = Path(__file__).resolve().parents[1]
    database = tmp_path / "isolated.sqlite"
    urls = {
        "file": f"sqlite+aiosqlite:///{database}",
        "file_uri": f"sqlite+aiosqlite:///{database.as_uri()}?uri=true&mode=rwc",
        "memory": "sqlite+aiosqlite:///:memory:",
        "empty_memory": "sqlite+aiosqlite://",
        "named_memory": "sqlite+aiosqlite:///file:claw-engine-test?mode=memory&cache=shared&uri=true",
    }
    expected = "AsyncAdaptedQueuePool" if kind.startswith("file") else "StaticPool"
    program = r'''
import asyncio, json, sys
from pathlib import Path
backend, root, expected = sys.argv[1:]
sys.path.insert(0, backend)
opened, forbidden = [], []
def audit(event, args):
    if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "subprocess.Popen"}:
        forbidden.append(event)
        raise AssertionError("engine regression forbids external IO")
    if event == "sqlite3.connect":
        target = str(args[0])
        if target != ":memory:" and not target.startswith("file:claw-engine-test?"):
            from urllib.parse import urlparse, unquote
            path = unquote(urlparse(target).path) if target.startswith("file:") else target
            assert Path(path).resolve().is_relative_to(Path(root).resolve()), target
        opened.append(True)
sys.addaudithook(audit)
from app.db.session import engine
from sqlalchemy import event, text
pool = engine.pool
assert type(pool).__name__ == expected, type(pool).__name__
assert pool._pre_ping is True and pool._recycle == 17
if expected == "AsyncAdaptedQueuePool":
    assert pool.size() == 2 and pool._max_overflow == 1 and pool.timeout() == 3
options = []
@event.listens_for(engine.sync_engine, "do_connect")
def inspect_connect(dialect, record, cargs, cparams):
    options.append({key: cparams[key] for key in ("check_same_thread", "timeout")})
async def check():
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT)"))
        await conn.execute(text("INSERT INTO probe VALUES(1, 'original')"))
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT value FROM probe WHERE id=1")) == "original"
        await conn.execute(text("UPDATE probe SET value='must rollback' WHERE id=1"))
        await conn.rollback()
        if expected == "AsyncAdaptedQueuePool":
            async with engine.connect() as other:
                assert await other.scalar(text("SELECT value FROM probe WHERE id=1")) == "original"
                assert pool.checkedout() == 2
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT value FROM probe WHERE id=1")) == "original"
    await engine.dispose()
asyncio.run(check())
assert opened and not forbidden
assert all(value == {"check_same_thread": False, "timeout": 30} for value in options)
print(json.dumps({"pool": type(pool).__name__, "connections": len(opened), "forbidden": forbidden}))
'''
    env = {**os.environ, "DATABASE_URL": urls[kind], "SQL_ECHO": "false",
           "DB_POOL_SIZE": "2", "DB_MAX_OVERFLOW": "1", "DB_POOL_TIMEOUT": "3",
           "DB_POOL_RECYCLE": "17", "PYTHONDONTWRITEBYTECODE": "1",
           "CLAW_DISABLE_SCHEDULER": "1", "AI_ENABLED": "false",
           "AI_CONFIG_PATH": str(tmp_path / "no-ai.json")}
    result = subprocess.run([sys.executable, "-B", "-c", program, str(backend), str(tmp_path), expected],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pool"] == expected


@pytest.mark.parametrize("url,is_memory,is_sqlite", [
    ("sqlite+aiosqlite:///isolated.sqlite", False, True),
    ("sqlite+aiosqlite:///:memory:", True, True),
    ("sqlite+aiosqlite://", True, True),
    ("sqlite+aiosqlite:///file:shared?mode=memory&cache=shared&uri=true", True, True),
    ("postgresql+asyncpg://user:secret@example.invalid/claw", False, False),
])
def test_pure_options_keep_dialect_specific_arguments_separate(url, is_memory, is_sqlite):
    from app.db.engine_options import async_engine_options
    from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool
    options = async_engine_options(url, echo=False, pool_size=2, max_overflow=1,
                                   pool_timeout=3, pool_recycle=17)
    assert options["poolclass"] is (StaticPool if is_memory else AsyncAdaptedQueuePool)
    assert options["pool_pre_ping"] is True and options["pool_recycle"] == 17
    assert ("connect_args" in options) is is_sqlite
    if is_sqlite:
        assert options["connect_args"] == {"check_same_thread": False, "timeout": 30}
    for key in ("pool_size", "max_overflow", "pool_timeout"):
        assert (key in options) is not is_memory


def test_declared_wencai_matches_verified_available_runtime_pin():
    from packaging.requirements import Requirement
    path = Path(__file__).resolve().parents[1] / "requirements.txt"
    requirements = {item.name.lower(): item for line in path.read_text().splitlines()
                    if line.strip() and not line.startswith("#")
                    for item in [Requirement(line)]}
    assert str(requirements["pywencai"].specifier) == "==0.13.1"
    assert "asyncio" in requirements["sqlalchemy"].extras
    # Real IC uses scipy.stats, including on empty/degenerate inputs; no fake fallback.
    assert str(requirements["scipy"].specifier) == "==1.17.1"
