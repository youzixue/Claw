"""全市场覆盖通过不等于每一只股票可成交；异常时钟始终保留并逐股失败关闭。"""
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from app.api.v1 import paper
from app.config.settings import settings
from app.data.quote_round import build_quote_round_record, quote_round_continuity


def _records(at, count, stale_count=0):
    return [{"code": f"{600000+i}", "price": 10, "prev_close": 10,
             "source_quote_at": at - timedelta(hours=4) if i < stale_count else at,
             "received_at": at} for i in range(count)]


def test_small_stale_subset_does_not_close_other_accounts_but_cannot_fill():
    at = datetime(2026, 9, 8, 13, 10)
    records = _records(at, 100, 1)
    meta = build_quote_round_record(records, expected_count=100, committed_at=at)
    assert meta["quality_status"] == "ok"
    assert meta["source_min_at"] == at - timedelta(hours=4)
    assert meta["as_of_at"] == at
    quality = json.loads(meta["component_watermarks_json"])["source_quote_quality"]
    assert quality["fresh_count"] == 99 and quality["stale_or_future_count"] == 1
    token = paper._QUOTE_ROUND_CONTEXT.set({**meta, "records": records,
        "records_by_code": {row["code"]: row for row in records}})
    try:
        valid, reason = paper._execution_quote_status(SimpleNamespace(**records[0]), at.date(), now=at)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert not valid and ("source_quote_at" in reason or "源" in reason)


def test_insufficient_fresh_coverage_still_blocks_whole_round():
    at = datetime(2026, 9, 8, 13, 10)
    meta = build_quote_round_record(_records(at, 100, 6), expected_count=100, committed_at=at)
    assert meta["quality_status"] == "degraded"
    assert "fresh_source_coverage" in meta["quality_reason"]


def test_one_future_clock_does_not_shift_decision_asof_but_remains_audited():
    at = datetime(2026, 9, 8, 13, 10)
    records = _records(at, 100)
    records[0]["source_quote_at"] = at + timedelta(days=1)
    meta = build_quote_round_record(records, expected_count=100, committed_at=at)
    assert meta["quality_status"] == "ok" and meta["as_of_at"] == at
    assert meta["source_max_at"] == at + timedelta(days=1)


def test_zero_fresh_and_missing_coverage_are_not_accepted():
    at = datetime(2026, 9, 8, 13, 10)
    for rows in ([], _records(at, 100, 100)):
        meta = build_quote_round_record(rows, expected_count=100, committed_at=at)
        assert meta["quality_status"] == "degraded"


@pytest.mark.parametrize("seconds,status", [(30, "continuous"), (90, "continuous"), (93, "gap"), (307, "gap")])
def test_fresh_snapshot_reports_prior_gap_without_disabling_position_risk(monkeypatch, seconds, status):
    monkeypatch.setattr(settings, "PAPER_MOMENTUM_RETEST_MAX_QUOTE_GAP_SEC", 90)
    at = datetime(2026, 9, 14, 11, 6, 52)
    meta = build_quote_round_record(
        _records(at, 100), expected_count=100, committed_at=at,
        previous_committed_at=at - timedelta(seconds=seconds),
    )
    assert meta["quality_status"] == "ok"
    continuity = json.loads(meta["component_watermarks_json"])["source_quote_continuity"]
    assert continuity["status"] == status
    assert continuity["active_gap_sec"] == seconds
    assert continuity["max_gap_sec"] == 90
    assert continuity["per_stock_clock_check_required"] is True


@pytest.mark.parametrize("previous,current", [
    (datetime(2026, 9, 14, 11, 29, 44), datetime(2026, 9, 14, 13, 0, 14)),
    (datetime(2026, 9, 14, 9, 24, 44), datetime(2026, 9, 14, 9, 30, 14)),
])
def test_planned_session_break_does_not_masquerade_as_collection_gap(previous, current):
    evidence = quote_round_continuity(previous, current)
    assert evidence["status"] == "continuous"
    assert evidence["active_gap_sec"] == 30
    assert evidence["wall_gap_sec"] > evidence["active_gap_sec"]


def test_missing_previous_round_is_unknown_not_proof_of_continuity():
    at = datetime(2026, 9, 14, 9, 30, 14)
    evidence = quote_round_continuity(None, at)
    assert evidence["status"] == "unknown"
    assert evidence["active_gap_sec"] is None


def test_new_date_does_not_bridge_overnight_or_claim_continuity():
    evidence = quote_round_continuity(
        datetime(2026, 9, 11, 15, 0), datetime(2026, 9, 14, 9, 30, 14),
    )
    assert evidence["status"] == "new_trade_date"
    assert evidence["active_gap_sec"] is None


@pytest.mark.parametrize("offset", [0, 1])
def test_repeated_or_future_previous_clock_cannot_claim_continuity(offset):
    at = datetime(2026, 9, 14, 10, 0)
    evidence = quote_round_continuity(at + timedelta(seconds=offset), at)
    assert evidence["status"] == "invalid_clock"
    assert evidence["active_gap_sec"] is None
