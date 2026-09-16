"""All fixtures use isolated SQLite/Parquet; no business DB, network or notifications."""
import asyncio
import importlib.util
import threading
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings
from app.db.session import Base
from app.models.governance import TradeCalendarModel
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.stock import QuoteRound
from app.models.trading import TradeFill, TradeOrder
from app.paper.post_exit_research import (
    PostExitPolicy, build_post_exit_report, evaluate_post_exit_labels, load_quote_archive,
)

DAY = date(2026, 9, 8)
EXIT = datetime(2026, 9, 8, 14, 58, 30)
ASOF = datetime(2026, 9, 8, 15, 15)


def cycle(**overrides):
    return {"eligible": True, "identity_status": "valid", "exit_at": EXIT.isoformat(),
            "final_exit_price": 10, "entry_basis_before_final_exit": 9,
            "fill_round_id": "exit-round", **overrides}


def record(at, price=10, **overrides):
    return {"round_id": "qr-" + at.strftime("%H%M%S"), "source_at": at.isoformat(),
            "commit_at": at.isoformat(), "price": price, "prev_close": 10,
            "limit_up": 11, "reason": "", **overrides}


def samples(close=11):
    return [record(EXIT+timedelta(seconds=30), 10.2),
            record(EXIT+timedelta(seconds=60), 10.3),
            record(datetime(2026, 9, 8, 15), close)]


@pytest.mark.parametrize("peak,close,expected", [
    (10.19, 10.19, ("false", "unknown", "unknown")),
    (10.2, 10.2, ("true", "unknown", "unknown")),
    (10.3, 10.3, ("true", "unknown", "unknown")),
    (11, 11, ("true", "unknown", "unknown")),
])
def test_three_layer_thresholds(peak, close, expected):
    rows = samples(close)
    rows[1]["price"] = peak
    rows[0]["price"] = min(peak, 10.19)
    value = evaluate_post_exit_labels(cycle(), rows, as_of=ASOF)
    assert tuple(value[k] for k in ("intraday_rebound", "close_early", "limit_level")) == expected
    assert value["executable_return"] is value["hypothetical_net_cash"] is None


@pytest.mark.parametrize("bad", [
    record(EXIT-timedelta(seconds=1), 99, commit_at=(EXIT+timedelta(seconds=30)).isoformat()),
    record(EXIT, 99, commit_at=(EXIT+timedelta(seconds=30)).isoformat()),
    record(EXIT+timedelta(seconds=30), 99, round_id="exit-round"),
    record(ASOF+timedelta(seconds=1), 99),
    record(EXIT+timedelta(seconds=30), 99, reason="invalid_quote_identity_clock_or_price"),
])
def test_ignores_pre_exit_same_round_future_or_invalid_samples(bad):
    rows = [dict(r, price=10, high=999, low=1) for r in samples(10)] + [bad]
    result = evaluate_post_exit_labels(cycle(), rows, as_of=ASOF)
    assert result["sampled_peak_after_exit_pct"] == 0
    assert result["intraday_rebound"] != "true"


def test_gaps_and_missing_limit_stay_unknown_without_erasing_positive_rebound():
    result = evaluate_post_exit_labels(
        cycle(exit_at=datetime(2026, 9, 8, 10).isoformat()),
        [record(datetime(2026, 9, 8, 15), 10.2, limit_up=None)], as_of=ASOF)
    assert result["intraday_rebound"] == "true"
    assert result["close_early"] == result["limit_level"] == "unknown"
    assert result["coverage"] == "partial"


def test_no_samples_unknown_and_intraday_close_is_pending():
    result = evaluate_post_exit_labels(cycle(), [], as_of=ASOF)
    assert result["intraday_rebound"] == result["close_early"] == result["limit_level"] == "unknown"
    result = evaluate_post_exit_labels(cycle(), samples(), as_of=datetime(2026, 9, 8, 14, 59, 30))
    assert result["intraday_rebound"] == "true"
    assert result["close_early"] == result["limit_level"] == "unknown"
    assert result["status"] == "pending_close"


def test_rebound_below_entry_cost_is_not_close_early():
    result = evaluate_post_exit_labels(cycle(entry_basis_before_final_exit=12), samples(), as_of=ASOF)
    assert result["close_early"] == "unknown"
    assert result["terminal_quote_vs_entry_basis_pct"] < 0


def test_duplicate_and_conflicting_clocks_and_price_basis_break():
    rows = samples()
    assert evaluate_post_exit_labels(cycle(), rows+rows, as_of=ASOF)["sample_count"] == 3
    result = evaluate_post_exit_labels(cycle(), rows+[dict(rows[0], price=99)], as_of=ASOF)
    assert result["intraday_rebound"] == "unknown"
    result = evaluate_post_exit_labels(cycle(), [rows[0], dict(rows[1], prev_close=9), rows[2]], as_of=ASOF)
    assert result["close_early"] == "unknown"


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("different", [{"price": 10.4}, {"prev_close": 9}])
@pytest.mark.parametrize("price", [10.19, 10.3])
def test_same_source_conflicting_price_basis_is_never_resolved_by_input_order(reverse, different, price):
    rows = [dict(row, price=price) for row in samples(price)]
    conflicting = dict(rows[0], round_id="other-round", **different)
    rows.append(conflicting)
    if reverse:
        rows.reverse()
    result = evaluate_post_exit_labels(cycle(), rows, as_of=ASOF)
    assert result["intraday_rebound"] == "unknown"
    assert result["sampled_peak_after_exit_pct"] is None
    assert result["reasons"] == ["price_basis_or_source_clock_conflict"]


def test_same_source_identical_price_basis_still_deduplicates_across_rounds():
    rows = samples()
    duplicate = dict(rows[0], round_id="other-round",
                     commit_at=(EXIT + timedelta(seconds=40)).isoformat())
    result = evaluate_post_exit_labels(cycle(), [duplicate, *rows], as_of=ASOF)
    assert result["sample_count"] == 3
    assert result["intraday_rebound"] == "true"


@pytest.mark.parametrize("kwargs", [{"max_source_age_sec": 0}, {"max_sample_gap_sec": -1},
                                    {"max_sample_gap_sec": True}])
def test_policy_rejects_invalid_research_clocks(kwargs):
    with pytest.raises(ValueError):
        PostExitPolicy(**kwargs)


def make_archive(root, at, price=11, *, codes=("600001",), limit=True):
    round_id = "qr-" + at.strftime("%Y%m%dT%H%M%S")
    rows = [{"code": code, "name": "样本", "price": price, "prev_close": 10, "high": 999,
             "source_quote_at": at.isoformat(), "received_at": at.isoformat(),
             "committed_at": at.isoformat(), "quote_round_id": round_id, "limit_up": 11}
            for code in codes]
    paths = {}
    for family in ("compact", "focus"):
        path = root / family / ("trade_date=" + DAY.isoformat()) / (round_id+".parquet")
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(path, index=False)
        paths[family] = str(path)
    return QuoteRound(round_id=round_id, trade_date=DAY, source="tencent",
                      committed_at=at, as_of_at=at, expected_count=len(codes), received_count=len(codes),
                      quality_status="ok", archive_status="ready", archive_path=paths["compact"],
                      focus_path=paths["focus"] if limit else None,
                      config_version="frozen-config", code_version="frozen-code")


def test_archive_batch_projection_and_no_duplicate_reads(tmp_path, monkeypatch):
    row = make_archive(tmp_path, datetime(2026, 9, 8, 15), codes=("600001", "600002"))
    original = pd.read_parquet
    reads = []
    def read(*args, **kwargs):
        reads.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, "read_parquet", read)
    series, manifest = load_quote_archive([row], {DAY: {"600001", "600002"}}, root=tmp_path, policy=PostExitPolicy())
    assert len(reads) == 2  # one compact + one focus for both codes/cycles
    assert "high" not in reads[0]["columns"]
    assert len(manifest) == 3
    assert all(m.get("sha256") for m in manifest if m["family"] != "round_metadata")
    assert series[DAY, "600001"][0]["limit_up"] == 11
    assert series[DAY, "600002"][0]["price"] == 11


@pytest.mark.parametrize("mode", ["outside", "missing", "corrupt", "symlink_escape", "missing_column",
                                    "source_before_day", "duplicate", "stale", "future", "degraded"])
def test_archive_invalid_paths_sources_and_schema_are_unknown(tmp_path, mode):
    root = tmp_path/"archive"
    row = make_archive(root, datetime(2026, 9, 8, 15))
    path = Path(row.archive_path)
    if mode == "outside":
        row.archive_path = str(tmp_path/"other.parquet")
    elif mode == "missing":
        path.unlink()
    elif mode == "corrupt":
        path.write_bytes(b"not parquet")
    elif mode == "symlink_escape":
        outside = tmp_path/"outside.parquet"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
    elif mode == "degraded":
        row.quality_status = "degraded"
    else:
        frame = pd.read_parquet(path)
        if mode == "missing_column":
            frame = frame.drop(columns=["source_quote_at"])
        elif mode == "duplicate":
            frame = pd.concat([frame, frame], ignore_index=True)
        else:
            delta = {"source_before_day": timedelta(days=-1), "stale": timedelta(minutes=-4), "future": timedelta(seconds=1)}[mode]
            frame["source_quote_at"] = (row.committed_at+delta).isoformat()
        frame.to_parquet(path, index=False)
    series, _ = load_quote_archive([row], {DAY: {"600001"}}, root=root, policy=PostExitPolicy())
    assert series[DAY, "600001"][0]["reason"]


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-01")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        account = PaperAccount(account_name="default", initial_capital=10000, status="active")
        db.add(account)
        await db.flush()
        specs = [
            ("buy", 200, 9, datetime(2026, 9, 7, 10), 0),
            ("sell", 100, 9.5, EXIT-timedelta(minutes=1), .95),
            ("sell", 100, 10, EXIT, 1),
        ]
        trade_ids = []
        for i, (side, qty, price, at, tax) in enumerate(specs):
            trade = PaperTradeLog(account_id=account.id, code="600001", trade_type=side,
                amount=qty, price=price, trade_time=at, commission=5, tax=tax,
                strategy_version="v1", fill_round_id=f"fill-{i}", reason="test")
            db.add(trade)
            await db.flush()
            trade_ids.append(trade.id)
            db.add(TradeOrder(order_id=f"order-{i}", account_id="default", code="600001",
                broker="paper", side=side, order_type="limit", price=price, quantity=qty,
                strategy_version="v1", status="filled", created_at=at, trade_date=at.date(),
                risk_json=json.dumps({"experiment_entry": {"active": True, "strategy_version": "v1"}})))
            db.add(TradeFill(fill_id=f"fill-{i}", order_id=f"order-{i}", broker="paper",
                code="600001", side=side, price=price, quantity=qty, commission=5, tax=tax,
                broker_trade_id=str(trade.id), fill_round_id=f"fill-{i}", filled_at=at, trade_date=at.date()))
        for r in samples():
            db.add(make_archive(tmp_path/"archive", datetime.fromisoformat(r["source_at"]), r["price"]))
        db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
        await db.commit()
    yield maker, tmp_path, trade_ids
    await engine.dispose()


async def build(db, root, **kwargs):
    return await build_post_exit_report(db, start_date=DAY, end_date=DAY, as_of=ASOF,
                                       archive_root=root/"archive", versions={"default": "v1"}, **kwargs)


@pytest.mark.asyncio
async def test_real_complete_cycle_ids_fees_and_read_only_builder(database):
    maker, root, ids = database
    async with maker() as db:
        await db.execute(text("PRAGMA query_only=ON"))
        await db.execute(text("BEGIN"))
        result = await build(db, root)
        assert len(result["cycles"]) == 1
        c = result["cycles"][0]
        assert c["first_buy_trade_id"] == ids[0] and c["exit_trade_id"] == ids[-1]
        assert c["final_exit_quantity"] == 100  # not 200 shares already partially exited
        assert c["final_exit_net_cash"] == 994
        assert c["net_pnl"] == pytest.approx(133.05)  # fees once, not duplicated with TradeFill
        assert c["actual_fees"] == pytest.approx(16.95)
        assert c["evaluation"]["limit_level"] == "unknown"
        assert result["archive_availability"] == "retrospective_read_no_historical_ready_at"
        assert result["evidence_hash"]
        json.dumps(result, allow_nan=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_close", [9, 10])
async def test_archive_to_read_only_report_keeps_duplicate_basis_conflicts(database, previous_close):
    maker, root, ids = database
    source_at = EXIT + timedelta(seconds=30)
    later_round = make_archive(root/"archive", source_at + timedelta(seconds=2), price=10.2)
    # Two independently committed source responses report the same source clock
    # and price. Receipt order is not authority to discard a contradictory basis.
    for raw in (later_round.archive_path, later_round.focus_path):
        frame = pd.read_parquet(raw)
        frame["source_quote_at"] = source_at.isoformat()
        frame["prev_close"] = previous_close
        frame.to_parquet(raw, index=False)
    async with maker() as db:
        db.add(later_round)
        await db.commit()
        await db.execute(text("PRAGMA query_only=ON"))
        await db.execute(text("BEGIN"))
        before = (await db.execute(text(
            "SELECT id, amount, price, commission, tax, trade_time FROM paper_trade_log ORDER BY id"
        ))).all()
        result = await build(db, root)
        after = (await db.execute(text(
            "SELECT id, amount, price, commission, tax, trade_time FROM paper_trade_log ORDER BY id"
        ))).all()
    assert after == before
    assert len(result["cycles"]) == 1
    value = result["cycles"][0]
    assert value["exit_trade_id"] == ids[-1]
    assert value["identity_status"] == "valid"
    assert value["net_pnl"] == pytest.approx(133.05)
    assert value["evaluation"]["close_early"] == value["evaluation"]["limit_level"] == "unknown"
    assert value["evaluation"]["executable_return"] is None
    assert len(result["manifest"]) == 12  # All four rounds remain evidence, not just the winner.
    if previous_close != 10:
        assert value["evaluation"]["intraday_rebound"] == "unknown"
        assert value["evaluation"]["sampled_peak_after_exit_pct"] is None
        assert value["evaluation"]["reasons"] == ["price_basis_or_source_clock_conflict"]
    else:
        assert value["evaluation"]["intraday_rebound"] == "true"
        assert value["evaluation"]["sample_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["forced", "old_version", "fill_price", "fill_clock", "fill_fee", "fill_qty",
                                   "fill_round", "fill_missing", "fill_duplicate", "missing_calendar", "order_future", "order_qty"])
async def test_excluded_and_unproven_cycles_retained(database, fault):
    maker, root, ids = database
    async with maker() as db:
        trade = await db.get(PaperTradeLog, ids[-1])
        fill = await db.scalar(select(TradeFill).where(TradeFill.broker_trade_id == str(ids[-1])))
        if fault == "forced":
            trade.forced_probe = True
        elif fault == "old_version":
            trade.strategy_version = "old"
        elif fault == "fill_price":
            fill.price = 99
        elif fault == "fill_clock":
            fill.filled_at = EXIT+timedelta(seconds=1)
        elif fault == "fill_fee":
            fill.commission = 0
        elif fault == "fill_qty":
            fill.quantity = 200
        elif fault == "fill_round":
            fill.fill_round_id = "other-round"
        elif fault == "fill_missing":
            await db.delete(fill)
        elif fault in {"order_future", "order_qty"}:
            order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == fill.order_id))
            if fault == "order_future":
                order.created_at = EXIT + timedelta(seconds=1)
            else:
                order.quantity = 1
        elif fault == "fill_duplicate":
            db.add(TradeFill(fill_id="duplicate", order_id=fill.order_id, broker="paper", code=fill.code,
                side=fill.side, price=fill.price, quantity=fill.quantity, filled_at=fill.filled_at,
                trade_date=DAY, broker_trade_id=fill.broker_trade_id))
        else:
            await db.delete(await db.get(TradeCalendarModel, DAY))
        await db.commit()
        result = await build(db, root)
    assert len(result["cycles"]) == 1
    c = result["cycles"][0]
    assert c["exclusion_reasons"]
    assert c["evaluation"]["intraday_rebound"] == c["evaluation"]["close_early"] == c["evaluation"]["limit_level"] == "unknown"


@pytest.mark.asyncio
async def test_partial_exit_no_tracking_and_later_data_does_not_enter_early_report(database):
    maker, root, ids = database
    async with maker() as db:
        before = await build_post_exit_report(db, start_date=DAY, end_date=DAY,
            as_of=EXIT-timedelta(seconds=1), archive_root=root/"archive", versions={"default":"v1"})
        assert before["cycles"] == []
        result = await build_post_exit_report(db, start_date=DAY, end_date=DAY,
            as_of=EXIT+timedelta(seconds=30), archive_root=root/"archive", versions={"default":"v1"})
        assert result["cycles"][0]["evaluation"]["close_early"] == "unknown"
        assert len(result["manifest"]) == 3


def test_same_minute_high_and_missing_limit_never_prove_limit_level(tmp_path):
    row = make_archive(tmp_path, datetime(2026, 9, 8, 15), price=10.3, limit=False)
    series, _ = load_quote_archive([row], {DAY: {"600001"}}, root=tmp_path, policy=PostExitPolicy())
    result = evaluate_post_exit_labels(cycle(), series[DAY, "600001"], as_of=ASOF)
    assert result["sampled_peak_after_exit_pct"] == 3
    assert result["limit_level"] == "unknown"


def test_five_percent_level_boundary_uses_final_exit_not_day_change():
    exact = evaluate_post_exit_labels(cycle(final_exit_price=11/1.05), samples(), as_of=ASOF)
    assert exact["limit_level"] == "unknown"
    under = evaluate_post_exit_labels(cycle(final_exit_price=11/1.049), samples(), as_of=ASOF)
    assert under["limit_level"] == "unknown"
    assert exact["terminal_quote_after_exit_pct"] == 5
    assert under["terminal_quote_after_exit_pct"] == 4.9


def test_lunch_is_not_coverage_gap_and_after_close_exit_unassessable():
    at = datetime(2026, 9, 8, 11, 29, 30)
    rows = [record(datetime(2026, 9, 8, 11, 30), 10),
            record(datetime(2026, 9, 8, 13), 10)]
    result = evaluate_post_exit_labels(cycle(exit_at=at.isoformat()), rows,
                                      as_of=datetime(2026, 9, 8, 13))
    assert result["coverage"] == "complete_sampled_window"
    result = evaluate_post_exit_labels(cycle(exit_at=datetime(2026, 9, 8, 15).isoformat()),
                                      [record(datetime(2026, 9, 8, 15, 1), 11)], as_of=ASOF)
    assert result["intraday_rebound"] == "unknown"


@pytest.mark.asyncio
async def test_accounts_and_repeated_codes_keep_distinct_real_liquidation_ids(database):
    maker, root, ids = database
    async with maker() as db:
        second = PaperAccount(account_name="default", initial_capital=10000, status="closed")
        db.add(second)
        await db.flush()
        # Same display account name, different immutable account ID and trade IDs.
        # Copy actual fully mapped cycle under a separate historical account instance.
        originals = list((await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all())
        for i, t in enumerate(originals):
            cloned = PaperTradeLog(account_id=second.id, code=t.code, trade_type=t.trade_type,
                amount=t.amount, price=t.price, trade_time=t.trade_time, commission=t.commission,
                tax=t.tax, strategy_version=t.strategy_version, fill_round_id=t.fill_round_id)
            db.add(cloned)
            await db.flush()
            db.add(TradeOrder(order_id=f"clone-order-{i}", account_id="default", code=t.code,
                broker="paper", side=t.trade_type, order_type="limit", price=t.price, quantity=t.amount,
                strategy_version="v1", status="filled", created_at=t.trade_time, trade_date=t.trade_time.date(),
                risk_json=json.dumps({"experiment_entry":{"active":True,"strategy_version":"v1"}})))
            db.add(TradeFill(fill_id=f"clone-fill-{i}", order_id=f"clone-order-{i}", broker="paper",
                code=t.code, side=t.trade_type, price=t.price, quantity=t.amount,
                commission=t.commission, tax=t.tax, broker_trade_id=str(cloned.id),
                fill_round_id=t.fill_round_id, filled_at=t.trade_time, trade_date=t.trade_time.date()))
        await db.commit()
        result = await build(db, root)
    assert len(result["cycles"]) == 2
    assert len({c["cycle_key"] for c in result["cycles"]}) == 2
    assert len({c["account_id"] for c in result["cycles"]}) == 2
    assert len(result["manifest"]) == 9  # 3 rounds*(metadata+2 families), batch reads
    assert all(c["evaluation"]["limit_level"] == "unknown" for c in result["cycles"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["null_fee", "malformed_entry", "same_day_buy", "excluded_flag", "orphan_sell"])
async def test_data_missing_and_forced_scope_not_silently_converted_to_negative(database, fault):
    maker, root, ids = database
    async with maker() as db:
        if fault == "orphan_sell":
            db.add(PaperTradeLog(account_id=1, code="600002", trade_type="sell",
                amount=100, price=10, trade_time=EXIT, commission=5, tax=1, strategy_version="v1"))
        elif fault == "malformed_entry":
            order = await db.scalar(select(TradeOrder).where(TradeOrder.side == "buy"))
            order.risk_json = '{"experiment_entry": ["bad"]}'
        elif fault == "same_day_buy":
            trade = await db.get(PaperTradeLog, ids[0])
            trade.trade_time = EXIT-timedelta(minutes=10)
        else:
            trade = await db.get(PaperTradeLog, ids[-1])
            if fault == "null_fee":
                trade.commission = None
            else:
                trade.excluded_from_performance = True
        await db.commit()
        result = await build(db, root)
    affected = result["cycles"][-1] if fault == "orphan_sell" else result["cycles"][0]
    assert affected["exclusion_reasons"]
    assert affected["evaluation"]["intraday_rebound"] == "unknown"
    if fault == "null_fee":
        assert affected["net_pnl"] is None


@pytest.mark.asyncio
async def test_corrupt_archive_does_not_block_other_rounds_and_manifest_hash_is_frozen(database):
    maker, root, _ = database
    async with maker() as db:
        first = await build(db, root)
        row = await db.scalar(select(QuoteRound).order_by(QuoteRound.committed_at))
        Path(row.archive_path).write_bytes(b"corrupt fixture")
        second = await build(db, root)
    assert first["cycles"][0]["evaluation"]["coverage"] == "complete_sampled_window"
    assert second["cycles"][0]["evaluation"]["coverage"] == "partial"
    assert second["cycles"][0]["evaluation"]["intraday_rebound"] == "true"
    assert first["evidence_hash"] != second["evidence_hash"]


@pytest.mark.asyncio
async def test_builder_never_autoflushes_pending_business_objects(database):
    maker, root, _ = database
    async with maker() as db:
        pending = PaperTradeLog(account_id=1, code="600001", trade_type="buy", amount=100)
        db.add(pending)  # deliberately missing required price/time
        result = await build(db, root)
        assert result["summary"]["full_exits"] == 1
        assert pending.id is None and pending in db.new


@pytest.mark.asyncio
async def test_builder_archive_io_off_loop_with_owned_metadata_and_optional_root(database, monkeypatch):
    from app.paper import post_exit_research as module
    maker, root, _ = database
    monkeypatch.setattr(settings, "QUOTE_ROUND_ARCHIVE_DIR", root/"archive")
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    started, release = asyncio.Event(), threading.Event()
    original = module.load_quote_archive
    def read_in_worker(refs, codes, **kwargs):
        assert threading.get_ident() != main_thread
        assert refs and all(isinstance(r, module.QuoteArchiveRef) for r in refs)
        assert all(not hasattr(r, "_sa_instance_state") for r in refs)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return original(refs, codes, **kwargs)
    monkeypatch.setattr(module, "load_quote_archive", read_in_worker)
    async with maker() as db:
        task = asyncio.create_task(module.build_post_exit_report(
            db, start_date=DAY, end_date=DAY, as_of=ASOF, versions={"default":"v1"}))
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            assert not task.done()  # event loop remains responsive while worker blocks
        finally:
            release.set()
        result = await asyncio.wait_for(task, timeout=5)
    assert result["cycles"][0]["evaluation"]["limit_level"] == "unknown"


def test_intraday_no_hit_is_right_censored_and_terminal_is_not_formal_close():
    result = evaluate_post_exit_labels(cycle(), [record(EXIT+timedelta(seconds=30), 10)],
                                      as_of=EXIT+timedelta(seconds=30))
    assert result["coverage"] == "complete_sampled_window"
    assert result["intraday_rebound"] == "unknown"
    assert result["status"] == "pending_close"
    terminal = evaluate_post_exit_labels(cycle(), samples(), as_of=ASOF)
    assert terminal["intraday_rebound"] == "true"
    assert terminal["close_early"] == terminal["limit_level"] == "unknown"
    assert terminal["close_after_exit_pct"] is terminal["close_vs_entry_basis_pct"] is None
    assert terminal["terminal_quote_after_exit_pct"] == 10
    assert "formal_close_health_unavailable" in terminal["reasons"]


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), "bad", None])
def test_dirty_numbers_are_not_prices(value):
    from app.paper.post_exit_research import _number
    assert _number(value) is None
    result = evaluate_post_exit_labels(cycle(final_exit_price=value), samples(), as_of=ASOF)
    assert result["intraday_rebound"] == "unknown"


@pytest.mark.asyncio
async def test_round_denominator_and_full_report_hash_cover_metadata_fees_and_policy(database):
    maker, root, ids = database
    async with maker() as db:
        first = await build(db, root)
        trade = await db.get(PaperTradeLog, ids[-1])
        fill = await db.scalar(select(TradeFill).where(TradeFill.broker_trade_id == str(ids[-1])))
        trade.commission = fill.commission = 6
        await db.commit()
        fees = await build(db, root)
        assert first["evidence_hash"] == fees["evidence_hash"]
        assert first["report_data_hash"] != fees["report_data_hash"]
        assert fees["cycles"][0]["actual_fees"] == pytest.approx(17.95)
        policy = await build(db, root, policy=PostExitPolicy(max_sample_gap_sec=100))
        assert policy["report_data_hash"] != fees["report_data_hash"]
        row = await db.scalar(select(QuoteRound).order_by(QuoteRound.committed_at))
        row.archive_status = "pending"
        await db.commit()
        pending = await build(db, root)
        metadata = [m for m in pending["manifest"] if m["family"] == "round_metadata"]
        assert len(metadata) == 3
        assert metadata[0]["reason_code"] == "archive_not_ready"
        assert first["evidence_hash"] != pending["evidence_hash"]
        row.config_version = "changed-pending"
        row.quality_status = "degraded"
        await db.commit()
        degraded = await build(db, root)
        assert degraded["evidence_hash"] != pending["evidence_hash"]
        assert any(m.get("reason_code") == "round_source_or_quality_unknown" for m in degraded["manifest"])


@pytest.mark.asyncio
async def test_dirty_historical_quantity_does_not_abort_or_invent_exit(database):
    maker, root, ids = database
    async with maker() as db:
        trade = await db.get(PaperTradeLog, ids[1])
        trade.amount = "broken"
        await db.commit()
        result = await build(db, root)
    assert result["summary"]["full_exits"] == 0
    assert result["cycles"]
    assert all(c["evaluation"]["status"] == "unknown" for c in result["cycles"])
    assert all(c["net_pnl"] is None for c in result["cycles"])
    assert all("invalid_quantity_history" in c["exclusion_reasons"] for c in result["cycles"])


def cli_module():
    path = Path(__file__).parents[1]/"scripts/paper_post_exit_research.py"
    spec = importlib.util.spec_from_file_location("post_exit_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_cli_readonly_database_new_output_and_output_guards(database, monkeypatch):
    maker, root, _ = database
    module = cli_module()
    monkeypatch.setattr(module, "ROOT", root)
    (root/"outputs").mkdir()
    args = SimpleNamespace(database=root/"test.db", archive_root=root/"archive", start=DAY, end=DAY,
        as_of=ASOF, output=root/"outputs/result.json", max_source_age_sec=180, max_sample_gap_sec=90)
    # Actual file URI ro + query_only; never execute a production CLI.
    before = (root/"test.db").read_bytes()
    result = await module.run(args)
    assert result["read_only"]
    assert (root/"test.db").read_bytes() == before
    assert args.output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        await module.run(args)
    args.output = root/"outside.json"
    with pytest.raises(ValueError):
        await module.run(args)
