"""Synthetic temp SQLite + current QuoteRoundArchive parquet; no production I/O."""
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import pytest_asyncio
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.stock import BrokenLimitPool, LimitUpPool, QuoteRound, StockKline
from app.review.evidence_store import evidence_session
from app.review import market_batch as batch

DAY = date(2026, 9, 30)
AT = datetime(2026, 9, 30, 21, 45)
TABLES = [model.__table__ for model in (StockKline, LimitUpPool, BrokenLimitPool, QuoteRound)]


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_guard():
    # Override conftest: never initialize the application's DB, even its test DB.
    yield


@pytest.fixture(autouse=True)
def no_network_and_no_global_writer(monkeypatch):
    attempts = []

    def deny_network(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("network forbidden in market-batch tests")

    def deny_global(*args, **kwargs):
        attempts.append("global_writer_or_initializer")
        raise AssertionError("application DB/initializer forbidden")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    import app.db.session as sessions
    monkeypatch.setattr(sessions, "init_db", deny_global)
    event.listen(sessions.engine.sync_engine, "do_connect", deny_global)
    try:
        yield attempts
    finally:
        event.remove(sessions.engine.sync_engine, "do_connect", deny_global)
        assert attempts == [], "even a caught network/global DB attempt fails the audit"


def _bar(code, day=DAY, *, change=1.0, source="ths", opening=10.0, low=9.0):
    return StockKline(code=code, trade_date=day, open=opening, high=11.0, low=low,
        close=10.0, prev_close=10.0, change_pct=change, volume=1000,
        amount=10000.0, turnover=1.0, source=source)


@pytest_asyncio.fixture
async def isolated(tmp_path):
    path = tmp_path / "synthetic.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add_all([
            _bar("600001", change=8, opening=10.5),
            _bar("600002", change=-5, opening=9.5),
            _bar("600003", change=0, low=9.5),
            _bar("600004", change=None, opening=None, low=None),
            _bar("600007", change=1, source="unknown"),
            LimitUpPool(code="600001", trade_date=DAY, quarantined=False, source="tencent",
                        observed_at=AT, source_quote_at=AT, limit_up_time="09:35:00"),
            LimitUpPool(code="600005", trade_date=DAY, quarantined=False, source="tencent"),
            LimitUpPool(code="600008", trade_date=DAY, quarantined=True),
            BrokenLimitPool(code="600001", trade_date=DAY, final_state="reclosed", close_at_limit=True),
            BrokenLimitPool(code="600002", trade_date=DAY, final_state="broken", close_price=9.5),
            BrokenLimitPool(code="600006", trade_date=DAY, final_state="unknown"),
        ])
        for i in range(1, 9):
            db.add(_bar("600001", DAY - timedelta(days=i),
                change=-i, source="tencent_close" if i == 1 else "ths"))
        await db.commit()
    await engine.dispose()
    return path, tmp_path / "archive"


async def _insert(path, rows):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            db.add_all(rows)
            await db.commit()
    finally:
        await engine.dispose()


async def _change(path, statement):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as conn:
            await conn.execute(statement)
    finally:
        await engine.dispose()


async def _read(isolated, **kwargs):
    path, root = isolated
    async with evidence_session(f"sqlite+aiosqlite:///{path}") as db:
        assert db.autoflush is False and db.in_transaction()
        return await batch.read_market_batch(db, day=DAY, at=kwargs.pop("at", AT),
                                             archive_root=root, **kwargs)


def _round(committed, *, name=None, source="tencent", quality="ok", as_of=None):
    return QuoteRound(round_id=name or f"qr-{committed:%Y%m%dT%H%M%S%f}-test",
        trade_date=DAY, source=source, committed_at=committed, as_of_at=as_of or committed,
        expected_count=7, received_count=7, source_time_count=7,
        source_min_at=committed - timedelta(seconds=1), source_max_at=committed,
        received_min_at=committed, received_max_at=committed, quality_status=quality,
        coverage=1, source_time_coverage=1, config_version="synthetic-config",
        code_version="synthetic-code", created_at=AT, archive_status="ready")


def _minute_row(minute, round_row, code="600001", *, opening=10.0, close=11.0,
                volume=123.0, amount=1230.0):
    commit = round_row.committed_at.isoformat()
    return {"code": code, "minute": minute.isoformat(), "quote_round_id": round_row.round_id,
        "source_quote_at": commit, "received_at": commit, "updated_at": commit,
        "first_observed_at": commit, "open": opening, "high": max(opening, close),
        "low": min(opening, close), "close": close, "price_samples": 1,
        "price_basis": "sampled_quote_prices", "volume_basis": "cumulative_session",
        "volume": volume, "amount": amount}


def _write_minute(root, minute, rows):
    path = root / "minute" / f"trade_date={DAY}" / f"minute={minute:%H%M}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")
    return path


def _items(result):
    return {row["code"]: row for row in result["items"]}


@pytest.mark.asyncio
async def test_summary_all_winners_failures_missing_and_quarantine_denominators(isolated):
    result = await _read(isolated)
    assert result["section"] == "summary" and "items" not in result
    assert result["total_stored_universe"] == result["assessed_universe"] == 7
    assert result["counts_by_cohort"]["all"] == {
        "all": 7, "day_kline": 5, "missing_day_kline": 2, "valid_limit_up": 2,
        "broken_limit": 3, "limit_and_broken": 1, "daily_up": 2, "daily_flat": 1,
        "daily_down": 1, "daily_missing_return": 3, "daily_unassessed_before_close": 0}
    assert result["counts_by_cohort"]["rising_or_limit"]["all"] == 3
    assert result["counts_by_cohort"]["non_rising"]["all"] == 4
    assert result["quarantined_limit_rows_excluded"] == 1
    for cohort, count in (("all", 7), ("rising_or_limit", 3), ("non_rising", 4)):
        for histogram in result["grouped_histograms"][cohort].values():
            assert sum(histogram.values()) == count
        assert result["coverage_by_cohort"][cohort]["missing_or_unassessed_code_minutes"] == 240 * count
    assert result["grouped_histograms"]["all"]["sampled_first_last_direction"] == {"missing": 7}
    assert result["archive"]["missing_required_file_minutes"] == 240
    assert not isolated[1].exists(), "missing archive is never initialized"


@pytest.mark.asyncio
async def test_six_history_mixed_basis_helpers_and_missing_not_flat(isolated):
    result = await _read(isolated, section="features", limit=200)
    rows = _items(result)
    assert len(rows["600001"]["recent_daily_bars"]) == 6
    assert [row["trade_date"] for row in rows["600001"]["recent_daily_bars"]] == [
        (DAY - timedelta(days=i)).isoformat() for i in reversed(range(6))]
    assert rows["600001"]["source_basis_set"] == ["tencent_close", "ths"]
    assert rows["600001"]["mixed_or_unknown_basis"] is True
    assert rows["600001"]["opening_bucket"] == "high_ge_5"
    assert rows["600002"]["opening_bucket"] == "negative_-7_to_-1"
    assert rows["600002"]["recovery_shape"] == "negative_open_recovery"
    assert rows["600003"]["recovery_shape"] == "zero_axis_then_underwater_recovery"
    assert rows["600004"]["opening_bucket"] == "unknown"
    assert rows["600005"]["daily_status"] == "missing"
    assert rows["600005"]["cohort"] == "rising_or_limit"
    assert rows["600006"]["cohort"] == "non_rising"
    assert rows["600007"]["daily_basis_quality"] == "unknown"
    for row in rows.values():
        assert row["minute"]["first_last_return_pct"] is None
        assert row["minute"]["sampled_close"] is None
        assert row["minute"]["status"] == "missing"


@pytest.mark.asyncio
async def test_history_has_no_arbitrary_calendar_lookback_or_future_bar(isolated):
    path, _ = isolated
    await _insert(path, [_bar("600006", DAY - timedelta(days=250), source="ths"),
                         _bar("600006", DAY + timedelta(days=1), change=99)])
    row = _items(await _read(isolated, section="features"))["600006"]
    assert row["recent_bar_count"] == 1
    assert row["recent_daily_bars"][0]["trade_date"] == (DAY - timedelta(days=250)).isoformat()
    assert row["day_kline"] is None


@pytest.mark.asyncio
async def test_cohort_keysets_global_histograms_and_fingerprint_ignore_paging(isolated):
    summary = await _read(isolated)
    codes, cursor, fingerprints = [], "", set()
    for _ in range(4):
        page = await _read(isolated, section="features", cursor=cursor, limit=2)
        fingerprints.add(page["input_fingerprint"])
        codes.extend(row["code"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert codes == ["600001", "600002", "600003", "600004", "600005", "600006", "600007"]
    assert fingerprints == {summary["input_fingerprint"]}
    rising = await _read(isolated, section="features", cohort="rising_or_limit", limit=2)
    assert [row["code"] for row in rising["items"]] == ["600001", "600005"]
    following = await _read(isolated, section="features", cohort="rising_or_limit", cursor=rising["next_cursor"], limit=2)
    assert [row["code"] for row in following["items"]] == ["600007"]
    failures = await _read(isolated, section="features", cohort="non_rising", limit=200)
    assert [row["code"] for row in failures["items"]] == ["600002", "600003", "600004", "600006"]
    assert (await _read(isolated, section="features", cursor="999999"))["items"] == []
    assert "file_manifest" not in rising["archive"] and "quote_rounds" not in rising


@pytest.mark.asyncio
async def test_code_cap_retains_known_global_denominators_and_unassessed(monkeypatch, isolated):
    monkeypatch.setattr(batch, "MAX_CODES", 2)
    result = await _read(isolated)
    assert result["truncated"] is True and result["assessed_universe"] == 2
    assert result["unassessed_code_budget"] == 5
    assert result["counts_by_cohort"]["rising_or_limit"]["all"] == 3
    assert result["grouped_histograms"]["all"]["opening_bucket"]["unassessed_code_budget"] == 5
    assert result["grouped_histograms"]["rising_or_limit"]["daily_direction"]["unassessed_code_budget"] == 2
    assert result["coverage_by_cohort"]["all"]["missing_or_unassessed_code_minutes"] == 7 * 240
    page = await _read(isolated, section="features")
    assert len(page["items"]) == 2 and page["next_cursor"] is None
    assert page["unreturned_due_code_budget"] == 5
    assert result["unassessed_codes_reference"]["after_code"] == "600002"


@pytest.mark.asyncio
async def test_actual_archive_contract_one_read_per_file_ohlc_and_cumulative_endpoints(monkeypatch, isolated):
    from app.data.quote_round import QuoteRoundArchive

    path, root = isolated
    archiver = QuoteRoundArchive(root=root)
    moments = [datetime.combine(DAY, time(9, 30, 15)), datetime.combine(DAY, time(9, 30, 45)),
               datetime.combine(DAY, time(9, 31, 20))]
    rounds = [_round(moment) for moment in moments]
    for i, round_row in enumerate(rounds):
        records = [{"code": code, "name": "synthetic", "price": (10 + i) if code == "600001" else 20 - i,
            "prev_close": 1000.0, "change_pct": -99.0, "volume": 100 + i * 50,
            "amount": 1000 + i * 500, "source_quote_at": round_row.committed_at,
            "received_at": round_row.committed_at, "updated_at": round_row.committed_at,
            "quote_round_id": round_row.round_id} for code in ("600001", "600002")]
        archiver.write_round({"trade_date": DAY, "committed_at": round_row.committed_at,
                              "round_id": round_row.round_id}, records)
    await _insert(path, rounds)
    calls = []
    original = batch._read_file

    def counted(file, observed):
        calls.append(file)
        return original(file, observed)

    monkeypatch.setattr(batch, "_read_file", counted)
    result = await _read(isolated, section="features")
    assert len(calls) == len(set(calls)) == 2
    winner, failure = _items(result)["600001"]["minute"], _items(result)["600002"]["minute"]
    assert winner["observed_regular_minutes"] == 2
    assert winner["sampled_open"] == 10 and winner["sampled_high"] == 12
    assert winner["sampled_low"] == 10 and winner["sampled_close"] == 12
    assert winner["first_last_return_pct"] == 20
    assert winner["sampled_range_pct_of_first"] == 20
    assert failure["first_last_return_pct"] == -10
    assert winner["price_samples"] == 3
    assert winner["cumulative_session_volume_at_end"] == 200
    assert winner["cumulative_session_amount_at_end"] == 2000
    assert winner["minute_volume"] is winner["minute_amount"] is None
    assert winner["first_observed_at"] == moments[0].isoformat()
    assert result["archive"]["rows_read"] == 4
    assert result["archive"]["missing_required_file_minutes"] == 238
    assert result["intraday_daily_prev_close_comparison"].startswith("not_performed")
    assert result["full_exchange_bars"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,expected", [
    ({"quote_round_id": "missing"}, "round_missing_at_cutoff"),
    ({"source_quote_at": "2026-09-30T22:00:00"}, "future_code_clock"),
    ({"received_at": "2026-09-30T22:00:00"}, "future_code_clock"),
    ({"updated_at": "2026-09-30T22:00:00"}, "future_code_clock"),
    ({"first_observed_at": "2026-09-30T22:00:00"}, "future_code_clock"),
    ({"source_quote_at": ""}, "invalid_or_missing_code_clock"),
    ({"source_quote_at": "2026-09-30T09:30:00+08:00"}, "invalid_or_missing_code_clock"),
    ({"source_quote_at": "2026-09-29T09:30:00"}, "code_clock_trade_date_mismatch"),
    ({"updated_at": "2026-09-30T09:30:29"}, "round_commit_identity_mismatch"),
    ({"minute": "2026-09-30T09:31:00"}, "minute_identity_mismatch"),
    ({"first_observed_at": "2026-09-30T09:29:59"}, "first_or_received_clock_order"),
    ({"received_at": "2026-09-30T09:30:31"}, "first_or_received_clock_order"),
    ({"source_quote_at": "2026-09-30T09:30:00"}, "code_clock_outside_round_bounds"),
    ({"received_at": "2026-09-30T09:30:29"}, "code_clock_outside_round_bounds"),
    ({"price_basis": "adjusted_daily"}, "unsupported_sampled_price_basis"),
    ({"volume_basis": "minute_increment"}, "unsupported_volume_basis"),
    ({"close": None}, "missing_or_invalid_sampled_price"),
    ({"high": 9.0}, "invalid_sampled_ohlc"),
    ({"price_samples": 0}, "missing_or_invalid_price_samples"),
])
async def test_per_code_clock_round_and_basis_failures_remain_missing(isolated, mutation, expected):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    await _insert(path, [round_row])
    row = _minute_row(minute, round_row)
    row.update(mutation)
    _write_minute(root, minute, [row])
    result = await _read(isolated, section="features")
    sample = _items(result)["600001"]["minute"]
    assert sample["observed_regular_minutes"] == 0 and sample["first_last_return_pct"] is None
    assert sample["rejected_rows"] == {expected: 1}
    assert result["archive"]["rejected_rows"] == 1
    assert result["counts_by_cohort"]["non_rising"]["all"] == 4


@pytest.mark.asyncio
async def test_duplicate_code_minute_rejects_both_rows_and_counts_outside_universe(isolated):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    await _insert(path, [round_row])
    row = _minute_row(minute, round_row)
    _write_minute(root, minute, [row, dict(row, close=10.5), _minute_row(minute, round_row, "599999")])
    result = await _read(isolated, section="features")
    assert _items(result)["600001"]["minute"]["rejected_rows"] == {"ambiguous_code_minute": 2}
    assert result["archive"]["validated_rows"] == 1
    assert result["archive"]["outside_selected_universe_rows"] == 1
    assert result["archive"]["rows_read"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["committed_at", "as_of_at"])
async def test_committed_and_asof_round_visibility_are_independent(isolated, field):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    setattr(round_row, field, AT + timedelta(seconds=1))
    await _insert(path, [round_row])
    _write_minute(root, minute, [_minute_row(minute, round_row)])
    result = await _read(isolated, section="features")
    assert _items(result)["600001"]["minute"]["rejected_rows"] == {"round_missing_at_cutoff": 1}


@pytest.mark.asyncio
async def test_mixed_minute_sources_not_combined_and_degraded_rounds_remain_descriptive(isolated):
    path, root = isolated
    m1, m2 = datetime.combine(DAY, time(9, 30)), datetime.combine(DAY, time(9, 31))
    r1 = _round(m1 + timedelta(seconds=30), quality="degraded")
    r2 = _round(m2 + timedelta(seconds=30), source="other_raw_quote")
    await _insert(path, [r1, r2])
    _write_minute(root, m1, [_minute_row(m1, r1)])
    one = _items(await _read(isolated, section="features"))["600001"]["minute"]
    assert one["first_last_return_pct"] == 10
    assert one["round_quality_minute_counts"] == {"degraded": 1}
    assert "degraded_or_unknown_quote_round" in one["quality_flags"]
    _write_minute(root, m2, [_minute_row(m2, r2, opening=11, close=12)])
    mixed = _items(await _read(isolated, section="features"))["600001"]["minute"]
    assert mixed["observed_regular_minutes"] == 2
    assert mixed["status"] == "unassessed_mixed_basis"
    assert mixed["sampled_open"] is mixed["sampled_close"] is mixed["first_last_return_pct"] is None
    assert mixed["cumulative_session_volume_at_end"] is None


@pytest.mark.asyncio
async def test_daytime_withholds_close_pool_final_values_and_unfinished_or_future_file(isolated):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=15))
    await _insert(path, [round_row])
    _write_minute(root, minute, [_minute_row(minute, round_row)])
    at = minute + timedelta(seconds=45)
    result = await _read(isolated, section="features", at=at)
    assert result["daily_close_withheld"] is True
    assert result["counts_by_cohort"]["all"]["daily_unassessed_before_close"] == 7
    assert result["counts_by_cohort"]["rising_or_limit"]["all"] == 0
    for row in result["items"]:
        assert row["day_kline"] is None and row["daily_direction"] == "unassessed_before_close"
        assert row["opening_bucket"] == "unknown"
        assert all(bar["trade_date"] < DAY.isoformat() for bar in row["recent_daily_bars"])
        if row["broken_limit"]:
            assert "close_price" not in row["broken_limit"]
    assert result["archive"]["files_read"] == 0
    assert result["archive"]["status_counts"] == {"future_or_unfinished_minute": 1}
    ready = await _read(isolated, section="features", at=minute + timedelta(minutes=1))
    assert _items(ready)["600001"]["minute"]["observed_regular_minutes"] == 1
    assert _items(ready)["600001"]["day_kline"] is None
    future = root / "minute" / f"trade_date={DAY}" / "minute=1530.parquet"
    future.write_bytes(b"future file must not be decoded")
    result = await _read(isolated, at=datetime.combine(DAY, time(10)))
    assert result["archive"]["files_read"] == 1
    assert result["archive"]["status_counts"]["future_or_unfinished_minute"] == 1


@pytest.mark.asyncio
async def test_afterclose_expected240_missing_gaps_and_no_nonregular_close_backfill(isolated):
    path, root = isolated
    minutes = [datetime.combine(DAY, time(9, 30)), datetime.combine(DAY, time(9, 32)),
               datetime.combine(DAY, time(15))]
    rounds = [_round(minute + timedelta(seconds=30)) for minute in minutes]
    await _insert(path, rounds)
    for minute, round_row in zip(minutes, rounds):
        _write_minute(root, minute, [_minute_row(minute, round_row)])
    result = await _read(isolated, section="features")
    row = _items(result)["600001"]["minute"]
    assert row["expected_regular_minutes"] == 240 and row["observed_regular_minutes"] == 2
    assert row["missing_regular_minutes"] == 238
    assert row["last_minute"] == minutes[1].isoformat()
    assert row["non_regular_rows_excluded"] == result["archive"]["non_regular_rows"] == 1
    assert row["longest_missing_regular_minute_run"] == 120, "lunch is not a fabricated minute interval"


@pytest.mark.asyncio
async def test_full_regular_sample_coverage_still_not_complete_exchange_bars(isolated):
    path, root = isolated
    minutes = sorted(batch._regular_minutes(DAY))
    rounds = [_round(minute + timedelta(seconds=30)) for minute in minutes]
    await _insert(path, rounds)
    for minute, round_row in zip(minutes, rounds):
        _write_minute(root, minute, [_minute_row(minute, round_row)])
    result = await _read(isolated, section="features")
    row = _items(result)["600001"]["minute"]
    assert row["status"] == "all_expected_sampled_minutes_observed"
    assert row["observed_regular_minutes"] == 240 and row["missing_regular_minutes"] == 0
    assert result["archive"]["files_read"] == 240
    assert result["archive"]["intraminute_quote_coverage"] == "unknown"
    assert result["full_exchange_bars"] is False


@pytest.mark.asyncio
async def test_missing_schema_decode_oversize_and_future_file_budget_failures(isolated):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    await _insert(path, [round_row])
    _write_minute(root, minute, [_minute_row(minute, round_row)])
    _write_minute(root, minute + timedelta(minutes=1), [{"code": "600001"}])
    directory = root / "minute" / f"trade_date={DAY}"
    (directory / "minute=0932.parquet").write_bytes(b"not parquet")
    (directory / "minute=0933.parquet").write_bytes(b"x" * (batch.MAX_MINUTE_FILE_BYTES + 1))
    (directory / "minute=1005.parquet").write_bytes(b"future")
    result = await _read(isolated, at=datetime.combine(DAY, time(10)))
    archive = result["archive"]
    assert archive["files_read"] == 3
    assert archive["file_errors"] == {"missing_minute_contract_columns": 1,
        "archive_decode_or_io_error": 1, "archive_file_byte_budget": 1}
    assert archive["rejected_required_file_minutes"] == 3
    assert archive["missing_required_file_minutes"] == 26
    assert archive["status_counts"]["future_or_unfinished_minute"] == 1
    assert archive["unassessed_known_file_rows"] == 1
    assert archive["unassessed_unknown_row_files"] == 2
    assert archive["row_denominator_status"] == "partial_with_unassessed_files"
    assert archive["file_manifest"][0]["sha256"] == hashlib.sha256((directory / "minute=0930.parquet").read_bytes()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("budget,reason", [
    ("MAX_MINUTE_FILES", "archive_file_budget"),
    ("MAX_MINUTE_TOTAL_BYTES", "archive_total_byte_budget"),
    ("MAX_MINUTE_ROWS", "archive_row_budget"),
    ("MAX_MINUTE_DECODED_BYTES", "archive_decoded_byte_budget"),
])
async def test_each_archive_budget_is_explicit_not_silent(monkeypatch, isolated, budget, reason):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    await _insert(path, [round_row])
    _write_minute(root, minute, [_minute_row(minute, round_row)])
    monkeypatch.setattr(batch, budget, 0)
    result = await _read(isolated, section="features")
    assert result["archive"]["file_errors"] == {reason: 1}
    assert result["archive"]["rejected_required_file_minutes"] == 1
    assert _items(result)["600001"]["minute"]["status"] == "missing"
    assert _items(result)["600001"]["minute"]["missing_regular_minutes"] == 240


@pytest.mark.asyncio
async def test_manifest_reference_list_bounded_but_failure_counts_not_truncated(monkeypatch, isolated):
    _, root = isolated
    directory = root / "minute" / f"trade_date={DAY}"
    directory.mkdir(parents=True)
    for name in ("0930", "0931", "0932"):
        (directory / f"minute={name}.parquet").write_bytes(b"invalid")
    monkeypatch.setattr(batch, "MAX_MINUTE_FILES", 1)
    result = await _read(isolated)
    assert result["archive"]["source_file_count"] == 3
    assert len(result["archive"]["file_manifest"]) == 1
    assert result["archive"]["manifest_omitted_files"] == 2
    assert result["archive"]["file_errors"]["archive_file_budget"] == 2
    assert result["archive"]["files_read"] == 1


@pytest.mark.asyncio
async def test_symlink_and_invalid_filename_never_read(isolated):
    _, root = isolated
    directory = root / "minute" / f"trade_date={DAY}"
    directory.mkdir(parents=True)
    target = root.parent / "must-not-read"
    target.write_bytes(b"not evidence")
    (directory / "minute=0930.parquet").symlink_to(target)
    (directory / "minute=2999.parquet").write_bytes(b"invalid clock")
    (directory / "minute=bogus.parquet").write_bytes(b"invalid name")
    result = await _read(isolated)
    assert result["archive"]["files_read"] == 0
    assert result["archive"]["file_errors"] == {"unsafe_archive_path": 1, "invalid_minute_filename": 2}
    assert result["archive"]["rejected_required_file_minutes"] == 1


@pytest.mark.asyncio
async def test_file_replacement_race_is_unassessed_and_reader_does_not_write(monkeypatch, isolated):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    await _insert(path, [round_row])
    file = _write_minute(root, minute, [_minute_row(minute, round_row)])
    original = batch._read_file

    def raced(target, observed):
        # Synthetic concurrent producer; not a write in the reader.
        target.write_bytes(b"changed")
        return original(target, observed)

    monkeypatch.setattr(batch, "_read_file", raced)
    result = await _read(isolated)
    assert result["archive"]["file_errors"] == {"archive_changed_during_read": 1}
    assert result["coverage_by_cohort"]["all"]["observed_code_minutes"] == 0
    assert file.read_bytes() == b"changed"


@pytest.mark.asyncio
async def test_mutable_projection_and_manifest_change_fingerprint_without_false_pit(monkeypatch, isolated):
    from app.review import evidence_readers, evidence_store

    first = await _read(isolated, section="features")
    monkeypatch.setattr(evidence_readers, "local_now", lambda: datetime(2026, 10, 1, 9))
    monkeypatch.setattr(evidence_store, "local_now", lambda: datetime(2026, 10, 1, 9))
    later_read = await _read(isolated)
    assert first["read_at"] != later_read["read_at"]
    assert first["input_fingerprint"] == later_read["input_fingerprint"]
    assert first["historical_PIT"] is False and first["availability_at"] is None
    assert first["historical_stock_identity"].startswith("unknown")
    assert first["pool_membership_basis"].startswith("current_mutable")
    assert first["eligible_for_historical_training_or_strategy_tuning"] is False
    assert first["cache"]["enabled"] is False
    path, root = isolated
    await _change(path, update(StockKline).where(StockKline.code == "600001", StockKline.trade_date == DAY)
                  .values(close=10.8, change_pct=8.1))
    revised = await _read(isolated)
    assert revised["input_fingerprint"] != first["input_fingerprint"]
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    await _insert(path, [round_row])
    file = _write_minute(root, minute, [_minute_row(minute, round_row)])
    observed = await _read(isolated)
    _write_minute(root, minute, [_minute_row(minute, round_row, close=10.5)])
    replaced = await _read(isolated)
    assert observed["input_fingerprint"] != replaced["input_fingerprint"]
    assert observed["archive"]["file_manifest"][0]["sha256"] != replaced["archive"]["file_manifest"][0]["sha256"]
    assert file.is_file()
    await _change(path, update(QuoteRound).where(QuoteRound.round_id == round_row.round_id).values(quality_status="degraded"))
    assert (await _read(isolated))["input_fingerprint"] != replaced["input_fingerprint"]


@pytest.mark.asyncio
async def test_200_dense_features_compact_under_one_mib(isolated):
    path, _ = isolated
    rows = []
    for i in range(200):
        code = f"000{i:03d}"
        for history_day in range(6):
            rows.append(_bar(code, DAY - timedelta(days=history_day), source="tencent_close" if history_day == 1 else "ths"))
        rows.extend([LimitUpPool(code=code, trade_date=DAY, quarantined=False,
            source="tencent", source_version="test-v1", source_quote_at=AT, observed_at=AT,
            consecutive_days=2, limit_up_time="09:35:00", limit_up_price=11, break_count=2),
            BrokenLimitPool(code=code, trade_date=DAY, source="tencent", source_version="test-v1",
                source_quote_at=AT, observed_at=AT, limit_up_time="09:35:00", break_time="10:30:00",
                close_price=10.5, close_at_limit=False, final_state="broken")])
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    rows.append(round_row)
    await _insert(path, rows)
    _write_minute(isolated[1], minute, [_minute_row(minute, round_row, f"000{i:03d}") for i in range(200)])
    result = await _read(isolated, section="features", limit=200)
    assert result["returned"] == 200 and result["next_cursor"] is not None
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    assert len(encoded) < 1024 * 1024
    assert all(row["recent_bar_count"] == 6 for row in result["items"])
    assert all("quote_round_id" not in row["minute"] for row in result["items"])
    assert all(row["minute"]["observed_regular_minutes"] == 1 for row in result["items"])
    print(f"200_dense_features_bytes={len(encoded)}")


@pytest.mark.asyncio
async def test_query_only_guard_no_autoflush_no_commit_and_all_local_content_unchanged(isolated):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30))
    await _insert(path, [round_row])
    file = _write_minute(root, minute, [_minute_row(minute, round_row)])
    before_db = hashlib.sha256(path.read_bytes()).hexdigest()
    before_file = hashlib.sha256(file.read_bytes()).hexdigest()
    before_paths = sorted(str(item) for item in root.rglob("*"))
    statements = []
    async with evidence_session(f"sqlite+aiosqlite:///{path}") as db:
        event.listen(db.bind.sync_engine, "before_cursor_execute",
                     lambda conn, cursor, sql, parameters, context, many: statements.append(sql))
        db.add(_bar("699999"))  # Never flushed even when a pending object exists.
        for sql in ("UPDATE stock_kline SET close=0", "CREATE TABLE forbidden(a)",
                    "DELETE FROM quote_round", "PRAGMA query_only=OFF"):
            with pytest.raises(RuntimeError, match="readonly_statement_denied"):
                await db.execute(text(sql))
        await batch.read_market_batch(db, day=DAY, at=AT, archive_root=root)
        assert db.new and db.in_transaction()
    assert statements and all(sql.lstrip().split()[0].upper() == "SELECT" for sql in statements)
    assert before_db == hashlib.sha256(path.read_bytes()).hexdigest()
    assert before_file == hashlib.sha256(file.read_bytes()).hexdigest()
    assert before_paths == sorted(str(item) for item in root.rglob("*"))


@pytest.mark.asyncio
async def test_empty_storage_returns_unknown_not_empty_exchange(tmp_path):
    path = tmp_path / "empty.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=TABLES))
    await engine.dispose()
    result = await _read((path, tmp_path / "absent"))
    assert result["status"] == "unavailable" and result["total_stored_universe"] == 0
    assert result["reason"].startswith("empty_stored_universe_not_confirmed")
    for histogram in result["grouped_histograms"]["all"].values():
        assert histogram == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"section": "raw"}, {"cohort": "winner_only"}, {"limit": 0}, {"limit": 201},
    {"limit": True}, {"cursor": 3},
])
async def test_options_rejected_without_silent_clamping(isolated, kwargs):
    with pytest.raises(ValueError):
        await _read(isolated, **kwargs)


@pytest.mark.asyncio
async def test_aware_cutoff_normalized_and_future_cutoff_denied(isolated):
    result = await _read(isolated, at=AT.replace(tzinfo=timezone(timedelta(hours=8))))
    assert result["as_of"] == AT.isoformat()
    with pytest.raises(ValueError, match="future"):
        await _read(isolated, at=datetime(2999, 1, 1))


@pytest.mark.asyncio
async def test_requires_caller_transaction_and_no_autoflush(isolated):
    path, root = isolated
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with async_sessionmaker(engine, autoflush=False)() as db:
            with pytest.raises(ValueError, match="caller-owned"):
                await batch.read_market_batch(db, day=DAY, at=AT, archive_root=root)
        async with async_sessionmaker(engine, autoflush=True)() as db:
            await db.execute(text("BEGIN"))
            with pytest.raises(ValueError, match="caller-owned"):
                await batch.read_market_batch(db, day=DAY, at=AT, archive_root=root)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_nonfinite_daily_returns_are_missing_not_rising_or_flat(isolated):
    path, _ = isolated
    await _change(path, update(StockKline).where(StockKline.code == "600007", StockKline.trade_date == DAY)
                  .values(change_pct=float("inf")))
    result = await _read(isolated, section="features")
    assert result["counts_by_cohort"]["all"]["daily_missing_return"] == 4
    assert result["counts_by_cohort"]["rising_or_limit"]["all"] == 2
    assert result["counts_by_cohort"]["non_rising"]["all"] == 5
    assert _items(result)["600007"]["daily_direction"] == "missing"
    assert result["coverage_by_cohort"]["all"]["missing_or_unassessed_code_minutes"] == 7 * 240
    await _change(path, update(StockKline).where(StockKline.code == "600007", StockKline.trade_date == DAY)
                  .values(change_pct=None))
    missing = await _read(isolated)
    assert missing["input_fingerprint"] != result["input_fingerprint"], "invalid float input differs from NULL"
    assert missing["counts_by_cohort"] == result["counts_by_cohort"]


@pytest.mark.asyncio
async def test_round_wrong_source_day_not_used_even_if_before_cutoff(isolated):
    path, root = isolated
    minute = datetime.combine(DAY, time(9, 30))
    round_row = _round(minute + timedelta(seconds=30), as_of=AT - timedelta(days=1))
    await _insert(path, [round_row])
    _write_minute(root, minute, [_minute_row(minute, round_row)])
    result = await _read(isolated, section="features")
    assert _items(result)["600001"]["minute"]["rejected_rows"] == {"round_clock_mismatch": 1}
    assert result["archive"]["validated_rows"] == 0


@pytest.mark.asyncio
async def test_total_byte_limit_prevents_second_read_and_preserves_all_missing(monkeypatch, isolated):
    path, root = isolated
    first, second = datetime.combine(DAY, time(9, 30)), datetime.combine(DAY, time(9, 31))
    r1, r2 = _round(first + timedelta(seconds=30)), _round(second + timedelta(seconds=30))
    await _insert(path, [r1, r2])
    f1 = _write_minute(root, first, [_minute_row(first, r1)])
    _write_minute(root, second, [_minute_row(second, r2)])
    monkeypatch.setattr(batch, "MAX_MINUTE_TOTAL_BYTES", f1.stat().st_size)
    result = await _read(isolated, section="features")
    assert result["archive"]["files_read"] == result["archive"]["read_attempts"] == 1
    assert result["archive"]["bytes_read"] == result["archive"]["bytes_reserved"] == f1.stat().st_size
    assert result["archive"]["file_errors"] == {"archive_total_byte_budget": 1}
    assert _items(result)["600001"]["minute"]["observed_regular_minutes"] == 1
    assert _items(result)["600001"]["minute"]["missing_regular_minutes"] == 239


@pytest.mark.asyncio
async def test_missing_schema_never_initializes_or_recovers_tables(tmp_path):
    from sqlalchemy.exc import OperationalError

    path = tmp_path / "no-tables.db"
    path.touch()
    before = path.read_bytes()
    async with evidence_session(f"sqlite+aiosqlite:///{path}") as db:
        with pytest.raises(OperationalError, match="no such table"):
            await batch.read_market_batch(db, day=DAY, at=AT, archive_root=tmp_path / "absent")
    assert path.read_bytes() == before
    assert not (tmp_path / "absent").exists()


def test_contract_limits_and_only_two_owned_new_files():
    assert batch.MAX_CODES == 20000 and batch.MAX_RECENT_BARS == 6
    assert batch.MAX_MINUTE_FILES == 300 and batch.MAX_MINUTE_FILE_BYTES == 4 * 1024 * 1024
    assert batch.MAX_MINUTE_TOTAL_BYTES == 128 * 1024 * 1024
    assert batch.MAX_FEATURE_ROWS == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("daily_allowed", [True, False])
async def test_indexed_recent_seek_matches_window_reference_without_full_history_rank(isolated, daily_allowed):
    from sqlalchemy import func
    path, _ = isolated
    selected = select(batch._population(DAY).c.code).order_by("code").limit(batch.MAX_CODES).subquery()
    history_day = DAY if daily_allowed else DAY - timedelta(days=1)
    rank = func.row_number().over(partition_by=StockKline.code,
        order_by=(StockKline.trade_date.desc(), StockKline.id.desc())).label("rank")
    ranked = select(*(getattr(StockKline, key) for key in batch.DAILY_FIELDS), rank).join(
        selected, StockKline.code == selected.c.code).where(StockKline.trade_date <= history_day).subquery()
    reference = select(*(ranked.c[key] for key in batch.DAILY_FIELDS)).where(
        ranked.c.rank <= batch.MAX_RECENT_BARS).order_by(ranked.c.code, ranked.c.trade_date, ranked.c.id)
    async with evidence_session(f"sqlite+aiosqlite:///{path}") as db:
        old_rows = [dict(row) for row in (await db.execute(reference)).mappings()]
        statements = []
        event.listen(db.bind.sync_engine, "before_cursor_execute",
            lambda conn, cursor, sql, params, ctx, many: statements.append(sql))
        actual, fingerprint = await batch._history(db, selected, DAY, daily_allowed)
    expected, digest = {}, hashlib.sha256()
    for row in old_rows:
        batch._hash_into(digest, row)
        expected.setdefault(row["code"], []).append(batch.owned(row))
    assert actual == expected and fingerprint == digest.hexdigest()
    assert len(statements) == 1
    assert "recent_kline" in statements[0] and "LIMIT" in statements[0]
    assert "row_number" not in statements[0].lower()


@pytest.mark.asyncio
async def test_summary_reference_sample_does_not_hide_full_manifest_failure_counts(isolated):
    _, root = isolated
    directory = root / "minute" / f"trade_date={DAY}"
    directory.mkdir(parents=True)
    for i in range(20):
        (directory / f"minute=09{30+i:02d}.parquet").write_bytes(b"invalid")
    result = await _read(isolated)
    archive = result["archive"]
    assert archive["source_file_count"] == archive["files_read"] == 20
    assert len(archive["file_manifest"]) == batch.MAX_MANIFEST_REFS == 12
    assert archive["manifest_omitted_files"] == 8
    assert archive["file_errors"] == {"archive_decode_or_io_error": 20}
    assert sum(archive["status_counts"].values()) == 20
    assert len(archive["file_manifest_sha256"]) == 64
    assert "cover_all_discovered_paths" in archive["manifest_scope"]
