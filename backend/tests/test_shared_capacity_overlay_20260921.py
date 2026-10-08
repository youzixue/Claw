"""Offline shared 50k accounting; fixtures are not historic market returns."""
from dataclasses import replace
from datetime import time, timedelta
import pytest
from test_paper_capacity_research import at, scope, signal, obs, SHA, PASS
from app.paper.capacity_research import SharedCapacityBudget, CapacityPolicy, run_shared_capacity_study, ARMS

POLICY = CapacityPolicy(fixed_times=(time(9,35),), reserve_until=time(9,35))

def fixture():
    a = scope(available_cash=50000, per_order_cash_limit=50000, max_positions=4, max_daily_buys=4)
    b = replace(a, account_id=3, account_name="mainline", strategy_version="other-v1")
    sa = signal("a", quantity=3000)
    sb = signal("b", "600002", account_id=3, account_name="mainline", strategy_version="other-v1", quantity=3000)
    budget = SharedCapacityBudget(a.trade_date, a.observed_at, a.window_end, 50000, 4, 4, 0, (), SHA)
    return budget, (a,b), (sa,sb), (obs(sa),obs(sb))

def run(parts, **kwargs):
    b,s,signals,rows = parts
    return run_shared_capacity_study(b,s,signals,rows,as_of=at(9,35),policy=POLICY,**kwargs)

def test_one_50000_not_twelve_budgets_preserves_displaced_and_no_pnl():
    report = run(fixture())
    for result in report["arms"].values():
        assert result["signal_count"] == 2
        assert result["selected_count"] == result["displaced_count"] == 1
        assert result["reserved_cash"] == 30009  # 30,000 × 0.0003 exceeds the 5-yuan minimum.
        assert result["unreserved_cash"] == 19991
        assert result["selected"][0]["signal_id"] == "a"
        assert result["displaced"][0]["signal_id"] == "b"
        assert result["displaced"][0]["status"] == "shared_available_cash_insufficient"
        assert result["executable_net_pnl"] is None
    assert report["winner"] is None
    assert report["contract"]["production_policy_changed"] is False

@pytest.mark.parametrize("guard", list(PASS.__dataclass_fields__))
@pytest.mark.parametrize("value", [False,None])
def test_failed_and_unknown_are_not_replaced_with_fictitious_valid_signals(guard,value):
    b,s,signals,rows = fixture()
    rows = (replace(rows[0], guards=replace(PASS, **{guard:value})), rows[1])
    report = run((b,s,signals,rows))
    for arm in ARMS:
        r = report["arms"][arm]
        assert r["selected"][0]["signal_id"] == "b"
        assert len(r["independent_excluded"]) == 1
        assert r["independent_excluded"][0]["signal_id"] == "a"
        assert r["signal_count"] == 2

def test_same_code_across_accounts_gets_one_reservation_not_duplicate_exposure():
    b,s,signals,rows = fixture()
    signals = (signals[0], replace(signals[1], code=signals[0].code))
    report = run((b,s,signals,(obs(signals[0]),obs(signals[1]))))
    assert report["arms"]["first_come"]["displaced"][0]["status"] == "shared_held_or_reserved_code"

def test_slot_limit_and_original_whole_lots_are_preserved():
    b,s,signals,rows = fixture()
    b = replace(b, max_positions=1)
    report = run((b,s,signals,rows))
    assert report["arms"]["first_come"]["displaced"][0]["status"] == "shared_position_or_daily_capacity_full"
    assert report["arms"]["first_come"]["selected"][0]["quantity"] == 3000

def test_future_high_score_cannot_rewrite_past_allocation_or_hash():
    parts = fixture()
    base = run(parts)
    b,s,signals,rows = parts
    future = replace(signals[1], signal_id="future", code="600003", confirmed_at=at(13), priority_at=at(13), priority_score=1e9)
    result = run((b,s,(*signals,future),(*rows,obs(future))))
    assert result == base

def test_scores_from_different_strategies_do_not_override_frozen_chronology():
    b,s,signals,rows = fixture()
    signals = (signals[0], replace(signals[1], priority_score=1e9))
    assert run((b,s,signals,rows))["arms"]["fixed_times"]["selected"][0]["signal_id"] == "a"

@pytest.mark.parametrize("bad", ["missing_scope","different_window","duplicate","invalid_budget","before"])
def test_incomplete_or_inconsistent_inputs_fail_closed(bad):
    b,s,signals,rows = fixture()
    with pytest.raises(ValueError):
        if bad == "missing_scope": s = s[:1]
        if bad == "different_window": s = (s[0],replace(s[1],observed_at=at(9,31)))
        if bad == "duplicate": s = (s[0],s[0])
        if bad == "invalid_budget": b = replace(b,available_cash=float("nan"))
        if bad == "before":
            run_shared_capacity_study(b,s,signals,rows,as_of=at(9,29),policy=POLICY)
        else: run((b,s,signals,rows))

def test_existing_cli_accepts_shared_input_and_refuses_outcome_fields():
    import json
    from dataclasses import asdict
    from scripts.paper_capacity_research import decode_shared_input
    b,s,signals,rows = fixture()
    def plain(value):
        return json.loads(json.dumps(value, default=lambda v:v.isoformat()))
    accounts = [{"schema":"paper_capacity_frozen_input_v1", "scope":plain(asdict(scope)),
        "signals":[plain(asdict(signal))], "observations":[plain(asdict(row))],
        "policy":plain(asdict(POLICY)), "as_of":at(9,35).isoformat()}
        for scope,signal,row in zip(s,signals,rows)]
    payload = {"schema":"paper_shared_capacity_frozen_input_v1",
               "budget":plain(asdict(b)), "accounts":accounts}
    decoded = decode_shared_input(payload)
    r = run_shared_capacity_study(*decoded[:4],as_of=decoded[4],policy=decoded[5])
    assert r["signal_count"] == 2
    with pytest.raises(ValueError):
        decode_shared_input({**payload,"today_winner_weights":{"default":1}})
    accounts[1]["as_of"] = at(9,36).isoformat()
    with pytest.raises(ValueError):
        decode_shared_input(payload)


def test_no_signals_keeps_entire_budget_and_reports_no_winner():
    b,s,_,_ = fixture()
    r = run((b,s,(),()))
    assert all(a["signal_count"] == 0 and a["unreserved_cash"] == 50000 for a in r["arms"].values())
