"""Synthetic isolated tests: breakdown retention is not live-source recovery."""
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import paper, tenbagger
from app.data.main_fund import current_main_fund_evidence, fund_order_breakdown, load_current_main_fund_map
from app.data.scheduler import DataScheduler
from app.db.session import Base, _ensure_close_quality_columns
from app.models.stock import FundFlow, StockSpot
from app.signal.anomaly_scanner import _build_large_order_inflow_context
from test_fund_downstream_contract import NOW, fund, scheduler_db_env

NEW_FIELDS = ("super_net_inflow", "super_net_inflow_pct", "big_net_inflow_pct",
              "mid_net_inflow_pct", "small_net_inflow_pct")
VALUES = {"super_net_inflow": -12_000_000, "super_net_inflow_pct": -1.2,
          "big_net_inflow": 22_000_000, "big_net_inflow_pct": 2.2,
          "mid_net_inflow": 0, "mid_net_inflow_pct": 0,
          "small_net_inflow": -10_000_000, "small_net_inflow_pct": -1.0}
LABELS = {"super": "超大单", "big": "大单", "mid": "中单", "small": "小单"}


def frame(now, prefix="今日", **overrides):
    row = {"代码": "000001", "名称": "隔离合成", "今日主力净流入-净额": 10_000_000,
           "今日主力净流入-净占比": 1.0, "source_quote_at": now - timedelta(seconds=3),
           "received_at": now - timedelta(seconds=2)}
    for key, value in {**VALUES, **overrides}.items():
        size = key.split("_")[0]
        unit = "净占比" if key.endswith("_pct") else "净额"
        row[f"{prefix}{LABELS[size]}净流入-{unit}"] = value
    return pd.DataFrame([row])


def parse(df, now):
    return DataScheduler._parse_individual_fund_flow_df(df, now.date(), observed_at=now)


@pytest.mark.parametrize("prefix", ["", "今日"])
def test_all_breakdowns_keep_provider_units_zero_and_negative_values(prefix):
    now = datetime.now()
    record = parse(frame(now, prefix), now)[0]
    for key, value in VALUES.items():
        assert record[key] == value
    evidence = current_main_fund_evidence(record, trade_date=now.date(), decision_at=now)
    assert {key: evidence[key] for key in VALUES} == VALUES
    assert evidence["source_quote_at"] == record["source_quote_at"]
    assert evidence["received_at"] == record["received_at"]


@pytest.mark.parametrize("field", list(VALUES))
@pytest.mark.parametrize("bad", [None, True, False, "-", float("nan"), float("inf")])
def test_invalid_optional_fields_are_null_not_zero_or_inferred(field, bad):
    now = datetime.now()
    record = parse(frame(now, **{field: bad}), now)[0]
    assert record[field] is None
    assert record["main_net_inflow"] == 10_000_000  # main coverage contract unchanged
    raw = {**record, field: bad}
    evidence = current_main_fund_evidence(raw, trade_date=now.date(), decision_at=now)
    assert evidence[field] is None


def load_migration():
    path = Path(__file__).resolve().parents[1] / "alembic/versions/027_fund_order_breakdown.py"
    spec = importlib.util.spec_from_file_location("fund_breakdown_027_test", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.mark.asyncio
@pytest.mark.parametrize("compat_first", [False, True])
async def test_migration_and_startup_are_idempotent_nullable_and_keep_legacy_rows(tmp_path, compat_first):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_026.db'}")
    migration = load_migration()

    def make_legacy(conn):
        # Existing 026 schema, original uniqueness and an untouched history marker.
        metadata = sa.MetaData()
        sa.Table("fund_flow", metadata,
                 *[sa.Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable)
                   for c in FundFlow.__table__.columns if c.name not in NEW_FIELDS],
                 sa.UniqueConstraint("code", "trade_date", name="uq_fund_flow_code_date"))
        metadata.create_all(conn)
        # The compatibility function visits these other existing app tables too.
        Base.metadata.create_all(conn)
        conn.execute(sa.text(
            "INSERT INTO fund_flow (id,code,trade_date,main_net_inflow,main_net_inflow_pct,"
            "big_net_inflow,source,observed_at) VALUES "
            "(11,'000001','2026-09-04',123,0,-7,'legacy','2026-09-04T15:01:02.123456')"))
        migration.op = Operations(MigrationContext.configure(conn))

    old_columns = [c.name for c in FundFlow.__table__.columns if c.name not in NEW_FIELDS]
    old_query = sa.text("SELECT " + ",".join(old_columns) + " FROM fund_flow")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(make_legacy)
            before = (await conn.execute(old_query)).all()
            if compat_first:
                await _ensure_close_quality_columns(conn)
            await conn.run_sync(lambda sync: migration.upgrade())
            await conn.run_sync(lambda sync: migration.upgrade())
            await _ensure_close_quality_columns(conn)
            await _ensure_close_quality_columns(conn)
            assert (await conn.execute(old_query)).all() == before
            assert (await conn.execute(sa.text("SELECT " + ",".join(NEW_FIELDS) +
                                               " FROM fund_flow"))).one() == (None,) * 5
            columns = await conn.run_sync(lambda sync: sa.inspect(sync).get_columns("fund_flow"))
            for column in columns:
                if column["name"] in NEW_FIELDS:
                    assert column["nullable"] is True
                    assert column["default"] is None
            uniques = await conn.run_sync(lambda sync: sa.inspect(sync).get_unique_constraints("fund_flow"))
            assert any(item["column_names"] == ["code", "trade_date"] for item in uniques)
            await conn.run_sync(lambda sync: migration.downgrade())
            assert (await conn.execute(old_query)).all() == before
            columns = await conn.run_sync(lambda sync: sa.inspect(sync).get_columns("fund_flow"))
            assert not set(NEW_FIELDS) & {column["name"] for column in columns}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_raw_upsert_to_shared_projection_and_consumers_never_reuses_old_breakdowns(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'breakdown.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now()
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
    monkeypatch.setattr(tenbagger, "resolve_latest_trade_date", AsyncMock(return_value=now.date()))
    forbidden = AsyncMock(side_effect=AssertionError("read consumers must not fetch/cache/commit"))
    monkeypatch.setattr(tenbagger.EastMoneySource, "get_individual_fund_flow", forbidden)
    try:
        async with sessions() as db:
            records = parse(frame(now), now)
            await DataScheduler._batch_upsert(db, FundFlow, records, ["code", "trade_date"])
            await db.commit()
            current = (await load_current_main_fund_map(db, trade_date=now.date(), decision_at=now))["000001"]
            assert {key: current[key] for key in VALUES} == VALUES
            frozen = dict(current)
            stored_before = (await db.execute(sa.text("SELECT * FROM fund_flow"))).all()
            with monkeypatch.context() as patch:
                patch.setattr(db, "commit", forbidden)
                patch.setattr(db, "flush", forbidden)
                snapshot = await tenbagger.prewarm_eastmoney_main_fund_snapshot(db, trade_date=now.date())
                item = snapshot["items"]["000001"]
                assert {key: item[key] for key in VALUES} == VALUES
                detail = tenbagger._merge_current_main_fund_detail({"unrelated": 7}, item)
                assert {key: detail[key] for key in VALUES} == VALUES
                assert detail["unrelated"] == 7
                candidate = {"detail": {key: 999 for key in VALUES}}
                paper._bind_main_fund_evidence(candidate, current)
                assert {key: candidate["detail"][key] for key in VALUES} == VALUES
            assert (await db.execute(sa.text("SELECT * FROM fund_flow"))).all() == stored_before
            forbidden.assert_not_awaited()
            # A real later row that omits breakdowns clears old values; it does
            # not relabel yesterday's / earlier-round components with new clocks.
            later = datetime.now()
            minimal = frame(later).drop(columns=[column for column in frame(later).columns
                                                 if any(label in column for label in LABELS.values())])
            records = parse(minimal, later)
            assert {key: records[0][key] for key in VALUES} == dict.fromkeys(VALUES)
            await DataScheduler._batch_upsert(db, FundFlow, records, ["code", "trade_date"])
            await db.commit()
            current = (await load_current_main_fund_map(db, trade_date=now.date(), decision_at=later))["000001"]
            assert {key: current[key] for key in VALUES} == dict.fromkeys(VALUES)
            assert {key: frozen[key] for key in VALUES} == VALUES
            assert current["main_net_inflow"] == 10_000_000
    finally:
        await engine.dispose()


@pytest.mark.parametrize("bad", [None, True, False, float("nan"), float("inf"), "-"])
@pytest.mark.parametrize("field", ["super_net_inflow", "big_net_inflow"])
def test_partial_large_order_breakdown_does_not_manufacture_positive_confirmation(field, bad):
    item = {"main_net_inflow": 80_000_000, "main_net_inflow_pct": 4.2,
            "super_net_inflow": 18_000_000, "big_net_inflow": 22_000_000,
            "is_stale": False, field: bad}
    result = _build_large_order_inflow_context(item, traded_amount=600_000_000)
    assert result["large_order_inflow_confirmed"] is False
    assert result["large_order_net_inflow"] is None
    assert result["large_order_breakdown_status"] == "unknown"


@pytest.mark.parametrize("super_amount,confirmed,total", [(0, True, 22_000_000),
                                                          (-21_000_000, False, 1_000_000)])
def test_measured_zero_and_negative_components_use_original_amount_gate(super_amount, confirmed, total):
    item = {"main_net_inflow": 80_000_000, "main_net_inflow_pct": 4.2,
            "super_net_inflow": super_amount, "big_net_inflow": 22_000_000, "is_stale": False}
    result = _build_large_order_inflow_context(item, traded_amount=600_000_000)
    assert result["large_order_inflow_confirmed"] is confirmed
    assert result["large_order_net_inflow"] == total
    assert result["large_order_breakdown_status"] == "known"
    assert _build_large_order_inflow_context({**item, "is_stale": True})["large_order_inflow_confirmed"] is False


def test_overflowing_component_sum_is_not_an_infinite_confirmation():
    item = {"main_net_inflow": 80_000_000, "main_net_inflow_pct": 4.2,
            "super_net_inflow": 1e308, "big_net_inflow": 1e308, "is_stale": False}
    result = _build_large_order_inflow_context(item)
    assert result["large_order_net_inflow"] is None
    assert result["large_order_inflow_confirmed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("direction,super_amount,big_amount,alignment", [
    (1, 100_000_000, 200_000_000, True),
    (-1, -100_000_000, -200_000_000, True),
    (1, -100_000_000, 400_000_000, False),
    (1, 0, 300_000_000, False),
    (1, None, 200_000_000, False),
    (1, 100_000_000, None, False),
])
async def test_scanner_passes_complete_nullable_breakdown_to_real_detector(
    scheduler_db_env, monkeypatch, injected, direction, super_amount, big_amount, alignment,
):
    from unittest.mock import Mock
    from app.models.stock import StockSpot
    from app.signal.anomaly_scanner import AnomalyScanner

    async with scheduler_db_env() as db:
        row = fund()
        row.main_net_inflow = direction * 300_000_000
        row.main_net_inflow_pct = direction * 8
        values = {**VALUES, "super_net_inflow": super_amount,
                  "super_net_inflow_pct": None if super_amount is None else super_amount / 1e8,
                  "big_net_inflow": big_amount,
                  "big_net_inflow_pct": None if big_amount is None else big_amount / 1e8}
        for key, value in values.items():
            setattr(row, key, value)
        db.add(row)
        db.add(StockSpot(code="000001", name="隔离合成", price=10, open=10, high=10, low=10,
                         prev_close=10, limit_up=11, limit_down=9, change_pct=0,
                         volume=100, amount=1000, volume_ratio=1, turnover=1, updated_at=NOW))
        await db.commit()
        before = (await db.execute(sa.text("SELECT * FROM fund_flow"))).all()
        scanner = AnomalyScanner()
        captured = []
        real_detect = scanner.capital_detector.detect

        def observe(**kwargs):
            result = real_detect(**kwargs)
            captured.extend(result)
            return result

        spy = Mock(side_effect=observe)
        monkeypatch.setattr(scanner.capital_detector, "detect", spy)
        kwargs = {}
        if injected:
            qualified = (await load_current_main_fund_map(
                db, trade_date=NOW.date(), decision_at=NOW))["000001"]
            kwargs["current_fund_map"] = {"000001": {
                **qualified, "provider_source": qualified["source"], "source": "eastmoney_main_fund",
            }}
        # Exercise actual scan -> qualification -> detector, not a helper-only test.
        events = await scanner.scan_market(db, target_date=NOW.date(), as_of_at=NOW, **kwargs)
        spy.assert_called_once()
        payload = spy.call_args.kwargs["fund_data"]
        assert {key: payload[key] for key in VALUES} == values
        assert payload["main_net_inflow"] == direction * 300_000_000
        assert payload["main_net_inflow_pct"] == direction * 8
        signals = [item for item in captured if item.anomaly_type == "main_inflow"]
        assert len(signals) == 1
        assert signals[0].detail["large_order_alignment"] is alignment
        assert signals[0].score == (75 if alignment else 70)  # existing bonus and thresholds
        capital_events = [item for item in events if item.event_type == "capital"
                          and item.detail.get("capital_anomaly_type") == "main_inflow"]
        assert len(capital_events) == 1
        assert {key: capital_events[0].detail[key] for key in VALUES} == values
        assert capital_events[0].detail["large_order_alignment"] is alignment
        assert capital_events[0].score == signals[0].score
        assert (await db.execute(sa.text("SELECT * FROM fund_flow"))).all() == before


def test_absent_breakdowns_are_not_reconstructed_from_main_amount():
    item = {"main_net_inflow": 80_000_000, "main_net_inflow_pct": 4.2, "is_stale": False}
    assert fund_order_breakdown(item) == dict.fromkeys(VALUES)
    assert _build_large_order_inflow_context(item)["large_order_inflow_confirmed"] is False


@pytest.mark.asyncio
async def test_scheduled_repeated_collection_updates_and_clears_same_row_components(monkeypatch):
    from types import SimpleNamespace
    from app.data import scheduler as module

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(FundFlow.__table__.create)
        await conn.run_sync(StockSpot.__table__.create)
    scheduler = DataScheduler()
    scheduler._tradeable_codes = ["000001"]
    frames = [frame(datetime.now()),
              frame(datetime.now(), super_net_inflow=-32_000_000, super_net_inflow_pct=-3.2),
              frame(datetime.now(), **dict.fromkeys(VALUES))]
    for item in frames:
        item.attrs.update(fund_flow_source="tencent",
                          fund_flow_source_version="tencent_hsfundtab_v1")
    fetch = AsyncMock(side_effect=frames)
    fallback = AsyncMock(side_effect=AssertionError("old individual sources are forbidden"))
    scheduler._sources = {
        "tencent": SimpleNamespace(get_individual_fund_flow=fetch),
        "eastmoney": SimpleNamespace(get_individual_fund_flow=fallback),
        "akshare": SimpleNamespace(get_individual_fund_flow=fallback),
    }
    monkeypatch.setattr(module, "async_session", sessions)
    monkeypatch.setattr(module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "_collect_concept_fund_flow_bounded", AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(module.data_quality_guard, "record_success", AsyncMock())
    monkeypatch.setattr(module.data_quality_guard, "record_failure", AsyncMock())
    expected_frames = [VALUES, {**VALUES, "super_net_inflow": -32_000_000,
                               "super_net_inflow_pct": -3.2}, dict.fromkeys(VALUES)]
    try:
        for expected in expected_frames:
            await scheduler._intraday_slow()
            async with sessions() as db:
                now = datetime.now()
                current = (await load_current_main_fund_map(
                    db, trade_date=now.date(), decision_at=now))["000001"]
                assert {key: current[key] for key in VALUES} == expected
                assert current["source"] == "tencent"
                assert current["source_version"] == "tencent_hsfundtab_v1"
                assert await db.scalar(sa.select(sa.func.count()).select_from(FundFlow)) == 1
        assert fetch.await_count == 3
        fallback.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("regression", ["source_minute", "receipt"])
async def test_scheduled_fresh_but_regressing_response_preserves_newer_row(monkeypatch, regression):
    from types import SimpleNamespace
    from app.data import scheduler as module

    now = datetime.now()
    newer = frame(now)
    newer["source_quote_at"] = now - timedelta(seconds=120)
    newer["received_at"] = now - timedelta(seconds=30)
    older = frame(now, **dict.fromkeys(VALUES))
    older["今日主力净流入-净额"] = -99_000_000
    older["今日主力净流入-净占比"] = -9.9
    older["source_quote_at"] = now - timedelta(
        seconds=180 if regression == "source_minute" else 120)
    older["received_at"] = now - timedelta(
        seconds=1 if regression == "source_minute" else 60)
    for item in (newer, older):
        item.attrs.update(fund_flow_source="tencent",
                          fund_flow_source_version="tencent_hsfundtab_v1")
        # Both responses are individually fresh and valid: rejection must come
        # from the persisted monotonicity check, not the ordinary 600s gate.
        parsed = DataScheduler._parse_individual_fund_flow_df(
            item, now.date(), source="tencent", source_version="tencent_hsfundtab_v1",
            observed_at=now)
        assert len(parsed) == 1

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    scheduler = DataScheduler()
    scheduler._tradeable_codes = ["000001"]
    fetch = AsyncMock(side_effect=[newer, older])
    forbidden = AsyncMock(side_effect=AssertionError("no old-source fallback"))
    scheduler._sources = {
        "tencent": SimpleNamespace(get_individual_fund_flow=fetch),
        "eastmoney": SimpleNamespace(get_individual_fund_flow=forbidden),
        "akshare": SimpleNamespace(get_individual_fund_flow=forbidden),
    }
    monkeypatch.setattr(module, "async_session", sessions)
    monkeypatch.setattr(module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "_collect_concept_fund_flow_bounded",
                        AsyncMock(return_value=pd.DataFrame()))
    monkeypatch.setattr(module.data_quality_guard, "record_success", AsyncMock())
    monkeypatch.setattr(module.data_quality_guard, "record_failure", AsyncMock())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(FundFlow.__table__.create)
            await conn.run_sync(StockSpot.__table__.create)
        await scheduler._intraday_slow()
        async with sessions() as db:
            before = (await db.execute(sa.text("SELECT * FROM fund_flow"))).all()
            assert len(before) == 1
            frozen = (await load_current_main_fund_map(
                db, trade_date=now.date(), decision_at=datetime.now()))["000001"]
            assert frozen["main_net_inflow"] == 10_000_000
            assert {key: frozen[key] for key in VALUES} == VALUES

        await scheduler._intraday_slow()
        async with sessions() as db:
            # Preserve every numeric field, source/version and all three clocks,
            # including observed_at: late receipt cannot whitewash old source time.
            assert (await db.execute(sa.text("SELECT * FROM fund_flow"))).all() == before
            current = (await load_current_main_fund_map(
                db, trade_date=now.date(), decision_at=datetime.now()))["000001"]
            assert current == frozen
        assert fetch.await_count == 2
        assert all(call.args == (["000001"],) for call in fetch.await_args_list)
        forbidden.assert_not_awaited()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_keyed_http_source_frame_retains_all_breakdowns_through_scheduler(monkeypatch):
    import httpx
    from app.data.sources.eastmoney_source import EastMoneySource

    # MockTransport only: not a captured live vendor sample or a recovery probe.
    raw = {"f124": int(datetime.now().timestamp()), "f12": "000001", "f14": "合成",
           "f2": 10, "f3": 1, "f62": 10_000_000, "f184": 1.0,
           "f66": -12_000_000, "f69": -1.2, "f72": 22_000_000, "f75": 2.2,
           "f78": 0, "f81": 0, "f84": -10_000_000, "f87": -1.0}
    real_client = httpx.AsyncClient
    calls = []
    def dispatch(request):
        calls.append(request)
        assert {"f66", "f69", "f75", "f81", "f87"} <= set(request.url.params["fields"].split(","))
        return httpx.Response(200, json={"data": {"total": 1, "diff": [raw]}})
    def factory(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(dispatch))
    monkeypatch.setattr("app.data.sources.eastmoney_source.httpx.AsyncClient", factory)
    df = await EastMoneySource().get_individual_fund_flow()
    observed = df.attrs["fund_flow_observed_at"]
    record = DataScheduler._parse_individual_fund_flow_df(
        df, observed.date(), source=df.attrs["fund_flow_source"],
        source_version=df.attrs["fund_flow_source_version"], observed_at=observed,
    )[0]
    assert {key: record[key] for key in VALUES} == VALUES
    assert record["source_quote_at"] <= record["received_at"] <= record["observed_at"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_alembic_upgrade_advances_only_isolated_026_schema(tmp_path):
    import asyncio
    import os
    import sys

    path = (tmp_path / "cli_026.db").resolve()
    assert path.is_relative_to(tmp_path.resolve())
    url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(sa.text("CREATE TABLE fund_flow (id INTEGER PRIMARY KEY, code TEXT, main_net_inflow FLOAT)"))
            await conn.execute(sa.text("INSERT INTO fund_flow VALUES (11, '000001', 123)"))
            await conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            await conn.execute(sa.text("INSERT INTO alembic_version VALUES ('026_auction_evidence')"))
        await engine.dispose()
        proc = await asyncio.create_subprocess_exec(
            # This test owns the 026 -> 027 funds migration, not future heads.
            sys.executable, "-B", "-m", "alembic", "upgrade", "027_fund_order_breakdown",
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "DATABASE_URL": url, "CLAW_DISABLE_SCHEDULER": "1",
                 "PYTHONDONTWRITEBYTECODE": "1", "SQL_ECHO": "false"},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        assert proc.returncode == 0, (stdout + stderr).decode()
        async with engine.connect() as conn:
            assert await conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "027_fund_order_breakdown"
            assert (await conn.execute(sa.text("SELECT id,code,main_net_inflow FROM fund_flow"))).one() == (11, "000001", 123)
            assert (await conn.execute(sa.text("SELECT " + ",".join(NEW_FIELDS) + " FROM fund_flow"))).one() == (None,) * 5
    finally:
        await engine.dispose()
