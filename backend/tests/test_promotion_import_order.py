"""Public research modules must import in a fresh interpreter in any order."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("first", [
    "app.promotion.shadow",
    "app.promotion.identity_evidence",
    "app.promotion.outcome_materials",
    "app.promotion.modeling.ledger_dataset",
    "app.promotion.modeling.daily_materials",
    "app.promotion.modeling.training",
    "app.data.source_capture",
    "app.data.sources.eastmoney_source",
    "app.data.sources.ths_kline_source",
    "app.paper.position_observation",
])
def test_independent_import_without_circular_bootstrap(first, tmp_path):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           "DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'unused.db'}"}
    script = (
        "import importlib; "
        f"importlib.import_module({first!r}); "
        "from app.promotion.shadow import _is_completed_outcome_date as old; "
        "from app.promotion.outcome_evidence import _is_completed_outcome_date as shared; "
        "assert old is shared; "
        "from app.promotion.modeling import train_promotion_challenger; "
        "assert callable(train_promotion_challenger)"
    )
    result = subprocess.run([sys.executable, "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "unused.db").exists()
