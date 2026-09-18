"""锁住 09:25 判定脚本：必须只读，且判定标准写在脚本里（不靠人记）。"""
from __future__ import annotations

from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2]
          / "scripts" / "check_auction_evidence_after_0925.py")


def test_read_only_and_no_state_change():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "mode=ro" in body
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "commit()"):
        assert forbidden not in body, forbidden


def test_reuses_production_health_function_instead_of_recomputing():
    """契约判定必须复用 get_snapshot_health，不得自己再算一套。"""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "auction_collector.get_snapshot_health" in body


def test_four_pre_registered_criteria_are_documented():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "双来源成立" in body
    assert "单来源" in body
    assert "invalid_clock" in body and "unverified_basis" in body
    assert "疑似被限频" in body
    assert "实跑前已固定" in body, "必须声明判定标准是预先固定的"


def test_reports_the_gate_numerator_explicitly():
    """必须报出闸门分子 multi_frame_complete_count 与每只的帧数分布。"""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "multi_frame_complete_count" in body
    assert "COUNT(DISTINCT source_quote_at)" in body
    assert ">=2" in body


def test_reports_actionable_attribution():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "prediction_not_actionable_reasons" in body
