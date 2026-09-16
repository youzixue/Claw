"""Exercise the existing 023 -> 024 migration on an isolated legacy schema."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_quote_round_upgrade_preserves_legacy_records_and_account_switches(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_023.db'}")
    migration_path = Path(__file__).resolve().parents[1] / "alembic/versions/024_quote_round_execution.py"
    spec = importlib.util.spec_from_file_location("quote_round_024_test", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def exercise(connection):
        metadata = sa.MetaData()
        tables = {}
        for name in ("stock_spot", "paper_trade_log", "paper_auto_trade_log", "trade_order", "trade_fill"):
            columns = [sa.Column("id", sa.Integer, primary_key=True), sa.Column("legacy_marker", sa.String(40))]
            if name == "paper_trade_log":
                columns.append(sa.Column("signal_id", sa.String(40), nullable=True))
            tables[name] = sa.Table(name, metadata, *columns)
        accounts = sa.Table("paper_account", metadata,
                            sa.Column("id", sa.Integer, primary_key=True),
                            sa.Column("auto_order_enabled", sa.Boolean, nullable=False))
        metadata.create_all(connection)
        for table in tables.values():
            connection.execute(table.insert().values(id=1, legacy_marker="preserve"))
        connection.execute(accounts.insert(), [{"id": 1, "auto_order_enabled": True},
                                               {"id": 2, "auto_order_enabled": False}])
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()  # Existing init_db may have already added columns/tables.
        inspector = sa.inspect(connection)
        assert {"quote_round", "paper_control_sample", "paper_daily_outcome"} <= set(inspector.get_table_names())
        for name in tables:
            assert connection.execute(sa.text(f"SELECT legacy_marker FROM {name}")).scalars().all() == ["preserve"]
        assert connection.execute(sa.text("SELECT auto_order_enabled FROM paper_account ORDER BY id")).scalars().all() == [1, 0]
        assert connection.execute(sa.text("SELECT tax, forced_probe, excluded_from_performance FROM paper_trade_log")).one() == (0, 0, 0)
        assert connection.execute(sa.text("SELECT decision_round_id, idempotency_key FROM trade_order")).one() == (None, None)
        assert connection.execute(sa.text("SELECT quote_round_id, reason_code FROM paper_auto_trade_log")).one() == (None, None)
        signal_column = next(item for item in inspector.get_columns("paper_trade_log") if item["name"] == "signal_id")
        assert signal_column["type"].length == 80
        assert any(item["name"] == "uq_trade_order_idempotency_key" and item["unique"] for item in inspector.get_indexes("trade_order"))
        # Migration creates audit structure only: no inferred rounds or synthetic trades.
        for name in ("quote_round", "paper_control_sample", "paper_daily_outcome"):
            assert connection.scalar(sa.text(f"SELECT COUNT(*) FROM {name}")) == 0

    try:
        async with engine.begin() as connection:
            await connection.run_sync(exercise)
    finally:
        await engine.dispose()
