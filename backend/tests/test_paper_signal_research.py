"""只用隔离SQLite；研究收益不生成订单，不调用真实网络。"""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db.session import Base
from app.models.paper import PaperAccount, PaperAutoTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.models.governance import TradeCalendarModel
from app.paper import signal_research as research
from test_paper_buy_points import setup, record, logs

DAY = date(2026, 9, 8)
AT = datetime(2026, 9, 8, 10)


def signal(**changes):
    return {"code": "600001", "trade_date": DAY.isoformat(), "reference_price": 10.,
            "evidence_status": "valid", "queue_order": False, **changes}


def sample_bars():
    return {("600001", DAY + timedelta(days=i)): SimpleNamespace(
        close=10+i, prev_close=9+i, source="tencent_close", high=1000, low=.01)
        for i in range(4)}


def markout(*, value=None, horizon=1, bars=None, calendar=None, at=datetime(2026, 9, 11, 16)):
    return research.horizon_markout(value or signal(), horizon=horizon,
        bars=sample_bars() if bars is None else bars,
        calendar={DAY+timedelta(days=i): True for i in range(4)} if calendar is None else calendar,
        as_of=at, policy=research.MarkoutPolicy())


@pytest.mark.parametrize("kwargs", [
    {"quantity": 1}, {"quantity": 150}, {"quantity": 0}, {"quantity": True},
    {"commission_rate": True}, {"commission_rate": float("nan")},
    {"minimum_commission": -1}, {"stamp_tax_rate": 1}, {"slippage_pct": 100},
    {"commission_rate": "0.0003"},
])
def test_invalid_policy_rejected(kwargs):
    with pytest.raises(ValueError):
        research.MarkoutPolicy(**kwargs)


def test_costs_apply_whole_lots_and_minimum_fee_each_side():
    result = markout()
    assert result["status"] == "evaluated" and result["gross_return_pct"] == 10
    assert result["buy_cash"] == 1006
    assert result["sell_cash"] == 1092.8011
    assert result["executable_return"] is None and result["same_day_high_used"] is False
    assert research.MarkoutPolicy().contract()["version"] != research.MarkoutPolicy(quantity=200).contract()["version"]


@pytest.mark.parametrize("at,reason", [
    (AT, "horizon_not_elapsed"),
    (datetime(2026, 9, 9, 12), "horizon_close_not_final"),
])
def test_t_plus_one_right_censored_before_legal_result(at, reason):
    result = markout(at=at)
    assert result["status"] == "right_censored" and result["reason"] == reason
    assert result["net_return_pct"] is None


@pytest.mark.parametrize("kind,reason", [
    ("calendar", "calendar_gap"), ("missing_bar", "formal_bar_missing"),
    ("source", "formal_bar_missing"), ("chain", "price_chain_discontinuity"),
    ("bad_close", "invalid_close"),
])
def test_bad_endpoint_does_not_skip_to_later_bar(kind, reason):
    bars = sample_bars()
    calendar = {DAY+timedelta(days=i): True for i in range(4)}
    day = DAY+timedelta(days=1)
    if kind == "calendar": del calendar[day]
    elif kind == "missing_bar": del bars[("600001", day)]
    elif kind == "source": bars[("600001", day)].source = "spot_fallback"
    elif kind == "chain": bars[("600001", day)].prev_close = 5
    elif kind == "bad_close": bars[("600001", day)].close = float("nan")
    result = markout(horizon=3, bars=bars, calendar=calendar)
    assert result["reason"] == reason and result["net_return_pct"] is None


def test_known_closed_day_not_counted_but_missing_calendar_is_not_assumed_weekend():
    cal = {DAY: True, DAY+timedelta(days=1): False, DAY+timedelta(days=2): True}
    bars = sample_bars()
    bars[("600001", DAY+timedelta(days=2))].prev_close = 10
    result = markout(calendar=cal, bars=bars)
    assert result["exit_date"] == "2026-09-10" and result["gross_return_pct"] == 20


@pytest.mark.parametrize("value,reason", [
    (signal(queue_order=True), "limit_up_queue_fill_unproven"),
    (signal(evidence_status="unknown"), "invalid_signal_evidence"),
])
def test_unproven_queue_and_unknown_evidence_not_success(value, reason):
    assert markout(value=value)["reason"] == reason


def log_row(id=1, account_id=1, version="v1", code="600001", at=AT, **changes):
    payload = {"account_name": "default", "signal_observed_at": at.isoformat(),
               "signal_key": "key-"+str(id), "decision_run_id": "decision", "queue_order": False}
    values = dict(id=id, account_id=account_id, strategy_version=version, code=code,
        trade_date=at.date(), created_at=at, as_of_at=at, quote_round_id="round-1",
        source="test", run_id="signal-"+str(id), price=10, action="buy_signal",
        decision="confirmed", candidate_json=json.dumps(payload))
    values.update(changes)
    return PaperAutoTradeLog(**values)


def test_old_labels_remain_unknown_and_cannot_borrow_future_or_other_version():
    row = log_row()
    assert research.signal_record(row, "default")["labels_status"] == "unknown_not_backfilled"
    payload = json.loads(row.candidate_json)
    labels = {"schema": "signal_labels_v1", "status": "recorded", "strategy_version": "v1",
              "observed_at": AT.isoformat(), "regime": "trend", "bull_bear": "bull"}
    payload["signal_labels"] = labels
    row.candidate_json = json.dumps(payload)
    assert research.signal_record(row, "default")["bull_bear"] == "bull"
    for key, bad in [("strategy_version", "another"), ("observed_at", (AT+timedelta(seconds=1)).isoformat())]:
        payload["signal_labels"] = {**labels, key: bad}
        row.candidate_json = json.dumps(payload)
        assert research.signal_record(row, "default")["bull_bear"] == "unknown"


def test_legacy_v1_uses_record_reference_without_backfilling_confirmation_time():
    row = log_row()
    payload = json.loads(row.candidate_json)
    payload.pop("signal_observed_at")
    payload["notification_schema"] = "paper_buy_point_v1"
    row.candidate_json = json.dumps(payload)
    result = research.signal_record(row, "default")
    assert result["evidence_status"] == "legacy_record_reference"
    assert result["observed_at"] is None and result["reference_at"] == AT.isoformat()
    assert result["bull_bear"] == "unknown" and result["session"] == "AM"
    assert markout(value=result)["status"] == "evaluated"
    payload["notification_schema"] = "paper_buy_point_v2"
    row.candidate_json = json.dumps(payload)
    assert research.signal_record(row, "default")["evidence_status"] == "unknown"


@pytest.mark.parametrize("change", [
    {"created_at": AT-timedelta(seconds=1)}, {"as_of_at": AT+timedelta(seconds=1)},
    {"price": float("nan")}, {"quote_round_id": ""}, {"strategy_version": ""},
])
def test_invalid_signal_kept_in_denominator_but_never_evaluated(change):
    result = research.signal_record(log_row(**change), "default")
    assert result["evidence_status"] == "unknown"


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'research.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_empty_report_lists_all_accounts_without_creating_them(db):
    result = await research.build_signal_research_report(db, start_date=DAY, end_date=DAY, as_of=AT)
    assert len(result["accounts"]) == 12 and result["summary"]["confirmation_events"] == 0
    assert await db.scalar(select(func.count(PaperAccount.id))) == 0
    assert not db.new and not db.dirty


@pytest.mark.asyncio
async def test_all_events_and_versions_preserved_first_causal_decision_and_exact_fill(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), log_row(id=3, version="v2"), log_row(id=5, at=AT+timedelta(days=1))])
    db.add(log_row(id=2, action="wait_buy", decision="wait", run_id="decision",
                   created_at=AT-timedelta(seconds=1), reason="达到当日买入上限"))
    db.add(log_row(id=4, action="buy", decision="executed", run_id="decision", version="another"))
    db.add(TradeOrder(order_id="o1", account_id="default", broker="paper", code="600001",
        side="buy", source="test", strategy_version="v1", decision_round_id="round-1",
        decision_at=AT, as_of_at=AT, created_at=AT, trade_date=DAY,
        order_type="limit", quantity=100, price=10, status="filled"))
    db.add_all([TradeFill(fill_id="f1", order_id="o1", broker="paper", code="600001", side="buy",
        quantity=100, price=10, commission=5, filled_at=AT+timedelta(seconds=30)),
        TradeFill(fill_id="future", order_id="o1", broker="paper", code="600001", side="buy",
        quantity=100, price=10, filled_at=AT+timedelta(days=1))])
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
    await db.flush()
    result = await research.build_signal_research_report(db, start_date=DAY, end_date=DAY,
                                                        as_of=AT+timedelta(minutes=1))
    assert result["summary"]["confirmation_events"] == 2
    assert len(result["strata"]) == 2
    a, b = result["signals"]
    # A later numeric ID cannot make a pre-signal decision causal.
    assert a["first_decision_id"] is None and b["first_decision_id"] is None
    assert a["actual_fill_ids"] == ["f1"] and b["actual_fill_quantity"] == 0
    assert a["actual_fill_cash_including_fees"] == 1005
    assert a["bull_bear"] == b["bull_bear"] == "unknown"
    assert a["markouts"]["1"]["status"] == "right_censored"
    assert not db.new and not db.dirty
    fill = await db.scalar(select(TradeFill).where(TradeFill.fill_id == "f1"))
    fill.commission = None
    await db.flush()
    missing_fee = await research.build_signal_research_report(db, start_date=DAY, end_date=DAY,
                                                            as_of=AT+timedelta(minutes=1))
    assert missing_fee["signals"][0]["actual_fill_cost_status"] == "unknown"
    assert missing_fee["signals"][0]["actual_fill_cash_including_fees"] is None


@pytest.mark.asyncio
async def test_reporting_keeps_events_but_deduplicates_stock_day_sample(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add_all([log_row(), log_row(id=2)])
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
    await db.flush()
    result = await research.build_signal_research_report(db, start_date=DAY, end_date=DAY, as_of=AT)
    assert result["summary"]["confirmation_events"] == 2
    assert result["summary"]["first_account_version_day_codes"] == 1


@pytest.mark.asyncio
async def test_signal_capture_survives_notification_switch_and_never_back_sends(setup, monkeypatch):
    from app.config.settings import settings
    from app.push import paper_buy_points as points
    maker, now, send = setup
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", now.date().isoformat())
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", now.replace(hour=0, minute=0, second=0).isoformat())
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", False)
    monkeypatch.setattr(research, "freeze_signal_labels", AsyncMock(return_value={"status": "unknown"}))
    row = await record(maker, now)
    payload = json.loads(row.candidate_json)
    assert payload["research_capture_schema"] == "all_confirmed_v1"
    assert payload["notification_allowed"] is False
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "disabled"
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", True)
    await points.dispatch_buy_points(now=now+timedelta(seconds=1), session_factory=maker)
    assert not send.called and len(await logs(maker)) == 1


@pytest.mark.asyncio
async def test_label_audit_failure_or_version_mismatch_is_unknown(db, monkeypatch):
    monkeypatch.setattr(research, "freeze_entry_evidence", AsyncMock(side_effect=ValueError("private source detail")))
    result = await research.freeze_signal_labels(db, "default", "v1", at=AT)
    assert result["reason"] == "audit_unavailable" and result["error_type"] == "ValueError"
    assert "private" not in json.dumps(result)
    monkeypatch.setattr(research, "freeze_entry_evidence", AsyncMock(return_value={"strategy_version": "old"}))
    assert (await research.freeze_signal_labels(db, "default", "v1", at=AT))["reason"] == "strategy_version_mismatch"


@pytest.mark.asyncio
async def test_cli_uses_immutable_read_transaction_and_rejects_writes(db, tmp_path, monkeypatch):
    import importlib.util
    import hashlib
    from pathlib import Path
    from sqlalchemy import text
    script_path = Path(__file__).resolve().parents[1] / "scripts/paper_signal_research.py"
    spec = importlib.util.spec_from_file_location("signal_research_cli", script_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    (tmp_path / "outputs").mkdir()
    await db.commit()
    database = Path(db.bind.url.database)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    args = SimpleNamespace(database=database, start=DAY, end=DAY,
                           output=tmp_path / "outputs/report.json", quantity=100)
    await cli.run(args)
    result = json.loads(args.output.read_text())
    assert result["read_only"] is True and len(result["accounts"]) == 12
    assert result["parallel_schema"] == "signal_portfolio_parallel_v1"
    assert len(result["actual_portfolio"]["accounts"]) == 12
    assert result["comparison_contract"]["full_market_same_shape"]["sample_count"] is None
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    with pytest.raises(ValueError, match="new JSON"):
        await cli.run(args)
    args.output = database
    with pytest.raises(ValueError, match="new JSON"):
        await cli.run(args)
    async def attempt_write(connection, **kwargs):
        await connection.execute(text("DELETE FROM paper_account"))
    monkeypatch.setattr(research, "build_signal_research_report", attempt_write)
    args.output = tmp_path / "outputs/must-not-exist.json"
    with pytest.raises(Exception, match="readonly"):
        await cli.run(args)
    assert not args.output.exists()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("price", [0, -1, True, None, float("nan"), float("inf"), 10**400])
def test_direct_markout_invalid_reference_is_unknown_not_report_failure(price):
    result = markout(value=signal(reference_price=price))
    assert result["status"] == "unknown"
    assert result["reason"] == "invalid_reference_price"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("price", [1e308, 5e-324])
def test_finite_input_arithmetic_overflow_does_not_evaluate(price):
    result = markout(value=signal(reference_price=price))
    assert result["status"] == "unknown"
    assert result["reason"] == "non_finite_markout_arithmetic"
    assert result["net_return_pct"] is result["gross_return_pct"] is None
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("kwargs", [{"quantity": 10**400}, {"minimum_commission": 10**400}])
def test_unrepresentable_policy_number_is_value_error(kwargs):
    with pytest.raises(ValueError):
        research.MarkoutPolicy(**kwargs)


def test_huge_integer_scalar_is_missing_not_overflow():
    assert research._number(10**400) is None


def test_allowlisted_close_is_observed_projection_not_finality_certificate():
    bars = sample_bars()
    result = markout(bars=bars)
    assert result["status"] == "evaluated"  # descriptive arithmetic remains available
    assert result["outcome_evidence_grade"] == "current_projection_unverified"
    assert result["promotion_evidence_eligible"] is False
    assert result["close_observations"] == [
        {"code": "600001", "trade_date": "2026-09-08", "source": "tencent_close",
         "close": 10., "prev_close": 9.},
        {"code": "600001", "trade_date": "2026-09-09", "source": "tencent_close",
         "close": 11., "prev_close": 10.},
    ]
    # The report owns the consumed leaf values, not references to mutable ORM rows.
    bars[("600001", DAY+timedelta(days=1))].close = 11.5
    changed = markout(bars=bars)
    assert result["reference_exit_close"] == result["close_observations"][-1]["close"] == 11
    assert changed["close_observations"][-1]["close"] == 11.5
    assert changed["outcome_evidence_grade"] == result["outcome_evidence_grade"]
    contract = research.MarkoutPolicy().contract()
    assert contract["exit"] == "source_allowlisted_horizon_close_minus_slippage"
    evidence = contract["outcome_evidence_contract"]
    assert evidence["supplier_finality_verified"] is False
    assert evidence["price_basis_verified"] is False
    assert evidence["historical_first_availability_verified"] is False


def test_initial_day_unused_previous_close_is_not_new_requirement():
    bars = sample_bars()
    del bars[("600001", DAY)].prev_close
    result = markout(bars=bars)
    assert result["status"] == "evaluated"
    assert result["close_observations"][0]["prev_close"] is None


def test_observed_source_identity_is_kept_even_when_price_is_unchanged():
    first = markout()
    bars = sample_bars()
    bars[("600001", DAY+timedelta(days=1))].source = "ths"
    second = markout(bars=bars)
    assert first["net_return_pct"] == second["net_return_pct"]
    assert first["close_observations"] != second["close_observations"]
    assert second["promotion_evidence_eligible"] is False


def test_unknown_or_censored_markout_does_not_gain_certification():
    for result in (markout(at=AT), markout(bars={}), markout(value=signal(evidence_status="unknown"))):
        assert result["promotion_evidence_eligible"] is False
        assert result["close_observations"] == []
        assert result["net_return_pct"] is None


def test_stats_large_finite_returns_do_not_overflow_mean():
    rows = []
    for i in range(2):
        value = {"status": "evaluated", "net_return_pct": 1e308,
                 "outcome_evidence_grade": "current_projection_unverified",
                 "promotion_evidence_eligible": False}
        rows.append({"account_id": 1, "strategy_version": "v1", "trade_date": DAY.isoformat(),
                     "code": str(i), "actual_fill_quantity": 0, "first_decision_state": "unknown",
                     "capture_contract": "test", "markouts": {"1": value, "3": value}})
    summary = research._stats(rows)
    for horizon in ("1", "3"):
        assert summary["horizons"][horizon]["mean_net_markout_pct"] == 1e308
        assert summary["horizons"][horizon]["certified_evaluated_count"] == 0
        assert summary["horizons"][horizon]["outcome_evidence_grades"] == {"current_projection_unverified": 2}
    json.dumps(summary, allow_nan=False)


@pytest.mark.asyncio
async def test_report_actual_cost_overflow_preserves_fill_link_and_denominator(db):
    db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
    db.add(log_row())
    db.add(TradeOrder(order_id="extreme-order", account_id="default", broker="paper", code="600001",
        side="buy", source="test", strategy_version="v1", decision_round_id="round-1",
        decision_at=AT, as_of_at=AT, created_at=AT, trade_date=DAY,
        order_type="limit", quantity=100, price=10, status="filled"))
    db.add(TradeFill(fill_id="extreme-fill", order_id="extreme-order", broker="paper", code="600001",
        side="buy", quantity=100, price=1e308, commission=5, tax=0, filled_at=AT))
    db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
    await db.flush()
    result = await research.build_signal_research_report(db, start_date=DAY, end_date=DAY, as_of=AT)
    row = result["signals"][0]
    assert result["summary"]["confirmation_events"] == 1
    assert row["actual_fill_ids"] == ["extreme-fill"] and row["actual_fill_quantity"] == 100
    assert row["actual_fill_cost_status"] == "unknown"
    assert row["actual_fill_cash_including_fees"] is None
    assert row["actual_fill_cost_reason"] == "non_finite_fill_cost_arithmetic"
    assert not db.new and not db.dirty
    json.dumps(result, allow_nan=False)
