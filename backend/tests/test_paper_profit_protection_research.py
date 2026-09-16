"""Pure fixtures only; no production DB, calendar fetch, collector or trading."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config.settings import settings
from app.paper.exit_audit import advance_extrema
from app.paper.profit_protection_research import ProfitProtectionPolicy, build_profit_protection_report

BUY = datetime(2026, 9, 8, 14, 59)
ASOF = datetime(2026, 9, 9, 10)
POLICY = ProfitProtectionPolicy("research:protect-v1", 4, 2)


def position(**kwargs):
    identity = {"position_id": 1, "account_id": 8, "account_name": "tenbagger",
        "code": "600001", "buy_time": BUY.isoformat(), "strategy_version": "old-v1",
        "position_revision": "lot-1"}
    return {**identity, "frozen_at": BUY.isoformat(), "buy_price": 10, "buy_amount": 200,
        "stop_loss_price": 9.4, "price_basis": "raw:action-revision-1",
        "position_ref": "frozen-position:1", "is_closed": False,
        "entry_policy": {**identity, "basis": "frozen_entry_order", "missing_keys": [],
            "order_id": "o1", "first_buy_trade_id": 1, "evidence_ref": "entry:o1",
            "observed_at": (BUY-timedelta(seconds=30)).isoformat(),
            "exit_parameters": {"stop_loss_pct": 6, "take_profit_pct": 18, "max_hold_days": 3}},
        **kwargs}


def sample(at=BUY, price=10, **kwargs):
    p = position()
    identity = {k:p[k] for k in ("position_id", "account_id", "account_name", "code",
        "buy_time", "strategy_version", "position_revision")}
    return {**identity, "sample_id": at.isoformat(), "source": "tencent",
        "source_quote_at": at.isoformat(), "received_at": at.isoformat(), "observed_at": at.isoformat(),
        "quote_round_id": "q:"+at.isoformat(), "evidence_ref": "quote:"+at.isoformat(),
        "price": price, "price_basis": p["price_basis"], "basis_ref": "daily-actions:none",
        "corporate_action_status": "none", "buy_price": 10, "buy_amount": 200,
        "position_state_at": at.isoformat(), "sellable_quantity": 200,
        "quantity_ref": "ledger:available", "position_active": True,
        "original_exit_trigger": False, "original_exit_ref": "frozen-exit-check",
        **kwargs}


def calendar():
    return [{"trade_date": day, "is_trade_day": True, "registered_at": "2026-09-01T00:00:00",
             "evidence_ref": "calendar:"+day} for day in ("2026-09-08", "2026-09-09")]


def build(rows, p=None, cal=None, policy=POLICY, as_of=ASOF):
    return build_profit_protection_report([p or position()], rows, calendar() if cal is None else cal,
                                         as_of=as_of, policies=[policy])


def result(value):
    return value["results"][0]["comparisons"][0]


def test_two_highs_and_t_plus_one_independent_from_alleged_sellable_quantity():
    rows = [sample(BUY, 11), sample(BUY+timedelta(seconds=30), 10.7),
            sample(datetime(2026, 9, 9, 9, 30), 10.4), sample(datetime(2026, 9, 9, 9, 30, 30), 10.1)]
    value = result(build(rows))
    a, b, c, d = value["observations"]
    assert b["research"]["post_entry"]["trigger"] == "true"
    assert b["execution_block"] == "t_plus_one"
    assert b["legal_quantity_gate"] == "false"
    assert b["research"]["post_sellable"]["observed_high"] is None
    assert c["research"]["post_entry"]["observed_high"] == 11
    assert c["research"]["post_sellable"]["observed_high"] == 10.4
    assert c["research"]["post_sellable"]["trigger"] == "unknown"
    assert d["research"]["post_sellable"]["trigger"] == "true"
    assert d["research"]["post_sellable"]["legal_research_alert"] == "true"
    assert all(not r["order_generated"] and r["executable_net_return"] is None for r in value["observations"])


def test_new_policy_and_current_settings_do_not_change_frozen_sl_tp_or_position(monkeypatch):
    p, rows = position(), [sample(BUY, 11)]
    before = deepcopy((p, rows))
    a = build(rows, p)
    monkeypatch.setattr(settings, "PAPER_AUTO_TAKE_PROFIT_PCT", 99)
    assert build(rows, p) == a
    b = build(rows, p, policy=replace(POLICY, version="research:other", activation_profit_pct=6))
    assert b["data_hash"] != a["data_hash"]
    assert b["results"][0]["frozen_position"] == a["results"][0]["frozen_position"]
    assert (p, rows) == before


@pytest.mark.parametrize("fault", ["account", "position", "version", "buy_time", "revision", "cost", "amount",
    "basis", "corporate_action", "source", "reference", "source_clock", "receive_clock", "state_clock",
    "bool_price", "nan_price", "negative", "unknown_active"])
def test_invalid_sample_unknown_never_fabricates_extrema(fault):
    row = sample()
    key, value = {
        "account": ("account_id",9), "position":("position_id",2), "version":("strategy_version","new"),
        "buy_time":("buy_time","2026-09-08T10:00:00"), "revision":("position_revision","lot-2"),
        "cost":("buy_price",9), "amount":("buy_amount",300), "basis":("price_basis","adjusted"),
        "corporate_action":("corporate_action_status","unknown"), "source":("source","legacy"),
        "reference":("evidence_ref",[]), "source_clock":("source_quote_at",None),
        "receive_clock":("received_at",(BUY-timedelta(seconds=1)).isoformat()),
        "state_clock":("position_state_at",(BUY-timedelta(seconds=1)).isoformat()),
        "bool_price":("price",True), "nan_price":("price",float("nan")),
        "negative":("price",-1), "unknown_active":("position_active",None)}[fault]
    row[key] = value
    value = result(build([row]))
    assert value["extrema"]["post_entry"] is None
    assert value["observations"] == []


@pytest.mark.parametrize("fault", ["missing", "partial", "version", "order", "future", "tp", "maxhold"])
def test_frozen_exit_policy_must_be_complete_no_live_defaults(fault):
    p = position()
    e = p["entry_policy"]
    if fault == "missing": p.pop("entry_policy")
    elif fault == "partial": e["missing_keys"] = ["take_profit_pct"]
    elif fault == "version": e["strategy_version"] = "new"
    elif fault == "order": e["order_id"] = None
    elif fault == "future": e["observed_at"] = ASOF.isoformat()
    elif fault == "tp": e["exit_parameters"].pop("take_profit_pct")
    else: e["exit_parameters"]["max_hold_days"] = True
    assert build([], p)["results"][0]["status"] == "unknown"


def test_registered_calendar_missing_late_or_conflicting_no_weekday_fallback():
    rows = [sample(datetime(2026,9,9,9,30),11)]
    for cal in ([], [calendar()[1]], [*calendar(), calendar()[0]],
                [dict(r, registered_at="2026-09-10T00:00:00") for r in calendar()]):
        value = result(build(rows, cal=cal))
        assert value["first_sellable_observation_at"] is None
        assert all(r["legal_quantity_gate"] == "unknown" for r in value["observations"])


def test_holiday_calendar_requires_explicit_closed_rows_and_rejects_false_registration():
    buy = datetime(2026,9,24,14,59)
    p = position(buy_time=buy.isoformat(), frozen_at=buy.isoformat())
    p["entry_policy"]["buy_time"] = buy.isoformat()
    p["entry_policy"]["observed_at"] = (buy-timedelta(seconds=1)).isoformat()
    at = datetime(2026,9,28,9,30)
    row = sample(at, 11, buy_time=buy.isoformat())
    cal = [{"trade_date":f"2026-09-{d}", "is_trade_day":d in (24,28),
            "registered_at":"2026-09-01T00:00:00", "evidence_ref":f"cal:{d}"} for d in range(24,29)]
    value = result(build([row], p, cal, as_of=at))
    assert value["observations"][0]["calendar_first_sell_day"] == "2026-09-28"
    assert value["observations"][0]["legal_quantity_gate"] == "true"
    assert result(build([row], p, cal[:1]+cal[2:], as_of=at))["observations"][0]["legal_quantity_gate"] == "unknown"
    holiday = dict(row, source_quote_at="2026-09-25T10:00:00", received_at="2026-09-25T10:00:00",
                   observed_at="2026-09-25T10:00:00", position_state_at="2026-09-25T10:00:00")
    cal[1]["is_trade_day"] = True
    assert result(build([holiday], p, cal, as_of=at))["observations"] == []


@pytest.mark.parametrize("quantity", [None, True, -1, 201, "200", 99, 0])
def test_legal_date_does_not_replace_quantity_evidence(quantity):
    at = datetime(2026,9,9,9,30)
    r = result(build([sample(at,11,sellable_quantity=quantity)]))["observations"][0]
    assert r["legal_quantity_gate"] != "true"
    assert r["research"]["post_sellable"]["observed_high"] is None


def test_first_sellable_quantity_clock_does_not_backfill_source_peak():
    at = datetime(2026,9,9,9,30)
    row = sample(at,11, observed_at=(at+timedelta(seconds=2)).isoformat())
    value = result(build([row, sample(at+timedelta(seconds=30),10.4)]))
    assert value["first_sellable_observation_at"] == "2026-09-09T09:30:02"
    assert value["extrema"]["post_sellable"]["post_entry_high"] == 10.4


def test_daily_high_ignored_and_same_pure_extrema_result():
    row = sample(BUY,10.2, high=99)
    value = result(build([row]))
    p = position()
    pos = SimpleNamespace(id=1, account_id=8, code="600001", buy_time=BUY, buy_price=10, strategy_version="old-v1")
    expected, _ = advance_extrema(None, position=pos,
        spot=SimpleNamespace(price=10.2, source_quote_at=BUY, received_at=BUY, quote_round_id=row["quote_round_id"]),
        observed_at=BUY, quote_ok=True, max_age_sec=90)
    assert value["extrema"]["post_entry"] == expected
    assert value["extrema"]["post_entry"]["post_entry_high"] == 10.2


def test_future_quotes_no_trigger_effect_and_missing_frame_no_legal_alert():
    rows = [sample(BUY,11), sample(datetime(2026,9,9,9,35),10.4)]
    a = result(build(rows))
    future = sample(datetime(2026,9,9,11),99)
    b = result(build(rows+[future]))
    assert a["observations"] == b["observations"]
    assert a["extrema"] == b["extrema"]
    assert b["rejection_counts"]["future_observation_excluded"] == 1
    assert a["historical_gap_or_invalid"]
    assert a["observations"][-1]["research"]["post_entry"]["trigger"] == "true"
    assert a["observations"][-1]["research"]["post_entry"]["legal_research_alert"] == "unknown"


def test_out_of_order_duplicate_and_conflicting_source():
    rows = [sample(BUY,11), sample(BUY+timedelta(seconds=30),10.5)]
    assert result(build(rows)) == result(build(list(reversed(rows))))
    assert result(build([rows[0], rows[0], rows[1]]))["observations"] == result(build(rows))["observations"]
    conflict = dict(rows[0], price=99, observed_at=(BUY+timedelta(seconds=1)).isoformat())
    value = result(build([rows[0], conflict, rows[1]]))
    assert value["extrema"]["post_entry"]["post_entry_high"] == 11
    assert value["historical_gap_or_invalid"]


def test_closed_position_not_resurrected_and_original_trigger_independent():
    at = datetime(2026,9,9,9,30)
    rows = [sample(at,11,original_exit_trigger=True),
            sample(at+timedelta(seconds=30),position_active=False),
            sample(at+timedelta(seconds=60),99)]
    value = result(build(rows))
    assert len(value["observations"]) == 1
    assert value["observations"][0]["original_exit_trigger"] == "true"
    assert value["position_terminated"]
    assert value["extrema"]["post_entry"]["post_entry_high"] == 11


def test_reused_sample_round_identity_and_stale_observation_are_rejected():
    at = BUY+timedelta(seconds=30)
    for extra in ({"sample_id":BUY.isoformat()}, {"quote_round_id":"q:"+BUY.isoformat()},
                  {"observed_at":(at+timedelta(seconds=91)).isoformat()}):
        value = result(build([sample(BUY,11),sample(at,99,**extra)]))
        assert value["extrema"]["post_entry"]["post_entry_high"] == 11
        assert value["rejected_evidence"]


def test_lunch_is_not_a_missing_trading_frame():
    p = position(buy_time="2026-09-08T10:00:00", frozen_at="2026-09-08T10:00:00")
    p["entry_policy"]["buy_time"] = p["buy_time"]
    p["entry_policy"]["observed_at"] = "2026-09-08T09:59:00"
    rows = [sample(datetime(2026,9,9,11,29,30),11,buy_time=p["buy_time"]),
            sample(datetime(2026,9,9,13),10.5,buy_time=p["buy_time"])]
    value = result(build(rows,p,as_of=datetime(2026,9,9,13)))
    assert not value["historical_gap_or_invalid"]
    assert value["observations"][-1]["research"]["post_sellable"]["legal_research_alert"] == "true"


def test_late_calendar_registration_never_backfills_earlier_alert():
    rows = [sample(datetime(2026,9,9,9,30),11),sample(datetime(2026,9,9,9,30,30),10.5)]
    cal = calendar()
    cal[1]["registered_at"] = "2026-09-09T09:31:00"
    value = result(build(rows,cal=cal))
    assert value["observations"] == []
    assert value["first_sellable_observation_at"] is None


def test_prefix_and_unknown_numeric_hashes_keep_evidence_identity():
    a = build([sample(price=None)])
    b = build([sample(price=float("nan"))])
    assert a["data_hash"] != b["data_hash"]
    rows = [sample(BUY,11),sample(BUY+timedelta(seconds=30),10.5)]
    prefix = result(build(rows,as_of=BUY+timedelta(seconds=30)))
    future = sample(datetime(2026,9,9,10),99)
    replay = result(build(rows+[future],as_of=BUY+timedelta(seconds=30)))
    assert prefix["observations"] == replay["observations"]
    assert prefix["extrema"] == replay["extrema"]


def test_empty_and_duplicate_positions_keep_denominators():
    assert result(build([]))["extrema"] == {"post_entry":None,"post_sellable":None}
    value = build_profit_protection_report([position(),position(),None], [], [], as_of=ASOF, policies=[POLICY])
    assert len(value["results"]) == 3
    assert all(r["status"] == "unknown" for r in value["results"])


@pytest.mark.parametrize("change", [
    {"version":"production:v1"},{"activation_profit_pct":True},{"activation_profit_pct":0},
    {"pullback_from_peak_pct":float("nan")},{"max_sample_gap_sec":False},{"max_quote_age_sec":0}])
def test_explicit_research_policy_validation(change):
    with pytest.raises(ValueError):
        replace(POLICY,**change)
