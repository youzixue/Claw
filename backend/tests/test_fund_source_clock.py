"""Isolated clock boundaries and legacy migration; no external requests."""
from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

from app.data.fund_flow_clock import eastmoney_quote_clock, local_clock, verified_fund_clocks
from app.data.scheduler import DataScheduler

NOW = datetime(2026, 9, 7, 10, 1, 2)


@pytest.mark.parametrize("raw", [None, True, False, "-", "invalid", float("nan"),
                                     float("inf"), 0, -1, 1788830000000, 1788830000.5])
def test_invalid_provider_seconds_never_become_local_now(raw):
    assert eastmoney_quote_clock(raw) is None


def test_f124_seconds_use_explicit_shanghai_timezone():
    seconds = int(NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
    assert eastmoney_quote_clock(seconds) == NOW
    assert eastmoney_quote_clock(str(seconds)) == NOW
    assert local_clock(NOW.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)) == NOW
    assert local_clock(pd.NaT) is None


@pytest.mark.parametrize("source,received,observed", [
    (None, NOW, NOW), (pd.NaT, NOW, NOW), ("invalid", NOW, NOW),
    (NOW, None, NOW), (NOW, pd.NaT, NOW), (NOW, NOW, None),
    (NOW + timedelta(seconds=1), NOW, NOW),
    (NOW, NOW + timedelta(seconds=1), NOW),
    (NOW - timedelta(seconds=601), NOW, NOW),
    (NOW - timedelta(days=1), NOW, NOW),
    (NOW, NOW, NOW - timedelta(seconds=1)),
])
def test_clock_order_date_and_freshness_fail_closed(source, received, observed):
    assert verified_fund_clocks(source, received, observed, NOW.date(), 600) is None


def test_exact_freshness_limit_and_equal_clocks_are_accepted():
    assert verified_fund_clocks(NOW, NOW, NOW, NOW.date(), 600) == (NOW, NOW, NOW)
    old = NOW - timedelta(seconds=600)
    assert verified_fund_clocks(old, NOW, NOW, NOW.date(), 600) == (old, NOW, NOW)


@pytest.mark.parametrize("limit", [0, -1, float("nan"), float("inf")])
def test_invalid_age_configuration_fails_closed(limit):
    assert verified_fund_clocks(NOW, NOW, NOW, NOW.date(), limit) is None


def test_parser_retains_only_fresh_rows_and_keeps_three_distinct_clocks():
    source = NOW - timedelta(seconds=20)
    received = NOW - timedelta(seconds=1)
    frame = pd.DataFrame([
        {"代码": "000001", "今日主力净流入-净额": 0, "今日主力净流入-净占比": 0,
         "source_quote_at": source, "received_at": received},
        {"代码": "000002", "今日主力净流入-净额": 100, "今日主力净流入-净占比": 1,
         "source_quote_at": NOW - timedelta(seconds=601), "received_at": received},
    ])
    rows = DataScheduler._parse_individual_fund_flow_df(frame, NOW.date(), observed_at=NOW)
    assert len(rows) == 1
    assert (rows[0]["source_quote_at"], rows[0]["received_at"], rows[0]["observed_at"]) == (source, received, NOW)
    assert rows[0]["main_net_inflow"] == 0


def test_tenbagger_snapshot_preserves_source_as_of_and_rejects_wrong_trade_date():
    from app.api.v1.tenbagger import _build_eastmoney_main_fund_items
    source = NOW - timedelta(seconds=20)
    received = NOW - timedelta(seconds=1)
    frame = pd.DataFrame([{
        "代码": "000001", "名称": "测试", "今日主力净流入-净额": 1,
        "今日主力净流入-净占比": 1, "source_quote_at": source, "received_at": received,
    }])
    result = _build_eastmoney_main_fund_items(frame, trade_date=NOW.date(), observed_at=NOW)
    assert result["000001"]["as_of"] == source.isoformat()
    assert result["000001"]["received_at"] == received.isoformat()
    assert _build_eastmoney_main_fund_items(frame, trade_date=date(2026, 9, 4), observed_at=NOW) == {}
    assert _build_eastmoney_main_fund_items(frame.drop(columns=["source_quote_at"]), observed_at=NOW) == {}


def test_cache_ttl_cannot_refresh_source_age_or_restore_legacy_unknown():
    from app.api.v1.tenbagger import _recheck_main_fund_snapshot_clocks
    source = NOW - timedelta(seconds=599)
    payload = {"source": "eastmoney_main_fund", "snapshot_time": NOW.isoformat(), "items": {
        "000001": {"source_quote_at": source.isoformat(), "received_at": NOW.isoformat(),
                   "observed_at": NOW.isoformat(), "as_of": source.isoformat(), "is_stale": False},
        "000002": {"main_net_inflow": 10, "is_stale": False},
    }}
    result = _recheck_main_fund_snapshot_clocks(payload, NOW.date(), NOW + timedelta(seconds=2))
    assert result["items"]["000001"]["is_stale"] is True
    assert result["items"]["000002"]["is_stale"] is True
    assert result["items"]["000001"]["as_of"] == source.isoformat()
    assert result["snapshot_time"] == NOW.isoformat()
    assert payload["items"]["000001"]["is_stale"] is False


@pytest.mark.asyncio
async def test_migration_adds_unknown_clocks_without_relabeling_history(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_funds.db'}")
    path = Path(__file__).resolve().parents[1] / "alembic/versions/025_fund_source_clocks.py"
    spec = importlib.util.spec_from_file_location("fund_clock_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def exercise(connection):
        metadata = sa.MetaData()
        legacy = sa.Table("fund_flow", metadata,
                          sa.Column("id", sa.Integer, primary_key=True),
                          sa.Column("main_net_inflow", sa.Float),
                          sa.Column("observed_at", sa.DateTime))
        metadata.create_all(connection)
        connection.execute(legacy.insert().values(id=1, main_net_inflow=123, observed_at=NOW))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        assert connection.execute(sa.select(legacy)).one() == (1, 123, NOW)
        assert connection.execute(sa.text("SELECT source_quote_at, received_at FROM fund_flow")).one() == (None, None)
        migration.downgrade()
        assert connection.execute(sa.select(legacy)).one() == (1, 123, NOW)
        assert "source_quote_at" not in {c["name"] for c in sa.inspect(connection).get_columns("fund_flow")}

    try:
        async with engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await engine.dispose()
