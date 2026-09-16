"""Research CLI cannot mutate the business database or register a direction model."""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url

from test_promotion_ledger_dataset import env, seed, truth, CUTOFF


def cli_module():
    spec = importlib.util.spec_from_file_location("direction_cli_under_test",
        Path(__file__).resolve().parents[1] / "scripts" / "train_promotion_challenger.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_original_training_cli_defaults_remain_compatible(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["train"])
    args = cli_module().parse_args()
    assert args.objective == "promotion"
    assert args.dataset_source == "historical_panel" and not args.persist


@pytest.mark.parametrize("extra", [[], ["--persist"], ["--target-board", "2"],
    ["--dataset-source", "historical_panel"], ["--as-of", "2026-09-08T20:00:00+08:00"],
    ["--as-of", "2100-01-01T20:00:00"]])
def test_direction_cli_rejects_unsafe_modes(monkeypatch, extra):
    argv = ["train", "--objective", "next_day_close_up", "--dataset-source", "prediction_snapshots"]
    if extra:
        argv += ["--as-of", CUTOFF.isoformat()] + extra
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli_module().parse_args()
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_direction_cli_is_physically_readonly_and_does_not_backfill_legacy(env, monkeypatch):
    db, path = env
    await seed(db)
    await truth(db)
    await db.close()
    before = path.read_bytes()
    monkeypatch.setattr("app.db.session.engine", SimpleNamespace(url=make_url(f"sqlite+aiosqlite:///{path}")))
    module = cli_module()
    args = SimpleNamespace(objective="next_day_close_up", as_of=CUTOFF,
        start_date=None, end_date=None, snapshot_context="promotion_2000", initial_train_days=25,
        validation_days=5, step_days=5, calibration_days=5)
    statements = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())
    event.listen(Engine, "before_cursor_execute", observe)
    try:
        result = await module._run(args)
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    assert result["status"] == "insufficient_evidence" and result["target_met"] is None
    assert not result["persisted"] and not result["manual_review_eligible"]
    assert path.read_bytes() == before
    assert set(statements) <= {"SELECT", "PRAGMA", "BEGIN"}


@pytest.mark.asyncio
async def test_direction_cli_refuses_missing_database(monkeypatch, tmp_path):
    path = tmp_path / "not-created.db"
    monkeypatch.setattr("app.db.session.engine", SimpleNamespace(url=make_url(f"sqlite+aiosqlite:///{path}")))
    with pytest.raises(ValueError, match="does not exist"):
        await cli_module()._run(SimpleNamespace(objective="next_day_close_up"))
    assert not path.exists()
