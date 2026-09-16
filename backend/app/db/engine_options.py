"""Explicit async pool contract, independent of SQLAlchemy's changing defaults.

This helper constructs options only: no settings, engine, DB connection or IO.
"""
from sqlalchemy.engine import make_url
from sqlalchemy.pool import AsyncAdaptedQueuePool, StaticPool


def async_engine_options(database_url, *, echo, pool_size, max_overflow,
                         pool_timeout, pool_recycle):
    url = make_url(database_url)
    options = {
        "echo": echo, "future": True, "pool_pre_ping": True,
        "pool_recycle": pool_recycle,
    }
    is_sqlite = url.get_backend_name() == "sqlite"
    if is_sqlite:
        # Keep the existing file-database busy timeout and thread policy.
        options["connect_args"] = {"check_same_thread": False, "timeout": 30}
    is_memory = is_sqlite and (
        not url.database or url.database == ":memory:" or url.query.get("mode") == "memory"
    )
    if is_memory:
        # One logical in-memory DB must not become one separate DB per pool slot.
        options["poolclass"] = StaticPool
    else:
        # aiosqlite used NullPool before SQLAlchemy 2.0.38; queue options otherwise
        # fail at import with the declared 2.0.35, while 2.0.48 happens to accept them.
        options.update(poolclass=AsyncAdaptedQueuePool, pool_size=pool_size,
                       max_overflow=max_overflow, pool_timeout=pool_timeout)
    return options
