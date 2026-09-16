"""F1 diagnostic coverage without changing entry decisions; isolated database only."""
import json
from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog
from app.models.stock import LimitUpPool, StockKline, StockTag
from test_paper_api import paper_client
from test_paper_buy_point_hooks import clock_and_guards

SIGNAL = date(2026, 9, 7)
CFG = {"min_consecutive": 3, "max_gap_days": 1, "min_dip_pct": -5.0, "min_vol_ratio": 1.5}


def bars(code="600111"):
    values = [
        ("2026-08-31",8,8,0,8,8,100000),
        ("2026-09-01",8.8,8,10,8.8,8.8,100000),
        ("2026-09-02",9.68,8.8,10,9.68,9.68,100000),
        ("2026-09-03",10.65,9.68,10,10.65,10.65,100000),
        ("2026-09-04",9.9,10.65,-7.04,9.9,9.9,100000),
        ("2026-09-07",10.89,9.9,10,10.1,10.1,200000),
    ]
    return [SimpleNamespace(code=code, trade_date=date.fromisoformat(day), close=cl,
        prev_close=prev, change_pct=change, open=op, high=cl, low=low, volume=volume)
        for day, cl, prev, change, op, low, volume in values]


@pytest.mark.parametrize("case,reason,stage", [
    ("short","reversal_history_insufficient","data_gate"),
    ("stale","reversal_signal_bar_missing","data_gate"),
    ("not_sealed","reversal_signal_not_sealed","strategy_filter"),
    ("still_sealed","reversal_no_break","strategy_filter"),
    ("no_prior","reversal_prior_limit_missing","strategy_filter"),
    ("boards","reversal_prior_boards_below_min","strategy_filter"),
    ("gap","reversal_gap_outside_window","strategy_filter"),
    ("anchor","reversal_dip_evidence_missing","data_gate"),
    ("low_missing","reversal_dip_evidence_missing","data_gate"),
    ("dip","reversal_dip_not_deep_enough","strategy_filter"),
    ("signal_volume","reversal_volume_missing","data_gate"),
    ("prior_volume","reversal_volume_missing","data_gate"),
    ("volume","reversal_volume_below_min","strategy_filter"),
    ("open_missing","reversal_signal_open_missing","data_gate"),
    ("open_at_limit","reversal_signal_open_at_limit","strategy_filter"),
])
def test_pattern_reports_first_failure_without_changing_result(case, reason, stage):
    hist, cfg, at = bars(), dict(CFG), SIGNAL
    if case == "short": hist = hist[:3]
    elif case == "stale": at = date(2026,9,8)
    elif case == "not_sealed": hist[-1].close, hist[-1].change_pct = 10.1, 2.0
    elif case == "still_sealed": hist[-2].change_pct = 10.0
    elif case == "no_prior":
        for row in hist[:-1]: row.close, row.prev_close, row.change_pct = 8,8,0
    elif case == "boards": cfg["min_consecutive"] = 4
    elif case == "gap": cfg["max_gap_days"] = 0
    elif case == "anchor": hist[-3].close = 0
    elif case == "low_missing": hist[-2].low = None
    elif case == "dip": hist[-2].low = 10.5
    elif case == "signal_volume": hist[-1].volume = None
    elif case == "prior_volume": hist[0].volume = float("nan")
    elif case == "volume": hist[-1].volume = 120000
    elif case == "open_missing": hist[-1].open = None
    elif case == "open_at_limit": hist[-1].open = 10.89
    before = deepcopy(hist)
    diagnostics = []
    assert paper._reversal_kline_pattern(hist, at, cfg) is None
    assert paper._reversal_kline_pattern(hist, at, cfg, diagnostics=diagnostics) is None
    assert len(diagnostics) == 1
    assert diagnostics[0]["reason_code"] == reason
    assert diagnostics[0]["stage_code"] == stage
    assert [vars(x).keys() for x in hist] == [vars(x).keys() for x in before]
    json.dumps(diagnostics, allow_nan=False)


def test_positive_shape_unchanged_future_bar_ignored_and_no_false_rejection():
    hist = bars()
    baseline = paper._reversal_kline_pattern(hist, SIGNAL, CFG)
    future = deepcopy(hist[-1]); future.trade_date = date(2026,9,8); future.volume = None
    diagnostics = []
    actual = paper._reversal_kline_pattern(hist+[future], SIGNAL, CFG, diagnostics=diagnostics)
    assert actual == baseline and actual["consec_before"] == 3
    assert actual["vol_ratio"] == 2.0 and not diagnostics


def test_zero_is_a_measured_missing_history_count_not_unknown():
    diagnostics = []
    paper._reversal_kline_pattern([], SIGNAL, CFG, diagnostics=diagnostics)
    assert diagnostics[0]["metric_value"] == 0
    assert diagnostics[0]["threshold_value"] == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected", [
    ("ok",None),("quote","quote_missing"),("change_missing","change_missing"),
    ("change","entry_change_above_max"),("vwap_missing","vwap_missing"),
    ("vwap","below_vwap"),("high_missing","high_missing"),
    ("pullback","high_pullback_above_max"),("limit_missing","limit_price_missing"),
    ("at_limit","reversal_current_at_limit"),("st","reversal_identity_blocked"),
])
async def test_live_filter_first_cause_and_unchanged_candidates(paper_client, monkeypatch, case, expected):
    _, maker = paper_client
    monkeypatch.setattr(paper.settings, "PAPER_REVERSAL_ENABLED", True)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=SIGNAL))
    monkeypatch.setattr(paper.settings, "PAPER_REVERSAL_MAX_INTRADAY_CONFIRM_CHANGE_PCT", 3.0)
    monkeypatch.setattr(paper.settings, "PAPER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT", 2.0)
    monkeypatch.setattr(paper.settings, "PAPER_REVERSAL_REQUIRE_ABOVE_VWAP", True)
    spot = SimpleNamespace(code="600111",name="隔离",price=10.5,change_pct=1.,
                           avg_price=10.4,high=10.6,limit_up=11.98)
    if case == "quote": spot = None
    elif case == "change_missing": spot.change_pct = None
    elif case == "change": spot.change_pct = 3.01
    elif case == "vwap_missing": spot.avg_price = None
    elif case == "vwap": spot.avg_price = 10.6
    elif case == "high_missing": spot.high = None
    elif case == "pullback": spot.high = 12
    elif case == "limit_missing": spot.limit_up = 0
    elif case == "at_limit": spot.limit_up = 10.5
    monkeypatch.setattr(paper, "_spot_by_code", AsyncMock(return_value=spot))
    async with maker() as db:
        db.add(LimitUpPool(code="600111",name="隔离",trade_date=SIGNAL,quarantined=False))
        db.add(StockTag(code="600111",name="隔离",board_type="main_sh",board_tag="tradeable",
                       is_st=case=="st"))
        db.add_all([StockKline(**vars(row)) for row in bars()])
        await db.flush()
        baseline = await paper._reversal_pullback_candidates(db,5,date(2026,9,8))
        diagnostics = []
        actual = await paper._reversal_pullback_candidates(db,5,date(2026,9,8),diagnostics=diagnostics)
        assert actual == baseline
        if expected:
            assert not actual[0] and len(diagnostics)==1
            assert diagnostics[0]["reason_code"] == expected
            assert diagnostics[0]["candidate"]["signal_date"] == "2026-09-07"
            assert diagnostics[0]["candidate"]["history_quality"] == "not_pit_certified_by_pattern_check"
        else:
            assert len(actual[0]) == 1 and diagnostics == []


@pytest.mark.asyncio
async def test_f_actual_loop_persists_each_stock_not_only_three_notes(paper_client, monkeypatch):
    _, maker = paper_client
    at, send, submit = clock_and_guards(monkeypatch)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=SIGNAL))
    monkeypatch.setattr(paper, "_record_control_sample", AsyncMock(return_value=None))
    token = paper._QUOTE_ROUND_CONTEXT.set({
        "round_id":"f-diagnostics", "as_of_at":at, "committed_at":at,
        "records":[], "quality_status":"ok", "code_version":"test-reversal-diag",
    })
    try:
        async with maker() as db:
            db.add_all([LimitUpPool(code=f"600{i:03}",name=f"缺历史{i}",trade_date=SIGNAL,
                                   seal_amount=1e8+i,quarantined=False) for i in range(55)])
            await db.commit()
            await paper.run_paper_auto_trade(db, account_name="reversal", now=at,
                execute=True, execution_mode="intraday", include_position_risk=False)
            logs = list((await db.scalars(select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.action == "candidate_reject"))).all())
            assert len(logs)==55 and len({x.code for x in logs})==55
            assert all(x.reason_code=="reversal_history_insufficient" and x.stage_code=="data_gate" for x in logs)
            assert all(x.quote_round_id=="f-diagnostics" and x.executed_trade_id is None for x in logs)
            assert all(x.metric_value==0 and x.threshold_value==6 for x in logs)
            assert all(json.loads(x.candidate_json)["diagnostic_scope"]=="first_rejection_per_stock" for x in logs)
        assert not send.called and not submit.called
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
