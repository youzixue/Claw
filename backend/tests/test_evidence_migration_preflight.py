"""Simulate existing 027 tables lacking triggers; never connect to the live DB."""
import asyncio
from datetime import datetime
import hashlib
import os
from pathlib import Path
import sys

import pytest
import sqlalchemy as sa

from app.models.news import FinanceNews, NewsContentVersion, NewsAnalysisVersion
from app.models.factor import FactorEvaluationRun


@pytest.mark.asyncio
async def test_real_027_to_029_preserves_existing_evidence_and_installs_trigger_guards(tmp_path):
    dbpath = (tmp_path / "existing_027.db").resolve()
    assert dbpath.is_relative_to(tmp_path.resolve())
    engine = sa.create_engine(f"sqlite:///{dbpath}")
    at = datetime(2026, 9, 14, 16, 30)
    tables = (FinanceNews, NewsContentVersion, NewsAnalysisVersion, FactorEvaluationRun)
    try:
        with engine.begin() as conn:
            for model in tables:
                model.__table__.create(conn)
            for table in (NewsContentVersion, NewsAnalysisVersion):
                for action in ("update", "delete"):
                    conn.execute(sa.text(f"DROP TRIGGER {table.__tablename__}_no_{action}"))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES (\'027_fund_order_breakdown\')"))
            conn.execute(FinanceNews.__table__.insert().values(
                id=1, source="cls", source_id="fixture", title="原文保留", publish_time=at))
            conn.execute(NewsContentVersion.__table__.insert().values(
                id=1, news_id=1, content_hash="a" * 64, source="cls", publish_time=at,
                first_received_at=at, received_at=at, content_available_at=at, recorded_at=at,
                origin="observed", payload_json="{\"original\":\"不可改写\"}",
                entity_evidence_json="[]", entity_verified_at=at, protocol_version="news_pit_v1"))
            encoded = "{\"summary\":\"原始分析\"}"
            conn.execute(NewsAnalysisVersion.__table__.insert().values(
                id=1, content_version_id=1, status="analyzed", analysis_completed_at=at,
                available_at=at, result_json=encoded, result_hash=hashlib.sha256(encoded.encode()).hexdigest(),
                protocol_version="news_pit_v1"))
            conn.execute(FactorEvaluationRun.__table__.insert().values(
                id=1, factor_name="fixture", eval_date=at.date(), as_of_at=at,
                protocol_version="fixture-research", input_hash="b" * 64,
                input_json="{\"original\":1}", result_json="{\"original\":2}", created_at=at))
            before = {model.__tablename__: conn.execute(sa.text("SELECT * FROM " + model.__tablename__)).all()
                      for model in tables}
        engine.dispose()
        for _ in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "alembic", "upgrade", "029_news_evidence_versions",
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{dbpath}",
                     "PYTHONDONTWRITEBYTECODE": "1", "CLAW_DISABLE_SCHEDULER": "1", "SQL_ECHO": "false"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, (stdout + stderr).decode()
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "029_news_evidence_versions"
            for model in tables:
                assert conn.execute(sa.text("SELECT * FROM " + model.__tablename__)).all() == before[model.__tablename__]
            triggers = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type=\'trigger\'")).scalars().all()
            assert sorted(triggers) == sorted(f"{table.__tablename__}_no_{action}"
                for table in (NewsContentVersion, NewsAnalysisVersion) for action in ("update", "delete"))
        for table in (NewsContentVersion, NewsAnalysisVersion):
            for statement in (f"DELETE FROM {table.__tablename__} WHERE id=1",
                              f"UPDATE {table.__tablename__} SET protocol_version=\'tampered\' WHERE id=1"):
                with engine.connect() as conn:
                    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
                        conn.execute(sa.text(statement))
                    conn.rollback()
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("table_already_exists", [False, True])
async def test_029_to_030_retains_prices_and_repairs_existing_table_triggers(tmp_path, table_already_exists):
    from app.models.stock import StockKline, StockKlineObservation
    dbpath = (tmp_path / "existing_029.db").resolve()
    engine = sa.create_engine(f"sqlite:///{dbpath}")
    at = datetime(2026, 9, 14, 18, 0)
    try:
        with engine.begin() as conn:
            StockKline.__table__.create(conn)
            conn.execute(StockKline.__table__.insert().values(
                id=17, code="600127", trade_date=at.date(), source="ths",
                open=9.7, high=10.3, low=9.6, close=10.3, prev_close=9.7, change_pct=6.19))
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('029_news_evidence_versions')"))
            before = conn.execute(sa.text("SELECT * FROM stock_kline")).all()
            if table_already_exists:
                StockKlineObservation.__table__.create(conn)
                for action in ("update", "delete"):
                    conn.execute(sa.text(f"DROP TRIGGER stock_kline_observation_no_{action}"))
                conn.execute(StockKlineObservation.__table__.insert().values(
                    id=1, code="600127", trade_date=at.date(), origin="legacy_projection",
                    disposition="preserved_original", payload_json='{"original":true}',
                    payload_hash="c" * 64, recorded_at=at, available_at=None,
                    source_version="legacy_unknown", price_basis="legacy_unknown",
                    quality_issues_json="[]", protocol_version="kline_observation_v1"))
                before_observations = conn.execute(sa.text("SELECT * FROM stock_kline_observation")).all()
        engine.dispose()
        for _ in range(2):
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "alembic", "upgrade", "030_kline_observations",
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{dbpath}",
                     "PYTHONDONTWRITEBYTECODE": "1", "CLAW_DISABLE_SCHEDULER": "1", "SQL_ECHO": "false"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0, (stdout + stderr).decode()
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "030_kline_observations"
            assert conn.execute(sa.text("SELECT * FROM stock_kline")).all() == before
            observations = conn.execute(sa.text("SELECT * FROM stock_kline_observation")).all()
            assert observations == (before_observations if table_already_exists else [])
            triggers = conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars().all()
            assert sorted(triggers) == ["stock_kline_observation_no_delete", "stock_kline_observation_no_update"]
        if table_already_exists:
            for action in ("DELETE FROM stock_kline_observation",
                           "UPDATE stock_kline_observation SET source_version='fake'"):
                with engine.connect() as conn:
                    with pytest.raises(sa.exc.IntegrityError, match="append-only"):
                        conn.execute(sa.text(action))
                    conn.rollback()
    finally:
        engine.dispose()
