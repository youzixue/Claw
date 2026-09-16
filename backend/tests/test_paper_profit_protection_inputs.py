"""Only temporary SQLite/archives. Missing facts remain blocked, not synthetic."""
import asyncio
from copy import deepcopy
from datetime import date, datetime
import json
import threading

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.session import Base
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.models.stock import QuoteRound
from app.models.governance import TradeCalendarModel
from app.paper import profit_protection_inputs as m
from app.paper.profit_protection_research import ProfitProtectionPolicy

DAY = date(2026, 9, 8)
BUY = datetime(2026, 9, 8, 10)
ASOF = datetime(2026, 9, 8, 14)
POLICIES = [ProfitProtectionPolicy("research:test", 5, 2)]


def evidence():
    p = dict(id=1, account_id=7, code="600000", buy_time=BUY, buy_price=10.,
             buy_amount=100, strategy_version="old-v1", stop_loss_price=9., is_closed=False)
    t = dict(id=11, account_id=7, code="600000", trade_type="buy", price=10.,
             amount=100, trade_time=BUY, commission=5., tax=0., strategy_version="old-v1",
             decision_round_id="decision", fill_round_id="fill", forced_probe=False,
             excluded_from_performance=False)
    f = dict(id=12, fill_id="f", order_id="o", broker="paper", broker_trade_id="11",
             code="600000", side="buy", price=10., quantity=100, commission=5.,
             tax=0., filled_at=BUY, decision_round_id="decision", fill_round_id="fill")
    e = dict(account_id=7, account_name="default", position_id=1, strategy_version="old-v1",
             observed_at=BUY.isoformat(), exit_parameters=dict(
                 stop_loss_pct=10, take_profit_pct=20, max_hold_days=3))
    o = dict(order_id="o", broker="paper", account_id="default", code="600000",
             side="buy", price=10., quantity=100, created_at=BUY,
             strategy_version="old-v1", decision_round_id="decision",
             risk_json=json.dumps({"experiment_entry": e}))
    return p, t, f, o


@pytest.mark.parametrize("raw", [
    "{}", '{"experiment_entry":false}', '{"experiment_entry":{"observed_at":false}}',
    '{"experiment_entry":{"exit_parameters":{"max_hold_days":true}}}',
])
def test_dirty_frozen_policy_unknown(raw):
    p, t, f, o = evidence()
    o["risk_json"] = raw
    a = m._audit(p, "default", [t], [(f, o)])
    assert a["status"] == "blocked"
    assert "frozen_exit_policy_incomplete" in a["reasons"]


def test_reconciled_visible_entry_is_not_historical_replay():
    p, t, f, o = evidence()
    before = deepcopy((p, t, f, o))
    a = m._audit(p, "default", [t], [(f, o)])
    assert a["status"] == "blocked"
    assert a["constant_lot_consistency"] is True
    assert a["actual_entry_fees"] == 5  # not 10
    assert a["execution_permission"] is a["cost_after_executable_return"] is None
    assert (p, t, f, o) == before
    assert set(m.BLOCKERS) <= set(a["reasons"])


@pytest.mark.parametrize("target,key,value,reason", [
    ("o", "account_id", "other", "order_account_name_conflict"),
    ("f", "quantity", 200, "fill_identity_conflict"),
    ("f", "commission", 6, "fill_price_or_fee_conflict"),
    ("f", "fill_round_id", "wrong", "fill_round_unproven"),
    ("o", "decision_round_id", "", "decision_round_unproven"),
    ("p", "strategy_version", "new-v2", "current_position_entry_conflict"),
    ("p", "is_closed", True, "current_position_entry_conflict"),
    ("t", "forced_probe", True, "forced_or_excluded_entry"),
    ("o", "risk_json", "[]", "frozen_exit_policy_incomplete"),
    ("o", "risk_json", "{bad", "frozen_exit_policy_incomplete"),
])
def test_bad_visible_evidence_preserved(target, key, value, reason):
    p, t, f, o = evidence()
    {"p": p, "t": t, "f": f, "o": o}[target][key] = value
    a = m._audit(p, "default", [t], [(f, o)])
    assert reason in a["reasons"]
    assert a["status"] == "blocked"
    assert a["constant_lot_consistency"] is None


@pytest.mark.parametrize("change", ["add", "trim", "missing", "duplicate"])
def test_non_constant_or_ambiguous_evidence(change):
    p, t, f, o = evidence()
    trades, pairs = [t], [(f, o)]
    if change in ("add", "trim"):
        trades.append(dict(t, id=22, trade_type="buy" if change == "add" else "sell"))
    elif change == "missing":
        pairs = []
    else:
        pairs *= 2
    a = m._audit(p, "default", trades, pairs)
    assert a["constant_lot_consistency"] is None
    assert a["actual_entry_fees"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("archive_state", ["ready", "pending", "bad_path", "degraded", "missing_code", "empty"])
async def test_sqlite_readonly_batch_worker_and_blocked_contract(tmp_path, monkeypatch, archive_state):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "test.db"))
    tables = [PaperAccount.__table__, PaperPosition.__table__, PaperTradeLog.__table__,
              TradeFill.__table__, TradeOrder.__table__, QuoteRound.__table__, TradeCalendarModel.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    p, t, f, o = evidence()
    path = tmp_path / "compact" / ("trade_date=" + DAY.isoformat()) / "q.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame([dict(code="600001" if archive_state == "missing_code" else "600000",
        price=11., prev_close=10., source_quote_at=BUY, received_at=BUY,
        committed_at=BUY, quote_round_id="q", high=999.)]).to_parquet(path)
    async with factory() as db:
        db.add(PaperAccount(id=7, account_name="default", initial_capital=10000))
        db.add(PaperAccount(id=8, account_name="default", initial_capital=10000))
        if archive_state != "empty":
            db.add(PaperPosition(**p))
            db.add(PaperTradeLog(**t))
            db.add(TradeFill(**f, trade_date=DAY))
            db.add(TradeOrder(**o, trade_date=DAY, order_type="limit", status="filled"))
        db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
        db.add(QuoteRound(round_id="q", trade_date=DAY, committed_at=BUY, as_of_at=BUY,
            source="tencent", expected_count=1, received_count=1, config_version="c",
            code_version="v", quality_status="degraded" if archive_state == "degraded" else "ok",
            archive_status="pending" if archive_state == "pending" else "ready",
            archive_path=str(tmp_path / "outside.parquet") if archive_state == "bad_path" else str(path)))
        await db.commit()
    main = threading.get_ident()
    real = m.load_quote_archive
    calls = []
    def worker(refs, codes, **kwargs):
        assert threading.get_ident() != main
        assert all(type(r) is m.QuoteArchiveRef for r in refs)
        calls.append(len(refs))
        return real(refs, codes, **kwargs)
    monkeypatch.setattr(m, "load_quote_archive", worker)
    try:
        async with factory() as db:
            await db.execute(text("PRAGMA query_only=ON"))
            await db.execute(text("BEGIN"))
            # Must not autoflush caller's pending business object.
            db.add(PaperAccount(account_name="not-written", initial_capital=1))
            result = await m.build_profit_protection_report(db, start_date=DAY, end_date=DAY,
                as_of=ASOF, archive_root=tmp_path, policies=POLICIES)
            assert result["status"] == ("empty" if archive_state == "empty" else "blocked")
            assert result["positions"] == result["samples"] == result["calendar"] == []
            assert result["frozen_report"]["results"] == []
            assert result["current_calendar_projection"][0].get("registered_at") is None
            assert len(result["position_audits"]) == (0 if archive_state == "empty" else 1)
            assert len(calls) == 1
            if archive_state != "empty":
                assert result["archive_manifest"]
            assert (await db.execute(text("select count(*) from paper_account"))).scalar() == 2
            repeated = await m.build_profit_protection_inputs(db, start_date=DAY, end_date=DAY,
                as_of=ASOF, archive_root=tmp_path, policies=POLICIES)
            assert repeated["data_hash"] == result["data_hash"]
            if result["position_audits"]:
                assert result["position_audits"][0]["current_position"]["account_id"] == 7
            await db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("start,end,asof,policies", [
    (DAY, date(2026, 9, 7), ASOF, POLICIES),
    (DAY, date(2026, 9, 9), ASOF, POLICIES),
    (DAY, DAY, ASOF, []),
    (DAY, DAY, ASOF.replace(tzinfo=__import__("datetime").timezone.utc), POLICIES),
])
async def test_invalid_boundaries_before_database(start, end, asof, policies):
    with pytest.raises(ValueError):
        await m.build_profit_protection_inputs(None, start_date=start, end_date=end,
            as_of=asof, policies=policies)
