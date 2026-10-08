"""Frozen research only; global conftest isolates DB, no production API/engine calls."""
from copy import deepcopy
from datetime import date, datetime
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.paper.research_reports import build_frozen_cycle_expectancy

START, END, AT = date(2026, 9, 1), date(2026, 9, 28), datetime(2026, 9, 28, 18, 11, 9)


def trade(tid, side, qty, price, day, version="old-v1", **extra):
    return dict(id=tid, account_id=9, code="600001", trade_type=side,
                amount=qty, price=price, commission=5., tax=1. if side == "sell" else 0.,
                trade_time=f"2026-09-{day:02d} 10:00:00", strategy_version=version,
                forced_probe=False, excluded_from_performance=False,
                realized_pnl=0. if side == "sell" else None, **extra)


def evidence(trades=None, positions=None):
    trades = trades if trades is not None else [
        trade(1, "buy", 200, 10, 1), trade(2, "sell", 100, 11, 2),
        trade(3, "sell", 100, 11, 3)]
    positions = positions or []
    cash = 5000 + sum((1 if t["trade_type"] == "sell" else -1) * t["price"] * t["amount"]
                      - t["commission"] - t["tax"] for t in trades)
    assets = cash + sum(p["buy_amount"] * p["current_price"] for p in positions if not p["is_closed"])
    return dict(as_of_at="2026-09-28T18:11:08+08:00",
                accounts=[dict(id=9, account_name="challenger_c", initial_capital=5000.,
                               current_capital=cash, total_assets=assets, status="active")],
                trades=trades, positions=positions)


def build(data=None, **kwargs):
    return build_frozen_cycle_expectancy(data if data is not None else evidence(),
        account_ids=kwargs.get("account_ids", [9]), start_date=kwargs.get("start_date", START),
        end_date=kwargs.get("end_date", END), as_of=kwargs.get("as_of", AT))


def test_partial_sells_are_one_complete_cycle_not_independent_wins(monkeypatch):
    from sqlalchemy.engine import Engine
    monkeypatch.setattr(Engine, "connect", lambda *a, **k: pytest.fail("offline connected"))
    data = evidence()
    before = deepcopy(data)
    r = build(data)
    assert data == before
    assert r["summary"]["eligible_window_cycles"] == 1
    s = r["strata"][0]
    assert (s["closed_cycles"], s["wins"], s["net_pnl"], s["expectancy_yuan"]) == (1, 1, 183., 183.)
    assert r["accounts"][0]["cycles"][0]["sell_trade_ids"] == [2, 3]
    assert not s["positive_expectancy_proven"] and not r["production_permission"]
    assert r["mode_expectancy"]["unknown_cycle_count"] == 1
    assert r["contract"]["fixed_T1_return"] is None


def test_scale_in_after_partial_sale_reuses_weighted_actual_cashflows():
    ts = [trade(1, "buy", 200, 10, 1), trade(2, "sell", 100, 11, 2),
          trade(3, "buy", 100, 12, 3), trade(4, "sell", 200, 13, 4)]
    r = build(evidence(ts))
    assert r["strata"][0]["net_pnl"] == 478
    assert r["accounts"][0]["cycles"][0]["buy_trade_ids"] == [1, 3]


@pytest.mark.parametrize("flag", ["forced_probe", "excluded_from_performance"])
def test_exclusion_on_any_leg_excludes_whole_cycle_but_preserves_denominator(flag):
    data = evidence()
    data["trades"][1][flag] = True
    r = build(data)
    assert r["accounts"][0]["excluded_complete_cycles"] == 1
    assert r["summary"]["input_trades_in_scope"] == 3
    assert r["summary"]["eligible_window_cycles"] == 0
    assert r["accounts"][0]["statistics"]["net_pnl"] is None


@pytest.mark.parametrize("bad", [None, "false", 1.0, 2])
def test_missing_or_malformed_exclusion_flags_make_account_unavailable(bad):
    data = evidence()
    data["trades"][0]["forced_probe"] = bad
    r = build(data)
    assert r["summary"]["unavailable_accounts"] == 1
    assert r["accounts"][0]["input_trade_count"] == 3
    assert r["accounts"][0]["statistics"] is None


def test_current_policy_not_used_historical_versions_and_unknown_remain(monkeypatch):
    from app.api.v1 import paper
    monkeypatch.setattr(paper, "_strategy_version", lambda *a: pytest.fail("current version read"))
    data = evidence()
    data["trades"][0]["strategy_version"] = None
    data["trades"][1]["strategy_version"] = "later-v2"
    r = build(data)
    assert r["strata"][0]["strategy_versions"] == ["UNKNOWN", "later-v2", "old-v1"]
    assert r["accounts"][0]["cycles"][0]["version_scope"] == "mixed_or_legacy_unknown"
    assert not r["contract"]["current_configuration_used"]


def test_open_inventory_is_censored_and_not_zero_loss():
    ts = [trade(1, "buy", 100, 10, 1)]
    ps = [dict(id=5, account_id=9, code="600001", buy_amount=100, buy_price=10.,
               current_price=9., is_closed=False)]
    r = build(evidence(ts, ps))
    assert r["accounts"][0]["open_positions"] == 1
    assert r["summary"]["eligible_window_cycles"] == 0
    assert r["accounts"][0]["statistics"]["win_rate_pct"] is None
    assert r["strata"][0]["right_censored_inventory_cycles"] == 1
    assert r["strata"][0]["closed_cycles"] == 0
    assert r["strata"][0]["net_pnl"] is None
    assert r["accounts"][0]["open_cycles"][0]["realized_complete_cycle_pnl"] is None


@pytest.mark.parametrize("kind", ["missing_buy", "bad_fee", "cash_mismatch", "future"])
def test_broken_basis_never_silently_drops_bad_account(kind):
    data = evidence()
    if kind == "missing_buy": data["trades"].pop(0)
    elif kind == "bad_fee": data["trades"][0]["commission"] = None
    elif kind == "cash_mismatch": data["accounts"][0]["current_capital"] += 1
    else: data["trades"][1]["trade_time"] = "2026-09-29 10:00:00"
    r = build(data)
    assert r["summary"]["unavailable_accounts"] == 1
    assert r["accounts"][0]["input_trade_count"] == len(data["trades"])
    assert r["accounts"][0]["statistics"] is None


@pytest.mark.parametrize("key", ["accounts", "trades", "positions"])
def test_missing_lists_fail_explicitly(key):
    data = evidence()
    del data[key]
    with pytest.raises(ValueError, match="list missing"):
        build(data)


def test_duplicate_trade_id_fails_not_deduplicated():
    data = evidence()
    data["trades"].append(deepcopy(data["trades"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        build(data)


def test_empty_missing_accounts_and_closed_window_denominators():
    r = build(evidence([], []), account_ids=[9, 10])
    assert r["summary"]["evaluated_accounts"] == 1
    assert r["summary"]["unavailable_accounts"] == 1
    assert r["accounts"][0]["statistics"]["sample_status"] == "no_completed_cycles"
    r = build(start_date=date(2026, 9, 4))
    assert r["accounts"][0]["closed_before_window"] == 1
    assert r["summary"]["eligible_window_cycles"] == 0
    r = build(start_date=date(2026, 9, 2))
    assert r["accounts"][0]["cycles"][0]["entry_before_requested_window"]


def test_input_hash_includes_unavailable_and_unknown_data_and_tail_is_descriptive():
    r = build()
    data = evidence()
    data["unsupported_intraday_markouts"] = {"5": 8, "15": 9, "30": 10}
    r2 = build(data)
    assert r["input_sha256"] != r2["input_sha256"]
    assert r["strata"] == r2["strata"]
    assert not r2["contract"]["intraday_5_15_30min_is_T1_net_return"]
    assert r2["sustained_shape_hypothesis"]["status"] == "not_tested"


def test_mode_not_inferred_from_confirmed_text():
    data = evidence()
    data["trades"][0]["reason"] = "C2 sustained shape confirmed profitable"
    r = build(data)
    assert r["mode_expectancy"]["status"] == "unavailable"
    assert r["accounts"][0]["cycles"][0]["mode"] == "unknown"


def test_frozen_cli_has_no_database_and_refuses_overwrite(tmp_path, monkeypatch):
    import socket
    from sqlalchemy.engine import Engine
    path = Path(__file__).resolve().parents[1] / "scripts/paper_signal_research.py"
    spec = importlib.util.spec_from_file_location("frozen_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(Engine, "connect", lambda *a, **k: pytest.fail("DB opened"))
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network opened"))
    source = tmp_path / "evidence.json"
    source.write_text(json.dumps(evidence()))
    args = SimpleNamespace(output=tmp_path / "outputs/new/result.json", as_of=AT,
        account_ids=[9], database=None, frozen_evidence=source, start=START, end=END)
    before = source.read_bytes()
    result = module.run_frozen(args)
    assert result["database_opened"] is False
    assert source.read_bytes() == before
    doc = json.loads(args.output.read_text())
    assert doc["strata"][0]["net_pnl"] == 183
    with pytest.raises(ValueError, match="new JSON"):
        module.run_frozen(args)
    args.output = tmp_path / "outputs/other.json"
    args.database = Path("must-not-open.db")
    with pytest.raises(ValueError, match="mutually exclusive"):
        module.run_frozen(args)
    import asyncio
    args.frozen_evidence = None
    with pytest.raises(ValueError, match="require --frozen-evidence"):
        asyncio.run(module.run(args))
