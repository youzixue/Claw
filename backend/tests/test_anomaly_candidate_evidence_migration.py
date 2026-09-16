"""Real 031->032 Alembic, confined to tiny temporary SQLite databases."""
import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys

import pytest
import sqlalchemy as sa

from app.models.signal import AnomalyCandidateRecord, AnomalyCandidateEvidence


@pytest.mark.asyncio
@pytest.mark.parametrize("already_exists", [False, True])
async def test_031_to_032_preserves_old_projection_and_any_existing_evidence(tmp_path, already_exists):
    dbpath = (tmp_path / "before_032.db").resolve()
    assert dbpath.is_relative_to(tmp_path.resolve())
    engine = sa.create_engine(f"sqlite:///{dbpath}")
    at = datetime(2026, 9, 14, 10)
    try:
        with engine.begin() as conn:
            AnomalyCandidateRecord.__table__.create(conn)
            conn.execute(AnomalyCandidateRecord.__table__.insert().values(
                record_id="legacy", trade_date=at.date(), code="600001", identity="legacy-identity",
                first_seen_at=at, last_seen_at=at, snapshot_json='{"legacy_reason":"preserve"}'))
            before = conn.execute(sa.text("SELECT * FROM anomaly_candidate_record")).all()
            conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY NOT NULL)"))
            conn.execute(sa.text("INSERT INTO alembic_version VALUES ('031_paper_sale_accounting')"))
            prior_evidence = []
            if already_exists:
                AnomalyCandidateEvidence.__table__.create(conn)
                for action in ("update", "delete", "replace"):
                    conn.execute(sa.text(f"DROP TRIGGER anomaly_candidate_evidence_no_{action}"))
                conn.execute(AnomalyCandidateEvidence.__table__.insert().values(
                    id=1, capture_id="prior", record_id="legacy", trade_date=at.date(), code="600001",
                    captured_at=at, protocol_version="fixture", payload_hash="fixture",
                    payload_json='{"original_fixture":true}'))
                prior_evidence = conn.execute(sa.text("SELECT * FROM anomaly_candidate_evidence")).all()
        engine.dispose()
        for _ in range(2):
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-B", "-m", "alembic", "upgrade", "032_anomaly_candidate_evidence",
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{dbpath}",
                     "PYTHONDONTWRITEBYTECODE": "1", "CLAW_DISABLE_SCHEDULER": "1", "SQL_ECHO": "false"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), timeout=30)
            except BaseException:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
                raise
            assert child.returncode == 0, (stdout + stderr).decode()
        with engine.begin() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "032_anomaly_candidate_evidence"
            assert conn.execute(sa.text("SELECT * FROM anomaly_candidate_record")).all() == before
            assert conn.execute(sa.text("SELECT * FROM anomaly_candidate_evidence")).all() == prior_evidence
            triggers = set(conn.execute(sa.text("SELECT name FROM sqlite_master WHERE type='trigger'")).scalars())
            assert triggers == {f"anomaly_candidate_evidence_no_{x}" for x in ("update", "delete", "replace")}
            if not already_exists:
                conn.execute(AnomalyCandidateEvidence.__table__.insert().values(
                    id=1, capture_id="new", record_id="legacy", trade_date=at.date(), code="600001",
                    captured_at=at, protocol_version="fixture", payload_hash="fixture", payload_json="{}"))
        for sql in (
            "UPDATE anomaly_candidate_evidence SET payload_json='changed'",
            "DELETE FROM anomaly_candidate_evidence",
            "INSERT OR REPLACE INTO anomaly_candidate_evidence SELECT * FROM anomaly_candidate_evidence",
        ):
            with engine.connect() as conn:
                with pytest.raises(sa.exc.IntegrityError, match="append-only"):
                    conn.execute(sa.text(sql))
                conn.rollback()
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT * FROM anomaly_candidate_record")).all() == before
    finally:
        engine.dispose()
