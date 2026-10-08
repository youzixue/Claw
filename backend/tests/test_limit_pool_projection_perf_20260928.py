"""Synthetic-scale regression: bounded SQL, not a production latency SLA."""
import time

import pytest
from sqlalchemy import event

from test_limit_pool_pipeline_20260928 import db, NOW, save, spot


@pytest.mark.asyncio
async def test_one_5000_quote_round_has_bounded_pool_sql(db):
    rows = []
    prefixes = ("600", "601", "603", "605", "000")
    for index in range(5000):
        code = prefixes[index // 1000] + f"{index % 1000:03d}"
        if code == "000000":
            code = "001000"
        kwargs = ({} if index < 100 else
                  {"price": 10.8, "high": 11} if index < 200 else
                  {"price": 9, "high": 10} if index < 250 else
                  {"price": 10.5, "high": 10.6})
        rows.append(spot(code, **kwargs))
    statements = []
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
    started = time.perf_counter()
    try:
        result = await save(db, rows)
    finally:
        elapsed = time.perf_counter() - started
        event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
    assert result["valid_count"] == 5000 and result["coverage"] == 1
    assert result["up_count"] == result["broken_count"] == 100
    assert result["down_count"] == 50
    assert len(statements) <= 10, "pool persistence regressed into per-stock SQL"
    print(f"synthetic_limit_pool_round: 5000 quotes, {len(statements)} SQL, {elapsed * 1000:.2f} ms")
