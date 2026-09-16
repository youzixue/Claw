"""Full-schema TEMPORARY release fixtures; never import production settings URLs."""
from datetime import date
from pathlib import Path

import sqlalchemy as sa

from app.db.session import Base
from app.models.stock import StockDaily
from scripts.audit_deployment_evidence import audit_database
from scripts.deployment_evidence_contract import TABLE_SINCE


def create_release_database(tmp_path, revision="030_kline_observations"):
    path = (tmp_path / "release.sqlite").resolve()
    assert path.is_relative_to(tmp_path.resolve())
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection, tables=[
                table for table in Base.metadata.sorted_tables
                if TABLE_SINCE.get(table.name, 27) <= int(revision[:3])
            ])
            # These pre030 migration indexes are not part of ORM metadata.
            # Include the actual022/024 baseline, rather than allowing unexpected
            # init_db DDL through the release checker to make a fixture pass.
            connection.execute(sa.text("CREATE UNIQUE INDEX uq_trade_order_idempotency_key "
                                       "ON trade_order (idempotency_key)"))
            connection.execute(sa.text("CREATE UNIQUE INDEX uq_paper_account_active_name "
                                       "ON paper_account (account_name) WHERE status = 'active'"))
            connection.execute(sa.text("CREATE TABLE alembic_version "
                                       "(version_num VARCHAR(32) PRIMARY KEY NOT NULL)"))
            connection.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision})
            connection.execute(StockDaily.__table__.insert().values(
                id=1, code="600001", trade_date=date(2026, 9, 14), close=9))
    finally:
        engine.dispose()
    return path


def release_pair(tmp_path, target="030_kline_observations"):
    path = create_release_database(tmp_path)
    before = audit_database(path)
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(connection, tables=[
                table for table in Base.metadata.sorted_tables
                if TABLE_SINCE.get(table.name, 27) <= int(target[:3])
            ])
            connection.execute(sa.text("UPDATE alembic_version SET version_num=:v"), {"v": target})
    finally:
        engine.dispose()
    return before, audit_database(path)
