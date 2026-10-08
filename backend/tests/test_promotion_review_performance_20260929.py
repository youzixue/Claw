"""Review I/O reductions preserve batch selection, risk calls and unknown outcomes."""
from collections import Counter
from datetime import date, datetime, timedelta
import json
import socket

import pytest
from sqlalchemy import event, select

from app.api.v1 import promotion as p
from app.models.governance import TradeCalendarModel
from app.models.signal import PromotionPredictionRecord as Record
from app.models.stock import LimitUpPool, StockKline
from test_promotion_api import promotion_api_env, disable_live_dragon_tiger_lookup

DAY = date(2026, 8, 13)
AFTER = date(2026, 8, 14)
NOW = datetime(2026, 8, 14, 22)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden in isolated review performance tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def make_record(code, *, day=DAY, source="schedule", context="promotion_2000",
                hour=20, batch="close", route="mainline_spread_start", factors=None,
                target=1, **extra):
    payload = {
        "prediction_snapshot_source": "schedule",
        "prediction_snapshot_context": "promotion_2000",
        "prediction_snapshot_recorded_at": f"{day}T20:00:00",
        "prediction_snapshot_batch_key": "legacy-key",
        "prediction_ranked_selected": True,
        "prediction_ranked_position": 1,
        "prediction_recall_ranked_selected": True,
        "prediction_recall_ranked_position": 1,
        "prediction_actionable": True,
        "route_score": 50,
    }
    payload.update(factors or {})
    return Record(
        code=code, target_board=target, prediction_trade_date=day,
        predicted_probability=.5, calibrated_probability=.5,
        candidate_route=route, snapshot_source=source, snapshot_context=context,
        snapshot_recorded_at=datetime.combine(day, datetime.min.time()).replace(hour=hour) if hour is not None else None,
        snapshot_batch_key=batch, factors_json=json.dumps(payload), **extra,
    )


@pytest.mark.asyncio
async def test_metadata_selector_matches_original_order_and_legacy_boundaries(promotion_api_env, monkeypatch):
    # The production legacy route set may currently be empty; exercise the
    # selector/filter ordering with an explicitly isolated historical fixture.
    legacy_route = "legacy_test_observation"
    monkeypatch.setattr(p, "FIRST_BOARD_LEGACY_OBSERVATION_RECORD_ROUTES", {legacy_route})
    Session, _ = promotion_api_env
    async with Session() as db:
        rows = [
            make_record("600001", context="promotion_1510", hour=23, batch="fallback"),
            make_record("600002", batch="first-tie"),
            make_record("600003", batch="other-tie"),
            make_record("600004", hour=19, batch="first-tie"),
            make_record("600005", context="promotion_0935", hour=23),
            make_record("600006", source="page", hour=23),
            make_record("600007", day=DAY-timedelta(days=1), source="legacy", context="legacy", hour=None, batch=None),
            make_record("600008", day=DAY-timedelta(days=1), source=" cron ", context="20:00", hour=21, batch="alias"),
            make_record("600009", day=DAY-timedelta(days=2), context="15:10", batch="fallback"),
            make_record("600010", day=DAY-timedelta(days=3), context="promotion_1305"),
            make_record("600011", day=DAY-timedelta(days=4), source="scheduler", context=" Promotion_2000 "),
            make_record("600012", day=DAY-timedelta(days=5), hour=None, batch="time-fallback",
                        factors={"prediction_snapshot_recorded_at": "bad"},
                        created_at=datetime(2026, 8, 8, 20), updated_at=datetime(2026, 8, 8, 21)),
            make_record("600013", day=DAY-timedelta(days=6), batch="",
                        factors={"prediction_snapshot_batch_key": "", "prediction_snapshot_recorded_at": ""}),
            make_record("600014", day=DAY-timedelta(days=6), hour=19, batch="schedule:promotion_2000:"),
            make_record("600015", target=2, context="promotion_1510"),
            make_record("600016", day=DAY-timedelta(days=7), hour=19, batch="earlier"),
            make_record("600017", day=DAY-timedelta(days=7), hour=21, batch="latest-observation",
                        route=legacy_route),
        ]
        db.add_all(rows)
        await db.commit()
        dates = sorted({r.prediction_trade_date for r in rows})
        original = (await db.execute(select(Record).where(
            Record.prediction_trade_date.in_(dates), Record.target_board.in_([1, 2]),
        ))).scalars().all()
        expected = [r.id for r in p._latest_learning_batch_records(original)
                    if p._prediction_record_snapshot_source(r) == "schedule"]
        actual = await p._load_promotion_review_close_records(db, dates)
        assert [r.id for r in actual] == expected
        assert rows[1] in actual and rows[3] in actual and rows[2] not in actual
        assert rows[-1] in actual and rows[-2] not in actual
        assert not any(r.prediction_trade_date == DAY-timedelta(days=7)
                       for r in actual if not p._is_legacy_observation_first_board_record(r))
        assert not any(r.prediction_trade_date == DAY-timedelta(days=3) for r in actual)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_identity_projection_uses_bounded_payload_queries(promotion_api_env, legacy):
    Session, _ = promotion_api_env
    async with Session() as db:
        # Enough rows to cross the bounded IN chunk; no per-record lazy loads.
        records = [make_record(f"60{i:04d}", source="legacy" if legacy else "schedule",
                               context="legacy" if legacy else "promotion_2000",
                               hour=None if legacy else 20, batch=None if legacy else "close")
                   for i in range(405)]
        records.append(make_record("601000", context="promotion_1305",
                                   factors={"unused": "x"*65536}))
        db.add_all(records)
        await db.commit()
        db.expunge_all()
        statements = []
        loaded = []
        def observe(conn, cursor, statement, params, ctx, many):
            if "FROM promotion_prediction_record" in statement:
                statements.append(statement)
        def on_load(session, record):
            if isinstance(record, Record):
                loaded.append(record.id)
        event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
        event.listen(db.sync_session, "loaded_as_persistent", on_load)
        try:
            selected = await p._load_promotion_review_close_records(db, [DAY])
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
            event.remove(db.sync_session, "loaded_as_persistent", on_load)
        assert len(selected) == 405
        assert len(loaded) == 405
        assert len(statements) == (5 if legacy else 3)
        first_projection = statements[0].split("FROM")[0]
        assert "factors_json" not in first_projection
        assert "reason_snapshot" not in first_projection
        assert all("promotion_prediction_record.id IN" in q for q in statements[1:])


async def seed_review(db, bad=None, selected=True):
    db.add_all([TradeCalendarModel(trade_date=d, is_trade_day=True) for d in (DAY, AFTER)])
    record = make_record("600001", factors={"prediction_ranked_selected": selected})
    db.add(record)
    db.add_all([LimitUpPool(code="600099", trade_date=DAY, consecutive_days=1, source="test"),
                LimitUpPool(code="600001", trade_date=AFTER, consecutive_days=1, source="test")])
    # Non-prediction market code must still count in direction totals.
    for code in ("600001", "600002"):
        db.add(StockKline(code=code, trade_date=DAY, close=10, volume=100, source="ths"))
        if bad == "missing" and code == "600001":
            continue
        db.add(StockKline(code=code, trade_date=AFTER, close=11,
            volume=0 if bad == "volume" and code == "600001" else 100,
            prev_close=9 if bad == "chain" and code == "600001" else 10,
            change_pct=float("nan") if bad == "nan" and code == "600001" else 10,
            source="spot" if bad == "source" and code == "600001" else "ths"))
    await db.commit()
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [3, 10, 30])
@pytest.mark.parametrize("bad", [None, "missing", "volume", "chain", "nan", "source"])
async def test_projection_and_per_lane_decode_preserve_market_and_unknowns(promotion_api_env, monkeypatch, days, bad):
    Session, _ = promotion_api_env
    async with Session() as db:
        record = await seed_review(db, bad=bad)
        payload = record.factors_json
        count = Counter()
        original = p._json_loads_safe
        def decode(value, default=None):
            count[value] += 1
            return original(value, default)
        monkeypatch.setattr(p, "_json_loads_safe", decode)
        statements = []
        def observe(conn, cursor, statement, params, ctx, many):
            if "FROM stock_kline" in statement:
                statements.append(statement)
        event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
        try:
            result = await p._build_promotion_daily_learning_review(db, lookback_days=days, now=NOW)
        finally:
            event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
        assert count[payload] == 1
        latest = result["latest"]
        assert latest["predicted_count"] == 1
        assert latest["directional_unknown_count"] == int(bad is not None)
        assert latest["directional_evaluable_count"] == int(bad is None)
        assert latest["directional_precision"] == (1 if bad is None else None)
        assert latest["actual_rising_count"] == (2 if bad is None else 1)
        assert statements
        projection = statements[0].split("FROM")[0]
        for field in ("code", "trade_date", "source", "close", "volume", "prev_close", "change_pct"):
            assert f"stock_kline.{field}" in projection
        assert "stock_kline.id" not in projection and "stock_kline.amount" not in projection


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [True, False, 0, 1, None])
async def test_ranked_boolean_semantics_and_no_cross_request_memo(promotion_api_env, selected):
    Session, _ = promotion_api_env
    async with Session() as db:
        record = await seed_review(db, selected=selected)
        first = await p._build_promotion_daily_learning_review(db, now=NOW)
        assert first["latest"]["predicted_count"] == int(selected is True)
        factors = json.loads(record.factors_json)
        factors["prediction_ranked_selected"] = not (selected is True)
        record.factors_json = json.dumps(factors)
        await db.commit()
        second = await p._build_promotion_daily_learning_review(db, now=NOW)
        assert second["latest"]["predicted_count"] == int(selected is not True)


def test_optional_decoded_helpers_preserve_defaults_and_do_not_mutate(monkeypatch):
    records = [make_record("600001"), make_record("600001", route="different",
                                                factors={"prediction_ranked_selected": False})]
    factors = {id(r): json.loads(r.factors_json) for r in records}
    frozen = json.dumps(list(factors.values()), sort_keys=True)
    expected_best = p._select_best_prediction_record(records)
    args = dict(code="600001", target_board=1, record=records[0], tag=None)
    expected_status = p._resolve_actual_replay_status(**args)
    metrics_args = dict(limit_up_codes={"600001"}, rising_codes={"600001"},
                        strong_rising_codes=set(), evaluable_codes={"600001"})
    expected_metrics = p._build_promotion_launch_precursor_metrics(records, **metrics_args)
    def no_decode(*args, **kwargs):
        raise AssertionError("helper ignored supplied decoded factors")
    monkeypatch.setattr(p, "_json_loads_safe", no_decode)
    lookup = lambda record: factors[id(record)]
    assert p._select_best_prediction_record(records, factors_for_record=lookup) is expected_best
    assert p._resolve_actual_replay_status(**args, factors=lookup(records[0])) == expected_status
    assert p._build_promotion_launch_precursor_metrics(records, factors_for_record=lookup, **metrics_args) == expected_metrics
    assert json.dumps(list(factors.values()), sort_keys=True) == frozen


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["update", "delete"])
async def test_two_phase_read_is_atomic_without_extending_risk_snapshot(promotion_api_env, monkeypatch, mutation):
    from sqlalchemy import delete, text, update
    from sqlalchemy.ext.asyncio import AsyncSession

    Session, _ = promotion_api_env
    async with Session() as db:
        assert (await db.scalar(text("PRAGMA journal_mode=WAL"))).lower() == "wal"
        await db.commit()
        record = make_record("600001")
        db.add(record)
        await db.commit()
        record_id, old_factors = record.id, record.factors_json
        db.expunge_all()
        original_execute = db.execute
        changed = False

        async def execute(statement, *args, **kwargs):
            nonlocal changed
            result = await original_execute(statement, *args, **kwargs)
            sql = str(statement)
            if not changed and "FROM promotion_prediction_record" in sql and "factors_json" not in sql:
                changed = True
                # Commit a concurrent writer immediately after metadata was
                # read but before the helper fetches the selected payload.
                async with AsyncSession(db.bind) as writer:
                    if mutation == "delete":
                        await writer.execute(delete(Record).where(Record.id == record_id))
                    else:
                        await writer.execute(update(Record).where(Record.id == record_id).values(
                            factors_json='{"prediction_ranked_selected":false}',
                            snapshot_batch_key="new-batch",
                            snapshot_recorded_at=datetime(2026, 8, 13, 21),
                        ))
                    await writer.commit()
            return result

        monkeypatch.setattr(db, "execute", execute)
        selected = await p._load_promotion_review_close_records(db, [DAY])
        assert changed
        assert [r.id for r in selected] == [record_id]
        assert selected[0].factors_json == old_factors
        assert selected[0].snapshot_batch_key == "close"
        # The short savepoint is released: subsequent independent current-risk
        # reads are not accidentally pinned to the prediction snapshot.
        current = (await original_execute(select(Record.factors_json).where(Record.id == record_id))).scalar_one_or_none()
        assert current == (None if mutation == "delete" else '{"prediction_ranked_selected":false}')


@pytest.mark.asyncio
@pytest.mark.parametrize("autoflush", [False, True])
@pytest.mark.parametrize("query_only", [False, True])
async def test_close_reader_never_flushes_dirty_or_pending_session(promotion_api_env, autoflush, query_only):
    from sqlalchemy import text
    from app.models.stock import StockSpot

    Session, _ = promotion_api_env
    async with Session(autoflush=autoflush) as db:
        record = make_record("600001")
        existing = StockSpot(code="600010", name="stored")
        db.add_all([record, existing])
        await db.commit()
        if query_only:
            await db.execute(text("PRAGMA query_only=ON"))
        existing.name = "uncommitted"
        pending = StockSpot(code="600011", name="pending")
        db.add(pending)
        statements = []
        def forbidden_flush(*args):
            raise AssertionError("read-only close helper flushed Session state")
        def observe(conn, cursor, statement, params, ctx, many):
            statements.append(statement)
        event.listen(db.sync_session, "before_flush", forbidden_flush)
        event.listen(db.bind.sync_engine, "before_cursor_execute", observe)
        try:
            selected = await p._load_promotion_review_close_records(db, [DAY])
        finally:
            event.remove(db.sync_session, "before_flush", forbidden_flush)
            event.remove(db.bind.sync_engine, "before_cursor_execute", observe)
        assert [row.code for row in selected] == ["600001"]
        assert existing in db.dirty and pending in db.new
        assert existing.name == "uncommitted"
        assert not any(statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}
                       for statement in statements)
        with db.no_autoflush:
            assert await db.scalar(select(StockSpot.name).where(StockSpot.code == "600010")) == "stored"
            assert await db.scalar(select(StockSpot.code).where(StockSpot.code == "600011")) is None
        if query_only:
            assert await db.scalar(text("PRAGMA query_only")) == 1
        await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_nested", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_close_reader_preserves_parent_transactions_and_rollback(promotion_api_env, monkeypatch, parent_nested, fail):
    from sqlalchemy import text
    from app.models.stock import StockSpot

    Session, _ = promotion_api_env
    async with Session(autoflush=False) as db:
        db.add(make_record("600001"))
        await db.commit()
        # Pin an actual outer transaction, not SQLite's logical-only SELECT
        # transaction, and place flushed marker writes exclusively in this fixture.
        await db.execute(text("BEGIN"))
        db.add(StockSpot(code="600020", name="outer"))
        await db.flush()
        outer = db.get_transaction()
        nested = await db.begin_nested() if parent_nested else None
        if parent_nested:
            db.add(StockSpot(code="600021", name="parent-savepoint"))
            await db.flush()
        pending = StockSpot(code="600022", name="must-remain-pending")
        db.add(pending)
        original_execute = db.execute

        async def execute(statement, *args, **kwargs):
            if fail and "promotion_prediction_record.factors_json" in str(statement):
                raise RuntimeError("injected payload read failure")
            return await original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", execute)
        if fail:
            with pytest.raises(RuntimeError, match="injected payload"):
                await p._load_promotion_review_close_records(db, [DAY])
        else:
            assert len(await p._load_promotion_review_close_records(db, [DAY])) == 1
        assert outer is db.get_transaction() and outer.is_active
        assert db.get_nested_transaction() is nested
        assert pending in db.new
        if nested is not None:
            assert nested.is_active
            await nested.rollback()
            with db.no_autoflush:
                assert (await original_execute(select(StockSpot.code).where(StockSpot.code == "600021"))).scalar_one_or_none() is None
        await db.rollback()
    async with Session() as verify:
        assert (await verify.execute(select(StockSpot.code).where(
            StockSpot.code.in_(["600020", "600021", "600022"])
        ))).all() == []
